"""Convert a plain PDF into the same ltx_*-classed structure html_convert.py
and latex_convert.py produce, so it flows through restyle()'s existing
pipeline unchanged -- same theming, highlighting, table/figure
fit-to-width, outline, citation hover previews, everything.

PDF backends:
  * Docling (default) -- local Python ML converter
  * MinerU -- official cloud API (https://mineru.net), free tier
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

from .html_convert import _normalize_body, _resolve_image_paths
from .mineru_normalize import enrich_mineru_html, prepare_mineru_markdown

StageCallback = Optional[Callable[[str], None]]

_MINERU_API_BASE = os.environ.get("MINERU_API_BASE", "https://mineru.net/api/v4").rstrip("/")
_MINERU_POLL_INTERVAL_S = 3.0
_MINERU_POLL_TIMEOUT_S = int(os.environ.get("MINERU_POLL_TIMEOUT_S", "1800"))  # 30 min


class PdfConvertError(RuntimeError):
    pass


def _opencv_env() -> dict[str, str]:
    """Disable OpenCV/OpenMP threading that SIGSEGVs on Apple Silicon."""
    env = os.environ.copy()
    env["OPENCV_NUM_THREADS"] = "0"
    env["OMP_NUM_THREADS"] = "1"
    return env


def _run_docling_isolated(pdf_path: str) -> str:
    """Run Docling in a completely separate subprocess.
    This prevents PyTorch from crashing the main web server process due to
    multiprocessing/CUDA/MPS resource conflicts when imported inside a threading server.
    """
    script = """
import sys
import traceback

try:
    import cv2
    # Disable OpenCV hardware optimizations to prevent KleidiCV SIGSEGV on Apple Silicon
    cv2.setUseOptimized(False)

    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    result = converter.convert(sys.argv[1])
    # export_to_html() returns a string; we print it to stdout so the parent process can capture it.
    print(result.document.export_to_html())
except Exception as e:
    traceback.print_exc()
    sys.exit(str(e))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, pdf_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_opencv_env(),
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise PdfConvertError(f"Docling failed to convert PDF:\n{e.stderr.strip()}")


def _mineru_token(explicit: Optional[str] = None) -> str:
    token = (explicit or "").strip() or (
        os.environ.get("MINERU_API_TOKEN")
        or os.environ.get("PAPER_READER_MINERU_TOKEN")
        or ""
    ).strip()
    if not token:
        raise PdfConvertError(
            "MinerU cloud API token missing.\n"
            "Create a free token at https://mineru.net/user-center/api-token\n"
            "then either:\n"
            "  • paste it in Preferences → MinerU API token, or\n"
            "  • export MINERU_API_TOKEN=… before starting the library"
        )
    return token


def _mineru_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }


def _mineru_check(resp: requests.Response, what: str) -> dict:
    try:
        payload = resp.json()
    except ValueError as e:
        raise PdfConvertError(
            f"MinerU {what}: non-JSON response HTTP {resp.status_code}: {resp.text[:300]}"
        ) from e
    if not isinstance(payload, dict):
        raise PdfConvertError(
            f"MinerU {what}: unexpected JSON ({type(payload).__name__}) "
            f"HTTP {resp.status_code}: {resp.text[:300]}"
        )
    if resp.status_code != 200:
        raise PdfConvertError(
            f"MinerU {what}: HTTP {resp.status_code}: {payload.get('msg') or resp.text[:300]}"
        )
    code = payload.get("code")
    if code not in (0, "0", None):
        msg = payload.get("msg") or payload.get("message") or str(payload)
        raise PdfConvertError(f"MinerU {what} failed ({code}): {msg}")
    return payload


def _find_mineru_markdown(out_root: Path, pdf_stem: str) -> Path:
    """Locate full.md (cloud zip) or a stem-named .md under out_root."""
    preferred = list(out_root.rglob("full.md"))
    if preferred:
        return preferred[0]

    candidates = [p for p in out_root.rglob("*.md") if p.is_file()]
    if not candidates:
        raise PdfConvertError(
            f"MinerU zip did not contain a markdown file under {out_root}."
        )

    def score(p: Path) -> tuple[int, int, str]:
        return (
            0 if p.stem in (pdf_stem, "full") else 1,
            len(p.parts),
            str(p),
        )

    return sorted(candidates, key=score)[0]


def _markdown_to_html_and_images(md_file: Path, workdir: str) -> str:
    try:
        import markdown
    except ImportError as e:
        raise PdfConvertError(
            "The 'markdown' package is required for the MinerU PDF backend.\n"
            "Install it with: pip install markdown"
        ) from e

    md_text = prepare_mineru_markdown(md_file.read_text(encoding="utf-8"))
    # MathML is already injected as raw HTML; keep it through markdown.
    html_content = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )
    images_dir = md_file.parent / "images"
    dest_images_dir = Path(workdir) / "images"
    if images_dir.is_dir():
        if dest_images_dir.exists():
            shutil.rmtree(dest_images_dir)
        shutil.copytree(images_dir, dest_images_dir)
    return html_content


def _run_mineru_cloud(
    pdf_path: str,
    workdir: str,
    *,
    api_token: Optional[str] = None,
    on_stage: StageCallback = None,
) -> str:
    """Upload a local PDF to MinerU cloud, poll, download result zip → HTML."""
    token = _mineru_token(api_token)
    src = Path(pdf_path)
    size_mb = src.stat().st_size / (1024 * 1024)
    if size_mb > 200:
        raise PdfConvertError(
            f"PDF is {size_mb:.1f} MB; MinerU cloud free tier caps files at 200 MB."
        )

    headers = _mineru_headers(token)
    model_version = os.environ.get("MINERU_MODEL_VERSION", "vlm").strip() or "vlm"
    language = os.environ.get("MINERU_LANGUAGE", "en").strip() or "en"

    if on_stage:
        on_stage("mineru: requesting upload URL")
    apply = requests.post(
        f"{_MINERU_API_BASE}/file-urls/batch",
        headers=headers,
        json={
            "files": [{"name": src.name, "data_id": src.stem[:120]}],
            "model_version": model_version,
            "enable_formula": True,
            "enable_table": True,
            "language": language,
        },
        timeout=60,
    )
    applied = _mineru_check(apply, "file-urls/batch")
    data = applied.get("data")
    if not isinstance(data, dict):
        data = {}
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if not batch_id or not file_urls:
        raise PdfConvertError(f"MinerU file-urls/batch returned no upload URL: {applied}")

    if on_stage:
        on_stage("mineru: uploading PDF")
    with src.open("rb") as f:
        put = requests.put(file_urls[0], data=f, timeout=600)
    if put.status_code not in (200, 201):
        raise PdfConvertError(
            f"MinerU PDF upload failed: HTTP {put.status_code}: {put.text[:300]}"
        )

    # Uploading to the signed URL auto-submits the parse task for this batch.
    deadline = time.monotonic() + _MINERU_POLL_TIMEOUT_S
    zip_url = ""
    last_state = ""
    while time.monotonic() < deadline:
        if on_stage:
            label = last_state or "queued"
            on_stage(f"mineru: {label}")
        time.sleep(_MINERU_POLL_INTERVAL_S)
        poll = requests.get(
            f"{_MINERU_API_BASE}/extract-results/batch/{batch_id}",
            headers=headers,
            timeout=60,
        )
        payload = _mineru_check(poll, "extract-results/batch")
        data_block = payload.get("data")
        if not isinstance(data_block, dict):
            continue
        results = data_block.get("extract_result") or []
        if not isinstance(results, list) or not results:
            continue
        item = results[0]
        if not isinstance(item, dict):
            continue
        state = (item.get("state") or "").lower()
        last_state = state or last_state
        if state in ("failed", "error"):
            raise PdfConvertError(
                f"MinerU parse failed: {item.get('err_msg') or item.get('error') or item}"
            )
        if state == "done":
            zip_url = item.get("full_zip_url") or ""
            if not zip_url:
                raise PdfConvertError(f"MinerU finished but returned no zip URL: {item}")
            break
    else:
        raise PdfConvertError(
            f"MinerU timed out after {_MINERU_POLL_TIMEOUT_S}s "
            f"(last state: {last_state or 'unknown'})."
        )

    if on_stage:
        on_stage("mineru: downloading results")
    zresp = requests.get(zip_url, timeout=600)
    if zresp.status_code != 200:
        raise PdfConvertError(
            f"MinerU zip download failed: HTTP {zresp.status_code}"
        )

    with tempfile.TemporaryDirectory(prefix="mineru_cloud_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        try:
            with zipfile.ZipFile(io.BytesIO(zresp.content)) as zf:
                zf.extractall(tmp_path)
        except zipfile.BadZipFile as e:
            raise PdfConvertError("MinerU returned an invalid result zip.") from e
        md_file = _find_mineru_markdown(tmp_path, src.stem)
        return _markdown_to_html_and_images(md_file, workdir)


def convert(
    input_path: str,
    workdir: str,
    backend: str = "docling",
    *,
    mineru_token: Optional[str] = None,
    on_stage: StageCallback = None,
) -> str:
    """Process a PDF and normalize HTML into the ltx_* structure restyle() expects.

    Returns the path to the generated HTML file.
    """
    src_path = Path(input_path).resolve()
    if not src_path.is_file():
        raise PdfConvertError(f"no such file: {input_path}")

    if backend == "mineru":
        html_content = _run_mineru_cloud(
            str(src_path),
            workdir,
            api_token=mineru_token,
            on_stage=on_stage,
        )
    else:
        if on_stage:
            on_stage("converting (docling)")
        html_content = _run_docling_isolated(str(src_path))

    soup = BeautifulSoup(html_content, "lxml")
    body = soup.find("body")
    if not body:
        # Fallback if there's no <body> tag (markdown → HTML fragment).
        body = soup

    # Tag the existing tree with ltx_* classes (same as HTML conversions)
    _normalize_body(soup, body)

    # Shared PDF enrichment (authors/abstract, Nature glued cites, bib, figs).
    # Previously MinerU-only — Docling Nature PDFs never got front matter or
    # citation linking without this.
    enrich_mineru_html(soup, body)
    _resolve_image_paths(body, Path(workdir))

    # Docling wraps pages in <div class="page">. Unwrap them so they don't
    # conflict with our CSS `.page` which adds reader margins (double margins).
    for page_div in body.find_all("div", class_="page"):
        page_div.unwrap()

    # Raw <table> elements need ltx_tabular (+ figure.ltx_table wrapper).
    for table in body.find_all("table"):
        table["class"] = (table.get("class") or []) + ["ltx_tabular"]
        if table.parent and table.parent.name != "figure":
            fig = soup.new_tag("figure")
            fig["class"] = ["ltx_table"]
            table.wrap(fig)

    out_soup = BeautifulSoup(
        '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>',
        "lxml",
    )
    article = out_soup.new_tag("article")
    article["class"] = ["ltx_document"]
    out_soup.body.append(article)

    # Lift the first heading out as the document title.
    title_text = "Untitled"
    first_heading = body.find(["h1", "h2", "h3"])
    if first_heading:
        title_text = first_heading.get_text(strip=True)
        first_heading.decompose()

    out_h1 = out_soup.new_tag("h1")
    out_h1["class"] = ["ltx_title_document"]
    out_h1.string = title_text
    article.append(out_h1)

    for child in list(body.children):
        if isinstance(child, Tag):
            article.append(child.extract())
        else:
            article.append(child)

    workdir_p = Path(workdir)
    workdir_p.mkdir(parents=True, exist_ok=True)
    out_path = workdir_p / "paper.html"
    out_path.write_text(str(out_soup), encoding="utf-8")
    return str(out_path)
