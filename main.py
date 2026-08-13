import asyncio
import os
import re
from dotenv import load_dotenv

# 必须在导入本项目模块之前加载 .env：transfer.forwarder 等模块在 import 时就用
# os.getenv 求值模块级配置常量，晚于此处加载会让 .env 里的值被默认值覆盖。
load_dotenv()

from telethon import TelegramClient, events, utils  # noqa: E402
from telethon.errors import MessageNotModifiedError  # noqa: E402
from telethon.tl.functions.messages import GetDiscussionMessageRequest  # noqa: E402

from common.filters import is_media_message  # noqa: E402
from common.proxy import parse_proxy, redact  # noqa: E402
from downloader.aria2_client import Aria2Client  # noqa: E402
from downloader import ytdlp_client  # noqa: E402
from transfer.routing import parse_target_chats, compute_targets, SentRegistry  # noqa: E402
from transfer import forwarder  # noqa: E402
from transfer import media_upload  # noqa: E402
from transfer import media_download  # noqa: E402
from transfer import startup_upload  # noqa: E402

# ================= 1. 读取配置 =================
try:
    API_ID = int(os.getenv("API_ID"))
    API_HASH = os.getenv("API_HASH")
except TypeError:
    print("❌ 启动失败：请检查 .env 文件（API_ID / API_HASH 必填）")
    exit(1)

# TARGET_CHAT：逗号分隔的多个备份目标 id，构成「互为备份」集合 S。
# 留空 = 仅下载模式（只监听收藏夹，只下载不转发）。
TARGET_CHATS = parse_target_chats(os.getenv("TARGET_CHAT"))

# SOURCE_CHAT：逗号分隔的单向监听源（机器人/频道/群）。
# 源里出现的图片/视频 → 无引用转发到 TARGET_CHAT 全部成员；源永不作为转发目标。
# 典型用法：把下载机器人加进来，它回给你的视频自动镜像到备份组。
SOURCE_CHATS = parse_target_chats(os.getenv("SOURCE_CHAT"))

# 磁力下载相关配置（均有默认值，旧 .env 不改也能跑）
MAX_UPLOAD_CONCURRENCY = int(os.getenv("MAX_UPLOAD_CONCURRENCY", "4"))
MAX_FILE_SIZE_GB = float(os.getenv("MAX_FILE_SIZE_GB", "4"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")
# 启动扫描上传目录：放进来的图片/视频在服务启动时按相册组上传到 TARGET_CHAT
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
# 相册/多文件从源下载时的并发数（默认 4；设 1 退回串行）
MAX_DOWNLOAD_CONCURRENCY = int(os.getenv("MAX_DOWNLOAD_CONCURRENCY", "4"))
# 单个大文件内部的分块连接数（默认 3；设 1 关闭分块，退回单连接下载）
DOWNLOAD_CONNECTIONS = int(os.getenv("DOWNLOAD_CONNECTIONS", "3"))
TORRENT_STALL_TIMEOUT = int(os.getenv("TORRENT_STALL_TIMEOUT", "300"))

# 转发/上传成功后是否删除本地下载的文件（默认 true 删除；设 false/0/no/off 保留）。
# 只影响「成功上传后」的清理；下载失败或无可上传文件时的残留垃圾仍会清掉。
DELETE_AFTER_UPLOAD = os.getenv("DELETE_AFTER_UPLOAD", "true").strip().lower() not in (
    "0", "false", "no", "off"
)

# 代理（可选）：socks5://[user:pass@]host:port 或 http://...；留空直连。
PROXY_URL = os.getenv("PROXY_URL", "")

_proxy = parse_proxy(PROXY_URL)
if _proxy:
    print(f"🌐 使用代理：{redact(PROXY_URL)}")
client = TelegramClient('my_transfer_session', API_ID, API_HASH, proxy=_proxy)
# 适当抬高自动等待阈值：并发上传触发小额 FloodWait 时由 telethon 自行 sleep
client.flood_sleep_threshold = 120

# aria2 下载引擎（磁力链接用）；是否就绪由 ARIA2_READY 标记
aria2 = Aria2Client(DOWNLOAD_DIR, stall_timeout=TORRENT_STALL_TIMEOUT)
ARIA2_READY = False

# yt-dlp 下载引擎（推特/X 链接用）；纯 Python 库，无守护进程，无需 start/stop
ytdlp = ytdlp_client.Ytdlp(DOWNLOAD_DIR)

# 磁力链接 / 消息链接识别
MAGNET_RE = r'magnet:\?xt=urn:btih:[^\s]+'
# 支持普通消息链接、帖子评论链接（?comment=xxx）、以及话题内消息链接
LINK_PATTERN = r'https?://t\.me/(?:c/(\d+)/|([\w_]+)/)(\d+)(?:/(\d+))?(\?\S*)?'
# 推特/X 推文链接（限定 /status/<id>，避免误抓主页/个人页；仅公开推文）
TWITTER_RE = r'https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/[^\s/]+/status/\d+'

# ================= 运行时状态（在 _amain 里解析赋值） =================
ME_ID = None            # 本账号自身 id（收藏夹 me 的 chat_id）
TARGET_IDS = []         # 规范化后的备份目标 id 列表
SOURCE_IDS = []         # 规范化后的单向监听源 id 列表（只进不出）
registry = SentRegistry()   # 记录机器人自己发出的消息，防止互为备份时死循环


async def safe_status_edit(status_msg, text):
    """更新状态消息，失败不打断下载。

    并发进度回调很容易在两次渲染间算出同样的文案，Telegram 会拒绝这种
    「没变化」的编辑并抛错；下载本身没问题，不该因此中断。
    """
    try:
        await status_msg.edit(text)
    except MessageNotModifiedError:
        pass
    except Exception as e:
        print(f"⚠️ 状态消息更新失败：{e}")


# ================= 2. 进度条工具类 =================
# ProgressBar / AggregateProgress 见 progress.py


# ================= 3. 磁力下载处理（仅收藏夹触发） =================
def _format_skipped(skipped):
    return "\n".join(
        f"  • {name}（{size / (1024 ** 3):.2f} GB，{reason}）"
        for name, size, reason in skipped
    )


async def _finish_status(status_msg, replied, uploaded, target_count, skipped, failed):
    """收尾提示。

    视频已回复到链接下方时，「已上传 N 个」就是废话，删掉不占版面；
    但跳过/失败这类用户不看消息就不知道的信息必须留着。
    """
    extras = []
    if skipped:
        extras.append("⏭️ 跳过：\n" + _format_skipped(skipped))
    if failed:
        extras.append("⚠️ 上传失败：\n" + "\n".join(
            f"  • {name}（{reason}）" for name, reason in failed
        ))

    if replied and not extras:
        try:
            await status_msg.delete()
        except Exception as e:
            print(f"⚠️ 删除状态消息失败：{e}")
        return

    head = f"🎉 **完成！已上传 {uploaded} 个视频到 {target_count} 个目标**"
    await safe_status_edit(status_msg, "\n".join(extras if replied else [head] + extras))


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

        # 把视频回复到原磁力链接消息下面
        replied = 0
        try:
            replied = await media_upload.reply_with_media(client, event, sent, registry=registry)
        except Exception as e:
            print(f"⚠️ 磁力视频回复到链接失败：{e}")

        await _finish_status(
            status_msg, replied, uploaded, len(TARGET_IDS), result.skipped, failed
        )

        await aria2.cleanup(result, delete_files=DELETE_AFTER_UPLOAD)
    except Exception as e:
        # 保证错误信息非空（某些异常如 TimeoutError 的 str() 为空），并打到 docker 日志
        detail = str(e).strip() or type(e).__name__
        print(f"⚠️ 磁力下载失败：{detail}")
        await safe_status_edit(status_msg, f"❌ **磁力下载失败**：\n`{detail}`")
        await aria2.cleanup(result)


# ================= 3.5 推特/X 链接处理（仅收藏夹触发） =================
async def handle_twitter(event, url):
    status_msg = await event.reply("🔗 **收到推特链接，正在解析…**")
    max_bytes = int(MAX_FILE_SIZE_GB * (1024 ** 3))
    result = None
    try:
        result = await ytdlp.download(url, status_msg, max_bytes)

        # 没有可上传的视频（纯图文推文，或视频超限）
        if not result.downloaded_files:
            msg = "❌ **未找到可下载的视频**"
            if result.skipped:
                msg += "\n⏭️ 以下视频因过大被跳过：\n" + _format_skipped(result.skipped)
            await status_msg.edit(msg)
            await ytdlp.cleanup(result)
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
            MAX_UPLOAD_CONCURRENCY, registry=registry, caption="",
        )

        # 从首个目标转给其余目标，零重复上传，drop_author 去引用
        for target in TARGET_IDS[1:]:
            for m in sent:
                try:
                    s2 = await client.forward_messages(target, m, drop_author=True)
                    registry.add_messages(s2)
                except Exception as e:
                    print(f"⚠️ 推特扇出到 {target} 失败：{e}")

        # 把视频回复到原链接消息下面，方便在收藏夹里直接播放
        replied = 0
        try:
            replied = await media_upload.reply_with_media(client, event, sent, registry=registry)
        except Exception as e:
            print(f"⚠️ 推特视频回复到链接失败：{e}")

        await _finish_status(
            status_msg, replied, uploaded, len(TARGET_IDS), result.skipped, failed
        )

        await ytdlp.cleanup(result, delete_files=DELETE_AFTER_UPLOAD)
    except Exception as e:
        detail = str(e).strip() or type(e).__name__
        print(f"⚠️ 推特下载失败：{detail}")
        await safe_status_edit(status_msg, f"❌ **推特下载失败**：\n`{detail}`")
        await ytdlp.cleanup(result)


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
            saved, dl_failed = await media_download.download_messages(
                client, msgs, DOWNLOAD_DIR, MAX_DOWNLOAD_CONCURRENCY,
                status_msg=status_msg, title="📥 **正在从源频道并发下载到服务器…**",
                connections=DOWNLOAD_CONNECTIONS,
            )
            for idx, reason in dl_failed:
                print(f"⚠️ 下载失败（第 {idx + 1} 个）：{reason}")
            paths = "\n".join(f"`{p}`" for p in saved)
            await status_msg.edit(f"🎉 **下载完成！**\n✅ 文件已保存到服务器：\n{paths}")
            return

        # ===== 模式 B：无引用镜像到全部备份目标 =====
        await forwarder.mirror(
            client, TARGET_IDS, msgs, registry,
            status_msg=status_msg, download_dir=DOWNLOAD_DIR,
        )

    except ValueError as ve:
        await safe_status_edit(status_msg, f"❌ **权限错误**：无法访问源频道。\n`{ve}`")
    except Exception as e:
        await safe_status_edit(status_msg, f"❌ **发生未知错误**：\n`{str(e)}`")


# ================= 5. 事件处理：路由 =================

def is_download_only(chat_id):
    """该会话此刻是否走「只下载不转发」。

    TARGET_CHAT 留空即仅下载模式：收藏夹与 SOURCE_CHAT 里的媒体都落盘。
    限定这两类而非「非备份群一律落盘」，是因为监听列表将来若扩充，
    不应默认把任何新监听到的东西都往磁盘上堆。
    """
    if TARGET_IDS:
        return False
    return chat_id == ME_ID or chat_id in SOURCE_IDS


async def save_saved_media(event, msgs):
    """仅下载模式：把直接发进收藏夹的图片/视频/相册下载到本地。

    README 承诺「留空 TARGET_CHAT = 收藏夹媒体下载到本地」，但原实现里这条
    路径只对链接/磁力生效，直发媒体会静默忽略（PR #1 报告的问题）。
    走 media_download 主路径，与链接流程共享并发/分块/命名/进度设施。

    进度/结果消息一律回到收藏夹：源可能是下载机器人或别人的频道，
    往那里刷进度既吵人也可能没有发言权。收藏夹里的消息回复到原消息下。
    """
    if event.chat_id == ME_ID:
        status_msg = await event.reply("📥 **收到媒体，正在下载到服务器…**")
    else:
        status_msg = await client.send_message(
            "me", f"📥 **收到来自监听源的媒体（chat={event.chat_id}），正在下载…**"
        )
    try:
        saved, dl_failed = await media_download.download_messages(
            client, msgs, DOWNLOAD_DIR, MAX_DOWNLOAD_CONCURRENCY,
            status_msg=status_msg, title="📥 **正在下载到服务器…**",
            connections=DOWNLOAD_CONNECTIONS,
        )
        registry.add_messages(status_msg)
        if not saved:
            reasons = "\n".join(f"  • 第 {i + 1} 个：{r}" for i, r in dl_failed)
            await safe_status_edit(status_msg, f"❌ **下载失败**\n{reasons}")
            return
        lines = "\n".join(f"`{p}`" for p in saved)
        text = f"🎉 **下载完成！** 共 {len(saved)} 个文件\n✅ 已保存到服务器：\n{lines}"
        if dl_failed:
            text += "\n⚠️ 部分失败：\n" + "\n".join(
                f"  • 第 {i + 1} 个：{r}" for i, r in dl_failed
            )
        await safe_status_edit(status_msg, text)
    except Exception as e:
        detail = str(e).strip() or type(e).__name__
        print(f"⚠️ 收藏夹媒体下载失败：{detail}")
        await safe_status_edit(status_msg, f"❌ **下载失败**：\n`{detail}`")


async def on_new_message(event):
    """单条消息：收藏夹处理链接/磁力/图片视频；备份群与单向源只处理图片视频。相册交给 on_album。"""
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
        tw = re.search(TWITTER_RE, text)
        if tw:
            await handle_twitter(event, tw.group(0))
            return
        # 收藏夹里的普通图片/视频：有备份目标则无引用转发，仅下载模式落盘
        if is_media_message(msg):
            if is_download_only(event.chat_id):
                await save_saved_media(event, [msg])
                return
            try:
                await forwarder.mirror(
                    client, compute_targets(event.chat_id, ME_ID, TARGET_IDS), [msg], registry
                )
            except Exception as e:
                print(f"⚠️ 收藏夹媒体转发失败（id={msg.id}）：{e}")
        return

    # 备份群 A/B/C 或单向源（机器人/频道）：只转图片/视频，静默无引用转发。
    # compute_targets 排除来源自身：备份群 → 其余成员；单向源不在集合里 → 全部成员。
    if is_media_message(msg):
        if is_download_only(event.chat_id):
            await save_saved_media(event, [msg])
            return
        try:
            await forwarder.mirror(
                client, compute_targets(event.chat_id, ME_ID, TARGET_IDS), [msg], registry
            )
        except Exception as e:
            print(f"⚠️ 备份群媒体转发失败（chat={event.chat_id}, id={msg.id}）：{e}")


async def on_album(event):
    """相册（合并视频/图文）：只要含图片或视频就整组无引用转发。"""
    if any(registry.contains(event.chat_id, m.id) for m in event.messages):
        return
    if not any(is_media_message(m) for m in event.messages):
        return
    targets = compute_targets(event.chat_id, ME_ID, TARGET_IDS)
    if is_download_only(event.chat_id):
        await save_saved_media(event, list(event.messages))
        return
    try:
        await forwarder.mirror(client, targets, list(event.messages), registry)
    except Exception as e:
        print(f"⚠️ 相册转发失败（chat={event.chat_id}）：{e}")


# ================= 6. 启动程序 =================
async def _amain():
    global ME_ID, TARGET_IDS, SOURCE_IDS

    await client.start()
    me = await client.get_me()
    ME_ID = me.id

    # 预热实体缓存，使「按 id 解析频道/群组」可用（否则新会话可能报找不到实体）
    try:
        await client.get_dialogs()
    except Exception as e:
        print(f"⚠️ 预取会话列表失败（可能影响按 id 解析目标）：{e}")

    # 规范化每个备份目标，并组成监听列表 = 收藏夹 + 全部目标 + 全部单向源
    monitor = ['me']
    for raw in TARGET_CHATS:
        try:
            ent = await client.get_entity(raw)
            TARGET_IDS.append(utils.get_peer_id(ent))
            monitor.append(ent)
        except Exception as e:
            print(f"⚠️ 无法访问备份目标 {raw}，已跳过：{e}")

    # 单向监听源（机器人/频道/群）：只监听，永不作为转发目标
    for raw in SOURCE_CHATS:
        try:
            ent = await client.get_entity(raw)
            sid = utils.get_peer_id(ent)
            if sid in TARGET_IDS:
                # 同时也是备份目标：已在监听列表里，按目标语义处理即可
                continue
            SOURCE_IDS.append(sid)
            monitor.append(ent)
        except Exception as e:
            print(f"⚠️ 无法访问监听源 {raw}，已跳过：{e}")

    client.add_event_handler(on_album, events.Album(chats=monitor))
    client.add_event_handler(on_new_message, events.NewMessage(chats=monitor))

    if TARGET_IDS:
        src = f" + {len(SOURCE_IDS)} 个单向源" if SOURCE_IDS else ""
        print(f"🚀 UserBot 已启动：监听 收藏夹 + {len(TARGET_IDS)} 个备份目标{src}，互为无引用转发图片/视频")
    else:
        src = f" + {len(SOURCE_IDS)} 个单向源" if SOURCE_IDS else ""
        print(f"🚀 UserBot 已启动：仅下载模式（未配置 TARGET_CHAT），监听 收藏夹{src}，媒体只下载到本地")

    # 启动扫描：把 UPLOAD_DIR 里的图片/视频按相册组上传到备份目标（防重复见模块注释）。
    # 放后台任务：触发限流时会延时半小时以上自动续传，等待期间不能阻塞消息监听。
    # 失败只警告：启动上传是附加能力，不该拖垮监听主服务。
    async def _startup_upload_task():
        try:
            await startup_upload.run(
                client, TARGET_IDS, UPLOAD_DIR, int(MAX_FILE_SIZE_GB * (1024 ** 3)),
                registry, DELETE_AFTER_UPLOAD,
            )
        except Exception as e:
            detail = str(e).strip() or type(e).__name__
            print(f"⚠️ uploads 目录启动扫描失败：{detail}")

    upload_task = asyncio.ensure_future(_startup_upload_task())

    try:
        await client.run_until_disconnected()
    finally:
        upload_task.cancel()


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
