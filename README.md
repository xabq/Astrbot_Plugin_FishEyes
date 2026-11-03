# AstrbotFishEyes - 电脑屏幕与远程控制插件 (Screen Control & Computer Use)

[![AstrBot Version](https://img.shields.io/badge/AstrBot-%3E%3D3.5.0-blue.svg)](https://github.com/Soulter/AstrBot)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**AstrbotFishEyes** 是一个让 AstrBot 具备**查看与操控电脑屏幕**能力的插件！
不仅支持**本地模式**与**远程模式（支持正/反向 SSH 隧道穿透）**，还完整集成了 **AstrBot 二级 WebUI 可视化控制台** 和 **LLM Agent 工具集 (Computer Use)**。

---

## 🌟 核心特性

- 🖥️ **本地 / 远程 双模式自由切换**
  - **本地模式 (Local)**：当 AstrBot 直接部署在桌面电脑上（Windows / macOS / Linux GUI）时，开箱即用，免配置直接捕获屏幕与模拟键鼠。
  - **远程模式 (Remote)**：当 AstrBot 部署在云服务器或 Docker 时，通过独立被控端桌面程序 `Client` 控制被控电脑；支持**局域网直连**、**正向 SSH 端口转发隧道**或**被控端反向 SSH 隧道**（穿透复杂局域网与内网环境）。
- 🌐 **集成 AstrBot 二级 WebUI (Plugin View 可视化控制台)**
  - 免额外开放端口，直接嵌入 AstrBot 管理面板，自动复用 JWT 鉴权。
  - **交互式屏幕画布**：实时刷新电脑画面，**直接在网页画面上点击、双击、右键，自动按真实分辨率换算坐标并下发操作**！
  - **虚拟控制面板**：快速发送文本（支持中文）、常用单键（Enter, Esc, Tab, Space, Win等）、常用组合快捷键（Win+D, Alt+Tab, Ctrl+C/V, 任务管理器等）、鼠标滚轮滚动。
  - **实时设备监视**：查看屏幕分辨率、延迟、在线状态，支持一键切换模式与重连测试。
  - **🔒 紧急锁定**：一键锁定键鼠下发，防止误触。
- 🤖 **LLM Agent 工具集 (Computer Use)**
  - 提供 11 个标准化 LLM Tools：`screen_screenshot`、`screen_click`、`screen_double_click`、`screen_right_click`、`screen_type`、`screen_press`、`screen_hotkey`、`screen_scroll`、`screen_move`、`screen_drag`、`screen_status`。
  - 配合支持视觉（Vision）的大模型，实现全自动自主决策操控电脑。
- 💬 **聊天指令交互**
  - 支持 `/screen 截图`、`/screen 点击 x y`、`/screen 输入 <文本>` 等直观快捷指令。
- 🛡️ **严格的安全与权限控制**
  - 管理员默认拥有全部权限，支持用户白名单控制。
  - PyAutoGUI 内置 FailSafe 保护（被控电脑将鼠标光标甩至屏幕最左上角即可立即中断模拟）。

---

## 📁 目录结构

```text
AstrbotFishEyes/
├── main.py                  # 插件主入口：注册二级 WebUI API、LLM 工具与聊天命令
├── screen_controller.py     # 统一屏幕与输入控制器 (抽象 Local 与 Remote)
├── ssh_tunnel.py            # 异步正向 SSH 端口转发隧道管理
├── metadata.yaml            # 插件元数据与说明
├── _conf_schema.json        # AstrBot 配置项规范
├── requirements.txt         # 插件依赖项
├── views/
│   └── screen_control/      # AstrBot 二级 WebUI 页面
│       ├── index.html       # 可视化控制台主页面
│       ├── app.js           # 前端交互与 AstrBot Bridge 通信
│       └── style.css        # 现代化暗色毛玻璃样式
└── Client/                  # 🌟 独立被控端桌面应用 (支持一键打包单个 Exe)
    ├── app.py               # 桌面 GUI、系统托盘与防后台休眠挂起管理
    ├── agent_server.py      # 本地屏幕截图与键鼠自动化多线程 HTTP 服务
    ├── reverse_tunnel.py    # 反向 SSH 隧道模块 (穿透内网/免公网 IP)
    ├── config.py            # 配置持久化与 Windows 开机自启
    ├── build_exe.py         # PyInstaller 一键编译单文件 EXE 脚本
    ├── build_exe.bat        # Windows 便捷双击打包脚本
    ├── requirements.txt     # 被控端打包依赖
    └── README.md            # 被控端专用说明文档
```

---

## 🚀 快速上手

### 场景一：AstrBot 运行在本地电脑（控制本机）

1. 将 `AstrbotFishEyes` 文件夹放入 AstrBot 的 `data/plugins/` 目录中。
2. 安装本地依赖：
   ```bash
   pip install -r requirements.txt
   ```
3. 在 AstrBot 管理面板中，进入本插件设置，确认 `mode` 设为 `local`（默认）。
4. 在 AstrBot 插件详情页点击打开**屏幕控制台 (View)**，即可立刻看到本机屏幕并进行点击控制！

---

### 场景二：AstrBot 运行在云服务器 / Docker（控制远程电脑）

当 AstrBot 部署在远程服务器，而你要操控你身边的个人电脑时：

#### 1. 被控电脑端（你的个人电脑）

- 运行被控端程序 `python Client/app.py` 或打包好的单文件 `AstrBotScreenAgent.exe`（在 `Client/` 目录双击 `build_exe.bat` 即可生成）。
- 该程序运行后自带现代 GUI 界面，点击最小化或关闭即可收起到系统右下角托盘在后台常驻运行。
- 如果电脑处于家庭/内网无公网 IP，在被控端界面勾选【启用反向 SSH 隧道】，填入云服务器 SSH 信息即可自动实现穿透！

#### 2. AstrBot 服务端配置

打开 AstrBot WebUI → 插件 → `AstrbotFishEyes` 设置：
- `mode`: 设置为 `remote`
- **情况 A（被控端开启了反向 SSH 隧道 或 局域网直连）**：
  - `remote_host`: `127.0.0.1`（若使用反向隧道）或被控电脑局域网 IP
  - `remote_port`: `8765`
  - `remote_token`: 与被控端设置的 Token 一致
- **情况 B（从服务器通过正向 SSH 隧道连接被控机）**：
  - `use_ssh_tunnel`: 开启
  - `ssh_host`: 被控电脑或中转跳板机 IP
  - `ssh_port`: `22`
  - `ssh_username`: SSH 用户名
  - `ssh_password` 或 `ssh_key_path`: SSH 凭据
  - `remote_port`: `8765`
  - `remote_token`: 被控端 Token

保存后，点击 WebUI 上的 **【连接】** 或在聊天中发送 `/screen 连接`，即可完成！

---

# 本插件完全使用Gemini开发，有问题的话自己交给agent修改即可
