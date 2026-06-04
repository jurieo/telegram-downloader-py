# telegram-downloader

一个基于 [Telethon](https://github.com/LonamiWebs/Telethon) 的 Telegram **UserBot**(个人账号机器人):监听你自己的「收藏夹 (Saved Messages)」,你把任意消息链接发进去,它就自动把对应的媒体文件**转发**或**下载**下来。

## ✨ 功能特性

- 🔗 **发链接即用**:往收藏夹粘贴 `https://t.me/...` 消息链接,自动识别并处理
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
```

> 💡 `TARGET_CHAT` 留空即进入「仅下载」模式。频道 ID 通常形如 `-100xxxxxxxxxx`。

## 🖥️ 本地运行

```bash
pip install -r requirements.txt
python telegram-downloader.py
```

**首次运行**会要求输入手机号 + 登录验证码(可能还有两步验证密码),成功后会在当前目录生成 `my_transfer_session.session` 登录态文件。之后运行不再需要登录。

> ⚠️ `.session` 文件等同于你的账号登录凭证,**切勿泄露或提交到 git**

## 📦 使用方法

1. 启动程序后,打开 Telegram,进入 **Saved Messages(收藏夹)**
2. 把任意消息链接粘贴进去,例如:
   - 公开频道:`https://t.me/channelname/123`
   - 私有频道:`https://t.me/c/1234567890/123`
3. 机器人会回复一条状态消息,并实时更新进度,直至完成

## 🐳 Docker 部署

### 拉取镜像

镜像由 GitHub Actions 自动构建并推送到 Docker Hub:

```bash
docker pull <你的DockerHub用户名>/telegram-downloader:latest
```

### 运行(挂载密钥与登录态)

> ⚠️ **必须先在本地完成一次登录生成 `my_transfer_session.session`**,再挂载进容器。容器内无交互终端,无法完成首次手机号+验证码登录。

```bash
docker run -d --name tg-downloader \
  --env-file ./.env \
  -v "$(pwd)/my_transfer_session.session:/app/my_transfer_session.session" \
  -v "$(pwd)/downloads:/app/downloads" \
  --restart unless-stopped \
  <你的DockerHub用户名>/telegram-downloader:latest
```

挂载说明:

| 挂载项                        | 作用                        |
| ----------------------------- | --------------------------- |
| `--env-file ./.env`           | 注入 API 凭证与目标频道配置 |
| `my_transfer_session.session` | 复用本地登录态,免容器内登录 |
| `downloads/`                  | 下载的文件持久化到宿主机    |

查看日志:`docker logs -f tg-downloader`

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
├── telegram-downloader.py   # 主程序
├── requirements.txt          # Python 依赖
├── Dockerfile                # 镜像构建
├── .dockerignore             # 排除密钥/登录态进镜像
├── .env.example              # 环境变量模板
└── .github/workflows/        # 手动触发的构建推送工作流
```

## ⚠️ 安全提示

- `.env` 与 `*.session` 含敏感凭证,已被 `.gitignore` 与 `.dockerignore` 排除,**请勿提交或打包进镜像**
- `.session` 一旦泄露,他人即可完全控制你的 Telegram 账号
- 本项目为个人账号 UserBot,请遵守 Telegram 服务条款,合理使用
