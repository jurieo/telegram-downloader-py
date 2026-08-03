"""PROXY_URL 解析：把环境变量里的代理地址转成 telethon 认的 proxy 参数。

支持 socks5:// 和 http://，可带用户名密码。留空 = 直连。
放在独立模块而不是 main.py 里，是为了能不建 TelegramClient 就测这段解析。
"""
import re

# socks5://[user:pass@]host:port | http://[user:pass@]host:port
# 密码里可能有 @ 和 :，所以 host 段限定为「非 : 非 @」并锚定末尾的 :port，
# 让贪婪的 (.+) 把中间的特殊字符都吃进密码里。
_PROXY_RE = re.compile(
    r'^(socks5|http)://(?:([^:@]+):(.+)@)?([^:@]+):(\d+)$'
)


def parse_proxy(url):
    """解析代理地址。

    返回 telethon 可直接传给 proxy= 的元组；留空、格式错误、
    或 SOCKS5 缺依赖时返回 None（调用方直连）。
    """
    if not url or not url.strip():
        return None

    m = _PROXY_RE.match(url.strip())
    if not m:
        print(f"⚠️ PROXY_URL 格式错误，已忽略（应形如 socks5://host:1080）：{url}")
        return None

    proto, user, passwd, host, port = m.groups()
    port = int(port)
    if not 0 < port < 65536:
        print(f"⚠️ PROXY_URL 端口非法，已忽略：{port}")
        return None

    if proto == 'socks5':
        try:
            import python_socks
        except ImportError:
            print("⚠️ 需要 SOCKS5 代理但 python-socks 未安装，将直连"
                  "（pip install python-socks 后重启生效）")
            return None
        # rdns=True：域名交给代理端解析，避免本地 DNS 污染
        return (python_socks.ProxyType.SOCKS5, host, port, True,
                user or None, passwd or None)

    return ('http', host, port, True, user or None, passwd or None)


def redact(url):
    """去掉凭据后的代理地址，可安全打日志。"""
    if not url:
        return ""
    return url.split('@')[-1] if '@' in url else url
