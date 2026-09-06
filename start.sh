#!/usr/bin/env bash
set -e

: "${TG_API_ID:?يجب ضبط TG_API_ID في Secrets}"
: "${TG_API_HASH:?يجب ضبط TG_API_HASH في Secrets}"

mkdir -p tg-bot-api-data

cleanup() {
  kill "${BOT_PID:-}" 2>/dev/null || true
  kill "${BOT_API_PID:-}" 2>/dev/null || true
  kill "${HEALTH_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> تشغيل Health Server على المنفذ 3000..."
HEALTH_PORT=3000 python3 health_server.py &
HEALTH_PID=$!

echo "==> تشغيل سيرفر Telegram Bot API المحلي على المنفذ 8081..."
./telegram-bot-api/telegram-bot-api \
  --api-id="$TG_API_ID" \
  --api-hash="$TG_API_HASH" \
  --local \
  --http-port=8081 \
  --dir=./tg-bot-api-data \
  --log=./log.txt &

BOT_API_PID=$!

for i in $(seq 1 30); do
  if curl -s "http://127.0.0.1:8081" > /dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "==> تشغيل البوت..."
python3 bot.py &
BOT_PID=$!
wait "$BOT_PID"
