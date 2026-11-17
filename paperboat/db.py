"""SQLite storage. Lists (authors, tags, ...) are JSON columns; search is FTS5."""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config
from .util import last_name, title_sim

# The first schema. Later changes are MIGRATIONS below; never edit this one.
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS papers (
    id            INTEGER PRIMARY KEY,
    title         TEXT,
    authors       TEXT NOT NULL DEFAULT '[]',  -- [{name, affiliations: [{name, country, type}]}]
    year          INTEGER,
    venue         TEXT,
    kind          TEXT,                        -- inproceedings | article | misc
    abstract      TEXT,
    doi           TEXT,
    arxiv_id      TEXT,
    url           TEXT,                        -- landing page / where it came from
    bibtex        TEXT,
    bibtex_key    TEXT,
    bibtex_source TEXT,                        -- dblp | doi | generated | manual
    pdf_file      TEXT,
    pdf_sha256    TEXT,
    pages         INTEGER,
    tags          TEXT NOT NULL DEFAULT '[]',
    notes         TEXT NOT NULL DEFAULT '',
    read_state    TEXT NOT NULL DEFAULT 'unread',
    starred       INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'incomplete',
    missing       TEXT NOT NULL DEFAULT '[]',
    sources       TEXT NOT NULL DEFAULT '[]',
    origin        TEXT,
    added_at      TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS papers_sha ON papers(pdf_sha256) WHERE pdf_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS papers_doi ON papers(lower(doi));
CREATE INDEX IF NOT EXISTS papers_arxiv ON papers(arxiv_id);
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    title, authors, venue, abstract, notes, tags, fulltext,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""

JSON_FIELDS = ("authors", "tags", "missing", "sources")
REQUIRED = ("title", "authors", "year", "venue")
EDITABLE = (
    "title", "authors", "year", "venue", "kind", "abstract", "doi", "arxiv_id", "url",
    "bibtex", "bibtex_key", "tags", "notes", "read_state", "starred",
)
READ_STATES = ("unread", "reading", "read")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compute_missing(p: dict) -> list[str]:
    miss = [f for f in REQUIRED if not p.get(f) and not (f == "venue" and p.get("arxiv_id"))]
    if not p.get("pdf_file"):
        miss.append("pdf")
    return miss


# ------------------------------------------------------------ migrations
# PRAGMA user_version is the number of migrations applied. Append new ones; never
# reorder or edit an applied one. Each runs in the same transaction as its version bump.

def _v1_schema(con) -> None:
    for stmt in SCHEMA_V1.split(";"):  # executescript() would commit mid-migration
        if stmt.strip():
            con.execute(stmt)


def _v2_recompute_status(con) -> None:
    """Abstract is no longer required and an arXiv id counts as a venue."""
    for row in con.execute("SELECT * FROM papers").fetchall():
        p = to_dict(row)
        missing = compute_missing(p)
        con.execute("UPDATE papers SET missing = ?, status = ? WHERE id = ?",
                    (json.dumps(missing), "incomplete" if missing else "complete", p["id"]))


def _v3_fixed_keys(con) -> None:
    """Citation keys stay fixed once derived from real metadata."""
    con.execute("ALTER TABLE papers ADD COLUMN key_locked INTEGER NOT NULL DEFAULT 0")
    con.execute("UPDATE papers SET key_locked = 1 WHERE bibtex_key IS NOT NULL"
                " AND title IS NOT NULL AND year IS NOT NULL AND authors != '[]'")
    con.execute("CREATE UNIQUE INDEX papers_key ON papers(bibtex_key) WHERE bibtex_key IS NOT NULL")


MIGRATIONS = [_v1_schema, _v2_recompute_status, _v3_fixed_keys]


def init() -> None:
    for d in (config.DATA_DIR, config.PDF_DIR, config.THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise RuntimeError(f"{config.DB_PATH} is schema version {version}, newer than this "
                               f"paperboat ({len(MIGRATIONS)}); upgrade paperboat")
        for n, migrate in enumerate(MIGRATIONS[version:], start=version + 1):
            con.execute("BEGIN IMMEDIATE")
            try:
                migrate(con)
                con.execute(f"PRAGMA user_version = {n}")
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
    finally:
        con.close()


@contextmanager
def connect():
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in JSON_FIELDS:
        d[f] = json.loads(d.get(f) or "[]")
    d["starred"] = bool(d["starred"])
    if "key_locked" in d:
        d["key_locked"] = bool(d["key_locked"])
    return d


def _encode(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k in JSON_FIELDS:
            v = json.dumps(v or [])
        elif k in ("starred", "key_locked"):
            v = int(bool(v))
        out[k] = v
    return out


def insert(con, fields: dict) -> int:
    fields = {**fields, "added_at": now(), "updated_at": now()}
    enc = _encode(fields)
    cols = ", ".join(enc)
    qs = ", ".join("?" for _ in enc)
    cur = con.execute(f"INSERT INTO papers ({cols}) VALUES ({qs})", list(enc.values()))
    return cur.lastrowid


def update(con, pid: int, fields: dict) -> None:
    if not fields:
        return
    enc = _encode({**fields, "updated_at": now()})
    sets = ", ".join(f"{k} = ?" for k in enc)
    con.execute(f"UPDATE papers SET {sets} WHERE id = ?", [*enc.values(), pid])


def get(con, pid: int) -> dict | None:
    return to_dict(con.execute("SELECT * FROM papers WHERE id = ?", (pid,)).fetchone())


def all_papers(con) -> list[dict]:
    return [to_dict(r) for r in con.execute("SELECT * FROM papers ORDER BY added_at DESC")]


def find_duplicate(con, sha: str | None = None, doi: str | None = None,
                   arxiv_id: str | None = None, exclude: int | None = None) -> dict | None:
    clauses, args = [], []
    if sha:
        clauses.append("pdf_sha256 = ?"); args.append(sha)
    if doi:
        clauses.append("lower(doi) = lower(?)"); args.append(doi)
    if arxiv_id:
        clauses.append("arxiv_id = ?"); args.append(arxiv_id)
    if not clauses:
        return None
    sql = f"SELECT * FROM papers WHERE ({' OR '.join(clauses)})"
    if exclude is not None:
        sql += " AND id != ?"; args.append(exclude)
    return to_dict(con.execute(sql + " LIMIT 1", args).fetchone())


TITLE_SAME = 0.95  # title similarity at which two records are the same paper


def find_same_title(con, title: str | None, authors: list[dict], doi: str | None = None,
                    arxiv_id: str | None = None) -> dict | None:
    """A stored paper that is definitely this one although no identifier matched: nearly the
    same title and the same first author. Records whose DOIs or arXiv ids differ are
    different works (a conference and a journal version), never the same paper."""
    if not title or len(title) < 12:
        return None
    first = last_name((authors or [{}])[0].get("name", "")).lower()
    for row in con.execute("SELECT * FROM papers WHERE title IS NOT NULL"):
        p = to_dict(row)
        if doi and p["doi"] and doi.lower() != p["doi"].lower():
            continue
        if arxiv_id and p["arxiv_id"] and arxiv_id != p["arxiv_id"]:
            continue
        sim = title_sim(title, p["title"])
        theirs = last_name((p["authors"] or [{}])[0].get("name", "")).lower()
        if first and theirs:
            if first == theirs and sim >= TITLE_SAME:
                return p
        elif sim >= 0.98:  # no authors to compare: demand a near-exact title
            return p
    return None


def key_taken(con, key: str, exclude: int | None) -> bool:
    row = con.execute("SELECT 1 FROM papers WHERE bibtex_key = ? AND id != ?", (key, exclude or -1)).fetchone()
    return row is not None


# ---------------------------------------------------------------- search

def reindex(con, pid: int, fulltext: str | None = None) -> None:
    p = get(con, pid)
    if fulltext is None:
        row = con.execute("SELECT fulltext FROM papers_fts WHERE rowid = ?", (pid,)).fetchone()
        fulltext = row[0] if row else ""
    con.execute("DELETE FROM papers_fts WHERE rowid = ?", (pid,))
    if p is None:
        return
    authors = " ; ".join(
        " ".join([a.get("name", "")] + [f.get("name", "") for f in a.get("affiliations", [])])
        for a in p["authors"]
    )
    con.execute(
        "INSERT INTO papers_fts (rowid, title, authors, venue, abstract, notes, tags, fulltext)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, p["title"] or "", authors, p["venue"] or "", p["abstract"] or "",
         p["notes"] or "", " ".join(p["tags"]), fulltext or ""),
    )


def delete(con, pid: int) -> None:
    con.execute("DELETE FROM papers WHERE id = ?", (pid,))
    con.execute("DELETE FROM papers_fts WHERE rowid = ?", (pid,))


def _fts_query(q: str) -> str:
    tokens = re.findall(r'"[^"]+"|\S+', q)
    parts = []
    for t in tokens:
        if t.startswith('"') and t.endswith('"') and len(t) > 2:
            parts.append('"' + t[1:-1].replace('"', "") + '"')
        else:
            t = re.sub(r"[^\w\-.]", "", t)
            if t:
                parts.append('"' + t + '"*')
    return " ".join(parts)


def search(con, q: str, limit: int = 500) -> list[dict]:
    fq = _fts_query(q)
    if not fq:
        return []
    rows = con.execute(
        "SELECT rowid AS id, snippet(papers_fts, -1, char(2), char(3), '…', 14) AS snippet,"
        " bm25(papers_fts, 10.0, 6.0, 3.0, 2.0, 3.0, 4.0, 1.0) AS rank"
        " FROM papers_fts WHERE papers_fts MATCH ? ORDER BY rank LIMIT ?",
        (fq, limit),
    ).fetchall()
    return [dict(r) for r in rows]
