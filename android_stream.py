"""
Android 低延迟画面流 (AstrbotFishEyes - Screen Stream)
====================================================

为什么需要它
------------
`adb exec-out screencap -p` 每抓一帧都要：
  1. 启动一次 adb 进程；
  2. 建立 adb server → 设备 adbd 的连接（走 SSH 反向隧道时每步都是一个 RTT）；
  3. 把 **设备原始分辨率** 的 PNG（1080x2400 常见 0.5~2 MB）整包传回服务器。

在「手机 → SSH 反向隧道 → 服务器」这种链路上，带宽往往只有 1~3 Mbps，
于是一帧就要 3~6 秒，点一下要等半天。

本模块改成：
    adb exec-out screenrecord --output-format=h264 ...  →  ffmpeg 解码成 MJPEG
    （手机侧就完成缩放 + H.264 压缩，2 Mbps 下 540x1200 已经很清晰）

服务器只保留「最新一帧」的 JPEG，WebUI 轮询直接拿缓存帧，
每帧延迟从秒级降到 100~300 ms，隧道占用也从突发 1 MB 变成平稳 2 Mbps。

设计要点
--------
- **可选**：没有 ffmpeg / 设备不支持 screenrecord / 任何异常 → 自动回退到 PNG 截图模式；
- **可重启**：screenrecord 最长 180 秒，EOF 或超时后自动重开；
- **有看门狗**：连续拿不到帧会重启，多次失败后本次会话直接停用流模式；
- **纯 asyncio**：adb 与 ffmpeg 两个子进程用管道相连，读取 ffmpeg 的 stdout 解析 MJPEG。
"""

import asyncio
import os
import shutil
import subprocess
import sys
import time
from typing import List, Optional, Tuple

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# MJPEG 帧边界
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"


def find_ffmpeg(extra_path: str = "") -> str:
    """查找 ffmpeg：配置 > PATH > imageio-ffmpeg 自带"""
    extra_path = (extra_path or "").strip()
    if extra_path:
        if os.path.isfile(extra_path):
            return extra_path
        which = shutil.which(extra_path)
        if which:
            return which
    which = shutil.which("ffmpeg")
    if which:
        return which
    try:  # 可选依赖：pip install imageio-ffmpeg
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return ""


def even(value: int) -> int:
    """H.264 编码要求宽高为偶数"""
    v = int(value)
    return v if v % 2 == 0 else v - 1


class AndroidScreenStream:
    """把 `adb screenrecord` 的 H.264 流解码成最新一帧 JPEG 的常驻任务"""

    def __init__(
        self,
        adb_argv: List[str],
        decoder_argv: List[str],
        logger=None,
        restart_delay: float = 0.8,
    ):
        self._adb_argv = list(adb_argv)
        self._decoder_argv = list(decoder_argv)
        self._logger = logger
        self._restart_delay = restart_delay

        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._adb_proc = None
        self._dec_proc = None

        self._latest: Optional[bytes] = None
        self._latest_ts: float = 0.0
        self._frame_count: int = 0
        self._failures: int = 0
        self._last_error: str = ""
        self._started_at: float = 0.0
        self._disabled: bool = False

    # ------------------------------------------------------------------ 状态

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def last_error(self) -> str:
        return self._last_error

    def latest(self) -> Tuple[Optional[bytes], float]:
        return self._latest, self._latest_ts

    def describe(self) -> dict:
        age_ms = int((time.time() - self._latest_ts) * 1000) if self._latest_ts else None
        return {
            "running": self.running,
            "disabled": self._disabled,
            "frames": self._frame_count,
            "age_ms": age_ms,
            "error": self._last_error,
            "adb": " ".join(self._adb_argv[-6:]),
        }

    # ------------------------------------------------------------------ 生命周期

    async def start(self):
        if self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._supervise())

    async def stop(self):
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._latest = None
        self._latest_ts = 0.0
        await self._kill_procs()

    async def _kill_procs(self):
        for proc in (self._dec_proc, self._adb_proc):
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        for proc in (self._dec_proc, self._adb_proc):
            if proc is None:
                continue
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=3)
            except Exception:
                pass
        self._adb_proc = None
        self._dec_proc = None

    # ------------------------------------------------------------------ 主循环

    async def _supervise(self):
        """反复拉起 管道 + 读帧；连续失败则停用"""
        while self._stop_event and not self._stop_event.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._last_error = f"{type(e).__name__}: {e}"
            await self._kill_procs()
            if self._stop_event.is_set():
                break
            if self._disabled:
                break
            # 只要拿到过帧，失败计数重置，避免偶发 EOF（180s 上限）被当成故障
            if self._frame_count > 0 and self._latest_ts and (time.time() - self._latest_ts) < 5:
                self._failures = 0
            else:
                self._failures += 1
            if self._failures >= 3:
                self._disabled = True
                self._last_error = self._last_error or "连续 3 次未取到画面帧，已停用流模式"
                self._log("warn", f"流模式已停用：{self._last_error}")
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._restart_delay)
            except asyncio.TimeoutError:
                pass

    async def _run_once(self):
        """拉起一次 adb → ffmpeg 管道，读帧直到 EOF"""
        self._started_at = time.time()
        # 用 subprocess.Popen 直接对接管道：adb 的 stdout 直接作为 ffmpeg 的 stdin，
        # 数据在 OS 层流转，不经过 Python 复制（asyncio 的 StreamReader 无法作为 stdin 传入）。
        self._adb_proc = subprocess.Popen(
            self._adb_argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        self._dec_proc = subprocess.Popen(
            self._decoder_argv,
            stdin=self._adb_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        # 父进程不再需要 adb 的 stdout 句柄
        try:
            self._adb_proc.stdout.close()
        except Exception:
            pass

        await self._read_frames(self._dec_proc.stdout)
        await self._drain_stderr()

    async def _read_frames(self, stream):
        """在线程里阻塞读 ffmpeg 的 stdout，主协程解析 MJPEG"""
        loop = asyncio.get_running_loop()
        buf = bytearray()
        while True:
            if self._stop_event and self._stop_event.is_set():
                return
            try:
                chunk = await asyncio.wait_for(
                    loop.run_in_executor(None, stream.read, 65536), timeout=10.0)
            except asyncio.TimeoutError:
                # 10 秒没有数据：认为流卡住，交给上层重启
                self._last_error = "流数据超时（10s 无新数据）"
                return
            except Exception as e:  # noqa: BLE001
                self._last_error = f"读取画面失败: {e}"
                return
            if not chunk:
                return
            buf.extend(chunk)
            self._extract_frames(buf)
            if len(buf) > 8 * 1024 * 1024:  # 防御：异常数据不要无限增长
                buf.clear()

    def _extract_frames(self, buf: bytearray):
        while True:
            start = buf.find(_SOI)
            if start < 0:
                if len(buf) > 2:
                    del buf[:-2]
                return
            if start > 0:
                del buf[:start]
            end = buf.find(_EOI, 2)
            if end < 0:
                return
            frame = bytes(buf[: end + 2])
            del buf[: end + 2]
            self._latest = frame
            self._latest_ts = time.time()
            self._frame_count += 1

    async def _drain_stderr(self):
        parts: List[str] = []
        for proc, tag in ((self._adb_proc, "adb"), (self._dec_proc, "ffmpeg")):
            if proc is None or proc.stderr is None:
                continue
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(proc.stderr.read, 4096), timeout=1.0)
                text = (data or b"").decode("utf-8", errors="replace").strip()
                if text:
                    parts.append(f"{tag}: {text[:300]}")
            except Exception:
                pass
        if parts:
            self._last_error = " | ".join(parts)

    def _log(self, level: str, message: str):
        if self._logger is None:
            return
        try:
            getattr(self._logger, level if level in ("info", "warning", "error") else "info")(message)
        except Exception:
            pass


def build_screenrecord_argv(
    adb_argv: List[str],
    size: str = "auto",
    bitrate: int = 2000000,
    time_limit: int = 170,
    to_stdout: bool = True,
) -> List[str]:
    """构造 `adb exec-out screenrecord ...` 命令（to_stdout=False 时输出到文件，用于探测）"""
    argv = list(adb_argv) + [
        "exec-out",
        "screenrecord",
        "--output-format=h264",
        f"--bit-rate={int(bitrate)}",
        f"--time-limit={int(time_limit)}",
    ]
    if size and size.lower() != "auto":
        argv.append(f"--size={size}")
    argv.append("-" if to_stdout else "/data/local/tmp/.fisheyes_probe.h264")
    return argv


def build_ffmpeg_argv(
    ffmpeg: str,
    fps: int = 10,
    quality: int = 6,
    variant: int = 1,
) -> List[str]:
    """
    构造把 H.264 流解码成 MJPEG 的命令。

    历史坑：
    - `-probesize 32 -analyzeduration 0` → 裸 H.264 读不到 SPS/PPS，ffmpeg 直接退出；
    - `-fflags nobuffer -flags low_delay` → 部分 ffmpeg 版本会「读到数据但一帧不出」。
    因此这里给出 4 组参数，按「最保守 → 最激进」排列，由调用方逐个试，
    哪组能出帧就用哪组（并在日志里记下来）。
    """
    fps = max(1, int(fps))
    q = str(max(2, min(20, int(quality))))
    base = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
    if variant <= 1:
        # V1：最保守，只做解码 + 限帧 + MJPEG
        return base + ["-f", "h264", "-i", "pipe:0", "-an",
                       "-vf", f"fps={fps}", "-f", "mjpeg", "-q:v", q, "pipe:1"]
    if variant == 2:
        # V2：加 nobuffer（更低延迟，个别版本会出问题）
        return base + ["-fflags", "nobuffer", "-f", "h264", "-i", "pipe:0", "-an",
                       "-vf", f"fps={fps}", "-f", "mjpeg", "-q:v", q, "pipe:1"]
    if variant == 3:
        # V3：用输出帧率参数代替 fps 滤镜，显式指定 mjpeg 编码器
        return base + ["-f", "h264", "-i", "pipe:0", "-an",
                       "-c:v", "mjpeg", "-q:v", q, "-r", str(fps), "-f", "image2pipe", "pipe:1"]
    if variant == 5:
        # V5：给老版本 ffmpeg（apt 4.x/5.x）兜底：放宽 YUV range 的严格校验
        return base + ["-strict", "unofficial", "-f", "h264", "-i", "pipe:0", "-an",
                       "-vf", f"fps={fps}", "-f", "mjpeg", "-q:v", q, "pipe:1"]
    # V4：显式指定 mjpeg 编码器 + mjpeg 封装
    return base + ["-f", "h264", "-i", "pipe:0", "-an",
                   "-c:v", "mjpeg", "-q:v", q, "-vf", f"fps={fps}", "-f", "mjpeg", "pipe:1"]
