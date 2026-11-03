# 🐟 AstrbotFishEyes

[](https://github.com/Soulter/AstrBot)
[]()
[License](https://www.google.com/search?q=LICENSE)

**AstrbotFishEyes** 是专为 [AstrBot](https://github.com/Soulter/AstrBot) 打造的电脑屏幕查看与远程自动化控制（Computer Use）插件。支持本地与远程双模式、正/反向 SSH 隧道内网穿透、像素坐标网格视觉定位、二级 WebUI 实时交互控制台以及独立的单文件被控端桌面程序。

---

## ✨ 核心特性

- **🖥️ 本地 / 远程双模式与内网穿透**：支持直接控制 AstrBot 所在主机（`local`），或通过独立被控端 `Client` 控制远程电脑（`remote`）；内置正/反向 SSH 隧道，被控机处于家庭宽带/校园网无公网 IP 也能直接穿透。
- **🎯 视觉网格识图与精准点击**：内置像素坐标标尺与网格叠加技术（`enable_coord_grid`），自动联动 AstrBot 默认图片转述模型（`default_image_caption_provider_id`）或自定义多模态模型识别屏幕内容、提取按钮像素坐标 `(x, y)` 并一步点击。
- **🌐 二级 WebUI 交互控制台**：嵌入 AstrBot 管理面板，支持显式【🔗 连接】/【🔌 断开连接】、画面实时点击/双击/右键、中文输入、快捷键面板与紧急锁定；切出页面或未连接时自动停止刷新。
- **⚡ 按需唤醒与空闲自动断连**：默认启动不自动连接；超过设定时间（`idle_timeout`）无操作自动断开连接并回收内存；未连接时若 Bot 需要操作电脑，会自动尝试连接一次。
- **🛡️ 独立被控端桌面应用（Client）**：支持一键打包为免 Python 环境的单文件 `AstrBotScreenAgent.exe`，支持系统托盘后台常驻、开机自启与 Windows 防休眠/防 EcoQoS 降频保护。

---

## 🚀 快速开始

### 1. 主控端安装（AstrBot 侧）

将插件放入 AstrBot 的 `data/plugins/` 目录并安装依赖：

```bash
cd AstrBot/data/plugins
git clone https://github.com/your-repo/AstrbotFishEyes.git
pip install -r AstrbotFishEyes/requirements.txt
```

> 若 AstrBot 部署在无桌面环境的 Linux / Docker 中，仅需使用 `remote`（远程模式），无需安装本地 GUI 环境。

---

### 2. 被控端操作指南（被操控的电脑侧）

> 💡 **提示**：若 AstrBot 直接运行在被控电脑本机，将插件设为 `local` 模式即可使用，**无需运行被控端程序**。仅当跨设备或通过云服务器控制时，需在被控电脑运行 `Client/`。

#### 第一步：启动被控端程序（二选一）

- **方式 A：打包为单文件 `.exe` 运行（推荐 Windows）**
  在开发机进入 `Client/` 目录，双击运行 `build_exe.bat`（或执行 `python build_exe.py`），将生成的 `Client/dist/AstrBotScreenAgent.exe` 复制到被控电脑双击打开即可，无需安装 Python。
- **方式 B：Python 源码直接运行**
  ```bash
  cd AstrbotFishEyes/Client
  pip install -r requirements.txt
  python app.py
  ```

#### 第二步：配置并开启被控端服务

1. **【⚙️ 服务配置】**：确认监听端口（默认 `8765`）、监听地址（局域网直连选 `0.0.0.0`，仅隧道访问可选 `127.0.0.1`），并设置**访问安全 Token**（可按需勾选“打开自动启动服务”与“开机自启”）。
2. **【🔒 反向 SSH 隧道】（内网无公网 IP 必选）**：当 AstrBot 在云服务器而被控机在家庭/校园内网时，勾选 **启用反向 SSH 隧道连接**，填入云服务器的 SSH 地址、端口（22）、用户名、密码/私钥及转发端口（默认 `8765`）。
3. **启动与后台常驻**：点击左下角 **【💾 保存所有配置】** → 点击右上角 **【▶ 启动服务】**（状态变为绿色 `● 服务运行中`）。随后点击右上角最小化/关闭或底部 **【📥 隐藏到托盘后台】**，程序即在右下角托盘静默常驻，双击托盘图标可随时唤起。

---

### 3. 主控端连接配置速查（AstrBot 插件设置）

根据你的网络环境，在 AstrBot 插件配置或二级 WebUI【⚙️ 连接配置】中对应填写：

| 部署场景 | `mode` | `remote_host` | `remote_port` | `use_ssh_tunnel` | 备注 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **同机自控**（AstrBot 与桌面同机） | `local` | - | - | `false` | 无需运行 `Client` |
| **局域网 / Tailscale 直连** | `remote` | 被控机局域网 IP | `8765` | `false` | 填入被控端一致的 `remote_token` |
| **云服务器 + 被控端反向隧道（推荐）** | `remote` | `127.0.0.1` | `8765` | `false` | 被控端已将端口映射至云服务器本地 |
| **云服务器主动发起正向 SSH 隧道** | `remote` | `127.0.0.1` | `8765` | `true` | 在插件中填写被控机 SSH 登录信息 |

---

### 4. 视觉识别与节能设置

- **视觉识图模型**：插件会自动按以下优先级调用多模态模型识别屏幕与点击坐标：
  1. 插件设置中的 `vision_provider_id`（可在下拉框直接选择模型提供商）；
  2. AstrBot 系统设置中的 **默认图片转述模型**（`default_image_caption_provider_id`）；
  3. 当前会话的主对话模型。
- **坐标网格定位（`enable_coord_grid`）**：默认开启。向视觉模型发送分析图时自动叠加像素坐标标尺与网格，确保纯文本或视觉模型都能获取精确的 `(x, y)` 像素坐标。
- **按需连接与空闲断连（`idle_timeout`）**：默认 `auto_connect = false`，超过 `idle_timeout`（默认 `300` 秒）无操作自动断开连接释放内存；未连接时若 Bot 需操作电脑会自动尝试连接一次。

---

## 🕹️ 使用指南

### 1. 二级 WebUI 可视化控制台
在 AstrBot 管理面板 → **【插件】** → 进入 `AstrbotFishEyes` 详情页 → 打开 **【屏幕控制台 (View)】**：
- **连接控制**：点击顶部 **【🔗 连接】** 开启画面，使用完毕可点击 **【🔌 断开连接】**（离开页面或切出标签页会自动暂停刷新）。
- **画布直控**：在左侧屏幕画面上直接点击（支持左键、右键、双击模式切换），自动按真实分辨率换算坐标下发。
- **快捷面板**：右侧支持发送中英文文本、常用按键与组合键（`Win+D`、`Alt+Tab`、`Ctrl+C/V`、任务管理器等）、滚轮滚动及 **【🔒 紧急锁定】**。

### 2. 聊天指令（`/screen`）
支持指令前缀：`/screen`、`/cu`、`/屏幕`、`/电脑控制`

| 指令 | 示例 | 说明 |
| :--- | :--- | :--- |
| `/screen 连接` / `断开` | `/screen 连接` | 手动建立连接 / 断开连接并释放内存 |
| `/screen 状态` | `/screen 状态` | 查看连接状态、分辨率及无操作自动断连剩余时间 |
| `/screen 截图 [目标]` | `/screen 截图 搜索框` | 截取屏幕并发送图片（带目标参数时同步返回视觉坐标分析） |
| `/screen 识别点击 <目标>` | `/screen 识别点击 登录按钮` | 视觉识别画面上的目标元素位置并自动点击 |
| `/screen 点击/双击/右键 x y` | `/screen 点击 640 420` | 在指定像素坐标执行单击、双击或右键 |
| `/screen 输入 <文本>` | `/screen 输入 你好世界` | 在当前焦点窗口输入中英文文本 |
| `/screen 按键 <key>` | `/screen 按键 enter` | 按下单键（`enter`、`esc`、`tab`、`space`、`win`、`f5` 等） |
| `/screen 组合键 <keys>` | `/screen 组合键 win d` | 执行组合键（如 `ctrl c`、`alt tab`、`win d`） |
| `/screen 滚动 <up/down> [n]` | `/screen 滚动 down 5` | 上下滚动鼠标滚轮 |
| `/screen 模式 [local/remote]` | `/screen 模式 remote` | 查看或切换控制模式 |

### 3. LLM Agent 工具集（Computer Use）
启用插件后，AI 可在对话中自主调用以下 12 个工具完成桌面操作：

| 工具名 | 参数 | 作用 |
| :--- | :--- | :--- |
| `screen_screenshot` | `target=""`, `quality=75` | 截取屏幕并通过视觉/转述模型返回画面描述与元素坐标 `(x, y)` |
| `screen_click_element` | `target`, `action="click"` | 视觉识别指定目标元素（如 `"搜索按钮"`）并一步完成单击/双击/右键 |
| `screen_click` | `x`, `y`, `button="left"` | 点击指定像素坐标 |
| `screen_double_click` | `x`, `y` | 双击指定像素坐标 |
| `screen_right_click` | `x`, `y` | 右键点击指定像素坐标 |
| `screen_type` | `text` | 输入中英文文本 |
| `screen_press` | `key` | 敲击单个键盘按键 |
| `screen_hotkey` | `keys` | 按下快捷键组合（如 `'ctrl c'`） |
| `screen_scroll` | `direction`, `amount=3` | 滚动鼠标滚轮 |
| `screen_move` / `screen_drag` | `x, y` / `x1, y1, x2, y2` | 移动鼠标光标 / 按住拖拽鼠标 |
| `screen_status` | 无 | 获取当前屏幕分辨率、环境与连接状态 |

---

## 📁 目录结构

```text
AstrbotFishEyes/
├── main.py                  # 插件主入口：WebUI 路由、视觉网格识图、LLM 工具与聊天指令
├── screen_controller.py     # 统一屏幕控制器（Local/Remote 抽象、按需连接与空闲自动断连）
├── ssh_tunnel.py            # 异步正向 SSH 端口转发隧道
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # AstrBot 插件配置 Schema
├── requirements.txt         # 主控端依赖
├── views/screen_control/    # 二级 WebUI 可视化控制台前端 (HTML/JS/CSS)
└── Client/                  # 独立被控端桌面程序
    ├── app.py               # 被控端 GUI、系统托盘与 Windows 防休眠/防降频管理
    ├── agent_server.py      # 多线程 HTTP 控制服务端
    ├── reverse_tunnel.py    # 反向 SSH 隧道模块（免公网 IP 穿透内网）
    ├── config.py            # 配置持久化与开机自启
    └── build_exe.bat        # 一键打包单文件 AstrBotScreenAgent.exe 脚本
```

---

## 🛡️ 安全说明

1. **权限控制**：默认仅 AstrBot 管理员与 `whitelist` 白名单用户可调用指令与 LLM 工具。
2. **緊急锁定**：WebUI 顶部提供一键 **【🔒 键鼠锁定】** 开关，开启后立即阻断一切键鼠下发。
3. **物理鼠标紧急中断（FailSafe）**：在被控电脑上将物理鼠标快速甩至屏幕最左上角 `(0, 0)` 即可立即中断模拟操作。
