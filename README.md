# telegram-downloader

一个基于 [Telethon](https://github.com/LonamiWebs/Telethon) 的 Telegram **UserBot**(个人账号机器人):监听你自己的「收藏夹 (Saved Messages)」,你把任意消息链接**或磁力链接**发进去,它就自动把对应的媒体文件**转发**或**下载**下来。

## ✨ 功能特性

- 🔗 **发链接即用**:往收藏夹粘贴 `https://t.me/...` 消息链接,自动识别并处理
- 🧲 **磁力链接下载**:粘贴 `magnet:?xt=...` 磁力链接,用 aria2 下载种子里的**视频文件**并上传到目标频道(只下视频、自动跳过超大文件)
- 🚀 **智能转发**:源频道允许转发时,直接**无引用转发**(不带「转发自」标签),零下载零流量
- 📥 **受保护内容兜底**:源频道开启「禁止保存/转发」时,自动回退为「下载到本地 → 重新上传」
- 💾 **仅下载模式**:不配置目标频道时,只把文件下载到本地,不做任何转发
- 📊 **实时进度条**:下载/上传进度动态显示在消息里
- 🐳 **Docker 一键部署**,支持 amd64 / arm64

## 🧠 工作模式

行为由 `.env` 里的 `TARGET_CHAT` 决定:

| `TARGET_CHAT`   | 行为                                                               |
| --------------- | ------------------------------------------------------------------ |
| **已配置**      | 优先无引用转发到目标频道;受保护内容则下载后重新上传,并清理本地缓存 |
| **留空 / 不填** | 仅把媒体下载到本地 `downloads/`,不转发、不上传、不删除             |

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

# 目标频道/群组 ID。留空 = 仅下载模式
TARGET_CHAT=-1001234567890

# ===== 磁力链接下载(可选,均有默认值) =====
MAX_UPLOAD_CONCURRENCY=4    # 并发上传上限(默认 4,过高易触发 FloodWait,建议 3-5)
MAX_FILE_SIZE_GB=4          # 单文件大小上限 GB,超过则不下载不上传(默认 4,对应 Premium 4GB)
DOWNLOAD_DIR=./downloads    # 下载目录
TORRENT_STALL_TIMEOUT=300   # 种子长时间无速度即放弃的秒数(处理"无人做种")
```

> 💡 `TARGET_CHAT` 留空即进入「仅下载」模式。频道 ID 通常形如 `-100xxxxxxxxxx`。
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
3. 或者粘贴一个**磁力链接**(`magnet:?xt=urn:btih:...`):程序会用 aria2 下载种子里的**视频文件**并上传到目标频道
   - 只下载/上传视频(音频、图片、字幕、nfo 等忽略,也不下载)
   - 单文件超过 `MAX_FILE_SIZE_GB`(默认 4GB)会被跳过,并在消息里说明原因
   - 多个视频按 `MAX_UPLOAD_CONCURRENCY` 并发上传
4. 机器人会回复一条状态消息,并实时更新进度,直至完成

> 🧲 本地运行磁力功能需要系统已安装 `aria2`(`apt install aria2` / `brew install aria2`);Docker 镜像已内置,无需额外安装。

## 🐳 Docker 部署(Docker Compose)

推荐用 `docker-compose.yml` 部署。先把里面的 `image:` 改成你自己的镜像名(与 GitHub Actions 推送的一致)。

`docker-compose.yml` 通过文件挂载提供配置与登录态:

| 挂载项 | 作用 |
|--------|------|
| `./.env:/app/.env:ro` | 注入 API 凭证与目标频道配置(只读) |
| `./my_transfer_session.session:/app/...` | Telegram 登录态,持久化到宿主机 |
| `./downloads:/app/downloads` | 下载的文件持久化到宿主机 |

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

## ⚙️ GitHub Actions 自动构建

工作流 `.github/workflows/telegram-downloader-docker.yml` 通过**手动触发**构建并推送镜像到 Docker Hub。

### 配置 Secrets

在 GitHub 仓库 **Settings → Secrets and variables → Actions** 添加:

| Secret            | 说明                                                                                                                          |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `DOCKERHUB_TOKEN` | Docker Hub 的 Access Token(在 [hub.docker.com](https://hub.docker.com) → Account Settings → Security 生成,**不要用登录密码**) |

> ℹ️ 镜像命名空间使用 GitHub 仓库所有者用户名(`github.repository_owner`),即**默认你的 Docker Hub 用户名与 GitHub 用户名相同**。若两者不同,请把工作流中的 `username:` 改为你的 Docker Hub 用户名(或另设一个 `DOCKERHUB_USERNAME` secret 引用)。

### 触发构建

1. 进入仓库 **Actions** 页
2. 左侧选择 **Build & Push telegram-downloader**
3. 点击 **Run workflow**,可填写镜像标签(默认 `latest`)

构建产物:`<用户名>/telegram-downloader:<tag>`,支持 `linux/amd64` 与 `linux/arm64`。

## 📁 项目结构

```
.
├── main.py                   # 主程序入口:配置、事件分流、t.me 转发流程
├── filters.py                # 视频文件识别 + 选择性下载决策(纯函数)
├── progress.py               # 进度条:单文件进度 + 并发上传聚合进度
├── aria2_client.py           # aria2c 守护进程管理 + JSON-RPC + 磁力下载
├── media_upload.py           # 视频并发上传 + FloodWait 退避
├── scripts/                  # 联调脚本(aria2_selftest.py,不依赖 Telegram)
├── tests/                    # 纯逻辑单元测试(pytest)
├── requirements.txt          # Python 依赖
├── Dockerfile                # 镜像构建(内置 aria2)
├── .dockerignore             # 排除密钥/登录态进镜像
├── .env.example              # 环境变量模板
└── .github/workflows/        # 手动触发的构建推送工作流
```

## ⚠️ 安全提示

- `.env` 与 `*.session` 含敏感凭证,已被 `.gitignore` 与 `.dockerignore` 排除,**请勿提交或打包进镜像**
- `.session` 一旦泄露,他人即可完全控制你的 Telegram 账号
- 本项目为个人账号 UserBot,请遵守 Telegram 服务条款,合理使用
