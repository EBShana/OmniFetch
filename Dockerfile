FROM python:3.12-slim

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
