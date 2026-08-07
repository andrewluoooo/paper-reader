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
     production pipelines. Also covers Springer Nature's shared
     ``c-article-*`` chrome (Nature, Nat. Commun., etc.).
  2. readability-lxml's general "find the main content" heuristic, for
     everything else.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup, Tag

from .mineru_normalize import _ensure_figure_table_ids, _normalize_crossrefs

try:
    from readability import Document as _ReadabilityDocument
except ImportError:  # pragma: no cover - optional dependency guard
    _ReadabilityDocument = None


class HtmlConvertError(RuntimeError):
    pass


_SECTION_ID_RE = re.compile(r"^s(ec)?-?\d", re.I)
_NATURE_TITLE_SUFFIX_RE = re.compile(
    r"\s*[|\-–—]\s*Nature(?:\s+(?:Communications|Methods|Medicine|Biotechnology|"
    r"Genetics|Neuroscience|Chemistry|Physics|Materials|Protocols|Climate|"
    r"Energy|Food|Water|Aging|Cancer|Electronics|Nanotechnology|"
    r"Photonics|Plants|Structural\s+&\s+Molecular\s+Biology|"
    r"Reviews?(?:\s+\w+)?|Astronomy))?\s*$",
    re.IGNORECASE,
)

_BODY_SELECTORS = [
    "#bodymatter",
    ".bodymatter",
    "#body-content",
    # Springer Nature (Nature, Nat. Commun., …)
    ".c-article-body",
    "article[data-article-body]",
    "article[data-type]",
]
_ABSTRACT_SELECTORS = [
    "#abstract",
    ".abstract",
    "#abstracts .abstract",
    # Nature: Abstract lives in #Abs1-content (and siblings Abs2…)
    "#Abs1-content",
    "#Abs1-section .c-article-section__content",
    "section[data-title='Abstract'] .c-article-section__content",
    "[data-title='Abstract'] .c-article-section__content",
]
_REFS_SELECTORS = [
    "#bibliography",
    ".bibliography",
    "#references",
    # Nature: References in #Bib1-content / ol.c-article-references
    "#Bib1-content",
    "#Bib1-section .c-article-section__content",
    ".c-article-references",
    "ol.c-article-references",
]
_AUTHOR_SELECTORS = [
    ".c-article-author-list",
    ".core-authors",
    ".authors",
    "[class*=author-list]",
    "[class*=contrib-group]",
]

_JUNK_SELECTORS = [
    "script", "style", "noscript", "nav", "form", "iframe",
    "[class*=advertis]", "[class*=cookie]", "[class*=subscribe]", "[class*=paywall]",
    "[class*=related-article]", "[class*=recommend]", "[class*=comment]", "[class*=share]",
    "[class*=social]", "[id*=modal]", "[class*=modal]",
    "[role=navigation]", "[role=banner]", "[role=contentinfo]",
    # Springer Nature chrome / outbound ref tools
    ".c-article-extras",
    ".c-article-associated-content",
    ".c-article-meta-recommendations",
    ".c-article-recommendations",
    ".c-article-metrics-bar",
    ".c-article-references__links",
    ".c-code-block__copy",
    ".js-section-access",
    "[data-track-action='reference link']",
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
        title = og["content"].split(" | ")[0].strip()
    else:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title = h1.get_text(" ", strip=True)
        elif soup.title and soup.title.get_text(strip=True):
            title = soup.title.get_text(strip=True).split(" | ")[0].strip()
        else:
            title = "Untitled"
    # Nature og:title is "Title - Nature" / "Title - Nature Communications"
    title = _NATURE_TITLE_SUFFIX_RE.sub("", title).strip()
    return title or "Untitled"


def _extract_venue(soup: BeautifulSoup) -> str:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content") and " | " in og["content"]:
        return og["content"].split(" | ", 1)[1].strip()
    site_name = soup.find("meta", attrs={"property": "og:site_name"})
    if site_name and site_name.get("content"):
        return site_name["content"].strip()
    # Nature uses "Title - Nature" rather than pipe separators
    og_title = og.get("content") if og else ""
    m = _NATURE_TITLE_SUFFIX_RE.search(og_title or "")
    if m:
        return m.group(0).lstrip(" |-\u2013\u2014").strip()
    return ""


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
    # Springer Nature display names (preferred over citation_author "Last, First")
    nature_names = [
        a.get_text(" ", strip=True)
        for a in soup.select('.c-article-author-list a[data-test="author-name"]')
    ]
    nature_names = [n for n in nature_names if n and "orcid" not in n.lower()]
    if nature_names:
        return nature_names

    names = [
        m["content"].strip()
        for m in soup.find_all("meta", attrs={"name": "citation_author"})
        if m.get("content")
    ]
    if names:
        return names
    el = _select_first(soup, _AUTHOR_SELECTORS)
    if el:
        # Nature author-list items without the data-test hook
        item_names = []
        for li in el.select("li"):
            name_el = li.select_one('[itemprop="name"], a[href*="#auth-"], span[itemprop="name"]')
            if name_el:
                n = name_el.get_text(" ", strip=True)
                if n and "orcid" not in n.lower() and len(n) < 80:
                    item_names.append(n)
        if item_names:
            return item_names
        text = el.get_text(" ", strip=True)
        parts = re.split(r",| and |;", text)
        names = [p.strip() for p in parts if p.strip() and len(p.strip()) < 60]
        if names:
            return names
    return []


def _is_bib_target(target: Tag, target_id: str) -> bool:
    """True if an anchor target looks like a bibliography entry."""
    tid = (target_id or "").lower()
    tcls = " ".join(target.get("class") or []).lower()
    if any(k in tid for k in ("bib", "ref-cr", "ref-")):
        return True
    if any(k in tcls for k in ("bib", "ref", "reference")):
        return True
    # Nature: id="ref-CR12" on a <p class="c-article-references__text">
    if re.match(r"^ref-?cr?\d+", tid) or re.match(r"^cr\d+$", tid):
        return True
    return False


def _make_soup_fragment(soup: BeautifulSoup, name: str, **attrs) -> Tag:
    return soup.new_tag(name, **attrs)


def _normalize_body(soup: BeautifulSoup, body: Tag) -> None:
    """Mutates `body` in place, tagging elements with the ltx_* classes
    restyle() already knows how to style -- reusing its whole pipeline
    (theming, TOC, table/figure fit-to-width, highlighting) instead of
    duplicating it."""
    _strip_junk(body)

    # Nature wraps each section title as h2#SecN / h2#Abs1 — promote classes.
    for h in body.select("h2.c-article-section__title, h3.c-article-section__title"):
        if h.name == "h2":
            h["class"] = (h.get("class") or []) + ["ltx_title_section"]
        else:
            h["class"] = (h.get("class") or []) + ["ltx_title_subsection"]

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
            classes = h.get("class") or []
            if cls not in classes:
                h["class"] = classes + [cls]

    for fig in body.find_all("figure"):
        is_table = fig.find("table") is not None
        fig["class"] = (fig.get("class") or []) + (["ltx_table"] if is_table else ["ltx_figure"])
        cap = fig.find("figcaption")
        if cap:
            cap["class"] = (cap.get("class") or []) + ["ltx_caption"]

    _ensure_figure_table_ids(body)
    _normalize_crossrefs(soup, body)

    # In-text citations on many academic-publishing platforms are plain
    # `<a href="...#SomeId">` links pointing straight at a bibliography
    # entry with `id="SomeId"` elsewhere on the page -- either a bare
    # "#SomeId" fragment, or (as browsers sometimes leave same-page
    # anchors when saving a page) the full absolute page URL with the
    # fragment tacked on the end. Nature uses <sup><a href="#ref-CR1">1</a></sup>.
    # Publisher-specific Nature #FigN / #ref-CRN rewrites are handled above;
    # this pass covers remaining generic publisher cite schemes.
    for a in body.find_all("a", href=True):
        href = a["href"]
        if "#" not in href:
            continue
        if a.find_parent("cite"):
            continue
        # Already normalized to reader-local targets
        if re.match(r"#(?:bib|fig|tab)\.\d+", href):
            continue
        target_id = href.rsplit("#", 1)[1]
        if not target_id:
            continue
        target = soup.find(id=target_id) or soup.find(id=f"bib.{target_id}")
        if target is None:
            continue
        if not _is_bib_target(target, target_id):
            continue
        # Prefer numeric bib.N when the id is Nature-style ref-CRN
        m = re.search(r"(\d+)$", str(target.get("id") or target_id))
        bib_id = f"bib.{m.group(1)}" if m else f"bib.{target_id}"
        if bib_id.startswith("bib.bib."):
            bib_id = bib_id[4:]
        a["href"] = f"#{bib_id}"
        a["class"] = (a.get("class") or []) + ["ltx_ref"]
        cite = soup.new_tag("cite")
        cite["class"] = ["ltx_cite", "ltx_citemacro_cite"]
        parent_sup = a.parent if a.parent and a.parent.name == "sup" else None
        if parent_sup is not None and parent_sup.parent is not None:
            parent_sup.wrap(cite)
        else:
            a.wrap(cite)


def _entry_id(entry: Tag) -> Optional[str]:
    """Bibliography entry id — Nature puts it on an inner <p>, not the <li>."""
    if entry.get("id"):
        return entry["id"]
    inner = entry.find(id=True)
    return inner["id"] if inner is not None else None


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
    # Individual entries are usually list items or id-bearing paragraphs
    # (Nature: <li>…<p id="ref-CR1">…</p></li>). Prefer id-bearing nodes
    # so in-text citation links keep resolving.
    entries = refs.find_all(["li", "div", "p"], id=True, recursive=True)
    if not entries:
        entries = refs.select("li.c-article-references__item") or refs.find_all(["li", "p"], recursive=True)
    seen_ids = set()
    for entry in entries:
        # Skip Nature's "Article / CAS / Google Scholar" link rows if any remain
        classes = " ".join(entry.get("class") or []).lower()
        if "references__links" in classes:
            continue
        text = entry.get_text(" ", strip=True)
        if not text or len(text) < 15:
            continue
        # Drop trailing "Google Scholar" / "CAS" / "Article" crumbs
        text = re.sub(
            r"\s*(?:Article(?:\s+CAS)?|CAS|PubMed|Google Scholar|ADS|MathSciNet)\s*$",
            "",
            text,
            flags=re.I,
        ).strip()
        entry_id = _entry_id(entry)
        if entry_id and entry_id in seen_ids:
            continue
        if entry_id:
            seen_ids.add(entry_id)
        # If we already captured the id-bearing <p>, skip the wrapping <li>
        # when both appear in `entries` (p has id, li may not — already handled).
        if entry.name == "li" and entry.find(id=True) and entry.get("id") is None:
            # Will be handled via the inner p; still OK to use li once if no p-id path
            # Prefer using this li only when we didn't already add via inner id —
            # but inner p is also in entries, so skip li without own id.
            continue
        li = soup.new_tag("li")
        li["class"] = ["ltx_bibitem"]
        if entry_id:
            # Nature uses ref-CR12; reader hover expects #bib.12
            m = re.match(r"^(?:bib\.)?ref-CR(\d+)$", entry_id, re.I)
            if m:
                li["id"] = f"bib.{m.group(1)}"
            elif entry_id.startswith("bib."):
                li["id"] = entry_id
            else:
                li["id"] = f"bib.{entry_id}"
        content = soup.new_tag("span")
        content["class"] = ["ltx_bibblock"]
        content.string = text
        li.append(content)
        ol.append(li)
    if not ol.find_all("li"):
        return None
    section.append(ol)
    return section


def _section_to_remove(el: Optional[Tag]) -> Optional[Tag]:
    """Climb to the Nature/JATS section wrapper so abstract/refs don't
    also appear in the body after we extract them separately."""
    if el is None:
        return None
    victim: Optional[Tag] = None
    for parent in [el, *list(el.parents)]:
        if not isinstance(parent, Tag):
            continue
        pid = (parent.get("id") or "").lower()
        classes = parent.get("class") or []
        # Exact class token -- not substring (avoids matching
        # c-article-section__content as a section wrapper).
        if pid.endswith("-section") or "c-article-section" in classes:
            victim = parent
            break
        if parent.name == "section" and parent is not el:
            victim = parent
            break
    if victim is None:
        victim = el
    # Nature wraps each c-article-section in a bare <section>; drop that
    # too when it would otherwise leave an empty shell titled "Abstract".
    parent = victim.parent if isinstance(victim.parent, Tag) else None
    if parent is not None and parent.name == "section":
        named = [c for c in parent.children if isinstance(c, Tag)]
        if named == [victim]:
            return parent
    return victim


def _normalize_authors(soup: BeautifulSoup, names: list[str]) -> Tag:
    wrap = soup.new_tag("div")
    # Name-only lists (typical of Nature HTML) read better as an inline
    # comma-separated line than 30+ stacked blocks meant for affiliations.
    wrap["class"] = ["ltx_authors", "ltx_authors_inline"]
    for i, name in enumerate(names):
        creator = soup.new_tag("span")
        creator["class"] = ["ltx_creator", "ltx_role_author"]
        person = soup.new_tag("span")
        person["class"] = ["ltx_personname"]
        person.string = name
        creator.append(person)
        wrap.append(creator)
        if i < len(names) - 1:
            wrap.append(soup.new_string(", "))
    return wrap


def _frontmatter_note(soup: BeautifulSoup, role: str, text: str) -> Tag:
    note = soup.new_tag("div")
    note["class"] = ["ltx_note", "ltx_note_frontmatter", f"ltx_role_{role}"]
    content = soup.new_tag("div")
    content["class"] = ["ltx_note_content"]
    content.string = text
    note.append(content)
    return note


def _absolutize_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


def _resolve_image_paths(body: Tag, source_dir: Path) -> None:
    """Rewrite image URLs so remote Nature/CDN assets keep working and
    local relative paths become absolute for restyle()'s inliner.

    Springer Nature pages use protocol-relative ``//media…`` URLs; treating
    those as local paths previously produced broken ``/media…`` srcs."""
    for img in body.find_all("img"):
        src = (img.get("src") or "").strip()
        # Prefer a real raster from srcset when src is a placeholder
        if (not src or src.startswith("data:")) and img.get("srcset"):
            # "url 685w, url2 1200w" → pick last (usually largest)
            candidates = [p.strip().split()[0] for p in img["srcset"].split(",") if p.strip()]
            if candidates:
                src = candidates[-1]
        if not src:
            continue
        src = _absolutize_url(src)
        if src.startswith(("data:", "http://", "https://")):
            img["src"] = src
            continue
        resolved = (source_dir / src).resolve()
        img["src"] = str(resolved)

    # <source srcset> inside <picture> — same protocol-relative problem
    for source in body.find_all("source"):
        srcset = source.get("srcset")
        if not srcset:
            continue
        parts = []
        for chunk in srcset.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            bits = chunk.split()
            bits[0] = _absolutize_url(bits[0])
            parts.append(" ".join(bits))
        if parts:
            source["srcset"] = ", ".join(parts)


def _flatten_nature_figures(body: Tag) -> None:
    """Simplify Nature figure chrome down to figure > img + figcaption."""
    for wrap in list(body.select(".c-article-section__figure")):
        fig = wrap.find("figure")
        if fig is None:
            continue
        # Drop "Full size image" / share chrome
        for junk in fig.select(
            ".c-article-section__figure-link, .c-article__pill-button, "
            ".c-article-section__figure-description, [id$='-desc']"
        ):
            junk.decompose()
        # Promote <picture> img; drop source siblings we don't need
        for picture in list(fig.find_all("picture")):
            img = picture.find("img")
            if img is not None:
                picture.replace_with(img)
            else:
                picture.decompose()
        # Unwrap nested content divs so restyle sees a clean figure
        for nested in list(fig.select(
            ".c-article-section__figure-content, .c-article-section__figure-item, "
            ".c-article-section__figure-picture"
        )):
            nested.unwrap()
        wrap.replace_with(fig)


def _strip_nature_chrome(body: Tag) -> None:
    """Remove leftover Nature page chrome that isn't article prose."""
    for sel in (
        ".app-explore-related-subjects",
        "#Explore-content",
        "#article-comments-section",
        "#article-comments-content",
        ".c-article-comments-list",
        "#author-information-section",
        "#author-information",
        "#author-information-content",
        "#author-notes",
        "#corresponding-author",
        "#corresponding-author-list",
        ".js-context-bar-sticky-point-mobile",
        ".c-article-info-details",
        "#additional-information-section",
        "#additional-information",
        "#additional-information-content",
        "#ethics-section",
        "#ethics",
        "#ethics-content",
        "#Ack1-section",
        "#rightslink",
        ".c-article-rights",
        ".c-article-supplementary__item",
        "#Supplementary\\:available",
        "[data-title='Supplementary information']",
        "[data-title='Rights and permissions']",
        "[data-title='About this article']",
        "[data-title='Ethics declarations']",
        "[data-title='Additional information']",
        "[data-title='Acknowledgements']",
        "[data-title='Author information']",
        ".u-js-hide",
        "button",
        ".c-article__pill-button",
    ):
        try:
            for junk in body.select(sel):
                junk.decompose()
        except Exception:
            continue
    # Empty section shells left after abstract/refs extraction
    for sec in list(body.find_all("section")):
        if not sec.get_text(strip=True):
            sec.decompose()
    # Unwrap Nature layout shells so headings/paragraphs sit at article level
    for sel in (".main-content", ".c-article-body", ".js-main-column"):
        for el in body.select(sel):
            if el is body:
                continue
            el.unwrap()
    for el in list(body.select(".c-article-section__content, .c-article-section")):
        if el is body:
            continue
        # Keep if it is the only wrapper for a heading+content pair — unwrap
        el.unwrap()
    # Drop empty leftover divs
    for div in list(body.find_all("div")):
        if div is body:
            continue
        if not div.get_text(strip=True) and not div.find(["img", "table", "figure", "svg"]):
            div.decompose()


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

    # Citation linking needs bibliography targets still present in `soup`.
    _flatten_nature_figures(body)
    _normalize_body(soup, body)
    _resolve_image_paths(body, src_path.parent)

    out_soup = BeautifulSoup(
        '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>', "lxml"
    )
    article = out_soup.new_tag("article")
    article["class"] = ["ltx_document"]
    out_soup.body.append(article)

    # Snapshot refs into ltx_bibliography before we detach them from the body.
    bib_section = _normalize_bibliography(out_soup, refs_el)

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
        paragraphs = abstract_el.find_all("p") or [abstract_el]
        for p in paragraphs:
            text = p.get_text(" ", strip=True)
            if not text:
                continue
            # Skip a lone "Abstract" label paragraph
            if re.fullmatch(r"abstract", text, re.I):
                continue
            ap = out_soup.new_tag("p")
            ap["class"] = ["ltx_p"]
            ap.string = text
            abstract_div.append(ap)
        if abstract_div.find("p"):
            article.append(abstract_div)

    # Drop abstract / references sections from the body so they aren't
    # duplicated after we place them in the standard front/back matter slots.
    for el in (abstract_el, refs_el):
        victim = _section_to_remove(el)
        if victim is not None and victim is not body:
            try:
                victim.decompose()
            except Exception:
                pass
    _strip_nature_chrome(body)

    # Move the normalized body's children into the new tree (can't just
    # append `body` itself -- it may still be wrapped in unrelated
    # ancestor markup we don't want to drag along).
    for child in list(body.children):
        article.append(child.extract())

    if bib_section is not None:
        article.append(bib_section)

    workdir_p = Path(workdir)
    workdir_p.mkdir(parents=True, exist_ok=True)
    out_path = workdir_p / "paper.html"
    out_path.write_text(str(out_soup), encoding="utf-8")
    return str(out_path)
