# telegram-downloader

一个基于 [Telethon](https://github.com/LonamiWebs/Telethon) 的 Telegram **UserBot**(个人账号机器人):监听你的**收藏夹**以及一组**互为备份的频道/群组**,自动把视频**无引用转发**(不带「转发自」标签)在它们之间同步,实现互为备份。也支持把消息链接、磁力链接对应的媒体下载/转发。

## ✨ 功能特性

- 🔁 **多源互为备份**:配置一组频道/群组,任意一个里出现的视频都无引用转发到其余成员(收藏夹 `me` 里的视频/链接则转发到全部成员)
- 📡 **单向监听源**:`SOURCE_CHAT` 配置机器人/频道/群,里面出现的图片/视频自动镜像到备份集合(典型用法:接收「下载机器人」回传的视频)
- 🎞️ **合并视频/相册**:整组(媒体组)一次性转发,保留「合并」效果不拆散
- 🔗 **发链接即用**:往收藏夹粘贴 `https://t.me/...` 消息链接,自动识别并处理
- 🐦 **推特/X 视频下载**:往收藏夹粘贴公开推文链接(`https://x.com/.../status/...`),用 yt-dlp 下载视频并上传到备份目标(仅公开推文、不需登录)
- 🧲 **磁力链接下载**:粘贴 `magnet:?xt=...` 磁力链接,用 aria2 下载种子里的**视频文件**并上传到备份目标(只下视频、自动跳过超大文件)
- 🚀 **智能转发**:源允许转发时,直接**无引用转发**,零下载零流量;受保护内容自动回退为「下载到本地 → 重新上传」
- ♻️ **防回环**:同一账号在多个备份群间转发不会死循环
- 💾 **仅下载模式**:不配置备份目标时,只把收藏夹里的文件下载到本地
- 📊 **实时进度条** · 🐳 **Docker 一键部署**(amd64 / arm64)

## 🧠 工作模式

行为由 `.env` 里的 `TARGET_CHAT` 决定。它是一个**逗号分隔的备份集合** S,例如 `TARGET_CHAT=-100AAA,-100BBB,-100CCC`:

| 视频出现的位置                    | 无引用转发到                  |
| --------------------------------- | ----------------------------- |
| **收藏夹 `me`**                   | S 全部成员(A、B、C)           |
| **S 中的某个(如 A)**              | S 的其余成员(B、C),不含 `me`  |
| **单向源 `SOURCE_CHAT` 中的某个** | S 全部成员;源永不作为转发目标 |

- **只转视频**(含相册里含视频的整组),其他消息忽略。
- **链接 / 磁力仅在收藏夹处理**:下载后转发到 S 全部;A/B/C 里的链接忽略。
- **`me` 永不作为转发目标**;A/B/C 里任何人发的视频都会触发备份。
- 典型用法:你从外部频道把视频**转发**进 A,机器人自动无引用镜像到 B、C。

| `TARGET_CHAT`     | 行为                                                     |
| ----------------- | -------------------------------------------------------- |
| **填 1 个或多个** | 按上表在收藏夹与备份集合之间无引用转发视频               |
| **留空 / 不填**   | 仅把收藏夹里的媒体下载到本地 `downloads/`,不转发、不上传 |

## 🔧 前置准备

### 1. 获取 Telegram API 凭证

1. 打开 [my.telegram.org](https://my.telegram.org) 并登录
2. 进入 **API development tools**,创建一个应用
3. 记下 `api_id` 和 `api_hash`

> ⚠️ API_ID/API_HASH 是「应用凭证」,与登录哪个账号本身无关(同一套凭证能登录不同账号)。但 Telegram 倾向于「谁创建的凭证就用谁登录」,**用一套凭证频繁登录多个不同账号容易触发风控/封号**。建议用你要登录的那个账号自己申请 API_ID/API_HASH。

### 2. 配置 `.env`

复制模板并填写:

```bash
cp .env.example .env
```

```dotenv
# Telegram API 凭证(从 my.telegram.org 获取)
API_ID=12345678
API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 备份集合:逗号分隔多个频道/群组 ID(互为无引用转发)。留空 = 仅下载模式
TARGET_CHAT=-1001111111111,-1002222222222,-1003333333333

# 单向监听源(可选):机器人/频道/群 ID,里面的图片/视频自动镜像到 TARGET_CHAT 全部成员
# 机器人对话 ID 是正数,可用 scripts/list_dialogs.py 查询
SOURCE_CHAT=

# ===== 磁力链接下载(可选,均有默认值) =====
MAX_UPLOAD_CONCURRENCY=4    # 并发上传上限(默认 4,过高易触发 FloodWait,建议 3-5)
MAX_DOWNLOAD_CONCURRENCY=4  # 相册/多文件从源下载的并发上限(默认 4,设 1 退回逐个下载)
DOWNLOAD_CONNECTIONS=3      # 单个大文件(>10MB)内部的分块连接数(默认 3,设 1 关闭分块)
MAX_FILE_SIZE_GB=4          # 单文件大小上限 GB,超过则不下载不上传(默认 4,对应 Premium 4GB)
DOWNLOAD_DIR=./downloads    # 下载目录
UPLOAD_DIR=./uploads        # 启动扫描上传目录(放进去的图片/视频在服务启动时按相册组上传到 TARGET_CHAT)
TORRENT_STALL_TIMEOUT=300   # 种子长时间无速度即放弃的秒数(处理"无人做种")

# ===== 转发限速(可选) =====
FORWARD_INTERVAL=3          # 每两次转发之间的最小间隔秒数(默认 3,防突发触发 FloodWait;设 0 关闭)

# ===== 本地文件清理(可选) =====
DELETE_AFTER_UPLOAD=true    # 转发/上传成功后是否删除本地下载的文件(默认 true;设 false 保留在 DOWNLOAD_DIR)

# ===== 代理(可选) =====
PROXY_URL=                  # socks5://[user:pass@]host:port 或 http://...;留空直连,格式错误只警告不影响启动
```

> 💡 `TARGET_CHAT` 可填 1 个或多个 ID,英文逗号分隔;留空即进入「仅下载」模式。频道 ID 通常形如 `-100xxxxxxxxxx`。
> 磁力相关字段不填则用默认值,旧 `.env` 无需改动即可使用。

## 🖥️ 本地运行

```bash
pip install -r requirements.txt
python main.py
```

**首次运行**会要求输入手机号 + 登录验证码(可能还有两步验证密码),成功后会在当前目录生成 `my_transfer_session.session` 登录态文件。之后运行不再需要登录。

> ⚠️ `.session` 文件等同于你的账号登录凭证,**切勿泄露或提交到 git**

## 📦 使用方法

1. 启动程序后,打开 Telegram,进入 **Saved Messages(收藏夹)**
2. 把任意消息链接粘贴进去,例如:
   - 公开频道:`https://t.me/channelname/123`
   - 私有频道:`https://t.me/c/1234567890/123`
   - 链接指向相册(一组多个视频)时,整组按 `MAX_DOWNLOAD_CONCURRENCY` **并发下载**,不再逐个排队
   - 超过 10MB 的单个文件再按 `DOWNLOAD_CONNECTIONS` **分块多连接下载**,两层并发叠加
3. 或者粘贴一个**磁力链接**(`magnet:?xt=urn:btih:...`):程序会用 aria2 下载种子里的**视频文件**并上传到目标频道
   - 只下载/上传视频(音频、图片、字幕、nfo 等忽略,也不下载)
   - 单文件超过 `MAX_FILE_SIZE_GB`(默认 4GB)会被跳过,并在消息里说明原因
   - 多个视频按 `MAX_UPLOAD_CONCURRENCY` 并发上传
4. 或者粘贴一个**公开推特/X 推文链接**(`https://x.com/<用户>/status/<id>`,链接后面带 `/video/1?s=...` 等参数也可):程序会用 yt-dlp 下载推文里的视频并上传到目标频道
   - 双通道:优先 [fxtwitter](https://github.com/FixTweet/FxTwitter) 公共 API 取 mp4 直链(快、对 X 新视频管线稳定);fxtwitter 不可用时回退 yt-dlp 官方解析兜底
   - 只支持**公开推文**(不接 cookies);需登录才能看的(NSFW/受保护/私密)会提示无法下载
   - 推文内**图片**不在下载范围(yt-dlp 面向视频)
5. 机器人会回复一条状态消息,并实时更新进度,直至完成
6. 也可以把图片/视频文件直接放进 `UPLOAD_DIR`(默认 `./uploads`)目录,**重启服务**后自动按相册组(每组最多 10 个,按路径排序)上传到 `TARGET_CHAT` 并扇出到其余备份目标
   - **子目录递归遍历**,只上传里面的图片/视频;同一子目录的文件归入同一批相册,不与其他目录混组(一个子目录 = 一批内容)
   - 防重复:`DELETE_AFTER_UPLOAD=true`(默认)时上传成功即删除文件(清空的子目录一并清理);设 `false` 保留文件,已传记录写入根目录的 `.uploaded.json` 清单(按相对路径记录),再次启动时跳过(文件内容改动过会重新上传)
   - 上传失败的文件不删除、不记清单,下轮扫描自动重试;触发限流时中止本轮,在后台等约半小时(Telegram 罚时更长则按罚时等)后自动续传,续传仍被限流才等到下次启动
   - 隐藏文件/隐藏目录、`.part`/`.tmp` 等未写完的临时文件、非图片/视频文件会被跳过

> 🧲 本地运行磁力功能需要系统已安装 `aria2`(`apt install aria2` / `brew install aria2`);Docker 镜像已内置。
> 🐦 推特下载**通常不需要 ffmpeg**(主通道 fxtwitter 直链是自带音轨的单文件 mp4);仅极少数只有分离音视频流的视频需要,本地可选装(`brew install ffmpeg`),镜像为控制体积未内置。

## 🐳 Docker 部署(Docker Compose)

推荐用 `docker-compose.yml` 部署。先把里面的 `image:` 改成你自己的镜像名(与 GitHub Actions 推送的一致)。

`docker-compose.yml` 通过文件挂载提供配置与登录态:

| 挂载项                                   | 作用                              |
| ---------------------------------------- | --------------------------------- |
| `./.env:/app/.env:ro`                    | 注入 API 凭证与目标频道配置(只读) |
| `./my_transfer_session.session:/app/...` | Telegram 登录态,持久化到宿主机    |
| `./downloads:/app/downloads`             | 下载的文件持久化到宿主机          |

### ⚠️ 首次登录:必须先在容器内交互完成一次

容器后台运行(`up -d`)没有交互终端,无法输入手机号和验证码。
**首次部署时,先用下面这条交互命令登录一次**,登录态会写入挂载的 `my_transfer_session.session`,之后就能后台运行了。

```bash
# 1. 准备工作:确保 .env 已填好,且 session 文件先占位存在(否则挂载会变成目录)
touch my_transfer_session.session

# 2. 交互式首次登录:依次输入手机号、Telegram 发来的验证码、(如有)两步验证密码
docker compose run --rm -it telegram-downloader python main.py

# 看到 "🚀 UserBot 已启动并正在监听收藏夹..." 即登录成功,按 Ctrl+C 退出
```

> 💡 为什么不能让容器自动生成?因为 Telegram 的验证码只发到你本人的 App,必须手动输入一次。这一步**只需做一次**,session 生成后会一直复用。

### 启动 / 常用命令

```bash
docker compose up -d        # 后台启动(首次登录完成后)
docker compose logs -f      # 查看实时日志
docker compose pull         # 拉取最新镜像
docker compose restart      # 重启
docker compose down         # 停止并移除容器
```

## 📁 项目结构

```
.
├── main.py                   # 主程序入口:配置、启动、事件路由(收藏夹链接/磁力/推特 + 多源视频备份)
├── common/                   # 共享工具
│   ├── filters.py            #   媒体识别:文件名/消息类型判断 + 种子选片(纯函数)
│   └── progress.py           #   进度条:单文件进度 + 并发上传聚合进度
├── downloader/               # 下载引擎
│   ├── aria2_client.py       #   aria2c 守护进程管理 + JSON-RPC + 磁力下载
│   └── ytdlp_client.py       #   推特/X 视频下载:fxtwitter 直链优先 + yt-dlp 官方解析兜底
├── transfer/                 # Telegram 转发侧
│   ├── routing.py            #   路由与防回环:目标解析 + 转发拓扑 + 已发消息登记(纯逻辑)
│   ├── forwarder.py          #   无引用转发:直接转发 + 相册整组 + 受保护下载重传 + 传一次转其余
│   ├── media_upload.py       #   视频并发上传 + FloodWait 退避
│   └── startup_upload.py     #   启动扫描 uploads 目录:按相册组上传 + 清单防重复
├── scripts/                  # 联调脚本(aria2_selftest.py / ytdlp_selftest.py,不依赖 Telegram)
├── tests/                    # 纯逻辑单元测试(pytest)
├── requirements.txt          # Python 依赖
├── Dockerfile                # 镜像构建(内置 aria2;不含 ffmpeg 以控制体积)
├── .dockerignore             # 排除密钥/登录态进镜像
├── .env.example              # 环境变量模板
└── .github/workflows/        # 手动触发的构建推送工作流
```

## ⚠️ 安全提示

- `.env` 与 `*.session` 含敏感凭证,已被 `.gitignore` 与 `.dockerignore` 排除,**请勿提交或打包进镜像**
- `.session` 一旦泄露,他人即可完全控制你的 Telegram 账号
- 本项目为个人账号 UserBot,请遵守 Telegram 服务条款,合理使用

## 鸣谢

感谢 [Linux.do](https://linux.do/) 社区提供的讨论、反馈与支持。
