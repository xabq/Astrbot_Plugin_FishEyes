"""
一键将 Client 打包为单个独立的 Windows 可执行程序 (.exe)
生成后的 AstrBotScreenAgent.exe 免安装任何 Python 和依赖，即开即用！
"""

import os
import subprocess
import sys

# 避免 Windows 终端由于 GBK 编码输出特殊字符导致 UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def build():
    client_dir = os.path.dirname(os.path.abspath(__file__))
    entry_script = os.path.join(client_dir, "app.py")
    dist_dir = os.path.join(client_dir, "dist")
    build_dir = os.path.join(client_dir, "build")

    print("=" * 60)
    print("[*] 开始打包 AstrBot 屏幕被控端为单个可执行程序 (EXE)...")
    print(f"[*] 入口脚本: {entry_script}")
    print("=" * 60)

    # 优先使用当前运行本脚本的 Python 解释器
    py_exec = sys.executable

    # 构造 PyInstaller 参数
    cmd = [
        py_exec,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",                    # 单文件模式
        "--windowed",                   # 无控制台黑窗口
        "--name", "AstrBotScreenAgent",
        "--distpath", dist_dir,
        "--workpath", build_dir,
        "--hidden-import", "http.server",
        "--hidden-import", "paramiko",
        "--hidden-import", "pystray",
        "--hidden-import", "pystray._win32",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
        "--hidden-import", "PIL.ImageGrab",
        "--hidden-import", "PIL.ImageTk",
        "--hidden-import", "pyautogui",
        "--hidden-import", "pyscreeze",
        "--hidden-import", "pyperclip",
        entry_script,
    ]

    print("[*] 正在执行 PyInstaller 构建，请稍候...")
    ret = subprocess.run(cmd, cwd=client_dir)

    if ret.returncode == 0:
        exe_path = os.path.join(dist_dir, "AstrBotScreenAgent.exe")
        print("\n" + "=" * 60)
        print("[SUCCESS] 打包成功！")
        print(f"生成的独立可执行文件位于:\n  -> {exe_path}")
        print("被控端电脑仅需这一个 .exe 文件即可完整运行，无需安装任何环境！")
        print("=" * 60)
    else:
        print("\n[ERROR] 打包失败，请检查终端错误提示！")
        sys.exit(ret.returncode)


if __name__ == "__main__":
    build()
