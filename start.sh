#!/usr/bin/env bash
set -e

: "${TG_API_ID:?يجب ضبط TG_API_ID في Secrets}"
: "${TG_API_HASH:?يجب ضبط TG_API_HASH في Secrets}"

mkdir -p tg-bot-api-data

echo "==> تشغيل سيرفر Telegram Bot API المحلي على المنفذ 8081..."
./telegram-bot-api/telegram-bot-api \
  --api-id="$TG_API_ID" \
  --api-hash="$TG_API_HASH" \
  --local \
  --http-port=8081 \
  --dir=./tg-bot-api-data \
  --log=./tg-bot-api-data/log.txt &

BOT_API_PID=$!

# انتظار حتى يصبح السيرفر جاهز
for i in $(seq 1 30); do
  if curl -s "http://127.0.0.1:8081" > /dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "==> تشغيل البوت..."
python3 bot.py

kill $BOT_API_PID 2>/dev/null || true
