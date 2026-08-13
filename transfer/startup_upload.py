"""启动扫描上传：服务启动时把 UPLOAD_DIR 里的图片/视频上传到备份目标。

- 按文件名排序后切成相册组（每组最多 10 个，Telegram 媒体组上限），整组一次
  send_file 生成相册，避免「一条对话只有一个文件」的碎片体验。
- 防重复：DELETE_AFTER_UPLOAD=true 时成功即删文件，目录里剩下的天然是未传的；
  =false 时把 (文件名, 大小, mtime) 记入目录内的 .uploaded.json 清单，下次启动
  命中即跳过；文件被改动过（大小或 mtime 变了）视为新文件重新上传。
- 失败的文件不删除、不记清单，下次扫描自动重试；命中 FloodWait 且重试耗尽时
  中止本次扫描，延时后自动续传（见 run），仍被限流才放弃到下次启动。
- 扫描/分组/清单是纯逻辑，与 telethon 调用分离，便于单测。
"""
import asyncio
import json
import os

from telethon.errors import FloodWaitError

from common.filters import is_image, is_video

# Telegram 相册（媒体组）单组文件数上限
ALBUM_SIZE = 10

# 清单文件放在上传目录内：挂 docker volume 时状态跟着目录走，无需额外挂载
MANIFEST_NAME = ".uploaded.json"

# 限流中止后自动续传前的最短等待秒数（默认半小时）
RESUME_DELAY = 1800

# 常见「还没写完」的临时文件后缀，扫描时跳过（下次启动它们多半已写完改名）
TEMP_EXTS = {".part", ".aria2", ".tmp", ".crdownload", ".download", ".downloading"}


# ================= 纯逻辑：扫描 / 清单 / 分组 =================

def scan_files(upload_dir):
    """扫描上传目录顶层，返回 (排序后的媒体文件路径列表, 被忽略的非媒体文件名列表)。

    跳过：子目录、隐藏文件（含清单自身）、临时后缀文件。
    排序按文件名，保证相册分组在多次启动间稳定可预期。
    """
    paths, ignored = [], []
    try:
        names = os.listdir(upload_dir)
    except FileNotFoundError:
        return [], []
    for name in sorted(names):
        if name.startswith("."):
            continue
        path = os.path.join(upload_dir, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() in TEMP_EXTS:
            continue
        if is_video(name) or is_image(name):
            paths.append(path)
        else:
            ignored.append(name)
    return paths, ignored


def load_manifest(upload_dir):
    """读取已上传清单：{文件名: {"size": int, "mtime": int}}。损坏/缺失按空清单处理。"""
    try:
        with open(os.path.join(upload_dir, MANIFEST_NAME), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_manifest(upload_dir, manifest):
    """清单落盘；失败只警告（最坏情况是下次重复上传，不该拖垮主流程）。"""
    try:
        with open(os.path.join(upload_dir, MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print(f"⚠️ 写入上传清单失败：{e}")


def record_uploaded(manifest, path):
    """把一个已成功上传的文件记入清单（同名旧记录被覆盖）。"""
    try:
        st = os.stat(path)
    except OSError:
        return
    manifest[os.path.basename(path)] = {"size": st.st_size, "mtime": int(st.st_mtime)}


def _in_manifest(manifest, path):
    ent = manifest.get(os.path.basename(path))
    if not isinstance(ent, dict):
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    return ent.get("size") == st.st_size and ent.get("mtime") == int(st.st_mtime)


def plan_uploads(paths, manifest, max_bytes):
    """决定哪些文件要传：返回 (待上传列表, skipped, 清单命中数)。

    skipped 为 [(文件名, 字节大小, 原因)]，目前仅「超过大小上限」；
    清单命中（上次已传成功）静默跳过，只回传个数用于日志。
    """
    pending, skipped, already = [], [], 0
    for path in paths:
        if _in_manifest(manifest, path):
            already += 1
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > max_bytes:
            skipped.append((os.path.basename(path), size, "超过大小上限"))
            continue
        pending.append(path)
    return pending, skipped, already


def chunk_albums(paths, size=ALBUM_SIZE):
    """把文件列表切成相册组，每组最多 size 个。"""
    return [paths[i:i + size] for i in range(0, len(paths), size)]


# ================= 上传流程 =================

async def _send_with_retry(client, target, files, caption, flood_retries):
    attempt = 0
    while True:
        try:
            return await client.send_file(
                target, files, caption=caption, supports_streaming=True
            )
        except FloodWaitError as e:
            attempt += 1
            if attempt > flood_retries:
                raise
            await asyncio.sleep(e.seconds + 1)


async def _upload_group(client, target, group, flood_retries):
    """整组发相册；相册整体失败时退回逐个上传，隔离坏文件。

    返回 (sent_messages, ok_paths, failed)，failed 为 [(文件名, 原因)]。
    FloodWait 重试耗尽会向上抛，由 process 中止本次扫描（下次启动续传）。
    """
    captions = [os.path.basename(p) for p in group]
    try:
        sent = await _send_with_retry(
            client, target, list(group), captions, flood_retries
        )
        return (sent if isinstance(sent, list) else [sent]), list(group), []
    except FloodWaitError:
        raise
    except Exception as e:
        if len(group) == 1:
            return [], [], [(captions[0], str(e).strip() or type(e).__name__)]
        print(f"⚠️ 相册整组上传失败，退回逐个上传：{e}")

    sent, ok_paths, failed = [], [], []
    for path in group:
        name = os.path.basename(path)
        try:
            m = await _send_with_retry(client, target, path, name, flood_retries)
            sent.append(m)
            ok_paths.append(path)
        except FloodWaitError:
            raise
        except Exception as e:
            failed.append((name, str(e).strip() or type(e).__name__))
    return sent, ok_paths, failed


async def _safe_edit(status_msg, text):
    try:
        await status_msg.edit(text)
    except Exception as e:
        print(f"⚠️ 启动上传状态更新失败：{e}")


async def process(client, target_ids, upload_dir, max_bytes, registry,
                  delete_after_upload, flood_retries=3):
    """单轮扫描：把 upload_dir 里的待传媒体按相册组上传到备份目标。

    上传到首个目标后用 forward_messages(drop_author=True) 服务器端扇出到其余
    目标，零重复上传；所有发出的消息记入 registry 防止被监听回调二次镜像。
    进度/结果发到收藏夹（与 save_saved_media 一致），docker 日志同步打印。
    返回 None = 本轮完成（含无事可做）；返回秒数 = 触发限流中止，值为
    Telegram 要求的等待时长，由 run 决定何时续传。
    """
    os.makedirs(upload_dir, exist_ok=True)

    if not target_ids:
        print("ℹ️ 未配置 TARGET_CHAT（仅下载模式），跳过 uploads 目录启动扫描")
        return None

    paths, ignored = scan_files(upload_dir)
    if ignored:
        print(f"ℹ️ uploads 目录里 {len(ignored)} 个非图片/视频文件被忽略：{', '.join(ignored[:5])}"
              + ("…" if len(ignored) > 5 else ""))

    manifest = load_manifest(upload_dir)
    pending, skipped, already = plan_uploads(paths, manifest, max_bytes)
    if already:
        print(f"ℹ️ uploads 目录 {already} 个文件已在清单中（上次已传），跳过")
    if not pending:
        if skipped:
            print("⏭️ uploads 目录仅剩超限文件，全部跳过："
                  + "、".join(name for name, _, _ in skipped))
        else:
            print("ℹ️ uploads 目录无待上传文件")
        return None

    groups = chunk_albums(pending)
    print(f"📦 uploads 目录发现 {len(pending)} 个待上传文件，分 {len(groups)} 组相册上传…")
    status_msg = await client.send_message(
        "me",
        f"📦 **启动扫描：uploads 目录发现 {len(pending)} 个待上传文件，"
        f"分 {len(groups)} 组相册上传…**",
    )
    registry.add_messages(status_msg)

    uploaded, failed = 0, []
    flood_seconds = None
    for gi, group in enumerate(groups):
        await _safe_edit(
            status_msg,
            f"📤 **正在上传第 {gi + 1}/{len(groups)} 组（{len(group)} 个文件）…**\n"
            f"✅ 已完成 {uploaded}/{len(pending)} 个",
        )
        try:
            sent, ok_paths, group_failed = await _upload_group(
                client, target_ids[0], group, flood_retries
            )
        except FloodWaitError as e:
            # 限流重试耗尽：中止本轮。未传的文件既没删也没进清单，续传时重扫即可。
            print(f"⏳ 启动上传触发限流（需等待 {e.seconds}s），中止本轮扫描")
            flood_seconds = e.seconds
            break

        registry.add_messages(sent)
        failed.extend(group_failed)
        uploaded += len(ok_paths)

        # 服务器端扇出到其余目标，零重复上传
        for target in target_ids[1:]:
            try:
                s2 = await client.forward_messages(target, sent, drop_author=True)
                registry.add_messages(s2)
            except Exception as e:
                print(f"⚠️ 启动上传扇出到 {target} 失败：{e}")

        # 成功即出队：删文件或记清单（每组落盘一次，中途挂掉不丢进度）
        for path in ok_paths:
            if delete_after_upload:
                try:
                    os.remove(path)
                except OSError as e:
                    print(f"⚠️ 删除已上传文件失败（{path}）：{e}")
            else:
                record_uploaded(manifest, path)
        if not delete_after_upload and ok_paths:
            save_manifest(upload_dir, manifest)

    extras = []
    if skipped:
        extras.append("⏭️ 跳过：\n" + "\n".join(
            f"  • {name}（{size / (1024 ** 3):.2f} GB，{reason}）"
            for name, size, reason in skipped
        ))
    if failed:
        extras.append("⚠️ 上传失败（下轮扫描自动重试）：\n" + "\n".join(
            f"  • {name}（{reason}）" for name, reason in failed
        ))
    if flood_seconds is not None:
        wait = max(RESUME_DELAY, flood_seconds)
        extras.append(f"⏳ 触发限流已中止，约 {max(1, round(wait / 60))} 分钟后自动续传剩余文件")

    head = f"🎉 **启动扫描完成！已上传 {uploaded} 个文件到 {len(target_ids)} 个目标**"
    await _safe_edit(status_msg, "\n".join([head] + extras))
    print(f"✅ uploads 本轮扫描结束：成功 {uploaded}，失败 {len(failed)}，超限跳过 {len(skipped)}")
    return flood_seconds


async def run(client, target_ids, upload_dir, max_bytes, registry,
              delete_after_upload, flood_retries=3, max_resumes=1):
    """扫描入口（常驻服务用）：限流中止后延时自动续传，不必等下次重启。

    等待时长取 RESUME_DELAY（默认半小时）与 Telegram 罚时的较大值——罚时更长时
    提前重试只会再撞限流、把惩罚越撞越长。续传 max_resumes 次后仍被限流才放弃
    （文件都还在，下次启动或手动重启再续）。设计为在后台任务里跑，等待不阻塞监听。
    """
    resumes = 0
    while True:
        flood_seconds = await process(
            client, target_ids, upload_dir, max_bytes, registry,
            delete_after_upload, flood_retries,
        )
        if flood_seconds is None:
            return
        if resumes >= max_resumes:
            print("⏳ uploads 续传仍被限流，放弃本次；剩余文件下次启动继续")
            return
        resumes += 1
        wait = max(RESUME_DELAY, flood_seconds)
        print(f"⏳ uploads 扫描将在 {wait}s 后自动续传（第 {resumes}/{max_resumes} 次）")
        await asyncio.sleep(wait)
