"""仅下载模式下收藏夹直发媒体落盘（save_saved_media 及路由分支）。

main.py 导入时读 .env 并构造 TelegramClient，打桩方式同 test_finish_status。
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest


@pytest.fixture()
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

    async def edit(self, text):
        self.edits.append(text)


class FakeClient:
    """仅覆盖 save_saved_media 用到的方法。"""

    def __init__(self):
        self.sent = []

    async def send_message(self, peer, text):
        st = FakeStatus()
        self.sent.append((peer, text, st))
        return st


def _event(chat_id=999, mid=1, media=True):
    msg = SimpleNamespace(
        id=mid, chat_id=chat_id, media=(object() if media else None),
        grouped_id=None, raw_text="", video=object(), gif=None,
        video_note=None, document=None, file=None,
    )
    ev = SimpleNamespace(chat_id=chat_id, message=msg)

    async def reply(text):
        ev.status = FakeStatus()
        return ev.status
    ev.reply = reply
    return ev


def run(coro):
    return asyncio.run(coro)


def test_saves_media_via_download_pipeline(main_mod, monkeypatch):
    """走 media_download 主路径（并发/分块设施），而非旁路串行下载。"""
    calls = {}

    async def fake_dl(client, msgs, download_dir, concurrency, **kw):
        calls["msgs"] = msgs
        calls["concurrency"] = concurrency
        calls["connections"] = kw.get("connections")
        return ["./downloads/a.mp4", "./downloads/b.mp4"], []

    monkeypatch.setattr(main_mod.media_download, "download_messages", fake_dl)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    ev = _event()
    run(main_mod.save_saved_media(ev, [ev.message, ev.message]))

    assert len(calls["msgs"]) == 2
    assert calls["concurrency"] == main_mod.MAX_DOWNLOAD_CONCURRENCY
    assert calls["connections"] == main_mod.DOWNLOAD_CONNECTIONS
    assert "下载完成" in ev.status.edits[-1]
    assert "a.mp4" in ev.status.edits[-1]


def test_reports_when_nothing_saved(main_mod, monkeypatch):
    async def fake_dl(*a, **kw):
        return [], [(0, "boom")]

    monkeypatch.setattr(main_mod.media_download, "download_messages", fake_dl)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    ev = _event()
    run(main_mod.save_saved_media(ev, [ev.message]))
    assert "下载失败" in ev.status.edits[-1]
    assert "boom" in ev.status.edits[-1]


def test_partial_failure_lists_both(main_mod, monkeypatch):
    async def fake_dl(*a, **kw):
        return ["./downloads/ok.mp4"], [(1, "timeout")]

    monkeypatch.setattr(main_mod.media_download, "download_messages", fake_dl)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    ev = _event()
    run(main_mod.save_saved_media(ev, [ev.message, ev.message]))
    text = ev.status.edits[-1]
    assert "ok.mp4" in text and "timeout" in text


def test_download_only_mode_routes_saved_media(main_mod, monkeypatch):
    """TARGET_IDS 为空 + 收藏夹直发视频 → 走落盘而非静默忽略。"""
    saved = []

    async def fake_save(event, msgs):
        saved.append(msgs)

    monkeypatch.setattr(main_mod, "save_saved_media", fake_save)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    monkeypatch.setattr(main_mod, "TARGET_IDS", [])
    ev = _event(chat_id=999)
    run(main_mod.on_new_message(ev))
    assert len(saved) == 1 and saved[0] == [ev.message]


def test_with_targets_still_mirrors_not_saves(main_mod, monkeypatch):
    """配置了备份目标时行为不变：走 mirror，不落盘。"""
    mirrored, saved = [], []

    async def fake_mirror(client, targets, msgs, registry, **kw):
        mirrored.append((targets, msgs))
        return True

    async def fake_save(event, msgs):
        saved.append(msgs)

    monkeypatch.setattr(main_mod.forwarder, "mirror", fake_mirror)
    monkeypatch.setattr(main_mod, "save_saved_media", fake_save)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    monkeypatch.setattr(main_mod, "TARGET_IDS", [-100222])
    ev = _event(chat_id=999)
    run(main_mod.on_new_message(ev))
    assert saved == []
    assert mirrored and mirrored[0][0] == [-100222]


def test_album_download_only_saves_whole_group(main_mod, monkeypatch):
    saved = []

    async def fake_save(event, msgs):
        saved.append(msgs)

    monkeypatch.setattr(main_mod, "save_saved_media", fake_save)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    monkeypatch.setattr(main_mod, "TARGET_IDS", [])
    msgs = [_event(chat_id=999, mid=i).message for i in (1, 2, 3)]
    ev = SimpleNamespace(chat_id=999, messages=msgs)
    run(main_mod.on_album(ev))
    assert len(saved) == 1 and len(saved[0]) == 3


def test_album_in_unmonitored_group_not_saved(main_mod, monkeypatch):
    """仅下载模式覆盖收藏夹与 SOURCE_CHAT；其余会话不落盘。"""
    saved = []

    async def fake_save(event, msgs):
        saved.append(msgs)

    async def fake_mirror(*a, **kw):
        return False

    monkeypatch.setattr(main_mod, "save_saved_media", fake_save)
    monkeypatch.setattr(main_mod.forwarder, "mirror", fake_mirror)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    monkeypatch.setattr(main_mod, "TARGET_IDS", [])
    monkeypatch.setattr(main_mod, "SOURCE_IDS", [])
    msgs = [_event(chat_id=-100333, mid=i).message for i in (1, 2)]
    ev = SimpleNamespace(chat_id=-100333, messages=msgs)
    run(main_mod.on_album(ev))
    assert saved == []


def test_source_chat_media_saved_in_download_only(main_mod, monkeypatch):
    """TARGET_CHAT 空但配了 SOURCE_CHAT：源里的新媒体也要下载，不能静默丢弃。"""
    saved = []

    async def fake_save(event, msgs):
        saved.append((event.chat_id, msgs))

    async def fake_mirror(*a, **kw):
        raise AssertionError("仅下载模式不应走转发")

    monkeypatch.setattr(main_mod, "save_saved_media", fake_save)
    monkeypatch.setattr(main_mod.forwarder, "mirror", fake_mirror)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    monkeypatch.setattr(main_mod, "TARGET_IDS", [])
    monkeypatch.setattr(main_mod, "SOURCE_IDS", [12345])
    ev = _event(chat_id=12345)
    run(main_mod.on_new_message(ev))
    assert saved and saved[0][0] == 12345


def test_source_album_saved_in_download_only(main_mod, monkeypatch):
    saved = []

    async def fake_save(event, msgs):
        saved.append(msgs)

    monkeypatch.setattr(main_mod, "save_saved_media", fake_save)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    monkeypatch.setattr(main_mod, "TARGET_IDS", [])
    monkeypatch.setattr(main_mod, "SOURCE_IDS", [12345])
    msgs = [_event(chat_id=12345, mid=i).message for i in (1, 2)]
    ev = SimpleNamespace(chat_id=12345, messages=msgs)
    run(main_mod.on_album(ev))
    assert len(saved) == 1 and len(saved[0]) == 2


def test_unmonitored_chat_not_saved(main_mod, monkeypatch):
    """既不是收藏夹也不在 SOURCE_IDS 的会话不落盘。"""
    saved = []

    async def fake_save(event, msgs):
        saved.append(msgs)

    async def fake_mirror(*a, **kw):
        return False

    monkeypatch.setattr(main_mod, "save_saved_media", fake_save)
    monkeypatch.setattr(main_mod.forwarder, "mirror", fake_mirror)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    monkeypatch.setattr(main_mod, "TARGET_IDS", [])
    monkeypatch.setattr(main_mod, "SOURCE_IDS", [12345])
    ev = _event(chat_id=-100777)
    run(main_mod.on_new_message(ev))
    assert saved == []


def test_source_status_message_goes_to_saved_messages(main_mod, monkeypatch):
    """源会话的进度不刷在源里（可能是机器人对话或无发言权的频道）。"""
    async def fake_dl(*a, **kw):
        return ["./downloads/x.mp4"], []

    monkeypatch.setattr(main_mod.media_download, "download_messages", fake_dl)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    fake_client = FakeClient()
    monkeypatch.setattr(main_mod, "client", fake_client)

    ev = _event(chat_id=12345)
    replied = []
    ev.reply = lambda text: replied.append(text)

    run(main_mod.save_saved_media(ev, [ev.message]))
    assert replied == []
    assert fake_client.sent and fake_client.sent[0][0] == "me"
    assert "下载完成" in fake_client.sent[0][2].edits[-1]


def test_saved_chat_status_replies_inline(main_mod, monkeypatch):
    """收藏夹里的媒体：状态消息仍作为回复挂在原消息下。"""
    async def fake_dl(*a, **kw):
        return ["./downloads/x.mp4"], []

    monkeypatch.setattr(main_mod.media_download, "download_messages", fake_dl)
    monkeypatch.setattr(main_mod, "ME_ID", 999)
    ev = _event(chat_id=999)
    run(main_mod.save_saved_media(ev, [ev.message]))
    assert "下载完成" in ev.status.edits[-1]
