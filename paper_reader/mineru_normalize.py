"""Enrich MinerU cloud markdown/HTML so it matches the LaTeXML shapes
restyle() already understands: MathML equations, figure/table wrappers,
bibliography entries, and clickable citation / figure / table links.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

try:
    from latex2mathml.converter import convert as _tex_to_mathml
except ImportError:  # pragma: no cover - optional at import time; convert() errors clearly
    _tex_to_mathml = None  # type: ignore[assignment]


_DISPLAY_MATH_RE = re.compile(
    r"\$\$(.+?)\$\$\s*(?:\(\s*(\d+)\s*\))?",
    re.DOTALL,
)
# Inline $...$ but not $$...$$ and not currency/escaped \$...
_INLINE_MATH_RE = re.compile(
    r"(?<![\\$])\$(?!\$)(.+?)(?<!\\)\$(?!\$)",
    re.DOTALL,
)
# Also accept \(...\) / \[...\] if MinerU ever emits them.
_DISPLAY_BRACKET_RE = re.compile(
    r"\\\[(.+?)\\\]\s*(?:\(\s*(\d+)\s*\))?",
    re.DOTALL,
)
_INLINE_PAREN_RE = re.compile(r"\\\((.+?)\\\)", re.DOTALL)

# Placeholder so currency \$ and other escaped dollars never start math spans.
_ESCAPED_DOLLAR_TOKEN = "\ue000DOLLAR\ue001"

# Inline math that looks like prose (bad MinerU / unbalanced $) must not be
# fed to latex2mathml — that renders each word as adjacent <mi> letters.
_MAX_INLINE_MATH_CHARS = 400
_TEX_HINT_RE = re.compile(
    r"\\|[_^]|\\frac|\\sum|\\prod|\\sqrt|\\mathrm|\\mathbf|\\pmb|"
    r"\\boldsymbol|\\mathbb|\\times|\\cdot|\\leq|\\geq|\\neq|\\in\b|"
    r"\\left|\\right|\\begin|\\end|[=≠≈∈≤≥]"
)

_FIG_CAPTION_RE = re.compile(
    r"^(?:Figure|Fig\.?)\s*(\d+)\s*[|:.\-–—]?\s*(.*)$", re.IGNORECASE | re.DOTALL
)
_TABLE_CAPTION_RE = re.compile(
    r"^(?:Table)\s*(\d+)\s*[|:.\-–—]?\s*(.*)$", re.IGNORECASE | re.DOTALL
)
_REF_HEADING_RE = re.compile(
    r"^(references|bibliography|works\s+cited)\b", re.IGNORECASE
)
_BIB_ENTRY_RE = re.compile(
    r"^\s*\[(\d+)\]\s*(.+)$", re.DOTALL
)
_CITE_CLUSTER_RE = re.compile(
    r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]"
)
_FIG_MENTION_RE = re.compile(
    # Nature: "Fig. 1a" / "Figure 2b" — letter panel suffix is common
    r"\b((?:Figure|Fig\.?)\s+)(\d+)([a-z])?\b",
    re.IGNORECASE,
)
_TABLE_MENTION_RE = re.compile(
    r"\b((?:Table)\s+)(\d+)\b", re.IGNORECASE
)
# Nature / many publishers glue cite numbers onto the preceding word:
# "effort1–4", "PDB5". MinerU/OCR often inserts a space: "effort 1–4".
_NATURE_GLUED_CITE_RE = re.compile(
    r"(?<=[A-Za-z\)])"
    r"(\d{1,3}(?:\s*[,，]\s*\d{1,3}|\s*[–—−-]\s*\d{1,3})*)"
    r"(?=[\s,.;:\)\]]|$)"
)
_NATURE_SPACED_CITE_RE = re.compile(
    r"(?<=[A-Za-z\)])\s+"
    r"(\d{1,3}(?:\s*[,，]\s*\d{1,3}|\s*[–—−-]\s*\d{1,3})+)"
    r"(?=[\s,.;:\)\]]|$)"
)
# Spaced single cite after a word/paren + trailing punctuation: "determined 5," / ") 15 ,"
_NATURE_SPACED_SINGLE_CITE_RE = re.compile(
    r"(?<=[a-z\)])\s+"
    r"(\d{1,3})"
    r"(?=\s*[,;.](?:\s|$))"
)

_ABSTRACT_HEADING_RE = re.compile(r"^abstract\b", re.IGNORECASE)
_ABSTRACT_PREFIX_RE = re.compile(
    r"^\s*abstract\s*(?:[-—–:.]\s*|\s+)", re.IGNORECASE
)
_SECTION_HEADING_RE = re.compile(
    r"^(?:\d+[.\s]+|[IVXLC]+[.\s]+)?(?:introduction|background|related\s+work|"
    r"overview|preliminar|method|model|system|design|evaluation|experiment|"
    r"result|discussion|conclusion|reference|acknowledgment|acknowledgement|"
    r"main\b|reporting\s+summary|data\s+availability|code\s+availability)\b",
    re.IGNORECASE,
)
# "FLETCH RYDELL, Duke University, USA" / "Jane Doe, MIT, USA"
_AUTHOR_COMMA_LINE_RE = re.compile(
    r"^[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ''\-\.]{0,40}"
    r"(?:\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ''\-\.]{0,40}){0,5}"
    r",\s+\S.+$"
)
_AFFIL_HINT_RE = re.compile(
    r"\b(University|Université|Universitat|Institute|Laboratory|Laboratories|"
    r"Labs?\b|College|Department|Dept\.|Inc\.|Ltd\.|Corp\.|NVIDIA|Google|"
    r"Meta|Microsoft|Amazon|Samsung|Intel|IBM|OpenAI|DeepMind|USA|UK|China|"
    r"Germany|France|Japan|Korea|Canada|Switzerland|Sweden|Netherlands)\b",
    re.IGNORECASE,
)
_EMAIL_LINE_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_DOI_OR_META_LINE_RE = re.compile(
    r"^(?:https?://)?(?:dx\.)?doi\.org/|"
    r"^\s*doi:\s*|"
    r"^\s*10\.\d{4,9}/|"
    r"^(?:Received|Accepted|Published|Open access|Check for updates|Article)\b",
    re.IGNORECASE,
)
# Nature: "1 DeepMind, London, UK. 2 School of Biological Sciences…"
_NUMBERED_AFFIL_RE = re.compile(
    r"^\s*\d+\s+\S.+\b(?:"
    r"University|Institute|Laboratory|Lab\b|College|Department|School|"
    r"DeepMind|Google|OpenAI|Inc\.|Ltd\.|UK|USA|China|Germany|France|"
    r"Japan|Korea|Canada|Switzerland|Sweden|Netherlands|London|Seoul"
    r")\b",
    re.IGNORECASE,
)
_NATURE_MARKER_RE = re.compile(
    r"10\.1038/|nature\.com|Published online|Check for updates|"
    r"nature research|Springer Nature",
    re.IGNORECASE,
)
# Mathematical Alphanumeric Symbols + a few letterlike math forms MinerU uses
# for italicized words (𝑠ℎ𝑖𝑚𝑠). Astral-plane codepoints sometimes arrive as
# U+FFFD with only BMP survivors like ℎ (U+210E) left.
_MATH_ALPHA_RE = re.compile(
    r"[\U0001D400-\U0001D7FF\u2102-\u2134\u2145-\u2149]"
)


def repair_unicode_artifacts(text: str) -> str:
    """Fix MinerU Unicode damage: math-italic words and common corruptions.

    MinerU often emits Mathematical Italic letters for emphasis. Some of those
    (outside the BMP) get replaced with U+FFFD in the export, which the reader
    shows as ``�``. Map intact math alphanumerics via NFKC and repair a few
    high-confidence corrupted runs.
    """
    import unicodedata

    if not text:
        return text

    # Repair corrupted runs BEFORE NFKC (which would turn ℎ into ASCII h and
    # break the �ℎ��� pattern). Pattern is specific to math-italic "shims"
    # surviving only as Planck-constant h amid U+FFFD.
    text = text.replace("\ufffd\u210e\ufffd\ufffd\ufffd", "shims")
    text = text.replace("\ufffdh\ufffd\ufffd\ufffd", "shims")

    # Murϕ / Murphi model checker (U+FFFD is not a word char, so avoid \\b after it)
    text = re.sub(r"\bMur\ufffd(?=\s|[\[\].,;:]|$)", "Murphi", text)
    text = re.sub(r"\bMur[\u03d5\u03c6](?=\s|[\[\].,;:]|$)", "Murphi", text)

    # CCICheck μhb graphs
    text = re.sub(r"\ufffdhb\b", "μhb", text)
    text = re.sub(r"\u03bchb\b", "μhb", text)

    def _nfkc_math(m: re.Match[str]) -> str:
        return unicodedata.normalize("NFKC", m.group(0))

    text = _MATH_ALPHA_RE.sub(_nfkc_math, text)

    return text


def _looks_like_tex(tex: str) -> bool:
    """True if a $-delimited span is plausibly math, not swallowed prose."""
    s = tex.strip()
    if not s or len(s) > _MAX_INLINE_MATH_CHARS:
        return False
    # Multi-paragraph / heading content is never inline math.
    if "\n\n" in tex or re.search(r"\n\s*#", tex):
        return False
    # Short symbol-ish spans (x, y_i, A, 0.5, ...) — allow without \\.
    if len(s) <= 24:
        return True
    if _TEX_HINT_RE.search(s):
        return True
    # Longer spans with no TeX hints are almost certainly prose.
    return False


def _mathml_fragment(tex: str, display: bool, eq_num: str | None = None) -> str:
    if _tex_to_mathml is None:
        raise RuntimeError("latex2mathml is required for MinerU equation rendering")
    cleaned = tex.strip().replace("\n", " ")
    # MinerU often inserts spaces around TeX tokens ("h _ { t }"); collapse
    # only runs of spaces that aren't meaningful, keep braces intact.
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    # Pull \tag{N} / \tag*{N} into the equation number if present.
    tag_m = re.search(r"\\tag\*?\{([^}]+)\}", cleaned)
    if tag_m and not eq_num:
        eq_num = tag_m.group(1).strip()
        cleaned = (cleaned[: tag_m.start()] + cleaned[tag_m.end() :]).strip()
    try:
        mathml = _tex_to_mathml(cleaned)
    except Exception:
        # Fall back to a visible TeX stub rather than dropping the equation.
        esc = (
            cleaned.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return (
            f'<span class="ltx_Math" data-tex="{esc}">'
            f"<code>{esc}</code></span>"
        )
    # latex2mathml emits display="inline" always; force block when needed.
    if display:
        mathml = re.sub(
            r'\bdisplay="inline"',
            'display="block"',
            mathml,
            count=1,
        )
        if 'display="' not in mathml:
            mathml = mathml.replace("<math ", '<math display="block" ', 1)
        tag_html = ""
        if eq_num:
            tag_html = (
                f'<span class="ltx_tag ltx_tag_equation">({eq_num})</span>'
            )
        return (
            f'<div class="ltx_equation">'
            f'<span class="ltx_Math">{mathml}</span>{tag_html}</div>'
        )
    return f'<span class="ltx_Math">{mathml}</span>'


def _replace_math_in_markdown(md: str) -> str:
    """Swap TeX delimiters for MathML (or stubs) before markdown parsing so
    Python-Markdown cannot mangle `$` / underscores inside formulas."""

    md = repair_unicode_artifacts(md)

    # Protect escaped dollars (\\$900 million) so they never open a math span.
    md = md.replace("\\$", _ESCAPED_DOLLAR_TOKEN)

    def repl_display(m: re.Match[str]) -> str:
        eq_num = m.group(2) if m.lastindex and m.lastindex >= 2 else None
        return (
            "\n\n"
            + _mathml_fragment(m.group(1), display=True, eq_num=eq_num)
            + "\n\n"
        )

    def repl_inline(m: re.Match[str]) -> str:
        body = m.group(1)
        if not _looks_like_tex(body):
            # Leave prose alone; keep literal dollars visible.
            return f"${body}$"
        return _mathml_fragment(body, display=False)

    md = _DISPLAY_MATH_RE.sub(repl_display, md)
    md = _DISPLAY_BRACKET_RE.sub(repl_display, md)
    md = _INLINE_MATH_RE.sub(repl_inline, md)
    md = _INLINE_PAREN_RE.sub(repl_inline, md)

    # Restore currency / escaped dollars as a plain $ character.
    md = md.replace(_ESCAPED_DOLLAR_TOKEN, "$")
    return md


def _next_significant_sibling(node: Tag | NavigableString) -> Tag | None:
    sib = node.next_sibling
    while sib is not None and isinstance(sib, NavigableString) and not str(sib).strip():
        sib = sib.next_sibling
    return sib if isinstance(sib, Tag) else None


def _clean_caption_rest(rest: str) -> str:
    return re.sub(r"^[|:.\-–—]\s*", "", (rest or "").strip())


def _attach_figure_caption(soup: BeautifulSoup, fig: Tag, num: str, rest: str) -> None:
    rest = _clean_caption_rest(rest)
    cap = soup.new_tag("figcaption")
    cap["class"] = ["ltx_caption"]
    cap.string = f"Figure {num}" + (f": {rest}" if rest else "")
    fig.append(cap)
    fig["id"] = f"fig.{num}"


def _ensure_figure_table_ids(body: Tag) -> None:
    """Assign #fig.N / #tab.N when Docling already wrapped figures with captions."""
    for fig in body.find_all("figure"):
        if fig.get("id"):
            continue
        cap = fig.find("figcaption")
        text = (cap or fig).get_text(" ", strip=True)
        m = _FIG_CAPTION_RE.match(text)
        if m and "ltx_table" not in (fig.get("class") or []):
            fig["id"] = f"fig.{m.group(1)}"
            classes = fig.get("class") or []
            if "ltx_figure" not in classes:
                fig["class"] = list(classes) + ["ltx_figure"]
            continue
        m = _TABLE_CAPTION_RE.match(text)
        if m:
            fig["id"] = f"tab.{m.group(1)}"
            classes = fig.get("class") or []
            for cls in ("ltx_figure", "ltx_table"):
                if cls not in classes:
                    classes = list(classes) + [cls]
            fig["class"] = classes


def _unwrap_lonely_paragraph(fig: Tag) -> None:
    """If figure is the sole content of a <p>, promote it out of the paragraph."""
    parent = fig.parent
    if parent is None or parent.name != "p":
        return
    meaningful = [
        c
        for c in parent.children
        if not (isinstance(c, NavigableString) and not str(c).strip())
    ]
    if meaningful == [fig]:
        parent.insert_before(fig.extract())
        parent.decompose()


def _paragraph_caption_match(node: Tag) -> re.Match[str] | None:
    """Match a Figure N caption, allowing leading junk from a co-located <img>."""
    text = node.get_text(" ", strip=True)
    m = _FIG_CAPTION_RE.match(text)
    if m:
        return m
    # Same <p> as the image: "… Fig. 1. Caption" after stripping empty alt text.
    m = re.search(
        r"(?:^|\s)((?:Figure|Fig\.?)\s*(\d+)\s*[:.]?\s*(.*))$",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    # Re-run with the standard pattern on the figure-caption slice.
    return _FIG_CAPTION_RE.match(m.group(1).strip())


def _wrap_orphan_images(soup: BeautifulSoup, body: Tag) -> None:
    """Wrap bare <img> as figure.ltx_figure and attach a following Figure N caption."""
    for img in list(body.find_all("img")):
        if not isinstance(img, Tag) or getattr(img, "decomposed", False):
            continue
        if img.find_parent("figure") is not None:
            continue
        fig = soup.new_tag("figure")
        fig["class"] = ["ltx_figure"]
        img_classes = img.get("class") or []
        if "ltx_graphics" not in img_classes:
            img["class"] = list(img_classes) + ["ltx_graphics"]
        img.replace_with(fig)
        fig.append(img)

        caption_el = None
        m = None

        # Prefer caption text in the same <p> as the image
        # (MinerU: <p><img/><br/>Fig. N. …</p>).
        parent = fig.parent
        if isinstance(parent, Tag) and parent.name == "p":
            m = _paragraph_caption_match(parent)
            if m:
                caption_el = parent

        # Else a following caption-only paragraph (no nested images).
        if caption_el is None:
            for candidate in (
                _next_significant_sibling(fig),
                _next_significant_sibling(parent) if isinstance(parent, Tag) else None,
            ):
                if not isinstance(candidate, Tag) or candidate.name != "p":
                    continue
                if candidate.find("img") is not None:
                    continue
                m = _FIG_CAPTION_RE.match(candidate.get_text(" ", strip=True))
                if m:
                    caption_el = candidate
                    break

        if caption_el is not None and m is not None:
            num, rest = m.group(1), _clean_caption_rest(m.group(2))
            if caption_el is parent:
                # Strip everything except the figure from the shared <p>.
                for child in list(caption_el.children):
                    if child is fig:
                        continue
                    if isinstance(child, NavigableString) or (
                        isinstance(child, Tag) and child.name == "br"
                    ):
                        child.extract()
                _attach_figure_caption(soup, fig, num, rest)
                _unwrap_lonely_paragraph(fig)
            else:
                _attach_figure_caption(soup, fig, num, rest)
                caption_el.decompose()
                _unwrap_lonely_paragraph(fig)
            continue

        _unwrap_lonely_paragraph(fig)


def _wrap_tables(soup: BeautifulSoup, body: Tag) -> None:
    """Ensure every table is figure.ltx_table > table.ltx_tabular, with caption."""
    for table in list(body.find_all("table")):
        classes = table.get("class") or []
        if "ltx_tabular" not in classes:
            table["class"] = list(classes) + ["ltx_tabular"]

        fig = table.find_parent("figure")
        if fig is None:
            fig = soup.new_tag("figure")
            fig["class"] = ["ltx_figure", "ltx_table"]
            table.replace_with(fig)
            fig.append(table)
        else:
            fig_classes = fig.get("class") or []
            for cls in ("ltx_figure", "ltx_table"):
                if cls not in fig_classes:
                    fig_classes = list(fig_classes) + [cls]
            fig["class"] = fig_classes

        if fig.find("figcaption") is not None:
            continue

        # Prefer a preceding "Table N:" paragraph, else following
        # (including the next sibling of a wrapping <p>).
        candidates: list[Tag] = []
        for start in (fig, fig.parent if fig.parent is not None else None):
            if start is None:
                continue
            prev = start.previous_sibling
            while prev is not None and isinstance(prev, NavigableString) and not str(prev).strip():
                prev = prev.previous_sibling
            if isinstance(prev, Tag) and prev.name == "p":
                candidates.append(prev)
            nxt = _next_significant_sibling(start)
            if isinstance(nxt, Tag) and nxt.name == "p":
                candidates.append(nxt)

        for cand in candidates:
            m = _TABLE_CAPTION_RE.match(cand.get_text(" ", strip=True))
            if not m:
                continue
            num, rest = m.group(1), _clean_caption_rest(m.group(2))
            cap = soup.new_tag("figcaption")
            cap["class"] = ["ltx_caption"]
            cap.string = f"Table {num}" + (f": {rest}" if rest else "")
            fig.insert(0, cap)
            fig["id"] = f"tab.{num}"
            cand.decompose()
            break


def _split_bib_blob(text: str) -> list[str]:
    """Split a blob that may contain multiple '[N] …' or 'N. …' entries."""
    text = text.strip()
    if not text:
        return []
    parts = re.split(r"(?=\[\d+\]|(?:(?<=\s)|^)\d{1,3}\.\s+)", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        return parts
    return [text]


def _iter_ref_entry_texts(node: Tag) -> list[str]:
    """Yield bibliography entry strings from a block that may use <br>-separated lines."""
    raw_html = node.decode_contents()
    if "<br" in raw_html.lower():
        chunks = re.split(r"<br\s*/?>", raw_html, flags=re.IGNORECASE)
        texts: list[str] = []
        for chunk in chunks:
            t = BeautifulSoup(chunk, "lxml").get_text(" ", strip=True)
            if t:
                texts.extend(_split_bib_blob(t))
        return texts
    text = node.get_text(" ", strip=True)
    return _split_bib_blob(text) if text else []


def _parse_bib_line(text: str) -> tuple[str, str] | None:
    m = _BIB_ENTRY_RE.match(text)
    if m:
        return m.group(1), m.group(2).strip()
    m2 = re.match(r"^\s*(\d+)\.\s+(.+)$", text, re.DOTALL)
    if m2 and len(m2.group(2)) > 20:
        return m2.group(1), m2.group(2).strip()
    # Nature two-column refs sometimes drop the period: "76  Wu, T. …"
    m3 = re.match(r"^\s*(\d{1,3})\s{2,}(.+)$", text, re.DOTALL)
    if m3 and len(m3.group(2)) > 20 and re.match(r"^[A-ZÀ-ÖØ-Þ]", m3.group(2)):
        return m3.group(1), m3.group(2).strip()
    return None


def _harvest_orphan_bib_entries(body: Tag) -> list[tuple[Tag, tuple[str, str]]]:
    """Find numbered bibliography paragraphs when no References heading exists.

    Nature PDFs often end with ``1. Author…`` / ``76. Author…`` blocks without
    a clear heading after Methods / reporting summary stripping.
    """
    found: list[tuple[Tag, tuple[str, str]]] = []
    for el in body.find_all(["p", "li"]):
        if el.find_parent("figure") is not None:
            continue
        text = el.get_text(" ", strip=True)
        parsed = _parse_bib_line(text)
        if not parsed:
            continue
        num, rest = parsed
        # Skip tiny / non-bibliographic numbered lines (figure panels, etc.)
        if len(rest) < 40:
            continue
        if not re.search(r"\b(et al|doi|Nature|Science|Cell|Phys|Chem|Biol|IEEE|ACM|Proc\.|Journal|Press)\b", rest, re.I):
            # Still accept author-like "Last, F. … (YEAR)"
            if not re.search(r"\(\s*19|20\d{2}\s*\)", rest):
                if text.count(",") < 1:
                    continue
        found.append((el, parsed))
    if len(found) < 3:
        return []
    # Prefer a trailing run with mostly increasing cite numbers
    best: list[tuple[Tag, tuple[str, str]]] = []
    run: list[tuple[Tag, tuple[str, str]]] = []
    prev_n = -1
    for item in found:
        n = int(item[1][0])
        if not run or n >= prev_n - 2:  # allow small disorder from 2-col layout
            run.append(item)
        else:
            if len(run) > len(best):
                best = run
            run = [item]
        prev_n = n
    if len(run) > len(best):
        best = run
    return best if len(best) >= 3 else found


def _emit_bibliography_section(
    soup: BeautifulSoup,
    body: Tag,
    entries: list[tuple[str, str]],
    to_remove: list[Tag],
    heading: Tag | None,
) -> None:
    if len(entries) < 2:
        return
    # Dedupe by number (keep longest text)
    by_num: dict[str, str] = {}
    for num, text in entries:
        prev = by_num.get(num, "")
        if len(text) >= len(prev):
            by_num[num] = text
    ordered = sorted(by_num.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)

    section = soup.new_tag("section")
    section["class"] = ["ltx_bibliography"]
    if heading is None:
        heading = soup.new_tag("h2")
        heading.string = "References"
    heading["class"] = (heading.get("class") or []) + ["ltx_title_bibliography"]
    if heading.parent is not None:
        heading.extract()
    section.append(heading)

    ul = soup.new_tag("ul")
    ul["class"] = ["ltx_biblist"]
    for num, text in ordered:
        li = soup.new_tag("li")
        li["class"] = ["ltx_bibitem"]
        li["id"] = f"bib.{num}"
        tag = soup.new_tag("span")
        tag["class"] = ["ltx_tag", "ltx_tag_bibitem"]
        tag.string = f"[{num}]"
        block = soup.new_tag("span")
        block["class"] = ["ltx_bibblock"]
        block.string = text
        li.append(tag)
        li.append(NavigableString(" "))
        li.append(block)
        ul.append(li)
    section.append(ul)

    for el in to_remove:
        try:
            el.decompose()
        except Exception:
            pass

    body.append(section)


def _build_bibliography(soup: BeautifulSoup, body: Tag) -> None:
    """Turn a References section (or Nature-style orphan numbered list) into bib."""
    heading = None
    for h in body.find_all(["h1", "h2", "h3", "h4"]):
        if _REF_HEADING_RE.match(h.get_text(" ", strip=True)):
            heading = h
            break

    entries: list[tuple[str, str]] = []
    to_remove: list[Tag] = []

    if heading is not None:
        node = heading.next_sibling
        while node is not None:
            nxt = node.next_sibling
            if isinstance(node, Tag) and node.name in ("h1", "h2", "h3", "h4"):
                break
            if isinstance(node, Tag) and node.name in ("p", "li", "div"):
                lines = _iter_ref_entry_texts(node)
                matched_any = False
                for line in lines:
                    parsed = _parse_bib_line(line)
                    if parsed:
                        entries.append(parsed)
                        matched_any = True
                    elif line and len(line) > 30 and entries:
                        num, prev = entries[-1]
                        entries[-1] = (num, f"{prev} {line}")
                        matched_any = True
                if matched_any:
                    to_remove.append(node)
            elif isinstance(node, Tag) and node.name in ("ol", "ul"):
                for li in node.find_all("li", recursive=False):
                    for line in _iter_ref_entry_texts(li):
                        parsed = _parse_bib_line(line)
                        if parsed:
                            entries.append(parsed)
                        elif line:
                            entries.append((str(len(entries) + 1), line))
                to_remove.append(node)
            node = nxt
    else:
        orphan = _harvest_orphan_bib_entries(body)
        for el, parsed in orphan:
            entries.append(parsed)
            to_remove.append(el)

    _emit_bibliography_section(soup, body, entries, to_remove, heading)


def _nature_fig_num_from_href(href: str) -> str | None:
    if not href:
        return None
    m = re.search(r"#Fig(\d+)\b", href, re.I)
    if m:
        return m.group(1)
    m = re.search(r"/figures/(\d+)\b", href, re.I)
    if m:
        return m.group(1)
    m = re.search(r"#fig\.(\d+)\b", href, re.I)
    if m:
        return m.group(1)
    return None


def _nature_bib_num_from_href(href: str) -> str | None:
    if not href:
        return None
    frag = href.rsplit("#", 1)[-1] if "#" in href else ""
    m = re.match(r"(?:bib\.)?ref-CR(\d+)$", frag, re.I)
    if m:
        return m.group(1)
    m = re.match(r"bib\.(\d+)$", frag, re.I)
    if m:
        return m.group(1)
    return None


def _normalize_crossrefs(soup: BeautifulSoup, body: Tag) -> None:
    """Rewrite publisher figure/cite anchors to local #fig.N / #bib.N.

    Nature HTML (and some PDF→HTML pipelines that preserve those anchors)
    uses ``/articles/…#Fig1`` and ``#ref-CR12``. The reader only resolves
    ``#fig.1`` / ``#bib.12`` for hover previews and in-page navigation.
    """
    # Normalize bibliography ids: bib.ref-CR12 / ref-CR12 → bib.12
    for el in list(body.find_all(id=True)):
        eid = str(el.get("id") or "")
        m = re.match(r"^(?:bib\.)?ref-CR(\d+)$", eid, re.I)
        if m:
            el["id"] = f"bib.{m.group(1)}"
            continue
        m = re.match(r"^bib\.bib\.(\d+)$", eid, re.I)
        if m:
            el["id"] = f"bib.{m.group(1)}"

    for a in list(body.find_all("a", href=True)):
        href = a.get("href") or ""
        fig_n = _nature_fig_num_from_href(href)
        if fig_n:
            # Drop Nature "Full size image" chrome links; keep real Fig. Na anchors.
            label = a.get_text(" ", strip=True).lower()
            if "full size" in label or "c-article__pill-button" in (a.get("class") or []):
                a.decompose()
                continue
            a["href"] = f"#fig.{fig_n}"
            classes = a.get("class") or []
            if "ltx_ref" not in classes:
                a["class"] = list(classes) + ["ltx_ref"]
            continue
        bib_n = _nature_bib_num_from_href(href)
        if not bib_n:
            # Absolute same-page cite that still points at a live bib target
            if "#" in href and not href.lstrip().startswith("#"):
                frag = href.rsplit("#", 1)[1]
                target = soup.find(id=frag) or soup.find(id=f"bib.{frag}")
                if target is not None and (
                    str(target.get("id", "")).startswith("bib.")
                    or re.match(r"(?:bib\.)?ref-CR\d+$", frag, re.I)
                ):
                    m = re.search(r"(\d+)$", str(target.get("id") or frag))
                    if m:
                        bib_n = m.group(1)
        if not bib_n:
            continue
        a["href"] = f"#bib.{bib_n}"
        classes = a.get("class") or []
        if "ltx_ref" not in classes:
            a["class"] = list(classes) + ["ltx_ref"]
        if a.find_parent("cite") is None:
            cite = soup.new_tag("cite")
            cite["class"] = ["ltx_cite", "ltx_citemacro_cite"]
            parent_sup = a.parent if a.parent and a.parent.name == "sup" else None
            if parent_sup is not None and parent_sup.parent is not None:
                parent_sup.wrap(cite)
            else:
                a.wrap(cite)


def _plausible_cite_nums(nums: list[str]) -> bool:
    """Reject years / absurd cite ids that Nature glued-cite regex can catch."""
    if not nums:
        return False
    ints: list[int] = []
    for n in nums:
        if not re.fullmatch(r"\d+", n):
            return False
        ints.append(int(n))
    if max(ints) > 400 or min(ints) < 1:
        return False
    # Standalone years are not citations
    if len(ints) == 1 and 1900 <= ints[0] <= 2099:
        return False
    return True


def _cite_nums_from_cluster(nums_raw: str) -> list[str]:
    nums: list[str] = []
    for chunk in re.split(r"\s*,\s*", nums_raw):
        chunk = chunk.strip()
        range_m = re.match(r"^(\d+)\s*[–—−-]\s*(\d+)$", chunk)
        if range_m:
            a, b = int(range_m.group(1)), int(range_m.group(2))
            if 0 < a <= b < a + 40:
                nums.extend(str(n) for n in range(a, b + 1))
                continue
        if re.fullmatch(r"\d+", chunk):
            nums.append(chunk)
    return nums


def _make_cite_element(soup: BeautifulSoup, nums: list[str], *, bracketed: bool) -> Tag:
    cite = soup.new_tag("cite")
    cite["class"] = ["ltx_cite", "ltx_citemacro_cite"]
    if bracketed:
        cite.append(NavigableString("["))
    for i, n in enumerate(nums):
        if i:
            cite.append(NavigableString("," if bracketed else ", "))
        a = soup.new_tag("a", attrs={"class": ["ltx_ref"], "href": f"#bib.{n}"})
        a.string = n
        cite.append(a)
    if bracketed:
        cite.append(NavigableString("]"))
    else:
        # Nature-style superscript appearance via <sup>
        wrap = soup.new_tag("sup")
        for child in list(cite.children):
            wrap.append(child.extract())
        cite.append(wrap)
    return cite


_NON_CITE_LABEL_RE = re.compile(
    r"(?:Fig(?:ure)?\.?|Tables?|Eq(?:uation)?\.?|Section|Sec\.|"
    r"Chapter|Ch\.|Appendix|Extended\s+Data|Extended\s+Fig\.?)\s*$",
    re.IGNORECASE,
)


def _preceded_by_non_cite_label(raw: str, start: int) -> bool:
    """True if this digit cluster is a figure/table/eq number, not a citation."""
    prefix = raw[max(0, start - 28) : start]
    return bool(_NON_CITE_LABEL_RE.search(prefix))


def _preceded_by_acronym(raw: str, start: int) -> bool:
    """True for CASP14 / COVID19-style tokens — not Nature superscript cites."""
    i = start - 1
    while i >= 0 and raw[i].isalpha():
        i -= 1
    word = raw[i + 1 : start]
    return len(word) >= 2 and word.isupper()


def _collect_nature_cite_spans(raw: str) -> list[tuple[int, int, list[str], bool]]:
    """Glued (effort1–4) and spaced (effort 1–4 / determined 5,) Nature cites."""
    spans: list[tuple[int, int, list[str], bool]] = []
    for regex in (_NATURE_GLUED_CITE_RE, _NATURE_SPACED_CITE_RE, _NATURE_SPACED_SINGLE_CITE_RE):
        for m in regex.finditer(raw):
            if _preceded_by_non_cite_label(raw, m.start()):
                continue
            # Glued digits on acronyms (CASP14) are not citations
            if regex is _NATURE_GLUED_CITE_RE and _preceded_by_acronym(raw, m.start()):
                continue
            nums = _cite_nums_from_cluster(m.group(1))
            if nums and _plausible_cite_nums(nums):
                spans.append((m.start(), m.end(), nums, False))
    return spans


def _rewrite_citations(soup: BeautifulSoup, body: Tag) -> None:
    """Turn [1] / [2, 3] and Nature glued/spaced cites into ltx_cite links."""
    nature_like = _looks_like_nature_doc(body)

    for text_node in list(body.find_all(string=True)):
        if not isinstance(text_node, NavigableString):
            continue
        if text_node.find_parent(["cite", "pre", "code", "math", "script", "style", "a", "figcaption"]):
            continue
        parent_li = text_node.find_parent("li", class_="ltx_bibitem")
        if parent_li is not None:
            continue
        raw = str(text_node)
        has_bracket = bool(_CITE_CLUSTER_RE.search(raw))
        has_nature = bool(
            nature_like
            and (
                _NATURE_GLUED_CITE_RE.search(raw)
                or _NATURE_SPACED_CITE_RE.search(raw)
                or _NATURE_SPACED_SINGLE_CITE_RE.search(raw)
            )
        )
        if not has_bracket and not has_nature:
            continue

        spans: list[tuple[int, int, list[str], bool]] = []
        for m in _CITE_CLUSTER_RE.finditer(raw):
            nums = _cite_nums_from_cluster(m.group(1))
            if nums and _plausible_cite_nums(nums):
                spans.append((m.start(), m.end(), nums, True))
        if nature_like:
            for start, end, nums, bracketed in _collect_nature_cite_spans(raw):
                if any(not (end <= s or start >= e) for s, e, _, _ in spans):
                    continue
                spans.append((start, end, nums, bracketed))

        spans.sort(key=lambda t: t[0])
        filtered: list[tuple[int, int, list[str], bool]] = []
        cur_end = -1
        for start, end, nums, bracketed in spans:
            if start < cur_end:
                continue
            filtered.append((start, end, nums, bracketed))
            cur_end = end

        parts: list[object] = []
        last = 0
        matched = False
        for start, end, nums, bracketed in filtered:
            if start > last:
                parts.append(raw[last:start])
            matched = True
            parts.append(_make_cite_element(soup, nums, bracketed=bracketed))
            last = end

        if not matched:
            continue
        if last < len(raw):
            parts.append(raw[last:])
        frag = soup.new_tag("span")
        for part in parts:
            if isinstance(part, str):
                if part:
                    frag.append(NavigableString(part))
            else:
                frag.append(part)
        text_node.replace_with(frag)
        frag.unwrap()


def _link_figure_table_mentions(soup: BeautifulSoup, body: Tag) -> None:
    """Make 'Figure 1' / 'Fig. 1a' / 'Table 2' link to #fig.1 / #tab.2 when ids exist."""
    fig_ids = {
        el["id"].split(".", 1)[1]
        for el in body.find_all(id=True)
        if str(el["id"]).startswith("fig.")
    }
    tab_ids = {
        el["id"].split(".", 1)[1]
        for el in body.find_all(id=True)
        if str(el["id"]).startswith("tab.")
    }
    if not fig_ids and not tab_ids:
        return

    for text_node in list(body.find_all(string=True)):
        if not isinstance(text_node, NavigableString):
            continue
        if text_node.find_parent(["a", "cite", "figcaption", "pre", "code", "math", "script"]):
            continue
        raw = str(text_node)

        # (start, end, display_text, href)
        events: list[tuple[int, int, str, str]] = []
        if fig_ids:
            for m in _FIG_MENTION_RE.finditer(raw):
                num = m.group(2)
                if num in fig_ids:
                    events.append((m.start(), m.end(), m.group(0), f"#fig.{num}"))
        if tab_ids:
            for m in _TABLE_MENTION_RE.finditer(raw):
                num = m.group(2)
                if num in tab_ids:
                    events.append((m.start(), m.end(), m.group(0), f"#tab.{num}"))
        if not events:
            continue
        events.sort(key=lambda e: e[0])

        parts: list[object] = []
        last = 0
        for start, end, full, href in events:
            if start < last:
                continue
            if start > last:
                parts.append(raw[last:start])
            a = soup.new_tag("a", attrs={"class": ["ltx_ref"], "href": href})
            a.string = full
            parts.append(a)
            last = end
        if last < len(raw):
            parts.append(raw[last:])
        if len(parts) == 1 and isinstance(parts[0], str):
            continue
        frag = soup.new_tag("span")
        for part in parts:
            if isinstance(part, str):
                if part:
                    frag.append(NavigableString(part))
            else:
                frag.append(part)
        text_node.replace_with(frag)
        frag.unwrap()


def prepare_mineru_markdown(md: str) -> str:
    """Preprocess MinerU markdown (math → MathML) before markdown.markdown()."""
    return _replace_math_in_markdown(md)


def _strip_html_sup(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _clean_author_noise(text: str) -> str:
    """Strip HTML superscripts / footnote markers MinerU leaves on names."""
    # Avoid BeautifulSoup's "looks like a URL" warning on doi lines.
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    # Nature: "Jumper1,4" / "Jumper 1,4 ✉" — affiliation superscript clusters
    text = re.sub(r"(?<=[A-Za-zÀ-ÿ])\s*\d+(?:\s*,\s*\d+)*", "", text)
    text = re.sub(r"[*†‡§¶#✉]+", "", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\s+&\s+", ", ", text)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_nature_doc(body: Tag) -> bool:
    text = body.get_text(" ", strip=True)
    if _NATURE_MARKER_RE.search(text[:4000]) or _NATURE_MARKER_RE.search(text):
        return True
    # Dense superscript-style cites without a Nature masthead (OCR truncations).
    spaced = len(_NATURE_SPACED_CITE_RE.findall(text[:8000]))
    glued = len(_NATURE_GLUED_CITE_RE.findall(text[:8000]))
    return spaced + glued >= 8


def _looks_like_author_paragraph(text: str) -> bool:
    raw = text
    text = _clean_author_noise(text)
    if not text:
        return False
    low = text.lower()
    if low.startswith(("abstract", "figure", "table", "keywords", "index terms")):
        return False
    if _NUMBERED_AFFIL_RE.match(raw) or _NUMBERED_AFFIL_RE.match(text):
        return False
    words = text.split()
    # Nature papers often pack 20–40 authors into one long line.
    max_len = 2500 if (text.count(",") >= 4 or "✉" in raw) else 600
    if len(text) > max_len:
        return False
    if _EMAIL_LINE_RE.search(text) and len(words) < 12:
        return True  # email row between authors and abstract
    if _AFFIL_HINT_RE.search(text) and len(text) < 400 and text.count(".") < 8 and len(words) < 40:
        # Dense Nature author lines can mention "DeepMind" etc. in the same
        # paragraph as names — still treat as authors when highly comma-dense.
        if text.count(",") < 5:
            return True
    if _AUTHOR_COMMA_LINE_RE.match(text) and len(text) < 200 and len(words) < 20:
        return True
    # Multi-author single line: "Alice, Bob, Carol" / Nature "A1, B1, C1 & D1"
    if text.count(",") >= 2 and len(words) <= 120:
        parts = [p.strip() for p in re.split(r",|\sand\s", text) if p.strip()]
        name_like = [
            p for p in parts
            if 1 <= len(p.split()) <= 5 and re.match(r"^[A-ZÀ-ÖØ-Þ]", p)
            and not _AFFIL_HINT_RE.search(p)
        ]
        if len(name_like) >= 2 and not text.rstrip().endswith("."):
            return True
        # Nature: long author lists sometimes end with a period after ✉ cleanup
        if len(name_like) >= 4:
            return True
    return False


def _looks_like_email_dump(text: str) -> bool:
    if not _EMAIL_LINE_RE.search(text):
        return False
    return (
        text.count("@") >= 2
        or text.lstrip().startswith("{")
        or len(text) < 280
    )


def _looks_like_abstract_paragraph(text: str) -> bool:
    text = text.strip()
    if len(text) < 80:
        return False
    if _looks_like_email_dump(text):
        return False
    if _NUMBERED_AFFIL_RE.match(text):
        return False
    if _ABSTRACT_PREFIX_RE.match(text):
        return True
    if _looks_like_author_paragraph(text):
        return False
    return True


def _parse_author_names(text: str) -> list[tuple[str, str]]:
    """Return [(display_name, affiliation)] from a MinerU author line."""
    text = _clean_author_noise(text)
    if not text:
        return []
    if _EMAIL_LINE_RE.search(text):
        letters = re.sub(r"[^A-Za-z]+", "", _EMAIL_LINE_RE.sub("", text))
        if len(letters) < 8:
            return []

    # Multi-author "A, B, C" / Nature "A, B, C & D" first — avoid treating the
    # whole line as one "Name, affiliation" when there are many name segments.
    if text.count(",") >= 2:
        names = []
        for part in re.split(r",|\sand\s", text):
            part = part.strip().strip(".")
            words = part.split()
            if not (1 <= len(words) <= 5 and re.match(r"^[A-ZÀ-ÖØ-Þ]", part)):
                continue
            if _AFFIL_HINT_RE.search(part) and len(words) > 3:
                continue
            names.append((part, ""))
        if len(names) >= 2:
            return names

    # Single "NAME, Affiliation, Country"
    if text.count(",") >= 1 and len(text) < 180 and text.count(",") <= 4:
        parts = [p.strip() for p in text.split(",")]
        name = parts[0]
        name_words = name.split()
        if 1 < len(name_words) <= 6 and not _AFFIL_HINT_RE.search(name):
            aff = ", ".join(parts[1:]).strip()
            return [(name.title() if name.isupper() else name, aff)]

    return []


def _promote_frontmatter(soup: BeautifulSoup, body: Tag) -> None:
    """Wrap MinerU author lines + abstract into ltx_authors / ltx_abstract."""
    if body.select_one(".ltx_authors") and body.select_one(".ltx_abstract"):
        return

    title = body.find(["h1", "h2", "h3"])
    if title is None:
        return

    # Gather front-matter nodes: paragraphs (+ optional Abstract heading)
    # until the first real section heading.
    front: list[Tag] = []
    node = title.next_sibling
    while node is not None:
        nxt = node.next_sibling
        if isinstance(node, Tag) and node.name in ("h1", "h2", "h3", "h4"):
            heading_text = node.get_text(" ", strip=True)
            if _ABSTRACT_HEADING_RE.match(heading_text):
                front.append(node)
                node = nxt
                continue
            if _SECTION_HEADING_RE.match(heading_text) or re.match(
                r"^\d+(\.\d+)*\s+\S", heading_text
            ):
                break
            # Unknown heading — stop to avoid swallowing body
            break
        if isinstance(node, Tag) and node.name == "p":
            front.append(node)
        node = nxt

    if not front:
        return

    author_nodes: list[Tag] = []
    abstract_nodes: list[Tag] = []
    mode = "authors"
    for el in front:
        if el.name in ("h1", "h2", "h3", "h4") and _ABSTRACT_HEADING_RE.match(
            el.get_text(" ", strip=True)
        ):
            mode = "abstract"
            abstract_nodes.append(el)
            continue
        text = el.get_text(" ", strip=True)
        if mode == "authors":
            if _ABSTRACT_PREFIX_RE.match(text):
                mode = "abstract"
                abstract_nodes.append(el)
            elif _DOI_OR_META_LINE_RE.match(text):
                # Nature DOI / Received / Published lines before authors
                continue
            elif _looks_like_email_dump(text):
                # Drop email rows; stay in author mode for following Abstract—
                continue
            elif _NUMBERED_AFFIL_RE.match(text):
                # Nature numbered affiliation block — skip, stay in authors/abstract gate
                continue
            elif _looks_like_author_paragraph(text):
                author_nodes.append(el)
            elif _looks_like_abstract_paragraph(text):
                mode = "abstract"
                abstract_nodes.append(el)
            else:
                # Short non-author preamble (dates, venue) before authors — skip
                if not author_nodes and len(text) < 160:
                    continue
                # Short non-author line after authors → still front matter; skip
                if author_nodes and len(text) < 80:
                    continue
                mode = "abstract"
                if _looks_like_abstract_paragraph(text):
                    abstract_nodes.append(el)
        else:
            if (
                _looks_like_email_dump(text)
                or _NUMBERED_AFFIL_RE.match(text)
                or _DOI_OR_META_LINE_RE.match(text)
            ):
                continue
            if _looks_like_abstract_paragraph(text) or _ABSTRACT_PREFIX_RE.match(text):
                abstract_nodes.append(el)

    # Build authors block
    if not body.select_one(".ltx_authors") and author_nodes:
        authors_wrap = soup.new_tag("div")
        # Name-only lists (Nature / dense PDF author lines) → inline comma list
        authors_wrap["class"] = ["ltx_authors", "ltx_authors_inline"]
        seen: set[str] = set()
        creators: list[Tag] = []
        for el in author_nodes:
            for name, aff in _parse_author_names(el.get_text(" ", strip=True)):
                key = name.lower()
                if key in seen or len(name) < 3:
                    continue
                seen.add(key)
                creator = soup.new_tag("span")
                creator["class"] = ["ltx_creator", "ltx_role_author"]
                person = soup.new_tag("span")
                person["class"] = ["ltx_personname"]
                person.string = name
                creator.append(person)
                if aff:
                    aff_el = soup.new_tag("span")
                    aff_el["class"] = ["ltx_author_notes", "ltx_role_affiliation"]
                    aff_el.string = aff
                    creator.append(NavigableString(" "))
                    creator.append(aff_el)
                    # Affiliations on creators → use stacked layout instead
                    authors_wrap["class"] = ["ltx_authors"]
                creators.append(creator)
        for i, creator in enumerate(creators):
            authors_wrap.append(creator)
            if "ltx_authors_inline" in (authors_wrap.get("class") or []) and i < len(creators) - 1:
                authors_wrap.append(NavigableString(", "))
        if authors_wrap.find("span", class_="ltx_creator"):
            # Insert after title; remove raw author paragraphs
            title.insert_after(authors_wrap)
            for el in author_nodes:
                el.decompose()

    # Build abstract block
    if not body.select_one(".ltx_abstract") and abstract_nodes:
        abstract_div = soup.new_tag("div")
        abstract_div["class"] = ["ltx_abstract"]
        heading = soup.new_tag("h6")
        heading["class"] = ["ltx_title_abstract"]
        heading.string = "Abstract"
        abstract_div.append(heading)
        for el in abstract_nodes:
            if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                el.decompose()
                continue
            text = el.get_text(" ", strip=True)
            text = _ABSTRACT_PREFIX_RE.sub("", text).strip()
            if not text:
                el.decompose()
                continue
            p = soup.new_tag("p")
            p["class"] = ["ltx_p"]
            p.string = text
            abstract_div.append(p)
            el.decompose()
        if abstract_div.find("p"):
            # Place after authors if present, else after title
            authors_el = body.select_one(".ltx_authors")
            if authors_el is not None:
                authors_el.insert_after(abstract_div)
            else:
                title.insert_after(abstract_div)


def enrich_mineru_html(soup: BeautifulSoup, body: Tag) -> None:
    """Post-process PDF-converter HTML into LaTeXML-compatible structure.

    Used for MinerU and Docling (and any future PDF backend) so Nature-style
    front matter / glued citations / numbered bibliographies all land in the
    shapes restyle() already understands.
    """
    _repair_text_nodes(body)
    _unwrap_cite_superscripts(body)
    _strip_nature_reporting_summary(body)
    _promote_frontmatter(soup, body)
    _wrap_orphan_images(soup, body)
    _wrap_tables(soup, body)
    _ensure_figure_table_ids(body)
    _build_bibliography(soup, body)
    _normalize_crossrefs(soup, body)
    _rewrite_citations(soup, body)
    _link_figure_table_mentions(soup, body)


def _unwrap_cite_superscripts(body: Tag) -> None:
    """Flatten <sup>1–4</sup> so glued-cite regex can see the numbers."""
    for sup in list(body.find_all("sup")):
        text = sup.get_text("", strip=True)
        if re.fullmatch(r"\d{1,3}(?:\s*[,，–—−-]\s*\d{1,3})*", text):
            sup.replace_with(NavigableString(text))


def _strip_nature_reporting_summary(body: Tag) -> None:
    """Drop Nature's appended 'Reporting Summary' checklist when present."""
    for h in list(body.find_all(["h1", "h2", "h3", "h4"])):
        title = h.get_text(" ", strip=True)
        if not re.match(r"^(?:nature\s+research\s*\|\s*)?reporting\s+summary\b", title, re.I):
            continue
        # Remove this heading and everything after it (form content, not article).
        node = h
        while node is not None:
            nxt = node.next_sibling
            if isinstance(node, Tag):
                node.decompose()
            elif isinstance(node, NavigableString):
                node.extract()
            node = nxt
        break


def _repair_text_nodes(root: Tag) -> None:
    """Apply Unicode artifact repair to every text node under root."""
    for text_node in list(root.find_all(string=True)):
        if not isinstance(text_node, NavigableString):
            continue
        if text_node.find_parent(["pre", "code", "script", "style", "math"]):
            continue
        raw = str(text_node)
        fixed = repair_unicode_artifacts(raw)
        if fixed != raw:
            text_node.replace_with(fixed)
