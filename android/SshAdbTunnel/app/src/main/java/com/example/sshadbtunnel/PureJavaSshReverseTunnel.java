package com.example.sshadbtunnel;

import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.math.BigInteger;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.PublicKey;
import java.security.SecureRandom;
import java.security.Signature;
import java.security.interfaces.ECPublicKey;
import java.security.spec.ECGenParameterSpec;
import java.security.spec.ECParameterSpec;
import java.security.spec.ECPoint;
import java.security.spec.ECPublicKeySpec;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.RSAPrivateCrtKeySpec;
import java.security.spec.RSAPublicKeySpec;
import java.security.spec.X509EncodedKeySpec;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import javax.crypto.Cipher;
import javax.crypto.KeyAgreement;
import javax.crypto.Mac;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * 纯系统库实现的 SSH-2.0 反向隧道客户端 (RFC 4253 / 4252 / 4254 / 5656 / 8332)
 *
 * 面向"长时间挂机 + 屏幕推流"场景的强化版：
 *  - 支持会话中密钥重协商 (rekey)。OpenSSH 默认 1GB 或 1 小时强制 rekey，
 *    不支持的话隧道必然每小时断一次。
 *  - rekey 时校验主机密钥指纹不变，防止重协商期间被中间人替换。
 *  - 内置 keepalive@openssh.com 心跳与静默检测，可及时发现半开连接。
 *  - 通道接收窗口按半窗批量归还，减少 WINDOW_ADJUST 包数量（推流时降低开销）。
 *  - 加大 socket 收发缓冲，适配高带宽低延迟的视频流。
 */
public class PureJavaSshReverseTunnel {

    public interface Logger {
        void log(String message);
    }

    private static final byte SSH_MSG_DISCONNECT = 1;
    private static final byte SSH_MSG_IGNORE = 2;
    private static final byte SSH_MSG_UNIMPLEMENTED = 3;
    private static final byte SSH_MSG_DEBUG = 4;
    private static final byte SSH_MSG_SERVICE_REQUEST = 5;
    private static final byte SSH_MSG_SERVICE_ACCEPT = 6;
    private static final byte SSH_MSG_EXT_INFO = 7;
    private static final byte SSH_MSG_KEXINIT = 20;
    private static final byte SSH_MSG_NEWKEYS = 21;
    private static final byte SSH_MSG_KEX_ECDH_INIT = 30;
    private static final byte SSH_MSG_KEX_ECDH_REPLY = 31;
    private static final byte SSH_MSG_USERAUTH_REQUEST = 50;
    private static final byte SSH_MSG_USERAUTH_FAILURE = 51;
    private static final byte SSH_MSG_USERAUTH_SUCCESS = 52;
    private static final byte SSH_MSG_USERAUTH_BANNER = 53;
    private static final byte SSH_MSG_GLOBAL_REQUEST = 80;
    private static final byte SSH_MSG_REQUEST_SUCCESS = 81;
    private static final byte SSH_MSG_REQUEST_FAILURE = 82;
    private static final byte SSH_MSG_CHANNEL_OPEN = 90;
    private static final byte SSH_MSG_CHANNEL_OPEN_CONFIRMATION = 91;
    private static final byte SSH_MSG_CHANNEL_OPEN_FAILURE = 92;
    private static final byte SSH_MSG_CHANNEL_WINDOW_ADJUST = 93;
    private static final byte SSH_MSG_CHANNEL_DATA = 94;
    private static final byte SSH_MSG_CHANNEL_EXTENDED_DATA = 95;
    private static final byte SSH_MSG_CHANNEL_EOF = 96;
    private static final byte SSH_MSG_CHANNEL_CLOSE = 97;
    private static final byte SSH_MSG_CHANNEL_REQUEST = 98;
    private static final byte SSH_MSG_CHANNEL_SUCCESS = 99;
    private static final byte SSH_MSG_CHANNEL_FAILURE = 100;

    private static final String KEX_ALG = "ecdh-sha2-nistp256";
    private static final String CIPHER_ALG = "aes128-ctr";
    private static final String MAC_ALG = "hmac-sha2-256";

    /** 单个通道我方接收窗口。推流场景调大，减少窗口等待。 */
    private static final int CLIENT_WINDOW = 8 * 1024 * 1024;
    private static final int CLIENT_MAX_PACKET = 32768;

    /** 读缓冲大小，尽量贴近 CHANNEL_DATA 单包上限。 */
    private static final int READ_CHUNK = 32768;

    private static final int SOCKET_BUFFER = 512 * 1024;

    private final SecureRandom random = new SecureRandom();
    private final Object writeLock = new Object();

    private Socket sshSocket;
    private DataInputStream in;
    private OutputStream out;

    private volatile Cipher encCipher;
    private volatile Cipher decCipher;
    private volatile Mac encMac;
    private volatile Mac decMac;
    private int sendSeq = 0;
    private int recvSeq = 0;
    private volatile boolean outboundEncrypted = false;
    private volatile boolean inboundEncrypted = false;
    private volatile boolean connected = false;

    private final AtomicInteger nextLocalChannelId = new AtomicInteger(100);
    private final Map<Integer, ForwardedChannel> channels = new ConcurrentHashMap<>();

    private String hostKeyFingerprint = "";
    private String localAdbHost = "127.0.0.1";
    private int localAdbPort = 0;
    private String clientVersion;
    private String serverVersion;
    private byte[] sessionId;
    private Logger logger;
    private final AtomicInteger rekeyCount = new AtomicInteger(0);

    private volatile long lastReceivedAt = System.currentTimeMillis();
    private volatile long lastSentAt = System.currentTimeMillis();

    public String getHostKeyFingerprint() {
        return hostKeyFingerprint;
    }

    public int getRekeyCount() {
        return rekeyCount.get();
    }

    public long getLastReceivedAt() {
        return lastReceivedAt;
    }

    public void connectAndForwardR(
            TunnelConfig config,
            final int localAdbPort,
            final Logger logger
    ) throws Exception {
        connectAndForwardR(config, "127.0.0.1", localAdbPort, logger);
    }

    /**
     * @param localAdbHost 手机侧 adbd 的地址。mDNS 解析出来可能是 Wi-Fi 地址而不是回环地址，
     *                     这种情况下必须用解析到的地址连接，否则桥接会失败。
     */
    public void connectAndForwardR(
            TunnelConfig config,
            String localAdbHost,
            final int localAdbPort,
            final Logger logger
    ) throws Exception {
        this.localAdbHost = (localAdbHost == null || localAdbHost.isEmpty())
                ? "127.0.0.1" : localAdbHost;
        this.localAdbPort = localAdbPort;
        this.logger = logger;
        this.clientVersion = "SSH-2.0-AndroidAdbTunnel_1.4";

        sshSocket = new Socket();
        sshSocket.setTcpNoDelay(true);
        sshSocket.setKeepAlive(true);
        try {
            sshSocket.setSendBufferSize(SOCKET_BUFFER);
            sshSocket.setReceiveBufferSize(SOCKET_BUFFER);
        } catch (Exception ignored) {
        }
        sshSocket.connect(new InetSocketAddress(config.serverIp, config.sshPort), 15000);
        sshSocket.setSoTimeout(20000);

        in = new DataInputStream(sshSocket.getInputStream());
        out = sshSocket.getOutputStream();

        // 1. 版本协商
        out.write((clientVersion + "\r\n").getBytes(StandardCharsets.US_ASCII));
        out.flush();
        serverVersion = readServerVersionLine();
        logger.log("SSH 握手: 服务端版本 " + serverVersion);

        // 2. 首次密钥交换（全程持锁，避免与通道数据交叉）
        synchronized (writeLock) {
            byte[] clientKexInit = buildKexInitPayload();
            sendPacket(clientKexInit);
            byte[] serverKexInit = readPacket();
            if (serverKexInit.length == 0 || serverKexInit[0] != SSH_MSG_KEXINIT) {
                throw new IOException("期望 SSH_MSG_KEXINIT(20)，实际收到 "
                        + (serverKexInit.length > 0 ? serverKexInit[0] : -1));
            }
            performKex(clientKexInit, serverKexInit, true);
        }

        // 3. 认证
        PacketWriter serviceReq = new PacketWriter();
        serviceReq.writeByte(SSH_MSG_SERVICE_REQUEST);
        serviceReq.writeString("ssh-userauth");
        sendPacket(serviceReq.toByteArray());

        byte[] serviceAccept = readNextNonIgnorePacket();
        if (serviceAccept[0] != SSH_MSG_SERVICE_ACCEPT) {
            throw new IOException("服务端拒绝 ssh-userauth 服务");
        }

        if (config.authType == TunnelConfig.AuthType.PASSWORD) {
            if (config.password == null || config.password.isEmpty()) {
                throw new IOException("已选择密码认证但密码为空");
            }
            PacketWriter authReq = new PacketWriter();
            authReq.writeByte(SSH_MSG_USERAUTH_REQUEST);
            authReq.writeString(config.username);
            authReq.writeString("ssh-connection");
            authReq.writeString("password");
            authReq.writeBoolean(false);
            authReq.writeString(config.password);
            sendPacket(authReq.toByteArray());
        } else {
            sendPublicKeyAuth(config.username, config.privateKey, config.keyPassphrase, sessionId);
        }

        while (true) {
            byte[] authResp = readNextNonIgnorePacket();
            if (authResp[0] == SSH_MSG_USERAUTH_BANNER) {
                PacketReader br = new PacketReader(authResp, 1);
                logger.log("服务端公告: " + br.readString().trim());
                continue;
            }
            if (authResp[0] == SSH_MSG_USERAUTH_SUCCESS) {
                logger.log("SSH 身份认证通过！");
                break;
            }
            if (authResp[0] == SSH_MSG_USERAUTH_FAILURE) {
                PacketReader fr = new PacketReader(authResp, 1);
                String methods = fr.readString();
                boolean partial = fr.readBoolean();
                throw new IOException("SSH 认证失败，服务端可接受的方法: " + methods
                        + (partial ? " (部分成功)" : ""));
            }
            throw new IOException("认证阶段收到意外消息类型: " + authResp[0]);
        }

        // 4. tcpip-forward 绑定服务器端口 P
        //
        // 注意绑定地址的写法：OpenSSH 自己的客户端执行 `ssh -R 5556:...` 时，
        // 发给服务端的监听地址是字符串 "localhost"（不是 "127.0.0.1"）。
        // sshd 在 GatewayPorts=no 时对监听地址有白名单校验，某些版本/发行版
        // 只认 "localhost"，写成 "127.0.0.1" 会被直接回 REQUEST_FAILURE。
        // 因此这里按 "localhost" -> "127.0.0.1" 顺序尝试，哪个成功用哪个。
        String boundAddress = null;
        String lastFailure = null;

        for (String bindAddr : new String[]{"localhost", "127.0.0.1"}) {
            ForwardRequestResult result = requestTcpipForward(bindAddr, config.remotePortP, logger);
            if (result.success) {
                boundAddress = bindAddr;
                break;
            }
            lastFailure = result.detail;
            logger.log("绑定 " + bindAddr + ":" + config.remotePortP + " 失败: " + result.detail);
            if (result.fatal) {
                throw new IOException("建立反向转发时服务端断开连接: " + result.detail);
            }
        }

        if (boundAddress == null) {
            throw new IOException("服务器拒绝绑定反向端口 " + config.remotePortP
                    + "，已尝试 localhost / 127.0.0.1。最后一次结果: " + lastFailure
                    + "。请检查服务端 AllowTcpForwarding、GatewayPorts、PermitListen 配置，"
                    + "或该端口是否被上次会话残留占用（TCP 超时前不会释放）");
        }

        sshSocket.setSoTimeout(0);
        connected = true;
        logger.log("反向转发已建立: 服务器 " + boundAddress + ":" + config.remotePortP
                + " -> 手机 " + this.localAdbHost + ":" + localAdbPort);
    }

    private static final class ForwardRequestResult {
        final boolean success;
        final boolean fatal;
        final String detail;

        private ForwardRequestResult(boolean success, boolean fatal, String detail) {
            this.success = success;
            this.fatal = fatal;
            this.detail = detail;
        }

        static ForwardRequestResult ok(String detail) {
            return new ForwardRequestResult(true, false, detail);
        }

        static ForwardRequestResult fail(String detail) {
            return new ForwardRequestResult(false, false, detail);
        }

        static ForwardRequestResult fatal(String detail) {
            return new ForwardRequestResult(false, true, detail);
        }
    }

    /**
     * 发送 tcpip-forward 全局请求并等待应答。
     *
     * 应答不一定是紧随其后的第一个包：服务端可能先发来 IGNORE / DEBUG / EXT_INFO，
     * 甚至它自己的 GLOBAL_REQUEST。必须循环读到 REQUEST_SUCCESS / REQUEST_FAILURE 为止，
     * 否则会把"别的包"误判成"拒绝绑定"。
     */
    private ForwardRequestResult requestTcpipForward(String bindAddress, int port, Logger logger)
            throws Exception {
        PacketWriter req = new PacketWriter();
        req.writeByte(SSH_MSG_GLOBAL_REQUEST);
        req.writeString("tcpip-forward");
        req.writeBoolean(true);
        req.writeString(bindAddress);
        req.writeUint32(port);
        sendPacket(req.toByteArray());

        while (true) {
            byte[] resp;
            try {
                resp = readPacket();
            } catch (EOFException | java.net.SocketTimeoutException | java.net.SocketException e) {
                return ForwardRequestResult.fatal("等待应答时连接中断: "
                        + e.getClass().getSimpleName() + " " + e.getMessage());
            }
            if (resp.length == 0) continue;
            byte t = resp[0];

            if (t == SSH_MSG_REQUEST_SUCCESS) {
                String extra = "";
                if (resp.length >= 5) {
                    int allocated = ((resp[1] & 0xFF) << 24) | ((resp[2] & 0xFF) << 16)
                            | ((resp[3] & 0xFF) << 8) | (resp[4] & 0xFF);
                    if (allocated != 0) {
                        extra = " (服务端分配端口 " + allocated + ")";
                    }
                }
                return ForwardRequestResult.ok(bindAddress + ":" + port + extra);
            }
            if (t == SSH_MSG_REQUEST_FAILURE) {
                return ForwardRequestResult.fail("服务端返回 SSH_MSG_REQUEST_FAILURE(82)");
            }
            if (t == SSH_MSG_DISCONNECT) {
                int reason = -1;
                String desc = "";
                try {
                    PacketReader pr = new PacketReader(resp, 1);
                    reason = pr.readUint32();
                    desc = pr.readString();
                } catch (Exception ignored) {
                }
                connected = false;
                return ForwardRequestResult.fatal("服务端断开 reason=" + reason + " " + desc);
            }
            if (t == SSH_MSG_GLOBAL_REQUEST) {
                PacketReader pr = new PacketReader(resp, 1);
                String name = pr.readString();
                boolean wantReply = pr.readBoolean();
                logger.log("服务端发来全局请求: " + name);
                if (wantReply) {
                    sendPacket(new byte[]{SSH_MSG_REQUEST_FAILURE});
                }
                continue;
            }
            if (t == SSH_MSG_IGNORE || t == SSH_MSG_DEBUG
                    || t == SSH_MSG_UNIMPLEMENTED || t == SSH_MSG_EXT_INFO) {
                continue;
            }
            logger.log("等待 tcpip-forward 应答时收到消息类型 " + t + "，忽略后继续等待");
        }
    }

    // ==================================================================
    // 密钥交换（首次 + 重协商共用）
    // ==================================================================

    /**
     * 执行一次完整的 ECDH 密钥交换并切换会话密钥。
     *
     * @param clientKexInit 已发送的我方 KEXINIT 负载
     * @param serverKexInit 已收到的服务端 KEXINIT 负载
     * @param initial       true 表示首次交换（会写入 sessionId）；false 为重协商（沿用原 sessionId）
     */
    private void performKex(byte[] clientKexInit, byte[] serverKexInit, boolean initial)
            throws Exception {
        ServerKexInit serverKex = parseKexInit(serverKexInit);
        negotiate(serverKex, logger);
        if (serverKex.firstKexPacketFollows
                && (serverKex.kex.isEmpty() || !KEX_ALG.equals(serverKex.kex.get(0)))) {
            readPacket(); // 服务端猜测错误时多发的包需要丢弃
        }

        KeyPairGenerator kpg = KeyPairGenerator.getInstance("EC");
        kpg.initialize(new ECGenParameterSpec("secp256r1"), random);
        KeyPair clientKp = kpg.generateKeyPair();
        ECPublicKey clientEcPub = (ECPublicKey) clientKp.getPublic();
        ECParameterSpec ecParams = clientEcPub.getParams();
        byte[] qC = encodeUncompressedEcPoint(clientEcPub.getW());

        PacketWriter ecdhInit = new PacketWriter();
        ecdhInit.writeByte(SSH_MSG_KEX_ECDH_INIT);
        ecdhInit.writeByteString(qC);
        sendPacket(ecdhInit.toByteArray());

        byte[] kexReply = readPacket();
        if (kexReply.length == 0 || kexReply[0] != SSH_MSG_KEX_ECDH_REPLY) {
            throw new IOException("期望 SSH_MSG_KEX_ECDH_REPLY(31)，实际收到 "
                    + (kexReply.length > 0 ? kexReply[0] : -1));
        }
        PacketReader replyReader = new PacketReader(kexReply, 1);
        byte[] kS = replyReader.readByteString();
        byte[] qS = replyReader.readByteString();
        byte[] sigBlob = replyReader.readByteString();

        PublicKey serverEcPub = decodeUncompressedEcPoint(qS, ecParams);
        KeyAgreement ka = KeyAgreement.getInstance("ECDH");
        ka.init(clientKp.getPrivate());
        ka.doPhase(serverEcPub, true);
        byte[] kMpint = new BigInteger(1, ka.generateSecret()).toByteArray();

        // H = SHA-256(V_C || V_S || I_C || I_S || K_S || Q_C || Q_S || K)
        MessageDigest sha256 = MessageDigest.getInstance("SHA-256");
        PacketWriter hashInput = new PacketWriter();
        hashInput.writeString(clientVersion);
        hashInput.writeString(serverVersion);
        hashInput.writeByteString(clientKexInit);
        hashInput.writeByteString(serverKexInit);
        hashInput.writeByteString(kS);
        hashInput.writeByteString(qC);
        hashInput.writeByteString(qS);
        hashInput.writeByteString(kMpint);
        byte[] h = sha256.digest(hashInput.toByteArray());

        String fingerprint = sha256Fingerprint(kS);
        if (initial) {
            hostKeyFingerprint = fingerprint;
            sessionId = h;
            logger.log("服务端主机密钥 SHA256 指纹: " + fingerprint);
        } else {
            if (!fingerprint.equals(hostKeyFingerprint)) {
                throw new IOException("重协商时主机密钥发生变化！原指纹 " + hostKeyFingerprint
                        + "，新指纹 " + fingerprint + "，可能存在中间人攻击，已中止连接");
            }
            logger.log("重协商主机密钥指纹一致: " + fingerprint);
        }
        verifyHostKeySignature(kS, sigBlob, h, logger);

        // 先发 NEWKEYS（用旧密钥加密），随后立即切换到新的发送密钥
        sendPacket(new byte[]{SSH_MSG_NEWKEYS});
        KeyMaterial km = deriveKeyMaterial(kMpint, h, sessionId);
        applyOutboundKeys(km);

        // 再收服务端 NEWKEYS（旧密钥解密），随后切换接收密钥
        byte[] serverNewKeys = readPacket();
        if (serverNewKeys.length == 0 || serverNewKeys[0] != SSH_MSG_NEWKEYS) {
            throw new IOException("期望 SSH_MSG_NEWKEYS(21)，实际收到 "
                    + (serverNewKeys.length > 0 ? serverNewKeys[0] : -1));
        }
        applyInboundKeys(km);

        if (!initial) {
            int n = rekeyCount.incrementAndGet();
            logger.log("密钥重协商完成 (第 " + n + " 次)，序列号连续，通道不受影响");
        }
    }

    private static final class KeyMaterial {
        byte[] ivC2S, ivS2C, keyC2S, keyS2C, macC2S, macS2C;
    }

    private KeyMaterial deriveKeyMaterial(byte[] kMpint, byte[] h, byte[] sid) throws Exception {
        KeyMaterial km = new KeyMaterial();
        km.ivC2S = deriveKey(kMpint, h, (byte) 'A', sid, 16);
        km.ivS2C = deriveKey(kMpint, h, (byte) 'B', sid, 16);
        km.keyC2S = deriveKey(kMpint, h, (byte) 'C', sid, 16);
        km.keyS2C = deriveKey(kMpint, h, (byte) 'D', sid, 16);
        km.macC2S = deriveKey(kMpint, h, (byte) 'E', sid, 32);
        km.macS2C = deriveKey(kMpint, h, (byte) 'F', sid, 32);
        return km;
    }

    private void applyOutboundKeys(KeyMaterial km) throws Exception {
        Cipher c = Cipher.getInstance("AES/CTR/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(km.keyC2S, "AES"), new IvParameterSpec(km.ivC2S));
        Mac m = Mac.getInstance("HmacSHA256");
        m.init(new SecretKeySpec(km.macC2S, "HmacSHA256"));
        encCipher = c;
        encMac = m;
        outboundEncrypted = true;
    }

    private void applyInboundKeys(KeyMaterial km) throws Exception {
        Cipher c = Cipher.getInstance("AES/CTR/NoPadding");
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(km.keyS2C, "AES"), new IvParameterSpec(km.ivS2C));
        Mac m = Mac.getInstance("HmacSHA256");
        m.init(new SecretKeySpec(km.macS2C, "HmacSHA256"));
        decCipher = c;
        decMac = m;
        inboundEncrypted = true;
    }

    /** 由服务端发起重协商时调用。 */
    private void handleServerInitiatedRekey(byte[] serverKexInit) throws Exception {
        logger.log("服务端发起密钥重协商 (rekey)，开始处理...");
        synchronized (writeLock) {
            byte[] clientKexInit = buildKexInitPayload();
            sendPacket(clientKexInit);
            performKex(clientKexInit, serverKexInit, false);
        }
    }

    // ==================================================================
    // 转发主循环
    // ==================================================================

    public void runForwardingLoop(final int localAdbPort, final Logger logger) throws Exception {
        runForwardingLoop(logger);
    }

    public void runForwardingLoop(final Logger logger) throws Exception {
        while (connected && !Thread.currentThread().isInterrupted()) {
            byte[] pkt;
            try {
                pkt = readNextNonIgnorePacket();
            } catch (EOFException | java.net.SocketException e) {
                connected = false;
                break;
            }
            if (pkt == null || pkt.length == 0) continue;
            byte msgType = pkt[0];

            if (msgType == SSH_MSG_CHANNEL_OPEN) {
                handleChannelOpen(pkt, logger);
            } else if (msgType == SSH_MSG_CHANNEL_DATA) {
                PacketReader pr = new PacketReader(pkt, 1);
                int recipientChannel = pr.readUint32();
                byte[] data = pr.readByteString();
                ForwardedChannel ch = channels.get(recipientChannel);
                if (ch != null && !ch.closed) {
                    try {
                        ch.adbOut.write(data);
                        ch.adbOut.flush();
                    } catch (IOException e) {
                        closeChannel(ch);
                        continue;
                    }
                    adjustReceiveWindow(ch, data.length);
                }
            } else if (msgType == SSH_MSG_CHANNEL_EXTENDED_DATA) {
                PacketReader pr = new PacketReader(pkt, 1);
                int recipientChannel = pr.readUint32();
                pr.readUint32();
                byte[] data = pr.readByteString();
                ForwardedChannel ch = channels.get(recipientChannel);
                if (ch != null && !ch.closed) {
                    adjustReceiveWindow(ch, data.length);
                }
            } else if (msgType == SSH_MSG_CHANNEL_WINDOW_ADJUST) {
                PacketReader pr = new PacketReader(pkt, 1);
                int recipientChannel = pr.readUint32();
                long add = pr.readUint32() & 0xFFFFFFFFL;
                ForwardedChannel ch = channels.get(recipientChannel);
                if (ch != null) {
                    synchronized (ch.windowLock) {
                        ch.remoteWindow += add;
                        ch.windowLock.notifyAll();
                    }
                }
            } else if (msgType == SSH_MSG_CHANNEL_EOF) {
                PacketReader pr = new PacketReader(pkt, 1);
                ForwardedChannel ch = channels.get(pr.readUint32());
                if (ch != null) closeChannel(ch);
            } else if (msgType == SSH_MSG_CHANNEL_CLOSE) {
                PacketReader pr = new PacketReader(pkt, 1);
                ForwardedChannel ch = channels.remove(pr.readUint32());
                if (ch != null) closeChannel(ch);
            } else if (msgType == SSH_MSG_CHANNEL_REQUEST) {
                PacketReader pr = new PacketReader(pkt, 1);
                int recipientChannel = pr.readUint32();
                pr.readString();
                boolean wantReply = pr.readBoolean();
                if (wantReply) {
                    PacketWriter chFail = new PacketWriter();
                    chFail.writeByte(SSH_MSG_CHANNEL_FAILURE);
                    chFail.writeUint32(recipientChannel);
                    sendPacket(chFail.toByteArray());
                }
            } else if (msgType == SSH_MSG_GLOBAL_REQUEST) {
                PacketReader pr = new PacketReader(pkt, 1);
                pr.readString();
                boolean wantReply = pr.readBoolean();
                if (wantReply) {
                    sendPacket(new byte[]{SSH_MSG_REQUEST_FAILURE});
                }
            } else if (msgType == SSH_MSG_KEXINIT) {
                // OpenSSH 默认 1GB / 1h 触发一次，必须应答，否则连接被断开
                handleServerInitiatedRekey(pkt);
            } else if (msgType == SSH_MSG_DISCONNECT) {
                PacketReader pr = new PacketReader(pkt, 1);
                int reason = pr.readUint32();
                String desc = "";
                try { desc = pr.readString(); } catch (Exception ignored) { }
                logger.log("服务端断开连接 (reason=" + reason + "): " + desc);
                connected = false;
                break;
            }
        }
    }

    /** 半窗批量归还，减少 WINDOW_ADJUST 包数量。 */
    private void adjustReceiveWindow(ForwardedChannel ch, int consumed) throws Exception {
        ch.consumedSinceAdjust += consumed;
        if (ch.consumedSinceAdjust >= CLIENT_WINDOW / 2) {
            int amount = ch.consumedSinceAdjust;
            ch.consumedSinceAdjust = 0;
            PacketWriter win = new PacketWriter();
            win.writeByte(SSH_MSG_CHANNEL_WINDOW_ADJUST);
            win.writeUint32(ch.serverChannelId);
            win.writeUint32(amount);
            sendPacket(win.toByteArray());
        }
    }

    private void handleChannelOpen(byte[] pkt, Logger logger) throws Exception {
        PacketReader pr = new PacketReader(pkt, 1);
        String channelType = pr.readString();
        final int serverChannelId = pr.readUint32();
        long initialWindow = pr.readUint32() & 0xFFFFFFFFL;
        int maxPacket = pr.readUint32();

        if (!"forwarded-tcpip".equals(channelType)) {
            PacketWriter fail = new PacketWriter();
            fail.writeByte(SSH_MSG_CHANNEL_OPEN_FAILURE);
            fail.writeUint32(serverChannelId);
            fail.writeUint32(3); // SSH_OPEN_UNKNOWN_CHANNEL_TYPE
            fail.writeString("Only forwarded-tcpip is supported");
            fail.writeString("");
            sendPacket(fail.toByteArray());
            logger.log("拒绝不支持的通道类型: " + channelType);
            return;
        }

        String connectedAddr = pr.readString();
        int connectedPort = pr.readUint32();
        String originAddr = pr.readString();
        int originPort = pr.readUint32();

        final int localChannelId = nextLocalChannelId.getAndIncrement();
        Socket adbSocket = connectLocalAdb();
        if (adbSocket == null) {
            PacketWriter fail = new PacketWriter();
            fail.writeByte(SSH_MSG_CHANNEL_OPEN_FAILURE);
            fail.writeUint32(serverChannelId);
            fail.writeUint32(2); // SSH_OPEN_CONNECT_FAILED
            fail.writeString("Failed to connect local ADB endpoint");
            fail.writeString("");
            sendPacket(fail.toByteArray());
            logger.log("本机 adb 端点连接失败 (" + localAdbHost + ":" + localAdbPort
                    + " 及 127.0.0.1:" + localAdbPort + ")，已回绝转发请求");
            return;
        }

        ForwardedChannel ch = new ForwardedChannel(localChannelId, serverChannelId, adbSocket);
        ch.remoteWindow = initialWindow;
        ch.remoteMaxPacket = maxPacket <= 0 ? 32768 : maxPacket;
        channels.put(localChannelId, ch);

        PacketWriter confirm = new PacketWriter();
        confirm.writeByte(SSH_MSG_CHANNEL_OPEN_CONFIRMATION);
        confirm.writeUint32(serverChannelId);
        confirm.writeUint32(localChannelId);
        confirm.writeUint32(CLIENT_WINDOW);
        confirm.writeUint32(CLIENT_MAX_PACKET);
        sendPacket(confirm.toByteArray());

        logger.log("转发连接 " + originAddr + ":" + originPort + " -> " + connectedAddr + ":" + connectedPort
                + "，已桥接至本机 " + localAdbHost + ":" + localAdbPort);
        startLocalAdbReader(ch, logger);
    }

    /**
     * 连接手机侧 adbd。优先用 mDNS 解析出来的地址（可能就是 Wi-Fi 地址），
     * 失败再退回 127.0.0.1，两者都试过仍失败才返回 null。
     */
    private Socket connectLocalAdb() {
        List<InetSocketAddress> candidates = new ArrayList<>();
        candidates.add(new InetSocketAddress(localAdbHost, localAdbPort));
        if (!"127.0.0.1".equals(localAdbHost) && !"localhost".equals(localAdbHost)) {
            candidates.add(new InetSocketAddress("127.0.0.1", localAdbPort));
        }

        for (InetSocketAddress target : candidates) {
            Socket s = new Socket();
            try {
                s.setTcpNoDelay(true);
                s.setKeepAlive(true);
                try {
                    s.setSendBufferSize(SOCKET_BUFFER);
                    s.setReceiveBufferSize(SOCKET_BUFFER);
                } catch (Exception ignored) {
                }
                s.connect(target, 3000);
                return s;
            } catch (Exception e) {
                try { s.close(); } catch (Exception ignored) { }
            }
        }
        return null;
    }

    private void startLocalAdbReader(final ForwardedChannel ch, final Logger logger) {
        new Thread(() -> {
            byte[] buf = new byte[READ_CHUNK];
            try {
                int n;
                while (connected && !ch.closed && (n = ch.adbIn.read(buf)) != -1) {
                    if (!sendChannelData(ch, buf, n)) break;
                }
            } catch (Exception ignored) {
            } finally {
                closeChannel(ch);
            }
        }, "LocalAdbBridge-" + ch.localChannelId).start();
    }

    private boolean sendChannelData(ForwardedChannel ch, byte[] buf, int len) throws Exception {
        int sent = 0;
        while (sent < len) {
            if (!connected || ch.closed) return false;
            int chunk;
            synchronized (ch.windowLock) {
                while (ch.remoteWindow <= 0 && connected && !ch.closed) {
                    ch.windowLock.wait(500);
                }
                if (!connected || ch.closed) return false;
                int cap = Math.max(1024, Math.min(ch.remoteMaxPacket, CLIENT_MAX_PACKET));
                chunk = (int) Math.min(Math.min(len - sent, ch.remoteWindow), cap);
                ch.remoteWindow -= chunk;
            }
            PacketWriter dataPkt = new PacketWriter();
            dataPkt.writeByte(SSH_MSG_CHANNEL_DATA);
            dataPkt.writeUint32(ch.serverChannelId);
            dataPkt.writeByteString(Arrays.copyOfRange(buf, sent, sent + chunk));
            sendPacket(dataPkt.toByteArray());
            sent += chunk;
        }
        return true;
    }

    private void closeChannel(ForwardedChannel ch) {
        if (ch.closed) return;
        ch.closed = true;
        channels.remove(ch.localChannelId);
        try {
            ch.adbSocket.close();
        } catch (Exception ignored) {
        }
        synchronized (ch.windowLock) {
            ch.windowLock.notifyAll();
        }
        if (connected && !ch.closeSent) {
            ch.closeSent = true;
            try {
                PacketWriter closePkt = new PacketWriter();
                closePkt.writeByte(SSH_MSG_CHANNEL_CLOSE);
                closePkt.writeUint32(ch.serverChannelId);
                sendPacket(closePkt.toByteArray());
            } catch (Exception ignored) {
            }
        }
    }

    // ==================================================================
    // 心跳
    // ==================================================================

    /** 发送 keepalive 全局请求；服务端会回 REQUEST_FAILURE/SUCCESS，由此可判断链路存活。 */
    public void sendKeepAlive() throws Exception {
        if (!connected) return;
        synchronized (writeLock) {
            if (!connected) return;
            PacketWriter pw = new PacketWriter();
            pw.writeByte(SSH_MSG_GLOBAL_REQUEST);
            pw.writeString("keepalive@openssh.com");
            pw.writeBoolean(true);
            sendPacket(pw.toByteArray());
        }
    }

    public void disconnect() {
        boolean wasConnected = connected;
        connected = false;
        for (ForwardedChannel ch : channels.values()) {
            synchronized (ch.windowLock) {
                ch.windowLock.notifyAll();
            }
            try {
                ch.adbSocket.close();
            } catch (Exception ignored) {
            }
        }
        channels.clear();
        if (wasConnected) {
            try {
                PacketWriter disc = new PacketWriter();
                disc.writeByte(SSH_MSG_DISCONNECT);
                disc.writeUint32(11); // SSH_DISCONNECT_BY_APPLICATION
                disc.writeString("client shutting down");
                disc.writeString("");
                sendPacket(disc.toByteArray());
            } catch (Exception ignored) {
            }
        }
        if (sshSocket != null) {
            try {
                sshSocket.close();
            } catch (Exception ignored) {
            }
        }
    }

    public boolean isConnected() {
        return connected && sshSocket != null && sshSocket.isConnected() && !sshSocket.isClosed();
    }

    // ==================================================================
    // 认证
    // ==================================================================

    private void sendPublicKeyAuth(String username, String pemKey, String passphrase, byte[] sessionId)
            throws Exception {
        ParsedKey key = parsePrivateKey(pemKey, passphrase);
        String signAlg = "ssh-rsa".equals(key.algorithm) ? "rsa-sha2-256" : key.algorithm;

        PacketWriter signData = new PacketWriter();
        signData.writeByteString(sessionId);
        signData.writeByte(SSH_MSG_USERAUTH_REQUEST);
        signData.writeString(username);
        signData.writeString("ssh-connection");
        signData.writeString("publickey");
        signData.writeBoolean(true);
        signData.writeString(signAlg);
        signData.writeByteString(key.publicBlob);

        byte[] rawSig;
        Signature sig;
        if ("ssh-ed25519".equals(key.algorithm)) {
            sig = Signature.getInstance("Ed25519");
            sig.initSign(key.privateKey);
            sig.update(signData.toByteArray());
            rawSig = sig.sign();
        } else {
            sig = Signature.getInstance("SHA256withRSA");
            sig.initSign(key.privateKey);
            sig.update(signData.toByteArray());
            rawSig = sig.sign();
        }

        PacketWriter sigBlobWriter = new PacketWriter();
        sigBlobWriter.writeString(signAlg);
        sigBlobWriter.writeByteString(rawSig);

        PacketWriter authReq = new PacketWriter();
        authReq.writeByte(SSH_MSG_USERAUTH_REQUEST);
        authReq.writeString(username);
        authReq.writeString("ssh-connection");
        authReq.writeString("publickey");
        authReq.writeBoolean(true);
        authReq.writeString(signAlg);
        authReq.writeByteString(key.publicBlob);
        authReq.writeByteString(sigBlobWriter.toByteArray());
        sendPacket(authReq.toByteArray());
    }

    private static final class ParsedKey {
        final String algorithm;
        final PrivateKey privateKey;
        final byte[] publicBlob;

        ParsedKey(String algorithm, PrivateKey privateKey, byte[] publicBlob) {
            this.algorithm = algorithm;
            this.privateKey = privateKey;
            this.publicBlob = publicBlob;
        }
    }

    private static ParsedKey parsePrivateKey(String pemText, String passphrase) throws Exception {
        if (pemText == null || pemText.trim().isEmpty()) {
            throw new IOException("已选择私钥认证但私钥内容为空");
        }
        String pem = pemText.replace("\r\n", "\n").trim();
        if (pem.contains("BEGIN OPENSSH PRIVATE KEY")) {
            return parseOpenSshKey(pem);
        }
        if (pem.contains("BEGIN RSA PRIVATE KEY")) {
            return parsePkcs1Rsa(pem);
        }
        if (pem.contains("BEGIN PRIVATE KEY")) {
            return parsePkcs8(pem);
        }
        if (pem.contains("BEGIN ENCRYPTED PRIVATE KEY")) {
            throw new IOException("不支持 ENCRYPTED PRIVATE KEY 格式，请用 ssh-keygen 转换为无口令私钥");
        }
        if (pem.contains("BEGIN EC PRIVATE KEY")) {
            throw new IOException("暂不支持 EC(ECDSA) 客户端私钥，请改用 RSA 或 Ed25519 私钥");
        }
        throw new IOException("无法识别的私钥格式，需要 PKCS#8 / PKCS#1 / OpenSSH 三种之一");
    }

    private static byte[] pemBody(String pem) {
        StringBuilder sb = new StringBuilder();
        for (String line : pem.split("\n")) {
            String t = line.trim();
            if (t.isEmpty() || t.startsWith("-----") || t.contains(":")) continue;
            sb.append(t);
        }
        return Base64.getMimeDecoder().decode(sb.toString());
    }

    private static ParsedKey parseOpenSshKey(String pem) throws Exception {
        byte[] blob = pemBody(pem);
        String magic = "openssh-key-v1";
        if (blob.length < magic.length() + 1) {
            throw new IOException("OpenSSH 私钥数据过短");
        }
        for (int i = 0; i < magic.length(); i++) {
            if (blob[i] != (byte) magic.charAt(i)) {
                throw new IOException("OpenSSH 私钥魔数校验失败");
            }
        }
        PacketReader pr = new PacketReader(blob, magic.length() + 1);
        String cipherName = pr.readString();
        pr.readString();
        pr.readByteString();
        int nKeys = pr.readUint32();
        if (nKeys != 1) {
            throw new IOException("仅支持包含单个密钥的 OpenSSH 私钥 (当前 " + nKeys + " 个)");
        }
        pr.readByteString();
        byte[] privBlob = pr.readByteString();

        if (!"none".equals(cipherName)) {
            throw new IOException("该 OpenSSH 私钥已加密 (" + cipherName
                    + ")，请执行 ssh-keygen -p -N \"\" -f 私钥文件 去掉口令后重试");
        }

        PacketReader kr = new PacketReader(privBlob, 0);
        int c1 = kr.readUint32();
        int c2 = kr.readUint32();
        if (c1 != c2) {
            throw new IOException("OpenSSH 私钥校验位不匹配，口令可能不正确");
        }
        String keyType = kr.readString();

        if ("ssh-rsa".equals(keyType)) {
            BigInteger n = new BigInteger(1, kr.readByteString());
            BigInteger e = new BigInteger(1, kr.readByteString());
            BigInteger d = new BigInteger(1, kr.readByteString());
            BigInteger iqmp = new BigInteger(1, kr.readByteString());
            BigInteger p = new BigInteger(1, kr.readByteString());
            BigInteger q = new BigInteger(1, kr.readByteString());

            BigInteger one = BigInteger.ONE;
            BigInteger dP = d.mod(p.subtract(one));
            BigInteger dQ = d.mod(q.subtract(one));
            KeyFactory kf = KeyFactory.getInstance("RSA");
            PrivateKey priv = kf.generatePrivate(new RSAPrivateCrtKeySpec(n, e, d, p, q, dP, dQ, iqmp));

            PacketWriter pub = new PacketWriter();
            pub.writeString("ssh-rsa");
            pub.writeByteString(e.toByteArray());
            pub.writeByteString(n.toByteArray());
            return new ParsedKey("ssh-rsa", priv, pub.toByteArray());
        }

        if ("ssh-ed25519".equals(keyType)) {
            byte[] pubRaw = kr.readByteString();
            byte[] privRaw = kr.readByteString();
            if (pubRaw.length != 32 || privRaw.length != 64) {
                throw new IOException("Ed25519 私钥长度异常");
            }
            byte[] seed = Arrays.copyOfRange(privRaw, 0, 32);

            byte[] pkcs8 = new byte[16 + 32];
            byte[] head = {
                    0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06,
                    0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20
            };
            System.arraycopy(head, 0, pkcs8, 0, 16);
            System.arraycopy(seed, 0, pkcs8, 16, 32);

            KeyFactory kf = KeyFactory.getInstance("Ed25519");
            PrivateKey priv = kf.generatePrivate(new PKCS8EncodedKeySpec(pkcs8));

            PacketWriter pub = new PacketWriter();
            pub.writeString("ssh-ed25519");
            pub.writeByteString(pubRaw);
            return new ParsedKey("ssh-ed25519", priv, pub.toByteArray());
        }

        throw new IOException("暂不支持的 OpenSSH 密钥类型: " + keyType + "（请使用 RSA 或 Ed25519）");
    }

    private static ParsedKey parsePkcs1Rsa(String pem) throws Exception {
        byte[] der = pemBody(pem);
        DerReader dr = new DerReader(der);
        dr.expect(0x30);
        dr.readLength();
        dr.readInteger();
        BigInteger n = dr.readInteger();
        BigInteger e = dr.readInteger();
        BigInteger d = dr.readInteger();
        BigInteger p = dr.readInteger();
        BigInteger q = dr.readInteger();
        BigInteger dP = dr.readInteger();
        BigInteger dQ = dr.readInteger();
        BigInteger qInv = dr.readInteger();

        KeyFactory kf = KeyFactory.getInstance("RSA");
        PrivateKey priv = kf.generatePrivate(new RSAPrivateCrtKeySpec(n, e, d, p, q, dP, dQ, qInv));

        PacketWriter pub = new PacketWriter();
        pub.writeString("ssh-rsa");
        pub.writeByteString(e.toByteArray());
        pub.writeByteString(n.toByteArray());
        return new ParsedKey("ssh-rsa", priv, pub.toByteArray());
    }

    private static ParsedKey parsePkcs8(String pem) throws Exception {
        byte[] der = pemBody(pem);
        KeyFactory rsa = KeyFactory.getInstance("RSA");
        try {
            PrivateKey priv = rsa.generatePrivate(new PKCS8EncodedKeySpec(der));
            java.security.interfaces.RSAPrivateCrtKey crt =
                    (java.security.interfaces.RSAPrivateCrtKey) priv;
            PacketWriter pub = new PacketWriter();
            pub.writeString("ssh-rsa");
            pub.writeByteString(crt.getPublicExponent().toByteArray());
            pub.writeByteString(crt.getModulus().toByteArray());
            return new ParsedKey("ssh-rsa", priv, pub.toByteArray());
        } catch (Exception rsaErr) {
            try {
                KeyFactory ed = KeyFactory.getInstance("Ed25519");
                PrivateKey priv = ed.generatePrivate(new PKCS8EncodedKeySpec(der));
                byte[] pubRaw = Arrays.copyOfRange(der, der.length - 32, der.length);
                PacketWriter pub = new PacketWriter();
                pub.writeString("ssh-ed25519");
                pub.writeByteString(pubRaw);
                return new ParsedKey("ssh-ed25519", priv, pub.toByteArray());
            } catch (Exception edErr) {
                throw new IOException("PKCS#8 私钥既不是 RSA 也不是 Ed25519: " + rsaErr.getMessage());
            }
        }
    }

    private static final class DerReader {
        private final byte[] buf;
        private int pos;

        DerReader(byte[] buf) {
            this.buf = buf;
        }

        void expect(int tag) throws IOException {
            int t = buf[pos++] & 0xFF;
            if (t != tag) {
                throw new IOException("DER 结构异常: 期望 tag 0x"
                        + Integer.toHexString(tag) + "，实际 0x" + Integer.toHexString(t));
            }
        }

        int readLength() {
            int len = buf[pos++] & 0xFF;
            if ((len & 0x80) != 0) {
                int n = len & 0x7F;
                len = 0;
                for (int i = 0; i < n; i++) {
                    len = (len << 8) | (buf[pos++] & 0xFF);
                }
            }
            return len;
        }

        BigInteger readInteger() {
            expectTag(0x02);
            int len = readLength();
            BigInteger v = new BigInteger(Arrays.copyOfRange(buf, pos, pos + len));
            pos += len;
            return v;
        }

        private void expectTag(int tag) {
            int t = buf[pos++] & 0xFF;
            if (t != tag) {
                throw new IllegalStateException("DER tag mismatch");
            }
        }
    }

    // ==================================================================
    // 主机密钥
    // ==================================================================

    private static String sha256Fingerprint(byte[] keyBlob) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(keyBlob);
        return "SHA256:" + Base64.getEncoder().withoutPadding().encodeToString(digest);
    }

    private void verifyHostKeySignature(byte[] kS, byte[] sigBlob, byte[] exchangeHashH, Logger logger)
            throws IOException {
        String alg;
        byte[] rawSig;
        try {
            PacketReader sr = new PacketReader(sigBlob, 0);
            alg = sr.readString();
            rawSig = sr.readByteString();
        } catch (Exception e) {
            throw new IOException("主机密钥签名字段解析失败: " + e.getMessage());
        }

        PublicKey hostKey;
        Signature verifier;
        byte[] sig = rawSig;
        try {
            hostKey = decodeHostKey(kS);
            switch (alg) {
                case "rsa-sha2-256":
                    verifier = Signature.getInstance("SHA256withRSA");
                    break;
                case "rsa-sha2-512":
                    verifier = Signature.getInstance("SHA512withRSA");
                    break;
                case "ssh-rsa":
                    verifier = Signature.getInstance("SHA1withRSA");
                    break;
                case "ecdsa-sha2-nistp256":
                    verifier = Signature.getInstance("SHA256withECDSA");
                    sig = ecdsaSshToDer(rawSig);
                    break;
                case "ssh-ed25519":
                    verifier = Signature.getInstance("Ed25519");
                    break;
                default:
                    logger.log("⚠ 未知主机密钥签名算法 " + alg + "，跳过校验（请人工核对指纹）");
                    return;
            }
        } catch (Exception e) {
            logger.log("⚠ 主机密钥校验不可用 (" + alg + "): " + e.getMessage()
                    + "，请人工核对上方指纹");
            return;
        }

        try {
            verifier.initVerify(hostKey);
            verifier.update(exchangeHashH);
            if (!verifier.verify(sig)) {
                throw new IOException("服务端主机密钥签名校验失败（可能存在中间人）");
            }
            logger.log("主机密钥签名校验通过 (" + alg + ")");
        } catch (IOException e) {
            throw e;
        } catch (Exception e) {
            logger.log("⚠ 主机密钥签名校验跳过 (" + alg + "): " + e.getMessage());
        }
    }

    private static byte[] ecdsaSshToDer(byte[] sshSig) throws Exception {
        PacketReader pr = new PacketReader(sshSig, 0);
        BigInteger r = new BigInteger(1, pr.readByteString());
        BigInteger s = new BigInteger(1, pr.readByteString());

        byte[] rb = r.toByteArray();
        byte[] sb = s.toByteArray();
        int total = 2 + rb.length + 2 + sb.length;
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        baos.write(0x30);
        writeDerLen(baos, total);
        baos.write(0x02);
        writeDerLen(baos, rb.length);
        baos.write(rb, 0, rb.length);
        baos.write(0x02);
        writeDerLen(baos, sb.length);
        baos.write(sb, 0, sb.length);
        return baos.toByteArray();
    }

    private static void writeDerLen(ByteArrayOutputStream baos, int len) {
        if (len < 0x80) {
            baos.write(len);
        } else if (len < 0x100) {
            baos.write(0x81);
            baos.write(len);
        } else {
            baos.write(0x82);
            baos.write((len >> 8) & 0xFF);
            baos.write(len & 0xFF);
        }
    }

    private static PublicKey decodeHostKey(byte[] blob) throws Exception {
        PacketReader pr = new PacketReader(blob, 0);
        String type = pr.readString();
        switch (type) {
            case "ssh-rsa":
            case "rsa-sha2-256":
            case "rsa-sha2-512": {
                BigInteger e = new BigInteger(1, pr.readByteString());
                BigInteger n = new BigInteger(1, pr.readByteString());
                return KeyFactory.getInstance("RSA").generatePublic(new RSAPublicKeySpec(n, e));
            }
            case "ecdsa-sha2-nistp256": {
                pr.readString();
                byte[] q = pr.readByteString();
                return decodeUncompressedEcPoint(q, p256Params());
            }
            case "ssh-ed25519": {
                byte[] pub = pr.readByteString();
                byte[] x509 = new byte[12 + pub.length];
                byte[] head = {0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00};
                System.arraycopy(head, 0, x509, 0, 12);
                System.arraycopy(pub, 0, x509, 12, pub.length);
                return KeyFactory.getInstance("Ed25519").generatePublic(new X509EncodedKeySpec(x509));
            }
            default:
                throw new IOException("不支持的主机密钥类型: " + type);
        }
    }

    private static ECParameterSpec cachedP256;

    private static ECParameterSpec p256Params() throws Exception {
        if (cachedP256 == null) {
            KeyPairGenerator kpg = KeyPairGenerator.getInstance("EC");
            kpg.initialize(new ECGenParameterSpec("secp256r1"));
            cachedP256 = ((ECPublicKey) kpg.generateKeyPair().getPublic()).getParams();
        }
        return cachedP256;
    }

    // ==================================================================
    // 算法协商
    // ==================================================================

    private static final class ServerKexInit {
        List<String> kex = new ArrayList<>();
        List<String> hostKeyAlgs = new ArrayList<>();
        List<String> encC2S = new ArrayList<>();
        List<String> encS2C = new ArrayList<>();
        List<String> macC2S = new ArrayList<>();
        List<String> macS2C = new ArrayList<>();
        boolean firstKexPacketFollows;
    }

    private static ServerKexInit parseKexInit(byte[] payload) {
        PacketReader pr = new PacketReader(payload, 1);
        pr.skipBytes(16);
        ServerKexInit s = new ServerKexInit();
        s.kex = pr.readNameList();
        s.hostKeyAlgs = pr.readNameList();
        s.encC2S = pr.readNameList();
        s.encS2C = pr.readNameList();
        s.macC2S = pr.readNameList();
        s.macS2C = pr.readNameList();
        pr.readNameList();
        pr.readNameList();
        pr.readNameList();
        pr.readNameList();
        s.firstKexPacketFollows = pr.readBoolean();
        return s;
    }

    private void negotiate(ServerKexInit server, Logger logger) throws IOException {
        List<String> missing = new ArrayList<>();
        if (!server.kex.contains(KEX_ALG)) missing.add("kex=" + KEX_ALG);
        if (!server.encC2S.contains(CIPHER_ALG)) missing.add("enc(c2s)=" + CIPHER_ALG);
        if (!server.encS2C.contains(CIPHER_ALG)) missing.add("enc(s2c)=" + CIPHER_ALG);
        if (!server.macC2S.contains(MAC_ALG)) missing.add("mac(c2s)=" + MAC_ALG);
        if (!server.macS2C.contains(MAC_ALG)) missing.add("mac(s2c)=" + MAC_ALG);

        if (!missing.isEmpty()) {
            throw new IOException("服务端不支持所需算法: " + String.join(", ", missing)
                    + " | 服务端 kex=" + server.kex
                    + " enc=" + server.encC2S
                    + " mac=" + server.macC2S);
        }
    }

    // ==================================================================
    // 传输层
    // ==================================================================

    private byte[] buildKexInitPayload() {
        PacketWriter pw = new PacketWriter();
        pw.writeByte(SSH_MSG_KEXINIT);
        byte[] cookie = new byte[16];
        random.nextBytes(cookie);
        pw.writeRawBytes(cookie);
        pw.writeString(KEX_ALG);
        pw.writeString("rsa-sha2-256,rsa-sha2-512,ecdsa-sha2-nistp256,ssh-ed25519,ssh-rsa");
        pw.writeString(CIPHER_ALG);
        pw.writeString(CIPHER_ALG);
        pw.writeString(MAC_ALG);
        pw.writeString(MAC_ALG);
        pw.writeString("none");
        pw.writeString("none");
        pw.writeString("");
        pw.writeString("");
        pw.writeBoolean(false);
        pw.writeUint32(0);
        return pw.toByteArray();
    }

    private byte[] deriveKey(byte[] kMpint, byte[] h, byte letter, byte[] sessionId, int neededLen)
            throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        PacketWriter pw = new PacketWriter();
        pw.writeByteString(kMpint);
        pw.writeRawBytes(h);
        pw.writeByte(letter);
        pw.writeRawBytes(sessionId);
        byte[] digest = md.digest(pw.toByteArray());
        return Arrays.copyOf(digest, neededLen);
    }

    private static byte[] encodeUncompressedEcPoint(ECPoint point) {
        byte[] out = new byte[65];
        out[0] = 0x04;
        byte[] x = toFixed32(point.getAffineX());
        byte[] y = toFixed32(point.getAffineY());
        System.arraycopy(x, 0, out, 1, 32);
        System.arraycopy(y, 0, out, 33, 32);
        return out;
    }

    private static PublicKey decodeUncompressedEcPoint(byte[] encoded, ECParameterSpec params)
            throws Exception {
        if (encoded.length != 65 || encoded[0] != 0x04) {
            throw new IOException("无效的 NIST P-256 未压缩点");
        }
        BigInteger x = new BigInteger(1, Arrays.copyOfRange(encoded, 1, 33));
        BigInteger y = new BigInteger(1, Arrays.copyOfRange(encoded, 33, 65));
        ECPoint w = new ECPoint(x, y);
        KeyFactory kf = KeyFactory.getInstance("EC");
        return kf.generatePublic(new ECPublicKeySpec(w, params));
    }

    private static byte[] toFixed32(BigInteger bi) {
        byte[] b = bi.toByteArray();
        if (b.length == 32) return b;
        if (b.length > 32) return Arrays.copyOfRange(b, b.length - 32, b.length);
        byte[] res = new byte[32];
        System.arraycopy(b, 0, res, 32 - b.length, b.length);
        return res;
    }

    private String readServerVersionLine() throws IOException {
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        int total = 0;
        while (true) {
            int b = in.read();
            if (b == -1) throw new EOFException("SSH 服务端在版本交换前关闭了连接");
            if (b == '\n') {
                String line = new String(baos.toByteArray(), StandardCharsets.US_ASCII).replace("\r", "");
                if (line.startsWith("SSH-2.0-")) {
                    return line;
                }
                if (line.startsWith("SSH-1.")) {
                    throw new IOException("服务端仅支持 SSH-1，不受支持: " + line);
                }
                baos.reset();
                total = 0;
            } else {
                baos.write(b);
                if (++total > 8192) {
                    throw new IOException("服务端版本行过长，可能不是 SSH 服务");
                }
            }
        }
    }

    private byte[] readNextNonIgnorePacket() throws Exception {
        while (true) {
            byte[] pkt = readPacket();
            if (pkt.length == 0) continue;
            byte t = pkt[0];
            if (t == SSH_MSG_IGNORE || t == SSH_MSG_DEBUG
                    || t == SSH_MSG_UNIMPLEMENTED || t == SSH_MSG_EXT_INFO) {
                continue;
            }
            return pkt;
        }
    }

    private void sendPacket(byte[] payload) throws Exception {
        synchronized (writeLock) {
            int blockSize = outboundEncrypted ? 16 : 8;
            int minTotal = 4 + 1 + payload.length;
            int padLen = blockSize - (minTotal % blockSize);
            if (padLen < 4) padLen += blockSize;

            int packetLength = 1 + payload.length + padLen;
            byte[] padding = new byte[padLen];
            random.nextBytes(padding);

            ByteBuffer plain = ByteBuffer.allocate(4 + packetLength);
            plain.putInt(packetLength);
            plain.put((byte) padLen);
            plain.put(payload);
            plain.put(padding);
            byte[] plainBytes = plain.array();

            if (!outboundEncrypted) {
                out.write(plainBytes);
                out.flush();
            } else {
                ByteBuffer seqBuf = ByteBuffer.allocate(4);
                seqBuf.putInt(sendSeq);
                encMac.update(seqBuf.array());
                encMac.update(plainBytes);
                byte[] macBytes = encMac.doFinal();

                byte[] cipherBytes = encCipher.update(plainBytes);
                out.write(cipherBytes);
                out.write(macBytes);
                out.flush();
            }
            sendSeq++;
            lastSentAt = System.currentTimeMillis();
        }
    }

    private byte[] readPacket() throws Exception {
        byte[] result;
        if (!inboundEncrypted) {
            int packetLength = in.readInt();
            if (packetLength < 12 || packetLength > 262144) {
                throw new IOException("无效的 SSH 包长度: " + packetLength);
            }
            int padLen = in.readUnsignedByte();
            if (padLen < 4 || padLen > 255 || padLen > packetLength - 1) {
                throw new IOException("无效的 SSH 填充长度: " + padLen);
            }
            byte[] payload = new byte[packetLength - padLen - 1];
            in.readFully(payload);
            byte[] pad = new byte[padLen];
            in.readFully(pad);
            recvSeq++;
            result = payload;
        } else {
            Cipher dc = decCipher;
            Mac dm = decMac;

            byte[] firstBlockEnc = new byte[16];
            in.readFully(firstBlockEnc);
            byte[] firstBlockDec = dc.update(firstBlockEnc);

            int packetLength = ByteBuffer.wrap(firstBlockDec, 0, 4).getInt();
            if (packetLength < 12 || packetLength > 262144) {
                throw new IOException("无效的 SSH 加密包长度: " + packetLength);
            }
            int totalPlainLen = 4 + packetLength;
            int remainingEncLen = totalPlainLen - 16;

            byte[] plainAll = new byte[totalPlainLen];
            System.arraycopy(firstBlockDec, 0, plainAll, 0, 16);

            if (remainingEncLen > 0) {
                byte[] remEnc = new byte[remainingEncLen];
                in.readFully(remEnc);
                byte[] remDec = dc.update(remEnc);
                if (remDec.length != remainingEncLen) {
                    throw new IOException("SSH 解密块长度异常");
                }
                System.arraycopy(remDec, 0, plainAll, 16, remainingEncLen);
            }

            byte[] receivedMac = new byte[32];
            in.readFully(receivedMac);

            ByteBuffer seqBuf = ByteBuffer.allocate(4);
            seqBuf.putInt(recvSeq);
            dm.update(seqBuf.array());
            dm.update(plainAll);
            byte[] computedMac = dm.doFinal();

            if (!MessageDigest.isEqual(receivedMac, computedMac)) {
                throw new IOException("SSH HMAC-SHA256 校验失败");
            }

            recvSeq++;
            int padLen = plainAll[4] & 0xFF;
            int payloadLen = packetLength - padLen - 1;
            if (payloadLen < 0) {
                throw new IOException("SSH 包负载长度非法");
            }
            result = Arrays.copyOfRange(plainAll, 5, 5 + payloadLen);
        }
        lastReceivedAt = System.currentTimeMillis();
        return result;
    }

    private static class ForwardedChannel {
        final int localChannelId;
        final int serverChannelId;
        final Socket adbSocket;
        final InputStream adbIn;
        final OutputStream adbOut;
        final Object windowLock = new Object();
        long remoteWindow = 0;
        int remoteMaxPacket = 32768;
        int consumedSinceAdjust = 0;
        volatile boolean closeSent = false;
        volatile boolean closed = false;

        ForwardedChannel(int localChannelId, int serverChannelId, Socket adbSocket) throws IOException {
            this.localChannelId = localChannelId;
            this.serverChannelId = serverChannelId;
            this.adbSocket = adbSocket;
            this.adbIn = adbSocket.getInputStream();
            this.adbOut = adbSocket.getOutputStream();
        }
    }

    private static class PacketWriter {
        private final ByteArrayOutputStream baos = new ByteArrayOutputStream();

        void writeByte(byte b) {
            baos.write(b);
        }

        void writeBoolean(boolean v) {
            baos.write(v ? 1 : 0);
        }

        void writeUint32(int v) {
            baos.write((v >>> 24) & 0xFF);
            baos.write((v >>> 16) & 0xFF);
            baos.write((v >>> 8) & 0xFF);
            baos.write(v & 0xFF);
        }

        void writeByteString(byte[] data) {
            writeUint32(data.length);
            baos.write(data, 0, data.length);
        }

        void writeString(String s) {
            writeByteString(s.getBytes(StandardCharsets.UTF_8));
        }

        void writeRawBytes(byte[] raw) {
            baos.write(raw, 0, raw.length);
        }

        byte[] toByteArray() {
            return baos.toByteArray();
        }
    }

    private static class PacketReader {
        private final byte[] buf;
        private int pos;

        PacketReader(byte[] buf, int startPos) {
            this.buf = buf;
            this.pos = startPos;
        }

        void skipBytes(int n) {
            pos += n;
        }

        boolean readBoolean() {
            return buf[pos++] != 0;
        }

        int readUint32() {
            int v = ((buf[pos] & 0xFF) << 24)
                    | ((buf[pos + 1] & 0xFF) << 16)
                    | ((buf[pos + 2] & 0xFF) << 8)
                    | (buf[pos + 3] & 0xFF);
            pos += 4;
            return v;
        }

        byte[] readByteString() {
            int len = readUint32();
            if (len < 0 || pos + len > buf.length) {
                throw new IndexOutOfBoundsException("SSH 字段长度越界: " + len);
            }
            byte[] out = Arrays.copyOfRange(buf, pos, pos + len);
            pos += len;
            return out;
        }

        String readString() {
            return new String(readByteString(), StandardCharsets.UTF_8);
        }

        List<String> readNameList() {
            String raw = readString();
            List<String> list = new ArrayList<>();
            if (raw.isEmpty()) return list;
            for (String s : raw.split(",")) {
                if (!s.isEmpty()) list.add(s);
            }
            return list;
        }
    }
}
