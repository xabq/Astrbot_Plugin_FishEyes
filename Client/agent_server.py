"""
Agent HTTP 服务模块 (AstrbotFishEyes)
负责响应屏幕截图、键鼠自动化操作的 HTTP API 请求，支持在后台线程稳定启停。
采用 Python 标准库原生 ThreadingHTTPServer 实现，彻底避免 Uvicorn/FastAPI 在
PyInstaller --windowed 无控制台模式及 Python 3.14 子线程事件循环下的崩溃与假死问题。
"""

import ctypes
import io
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse


class _NullStream:
    """当 PyInstaller --windowed 模式下 stdout/stderr 为 None 时的安全兜底流"""
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

# Windows 下开启高 DPI 感知，确保截图像素尺寸与鼠标点击坐标 1:1 严格对齐
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

try:
    import pyautogui
    from PIL import Image
    try:
        from PIL import ImageGrab
    except ImportError:
        ImageGrab = None
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    HAS_GUI = True
except Exception:
    HAS_GUI = False
    ImageGrab = None

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False


def _take_screen_image():
    """安全截取屏幕图像，兼容 pyautogui 与 PIL.ImageGrab 兜底，并统一转换为 RGB"""
    last_err = None
    if HAS_GUI:
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

    raise RuntimeError(f"无法获取屏幕画面: {last_err or 'GUI 环境不可用'}")


def check_port_available(host: str, port: int) -> bool:
    """检查指定 IP 与端口是否可用（准确兼容 Windows 端口独占检测）"""
    port = int(port)
    bind_host = "127.0.0.1" if host in ("0.0.0.0", "") else host

    # 1. 先探测本地该端口是否已有服务正在监听
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return False
    except Exception:
        pass

    # 2. 尝试实际绑定该地址与端口（Windows 下使用 SO_EXCLUSIVEADDRUSE 防止误判）
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            elif sys.platform != "win32":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_host if host != "0.0.0.0" else "0.0.0.0", port))
            return True
    except OSError:
        return False


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    """支持快速释放端口与后台守护工作线程的 HTTP Server"""
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except Exception:
                pass
        super().server_bind()


class AgentServer:
    def __init__(self):
        self.token: str = ""
        self.log_callback: Optional[Callable[[str], None]] = None
        self.status_callback: Optional[Callable[[bool], None]] = None
        self._server_thread: Optional[threading.Thread] = None
        self._httpd: Optional[_ReusableThreadingHTTPServer] = None
        self.is_running: bool = False
        self._lock = threading.Lock()

    def log(self, message: str, level: str = "INFO"):
        formatted = f"[{time.strftime('%H:%M:%S')}] [{level}] {message}"
        if self.log_callback:
            try:
                self.log_callback(formatted)
            except Exception:
                pass
        else:
            try:
                print(formatted)
            except Exception:
                pass

    def _notify_status(self, running: bool):
        self.is_running = running
        if self.status_callback:
            try:
                self.status_callback(running)
            except Exception:
                pass

    def _make_handler(self):
        server_ref = self

        class AgentRequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format_str, *args):
                pass

            def _send_cors_headers(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")

            def _send_json(self, status_code: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _send_bytes(self, status_code: int, content_type: str, data: bytes):
                self.send_response(status_code)
                self._send_cors_headers()
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def _check_auth(self, parsed_url) -> bool:
                if parsed_url.path in ("/health", "/", "/docs", "/openapi.json"):
                    return True
                if not server_ref.token:
                    return True
                req_token = self.headers.get("X-Agent-Token")
                if not req_token:
                    qs = parse_qs(parsed_url.query)
                    req_token = (qs.get("token") or [None])[0]
                if req_token != server_ref.token:
                    server_ref.log(f"拦截未授权请求: {parsed_url.path}", "WARN")
                    self._send_json(401, {"detail": "Unauthorized: invalid token"})
                    return False
                return True

            def _read_json_body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                return json.loads(raw.decode("utf-8")) if raw else {}

            def do_OPTIONS(self):
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def do_GET(self):
                parsed = urlparse(self.path)
                if not self._check_auth(parsed):
                    return

                path = parsed.path
                qs = parse_qs(parsed.query)

                if path == "/":
                    self._send_json(200, {
                        "service": "AstrbotFishEyes Screen Agent",
                        "status": "running",
                        "auth_enabled": bool(server_ref.token),
                    })
                    return

                if path == "/health":
                    if not HAS_GUI and ImageGrab is None:
                        self._send_json(200, {"status": "error", "message": "GUI not ready"})
                        return
                    try:
                        size = pyautogui.size()
                        self._send_json(200, {
                            "status": "ok",
                            "screen": {"width": size.width, "height": size.height},
                            "platform": sys.platform,
                            "agent_version": "1.0.0",
                        })
                    except Exception as e:
                        self._send_json(200, {"status": "error", "message": str(e)})
                    return

                if path == "/screenshot":
                    if not HAS_GUI and ImageGrab is None:
                        self._send_json(500, {"detail": "GUI not available"})
                        return
                    try:
                        q_raw = (qs.get("quality") or ["75"])[0]
                        quality = max(10, min(100, int(q_raw)))
                    except Exception:
                        quality = 75

                    try:
                        t0 = time.time()
                        img = _take_screen_image()
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=quality)
                        jpeg_bytes = buf.getvalue()
                        cost = int((time.time() - t0) * 1000)
                        server_ref.log(f"截取屏幕完成 ({img.width}x{img.height}, 耗时: {cost}ms)")
                        self._send_bytes(200, "image/jpeg", jpeg_bytes)
                    except Exception as e:
                        server_ref.log(f"截屏失败: {e}", "ERROR")
                        self._send_json(500, {"detail": str(e)})
                    return

                self._send_json(404, {"detail": f"Not Found: {path}"})

            def do_POST(self):
                parsed = urlparse(self.path)
                if not self._check_auth(parsed):
                    return

                path = parsed.path
                try:
                    data = self._read_json_body()
                except Exception as e:
                    self._send_json(400, {"detail": f"Invalid JSON body: {e}"})
                    return

                try:
                    if path == "/click":
                        x = int(data.get("x", 0))
                        y = int(data.get("y", 0))
                        button = data.get("button", "left")
                        clicks = int(data.get("clicks", 1))
                        interval = float(data.get("interval", 0.05))
                        pyautogui.click(x=x, y=y, button=button, clicks=clicks, interval=interval)
                        server_ref.log(f"鼠标点击: 坐标({x}, {y}), 按键:{button}, 次数:{clicks}")
                        self._send_json(200, {"status": "ok", "action": "click", "x": x, "y": y, "button": button})
                        return

                    if path == "/double_click":
                        x = int(data.get("x", 0))
                        y = int(data.get("y", 0))
                        pyautogui.doubleClick(x=x, y=y)
                        server_ref.log(f"鼠标双击: 坐标({x}, {y})")
                        self._send_json(200, {"status": "ok", "action": "double_click", "x": x, "y": y})
                        return

                    if path == "/right_click":
                        x = int(data.get("x", 0))
                        y = int(data.get("y", 0))
                        pyautogui.rightClick(x=x, y=y)
                        server_ref.log(f"鼠标右键点击: 坐标({x}, {y})")
                        self._send_json(200, {"status": "ok", "action": "right_click", "x": x, "y": y})
                        return

                    if path == "/move":
                        x = int(data.get("x", 0))
                        y = int(data.get("y", 0))
                        duration = float(data.get("duration", 0.0))
                        pyautogui.moveTo(x=x, y=y, duration=duration)
                        server_ref.log(f"鼠标移动: ({x}, {y})")
                        self._send_json(200, {"status": "ok", "action": "move", "x": x, "y": y})
                        return

                    if path == "/drag":
                        x1, y1 = int(data.get("x1", 0)), int(data.get("y1", 0))
                        x2, y2 = int(data.get("x2", 0)), int(data.get("y2", 0))
                        duration = float(data.get("duration", 0.5))
                        button = data.get("button", "left")
                        pyautogui.moveTo(x1, y1)
                        pyautogui.dragTo(x2, y2, duration=duration, button=button)
                        server_ref.log(f"鼠标拖拽: ({x1},{y1}) -> ({x2},{y2})")
                        self._send_json(200, {"status": "ok", "action": "drag"})
                        return

                    if path == "/type":
                        text = data.get("text", "")
                        interval = float(data.get("interval", 0.0))
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
                        display_t = (text[:20] + "...") if len(text) > 20 else text
                        server_ref.log(f"输入文本: {repr(display_t)} (长度:{len(text)})")
                        self._send_json(200, {"status": "ok", "action": "type", "length": len(text)})
                        return

                    if path == "/press":
                        keys = data.get("keys", [])
                        if isinstance(keys, str):
                            keys = [keys]
                        for k in keys:
                            pyautogui.press(k)
                        server_ref.log(f"按键输入: {keys}")
                        self._send_json(200, {"status": "ok", "action": "press", "keys": keys})
                        return

                    if path == "/hotkey":
                        keys = data.get("keys", [])
                        if isinstance(keys, str):
                            keys = [keys]
                        pyautogui.hotkey(*keys)
                        server_ref.log(f"组合键: {' + '.join(keys)}")
                        self._send_json(200, {"status": "ok", "action": "hotkey", "keys": keys})
                        return

                    if path == "/scroll":
                        direction = data.get("direction", "down")
                        amount = int(data.get("amount", 3))
                        x = data.get("x")
                        y = data.get("y")
                        clicks = amount if direction == "up" else -amount
                        kwargs = {}
                        if x is not None and y is not None:
                            kwargs["x"] = int(x)
                            kwargs["y"] = int(y)
                        pyautogui.scroll(clicks, **kwargs)
                        server_ref.log(f"滚轮滚动: {direction} {amount} 格")
                        self._send_json(200, {"status": "ok", "action": "scroll"})
                        return

                    self._send_json(404, {"detail": f"Not Found: {path}"})
                except Exception as e:
                    server_ref.log(f"执行操作 {path} 失败: {e}", "ERROR")
                    self._send_json(500, {"detail": str(e)})

        return AgentRequestHandler

    def start(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        token: str = "",
        log_callback=None,
        status_callback=None,
    ) -> bool:
        with self._lock:
            if self.is_running:
                return True

            self.token = token or ""
            self.log_callback = log_callback
            self.status_callback = status_callback

            bind_host = host.strip() or "0.0.0.0"
            bind_port = int(port)

            try:
                handler_cls = self._make_handler()
                httpd = _ReusableThreadingHTTPServer((bind_host, bind_port), handler_cls)
                self._httpd = httpd
            except Exception as e:
                self.log(f"无法绑定本地端口 {bind_host}:{bind_port} -> {e}", "ERROR")
                self._httpd = None
                self._notify_status(False)
                return False

            self._notify_status(True)
            self.log(
                f"本地服务已就绪: http://{bind_host}:{bind_port} (安全Token: {'已启用' if self.token else '未启用'})",
                "SUCCESS",
            )

            def _serve():
                try:
                    httpd.serve_forever(poll_interval=0.2)
                except Exception as e:
                    self.log(f"服务异常退出: {e}", "ERROR")
                finally:
                    try:
                        httpd.server_close()
                    except Exception:
                        pass
                    self._notify_status(False)
                    self.log("本地服务已停止", "INFO")

            self._server_thread = threading.Thread(
                target=_serve, daemon=True, name="AgentHttpServer"
            )
            self._server_thread.start()
            return True

    def stop(self):
        with self._lock:
            httpd = self._httpd
            if not self.is_running and httpd is None:
                return
            self.log("正在停止本地服务...", "INFO")
            self._httpd = None

        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=1.5)
        self._notify_status(False)
