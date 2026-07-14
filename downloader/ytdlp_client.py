"""yt-dlp 下载封装：把公开推特/X 链接里的视频下载到本地。

结构对齐 aria2_client：对外给一个 download() 协程，返回 DownloadResult（字段与
aria2 的 DownloadResult 同名 downloaded_files / skipped / root_dir），让 handle_twitter
能像消费 download_magnet 的结果一样消费它。

下载通道（按序尝试）：
  1. fxtwitter 公共 API——直接向 api.fxtwitter.com/status/<id> 要 mp4 直链，
     再交给 yt-dlp 下载。实测 X 的新视频管线（amplify_video）匿名场景下
     yt-dlp 官方 extractor 基本必失败（报 "No video could be found"），
     fxtwitter 则稳定可用，故作为首选。这也是市面上「发链接即下」推特机器人
     普遍采用的解析通道（FixTweet/FxEmbed，MIT 开源，公开 API，限额 1000 次/分钟/IP）。
  2. yt-dlp 官方 twitter extractor（匿名）兜底——fxtwitter 宕机/限流/未收录时使用。

yt-dlp 是同步阻塞库，下载丢进线程池执行（run_in_executor），避免卡住 telethon 的事件循环；
进度用「下载线程写共享状态 + 主协程每 2s 轮询渲染」，与 aria2_client._wait_complete 同构。

不在模块顶层 import yt_dlp（延迟到 download 内），这样缺少该依赖时应用仍能启动，
只是粘贴推特链接时给出「未安装」提示，风格对齐 aria2 不可用时的优雅降级。
"""
import asyncio
import json
import os
import re
import urllib.request

from common.progress import make_bar

# fxtwitter 公共解析 API（FixTweet/FxEmbed 项目）
FXTWITTER_API = "https://api.fxtwitter.com/status/{tweet_id}"


class YtdlpError(Exception):
    pass


class DownloadResult:
    def __init__(self, downloaded_files, skipped, root_dir=None):
        self.downloaded_files = downloaded_files   # 已下载的视频文件绝对路径列表
        self.skipped = skipped                     # [(name, size_bytes, reason)]
        self.root_dir = root_dir                   # 与 aria2 对齐；yt-dlp 无顶层目录，恒为 None


class Ytdlp:
    """封装 yt-dlp，把公开推特/X 链接的视频下载到 download_dir。无守护进程，无需 start/stop。"""

    def __init__(self, download_dir):
        self.download_dir = os.path.abspath(download_dir)

    async def download(self, url, status_msg, max_file_bytes):
        try:
            import yt_dlp  # noqa: F401  仅探测依赖是否可用
        except ImportError:
            raise YtdlpError("yt-dlp 未安装，无法下载推特视频")

        os.makedirs(self.download_dir, exist_ok=True)
        await status_msg.edit("🔗 **正在解析推特链接…**")

        # 通道 1：fxtwitter 直链（实测官方匿名 extractor 对 amplify_video 基本必失败，直连更快更稳）
        direct_urls = await self._fxtwitter_video_urls(url)
        if direct_urls:
            paths = await self._ytdlp_download(direct_urls, status_msg)
        else:
            # 通道 2：yt-dlp 官方 extractor 兜底（fxtwitter 宕机/限流/未收录时）
            await status_msg.edit("🔁 **fxtwitter 未返回视频，改用 yt-dlp 官方解析…**")
            paths = await self._ytdlp_download([url], status_msg)

        # 大小上限：超限的删除并计入 skipped（复用 magnet 的「跳过并报告」语义）
        downloaded, skipped = [], []
        for p in paths:
            if not p or not os.path.isfile(p):
                continue
            size = os.path.getsize(p)
            if size > max_file_bytes:
                self._unlink(p)
                skipped.append((os.path.basename(p), size, "超过大小上限"))
            else:
                downloaded.append(p)
        return DownloadResult(downloaded, skipped)

    # ---------- 通道 1：yt-dlp（extractor 或直链均可） ----------
    async def _ytdlp_download(self, urls, status_msg):
        """用 yt-dlp 逐个下载 urls，轮询渲染进度，返回最终文件路径列表。"""
        import yt_dlp

        # 下载线程与主协程共享的进度状态（GIL 下对这些键的简单赋值是安全的）
        progress = {"status": None, "downloaded": 0, "total": 0, "speed": 0}

        def hook(d):
            st = d.get("status")
            progress["status"] = st
            if st == "downloading":
                progress["downloaded"] = d.get("downloaded_bytes") or 0
                progress["total"] = (
                    d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                )
                progress["speed"] = d.get("speed") or 0

        ydl_opts = {
            # 优先单文件 mp4（自带音轨，无需 ffmpeg 合并）；没有单文件时才退到
            # 「最佳视频+音频」合并（需要 ffmpeg，仅极少数 HLS-only 场景命中）。
            # 主通道 fxtwitter 直链本身就是单文件 mp4，恒走第一分支。
            "format": "b[ext=mp4]/bv*+ba/b",
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(self.download_dir, "%(id)s.%(ext)s"),
            "noplaylist": False,       # 一条推文可能含多个视频 → 多 entry → 多文件
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,        # 关掉 yt-dlp 自己的控制台进度条
            "progress_hooks": [hook],
            "logger": _SilentLogger(),
        }

        def _blocking_download():
            paths = []
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                for u in urls:
                    info = ydl.extract_info(u, download=True)
                    paths.extend(_collect_filepaths(info))
            return paths

        loop = asyncio.get_event_loop()
        fut = loop.run_in_executor(None, _blocking_download)

        # 主协程轮询渲染进度（每 2s），直到下载线程完成
        while not fut.done():
            await self._render(status_msg, progress)
            await asyncio.sleep(2)

        try:
            return await fut
        except Exception as e:
            raise YtdlpError(_clean_error(e))

    # ---------- 通道 2：fxtwitter 直链解析 ----------
    async def _fxtwitter_video_urls(self, url):
        """向 fxtwitter 公共 API 要该推文的视频 mp4 直链列表；拿不到返回 []。"""
        m = re.search(r"/status/(\d+)", url)
        if not m:
            return []
        api = FXTWITTER_API.format(tweet_id=m.group(1))

        def _fetch():
            req = urllib.request.Request(
                api, headers={"User-Agent": "telegram-downloader/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())

        loop = asyncio.get_event_loop()
        try:
            body = await loop.run_in_executor(None, _fetch)
        except Exception as e:
            print(f"⚠️ fxtwitter 回退查询失败：{str(e).strip() or type(e).__name__}")
            return []

        if body.get("code") != 200:
            return []
        media = (body.get("tweet") or {}).get("media") or {}
        # videos 含普通视频与 gif（推特 gif 本质也是 mp4），都收
        return [v["url"] for v in media.get("videos") or [] if v.get("url")]

    async def _render(self, status_msg, progress):
        total = progress["total"]
        done = progress["downloaded"]
        speed = progress["speed"]
        if progress["status"] is None:
            text = "🔗 **正在解析推特链接…**"
        elif total <= 0:
            text = "📥 **正在下载推特视频…** 正在连接"
        else:
            text = (
                "📥 **正在下载推特视频…**\n"
                f"[{make_bar(done, total)}] {done / total * 100:.1f}%\n"
                f"📦 {done / 1048576:.1f} / {total / 1048576:.1f} MB · "
                f"⬇ {speed / 1048576:.2f} MB/s"
            )
        try:
            await status_msg.edit(text)
        except Exception:
            # 限流/文字未变更等编辑异常忽略，不影响下载
            pass

    async def cleanup(self, result):
        if result is None:
            return
        for p in result.downloaded_files:
            self._unlink(p)

    @staticmethod
    def _unlink(path):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass


class _SilentLogger:
    """吞掉 yt-dlp 的 debug/info/warning，只把 error 打到 docker 日志。"""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        print(f"⚠️ yt-dlp: {msg}")


def _collect_filepaths(info):
    """从 extract_info 返回的 info 里收集最终文件路径（合并/后处理后的路径）。

    单条推文：info['requested_downloads'][*]['filepath']；
    含多视频（yt-dlp 作为 playlist 返回）：遍历 info['entries']。
    """
    paths = []

    def _from(entry):
        if not entry:
            return
        for d in entry.get("requested_downloads") or []:
            fp = d.get("filepath")
            if fp:
                paths.append(fp)

    if info and info.get("entries") is not None:
        for e in list(info["entries"]):
            _from(e)
    else:
        _from(info)
    return paths


def _clean_error(e):
    """把 yt-dlp 的报错转成简洁中文提示。"""
    msg = (str(e).strip() or type(e).__name__).replace("ERROR: ", "")
    low = msg.lower()
    if "ffmpeg" in low:
        return "该视频只有分离的音视频流，需要 ffmpeg 合并（当前环境未安装）"
    if "no video" in low or "no media" in low or "unsupported url" in low:
        return "未在该推文中找到可下载的视频"
    if any(k in low for k in ("nsfw", "age", "log in", "authenticat", "protected", "private")):
        return "该推文需要登录才能查看（本项目只支持公开推文）"
    return msg
