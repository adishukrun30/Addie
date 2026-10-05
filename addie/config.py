"""Runtime configuration. Personal data lives outside the repository by default."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("ADDIE_DATA_DIR", Path.home() / ".addie")).expanduser()
DB_PATH = DATA_DIR / "addie.db"
SALT_PATH = DATA_DIR / "addie.salt"

HOST = "127.0.0.1"  # never listen on a network interface
PORT = int(os.environ.get("ADDIE_PORT", "8765"))

AUTO_LOCK_MINUTES = int(os.environ.get("ADDIE_AUTO_LOCK_MINUTES", "15"))
MIN_PASSWORD_LENGTH = 12
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
