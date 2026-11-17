import re
import unicodedata
from difflib import SequenceMatcher


def clean_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def norm_title(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", ascii_fold(s or "").lower()).strip()


def title_sim(a: str | None, b: str | None) -> float:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def strip_tags(s: str | None) -> str:
    return clean_ws(re.sub(r"<[^>]+>", " ", s or ""))


def person_name(name: str) -> str:
    """Normalize 'Last, First' to 'First Last' and drop DBLP disambiguation digits."""
    name = clean_ws(re.sub(r"\s+\d{4}$", "", name or ""))
    if name.count(",") == 1:
        last, first = (p.strip() for p in name.split(","))
        if first and last:
            name = f"{first} {last}"
    return name


def last_name(name: str) -> str:
    parts = person_name(name).split()
    return parts[-1] if parts else ""


def year_of(s) -> int | None:
    if isinstance(s, int):
        return s
    m = re.search(r"(19|20)\d{2}", str(s or ""))
    return int(m.group(0)) if m else None


def slug(s: str, maxlen: int = 80) -> str:
    s = re.sub(r"[^A-Za-z0-9 _.-]+", "", ascii_fold(s or "")).strip()
    return re.sub(r"\s+", " ", s)[:maxlen].strip() or "paper"
