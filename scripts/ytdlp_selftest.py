"""yt-dlp 推特下载联调脚本（不依赖 Telegram，不需要登录态）。

用来在本机验证推特下载链路是否打通：
  解析链接 → yt-dlp 下载(合并视频/音频) → 拿到最终文件路径 → 大小检查/清理

前置：
  pip install -U yt-dlp
  本机装 ffmpeg（合并高清分离流需要）：macOS `brew install ffmpeg` / Debian `apt install ffmpeg`

用法：
  # 下载并在结束后清理（默认）
  python scripts/ytdlp_selftest.py "https://x.com/<user>/status/<id>"

  # 下载后保留本地文件 / 调整大小上限
  python scripts/ytdlp_selftest.py "https://x.com/<user>/status/<id>" --keep --max-gb 1

只支持公开推文（不接 cookies）。纯图文/需登录的推文会给出对应提示。
"""

import argparse
import asyncio
import os
import sys

# 让脚本能 import 项目根目录下的 downloader / common 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from downloader.ytdlp_client import Ytdlp, YtdlpError  # noqa: E402

# 用独立子目录，避免和正式 downloads 内容混在一起
SELFTEST_DIR = "./downloads/_selftest"


class ConsoleStatus:
    """假的状态消息对象：把 download 里的 edit() 输出打到控制台。"""

    async def edit(self, text):
        print("· " + text.replace("\n", "  "))


def _mb(n):
    return f"{int(n) / 1048576:.1f} MB"


async def run(args):
    ytdlp = Ytdlp(SELFTEST_DIR)
    max_bytes = int(args.max_gb * (1024 ** 3))
    status = ConsoleStatus()

    try:
        result = await ytdlp.download(args.url, status, max_bytes)
    except YtdlpError as e:
        print(f"\n❌ 下载链路报错：{e}")
        return

    print("\n下载结果：")
    if not result.downloaded_files:
        print("  未拿到任何视频文件。")
    for p in result.downloaded_files:
        exists = os.path.isfile(p)
        size = _mb(os.path.getsize(p)) if exists else "缺失"
        print(f"  • {'✅' if exists else '❌'} {os.path.basename(p)}  {size}")
    if result.skipped:
        print("  跳过（视频但超过上限）：")
        for name, sz, reason in result.skipped:
            print(f"    • {name}（{sz / 1073741824:.2f} GB，{reason}）")

    if args.keep:
        print("\n（--keep）保留本地文件，未清理")
    else:
        await ytdlp.cleanup(result)
        print("\n✅ 已清理本地文件")


def parse_args():
    p = argparse.ArgumentParser(description="yt-dlp 推特下载联调脚本（不依赖 Telegram）")
    p.add_argument("url", help="公开推特/X 推文链接")
    p.add_argument("--max-gb", type=float, default=4.0, help="单文件大小上限 GB（默认 4）")
    p.add_argument("--keep", action="store_true", help="下载后保留本地文件，不清理")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
