"""DragonRecorder tray app entry point.

Threading: pywebview owns the main thread; the tray icon, global hotkeys,
and the render-job poller run in daemon threads. Overlay windows are created
once and shown/hidden per take.
"""

import io
import logging
import os
import shutil
import sys
import threading
import time

# WebView2 flags for our own windows only:
# - auto-grant camera/mic (pywebview has no PermissionRequested handler, so
#   the prompt can't render in a frameless window and capture fails)
# - no HTTP cache, so overlay pages always load fresh CSS/JS after updates
# --disable-direct-composition is what makes the round webcam bubble
# possible: WebView2 normally composites through a DirectComposition visual
# that SetWindowRgn cannot clip, so the window kept painting a white square
# around the circle no matter how the region was set. The legacy path draws
# into the window's redirection surface, which the region does clip. These
# overlays are a few hundred pixels each, so the compositing cost is moot.
os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                      "--use-fake-ui-for-media-stream --disable-http-cache "
                      "--disable-direct-composition")

import keyboard
import pystray
import webview
from PIL import Image, ImageDraw

from . import api, config, devices, recorder, session, ui

# A log line took down a whole recording's processing: the console stream is
# cp1252 here, an INFO message contained an arrow, and the UnicodeEncodeError
# propagated out of log.info() and aborted run_pipeline - no title, no
# captions, no edits. Two guards: write UTF-8 with replacement everywhere,
# and never let a logging failure raise into application code.
logging.raiseExceptions = False
_stream = logging.StreamHandler(
    io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace",
                     line_buffering=True)
    if hasattr(sys.stderr, "buffer") else sys.stderr)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(config.APPDATA_DIR / "client.log", encoding="utf-8",
                            errors="replace"),
        _stream,
    ],
)
log = logging.getLogger("dr.main")

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
        # follow the Windows default device while the pick is automatic
        if dshow["mics"] and (settings.get("mic_auto", True)
                              or settings["mic"] not in dshow["mics"]):
            settings["mic"] = devices.pick_default_mic(dshow["mics"])
            settings["mic_auto"] = True
            config.save_settings(settings)
        return {
            "monitors": devices.list_monitors(),
            "cameras": dshow["cameras"],
            "mics": dshow["mics"],
            "settings": settings,
            "token_ok": bool(config.CAPTURE_TOKEN and config.SERVER_URL),
            "hotkeys": {"record": HOTKEY_RECORD, "draw": HOTKEY_DRAW},
        }

    def get_previews(self):
        """Downscaled screenshots of every monitor for the screen picker."""
        import base64
        import io

        import mss
        previews = {}
        try:
            with mss.mss() as sct:
                for i, mon in enumerate(sct.monitors[1:], start=1):
                    shot = sct.grab(mon)
                    img = Image.frombytes("RGB", shot.size, shot.bgra,
                                          "raw", "BGRX")
                    img.thumbnail((300, 300))
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=55)
                    previews[str(i)] = ("data:image/jpeg;base64,"
                                        + base64.b64encode(buf.getvalue()).decode())
        except Exception:
            log.exception("monitor previews failed")
        return previews

    def save_setup(self, s):
        cur = config.load_settings()
        old_cam, old_blur = cur["camera"], cur["blur"]
        old_mon = cur["monitor"]
        old_shape = cur.get("bubble_shape", "rect")
        old_mic = cur["mic"]
        old_strength = cur.get("blur_strength", 3)
        old_scale = cur.get("bubble_scale", 100)
        cur.update({k: s[k] for k in config.PANEL_KEYS if k in s})
        if "mic" in s and s["mic"] != old_mic:
            # an explicit pick in the card stops the follow-the-default logic
            cur["mic_auto"] = False
        if cur["monitor"] != old_mon:
            # forget the dragged position: it belongs to the old monitor
            cur["bubble_x"] = cur["bubble_y"] = None
        config.save_settings(cur)
        log.info("settings saved: %s", {k: cur[k] for k in config.PANEL_KEYS})
        # keep the live preview in sync while the panel is open
        ov = self._app.overlays
        if getattr(ov, "_panel_visible", False):
            if cur["monitor"] != old_mon:
                # card + webcam follow the chosen screen
                ov.show_panel()
                return
            shape_changed = (cur.get("bubble_shape", "rect") != old_shape
                             or cur.get("bubble_scale", 100) != old_scale)
            if cur["camera"] != old_cam or shape_changed:
                ov.hide_bubble()
                if cur["camera"]:
                    ov.show_bubble(cur["monitor"], cur["camera"], cur["blur"])
            elif cur["camera"] and (cur["blur"] != old_blur
                                    or cur.get("blur_strength") != old_strength):
                ov.set_bubble_blur(cur["blur"], cur.get("blur_strength", 3))

    def start_recording(self):
        # keep the webcam preview up — it becomes the recorded bubble
        self._app.overlays.hide_panel(keep_bubble=True)
        self._app.session.toggle()

    def hide_panel(self):
        self._app.overlays.hide_panel()

    def preview_bubble_scale(self, pct):
        """Live feedback while the size slider is being dragged; the value is
        persisted separately when the drag ends."""
        try:
            self._app.overlays.resize_bubble(int(pct))
        except Exception:
            log.exception("bubble resize preview failed")

    def panel_visible(self):
        # ground truth for the page's stream watchdog: the panel html only
        # holds camera/mic streams while the card is actually on screen
        return bool(getattr(self._app.overlays, "_panel_visible", False))

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
        log.info("toast: %s - %s", title, msg)
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

    def recover_unfinished_takes(self):
        """Finish takes the app was killed in the middle of.

        A slug is minted when recording starts, so a crash (or a kill) between
        then and the end of processing leaves a recording stuck at
        "uploading..." or sitting there with no title. The local take dir
        still has the video, so both are recoverable rather than mysteries in
        the library.
        """
        time.sleep(20)          # let the UI settle first
        cutoff = time.time() - 2 * 86400
        for d in sorted(config.RECORDINGS_DIR.iterdir()):
            try:
                marker, video = d / "slug.txt", d / "video.mp4"
                if not (d.is_dir() and marker.exists() and video.exists()):
                    continue
                if d.stat().st_mtime < cutoff or (d / "recovered.txt").exists():
                    continue
                slug = marker.read_text("utf-8").strip()
                if not slug or slug.startswith("local-"):
                    continue
                state = api.get_state(slug)
                if state is None or state.get("status") == "expired":
                    continue
                if state.get("status") == "pending":
                    log.info("recovering %s: upload never finished", slug)
                    dur = recorder.probe_duration(video)
                    if not api.upload_video(slug, video, dur):
                        log.warning("recovery upload failed for %s", slug)
                        continue
                elif state.get("title"):
                    continue        # nothing to do
                else:
                    log.info("recovering %s: uploaded but never analyzed", slug)
                from . import processing
                processing.run_pipeline(slug, video)
                (d / "recovered.txt").write_text("done", "utf-8")
            except Exception:
                log.exception("could not recover take in %s", d)

    def watch_events_loop(self):
        """Tray toast when someone finishes watching one of your recordings.

        The high-water mark is on disk, so restarting the app does not
        re-announce sessions you have already been told about.
        """
        marker = config.APPDATA_DIR / "watch_seen.txt"
        try:
            since = marker.read_text("utf-8").strip() or None
        except OSError:
            since = None
        while True:
            time.sleep(60)
            try:
                events = api.get_watch_events(since)
                for e in sorted(events, key=lambda x: x["at"]):
                    mins, secs = divmod(int(e["watched_s"]), 60)
                    self.notify(
                        f"{e['who']} watched {mins}:{secs:02d} ({e['pct']}%)",
                        e["title"])
                    since = e["at"]
                if events:
                    marker.write_text(since or "", "utf-8")
            except Exception:
                log.exception("watch event poll failed")

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
                if dshow["mics"] and (s.get("mic_auto", True)
                                      or s["mic"] not in dshow["mics"]):
                    s["mic"] = devices.pick_default_mic(dshow["mics"])
                    s["mic_auto"] = True
                    config.save_settings(s)
                    log.info("mic follows Windows default: %s", s["mic"])
            except Exception:
                log.exception("mic auto-pick failed")
        threading.Thread(target=auto_mic, daemon=True).start()
        threading.Thread(target=self.run_tray, daemon=True).start()
        threading.Thread(target=self.cleanup_old_takes, daemon=True).start()
        threading.Thread(target=self.watch_events_loop, daemon=True).start()
        threading.Thread(target=self.recover_unfinished_takes,
                         daemon=True).start()
        # web dashboard "Record a video" button → open the launcher panel
        from . import bridge

        def render_now():
            # kept so older player pages that poke this endpoint still get a
            # 200; the server renders cut edits itself now
            log.debug("render poke ignored - the server renders edits")
        bridge.start(self.overlays.show_panel, self.overlays.hide_panel,
                     lambda: {"state": self.session.state.name,
                              "pid": os.getpid()},
                     render_now)

    def main(self):
        from . import bridge
        if bridge.poke_existing():
            log.info("already running - opened the existing panel instead")
            return
        self.overlays.create_panel(PanelApi(self))
        self.overlays.ensure_toolbar(ToolbarApi(self))
        self.overlays.ensure_countdown()
        self.overlays.ensure_bubble()
        webview.start(self.on_started, gui="edgechromium", debug=False,
                      private_mode=False)


if __name__ == "__main__":
    App().main()
