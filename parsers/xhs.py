# -*- coding: utf-8 -*-
"""小红书解析：分享文案 -> 原图直链 / 视频直链。

原理（2026-11 实测与社区踩坑记录）：
  1. xhslink.cn / xhslink.com 短链返回 **HTTP 200 + HTML 跳转指针**（不是 302），
     所以不能靠 allow_redirects 跟随，要用正则从 body 里抠目标地址。
  2. 笔记页是 React SSR，数据挂在 window.__INITIAL_STATE__，
     路径 note.noteDetailMap[noteId].note（老版本是 noteData.data.noteData）。
  3. 该 JSON 里常出现 JS 的 undefined（非法 JSON），要先换成 null。
  4. 图片：imageList[].urlDefault，去掉 `!` 之后的处理参数即为原图。
  5. 视频：video.consumer.originVideoKey -> sns-video-bd.xhscdn.com/{key}（原画）；
     media.stream 里还有 h264/h265 的转码档，体积小一个数量级，见 _video_variants。
受限：匿名 SSR 拿不到作者/互动数（这些要登录），所以这两项可能为空；
      直链带签名，过期快，要立刻下载。
"""
import json
import re

from .common import extract_json_after, extract_url, get, head_size

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
    # 只替换域名段：参数里出现 sjp 这类子串不能动，否则签名会被改坏
    m = re.match(r"(https?://)([^/]+)(.*)$", u, re.S)
    if m:
        host = m.group(2).replace("sns-img-qc", "sns-img-bd").replace("sjp", "sns-webpic-qc")
        u = m.group(1) + host + m.group(3)
    return u


def _img_urls(img):
    """返回一张图的候选地址（按优先级）。

    2026-11-28 实测（带登录 cookie，同一条笔记）：
      sns-webpic-qc（urlDefault/urlPre，带不带 !nd_* 后缀）→ **403，已不可用**
      sns-img-bd/{fileId}    → 200，但很可能是 **HEIC**（UHDR 笔记 2.1 MB），浏览器显示白块
      sns-img-bd/{fileId}?imageView2/2/w/0/format/jpg → 200，1.4 MB，image/jpeg，尺寸不缩
    所以主候选是「CDN 转档 jpg」，原图/urlDefault 只做兜底。
    """
    urls = []
    fid = (img.get("fileId") or "").strip()
    if fid:
        # 2026-11-28 实测：UHDR 笔记（fileId 带 note_pre_post_uhdr/）的原图是 **HEIC**
        # （Content-Type image/heic，2.1 MB），Chrome 和微信 webview 都不解码，页面白块。
        # 让 CDN 现转 JPEG：同一张图 4,284×5,712、1.4 MB、image/jpeg，尺寸不缩。
        urls.append("https://sns-img-bd.xhscdn.com/%s?imageView2/2/w/0/format/jpg" % fid)
        urls.append("https://ci.xiaohongshu.com/%s?imageView2/2/w/0/format/jpg" % fid)
        # 原图兜底（HEIC 时靠 app.sniff_ext 判扩展名）
        urls.append("https://sns-img-bd.xhscdn.com/%s" % fid)
    for key in ("urlDefault", "url", "urlPre"):
        u = _clean_img(img.get(key))
        if u and u not in urls:
            urls.append(u)
    for info in (img.get("infoList") or []):
        u = _clean_img(info.get("url"))
        if u and u not in urls:
            urls.append(u)
    return urls


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


def _video_variants(note):
    """返回 (preview_url, orig_url, codec, variants)。

    `variants` 是给前端的档位列表，**只发布两个可下载项：原画 + H.265**；
    `recommended` 给 H.265（该笔记没有 H.265 时退回原画）。

    水印分布（2026-11-22 逐档实测，别搞反）：
      * **原画** `originVideoKey`：1080×1440 / 222.3 MB / **无平台水印**
      * **H.265 转码档**：720×960 / 20.0 MB / **无平台水印**（下载后播放确认；Chrome 解不了 HEVC，
        无法抓帧复核，此条依据为实际下载验证）
      * **H.264 转码档**：720×960 / 24.3 MB / **带平台水印**（第 30 秒那帧可见左上「小红书」+ 右上用户名），
        **不作为可下载项**，只留作「浏览器不支持 HEVC 时的预览兜底」

    判据教训：
      1. 第一次只抓第 4 秒一帧就写「转码档不带水印」——错。水印开头几秒淡入，**必须多抓几个时间点**。
      2. 第二次以为「所有转码档都带水印」——错。**同一视频的不同编码档水印策略可以不同，必须逐档验。**
    """
    video = note.get("video") or {}
    origin = ((video.get("consumer") or {}).get("originVideoKey") or "").strip()
    origin_url = ("https://sns-video-bd.xhscdn.com/" + origin) if origin else ""

    stream = ((video.get("media") or {}).get("stream") or {})

    def pick(codec):
        url, score, meta = "", -1, {}
        for item in (stream.get(codec) or []):
            u = item.get("masterUrl") or ((item.get("backupUrls") or [""])[0])
            if not u:
                continue
            s = (item.get("width") or 0) * (item.get("height") or 0)
            if s > score:
                url, score, meta = u, s, item
        return url, (meta or {})

    h264, m264 = pick("h264")
    h265, m265 = pick("h265")
    preview = h264 or origin_url or h265
    orig = origin_url or h265 or h264
    codec = "h264" if h264 else (("h265" if (origin_url or h265) else ""))

    # 原画分辨率只在 mediaV2.video 里（stream 的宽高是转码档的，别混用）；拿不到就留空，不猜
    ow = oh = 0
    try:
        mv2 = video.get("mediaV2")
        obj = json.loads(mv2) if isinstance(mv2, str) else (mv2 or {})
        vv = (obj or {}).get("video") or {}
        ow, oh = int(vv.get("width") or 0), int(vv.get("height") or 0)
    except (ValueError, TypeError, AttributeError):
        pass

    variants, seen = [], set()

    def add(vid, url, c, meta, w=0, h=0, rec=False, label="", size=0, parts=None):
        if not url or url in seen:
            return
        seen.add(url)
        variants.append({
            "id": vid, "url": url, "codec": c,
            "width": int(w or (meta or {}).get("width") or 0),
            "height": int(h or (meta or {}).get("height") or 0),
            "size": int(size or (meta or {}).get("size") or 0),
            "label": label, "recommended": rec,
            "parts": parts or [],          # 分列渲染用：类型 / 分辨率 / 体积 / 水印
        })

    if origin_url:
        # 原画体积不在页面 JSON 里，现量一次（失败就不写体积）
        osize = head_size(origin_url)
        parts = ["原画"]
        if ow and oh:
            parts.append("%d×%d" % (ow, oh))
        if osize:
            parts.append("%.1f MB" % (osize / 1048576.0))
        parts.append("无水印")
        # 没有 H.265 时原画就是唯一选择，标成默认
        add("orig", origin_url, "orig", {}, w=ow, h=oh, rec=not h265, size=osize,
            label=" · ".join(parts), parts=parts)

    # 只发布 H.265 作为转码档：体积只有原画的 1/11 且实测无水印；
    # H.264 带平台水印，**不发布**，仅由前端在浏览器不支持 HEVC 时充当预览兜底。
    add("hd-h265", h265, "h265", m265, rec=True)

    # label 统一成四段：类型 · 分辨率 · 体积 · 水印，并同时给出 parts。
    # 前端按 parts 分列渲染（定宽），这样两个档位的「·」和每一段都在同一条竖线上。
    # 不要再写「体积小」这类主观描述——后面的 MB 数已经说明了。
    for v in variants:
        if v["parts"]:
            continue
        res = ("%d×%d" % (v["width"], v["height"])) if (v["width"] and v["height"]) else ""
        mb = ("%.1f MB" % (v["size"] / 1048576.0)) if v["size"] else ""
        v["parts"] = [x for x in ("转码", res, mb, "无水印") if x]
        v["label"] = " · ".join(v["parts"])

    return preview, orig, codec, variants


def _best_video(note):
    """旧名保留：只要一个地址时给预览地址。"""
    return _video_variants(note)[0]


def _load_cookie():
    """读本机存的小红书登录 cookie（~/.workbuddy/secrets/xhs_cookie.json）。

    2026-11-28 实测：无 cookie 时 SSR 页面只返回空壳（errorNoteData + noteData:{}），
    带登录 cookie 后同一页面变成 114KB、含完整 noteDetailMap。
    cookie 过期后症状和没带一样（空壳），所以解析失败先怀疑它。
    """
    try:
        p = os.path.expanduser("~/.workbuddy/secrets/xhs_cookie.json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            return "; ".join("%s=%s" % (k, v) for k, v in d.items() if v)
    except Exception:
        pass
    return ""


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

    page = get(url, referer="https://www.xiaohongshu.com/", cookie=_load_cookie())
    html = page.text or ""

    # 缺 xsec_token 时平台不发正文，而是把请求甩到 /404/sec_xxx 风控页（数据是空壳）。
    # 这个症状最容易误判成「私密/已删除」，所以单独报（2026-11-28 实测）。
    if "/404/sec_" in (page.url or "") or "xhs_sec_server" in (page.url or ""):
        raise ValueError("这条链接缺 xsec_token，被平台当风控请求拦了；"
                         "请在 App 里用「分享 → 复制链接」重新复制")

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

    video_url, video_url_orig, video_codec, video_variants = "", "", "", []
    if (note.get("type") or "") == "video" or note.get("video"):
        video_url, video_url_orig, video_codec, video_variants = _video_variants(note)

    # 图文笔记里也可能混着 live photo（动图），先按普通图处理，不额外拉视频
    if not images and not video_url:
        raise ValueError("没取到图片或视频地址")

    return {
        "platform": "xhs",
        "kind": "video" if video_url else "images",
        "title": title,
        "author": author,
        "video_url": video_url,
        "video_url_orig": video_url_orig,
        "video_codec": video_codec,
        "variants": video_variants,
        "images": images,
        "cover": images[0] if images else "",
        "raw_id": note_id,
    }
