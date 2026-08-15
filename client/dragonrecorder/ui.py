"""Overlay window management on pywebview (WebView2).

Three window classes, distinguished only by whether capture sees them:
- bubble + draw overlay: real windows meant to be captured
- toolbar + countdown: WDA_EXCLUDEFROMCAPTURE — visible to the operator only
The exclusion is applied once at window creation; windows are then shown and
hidden, never recreated, so the affinity flag sticks.
"""

import ctypes
import json
import logging
import threading

import webview

from . import config, devices, winapi

log = logging.getLogger("dr.ui")


def _dpi_scale() -> float:
    """pywebview makes the process DPI-aware, so window sizes are physical
    pixels while page CSS renders at the display scale — windows must be
    scaled up or their content is clipped at >100% display scaling.
    Awareness must be declared BEFORE reading the DPI or Windows lies and
    reports 96; pywebview sets the same awareness again later (idempotent)."""
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


S = _dpi_scale()
TOOLBAR_W, TOOLBAR_H = int(60 * S), int(430 * S)
BUBBLE = int(358 * S)                       # circle shape (square window)
BUBBLE_RECT_W, BUBBLE_RECT_H = int(488 * S), int(274 * S)   # 16:9 fallback
COUNTDOWN = int(180 * S)
PANEL_W, PANEL_H = int(336 * S), int(710 * S)   # tall enough for the tune rows
PANEL_MARGIN = int(14 * S)


def _url(name: str) -> str:
    # plain file URI — NO query string: pywebview mangles ?v= into the path
    # and WebView2 then 404s every overlay page ("file not found" white box).
    # Staleness is handled by --disable-http-cache in the browser args.
    return (config.UI_DIR / name).as_uri()


class Overlays:
    def __init__(self):
        self.panel = None
        self.toolbar = None
        self.countdown = None
        self.bubble = None
        self.draw = None
        self.draw_mode = False
        self._draw_hwnd = 0
        self._lock = threading.Lock()
        self._hwnds: dict[str, int] = {}
        self._shown: set[str] = set()   # titles shown at least once
        # set by App: () -> bool, true while a take is recording/paused
        self.recording_check = None

    def _hwnd(self, title: str) -> int:
        h = self._hwnds.get(title, 0)
        if not h or not winapi.user32.IsWindow(h):
            h = winapi.find_window(title, timeout_s=3)
            self._hwnds[title] = h
        return h

    def _pin(self, title: str, x: int, y: int, w: int, h: int,
             window=None) -> None:
        """Enforce exact geometry + topmost after showing a window. The
        managed resize jiggle forces WinForms resize events through to the
        WebView2 controller — without it the composition surface can stay at
        its 768x768 default and the page never reaches the screen."""
        self._shown.add(title)
        hwnd = self._hwnd(title)
        # raw size jiggle: an actual WM_SIZE after WebView2's controller is
        # up is what makes the WinForms control push its bounds into the
        # controller (this is why the bubble worked: its show changes size)
        winapi.force_rect_topmost(hwnd, x, y, w, h - 2)
        winapi.force_rect_topmost(hwnd, x, y, w, h)

    def _shape_by_css(self, title: str) -> None:
        """Let the page's CSS define the shape and key out everything else.

        Any leftover region has to go first: a region wins over the colour
        key and composites its excluded area as opaque white.
        """
        hwnd = self._hwnd(title)
        winapi.clear_region(hwnd)
        winapi.dwm_no_round(hwnd)
        winapi.set_colorkey_transparent(hwnd)

    def _hide_soon(self, window, title: str) -> None:
        """transparent=True windows ignore hidden=True at creation — hide
        them for real once their hwnd exists. WebView2 windows can take many
        seconds to realize, so retry until the hide verifiably sticks."""
        def run():
            import time
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if title in self._shown:   # deliberately shown meanwhile
                    return
                h = winapi.find_window(title, timeout_s=2)
                if h:
                    self._hwnds[title] = h
                    winapi.hide_window(h)
                    time.sleep(0.3)
                    if not winapi.user32.IsWindowVisible(h):
                        return
                time.sleep(0.5)
            log.warning("could not hide %s after creation", title)
        threading.Thread(target=run, daemon=True).start()

    # ---- panel (pre-record launcher, Loom-style, top-right) ----

    def create_panel(self, js_api) -> "webview.Window":
        x, y = self._panel_pos()
        # Opaque + win32 rounded region: pywebview/WebView2 per-pixel
        # transparency is unreliable (white boxes, blank windows) — shaped
        # opaque windows are not.
        self.panel = webview.create_window(
            "DR-Panel", _url("panel.html"), js_api=js_api,
            x=-4000, y=0, width=PANEL_W, height=PANEL_H, frameless=True,
            resizable=False, on_top=True, background_color="#17181c",
            easy_drag=False)
        self._panel_visible = False
        # NOT capture-excluded: the panel hides before recording starts, so
        # it can never appear in a video — and staying capturable keeps it
        # visible to remote-stream operators
        self._exclude_later("DR-Panel", exclude=False)
        return self.panel

    def _panel_pos(self) -> tuple[int, int]:
        geo = devices.monitor_geometry(config.load_settings()["monitor"])
        return (geo["left"] + geo["width"] - PANEL_W - PANEL_MARGIN,
                geo["top"] + PANEL_MARGIN)

    def show_panel(self):
        if self.panel:
            x, y = self._panel_pos()
            self.panel.show()
            self._pin("DR-Panel", x, y, PANEL_W, PANEL_H, self.panel)
            winapi.clear_region(self._hwnd("DR-Panel"))
            winapi.dwm_round_corners(self._hwnd("DR-Panel"))
            try:   # refresh screen previews each time the card opens
                self.panel.evaluate_js("window.onPanelShown && onPanelShown()")
            except Exception:
                pass
            self._panel_visible = True
            # Loom behavior: opening the capture panel also shows the webcam
            # preview bubble bottom-left, before any recording starts
            s = config.load_settings()
            if s["camera"]:
                self.show_bubble(s["monitor"], s["camera"], s["blur"])

    def hide_panel(self, keep_bubble: bool = False):
        if self.panel:
            try:   # stop the in-card camera preview
                self.panel.evaluate_js("window.onPanelHidden && onPanelHidden()")
            except Exception:
                pass
            winapi.park(self._hwnd("DR-Panel"))
            self._panel_visible = False
            # panel dismissed without recording → drop the preview too,
            # unless a recording is about to start (bubble stays persistent)
            if not keep_bubble and not (self.recording_check
                                        and self.recording_check()):
                self.hide_bubble()

    def set_panel_link(self, url: str) -> None:
        try:
            self.panel.evaluate_js(
                f"window.setLastLink && setLastLink({json.dumps(url)})")
        except Exception:
            pass

    def toggle_panel(self):
        if getattr(self, "_panel_visible", False):
            self.hide_panel()
        else:
            self.show_panel()

    # ---- toolbar (capture-excluded) ----

    def ensure_toolbar(self, js_api):
        with self._lock:
            if self.toolbar:
                return
            self.toolbar = webview.create_window(
                "DR-Toolbar", _url("toolbar.html"), js_api=js_api,
                x=-4000, y=0,
                width=TOOLBAR_W, height=TOOLBAR_H, frameless=True,
                on_top=True, resizable=False, hidden=True, focus=False,
                easy_drag=False, background_color="#101114")
            self._exclude_later("DR-Toolbar")

    def show_toolbar(self, monitor: int):
        # Loom-style: vertical bar on the left edge, vertically centered
        geo = devices.monitor_geometry(monitor)
        x = geo["left"] + int(14 * S)
        y = geo["top"] + (geo["height"] - TOOLBAR_H) // 2
        self.toolbar.show()
        self._pin("DR-Toolbar", x, y, TOOLBAR_W, TOOLBAR_H, self.toolbar)
        winapi.dwm_round_corners(self._hwnd("DR-Toolbar"))

    def hide_toolbar(self):
        if self.toolbar:
            self.toolbar.hide()
            winapi.hide_window(self._hwnd("DR-Toolbar"))

    # ---- countdown (capture-excluded) ----

    def ensure_countdown(self):
        with self._lock:
            if self.countdown:
                return
            self.countdown = webview.create_window(
                "DR-Countdown", _url("countdown.html"),
                x=-4000, y=0,
                width=COUNTDOWN, height=COUNTDOWN, frameless=True,
                on_top=True, resizable=False, focus=False,
                easy_drag=False, transparent=True,
                background_color="#0d0e11")
            # not excluded: the countdown finishes before ffmpeg starts
            self._exclude_later("DR-Countdown", exclude=False)

    def show_countdown(self, monitor: int, seconds: int):
        self.ensure_countdown()
        geo = devices.monitor_geometry(monitor)
        x = geo["left"] + (geo["width"] - COUNTDOWN) // 2
        y = geo["top"] + (geo["height"] - COUNTDOWN) // 2
        self.set_countdown(seconds)
        self.countdown.show()
        self._pin("DR-Countdown", x, y, COUNTDOWN, COUNTDOWN, self.countdown)
        self._shape_by_css("DR-Countdown")

    def set_countdown(self, n: int):
        if self.countdown:
            self.countdown.evaluate_js(f"setCount({n})")

    def hide_countdown(self):
        if self.countdown:
            winapi.park(self._hwnd("DR-Countdown"))

    # ---- webcam bubble (captured on purpose) ----

    def ensure_bubble(self):
        with self._lock:
            if self.bubble:
                return
            # transparent: the round/rounded shape is drawn by CSS. Window
            # regions are not an option here — see bubble.html.
            self.bubble = webview.create_window(
                "DR-Bubble", _url("bubble.html"),
                x=-4000, y=0,
                width=BUBBLE, height=BUBBLE, frameless=True, on_top=True,
                resizable=False, focus=False, transparent=True,
                background_color="#0e0f12", easy_drag=True)
            # not capture-excluded (the bubble is recorded on purpose), but
            # it must stay out of the taskbar/alt-tab like every overlay
            self._exclude_later("DR-Bubble", exclude=False)

            def moved(*_):
                if self.bubble.x <= -3000:   # parked off-screen, not a drag
                    return
                s = config.load_settings()
                s["bubble_x"] = self.bubble.x
                s["bubble_y"] = self.bubble.y
                config.save_settings(s)
            self.bubble.events.moved += moved

    def show_bubble(self, monitor: int, camera: str, blur: bool):
        self.ensure_bubble()
        s = config.load_settings()
        shape = s.get("bubble_shape", "rect")
        # invalidate any pending aspect-fit from a previous show — a stale
        # rect fit landing after a switch to circle causes geometry mismatch
        self._aspect_token = object()
        if shape == "circle":
            w = h = BUBBLE
            css_w = css_h = 358
        else:
            w, h = BUBBLE_RECT_W, BUBBLE_RECT_H
            css_w, css_h = 488, 274
        geo = devices.monitor_geometry(monitor)
        m = int(32 * S)
        x = s["bubble_x"] if s["bubble_x"] is not None else geo["left"] + m
        y = (s["bubble_y"] if s["bubble_y"] is not None
             else geo["top"] + geo["height"] - h - m)
        self.bubble.evaluate_js(f"setShape({css_w}, {css_h})")
        self.bubble.show()
        self._pin("DR-Bubble", x, y, w, h, self.bubble)
        self._shape_by_css("DR-Bubble")
        self._bubble_visible = True
        cam_js = camera.replace("\\", "\\\\").replace("'", "\\'")
        self.bubble.evaluate_js(
            f"startCamera('{cam_js}', {str(blur).lower()}, "
            f"{int(s.get('blur_strength', 6))})")

    def set_bubble_blur(self, blur: bool, strength: int | None = None):
        if self.bubble:
            if strength is None:
                strength = int(config.load_settings().get("blur_strength", 6))
            self.bubble.evaluate_js(
                f"setBlur({str(blur).lower()}, {int(strength)})")

    def toggle_bubble_visible(self) -> bool:
        """Camera on/off mid-recording. Returns new visibility."""
        if not self.bubble:
            return False
        if getattr(self, "_bubble_visible", True):
            self.bubble.hide()
            self._bubble_visible = False
        else:
            self.bubble.show()
            self._bubble_visible = True
        return self._bubble_visible

    def hide_bubble(self):
        if self.bubble:
            try:
                self.bubble.evaluate_js("stopCamera()")
            except Exception:
                log.warning("stopCamera evaluate failed", exc_info=True)
            winapi.park(self._hwnd("DR-Bubble"))
            self._bubble_visible = False

    # ---- drawing overlay (captured on purpose) ----

    def ensure_draw(self, monitor: int):
        with self._lock:
            if self.draw:
                return
            geo = devices.monitor_geometry(monitor)
            self.draw = webview.create_window(
                "DR-Draw", _url("draw.html"),
                x=geo["left"], y=geo["top"],
                width=geo["width"], height=geo["height"] - 1,
                frameless=True, on_top=True, resizable=False, hidden=True,
                transparent=True, focus=False)

            def setup():
                self._draw_hwnd = winapi.find_window("DR-Draw")
                winapi.set_toolwindow(self._draw_hwnd)
            threading.Thread(target=setup, daemon=True).start()

    def toggle_draw(self, monitor: int) -> bool:
        """Draw mode: window absorbs mouse input. Off: click-through."""
        self.ensure_draw(monitor)
        self.draw_mode = not self.draw_mode
        if self.draw_mode:
            geo = devices.monitor_geometry(monitor)
            self.draw.show()
            self._pin("DR-Draw", geo["left"], geo["top"],
                      geo["width"], geo["height"] - 1)
            winapi.set_click_through(self._draw_hwnd, False)
            self.draw.evaluate_js("setActive(true)")
        else:
            winapi.set_click_through(self._draw_hwnd, True)
            self.draw.evaluate_js("setActive(false)")
        return self.draw_mode

    def hide_draw(self):
        if self.draw:
            self.draw.hide()
            self.draw_mode = False

    # ---- helpers ----

    def _exclude_later(self, title: str, exclude: bool = True):
        def run():
            hwnd = winapi.find_window(title)
            if exclude and config.CAPTURE_EXCLUDE:
                if not winapi.exclude_from_capture(hwnd):
                    log.error("capture exclusion FAILED for %s — it would "
                              "appear in recordings", title)
            elif exclude:
                log.info("CAPTURE_EXCLUDE=0: %s will appear in recordings",
                         title)
            winapi.set_toolwindow(hwnd)
        threading.Thread(target=run, daemon=True).start()
