"""Local metadata extraction: identifiers in URLs, PDF text, and publisher landing pages."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import pymupdf

from .util import clean_ws, person_name, year_of

ARXIV_ID = r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})"
ARXIV_IN_TEXT = re.compile(r"arXiv:\s*" + ARXIV_ID + r"(v\d+)?", re.I)
ARXIV_IN_URL = re.compile(r"arxiv\.org/(?:abs|pdf|html|format)/" + ARXIV_ID + r"(v\d+)?", re.I)
DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
DOI_IN_URL = re.compile(r"(?:doi\.org/|/doi/(?:abs/|pdf/|full/|epdf/|book/)?)(10\.\d{4,9}/[^?#\s]+)", re.I)
ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)

BAD_META_TITLE = re.compile(r"^(microsoft word|untitled|title|paper|main|arxiv|\S+\.(pdf|docx?|tex|dvi))\b", re.I)


def clean_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi.strip(), flags=re.I)
    doi = doi.rstrip(".,;)]}").lower()
    return doi if doi.startswith("10.") else None


def split_doi(doi: str | None) -> tuple[str | None, str | None]:
    """Return (doi, arxiv_id); arXiv's DataCite DOIs are turned into arXiv ids."""
    doi = clean_doi(doi)
    if doi and (m := ARXIV_DOI.match(doi)):
        return None, m.group(1)
    return doi, None


def ids_from_url(url: str | None) -> dict:
    out = {}
    if not url:
        return out
    if m := ARXIV_IN_URL.search(url):
        out["arxiv_id"] = m.group(1).removesuffix(".pdf")
    if m := DOI_IN_URL.search(url):
        doi, aid = split_doi(m.group(1))
        if doi:
            out["doi"] = doi
        if aid and "arxiv_id" not in out:
            out["arxiv_id"] = aid
    return out


def pdf_url_for(url: str) -> str | None:
    """Known landing-page -> PDF rewrites, so we can skip HTML scraping."""
    if m := ARXIV_IN_URL.search(url):
        return f"https://arxiv.org/pdf/{m.group(1)}{m.group(2) or ''}"
    if m := re.search(r"openreview\.net/forum\?id=([\w\-]+)", url):
        return f"https://openreview.net/pdf?id={m.group(1)}"
    if m := re.search(r"(aclanthology\.org/[\w.\-]+?)/?$", url):
        if not url.endswith(".pdf"):
            return f"https://{m.group(1)}.pdf"
    return None


# ------------------------------------------------------------------ PDF

def _title_from_layout(page) -> str | None:
    """The largest horizontal text in the top part of page 1 is usually the title."""
    h = page.rect.height
    spans = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            if abs(line["dir"][0] - 1) > 0.01:  # skip rotated text such as arXiv's margin stamp
                continue
            for s in line["spans"]:
                t = s["text"].strip()
                if len(t) >= 1 and s["bbox"][1] < h * 0.55:
                    spans.append((round(s["size"], 1), s["bbox"][1], s["bbox"][0], t))
    for size in sorted({s[0] for s in spans}, reverse=True)[:4]:
        sel = sorted((s for s in spans if abs(s[0] - size) < 0.6), key=lambda s: (round(s[1] / 3), s[2]))
        text = clean_ws(" ".join(s[3] for s in sel))
        if 10 <= len(text) <= 300 and re.search(r"[A-Za-z]{3}", text):
            return text
    return None


def _abstract_from_text(text: str) -> str | None:
    m = re.search(
        r"(?is)\babstract\b[\s.:—–-]*(.+?)"
        r"(?=\n\s*(?:\d\.?|I\.?)?\s*introduction\b|\n\s*(?:keywords|index terms|ccs concepts|"
        r"acm reference format|additional key words)\b|\Z)",
        text,
    )
    if not m:
        return None
    body = re.sub(r"-\n(?=[a-z])", "", m.group(1))
    body = clean_ws(body)
    return body if 200 <= len(body) <= 4000 else None


def is_pdf(data: bytes | None) -> bool:
    return bool(data) and b"%PDF-" in data[:1024]


def analyze_pdf(data: bytes) -> dict | None:
    if not is_pdf(data):
        return None
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        return None
    try:
        texts = [p.get_text() for p in doc]
        first = texts[0] if texts else ""
        head = "\n".join(texts[:2])
        info: dict = {"pages": doc.page_count, "fulltext": "\n".join(texts)}

        if m := ARXIV_IN_TEXT.search(head):
            info["arxiv_id"] = m.group(1)
        if m := DOI_IN_TEXT.search(first):
            doi, aid = split_doi(m.group(1))
            if doi:
                info["doi"] = doi
            elif aid:
                info.setdefault("arxiv_id", aid)

        titles = []
        meta_title = clean_ws((doc.metadata or {}).get("title"))
        if len(meta_title) >= 10 and not BAD_META_TITLE.match(meta_title):
            titles.append(meta_title)
        if doc.page_count and (t := _title_from_layout(doc[0])):
            titles.append(t)
        info["titles"] = titles
        info["abstract"] = _abstract_from_text(first) or _abstract_from_text(head)

        if doc.page_count:
            page = doc[0]
            zoom = 360 / max(page.rect.width, 1)
            info["thumbnail"] = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
        return info
    finally:
        doc.close()


# ----------------------------------------------------------- landing pages

class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = dict(attrs)
        name = (a.get("name") or a.get("property") or "").lower()
        if name and a.get("content"):
            self.meta.setdefault(name, []).append(a["content"])


def parse_landing(html: str, base_url: str) -> dict:
    """Google-Scholar style citation_* tags, which most publishers and repositories emit."""
    p = _MetaParser()
    try:
        p.feed(html)
    except Exception:
        pass
    m = p.meta

    def first(*keys):
        for k in keys:
            if m.get(k):
                return clean_ws(m[k][0])
        return None

    authors = m.get("citation_author") or m.get("dc.creator") or []
    venue = first("citation_conference_title", "citation_journal_title", "citation_inbook_title")
    rec = {
        "source": "landing",
        "title": first("citation_title", "dc.title"),
        "authors": [{"name": person_name(a), "affiliations": []} for a in authors if a.strip()],
        "venue": venue,
        "kind": "article" if m.get("citation_journal_title") else ("inproceedings" if venue else None),
        "year": year_of(first("citation_publication_date", "citation_date", "citation_online_date", "dc.date")),
        "abstract": first("citation_abstract", "dc.description"),
        "pdf_url": urljoin(base_url, first("citation_pdf_url")) if first("citation_pdf_url") else None,
    }
    doi, aid = split_doi(first("citation_doi", "dc.identifier"))
    rec["doi"] = doi
    rec["arxiv_id"] = first("citation_arxiv_id") or aid
    rec = {k: v for k, v in rec.items() if v}
    return rec if len(rec) > 1 else {}
