---
name: SelectionWay publishing
description: Deployment constraints for the SelectionWay Flask and Telegram service
---

The SelectionWay service is published through its root web artifact, while the
Telegram bot and Flask server run from the repository root. Workspace-wide
frontend builds may not receive workflow-only `PORT` and `BASE_PATH` values, so
Vite configs need safe build defaults while production commands explicitly set
the service port.

**Why:** The publishing builder does not behave like a development workflow:
missing workflow variables can fail an otherwise valid Vite build, and the
Python service must be wired separately from the frontend package.

**How to apply:** Keep artifact production build/run commands explicit, keep
Python dependencies in the root requirements file, and never log Telegram
HTTP client URLs at INFO because they contain the bot token.