# syntax=docker/dockerfile:1
FROM python:3.13-slim

# 时区设为上海，避免日志/限流时间错乱（可按需修改）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 系统依赖：aria2 用于磁力链接下载（amd64/arm64 均有，体积小）。
# 不装 ffmpeg：推特下载主通道(fxtwitter 直链)是自带音轨的单文件 mp4，无需合并；
# yt-dlp 兜底通道也优先选单文件 mp4（见 ytdlp_client 的 format），省下 200MB+ 镜像体积。
RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用 Docker 层缓存（requirements 不变则不重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝源码（.session / .env / downloads 已被 .dockerignore 排除，运行时挂载）
COPY main.py .
COPY common/ ./common/
COPY downloader/ ./downloader/
COPY transfer/ ./transfer/
# 联调脚本（不依赖 Telegram，可在容器内单独跑来排查下载链路）
COPY scripts/ ./scripts/

# 下载目录作为数据卷，方便持久化到宿主机
VOLUME ["/app/downloads"]

CMD ["python", "main.py"]
