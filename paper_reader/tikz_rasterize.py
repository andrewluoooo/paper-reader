"""
Pre-rasterize TikZ pictures that LaTeXML cannot render faithfully.

LaTeXML often mangles non-trivial TikZ: pgfplots may emit empty SVGs, and
architecture / block diagrams can end up with wrong scale, position, and
z-order (labels overlapping body text). Real ``pdflatex`` renders those
pictures correctly, so we extract each substantial ``tikzpicture`` (and
``\\input{….tikz}`` files), compile to a cropped PNG, and replace the
source with ``\\includegraphics``.

Tiny inline decorations (short pictures without plots) are left for
LaTeXML when possible.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_PLOT_MARKERS = (
    r"\\begin\{axis\}",
    r"\\begin\{semilogyaxis\}",
    r"\\begin\{loglogaxis\}",
    r"\\begin\{semilogxaxis\}",
    r"\\addplot",
    r"\\pgfplotsset",
)
_PLOT_RE = re.compile("|".join(_PLOT_MARKERS))

_DRAW_RE = re.compile(
    r"\\(?:node|draw|path|fill|pic\b|matrix|addplot)|"
    r"\\begin\{(?:axis|semilogyaxis|loglogaxis|semilogxaxis)\}"
)

_BEGIN_TIKZ_RE = re.compile(r"\\begin\{tikzpicture\}(\s*\[[^\]]*\])?")
_END_TIKZ = r"\end{tikzpicture}"

_INPUT_TIKZ_RE = re.compile(
    r"\\input\s*\{([^}]+\.tikz)\}",
    re.IGNORECASE,
)

_USEPACKAGE_RE = re.compile(
    r"\\usepackage(?:\s*\[[^\]]*\])?\s*\{([^}]+)\}"
)
_USETIKZLIB_RE = re.compile(r"\\usetikzlibrary\s*\{[^}]+\}")
_USEPGFPLOTSLIB_RE = re.compile(r"\\usepgfplotslibrary\s*\{[^}]+\}")

# Packages from the paper preamble that help TikZ compile, when installed.
_TIKZ_RELATED_PKGS = frozenset({
    "circuitikz",
    "tkz-euclide",
    "tkz-base",
    "pgf-pie",
    "forest",
    "tikz-cd",
    "tikz-3dplot",
    "smartdiagram",
    "pgfplots",
    "pgfplotstable",
    "xcolor",
    "amsmath",
    "amssymb",
    "amsfonts",
    "babel",
})


def _extract_brace_group(text: str, open_idx: int) -> str | None:
    """Return the `{...}` group starting at ``open_idx``, or None."""
    if open_idx >= len(text) or text[open_idx] != "{":
        return None
    depth = 0
    for i in range(open_idx, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
    return None


def _find_pgfplotssets(text: str) -> list[str]:
    out: list[str] = []
    needle = r"\pgfplotsset"
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            break
        j = i + len(needle)
        while j < len(text) and text[j].isspace():
            j += 1
        group = _extract_brace_group(text, j)
        if group:
            out.append(needle + group)
            start = j + len(group)
        else:
            start = j + 1
    return out


def _find_pdflatex() -> str | None:
    return shutil.which("pdflatex")


def _pkg_available(name: str, work_dir: Path) -> bool:
    if (work_dir / f"{name}.sty").is_file():
        return True
    kpsewhich = shutil.which("kpsewhich")
    if not kpsewhich:
        return False
    try:
        r = subprocess.run(
            [kpsewhich, f"{name}.sty"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(r.stdout.strip())


def _should_rasterize(body: str) -> bool:
    """True for plots and non-trivial diagrams; false for tiny decorations."""
    if _PLOT_RE.search(body):
        return True
    compact = re.sub(r"\s+", " ", body).strip()
    if len(compact) < 250:
        return False
    if not _DRAW_RE.search(body):
        return False
    # Architecture / block diagrams: many commands or multi-line layout.
    if body.count("\\") >= 10 or body.count("\n") >= 6:
        return True
    return len(compact) >= 600


def _extract_balanced_env(text: str, begin_match: re.Match) -> tuple[int, int, str] | None:
    """Return (start, end, body_including_begin_end) for a tikzpicture."""
    start = begin_match.start()
    i = begin_match.end()
    depth = 1
    while depth and i < len(text):
        next_begin = text.find(r"\begin{tikzpicture}", i)
        next_end = text.find(_END_TIKZ, i)
        if next_end < 0:
            return None
        if next_begin >= 0 and next_begin < next_end:
            depth += 1
            i = next_begin + len(r"\begin{tikzpicture}")
        else:
            depth -= 1
            i = next_end + len(_END_TIKZ)
            if depth == 0:
                return start, i, text[start:i]
    return None


def _harvest_preamble(main_tex: Path) -> str:
    """Pull TikZ/pgfplots-related preamble lines from the main document."""
    text = main_tex.read_text(encoding="utf-8", errors="ignore")
    end = text.find(r"\begin{document}")
    preamble = text[: end if end >= 0 else len(text)]
    work_dir = main_tex.parent

    lines: list[str] = [
        r"\usepackage{amsmath}",
        r"\usepackage{xcolor}",
        r"\usepackage{tikz}",
        r"\usepackage{pgfplots}",
        r"\usepackage{pgfplotstable}",
        r"\pgfplotsset{compat=newest}",
    ]

    # Local .sty files the paper ships (colorblind, circuitikzgit, ...)
    for sty in sorted(work_dir.glob("*.sty")):
        pkg = sty.name[: -len(".sty")]
        opt_m = re.search(
            rf"\\usepackage\s*\[([^\]]*)\]\s*\{{{re.escape(pkg)}\}}",
            preamble,
        )
        if opt_m:
            lines.append(f"\\usepackage[{opt_m.group(1)}]{{{pkg}}}")
        else:
            lines.append(f"\\usepackage{{{pkg}}}")

    for m in _USEPACKAGE_RE.finditer(preamble):
        for pkg in (p.strip() for p in m.group(1).split(",")):
            if pkg not in _TIKZ_RELATED_PKGS:
                continue
            if pkg in {"amsmath", "xcolor", "tikz", "pgfplots", "pgfplotstable"}:
                continue
            if not _pkg_available(pkg, work_dir):
                continue
            opt_m = re.search(
                rf"\\usepackage\s*\[([^\]]*)\]\s*\{{{re.escape(pkg)}\}}",
                preamble,
            )
            line = (
                f"\\usepackage[{opt_m.group(1)}]{{{pkg}}}"
                if opt_m
                else f"\\usepackage{{{pkg}}}"
            )
            if line not in lines:
                lines.append(line)

    for m in _USETIKZLIB_RE.finditer(preamble):
        line = m.group(0)
        if line not in lines:
            lines.append(line)
    for m in _USEPGFPLOTSLIB_RE.finditer(preamble):
        line = m.group(0)
        if line not in lines:
            lines.append(line)
    lines.extend(_find_pgfplotssets(preamble))
    return "\n".join(lines)


def _stub_refs(body: str) -> str:
    """Replace \\eqref/\\ref/\\ac inside plots with inert text so a
    standalone compile doesn't need the whole paper's aux file."""
    body = re.sub(r"\\eqref\{[^}]+\}", r"(\\textrm{?})", body)
    body = re.sub(r"\\ref\{[^}]+\}", r"?", body)
    body = re.sub(r"\\ac[slp]?\{([^}]+)\}", r"\1", body)
    return body


def _trim_png(png_path: Path) -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    im = Image.open(png_path).convert("RGB")
    px = im.load()
    w, h = im.size
    bg = (255, 255, 255)
    tol = 12

    def near(c: tuple[int, ...]) -> bool:
        return all(abs(c[i] - bg[i]) <= tol for i in range(3))

    try:
        top = next(y for y in range(h) if not all(near(px[x, y]) for x in range(w)))
        bot = h - 1 - next(y for y in range(h) if not all(near(px[x, h - 1 - y]) for x in range(w)))
        left = next(x for x in range(w) if not all(near(px[x, y]) for y in range(top, bot + 1)))
        right = w - 1 - next(
            x for x in range(w) if not all(near(px[w - 1 - x, y]) for y in range(top, bot + 1))
        )
    except StopIteration:
        return
    pad = 4
    im.crop((
        max(0, left - pad), max(0, top - pad),
        min(w, right + 1 + pad), min(h, bot + 1 + pad),
    )).save(png_path)


def _geometry_for(body: str) -> str:
    """Large canvases for Huge architecture diagrams; compact for plots."""
    large = (
        len(body) > 1500
        or bool(re.search(r"\\Huge|\\huge", body))
        or bool(re.search(r"minimum width=\d", body))
    )
    if large:
        return r"\usepackage[margin=0.4cm,paperwidth=50cm,paperheight=50cm]{geometry}"
    return r"\usepackage[margin=0.4cm,paperwidth=20cm,paperheight=20cm]{geometry}"


def _includegraphics_cmd(png_rel: Path, body: str) -> str:
    # Plots are usually wider than tall; block diagrams may be taller.
    if _PLOT_RE.search(body):
        return (
            f"\\includegraphics[width=\\linewidth,height=0.35\\textheight,"
            f"keepaspectratio]{{{png_rel.as_posix()}}}"
        )
    return (
        f"\\includegraphics[width=\\linewidth,height=0.55\\textheight,"
        f"keepaspectratio]{{{png_rel.as_posix()}}}"
    )


def _compile_snippet(
    work_dir: Path,
    snippet_id: int,
    body: str,
    preamble_pkgs: str,
    relative_dir: Path,
) -> Path | None:
    """Compile one tikzpicture to PNG; return path relative to work_dir, or None."""
    del relative_dir  # reserved for future path rewriting
    pdflatex = _find_pdflatex()
    pdftoppm = shutil.which("pdftoppm")
    if not pdflatex or not pdftoppm:
        return None

    out_dir = work_dir / "pr_tikz_raster"
    out_dir.mkdir(exist_ok=True)
    stem = f"plot_{snippet_id:03d}"
    tex_path = out_dir / f"{stem}.tex"
    body = _stub_refs(body)

    # Paths in \\addplot table{figures/...} are relative to the main tex
    # dir, not out_dir -- chdir to work_dir and point output elsewhere via
    # -output-directory.
    tex_path.write_text(
        "\\documentclass{article}\n"
        f"{_geometry_for(body)}\n"
        "\\pagestyle{empty}\n"
        f"{preamble_pkgs}\n"
        "\\begin{document}\n"
        "\\noindent\n"
        f"{body}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    try:
        subprocess.run(
            [
                pdflatex,
                "-interaction=nonstopmode",
                f"-output-directory={out_dir}",
                str(tex_path.name),
            ],
            cwd=work_dir,  # so figures/ CSV paths resolve
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    pdf = out_dir / f"{stem}.pdf"
    if not pdf.is_file() or pdf.stat().st_size < 200:
        return None

    try:
        subprocess.run(
            [pdftoppm, "-png", "-r", "200", "-singlefile", str(pdf), str(out_dir / stem)],
            check=True, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError, subprocess.CalledProcessError):
        return None

    png = out_dir / f"{stem}.png"
    if not png.is_file():
        return None
    _trim_png(png)
    return Path("pr_tikz_raster") / f"{stem}.png"


def _file_local_pgfplotssets(text: str, before: int) -> str:
    """Capture \\pgfplotsset{...} blocks in this file that appear before a
    picture -- plot styles like ``afu error/.style=...`` live next to the
    figures that use them, not in the main preamble."""
    return "\n".join(_find_pgfplotssets(text[:before]))


def _resolve_input_path(work_dir: Path, tex_file: Path, rel: str) -> Path | None:
    rel = rel.strip().replace("\\", "/")
    candidates = [
        tex_file.parent / rel,
        work_dir / rel,
    ]
    for c in candidates:
        try:
            resolved = c.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _patch_tex_file(
    work_dir: Path,
    tex_file: Path,
    preamble: str,
    snippet_id_start: int,
) -> tuple[int, int]:
    """Patch one .tex file. Returns (rasterized_count, next_snippet_id)."""
    text = tex_file.read_text(encoding="utf-8", errors="ignore")
    if not (_BEGIN_TIKZ_RE.search(text) or _INPUT_TIKZ_RE.search(text)):
        return 0, snippet_id_start

    # Collect replacements as (start, end, body_for_compile, kind)
    # kind is 'env' or 'input'; body is the tikz source to compile.
    jobs: list[tuple[int, int, str]] = []

    for m in _INPUT_TIKZ_RE.finditer(text):
        path = _resolve_input_path(work_dir, tex_file, m.group(1))
        if path is None:
            continue
        body = path.read_text(encoding="utf-8", errors="ignore")
        if not _should_rasterize(body):
            continue
        jobs.append((m.start(), m.end(), body))

    for begin in _BEGIN_TIKZ_RE.finditer(text):
        extracted = _extract_balanced_env(text, begin)
        if not extracted:
            continue
        start, end, body = extracted
        if not _should_rasterize(body):
            continue
        # Skip if this span is inside an already-scheduled \\input span
        # (shouldn't happen — inputs are external files).
        jobs.append((start, end, body))

    if not jobs:
        return 0, snippet_id_start

    # Drop overlapping jobs (prefer earlier / longer).
    jobs.sort(key=lambda j: (j[0], -(j[1] - j[0])))
    filtered: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, body in jobs:
        if start < last_end:
            continue
        filtered.append((start, end, body))
        last_end = end
    jobs = filtered

    out: list[str] = []
    pos = 0
    count = 0
    snippet_id = snippet_id_start
    changed = False

    for start, end, body in jobs:
        out.append(text[pos:start])
        snippet_id += 1
        local_sets = _file_local_pgfplotssets(text, start)
        body_with_styles = f"{local_sets}\n{body}" if local_sets else body
        png_rel = _compile_snippet(
            work_dir, snippet_id, body_with_styles, preamble, tex_file.parent
        )
        if png_rel is not None:
            out.append(_includegraphics_cmd(png_rel, body))
            count += 1
            changed = True
        else:
            out.append(text[start:end])
        pos = end

    out.append(text[pos:])
    if changed:
        tex_file.write_text("".join(out), encoding="utf-8")
    return count, snippet_id


def patch_source_tree(work_dir: Path, main_tex: Path | None = None) -> int:
    """Replace substantial TikZ under ``work_dir`` with PNGs.
    Returns the number of pictures successfully rasterized."""
    if main_tex is None:
        mains = list(work_dir.glob("*.tex"))
        main_tex = next(
            (p for p in mains if r"\documentclass" in p.read_text(encoding="utf-8", errors="ignore")),
            None,
        )
        if main_tex is None:
            return 0

    if not _find_pdflatex() or not shutil.which("pdftoppm"):
        return 0

    preamble = _harvest_preamble(main_tex)
    count = 0
    snippet_id = 0

    for tex_file in sorted(work_dir.rglob("*.tex")):
        if "pr_tikz_raster" in tex_file.parts:
            continue
        n, snippet_id = _patch_tex_file(work_dir, tex_file, preamble, snippet_id)
        count += n

    return count
