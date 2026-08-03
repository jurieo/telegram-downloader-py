import asyncio
from types import SimpleNamespace

from transfer import media_upload
from transfer.routing import SentRegistry

ME = 12345


class FakeClient:
    """记录 send_file 调用的假客户端。"""

    def __init__(self):
        self.sends = []   # [(target, file, reply_to)]
        self._id = 500

    async def send_file(self, target, file, reply_to=None, **kw):
        self.sends.append((target, file, reply_to))
        self._id += 1
        if isinstance(file, (list, tuple)):
            return [SimpleNamespace(chat_id=target, id=self._id + i) for i in range(len(file))]
        return SimpleNamespace(chat_id=target, id=self._id)


def _event(mid=7):
    return SimpleNamespace(chat_id=ME, message=SimpleNamespace(id=mid))


def _sent(n, with_media=True):
    return [
        SimpleNamespace(chat_id=-100111, id=900 + i,
                        media=(object() if with_media else None))
        for i in range(n)
    ]


def run(coro):
    return asyncio.run(coro)


def test_single_video_replies_to_the_link_message():
    c = FakeClient()
    sent = _sent(1)
    n = run(media_upload.reply_with_media(c, _event(mid=7), sent))
    assert n == 1
    target, file, reply_to = c.sends[0]
    assert target == ME
    assert reply_to == 7               # 回复到链接那条消息
    assert file is sent[0].media       # 复用已上传的媒体，不重传本地文件


def test_multiple_videos_reply_as_one_album():
    c = FakeClient()
    sent = _sent(3)
    n = run(media_upload.reply_with_media(c, _event(), sent))
    assert n == 3
    assert len(c.sends) == 1                       # 一次发送 → 合成相册
    assert c.sends[0][1] == [m.media for m in sent]


def test_replied_messages_registered_to_avoid_reforward():
    c = FakeClient()
    reg = SentRegistry()
    run(media_upload.reply_with_media(c, _event(), _sent(2), registry=reg))
    # 回复落在收藏夹里，登记后事件回调命中即跳过，不会被二次镜像
    assert len(reg._set) == 2


def test_noop_without_uploaded_media():
    c = FakeClient()
    assert run(media_upload.reply_with_media(c, _event(), [])) == 0
    assert run(media_upload.reply_with_media(c, _event(), None)) == 0
    assert run(media_upload.reply_with_media(c, _event(), _sent(2, with_media=False))) == 0
    assert c.sends == []
