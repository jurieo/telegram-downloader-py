import asyncio
from types import SimpleNamespace

import pytest

from transfer import media_download


class FakeClient:
    """假客户端：记录下载调用，并统计同时在下载的峰值并发数。"""

    def __init__(self, delay=0.02, fail_on=()):
        self.delay = delay
        self.fail_on = set(fail_on)      # 会抛异常的消息 id
        self.downloads = []              # [message]
        self.inflight = 0
        self.peak = 0

    async def download_media(self, message, file=None, progress_callback=None):
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(self.delay)
            if message.id in self.fail_on:
                raise RuntimeError("boom")
            self.downloads.append(message)
            if progress_callback:
                await progress_callback(100, 100)
            # file 现在是调用方算好的完整路径，原样返回即可
            return file
        finally:
            self.inflight -= 1


class FakeStatus:
    def __init__(self):
        self.texts = []

    async def edit(self, text):
        self.texts.append(text)


def _msg(mid, size=1024):
    return SimpleNamespace(
        id=mid, media=object(), document=SimpleNamespace(size=size),
        file=SimpleNamespace(size=size, name=f"{mid}.mp4"),
    )


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def dl(tmp_path):
    """下载目录用 pytest 临时目录，避免在工作区留下垃圾。"""
    return str(tmp_path)


def test_downloads_run_concurrently(dl):
    c = FakeClient(delay=0.05)
    msgs = [_msg(i) for i in range(1, 6)]
    paths, failed = run(media_download.download_messages(c, msgs, dl, 5))
    assert len(paths) == 5 and failed == []
    assert c.peak > 1          # 确实并发，而非一个一个下


def test_concurrency_is_capped_by_semaphore(dl):
    c = FakeClient(delay=0.02)
    msgs = [_msg(i) for i in range(1, 9)]
    run(media_download.download_messages(c, msgs, dl, 3))
    assert c.peak <= 3


def test_concurrency_one_is_serial(dl):
    c = FakeClient(delay=0.01)
    msgs = [_msg(i) for i in range(1, 5)]
    run(media_download.download_messages(c, msgs, dl, 1))
    assert c.peak == 1


def test_paths_keep_input_order_for_album(dl):
    # 让后面的消息下得更快，验证结果仍按传入顺序（相册顺序不能乱）
    class Staggered(FakeClient):
        async def download_media(self, message, file=None, progress_callback=None):
            await asyncio.sleep(0.05 / message.id)
            return file

    c = Staggered()
    msgs = [_msg(i) for i in range(1, 5)]
    paths, _ = run(media_download.download_messages(c, msgs, dl, 4))
    assert [p.rsplit("/", 1)[-1] for p in paths] == \
        ["1.mp4", "2.mp4", "3.mp4", "4.mp4"]


def test_one_failure_does_not_block_others(dl):
    c = FakeClient(delay=0.01, fail_on=(2,))
    msgs = [_msg(i) for i in range(1, 5)]
    paths, failed = run(media_download.download_messages(c, msgs, dl, 4))
    assert len(paths) == 3                      # 失败的那个被跳过
    assert [idx for idx, _ in failed] == [1]    # 序号 1 = 第 2 个
    assert not any(p.endswith("2.mp4") for p in paths)


def test_progress_rendered_to_status_message(dl):
    c = FakeClient(delay=0.01)
    st = FakeStatus()
    run(media_download.download_messages(c, [_msg(1), _msg(2)], dl, 2, status_msg=st))
    assert st.texts
    assert any("2/2" in t for t in st.texts)    # 汇总显示完成数


def test_empty_input_is_noop(dl):
    c = FakeClient()
    assert run(media_download.download_messages(c, [], dl, 4)) == ([], [])
    assert c.downloads == []


def test_message_size_falls_back_to_document():
    assert media_download.message_size(_msg(1, size=2048)) == 2048
    no_file = SimpleNamespace(file=None, document=SimpleNamespace(size=777))
    assert media_download.message_size(no_file) == 777
    nothing = SimpleNamespace(file=None, document=None)
    assert media_download.message_size(nothing) == 0
