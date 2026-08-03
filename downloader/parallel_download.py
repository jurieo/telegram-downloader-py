"""单文件分块并行下载：把一个大文件切成若干连续区间，多条连接同时拉。

这是「文件内部」的加速，与 transfer/media_download.py 的「多文件之间」并发
正交：一个相册 4 个视频，外层 4 个文件并发，每个文件内部再 3 条连接分块，
两层叠加。

与逐块请求的写法不同，这里按 **连续区间** 切分——每条连接只调一次
iter_download 并顺序读完自己那段。telethon 的 iter_download 每次调用都要
定位 DC、建会话，按 512KB 逐块调用的话 1GB 文件要建 2048 次，开销反过来
盖掉并行的收益。

小文件直接走 client.download_media：分块的固定开销对小文件不划算，而且
Telegram 对同一文件的并发连接数敏感，没必要为几 MB 冒限流的风险。
"""
import asyncio
import inspect
import math
import os

# Telegram 要求 offset 是 512KB 的整数倍，这个值不能随意改小
CHUNK_SIZE = 512 * 1024
# 低于此大小直接单连接下载，不分块
MIN_PARALLEL_SIZE = 10 * 1024 * 1024
MAX_PART_RETRIES = 3
RETRY_BASE_DELAY = 0.5


def media_size(msg_media):
    """尽力取出媒体字节数；取不到返回 None（调用方据此退回单连接下载）。"""
    doc = getattr(msg_media, "document", None)
    if doc is not None and getattr(doc, "size", None):
        return doc.size
    media = getattr(msg_media, "media", None)
    if media is not None:
        doc = getattr(media, "document", None)
        if doc is not None and getattr(doc, "size", None):
            return doc.size
    f = getattr(msg_media, "file", None)
    if f is not None and getattr(f, "size", None):
        return f.size
    return getattr(msg_media, "size", None)


def plan_ranges(size, connections):
    """把 [0, size) 切成 connections 段连续区间，每段起点对齐 CHUNK_SIZE。

    返回 [(offset, length), ...]。段数可能少于 connections（文件不够大时），
    保证不产生空段，也保证覆盖完整、互不重叠。
    """
    total_chunks = math.ceil(size / CHUNK_SIZE)
    n = max(1, min(connections, total_chunks))
    chunks_per_part = math.ceil(total_chunks / n)

    ranges = []
    for i in range(n):
        start_chunk = i * chunks_per_part
        if start_chunk >= total_chunks:
            break
        offset = start_chunk * CHUNK_SIZE
        end = min(size, (start_chunk + chunks_per_part) * CHUNK_SIZE)
        ranges.append((offset, end - offset))
    return ranges


async def _fetch_range(client, msg_media, path, offset, length, on_bytes):
    """把 [offset, offset+length) 写入 path 的对应位置，失败重试整段。"""
    last_error = None
    for attempt in range(1, MAX_PART_RETRIES + 1):
        written = 0
        try:
            # 用 'rb+' 定位写入：文件已被 truncate 到最终大小，各段互不干扰
            with open(path, "rb+") as out:
                out.seek(offset)
                async for chunk in client.iter_download(
                    msg_media,
                    offset=offset,
                    request_size=CHUNK_SIZE,
                ):
                    if written >= length:
                        break
                    # 末段可能多读，裁掉超出部分，避免越界写坏下一段
                    data = bytes(chunk)[: length - written]
                    out.write(data)
                    written += len(data)
                    await on_bytes(len(data))
            if written != length:
                raise RuntimeError(f"区间不完整：期望 {length} 字节，实际 {written}")
            return
        except Exception as e:
            last_error = e
            # 重试前把本次已计入的字节退回，否则进度会越滚越大
            if written:
                await on_bytes(-written)
            if attempt == MAX_PART_RETRIES:
                break
            print(f"⚠️ 分块 offset={offset} 第 {attempt}/{MAX_PART_RETRIES} 次失败：{e}，重试中")
            await asyncio.sleep(RETRY_BASE_DELAY * attempt)

    detail = str(last_error).strip() or type(last_error).__name__
    raise RuntimeError(f"分块 offset={offset} 重试 {MAX_PART_RETRIES} 次仍失败：{detail}")


async def parallel_download(client, msg_media, file_path, progress_callback=None,
                            connections=3):
    """分块并行下载到 file_path，返回最终路径。

    - 小于 MIN_PARALLEL_SIZE 或取不到大小时退回 client.download_media。
    - 先写 .part 再 os.replace 原子改名：中途失败不会留下看似完整的文件，
      下游按「文件存在即下载成功」判断时不会误读半个文件。
    """
    size = media_size(msg_media)
    if not size or size < MIN_PARALLEL_SIZE:
        return await client.download_media(
            msg_media, file=file_path, progress_callback=progress_callback
        )

    partial = f"{file_path}.part"
    downloaded = 0
    lock = asyncio.Lock()

    async def on_bytes(delta):
        nonlocal downloaded
        async with lock:
            downloaded += delta
            current = downloaded
        if progress_callback:
            result = progress_callback(current, size)
            if inspect.isawaitable(result):
                await result

    # 预分配到最终大小，各段才能安全地 seek 到自己的位置写
    with open(partial, "wb") as out:
        out.truncate(size)

    try:
        await asyncio.gather(*[
            _fetch_range(client, msg_media, partial, offset, length, on_bytes)
            for offset, length in plan_ranges(size, connections)
        ])
        os.replace(partial, file_path)
        return file_path
    except Exception:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
