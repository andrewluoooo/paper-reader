"""
Convert an EPUB ebook into the same ``ltx_*``-classed HTML structure
LaTeXML / html_convert / pdf_convert produce, so it flows through
``restyle()`` unchanged (theming, outline, highlighting, image inlining).

An EPUB is a ZIP of XHTML chapters plus a package document (OPF) that
lists reading order and Dublin Core metadata. No new dependencies --
stdlib ``zipfile`` plus BeautifulSoup, matching the HTML-import path.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from .html_convert import _frontmatter_note, _normalize_authors

class EpubConvertError(RuntimeError):
    pass


_JUNK_SELECTORS = [
    "script", "style", "noscript", "nav", "form", "iframe",
    "[epub\\:type=pagebreak]", "[role=doc-pagebreak]",
]

_REF_CHAPTER_RE = re.compile(
    r"^(references?|bibliograph(y|ies)|works cited|notes|endnotes|further reading)$",
    re.I,
)


def _soup_xml(text: str) -> BeautifulSoup:
    return BeautifulSoup(text, "xml")


def _soup_html(text: str) -> BeautifulSoup:
    # lxml HTML mode tolerates XHTML namespaces better than the XML parser
    # for chapter bodies (which mix HTML + optional SVG/MathML).
    return BeautifulSoup(text, "lxml")


def _local_name(tag: Tag) -> str:
    name = tag.name or ""
    return name.split(":")[-1].lower()


def _attr(el: Tag, *keys: str) -> str:
    for key in keys:
        if el.has_attr(key) and el[key]:
            return str(el[key]).strip()
        # namespaced attrs show up as "{ns}key" or "prefix:key" depending on parser
        for attr_key, val in el.attrs.items():
            if str(attr_key).endswith("}" + key) or str(attr_key).endswith(":" + key):
                if val:
                    return str(val).strip() if not isinstance(val, list) else " ".join(val)
    return ""


def _extract_epub(epub_path: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(epub_path) as zf:
            # Zip-slip guard: only extract paths that stay under dest.
            for info in zf.infolist():
                target = (dest / info.filename).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise EpubConvertError(f"refusing unsafe path in EPUB: {info.filename}")
            zf.extractall(dest)
    except zipfile.BadZipFile as e:
        raise EpubConvertError(f"not a valid EPUB (zip) file: {e}") from e
    return dest


def _opf_path(root: Path) -> Path:
    container = root / "META-INF" / "container.xml"
    if not container.is_file():
        # Some malformed EPUBs put the OPF at the root -- last-ditch search.
        candidates = list(root.rglob("*.opf"))
        if len(candidates) == 1:
            return candidates[0]
        raise EpubConvertError("EPUB is missing META-INF/container.xml")
    soup = _soup_xml(container.read_text(encoding="utf-8", errors="ignore"))
    rootfile = None
    for el in soup.find_all(True):
        if _local_name(el) == "rootfile" and _attr(el, "full-path", "fullpath"):
            rootfile = el
            break
    if rootfile is None:
        raise EpubConvertError("container.xml has no rootfile entry")
    rel = unquote(_attr(rootfile, "full-path", "fullpath"))
    opf = (root / rel).resolve()
    if not opf.is_file():
        raise EpubConvertError(f"OPF package document not found: {rel}")
    return opf


def _parse_opf(opf_path: Path) -> tuple[dict, list[Path]]:
    """Return (metadata, ordered chapter paths)."""
    soup = _soup_xml(opf_path.read_text(encoding="utf-8", errors="ignore"))
    opf_dir = opf_path.parent

    meta: dict = {"title": "", "authors": [], "date": "", "identifier": "", "publisher": ""}
    for el in soup.find_all(True):
        name = _local_name(el)
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if name == "title" and not meta["title"]:
            meta["title"] = text
        elif name == "creator":
            meta["authors"].append(text)
        elif name == "date" and not meta["date"]:
            meta["date"] = text[:4] if re.match(r"^\d{4}", text) else text
        elif name == "identifier" and not meta["identifier"]:
            meta["identifier"] = text
        elif name == "publisher" and not meta["publisher"]:
            meta["publisher"] = text

    # Manifest: id → href (and media-type)
    manifest: dict[str, tuple[str, str]] = {}
    for item in soup.find_all(True):
        if _local_name(item) != "item":
            continue
        item_id = _attr(item, "id")
        href = _attr(item, "href")
        media = _attr(item, "media-type", "media-type")
        if item_id and href:
            manifest[item_id] = (unquote(href), media.lower())

    # Spine reading order
    chapters: list[Path] = []
    spine = None
    for el in soup.find_all(True):
        if _local_name(el) == "spine":
            spine = el
            break
    if spine is None:
        raise EpubConvertError("OPF has no spine (reading order)")

    for itemref in spine.find_all(True, recursive=False):
        if _local_name(itemref) != "itemref":
            continue
        idref = _attr(itemref, "idref")
        if not idref or idref not in manifest:
            continue
        href, media = manifest[idref]
        if media and ("html" not in media and "xml" not in media and media != "application/xhtml+xml"):
            # Skip non-document spine items (e.g. images mistakenly listed).
            if not href.lower().endswith((".xhtml", ".html", ".htm", ".xml")):
                continue
        chapter = (opf_dir / href).resolve()
        if chapter.is_file():
            chapters.append(chapter)

    if not chapters:
        raise EpubConvertError("EPUB spine lists no readable XHTML chapters")

    if not meta["title"]:
        meta["title"] = opf_path.stem
    return meta, chapters


def _strip_junk(el: Tag) -> None:
    for sel in _JUNK_SELECTORS:
        try:
            for junk in el.select(sel):
                junk.decompose()
        except Exception:
            continue
    # Drop empty headings / page-break styled elements
    for tag in list(el.find_all(True)):
        classes = " ".join(tag.get("class") or []).lower()
        if "pagebreak" in classes or "page-break" in classes:
            tag.decompose()


def _resolve_resources(body: Tag, chapter_path: Path) -> None:
    """Rewrite local <img src> (and SVG <image href>) to absolute paths so
    restyle()'s _inline_images can find them regardless of where paper.html
    is written -- same approach as html_convert._resolve_image_paths."""
    for img in body.find_all("img"):
        src = img.get("src") or ""
        if not src or src.startswith("data:") or src.startswith("http"):
            continue
        rel = unquote(urlparse(src).path)
        src_path = (chapter_path.parent / rel).resolve()
        if not src_path.is_file():
            img.decompose()
            continue
        img["src"] = str(src_path)

    for image in body.find_all(["image", "svg:image"]):
        href = (
            image.get("href")
            or image.get("{http://www.w3.org/1999/xlink}href")
            or ""
        )
        if not href or href.startswith("data:") or href.startswith("http"):
            continue
        rel = unquote(urlparse(href).path)
        src_path = (chapter_path.parent / rel).resolve()
        if src_path.is_file():
            image["href"] = str(src_path)


def _heading_class(tag_name: str) -> Optional[str]:
    return {
        "h1": "ltx_title_section",
        "h2": "ltx_title_subsection",
        "h3": "ltx_title_subsubsection",
        "h4": "ltx_title_subsubsection",
        "h5": "ltx_title_subsubsection",
        "h6": "ltx_title_subsubsection",
    }.get(tag_name)


def _normalize_chapter_body(body: Tag, doc_title: str) -> None:
    """Tag chapter body elements with the ltx_* classes restyle() expects."""
    _strip_junk(body)

    for p in body.find_all("p"):
        classes = p.get("class") or []
        if "ltx_p" not in classes:
            p["class"] = list(classes) + ["ltx_p"]

    # Leaf divs with substantial text → paragraphs (same heuristic as html_convert)
    block_descendants = ("p", "div", "table", "figure", "ul", "ol", "section",
                         "h1", "h2", "h3", "h4", "h5", "h6", "blockquote")
    for div in body.find_all("div"):
        if div.find(block_descendants) is not None:
            continue
        text = div.get_text(" ", strip=True)
        if len(text) < 40:
            continue
        classes = div.get("class") or []
        if "ltx_p" not in classes:
            div["class"] = list(classes) + ["ltx_p"]

    for tag_name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        cls = _heading_class(tag_name)
        for h in body.find_all(tag_name):
            text = h.get_text(" ", strip=True)
            # Drop a leading chapter title that just repeats the book title
            if tag_name == "h1" and doc_title and text.lower() == doc_title.lower():
                h.decompose()
                continue
            classes = h.get("class") or []
            if cls and cls not in classes:
                h["class"] = list(classes) + [cls]

    for fig in body.find_all("figure"):
        is_table = fig.find("table") is not None
        classes = fig.get("class") or []
        role = "ltx_table" if is_table else "ltx_figure"
        if role not in classes:
            fig["class"] = list(classes) + [role]
        cap = fig.find("figcaption")
        if cap:
            cap_classes = cap.get("class") or []
            if "ltx_caption" not in cap_classes:
                cap["class"] = list(cap_classes) + ["ltx_caption"]

    # Images not already in a figure → wrap so restyle treats them as figures
    doc = body
    while doc.parent is not None:
        doc = doc.parent
    if isinstance(doc, BeautifulSoup):
        for img in list(body.find_all("img")):
            if img.find_parent("figure") is not None:
                continue
            wrapper = doc.new_tag("figure")
            wrapper["class"] = ["ltx_figure"]
            img.replace_with(wrapper)
            wrapper.append(img)

    for bq in body.find_all("blockquote"):
        classes = bq.get("class") or []
        if "ltx_blockquote" not in classes:
            bq["class"] = list(classes) + ["ltx_blockquote"]
        for p in bq.find_all("p"):
            p_classes = p.get("class") or []
            if "ltx_p" not in p_classes:
                p["class"] = list(p_classes) + ["ltx_p"]


def _chapter_looks_like_references(body: Tag) -> bool:
    for h in body.find_all(["h1", "h2", "h3"]):
        if _REF_CHAPTER_RE.match(h.get_text(" ", strip=True)):
            return True
    return False


def _normalize_references_chapter(out_soup: BeautifulSoup, body: Tag) -> Optional[Tag]:
    """Turn a references-looking chapter into an ltx_bibliography section."""
    entries: list[str] = []
    for el in body.find_all(["li", "p"]):
        text = el.get_text(" ", strip=True)
        if text and len(text) >= 20:
            entries.append(text)
    if len(entries) < 3:
        return None

    section = out_soup.new_tag("section")
    section["class"] = ["ltx_bibliography"]
    heading = out_soup.new_tag("h2")
    heading["class"] = ["ltx_title_bibliography"]
    heading.string = "References"
    section.append(heading)
    ol = out_soup.new_tag("ul")
    ol["class"] = ["ltx_biblist"]
    for i, text in enumerate(entries, start=1):
        li = out_soup.new_tag("li")
        li["class"] = ["ltx_bibitem"]
        li["id"] = f"bib.epub{i}"
        block = out_soup.new_tag("span")
        block["class"] = ["ltx_bibblock"]
        block.string = text
        li.append(block)
        ol.append(li)
    section.append(ol)
    return section


def _get_body(chapter_soup: BeautifulSoup) -> Optional[Tag]:
    body = chapter_soup.body
    if body is not None:
        return body
    # XML parse sometimes leaves content under <html> without a <body>
    html = chapter_soup.find("html") or chapter_soup.find(re.compile(r"html$", re.I))
    if html is not None:
        return html
    return chapter_soup


def convert(input_path: str, workdir: str) -> str:
    """Convert an EPUB into the ltx_*-classed structure restyle() expects.
    Returns the path to the generated HTML file (same contract as the
    other converters)."""
    src_path = Path(input_path).resolve()
    if not src_path.is_file():
        raise EpubConvertError(f"no such file: {input_path}")
    if not src_path.name.lower().endswith(".epub"):
        raise EpubConvertError(f"not an .epub file: {input_path}")

    workdir_p = Path(workdir)
    workdir_p.mkdir(parents=True, exist_ok=True)
    extract_dir = workdir_p / "epub_src"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    _extract_epub(src_path, extract_dir)

    opf = _opf_path(extract_dir)
    meta, chapters = _parse_opf(opf)

    out_soup = BeautifulSoup(
        '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>',
        "lxml",
    )
    article = out_soup.new_tag("article")
    article["class"] = ["ltx_document"]
    out_soup.body.append(article)

    h1 = out_soup.new_tag("h1")
    h1["class"] = ["ltx_title_document"]
    h1.string = meta["title"]
    article.append(h1)

    if meta["authors"]:
        article.append(_normalize_authors(out_soup, meta["authors"]))
    if meta["publisher"]:
        article.append(_frontmatter_note(out_soup, "conference", meta["publisher"]))
    if meta["date"]:
        article.append(_frontmatter_note(out_soup, "journalyear", meta["date"]))
    if meta["identifier"]:
        # ISBN / UUID / DOI -- surface as a frontmatter note when it looks useful
        ident = meta["identifier"]
        role = "doi" if re.match(r"^10\.\d{4,9}/", ident) else "conference"
        if role == "doi" or ident.lower().startswith("isbn"):
            article.append(_frontmatter_note(out_soup, role if role == "doi" else "conference", ident))

    bib_section: Optional[Tag] = None
    appended_any = False

    for chapter_path in chapters:
        try:
            raw = chapter_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        chapter_soup = _soup_html(raw)
        body = _get_body(chapter_soup)
        if body is None:
            continue

        # Work on a detached clone of the body's children so we don't keep
        # the chapter's <html>/<head> wrappers.
        fragment = out_soup.new_tag("div")
        for child in list(body.children):
            if isinstance(child, NavigableString) and not str(child).strip():
                continue
            fragment.append(child.extract() if hasattr(child, "extract") else child)

        if not fragment.get_text(" ", strip=True):
            continue

        _resolve_resources(fragment, chapter_path)
        _normalize_chapter_body(fragment, meta["title"])

        if _chapter_looks_like_references(fragment) and bib_section is None:
            maybe_bib = _normalize_references_chapter(out_soup, fragment)
            if maybe_bib is not None:
                bib_section = maybe_bib
                continue

        for child in list(fragment.children):
            article.append(child)
            appended_any = True

    if bib_section is not None:
        article.append(bib_section)

    if not appended_any:
        raise EpubConvertError(
            "couldn't extract any readable chapter content from this EPUB"
        )

    out_path = workdir_p / "paper.html"
    out_path.write_text(str(out_soup), encoding="utf-8")
    return str(out_path)
