"""
AstrBot 屏幕被控端桌面应用 (Client GUI)
======================================
单文件独立运行，支持：
1. 本地 Agent HTTP 服务与自定义端口/Token
2. 可选反向 SSH 隧道（穿透内网/免公网 IP）
3. 系统托盘常驻，支持最小化/关闭隐藏到托盘后台、防 Windows 后台挂起节流
4. 纯轻量化 GUI，支持一键打包为单个 Exe
"""

import ctypes
import os
import queue
import sys
import threading
import time
from typing import Optional


class _NullStream:
    """防止 PyInstaller --windowed 模式下 sys.stdout / sys.stderr 为 None 导致崩溃"""
    def write(self, *_args, **_kwargs):
        return 0

    def flush(self):
        pass

    def isatty(self) -> bool:
        return False


if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
from PIL import Image, ImageTk, ImageDraw

# 尝试导入系统托盘支持
try:
    import pystray
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

from config import load_config, save_config
from agent_server import AgentServer, check_port_available
from reverse_tunnel import ReverseSSHTunnel


def _disable_windows_background_throttling():
    """
    禁用 Windows 10/11 对后台隐藏窗口进程自动启用的 EcoQoS (效能模式/电源节流) 与定时器降频，
    确保窗口最小化/隐藏至系统托盘后，后台 HTTP 服务与截屏线程不会被系统挂起或降速。
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        current_process = kernel32.GetCurrentProcess()

        # 1. 提升进程优先级为 ABOVE_NORMAL_PRIORITY_CLASS (0x00008000)
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        kernel32.SetPriorityClass(current_process, ABOVE_NORMAL_PRIORITY_CLASS)

        # 2. 显式关闭 ProcessPowerThrottling (ProcessInformationClass = 4)
        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]

        PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
        PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4

        state = PROCESS_POWER_THROTTLING_STATE()
        state.Version = PROCESS_POWER_THROTTLING_CURRENT_VERSION
        state.ControlMask = (
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED
            | PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
        )
        state.StateMask = 0  # 0 表示彻底禁用节流

        kernel32.SetProcessInformation(
            current_process,
            4,  # ProcessPowerThrottling
            ctypes.byref(state),
            ctypes.sizeof(state),
        )
    except Exception:
        pass


def _set_windows_keep_awake(enable: bool):
    """在服务开启期间阻止 Windows 休眠或息屏导致截屏失败/后台挂起"""
    if sys.platform != "win32":
        return
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        if enable:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def set_auto_start_windows(enabled: bool):
    """设置或取消 Windows 开机自启"""
    if sys.platform != "win32":
        return
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        )
        app_name = "AstrBotScreenAgent"
        if enabled:
            if getattr(sys, "frozen", False):
                cmd_str = f'"{sys.executable}"'
            else:
                cmd_str = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd_str)
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[AutoStart] 注册表修改异常: {e}")


def create_tray_image():
    """动态生成一张 64x64 的托盘小图标"""
    img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill="#2563eb")
    draw.rectangle([14, 16, 50, 40], outline="white", width=3)
    draw.line([32, 40, 32, 48], fill="white", width=4)
    draw.line([22, 48, 42, 48], fill="white", width=4)
    return img


class ClientApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AstrBot 电脑控制被控端")
        self.root.geometry("620x560")
        self.root.minsize(580, 500)

        # 禁用 Windows 后台电源节流与挂起
        _disable_windows_background_throttling()

        # 跨线程安全消息队列（避免子线程/托盘线程直接操作 Tkinter 引发死锁挂起）
        self._ui_queue: "queue.Queue[tuple]" = queue.Queue()

        # 加载配置
        self.cfg = load_config()

        # 核心服务组件
        self.server = AgentServer()
        self.tunnel = ReverseSSHTunnel()

        # 托盘与窗口状态
        self.tray_icon = None
        self._tray_ready = False
        self._is_hidden_to_tray = False
        self._tray_notified_once = False
        self._is_quitting = False

        self._setup_style()
        self._setup_ui()
        self._init_tray()

        # 绑定关闭窗口事件 -> 最小化到托盘
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        # 绑定窗口最小化按钮 (_) 事件 -> 自动收起到系统托盘后台
        self.root.bind("<Unmap>", self._on_window_unmap)

        # 默认不自动开启、不自动隐藏（除非配置明确为 True）
        if bool(self.cfg.get("auto_start", False)):
            self.root.after(400, self.start_all_services)

        if bool(self.cfg.get("start_minimized", False)):
            self.root.after(800, self.hide_to_tray)

        # 定时处理跨线程 UI 队列并刷新界面状态
        self.root.after(80, self._periodic_check)

    def _setup_style(self):
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        self.root.configure(bg="#f1f5f9")
        self.style.configure(".", background="#f1f5f9", font=("Segoe UI", 9))
        self.style.configure("TNotebook", background="#f1f5f9")
        self.style.configure("TNotebook.Tab", padding=[16, 8], font=("Segoe UI", 10, "bold"))
        self.style.configure("Card.TFrame", background="#ffffff", relief="flat")
        self.style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"), foreground="#0f172a", background="#ffffff")
        self.style.configure("Muted.TLabel", font=("Segoe UI", 9), foreground="#64748b", background="#ffffff")
        self.style.configure("Success.TLabel", font=("Segoe UI", 10, "bold"), foreground="#16a34a", background="#ffffff")
        self.style.configure("Danger.TLabel", font=("Segoe UI", 10, "bold"), foreground="#dc2626", background="#ffffff")
        self.style.configure("Action.TButton", font=("Segoe UI", 10, "bold"), padding=6)

    def _setup_ui(self):
        # 顶部状态卡片
        header_card = ttk.Frame(self.root, style="Card.TFrame", padding=16)
        header_card.pack(fill="x", padx=16, pady=(16, 10))

        status_frame = ttk.Frame(header_card, style="Card.TFrame")
        status_frame.pack(side="left", fill="both", expand=True)

        self.lbl_title = ttk.Label(status_frame, text="🖥️ 被控端核心服务", style="Header.TLabel")
        self.lbl_title.pack(anchor="w")

        self.lbl_status = ttk.Label(status_frame, text="● 服务未运行", style="Danger.TLabel")
        self.lbl_status.pack(anchor="w", pady=(4, 0))

        self.lbl_substatus = ttk.Label(status_frame, text="等待启动...", style="Muted.TLabel")
        self.lbl_substatus.pack(anchor="w")

        self.btn_toggle_service = ttk.Button(
            header_card,
            text="▶ 启动服务",
            style="Action.TButton",
            command=self.toggle_services,
        )
        self.btn_toggle_service.pack(side="right", padx=8)

        # 选项卡
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=16, pady=6)

        # Tab 1: 本地服务设置
        self.tab_server = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.tab_server, text="⚙️ 服务配置")
        self._build_server_tab()

        # Tab 2: 反向 SSH 隧道
        self.tab_tunnel = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.tab_tunnel, text="🔒 反向 SSH 隧道")
        self._build_tunnel_tab()

        # Tab 3: 运行日志
        self.tab_logs = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.tab_logs, text="📜 运行日志")
        self._build_logs_tab()

        # 底部操作栏
        bottom_bar = ttk.Frame(self.root, padding=(16, 10))
        bottom_bar.pack(fill="x")

        btn_save = ttk.Button(bottom_bar, text="💾 保存所有配置", command=self.save_current_config)
        btn_save.pack(side="left")

        btn_exit = ttk.Button(bottom_bar, text="❌ 彻底退出", command=self.quit_app)
        btn_exit.pack(side="right", padx=6)

        btn_hide = ttk.Button(bottom_bar, text="📥 隐藏到托盘后台", command=self.hide_to_tray)
        btn_hide.pack(side="right")

    def _build_server_tab(self):
        row1 = ttk.Frame(self.tab_server)
        row1.pack(fill="x", pady=6)
        ttk.Label(row1, text="本地监听端口:", width=16).pack(side="left")
        self.entry_port = ttk.Entry(row1, width=12)
        self.entry_port.insert(0, str(self.cfg.get("port", 8765)))
        self.entry_port.pack(side="left")
        ttk.Label(row1, text=" (默认 8765)", foreground="#64748b").pack(side="left", padx=6)

        row2 = ttk.Frame(self.tab_server)
        row2.pack(fill="x", pady=6)
        ttk.Label(row2, text="监听网络地址:", width=16).pack(side="left")
        self.combo_host = ttk.Combobox(row2, values=["0.0.0.0", "127.0.0.1"], width=12)
        self.combo_host.set(self.cfg.get("host", "0.0.0.0"))
        self.combo_host.pack(side="left")
        ttk.Label(row2, text=" (0.0.0.0 允许局域网设备直接访问)", foreground="#64748b").pack(side="left", padx=6)

        row3 = ttk.Frame(self.tab_server)
        row3.pack(fill="x", pady=6)
        ttk.Label(row3, text="访问安全 Token:", width=16).pack(side="left")
        self.entry_token = ttk.Entry(row3, width=28, show="*")
        self.entry_token.insert(0, str(self.cfg.get("token", "")))
        self.entry_token.pack(side="left")
        ttk.Label(row3, text=" (防非授权访问，建议配置)", foreground="#64748b").pack(side="left", padx=6)

        sep = ttk.Separator(self.tab_server, orient="horizontal")
        sep.pack(fill="x", pady=14)

        # 默认不勾选：打开应用自动启动服务
        self.var_auto_start = tk.BooleanVar(value=bool(self.cfg.get("auto_start", False)))
        chk1 = ttk.Checkbutton(self.tab_server, text="打开应用时自动启动服务", variable=self.var_auto_start)
        chk1.pack(anchor="w", pady=4)

        # 默认不勾选：自动后台托管
        self.var_minimized = tk.BooleanVar(value=bool(self.cfg.get("start_minimized", False)))
        chk2 = ttk.Checkbutton(self.tab_server, text="启动后自动静默最小化到系统托盘", variable=self.var_minimized)
        chk2.pack(anchor="w", pady=4)

        # 默认不勾选：开机自启
        self.var_boot = tk.BooleanVar(value=bool(self.cfg.get("auto_launch_boot", False)))
        chk3 = ttk.Checkbutton(self.tab_server, text="随 Windows 开机自动启动并在后台驻留", variable=self.var_boot)
        chk3.pack(anchor="w", pady=4)

    def _build_tunnel_tab(self):
        desc = (
            "💡 反向 SSH 隧道：当本电脑处于局域网/内网、没有公网 IP 时使用。\n"
            "本机会主动连接云端 AstrBot 服务器，将服务器指定端口反向转发回本程序。\n"
            "AstrBot 即可在服务器本地以 127.0.0.1 直接操控本电脑，无需映射端口。"
        )
        ttk.Label(self.tab_tunnel, text=desc, foreground="#475569", justify="left").pack(anchor="w", pady=(0, 10))

        self.var_enable_tunnel = tk.BooleanVar(value=bool(self.cfg.get("enable_reverse_tunnel", False)))
        chk_tunnel = ttk.Checkbutton(
            self.tab_tunnel,
            text="启用反向 SSH 隧道连接",
            variable=self.var_enable_tunnel,
        )
        chk_tunnel.pack(anchor="w", pady=6)

        row1 = ttk.Frame(self.tab_tunnel)
        row1.pack(fill="x", pady=4)
        ttk.Label(row1, text="SSH 服务器地址:", width=16).pack(side="left")
        self.entry_ssh_host = ttk.Entry(row1, width=24)
        self.entry_ssh_host.insert(0, str(self.cfg.get("ssh_host", "")))
        self.entry_ssh_host.pack(side="left")

        ttk.Label(row1, text="端口:", width=6).pack(side="left", padx=(10, 0))
        self.entry_ssh_port = ttk.Entry(row1, width=8)
        self.entry_ssh_port.insert(0, str(self.cfg.get("ssh_port", 22)))
        self.entry_ssh_port.pack(side="left")

        row2 = ttk.Frame(self.tab_tunnel)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="SSH 用户名:", width=16).pack(side="left")
        self.entry_ssh_user = ttk.Entry(row2, width=24)
        self.entry_ssh_user.insert(0, str(self.cfg.get("ssh_user", "")))
        self.entry_ssh_user.pack(side="left")

        row3 = ttk.Frame(self.tab_tunnel)
        row3.pack(fill="x", pady=4)
        ttk.Label(row3, text="SSH 登录密码:", width=16).pack(side="left")
        self.entry_ssh_pass = ttk.Entry(row3, width=24, show="*")
        self.entry_ssh_pass.insert(0, str(self.cfg.get("ssh_password", "")))
        self.entry_ssh_pass.pack(side="left")
        ttk.Label(row3, text=" (与私钥二选一)", foreground="#64748b").pack(side="left", padx=6)

        row4 = ttk.Frame(self.tab_tunnel)
        row4.pack(fill="x", pady=4)
        ttk.Label(row4, text="SSH 私钥文件:", width=16).pack(side="left")
        self.entry_ssh_key = ttk.Entry(row4, width=24)
        self.entry_ssh_key.insert(0, str(self.cfg.get("ssh_key_path", "")))
        self.entry_ssh_key.pack(side="left")
        ttk.Button(row4, text="浏览...", width=8, command=self._browse_ssh_key).pack(side="left", padx=6)

        row5 = ttk.Frame(self.tab_tunnel)
        row5.pack(fill="x", pady=6)
        ttk.Label(row5, text="服务器转发端口:", width=16).pack(side="left")
        self.entry_remote_forward = ttk.Entry(row5, width=12)
        self.entry_remote_forward.insert(0, str(self.cfg.get("remote_forward_port", 8765)))
        self.entry_remote_forward.pack(side="left")
        ttk.Label(row5, text=" (在云服务器上暴露该端口供 AstrBot 调用)", foreground="#64748b").pack(side="left", padx=6)

    def _build_logs_tab(self):
        self.txt_logs = scrolledtext.ScrolledText(self.tab_logs, wrap="word", font=("Consolas", 9), bg="#0f172a", fg="#f8fafc")
        self.txt_logs.pack(fill="both", expand=True)

        btn_clear = ttk.Button(self.tab_logs, text="清空日志", command=lambda: self.txt_logs.delete("1.0", tk.END))
        btn_clear.pack(anchor="e", pady=(6, 0))

    def _browse_ssh_key(self):
        path = filedialog.askopenfilename(title="选择 SSH 私钥文件", filetypes=[("All Files", "*.*")])
        if path:
            self.entry_ssh_key.delete(0, tk.END)
            self.entry_ssh_key.insert(0, path)

    # =========================================================================
    #  系统托盘与最小化后台托管逻辑（全队列驱动，杜绝跨线程死锁）
    # =========================================================================

    def _init_tray(self):
        if not HAS_TRAY:
            return

        try:
            image = create_tray_image()
            menu = pystray.Menu(
                pystray.MenuItem(
                    "🖥️ 显示控制面板",
                    lambda icon, item: self._ui_queue.put(("show_window", None)),
                    default=True,
                ),
                pystray.MenuItem(
                    "▶️ 启动/停止服务",
                    lambda icon, item: self._ui_queue.put(("toggle_services", None)),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "❌ 退出程序",
                    lambda icon, item: self._ui_queue.put(("quit_app", None)),
                ),
            )

            self.tray_icon = pystray.Icon(
                "AstrBotScreenAgent",
                image,
                "AstrBot 电脑控制被控端",
                menu,
            )

            def _on_tray_setup(icon):
                icon.visible = True
                self._tray_ready = True

            def _tray_runner():
                try:
                    self.tray_icon.run(setup=_on_tray_setup)
                except Exception as e:
                    self._tray_ready = False
                    self._ui_queue.put(("log", f"[WARN] 系统托盘运行异常: {e}"))

            threading.Thread(target=_tray_runner, daemon=True, name="TrayThread").start()
        except Exception as e:
            self._tray_ready = False
            self.log(f"[WARN] 初始化系统托盘失败: {e}")

    def _on_window_unmap(self, event=None):
        """监听用户点击窗口右上角最小化按钮 (_) 事件，自动转入系统托盘后台"""
        if event is not None and event.widget is not self.root:
            return
        if self._is_quitting or self._is_hidden_to_tray:
            return
        try:
            if self.root.state() == "iconic":
                # 延迟 20ms 等待 Windows 完成最小化动画后再隐藏到托盘，避免窗口句柄状态卡死
                self.root.after(20, self.hide_to_tray)
        except Exception:
            pass

    def hide_to_tray(self):
        """最小化隐藏窗口到系统托盘后台"""
        if self._is_quitting:
            return

        # 若系统托盘不可用，降级为普通任务栏最小化，防止窗口彻底消失无法找回
        if not (HAS_TRAY and self.tray_icon and self._tray_ready):
            try:
                self.root.iconify()
            except Exception:
                pass
            return

        self._is_hidden_to_tray = True
        try:
            self.root.withdraw()
        except Exception:
            pass

        # 仅首次隐藏时在独立线程异步弹出托盘提示，避免阻塞主线程
        if not self._tray_notified_once:
            self._tray_notified_once = True

            def _async_notify():
                try:
                    if self.tray_icon and getattr(self.tray_icon, "visible", False):
                        self.tray_icon.notify(
                            "已最小化到系统托盘并在后台常驻运行，双击托盘图标可重新打开界面。",
                            "AstrBot 屏幕被控端",
                        )
                except Exception:
                    pass

            threading.Thread(target=_async_notify, daemon=True).start()

    def _do_show_window(self):
        """在主线程中安全恢复显示窗口（解决 iconic 状态下 deiconify 无法弹出的问题）"""
        self._is_hidden_to_tray = False
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.focus_force()
            self.root.after(150, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def show_from_tray(self, icon=None, item=None):
        """从托盘恢复窗口显示（线程安全）"""
        self._ui_queue.put(("show_window", None))

    # =========================================================================
    #  日志输出与状态刷新
    # =========================================================================

    def log(self, text: str):
        """线程安全的日志投递"""
        self._ui_queue.put(("log", text))

    def _append_log_main_thread(self, text: str):
        if hasattr(self, "txt_logs"):
            try:
                self.txt_logs.insert(tk.END, text + "\n")
                # 限制最大行数防止长期后台运行内存膨胀
                line_count = int(self.txt_logs.index("end-1c").split(".")[0])
                if line_count > 1000:
                    self.txt_logs.delete("1.0", f"{line_count - 800}.0")
                self.txt_logs.see(tk.END)
            except Exception:
                pass

    def _update_ui_state(self):
        is_running = self.server.is_running
        if is_running:
            self.lbl_status.config(text="● 服务运行中", style="Success.TLabel")
            port = self.entry_port.get().strip()
            tunnel_active = self.tunnel.is_running
            sub = f"监听端口: {port} | 反向隧道: {'🟢 已连接' if tunnel_active else '⚪ 未开启'}"
            self.lbl_substatus.config(text=sub)
            self.btn_toggle_service.config(text="⏹ 停止服务")
        else:
            self.lbl_status.config(text="● 服务已停止", style="Danger.TLabel")
            self.lbl_substatus.config(text="未对外提供控制接口")
            self.btn_toggle_service.config(text="▶ 启动服务")

    def _periodic_check(self):
        """主线程定时轮询跨线程事件队列并同步 UI 状态"""
        if self._is_quitting:
            return

        # 批量消费队列事件
        for _ in range(50):
            try:
                event_type, payload = self._ui_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "log":
                self._append_log_main_thread(str(payload))
            elif event_type == "update_state":
                self._update_ui_state()
            elif event_type == "show_window":
                self._do_show_window()
            elif event_type == "toggle_services":
                self.toggle_services()
            elif event_type == "quit_app":
                self.quit_app()
                return

        self._update_ui_state()
        if not self._is_quitting:
            self.root.after(100, self._periodic_check)

    # =========================================================================
    #  服务启停与保存
    # =========================================================================

    def start_all_services(self):
        if self.server.is_running:
            return

        try:
            port = int(self.entry_port.get().strip() or 8765)
            if not (1 <= port <= 65535):
                raise ValueError("Port out of range")
        except ValueError:
            messagebox.showerror("格式错误", "本地监听端口必须为 1~65535 之间的整数 (例如 8765)")
            return

        host = self.combo_host.get().strip() or "0.0.0.0"
        token = self.entry_token.get().strip()

        # 预先检查端口占用
        if not check_port_available(host, port):
            err_msg = f"本地端口 {port} 已被占用！\n请更换其他端口（如 {port + 1}）或关闭占用该端口的应用后再试。"
            self.log(f"[ERROR] {err_msg}")
            if not self._is_hidden_to_tray:
                messagebox.showerror("端口冲突", err_msg)
            return

        # 启动 Agent 本地 HTTP 服务
        ok = self.server.start(
            host=host,
            port=port,
            token=token,
            log_callback=self.log,
            status_callback=lambda _running: self._ui_queue.put(("update_state", None)),
        )

        if not ok:
            err_msg = f"本地服务启动失败（端口 {host}:{port}），请查看【运行日志】了解详情。"
            if not self._is_hidden_to_tray:
                messagebox.showerror("启动失败", err_msg)
            self._update_ui_state()
            return

        # 开启 Windows 防休眠保活
        _set_windows_keep_awake(True)

        # 若开启了反向隧道
        if self.var_enable_tunnel.get():
            ssh_host = self.entry_ssh_host.get().strip()
            try:
                ssh_port = int(self.entry_ssh_port.get().strip() or 22)
            except ValueError:
                ssh_port = 22
            ssh_user = self.entry_ssh_user.get().strip()
            ssh_pass = self.entry_ssh_pass.get()
            ssh_key = self.entry_ssh_key.get().strip()
            try:
                remote_fwd = int(self.entry_remote_forward.get().strip() or 8765)
            except ValueError:
                remote_fwd = 8765

            self.tunnel.start(
                ssh_host=ssh_host,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                ssh_password=ssh_pass,
                ssh_key_path=ssh_key,
                remote_port=remote_fwd,
                local_port=port,
                log_callback=self.log,
                status_callback=lambda _s: self._ui_queue.put(("update_state", None)),
            )

        self._update_ui_state()

    def stop_all_services(self):
        self.tunnel.stop()
        self.server.stop()
        _set_windows_keep_awake(False)
        self._update_ui_state()

    def toggle_services(self, icon=None, item=None):
        if threading.current_thread() is not threading.main_thread():
            self._ui_queue.put(("toggle_services", None))
            return
        if self.server.is_running:
            self.stop_all_services()
        else:
            self.start_all_services()

    def save_current_config(self):
        try:
            self.cfg["port"] = int(self.entry_port.get().strip() or 8765)
            self.cfg["host"] = self.combo_host.get().strip() or "0.0.0.0"
            self.cfg["token"] = self.entry_token.get().strip()
            self.cfg["auto_start"] = self.var_auto_start.get()
            self.cfg["start_minimized"] = self.var_minimized.get()
            self.cfg["auto_launch_boot"] = self.var_boot.get()

            self.cfg["enable_reverse_tunnel"] = self.var_enable_tunnel.get()
            self.cfg["ssh_host"] = self.entry_ssh_host.get().strip()
            self.cfg["ssh_port"] = int(self.entry_ssh_port.get().strip() or 22)
            self.cfg["ssh_user"] = self.entry_ssh_user.get().strip()
            self.cfg["ssh_password"] = self.entry_ssh_pass.get()
            self.cfg["ssh_key_path"] = self.entry_ssh_key.get().strip()
            self.cfg["remote_forward_port"] = int(self.entry_remote_forward.get().strip() or 8765)

            save_config(self.cfg)
            set_auto_start_windows(self.cfg["auto_launch_boot"])
            messagebox.showinfo("成功", "配置已成功保存！")
        except Exception as e:
            messagebox.showerror("错误", f"保存配置失败: {e}")

    def quit_app(self, icon=None, item=None):
        if threading.current_thread() is not threading.main_thread():
            self._ui_queue.put(("quit_app", None))
            return
        if self._is_quitting:
            return
        self._is_quitting = True
        self.stop_all_services()
        if HAS_TRAY and self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass


def main():
    root = tk.Tk()
    app = ClientApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
