"""DragonRecorder tray app entry point.

Threading: pywebview owns the main thread; the tray icon, global hotkeys,
and the render-job poller run in daemon threads. Overlay windows are created
once and shown/hidden per take.
"""

import logging
import shutil
import threading
import time

import keyboard
import pystray
import webview
from PIL import Image, ImageDraw

from . import config, devices, session, ui

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(config.APPDATA_DIR / "client.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("dr.main")

HOTKEY_RECORD = "ctrl+shift+r"
HOTKEY_DRAW = "ctrl+shift+d"
LOCAL_KEEP_DAYS = 14


class PanelApi:
    """JS bridge for the pre-record panel."""

    def __init__(self, app: "App"):
        self.app = app

    def get_setup(self):
        settings = config.load_settings()
        try:
            dshow = devices.list_dshow_devices()
        except Exception:
            log.exception("dshow enumeration failed")
            dshow = {"cameras": [], "mics": []}
        return {
            "monitors": devices.list_monitors(),
            "cameras": dshow["cameras"],
            "mics": dshow["mics"],
            "settings": settings,
            "token_ok": bool(config.CAPTURE_TOKEN and config.SERVER_URL),
        }

    def save_setup(self, s):
        cur = config.load_settings()
        cur.update({k: s[k] for k in ("monitor", "camera", "mic", "blur")
                    if k in s})
        config.save_settings(cur)

    def start_recording(self):
        self.app.overlays.hide_panel()
        self.app.session.toggle()


class ToolbarApi:
    """JS bridge for the recording toolbar."""

    def __init__(self, app: "App"):
        self.app = app

    def get_state(self):
        return self.app.session.toolbar_state()

    def stop(self):
        self.app.session.stop()

    def pause_resume(self):
        self.app.session.pause_resume()

    def trash(self):
        self.app.session.trash()

    def restart(self):
        self.app.session.restart()

    def toggle_draw(self):
        return self.app.overlays.toggle_draw(config.load_settings()["monitor"])

    def toggle_camera(self):
        return self.app.overlays.toggle_bubble_visible()

    def toggle_blur(self):
        s = config.load_settings()
        s["blur"] = not s["blur"]
        config.save_settings(s)
        self.app.overlays.set_bubble_blur(s["blur"])
        return s["blur"]


class App:
    def __init__(self):
        self.overlays = ui.Overlays()
        self.session = session.Session(self.overlays, self.notify)
        self.tray: pystray.Icon | None = None

    # ---- tray ----

    def _tray_image(self, live: bool = False) -> Image.Image:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([8, 8, 56, 56], outline=(232, 233, 235, 255), width=5)
        d.ellipse([22, 22, 42, 42],
                  fill=(255, 69, 69, 255) if live else (232, 233, 235, 255))
        return img

    def notify(self, title: str, msg: str):
        log.info("toast: %s — %s", title, msg)
        try:
            if self.tray:
                self.tray.notify(msg, title)
        except Exception:
            pass

    def run_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open panel",
                             lambda: self.overlays.show_panel(), default=True),
            pystray.MenuItem(f"Start/stop recording ({HOTKEY_RECORD})",
                             lambda: self.session.toggle()),
            pystray.MenuItem("Quit", self.quit),
        )
        self.tray = pystray.Icon("DragonRecorder", self._tray_image(),
                                 "DragonRecorder", menu)
        threading.Thread(target=self._tray_state_loop, daemon=True).start()
        self.tray.run()   # runs on its own thread

    def _tray_state_loop(self):
        was_live = False
        while True:
            live = self.session.state in (session.State.RECORDING,
                                          session.State.PAUSED)
            if live != was_live and self.tray:
                self.tray.icon = self._tray_image(live)
                was_live = live
            time.sleep(0.5)

    def quit(self):
        if self.session.state in (session.State.RECORDING, session.State.PAUSED):
            self.session.stop()
            time.sleep(1)
        if self.tray:
            self.tray.stop()
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass

    # ---- background workers ----

    def render_job_loop(self):
        from . import processing
        while True:
            time.sleep(300)
            try:
                processing.poll_render_jobs()
            except Exception:
                log.exception("render job poll failed")

    def cleanup_old_takes(self):
        cutoff = time.time() - LOCAL_KEEP_DAYS * 86400
        for d in config.RECORDINGS_DIR.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    # ---- wiring ----

    def on_started(self):
        """Runs once pywebview's event loop is live."""
        self.overlays.ensure_toolbar(ToolbarApi(self))
        self.overlays.ensure_countdown()
        if config.load_settings()["camera"]:
            self.overlays.ensure_bubble()
        keyboard.add_hotkey(HOTKEY_RECORD, self.session.toggle)
        keyboard.add_hotkey(
            HOTKEY_DRAW,
            lambda: self.overlays.toggle_draw(config.load_settings()["monitor"]))
        threading.Thread(target=self.run_tray, daemon=True).start()
        threading.Thread(target=self.render_job_loop, daemon=True).start()
        threading.Thread(target=self.cleanup_old_takes, daemon=True).start()

    def main(self):
        self.overlays.create_panel(PanelApi(self))
        webview.start(self.on_started, gui="edgechromium", debug=False,
                      private_mode=False)


if __name__ == "__main__":
    App().main()
