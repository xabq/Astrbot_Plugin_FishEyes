"""
SSH 隧道管理器 (AstrbotFishEyes)

通过 SSH 端口转发，将本地可用端口映射到远程被控电脑上运行的 Agent 端口。
使用 asyncssh 实现全异步非阻塞连接。
"""

import asyncio
import os
import socket
from typing import Optional

try:
    import asyncssh
    HAS_ASYNCSSH = True
except ImportError:
    HAS_ASYNCSSH = False


class SSHTunnel:
    """SSH 端口转发隧道管理器"""

    def __init__(
        self,
        ssh_host: str,
        ssh_port: int = 22,
        ssh_username: str = "",
        ssh_password: str = "",
        ssh_key_path: str = "",
        remote_port: int = 8765,
        agent_token: str = "",
    ):
        self.ssh_host = (ssh_host or "").strip()
        self.ssh_port = int(ssh_port or 22)
        self.ssh_username = (ssh_username or "").strip()
        self.ssh_password = ssh_password or ""
        self.ssh_key_path = os.path.expanduser((ssh_key_path or "").strip()) if ssh_key_path else ""
        self.remote_port = int(remote_port or 8765)
        self.agent_token = (agent_token or "").strip()

        self._conn: Optional[object] = None
        self._listener: Optional[object] = None
        self._local_port: int = 0
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        if not self._connected or self._conn is None:
            return False
        if hasattr(self._conn, "is_closed"):
            is_closed = self._conn.is_closed
            if callable(is_closed):
                if is_closed():
                    return False
            elif is_closed:
                return False
        return True

    @property
    def local_port(self) -> int:
        return self._local_port

    @property
    def base_url(self) -> str:
        if self._local_port:
            return f"http://127.0.0.1:{self._local_port}"
        return ""

    def matches_config(self, cfg: dict) -> bool:
        """检查当前隧道配置是否与最新配置一致"""
        key_expanded = os.path.expanduser((cfg.get("ssh_key_path") or "").strip()) if cfg.get("ssh_key_path") else ""
        return (
            self.ssh_host == (cfg.get("ssh_host") or "").strip()
            and self.ssh_port == int(cfg.get("ssh_port") or 22)
            and self.ssh_username == (cfg.get("ssh_username") or "").strip()
            and self.ssh_password == (cfg.get("ssh_password") or "")
            and self.ssh_key_path == key_expanded
            and self.remote_port == int(cfg.get("remote_port") or 8765)
        )

    def _find_free_port(self) -> int:
        """获取一个本地随机可用端口"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _get_connect_kwargs(self) -> dict:
        kwargs = {
            "host": self.ssh_host,
            "port": self.ssh_port,
            "username": self.ssh_username,
            "known_hosts": None,  # 允许连接未知主机（插件环境友好）
            "connect_timeout": 15,
            "keepalive_interval": 15,
        }
        if self.ssh_key_path and os.path.exists(self.ssh_key_path):
            kwargs["client_keys"] = [self.ssh_key_path]
            if self.ssh_password:
                kwargs["passphrase"] = self.ssh_password
        elif self.ssh_password:
            kwargs["password"] = self.ssh_password
        return kwargs

    async def connect(self) -> bool:
        """建立 SSH 连接并创建本地端口转发 (使用 asyncssh.connect 而非返回元组的 create_connection)"""
        if not HAS_ASYNCSSH:
            raise RuntimeError("未安装 asyncssh 依赖，请在终端执行 pip install asyncssh")

        if not self.ssh_host or not self.ssh_username:
            raise ValueError("SSH 连接信息不完整 (缺少 ssh_host 或 ssh_username)")

        async with self._lock:
            await self._disconnect_internal()
            self._local_port = self._find_free_port()
            try:
                self._conn = await asyncssh.connect(**self._get_connect_kwargs())
                self._listener = await self._conn.forward_local_port(
                    "127.0.0.1",
                    self._local_port,
                    "127.0.0.1",
                    self.remote_port,
                )
                # 若自动分配了端口则同步更新
                if hasattr(self._listener, "get_port"):
                    actual_port = self._listener.get_port()
                    if actual_port:
                        self._local_port = actual_port
                self._connected = True
                return True
            except Exception as e:
                await self._disconnect_internal()
                raise RuntimeError(f"SSH 隧道连接失败 ({self.ssh_host}:{self.ssh_port}): {e}") from e

    async def ensure_connected(self) -> bool:
        """确保 SSH 隧道处于连接状态"""
        if self.connected:
            return True
        return await self.connect()

    async def disconnect(self):
        """外部断开调用"""
        async with self._lock:
            await self._disconnect_internal()

    async def _disconnect_internal(self):
        """关闭 SSH 连接与隧道"""
        self._connected = False
        if self._listener:
            try:
                self._listener.close()
                await self._listener.wait_closed()
            except Exception:
                pass
            self._listener = None

        if self._conn:
            try:
                self._conn.close()
                await self._conn.wait_closed()
            except Exception:
                pass
            self._conn = None
        self._local_port = 0

    def __repr__(self) -> str:
        status = "已连接" if self.connected else "未连接"
        return f"<SSHTunnel {self.ssh_username}@{self.ssh_host}:{self.ssh_port} -> 127.0.0.1:{self._local_port} [{status}]>"
