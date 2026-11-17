"""Turn a URL or an uploaded PDF into a stored paper."""

import hashlib
import logging
import re
from pathlib import Path

import httpx

from . import bibtex, config, db
from .extract import analyze_pdf, is_pdf, ids_from_url, parse_landing, pdf_url_for
from .resolve import resolve

log = logging.getLogger("paperboat.ingest")

META_FIELDS = ("title", "authors", "year", "venue", "kind", "abstract", "doi", "arxiv_id")


class IngestError(Exception):
    pass


async def _download(client: httpx.AsyncClient, url: str) -> tuple[bytes, str, str]:
    async with client.stream("GET", url) as r:
        if r.status_code != 200:
            raise IngestError(f"{url} returned HTTP {r.status_code}")
        chunks, size = [], 0
        async for chunk in r.aiter_bytes():
            size += len(chunk)
            if size > config.MAX_PDF_BYTES:
                raise IngestError("download exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks), r.headers.get("content-type", ""), str(r.url)


async def fetch_url(url: str) -> tuple[bytes | None, dict | None, str | None]:
    """Fetch a URL and return (pdf_bytes, landing_record, error)."""
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"}
    async with httpx.AsyncClient(headers=headers, timeout=60, follow_redirects=True) as c:
        landing = None
        try:
            target = pdf_url_for(url) or url
            body, ctype, final = await _download(c, target)
            if is_pdf(body):
                return body, None, None
            if "html" not in ctype:
                return None, None, f"URL is neither a PDF nor a web page ({ctype or 'unknown type'})"
            landing = parse_landing(body.decode("utf-8", "replace"), final)
            if pdf := landing.pop("pdf_url", None):
                body, _, _ = await _download(c, pdf)
                if is_pdf(body):
                    return body, landing, None
            return None, landing or None, "no PDF found at that URL"
        except (httpx.HTTPError, IngestError) as e:
            return None, landing, f"could not fetch {url}: {e}"


def _save_pdf(data: bytes, sha: str, info: dict) -> dict:
    name = f"{sha[:24]}.pdf"
    (config.PDF_DIR / name).write_bytes(data)
    if thumb := info.get("thumbnail"):
        (config.THUMB_DIR / f"{sha[:24]}.png").write_bytes(thumb)
    return {"pdf_file": name, "pdf_sha256": sha, "pages": info.get("pages")}


def _pretty_bib(bib: str) -> str:
    if "\n" in bib.strip():
        return bib.strip()
    bib = re.sub(r",\s*(\w+)\s*=\s*", r",\n  \1 = ", bib.strip())
    return re.sub(r"\}\s*\}$", "}\n}", bib)


def finalize(con, pid: int, *, fulltext: str | None = None, regenerate: bool = False) -> dict:
    """Recompute BibTeX, missing fields and status; refresh the search index.

    The citation key is derived from first author, year and title until all three are
    known, then it is locked, so documents that cite it keep working after later edits.
    Only an explicit PATCH of bibtex_key changes a locked key."""
    p = db.get(con, pid)
    if p["key_locked"] and p["bibtex_key"]:
        key = p["bibtex_key"]
    else:
        key = base = bibtex.make_key(p)
        n = 0
        while db.key_taken(con, key, pid):
            key = base + "abcdefghijklmnopqrstuvwxyz"[n % 26] * (n // 26 + 1)
            n += 1
        if p["title"] and p["year"] and p["authors"]:
            db.update(con, pid, {"key_locked": True})
    if regenerate or not p["bibtex"] or p["bibtex_source"] in (None, "generated"):
        bib, src = bibtex.generate(p, key), "generated"
    else:
        bib, src = bibtex.rekey(_pretty_bib(p["bibtex"]), key), p["bibtex_source"]
    missing = db.compute_missing(p)
    db.update(con, pid, {
        "bibtex": bib, "bibtex_key": key, "bibtex_source": src,
        "missing": missing, "status": "incomplete" if missing else "complete",
    })
    db.reindex(con, pid, fulltext)
    return db.get(con, pid)


def _attach(con, paper: dict, pdf: bytes | None, sha: str | None, info: dict | None) -> dict:
    if pdf and info and not paper.get("pdf_file"):
        db.update(con, paper["id"], _save_pdf(pdf, sha, info))
        return finalize(con, paper["id"], fulltext=info.get("fulltext"))
    return paper


async def ingest(*, pdf: bytes | None = None, url: str | None = None, filename: str | None = None,
                 origin: str | None = None, hints: dict | None = None) -> tuple[dict, bool, str | None]:
    """Returns (paper, created, warning)."""
    hints = {k: v for k, v in (hints or {}).items() if v}
    landing, warning = None, None
    if url and pdf is None:
        pdf, landing, warning = await fetch_url(url)
        if pdf is None and landing is None and not ids_from_url(url) and not hints:
            raise IngestError(warning or "nothing to work with")

    info, sha = None, None
    if pdf is not None:
        info = analyze_pdf(pdf)
        if info is None:
            raise IngestError("the file is not a readable PDF")
        sha = hashlib.sha256(pdf).hexdigest()
        with db.connect() as con:
            if dup := db.find_duplicate(con, sha=sha):
                return dup, False, None

    ids = ids_from_url(url)
    info = info or {}
    landing = landing or {}
    arxiv_id = hints.get("arxiv_id") or ids.get("arxiv_id") or landing.get("arxiv_id") or info.get("arxiv_id")
    doi = hints.get("doi") or ids.get("doi") or landing.get("doi") or info.get("doi")

    with db.connect() as con:
        if dup := db.find_duplicate(con, doi=doi, arxiv_id=arxiv_id):
            return _attach(con, dup, pdf, sha, info), False, None

    titles = [hints.get("title"), *info.get("titles", [])]
    if filename and not any(titles):
        titles.append(Path(filename).stem.replace("_", " "))
    pdf_rec = info and {"source": "pdf", "title": next((t for t in titles[1:] if t), None),
               "abstract": info.get("abstract"), "doi": info.get("doi"), "arxiv_id": info.get("arxiv_id")}
    meta = await resolve(arxiv_id=arxiv_id, doi=doi, titles=titles, landing=landing or None,
                         pdf=pdf_rec, hints=hints)

    with db.connect() as con:
        if dup := db.find_duplicate(con, doi=meta.get("doi"), arxiv_id=meta.get("arxiv_id")):
            return _attach(con, dup, pdf, sha, info), False, None
        fields = {k: meta.get(k) for k in META_FIELDS}
        fields.update({
            "bibtex": meta.get("bibtex"), "bibtex_source": meta.get("bibtex_source"),
            "sources": meta.get("sources", []), "url": url, "origin": origin,
            "tags": hints.get("tags") or [],
        })
        pid = db.insert(con, fields)
        if pdf:
            db.update(con, pid, _save_pdf(pdf, sha, info))
        paper = finalize(con, pid, fulltext=info.get("fulltext", ""))
    return paper, True, warning


async def attach_pdf(pid: int, pdf: bytes) -> dict:
    info = analyze_pdf(pdf)
    if info is None:
        raise IngestError("the file is not a readable PDF")
    sha = hashlib.sha256(pdf).hexdigest()
    with db.connect() as con:
        if (dup := db.find_duplicate(con, sha=sha, exclude=pid)):
            raise IngestError(f"this PDF is already stored as paper {dup['id']}")
        db.update(con, pid, _save_pdf(pdf, sha, info))
        p = db.get(con, pid)
        updates = {}
        if not p["abstract"] and info.get("abstract"):
            updates["abstract"] = info["abstract"]
        db.update(con, pid, updates)
        return finalize(con, pid, fulltext=info.get("fulltext"))


async def relookup(pid: int, overwrite: bool = False) -> dict:
    """Run the online lookup again with whatever the paper now has (e.g. a corrected title)."""
    with db.connect() as con:
        p = db.get(con, pid)
    meta = await resolve(arxiv_id=p["arxiv_id"], doi=p["doi"], titles=[p["title"]])
    with db.connect() as con:
        p = db.get(con, pid)
        updates = {}
        for k in META_FIELDS:
            if meta.get(k) and (overwrite or not p.get(k)):
                updates[k] = meta[k]
        # Affiliations found later are worth adding even if the author list is kept.
        if "authors" not in updates and meta.get("authors") and len(meta["authors"]) == len(p["authors"]):
            merged = [
                {**a, "affiliations": a.get("affiliations") or b.get("affiliations") or []}
                for a, b in zip(p["authors"], meta["authors"])
            ]
            if merged != p["authors"]:
                updates["authors"] = merged
        if meta.get("bibtex") and p["bibtex_source"] in (None, "generated"):
            updates.update(bibtex=meta["bibtex"], bibtex_source=meta["bibtex_source"])
        updates["sources"] = sorted(set(p["sources"]) | set(meta.get("sources", [])))
        db.update(con, pid, updates)
        return finalize(con, pid)
