import os
import secrets
import sqlite3
import time
from pathlib import Path
from flask import Flask, g, jsonify, make_response, redirect, render_template, request, url_for

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "5san.db"
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # İstek başına 2 MB

BOARDS = ["Genel", "Teknoloji", "Oyun", "Mizah", "Siyaset", "Dizi", "Anime", "Sohbet", "Hikâye", "Diğer"]

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS posts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      nickname TEXT NOT NULL DEFAULT '',
      board TEXT NOT NULL DEFAULT 'Genel',
      title TEXT NOT NULL DEFAULT '',
      body TEXT NOT NULL,
      created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS likes (
      post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
      voter_token TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      PRIMARY KEY (post_id, voter_token)
    );
    CREATE TABLE IF NOT EXISTS reports (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
      reason TEXT NOT NULL,
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_posts_board_created ON posts(board, created_at DESC);
    """)
    conn.commit()
    conn.close()

@app.before_request
def ensure_voter():
    # E-posta veya hesap gerektirmeyen, tarayıcıya özel anonim kimlik.
    if not request.cookies.get("5san_voter"):
        g.new_voter_token = secrets.token_urlsafe(32)
    else:
        g.new_voter_token = None

@app.after_request
def set_voter_cookie(response):
    token = getattr(g, "new_voter_token", None)
    if token:
        response.set_cookie("5san_voter", token, max_age=60*60*24*365,
                            httponly=True, samesite="Lax", secure=request.is_secure)
    return response

def voter_token():
    return request.cookies.get("5san_voter") or getattr(g, "new_voter_token", None)

@app.get("/")
def index():
    board = request.args.get("board", "Hepsi")
    if board not in ["Hepsi"] + BOARDS:
        board = "Hepsi"
    sort = request.args.get("sort", "new")
    query = request.args.get("q", "").strip()[:100]
    clauses, params = [], []
    if board != "Hepsi":
        clauses.append("p.board = ?"); params.append(board)
    if query:
        clauses.append("(p.title LIKE ? OR p.body LIKE ? OR p.nickname LIKE ?)")
        like_q = f"%{query}%"; params.extend([like_q, like_q, like_q])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = "likes_count DESC, p.created_at DESC" if sort == "popular" else "p.created_at ASC" if sort == "old" else "p.created_at DESC"
    posts = db().execute(f"""
      SELECT p.*, COUNT(l.voter_token) AS likes_count
      FROM posts p LEFT JOIN likes l ON l.post_id = p.id
      {where}
      GROUP BY p.id ORDER BY {order} LIMIT 100
    """, params).fetchall()
    return render_template("index.html", posts=posts, boards=BOARDS, active_board=board,
                           sort=sort, query=query)

@app.post("/post")
def create_post():
    nickname = request.form.get("nickname", "").strip()[:24]
    board = request.form.get("board", "Genel")
    title = request.form.get("title", "").strip()[:100]
    body = request.form.get("body", "").strip()[:4000]
    if board not in BOARDS or not body:
        return redirect(url_for("index"))
    # Basit gönderi aralığı: aynı tarayıcıdan 10 saniyede bir gönderi.
    now = int(time.time())
    last = request.cookies.get("5san_last_post", "0")
    try: last_post = int(last)
    except ValueError: last_post = 0
    if now - last_post < 10:
        return redirect(url_for("index", board=board, error="wait"))
    db().execute("INSERT INTO posts(nickname, board, title, body, created_at) VALUES(?,?,?,?,?)",
                 (nickname, board, title, body, now))
    db().commit()
    response = make_response(redirect(url_for("index", board=board)))
    response.set_cookie("5san_last_post", str(now), max_age=60, httponly=True, samesite="Lax")
    return response

@app.post("/like/<int:post_id>")
def like(post_id):
    token = voter_token()
    if not token:
        return jsonify(ok=False, error="Anonim kimlik bulunamadı."), 400
    try:
        db().execute("INSERT INTO likes(post_id, voter_token, created_at) VALUES(?,?,?)",
                     (post_id, token, int(time.time())))
        db().commit()
        return jsonify(ok=True)
    except sqlite3.IntegrityError:
        return jsonify(ok=False, error="Bu gönderiyi zaten beğendin."), 409

@app.post("/report/<int:post_id>")
def report(post_id):
    reason = request.form.get("reason", "Uygunsuz içerik").strip()[:200]
    exists = db().execute("SELECT id FROM posts WHERE id=?", (post_id,)).fetchone()
    if exists:
        db().execute("INSERT INTO reports(post_id, reason, created_at) VALUES(?,?,?)",
                     (post_id, reason, int(time.time())))
        db().commit()
    return redirect(url_for("index"))

@app.errorhandler(413)
def too_large(error):
    return "İstek çok büyük. Görsel yükleme bu başlangıç sürümünde kapalı.", 413

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=False)
