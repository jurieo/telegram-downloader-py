"""无引用转发核心：直接转发优先，受保护内容下载重传兜底。

- 相册（合并视频/图文）整组一次性转发/重发，保留合并效果。
- drop_author=True 去掉「转发自 xxx」引用头。
- 需要发多个目标时，先传到第一个目标，再从它服务器端转给其余目标，避免重复上传。
- 每次发送后立刻把返回消息记入 registry，防止互为备份时死循环。
"""
import asyncio
import os
import time

from telethon.errors import ChatForwardsRestrictedError, FloodWaitError

from common.progress import ProgressBar
from transfer import media_download

# ================= 限流保护与提醒 =================
# 命中 FloodWait 后进入全局「冷却」：冷却期内跳过所有转发，避免在惩罚期继续发
# 请求反而把等待时间越撞越长。冷却到期后自动恢复。
_flood_until = 0.0        # 冷却截止时间戳（time.time()）
_cooldown_active = False  # 是否处于冷却，用于「进入/恢复」各提醒一次
_last_forward = 0.0       # 已预约的下一次转发时间戳，配合 FORWARD_INTERVAL 做削峰限速

# 每次 forward_messages 之间的最小间隔（秒），降低瞬时突发触发限流的概率；
# 设 0 关闭限速（测试用）。可用环境变量 FORWARD_INTERVAL 调整。
FORWARD_INTERVAL = float(os.getenv("FORWARD_INTERVAL", "3"))

# 转发/上传成功后是否删除本地下载的文件（默认 true 删除；设 false/0/no/off 保留）。
DELETE_AFTER_UPLOAD = os.getenv("DELETE_AFTER_UPLOAD", "true").strip().lower() not in (
    "0", "false", "no", "off"
)

# 相册/多文件从源下载时的并发数（默认 4，与上传并发同量级；设 1 退回串行）。
MAX_DOWNLOAD_CONCURRENCY = int(os.getenv("MAX_DOWNLOAD_CONCURRENCY", "4"))
# 单个大文件内部的分块连接数（默认 3；设 1 关闭分块，退回单连接下载）。
DOWNLOAD_CONNECTIONS = int(os.getenv("DOWNLOAD_CONNECTIONS", "3"))


def reset_flood_state():
    """重置限流冷却与限速时钟（测试用）。"""
    global _flood_until, _cooldown_active, _last_forward
    _flood_until = 0.0
    _cooldown_active = False
    _last_forward = 0.0


def in_cooldown():
    """当前是否处于转发限流冷却期。"""
    return time.time() < _flood_until


def cooldown_remaining():
    """冷却剩余秒数（已过期返回 0）。"""
    return max(0, int(_flood_until - time.time()))


async def _notify(client, text):
    """给收藏夹（Saved Messages）发一条状态提醒；失败不影响主流程。"""
    try:
        await client.send_message("me", text)
    except Exception as e:
        print(f"⚠️ 发送状态提醒失败：{e}")


async def _enter_cooldown(client, seconds):
    """进入限流冷却，并往收藏夹发一次「已暂停」提醒。"""
    global _flood_until, _cooldown_active
    _flood_until = time.time() + seconds
    _cooldown_active = True
    mins = max(1, round(seconds / 60))
    print(f"⏳ 转发触发限流：需等待 {seconds}s，进入冷却，期间跳过转发")
    await _notify(
        client,
        f"⏳ **转发被限流，已暂停**\n"
        f"需等待约 {mins} 分钟（{seconds}s），这段时间新的图片/视频**暂停转发**"
        f"（源消息仍在，不会丢失）。到期后自动恢复并再通知你。",
    )


async def _pace():
    """连续转发之间强制留出 FORWARD_INTERVAL 间隔（削峰限速）。

    用「预约下一个时隙」的方式：读改 _last_forward 之间没有 await，在单事件循环里
    是原子的，因此无需锁也能给并发的转发排队，避免瞬时突发触发限流。
    """
    global _last_forward
    if FORWARD_INTERVAL <= 0:
        return
    now = time.time()
    scheduled = max(now, _last_forward + FORWARD_INTERVAL)
    _last_forward = scheduled
    delay = scheduled - now
    if delay > 0:
        await asyncio.sleep(delay)


def _with_media(messages):
    """归一化成列表，并只保留带媒体的消息。"""
    if not isinstance(messages, (list, tuple)):
        messages = [messages]
    return [m for m in messages if m is not None and getattr(m, "media", None) is not None]


async def mirror(client, targets, messages, registry, status_msg=None, download_dir="./downloads"):
    """把 messages（单条或相册列表）无引用镜像到 targets（多个目标）。

    优先零下载直接转发；源受保护（禁转）时下载后重传。status_msg 非空（me 的链接
    流程）时显示进度与结果，否则静默（A/B/C 备份群）。返回是否有实际转发。
    命中 FloodWait 限流时进入冷却并跳过（返回 False），期间的调用直接跳过。
    """
    global _cooldown_active
    msgs = _with_media(messages)
    if not msgs or not targets:
        return False

    # 处于限流冷却期：直接跳过，不发任何转发请求（源消息仍在，不算丢失）
    if in_cooldown():
        secs = cooldown_remaining()
        print(f"⏳ 转发限流冷却中（剩余约 {secs}s），跳过本组 {len(msgs)} 条")
        if status_msg:
            await status_msg.edit(f"⏳ **转发被限流，冷却剩余约 {secs}s，已跳过本次**")
        return False

    # 冷却刚结束：通知已恢复
    if _cooldown_active:
        _cooldown_active = False
        await _notify(client, "✅ **转发已恢复**（限流冷却结束）。")

    restricted = any(getattr(m, "noforwards", False) for m in msgs)
    try:
        if not restricted:
            try:
                await _direct_forward(client, targets, msgs, registry)
                if status_msg:
                    await status_msg.edit(f"🎉 **已无引用转发到 {len(targets)} 个目标**")
                return True
            except ChatForwardsRestrictedError:
                # noforwards 漏判或源临时限制：回退到下载重传
                if status_msg:
                    await status_msg.edit("⚠️ **直接转发被限制，改用下载重传方案...**")

        await _download_and_reupload(client, targets, msgs, registry, status_msg, download_dir)
        return True
    except FloodWaitError as e:
        await _enter_cooldown(client, e.seconds)
        if status_msg:
            await status_msg.edit(
                f"⏳ **转发被限流（需等待约 {e.seconds}s），已进入冷却并跳过本次**"
            )
        return False


async def _direct_forward(client, targets, msgs, registry):
    """零下载直接转发：整组一次性转发以保留相册合并，drop_author 去引用。"""
    for target in targets:
        await _pace()
        sent = await client.forward_messages(target, msgs, drop_author=True)
        registry.add_messages(sent)


async def _download_and_reupload(client, targets, msgs, registry, status_msg, download_dir):
    """受保护内容兜底：下载到本地 → 传到首个目标 → 服务器端转给其余目标 → 清理。"""
    paths, dl_failed = await media_download.download_messages(
        client, msgs, download_dir, MAX_DOWNLOAD_CONCURRENCY,
        status_msg=status_msg, title="📥 **正在从源并发下载到服务器…**",
        connections=DOWNLOAD_CONNECTIONS,
    )
    for idx, reason in dl_failed:
        print(f"⚠️ 下载源消息失败（第 {idx + 1} 个）：{reason}")

    if not paths:
        if status_msg:
            await status_msg.edit("❌ **下载失败**：没有可上传的文件。")
        return

    if status_msg:
        await status_msg.edit("✅ **下载完成，正在上传到目标...**")

    first = targets[0]
    caption = getattr(msgs[0], "text", "") or ""
    ul_cb = ProgressBar(status_msg, "📤 **正在上传到目标...**").callback if status_msg else None

    if len(paths) > 1:
        # 相册整组重发（一次 send_file 传文件列表即生成相册）
        sent = await client.send_file(
            first, paths, caption=caption, supports_streaming=True, progress_callback=ul_cb
        )
    else:
        # 单文件：复用源媒体的视频属性/mime，避免重传后「方块视频」或不可流式播放
        send_kwargs = {}
        doc = getattr(msgs[0], "document", None)
        if doc is not None:
            send_kwargs["attributes"] = doc.attributes
            send_kwargs["mime_type"] = doc.mime_type
        sent = await client.send_file(
            first, paths[0], caption=caption, supports_streaming=True,
            progress_callback=ul_cb, **send_kwargs
        )
    registry.add_messages(sent)

    # 从首个目标服务器端转给其余目标，零重复上传
    for target in targets[1:]:
        await _pace()
        s2 = await client.forward_messages(target, sent, drop_author=True)
        registry.add_messages(s2)

    if DELETE_AFTER_UPLOAD:
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass

    if status_msg:
        await status_msg.edit(f"🎉 **已下载重传并无引用转发到 {len(targets)} 个目标**")
