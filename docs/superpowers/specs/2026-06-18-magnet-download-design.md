# 磁力链接下载功能 — 设计文档

- 日期：2026-06-18

## 1. 背景与目标

现有程序是一个基于 Telethon 的 Telegram UserBot：监听账号「收藏夹（Saved Messages）」，
用户发一个 `t.me` 消息链接，它就把对应媒体**无引用转发**到 `TARGET_CHAT`；源频道禁止转发时
回退为「下载到本地 → 重新上传」。仅下载模式下（`TARGET_CHAT` 留空）只下载不上传。

**本次新增能力**：用户在收藏夹发送一个**磁力链接（magnet）**，程序自动用 BitTorrent 引擎
下载种子内容，挑出其中的**视频文件**上传到 `TARGET_CHAT`。

## 2. 需求决策

| 决策点            | 结论                                                                                                                         |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 多文件/文件夹处理 | **只处理视频文件**（不要音频、图片、字幕、nfo 等）。通过 aria2 `select-file` **选择性下载**，非视频文件不下载                |
| 单文件大小上限    | 超过 `MAX_FILE_SIZE_GB`（默认 **4**，对应 Telegram Premium 4GB）的视频**不下载、不上传**，但在状态消息里报告文件名+大小+原因 |
| 下载引擎          | **aria2c + JSON-RPC**（Dockerfile 里 apt 安装；后台守护进程；标准库 urllib 调 RPC，不引入新 HTTP 依赖）                      |
| 上传并发上限      | `MAX_UPLOAD_CONCURRENCY`，默认 **4**（信号量控制；过高易触发 Telegram FloodWait）                                            |
| 视频元数据        | 新增 `hachoir` 依赖，让 telethon 自动读取视频宽高/时长，上传带 `supports_streaming=True`                                     |
| 仅下载模式        | `TARGET_CHAT` 留空时，磁力下载完只保留本地并报告路径，不上传、不删除（与现有行为一致）                                       |

## 3. 整体流程

事件处理器 `my_event_handler`（监听 `chats='me'`）在现有 `t.me` 识别之前/之后增加磁力识别，做分流：

```
收到收藏夹消息
  ├─ 匹配 magnet:?xt=urn:btih:...  → 磁力下载新流程
  ├─ 匹配 t.me 链接                → 现有转发/重传流程（保持不动）
  └─ 都不匹配                       → 忽略
```

磁力下载新流程：

```
回复占位状态消息
  → aria2 添加磁力（先抓元数据）
  → 拿到文件清单，挑选「视频 且 ≤ MAX_FILE_SIZE_GB」的文件索引
  → select-file 只下载这些文件（其余不下载）
  → 轮询下载进度（复用 ProgressBar 风格的进度条）
  → 下载完成
  → 并发上传视频文件到 TARGET_CHAT（聚合进度 + FloodWait 退避）
  → 状态消息汇总：已上传 N 个 / 跳过哪些（超大、无视频）
  → 删除本地文件 + 清理 aria2 下载记录
```

磁力正则（识别即可，宽松匹配）：`magnet:\?xt=urn:btih:[0-9a-zA-Z]+[^\s]*`

## 4. 模块拆分（轻度重构）

当前是单文件 214 行。新功能体量较大，拆成聚焦的小文件，保持每个文件职责单一、可独立理解：

| 文件              | 职责                                                                                        | 依赖               |
| ----------------- | ------------------------------------------------------------------------------------------- | ------------------ |
| `main.py`         | 入口、配置加载、`TelegramClient`、事件分流、**现有 t.me 流程原样保留**                      | 其余模块           |
| `progress.py`     | 抽出现有 `ProgressBar`（单文件下载/上传进度）+ 新增 `AggregateProgress`（并发上传聚合进度） | telethon           |
| `aria2_client.py` | aria2c 守护进程启停 + JSON-RPC 客户端 + `download_magnet()`                                 | 标准库 only        |
| `media_upload.py` | 视频识别/大小过滤 + 并发上传池 + FloodWait 处理 + 本地清理                                  | telethon, progress |

Dockerfile 的 `COPY telegram-downloader.py .` 改为 `COPY *.py .`；`.dockerignore` 无需改动。

### 模块接口约定

- `progress.ProgressBar(message_obj, action_title)` —— 现有类原样迁移，`async callback(current, total)`。
- `progress.AggregateProgress(message_obj, action_title, total_files, total_bytes)` ——
  - `async update(file_key, current_bytes, file_done: bool)`：各上传任务回调它，内部累计「已完成文件数 / 已传字节数」，2 秒限流编辑状态消息。
- `aria2_client.Aria2Client`：
  - `start()` / `stop()`：用 `subprocess.Popen` 启停守护进程，进程退出时确保 kill。
  - `async download_magnet(magnet, status_msg, video_exts, max_file_bytes) -> DownloadResult`：
    完成下载并返回 `DownloadResult { downloaded_files: [path...], skipped: [(name, size, reason)...], root_dir }`。
  - `async cleanup(gid, root_dir)`：删除本地文件/目录 + `aria2.removeDownloadResult`。
  - 内部 RPC：`addUri` / `tellStatus` / `getFiles` / `changeOption` / `unpause` / `removeDownloadResult`，
    全部经 `loop.run_in_executor` 包裹阻塞的 urllib 调用。
- `media_upload.upload_videos(client, target_chat, files, status_msg, concurrency) -> (uploaded_count, failed)`：
  `asyncio.Semaphore(concurrency)` 限流 + `asyncio.gather`；每个视频 `send_file(..., supports_streaming=True)`；
  捕获 `FloodWaitError` 退避重试；上传进度汇报给 `AggregateProgress`。

## 5. aria2 守护进程与 RPC

启动命令（`subprocess.Popen`，由本程序管理生命周期，进程退出时 kill）：

```
aria2c
  --enable-rpc
  --rpc-listen-all=false
  --rpc-listen-port=${ARIA2_RPC_PORT:-6800}
  --rpc-secret=<内部生成/固定 secret>
  --dir=${DOWNLOAD_DIR}
  --seed-time=0                        # 下完即停，不做种、不耗上行
  --bt-stop-timeout=${TORRENT_STALL_TIMEOUT}   # 长时间 0 速度即放弃（无人做种）
  --pause-metadata=false
  --console-log-level=warn
  --daemon=false
```

RPC：标准库 `urllib.request` POST `http://127.0.0.1:<port>/jsonrpc`，body 为 JSON-RPC；
有 secret 时参数首位传 `token:<secret>`。阻塞调用统一 `await loop.run_in_executor(None, ...)`。

### 磁力 → 选择性下载的处理

1. `aria2.addUri([magnet], {dir, pause: "true"})` 添加（pause 让文件下载不自动开始，但元数据照常抓取）。
2. 轮询 `tellStatus`：磁力先下载 metadata；metadata 完成后通过 `followedBy` 派生出**真正的文件下载 gid**，代码自动切换到该 gid。
3. 在该 gid 上 `getFiles` 拿到文件清单（`index / path / length`）。按规则计算选中集合：
   - 视频扩展名 且 `length <= max_file_bytes` → **选中下载**；
   - 视频但 `length > max_file_bytes` → 不下载，记入 `skipped`（原因：超过 4GB，附大小）；
   - 非视频 → 不下载（静默忽略）。
4. 若选中集合为空 → 放弃下载并提示「未找到可上传的视频文件」（如有超大视频则一并说明）。
5. `aria2.changeOption(gid, {"select-file": "1,3,..."})` 设置只下选中文件 → `aria2.unpause(gid)` 开始下载。
6. 轮询进度直至 `status == complete`，用 `ProgressBar` 风格展示「进度% / 速度 / 已下 MB / 总 MB / 连接数」，沿用 2 秒限流。

## 6. 并发上传与聚合进度

- 下载完成后，对选中的视频文件用 `asyncio.Semaphore(MAX_UPLOAD_CONCURRENCY)` + `asyncio.gather` 并发上传。
- **聚合进度**：并发场景无法在一条消息上画多个进度条，改为一条状态消息显示
  `📤 上传中 3/9 个文件 · 1.2 GB / 5.6 GB · xx MB/s`，由 `AggregateProgress` 汇总各任务回调、2 秒限流刷新。
- 每个视频：`client.send_file(TARGET_CHAT, path, caption=<文件名>, supports_streaming=True, progress_callback=...)`。
  装了 `hachoir` 后 telethon 能自动补全视频宽高/时长，避免「方块视频」。
- **FloodWait 处理**：设置 `client.flood_sleep_threshold`（如 120）；单个上传捕获 `FloodWaitError`，
  按 `e.seconds` 退避后重试（有限次数），失败计入 `failed`。这也是默认并发取 4（而非更高）的原因。

## 7. 汇总报告与清理

上传结束后，状态消息汇总，例如：

```
🎉 完成！已上传 7 个视频
⏭️ 跳过：
  • Movie.2160p.REMUX.mkv（6.3 GB，超过 4GB 上限）
  • 其余非视频文件已忽略
```

随后**删除本地下载的文件/文件夹**（含 `.aria2` 控制文件）+ `aria2.removeDownloadResult(gid)`。
下载或上传失败时同样清理残留分片。仅下载模式（`TARGET_CHAT` 留空）则**不删除**，只报告本地路径。

## 8. 配置（`.env` 新增字段）

```dotenv
# 并发上传上限（默认 4；过高易触发 Telegram FloodWait 限流）
MAX_UPLOAD_CONCURRENCY=4
# 单文件大小上限(GB)，超过则不下载/不上传（默认 4，对应 Premium 4GB）
MAX_FILE_SIZE_GB=4
# 下载目录（默认 ./downloads，沿用现有）
DOWNLOAD_DIR=./downloads
# 种子长时间无下载速度即放弃的秒数（默认 300，处理“无人做种”）
TORRENT_STALL_TIMEOUT=300
```

`.env.example` 与 README 同步更新。aria2 的 RPC 端口/密钥用内部默认值（仅监听 127.0.0.1），不要求用户配置；
如需可选地暴露 `ARIA2_RPC_PORT`，默认 6800。

依赖（`requirements.txt`）新增：`hachoir`。系统依赖（Dockerfile）新增：`aria2`。

## 9. 错误处理边界

| 场景                          | 处理                                                       |
| ----------------------------- | ---------------------------------------------------------- |
| 无人做种 / 长期 0 速度        | `bt-stop-timeout` 触发，放弃并提示，清理分片               |
| 元数据抓取超时                | 超过 `TORRENT_STALL_TIMEOUT` 仍拿不到文件清单 → 放弃并提示 |
| 种子内无视频文件              | 提示「未找到可上传的视频文件」，清理                       |
| 视频全部超过 4GB              | 提示全部因超限被跳过，附各文件大小，清理                   |
| aria2 守护进程异常 / RPC 不通 | 状态消息报错                                               |
| 上传 FloodWait                | 按 `e.seconds` 退避重试（见 §6）                           |
| 磁盘/网络异常                 | 捕获并在状态消息报错，清理残留                             |

## 10. 测试策略

- **aria2 RPC 客户端**：用一条公开、健康的小体积测试种子（如发行版 ISO 的 magnet）实测
  `addUri → metadata → getFiles → select-file → 进度 → complete` 全链路。
- **视频过滤逻辑**：单元测试 `is_video(name)` 与「选中/跳过」决策（构造 mock 文件清单，覆盖 视频≤上限 / 视频>上限 / 非视频 / 空集合 四种情况）。
- **并发上传**：mock `client.send_file`，验证信号量并发上限、聚合进度累计、FloodWait 退避重试。
- **分流**：验证 magnet / t.me / 无关文本三类输入各自走对分支；确认 t.me 现有流程行为不回归。
- **端到端**：在测试频道用一个含视频+非视频的真实小种子跑一遍，确认只上传视频、非视频未下载、超大视频被跳过并报告、本地清理干净。

## 11. 不做（YAGNI）

- 不做大视频自动 ffmpeg 分割（已确认超限直接跳过）。
- 不做断点续传跨重启恢复（个人工具，重启重下即可）。
- 不引入外部 qBittorrent/Transmission 容器。
- 不做 .torrent 文件 / HTTP 直链下载（本次只针对 magnet）。
- 不做下载队列限流（多磁力同时发交给 aria2 自身并发处理）。
