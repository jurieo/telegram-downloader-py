"""PROXY_URL 解析测试。

这段解析是启动路径上的代码，格式错了要退化成直连而不是让进程起不来，
所以「坏输入返回 None」和「好输入解析对」同样重要。
"""
import sys

import pytest

from common.proxy import parse_proxy, redact


def test_empty_means_direct():
    assert parse_proxy("") is None
    assert parse_proxy(None) is None
    assert parse_proxy("   ") is None


def test_socks5_without_credentials():
    python_socks = pytest.importorskip("python_socks")
    proto, host, port, rdns, user, passwd = parse_proxy("socks5://127.0.0.1:1080")
    assert proto == python_socks.ProxyType.SOCKS5
    assert (host, port) == ("127.0.0.1", 1080)
    # rdns=True 让域名由代理端解析，绕开本地 DNS 污染
    assert rdns is True
    assert user is None and passwd is None


def test_socks5_with_credentials():
    pytest.importorskip("python_socks")
    _, host, port, _, user, passwd = parse_proxy("socks5://alice:s3cret@proxy.lan:9050")
    assert (host, port) == ("proxy.lan", 9050)
    assert (user, passwd) == ("alice", "s3cret")


def test_password_may_contain_colon_and_at():
    """密码里带 : 和 @ 很常见，不能把它们当分隔符切错。"""
    pytest.importorskip("python_socks")
    _, host, port, _, user, passwd = parse_proxy("socks5://bob:p@ss:w0rd@10.0.0.5:1080")
    assert (host, port) == ("10.0.0.5", 1080)
    assert user == "bob"
    assert passwd == "p@ss:w0rd"


def test_http_proxy():
    proto, host, port, rdns, user, passwd = parse_proxy("http://gw.internal:3128")
    assert proto == "http"
    assert (host, port) == ("gw.internal", 3128)
    assert rdns is True
    assert user is None and passwd is None


@pytest.mark.parametrize("bad", [
    "127.0.0.1:1080",              # 缺协议
    "socks5://127.0.0.1",          # 缺端口
    "ftp://127.0.0.1:21",          # 不支持的协议
    "socks4://127.0.0.1:1080",     # 不支持的协议
    "socks5://127.0.0.1:abc",      # 端口非数字
    "socks5://:1080",              # 缺主机
])
def test_malformed_falls_back_to_direct(bad, capsys):
    assert parse_proxy(bad) is None
    # 静默直连会让人以为代理生效了，必须有警告
    assert "PROXY_URL" in capsys.readouterr().out


def test_port_out_of_range(capsys):
    assert parse_proxy("http://host:70000") is None
    assert "端口" in capsys.readouterr().out


def test_socks5_without_dependency_falls_back(monkeypatch, capsys):
    """python-socks 没装时应直连并提示，而不是抛 ImportError 崩在启动阶段。"""
    monkeypatch.setitem(sys.modules, "python_socks", None)
    monkeypatch.delitem(sys.modules, "python_socks")

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "python_socks":
            raise ImportError("no module named python_socks")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert parse_proxy("socks5://127.0.0.1:1080") is None
    assert "python-socks" in capsys.readouterr().out


def test_redact_strips_credentials():
    assert redact("socks5://alice:s3cret@proxy.lan:9050") == "proxy.lan:9050"
    # 无凭据时原样返回，日志里仍能看出连的是哪台
    assert redact("http://gw.internal:3128") == "http://gw.internal:3128"
    assert redact("") == ""
