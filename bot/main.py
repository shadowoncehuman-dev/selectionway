"""
SelectionWay Telegram Bot
- Telegram bot (python-telegram-bot 21.6, polling)
- Flask web server: Mini App UI, HLS proxy, image/video proxy, Admin portal
- SQLite user DB with rate limiting
"""
import os, re, json, html, sqlite3, threading, requests, logging, asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, parse_qs, quote, urlparse
from flask import (Flask, request, jsonify, render_template, Response,
                   redirect, url_for, session, stream_with_context)
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      BotCommand, WebAppInfo)
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, filters, ContextTypes)

# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

_admin_ids_raw = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip().isdigit()}

ADMIN_WEB_PASSWORD = os.environ.get("ADMIN_WEB_PASSWORD", "admin123")

PORT = int(os.environ.get("PORT", 5000))
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")

# The public URL for the Mini App WebApp button (uses Replit dev domain)
def get_webapp_url():
    domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
    if domain:
        return f"https://{domain}"
    return f"http://localhost:{PORT}"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 3500
PERIOD_LABELS = {"day": "per day", "week": "per week", "month": "per month", "unlimited": "unlimited"}
IST = ZoneInfo("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────

def _get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            first_name   TEXT,
            is_banned    INTEGER DEFAULT 0,
            fetch_limit  INTEGER DEFAULT 1,
            fetch_period TEXT    DEFAULT 'day',
            joined_at    TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS fetch_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            batch_id    TEXT,
            batch_name  TEXT,
            fetched_at  TEXT DEFAULT (datetime('now'))
        );
        """)

def db_ensure_user(user_id: int, username: str = "", first_name: str = ""):
    with _get_db() as db:
        db.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name
        """, (user_id, username or "", first_name or ""))

def db_get_user(user_id: int):
    with _get_db() as db:
        return db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

def db_all_users(limit: int = 1000, offset: int = 0):
    with _get_db() as db:
        return db.execute(
            "SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()

def db_count_users():
    with _get_db() as db:
        return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def db_set_banned(user_id: int, banned: bool):
    with _get_db() as db:
        db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?",
                   (1 if banned else 0, user_id))

def db_set_limit(user_id: int, limit: int, period: str):
    with _get_db() as db:
        db.execute("UPDATE users SET fetch_limit = ?, fetch_period = ? WHERE user_id = ?",
                   (limit, period, user_id))

def db_log_fetch(user_id: int, batch_id: str, batch_name: str):
    with _get_db() as db:
        db.execute("INSERT INTO fetch_log (user_id, batch_id, batch_name) VALUES (?, ?, ?)",
                   (user_id, batch_id, batch_name))

def db_fetch_count_in_period(user_id: int, period: str) -> int:
    if period == "unlimited":
        return 0
    now_utc = datetime.now(timezone.utc)
    if period == "day":
        since = (now_utc - timedelta(days=1)).isoformat()
    elif period == "week":
        since = (now_utc - timedelta(weeks=1)).isoformat()
    elif period == "month":
        since = (now_utc - timedelta(days=30)).isoformat()
    else:
        return 0
    with _get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE user_id = ? AND fetched_at >= ?",
            (user_id, since)
        ).fetchone()
    return row[0] if row else 0

def db_user_fetch_history(user_id: int, limit: int = 10):
    with _get_db() as db:
        return db.execute(
            "SELECT batch_name, fetched_at FROM fetch_log WHERE user_id = ? ORDER BY fetched_at DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()

def db_stats():
    with _get_db() as db:
        total   = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned  = db.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
        fetches = db.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
        today   = db.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE fetched_at >= datetime('now','-1 day')"
        ).fetchone()[0]
    return {"total": total, "banned": banned, "fetches": fetches, "today": today}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def check_rate_limit(user_id: int):
    if is_admin(user_id):
        return True, ""
    user = db_get_user(user_id)
    if not user:
        return True, ""
    if user["is_banned"]:
        return False, "🚫 You have been banned from using this bot."
    period = user["fetch_period"]
    limit  = user["fetch_limit"]
    if period == "unlimited":
        return True, ""
    count = db_fetch_count_in_period(user_id, period)
    if count >= limit:
        return False, (
            f"⏳ You've reached your fetch limit ({limit} {PERIOD_LABELS[period]}).\n"
            "Please wait for the period to reset, or ask an admin for a higher limit."
        )
    return True, ""

# ─────────────────────────────────────────────────────────────────
# SelectionWay API
# ─────────────────────────────────────────────────────────────────

BASE_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "content-type": "application/json",
    "accept": "*/*",
    "origin": "https://www.selectionway.com",
    "referer": "https://www.selectionway.com/",
    "accept-encoding": "gzip, deflate",   # no br/zstd — urllib3 can't decode them
    "accept-language": "en-US,en;q=0.9",
}

def sw_get_all_batches():
    try:
        r = requests.get(
            "https://backend.multistreaming.site/api/courses/active?userId=1448640",
            headers={**BASE_HEADERS, "host": "backend.multistreaming.site"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("state") == 200:
            return True, data["data"]
        return False, "API returned non-200"
    except Exception as e:
        return False, str(e)

def sw_get_batch_classes(course_id: str):
    try:
        r = requests.get(
            f"https://backend.multistreaming.site/api/courses/{course_id}/classes?populate=full",
            headers={**BASE_HEADERS, "host": "backend.multistreaming.site"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("state") == 200:
            return True, data["data"]
        return False, "No data"
    except Exception as e:
        return False, str(e)

YOUTUBE_ID_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,})'
)
QUALITY_ORDER = ["720p", "480p", "360p", "240p"]
DAY_ALIASES = {
    'mon': 0, 'tue': 1, 'tues': 1, 'wed': 2, 'thu': 3, 'thur': 3, 'thurs': 3,
    'fri': 4, 'sat': 5, 'sun': 6,
}
TIME_RANGE_RE = re.compile(
    r'(\d{1,2}:\d{2}\s*[APap][Mm])\s*-\s*(\d{1,2}:\d{2}\s*[APap][Mm])\s*\(([^)]*)\)'
)

def _parse_time_12h(text):
    return datetime.strptime(text.strip().upper().replace(' ', ''), "%I:%M%p").time()

def _expand_days(day_text):
    days = set()
    for part in re.split(r'[,&]', day_text or ''):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            bounds = [p.strip()[:3].lower() for p in part.split('-', 1)]
            start, end = DAY_ALIASES.get(bounds[0]), DAY_ALIASES.get(bounds[1])
            if start is None or end is None:
                continue
            d = start
            while True:
                days.add(d)
                if d == end:
                    break
                d = (d + 1) % 7
        else:
            key = part[:3].lower()
            if key in DAY_ALIASES:
                days.add(DAY_ALIASES[key])
    return days

def is_batch_live_now(batch):
    if not batch.get('isLive') or not batch.get('isTimeTable'):
        return False
    now = datetime.now(IST)
    today = now.weekday()
    now_time = now.time()
    for slot in batch.get('timeTable') or []:
        match = TIME_RANGE_RE.search(slot.get('time') or '')
        if not match:
            continue
        try:
            start_t = _parse_time_12h(match.group(1))
            end_t   = _parse_time_12h(match.group(2))
        except ValueError:
            continue
        if today not in _expand_days(match.group(3)):
            continue
        if start_t <= end_t:
            if start_t <= now_time <= end_t:
                return True
        elif now_time >= start_t or now_time <= end_t:
            return True
    return False

def is_class_live_now(class_item):
    if not class_item.get('isLive'):
        return False
    start_raw, end_raw = class_item.get('startDate'), class_item.get('endDate')
    if not start_raw or not end_raw:
        return False
    try:
        start = datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
        end   = datetime.fromisoformat(end_raw.replace('Z', '+00:00'))
    except ValueError:
        return False
    return start <= datetime.now(timezone.utc) <= end

def extract_youtube_id(url):
    match = YOUTUBE_ID_RE.search(url or '')
    return match.group(1) if match else None

def sort_batches_by_recency(batches):
    def key(c):
        return c.get('updatedAt') or c.get('createdAt') or ''
    return sorted(batches, key=key, reverse=True)

def clean_url(url):
    return (url or "").replace(" ", "%20")

def proxied_hls_url(raw_url: str) -> str:
    base = get_webapp_url()
    return f"{base}/hls?url={quote(raw_url, safe='')}"

def build_classes_data(classes_data, batch_meta: dict):
    """Parse raw classes API response into structured topics + video list."""
    video_links = []
    pdf_links   = []
    test_links  = []
    topics      = []

    pdf_url = clean_url(batch_meta.get('batchInfoPdfUrl', ''))
    if pdf_url:
        pdf_links.append({"name": "Batch Info PDF", "url": pdf_url, "topic": "Batch Info"})

    if classes_data and "classes" in classes_data:
        for topic_group in classes_data["classes"]:
            topic_name    = topic_group.get("topicName") or "General"
            topic_classes = []
            topic_teacher = ""

            for class_item in topic_group.get("classes", []):
                title   = class_item.get("title", "Unknown Title")
                teacher = class_item.get("teacherName") or ""
                if teacher and not topic_teacher:
                    topic_teacher = teacher

                mp4_recordings = class_item.get("mp4Recordings", []) or []
                class_link     = clean_url(class_item.get("class_link", ""))
                live_now       = is_class_live_now(class_item)

                by_quality = {r.get("quality"): clean_url(r.get("url", ""))
                              for r in mp4_recordings if r.get("url")}
                mp4_urls = [by_quality[q] for q in QUALITY_ORDER if q in by_quality]
                for q, u in by_quality.items():
                    if q not in QUALITY_ORDER and u not in mp4_urls:
                        mp4_urls.append(u)

                if mp4_urls:
                    video_type      = "mp4"
                    display_quality = next((q for q in QUALITY_ORDER if q in by_quality), "SD")
                elif live_now and class_link:
                    video_type      = "hls" if ".m3u8" in class_link else (
                        "youtube" if extract_youtube_id(class_link) else "link")
                    display_quality = "LIVE"
                elif extract_youtube_id(class_link):
                    video_type      = "youtube"
                    display_quality = "YouTube"
                elif ".m3u8" in class_link:
                    video_type      = "hls"
                    display_quality = "HLS"
                elif class_link:
                    video_type      = "link"
                    display_quality = "External"
                else:
                    video_type      = "none"
                    display_quality = ""

                # For HLS, rewrite through our proxy immediately so the Mini App
                # can use the URL directly.
                playable_url = class_link
                if video_type == "hls" and class_link:
                    playable_url = proxied_hls_url(class_link)

                video_index = len(video_links)
                video_links.append({
                    "title":    title,
                    "quality":  display_quality,
                    "video_type": video_type,
                    "mp4_urls": mp4_urls,
                    "url":      playable_url,
                    "raw_url":  class_link,
                    "live":     live_now,
                    "playable": video_type in ("mp4", "youtube", "hls"),
                })

                class_pdfs = []
                for pdf in class_item.get("classPdf", []) or []:
                    pdf_name = pdf.get("name") or title
                    pdf_url_item = pdf.get("url")
                    if pdf_url_item:
                        entry = {"name": pdf_name, "url": pdf_url_item, "topic": topic_name}
                        pdf_links.append(entry)
                        class_pdfs.append(entry)

                class_tests = []
                for test in class_item.get("classTest", []) or []:
                    test_name = test.get("name") or test.get("title") or title
                    test_url  = test.get("url") or test.get("link")
                    entry = {"name": test_name, "url": test_url, "topic": topic_name}
                    test_links.append(entry)
                    class_tests.append(entry)

                dur_s     = class_item.get("duration")
                dur_text  = f"{dur_s / 60:.0f} min" if dur_s else ""
                date_raw  = class_item.get("startDate") or class_item.get("classCreatedAt")
                date_text = ""
                if date_raw:
                    try:
                        date_text = datetime.fromisoformat(
                            date_raw.replace('Z', '+00:00')
                        ).strftime("%d %b %Y")
                    except ValueError:
                        pass

                topic_classes.append({
                    "title":        title,
                    "duration_text": dur_text,
                    "date_text":    date_text,
                    "pdfs":         class_pdfs,
                    "tests":        class_tests,
                    "video_index":  video_index,
                    "live":         live_now,
                })

            if topic_classes:
                topics.append({
                    "name":    topic_name,
                    "teacher": topic_teacher,
                    "classes": topic_classes,
                })

    return {
        "topics":      topics,
        "video_links": video_links,
        "pdf_links":   pdf_links,
        "test_links":  test_links,
    }

# ─────────────────────────────────────────────────────────────────
# Flask App
# ─────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("SESSION_SECRET", "selectionway-secret-key-2024")

# ── Proxy helpers ────────────────────────────────────────────────

def _proxy_headers(url: str) -> dict:
    return {
        **BASE_HEADERS,
        "host": urlparse(url).netloc,
        "sec-fetch-site": "cross-site",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-dest": "image",
    }

@app.route("/hls")
def hls_proxy():
    """CORS proxy for HLS playlists + segments."""
    target = request.args.get("url")
    if not target:
        return "Missing url", 400
    try:
        upstream = requests.get(
            target,
            headers={"User-Agent": BASE_HEADERS["user-agent"], "Referer": "https://www.selectionway.com/"},
            timeout=20,
            stream=True,
        )
    except requests.RequestException as e:
        return str(e), 502

    content_type = upstream.headers.get("Content-Type", "")
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }

    if target.endswith(".m3u8") or "mpegurl" in content_type:
        text = upstream.content.decode("utf-8", "ignore")
        rewritten = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                absolute = urljoin(target, stripped)
                rewritten.append(f"/hls?url={quote(absolute, safe='')}")
            else:
                rewritten.append(line)
        payload = ("\n".join(rewritten) + "\n").encode("utf-8")
        return Response(
            payload,
            status=200,
            headers={
                **cors_headers,
                "Content-Type": "application/vnd.apple.mpegurl",
                "Content-Length": str(len(payload)),
            },
        )
    else:
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            except (BrokenPipeError, ConnectionResetError):
                pass
        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers={
                **cors_headers,
                "Content-Type": content_type or "video/mp2t",
            },
        )

@app.route("/proxy-img")
def proxy_img():
    """Image proxy so thumbnails load inside the Mini App WebView."""
    target = request.args.get("url")
    if not target:
        return "Missing url", 400
    try:
        r = requests.get(
            target,
            headers=_proxy_headers(target),
            timeout=10,
            stream=True,
        )
        content_type = r.headers.get("Content-Type", "image/jpeg")
        def gen():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        return Response(stream_with_context(gen()), status=r.status_code,
                        headers={"Content-Type": content_type,
                                 "Cache-Control": "public, max-age=86400"})
    except Exception as e:
        return str(e), 502

@app.route("/proxy-video")
def proxy_video():
    """Stream an mp4 video through the server with correct Referer/User-Agent.
    Supports Range requests so the browser seek bar works."""
    target = request.args.get("url")
    if not target:
        return "Missing url", 400

    range_header = request.headers.get("Range")
    req_headers = {
        "User-Agent": BASE_HEADERS["user-agent"],
        "Referer":    "https://www.selectionway.com/",
        "Origin":     "https://www.selectionway.com",
    }
    if range_header:
        req_headers["Range"] = range_header

    try:
        upstream = requests.get(target, headers=req_headers, stream=True, timeout=10)
    except Exception as e:
        return str(e), 502

    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": "bytes",
    }
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            resp_headers[h] = upstream.headers[h]

    def gen():
        try:
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        except (BrokenPipeError, ConnectionResetError):
            pass

    return Response(
        stream_with_context(gen()),
        status=upstream.status_code,
        headers=resp_headers,
    )

# ── Mini App API ─────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("miniapp.html")

@app.route("/api/batches")
def api_batches():
    ok, data = sw_get_all_batches()
    if not ok:
        return jsonify({"error": data}), 500
    sorted_data = sort_batches_by_recency(data)
    # Slim down payload (keep only what the Mini App needs)
    slim = []
    for b in sorted_data:
        cat = (b.get("mainCategory") or {}).get("mainCategoryName", "")
        thumb = b.get("bannerSquare") or b.get("banner") or ""
        if thumb:
            thumb = f"/proxy-img?url={quote(thumb, safe='')}"
        live_now = is_batch_live_now(b)
        slim.append({
            "id":          b.get("id"),
            "title":       b.get("title", ""),
            "category":    cat,
            "short_desc":  b.get("short_description", ""),
            "price":       b.get("discountPrice") or b.get("price") or "",
            "thumb":       thumb,
            "isLive":      bool(b.get("isLive")),
            "liveNow":     live_now,
            "validity":    b.get("validity", ""),
            "totalClass":  b.get("liveClassesCount", 0),
            "faculty":     (b.get("facultyDetails") or {}).get("name", ""),
        })
    return jsonify(slim)

@app.route("/api/batch/<batch_id>/classes")
def api_batch_classes(batch_id):
    # Get classes
    ok, classes_data = sw_get_batch_classes(batch_id)
    if not ok:
        return jsonify({"error": classes_data}), 500

    # Get batch meta from the global list
    ok2, all_batches = sw_get_all_batches()
    batch_meta = {}
    if ok2:
        for b in all_batches:
            if b.get("id") == batch_id:
                batch_meta = b
                break

    result = build_classes_data(classes_data, batch_meta)

    # Proxy thumbnails in batch_meta before sending
    thumb = batch_meta.get("bannerSquare") or batch_meta.get("banner") or ""
    if thumb:
        thumb = f"/proxy-img?url={quote(thumb, safe='')}"

    cat = (batch_meta.get("mainCategory") or {}).get("mainCategoryName", "")
    desc_items = batch_meta.get("description") or []
    if isinstance(desc_items, str):
        desc_items = [desc_items]
    highlights = batch_meta.get("courseHighlights") or []

    result["batch_meta"] = {
        "id":          batch_id,
        "title":       batch_meta.get("title", ""),
        "category":    cat,
        "thumb":       thumb,
        "price":       batch_meta.get("discountPrice") or batch_meta.get("price") or "",
        "validity":    batch_meta.get("validity", ""),
        "faculty":     (batch_meta.get("facultyDetails") or {}).get("name", ""),
        "short_desc":  batch_meta.get("short_description", ""),
        "description": desc_items[:6],
        "highlights":  highlights[:5],
    }

    return jsonify(result)

# ── Admin portal ─────────────────────────────────────────────────

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == ADMIN_WEB_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_home"))
        error = "Invalid password"
    return render_template("admin_login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_required
def admin_home():
    stats = db_stats()
    return render_template("admin.html", stats=stats)

@app.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    return jsonify(db_stats())

@app.route("/api/admin/users")
@admin_required
def api_admin_users():
    page = int(request.args.get("page", 0))
    PAGE = 10
    total = db_count_users()
    rows  = db_all_users(limit=PAGE, offset=page * PAGE)
    users = []
    for u in rows:
        limit_str = f"{u['fetch_limit']} {PERIOD_LABELS.get(u['fetch_period'], u['fetch_period'])}"
        users.append({
            "user_id":    u["user_id"],
            "username":   u["username"] or "",
            "first_name": u["first_name"] or "",
            "is_banned":  bool(u["is_banned"]),
            "is_admin":   is_admin(u["user_id"]),
            "limit_str":  limit_str,
            "fetch_limit":  u["fetch_limit"],
            "fetch_period": u["fetch_period"],
            "joined_at":  (u["joined_at"] or "")[:10],
        })
    return jsonify({"users": users, "total": total, "page": page, "page_size": PAGE})

@app.route("/api/admin/user/<int:uid>")
@admin_required
def api_admin_user(uid):
    u = db_get_user(uid)
    if not u:
        return jsonify({"error": "User not found"}), 404
    hist = db_user_fetch_history(uid, 20)
    history = [{"batch_name": r["batch_name"], "fetched_at": r["fetched_at"][:16]} for r in hist]
    return jsonify({
        "user_id":    u["user_id"],
        "username":   u["username"] or "",
        "first_name": u["first_name"] or "",
        "is_banned":  bool(u["is_banned"]),
        "is_admin":   is_admin(u["user_id"]),
        "fetch_limit":  u["fetch_limit"],
        "fetch_period": u["fetch_period"],
        "joined_at":  (u["joined_at"] or "")[:10],
        "history":    history,
    })

@app.route("/api/admin/ban", methods=["POST"])
@admin_required
def api_admin_ban():
    data = request.get_json()
    uid  = int(data.get("user_id", 0))
    ban  = bool(data.get("banned", True))
    db_set_banned(uid, ban)
    return jsonify({"ok": True, "banned": ban})

@app.route("/api/admin/limit", methods=["POST"])
@admin_required
def api_admin_limit():
    data   = request.get_json()
    uid    = int(data.get("user_id", 0))
    limit  = int(data.get("limit", 1))
    period = data.get("period", "day")
    if period not in ("day", "week", "month", "unlimited"):
        return jsonify({"error": "Invalid period"}), 400
    count = 999999 if period == "unlimited" else limit
    db_set_limit(uid, count, period)
    return jsonify({"ok": True})

# ─────────────────────────────────────────────────────────────────
# Telegram Bot helpers
# ─────────────────────────────────────────────────────────────────

BATCH_LINK_ID_RE = re.compile(r'/batches/[^/]+/([a-fA-F0-9]{16,32})')
RAW_ID_RE        = re.compile(r'^[a-fA-F0-9]{16,32}$')

def extract_batch_id(text: str):
    text = text.strip()
    m = BATCH_LINK_ID_RE.search(text)
    if m:
        return m.group(1)
    if RAW_ID_RE.match(text):
        return text
    return None

def admin_main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users",  callback_data="adm:users:0"),
         InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
    ])

def admin_user_kb(uid: int, is_banned_flag: bool):
    ban_label = "✅ Unban" if is_banned_flag else "🚫 Ban"
    ban_cb    = f"adm:unban:{uid}" if is_banned_flag else f"adm:ban:{uid}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ban_label,       callback_data=ban_cb),
         InlineKeyboardButton("⚙️ Set Limit", callback_data=f"adm:setlimit:{uid}")],
        [InlineKeyboardButton("🕓 History",    callback_data=f"adm:history:{uid}"),
         InlineKeyboardButton("« Back",        callback_data="adm:users:0")],
    ])

def fmt_user_row(u):
    name = u["first_name"] or u["username"] or f"uid:{u['user_id']}"
    badge = " 🚫" if u["is_banned"] else (" 👑" if is_admin(u["user_id"]) else "")
    limit_str = f"{u['fetch_limit']}/{PERIOD_LABELS.get(u['fetch_period'], u['fetch_period'])}"
    return f"*{name}*{badge} — `{u['user_id']}` — {limit_str}"

async def send_long(target, text, parse_mode="Markdown"):
    lines = text.split("\n")
    chunks, current = [], ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MAX_MESSAGE_LENGTH:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for chunk in chunks:
        await target.reply_text(chunk, parse_mode=parse_mode)

# ─────────────────────────────────────────────────────────────────
# Telegram Handlers
# ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = user.id
    db_ensure_user(uid, user.username or "", user.first_name or "")

    admin_flag = is_admin(uid)
    name_md    = user.first_name or "there"
    webapp_url = get_webapp_url()

    if admin_flag:
        greeting = (
            f"👑 *Welcome back, Admin {name_md}!*\n\n"
            f"You have full access to the bot and admin controls.\n"
            f"Use /admin to open the admin panel."
        )
    else:
        greeting = (
            f"👋 *Hello, {name_md}!* Welcome to *SelectionWay Bot*\n\n"
            f"Browse every SelectionWay batch, explore topics, and watch lectures right here in Telegram.\n\n"
            f"📌 *How to use:*\n"
            f"  • Tap *Open Course Browser* below to launch the Mini App\n"
            f"  • Browse batches → topics → lectures → play video\n"
            f"  • Or paste a batch link to get a full HTML file"
        )

    kb_rows = [
        [InlineKeyboardButton(
            "📚 Open Course Browser",
            web_app=WebAppInfo(url=webapp_url)
        )],
    ]
    if admin_flag:
        kb_rows.append([InlineKeyboardButton("🛡 Admin Panel", callback_data="adm:menu")])
        kb_rows.append([InlineKeyboardButton("🌐 Web Admin Portal",
                                              web_app=WebAppInfo(url=f"{webapp_url}/admin"))])

    await update.message.reply_text(
        greeting,
        reply_markup=InlineKeyboardMarkup(kb_rows),
        parse_mode="Markdown",
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return
    s = db_stats()
    await update.message.reply_text(
        f"🛡 *Admin Panel*\n\n"
        f"👥 Total users: *{s['total']}*\n"
        f"🚫 Banned: *{s['banned']}*\n"
        f"📦 Total fetches: *{s['fetches']}*\n"
        f"📅 Fetches (last 24 h): *{s['today']}*",
        reply_markup=admin_main_kb(),
        parse_mode="Markdown",
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data

    if not data.startswith("adm:"):
        return

    if not is_admin(uid):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    parts = data.split(":")

    if parts[1] == "menu":
        s = db_stats()
        await query.edit_message_text(
            f"🛡 *Admin Panel*\n\n"
            f"👥 Total users: *{s['total']}*\n"
            f"🚫 Banned: *{s['banned']}*\n"
            f"📦 Total fetches: *{s['fetches']}*\n"
            f"📅 Fetches (last 24 h): *{s['today']}*",
            reply_markup=admin_main_kb(),
            parse_mode="Markdown",
        )

    elif parts[1] == "stats":
        s = db_stats()
        await query.edit_message_text(
            f"📊 *Bot Statistics*\n\n"
            f"👥 Total users: *{s['total']}*\n"
            f"🚫 Banned: *{s['banned']}*\n"
            f"📦 Total fetches ever: *{s['fetches']}*\n"
            f"📅 Fetches (last 24 h): *{s['today']}*",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="adm:menu")]]),
            parse_mode="Markdown",
        )

    elif parts[1] == "users":
        page = int(parts[2]) if len(parts) > 2 else 0
        PAGE = 10
        users = db_all_users(limit=PAGE * 10, offset=0)   # fetch enough for display
        total = db_count_users()
        chunk = list(db_all_users(limit=PAGE, offset=page * PAGE))
        lines = [f"{page*PAGE + i + 1}. {fmt_user_row(u)}" for i, u in enumerate(chunk)]
        nav   = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"adm:users:{page-1}"))
        if (page + 1) * PAGE < total:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"adm:users:{page+1}"))
        user_btns = [
            [InlineKeyboardButton(
                f"👤 {u['first_name'] or u['user_id']}",
                callback_data=f"adm:user:{u['user_id']}"
            )]
            for u in chunk
        ]
        kb = user_btns + ([nav] if nav else []) + [[InlineKeyboardButton("« Back", callback_data="adm:menu")]]
        await query.edit_message_text(
            f"👥 *Users* (page {page+1} / {max(1, -(-total//PAGE))})\n\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )

    elif parts[1] == "user":
        target_uid = int(parts[2])
        u = db_get_user(target_uid)
        if not u:
            await query.answer("User not found.", show_alert=True)
            return
        hist = db_user_fetch_history(target_uid, 5)
        hist_lines = "\n".join(f"  • {r['batch_name']} ({r['fetched_at'][:10]})" for r in hist) or "  (none yet)"
        limit_str  = f"{u['fetch_limit']} {PERIOD_LABELS.get(u['fetch_period'], u['fetch_period'])}"
        status     = "🚫 Banned" if u["is_banned"] else "✅ Active"
        await query.edit_message_text(
            f"👤 *User Profile*\n\n"
            f"Name: {u['first_name']} (@{u['username'] or '-'})\n"
            f"ID: `{u['user_id']}`\n"
            f"Status: {status}\n"
            f"Fetch limit: {limit_str}\n"
            f"Joined: {(u['joined_at'] or '')[:10]}\n\n"
            f"📦 Recent fetches:\n{hist_lines}",
            reply_markup=admin_user_kb(target_uid, bool(u["is_banned"])),
            parse_mode="Markdown",
        )

    elif parts[1] in ("ban", "unban"):
        target_uid = int(parts[2])
        do_ban = parts[1] == "ban"
        db_set_banned(target_uid, do_ban)
        u      = db_get_user(target_uid)
        hist   = db_user_fetch_history(target_uid, 5)
        hist_lines = "\n".join(f"  • {r['batch_name']} ({r['fetched_at'][:10]})" for r in hist) or "  (none yet)"
        limit_str  = f"{u['fetch_limit']} {PERIOD_LABELS.get(u['fetch_period'], u['fetch_period'])}"
        status     = "🚫 Banned" if u["is_banned"] else "✅ Active"
        await query.answer(f"User {'banned 🚫' if do_ban else 'unbanned ✅'}.")
        await query.edit_message_text(
            f"👤 *User Profile*\n\n"
            f"Name: {u['first_name']} (@{u['username'] or '-'})\n"
            f"ID: `{u['user_id']}`\n"
            f"Status: {status}\n"
            f"Fetch limit: {limit_str}\n"
            f"Joined: {(u['joined_at'] or '')[:10]}\n\n"
            f"📦 Recent fetches:\n{hist_lines}",
            reply_markup=admin_user_kb(target_uid, bool(u["is_banned"])),
            parse_mode="Markdown",
        )

    elif parts[1] == "history":
        target_uid = int(parts[2])
        hist  = db_user_fetch_history(target_uid, 20)
        lines = "\n".join(f"• {r['batch_name']} — {r['fetched_at'][:16]}" for r in hist) or "(none)"
        await query.edit_message_text(
            f"🕓 *Fetch History* for `{target_uid}`\n\n{lines}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"adm:user:{target_uid}")]]),
            parse_mode="Markdown",
        )

    elif parts[1] == "setlimit":
        target_uid = int(parts[2])
        context.user_data["admin_setting_limit_for"] = target_uid
        await query.edit_message_text(
            f"⚙️ *Set fetch limit for user* `{target_uid}`\n\n"
            f"Send a message in this format:\n"
            f"`<count> <period>`\n\n"
            f"Examples:\n"
            f"  `1 day`  — 1 fetch per day\n"
            f"  `3 week` — 3 per week\n"
            f"  `5 month` — 5 per month\n"
            f"  `0 unlimited` — no limit\n",
            parse_mode="Markdown",
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    uid     = user.id
    text    = update.message.text
    db_ensure_user(uid, user.username or "", user.first_name or "")

    # Admin setting a limit
    if context.user_data.get("admin_setting_limit_for") and is_admin(uid):
        target_uid = context.user_data.pop("admin_setting_limit_for")
        parts = text.strip().lower().split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1] in {"day", "week", "month", "unlimited"}:
            count  = int(parts[0])
            period = parts[1]
            if period == "unlimited":
                count = 999999
            db_set_limit(target_uid, count, period)
            await update.message.reply_text(
                f"✅ Limit for `{target_uid}` set to *{count} {PERIOD_LABELS.get(period, period)}*.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ Invalid format. Use: `<count> <period>` — e.g. `2 day` or `0 unlimited`",
                parse_mode="Markdown",
            )
        return

    # Check rate limit
    allowed, rl_msg = check_rate_limit(uid)
    if not allowed:
        await update.message.reply_text(rl_msg)
        return

    # Batch link / raw ID?
    batch_id = extract_batch_id(text)
    if batch_id:
        # Log and respond with Mini App link
        webapp_url = get_webapp_url()
        await update.message.reply_text(
            f"📚 Opening batch in the Mini App…",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("▶ Open Batch", web_app=WebAppInfo(url=f"{webapp_url}/#batch={batch_id}"))
            ]]),
        )
        db_log_fetch(uid, batch_id, "direct-link")
        return

    # Otherwise suggest the Mini App
    webapp_url = get_webapp_url()
    await update.message.reply_text(
        "📚 Use the *Course Browser* to browse batches and play lectures:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📚 Open Course Browser", web_app=WebAppInfo(url=webapp_url))
        ]]),
        parse_mode="Markdown",
    )


async def post_init(application):
    commands = [
        BotCommand("start", "Browse all batches & launch Mini App"),
        BotCommand("admin", "Admin panel (admins only)"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands registered.")


# ─────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────

def run_bot():
    """Run the Telegram bot in its own thread (with its own event loop)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Telegram bot polling…")
    application.run_polling(close_loop=False)


def main():
    init_db()
    logger.info("DB initialised.")

    # Run the Telegram bot in a background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    logger.info("Bot thread started.")

    # Run Flask in the main thread
    logger.info(f"Flask web server starting on port {PORT}…")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
