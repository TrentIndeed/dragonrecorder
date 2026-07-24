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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config

log = logging.getLogger("dr.bridge")

PORT = int(__import__("os").environ.get("RECORD_BRIDGE_PORT", "8477"))


def poke_existing() -> bool:
    """True if another instance is already running — and if so, tell it to
    show its panel, so a double launch feels like 'open the app'."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/open",
                                     method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def start(open_panel) -> None:
    """Start the listener in a daemon thread. open_panel: zero-arg callable."""

    class Handler(BaseHTTPRequestHandler):
        def _cors_ok(self) -> bool:
            origin = self.headers.get("Origin", "")
            return origin == config.SERVER_URL or origin == ""

        def _respond(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
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
