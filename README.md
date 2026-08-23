# growth-no1

عامل رشد X با Playwright که روی GitHub Actions اجرا می‌شود و از تلگرام کنترل
می‌شود. اجرای فعلی live است و پس از عبور از گیت‌های ارتباط، ایمنی و تولید متن
LLM پاسخ را ارسال می‌کند. هر پاس حداکثر یک پاسخ می‌فرستد.

## اجرای ابری بدون VPS

فایل `.github/workflows/growth-agent.yml` با cron گیت‌هاب بیدار می‌شود و داخل
پنجره‌های تهران یک جلسه محدود ۵۰ دقیقه‌ای می‌سازد. جلسه فرمان‌های تلگرام را هر
حداکثر ۳۰ ثانیه می‌خواند، پاس اول را با تأخیر تصادفی صفر تا دو دقیقه و پاس‌های
بعدی را با فاصله تصادفی ۴ تا ۱۲ دقیقه اجرا می‌کند. خاموش‌بودن کامپیوتر شخصی
اثری روی آن ندارد.

GitHub زمان اجرای cron را دقیق تضمین نمی‌کند؛ فرمان تلگرام معمولاً در اجرای بعدی
و با چند دقیقه تأخیر اعمال می‌شود. هم‌زمان polling محلی بات را اجرا نکنید، چون
با `getUpdates` ابری تداخل ایجاد می‌کند.

## Secrets لازم در GitHub

در مسیر **Settings → Secrets and variables → Actions** این سه Repository Secret
را بسازید:

- `X_COOKIES_JSON`: کل JSON خروجی Cookie Editor V3
- `TELEGRAM_BOT_TOKEN`: توکن BotFather
- `TELEGRAM_CHAT_ID`: شناسه عددی چت مجاز

مقادیر secret هیچ‌وقت در `settings.json` یا state ابری ذخیره نمی‌شوند. برای
Gemini بعداً secret مناسب provider اضافه می‌شود؛ فعلاً نبودن کلید API مجاز است.

## فرمان‌های تلگرام در حالت GitHub Actions

- `/status` و `/stats`
- `/pause` و `/resume`
- `/set_limit morning 3`
- `/set_limit evening 5`
- `/set_window morning 06:00 12:00`
- `/set_window evening 12:30 01:00`
- `/set_skill متن دستور رفتاری` یا `/set_skill clear`
- `/set_style witty|analytical|supportive`
- `/set_style custom متن سبک دلخواه`
- `/get_skill`
- `/current_api`
- `/help`

فرمان `/set_api` در حالت ابری کلید را ذخیره نمی‌کند و پیام حاوی کلید را، اگر
تلگرام اجازه دهد، حذف می‌کند. کلید provider باید فقط در GitHub Repository
Secrets قرار گیرد.

تغییرات امن در `data/cloud_runtime.json` نوشته و با Actions cache بین اجراها
حفظ می‌شوند؛ `config/settings.json` داخل checkout دست‌نخورده می‌ماند.

## تست و اجرای دستی

از تب Actions، workflow با نام **Growth Reply Agent** را با **Run workflow**
اجرا کنید. اجرای دستی نیز live است و باید با احتیاط استفاده شود.

اجرای تست‌ها در ویندوز:

```powershell
python tests/test_scheduler_and_drafts.py
python tests/test_cookies_and_provider.py
python tests/test_reply_agent_evidence.py
python tests/test_safety_gates.py
python tests/test_telegram_cloud.py
python tests/test_runtime_config_security.py
```

اجرای محلی یک پاس dry-run:

```powershell
python src/growth_no1/runner.py --once
```

فایل‌های `data/`، شواهد Playwright و `config/secrets.json` نباید commit شوند.
