"""并发下载：把一组消息（相册/多条）里的媒体同时下载到本地。

与 media_upload.upload_videos 同构——用信号量限并发，进度用 AggregateProgress
汇总到一条状态消息上（并发场景无法在一条消息里画多个进度条）。

返回的路径列表**与传入的 msgs 顺序一致**，这点对相册很关键：下游要按原顺序
重新发成相册，顺序错了合并出来的相册就乱了。
"""
import asyncio
import os

from common.progress import AggregateProgress


def message_size(msg):
    """尽力取出一条消息里媒体的字节数；取不到返回 0（只影响进度条精度）。"""
    f = getattr(msg, "file", None)
    size = getattr(f, "size", None) if f is not None else None
    if size:
        return size
    doc = getattr(msg, "document", None)
    return getattr(doc, "size", 0) or 0 if doc is not None else 0


async def download_messages(client, msgs, download_dir, concurrency,
                            status_msg=None, title="📥 **正在下载到服务器…**"):
    """并发下载 msgs 里的媒体到 download_dir。

    - concurrency 限制同时下载的文件数（过高同样会触发 Telegram 限流）。
    - status_msg 非空时汇总显示「已完成文件数 / 已下载字节数」，否则静默。
    - 单个文件失败不影响其余：失败的位置在结果里被跳过。
    返回 (paths, failed)，paths 与 msgs 顺序一致，failed 为 [(序号, 原因)]。
    """
    if not msgs:
        return [], []

    os.makedirs(download_dir, exist_ok=True)

    agg = None
    if status_msg is not None:
        total_bytes = sum(message_size(m) for m in msgs)
        agg = AggregateProgress(status_msg, title, len(msgs), total_bytes)
        await agg.render(force=True)

    sem = asyncio.Semaphore(max(1, concurrency))
    results = [None] * len(msgs)
    failed = []

    async def _download_one(idx, msg):
        async with sem:
            try:
                results[idx] = await client.download_media(
                    msg,
                    file=download_dir + "/",
                    progress_callback=agg.callback_for(idx) if agg else None,
                )
            except Exception as e:
                failed.append((idx, str(e).strip() or type(e).__name__))
            finally:
                if agg:
                    agg.mark_done(idx)
                    await agg.render(force=True)

    await asyncio.gather(*[_download_one(i, m) for i, m in enumerate(msgs)])
    return [p for p in results if p], failed
