# -*- coding: utf-8 -*-
"""长驻无头浏览器：抖音的详情接口要完整浏览器指纹 + 动态签名，纯 HTTP 走不通，
所以复用同一个 Chrome 实例，每次解析只做 navigate + 拦响应。

设计取舍：
  - 不做并发池。这是一个几个人自用的小工具，串行解析（加锁）足够，
    起多个浏览器反而吃内存。
  - 浏览器懒启动：第一次解析抖音时才起，没人用抖音就不占内存。
  - 端口固定：避免和用户自己的 9222（店铺登录态）冲突。
"""
import atexit
import json
import os
import subprocess
import tempfile
import threading
import time

import websocket

CHROME_CANDIDATES = [
    os.path.expanduser(r"~/.workbuddy/browsers/chrome-win64/chrome.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")


def _find_chrome():
    env = os.environ.get("CHROME_PATH")
    if env and os.path.isfile(env):
        return env
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise RuntimeError("找不到 Chrome。装一个 Chrome，或用环境变量 CHROME_PATH 指定路径")


class Browser:
    """一个浏览器实例 + 一把锁，保证同一时刻只有一个解析在跑。"""

    def __init__(self, port=9336):
        self.port = port
        self.proc = None
        self.ws = None
        self.mid = 0
        self.lock = threading.Lock()
        self.profile = None

    # ---------- 生命周期 ----------
    def ensure(self):
        if self.ws is not None:
            try:
                self._send("Browser.getVersion")
                return
            except Exception:
                self.close()

        chrome = _find_chrome()
        self.profile = tempfile.mkdtemp(prefix="wm-chrome-")
        self.proc = subprocess.Popen([
            chrome, "--headless=new", "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=" + self.profile, "--no-first-run",
            "--no-default-browser-check", "--disable-gpu",
            # 容器里通常是 root，不加 --no-sandbox 直接启动失败
            "--no-sandbox", "--disable-dev-shm-usage",
            # 新版 Chromium 会拒绝来自 127.0.0.1 的 CDP 连接，必须显式放开
            "--remote-allow-origins=*",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,900", "--mute-audio",
            "--user-agent=" + UA,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for _ in range(60):
            try:
                self._list_pages()
                break
            except Exception:
                time.sleep(0.25)
        else:
            self.close()
            raise RuntimeError("浏览器启动超时")

        page = next(t for t in self._list_pages() if t.get("type") == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
        self._send("Page.enable")
        self._send("Network.enable", {"maxTotalBufferSize": 64 * 1024 * 1024})

    def close(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

    # ---------- CDP 基础 ----------
    def _url(self, path):
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=10) as r:
            return json.loads(r.read().decode())

    def _list_pages(self):
        return self._url("/json/list")

    def _send(self, method, params=None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
        return self.mid

    def _wait(self, want_id, timeout=20, collect=None):
        """等某条命令的响应；途中把 Network.responseReceived 收进 collect。"""
        end = time.time() + timeout
        self.ws.settimeout(1)
        while time.time() < end:
            try:
                m = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if m.get("id") == want_id:
                return m
            if collect is not None and m.get("method") == "Network.responseReceived":
                collect(m["params"])
        return None

    # ---------- 抖音详情 ----------
    def douyin_detail(self, aweme_id, timeout=28):
        """打开视频页，拦它自己发出的 aweme/detail 响应，返回 aweme_detail 字典。

        两个必须的细节（都是实测踩出来的）：
          1. 解析前先回 about:blank：抖音是 SPA，同站从 A 视频跳到 B 视频
             不会重新发 detail 请求，会直接复用上一页的状态。
          2. 收到 responseReceived 就立刻取 body：Chrome 会释放较早的响应体，
             等循环结束再取，第二次解析就拿到空字符串。
        """
        self.ensure()
        try:
            self._wait(self._send("Page.navigate", {"url": "about:blank"}), 5)
        except Exception:
            pass
        time.sleep(0.3)

        nav = self._send("Page.navigate", {"url": "https://www.douyin.com/video/%s" % aweme_id})
        self._wait(nav, 20)

        end = time.time() + timeout
        self.ws.settimeout(1)
        while time.time() < end:
            try:
                m = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if m.get("method") != "Network.responseReceived":
                continue
            if "aweme/detail" not in m["params"]["response"]["url"]:
                continue
            req_id = m["params"]["requestId"]
            body = self._body(req_id)
            if not body:
                continue
            try:
                data = json.loads(body)
            except ValueError:
                continue
            item = data.get("aweme_detail") or {}
            if not item and data.get("aweme_details"):
                item = data["aweme_details"][0]
            if item:
                return item
        raise RuntimeError("没拿到详情（可能是私密作品，或这轮被风控拦了，重试一次）")

    def _body(self, req_id, timeout=12):
        res = self._wait(self._send("Network.getResponseBody", {"requestId": req_id}), timeout) or {}
        return (res.get("result") or {}).get("body") or ""


_browser = Browser()
atexit.register(_browser.close)
