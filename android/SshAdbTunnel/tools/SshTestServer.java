import javax.crypto.Cipher;
import javax.crypto.KeyAgreement;
import javax.crypto.Mac;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.OutputStream;
import java.math.BigInteger;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.PublicKey;
import java.security.SecureRandom;
import java.security.Signature;
import java.security.interfaces.ECPublicKey;
import java.security.interfaces.RSAPublicKey;
import java.security.spec.ECGenParameterSpec;
import java.security.spec.ECParameterSpec;
import java.security.spec.ECPoint;
import java.security.spec.ECPublicKeySpec;
import java.security.spec.RSAPublicKeySpec;
import java.util.Arrays;
import java.util.Locale;

/**
 * 最小 SSH-2.0 服务端桩，用于回环验证 PureJavaSshReverseTunnel。
 * 覆盖：版本交换、KEXINIT 协商、ECDH-P256、NEWKEYS、AES128-CTR + HMAC-SHA256、
 *       认证、tcpip-forward、forwarded-tcpip 双向数据，以及**服务端强制 rekey**。
 */
public class SshTestServer implements Runnable {

    private static final byte MSG_KEXINIT = 20;
    private static final byte MSG_NEWKEYS = 21;
    private static final byte MSG_KEX_ECDH_INIT = 30;
    private static final byte MSG_KEX_ECDH_REPLY = 31;
    private static final byte MSG_SERVICE_REQUEST = 5;
    private static final byte MSG_SERVICE_ACCEPT = 6;
    private static final byte MSG_USERAUTH_REQUEST = 50;
    private static final byte MSG_USERAUTH_SUCCESS = 52;
    private static final byte MSG_GLOBAL_REQUEST = 80;
    private static final byte MSG_REQUEST_SUCCESS = 81;
    private static final byte MSG_REQUEST_FAILURE = 82;
    private static final byte MSG_CHANNEL_OPEN = 90;
    private static final byte MSG_CHANNEL_OPEN_CONFIRMATION = 91;
    private static final byte MSG_CHANNEL_DATA = 94;
    private static final byte MSG_CHANNEL_WINDOW_ADJUST = 93;
    private static final byte MSG_CHANNEL_CLOSE = 97;

    private final int listenPort;
    private final String expectedPassword;
    private final int remotePortP;
    private final int localAdbPort;
    private final boolean doRekey;
    private final long sinkBytes;
    /** 非 null 时模拟 GatewayPorts 白名单：只接受这个绑定地址，其余回 REQUEST_FAILURE。 */
    private final String onlyAcceptBindAddress;

    private final SecureRandom random = new SecureRandom();

    private Socket sock;
    private DataInputStream in;
    private OutputStream out;
    private Cipher encCipher;
    private Cipher decCipher;
    private Mac encMac;
    private Mac decMac;
    private int sendSeq = 0;
    private int recvSeq = 0;
    private boolean encrypted = false;

    private KeyPair hostKp;
    private byte[] hostKeyBlob;
    private byte[] sessionId;

    public volatile String result = null;
    public volatile String clientVersion = null;
    public volatile String authMethod = null;
    public volatile String negotiated = null;
    public volatile String hostKeyFingerprint = null;
    public volatile int rekeyCount = 0;
    public volatile String rekeyEcho = null;

    public SshTestServer(int listenPort, String expectedPassword, int remotePortP,
                         int localAdbPort, boolean doRekey) {
        this(listenPort, expectedPassword, remotePortP, localAdbPort, doRekey, 0, null);
    }

    public SshTestServer(int listenPort, String expectedPassword, int remotePortP,
                         int localAdbPort, boolean doRekey, long sinkBytes) {
        this(listenPort, expectedPassword, remotePortP, localAdbPort, doRekey, sinkBytes, null);
    }

    public SshTestServer(int listenPort, String expectedPassword, int remotePortP,
                         int localAdbPort, boolean doRekey, long sinkBytes,
                         String onlyAcceptBindAddress) {
        this.listenPort = listenPort;
        this.expectedPassword = expectedPassword;
        this.remotePortP = remotePortP;
        this.localAdbPort = localAdbPort;
        this.doRekey = doRekey;
        this.sinkBytes = sinkBytes;
        this.onlyAcceptBindAddress = onlyAcceptBindAddress;
    }

    public volatile double throughputMbps = 0;
    public volatile String bindAddressSeen = null;
    public volatile int bindAttempts = 0;

    @Override
    public void run() {
        try (ServerSocket ss = new ServerSocket()) {
            ss.setReuseAddress(true);
            ss.bind(new InetSocketAddress("127.0.0.1", listenPort));
            sock = ss.accept();
            sock.setTcpNoDelay(true);
            sock.setSoTimeout(30000);
            in = new DataInputStream(sock.getInputStream());
            out = sock.getOutputStream();
            serve();
            result = "PASS";
        } catch (Throwable t) {
            result = "FAIL: " + t.getClass().getSimpleName() + ": " + t.getMessage();
            t.printStackTrace();
        } finally {
            try { if (sock != null) sock.close(); } catch (Exception ignored) { }
        }
    }

    private void serve() throws Exception {
        String serverVersion = "SSH-2.0-TestSSHd_1.0";
        out.write((serverVersion + "\r\n").getBytes(StandardCharsets.US_ASCII));
        out.flush();
        clientVersion = readLine();

        KeyPairGenerator rsaGen = KeyPairGenerator.getInstance("RSA");
        rsaGen.initialize(2048, random);
        hostKp = rsaGen.generateKeyPair();
        hostKeyBlob = buildRsaPublicBlob((RSAPublicKey) hostKp.getPublic());
        hostKeyFingerprint = "SHA256:" + java.util.Base64.getEncoder().withoutPadding()
                .encodeToString(MessageDigest.getInstance("SHA-256").digest(hostKeyBlob));

        // ---- 首次密钥交换：客户端先发 KEXINIT ----
        byte[] clientKexInit = readPacket();
        require(clientKexInit[0] == MSG_KEXINIT, "期望客户端 KEXINIT");
        doKex(clientKexInit, true);

        // ---- SERVICE / AUTH ----
        byte[] svc = readPacket();
        require(svc[0] == MSG_SERVICE_REQUEST, "期望 SERVICE_REQUEST");
        PacketWriter accept = new PacketWriter();
        accept.writeByte(MSG_SERVICE_ACCEPT);
        accept.writeString(new PacketReader(svc, 1).readString());
        sendPacket(accept.toByteArray());

        byte[] auth = readPacket();
        require(auth[0] == MSG_USERAUTH_REQUEST, "期望 USERAUTH_REQUEST");
        PacketReader ar = new PacketReader(auth, 1);
        ar.readString();
        ar.readString();
        String method = ar.readString();
        authMethod = method;
        if ("password".equals(method)) {
            ar.readBoolean();
            String pwd = ar.readString();
            require(pwd.equals(expectedPassword), "密码不匹配，收到 [" + pwd + "]");
        } else if ("publickey".equals(method)) {
            require(ar.readBoolean(), "publickey 请求未携带签名");
            ar.readString();
            ar.readByteString();
            ar.readByteString();
        } else {
            throw new IOException("未知认证方法: " + method);
        }
        sendPacket(new byte[]{MSG_USERAUTH_SUCCESS});

        // ---- tcpip-forward ----
        String bindAddr;
        int bindPort = remotePortP;
        while (true) {
            byte[] fwd = readPacket();
            require(fwd[0] == MSG_GLOBAL_REQUEST, "期望 GLOBAL_REQUEST，收到 " + fwd[0]);
            PacketReader fr = new PacketReader(fwd, 1);
            require("tcpip-forward".equals(fr.readString()), "期望 tcpip-forward");
            fr.readBoolean();
            bindAddr = fr.readString();
            bindPort = fr.readUint32();
            require(bindPort == remotePortP, "反向端口不匹配: " + bindPort + " != " + remotePortP);

            bindAttempts++;
            if (onlyAcceptBindAddress != null && !onlyAcceptBindAddress.equals(bindAddr)) {
                System.out.println("  [server] 拒绝绑定地址 \"" + bindAddr
                        + "\"（模拟 GatewayPorts 白名单只接受 \"" + onlyAcceptBindAddress + "\"）");
                sendPacket(new byte[]{MSG_REQUEST_FAILURE});
                if (bindAttempts > 5) {
                    throw new IOException("客户端始终没有改用 " + onlyAcceptBindAddress);
                }
                continue;
            }
            sendPacket(new byte[]{MSG_REQUEST_SUCCESS});
            break;
        }
        bindAddressSeen = bindAddr;
        System.out.println("  [server] 接受绑定地址 \"" + bindAddr + "\"（第 " + bindAttempts + " 次尝试）");

        // ---- 打开 forwarded-tcpip 通道 ----
        int clientChannelId = openChannel(bindAddr, bindPort);

        // ---- 吞吐模式：只收不发，统计客户端加密上行速率 ----
        if (sinkBytes > 0) {
            long received = 0;
            long start = System.nanoTime();
            long nextReport = 8L * 1024 * 1024;
            while (received < sinkBytes) {
                byte[] pkt = readPacket();
                if (pkt[0] == MSG_CHANNEL_DATA) {
                    PacketReader dr = new PacketReader(pkt, 1);
                    dr.readUint32(); // 收到的 recipient 是服务端自己的 channel id，不能拿来回发
                    byte[] payload = dr.readByteString();
                    received += payload.length;
                    if (received >= nextReport) {
                        System.out.println("  [server] 已接收 " + (received / 1048576) + " MB");
                        nextReport += 8L * 1024 * 1024;
                    }
                    // 归还窗口：必须用客户端的 channel id
                    PacketWriter win = new PacketWriter();
                    win.writeByte(MSG_CHANNEL_WINDOW_ADJUST);
                    win.writeUint32(clientChannelId);
                    win.writeUint32(payload.length);
                    sendPacket(win.toByteArray());
                } else {
                    System.out.println("  [server] 收到非数据包 type=" + pkt[0] + " received=" + received);
                }
            }
            double seconds = (System.nanoTime() - start) / 1e9;
            throughputMbps = (sinkBytes * 8.0 / 1e6) / seconds;
            negotiated = String.format(Locale.ROOT,
                    "上行 %.1f MB / %.2f s = %.1f Mbps", sinkBytes / 1048576.0, seconds, throughputMbps);
            try { Thread.sleep(200); } catch (InterruptedException ignored) { }
            return;
        }

        // ---- 第一轮数据 ----
        String p1 = "ADB-HANDSHAKE-CNXN-OK";
        sendChannelData(clientChannelId, p1);
        String e1 = readEcho();
        require(p1.equals(e1), "回环数据不一致: 发出 [" + p1 + "] 收到 [" + e1 + "]");
        negotiated = "forwarded-tcpip OK, echo=" + e1;

        // ---- 服务端强制 rekey（模拟 OpenSSH 1 小时/1GB 行为）----
        if (doRekey) {
            serverInitiatedRekey();
            rekeyCount++;

            // rekey 后通道必须仍然可用
            String p2 = "AFTER-REKEY-STILL-ALIVE";
            sendChannelData(clientChannelId, p2);
            String e2 = readEcho();
            rekeyEcho = e2;
            require(p2.equals(e2), "rekey 后数据不一致: 发出 [" + p2 + "] 收到 [" + e2 + "]");
        }

        PacketWriter close = new PacketWriter();
        close.writeByte(MSG_CHANNEL_CLOSE);
        close.writeUint32(clientChannelId);
        sendPacket(close.toByteArray());

        try { Thread.sleep(300); } catch (InterruptedException ignored) { }
    }

    private int openChannel(String bindAddr, int bindPort) throws Exception {
        PacketWriter open = new PacketWriter();
        open.writeByte(MSG_CHANNEL_OPEN);
        open.writeString("forwarded-tcpip");
        open.writeUint32(0);
        open.writeUint32(8 * 1024 * 1024);
        open.writeUint32(32768);
        open.writeString(bindAddr);
        open.writeUint32(bindPort);
        open.writeString("127.0.0.1");
        open.writeUint32(44444);
        sendPacket(open.toByteArray());

        byte[] confirm = readPacket();
        require(confirm[0] == MSG_CHANNEL_OPEN_CONFIRMATION, "期望 CHANNEL_OPEN_CONFIRMATION");
        PacketReader cr = new PacketReader(confirm, 1);
        cr.readUint32();
        int clientChannelId = cr.readUint32();
        cr.readUint32();
        cr.readUint32();
        return clientChannelId;
    }

    private void sendChannelData(int clientChannelId, String payload) throws Exception {
        PacketWriter data = new PacketWriter();
        data.writeByte(MSG_CHANNEL_DATA);
        data.writeUint32(clientChannelId);
        data.writeByteString(payload.getBytes(StandardCharsets.US_ASCII));
        sendPacket(data.toByteArray());
    }

    private String readEcho() throws Exception {
        for (int i = 0; i < 40; i++) {
            byte[] pkt = readPacket();
            if (pkt[0] == MSG_CHANNEL_DATA) {
                PacketReader dr = new PacketReader(pkt, 1);
                dr.readUint32();
                return new String(dr.readByteString(), StandardCharsets.US_ASCII);
            }
            if (pkt[0] == MSG_CHANNEL_WINDOW_ADJUST || pkt[0] == MSG_GLOBAL_REQUEST
                    || pkt[0] == MSG_REQUEST_FAILURE) {
                continue;
            }
        }
        return null;
    }

    /** 服务端主动发起密钥重协商。 */
    private void serverInitiatedRekey() throws Exception {
        byte[] serverKexInit = buildKexInit();
        sendPacket(serverKexInit);

        byte[] clientKexInit = readPacket();
        require(clientKexInit[0] == MSG_KEXINIT, "rekey: 期望客户端 KEXINIT，收到 " + clientKexInit[0]);

        doKexWithPayloads(clientKexInit, serverKexInit, false);
    }

    private void doKex(byte[] clientKexInit, boolean initial) throws Exception {
        byte[] serverKexInit = buildKexInit();
        sendPacket(serverKexInit);
        doKexWithPayloads(clientKexInit, serverKexInit, initial);
    }

    private void doKexWithPayloads(byte[] clientKexInit, byte[] serverKexInit, boolean initial)
            throws Exception {
        byte[] ecdhInit = readPacket();
        require(ecdhInit[0] == MSG_KEX_ECDH_INIT, "期望 KEX_ECDH_INIT");
        byte[] qC = new PacketReader(ecdhInit, 1).readByteString();

        KeyPairGenerator kpg = KeyPairGenerator.getInstance("EC");
        kpg.initialize(new ECGenParameterSpec("secp256r1"), random);
        KeyPair serverKp = kpg.generateKeyPair();
        ECPublicKey serverEcPub = (ECPublicKey) serverKp.getPublic();
        ECParameterSpec params = serverEcPub.getParams();
        byte[] qS = encodePoint(serverEcPub.getW());

        KeyAgreement ka = KeyAgreement.getInstance("ECDH");
        ka.init(serverKp.getPrivate());
        ka.doPhase(decodePoint(qC, params), true);
        byte[] kMpint = new BigInteger(1, ka.generateSecret()).toByteArray();

        PacketWriter hi = new PacketWriter();
        hi.writeString(clientVersion);
        hi.writeString("SSH-2.0-TestSSHd_1.0");
        hi.writeByteString(clientKexInit);
        hi.writeByteString(serverKexInit);
        hi.writeByteString(hostKeyBlob);
        hi.writeByteString(qC);
        hi.writeByteString(qS);
        hi.writeByteString(kMpint);
        byte[] h = MessageDigest.getInstance("SHA-256").digest(hi.toByteArray());

        if (initial) {
            sessionId = h;
        }

        Signature sig = Signature.getInstance("SHA256withRSA");
        sig.initSign(hostKp.getPrivate());
        sig.update(h);
        byte[] rawSig = sig.sign();

        PacketWriter sigBlob = new PacketWriter();
        sigBlob.writeString("rsa-sha2-256");
        sigBlob.writeByteString(rawSig);

        PacketWriter reply = new PacketWriter();
        reply.writeByte(MSG_KEX_ECDH_REPLY);
        reply.writeByteString(hostKeyBlob);
        reply.writeByteString(qS);
        reply.writeByteString(sigBlob.toByteArray());
        sendPacket(reply.toByteArray());

        // 收客户端 NEWKEYS（旧密钥） -> 切换接收密钥
        byte[] cNewKeys = readPacket();
        require(cNewKeys[0] == MSG_NEWKEYS, "期望客户端 NEWKEYS");

        byte[] ivC2S = derive(kMpint, h, 'A', sessionId, 16);
        byte[] ivS2C = derive(kMpint, h, 'B', sessionId, 16);
        byte[] keyC2S = derive(kMpint, h, 'C', sessionId, 16);
        byte[] keyS2C = derive(kMpint, h, 'D', sessionId, 16);
        byte[] macC2S = derive(kMpint, h, 'E', sessionId, 32);
        byte[] macS2C = derive(kMpint, h, 'F', sessionId, 32);

        decCipher = Cipher.getInstance("AES/CTR/NoPadding");
        decCipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(keyC2S, "AES"), new IvParameterSpec(ivC2S));
        decMac = Mac.getInstance("HmacSHA256");
        decMac.init(new SecretKeySpec(macC2S, "HmacSHA256"));

        // 发服务端 NEWKEYS（旧密钥） -> 切换发送密钥
        sendPacket(new byte[]{MSG_NEWKEYS});

        encCipher = Cipher.getInstance("AES/CTR/NoPadding");
        encCipher.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(keyS2C, "AES"), new IvParameterSpec(ivS2C));
        encMac = Mac.getInstance("HmacSHA256");
        encMac.init(new SecretKeySpec(macS2C, "HmacSHA256"));

        encrypted = true;
    }

    // ---------- helpers ----------

    private void require(boolean cond, String msg) throws IOException {
        if (!cond) throw new IOException(msg);
    }

    private String readLine() throws IOException {
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        while (true) {
            int b = in.read();
            if (b == -1) throw new EOFException("客户端在版本交换前断开");
            if (b == '\n') return new String(baos.toByteArray(), StandardCharsets.US_ASCII).replace("\r", "");
            baos.write(b);
        }
    }

    private byte[] buildKexInit() {
        PacketWriter pw = new PacketWriter();
        pw.writeByte(MSG_KEXINIT);
        byte[] cookie = new byte[16];
        random.nextBytes(cookie);
        pw.writeRawBytes(cookie);
        pw.writeString("ecdh-sha2-nistp256");
        pw.writeString("rsa-sha2-256,ssh-rsa");
        pw.writeString("aes128-ctr");
        pw.writeString("aes128-ctr");
        pw.writeString("hmac-sha2-256");
        pw.writeString("hmac-sha2-256");
        pw.writeString("none");
        pw.writeString("none");
        pw.writeString("");
        pw.writeString("");
        pw.writeBoolean(false);
        pw.writeUint32(0);
        return pw.toByteArray();
    }

    private byte[] buildRsaPublicBlob(RSAPublicKey pub) {
        PacketWriter pw = new PacketWriter();
        pw.writeString("ssh-rsa");
        pw.writeByteString(pub.getPublicExponent().toByteArray());
        pw.writeByteString(pub.getModulus().toByteArray());
        return pw.toByteArray();
    }

    private byte[] derive(byte[] k, byte[] h, char letter, byte[] sid, int len) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        PacketWriter pw = new PacketWriter();
        pw.writeByteString(k);
        pw.writeRawBytes(h);
        pw.writeByte((byte) letter);
        pw.writeRawBytes(sid);
        return Arrays.copyOf(md.digest(pw.toByteArray()), len);
    }

    private static byte[] encodePoint(ECPoint p) {
        byte[] o = new byte[65];
        o[0] = 0x04;
        System.arraycopy(fixed32(p.getAffineX()), 0, o, 1, 32);
        System.arraycopy(fixed32(p.getAffineY()), 0, o, 33, 32);
        return o;
    }

    private static PublicKey decodePoint(byte[] enc, ECParameterSpec params) throws Exception {
        BigInteger x = new BigInteger(1, Arrays.copyOfRange(enc, 1, 33));
        BigInteger y = new BigInteger(1, Arrays.copyOfRange(enc, 33, 65));
        return KeyFactory.getInstance("EC")
                .generatePublic(new ECPublicKeySpec(new ECPoint(x, y), params));
    }

    private static byte[] fixed32(BigInteger bi) {
        byte[] b = bi.toByteArray();
        if (b.length == 32) return b;
        if (b.length > 32) return Arrays.copyOfRange(b, b.length - 32, b.length);
        byte[] r = new byte[32];
        System.arraycopy(b, 0, r, 32 - b.length, b.length);
        return r;
    }

    private void sendPacket(byte[] payload) throws Exception {
        int blockSize = encrypted ? 16 : 8;
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

        if (!encrypted) {
            out.write(plainBytes);
        } else {
            ByteBuffer seq = ByteBuffer.allocate(4);
            seq.putInt(sendSeq);
            encMac.update(seq.array());
            encMac.update(plainBytes);
            out.write(encCipher.update(plainBytes));
            out.write(encMac.doFinal());
        }
        out.flush();
        sendSeq++;
    }

    private byte[] readPacket() throws Exception {
        if (!encrypted) {
            int len = in.readInt();
            if (len < 12 || len > 262144) throw new IOException("非法包长 " + len);
            int padLen = in.readUnsignedByte();
            byte[] payload = new byte[len - padLen - 1];
            in.readFully(payload);
            byte[] pad = new byte[padLen];
            in.readFully(pad);
            recvSeq++;
            return payload;
        }
        Cipher dc = decCipher;
        Mac dm = decMac;
        byte[] first = new byte[16];
        in.readFully(first);
        byte[] dec = dc.update(first);
        int len = ByteBuffer.wrap(dec, 0, 4).getInt();
        if (len < 12 || len > 262144) throw new IOException("非法加密包长 " + len);
        int total = 4 + len;
        byte[] plainAll = new byte[total];
        System.arraycopy(dec, 0, plainAll, 0, 16);
        int rest = total - 16;
        if (rest > 0) {
            byte[] r = new byte[rest];
            in.readFully(r);
            byte[] rd = dc.update(r);
            System.arraycopy(rd, 0, plainAll, 16, rest);
        }
        byte[] mac = new byte[32];
        in.readFully(mac);
        ByteBuffer seq = ByteBuffer.allocate(4);
        seq.putInt(recvSeq);
        dm.update(seq.array());
        dm.update(plainAll);
        if (!MessageDigest.isEqual(mac, dm.doFinal())) {
            throw new IOException("服务端 HMAC 校验失败");
        }
        recvSeq++;
        int padLen = plainAll[4] & 0xFF;
        return Arrays.copyOfRange(plainAll, 5, 5 + (len - padLen - 1));
    }

    private static class PacketWriter {
        private final ByteArrayOutputStream baos = new ByteArrayOutputStream();

        void writeByte(byte b) { baos.write(b); }

        void writeBoolean(boolean v) { baos.write(v ? 1 : 0); }

        void writeUint32(int v) {
            baos.write((v >>> 24) & 0xFF);
            baos.write((v >>> 16) & 0xFF);
            baos.write((v >>> 8) & 0xFF);
            baos.write(v & 0xFF);
        }

        void writeByteString(byte[] d) {
            writeUint32(d.length);
            baos.write(d, 0, d.length);
        }

        void writeString(String s) { writeByteString(s.getBytes(StandardCharsets.UTF_8)); }

        void writeRawBytes(byte[] d) { baos.write(d, 0, d.length); }

        byte[] toByteArray() { return baos.toByteArray(); }
    }

    private static class PacketReader {
        private final byte[] buf;
        private int pos;

        PacketReader(byte[] buf, int start) {
            this.buf = buf;
            this.pos = start;
        }

        boolean readBoolean() { return buf[pos++] != 0; }

        int readUint32() {
            int v = ((buf[pos] & 0xFF) << 24) | ((buf[pos + 1] & 0xFF) << 16)
                    | ((buf[pos + 2] & 0xFF) << 8) | (buf[pos + 3] & 0xFF);
            pos += 4;
            return v;
        }

        byte[] readByteString() {
            int len = readUint32();
            byte[] o = Arrays.copyOfRange(buf, pos, pos + len);
            pos += len;
            return o;
        }

        String readString() { return new String(readByteString(), StandardCharsets.UTF_8); }
    }
}
