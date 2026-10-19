import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import Tk, messagebox

APP_TITLE = "医析台"
PREFERRED_PORT = 8501
# 内层程序启动后会把实际监听端口写在这里（端口被占用时会自动改用 8502、8503…）
PORT_FILE = Path.home() / "医析台端口.txt"


def show_message(kind: str, message: str) -> None:
    root = Tk()
    root.withdraw()
    getattr(messagebox, kind)(APP_TITLE, message)
    root.destroy()


def show_error(message: str) -> None:
    show_message("showerror", message)


def is_streamlit_running(port: int) -> bool:
    """端口上是否已有本程序（Streamlit）在运行。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            head = response.read(4096).decode("utf-8", "ignore").lower()
    except Exception:
        return False
    return "streamlit" in head


def read_reported_port(timeout: float = 40.0):
    """读取内层程序写入的实际端口。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = PORT_FILE.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        except Exception:
            pass
        time.sleep(0.3)
    return None


def main() -> int:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    payload_root = bundle_root / "payload"
    application = payload_root / "医析台.exe"

    if not application.is_file():
        show_error("应用资源不完整，无法启动。请重新获取完整的单文件程序。")
        return 1

    # 已经在运行：直接打开已有窗口。否则第二份实例会因端口被占用而静默退出，
    # 用户会误以为「双击没反应」或「更新没生效」。
    if is_streamlit_running(PREFERRED_PORT):
        webbrowser.open(f"http://127.0.0.1:{PREFERRED_PORT}")
        show_message(
            "showinfo",
            f"程序已在运行，已为你打开现有窗口（端口 {PREFERRED_PORT}）。\n\n"
            "如果需要使用刚更新过的版本，请先在任务栏关闭正在运行的程序，再重新双击。",
        )
        return 0

    try:
        internal_root = payload_root / "_internal"
        child_env = os.environ.copy()
        dll_paths = [internal_root]
        child_env["PATH"] = os.pathsep.join(
            str(path) for path in dll_paths if path.is_dir()
        ) + os.pathsep + child_env.get("PATH", "")

        # 只监听本机回环地址：既不触发 Windows 防火墙「允许访问网络」提示，
        # 也避免局域网内其他机器访问到本机患者数据。
        child_env["STREAMLIT_SERVER_ADDRESS"] = "127.0.0.1"
        child_env["STREAMLIT_SERVER_HEADLESS"] = "true"
        # 关闭 Streamlit 匿名使用统计上报，保证运行信息零外传。
        child_env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"

        try:
            PORT_FILE.unlink()
        except OSError:
            pass

        process = subprocess.Popen(
            [str(application)],
            cwd=str(payload_root),
            env=child_env,
        )
        port = read_reported_port() or PREFERRED_PORT
        address = f"http://127.0.0.1:{port}"
        for _ in range(180):
            if process.poll() is not None:
                show_error(
                    "程序启动失败。可查看用户目录下的「医析台启动日志.txt」了解详情。"
                )
                return process.returncode or 1
            if is_streamlit_running(port):
                webbrowser.open(address)
                break
            time.sleep(0.5)
        return process.wait()
    except OSError as error:
        show_error(f"应用启动失败：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
