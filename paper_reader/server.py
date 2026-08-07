"""
A very small local web app around the existing convert+restyle pipeline:
drag a LaTeX source file (.tex / .tar.gz / .tgz / .zip), HTML page, PDF, or
EPUB onto the home page, it gets parsed the same way the CLI does, and the
result is added to a local library you can search and open (each paper opens
as its own self-contained reader page, in a new tab).

No framework -- just the standard library's http.server, since the only
job here is "save an upload, run a function, list some JSON, serve some
files." Runs entirely on localhost.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .epub_convert import EpubConvertError
from .epub_convert import convert as convert_epub
from .html_convert import HtmlConvertError
from .html_convert import convert as convert_html
from .latex_convert import LatexConvertError, convert as convert_latex
from .pdf_convert import PdfConvertError
from .pdf_convert import convert as convert_pdf
from .restyle import restyle
from .vault import (
    ACCOUNTS_PATH,
    LIBRARY_DIR,
    Vault,
    derive_fernet,
    ensure_empty_vault,
    migrate_legacy_into_vault,
)

PID_PATH = LIBRARY_DIR / "server.pid"
LOG_PATH = LIBRARY_DIR / "server.log"
# Legacy plaintext paths (pre-vault). Kept for one-shot migration only.
LEGACY_AUTH_PATH = LIBRARY_DIR / "auth.json"

ALLOWED_UPLOAD_SUFFIXES = (
    ".tex", ".zip", ".tar.gz", ".tgz", ".tar", ".html", ".htm", ".pdf", ".epub",
)
HTML_SOURCE_SUFFIXES = (".html", ".htm")
PDF_SOURCE_SUFFIXES = (".pdf",)
EPUB_SOURCE_SUFFIXES = (".epub",)
MAX_UPLOAD_BYTES = 60 * 1024 * 1024  # 60MB is generous for a LaTeX source tree
PAPER_STATUSES = ("inbox", "later", "archive", "trash")

# Auth is on by default. Each secret key owns an encrypted vault under
# ~/.paper_reader_library/vaults/<id>/. Only key/enc salts + hashes live in
# accounts.json. Set PAPER_READER_DISABLE_AUTH=1 to skip login (still uses a
# local encrypted vault unlocked automatically).
SESSION_COOKIE = "paper_reader_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days
_PBKDF2_ITERATIONS = 200_000
_SECRET_KEY_BYTES = 32  # 256 bits of entropy
_AUTH_LOCK = threading.Lock()
_SESSION_LOCK = threading.Lock()
# token -> {account_id, vault, exp}; DEK lives only here (process memory)
_SESSIONS: dict[str, dict] = {}
_TLS = threading.local()
_LOCAL_ACCOUNT_ID = "local"
_LOCAL_VAULT_META = LIBRARY_DIR / ".local_vault.json"
_PROFILE_NAME = "profile.json"
_AVATAR_NAME = "avatar.jpg"
_MAX_DISPLAY_NAME_LEN = 64
_MAX_AVATAR_BYTES = 512 * 1024  # cropped JPEG upload cap
_GIT_SYNC_LOCK = threading.Lock()


def _auth_disabled() -> bool:
    return os.environ.get("PAPER_READER_DISABLE_AUTH", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _auth_enabled() -> bool:
    return not _auth_disabled()


def _current_vault() -> Optional[Vault]:
    return getattr(_TLS, "vault", None)


def _set_current_vault(vault: Optional[Vault]) -> None:
    _TLS.vault = vault


def _require_vault() -> Vault:
    vault = _current_vault()
    if vault is None:
        raise RuntimeError("vault is locked — sign in to unlock your library")
    return vault


def _empty_accounts() -> dict:
    return {
        "accounts": [],
        "session_secret": os.urandom(32).hex(),
    }


def _save_accounts(data: dict) -> None:
    from .vault import _atomic_write_bytes

    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(ACCOUNTS_PATH, json.dumps(data, indent=2).encode("utf-8"))


def _migrate_legacy_auth_file(data: dict) -> dict:
    """Fold single-account auth.json into accounts.json once.

    Only imports when there are not yet any accounts — otherwise a restored
    auth.json would create an orphan second account alongside an existing one.
    """
    if not LEGACY_AUTH_PATH.is_file():
        return data
    existing = data.get("accounts") or []
    if existing:
        # Accounts already present; drop stale auth.json so it cannot reappear.
        try:
            LEGACY_AUTH_PATH.unlink()
        except OSError:
            pass
        return data
    try:
        legacy = json.loads(LEGACY_AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    if not isinstance(legacy, dict):
        return data
    if not legacy.get("key_hash") or not legacy.get("key_salt"):
        return data
    account = {
        "id": uuid.uuid4().hex,
        "key_salt": legacy["key_salt"],
        "key_hash": legacy["key_hash"],
        "enc_salt": os.urandom(16).hex(),
        "createdAt": legacy.get("createdAt") or time.time(),
    }
    data.setdefault("accounts", []).append(account)
    if legacy.get("session_secret") and not data.get("session_secret"):
        data["session_secret"] = legacy["session_secret"]
    data.setdefault("legacy_migrated", False)
    _save_accounts(data)
    try:
        LEGACY_AUTH_PATH.unlink()
    except OSError:
        pass
    return data


def _load_accounts() -> dict:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    if ACCOUNTS_PATH.is_file():
        try:
            data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("accounts"), list):
                return _migrate_legacy_auth_file(data)
        except (OSError, json.JSONDecodeError):
            pass
    data = _empty_accounts()
    data = _migrate_legacy_auth_file(data)
    if not ACCOUNTS_PATH.is_file():
        _save_accounts(data)
    return data


def _account_exists() -> bool:
    return bool(_load_accounts().get("accounts"))


def _generate_secret_key() -> str:
    """High-entropy account key. Format: pr_<urlsafe>. Never stored plaintext."""
    return "pr_" + secrets.token_urlsafe(_SECRET_KEY_BYTES)


def _hash_secret_key(secret_key: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", secret_key.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return digest.hex()


def _normalize_secret_key(raw: str) -> str:
    # Allow users to paste keys with accidental whitespace/newlines.
    return "".join((raw or "").split())


def _create_account() -> tuple[str, dict]:
    """Create a new secret-key account with its own empty vault.
    Returns (plaintext_secret_key, account_record). Does not remove prior accounts."""
    with _AUTH_LOCK:
        secret_key = _generate_secret_key()
        key_salt = os.urandom(16)
        enc_salt = os.urandom(16)
        account = {
            "id": uuid.uuid4().hex,
            "key_salt": key_salt.hex(),
            "key_hash": _hash_secret_key(secret_key, key_salt),
            "enc_salt": enc_salt.hex(),
            "createdAt": time.time(),
        }
        data = _load_accounts()
        data.setdefault("accounts", []).append(account)
        if not data.get("session_secret"):
            data["session_secret"] = os.urandom(32).hex()
        _save_accounts(data)
        ensure_empty_vault(account["id"])
        return secret_key, account


def _find_account_for_key(secret_key: str) -> Optional[dict]:
    key = _normalize_secret_key(secret_key)
    if not key:
        return None
    for acct in _load_accounts().get("accounts") or []:
        try:
            salt = bytes.fromhex(acct["key_salt"])
        except (KeyError, ValueError, TypeError):
            continue
        expected = acct.get("key_hash") or ""
        got = _hash_secret_key(key, salt)
        if hmac.compare_digest(got, expected):
            return acct
    return None


def _unlock_vault_for_account(secret_key: str, account: dict) -> Vault:
    key = _normalize_secret_key(secret_key)
    try:
        enc_salt = bytes.fromhex(account["enc_salt"])
    except (KeyError, ValueError, TypeError) as e:
        raise ValueError("account is missing encryption salt") from e
    fernet = derive_fernet(key, enc_salt)
    vault = Vault(account["id"], fernet)
    migrated = migrate_legacy_into_vault(vault)
    if migrated:
        print(f"[library] migrated {migrated} legacy paper(s) into vault {account['id'][:8]}…")
    return vault


def _ensure_auth_disabled_vault() -> Vault:
    """When auth is off, unlock a machine-local vault automatically."""
    existing = _current_vault()
    if existing is not None and existing.account_id == _LOCAL_ACCOUNT_ID:
        return existing
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    if _LOCAL_VAULT_META.is_file():
        meta = json.loads(_LOCAL_VAULT_META.read_text(encoding="utf-8"))
        secret = meta["secret"]
        enc_salt = bytes.fromhex(meta["enc_salt"])
    else:
        secret = "pr_" + secrets.token_urlsafe(_SECRET_KEY_BYTES)
        enc_salt = os.urandom(16)
        _LOCAL_VAULT_META.write_text(
            json.dumps({"secret": secret, "enc_salt": enc_salt.hex()}, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(_LOCAL_VAULT_META, 0o600)
        except OSError:
            pass
    vault = Vault(_LOCAL_ACCOUNT_ID, derive_fernet(secret, enc_salt))
    migrated = migrate_legacy_into_vault(vault)
    if migrated:
        print(f"[library] migrated {migrated} legacy paper(s) into local vault")
    _set_current_vault(vault)
    return vault


def _make_session(account_id: str, vault: Vault) -> str:
    token = secrets.token_urlsafe(32)
    with _SESSION_LOCK:
        _SESSIONS[token] = {
            "account_id": account_id,
            "vault": vault,
            "exp": time.time() + SESSION_MAX_AGE,
        }
    return token


def _get_session(token: str) -> Optional[dict]:
    if not token:
        return None
    with _SESSION_LOCK:
        sess = _SESSIONS.get(token)
        if sess is None:
            return None
        if sess["exp"] < time.time():
            _SESSIONS.pop(token, None)
            return None
        return sess


def _clear_session(token: str) -> None:
    if not token:
        return
    with _SESSION_LOCK:
        _SESSIONS.pop(token, None)


def _parse_cookies(cookie_header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not cookie_header:
        return out
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def _secure_cookies() -> bool:
    """Use Secure cookies behind HTTPS (e.g. Fly.io)."""
    raw = os.environ.get("PAPER_READER_SECURE_COOKIES", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    # Fly / common PaaS markers
    return bool(os.environ.get("FLY_APP_NAME") or os.environ.get("FLY_MACHINE_ID"))


def _session_set_cookie(token: str) -> str:
    parts = [
        f"{SESSION_COOKIE}={token}",
        "Path=/",
        f"Max-Age={SESSION_MAX_AGE}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if _secure_cookies():
        parts.append("Secure")
    return "; ".join(parts)


def _session_clear_cookie() -> str:
    parts = [
        f"{SESSION_COOKIE}=",
        "Path=/",
        "Max-Age=0",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if _secure_cookies():
        parts.append("Secure")
    return "; ".join(parts)


def _auth_entry_path() -> str:
    """Unauthenticated browsers land on generate-key; sign-in is a secondary option."""
    return "/signup"


def _bind_vault_from_request(handler: "Handler") -> Optional[Vault]:
    """Attach the unlocked vault for this request thread (if any)."""
    if not _auth_enabled():
        return _ensure_auth_disabled_vault()
    cookies = _parse_cookies(handler.headers.get("Cookie", ""))
    sess = _get_session(cookies.get(SESSION_COOKIE, ""))
    if sess is None:
        _set_current_vault(None)
        return None
    vault = sess["vault"]
    _set_current_vault(vault)
    return vault


def _profile_path(vault: Vault) -> Path:
    return vault.root / _PROFILE_NAME


def _avatar_path(vault: Vault) -> Path:
    return vault.root / _AVATAR_NAME


def _normalize_display_name(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    # Collapse whitespace; strip control characters.
    cleaned = "".join(ch for ch in raw if ch.isprintable() or ch in "\t ")
    cleaned = " ".join(cleaned.split())
    return cleaned[:_MAX_DISPLAY_NAME_LEN]


def _load_profile(vault: Vault) -> dict:
    path = _profile_path(vault)
    if not path.is_file():
        return {"displayName": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"displayName": ""}
    if not isinstance(data, dict):
        return {"displayName": ""}
    return {"displayName": _normalize_display_name(data.get("displayName"))}


def _save_profile(vault: Vault, display_name: str) -> dict:
    from .vault import _atomic_write_bytes

    profile = {"displayName": _normalize_display_name(display_name)}
    path = _profile_path(vault)
    _atomic_write_bytes(path, json.dumps(profile, indent=2).encode("utf-8"))
    return profile


def _account_public_payload(vault: Vault) -> dict:
    profile = _load_profile(vault)
    return {
        "displayName": profile.get("displayName") or "",
        "hasAvatar": _avatar_path(vault).is_file(),
    }


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


def _load_index(vault: Optional[Vault] = None) -> list[dict]:
    v = vault or _current_vault()
    if v is None:
        return []
    return v.load_index()


def _save_index(items: list[dict], vault: Optional[Vault] = None) -> None:
    v = vault or _require_vault()
    v.save_index(items)


def _process_upload(
    filename: str,
    data: bytes,
    vault: Optional[Vault] = None,
    pdf_parser: str = "docling",
    mineru_token: str = "",
) -> dict:
    """Run the same convert()+restyle() pipeline the CLI uses, encrypt into
    the unlocked vault, and return its index entry."""
    v = vault or _require_vault()
    job_id = _job_start(filename)
    try:
        paper_id = uuid.uuid4().hex[:12]
        is_html_source = filename.lower().endswith(HTML_SOURCE_SUFFIXES)
        is_pdf_source = filename.lower().endswith(PDF_SOURCE_SUFFIXES)
        is_epub_source = filename.lower().endswith(EPUB_SOURCE_SUFFIXES)
        kind = (
            "html" if is_html_source
            else "pdf" if is_pdf_source
            else "epub" if is_epub_source
            else "latex"
        )
        with tempfile.TemporaryDirectory(prefix="paper_reader_upload_") as tmp:
            tmp_root = Path(tmp)
            upload_path = tmp_root / os.path.basename(filename)
            upload_path.write_bytes(data)
            raw_workdir = tmp_root / "raw"
            raw_workdir.mkdir(parents=True, exist_ok=True)
            _job_stage(job_id, f"converting ({kind})")
            if is_html_source:
                raw_html_path = convert_html(str(upload_path), str(raw_workdir))
            elif is_pdf_source:
                raw_html_path = convert_pdf(
                    str(upload_path),
                    str(raw_workdir),
                    backend=pdf_parser,
                    mineru_token=mineru_token or None,
                    on_stage=lambda s: _job_stage(job_id, s),
                )
            elif is_epub_source:
                raw_html_path = convert_epub(str(upload_path), str(raw_workdir))
            else:
                raw_html_path = convert_latex(str(upload_path), str(raw_workdir))

            _job_stage(job_id, "styling")
            html_out, metadata = restyle(raw_html_path, source_name=filename, back_link="/")
            rel_raw = str(Path(raw_html_path).resolve().relative_to(raw_workdir.resolve()))

            _job_stage(job_id, "saving")
            v.write_html(paper_id, html_out)
            v.encrypt_raw_tree(paper_id, raw_workdir)

        entry = {
            "id": paper_id,
            "title": metadata.get("title") or filename,
            "authors": [a["name"] for a in metadata.get("authors", [])],
            "venue": metadata.get("venue", ""),
            "summary": (metadata.get("abstract") or "").strip()[:320],
            "sourceFilename": filename,
            "addedAt": time.time(),
            "lastOpenedAt": None,
            "rawHtmlPath": rel_raw,
            "tags": [],
            "status": "inbox",
            "pinned": False,
            "completed": False,
            "deletedAt": None,
        }
        items = _load_index(v)
        items.insert(0, entry)
        _save_index(items, v)
        _job_finish(job_id, ok=True, paper_id=paper_id)
        return entry
    except Exception as e:
        _job_finish(job_id, ok=False, error=str(e))
        raise


def _rebuild_paper(entry: dict, vault: Optional[Vault] = None) -> bool:
    """Re-run restyle() on a paper's stored raw (pre-restyle) HTML, so it
    picks up the current reader CSS/JS. Returns False if there's no raw
    source available to rebuild from."""
    v = vault or _require_vault()
    paper_id = entry.get("id")
    if not paper_id:
        return False
    raw_rel = entry.get("rawHtmlPath") or ""
    with tempfile.TemporaryDirectory(prefix="paper_reader_rebuild_") as tmp:
        dest = Path(tmp) / "raw"
        if not v.decrypt_raw_tree(paper_id, dest):
            # Legacy absolute path from pre-vault uploads (if still on disk).
            if raw_rel and Path(raw_rel).is_file():
                html_out, metadata = restyle(
                    raw_rel, source_name=entry.get("sourceFilename", ""), back_link="/"
                )
                v.write_html(paper_id, html_out)
                entry["title"] = metadata.get("title") or entry["title"]
                entry["authors"] = [a["name"] for a in metadata.get("authors", [])]
                entry["venue"] = metadata.get("venue", "")
                entry["summary"] = (metadata.get("abstract") or "").strip()[:320]
                return True
            return False
        candidate = dest / raw_rel if raw_rel and not str(raw_rel).startswith("vault:") else None
        if candidate is None or not candidate.is_file():
            htmls = sorted(dest.rglob("*.html"))
            if not htmls:
                return False
            candidate = htmls[0]
        html_out, metadata = restyle(
            str(candidate), source_name=entry.get("sourceFilename", ""), back_link="/"
        )
        v.write_html(paper_id, html_out)
        try:
            entry["rawHtmlPath"] = str(candidate.resolve().relative_to(dest.resolve()))
        except ValueError:
            pass
    entry["title"] = metadata.get("title") or entry["title"]
    entry["authors"] = [a["name"] for a in metadata.get("authors", [])]
    entry["venue"] = metadata.get("venue", "")
    entry["summary"] = (metadata.get("abstract") or "").strip()[:320]
    return True


def _trigger_git_sync():
    import subprocess
    lib = LIBRARY_DIR
    if not (lib / ".git").exists():
        return
    def sync_task():
        # Serialize syncs so concurrent paper opens don't pile up git pulls.
        if not _GIT_SYNC_LOCK.acquire(blocking=False):
            return
        try:
            subprocess.run(["git", "add", "."], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "commit", "-m", "Auto-sync update"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "pull", "origin", "main", "--rebase", "--strategy-option=ours"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "push", "origin", "main"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[library] git sync failed: {e}")
        finally:
            _GIT_SYNC_LOCK.release()
    threading.Thread(target=sync_task, daemon=True).start()


def _delete_paper(paper_id: str, vault: Optional[Vault] = None) -> bool:
    """Permanently remove a paper from the unlocked vault."""
    v = vault or _require_vault()
    items = _load_index(v)
    remaining = [e for e in items if e["id"] != paper_id]
    if len(remaining) == len(items):
        return False
    _save_index(remaining, v)
    _trigger_git_sync()
    v.delete_paper_files(paper_id)
    return True


def _set_paper_tags(paper_id: str, tags: list, vault: Optional[Vault] = None) -> dict | None:
    """Replace a paper's tag list."""
    v = vault or _require_vault()
    items = _load_index(v)
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
    _save_index(items, v)
    _trigger_git_sync()
    return entry


def _update_paper(paper_id: str, updates: dict, vault: Optional[Vault] = None) -> dict | None:
    v = vault or _require_vault()
    items = _load_index(v)
    entry = next((e for e in items if e["id"] == paper_id), None)
    if entry is None:
        return None
    for k, val in updates.items():
        if k in ("pinned", "completed"):
            entry[k] = bool(val)
    _save_index(items, v)
    _trigger_git_sync()
    return entry


def _set_paper_status(paper_id: str, status: str, vault: Optional[Vault] = None) -> dict | None:
    """Move a paper between inbox/later/archive/trash."""
    v = vault or _require_vault()
    items = _load_index(v)
    entry = next((e for e in items if e["id"] == paper_id), None)
    if entry is None:
        return None
    entry["status"] = status
    entry["deletedAt"] = time.time() if status == "trash" else None
    _save_index(items, v)
    _trigger_git_sync()
    return entry


def _touch_opened(paper_id: str, vault: Optional[Vault] = None) -> None:
    """Record that a paper's reader page was just served."""
    v = vault or _current_vault()
    if v is None:
        return
    try:
        items = _load_index(v)
        entry = next((e for e in items if e["id"] == paper_id), None)
        if entry is None:
            return
        entry["lastOpenedAt"] = time.time()
        _save_index(items, v)
    except OSError as e:
        # Never fail serving a paper because last-opened couldn't flush
        # (concurrent index writes used to raise FileNotFoundError here).
        print(f"[library] could not update lastOpenedAt for {paper_id}: {e}")


def rebuild_library(quiet: bool = False, vault: Optional[Vault] = None) -> tuple[int, int]:
    """Re-apply restyle() to every paper in the vault. Deferred until unlock
    when auth is enabled (DEK is not available at cold start)."""
    v = vault
    if v is None:
        if not _auth_enabled():
            v = _ensure_auth_disabled_vault()
        else:
            v = _current_vault()
    if v is None:
        if not quiet:
            print("[library] vault locked — rebuild deferred until sign-in")
        return 0, 0
    prev = _current_vault()
    _set_current_vault(v)
    try:
        items = _load_index(v)
        rebuilt = sum(1 for entry in items if _rebuild_paper(entry, v))
        skipped = len(items) - rebuilt
        if items:
            _save_index(items, v)
        if not quiet:
            msg = f"[library] rebuilt {rebuilt} paper(s) with the current reader styling"
            if skipped:
                msg += f"; skipped {skipped} (no stored raw source -- re-upload to enable rebuilding)"
            print(msg)
        return rebuilt, skipped
    finally:
        _set_current_vault(prev)


HOME_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath fill='%23c47a5a' d='M10 19h12l-1.4 9.2c-.1.8-.8 1.4-1.6 1.4h-6c-.8 0-1.5-.6-1.6-1.4L10 19z'/%3E%3Cellipse fill='%234a3728' cx='16' cy='19' rx='6.2' ry='2'/%3E%3Cpath fill='%233a6b4f' d='M16 19v-8' stroke='%233a6b4f' stroke-width='1.6' stroke-linecap='round'/%3E%3Cellipse fill='%234fa882' cx='11.5' cy='12' rx='4.2' ry='2.4' transform='rotate(-35 11.5 12)'/%3E%3Cellipse fill='%233d8b6e' cx='20.5' cy='11.5' rx='4.2' ry='2.4' transform='rotate(35 20.5 11.5)'/%3E%3Cellipse fill='%232f6f56' cx='16' cy='7.5' rx='3.2' ry='4.4'/%3E%3C/svg%3E">
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
@keyframes leafRustle {
  0%, 100% { transform: rotate(0deg); }
  20% { transform: rotate(4deg); }
  45% { transform: rotate(-2.5deg); }
  70% { transform: rotate(3deg); }
  85% { transform: rotate(-1.5deg); }
}
@keyframes leafRustleAlt {
  0%, 100% { transform: rotate(0deg); }
  25% { transform: rotate(-3.5deg); }
  50% { transform: rotate(2deg); }
  75% { transform: rotate(-2deg); }
}
@keyframes stemSway {
  0%, 100% { transform: rotate(0deg); }
  50% { transform: rotate(1.2deg); }
}
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
.sidebar-top {
  display: flex;
  align-items: center;
  gap: 0.55em;
  padding: 0 0.3em;
  margin-bottom: 1.4em;
}
.brand {
  display: flex; align-items: center; gap: 0.55em; flex: 1; min-width: 0;
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  font-weight: 600; font-size: 1.05em; line-height: 1.25;
}
.brand-text { min-width: 0; flex: 1; }
.brand-logo {
  width: 4.6em; height: 4.6em; flex-shrink: 0; display: block; overflow: visible;
}
.brand-logo .plant-foliage {
  transform-origin: 16px 22px;
  animation: stemSway 5.5s ease-in-out infinite;
}
.brand-logo .leaf {
  transform-box: fill-box;
  transform-origin: center bottom;
  animation: leafRustle 3.4s ease-in-out infinite;
}
.brand-logo .leaf-1 { animation-duration: 3.1s; animation-delay: 0s; }
.brand-logo .leaf-2 { animation-name: leafRustleAlt; animation-duration: 2.8s; animation-delay: -0.6s; }
.brand-logo .leaf-3 { animation-duration: 3.6s; animation-delay: -1.2s; }
.brand-logo .leaf-4 { animation-name: leafRustleAlt; animation-duration: 3.0s; animation-delay: -0.3s; }
.brand-logo .leaf-5 { animation-duration: 2.6s; animation-delay: -1.8s; }
@media (prefers-reduced-motion: reduce) {
  .brand-logo .plant-foliage,
  .brand-logo .leaf { animation: none !important; }
}
.add-paper-fab {
  position: absolute;
  right: max(1.4rem, 2.4vw);
  bottom: 1.6rem;
  z-index: 40;
  width: 72px;
  height: 72px;
  border-radius: 999px;
  border: 1px solid var(--rule);
  background: var(--fg);
  color: var(--bg);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.18);
  transition: transform 0.12s ease, box-shadow 0.15s ease, background-color 0.15s ease;
}
.add-paper-fab:hover {
  transform: scale(1.05);
  box-shadow: 0 14px 32px rgba(0, 0, 0, 0.22);
}
.add-paper-fab:active { transform: scale(0.96); }
.add-paper-fab:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.add-paper-fab svg { width: 32px; height: 32px; display: block; }
@media (prefers-color-scheme: dark) {
  .add-paper-fab { box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45); }
}
:root[data-theme="dark"] .add-paper-fab { box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45); }
:root[data-theme="light"] .add-paper-fab { box-shadow: 0 10px 28px rgba(0, 0, 0, 0.18); }
.sidebar-nav { display: flex; flex-direction: column; gap: 0.1em; flex: 1; min-height: 0; }
.nav-item {
  display: flex; align-items: center; gap: 0.5em; width: 100%; text-align: left; background: none; border: none; cursor: pointer;
  padding: 0.5em 0.6em; border-radius: 7px; font-size: 0.92em; color: var(--fg);
  transition: background-color 0.15s ease;
}
.nav-item:hover { background: var(--rule); }
.nav-item.active { background: var(--rule); font-weight: 600; }
.nav-item-sub { color: var(--muted); font-size: 0.85em; margin-left: auto; }
.nav-section-label {
  margin: 1.1em 0 0.3em; padding: 0 0.6em; font-size: 0.72em; font-weight: 600; letter-spacing: 0.05em;
  text-transform: uppercase; color: var(--muted);
}
.nav-tags { display: flex; flex-direction: column; gap: 0.05em; overflow-y: auto; min-height: 0; }
.nav-tags-empty { padding: 0.4em 0.6em; font-size: 0.82em; color: var(--muted); }

.collapse-icon { transition: transform 0.2s ease; opacity: 0.5; }
.nav-section-label:hover .collapse-icon { opacity: 1; }
.nav-section-label.collapsed .collapse-icon { transform: rotate(-90deg); }

.nav-tag-item {
  display: flex; align-items: center; gap: 0.4em; width: 100%; text-align: left; background: none; border: none; cursor: pointer;
  padding: 0.4em 0.6em; border-radius: 7px; font-size: 0.85em; color: var(--muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.nav-tag-item:hover { background: var(--rule); color: var(--fg); }
.nav-tag-item.active { background: var(--accent); color: #fff; }
.nav-tag-item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sidebar-bottom { margin-top: 0.8em; padding-top: 0.6em; border-top: 1px solid var(--rule); flex-shrink: 0; position: relative; }
.sidebar-footer { padding: 0.6em 0.6em 0.1em; font-size: 0.78em; color: var(--muted); }
.sidebar-footer a { color: var(--muted); text-decoration: underline; text-underline-offset: 2px; }
.sidebar-footer a:hover { color: var(--accent); }
.footer-sep { color: var(--rule); }

.prefs-overlay {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.2em;
  box-sizing: border-box;
}
.prefs-overlay[hidden] { display: none; }
.prefs-modal {
  width: min(90vw, 420px);
  max-height: calc(100vh - 2.4em);
  max-height: calc(100dvh - 2.4em);
  overflow-x: hidden;
  overflow-y: auto;
  overflow-wrap: anywhere;
  box-sizing: border-box;
  background: var(--control-bg, var(--bg));
  border: 1px solid var(--rule);
  border-radius: 12px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.3);
  padding: 1.5em;
  display: flex; flex-direction: column;
  min-width: 0;
}
.prefs-modal-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 1em; padding-bottom: 0.5em; border-bottom: 1px solid var(--rule);
}
.prefs-modal-header h2 {
  margin: 0; font-size: 1.2em; font-weight: 600;
}
.prefs-modal-close {
  background: none; border: none; font-size: 1.5em; line-height: 1; cursor: pointer; color: var(--muted);
}
.prefs-modal-close:hover { color: var(--fg); }
.prefs-popover-label {
  font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--muted); margin: 1em 0 0.4em 0;
}
.prefs-popover-row { display: flex; align-items: center; justify-content: space-between; padding: 0.35em 0 0.55em 0; }
.prefs-theme-grid { display: flex; gap: 0.6em; padding: 0 0 0.5em 0; }
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

/* ----------------------------------------------------------- account view */
.account-view { max-width: 420px; }
.account-view[hidden], .library-view[hidden] { display: none !important; }
.account-hero {
  display: flex; flex-direction: column; align-items: center; gap: 1.1em;
  padding: 0.4em 0 1.6em; border-bottom: 1px solid var(--rule); margin-bottom: 1.4em;
}
.account-avatar {
  width: 112px; height: 112px; border-radius: 50%; overflow: hidden;
  background: var(--rule); border: 2px solid var(--rule);
  display: flex; align-items: center; justify-content: center;
  font-size: 2.4em; font-weight: 600; color: var(--muted); position: relative;
}
.account-avatar img {
  width: 100%; height: 100%; object-fit: cover; display: block;
}
.account-avatar-actions { display: flex; gap: 0.5em; flex-wrap: wrap; justify-content: center; }
.account-field { display: flex; flex-direction: column; gap: 0.35em; margin-bottom: 1.1em; }
.account-field label {
  font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); font-weight: 600;
}
.account-field input {
  padding: 0.7em 0.85em; border-radius: 8px; border: 1px solid var(--rule);
  background: var(--card-bg); color: var(--fg); font-size: 0.95em; outline: none;
}
.account-field input:focus { border-color: var(--accent); }
.account-actions { display: flex; gap: 0.5em; align-items: center; }
.account-note {
  margin-top: 1.4em; font-size: 0.82em; color: var(--muted); line-height: 1.5;
}
.account-save-status { font-size: 0.82em; color: var(--muted); min-height: 1.2em; }
.account-save-status.error { color: var(--error); }
.avatar-crop-overlay {
  position: fixed; inset: 0; z-index: 80; background: rgba(0,0,0,0.55);
  display: flex; align-items: center; justify-content: center; padding: 1.2em;
}
.avatar-crop-overlay[hidden] { display: none; }
.avatar-crop-card {
  width: min(420px, 100%); background: var(--card-bg); color: var(--fg);
  border: 1px solid var(--rule); border-radius: 14px; padding: 1.1em 1.2em 1.2em;
  box-shadow: 0 16px 40px rgba(0,0,0,0.28);
}
.avatar-crop-card h2 { margin: 0 0 0.35em; font-size: 1.05em; font-weight: 600; }
.avatar-crop-card p { margin: 0 0 0.9em; font-size: 0.82em; color: var(--muted); }
.avatar-crop-stage {
  position: relative; width: 100%; aspect-ratio: 1; border-radius: 12px; overflow: hidden;
  background: #111; cursor: grab; touch-action: none; user-select: none;
}
.avatar-crop-stage.dragging { cursor: grabbing; }
.avatar-crop-stage canvas { display: block; width: 100%; height: 100%; }
.avatar-crop-mask {
  position: absolute; inset: 0; pointer-events: none;
  background: radial-gradient(circle closest-side, transparent 66%, rgba(0,0,0,0.55) 67%);
}
.avatar-crop-controls { margin-top: 0.9em; display: flex; flex-direction: column; gap: 0.7em; }
.avatar-crop-controls input[type=range] { width: 100%; }
.avatar-crop-actions { display: flex; gap: 0.5em; justify-content: flex-end; }

/* --------------------------------------------------------------- main col */
.main-col {
  flex: 1; min-width: 0; overflow: hidden; position: relative;
  display: flex; flex-direction: column;
}
.main-col-scroll {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: 2.4em 3.2vw 8vh;
}
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
.paper-list { display: flex; flex-direction: column; padding-bottom: 5.5rem; }
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
.info-hl-author {
  display: flex; align-items: center; gap: 0.45em; margin-bottom: 0.5em;
  font-size: 0.78em; color: var(--muted);
}
.info-hl-author-avatar {
  width: 22px; height: 22px; border-radius: 50%; overflow: hidden; flex-shrink: 0;
  background: var(--rule); color: var(--fg); font-size: 0.72em; font-weight: 600;
  display: inline-flex; align-items: center; justify-content: center;
}
.info-hl-author-avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
.info-hl-author-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--fg); font-weight: 600; }
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
        <svg class="brand-logo" viewBox="0 0 32 32" width="72" height="72" aria-hidden="true" focusable="false">
          <!-- terracotta pot -->
          <path fill="#c47a5a" d="M9.5 19.2h13l-1.55 9.1c-.15.9-.9 1.55-1.8 1.55h-6.3c-.9 0-1.65-.65-1.8-1.55L9.5 19.2z"/>
          <path fill="#a86548" d="M9.5 19.2h13l-.35 2.05H9.85z" opacity=".55"/>
          <ellipse fill="#4a3728" cx="16" cy="19.1" rx="6.6" ry="2.15"/>
          <ellipse fill="#3a2a1f" cx="16" cy="18.6" rx="5.4" ry="1.35" opacity=".55"/>
          <!-- foliage group: gentle stem sway -->
          <g class="plant-foliage">
            <path fill="none" stroke="#3a6b4f" stroke-width="1.7" stroke-linecap="round"
                  d="M16 19.1c0-3.2.15-6.4.1-9.5"/>
            <!-- left mid leaf -->
            <g class="leaf leaf-1">
              <ellipse fill="#4fa882" cx="10.8" cy="12.2" rx="5.1" ry="2.55" transform="rotate(-42 10.8 12.2)"/>
              <path fill="none" stroke="#2f6f56" stroke-width=".7" stroke-linecap="round"
                    d="M14.6 14.4c-1.6-1.1-3.2-2-5.1-2.5" opacity=".55"/>
            </g>
            <!-- right mid leaf -->
            <g class="leaf leaf-2">
              <ellipse fill="#3d8b6e" cx="21.2" cy="11.6" rx="5.1" ry="2.55" transform="rotate(40 21.2 11.6)"/>
              <path fill="none" stroke="#2a5f4a" stroke-width=".7" stroke-linecap="round"
                    d="M17.4 13.8c1.6-1.1 3.2-2 5.1-2.5" opacity=".55"/>
            </g>
            <!-- top center leaf -->
            <g class="leaf leaf-3">
              <ellipse fill="#2f6f56" cx="16" cy="6.6" rx="3.35" ry="5.1"/>
              <path fill="none" stroke="#245a45" stroke-width=".75" stroke-linecap="round"
                    d="M16 10.8V3.4" opacity=".5"/>
            </g>
            <!-- small left upper leaf -->
            <g class="leaf leaf-4">
              <ellipse fill="#5bb892" cx="12.2" cy="8.4" rx="3.3" ry="1.75" transform="rotate(-55 12.2 8.4)"/>
            </g>
            <!-- small right upper leaf -->
            <g class="leaf leaf-5">
              <ellipse fill="#458f70" cx="19.8" cy="7.9" rx="3.2" ry="1.7" transform="rotate(52 19.8 7.9)"/>
            </g>
          </g>
        </svg>
        <span class="brand-text">Andrew&rsquo;s Paper Library</span>
      </span>
    </div>
    <nav class="sidebar-nav">
      <button type="button" class="nav-item" id="navHome">Home</button>
      <div class="nav-section-label" id="navPinnedLabel" style="cursor:pointer; display:flex; justify-content:space-between; align-items:center; user-select:none;" hidden>Pinned <svg class="collapse-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg></div>
      <div class="nav-tags" id="navPinned" hidden></div>
      <div class="nav-section-label" id="navTagsLabel" style="cursor:pointer; display:flex; justify-content:space-between; align-items:center; user-select:none;">Tags <svg class="collapse-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg></div>
      <div class="nav-tags" id="navTags"></div>
    </nav>
    <div class="sidebar-bottom">
      <button type="button" class="nav-item" id="navAccountBtn">Account</button>
      <button type="button" class="nav-item" id="navSearchBtn" title="Search papers (/)">Search</button>
      <button type="button" class="nav-item" id="navPrefsBtn">Preferences <span class="nav-item-sub" id="prefsThemeLabel">Auto</span></button>
      
      <div class="sidebar-footer">
        <a href="/pipeline">Pipeline</a>
        <span class="footer-sep">&middot;</span>
        <a href="/about">About</a>
        <span class="footer-sep">&middot;</span>
        <a href="/guide">Guide</a>
        <span class="footer-sep">&middot;</span>
        <a href="https://github.com/andrewluoooo/paper-reader">GitHub</a>
      </div>
    </div>
  </aside>

  <div class="prefs-overlay" id="prefsOverlay" hidden>
    <div class="prefs-modal" id="prefsModal">
      <div class="prefs-modal-header">
        <h2>Preferences</h2>
        <button class="prefs-modal-close" id="prefsCloseBtn">&times;</button>
      </div>
      <div class="prefs-popover-label">Theme</div>
      <div class="prefs-theme-grid" id="prefsThemeGrid">
        <button type="button" data-value="auto">Auto</button>
        <button type="button" data-value="light">Light</button>
        <button type="button" data-value="dark">Dark</button>
      </div>
      <div class="prefs-popover-label">PDF Parser</div>
      <div class="prefs-theme-grid" id="prefsPdfParserGrid">
        <button type="button" data-value="docling">Docling</button>
        <button type="button" data-value="mineru">MinerU Cloud</button>
      </div>
      <div class="prefs-popover-row" id="prefsMineruTokenRow" style="flex-direction: column; align-items: stretch; gap: 0.35em; display: none;">
        <span style="font-size: 0.78em; color: var(--muted); line-height: 1.4;">
          Free-tier token from
          <a href="https://mineru.net/user-center/api-token" target="_blank" rel="noopener" style="color: var(--accent);">mineru.net</a>
          (PDFs are uploaded to MinerU&rsquo;s cloud). Or set <code>MINERU_API_TOKEN</code>.
        </span>
        <input type="password" id="prefsMineruToken" class="prefs-input" placeholder="MinerU API token" autocomplete="off" spellcheck="false">
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
      <!--AUTH_LOGOUT_SLOT-->
    </div>
  </div>

  <main class="main-col">
    <div class="main-col-scroll">
    <div id="libraryView" class="library-view">
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

    <div class="search-row" id="searchRow" style="position: relative;" hidden>
      <input type="text" id="searchBox" placeholder="Search papers by title or author..." style="padding-right: 32px;">
      <button type="button" id="searchCancelBtn" aria-label="Cancel search" style="position: absolute; right: 6px; top: 50%; transform: translateY(-50%); background: transparent; border: none; font-size: 1.2em; color: var(--muted); cursor: pointer; padding: 0.2em 0.4em; display: flex; align-items: center; justify-content: center;">&times;</button>
    </div>

    <div class="status" id="status"></div>

    <div class="paper-list" id="paperList"></div>
    </div>

    <div id="accountView" class="account-view" hidden>
      <div class="main-topbar">
        <div class="topbar-title">Account</div>
      </div>
      <div class="account-hero">
        <div class="account-avatar" id="accountAvatar" aria-hidden="true">?</div>
        <div class="account-avatar-actions">
          <button type="button" class="prefs-btn" id="accountAvatarBtn" style="padding: 0.45em 0.9em;">Upload photo</button>
          <button type="button" class="prefs-btn" id="accountAvatarRemoveBtn" style="padding: 0.45em 0.9em;" hidden>Remove</button>
        </div>
      </div>
      <div class="account-field">
        <label for="accountDisplayName">Display name</label>
        <input type="text" id="accountDisplayName" maxlength="64" placeholder="Your name" autocomplete="nickname">
      </div>
      <div class="account-actions">
        <button type="button" class="prefs-btn" id="accountSaveBtn" style="padding: 0.55em 1.1em; flex: 0 0 auto;">Save</button>
        <span class="account-save-status" id="accountSaveStatus"></span>
      </div>
      <p class="account-note">Your secret key is never shown here and cannot be changed from this page. Keep a private offline copy of the key you already saved.</p>
    </div>
    </div>
    <button type="button" class="add-paper-fab" id="addPaperBtn" aria-label="Add a paper" title="Add a paper (LaTeX source, PDF, EPUB, or saved HTML page)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <line x1="12" y1="5" x2="12" y2="19"></line>
        <line x1="5" y1="12" x2="19" y2="12"></line>
      </svg>
    </button>
  </main>

  <aside class="info-panel" id="infoPanel" hidden>
    <div class="info-panel-top">
      <span>Notes & Info</span>
    </div>
    <div class="info-panel-body" id="infoPanelBody"></div>
  </aside>
</div>

<input type="file" id="fileInput" accept=".tex,.zip,.tar.gz,.tgz,.tar,.html,.htm,.pdf,.epub,application/epub+zip">
<input type="file" id="avatarFileInput" accept="image/jpeg,image/png,image/webp,image/gif,.jpg,.jpeg,.png,.webp,.gif">

<div class="avatar-crop-overlay" id="avatarCropOverlay" hidden>
  <div class="avatar-crop-card" role="dialog" aria-modal="true" aria-labelledby="avatarCropTitle">
    <h2 id="avatarCropTitle">Crop profile photo</h2>
    <p>Drag to reposition. Use the slider to zoom. The circle is what gets saved.</p>
    <div class="avatar-crop-stage" id="avatarCropStage">
      <canvas id="avatarCropCanvas" width="360" height="360"></canvas>
      <div class="avatar-crop-mask" aria-hidden="true"></div>
    </div>
    <div class="avatar-crop-controls">
      <label for="avatarCropZoom" style="font-size: 0.78em; color: var(--muted);">Zoom</label>
      <input type="range" id="avatarCropZoom" min="1" max="3" step="0.01" value="1">
      <div class="avatar-crop-actions">
        <button type="button" class="prefs-btn" id="avatarCropCancel" style="padding: 0.45em 0.9em; flex: 0 0 auto;">Cancel</button>
        <button type="button" class="prefs-btn" id="avatarCropApply" style="padding: 0.45em 0.9em; flex: 0 0 auto;">Apply</button>
      </div>
    </div>
  </div>
</div>

<div class="drop-overlay" id="dropOverlay" hidden>
  <div class="drop-overlay-card">
    <strong>Drop to add to your library</strong>
    <div>.tex, .zip, .tar.gz, .tgz, .pdf, .epub &mdash; or a saved .html paper page</div>
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
  var ACCOUNT_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>';
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
    var pop = document.getElementById("prefsOverlay");
    var popModal = document.getElementById("prefsModal");
    var closeBtn = document.getElementById("prefsCloseBtn");
    var themeGrid = document.getElementById("prefsThemeGrid");
    var pdfParserGrid = document.getElementById("prefsPdfParserGrid");
    var vimToggle = document.getElementById("prefsVimNavToggle");
    if (!btn || !pop) return;

    function setActiveTheme(val) {
      if (!themeGrid) return;
      themeGrid.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("active", b.dataset.value === val);
      });
    }
    setActiveTheme(loadSettings().theme || "auto");
    
    var mineruTokenRow = document.getElementById("prefsMineruTokenRow");
    var mineruTokenInput = document.getElementById("prefsMineruToken");
    function setActivePdfParser(val) {
      if (!pdfParserGrid) return;
      pdfParserGrid.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("active", b.dataset.value === val);
      });
      if (mineruTokenRow) mineruTokenRow.style.display = val === "mineru" ? "flex" : "none";
    }
    setActivePdfParser(loadSettings().pdfParser || "docling");
    if (mineruTokenInput) {
      mineruTokenInput.value = loadSettings().mineruApiToken || "";
      mineruTokenInput.addEventListener("input", function () {
        saveSettings({ mineruApiToken: mineruTokenInput.value.trim() });
      });
    }

    function openPrefs() { pop.hidden = false; }
    function closePrefs() { pop.hidden = true; }

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      openPrefs();
    });
    if (closeBtn) {
      closeBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        closePrefs();
      });
    }
    document.addEventListener("click", function (e) {
      if (!pop.hidden && !popModal.contains(e.target) && e.target !== btn) closePrefs();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !pop.hidden) closePrefs();
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

    if (pdfParserGrid) {
      pdfParserGrid.querySelectorAll("button").forEach(function (b) {
        b.addEventListener("click", function () {
          var val = b.dataset.value;
          saveSettings({ pdfParser: val });
          setActivePdfParser(val);
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

    var exportBtn = document.getElementById("prefsExportBtn");
    if (exportBtn) {
      exportBtn.addEventListener("click", function () {
        exportBtn.disabled = true;
        fetch("/api/export", { credentials: "same-origin" })
          .then(function (r) {
            if (!r.ok) throw new Error("export failed");
            return r.blob();
          })
          .then(function (blob) {
            var a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = "paper-library-export.zip";
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
          })
          .catch(function () { alert("Could not export library"); })
          .finally(function () { exportBtn.disabled = false; });
      });
    }

    var logoutBtn = document.getElementById("prefsLogoutBtn");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", function () {
        fetch("/api/logout", { method: "POST", credentials: "same-origin" })
          .finally(function () { window.location.href = "/signup"; });
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
    var typeLabel =
      (srcLower.slice(-5) === ".html" || srcLower.slice(-4) === ".htm") ? "HTML import" :
      (srcLower.slice(-4) === ".pdf") ? "PDF import" :
      (srcLower.slice(-5) === ".epub") ? "EPUB import" :
      "LaTeX source";
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

        var authorName = (h.authorName || "").trim();
        if (authorName || h.authorHasAvatar) {
          var author = document.createElement("div");
          author.className = "info-hl-author";
          var av = document.createElement("div");
          av.className = "info-hl-author-avatar";
          av.setAttribute("aria-hidden", "true");
          if (h.authorHasAvatar) {
            var img = document.createElement("img");
            img.alt = "";
            img.src = "/api/account/avatar";
            img.addEventListener("error", function () {
              av.textContent = (authorName || "?").charAt(0).toUpperCase();
            });
            av.appendChild(img);
          } else {
            av.textContent = (authorName || "?").charAt(0).toUpperCase();
          }
          author.appendChild(av);
          if (authorName) {
            var nm = document.createElement("span");
            nm.className = "info-hl-author-name";
            nm.textContent = authorName;
            author.appendChild(nm);
          }
          item.appendChild(author);
        }

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
      if (!papers.length) emptyMsg = "No papers yet \u2014 drop a LaTeX source, PDF, EPUB, or saved HTML paper page anywhere on this page to get started.";
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
    var PULL_THRESHOLD = 300;
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
      
      var mainCol = document.querySelector(".main-col-scroll") || document.querySelector(".main-col");
      var currentScrollY = mainCol ? mainCol.scrollTop : 0;
      
      if (currentScrollY > 0) {
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
      '<span class="undo-toast-spinner"></span>Parsing "' + escHtml(file.name) + '"\\u2026 this can take a few minutes.',
      "loading"
    );
    var pdfParser = loadSettings().pdfParser || "docling";
    var uploadHeaders = {};
    if (pdfParser === "mineru") {
      var mineruTok = (loadSettings().mineruApiToken || "").trim();
      if (mineruTok) uploadHeaders["X-MinerU-Token"] = mineruTok;
    }
    fetch("/api/upload?filename=" + encodeURIComponent(file.name) + "&pdfParser=" + encodeURIComponent(pdfParser), {
      method: "POST",
      body: file,
      headers: uploadHeaders,
      credentials: "same-origin"
    })
      .then(function (r) {
        return r.text().then(function (text) {
          var data = {};
          if (text) {
            try { data = JSON.parse(text); }
            catch (e) {
              throw new Error(
                r.status === 401 || r.status === 302
                  ? "session expired — refresh and sign in again"
                  : ("server returned non-JSON (HTTP " + r.status + ")")
              );
            }
          }
          return { ok: r.ok, status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.ok) {
          if (res.status === 401 || (res.data && res.data.error === "unauthorized")) {
            showNoticeToast(
              'Session expired — <a href="/login?next=/">sign in</a> and try the upload again.',
              "error", 10000, toast
            );
            return;
          }
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
  document.getElementById("searchCancelBtn").addEventListener("click", function () {
    searchOpen = false;
    searchRow.hidden = true;
    searchBox.value = "";
    render("");
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
    showLibraryView();
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

  var currentMainView = "library";
  function setNavActive() {
    var home = document.getElementById("navHome");
    var acct = document.getElementById("navAccountBtn");
    if (home) home.classList.toggle("active", currentMainView === "library");
    if (acct) acct.classList.toggle("active", currentMainView === "account");
  }
  function showLibraryView() {
    currentMainView = "library";
    document.getElementById("libraryView").hidden = false;
    document.getElementById("accountView").hidden = true;
    document.getElementById("addPaperBtn").hidden = false;
    document.getElementById("infoPanel").hidden = document.getElementById("infoPanel").hidden;
    setNavActive();
  }
  function showAccountView() {
    currentMainView = "account";
    closeInfoPanel();
    var pop = document.getElementById("prefsOverlay");
    if (pop) pop.hidden = true;
    document.getElementById("libraryView").hidden = true;
    document.getElementById("accountView").hidden = false;
    document.getElementById("addPaperBtn").hidden = true;
    setNavActive();
    loadAccountProfile();
  }

  function initialFromName(name) {
    var t = (name || "").trim();
    return t ? t.charAt(0).toUpperCase() : "?";
  }
  function renderAccountAvatar(hasAvatar, displayName) {
    var el = document.getElementById("accountAvatar");
    var removeBtn = document.getElementById("accountAvatarRemoveBtn");
    if (!el) return;
    el.innerHTML = "";
    if (hasAvatar) {
      var img = document.createElement("img");
      img.alt = "";
      img.src = "/api/account/avatar?t=" + Date.now();
      el.appendChild(img);
      if (removeBtn) removeBtn.hidden = false;
    } else {
      el.textContent = initialFromName(displayName);
      if (removeBtn) removeBtn.hidden = true;
    }
  }
  function loadAccountProfile() {
    var status = document.getElementById("accountSaveStatus");
    if (status) { status.textContent = ""; status.classList.remove("error"); }
    fetch("/api/account", { credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.data.error || "failed");
        var name = res.data.displayName || "";
        document.getElementById("accountDisplayName").value = name;
        renderAccountAvatar(!!res.data.hasAvatar, name);
      })
      .catch(function () {
        if (status) {
          status.textContent = "Could not load account";
          status.classList.add("error");
        }
      });
  }

  function initAccountView() {
    var navAccount = document.getElementById("navAccountBtn");
    if (!navAccount) return;
    navAccount.addEventListener("click", function () { showAccountView(); });

    document.getElementById("accountSaveBtn").addEventListener("click", function () {
      var status = document.getElementById("accountSaveStatus");
      var name = document.getElementById("accountDisplayName").value;
      status.textContent = "Saving…";
      status.classList.remove("error");
      fetch("/api/account", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ displayName: name })
      }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
        .then(function (res) {
          if (!res.ok) throw new Error(res.data.error || "save failed");
          document.getElementById("accountDisplayName").value = res.data.displayName || "";
          var el = document.getElementById("accountAvatar");
          if (el && !el.querySelector("img")) el.textContent = initialFromName(res.data.displayName);
          status.textContent = "Saved";
          restampAllStoredHighlights(res.data);
        })
        .catch(function (e) {
          status.textContent = e.message || "Could not save";
          status.classList.add("error");
        });
    });

    var avatarInput = document.getElementById("avatarFileInput");
    document.getElementById("accountAvatarBtn").addEventListener("click", function () {
      avatarInput.value = "";
      avatarInput.click();
    });
    document.getElementById("accountAvatarRemoveBtn").addEventListener("click", function () {
      fetch("/api/account/avatar", { method: "DELETE", credentials: "same-origin" })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
        .then(function (res) {
          if (!res.ok) throw new Error(res.data.error || "remove failed");
          renderAccountAvatar(false, document.getElementById("accountDisplayName").value);
          restampAllStoredHighlights({
            displayName: document.getElementById("accountDisplayName").value,
            hasAvatar: false
          });
        })
        .catch(function () { alert("Could not remove photo"); });
    });

    var crop = {
      img: null,
      scale: 1,
      minScale: 1,
      offsetX: 0,
      offsetY: 0,
      dragging: false,
      lastX: 0,
      lastY: 0
    };
    var overlay = document.getElementById("avatarCropOverlay");
    var stage = document.getElementById("avatarCropStage");
    var canvas = document.getElementById("avatarCropCanvas");
    var ctx = canvas.getContext("2d");
    var zoom = document.getElementById("avatarCropZoom");

    function drawCrop() {
      var w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "#111";
      ctx.fillRect(0, 0, w, h);
      if (!crop.img) return;
      var dw = crop.img.width * crop.scale;
      var dh = crop.img.height * crop.scale;
      var dx = (w - dw) / 2 + crop.offsetX;
      var dy = (h - dh) / 2 + crop.offsetY;
      ctx.drawImage(crop.img, dx, dy, dw, dh);
    }
    function openCrop(file) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        URL.revokeObjectURL(url);
        crop.img = img;
        var fit = Math.max(canvas.width / img.width, canvas.height / img.height);
        crop.minScale = fit;
        crop.scale = fit;
        crop.offsetX = 0;
        crop.offsetY = 0;
        zoom.min = "1";
        zoom.max = "3";
        zoom.value = "1";
        overlay.hidden = false;
        drawCrop();
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        alert("Could not read that image");
      };
      img.src = url;
    }
    function closeCrop() {
      overlay.hidden = true;
      crop.img = null;
    }
    function effectiveScale() {
      return crop.minScale * parseFloat(zoom.value || "1");
    }
    avatarInput.addEventListener("change", function () {
      var f = avatarInput.files && avatarInput.files[0];
      if (!f) return;
      if (!/^image\\/(jpeg|png|webp|gif)$/i.test(f.type) && !/\\.(jpe?g|png|webp|gif)$/i.test(f.name)) {
        alert("Choose a JPEG, PNG, WebP, or GIF image");
        return;
      }
      openCrop(f);
    });
    zoom.addEventListener("input", function () {
      crop.scale = effectiveScale();
      drawCrop();
    });
    stage.addEventListener("pointerdown", function (e) {
      if (!crop.img) return;
      crop.dragging = true;
      stage.classList.add("dragging");
      crop.lastX = e.clientX;
      crop.lastY = e.clientY;
      stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener("pointermove", function (e) {
      if (!crop.dragging) return;
      crop.offsetX += e.clientX - crop.lastX;
      crop.offsetY += e.clientY - crop.lastY;
      crop.lastX = e.clientX;
      crop.lastY = e.clientY;
      drawCrop();
    });
    function endDrag() {
      crop.dragging = false;
      stage.classList.remove("dragging");
    }
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);
    document.getElementById("avatarCropCancel").addEventListener("click", closeCrop);
    document.getElementById("avatarCropApply").addEventListener("click", function () {
      if (!crop.img) return;
      var out = document.createElement("canvas");
      out.width = 256;
      out.height = 256;
      var octx = out.getContext("2d");
      octx.fillStyle = "#111";
      octx.fillRect(0, 0, 256, 256);
      octx.save();
      octx.beginPath();
      octx.arc(128, 128, 128, 0, Math.PI * 2);
      octx.closePath();
      octx.clip();
      var scaleRatio = 256 / canvas.width;
      var dw = crop.img.width * crop.scale * scaleRatio;
      var dh = crop.img.height * crop.scale * scaleRatio;
      var dx = (256 - dw) / 2 + crop.offsetX * scaleRatio;
      var dy = (256 - dh) / 2 + crop.offsetY * scaleRatio;
      octx.drawImage(crop.img, dx, dy, dw, dh);
      octx.restore();
      out.toBlob(function (blob) {
        if (!blob) { alert("Could not crop image"); return; }
        fetch("/api/account/avatar", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "image/jpeg" },
          body: blob
        }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
          .then(function (res) {
            if (!res.ok) throw new Error(res.data.error || "upload failed");
            closeCrop();
            renderAccountAvatar(true, document.getElementById("accountDisplayName").value);
            restampAllStoredHighlights({
              displayName: document.getElementById("accountDisplayName").value,
              hasAvatar: true
            });
          })
          .catch(function (e) { alert(e.message || "Could not upload photo"); });
      }, "image/jpeg", 0.92);
    });
    setNavActive();
  }
  document.addEventListener("keydown", function (e) {
    if (!document.getElementById("avatarCropOverlay").hidden) {
      if (e.key === "Escape") {
        e.preventDefault();
        document.getElementById("avatarCropOverlay").hidden = true;
      }
      return;
    }
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
  initAccountView();
  initPullToRefresh();

  function restampAllStoredHighlights(profile) {
    var name = ((profile && profile.displayName) || "").trim() || "You";
    var hasAvatar = !!(profile && profile.hasAvatar);
    var keys = [];
    for (var i = 0; i < localStorage.length; i++) {
      var k = localStorage.key(i);
      if (k && k.indexOf("paper_reader_highlights::") === 0) keys.push(k);
    }
    var touched = 0;
    keys.forEach(function (key) {
      try {
        var list = JSON.parse(localStorage.getItem(key) || "[]");
        if (!Array.isArray(list) || !list.length) return;
        var changed = false;
        list.forEach(function (h) {
          if (!h || typeof h !== "object") return;
          var theirs = (h.authorName ? String(h.authorName) : "").trim();
          // Only restamp highlights already owned by this account (or legacy
          // unmarked when signed in as anonymous "You"). Never rewrite others.
          var isOwn = theirs ? theirs === name : name === "You";
          if (!isOwn) return;
          if (h.authorName !== name || !!h.authorHasAvatar !== hasAvatar) {
            h.authorName = name;
            h.authorHasAvatar = hasAvatar;
            changed = true;
          }
        });
        if (changed) {
          localStorage.setItem(key, JSON.stringify(list));
          touched += 1;
        }
      } catch (e) {}
    });
    return touched;
  }
  fetch("/api/account", { credentials: "same-origin" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (profile) {
      if (profile) restampAllStoredHighlights(profile);
    })
    .catch(function () {});
  
  // Inject icons into static elements
  document.getElementById("navHome").innerHTML = HOME_ICON + "<span>Home</span>";
  document.getElementById("navAccountBtn").innerHTML = ACCOUNT_ICON + "<span>Account</span>";
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

  function initCollapsible(labelId, contentId, storageKey) {
    var label = document.getElementById(labelId);
    var content = document.getElementById(contentId);
    if (!label || !content) return;
    
    var isCollapsed = localStorage.getItem(storageKey) === "true";
    if (isCollapsed) {
      label.classList.add("collapsed");
      content.style.display = "none";
    }
    
    label.addEventListener("click", function() {
      isCollapsed = !isCollapsed;
      localStorage.setItem(storageKey, isCollapsed);
      if (isCollapsed) {
        label.classList.add("collapsed");
        content.style.display = "none";
      } else {
        label.classList.remove("collapsed");
        content.style.display = "";
      }
    });
  }
  
  initCollapsible("navPinnedLabel", "navPinned", "paper_reader_pinned_collapsed");
  initCollapsible("navTagsLabel", "navTags", "paper_reader_tags_collapsed");

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
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath fill='%23c47a5a' d='M10 19h12l-1.4 9.2c-.1.8-.8 1.4-1.6 1.4h-6c-.8 0-1.5-.6-1.6-1.4L10 19z'/%3E%3Cellipse fill='%234a3728' cx='16' cy='19' rx='6.2' ry='2'/%3E%3Cpath fill='%233a6b4f' d='M16 19v-8' stroke='%233a6b4f' stroke-width='1.6' stroke-linecap='round'/%3E%3Cellipse fill='%234fa882' cx='11.5' cy='12' rx='4.2' ry='2.4' transform='rotate(-35 11.5 12)'/%3E%3Cellipse fill='%233d8b6e' cx='20.5' cy='11.5' rx='4.2' ry='2.4' transform='rotate(35 20.5 11.5)'/%3E%3Cellipse fill='%232f6f56' cx='16' cy='7.5' rx='3.2' ry='4.4'/%3E%3C/svg%3E">
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
  <p>pdfs are inherently flawed because they don't contain any semantic meaning. this is really bad when the whole point of the document is to transfer semantic meaning, which is why open formats like markdown, html, xml, and epub are the best way moving forward.</p>
</div>
</body>
</html>
"""

GUIDE_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath fill='%23c47a5a' d='M10 19h12l-1.4 9.2c-.1.8-.8 1.4-1.6 1.4h-6c-.8 0-1.5-.6-1.6-1.4L10 19z'/%3E%3Cellipse fill='%234a3728' cx='16' cy='19' rx='6.2' ry='2'/%3E%3Cpath fill='%233a6b4f' d='M16 19v-8' stroke='%233a6b4f' stroke-width='1.6' stroke-linecap='round'/%3E%3Cellipse fill='%234fa882' cx='11.5' cy='12' rx='4.2' ry='2.4' transform='rotate(-35 11.5 12)'/%3E%3Cellipse fill='%233d8b6e' cx='20.5' cy='11.5' rx='4.2' ry='2.4' transform='rotate(35 20.5 11.5)'/%3E%3Cellipse fill='%232f6f56' cx='16' cy='7.5' rx='3.2' ry='4.4'/%3E%3C/svg%3E">
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
<title>Guide &mdash; Andrew's Paper Library</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db;
  --card-bg: #fbfaf8;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #1e1e1e; --fg: #ece9e2; --muted: #a9a49a; --rule: #444444; --accent: #7fa7ff; --card-bg: #2a2a2a; }
}
:root[data-theme="light"] { --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db; --card-bg: #fbfaf8; }
:root[data-theme="dark"] { --bg: #1e1e1e; --fg: #ece9e2; --muted: #a9a49a; --rule: #444444; --accent: #7fa7ff; --card-bg: #2a2a2a; }
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 640px; margin: 0 auto; padding: 9vh 6vw 12vh; }
.back-link {
  display: inline-flex; align-items: center; gap: 0.4em; color: var(--muted); text-decoration: none;
  font-size: 0.85em; margin-bottom: 2.5em;
}
.back-link:hover { color: var(--accent); }
.back-link svg { display: block; }
h1 { font-size: 1.6em; margin: 0 0 0.35em; }
.lede { color: var(--muted); line-height: 1.6; margin: 0 0 2em; }
h2 { font-size: 1.05em; margin: 1.8em 0 0.55em; }
p, li { line-height: 1.65; font-size: 0.98em; }
p { margin: 0 0 0.75em; }
ul { margin: 0 0 0.75em; padding-left: 1.2em; }
li { margin: 0.25em 0; }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.88em; background: var(--card-bg); border: 1px solid var(--rule);
  border-radius: 4px; padding: 0.1em 0.35em;
}
pre {
  background: var(--card-bg); border: 1px solid var(--rule); border-radius: 8px;
  padding: 0.85em 1em; overflow-x: auto; margin: 0 0 1em; font-size: 0.88em; line-height: 1.5;
}
pre code { background: none; border: none; padding: 0; font-size: inherit; }
table { width: 100%; border-collapse: collapse; margin: 0 0 1em; font-size: 0.92em; }
th, td { text-align: left; padding: 0.45em 0.5em; border-bottom: 1px solid var(--rule); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 0.85em; }
a { color: var(--accent); }
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
  <h1>Guide</h1>
  <p class="lede">A local library for reading research papers as clean, reflowable HTML.</p>

  <h2>Start</h2>
  <pre><code>paper-reader --library</code></pre>
  <p>Opens <code>http://127.0.0.1:8765</code>. Papers live in <code>~/.paper_reader_library</code>.</p>
  <pre><code>paper-reader --stop-library
paper-reader --rebuild-library</code></pre>

  <h2>Add papers</h2>
  <p>Use the <strong>+</strong> button (bottom-right), or drag a file onto the library page.</p>
  <table>
    <thead><tr><th>Format</th><th>Notes</th></tr></thead>
    <tbody>
      <tr><td><code>.tex</code> / <code>.zip</code> / <code>.tar.gz</code></td><td>LaTeX source (e.g. arXiv &ldquo;Other formats &rarr; Source&rdquo;)</td></tr>
      <tr><td><code>.html</code> / <code>.htm</code></td><td>Saved publisher page (Save Page As &rarr; Webpage, Complete)</td></tr>
      <tr><td><code>.pdf</code></td><td>Needs <a href="https://github.com/kermitt2/grobid">GROBID</a> running locally</td></tr>
      <tr><td><code>.epub</code></td><td>Ebook &rarr; same reader format</td></tr>
    </tbody>
  </table>
  <p>One-shot CLI (no library):</p>
  <pre><code>paper-reader path/to/paper.tar.gz -o paper.html</code></pre>

  <h2>Organize</h2>
  <ul>
    <li><strong>Tabs:</strong> Inbox &middot; Later &middot; Completed &middot; Archive &middot; Trash</li>
    <li><strong>Pin</strong> papers for the sidebar; <strong>tags</strong> filter the list</li>
    <li><strong>Search</strong> (<code>/</code> or sidebar) by title/author</li>
    <li>Drag a card onto a tab (or Pinned) to move it</li>
  </ul>

  <h2>Read</h2>
  <ul>
    <li>Click text to highlight; add optional notes</li>
    <li>Hover citations / figures / tables for previews</li>
    <li>Theme, font size, and column width in the reader chrome</li>
    <li>Highlights stay in browser <code>localStorage</code> (per paper)</li>
  </ul>

  <h2>Sync (optional)</h2>
  <p>Preferences &rarr; paste a git remote &rarr; <strong>Setup</strong>, then <strong>Sync Now</strong> (or pull-to-refresh on the library) to backup/sync the library folder.</p>

  <h2>Account &amp; vaults</h2>
  <p>Accounts are anonymous secret keys &mdash; no email or username. Each key unlocks its own encrypted vault on this device. Generate a key and save it; it is shown once and cannot be recovered. A new key starts an empty vault; your previous key still opens its vault. Export a plaintext ZIP from Preferences. Log out from Preferences.</p>
  <p>To leave the library open (no sign-in), start with:</p>
  <pre><code>export PAPER_READER_DISABLE_AUTH=1
paper-reader --library</code></pre>
</div>
</body>
</html>
"""

PIPELINE_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath fill='%23c47a5a' d='M10 19h12l-1.4 9.2c-.1.8-.8 1.4-1.6 1.4h-6c-.8 0-1.5-.6-1.6-1.4L10 19z'/%3E%3Cellipse fill='%234a3728' cx='16' cy='19' rx='6.2' ry='2'/%3E%3Cpath fill='%233a6b4f' d='M16 19v-8' stroke='%233a6b4f' stroke-width='1.6' stroke-linecap='round'/%3E%3Cellipse fill='%234fa882' cx='11.5' cy='12' rx='4.2' ry='2.4' transform='rotate(-35 11.5 12)'/%3E%3Cellipse fill='%233d8b6e' cx='20.5' cy='11.5' rx='4.2' ry='2.4' transform='rotate(35 20.5 11.5)'/%3E%3Cellipse fill='%232f6f56' cx='16' cy='7.5' rx='3.2' ry='4.4'/%3E%3C/svg%3E">
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

LOGIN_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath fill='%23c47a5a' d='M10 19h12l-1.4 9.2c-.1.8-.8 1.4-1.6 1.4h-6c-.8 0-1.5-.6-1.6-1.4L10 19z'/%3E%3Cellipse fill='%234a3728' cx='16' cy='19' rx='6.2' ry='2'/%3E%3Cpath fill='%233a6b4f' d='M16 19v-8' stroke='%233a6b4f' stroke-width='1.6' stroke-linecap='round'/%3E%3Cellipse fill='%234fa882' cx='11.5' cy='12' rx='4.2' ry='2.4' transform='rotate(-35 11.5 12)'/%3E%3Cellipse fill='%233d8b6e' cx='20.5' cy='11.5' rx='4.2' ry='2.4' transform='rotate(35 20.5 11.5)'/%3E%3Cellipse fill='%232f6f56' cx='16' cy='7.5' rx='3.2' ry='4.4'/%3E%3C/svg%3E">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in &mdash; Andrew's Paper Library</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db;
  --card-bg: #fbfaf8; --error: #b3261e;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #1e1e1e; --fg: #ece9e2; --muted: #a9a49a; --rule: #444444; --accent: #7fa7ff; --card-bg: #2a2a2a; --error: #ff6b60; }
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: var(--bg); color: var(--fg);
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  padding: 2rem 1.2rem;
}
.card {
  width: 100%; max-width: 420px; background: var(--card-bg); border: 1px solid var(--rule);
  border-radius: 12px; padding: 1.8em 1.6em 1.6em;
}
.brand {
  display: flex; align-items: center; gap: 0.65em; margin-bottom: 1.4em;
  font-weight: 600; font-size: 1.05em; line-height: 1.25;
}
.brand svg { width: 2.6em; height: 2.6em; flex-shrink: 0; }
h1 { font-size: 1.25em; margin: 0 0 0.35em; }
.lede { color: var(--muted); font-size: 0.92em; line-height: 1.5; margin: 0 0 1.3em; }
label { display: block; font-size: 0.82em; font-weight: 600; color: var(--muted); margin: 0 0 0.4em; }
textarea {
  width: 100%; min-height: 5.2em; padding: 0.7em 0.85em; border-radius: 8px; border: 1px solid var(--rule);
  background: var(--bg); color: var(--fg); font-size: 0.88em; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  outline: none; resize: vertical; line-height: 1.45;
}
textarea:focus { border-color: var(--accent); }
button[type="submit"] {
  width: 100%; margin-top: 1.15em; padding: 0.75em 1em; border-radius: 8px; border: none;
  background: var(--fg); color: var(--bg); font-size: 0.95em; font-weight: 600; cursor: pointer;
}
button[type="submit"]:hover { opacity: 0.9; }
button[type="submit"]:disabled { opacity: 0.55; cursor: default; }
.error { color: var(--error); font-size: 0.88em; margin-top: 0.85em; min-height: 1.3em; }
</style>
</head>
<body>
<div class="card">
  <div class="brand">
    <svg viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <path fill="#c47a5a" d="M9.5 19.2h13l-1.55 9.1c-.15.9-.9 1.55-1.8 1.55h-6.3c-.9 0-1.65-.65-1.8-1.55L9.5 19.2z"/>
      <ellipse fill="#4a3728" cx="16" cy="19.1" rx="6.6" ry="2.15"/>
      <path fill="none" stroke="#3a6b4f" stroke-width="1.7" stroke-linecap="round" d="M16 19.1c0-3.2.15-6.4.1-9.5"/>
      <ellipse fill="#4fa882" cx="10.8" cy="12.2" rx="5.1" ry="2.55" transform="rotate(-42 10.8 12.2)"/>
      <ellipse fill="#3d8b6e" cx="21.2" cy="11.6" rx="5.1" ry="2.55" transform="rotate(40 21.2 11.6)"/>
      <ellipse fill="#2f6f56" cx="16" cy="6.6" rx="3.35" ry="5.1"/>
    </svg>
    <span>Andrew&rsquo;s Paper Library</span>
  </div>
  <h1>Sign in</h1>
  <p class="lede">Paste your secret key. No username or email &mdash; the key is your account.</p>
  <form id="loginForm">
    <label for="secretKey">Secret key</label>
    <textarea id="secretKey" name="secretKey" autocomplete="off" spellcheck="false" required autofocus placeholder="pr_…"></textarea>
    <button type="submit" id="submitBtn">Sign in</button>
    <div class="error" id="error" aria-live="polite"></div>
  </form>
</div>
<script>
(function () {
  var form = document.getElementById("loginForm");
  var keyEl = document.getElementById("secretKey");
  var btn = document.getElementById("submitBtn");
  var err = document.getElementById("error");
  var params = new URLSearchParams(window.location.search);
  var next = params.get("next") || "/";
  if (!next.startsWith("/") || next.startsWith("//")) next = "/";

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    err.textContent = "";
    btn.disabled = true;
    fetch("/api/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ secretKey: keyEl.value })
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (res) {
      if (!res.ok) {
        err.textContent = res.data.error || "Invalid secret key";
        btn.disabled = false;
        keyEl.focus();
        return;
      }
      window.location.replace(next);
    }).catch(function () {
      err.textContent = "Could not reach the server";
      btn.disabled = false;
    });
  });
})();
</script>
</body>
</html>
"""

SIGNUP_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath fill='%23c47a5a' d='M10 19h12l-1.4 9.2c-.1.8-.8 1.4-1.6 1.4h-6c-.8 0-1.5-.6-1.6-1.4L10 19z'/%3E%3Cellipse fill='%234a3728' cx='16' cy='19' rx='6.2' ry='2'/%3E%3Cpath fill='%233a6b4f' d='M16 19v-8' stroke='%233a6b4f' stroke-width='1.6' stroke-linecap='round'/%3E%3Cellipse fill='%234fa882' cx='11.5' cy='12' rx='4.2' ry='2.4' transform='rotate(-35 11.5 12)'/%3E%3Cellipse fill='%233d8b6e' cx='20.5' cy='11.5' rx='4.2' ry='2.4' transform='rotate(35 20.5 11.5)'/%3E%3Cellipse fill='%232f6f56' cx='16' cy='7.5' rx='3.2' ry='4.4'/%3E%3C/svg%3E">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Create account &mdash; Andrew's Paper Library</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --rule: #e3e0d8; --accent: #1a56db;
  --card-bg: #fbfaf8; --error: #b3261e; --warn-bg: color-mix(in srgb, #c47a5a 12%, var(--card-bg));
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #1e1e1e; --fg: #ece9e2; --muted: #a9a49a; --rule: #444444; --accent: #7fa7ff; --card-bg: #2a2a2a; --error: #ff6b60; }
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: var(--bg); color: var(--fg);
  font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  padding: 2rem 1.2rem;
}
.card {
  width: 100%; max-width: 440px; background: var(--card-bg); border: 1px solid var(--rule);
  border-radius: 12px; padding: 1.8em 1.6em 1.6em;
}
.brand {
  display: flex; align-items: center; gap: 0.65em; margin-bottom: 1.4em;
  font-weight: 600; font-size: 1.05em; line-height: 1.25;
}
.brand svg { width: 2.6em; height: 2.6em; flex-shrink: 0; }
h1 { font-size: 1.25em; margin: 0 0 0.35em; }
.lede { color: var(--muted); font-size: 0.92em; line-height: 1.5; margin: 0 0 1.3em; }
.primary-btn, .secondary-btn {
  width: 100%; padding: 0.75em 1em; border-radius: 8px; font-size: 0.95em; font-weight: 600; cursor: pointer;
}
.primary-btn {
  border: none; background: var(--fg); color: var(--bg);
}
.primary-btn:hover { opacity: 0.9; }
.primary-btn:disabled { opacity: 0.55; cursor: default; }
.secondary-btn {
  margin-top: 0.55em; border: 1px solid var(--rule); background: transparent; color: var(--fg);
}
.secondary-btn:hover { background: var(--rule); }
.error { color: var(--error); font-size: 0.88em; margin-top: 0.85em; min-height: 1.3em; }
.warn {
  background: var(--warn-bg); border: 1px solid var(--rule); border-radius: 8px;
  padding: 0.85em 1em; font-size: 0.88em; line-height: 1.5; margin: 0 0 1em; color: var(--fg);
}
.key-box {
  border: 1px solid var(--rule); border-radius: 8px; background: var(--bg);
  padding: 0.85em 1em; margin-bottom: 0.75em;
}
.key-label { font-size: 0.78em; font-weight: 600; color: var(--muted); margin-bottom: 0.45em; text-transform: uppercase; letter-spacing: 0.04em; }
.key-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.82em; line-height: 1.5; word-break: break-all; user-select: all;
}
.key-actions { display: flex; gap: 0.5em; margin-top: 0.75em; }
.key-actions button {
  flex: 1; padding: 0.45em 0.6em; border-radius: 6px; border: 1px solid var(--rule);
  background: var(--card-bg); color: var(--fg); font-size: 0.82em; cursor: pointer;
}
.key-actions button:hover { background: var(--rule); }
.switch { margin-top: 1.1em; font-size: 0.88em; color: var(--muted); text-align: center; }
.switch a { color: var(--accent); text-decoration: none; }
.switch a:hover { text-decoration: underline; }
[hidden] { display: none !important; }
</style>
</head>
<body>
<div class="card">
  <div class="brand">
    <svg viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <path fill="#c47a5a" d="M9.5 19.2h13l-1.55 9.1c-.15.9-.9 1.55-1.8 1.55h-6.3c-.9 0-1.65-.65-1.8-1.55L9.5 19.2z"/>
      <ellipse fill="#4a3728" cx="16" cy="19.1" rx="6.6" ry="2.15"/>
      <path fill="none" stroke="#3a6b4f" stroke-width="1.7" stroke-linecap="round" d="M16 19.1c0-3.2.15-6.4.1-9.5"/>
      <ellipse fill="#4fa882" cx="10.8" cy="12.2" rx="5.1" ry="2.55" transform="rotate(-42 10.8 12.2)"/>
      <ellipse fill="#3d8b6e" cx="21.2" cy="11.6" rx="5.1" ry="2.55" transform="rotate(40 21.2 11.6)"/>
      <ellipse fill="#2f6f56" cx="16" cy="6.6" rx="3.35" ry="5.1"/>
    </svg>
    <span>Andrew&rsquo;s Paper Library</span>
  </div>

  <div id="stepCreate">
    <h1>Create account</h1>
    <p class="lede">Generate an anonymous secret key, or sign in with one you already saved. Each key unlocks its own encrypted vault; a new key starts empty and does not replace an existing vault.</p>
    <button type="button" class="primary-btn" id="generateBtn">Generate secret key</button>
    <div class="error" id="createError" aria-live="polite"></div>
    <div class="switch">Already have a key? <a id="signInLink" href="/login">Sign in</a></div>
  </div>

  <div id="stepReveal" hidden>
    <h1>Save your secret key</h1>
    <div class="warn">
      This key is shown <strong>once</strong>. It is not stored in plaintext and cannot be recovered.
      If you lose it, you lose access to this library account permanently.
    </div>
    <div class="key-box">
      <div class="key-label">Your secret key</div>
      <div class="key-value" id="keyValue" data-hidden="1">••••••••••••••••••••••••••••••••</div>
      <div class="key-actions">
        <button type="button" id="toggleKeyBtn">Show</button>
        <button type="button" id="copyKeyBtn">Copy</button>
      </div>
    </div>
    <button type="button" class="primary-btn" id="continueBtn">I&rsquo;ve saved it &mdash; continue</button>
    <div class="error" id="revealError" aria-live="polite"></div>
  </div>
</div>
<script>
(function () {
  var stepCreate = document.getElementById("stepCreate");
  var stepReveal = document.getElementById("stepReveal");
  var generateBtn = document.getElementById("generateBtn");
  var createError = document.getElementById("createError");
  var revealError = document.getElementById("revealError");
  var keyValue = document.getElementById("keyValue");
  var toggleBtn = document.getElementById("toggleKeyBtn");
  var copyBtn = document.getElementById("copyKeyBtn");
  var continueBtn = document.getElementById("continueBtn");
  var secretKey = "";
  var params = new URLSearchParams(window.location.search);
  var next = params.get("next") || "/";
  if (!next.startsWith("/") || next.startsWith("//")) next = "/";
  var signInLink = document.getElementById("signInLink");
  if (signInLink) signInLink.href = "/login?next=" + encodeURIComponent(next);

  function mask(s) {
    return "•".repeat(Math.min(40, Math.max(24, s.length)));
  }

  function renderKey() {
    var hidden = keyValue.getAttribute("data-hidden") === "1";
    keyValue.textContent = hidden ? mask(secretKey) : secretKey;
    toggleBtn.textContent = hidden ? "Show" : "Hide";
  }

  generateBtn.addEventListener("click", function () {
    createError.textContent = "";
    generateBtn.disabled = true;
    fetch("/api/signup", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (res) {
      if (!res.ok) {
        createError.textContent = res.data.error || "Could not create account";
        generateBtn.disabled = false;
        return;
      }
      secretKey = res.data.secretKey || "";
      if (!secretKey) {
        createError.textContent = "Server did not return a secret key";
        generateBtn.disabled = false;
        return;
      }
      keyValue.setAttribute("data-hidden", "1");
      renderKey();
      stepCreate.hidden = true;
      stepReveal.hidden = false;
    }).catch(function () {
      createError.textContent = "Could not reach the server";
      generateBtn.disabled = false;
    });
  });

  toggleBtn.addEventListener("click", function () {
    var hidden = keyValue.getAttribute("data-hidden") === "1";
    keyValue.setAttribute("data-hidden", hidden ? "0" : "1");
    renderKey();
  });

  copyBtn.addEventListener("click", function () {
    revealError.textContent = "";
    if (!secretKey) return;
    function ok() {
      copyBtn.textContent = "Copied";
      setTimeout(function () { copyBtn.textContent = "Copy"; }, 1500);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(secretKey).then(ok).catch(function () {
        revealError.textContent = "Could not copy — use Show and select the key";
      });
    } else {
      revealError.textContent = "Could not copy — use Show and select the key";
    }
  });

  continueBtn.addEventListener("click", function () {
    window.location.replace(next);
  });
})();
</script>
</body>
</html>
"""

AUTH_EXPORT_HTML = """
        <div class="prefs-popover-label" style="margin-top: 0.6em;">Library</div>
        <div class="prefs-popover-row" style="flex-direction: column; align-items: stretch; gap: 0.45em; padding-bottom: 0.2em;">
          <div style="font-size: 0.78em; color: var(--muted); line-height: 1.45;">
            Download a plaintext ZIP of your unlocked papers (HTML + index).
          </div>
          <button type="button" class="prefs-btn" id="prefsExportBtn" style="width: 100%;">Export library</button>
        </div>
"""

AUTH_LOGOUT_HTML = AUTH_EXPORT_HTML + """
        <div class="prefs-popover-label" style="margin-top: 0.6em;">Account</div>
        <div class="prefs-popover-row" style="flex-direction: column; align-items: stretch; gap: 0.45em; padding-bottom: 0.2em;">
          <div style="font-size: 0.78em; color: var(--muted); line-height: 1.45;">
            Anonymous secret-key vault. Papers are encrypted at rest; the key is never stored in plaintext and cannot be recovered if lost.
          </div>
          <button type="button" class="prefs-btn" id="prefsLogoutBtn" style="width: 100%;">Log out</button>
        </div>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "PaperReaderLibrary/1.0"

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        extra_headers: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Papers get tagged/moved/opened from other tabs and pages all the
            # time -- never let the browser serve a stale cached copy of the
            # library index, the home page, or a reader page.
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for key, value in extra_headers:
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client navigated away / aborted — normal, must not tear down the server.
            return
        except OSError as e:
            if getattr(e, "errno", None) in (32, 54, 104):  # EPIPE / ECONNRESET-ish
                return
            raise

    def _send_json(
        self,
        obj,
        status: int = 200,
        extra_headers: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        self._send_bytes(
            json.dumps(obj).encode("utf-8"),
            "application/json",
            status,
            extra_headers=extra_headers,
        )

    def _send_html(self, html_str: str, status: int = 200) -> None:
        self._send_bytes(html_str.encode("utf-8"), "text/html; charset=utf-8", status)

    def _home_html(self) -> str:
        from .palette import get_palette_html

        html_out = HOME_PAGE_HTML.replace("</body>", get_palette_html("home") + "</body>")
        if _auth_enabled():
            html_out = html_out.replace("<!--AUTH_LOGOUT_SLOT-->", AUTH_LOGOUT_HTML)
        else:
            html_out = html_out.replace("<!--AUTH_LOGOUT_SLOT-->", AUTH_EXPORT_HTML)
        return html_out

    def _redirect(self, location: str, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _safe_next(self, raw: Optional[str]) -> str:
        nxt = raw or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            return "/"
        return nxt

    def _require_auth(self) -> bool:
        """Return True if the request may proceed. On failure, response is sent."""
        vault = _bind_vault_from_request(self)
        if not _auth_enabled():
            return True
        path = urlparse(self.path).path
        public = (
            "/login", "/signup",
            "/api/login", "/api/signup", "/api/logout",
        )
        if path in public:
            return True
        if vault is not None:
            return True
        if path.startswith("/api/"):
            self._send_json({"error": "unauthorized"}, 401)
            return False
        next_url = self.path if self.path.startswith("/") else "/"
        entry = _auth_entry_path()
        loc = entry + "?next=" + urllib.parse.quote(next_url, safe="")
        self._redirect(loc)
        return False

    def _serve_auth_page(self, kind: str) -> None:
        """Serve login or signup, or bounce if already signed in."""
        if not _auth_enabled():
            self._redirect("/")
            return
        qs = parse_qs(urlparse(self.path).query)
        nxt = self._safe_next((qs.get("next") or [None])[0])
        if _bind_vault_from_request(self) is not None:
            self._redirect(nxt)
            return
        if kind == "signup":
            self._send_html(SIGNUP_PAGE_HTML)
            return
        self._send_html(LOGIN_PAGE_HTML)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self._serve_auth_page("login")
        elif parsed.path == "/signup":
            self._serve_auth_page("signup")
        elif parsed.path == "/":
            self._send_html(self._home_html())
        elif parsed.path == "/about":
            self._send_html(ABOUT_PAGE_HTML)
        elif parsed.path == "/guide":
            self._send_html(GUIDE_PAGE_HTML)
        elif parsed.path == "/pipeline":
            from .palette import get_palette_html
            self._send_html(PIPELINE_PAGE_HTML.replace('</body>', get_palette_html('home') + '</body>'))
        elif parsed.path == "/api/papers":
            self._send_json(_load_index())
        elif parsed.path == "/api/pipeline-status":
            self._send_json(_list_jobs())
        elif parsed.path == "/api/export":
            self._handle_export()
        elif parsed.path == "/api/account":
            self._handle_account_get()
        elif parsed.path == "/api/account/avatar":
            self._handle_account_avatar_get()
        elif parsed.path.startswith("/library/"):
            self._serve_library_file(parsed.path[len("/library/") :])
        else:
            self._send_html("<h1>404</h1>", 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/signup":
            self._handle_signup()
            return
        if parsed.path == "/api/login":
            self._handle_login()
            return
        if parsed.path == "/api/logout":
            cookies = _parse_cookies(self.headers.get("Cookie", ""))
            _clear_session(cookies.get(SESSION_COOKIE, ""))
            _set_current_vault(None)
            self._send_json({"ok": True}, extra_headers=[("Set-Cookie", _session_clear_cookie())])
            return
        if parsed.path == "/api/account":
            self._handle_account_post()
            return
        if parsed.path == "/api/account/avatar":
            self._handle_account_avatar_post()
            return
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

    def _handle_signup(self) -> None:
        if not _auth_enabled():
            self._send_json({"error": "authentication is disabled"}, 400)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            try:
                self.rfile.read(length)
            except OSError:
                pass
        try:
            secret_key, account = _create_account()
            vault = _unlock_vault_for_account(secret_key, account)
        except (ValueError, OSError) as e:
            self._send_json({"error": str(e)}, 400)
            return
        token = _make_session(account["id"], vault)
        _set_current_vault(vault)
        threading.Thread(target=rebuild_library, kwargs={"vault": vault, "quiet": True}, daemon=True).start()
        self._send_json(
            {"ok": True, "secretKey": secret_key},
            extra_headers=[("Set-Cookie", _session_set_cookie(token))],
        )

    def _handle_login(self) -> None:
        if not _auth_enabled():
            self._send_json({"error": "authentication is disabled"}, 400)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        secret_key = body.get("secretKey") if isinstance(body.get("secretKey"), str) else ""
        account = _find_account_for_key(secret_key)
        if account is None:
            self._send_json({"error": "invalid secret key"}, 401)
            return
        try:
            vault = _unlock_vault_for_account(secret_key, account)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return
        token = _make_session(account["id"], vault)
        _set_current_vault(vault)
        threading.Thread(target=rebuild_library, kwargs={"vault": vault, "quiet": True}, daemon=True).start()
        self._send_json({"ok": True}, extra_headers=[("Set-Cookie", _session_set_cookie(token))])

    def _handle_export(self) -> None:
        vault = _current_vault()
        if vault is None:
            self._send_json({"error": "unauthorized"}, 401)
            return
        try:
            data = vault.export_zip_bytes()
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
            return
        self._send_bytes(
            data,
            "application/zip",
            extra_headers=[
                ("Content-Disposition", 'attachment; filename="paper-library-export.zip"'),
            ],
        )

    def _handle_account_get(self) -> None:
        vault = _current_vault()
        if vault is None:
            self._send_json({"error": "unauthorized"}, 401)
            return
        self._send_json(_account_public_payload(vault))

    def _handle_account_post(self) -> None:
        vault = _current_vault()
        if vault is None:
            self._send_json({"error": "unauthorized"}, 401)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "invalid json"}, 400)
            return
        if not isinstance(body, dict):
            self._send_json({"error": "invalid json"}, 400)
            return
        if "displayName" not in body:
            self._send_json({"error": "displayName is required"}, 400)
            return
        profile = _save_profile(vault, body.get("displayName"))
        self._send_json({
            "ok": True,
            "displayName": profile["displayName"],
            "hasAvatar": _avatar_path(vault).is_file(),
        })

    def _handle_account_avatar_get(self) -> None:
        vault = _current_vault()
        if vault is None:
            self._send_json({"error": "unauthorized"}, 401)
            return
        path = _avatar_path(vault)
        if not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        try:
            data = path.read_bytes()
        except OSError as e:
            self._send_json({"error": str(e)}, 500)
            return
        self._send_bytes(data, "image/jpeg")

    def _handle_account_avatar_post(self) -> None:
        vault = _current_vault()
        if vault is None:
            self._send_json({"error": "unauthorized"}, 401)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            self._send_json({"error": "empty body"}, 400)
            return
        if length > _MAX_AVATAR_BYTES:
            self._send_json({"error": "image too large"}, 400)
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("image/jpeg", "image/jpg"):
            self._send_json({"error": "expected image/jpeg"}, 400)
            return
        data = self.rfile.read(length)
        if not (len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF):
            self._send_json({"error": "invalid jpeg"}, 400)
            return
        path = _avatar_path(vault)
        try:
            from .vault import _atomic_write_bytes

            _atomic_write_bytes(path, data)
        except OSError as e:
            self._send_json({"error": str(e)}, 500)
            return
        self._send_json({"ok": True, "hasAvatar": True})

    def _handle_account_avatar_delete(self) -> None:
        vault = _current_vault()
        if vault is None:
            self._send_json({"error": "unauthorized"}, 401)
            return
        path = _avatar_path(vault)
        try:
            if path.is_file():
                path.unlink()
        except OSError as e:
            self._send_json({"error": str(e)}, 500)
            return
        self._send_json({"ok": True, "hasAvatar": False})

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
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
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/account/avatar":
            self._handle_account_avatar_delete()
            return
        if parsed.path.startswith("/api/papers/"):
            paper_id = os.path.basename(parsed.path[len("/api/papers/") :])
            if _delete_paper(paper_id):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, 404)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_PUT(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
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

    def _inject_reader_profile(self, html_body: str, vault: Vault) -> str:
        """Stamp the signed-in display name / avatar flag into reader pages."""
        profile = _account_public_payload(vault)
        snippet = (
            "<script>window.__paperReaderProfile="
            + json.dumps(profile, separators=(",", ":"))
            + ";</script>\n"
        )
        lower = html_body.lower()
        idx = lower.rfind("</head>")
        if idx != -1:
            return html_body[:idx] + snippet + html_body[idx:]
        idx = lower.rfind("<body")
        if idx != -1:
            # Insert right after the opening <body...> tag.
            gt = html_body.find(">", idx)
            if gt != -1:
                return html_body[: gt + 1] + "\n" + snippet + html_body[gt + 1 :]
        return snippet + html_body

    def _serve_library_file(self, name: str) -> None:
        safe_name = os.path.basename(name)  # no path traversal
        if not safe_name.endswith(".html"):
            self._send_html("<h1>404</h1>", 404)
            return
        vault = _current_vault()
        if vault is None:
            self._send_json({"error": "unauthorized"}, 401)
            return
        paper_id = safe_name[: -len(".html")]
        html_body = vault.read_html(paper_id)
        if html_body is None:
            self._send_html("<h1>404</h1>", 404)
            return
        _touch_opened(paper_id, vault)
        html_body = self._inject_reader_profile(html_body, vault)
        self._send_bytes(html_body.encode("utf-8"), "text/html; charset=utf-8")

    def _handle_upload(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        filename = (qs.get("filename") or ["upload"])[0]
        if not filename.lower().endswith(ALLOWED_UPLOAD_SUFFIXES):
            self._send_json(
                {"error": "unsupported file type -- use .tex, .zip, .tar.gz, .tgz, .tar, .html, .pdf, or .epub"}, 400
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

        pdf_parser = (qs.get("pdfParser") or ["docling"])[0]
        mineru_token = (self.headers.get("X-MinerU-Token") or "").strip()

        try:
            entry = _process_upload(
                filename,
                data,
                pdf_parser=pdf_parser,
                mineru_token=mineru_token,
            )
        except (LatexConvertError, HtmlConvertError, PdfConvertError, EpubConvertError) as e:
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
    """True if something accepts HTTP on host:port (auth may return 401/302)."""
    url = f"http://{host}:{port}/signup"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "paper-reader-cli"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 500
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
        if not _auth_enabled():
            print("Authentication disabled (PAPER_READER_DISABLE_AUTH is set); local vault auto-unlocked.")
        elif not _account_exists():
            print("No account yet — open the library to create a secret-key vault.")
        else:
            print("Authentication required (sign in to unlock your encrypted vault).")
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

