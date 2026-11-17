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
| USENIX paper page, named on the PDF's cover page | title, authors with institutions, venue, year |
| arXiv API | title, authors, abstract, year, published DOI |
| Crossref | metadata for a DOI |
| doi.org | publisher BibTeX |
| OpenAlex | institutions and countries of authors, published venue, abstract |
| DBLP | venue abbreviations and clean BibTeX for CS papers |
| Semantic Scholar | venue and abstract |

Every source is best-effort. DBLP and Semantic Scholar often throttle anonymous clients;
when they do, the venue of an arXiv paper may stay "arXiv". You can then fix it by hand,
or click **Look up again** later. A paper is *complete* when it has a PDF, title,
authors, year, and venue; an arXiv id counts as a venue. BibTeX is always produced: from
DBLP or the publisher when available, otherwise generated. Citation keys look like
`vaswani2017attention`, are unique across the library, and stay fixed once first author,
year and title are known, so later edits don't break documents that cite them.

Venue names are mapped to one short name per venue, so "Advances in Neural Information
Processing Systems 36", "NIPS" and "NeurIPS" all group as **NeurIPS**. The table is in
`paperboat/venues.py`. To add or override entries, put `{"Short name": ["alias", ...]}`
in `venues.json` in the data directory and restart.

The same paper is never stored twice. A submission matches an existing paper by identical
PDF, DOI, arXiv id, or nearly the same title with the same first author. A newer PDF
replaces the stored one.

The database records its schema version (`PRAGMA user_version`). paperboat upgrades it
on start using the steps in `db.MIGRATIONS`.

## API (for remote clients)

The full reference is [`paperboat/api.md`](paperboat/api.md). A running server also serves
it as raw Markdown at `http://HOST:8077/api.md`, with its own address filled in, ready to
paste into a client app or a prompt. The web app links to it from the **API** button.

In short: `POST /api/papers` with a link or a PDF. The reply is `201` for a new paper or
`200` with an `existing` link when the paper is already in the library. If `status` is
`"incomplete"`, `PATCH` the fields listed in `fix`.

## Frontend

Open `http://HOST:8077/`.

- **Add papers:** drop PDFs, folders of PDFs, or links anywhere on the page, paste one or
  more URLs, or use **Add paper**. Big drops upload three at a time and end with a summary.
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
