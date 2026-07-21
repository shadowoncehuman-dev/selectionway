# SelectionWay Bot

A Telegram bot + Mini App that lets users browse and watch course content from selectionway.com. Users interact via Telegram; the Mini App opens in Telegram's built-in browser for rich course browsing.

## Run & Operate

- **On Replit**: run `python bot/main.py` (set env vars below first)
- **On Railway**: connect the GitHub repo — Railway auto-detects `railway.toml` and runs `python bot/main.py`

## Required Environment Variables

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `ADMIN_IDS` | Comma-separated Telegram user IDs with admin access |
| `ADMIN_WEB_PASSWORD` | Password for the web admin portal at `/admin` |
| `SESSION_SECRET` | Flask session secret key |
| `WEBAPP_URL` | **Railway/prod only** — public URL of the deployed service (e.g. `https://your-app.up.railway.app`). The bot uses this as the Mini App button URL. |

## Stack

- Python 3.12, Flask, python-telegram-bot 21.6
- SQLite (runtime DB — `bot/bot_data.db`, not committed)
- Telegram Bot API + Web App (Mini App)

## Where things live

- `bot/main.py` — entire backend: Flask routes, Telegram bot handlers, DB helpers, SelectionWay API calls
- `bot/templates/` — Jinja2 HTML templates (Mini App UI + Admin portal)
- `bot/bot_data.db` — SQLite DB (users, fetch log) — gitignored, created at runtime
- `railway.toml` — Railway deployment config
- `Procfile` — fallback start command for Heroku-style platforms
- `requirements.txt` — Python dependencies (root level for Railway auto-detection)

## Architecture decisions

- Flask + Telegram polling run in one process: bot in a daemon thread, Flask in the main thread.
- API routes use `/bot-api/` prefix (not `/api/`) to avoid conflicts.
- HLS streams are proxied through `/hls?url=` so the Mini App WebView can play them (CORS workaround).
- Thumbnails use the `banner` field only — `bannerSquare` can 404 on some batches.
- `WEBAPP_URL` env var takes priority over `REPLIT_DEV_DOMAIN` for the Mini App button URL.

## User preferences

- Deploy on Railway via GitHub repo `shadowoncehuman-dev/selectionway`.
- Changes are made on Replit and pushed to GitHub using the `github_token` secret.

## Gotchas

- `bot_data.db` is gitignored — Railway will create a fresh DB on each deploy (ephemeral). Consider adding a PostgreSQL addon on Railway if you need persistent user data across deploys.
- Always set `WEBAPP_URL` on Railway to the service's public URL, otherwise the Mini App button in Telegram won't link correctly.
