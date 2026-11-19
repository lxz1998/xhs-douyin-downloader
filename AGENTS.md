# AGENTS.md · 给接手这个项目的 AI

> 这份文件是给**模型**看的，不是给人看的。改代码前先读完它。
> 人看的说明在 `README.md`。

## 这是什么

一个跑在本机的去水印小工具：起一个 Flask，浏览器里粘链接，拿原图和无水印视频。
不依赖任何服务器、域名、备案——所有解析都在本机完成。

## 三十秒上手

```bash
python -m pip install -r requirements.txt
python app.py                     # 默认 8080；Windows 双击 启动.bat 会用 8090
# 浏览器打开 http://127.0.0.1:8080
```

## 唯一判据：selftest.py

**改完任何东西，必须跑：**

```bash
python selftest.py "<一条真实的小红书链接>"
```

退出码 0 才算改对。它验七件事：首页能开、非法输入被拒、多链接提取与去重、历史接口读写删、
真链接出图、图片能下、视频支持 Range。

`python selftest.py`（不带链接）只跑前四项，不联网，适合频繁改 UI 时快速回归。

**不要用「我觉得应该没问题」代替这一步。** 这个项目最容易骗人的地方是：服务起来了、
页面上也显示出标题了，但图片其实是 0 张、下载 403、或者视频一直转圈。

## 文件职责

| 文件 | 干什么 | 改动风险 |
|---|---|---|
| `launcher.pyw` | 挑空闲端口 → 起 Flask → 等端口真通了再开浏览器 | 低 |
| `app.py` | 路由、代理下载（含 Range 透传）、打包 zip、历史记录、平台分发 | 中 |
| `parsers/xhs.py` | 小红书：解析 `__INITIAL_STATE__` 拿原图 / 视频直链 | 高（页面结构一变就挂） |
| `parsers/douyin.py` | 抖音：走 CDP 拦 XHR 拿 `aweme_detail` | 高 |
| `parsers/browser.py` | 起 Chrome、连 CDP、收响应体 | 高 |
| `parsers/common.py` | 抠链接（单个 / 全部）、带重试的 GET、JSON 抠取、域名白名单 | 中 |
| `static/index.html` | 单页前端（无框架，原生 JS） | 低 |

## 四条不能动的约束

1. **`media_headers()` 里两套 Referer 别合并。** 抖音 CDN 只认 `douyin`，小红书只认 `xiaohongshu`，
   统一设一个，另一半直接 502。
2. **`ALLOWED_MEDIA_HOSTS` 别放宽。** 这个白名单把服务从「下载器」变成「任意 URL 抓取器」，是个 SSRF 口子。
   要加平台就精确加域名，别用通配。
3. **扩展名看文件头（`sniff_ext`），不看 Content-Type。** 小红书 CDN 经常返回
   `application/octet-stream`，只信头信息会把 jpg 全存成 `.bin`。
4. **`/api/dl` 必须透传 `Range`。** 小红书的视频是 `moov` 在文件末尾的 QuickTime 容器，
   浏览器要 Range 跳到末尾读索引才能出画面。把 Range 丢掉，`<video>` 会永远 `readyState=0`
   ——不报错、不转圈、就是黑的（2026-11-19 踩过）。同理 `Content-Length` 和 `Content-Range` 也要带回去。

## 已知边界（别当成 bug 去修）

- **抖音在机房 IP 上一定失败。** 云端打开抖音页能出 HTML，但 `aweme/detail` 不返数据。这是风控，不是代码问题。
- **抖音需要本机 Chrome。** `browser.py` 会自己找并起 Chrome；没装就降级（`DOUYIN_READY=False`），小红书不受影响。
- **匿名拿不到作者互动数。** 小红书 SSR 对未登录用户不给这些字段，空着是正常的。
- **视频直链带签名，会过期。** 拿到就得下，不能存着以后用。
- **图片上的字去不掉。** 这是「拿原始地址」，不是图像修复。抹掉已烧进画面的字需要 LaMa 这类模型。
- **小红书链接必须带 `xsec_token`。** 截掉后平台返回登录墙，表现为「笔记可能是私密/已删除」，不是笔记没了。

## 扩展点（按性价比排）

1. **加平台**：`parsers/` 下新建模块，暴露 `parse(text) -> dict`（字段照 `xhs.parse` 的返回），
   在 `app.py` 的 `detect_platform` 加分支，再去 `common.ALLOWED_MEDIA_HOSTS` 补它的 CDN 域名。三处缺一不可。
2. **加清晰度选项**：小红书视频有多档码率，`xhs.py` 现在取最高档。要做选择器，
   在返回里多给一个 `variants` 列表，前端加下拉。
3. **换图标**：`python -m PyInstaller --onefile --icon=你的.ico ...`
4. **做托盘 / 后台常驻**：不要。控制台窗口是用户唯一的「怎么退出」的线索。

## 打包成 exe（发给不装 Python 的人）

```bash
python -m PyInstaller --onefile --name "去水印" --add-data "static;static" --console --clean -y app.py
```

**两个坑，都踩过：**

- 必须用 `python -m PyInstaller`，不要用裸的 `pyinstaller`——两者可能指向不同环境，
  打出来的 exe 一启动就 `No module named 'flask'`。
- `--add-data` 在 Windows 上用分号 `;`，Linux/macOS 用冒号 `:`。写错了 `static/` 不会进包，exe 打开是 404。

## 排查顺序（出问题时按这个来，别乱试）

1. `python selftest.py` —— 先分清是「本地逻辑坏了」还是「平台变了」
2. 解析 422 且提示「没找到笔记数据」→ 小红书改了结构，去 `parsers/xhs.py` 的 `_find_note()` 加新路径
3. 图片 403/502 → 先看 `media_headers` 的 Referer，再看 CDN 域名有没有变（可能不在 `ALLOWED_MEDIA_HOSTS`）
4. 视频黑屏 / 一直转圈 → 先 `curl -I "/api/dl?u=..."` 看有没有 `Accept-Ranges` 和 `Content-Length`，
   再 `curl -H "Range: bytes=0-1023"` 看是不是 `206`
5. exe 能开但页面 404 → `--add-data` 没写对，`static/` 没进包

## 前后端的坑（都是「代码没错、语法也过，就是不工作」）

1. **弹层的 DOM 必须写在 `<script>` 之前。** 把灯箱的 `<div>` 插到 `</script>` 后面，
   脚本执行时 `getElementById('lb')` 返回 null，整个查看器一个事件都没绑上。
   表现是「鼠标有放大镜光标，但点了没反应」——看起来像 CSS 问题，其实是顺序问题。
2. **flex 居中 + transform 平移 = 双重偏移。** 居中只能有一个来源。
3. **图片必须走 `/api/dl` 代理，不能直连 CDN。** 小红书 CDN 校验 Referer，直连 403。
4. **`.bat` 必须 CRLF 换行 + 纯 ASCII。** LF 会让 cmd 解析错乱；UTF-8 中文在 GBK 的 cmd 里会被拆碎。
5. **双击启动的场景下，`python` 必须在系统 PATH 里。** 命令能跑不代表双击能跑——
   终端里可能有额外注入的 PATH。判据：用干净环境跑一次（Windows 上 `set PATH=C:\Windows\system32` 再试）。
6. **浏览器要等端口真的通了再开。** 先 `start http://...` 再起 Flask，用户会看到「无法访问此网站」。
   正确做法是轮询 `socket.create_connection` 直到成功再打开。
7. **`/api/quit` 这类「自杀接口」的延迟要 ≥0.8 秒**，否则响应还没发完就被 `os._exit(0)` 掐了。
   而且必须限制 `remote_addr` 是 127.0.0.1，否则谁都能关你的服务。
8. **改前端后要强刷。** 本地 Flask 不做缓存失效，浏览器会留着旧 JS，表现是「改了没效果」。

**一句话**：前后端都能跑、语法也都对，但**位置、顺序、环境**这三样错了，表现都是「没反应」。
遇到「没反应」先查这三样，别急着改逻辑。

## 数据与隐私

- 历史记录写在 `~/.watermark/history.json`，只存标题 / 平台 / 张数 / 时间，**不存直链**。
- 项目目录里不应该出现任何 cookie、token、账号密码。`is_allowed_media_url` 只是为了防 SSRF，不是鉴权。
- 本工具不收集任何用户数据，不向任何第三方服务器发送内容（解析是直接请求平台自己的接口）。
