FROM python:3.12-slim

LABEL org.opencontainers.image.title="disaster-alert-bot" \
      org.opencontainers.image.description="재난문자/기상청 지진정보 Discord 알림봇"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul \
    DB_PATH=/app/data/bot.db \
    SAFETY_DB=/data/safety.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot

VOLUME ["/app/data"]
CMD ["python", "-m", "bot"]
