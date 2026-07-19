---
name: SelectionWay ban flow
description: How admin bans affect bot commands and mini app access
---

- **Bot commands**: `check_rate_limit(user_id)` in main.py is called before every command handler. If `user.is_banned`, returns `(False, "🚫 You have been banned…")` and the bot replies with that message.
- **Mini app**: On startup, `boot()` reads `Telegram.WebApp.initDataUnsafe.user.id`, calls `/bot-api/check-user?user_id=<id>`. If `{"banned": true}`, replaces the entire UI with a full-screen ban screen (🚫 icon, pulsing red ring, ban message). Header and topbar are hidden.
- `/bot-api/check-user` endpoint: GET, param `user_id`. Queries SQLite `users` table. Returns `{"banned": false}` for unknown users (graceful) and for admins.
- If no Telegram user context (browser preview, no initData), ban check is skipped and content loads normally.
- Network errors on ban check are caught silently — user gets content access (fail-open, better than blocking legit users).

**Why fail-open on network error:** A ban check timeout or 502 should not lock out real users. Banning is an admin action for bad actors, not a security boundary.
