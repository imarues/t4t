# Kira Bot

بوت تيليجرام (Python) جاهز للرفع على GitHub والتشغيل مباشرة عبر Replit، مع سيرفر `telegram-bot-api` محلي لدعم رفع/تنزيل ملفات كبيرة (حتى ~2GB).

## 📁 محتويات المشروع

| الملف | الوظيفة |
|---|---|
| `bot.py` | كود البوت الرئيسي |
| `requirements.txt` | مكتبات بايثون المطلوبة |
| `replit.nix` | حزم النظام المطلوبة على Replit (cmake, gcc...) |
| `.replit` | إعدادات تشغيل Replit |
| `setup.sh` | تثبيت المكتبات + بناء `telegram-bot-api` من المصدر |
| `start.sh` | تشغيل سيرفر Bot API المحلي ثم تشغيل البوت |
| `.env.example` | نموذج للمتغيرات البيئية المطلوبة |

## 🚀 الرفع على GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/USERNAME/REPO_NAME.git
git push -u origin main
```

> تأكد إن `.env` مش موجود في الريبو — ملف `.gitignore` بيستثنيه تلقائياً.

## 🔗 الربط بـ Replit

1. من لوحة Replit اختر **Create Repl → Import from GitHub**.
2. الصق رابط الريبو بتاعك واضغط **Import**.
3. Replit هيقرأ `.replit` و`replit.nix` تلقائياً ويجهز البيئة.
4. من تبويب **Secrets** (🔒) في الشريط الجانبي، أضف المتغيرات دي:
   - `BOT_TOKEN` — توكن البوت من [@BotFather](https://t.me/BotFather)
   - `TG_API_ID` و `TG_API_HASH` — من [my.telegram.org](https://my.telegram.org)
5. افتح الـ Shell في Replit وشغّل مرة واحدة فقط:
   ```bash
   bash setup.sh
   ```
   (خطوة بناء `telegram-bot-api` من المصدر بتاخد 15-30 دقيقة تقريباً — تحصل مرة واحدة بس).
6. بعد كده اضغط **Run** — أو شغّل يدوياً:
   ```bash
   bash start.sh
   ```

## ⚙️ ملاحظات

- لو حابب تشغيله باستمرار (uptime دائم)، هتحتاج خطة Replit مدفوعة (Reserved VM / Always On) لأن الحساب المجاني بيوقف الريبو بعد فترة خمول.
- سيرفر `telegram-bot-api` بيشتغل محلياً على المنفذ `8081` عشان يدعم ملفات أكبر من حدود Telegram API العادي (50MB).
- تقدر تعدّل الحدود القصوى للملفات من متغيرات البيئة في `.env.example`.
