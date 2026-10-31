#!/usr/bin/env python3
"""5san — anonim imageboard (Flask + SQLite + Pillow).

Çalıştırma:   python app.py
Üretim:       waitress-serve --listen=0.0.0.0:5000 app:app
Ortam değişkenleri:
  HOST, PORT          sunucu adresi (varsayılan 127.0.0.1:5000)
  FIVESAN_DATA        veri klasörü (varsayılan ./data)
  FLASK_SECRET_KEY    oturum/IP-hash anahtarı (yoksa data/secret.key otomatik üretilir)
  ADMIN_PASSWORD      yönetici şifresi (yoksa data/admin.pass otomatik üretilir)
  TRUST_PROXY=1       nginx/Cloudflare arkasındaysan gerçek IP için
  HTTPS=1             çerezleri yalnızca HTTPS'te gönder
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import (Flask, abort, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from markupsafe import Markup, escape
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

# --------------------------------------------------------------------------
# Ayarlar
# --------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("FIVESAN_DATA", BASE / "data"))
UPLOADS, THUMBS, LOGS = DATA / "uploads", DATA / "thumbs", DATA / "logs"
for _d in (UPLOADS, THUMBS, LOGS):
    _d.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "5san.db"

# slug: (ad, açıklama, görselsiz başlık açılabilir mi)
BOARDS = {
    "b":       {"name": "Genel",     "desc": "Her şey burada",              "text": False},
    "tech":    {"name": "Teknoloji", "desc": "Yazılım, donanım, internet",  "text": False},
    "games":   {"name": "Oyun",      "desc": "Her platformdan oyunlar",     "text": False},
    "meme":    {"name": "Mizah",     "desc": "Meme ve komik içerik",        "text": False},
    "siyaset": {"name": "Siyaset",   "desc": "Tartışma, haber, gündem",     "text": True},
    "dizi":    {"name": "Dizi",      "desc": "Dizi ve film",                "text": False},
    "anime":   {"name": "Anime",     "desc": "Anime ve manga",              "text": False},
    "soc":     {"name": "Sohbet",    "desc": "Sadece muhabbet",             "text": True},
    "story":   {"name": "Hikâye",    "desc": "Yazı, hikâye, yaratıcı içerik", "text": True},
    "diger":   {"name": "Diğer",     "desc": "Başka hiçbir yere uymayanlar", "text": False},
}

# Ana sayfadaki kategori sütunları
CATEGORIES = [
    ("Genel", ["b", "soc", "diger"]),
    ("Eğlence", ["meme", "games", "dizi", "anime"]),
    ("İlgi alanları", ["tech", "story"]),
    ("Tartışma", ["siyaset"]),
]

THREADS_PER_PAGE = 10
PREVIEW_REPLIES = 5
MAX_THREADS = 100          # tahta başına; fazlası en eski (bump) olandan silinir
BUMP_LIMIT = 300           # bundan sonra yanıtlar başlığı yukarı çıkarmaz
MAX_REPLIES = 500          # bundan sonra başlık dolar
MAX_BODY = 3000
MAX_IMG_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_DIM = 8000
THUMB_SIZE = 250
POST_COOLDOWN = 8          # sn, aynı kişi (herhangi bir gönderi)
THREAD_COOLDOWN = 60       # sn, aynı kişi (yeni başlık)
FLOOD_LIMIT = 30           # 10 dk içinde en fazla gönderi
MAX_LINKS = 4
REPORT_REASONS = ["Hukuka aykırı içerik", "Spam", "Kişisel bilgi", "Diğer"]
TZ = timezone(timedelta(hours=3))  # Türkiye saati

Image.MAX_IMAGE_PIXELS = MAX_PIXELS * 2


def _load_or_create(path: Path, make) -> str:
    if path.exists():
        return path.read_text().strip()
    value = make()
    path.write_text(value)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


SECRET = os.environ.get("FLASK_SECRET_KEY") or _load_or_create(
    DATA / "secret.key", lambda: secrets.token_hex(32))


def _admin_password() -> str:
    env = os.environ.get("ADMIN_PASSWORD")
    if env:
        return env
    p = DATA / "admin.pass"
    fresh = not p.exists()
    pw = _load_or_create(p, lambda: secrets.token_urlsafe(12))
    if fresh:
        print(f"[5san] Yönetici şifresi üretildi: {pw}   (dosya: {p})")
    return pw


ADMIN_PW = _admin_password()

# --------------------------------------------------------------------------
# Uygulama
# --------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = SECRET
app.config.update(
    MAX_CONTENT_LENGTH=MAX_IMG_BYTES + 512 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    TEMPLATES_AUTO_RELOAD=False,
)
if os.environ.get("TRUST_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_handler = RotatingFileHandler(LOGS / "app.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
app.logger.addHandler(_handler)
app.logger.setLevel(logging.INFO)


class PostError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message, self.code = message, code


# --------------------------------------------------------------------------
# Veritabanı
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  board      TEXT NOT NULL,
  parent_id  INTEGER REFERENCES posts(id) ON DELETE CASCADE,
  name       TEXT NOT NULL DEFAULT '',
  trip       TEXT NOT NULL DEFAULT '',
  subject    TEXT NOT NULL DEFAULT '',
  body       TEXT NOT NULL DEFAULT '',
  ip_hash    TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  bumped_at  INTEGER NOT NULL,
  sticky     INTEGER NOT NULL DEFAULT 0,
  locked     INTEGER NOT NULL DEFAULT 0,
  img_file   TEXT, img_thumb TEXT, img_w INTEGER, img_h INTEGER,
  img_size   INTEGER, img_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_threads ON posts(board, parent_id, sticky DESC, bumped_at DESC);
CREATE INDEX IF NOT EXISTS idx_replies ON posts(parent_id, id);
CREATE INDEX IF NOT EXISTS idx_ip      ON posts(ip_hash, created_at);
CREATE TABLE IF NOT EXISTS reports (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  reason     TEXT NOT NULL,
  ip_hash    TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_ip ON reports(ip_hash, created_at);
CREATE TABLE IF NOT EXISTS bans (
  ip_hash    TEXT PRIMARY KEY,
  until      INTEGER NOT NULL DEFAULT 0,
  reason     TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    # isolation_level=None: otomatik commit; yazma bölümlerinde BEGIN IMMEDIATE elle kullanılır.
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db() -> None:
    conn = connect()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version = 2")
    conn.close()


def db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def _close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


init_db()

# --------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


def clean(s: str) -> str:
    return _CTRL.sub("", s or "")


def clean_body(s: str) -> str:
    s = clean(s).replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n{4,}", "\n\n\n", s)


def ip_hash() -> str:
    ip = request.remote_addr or "0.0.0.0"
    return hmac.new(SECRET.encode(), ip.encode(), hashlib.sha256).hexdigest()[:20]


def poster_id(h: str, tid: int) -> str:
    return hashlib.sha256(f"{h}:{tid}:{SECRET}".encode()).hexdigest()[:8]


def idcolor(pid: str) -> str:
    r, g_, b = (int(pid[i:i + 2], 16) for i in (0, 2, 4))
    fg = "#000" if (0.299 * r + 0.587 * g_ + 0.114 * b) > 140 else "#fff"
    return f"background:#{pid[:6]};color:{fg}"


def parse_name(raw: str) -> tuple[str, str]:
    raw = clean(raw)[:60]
    trip = ""
    if "#" in raw:
        raw, secret = raw.split("#", 1)
        digest = hmac.new(SECRET.encode(), secret.encode(), hashlib.sha256).digest()
        trip = "!" + base64.urlsafe_b64encode(digest)[:10].decode()
    return raw.strip()[:24], trip


def need_board(slug: str) -> dict:
    if slug not in BOARDS:
        abort(404)
    return BOARDS[slug]


def remove_files(*names) -> None:
    for n in names:
        if not n:
            continue
        for folder in (UPLOADS, THUMBS):
            try:
                (folder / n).unlink(missing_ok=True)
            except OSError:
                app.logger.warning("dosya silinemedi: %s", n)


def delete_post(conn: sqlite3.Connection, pid: int) -> bool:
    """Gönderiyi (OP ise tüm başlığı) ve görsellerini siler. OP silindiyse True döner."""
    row = conn.execute("SELECT id, parent_id FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return False
    files = conn.execute(
        "SELECT img_file, img_thumb FROM posts WHERE id=? OR parent_id=?", (pid, pid)).fetchall()
    conn.execute("DELETE FROM posts WHERE id=?", (pid,))  # yanıtlar CASCADE ile gider
    for f in files:
        remove_files(f["img_file"], f["img_thumb"])
    return row["parent_id"] is None


def prune(conn: sqlite3.Connection, board: str) -> None:
    old = conn.execute(
        "SELECT id FROM posts WHERE board=? AND parent_id IS NULL AND sticky=0 "
        "ORDER BY bumped_at DESC LIMIT -1 OFFSET ?", (board, MAX_THREADS)).fetchall()
    for r in old:
        delete_post(conn, r["id"])


def check_ban(conn: sqlite3.Connection, h: str, now: int) -> None:
    r = conn.execute("SELECT * FROM bans WHERE ip_hash=?", (h,)).fetchone()
    if not r:
        return
    if r["until"] and r["until"] <= now:
        conn.execute("DELETE FROM bans WHERE ip_hash=?", (h,))
        return
    until = "kalıcı" if not r["until"] else "bitiş: " + datetime.fromtimestamp(r["until"], TZ).strftime("%d/%m/%y %H:%M")
    raise PostError(f"Yasaklısın ({until}). Sebep: {r['reason'] or 'belirtilmedi'}", 403)


def save_image(f) -> dict:
    raw = f.read(MAX_IMG_BYTES + 1)
    if len(raw) > MAX_IMG_BYTES:
        raise PostError(f"Görsel en fazla {MAX_IMG_BYTES // 1024 // 1024} MB olabilir.")
    try:
        probe = Image.open(io.BytesIO(raw))
        fmt = probe.format
        probe.verify()
        img = Image.open(io.BytesIO(raw))
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise PostError("Geçersiz veya bozuk görsel dosyası.")
    exts = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}
    if fmt not in exts:
        raise PostError("Sadece JPG, PNG, GIF ve WEBP yüklenebilir.")
    if img.width * img.height > MAX_PIXELS or max(img.size) > MAX_DIM:
        raise PostError("Görsel çözünürlüğü çok yüksek.")

    base = f"{time.time_ns() // 1_000_000}{secrets.token_hex(3)}"
    animated = getattr(img, "is_animated", False)
    try:
        if animated:
            data, (w, h) = raw, img.size  # animasyonu bozmamak için orijinal
        else:
            # Yeniden kaydetmek EXIF/GPS gibi tüm üst veriyi temizler.
            clean_img = ImageOps.exif_transpose(img)
            if fmt == "JPEG" and clean_img.mode not in ("RGB", "L"):
                clean_img = clean_img.convert("RGB")
            buf = io.BytesIO()
            opts = {"quality": 90, "optimize": True} if fmt == "JPEG" else \
                   {"optimize": True} if fmt == "PNG" else {"quality": 90}
            clean_img.save(buf, fmt, **opts)
            data, (w, h) = buf.getvalue(), clean_img.size

        thumb = ImageOps.exif_transpose(img)
        if thumb.mode in ("RGBA", "LA", "P"):
            thumb = thumb.convert("RGBA")
            bg = Image.new("RGB", thumb.size, (20, 18, 29))
            bg.paste(thumb, mask=thumb.split()[-1])
            thumb = bg
        else:
            thumb = thumb.convert("RGB")
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))

        file_name, thumb_name = f"{base}.{exts[fmt]}", f"{base}.jpg"
        (UPLOADS / file_name).write_bytes(data)
        thumb.save(THUMBS / thumb_name, "JPEG", quality=82)
    except (OSError, ValueError, SyntaxError):
        raise PostError("Görsel işlenemedi.")

    orig = re.sub(r"[^\w.\- ]", "", Path(f.filename or "image").name, flags=re.UNICODE)[-40:] or "image"
    return {"file": file_name, "thumb": thumb_name, "w": w, "h": h, "size": len(data), "name": orig}


# --------------------------------------------------------------------------
# Şablon filtreleri
# --------------------------------------------------------------------------
_QUOTE = re.compile(r"&gt;&gt;(\d{1,10})")
_URL = re.compile(r"https?://[^\s]+?(?=$|\s|&lt;|&gt;|&#34;|&#39;)")
_SPOILER = re.compile(r"\[spoiler\](.+?)\[/spoiler\]", re.S)


@app.template_filter("render_body")
def render_body(body: str) -> Markup:
    out = []
    for line in body.split("\n"):
        green = line.startswith(">") and not line.startswith(">>")
        html = str(escape(line))
        html = _QUOTE.sub(lambda m: f'<a class="quotelink" href="{url_for("go", pid=int(m.group(1)))}">&gt;&gt;{m.group(1)}</a>', html)
        html = _URL.sub(lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener noreferrer nofollow ugc">{m.group(0)}</a>', html)
        html = _SPOILER.sub(r'<s class="sp">\1</s>', html)
        out.append(f'<span class="gt">{html}</span>' if green else html)
    return Markup("\n".join(out))


@app.template_filter("dt")
def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d/%m/%y %H:%M:%S")


@app.template_filter("fsize")
def fmt_size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


@app.context_processor
def inject():
    return dict(boards=BOARDS, csrf_token=lambda: session.get("csrf", ""),
                is_admin=bool(session.get("admin")), poster_id=poster_id, idcolor=idcolor,
                MAX_BODY=MAX_BODY, MAX_IMG_MB=MAX_IMG_BYTES // 1024 // 1024)


def backlinks(posts) -> dict:
    m: dict[int, list[int]] = {}
    for p in posts:
        for t in set(re.findall(r">>(\d{1,10})", p["body"])):
            m.setdefault(int(t), []).append(p["id"])
    return m


# --------------------------------------------------------------------------
# Güvenlik: CSRF, başlıklar, hata sayfaları
# --------------------------------------------------------------------------
@app.before_request
def csrf_protect():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not hmac.compare_digest(sent, session["csrf"]):
            raise PostError("Oturum doğrulaması başarısız. Sayfayı yenileyip tekrar dene.")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return resp


HTTP_MESSAGES = {400: "Geçersiz istek.", 403: "Erişim yok.", 404: "Sayfa bulunamadı.",
                 405: "Bu işlem desteklenmiyor.", 413: f"Dosya çok büyük (en fazla {MAX_IMG_BYTES // 1024 // 1024} MB görsel).",
                 429: "Çok fazla istek. Biraz bekle."}


@app.errorhandler(PostError)
def _post_error(e: PostError):
    return render_template("error.html", title="Hata", message=e.message, code=e.code), e.code


@app.errorhandler(HTTPException)
def _http_error(e: HTTPException):
    msg = HTTP_MESSAGES.get(e.code, e.description)
    return render_template("error.html", title=f"Hata {e.code}", message=msg, code=e.code), e.code


@app.errorhandler(Exception)
def _crash(e: Exception):
    app.logger.exception("beklenmeyen hata: %s", e)
    return render_template("error.html", title="Hata 500", message="Sunucuda bir sorun oldu.", code=500), 500


# --------------------------------------------------------------------------
# Sayfalar
# --------------------------------------------------------------------------
@app.get("/")
def home():
    conn = db()
    stats = {r["board"]: r for r in conn.execute(
        "SELECT board, SUM(parent_id IS NULL) AS threads, COUNT(*) AS posts FROM posts GROUP BY board")}
    popular = conn.execute(
        "SELECT p.id, p.board, p.subject, p.body, p.img_thumb, "
        "(SELECT COUNT(*) FROM posts r WHERE r.parent_id=p.id) AS replies "
        "FROM posts p WHERE p.parent_id IS NULL AND p.img_thumb IS NOT NULL "
        "ORDER BY replies DESC, p.bumped_at DESC LIMIT 6").fetchall()
    return render_template("home.html", stats=stats, popular=popular, categories=CATEGORIES)


@app.get("/<board>/", defaults={"page": 1})
@app.get("/<board>/<int:page>")
def board_view(board, page):
    cfg = need_board(board)
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM posts WHERE board=? AND parent_id IS NULL", (board,)).fetchone()[0]
    pages = max(1, math.ceil(total / THREADS_PER_PAGE))
    if page < 1 or page > pages:
        abort(404)
    ops = conn.execute(
        "SELECT * FROM posts WHERE board=? AND parent_id IS NULL "
        "ORDER BY sticky DESC, bumped_at DESC LIMIT ? OFFSET ?",
        (board, THREADS_PER_PAGE, (page - 1) * THREADS_PER_PAGE)).fetchall()
    threads = []
    for op in ops:
        n = conn.execute("SELECT COUNT(*) FROM posts WHERE parent_id=?", (op["id"],)).fetchone()[0]
        tail = conn.execute(
            "SELECT * FROM (SELECT * FROM posts WHERE parent_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (op["id"], PREVIEW_REPLIES)).fetchall()
        threads.append({"op": op, "replies": tail, "omitted": n - len(tail),
                        "back": backlinks([op] + list(tail))})
    return render_template("board.html", board=board, cfg=cfg, threads=threads, page=page, pages=pages)


@app.get("/<board>/catalog")
def catalog(board):
    cfg = need_board(board)
    q = clean(request.args.get("q", "")).strip()[:60]
    sql = ("SELECT p.*, (SELECT COUNT(*) FROM posts r WHERE r.parent_id=p.id) AS replies, "
           "(SELECT COUNT(*) FROM posts r WHERE r.parent_id=p.id AND r.img_file IS NOT NULL) AS images "
           "FROM posts p WHERE p.board=? AND p.parent_id IS NULL")
    params: list = [board]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += " AND (p.subject LIKE ? ESCAPE '\\' OR p.body LIKE ? ESCAPE '\\')"
        params += [like, like]
    sql += " ORDER BY p.sticky DESC, p.bumped_at DESC LIMIT 200"
    return render_template("catalog.html", board=board, cfg=cfg, threads=db().execute(sql, params).fetchall(), q=q)


@app.get("/<board>/thread/<int:tid>")
def thread_view(board, tid):
    cfg = need_board(board)
    conn = db()
    op = conn.execute("SELECT * FROM posts WHERE id=? AND board=? AND parent_id IS NULL", (tid, board)).fetchone()
    if not op:
        abort(404)
    replies = conn.execute("SELECT * FROM posts WHERE parent_id=? ORDER BY id", (tid,)).fetchall()
    return render_template("thread.html", board=board, cfg=cfg, op=op, replies=replies,
                           back=backlinks([op] + list(replies)))


@app.get("/go/<int:pid>")
def go(pid):
    r = db().execute("SELECT id, board, parent_id FROM posts WHERE id=?", (pid,)).fetchone()
    if not r:
        abort(404)
    tid = r["parent_id"] or r["id"]
    return redirect(url_for("thread_view", board=r["board"], tid=tid) + f"#p{pid}")


FILE_RE = re.compile(r"^\d{10,}[0-9a-f]{6}\.(?:jpg|png|gif|webp)$")


@app.get("/uploads/<name>")
def uploaded(name):
    if not FILE_RE.match(name):
        abort(404)
    return send_from_directory(UPLOADS, name, max_age=31536000)


@app.get("/thumbs/<name>")
def thumb(name):
    if not FILE_RE.match(name):
        abort(404)
    return send_from_directory(THUMBS, name, max_age=31536000)


# --------------------------------------------------------------------------
# Gönderi oluşturma
# --------------------------------------------------------------------------
@app.post("/<board>/post")
def new_thread(board):
    return handle_post(board, None)


@app.post("/<board>/thread/<int:tid>/reply")
def reply(board, tid):
    return handle_post(board, tid)


def handle_post(board: str, tid: int | None):
    cfg = need_board(board)
    conn = db()
    if request.form.get("website"):  # honeypot: botlar doldurur, sessizce yut
        return redirect(url_for("board_view", board=board))
    h, now = ip_hash(), int(time.time())
    check_ban(conn, h, now)

    parent, n = None, 0
    if tid is not None:
        parent = conn.execute("SELECT * FROM posts WHERE id=? AND board=? AND parent_id IS NULL", (tid, board)).fetchone()
        if not parent:
            abort(404)
        if parent["locked"]:
            raise PostError("Bu başlık kilitli.")
        n = conn.execute("SELECT COUNT(*) FROM posts WHERE parent_id=?", (tid,)).fetchone()[0]
        if n >= MAX_REPLIES:
            raise PostError("Bu başlık yanıt sınırına ulaştı. Yeni başlık aç.")

    name, trip = parse_name(request.form.get("name", ""))
    subject = clean(request.form.get("subject", "")).strip()[:80]
    body = clean_body(request.form.get("body", ""))
    sage = bool(request.form.get("sage"))
    f = request.files.get("image")
    has_file = bool(f and f.filename)

    if len(body) > MAX_BODY:
        raise PostError(f"Mesaj en fazla {MAX_BODY} karakter olabilir.")
    if body.count("\n") > 60:
        raise PostError("Mesaj çok fazla satır içeriyor.")
    if len(re.findall(r"https?://", body)) > MAX_LINKS:
        raise PostError(f"En fazla {MAX_LINKS} bağlantı koyabilirsin.")
    if not body and not has_file:
        raise PostError("Boş gönderi gönderilemez.")
    if tid is None and not has_file and not cfg["text"]:
        raise PostError("Bu tahtada yeni başlık için görsel gerekli.")

    # Hız sınırları (kişi başına, IP hash üzerinden)
    last = conn.execute("SELECT MAX(created_at) FROM posts WHERE ip_hash=?", (h,)).fetchone()[0] or 0
    if now - last < POST_COOLDOWN:
        raise PostError(f"Çok hızlısın. {POST_COOLDOWN - (now - last)} sn bekle.", 429)
    if tid is None:
        lt = conn.execute("SELECT MAX(created_at) FROM posts WHERE ip_hash=? AND parent_id IS NULL", (h,)).fetchone()[0] or 0
        if now - lt < THREAD_COOLDOWN:
            raise PostError(f"Yeni başlık için {THREAD_COOLDOWN - (now - lt)} sn bekle.", 429)
    if conn.execute("SELECT COUNT(*) FROM posts WHERE ip_hash=? AND created_at>?", (h, now - 600)).fetchone()[0] >= FLOOD_LIMIT:
        raise PostError("10 dakikada çok fazla gönderi attın.", 429)
    if body and conn.execute("SELECT 1 FROM posts WHERE ip_hash=? AND body=? AND created_at>?", (h, body, now - 300)).fetchone():
        raise PostError("Aynı mesajı az önce gönderdin.")

    img = save_image(f) if has_file else {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO posts(board,parent_id,name,trip,subject,body,ip_hash,created_at,bumped_at,"
            "img_file,img_thumb,img_w,img_h,img_size,img_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (board, tid, name, trip, subject, body, h, now, now, img.get("file"), img.get("thumb"),
             img.get("w"), img.get("h"), img.get("size"), img.get("name")))
        pid = cur.lastrowid
        if parent and not sage and n < BUMP_LIMIT:
            conn.execute("UPDATE posts SET bumped_at=? WHERE id=?", (now, tid))
        conn.execute("COMMIT")
    except sqlite3.Error as e:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        remove_files(img.get("file"), img.get("thumb"))
        app.logger.error("gönderi kaydedilemedi: %s", e)
        raise PostError("Sunucu meşgul, tekrar dene.", 503)

    if tid is None:
        prune(conn, board)
        return redirect(url_for("thread_view", board=board, tid=pid))
    return redirect(url_for("thread_view", board=board, tid=tid) + f"#p{pid}")


@app.post("/report/<int:pid>")
def report(pid):
    conn = db()
    row = conn.execute("SELECT id, board, parent_id FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    reason = request.form.get("reason", "")
    reason = reason if reason in REPORT_REASONS else "Diğer"
    h, now = ip_hash(), int(time.time())
    if conn.execute("SELECT COUNT(*) FROM reports WHERE ip_hash=? AND created_at>?", (h, now - 3600)).fetchone()[0] >= 10:
        raise PostError("Çok fazla rapor gönderdin. Sonra tekrar dene.", 429)
    if not conn.execute("SELECT 1 FROM reports WHERE post_id=? AND ip_hash=?", (pid, h)).fetchone():
        conn.execute("INSERT INTO reports(post_id, reason, ip_hash, created_at) VALUES(?,?,?,?)", (pid, reason, h, now))
    back = url_for("thread_view", board=row["board"], tid=row["parent_id"] or row["id"])
    return render_template("error.html", title="Rapor alındı", message="Raporun moderatörlere iletildi.", code=200, back=back)


# --------------------------------------------------------------------------
# Yönetim
# --------------------------------------------------------------------------
_LOGIN_FAILS: dict[str, list[float]] = {}


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*a, **kw)
    return wrapper


def safe_next(n: str | None) -> str:
    if n and n.startswith("/") and not n.startswith("//") and "\\" not in n:
        return n
    return url_for("admin_panel")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        ip, now = request.remote_addr or "?", time.time()
        fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 300]
        if len(fails) >= 5:
            raise PostError("Çok fazla deneme. 5 dakika bekle.", 429)
        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PW):
            csrf = session.get("csrf")
            session.clear()
            session["csrf"], session["admin"] = csrf or secrets.token_urlsafe(24), True
            session.permanent = True
            app.logger.info("yönetici girişi")
            return redirect(url_for("admin_panel"))
        fails.append(now)
        _LOGIN_FAILS[ip] = fails
        raise PostError("Şifre yanlış.", 403)
    return render_template("admin_login.html")


@app.post("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("home"))


@app.get("/admin")
@admin_required
def admin_panel():
    conn = db()
    reports = conn.execute(
        "SELECT p.*, COUNT(r.id) AS rcount, GROUP_CONCAT(DISTINCT r.reason) AS reasons, MAX(r.created_at) AS rlast "
        "FROM reports r JOIN posts p ON p.id=r.post_id GROUP BY p.id ORDER BY rlast DESC LIMIT 100").fetchall()
    bans = conn.execute("SELECT * FROM bans ORDER BY created_at DESC LIMIT 100").fetchall()
    return render_template("admin.html", reports=reports, bans=bans)


@app.post("/admin/action")
@admin_required
def admin_action():
    conn = db()
    action = request.form.get("action", "")
    nxt = safe_next(request.form.get("next"))
    if action == "unban":
        conn.execute("DELETE FROM bans WHERE ip_hash=?", (request.form.get("ip_hash", ""),))
        return redirect(nxt)
    try:
        pid = int(request.form.get("post_id", ""))
    except ValueError:
        abort(400)
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return redirect(nxt)
    now = int(time.time())

    if action in ("ban", "ban_delete"):
        try:
            hours = max(0, int(request.form.get("hours", "24")))
        except ValueError:
            hours = 24
        conn.execute("INSERT OR REPLACE INTO bans(ip_hash, until, reason, created_at) VALUES(?,?,?,?)",
                     (row["ip_hash"], 0 if hours == 0 else now + hours * 3600,
                      clean(request.form.get("reason", ""))[:200], now))
        app.logger.info("ban: post %s, %s saat", pid, hours)
    if action in ("delete", "ban_delete"):
        was_op = delete_post(conn, pid)
        app.logger.info("silindi: post %s", pid)
        if was_op and f"/thread/{pid}" in nxt:
            nxt = url_for("board_view", board=row["board"])
    elif action == "sticky" and row["parent_id"] is None:
        conn.execute("UPDATE posts SET sticky=1-sticky WHERE id=?", (pid,))
    elif action == "lock" and row["parent_id"] is None:
        conn.execute("UPDATE posts SET locked=1-locked WHERE id=?", (pid,))
    elif action == "dismiss":
        conn.execute("DELETE FROM reports WHERE post_id=?", (pid,))
    return redirect(nxt)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "5000"))
    try:
        from waitress import serve
    except ImportError:
        app.logger.warning("waitress yok; geliştirme sunucusu kullanılıyor (pip install waitress)")
        app.run(host=host, port=port, threaded=True, debug=False)
    else:
        print(f"[5san] http://{host}:{port}")
        serve(app, host=host, port=port, threads=8, max_request_body_size=8 * 1024 * 1024)
