"""Convert a plain PDF into the same ltx_*-classed structure html_convert.py
and latex_convert.py produce, so it flows through restyle() unchanged --
same theming, highlighting, outline, and table/figure handling as an
arXiv/LaTeXML paper.

There's no semantic markup to read here (unlike LaTeX or saved publisher
HTML), so structure has to be *reconstructed* from layout: font sizes,
boldness, and position on the page. This is inherently best-effort --
it won't be as reliable as the LaTeXML or HTML paths, especially for
unusual layouts, but it's enough to get a normal single- or two-column
academic paper into a readable, highlightable, searchable form.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from .html_convert import _normalize_authors

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None

_BOLD_FLAG = 1 << 4

_HEADING_KEYWORDS = {
    "abstract": "abstract",
    "references": "references",
    "bibliography": "references",
    "acknowledgments": None,
    "acknowledgements": None,
}
_NUMBERED_HEADING_RE = re.compile(r"^(\d+(\.\d+){0,3})\.?\s+\S")
_APPENDIX_HEADING_RE = re.compile(r"^appendix\b", re.IGNORECASE)
_BRACKET_REF_RE = re.compile(r"(?=\[\d+\])")


class PdfConvertError(RuntimeError):
    pass


class _Block:
    __slots__ = ("text", "size", "bold", "bbox", "page")

    def __init__(self, text: str, size: float, bold: bool, bbox: tuple, page: int):
        self.text = text
        self.size = size
        self.bold = bold
        self.bbox = bbox
        self.page = page


def _dehyphenate_join(lines: list[str]) -> str:
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if out.endswith("-") and out[:-1] and out[-2:-1].isalpha():
            out = out[:-1] + line
        elif out:
            out = out + " " + line
        else:
            out = line
    return out


def _extract_blocks(doc) -> tuple[list[_Block], dict[int, float]]:
    """Flatten every page into position-ordered text blocks, applying a
    left/right two-column reading-order heuristic (very common in academic
    PDFs, and something PyMuPDF's raw block order gets wrong on its own)."""
    blocks: list[_Block] = []
    page_heights: dict[int, float] = {}

    for page_index, page in enumerate(doc):
        page_width = page.rect.width
        page_heights[page_index] = page.rect.height
        raw = page.get_text("dict")["blocks"]

        page_blocks = []
        for b in raw:
            if b.get("type") != 0:  # skip images
                continue
            lines_out = []
            sizes = []
            bold_votes = 0
            span_count = 0
            for line in b.get("lines", []):
                line_text = "".join(span["text"] for span in line.get("spans", []))
                if line_text.strip():
                    lines_out.append(line_text)
                for span in line.get("spans", []):
                    if span["text"].strip():
                        sizes.append(span["size"])
                        span_count += 1
                        if span["flags"] & _BOLD_FLAG:
                            bold_votes += 1
            text = _dehyphenate_join(lines_out)
            if not text:
                continue
            size = round(max(sizes), 1) if sizes else 0.0
            bold = span_count > 0 and bold_votes / span_count > 0.5
            x0, y0, x1, y1 = b["bbox"]
            page_blocks.append(_Block(text, size, bold, (x0, y0, x1, y1), page_index))

        blocks.extend(_order_two_column(page_blocks, page_width))

    return blocks, page_heights


def _order_two_column(page_blocks: list[_Block], page_width: float) -> list[_Block]:
    if not page_blocks:
        return []
    mid = page_width / 2
    margin = page_width * 0.03

    def classify(b: _Block) -> str:
        x0, y0, x1, y1 = b.bbox
        if (x1 - x0) > page_width * 0.6:
            return "full"
        if x1 <= mid + margin:
            return "left"
        if x0 >= mid - margin:
            return "right"
        return "full"

    tagged = sorted(((classify(b), b) for b in page_blocks), key=lambda t: t[1].bbox[1])

    ordered: list[_Block] = []
    run: list[tuple[str, _Block]] = []

    def flush_run():
        if not run:
            return
        left_run = sorted((b for c, b in run if c == "left"), key=lambda b: b.bbox[1])
        right_run = sorted((b for c, b in run if c == "right"), key=lambda b: b.bbox[1])
        ordered.extend(left_run)
        ordered.extend(right_run)
        run.clear()

    for c, b in tagged:
        if c == "full":
            flush_run()
            ordered.append(b)
        else:
            run.append((c, b))
    flush_run()
    return ordered


def _body_font_size(blocks: list[_Block]) -> float:
    sizes = Counter(b.size for b in blocks for _ in range(len(b.text)))
    if not sizes:
        return 10.0
    return sizes.most_common(1)[0][0]


def _strip_running_furniture(blocks: list[_Block], page_heights: dict[int, float]) -> list[_Block]:
    """Drop page numbers and repeated running headers/footers -- short
    text sitting in the top/bottom margin that either is purely numeric
    or repeats (ignoring digits) across multiple pages."""
    normalized_counts = Counter()
    for b in blocks:
        height = page_heights.get(b.page, 0) or 1
        y0, y1 = b.bbox[1], b.bbox[3]
        in_margin = y0 < height * 0.06 or y1 > height * 0.94
        if in_margin and len(b.text) < 80:
            normalized = re.sub(r"\d+", "#", b.text.strip().lower())
            normalized_counts[normalized] += 1

    num_pages = len(page_heights) or 1
    # Require at least 2 occurrences before treating something as "repeated" --
    # a single hit already clears the ratio threshold on short documents.
    repeated = {t for t, c in normalized_counts.items() if c >= 2 and (c >= 3 or c / num_pages > 0.4)}

    kept = []
    for b in blocks:
        height = page_heights.get(b.page, 0) or 1
        y0, y1 = b.bbox[1], b.bbox[3]
        in_margin = y0 < height * 0.06 or y1 > height * 0.94
        if in_margin and len(b.text) < 80:
            if b.text.strip().replace(" ", "").isdigit():
                continue
            normalized = re.sub(r"\d+", "#", b.text.strip().lower())
            if normalized in repeated:
                continue
        kept.append(b)
    return kept


def _heading_level_map(heading_sizes: list[float]) -> dict[float, int]:
    """Rank distinct heading font sizes so the biggest gets ltx_title_section
    (level 1), next ltx_title_subsection (2), then ltx_title_subsubsection (3+),
    since a document's actual point sizes vary too much to hardcode thresholds."""
    distinct = sorted(set(heading_sizes), reverse=True)
    return {size: min(i + 1, 3) for i, size in enumerate(distinct)}


_LEVEL_CLASS = {
    1: "ltx_title_section",
    2: "ltx_title_subsection",
    3: "ltx_title_subsubsection",
}


def _looks_like_heading(text: str, size: float, bold: bool, body_size: float) -> bool:
    if len(text) > 120:
        return False
    if _NUMBERED_HEADING_RE.match(text):
        return True
    lowered = text.strip().lower().rstrip(":")
    if lowered in _HEADING_KEYWORDS:
        return True
    if _APPENDIX_HEADING_RE.match(text):
        return True
    if size >= body_size * 1.15 and (bold or size >= body_size * 1.3):
        return True
    return False


def convert(input_path: str, workdir: str) -> str:
    """Extract a best-effort ltx_*-classed structure from a PDF via
    layout heuristics (font size, boldness, column position). Returns
    the path to the generated HTML file, same contract as
    latex_convert.convert() / html_convert.convert()."""
    if fitz is None:
        raise PdfConvertError("PyMuPDF (pymupdf) isn't installed -- required to read PDFs")

    src_path = Path(input_path).resolve()
    if not src_path.is_file():
        raise PdfConvertError(f"no such file: {input_path}")

    try:
        doc = fitz.open(str(src_path))
    except Exception as e:
        raise PdfConvertError(f"couldn't open PDF: {e}") from e

    if doc.page_count == 0:
        raise PdfConvertError("PDF has no pages")

    blocks, page_heights = _extract_blocks(doc)
    doc.close()

    if not blocks:
        raise PdfConvertError(
            "couldn't find any extractable text in this PDF -- it may be a scanned "
            "image without a text layer"
        )

    blocks = _strip_running_furniture(blocks, page_heights)
    body_size = _body_font_size(blocks)

    # Title: the largest-font block in the top half of page 1.
    page1_top = [b for b in blocks if b.page == 0 and b.bbox[1] < page_heights[0] * 0.5]
    title_block = max(page1_top, key=lambda b: b.size, default=None) or blocks[0]
    title_text = title_block.text.strip() or "Untitled"

    # Authors: the next short block(s) after the title, before the abstract
    # or first section heading -- best-effort, no affiliation/email parsing.
    author_names: list[str] = []
    title_idx = blocks.index(title_block)
    for b in blocks[title_idx + 1 : title_idx + 4]:
        if b is title_block:
            continue
        lowered = b.text.strip().lower().rstrip(":")
        if lowered in _HEADING_KEYWORDS or _NUMBERED_HEADING_RE.match(b.text):
            break
        if len(b.text) > 250 or b.size > title_block.size:
            break
        parts = re.split(r",|;|\band\b|\n", b.text)
        candidates = [p.strip() for p in parts if 2 < len(p.strip()) <= 60]
        if candidates:
            author_names.extend(candidates)
        break  # only the immediate next block is treated as the author line

    heading_sizes = [
        b.size
        for b in blocks
        if b is not title_block
        and _looks_like_heading(b.text, b.size, b.bold, body_size)
        and b.text.strip().lower().rstrip(":") not in _HEADING_KEYWORDS
    ]
    level_map = _heading_level_map(heading_sizes)

    out_soup = BeautifulSoup(
        '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>', "lxml"
    )
    article = out_soup.new_tag("article")
    article["class"] = ["ltx_document"]
    out_soup.body.append(article)

    h1 = out_soup.new_tag("h1")
    h1["class"] = ["ltx_title_document"]
    h1.string = title_text
    article.append(h1)

    if author_names:
        article.append(_normalize_authors(out_soup, author_names))

    # Walk the remaining blocks once, routing each into abstract /
    # bibliography / headings / paragraphs based on section state.
    skip_ids = {id(title_block)}
    if author_names:
        # the block we pulled author names from is blocks[title_idx+1]
        for b in blocks[title_idx + 1 : title_idx + 2]:
            skip_ids.add(id(b))

    abstract_div = out_soup.new_tag("div")
    abstract_div["class"] = ["ltx_abstract"]
    abstract_heading = out_soup.new_tag("h2")
    abstract_heading["class"] = ["ltx_title_abstract"]
    abstract_heading.string = "Abstract"
    abstract_div.append(abstract_heading)

    bib_section = out_soup.new_tag("section")
    bib_section["class"] = ["ltx_bibliography"]
    bib_heading = out_soup.new_tag("h2")
    bib_heading["class"] = ["ltx_title_bibliography"]
    bib_heading.string = "References"
    bib_section.append(bib_heading)
    bib_list = out_soup.new_tag("ul")
    bib_list["class"] = ["ltx_biblist"]
    bib_section.append(bib_list)

    state = "body"  # body | abstract | references
    ref_buffer: list[str] = []

    def flush_ref_buffer():
        combined = " ".join(ref_buffer)
        ref_buffer.clear()
        if not combined.strip():
            return
        entries = _BRACKET_REF_RE.split(combined)
        entries = [e.strip() for e in entries if e.strip()]
        if len(entries) < 2:
            entries = [combined.strip()]
        for entry in entries:
            if len(entry) < 15:
                continue
            li = out_soup.new_tag("li")
            li["class"] = ["ltx_bibitem"]
            span = out_soup.new_tag("span")
            span["class"] = ["ltx_bibblock"]
            span.string = entry
            li.append(span)
            bib_list.append(li)

    for b in blocks:
        if id(b) in skip_ids:
            continue
        text = b.text.strip()
        if not text:
            continue
        lowered = text.lower().rstrip(":")

        if lowered == "abstract" and state == "body":
            state = "abstract"
            continue
        if lowered in ("references", "bibliography"):
            flush_ref_buffer()
            state = "references"
            continue
        if state == "references" and _APPENDIX_HEADING_RE.match(text):
            flush_ref_buffer()
            state = "body"
            # fall through -- treat the appendix heading itself normally below

        if state == "abstract":
            if _looks_like_heading(text, b.size, b.bold, body_size):
                state = "body"
                # fall through, handled as a normal heading/paragraph below
            else:
                p = out_soup.new_tag("p")
                p["class"] = ["ltx_p"]
                p.string = text
                abstract_div.append(p)
                continue

        if state == "references":
            ref_buffer.append(text)
            continue

        # state == "body"
        if _looks_like_heading(text, b.size, b.bold, body_size):
            level = level_map.get(b.size, 3)
            heading = out_soup.new_tag("h2" if level == 1 else "h3" if level == 2 else "h4")
            heading["class"] = [_LEVEL_CLASS.get(level, "ltx_title_subsubsection")]
            heading.string = text
            article.append(heading)
        else:
            p = out_soup.new_tag("p")
            p["class"] = ["ltx_p"]
            p.string = text
            article.append(p)

    flush_ref_buffer()

    if abstract_div.find("p"):
        # Abstract belongs right after authors/frontmatter, ahead of the body.
        anchor = article.find("div", class_="ltx_authors") or h1
        anchor.insert_after(abstract_div)

    if bib_list.find("li"):
        article.append(bib_section)

    workdir_p = Path(workdir)
    workdir_p.mkdir(parents=True, exist_ok=True)
    out_path = workdir_p / "paper.html"
    out_path.write_text(str(out_soup), encoding="utf-8")
    return str(out_path)
