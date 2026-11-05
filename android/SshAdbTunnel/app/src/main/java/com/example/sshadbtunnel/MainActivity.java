package com.example.sshadbtunnel;

import android.Manifest;
import android.app.Activity;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.IBinder;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.List;

public class MainActivity extends Activity implements SshTunnelService.TunnelEventListener {

    private SshTunnelService tunnelService;
    private boolean bound = false;

    private EditText inputServerIp;
    private EditText inputSshPort;
    private EditText inputUsername;
    private RadioButton radioAuthPassword;
    private RadioButton radioAuthKey;
    private EditText inputPassword;
    private LinearLayout keyAuthContainer;
    private EditText inputPrivateKey;
    private EditText inputPassphrase;

    private RadioButton radioModePairing;
    private RadioButton radioModeConnect;
    private TextView textAutoPortStatus;
    private EditText inputRemotePortP;

    private TextView textKeepAliveStatus;
    private TextView textStatusBadge;
    private Button btnStartStop;
    private TextView textLogs;

    private final ServiceConnection connection = new ServiceConnection() {
        @Override
        public void onServiceConnected(ComponentName name, IBinder service) {
            SshTunnelService.LocalBinder binder = (SshTunnelService.LocalBinder) service;
            tunnelService = binder.getService();
            bound = true;
            tunnelService.setListener(MainActivity.this);
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {
            bound = false;
            tunnelService = null;
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildContentView());
        restorePrefs();
        requestNotificationPermission();

        Intent intent = new Intent(this, SshTunnelService.class);
        bindService(intent, connection, Context.BIND_AUTO_CREATE);
    }

    private static final int REQ_NOTIFICATION = 1001;

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},
                        REQ_NOTIFICATION);
            }
        }
    }

    private SharedPreferences prefs() {
        return getSharedPreferences("sshadbtunnel", Context.MODE_PRIVATE);
    }

    private void restorePrefs() {
        try {
            SharedPreferences p = prefs();
            if (p.contains("serverIp")) inputServerIp.setText(p.getString("serverIp", "203.0.113.10"));
            if (p.contains("sshPort")) inputSshPort.setText(p.getString("sshPort", "22"));
            if (p.contains("username")) inputUsername.setText(p.getString("username", "root"));
            if (p.contains("remotePortP")) inputRemotePortP.setText(p.getString("remotePortP", "5555"));
            if (p.getBoolean("keyAuth", false)) radioAuthKey.setChecked(true);
            if (p.getBoolean("connectMode", false)) radioModeConnect.setChecked(true);
        } catch (Exception ignored) {
        }
    }

    private void savePrefs() {
        try {
            prefs().edit()
                    .putString("serverIp", inputServerIp.getText().toString().trim())
                    .putString("sshPort", inputSshPort.getText().toString().trim())
                    .putString("username", inputUsername.getText().toString().trim())
                    .putString("remotePortP", inputRemotePortP.getText().toString().trim())
                    .putBoolean("keyAuth", radioAuthKey.isChecked())
                    .putBoolean("connectMode", radioModeConnect.isChecked())
                    .apply();
        } catch (Exception ignored) {
        }
    }

    private View buildContentView() {
        ScrollView scrollView = new ScrollView(this);
        scrollView.setBackgroundColor(Color.parseColor("#090D16"));
        scrollView.setFillViewport(true);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        root.setPadding(pad, pad, pad, pad);
        scrollView.addView(root);

        // Header Row
        LinearLayout headerRow = new LinearLayout(this);
        headerRow.setOrientation(LinearLayout.HORIZONTAL);
        headerRow.setGravity(Gravity.CENTER_VERTICAL);

        TextView title = new TextView(this);
        title.setText("SSH 反向隧道远程 ADB");
        title.setTextColor(Color.WHITE);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 20);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        headerRow.addView(title, titleLp);

        textStatusBadge = new TextView(this);
        textStatusBadge.setText("未启动");
        textStatusBadge.setTextColor(Color.parseColor("#38BDF8"));
        textStatusBadge.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        textStatusBadge.setPadding(dp(10), dp(4), dp(10), dp(4));
        textStatusBadge.setBackground(roundedRect("#1E293B", "#334155", 20));
        headerRow.addView(textStatusBadge);
        root.addView(headerRow);

        TextView subtitle = new TextView(this);
        subtitle.setText("手机端口免手填：1秒轮询系统接口自动获取，获取到即停止轮询并映射 127.0.0.1:P");
        subtitle.setTextColor(Color.parseColor("#94A3B8"));
        subtitle.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        subtitle.setPadding(0, dp(4), 0, dp(12));
        root.addView(subtitle);

        // Card 1: SSH Server & Auth
        LinearLayout cardSsh = createCard();
        cardSsh.addView(sectionTitle("1. SSH 服务器与认证配置"));

        inputServerIp = createInput("服务器 IP 或域名 (例如 203.0.113.10)", "203.0.113.10", InputType.TYPE_CLASS_TEXT);
        cardSsh.addView(labeledField("服务器 IP / 域名 *", inputServerIp));

        inputSshPort = createInput("SSH 端口 (默认 22)", "22", InputType.TYPE_CLASS_NUMBER);
        cardSsh.addView(labeledField("SSH 端口 *", inputSshPort));

        inputUsername = createInput("SSH 用户名 (例如 root / ubuntu)", "root", InputType.TYPE_CLASS_TEXT);
        cardSsh.addView(labeledField("SSH 用户名 *", inputUsername));

        // Auth Type RadioGroup
        RadioGroup authGroup = new RadioGroup(this);
        authGroup.setOrientation(RadioGroup.HORIZONTAL);
        radioAuthPassword = new RadioButton(this);
        radioAuthPassword.setId(View.generateViewId());
        radioAuthPassword.setText("密码认证");
        radioAuthPassword.setTextColor(Color.WHITE);

        radioAuthKey = new RadioButton(this);
        radioAuthKey.setId(View.generateViewId());
        radioAuthKey.setText("SSH 私钥认证");
        radioAuthKey.setTextColor(Color.WHITE);

        authGroup.addView(radioAuthPassword);
        authGroup.addView(radioAuthKey);
        authGroup.check(radioAuthPassword.getId());
        cardSsh.addView(authGroup);

        inputPassword = createInput("输入 SSH 登录密码", "", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        final View passwordFieldWrapper = labeledField("SSH 密码", inputPassword);
        cardSsh.addView(passwordFieldWrapper);

        keyAuthContainer = new LinearLayout(this);
        keyAuthContainer.setOrientation(LinearLayout.VERTICAL);
        keyAuthContainer.setVisibility(View.GONE);
        inputPrivateKey = createInput("粘贴 PEM / OpenSSH 私钥内容 (-----BEGIN OPENSSH PRIVATE KEY-----)", "", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        inputPrivateKey.setMinLines(3);
        keyAuthContainer.addView(labeledField("SSH 私钥 (Private Key)", inputPrivateKey));

        inputPassphrase = createInput("私钥口令 Passphrase (无加密请留空)", "", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        keyAuthContainer.addView(labeledField("私钥口令 (选填)", inputPassphrase));
        cardSsh.addView(keyAuthContainer);

        authGroup.setOnCheckedChangeListener((group, checkedId) -> {
            boolean isPwd = (checkedId == radioAuthPassword.getId());
            passwordFieldWrapper.setVisibility(isPwd ? View.VISIBLE : View.GONE);
            keyAuthContainer.setVisibility(isPwd ? View.GONE : View.VISIBLE);
        });

        root.addView(cardSsh, cardLayoutParams());

        // Card 2: Listen Mode & Server Port P
        LinearLayout cardMode = createCard();
        cardMode.addView(sectionTitle("2. 监听端口类型与服务器端口 P"));

        RadioGroup modeGroup = new RadioGroup(this);
        modeGroup.setOrientation(RadioGroup.VERTICAL);
        radioModePairing = new RadioButton(this);
        radioModePairing.setId(View.generateViewId());
        radioModePairing.setText("监听配对端口（首次配对时使用）");
        radioModePairing.setTextColor(Color.WHITE);

        radioModeConnect = new RadioButton(this);
        radioModeConnect.setId(View.generateViewId());
        radioModeConnect.setText("监听连接端口（配对完成后使用）");
        radioModeConnect.setTextColor(Color.WHITE);

        modeGroup.addView(radioModePairing);
        modeGroup.addView(radioModeConnect);
        modeGroup.check(radioModePairing.getId());
        cardMode.addView(modeGroup);

        // Auto-Acquired System Port Status Banner
        LinearLayout autoBox = new LinearLayout(this);
        autoBox.setOrientation(LinearLayout.VERTICAL);
        autoBox.setPadding(dp(12), dp(10), dp(12), dp(10));
        autoBox.setBackground(roundedRect("#0F172A", "#0284C7", 12));

        TextView autoTitle = new TextView(this);
        autoTitle.setText("手机本机端口 (无需手动填写 · 1s轮询系统接口自动获取)");
        autoTitle.setTextColor(Color.parseColor("#38BDF8"));
        autoTitle.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        autoBox.addView(autoTitle);

        textAutoPortStatus = new TextView(this);
        textAutoPortStatus.setText("启动后 1s 轮询系统接口自动获取，获取到即停止轮询");
        textAutoPortStatus.setTextColor(Color.parseColor("#34D399"));
        textAutoPortStatus.setTypeface(Typeface.MONOSPACE, Typeface.BOLD);
        textAutoPortStatus.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        textAutoPortStatus.setPadding(0, dp(4), 0, 0);
        autoBox.addView(textAutoPortStatus);

        LinearLayout.LayoutParams autoLp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        autoLp.setMargins(0, dp(6), 0, dp(8));
        cardMode.addView(autoBox, autoLp);

        inputRemotePortP = createInput("服务器映射端口 P (例如 5555)", "5555", InputType.TYPE_CLASS_NUMBER);
        cardMode.addView(labeledField("服务器方使用端口 P *", inputRemotePortP));

        root.addView(cardMode, cardLayoutParams());

        // Start / Stop Button
        btnStartStop = new Button(this);
        btnStartStop.setText("启动反向隧道服务 (1s轮询自动获取端口)");
        btnStartStop.setTextColor(Color.WHITE);
        btnStartStop.setTypeface(Typeface.DEFAULT_BOLD);
        btnStartStop.setBackground(roundedRect("#2563EB", "#38BDF8", 12));
        btnStartStop.setPadding(dp(14), dp(14), dp(14), dp(14));
        btnStartStop.setOnClickListener(v -> toggleTunnelService());
        root.addView(btnStartStop, cardLayoutParams());

        // Card: 后台保活
        LinearLayout cardKeep = createCard();
        cardKeep.setBackground(roundedRect("#1C1917", "#F59E0B", 14));
        cardKeep.addView(sectionTitle("3. 长时间挂机（截屏推流）保活设置"));

        textKeepAliveStatus = new TextView(this);
        textKeepAliveStatus.setTextColor(Color.parseColor("#FCD34D"));
        textKeepAliveStatus.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        textKeepAliveStatus.setPadding(0, 0, 0, dp(8));
        cardKeep.addView(textKeepAliveStatus);

        Button btnBattery = new Button(this);
        btnBattery.setText("允许后台运行（忽略电池优化）");
        btnBattery.setTextColor(Color.WHITE);
        btnBattery.setBackground(roundedRect("#B45309", "#F59E0B", 10));
        btnBattery.setOnClickListener(v -> requestIgnoreBatteryOptimizations());
        cardKeep.addView(btnBattery);

        Button btnPowerSettings = new Button(this);
        btnPowerSettings.setText("打开系统电池优化设置");
        btnPowerSettings.setTextColor(Color.WHITE);
        btnPowerSettings.setBackground(roundedRect("#78350F", "#B45309", 10));
        btnPowerSettings.setOnClickListener(v -> openBatterySettings());
        cardKeep.addView(btnPowerSettings);

        root.addView(cardKeep, cardLayoutParams());
        updateKeepAliveStatus();

        // Card 4: Live Logs
        LinearLayout cardLogs = createCard();
        cardLogs.addView(sectionTitle("4. 系统接口 1s 轮询与 SSH 隧道实时日志"));
        textLogs = new TextView(this);
        textLogs.setText("[就绪] 请填写服务器参数并点击启动，随后切往系统「无线调试」打开配对码窗口...");
        textLogs.setTextColor(Color.parseColor("#CBD5E1"));
        textLogs.setTypeface(Typeface.MONOSPACE);
        textLogs.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        cardLogs.addView(textLogs);
        root.addView(cardLogs, cardLayoutParams());

        return scrollView;
    }

    private void updateKeepAliveStatus() {
        if (textKeepAliveStatus == null) return;
        boolean exempt = false;
        try {
            android.os.PowerManager pm =
                    (android.os.PowerManager) getSystemService(Context.POWER_SERVICE);
            exempt = pm != null && pm.isIgnoringBatteryOptimizations(getPackageName());
        } catch (Exception ignored) {
        }
        textKeepAliveStatus.setText(exempt
                ? "✔ 已加入电池优化白名单，可长时间后台运行"
                : "✘ 未加入白名单，息屏后系统可能冻结进程导致隧道中断");
    }

    private void requestIgnoreBatteryOptimizations() {
        try {
            Intent i = new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            i.setData(android.net.Uri.parse("package:" + getPackageName()));
            startActivity(i);
        } catch (Exception e) {
            openBatterySettings();
        }
    }

    private void openBatterySettings() {
        try {
            startActivity(new Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        } catch (Exception e) {
            Toast.makeText(this, "无法打开电池优化设置: " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    private void toggleTunnelService() {
        if (!bound || tunnelService == null) {
            Toast.makeText(this, "服务尚未就绪，请稍后重试", Toast.LENGTH_SHORT).show();
            return;
        }
        if (tunnelService.isTunnelRunning()) {
            tunnelService.stopTunnel();
            return;
        }

        String ip = inputServerIp.getText().toString().trim();
        if (ip.isEmpty()) {
            Toast.makeText(this, "请填写服务器 IP", Toast.LENGTH_SHORT).show();
            return;
        }
        int sshPort = parseIntOrDefault(inputSshPort.getText().toString().trim(), 22);
        if (sshPort < 1 || sshPort > 65535) {
            Toast.makeText(this, "SSH 端口需在 1-65535 之间", Toast.LENGTH_SHORT).show();
            return;
        }
        String user = inputUsername.getText().toString().trim();
        if (user.isEmpty()) user = "root";

        TunnelConfig.AuthType authType = radioAuthPassword.isChecked()
                ? TunnelConfig.AuthType.PASSWORD
                : TunnelConfig.AuthType.PRIVATE_KEY;

        if (authType == TunnelConfig.AuthType.PASSWORD
                && inputPassword.getText().toString().isEmpty()) {
            Toast.makeText(this, "请填写 SSH 密码", Toast.LENGTH_SHORT).show();
            return;
        }
        if (authType == TunnelConfig.AuthType.PRIVATE_KEY
                && inputPrivateKey.getText().toString().trim().isEmpty()) {
            Toast.makeText(this, "请粘贴 SSH 私钥内容", Toast.LENGTH_SHORT).show();
            return;
        }

        TunnelConfig.ListenMode listenMode = radioModePairing.isChecked()
                ? TunnelConfig.ListenMode.PAIRING
                : TunnelConfig.ListenMode.CONNECT;

        int remoteP = parseIntOrDefault(inputRemotePortP.getText().toString().trim(), 5555);
        if (remoteP < 1024 || remoteP > 65535) {
            Toast.makeText(this, "服务器端口 P 需在 1024-65535 之间", Toast.LENGTH_SHORT).show();
            return;
        }

        TunnelConfig config = new TunnelConfig(
                ip,
                sshPort,
                user,
                authType,
                inputPassword.getText().toString(),
                inputPrivateKey.getText().toString(),
                inputPassphrase.getText().toString(),
                listenMode,
                remoteP
        );

        savePrefs();

        try {
            Intent fgIntent = new Intent(this, SshTunnelService.class);
            startService(fgIntent);
        } catch (Exception e) {
            Toast.makeText(this, "启动前台服务失败: " + e.getMessage(), Toast.LENGTH_LONG).show();
            return;
        }
        tunnelService.startTunnel(config);
    }

    @Override
    public void onStateChanged(String statusBadge, String autoPortStatus, boolean isRunning) {
        textStatusBadge.setText(statusBadge);
        textAutoPortStatus.setText(autoPortStatus);
        if (isRunning) {
            btnStartStop.setText("停止反向隧道服务");
            btnStartStop.setBackground(roundedRect("#DC2626", "#F87171", 12));
        } else {
            btnStartStop.setText("启动反向隧道服务 (1s轮询自动获取端口)");
            btnStartStop.setBackground(roundedRect("#2563EB", "#38BDF8", 12));
        }
    }

    @Override
    public void onLogsUpdated(List<String> logs) {
        StringBuilder sb = new StringBuilder();
        int start = Math.max(0, logs.size() - 15);
        for (int i = start; i < logs.size(); i++) {
            sb.append(logs.get(i)).append("\n");
        }
        textLogs.setText(sb.toString().trim());
    }

    private LinearLayout createCard() {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        int p = dp(14);
        card.setPadding(p, p, p, p);
        card.setBackground(roundedRect("#1E293B", "#334155", 14));
        return card;
    }

    private LinearLayout.LayoutParams cardLayoutParams() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        lp.setMargins(0, 0, 0, dp(12));
        return lp;
    }

    private TextView sectionTitle(String text) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextColor(Color.parseColor("#38BDF8"));
        tv.setTypeface(Typeface.DEFAULT_BOLD);
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        tv.setPadding(0, 0, 0, dp(8));
        return tv;
    }

    private EditText createInput(String hint, String initial, int inputType) {
        EditText et = new EditText(this);
        et.setHint(hint);
        et.setHintTextColor(Color.parseColor("#64748B"));
        et.setText(initial);
        et.setTextColor(Color.WHITE);
        et.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        et.setInputType(inputType);
        et.setPadding(dp(12), dp(10), dp(12), dp(10));
        et.setBackground(roundedRect("#090D16", "#475569", 10));
        return et;
    }

    private View labeledField(String label, View field) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(0, dp(4), 0, dp(8));
        TextView tv = new TextView(this);
        tv.setText(label);
        tv.setTextColor(Color.parseColor("#CBD5E1"));
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        tv.setPadding(0, 0, 0, dp(4));
        box.addView(tv);
        box.addView(field);
        return box;
    }

    private GradientDrawable roundedRect(String fillHex, String strokeHex, int radiusDp) {
        GradientDrawable gd = new GradientDrawable();
        gd.setColor(Color.parseColor(fillHex));
        gd.setCornerRadius(dp(radiusDp));
        gd.setStroke(dp(1), Color.parseColor(strokeHex));
        return gd;
    }

    private int dp(int v) {
        return (int) TypedValue.applyDimension(
                TypedValue.COMPLEX_UNIT_DIP,
                v,
                getResources().getDisplayMetrics()
        );
    }

    private int parseIntOrDefault(String s, int def) {
        try {
            return Integer.parseInt(s);
        } catch (Exception e) {
            return def;
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        updateKeepAliveStatus();
    }

    @Override
    protected void onDestroy() {
        if (bound && tunnelService != null) {
            tunnelService.clearListener(this);
        }
        if (bound) {
            try {
                unbindService(connection);
            } catch (Exception ignored) {
            }
            bound = false;
        }
        super.onDestroy();
    }
}
