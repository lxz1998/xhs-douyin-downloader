# -*- coding: utf-8 -*-
"""抖音解析：分享文案 -> 无水印视频直链 / 图集图片直链。

2026-11-19 实测的三条路，只有第三条还活着：
  1. ✗ 分享页 window._ROUTER_DATA —— 只剩页面框架信息，作品数据已被移走
  2. ✗ 官方 iteminfo / aweme/detail 直连 —— 403 Blocked by ArgusSecurityPlugin
     （补上 ttwid 也没用，要的是完整浏览器指纹 + 动态签名）
  3. ✓ 真实浏览器打开视频页，拦它自己发出的 aweme/detail 响应
     —— 浏览器自带指纹和签名，返回的 video.play_addr 就是无水印直链
实测帧检查：play_addr 下载的 1080x1920 视频里没有任何抖音水印。
"""
import re

from . import browser
from .common import extract_url, get

ID_RE = re.compile(r"/(?:share/)?(video|note|slides|gallery)/(\d+)")
PLAYWM_RE = re.compile(r"/playwm/")


def _pick_url(url_list):
    """url_list 里优先取 https、优先不带水印标记的地址。"""
    if not url_list:
        return ""
    cands = [u for u in url_list if isinstance(u, str) and u.startswith("http")]
    if not cands:
        return ""
    no_water = [u for u in cands if "playwm" not in u and "watermark=1" not in u]
    return (no_water or cands)[0]


def resolve_id(share_text):
    """短链 -> aweme_id。纯 HTTP 就能做，不用起浏览器。"""
    url = extract_url(share_text)
    if not url:
        raise ValueError("没找到链接，把整段分享文案粘进来就行")
    if "douyin.com" not in url:
        raise ValueError("这不像抖音链接")

    resp = get(url, allow_redirects=True)
    final_url = resp.url or url
    m = ID_RE.search(final_url) or re.search(r"/(?:video|note|slides)/(\d+)", final_url)
    if not m:
        raise ValueError("链接里没解析出作品 ID（试试重新复制分享链接）")
    return m.group(2) if m.lastindex == 2 else m.group(1)


def parse(share_text):
    aweme_id = resolve_id(share_text)
    item = browser._browser.douyin_detail(aweme_id)

    title = (item.get("desc") or "").strip()
    author = ((item.get("author") or {}).get("nickname") or "").strip()

    video = item.get("video") or {}
    video_url = ""
    for key in ("play_addr", "play_addr_h264", "play_addr_265"):
        u = _pick_url((video.get(key) or {}).get("url_list"))
        if u:
            video_url = PLAYWM_RE.sub("/play/", u)
            break

    images = []
    for img in (item.get("images") or []):
        u = _pick_url(img.get("url_list"))
        if u:
            images.append(u)

    if not video_url and not images:
        raise ValueError("没取到视频或图片地址")

    return {
        "platform": "douyin",
        "kind": "video" if video_url else "images",
        "title": title,
        "author": author,
        "video_url": video_url,
        "images": images,
        "cover": _pick_url((video.get("cover") or {}).get("url_list") or []),
        "raw_id": aweme_id,
    }
