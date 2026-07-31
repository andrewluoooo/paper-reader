"""
A very small local web app around the existing convert+restyle pipeline:
drag a LaTeX source file (.tex / .tar.gz / .tgz / .zip) onto the home
page, it gets parsed the same way the CLI does, and the result is added
to a local library you can search and open (each paper opens as its own
self-contained reader page, in a new tab).

No framework -- just the standard library's http.server, since the only
job here is "save an upload, run a function, list some JSON, serve some
files." Runs entirely on localhost.
"""

from __future__ import annotations

import html
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .html_convert import HtmlConvertError
from .html_convert import convert as convert_html
from .latex_convert import LatexConvertError, convert as convert_latex
from .restyle import restyle

LIBRARY_DIR = Path.home() / ".paper_reader_library"
INDEX_PATH = LIBRARY_DIR / "index.json"
# Pre-restyle LaTeXML output (HTML + figure files) for each paper, kept
# around permanently (not in a TemporaryDirectory) so that reader/CSS/JS
# changes in restyle.py can be re-applied to already-uploaded papers
# without re-running the slow LaTeX->HTML conversion.
RAW_DIR = LIBRARY_DIR / "raw"

ALLOWED_UPLOAD_SUFFIXES = (".tex", ".zip", ".tar.gz", ".tgz", ".tar", ".html", ".htm")
HTML_SOURCE_SUFFIXES = (".html", ".htm")
MAX_UPLOAD_BYTES = 60 * 1024 * 1024  # 60MB is generous for a LaTeX source tree


def _load_index() -> list[dict]:
    if INDEX_PATH.is_file():
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    return []


def _save_index(items: list[dict]) -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")


def _process_upload(filename: str, data: bytes) -> dict:
    """Run the same convert()+restyle() pipeline the CLI uses, save the
    result into the library, and return its index entry."""
    paper_id = uuid.uuid4().hex[:12]
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    raw_workdir = RAW_DIR / paper_id
    raw_workdir.mkdir(parents=True, exist_ok=True)

    is_html_source = filename.lower().endswith(HTML_SOURCE_SUFFIXES)
    with tempfile.TemporaryDirectory(prefix="paper_reader_upload_") as tmp:
        tmp_path = Path(tmp) / os.path.basename(filename)
        tmp_path.write_bytes(data)
        if is_html_source:
            raw_html_path = convert_html(str(tmp_path), str(raw_workdir))
        else:
            raw_html_path = convert_latex(str(tmp_path), str(raw_workdir))

    html_out, metadata = restyle(raw_html_path, source_name=filename, back_link="/")
    (LIBRARY_DIR / f"{paper_id}.html").write_text(html_out, encoding="utf-8")

    entry = {
        "id": paper_id,
        "title": metadata.get("title") or filename,
        "authors": [a["name"] for a in metadata.get("authors", [])],
        "venue": metadata.get("venue", ""),
        "sourceFilename": filename,
        "addedAt": time.time(),
        "lastOpenedAt": None,
        "rawHtmlPath": raw_html_path,
        "tags": [],
    }
    items = _load_index()
    items.insert(0, entry)
    _save_index(items)
    return entry


def _rebuild_paper(entry: dict) -> bool:
    """Re-run restyle() on a paper's stored raw (pre-restyle) HTML, so it
    picks up the current reader CSS/JS. Returns False (and leaves the
    entry untouched) if there's no raw HTML on disk to rebuild from --
    e.g. papers uploaded before rawHtmlPath started being recorded."""
    raw_html_path = entry.get("rawHtmlPath")
    if not raw_html_path or not Path(raw_html_path).is_file():
        return False
    html_out, metadata = restyle(raw_html_path, source_name=entry.get("sourceFilename", ""), back_link="/")
    (LIBRARY_DIR / f"{entry['id']}.html").write_text(html_out, encoding="utf-8")
    entry["title"] = metadata.get("title") or entry["title"]
    entry["authors"] = [a["name"] for a in metadata.get("authors", [])]
    entry["venue"] = metadata.get("venue", "")
    return True


def _delete_paper(paper_id: str) -> bool:
    """Remove a paper from the index and delete its stored HTML + raw
    source. Returns False if no entry with that id exists."""
    items = _load_index()
    remaining = [e for e in items if e["id"] != paper_id]
    if len(remaining) == len(items):
        return False
    _save_index(remaining)
    html_path = LIBRARY_DIR / f"{paper_id}.html"
    if html_path.is_file():
        html_path.unlink()
    raw_path = RAW_DIR / paper_id
    if raw_path.is_dir():
        shutil.rmtree(raw_path)
    return True


def _set_paper_tags(paper_id: str, tags: list) -> dict | None:
    """Replace a paper's tag list. Tags are normalized (trimmed, empty
    ones dropped, de-duplicated case-insensitively but keeping first
    casing seen) and sorted. Returns the updated entry, or None if no
    paper with that id exists."""
    items = _load_index()
    entry = next((e for e in items if e["id"] == paper_id), None)
    if entry is None:
        return None
    seen = {}
    for t in tags:
        if not isinstance(t, str):
            continue
        t = t.strip()
        if not t:
            continue
        seen.setdefault(t.lower(), t)
    entry["tags"] = sorted(seen.values(), key=str.lower)
    _save_index(items)
    return entry


def _touch_opened(paper_id: str) -> None:
    """Record that a paper's reader page was just served, for the
    "most recently opened" sort option. Best-effort: silently does
    nothing if the id isn't in the index."""
    items = _load_index()
    entry = next((e for e in items if e["id"] == paper_id), None)
    if entry is None:
        return
    entry["lastOpenedAt"] = time.time()
    _save_index(items)


def rebuild_library(quiet: bool = False) -> tuple[int, int]:
    """Re-apply the current restyle() output to every paper in the
    library. Called automatically on server startup so reader changes
    (CSS, highlighting, etc.) show up in already-uploaded papers without
    needing to re-upload them."""
    items = _load_index()
    rebuilt = sum(1 for entry in items if _rebuild_paper(entry))
    skipped = len(items) - rebuilt
    if items:
        _save_index(items)
    if not quiet:
        msg = f"[library] rebuilt {rebuilt} paper(s) with the current reader styling"
        if skipped:
            msg += f"; skipped {skipped} (no stored raw source -- re-upload to enable rebuilding)"
        print(msg)
    return rebuilt, skipped


HOME_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<script>
(function () {
  try {
    var s = JSON.parse(localStorage.getItem("paper_reader_settings") || "{}");
    if (s.theme === "light" || s.theme === "dark") document.documentElement.setAttribute("data-theme", s.theme);
  } catch (e) {}
})();
</script>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Andrew's Paper Library</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8;
  --accent: #1a56db; --card-bg: #fbfaf8; --error: #b3261e;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #161513; --fg: #ece9e2; --muted: #a9a49a; --rule: #33322d; --accent: #7fa7ff; --card-bg: #1b1a18; --error: #ff6b60; }
}
:root[data-theme="light"] {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db; --card-bg: #fbfaf8; --error: #b3261e;
}
:root[data-theme="dark"] {
  --bg: #161513; --fg: #ece9e2; --muted: #a9a49a; --rule: #33322d; --accent: #7fa7ff; --card-bg: #1b1a18; --error: #ff6b60;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, "Times New Roman", serif;
}
.wrap { max-width: 820px; margin: 0 auto; padding: 7vh 6vw 12vh; }
.header-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 1em; }
h1 { font-size: 2em; margin: 0 0 0.2em; }
.sub { color: var(--muted); margin: 0 0 2.2em; font-family: -apple-system, "Segoe UI", sans-serif; font-size: 0.95em; }
.icon-btn {
  flex-shrink: 0; width: 36px; height: 36px; border-radius: 999px; border: 1px solid var(--rule);
  background: var(--card-bg); color: var(--fg); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.icon-btn:hover { background: var(--rule); }
.icon-btn svg { display: block; }
.dropzone {
  border: 2px dashed var(--rule); border-radius: 12px; padding: 3em 2em;
  text-align: center; color: var(--muted); font-family: -apple-system, "Segoe UI", sans-serif;
  cursor: pointer; margin-bottom: 1.2em; transition: border-color 0.15s, background 0.15s;
}
.dropzone.dragover { border-color: var(--accent); background: var(--card-bg); }
.dropzone strong { color: var(--fg); }
.dropzone .hint { font-size: 0.82em; margin-top: 0.4em; }
input[type=file] { display: none; }
.status { font-family: -apple-system, "Segoe UI", sans-serif; font-size: 0.88em; margin-bottom: 1.6em; min-height: 1.3em; }
.status.error { color: var(--error); }
.status.loading { color: var(--muted); }
.spinner {
  display: inline-block; width: 0.9em; height: 0.9em; border: 2px solid var(--rule);
  border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite;
  margin-right: 0.5em; vertical-align: -0.15em;
}
@keyframes spin { to { transform: rotate(360deg); } }
.search-row { display: flex; gap: 0.6em; margin-bottom: 1em; }
.search-row input {
  flex: 1; min-width: 0; padding: 0.7em 1em; border-radius: 8px; border: 1px solid var(--rule);
  background: var(--card-bg); color: var(--fg); font-family: -apple-system, "Segoe UI", sans-serif;
  font-size: 0.95em;
}
.sort-select {
  flex-shrink: 0; padding: 0.7em 2.2em 0.7em 1em; border-radius: 8px; border: 1px solid var(--rule);
  background: var(--card-bg); color: var(--fg); font-family: -apple-system, "Segoe UI", sans-serif;
  font-size: 0.9em; cursor: pointer;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%235b5b5b' stroke-width='1.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 0.9em center; appearance: none; -webkit-appearance: none;
}
.sort-select:hover { border-color: var(--accent); }
.tag-filter-row { display: flex; flex-wrap: wrap; gap: 0.5em; margin-bottom: 1.4em; }
.tag-filter-row:empty { display: none; }
.tag-chip {
  border: 1px solid var(--rule); background: var(--card-bg); color: var(--muted);
  border-radius: 999px; padding: 0.3em 0.85em; font-size: 0.8em;
  font-family: -apple-system, "Segoe UI", sans-serif; cursor: pointer;
}
.tag-chip:hover { border-color: var(--accent); color: var(--fg); }
.tag-chip.active { background: var(--accent); border-color: var(--accent); color: #fff; }
.paper-list { display: flex; flex-direction: column; gap: 0.7em; }
.paper-card {
  display: flex; flex-direction: column;
  border: 1px solid var(--rule); border-radius: 10px; padding: 1em 1.2em;
  background: var(--card-bg);
}
.paper-card:hover { border-color: var(--accent); }
.paper-card-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.8em; }
.paper-card-link { display: block; flex: 1; min-width: 0; text-decoration: none; color: inherit; cursor: pointer; }
.paper-title { font-size: 1.05em; font-weight: 700; margin: 0 0 0.3em; line-height: 1.3; }
.paper-meta { color: var(--muted); font-size: 0.85em; font-family: -apple-system, "Segoe UI", sans-serif; }
.paper-tags {
  display: flex; flex-wrap: wrap; align-items: center; gap: 0.4em;
  margin-top: 0.6em; font-family: -apple-system, "Segoe UI", sans-serif;
}
.paper-tag {
  display: inline-flex; align-items: center; gap: 0.3em;
  background: var(--bg); border: 1px solid var(--rule); border-radius: 999px;
  padding: 0.15em 0.6em; font-size: 0.76em; color: var(--muted);
}
.paper-tag button {
  border: none; background: none; color: var(--muted); cursor: pointer; padding: 0;
  display: inline-flex; line-height: 1; font-size: 1em;
}
.paper-tag button:hover { color: var(--error); }
.paper-tag-add {
  border: 1px dashed var(--rule); background: none; color: var(--muted); cursor: pointer;
  border-radius: 999px; padding: 0.15em 0.6em; font-size: 0.76em;
  font-family: -apple-system, "Segoe UI", sans-serif;
}
.paper-tag-add:hover { border-color: var(--accent); color: var(--fg); }
.paper-tag-input {
  border: 1px solid var(--rule); background: var(--bg); color: var(--fg); border-radius: 999px;
  padding: 0.15em 0.6em; font-size: 0.76em; font-family: -apple-system, "Segoe UI", sans-serif;
  width: 8em;
}
.paper-delete-btn {
  flex-shrink: 0; border: none; background: none; color: var(--muted); cursor: pointer;
  padding: 0.4em; border-radius: 7px; display: inline-flex; align-items: center; justify-content: center;
  margin-top: -0.2em;
}
.paper-delete-btn:hover { color: var(--error); background: rgba(179, 38, 30, 0.1); }
.paper-delete-btn svg { width: 16px; height: 16px; display: block; }
.empty-state { color: var(--muted); font-family: -apple-system, "Segoe UI", sans-serif; text-align: center; padding: 3em 0; }
.site-footer {
  margin-top: 3em; padding-top: 1.5em; border-top: 1px solid var(--rule);
  font-family: -apple-system, "Segoe UI", sans-serif; font-size: 0.82em; color: var(--muted);
  display: flex; align-items: center; gap: 0.6em; flex-wrap: wrap;
}
.site-footer a { color: var(--muted); text-decoration: underline; text-underline-offset: 2px; }
.site-footer a:hover { color: var(--accent); }
.footer-sep { color: var(--rule); }
</style>
</head>
<body>
<div class="wrap">
  <div class="header-row">
    <div>
      <h1>Andrew&rsquo;s Paper Library</h1>
      <p class="sub">Drop a LaTeX source file to parse it into a reader page.</p>
    </div>
    <button type="button" class="icon-btn" id="themeToggleBtn" aria-label="Toggle theme"></button>
  </div>

  <div class="dropzone" id="dropzone">
    <div><strong>Drag &amp; drop</strong> a LaTeX source or saved HTML paper page here, or click to browse</div>
    <div class="hint">.tex, .zip, .tar.gz, .tgz &mdash; or a saved .html paper page (Save Page As&hellip; Webpage, Complete)</div>
    <input type="file" id="fileInput" accept=".tex,.zip,.tar.gz,.tgz,.tar,.html,.htm">
  </div>
  <div class="status" id="status"></div>

  <div class="search-row">
    <input type="text" id="searchBox" placeholder="Search papers by title or author...">
    <select id="sortSelect" class="sort-select" aria-label="Sort papers by">
      <option value="added">Most recently added</option>
      <option value="opened">Most recently opened</option>
      <option value="title">Title (A&ndash;Z)</option>
    </select>
  </div>
  <div class="tag-filter-row" id="tagFilterRow"></div>

  <div class="paper-list" id="paperList"></div>

  <footer class="site-footer">
    <a href="/about">About</a>
    <span class="footer-sep">&middot;</span>
    <a href="https://github.com/andrewluoooo/paper-reader">GitHub</a>
    <span class="footer-sep">&middot;</span>
    <span>Designed by Andrew Luo and Created with Claude Code</span>
  </footer>
</div>

<script>
(function () {
  var papers = [];
  var TRASH_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<polyline points="3 6 5 6 21 6"></polyline>' +
    '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>' +
    '<line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>';

  var SETTINGS_KEY = "paper_reader_settings";
  var SVG_OPEN = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
  var THEME_ICONS = {
    auto: SVG_OPEN + '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/></svg>',
    light: SVG_OPEN + '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>' +
      '<line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>' +
      '<line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>' +
      '<line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>',
    dark: SVG_OPEN + '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
  };

  function loadSettings() {
    try { return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}"); } catch (e) { return {}; }
  }
  function saveSettings(patch) {
    var s = loadSettings();
    Object.assign(s, patch);
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (e) {}
  }
  function applyTheme(theme) {
    var root = document.documentElement;
    if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
    else root.removeAttribute("data-theme");
    var btn = document.getElementById("themeToggleBtn");
    if (btn) btn.innerHTML = THEME_ICONS[theme] || THEME_ICONS.auto;
  }
  function initTheme() {
    applyTheme(loadSettings().theme || "auto");
    var btn = document.getElementById("themeToggleBtn");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var cur = loadSettings().theme || "auto";
      var next = cur === "auto" ? "light" : cur === "light" ? "dark" : "auto";
      saveSettings({ theme: next });
      applyTheme(next);
    });
  }

  function fmtDate(ts) {
    var d = new Date(ts * 1000);
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  var activeTags = [];
  var sortBy = loadSettings().sortBy || "added";

  var SORT_COMPARATORS = {
    added: function (a, b) { return (b.addedAt || 0) - (a.addedAt || 0); },
    opened: function (a, b) { return (b.lastOpenedAt || 0) - (a.lastOpenedAt || 0); },
    title: function (a, b) { return a.title.localeCompare(b.title); }
  };

  function allTags() {
    var set = {};
    papers.forEach(function (p) { (p.tags || []).forEach(function (t) { set[t] = true; }); });
    return Object.keys(set).sort(function (a, b) { return a.localeCompare(b); });
  }

  function renderTagFilterRow() {
    var row = document.getElementById("tagFilterRow");
    var tags = allTags();
    // drop any active filter tags that no longer exist on any paper
    activeTags = activeTags.filter(function (t) { return tags.indexOf(t) !== -1; });
    row.innerHTML = "";
    tags.forEach(function (t) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "tag-chip" + (activeTags.indexOf(t) !== -1 ? " active" : "");
      chip.textContent = t;
      chip.addEventListener("click", function () {
        var idx = activeTags.indexOf(t);
        if (idx === -1) activeTags.push(t); else activeTags.splice(idx, 1);
        renderTagFilterRow();
        render(document.getElementById("searchBox").value);
      });
      row.appendChild(chip);
    });
  }

  function updateTags(p, newTags) {
    fetch("/api/papers/" + encodeURIComponent(p.id) + "/tags", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tags: newTags })
    })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (!res.ok) {
          setStatus("Could not update tags: " + (res.data.error || "unknown error"), "error");
          return;
        }
        p.tags = res.data.tags;
        renderTagFilterRow();
        render(document.getElementById("searchBox").value);
      })
      .catch(function (e) { setStatus("Could not update tags: " + e.message, "error"); });
  }

  function startTagInput(p, tagsWrap, addBtn) {
    var input = document.createElement("input");
    input.type = "text";
    input.className = "paper-tag-input";
    input.placeholder = "tag name";
    addBtn.replaceWith(input);
    input.focus();
    function commit() {
      var val = input.value.trim();
      if (val) updateTags(p, (p.tags || []).concat([val]));
      else render(document.getElementById("searchBox").value);
    }
    input.addEventListener("keydown", function (e) {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      else if (e.key === "Escape") { render(document.getElementById("searchBox").value); }
    });
    input.addEventListener("blur", commit);
    input.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); });
  }

  function render(filter) {
    var list = document.getElementById("paperList");
    var q = (filter || "").trim().toLowerCase();
    var filtered = papers.filter(function (p) {
      if (q) {
        var hay = (p.title + " " + (p.authors || []).join(" ") + " " + (p.venue || "")).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      if (activeTags.length) {
        var ptags = (p.tags || []).map(function (t) { return t.toLowerCase(); });
        if (!activeTags.every(function (t) { return ptags.indexOf(t.toLowerCase()) !== -1; })) return false;
      }
      return true;
    });
    filtered.sort(SORT_COMPARATORS[sortBy] || SORT_COMPARATORS.added);
    if (!filtered.length) {
      list.innerHTML = '<div class="empty-state">' +
        (papers.length ? "No matching papers." : "No papers yet \\u2014 drop one above to get started.") +
        "</div>";
      return;
    }
    list.innerHTML = "";
    filtered.forEach(function (p) {
      var card = document.createElement("div");
      card.className = "paper-card";

      var top = document.createElement("div");
      top.className = "paper-card-top";

      var a = document.createElement("a");
      a.className = "paper-card-link";
      a.href = "/library/" + encodeURIComponent(p.id) + ".html";
      var titleEl = document.createElement("div");
      titleEl.className = "paper-title";
      titleEl.textContent = p.title;
      var metaEl = document.createElement("div");
      metaEl.className = "paper-meta";
      var authors = (p.authors || []).join(", ");
      metaEl.textContent = (authors ? authors + " \\u2014 " : "") + fmtDate(p.addedAt);
      a.appendChild(titleEl);
      a.appendChild(metaEl);

      var delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "paper-delete-btn";
      delBtn.setAttribute("aria-label", "Remove from library");
      delBtn.title = "Remove from library";
      delBtn.innerHTML = TRASH_ICON;
      delBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        removePaper(p);
      });

      top.appendChild(a);
      top.appendChild(delBtn);
      card.appendChild(top);

      var tagsWrap = document.createElement("div");
      tagsWrap.className = "paper-tags";
      (p.tags || []).forEach(function (t) {
        var pill = document.createElement("span");
        pill.className = "paper-tag";
        var label = document.createElement("span");
        label.textContent = t;
        var rm = document.createElement("button");
        rm.type = "button";
        rm.innerHTML = "&times;";
        rm.setAttribute("aria-label", "Remove tag " + t);
        rm.addEventListener("click", function (e) {
          e.preventDefault();
          e.stopPropagation();
          updateTags(p, (p.tags || []).filter(function (x) { return x !== t; }));
        });
        pill.appendChild(label);
        pill.appendChild(rm);
        tagsWrap.appendChild(pill);
      });
      var addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "paper-tag-add";
      addBtn.textContent = "+ tag";
      addBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        startTagInput(p, tagsWrap, addBtn);
      });
      tagsWrap.appendChild(addBtn);
      card.appendChild(tagsWrap);

      list.appendChild(card);
    });
  }

  function removePaper(p) {
    if (!window.confirm('Remove "' + p.title + '" from your library? This cannot be undone.')) return;
    fetch("/api/papers/" + encodeURIComponent(p.id), { method: "DELETE" })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (!res.ok) {
          setStatus("Could not remove paper: " + (res.data.error || "unknown error"), "error");
          return;
        }
        loadPapers();
      })
      .catch(function (e) {
        setStatus("Could not remove paper: " + e.message, "error");
      });
  }

  function loadPapers() {
    fetch("/api/papers").then(function (r) { return r.json(); }).then(function (data) {
      papers = data;
      renderTagFilterRow();
      render(document.getElementById("searchBox").value);
    });
  }

  function setStatus(msg, cls) {
    var el = document.getElementById("status");
    el.className = "status" + (cls ? " " + cls : "");
    el.textContent = msg;
  }
  function setStatusLoading(msg) {
    var el = document.getElementById("status");
    el.className = "status loading";
    el.innerHTML = '<span class="spinner"></span>' + msg;
  }

  function upload(file) {
    setStatusLoading('Parsing "' + file.name + '"\\u2026 this can take up to a minute.');
    fetch("/api/upload?filename=" + encodeURIComponent(file.name), { method: "POST", body: file })
      .then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      })
      .then(function (res) {
        if (!res.ok) {
          setStatus('Could not parse "' + file.name + '": ' + (res.data.error || "unknown error"), "error");
          return;
        }
        setStatus("");
        loadPapers();
      })
      .catch(function (e) {
        setStatus("Upload failed: " + e.message, "error");
      });
  }

  var dropzone = document.getElementById("dropzone");
  var fileInput = document.getElementById("fileInput");
  dropzone.addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () {
    if (fileInput.files[0]) upload(fileInput.files[0]);
    fileInput.value = "";
  });
  ["dragenter", "dragover"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.add("dragover"); });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.remove("dragover"); });
  });
  dropzone.addEventListener("drop", function (e) {
    var files = e.dataTransfer.files;
    if (files && files[0]) upload(files[0]);
  });

  document.getElementById("searchBox").addEventListener("input", function () {
    render(this.value);
  });
  var sortSelect = document.getElementById("sortSelect");
  sortSelect.value = sortBy;
  sortSelect.addEventListener("change", function () {
    sortBy = this.value;
    saveSettings({ sortBy: sortBy });
    render(document.getElementById("searchBox").value);
  });

  initTheme();
  loadPapers();
})();
</script>
</body>
</html>
"""

ABOUT_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<script>
(function () {
  try {
    var s = JSON.parse(localStorage.getItem("paper_reader_settings") || "{}");
    if (s.theme === "light" || s.theme === "dark") document.documentElement.setAttribute("data-theme", s.theme);
  } catch (e) {}
})();
</script>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>About &mdash; Andrew's Paper Library</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #161513; --fg: #ece9e2; --muted: #a9a49a; --rule: #33322d; --accent: #7fa7ff; }
}
:root[data-theme="light"] { --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db; }
:root[data-theme="dark"] { --bg: #161513; --fg: #ece9e2; --muted: #a9a49a; --rule: #33322d; --accent: #7fa7ff; }
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, "Times New Roman", serif;
}
.wrap { max-width: 640px; margin: 0 auto; padding: 9vh 6vw 12vh; }
.back-link {
  display: inline-flex; align-items: center; gap: 0.4em; color: var(--muted); text-decoration: none;
  font-family: -apple-system, "Segoe UI", sans-serif; font-size: 0.85em; margin-bottom: 2.5em;
}
.back-link:hover { color: var(--accent); }
.back-link svg { display: block; }
h1 { font-size: 1.6em; margin: 0 0 0.9em; }
p { line-height: 1.7; font-size: 1.05em; }
p a { color: var(--accent); }
</style>
</head>
<body>
<div class="wrap">
  <a class="back-link" href="/">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <line x1="19" y1="12" x2="5" y2="12"></line><polyline points="12 19 5 12 12 5"></polyline>
    </svg>
    Back to library
  </a>
  <h1>About</h1>
  <p>readwise (Reader) is one of my favorite products of all time. unfortunately they never added latex support so I could not read papers using the default. i vibe coded this out so that i can do that now.</p>
  <p>given the fact that this is vibe coded and that i am prone to dumbassery, please treat this as a prototype and don't do anything extremely stupid. if you like this idea, let me know and i might flesh it out even more! you are free to self host if you find this valuable for your workflow. all the code is on <a href="https://github.com/andrewluoooo/paper-reader">github</a>.</p>
</div>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "PaperReaderLibrary/1.0"

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status: int = 200) -> None:
        self._send_bytes(json.dumps(obj).encode("utf-8"), "application/json", status)

    def _send_html(self, html_str: str, status: int = 200) -> None:
        self._send_bytes(html_str.encode("utf-8"), "text/html; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HOME_PAGE_HTML)
        elif parsed.path == "/about":
            self._send_html(ABOUT_PAGE_HTML)
        elif parsed.path == "/api/papers":
            self._send_json(_load_index())
        elif parsed.path.startswith("/library/"):
            self._serve_library_file(parsed.path[len("/library/") :])
        else:
            self._send_html("<h1>404</h1>", 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self._handle_upload(parsed)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/papers/"):
            paper_id = os.path.basename(parsed.path[len("/api/papers/") :])
            if _delete_paper(paper_id):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, 404)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/papers/") and parsed.path.endswith("/tags"):
            paper_id = os.path.basename(parsed.path[len("/api/papers/") : -len("/tags")])
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json({"error": "invalid JSON body"}, 400)
                return
            tags = body.get("tags")
            if not isinstance(tags, list):
                self._send_json({"error": "expected {\"tags\": [...]}"}, 400)
                return
            entry = _set_paper_tags(paper_id, tags)
            if entry is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json(entry)
        else:
            self._send_json({"error": "not found"}, 404)

    def _serve_library_file(self, name: str) -> None:
        safe_name = os.path.basename(name)  # no path traversal
        if not safe_name.endswith(".html"):
            self._send_html("<h1>404</h1>", 404)
            return
        path = LIBRARY_DIR / safe_name
        if not path.is_file():
            self._send_html("<h1>404</h1>", 404)
            return
        _touch_opened(safe_name[: -len(".html")])
        self._send_bytes(path.read_bytes(), "text/html; charset=utf-8")

    def _handle_upload(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        filename = (qs.get("filename") or ["upload"])[0]
        if not filename.lower().endswith(ALLOWED_UPLOAD_SUFFIXES):
            self._send_json({"error": "unsupported file type -- use .tex, .zip, .tar.gz, .tgz, or .tar"}, 400)
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            self._send_json({"error": "empty upload"}, 400)
            return
        if length > MAX_UPLOAD_BYTES:
            self._send_json({"error": "file too large (60MB limit)"}, 400)
            return
        data = self.rfile.read(length)

        try:
            entry = _process_upload(filename, data)
        except (LatexConvertError, HtmlConvertError) as e:
            self._send_json({"error": str(e)}, 400)
            return
        except Exception as e:  # keep the server alive even if one paper fails to convert
            self._send_json({"error": f"unexpected error: {html.escape(str(e))}"}, 500)
            return

        self._send_json(entry)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        print("[library] " + (format % args))


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    rebuild_library()
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"Andrew's Paper Library running at {url}  (library stored in {LIBRARY_DIR})")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
