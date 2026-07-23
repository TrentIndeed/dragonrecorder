import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "dragonrecorder.sqlite3"))
CAPTURE_TOKEN = os.environ.get("CAPTURE_TOKEN", "")
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "10"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "14"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PUBLIC_URL = os.environ.get("SERVER_URL", "").rstrip("/")

# --- dashboard auth (same scheme as remote_pc) ---
DASH_USER = os.environ.get("DASH_USER", "trenton")
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
DASH_PASSWORD_HASH = os.environ.get("DASH_PASSWORD_HASH", "")
# Stable secret keeps sessions across restarts; regenerated if unset.
DASH_SECRET = os.environ.get("DASH_SECRET") or secrets.token_urlsafe(32)
DASH_SESSION_TTL = int(os.environ.get("DASH_SESSION_TTL", 30 * 24 * 3600))
DASH_MAX_LOGIN_ATTEMPTS = int(os.environ.get("DASH_MAX_LOGIN_ATTEMPTS", 8))
DASH_LOCKOUT_SECONDS = int(os.environ.get("DASH_LOCKOUT_SECONDS", 900))
DASH_COOKIE = "dr_session"

DATA_DIR.mkdir(parents=True, exist_ok=True)
