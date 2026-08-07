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
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .html_convert import HtmlConvertError
from .html_convert import convert as convert_html
from .latex_convert import LatexConvertError, convert as convert_latex
from .pdf_convert import PdfConvertError
from .pdf_convert import convert as convert_pdf
from .restyle import restyle

LIBRARY_DIR = Path.home() / ".paper_reader_library"
INDEX_PATH = LIBRARY_DIR / "index.json"
PID_PATH = LIBRARY_DIR / "server.pid"
LOG_PATH = LIBRARY_DIR / "server.log"
# Pre-restyle LaTeXML output (HTML + figure files) for each paper, kept
# around permanently (not in a TemporaryDirectory) so that reader/CSS/JS
# changes in restyle.py can be re-applied to already-uploaded papers
# without re-running the slow LaTeX->HTML conversion.
RAW_DIR = LIBRARY_DIR / "raw"

ALLOWED_UPLOAD_SUFFIXES = (".tex", ".zip", ".tar.gz", ".tgz", ".tar", ".html", ".htm", ".pdf")
HTML_SOURCE_SUFFIXES = (".html", ".htm")
PDF_SOURCE_SUFFIXES = (".pdf",)
MAX_UPLOAD_BYTES = 60 * 1024 * 1024  # 60MB is generous for a LaTeX source tree
PAPER_STATUSES = ("inbox", "later", "archive", "trash")

# ---------------------------------------------------------------- pipeline jobs
# Uploads run in their own thread (ThreadingHTTPServer) and can take anywhere
# from seconds to well over an hour (a real LaTeXML run on a large paper) --
# this is just an in-memory, best-effort record of what's currently
# converting and what recently finished, for the /pipeline status page. Not
# persisted: a server restart clears it, same as it would drop an in-flight
# upload's connection anyway.
_JOBS_LOCK = threading.Lock()
_active_jobs: dict[str, dict] = {}
_JOB_HISTORY_LIMIT = 30
_job_history: list[dict] = []


def _job_start(filename: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _active_jobs[job_id] = {
            "id": job_id,
            "filename": filename,
            "stage": "preparing",
            "startedAt": time.time(),
        }
    return job_id


def _job_stage(job_id: str, stage: str) -> None:
    with _JOBS_LOCK:
        job = _active_jobs.get(job_id)
        if job is not None:
            job["stage"] = stage


def _job_finish(job_id: str, ok: bool, error: str = "", paper_id: str = "") -> None:
    with _JOBS_LOCK:
        job = _active_jobs.pop(job_id, None)
        if job is None:
            return
        job["finishedAt"] = time.time()
        job["ok"] = ok
        job["error"] = error
        job["paperId"] = paper_id
        _job_history.insert(0, job)
        del _job_history[_JOB_HISTORY_LIMIT:]


def _list_jobs() -> dict:
    with _JOBS_LOCK:
        active = [dict(j) for j in _active_jobs.values()]
        history = [dict(j) for j in _job_history]
    active.sort(key=lambda j: j["startedAt"])
    return {"active": active, "history": history}


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
    job_id = _job_start(filename)
    try:
        paper_id = uuid.uuid4().hex[:12]
        LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        raw_workdir = RAW_DIR / paper_id
        raw_workdir.mkdir(parents=True, exist_ok=True)

        is_html_source = filename.lower().endswith(HTML_SOURCE_SUFFIXES)
        is_pdf_source = filename.lower().endswith(PDF_SOURCE_SUFFIXES)
        kind = "html" if is_html_source else "pdf" if is_pdf_source else "latex"
        with tempfile.TemporaryDirectory(prefix="paper_reader_upload_") as tmp:
            tmp_path = Path(tmp) / os.path.basename(filename)
            tmp_path.write_bytes(data)
            _job_stage(job_id, f"converting ({kind})")
            if is_html_source:
                raw_html_path = convert_html(str(tmp_path), str(raw_workdir))
            elif is_pdf_source:
                raw_html_path = convert_pdf(str(tmp_path), str(raw_workdir))
            else:
                raw_html_path = convert_latex(str(tmp_path), str(raw_workdir))

        _job_stage(job_id, "styling")
        html_out, metadata = restyle(raw_html_path, source_name=filename, back_link="/")
        (LIBRARY_DIR / f"{paper_id}.html").write_text(html_out, encoding="utf-8")

        _job_stage(job_id, "saving")
        entry = {
            "id": paper_id,
            "title": metadata.get("title") or filename,
            "authors": [a["name"] for a in metadata.get("authors", [])],
            "venue": metadata.get("venue", ""),
            "summary": (metadata.get("abstract") or "").strip()[:320],
            "sourceFilename": filename,
            "addedAt": time.time(),
            "lastOpenedAt": None,
            "rawHtmlPath": raw_html_path,
            "tags": [],
            "status": "inbox",
            "pinned": False,
            "completed": False,
            "deletedAt": None,
        }
        items = _load_index()
        items.insert(0, entry)
        _save_index(items)
        _job_finish(job_id, ok=True, paper_id=paper_id)
        return entry
    except Exception as e:
        _job_finish(job_id, ok=False, error=str(e))
        raise


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
    entry["summary"] = (metadata.get("abstract") or "").strip()[:320]
    return True



def _trigger_git_sync():
    import threading, subprocess
    lib = LIBRARY_DIR
    if not (lib / ".git").exists():
        return
    def sync_task():
        try:
            subprocess.run(["git", "add", "."], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "commit", "-m", "Auto-sync update"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "pull", "origin", "main", "--rebase", "--strategy-option=ours"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "push", "origin", "main"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    threading.Thread(target=sync_task, daemon=True).start()

def _delete_paper(paper_id: str) -> bool:
    """Permanently remove a paper: its index entry, generated HTML, and
    raw source are all deleted from disk with no way back. This is the
    trash can's own "delete permanently" action (DELETE /api/papers/id)
    -- ordinary deletion from inbox/later/archive is a soft delete via
    _set_paper_status(paper_id, "trash") instead, which this function
    has no involvement in. Returns False if no entry with that id
    exists."""
    items = _load_index()
    remaining = [e for e in items if e["id"] != paper_id]
    if len(remaining) == len(items):
        return False
    _save_index(remaining)
    _trigger_git_sync()
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
    _trigger_git_sync()
    return entry



def _update_paper(paper_id: str, updates: dict) -> dict | None:
    items = _load_index()
    entry = next((e for e in items if e["id"] == paper_id), None)
    if entry is None:
        return None
    for k, v in updates.items():
        if k in ("pinned", "completed"):
            entry[k] = bool(v)
    _save_index(items)
    _trigger_git_sync()
    return entry


def _set_paper_status(paper_id: str, status: str) -> dict | None:
    """Move a paper between inbox/later/archive/trash. Returns the
    updated entry, or None if no paper with that id exists. Caller is
    responsible for validating status against PAPER_STATUSES.

    "trash" is a soft delete -- the entry, its generated HTML, and its
    raw source are all left untouched on disk, exactly like any other
    status. Only _delete_paper() (used for the trash can's own
    "delete permanently" action) actually removes anything. deletedAt
    just records when a paper most recently landed in the trash, so the
    trash view can show "Deleted 3 days ago"; it's cleared again if the
    paper is restored to any other status."""
    items = _load_index()
    entry = next((e for e in items if e["id"] == paper_id), None)
    if entry is None:
        return None
    entry["status"] = status
    entry["deletedAt"] = time.time() if status == "trash" else None
    _save_index(items)
    _trigger_git_sync()
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
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='m16 6 4 14'/><path d='M12 6v14'/><path d='M8 8v12'/><path d='M4 4v16'/></svg>">
<script>
(function () {
  try {
    var s = JSON.parse(localStorage.getItem("paper_reader_settings") || "{}");
    if (s.theme === "light" || s.theme === "dark") document.documentElement.setAttribute("data-theme", s.theme);
    if (s.librarySidebarHidden) document.documentElement.classList.add("library-sidebar-collapsed");
    if (s.libraryInfoPanelHidden) document.documentElement.classList.add("library-info-collapsed");
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
  --accent: #1a56db; --card-bg: #fbfaf8; --error: #b3261e; --sidebar-bg: #f7f5f1;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #1e1e1e; --fg: #ece9e2; --muted: #a9a49a; --rule: #444444; --accent: #7fa7ff; --card-bg: #2a2a2a; --error: #ff6b60; --sidebar-bg: #222222; }
}
:root[data-theme="light"] {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db; --card-bg: #fbfaf8; --error: #b3261e; --sidebar-bg: #f7f5f1;
}
:root[data-theme="dark"] {
  --bg: #1e1e1e; --fg: #ece9e2; --muted: #a9a49a; --rule: #444444; --accent: #7fa7ff; --card-bg: #2a2a2a; --error: #ff6b60; --sidebar-bg: #222222;
}

:root[data-theme="dark"] [style*="color: #000"],
:root[data-theme="dark"] [style*="color:#000"],
:root[data-theme="dark"] [style*="color: black"],
:root[data-theme="dark"] [style*="color:black"],
:root:not([data-theme="light"]) [style*="color: #000"],
:root:not([data-theme="light"]) [style*="color:#000"],
:root:not([data-theme="light"]) [style*="color: black"],
:root:not([data-theme="light"]) [style*="color:black"] {
  color: inherit !important;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  line-height: 1.5;
  transition: background-color 0.2s ease, color 0.2s ease;
}
button, input, select, textarea { font-family: inherit; }
.app-shell { display: flex; height: 100vh; overflow: hidden; }

/* ------------------------------------------------------------- animations */
@keyframes cardIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
@keyframes panelSlideIn { from { opacity: 0; transform: translateX(10px); } to { opacity: 1; transform: translateX(0); } }
@keyframes overlayFadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes overlayCardIn { from { opacity: 0; transform: scale(0.96); } to { opacity: 1; transform: scale(1); } }
@keyframes menuIn { from { opacity: 0; transform: scale(0.95) translateY(-4px); } to { opacity: 1; transform: scale(1) translateY(0); } }
@keyframes slideDown { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.001ms !important; animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important; scroll-behavior: auto !important;
  }
}

/* ---------------------------------------------------------------- sidebar */
.sidebar {
  flex: 0 0 234px; width: 234px; flex-shrink: 0; background: var(--sidebar-bg); border-right: 1px solid var(--rule);
  display: flex; flex-direction: column; padding: 1.1em 0.9em; overflow-x: hidden; overflow-y: auto;
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  transition: flex-basis 0.22s ease, width 0.22s ease, padding-left 0.22s ease, padding-right 0.22s ease, border-right-color 0.22s ease, opacity 0.15s ease, background-color 0.2s ease, border-color 0.2s ease;
}
html.library-sidebar-collapsed .sidebar {
  flex-basis: 0; width: 0; padding-left: 0; padding-right: 0; border-right-color: transparent; opacity: 0;
}
.sidebar-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.6em; padding: 0 0.3em; margin-bottom: 1.4em; }
.brand { display: flex; align-items: center; gap: 0.35em; flex: 1; min-width: 0; font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif; font-weight: 600; font-size: 1.05em; line-height: 1.25; }
.sidebar-add-btn { width: 28px; height: 28px; font-size: 1.2em; line-height: 1; }
.sidebar-nav { display: flex; flex-direction: column; gap: 0.1em; flex: 1; min-height: 0; }
.nav-item {
  display: flex; align-items: center; gap: 0.5em; width: 100%; text-align: left; background: none; border: none; cursor: pointer;
  padding: 0.5em 0.6em; border-radius: 7px; font-size: 0.92em; color: var(--fg);
  transition: background-color 0.15s ease;
}
.nav-item:hover { background: var(--rule); }
.nav-item-sub { color: var(--muted); font-size: 0.85em; margin-left: auto; }
.nav-section-label {
  margin: 1.1em 0 0.3em; padding: 0 0.6em; font-size: 0.72em; font-weight: 600; letter-spacing: 0.05em;
  text-transform: uppercase; color: var(--muted);
}
.nav-tags { display: flex; flex-direction: column; gap: 0.05em; overflow-y: auto; min-height: 0; }
.nav-tags-empty { padding: 0.4em 0.6em; font-size: 0.82em; color: var(--muted); }
.nav-tag-item {
  display: flex; align-items: center; gap: 0.4em; width: 100%; text-align: left; background: none; border: none; cursor: pointer;
  padding: 0.4em 0.6em; border-radius: 7px; font-size: 0.85em; color: var(--muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.nav-tag-item:hover { background: var(--rule); color: var(--fg); }
.nav-tag-item.active { background: var(--accent); color: #fff; }
.sidebar-bottom { margin-top: 0.8em; padding-top: 0.6em; border-top: 1px solid var(--rule); flex-shrink: 0; position: relative; }
.sidebar-footer { padding: 0.6em 0.6em 0.1em; font-size: 0.78em; color: var(--muted); }
.sidebar-footer a { color: var(--muted); text-decoration: underline; text-underline-offset: 2px; }
.sidebar-footer a:hover { color: var(--accent); }
.footer-sep { color: var(--rule); }

.prefs-popover {
  position: absolute; left: 0; bottom: 100%; margin-bottom: 0.4em; z-index: 400;
  width: 220px; background: var(--card-bg); border: 1px solid var(--rule);
  border-radius: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18); padding: 0.7em;
}
.prefs-popover[hidden] { display: none; }
.prefs-popover-label {
  font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--muted); margin: 0.3em 0.4em;
}
.prefs-popover-row { display: flex; align-items: center; justify-content: space-between; padding: 0.35em 0.4em 0.55em; }
.prefs-theme-grid { display: flex; gap: 0.3em; padding: 0 0.4em 0.5em; }
.prefs-theme-grid button {
  flex: 1; padding: 0.4em 0; border-radius: 7px; border: 1px solid var(--rule);
  background: var(--bg); color: var(--fg); font-size: 0.78em; cursor: pointer;
}
.prefs-theme-grid button.active { border-color: var(--accent); color: var(--accent); }
.prefs-switch { position: relative; display: inline-block; width: 34px; height: 20px; flex-shrink: 0; }
.prefs-switch input { position: absolute; opacity: 0; width: 100%; height: 100%; margin: 0; cursor: pointer; }
.prefs-switch-track { position: absolute; inset: 0; background: var(--rule); border-radius: 999px; transition: background 0.15s ease; }
.prefs-switch-track::before {
  content: ""; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px;
  background: #fff; border-radius: 50%; transition: transform 0.15s ease; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
}
.prefs-switch input:checked + .prefs-switch-track { background: var(--accent); }
.prefs-switch input:checked + .prefs-switch-track::before { transform: translateX(14px); }
.prefs-switch input:focus-visible + .prefs-switch-track { outline: 2px solid var(--accent); outline-offset: 2px; }
.prefs-input {
  width: 100%; box-sizing: border-box; padding: 0.4em 0.5em; border-radius: 6px; border: 1px solid var(--rule);
  background: var(--bg); color: var(--fg); font-size: 0.78em; outline: none;
}
.prefs-input:focus { border-color: var(--accent); }
.prefs-btn {
  flex: 1; padding: 0.4em 0; border-radius: 6px; border: 1px solid var(--rule);
  background: var(--bg); color: var(--fg); font-size: 0.78em; cursor: pointer;
}
.prefs-btn:hover { background: var(--rule); }

/* --------------------------------------------------------------- main col */
.main-col { flex: 1; min-width: 0; overflow-y: auto; padding: 2.4em 3.2vw 8vh; }
.main-topbar {
  display: flex; align-items: center; gap: 1.8em; border-bottom: 1px solid var(--rule); margin-bottom: 1.3em;
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
}
.topbar-title { display: flex; align-items: center; gap: 0.3em; font-weight: 600; font-size: 0.95em; padding-bottom: 0.75em; color: var(--fg); }
.topbar-title svg { width: 11px; height: 11px; color: var(--muted); }
.tabs-row { display: flex; align-items: flex-end; gap: 1.6em; flex: 1; }
.tab-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 0.4em; background: none; border: none; padding: 0 0 0.7em; margin-bottom: -1px; cursor: pointer;
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif; font-size: 0.82em; font-weight: 600;
  letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted);
  border-bottom: 2px solid transparent;
  transition: color 0.15s ease, border-bottom-color 0.15s ease;
}
.tab-btn:hover { color: var(--fg); }
.tab-btn.active { color: var(--fg); border-bottom-color: var(--accent); }
.icon-btn {
  flex-shrink: 0; width: 34px; height: 34px; border-radius: 999px; border: 1px solid var(--rule);
  background: var(--card-bg); color: var(--fg); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  transition: background-color 0.15s ease, transform 0.1s ease;
}
.icon-btn:hover { background: var(--rule); }
.icon-btn:active { transform: scale(0.92); }
.icon-btn svg { display: block; }
input[type=file] { display: none; }
.status { font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif; font-size: 0.88em; margin-bottom: 1em; min-height: 1.3em; }
.status.error { color: var(--error); }
@keyframes spin { to { transform: rotate(360deg); } }
.search-row { display: flex; gap: 0.6em; margin-bottom: 1.2em; animation: slideDown 0.15s ease; }
.search-row[hidden] { display: none; }
.search-row input {
  flex: 1; min-width: 0; padding: 0.7em 1em; border-radius: 8px; border: 1px solid var(--rule);
  background: var(--card-bg); color: var(--fg); font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  font-size: 0.95em; transition: background-color 0.2s ease, border-color 0.15s ease;
}
.search-row input:focus { border-color: var(--accent); outline: none; }
.sort-control {
  position: relative; display: flex; align-items: center; flex-shrink: 0; margin-bottom: 0.6em; border-radius: 6px;
  transition: background-color 0.15s ease;
}
.sort-control:hover { background: var(--rule); }
.sort-control svg.sort-lead-icon { position: absolute; left: 0.5em; width: 12px; height: 12px; color: var(--muted); pointer-events: none; }
.sort-select {
  flex-shrink: 0; padding: 0.4em 1.7em 0.4em 1.7em; border-radius: 6px; border: none;
  background: none; color: var(--muted); font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  font-size: 0.85em; font-weight: 600; cursor: pointer; transition: color 0.15s ease;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%235b5b5b' stroke-width='1.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 0.55em center; appearance: none; -webkit-appearance: none;
}
.sort-control:hover .sort-select { color: var(--fg); }
.paper-list { display: flex; flex-direction: column; }
.paper-card {
  position: relative; display: flex; align-items: flex-start; gap: 0.9em;
  border: 1px solid var(--rule); border-radius: 10px;
  padding: 0.85em 1.1em;
  margin-bottom: 0.6em;
  background: var(--card-bg);
  animation: cardIn 0.18s ease both;
  transition: border-color 0.15s ease, background-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
}
.paper-card:last-child { margin-bottom: 0; }
.paper-card::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background-color: transparent; pointer-events: none;
  transition: background-color 0.15s ease;
}
.paper-card:hover {
  border-color: var(--accent);
  transform: translateY(-1px); box-shadow: 0 4px 14px rgba(0, 0, 0, 0.07);
}
.paper-card.selected { border-color: var(--accent); }

.paper-card.completed { opacity: 0.65; }
.paper-card.completed:hover { opacity: 1; }

.paper-card.dragging { opacity: 0.5; }
.tab-btn.drag-over, #navPinned.drag-over, #navPinnedLabel.drag-over { background-color: var(--rule); border-color: var(--accent); }
#navPinned.drag-over { border-radius: 6px; border: 1px dashed var(--accent); }

.nav-pinned-empty { padding: 0.5em 1.2em; font-size: 0.8em; color: var(--muted); }


.card-progress-track {
  position: absolute; left: 0; bottom: 0; right: 0; height: 3px;
  background: transparent; pointer-events: none;
  border-radius: 0 0 9px 9px;
  clip-path: inset(0 0 0 0 round 0 0 9px 9px);
  -webkit-clip-path: inset(0 0 0 0 round 0 0 9px 9px);
}
.card-progress-fill {
  height: 100%; background: var(--accent); opacity: 0.8;
}
.paper-thumb {
  flex-shrink: 0; width: 44px; height: 44px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 600; font-size: 1.05em; font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
}
.paper-card-link { display: block; flex: 1; min-width: 0; text-decoration: none; color: inherit; cursor: pointer; }
.paper-title {
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  font-size: 1.02em; font-weight: 600; margin: 0 0 0.22em; line-height: 1.35;
}
.paper-summary {
  color: var(--muted); font-size: 0.85em; font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  line-height: 1.5;
  margin-bottom: 0.3em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.paper-meta {
  color: var(--muted); font-size: 0.82em; line-height: 1.4; font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  display: flex; align-items: center; gap: 0.35em;
}
.paper-meta svg { width: 13px; height: 13px; flex-shrink: 0; }
.paper-actions { display: flex; align-items: center; flex-shrink: 0; gap: 0.05em; margin-top: -0.15em; }
.paper-action-btn {
  border: none; background: none; color: var(--muted); cursor: pointer;
  padding: 0.4em; border-radius: 7px; display: inline-flex; align-items: center; justify-content: center;
  transition: background-color 0.15s ease, color 0.15s ease, transform 0.1s ease;
}
.paper-action-btn:hover { color: var(--fg); background: var(--rule); }
.paper-action-btn:active { transform: scale(0.9); }
.paper-action-btn.active { color: var(--accent); }
.paper-action-btn.pin-btn.active { color: #10b981; fill: #10b981; }
.paper-action-btn.danger-action:hover { color: var(--error); }
.paper-action-btn svg { width: 16px; height: 16px; display: block; }
.more-wrap { position: relative; }
.more-menu {
  position: absolute; right: 0; top: calc(100% + 4px); min-width: 170px; z-index: 30; overflow: hidden;
  background: var(--card-bg); border: 1px solid var(--rule); border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.15);
  transform-origin: top right; animation: menuIn 0.12s ease;
}
.more-menu button {
  display: block; width: 100%; text-align: left; padding: 0.6em 0.9em; border: none; background: none;
  color: var(--fg); font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif; font-size: 0.85em; cursor: pointer;
  transition: background-color 0.1s ease;
}
.more-menu button:hover { background: var(--rule); }
.more-menu button.danger { color: var(--error); }
.empty-state { color: var(--muted); font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif; text-align: center; padding: 3em 0; }

/* -------------------------------------------------------------- tags UI (info panel) */
.paper-tags { display: flex; flex-wrap: wrap; align-items: center; gap: 0.4em; }
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
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif; transition: border-color 0.15s ease, color 0.15s ease;
}
.paper-tag-add:hover { border-color: var(--accent); color: var(--fg); }
.paper-tag-input {
  border: 1px solid var(--rule); background: var(--bg); color: var(--fg); border-radius: 999px;
  padding: 0.15em 0.6em; font-size: 0.76em; font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  width: 8em;
}

/* --------------------------------------------------------------- info panel */
.info-panel {
  flex: 0 0 300px; width: 300px; flex-shrink: 0; border-left: 1px solid var(--rule); overflow-x: hidden; overflow-y: auto;
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  transition: flex-basis 0.22s ease, width 0.22s ease, padding-left 0.22s ease, padding-right 0.22s ease, border-left-color 0.22s ease, opacity 0.15s ease, background-color 0.2s ease, border-color 0.2s ease;
}
.info-panel[hidden], html.library-info-collapsed .info-panel {
  display: block; /* Override default hidden behavior so it can animate */
  flex-basis: 0; width: 0; padding-left: 0; padding-right: 0; border-left-color: transparent; opacity: 0;
}
.info-panel-top {
  display: flex; align-items: center; justify-content: space-between;
  padding: 1.1em 1.2em; border-bottom: 1px solid var(--rule); font-weight: 600; font-size: 0.9em;
}
.info-panel-top .icon-btn { width: 28px; height: 28px; }
.info-panel-body { padding: 1.2em; }
.info-title { font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif; font-size: 1.15em; font-weight: 600; margin: 0 0 0.6em; line-height: 1.35; }
.info-open-link { display: inline-block; color: var(--accent); text-decoration: none; font-size: 0.88em; margin-bottom: 1.2em; }
.info-open-link:hover { text-decoration: underline; }
.info-authors-row { display: flex; align-items: center; gap: 0.7em; margin-bottom: 1.3em; }
.info-avatar {
  flex-shrink: 0; width: 34px; height: 34px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center; color: #fff; font-weight: 600; font-size: 0.95em;
}
.info-authors-row div:last-child { font-size: 0.88em; }
.info-section-label {
  font-size: 0.72em; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase;
  color: var(--muted); margin: 0 0 0.5em;
}
.info-summary { font-size: 0.88em; line-height: 1.6; margin-bottom: 1.3em; }
.info-meta-table { margin-bottom: 1.3em; }
.info-meta-row { display: flex; justify-content: space-between; gap: 0.8em; padding: 0.4em 0; border-bottom: 1px solid var(--rule); font-size: 0.85em; }
.info-meta-row:last-child { border-bottom: none; }
.info-meta-label { color: var(--muted); flex-shrink: 0; }
.info-meta-value { text-align: right; overflow-wrap: anywhere; }

.info-hl-empty { color: var(--muted); font-size: 0.83em; padding: 1.2em; text-align: center; }
.info-hl-item { border: 1px solid var(--rule); border-radius: 8px; padding: 0.7em; margin-bottom: 0.8em; }
.info-hl-quote {
  border-left: 3px solid var(--hl-color, #ffeb3b);
  padding-left: 0.6em; font-size: 0.85em; line-height: 1.45; margin-bottom: 0.5em;
  color: var(--fg);
  overflow-x: auto; max-width: 100%;
}
.info-hl-quote math { font-size: 1em; }
.info-hl-note {
  width: 100%; border: 1px solid var(--rule); border-radius: 6px;
  background: var(--bg); color: var(--fg); font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  font-size: 0.82em; padding: 0.5em; resize: vertical; min-height: 2.6em;
}
.info-hl-note[readonly] { background: transparent; border-color: transparent; padding: 0; resize: none; }/* ------------------------------------------------------------ drop overlay */
.drop-overlay {
  position: fixed; inset: 0; background: rgba(26, 86, 219, 0.08);
  border: 3px dashed var(--accent); z-index: 999;
  display: flex; align-items: center; justify-content: center; pointer-events: none;
  animation: overlayFadeIn 0.15s ease;
}
.drop-overlay[hidden] { display: none; }
.drop-overlay-card {
  background: var(--card-bg); border: 1px solid var(--rule); border-radius: 14px; padding: 2.2em 3em;
  text-align: center; font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
  animation: overlayCardIn 0.18s ease;
}
.drop-overlay-card strong { display: block; font-size: 1.2em; margin-bottom: 0.4em; color: var(--fg); }
.drop-overlay-card div { color: var(--muted); font-size: 0.88em; }

/* ------------------------------------------------------------ confirm dialog */
.confirm-overlay {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.4); z-index: 1000;
  display: flex; align-items: center; justify-content: center;
  animation: overlayFadeIn 0.15s ease;
}
.confirm-overlay[hidden] { display: none; }
.confirm-card {
  background: var(--card-bg); border: 1px solid var(--rule); border-radius: 14px;
  padding: 1.6em 1.8em; max-width: 360px; width: calc(100% - 2.4em);
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.24);
  animation: overlayCardIn 0.18s ease;
}
.confirm-card strong { display: block; font-size: 1.05em; margin-bottom: 0.5em; color: var(--fg); }
.confirm-card p { color: var(--muted); font-size: 0.86em; line-height: 1.5; margin: 0 0 1.4em; overflow-wrap: anywhere; }
.confirm-card-actions { display: flex; justify-content: flex-end; gap: 0.6em; }
.confirm-card-actions button {
  display: flex; align-items: center; justify-content: center; gap: 0.4em; border: none; border-radius: 8px; padding: 0.55em 1.1em; font-size: 0.85em; font-weight: 600; cursor: pointer;
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  transition: opacity 0.15s ease;
}
.confirm-cancel-btn { background: var(--rule); color: var(--fg); }
.confirm-cancel-btn:hover { opacity: 0.8; }
.confirm-delete-btn { background: var(--error); color: #fff; }
.confirm-delete-btn:hover { opacity: 0.85; }

/* --------------------------------------------------------------- undo toasts */
.undo-toast-container {
  position: fixed; left: 1.2em; bottom: 1.2em; z-index: 200;
  display: flex; flex-direction: column-reverse; gap: 0.6em;
  pointer-events: none;
}
.undo-toast {
  pointer-events: auto;
  background: var(--fg); color: var(--bg);
  border-radius: 10px; padding: 0.75em 0.6em 0.75em 1em;
  min-width: 260px; max-width: 340px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
  display: flex; align-items: center; gap: 0.6em;
  position: relative; overflow: hidden;
  animation: undoToastIn 0.2s ease;
}
.undo-toast.leaving { animation: undoToastOut 0.15s ease forwards; }
.undo-toast-msg {
  flex: 1; min-width: 0; font-size: 0.88em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.undo-toast-btn {
  flex-shrink: 0; border: none; background: none; color: inherit; cursor: pointer;
  font-weight: 700; font-size: 0.85em; padding: 0.35em 0.6em; border-radius: 6px;
  text-decoration: underline; text-underline-offset: 2px;
}
.undo-toast-btn:hover { background: color-mix(in srgb, var(--bg) 15%, transparent); }
.undo-toast-bar {
  position: absolute; left: 0; right: 0; bottom: 0; height: 3px;
  background: color-mix(in srgb, var(--bg) 22%, transparent);
}
.undo-toast-bar-fill {
  height: 100%; width: 100%; background: var(--bg);
  transform-origin: left; transform: scaleX(1);
}
@keyframes undoToastIn { from { opacity: 0; transform: translateY(8px) scale(0.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
@keyframes undoToastOut { from { opacity: 1; transform: translateY(0); } to { opacity: 0; transform: translateY(6px); } }

/* plain (non-undo) notices, e.g. upload progress/result -- same toast
   stack, just full-width wrapping text instead of a truncated row with
   an action button */
.undo-toast.notice-toast { align-items: flex-start; }
.undo-toast.notice-toast.error { background: #b3261e; color: #fff; }
.undo-toast.notice-toast a { color: inherit; font-weight: 700; text-decoration: underline; text-underline-offset: 2px; }
.undo-toast-spinner {
  flex-shrink: 0; display: inline-block; width: 0.9em; height: 0.9em;
  border: 2px solid color-mix(in srgb, var(--bg) 30%, transparent); border-top-color: var(--bg);
  border-radius: 50%; animation: spin 0.7s linear infinite;
}

/* -------------------------------------------------------- pull to refresh */
.pull-refresh-indicator {
  position: fixed; top: 0.9em; left: 50%; transform: translateX(-50%) translateY(-6px);
  z-index: 300; display: flex; align-items: center; gap: 0.5em;
  background: var(--card-bg); border: 1px solid var(--rule); border-radius: 999px;
  padding: 0.45em 1em; font-size: 0.82em; color: var(--muted);
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.14);
  opacity: 0; pointer-events: none;
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.pull-refresh-indicator[hidden] { display: none; }
.pull-refresh-indicator.visible { opacity: 1; transform: translateX(-50%) translateY(0); }
.pull-refresh-arrow {
  width: 14px; height: 14px; flex-shrink: 0; color: var(--muted);
  transition: transform 0.12s ease;
}
.pull-refresh-arrow[hidden] { display: none; }
.pull-refresh-spinner {
  width: 0.9em; height: 0.9em; border: 2px solid var(--rule); border-top-color: var(--accent);
  border-radius: 50%; flex-shrink: 0;
}
.pull-refresh-spinner[hidden] { display: none; }
.pull-refresh-spinner.spinning { animation: spin 0.6s linear infinite; }

@media (max-width: 1000px) { .info-panel { display: none !important; } }
@media (max-width: 720px) { .sidebar { display: none; } }
</style>
</head>
<body>
<div class="app-shell">
  <aside class="sidebar">
    <div class="sidebar-top">
      <span class="brand">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/></svg>
        Andrew&rsquo;s Paper Library
      </span>
      <button type="button" class="icon-btn sidebar-add-btn" id="addPaperBtn" aria-label="Add a paper" title="Add a paper (LaTeX source or saved HTML page)">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="12" y1="5" x2="12" y2="19"></line>
          <line x1="5" y1="12" x2="19" y2="12"></line>
        </svg>
      </button>
    </div>
    <nav class="sidebar-nav">
      <button type="button" class="nav-item" id="navHome">Home</button>
      <div class="nav-section-label" id="navPinnedLabel" hidden>Pinned</div>
      <div class="nav-tags" id="navPinned" hidden></div>
      <div class="nav-section-label">Tags</div>
      <div class="nav-tags" id="navTags"></div>
    </nav>
    <div class="sidebar-bottom">
      <button type="button" class="nav-item" id="navSearchBtn" title="Search papers (/)">Search</button>
      <button type="button" class="nav-item" id="navPrefsBtn">Preferences <span class="nav-item-sub" id="prefsThemeLabel">Auto</span></button>
      <div class="prefs-popover" id="prefsPopover" hidden>
        <div class="prefs-popover-label">Theme</div>
        <div class="prefs-theme-grid" id="prefsThemeGrid">
          <button type="button" data-value="auto">Auto</button>
          <button type="button" data-value="light">Light</button>
          <button type="button" data-value="dark">Dark</button>
        </div>
        <div class="prefs-popover-label">Navigation</div>
        <div class="prefs-popover-row">
          <span>Vim keys</span>
          <label class="prefs-switch">
            <input type="checkbox" id="prefsVimNavToggle">
            <span class="prefs-switch-track"></span>
          </label>
        </div>
        <div class="prefs-popover-row" style="margin-top: 0.4em;">
          <span>Palette Key</span>
          <input type="text" id="prefsPaletteKey" class="prefs-input" style="width: 40px; text-align: center; text-transform: lowercase;" maxlength="1">
        </div>
        <div class="prefs-popover-label" style="margin-top: 0.6em;">Sync</div>
        <div class="prefs-popover-row" style="flex-direction: column; align-items: stretch; gap: 0.4em; padding-bottom: 0.2em;">
          <input type="text" id="prefsGitUrl" placeholder="git@github.com:user/repo.git" class="prefs-input">
          <div style="display: flex; gap: 0.4em;">
            <button type="button" class="prefs-btn" id="prefsGitSetupBtn">Setup</button>
            <button type="button" class="prefs-btn" id="prefsGitSyncBtn">Sync Now</button>
          </div>
          <div id="prefsGitStatus" style="font-size: 0.75em; color: var(--muted); text-align: center; margin-top: 0.2em;">Not configured</div>
        </div>
      </div>
      <div class="sidebar-footer">
        <a href="/pipeline">Pipeline</a>
        <span class="footer-sep">&middot;</span>
        <a href="/about">About</a>
        <span class="footer-sep">&middot;</span>
        <a href="https://github.com/andrewluoooo/paper-reader">GitHub</a>
      </div>
    </div>
  </aside>

  <main class="main-col">
    <div class="main-topbar">
      <div class="topbar-title">
        Library
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"></polyline></svg>
      </div>
      <div class="tabs-row" role="tablist">
        <button type="button" class="tab-btn" role="tab" data-status="inbox">Inbox</button>
        <button type="button" class="tab-btn" role="tab" data-status="later">Later</button>
        <button type="button" class="tab-btn" role="tab" data-status="completed">Completed</button>
        <button type="button" class="tab-btn" role="tab" data-status="archive">Archive</button>
        <button type="button" class="tab-btn" role="tab" data-status="trash">Trash</button>
      </div>
      <div class="sort-control">
        <svg class="sort-lead-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"></line><polyline points="6 13 12 19 18 13"></polyline></svg>
        <select id="sortSelect" class="sort-select" aria-label="Sort papers by">
          <option value="added">Most recently added</option>
          <option value="opened">Most recently opened</option>
          <option value="title">Title (A&ndash;Z)</option>
        </select>
      </div>
    </div>

    <div class="search-row" id="searchRow" hidden>
      <input type="text" id="searchBox" placeholder="Search papers by title or author...">
    </div>

    <div class="status" id="status"></div>

    <div class="paper-list" id="paperList"></div>
  </main>

  <aside class="info-panel" id="infoPanel" hidden>
    <div class="info-panel-top">
      <span>Notes & Info</span>
    </div>
    <div class="info-panel-body" id="infoPanelBody"></div>
  </aside>
</div>

<input type="file" id="fileInput" accept=".tex,.zip,.tar.gz,.tgz,.tar,.html,.htm,.pdf">

<div class="drop-overlay" id="dropOverlay" hidden>
  <div class="drop-overlay-card">
    <strong>Drop to add to your library</strong>
    <div>.tex, .zip, .tar.gz, .tgz, .pdf &mdash; or a saved .html paper page</div>
  </div>
</div>

<div class="confirm-overlay" id="confirmOverlay" hidden>
  <div class="confirm-card">
    <strong>Delete permanently?</strong>
    <p id="confirmMessage"></p>
    <div class="confirm-card-actions">
      <button type="button" class="confirm-cancel-btn" id="confirmCancelBtn">Cancel</button>
      <button type="button" class="confirm-delete-btn" id="confirmDeleteBtn">Delete</button>
    </div>
  </div>
</div>

<div class="undo-toast-container" id="undoToasts"></div>

<div class="pull-refresh-indicator" id="pullRefreshIndicator" hidden>
  <svg class="pull-refresh-arrow" id="pullRefreshArrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>
  <span class="pull-refresh-spinner" id="pullRefreshSpinner" hidden></span>
  <span id="pullRefreshLabel">Pull to refresh</span>
</div>

<script>
(function () {
  var papers = [];
  var MORE_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="5" cy="12" r="1.5"></circle><circle cx="12" cy="12" r="1.5"></circle><circle cx="19" cy="12" r="1.5"></circle></svg>';
  var INBOX_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"></polyline>' +
    '<path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"></path></svg>';
  var LATER_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>';
  var ARCHIVE_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<polyline points="21 8 21 21 3 21 3 8"></polyline><rect x="1" y="3" width="22" height="5"></rect>' +
    '<line x1="10" y1="12" x2="14" y2="12"></line></svg>';
  var TRASH_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<polyline points="3 6 5 6 21 6"></polyline>' +
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>' +
    '<path d="M10 11v6"></path><path d="M14 11v6"></path>' +
    '<path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"></path></svg>';
  var BOOK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"></path>' +
    '<path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"></path></svg>';
  var HOME_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path><polyline points="9 22 9 12 15 12 15 22"></polyline></svg>';
  var SEARCH_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>';
  var PREFS_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>';
  var TAG_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="4" y1="9" x2="20" y2="9"></line><line x1="4" y1="15" x2="20" y2="15"></line><line x1="10" y1="3" x2="8" y2="21"></line><line x1="16" y1="3" x2="14" y2="21"></line></svg>';
  var X_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
  
  var PIN_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 11.2V6a3 3 0 0 0-6 0v5.2a2 2 0 0 1-1.11 1.35l-1.78.9A2 2 0 0 0 5 15.24Z"></path></svg>';
  var CHECK_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"></polyline></svg>';

  var STATUS_ACTIONS = [
    { status: "inbox", icon: INBOX_ICON, label: "Move to Inbox" },
    { status: "later", icon: LATER_ICON, label: "Move to Later" },
    { status: "archive", icon: ARCHIVE_ICON, label: "Move to Archive", shortcut: "A" }
  ];
  var TAB_EMPTY_MESSAGES = {
    inbox: "Nothing in your inbox.",
    later: "Nothing saved for later.",
    archive: "Nothing archived yet.",
    trash: "Trash is empty."
  };
  var THUMB_GRADIENTS = [
    ["#f97316", "#ef4444"], ["#8b5cf6", "#6366f1"], ["#06b6d4", "#3b82f6"],
    ["#f43f5e", "#ec4899"], ["#22c55e", "#0ea5e9"], ["#eab308", "#f97316"],
    ["#a855f7", "#d946ef"], ["#14b8a6", "#22c55e"]
  ];

  function hashStr(s) {
    var h = 0;
    for (var i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) | 0; }
    return Math.abs(h);
  }
  function thumbGradient(seed) {
    var pair = THUMB_GRADIENTS[hashStr(seed) % THUMB_GRADIENTS.length];
    return "linear-gradient(135deg, " + pair[0] + ", " + pair[1] + ")";
  }

  var SETTINGS_KEY = "paper_reader_settings";
  var THEME_LABELS = { auto: "Auto", light: "Light", dark: "Dark" };

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
    var label = document.getElementById("prefsThemeLabel");
    if (label) label.textContent = THEME_LABELS[theme] || "Auto";
  }
  function initTheme() {
    applyTheme(loadSettings().theme || "auto");
    var btn = document.getElementById("navPrefsBtn");
    var pop = document.getElementById("prefsPopover");
    var themeGrid = document.getElementById("prefsThemeGrid");
    var vimToggle = document.getElementById("prefsVimNavToggle");
    if (!btn || !pop) return;

    function setActiveTheme(val) {
      if (!themeGrid) return;
      themeGrid.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("active", b.dataset.value === val);
      });
    }
    setActiveTheme(loadSettings().theme || "auto");

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      pop.hidden = !pop.hidden;
    });
    document.addEventListener("click", function (e) {
      if (!pop.hidden && !pop.contains(e.target) && e.target !== btn) pop.hidden = true;
    });

    if (themeGrid) {
      themeGrid.querySelectorAll("button").forEach(function (b) {
        b.addEventListener("click", function () {
          var val = b.dataset.value;
          saveSettings({ theme: val });
          applyTheme(val);
          setActiveTheme(val);
        });
      });
    }

    // Shares the same "paper_reader_settings" localStorage key/field the
    // reader page's own Aa-popover vim toggle uses -- flipping it here
    // takes effect the moment a paper is opened, no separate wiring needed.
    if (vimToggle) {
      vimToggle.checked = !!loadSettings().vimNav;
      vimToggle.addEventListener("change", function () {
        saveSettings({ vimNav: vimToggle.checked });
      });
    }

    var gitUrlInput = document.getElementById("prefsGitUrl");
    var gitSetupBtn = document.getElementById("prefsGitSetupBtn");
    var gitSyncBtn = document.getElementById("prefsGitSyncBtn");
    var gitStatus = document.getElementById("prefsGitStatus");
    if (gitUrlInput) {
      var initialUrl = loadSettings().gitUrl;
      gitUrlInput.value = initialUrl || "";
      if (initialUrl) {
        gitStatus.textContent = "Ready to sync.";
      }
      gitUrlInput.addEventListener("input", function() {
        saveSettings({ gitUrl: gitUrlInput.value });
      });
      gitSetupBtn.addEventListener("click", function() {
        if (!gitUrlInput.value) { gitStatus.textContent = "Please enter a URL first."; return; }
        gitStatus.textContent = "Setting up...";
        fetch("/api/git/setup", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: gitUrlInput.value })
        }).then(function(r) { return r.json(); }).then(function(res) {
          if (res.ok) gitStatus.textContent = "Setup complete! Ready to sync.";
          else gitStatus.textContent = "Setup failed: " + (res.error || "Unknown error");
        }).catch(function() { gitStatus.textContent = "Network error during setup."; });
      });
      gitSyncBtn.addEventListener("click", function() {
        gitStatus.textContent = "Syncing...";
        fetch("/api/git/sync", { method: "POST" })
        .then(function(r) { return r.json(); }).then(function(res) {
          if (res.ok) { gitStatus.textContent = "Synced successfully."; loadPapers(); }
          else gitStatus.textContent = "Sync failed: " + (res.error || "Unknown error");
        }).catch(function() { gitStatus.textContent = "Network error during sync."; });
      });
    }

    var pk = document.getElementById("prefsPaletteKey");
    if (pk) {
      pk.value = loadSettings().paletteShortcut || "p";
      pk.addEventListener("input", function() {
        var v = pk.value.toLowerCase().trim() || "p";
        saveSettings({ paletteShortcut: v });
      });
    }
  }

  function fmtDate(ts) {
    var d = new Date(ts * 1000);
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  var activeTags = [];
  var sortBy = loadSettings().sortBy || "added";
  var currentTab = loadSettings().tab || "inbox";
  var selectedPaper = null;
  var searchOpen = false;
  var hoveredPaperId = null; // whichever card the mouse is over -- target of the a/d shortcuts below

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

  function renderSidebarTags() {
    var wrap = document.getElementById("navTags");
    var tags = allTags();
    // drop any active filter tags that no longer exist on any paper
    activeTags = activeTags.filter(function (t) { return tags.indexOf(t) !== -1; });
    wrap.innerHTML = "";
    if (!tags.length) {
      var empty = document.createElement("div");
      empty.className = "nav-tags-empty";
      empty.textContent = "No tags yet";
      wrap.appendChild(empty);
      return;
    }
    tags.forEach(function (t) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "nav-tag-item" + (activeTags.indexOf(t) !== -1 ? " active" : "");
      item.innerHTML = TAG_ICON + "<span>" + t + "</span>";
      item.title = t;
      item.addEventListener("click", function () {
        var idx = activeTags.indexOf(t);
        if (idx === -1) activeTags.push(t); else activeTags.splice(idx, 1);
        renderSidebarTags();
      renderSidebarPinned();
        render(document.getElementById("searchBox").value);
      });
      wrap.appendChild(item);
    });
  }


  function renderSidebarPinned() {
    var wrap = document.getElementById("navPinned");
    var label = document.getElementById("navPinnedLabel");
    if (!wrap || !label) return;
    
    var pinnedPapers = papers.filter(function(p) { return p.pinned; });
    wrap.hidden = false;
    label.hidden = false;
    
    wrap.innerHTML = "";
    if (pinnedPapers.length > 0) {
      pinnedPapers.forEach(function(p) {
        var item = document.createElement("a");
        item.className = "nav-tag-item";
        item.href = "/library/" + encodeURIComponent(p.id) + ".html";
        item.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:0.4em"><path d="m21 16-5.5-5.5"></path><path d="M15.5 10.5 12 7l-5.5 5.5"></path><path d="m3 21 6-6"></path></svg><span>' + escHtml(p.title || "Untitled") + '</span>';
        wrap.appendChild(item);
      });
    } else {
      wrap.innerHTML = '<div class="nav-pinned-empty">Drop papers here to pin</div>';
    }
  }

  function updatePaper(p, updates) {
    fetch("/api/papers/" + encodeURIComponent(p.id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates)
    }).then(function (r) {
      if (r.ok) loadPapers();
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
        renderSidebarTags();
      renderSidebarPinned();
        render(document.getElementById("searchBox").value);
        if (selectedPaper && selectedPaper.id === p.id) renderInfoPanel(p);
      })
      .catch(function (e) { setStatus("Could not update tags: " + e.message, "error"); });
  }

  /* ------------------------------------------------------------ undo toasts */
  // Delete and archive are both easy to trigger by accident, so instead of
  // a blocking confirm() they apply optimistically and give the user a
  // 5s window (with a visible countdown bar) to undo before the change
  // actually hits the server. Multiple toasts can stack; each tracks its
  // own timer under a stable key so a second action on the same paper
  // flushes (commits) whatever was already pending first, rather than
  // leaving two conflicting timers racing each other.
  var pendingActions = {};
  var UNDO_WINDOW_MS = 5000;

  function flushPendingAction(key) {
    var pending = pendingActions[key];
    if (!pending) return;
    clearTimeout(pending.timer);
    if (pending.toast && pending.toast.parentNode) pending.toast.remove();
    delete pendingActions[key];
    pending.onCommit();
  }

  // Runs any still-pending undo actions immediately rather than losing
  // them if the user navigates away (opens a paper, closes the tab) while
  // a countdown is still running.
  function flushAllPendingActions() {
    Object.keys(pendingActions).forEach(flushPendingAction);
  }
  window.addEventListener("pagehide", flushAllPendingActions);
  window.addEventListener("beforeunload", flushAllPendingActions);

  function showUndoToast(key, message, onCommit, onUndo) {
    flushPendingAction(key);

    var container = document.getElementById("undoToasts");
    var toast = document.createElement("div");
    toast.className = "undo-toast";

    var msg = document.createElement("div");
    msg.className = "undo-toast-msg";
    msg.textContent = message;
    msg.title = message;
    toast.appendChild(msg);

    var undoBtn = document.createElement("button");
    undoBtn.type = "button";
    undoBtn.className = "undo-toast-btn";
    undoBtn.textContent = "Undo";
    toast.appendChild(undoBtn);

    var bar = document.createElement("div");
    bar.className = "undo-toast-bar";
    var fill = document.createElement("div");
    fill.className = "undo-toast-bar-fill";
    bar.appendChild(fill);
    toast.appendChild(bar);

    container.appendChild(toast);
    // Kick the fill's shrink animation off on the next frame so the
    // initial full-width state actually paints first.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        fill.style.transition = "transform " + UNDO_WINDOW_MS + "ms linear";
        fill.style.transform = "scaleX(0)";
      });
    });

    var timer = setTimeout(function () {
      delete pendingActions[key];
      toast.classList.add("leaving");
      setTimeout(function () { toast.remove(); }, 150);
      onCommit();
    }, UNDO_WINDOW_MS);

    undoBtn.addEventListener("click", function () {
      clearTimeout(timer);
      delete pendingActions[key];
      toast.remove();
      onUndo();
    });

    pendingActions[key] = { timer: timer, toast: toast, onCommit: onCommit };
  }

  // A plain (no undo, no countdown) notice in the same toast stack --
  // used for upload progress/result. Returns the toast element so the
  // caller can update it in place (e.g. loading -> success) instead of
  // stacking a separate toast for each step of one upload.
  function showNoticeToast(html, cls, autoHideMs, existing) {
    var container = document.getElementById("undoToasts");
    var toast = existing || document.createElement("div");
    toast.className = "undo-toast notice-toast" + (cls ? " " + cls : "");
    toast.innerHTML = html;
    if (!existing) container.appendChild(toast);
    if (toast._hideTimer) { clearTimeout(toast._hideTimer); toast._hideTimer = null; }
    if (autoHideMs) {
      toast._hideTimer = setTimeout(function () {
        toast.classList.add("leaving");
        setTimeout(function () { toast.remove(); }, 150);
      }, autoHideMs);
    }
    return toast;
  }

  function commitPaperStatus(p, status) {
    fetch("/api/papers/" + encodeURIComponent(p.id) + "/status", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: status })
    })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (!res.ok) setStatus("Could not move paper: " + (res.data.error || "unknown error"), "error");
      })
      .catch(function (e) { setStatus("Could not move paper: " + e.message, "error"); });
  }

  // Archiving/deleting a card slides it sideways and fades it out, then
  // collapses the space it took up (height/margin/padding -> 0) so the
  // cards below smoothly flow up into place -- ordinary CSS layout
  // animation, no per-sibling bookkeeping needed. `onDone` fires once the
  // card has fully collapsed and is where the caller should actually
  // mutate state + re-render; if the card element can't be found (already
  // gone, or this status change doesn't remove it from view) it fires
  // immediately so callers don't need a separate "no animation" branch.
  var CARD_SLIDE_MS = 200;
  var CARD_COLLAPSE_MS = 220;
  function animateCardExit(paperId, onDone) {
    var card = document.querySelector('.paper-card[data-paper-id="' + paperId + '"]');
    if (!card) { onDone(); return; }
    card.style.pointerEvents = "none";
    card.style.transition = "transform " + CARD_SLIDE_MS + "ms ease, opacity " + CARD_SLIDE_MS + "ms ease";
    card.style.transform = "translateX(40px)";
    card.style.opacity = "0";

    var collapsed = false;
    function collapse() {
      if (collapsed) return;
      collapsed = true;
      var cs = getComputedStyle(card);
      card.style.height = card.getBoundingClientRect().height + "px";
      card.style.marginBottom = cs.marginBottom;
      card.style.paddingTop = cs.paddingTop;
      card.style.paddingBottom = cs.paddingBottom;
      card.style.overflow = "hidden";
      card.getBoundingClientRect(); // force reflow so the transition below starts from these values
      card.style.transition = [
        "height " + CARD_COLLAPSE_MS + "ms ease",
        "margin-bottom " + CARD_COLLAPSE_MS + "ms ease",
        "padding-top " + CARD_COLLAPSE_MS + "ms ease",
        "padding-bottom " + CARD_COLLAPSE_MS + "ms ease",
        "border-width " + CARD_COLLAPSE_MS + "ms ease"
      ].join(", ");
      requestAnimationFrame(function () {
        card.style.height = "0px";
        card.style.marginBottom = "0px";
        card.style.paddingTop = "0px";
        card.style.paddingBottom = "0px";
        card.style.borderWidth = "0px";
      });
      var finished = false;
      function finish() {
        if (finished) return;
        finished = true;
        card.removeEventListener("transitionend", finish);
        onDone();
      }
      card.addEventListener("transitionend", finish);
      setTimeout(finish, CARD_COLLAPSE_MS + 80); // fallback in case transitionend is missed
    }
    card.addEventListener("transitionend", collapse, { once: true });
    setTimeout(collapse, CARD_SLIDE_MS + 40); // fallback
  }

  // Archive and trash are both "soft" transitions worth a second chance
  // (unlike moving to inbox/later, which is easy to immediately undo by
  // just moving it back) -- both go through the same undo-toast path,
  // just with their own verb in the toast message.
  var UNDO_TOAST_VERBS = { archive: "Archived", trash: "Moved to trash" };

  function updatePaperStatus(p, status) {
    var prevStatus = p.status || "inbox";
    if (status === prevStatus) return;
    var leavesView = status !== currentTab;
    var verb = UNDO_TOAST_VERBS[status];
    if (!verb) {
      function applyStatus() {
        commitPaperStatus(p, status);
        p.status = status;
        render(document.getElementById("searchBox").value);
        if (selectedPaper && selectedPaper.id === p.id) renderInfoPanel(p);
      }
      if (leavesView) animateCardExit(p.id, applyStatus); else applyStatus();
      return;
    }
    var undone = false;
    function applyChange() {
      if (undone) return;
      p.status = status;
      render(document.getElementById("searchBox").value);
      if (selectedPaper && selectedPaper.id === p.id) renderInfoPanel(p);
    }
    if (leavesView) animateCardExit(p.id, applyChange); else applyChange();
    showUndoToast(
      "status:" + p.id,
      verb + ' "' + p.title + '"',
      function () { commitPaperStatus(p, status); },
      function () {
        undone = true;
        p.status = prevStatus;
        render(document.getElementById("searchBox").value);
        if (selectedPaper && selectedPaper.id === p.id) renderInfoPanel(p);
      }
    );
  }

  function buildTagsEditor(p) {
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
    return tagsWrap;
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
      else if (selectedPaper && selectedPaper.id === p.id) renderInfoPanel(p);
    }
    input.addEventListener("keydown", function (e) {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      else if (e.key === "Escape") { if (selectedPaper && selectedPaper.id === p.id) renderInfoPanel(p); }
    });
    input.addEventListener("blur", commit);
    input.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); });
  }

  function openInfoPanel(p) {
    selectedPaper = p;
    document.getElementById("infoPanel").hidden = false;
    renderInfoPanel(p);
  }
  function closeInfoPanel() {
    selectedPaper = null;
    document.getElementById("infoPanel").hidden = true;
  }

  var openMoreWrap = null;
  function closeMoreMenu() {
    if (openMoreWrap) {
      var existing = openMoreWrap.querySelector(".more-menu");
      if (existing) existing.remove();
      openMoreWrap = null;
    }
  }
  function toggleMoreMenu(wrap, p) {
    if (openMoreWrap === wrap) { closeMoreMenu(); return; }
    closeMoreMenu();
    var menu = document.createElement("div");
    menu.className = "more-menu";
    var infoItem = document.createElement("button");
    infoItem.type = "button";
    infoItem.textContent = "Show info";
    infoItem.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      closeMoreMenu();
      if (selectedPaper && selectedPaper.id === p.id) closeInfoPanel();
      else openInfoPanel(p);
      render(document.getElementById("searchBox").value);
    });
    var removeItem = document.createElement("button");
    removeItem.type = "button";
    removeItem.className = "danger";
    removeItem.textContent = currentTab === "trash" ? "Delete permanently" : "Move to Trash";
    removeItem.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      closeMoreMenu();
      deletePaperFromCard(p);
    });

    menu.appendChild(removeItem);
    wrap.appendChild(menu);
    openMoreWrap = wrap;
  }
  document.addEventListener("click", function (e) {
    if (openMoreWrap && !openMoreWrap.contains(e.target)) closeMoreMenu();
  });

  function renderInfoPanel(p) {
    var body = document.getElementById("infoPanelBody");
    body.innerHTML = "";

    var titleEl = document.createElement("div");
    titleEl.className = "info-title";
    titleEl.textContent = p.title;
    body.appendChild(titleEl);

    var openLink = document.createElement("a");
    openLink.className = "info-open-link";
    openLink.href = "/library/" + encodeURIComponent(p.id) + ".html";
    openLink.textContent = "Open paper \\u2192";
    body.appendChild(openLink);

    if ((p.authors || []).length) {
      var authorsRow = document.createElement("div");
      authorsRow.className = "info-authors-row";
      var avatar = document.createElement("div");
      avatar.className = "info-avatar";
      avatar.style.background = thumbGradient(p.id);
      avatar.textContent = p.title.trim().charAt(0).toUpperCase();
      var name = document.createElement("div");
      name.textContent = p.authors.join(", ");
      authorsRow.appendChild(avatar);
      authorsRow.appendChild(name);
      body.appendChild(authorsRow);
    }

    if (p.summary) {
      var sumLabel = document.createElement("div");
      sumLabel.className = "info-section-label";
      sumLabel.textContent = "Summary";
      var sumBody = document.createElement("div");
      sumBody.className = "info-summary";
      sumBody.textContent = p.summary;
      body.appendChild(sumLabel);
      body.appendChild(sumBody);
    }

    var metaLabel = document.createElement("div");
    metaLabel.className = "info-section-label";
    metaLabel.textContent = "Metadata";
    body.appendChild(metaLabel);
    var metaTable = document.createElement("div");
    metaTable.className = "info-meta-table";
    function metaRow(label, value) {
      if (!value) return;
      var row = document.createElement("div");
      row.className = "info-meta-row";
      var l = document.createElement("span");
      l.className = "info-meta-label";
      l.textContent = label;
      var v = document.createElement("span");
      v.className = "info-meta-value";
      v.textContent = value;
      row.appendChild(l);
      row.appendChild(v);
      metaTable.appendChild(row);
    }
    var srcLower = (p.sourceFilename || "").toLowerCase();
    var typeLabel = (srcLower.slice(-5) === ".html" || srcLower.slice(-4) === ".htm") ? "HTML import" : "LaTeX source";
    metaRow("Type", typeLabel);
    metaRow("Venue", p.venue);
    metaRow("Added", fmtDate(p.addedAt));
    metaRow("Last opened", p.lastOpenedAt ? fmtDate(p.lastOpenedAt) : "Never");
    metaRow("Source file", p.sourceFilename);
    body.appendChild(metaTable);

    var tagsLabel = document.createElement("div");
    tagsLabel.className = "info-section-label";
    tagsLabel.textContent = "Tags";
    body.appendChild(tagsLabel);
    body.appendChild(buildTagsEditor(p));

    var hlLabel = document.createElement("div");
    hlLabel.className = "info-section-label";
    hlLabel.style.marginTop = "1.5em";
    hlLabel.textContent = "Highlights";
    body.appendChild(hlLabel);

    var hlKey = "paper_reader_highlights::" + encodeURIComponent(p.title || "");
    var highlights = [];
    try { highlights = JSON.parse(localStorage.getItem(hlKey) || "[]"); } catch (e) {}

    if (!highlights.length) {
      var empty = document.createElement("div");
      empty.className = "info-hl-empty";
      empty.textContent = "No highlights for this paper.";
      body.appendChild(empty);
    } else {
      var HL_COLORS = { yellow: "#ffeb3b", green: "#8bc34a", blue: "#03a9f4", purple: "#9c27b0", pink: "#e91e63" };
      highlights.forEach(function (h) {
        var item = document.createElement("div");
        item.className = "info-hl-item";

        var quote = document.createElement("div");
        quote.className = "info-hl-quote";
        quote.style.setProperty("--hl-color", HL_COLORS[h.color] || HL_COLORS.yellow);
        if (h.html) quote.innerHTML = h.html;
        else quote.textContent = h.text || "";
        item.appendChild(quote);

        if (h.note) {
          var note = document.createElement("textarea");
          note.className = "info-hl-note";
          note.readOnly = true;
          note.value = h.note;
          item.appendChild(note);
        }

        body.appendChild(item);
      });
    }
  }

  function render(filter) {
    var list = document.getElementById("paperList");
    var q = (filter || "").trim().toLowerCase();

    var filtered = papers.filter(function (p) {
      if (currentTab === "completed") {
        if (!p.completed || p.status === "trash") return false;
      } else {
        if ((p.status || "inbox") !== currentTab) return false;
        if (p.completed && (currentTab === "inbox" || currentTab === "later")) return false;
      }
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
      var emptyMsg;
      if (!papers.length) emptyMsg = "No papers yet \u2014 drop a LaTeX source or saved HTML paper page anywhere on this page to get started.";
      else if (q || activeTags.length) emptyMsg = "No matching papers.";
      else emptyMsg = TAB_EMPTY_MESSAGES[currentTab] || "Nothing here yet.";
      list.innerHTML = '<div class="empty-state">' + emptyMsg + "</div>";
      closeInfoPanel();
      return;
    }
    
    if (!selectedPaper || !filtered.some(function (p) { return p.id === selectedPaper.id; })) {
      openInfoPanel(filtered[0]);
    }

    list.innerHTML = "";
    openMoreWrap = null;
    filtered.forEach(function (p) {
      var card = document.createElement("div");
      card.className = "paper-card" + (selectedPaper && selectedPaper.id === p.id ? " selected" : "") + (p.completed ? " completed" : "");
      card.dataset.paperId = p.id;
      card.draggable = true;
      card.addEventListener("dragstart", function(e) {
        e.dataTransfer.setData("application/x-paper-id", p.id);
        e.dataTransfer.effectAllowed = "move";
        
        var rect = card.getBoundingClientRect();
        e.dataTransfer.setDragImage(card, e.clientX - rect.left, e.clientY - rect.top);
        
        setTimeout(function() { card.classList.add("dragging"); }, 0);
      });
      card.addEventListener("dragend", function(e) {
        card.classList.remove("dragging");
      });
      card.addEventListener("mouseenter", function () { hoveredPaperId = p.id; openInfoPanel(p); });
      card.addEventListener("mouseleave", function () { if (hoveredPaperId === p.id) hoveredPaperId = null; });

      var thumb = document.createElement("div");
      thumb.className = "paper-thumb";
      thumb.style.background = thumbGradient(p.id);
      thumb.textContent = (p.title || "?").trim().charAt(0).toUpperCase();
      card.appendChild(thumb);

      var a = document.createElement("a");
      a.className = "paper-card-link";
      a.href = "/library/" + encodeURIComponent(p.id) + ".html";
      a.draggable = false;

      
      var titleEl = document.createElement("div");
      titleEl.className = "paper-title";
      if (p.completed) {
        var checkEl = document.createElement("span");
        checkEl.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="color: var(--accent); margin-right: 5px; vertical-align: -2px;"><polyline points="20 6 9 17 4 12"></polyline></svg>';
        titleEl.appendChild(checkEl);
      }
      titleEl.appendChild(document.createTextNode(p.title));
      a.appendChild(titleEl);


      if (p.summary) {
        var summaryEl = document.createElement("div");
        summaryEl.className = "paper-summary";
        summaryEl.textContent = p.summary;
        a.appendChild(summaryEl);
      }

      var metaEl = document.createElement("div");
      metaEl.className = "paper-meta";
      var iconSpan = document.createElement("span");
      iconSpan.innerHTML = BOOK_ICON;
      metaEl.appendChild(iconSpan.firstChild);
      var metaText = document.createElement("span");
      var authors = (p.authors || []).join(", ");
      var dateLabel = (p.status === "trash" && p.deletedAt) ? ("Deleted " + fmtDate(p.deletedAt)) : fmtDate(p.addedAt);
      metaText.textContent = (authors ? authors + " \\u2022 " : "") + dateLabel;
      metaEl.appendChild(metaText);
      a.appendChild(metaEl);

      card.appendChild(a);

      var actions = document.createElement("div");
      actions.className = "paper-actions";

      var moreWrap = document.createElement("div");
      moreWrap.className = "more-wrap";
      var moreBtn = document.createElement("button");
      moreBtn.type = "button";
      moreBtn.className = "paper-action-btn";
      moreBtn.setAttribute("aria-label", "More actions");
      moreBtn.title = "More actions";
      moreBtn.innerHTML = MORE_ICON;
      moreBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        toggleMoreMenu(moreWrap, p);
      });
      moreWrap.appendChild(moreBtn);
      actions.appendChild(moreWrap);

      var completeBtn = document.createElement("button");
      completeBtn.type = "button";
      completeBtn.className = "paper-action-btn" + (p.completed ? " active" : "");
      completeBtn.setAttribute("aria-label", p.completed ? "Mark as unread" : "Mark as complete");
      completeBtn.title = p.completed ? "Mark as unread" : "Mark as complete";
      completeBtn.innerHTML = CHECK_ICON;
      completeBtn.addEventListener("click", function(e) {
        e.preventDefault(); e.stopPropagation();
        updatePaper(p, { completed: !p.completed });
      });
      actions.appendChild(completeBtn);
      
      var pinBtn2 = document.createElement("button");
      pinBtn2.type = "button";
      pinBtn2.className = "paper-action-btn pin-btn" + (p.pinned ? " active" : "");
      pinBtn2.setAttribute("aria-label", p.pinned ? "Unpin from sidebar" : "Pin to sidebar");
      pinBtn2.title = p.pinned ? "Unpin from sidebar" : "Pin to sidebar";
      pinBtn2.innerHTML = PIN_ICON;
      pinBtn2.addEventListener("click", function(e) {
        e.preventDefault(); e.stopPropagation();
        updatePaper(p, { pinned: !p.pinned });
      });
      actions.appendChild(pinBtn2);


      STATUS_ACTIONS.forEach(function (spec) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "paper-action-btn" + ((p.status || "inbox") === spec.status ? " active" : "");
        var label = spec.label + (spec.shortcut ? " (" + spec.shortcut + ")" : "");
        btn.setAttribute("aria-label", label);
        btn.title = label;
        btn.innerHTML = spec.icon;
        btn.addEventListener("click", function (e) {
          e.preventDefault();
          e.stopPropagation();
          updatePaperStatus(p, spec.status);
        });
        actions.appendChild(btn);
      });

      var deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "paper-action-btn danger-action";
      var deleteLabel = currentTab === "trash" ? "Delete permanently (D)" : "Delete (D)";
      deleteBtn.setAttribute("aria-label", deleteLabel);
      deleteBtn.title = deleteLabel;
      deleteBtn.innerHTML = TRASH_ICON;
      deleteBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        deletePaperFromCard(p);
      });
      actions.appendChild(deleteBtn);

      card.appendChild(actions);

      var pctStr = localStorage.getItem("paper_reader_pct::" + encodeURIComponent(p.title || ""));
      if (pctStr) {
        var pct = parseFloat(pctStr);
        if (pct > 0 && pct <= 1) {
          var progTrack = document.createElement("div");
          progTrack.className = "card-progress-track";
          var progFill = document.createElement("div");
          progFill.className = "card-progress-fill";
          progFill.style.width = (pct * 100) + "%";
          progTrack.appendChild(progFill);
          card.appendChild(progTrack);
        }
      }

      list.appendChild(card);
    });
  }

  // Small centered confirm dialog (matches the drop-overlay/more-menu
  // visual language) gating the one truly destructive action in this app --
  // permanent delete. Cancel, clicking the backdrop, or Escape all just
  // close it with no callback; only the Delete button runs onConfirm.
  var confirmOverlay = document.getElementById("confirmOverlay");
  var confirmMessageEl = document.getElementById("confirmMessage");
  var confirmCancelBtn = document.getElementById("confirmCancelBtn");
  var confirmDeleteBtn = document.getElementById("confirmDeleteBtn");
  var confirmActiveCallback = null;

  function closeConfirmDialog() {
    confirmOverlay.hidden = true;
    confirmActiveCallback = null;
  }
  confirmCancelBtn.addEventListener("click", closeConfirmDialog);
  confirmOverlay.addEventListener("click", function (e) {
    if (e.target === confirmOverlay) closeConfirmDialog();
  });
  confirmDeleteBtn.addEventListener("click", function () {
    var cb = confirmActiveCallback;
    closeConfirmDialog();
    if (cb) cb();
  });

  function confirmPermanentDelete(p, onConfirm) {
    confirmMessageEl.textContent = 'Permanently delete "' + p.title + '"? This cannot be undone.';
    confirmActiveCallback = onConfirm;
    confirmOverlay.hidden = false;
    // Default focus on Cancel, not Delete, so a stray Enter press doesn't
    // confirm the destructive action.
    confirmCancelBtn.focus();
  }

  function commitPaperRemoval(p) {
    fetch("/api/papers/" + encodeURIComponent(p.id), { method: "DELETE" })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (!res.ok) setStatus("Could not remove paper: " + (res.data.error || "unknown error"), "error");
      })
      .catch(function (e) { setStatus("Could not remove paper: " + e.message, "error"); });
  }

  // The trash can's own "delete permanently" action -- unlike
  // updatePaperStatus(p, "trash"), this actually removes the paper (via
  // commitPaperRemoval -> DELETE /api/papers/id, which unlinks its HTML
  // and raw source too) once the undo window lapses. Ordinary deletion
  // from inbox/later/archive should call updatePaperStatus(p, "trash")
  // instead, which is fully recoverable from the Trash tab.
  function permanentlyDeletePaper(p) {
    if (selectedPaper && selectedPaper.id === p.id) closeInfoPanel();
    // `removed` and `undone` independently track which of "the exit
    // animation finished" and "the user hit undo" happened first, since
    // either order is possible within the 5s undo window -- undo arriving
    // first just cancels the still-pending removal (p was never taken out
    // of `papers`), undo arriving after has to add p back in.
    var undone = false, removed = false;
    animateCardExit(p.id, function () {
      if (undone) return;
      removed = true;
      papers = papers.filter(function (x) { return x.id !== p.id; });
      renderSidebarTags();
      renderSidebarPinned();
      render(document.getElementById("searchBox").value);
    });
    showUndoToast(
      "delete:" + p.id,
      'Permanently deleted "' + p.title + '"',
      function () { commitPaperRemoval(p); },
      function () {
        undone = true;
        if (removed) papers.push(p);
        renderSidebarTags();
      renderSidebarPinned();
        render(document.getElementById("searchBox").value);
      }
    );
  }

  // What the trash-icon button and the "d" shortcut mean depends on
  // where you are: everywhere else it's a soft delete (recoverable from
  // the Trash tab), but from inside the Trash tab it's already trash --
  // there's nowhere further to move it, so it deletes for real.
  function deletePaperFromCard(p) {
    if (currentTab === "trash") {
      confirmPermanentDelete(p, function () { permanentlyDeletePaper(p); });
    } else {
      updatePaperStatus(p, "trash");
    }
  }

  function loadPapers() {
    fetch("/api/papers").then(function (r) { return r.json(); }).then(function (data) {
      papers = data;
      renderSidebarTags();
      renderSidebarPinned();
      if (selectedPaper) {
        var found = papers.filter(function (x) { return x.id === selectedPaper.id; })[0];
        if (found) { selectedPaper = found; renderInfoPanel(selectedPaper); }
        else closeInfoPanel();
      }
      render(document.getElementById("searchBox").value);
    });
  }

  /* -------------------------------------------------------- pull to refresh */
  // Mirrors iOS/macOS pull-to-refresh: pulling (scrolling up while already
  // at the top of the page) tips an arrow over as you approach the
  // threshold and swaps the label to "Release to refresh", but the reload
  // itself only fires once you actually let go -- not mid-pull. A mouse
  // wheel has no real "finger lifted" event, so "release" is inferred by
  // a short pause in upward wheel activity (debounced); if you stop short
  // of the threshold it just springs back with no refresh, same as the
  // native gesture.
  function initPullToRefresh() {
    var indicator = document.getElementById("pullRefreshIndicator");
    var label = document.getElementById("pullRefreshLabel");
    var arrow = document.getElementById("pullRefreshArrow");
    var spinner = document.getElementById("pullRefreshSpinner");
    if (!indicator) return;
    var PULL_THRESHOLD = 170;
    var RELEASE_DEBOUNCE_MS = 200;
    var pullAmount = 0;
    var cooldownUntil = 0;
    var releaseTimer = null;
    var hideTimer = null;
    var refreshing = false;

    function reveal() {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      indicator.hidden = false;
      void indicator.offsetWidth; // force reflow so the fade-in transition plays
      indicator.classList.add("visible");
    }
    function hide(delay) {
      if (hideTimer) clearTimeout(hideTimer);
      hideTimer = setTimeout(function () {
        indicator.classList.remove("visible");
        setTimeout(function () { indicator.hidden = true; }, 150);
      }, delay || 0);
    }
    function updatePullUI(amount) {
      reveal();
      arrow.hidden = false;
      spinner.hidden = true;
      spinner.classList.remove("spinning");
      var pct = Math.max(0, Math.min(1, amount / PULL_THRESHOLD));
      arrow.style.transform = "rotate(" + (pct * 180) + "deg)";
      label.hidden = pct < 1;
      label.textContent = pct >= 1 ? "Release to refresh" : "";
    }
    function cancelPull() {
      if (releaseTimer) { clearTimeout(releaseTimer); releaseTimer = null; }
      pullAmount = 0;
      hide();
    }
    function release() {
      releaseTimer = null;
      if (pullAmount < PULL_THRESHOLD) { cancelPull(); return; }
      pullAmount = 0;
      refreshing = true;
      cooldownUntil = Date.now() + 1500;
      arrow.hidden = true;
      spinner.hidden = false;
      spinner.classList.add("spinning");
      label.hidden = false;
      if (loadSettings().gitUrl) {
        label.textContent = "Syncing & Refreshing\\u2026";
        fetch("/api/git/sync", { method: "POST" })
          .then(function() { loadPapers(); })
          .catch(function() { loadPapers(); })
          .finally(function() {
            setTimeout(function () {
              refreshing = false;
              hide(300);
            }, 500);
          });
      } else {
        label.textContent = "Refreshing\\u2026";
        loadPapers();
        setTimeout(function () {
          refreshing = false;
          hide(300);
        }, 500);
      }
    }

    window.addEventListener("wheel", function (e) {
      if (refreshing || Date.now() < cooldownUntil) return;
      if (e.target && e.target.closest && e.target.closest(".sidebar, .info-panel")) return;
      if (window.scrollY > 0) {
        if (pullAmount) cancelPull();
        return;
      }
      if (e.deltaY < 0) {
        pullAmount += -e.deltaY;
        updatePullUI(pullAmount);
        if (releaseTimer) clearTimeout(releaseTimer);
        releaseTimer = setTimeout(release, RELEASE_DEBOUNCE_MS);
      } else if (e.deltaY > 0 && pullAmount) {
        cancelPull();
      }
    }, { passive: true });
  }

  function setStatus(msg, cls) {
    var el = document.getElementById("status");
    el.className = "status" + (cls ? " " + cls : "");
    el.textContent = msg;
  }

  function escHtml(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function upload(file) {
    var toast = showNoticeToast(
      '<span class="undo-toast-spinner"></span>Parsing "' + escHtml(file.name) + '"\\u2026 this can take up to a minute.',
      "loading"
    );
    fetch("/api/upload?filename=" + encodeURIComponent(file.name), { method: "POST", body: file })
      .then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      })
      .then(function (res) {
        if (!res.ok) {
          showNoticeToast(
            'Could not parse "' + escHtml(file.name) + '": ' + escHtml(res.data.error || "unknown error"),
            "error", 8000, toast
          );
          return;
        }
        var openHref = "/library/" + encodeURIComponent(res.data.id) + ".html";
        showNoticeToast(
          'Added "' + escHtml(res.data.title || file.name) + '" to your library. <a href="' + openHref + '">Open it</a>',
          "success", 8000, toast
        );
        loadPapers();
      })
      .catch(function (e) {
        showNoticeToast("Upload failed: " + escHtml(e.message), "error", 8000, toast);
      });
  }

  var fileInput = document.getElementById("fileInput");
  document.getElementById("addPaperBtn").addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () {
    if (fileInput.files[0]) upload(fileInput.files[0]);
    fileInput.value = "";
  });

  /* whole-page drag & drop upload */
  var dropOverlay = document.getElementById("dropOverlay");
  var dragCounter = 0;
  function hasFiles(e) {
    return !!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") !== -1);
  }
  window.addEventListener("dragenter", function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragCounter++;
    dropOverlay.hidden = false;
  });
  window.addEventListener("dragover", function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
  });
  window.addEventListener("dragleave", function (e) {
    if (!hasFiles(e)) return;
    dragCounter--;
    if (dragCounter <= 0) { dragCounter = 0; dropOverlay.hidden = true; }
  });
  window.addEventListener("drop", function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragCounter = 0;
    dropOverlay.hidden = true;
    var files = e.dataTransfer.files;
    if (files && files[0]) upload(files[0]);
  });

  var searchBox = document.getElementById("searchBox");
  var searchRow = document.getElementById("searchRow");
  searchBox.addEventListener("input", function () {
    render(this.value);
  });
  searchBox.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      searchOpen = false;
      searchRow.hidden = true;
      searchBox.value = "";
      searchBox.blur();
      render("");
    }
  });
  document.getElementById("navSearchBtn").addEventListener("click", function () {
    searchOpen = !searchOpen;
    searchRow.hidden = !searchOpen;
    if (searchOpen) searchBox.focus();
    else { searchBox.value = ""; render(""); }
  });

  var sortSelect = document.getElementById("sortSelect");
  sortSelect.value = sortBy;
  sortSelect.addEventListener("change", function () {
    sortBy = this.value;
    saveSettings({ sortBy: sortBy });
    render(document.getElementById("searchBox").value);
  });

  var tabButtons = Array.prototype.slice.call(document.querySelectorAll(".tab-btn"));
  function applyActiveTab() {
    tabButtons.forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.status === currentTab);
    });
  }
  tabButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      currentTab = btn.dataset.status;
      saveSettings({ tab: currentTab });
      applyActiveTab();
      closeInfoPanel();
      render(document.getElementById("searchBox").value);
    });
  });
  applyActiveTab();

  document.getElementById("navHome").addEventListener("click", function () {
    closeInfoPanel();
    activeTags = [];
    currentTab = "inbox";
    saveSettings({ tab: currentTab });
    applyActiveTab();
    renderSidebarTags();
      renderSidebarPinned();
    searchOpen = false;
    searchRow.hidden = true;
    searchBox.value = "";
    render("");
  });

  document.getElementById("infoCloseBtn").addEventListener("click", function () {
    closeInfoPanel();
    render(document.getElementById("searchBox").value);
  });
  document.addEventListener("keydown", function (e) {
    if (!confirmOverlay.hidden) {
      if (e.key === "Escape") closeConfirmDialog();
      return;
    }
    if (e.key === "Escape" && !document.getElementById("infoPanel").hidden) {
      closeInfoPanel();
      render(document.getElementById("searchBox").value);
      return;
    }
    // "a"/"d" (archive/delete) target whichever card the mouse is
    // currently over, matching the hint shown in each button's own
    // tooltip -- same reasoning as the reader page's hover-scoped
    // shortcuts, and guarded the same way against typing in a field.
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target;
    var tag = t && t.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;

    if (e.key === "[") {
      e.preventDefault();
      var c = document.documentElement.classList.toggle("library-sidebar-collapsed");
      saveSettings({ librarySidebarHidden: c });
      return;
    }
    if (e.key === "]") {
      e.preventDefault();
      var c = document.documentElement.classList.toggle("library-info-collapsed");
      saveSettings({ libraryInfoPanelHidden: c });
      var ip = document.getElementById("infoPanel");
      if (!c && ip.hidden) {
        var targetId = selectedPaper ? selectedPaper.id : hoveredPaperId;
        if (targetId) {
          var p = papers.filter(function (x) { return x.id === targetId; })[0];
          if (p) openInfoPanel(p);
        }
      }
      return;
    }

    if (e.key === "/") {
      e.preventDefault();
      searchOpen = true;
      searchRow.hidden = false;
      searchBox.focus();
      searchBox.select();
      return;
    }

    if (!hoveredPaperId) return;
    var hp = papers.filter(function (x) { return x.id === hoveredPaperId; })[0];
    if (!hp) return;
    if (e.key === "a" || e.key === "A") {
      e.preventDefault();
      updatePaperStatus(hp, "archive");
    } else if (e.key === "d" || e.key === "D") {
      e.preventDefault();
      deletePaperFromCard(hp);
    }
  });

  /* refresh the list (and re-sort by "most recently opened", etc.) whenever
     the user comes back to this page -- browser back/forward can restore it
     from bfcache without re-running this script, and it may have been left
     open in a background tab while a paper was read elsewhere */
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) loadPapers();
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") loadPapers();
  });

  initTheme();
  initPullToRefresh();
  
  // Inject icons into static elements
  document.getElementById("navHome").innerHTML = HOME_ICON + "<span>Home</span>";
  document.getElementById("navSearchBtn").innerHTML = SEARCH_ICON + "<span>Search</span>";
  document.getElementById("navPrefsBtn").innerHTML = PREFS_ICON + '<span>Preferences</span> <span class="nav-item-sub" id="prefsThemeLabel">Auto</span>';
  
  var tabs = document.querySelectorAll(".tab-btn");
  if (tabs.length >= 5) {
    tabs[0].innerHTML = INBOX_ICON + "<span>Inbox</span>";
    tabs[1].innerHTML = LATER_ICON + "<span>Later</span>";
    tabs[2].innerHTML = CHECK_ICON + "<span>Completed</span>";
    tabs[3].innerHTML = ARCHIVE_ICON + "<span>Archive</span>";
    tabs[4].innerHTML = TRASH_ICON + "<span>Trash</span>";
  }
  document.getElementById("confirmCancelBtn").innerHTML = X_ICON + "<span>Cancel</span>";
  document.getElementById("confirmDeleteBtn").innerHTML = TRASH_ICON + "<span>Delete</span>";

  
  function setupDragDropZones() {
    var zones = [];
    document.querySelectorAll(".tab-btn").forEach(function(el) { zones.push(el); });
    var navPinned = document.getElementById("navPinned");
    var navPinnedLabel = document.getElementById("navPinnedLabel");
    if (navPinned) zones.push(navPinned);
    if (navPinnedLabel) zones.push(navPinnedLabel);

    zones.forEach(function(zone) {
      zone.addEventListener("dragenter", function(e) {
        if (e.dataTransfer.types.indexOf("application/x-paper-id") !== -1) {
          e.preventDefault();
          zone.classList.add("drag-over");
        }
      });
      zone.addEventListener("dragover", function(e) {
        if (e.dataTransfer.types.indexOf("application/x-paper-id") !== -1) {
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
        }
      });
      zone.addEventListener("dragleave", function(e) {
        zone.classList.remove("drag-over");
      });
      zone.addEventListener("drop", function(e) {
        zone.classList.remove("drag-over");
        var paperId = e.dataTransfer.getData("application/x-paper-id");
        if (!paperId) return;
        
        var p = papers.filter(function(x) { return x.id === paperId; })[0];
        if (!p) return;
        
        if (zone === navPinned || zone === navPinnedLabel) {
          if (!p.pinned) updatePaper(p, { pinned: true });
        } else if (zone.classList.contains("tab-btn")) {
          var targetStatus = zone.dataset.status;
          if (targetStatus === "completed") {
            if (!p.completed) updatePaper(p, { completed: true });
          } else {
            if (p.status !== targetStatus) updatePaperStatus(p, targetStatus);
          }
        }
      });
    });
  }
  setupDragDropZones();

  loadPapers();
})();
</script>
</body>
</html>
"""

ABOUT_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='m16 6 4 14'/><path d='M12 6v14'/><path d='M8 8v12'/><path d='M4 4v16'/></svg>">
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
  :root { --bg: #1e1e1e; --fg: #ece9e2; --muted: #a9a49a; --rule: #444444; --accent: #7fa7ff; }
}
:root[data-theme="light"] { --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db; }
:root[data-theme="dark"] { --bg: #1e1e1e; --fg: #ece9e2; --muted: #a9a49a; --rule: #444444; --accent: #7fa7ff; }

:root[data-theme="dark"] [style*="color: #000"],
:root[data-theme="dark"] [style*="color:#000"],
:root[data-theme="dark"] [style*="color: black"],
:root[data-theme="dark"] [style*="color:black"],
:root:not([data-theme="light"]) [style*="color: #000"],
:root:not([data-theme="light"]) [style*="color:#000"],
:root:not([data-theme="light"]) [style*="color: black"],
:root:not([data-theme="light"]) [style*="color:black"] {
  color: inherit !important;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 640px; margin: 0 auto; padding: 9vh 6vw 12vh; }
.back-link {
  display: inline-flex; align-items: center; gap: 0.4em; color: var(--muted); text-decoration: none;
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif; font-size: 0.85em; margin-bottom: 2.5em;
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
  <p>accessibility is also a huge issue with pdf research papers, making them difficult to read on various devices or with assistive technologies. this project addresses this by converting papers into a clean, responsive html format.</p>
</div>
</body>
</html>
"""

PIPELINE_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='m16 6 4 14'/><path d='M12 6v14'/><path d='M8 8v12'/><path d='M4 4v16'/></svg>">
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
<title>Pipeline &mdash; Andrew's Paper Library</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db;
  --card-bg: #fbfaf8; --error: #b3261e; --ok: #1a7d3a;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #1e1e1e; --fg: #ece9e2; --muted: #a9a49a; --rule: #444444; --accent: #7fa7ff; --card-bg: #2a2a2a; --error: #ff6b60; --ok: #5fd88a; }
}
:root[data-theme="light"] { --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db; --card-bg: #fbfaf8; --error: #b3261e; --ok: #1a7d3a; }
:root[data-theme="dark"] { --bg: #1e1e1e; --fg: #ece9e2; --muted: #a9a49a; --rule: #444444; --accent: #7fa7ff; --card-bg: #2a2a2a; --error: #ff6b60; --ok: #5fd88a; }

:root[data-theme="dark"] [style*="color: #000"],
:root[data-theme="dark"] [style*="color:#000"],
:root[data-theme="dark"] [style*="color: black"],
:root[data-theme="dark"] [style*="color:black"],
:root:not([data-theme="light"]) [style*="color: #000"],
:root:not([data-theme="light"]) [style*="color:#000"],
:root:not([data-theme="light"]) [style*="color: black"],
:root:not([data-theme="light"]) [style*="color:black"] {
  color: inherit !important;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 760px; margin: 0 auto; padding: 9vh 6vw 12vh; }
.back-link {
  display: inline-flex; align-items: center; gap: 0.4em; color: var(--muted); text-decoration: none;
  font-size: 0.85em; margin-bottom: 2.5em;
}
.back-link:hover { color: var(--accent); }
.back-link svg { display: block; }
h1 { font-size: 1.6em; margin: 0 0 0.2em; }
.subtitle { color: var(--muted); font-size: 0.92em; margin: 0 0 2em; }
h2 { font-size: 1.02em; margin: 2.2em 0 0.8em; }
.empty { color: var(--muted); font-size: 0.92em; padding: 1.2em 0; }
.job-list { display: flex; flex-direction: column; gap: 0.6em; }
.job-card {
  border: 1px solid var(--rule); border-radius: 10px; background: var(--card-bg);
  padding: 0.9em 1.1em; display: flex; align-items: center; gap: 0.9em;
}
.job-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.94em; }
.job-meta { display: flex; align-items: center; gap: 0.6em; flex-shrink: 0; font-size: 0.85em; color: var(--muted); }
.job-stage {
  display: inline-flex; align-items: center; gap: 0.4em; padding: 0.25em 0.65em; border-radius: 999px;
  background: color-mix(in srgb, var(--accent) 14%, transparent); color: var(--accent); font-weight: 600;
}
.job-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
.job-elapsed { font-variant-numeric: tabular-nums; }
.job-result {
  display: inline-flex; align-items: center; gap: 0.4em; padding: 0.25em 0.65em; border-radius: 999px; font-weight: 600;
}
.job-result.ok { background: color-mix(in srgb, var(--ok) 14%, transparent); color: var(--ok); }
.job-result.fail { background: color-mix(in srgb, var(--error) 14%, transparent); color: var(--error); }
.job-error { color: var(--error); font-size: 0.85em; margin-top: 0.4em; }
.job-open { color: var(--accent); font-size: 0.85em; text-decoration: none; }
.job-open:hover { text-decoration: underline; }
.job-card-col { display: flex; flex-direction: column; gap: 0.3em; flex: 1; min-width: 0; }
@media (prefers-reduced-motion: reduce) { .job-dot { animation: none; } }
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
  <h1>Pipeline</h1>
  <p class="subtitle" id="subtitle">Checking&hellip;</p>

  <h2>Processing</h2>
  <div id="activeList"></div>

  <h2>Recently finished</h2>
  <div id="historyList"></div>
</div>
<script>
function esc(s) {
  var d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
function fmtElapsed(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 60) return seconds + "s";
  var m = Math.floor(seconds / 60), s = seconds % 60;
  if (m < 60) return m + "m " + s + "s";
  var h = Math.floor(m / 60);
  return h + "h " + (m % 60) + "m";
}
function fmtAgo(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 5) return "just now";
  return fmtElapsed(seconds) + " ago";
}

function renderActive(jobs) {
  var el = document.getElementById("activeList");
  if (!jobs.length) {
    el.innerHTML = '<div class="empty">Nothing converting right now.</div>';
    return;
  }
  var now = Date.now() / 1000;
  el.innerHTML = '<div class="job-list">' + jobs.map(function (j) {
    return '<div class="job-card">' +
      '<span class="job-name" title="' + esc(j.filename) + '">' + esc(j.filename) + '</span>' +
      '<span class="job-meta">' +
        '<span class="job-stage"><span class="job-dot"></span>' + esc(j.stage) + '</span>' +
        '<span class="job-elapsed">' + fmtElapsed(now - j.startedAt) + '</span>' +
      '</span>' +
    '</div>';
  }).join("") + '</div>';
}

function renderHistory(jobs) {
  var el = document.getElementById("historyList");
  if (!jobs.length) {
    el.innerHTML = '<div class="empty">No papers have finished processing yet this session.</div>';
    return;
  }
  var now = Date.now() / 1000;
  el.innerHTML = '<div class="job-list">' + jobs.map(function (j) {
    var resultBadge = j.ok
      ? '<span class="job-result ok">Done</span>'
      : '<span class="job-result fail">Failed</span>';
    var detail = j.ok
      ? (j.paperId ? '<a class="job-open" href="/library/' + esc(j.paperId) + '.html">Open it</a>' : '')
      : '<div class="job-error">' + esc(j.error || "unknown error") + '</div>';
    return '<div class="job-card">' +
      '<div class="job-card-col">' +
        '<span class="job-name" title="' + esc(j.filename) + '">' + esc(j.filename) + '</span>' +
        detail +
      '</div>' +
      '<span class="job-meta">' +
        resultBadge +
        '<span>' + fmtAgo(now - j.finishedAt) + '</span>' +
      '</span>' +
    '</div>';
  }).join("") + '</div>';
}

function refresh() {
  fetch("/api/pipeline-status").then(function (r) { return r.json(); }).then(function (data) {
    renderActive(data.active || []);
    renderHistory(data.history || []);
    var n = (data.active || []).length;
    document.getElementById("subtitle").textContent = n
      ? (n === 1 ? "1 paper converting" : n + " papers converting")
      : "Nothing in progress";
  }).catch(function () {
    document.getElementById("subtitle").textContent = "Couldn't reach the server";
  });
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "PaperReaderLibrary/1.0"

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Papers get tagged/moved/opened from other tabs and pages all the
        # time -- never let the browser serve a stale cached copy of the
        # library index, the home page, or a reader page.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status: int = 200) -> None:
        self._send_bytes(json.dumps(obj).encode("utf-8"), "application/json", status)

    def _send_html(self, html_str: str, status: int = 200) -> None:
        self._send_bytes(html_str.encode("utf-8"), "text/html; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        parsed = urlparse(self.path)
        if parsed.path == "/":
            from .palette import get_palette_html
            self._send_html(HOME_PAGE_HTML.replace('</body>', get_palette_html('home') + '</body>'))
        elif parsed.path == "/about":
            self._send_html(ABOUT_PAGE_HTML)
        elif parsed.path == "/pipeline":
            from .palette import get_palette_html
            self._send_html(PIPELINE_PAGE_HTML.replace('</body>', get_palette_html('home') + '</body>'))
        elif parsed.path == "/api/papers":
            self._send_json(_load_index())
        elif parsed.path == "/api/pipeline-status":
            self._send_json(_list_jobs())
        elif parsed.path.startswith("/library/"):
            self._serve_library_file(parsed.path[len("/library/") :])
        else:
            self._send_html("<h1>404</h1>", 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self._handle_upload(parsed)

        elif parsed.path == "/api/git/setup":
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                import json, subprocess
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                url = body.get("url")
                if not url:
                    self._send_json({"error": "missing url"}, 400)
                    return
                lib = LIBRARY_DIR
                subprocess.run(["git", "init"], cwd=lib, check=False)
                
                # Ensure server logs are ignored so they don't block rebases
                gitignore = lib / ".gitignore"
                if not gitignore.exists():
                    gitignore.write_text("server.log\nserver.pid\n")
                    
                subprocess.run(["git", "remote", "remove", "origin"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                res = subprocess.run(["git", "remote", "add", "origin", url], cwd=lib, capture_output=True, text=True)
                if res.returncode != 0:
                    self._send_json({"error": res.stderr}, 400)
                    return
                subprocess.run(["git", "fetch", "origin"], cwd=lib, check=False)
                subprocess.run(["git", "branch", "-M", "main"], cwd=lib, check=False)
                
                # Commit any unstaged local files first before pulling, to prevent rebase errors
                subprocess.run(["git", "add", "."], cwd=lib, check=False)
                subprocess.run(["git", "commit", "-m", "Auto-sync update"], cwd=lib, check=False)
                
                pull_res = subprocess.run(["git", "pull", "origin", "main", "--rebase", "--strategy-option=ours"], cwd=lib, capture_output=True, text=True)
                if pull_res.returncode != 0 and "couldn't find remote ref" not in pull_res.stderr:
                    self._send_json({"error": pull_res.stderr}, 400)
                    return
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif parsed.path == "/api/git/sync":
            try:
                import subprocess
                lib = LIBRARY_DIR
                if not (lib / ".git").exists():
                    self._send_json({"error": "Not configured"}, 400)
                    return
                subprocess.run(["git", "add", "."], cwd=lib, check=False)
                subprocess.run(["git", "commit", "-m", "Manual sync update"], cwd=lib, check=False)
                pull = subprocess.run(["git", "pull", "origin", "main", "--rebase", "--strategy-option=ours"], cwd=lib, capture_output=True, text=True)
                push = subprocess.run(["git", "push", "origin", "main"], cwd=lib, capture_output=True, text=True)
                if push.returncode != 0:
                    self._send_json({"error": push.stderr}, 400)
                else:
                    self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": "not found"}, 404)


    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/papers/"):
            paper_id = os.path.basename(parsed.path[len("/api/papers/") :])
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json({"error": "invalid JSON body"}, 400)
                return
            entry = _update_paper(paper_id, body)
            if entry is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json(entry)
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
        elif parsed.path.startswith("/api/papers/") and parsed.path.endswith("/status"):
            paper_id = os.path.basename(parsed.path[len("/api/papers/") : -len("/status")])
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json({"error": "invalid JSON body"}, 400)
                return
            status_val = body.get("status")
            if status_val not in PAPER_STATUSES:
                self._send_json({"error": "status must be one of: " + ", ".join(PAPER_STATUSES)}, 400)
                return
            entry = _set_paper_status(paper_id, status_val)
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
            self._send_json(
                {"error": "unsupported file type -- use .tex, .zip, .tar.gz, .tgz, .tar, .html, or .pdf"}, 400
            )
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
        except (LatexConvertError, HtmlConvertError, PdfConvertError) as e:
            self._send_json({"error": str(e)}, 400)
            return
        except Exception as e:  # keep the server alive even if one paper fails to convert
            self._send_json({"error": f"unexpected error: {html.escape(str(e))}"}, 500)
            return

        _trigger_git_sync()
        self._send_json(entry)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        print("[library] " + (format % args))


def is_server_running(host: str = "127.0.0.1", port: int = 8765) -> bool:
    url = f"http://{host}:{port}/api/papers"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "paper-reader-cli"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def stop_server() -> bool:
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
        if PID_PATH.exists():
            try:
                PID_PATH.unlink()
            except OSError:
                pass
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        if PID_PATH.exists():
            try:
                PID_PATH.unlink()
            except OSError:
                pass
        return False


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        PID_PATH.write_text(str(os.getpid()))
    except OSError:
        pass

    try:
        server = ThreadingHTTPServer((host, port), Handler)
        url = f"http://{host}:{port}/"
        print(f"Andrew's Paper Library running at {url}  (library stored in {LIBRARY_DIR})")
        print("Press Ctrl+C to stop.")

        # Rebuild library asynchronously so server startup is immediate
        threading.Thread(target=rebuild_library, daemon=True).start()

        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        if PID_PATH.exists():
            try:
                PID_PATH.unlink()
            except OSError:
                pass



if __name__ == "__main__":
    run()

