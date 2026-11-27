FROM python:3.12-slim

# python:slim 默认不带 tzdata；装上它，compose 里的 TZ=Asia/Shanghai 才会生效，
# 每日派发额度才会按北京时间午夜重置。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config ./config

ENV OMNIFETCH_DB=/data/omnifetch.db \
    OMNIFETCH_CONFIG=/app/config \
    OMNIFETCH_POLL_INTERVAL=30

VOLUME /data
EXPOSE 8080

# The DB schema is created/verified at startup; the container is stateless
# and can be recreated freely — all persistent data lives in /data.
CMD ["omnifetch", "serve"]
