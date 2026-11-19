# -*- coding: utf-8 -*-
"""本机版自检。改完代码、或者「好像不能用了」的时候跑一次。

用法：
    python selftest.py                          # 不联网，只测页面与路由
    python selftest.py <小红书链接或分享文案>     # 加上真实解析 + 图片下载

退出码 0 = 全过；非 0 = 有项没过，看 [FAIL] 那行。
"""
import sys
import traceback

FAILED = []


def check(name, fn):
    try:
        detail = fn()
        print("[PASS] %s%s" % (name, ("  " + detail) if detail else ""))
    except Exception as e:
        print("[FAIL] %s  ->  %s: %s" % (name, type(e).__name__, e))
        traceback.print_exc()
        FAILED.append(name)


def main():
    from app import app

    client = app.test_client()

    def page():
        r = client.get("/")
        assert r.status_code == 200, "首页 HTTP %s" % r.status_code
        body = r.data.decode("utf-8", "ignore")
        assert "<title>" in body, "首页没有 title"
        assert "btnGo" in body, "首页没有解析按钮"
        return "200 / %d 字节" % len(r.data)

    def bad_input():
        r = client.post("/api/parse", json={"text": ""})
        assert r.status_code == 400, "空输入应 400，实际 %s" % r.status_code
        r2 = client.post("/api/parse", json={"text": "https://example.com/x"})
        assert r2.status_code == 400, "未知平台应 400，实际 %s" % r2.status_code
        return "空输入与未知平台都被拒"

    def url_extract():
        """多链接提取是批量解析的前提，纯函数，不联网。"""
        from parsers.common import extract_urls
        text = ("复制打开抖音 https://v.douyin.com/aaa/ 看看\n"
                "小红书笔记 http://xhslink.com/o/bbb 快来看\n"
                "https://v.douyin.com/aaa/")          # 重复的只留一次
        got = extract_urls(text)
        assert len(got) == 2, "应提取 2 条（去重后），实际 %d：%s" % (len(got), got)
        assert got[0].endswith("/aaa/") and got[1].endswith("/bbb"), "顺序或内容不对：%s" % got
        assert extract_urls("没有链接的一段话") == [], "无链接时应返回空"
        return "2 条，重复已去重"

    def history_api():
        """写 → 读 → 删，跑完必须还原成原来的条数。"""
        from app import record_history
        r0 = client.get("/api/history").get_json() or {}
        n0 = len(r0.get("items") or [])
        e = record_history({"platform": "xhs", "kind": "images", "title": "自检写入",
                            "images": [1, 2], "author": "t"}, "https://example.com/x")
        n1 = len((client.get("/api/history").get_json() or {}).get("items") or [])
        assert n1 == n0 + 1, "写入后条数应为 %d，实际 %d" % (n0 + 1, n1)
        client.post("/api/history/del", json={"id": e["id"]})
        n2 = len((client.get("/api/history").get_json() or {}).get("items") or [])
        assert n2 == n0, "删除后条数应回到 %d，实际 %d" % (n0, n2)
        return "写入 / 读取 / 删除都正常，已还原"

    check("首页可打开", page)
    check("非法输入被拒", bad_input)
    check("多链接提取", url_extract)
    check("历史接口", history_api)
    # 注意：不要在这里测 /api/quit —— test_client 的 remote_addr 是 127.0.0.1，
    # 一调就把自检进程自己关掉了。

    url = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            url = arg
            break

    if url:
        def real_parse():
            r = client.post("/api/parse", json={"text": url})
            data = r.get_json() or {}
            assert r.status_code == 200, "解析失败 HTTP %s：%s" % (r.status_code, data.get("error"))
            assert data.get("ok"), "ok 不是 true"
            n = len(data.get("images") or [])
            assert n or data.get("video_url"), "既没有图片也没有视频"
            return "%s | %d 张图" % ((data.get("title") or "")[:24], n)

        def real_download():
            r = client.post("/api/parse", json={"text": url})
            data = r.get_json() or {}
            imgs = data.get("images") or []
            if not imgs:
                return "视频作品，跳过图片下载"
            from urllib.parse import quote
            d = client.get("/api/dl?u=%s&name=t.jpg" % quote(imgs[0], safe=""))
            assert d.status_code == 200, "下载 HTTP %s" % d.status_code
            assert len(d.data) > 2000, "只拿到 %d 字节，不像真图" % len(d.data)
            return "%d 字节 / %s" % (len(d.data), d.headers.get("Content-Type", ""))

        def video_range():
            """视频必须支持 Range：moov 在文件末尾，不 206 就播不了（2026-11-19 修的就是这个）。"""
            from urllib.parse import quote
            data = (client.post("/api/parse", json={"text": url}).get_json() or {})
            vu = data.get("video_url")
            if not vu:
                return "图片作品，跳过"
            d = client.get("/api/dl?u=%s" % quote(vu, safe=""),
                           headers={"Range": "bytes=0-1023"})
            assert d.status_code == 206, "Range 应返回 206，实际 %s" % d.status_code
            assert d.headers.get("Accept-Ranges") == "bytes", "缺 Accept-Ranges 头"
            cr = d.headers.get("Content-Range") or ""
            assert cr.startswith("bytes 0-1023/"), "Content-Range 不对：%s" % cr
            assert len(d.data) == 1024, "应拿 1024 字节，实际 %d" % len(d.data)
            return "206 / %s" % cr

        check("真实链接解析", real_parse)
        check("原图能否下载", real_download)
        check("视频 Range 支持", video_range)
    else:
        print("[SKIP] 真实链接解析（没给链接；要测就 python selftest.py <链接>）")

    print("-" * 48)
    if FAILED:
        print("结果：FAIL（%d 项没过：%s）" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("结果：PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
