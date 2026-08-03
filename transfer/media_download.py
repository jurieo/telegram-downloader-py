"""并发下载：把一组消息（相册/多条）里的媒体同时下载到本地。

与 media_upload.upload_videos 同构——用信号量限并发，进度用 AggregateProgress
汇总到一条状态消息上（并发场景无法在一条消息里画多个进度条）。

返回的路径列表**与传入的 msgs 顺序一致**，这点对相册很关键：下游要按原顺序
重新发成相册，顺序错了合并出来的相册就乱了。
"""
import asyncio
import os

from common.progress import AggregateProgress
from downloader.parallel_download import parallel_download


def message_size(msg):
    """尽力取出一条消息里媒体的字节数；取不到返回 0（只影响进度条精度）。"""
    f = getattr(msg, "file", None)
    size = getattr(f, "size", None) if f is not None else None
    if size:
        return size
    doc = getattr(msg, "document", None)
    return getattr(doc, "size", 0) or 0 if doc is not None else 0


def _target_path(msg, download_dir, taken):
    """给消息挑一个本地落地路径；名字冲突时加 _1/_2 后缀。

    分块下载必须先知道确切路径（要预分配再定位写），不能像 download_media
    那样只给目录让 telethon 自己命名。taken 记录本批已占用的名字，避免同一
    相册里两个同名文件互相覆盖。
    """
    f = getattr(msg, "file", None)
    name = getattr(f, "name", None) if f is not None else None
    if not name:
        ext = getattr(f, "ext", None) if f is not None else None
        name = f"{getattr(msg, 'id', 'media')}{ext or '.bin'}"
    name = os.path.basename(name)

    stem, ext = os.path.splitext(name)
    candidate = name
    i = 0
    while candidate in taken or os.path.exists(os.path.join(download_dir, candidate)):
        i += 1
        candidate = f"{stem}_{i}{ext}"
    taken.add(candidate)
    return os.path.join(download_dir, candidate)


async def download_messages(client, msgs, download_dir, concurrency,
                            status_msg=None, title="📥 **正在下载到服务器…**",
                            connections=3):
    """并发下载 msgs 里的媒体到 download_dir。

    - concurrency 限制同时下载的文件数（过高同样会触发 Telegram 限流）。
    - connections 是每个大文件内部的分块连接数，与 concurrency 叠加：
      同时下载的连接总数约为两者相乘，调高前留意限流。
    - status_msg 非空时汇总显示「已完成文件数 / 已下载字节数」，否则静默。
    - 单个文件失败不影响其余：失败的位置在结果里被跳过。
    返回 (paths, failed)，paths 与 msgs 顺序一致，failed 为 [(序号, 原因)]。
    """
    if not msgs:
        return [], []

    os.makedirs(download_dir, exist_ok=True)
    taken = set()

    agg = None
    if status_msg is not None:
        total_bytes = sum(message_size(m) for m in msgs)
        agg = AggregateProgress(status_msg, title, len(msgs), total_bytes)
        await agg.render(force=True)

    # 路径必须串行算完再并发下载：并发里算名字会两个协程同时看到「未占用」
    paths = [_target_path(m, download_dir, taken) for m in msgs]

    sem = asyncio.Semaphore(max(1, concurrency))
    results = [None] * len(msgs)
    failed = []

    async def _download_one(idx, msg):
        async with sem:
            try:
                results[idx] = await parallel_download(
                    client,
                    msg,
                    paths[idx],
                    progress_callback=agg.callback_for(idx) if agg else None,
                    connections=connections,
                )
            except Exception as e:
                failed.append((idx, str(e).strip() or type(e).__name__))
            finally:
                if agg:
                    agg.mark_done(idx)
                    await agg.render(force=True)

    await asyncio.gather(*[_download_one(i, m) for i, m in enumerate(msgs)])
    return [p for p in results if p], failed
