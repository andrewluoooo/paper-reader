"""Convert a plain PDF into the same ltx_*-classed structure html_convert.py
and latex_convert.py produce, so it flows through restyle()'s existing
pipeline unchanged -- same theming, highlighting, table/figure
fit-to-width, outline, citation hover previews, everything.

Structure comes from GROBID (https://github.com/kermitt2/grobid), a
machine-learning service purpose-built for parsing scholarly PDFs -- far
more reliable than reconstructing headings/paragraphs/references from
raw font-size and position heuristics ourselves, especially for citation
parsing and bibliography structure, which is GROBID's specialty. GROBID
runs as a local HTTP service (see README: "Importing a PDF" for how to
start it); this module is just a client plus a TEI-XML -> ltx_* mapper,
mirroring html_convert.py's approach of tagging an existing tree rather
than hand-building text from scratch.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

from .html_convert import _frontmatter_note, _normalize_authors

GROBID_URL = os.environ.get("GROBID_URL", "http://localhost:8070").rstrip("/")
_TIMEOUT = 180  # GROBID's ML pipeline on a full paper can take a while, especially cold

_HEAD_LEVEL_CLASS = {1: "ltx_title_section", 2: "ltx_title_subsection", 3: "ltx_title_subsubsection"}


class PdfConvertError(RuntimeError):
    pass


def _strip_namespaces(xml_text: str) -> str:
    """GROBID's TEI output declares a default `xmlns="...tei..."` plus a
    couple of prefixed ones (xlink etc.) -- stripping them lets every
    selector below use plain local tag names instead of having to fight
    namespace-aware lookups. `xml:id` (TEI's id attribute) becomes a
    plain `id` the same way, matching every other ltx_* id convention
    already used elsewhere in the pipeline."""
    xml_text = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml_text)
    xml_text = xml_text.replace("xml:id=", "id=")
    return xml_text


def _call_grobid(pdf_bytes: bytes) -> str:
    try:
        resp = requests.post(
            f"{GROBID_URL}/api/processFulltextDocument",
            files={"input": ("paper.pdf", pdf_bytes, "application/pdf")},
            data={"consolidateHeader": "1", "consolidateCitations": "0"},
            timeout=_TIMEOUT,
        )
    except requests.exceptions.ConnectionError as e:
        raise PdfConvertError(
            f"couldn't reach the GROBID service at {GROBID_URL} -- start it first "
            "(see README: \"Importing a PDF\")"
        ) from e
    except requests.exceptions.Timeout as e:
        raise PdfConvertError("GROBID timed out processing this PDF") from e
    if resp.status_code != 200:
        raise PdfConvertError(f"GROBID returned an error (HTTP {resp.status_code}) processing this PDF")
    return resp.text


def _person_name(persname: Optional[Tag]) -> str:
    if persname is None:
        return ""
    forenames = [f.get_text(strip=True) for f in persname.find_all("forename")]
    surname = persname.find("surname")
    parts = forenames + ([surname.get_text(strip=True)] if surname else [])
    return " ".join(p for p in parts if p).strip()


def _year_from(date_tag: Optional[Tag]) -> str:
    if date_tag is None:
        return ""
    when = date_tag.get("when", "")
    m = re.match(r"\d{4}", when) or re.search(r"\d{4}", date_tag.get_text())
    return m.group(0) if m else ""


def _extract_title(tei: BeautifulSoup) -> str:
    title = tei.select_one("teiHeader titleStmt title")
    if title and title.get_text(strip=True):
        return title.get_text(" ", strip=True)
    title = tei.select_one("sourceDesc biblStruct analytic title")
    if title and title.get_text(strip=True):
        return title.get_text(" ", strip=True)
    return "Untitled"


def _extract_authors(tei: BeautifulSoup) -> list[str]:
    names = []
    for author in tei.select("sourceDesc biblStruct analytic author"):
        name = _person_name(author.find("persName"))
        if name:
            names.append(name)
    return names


def _extract_venue_year_doi(tei: BeautifulSoup) -> tuple[str, str, str]:
    venue_title = tei.select_one("sourceDesc biblStruct monogr title")
    venue = venue_title.get_text(" ", strip=True) if venue_title else ""
    year = _year_from(tei.select_one("sourceDesc biblStruct monogr imprint date"))
    idno = tei.select_one('sourceDesc biblStruct idno[type="DOI"]')
    doi = idno.get_text(strip=True) if idno else ""
    return venue, year, doi


def _extract_abstract_paragraphs(tei: BeautifulSoup) -> list[str]:
    abstract = tei.select_one("profileDesc abstract")
    if abstract is None:
        return []
    paras = abstract.find_all("p")
    if not paras:
        text = abstract.get_text(" ", strip=True)
        return [text] if text else []
    return [p.get_text(" ", strip=True) for p in paras if p.get_text(strip=True)]


def _heading_level_from_n(n: str) -> int:
    """GROBID numbers headings via `<head n="2.1">` rather than nesting
    <div>s per level -- the dot-count in that numbering is the level."""
    if not n:
        return 1
    return min(n.count(".") + 1, 3)


def _rewrite_refs(scope: Tag, tei: BeautifulSoup) -> None:
    """In-text `<ref type="bibr" target="#bN">` becomes the same
    <cite class="ltx_cite"><a href="#bib.bN" class="ltx_ref">...</a></cite>
    shape LaTeXML/html_convert.py use, which is what the reader's citation
    hover-preview and highlighting-safe wrapping key off of. Figure/table/
    equation refs have nothing to link to here, so just keep their text."""
    for ref in scope.find_all("ref"):
        if ref.get("type") != "bibr":
            ref.unwrap()
            continue
        target = (ref.get("target") or "").lstrip("#")
        if not target:
            ref.unwrap()
            continue
        a = tei.new_tag("a", href=f"#bib.{target}")
        a["class"] = ["ltx_ref"]
        a.string = ref.get_text(" ", strip=True) or "?"
        cite = tei.new_tag("cite")
        cite["class"] = ["ltx_cite", "ltx_citemacro_cite"]
        cite.append(a)
        ref.replace_with(cite)


def _normalize_body(tei: BeautifulSoup, body: Tag) -> None:
    """Mutates `body` in place, same philosophy as html_convert.py's
    _normalize_body: tag the existing tree with ltx_* classes instead of
    rebuilding text by hand, so inline markup (citations, emphasis)
    survives untouched."""
    # Figures first -- they carry their own <head> (a "Figure 1"-style
    # label) that must be consumed into the caption before the generic
    # heading pass below would otherwise misread it as a section heading.
    for fig in body.find_all("figure"):
        is_table = fig.get("type") == "table" or fig.find("table") is not None
        fig["class"] = ["ltx_table"] if is_table else ["ltx_figure"]
        head = fig.find("head")
        desc = fig.find("figDesc")
        caption_text = " ".join(
            t.get_text(" ", strip=True) for t in (head, desc) if t is not None and t.get_text(strip=True)
        )
        for stray in fig.find_all(["head", "figDesc", "graphic"]):
            stray.decompose()
        table = fig.find("table")
        if table is not None:
            for row in table.find_all("row"):
                row.name = "tr"
            for cell in table.find_all("cell"):
                cell.name = "td"
        if caption_text:
            cap = tei.new_tag("figcaption")
            cap["class"] = ["ltx_caption"]
            cap.string = caption_text
            fig.insert(0, cap)

    for head in body.find_all("head"):
        level = _heading_level_from_n(head.get("n", ""))
        head.name = "h2" if level == 1 else "h3" if level == 2 else "h4"
        head["class"] = [_HEAD_LEVEL_CLASS.get(level, "ltx_title_subsubsection")]
        if head.has_attr("n"):
            del head["n"]

    for p in body.find_all(["p", "formula"]):
        _rewrite_refs(p, tei)
        p.name = "p"
        p["class"] = ["ltx_p"]


def _format_biblstruct(bibl: Tag) -> tuple[str, str]:
    bib_id = bibl.get("id", "")
    analytic = bibl.find("analytic")
    monogr = bibl.find("monogr")

    title_text, venue = "", ""
    if analytic is not None and analytic.find("title") is not None:
        title_text = analytic.find("title").get_text(" ", strip=True)
        if monogr is not None and monogr.find("title") is not None:
            venue = monogr.find("title").get_text(" ", strip=True)
    elif monogr is not None and monogr.find("title") is not None:
        title_text = monogr.find("title").get_text(" ", strip=True)

    author_container = analytic if analytic is not None else bibl
    names = []
    for author in author_container.find_all("author"):
        name = _person_name(author.find("persName"))
        if name:
            names.append(name)

    year = _year_from(bibl.find("date"))

    parts = []
    if names:
        parts.append(", ".join(names) + ".")
    if title_text:
        parts.append(title_text + ".")
    if venue:
        parts.append(venue + ("," if year else "."))
    if year:
        parts.append(year + ".")
    text = " ".join(parts).strip()
    if not text:
        text = bibl.get_text(" ", strip=True)
    return bib_id, text


def convert(input_path: str, workdir: str) -> str:
    """Send a PDF to a local GROBID service and normalize its TEI-XML
    response into the ltx_*-classed structure restyle() expects. Returns
    the path to the generated HTML file, same contract as
    latex_convert.convert() / html_convert.convert()."""
    src_path = Path(input_path).resolve()
    if not src_path.is_file():
        raise PdfConvertError(f"no such file: {input_path}")

    tei_xml = _call_grobid(src_path.read_bytes())
    tei = BeautifulSoup(_strip_namespaces(tei_xml), "xml")

    body = tei.find("body")
    if body is None or not body.get_text(strip=True):
        raise PdfConvertError(
            "GROBID couldn't extract any body text from this PDF -- it may be a scanned "
            "image without a text layer, or an unusual layout it doesn't parse well"
        )

    title_text = _extract_title(tei)
    author_names = _extract_authors(tei)
    venue, year, doi = _extract_venue_year_doi(tei)
    abstract_paragraphs = _extract_abstract_paragraphs(tei)

    _normalize_body(tei, body)

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
    if venue:
        article.append(_frontmatter_note(out_soup, "conference", venue))
    if year:
        article.append(_frontmatter_note(out_soup, "journalyear", year))
    if doi:
        article.append(_frontmatter_note(out_soup, "doi", doi))

    if abstract_paragraphs:
        abstract_div = out_soup.new_tag("div")
        abstract_div["class"] = ["ltx_abstract"]
        abstract_heading = out_soup.new_tag("h2")
        abstract_heading["class"] = ["ltx_title_abstract"]
        abstract_heading.string = "Abstract"
        abstract_div.append(abstract_heading)
        for text in abstract_paragraphs:
            p = out_soup.new_tag("p")
            p["class"] = ["ltx_p"]
            p.string = text
            abstract_div.append(p)
        article.append(abstract_div)

    # Move the normalized body's children into the new tree, same reason
    # as html_convert.py: `body` may carry unrelated ancestor markup we
    # don't want to drag along by re-parenting `body` itself.
    for child in list(body.children):
        article.append(child.extract())

    bib_list_tag = tei.select_one('back div[type="references"] listBibl') or tei.select_one("back listBibl")
    if bib_list_tag is not None:
        bib_section = out_soup.new_tag("section")
        bib_section["class"] = ["ltx_bibliography"]
        bib_heading = out_soup.new_tag("h2")
        bib_heading["class"] = ["ltx_title_bibliography"]
        bib_heading.string = "References"
        bib_section.append(bib_heading)
        bib_list = out_soup.new_tag("ul")
        bib_list["class"] = ["ltx_biblist"]
        bib_section.append(bib_list)
        for bibl in bib_list_tag.find_all("biblStruct"):
            bib_id, text = _format_biblstruct(bibl)
            if not text:
                continue
            li = out_soup.new_tag("li")
            li["class"] = ["ltx_bibitem"]
            if bib_id:
                li["id"] = f"bib.{bib_id}"
            span = out_soup.new_tag("span")
            span["class"] = ["ltx_bibblock"]
            span.string = text
            li.append(span)
            bib_list.append(li)
        if bib_list.find("li"):
            article.append(bib_section)

    workdir_p = Path(workdir)
    workdir_p.mkdir(parents=True, exist_ok=True)
    out_path = workdir_p / "paper.html"
    out_path.write_text(str(out_soup), encoding="utf-8")
    return str(out_path)
