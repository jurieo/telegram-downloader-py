import asyncio
from types import SimpleNamespace

import forwarder
from routing import SentRegistry

B, C = -100222, -100333


class FakeClient:
    """记录 forward_messages / send_file / download_media 调用的假客户端。"""

    def __init__(self):
        self.forwards = []   # [(target, messages, drop_author)]
        self.sends = []      # [(target, file, caption)]
        self.downloads = []  # [message]
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
