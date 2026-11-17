// paperboat frontend: one module, no build step.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const countryName = (() => {
  try { const dn = new Intl.DisplayNames(undefined, { type: "region" }); return (c) => (c ? dn.of(c.toUpperCase()) : c); }
  catch { return (c) => c; }
})();

// ------------------------------------------------------------- grouping

const KINDS = {
  author:      { label: "Authors",      one: "Author",      keys: (p) => p.authors.map((a) => a.name) },
  institution: { label: "Institutions", one: "Institution", keys: (p) => uniq(p.authors.flatMap((a) => (a.affiliations || []).map((f) => f.name))) },
  country:     { label: "Countries",    one: "Country",     keys: (p) => uniq(p.authors.flatMap((a) => (a.affiliations || []).map((f) => f.country).filter(Boolean))).map(countryName) },
  venue:       { label: "Venues",       one: "Venue",       keys: (p) => (p.venue ? [p.venue] : []) },
  year:        { label: "Years",        one: "Year",        keys: (p) => (p.year ? [String(p.year)] : []) },
  tag:         { label: "Tags",         one: "Tag",         keys: (p) => p.tags },
  origin:      { label: "Added from",   one: "Added from",  keys: (p) => (p.origin ? [p.origin] : []) },
};
const uniq = (xs) => [...new Set(xs.filter(Boolean))];

const VIEWS = [
  { id: "all",       label: "All papers",      test: () => true },
  { id: "attention", label: "Needs attention", test: (p) => p.status !== "complete" },
  { id: "starred",   label: "Starred",         test: (p) => p.starred },
  { id: "unread",    label: "Unread",          test: (p) => p.read_state === "unread" },
  { id: "reading",   label: "Reading",         test: (p) => p.read_state === "reading" },
  { id: "read",      label: "Read",            test: (p) => p.read_state === "read" },
];

// ---------------------------------------------------------------- state

const store = {
  get(k, d) { try { const v = localStorage.getItem("pb:" + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("pb:" + k, JSON.stringify(v)); } catch {} },
};

const state = {
  papers: [],
  byId: new Map(),
  hits: null,          // Map id -> snippet when a search is active
  view: "all",
  filters: [],         // [{kind, value}]
  facetKind: store.get("facetKind", "author"),
  groupBy: store.get("groupBy", ""),
  sort: store.get("sort", "added"),
  collapsed: new Set(),
  selected: new Set(),
  current: null,
  editing: false,
};

// ------------------------------------------------------------------ api

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  const ct = res.headers.get("content-type") || "";
  body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = (body && (body.error || body.detail)) || res.statusText;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return { status: res.status, body };
}
const patch = (id, data) => api(`/api/papers/${id}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(data) });

async function loadAll() {
  const { body } = await api("/api/papers");
  setPapers(body.papers);
}
function setPapers(list) {
  state.papers = list;
  state.byId = new Map(list.map((p) => [p.id, p]));
}
function upsert(p) {
  if (state.byId.has(p.id)) state.papers = state.papers.map((x) => (x.id === p.id ? p : x));
  else state.papers.unshift(p);
  state.byId.set(p.id, p);
}

// ------------------------------------------------------------ filtering

function visible() {
  const view = VIEWS.find((v) => v.id === state.view) || VIEWS[0];
  let list = state.papers.filter(view.test);
  for (const f of state.filters) list = list.filter((p) => KINDS[f.kind].keys(p).includes(f.value));
  if (state.hits) return list.filter((p) => state.hits.has(p.id)).sort((a, b) => state.hits.get(a.id).rank - state.hits.get(b.id).rank);
  const first = (p) => (p.authors[0]?.name || "").split(" ").pop().toLowerCase();
  const cmp = {
    added: (a, b) => (b.added_at || "").localeCompare(a.added_at || ""),
    year: (a, b) => (b.year || 0) - (a.year || 0) || (b.added_at || "").localeCompare(a.added_at || ""),
    "year-asc": (a, b) => (a.year || 9999) - (b.year || 9999),
    title: (a, b) => (a.title || "").localeCompare(b.title || ""),
    author: (a, b) => first(a).localeCompare(first(b)),
  }[state.sort];
  return list.sort(cmp);
}

// ------------------------------------------------------------- rendering

function render() {
  renderViews();
  renderFacets();
  renderToolbar();
  renderList();
  renderDetail();
}

function renderViews() {
  $("#views").innerHTML = VIEWS.map((v) => {
    const n = state.papers.filter(v.test).length;
    if (v.id === "attention" && n === 0) return "";
    return `<button class="view ${v.id === state.view ? "active" : ""} ${v.id}" data-view="${v.id}">
      <span>${esc(v.label)}</span><span class="n">${n}</span></button>`;
  }).join("");
}

function facetCounts(kind, papers) {
  const counts = new Map();
  for (const p of papers) for (const k of KINDS[kind].keys(p)) counts.set(k, (counts.get(k) || 0) + 1);
  return counts;
}

function renderFacets() {
  const sel = $("#facet-kind");
  if (!sel.options.length) {
    sel.innerHTML = Object.entries(KINDS).map(([k, v]) => `<option value="${k}">Browse ${v.label.toLowerCase()}</option>`).join("");
  }
  sel.value = state.facetKind;
  const filt = $("#facet-filter").value.trim().toLowerCase();
  const counts = facetCounts(state.facetKind, state.papers);
  let entries = [...counts.entries()];
  entries.sort(state.facetKind === "year" ? (a, b) => b[0].localeCompare(a[0]) : (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  if (filt) entries = entries.filter(([k]) => k.toLowerCase().includes(filt));
  const active = new Set(state.filters.filter((f) => f.kind === state.facetKind).map((f) => f.value));
  $("#facets").innerHTML = entries.length
    ? entries.slice(0, 400).map(([k, n]) => `<li><button class="${active.has(k) ? "on" : ""}" data-facet="${esc(k)}" title="${esc(k)}">
        <span class="label">${esc(k)}</span><span class="n">${n}</span></button></li>`).join("")
    : `<li class="empty">${state.papers.length ? "Nothing here yet." : "Add a paper to get started."}</li>`;
}

function renderToolbar() {
  const shown = visible();
  $("#count").textContent = `${shown.length} paper${shown.length === 1 ? "" : "s"}${state.selected.size ? ` · ${state.selected.size} selected` : ""}`;
  const allSel = shown.length > 0 && shown.every((p) => state.selected.has(p.id));
  $("#select-all").checked = allSel;
  $("#select-all").indeterminate = !allSel && shown.some((p) => state.selected.has(p.id));
  const chips = state.filters.map((f, i) => `<span class="chip"><b>${esc(KINDS[f.kind].one)}</b> ${esc(f.value)}<button data-unfilter="${i}" title="Remove filter">×</button></span>`);
  if (state.hits) chips.unshift(`<span class="chip"><b>Search</b> ${esc($("#q").value)}<button data-unsearch title="Clear search">×</button></span>`);
  $("#chips").innerHTML = chips.join("");
  const gb = $("#group-by");
  if (!gb.options.length) gb.innerHTML = `<option value="">No grouping</option>` + Object.entries(KINDS).map(([k, v]) => `<option value="${k}">${v.one}</option>`).join("");
  gb.value = state.groupBy;
  $("#sort").value = state.sort;
  $("#sort").disabled = !!state.hits;
}

function authorLine(p, max = 6) {
  const names = p.authors.map((a) => a.name);
  if (!names.length) return `<span class="muted">Unknown authors</span>`;
  return esc(names.length > max ? names.slice(0, max - 1).join(", ") + ", … " + names[names.length - 1] : names.join(", "));
}

function snippetHtml(s) {
  return esc(s).replace(/\u0002/g, "<mark>").replace(/\u0003/g, "</mark>");
}

function rowHtml(p) {
  const hit = state.hits?.get(p.id);
  const missing = p.missing.filter((m) => m !== "pdf");
  return `<div class="row ${state.current === p.id ? "current" : ""}" data-id="${p.id}">
    <input type="checkbox" data-select="${p.id}" ${state.selected.has(p.id) ? "checked" : ""} aria-label="Select">
    ${p.thumb_url ? `<img class="thumb" loading="lazy" src="${p.thumb_url}" alt="">` : `<div class="thumb">no pdf</div>`}
    <div class="body">
      <div class="title">${esc(p.title || "Untitled")}</div>
      <div class="authors">${authorLine(p)}</div>
      <div class="meta">
        ${p.venue ? `<span class="venue">${esc(p.venue)}</span>` : ""}
        ${p.year ? `<span>${p.year}</span>` : ""}
        ${p.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}
      </div>
      ${hit?.snippet ? `<div class="snippet">${snippetHtml(hit.snippet)}</div>` : ""}
    </div>
    <div class="badges">
      <div>${p.starred ? `<span class="star" title="Starred">★</span> ` : ""}<span class="state ${p.read_state}">${p.read_state}</span></div>
      ${missing.length ? `<span class="badge-warn" title="Missing: ${esc(missing.join(", "))}">needs ${esc(missing.join(", "))}</span>` : ""}
      ${!p.has_pdf ? `<span class="badge-warn">no PDF</span>` : ""}
      <div class="row-actions">
        ${p.has_pdf ? `<button class="btn small" data-act="read" title="Read (o)">Read</button>` : ""}
        <button class="btn small" data-act="copy-bib" title="Copy BibTeX">BibTeX</button>
      </div>
    </div>
  </div>`;
}

function renderList() {
  const list = visible();
  const el = $("#list");
  if (!list.length) {
    el.innerHTML = state.papers.length
      ? `<div class="empty-list"><h3>No matching papers</h3>Try removing a filter or changing the search.</div>`
      : `<div class="empty-list"><h3>Your library is empty</h3>Drop a PDF anywhere on this page, use “Add paper”, or POST to <code>/api/papers</code> from another machine.</div>`;
    return;
  }
  if (!state.groupBy) { el.innerHTML = list.map(rowHtml).join(""); return; }
  const groups = new Map();
  for (const p of list) {
    const keys = KINDS[state.groupBy].keys(p);
    for (const k of keys.length ? keys : ["(none)"]) {
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(p);
    }
  }
  let entries = [...groups.entries()];
  entries.sort(state.groupBy === "year"
    ? (a, b) => b[0].localeCompare(a[0])
    : (a, b) => (a[0] === "(none)") - (b[0] === "(none)") || b[1].length - a[1].length || a[0].localeCompare(b[0]));
  el.innerHTML = entries.map(([k, ps]) => {
    const closed = state.collapsed.has(k);
    return `<div class="group-head" data-group="${esc(k)}"><span class="caret">${closed ? "▸" : "▾"}</span>${esc(k)} <span class="n">${ps.length}</span></div>
      ${closed ? "" : ps.map(rowHtml).join("")}`;
  }).join("");
}

// --------------------------------------------------------------- detail

function citationText(p) {
  const names = p.authors.map((a) => a.name);
  const au = names.length > 2 ? names.slice(0, -1).join(", ") + ", and " + names.at(-1) : names.join(" and ");
  return `${au}. ${p.year ? p.year + ". " : ""}${p.title}.${p.venue ? " " + p.venue + "." : ""}${p.doi ? " https://doi.org/" + p.doi : p.arxiv_id ? " arXiv:" + p.arxiv_id : ""}`;
}

function renderDetail() {
  const el = $("#detail");
  const p = state.byId.get(state.current);
  if (!p) { el.hidden = true; return; }
  el.hidden = false;
  if (el.dataset.id === String(p.id) && el.dataset.rev === p.updated_at && el.dataset.editing === String(state.editing)) return;
  el.dataset.id = p.id; el.dataset.rev = p.updated_at; el.dataset.editing = state.editing;

  const missing = p.missing.filter((m) => m !== "pdf");
  const affs = uniq(p.authors.flatMap((a) => (a.affiliations || []).map((f) => f.name)));
  const links = [];
  if (p.doi) links.push(`<a href="https://doi.org/${esc(p.doi)}" target="_blank" rel="noopener">doi:${esc(p.doi)}</a>`);
  if (p.arxiv_id) links.push(`<a href="https://arxiv.org/abs/${esc(p.arxiv_id)}" target="_blank" rel="noopener">arXiv:${esc(p.arxiv_id)}</a>`);
  if (p.url) links.push(`<a href="${esc(p.url)}" target="_blank" rel="noopener">source</a>`);

  el.innerHTML = `
    <button class="icon-btn close" data-act="close" title="Close (Esc)">✕</button>
    ${p.thumb_url ? `<img class="detail-thumb" src="${p.thumb_url}" alt="" data-act="read" title="Read">` : ""}
    <h1>${esc(p.title || "Untitled")}</h1>
    <div class="authors-full">${p.authors.map((a) => `<a data-filter-kind="author" data-filter-value="${esc(a.name)}">${esc(a.name)}</a>`).join(", ") || '<span class="muted">Unknown authors</span>'}</div>
    ${affs.length ? `<div class="affils">${affs.map((a) => `<span class="linkish" data-filter-kind="institution" data-filter-value="${esc(a)}">${esc(a)}</span>`).join(" · ")}</div>` : ""}
    <div class="publine">${[
      p.venue && `<span class="venue linkish" data-filter-kind="venue" data-filter-value="${esc(p.venue)}">${esc(p.venue)}</span>`,
      p.year && `<span class="linkish" data-filter-kind="year" data-filter-value="${p.year}">${p.year}</span>`,
      p.pages && `${p.pages} page${p.pages === 1 ? "" : "s"}`,
    ].filter(Boolean).join(" · ")}</div>
    <div style="clear:both"></div>

    ${missing.length || !p.has_pdf ? `<div class="missing-box">
        ${missing.length ? `<strong>Missing:</strong> ${esc(missing.join(", "))}. Fill them in below, or correct the title and look it up again.` : ""}
        ${!p.has_pdf ? `<div>${missing.length ? "<br>" : ""}<strong>No PDF stored.</strong> <label class="linkish">Attach one<input type="file" accept="application/pdf,.pdf" data-attach hidden></label></div>` : ""}
      </div>` : ""}

    <div class="actions">
      ${p.has_pdf ? `<button class="btn primary" data-act="read">Read</button>
      <a class="btn" href="${p.pdf_url}" target="_blank" rel="noopener">Open PDF ↗</a>
      <a class="btn" href="${p.pdf_url}?download=1">Download</a>` : ""}
      <button class="btn" data-act="share">Share</button>
      <button class="btn" data-act="star">${p.starred ? "★ Starred" : "☆ Star"}</button>
    </div>
    <div class="seg" role="group" aria-label="Reading state">
      ${["unread", "reading", "read"].map((s) => `<button data-state="${s}" class="${p.read_state === s ? "on" : ""}">${s}</button>`).join("")}
    </div>

    <section>
      <h3>Tags</h3>
      <div class="tags-edit">
        ${p.tags.map((t) => `<span class="tag">${esc(t)}<button data-untag="${esc(t)}" title="Remove">×</button></span>`).join("")}
        <input data-addtag placeholder="add tag…" list="tag-options">
        <datalist id="tag-options">${uniq(state.papers.flatMap((x) => x.tags)).map((t) => `<option value="${esc(t)}">`).join("")}</datalist>
      </div>
    </section>

    <section>
      <h3>Notes <span class="saved" data-saved></span></h3>
      <textarea class="notes" data-notes placeholder="Your notes. Saved as you type; included in search.">${esc(p.notes)}</textarea>
    </section>

    ${state.editing || missing.length ? editForm(p, missing) : ""}

    ${p.abstract ? `<section><h3>Abstract</h3><p class="abstract collapsed" data-act="expand">${esc(p.abstract)}</p></section>` : ""}

    <section>
      <h3>Cite <span class="grow"></span>
        <button class="btn small" data-act="copy-bib">Copy BibTeX</button>
        <button class="btn small" data-act="copy-text">Copy text</button>
        <a class="btn small" href="/api/papers/${p.id}/bibtex?download=1">.bib</a>
      </h3>
      <pre class="bib">${esc(p.bibtex)}</pre>
      <div class="muted" style="margin-top:6px">BibTeX from ${esc({ dblp: "DBLP", doi: "the publisher (doi.org)", manual: "you", generated: "paperboat's own metadata" }[p.bibtex_source] || p.bibtex_source)} · key <code>${esc(p.bibtex_key)}</code></div>
    </section>

    <section>
      <h3>Details <span class="grow"></span>${state.editing || missing.length ? "" : `<button class="btn small" data-act="edit">Edit metadata</button>`}</h3>
      <dl class="kv">
        ${links.length ? `<dt>Links</dt><dd>${links.join(" · ")}</dd>` : ""}
        <dt>Added</dt><dd>${esc(new Date(p.added_at).toLocaleString())}${p.origin ? ` from <span class="linkish" data-filter-kind="origin" data-filter-value="${esc(p.origin)}">${esc(p.origin)}</span>` : ""}</dd>
        <dt>Metadata</dt><dd>${esc(p.sources.join(", ") || "manual")}</dd>
      </dl>
      <div class="actions">
        <button class="btn small" data-act="resolve" title="Query arXiv, Crossref, OpenAlex, DBLP and Semantic Scholar again">Look up again</button>
        <button class="btn small danger" data-act="delete">Delete</button>
      </div>
    </section>`;
}

function editForm(p, missing) {
  const cls = (f) => (missing.includes(f) ? "missing" : "");
  return `<section>
    <h3>${missing.length ? "Complete metadata" : "Edit metadata"}</h3>
    <form class="edit-form" data-edit>
      <label class="${cls("title")}"><span>Title</span><input name="title" value="${esc(p.title)}"></label>
      <label class="${cls("authors")}"><span>Authors (one per line)</span><textarea name="authors" rows="${Math.min(8, Math.max(2, p.authors.length))}">${esc(p.authors.map((a) => a.name).join("\n"))}</textarea></label>
      <div class="two">
        <label class="${cls("venue")}"><span>Venue</span><input name="venue" value="${esc(p.venue)}" placeholder="e.g. PLDI, NeurIPS, arXiv"></label>
        <label class="${cls("year")}"><span>Year</span><input name="year" inputmode="numeric" value="${esc(p.year)}"></label>
      </div>
      <div class="two">
        <label><span>DOI</span><input name="doi" value="${esc(p.doi)}"></label>
        <label><span>arXiv id</span><input name="arxiv_id" value="${esc(p.arxiv_id)}"></label>
      </div>
      <div class="two">
        <label><span>Type</span><select name="kind">
          <option value="" ${p.kind ? "" : "selected"}>(automatic)</option>
          ${["inproceedings", "article", "misc"].map((k) => `<option ${p.kind === k ? "selected" : ""}>${k}</option>`).join("")}
        </select></label>
        <label><span>URL</span><input name="url" value="${esc(p.url)}"></label>
      </div>
      <label class="${cls("abstract")}"><span>Abstract</span><textarea name="abstract" rows="5">${esc(p.abstract)}</textarea></label>
      <label><span>BibTeX (leave as is to keep it in sync with the fields above; clear to regenerate)</span><textarea class="mono" name="bibtex" rows="6">${esc(p.bibtex)}</textarea></label>
      <label><span>Citation key (stays fixed once set; clear to derive it again from the fields above)</span><input class="mono" name="bibtex_key" value="${esc(p.bibtex_key)}"></label>
      <div class="form-actions">
        ${state.editing ? `<button type="button" class="btn" data-act="cancel-edit">Cancel</button>` : ""}
        <button type="button" class="btn" data-act="resolve">Look up again</button>
        <button class="btn primary">Save</button>
      </div>
    </form>
  </section>`;
}

// --------------------------------------------------------------- actions

function toast(title, msg = "", kind = "", action = null, ms = 5000) {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.innerHTML = `<div class="t">${esc(title)}</div>${msg ? `<div>${esc(msg)}</div>` : ""}`;
  if (action) {
    const b = document.createElement("button");
    b.className = "btn small"; b.textContent = action.label;
    b.onclick = () => { action.run(); t.remove(); };
    t.append(b);
  }
  $("#toasts").append(t);
  if (ms) setTimeout(() => t.remove(), ms);
  return t;
}

async function copy(text, what = "Copied") {
  try { await navigator.clipboard.writeText(text); toast(what); }
  catch {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove(); toast(what);
  }
}

function openPaper(id, { push = true } = {}) {
  state.current = id;
  state.editing = false;
  if (push && location.hash !== `#/paper/${id}`) history.pushState(null, "", `#/paper/${id}`);
  renderList(); renderDetail();
  const row = $(`.row[data-id="${id}"]`);
  row?.scrollIntoView({ block: "nearest" });
}

function closePaper() {
  state.current = null;
  if (location.hash) history.pushState(null, "", location.pathname);
  renderList(); renderDetail();
}

let notesTimer = null;
function saveNotesSoon(id, text, indicator) {
  clearTimeout(notesTimer);
  if (indicator) indicator.textContent = "…";
  notesTimer = setTimeout(async () => {
    try {
      const { body } = await patch(id, { notes: text });
      const p = state.byId.get(id);
      if (p) { p.notes = body.paper.notes; p.updated_at = body.paper.updated_at; }
      $$("[data-saved]").concat($("#reader-saved")).forEach((s) => s && (s.textContent = "saved"));
      if ($("#detail").dataset.id === String(id)) $("#detail").dataset.rev = body.paper.updated_at;
    } catch (e) { toast("Couldn't save notes", e.message, "err"); }
  }, 600);
}

async function update(id, data) {
  try {
    const { body } = await patch(id, data);
    upsert(body.paper);
    render();
    return body;
  } catch (e) { toast("Update failed", e.message, "err"); }
}

function openReader(id) {
  const p = state.byId.get(id);
  if (!p?.has_pdf) return;
  state.current = id;
  $("#reader-title").textContent = p.title || "Untitled";
  $("#reader-frame").src = p.pdf_url;
  $("#reader-newtab").href = p.pdf_url;
  $("#reader-text").value = p.notes || "";
  $("#reader-saved").textContent = "";
  $("#reader").hidden = false;
  if (location.hash !== `#/read/${id}`) history.pushState(null, "", `#/read/${id}`);
  if (p.read_state === "unread") update(id, { read_state: "reading" });
}
function closeReader() {
  $("#reader").hidden = true;
  $("#reader-frame").src = "about:blank";
  const id = state.current;
  if (id) { history.pushState(null, "", `#/paper/${id}`); openPaper(id, { push: false }); }
  // Notes may have changed in the reader.
  const el = $("#detail"); el.dataset.rev = "";
  renderDetail();
}

async function share(p) {
  const link = `${location.origin}/#/paper/${p.id}`;
  if (navigator.share) {
    try { await navigator.share({ title: p.title, text: citationText(p), url: link }); return; } catch (e) { if (e.name === "AbortError") return; }
  }
  const t = toast("Share", "", "", null, 10000);
  t.innerHTML += `<div class="actions" style="margin:6px 0 0">
    <button class="btn small" data-c="link">Copy page link</button>
    ${p.has_pdf ? `<button class="btn small" data-c="pdf">Copy PDF link</button>` : ""}
    <button class="btn small" data-c="cite">Copy citation</button>
    <a class="btn small" href="mailto:?subject=${encodeURIComponent(p.title)}&body=${encodeURIComponent(citationText(p) + "\n\n" + link)}">Email</a></div>`;
  t.addEventListener("click", (e) => {
    const c = e.target.dataset.c;
    if (c === "link") copy(link, "Link copied");
    if (c === "pdf") copy(location.origin + p.pdf_url, "PDF link copied");
    if (c === "cite") copy(citationText(p), "Citation copied");
    if (c) t.remove();
  });
}

async function exportBib(which) {
  const ids = which.includes("selected") ? [...state.selected] : which.includes("shown") ? visible().map((p) => p.id) : null;
  if (ids && !ids.length) { toast("Nothing to export", which.includes("selected") ? "Select some papers first." : ""); return; }
  const q = ids ? `ids=${ids.join(",")}` : "";
  if (which.startsWith("copy")) {
    const { body } = await api(`/api/export.bib?${q}`);
    copy(body, `Copied ${ids.length} BibTeX entr${ids.length === 1 ? "y" : "ies"}`);
  } else {
    location.href = `/api/export.bib?${q}${q ? "&" : ""}download=1`;
  }
}

// ---------------------------------------------------------------- adding

// Returns what happened: "added", "incomplete", "duplicate" or "error". In a batch only
// errors get their own toast; the batch shows progress and a summary instead.
async function submit({ file = null, url = null }, { batch = false } = {}) {
  const label = file ? file.name : url;
  const pending = batch ? null : toast("Adding…", label, "", null, 0);
  try {
    let res;
    if (file) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("origin", "web");
      res = await fetch("/api/papers", { method: "POST", body: fd });
    } else {
      res = await fetch("/api/papers", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ url, origin: "web" }) });
    }
    const body = await res.json();
    pending?.remove();
    if (body.status === "error" || !body.paper) {
      toast("Couldn't add " + (batch ? label : "paper"), body.error || body.detail || res.statusText, "err", null, 9000);
      return "error";
    }
    upsert(body.paper);
    render();
    const kind = body.duplicate ? "duplicate" : body.status === "incomplete" ? "incomplete" : "added";
    if (batch) return kind;
    const open = { label: "Open", run: () => openPaper(body.paper.id) };
    if (body.duplicate) {
      const how = { pdf: "same PDF", doi: "same DOI", arxiv_id: "same arXiv id", title: "same title and first author" }[body.existing?.matched_on];
      const note = [how, body.existing?.pdf_replaced && "PDF replaced with this one"].filter(Boolean).join("; ");
      toast("Already in your library", body.paper.title + (note ? ` (${note})` : ""), "", open, 7000);
    }
    else if (body.status === "incomplete") {
      toast("Added, but metadata is incomplete", `Missing: ${body.missing.join(", ")}${body.warning ? ` (${body.warning})` : ""}`, "warn", open, 9000);
      openPaper(body.paper.id);
    } else toast("Added", body.paper.title, "", open);
    return kind;
  } catch (e) {
    pending?.remove();
    toast("Couldn't add " + (batch ? label : "paper"), e.message, "err", null, 9000);
    return "error";
  }
}

// Each submission does a minute of lookups against rate-limited services, so a big
// drop goes through a small queue instead of all at once.
const MAX_UPLOADS = 3;
const queue = [];
let active = 0;
let batch = null; // { total, done, counts, toast } while a multi-paper add is running

function addMany(jobs) {
  if (!jobs.length) return;
  const many = jobs.length > 1 || batch !== null;
  if (many && !batch) {
    batch = { total: 0, done: 0, counts: { added: 0, incomplete: 0, duplicate: 0, error: 0 }, toast: toast("Adding papers…", "", "", null, 0) };
  }
  if (many) batch.total += jobs.length;
  for (const job of jobs) queue.push({ job, many });
  showProgress();
  pump();
}

function pump() {
  while (active < MAX_UPLOADS && queue.length) {
    const { job, many } = queue.shift();
    active++;
    submit(job, { batch: many }).then((kind) => {
      active--;
      if (many) { batch.counts[kind]++; batch.done++; showProgress(); }
      pump();
    });
  }
}

function showProgress() {
  if (!batch) return;
  const { total, done, counts: c } = batch;
  if (done < total) {
    batch.toast.innerHTML = `<div class="t">Adding papers… ${done} of ${total}</div><div>${esc(summary(c))}</div>`;
    return;
  }
  batch.toast.remove();
  batch = null;
  const kind = c.error ? "err" : c.incomplete ? "warn" : "";
  toast(`Added ${c.added + c.incomplete} of ${total} papers`, summary(c) + (c.incomplete ? ". Incomplete ones are under Needs attention." : ""), kind, null, 12000);
}

function summary(c) {
  return [c.incomplete && `${c.incomplete} need metadata`, c.duplicate && `${c.duplicate} already in library`,
          c.error && `${c.error} failed`].filter(Boolean).join(" · ");
}

const isPdf = (f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf");

function addFiles(files) {
  files = [...files];
  const pdfs = files.filter(isPdf);
  if (!pdfs.length) { toast("Only PDFs can be added", files.length ? `Got ${files.map((f) => f.name).join(", ")}` : "", "warn"); return; }
  if (pdfs.length < files.length) toast(`Skipped ${files.length - pdfs.length} files that aren't PDFs`, "", "warn");
  addMany(pdfs.map((file) => ({ file })));
}

// A drop can carry files, folders (walked for PDFs), or links: one URL per line in
// text/uri-list. Everything on the DataTransfer must be read before the first await.
async function handleDrop(dt) {
  const text = dt.getData("text/uri-list") || dt.getData("text/plain") || "";
  const plain = [...dt.files];
  const entries = [...(dt.items || [])].filter((i) => i.kind === "file").map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
  const files = entries.some((e) => e.isDirectory) ? await walkEntries(entries) : plain;
  if (files.length) return addFiles(files);
  const links = text.split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith("#"));
  const web = links.filter((l) => /^https?:\/\//.test(l));
  if (web.length) return addMany(web.map((url) => ({ url })));
  if (links.some((l) => l.startsWith("file:"))) {
    toast("Your file manager sent file paths, not files", "Browsers can't read a dropped path. Use Add paper → choose files (you can select many), or drag from another file manager.", "warn", null, 12000);
  } else if (entries.length || links.length) {
    toast("Nothing to add in that drop", "Drop PDFs, a folder of PDFs, or links to papers.", "warn");
  }
}

async function walkEntries(entries) {
  const out = [];
  const walk = async (entry) => {
    if (entry.isFile) {
      if (entry.name.toLowerCase().endsWith(".pdf")) out.push(await new Promise((ok, err) => entry.file(ok, err)));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) { // readEntries returns at most ~100 entries per call
        const chunk = await new Promise((ok, err) => reader.readEntries(ok, err));
        if (!chunk.length) break;
        for (const e of chunk) await walk(e);
      }
    }
  };
  for (const e of entries) await walk(e);
  return out;
}

// ---------------------------------------------------------------- search

let searchTimer = null;
let searchSeq = 0;
function runSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const q = $("#q").value.trim();
    if (!q) { state.hits = null; render(); return; }
    const seq = ++searchSeq;
    try {
      const { body } = await api(`/api/papers?q=${encodeURIComponent(q)}`);
      if (seq !== searchSeq) return;
      state.hits = new Map(body.papers.map((p, i) => [p.id, { snippet: p.snippet, rank: i }]));
      render();
    } catch (e) { toast("Search failed", e.message, "err"); }
  }, 180);
}

// ---------------------------------------------------------------- events

function wire() {
  $("#q").addEventListener("input", runSearch);
  $("#views").addEventListener("click", (e) => {
    const b = e.target.closest("[data-view]"); if (!b) return;
    state.view = b.dataset.view; render(); $("#side").classList.remove("open");
  });
  $("#facet-kind").addEventListener("change", (e) => { state.facetKind = e.target.value; store.set("facetKind", state.facetKind); $("#facet-filter").value = ""; renderFacets(); });
  $("#facet-filter").addEventListener("input", renderFacets);
  $("#facets").addEventListener("click", (e) => {
    const b = e.target.closest("[data-facet]"); if (!b) return;
    toggleFilter(state.facetKind, b.dataset.facet);
    $("#side").classList.remove("open");
  });
  $("#chips").addEventListener("click", (e) => {
    if (e.target.dataset.unfilter != null) { state.filters.splice(+e.target.dataset.unfilter, 1); render(); }
    if (e.target.hasAttribute("data-unsearch")) { $("#q").value = ""; state.hits = null; render(); }
  });
  $("#group-by").addEventListener("change", (e) => { state.groupBy = e.target.value; state.collapsed.clear(); store.set("groupBy", state.groupBy); renderList(); });
  $("#sort").addEventListener("change", (e) => { state.sort = e.target.value; store.set("sort", state.sort); renderList(); });
  $("#select-all").addEventListener("change", (e) => {
    for (const p of visible()) e.target.checked ? state.selected.add(p.id) : state.selected.delete(p.id);
    renderToolbar(); renderList();
  });
  $("#export-btn").addEventListener("click", (e) => { e.stopPropagation(); $("#export-menu").hidden = !$("#export-menu").hidden; });
  $("#export-menu").addEventListener("click", (e) => { const w = e.target.dataset.export; if (w) { $("#export-menu").hidden = true; exportBib(w); } });
  document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) $("#export-menu").hidden = true; });
  $("#toggle-side").addEventListener("click", () => $("#side").classList.toggle("open"));

  $("#list").addEventListener("click", (e) => {
    const g = e.target.closest("[data-group]");
    if (g) { const k = g.dataset.group; state.collapsed.has(k) ? state.collapsed.delete(k) : state.collapsed.add(k); renderList(); return; }
    const row = e.target.closest(".row"); if (!row) return;
    const id = +row.dataset.id;
    if (e.target.matches("[data-select]")) {
      e.target.checked ? state.selected.add(id) : state.selected.delete(id);
      renderToolbar(); return;
    }
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "read") return openReader(id);
    if (act === "copy-bib") return copy(state.byId.get(id).bibtex, "BibTeX copied");
    openPaper(id);
  });
  $("#list").addEventListener("dblclick", (e) => { const row = e.target.closest(".row"); if (row) openReader(+row.dataset.id); });

  const detail = $("#detail");
  detail.addEventListener("click", async (e) => {
    const p = state.byId.get(state.current); if (!p) return;
    const f = e.target.closest("[data-filter-kind]");
    if (f) return toggleFilter(f.dataset.filterKind, f.dataset.filterValue, true);
    const st = e.target.closest("[data-state]");
    if (st) return update(p.id, { read_state: st.dataset.state });
    if (e.target.dataset.untag) return update(p.id, { tags: p.tags.filter((t) => t !== e.target.dataset.untag) });
    const act = e.target.closest("[data-act]")?.dataset.act;
    switch (act) {
      case "close": return closePaper();
      case "read": return openReader(p.id);
      case "star": return update(p.id, { starred: !p.starred });
      case "share": return share(p);
      case "copy-bib": return copy(p.bibtex, "BibTeX copied");
      case "copy-text": return copy(citationText(p), "Citation copied");
      case "expand": return e.target.classList.toggle("collapsed");
      case "edit": state.editing = true; return renderDetail();
      case "cancel-edit": state.editing = false; return renderDetail();
      case "resolve": {
        const t = toast("Looking up…", p.title || "", "", null, 0);
        try {
          const form = $("[data-edit]", detail);
          if (form) await saveForm(form, p, { quiet: true });   // look up using what was just typed
          const { body } = await api(`/api/papers/${p.id}/resolve`, { method: "POST" });
          upsert(body.paper); state.editing = false; render();
          toast(body.status === "complete" ? "Metadata complete" : "Still missing: " + body.missing.join(", "), "", body.status === "complete" ? "" : "warn");
        } catch (err) { toast("Lookup failed", err.message, "err"); }
        finally { t.remove(); }
        return;
      }
      case "delete":
        if (!confirm(`Delete “${p.title || "this paper"}” and its PDF?`)) return;
        await api(`/api/papers/${p.id}`, { method: "DELETE" });
        state.papers = state.papers.filter((x) => x.id !== p.id); state.byId.delete(p.id); state.selected.delete(p.id);
        closePaper(); render(); toast("Deleted");
    }
  });
  detail.addEventListener("input", (e) => {
    if (e.target.matches("[data-notes]")) saveNotesSoon(state.current, e.target.value, $("[data-saved]", detail));
  });
  detail.addEventListener("keydown", (e) => {
    if (e.target.matches("[data-addtag]") && (e.key === "Enter" || e.key === ",")) {
      e.preventDefault();
      const p = state.byId.get(state.current);
      const t = e.target.value.trim();
      if (t && !p.tags.includes(t)) update(p.id, { tags: [...p.tags, t] }).then(() => $("[data-addtag]", detail)?.focus());
      e.target.value = "";
    }
  });
  detail.addEventListener("change", async (e) => {
    if (e.target.matches("[data-attach]") && e.target.files[0]) {
      const fd = new FormData(); fd.append("file", e.target.files[0]);
      try {
        const { body } = await api(`/api/papers/${state.current}/pdf`, { method: "POST", body: fd });
        upsert(body.paper); render(); toast("PDF attached");
      } catch (err) { toast("Couldn't attach PDF", err.message, "err"); }
    }
  });
  detail.addEventListener("submit", async (e) => {
    e.preventDefault();
    const p = state.byId.get(state.current);
    await saveForm(e.target, p);
  });

  // reader
  $("#reader-close").addEventListener("click", closeReader);
  $("#reader-text").addEventListener("input", (e) => saveNotesSoon(state.current, e.target.value, $("#reader-saved")));
  $("#reader-notes-toggle").addEventListener("click", () => {
    const n = $("#reader-notes");
    if (matchMedia("(max-width: 760px)").matches) n.classList.toggle("open"); else n.hidden = !n.hidden;
  });

  // add dialog
  const dlg = $("#add-dialog");
  $("#add-btn").addEventListener("click", () => { $("#add-url").value = ""; dlg.showModal(); $("#add-url").focus(); });
  $("#add-form").addEventListener("submit", () => {
    const urls = $("#add-url").value.split(/\s+/).filter((u) => /^https?:\/\//.test(u));
    if (dlg.returnValue === "ok") addMany(urls.map((url) => ({ url })));
  });
  $("#add-file").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; dlg.close(); });
  const dz = $("#drop-zone");
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); e.stopPropagation(); dz.classList.remove("over"); dlg.close(); handleDrop(e.dataTransfer); });

  // drop anywhere; also accept dragged links
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].some((t) => t === "Files" || t === "text/uri-list");
  window.addEventListener("dragenter", (e) => { if (hasFiles(e) && !dlg.open) { depth++; $("#dropcover").hidden = false; } });
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; $("#dropcover").hidden = true; } });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    e.preventDefault(); depth = 0; $("#dropcover").hidden = true;
    handleDrop(e.dataTransfer);
  });
  // paste a link anywhere (outside inputs) to add it
  window.addEventListener("paste", (e) => {
    if (e.target.closest("input, textarea")) return;
    const urls = e.clipboardData.getData("text").trim().split(/\s+/);
    if (urls[0] && urls.every((u) => /^https?:\/\/\S+$/.test(u))) addMany(urls.map((url) => ({ url })));
  });

  // keyboard
  window.addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) {
      if (e.key === "Escape" && e.target.id === "q") { e.target.value = ""; state.hits = null; e.target.blur(); render(); }
      return;
    }
    if ($("#add-dialog").open) return;
    if (!$("#reader").hidden) { if (e.key === "Escape") closeReader(); return; }
    const list = visible();
    const i = list.findIndex((p) => p.id === state.current);
    const keys = {
      "/": () => $("#q").focus(),
      j: () => list.length && openPaper(list[Math.min(list.length - 1, i + 1)].id),
      k: () => list.length && openPaper(list[Math.max(0, i - 1)].id),
      o: () => state.current && openReader(state.current),
      Enter: () => state.current && openReader(state.current),
      s: () => state.current && update(state.current, { starred: !state.byId.get(state.current).starred }),
      e: () => { if (state.current) { state.editing = true; renderDetail(); } },
      b: () => state.current && copy(state.byId.get(state.current).bibtex, "BibTeX copied"),
      a: () => $("#add-btn").click(),
      Escape: () => closePaper(),
    };
    if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
  });

  window.addEventListener("popstate", route);
}

async function saveForm(form, p, { quiet = false } = {}) {
  const fd = Object.fromEntries(new FormData(form));
  const current = {
    title: p.title, authors: p.authors.map((a) => a.name).join("\n"), venue: p.venue, year: p.year,
    doi: p.doi, arxiv_id: p.arxiv_id, kind: p.kind, url: p.url, abstract: p.abstract, bibtex: p.bibtex,
    bibtex_key: p.bibtex_key,
  };
  // Send only what changed, so an untouched form never discards fetched BibTeX or affiliations.
  const data = {};
  for (const [k, v] of Object.entries(current)) if ((fd[k] ?? "").trim() !== String(v ?? "").trim()) data[k] = fd[k];
  if (!Object.keys(data).length) { state.editing = false; renderDetail(); return; }
  const body = await update(p.id, data);
  if (!body) return;
  state.editing = false;
  renderDetail();
  if (!quiet) toast(body.status === "complete" ? "Saved" : "Saved — still missing: " + body.missing.join(", "), "", body.status === "complete" ? "" : "warn");
}

function toggleFilter(kind, value, only = false) {
  const i = state.filters.findIndex((f) => f.kind === kind && f.value === value);
  if (i >= 0 && !only) state.filters.splice(i, 1);
  else if (i < 0) state.filters.push({ kind, value });
  state.facetKind = kind; store.set("facetKind", kind);
  render();
}

function route() {
  const m = location.hash.match(/^#\/(paper|read)\/(\d+)/);
  if (!m) { $("#reader").hidden = true; state.current = null; render(); return; }
  const id = +m[2];
  if (!state.byId.has(id)) return;
  if (m[1] === "read") { openPaper(id, { push: false }); openReader(id); }
  else { $("#reader").hidden = true; openPaper(id, { push: false }); }
}

// Pick up papers added from other machines without a reload.
async function refresh() {
  if (document.hidden) return;
  try {
    const { body } = await api("/api/papers");
    const sig = (ps) => ps.map((p) => p.id + p.updated_at).join();
    if (sig(body.papers) !== sig(state.papers)) {
      const focused = document.activeElement;
      const typing = focused && $("#detail").contains(focused) && focused.matches("textarea, input");
      setPapers(body.papers);
      if (typing) { renderViews(); renderFacets(); renderToolbar(); renderList(); }
      else render();
    }
  } catch {}
}

// Bookmarkable views: /?q=attention&group=venue
const params = new URLSearchParams(location.search);
if (params.get("group") in KINDS) state.groupBy = params.get("group");
if (params.get("q")) { $("#q").value = params.get("q"); runSearch(); }

wire();
loadAll().then(() => { render(); route(); }).catch((e) => toast("Couldn't load library", e.message, "err", null, 0));
setInterval(refresh, 15000);
document.addEventListener("visibilitychange", refresh);
