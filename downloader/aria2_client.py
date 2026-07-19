import asyncio
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.request

from common.filters import plan_files
from common.progress import make_bar

# 给「裸磁力」(只有 infohash、不带 tracker) 兜底用的公共 tracker，
# 注入后即便磁力本身没带 tracker 也能找到 peer。
DEFAULT_TRACKERS = ",".join([
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
])

# DHT 引导节点：容器首次启动没有 DHT 路由表，必须给入口点才能 bootstrap。
DHT_ENTRY_POINT = "router.bittorrent.com:6881"


class Aria2Error(Exception):
    pass


class DownloadResult:
    def __init__(self, downloaded_files, skipped, root_dir, gid=None):
        self.downloaded_files = downloaded_files   # 已下载的视频文件绝对路径
        self.skipped = skipped                     # [(name, size_bytes, reason)]
        self.root_dir = root_dir                   # 种子在本地的顶层目录（单文件种子为 None）
        self.gid = gid                             # aria2 下载 gid，用于清理


class Aria2Client:
    """管理一个本地 aria2c 守护进程，并通过 JSON-RPC 下发磁力下载。

    阻塞的 urllib RPC 调用统一丢进 executor，避免卡住 telethon 的事件循环。
    """

    def __init__(self, download_dir, rpc_port=6800, stall_timeout=300):
        self.download_dir = os.path.abspath(download_dir)
        self.rpc_port = rpc_port
        self.stall_timeout = stall_timeout
        self.secret = secrets.token_hex(16)
        self.endpoint = f"http://127.0.0.1:{rpc_port}/jsonrpc"
        self.proc = None

    # ---------- 守护进程生命周期 ----------
    def start(self):
        os.makedirs(self.download_dir, exist_ok=True)
        args = [
            "aria2c",
            "--enable-rpc",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={self.rpc_port}",
            f"--rpc-secret={self.secret}",
            f"--dir={self.download_dir}",
            "--seed-time=0",                            # 下完即停，不做种、不耗上行
            f"--bt-stop-timeout={self.stall_timeout}",  # 长时间 0 速度即停止（无人做种）
            # 磁力元数据下完后，由它派生的文件下载先暂停，等我们选好视频再继续
            "--pause-metadata=true",
            "--bt-save-metadata=false",
            # 找种增强：开 DHT(带引导节点) + LPD + PEX，并注入公共 tracker，
            # 让没带 tracker 的裸磁力也能找到 peer
            "--enable-dht=true",
            f"--dht-entry-point={DHT_ENTRY_POINT}",
            "--bt-enable-lpd=true",
            "--enable-peer-exchange=true",
            f"--bt-tracker={DEFAULT_TRACKERS}",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--daemon=false",
        ]
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # 轮询等待 RPC 端口就绪（最多 ~10 秒）
        for _ in range(50):
            try:
                self._rpc_sync("aria2.getVersion", [])
                return
            except Exception:
                time.sleep(0.2)
        raise Aria2Error("aria2c RPC 启动超时")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
            self.proc = None

    # ---------- JSON-RPC ----------
    def _rpc_sync(self, method, params):
        payload = {
            "jsonrpc": "2.0",
            "id": "tg",
            "method": method,
            "params": [f"token:{self.secret}", *params],
        }
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        if "error" in body:
            raise Aria2Error(body["error"].get("message", str(body["error"])))
        return body["result"]

    async def _rpc(self, method, *params):
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._rpc_sync, method, list(params))
        except Aria2Error:
            raise
        except Exception as e:
            # 把空消息的网络异常(如 TimeoutError)包装成有内容的错误，避免显示空白
            detail = str(e).strip() or type(e).__name__
            raise Aria2Error(f"RPC {method} 失败：{detail}")

    # ---------- 下载主流程 ----------
    async def download_magnet(self, magnet, status_msg, max_file_bytes):
        await status_msg.edit("🧲 **正在从 DHT 获取种子元数据…**")
        meta_gid = await self._rpc("aria2.addUri", [magnet])

        # 磁力先下元数据；完成后通过 followedBy 派生出真正的文件下载（已暂停）
        file_gid = await self._wait_metadata(meta_gid)
        files = await self._rpc("aria2.getFiles", file_gid)
        selected_indices, selected_paths, skipped = plan_files(files, max_file_bytes)

        if not selected_indices:
            await self._safe_remove(file_gid)
            return DownloadResult([], skipped, None)

        # 只下载选中的视频文件，再取消暂停
        await self._rpc(
            "aria2.changeOption", file_gid,
            {"select-file": ",".join(selected_indices)},
        )
        await self._rpc("aria2.unpause", file_gid)
        await self._wait_complete(file_gid, status_msg)

        root_dir = self._torrent_root(selected_paths)
        return DownloadResult(selected_paths, skipped, root_dir, file_gid)

    async def _wait_metadata(self, gid):
        start = time.time()
        while True:
            st = await self._rpc("aria2.tellStatus", gid)
            if st.get("status") == "error":
                raise Aria2Error(st.get("errorMessage", "种子元数据下载失败"))
            followed = st.get("followedBy")
            if followed:
                return followed[0]
            if time.time() - start > self.stall_timeout:
                raise Aria2Error("获取种子元数据超时（可能无人做种）")
            await asyncio.sleep(1)

    async def _wait_complete(self, gid, status_msg):
        last_render = 0
        last_done = -1
        last_change = time.time()
        while True:
            st = await self._rpc("aria2.tellStatus", gid)
            status = st.get("status")
            if status == "complete":
                return
            if status in ("error", "removed"):
                raise Aria2Error(st.get("errorMessage", "下载中止"))

            total = int(st.get("totalLength", 0))
            done = int(st.get("completedLength", 0))
            speed = int(st.get("downloadSpeed", 0))
            conns = st.get("connections", "0")

            now = time.time()
            if now - last_render >= 2:
                last_render = now
                await self._render(status_msg, total, done, speed, conns)

            # 自检：长时间无进度即放弃（兜底 aria2 的 bt-stop-timeout）
            if done != last_done:
                last_done, last_change = done, now
            elif now - last_change > self.stall_timeout:
                raise Aria2Error("长时间无下载进度（可能无人做种），已放弃")

            await asyncio.sleep(1)

    async def _render(self, status_msg, total, done, speed, conns):
        if total <= 0:
            text = "📥 **正在下载…** 正在连接 peers"
        else:
            text = (
                "📥 **正在下载种子内的视频…**\n"
                f"[{make_bar(done, total)}] {done / total * 100:.1f}%\n"
                f"📦 {done / 1048576:.1f} / {total / 1048576:.1f} MB · "
                f"⬇ {speed / 1048576:.2f} MB/s · 🔗 {conns}"
            )
        try:
            await status_msg.edit(text)
        except Exception:
            # 限流/文字未变更等编辑异常忽略，不影响下载
            pass

    # ---------- 清理 ----------
    async def cleanup(self, result, delete_files=True):
        """清理下载任务：移除 aria2 任务记录；delete_files=False 时保留本地文件。"""
        if result is None:
            return
        if delete_files:
            try:
                if result.root_dir and os.path.isdir(result.root_dir):
                    shutil.rmtree(result.root_dir, ignore_errors=True)
                else:
                    for p in result.downloaded_files:
                        self._unlink(p)
                        self._unlink(p + ".aria2")
            except Exception:
                pass
        if result.gid:
            await self._safe_remove(result.gid)

    async def _safe_remove(self, gid):
        # active 的用 remove，已完成/已停止的用 removeDownloadResult，两者都试一遍
        for method in ("aria2.remove", "aria2.removeDownloadResult"):
            try:
                await self._rpc(method, gid)
            except Exception:
                pass

    @staticmethod
    def _unlink(path):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass

    def _torrent_root(self, paths):
        """种子若有顶层目录则返回它（用于整体删除）；单文件种子返回 None。"""
        roots = set()
        for p in paths:
            rel = os.path.relpath(os.path.abspath(p), self.download_dir)
            roots.add(rel.split(os.sep)[0])
        if len(roots) == 1:
            candidate = os.path.join(self.download_dir, roots.pop())
            if os.path.isdir(candidate):
                return candidate
        return None
