import re

from .util import ascii_fold, last_name

STOP = {"a", "an", "the", "on", "of", "in", "for", "to", "and", "with", "towards", "toward", "via", "is", "are"}


def make_key(p: dict) -> str:
    first = (p.get("authors") or [{}])[0].get("name", "")
    last = re.sub(r"[^a-z]", "", ascii_fold(last_name(first)).lower()) or "anon"
    words = [w for w in re.findall(r"[a-z0-9]+", ascii_fold(p.get("title") or "").lower()) if w not in STOP]
    return f"{last}{p.get('year') or 'nd'}{words[0] if words else ''}"


def rekey(bib: str, key: str) -> str:
    return re.sub(r"^(\s*@\w+\s*\{)\s*[^,]*,", lambda m: f"{m.group(1)}{key},", bib, count=1)


def _esc(s: str) -> str:
    return re.sub(r"([&%#_$])", r"\\\1", str(s))


def generate(p: dict, key: str) -> str:
    venue = p.get("venue")
    arxiv_only = p.get("arxiv_id") and (not venue or venue.lower() in ("arxiv", "corr"))
    kind = "misc" if arxiv_only else (p.get("kind") or ("inproceedings" if venue else "misc"))
    fields = [("title", "{" + _esc(p.get("title") or "") + "}")]
    if p.get("authors"):
        fields.append(("author", " and ".join(_esc(a["name"]) for a in p["authors"])))
    if venue and not arxiv_only:
        fields.append(({"article": "journal", "inproceedings": "booktitle"}.get(kind, "howpublished"), _esc(venue)))
    if p.get("year"):
        fields.append(("year", str(p["year"])))
    if p.get("doi"):
        fields.append(("doi", p["doi"]))
    if p.get("arxiv_id"):
        fields += [("eprint", p["arxiv_id"]), ("archivePrefix", "arXiv")]
    url = p.get("url") or (f"https://doi.org/{p['doi']}" if p.get("doi") else None) \
        or (f"https://arxiv.org/abs/{p['arxiv_id']}" if p.get("arxiv_id") else None)
    if url:
        fields.append(("url", url))
    body = ",\n".join(f"  {k:<13} = {{{v}}}" for k, v in fields)
    return f"@{kind}{{{key},\n{body}\n}}"
