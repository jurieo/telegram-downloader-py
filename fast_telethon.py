import asyncio
import inspect
import logging
import math
import os

from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument


log = logging.getLogger(__name__)

CHUNK_SIZE = 512 * 1024
MAX_CHUNK_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 0.5
MAX_DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_BASE_DELAY = 2


async def parallel_download(
    client: TelegramClient,
    msg_media,
    file_path: str,
    progress_callback=None,
    connections: int = 3,
) -> str:
    """Download large Telegram media in parallel without publishing partial files."""
    if isinstance(msg_media, MessageMediaDocument):
        size = msg_media.document.size
    else:
        size = getattr(msg_media, "size", None)
        if size is None and hasattr(msg_media, "document"):
            size = msg_media.document.size

    if not size or size < 10 * 1024 * 1024:
        return await client.download_media(msg_media, file=file_path, progress_callback=progress_callback)

    part_count = math.ceil(size / CHUNK_SIZE)
    partial_path = f"{file_path}.part"
    downloaded = 0
    progress_lock = asyncio.Lock()

    def remove_partial_file():
        try:
            os.remove(partial_path)
        except FileNotFoundError:
            pass

    with open(partial_path, "wb") as output:
        output.truncate(size)

    async def download_part(part):
        offset = part * CHUNK_SIZE
        expected_bytes = min(CHUNK_SIZE, size - offset)
        last_error = None

        for attempt in range(1, MAX_CHUNK_RETRIES + 1):
            try:
                chunk = None
                async for candidate in client.iter_download(
                    msg_media,
                    offset=offset,
                    request_size=CHUNK_SIZE,
                    chunk_size=CHUNK_SIZE,
                    limit=1,
                ):
                    chunk = bytes(candidate)
                    break

                if chunk is None:
                    raise RuntimeError("Telegram returned no data")
                if len(chunk) != expected_bytes:
                    raise RuntimeError(
                        f"incomplete chunk: expected {expected_bytes} bytes, got {len(chunk)}"
                    )

                with open(partial_path, "rb+") as output:
                    output.seek(offset)
                    output.write(chunk)
                return len(chunk)
            except Exception as exc:
                last_error = exc
                if attempt == MAX_CHUNK_RETRIES:
                    break
                log.warning(
                    "chunk %s failed on attempt %s/%s: %s; retrying",
                    part,
                    attempt,
                    MAX_CHUNK_RETRIES,
                    exc,
                )
                await asyncio.sleep(RETRY_BASE_DELAY_SECONDS * attempt)

        raise RuntimeError(
            f"chunk {part} failed after {MAX_CHUNK_RETRIES} attempts: {last_error}"
        ) from last_error

    async def worker(queue):
        nonlocal downloaded
        while True:
            try:
                part = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            try:
                written = await download_part(part)
                async with progress_lock:
                    downloaded += written
                    current_downloaded = downloaded

                if progress_callback:
                    result = progress_callback(current_downloaded, size)
                    if inspect.isawaitable(result):
                        await result
            finally:
                queue.task_done()

    queue = asyncio.Queue()
    for part in range(part_count):
        queue.put_nowait(part)

    tasks = [asyncio.create_task(worker(queue)) for _ in range(max(1, connections))]
    try:
        await asyncio.gather(*tasks)
        os.replace(partial_path, file_path)
        return file_path
    except Exception:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        remove_partial_file()
        raise
