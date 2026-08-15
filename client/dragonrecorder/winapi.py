"""Thin ctypes layer over the two win32 tricks the overlay system rests on:

- SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE): window is visible on the
  physical monitor but excluded from DWM-composited capture (ddagrab) AND GDI
  capture (gdigrab). Windows 10 2004+.
- WS_EX_TRANSPARENT: clicks pass through the window. Toggled on the drawing
  overlay to switch between draw mode and pass-through mode.
"""

import ctypes
import ctypes.wintypes as wt
import logging

log = logging.getLogger("dr.winapi")

user32 = ctypes.windll.user32

WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


def find_window(title: str, timeout_s: float = 5.0) -> int:
    """Locate a window by exact title (pywebview windows get unique titles)."""
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return hwnd
        time.sleep(0.1)
    return 0


def exclude_from_capture(hwnd: int) -> bool:
    if not hwnd:
        return False
    ok = bool(user32.SetWindowDisplayAffinity(wt.HWND(hwnd), WDA_EXCLUDEFROMCAPTURE))
    if not ok:
        log.error("SetWindowDisplayAffinity failed (err %d)",
                  ctypes.get_last_error())
    return ok


def set_click_through(hwnd: int, enabled: bool) -> None:
    if not hwnd:
        return
    style = user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE)
    if enabled:
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE, style)


HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040



SWP_NOSIZE = 0x0001


def park(hwnd: int) -> None:
    """Move a window far off-screen instead of hiding it. Transparent
    pywebview windows must never be SW_HIDE'd or created hidden — that skips
    the show/hide hack pywebview needs to activate transparency, leaving a
    white background. Parked windows are still 'visible' to the taskbar, so
    make sure the toolwindow style is on (idempotent fast-path inside)."""
    if hwnd:
        user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), -4000, 0,
                            0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
        set_toolwindow(hwnd)


gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2


def dwm_no_round(hwnd: int) -> None:
    """Disable DWM corner rounding — it fights SetWindowRgn shapes (the
    lingering 'white square' behind circles)."""
    if hwnd:
        pref = ctypes.c_int(DWMWCP_DONOTROUND)
        dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd),
                                     DWMWA_WINDOW_CORNER_PREFERENCE,
                                     ctypes.byref(pref), 4)


def dwm_round_corners(hwnd: int) -> None:
    """Windows 11 native rounded corners: antialiased and shadow-capable,
    unlike SetWindowRgn which clips hard and leaves white edge artifacts."""
    if hwnd:
        pref = ctypes.c_int(DWMWCP_ROUND)
        dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd),
                                     DWMWA_WINDOW_CORNER_PREFERENCE,
                                     ctypes.byref(pref), 4)


def clear_region(hwnd: int) -> None:
    if hwnd:
        user32.SetWindowRgn(wt.HWND(hwnd), None, True)


LWA_COLORKEY = 0x00000001
COLOR_BTNFACE = 15


def set_colorkey_transparent(hwnd: int, rgb: tuple[int, int, int] | None = None
                             ) -> bool:
    """Punch the host form's background colour out of a WebView2 window.

    This is the only shaping mechanism that actually works on this stack,
    and it took measuring to find:

    - SetWindowRgn clips input but composites the excluded area as opaque
      WHITE — that was the white square behind the round bubble.
    - Colour-keying a colour the *page* paints does nothing: the key applies
      to the form's own painting, not to WebView2's child surface.

    So the page is created transparent (its transparent areas expose the
    WinForms background), and that background colour is keyed out here. The
    camera image is painted by WebView2, so even a pure-white shirt can
    never be keyed away by accident.
    """
    if not hwnd:
        return False
    if rgb is None:
        # whatever the form actually paints — SystemColors.Control, which a
        # custom Windows theme can change
        c = ctypes.windll.user32.GetSysColor(COLOR_BTNFACE)
        rgb = (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)
    style = user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE)
    user32.SetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE, style | WS_EX_LAYERED)
    key = rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)   # COLORREF is 0x00BBGGRR
    ok = bool(user32.SetLayeredWindowAttributes(
        wt.HWND(hwnd), key, 0, LWA_COLORKEY))
    if not ok:
        log.error("colour key failed for hwnd %s", hwnd)
    return ok


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    r = wt.RECT()
    if not hwnd or not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def window_size(hwnd: int) -> tuple[int, int]:
    r = window_rect(hwnd)
    return (0, 0) if r is None else (r[2] - r[0], r[3] - r[1])


def hide_window(hwnd: int) -> None:
    """SW_HIDE directly — pywebview's hidden=True and .hide() are both
    unreliable for transparent windows."""
    if hwnd:
        user32.ShowWindow(wt.HWND(hwnd), 0)


SWP_NOSENDCHANGING = 0x0400


def force_rect_topmost(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """Pin a window to an exact rect and keep it topmost. pywebview's
    frameless windows come out smaller than requested (it subtracts standard
    window decorations that frameless windows don't have) and can lose their
    topmost bit across hide/show — this fixes both in one call.

    SWP_NOSENDCHANGING skips WM_WINDOWPOSCHANGING, which is where these
    windows enforce a 200px minimum width. The toolbar is 60px wide, and
    without this it silently became a 200px slab painted around a 56px bar.
    """
    if not hwnd:
        return
    user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), x, y, w, h,
                        SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOSENDCHANGING)


WS_EX_APPWINDOW = 0x00040000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4


def set_toolwindow(hwnd: int) -> None:
    """Keep overlay windows out of the taskbar and alt-tab. The taskbar only
    re-evaluates a window's button on hide→show, so setting the style on an
    already-visible window must cycle visibility (safe: callers park windows
    off-screen, so the cycle is never seen)."""
    if not hwnd:
        return
    style = user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE)
    if style & WS_EX_TOOLWINDOW and not style & WS_EX_APPWINDOW:
        return
    was_visible = user32.IsWindowVisible(wt.HWND(hwnd))
    if was_visible:
        user32.ShowWindow(wt.HWND(hwnd), SW_HIDE)
    user32.SetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE,
                          (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
    if was_visible:
        user32.ShowWindow(wt.HWND(hwnd), SW_SHOWNOACTIVATE)
