import asyncio
import os

from telethon.errors import FloodWaitError

from common.progress import AggregateProgress


async def upload_videos(client, target_chat, files, status_msg, concurrency,
                        registry=None, flood_retries=3, caption=None):
    """并发上传视频文件到目标频道。

    - 用信号量限制最大并发（并发越高越容易触发 Telegram FloodWait）。
    - 视频带 supports_streaming=True，让 Telegram 当可在线播放的视频处理；
      装了 hachoir 后 telethon 会自动补全宽高/时长，避免“方块视频”。
    - 单个文件触发 FloodWaitError 时按要求退避重试。
    - registry 非空时，把每条成功上传的消息记入，供上层扇出到其余目标并防回环。
    - caption：None = 用文件名当说明（磁力流，种子文件名有意义）；
      传字符串（可为空串）则统一用它（推特流的文件名是随机 id，传 "" 不带说明）。
    返回 (成功数, failed, sent_messages)，failed 为 [(文件名, 原因)]。
    """
    total_bytes = 0
    for p in files:
        try:
            total_bytes += os.path.getsize(p)
        except OSError:
            pass

    agg = AggregateProgress(status_msg, "📤 **正在上传视频到目标频道…**", len(files), total_bytes)
    await agg.render(force=True)

    sem = asyncio.Semaphore(max(1, concurrency))
    failed = []
    sent_messages = []

    async def _upload_one(path):
        name = os.path.basename(path)
        async with sem:
            attempt = 0
            while True:
                try:
                    msg = await client.send_file(
                        target_chat,
                        path,
                        caption=name if caption is None else caption,
                        supports_streaming=True,
                        progress_callback=agg.callback_for(path),
                    )
                    if registry is not None:
                        registry.add_messages(msg)
                    sent_messages.append(msg)
                    agg.mark_done(path)
                    await agg.render(force=True)
                    return
                except FloodWaitError as e:
                    attempt += 1
                    if attempt > flood_retries:
                        failed.append((name, f"FloodWait 重试 {flood_retries} 次仍失败"))
                        agg.mark_done(path)
                        return
                    await asyncio.sleep(e.seconds + 1)
                except Exception as e:
                    failed.append((name, str(e)))
                    agg.mark_done(path)
                    return

    await asyncio.gather(*[_upload_one(p) for p in files])
    return len(files) - len(failed), failed, sent_messages
