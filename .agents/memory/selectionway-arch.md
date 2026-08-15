---
name: SelectionWay bot architecture
description: Key structural decisions for the SelectionWay Telegram bot on Replit
---

- Single Python process: Flask (main thread) + Telegram polling (daemon thread with its own asyncio loop using `async with application` API, NOT `run_polling` — signal handler restriction)
- Artifact: `artifacts/selectionway-bot`, kind=web, port=19670, run cmd `cd ../../bot && python main.py`
- Flask API routes MUST be `/bot-api/` prefix — `/api/` is claimed by the `artifacts/api-server` artifact and intercepts all requests
- SQLite DB at `bot/bot_data.db` — tables: `users` (user_id, is_banned, fetch_limit, fetch_period), `fetch_log`
- Secrets: BOT_TOKEN, ADMIN_IDS, ADMIN_WEB_PASSWORD, SESSION_SECRET
- Telegram 409 Conflict on restart is transient — resolves on its own (old poller dying)

**Why /bot-api/ prefix:** The api-server artifact declares paths=["/api"] in artifact.toml and intercepts all /api/* requests before they reach Flask.
