"""无引用转发核心：直接转发优先，受保护内容下载重传兜底。

- 相册（合并视频/图文）整组一次性转发/重发，保留合并效果。
- drop_author=True 去掉「转发自 xxx」引用头。
- 需要发多个目标时，先传到第一个目标，再从它服务器端转给其余目标，避免重复上传。
- 每次发送后立刻把返回消息记入 registry，防止互为备份时死循环。
"""
import os

from telethon.errors import ChatForwardsRestrictedError

from progress import ProgressBar


def _with_media(messages):
    """归一化成列表，并只保留带媒体的消息。"""
    if not isinstance(messages, (list, tuple)):
        messages = [messages]
    return [m for m in messages if m is not None and getattr(m, "media", None) is not None]


async def mirror(client, targets, messages, registry, status_msg=None, download_dir="./downloads"):
    """把 messages（单条或相册列表）无引用镜像到 targets（多个目标）。

    优先零下载直接转发；源受保护（禁转）时下载后重传。status_msg 非空（me 的链接
    流程）时显示进度与结果，否则静默（A/B/C 备份群）。返回是否有实际转发。
    """
    msgs = _with_media(messages)
    if not msgs or not targets:
        return False

    restricted = any(getattr(m, "noforwards", False) for m in msgs)
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


async def _direct_forward(client, targets, msgs, registry):
    """零下载直接转发：整组一次性转发以保留相册合并，drop_author 去引用。"""
    for target in targets:
        sent = await client.forward_messages(target, msgs, drop_author=True)
        registry.add_messages(sent)


async def _download_and_reupload(client, targets, msgs, registry, status_msg, download_dir):
    """受保护内容兜底：下载到本地 → 传到首个目标 → 服务器端转给其余目标 → 清理。"""
    os.makedirs(download_dir, exist_ok=True)

    paths = []
    for idx, m in enumerate(msgs):
        cb = None
        if status_msg:
            title = "📥 **正在从源下载到服务器...**"
            if len(msgs) > 1:
                title = f"📥 **正在下载第 {idx + 1}/{len(msgs)} 个...**"
            cb = ProgressBar(status_msg, title).callback
        path = await client.download_media(m, file=download_dir + "/", progress_callback=cb)
        if path:
            paths.append(path)

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
        s2 = await client.forward_messages(target, sent, drop_author=True)
        registry.add_messages(s2)

    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass

    if status_msg:
        await status_msg.edit(f"🎉 **已下载重传并无引用转发到 {len(targets)} 个目标**")
