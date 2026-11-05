import com.example.sshadbtunnel.PureJavaSshReverseTunnel;
import com.example.sshadbtunnel.TunnelConfig;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

/**
 * PureJavaSshReverseTunnel 回环测试：
 * 本地假 adb 回声服务 <- 客户端隧道 <- 测试 SSH 服务端 <- forwarded-tcpip 通道
 */
public class SshClientTest {

    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        String keyRsa = args.length > 0 ? args[0] : null;
        String keyEd = args.length > 1 ? args[1] : null;

        runScenario("password-auth", 2222, 15555, 5555, null, "testpass", false, null, null);
        if (keyRsa != null) {
            runScenario("openssh-rsa-key", 2223, 15556, 5556, keyRsa, null, false, null, null);
        }
        if (keyEd != null) {
            runScenario("openssh-ed25519-key", 2224, 15557, 5557, keyEd, null, false, null, null);
            runScenario("server-forced-rekey", 2225, 15558, 5558, keyEd, null, true, null, null);
        }

        // 回归测试：模拟 GatewayPorts 白名单只接受 "127.0.0.1"。
        // 客户端默认先发 "localhost"，必须能识别 REQUEST_FAILURE 并自动改用 "127.0.0.1"。
        runScenario("gateway-ports-bind-fallback", 2227, 15560, 5560,
                null, "testpass", false, null, "127.0.0.1");

        // 回归测试：adbd 只绑在非回环地址上（模拟 mDNS 解析出 Wi-Fi 地址的情况）。
        // 若客户端把目标写死成 127.0.0.1，桥接必然失败。
        String lanIp = findLanIpv4();
        if (lanIp != null) {
            runScenario("non-loopback-endpoint", 2226, 15559, 5559, null, "testpass", false, lanIp, null);
        } else {
            System.out.println();
            System.out.println("跳过 non-loopback-endpoint：本机没有可用的非回环 IPv4 地址");
        }

        System.out.println();
        System.out.println("================ 测试汇总 ================");
        if (failures.isEmpty()) {
            System.out.println("全部通过 ✅");
            System.exit(0);
        } else {
            for (String f : failures) System.out.println("失败: " + f);
            System.exit(1);
        }
    }

    private static void runScenario(String name, int sshPort, int adbPort, int remoteP,
                                    String keyPath, String password, boolean rekey,
                                    String adbBindHost, String onlyAcceptBindAddress)
            throws Exception {
        System.out.println();
        System.out.println("---------------- 场景: " + name + " ----------------");

        String bindHost = adbBindHost == null ? "127.0.0.1" : adbBindHost;
        ServerSocket adbServer = new ServerSocket();
        adbServer.setReuseAddress(true);
        adbServer.bind(new InetSocketAddress(bindHost, adbPort));
        Thread adbThread = new Thread(() -> {
            try (Socket s = adbServer.accept()) {
                InputStream is = s.getInputStream();
                OutputStream os = s.getOutputStream();
                byte[] buf = new byte[4096];
                int n;
                while ((n = is.read(buf)) != -1) {
                    os.write(buf, 0, n);
                    os.flush();
                }
            } catch (Exception ignored) {
            }
        }, "fake-adb-" + adbPort);
        adbThread.setDaemon(true);
        adbThread.start();

        SshTestServer server = new SshTestServer(sshPort, "testpass", remoteP, adbPort,
                rekey, 0, onlyAcceptBindAddress);
        Thread serverThread = new Thread(server, "ssh-test-server-" + sshPort);
        serverThread.setDaemon(true);
        serverThread.start();

        Thread.sleep(300);

        TunnelConfig.AuthType authType = keyPath == null
                ? TunnelConfig.AuthType.PASSWORD : TunnelConfig.AuthType.PRIVATE_KEY;
        String keyText = keyPath == null ? "" : new String(Files.readAllBytes(Paths.get(keyPath)),
                StandardCharsets.UTF_8);

        TunnelConfig cfg = new TunnelConfig(
                "127.0.0.1", sshPort, "tester", authType,
                password == null ? "" : password, keyText, "",
                TunnelConfig.ListenMode.CONNECT, remoteP);

        List<String> logs = new ArrayList<>();
        PureJavaSshReverseTunnel.Logger logger = m -> {
            synchronized (logs) { logs.add(m); }
            System.out.println("  [client] " + m);
        };

        PureJavaSshReverseTunnel tunnel = new PureJavaSshReverseTunnel();
        String clientError = null;
        try {
            tunnel.connectAndForwardR(cfg, bindHost, adbPort, logger);
            tunnel.runForwardingLoop(logger);
        } catch (Exception e) {
            clientError = e.getClass().getSimpleName() + ": " + e.getMessage();
            System.out.println("  [client] 异常: " + clientError);
        } finally {
            tunnel.disconnect();
            adbServer.close();
        }

        serverThread.join(5000);

        System.out.println("  服务端结果: " + server.result);
        System.out.println("  协商/通道: " + server.negotiated);
        System.out.println("  adb 绑定地址: " + bindHost + ":" + adbPort);
        System.out.println("  tcpip-forward 绑定地址: " + server.bindAddressSeen
                + "（共尝试 " + server.bindAttempts + " 次）");
        if (rekey) {
            System.out.println("  rekey 次数: " + server.rekeyCount);
            System.out.println("  rekey 后回环: " + server.rekeyEcho);
            System.out.println("  客户端 rekey 计数: " + tunnel.getRekeyCount());
        }
        System.out.println("  认证方法: " + server.authMethod);
        System.out.println("  客户端异常: " + (clientError == null ? "无" : clientError));

        boolean rekeyOk = !rekey
                || (server.rekeyCount >= 1
                    && "AFTER-REKEY-STILL-ALIVE".equals(server.rekeyEcho)
                    && tunnel.getRekeyCount() >= 1);

        boolean ok = "PASS".equals(server.result)
                && clientError == null
                && server.negotiated != null
                && rekeyOk;

        if (!ok) {
            failures.add(name + " -> server=" + server.result + ", client=" + clientError
                    + ", rekey=" + tunnel.getRekeyCount());
            System.out.println("  结果: 失败 ❌");
        } else {
            System.out.println("  结果: 通过 ✅");
        }
    }

    /** 找一个可绑定的非回环 IPv4 地址，用来模拟 mDNS 解析出的 Wi-Fi 地址。 */
    private static String findLanIpv4() {
        try {
            java.util.Enumeration<java.net.NetworkInterface> ifs =
                    java.net.NetworkInterface.getNetworkInterfaces();
            while (ifs.hasMoreElements()) {
                java.net.NetworkInterface ni = ifs.nextElement();
                if (!ni.isUp() || ni.isLoopback() || ni.isVirtual()) continue;
                java.util.Enumeration<java.net.InetAddress> addrs = ni.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    java.net.InetAddress a = addrs.nextElement();
                    if (a instanceof java.net.Inet4Address && !a.isLoopbackAddress()
                            && !a.isLinkLocalAddress()) {
                        return a.getHostAddress();
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return null;
    }
}
