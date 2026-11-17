"""HTTP API and static frontend."""

import logging
import re
import socket
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, config, db, venues
from .ingest import META_FIELDS, IngestError, Outcome, attach_pdf, finalize, ingest, relookup, remove_pdf_files
from .util import last_name, slug

log = logging.getLogger("paperboat")
STATIC = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_app):
    db.init()
    venues.reset()
    yield


app = FastAPI(title="paperboat", version=__version__, lifespan=lifespan)


# ------------------------------------------------------------- helpers

def public(p: dict, snippet: str | None = None) -> dict:
    out = {k: v for k, v in p.items() if k not in ("pdf_file", "pdf_sha256", "key_locked")}
    out["has_pdf"] = bool(p.get("pdf_file"))
    out["pdf_url"] = f"/api/papers/{p['id']}/pdf" if p.get("pdf_file") else None
    out["thumb_url"] = f"/api/papers/{p['id']}/thumb" if p.get("pdf_file") else None
    out["link"] = f"/#/paper/{p['id']}"
    if snippet is not None:
        out["snippet"] = snippet
    return out


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def submission_response(o: Outcome, request: Request) -> JSONResponse:
    """201 = stored as a new paper; 200 = already in the library (see "existing").
    Either way "status" may be "incomplete": then PATCH the fields listed in "fix"."""
    p = o.paper
    body = {
        "status": p["status"],
        "duplicate": not o.created,
        "id": p["id"],
        "missing": p["missing"],
        "paper": public(p),
    }
    if o.warning:
        body["warning"] = o.warning
    if not o.created:
        body["existing"] = {
            "id": p["id"],
            "title": p["title"],
            "matched_on": o.matched_on,
            "pdf_replaced": o.pdf_replaced,
            "api_url": f"{_base(request)}/api/papers/{p['id']}",
            "link": f"{_base(request)}/#/paper/{p['id']}",
        }
    if p["status"] == "incomplete":
        body["fix"] = {
            "method": "PATCH",
            "url": f"/api/papers/{p['id']}",
            "fields": [f for f in p["missing"] if f != "pdf"],
        }
        if "pdf" in p["missing"]:
            body["fix"]["attach_pdf"] = {"method": "POST", "url": f"/api/papers/{p['id']}/pdf"}
    return JSONResponse(body, status_code=201 if o.created else 200)


def _require(pid: int) -> dict:
    with db.connect() as con:
        p = db.get(con, pid)
    if p is None:
        raise HTTPException(404, "no such paper")
    return p


def _normalize_authors(v) -> list[dict]:
    if isinstance(v, str):
        v = [x for x in (s.strip() for s in v.replace(" and ", "\n").splitlines()) if x]
    out = []
    for a in v or []:
        if isinstance(a, str):
            out.append({"name": a.strip(), "affiliations": []})
        elif isinstance(a, dict) and a.get("name"):
            affs = a.get("affiliations") or []
            affs = [{"name": x} if isinstance(x, str) else x for x in affs]
            out.append({"name": a["name"].strip(), "affiliations": affs})
    return out


def _normalize_tags(v) -> list[str]:
    if isinstance(v, str):
        v = v.split(",")
    seen = []
    for t in v or []:
        t = str(t).strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def _clean_fields(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if k not in db.EDITABLE:
            continue
        if k == "authors":
            v = _normalize_authors(v)
        elif k == "tags":
            v = _normalize_tags(v)
        elif k == "year":
            v = int(v) if str(v or "").strip().isdigit() else None
        elif k == "read_state":
            if v not in db.READ_STATES:
                raise HTTPException(400, f"read_state must be one of {db.READ_STATES}")
        elif k == "starred":
            v = bool(v)
        elif k == "bibtex_key":
            v = str(v or "").strip() or None  # empty: derive it again from the metadata
            if v and not re.fullmatch(r"[A-Za-z0-9_:.\-]+", v):
                raise HTTPException(400, "bibtex_key may use only letters, digits, and _ : . -")
        elif k == "venue" and isinstance(v, str):
            v = venues.canonical(v.strip()) or None
        elif isinstance(v, str):
            v = v.strip() if k != "notes" else v
            if k in ("doi", "arxiv_id", "venue", "title", "abstract", "url", "kind") and not v:
                v = None
        out[k] = v
    return out


def _filename(p: dict) -> str:
    first = (p["authors"] or [{}])[0].get("name", "")
    return slug(f"{last_name(first)}{p['year'] or ''} - {p['title'] or 'paper'}", 120) + ".pdf"


# ------------------------------------------------------------ submit

@app.post("/api/papers")
async def submit(request: Request):
    """Submit a paper. Either multipart form (file=<pdf>, and/or url=..., optional
    origin/title/doi/arxiv_id/tags) or JSON {"url": ..., "origin": ..., ...}."""
    ctype = request.headers.get("content-type", "")
    pdf, filename = None, None
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        data = {k: v for k, v in form.items() if isinstance(v, str)}
        upload = form.get("file")
        if upload is not None and not isinstance(upload, str):
            pdf = await upload.read()
            filename = upload.filename
    elif ctype.startswith("application/json"):
        data = await request.json()
    elif ctype.startswith("application/pdf"):
        pdf, data = await request.body(), dict(request.query_params)
        filename = request.headers.get("x-filename")
    else:
        raise HTTPException(415, "send multipart/form-data, application/json, or application/pdf")

    url = (data.get("url") or "").strip() or None
    if pdf is None and not url:
        raise HTTPException(400, "provide a PDF file or a url")
    origin = data.get("origin") or request.headers.get("x-paperboat-origin") or (request.client.host if request.client else None)
    hints = {
        "title": data.get("title"), "doi": data.get("doi"), "arxiv_id": data.get("arxiv_id"),
        "venue": data.get("venue"), "year": _clean_fields({"year": data.get("year")}).get("year"),
        "tags": _normalize_tags(data.get("tags")),
    }
    try:
        outcome = await ingest(pdf=pdf, url=url, filename=filename, origin=origin, hints=hints)
    except IngestError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    return submission_response(outcome, request)


# --------------------------------------------------------- read / edit

@app.get("/api/papers")
def list_papers(q: str | None = None):
    with db.connect() as con:
        if q and q.strip():
            hits = db.search(con, q)
            out = []
            for h in hits:
                if p := db.get(con, h["id"]):
                    out.append(public(p, h["snippet"]))
            return {"papers": out, "query": q}
        return {"papers": [public(p) for p in db.all_papers(con)]}


@app.get("/api/papers/{pid}")
def get_paper(pid: int):
    return public(_require(pid))


@app.patch("/api/papers/{pid}")
async def patch_paper(pid: int, request: Request):
    """Update metadata and/or annotations. Response carries the new status and missing list."""
    old = _require(pid)
    fields = _clean_fields(await request.json())
    if "authors" in fields:  # names typed in a form carry no affiliations; keep the known ones
        known = {a["name"]: a.get("affiliations") or [] for a in old["authors"]}
        for a in fields["authors"]:
            a["affiliations"] = a["affiliations"] or known.get(a["name"], [])
    if "bibtex_key" in fields:
        if fields["bibtex_key"] == old["bibtex_key"]:
            del fields["bibtex_key"]
        elif fields["bibtex_key"] is None:
            fields["key_locked"] = False
        else:
            with db.connect() as con:
                if db.key_taken(con, fields["bibtex_key"], pid):
                    raise HTTPException(409, f"citation key {fields['bibtex_key']} is already used by another paper")
            fields["key_locked"] = True
    regenerate = False
    if "bibtex" in fields:
        if fields["bibtex"]:
            fields["bibtex_source"] = "manual"
        else:
            fields["bibtex_source"], regenerate = "generated", True
    elif any(k in fields and fields[k] != old.get(k) for k in META_FIELDS) and old["bibtex_source"] != "manual":
        # Edited metadata: a fetched BibTeX no longer agrees with it, so rebuild.
        fields["bibtex_source"], regenerate = "generated", True
    with db.connect() as con:
        db.update(con, pid, fields)
        p = finalize(con, pid, regenerate=regenerate)
    return {"status": p["status"], "missing": p["missing"], "paper": public(p)}


@app.delete("/api/papers/{pid}")
def delete_paper(pid: int):
    p = _require(pid)
    with db.connect() as con:
        db.delete(con, pid)
    remove_pdf_files(p.get("pdf_file"))  # PDFs are unique per paper (sha index)
    return {"deleted": pid}


@app.post("/api/papers/{pid}/resolve")
async def resolve_again(pid: int, overwrite: bool = False):
    _require(pid)
    p = await relookup(pid, overwrite=overwrite)
    return {"status": p["status"], "missing": p["missing"], "paper": public(p)}


@app.post("/api/papers/{pid}/pdf")
async def upload_pdf(pid: int, request: Request):
    _require(pid)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(400, "missing file")
        data = await upload.read()
    else:
        data = await request.body()
    try:
        p = await attach_pdf(pid, data)
    except IngestError as e:
        raise HTTPException(400, str(e))
    return {"status": p["status"], "missing": p["missing"], "paper": public(p)}


# -------------------------------------------------------------- files

@app.get("/api/papers/{pid}/pdf")
def get_pdf(pid: int, download: bool = False):
    p = _require(pid)
    if not p.get("pdf_file"):
        raise HTTPException(404, "no PDF stored for this paper")
    return FileResponse(
        config.PDF_DIR / p["pdf_file"], media_type="application/pdf", filename=_filename(p),
        content_disposition_type="attachment" if download else "inline",
    )


@app.get("/api/papers/{pid}/thumb")
def get_thumb(pid: int):
    p = _require(pid)
    path = config.THUMB_DIR / (p.get("pdf_file") or "").replace(".pdf", ".png")
    if not p.get("pdf_file") or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


def _bib_response(text: str, name: str, download: bool) -> Response:
    headers = {"Content-Disposition": f'attachment; filename="{name}"'} if download else {}
    return PlainTextResponse(text, media_type="application/x-bibtex; charset=utf-8", headers=headers)


@app.get("/api/papers/{pid}/bibtex")
def get_bibtex(pid: int, download: bool = False):
    p = _require(pid)
    return _bib_response(p["bibtex"] + "\n", f"{p['bibtex_key']}.bib", download)


@app.get("/api/export.bib")
def export_bib(ids: str | None = None, download: bool = False):
    """All citations, or only ?ids=1,2,3."""
    with db.connect() as con:
        papers = db.all_papers(con)
    if ids:
        wanted = {int(x) for x in ids.split(",") if x.strip().isdigit()}
        papers = [p for p in papers if p["id"] in wanted]
    papers.sort(key=lambda p: p["bibtex_key"] or "")
    return _bib_response("\n\n".join(p["bibtex"] for p in papers if p["bibtex"]) + "\n", "paperboat.bib", download)


@app.get("/api/info")
def info():
    return {"name": "paperboat", "host": socket.gethostname(), "port": config.PORT, "data_dir": str(config.DATA_DIR)}


# ----------------------------------------------------------- frontend

@app.get("/api.md")
def api_doc(request: Request):
    """The API reference as raw Markdown, with this server's address filled in, for
    pasting into a client app or a prompt."""
    text = (Path(__file__).parent / "api.md").read_text().replace("http://HOST:8077", _base(request))
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
