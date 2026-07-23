"""Take lifecycle: the state machine between hotkeys, overlay windows,
ffmpeg, the clipboard, and the server.

The invariant that matters: the share link hits the clipboard the instant
recording stops. The slug is minted (in the background) when recording
starts, so stop only has to copy a string. Upload and all processing happen
after, in worker threads.
"""

import logging
import shutil
import threading
import time
import uuid
from enum import Enum, auto

import pyperclip

from . import api, config, recorder

log = logging.getLogger("dr.session")


class State(Enum):
    IDLE = auto()
    COUNTDOWN = auto()
    RECORDING = auto()
    PAUSED = auto()
    FINISHING = auto()


class Session:
    """Singleton owning the current take. UI methods are thin and re-entrant;
    everything slow runs in daemon threads."""

    def __init__(self, ui, notify):
        self.ui = ui                    # ui.Overlays instance
        self.notify = notify            # notify(title, msg) -> tray toast
        self.state = State.IDLE
        self.rec: recorder.Recorder | None = None
        self.slug: str | None = None
        self._slug_box: dict = {"slug": None}
        self._slug_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._countdown_cancel = threading.Event()

    # ---- start/stop entry point (the record hotkey) ----

    def toggle(self):
        with self._lock:
            if self.state == State.IDLE:
                self._begin()
            elif self.state == State.COUNTDOWN:
                self._cancel_countdown()
            elif self.state in (State.RECORDING, State.PAUSED):
                self.stop()

    def _begin(self, countdown_s: int = 3):
        settings = config.load_settings()
        self.state = State.COUNTDOWN
        self.slug = None
        self._slug_box = {"slug": None}   # one box per take, so a cancelled
        self._countdown_cancel.clear()    # take can never release a newer slug
        # mint in parallel with the countdown — never delays anything
        self._slug_thread = threading.Thread(
            target=self._mint, args=(self._slug_box,), daemon=True)
        self._slug_thread.start()
        take_dir = config.RECORDINGS_DIR / time.strftime("%Y%m%d-%H%M%S")
        self.rec = recorder.Recorder(
            take_dir, settings["monitor"], settings["mic"],
            settings.get("fps", 30))
        if countdown_s > 0:
            self.ui.show_countdown(settings["monitor"], countdown_s)
            threading.Thread(target=self._countdown_then_start,
                             args=(countdown_s,), daemon=True).start()
        else:
            threading.Thread(target=self._start_recording, daemon=True).start()

    def _mint(self, box: dict):
        slug = api.mint_slug()
        if slug is None:
            # Server unreachable: mint locally so the flow (and clipboard)
            # still works; a real slug is re-minted at upload time.
            slug = "local-" + uuid.uuid4().hex[:10]
            log.warning("server unreachable, using local slug %s", slug)
        box["slug"] = slug
        if box is self._slug_box:
            self.slug = slug

    def _countdown_then_start(self, seconds: int):
        for remaining in range(seconds, 0, -1):
            self.ui.set_countdown(remaining)
            if self._countdown_cancel.wait(1.0):
                return
        self.ui.hide_countdown()
        self._start_recording()

    def _cancel_countdown(self):
        self._countdown_cancel.set()
        self.ui.hide_countdown()
        self._release_slug_async()
        self.state = State.IDLE

    def _start_recording(self):
        with self._lock:
            if self._countdown_cancel.is_set():
                return
            settings = config.load_settings()
            try:
                self.rec.start_segment()
            except recorder.FfmpegDied as exc:
                log.error("could not start capture: %s", exc)
                self.state = State.IDLE
                self.ui.hide_countdown()
                self.notify("Recording failed to start",
                            "ffmpeg could not open the capture. See ffmpeg.log.")
                api.report_failure(f"recording failed to start: {exc}")
                self._release_slug_async()
                return
            self.state = State.RECORDING
            self.ui.show_toolbar(settings["monitor"])
            if settings["camera"]:
                self.ui.show_bubble(settings["monitor"], settings["camera"],
                                    settings["blur"])
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self):
        """Notice ffmpeg dying mid-take."""
        rec = self.rec
        while self.state in (State.RECORDING, State.PAUSED) and rec is self.rec:
            if self.state == State.RECORDING and rec.proc and not rec.alive():
                log.error("ffmpeg died mid-recording")
                api.report_failure("ffmpeg died mid-capture: " + rec._tail_log())
                self.notify("Recording hiccup",
                            "Capture died — stopping and salvaging the take.")
                self.stop()
                return
            time.sleep(1.0)

    # ---- toolbar controls ----

    def pause_resume(self):
        with self._lock:
            if self.state == State.RECORDING:
                self.rec.stop_segment()
                self.state = State.PAUSED
            elif self.state == State.PAUSED:
                try:
                    self.rec.start_segment()
                    self.state = State.RECORDING
                except recorder.FfmpegDied as exc:
                    self.notify("Resume failed", "Could not restart capture.")
                    api.report_failure(f"resume failed: {exc}")

    def stop(self):
        with self._lock:
            if self.state not in (State.RECORDING, State.PAUSED):
                return
            self.state = State.FINISHING
            rec, slug = self.rec, None
            # link on the clipboard *now* — before any upload
            if self._slug_thread:
                self._slug_thread.join(timeout=3)
            slug = self.slug
            url = api.share_url(slug)
            try:
                pyperclip.copy(url)
            except Exception:
                log.exception("clipboard copy failed")
            self.ui.hide_toolbar()
            self.ui.hide_bubble()
            self.ui.show_panel()   # ready for the next take, Loom-style
            self.notify("Link copied", url)
        threading.Thread(target=self._finish_and_upload, args=(rec, slug),
                         daemon=True).start()

    def _finish_and_upload(self, rec: recorder.Recorder, slug: str):
        try:
            final = rec.finish()
        except recorder.FfmpegDied as exc:
            log.error("finish failed: %s", exc)
            self.notify("Recording failed", "No video was produced.")
            api.report_failure(f"recording produced no file: {exc}")
            api.trash(slug)
            self.state = State.IDLE
            return
        finally:
            with self._lock:
                if self.rec is rec:
                    self.state = State.IDLE
        duration = recorder.probe_duration(final)
        if slug.startswith("local-"):
            # server was down at start; try once more for a real slug
            real = api.mint_slug()
            if real:
                slug = real
                pyperclip.copy(api.share_url(slug))
                self.notify("Link updated", api.share_url(slug))
            else:
                self.notify("Upload failed", "Server unreachable — recording "
                            f"kept locally at {final}")
                api.report_failure("upload skipped, server unreachable")
                return
        # remember which slug this take belongs to (render jobs look it up)
        (final.parent / "slug.txt").write_text(slug, "utf-8")
        ok = api.upload_video(slug, final, duration)
        if not ok:
            self.notify("Upload failed",
                        f"Kept locally at {final}. It will not retry.")
            api.report_failure(f"upload failed for {slug}")
            return
        self.notify("Recording is live", api.share_url(slug))
        # post-link processing: transcript, title, thumbnail, edits, renders
        from . import processing
        try:
            processing.run_pipeline(slug, final)
        except Exception:
            log.exception("processing pipeline failed")
            api.report_failure(f"processing pipeline failed for {slug}")

    def trash(self):
        """Kill the take: no link, no upload, slug released, files deleted."""
        with self._lock:
            if self.state not in (State.RECORDING, State.PAUSED):
                return
            rec, slug = self.rec, self.slug
            self.state = State.IDLE
            self.rec = None
            rec.abort()
            self.ui.hide_toolbar()
            self.ui.hide_bubble()
            self.ui.show_panel()
        threading.Thread(target=self._cleanup_take, args=(rec, slug),
                         daemon=True).start()
        self.notify("Take trashed", "No link, nothing uploaded.")

    def restart(self):
        """Trash the current take and immediately begin a new one."""
        with self._lock:
            if self.state not in (State.RECORDING, State.PAUSED):
                return
            rec, slug = self.rec, self.slug
            self.rec = None
            rec.abort()
        threading.Thread(target=self._cleanup_take, args=(rec, slug),
                         daemon=True).start()
        self.state = State.IDLE
        self._begin(countdown_s=0)

    def _cleanup_take(self, rec: recorder.Recorder, slug: str | None):
        time.sleep(0.5)  # let ffmpeg release file handles
        shutil.rmtree(rec.take_dir, ignore_errors=True)
        if slug and not slug.startswith("local-"):
            api.trash(slug)

    def _release_slug_async(self):
        box, thread = self._slug_box, self._slug_thread
        self.slug = None

        def rel():
            if thread:
                thread.join(timeout=5)
            s = box.get("slug")
            if s and not s.startswith("local-"):
                api.trash(s)
        threading.Thread(target=rel, daemon=True).start()

    # ---- state for the toolbar's poll ----

    def toolbar_state(self) -> dict:
        return {
            "state": self.state.name,
            "elapsed": self.rec.elapsed() if self.rec else 0,
        }
