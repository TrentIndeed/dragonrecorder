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


def set_toolwindow(hwnd: int) -> None:
    """Keep overlay windows out of the taskbar and alt-tab."""
    if not hwnd:
        return
    style = user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE)
    user32.SetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE, style | WS_EX_TOOLWINDOW)
