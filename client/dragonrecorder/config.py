"""Client configuration: .env for secrets/endpoints, JSON for persisted UI
settings (last-used devices)."""

import json
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

CLIENT_DIR = Path(__file__).resolve().parent.parent
# .env may live in client/ or the repo root
load_dotenv(CLIENT_DIR / ".env")
load_dotenv(CLIENT_DIR.parent / ".env")

SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:8082").rstrip("/")
CAPTURE_TOKEN = os.environ.get("CAPTURE_TOKEN", "")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
# Exclude toolbar/countdown/panel from screen capture (so they never appear
# in recordings). MUST be 0 when this machine is operated through a
# capture-based remote stream (remote_pc, Parsec, etc.) — excluded windows
# are invisible to the stream too, so the operator can't see them at all.
CAPTURE_EXCLUDE = os.environ.get("CAPTURE_EXCLUDE", "1") != "0"
# global hotkeys ('keyboard' library syntax). Defaults avoid browser/editor
# collisions (ctrl+shift+r is Chrome hard-refresh).
HOTKEY_RECORD = os.environ.get("HOTKEY_RECORD", "ctrl+alt+c")
HOTKEY_DRAW = os.environ.get("HOTKEY_DRAW", "ctrl+alt+d")

# App data lives inside the repo, NOT under %APPDATA%: Microsoft Store
# Python silently virtualizes AppData writes into its package container
# (Packages/PythonSoftwareFoundation.../LocalCache), where no other process
# can find them — settings "didn't persist" and logs "didn't exist".
APPDATA_DIR = Path(os.environ.get("DR_DATA_DIR", CLIENT_DIR.parent / ".appdata"))
APPDATA_DIR.mkdir(parents=True, exist_ok=True)
RECORDINGS_DIR = APPDATA_DIR / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = APPDATA_DIR / "settings.json"

UI_DIR = CLIENT_DIR / "dragonrecorder" / "ui_html"

DEFAULT_SETTINGS = {
    "monitor": 1,          # mss 1-based index
    "camera": "",          # dshow device name, "" = none
    "mic": "",             # dshow device name, "" = none
    "mic_auto": True,      # True: follow the Windows default device
    "mic_denoise": True,   # hum/hiss filter on the captured mic
    "blur": False,
    "blur_strength": 6,    # background blur radius in px (2-16)
    "bubble_shape": "rect",   # "rect" (rounded rectangle) or "circle"
    "bubble_scale": 80,    # camera bubble size, percent (50-160)
    "fps": 30,
    # how far ahead of the video the mic runs, in ms — see recorder._cmd.
    # Re-measure per machine with tools/measure_av_sync.py.
    "av_offset_ms": 260,
    "start_sound": True,   # audible cue when capture actually begins
    "bubble_x": None,      # remembered bubble position
    "bubble_y": None,
}
# every settings key the panel is allowed to write (keeps a malformed or
# stale payload from clobbering unrelated state like bubble_x)
PANEL_KEYS = ("monitor", "camera", "mic", "blur", "bubble_shape",
              "blur_strength", "bubble_scale", "mic_denoise", "start_sound",
              "fps")


def load_settings() -> dict:
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text("utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), "utf-8")


def find_ffmpeg() -> str:
    explicit = os.environ.get("FFMPEG_PATH", "")
    if explicit and Path(explicit).exists():
        return explicit
    # a build whose NVENC API matches the installed driver, if one was set up
    pinned = sorted((Path.home() / "AppData/Local/dragonrecorder-ffmpeg")
                    .glob("**/bin/ffmpeg.exe"))
    if pinned:
        return str(pinned[-1])
    found = shutil.which("ffmpeg")
    if found:
        return found
    # winget installs land here without a PATH refresh in the current process
    winget = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if winget.exists():
        hits = sorted(winget.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"))
        if hits:
            return str(hits[-1])
    raise FileNotFoundError(
        "ffmpeg not found. Install it (winget install Gyan.FFmpeg) or set "
        "FFMPEG_PATH in .env")


def find_ffprobe() -> str:
    return str(Path(find_ffmpeg()).with_name("ffprobe.exe"))
