"""列出当前账号的所有对话（群/频道/私聊）及其 chat id。

用途：拿到要填进 .env 的 TARGET_CHAT（频道/群通常是 -100 开头的负数）。

用法（在项目根目录运行；⚠️ 先停掉正在运行的 main.py，
      因为 telethon 的 .session 是 sqlite，同一文件不能被两个进程同时打开）：

    python scripts/list_dialogs.py            # 列出全部
    python scripts/list_dialogs.py 备份         # 只显示标题含「备份」的对话

复用 main.py 生成的登录态（my_transfer_session.session），无需重新登录。
"""
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient

# 定位到项目根目录（本脚本在 scripts/ 下），保证无论从哪运行都能找到 .env 和 session
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

try:
    API_ID = int(os.getenv("API_ID"))
    API_HASH = os.getenv("API_HASH")
except (TypeError, ValueError):
    print("❌ 请先在 .env 里填好 API_ID / API_HASH")
    sys.exit(1)

SESSION = os.path.join(ROOT, "my_transfer_session")


def _kind(entity):
    """把实体归类成好认的中文标签。"""
    if getattr(entity, "broadcast", False):
        return "📢 频道"
    if getattr(entity, "megagroup", False):
        return "👥 超级群"
    if entity.__class__.__name__ == "Chat":
        return "👥 普通群"
    if getattr(entity, "bot", False):
        return "🤖 机器人"
    return "👤 私聊"


async def main(client):
    keyword = sys.argv[1] if len(sys.argv) > 1 else None

    rows = []
    async for dialog in client.iter_dialogs():
        name = dialog.name or "(无标题)"
        if keyword and keyword not in name:
            continue
        # dialog.id 已是可直接填入 TARGET_CHAT 的「带标记 id」（频道/群为 -100...）
        rows.append((dialog.id, _kind(dialog.entity), name))

    if not rows:
        print("没有匹配的对话。" if keyword else "没有找到任何对话。")
        return

    # 频道/超级群排前面，方便挑备份目标
    rows.sort(key=lambda r: (0 if r[0] < 0 else 1, r[0]))

    width = max(len(str(r[0])) for r in rows)
    print(f"\n共 {len(rows)} 个对话：\n")
    print(f"{'chat_id'.ljust(width)}  类型      名称")
    print(f"{'-' * width}  --------  {'-' * 30}")
    for cid, kind, name in rows:
        print(f"{str(cid).ljust(width)}  {kind}  {name}")
    print("\n💡 把想互为备份的频道/群 id 用英文逗号拼进 .env 的 TARGET_CHAT，例如：")
    print("   TARGET_CHAT=-1001111111111,-1002222222222,-1003333333333\n")


if __name__ == "__main__":
    # 只开一个 client：with 进入时同步登录（复用 session），退出时自动断开
    with TelegramClient(SESSION, API_ID, API_HASH) as client:
        client.loop.run_until_complete(main(client))
