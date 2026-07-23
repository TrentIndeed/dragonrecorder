"""Server client. Every call is best-effort with retries where it matters —
the recorder must keep working when the network hiccups."""

import logging
import time
from pathlib import Path

import httpx

from . import config

log = logging.getLogger("dr.api")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=config.SERVER_URL,
        headers={"Authorization": f"Bearer {config.CAPTURE_TOKEN}"},
        timeout=30,
    )


def mint_slug() -> str | None:
    try:
        with _client() as c:
            r = c.post("/api/recordings")
            r.raise_for_status()
            return r.json()["slug"]
    except Exception as exc:
        log.warning("mint failed: %s", exc)
        return None


def trash(slug: str) -> None:
    try:
        with _client() as c:
            c.delete(f"/api/recordings/{slug}")
    except Exception as exc:
        log.warning("trash failed for %s: %s", slug, exc)


def upload_video(slug: str, path: Path, duration_s: float, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            with _client() as c, open(path, "rb") as f:
                r = c.put(
                    f"/api/recordings/{slug}/file",
                    params={"duration_s": duration_s},
                    content=f,
                    timeout=httpx.Timeout(30, write=600, read=600),
                )
                r.raise_for_status()
                return True
        except Exception as exc:
            log.warning("upload attempt %d failed: %s", attempt + 1, exc)
            time.sleep(2 ** attempt * 2)
    return False


def set_meta(slug: str, **fields) -> None:
    try:
        with _client() as c:
            c.post(f"/api/recordings/{slug}/meta", json=fields)
    except Exception as exc:
        log.warning("set_meta failed: %s", exc)


def upload_asset(slug: str, kind: str, path: Path) -> bool:
    try:
        with _client() as c, open(path, "rb") as f:
            r = c.put(f"/api/recordings/{slug}/assets/{kind}", content=f,
                      timeout=httpx.Timeout(30, write=600, read=600))
            r.raise_for_status()
            return True
    except Exception as exc:
        log.warning("upload_asset %s failed: %s", kind, exc)
        return False


def register_edit(slug: str, kind: str, count: int, enabled: bool,
                  data: dict | None = None) -> None:
    try:
        with _client() as c:
            c.post(f"/api/recordings/{slug}/edits",
                   json={"kind": kind, "count": count, "enabled": enabled,
                         "data": data})
    except Exception as exc:
        log.warning("register_edit failed: %s", exc)


def get_auto_apply() -> dict:
    try:
        with _client() as c:
            r = c.get("/api/settings/auto-apply")
            r.raise_for_status()
            return r.json()
    except Exception:
        return {"fillers": True, "silences": True, "captions": False}


def get_render_jobs() -> list[dict]:
    try:
        with _client() as c:
            r = c.get("/api/render-jobs")
            r.raise_for_status()
            return r.json()["jobs"]
    except Exception:
        return []


def report_failure(message: str) -> None:
    """Route client-side failures through the server's Telegram wiring."""
    try:
        with _client() as c:
            c.post("/api/report", json={"message": message})
    except Exception as exc:
        log.warning("report_failure could not reach server: %s", exc)


def share_url(slug: str) -> str:
    return f"{config.SERVER_URL}/w/{slug}"
