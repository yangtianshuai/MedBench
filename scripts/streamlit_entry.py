from pathlib import Path
import socket
import sys
import traceback
from urllib import request

from streamlit import config
from streamlit.web import bootstrap

# 实际监听端口写在这里，供单文件启动器读取（端口被占用时会自动改用 8502、8503…）
PORT_FILE = Path.home() / "医析台端口.txt"


def is_streamlit_running(port: int) -> bool:
    """该端口上是否已有（本程序启动的）Streamlit 在运行。"""
    try:
        with request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            head = response.read(4096).decode("utf-8", "ignore").lower()
    except Exception:
        return False
    return "streamlit" in head


def pick_free_port(preferred: int = 8501, limit: int = 10) -> int:
    """从首选端口开始找一个能绑定的端口，避免因 8501 被占用而启动失败。"""
    for port in range(preferred, preferred + limit):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


if __name__ == "__main__":
    log_path = Path.home() / "医析台启动日志.txt"
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    app_path = bundle_root / "app.py"
    modules_path = bundle_root / "modules"
    if modules_path.is_dir():
        sys.path.insert(0, str(modules_path))
    try:
        port = pick_free_port()
        PORT_FILE.write_text(str(port), encoding="utf-8")
        config.set_option("global.developmentMode", False)
        config.set_option("server.headless", True)
        config.set_option("server.port", port)
        # 只监听本机回环地址：既不触发 Windows 防火墙「允许访问网络」提示，
        # 也避免局域网内其他机器访问到本机患者数据。
        config.set_option("server.address", "127.0.0.1")
        # 关闭 Streamlit 匿名使用统计上报，保证运行信息零外传。
        config.set_option("browser.gatherUsageStats", False)
        log_path.write_text(
            f"bundle_root={bundle_root}\napp_path={app_path}\nexists={app_path.is_file()}\nport={port}\n",
            encoding="utf-8",
        )
        bootstrap.run(
            str(app_path),
            False,
            [],
            {"server.headless": True, "server.port": port,
             "server.address": "127.0.0.1", "browser.gatherUsageStats": False},
        )
    except Exception:
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise
