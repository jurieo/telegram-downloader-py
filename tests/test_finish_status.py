"""收尾提示 _finish_status 的分支覆盖。

main.py 在导入时读 .env 并构造 TelegramClient，所以先打桩环境变量、
再 patch TelegramClient，避免真的建连接。
"""
import asyncio
import os
import sys
from unittest import mock

import pytest


@pytest.fixture(scope="module")
def main_mod():
    env = {"API_ID": "1", "API_HASH": "x", "TARGET_CHAT": "", "SOURCE_CHAT": ""}
    with mock.patch.dict(os.environ, env), \
         mock.patch("telethon.TelegramClient"), \
         mock.patch("dotenv.load_dotenv"):
        sys.modules.pop("main", None)
        import main
        yield main
        sys.modules.pop("main", None)


class FakeStatus:
    def __init__(self):
        self.edits = []
        self.deleted = False

    async def edit(self, text):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


def run(coro):
    return asyncio.run(coro)


def test_deletes_status_when_replied_and_clean(main_mod):
    """回复成功且无跳过/失败：视频即凭证，状态消息整条删掉。"""
    st = FakeStatus()
    run(main_mod._finish_status(st, replied=2, uploaded=2, target_count=3,
                                skipped=[], failed=[]))
    assert st.deleted
    assert st.edits == []


def test_keeps_upload_line_when_not_replied(main_mod):
    """回复失败：没有视频做凭证，必须保留「已上传」提示。"""
    st = FakeStatus()
    run(main_mod._finish_status(st, replied=0, uploaded=2, target_count=3,
                                skipped=[], failed=[]))
    assert not st.deleted
    assert "已上传 2 个视频到 3 个目标" in st.edits[-1]


def test_replied_but_skipped_keeps_only_extras(main_mod):
    """回复成功但有跳过：删掉「已上传」，保留跳过说明。"""
    st = FakeStatus()
    run(main_mod._finish_status(st, replied=1, uploaded=1, target_count=2,
                                skipped=[("big.mp4", 5 * 1024 ** 3, "超限")],
                                failed=[]))
    assert not st.deleted
    text = st.edits[-1]
    assert "已上传" not in text
    assert "big.mp4" in text and "跳过" in text


def test_replied_but_failed_keeps_failure_detail(main_mod):
    st = FakeStatus()
    run(main_mod._finish_status(st, replied=1, uploaded=1, target_count=1,
                                skipped=[], failed=[("a.mp4", "FloodWait")]))
    text = st.edits[-1]
    assert "已上传" not in text
    assert "a.mp4" in text and "FloodWait" in text


def test_delete_failure_is_swallowed(main_mod):
    """状态消息已被手动删除时，delete 抛错不应炸掉主流程。"""
    class Boom(FakeStatus):
        async def delete(self):
            raise RuntimeError("message not found")

    run(main_mod._finish_status(Boom(), replied=1, uploaded=1, target_count=1,
                                skipped=[], failed=[]))
