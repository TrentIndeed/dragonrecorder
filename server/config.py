import os
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

DATA_DIR.mkdir(parents=True, exist_ok=True)
