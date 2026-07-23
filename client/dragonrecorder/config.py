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
# global hotkeys ('keyboard' library syntax). Defaults avoid browser/editor
# collisions (ctrl+shift+r is Chrome hard-refresh).
HOTKEY_RECORD = os.environ.get("HOTKEY_RECORD", "ctrl+alt+c")
HOTKEY_DRAW = os.environ.get("HOTKEY_DRAW", "ctrl+alt+d")

APPDATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "DragonRecorder"
APPDATA_DIR.mkdir(parents=True, exist_ok=True)
RECORDINGS_DIR = APPDATA_DIR / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = APPDATA_DIR / "settings.json"

UI_DIR = CLIENT_DIR / "dragonrecorder" / "ui_html"

DEFAULT_SETTINGS = {
    "monitor": 1,          # mss 1-based index
    "camera": "",          # dshow device name, "" = none
    "mic": "",             # dshow device name, "" = none
    "blur": False,
    "fps": 30,
    "bubble_x": None,      # remembered bubble position
    "bubble_y": None,
}


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
