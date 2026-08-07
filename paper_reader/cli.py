from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time

from .epub_convert import EpubConvertError
from .epub_convert import convert as convert_epub
from .html_convert import HtmlConvertError
from .html_convert import convert as convert_html
from .latex_convert import LatexConvertError, convert as convert_latex
from .pdf_convert import PdfConvertError
from .pdf_convert import convert as convert_pdf
from .restyle import restyle

HTML_SOURCE_SUFFIXES = (".html", ".htm")
PDF_SOURCE_SUFFIXES = (".pdf",)
EPUB_SOURCE_SUFFIXES = (".epub",)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="paper_reader",
        description=(
            "Convert a research paper into a single-column, reader-style HTML page -- "
            "either a LaTeX source (a .tex file, a project directory, or a source "
            "tarball/zip, e.g. an arXiv 'Other formats -> Source' download), an "
            "already-rendered HTML paper page saved from a publisher's site (browser "
            "'Save Page As... Webpage, Complete'), a plain .pdf (parsed via a local "
            "GROBID service -- see README), or an .epub ebook."
        ),
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="Path to a .tex file, LaTeX project directory, source archive, a saved .html paper page, a .pdf, or an .epub",
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
        help="Launch the local library web app (drag-and-drop papers, search, open the reader)",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port for --library (default: 8765)")
    parser.add_argument(
        "--no-browser", action="store_true", help="With --library, don't auto-open a browser tab"
    )
    parser.add_argument(
        "--foreground",
        "--fg",
        action="store_true",
        help="With --library, run the server in the foreground instead of detaching to background",
    )
    parser.add_argument(
        "--stop-library",
        "--stop",
        action="store_true",
        help="Stop the background library server if it is running",
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

    if args.stop_library:
        from . import server

        if server.stop_server():
            print("Stopped library server.")
        else:
            print("Library server is not running.")
        return 0

    if args.library:
        from . import server

        if args.foreground:
            server.run(port=args.port, open_browser=not args.no_browser)
            return 0

        url = f"http://127.0.0.1:{args.port}/"
        if server.is_server_running(port=args.port):
            print(
                f"Andrew's Paper Library is already running at {url}  (library stored in {server.LIBRARY_DIR})"
            )
            if not args.no_browser:
                import webbrowser

                webbrowser.open(url)
            return 0

        server.LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = open(server.LOG_PATH, "a", encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            "paper_reader",
            "--library",
            "--foreground",
            "--port",
            str(args.port),
            "--no-browser",
        ]

        popen_kwargs = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            )

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            close_fds=True,
            **popen_kwargs,
        )

        start_time = time.time()
        started = False
        while time.time() - start_time < 3.0:
            if proc.poll() is not None:
                break
            if server.is_server_running(port=args.port):
                started = True
                break
            time.sleep(0.1)

        if started or (proc.poll() is None):
            print(
                f"Andrew's Paper Library running in background at {url} (PID {proc.pid})  "
                f"(library stored in {server.LIBRARY_DIR})"
            )
            if not args.no_browser:
                import webbrowser

                webbrowser.open(url)
            return 0
        else:
            print(
                f"error: failed to start library server. See log file at {server.LOG_PATH}",
                file=sys.stderr,
            )
            return 1

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
    is_epub_source = src.lower().endswith(EPUB_SOURCE_SUFFIXES)

    work_ctx = tempfile.TemporaryDirectory(prefix="paper_reader_")
    workdir = work_ctx.name
    try:
        try:
            if is_html_source:
                raw_html_path = convert_html(src, workdir)
            elif is_pdf_source:
                raw_html_path = convert_pdf(src, workdir)
            elif is_epub_source:
                raw_html_path = convert_epub(src, workdir)
            else:
                raw_html_path = convert_latex(src, workdir)
        except (LatexConvertError, HtmlConvertError, PdfConvertError, EpubConvertError) as e:
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
