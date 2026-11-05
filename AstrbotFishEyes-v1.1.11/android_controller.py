"""
Android (ADB) 控制器 (AstrbotFishEyes - Android ADB Device Controller)
=====================================================================

本模块让 AstrBot 可以直接通过 `adb` 操作一台 Android 手机 / 平板：

1. **配对与连接**：`adb pair`（无线调试配对码）/ `adb connect`（无线调试或 SSH 反向隧道暴露的 adbd 端口）；
2. **画面**：`adb exec-out screencap -p` 抓屏，自动转 JPEG 并可按宽度压缩，坐标自动换算回设备真实像素；
3. **触控**：`input tap` / `input swipe`（拖拽、长按、上下左右滑动）/ `input keyevent` / `input keycombination`；
4. **文本**：ASCII 走 `input text`，中文等非 ASCII 依次尝试 ADBKeyboard（广播）/ 剪贴板粘贴；
5. **应用与 Shell**：启动/停止应用、列出已安装包、执行任意 `adb shell` 命令；
6. **状态**：设备序列号、型号、Android 版本、分辨率、ADB 版本与设备列表；
7. **节能**：与 PC 模式一致的显式连接 / 断开 / 无操作自动断连。

设计原则：所有 adb 调用均走 `asyncio.create_subprocess_exec`（不经过本地 shell，天然免疫路径空格与转义问题），
任何失败都给出可直接执行的中文排查提示。
"""

import asyncio
import base64
import io
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from android_stream import (
    AndroidScreenStream,
    build_ffmpeg_argv,
    build_screenrecord_argv,
    even,
    find_ffmpeg,
)

PLUGIN_TAG = "AstrbotFishEyes"

# Windows 下隐藏 adb 子进程的控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_ADB_BIN_NAME = "adb.exe" if sys.platform == "win32" else "adb"

# ADBKeyboard（开源输入法）用于向 Android 注入中文等非 ASCII 文本
ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"

# 设备端 JPEG 编码器（随插件分发的小 dex，用 app_process 运行，无需安装 APK）
DEVICE_DEX_REMOTE = "/data/local/tmp/fisheyes_jpeg.dex"
DEVICE_TMP_PNG = "/data/local/tmp/.fisheyes_cap.png"
DEVICE_TMP_JPG = "/data/local/tmp/.fisheyes_cap.jpg"

# 常用键名 → Android KeyEvent 码
ANDROID_KEYCODES: Dict[str, int] = {
    "enter": 66, "回车": 66, "return": 66,
    "escape": 4, "esc": 4, "back": 4, "返回": 4,
    "backspace": 67, "退格": 67, "del": 67, "delete": 67, "删除": 67,
    "tab": 61, "制表": 61,
    "space": 62, "空格": 62,
    "home": 3, "主屏": 3, "win": 3, "windows": 3, "desktop": 3,
    "recents": 187, "recent": 187, "app_switch": 187, "多任务": 187,
    "menu": 82, "菜单": 82, "f5": 82,
    "power": 26, "电源": 26,
    "wakeup": 224, "点亮": 224, "sleep": 223, "休眠": 223,
    "volume_up": 24, "音量+": 24, "volume_down": 25, "音量-": 25, "mute": 164, "静音": 164,
    "camera": 27, "相机": 27,
    "up": 19, "上": 19, "down": 20, "下": 20, "left": 21, "左": 21, "right": 22, "右": 22,
    "center": 23, "ok": 23, "确定": 23,
    "notification": 83, "通知": 83,
    "search": 84, "搜索": 84,
    "play_pause": 85, "media_play_pause": 85, "播放暂停": 85,
    "page_up": 92, "page_down": 93,
    "move_home": 122, "move_end": 123,
    "copy": 278, "paste": 279, "cut": 277, "select_all": 292,
    "alt_left": 57, "alt_right": 58, "ctrl_left": 113, "ctrl_right": 114,
    "shift_left": 59, "shift_right": 60, "meta_left": 117, "meta_right": 118,
}

# PC 键名 → Android 键名的近似映射（让 WebUI / 聊天里沿用同一套按键名）
PC_TO_ANDROID_KEY = {
    "win": "home", "windows": "home", "super": "home", "cmd": "home",
    "escape": "back", "esc": "back",
    "f5": "menu",
    "alt": "alt_left", "ctrl": "ctrl_left", "shift": "shift_left",
}


def _split_command_line(raw: str) -> List[str]:
    """把用户填写的 adb 路径/命令行拆成 argv（Windows 保留反斜杠，Linux 走 POSIX 规则）"""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parts = shlex.split(raw, posix=(os.name != "nt"))
    except ValueError:
        parts = raw.split()
    cleaned: List[str] = []
    for p in parts:
        p = p.strip()
        if len(p) >= 2 and p[0] == p[-1] and p[0] in ("'", '"'):
            p = p[1:-1]
        if p:
            cleaned.append(p)
    return cleaned


def parse_adb_devices(output: str) -> List[Dict[str, str]]:
    """
    解析 `adb devices -l` 输出：
        List of devices attached
        R58M12345    device product:beyond1q model:SM_G9730 device:beyond1q transport_id:1
        127.0.0.1:5555   device product:... model:...
    """
    devices: List[Dict[str, str]] = []
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        if line.lower().startswith("adb server") or line.lower().startswith("daemon"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state not in ("device", "offline", "unauthorized", "no", "recovery", "sideload", "bootloader"):
            continue
        info: Dict[str, str] = {"serial": serial, "state": state}
        for token in parts[2:]:
            if ":" in token:
                key, _, value = token.partition(":")
                info[key] = value
        devices.append(info)
    devices.sort(key=lambda d: (0 if d.get("state") == "device" else 1, d.get("serial", "")))
    return devices


def _escape_input_text(text: str) -> str:
    """把文本转换成 `input text` 可安全接收的形式：空格用 %s，Shell 元字符加反斜杠转义"""
    text = text.replace(" ", "%s")
    return re.sub(r'([\\\'"`$&|;<>()*?\[\]{}#~!^])', r"\\\1", text)


def _shell_quote(text: str) -> str:
    """设备侧 sh 的单引号安全引用"""
    return "'" + str(text).replace("'", "'\\''") + "'"


class AndroidADBController:
    """Android ADB 设备控制器（接口与 UnifiedController 保持一致，便于上层统一调度）"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config if config is not None else {}
        self.is_locked: bool = False

        # 连接状态
        self._connected: bool = False
        self._last_error: Optional[str] = None
        self.last_active_time: float = 0.0
        self._idle_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # adb 可执行文件
        self._adb_cmd: List[str] = []
        self._adb_version: str = ""
        self._adb_source: str = ""
        self._adb_error: str = ""
        self._adb_probe_time: float = 0.0
        self._adb_auto_filled: bool = False

        # 设备信息
        self._serial: str = ""
        self._model: str = ""
        self._manufacturer: str = ""
        self._android_version: str = ""
        self._wm_size: Tuple[int, int] = (0, 0)          # wm size 报告的设备分辨率
        self._device_size: Tuple[int, int] = (0, 0)      # 截图 PNG 的原始像素（= 输入坐标空间）
        self._capture_size: Tuple[int, int] = (0, 0)     # 压缩后 JPEG 的像素（= 前端 / 视觉模型看到的坐标空间）
        self._scale: Tuple[float, float] = (1.0, 1.0)
        self._devices_cache: List[Dict[str, str]] = []
        self._devices_cache_time: float = 0.0
        self._ime_cache: Optional[bool] = None

        # 日志（由 DeviceController 注入共享列表）
        self.action_logs: List[Dict[str, Any]] = []
        self._last_screenshot_cache: Optional[Dict[str, Any]] = None

        # 低延迟视频流（可选，需 ffmpeg）
        self._stream: Optional[AndroidScreenStream] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._stream_reason: str = ""
        self._stream_ffmpeg_variant: int = 0
        self._stream_size_used: Optional[str] = None
        self._last_png_digest: str = ""
        self._last_jpeg: Optional[bytes] = None
        self._last_jpeg_meta: Tuple[int, int, int, int] = (0, 0, 0, 0)

        # 设备端 JPEG 编码（无需 ffmpeg：手机侧先缩放+压缩，再传回来）
        self._dex_ready: bool = False
        self._jpeg_reason: str = ""
        self._jpeg_failures: int = 0
        self._app_process: str = ""
        self._jpeg_variant: str = ""
        self._diag_cache: Dict[str, Any] = {}
        self._ffmpeg_version_cache: str = ""

    # =========================================================================
    #  基础属性
    # =========================================================================

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def mode(self) -> str:
        return "android"

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def idle_timeout(self) -> int:
        """Android 目标的无操作自动断连时间（秒）；默认 0 = 永不断连"""
        try:
            return max(0, int(self.config.get("adb_idle_timeout", 0)))
        except Exception:
            return 0

    def touch_activity(self):
        self.last_active_time = time.time()

    def _is_bot_source(self, source: str) -> bool:
        s = (source or "").lower()
        return s in ("llm", "bot") or s.startswith("chat")

    def record_log(self, action: str, details: str, status: str = "ok", source: str = "system"):
        self.action_logs.insert(0, {
            "time": time.strftime("%H:%M:%S"),
            "action": action,
            "details": details,
            "status": status,
            "source": source,
        })
        if len(self.action_logs) > 50:
            self.action_logs.pop()

    # =========================================================================
    #  无操作自动断连
    # =========================================================================

    def _ensure_idle_monitor(self):
        try:
            loop = asyncio.get_running_loop()
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = loop.create_task(self._idle_monitor_loop())
        except RuntimeError:
            pass

    async def _idle_monitor_loop(self):
        try:
            while self._connected:
                await asyncio.sleep(3.0)
                await self.check_idle_timeout()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def check_idle_timeout(self) -> bool:
        if not self._connected:
            return False
        timeout_sec = self.idle_timeout
        if timeout_sec <= 0 or self.last_active_time <= 0:
            return False
        if time.time() - self.last_active_time >= timeout_sec:
            await self.disconnect(
                reason=f"超过 {timeout_sec} 秒无操作，已自动断开 Android 连接并释放内存",
                source="idle_timer",
            )
            return True
        return False

    # =========================================================================
    #  adb 定位与进程调用
    # =========================================================================

    def _bundled_adb_candidates(self) -> List[List[str]]:
        """
        插件自带 platform-tools 的候选路径（默认打包 Windows / Linux 的 adb）。

        目录约定：
            platform-tools/windows/adb.exe + AdbWinApi.dll + AdbWinUsbApi.dll
            platform-tools/linux/adb
            platform-tools/darwin/adb        （可选，自行放入）
            platform-tools/adb[.exe]         （平铺方式也支持）
        """
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        if sys.platform == "win32":
            subdirs = ["windows", "win"]
            names = ["adb.exe", "adb"]
        elif sys.platform == "darwin":
            subdirs = ["darwin", "macos", "mac"]
            names = ["adb"]
        else:
            subdirs = ["linux"]
            names = ["adb"]

        out: List[List[str]] = []
        for sub in subdirs:
            for name in names:
                out.append([os.path.join(plugin_dir, "platform-tools", sub, name)])
        for name in names:
            out.append([os.path.join(plugin_dir, "platform-tools", name)])
            out.append([os.path.join(plugin_dir, "android", "platform-tools", name)])
        return out

    def _candidate_adb_commands(self) -> List[List[str]]:
        """
        按优先级返回 adb 启动命令的候选 argv 列表：
        1. 插件设置 adb_path（允许带参数，整行作为 argv 前缀）
        2. 环境变量 ADB_PATH
        3. **插件自带的 platform-tools**（随包分发，免安装）
        4. 系统 PATH 中的 adb
        5. ANDROID_HOME / ANDROID_SDK_ROOT / 常见安装目录
        """
        cmds: List[List[str]] = []
        seen = set()

        def add(argv: List[str]):
            if not argv:
                return
            key = tuple(a.lower() if sys.platform == "win32" else a for a in argv)
            if key in seen:
                return
            seen.add(key)
            cmds.append(argv)

        raw_cfg = str(self.config.get("adb_path") or "").strip()
        if raw_cfg:
            add(_split_command_line(raw_cfg))

        env_path = os.environ.get("ADB_PATH", "").strip()
        if env_path:
            add(_split_command_line(env_path))

        # 插件自带 adb（默认随包分发）
        for argv in self._bundled_adb_candidates():
            add(argv)

        which = shutil.which("adb")
        if which:
            add([which])

        sdk_roots = [
            os.environ.get("ANDROID_HOME", ""),
            os.environ.get("ANDROID_SDK_ROOT", ""),
            os.path.expanduser("~/Android/Sdk"),
            os.path.expanduser("~/Library/Android/sdk"),
            os.path.expanduser("~/Android/Sdk/platform-tools"),
        ]
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                sdk_roots.append(os.path.join(local, "Android", "Sdk"))
            sdk_roots.append(r"C:\Android\Sdk")
            sdk_roots.append(r"C:\platform-tools")
        else:
            sdk_roots.extend(["/usr/lib/android-sdk", "/opt/android-sdk", "/usr/local/platform-tools"])

        for root in sdk_roots:
            if not root:
                continue
            add([os.path.join(root, "platform-tools", _ADB_BIN_NAME)])
            add([os.path.join(root, _ADB_BIN_NAME)])

        return cmds

    def _detect_adb_source(self, argv: List[str]) -> str:
        """判断最终选中的 adb 来自哪里：bundled / config / env / path / sdk"""
        # 插件内置优先判断：自动填写到 adb_path 后仍然是"内置"
        try:
            plugin_dir = os.path.dirname(os.path.abspath(__file__)).lower()
            if os.path.abspath(argv[0]).lower().startswith(plugin_dir):
                return "bundled"
        except Exception:
            pass
        cfg = str(self.config.get("adb_path") or "").strip()
        if cfg and _split_command_line(cfg) == argv:
            return "config"
        env_path = os.environ.get("ADB_PATH", "").strip()
        if env_path and _split_command_line(env_path) == argv:
            return "env"
        if shutil.which(argv[0]):
            return "path"
        return "sdk"

    def bundled_adb_path(self) -> str:
        """当前系统对应的内置 adb 预期路径（不检查是否存在）"""
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        if sys.platform == "win32":
            return os.path.join(plugin_dir, "platform-tools", "windows", "adb.exe")
        if sys.platform == "darwin":
            return os.path.join(plugin_dir, "platform-tools", "darwin", "adb")
        return os.path.join(plugin_dir, "platform-tools", "linux", "adb")

    def adb_path_display(self) -> str:
        """当前生效的 adb 路径（字符串形式）"""
        return " ".join(self._adb_cmd) if self._adb_cmd else ""

    def consume_auto_filled(self) -> bool:
        """是否发生过"自动填写 adb_path"（消费一次，供上层持久化）"""
        if self._adb_auto_filled:
            self._adb_auto_filled = False
            return True
        return False

    def invalidate_adb(self):
        """配置里的 adb 路径变化后，丢弃已解析的 adb 缓存，下次调用重新探测"""
        self._adb_cmd = []
        self._adb_version = ""
        self._adb_source = ""
        self._adb_error = ""
        self._adb_probe_time = 0.0
        self._ime_cache = None
        self._devices_cache = []
        self._devices_cache_time = 0.0

    async def resolve_adb(self, force: bool = False) -> List[str]:
        """定位可用的 adb 可执行文件（带缓存），返回 argv 前缀"""
        if self._adb_cmd and not force:
            return self._adb_cmd
        # 探测失败后 10 秒内不重复探测，避免状态轮询频繁拉起进程
        if (
            not force
            and not self._adb_cmd
            and self._adb_error
            and (time.time() - self._adb_probe_time) < 10.0
        ):
            raise RuntimeError(self._adb_error)

        errors: List[str] = []
        for argv in self._candidate_adb_commands():
            first = argv[0]
            # 纯命令名交给系统 PATH 解析
            looks_like_path = os.sep in first or (os.altsep and os.altsep in first)
            if looks_like_path and not os.path.isfile(first):
                continue
            if not looks_like_path and shutil.which(first) is None:
                continue
            # Linux / macOS：zip 解包会丢掉可执行位，这里补回来（内置 adb 必需）
            if os.name != "nt" and os.path.isfile(first):
                try:
                    if not os.access(first, os.X_OK):
                        os.chmod(first, 0o755)
                except Exception:
                    pass
            try:
                code, out, err = await self._run(argv + ["version"], timeout=12)
                text = (out or b"").decode("utf-8", errors="replace") + (err or b"").decode("utf-8", errors="replace")
                if not (code == 0 and "Android Debug Bridge" in text) and os.name != "nt" and os.path.isfile(first):
                    # 可能是解包丢可执行位 / 权限不足，补一次 chmod 再试
                    try:
                        os.chmod(first, 0o755)
                        code, out, err = await self._run(argv + ["version"], timeout=12)
                        text = (out or b"").decode("utf-8", errors="replace") + (err or b"").decode("utf-8", errors="replace")
                    except Exception:
                        pass
                if code == 0 and "Android Debug Bridge" in text:
                    self._adb_cmd = argv
                    self._adb_source = self._detect_adb_source(argv)
                    first_line = text.strip().splitlines()[0].strip()
                    self._adb_version = first_line
                    self._adb_error = ""
                    # 自动把实际路径写回配置：用户不用手填，设置页能直接看到
                    cfg_raw = str(self.config.get("adb_path") or "").strip()
                    if not cfg_raw or (self._adb_auto_filled and _split_command_line(cfg_raw) != argv):
                        self.config["adb_path"] = argv[0]
                        self._adb_auto_filled = True
                    return self._adb_cmd
                errors.append(f"{' '.join(argv)}: {text.strip()[:120] or '无法执行'}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{' '.join(argv)}: {e}")

        self._adb_cmd = []
        self._adb_source = ""
        self._adb_probe_time = time.time()
        self._adb_error = (
            "未找到可用的 adb 可执行文件。本插件默认自带 platform-tools"
            f"（{os.path.join(os.path.dirname(os.path.abspath(__file__)), 'platform-tools')}），"
            "若该目录缺失，请重新安装完整插件包，或在插件设置【ADB 可执行文件路径】中填写 adb 绝对路径。"
        )
        if errors:
            self._adb_error += "\n已尝试: " + "；".join(errors[:5])
        raise RuntimeError(self._adb_error)

    async def _run(self, cmd: List[str], timeout: Optional[float] = None) -> Tuple[int, bytes, bytes]:
        """执行外部命令，返回 (returncode, stdout_bytes, stderr_bytes)"""
        timeout_sec = float(timeout or self.config.get("command_timeout", 30) or 30)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise TimeoutError(f"命令超时（{timeout_sec:.0f}s）: {' '.join(cmd)}")
        return (proc.returncode if proc.returncode is not None else -1), out or b"", err or b""

    async def _adb(
        self,
        *args: str,
        use_serial: bool = True,
        timeout: Optional[float] = None,
    ) -> Tuple[int, bytes, bytes]:
        """调用 adb（自动带上 -s <serial>）"""
        await self.resolve_adb()
        cmd = list(self._adb_cmd)
        if use_serial and self._serial:
            cmd.extend(["-s", self._serial])
        cmd.extend([str(a) for a in args])
        return await self._run(cmd, timeout=timeout)

    async def _adb_shell(self, command: str, timeout: Optional[float] = None) -> Tuple[int, str, str]:
        """执行 `adb shell <command>`（命令整体作为一个参数交给设备侧 sh）"""
        code, out, err = await self._adb("shell", command, timeout=timeout)
        return code, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")

    @staticmethod
    def _short(text: str, limit: int = 300) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    # =========================================================================
    #  设备发现 / 配对 / 连接
    # =========================================================================

    async def devices(self, refresh: bool = True) -> List[Dict[str, str]]:
        """列出 adb 设备（带 5 秒缓存）"""
        now = time.time()
        if not refresh and self._devices_cache and (now - self._devices_cache_time) < 5.0:
            return self._devices_cache
        code, out, err = await self._adb("devices", "-l", use_serial=False, timeout=15)
        text = out.decode("utf-8", errors="replace")
        if code != 0 and not text.strip():
            raise RuntimeError(f"执行 adb devices 失败: {self._short(err.decode('utf-8', errors='replace'))}")
        self._devices_cache = parse_adb_devices(text)
        self._devices_cache_time = now
        return self._devices_cache

    @staticmethod
    def _split_host_port(host: str, port: Any = None) -> Tuple[str, str]:
        host = str(host or "").strip()
        if host.count(":") == 1 and not host.startswith("["):
            h, _, p = host.partition(":")
            if p.isdigit():
                return h or "127.0.0.1", p
        return host or "127.0.0.1", str(port or "").strip() or "5555"

    async def pair(self, host: str, port: Any, code: str) -> Dict[str, Any]:
        """无线调试配对：`adb pair <host>:<port> <配对码>`"""
        host, port = self._split_host_port(host, port)
        code = str(code or "").strip()
        if not code:
            return {"ok": False, "message": "配对码不能为空：请在手机「无线调试 → 使用配对码配对设备」中查看 6 位配对码。"}
        await self.resolve_adb()
        addr = f"{host}:{port}"
        rc, out, err = await self._adb("pair", addr, code, use_serial=False, timeout=90)
        text = (out.decode("utf-8", errors="replace") + err.decode("utf-8", errors="replace")).strip()
        ok = rc == 0 and "successfully paired" in text.lower()
        self.record_log("adb_pair", f"{addr} → {'成功' if ok else '失败'}: {self._short(text, 160)}",
                        status="ok" if ok else "error", source="webui")
        if ok:
            self.config["adb_pair_host"] = host
            self.config["adb_pair_port"] = int(port) if str(port).isdigit() else port
            return {"ok": True, "message": text or f"配对成功：{addr}", "address": addr}
        if "unknown command" in text.lower() or "unrecognized" in text.lower():
            text += "\n（当前 adb 不支持 pair，请升级 platform-tools 到 30.0.0 以上）"
        return {"ok": False, "message": text or f"配对失败：{addr}", "address": addr}

    async def adb_connect(self, host: str, port: Any) -> Dict[str, Any]:
        """连接设备：`adb connect <host>:<port>`，并等待设备进入 device 状态"""
        host, port = self._split_host_port(host, port)
        await self.resolve_adb()
        addr = f"{host}:{port}"
        rc, out, err = await self._adb("connect", addr, use_serial=False, timeout=30)
        text = (out.decode("utf-8", errors="replace") + err.decode("utf-8", errors="replace")).strip()
        lowered = text.lower()
        ok = rc == 0 and ("connected to" in lowered or "already connected" in lowered)
        self.record_log("adb_connect", f"{addr} → {'成功' if ok else '失败'}: {self._short(text, 160)}",
                        status="ok" if ok else "error", source="webui")
        if not ok:
            return {"ok": False, "message": text or f"连接失败：{addr}", "address": addr}

        state = await self._wait_for_device(addr, timeout=10.0)
        self.config["adb_host"] = host
        self.config["adb_port"] = int(port) if str(port).isdigit() else port
        if state == "device":
            self._serial = addr
            return {"ok": True, "message": f"已连接 {addr}", "address": addr, "serial": addr, "state": state}
        if state == "unauthorized":
            return {
                "ok": False,
                "message": f"设备 {addr} 处于未授权状态：请在手机屏幕上点击【允许 USB 调试】并勾选「一律允许」。",
                "address": addr,
                "state": state,
            }
        return {
            "ok": False,
            "message": f"已下发 adb connect {addr}，但设备未进入就绪状态（当前: {state or '未出现'}）。"
                       f"请确认手机无线调试已开启、端口正确（配对端口 ≠ 连接端口）。",
            "address": addr,
            "state": state,
        }

    async def _wait_for_device(self, serial: str, timeout: float = 10.0) -> str:
        """轮询等待指定序列号进入 device 状态，返回最终 state"""
        deadline = time.time() + max(1.0, timeout)
        last_state = ""
        while time.time() < deadline:
            try:
                devs = await self.devices(refresh=True)
            except Exception:
                devs = []
            for d in devs:
                if d.get("serial") == serial:
                    last_state = d.get("state", "")
                    break
            if last_state == "device":
                return "device"
            if last_state in ("unauthorized", "offline"):
                # 再给一点时间，某些机型短暂 offline
                await asyncio.sleep(0.5)
                continue
            await asyncio.sleep(0.5)
        return last_state

    async def adb_disconnect(self, serial: str = "") -> Dict[str, Any]:
        """断开设备连接（TCP 设备用 `adb disconnect`，USB 设备仅从插件侧解绑）"""
        await self.resolve_adb()
        target = (serial or self._serial or "").strip()
        if not target:
            return {"ok": False, "message": "当前没有已连接的设备序列号"}
        is_tcp = ":" in target
        if is_tcp:
            rc, out, err = await self._adb("disconnect", target, use_serial=False, timeout=20)
            text = (out.decode("utf-8", errors="replace") + err.decode("utf-8", errors="replace")).strip()
            ok = rc == 0
            self.record_log("adb_disconnect", f"{target}: {self._short(text, 160)}",
                            status="ok" if ok else "error", source="webui")
            if self._serial == target:
                self._serial = ""
            return {"ok": ok, "message": text or f"已断开 {target}", "serial": target}
        # USB 设备：adb 不支持 disconnect，只解除插件侧的绑定
        if self._serial == target:
            self._serial = ""
        self.record_log("adb_disconnect", f"解除 USB 设备绑定: {target}", source="webui")
        return {"ok": True, "message": f"已解除设备绑定: {target}（USB 设备保持物理连接）", "serial": target}

    async def connect(self, source: str = "manual") -> Dict[str, Any]:
        """建立 Android 连接：选定设备 → 读取设备信息 → 就绪"""
        async with self._lock:
            self._last_error = None
            try:
                await self.resolve_adb()
                serial_cfg = str(self.config.get("adb_serial") or "").strip()
                chosen: Optional[Dict[str, str]] = None
                devs = await self.devices(refresh=True)

                if serial_cfg:
                    for d in devs:
                        if d.get("serial") == serial_cfg and d.get("state") == "device":
                            chosen = d
                            break
                    if chosen is None and ":" in serial_cfg:
                        res = await self.adb_connect(*self._split_host_port(serial_cfg, None))
                        if not res.get("ok"):
                            raise RuntimeError(res.get("message") or "连接失败")
                        chosen = {"serial": serial_cfg, "state": "device"}

                if chosen is None:
                    ready = [d for d in devs if d.get("state") == "device"]
                    if ready:
                        chosen = ready[0]

                if chosen is None:
                    host = str(self.config.get("adb_host") or "127.0.0.1").strip()
                    port = self.config.get("adb_port") or 5555
                    res = await self.adb_connect(host, port)
                    if res.get("ok"):
                        chosen = {"serial": res.get("serial") or res.get("address", ""), "state": "device"}
                    else:
                        unauthorized = [d for d in devs if d.get("state") == "unauthorized"]
                        hint = ""
                        if unauthorized:
                            hint = "（检测到未授权设备，请在手机屏幕上允许 USB 调试）"
                        raise RuntimeError((res.get("message") or "未发现 Android 设备") + hint)

                self._serial = chosen.get("serial", "")
                await self._load_device_info()
                self._connected = True
                self.touch_activity()
                self._ensure_idle_monitor()
                # 低延迟流模式：后台拉起，不阻塞连接返回
                try:
                    self._stream_task = asyncio.get_running_loop().create_task(self._maybe_start_stream())
                except RuntimeError:
                    self._stream_task = None
                w, h = self._capture_size if self._capture_size[0] else self._wm_size
                self.record_log(
                    "connect",
                    f"Android 设备已连接: {self._serial} ({self._manufacturer} {self._model}, "
                    f"Android {self._android_version}, {w}x{h})",
                    source=source,
                )
            except Exception as e:  # noqa: BLE001
                self._connected = False
                self._last_error = str(e)
                self.record_log("connect", f"Android 连接失败: {e}", status="error", source=source)

        return await self.get_status(probe=False)

    async def _load_device_info(self):
        """读取型号 / 厂商 / Android 版本 / wm size"""
        try:
            code, out, _err = await self._adb_shell(
                "getprop ro.product.manufacturer; getprop ro.product.model; "
                "getprop ro.build.version.release; wm size",
                timeout=20,
            )
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if len(lines) >= 3:
                self._manufacturer = lines[0]
                self._model = lines[1]
                self._android_version = lines[2]
            for ln in lines:
                m = re.search(r"(?:Physical|Override) size:\s*(\d+)\s*x\s*(\d+)", ln)
                if m:
                    self._wm_size = (int(m.group(1)), int(m.group(2)))
                    if self._device_size[0] == 0:
                        self._device_size = self._wm_size
                        self._capture_size = self._wm_size
                        self._scale = (1.0, 1.0)
                    break
        except Exception as e:  # noqa: BLE001
            self.record_log("device_info", f"读取设备信息失败: {e}", status="error", source="system")

    async def disconnect(self, reason: str = "手动断开 Android 连接", source: str = "manual") -> Dict[str, Any]:
        """断开插件与 Android 设备的会话（不强制 adb disconnect，除非是 TCP 设备且调用方要求）"""
        was_connected = self._connected
        self._connected = False
        self.last_active_time = 0.0
        self._last_error = None
        self._last_screenshot_cache = None
        # 关闭视频流
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
        self._stream_task = None
        await self._stop_stream()
        # 设备重启/换机后需要重新推送编码器
        self._dex_ready = False
        self._jpeg_failures = 0
        if self._idle_task and not self._idle_task.done() and asyncio.current_task() is not self._idle_task:
            self._idle_task.cancel()
        self._idle_task = None
        if was_connected or source == "manual":
            self.record_log("disconnect", reason, source=source)
        return await self.get_status(probe=False)

    # =========================================================================
    #  状态
    # =========================================================================

    def describe(self) -> Dict[str, Any]:
        """给 WebUI / 状态接口用的 Android 环境快照（不发起 adb 调用）"""
        dev_w, dev_h = self._device_size if self._device_size[0] else self._wm_size
        cap_w, cap_h = self._capture_size if self._capture_size[0] else (dev_w, dev_h)
        return {
            "adb_available": bool(self._adb_cmd),
            "adb_path": " ".join(self._adb_cmd) if self._adb_cmd else "",
            "adb_path_config": str(self.config.get("adb_path") or ""),
            "adb_version": self._adb_version,
            "adb_source": self._adb_source,
            "adb_error": self._adb_error if not self._adb_cmd else "",
            "platform": sys.platform,
            "bundled_adb": {
                "path": self.bundled_adb_path(),
                "exists": os.path.isfile(self.bundled_adb_path()),
                "dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform-tools"),
            },
            "serial": self._serial,
            "model": self._model,
            "manufacturer": self._manufacturer,
            "android_version": self._android_version,
            "devices": list(self._devices_cache),
            "devices_checked_at": self._devices_cache_time,
            "host": self.config.get("adb_host", "127.0.0.1"),
            "port": self.config.get("adb_port", 5555),
            "pair_host": self.config.get("adb_pair_host", "127.0.0.1"),
            "pair_port": self.config.get("adb_pair_port", 5555),
            "text_mode": self.config.get("adb_text_mode", "auto"),
            "adbkeyboard": self._ime_cache,
            "idle_timeout": self.idle_timeout,
            "device_screen": {"width": dev_w, "height": dev_h},
            "capture_screen": {"width": cap_w, "height": cap_h},
            "scale": {"x": self._scale[0], "y": self._scale[1]},
            "stream": (
                dict(self._stream.describe(), reason=self._stream_reason, **self._stream_settings(),
                     ffmpeg_variant=self._stream_ffmpeg_variant, size_used=self._stream_size_used)
                if self._stream is not None
                else dict({"running": False, "frames": 0, "age_ms": None},
                          reason=self._stream_reason, **self._stream_settings(),
                          ffmpeg_variant=self._stream_ffmpeg_variant, size_used=self._stream_size_used)
            ),
            "jpeg": {
                "mode": str(self.config.get("adb_jpeg_mode", "auto") or "auto"),
                "quality": int(self.config.get("adb_jpeg_quality", 70) or 70),
                "dex_ready": self._dex_ready,
                "reason": self._jpeg_reason,
                "variant": self._jpeg_variant,
                "app_process": self._app_process,
            },
        }

    async def get_status(self, probe: bool = False) -> Dict[str, Any]:
        """获取 Android 连接状态（未连接时也会带出 adb 可用性与设备列表）"""
        await self.check_idle_timeout()

        # 设备列表：连接中或首次查询时刷新（供设置页「配对与连接」使用）
        try:
            await self.devices(refresh=False)
        except Exception as e:  # noqa: BLE001
            if not self._adb_error:
                self._adb_error = str(e)

        if probe and not self._connected:
            return await self.connect(source="probe")

        idle_remaining = None
        if self._connected and self.idle_timeout > 0 and self.last_active_time > 0:
            idle_remaining = max(0, int(self.idle_timeout - (time.time() - self.last_active_time)))

        cap_w, cap_h = self._capture_size if self._capture_size[0] else self._wm_size
        status: Dict[str, Any] = {
            "mode": "android",
            "target": "android",
            "is_locked": self.is_locked,
            "connected": self._connected,
            "screen": {"width": cap_w, "height": cap_h} if self._connected else {"width": 0, "height": 0},
            "platform": "android",
            "agent_version": "adb",
            "ssh_tunnel": False,
            "idle_timeout": self.idle_timeout,
            "idle_remaining": idle_remaining,
            "error": self._last_error,
            "serial": self._serial,
            "device": {
                "manufacturer": self._manufacturer,
                "model": self._model,
                "android_version": self._android_version,
                "serial": self._serial,
                "device_screen": {"width": self._device_size[0] or self._wm_size[0],
                                  "height": self._device_size[1] or self._wm_size[1]},
            },
            "android": self.describe(),
        }

        if not self._connected:
            return status

        # 轻量健康检查：get-state 应为 device
        try:
            code, out, err = await self._adb("get-state", timeout=6)
            state = out.decode("utf-8", errors="replace").strip()
            if code != 0 or state != "device":
                raise RuntimeError(state or err.decode("utf-8", errors="replace").strip() or "设备已离线")
        except Exception as e:  # noqa: BLE001
            # 反向隧道刚重连时 adb 会短暂 offline：先自动 adb connect 一次，能救回来就不中断会话
            recovered = False
            try:
                host = str(self.config.get("adb_host") or "127.0.0.1").strip()
                port = self.config.get("adb_port") or 5555
                res = await self.adb_connect(host, port)
                if res.get("ok"):
                    self._serial = res.get("serial") or self._serial
                    code2, out2, _e2 = await self._adb("get-state", timeout=6)
                    if out2.decode("utf-8", errors="replace").strip() == "device":
                        recovered = True
            except Exception:
                pass
            if recovered:
                self._last_error = None
                self.record_log(
                    "reconnect",
                    "健康检查失败后自动 adb connect 成功，会话已恢复（隧道短暂重连）",
                    source="health_check",
                )
            else:
                self._last_error = str(e)
                await self.disconnect(reason=f"Android 连接中断: {e}", source="health_check")
                status["connected"] = False
                status["error"] = str(e)
                status["screen"] = {"width": 0, "height": 0}

        return status

    # =========================================================================
    #  截图
    # =========================================================================

    async def _capture_png(self) -> bytes:
        """抓取一帧 PNG 截图（exec-out 优先，失败回退到 pull 文件）"""
        try:
            code, out, err = await self._adb("exec-out", "screencap", "-p", timeout=25)
        except TimeoutError:
            code, out, err = -1, b"", b"screencap timeout"

        if out.startswith(b"\x89PNG\r\n\x1a\n"):
            return out
        # 某些 adb 版本会在二进制流里插入 CRLF
        if b"\r\n" in out:
            repaired = out.replace(b"\r\n", b"\n")
            if repaired.startswith(b"\x89PNG\r\n\x1a\n"):
                return repaired

        # 回退：先在设备侧落盘再 pull
        remote = "/sdcard/.fisheyes_screencap.png"
        await self._adb_shell(f"screencap -p {remote}", timeout=25)
        fd, local = tempfile.mkstemp(prefix="fisheyes_adb_", suffix=".png")
        os.close(fd)
        try:
            rc, out2, err2 = await self._adb("pull", remote, local, timeout=40)
            await self._adb_shell(f"rm -f {remote}", timeout=10)
            if os.path.isfile(local):
                with open(local, "rb") as f:
                    data = f.read()
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    return data
            raise RuntimeError(
                "Android 截图失败: " + self._short(
                    (err or b"").decode("utf-8", errors="replace")
                    + (err2 or b"").decode("utf-8", errors="replace"), 200)
            )
        finally:
            try:
                os.remove(local)
            except Exception:
                pass

    # =========================================================================
    #  设备端 JPEG（参考 PC 端 Agent：在被控侧压缩后再传）
    # =========================================================================

    def _device_dex_local(self) -> str:
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tools", "android", "fisheyes_jpeg.dex")

    async def _ensure_device_dex(self) -> bool:
        """把设备端 JPEG 编码器（~3 KB dex）推到手机 /data/local/tmp，一次即可"""
        if self._dex_ready:
            return True
        local = self._device_dex_local()
        if not os.path.isfile(local):
            self._jpeg_reason = "插件缺少 tools/android/fisheyes_jpeg.dex"
            return False
        want = os.path.getsize(local)
        try:
            await self._adb("push", local, DEVICE_DEX_REMOTE, timeout=40)
        except Exception as e:  # noqa: BLE001
            self._jpeg_reason = f"编码器 push 失败: {e}"
            return False

        # 远端大小校验：wc -c 优先，stat -c 兜底；都不可用时只记日志、不阻断
        remote_size = ""
        for probe in (f"wc -c < {DEVICE_DEX_REMOTE}", f"stat -c %s {DEVICE_DEX_REMOTE}"):
            try:
                _code, out, _err = await self._adb_shell(probe)
                text = (out or "").strip()
                if text.isdigit():
                    remote_size = text
                    break
            except Exception:
                continue
        if remote_size and int(remote_size) != want:
            self._jpeg_reason = f"编码器大小不一致（远端 {remote_size} / 本地 {want}）"
            return False
        if not remote_size:
            self.record_log("jpeg", "无法校验设备端编码器大小，直接尝试编码", status="error", source="system")
        self._dex_ready = True
        self._jpeg_reason = ""
        return True

    def _app_process_choices(self) -> List[str]:
        """不同 ROM 的 app_process 路径/名字不一样，按可能性排列"""
        base = ["/system/bin/app_process", "/system/bin/app_process64", "/system/bin/app_process32"]
        if self._app_process and self._app_process in base:
            base = [self._app_process] + [p for p in base if p != self._app_process]
        return base

    def _jpeg_variants(self, max_w: int, q: int) -> List[Tuple[str, str, str]]:
        """返回 (kind, app_process, 命令) 列表：管道优先，文件式兜底"""
        variants: List[Tuple[str, str, str]] = []
        for app in self._app_process_choices():
            variants.append((
                "pipe",
                app,
                f"screencap -p | CLASSPATH={DEVICE_DEX_REMOTE} {app} /system/bin "
                f"com.fisheyes.JpegEncoder {max_w} {q}",
            ))
        for app in self._app_process_choices()[:2]:
            variants.append((
                "file",
                app,
                f"screencap -p {DEVICE_TMP_PNG} && CLASSPATH={DEVICE_DEX_REMOTE} {app} /system/bin "
                f"com.fisheyes.JpegEncoder {max_w} {q} {DEVICE_TMP_PNG} > {DEVICE_TMP_JPG} && "
                f"cat {DEVICE_TMP_JPG}; rm -f {DEVICE_TMP_PNG} {DEVICE_TMP_JPG}",
            ))
        # 上次成功的组合放最前，避免每帧都重试失败项
        if self._jpeg_variant and self._app_process:
            variants.sort(key=lambda v: 0 if (v[0] == self._jpeg_variant and v[1] == self._app_process) else 1)
        return variants

    async def _capture_device_jpeg(self, quality: int) -> Optional[bytes]:
        """
        在**手机侧**完成 screencap → 缩放 → JPEG 压缩，只把 JPEG 传回来。

        等价于 PC 端 Agent 的 `/screenshot?quality=` 行为：
        PNG 是 0.5~2 MB，设备端压成 JPEG 后通常 80~200 KB。
        会依次尝试 app_process64/app_process32/文件式写法，兼容不同 ROM。
        """
        mode = str(self.config.get("adb_jpeg_mode", "auto") or "auto").strip().lower()
        if mode == "off" or self._jpeg_failures >= 4:
            return None
        if not await self._ensure_device_dex():
            return None

        try:
            max_w = int(self.config.get("adb_screenshot_max_width", 720) or 0)
        except Exception:
            max_w = 720
        try:
            q = int(self.config.get("adb_jpeg_quality", 70) or 70)
        except Exception:
            q = 70
        q = max(10, min(100, q))

        errors: List[str] = []
        for kind, app, cmd in self._jpeg_variants(max_w, q):
            try:
                code, out, err = await self._adb("exec-out", cmd, timeout=45)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{kind}/{app}: {e}")
                continue
            if out.startswith(b"\xff\xd8"):
                self._jpeg_failures = 0
                self._jpeg_reason = ""
                if self._app_process != app or self._jpeg_variant != kind:
                    self._app_process = app
                    self._jpeg_variant = kind
                    self.record_log(
                        "jpeg",
                        f"设备端 JPEG 已启用（{kind} / {app}，q{q}）",
                        source="system",
                    )
                return out
            text = (err or b"").decode("utf-8", errors="replace").strip()
            errors.append(f"{kind}/{app}: {text[:120] or f'rc={code}, {len(out)}B 非 JPEG'}")

        self._jpeg_failures += 1
        self._jpeg_reason = self._short(" | ".join(errors[:3]), 220)
        if self._jpeg_failures >= 4:
            self.record_log(
                "jpeg",
                f"设备端 JPEG 连续失败，本次会话回退 PNG 截图：{self._jpeg_reason}",
                status="error",
                source="system",
            )
        return None

    async def _ffmpeg_version(self, ffmpeg: str) -> str:
        """读取服务器 ffmpeg 版本（apt 4.x 与 9.x 对 YUV range 的严格度不同）"""
        if not ffmpeg:
            return ""
        if self._ffmpeg_version_cache:
            return self._ffmpeg_version_cache
        try:
            _code, out, _err = await self._run([ffmpeg, "-version"], timeout=10)
            text = (out or b"").decode("utf-8", "replace").strip()
            self._ffmpeg_version_cache = text.splitlines()[0][:120] if text else ""
        except Exception:
            self._ffmpeg_version_cache = ""
        return self._ffmpeg_version_cache

    async def _probe_stream(self) -> Dict[str, Any]:
        """
        视频流自检：先跑 3 秒 screenrecord 看设备侧有没有吐字节，
        再跑 4 秒完整管道看 ffmpeg 能不能解出帧——用来区分「设备侧不输出」和「ffmpeg 解不了」。
        """
        cfg = self._stream_settings()
        info: Dict[str, Any] = {}
        ffmpeg = find_ffmpeg(cfg["ffmpeg"])
        info["ffmpeg"] = ffmpeg
        if not ffmpeg:
            info["error"] = "未找到 ffmpeg"
            return info
        try:
            prefix = await self._adb_prefix()
        except Exception as e:  # noqa: BLE001
            info["error"] = f"adb 不可用: {e}"
            return info

        # A) 设备侧 3 秒 screenrecord
        argv = build_screenrecord_argv(
            prefix, size=cfg["size"], bitrate=cfg["bitrate"], time_limit=3)
        t0 = time.time()
        try:
            code, out, err = await self._run(argv, timeout=25)
            info["screenrecord_ms"] = int((time.time() - t0) * 1000)
            info["screenrecord_bytes"] = len(out or b"")
            info["screenrecord_stderr"] = (err or b"").decode("utf-8", "replace").strip()[:300]
            info["h264_head"] = (out or b"")[:8].hex()
        except Exception as e:  # noqa: BLE001
            info["screenrecord_error"] = str(e)

        # B) 完整管道：逐个参数变体试，记录哪一组能出帧
        try:
            prefix2 = await self._adb_prefix()
            best: Dict[str, Any] = {"pipeline_frames": 0}
            for variant, size in [(1, cfg["size"]), (5, cfg["size"]), (3, cfg["size"]), (4, cfg["size"]), (1, "")]:
                stream = AndroidScreenStream(
                    adb_argv=build_screenrecord_argv(
                        prefix2, size=size, bitrate=cfg["bitrate"], time_limit=4),
                    decoder_argv=build_ffmpeg_argv(
                        ffmpeg, fps=cfg["fps"], quality=cfg["quality"], variant=variant),
                )
                await stream.start()
                t0 = time.time()
                while time.time() - t0 < 8 and stream.frame_count == 0 and stream.running:
                    await asyncio.sleep(0.2)
                await asyncio.sleep(0.3)
                info_v = {
                    "variant": variant,
                    "size": size or "默认",
                    "pipeline_frames": stream.frame_count,
                    "pipeline_error": self._short(stream.last_error or "", 200),
                }
                frame, _ts = stream.latest()
                if frame:
                    try:
                        w, h = self._jpeg_size(frame)
                        info_v["frame_size"] = f"{w}x{h}"
                        info_v["frame_bytes"] = len(frame)
                    except Exception:
                        pass
                await stream.stop()
                if stream.frame_count > 0:
                    best = info_v
                    break
                if not best.get("pipeline_error"):
                    best = info_v
            info.update(best)
        except Exception as e:  # noqa: BLE001
            info["pipeline_exception"] = str(e)
        return info

    async def adb_diagnostics(self) -> Dict[str, Any]:
        """
        诊断：把「一次 adb 命令的固定往返」与「PNG/JPEG 的传输量」拆开量出来，
        判断瓶颈是 RTT 还是带宽。结果同时给 WebUI / 聊天命令用。
        """
        out: Dict[str, Any] = {
            "serial": self._serial,
            "connected": self._connected,
            "adb": " ".join(self._adb_cmd),
            "app_process": self._app_process or "(未探测)",
            "jpeg_variant": self._jpeg_variant or "(未探测)",
            "dex_ready": self._dex_ready,
        }
        if not self._connected:
            out["error"] = "未连接 Android 设备"
            return out

        # 0) 视频流前置条件：ffmpeg 与 screenrecord
        ffmpeg = find_ffmpeg(str(self.config.get("adb_ffmpeg_path") or ""))
        out["ffmpeg"] = ffmpeg or ""
        out["ffmpeg_version"] = await self._ffmpeg_version(ffmpeg) if ffmpeg else ""
        try:
            _c, sr_out, _e = await self._adb_shell(
                "command -v screenrecord || ls /system/bin/screenrecord", timeout=15)
            out["screenrecord"] = (sr_out or "").strip() or "未找到"
        except Exception as e:  # noqa: BLE001
            out["screenrecord"] = f"探测失败: {e}"
        out["stream"] = dict(
            self._stream.describe() if self._stream is not None else {"running": False, "frames": 0},
            reason=self._stream_reason,
            **self._stream_settings(),
        )
        # 视频流没起来时，跑一次自检定位卡在哪一步
        if not out["stream"].get("running"):
            try:
                out["stream_probe"] = await self._probe_stream()
            except Exception as e:  # noqa: BLE001
                out["stream_probe"] = {"error": str(e)}

        # 1) 基线：一条最简单的 shell 命令，衡量 adb 固定往返
        samples: List[int] = []
        for _ in range(3):
            t0 = time.time()
            try:
                await self._adb_shell("echo ok", timeout=20)
                samples.append(int((time.time() - t0) * 1000))
            except Exception as e:  # noqa: BLE001
                out["baseline_error"] = str(e)
                break
        out["baseline_ms"] = min(samples) if samples else None
        out["baseline_samples"] = samples

        # 2) PNG：抓图 + 传输
        try:
            t0 = time.time()
            png = await self._capture_png()
            out["png"] = {"ok": True, "ms": int((time.time() - t0) * 1000), "bytes": len(png)}
        except Exception as e:  # noqa: BLE001
            out["png"] = {"ok": False, "error": str(e)[:200]}

        # 3) 设备端 JPEG：重置失败计数后单独试一次
        self._jpeg_failures = 0
        self._jpeg_reason = ""
        t0 = time.time()
        jpeg = await self._capture_device_jpeg(70)
        if jpeg:
            out["jpeg"] = {"ok": True, "ms": int((time.time() - t0) * 1000), "bytes": len(jpeg),
                           "variant": self._jpeg_variant, "app_process": self._app_process}
        else:
            out["jpeg"] = {"ok": False, "ms": int((time.time() - t0) * 1000),
                           "error": self._jpeg_reason or "未知原因"}

        # 4) 结论
        base = out.get("baseline_ms") or 0
        png = out.get("png") or {}
        png_ms = png.get("ms") or 0
        png_bytes = png.get("bytes") or 0
        jpeg = out.get("jpeg") or {}
        stream = out.get("stream") or {}

        # 估算链路速率（去掉固定往返）
        rate_kbps = 0
        if png.get("ok") and png_ms > base and png_bytes:
            rate_kbps = int(png_bytes * 8 / max(0.001, (png_ms - base) / 1000.0) / 1000)
        out["link_kbps"] = rate_kbps

        probe = out.get("stream_probe") or {}
        if not out.get("ffmpeg"):
            advice = (
                f"链路约 {rate_kbps} kbps、adb 基线 {base} ms，瓶颈是传输量。"
                "当前服务器没有 ffmpeg，所以只能走截图；装上即可启用 H.264 视频流"
                "（`pip install imageio-ffmpeg` 或 `apt install ffmpeg`），重连后延迟可降到 0.2~0.3s。"
            )
        elif self._stream_healthy(stream):
            advice = (
                f"视频流已启用（{stream.get('size')} @{int((stream.get('bitrate') or 0) / 1000)}kbps），"
                "延迟应已在 0.1~0.3s；若 WebUI 仍慢，看顶部 HUD 里的「服务端 xxx ms」判断是不是浏览器那段慢。"
            )
        elif probe and (probe.get("screenrecord_bytes") or 0) <= 0 and not probe.get("error"):
            advice = (
                "设备侧 screenrecord 跑 3 秒没有任何输出"
                f"（stderr: {probe.get('screenrecord_stderr') or '无'}）。"
                "这类 ROM 会限制 adb shell 下的 screenrecord，只能改用 App 内录屏（APK 方案）。"
            )
        elif probe and (probe.get("pipeline_frames") or 0) <= 0:
            advice = (
                f"设备侧已输出 {probe.get('screenrecord_bytes')} 字节 H.264"
                f"（头部 {probe.get('h264_head') or '-'}），"
                f"但 ffmpeg 没解出帧：{probe.get('pipeline_error') or probe.get('error') or '未知原因'}。"
                "把这段发我即可定位（多半是 ffmpeg 参数/版本问题）。"
            )
        elif probe and (probe.get("pipeline_frames") or 0) > 0:
            advice = (
                f"管线能出帧（{probe.get('pipeline_frames')} 帧，{probe.get('frame_size')}），"
                f"但常驻流没保持：{stream.get('reason') or '未知'}。重连一次通常即可。"
            )
        elif base >= 1200:
            advice = (
                f"adb 单次命令固定耗时 {base} ms，瓶颈是隧道往返而不是带宽；"
                "需要把「每帧一次 adb 调用」改成「手机 App 内 HTTP 截图服务 + 一次 adb forward」。"
            )
        elif jpeg.get("ok") and (jpeg.get("ms") or 0) < png_ms:
            advice = f"设备端 JPEG 已生效（{jpeg.get('ms')} ms / {jpeg.get('bytes', 0) // 1024} KB），比 PNG 快。"
        else:
            advice = (
                f"设备端 JPEG 已生效但没更快：省下的传输时间被手机侧编码吃掉了"
                f"（PNG {png_ms} ms → JPEG {jpeg.get('ms')} ms）。"
                "ffmpeg 已就绪，请确认「画面模式」为视频流；若显示截图模式，看括号里的 screenrecord 报错。"
            )
        out["advice"] = advice
        self._diag_cache = out
        return out

    def _jpeg_result(
        self,
        jpeg: bytes,
        mode: str,
        include_bytes: bool,
        age_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """把一张现成 JPEG 包装成截图结果（统一坐标换算）"""
        try:
            cap_w, cap_h = self._jpeg_size(jpeg)
        except Exception:
            return None
        dev_w, dev_h = self._device_size if self._device_size[0] else self._wm_size
        if not dev_w or not dev_h:
            dev_w, dev_h = cap_w, cap_h
        self._capture_size = (cap_w, cap_h)
        self._scale = (dev_w / float(cap_w) if cap_w else 1.0, dev_h / float(cap_h) if cap_h else 1.0)
        img_b64 = base64.b64encode(jpeg).decode("utf-8")
        result: Dict[str, Any] = {
            "base64": img_b64,
            "data_url": f"data:image/jpeg;base64,{img_b64}",
            "width": cap_w,
            "height": cap_h,
            "device_width": dev_w,
            "device_height": dev_h,
            "size_bytes": len(jpeg),
            "cost_ms": 0,
            "capture_ms": 0,
            "encode_ms": 0,
            "mode": mode,
            "platform": "android",
            "serial": self._serial,
        }
        if age_ms is not None:
            result["stream_age_ms"] = age_ms
        if include_bytes:
            result["bytes"] = jpeg
        return result

    def _encode_jpeg(self, png: bytes, quality: int) -> Tuple[bytes, int, int, int, int]:
        """PNG → JPEG（可选按宽度缩放），返回 (jpeg, 设备宽, 设备高, 输出宽, 输出高)"""
        max_w = 0
        try:
            max_w = int(self.config.get("adb_screenshot_max_width", 720) or 0)
        except Exception:
            max_w = 720
        with Image.open(io.BytesIO(png)) as im:
            im = im.convert("RGB")
            dev_w, dev_h = im.size
            if max_w and dev_w > max_w:
                cap_w = max_w
                cap_h = max(1, int(round(dev_h * max_w / float(dev_w))))
                # BILINEAR 比 LANCZOS 快 3~5 倍，缩放后的 JPEG 观感差异很小
                im = im.resize((cap_w, cap_h), Image.BILINEAR)
            else:
                cap_w, cap_h = dev_w, dev_h
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=max(10, min(100, int(quality))), optimize=False)
            return buf.getvalue(), dev_w, dev_h, cap_w, cap_h

    # =========================================================================
    #  低延迟视频流（可选）
    # =========================================================================

    async def _adb_prefix(self) -> List[str]:
        """返回带 -s <serial> 的 adb argv 前缀"""
        await self.resolve_adb()
        cmd = list(self._adb_cmd)
        if self._serial:
            cmd.extend(["-s", self._serial])
        return cmd

    def _stream_healthy(self, stream: Dict[str, Any]) -> bool:
        """只有「有帧且帧是新的」才算视频流真的在工作（不能只看任务存活）"""
        if not stream.get("running"):
            return False
        if not (stream.get("frames") or 0):
            return False
        age = stream.get("age_ms")
        return age is None or age <= 3000

    def _stream_settings(self) -> Dict[str, Any]:
        dev_w, dev_h = self._device_size if self._device_size[0] else self._wm_size
        size = str(self.config.get("adb_stream_size", "auto") or "auto").strip()
        if size.lower() == "auto":
            if dev_w and dev_h:
                # 默认取一半分辨率：1080x2400 → 540x1200，H.264 下肉眼几乎无损
                size = f"{even(max(2, dev_w // 2))}x{even(max(2, dev_h // 2))}"
            else:
                size = "540x1200"
        return {
            "mode": str(self.config.get("adb_stream_mode", "auto") or "auto").strip().lower(),
            "size": size,
            "bitrate": int(self.config.get("adb_stream_bitrate", 2000000) or 2000000),
            "fps": int(self.config.get("adb_stream_max_fps", 10) or 10),
            "quality": int(self.config.get("adb_stream_quality", 6) or 6),
            "ffmpeg": str(self.config.get("adb_ffmpeg_path") or ""),
        }

    def _stream_combos(self) -> List[Tuple[int, str]]:
        """(ffmpeg 参数变体, screenrecord 尺寸) 组合，上次成功的排最前"""
        cfg = self._stream_settings()
        combos: List[Tuple[int, str]] = [(v, cfg["size"]) for v in (1, 5, 3, 4, 2)]
        combos.append((1, ""))
        if self._stream_ffmpeg_variant and self._stream_size_used is not None:
            best = (self._stream_ffmpeg_variant, self._stream_size_used)
            combos = [best] + [c for c in combos if c != best]
        return combos

    async def _maybe_start_stream(self):
        """连接成功后尝试拉起 H.264 视频流（失败则静默回退到截图模式）"""
        await self._stop_stream()
        cfg = self._stream_settings()
        if cfg["mode"] == "off":
            self._stream_reason = "配置已关闭流模式（adb_stream_mode=off）"
            return
        ffmpeg = find_ffmpeg(cfg["ffmpeg"])
        if not ffmpeg:
            self._stream_reason = (
                "未找到 ffmpeg：流模式已跳过。安装 ffmpeg（apt install ffmpeg）"
                "或 pip install imageio-ffmpeg 后重连即可启用低延迟画面。"
            )
            self.record_log("stream", self._stream_reason, status="error", source="system")
            return
        try:
            prefix = await self._adb_prefix()
            # 逐个尝试 (ffmpeg 参数变体 × screenrecord 尺寸)，第一组出帧就固定下来
            errors: List[str] = []
            for variant, size in self._stream_combos():
                adb_argv = build_screenrecord_argv(
                    prefix, size=size, bitrate=cfg["bitrate"], time_limit=170)
                dec_argv = build_ffmpeg_argv(
                    ffmpeg, fps=cfg["fps"], quality=cfg["quality"], variant=variant)
                stream = AndroidScreenStream(adb_argv=adb_argv, decoder_argv=dec_argv)
                self._stream = stream
                await stream.start()
                deadline = time.time() + 8.0
                while time.time() < deadline:
                    frame, _ts = stream.latest()
                    if frame:
                        self._stream_reason = ""
                        self._stream_ffmpeg_variant = variant
                        self._stream_size_used = size
                        self.record_log(
                            "stream",
                            f"低延迟流模式已启用：V{variant} / {size or '设备默认'} "
                            f"@ {cfg['bitrate'] // 1000}kbps, {cfg['fps']}fps",
                            source="system",
                        )
                        return
                    if stream.disabled or not stream.running:
                        break
                    await asyncio.sleep(0.2)
                errors.append(
                    f"V{variant}/{size or '默认'}: {self._short(stream.last_error or '8 秒内无画面帧', 80)}")
                await self._stop_stream()
            # 全部失败：跑一次自检，把「设备侧有没有输出 / ffmpeg 有没有解出」写进原因
            try:
                probe = await self._probe_stream()
                if probe:
                    errors.append(
                        f"自检: screenrecord={probe.get('screenrecord_bytes')}B/{probe.get('screenrecord_ms')}ms, "
                        f"head={probe.get('h264_head') or '-'}, "
                        f"frames={probe.get('pipeline_frames')}, "
                        f"err={self._short(str(probe.get('pipeline_error') or probe.get('screenrecord_stderr') or probe.get('error') or ''), 160)}"
                    )
            except Exception:
                pass
            self._stream_reason = self._short(" | ".join(errors), 420)
            self.record_log(
                "stream",
                f"流模式启用失败，已回退截图模式：{self._stream_reason}",
                status="error",
                source="system",
            )
        except Exception as e:  # noqa: BLE001
            self._stream_reason = f"{type(e).__name__}: {e}"
            self.record_log("stream", f"流模式异常，已回退截图模式：{e}", status="error", source="system")
        finally:
            if not (self._stream and self._stream.running and self._stream.latest()[0]):
                await self._stop_stream()

    async def _stop_stream(self):
        if self._stream is not None:
            try:
                await self._stream.stop()
            except Exception:
                pass
            self._stream = None

    @staticmethod
    def _jpeg_size(data: bytes) -> Tuple[int, int]:
        with Image.open(io.BytesIO(data)) as im:
            return im.size

    async def take_screenshot(
        self,
        quality: Optional[int] = None,
        source: str = "system",
        include_bytes: bool = True,
    ) -> Dict[str, Any]:
        """抓取 Android 屏幕：视频流 > 设备端 JPEG > PNG 截图（逐级回退）"""
        await self._ensure_active_for_action(source=source)

        if quality is None:
            quality = int(self.config.get("screenshot_quality", 75) or 75)
        quality = max(10, min(100, int(quality)))
        t0 = time.time()

        # 1) 流模式：直接返回缓存的最新一帧（0 次 adb 调用）
        if self._stream is not None and self._stream.running:
            frame, ts = self._stream.latest()
            age = time.time() - ts if ts else 999.0
            if frame and age <= 3.0:
                res = await asyncio.get_running_loop().run_in_executor(
                    None, self._jpeg_result, frame, "stream", include_bytes, int(age * 1000))
                if res is not None:
                    res["cost_ms"] = int((time.time() - t0) * 1000)
                    if source != "webui_auto":
                        self.record_log(
                            "screenshot",
                            f"[流] {res['width']}x{res['height']} (设备 {res['device_width']}x{res['device_height']}), "
                            f"{res['size_bytes'] // 1024}KB, 帧龄 {res['stream_age_ms']}ms, 取帧 {res['cost_ms']}ms",
                            source=source,
                        )
                    return res

        # 2) 设备端 JPEG：手机侧 screencap → 缩放 → JPEG，隧道只传 ~100KB
        dev_jpeg = await self._capture_device_jpeg(quality)
        if dev_jpeg:
            res = await asyncio.get_running_loop().run_in_executor(
                None, self._jpeg_result, dev_jpeg, "device_jpeg", include_bytes)
            if res is not None:
                res["cost_ms"] = int((time.time() - t0) * 1000)
                res["capture_ms"] = res["cost_ms"]
                if source != "webui_auto":
                    self.record_log(
                        "screenshot",
                        f"[设备端JPEG] {res['width']}x{res['height']} "
                        f"(设备 {res['device_width']}x{res['device_height']}), "
                        f"{res['size_bytes'] // 1024}KB, {res['cost_ms']}ms",
                        source=source,
                    )
                return res

        # 3) 回退：PNG 截图（服务器侧压缩，弱网下最慢）
        cap_t0 = time.time()
        png = await self._capture_png()
        capture_ms = int((time.time() - cap_t0) * 1000)

        loop = asyncio.get_running_loop()
        enc_t0 = time.time()
        jpeg, dev_w, dev_h, cap_w, cap_h = await loop.run_in_executor(None, self._encode_jpeg, png, quality)
        encode_ms = int((time.time() - enc_t0) * 1000)

        self._device_size = (dev_w, dev_h)
        self._capture_size = (cap_w, cap_h)
        self._scale = (dev_w / float(cap_w) if cap_w else 1.0, dev_h / float(cap_h) if cap_h else 1.0)

        img_b64 = base64.b64encode(jpeg).decode("utf-8")
        cost = int((time.time() - t0) * 1000)
        result: Dict[str, Any] = {
            "base64": img_b64,
            "data_url": f"data:image/jpeg;base64,{img_b64}",
            "width": cap_w,
            "height": cap_h,
            "device_width": dev_w,
            "device_height": dev_h,
            "size_bytes": len(jpeg),
            "cost_ms": cost,
            "capture_ms": capture_ms,
            "encode_ms": encode_ms,
            "png_bytes": len(png),
            "mode": "screenshot",
            "platform": "android",
            "serial": self._serial,
        }
        if include_bytes:
            result["bytes"] = jpeg
            result["raw_bytes"] = png

        self._last_screenshot_cache = {
            "width": cap_w,
            "height": cap_h,
            "device_width": dev_w,
            "device_height": dev_h,
            "size_bytes": len(jpeg),
            "cost_ms": cost,
        }
        if source != "webui_auto":
            self.record_log(
                "screenshot",
                f"Android {self._serial}: {cap_w}x{cap_h} (设备 {dev_w}x{dev_h}), "
                f"PNG {len(png)//1024}KB → JPEG {len(jpeg)//1024}KB, "
                f"抓图 {capture_ms}ms + 编码 {encode_ms}ms = {cost}ms",
                source=source,
            )
        return result

    # =========================================================================
    #  坐标换算与触控
    # =========================================================================

    def _to_device(self, x: Any, y: Any) -> Tuple[int, int]:
        """把截图（可能已缩放）坐标换算为设备真实像素坐标"""
        try:
            fx = float(x)
            fy = float(y)
        except Exception:
            fx = fy = 0.0
        sx, sy = self._scale
        dev_w, dev_h = self._device_size if self._device_size[0] else self._wm_size
        dx = int(round(fx * sx))
        dy = int(round(fy * sy))
        if dev_w > 0:
            dx = max(0, min(dx, dev_w - 1))
        if dev_h > 0:
            dy = max(0, min(dy, dev_h - 1))
        return dx, dy

    def _device_size_or_default(self) -> Tuple[int, int]:
        if self._device_size[0]:
            return self._device_size
        if self._wm_size[0]:
            return self._wm_size
        return (1080, 1920)

    def _check_lock(self):
        if self.is_locked:
            raise PermissionError("屏幕控制已被紧急锁定！请在 WebUI 中解除锁定后再执行操作。")

    async def click(self, x: int, y: int, button: str = "left", clicks: int = 1, source: str = "system") -> Dict[str, Any]:
        """点击（右键 → 长按；clicks>=2 → 双击）"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        dx, dy = self._to_device(x, y)
        button = (button or "left").lower()
        if button == "right":
            return await self.long_press(dx, dy, 650, source=source, _already_ready=True)
        if int(clicks or 1) >= 2:
            return await self.double_click(dx, dy, source=source, _already_ready=True)
        code, out, err = await self._adb_shell(f"input tap {dx} {dy}", timeout=20)
        self._raise_if_input_failed(code, out, err, "点击")
        self.record_log("tap", f"设备坐标: ({dx}, {dy})", source=source)
        return {"status": "ok", "action": "click", "x": dx, "y": dy, "device": True}

    async def double_click(self, x: int, y: int, source: str = "system", _already_ready: bool = False) -> Dict[str, Any]:
        """双击（两次 tap，间隔 90ms）"""
        self._check_lock()
        if not _already_ready:
            await self._ensure_active_for_action(source=source)
            x, y = self._to_device(x, y)
        await self._adb_shell(f"input tap {x} {y}", timeout=20)
        await asyncio.sleep(0.09)
        code, out, err = await self._adb_shell(f"input tap {x} {y}", timeout=20)
        self._raise_if_input_failed(code, out, err, "双击")
        self.record_log("double_tap", f"设备坐标: ({x}, {y})", source=source)
        return {"status": "ok", "action": "double_click", "x": x, "y": y, "device": True}

    async def long_press(self, x: int, y: int, duration_ms: int = 700, source: str = "system",
                         _already_ready: bool = False) -> Dict[str, Any]:
        """长按（同点 swipe 实现）"""
        self._check_lock()
        if not _already_ready:
            await self._ensure_active_for_action(source=source)
            x, y = self._to_device(x, y)
        duration_ms = max(120, min(int(duration_ms or 700), 10000))
        code, out, err = await self._adb_shell(f"input swipe {x} {y} {x} {y} {duration_ms}", timeout=25)
        self._raise_if_input_failed(code, out, err, "长按")
        self.record_log("long_press", f"设备坐标: ({x}, {y}), {duration_ms}ms", source=source)
        return {"status": "ok", "action": "long_press", "x": x, "y": y, "duration_ms": duration_ms}

    async def right_click(self, x: int, y: int, source: str = "system") -> Dict[str, Any]:
        """Android 没有右键：映射为长按（弹出上下文菜单 / 进入编辑）"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        x, y = self._to_device(x, y)
        return await self.long_press(x, y, 700, source=source, _already_ready=True)

    async def move(self, x: int, y: int, duration: float = 0.0, source: str = "system") -> Dict[str, Any]:
        """Android 无鼠标悬停语义，保留接口以兼容上层调用"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        return {"status": "ok", "action": "move", "note": "Android 无鼠标移动语义，已忽略", "x": int(x), "y": int(y)}

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
                    source: str = "system") -> Dict[str, Any]:
        """滑动 / 拖拽（设备坐标）"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        ax, ay = self._to_device(x1, y1)
        bx, by = self._to_device(x2, y2)
        duration_ms = max(50, min(int(duration_ms or 300), 20000))
        code, out, err = await self._adb_shell(f"input swipe {ax} {ay} {bx} {by} {duration_ms}", timeout=30)
        self._raise_if_input_failed(code, out, err, "滑动")
        self.record_log("swipe", f"({ax},{ay}) → ({bx},{by}), {duration_ms}ms", source=source)
        return {"status": "ok", "action": "swipe", "from": [ax, ay], "to": [bx, by], "duration_ms": duration_ms}

    async def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5,
                   button: str = "left", source: str = "system") -> Dict[str, Any]:
        """拖拽（= 滑动，时长以毫秒计）"""
        return await self.swipe(x1, y1, x2, y2, int(float(duration or 0.5) * 1000), source=source)

    async def swipe_direction(self, direction: str, amount: int = 3, source: str = "system") -> Dict[str, Any]:
        """按方向滑动（在屏幕中心区域，用于翻页 / 回到桌面手势等）"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        w, h = self._device_size_or_default()
        cx, cy = w // 2, h // 2
        ratio = min(0.45, max(0.12, 0.10 + 0.05 * max(1, int(amount or 3))))
        dx = int(w * ratio)
        dy = int(h * ratio)
        d = (direction or "up").lower()
        if d in ("up", "上", "上滑"):
            x1, y1, x2, y2 = cx, cy + dy, cx, cy - dy
        elif d in ("down", "下", "下滑"):
            x1, y1, x2, y2 = cx, cy - dy, cx, cy + dy
        elif d in ("left", "左", "左滑"):
            x1, y1, x2, y2 = cx + dx, cy, cx - dx, cy
        elif d in ("right", "右", "右滑"):
            x1, y1, x2, y2 = cx - dx, cy, cx + dx, cy
        else:
            raise ValueError(f"未知滑动方向: {direction}（可选 up/down/left/right）")
        duration = 220 if d in ("left", "right", "左", "右", "左滑", "右滑") else 280
        code, out, err = await self._adb_shell(f"input swipe {x1} {y1} {x2} {y2} {duration}", timeout=30)
        self._raise_if_input_failed(code, out, err, "滑动")
        self.record_log("swipe", f"方向 {d}: ({x1},{y1}) → ({x2},{y2})", source=source)
        return {"status": "ok", "action": "swipe", "direction": d,
                "from": [x1, y1], "to": [x2, y2], "duration_ms": duration}

    async def scroll(self, direction: str = "down", amount: int = 3,
                     x: Optional[int] = None, y: Optional[int] = None, source: str = "system") -> Dict[str, Any]:
        """滚动 = 反向滑动（down 表示内容向上翻，等价于手指上滑）"""
        d = (direction or "down").lower()
        if d in ("down", "下"):
            return await self.swipe_direction("up", amount=amount, source=source)
        if d in ("up", "上"):
            return await self.swipe_direction("down", amount=amount, source=source)
        return await self.swipe_direction(d, amount=amount, source=source)

    # =========================================================================
    #  文本 / 按键
    # =========================================================================

    async def _has_adb_keyboard(self) -> bool:
        """检测设备是否安装并启用了 ADBKeyboard 输入法"""
        if self._ime_cache is not None:
            return self._ime_cache
        try:
            code, out, err = await self._adb_shell("ime list -s", timeout=15)
            text = (out + err).lower()
            self._ime_cache = "adbkeyboard" in text
        except Exception:
            self._ime_cache = False
        return self._ime_cache

    async def _input_text_via_adb_keyboard(self, text: str) -> bool:
        """通过 ADBKeyboard 广播注入文本（支持中文 / emoji）"""
        if not await self._has_adb_keyboard():
            return False
        try:
            await self._adb_shell(f"ime enable {ADB_KEYBOARD_IME}", timeout=15)
            await self._adb_shell(f"ime set {ADB_KEYBOARD_IME}", timeout=15)
            b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            code, out, err = await self._adb_shell(f"am broadcast -a ADB_INPUT_B64 --es msg {b64}", timeout=20)
            combined = (out + err).lower()
            if "broadcast completed" in combined or code == 0:
                return True
            return False
        except Exception:
            return False

    async def _input_text_via_clipboard(self, text: str) -> bool:
        """剪贴板兜底：cmd clipboard set text → KEYCODE_PASTE"""
        try:
            code, out, err = await self._adb_shell(f"cmd clipboard set text {_shell_quote(text)}", timeout=20)
            if code != 0 or "error" in (out + err).lower() or "unknown" in (out + err).lower():
                return False
            await self._adb_shell("input keyevent 279", timeout=15)
            return True
        except Exception:
            return False

    async def type_text(self, text: str, interval: float = 0.0, source: str = "system") -> Dict[str, Any]:
        """向 Android 输入文本（ASCII 走 input text，中文优先 ADBKeyboard，其次剪贴板粘贴）"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        text = str(text or "")
        if not text:
            return {"status": "ok", "action": "type", "length": 0}

        mode = str(self.config.get("adb_text_mode", "auto") or "auto").lower()
        needs_unicode = (not text.isascii()) or ("%" in text)
        method = "input"

        if mode == "ascii" or (mode == "auto" and not needs_unicode):
            escaped = _escape_input_text(text)
            code, out, err = await self._adb_shell(f"input text {escaped}", timeout=30)
            self._raise_if_input_failed(code, out, err, "文本输入")
            method = "input_text"
        elif mode in ("auto", "adbkeyboard"):
            if await self._input_text_via_adb_keyboard(text):
                method = "adbkeyboard"
            elif await self._input_text_via_clipboard(text):
                method = "clipboard"
            else:
                raise RuntimeError(
                    "无法输入非 ASCII 文本：当前设备未安装 ADBKeyboard，且剪贴板注入失败。\n"
                    "请在手机上安装 ADBKeyboard（开源输入法）后重试，或把插件设置【Android 文本输入方式】改为 ascii 只输入英文数字。"
                )
        else:  # clipboard
            if not await self._input_text_via_clipboard(text):
                raise RuntimeError("剪贴板注入失败：该 ROM 可能移除了 `cmd clipboard`，请改用 ADBKeyboard 或 ascii 模式。")
            method = "clipboard"

        self.record_log("type", f"输入 {len(text)} 字符（{method}）", source=source)
        return {"status": "ok", "action": "type", "length": len(text), "method": method}

    def _keycode(self, key: Any) -> str:
        """键名 / KEYCODE 名 / 数字 → keyevent 参数"""
        raw = str(key or "").strip()
        if not raw:
            raise ValueError("按键参数为空")
        if raw.isdigit():
            return raw
        low = raw.lower()
        if low.startswith("keycode_"):
            return low
        mapped = PC_TO_ANDROID_KEY.get(low, low)
        if mapped in ANDROID_KEYCODES:
            return str(ANDROID_KEYCODES[mapped])
        if mapped in ANDROID_KEYCODES:
            return str(ANDROID_KEYCODES[mapped])
        # 单字符 → 字母/数字 keyevent
        if len(raw) == 1 and raw.isalnum():
            ch = raw.upper()
            if ch.isalpha():
                return str(29 + (ord(ch) - ord("A")))
            if ch.isdigit():
                return str(7 + (ord(ch) - ord("0")))
        raise ValueError(f"无法识别的按键: {key}（可用 enter/back/home/recents/volume_up/up/down/left/right 或 KEYCODE_XXX）")

    async def press_key(self, key: str, source: str = "system") -> Dict[str, Any]:
        """按下单个按键"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        code = self._keycode(key)
        rc, out, err = await self._adb_shell(f"input keyevent {code}", timeout=20)
        self._raise_if_input_failed(rc, out, err, "按键")
        self.record_log("keyevent", f"{key} → {code}", source=source)
        return {"status": "ok", "action": "press", "key": key, "keycode": code}

    async def hotkey(self, keys: List[str], source: str = "system") -> Dict[str, Any]:
        """组合键：Android 11+ 用 `input keycombination`，旧版本退化为顺序按键"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        codes = [self._keycode(k) for k in (keys or [])]
        if not codes:
            raise ValueError("组合键参数为空")
        if len(codes) == 1:
            await self._adb_shell(f"input keyevent {codes[0]}", timeout=20)
            self.record_log("keyevent", f"{keys[0]} → {codes[0]}", source=source)
            return {"status": "ok", "action": "hotkey", "keys": keys, "mode": "keyevent"}

        rc, out, err = await self._adb_shell(f"input keycombination {' '.join(codes)}", timeout=25)
        combined = (out + err).lower()
        if rc == 0 and "unknown" not in combined and "error" not in combined:
            self.record_log("keycombination", f"{' + '.join(map(str, keys))} → {codes}", source=source)
            return {"status": "ok", "action": "hotkey", "keys": keys, "mode": "keycombination"}

        for c in codes:
            await self._adb_shell(f"input keyevent {c}", timeout=20)
            await asyncio.sleep(0.03)
        self.record_log("hotkey_fallback", f"{' + '.join(map(str, keys))} 顺序按键 {codes}", source=source)
        return {"status": "ok", "action": "hotkey", "keys": keys, "mode": "sequential"}

    # =========================================================================
    #  应用与 Shell
    # =========================================================================

    async def list_packages(self, third_party: bool = True, keyword: str = "") -> List[str]:
        """列出已安装应用包名"""
        await self.resolve_adb()
        if not self._connected:
            raise ConnectionError("请先连接 Android 设备")
        flag = "-3" if third_party else ""
        code, out, err = await self._adb_shell(f"pm list packages {flag}".strip(), timeout=40)
        packages = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkg = line[len("package:"):].strip()
                if pkg and (not keyword or keyword.lower() in pkg.lower()):
                    packages.append(pkg)
        return sorted(set(packages))

    async def launch_app(self, package: str, source: str = "system") -> Dict[str, Any]:
        """启动应用（monkey 优先，失败回退 am start）"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        package = str(package or "").strip()
        if not package:
            raise ValueError("请提供要启动的应用包名，例如 com.tencent.mm")
        if "/" in package:
            code, out, err = await self._adb_shell(f"am start -n {package}", timeout=25)
            self._raise_if_input_failed(code, out, err, "启动 Activity")
            self.record_log("launch", f"Activity: {package}", source=source)
            return {"status": "ok", "action": "launch_app", "component": package}

        code, out, err = await self._adb_shell(
            f"monkey -p {package} -c android.intent.category.LAUNCHER 1", timeout=30)
        combined = (out + err)
        if "No activities found" in combined or code != 0:
            rc, out2, err2 = await self._adb_shell(
                f"cmd package resolve-activity --brief {package}", timeout=25)
            component = ""
            for line in out2.splitlines():
                line = line.strip()
                if "/" in line:
                    component = line
            if component:
                await self._adb_shell(f"am start -n {component}", timeout=25)
                self.record_log("launch", f"{package} → {component}", source=source)
                return {"status": "ok", "action": "launch_app", "package": package, "component": component}
            raise RuntimeError(f"启动应用失败: {self._short(combined + err2, 300)}")
        self.record_log("launch", f"包名: {package}", source=source)
        return {"status": "ok", "action": "launch_app", "package": package}

    async def stop_app(self, package: str, source: str = "system") -> Dict[str, Any]:
        """强制停止应用"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        package = str(package or "").strip()
        if not package:
            raise ValueError("请提供要停止的应用包名")
        code, out, err = await self._adb_shell(f"am force-stop {package}", timeout=20)
        self._raise_if_input_failed(code, out, err, "停止应用")
        self.record_log("stop_app", f"包名: {package}", source=source)
        return {"status": "ok", "action": "stop_app", "package": package}

    async def current_app(self) -> str:
        """当前前台应用（尽力而为）"""
        try:
            code, out, _err = await self._adb_shell(
                "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'", timeout=20)
            return self._short(out.strip(), 400)
        except Exception as e:  # noqa: BLE001
            return f"读取失败: {e}"

    async def install_apk(self, apk_path: str, source: str = "system") -> Dict[str, Any]:
        """安装 APK（服务器本地路径）"""
        await self._ensure_active_for_action(source=source)
        apk_path = str(apk_path or "").strip()
        if not apk_path or not os.path.isfile(apk_path):
            raise FileNotFoundError(f"APK 文件不存在: {apk_path}")
        await self.resolve_adb()
        code, out, err = await self._adb("install", "-r", apk_path, timeout=240)
        text = (out.decode("utf-8", errors="replace") + err.decode("utf-8", errors="replace")).strip()
        ok = code == 0 and "success" in text.lower()
        self.record_log("install_apk", f"{os.path.basename(apk_path)} → {'成功' if ok else '失败'}",
                        status="ok" if ok else "error", source=source)
        return {"status": "ok" if ok else "error", "action": "install_apk",
                "apk": apk_path, "message": self._short(text, 500)}

    async def shell(self, command: str, source: str = "system") -> Dict[str, Any]:
        """执行任意 adb shell 命令"""
        await self._ensure_active_for_action(source=source)
        command = str(command or "").strip()
        if not command:
            raise ValueError("命令不能为空")
        code, out, err = await self._adb_shell(command, timeout=self.config.get("adb_shell_timeout", 60) or 60)
        self.record_log("adb_shell", f"$ {self._short(command, 120)} → rc={code}", source=source)
        return {
            "status": "ok" if code == 0 else "error",
            "action": "shell",
            "command": command,
            "code": code,
            "stdout": out,
            "stderr": err,
        }

    # =========================================================================
    #  连接前置检查 / 错误处理
    # =========================================================================

    async def _ensure_active_for_action(self, source: str = "system"):
        """截图 / 操作前的连接检查：Bot 来源可自动连接一次，WebUI 未连接则提示先连接"""
        await self.check_idle_timeout()
        if not self._connected:
            if self._is_bot_source(source):
                self.record_log("auto_connect", "未连接状态下 Bot 请求 Android 操作，自动尝试一次连接...", source=source)
                status = await self.connect(source=source)
                if not status.get("connected"):
                    raise ConnectionError(
                        f"Bot 自动连接 Android 失败: {status.get('error') or '请检查手机无线调试 / SshAdbTunnel 隧道是否就绪'}"
                    )
            else:
                raise ConnectionError("当前未连接 Android 设备，请先在右侧【配对与连接】中连接设备。")
        if source != "webui_auto":
            self.touch_activity()

    @staticmethod
    def _raise_if_input_failed(code: int, out: str, err: str, action: str):
        combined = f"{out}\n{err}".strip()
        low = combined.lower()
        if code == 0 and "error:" not in low and "exception" not in low:
            return
        if "no devices" in low or "device offline" in low or "not found" in low:
            raise ConnectionError(f"{action}失败：设备已断开（{combined[:200]}）")
        raise RuntimeError(f"{action}失败（rc={code}）: {combined[:300] or '设备未返回任何信息'}")

    async def close(self):
        await self.disconnect(reason="插件卸载或关闭", source="system")
