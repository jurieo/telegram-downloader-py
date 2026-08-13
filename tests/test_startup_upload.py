import asyncio
import json
import os
from types import SimpleNamespace

from telethon.errors import FloodWaitError

from transfer import startup_upload
from transfer.routing import SentRegistry

GB = 1024 ** 3


def run(coro):
    return asyncio.run(coro)


def _touch(dirpath, name, size=10, mtime=None):
    path = os.path.join(dirpath, name)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class FakeClient:
    """记录发送/转发调用的假客户端，可注入按次数触发的异常。"""

    def __init__(self, send_errors=None):
        self.sent_files = []     # [(target, file, caption)]
        self.forwards = []       # [(target, messages)]
        self.messages = []       # send_message 文案
        self._send_errors = list(send_errors or [])
        self._id = 100

    async def send_message(self, target, text):
        self.messages.append(text)
        self._id += 1
        msg = SimpleNamespace(chat_id=target, id=self._id, edits=[])

        async def _edit(t):
            msg.edits.append(t)
        msg.edit = _edit
        return msg

    async def send_file(self, target, file, caption=None, **kw):
        if self._send_errors:
            err = self._send_errors.pop(0)
            if err is not None:
                raise err
        self.sent_files.append((target, file, caption))
        self._id += 1
        if isinstance(file, list):
            return [SimpleNamespace(chat_id=target, id=self._id + i)
                    for i in range(len(file))]
        return SimpleNamespace(chat_id=target, id=self._id)

    async def forward_messages(self, target, messages, drop_author=False):
        self.forwards.append((target, messages))
        self._id += 1
        return [SimpleNamespace(chat_id=target, id=self._id)]


# ================= 纯逻辑：扫描 / 分组 / 清单 =================

def test_scan_skips_hidden_temp_and_non_media(tmp_path):
    d = str(tmp_path)
    _touch(d, "b.mp4")
    _touch(d, "a.jpg")
    _touch(d, ".uploaded.json")
    _touch(d, "movie.mp4.part")
    _touch(d, "notes.txt")
    os.mkdir(os.path.join(d, "sub"))

    paths, ignored = startup_upload.scan_files(d)
    assert [os.path.basename(p) for p in paths] == ["a.jpg", "b.mp4"]  # 按名排序
    assert ignored == ["notes.txt"]


def test_scan_missing_dir_returns_empty(tmp_path):
    paths, ignored = startup_upload.scan_files(str(tmp_path / "nope"))
    assert paths == [] and ignored == []


def test_chunk_albums_max_10_per_group():
    paths = [f"{i}.mp4" for i in range(23)]
    groups = startup_upload.chunk_albums(paths)
    assert [len(g) for g in groups] == [10, 10, 3]
    assert startup_upload.chunk_albums([]) == []


def test_plan_skips_manifest_hits_and_oversize(tmp_path):
    d = str(tmp_path)
    done = _touch(d, "done.mp4", size=10, mtime=1000)
    _touch(d, "new.mp4", size=10)
    big = _touch(d, "big.mp4", size=200)
    manifest = {}
    startup_upload.record_uploaded(manifest, done)

    pending, skipped, already = startup_upload.plan_uploads(
        [done, os.path.join(d, "new.mp4"), big], manifest, max_bytes=100
    )
    assert [os.path.basename(p) for p in pending] == ["new.mp4"]
    assert skipped == [("big.mp4", 200, "超过大小上限")]
    assert already == 1


def test_modified_file_reuploads(tmp_path):
    """同名文件大小/mtime 变了 → 视为新文件，不算清单命中。"""
    d = str(tmp_path)
    p = _touch(d, "v.mp4", size=10, mtime=1000)
    manifest = {}
    startup_upload.record_uploaded(manifest, p)
    _touch(d, "v.mp4", size=20, mtime=2000)  # 内容更新

    pending, _, already = startup_upload.plan_uploads([p], manifest, max_bytes=GB)
    assert pending == [p] and already == 0


def test_manifest_roundtrip_and_corrupt(tmp_path):
    d = str(tmp_path)
    p = _touch(d, "v.mp4", mtime=1234)
    manifest = {}
    startup_upload.record_uploaded(manifest, p)
    startup_upload.save_manifest(d, manifest)
    assert startup_upload.load_manifest(d) == manifest

    with open(os.path.join(d, startup_upload.MANIFEST_NAME), "w") as f:
        f.write("{broken")
    assert startup_upload.load_manifest(d) == {}


# ================= 上传流程 =================

def test_uploads_as_albums_and_fans_out(tmp_path):
    d = str(tmp_path)
    for i in range(12):
        _touch(d, f"{i:02d}.mp4")
    c = FakeClient()
    reg = SentRegistry()

    run(startup_upload.process(c, [-100111, -100222], d, GB, reg,
                               delete_after_upload=True))

    # 12 个文件 → 2 组相册（10 + 2），只上传到首个目标
    assert len(c.sent_files) == 2
    assert all(t == -100111 for t, _, _ in c.sent_files)
    assert [len(f) for _, f, _ in c.sent_files] == [10, 2]
    # 每组扇出一次到第二个目标
    assert [t for t, _ in c.forwards] == [-100222, -100222]
    # 成功即删（DELETE_AFTER_UPLOAD=true）
    assert startup_upload.scan_files(d)[0] == []
    # 发出的消息已登记，防二次镜像
    assert len(reg._set) > 0


def test_manifest_prevents_reupload_when_keeping_files(tmp_path):
    d = str(tmp_path)
    _touch(d, "a.mp4")
    _touch(d, "b.jpg")

    c1 = FakeClient()
    run(startup_upload.process(c1, [-100111], d, GB, SentRegistry(),
                               delete_after_upload=False))
    assert len(c1.sent_files) == 1        # 一组相册
    assert startup_upload.scan_files(d)[0] != []   # 文件保留

    # 二次启动：清单全命中，不再发送
    c2 = FakeClient()
    run(startup_upload.process(c2, [-100111], d, GB, SentRegistry(),
                               delete_after_upload=False))
    assert c2.sent_files == []
    assert c2.messages == []              # 无待传时不打扰收藏夹


def test_album_failure_falls_back_to_single_files(tmp_path):
    d = str(tmp_path)
    for i in range(3):
        _touch(d, f"{i}.mp4")
    # 第一次（整组相册）失败，后续逐个上传时第 2 个再失败
    c = FakeClient(send_errors=[RuntimeError("album boom"), None,
                                RuntimeError("file boom"), None])
    run(startup_upload.process(c, [-100111], d, GB, SentRegistry(),
                               delete_after_upload=True))

    assert len(c.sent_files) == 2         # 3 个里成功 2 个
    remaining = [os.path.basename(p) for p in startup_upload.scan_files(d)[0]]
    assert remaining == ["1.mp4"]         # 失败的保留，下次启动重试


def test_floodwait_exhausted_aborts_and_keeps_files(tmp_path):
    d = str(tmp_path)
    _touch(d, "a.mp4")
    c = FakeClient(send_errors=[FloodWaitError(request=None, capture=77)] * 10)
    flood_seconds = run(startup_upload.process(
        c, [-100111], d, GB, SentRegistry(),
        delete_after_upload=True, flood_retries=0,
    ))
    # 中止但不抛出，回传罚时供 run 决定续传时机；文件保留待续传
    assert flood_seconds == 77
    assert [os.path.basename(p) for p in startup_upload.scan_files(d)[0]] == ["a.mp4"]


def test_run_resumes_after_floodwait(tmp_path, monkeypatch):
    """限流中止后 run 延时自动续传：等待取 max(半小时, 罚时)，第二轮传完剩余文件。"""
    d = str(tmp_path)
    _touch(d, "a.mp4")
    _touch(d, "b.mp4")
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(startup_upload.asyncio, "sleep", fake_sleep)

    # 第一轮：整组相册失败 → 逐个上传，a 成功、b 撞限流中止；第二轮：b 成功
    c = FakeClient(send_errors=[RuntimeError("album boom"), None,
                                FloodWaitError(request=None, capture=60)])
    run(startup_upload.run(c, [-100111], d, GB, SentRegistry(),
                           delete_after_upload=True, flood_retries=0))

    assert sleeps == [startup_upload.RESUME_DELAY]   # 罚时 60s < 半小时 → 等半小时
    assert startup_upload.scan_files(d)[0] == []     # 两个文件最终都传完删掉


def test_run_penalty_longer_than_resume_delay(tmp_path, monkeypatch):
    """Telegram 罚时超过半小时 → 按罚时等，提前重试只会再撞限流。"""
    d = str(tmp_path)
    _touch(d, "a.mp4")
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(startup_upload.asyncio, "sleep", fake_sleep)

    penalty = startup_upload.RESUME_DELAY + 600
    c = FakeClient(send_errors=[FloodWaitError(request=None, capture=penalty), None])
    run(startup_upload.run(c, [-100111], d, GB, SentRegistry(),
                           delete_after_upload=True, flood_retries=0))
    assert sleeps == [penalty]
    assert startup_upload.scan_files(d)[0] == []


def test_run_gives_up_after_max_resumes(tmp_path, monkeypatch):
    """续传后仍被限流 → 放弃（不无限循环），文件保留到下次启动。"""
    d = str(tmp_path)
    _touch(d, "a.mp4")

    async def fake_sleep(seconds):
        pass
    monkeypatch.setattr(startup_upload.asyncio, "sleep", fake_sleep)

    c = FakeClient(send_errors=[FloodWaitError(request=None, capture=1)] * 10)
    run(startup_upload.run(c, [-100111], d, GB, SentRegistry(),
                           delete_after_upload=True, flood_retries=0, max_resumes=1))
    assert [os.path.basename(p) for p in startup_upload.scan_files(d)[0]] == ["a.mp4"]


def test_noop_without_targets(tmp_path):
    d = str(tmp_path)
    _touch(d, "a.mp4")
    c = FakeClient()
    run(startup_upload.process(c, [], d, GB, SentRegistry(),
                               delete_after_upload=True))
    assert c.sent_files == [] and c.messages == []


def test_creates_upload_dir(tmp_path):
    d = str(tmp_path / "uploads")
    run(startup_upload.process(FakeClient(), [-100111], d, GB, SentRegistry(),
                               delete_after_upload=True))
    assert os.path.isdir(d)
