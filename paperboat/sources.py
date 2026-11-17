"""Online metadata sources. Each returns a normalized record dict or None, never raises.

Record fields: source, title, authors [{name, affiliations: [{name, country?, type?}]}],
year, venue, kind, abstract, doi, arxiv_id, bibtex.
"""

import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import httpx

from . import config
from .extract import split_doi
from .util import ascii_fold, clean_ws, last_name, person_name, strip_tags, title_sim, year_of

log = logging.getLogger("paperboat.sources")
MATCH = 0.9  # minimum title similarity before trusting a search hit


async def _get(client: httpx.AsyncClient, url: str, **kw) -> httpx.Response | None:
    try:
        r = await client.get(url, **kw)
    except httpx.HTTPError as e:
        log.info("%s failed: %s", url, e)
        return None
    if r.status_code != 200:
        log.info("%s -> HTTP %s", url, r.status_code)
        return None
    return r


def _json(r: httpx.Response | None):
    if r is None or "json" not in r.headers.get("content-type", ""):
        return None
    try:
        return r.json()
    except ValueError:
        return None


# ------------------------------------------------------------------ arXiv

ATOM = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}


async def arxiv(client, aid: str) -> dict | None:
    r = await _get(client, "https://export.arxiv.org/api/query", params={"id_list": aid})
    if r is None:
        return None
    try:
        entry = ET.fromstring(r.text).find("a:entry", ATOM)
    except ET.ParseError:
        return None
    if entry is None or "arxiv.org/abs/" not in (entry.findtext("a:id", "", ATOM)):
        return None
    authors = []
    for a in entry.findall("a:author", ATOM):
        affs = [{"name": clean_ws(x.text)} for x in a.findall("x:affiliation", ATOM) if x.text]
        authors.append({"name": clean_ws(a.findtext("a:name", "", ATOM)), "affiliations": affs})
    doi, _ = split_doi(entry.findtext("x:doi", None, ATOM))
    cat = entry.find("x:primary_category", ATOM)
    return {
        "source": "arxiv",
        "arxiv_id": aid,
        "title": clean_ws(entry.findtext("a:title", "", ATOM)),
        "authors": authors,
        "year": year_of(entry.findtext("a:published", "", ATOM)),
        "abstract": clean_ws(entry.findtext("a:summary", "", ATOM)),
        "doi": doi,
        "journal_ref": clean_ws(entry.findtext("x:journal_ref", "", ATOM)) or None,
        "primary_class": cat.get("term") if cat is not None else None,
    }


# --------------------------------------------------------------- Crossref

async def crossref(client, doi: str) -> dict | None:
    data = _json(await _get(client, f"https://api.crossref.org/works/{quote(doi, safe='/')}"))
    if not data:
        return None
    m = data.get("message", {})
    authors = []
    for a in m.get("author", []):
        name = clean_ws(f"{a.get('given', '')} {a.get('family', '')}") or a.get("name", "")
        affs = [{"name": clean_ws(x["name"])} for x in a.get("affiliation", []) if x.get("name")]
        authors.append({"name": name, "affiliations": affs})
    year = None
    for k in ("published-print", "published-online", "issued", "published"):
        parts = (m.get(k) or {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            year = parts[0][0]
            break
    ctype = m.get("type", "")
    return {
        "source": "crossref",
        "doi": doi,
        "title": clean_ws((m.get("title") or [""])[0]),
        "authors": authors,
        "year": year,
        "venue": clean_ws((m.get("container-title") or [""])[0]) or None,
        "kind": "article" if ctype == "journal-article" else ("inproceedings" if "proceedings" in ctype else None),
        "abstract": strip_tags(m.get("abstract")).removeprefix("Abstract ").strip() or None,
    }


async def doi_bibtex(client, doi: str) -> dict | None:
    r = await _get(client, f"https://doi.org/{doi}", headers={"Accept": "application/x-bibtex"})
    if r is None or not r.text.lstrip().startswith("@"):
        return None
    return {"source": "doi", "doi": doi, "bibtex": r.text.strip()}


# --------------------------------------------------------------- OpenAlex

def _openalex_work(w: dict) -> dict:
    authors = []
    for a in w.get("authorships", []):
        affs = [
            {"name": i["display_name"], "country": i.get("country_code"), "type": i.get("type")}
            for i in a.get("institutions", []) if i.get("display_name")
        ]
        authors.append({"name": (a.get("author") or {}).get("display_name", ""), "affiliations": affs})
    abstract = None
    if inv := w.get("abstract_inverted_index"):
        pos = sorted((i, word) for word, idxs in inv.items() for i in idxs)
        abstract = " ".join(word for _, word in pos)
    # Prefer a published venue among all locations over the arXiv repository copy.
    venue, kind = None, None
    locs = [w.get("primary_location") or {}] + (w.get("locations") or [])
    for loc in locs:
        src = loc.get("source") or {}
        if src.get("display_name") and src.get("type") in ("journal", "conference", "book series"):
            venue = src["display_name"]
            kind = "article" if src["type"] == "journal" else "inproceedings"
            break
    doi, aid = split_doi(w.get("doi"))
    return {
        "source": "openalex",
        "title": clean_ws(w.get("display_name") or w.get("title")),
        "authors": authors,
        "year": w.get("publication_year"),
        "venue": venue,
        "kind": kind,
        "abstract": abstract,
        "doi": doi,
        "arxiv_id": aid,
    }


async def openalex(client, doi: str | None = None, title: str | None = None) -> dict | None:
    params = {"api_key": config.OPENALEX_API_KEY} if config.OPENALEX_API_KEY else {}
    if doi:
        w = _json(await _get(client, f"https://api.openalex.org/works/doi:{doi}", params=params))
        return _openalex_work(w) if w else None
    if title:
        data = _json(await _get(client, "https://api.openalex.org/works",
                                params={**params, "search": title, "per-page": 5}))
        matches = [_openalex_work(w) for w in (data or {}).get("results", [])
                   if title_sim(w.get("display_name"), title) >= MATCH]
        if not matches:
            return None
        # The arXiv copy and the published version are separate works; prefer the published
        # one when its first author agrees with the best hit.
        best = matches[0]
        lead = last_name((best["authors"] or [{"name": ""}])[0]["name"]).lower()
        for m in matches:
            if m["venue"] and last_name((m["authors"] or [{"name": ""}])[0]["name"]).lower() == lead:
                m["arxiv_id"] = m["arxiv_id"] or next((x["arxiv_id"] for x in matches if x["arxiv_id"]), None)
                return m
        return best
    return None


# -------------------------------------------------------- Semantic Scholar

S2_FIELDS = "title,authors,venue,year,abstract,externalIds,publicationVenue,publicationTypes"


def _s2_paper(p: dict) -> dict:
    venue = p.get("venue") or (p.get("publicationVenue") or {}).get("name")
    if venue and re.search(r"arxiv", venue, re.I):
        venue = None
    ext = p.get("externalIds") or {}
    doi, _ = split_doi(ext.get("DOI"))
    types = p.get("publicationTypes") or []
    return {
        "source": "s2",
        "title": clean_ws(p.get("title")),
        "authors": [{"name": a.get("name", ""), "affiliations": []} for a in p.get("authors", [])],
        "year": p.get("year"),
        "venue": venue,
        # S2 labels many conference papers "JournalArticle" too, so only its "Conference" is trusted.
        "kind": "inproceedings" if "Conference" in types else None,
        "abstract": p.get("abstract"),
        "doi": doi,
        "arxiv_id": ext.get("ArXiv"),
    }


async def semantic_scholar(client, ident: str | None = None, title: str | None = None) -> dict | None:
    headers = {"x-api-key": config.S2_API_KEY} if config.S2_API_KEY else {}
    base = "https://api.semanticscholar.org/graph/v1/paper"
    if ident:
        p = _json(await _get(client, f"{base}/{ident}", params={"fields": S2_FIELDS}, headers=headers))
        return _s2_paper(p) if p else None
    if title:
        data = _json(await _get(client, f"{base}/search/match",
                                params={"query": title, "fields": S2_FIELDS}, headers=headers))
        for p in (data or {}).get("data", []):
            if title_sim(p.get("title"), title) >= MATCH:
                return _s2_paper(p)
    return None


# -------------------------------------------------------------------- DBLP
# DBLP's search API and .bib downloads sit behind a bot check (Anubis). Its SPARQL endpoint
# (QLever, with a word index over titles) is open, so lookups go through that instead,
# and BibTeX for DBLP matches is generated from the record.

DBLP_SPARQL = "https://sparql.dblp.org/sparql"
_DBLP_PREFIX = ("PREFIX dblp: <https://dblp.org/rdf/schema#> "
                "PREFIX ql: <http://qlever.cs.uni-freiburg.de/builtin-functions/> ")
_STOP = {"the", "and", "for", "with", "from", "into", "via", "its", "are", "not", "how", "what", "why"}
_KINDS = {"Inproceedings": "inproceedings", "Article": "article"}


async def _sparql(client, query: str) -> list[dict] | None:
    data = _json(await _get(client, DBLP_SPARQL, params={"query": _DBLP_PREFIX + query},
                            headers={"Accept": "application/sparql-results+json"}))
    if data is None:
        return None
    return [{k: v["value"] for k, v in b.items()} for b in (data.get("results") or {}).get("bindings", [])]


def _title_words(title: str) -> list[str]:
    """Distinctive words of a title, longest first (ascii_fold also undoes PDF ligatures)."""
    words = re.findall(r"[a-z0-9]+", ascii_fold(title).lower())
    return sorted({w for w in words if len(w) >= 3 and w not in _STOP}, key=len, reverse=True)


async def _dblp_candidates(client, title: str, words: list[str]) -> list[dict]:
    """Records whose title has all the words and about the same length."""
    n = len(title)
    has = " ".join(f'?t ql:contains-word "{w}" .' for w in words)  # words are [a-z0-9]+
    return await _sparql(client, (
        f"SELECT ?p ?title WHERE {{ ?p dblp:title ?title . ?t ql:contains-entity ?title . {has} "
        f"FILTER(STRLEN(?title) >= {n * 4 // 5} && STRLEN(?title) <= {n * 6 // 5 + 2}) }} LIMIT 100")) or []


async def dblp(client, title: str) -> list[dict]:
    """Returns up to two records: the published version ("dblp") and the CoRR preprint ("dblp-corr")."""
    words = _title_words(title)
    if not words:
        return []
    rows = await _dblp_candidates(client, title, words[:8])
    if not rows and len(words) > 3:  # a word garbled in the source title: try the longest three
        rows = await _dblp_candidates(client, title, words[:3])
    ranked = sorted(((title_sim(r["title"], title), r["p"]) for r in rows), reverse=True)
    keys = [p for sim, p in ranked if sim >= MATCH][:6]
    if not keys:
        return []
    values = " ".join(f"<{k}>" for k in keys)
    info = await _sparql(client, (
        f"SELECT ?p ?title ?type ?venue ?year ?doi WHERE {{ VALUES ?p {{ {values} }} "
        "?p dblp:title ?title ; a ?type . FILTER(?type != dblp:Publication) "
        "OPTIONAL { ?p dblp:publishedIn ?venue } OPTIONAL { ?p dblp:yearOfPublication ?year } "
        "OPTIONAL { ?p dblp:doi ?doi } }")) or []
    sigs = await _sparql(client, (
        f"SELECT ?p ?ord ?name WHERE {{ VALUES ?p {{ {values} }} "
        "?p dblp:hasSignature ?s . ?s dblp:signatureOrdinal ?ord ; dblp:signatureDblpName ?name }")) or []

    by_key: dict[str, dict] = {}
    for r in info:  # OPTIONALs can repeat a record; keep the first value of each field
        d = by_key.setdefault(r["p"], {})
        for k, v in r.items():
            d.setdefault(k, v)
    authors: dict[str, list[tuple[int, str]]] = {}
    for s in sigs:
        authors.setdefault(s["p"], []).append((int(s["ord"]), s["name"]))

    found: dict[str, dict] = {}
    for key in keys:  # best title match first
        d = by_key.get(key)
        if not d:
            continue
        corr = d.get("venue") == "CoRR"
        src = "dblp-corr" if corr else "dblp"
        if src in found:
            continue
        doi, aid = split_doi(d.get("doi"))
        found[src] = {
            "source": src,
            "title": clean_ws(d["title"]).rstrip("."),
            "authors": [{"name": person_name(n), "affiliations": []} for _, n in sorted(authors.get(key, []))],
            "year": year_of(d.get("year")),
            "venue": None if corr else d.get("venue"),
            "kind": _KINDS.get(d["type"].rsplit("#", 1)[-1], "misc"),
            "doi": doi,
            "arxiv_id": aid,
            "key": key.rsplit("/rec/", 1)[-1],
        }
    return list(found.values())
