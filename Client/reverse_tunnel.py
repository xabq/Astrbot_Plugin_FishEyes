"""
反向 SSH 隧道模块 (AstrbotFishEyes - Reverse SSH Tunnel)

让被控电脑（处于内网、家庭宽带、无公网 IP）主动向远程 AstrBot 服务器发起 SSH 连接，
并将服务器上的指定端口转发回本机的 Agent 服务端口。
实现免公网 IP、免端口映射、穿透 NAT 的远程电脑控制。
"""

import os
import select
import socket
import sys
import threading
import time
from typing import Callable, Optional

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


class ReverseSSHTunnel:
    def __init__(self):
        self.is_running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._active_client = None
        self.log_callback: Optional[Callable[[str], None]] = None
        self.status_callback: Optional[Callable[[str], None]] = None

    def log(self, message: str, level: str = "INFO"):
        if self.log_callback:
            try:
                self.log_callback(f"[{time.strftime('%H:%M:%S')}] [SSH隧道] {message}")
            except Exception:
                pass
        else:
            try:
                print(f"[SSH Tunnel {level}] {message}")
            except Exception:
                pass

    def _set_status(self, status: str):
        if self.status_callback:
            try:
                self.status_callback(status)
            except Exception:
                pass

    def start(
        self,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        ssh_password: str = "",
        ssh_key_path: str = "",
        remote_port: int = 8765,
        local_port: int = 8765,
        log_callback=None,
        status_callback=None,
    ):
        if self.is_running:
            return

        self.log_callback = log_callback
        self.status_callback = status_callback

        if not HAS_PARAMIKO:
            self.log("未安装 paramiko 库，无法启动反向 SSH 隧道！请执行 pip install paramiko", "ERROR")
            return

        ssh_host = (ssh_host or "").strip()
        ssh_user = (ssh_user or "").strip()
        ssh_key_expanded = os.path.expanduser((ssh_key_path or "").strip()) if ssh_key_path else ""

        if not ssh_host or not ssh_user:
            self.log("SSH 主机地址或用户名为空，取消启动反向隧道", "WARN")
            return

        self._stop_event.clear()
        self.is_running = True

        def _tunnel_loop():
            self._set_status("connecting")
            while not self._stop_event.is_set():
                client = None
                try:
                    self.log(f"正在连接 SSH 服务器: {ssh_user}@{ssh_host}:{ssh_port}...")
                    client = paramiko.SSHClient()
                    self._active_client = client
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                    connect_kwargs = {
                        "hostname": ssh_host,
                        "port": int(ssh_port or 22),
                        "username": ssh_user,
                        "timeout": 15,
                        "banner_timeout": 15,
                    }

                    if ssh_key_expanded and os.path.exists(ssh_key_expanded):
                        connect_kwargs["key_filename"] = ssh_key_expanded
                        if ssh_password:
                            connect_kwargs["passphrase"] = ssh_password
                    elif ssh_password:
                        connect_kwargs["password"] = ssh_password

                    client.connect(**connect_kwargs)
                    transport = client.get_transport()
                    if transport is None:
                        raise RuntimeError("无法获取 SSH Transport 会话")
                    transport.set_keepalive(15)

                    self.log(f"SSH 连接成功！正在向服务器申请反向端口映射 : {remote_port} -> 本机 127.0.0.1:{local_port}")
                    transport.request_port_forward("", int(remote_port))
                    self._set_status("connected")
                    self.log(f"✅ 反向隧道已就绪！服务器访问 http://127.0.0.1:{remote_port} 即可直达本机", "SUCCESS")

                    while not self._stop_event.is_set():
                        chan = transport.accept(1.0)
                        if chan is None:
                            if not transport.is_active():
                                raise RuntimeError("SSH Transport 处于非活动状态 (连接断开)")
                            continue
                        thr = threading.Thread(
                            target=self._handler,
                            args=(chan, "127.0.0.1", int(local_port)),
                            daemon=True,
                        )
                        thr.start()

                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    self._set_status("error")
                    self.log(f"SSH 隧道断开或连接失败: {e}，5秒后尝试自动重连...", "ERROR")
                    self._stop_event.wait(5.0)
                finally:
                    self._active_client = None
                    if client:
                        try:
                            client.close()
                        except Exception:
                            pass

            self._set_status("disconnected")
            self.is_running = False
            self.log("反向 SSH 隧道已停止", "INFO")

        self._thread = threading.Thread(target=_tunnel_loop, daemon=True, name="ReverseSSHTunnel")
        self._thread.start()

    def _handler(self, chan, local_host, local_port):
        """双向管道数据转发"""
        sock = socket.socket()
        try:
            sock.settimeout(5.0)
            sock.connect((local_host, local_port))
            sock.settimeout(None)
        except Exception as err:
            self.log(f"转发连接到本地 Agent 端口 {local_port} 失败: {err}", "ERROR")
            try:
                chan.close()
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
            return

        try:
            while not self._stop_event.is_set():
                r, _w, _x = select.select([sock, chan], [], [], 1.0)
                if self._stop_event.is_set():
                    break
                if sock in r:
                    data = sock.recv(4096)
                    if len(data) == 0:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(4096)
                    if len(data) == 0:
                        break
                    sock.sendall(data)
        except Exception:
            pass
        finally:
            try:
                chan.close()
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def stop(self):
        if not self.is_running and self._active_client is None:
            return
        self.log("正在关闭反向 SSH 隧道...", "INFO")
        self._stop_event.set()
        self.is_running = False
        client = self._active_client
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
