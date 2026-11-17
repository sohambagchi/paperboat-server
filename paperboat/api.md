# paperboat API

paperboat is a personal paper library at `http://HOST:8077`. Send it a link to a paper or
a PDF. It stores the PDF, looks up the metadata (title, authors, venue, year, BibTeX), and
tells you if anything is still missing so you can ask the user for it.

There is no authentication. All requests and responses are JSON unless noted.

## Add a paper: `POST /api/papers`

Send any one of these:

```sh
# a link: arXiv, DOI, publisher page, OpenReview, a direct PDF link, ...
curl -X POST http://HOST:8077/api/papers -H 'content-type: application/json' \
     -d '{"url": "https://arxiv.org/abs/1706.03762", "origin": "laptop"}'

# a PDF file; "url" is optional and helps the lookup
curl -X POST http://HOST:8077/api/papers -F file=@paper.pdf -F origin=laptop -F url=https://...

# raw PDF bytes; fields go in the query string, the file name in X-Filename
curl -X POST 'http://HOST:8077/api/papers?origin=laptop' -H 'content-type: application/pdf' \
     -H 'x-filename: paper.pdf' --data-binary @paper.pdf
```

Optional fields, in the JSON body, the form, or the query string:

| Field | Meaning |
| --- | --- |
| `origin` | name of the sending machine or app, used to filter papers by source. Defaults to the `X-Paperboat-Origin` header, then the client's IP address. |
| `title`, `doi`, `arxiv_id`, `venue`, `year` | hints. They override anything looked up, so send them only when you are sure. |
| `tags` | comma-separated tags, e.g. `"memory models, gpu"` |

A submission can take up to a minute while paperboat queries arXiv, Crossref, DBLP,
OpenAlex and Semantic Scholar. Use a long timeout.

### Response

| Code | Meaning |
| --- | --- |
| `201` | stored as a new paper |
| `200` | the paper was already in the library; nothing new was created (see `existing`) |
| `400` | nothing usable: unreachable URL, not a PDF, and so on. Body: `{"status": "error", "error": "..."}`, or `{"detail": "..."}` when neither a file nor a URL was sent |
| `415` | unsupported content type |

`201` and `200` both return the paper, and its `status` can still be `"incomplete"`:

```json
{
  "status": "incomplete",
  "duplicate": false,
  "id": 12,
  "missing": ["venue", "pdf"],
  "warning": "could not fetch https://dl.acm.org/...: HTTP 403",
  "paper": { "...": "the full paper record, see below" },
  "fix": {
    "method": "PATCH", "url": "/api/papers/12", "fields": ["venue"],
    "attach_pdf": { "method": "POST", "url": "/api/papers/12/pdf" }
  }
}
```

- `status`: `"complete"` when the paper has a title, authors, year, venue and PDF.
  An arXiv id counts as a venue. Otherwise `"incomplete"`, and `missing` lists what is
  absent (`title`, `authors`, `year`, `venue`, `pdf`).
- `fix` is present only when incomplete. `fix.fields` are the metadata fields to ask the
  user for; `fix.attach_pdf` is present when the PDF is missing.
- `warning` is present when something went wrong but the paper was stored anyway, usually
  a publisher page that refused the download.

When the paper was already in the library (`200`, `"duplicate": true`), the response
also says which paper it matched and how:

```json
"existing": {
  "id": 7,
  "title": "Attention Is All You Need",
  "matched_on": "doi",
  "pdf_replaced": true,
  "api_url": "http://HOST:8077/api/papers/7",
  "link": "http://HOST:8077/#/paper/7"
}
```

- `matched_on`: `pdf` (identical file), `doi`, `arxiv_id`, or `title` (nearly the same
  title and the same first author, with no conflicting DOI or arXiv id).
- `pdf_replaced`: the submitted PDF was different and now replaces the stored one. The
  newest PDF always wins, e.g. a published version replaces a preprint.
- `link` opens the paper in the web app. Show it to the user.

### Suggested client flow

1. `POST` the link or PDF.
2. If `duplicate`, tell the user it is already in the library and show `existing.link`.
3. If `status` is `"incomplete"`, ask the user for each field in `fix.fields` and send
   them with `PATCH`. If `fix.attach_pdf` is present and the user has the PDF, upload it.
4. Check `status` in the reply, since a `PATCH` or upload returns the new `status` and `missing`.

## Complete or edit a paper: `PATCH /api/papers/{id}`

Send only the fields that change:

```sh
curl -X PATCH http://HOST:8077/api/papers/12 -H 'content-type: application/json' \
     -d '{"venue": "PLDI", "authors": ["Ada Lovelace", "Charles Babbage"], "year": 2024}'
```

Returns `200` with `{"status", "missing", "paper"}`.

| Field | Value |
| --- | --- |
| `title`, `abstract`, `url` | string |
| `authors` | a list of names, a list of `{"name", "affiliations"}`, or one string with one name per line |
| `year` | integer |
| `venue` | string. Known venues are renamed to their short name: "Programming Language Design and Implementation" becomes "PLDI". |
| `kind` | `inproceedings`, `article`, or `misc` |
| `doi`, `arxiv_id` | string |
| `bibtex` | a full BibTeX entry to use as is; `""` regenerates it from the metadata |
| `bibtex_key` | the citation key. It is derived once from first author, year and title (`vaswani2017attention`) and then stays fixed when metadata changes. Set it to rename it; `""` derives it again. `409` if another paper uses it. |
| `tags` | a list of strings, or one comma-separated string |
| `notes` | string |
| `read_state` | `unread`, `reading`, or `read` |
| `starred` | boolean |

Errors from this and the endpoints below: `{"detail": "..."}` with `400`, `404` (no such
paper) or `409`.

## Attach a PDF: `POST /api/papers/{id}/pdf`

```sh
curl -X POST http://HOST:8077/api/papers/12/pdf -F file=@paper.pdf
# or: -H 'content-type: application/pdf' --data-binary @paper.pdf
```

Replaces any stored PDF. Returns `{"status", "missing", "paper"}`. `400` if the file is not
a PDF or is already stored as a different paper.

## Other endpoints

| Endpoint | |
| --- | --- |
| `GET /api/papers` | all papers, newest first: `{"papers": [...]}` |
| `GET /api/papers?q=...` | full-text search over titles, authors, affiliations, venues, abstracts, notes, tags and PDF text. Each result has a `snippet`, with matches between `\u0002` and `\u0003`. `"quoted phrases"` work; other words match as prefixes. |
| `GET /api/papers/{id}` | one paper |
| `POST /api/papers/{id}/resolve` | look the paper up again with its current title and ids; fills empty fields. `?overwrite=true` replaces filled ones too. |
| `DELETE /api/papers/{id}` | delete the paper and its PDF |
| `GET /api/papers/{id}/pdf` | the PDF; `?download=1` for an attachment named `Author2024 - Title.pdf` |
| `GET /api/papers/{id}/thumb` | PNG of the first page |
| `GET /api/papers/{id}/bibtex` | the BibTeX entry as text; `?download=1` for a `.bib` file |
| `GET /api/export.bib` | BibTeX for the whole library; `?ids=1,2,3` for some papers; `?download=1` |
| `GET /api/info` | server name, host, port and data directory |
| `GET /api.md` | this document |

## The paper record

```json
{
  "id": 7,
  "title": "Attention Is All You Need",
  "authors": [{"name": "Ashish Vaswani",
               "affiliations": [{"name": "Google (United States)", "country": "US", "type": "company"}]}],
  "year": 2017,
  "venue": "NeurIPS",
  "kind": "inproceedings",
  "abstract": "The dominant sequence transduction models ...",
  "doi": null,
  "arxiv_id": "1706.03762",
  "url": "https://arxiv.org/abs/1706.03762",
  "bibtex": "@inproceedings{vaswani2017attention, ...}",
  "bibtex_key": "vaswani2017attention",
  "bibtex_source": "generated",
  "pages": 15,
  "tags": ["transformers"],
  "notes": "",
  "read_state": "unread",
  "starred": false,
  "status": "complete",
  "missing": [],
  "sources": ["arxiv", "dblp", "openalex"],
  "origin": "laptop",
  "added_at": "2026-11-17T10:00:00+00:00",
  "updated_at": "2026-11-17T10:00:00+00:00",
  "has_pdf": true,
  "pdf_url": "/api/papers/7/pdf",
  "thumb_url": "/api/papers/7/thumb",
  "link": "/#/paper/7"
}
```

`bibtex_source` is `doi` (the publisher), `generated` (built from the fields), `manual`
(set by a user), or `dblp` (on papers added while DBLP's BibTeX could still be fetched). `sources` lists where the metadata came from. Relative URLs are
relative to `http://HOST:8077`.
