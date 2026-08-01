from __future__ import annotations

import argparse
import os
import sys
import tempfile

from .html_convert import HtmlConvertError
from .html_convert import convert as convert_html
from .latex_convert import LatexConvertError, convert as convert_latex
from .pdf_convert import PdfConvertError
from .pdf_convert import convert as convert_pdf
from .restyle import restyle

HTML_SOURCE_SUFFIXES = (".html", ".htm")
PDF_SOURCE_SUFFIXES = (".pdf",)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="paper_reader",
        description=(
            "Convert a research paper into a single-column, reader-style HTML page -- "
            "either a LaTeX source (a .tex file, a project directory, or a source "
            "tarball/zip, e.g. an arXiv 'Other formats -> Source' download), an "
            "already-rendered HTML paper page saved from a publisher's site (browser "
            "'Save Page As... Webpage, Complete'), or a plain .pdf (structure is "
            "reconstructed from layout on a best-effort basis)."
        ),
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="Path to a .tex file, LaTeX project directory, source archive, a saved .html paper page, or a .pdf",
    )
    parser.add_argument(
        "-o", "--output", help="Path to write the output HTML file (default: alongside the source)"
    )
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help="Don't delete the intermediate LaTeXML build directory (useful for debugging conversion issues)",
    )
    parser.add_argument(
        "--library",
        action="store_true",
        help="Launch the local library web app instead (drag-and-drop papers, search, open the reader)",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port for --library (default: 8765)")
    parser.add_argument(
        "--no-browser", action="store_true", help="With --library, don't auto-open a browser tab"
    )
    parser.add_argument(
        "--rebuild-library",
        action="store_true",
        help="Re-render every paper already in the library with the current reader styling, then exit "
        "(also happens automatically whenever --library starts)",
    )
    args = parser.parse_args(argv)

    if args.rebuild_library:
        from . import server

        server.rebuild_library()
        return 0

    if args.library:
        from . import server

        server.run(port=args.port, open_browser=not args.no_browser)
        return 0

    if not args.source:
        parser.error("source is required unless --library is given")

    if not os.path.exists(args.source):
        print(f"error: no such file or directory: {args.source}", file=sys.stderr)
        return 1

    src = os.path.abspath(args.source)
    base = os.path.basename(src.rstrip("/"))
    stem = os.path.splitext(base)[0]
    out_path = os.path.abspath(args.output) if args.output else os.path.join(os.path.dirname(src), stem + ".html")

    is_html_source = src.lower().endswith(HTML_SOURCE_SUFFIXES)
    is_pdf_source = src.lower().endswith(PDF_SOURCE_SUFFIXES)

    work_ctx = tempfile.TemporaryDirectory(prefix="paper_reader_")
    workdir = work_ctx.name
    try:
        try:
            if is_html_source:
                raw_html_path = convert_html(src, workdir)
            elif is_pdf_source:
                raw_html_path = convert_pdf(src, workdir)
            else:
                raw_html_path = convert_latex(src, workdir)
        except (LatexConvertError, HtmlConvertError, PdfConvertError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        html_out, _metadata = restyle(raw_html_path, source_name=base)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_out)

        print(f"wrote {out_path}")
        return 0
    finally:
        if args.keep_work_dir:
            print(f"build directory kept at: {workdir}")
            work_ctx._finalizer.detach()  # type: ignore[attr-defined]
        else:
            work_ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
