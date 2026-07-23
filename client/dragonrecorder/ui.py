"""Overlay window management on pywebview (WebView2).

Three window classes, distinguished only by whether capture sees them:
- bubble + draw overlay: real windows meant to be captured
- toolbar + countdown: WDA_EXCLUDEFROMCAPTURE — visible to the operator only
The exclusion is applied once at window creation; windows are then shown and
hidden, never recreated, so the affinity flag sticks.
"""

import logging
import threading

import webview

from . import config, devices, winapi

log = logging.getLogger("dr.ui")

TOOLBAR_W, TOOLBAR_H = 460, 64
BUBBLE = 220
COUNTDOWN = 180
PANEL_W, PANEL_H = 336, 396
PANEL_MARGIN = 14


def _url(name: str) -> str:
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

    # ---- panel (pre-record launcher, Loom-style, top-right) ----

    def create_panel(self, js_api) -> "webview.Window":
        x, y = self._panel_pos()
        self.panel = webview.create_window(
            "DragonRecorder", _url("panel.html"), js_api=js_api,
            x=x, y=y, width=PANEL_W, height=PANEL_H, frameless=True,
            resizable=False, on_top=True, transparent=True,
            background_color="#17181c")
        self._panel_visible = True
        # excluded from capture like the toolbar: if it's open when recording
        # starts (countdown overlap), it must not land in the video
        self._exclude_later("DragonRecorder")
        return self.panel

    def _panel_pos(self) -> tuple[int, int]:
        geo = devices.monitor_geometry(config.load_settings()["monitor"])
        return (geo["left"] + geo["width"] - PANEL_W - PANEL_MARGIN,
                geo["top"] + PANEL_MARGIN)

    def show_panel(self):
        if self.panel:
            self.panel.move(*self._panel_pos())
            self.panel.show()
            self._panel_visible = True

    def hide_panel(self):
        if self.panel:
            self.panel.hide()
            self._panel_visible = False

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
                width=TOOLBAR_W, height=TOOLBAR_H, frameless=True,
                on_top=True, resizable=False, hidden=True, focus=False,
                background_color="#101114")
            self._exclude_later("DR-Toolbar")

    def show_toolbar(self, monitor: int):
        geo = devices.monitor_geometry(monitor)
        x = geo["left"] + (geo["width"] - TOOLBAR_W) // 2
        y = geo["top"] + geo["height"] - TOOLBAR_H - 48
        self.toolbar.move(x, y)
        self.toolbar.show()

    def hide_toolbar(self):
        if self.toolbar:
            self.toolbar.hide()

    # ---- countdown (capture-excluded) ----

    def ensure_countdown(self):
        with self._lock:
            if self.countdown:
                return
            self.countdown = webview.create_window(
                "DR-Countdown", _url("countdown.html"),
                width=COUNTDOWN, height=COUNTDOWN, frameless=True,
                on_top=True, resizable=False, hidden=True, focus=False,
                transparent=True)
            self._exclude_later("DR-Countdown")

    def show_countdown(self, monitor: int, seconds: int):
        self.ensure_countdown()
        geo = devices.monitor_geometry(monitor)
        x = geo["left"] + (geo["width"] - COUNTDOWN) // 2
        y = geo["top"] + (geo["height"] - COUNTDOWN) // 2
        self.countdown.move(x, y)
        self.set_countdown(seconds)
        self.countdown.show()

    def set_countdown(self, n: int):
        if self.countdown:
            self.countdown.evaluate_js(f"setCount({n})")

    def hide_countdown(self):
        if self.countdown:
            self.countdown.hide()

    # ---- webcam bubble (captured on purpose) ----

    def ensure_bubble(self):
        with self._lock:
            if self.bubble:
                return
            self.bubble = webview.create_window(
                "DR-Bubble", _url("bubble.html"),
                width=BUBBLE, height=BUBBLE, frameless=True, on_top=True,
                resizable=False, hidden=True, focus=False, transparent=True,
                easy_drag=True)

            def moved(*_):
                s = config.load_settings()
                s["bubble_x"] = self.bubble.x
                s["bubble_y"] = self.bubble.y
                config.save_settings(s)
            self.bubble.events.moved += moved

    def show_bubble(self, monitor: int, camera: str, blur: bool):
        self.ensure_bubble()
        s = config.load_settings()
        geo = devices.monitor_geometry(monitor)
        x = s["bubble_x"] if s["bubble_x"] is not None else geo["left"] + 32
        y = (s["bubble_y"] if s["bubble_y"] is not None
             else geo["top"] + geo["height"] - BUBBLE - 32)
        self.bubble.move(x, y)
        self.bubble.show()
        cam_js = camera.replace("\\", "\\\\").replace("'", "\\'")
        self.bubble.evaluate_js(
            f"startCamera('{cam_js}', {str(blur).lower()})")

    def set_bubble_blur(self, blur: bool):
        if self.bubble:
            self.bubble.evaluate_js(f"setBlur({str(blur).lower()})")

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
            self.bubble.evaluate_js("stopCamera()")
            self.bubble.hide()
            self._bubble_visible = True

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
            self.draw.show()
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

    def _exclude_later(self, title: str):
        def run():
            hwnd = winapi.find_window(title)
            if not winapi.exclude_from_capture(hwnd):
                log.error("capture exclusion FAILED for %s — it would appear "
                          "in recordings", title)
            winapi.set_toolwindow(hwnd)
        threading.Thread(target=run, daemon=True).start()
