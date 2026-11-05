import com.example.sshadbtunnel.PureJavaSshReverseTunnel;
import com.example.sshadbtunnel.TunnelConfig;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.ArrayList;
import java.util.List;

/**
 * 上行吞吐压测：模拟手机 adbd 持续吐视频流，经隧道加密上行到服务端。
 * 测量的是本实现最关键的性能路径（手机端 AES-CTR + HMAC-SHA256 加密）。
 */
public class SshThroughputTest {

    private static final int TOTAL_MB = 64;

    public static void main(String[] args) throws Exception {
        int sshPort = 2322;
        int adbPort = 15655;
        int remoteP = 5655;
        long totalBytes = TOTAL_MB * 1024L * 1024L;

        // 假 adbd：连上来就狂发数据
        ServerSocket adbServer = new ServerSocket();
        adbServer.setReuseAddress(true);
        adbServer.bind(new InetSocketAddress("127.0.0.1", adbPort));
        Thread adbThread = new Thread(() -> {
            try (Socket s = adbServer.accept()) {
                OutputStream os = s.getOutputStream();
                byte[] chunk = new byte[32 * 1024];
                long sent = 0;
                while (sent < totalBytes) {
                    os.write(chunk, 0, chunk.length);
                    sent += chunk.length;
                }
                os.flush();
            } catch (Exception ignored) {
            }
        }, "fake-adbd-source");
        adbThread.setDaemon(true);
        adbThread.start();

        SshTestServer server = new SshTestServer(sshPort, "testpass", remoteP, adbPort,
                false, totalBytes);
        Thread serverThread = new Thread(server, "ssh-throughput-server");
        serverThread.setDaemon(true);
        serverThread.start();

        Thread.sleep(300);

        TunnelConfig cfg = new TunnelConfig("127.0.0.1", sshPort, "tester",
                TunnelConfig.AuthType.PASSWORD, "testpass", "", "",
                TunnelConfig.ListenMode.CONNECT, remoteP);

        List<String> logs = new ArrayList<>();
        PureJavaSshReverseTunnel tunnel = new PureJavaSshReverseTunnel();
        try {
            tunnel.connectAndForwardR(cfg, adbPort, m -> {
                synchronized (logs) { logs.add(m); }
            });
            tunnel.runForwardingLoop(adbPort, m -> {
                synchronized (logs) { logs.add(m); }
            });
        } catch (Exception e) {
            System.out.println("客户端异常: " + e);
        } finally {
            tunnel.disconnect();
            adbServer.close();
        }

        serverThread.join(10000);
        System.out.println("服务端结果: " + server.result);
        System.out.println("上行吞吐: " + server.negotiated);
        System.out.println("理论换算: " + String.format("%.0f", server.throughputMbps / 8) + " MB/s");
        System.exit("PASS".equals(server.result) ? 0 : 1);
    }
}
