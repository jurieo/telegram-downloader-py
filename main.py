import os
import re
import time
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError, ChatForwardsRestrictedError
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from progress import ProgressBar
from aria2_client import Aria2Client
import media_upload

# ================= 1. 读取配置 =================
load_dotenv()

try:
    API_ID = int(os.getenv("API_ID"))
    API_HASH = os.getenv("API_HASH")
except TypeError:
    print("❌ 启动失败：请检查 .env 文件（API_ID / API_HASH 必填）")
    exit(1)

# TARGET_CHAT 可选：留空则进入「仅下载」模式，不再转发/重传
_target = os.getenv("TARGET_CHAT")
TARGET_CHAT = int(_target) if _target else None

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

# 磁力链接识别
MAGNET_RE = r'magnet:\?xt=urn:btih:[^\s]+'

# ================= 2. 进度条工具类 =================
# ProgressBar / AggregateProgress 见 progress.py


# ================= 3. 磁力下载处理 =================
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

        # ===== 模式 A：未配置 TARGET_CHAT，仅下载到本地（不上传、不删除） =====
        if TARGET_CHAT is None:
            paths = "\n".join(f"`{p}`" for p in result.downloaded_files)
            await status_msg.edit(
                f"🎉 **下载完成！** 共 {len(result.downloaded_files)} 个视频\n"
                f"✅ 已保存到服务器：\n{paths}"
            )
            return

        # ===== 模式 B：已配置 TARGET_CHAT，并发上传后清理 =====
        await status_msg.edit(
            f"✅ **下载完成，共 {len(result.downloaded_files)} 个视频，开始上传…**"
        )
        uploaded, failed = await media_upload.upload_videos(
            client, TARGET_CHAT, result.downloaded_files, status_msg, MAX_UPLOAD_CONCURRENCY
        )

        summary = f"🎉 **完成！已上传 {uploaded} 个视频**"
        if result.skipped:
            summary += "\n⏭️ 跳过：\n" + _format_skipped(result.skipped)
        if failed:
            summary += "\n⚠️ 上传失败：\n" + "\n".join(
                f"  • {name}（{reason}）" for name, reason in failed
            )
        await status_msg.edit(summary)

        await aria2.cleanup(result)
    except Exception as e:
        await status_msg.edit(f"❌ **磁力下载失败**：\n`{e}`")
        await aria2.cleanup(result)


# ================= 4. 核心处理逻辑 =================
@client.on(events.NewMessage(chats='me'))
async def my_event_handler(event):
    text = event.raw_text

    # 优先识别磁力链接，走磁力下载流程
    magnet_match = re.search(MAGNET_RE, text)
    if magnet_match:
        await handle_magnet(event, magnet_match.group(0))
        return

    # 支持普通消息链接、帖子评论链接（?comment=xxx）、以及话题内消息链接
    # 话题链接格式：t.me/username/topic_id/msg_id 或 t.me/c/chat_id/topic_id/msg_id
    link_pattern = r'https?://t\.me/(?:c/(\d+)/|([\w_]+)/)(\d+)(?:/(\d+))?(\?\S*)?'
    match = re.search(link_pattern, text)

    if not match:
        return

    # 发送一条占位消息，后面所有的进度都在这条消息上动态修改
    status_msg = await event.reply("🔄 **正在解析链接...**")

    private_chat_id, public_username, first_id, second_id, query = match.groups()
    if second_id:
        msg_id = int(second_id)
    else:
        msg_id = int(first_id)

    # 从查询串里提取评论 id（?comment=xxx，顺序无关）
    comment_id = None
    if query:
        cm = re.search(r'[?&]comment=(\d+)', query)
        if cm:
            comment_id = cm.group(1)

    try:
        if private_chat_id:
            chat_id = int(f"-100{private_chat_id}")
        else:
            chat_id = public_username

        if comment_id:
            # 评论区文件：评论实际存放在频道关联的「讨论群」里，而非频道本身。
            # ?comment=xxx 中的 xxx 是「讨论群内」的消息 id。
            # 步骤：用帖子定位讨论串 → 从返回的 chats 里取讨论群(megagroup)
            #       → 用 comment_id 到讨论群里取那条评论。
            await status_msg.edit("🔄 **正在解析评论区...**")
            discussion = await client(GetDiscussionMessageRequest(peer=chat_id, msg_id=msg_id))
            # 关联讨论群在 chats 里，且带完整 access_hash，实体解析才可靠
            disc_group = next(
                (c for c in discussion.chats if getattr(c, 'megagroup', False)),
                None
            )
            if disc_group is None:
                await status_msg.edit("❌ **解析失败**：该频道未开启评论（找不到关联讨论群）。")
                return
            source_msg = await client.get_messages(disc_group, ids=int(comment_id))
        else:
            source_msg = await client.get_messages(chat_id, ids=msg_id)

        if not source_msg or not source_msg.media:
            await status_msg.edit("❌ **解析失败**：找不到消息，或者该消息中没有可下载的媒体文件。")
            return

        # 受保护内容时使用的回退方案：下载到本地 → 重新上传
        async def download_to_local():
            download_dir = './downloads'
            if not os.path.exists(download_dir):
                os.makedirs(download_dir)

            # 下载，绑定下载进度条
            dl_progress = ProgressBar(status_msg, "📥 **正在从源频道下载到服务器...**")
            return await client.download_media(
                source_msg,
                file=download_dir + '/',
                progress_callback=dl_progress.callback
            )

        # ===== 模式 A：未配置 TARGET_CHAT，仅下载到本地 =====
        if TARGET_CHAT is None:
            file_path = await download_to_local()
            await status_msg.edit(
                "🎉 **下载完成！**\n"
                f"✅ 文件已保存到服务器：\n`{file_path}`"
            )
            return

        # ===== 模式 B：已配置 TARGET_CHAT，转发 / 下载重传 =====
        async def download_and_reupload():
            file_path = await download_to_local()

            await status_msg.edit("✅ **下载到服务器完成！**\n🚀 准备开始上传...")
            time.sleep(1)  # 停顿1秒，让用户看清楚过渡状态

            # 复用原始媒体的属性（视频宽高/时长/可流式播放、文件名、mime 类型）。
            # 否则 Telethon 重传时无法识别视频元数据，会导致：
            #   1) 视频变形为正方形（缺宽高）  2) 必须下完才能播放（缺 supports_streaming）
            send_kwargs = {}
            doc = getattr(source_msg, 'document', None)
            if doc:
                send_kwargs['attributes'] = doc.attributes
                send_kwargs['mime_type'] = doc.mime_type

            # 上传，绑定上传进度条
            ul_progress = ProgressBar(status_msg, "📤 **正在将文件上传到目标频道...**")
            await client.send_file(
                TARGET_CHAT,
                file_path,
                caption=source_msg.text if source_msg.text else "",
                progress_callback=ul_progress.callback,
                **send_kwargs
            )

            # 清理并发送最终成功提示
            os.remove(file_path)
            await status_msg.edit(
                "🎉 转发成功"
            )

        # 步骤 0：优先尝试直接转发（零下载、零本地占用）
        # noforwards 为 False 表示源频道未开启"禁止转发/限制保存"
        if not source_msg.noforwards:
            try:
                await status_msg.edit("🚀 **该消息允许转发，正在直接转发...**")
                await client.forward_messages(
                    TARGET_CHAT,
                    source_msg,
                    drop_author=True  # 无引用转发：不显示"转发自 xxx"
                )
                await status_msg.edit(
                    "🎉 **转发任务完成！**\n"
                    "✅ 已直接无引用转发，无需下载上传"
                )
                return
            except ChatForwardsRestrictedError:
                # 兜底：noforwards 漏判或源频道临时限制，回退到下载重传
                await status_msg.edit("⚠️ **直接转发被限制，改用下载重传方案...**")

        # 受保护内容（noforwards=True 或转发被拒）：走下载 → 重新上传
        await download_and_reupload()

    except ValueError as ve:
        await status_msg.edit(f"❌ **权限错误**：无法访问源频道。\n`{ve}`")
    except Exception as e:
        await status_msg.edit(f"❌ **发生未知错误**：\n`{str(e)}`")

# ================= 5. 启动程序 =================
if __name__ == '__main__':
    print("🚀 UserBot 启动中…")
    try:
        aria2.start()
        ARIA2_READY = True
        print("✅ aria2 下载引擎已就绪（支持磁力链接）")
    except Exception as e:
        print(f"⚠️ aria2 启动失败，磁力下载不可用（t.me 链接仍可用）：{e}")

    try:
        client.start()
        print("🚀 UserBot 已启动并正在监听收藏夹...")
        client.run_until_disconnected()
    finally:
        aria2.stop()
