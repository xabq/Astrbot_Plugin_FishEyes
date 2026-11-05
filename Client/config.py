"""
Client 配置文件持久化管理
"""

import json
import os
import sys

DEFAULT_CONFIG = {
    # 本地 Agent HTTP 服务
    "host": "0.0.0.0",
    "port": 8765,
    "token": "",
    "auto_start": False,           # 默认不自动启动服务
    "start_minimized": False,      # 默认不自动后台托盘托管
    "auto_launch_boot": False,     # 默认不开机自启

    # 反向 SSH 隧道设置
    "enable_reverse_tunnel": False,
    "ssh_host": "",
    "ssh_port": 22,
    "ssh_user": "",
    "ssh_password": "",
    "ssh_key_path": "",
    "remote_forward_port": 8765,  # 在服务器上暴露转发的端口
}


def get_config_path() -> str:
    """获取配置文件路径 (优先与可执行文件同目录)"""
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    config_file = os.path.join(base_dir, "config.json")
    try:
        # 测试当前目录是否可写
        test_file = os.path.join(base_dir, ".perm_test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        return config_file
    except Exception:
        # 回退至 AppData 目录
        app_data = os.environ.get("APPDATA") or os.path.expanduser("~")
        target_dir = os.path.join(app_data, "AstrBotScreenAgent")
        os.makedirs(target_dir, exist_ok=True)
        return os.path.join(target_dir, "config.json")


def load_config() -> dict:
    """加载配置"""
    path = get_config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                cfg = dict(DEFAULT_CONFIG)
                cfg.update(data)
                return cfg
        except Exception as e:
            print(f"[Config] 加载配置文件失败，将使用默认值: {e}")
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> bool:
    """保存配置"""
    path = get_config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[Config] 保存配置文件失败: {e}")
        return False
