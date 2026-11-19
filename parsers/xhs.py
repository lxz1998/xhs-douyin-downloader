# -*- coding: utf-8 -*-
"""小红书解析：分享文案 -> 原图直链 / 视频直链。

原理（2026-11 实测与社区踩坑记录）：
  1. xhslink.cn / xhslink.com 短链返回 **HTTP 200 + HTML 跳转指针**（不是 302），
     所以不能靠 allow_redirects 跟随，要用正则从 body 里抠目标地址。
  2. 笔记页是 React SSR，数据挂在 window.__INITIAL_STATE__，
     路径 note.noteDetailMap[noteId].note（老版本是 noteData.data.noteData）。
  3. 该 JSON 里常出现 JS 的 undefined（非法 JSON），要先换成 null。
  4. 图片：imageList[].urlDefault，去掉 `!` 之后的处理参数即为原图。
  5. 视频：video.consumer.originVideoKey -> sns-video-bd.xhscdn.com/{key}；
     兜底用 media.stream 里最高档的 masterUrl。
受限：匿名 SSR 拿不到作者/互动数（这些要登录），所以这两项可能为空；
      视频匿名多为中码率，不是原画质；直链带签名，过期快，要立刻下载。
"""
import re

from .common import extract_json_after, extract_url, get

SHORT_HOSTS = ("xhslink.cn", "xhslink.com", "xhslink.net")
NOTE_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-zA-Z]+)")


def _resolve_short(url):
    """xhslink 短链：200 + HTML 指针，取出真正的小红书笔记地址。"""
    r = get(url, allow_redirects=False)
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("Location") or ""
        if "xiaohongshu.com" in loc:
            return loc
    html = r.text or ""
    m = re.search(r'https?://www\.xiaohongshu\.com/[^"\'<>\s]+', html)
    if m:
        return m.group(0).replace("&amp;", "&")
    # 有些版本是 JS 跳转
    m = re.search(r'location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', html)
    if m and "xiaohongshu.com" in m.group(1):
        return m.group(1)
    return url


def _clean_img(u):
    """去掉 CDN 的缩放/格式后缀，得到原图。"""
    if not u:
        return ""
    u = u.split("!")[0]
    u = u.replace("//sns-img-qc", "//sns-img-bd").replace("sjp", "sns-webpic-qc")
    return u


def _img_urls(img):
    """返回一张图的候选地址（按优先级）。

    2026-11 实测：带 !h5_1080jpg 的只有 225KB（1080 宽），
    而 sns-img-bd/{fileId} 是 1.19MB 的真原图；ci.xiaohongshu.com 是 282KB 的转换版。
    所以优先 sns-img-bd，失败再退 ci。
    """
    fid = (img.get("fileId") or "").strip()
    if fid:
        return ["https://sns-img-bd.xhscdn.com/%s" % fid,
                "https://ci.xiaohongshu.com/%s?imageView2/format/jpeg" % fid]
    for key in ("urlDefault", "url", "urlPre"):
        u = _clean_img(img.get(key))
        if u:
            return [u]
    for info in (img.get("infoList") or []):
        u = _clean_img(info.get("url"))
        if u:
            return [u]
    return []


def _find_note(state, note_id):
    """在 __INITIAL_STATE__ 里定位到当前笔记对象。

    2026-11 实测结构：state.noteData.data.noteData.imageList / video
    （不是社区文档里写的 note.noteDetailMap，那条路径已经不存在了）
    """
    try:
        note = state["noteData"]["data"]["noteData"]
        if isinstance(note, dict) and note:
            return note
    except (KeyError, TypeError):
        pass
    # 老结构兜底
    note = (state or {}).get("note") or {}
    detail = note.get("noteDetailMap") or {}
    if note_id and detail.get(note_id):
        item = detail[note_id]
        return item.get("note") or item
    for v in detail.values():
        if isinstance(v, dict) and v.get("note"):
            return v["note"]
    return None


def _best_video(note):
    """优先取原始视频 key，兜底取码率最高的一档。"""
    video = note.get("video") or {}
    key = ((video.get("consumer") or {}).get("originVideoKey") or "").strip()
    if key:
        return "https://sns-video-bd.xhscdn.com/" + key
    stream = ((video.get("media") or {}).get("stream") or {})
    best, best_score = "", -1
    for codec in ("h264", "h265", "av1"):
        for item in (stream.get(codec) or []):
            url = item.get("masterUrl") or ((item.get("backupUrls") or [""])[0])
            if not url:
                continue
            score = (item.get("width") or 0) * (item.get("height") or 0)
            if score > best_score:
                best, best_score = url, score
    return best


def parse(share_text):
    url = extract_url(share_text)
    if not url:
        raise ValueError("没找到链接，把整段分享文案粘进来就行")

    if any(h in url for h in SHORT_HOSTS):
        url = _resolve_short(url)

    m = NOTE_RE.search(url)
    if not m:
        raise ValueError("链接里没解析出笔记 ID（建议用 App 里的「分享 → 复制链接」）")
    note_id = m.group(1)

    page = get(url, referer="https://www.xiaohongshu.com/")
    html = page.text or ""
    state = extract_json_after(html, "window.__INITIAL_STATE__")
    if not state:
        raise ValueError("页面里没找到笔记数据（可能需要带 xsec_token 的分享链接）")

    note = _find_note(state, note_id)
    if not note:
        raise ValueError("这条笔记可能是私密/已删除，或平台改了页面结构")

    title = (note.get("title") or note.get("desc") or "").strip()
    user = note.get("user") or {}
    author = (user.get("nickName") or user.get("nickname") or "").strip()

    images = []
    for img in (note.get("imageList") or []):
        cands = _img_urls(img)
        if cands:
            images.append(cands[0])

    video_url = ""
    if (note.get("type") or "") == "video" or note.get("video"):
        video_url = _best_video(note)

    # 图文笔记里也可能混着 live photo（动图），先按普通图处理，不额外拉视频
    if not images and not video_url:
        raise ValueError("没取到图片或视频地址")

    return {
        "platform": "xhs",
        "kind": "video" if video_url else "images",
        "title": title,
        "author": author,
        "video_url": video_url,
        "images": images,
        "cover": images[0] if images else "",
        "raw_id": note_id,
    }
