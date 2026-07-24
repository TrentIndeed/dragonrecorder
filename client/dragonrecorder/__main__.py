"""DragonRecorder tray app entry point.

Threading: pywebview owns the main thread; the tray icon, global hotkeys,
and the render-job poller run in daemon threads. Overlay windows are created
once and shown/hidden per take.
"""

import logging
import os
import shutil
import threading
import time

# WebView2 flags for our own windows only:
# - auto-grant camera/mic (pywebview has no PermissionRequested handler, so
#   the prompt can't render in a frameless window and capture fails)
# - no HTTP cache, so overlay pages always load fresh CSS/JS after updates
os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                      "--use-fake-ui-for-media-stream --disable-http-cache")

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

def _best_mic(mics: list) -> str:
    """Prefer an actual microphone over line-ins when auto-picking."""
    for m in mics:
        if "microphone" in m.lower():
            return m
    return mics[0]


HOTKEY_RECORD = config.HOTKEY_RECORD
HOTKEY_DRAW = config.HOTKEY_DRAW
LOCAL_KEEP_DAYS = 14


class PanelApi:
    """JS bridge for the pre-record panel."""

    def __init__(self, app: "App"):
        self._app = app

    def get_setup(self):
        settings = config.load_settings()
        try:
            dshow = devices.list_dshow_devices()
        except Exception:
            log.exception("dshow enumeration failed")
            dshow = {"cameras": [], "mics": []}
        # auto-pick a microphone when none is set (or the saved one is gone)
        if dshow["mics"] and settings["mic"] not in dshow["mics"]:
            settings["mic"] = _best_mic(dshow["mics"])
            config.save_settings(settings)
        return {
            "monitors": devices.list_monitors(),
            "cameras": dshow["cameras"],
            "mics": dshow["mics"],
            "settings": settings,
            "token_ok": bool(config.CAPTURE_TOKEN and config.SERVER_URL),
            "hotkeys": {"record": HOTKEY_RECORD, "draw": HOTKEY_DRAW},
        }

    def save_setup(self, s):
        cur = config.load_settings()
        old_cam, old_blur = cur["camera"], cur["blur"]
        old_mon = cur["monitor"]
        old_shape = cur.get("bubble_shape", "rect")
        cur.update({k: s[k] for k in ("monitor", "camera", "mic", "blur",
                                      "bubble_shape") if k in s})
        if cur["monitor"] != old_mon:
            # forget the dragged position: it belongs to the old monitor
            cur["bubble_x"] = cur["bubble_y"] = None
        config.save_settings(cur)
        # keep the live preview in sync while the panel is open
        ov = self._app.overlays
        if getattr(ov, "_panel_visible", False):
            if cur["monitor"] != old_mon:
                # card + webcam follow the chosen screen
                ov.show_panel()
                return
            shape_changed = cur.get("bubble_shape", "rect") != old_shape
            if cur["camera"] != old_cam or shape_changed:
                ov.hide_bubble()
                if cur["camera"]:
                    ov.show_bubble(cur["monitor"], cur["camera"], cur["blur"])
            elif cur["blur"] != old_blur and cur["camera"]:
                ov.set_bubble_blur(cur["blur"])

    def start_recording(self):
        self._app.overlays.hide_panel()
        self._app.session.toggle()

    def hide_panel(self):
        self._app.overlays.hide_panel()

    def quit_app(self):
        # the card's X closes the whole app (tray, bridge, processing) —
        # in a thread so the JS call returns before windows are destroyed
        threading.Thread(target=self._app.quit, daemon=True).start()

    def open_dashboard(self):
        import webbrowser
        webbrowser.open(f"{config.SERVER_URL}/dash")


class ToolbarApi:
    """JS bridge for the recording toolbar."""

    def __init__(self, app: "App"):
        self._app = app

    def get_state(self):
        return self._app.session.toolbar_state()

    def stop(self):
        self._app.session.stop()

    def pause_resume(self):
        self._app.session.pause_resume()

    def trash(self):
        self._app.session.trash()

    def restart(self):
        self._app.session.restart()

    def toggle_draw(self):
        return self._app.overlays.toggle_draw(config.load_settings()["monitor"])

    def toggle_camera(self):
        return self._app.overlays.toggle_bubble_visible()

    def toggle_blur(self):
        s = config.load_settings()
        s["blur"] = not s["blur"]
        config.save_settings(s)
        self._app.overlays.set_bubble_blur(s["blur"])
        return s["blur"]


class App:
    def __init__(self):
        self.overlays = ui.Overlays()
        self.session = session.Session(self.overlays, self.notify)
        self.overlays.recording_check = lambda: self.session.state in (
            session.State.RECORDING, session.State.PAUSED)
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
        # graceful stop so a take in progress still gets its link/upload
        if self.session.state in (session.State.RECORDING, session.State.PAUSED):
            self.session.stop()
            time.sleep(1.5)
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass
        # hard exit: guarantees the bridge, any whisper transcription, and
        # orphaned ffmpeg render children all stop when the card is closed
        os._exit(0)

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

    def on_record_hotkey(self):
        """Idle: the hotkey opens/closes the launcher panel (recording starts
        from its button, like Loom). Countdown: cancels. Recording: stops."""
        if self.session.state == session.State.IDLE:
            self.overlays.toggle_panel()
        else:
            self.overlays.hide_panel()
            self.session.toggle()

    def on_started(self):
        """Runs once pywebview's event loop is live."""
        self.overlays.ensure_toolbar(ToolbarApi(self))
        self.overlays.ensure_countdown()
        if config.load_settings()["camera"]:
            self.overlays.ensure_bubble()
        keyboard.add_hotkey(HOTKEY_RECORD, self.on_record_hotkey)
        keyboard.add_hotkey(
            HOTKEY_DRAW,
            lambda: self.overlays.toggle_draw(config.load_settings()["monitor"]))
        def auto_mic():
            try:
                dshow = devices.list_dshow_devices()
                s = config.load_settings()
                if dshow["mics"] and s["mic"] not in dshow["mics"]:
                    s["mic"] = _best_mic(dshow["mics"])
                    config.save_settings(s)
                    log.info("auto-selected microphone: %s", s["mic"])
            except Exception:
                log.exception("mic auto-pick failed")
        threading.Thread(target=auto_mic, daemon=True).start()
        threading.Thread(target=self.run_tray, daemon=True).start()
        threading.Thread(target=self.render_job_loop, daemon=True).start()
        threading.Thread(target=self.cleanup_old_takes, daemon=True).start()
        # web dashboard "Record a video" button → open the launcher panel
        from . import bridge
        bridge.start(self.overlays.show_panel, self.overlays.hide_panel,
                     lambda: {"state": self.session.state.name,
                              "pid": os.getpid()})

    def main(self):
        from . import bridge
        if bridge.poke_existing():
            log.info("already running — opened the existing panel instead")
            return
        self.overlays.create_panel(PanelApi(self))
        self.overlays.ensure_toolbar(ToolbarApi(self))
        self.overlays.ensure_countdown()
        self.overlays.ensure_bubble()
        webview.start(self.on_started, gui="edgechromium", debug=False,
                      private_mode=False)


if __name__ == "__main__":
    App().main()
