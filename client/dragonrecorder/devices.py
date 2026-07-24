"""Device enumeration. Monitors via mss (same convention as remote_pc:
index 0 is the combined virtual screen, so physical monitors are 1-based).
Cameras and mics via ffmpeg's dshow device listing."""

import re
import subprocess

import mss

from . import config

CREATE_NO_WINDOW = 0x08000000


def list_monitors() -> list[dict]:
    with mss.mss() as sct:
        return [
            {"index": i + 1, "width": m["width"], "height": m["height"],
             "left": m["left"], "top": m["top"],
             "primary": m["left"] == 0 and m["top"] == 0}
            for i, m in enumerate(sct.monitors[1:])
        ]


def monitor_geometry(index: int) -> dict:
    with mss.mss() as sct:
        mons = sct.monitors[1:]
        if not 1 <= index <= len(mons):
            index = 1
        return dict(mons[index - 1])


def list_dshow_devices() -> dict:
    """Parse `ffmpeg -list_devices true -f dshow -i dummy` stderr."""
    proc = subprocess.run(
        [config.find_ffmpeg(), "-hide_banner", "-list_devices", "true",
         "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW, timeout=15,
    )
    cameras: list[str] = []
    mics: list[str] = []
    for line in proc.stderr.splitlines():
        m = re.search(r'"([^"]+)"\s+\((video|audio)\)', line)
        if m:
            (cameras if m.group(2) == "video" else mics).append(m.group(1))
    return {"cameras": cameras, "mics": mics}


def default_mic(mics: list[str]) -> str | None:
    """The Windows default capture device, matched against the dshow list.
    Follows whatever the user sets in Windows Sound settings."""
    try:
        import comtypes
        from pycaw.constants import CLSID_MMDeviceEnumerator
        from pycaw.pycaw import (AudioUtilities, EDataFlow, ERole,
                                 IMMDeviceEnumerator)
        enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator,
                                         IMMDeviceEnumerator,
                                         comtypes.CLSCTX_INPROC_SERVER)
        dev = enum.GetDefaultAudioEndpoint(EDataFlow.eCapture.value,
                                           ERole.eConsole.value)
        name = AudioUtilities.CreateDevice(dev).FriendlyName or ""
    except Exception:
        return None
    if name in mics:
        return name
    for m in mics:                      # fuzzy: endpoint vs dshow naming
        if name and (name in m or m in name):
            return m
    return None


def pick_default_mic(mics: list[str]) -> str:
    """OS default > 'Microphone'-named device > first."""
    if not mics:
        return ""
    best = default_mic(mics)
    if best:
        return best
    for m in mics:
        if "microphone" in m.lower():
            return m
    return mics[0]
