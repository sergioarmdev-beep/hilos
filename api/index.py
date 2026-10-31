import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).resolve().parent.parent
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").replace("libsql://", "https://")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
LOCAL_DB = "/tmp/hilos.db" if os.getenv("VERCEL") else str(BASE / "local.db")

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')))",
    "CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')))",
    "CREATE TABLE IF NOT EXISTS votes (post_id INTEGER NOT NULL, voter TEXT NOT NULL, value INTEGER NOT NULL, PRIMARY KEY (post_id, voter))",
    "CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id)",
]

# ---------- Base de datos: Turso por HTTP, o SQLite local ----------

def _enc(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    return {"type": "text", "value": str(v)}


def _dec(c):
    t, v = c["type"], c.get("value")
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    return None if t == "null" else v


def run(stmts):
    """stmts: lista de (sql, args). Devuelve una lista de filas (dicts) por sentencia."""
    if TURSO_URL:
        reqs = [{"type": "execute", "stmt": {"sql": s, "args": [_enc(a) for a in args]}} for s, args in stmts]
        reqs.append({"type": "close"})
        r = httpx.post(
            f"{TURSO_URL}/v2/pipeline",
            json={"requests": reqs},
            headers={"Authorization": f"Bearer {TURSO_TOKEN}"},
            timeout=10,
        )
        r.raise_for_status()
        out = []
        for item in r.json()["results"][: len(stmts)]:
            if item["type"] == "error":
                raise RuntimeError(item["error"]["message"])
            res = item["response"]["result"]
            cols = [c["name"] for c in res["cols"]]
            out.append([dict(zip(cols, map(_dec, row))) for row in res["rows"]])
        return out
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    try:
        out = [[dict(r) for r in con.execute(s, args).fetchall()] for s, args in stmts]
        con.commit()
        return out
    finally:
        con.close()


_ready = False


def db(*stmts):
    global _ready
    if not _ready:
        run([(s, []) for s in SCHEMA])
        _ready = True
    return run(list(stmts))


# ---------- App ----------

app = FastAPI()
templates = Jinja2Templates(directory=BASE / "templates")


def ago(ts):
    s = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
    for n, u in ((86400, "d"), (3600, "h"), (60, "min")):
        if s >= n:
            return f"hace {int(s // n)} {u}"
    return "ahora"


templates.env.filters["ago"] = ago


def clean(s, n):
    return s.strip()[:n]


def saved_name(request):
    return unquote(request.cookies.get("name", ""))


def redirect(url, author):
    resp = RedirectResponse(url, 303)
    resp.set_cookie("name", quote(author), max_age=31536000, samesite="lax")
    return resp


@app.middleware("http")
async def visitor_cookie(request: Request, call_next):
    vid = request.cookies.get("vid")
    request.state.vid = vid or uuid.uuid4().hex
    resp = await call_next(request)
    if not vid:
        resp.set_cookie("vid", request.state.vid, max_age=31536000, httponly=True, samesite="lax")
    return resp


POSTS = """SELECT p.id, p.author, p.title, p.body, p.created_at,
  COALESCE((SELECT SUM(value) FROM votes WHERE post_id = p.id), 0) AS score,
  (SELECT COUNT(*) FROM comments WHERE post_id = p.id) AS ncom,
  COALESCE((SELECT value FROM votes WHERE post_id = p.id AND voter = ?), 0) AS mine
  FROM posts p"""


@app.get("/")
def index(request: Request, sort: str = "top"):
    order = "p.id DESC" if sort == "new" else "score DESC, p.id DESC"
    posts = db((f"{POSTS} ORDER BY {order} LIMIT 100", [request.state.vid]))[0]
    return templates.TemplateResponse(request, "index.html", {"posts": posts, "sort": sort, "name": saved_name(request)})


@app.post("/post")
def create_post(title: str = Form(...), body: str = Form(""), author: str = Form("")):
    title, body, author = clean(title, 150), clean(body, 5000), clean(author, 30) or "anónimo"
    if not title:
        raise HTTPException(400, "El título es obligatorio")
    pid = db(("INSERT INTO posts (author, title, body) VALUES (?, ?, ?) RETURNING id", [author, title, body]))[0][0]["id"]
    return redirect(f"/p/{pid}", author)


@app.get("/p/{pid}")
def post_page(request: Request, pid: int):
    posts, comments = db(
        (f"{POSTS} WHERE p.id = ?", [request.state.vid, pid]),
        ("SELECT * FROM comments WHERE post_id = ? ORDER BY id", [pid]),
    )
    if not posts:
        raise HTTPException(404, "Ese post no existe")
    return templates.TemplateResponse(request, "post.html", {"p": posts[0], "comments": comments, "name": saved_name(request)})


@app.post("/p/{pid}/comment")
def comment(pid: int, body: str = Form(...), author: str = Form("")):
    body, author = clean(body, 2000), clean(author, 30) or "anónimo"
    if body:
        db(("INSERT INTO comments (post_id, author, body) VALUES (?, ?, ?)", [pid, author, body]))
    return redirect(f"/p/{pid}#comentarios", author)


@app.post("/vote/{pid}")
def vote(request: Request, pid: int, value: int = Form(...)):
    if value not in (1, -1):
        raise HTTPException(400, "Voto inválido")
    vid = request.state.vid
    _, res = db(
        ("INSERT INTO votes (post_id, voter, value) VALUES (?, ?, ?) ON CONFLICT(post_id, voter) DO UPDATE SET value = CASE WHEN value = excluded.value THEN 0 ELSE excluded.value END", [pid, vid, value]),
        ("SELECT COALESCE(SUM(value), 0) AS score, COALESCE(SUM(CASE WHEN voter = ? THEN value END), 0) AS mine FROM votes WHERE post_id = ?", [vid, pid]),
    )
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(res[0])
    return RedirectResponse(request.headers.get("referer") or "/", 303)
