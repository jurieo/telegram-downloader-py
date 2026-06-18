"""aria2 选择性下载联调脚本（不依赖 Telegram，不需要登录态）。

用来在本机验证磁力下载链路是否打通：
  守护进程启动 → addUri → 抓元数据 → getFiles → plan_files(只挑视频) →
  select-file → unpause → 进度轮询 → complete → 清理

前置：本机已安装 aria2（macOS: `brew install aria2`；Debian: `apt install aria2`）。

用法：
  # 默认只验证到“拿到文件清单 + 选片决策”（快，几乎不下载数据）
  python scripts/aria2_selftest.py

  # 实际下载选中的视频文件，跑完整链路（默认 Sintel 约 129MB 视频）
  python scripts/aria2_selftest.py --download

  # 指定自己的磁力链接 / 调整大小上限 / 下载后保留文件
  python scripts/aria2_selftest.py "magnet:?xt=urn:btih:..." --download --max-gb 1 --keep

默认测试种子为 WebTorrent 的 Sintel 测试种子：包含 Sintel.mp4（视频）+ 字幕/海报（非视频），
正好能验证“只选视频、忽略其它”的逻辑。
"""

import argparse
import asyncio
import os
import sys

# 让脚本能 import 项目根目录下的 aria2_client / filters
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria2_client import Aria2Client, Aria2Error  # noqa: E402
from filters import plan_files                     # noqa: E402

# WebTorrent Sintel 测试种子（含视频 + 字幕 + 海报，长期有做种）
DEFAULT_MAGNET = (
    "magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10"
    "&dn=Sintel"
    "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
    "&tr=udp%3A%2F%2Fexplodie.org%3A6969"
    "&tr=udp%3A%2F%2Ftracker.openbittorrent.com%3A6969%2Fannounce"
)

# 用独立子目录，避免和正式 downloads 内容混在一起
SELFTEST_DIR = "./downloads/_selftest"


class ConsoleStatus:
    """假的状态消息对象：把 download_magnet 里的 edit() 输出打到控制台。"""

    async def edit(self, text):
        print("· " + text.replace("\n", "  "))


def _mb(n):
    return f"{int(n) / 1048576:.1f} MB"


async def inspect_only(aria2, magnet, max_bytes):
    """只验证到“文件清单 + 选片决策”，不真正下载数据。"""
    gid = await aria2._rpc("aria2.addUri", [magnet])
    print("已添加磁力，等待元数据（DHT/Tracker）…")
    file_gid = await aria2._wait_metadata(gid)
    files = await aria2._rpc("aria2.getFiles", file_gid)

    print(f"\n种子共 {len(files)} 个文件：")
    for f in files:
        print(f"  [{f['index']}] {os.path.basename(f['path'])}  {_mb(f['length'])}")

    indices, paths, skipped = plan_files(files, max_bytes)
    print("\n✅ 选中下载（视频且未超限）：")
    for p in paths:
        print(f"  • {os.path.basename(p)}")
    if skipped:
        print("⏭️ 跳过（视频但超过上限）：")
        for name, size, reason in skipped:
            print(f"  • {name}（{size / 1073741824:.2f} GB，{reason}）")
    if not paths and not skipped:
        print("  （没有视频文件——非视频会被静默忽略）")

    # 清理这次的下载记录（没真正下数据）
    await aria2._safe_remove(file_gid)
    print("\n✅ 元数据/选片链路验证完成（未下载文件数据）")


async def full_download(aria2, magnet, max_bytes, keep):
    """跑真实的 download_magnet：下载选中的视频文件。"""
    status = ConsoleStatus()
    result = await aria2.download_magnet(magnet, status, max_bytes)

    print("\n下载结果：")
    if not result.downloaded_files:
        print("  未选中任何视频文件。")
    for p in result.downloaded_files:
        exists = os.path.isfile(p)
        size = _mb(os.path.getsize(p)) if exists else "缺失"
        print(f"  • {'✅' if exists else '❌'} {os.path.basename(p)}  {size}")
    if result.skipped:
        print("  跳过：")
        for name, sz, reason in result.skipped:
            print(f"    • {name}（{sz / 1073741824:.2f} GB，{reason}）")

    if keep:
        print("\n（--keep）保留本地文件，未清理")
    else:
        await aria2.cleanup(result)
        print("\n✅ 已清理本地文件")


def parse_args():
    p = argparse.ArgumentParser(description="aria2 选择性下载联调脚本（不依赖 Telegram）")
    p.add_argument("magnet", nargs="?", default=DEFAULT_MAGNET, help="磁力链接（默认 Sintel 测试种子）")
    p.add_argument("--download", action="store_true", help="实际下载选中文件（默认只验证到文件清单）")
    p.add_argument("--max-gb", type=float, default=4.0, help="单文件大小上限 GB（默认 4）")
    p.add_argument("--keep", action="store_true", help="下载后保留本地文件，不清理")
    return p.parse_args()


async def run(args):
    aria2 = Aria2Client(SELFTEST_DIR, stall_timeout=120)
    try:
        aria2.start()
    except FileNotFoundError:
        print("❌ 找不到 aria2c，请先安装：macOS `brew install aria2` / Debian `apt install aria2`")
        return
    except Aria2Error as e:
        print(f"❌ aria2 启动失败：{e}")
        return
    print("✅ aria2 守护进程已启动")

    try:
        max_bytes = int(args.max_gb * (1024 ** 3))
        if args.download:
            await full_download(aria2, args.magnet, max_bytes, args.keep)
        else:
            await inspect_only(aria2, args.magnet, max_bytes)
    except Aria2Error as e:
        print(f"\n❌ 下载链路报错：{e}")
    finally:
        aria2.stop()
        print("✅ aria2 守护进程已停止")


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
