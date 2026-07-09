from types import SimpleNamespace

from filters import is_video_message


def _msg(**kw):
    """构造一个带默认属性的假消息，缺省字段一律为 None。"""
    base = dict(media=None, gif=None, video_note=None, video=None,
                document=None, file=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_none_message():
    assert not is_video_message(None)


def test_no_media():
    assert not is_video_message(_msg(media=None, video=object()))


def test_video_message():
    assert is_video_message(_msg(media=object(), video=object()))


def test_gif_is_not_video():
    # GIF 底层也是 mp4，但不算需要备份的视频文件
    assert not is_video_message(_msg(media=object(), gif=object(), video=object()))


def test_round_video_note_is_not_video():
    assert not is_video_message(_msg(media=object(), video_note=object()))


def test_document_video_mime():
    doc = SimpleNamespace(mime_type="video/x-matroska")
    assert is_video_message(_msg(media=object(), document=doc))


def test_document_by_extension_when_mime_missing():
    doc = SimpleNamespace(mime_type=None)
    f = SimpleNamespace(name="movie.mkv")
    assert is_video_message(_msg(media=object(), document=doc, file=f))


def test_document_non_video():
    doc = SimpleNamespace(mime_type="application/pdf")
    f = SimpleNamespace(name="doc.pdf")
    assert not is_video_message(_msg(media=object(), document=doc, file=f))


def test_photo_is_not_video():
    # 纯图片：有 media 但既非 video 也非 document
    assert not is_video_message(_msg(media=object()))
