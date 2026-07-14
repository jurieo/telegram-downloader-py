from types import SimpleNamespace

from routing import parse_target_chats, compute_targets, SentRegistry


# ---------- parse_target_chats ----------
def test_parse_single():
    assert parse_target_chats("-1002130548389") == [-1002130548389]


def test_parse_multiple_comma():
    assert parse_target_chats("-100111,-100222,-100333") == [-100111, -100222, -100333]


def test_parse_strips_spaces_and_blanks():
    assert parse_target_chats(" -100111 , , -100222 ,") == [-100111, -100222]


def test_parse_dedupes_keeping_order():
    assert parse_target_chats("-100111,-100222,-100111") == [-100111, -100222]


def test_parse_skips_non_numeric():
    assert parse_target_chats("-100111,abc,-100222") == [-100111, -100222]


def test_parse_empty_is_download_only():
    assert parse_target_chats("") == []
    assert parse_target_chats(None) == []


# ---------- compute_targets ----------
ME = 42
A, B, C = -100111, -100222, -100333
S = [A, B, C]


def test_from_me_fans_out_to_all():
    assert compute_targets(ME, ME, S) == [A, B, C]


def test_from_member_excludes_self_and_me():
    assert compute_targets(A, ME, S) == [B, C]
    assert compute_targets(B, ME, S) == [A, C]
    assert compute_targets(C, ME, S) == [A, B]


def test_me_never_a_target_even_if_configured():
    # 即便 me 误配进 S，也不会转回 me
    assert compute_targets(A, ME, [ME, A, B]) == [B]


def test_from_outside_source_fans_out_to_all():
    # 单向源（SOURCE_CHAT，如下载机器人）：来源不在集合里 → 转发到集合全部成员。
    # 这是 SOURCE_CHAT 功能依赖的核心语义，钉死防止改坏。
    BOT = 987654321  # 机器人对话是正数用户 id
    assert compute_targets(BOT, ME, S) == [A, B, C]


def test_empty_targets():
    assert compute_targets(ME, ME, []) == []


# ---------- SentRegistry ----------
def test_registry_add_and_contains():
    r = SentRegistry()
    r.add(-100111, 5)
    assert r.contains(-100111, 5)
    assert not r.contains(-100111, 6)


def test_registry_add_messages_single_and_list():
    r = SentRegistry()
    r.add_messages(SimpleNamespace(chat_id=-100111, id=7))
    r.add_messages([SimpleNamespace(chat_id=-100222, id=8),
                    SimpleNamespace(chat_id=-100333, id=9)])
    assert r.contains(-100111, 7)
    assert r.contains(-100222, 8)
    assert r.contains(-100333, 9)


def test_registry_ignores_none():
    r = SentRegistry()
    r.add_messages(None)
    r.add_messages([None])
    r.add(None, 1)
    r.add(-100111, None)
    assert not r.contains(-100111, None)


def test_registry_bounded_evicts_oldest():
    r = SentRegistry(maxlen=2)
    r.add(1, 1)
    r.add(1, 2)
    r.add(1, 3)          # 淘汰 (1,1)
    assert not r.contains(1, 1)
    assert r.contains(1, 2)
    assert r.contains(1, 3)
