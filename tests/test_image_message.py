from types import SimpleNamespace

from common.filters import is_image_message, is_media_message


def _msg(**kw):
    """构造一个带默认属性的假消息，缺省字段一律为 None。"""
    base = dict(media=None, gif=None, video_note=None, video=None,
                photo=None, sticker=None, document=None, file=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ---------- is_image_message ----------
def test_none_message():
    assert not is_image_message(None)


def test_no_media():
    assert not is_image_message(_msg(media=None, photo=object()))


def test_photo_message():
    assert is_image_message(_msg(media=object(), photo=object()))


def test_sticker_is_not_image():
    assert not is_image_message(_msg(media=object(), sticker=object(), photo=object()))


def test_gif_is_not_image():
    assert not is_image_message(_msg(media=object(), gif=object()))


def test_document_image_mime():
    doc = SimpleNamespace(mime_type="image/png")
    assert is_image_message(_msg(media=object(), document=doc))


def test_document_by_extension_when_mime_missing():
    doc = SimpleNamespace(mime_type=None)
    f = SimpleNamespace(name="pic.JPG")  # 大小写不敏感
    assert is_image_message(_msg(media=object(), document=doc, file=f))


def test_document_non_image():
    doc = SimpleNamespace(mime_type="application/pdf")
    f = SimpleNamespace(name="doc.pdf")
    assert not is_image_message(_msg(media=object(), document=doc, file=f))


def test_video_is_not_image():
    assert not is_image_message(_msg(media=object(), video=object()))


# ---------- is_media_message（图片或视频） ----------
def test_media_accepts_video():
    assert is_media_message(_msg(media=object(), video=object()))


def test_media_accepts_photo():
    assert is_media_message(_msg(media=object(), photo=object()))


def test_media_rejects_sticker():
    assert not is_media_message(_msg(media=object(), sticker=object()))


def test_media_rejects_gif():
    assert not is_media_message(_msg(media=object(), gif=object(), video=object()))


def test_media_rejects_plain_document():
    doc = SimpleNamespace(mime_type="application/pdf")
    f = SimpleNamespace(name="doc.pdf")
    assert not is_media_message(_msg(media=object(), document=doc, file=f))
