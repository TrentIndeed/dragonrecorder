import asyncio
import json
import logging
import secrets
import shutil
import string
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiofiles
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import config
import db
from notify import send_telegram

log = logging.getLogger("dragonrecorder")
logging.basicConfig(level=logging.INFO)

templates = Jinja2Templates(directory=config.BASE_DIR / "templates")

SLUG_ALPHABET = string.ascii_letters + string.digits
CUT_KINDS = ("fillers", "silences")  # kinds whose toggle requires a derived render
ALL_EDIT_KINDS = ("fillers", "silences", "captions")
ASSET_FILES = {"thumb": "thumb.jpg", "vtt": "captions.vtt", "words": "words.json"}
REACTION_EMOJI = ["👍", "❤️", "🔥", "😂", "🤯"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_slug() -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(10))


def slug_dir(slug: str) -> Path:
    return config.DATA_DIR / slug


def free_gb() -> float:
    return shutil.disk_usage(config.DATA_DIR).free / 1e9


def require_token(authorization: str = Header(default="")) -> None:
    if not config.CAPTURE_TOKEN:
        raise HTTPException(503, "server has no CAPTURE_TOKEN configured")
    if authorization != f"Bearer {config.CAPTURE_TOKEN}":
        raise HTTPException(401, "bad token")


def get_recording(dbc, slug: str):
    row = dbc.execute("SELECT * FROM recordings WHERE slug=?", (slug,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such recording")
    return row


def current_video_file(dbc, slug: str) -> str:
    """The file the player should load: a derived render matching the enabled
    cut-edit set if it exists, else the original."""
    enabled = [
        r["kind"]
        for r in dbc.execute(
            "SELECT kind FROM edits WHERE slug=? AND enabled=1 AND kind IN (?,?)",
            (slug, *CUT_KINDS),
        )
    ]
    if enabled:
        name = "cut_" + "+".join(sorted(enabled)) + ".mp4"
        if (slug_dir(slug) / name).exists():
            return name
    return "video.mp4"


app = FastAPI(title="DragonRecorder", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "static"), name="static")
# Dev fallback; in production Caddy serves /media/* directly for range requests.
app.mount("/media", StaticFiles(directory=config.DATA_DIR), name="media")


# ---------------------------------------------------------------- client API

@app.post("/api/recordings", dependencies=[Depends(require_token)])
def mint_recording():
    if free_gb() < config.MIN_FREE_GB:
        send_telegram(f"⛔ DragonRecorder: refused new recording, {free_gb():.1f} GB free")
        raise HTTPException(507, f"under {config.MIN_FREE_GB} GB free disk")
    slug = new_slug()
    with db.connect() as dbc:
        dbc.execute("INSERT INTO recordings (slug) VALUES (?)", (slug,))
    return {"slug": slug}


@app.delete("/api/recordings/{slug}", dependencies=[Depends(require_token)])
def trash_recording(slug: str):
    """Trashed take: release the slug entirely — the link never existed."""
    with db.connect() as dbc:
        get_recording(dbc, slug)
        dbc.execute("DELETE FROM recordings WHERE slug=?", (slug,))
        for t in ("views", "comments", "reactions", "edits"):
            dbc.execute(f"DELETE FROM {t} WHERE slug=?", (slug,))
    shutil.rmtree(slug_dir(slug), ignore_errors=True)
    return {"ok": True}


@app.put("/api/recordings/{slug}/file", dependencies=[Depends(require_token)])
async def upload_file(slug: str, request: Request, duration_s: float = 0):
    with db.connect() as dbc:
        get_recording(dbc, slug)
    if free_gb() < config.MIN_FREE_GB:
        raise HTTPException(507, "low disk")
    d = slug_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    tmp, final = d / "video.mp4.part", d / "video.mp4"
    size = 0
    async with aiofiles.open(tmp, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            await f.write(chunk)
    tmp.replace(final)
    now = utcnow()
    with db.connect() as dbc:
        dbc.execute(
            "UPDATE recordings SET status='ready', size_bytes=?, duration_s=?,"
            " ready_at=?, expires_at=? WHERE slug=?",
            (size, duration_s or None, iso(now),
             iso(now + timedelta(days=config.RETENTION_DAYS)), slug),
        )
    return {"ok": True, "size": size}


@app.post("/api/recordings/{slug}/meta", dependencies=[Depends(require_token)])
async def set_meta(slug: str, request: Request):
    body = await request.json()
    fields = {k: body[k] for k in ("title", "description", "duration_s", "transcript")
              if k in body}
    if not fields:
        return {"ok": True}
    sets = ", ".join(f"{k}=?" for k in fields)
    with db.connect() as dbc:
        get_recording(dbc, slug)
        if "title" in fields:
            sets += ", title_is_ai=?"
            dbc.execute(f"UPDATE recordings SET {sets} WHERE slug=?",
                        (*fields.values(), int(body.get("title_is_ai", True)), slug))
        else:
            dbc.execute(f"UPDATE recordings SET {sets} WHERE slug=?",
                        (*fields.values(), slug))
    return {"ok": True}


@app.put("/api/recordings/{slug}/assets/{kind}", dependencies=[Depends(require_token)])
async def upload_asset(slug: str, kind: str, request: Request):
    render_kinds = {f"cut_{'+'.join(sorted(c))}": f"cut_{'+'.join(sorted(c))}.mp4"
                    for c in (["fillers"], ["silences"], ["fillers", "silences"])}
    files = {**ASSET_FILES, **render_kinds}
    if kind not in files:
        raise HTTPException(400, f"unknown asset kind {kind}")
    with db.connect() as dbc:
        get_recording(dbc, slug)
    d = slug_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (files[kind] + ".part")
    async with aiofiles.open(tmp, "wb") as f:
        async for chunk in request.stream():
            await f.write(chunk)
    tmp.replace(d / files[kind])
    flag = {"thumb": "has_thumb", "vtt": "has_vtt", "words": "has_words"}.get(kind)
    with db.connect() as dbc:
        get_recording(dbc, slug)
        if flag:
            dbc.execute(f"UPDATE recordings SET {flag}=1 WHERE slug=?", (slug,))
        if kind in render_kinds:
            for k in kind.removeprefix("cut_").split("+"):
                dbc.execute("UPDATE edits SET has_render=1 WHERE slug=? AND kind=?",
                            (slug, k))
    return {"ok": True}


@app.post("/api/recordings/{slug}/edits", dependencies=[Depends(require_token)])
async def register_edit(slug: str, request: Request):
    body = await request.json()
    kind = body.get("kind")
    if kind not in ALL_EDIT_KINDS:
        raise HTTPException(400, "bad kind")
    with db.connect() as dbc:
        get_recording(dbc, slug)
        dbc.execute(
            "INSERT INTO edits (slug, kind, count, enabled, data) VALUES (?,?,?,?,?)"
            " ON CONFLICT(slug, kind) DO UPDATE SET count=excluded.count,"
            " enabled=excluded.enabled, data=excluded.data",
            (slug, kind, int(body.get("count", 0)), int(body.get("enabled", False)),
             json.dumps(body.get("data")) if body.get("data") is not None else None),
        )
    return {"ok": True}


@app.get("/api/render-jobs", dependencies=[Depends(require_token)])
def render_jobs():
    """Cut-edit combos toggled on (from the dashboard) whose render hasn't been
    uploaded yet. The client tray polls this and produces the renders."""
    jobs = []
    with db.connect() as dbc:
        rows = dbc.execute(
            "SELECT slug, GROUP_CONCAT(kind) kinds FROM edits"
            " WHERE enabled=1 AND kind IN (?,?) GROUP BY slug", CUT_KINDS).fetchall()
        for r in rows:
            kinds = sorted(r["kinds"].split(","))
            name = "cut_" + "+".join(kinds) + ".mp4"
            if not (slug_dir(r["slug"]) / name).exists():
                row = dbc.execute("SELECT status FROM recordings WHERE slug=?",
                                  (r["slug"],)).fetchone()
                if row and row["status"] == "ready":
                    jobs.append({"slug": r["slug"], "kinds": kinds})
    return {"jobs": jobs}


@app.post("/api/report", dependencies=[Depends(require_token)])
async def client_report(request: Request):
    """Client-side failures (ffmpeg died, upload failed after retries)."""
    body = await request.json()
    send_telegram(f"🔥 DragonRecorder client: {body.get('message', 'unknown error')}")
    return {"ok": True}


# ---------------------------------------------------------------- viewer

def ensure_viewer(response: Response, dr_vid: str | None) -> str:
    if dr_vid:
        return dr_vid
    vid = uuid.uuid4().hex
    response.set_cookie("dr_vid", vid, max_age=10 * 365 * 24 * 3600, samesite="lax")
    return vid


@app.get("/w/{slug}", response_class=HTMLResponse)
def watch(request: Request, slug: str, n: str | None = None,
          dr_vid: str | None = Cookie(default=None),
          dr_owner: str | None = Cookie(default=None)):
    with db.connect() as dbc:
        row = dbc.execute("SELECT * FROM recordings WHERE slug=?", (slug,)).fetchone()
        if row is None:
            return templates.TemplateResponse(request, "gone.html",
                                              {"reason": "notfound"}, status_code=404)
        if row["status"] == "expired":
            return templates.TemplateResponse(request, "gone.html",
                                              {"reason": "expired"}, status_code=410)
        if row["status"] != "ready":
            return templates.TemplateResponse(request, "processing.html",
                                              {"slug": slug})
        video_file = current_video_file(dbc, slug)
        edits = {r["kind"]: dict(r) for r in dbc.execute(
            "SELECT * FROM edits WHERE slug=?", (slug,))}
        n_viewers = dbc.execute(
            "SELECT COUNT(*) c FROM views WHERE slug=? AND is_owner=0",
            (slug,)).fetchone()["c"]
        reactions = dbc.execute(
            "SELECT emoji, COUNT(*) c FROM reactions WHERE slug=? GROUP BY emoji",
            (slug,)).fetchall()
        comments = dbc.execute(
            "SELECT * FROM comments WHERE slug=? ORDER BY created_at", (slug,)).fetchall()

    resp = templates.TemplateResponse(request, "watch.html", {
        "r": dict(row), "slug": slug, "video_file": video_file,
        "captions_on": edits.get("captions", {}).get("enabled") and row["has_vtt"],
        "views": n_viewers,
        "reaction_emoji": REACTION_EMOJI,
        "reaction_counts": {r["emoji"]: r["c"] for r in reactions},
        "comments": [dict(c) for c in comments],
        "label": n or "",
        "is_owner": bool(dr_owner),
    })
    vid = ensure_viewer(resp, dr_vid)
    with db.connect() as dbc:
        seen_before = dbc.execute(
            "SELECT 1 FROM views WHERE slug=? AND viewer_id=?",
            (slug, vid)).fetchone() is not None
        dbc.execute(
            "INSERT INTO views (slug, viewer_id, label, is_owner, last_seen)"
            " VALUES (?,?,?,?,?) ON CONFLICT(slug, viewer_id) DO UPDATE SET"
            " last_seen=excluded.last_seen,"
            " label=COALESCE(excluded.label, views.label),"
            " is_owner=MAX(views.is_owner, excluded.is_owner)",
            (slug, vid, n, int(bool(dr_owner)), iso(utcnow())),
        )
    if not dr_owner and not seen_before:
        who = n or "someone"
        title = row["title"] or slug
        send_telegram(f"👀 {who} is watching “{title}”\n{config.PUBLIC_URL}/w/{slug}")
    return resp


@app.get("/api/w/{slug}/state")
def watch_state(slug: str):
    with db.connect() as dbc:
        row = dbc.execute("SELECT status, title FROM recordings WHERE slug=?",
                          (slug,)).fetchone()
    if row is None:
        raise HTTPException(404)
    return {"status": row["status"], "title": row["title"]}


def merge_ranges(ranges: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for s, e in sorted((r for r in ranges if e_ok(r)), key=lambda r: r[0]):
        if out and s <= out[-1][1] + 0.5:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def e_ok(r) -> bool:
    return isinstance(r, (list, tuple)) and len(r) == 2 and r[1] > r[0] >= 0


@app.post("/api/w/{slug}/progress")
async def progress(slug: str, request: Request,
                   dr_vid: str | None = Cookie(default=None)):
    if not dr_vid:
        return {"ok": False}
    body = await request.json()
    ranges = [r for r in body.get("ranges", []) if e_ok(r)]
    with db.connect() as dbc:
        row = dbc.execute("SELECT ranges FROM views WHERE slug=? AND viewer_id=?",
                          (slug, dr_vid)).fetchone()
        if row is None:
            return {"ok": False}
        merged = merge_ranges(json.loads(row["ranges"]) + ranges)
        watched = sum(e - s for s, e in merged)
        max_pos = max([e for _, e in merged], default=0)
        dbc.execute(
            "UPDATE views SET ranges=?, watched_s=?, max_pos_s=?, last_seen=?"
            " WHERE slug=? AND viewer_id=?",
            (json.dumps(merged), watched, max_pos, iso(utcnow()), slug, dr_vid),
        )
    return {"ok": True}


@app.get("/api/w/{slug}/heatmap")
def heatmap(slug: str):
    """Per-percent viewer coverage for the scrub-bar attention histogram."""
    with db.connect() as dbc:
        row = get_recording(dbc, slug)
        views = dbc.execute(
            "SELECT ranges FROM views WHERE slug=? AND is_owner=0 AND watched_s>0",
            (slug,)).fetchall()
    dur = row["duration_s"] or 0
    buckets = [0] * 100
    if dur:
        for v in views:
            for s, e in json.loads(v["ranges"]):
                for b in range(int(s / dur * 100), min(int(e / dur * 100) + 1, 100)):
                    buckets[b] += 1
    return {"buckets": buckets, "viewers": len(views)}


@app.post("/api/w/{slug}/comments")
async def add_comment(slug: str, request: Request,
                      dr_vid: str | None = Cookie(default=None)):
    body = await request.json()
    text = (body.get("body") or "").strip()[:2000]
    if not text:
        raise HTTPException(400, "empty comment")
    author = (body.get("author") or "Anonymous").strip()[:80] or "Anonymous"
    at_s = body.get("at_s")
    with db.connect() as dbc:
        get_recording(dbc, slug)
        cur = dbc.execute(
            "INSERT INTO comments (slug, viewer_id, author, body, at_s)"
            " VALUES (?,?,?,?,?)",
            (slug, dr_vid or "anon", author, text,
             float(at_s) if at_s is not None else None),
        )
        row = dbc.execute("SELECT * FROM comments WHERE id=?",
                          (cur.lastrowid,)).fetchone()
    return dict(row)


@app.post("/api/w/{slug}/reactions")
async def toggle_reaction(slug: str, request: Request, response: Response,
                          dr_vid: str | None = Cookie(default=None)):
    body = await request.json()
    emoji = body.get("emoji")
    if emoji not in REACTION_EMOJI:
        raise HTTPException(400, "unknown emoji")
    vid = ensure_viewer(response, dr_vid)
    with db.connect() as dbc:
        get_recording(dbc, slug)
        existing = dbc.execute(
            "SELECT 1 FROM reactions WHERE slug=? AND viewer_id=? AND emoji=?",
            (slug, vid, emoji)).fetchone()
        if existing:
            dbc.execute("DELETE FROM reactions WHERE slug=? AND viewer_id=? AND emoji=?",
                        (slug, vid, emoji))
        else:
            dbc.execute("INSERT INTO reactions (slug, viewer_id, emoji) VALUES (?,?,?)",
                        (slug, vid, emoji))
        counts = dbc.execute(
            "SELECT emoji, COUNT(*) c FROM reactions WHERE slug=? GROUP BY emoji",
            (slug,)).fetchall()
    return {"counts": {r["emoji"]: r["c"] for r in counts}, "toggled": not existing}


# ---------------------------------------------------------------- dashboard
# Single-user session login, same scheme as remote_pc (auth.py). The login
# endpoints are rate limited per IP and ping Telegram like remote_pc does.

def dash_authed(request: Request) -> bool:
    return auth.validate_token(request.cookies.get(config.DASH_COOKIE, ""))


def require_dash(request: Request) -> None:
    if not dash_authed(request):
        raise HTTPException(401, "unauthorized")


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host or "?")


@app.get("/dash", response_class=HTMLResponse)
def dashboard(request: Request):
    if not dash_authed(request):
        return templates.TemplateResponse(request, "login.html", {})
    resp = templates.TemplateResponse(request, "dash.html", {})
    # Visiting the dashboard marks this browser as the owner so its plays
    # don't count as views or fire notifications.
    resp.set_cookie("dr_owner", "1", max_age=10 * 365 * 24 * 3600, samesite="lax")
    return resp


@app.post("/api/dash/login")
async def dash_login(request: Request):
    ip = client_ip(request)
    if auth.is_locked(ip):
        raise HTTPException(429, "too many attempts — try again later")
    body = await request.json()
    if auth.check_credentials(body.get("username", ""), body.get("password", "")):
        auth.clear_failures(ip)
        token = auth.create_session()
        send_telegram(f"✅ DragonRecorder dashboard login\nIP: {ip}")
        resp = JSONResponse({"ok": True})
        max_age = config.DASH_SESSION_TTL if body.get("remember", True) else None
        resp.set_cookie(config.DASH_COOKIE, token, max_age=max_age, path="/",
                        httponly=True, samesite="lax")
        # the owner cookie rides along so logins never count as views
        resp.set_cookie("dr_owner", "1", max_age=10 * 365 * 24 * 3600,
                        samesite="lax")
        return resp
    auth.record_failure(ip)
    if auth.is_locked(ip):
        send_telegram(f"🚫 DragonRecorder dashboard: too many failed logins — "
                      f"IP locked out\nIP: {ip}")
    await asyncio.sleep(1.0)
    raise HTTPException(401, "wrong username or password")


@app.post("/api/dash/logout")
def dash_logout(request: Request):
    auth.destroy_session(request.cookies.get(config.DASH_COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.DASH_COOKIE, path="/")
    return resp


@app.get("/api/dash/recordings", dependencies=[Depends(require_dash)])
def dash_list():
    with db.connect() as dbc:
        rows = dbc.execute(
            "SELECT r.*,"
            " (SELECT COUNT(*) FROM views v WHERE v.slug=r.slug AND v.is_owner=0"
            "   AND v.watched_s>0) views,"
            " (SELECT COUNT(*) FROM comments c WHERE c.slug=r.slug) comments"
            " FROM recordings r ORDER BY r.created_at DESC").fetchall()
    return {"recordings": [dict(r) for r in rows]}


@app.get("/api/dash/recordings/{slug}", dependencies=[Depends(require_dash)])
def dash_detail(slug: str):
    with db.connect() as dbc:
        row = get_recording(dbc, slug)
        viewers = dbc.execute(
            "SELECT viewer_id, label, is_owner, started_at, last_seen, watched_s,"
            " max_pos_s FROM views WHERE slug=? ORDER BY started_at DESC",
            (slug,)).fetchall()
        edits = dbc.execute("SELECT * FROM edits WHERE slug=?", (slug,)).fetchall()
        comments = dbc.execute(
            "SELECT * FROM comments WHERE slug=? ORDER BY created_at", (slug,)).fetchall()
    return {
        "recording": dict(row),
        "viewers": [dict(v) for v in viewers],
        "edits": [dict(e) for e in edits],
        "comments": [dict(c) for c in comments],
    }


@app.patch("/api/dash/recordings/{slug}", dependencies=[Depends(require_dash)])
async def dash_update(slug: str, request: Request):
    body = await request.json()
    with db.connect() as dbc:
        get_recording(dbc, slug)
        if "title" in body:
            dbc.execute("UPDATE recordings SET title=?, title_is_ai=0 WHERE slug=?",
                        (str(body["title"])[:200], slug))
        if "description" in body:
            dbc.execute("UPDATE recordings SET description=? WHERE slug=?",
                        (str(body["description"])[:5000], slug))
    return {"ok": True}


@app.delete("/api/dash/recordings/{slug}", dependencies=[Depends(require_dash)])
def dash_delete(slug: str):
    with db.connect() as dbc:
        get_recording(dbc, slug)
        dbc.execute("DELETE FROM recordings WHERE slug=?", (slug,))
        for t in ("views", "comments", "reactions", "edits"):
            dbc.execute(f"DELETE FROM {t} WHERE slug=?", (slug,))
    shutil.rmtree(slug_dir(slug), ignore_errors=True)
    return {"ok": True}


@app.post("/api/dash/recordings/{slug}/edits/{kind}", dependencies=[Depends(require_dash)])
async def dash_toggle_edit(slug: str, kind: str, request: Request):
    body = await request.json()
    enabled = int(bool(body.get("enabled")))
    with db.connect() as dbc:
        get_recording(dbc, slug)
        row = dbc.execute("SELECT 1 FROM edits WHERE slug=? AND kind=?",
                          (slug, kind)).fetchone()
        if row is None:
            raise HTTPException(404, "edit not detected for this recording")
        dbc.execute("UPDATE edits SET enabled=? WHERE slug=? AND kind=?",
                    (enabled, slug, kind))
    return {"ok": True}


DEFAULT_AUTO_APPLY = {"fillers": True, "silences": True, "captions": False}


def get_auto_apply(dbc) -> dict:
    row = dbc.execute("SELECT value FROM settings WHERE key='auto_apply'").fetchone()
    return {**DEFAULT_AUTO_APPLY, **(json.loads(row["value"]) if row else {})}


@app.get("/api/settings/auto-apply")
def read_auto_apply():
    """Which edit toggles default to on for new recordings. Read by both the
    dashboard UI and the client (which is why it isn't under /api/dash)."""
    with db.connect() as dbc:
        return get_auto_apply(dbc)


@app.put("/api/dash/settings/auto-apply", dependencies=[Depends(require_dash)])
async def write_auto_apply(request: Request):
    body = await request.json()
    clean = {k: bool(body[k]) for k in ALL_EDIT_KINDS if k in body}
    with db.connect() as dbc:
        merged = {**get_auto_apply(dbc), **clean}
        dbc.execute(
            "INSERT INTO settings (key, value) VALUES ('auto_apply', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(merged),))
    return merged


# ---------------------------------------------------------------- hygiene

@app.get("/healthz")
def healthz():
    return {"status": "ok", "free_gb": round(free_gb(), 1)}


async def reaper_loop():
    while True:
        try:
            reap()
        except Exception:
            log.exception("reaper failed")
        await asyncio.sleep(24 * 3600)


def reap():
    now = iso(utcnow())
    stale_pending = iso(utcnow() - timedelta(hours=24))
    with db.connect() as dbc:
        expired = dbc.execute(
            "SELECT slug FROM recordings WHERE status='ready' AND expires_at<?",
            (now,)).fetchall()
        for r in expired:
            shutil.rmtree(slug_dir(r["slug"]), ignore_errors=True)
            dbc.execute("UPDATE recordings SET status='expired' WHERE slug=?",
                        (r["slug"],))
        if expired:
            log.info("reaper expired %d recordings", len(expired))
        # pending rows the client never finished (crash without trash)
        dead = dbc.execute(
            "SELECT slug FROM recordings WHERE status='pending' AND created_at<?",
            (stale_pending,)).fetchall()
        for r in dead:
            dbc.execute("UPDATE recordings SET status='failed' WHERE slug=?",
                        (r["slug"],))
        if dead:
            send_telegram(f"⚠️ DragonRecorder: {len(dead)} recording(s) never finished "
                          "uploading and were marked failed")
        # broken links: ready rows whose file vanished
        broken = [r["slug"] for r in
                  dbc.execute("SELECT slug FROM recordings WHERE status='ready'")
                  if not (slug_dir(r["slug"]) / "video.mp4").exists()]
        for s in broken:
            dbc.execute("UPDATE recordings SET status='failed' WHERE slug=?", (s,))
        if broken:
            send_telegram("💔 DragonRecorder: live link(s) with missing files: "
                          + ", ".join(broken))
    if free_gb() < config.MIN_FREE_GB:
        send_telegram(f"💾 DragonRecorder: disk low — {free_gb():.1f} GB free")


@app.on_event("startup")
async def startup():
    db.init_db()
    asyncio.create_task(reaper_loop())
