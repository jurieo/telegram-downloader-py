import os
import re
import time
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError, ChatForwardsRestrictedError
from telethon.tl.functions.messages import GetDiscussionMessageRequest

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

client = TelegramClient('my_transfer_session', API_ID, API_HASH)

# ================= 2. 进度条工具类 =================
class ProgressBar:
    def __init__(self, message_obj, action_title):
        self.message_obj = message_obj
        self.action_title = action_title
        self.last_update_time = 0

    async def callback(self, current_bytes, total_bytes):
        # 如果获取不到总大小，直接跳过计算
        if total_bytes == 0:
            return

        now = time.time()
        # 核心限流：限制每 2 秒最多更新一次界面（防止被 Telegram 官方限制）
        # 如果进度到达 100% (current_bytes == total_bytes)，强制更新一次
        if now - self.last_update_time < 2 and current_bytes != total_bytes:
            return

        self.last_update_time = now

        # 计算百分比和容量 (转换为 MB)
        percent = (current_bytes / total_bytes) * 100
        current_mb = current_bytes / (1024 * 1024)
        total_mb = total_bytes / (1024 * 1024)

        # 绘制字符进度条 [██████░░░░]
        bar_length = 15
        filled_length = int(bar_length * current_bytes // total_bytes)
        bar = '█' * filled_length + '░' * (bar_length - filled_length)

        # 组装展示文本
        text = (
            f"{self.action_title}\n"
            f"[{bar}] {percent:.1f}%\n"
            f"📦 进度: {current_mb:.2f} MB / {total_mb:.2f} MB"
        )

        try:
            # 动态编辑那条状态消息
            await self.message_obj.edit(text)
        except MessageNotModifiedError:
            # 如果文字没变化，Telegram 会报错，直接忽略即可
            pass
        except Exception as e:
            print(f"进度条更新异常: {e}")

# ================= 3. 核心处理逻辑 =================
@client.on(events.NewMessage(chats='me'))
async def my_event_handler(event):
    text = event.raw_text
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

# ================= 4. 启动程序 =================
if __name__ == '__main__':
    print(f"🚀 UserBot 已启动并正在监听收藏夹...")
    client.start()
    client.run_until_disconnected()