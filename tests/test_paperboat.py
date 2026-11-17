"""Offline tests: identifier parsing, merging, BibTeX, and the HTTP flow with lookups stubbed out."""

import json
import sqlite3

import pymupdf
import pytest
from fastapi.testclient import TestClient

from paperboat import bibtex, config, db, extract, ingest, venues
from paperboat.resolve import merge

ABSTRACT = ("We study the problem of collecting papers from many machines into one library. " * 4).strip()


def make_pdf(title="Folding Paper Boats at Scale: A Study", extra="") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), title, fontsize=18)
    page.insert_text((72, 120), "Ada Lovelace, Charles Babbage", fontsize=11)
    body = f"Abstract\n{ABSTRACT}\n1 Introduction\nBoats. {extra}"
    page.insert_textbox(pymupdf.Rect(72, 150, 540, 700), body, fontsize=10)
    return doc.tobytes()


# ------------------------------------------------------------- parsing

@pytest.mark.parametrize("url,expected", [
    ("https://arxiv.org/abs/1706.03762v5", {"arxiv_id": "1706.03762"}),
    ("https://arxiv.org/pdf/2005.14165.pdf", {"arxiv_id": "2005.14165"}),
    ("https://dl.acm.org/doi/10.1145/3591280", {"doi": "10.1145/3591280"}),
    ("https://doi.org/10.48550/arXiv.1810.04805", {"arxiv_id": "1810.04805"}),
    ("https://example.com/paper.pdf", {}),
])
def test_ids_from_url(url, expected):
    assert extract.ids_from_url(url) == expected


def test_pdf_url_rewrites():
    assert extract.pdf_url_for("https://arxiv.org/abs/1706.03762v2") == "https://arxiv.org/pdf/1706.03762v2"
    assert extract.pdf_url_for("https://openreview.net/forum?id=abc-1") == "https://openreview.net/pdf?id=abc-1"


def test_parse_landing():
    html = """<html><head>
      <meta name="citation_title" content="{Paper} Boats">
      <meta name="citation_author" content="Lovelace, Ada">
      <meta name="citation_author_institution" content="Analytical Engines Ltd">
      <meta name="citation_author" content="Charles Babbage">
      <meta name="citation_conference_title" content="BOATS '24">
      <meta name="citation_publication_date" content="2024/05/01">
      <meta name="citation_doi" content="10.1234/boats.1">
      <meta name="citation_pdf_url" content="/files/boats.pdf">
    </head></html>"""
    rec = extract.parse_landing(html, "https://pub.example/paper/1")
    assert rec["title"] == "Paper Boats"
    assert [a["name"] for a in rec["authors"]] == ["Ada Lovelace", "Charles Babbage"]
    assert [a["affiliations"] for a in rec["authors"]] == [[{"name": "Analytical Engines Ltd"}], []]
    assert rec["year"] == 2024 and rec["kind"] == "inproceedings"
    assert rec["pdf_url"] == "https://pub.example/files/boats.pdf"
    assert extract.parse_landing("<html>challenge</html>", "https://x") == {}


USENIX_PAGE = "https://www.usenix.org/conference/osdi22/presentation/lovelace"


def make_usenix_pdf() -> bytes:
    """A USENIX-style PDF: a cover page naming the paper's page, then the paper."""
    doc = pymupdf.open()
    cover = doc.new_page()
    cover.insert_textbox(pymupdf.Rect(72, 72, 540, 300),
                         "This paper is included in the Proceedings of the\n16th USENIX Symposium on "
                         "Operating Systems Design and Implementation.\nJuly 11-13, 2022", fontsize=10)
    cover.insert_text((72, 340), "Folding Paper Boats at Scale", fontsize=18)
    cover.insert_text((72, 370), "Ada Lovelace, Analytical Engines Ltd", fontsize=10)
    cover.insert_text((72, 390), USENIX_PAGE, fontsize=10)
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 540, 700), f"Folding Paper Boats at Scale\nAbstract\n{ABSTRACT}", fontsize=10)
    return doc.tobytes()


def test_analyze_pdf_finds_usenix_page():
    info = extract.analyze_pdf(make_usenix_pdf())
    assert info["landing_url"] == USENIX_PAGE
    assert "landing_url" not in extract.analyze_pdf(make_pdf())
    older = "https://www.usenix.org/conference/atc17/technical-sessions/presentation/lawall"
    assert extract.LANDING_IN_PDF.search(older).group(0) == older


def test_usenix_pdf_gets_metadata_from_its_page(client, monkeypatch):
    fetched = []

    async def fake_fetch_landing(url):
        fetched.append(url)
        return {"source": "landing", "title": "Folding Paper Boats at Scale", "year": 2022, "kind": "inproceedings",
                "venue": "16th USENIX Symposium on Operating Systems Design and Implementation (OSDI 22)",
                "authors": [{"name": "Ada Lovelace", "affiliations": [{"name": "Analytical Engines Ltd"}]}]}

    monkeypatch.setattr(ingest, "fetch_landing", fake_fetch_landing)
    r = client.post("/api/papers", files={"file": ("osdi22-lovelace.pdf", make_usenix_pdf(), "application/pdf")})
    body = r.json()
    assert r.status_code == 201 and body["status"] == "complete", body["missing"]
    p = body["paper"]
    assert p["venue"] == "OSDI" and p["year"] == 2022 and p["url"] == USENIX_PAGE
    assert p["authors"][0]["affiliations"] == [{"name": "Analytical Engines Ltd"}]

    client.patch(f"/api/papers/{p['id']}", json={"venue": ""})
    p = client.post(f"/api/papers/{p['id']}/resolve").json()["paper"]  # Look up again reads the page too
    assert p["venue"] == "OSDI" and fetched == [USENIX_PAGE, USENIX_PAGE]


def test_analyze_pdf():
    info = extract.analyze_pdf(make_pdf(extra="See arXiv:2401.01234v2 for details."))
    assert info["titles"][-1] == "Folding Paper Boats at Scale: A Study"
    assert info["arxiv_id"] == "2401.01234"
    assert info["abstract"].startswith("We study the problem")
    assert info["thumbnail"][:4] == b"\x89PNG"
    assert extract.analyze_pdf(b"<html>not a pdf</html>") is None


# --------------------------------------------------------------- merging

def test_merge_prefers_published_venue_and_openalex_affiliations():
    recs = [
        {"source": "arxiv", "title": "T", "year": 2023, "arxiv_id": "2301.00001", "abstract": ABSTRACT,
         "authors": [{"name": "Ada Lovelace", "affiliations": []}]},
        {"source": "openalex", "title": "T", "venue": "PLDI", "year": 2024,
         "authors": [{"name": "A. Lovelace", "affiliations": [{"name": "Analytical Engines Ltd", "country": "GB"}]}]},
        {"source": "dblp", "title": "T", "venue": "PLDI", "year": 2024, "bibtex": "@inproceedings{DBLP:x, title={T}}"},
    ]
    m = merge(recs)
    assert m["venue"] == "PLDI" and m["year"] == 2024
    assert m["authors"][0]["affiliations"][0]["name"] == "Analytical Engines Ltd"
    assert m["bibtex_source"] == "dblp"


def test_merge_takes_affiliations_from_landing_page_when_dblp_has_the_authors():
    recs = [
        {"source": "dblp", "title": "T", "venue": "OSDI", "authors": [{"name": "Ada Lovelace", "affiliations": []}]},
        {"source": "landing", "title": "T", "authors": [{"name": "Ada Lovelace", "affiliations": [{"name": "Engines Ltd"}]}]},
    ]
    assert merge(recs)["authors"][0]["affiliations"] == [{"name": "Engines Ltd"}]


def test_merge_rejects_citation_line_as_abstract_and_falls_back_to_arxiv_venue():
    recs = [
        {"source": "openalex", "title": "T", "abstract": "Ada Lovelace, Charles Babbage. Proceedings of Boats. " * 4,
         "authors": [{"name": "Ada Lovelace"}, {"name": "Charles Babbage"}]},
        {"source": "pdf", "abstract": ABSTRACT, "arxiv_id": "2301.00001"},
    ]
    m = merge(recs)
    assert m["abstract"] == ABSTRACT
    assert m["venue"] == "arXiv" and m["kind"] == "misc"


def test_dblp_sparql_lookup():
    import asyncio

    import httpx

    from paperboat import sources

    conf, corr = "https://dblp.org/rec/conf/boats/Lovelace24", "https://dblp.org/rec/journals/corr/abs-2401-00001"
    rows = {
        "contains-word": [{"p": conf, "title": "Folding Paper Boats at Scale: A Study."},
                          {"p": corr, "title": "Folding Paper Boats at Scale: A Study"},
                          {"p": "https://dblp.org/rec/x", "title": "Folding Paper Planes at Scale: A Survey"}],
        "hasSignature": [{"p": conf, "ord": "2", "name": "Charles Babbage 0001"}, {"p": conf, "ord": "1", "name": "Ada Lovelace"},
                         {"p": corr, "ord": "1", "name": "Ada Lovelace"}],
        "publishedIn": [{"p": conf, "title": "Folding Paper Boats at Scale: A Study.", "year": "2024", "venue": "BOATS",
                         "type": "https://dblp.org/rdf/schema#Inproceedings", "doi": "https://doi.org/10.1234/BOATS.1"},
                        {"p": corr, "title": "Folding Paper Boats at Scale: A Study", "year": "2024", "venue": "CoRR",
                         "type": "https://dblp.org/rdf/schema#Informal", "doi": "https://doi.org/10.48550/arXiv.2401.00001"}],
    }
    queries = []

    def answer(request):
        q = request.url.params["query"]
        queries.append(q)
        kind = next(k for k in rows if k in q)
        bindings = [{k: {"type": "literal", "value": v} for k, v in r.items()} for r in rows[kind]]
        return httpx.Response(200, json={"results": {"bindings": bindings}},
                              headers={"content-type": "application/sparql-results+json"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as c:
            return await sources.dblp(c, "Folding Paper Boats at Scale: A Study")

    pub, pre = sorted(asyncio.run(run()), key=lambda r: r["source"])
    assert pub["source"] == "dblp" and pub["venue"] == "BOATS" and pub["kind"] == "inproceedings"
    assert pub["year"] == 2024 and pub["doi"] == "10.1234/boats.1" and pub["title"].endswith("Study")
    assert [a["name"] for a in pub["authors"]] == ["Ada Lovelace", "Charles Babbage"]
    assert pre["source"] == "dblp-corr" and pre["venue"] is None and pre["arxiv_id"] == "2401.00001"
    assert all(w in queries[0] for w in ('"folding"', '"boats"', '"study"', '"scale"', '"paper"'))


def test_bibtex_generate_and_rekey():
    p = {"title": "The Boat & You", "authors": [{"name": "Ada Lovelace"}], "year": 2024,
         "venue": "BOATS", "kind": "inproceedings"}
    key = bibtex.make_key(p)
    assert key == "lovelace2024boat"
    bib = bibtex.generate(p, key)
    assert bib.startswith("@inproceedings{lovelace2024boat,") and r"The Boat \& You" in bib and "booktitle" in bib
    assert bibtex.rekey("@article{DBLP:journals/x/Y24,\n title={T}}", "k1").startswith("@article{k1,")


# ------------------------------------------------------------ HTTP flow

@pytest.fixture
def client(tmp_path, monkeypatch):
    config.set_data_dir(tmp_path)

    async def fake_resolve(**kw):  # nothing found online
        return merge([r for r in (kw.get("landing"), kw.get("pdf")) if r])

    monkeypatch.setattr(ingest, "resolve", fake_resolve)
    from paperboat.app import app
    with TestClient(app) as c:
        yield c


def test_submit_incomplete_then_complete_via_patch(client):
    pdf = make_pdf()
    r = client.post("/api/papers", files={"file": ("boats.pdf", pdf, "application/pdf")}, data={"origin": "malt"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "incomplete" and not body["duplicate"]
    assert set(body["missing"]) == {"authors", "year", "venue"}
    assert body["fix"]["method"] == "PATCH" and body["fix"]["fields"] == body["missing"]
    pid = body["id"]
    assert body["paper"]["title"] == "Folding Paper Boats at Scale: A Study"
    assert body["paper"]["origin"] == "malt"

    r = client.patch(f"/api/papers/{pid}", json={"authors": "Ada Lovelace\nCharles Babbage", "year": "2024", "venue": "BOATS"})
    assert r.status_code == 200 and r.json()["status"] == "complete"
    paper = r.json()["paper"]
    assert paper["bibtex_key"] == "lovelace2024folding"

    # duplicate upload is recognized
    r = client.post("/api/papers", files={"file": ("again.pdf", pdf, "application/pdf")})
    assert r.status_code == 200 and r.json()["duplicate"] and r.json()["id"] == pid
    existing = r.json()["existing"]
    assert existing["matched_on"] == "pdf" and not existing["pdf_replaced"]
    assert existing["link"] == f"http://testserver/#/paper/{pid}"

    # annotations, search (title, notes, full text), export, files
    client.patch(f"/api/papers/{pid}", json={"tags": "boats, origami", "notes": "check the keel experiments", "starred": True})
    for q in ("folding", "keel", "introduction boats", "lovelace"):
        hits = client.get("/api/papers", params={"q": q}).json()["papers"]
        assert [h["id"] for h in hits] == [pid], q
    assert "lovelace2024folding" in client.get("/api/export.bib").text
    r = client.get(f"/api/papers/{pid}/pdf", params={"download": 1})
    assert r.headers["content-type"] == "application/pdf" and "attachment" in r.headers["content-disposition"]
    assert client.get(f"/api/papers/{pid}/thumb").headers["content-type"] == "image/png"

    assert client.delete(f"/api/papers/{pid}").status_code == 200
    assert client.get("/api/papers").json()["papers"] == []


def test_patch_keeps_affiliations_and_fetched_bibtex_when_unchanged(client):
    r = client.post("/api/papers", files={"file": ("b.pdf", make_pdf("Another Boat Paper Title"), "application/pdf")})
    pid = r.json()["id"]
    client.patch(f"/api/papers/{pid}", json={"authors": [{"name": "Ada Lovelace", "affiliations": ["Engines Ltd"]}],
                                            "bibtex": "@inproceedings{x, title={Fetched}}"})
    p = client.patch(f"/api/papers/{pid}", json={"authors": "Ada Lovelace", "notes": "hi"}).json()["paper"]
    assert p["authors"][0]["affiliations"] == [{"name": "Engines Ltd"}]
    assert p["bibtex_source"] == "manual" and "Fetched" in p["bibtex"]


def test_rejects_non_pdf_and_empty_requests(client):
    r = client.post("/api/papers", files={"file": ("x.pdf", b"<html>nope</html>", "application/pdf")})
    assert r.status_code == 400 and r.json()["status"] == "error"
    assert client.post("/api/papers", json={}).status_code == 400


@pytest.mark.parametrize("given,expected", [
    ("Advances in Neural Information Processing Systems 36", "NeurIPS"),
    ("NIPS", "NeurIPS"),
    ("Proceedings of the 45th ACM SIGPLAN International Conference on Programming Language "
     "Design and Implementation", "PLDI"),
    ("2024 IEEE Symposium on Security and Privacy (SP)", "IEEE S&P"),
    ("PLDI '24: Proceedings of whatever", "PLDI"),
    ("CoRR", "arXiv"),
    ("Machine Learning", "Machine Learning"),          # the journal, not ICML
    ("Workshop on Boats (PLDI)", "Workshop on Boats (PLDI)"),
    ("BOATS", "BOATS"),
])
def test_venue_canonical(given, expected):
    assert venues.canonical(given) == expected


def test_merge_skips_preprint_venue_and_canonicalizes():
    recs = [
        {"source": "s2", "title": "T", "venue": "arXiv.org", "arxiv_id": "2301.00001"},
        {"source": "crossref", "title": "T", "venue": "Proceedings of the ACM on Programming Languages"},
    ]
    assert merge(recs)["venue"] == "PACMPL"
    assert merge(recs[:1])["venue"] == "arXiv"


def test_complete_without_abstract_and_arxiv_counts_as_venue():
    p = {"title": "T", "authors": [{"name": "A"}], "year": 2024, "pdf_file": "x.pdf"}
    assert db.compute_missing(p) == ["venue"]
    assert db.compute_missing({**p, "arxiv_id": "2401.00001"}) == []


def _complete(client, title="Folding Paper Boats at Scale: A Study", extra=""):
    r = client.post("/api/papers", files={"file": ("p.pdf", make_pdf(title, extra), "application/pdf")})
    pid = r.json()["id"]
    client.patch(f"/api/papers/{pid}", json={"authors": "Ada Lovelace", "year": 2024, "venue": "BOATS"})
    return pid


def test_citation_key_stays_fixed(client):
    pid = _complete(client)
    p = client.patch(f"/api/papers/{pid}", json={"title": "Sinking Paper Boats", "year": 2025}).json()["paper"]
    assert p["bibtex_key"] == "lovelace2024folding" and "{lovelace2024folding," in p["bibtex"]
    assert "key_locked" not in p

    p = client.patch(f"/api/papers/{pid}", json={"bibtex_key": "boats-main"}).json()["paper"]
    assert p["bibtex_key"] == "boats-main" and p["bibtex"].startswith("@inproceedings{boats-main,")
    p = client.patch(f"/api/papers/{pid}", json={"bibtex_key": ""}).json()["paper"]
    assert p["bibtex_key"] == "lovelace2025sinking"

    other = _complete(client, "An Entirely Different Raft Paper")
    r = client.patch(f"/api/papers/{other}", json={"bibtex_key": "lovelace2025sinking"})
    assert r.status_code == 409
    assert client.patch(f"/api/papers/{other}", json={"bibtex_key": "bad key!"}).status_code == 400


def test_same_title_is_a_duplicate_and_newest_pdf_wins(client):
    pid = _complete(client)
    old_file = next(config.PDF_DIR.iterdir())
    r = client.post("/api/papers", files={"file": ("v2.pdf", make_pdf(extra="Camera-ready."), "application/pdf")})
    body = r.json()
    assert r.status_code == 200 and body["duplicate"] and body["id"] == pid
    assert body["existing"]["matched_on"] == "title" and body["existing"]["pdf_replaced"]
    assert not old_file.exists() and len(list(config.PDF_DIR.iterdir())) == 1
    hits = client.get("/api/papers", params={"q": "camera"}).json()["papers"]
    assert [h["id"] for h in hits] == [pid]


def test_same_title_with_different_arxiv_id_is_a_new_paper(client):
    pid = _complete(client, extra="arXiv:2401.00001")
    r = client.post("/api/papers", files={"file": ("x.pdf", make_pdf(extra="arXiv:2402.00002"), "application/pdf")})
    assert r.status_code == 201 and r.json()["id"] != pid


def test_api_doc_is_markdown_with_this_host(client):
    r = client.get("/api.md")
    assert r.headers["content-type"].startswith("text/markdown")
    assert "http://testserver/api/papers" in r.text and "HOST:8077" not in r.text


# ------------------------------------------------------------ migrations

def test_migrates_unversioned_v1_database(tmp_path):
    config.set_data_dir(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    con.executescript(db.SCHEMA_V1)  # a database from before versioning: user_version 0
    con.execute("INSERT INTO papers (title, authors, year, venue, bibtex_key, pdf_file, status, missing,"
                " added_at, updated_at) VALUES ('T', ?, 2024, 'X', 'a2024t', 'f.pdf', 'incomplete',"
                " '[\"abstract\"]', 'now', 'now')", (json.dumps([{"name": "A"}]),))
    con.commit()
    con.close()
    db.init()
    db.init()  # idempotent
    with db.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        p = db.get(con, 1)
    assert p["status"] == "complete" and p["missing"] == [] and p["key_locked"]


def test_refuses_newer_database(tmp_path):
    config.set_data_dir(tmp_path)
    db.init()
    with db.connect() as con:
        con.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    with pytest.raises(RuntimeError, match="newer"):
        db.init()
