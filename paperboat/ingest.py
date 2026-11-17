"""Turn a URL or an uploaded PDF into a stored paper."""

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import bibtex, config, db
from .extract import analyze_pdf, is_pdf, ids_from_url, parse_landing, pdf_url_for
from .resolve import resolve

log = logging.getLogger("paperboat.ingest")

META_FIELDS = ("title", "authors", "year", "venue", "kind", "abstract", "doi", "arxiv_id")


class IngestError(Exception):
    pass


@dataclass
class Outcome:
    paper: dict
    created: bool
    warning: str | None = None
    matched_on: str | None = None   # for a duplicate: pdf | doi | arxiv_id | title
    pdf_replaced: bool = False      # a duplicate whose stored PDF was replaced by this one


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


def remove_pdf_files(pdf_file: str | None) -> None:
    if pdf_file:
        (config.PDF_DIR / pdf_file).unlink(missing_ok=True)
        (config.THUMB_DIR / pdf_file.replace(".pdf", ".png")).unlink(missing_ok=True)


def _store_pdf(con, p: dict, pdf: bytes, sha: str, info: dict) -> dict:
    """Store pdf as the paper's PDF. The newest PDF wins: an older one is deleted."""
    updates = _save_pdf(pdf, sha, info)
    if not p.get("abstract") and info.get("abstract"):
        updates["abstract"] = info["abstract"]
    db.update(con, p["id"], updates)
    paper = finalize(con, p["id"], fulltext=info.get("fulltext"))
    if p.get("pdf_file") and p["pdf_file"] != updates["pdf_file"]:
        remove_pdf_files(p["pdf_file"])
    return paper


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


def _duplicate(con, paper: dict, matched_on: str, pdf: bytes | None, sha: str | None,
               info: dict | None) -> Outcome:
    """The submission is a paper we already have. Its PDF, if new, replaces the stored one."""
    if pdf and info and paper.get("pdf_sha256") != sha:
        return Outcome(_store_pdf(con, paper, pdf, sha, info), False, None, matched_on, pdf_replaced=True)
    return Outcome(paper, False, None, matched_on)


def _matched_on(dup: dict, sha=None, doi=None, arxiv_id=None) -> str:
    if sha and dup.get("pdf_sha256") == sha:
        return "pdf"
    if doi and (dup.get("doi") or "").lower() == doi.lower():
        return "doi"
    return "arxiv_id"


async def ingest(*, pdf: bytes | None = None, url: str | None = None, filename: str | None = None,
                 origin: str | None = None, hints: dict | None = None) -> Outcome:
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
                return Outcome(dup, False, None, "pdf")

    ids = ids_from_url(url)
    info = info or {}
    landing = landing or {}
    arxiv_id = hints.get("arxiv_id") or ids.get("arxiv_id") or landing.get("arxiv_id") or info.get("arxiv_id")
    doi = hints.get("doi") or ids.get("doi") or landing.get("doi") or info.get("doi")

    with db.connect() as con:
        if dup := db.find_duplicate(con, doi=doi, arxiv_id=arxiv_id):
            return _duplicate(con, dup, _matched_on(dup, doi=doi, arxiv_id=arxiv_id), pdf, sha, info)

    titles = [hints.get("title"), *info.get("titles", [])]
    if filename and not any(titles):
        titles.append(Path(filename).stem.replace("_", " "))
    pdf_rec = info and {"source": "pdf", "title": next((t for t in titles[1:] if t), None),
               "abstract": info.get("abstract"), "doi": info.get("doi"), "arxiv_id": info.get("arxiv_id")}
    meta = await resolve(arxiv_id=arxiv_id, doi=doi, titles=titles, landing=landing or None,
                         pdf=pdf_rec, hints=hints)

    try:
        with db.connect() as con:
            if dup := db.find_duplicate(con, doi=meta.get("doi"), arxiv_id=meta.get("arxiv_id")):
                how = _matched_on(dup, doi=meta.get("doi"), arxiv_id=meta.get("arxiv_id"))
                return _duplicate(con, dup, how, pdf, sha, info)
            if dup := db.find_same_title(con, meta.get("title"), meta.get("authors") or [],
                                         doi=meta.get("doi"), arxiv_id=meta.get("arxiv_id")):
                return _duplicate(con, dup, "title", pdf, sha, info)
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
    except sqlite3.IntegrityError:
        # The same PDF or paper arrived twice at once and the other request stored it first.
        with db.connect() as con:
            dup = db.find_duplicate(con, sha=sha, doi=meta.get("doi"), arxiv_id=meta.get("arxiv_id"))
        if dup is None:
            raise
        return Outcome(dup, False, None, _matched_on(dup, sha, meta.get("doi"), meta.get("arxiv_id")))
    return Outcome(paper, True, warning)


async def attach_pdf(pid: int, pdf: bytes) -> dict:
    info = analyze_pdf(pdf)
    if info is None:
        raise IngestError("the file is not a readable PDF")
    sha = hashlib.sha256(pdf).hexdigest()
    with db.connect() as con:
        if (dup := db.find_duplicate(con, sha=sha, exclude=pid)):
            raise IngestError(f"this PDF is already stored as paper {dup['id']}")
        return _store_pdf(con, db.get(con, pid), pdf, sha, info)


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
