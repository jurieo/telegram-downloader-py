import asyncio
import time
from types import SimpleNamespace

from telethon.errors import FloodWaitError

from transfer import forwarder
from transfer.routing import SentRegistry

B, C = -100222, -100333


class FakeClient:
    """记录 forward_messages / send_file / download_media / send_message 调用的假客户端。"""

    def __init__(self):
        self.forwards = []   # [(target, messages, drop_author)]
        self.sends = []      # [(target, file, caption)]
        self.downloads = []  # [message]
        self.messages = []   # [(peer, text)] —— send_message（状态提醒）
        self._id = 1000

    def _new(self, target):
        self._id += 1
        return SimpleNamespace(chat_id=target, id=self._id)

    async def forward_messages(self, target, messages, drop_author=None):
        self.forwards.append((target, messages, drop_author))
        if isinstance(messages, (list, tuple)):
            return [self._new(target) for _ in messages]
        return self._new(target)

    async def send_file(self, target, file, caption=None, supports_streaming=None,
                        progress_callback=None, **kw):
        self.sends.append((target, file, caption))
        if isinstance(file, (list, tuple)):
            return [self._new(target) for _ in file]
        return self._new(target)

    async def download_media(self, message, file=None, progress_callback=None):
        self.downloads.append(message)
        return (file or "") + f"f{len(self.downloads)}.mp4"

    async def send_message(self, peer, text):
        self.messages.append((peer, text))
        return self._new(peer)


class FloodOnceClient(FakeClient):
    """第一次 forward_messages 抛 FloodWait，之后正常（模拟限流）。"""

    def __init__(self, seconds=1447):
        super().__init__()
        self.seconds = seconds
        self._flooded = False

    async def forward_messages(self, target, messages, drop_author=None):
        if not self._flooded:
            self._flooded = True
            raise FloodWaitError(request=None, capture=self.seconds)
        return await super().forward_messages(target, messages, drop_author=drop_author)


def _msg(noforwards=False, media=True, grouped_id=None, text="", mid=1):
    return SimpleNamespace(
        noforwards=noforwards, media=(object() if media else None),
        grouped_id=grouped_id, text=text, document=None, id=mid, chat_id=-100999,
    )


def run(coro):
    return asyncio.run(coro)


def test_direct_forward_to_each_target_with_drop_author():
    c = FakeClient()
    reg = SentRegistry()
    msgs = [_msg()]
    ok = run(forwarder.mirror(c, [B, C], msgs, reg))
    assert ok is True
    assert len(c.forwards) == 2
    assert c.forwards[0][0] == B and c.forwards[0][2] is True
    assert c.forwards[1][0] == C and c.forwards[1][2] is True
    assert c.sends == [] and c.downloads == []
    # 两次转发的返回消息都登记进 registry（防回环）
    assert len(reg._set) == 2


def test_album_forwarded_as_whole_group():
    c = FakeClient()
    reg = SentRegistry()
    msgs = [_msg(grouped_id=7, mid=1), _msg(grouped_id=7, mid=2)]
    run(forwarder.mirror(c, [B], msgs, reg))
    assert len(c.forwards) == 1
    # 整组作为列表一次性转发，保留合并
    assert c.forwards[0][1] == msgs


def test_restricted_triggers_download_reupload_then_fanout():
    c = FakeClient()
    reg = SentRegistry()
    msgs = [_msg(noforwards=True)]
    run(forwarder.mirror(c, [B, C], msgs, reg))
    assert len(c.downloads) == 1            # 下载源
    assert len(c.sends) == 1                # 上传到首个目标
    assert c.sends[0][0] == B
    assert len(c.forwards) == 1             # 从首个目标转给其余
    assert c.forwards[0][0] == C and c.forwards[0][2] is True
    # 上传 1 条 + 转发 1 条都登记（防回环）
    assert len(reg._set) == 2


def test_restricted_album_reuploads_file_list():
    c = FakeClient()
    reg = SentRegistry()
    msgs = [_msg(noforwards=True, grouped_id=9, mid=1),
            _msg(noforwards=True, grouped_id=9, mid=2)]
    run(forwarder.mirror(c, [B, C], msgs, reg))
    assert len(c.downloads) == 2
    assert len(c.sends) == 1
    # 相册以文件列表整组重发
    assert isinstance(c.sends[0][1], list) and len(c.sends[0][1]) == 2
    assert len(c.forwards) == 1 and c.forwards[0][0] == C


def test_noop_without_targets_or_media():
    c = FakeClient()
    reg = SentRegistry()
    assert run(forwarder.mirror(c, [], [_msg()], reg)) is False
    assert run(forwarder.mirror(c, [B], [_msg(media=False)], reg)) is False
    assert c.forwards == [] and c.sends == [] and c.downloads == []


# ---------- FloodWait 限流冷却 ----------
def test_floodwait_enters_cooldown_and_notifies():
    c = FloodOnceClient(seconds=1447)
    reg = SentRegistry()
    ok = run(forwarder.mirror(c, [B, C], [_msg()], reg))
    assert ok is False                     # 命中限流：本次未成功转发
    assert forwarder.in_cooldown()         # 进入冷却
    # 往收藏夹发了一条「已暂停」提醒
    assert len(c.messages) == 1
    assert c.messages[0][0] == "me" and "暂停" in c.messages[0][1]


def test_skips_all_forwards_while_in_cooldown():
    c = FloodOnceClient(seconds=1447)
    reg = SentRegistry()
    run(forwarder.mirror(c, [B, C], [_msg()], reg))     # 触发冷却
    forwards_before = len(c.forwards)
    notify_before = len(c.messages)
    # 冷却期内再来消息：直接跳过，不产生任何新的转发请求
    ok = run(forwarder.mirror(c, [B, C], [_msg(mid=2)], reg))
    assert ok is False
    assert len(c.forwards) == forwards_before
    assert len(c.messages) == notify_before   # 跳过时不重复刷提醒


def test_resume_after_cooldown_notifies_and_forwards():
    c = FloodOnceClient(seconds=1447)
    reg = SentRegistry()
    run(forwarder.mirror(c, [B, C], [_msg()], reg))     # 触发冷却
    forwarder.reset_flood_state()                        # 模拟冷却到期
    forwarder._cooldown_active = True                    # 但仍标记「刚从冷却出来」
    ok = run(forwarder.mirror(c, [B], [_msg(mid=3)], reg))
    assert ok is True
    # 发了「已恢复」提醒，且恢复了正常转发
    assert any("恢复" in text for _, text in c.messages)
    assert len(c.forwards) >= 1


def test_pace_enforces_interval_between_forwards():
    c = FakeClient()
    reg = SentRegistry()
    forwarder.FORWARD_INTERVAL = 0.05
    try:
        start = time.monotonic()
        run(forwarder.mirror(c, [B, C], [_msg()], reg))   # 两个目标 → 至少间隔一次
        elapsed = time.monotonic() - start
    finally:
        forwarder.FORWARD_INTERVAL = 0
    assert len(c.forwards) == 2
    assert elapsed >= 0.05



def test_reupload_deletes_local_files_by_default(monkeypatch):
    removed = []
    monkeypatch.setattr(forwarder.os, "remove", lambda p: removed.append(p))
    monkeypatch.setattr(forwarder, "DELETE_AFTER_UPLOAD", True)
    c = FakeClient()
    run(forwarder.mirror(c, [B], [_msg(noforwards=True)], SentRegistry()))
    assert removed == ["./downloads/f1.mp4"]


def test_reupload_keeps_local_files_when_delete_disabled(monkeypatch):
    removed = []
    monkeypatch.setattr(forwarder.os, "remove", lambda p: removed.append(p))
    monkeypatch.setattr(forwarder, "DELETE_AFTER_UPLOAD", False)
    c = FakeClient()
    run(forwarder.mirror(c, [B], [_msg(noforwards=True)], SentRegistry()))
    assert removed == []
