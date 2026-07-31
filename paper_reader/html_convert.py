"""
Convert an already-rendered HTML research paper (saved from a publisher's
page -- ACM DL, IEEE Xplore, Springer, and similar sites all work this
way) into the same `ltx_*`-classed structure LaTeXML produces, so it
flows through restyle()'s existing pipeline unchanged: same theming,
highlighting, table/figure fit-to-width, outline, citation hover
previews, everything.

Deliberately takes a LOCAL HTML file the user already saved from their
own browser (Save Page As... "Webpage, Complete") rather than fetching
the URL itself. Many publisher sites, ACM DL included, sit behind a
bot-detection challenge (Cloudflare et al.) -- respecting that instead of
trying to script around it is the right default, and it also means
whatever institutional/personal access the user's own browser session
has already applies, same as reading the page normally would.

"As flexible as possible" here means not hard-coding one publisher's
markup. Two extraction strategies are tried in order:
  1. Landmark IDs/classes that JATS-derived academic publishing platforms
     tend to share (`#bodymatter`, `#bibliography`, `#abstract`, and
     `id="sec-N"`-style section numbering) -- covers ACM, and likely a
     good chunk of IEEE/Springer/Wiley-style pages built on similar
     production pipelines.
  2. readability-lxml's general "find the main content" heuristic, for
     everything else.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup, Tag

try:
    from readability import Document as _ReadabilityDocument
except ImportError:  # pragma: no cover - optional dependency guard
    _ReadabilityDocument = None


class HtmlConvertError(RuntimeError):
    pass


_SECTION_ID_RE = re.compile(r"^s(ec)?-?\d")

_BODY_SELECTORS = ["#bodymatter", ".bodymatter", "#body-content", "article[data-type]"]
_ABSTRACT_SELECTORS = ["#abstract", ".abstract", "#abstracts .abstract"]
_REFS_SELECTORS = ["#bibliography", ".bibliography", "#references"]
_AUTHOR_SELECTORS = [".core-authors", ".authors", "[class*=author-list]", "[class*=contrib-group]"]

_JUNK_SELECTORS = [
    "script", "style", "noscript", "nav", "form", "iframe",
    "[class*=advertis]", "[class*=cookie]", "[class*=subscribe]", "[class*=paywall]",
    "[class*=related-article]", "[class*=recommend]", "[class*=comment]", "[class*=share]",
    "[class*=social]", "[id*=modal]", "[class*=modal]",
    "[role=navigation]", "[role=banner]", "[role=contentinfo]",
]


def _select_first(soup: BeautifulSoup, selectors: list[str]) -> Optional[Tag]:
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            return el
    return None


def _find_body_by_section_landmarks(soup: BeautifulSoup) -> Optional[Tag]:
    """Fallback for the `#bodymatter`-style selectors: find every element
    whose id looks like a JATS-style section number ("sec-1", "sec-2-1",
    ...) and walk up to their common ancestor."""
    secs = [el for el in soup.find_all(id=True) if _SECTION_ID_RE.match(el["id"])]
    if len(secs) < 3:
        return None
    candidate = secs[0]
    while candidate is not None:
        if all(s is candidate or s in candidate.descendants for s in secs):
            return candidate
        candidate = candidate.parent
    return None


def _find_body(soup: BeautifulSoup) -> Optional[Tag]:
    el = _select_first(soup, _BODY_SELECTORS)
    if el is not None and len(el.get_text(strip=True)) > 500:
        return el
    el = _find_body_by_section_landmarks(soup)
    if el is not None and len(el.get_text(strip=True)) > 500:
        return el
    if _ReadabilityDocument is not None:
        try:
            summary_html = _ReadabilityDocument(str(soup)).summary()
        except Exception:
            summary_html = ""
        if summary_html and len(BeautifulSoup(summary_html, "lxml").get_text(strip=True)) > 500:
            return BeautifulSoup(summary_html, "lxml")
    return None


def _strip_junk(el: Tag) -> None:
    for sel in _JUNK_SELECTORS:
        for junk in el.select(sel):
            junk.decompose()


def _extract_title(soup: BeautifulSoup) -> str:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].split(" | ")[0].strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True).split(" | ")[0].strip()
    return "Untitled"


def _extract_venue(soup: BeautifulSoup) -> str:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content") and " | " in og["content"]:
        return og["content"].split(" | ", 1)[1].strip()
    site_name = soup.find("meta", attrs={"property": "og:site_name"})
    return site_name["content"].strip() if site_name and site_name.get("content") else ""


def _extract_doi(soup: BeautifulSoup) -> str:
    for meta in soup.find_all("meta", attrs={"name": re.compile("^(dc\\.identifier|citation_doi)$", re.I)}):
        content = meta.get("content", "")
        scheme = meta.get("scheme", "")
        if "doi" in scheme.lower() or re.match(r"^10\.\d{4,9}/", content):
            return content.strip()
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        m = re.search(r"(10\.\d{4,9}/\S+)", canonical["href"])
        if m:
            return m.group(1)
    return ""


def _extract_authors(soup: BeautifulSoup) -> list[str]:
    for citation_meta in soup.find_all("meta", attrs={"name": "citation_author"}):
        pass
    names = [m["content"].strip() for m in soup.find_all("meta", attrs={"name": "citation_author"}) if m.get("content")]
    if names:
        return names
    el = _select_first(soup, _AUTHOR_SELECTORS)
    if el:
        text = el.get_text(" ", strip=True)
        parts = re.split(r",| and |;", text)
        names = [p.strip() for p in parts if p.strip() and len(p.strip()) < 60]
        if names:
            return names
    return []


def _make_soup_fragment(soup: BeautifulSoup, name: str, **attrs) -> Tag:
    return soup.new_tag(name, **attrs)


def _normalize_body(soup: BeautifulSoup, body: Tag) -> None:
    """Mutates `body` in place, tagging elements with the ltx_* classes
    restyle() already knows how to style -- reusing its whole pipeline
    (theming, TOC, table/figure fit-to-width, highlighting) instead of
    duplicating it."""
    _strip_junk(body)

    for p in body.find_all("p"):
        p["class"] = (p.get("class") or []) + ["ltx_p"]

    # Not every site uses semantic <p> for paragraph text -- some
    # (ACM DL among them) use plain <div>s instead. Treat any "leaf" div
    # (no other block-level element inside it -- just text and inline
    # markup like links/emphasis/math) with a meaningful amount of text
    # as a paragraph too.
    _BLOCK_DESCENDANTS = ("p", "div", "table", "figure", "ul", "ol", "section", "h1", "h2", "h3", "h4", "h5", "h6")
    for div in body.find_all("div"):
        if div.find(_BLOCK_DESCENDANTS) is not None:
            continue
        text = div.get_text(" ", strip=True)
        if len(text) < 30:
            continue
        div["class"] = (div.get("class") or []) + ["ltx_p"]

    heading_classes = {"h2": "ltx_title_section", "h3": "ltx_title_subsection", "h4": "ltx_title_subsubsection"}
    for tag_name, cls in heading_classes.items():
        for h in body.find_all(tag_name):
            h["class"] = (h.get("class") or []) + [cls]

    for fig in body.find_all("figure"):
        is_table = fig.find("table") is not None
        fig["class"] = (fig.get("class") or []) + (["ltx_table"] if is_table else ["ltx_figure"])
        cap = fig.find("figcaption")
        if cap:
            cap["class"] = (cap.get("class") or []) + ["ltx_caption"]

    # In-text citations on many academic-publishing platforms are plain
    # `<a href="...#SomeId">` links pointing straight at a bibliography
    # entry with `id="SomeId"` elsewhere on the page -- either a bare
    # "#SomeId" fragment, or (as browsers sometimes leave same-page
    # anchors when saving a page) the full absolute page URL with the
    # fragment tacked on the end. If we can find that target and it looks
    # like a bibliography entry, wrap the link in the same
    # <cite class="ltx_cite"> shape LaTeXML output uses -- that's what
    # makes the hover preview and highlighting-safe atomic wrapping apply
    # to it, without needing to know this particular site's markup beyond
    # "a link that resolves to a same-page target".
    for a in body.find_all("a", href=True):
        href = a["href"]
        if "#" not in href:
            continue
        target_id = href.rsplit("#", 1)[1]
        if not target_id:
            continue
        target = soup.find(id=target_id)
        if target is None or a.find_parent("cite"):
            continue
        target_classes = " ".join(target.get("class") or [])
        if "bib" not in target_id.lower() and "bib" not in target_classes.lower() and "ref" not in target_classes.lower():
            continue
        # The reader's citation hover-preview only recognizes LaTeXML's
        # own "#bib.<id>" convention (see initRefPreviews() in
        # restyle.py), so rewrite the link to match it -- reusing that
        # existing, unmodified JS instead of teaching it a second scheme.
        a["href"] = f"#bib.{target_id}"
        a["class"] = (a.get("class") or []) + ["ltx_ref"]
        cite = soup.new_tag("cite")
        cite["class"] = ["ltx_cite", "ltx_citemacro_cite"]
        a.wrap(cite)


def _normalize_bibliography(soup: BeautifulSoup, refs: Optional[Tag]) -> Optional[Tag]:
    if refs is None:
        return None
    _strip_junk(refs)
    section = soup.new_tag("section")
    section["class"] = ["ltx_bibliography"]
    heading = soup.new_tag("h2")
    heading["class"] = ["ltx_title_bibliography"]
    heading.string = "References"
    section.append(heading)

    ol = soup.new_tag("ul")
    ol["class"] = ["ltx_biblist"]
    # Individual entries are usually list items or divs with an id that
    # in-text citation links (rewritten above) point back at -- reuse
    # whichever ids are already there so those links keep resolving.
    entries = refs.find_all(["li", "div"], id=True, recursive=True)
    if not entries:
        entries = refs.find_all(["li", "p"], recursive=True)
    seen_ids = set()
    for entry in entries:
        text = entry.get_text(" ", strip=True)
        if not text or len(text) < 15:
            continue
        entry_id = entry.get("id")
        if entry_id and entry_id in seen_ids:
            continue
        if entry_id:
            seen_ids.add(entry_id)
        li = soup.new_tag("li")
        li["class"] = ["ltx_bibitem"]
        if entry_id:
            li["id"] = f"bib.{entry_id}"  # matches the "#bib.<id>" rewrite in _normalize_body
        content = soup.new_tag("span")
        content["class"] = ["ltx_bibblock"]
        content.string = text
        li.append(content)
        ol.append(li)
    if not ol.find_all("li"):
        return None
    section.append(ol)
    return section


def _normalize_authors(soup: BeautifulSoup, names: list[str]) -> Tag:
    wrap = soup.new_tag("div")
    wrap["class"] = ["ltx_authors"]
    for name in names:
        creator = soup.new_tag("span")
        creator["class"] = ["ltx_creator", "ltx_role_author"]
        person = soup.new_tag("span")
        person["class"] = ["ltx_personname"]
        person.string = name
        creator.append(person)
        wrap.append(creator)
    return wrap


def _frontmatter_note(soup: BeautifulSoup, role: str, text: str) -> Tag:
    note = soup.new_tag("div")
    note["class"] = ["ltx_note", "ltx_note_frontmatter", f"ltx_role_{role}"]
    content = soup.new_tag("div")
    content["class"] = ["ltx_note_content"]
    content.string = text
    note.append(content)
    return note


def _resolve_image_paths(body: Tag, source_dir: Path) -> None:
    """Rewrite every non-remote <img src> to an absolute local path.
    restyle()'s _inline_images() joins whatever's here onto its own
    workdir-based base_dir with os.path.join, which discards that base
    entirely once the path is already absolute -- so this survives
    regardless of where the normalized HTML ends up being written."""
    for img in body.find_all("img"):
        src = img.get("src", "")
        if not src or src.startswith("data:") or src.startswith("http"):
            continue
        resolved = (source_dir / src).resolve()
        img["src"] = str(resolved)


def convert(input_path: str, workdir: str) -> str:
    """Convert a saved HTML paper page into the ltx_*-classed structure
    restyle() expects. Returns the path to the generated HTML file, same
    contract as latex_convert.convert()."""
    src_path = Path(input_path).resolve()
    if not src_path.is_file():
        raise HtmlConvertError(f"no such file: {input_path}")

    with open(src_path, "r", encoding="utf-8", errors="ignore") as f:
        raw_html = f.read()
    soup = BeautifulSoup(raw_html, "lxml")

    body = _find_body(soup)
    if body is None:
        raise HtmlConvertError(
            "couldn't find the article body in this HTML file -- it may not be a "
            "saved paper page, or its layout isn't one this parser recognizes yet"
        )

    title = _extract_title(soup)
    authors = _extract_authors(soup)
    venue = _extract_venue(soup)
    doi = _extract_doi(soup)

    abstract_el = _select_first(soup, _ABSTRACT_SELECTORS)
    refs_el = _select_first(soup, _REFS_SELECTORS)

    _normalize_body(soup, body)
    _resolve_image_paths(body, src_path.parent)

    out_soup = BeautifulSoup(
        '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>', "lxml"
    )
    article = out_soup.new_tag("article")
    article["class"] = ["ltx_document"]
    out_soup.body.append(article)

    h1 = out_soup.new_tag("h1")
    h1["class"] = ["ltx_title_document"]
    h1.string = title
    article.append(h1)

    if authors:
        article.append(_normalize_authors(out_soup, authors))
    if venue:
        article.append(_frontmatter_note(out_soup, "conference", venue))
    if doi:
        article.append(_frontmatter_note(out_soup, "doi", doi))

    if abstract_el is not None:
        _strip_junk(abstract_el)
        abstract_div = out_soup.new_tag("div")
        abstract_div["class"] = ["ltx_abstract"]
        abstract_heading = out_soup.new_tag("h2")
        abstract_heading["class"] = ["ltx_title_abstract"]
        abstract_heading.string = "Abstract"
        abstract_div.append(abstract_heading)
        for p in abstract_el.find_all("p") or [abstract_el]:
            text = p.get_text(" ", strip=True)
            if not text:
                continue
            ap = out_soup.new_tag("p")
            ap["class"] = ["ltx_p"]
            ap.string = text
            abstract_div.append(ap)
        if abstract_div.find("p"):
            article.append(abstract_div)

    # Move the normalized body's children into the new tree (can't just
    # append `body` itself -- it may still be wrapped in unrelated
    # ancestor markup we don't want to drag along).
    for child in list(body.children):
        article.append(child.extract())

    bib_section = _normalize_bibliography(out_soup, refs_el)
    if bib_section is not None:
        article.append(bib_section)

    workdir_p = Path(workdir)
    workdir_p.mkdir(parents=True, exist_ok=True)
    out_path = workdir_p / "paper.html"
    out_path.write_text(str(out_soup), encoding="utf-8")
    return str(out_path)
