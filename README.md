# paperboat

A personal paper collector. Other machines send it a link or a PDF; it stores the PDF,
looks up the metadata, and serves a web app for browsing, reading, searching, annotating,
and exporting citations.

## Running

```sh
nix run .                         # serves on 0.0.0.0:8077, data in ~/.local/share/paperboat
nix run . -- --port 9000 --data /srv/papers
nix develop -c python -m paperboat --reload   # development
nix develop -c pytest -q                      # offline tests
```

Environment variables: `PAPERBOAT_DATA`, `PAPERBOAT_HOST`, `PAPERBOAT_PORT`, and optional
`S2_API_KEY` (Semantic Scholar) and `OPENALEX_API_KEY` for higher rate limits.

There is no authentication. Bind it to a trusted network such as a LAN or tailnet.

To keep it running as a user service (`~/.config/systemd/user/paperboat.service`):

```ini
[Unit]
Description=paperboat paper collector
After=network-online.target

[Service]
WorkingDirectory=%h/apps/paperboat
ExecStart=/nix/var/nix/profiles/default/bin/nix run .
Restart=on-failure

[Install]
WantedBy=default.target
```

```sh
systemctl --user enable --now paperboat && loginctl enable-linger $USER
```

## Where metadata comes from

| Source | Used for |
| --- | --- |
| URL and PDF text | arXiv id, DOI, title (largest text on page 1), abstract, full text for search |
| Publisher page `citation_*` tags | title, authors, venue, year, DOI, link to the PDF |
| arXiv API | title, authors, abstract, year, published DOI |
| Crossref | metadata for a DOI |
| doi.org | publisher BibTeX |
| OpenAlex | institutions and countries of authors, published venue, abstract |
| DBLP | venue abbreviations and clean BibTeX for CS papers |
| Semantic Scholar | venue and abstract |

Every source is best-effort. DBLP and Semantic Scholar often throttle anonymous clients;
when they do, the venue of an arXiv paper may stay "arXiv". You can then fix it by hand,
or click **Look up again** later. A paper is *complete* when it has a PDF, title,
authors, year, venue, and abstract. BibTeX is always produced: from DBLP or the
publisher when available, otherwise generated. Citation keys look like
`vaswani2017attention` and are unique across the library.

The database records its schema version (`PRAGMA user_version`). paperboat upgrades it
on start using the steps in `db.MIGRATIONS`.

## API (for remote clients)

### Submit a paper: `POST /api/papers`

Send any one of:

```sh
# a link (arXiv, DOI, publisher page, direct PDF, ...)
curl -X POST http://HOST:8077/api/papers -H 'content-type: application/json' \
     -d '{"url": "https://arxiv.org/abs/1706.03762", "origin": "malt"}'

# a downloaded PDF (url is optional and helps the lookup)
curl -X POST http://HOST:8077/api/papers -F file=@paper.pdf -F origin=malt -F url=https://...

# raw bytes
curl -X POST 'http://HOST:8077/api/papers?origin=malt' -H 'content-type: application/pdf' --data-binary @paper.pdf
```

Optional hint fields, which override anything looked up: `title`, `doi`, `arxiv_id`,
`venue`, `year`, `tags` (comma-separated).

Responses:

| Code | Meaning |
| --- | --- |
| `201` | stored as a new paper |
| `200` | already in the library (`"duplicate": true`) |
| `400` | nothing usable: unreachable URL, not a PDF, and so on (`{"status": "error", "error": ...}`) |

A `201` or `200` paper can still have `"status": "incomplete"`: see `missing` and `fix`.

```json
{
  "status": "incomplete",
  "duplicate": false,
  "id": 12,
  "missing": ["venue", "pdf"],
  "warning": "could not fetch https://dl.acm.org/...: HTTP 403",
  "paper": { "...": "full record" },
  "fix": {
    "method": "PATCH", "url": "/api/papers/12", "fields": ["venue"],
    "attach_pdf": { "method": "POST", "url": "/api/papers/12/pdf" }
  }
}
```

### Completing a paper

The client asks the user for the fields listed in `fix.fields` and sends them back:

```sh
curl -X PATCH http://HOST:8077/api/papers/12 -H 'content-type: application/json' \
     -d '{"venue": "PLDI", "authors": ["Ada Lovelace", "Charles Babbage"], "year": 2024}'
curl -X POST http://HOST:8077/api/papers/12/pdf -F file=@paper.pdf   # when "pdf" is missing
```

`PATCH` always returns `200` with `{"status", "missing", "paper"}`. Check `status`.

Editable fields: `title`, `authors` (a list of names, a list of `{name, affiliations}`, or
one name per line), `year`, `venue`, `kind` (`inproceedings`/`article`/`misc`), `abstract`,
`doi`, `arxiv_id`, `url`, `bibtex` (an empty string regenerates it), `tags`, `notes`,
`read_state` (`unread`/`reading`/`read`), `starred`.

### Other endpoints

| Endpoint | |
| --- | --- |
| `GET /api/papers[?q=...]` | all papers, or full-text search results with snippets |
| `GET /api/papers/{id}` | one paper |
| `POST /api/papers/{id}/resolve[?overwrite=true]` | look the paper up again using its current title and ids |
| `DELETE /api/papers/{id}` | delete the paper and its PDF |
| `GET /api/papers/{id}/pdf[?download=1]` | the PDF, inline or as an attachment |
| `GET /api/papers/{id}/bibtex[?download=1]` | the paper's BibTeX entry |
| `GET /api/export.bib[?ids=1,2,3][&download=1]` | BibTeX for the whole library or for the listed ids |

## Frontend

Open `http://HOST:8077/`.

- **Add papers:** drop PDFs or links anywhere on the page, paste a URL, or use **Add paper**.
- **Browse:** the sidebar filters by author, institution, country, venue, year, tag, or the
  machine a paper came from. **Group** splits the list into sections by the same keys.
- **Search:** full-text search over titles, authors, affiliations, abstracts, notes, tags,
  and the PDF text, with highlighted snippets.
- **Read:** an in-app reader with a notes panel beside the PDF, or open the PDF in a new tab.
- **Annotate:** tags, notes, a star, and a reading state (unread, reading, read).
- **Cite and export:** copy BibTeX or a plain-text citation, or download `.bib` for one
  paper, the selected papers, the papers shown, or the whole library.
- **Share:** the native share sheet where the browser has one; otherwise copy the page
  link, PDF link, or citation, or send it by email.
- **Keyboard:** `/` search, `j`/`k` move, `o` or `Enter` read, `s` star, `e` edit,
  `b` copy BibTeX, `a` add, `Esc` close.
- **Bookmarkable views:** `/?q=attention&group=venue`.
