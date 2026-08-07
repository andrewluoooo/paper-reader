"""Per-account encrypted paper vaults (Fernet).

Layout under ``~/.paper_reader_library``::

    accounts.json
    vaults/<account_id>/
      index.json.enc
      <paper_id>.html.enc
      raw/<paper_id>/...   # each file stored with a .enc suffix

The data-encryption key (DEK) is derived from the account secret key +
per-account ``enc_salt`` and must only live in process memory for an
unlocked session.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


def _default_library_dir() -> Path:
    env = os.environ.get("PAPER_READER_LIBRARY_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".paper_reader_library"


LIBRARY_DIR = _default_library_dir()
ACCOUNTS_PATH = LIBRARY_DIR / "accounts.json"
VAULTS_DIR = LIBRARY_DIR / "vaults"
LEGACY_INDEX = LIBRARY_DIR / "index.json"
LEGACY_RAW = LIBRARY_DIR / "raw"
LEGACY_MARKER = LIBRARY_DIR / ".legacy_migrated"

_PBKDF2_ITERATIONS = 200_000
_ENC_SUFFIX = ".enc"
_INDEX_LOCKS: dict[str, threading.Lock] = {}
_INDEX_LOCKS_GUARD = threading.Lock()


def _index_lock_for(vdir: Path) -> threading.Lock:
    key = str(vdir.resolve())
    with _INDEX_LOCKS_GUARD:
        lock = _INDEX_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _INDEX_LOCKS[key] = lock
        return lock


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a unique temp file in the same directory, then replace.

    Fixed ``.tmp`` names race under ThreadingHTTPServer (concurrent index
    touches from opening papers) and raise FileNotFoundError on replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def vaults_root(library_dir: Path) -> Path:
    return library_dir / "vaults"


def vault_dir(library_dir: Path, account_id: str) -> Path:
    return vaults_root(library_dir) / account_id


def ensure_vault_dir(library_dir: Path, account_id: str) -> Path:
    path = vault_dir(library_dir, account_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "raw").mkdir(parents=True, exist_ok=True)
    return path


def ensure_empty_vault(account_id: str) -> Path:
    return ensure_vault_dir(LIBRARY_DIR, account_id)


def derive_fernet(secret_key: str, enc_salt: bytes) -> Fernet:
    """Derive a Fernet key from the account secret + enc_salt."""
    raw = hashlib.pbkdf2_hmac(
        "sha256",
        secret_key.encode("utf-8"),
        enc_salt,
        _PBKDF2_ITERATIONS,
        dklen=32,
    )
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_bytes(fernet: Fernet, data: bytes) -> bytes:
    return fernet.encrypt(data)


def decrypt_bytes(fernet: Fernet, data: bytes) -> bytes:
    return fernet.decrypt(data)


def encrypt_text(fernet: Fernet, text: str) -> bytes:
    return encrypt_bytes(fernet, text.encode("utf-8"))


def decrypt_text(fernet: Fernet, data: bytes) -> str:
    return decrypt_bytes(fernet, data).decode("utf-8")


def index_enc_path(vdir: Path) -> Path:
    return vdir / "index.json.enc"


def html_enc_path(vdir: Path, paper_id: str) -> Path:
    return vdir / f"{paper_id}.html.enc"


def raw_paper_dir(vdir: Path, paper_id: str) -> Path:
    return vdir / "raw" / paper_id


def load_index(vdir: Path, fernet: Fernet) -> list[dict]:
    path = index_enc_path(vdir)
    with _index_lock_for(vdir):
        if not path.is_file():
            return []
        try:
            raw = decrypt_text(fernet, path.read_bytes())
        except (OSError, InvalidToken, UnicodeDecodeError):
            return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        try:
            data, _end = json.JSONDecoder().raw_decode(raw.lstrip())
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


def save_index(vdir: Path, fernet: Fernet, items: list[dict]) -> None:
    vdir.mkdir(parents=True, exist_ok=True)
    payload = encrypt_text(fernet, json.dumps(items, indent=2))
    path = index_enc_path(vdir)
    with _index_lock_for(vdir):
        _atomic_write_bytes(path, payload)


def write_html(vdir: Path, fernet: Fernet, paper_id: str, html_out: str) -> None:
    vdir.mkdir(parents=True, exist_ok=True)
    path = html_enc_path(vdir, paper_id)
    _atomic_write_bytes(path, encrypt_text(fernet, html_out))


def read_html(vdir: Path, fernet: Fernet, paper_id: str) -> Optional[bytes]:
    path = html_enc_path(vdir, paper_id)
    if not path.is_file():
        return None
    try:
        return decrypt_bytes(fernet, path.read_bytes())
    except (OSError, InvalidToken):
        return None


def delete_paper_files(vdir: Path, paper_id: str) -> None:
    html_path = html_enc_path(vdir, paper_id)
    if html_path.is_file():
        html_path.unlink()
    raw_path = raw_paper_dir(vdir, paper_id)
    if raw_path.is_dir():
        shutil.rmtree(raw_path)


def encrypt_raw_tree(fernet: Fernet, src_dir: Path, dest_dir: Path) -> None:
    """Encrypt every file under src_dir into dest_dir as ``<name>.enc``."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not src_dir.is_dir():
        return
    for root, _dirs, files in os.walk(src_dir):
        rel_root = Path(root).relative_to(src_dir)
        out_root = dest_dir / rel_root
        out_root.mkdir(parents=True, exist_ok=True)
        for name in files:
            src = Path(root) / name
            out = out_root / f"{name}{_ENC_SUFFIX}"
            out.write_bytes(encrypt_bytes(fernet, src.read_bytes()))


def decrypt_raw_tree(fernet: Fernet, src_dir: Path, dest_dir: Path) -> None:
    """Decrypt an encrypted raw tree into a plaintext dest_dir."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not src_dir.is_dir():
        return
    for root, _dirs, files in os.walk(src_dir):
        rel_root = Path(root).relative_to(src_dir)
        out_root = dest_dir / rel_root
        out_root.mkdir(parents=True, exist_ok=True)
        for name in files:
            if not name.endswith(_ENC_SUFFIX):
                continue
            src = Path(root) / name
            out_name = name[: -len(_ENC_SUFFIX)]
            out = out_root / out_name
            out.write_bytes(decrypt_bytes(fernet, src.read_bytes()))


def legacy_plaintext_present(library_dir: Path) -> bool:
    """True if a flat (pre-vault) library still has claimable papers."""
    if (library_dir / ".legacy_migrated").is_file():
        return False
    index = library_dir / "index.json"
    if index.is_file():
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
            if isinstance(data, list) and len(data) > 0:
                return True
        except (OSError, json.JSONDecodeError):
            pass
    for path in library_dir.glob("*.html"):
        if path.is_file():
            return True
    raw = library_dir / "raw"
    if raw.is_dir() and any(raw.iterdir()):
        return True
    return False


def _migrate_legacy_files(library_dir: Path, vdir: Path, fernet: Fernet) -> int:
    """Encrypt flat library files into ``vdir``. Returns paper count."""
    ensure_vault_dir(library_dir, vdir.name)
    index_path = library_dir / "index.json"
    items: list[dict] = []
    if index_path.is_file():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                items = data
        except (OSError, json.JSONDecodeError):
            items = []

    seen_ids = {e.get("id") for e in items if isinstance(e, dict)}
    for html_path in library_dir.glob("*.html"):
        paper_id = html_path.stem
        if paper_id in seen_ids:
            continue
        if len(paper_id) < 8:
            continue
        items.append(
            {
                "id": paper_id,
                "title": paper_id,
                "authors": [],
                "venue": "",
                "summary": "",
                "sourceFilename": "",
                "addedAt": html_path.stat().st_mtime,
                "lastOpenedAt": None,
                "rawHtmlPath": "",
                "tags": [],
                "status": "inbox",
                "pinned": False,
                "completed": False,
                "deletedAt": None,
            }
        )
        seen_ids.add(paper_id)

    for entry in items:
        if not isinstance(entry, dict):
            continue
        paper_id = entry.get("id")
        if not paper_id or not isinstance(paper_id, str):
            continue
        plain_html = library_dir / f"{paper_id}.html"
        if plain_html.is_file():
            write_html(vdir, fernet, paper_id, plain_html.read_text(encoding="utf-8"))

        raw_src: Optional[Path] = None
        recorded = entry.get("rawHtmlPath")
        if recorded:
            recorded_path = Path(str(recorded))
            raw_root = library_dir / "raw" / paper_id
            if raw_root.is_dir():
                raw_src = raw_root
            elif recorded_path.is_file():
                raw_src = recorded_path.parent
        if raw_src is None:
            candidate = library_dir / "raw" / paper_id
            if candidate.is_dir():
                raw_src = candidate

        if raw_src is not None and raw_src.is_dir():
            encrypt_raw_tree(fernet, raw_src, raw_paper_dir(vdir, paper_id))
            if recorded and Path(str(recorded)).is_file():
                try:
                    rel = Path(str(recorded)).relative_to(raw_src)
                    entry["rawHtmlPath"] = str(rel).replace("\\", "/")
                except ValueError:
                    entry["rawHtmlPath"] = Path(str(recorded)).name
            else:
                htmls = list(raw_src.rglob("*.html"))
                if htmls:
                    try:
                        entry["rawHtmlPath"] = str(htmls[0].relative_to(raw_src)).replace("\\", "/")
                    except ValueError:
                        entry["rawHtmlPath"] = htmls[0].name

    save_index(vdir, fernet, items)

    if index_path.is_file():
        index_path.unlink()
    bak = library_dir / "index.json.bak"
    if bak.is_file():
        bak.unlink()
    for html_path in list(library_dir.glob("*.html")):
        if html_path.is_file():
            html_path.unlink()
    raw_root = library_dir / "raw"
    if raw_root.is_dir():
        shutil.rmtree(raw_root)

    return len(items)


def migrate_legacy_into_vault(vault: "Vault") -> int:
    """Move plaintext library files into the vault once. Returns paper count."""
    library_dir = LIBRARY_DIR
    if LEGACY_MARKER.is_file():
        return 0
    if not legacy_plaintext_present(library_dir):
        LEGACY_MARKER.write_text("1\n", encoding="utf-8")
        return 0
    count = _migrate_legacy_files(library_dir, vault.root, vault.fernet)
    LEGACY_MARKER.write_text("1\n", encoding="utf-8")
    return count


def build_export_zip(vdir: Path, fernet: Fernet) -> bytes:
    """Return a ZIP of decrypted index.json + reader HTML files."""
    items = load_index(vdir, fernet)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.json", json.dumps(items, indent=2))
        for entry in items:
            if not isinstance(entry, dict):
                continue
            paper_id = entry.get("id")
            if not paper_id:
                continue
            html_bytes = read_html(vdir, fernet, str(paper_id))
            if html_bytes is not None:
                title = (entry.get("title") or paper_id).strip() or paper_id
                safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in title)[:80].strip()
                zf.writestr(f"{safe or paper_id}_{paper_id}.html", html_bytes)
    return buf.getvalue()


class Vault:
    """Unlocked vault bound to an account id + Fernet DEK."""

    def __init__(self, account_id: str, fernet: Fernet, library_dir: Optional[Path] = None):
        self.account_id = account_id
        self.fernet = fernet
        self.library_dir = library_dir or LIBRARY_DIR
        self.root = ensure_vault_dir(self.library_dir, account_id)

    def load_index(self) -> list[dict]:
        return load_index(self.root, self.fernet)

    def save_index(self, items: list[dict]) -> None:
        save_index(self.root, self.fernet, items)

    def write_html(self, paper_id: str, html: str) -> None:
        write_html(self.root, self.fernet, paper_id, html)

    def read_html(self, paper_id: str) -> Optional[str]:
        data = read_html(self.root, self.fernet, paper_id)
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")

    def delete_paper_files(self, paper_id: str) -> None:
        delete_paper_files(self.root, paper_id)

    def encrypt_raw_tree(self, paper_id: str, plaintext_dir: Path) -> None:
        encrypt_raw_tree(self.fernet, plaintext_dir, raw_paper_dir(self.root, paper_id))

    def decrypt_raw_tree(self, paper_id: str, dest_dir: Path) -> bool:
        enc = raw_paper_dir(self.root, paper_id)
        if not enc.is_dir():
            return False
        decrypt_raw_tree(self.fernet, enc, dest_dir)
        return any(dest_dir.rglob("*"))

    def export_zip_bytes(self) -> bytes:
        return build_export_zip(self.root, self.fernet)
