"""分块并行下载 parallel_download 的单元测试。

假 client 用内存里的一段 bytes 模拟 Telegram 文件，iter_download 按
offset/request_size 切片吐块，可注入指定 offset 的失败来测重试与清理。
"""
import asyncio
import os
from types import SimpleNamespace

import pytest

from downloader import parallel_download as pd


class FakeClient:
    def __init__(self, blob, fail_offsets=None, fail_times=1):
        self.blob = blob
        self.fail_offsets = dict.fromkeys(fail_offsets or (), fail_times)
        self.iter_calls = []          # 每次 iter_download 的起始 offset
        self.download_media_calls = 0
        self.inflight = 0
        self.peak = 0

    def iter_download(self, media, offset=0, request_size=None, **kw):
        self.iter_calls.append(offset)
        client = self

        class _Iter:
            def __init__(self):
                self.pos = offset
                self.entered = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.entered:
                    self.entered = True
                    client.inflight += 1
                    client.peak = max(client.peak, client.inflight)
                await asyncio.sleep(0)
                if client.fail_offsets.get(offset, 0) > 0:
                    client.fail_offsets[offset] -= 1
                    client.inflight -= 1
                    raise ConnectionError("chunk lost")
                if self.pos >= len(client.blob):
                    client.inflight -= 1
                    raise StopAsyncIteration
                chunk = client.blob[self.pos:self.pos + request_size]
                self.pos += len(chunk)
                return chunk

        return _Iter()

    async def download_media(self, media, file=None, progress_callback=None):
        self.download_media_calls += 1
        with open(file, "wb") as f:
            f.write(self.blob)
        return file


def _media(size):
    return SimpleNamespace(document=SimpleNamespace(size=size))


def run(coro):
    return asyncio.run(coro)


# ---------- plan_ranges：纯函数，先钉死切分正确性 ----------
def test_ranges_cover_everything_without_overlap():
    size = 25 * 1024 * 1024 + 12345      # 非对齐尾巴
    ranges = pd.plan_ranges(size, 3)
    assert sum(l for _, l in ranges) == size
    pos = 0
    for offset, length in ranges:        # 首尾相接、无空洞无重叠
        assert offset == pos
        assert offset % pd.CHUNK_SIZE == 0   # Telegram 要求 offset 对齐
        pos = offset + length
    assert pos == size


def test_small_file_fewer_ranges_than_connections():
    # 文件只有 1 个 chunk 时，不该切出 8 个空段
    ranges = pd.plan_ranges(100, 8)
    assert ranges == [(0, 100)]


def test_ranges_with_single_connection():
    size = 30 * 1024 * 1024
    assert pd.plan_ranges(size, 1) == [(0, size)]


# ---------- parallel_download ----------
def test_small_file_uses_download_media(tmp_path):
    c = FakeClient(b"x" * 1024)
    out = str(tmp_path / "small.mp4")
    p = run(pd.parallel_download(c, _media(1024), out))
    assert p == out
    assert c.download_media_calls == 1
    assert c.iter_calls == []            # 没走分块


def test_unknown_size_falls_back(tmp_path):
    c = FakeClient(b"x" * 1024)
    out = str(tmp_path / "nosize.mp4")
    media = SimpleNamespace()            # 取不到 size
    run(pd.parallel_download(c, media, out))
    assert c.download_media_calls == 1


def test_large_file_downloads_in_parallel_and_bytes_match(tmp_path):
    size = 12 * 1024 * 1024
    blob = bytes(range(256)) * (size // 256)
    c = FakeClient(blob)
    out = str(tmp_path / "big.mp4")
    p = run(pd.parallel_download(c, _media(size), out, connections=3))
    assert p == out
    assert len(c.iter_calls) == 3        # 每条连接一次 iter_download，而非每块一次
    assert c.peak > 1                    # 确实并行
    with open(out, "rb") as f:
        assert f.read() == blob          # 逐字节一致，分块没写串位置
    assert not os.path.exists(out + ".part")


def test_transient_failure_retries_and_succeeds(tmp_path):
    size = 12 * 1024 * 1024
    blob = b"a" * size
    ranges = pd.plan_ranges(size, 3)
    c = FakeClient(blob, fail_offsets=[ranges[1][0]], fail_times=1)
    out = str(tmp_path / "retry.mp4")
    run(pd.parallel_download(c, _media(size), out, connections=3))
    with open(out, "rb") as f:
        assert f.read() == blob
    # 失败段重试 = 该 offset 出现两次
    assert c.iter_calls.count(ranges[1][0]) == 2


def test_permanent_failure_cleans_partial_file(tmp_path):
    size = 12 * 1024 * 1024
    c = FakeClient(b"a" * size, fail_offsets=[0], fail_times=99)
    out = str(tmp_path / "fail.mp4")
    with pytest.raises(RuntimeError):
        run(pd.parallel_download(c, _media(size), out, connections=2))
    # 失败后既没有半成品 .part，也没有假装完整的目标文件
    assert not os.path.exists(out)
    assert not os.path.exists(out + ".part")


def test_progress_reaches_total_despite_retries(tmp_path):
    """重试会把已计数字节回退再重下，最终进度必须恰好等于文件大小。"""
    size = 12 * 1024 * 1024
    ranges = pd.plan_ranges(size, 3)
    c = FakeClient(b"b" * size, fail_offsets=[ranges[2][0]], fail_times=1)
    seen = []

    async def cb(current, total):
        seen.append((current, total))

    out = str(tmp_path / "prog.mp4")
    run(pd.parallel_download(c, _media(size), out, connections=3, progress_callback=cb))
    assert seen[-1] == (size, size)
    assert all(t == size for _, t in seen)


def test_media_size_extraction():
    assert pd.media_size(SimpleNamespace(document=SimpleNamespace(size=42))) == 42
    # message 对象：document 藏在 .media 里
    msg = SimpleNamespace(
        document=None,
        media=SimpleNamespace(document=SimpleNamespace(size=7)),
        file=None,
    )
    assert pd.media_size(msg) == 7
    assert pd.media_size(SimpleNamespace()) is None
