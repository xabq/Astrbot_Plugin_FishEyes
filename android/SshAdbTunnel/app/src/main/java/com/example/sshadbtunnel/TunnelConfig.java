package com.example.sshadbtunnel;

public class TunnelConfig {

    public enum AuthType {
        PASSWORD,
        PRIVATE_KEY
    }

    public enum ListenMode {
        PAIRING("监听配对端口 (Pairing)", "_adb-tls-pairing._tcp."),
        CONNECT("监听连接端口 (Connect)", "_adb-tls-connect._tcp.");

        public final String label;
        public final String nsdServiceType;

        ListenMode(String label, String nsdServiceType) {
            this.label = label;
            this.nsdServiceType = nsdServiceType;
        }
    }

    public final String serverIp;
    public final int sshPort;
    public final String username;
    public final AuthType authType;
    public final String password;
    public final String privateKey;
    public final String keyPassphrase;
    public final ListenMode listenMode;
    public final int remotePortP;

    public TunnelConfig(
            String serverIp,
            int sshPort,
            String username,
            AuthType authType,
            String password,
            String privateKey,
            String keyPassphrase,
            ListenMode listenMode,
            int remotePortP
    ) {
        this.serverIp = serverIp;
        this.sshPort = sshPort;
        this.username = username;
        this.authType = authType;
        this.password = password;
        this.privateKey = privateKey;
        this.keyPassphrase = keyPassphrase;
        this.listenMode = listenMode;
        this.remotePortP = remotePortP;
    }
}
