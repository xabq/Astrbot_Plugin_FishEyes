/**
 * AstrbotFishEyes - 屏幕与远程控制台前端逻辑 (v1.1.0)
 * 支持：
 * 1. 左上角【PC / Android】控制目标切换，切换后控制面板与连接配置随之切换；
 * 2. Android 目标：ADB 配对（配对码）、连接、设备列表、应用启动、ADB Shell 快捷执行；
 * 3. 显式【连接】与【断开连接】双按钮控制；
 * 4. 页面/视口可见性感知：当未查看本插件 WebUI（切出标签页、隐藏或关闭）或处于未连接状态时，立即停止刷新画面；
 * 5. 无操作自动断连时间配置与状态倒计时同步。
 */

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const state = {
  target: "pc", // 'pc' | 'android'
  mode: "local",
  version: "",
  connected: false,
  isLocked: false,
  screenWidth: 0,
  screenHeight: 0,
  idleTimeout: 300,
  idleRemaining: null,
  currentClickMode: "left", // 'left' | 'right' | 'double'
  autoRefreshTimer: null,
  statusPollTimer: null,
  isFetchingFrame: false,
  isViewportVisible: true,
  isSwitching: false,
  android: {
    adb_available: false,
    adb_path: "",
    adb_version: "",
    adb_error: "",
    serial: "",
    model: "",
    manufacturer: "",
    android_version: "",
    devices: [],
    device_screen: { width: 0, height: 0 },
    capture_screen: { width: 0, height: 0 },
  },
  androidApps: [],
};

// API 请求封装（无缝兼容 AstrBot 桥梁及独立运行模式）
async function apiGet(endpoint, params = {}) {
  try {
    if (bridge && typeof bridge.apiGet === "function") {
      return await bridge.apiGet(endpoint, params);
    }
    const qs = new URLSearchParams(params).toString();
    const url = `/api/plug/AstrbotFishEyes/${endpoint}${qs ? "?" + qs : ""}`;
    const resp = await fetch(url);
    return await resp.json();
  } catch (err) {
    console.error(`[API GET 失败: ${endpoint}]`, err);
    throw err;
  }
}

async function apiPost(endpoint, data = {}) {
  try {
    if (bridge && typeof bridge.apiPost === "function") {
      return await bridge.apiPost(endpoint, data);
    }
    const url = `/api/plug/AstrbotFishEyes/${endpoint}`;
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    return await resp.json();
  } catch (err) {
    console.error(`[API POST 失败: ${endpoint}]`, err);
    throw err;
  }
}

function unwrap(res) {
  if (res && res.data !== undefined) return res.data;
  return res;
}

function apiError(res) {
  if (res && res.status === "error") return res.error || res.message || "未知错误";
  return null;
}

function escapeHtml(text) {
  return String(text === undefined || text === null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// =========================================================================
//  DOM 元素引用
// =========================================================================
const elements = {
  statusBadge: document.getElementById("status-badge"),
  statusText: document.getElementById("status-text"),
  modeText: document.getElementById("mode-text"),
  targetText: document.getElementById("target-text"),
  screenResText: document.getElementById("screen-res-text"),
  idleBadge: document.getElementById("idle-badge"),
  idleTimerText: document.getElementById("idle-timer-text"),
  hudLatency: document.getElementById("hud-latency"),
  coordsHud: document.getElementById("coords-hud"),
  screenViewport: document.getElementById("screen-viewport"),
  screenImg: document.getElementById("screen-canvas"),
  canvasContainer: document.getElementById("canvas-container"),
  loadingPlaceholder: document.getElementById("loading-placeholder"),
  screenHint: document.getElementById("screen-hint"),
  brandTitle: document.getElementById("brand-title"),
  brandSubtitle: document.getElementById("brand-subtitle"),

  // 左上角目标切换
  targetSwitch: document.getElementById("target-switch"),
  targetOptions: document.querySelectorAll(".target-option"),

  btnConnect: document.getElementById("btn-connect"),
  btnDisconnect: document.getElementById("btn-disconnect"),
  btnRefresh: document.getElementById("btn-refresh"),
  btnEmergencyLock: document.getElementById("btn-emergency-lock"),
  autoRefreshSelect: document.getElementById("auto-refresh-select"),

  // 鼠标点击模式选择
  btnMouseLeft: document.getElementById("btn-mouse-left"),
  btnMouseRight: document.getElementById("btn-mouse-right"),
  btnMouseDouble: document.getElementById("btn-mouse-double"),

  // 文本输入
  inputText: document.getElementById("input-type-text"),
  btnSendText: document.getElementById("btn-send-text"),
  inputAndroidText: document.getElementById("input-android-text"),
  btnSendAndroidText: document.getElementById("btn-send-android-text"),

  // 滚轮
  btnScrollUp: document.getElementById("btn-scroll-up"),
  btnScrollDown: document.getElementById("btn-scroll-down"),

  // Android：应用 / Shell
  inputAppPackage: document.getElementById("input-app-package"),
  btnLaunchApp: document.getElementById("btn-launch-app"),
  btnStopApp: document.getElementById("btn-stop-app"),
  btnRefreshApps: document.getElementById("btn-refresh-apps"),
  androidAppList: document.getElementById("android-app-list"),
  inputAdbShell: document.getElementById("input-adb-shell"),
  btnRunShell: document.getElementById("btn-run-shell"),
  adbShellOutput: document.getElementById("adb-shell-output"),

  // 选项卡
  tabBtns: document.querySelectorAll(".tab-btn"),
  tabContents: document.querySelectorAll(".tab-content"),

  // 配置表单（PC）
  settingMode: document.getElementById("setting-mode"),
  settingAutoConnect: document.getElementById("setting-auto-connect"),
  settingIdleTimeout: document.getElementById("setting-idle-timeout"),
  settingRemoteHost: document.getElementById("setting-remote-host"),
  settingRemotePort: document.getElementById("setting-remote-port"),
  settingRemoteToken: document.getElementById("setting-remote-token"),
  settingUseSsh: document.getElementById("setting-use-ssh"),
  settingSshHost: document.getElementById("setting-ssh-host"),
  settingSshPort: document.getElementById("setting-ssh-port"),
  settingSshUser: document.getElementById("setting-ssh-user"),
  settingSshPass: document.getElementById("setting-ssh-pass"),
  settingSshKey: document.getElementById("setting-ssh-key"),

  // 配置表单（Android / ADB 配对与连接）
  adbStatusChip: document.getElementById("adb-status-chip"),
  adbHintBox: document.getElementById("adb-hint-box"),
  btnAdbDiag: document.getElementById("btn-adb-diag"),
  adbDiagOutput: document.getElementById("adb-diag-output"),
  settingAdbPath: document.getElementById("setting-adb-path"),
  settingAdbSerial: document.getElementById("setting-adb-serial"),
  settingAdbPairHost: document.getElementById("setting-adb-pair-host"),
  settingAdbPairPort: document.getElementById("setting-adb-pair-port"),
  settingAdbHost: document.getElementById("setting-adb-host"),
  settingAdbPort: document.getElementById("setting-adb-port"),
  settingAdbMaxWidth: document.getElementById("setting-adb-max-width"),
  settingAdbTextMode: document.getElementById("setting-adb-text-mode"),
  settingAdbIdleTimeout: document.getElementById("setting-adb-idle-timeout"),
  adbSelfCheck: document.getElementById("adb-selfcheck"),
  inputPairCode: document.getElementById("input-pair-code"),
  btnAdbPair: document.getElementById("btn-adb-pair"),
  btnAdbConnect: document.getElementById("btn-adb-connect"),
  btnAdbDisconnect: document.getElementById("btn-adb-disconnect"),
  btnAdbRefresh: document.getElementById("btn-adb-refresh"),
  adbDeviceList: document.getElementById("adb-device-list"),

  btnSaveSettings: document.getElementById("btn-save-settings"),
  btnTestConnect: document.getElementById("btn-test-connect"),
  btnSettingsDisconnect: document.getElementById("btn-settings-disconnect"),

  // 日志列表
  logListContainer: document.getElementById("log-list-container"),
  btnRefreshLogs: document.getElementById("btn-refresh-logs"),
};

// =========================================================================
//  可见性检测：确保未查看 WebUI 时绝不后台刷新画面
// =========================================================================

function isWebUIVisible() {
  if (document.hidden || document.visibilityState !== "visible") {
    return false;
  }
  if (!state.isViewportVisible) {
    return false;
  }
  return true;
}

function stopFrameRefreshTimer() {
  if (state.autoRefreshTimer) {
    clearTimeout(state.autoRefreshTimer);
    clearInterval(state.autoRefreshTimer);
    state.autoRefreshTimer = null;
  }
}

function syncFrameRefreshTimer() {
  stopFrameRefreshTimer();

  // 仅当【已连接】且【正在查看当前 WebUI】时才允许开启定时画面刷新
  if (!state.connected || !isWebUIVisible()) {
    return;
  }

  const interval = parseInt(elements.autoRefreshSelect.value, 10);
  if (!(interval > 0)) {
    return;
  }
  // 用「上一帧回来再排下一帧」代替固定 setInterval：
  // 慢链路下不会堆积请求，快链路下也不会白白等待。
  const tick = async () => {
    if (!state.connected || !isWebUIVisible()) {
      stopFrameRefreshTimer();
      return;
    }
    await fetchFrame("webui_auto");
    if (state.connected && isWebUIVisible()) {
      state.autoRefreshTimer = setTimeout(tick, interval);
    }
  };
  state.autoRefreshTimer = setTimeout(tick, interval);
}

function disconnectedMessage() {
  const isAndroid = state.target === "android";
  return `当前未连接${isAndroid ? " Android 设备" : "被控电脑"}（已释放画面缓存与内存）<br />
     <span style="font-size: 12px; color: var(--text-muted); display: inline-block; margin-top: 6px;">
       ${
         isAndroid
           ? "请先在右侧【连接配置 → 配对与连接】中完成配对并连接设备；未连接时若 Bot 需要操作手机会自动尝试连接一次。"
           : "请点击上方或右侧的【🔗 连接】按钮建立连接；未连接时若 Bot 需要操作电脑会自动尝试连接一次。"
       }
     </span>`;
}

function clearCanvasForDisconnected(messageHtml) {
  stopFrameRefreshTimer();
  elements.screenImg.style.display = "none";
  elements.screenImg.removeAttribute("src");
  elements.loadingPlaceholder.style.display = "block";
  elements.loadingPlaceholder.innerHTML = messageHtml || disconnectedMessage();
  elements.hudLatency.textContent = "延迟: -- ms";
}

// =========================================================================
//  控制目标切换（左上角 PC / Android）
// =========================================================================

function applyTargetUI() {
  const isAndroid = state.target === "android";

  document.body.dataset.target = state.target;

  elements.targetOptions.forEach((btn) => {
    const active = btn.dataset.target === state.target;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });

  document.querySelectorAll(".target-group").forEach((group) => {
    const isAndroidGroup = group.id.indexOf("android") === 0;
    const show = isAndroid ? isAndroidGroup : !isAndroidGroup;
    group.style.display = show ? "" : "none";
  });

  elements.brandTitle.textContent = isAndroid
    ? "AstrbotFishEyes · 安卓手机控制 (ADB)"
    : "AstrbotFishEyes · 电脑屏幕与远程控制";
  elements.brandSubtitle.textContent = `${
    isAndroid ? "AstrBot Android ADB Console" : "AstrBot Computer Use & Interactive Console"
  }${state.version ? " · v" + state.version : ""}`;
  elements.targetText.textContent = isAndroid ? "Android" : "PC";
  elements.screenHint.textContent = isAndroid
    ? "💡 点击画面 = 点按手机屏幕"
    : "💡 点击画面直接下发鼠标点击";
  elements.btnMouseRight.textContent = isAndroid ? "长按模式" : "右键模式";
  elements.coordsHud.textContent = isAndroid ? "坐标: (X: --, Y: --)" : "坐标: (X: --, Y: --)";
  elements.btnAdbDisconnect.disabled = !(isAndroid && state.connected);
}

async function switchTarget(target) {
  if (state.isSwitching || target === state.target) return;

  state.isSwitching = true;
  const previous = state.target;
  state.target = target;
  applyTargetUI();
  stopFrameRefreshTimer();
  elements.statusBadge.className = "badge connecting";
  elements.statusText.textContent = "正在切换目标...";
  elements.loadingPlaceholder.style.display = "block";
  elements.loadingPlaceholder.textContent = target === "android"
    ? "正在切换到 Android（ADB）目标..."
    : "正在切换到 PC 目标...";

  try {
    const res = await apiPost("switch", { target, connect: true });
    const err = apiError(res);
    if (err) throw new Error(err);

    const data = unwrap(res) || {};
    applyStatusData(data);
    if (state.connected) {
      await fetchFrame("webui_manual");
    } else {
      clearCanvasForDisconnected(
        `${
          target === "android"
            ? "已切换到 Android 目标，但尚未连接设备。<br /><span style=\"font-size:12px;color:var(--text-muted);\">请在右侧【连接配置 → 配对与连接】中完成配对与连接。</span>"
            : "已切换到 PC 目标，但尚未连接被控电脑。<br /><span style=\"font-size:12px;color:var(--text-muted);\">请点击【🔗 连接】建立连接。</span>"
        }${data.error ? `<br /><span style="font-size:12px;color:var(--danger-color);">${escapeHtml(data.error)}</span>` : ""}`
      );
    }
    if (target === "android") {
      await refreshAndroidDevices(true);
    }
  } catch (err) {
    state.target = previous;
    applyTargetUI();
    elements.statusBadge.className = "badge offline";
    elements.statusText.textContent = "切换失败";
    clearCanvasForDisconnected(`❌ 切换控制目标失败：${escapeHtml(err.message || err)}`);
    await updateStatus();
  } finally {
    state.isSwitching = false;
  }
}

// =========================================================================
//  核心逻辑：状态同步与连接/断开控制
// =========================================================================

function applyStatusData(data) {
  const wasConnected = state.connected;
  state.connected = !!data.connected;
  state.mode = data.mode || "local";
  state.isLocked = !!data.is_locked;
  state.idleTimeout = data.idle_timeout !== undefined ? data.idle_timeout : state.idleTimeout;
  state.idleRemaining = data.idle_remaining;

  if (data.target && data.target !== state.target && !state.isSwitching) {
    state.target = data.target;
    applyTargetUI();
  }
  if (data.android) {
    state.android = Object.assign({}, state.android, data.android);
    renderAndroidInfo();
  }

  if (data.version && data.version !== state.version) {
    state.version = data.version;
    elements.brandSubtitle.textContent = `${
      state.target === "android" ? "AstrBot Android ADB Console" : "AstrBot Computer Use & Interactive Console"
    } · v${state.version}`;
  }

  if (data.screen && data.screen.width > 0) {
    state.screenWidth = data.screen.width;
    state.screenHeight = data.screen.height;
  } else if (!state.connected) {
    state.screenWidth = 0;
    state.screenHeight = 0;
  }

  if (state.target === "android") {
    const model = [state.android.manufacturer, state.android.model].filter(Boolean).join(" ");
    elements.modeText.textContent = state.connected ? "ADB 已连接" : "ADB 无线调试";
    elements.modeText.title = [
      state.android.serial ? `设备: ${state.android.serial}` : "",
      model,
      state.android.android_version ? `Android ${state.android.android_version}` : "",
    ]
      .filter(Boolean)
      .join(" · ");
  } else {
    elements.modeText.textContent = state.mode === "local" ? "本机 (Local)" : "远程 (Remote)";
    elements.modeText.title = "";
  }

  elements.screenResText.textContent =
    state.screenWidth > 0 ? `${state.screenWidth} x ${state.screenHeight}` : "-- x --";

  // 同步连接/断开按钮状态
  elements.btnConnect.disabled = state.connected;
  elements.btnDisconnect.disabled = !state.connected;
  elements.btnRefresh.disabled = !state.connected;
  elements.btnTestConnect.disabled = state.connected;
  elements.btnSettingsDisconnect.disabled = !state.connected;
  elements.btnAdbDisconnect.disabled = !(state.connected && state.target === "android");

  if (state.connected) {
    elements.statusBadge.className = "badge online";
    elements.statusText.textContent = state.target === "android" ? "设备已连接" : "已连接就绪";
    if (state.idleTimeout > 0 && state.idleRemaining !== null && state.idleRemaining !== undefined) {
      elements.idleBadge.style.display = "inline-flex";
      elements.idleTimerText.textContent = `${state.idleRemaining}s`;
    } else {
      elements.idleBadge.style.display = "none";
    }
    syncFrameRefreshTimer();
  } else {
    elements.statusBadge.className = "badge offline";
    const errMsg = data.error ? `未连接 (${data.error})` : "未连接";
    elements.statusText.textContent = errMsg;
    elements.statusText.title = data.error || "";
    elements.idleBadge.style.display = "none";
    if (wasConnected || elements.screenImg.style.display !== "none") {
      clearCanvasForDisconnected();
    } else {
      stopFrameRefreshTimer();
    }
  }

  updateLockUI();
}

async function updateStatus() {
  if (!isWebUIVisible()) return;
  try {
    const res = await apiGet("status");
    const err = apiError(res);
    if (err) throw new Error(err);
    applyStatusData(unwrap(res) || {});
  } catch (err) {
    elements.statusBadge.className = "badge offline";
    elements.statusText.textContent = "通信失败";
    stopFrameRefreshTimer();
  }
}

async function connectTarget() {
  try {
    elements.btnConnect.disabled = true;
    elements.btnTestConnect.disabled = true;
    elements.statusBadge.className = "badge connecting";
    elements.statusText.textContent = "正在连接...";
    elements.loadingPlaceholder.style.display = "block";
    elements.loadingPlaceholder.textContent = "正在建立连接并获取屏幕画面...";

    const res = await apiPost("connect", {});
    const err = apiError(res);
    if (err) throw new Error(err);
    const data = unwrap(res) || {};
    applyStatusData(data);

    if (data.connected) {
      await fetchFrame("webui_manual");
    } else {
      clearCanvasForDisconnected(
        `❌ 连接失败：${escapeHtml(data.error || "请检查被控端服务是否已开启")}<br />
         <span style="font-size: 12px; color: var(--text-muted); display: inline-block; margin-top: 6px;">
           ${
             state.target === "android"
               ? "请确认手机「无线调试」已开启、SshAdbTunnel 隧道已就绪，并在【连接配置 → 配对与连接】中检查地址与端口。"
               : "确认被控端运行后，可再次点击【🔗 连接】按钮重试。"
           }
         </span>`
      );
    }
  } catch (err) {
    elements.btnConnect.disabled = false;
    elements.btnTestConnect.disabled = false;
    elements.statusBadge.className = "badge offline";
    elements.statusText.textContent = "连接异常";
    clearCanvasForDisconnected(`❌ 连接请求异常: ${escapeHtml(err.message || err)}`);
  }
}

async function disconnectTarget() {
  try {
    stopFrameRefreshTimer();
    const res = await apiPost("disconnect", {});
    const err = apiError(res);
    if (err) throw new Error(err);
    const data = unwrap(res) || {};
    applyStatusData(data);
    clearCanvasForDisconnected(
      `🔌 已主动断开连接并释放内存资源<br />
       <span style="font-size: 12px; color: var(--text-muted); display: inline-block; margin-top: 6px;">
         需要查看或控制设备时，点击【🔗 连接】即可重新连接。
       </span>`
    );
  } catch (err) {
    console.error("断开连接异常:", err);
  }
}

async function fetchFrame(source = "webui_auto") {
  // 未连接或未查看 WebUI 时绝不请求截图
  if (!state.connected) return;
  if (source === "webui_auto" && !isWebUIVisible()) {
    stopFrameRefreshTimer();
    return;
  }
  if (state.isFetchingFrame) return;

  state.isFetchingFrame = true;
  const t0 = performance.now();

  try {
    const res = await apiGet("screenshot", { quality: 75, source: source });
    const err = apiError(res);
    if (err) {
      // 若后端因超时无操作已自动断连，同步刷新前端状态并停止定时器
      await updateStatus();
      return;
    }
    const data = unwrap(res) || {};
    if (data && (data.data_url || data.base64)) {
      const src = data.data_url || `data:image/jpeg;base64,${data.base64}`;
      elements.screenImg.src = src;
      elements.screenImg.style.display = "block";
      elements.loadingPlaceholder.style.display = "none";

      if (data.width && data.height) {
        state.screenWidth = data.width;
        state.screenHeight = data.height;
        elements.screenResText.textContent = `${data.width} x ${data.height}`;
      }

      const cost = Math.round(performance.now() - t0);
      const serverMs = data.cost_ms;
      const serverTag = typeof serverMs === "number" ? `（服务端 ${serverMs} ms）` : "";
      if (data.mode === "stream") {
        elements.hudLatency.textContent = `延迟: ${cost} ms${serverTag} · 视频流 (帧龄 ${data.stream_age_ms || 0} ms)`;
      } else if (data.mode === "device_jpeg") {
        elements.hudLatency.textContent = `延迟: ${cost} ms${serverTag} · 设备端 JPEG`;
      } else if (data.mode === "screenshot") {
        elements.hudLatency.textContent =
          `延迟: ${cost} ms${serverTag} · 截图 (抓图 ${data.capture_ms || 0} / 编码 ${data.encode_ms || 0} ms)`;
      } else {
        elements.hudLatency.textContent = `延迟: ${cost} ms${serverTag}`;
      }
    }
  } catch (err) {
    elements.hudLatency.textContent = `抓图失败`;
    await updateStatus();
  } finally {
    state.isFetchingFrame = false;
  }
}

// =========================================================================
//  核心逻辑：交互式画布与坐标精确换算
// =========================================================================

function getCanvasCoordinates(event) {
  const img = elements.screenImg;
  const rect = img.getBoundingClientRect();
  if (!rect.width || !rect.height) {
    return null;
  }

  const clientX = event.clientX;
  const clientY = event.clientY;

  if (
    clientX < rect.left ||
    clientX > rect.right ||
    clientY < rect.top ||
    clientY > rect.bottom
  ) {
    return null;
  }

  const scaleX = (img.naturalWidth || state.screenWidth) / rect.width;
  const scaleY = (img.naturalHeight || state.screenHeight) / rect.height;

  const realX = Math.round((clientX - rect.left) * scaleX);
  const realY = Math.round((clientY - rect.top) * scaleY);

  return {
    x: Math.max(0, Math.min(realX, (img.naturalWidth || state.screenWidth) - 1)),
    y: Math.max(0, Math.min(realY, (img.naturalHeight || state.screenHeight) - 1)),
    pixelLeft: clientX - elements.canvasContainer.getBoundingClientRect().left,
    pixelTop: clientY - elements.canvasContainer.getBoundingClientRect().top,
  };
}

function showClickRipple(x, y) {
  const ripple = document.createElement("div");
  ripple.className = "click-ripple";
  ripple.style.left = `${x}px`;
  ripple.style.top = `${y}px`;
  elements.canvasContainer.appendChild(ripple);
  setTimeout(() => ripple.remove(), 500);
}

async function handleCanvasClick(event, overrideMode = null) {
  event.preventDefault();
  if (!state.connected) {
    alert(
      state.target === "android"
        ? "当前未连接 Android 设备，请先在右侧【连接配置 → 配对与连接】中连接！"
        : "当前未连接被控电脑，请先点击【🔗 连接】按钮！"
    );
    return;
  }
  if (state.isLocked) {
    alert("当前处于锁定状态，已阻止发送操作！点击右上角解锁。");
    return;
  }

  const coords = getCanvasCoordinates(event);
  if (!coords) return;

  showClickRipple(coords.pixelLeft, coords.pixelTop);

  try {
    const modeToUse = overrideMode || state.currentClickMode;
    let actionType = "click";
    let button = "left";

    if (modeToUse === "right" || event.button === 2) {
      actionType = state.target === "android" ? "long_press" : "right_click";
      button = "right";
    } else if (modeToUse === "double") {
      actionType = "double_click";
    }

    const payload = {
      action: actionType,
      x: coords.x,
      y: coords.y,
      button: button,
      source: "webui_canvas",
    };
    if (actionType === "long_press") payload.duration_ms = 700;

    await apiPost("action", payload);

    setTimeout(() => fetchFrame("webui_manual"), 200);
  } catch (err) {
    console.error("执行点击失败:", err);
  }
}

// =========================================================================
//  核心逻辑：按键与控制指令
// =========================================================================

async function sendText() {
  const text = elements.inputText.value;
  if (!text) return;
  if (!state.connected) {
    alert("请先点击【🔗 连接】按钮建立连接！");
    return;
  }
  if (state.isLocked) {
    alert("当前已锁定！");
    return;
  }

  try {
    await apiPost("action", {
      action: "type",
      text: text,
      source: "webui_input",
    });
    elements.inputText.value = "";
    setTimeout(() => fetchFrame("webui_manual"), 250);
  } catch (err) {
    alert(`输入失败: ${err.message || err}`);
  }
}

async function sendAndroidText() {
  const text = elements.inputAndroidText.value;
  if (!text) return;
  if (!state.connected) {
    alert("请先连接 Android 设备！");
    return;
  }
  if (state.isLocked) return;

  try {
    const res = await apiPost("action", { action: "type", text: text, source: "webui_input" });
    const err = apiError(res);
    if (err) throw new Error(err);
    const data = unwrap(res) || {};
    elements.inputAndroidText.value = "";
    setTimeout(() => fetchFrame("webui_manual"), 250);
    if (data.method && data.method !== "input_text") {
      console.log(`[Android 文本输入方式] ${data.method}`);
    }
  } catch (err) {
    alert(`输入失败: ${err.message || err}`);
  }
}

async function sendKey(key) {
  if (!state.connected) {
    alert("请先点击【🔗 连接】按钮建立连接！");
    return;
  }
  if (state.isLocked) return;
  try {
    await apiPost("action", {
      action: "press",
      key: key,
      source: "webui_button",
    });
    setTimeout(() => fetchFrame("webui_manual"), 200);
  } catch (err) {
    console.error("按键下发失败:", err);
  }
}

async function sendHotkey(hotkeyStr) {
  if (!state.connected) {
    alert("请先点击【🔗 连接】按钮建立连接！");
    return;
  }
  if (state.isLocked) return;
  const keys = hotkeyStr.split(" ").filter(Boolean);
  try {
    await apiPost("action", {
      action: "hotkey",
      keys: keys,
      source: "webui_button",
    });
    setTimeout(() => fetchFrame("webui_manual"), 300);
  } catch (err) {
    console.error("组合键下发失败:", err);
  }
}

async function sendSwipe(direction) {
  if (!state.connected) {
    alert("请先连接 Android 设备！");
    return;
  }
  if (state.isLocked) return;
  try {
    await apiPost("action", {
      action: "swipe",
      direction: direction,
      amount: 3,
      source: "webui_button",
    });
    setTimeout(() => fetchFrame("webui_manual"), 250);
  } catch (err) {
    console.error("滑动下发失败:", err);
  }
}

async function sendScroll(direction) {
  if (!state.connected) {
    alert("请先点击【🔗 连接】按钮建立连接！");
    return;
  }
  if (state.isLocked) return;
  try {
    await apiPost("action", {
      action: "scroll",
      direction: direction,
      amount: 3,
      source: "webui_scroll",
    });
    setTimeout(() => fetchFrame("webui_manual"), 200);
  } catch (err) {
    console.error("滚轮下发失败:", err);
  }
}

// =========================================================================
//  Android：应用启动 / 停止、应用列表、ADB Shell
// =========================================================================

async function launchApp(packageName) {
  const pkg = (packageName || "").trim();
  if (!pkg) {
    alert("请填写应用包名，例如 com.tencent.mm");
    return;
  }
  if (!state.connected) {
    alert("请先连接 Android 设备！");
    return;
  }
  try {
    const res = await apiPost("action", { action: "launch_app", package: pkg, source: "webui_app" });
    const err = apiError(res);
    if (err) throw new Error(err);
    setTimeout(() => fetchFrame("webui_manual"), 500);
  } catch (err) {
    alert(`启动应用失败: ${err.message || err}`);
  }
}

async function stopApp() {
  const pkg = (elements.inputAppPackage.value || "").trim();
  if (!pkg) {
    alert("请填写要停止的应用包名");
    return;
  }
  try {
    const res = await apiPost("action", { action: "stop_app", package: pkg, source: "webui_app" });
    const err = apiError(res);
    if (err) throw new Error(err);
  } catch (err) {
    alert(`停止应用失败: ${err.message || err}`);
  }
}

async function refreshAndroidApps() {
  if (!state.connected) {
    elements.androidAppList.innerHTML = `<div class="empty-hint">请先连接 Android 设备后再加载应用列表</div>`;
    return;
  }
  elements.btnRefreshApps.disabled = true;
  elements.btnRefreshApps.textContent = "⏳ 加载中...";
  try {
    const res = await apiGet("adb_apps", {});
    const err = apiError(res);
    if (err) throw new Error(err);
    const data = unwrap(res) || {};
    state.androidApps = data.packages || [];
    if (!state.androidApps.length) {
      elements.androidAppList.innerHTML = `<div class="empty-hint">未获取到第三方应用（可尝试在手机端确认 adb 权限）</div>`;
    } else {
      elements.androidAppList.innerHTML = state.androidApps
        .map((p) => `<button class="app-item" data-package="${escapeHtml(p)}" title="${escapeHtml(p)}">${escapeHtml(p)}</button>`)
        .join("");
    }
  } catch (err) {
    elements.androidAppList.innerHTML = `<div class="empty-hint">加载失败: ${escapeHtml(err.message || err)}</div>`;
  } finally {
    elements.btnRefreshApps.disabled = false;
    elements.btnRefreshApps.textContent = "🔄 刷新应用列表";
  }
}

async function runAdbShell() {
  const command = (elements.inputAdbShell.value || "").trim();
  if (!command) {
    alert("请输入要执行的 adb shell 命令");
    return;
  }
  if (!state.connected) {
    alert("请先连接 Android 设备！");
    return;
  }
  elements.btnRunShell.disabled = true;
  elements.btnRunShell.textContent = "⏳";
  elements.adbShellOutput.textContent = `$ adb shell ${command}\n执行中...`;
  try {
    const res = await apiPost("action", { action: "shell", command: command, source: "webui_shell" });
    const err = apiError(res);
    if (err) throw new Error(err);
    const data = unwrap(res) || {};
    const out = (data.stdout || "").trim();
    const errOut = (data.stderr || "").trim();
    elements.adbShellOutput.textContent =
      `$ adb shell ${command}\n返回码: ${data.code}\n${out || errOut || "(无输出)"}`;
    setTimeout(() => fetchFrame("webui_manual"), 300);
  } catch (err) {
    elements.adbShellOutput.textContent = `$ adb shell ${command}\n❌ ${err.message || err}`;
  } finally {
    elements.btnRunShell.disabled = false;
    elements.btnRunShell.textContent = "执行";
  }
}

// =========================================================================
//  Android：配对 / 连接 / 设备列表
// =========================================================================

function renderAndroidInfo() {
  const info = state.android || {};
  if (!elements.adbStatusChip) return;

  if (!info.adb_available) {
    elements.adbStatusChip.className = "status-chip err";
    elements.adbStatusChip.textContent = "ADB 未就绪";
    if (info.adb_error) {
      elements.adbHintBox.textContent = info.adb_error;
    }
  } else {
    const srcLabel =
      info.adb_source === "bundled"
        ? " · 内置"
        : info.adb_source === "config"
          ? " · 自定义"
          : info.adb_source === "path"
            ? " · 系统 PATH"
            : "";
    elements.adbStatusChip.className = "status-chip ok";
    elements.adbStatusChip.textContent = `ADB 就绪${srcLabel}${info.adb_version ? " · " + info.adb_version.replace(/^.*?version\s*/i, "") : ""}`;
    elements.adbHintBox.textContent = info.serial
      ? `当前设备：${info.serial}${info.model ? " · " + [info.manufacturer, info.model].filter(Boolean).join(" ") : ""}`
      : `ADB 已就绪${srcLabel}，等待配对 / 连接设备（支持 127.0.0.1:5555 反向隧道）。`;
  }

  const devices = info.devices || [];
  if (!devices.length) {
    elements.adbDeviceList.innerHTML = `<div class="empty-hint">未检测到设备</div>`;
  } else {
    elements.adbDeviceList.innerHTML = devices
      .map((d) => {
        const stateCls = d.state === "device" ? "ready" : d.state === "unauthorized" ? "unauthorized" : "";
        const label = [d.serial, d.model || d.product || ""].filter(Boolean).join("  ");
        return `<div class="device-item ${stateCls}">
          <span class="device-serial" title="${escapeHtml(label)}">${escapeHtml(d.serial || "")}</span>
          <span class="device-state">${escapeHtml(d.state || "")}</span>
        </div>`;
      })
      .join("");
  }

  renderAdbSelfCheck(info);

  // 后端会自动探测并写回 adb_path：输入框为空时同步显示，避免用户以为没配置
  const autoPath = info.adb_path_config || info.adb_path || "";
  if (autoPath && elements.settingAdbPath && !elements.settingAdbPath.value
      && document.activeElement !== elements.settingAdbPath) {
    elements.settingAdbPath.value = autoPath;
  }
}

const ADB_SOURCE_TEXT = {
  bundled: "插件内置",
  config: "插件设置",
  env: "环境变量 ADB_PATH",
  path: "系统 PATH",
  sdk: "Android SDK",
};

async function runAdbDiag() {
  if (!state.connected) {
    alert("请先连接 Android 设备再运行诊断");
    return;
  }
  const oldText = elements.btnAdbDiag.textContent;
  elements.btnAdbDiag.disabled = true;
  elements.btnAdbDiag.textContent = "⏳ 诊断中（10~40s）...";
  elements.adbDiagOutput.style.display = "block";
  elements.adbDiagOutput.textContent = "正在测量 adb 往返基线、PNG 抓图与设备端 JPEG 耗时…";
  try {
    const res = await apiGet("adb_diag");
    const err = apiError(res);
    if (err) throw new Error(err);
    const d = unwrap(res) || {};
    const png = d.png || {};
    const jpeg = d.jpeg || {};
    const probe = d.stream_probe || {};
    const stream = d.stream || {};
    const kb = (n) => Math.round((n || 0) / 1024);
    elements.adbDiagOutput.textContent = [
      `adb 基线: ${d.baseline_ms} ms  [${(d.baseline_samples || []).join(", ")}]  链路≈${d.link_kbps || 0}kbps`,
      `PNG: ${png.ok ? `${png.ms} ms / ${kb(png.bytes)} KB` : `失败 ${png.error || ""}`}`,
      `设备端 JPEG: ${
        jpeg.ok
          ? `${jpeg.ms} ms / ${kb(jpeg.bytes)} KB (${jpeg.variant || ""} / ${jpeg.app_process || ""})`
          : `未生效 ${jpeg.error || ""}`
      }`,
      `ffmpeg: ${d.ffmpeg || "未安装"}${d.ffmpeg_version ? "  " + d.ffmpeg_version : ""}    screenrecord: ${d.screenrecord || "-"}`,
      `流自检: screenrecord ${probe.screenrecord_bytes ?? "-"}B / ${probe.screenrecord_ms ?? "-"}ms，` +
        `head ${probe.h264_head || "-"}，ffmpeg 解出 ${probe.pipeline_frames ?? "-"} 帧` +
        `${probe.pipeline_error ? "（" + probe.pipeline_error + "）" : ""}`,
      `流状态: running=${stream.running} frames=${stream.frames} 原因=${stream.reason || "-"}`,
      `结论: ${d.advice || ""}`,
    ].join("\n");
  } catch (e) {
    elements.adbDiagOutput.textContent = `诊断失败: ${e.message || e}`;
  } finally {
    elements.btnAdbDiag.disabled = false;
    elements.btnAdbDiag.textContent = oldText;
  }
}

function renderAdbSelfCheck(info) {
  if (!elements.adbSelfCheck) return;
  info = info || {};
  const bundled = info.bundled_adb || {};
  const idle = info.idle_timeout === undefined ? 0 : info.idle_timeout;
  const rows = [
    {
      key: "运行平台",
      val: info.platform || "-",
      cls: "",
    },
    {
      key: "内置 adb",
      val: `${bundled.exists ? "已就绪" : "缺失"} · ${bundled.path || "(未打包)"}`,
      cls: bundled.exists ? "ok" : "err",
    },
    {
      key: "实际使用",
      val: info.adb_path || "(尚未解析)",
      cls: info.adb_path ? "ok" : "err",
    },
    {
      key: "来源",
      val: ADB_SOURCE_TEXT[info.adb_source] || info.adb_source || "-",
      cls: "",
    },
    {
      key: "版本",
      val: (info.adb_version || "-").replace(/^.*?version\s*/i, "") || "-",
      cls: "",
    },
    {
      key: "Android 断连",
      val: idle > 0 ? `${idle} 秒` : "永不断连",
      cls: "",
    },
  ];

  const stream = info.stream || {};
  if (stream.running) {
    rows.push({
      key: "画面模式",
      val: `视频流 ${stream.size || ""} @${Math.round((stream.bitrate || 0) / 1000)}kbps · ${stream.frames || 0} 帧`,
      cls: "ok",
    });
  } else {
    rows.push({
      key: "画面模式",
      val: `截图模式${stream.reason ? "（" + String(stream.reason).slice(0, 120) + "）" : ""}`,
      cls: stream.mode === "off" ? "" : "err",
      title: String(stream.reason || ""),
    });
  }

  const jpeg = info.jpeg || {};
  let jpegVal;
  let jpegCls = "";
  if (jpeg.mode === "off") {
    jpegVal = "已关闭（adb_jpeg_mode=off）";
  } else if (jpeg.dex_ready) {
    jpegVal = `已就绪 · q${jpeg.quality || 70}`;
    jpegCls = "ok";
  } else if (jpeg.reason) {
    jpegVal = `不可用（${String(jpeg.reason).slice(0, 42)}）`;
    jpegCls = "err";
  } else {
    jpegVal = "待首次截图时自动启用";
  }
  rows.push({ key: "设备端压缩", val: jpegVal, cls: jpegCls });
  elements.adbSelfCheck.innerHTML = rows
    .map(
      (r) => `<div class="kv-row ${r.cls}">
        <span class="kv-key">${escapeHtml(r.key)}</span>
        <span class="kv-val" title="${escapeHtml(r.title || r.val)}">${escapeHtml(r.val)}</span>
      </div>`
    )
    .join("");
}

async function refreshAndroidDevices(quiet = false) {
  if (!quiet) {
    elements.btnAdbRefresh.disabled = true;
    elements.btnAdbRefresh.textContent = "⏳ 刷新中...";
  }
  try {
    const res = await apiGet("adb_devices");
    const err = apiError(res);
    if (err) throw new Error(err);
    const data = unwrap(res) || {};
    if (data.android) {
      state.android = Object.assign({}, state.android, data.android);
      renderAndroidInfo();
    }
  } catch (err) {
    if (!quiet) {
      elements.adbDeviceList.innerHTML = `<div class="empty-hint">刷新失败: ${escapeHtml(err.message || err)}</div>`;
    }
  } finally {
    if (!quiet) {
      elements.btnAdbRefresh.disabled = false;
      elements.btnAdbRefresh.textContent = "🔄 刷新设备列表";
    }
  }
}

async function adbPair() {
  const host = (elements.settingAdbPairHost.value || "").trim() || "127.0.0.1";
  const port = parseInt(elements.settingAdbPairPort.value, 10) || 5555;
  const code = (elements.inputPairCode.value || "").trim();
  if (!code) {
    alert("请输入手机弹窗中显示的配对码");
    return;
  }

  elements.btnAdbPair.disabled = true;
  elements.btnAdbPair.textContent = "⏳ 配对中...";
  try {
    const res = await apiPost("adb_pair", { host: host, port: port, code: code });
    const err = apiError(res);
    if (err) throw new Error(err);
    const data = unwrap(res) || {};
    if (data.android) {
      state.android = Object.assign({}, state.android, data.android);
      renderAndroidInfo();
    }
    alert(`${data.ok ? "✅ 配对成功" : "❌ 配对失败"}\n\n${data.message || ""}`);
    if (data.ok) {
      elements.inputPairCode.value = "";
      await refreshAndroidDevices(true);
    }
  } catch (err) {
    alert(`配对请求异常: ${err.message || err}`);
  } finally {
    elements.btnAdbPair.disabled = false;
    elements.btnAdbPair.textContent = "配对";
  }
}

async function adbConnect() {
  const host = (elements.settingAdbHost.value || "").trim() || "127.0.0.1";
  const port = parseInt(elements.settingAdbPort.value, 10) || 5555;

  elements.btnAdbConnect.disabled = true;
  elements.btnAdbConnect.textContent = "⏳ 连接中...";
  try {
    const res = await apiPost("adb_connect", { host: host, port: port });
    const err = apiError(res);
    if (err) throw new Error(err);
    const data = unwrap(res) || {};
    if (data.android) {
      state.android = Object.assign({}, state.android, data.android);
      renderAndroidInfo();
    }
    if (data.status) {
      applyStatusData(data.status);
    } else {
      await updateStatus();
    }
    if (data.ok && state.connected) {
      await fetchFrame("webui_manual");
      alert(`✅ 已连接 ${data.serial || host + ":" + port}`);
    } else {
      alert(`❌ 连接失败\n\n${data.message || "请检查手机无线调试与隧道状态"}`);
    }
  } catch (err) {
    alert(`连接请求异常: ${err.message || err}`);
  } finally {
    elements.btnAdbConnect.disabled = false;
    elements.btnAdbConnect.textContent = "🔗 连接设备";
  }
}

async function adbDisconnect() {
  try {
    const res = await apiPost("adb_disconnect", {});
    const err = apiError(res);
    if (err) throw new Error(err);
    const data = unwrap(res) || {};
    if (data.android) {
      state.android = Object.assign({}, state.android, data.android);
      renderAndroidInfo();
    }
    if (data.status) {
      applyStatusData(data.status);
    } else {
      await updateStatus();
    }
    clearCanvasForDisconnected(`🔌 已断开 Android 设备连接`);
  } catch (err) {
    alert(`断开失败: ${err.message || err}`);
  }
}

function updateLockUI() {
  if (state.isLocked) {
    elements.btnEmergencyLock.classList.add("locked");
    elements.btnEmergencyLock.textContent = "🔒 键鼠已锁定";
  } else {
    elements.btnEmergencyLock.classList.remove("locked");
    elements.btnEmergencyLock.textContent = "🔓 键鼠已解锁";
  }
}

async function toggleEmergencyLock() {
  state.isLocked = !state.isLocked;
  updateLockUI();
  try {
    await apiPost("lock", { locked: state.isLocked });
  } catch (err) {
    console.error("更新锁定状态失败:", err);
  }
}

// =========================================================================
//  核心逻辑：日志拉取与渲染
// =========================================================================

async function fetchLogs() {
  try {
    const res = await apiGet("logs");
    const logs = unwrap(res) || [];
    if (!Array.isArray(logs) || logs.length === 0) {
      elements.logListContainer.innerHTML = `<div style="color: var(--text-muted); font-size: 12px; padding: 10px;">暂无日志记录...</div>`;
      return;
    }

    elements.logListContainer.innerHTML = logs
      .map(
        (log) => `
      <div class="log-item ${log.status === "error" ? "error" : ""}">
        <span class="log-time">${escapeHtml(log.time || "--:--:--")}</span>
        <span class="log-action">[${escapeHtml(log.source || "sys")}] ${escapeHtml(log.action || "")}</span>
        <span class="log-details" title="${escapeHtml(log.details || "")}">${escapeHtml(log.details || "")}</span>
      </div>
    `
      )
      .join("");
  } catch (err) {
    console.error("获取日志失败:", err);
  }
}

// =========================================================================
//  核心逻辑：配置保存
// =========================================================================

async function loadConfig() {
  try {
    const res = await apiGet("config");
    const err = apiError(res);
    if (err) throw new Error(err);
    const cfg = unwrap(res) || {};

    // PC
    elements.settingMode.value = cfg.mode || "local";
    elements.settingAutoConnect.checked = !!cfg.auto_connect;
    elements.settingIdleTimeout.value = cfg.idle_timeout !== undefined ? cfg.idle_timeout : 300;
    elements.settingRemoteHost.value = cfg.remote_host || "127.0.0.1";
    elements.settingRemotePort.value = cfg.remote_port || 8765;
    elements.settingRemoteToken.value = cfg.remote_token || "";
    elements.settingUseSsh.checked = !!cfg.use_ssh_tunnel;
    elements.settingSshHost.value = cfg.ssh_host || "";
    elements.settingSshPort.value = cfg.ssh_port || 22;
    elements.settingSshUser.value = cfg.ssh_username || "";
    elements.settingSshPass.value = cfg.ssh_password || "";
    elements.settingSshKey.value = cfg.ssh_key_path || "";

    // Android / ADB
    elements.settingAdbPath.value = cfg.adb_path || "";
    elements.settingAdbSerial.value = cfg.adb_serial || "";
    elements.settingAdbHost.value = cfg.adb_host || "127.0.0.1";
    elements.settingAdbPort.value = cfg.adb_port || 5555;
    elements.settingAdbPairHost.value = cfg.adb_pair_host || "127.0.0.1";
    elements.settingAdbPairPort.value = cfg.adb_pair_port || 5555;
    elements.settingAdbMaxWidth.value =
      cfg.adb_screenshot_max_width !== undefined ? cfg.adb_screenshot_max_width : 720;
    elements.settingAdbTextMode.value = cfg.adb_text_mode || "auto";
    elements.settingAdbIdleTimeout.value =
      cfg.adb_idle_timeout !== undefined ? cfg.adb_idle_timeout : 0;

    if (cfg.target && cfg.target !== state.target) {
      state.target = cfg.target === "android" ? "android" : "pc";
      applyTargetUI();
    }
  } catch (err) {
    console.error("加载配置失败:", err);
  }
}

async function saveSettings() {
  const idleVal = parseInt(elements.settingIdleTimeout.value, 10);
  const maxWidthVal = parseInt(elements.settingAdbMaxWidth.value, 10);
  const adbIdleVal = parseInt(elements.settingAdbIdleTimeout.value, 10);
  const payload = {
    target: state.target,
    // PC
    mode: elements.settingMode.value,
    auto_connect: elements.settingAutoConnect.checked,
    idle_timeout: Number.isNaN(idleVal) ? 300 : Math.max(0, idleVal),
    remote_host: elements.settingRemoteHost.value.trim(),
    remote_port: parseInt(elements.settingRemotePort.value, 10) || 8765,
    remote_token: elements.settingRemoteToken.value.trim(),
    use_ssh_tunnel: elements.settingUseSsh.checked,
    ssh_host: elements.settingSshHost.value.trim(),
    ssh_port: parseInt(elements.settingSshPort.value, 10) || 22,
    ssh_username: elements.settingSshUser.value.trim(),
    ssh_password: elements.settingSshPass.value,
    ssh_key_path: elements.settingSshKey.value.trim(),
    // Android
    adb_path: elements.settingAdbPath.value.trim(),
    adb_serial: elements.settingAdbSerial.value.trim(),
    adb_host: elements.settingAdbHost.value.trim() || "127.0.0.1",
    adb_port: parseInt(elements.settingAdbPort.value, 10) || 5555,
    adb_pair_host: elements.settingAdbPairHost.value.trim() || "127.0.0.1",
    adb_pair_port: parseInt(elements.settingAdbPairPort.value, 10) || 5555,
    adb_screenshot_max_width: Number.isNaN(maxWidthVal) ? 720 : Math.max(0, maxWidthVal),
    adb_text_mode: elements.settingAdbTextMode.value,
    adb_idle_timeout: Number.isNaN(adbIdleVal) ? 0 : Math.max(0, adbIdleVal),
  };

  try {
    elements.btnSaveSettings.textContent = "⏳ 正在保存...";
    const res = await apiPost("config", payload);
    const err = apiError(res);
    if (err) throw new Error(err);
    alert("✅ 配置保存成功！");
    await updateStatus();
    if (state.target === "android") {
      await refreshAndroidDevices(true);
    }
  } catch (err) {
    alert(`❌ 保存配置失败: ${err.message || err}`);
  } finally {
    elements.btnSaveSettings.textContent = "💾 保存并应用设置";
  }
}

// =========================================================================
//  事件监听绑定与生命周期管理
// =========================================================================

function bindEvents() {
  // 左上角目标切换
  elements.targetOptions.forEach((btn) => {
    btn.addEventListener("click", () => switchTarget(btn.dataset.target));
  });

  // 选项卡切换
  elements.tabBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      elements.tabBtns.forEach((b) => b.classList.remove("active"));
      elements.tabContents.forEach((c) => c.classList.remove("active"));
      btn.classList.add("active");
      const targetTab = document.getElementById(btn.dataset.tab);
      if (targetTab) targetTab.classList.add("active");
      if (btn.dataset.tab === "tab-logs") fetchLogs();
      if (btn.dataset.tab === "tab-settings" && state.target === "android") {
        refreshAndroidDevices(true);
      }
    });
  });

  // 连接 / 断开连接 按钮（顶部与设置页双入口）
  elements.btnConnect.addEventListener("click", connectTarget);
  elements.btnDisconnect.addEventListener("click", disconnectTarget);
  elements.btnTestConnect.addEventListener("click", connectTarget);
  elements.btnSettingsDisconnect.addEventListener("click", disconnectTarget);

  // 画面刷新与定时器
  elements.btnRefresh.addEventListener("click", () => fetchFrame("webui_manual"));
  elements.autoRefreshSelect.addEventListener("change", syncFrameRefreshTimer);
  elements.btnEmergencyLock.addEventListener("click", toggleEmergencyLock);

  // 页面可见性与窗口切出监听：离开或隐藏 WebUI 时立即停止画面刷新
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || document.visibilityState !== "visible") {
      stopFrameRefreshTimer();
    } else {
      updateStatus();
      syncFrameRefreshTimer();
    }
  });

  window.addEventListener("pagehide", stopFrameRefreshTimer);
  window.addEventListener("beforeunload", stopFrameRefreshTimer);

  // IntersectionObserver：当插件视图在父级框架中被隐藏或切走时停止刷新
  if ("IntersectionObserver" in window && elements.screenViewport) {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          state.isViewportVisible = entry.isIntersecting;
        }
        if (!state.isViewportVisible) {
          stopFrameRefreshTimer();
        } else {
          syncFrameRefreshTimer();
        }
      },
      { threshold: 0.05 }
    );
    observer.observe(elements.screenViewport);
  }

  // 鼠标点击模式切换
  elements.btnMouseLeft.addEventListener("click", () => {
    state.currentClickMode = "left";
    elements.btnMouseLeft.className = "btn btn-primary";
    elements.btnMouseRight.className = "btn";
    elements.btnMouseDouble.className = "btn";
  });
  elements.btnMouseRight.addEventListener("click", () => {
    state.currentClickMode = "right";
    elements.btnMouseLeft.className = "btn";
    elements.btnMouseRight.className = "btn btn-primary";
    elements.btnMouseDouble.className = "btn";
  });
  elements.btnMouseDouble.addEventListener("click", () => {
    state.currentClickMode = "double";
    elements.btnMouseLeft.className = "btn";
    elements.btnMouseRight.className = "btn";
    elements.btnMouseDouble.className = "btn btn-primary";
  });

  // 画布鼠标事件
  elements.screenImg.addEventListener("mousemove", (e) => {
    const coords = getCanvasCoordinates(e);
    if (coords) {
      elements.coordsHud.textContent = `坐标: (X: ${coords.x}, Y: ${coords.y})`;
    }
  });
  elements.screenImg.addEventListener("mouseleave", () => {
    elements.coordsHud.textContent = `坐标: (X: --, Y: --)`;
  });
  elements.screenImg.addEventListener("click", (e) => handleCanvasClick(e));
  elements.screenImg.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    handleCanvasClick(e, "right");
  });

  // 文本发送
  elements.btnSendText.addEventListener("click", sendText);
  elements.inputText.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendText();
  });
  elements.btnSendAndroidText.addEventListener("click", sendAndroidText);
  elements.inputAndroidText.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendAndroidText();
  });

  // 单键与组合键
  document.querySelectorAll("[data-key]").forEach((btn) => {
    btn.addEventListener("click", () => sendKey(btn.dataset.key));
  });
  document.querySelectorAll("[data-hotkey]").forEach((btn) => {
    btn.addEventListener("click", () => sendHotkey(btn.dataset.hotkey));
  });

  // Android 手势滑动
  document.querySelectorAll("[data-swipe]").forEach((btn) => {
    btn.addEventListener("click", () => sendSwipe(btn.dataset.swipe));
  });

  // 滚轮
  elements.btnScrollUp.addEventListener("click", () => sendScroll("up"));
  elements.btnScrollDown.addEventListener("click", () => sendScroll("down"));

  // Android 应用与 Shell
  elements.btnLaunchApp.addEventListener("click", () => launchApp(elements.inputAppPackage.value));
  elements.btnStopApp.addEventListener("click", stopApp);
  elements.btnRefreshApps.addEventListener("click", refreshAndroidApps);
  elements.inputAppPackage.addEventListener("keydown", (e) => {
    if (e.key === "Enter") launchApp(elements.inputAppPackage.value);
  });
  elements.androidAppList.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-package]");
    if (btn) launchApp(btn.dataset.package);
  });
  elements.btnRunShell.addEventListener("click", runAdbShell);
  elements.inputAdbShell.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runAdbShell();
  });

  // Android 配对与连接
  elements.btnAdbPair.addEventListener("click", adbPair);
  elements.inputPairCode.addEventListener("keydown", (e) => {
    if (e.key === "Enter") adbPair();
  });
  elements.btnAdbConnect.addEventListener("click", adbConnect);
  elements.btnAdbDisconnect.addEventListener("click", adbDisconnect);
  elements.btnAdbRefresh.addEventListener("click", () => refreshAndroidDevices(false));
  elements.btnAdbDiag.addEventListener("click", runAdbDiag);

  // 设置面板
  elements.btnSaveSettings.addEventListener("click", saveSettings);
  elements.btnRefreshLogs.addEventListener("click", fetchLogs);
}

// =========================================================================
//  初始化入口
// =========================================================================

async function init() {
  if (bridge && typeof bridge.ready === "function") {
    try {
      const ctx = await bridge.ready();
      console.log("[AstrBot Bridge 就绪]", ctx);
    } catch (e) {
      console.warn("Bridge 初始化异常:", e);
    }
  }

  bindEvents();
  applyTargetUI();
  await loadConfig();
  await updateStatus();

  // 仅当后端已处于连接状态（例如用户勾选了 auto_connect 或刚被 Bot 唤醒）时才拉取首帧
  if (state.connected && isWebUIVisible()) {
    await fetchFrame("webui_manual");
  }

  // 每 5 秒在页面可见时同步一次连接状态与空闲倒计时（不会主动发起远程连接）
  state.statusPollTimer = setInterval(() => {
    if (isWebUIVisible()) {
      updateStatus();
    }
  }, 5000);
}

window.addEventListener("DOMContentLoaded", init);
