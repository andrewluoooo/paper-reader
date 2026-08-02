"""
Convert a LaTeX paper source (single .tex file, a project directory, or an
arXiv-style source tarball) into structured HTML5+MathML using LaTeXML
(https://dlmf.nist.gov/LaTeXML/), which is what arXiv itself uses to
produce its HTML paper views.

LaTeXML's own image post-processing needs the Perl `Image::Magick`
binding, which is a heavy/fragile install (CPAN, built against the local
ImageMagick). To avoid that dependency we rasterize any vector figures
(PDF/EPS, the common `\\includegraphics` case for matplotlib/tikz output)
to PNG ourselves with `pdftoppm`/`gs` first, and rewrite the `.tex`
sources to point at the rasterized copies before invoking `latexmlc`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Optional

from . import algorithm2e_patch, siunitx_patch

VECTOR_EXTS = {".pdf", ".eps"}
INCLUDEGRAPHICS_RE = re.compile(r"(\\includegraphics(?:\s*\[[^\]]*\])?\{)([^}]*)(\})")


class LatexConvertError(RuntimeError):
    pass


def _which_or_raise(tool: str, purpose: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise LatexConvertError(f"required tool '{tool}' not found on PATH ({purpose})")
    return path


def _prepare_source_dir(input_path: str, workdir: Path) -> Path:
    """Materialize the LaTeX project as a plain directory under `workdir`,
    regardless of whether `input_path` was a .tex file, a directory, or a
    tar/zip archive. Never mutates the user's original files."""
    src_dir = workdir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    p = Path(input_path)

    if p.is_dir():
        shutil.copytree(p, src_dir, dirs_exist_ok=True)
    elif tarfile.is_tarfile(p):
        with tarfile.open(p) as tf:
            tf.extractall(src_dir, filter="data")
    elif zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as zf:
            zf.extractall(src_dir)
    elif p.suffix.lower() == ".tex":
        shutil.copytree(p.parent, src_dir, dirs_exist_ok=True)
    else:
        raise LatexConvertError(f"unrecognized input type: {input_path}")

    return src_dir


def _find_main_tex(src_dir: Path) -> Path:
    tex_files = list(src_dir.rglob("*.tex"))
    if not tex_files:
        raise LatexConvertError(f"no .tex files found under {src_dir}")
    if len(tex_files) == 1:
        return tex_files[0]

    candidates = []
    for f in tex_files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if r"\documentclass" in text and r"\begin{document}" in text:
            candidates.append(f)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        for name in ("main.tex", "paper.tex", "root.tex"):
            for f in candidates:
                if f.name.lower() == name:
                    return f
        return min(candidates, key=lambda f: len(f.parts))  # prefer top-level

    raise LatexConvertError(
        f"could not determine the main .tex file among: {[str(f) for f in tex_files]}"
    )


def _use_prebuilt_bibliography(main_tex: Path) -> None:
    """If a pre-generated .bbl sits next to the main .tex (the common arXiv
    submission pattern -- authors check in the bibtex/biber output since
    arXiv doesn't reliably run bibtex itself), point the document at it
    directly instead of \\bibliography{...}.

    LaTeXML's built-in BibTeX emulation only understands classic BibTeX.
    Many modern .bib files are biblatex-flavored (a bare `date = {...}`
    field instead of `year`, entry types like @online/@software) that it
    can't resolve, silently leaving every \\cite as a "missing bibkey"
    with no error. A checked-in .bbl is already-formatted plain LaTeX by
    the time LaTeXML sees it, sidestepping the whole problem."""
    bbl = main_tex.with_suffix(".bbl")
    if not bbl.is_file():
        return
    text = main_tex.read_text(encoding="utf-8", errors="ignore")
    new_text = re.sub(r"\\bibliography\{[^}]*\}", f"\\\\input{{{bbl.name}}}", text)
    if new_text != text:
        main_tex.write_text(new_text, encoding="utf-8")


def _rasterize_vector_figures(src_dir: Path) -> set[str]:
    """Convert every .pdf/.eps figure under src_dir to a sibling .png (via
    pdftoppm / ghostscript) and delete the vector original, so LaTeXML's
    graphics resolution has no choice but to pick up the raster copy.
    Returns the set of basenames (without extension) that were converted.
    """
    converted: set[str] = set()
    pdftoppm = shutil.which("pdftoppm")
    gs = shutil.which("gs")

    for f in list(src_dir.rglob("*")):
        if f.suffix.lower() not in VECTOR_EXTS:
            continue
        png_path = f.with_suffix(".png")
        try:
            if f.suffix.lower() == ".pdf" and pdftoppm:
                subprocess.run(
                    [pdftoppm, "-png", "-r", "200", "-singlefile", str(f), str(png_path.with_suffix(""))],
                    check=True, capture_output=True,
                )
            elif f.suffix.lower() == ".eps" and gs:
                subprocess.run(
                    [gs, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=png16m", "-r200",
                     f"-sOutputFile={png_path}", str(f)],
                    check=True, capture_output=True,
                )
            elif f.suffix.lower() == ".pdf":
                _which_or_raise("pdftoppm", "rasterizing PDF figures")
            else:
                _which_or_raise("gs", "rasterizing EPS figures")
        except subprocess.CalledProcessError as e:
            raise LatexConvertError(f"failed to rasterize {f}: {e.stderr.decode(errors='ignore')[:500]}")

        if png_path.is_file():
            converted.add(f.stem)
            f.unlink()

    return converted


def _rewrite_includegraphics(src_dir: Path, converted_basenames: set[str]) -> None:
    if not converted_basenames:
        return
    for tex_file in src_dir.rglob("*.tex"):
        text = tex_file.read_text(encoding="utf-8", errors="ignore")

        def repl(m: re.Match) -> str:
            prefix, path, suffix = m.groups()
            stem, ext = os.path.splitext(path)
            base = os.path.basename(stem)
            if base in converted_basenames and ext.lower() in (".pdf", ".eps", ""):
                return f"{prefix}{stem}.png{suffix}"
            return m.group(0)

        new_text = INCLUDEGRAPHICS_RE.sub(repl, text)
        if new_text != text:
            tex_file.write_text(new_text, encoding="utf-8")


def convert(input_path: str, workdir: str) -> str:
    """Run the full LaTeX -> HTML5 conversion. Returns the path to the
    generated HTML file (figures sit alongside it as relative files, so
    the caller should resolve <img src> relative to its directory)."""
    _which_or_raise("latexmlc", "converting LaTeX to HTML")

    workdir_p = Path(workdir)
    workdir_p.mkdir(parents=True, exist_ok=True)

    src_dir = _prepare_source_dir(input_path, workdir_p)
    main_tex = _find_main_tex(src_dir)
    _use_prebuilt_bibliography(main_tex)

    converted = _rasterize_vector_figures(src_dir)
    _rewrite_includegraphics(src_dir, converted)
    algorithm2e_patch.patch_source_tree(src_dir)
    siunitx_patch.patch_source_tree(main_tex.parent)

    out_html = main_tex.with_name("paper.html")
    log_path = main_tex.with_name("latexml_run.log")

    # latexmlc's own default timeout (600s) is too tight for long papers with
    # many figures/citations -- raise it rather than have a real, otherwise
    # fine conversion get cut off mid-way and silently produce an empty stub.
    cmd = ["latexmlc", f"--dest={out_html.name}", "--format=html5", "--timeout=1800", main_tex.name]
    proc = subprocess.run(cmd, cwd=main_tex.parent, capture_output=True, text=True)
    log_path.write_text((proc.stdout or "") + "\n" + (proc.stderr or ""), encoding="utf-8")

    if not out_html.is_file() or out_html.stat().st_size < 500:
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-3000:]
        raise LatexConvertError(
            f"latexmlc did not produce usable HTML for {main_tex}.\n"
            f"--- tail of latexmlc output ---\n{tail}\n"
            f"(full log: {log_path})"
        )

    return str(out_html)
