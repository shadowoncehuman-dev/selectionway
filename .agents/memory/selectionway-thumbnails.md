---
name: SelectionWay thumbnail fix
description: How batch thumbnails are sourced and displayed
---

- Always use `banner` field first — it returns HTTP 200 for ALL batches
- `bannerSquare` is stale/404 for many batches — never use it as primary
- Field priority in backend: `banner > bannerSquare > bannerLandscape > thumbnail > image > coverImage > photo`
- Do NOT proxy image URLs through `/proxy-img` for `<img>` tags — img tags have no CORS restriction, proxy adds latency and fails when origin is 404
- In the frontend `loadImg()`, try direct URL first; `onerror` retries via `/proxy-img?url=...` as fallback for browser CSP environments
- The upstream image host `selectionwayserver.hranker.com` needs no special headers for `<img>` fetches

**Why direct URLs:** The proxy test that originally showed 404 was using a fabricated/wrong URL. The proxy itself works; the banner field URLs are always valid. Removing the proxy layer eliminated the 404s and improved load speed.
