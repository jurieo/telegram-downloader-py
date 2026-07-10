import os
import re
from dotenv import load_dotenv
from telethon import TelegramClient, events, utils
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from progress import ProgressBar
from aria2_client import Aria2Client
from filters import is_media_message
from routing import parse_target_chats, compute_targets, SentRegistry
import forwarder
import media_upload

# ================= 1. 读取配置 =================
load_dotenv()

try:
    API_ID = int(os.getenv("API_ID"))
    API_HASH = os.getenv("API_HASH")
except TypeError:
    print("❌ 启动失败：请检查 .env 文件（API_ID / API_HASH 必填）")
    exit(1)

# TARGET_CHAT：逗号分隔的多个备份目标 id，构成「互为备份」集合 S。
# 留空 = 仅下载模式（只监听收藏夹，只下载不转发）。
TARGET_CHATS = parse_target_chats(os.getenv("TARGET_CHAT"))

# 磁力下载相关配置（均有默认值，旧 .env 不改也能跑）
MAX_UPLOAD_CONCURRENCY = int(os.getenv("MAX_UPLOAD_CONCURRENCY", "4"))
MAX_FILE_SIZE_GB = float(os.getenv("MAX_FILE_SIZE_GB", "4"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")
TORRENT_STALL_TIMEOUT = int(os.getenv("TORRENT_STALL_TIMEOUT", "300"))

client = TelegramClient('my_transfer_session', API_ID, API_HASH)
# 适当抬高自动等待阈值：并发上传触发小额 FloodWait 时由 telethon 自行 sleep
client.flood_sleep_threshold = 120

# aria2 下载引擎（磁力链接用）；是否就绪由 ARIA2_READY 标记
aria2 = Aria2Client(DOWNLOAD_DIR, stall_timeout=TORRENT_STALL_TIMEOUT)
ARIA2_READY = False

# 磁力链接 / 消息链接识别
MAGNET_RE = r'magnet:\?xt=urn:btih:[^\s]+'
# 支持普通消息链接、帖子评论链接（?comment=xxx）、以及话题内消息链接
LINK_PATTERN = r'https?://t\.me/(?:c/(\d+)/|([\w_]+)/)(\d+)(?:/(\d+))?(\?\S*)?'

# ================= 运行时状态（在 _amain 里解析赋值） =================
ME_ID = None            # 本账号自身 id（收藏夹 me 的 chat_id）
TARGET_IDS = []         # 规范化后的备份目标 id 列表
registry = SentRegistry()   # 记录机器人自己发出的消息，防止互为备份时死循环


# ================= 2. 进度条工具类 =================
# ProgressBar / AggregateProgress 见 progress.py


# ================= 3. 磁力下载处理（仅收藏夹触发） =================
def _format_skipped(skipped):
    return "\n".join(
        f"  • {name}（{size / (1024 ** 3):.2f} GB，{reason}）"
        for name, size, reason in skipped
    )


async def handle_magnet(event, magnet):
    if not ARIA2_READY:
        await event.reply("❌ **磁力下载不可用**：aria2 引擎未就绪。")
        return

    status_msg = await event.reply("🧲 **收到磁力链接，正在准备下载…**")
    max_bytes = int(MAX_FILE_SIZE_GB * (1024 ** 3))
    result = None
    try:
        result = await aria2.download_magnet(magnet, status_msg, max_bytes)

        # 没有可上传的视频（全是非视频，或视频全部超限）
        if not result.downloaded_files:
            msg = "❌ **未找到可上传的视频文件**"
            if result.skipped:
                msg += "\n⏭️ 以下视频因过大被跳过：\n" + _format_skipped(result.skipped)
            await status_msg.edit(msg)
            await aria2.cleanup(result)
            return

        # ===== 模式 A：未配置备份目标，仅下载到本地（不上传、不删除） =====
        if not TARGET_IDS:
            paths = "\n".join(f"`{p}`" for p in result.downloaded_files)
            await status_msg.edit(
                f"🎉 **下载完成！** 共 {len(result.downloaded_files)} 个视频\n"
                f"✅ 已保存到服务器：\n{paths}"
            )
            return

        # ===== 模式 B：上传到首个目标，再服务器端扇出到其余目标 =====
        await status_msg.edit(
            f"✅ **下载完成，共 {len(result.downloaded_files)} 个视频，开始上传…**"
        )
        uploaded, failed, sent = await media_upload.upload_videos(
            client, TARGET_IDS[0], result.downloaded_files, status_msg,
            MAX_UPLOAD_CONCURRENCY, registry=registry,
        )

        # 从首个目标转给其余目标，零重复上传，drop_author 去引用
        for target in TARGET_IDS[1:]:
            for m in sent:
                try:
                    s2 = await client.forward_messages(target, m, drop_author=True)
                    registry.add_messages(s2)
                except Exception as e:
                    print(f"⚠️ 磁力扇出到 {target} 失败：{e}")

        summary = f"🎉 **完成！已上传 {uploaded} 个视频到 {len(TARGET_IDS)} 个目标**"
        if result.skipped:
            summary += "\n⏭️ 跳过：\n" + _format_skipped(result.skipped)
        if failed:
            summary += "\n⚠️ 上传失败：\n" + "\n".join(
                f"  • {name}（{reason}）" for name, reason in failed
            )
        await status_msg.edit(summary)

        await aria2.cleanup(result)
    except Exception as e:
        # 保证错误信息非空（某些异常如 TimeoutError 的 str() 为空），并打到 docker 日志
        detail = str(e).strip() or type(e).__name__
        print(f"⚠️ 磁力下载失败：{detail}")
        await status_msg.edit(f"❌ **磁力下载失败**：\n`{detail}`")
        await aria2.cleanup(result)


# ================= 4. 链接处理（仅收藏夹触发） =================
async def get_album(chat, message, max_amp=10):
    """从一条消息取出它所在的整个相册（媒体组）。

    链接只指向相册中的一条，按 grouped_id 在 ±max_amp 窗口内捞出同组消息。
    非相册消息直接返回单元素列表。
    """
    if getattr(message, "grouped_id", None) is None:
        return [message]
    ids = list(range(message.id - max_amp, message.id + max_amp + 1))
    around = await client.get_messages(chat, ids=ids)
    group = [
        m for m in around
        if m is not None and m.grouped_id == message.grouped_id and m.media is not None
    ]
    return group or [message]


async def handle_link(event):
    status_msg = await event.reply("🔄 **正在解析链接...**")

    match = re.search(LINK_PATTERN, event.raw_text)
    if not match:
        await status_msg.edit("❌ **未识别到有效链接**")
        return

    private_chat_id, public_username, first_id, second_id, query = match.groups()
    msg_id = int(second_id) if second_id else int(first_id)

    # 从查询串里提取评论 id（?comment=xxx，顺序无关）
    comment_id = None
    if query:
        cm = re.search(r'[?&]comment=(\d+)', query)
        if cm:
            comment_id = cm.group(1)

    try:
        chat_ref = int(f"-100{private_chat_id}") if private_chat_id else public_username

        if comment_id:
            # 评论区文件存放在频道关联的「讨论群」里，?comment=xxx 是讨论群内的消息 id。
            await status_msg.edit("🔄 **正在解析评论区...**")
            discussion = await client(GetDiscussionMessageRequest(peer=chat_ref, msg_id=msg_id))
            disc_group = next(
                (c for c in discussion.chats if getattr(c, 'megagroup', False)),
                None,
            )
            if disc_group is None:
                await status_msg.edit("❌ **解析失败**：该频道未开启评论（找不到关联讨论群）。")
                return
            search_chat = disc_group
            source_msg = await client.get_messages(disc_group, ids=int(comment_id))
        else:
            search_chat = chat_ref
            source_msg = await client.get_messages(chat_ref, ids=msg_id)

        if not source_msg or not source_msg.media:
            await status_msg.edit("❌ **解析失败**：找不到消息，或者该消息中没有可下载的媒体文件。")
            return

        # 若链接指向相册，取出整组一起处理（保留合并效果）
        msgs = await get_album(search_chat, source_msg)

        # ===== 模式 A：未配置备份目标，仅下载到本地 =====
        if not TARGET_IDS:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            saved = []
            for i, m in enumerate(msgs):
                title = "📥 **正在从源频道下载到服务器...**"
                if len(msgs) > 1:
                    title = f"📥 **正在下载第 {i + 1}/{len(msgs)} 个...**"
                p = await client.download_media(
                    m, file=DOWNLOAD_DIR + "/", progress_callback=ProgressBar(status_msg, title).callback
                )
                if p:
                    saved.append(p)
            paths = "\n".join(f"`{p}`" for p in saved)
            await status_msg.edit(f"🎉 **下载完成！**\n✅ 文件已保存到服务器：\n{paths}")
            return

        # ===== 模式 B：无引用镜像到全部备份目标 =====
        await forwarder.mirror(
            client, TARGET_IDS, msgs, registry,
            status_msg=status_msg, download_dir=DOWNLOAD_DIR,
        )

    except ValueError as ve:
        await status_msg.edit(f"❌ **权限错误**：无法访问源频道。\n`{ve}`")
    except Exception as e:
        await status_msg.edit(f"❌ **发生未知错误**：\n`{str(e)}`")


# ================= 5. 事件处理：路由 =================
async def on_new_message(event):
    """单条消息：收藏夹处理链接/磁力/图片视频；备份群只处理图片视频。相册交给 on_album。"""
    msg = event.message

    # 防回环：机器人自己发出/转发出去的消息直接跳过
    if registry.contains(event.chat_id, msg.id):
        return
    # 成组消息（相册）交给 on_album，避免重复处理
    if getattr(msg, "grouped_id", None) is not None:
        return

    if event.chat_id == ME_ID:
        text = msg.raw_text or ""
        magnet = re.search(MAGNET_RE, text)
        if magnet:
            await handle_magnet(event, magnet.group(0))
            return
        if re.search(LINK_PATTERN, text):
            await handle_link(event)
            return
        # 收藏夹里的普通图片/视频：无引用转发到全部备份目标
        if is_media_message(msg):
            await forwarder.mirror(
                client, compute_targets(event.chat_id, ME_ID, TARGET_IDS), [msg], registry
            )
        return

    # 备份群 A/B/C：只转图片/视频，静默无引用转发到其余成员
    if is_media_message(msg):
        await forwarder.mirror(
            client, compute_targets(event.chat_id, ME_ID, TARGET_IDS), [msg], registry
        )


async def on_album(event):
    """相册（合并视频/图文）：只要含图片或视频就整组无引用转发。"""
    if any(registry.contains(event.chat_id, m.id) for m in event.messages):
        return
    if not any(is_media_message(m) for m in event.messages):
        return
    targets = compute_targets(event.chat_id, ME_ID, TARGET_IDS)
    await forwarder.mirror(client, targets, list(event.messages), registry)


# ================= 6. 启动程序 =================
async def _amain():
    global ME_ID, TARGET_IDS

    await client.start()
    me = await client.get_me()
    ME_ID = me.id

    # 预热实体缓存，使「按 id 解析频道/群组」可用（否则新会话可能报找不到实体）
    try:
        await client.get_dialogs()
    except Exception as e:
        print(f"⚠️ 预取会话列表失败（可能影响按 id 解析目标）：{e}")

    # 规范化每个备份目标，并组成监听列表 = 收藏夹 + 全部目标
    monitor = ['me']
    for raw in TARGET_CHATS:
        try:
            ent = await client.get_entity(raw)
            TARGET_IDS.append(utils.get_peer_id(ent))
            monitor.append(ent)
        except Exception as e:
            print(f"⚠️ 无法访问备份目标 {raw}，已跳过：{e}")

    client.add_event_handler(on_album, events.Album(chats=monitor))
    client.add_event_handler(on_new_message, events.NewMessage(chats=monitor))

    if TARGET_IDS:
        print(f"🚀 UserBot 已启动：监听 收藏夹 + {len(TARGET_IDS)} 个备份目标，互为无引用转发视频")
    else:
        print("🚀 UserBot 已启动：仅下载模式（未配置 TARGET_CHAT），只监听收藏夹")

    await client.run_until_disconnected()


if __name__ == '__main__':
    print("🚀 UserBot 启动中…")
    try:
        aria2.start()
        ARIA2_READY = True
        print("✅ aria2 下载引擎已就绪（支持磁力链接）")
    except Exception as e:
        print(f"⚠️ aria2 启动失败，磁力下载不可用（t.me 链接仍可用）：{e}")

    try:
        client.loop.run_until_complete(_amain())
    finally:
        aria2.stop()
