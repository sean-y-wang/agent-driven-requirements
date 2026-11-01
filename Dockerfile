# syntax=docker/dockerfile:1
# Image for Agent-Driven Requirements (pip package name remains feishu-bitable-bot).
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BOT_CONFIG=/app/config/roles.yaml BOT_DB_PATH=/data/bot.db \
    REPORT_DB_PATH=/data/reports.db PROGRESS_LOCK_DIR=/data/progress-locks TZ=Asia/Shanghai
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --home /app bot && mkdir -p /app/config /data && chown bot /data
WORKDIR /app
USER 10001
VOLUME ["/data"]
# Events arrive over an outbound WebSocket long connection; 8787 is only the optional
# internal dashboard (enabled when DASHBOARD_TOKEN is set; keep it on the LAN).
ENV DASHBOARD_PORT=8787 BOT_ENV_FILE=/app/.env
EXPOSE 8787
ENTRYPOINT ["python", "-m", "bitable_bot"]
