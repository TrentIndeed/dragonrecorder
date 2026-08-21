"""Loopback bridge so the web dashboard's "Record a video" button can open
the native launcher panel — the browser can't reach the tray app any other
way. Chrome exempts 127.0.0.1 from mixed-content blocking, so the HTTPS
dashboard may call this plain-HTTP loopback listener.

Security posture: binds loopback only, allows exactly one Origin (the
configured SERVER_URL), and the only action is showing the launcher panel —
nothing records, uploads, or reads anything.
"""

import json
import logging
import math
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config

log = logging.getLogger("dr.bridge")

PORT = int(__import__("os").environ.get("RECORD_BRIDGE_PORT", "8477"))

# The take that just finished, for any DragonRecorder tab that is already
# open. A page polls /status, sees a slug it has not shown yet, POSTs
# /claim and navigates itself — so the library tab you were looking at
# becomes the new recording instead of a second tab appearing beside it.
# Exactly one of the two wins: whoever calls claim()/mark_opened() first.
# Pages long-poll /wait rather than polling on a timer: a browser tab that
# isn't in front gets its timers throttled to once a minute, which would
# miss the handoff window every time. A parked request wakes the instant
# announce() fires, even in a background tab.
_last: dict = {"slug": None, "url": None, "at": 0.0, "taken": True}
_cv = threading.Condition()
WAIT_TIMEOUT_S = 25.0


def announce(slug: str, url: str) -> None:
    with _cv:
        _last.update(slug=slug, url=url, at=time.time(), taken=False)
        _cv.notify_all()


def claim() -> bool:
    """Take the pending recording. True for the first caller only."""
    with _cv:
        first = not _last["taken"]
        _last["taken"] = True
        return first


def wait_for(since: float, timeout: float = WAIT_TIMEOUT_S) -> dict:
    """Block until a take newer than `since` is up for grabs, or time out."""
    deadline = time.time() + max(0.0, min(timeout, WAIT_TIMEOUT_S))
    with _cv:
        while True:
            if (_last["slug"] and not _last["taken"]
                    and _last["at"] > since):
                return {"slug": _last["slug"], "at": _last["at"],
                        "taken": False}
            left = deadline - time.time()
            if left <= 0:
                return {"slug": None, "at": _last["at"], "taken": True}
            _cv.wait(left)


def mark_opened() -> bool:
    """The app is opening the tab itself. Same race, from the other side."""
    return claim()


def is_taken() -> bool:
    with _cv:
        return bool(_last["taken"])


def last_recording() -> dict:
    with _cv:
        return {"slug": _last["slug"], "at": _last["at"],
                "taken": _last["taken"]}


def start(open_panel, close_panel=None, get_status=None,
          render_now=None) -> None:
    """Start the listener in a daemon thread. open_panel/close_panel/
    render_now: zero-arg callables; get_status: () -> dict for /status."""

    class Handler(BaseHTTPRequestHandler):
        def _cors_ok(self) -> bool:
            origin = self.headers.get("Origin", "")
            return origin == config.SERVER_URL or origin == ""

        def _respond(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            try:
                self._write(code, data)
            except OSError:
                pass        # tab closed while its /wait was parked

        def _write(self, code: int, data: bytes) -> None:
            self.send_response(code)
            self.send_header("Access-Control-Allow-Origin", config.SERVER_URL)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", config.SERVER_URL)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.end_headers()

        def do_GET(self):
            if not self._cors_ok():
                return self._respond(403, {"error": "bad origin"})
            if self.path == "/ping":
                return self._respond(200, {"ok": True})
            if self.path.startswith("/wait"):
                q = urllib.parse.urlparse(self.path).query
                raw = urllib.parse.parse_qs(q).get("since", ["0"])[0]
                try:
                    since = float(raw)
                except ValueError:
                    return self._respond(400, {"error": "bad since"})
                if not math.isfinite(since):
                    return self._respond(400, {"error": "bad since"})
                return self._respond(200, wait_for(since))
            if self.path == "/status" and get_status:
                try:
                    body = get_status()
                    body["last_recording"] = last_recording()
                    return self._respond(200, body)
                except Exception:
                    return self._respond(500, {"error": "status failed"})
            self._respond(404, {"error": "unknown"})

        def do_POST(self):
            if not self._cors_ok():
                return self._respond(403, {"error": "bad origin"})
            if self.path == "/open":
                try:
                    open_panel()
                except Exception:
                    log.exception("open_panel failed")
                    return self._respond(500, {"error": "panel failed"})
                return self._respond(200, {"ok": True})
            if self.path == "/close" and close_panel:
                try:
                    close_panel()
                except Exception:
                    log.exception("close_panel failed")
                return self._respond(200, {"ok": True})
            if self.path == "/claim":
                # an open tab is taking this recording over, so the app
                # skips opening one of its own
                rec = last_recording()
                return self._respond(200, {"ok": True, "first": claim(),
                                           "slug": rec["slug"]})
            if self.path == "/render" and render_now:
                try:
                    render_now()
                except Exception:
                    log.exception("render_now failed")
                return self._respond(200, {"ok": True})
            self._respond(404, {"error": "unknown"})

        def log_message(self, *args):
            pass

    def run():
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
            log.info("record bridge on 127.0.0.1:%d", PORT)
            srv.serve_forever()
        except OSError as exc:
            log.warning("record bridge not started: %s", exc)

    threading.Thread(target=run, daemon=True).start()
