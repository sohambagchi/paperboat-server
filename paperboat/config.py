import os
from pathlib import Path

from . import __version__


def _default_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "paperboat"


DATA_DIR = Path(os.environ.get("PAPERBOAT_DATA") or _default_data_dir()).expanduser()
PDF_DIR = DATA_DIR / "pdfs"
THUMB_DIR = DATA_DIR / "thumbs"
DB_PATH = DATA_DIR / "paperboat.db"

HOST = os.environ.get("PAPERBOAT_HOST", "0.0.0.0")
PORT = int(os.environ.get("PAPERBOAT_PORT", "8077"))

# Optional keys; every lookup works without them, just with lower rate limits.
S2_API_KEY = os.environ.get("S2_API_KEY")
OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY")

HTTP_TIMEOUT = float(os.environ.get("PAPERBOAT_HTTP_TIMEOUT", "15"))
MAX_PDF_BYTES = 200 * 1024 * 1024
USER_AGENT = f"paperboat/{__version__} (personal paper library)"


def set_data_dir(path: str | Path) -> None:
    global DATA_DIR, PDF_DIR, THUMB_DIR, DB_PATH
    DATA_DIR = Path(path).expanduser()
    PDF_DIR = DATA_DIR / "pdfs"
    THUMB_DIR = DATA_DIR / "thumbs"
    DB_PATH = DATA_DIR / "paperboat.db"
