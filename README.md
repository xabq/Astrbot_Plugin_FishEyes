# 🐟鱼人之眼(AstrbotFishEyes)

[![AstrBot Version](https://img.shields.io/badge/AstrBot-%3E%3D3.5.0-blue.svg)](https://github.com/Soulter/AstrBot)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-1.1.12-brightgreen.svg)](CHANGELOG.md)

**AstrbotFishEyes** 是专为 [AstrBot](https://github.com/Soulter/AstrBot) 打造的**电脑 + Android 双端屏幕查看与远程自动化控制（Computer Use）插件**。支持本地与远程双模式、正/反向 SSH 隧道内网穿透、**USB 直连或无线调试的 ADB 控制**、像素坐标网格视觉定位、二级 WebUI 实时交互控制台，以及独立的单文件被控端桌面程序。

---

## ✨ 核心特性

- **🖥️🤖 PC / Android 双控制目标**：WebUI 左上角一键切换。PC 走本机 GUI 或远程 `Client`；Android 通过 `adb` 控制手机/平板，切换时自动断开旧会话。
- **🔌 三种 Android 接入方式**：**USB 直连**（最省事、零配置）、**无线调试配对**（`adb pair` + `adb connect`）、**SSH 反向隧道**（手机在家庭/校园网无公网 IP 时穿透）。
- **📦 内置 adb，免安装**：随包分发官方 platform-tools（Windows `adb.exe` / Linux `adb`），启动时**按系统自动识别并把真实路径写回配置**；Linux 解包丢失的可执行位会自动补 `chmod 755`。
- **🚀 三级画面链路，弱网也能用**：`H.264 视频流`（0.1~0.3s）→ `设备端 JPEG`（手机侧压缩，~1s）→ `PNG 截图`（兜底），逐级自动回退。
- **🎯 视觉网格识图与精准点击**：内置像素坐标标尺与网格叠加（`enable_coord_grid`），联动 AstrBot 默认图片转述模型或自定义多模态模型识别屏幕内容、提取按钮坐标 `(x, y)` 并一步点击。
- **🌐 二级 WebUI 交互控制台**：嵌入 AstrBot 管理面板，支持【🔗 连接】/【🔌 断开连接】、画面实时点击/双击/右键（Android 下映射为点按/长按）、中文输入、手势与快捷键面板、紧急锁定；切出页面或未连接时自动停止刷新。
- **🩺 链路自检与诊断**：一键量出 adb 固定往返、PNG/JPEG 耗时与字节数、ffmpeg/screenrecord 可用性、视频流失败原因，直接告诉你是带宽、RTT 还是手机侧编码的问题。
- **⚡ 按需唤醒与分端断连策略**：PC 与 Android 的「无操作自动断连」**各自独立**——PC 默认 300 秒，**Android 默认 0（永不断连）**；未连接时若 Bot 需要操作，会自动尝试连接一次。
- **🛡️ 独立被控端桌面应用（Client）**：支持一键打包为免 Python 环境的单文件 `AstrBotScreenAgent.exe`，支持系统托盘后台常驻、开机自启与 Windows 防休眠/防 EcoQoS 降频保护。

---

## 🚀 快速开始

### 1. 主控端安装（AstrBot 侧）

**方式 A（推荐）**：面板 →【插件】→ 安装插件 → 上传 `AstrbotFishEyes-v<版本>.zip`（`metadata.yaml` 位于压缩包根目录）。

**方式 B**：把 `AstrbotFishEyes/` 整个文件夹放进 AstrBot 的 `data/plugins/`：

```bash
cd AstrBot/data/plugins
# 复制 AstrbotFishEyes 文件夹进来
pip install -r AstrbotFishEyes/requirements.txt
```

> 若 AstrBot 部署在无桌面环境的 Linux / Docker 中，PC 侧仅需使用 `remote`（远程模式），无需本地 GUI 环境；Android 侧不受影响。

依赖说明：`aiohttp`、`asyncssh`、`pillow`、`pyautogui`、`pyperclip`、`imageio-ffmpeg`（提供静态 ffmpeg，用于视频流；系统已装 ffmpeg 时可删）。

---

### 2. PC 被控端操作指南（被操控的电脑侧）

> 💡 **提示**：若 AstrBot 直接运行在被控电脑本机，将插件设为 `local` 模式即可，**无需运行被控端程序**。仅当跨设备或通过云服务器控制时，需在被控电脑运行 `Client/`。

#### 第一步：启动被控端程序（二选一）

- **方式 A：打包为单文件 `.exe` 运行（推荐 Windows）**
  在开发机进入 `Client/` 目录，双击 `build_exe.bat`（或 `python build_exe.py`），把生成的 `Client/dist/AstrBotScreenAgent.exe` 复制到被控电脑双击运行，无需 Python。
- **方式 B：Python 源码直接运行**

  ```bash
  cd AstrbotFishEyes/Client
  pip install -r requirements.txt
  python app.py
  ```

#### 第二步：配置并开启被控端服务

1. **【⚙️ 服务配置】**：确认监听端口（默认 `8765`）、监听地址（局域网直连选 `0.0.0.0`，仅隧道访问可选 `127.0.0.1`），设置**访问安全 Token**。
2. **【🔒 反向 SSH 隧道】**：AstrBot 在云服务器、被控机在家庭/校园内网时勾选，填服务器 SSH 地址、端口（22）、用户名、密码/私钥与转发端口（默认 `8765`）。
3. **启动与常驻**：点击 **【💾 保存所有配置】** → **【▶ 启动服务】**，再点最小化/关闭收进托盘。

---

### 3. Android 被控端（ADB）

手机端**不需要装任何被控 App**（隧道方式除外），插件通过 `adb` 操作手机。三种接入方式任选其一：

#### 方式 A：USB 直连（推荐，零配置、延迟最低）

1. 手机：设置 → 关于手机 → 连点「版本号」7 次开启开发者选项；
2. 手机：开发者选项 → 打开 **「USB 调试」**（部分机型还需打开「USB 安装」）；
3. 用数据线把手机连到**运行 AstrBot 的那台机器**（Docker 部署需把 USB 设备透传进容器，`--device /dev/bus/usb`）；
4. 手机上弹出「允许 USB 调试吗？」→ 勾选「一律允许」→ 确定；
5. 插件侧无需填任何东西：`adb devices` 里出现 `device` 状态即会被自动选中。

> USB 模式**不需要配对码、不需要隧道**，也不受 Wi-Fi/SSH 影响，是延迟最低的一条路。
> 若同时插着 USB 又连着无线，可在设置项 `adb_serial` 里显式指定要用的序列号。

#### 方式 B：无线调试（同一局域网 / 直连可达）

1. 手机：开发者选项 → **无线调试** → 打开；
2. 点「**使用配对码配对设备**」，记下**配对端口**与 6 位配对码；
3. WebUI【连接配置 → 配对与连接】：填配对地址/端口 + 配对码 → 【配对】；
4. 回到「无线调试」主页，记下**连接端口** → 填连接地址/端口 → 【连接设备】。

> 配对端口 ≠ 连接端口，别混用；配对是持久的（密钥在手机 `/data/misc/adb`），重启后只需重新连接。

#### 方式 C：SSH 反向隧道（手机在内网、无公网 IP）

1. 安装插件包内 `android/SshAdbTunnel/SshAdbTunnel-universal.apk`（版本与插件一致）；
2. App 内填服务器 SSH 地址/端口/用户名/凭据，监听模式选「连接端口」，服务器端口填 `5555`，启动隧道；
3. WebUI【配对与连接】里地址填 `127.0.0.1`、端口填 `5555`，配对/连接即可。

> App 会把手机的 adbd 端口映射到服务器的 `127.0.0.1:5555`，因此服务器侧看起来就是"本地设备"。

---

### 4. 主控端连接配置速查（AstrBot 插件设置 / WebUI【⚙️ 连接配置】）

| 部署场景 | `target` | 关键配置 | 备注 |
| :--- | :--- | :--- | :--- |
| **PC 同机自控** | `pc` | `mode=local` | 无需运行 `Client` |
| **PC 局域网 / Tailscale 直连** | `pc` | `mode=remote`、`remote_host`=被控机 IP、`remote_port=8765` | 填一致的 `remote_token` |
| **PC 云服务器 + 被控端反向隧道** | `pc` | `mode=remote`、`remote_host=127.0.0.1` | 被控端已把端口映射到服务器本地 |
| **PC 云服务器主动正向 SSH** | `pc` | `mode=remote`、`use_ssh_tunnel=true` + SSH 凭据 | 插件主动建隧道 |
| **Android USB 直连** | `android` | `adb_serial` 留空 | 自动选中已就绪的 USB 设备 |
| **Android 无线调试** | `android` | `adb_host`=手机 IP、`adb_port`=连接端口 | 首次需 `adb pair` |
| **Android SSH 反向隧道** | `android` | `adb_host=127.0.0.1`、`adb_port=5555` | 配合包内 SshAdbTunnel App |

---

### 5. 视觉识别与节能设置

- **视觉识图模型**（按优先级）：
  1. 插件设置 `vision_provider_id`；
  2. AstrBot 系统设置中的**默认图片转述模型**（`default_image_caption_provider_id`）；
  3. 当前会话的主对话模型。
- **坐标网格定位（`enable_coord_grid`）**：默认开启，分析图上叠加像素标尺与网格，纯文本模型也能读出 `(x, y)`。
- **分端空闲断连**：
  - PC：`idle_timeout`（默认 `300` 秒）；
  - Android：`adb_idle_timeout`（默认 **`0` = 永不断连**，手机挂机场景推荐保持 0）。
- **按需连接**：`auto_connect = false` 时启动不自动连；Bot 需要操作而当前未连接时，会自动尝试连接一次。

---

## 🕹️ 使用指南

### 1. 二级 WebUI 可视化控制台

AstrBot 管理面板 →【插件】→ `AstrbotFishEyes` 详情页 → 打开 **【屏幕控制台 (View)】**：

- **左上角 PC / Android 切换**：切换后标题、模式徽标、画布提示与控制面板随之变化（Android 下「右键模式」变为「长按模式」）。
- **连接控制**：顶部【🔗 连接】/【🔌 断开连接】；离开页面或切出标签页自动暂停刷新。
- **画布直控**：左侧画面直接点击，自动按真实分辨率换算坐标；Android 下点击=点按、长按模式=长按、双击=双击。
- **快捷面板**：PC 侧为文本/单键/组合键/滚轮；Android 侧为文本、四向手势、导航与音量键、应用启动/停止、ADB Shell 快捷执行。
- **连接配置 → 配对与连接**：ADB 路径、设备序列号、配对（地址/端口/配对码）、连接/断开、设备列表。
- **连接配置 → 🩺 环境自检**：运行平台、内置 adb、实际使用路径、来源、版本、Android 断连策略、画面模式、设备端压缩；一键【运行链路诊断】。
- **紧急锁定**：一键阻断所有键鼠/触控下发。

### 2. 聊天指令

**PC（前缀 `/screen`、`/cu`、`/屏幕`、`/电脑控制`）**

| 指令 | 示例 | 说明 |
| :--- | :--- | :--- |
| `/screen 连接` / `断开` | `/screen 连接` | 手动连接 / 断开并释放内存 |
| `/screen 状态` | `/screen 状态` | 目标、分辨率、连接状态、空闲倒计时 |
| `/screen 截图 [目标]` | `/screen 截图 搜索框` | 截图并发送（带目标时返回坐标分析） |
| `/screen 识别点击 <目标>` | `/screen 识别点击 登录按钮` | 视觉定位并点击 |
| `/screen 点击/双击/右键 x y` | `/screen 点击 640 420` | 指定坐标操作（Android 下右键=长按） |
| `/screen 输入 <文本>` | `/screen 输入 你好世界` | 输入中英文 |
| `/screen 按键 <key>` | `/screen 按键 enter` | 单键 |
| `/screen 组合键 <keys>` | `/screen 组合键 win d` | 组合键 |
| `/screen 滚动 <up/down> [n]` | `/screen 滚动 down 5` | 滚轮 |
| `/screen 切换 pc\|android` | `/screen 切换 android` | 切换控制目标 |
| `/screen 模式 [local/remote]` | `/screen 模式 remote` | 查看/切换 PC 模式 |

**Android（前缀 `/adb`、`/安卓`、`/手机`）**

| 指令 | 说明 |
| :--- | :--- |
| `/adb 状态` / `/adb 诊断` | 环境状态 / 链路诊断（基线、PNG、JPEG、流自检） |
| `/adb 设备` | 列出 adb 设备（USB 与无线都会列出） |
| `/adb 配对 <端口> <配对码>` | 无线调试配对 |
| `/adb 连接 [host:port]` | 连接设备（USB 直连可省略地址） |
| `/adb 断开 [serial]` | 断开设备 |
| `/adb 截图 [目标]` / `/adb 识别点击 <目标>` | 截图 / 视觉定位点击 |
| `/adb 点击 x y` / `/adb 长按 x y [ms]` | 点按 / 长按 |
| `/adb 滑动 x1 y1 x2 y2 [ms]` 或 `/adb 滑动 up\|down\|left\|right` | 滑动 |
| `/adb 输入 <文本>` / `/adb 按键 <key>` / `/adb 组合键 k1 k2` | 文本与按键 |
| `/adb 应用 <包名>` / `/adb 停止 <包名>` / `/adb 应用列表 [关键字]` | 应用管理 |
| `/adb 命令 <shell>` / `/adb 安装 <apk 路径>` | 任意 adb shell / 安装 APK |

### 3. LLM Agent 工具集（Computer Use）

**PC（12 个）**：`screen_screenshot`、`screen_click_element`、`screen_click`、`screen_double_click`、`screen_right_click`、`screen_type`、`screen_press`、`screen_hotkey`、`screen_scroll`、`screen_move`、`screen_drag`、`screen_status`。

**Android（13 个）**：`android_screenshot`、`android_click_element`、`android_tap`、`android_long_press`、`android_swipe`、`android_type`、`android_key`、`android_open_app`、`android_stop_app`、`android_shell`、`android_pair`、`android_connect`、`android_status`。

> 工具作用于**当前控制目标**；`android_*` 会先把目标切到 Android 再执行。

---

## ⚡ 画面性能与低延迟

Android 侧按「能快则快」逐级回退：

| 模式 | 每帧流量 | 单帧延迟 | 依赖 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| **H.264 视频流** | 平稳 ~2 Mbps | **0.1~0.3 s** | ffmpeg | `screenrecord` → ffmpeg → MJPEG 缓存帧，WebUI 取缓存 |
| **设备端 JPEG** | 80~200 KB | ~1 s | 无 | 手机侧 `app_process` 跑小 dex 完成缩放+压缩 |
| **PNG 截图** | 0.5~4.5 MB | 1.5~12 s | 无 | 兜底，弱网下很慢 |

相关配置：

| 配置 | 默认 | 说明 |
| :--- | :--- | :--- |
| `adb_stream_mode` | `auto` | `auto/on/off`，`off` 表示永远用截图 |
| `adb_stream_size` | `auto` | `auto` = 设备分辨率一半（1080x2400 → 540x1200） |
| `adb_stream_bitrate` | `2000000` | 3 Mbps 链路建议 `1500000` |
| `adb_stream_max_fps` | `10` | 8~15 足够 |
| `adb_stream_quality` | `6` | ffmpeg `-q:v`，数字越小越清晰 |
| `adb_ffmpeg_path` | 空 | 自动查找 PATH（apt 的 `/usr/bin/ffmpeg` 优先）或 `imageio-ffmpeg` |
| `adb_jpeg_mode` | `auto` | 设备端 JPEG 开关 |
| `adb_jpeg_quality` | `70` | 设备端 JPEG 质量 |
| `adb_screenshot_max_width` | `720` | 输出宽度（0 = 原图） |

> 视频流需要服务器有 `ffmpeg`：`apt install ffmpeg` 或 `pip install imageio-ffmpeg`。
> 没有 ffmpeg 时会自动回退到设备端 JPEG / PNG，功能不受影响。

---

## 📁 目录结构

```text
AstrbotFishEyes/
├── main.py                  # 插件入口：WebUI 路由、视觉网格识图、LLM 工具与聊天指令
├── device_controller.py     # PC / Android 双目标门面（切换、状态聚合、共享日志与锁定）
├── screen_controller.py     # PC 统一控制器（Local/Remote、按需连接、空闲自动断连）
├── android_controller.py    # Android ADB 控制器（配对/连接/截图/触控/文本/应用/Shell/诊断）
├── android_stream.py        # 低延迟画面流（screenrecord H.264 → ffmpeg → MJPEG 缓存）
├── ssh_tunnel.py            # 异步正向 SSH 端口转发隧道
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # AstrBot 插件配置 Schema
├── requirements.txt         # 主控端依赖
├── views/screen_control/    # 二级 WebUI 控制台前端 (HTML/JS/CSS)
├── platform-tools/          # 内置 adb（windows/adb.exe、linux/adb、NOTICE.txt）
├── tools/android/           # 设备端 JPEG 编码器（JpegEncoder.java + fisheyes_jpeg.dex）
├── Client/                  # 独立被控端桌面程序（PC 远程模式）
│   ├── app.py               # GUI、系统托盘与防休眠/防降频
│   ├── agent_server.py      # 多线程 HTTP 控制服务端
│   ├── reverse_tunnel.py    # 反向 SSH 隧道（免公网 IP）
│   ├── config.py            # 配置持久化与开机自启
│   └── build_exe.bat        # 一键打包单文件 AstrBotScreenAgent.exe
└── android/SshAdbTunnel/    # Android 端 SSH 反向隧道 App 源码 + 构建脚本 + APK
```

---

## 🔧 常见问题

**Q：WebUI 显示「ADB 未就绪」？**
先看【环境自检 → 内置 adb】是否「已就绪」。若缺失，说明插件包不完整（请用官方 zip 重装）；Linux 上若权限异常，插件会自动 `chmod 755` 重试一次。

**Q：USB 插上了但没识别？**
`adb devices` 应出现 `device` 状态；若为 `unauthorized`，在手机上点「允许 USB 调试」；Docker 部署需透传 USB 设备；部分机型需关闭「仅充电」模式。

**Q：画面很卡 / 延迟高？**
先点【🩺 运行链路诊断】：
- 基线 > 1.2 s → 隧道往返慢（换 USB 直连或缩短链路）；
- PNG 传输占大头 → 装 ffmpeg 启用视频流；
- 设备端 JPEG 比 PNG 还慢 → 手机侧编码开销大，用视频流或调小 `adb_screenshot_max_width`。

**Q：视频流起不来？**
自检会给出逐组参数的结果（V1/V5/V3/V4/V2）。常见原因是 ffmpeg 版本过老或 `screenrecord` 被 ROM 限制；插件会自动回退，不影响使用。

**Q：SSH 隧道 App 首次连接后掉线？**
1.1.11 起已修复：mDNS 发现会在 20 秒无结果时自动重新注册，`resolveService` 遇 `FAILURE_ALREADY_ACTIVE` 会退避重试，不再需要重启 App。

---

## 🛡️ 安全说明

1. **权限控制**：默认仅 AstrBot 管理员与 `whitelist` 白名单用户可调用指令与 LLM 工具。
2. **紧急锁定**：WebUI 顶部一键【🔒 键鼠锁定】，开启后立即阻断一切键鼠/触控下发（PC 与 Android 同时生效）。
3. **物理鼠标紧急中断（FailSafe）**：PC 侧把物理鼠标快速甩至屏幕左上角 `(0, 0)` 即可中断模拟操作。
4. **Android 侧不修改系统**：所有操作都走 `adb` 公开接口（`input`/`screencap`/`screenrecord`/`pm`/`am`），不做提权或系统改动。

---

## 📌 版本规则

- 首次修改版本号为 **1.1.0**，之后每次改动在末尾累加 **0.0.1**（`1.1.10 → 1.1.11 → 1.1.12 …`）；
- 插件、`metadata.yaml`、APK（`versionName`/`versionCode`）**保持同一版本号**；
- 历史记录见 [CHANGELOG.md](CHANGELOG.md)。
