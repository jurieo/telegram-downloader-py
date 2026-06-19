# syntax=docker/dockerfile:1
FROM python:3.13-slim

# 时区设为上海，避免日志/限流时间错乱（可按需修改）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 系统依赖：aria2 用于磁力链接下载（amd64/arm64 均有，体积小）
RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用 Docker 层缓存（requirements 不变则不重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝源码（.session / .env / downloads 已被 .dockerignore 排除，运行时挂载）
COPY *.py .
# 联调脚本（不依赖 Telegram，可在容器内单独跑来排查 aria2 下载链路）
COPY scripts/ ./scripts/

# 下载目录作为数据卷，方便持久化到宿主机
VOLUME ["/app/downloads"]

CMD ["python", "main.py"]
