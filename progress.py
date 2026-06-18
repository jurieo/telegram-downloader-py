import time

from telethon.errors import MessageNotModifiedError


def make_bar(current_bytes, total_bytes, length=15):
    """绘制字符进度条 [██████░░░]，对越界比例做钳制。"""
    if total_bytes <= 0:
        return "░" * length
    ratio = min(max(current_bytes / total_bytes, 0.0), 1.0)
    filled = int(length * ratio)
    return "█" * filled + "░" * (length - filled)


class ProgressBar:
    """单文件下载/上传进度条：把进度动态编辑到同一条状态消息上。"""

    def __init__(self, message_obj, action_title):
        self.message_obj = message_obj
        self.action_title = action_title
        self.last_update_time = 0

    async def callback(self, current_bytes, total_bytes):
        # 如果获取不到总大小，直接跳过计算
        if total_bytes == 0:
            return

        now = time.time()
        # 核心限流：每 2 秒最多更新一次界面（防止被 Telegram 官方限制）
        # 进度到达 100% 时强制更新一次
        if now - self.last_update_time < 2 and current_bytes != total_bytes:
            return

        self.last_update_time = now

        percent = (current_bytes / total_bytes) * 100
        current_mb = current_bytes / (1024 * 1024)
        total_mb = total_bytes / (1024 * 1024)
        bar = make_bar(current_bytes, total_bytes)

        text = (
            f"{self.action_title}\n"
            f"[{bar}] {percent:.1f}%\n"
            f"📦 进度: {current_mb:.2f} MB / {total_mb:.2f} MB"
        )

        try:
            await self.message_obj.edit(text)
        except MessageNotModifiedError:
            # 文字没变化时 Telegram 会报错，直接忽略
            pass
        except Exception as e:
            print(f"进度条更新异常: {e}")


class AggregateProgress:
    """并发上传聚合进度：多个文件同时上传，汇总显示在一条状态消息上。

    并发场景无法在一条消息上画多个进度条，改为统计“已完成文件数 / 已传字节数”。
    """

    def __init__(self, message_obj, title, total_files, total_bytes):
        self.message_obj = message_obj
        self.title = title
        self.total_files = total_files
        self.total_bytes = total_bytes
        self._current = {}      # key(文件路径) -> 该文件已上传字节
        self._done = 0          # 已完成文件数
        self._last_update = 0
        self._last_text = None

    def callback_for(self, key):
        """返回某个文件的 telethon 上传进度回调（async）。"""
        async def _cb(current_bytes, total_bytes):
            self._current[key] = current_bytes
            await self.render()
        return _cb

    def mark_done(self, key):
        self._done += 1

    async def render(self, force=False):
        now = time.time()
        if not force and now - self._last_update < 2:
            return
        self._last_update = now

        uploaded = sum(self._current.values())
        total = self.total_bytes if self.total_bytes > 0 else 1
        bar = make_bar(uploaded, total)
        text = (
            f"{self.title}\n"
            f"[{bar}] {min(uploaded / total * 100, 100):.1f}%\n"
            f"✅ {self._done}/{self.total_files} 个文件 · "
            f"{uploaded / (1024 * 1024):.1f} / {self.total_bytes / (1024 * 1024):.1f} MB"
        )
        # 文字无变化就不编辑，避免 MessageNotModifiedError
        if text == self._last_text:
            return
        self._last_text = text
        try:
            await self.message_obj.edit(text)
        except MessageNotModifiedError:
            pass
        except Exception as e:
            print(f"聚合进度更新异常: {e}")
