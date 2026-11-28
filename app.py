# -*- coding: utf-8 -*-
"""去水印下载器 —— 本地/云端都能跑的单文件后端。

启动：
    python app.py                 # 默认 8080
    PORT=3000 python app.py       # 部署到云平台时平台会注入 PORT

接口：
    POST /api/parse   {"text": "整段分享文案或链接"}  -> 作品信息 + 媒体直链
    GET  /api/dl?u=..&name=..                          -> 代理下载单个文件（避开防盗链/CORS）
    POST /api/zip     {"urls": [..], "title": ".."}    -> 图集打包成一个 zip
"""
import io
import json
import os
import re
import threading
import time
import zipfile
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from parsers import douyin, xhs
from parsers.common import extract_urls, is_allowed_media_url, safe_filename

app = Flask(__name__, static_folder="static", static_url_path="")

MAX_ZIP_BYTES = 300 * 1024 * 1024   # 单次打包上限 300MB，防止被人当网盘
MAX_ZIP_FILES = 60

# 历史记录存在用户目录而不是项目目录：exe 单文件运行时 __file__ 在临时解压目录，
# 写那儿一关程序就没了。
HIST_MAX = 100
_HIST_LOCK = threading.Lock()
_hist_file = None


def _hist_path():
    global _hist_file
    if _hist_file is None:
        d = os.environ.get("WM_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".watermark")
        os.makedirs(d, exist_ok=True)
        _hist_file = os.path.join(d, "history.json")
    return _hist_file


def _load_hist():
    try:
        with open(_hist_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_hist(items):
    p = _hist_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items[:HIST_MAX], f, ensure_ascii=False)
    os.replace(tmp, p)


def record_history(data, src_url):
    """解析成功后记一条。只存元信息——直链有时效，存了也没用。

    同一作品重复解析（含点历史重解析）只留最新一条：按 raw_id 判同，
    没有 raw_id 时退回比链接原文。
    """
    entry = {
        "id": "%d%03d" % (int(time.time() * 1000), int(time.time() * 997) % 1000),
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "platform": data.get("platform") or "",
        "kind": data.get("kind") or "",
        "title": (data.get("title") or "（无标题）")[:120],
        "author": data.get("author") or "",
        "count": len(data.get("images") or []) if data.get("kind") == "images" else 1,
        "src_url": src_url or "",
        "raw_id": data.get("raw_id") or "",
    }
    with _HIST_LOCK:
        items = _load_hist()
        if entry["raw_id"]:
            items = [x for x in items
                     if not (x.get("platform") == entry["platform"]
                             and x.get("raw_id") == entry["raw_id"])]
        else:
            items = [x for x in items if (x.get("src_url") or "") != entry["src_url"]]
        items.insert(0, entry)
        try:
            _save_hist(items)
        except Exception:
            pass          # 写不进去不影响解析
    return entry

def media_headers(url):
    """CDN 会校验 Referer：抖音的只认 douyin，小红书的只认 xiaohongshu。"""
    u = (url or "").lower()
    if "douyin" in u or "bytedance" in u or "byteimg" in u or "zjcdn" in u:
        ref = "https://www.douyin.com/"
    else:
        ref = "https://www.xiaohongshu.com/"
    return {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                       "Mobile/15E148 Safari/604.1"),
        "Referer": ref,
    }


def detect_platform(text):
    t = (text or "").lower()
    if "douyin.com" in t:
        return douyin
    if "xiaohongshu.com" in t or "xhslink" in t:
        return xhs
    raise ValueError("只认抖音和小红书的链接，别平台的还没做")


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/parse")
def api_parse():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="先粘一条链接进来"), 400
    try:
        mod = detect_platform(text)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    try:
        data = mod.parse(text)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 422
    except Exception as e:                      # 网络/解析异常都给可读信息
        return jsonify(ok=False, error="解析失败：%s" % e), 500
    data["ok"] = True
    try:
        record_history(data, text[:600])
    except Exception:
        pass
    return jsonify(data)


@app.get("/api/history")
def api_history():
    return jsonify(ok=True, items=_load_hist())


@app.post("/api/history/del")
def api_history_del():
    body = request.get_json(silent=True) or {}
    hid = str(body.get("id") or "")
    if not hid:
        return jsonify(ok=False, error="缺少 id"), 400
    with _HIST_LOCK:
        items = [x for x in _load_hist() if x.get("id") != hid]
        _save_hist(items)
    return jsonify(ok=True, items=items)


@app.post("/api/history/clear")
def api_history_clear():
    with _HIST_LOCK:
        _save_hist([])
    return jsonify(ok=True)


@app.get("/api/dl")
def api_download():
    url = request.args.get("u", "")
    if not is_allowed_media_url(url):
        return jsonify(ok=False, error="不是允许的媒体地址"), 400
    name = request.args.get("name") or ""
    name = safe_filename(name, "media") if name else ""

    # Range 必须透传：小红书的视频是「moov 在文件末尾」的 mov，
    # 浏览器要 Range 跳到末尾读索引才能出画面。不透传就一直 readyState=0（2026-11-19 实测）。
    rng = request.headers.get("Range")
    try:
        r = requests_get_stream(url, extra_headers={"Range": rng} if rng else None)
    except Exception as e:
        return jsonify(ok=False, error="下载失败：%s" % e), 502

    ctype = r.headers.get("Content-Type", "application/octet-stream")
    headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes"}
    # 长度和分段信息原样带回，缺了它们播放器无法 seek，也会一直转圈
    for k in ("Content-Length", "Content-Range"):
        if r.headers.get(k):
            headers[k] = r.headers[k]
    if name:
        if not re.search(r"\.(mp4|jpe?g|png|webp|gif|heic|avif)$", name, re.I):
            name += guess_ext(ctype)
        # 中文文件名走 RFC 5987，不然浏览器会存成乱码
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''%s" % quote(name)
    else:
        headers["Content-Disposition"] = "inline"
    status = 206 if r.status_code == 206 else 200
    return Response(r.iter_content(64 * 1024), status=status, content_type=ctype, headers=headers)


@app.post("/api/zip")
def api_zip():
    body = request.get_json(silent=True) or {}
    urls = [u for u in (body.get("urls") or []) if is_allowed_media_url(u)]
    if not urls:
        return jsonify(ok=False, error="没有可打包的图片"), 400
    urls = urls[:MAX_ZIP_FILES]
    title = safe_filename(body.get("title") or "图集", "图集")

    buf = io.BytesIO()
    total = 0
    used = set()
    ok_count = 0
    failed = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, u in enumerate(urls, 1):
            try:
                r = requests_get_stream(u, timeout=30)
                data = r.content
                total += len(data)
                if total > MAX_ZIP_BYTES:
                    failed.append("第 %d 张后超出体积上限" % i)
                    break
                ext = sniff_ext(data, r.headers.get("Content-Type", ""))
                name = "%02d%s" % (i, ext)
                while name in used:
                    name = "_" + name
                used.add(name)
                z.writestr(name, data)
                ok_count += 1
            except Exception:
                failed.append("第 %d 张下载失败" % i)
    if ok_count == 0:
        return jsonify(ok=False, error="一张都没下下来：" + "；".join(failed[:3])), 502

    buf.seek(0)
    return send_file(
        buf, mimetype="application/zip", as_attachment=True,
        download_name="%s.zip" % title,
        max_age=0,
    )


def requests_get_stream(url, timeout=40, extra_headers=None):
    import requests
    h = media_headers(url)
    if extra_headers:
        h.update(extra_headers)
    r = requests.get(url, headers=h, timeout=timeout, stream=True)
    r.raise_for_status()
    return r


def sniff_ext(data, ctype=""):
    """先看文件头再信 Content-Type：小红书的原图 CDN 返回 application/octet-stream，
    只按头信息判断会把 6 张 jpg 全存成 .bin。

    HEIC 也带 ftyp，且位置和 mp4 一样（4:8）——必须先看品牌再判 mp4，
    否则 UHDR 原图会存成 .mp4（2026-11-28 实测：brand 是 heic）。
    """
    head = data[:12] if data else b""
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return ".heic"
        if brand in (b"avif", b"avis"):
            return ".avif"
        return ".mp4"
    return guess_ext(ctype)


def guess_ext(ctype):
    ctype = (ctype or "").lower()
    for key, ext in (("mp4", ".mp4"), ("jpeg", ".jpg"), ("jpg", ".jpg"),
                     ("png", ".png"), ("webp", ".webp"), ("gif", ".gif"),
                     ("heic", ".heic"), ("avif", ".avif")):
        if key in ctype:
            return ext
    return ".bin"


# ---- 跨域：网页在云端，解析在本机（127.0.0.1:8090）----
# 两个必须都满足，否则 Chromium 会拦：
# 1) 常规 CORS 预检；
# 2) Private Network Access：公网页面访问本地要返回 Allow-Private-Network。
@app.before_request
def _preflight():
    if request.method == "OPTIONS":
        return ("", 204)


@app.after_request
def _cors(resp):
    origin = request.headers.get("Origin", "")
    resp.headers["Access-Control-Allow-Origin"] = origin or "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    resp.headers["Vary"] = "Origin"
    return resp


@app.get("/api/diag")
def api_diag():
    """诊断：打开抖音页，返回浏览器实际看到的内容（只在排查云端风控时用）。"""
    import time as _t
    url = request.args.get("url") or "https://www.douyin.com/video/7702012274516512050"
    if "douyin.com" not in url:               # 不开放成任意 URL 抓取器
        return jsonify(ok=False, error="只允许 douyin.com"), 400
    try:
        from parsers.browser import _browser as B
        B.ensure()
        B._wait(B._send("Page.navigate", {"url": url}), 25)
        _t.sleep(10)
        expr = ("JSON.stringify({t:document.title,u:location.href,"
                "v:document.querySelectorAll('video').length,"
                "b:document.body.innerText.slice(0,400),"
                "n:document.documentElement.outerHTML.length})")
        r = B._wait(B._send("Runtime.evaluate", {"expression": expr, "returnByValue": True}), 20)
        val = (r.get("result") or {}).get("result", {}).get("value")
        return jsonify(ok=True, page=json.loads(val) if val else None)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.get("/api/ip")
def api_ip():
    """看云端出口 IP 归属，判断是不是机房 IP 被风控。"""
    try:
        r = requests_get_stream("https://api.ipify.org?format=json", timeout=10)
        return jsonify(json.loads(r.content.decode()))
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/quit")
def api_quit():
    """关掉自己。无窗口模式下没有黑窗口可关，只能从页面上点。"""
    import threading as _threading

    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify(ok=False, error="只允许本机调用"), 403

    def _bye():
        import time as _t
        _t.sleep(0.8)
        os._exit(0)

    _threading.Thread(target=_bye, daemon=True).start()
    return jsonify(ok=True, msg="服务已停止，可以关掉这个页面了")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
