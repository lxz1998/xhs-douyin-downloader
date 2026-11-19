# -*- coding: utf-8 -*-
"""无窗口启动器（用 pythonw 跑，不弹黑窗口）。

做三件事：
1. 挑一个空闲端口（8090 被占就往后找，不再直接失败）
2. 起 Flask
3. **等端口真的通了**才开浏览器 —— 之前是浏览器先开、服务后起，会看到「无法访问」

参数：
    --no-browser    只起服务、不开浏览器（开机自启用这个，否则每次开机弹一个页面）
"""
import os
import socket
import sys
import threading
import time
import webbrowser

# app.py 里的 static 是相对路径，先切到脚本所在目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def free_port(start=8090, tries=30):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


PORT = free_port()


def open_when_ready():
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
                webbrowser.open("http://127.0.0.1:%d" % PORT)
                return
        except OSError:
            time.sleep(0.2)
    webbrowser.open("http://127.0.0.1:%d" % PORT)


if "--no-browser" not in sys.argv:
    threading.Thread(target=open_when_ready, daemon=True).start()

from app import app  # noqa: E402  （必须在 chdir 之后）

app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
