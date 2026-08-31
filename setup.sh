#!/usr/bin/env bash
set -e

echo "==> تثبيت مكتبات بايثون..."
pip install -r requirements.txt --break-system-packages

echo "==> تثبيت متصفح Playwright (Chromium)..."
python3 -m playwright install --with-deps chromium || python3 -m playwright install chromium

echo "==> بناء telegram-bot-api من المصدر (قد يأخذ 15-30 دقيقة ويستهلك موارد كثيرة)..."
if [ ! -f "./telegram-bot-api/telegram-bot-api" ]; then
  rm -rf td
  git clone --depth 1 https://github.com/tdlib/telegram-bot-api.git td
  cd td
  git submodule update --init --recursive --depth 1
  mkdir -p build
  cd build
  cmake -DCMAKE_BUILD_TYPE=Release .. 
  cmake --build . --target install -j 2
  cd ../..
  mkdir -p telegram-bot-api
  cp ./td/build/telegram-bot-api telegram-bot-api/telegram-bot-api 2>/dev/null || \
  find ./td -maxdepth 4 -name "telegram-bot-api" -type f -exec cp {} telegram-bot-api/telegram-bot-api \;
  chmod +x telegram-bot-api/telegram-bot-api
fi

echo "==> تم الإعداد. الآن شغّل start.sh"
