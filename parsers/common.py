# -*- coding: utf-8 -*-
"""通用工具：链接提取、带重试的请求、页面内 JSON 抠取、安全域名校验。"""
import json
import re
import time

import requests

# 移动端 UA：两个平台的分享页都是给手机看的，桌面 UA 拿不到 SSR 数据
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff，。、；：！？（）()【】\[\]<>\"']+")

# 允许代理下载的 CDN 域名后缀（防止服务被当成开放代理 / SSRF）
ALLOWED_MEDIA_HOSTS = (
    "douyinpic.com", "douyinvod.com", "iesdouyin.com", "douyin.com",
    "byteimg.com", "bytedance.com", "zjcdn.com", "ibyteimg.com",
    "xhscdn.com", "xiaohongshu.com", "sns-img", "sns-video",
)


def extract_url(text):
    """从整段分享文案里抠出第一个链接（微信/小红书复制的都是一大段话）。"""
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip("，。、；：") if m else None


def extract_urls(text):
    """抠出全部链接，按出现顺序去重。

    一次粘多条时常是「文案1 链接1 文案2 链接2」混排，所以要全扫而不是只取第一个。
    """
    if not text:
        return []
    seen, out = set(), []
    for m in URL_RE.finditer(text):
        u = m.group(0).rstrip("，。、；：")
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def get(url, referer=None, retries=2, timeout=15, allow_redirects=True, stream=False):
    """带重试的 GET。两个平台的 SSR 页偶尔 5xx，重试两次基本能过。"""
    headers = {
        "User-Agent": MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout,
                             allow_redirects=allow_redirects, stream=stream)
            if r.status_code < 500:
                return r
            last = RuntimeError("HTTP %s" % r.status_code)
        except requests.RequestException as e:
            last = e
        if i < retries:
            time.sleep(0.6 * (i + 1))
    raise RuntimeError("请求失败：%s（%s）" % (url[:80], last))


def extract_json_after(html, marker):
    """从 HTML 里抠出 marker 后面那个 JSON 对象。

    比正则可靠：用 json.JSONDecoder.raw_decode 做括号配平，
    能正确跳过字符串里的花括号和转义。找不到就返回 None。

    注意：小红书的 __INITIAL_STATE__ 里混着 JS 的 undefined（不是合法 JSON），
    必须先替换成 null 才能解析；正则要求 undefined 前后是 JSON 定界符，
    所以不会误伤字符串里的 "undefined" 文本。
    """
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    text = re.sub(r"(?<=[:,\[])\s*undefined(?=[,}\]])", "null", html[j:])
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)
        return obj
    except ValueError:
        # SSR 的 JSON 偶尔被截断，退一步：逐步截短再试
        for cut in range(len(text), max(len(text) - 40000, 0), -500):
            try:
                return json.JSONDecoder().raw_decode(text[:cut])[0]
            except ValueError:
                continue
    return None


def is_allowed_media_url(url):
    """只允许代理白名单域名的媒体，避免服务变成任意 URL 抓取器。"""
    m = re.match(r"https?://([^/]+)/", url or "")
    if not m:
        return False
    host = m.group(1).lower().split(":")[0]
    return any(host == h or host.endswith("." + h) or h in host
               for h in ALLOWED_MEDIA_HOSTS)


def safe_filename(name, fallback="file", limit=60):
    """去掉文件名里的非法字符，避免 Windows 上保存失败。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:limit] or fallback)
