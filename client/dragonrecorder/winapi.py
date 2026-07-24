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
    white background."""
    if hwnd:
        user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), -4000, 0,
                            0, 0, SWP_NOSIZE | SWP_NOACTIVATE)


gdi32 = ctypes.windll.gdi32


def set_round_region(hwnd: int, w: int, h: int, radius: int) -> None:
    """Clip a window to a rounded rectangle at the OS level. Shaped opaque
    windows replace per-pixel transparency, which pywebview/WebView2 cannot
    do reliably (white box artifacts)."""
    if hwnd:
        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
        user32.SetWindowRgn(wt.HWND(hwnd), rgn, True)


def set_ellipse_region(hwnd: int, w: int, h: int) -> None:
    if hwnd:
        rgn = gdi32.CreateEllipticRgn(0, 0, w + 1, h + 1)
        user32.SetWindowRgn(wt.HWND(hwnd), rgn, True)


def hide_window(hwnd: int) -> None:
    """SW_HIDE directly — pywebview's hidden=True and .hide() are both
    unreliable for transparent windows."""
    if hwnd:
        user32.ShowWindow(wt.HWND(hwnd), 0)


def force_rect_topmost(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """Pin a window to an exact rect and keep it topmost. pywebview's
    frameless windows come out smaller than requested (it subtracts standard
    window decorations that frameless windows don't have) and can lose their
    topmost bit across hide/show — this fixes both in one call."""
    if not hwnd:
        return
    user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), x, y, w, h,
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)


def set_toolwindow(hwnd: int) -> None:
    """Keep overlay windows out of the taskbar and alt-tab."""
    if not hwnd:
        return
    style = user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE)
    user32.SetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE, style | WS_EX_TOOLWINDOW)
