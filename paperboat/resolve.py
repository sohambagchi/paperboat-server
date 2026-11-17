"""Gather metadata from every source that applies and merge it field by field."""

import asyncio
import logging

import httpx

from . import config, sources, venues
from .util import last_name, title_sim

log = logging.getLogger("paperboat.resolve")

# Field -> sources in order of trust. "pdf" is local text extraction, the last resort.
PRIORITY = {
    "title":    ["arxiv", "crossref", "dblp", "dblp-corr", "landing", "s2", "openalex", "pdf"],
    "authors":  ["arxiv", "crossref", "dblp", "dblp-corr", "landing", "s2", "openalex"],
    "venue":    ["dblp", "s2", "crossref", "landing", "openalex"],
    "kind":     ["dblp", "crossref", "landing", "s2", "openalex"],
    "year":     ["dblp", "crossref", "s2", "openalex", "landing", "arxiv", "dblp-corr"],
    "abstract": ["arxiv", "s2", "openalex", "crossref", "landing", "pdf"],
    "doi":      ["crossref", "landing", "dblp", "s2", "openalex", "arxiv", "pdf"],
    "arxiv_id": ["arxiv", "landing", "s2", "openalex", "dblp-corr", "pdf"],
    "bibtex":   ["dblp", "doi", "dblp-corr"],
}
AFFILIATION_SOURCES = ["openalex", "landing", "crossref", "arxiv"]


def merge(recs: list[dict]) -> dict:
    by_src: dict[str, dict] = {}
    for r in recs:
        by_src.setdefault(r["source"], {}).update({k: v for k, v in r.items() if v})
    out: dict = {}
    preprint = False
    for field, order in PRIORITY.items():
        for src in order:
            v = by_src.get(src, {}).get(field)
            if field == "abstract" and not _plausible_abstract(v, recs):
                continue
            if field == "venue" and v and venues.canonical(v) == "arXiv":
                preprint = True  # S2/OpenAlex say "arXiv.org" even once it's published
                continue
            if v:
                out[field] = v
                if field == "bibtex":
                    out["bibtex_source"] = "doi" if src == "doi" else "dblp"
                break
    if (v := out.get("venue")) and v.islower():  # OpenAlex sometimes has "neural information processing systems"
        small = {"of", "on", "for", "and", "the", "in", "to", "a"}
        out["venue"] = " ".join(w if i and w in small else w.capitalize() for i, w in enumerate(v.split()))
    out["venue"] = venues.canonical(out.get("venue"))
    if not out.get("venue") and (out.get("arxiv_id") or preprint):
        out["venue"] = venues.canonical(by_src.get("arxiv", {}).get("journal_ref")) or "arXiv"
        if out["venue"] == "arXiv":
            out["kind"] = "misc"
    out["authors"] = _with_affiliations(out.get("authors") or [], by_src)
    out["sources"] = sorted(s for s in by_src if s not in ("hint",))
    # Whatever the submitting client told us explicitly wins.
    out.update({k: v for k, v in by_src.get("hint", {}).items() if k != "source" and v})
    out["venue"] = venues.canonical(out.get("venue"))
    return out


def _plausible_abstract(text: str | None, recs: list[dict]) -> bool:
    """Some indexes store a citation line ("A, B, C. Proceedings of ...") as the abstract."""
    if not text or len(text) < 150:
        return False
    head = text[:150].lower()
    names = {last_name(a["name"]).lower() for r in recs for a in (r.get("authors") or [])[:3]}
    return sum(1 for n in names if n and len(n) > 2 and n in head) < 2


def _with_affiliations(authors: list[dict], by_src: dict) -> list[dict]:
    """Attach affiliations by last-name match. OpenAlex wins because its institutions are
    normalized (with country and type), which is what makes grouping by institution work."""
    authors = [{"name": a["name"], "affiliations": list(a.get("affiliations") or [])} for a in authors]
    for src in AFFILIATION_SOURCES:
        other = by_src.get(src, {}).get("authors") or []
        pool = {last_name(a["name"]).lower(): a for a in other if a.get("affiliations")}
        for i, a in enumerate(authors):
            if a["affiliations"] and src != "openalex":
                continue
            match = pool.get(last_name(a["name"]).lower())
            if match is None and len(other) == len(authors) and other[i].get("affiliations"):
                match = other[i]
            if match:
                a["affiliations"] = match["affiliations"]
    return authors


async def resolve(*, arxiv_id=None, doi=None, titles=(), landing=None, pdf=None, hints=None) -> dict:
    """Look a paper up everywhere we can and merge the results.

    titles: candidate titles (from hints / PDF), tried in order when there is no identifier.
    landing: record parsed from a publisher page. pdf: record from local PDF extraction.
    """
    recs: list[dict] = []
    if hints:
        recs.append({"source": "hint", **hints})
    if landing:
        recs.append(landing)
    if pdf:
        recs.append(pdf)

    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT},
                                 timeout=config.HTTP_TIMEOUT, follow_redirects=True) as c:
        # 1. authoritative lookups by identifier
        first = await asyncio.gather(
            sources.arxiv(c, arxiv_id) if arxiv_id else _none(),
            sources.crossref(c, doi) if doi else _none(),
        )
        recs += [r for r in first if r]
        if not doi:  # arXiv often knows the published DOI
            doi = next((r["doi"] for r in recs if r.get("doi")), None)
            if doi:
                if cr := await sources.crossref(c, doi):
                    recs.append(cr)

        known_title = next((r["title"] for r in recs if r.get("title") and r["source"] in ("arxiv", "crossref", "landing", "hint")), None)
        candidates = [known_title] if known_title else [t for t in titles if t][:2]

        # 2. enrichment: venue/affiliations/bibtex, and title search when we have no identifier
        for title in candidates:
            s2_ident = f"ARXIV:{arxiv_id}" if arxiv_id else (f"DOI:{doi}" if doi else None)
            got = await asyncio.gather(
                sources.dblp(c, title),
                sources.openalex(c, doi=doi) if doi else sources.openalex(c, title=title),
                sources.semantic_scholar(c, ident=s2_ident) if s2_ident else sources.semantic_scholar(c, title=title),
            )
            dblp_recs, oa, s2 = got
            if oa and oa.get("title") and title_sim(oa["title"], title) < sources.MATCH:
                oa = None  # DOI lookups can resolve to a different work (e.g. a book chapter)
            new = [*dblp_recs, *(r for r in (oa, s2) if r)]
            if not oa and doi and not arxiv_id and (oa := await sources.openalex(c, title=title)):
                new.append(oa)
            recs += new
            if new or known_title:
                break

        # 3. a published DOI discovered via enrichment gets Crossref + doi.org BibTeX
        if not doi:
            doi = next((r["doi"] for r in recs if r.get("doi") and r["source"] in ("dblp", "s2", "openalex")), None)
            if doi and (cr := await sources.crossref(c, doi)):
                recs.append(cr)
        have_dblp_bib = any(r["source"] == "dblp" and r.get("bibtex") for r in recs)
        if doi and not have_dblp_bib and (bib := await sources.doi_bibtex(c, doi)):
            recs.append(bib)

    for r in recs:
        log.info("source %s: %s", r["source"], sorted(k for k, v in r.items() if v and k != "source"))
    return merge(recs)


async def _none():
    return None
