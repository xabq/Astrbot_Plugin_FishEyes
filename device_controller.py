"""
控制目标门面 (AstrbotFishEyes - Device Controller Facade)
========================================================

把两套完全不同的被控目标统一成同一套异步接口，供 `main.py`（WebUI API / LLM 工具 / 聊天指令）调用：

- **pc**      → `screen_controller.UnifiedController`（本机 pyautogui 或远程 Agent / SSH 隧道）
- **android** → `android_controller.AndroidADBController`（adb 无线调试 / SSH 反向隧道暴露的 adbd）

门面负责：
1. `target` 目标切换（切换时先断开旧目标，避免两套会话同时存活）；
2. 统一的 `status` 聚合（PC 状态 + Android/ADB 环境快照，供 WebUI 左上角切换与右侧配对面板使用）；
3. 共享一份操作日志，WebUI「运行日志」不分目标都能看到；
4. 紧急锁定开关同时作用于两个目标。
"""

import time
from typing import Any, Dict, List, Optional

from android_controller import AndroidADBController
from screen_controller import UnifiedController

PLUGIN_NAME = "AstrbotFishEyes"
PLUGIN_VERSION = "1.1.12"

TARGET_PC = "pc"
TARGET_ANDROID = "android"
TARGETS = (TARGET_PC, TARGET_ANDROID)

_TARGET_ALIASES = {
    "pc": TARGET_PC, "local": TARGET_PC, "remote": TARGET_PC, "windows": TARGET_PC,
    "computer": TARGET_PC, "desktop": TARGET_PC, "电脑": TARGET_PC, "本机": TARGET_PC,
    "android": TARGET_ANDROID, "adb": TARGET_ANDROID, "phone": TARGET_ANDROID,
    "mobile": TARGET_ANDROID, "安卓": TARGET_ANDROID, "手机": TARGET_ANDROID,
}

TARGET_LABELS = {TARGET_PC: "PC 电脑", TARGET_ANDROID: "Android 手机"}


def normalize_target(value: Any, default: str = TARGET_PC) -> str:
    """把用户输入的目标别名归一化为 pc / android"""
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    return _TARGET_ALIASES.get(raw, default if raw not in TARGETS else raw)


class DeviceController:
    """PC / Android 双目标统一控制器"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config if config is not None else {}
        self.target: str = normalize_target(self.config.get("target", TARGET_PC))

        self.action_logs: List[Dict[str, Any]] = []
        self._locked = False

        self.pc = UnifiedController(self.config)
        self.android = AndroidADBController(self.config)

        # 共享同一份日志列表（两个子控制器内部各自按 50 条截断，写的是同一个 list）
        self.pc.action_logs = self.action_logs
        self.android.action_logs = self.action_logs
        self.pc.is_locked = False
        self.android.is_locked = False

        # 记录已生效的 adb 路径，配置变化时丢弃 adb 探测缓存
        self._adb_path_snapshot = str(self.config.get("adb_path") or "")

    # =========================================================================
    #  基础属性
    # =========================================================================

    @property
    def mode(self) -> str:
        """当前目标的模式名（PC: local/remote；Android: android）"""
        return self.pc.mode if self.target == TARGET_PC else "android"

    @property
    def active(self):
        return self.pc if self.target == TARGET_PC else self.android

    @property
    def connected(self) -> bool:
        return bool(self.active.connected)

    @property
    def idle_timeout(self) -> int:
        """当前目标的无操作自动断连时间（PC 用 idle_timeout，Android 用 adb_idle_timeout）"""
        return int(self.active.idle_timeout)

    @property
    def is_locked(self) -> bool:
        return self._locked

    @is_locked.setter
    def is_locked(self, value: bool):
        self._locked = bool(value)
        self.pc.is_locked = self._locked
        self.android.is_locked = self._locked

    def touch_activity(self):
        self.pc.touch_activity()
        self.android.touch_activity()

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
    #  目标切换
    # =========================================================================

    def _set_target(self, target: str):
        self.target = target
        try:
            self.config["target"] = target
        except Exception:
            pass

    async def _disconnect_target(self, target: str, reason: str, source: str) -> Dict[str, Any]:
        if target == TARGET_ANDROID:
            return await self.android.disconnect(reason=reason, source=source)
        return await self.pc.disconnect(reason=reason, source=source)

    async def switch_target(self, target: Any, auto_connect: bool = True) -> Dict[str, Any]:
        """切换控制目标；切换时断开旧目标连接，避免两套会话同时占用资源"""
        new_target = normalize_target(target, default=self.target)
        if new_target == self.target:
            if auto_connect and not self.connected:
                return await self.connect(source="switch")
            return await self.get_status()

        old_target = self.target
        if self.pc.connected or self.android.connected:
            await self._disconnect_target(
                old_target,
                reason=f"控制目标切换为 {TARGET_LABELS[new_target]}",
                source="switch",
            )
        self._set_target(new_target)
        self.record_log(
            "switch_target",
            f"{TARGET_LABELS[old_target]} → {TARGET_LABELS[new_target]}",
            source="webui",
        )
        if auto_connect:
            return await self.connect(source="switch")
        return await self.get_status()

    # =========================================================================
    #  配置更新
    # =========================================================================

    async def update_config_async(self, new_config: dict):
        old_target = self.target

        self.config.update(new_config or {})
        new_target = normalize_target(self.config.get("target", self.target), default=self.target)

        # 注意：调用方（main.py）通常已经先更新过 config，因此这里对比的是“上次生效值”而不是入参
        adb_path_now = str(self.config.get("adb_path") or "")
        if adb_path_now != self._adb_path_snapshot:
            self._adb_path_snapshot = adb_path_now
            self.android.invalidate_adb()

        await self.pc.update_config_async(self.config)

        if new_target != old_target:
            if self.pc.connected or self.android.connected:
                await self._disconnect_target(
                    old_target,
                    reason=f"控制目标切换为 {TARGET_LABELS[new_target]}",
                    source="config",
                )
            self._set_target(new_target)

        self.touch_activity()

    def update_config(self, new_config: dict):
        self.config.update(new_config or {})
        adb_path_now = str(self.config.get("adb_path") or "")
        if adb_path_now != self._adb_path_snapshot:
            self._adb_path_snapshot = adb_path_now
            self.android.invalidate_adb()
        self._set_target(normalize_target(self.config.get("target", self.target), default=self.target))
        self.pc.update_config(self.config)
        self.touch_activity()

    # =========================================================================
    #  连接 / 断开 / 状态
    # =========================================================================

    async def connect(self, source: str = "manual") -> Dict[str, Any]:
        if self.target == TARGET_ANDROID:
            await self.android.connect(source=source)
        else:
            await self.pc.connect(source=source)
        return await self.get_status()

    async def disconnect(self, reason: str = "手动断开连接", source: str = "manual") -> Dict[str, Any]:
        await self._disconnect_target(self.target, reason=reason, source=source)
        return await self.get_status()

    async def get_status(self, probe: bool = False) -> Dict[str, Any]:
        """聚合状态：当前目标 + 另一个目标的在线情况 + Android/ADB 环境快照"""
        if self.target == TARGET_ANDROID:
            try:
                await self.android.resolve_adb()
            except Exception:
                pass
            status = await self.android.get_status(probe=probe)
        else:
            status = await self.pc.get_status(probe=probe)

        status["target"] = self.target
        status["target_label"] = TARGET_LABELS.get(self.target, self.target)
        status["targets"] = list(TARGETS)
        status["version"] = PLUGIN_VERSION
        status["pc"] = {
            "mode": self.pc.mode,
            "connected": self.pc.connected,
            "idle_timeout": self.pc.idle_timeout,
        }
        android_info = self.android.describe()
        android_info["connected"] = self.android.connected
        android_info["is_locked"] = self.android.is_locked
        android_info["idle_timeout"] = self.android.idle_timeout
        status["android"] = android_info
        status["is_locked"] = self._locked
        return status

    async def ensure_adb_ready(self) -> Dict[str, Any]:
        """
        解析 adb 可执行文件（优先插件内置），并把实际路径**自动写回配置** `adb_path`，
        这样 WebUI 设置页无需手动填写就能看到当前使用的 adb。
        """
        try:
            await self.android.resolve_adb()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        changed = self.android.consume_auto_filled()
        info = self.android.describe()
        return {
            "ok": True,
            "path": info.get("adb_path"),
            "source": info.get("adb_source"),
            "version": info.get("adb_version"),
            "changed": changed,
        }

    async def close(self):
        """插件卸载：释放两个目标的所有资源"""
        try:
            await self.pc.close()
        except Exception:
            pass
        try:
            await self.android.close()
        except Exception:
            pass

    # =========================================================================
    #  截图与输入（统一转发到当前目标）
    # =========================================================================

    async def take_screenshot(
        self,
        quality: Optional[int] = None,
        source: str = "system",
        include_bytes: bool = True,
    ) -> Dict[str, Any]:
        return await self.active.take_screenshot(
            quality=quality, source=source, include_bytes=include_bytes)

    async def click(self, x: int, y: int, button: str = "left", clicks: int = 1,
                    source: str = "system") -> Dict[str, Any]:
        return await self.active.click(x=x, y=y, button=button, clicks=clicks, source=source)

    async def double_click(self, x: int, y: int, source: str = "system") -> Dict[str, Any]:
        return await self.active.double_click(x=x, y=y, source=source)

    async def right_click(self, x: int, y: int, source: str = "system") -> Dict[str, Any]:
        return await self.active.right_click(x=x, y=y, source=source)

    async def move(self, x: int, y: int, duration: float = 0.0, source: str = "system") -> Dict[str, Any]:
        return await self.active.move(x=x, y=y, duration=duration, source=source)

    async def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5,
                   button: str = "left", source: str = "system") -> Dict[str, Any]:
        return await self.active.drag(x1=x1, y1=y1, x2=x2, y2=y2,
                                      duration=duration, button=button, source=source)

    async def long_press(self, x: int, y: int, duration_ms: int = 700,
                         source: str = "system") -> Dict[str, Any]:
        if self.target == TARGET_ANDROID:
            return await self.android.long_press(x=x, y=y, duration_ms=duration_ms, source=source)
        # PC 模式没有长按语义，退化为单击
        res = await self.pc.click(x=x, y=y, button="left", clicks=1, source=source)
        res["note"] = "PC 模式不支持长按，已退化为单击"
        return res

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
                    source: str = "system") -> Dict[str, Any]:
        if self.target == TARGET_ANDROID:
            return await self.android.swipe(x1=x1, y1=y1, x2=x2, y2=y2,
                                            duration_ms=duration_ms, source=source)
        return await self.pc.drag(x1=x1, y1=y1, x2=x2, y2=y2,
                                  duration=max(0.1, duration_ms / 1000.0), source=source)

    async def swipe_direction(self, direction: str, amount: int = 3,
                              source: str = "system") -> Dict[str, Any]:
        if self.target == TARGET_ANDROID:
            return await self.android.swipe_direction(direction=direction, amount=amount, source=source)
        d = str(direction or "up").lower()
        if d in ("up", "上", "上滑"):
            return await self.pc.scroll(direction="down", amount=amount, source=source)
        if d in ("down", "下", "下滑"):
            return await self.pc.scroll(direction="up", amount=amount, source=source)
        raise RuntimeError("PC 模式不支持左右滑动，请使用拖拽（drag）或滚动（scroll）。")

    async def type_text(self, text: str, interval: float = 0.0, source: str = "system") -> Dict[str, Any]:
        return await self.active.type_text(text=text, interval=interval, source=source)

    async def press_key(self, key: str, source: str = "system") -> Dict[str, Any]:
        return await self.active.press_key(key=key, source=source)

    async def hotkey(self, keys: List[str], source: str = "system") -> Dict[str, Any]:
        return await self.active.hotkey(keys=keys, source=source)

    async def scroll(self, direction: str = "down", amount: int = 3,
                     x: Optional[int] = None, y: Optional[int] = None,
                     source: str = "system") -> Dict[str, Any]:
        return await self.active.scroll(direction=direction, amount=amount, x=x, y=y, source=source)

    # =========================================================================
    #  Android 专属能力（PC 目标下调用会给出明确提示）
    # =========================================================================

    def _require_android(self, action: str):
        if self.target != TARGET_ANDROID:
            raise RuntimeError(f"【{action}】仅支持 Android 目标，请先在左上角切换到 Android。")

    async def adb_devices(self, refresh: bool = True) -> List[Dict[str, str]]:
        return await self.android.devices(refresh=refresh)

    async def adb_pair(self, host: str, port: Any, code: str) -> Dict[str, Any]:
        return await self.android.pair(host=host, port=port, code=code)

    async def adb_connect(self, host: str, port: Any) -> Dict[str, Any]:
        return await self.android.adb_connect(host=host, port=port)

    async def adb_disconnect(self, serial: str = "") -> Dict[str, Any]:
        return await self.android.adb_disconnect(serial=serial)

    async def adb_shell(self, command: str, source: str = "system") -> Dict[str, Any]:
        self._require_android("ADB Shell")
        return await self.android.shell(command=command, source=source)

    async def adb_diagnostics(self) -> Dict[str, Any]:
        """诊断 adb 往返 / PNG / 设备端 JPEG 各阶段耗时（判断瓶颈是 RTT 还是带宽）"""
        self._require_android("ADB 诊断")
        return await self.android.adb_diagnostics()

    async def launch_app(self, package: str, source: str = "system") -> Dict[str, Any]:
        self._require_android("启动应用")
        return await self.android.launch_app(package=package, source=source)

    async def stop_app(self, package: str, source: str = "system") -> Dict[str, Any]:
        self._require_android("停止应用")
        return await self.android.stop_app(package=package, source=source)

    async def list_packages(self, third_party: bool = True, keyword: str = "") -> List[str]:
        self._require_android("应用列表")
        return await self.android.list_packages(third_party=third_party, keyword=keyword)

    async def current_app(self) -> str:
        self._require_android("前台应用")
        return await self.android.current_app()

    async def install_apk(self, apk_path: str, source: str = "system") -> Dict[str, Any]:
        self._require_android("安装 APK")
        return await self.android.install_apk(apk_path=apk_path, source=source)
