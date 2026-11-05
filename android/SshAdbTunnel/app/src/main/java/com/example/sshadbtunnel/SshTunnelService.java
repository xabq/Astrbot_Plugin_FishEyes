package com.example.sshadbtunnel;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Binder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 面向长时间挂机的前台服务。
 *
 * 关键设计：
 *  - 前台服务类型使用 specialUse（dataSync 在 Android 15 有累计 6 小时上限，会被系统掐掉）
 *  - PARTIAL_WAKE_LOCK + WIFI HIGH_PERF 锁，防止息屏后 CPU/网卡休眠导致推流卡死
 *  - 断线自动重连 + 指数退避；每次重连都重新发现 adb 端口（无线调试端口会变）
 *  - keepalive@openssh.com 心跳 + 静默检测，及时发现半开连接并重连
 *  - SSH 层支持 rekey，服务端每小时强制重协商时不会断流
 */
public class SshTunnelService extends Service {

    public interface TunnelEventListener {
        void onStateChanged(String statusBadge, String autoPortStatus, boolean isRunning);
        void onLogsUpdated(List<String> logs);
    }

    private static final long KEEPALIVE_INTERVAL_MS = 20_000L;
    private static final long SILENT_TIMEOUT_MS = 75_000L;
    private static final int INITIAL_BACKOFF_MS = 3_000;
    private static final int MAX_BACKOFF_MS = 60_000;

    private final IBinder binder = new LocalBinder();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final List<String> logHistory = new ArrayList<>();
    private TunnelEventListener listener;

    private volatile boolean isRunning = false;
    private final AtomicInteger runGeneration = new AtomicInteger(0);
    private Thread workerThread;
    private Thread keepAliveThread;
    private volatile PureJavaSshReverseTunnel sshTunnel;

    private volatile AdbEndpoint discoveredEndpoint = null;
    private volatile AdbEndpoint lastGoodEndpoint = null;
    private final java.util.concurrent.atomic.AtomicBoolean resolving =
            new java.util.concurrent.atomic.AtomicBoolean(false);
    private NsdManager nsdManager;
    private NsdManager.DiscoveryListener discoveryListener;

    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;

    private String currentStatusBadge = "未启动";
    private String currentAutoPortStatus = "启动后 1s 轮询系统接口自动获取";

    public class LocalBinder extends Binder {
        public SshTunnelService getService() {
            return SshTunnelService.this;
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return binder;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // 配置不落盘，进程被回收后无法自动恢复隧道，因此不使用 START_STICKY
        return START_NOT_STICKY;
    }

    public void setListener(TunnelEventListener l) {
        this.listener = l;
        notifyUiState();
        notifyUiLogs();
    }

    public void clearListener(TunnelEventListener l) {
        if (this.listener == l) {
            this.listener = null;
        }
    }

    public void startTunnel(final TunnelConfig config) {
        stopTunnel();
        final int generation = runGeneration.incrementAndGet();
        isRunning = true;

        acquireLocks();

        try {
            startForegroundInternal("正在通过系统接口每秒轮询获取端口...");
        } catch (Exception e) {
            appendLog("无法启动前台服务: " + e.getMessage());
            isRunning = false;
            releaseLocks();
            updateUiState("启动失败", String.valueOf(e.getMessage()), false);
            return;
        }

        workerThread = new Thread(() -> supervise(config, generation), "SshAdbTunnelWorker");
        workerThread.start();
    }

    /**
     * 监管循环：发现端口 -> 建隧道 -> 转发 -> 断开 -> 退避 -> 重来
     */
    private void supervise(final TunnelConfig config, final int generation) {
        appendLog("启动服务: 模式=" + config.listenMode.label
                + ", 服务器=" + config.serverIp + ":" + config.sshPort
                + ", 服务器端口P=" + config.remotePortP);

        int backoff = INITIAL_BACKOFF_MS;
        boolean firstAttempt = true;

        while (isAlive(generation)) {
            AdbEndpoint endpoint = firstAttempt
                    ? pollLocalPort(config, generation, true)
                    : pollLocalPort(config, generation, false);
            firstAttempt = false;
            if (endpoint == null) break;

            PureJavaSshReverseTunnel tunnel = null;
            try {
                updateUiState("正在连接 SSH...",
                        endpoint + " (已获取 · 轮询已停止)", true);
                appendLog("正在连接 SSH 服务器 " + config.username + "@"
                        + config.serverIp + ":" + config.sshPort + " ...");

                tunnel = new PureJavaSshReverseTunnel();
                sshTunnel = tunnel;
                tunnel.connectAndForwardR(config, endpoint.host, endpoint.port, this::appendLog);

                if (!isAlive(generation)) break;

                backoff = INITIAL_BACKOFF_MS;
                lastGoodEndpoint = endpoint;

                appendLog("SSH 反向隧道建立成功！服务器 127.0.0.1:" + config.remotePortP
                        + " ==> 手机 " + endpoint);
                updateUiState("隧道运行中 (P=" + config.remotePortP + ")",
                        endpoint + " (已映射至服务器:" + config.remotePortP + ")", true);
                updateNotification("隧道运行中 · 服务器端口 P=" + config.remotePortP);

                startKeepAlive(generation, tunnel);
                tunnel.runForwardingLoop(this::appendLog);
                appendLog("转发循环结束（服务端断开或网络中断）。");

            } catch (Exception e) {
                String msg = e.getMessage() != null ? e.getMessage() : e.toString();
                appendLog("SSH 异常: " + msg);
                if (isAlive(generation)) {
                    updateUiState("连接异常 · 准备重连",
                            "端点 " + endpoint + " | " + msg, true);
                }
            } finally {
                stopKeepAlive();
                if (sshTunnel == tunnel) {
                    sshTunnel = null;
                }
                if (tunnel != null) {
                    tunnel.disconnect();
                }
            }

            if (!isAlive(generation)) break;

            appendLog("将在 " + (backoff / 1000) + " 秒后自动重连 ...");
            updateNotification("重连中，等待 " + (backoff / 1000) + " 秒 ...");
            if (!sleepInterruptibly(backoff, generation)) break;
            backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        }

        if (runGeneration.get() == generation && isRunning) {
            isRunning = false;
            stopMdnsDiscovery();
            updateUiState("未启动", "启动后 1s 轮询系统接口自动获取", false);
            stopForegroundCompat();
            releaseLocks();
        }
    }

    /**
     * 轮询手机侧 adb 端点。拿到一个有效端点后立刻停止轮询并返回。
     *
     * 关键点：mDNS 给出的端口就是权威结果，不再拿 127.0.0.1 去做"预连校验"。
     * adbd 的无线调试端口不一定绑在回环地址上，预连失败并不代表端口无效，
     * 之前正是因为这道校验把已经拿到的端口判成无效，才会一直轮询下去。
     *
     * @param verbose 首次启动时逐秒打印日志，重连时只每 10 次打印一次，避免刷屏
     * @return 有效端点；返回 null 表示已被停止
     */
    private AdbEndpoint pollLocalPort(TunnelConfig config, int generation, boolean verbose) {
        // 重连时先复用上次成功的端点，避免 mDNS 重新发现带来的几秒空窗
        AdbEndpoint remembered = lastGoodEndpoint;
        if (remembered != null && remembered.isReachable(600)) {
            appendLog("复用上次成功端点: " + remembered + "，跳过轮询");
            return remembered;
        }
        lastGoodEndpoint = null;

        startMdnsDiscovery(config.listenMode);
        int attempt = 0;
        long startedAt = System.currentTimeMillis();

        while (isAlive(generation)) {
            attempt++;

            // 1) mDNS 结果优先：直接采用，不做回环预检
            AdbEndpoint found = discoveredEndpoint;
            if (found != null) {
                stopMdnsDiscovery();
                discoveredEndpoint = null;
                appendLog("已获取" + modeName(config.listenMode) + "端口: " + found
                        + "，停止轮询并开始建立隧道");
                updateUiState("已获取端口 · 正在建立隧道",
                        found + " (轮询已停止)", true);
                return found;
            }

            // 2) /proc 兜底（Android 10+ 基本读不到，仅作最后手段）
            //    给 mDNS 留出充足时间，避免兜底抢在 mDNS 之前抓到无关端口
            if (attempt >= 10) {
                AdbEndpoint proc = scanProcNetEndpoint();
                if (proc != null) {
                    stopMdnsDiscovery();
                    appendLog("⚠ mDNS 长时间无结果，/proc 兜底扫描到端口 " + proc
                            + "（该结果可能不是无线调试端口，仅供尝试）");
                    updateUiState("已获取端口 · 正在建立隧道",
                            proc + " (轮询已停止)", true);
                    return proc;
                }
            }

            // 3) 每 20 秒重新注册一次发现：
            //    部分机型 discoverServices 注册后会静默失效（尤其首次连接断开后），
            //    表现为「一直轮询但永远收不到广播」，重启 App 才恢复。
            if (attempt > 1 && attempt % 20 == 0) {
                appendLog("重新注册 mDNS 发现（第 " + attempt + " 次轮询）");
                stopMdnsDiscovery();
                if (!sleepInterruptibly(400L, generation)) break;
                startMdnsDiscovery(config.listenMode);
            }

            if (verbose || attempt % 10 == 0) {
                long elapsed = (System.currentTimeMillis() - startedAt) / 1000;
                updateUiState("轮询中 (" + elapsed + "s)",
                        "等待系统广播 " + config.listenMode.nsdServiceType
                                + " · 已查询 " + attempt + " 次", true);
                appendLog("第 " + attempt + " 次查询: 尚未发现"
                        + modeName(config.listenMode) + "端口，1 秒后继续");
                updateNotification("正在等待手机端" + modeName(config.listenMode) + "端口出现...");
            }

            if (!sleepInterruptibly(1000L, generation)) break;
        }
        stopMdnsDiscovery();
        discoveredEndpoint = null;
        return null;
    }

    private static String modeName(TunnelConfig.ListenMode mode) {
        return mode == TunnelConfig.ListenMode.PAIRING ? "配对" : "连接";
    }

    private void startKeepAlive(final int generation, final PureJavaSshReverseTunnel tunnel) {
        stopKeepAlive();
        keepAliveThread = new Thread(() -> {
            while (isAlive(generation) && tunnel.isConnected()) {
                try {
                    Thread.sleep(KEEPALIVE_INTERVAL_MS);
                } catch (InterruptedException e) {
                    return;
                }
                if (!isAlive(generation) || !tunnel.isConnected()) return;

                long silent = System.currentTimeMillis() - tunnel.getLastReceivedAt();
                if (silent > SILENT_TIMEOUT_MS) {
                    appendLog("链路静默 " + (silent / 1000) + " 秒，判定连接已死，主动断开以触发重连");
                    tunnel.disconnect();
                    return;
                }
                try {
                    tunnel.sendKeepAlive();
                } catch (Exception e) {
                    appendLog("心跳发送失败: " + e.getMessage());
                    tunnel.disconnect();
                    return;
                }
            }
        }, "SshKeepAlive");
        keepAliveThread.start();
    }

    private void stopKeepAlive() {
        if (keepAliveThread != null) {
            keepAliveThread.interrupt();
            keepAliveThread = null;
        }
    }

    private boolean sleepInterruptibly(long ms, int generation) {
        long end = System.currentTimeMillis() + ms;
        while (System.currentTimeMillis() < end) {
            if (!isAlive(generation)) return false;
            try {
                Thread.sleep(Math.min(250L, Math.max(1L, end - System.currentTimeMillis())));
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
        return isAlive(generation);
    }

    private boolean isAlive(int generation) {
        return isRunning
                && runGeneration.get() == generation
                && !Thread.currentThread().isInterrupted();
    }

    // ------------------------------------------------------------------
    // 端口发现
    // ------------------------------------------------------------------

    /** 手机侧 adbd 端点。host 取 mDNS 解析结果，可能是 Wi-Fi 地址而非回环地址。 */
    private static final class AdbEndpoint {
        final String host;
        final int port;

        AdbEndpoint(String host, int port) {
            this.host = (host == null || host.isEmpty()) ? "127.0.0.1" : host;
            this.port = port;
        }

        boolean isReachable(int timeoutMs) {
            if (port < 1 || port > 65535) return false;
            if (probe(host, port, timeoutMs)) return true;
            return !"127.0.0.1".equals(host) && probe("127.0.0.1", port, timeoutMs);
        }

        private static boolean probe(String h, int p, int timeoutMs) {
            try (Socket s = new Socket()) {
                s.connect(new InetSocketAddress(h, p), timeoutMs);
                return true;
            } catch (Exception e) {
                return false;
            }
        }

        @Override
        public String toString() {
            return host + ":" + port;
        }
    }

    /**
     * /proc/net/tcp* 兜底扫描。
     * Android 10+ 对普通应用读取该文件有 SELinux 限制，多数机型读不到，因此仅作最后手段。
     * 候选端口必须能实际连上才认为有效。
     */
    private AdbEndpoint scanProcNetEndpoint() {
        Set<Integer> ports = new LinkedHashSet<>();
        String[] paths = new String[]{"/proc/net/tcp6", "/proc/net/tcp"};
        for (String path : paths) {
            File f = new File(path);
            if (!f.exists() || !f.canRead()) continue;
            try (BufferedReader br = new BufferedReader(new FileReader(f))) {
                String line = br.readLine();
                while ((line = br.readLine()) != null) {
                    String[] parts = line.trim().split("\\s+");
                    if (parts.length >= 4 && "0A".equalsIgnoreCase(parts[3])) {
                        String[] addrPort = parts[1].split(":");
                        if (addrPort.length == 2) {
                            try {
                                int port = Integer.parseInt(addrPort[1], 16);
                                // adbd 的无线调试端口是内核分配的临时端口，落在 30000-49999 区间
                                if (port >= 30000 && port <= 49999) {
                                    ports.add(port);
                                }
                            } catch (NumberFormatException ignored) {
                            }
                        }
                    }
                }
            } catch (Exception ignored) {
            }
        }
        for (Integer p : ports) {
            AdbEndpoint ep = new AdbEndpoint("127.0.0.1", p);
            if (ep.isReachable(300)) {
                return ep;
            }
        }
        return null;
    }

    private void startMdnsDiscovery(final TunnelConfig.ListenMode mode) {
        stopMdnsDiscovery();
        discoveredEndpoint = null;
        resolving.set(false);
        nsdManager = (NsdManager) getSystemService(Context.NSD_SERVICE);
        if (nsdManager == null) {
            appendLog("系统 NsdManager 不可用，只能依赖 /proc 兜底");
            return;
        }

        discoveryListener = new NsdManager.DiscoveryListener() {
            @Override
            public void onDiscoveryStarted(String regType) {
                appendLog("已启动系统 NsdManager 监听: " + regType);
            }

            @Override
            public void onServiceFound(NsdServiceInfo service) {
                // Android 12+ 同一时刻只允许一个 resolve，重复调用会返回 FAILURE_ALREADY_ACTIVE
                if (!resolving.compareAndSet(false, true)) {
                    return;
                }
                resolveServiceWithRetry(service, mode, 0);
            }

            @Override public void onServiceLost(NsdServiceInfo service) {}
            @Override public void onDiscoveryStopped(String serviceType) {}
            @Override public void onStartDiscoveryFailed(String serviceType, int errorCode) {
                appendLog("mDNS 发现启动失败 (errorCode=" + errorCode + ")，改用 /proc 兜底扫描");
            }
            @Override public void onStopDiscoveryFailed(String serviceType, int errorCode) {}
        };

        try {
            nsdManager.discoverServices(mode.nsdServiceType, NsdManager.PROTOCOL_DNS_SD, discoveryListener);
        } catch (Exception e) {
            appendLog("mDNS 发现异常: " + e.getMessage() + "，400ms 后重试一次");
            new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(() -> {
                try {
                    if (nsdManager != null && discoveryListener != null) {
                        nsdManager.discoverServices(
                                mode.nsdServiceType, NsdManager.PROTOCOL_DNS_SD, discoveryListener);
                    }
                } catch (Exception e2) {
                    appendLog("mDNS 重试仍失败: " + e2.getMessage());
                }
            }, 400L);
        }
    }

    /**
     * resolveService 的重试包装。
     *
     * Android 12+ 在发现刚注册 / 刚重启时会抛 FAILURE_ALREADY_ACTIVE，
     * 原来直接放弃就会「一直轮询但永远收不到端口」，必须重启 App 才能恢复。
     * 这里最多重试 3 次，间隔 300/600/900ms。
     */
    private void resolveServiceWithRetry(final NsdServiceInfo service,
                                         final TunnelConfig.ListenMode mode,
                                         final int depth) {
        if (nsdManager == null) {
            resolving.set(false);
            return;
        }
        try {
            nsdManager.resolveService(service, new NsdManager.ResolveListener() {
                @Override
                public void onResolveFailed(NsdServiceInfo serviceInfo, int errorCode) {
                    resolving.set(false);
                    appendLog("解析服务 " + serviceInfo.getServiceName()
                            + " 失败 (errorCode=" + errorCode + ")，等待下次广播");
                }

                @Override
                public void onServiceResolved(NsdServiceInfo serviceInfo) {
                    resolving.set(false);
                    InetAddress host = serviceInfo.getHost();
                    int port = serviceInfo.getPort();
                    if (port <= 0) {
                        return;
                    }
                    String hostAddr = (host == null) ? null : host.getHostAddress();
                    AdbEndpoint ep = new AdbEndpoint(hostAddr, port);
                    if (discoveredEndpoint == null) {
                        discoveredEndpoint = ep;
                        appendLog("mDNS 发现" + modeName(mode) + "服务: "
                                + serviceInfo.getServiceName() + " -> " + ep);
                    }
                }
            });
        } catch (Exception e) {
            resolving.set(false);
            if (depth < 3) {
                final long delay = 300L * (depth + 1);
                appendLog("resolveService 异常: " + e.getMessage() + "，" + delay + "ms 后重试");
                new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(() -> {
                    if (!resolving.compareAndSet(false, true)) {
                        return;
                    }
                    resolveServiceWithRetry(service, mode, depth + 1);
                }, delay);
            } else {
                appendLog("resolveService 连续失败，等待下一次发现: " + e.getMessage());
            }
        }
    }

    private void stopMdnsDiscovery() {
        if (nsdManager != null && discoveryListener != null) {
            try {
                nsdManager.stopServiceDiscovery(discoveryListener);
            } catch (Exception ignored) {
            }
        }
        discoveryListener = null;
    }

    // ------------------------------------------------------------------
    // 电源 / WiFi 锁
    // ------------------------------------------------------------------

    @SuppressWarnings("deprecation")
    private void acquireLocks() {
        try {
            if (wakeLock == null) {
                PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
                if (pm != null) {
                    wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "SshAdbTunnel::tunnel");
                    wakeLock.setReferenceCounted(false);
                }
            }
            if (wakeLock != null && !wakeLock.isHeld()) {
                wakeLock.acquire();
            }
        } catch (Exception e) {
            appendLog("获取 WakeLock 失败: " + e.getMessage());
        }

        try {
            if (wifiLock == null) {
                WifiManager wm = (WifiManager) getApplicationContext()
                        .getSystemService(Context.WIFI_SERVICE);
                if (wm != null) {
                    int mode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                            ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                            : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
                    wifiLock = wm.createWifiLock(mode, "SshAdbTunnel::wifi");
                    wifiLock.setReferenceCounted(false);
                }
            }
            if (wifiLock != null && !wifiLock.isHeld()) {
                wifiLock.acquire();
            }
        } catch (Exception e) {
            appendLog("获取 WifiLock 失败: " + e.getMessage());
        }
    }

    private void releaseLocks() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
        } catch (Exception ignored) {
        }
        try {
            if (wifiLock != null && wifiLock.isHeld()) {
                wifiLock.release();
            }
        } catch (Exception ignored) {
        }
    }

    // ------------------------------------------------------------------
    // 生命周期
    // ------------------------------------------------------------------

    public void stopTunnel() {
        runGeneration.incrementAndGet();
        isRunning = false;
        stopMdnsDiscovery();
        stopKeepAlive();
        if (workerThread != null) {
            workerThread.interrupt();
            workerThread = null;
        }
        PureJavaSshReverseTunnel t = sshTunnel;
        sshTunnel = null;
        if (t != null) {
            new Thread(t::disconnect).start();
        }
        currentStatusBadge = "未启动";
        currentAutoPortStatus = "启动后 1s 轮询系统接口自动获取";
        notifyUiState();
        appendLog("服务已停止。");
        stopForegroundCompat();
        releaseLocks();
    }

    public boolean isTunnelRunning() {
        return isRunning;
    }

    private void updateUiState(String badge, String portStatus, boolean running) {
        this.currentStatusBadge = badge;
        this.currentAutoPortStatus = portStatus;
        this.isRunning = running;
        notifyUiState();
    }

    private void notifyUiState() {
        mainHandler.post(() -> {
            TunnelEventListener l = listener;
            if (l != null) {
                l.onStateChanged(currentStatusBadge, currentAutoPortStatus, isRunning);
            }
        });
    }

    private synchronized void appendLog(String msg) {
        String ts = new SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(new Date());
        logHistory.add("[" + ts + "] " + msg);
        if (logHistory.size() > 80) {
            logHistory.remove(0);
        }
        notifyUiLogs();
    }

    private void notifyUiLogs() {
        final List<String> snapshot;
        synchronized (this) {
            snapshot = new ArrayList<>(logHistory);
        }
        mainHandler.post(() -> {
            TunnelEventListener l = listener;
            if (l != null) {
                l.onLogsUpdated(snapshot);
            }
        });
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    "ssh_adb_channel",
                    "SSH ADB 反向隧道服务",
                    NotificationManager.IMPORTANCE_LOW
            );
            channel.setShowBadge(false);
            NotificationManager manager = getSystemService(NotificationManager.class);
            if (manager != null) {
                manager.createNotificationChannel(channel);
            }
        }
    }

    private void startForegroundInternal(String content) {
        Notification n = buildNotification(content);
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1001, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(1001, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(1001, n);
        }
    }

    @SuppressWarnings("deprecation")
    private void stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE);
        } else {
            stopForeground(true);
        }
    }

    private Notification buildNotification(String content) {
        Notification.Builder builder = (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ? new Notification.Builder(this, "ssh_adb_channel")
                : new Notification.Builder(this);

        PendingIntent pi = null;
        try {
            Intent open = new Intent(this, MainActivity.class);
            open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                flags |= PendingIntent.FLAG_IMMUTABLE;
            }
            pi = PendingIntent.getActivity(this, 0, open, flags);
        } catch (Exception ignored) {
        }

        builder.setContentTitle("SSH 反向隧道 ADB 控制")
                .setContentText(content)
                .setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setOngoing(true);
        if (pi != null) {
            builder.setContentIntent(pi);
        }
        return builder.build();
    }

    private void updateNotification(String content) {
        NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) {
            try {
                manager.notify(1001, buildNotification(content));
            } catch (Exception ignored) {
            }
        }
    }

    @Override
    public void onDestroy() {
        stopTunnel();
        super.onDestroy();
    }
}
