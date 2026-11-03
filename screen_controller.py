"""
屏幕与键鼠控制器 (AstrbotFishEyes - Unified Screen & Input Controller)

统一抽象本地模式 (Local) 与远程模式 (Remote - Direct HTTP / SSH Tunnel) 的控制逻辑。
支持：
1. 显式连接 (connect) 与断开连接 (disconnect) 状态管理，默认不自动连接；
2. 无操作超时自动断连并回收缓存/会话内存 (idle_timeout)；
3. 未连接时若 Bot (LLM 工具或聊天命令) 需要查看或操作电脑，自动尝试一次连接；
4. 为 WebUI、Chat 命令与 LLM Tools 提供统一一致的异步调用接口。
"""

import asyncio
import base64
import ctypes
import gc
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

# Windows 下开启高 DPI 感知，确保截图像素尺寸与鼠标点击坐标 1:1 对齐
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# 尝试导入本地 GUI 自动化库（若处于无桌面环境，优雅降级）
try:
    import pyautogui
    import PIL.Image
    try:
        from PIL import ImageGrab
    except ImportError:
        ImageGrab = None
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    HAS_LOCAL_GUI = True
except Exception:
    HAS_LOCAL_GUI = False
    ImageGrab = None
    import PIL.Image

try:
    import pyperclip
    HAS_PYPERCLIP = True
except Exception:
    HAS_PYPERCLIP = False

from ssh_tunnel import SSHTunnel


def _local_grab_screen():
    """本地安全截屏，兼容 pyautogui 与 PIL.ImageGrab 兜底，并确保为 RGB 模式"""
    last_err = None
    if HAS_LOCAL_GUI:
        try:
            img = pyautogui.screenshot()
            if img is not None:
                return img.convert("RGB") if img.mode != "RGB" else img
        except Exception as e:
            last_err = e
    if ImageGrab is not None:
        try:
            img = ImageGrab.grab()
            if img is not None:
                return img.convert("RGB") if img.mode != "RGB" else img
        except Exception as e:
            last_err = e
    raise RuntimeError(f"本地截屏失败: {last_err or '缺少 GUI 环境'}")


class UnifiedController:
    """统一屏幕与输入控制器"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config if config is not None else {}
        self.mode: str = self.config.get("mode", "local")  # 'local' | 'remote'
        self.is_locked: bool = False  # 紧急锁定开关（锁定后禁止键鼠下发）

        # 显式连接状态管理（默认未连接，节省内存与后台开销）
        self._connected: bool = False
        self.last_active_time: float = 0.0
        self._cached_screen: Dict[str, int] = {"width": 0, "height": 0}
        self._cached_platform: str = sys.platform
        self._cached_agent_version: str = "1.0.0"
        self._last_error: Optional[str] = None
        self._idle_task: Optional[asyncio.Task] = None

        # 远程连接组件
        self.tunnel: Optional[SSHTunnel] = None
        self._http_session: Optional[Any] = None
        self._session_timeout_sec: int = 0

        # 操作日志缓冲区（最近 50 条）
        self.action_logs: List[Dict[str, Any]] = []
        self._last_screenshot_cache: Optional[Dict[str, Any]] = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def idle_timeout(self) -> int:
        try:
            val = int(self.config.get("idle_timeout", 300))
            return max(0, val)
        except Exception:
            return 300

    def touch_activity(self):
        """刷新最近一次有效操作时间戳（Bot 或管理员触发操作时调用）"""
        self.last_active_time = time.time()

    def _is_bot_source(self, source: str) -> bool:
        """判断请求来源是否为 Bot（LLM 工具或聊天指令）"""
        s = (source or "").lower()
        return s == "llm" or s == "bot" or s.startswith("chat")

    def _ensure_idle_monitor(self):
        """启动后台无操作自动断连巡检协程（仅在已连接状态下运行）"""
        try:
            loop = asyncio.get_running_loop()
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = loop.create_task(self._idle_monitor_loop())
        except RuntimeError:
            pass

    async def _idle_monitor_loop(self):
        """后台巡检：当超过 idle_timeout 秒无任何 Bot 或管理员操作时自动断连释放内存"""
        try:
            while self._connected:
                await asyncio.sleep(3.0)
                await self.check_idle_timeout()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def check_idle_timeout(self) -> bool:
        """检查是否达到无操作超时阈值；若已超时则自动断开连接并返回 True"""
        if not self._connected:
            return False
        timeout_sec = self.idle_timeout
        if timeout_sec <= 0 or self.last_active_time <= 0:
            return False

        elapsed = time.time() - self.last_active_time
        if elapsed >= timeout_sec:
            await self.disconnect(
                reason=f"超过 {timeout_sec} 秒无操作，已自动断开连接并释放内存",
                source="idle_timer",
            )
            return True
        return False

    async def update_config_async(self, new_config: dict):
        """异步更新配置并在连接参数变化时自动重置隧道与会话"""
        old_mode = self.mode
        self.config.update(new_config)
        self.mode = self.config.get("mode", self.mode)

        use_ssh = bool(self.config.get("use_ssh_tunnel", False))
        if self.tunnel is not None:
            if not use_ssh or self.mode != "remote" or not self.tunnel.matches_config(self.config):
                await self.tunnel.disconnect()
                self.tunnel = None

        new_timeout = int(self.config.get("command_timeout", 30) or 30)
        if self._http_session and not self._http_session.closed and new_timeout != self._session_timeout_sec:
            await self._http_session.close()
            self._http_session = None

        if old_mode != self.mode and self._connected:
            await self.disconnect(reason=f"控制模式切换为 {self.mode}，请重新连接", source="config")

        self.touch_activity()

    def update_config(self, new_config: dict):
        """同步更新配置兼容接口"""
        self.config.update(new_config)
        self.mode = self.config.get("mode", self.mode)
        if self.tunnel is not None and not self.tunnel.matches_config(self.config):
            old_tunnel = self.tunnel
            self.tunnel = None
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(old_tunnel.disconnect())
            except RuntimeError:
                pass
        self.touch_activity()

    def record_log(self, action: str, details: str, status: str = "ok", source: str = "system"):
        """记录一条操作日志"""
        log_entry = {
            "time": time.strftime("%H:%M:%S"),
            "action": action,
            "details": details,
            "status": status,
            "source": source,
        }
        self.action_logs.insert(0, log_entry)
        if len(self.action_logs) > 50:
            self.action_logs.pop()

    # =========================================================================
    #  连接、断开连接与按需自动重连管理
    # =========================================================================

    async def get_http_session(self):
        if not HAS_AIOHTTP:
            return None
        timeout_sec = int(self.config.get("command_timeout", 30) or 30)
        if self._http_session is None or self._http_session.closed or self._session_timeout_sec != timeout_sec:
            if self._http_session and not self._http_session.closed:
                await self._http_session.close()
            timeout = aiohttp.ClientTimeout(total=timeout_sec, connect=min(8, timeout_sec))
            self._http_session = aiohttp.ClientSession(timeout=timeout)
            self._session_timeout_sec = timeout_sec
        return self._http_session

    async def ensure_connection(self) -> bool:
        """确保底层传输通道 (本地 GUI 或远程 SSH 隧道) 就绪"""
        if self.mode == "local":
            if not HAS_LOCAL_GUI and ImageGrab is None:
                raise RuntimeError(
                    "本地缺少 GUI 环境或未安装 pyautogui/pillow。若 AstrBot 运行在无桌面的 Linux/Docker 中，请切换到【远程模式 (remote)】连接被控电脑！"
                )
            return True

        # Remote 模式
        use_ssh = bool(self.config.get("use_ssh_tunnel", False))
        if use_ssh:
            if self.tunnel is not None and not self.tunnel.matches_config(self.config):
                await self.tunnel.disconnect()
                self.tunnel = None

            if not self.tunnel:
                self.tunnel = SSHTunnel(
                    ssh_host=self.config.get("ssh_host", ""),
                    ssh_port=self.config.get("ssh_port", 22),
                    ssh_username=self.config.get("ssh_username", ""),
                    ssh_password=self.config.get("ssh_password", ""),
                    ssh_key_path=self.config.get("ssh_key_path", ""),
                    remote_port=self.config.get("remote_port", 8765),
                    agent_token=self.config.get("remote_token", ""),
                )
            await self.tunnel.ensure_connected()
        return True

    async def connect(self, source: str = "manual") -> Dict[str, Any]:
        """显式建立连接并更新活动时间"""
        async with self._lock:
            self._last_error = None
            try:
                await self.ensure_connection()
                if self.mode == "local":
                    size = pyautogui.size()
                    self._cached_screen = {"width": size.width, "height": size.height}
                    self._cached_platform = sys.platform
                    self._connected = True
                else:
                    resp = await self._remote_request("GET", "/health", timeout_override=6.0)
                    if resp.get("status") != "ok":
                        err_msg = resp.get("message") or "被控端返回状态未就绪"
                        raise RuntimeError(err_msg)
                    self._cached_screen = resp.get("screen", {"width": 0, "height": 0})
                    self._cached_platform = resp.get("platform", "remote")
                    self._cached_agent_version = resp.get("agent_version", "1.0.0")
                    self._connected = True

                self.touch_activity()
                self._ensure_idle_monitor()
                self.record_log(
                    "connect",
                    f"连接成功 ({self.mode} 模式, 分辨率 {self._cached_screen.get('width')}x{self._cached_screen.get('height')})",
                    source=source,
                )
            except Exception as e:
                self._connected = False
                self._last_error = str(e)
                await self._release_resources()
                self.record_log("connect", f"连接失败: {e}", status="error", source=source)

        return await self.get_status(probe=False)

    async def _release_resources(self):
        """内部释放截图缓存、HTTP 会话与 SSH 隧道以节省内存"""
        self._last_screenshot_cache = None
        if self._http_session and not self._http_session.closed:
            try:
                await self._http_session.close()
            except Exception:
                pass
        self._http_session = None
        if self.tunnel:
            try:
                await self.tunnel.disconnect()
            except Exception:
                pass
            self.tunnel = None
        gc.collect()

    async def disconnect(self, reason: str = "手动断开连接", source: str = "manual") -> Dict[str, Any]:
        """显式断开连接并释放内存资源"""
        was_connected = self._connected
        self._connected = False
        self.last_active_time = 0.0
        self._last_error = None

        if self._idle_task and not self._idle_task.done() and asyncio.current_task() is not self._idle_task:
            self._idle_task.cancel()
        self._idle_task = None

        await self._release_resources()

        if was_connected or source == "manual":
            self.record_log("disconnect", reason, source=source)

        return await self.get_status(probe=False)

    async def _ensure_active_for_action(self, source: str = "system"):
        """
        在执行截图或键鼠操作前检查连接状态：
        - 先检查是否已超时自动断连；
        - 若当前处于未连接状态：
          - 当请求来自 Bot (LLM 工具或聊天指令) 时，自动尝试一次连接；
          - 当请求来自 WebUI 自动刷新或未连接点击时，不自动重连，直接提示先连接。
        """
        await self.check_idle_timeout()

        if not self._connected:
            if self._is_bot_source(source):
                self.record_log("auto_connect", "未连接状态下 Bot 请求操作，自动尝试一次连接...", source=source)
                status = await self.connect(source=source)
                if not status.get("connected"):
                    raise ConnectionError(f"Bot 自动尝试连接失败: {status.get('error') or '请检查被控端服务是否已开启'}")
            else:
                raise ConnectionError("当前未连接被控端，请先点击【连接】按钮建立连接。")

        # 非 WebUI 后台自动轮询的所有操作均刷新无操作计时器
        if source != "webui_auto":
            self.touch_activity()

    def _get_remote_base_url(self) -> str:
        """获取远程 Agent 的基础 HTTP 请求 URL（自动清洗用户输入的协议头或端口后缀）"""
        use_ssh = bool(self.config.get("use_ssh_tunnel", False))
        if use_ssh:
            if not self.tunnel or not self.tunnel.connected:
                raise RuntimeError("SSH 隧道尚未建立，请先建立连接")
            return self.tunnel.base_url

        raw_host = (self.config.get("remote_host") or "127.0.0.1").strip()
        default_port = int(self.config.get("remote_port") or 8765)

        if "://" in raw_host:
            parsed = urlparse(raw_host)
            scheme = parsed.scheme or "http"
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or default_port
            return f"{scheme}://{host}:{port}"

        raw_host = raw_host.rstrip("/")
        if ":" in raw_host and raw_host.count(":") == 1:
            host_part, port_part = raw_host.split(":", 1)
            if port_part.isdigit():
                return f"http://{host_part or '127.0.0.1'}:{int(port_part)}"

        return f"http://{raw_host or '127.0.0.1'}:{default_port}"

    def _urllib_sync_request(
        self,
        method: str,
        url: str,
        headers: dict,
        json_data: Optional[dict],
        timeout_sec: float,
    ) -> Any:
        """当环境未安装 aiohttp 时的标准库 urllib 无缝后备请求"""
        req_headers = dict(headers)
        body_bytes = None
        if json_data is not None:
            body_bytes = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read()
                if "image/" in content_type:
                    return {"_image_bytes": raw}
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as he:
            if he.code == 401:
                raise PermissionError("被控端拒绝访问 (401)：安全 Token 不匹配，请检查插件与被控端的 Token 设置。") from he
            err_body = ""
            try:
                err_body = he.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"远程被控端响应异常 [{he.code}]: {err_body}") from he
        except urllib.error.URLError as ue:
            raise ConnectionError(f"无法连接到被控端 ({url})，请确认被控端已点击【启动服务】且网络/防火墙畅通: {ue.reason}") from ue

    async def _remote_request(
        self,
        method: str,
        path: str,
        json_data: Optional[dict] = None,
        timeout_override: Optional[float] = None,
    ) -> Any:
        """向远程 Agent 发送 HTTP 请求（优先使用 aiohttp，缺失时自动降级为标准库 urllib）"""
        await self.ensure_connection()
        base_url = self._get_remote_base_url()
        url = f"{base_url}{path}"
        headers = {}
        token = (self.config.get("remote_token") or "").strip()
        if token:
            headers["X-Agent-Token"] = token

        timeout_sec = float(timeout_override or self.config.get("command_timeout", 30) or 30)

        if not HAS_AIOHTTP:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                self._urllib_sync_request,
                method,
                url,
                headers,
                json_data,
                timeout_sec,
            )

        session = await self.get_http_session()
        req_kwargs: Dict[str, Any] = {"json": json_data, "headers": headers}
        if timeout_override is not None:
            req_kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout_override, connect=min(5.0, timeout_override))

        try:
            async with session.request(method, url, **req_kwargs) as resp:
                if resp.status == 401:
                    raise PermissionError("被控端拒绝访问 (401)：安全 Token 不匹配，请检查插件与被控端的 Token 设置。")
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"远程被控端响应异常 [{resp.status}]: {text}")
                content_type = resp.headers.get("Content-Type", "")
                if "image/" in content_type:
                    return {"_image_bytes": await resp.read()}
                return await resp.json()
        except aiohttp.ClientConnectorError as e:
            raise ConnectionError(f"无法连接到被控端 ({base_url})，请确认被控端已点击【启动服务】且网络/防火墙畅通: {e}") from e
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"连接被控端 ({base_url}) 超时，请检查 IP、端口或隧道状态") from e

    async def close(self):
        """释放全部资源"""
        await self.disconnect(reason="插件卸载或关闭", source="system")

    # =========================================================================
    #  核心功能：状态与截图
    # =========================================================================

    async def get_status(self, probe: bool = False) -> Dict[str, Any]:
        """
        获取当前控制器状态。
        - 默认 probe=False：仅返回当前连接状态（不会在未连接时自动发起连接，防止后台被意外唤醒）；
        - 若当前已连接，先检查是否达到无操作超时阈值。
        """
        await self.check_idle_timeout()

        if probe and not self._connected:
            return await self.connect(source="probe")

        idle_remaining = None
        if self._connected and self.idle_timeout > 0 and self.last_active_time > 0:
            idle_remaining = max(0, int(self.idle_timeout - (time.time() - self.last_active_time)))

        status_info: Dict[str, Any] = {
            "mode": self.mode,
            "is_locked": self.is_locked,
            "connected": self._connected,
            "screen": self._cached_screen if self._connected else {"width": 0, "height": 0},
            "platform": self._cached_platform if self._connected else sys.platform,
            "agent_version": self._cached_agent_version,
            "ssh_tunnel": bool(self.config.get("use_ssh_tunnel", False)),
            "idle_timeout": self.idle_timeout,
            "idle_remaining": idle_remaining,
            "error": self._last_error,
        }

        if not self._connected:
            return status_info

        # 若处于已连接状态，轻量校验一次底层健康状态
        try:
            if self.mode == "local":
                if not HAS_LOCAL_GUI and ImageGrab is None:
                    raise RuntimeError("本地未安装或缺少桌面 GUI 环境 (pyautogui)")
                size = pyautogui.size()
                self._cached_screen = {"width": size.width, "height": size.height}
                status_info["screen"] = self._cached_screen
            else:
                resp = await self._remote_request("GET", "/health", timeout_override=4.0)
                if resp.get("status") != "ok":
                    raise RuntimeError(resp.get("message") or "被控端状态异常")
                self._cached_screen = resp.get("screen", self._cached_screen)
                status_info["screen"] = self._cached_screen
                status_info["platform"] = resp.get("platform", "remote")
        except Exception as e:
            self._last_error = str(e)
            await self.disconnect(reason=f"连接中断: {e}", source="health_check")
            status_info["connected"] = False
            status_info["error"] = str(e)
            status_info["screen"] = {"width": 0, "height": 0}

        return status_info

    async def take_screenshot(
        self,
        quality: Optional[int] = None,
        source: str = "system",
        include_bytes: bool = True,
    ) -> Dict[str, Any]:
        """获取屏幕截图，返回 base64 及可选的原始二进制字节"""
        await self._ensure_active_for_action(source=source)

        if quality is None:
            quality = int(self.config.get("screenshot_quality", 75) or 75)
        quality = max(10, min(100, int(quality)))
        t0 = time.time()

        loop = asyncio.get_running_loop()

        if self.mode == "local":
            if not HAS_LOCAL_GUI and ImageGrab is None:
                raise RuntimeError("本地缺少桌面 GUI 环境，无法截图！请切换至 Remote 模式。")

            def _capture_local():
                img = _local_grab_screen()
                w_local, h_local = img.size
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                return buf.getvalue(), w_local, h_local

            image_bytes, w, h = await loop.run_in_executor(None, _capture_local)
        else:
            resp = await self._remote_request("GET", f"/screenshot?quality={quality}")
            image_bytes = resp.get("_image_bytes")
            if not image_bytes:
                raise RuntimeError("远程被控端返回截图数据为空")

            def _parse_size(raw_bytes: bytes):
                with PIL.Image.open(io.BytesIO(raw_bytes)) as im:
                    return im.size

            w, h = await loop.run_in_executor(None, _parse_size, image_bytes)

        self._cached_screen = {"width": w, "height": h}
        img_b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{img_b64}"
        cost = int((time.time() - t0) * 1000)

        result: Dict[str, Any] = {
            "base64": img_b64,
            "data_url": data_url,
            "width": w,
            "height": h,
            "size_bytes": len(image_bytes),
            "cost_ms": cost,
        }
        if include_bytes:
            result["bytes"] = image_bytes

        # 仅缓存不含原始 bytes 的轻量元数据，防止重复驻留双份大图内存
        self._last_screenshot_cache = {
            "width": w,
            "height": h,
            "size_bytes": len(image_bytes),
            "cost_ms": cost,
        }
        if source != "webui_auto":
            self.record_log("screenshot", f"尺寸: {w}x{h}, 大小: {len(image_bytes)//1024}KB, 耗时: {cost}ms", source=source)
        return result

    # =========================================================================
    #  核心功能：鼠标与键盘输入
    # =========================================================================

    def _check_lock(self):
        if self.is_locked:
            raise PermissionError("屏幕控制已被紧急锁定！请在 WebUI 中解除锁定后再执行操作。")

    async def click(self, x: int, y: int, button: str = "left", clicks: int = 1, source: str = "system") -> Dict[str, Any]:
        """点击鼠标坐标"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        x, y = int(x), int(y)
        clicks = int(clicks)
        if self.mode == "local":
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: pyautogui.click(x=x, y=y, button=button, clicks=clicks, interval=0.05),
            )
            res = {"status": "ok", "action": "click", "x": x, "y": y, "button": button}
        else:
            res = await self._remote_request("POST", "/click", {"x": x, "y": y, "button": button, "clicks": clicks})

        self.record_log(f"click_{button}", f"坐标: ({x}, {y}), 次数: {clicks}", source=source)
        return res

    async def double_click(self, x: int, y: int, source: str = "system") -> Dict[str, Any]:
        """双击鼠标"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        x, y = int(x), int(y)
        if self.mode == "local":
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: pyautogui.doubleClick(x=x, y=y))
            res = {"status": "ok", "action": "double_click", "x": x, "y": y}
        else:
            res = await self._remote_request("POST", "/double_click", {"x": x, "y": y})
        self.record_log("double_click", f"坐标: ({x}, {y})", source=source)
        return res

    async def right_click(self, x: int, y: int, source: str = "system") -> Dict[str, Any]:
        """右键点击"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        x, y = int(x), int(y)
        if self.mode == "local":
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: pyautogui.rightClick(x=x, y=y))
            res = {"status": "ok", "action": "right_click", "x": x, "y": y}
        else:
            res = await self._remote_request("POST", "/right_click", {"x": x, "y": y})
        self.record_log("right_click", f"坐标: ({x}, {y})", source=source)
        return res

    async def move(self, x: int, y: int, duration: float = 0.0, source: str = "system") -> Dict[str, Any]:
        """移动鼠标"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        x, y = int(x), int(y)
        if self.mode == "local":
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: pyautogui.moveTo(x=x, y=y, duration=duration))
            res = {"status": "ok", "action": "move", "x": x, "y": y}
        else:
            res = await self._remote_request("POST", "/move", {"x": x, "y": y, "duration": duration})

        self.record_log("move", f"移动到: ({x}, {y})", source=source)
        return res

    async def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5, button: str = "left", source: str = "system") -> Dict[str, Any]:
        """鼠标拖拽"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        if self.mode == "local":
            loop = asyncio.get_running_loop()

            def _do_drag():
                pyautogui.moveTo(x1, y1)
                pyautogui.dragTo(x2, y2, duration=duration, button=button)

            await loop.run_in_executor(None, _do_drag)
            res = {"status": "ok", "action": "drag", "from": [x1, y1], "to": [x2, y2]}
        else:
            res = await self._remote_request("POST", "/drag", {
                "x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration, "button": button
            })
        self.record_log("drag", f"从 ({x1},{y1}) 拖动到 ({x2},{y2})", source=source)
        return res

    async def type_text(self, text: str, interval: float = 0.0, source: str = "system") -> Dict[str, Any]:
        """输入文本（自动处理非 ASCII/中文字符）"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        if not text:
            return {"status": "ok", "action": "type", "length": 0}

        if self.mode == "local":
            loop = asyncio.get_running_loop()

            def _do_type():
                if text.isascii():
                    pyautogui.typewrite(text, interval=interval)
                else:
                    if HAS_PYPERCLIP:
                        old = pyperclip.paste()
                        pyperclip.copy(text)
                        pyautogui.hotkey("ctrl", "v")
                        time.sleep(0.1)
                        pyperclip.copy(old)
                    else:
                        for char in text:
                            pyautogui.press(char)

            await loop.run_in_executor(None, _do_type)
            res = {"status": "ok", "action": "type", "length": len(text)}
        else:
            res = await self._remote_request("POST", "/type", {"text": text, "interval": interval})

        display_txt = (text[:30] + "...") if len(text) > 30 else text
        self.record_log("type", f"输入: {repr(display_txt)}", source=source)
        return res

    async def press_key(self, key: str, source: str = "system") -> Dict[str, Any]:
        """敲击单键 (enter, esc, tab, backspace, win 等)"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        if self.mode == "local":
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: pyautogui.press(key))
            res = {"status": "ok", "action": "press", "key": key}
        else:
            res = await self._remote_request("POST", "/press", {"keys": [key]})

        self.record_log("press", f"按键: {key}", source=source)
        return res

    async def hotkey(self, keys: List[str], source: str = "system") -> Dict[str, Any]:
        """执行快捷键组合 (例如 ['ctrl', 'c'], ['alt', 'tab'])"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        if self.mode == "local":
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: pyautogui.hotkey(*keys))
            res = {"status": "ok", "action": "hotkey", "keys": keys}
        else:
            res = await self._remote_request("POST", "/hotkey", {"keys": keys})

        self.record_log("hotkey", f"组合键: {' + '.join(keys)}", source=source)
        return res

    async def scroll(self, direction: str = "down", amount: int = 3, x: Optional[int] = None, y: Optional[int] = None, source: str = "system") -> Dict[str, Any]:
        """滚动屏幕滚轮"""
        self._check_lock()
        await self._ensure_active_for_action(source=source)
        clicks = amount if direction == "up" else -amount
        if self.mode == "local":
            loop = asyncio.get_running_loop()
            kwargs = {}
            if x is not None and y is not None:
                kwargs["x"] = int(x)
                kwargs["y"] = int(y)
            await loop.run_in_executor(None, lambda: pyautogui.scroll(clicks, **kwargs))
            res = {"status": "ok", "action": "scroll", "direction": direction, "amount": amount}
        else:
            req_data: Dict[str, Any] = {"direction": direction, "amount": amount}
            if x is not None and y is not None:
                req_data["x"] = int(x)
                req_data["y"] = int(y)
            res = await self._remote_request("POST", "/scroll", req_data)

        self.record_log("scroll", f"方向: {direction}, 步长: {amount}", source=source)
        return res
