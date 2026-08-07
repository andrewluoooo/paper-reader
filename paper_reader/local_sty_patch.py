"""
Auto-bind paper-bundled ``.sty`` files that LaTeXML has no binding for.

LaTeXML silently skips unknown ``\\usepackage{foo}`` when there is no
``foo.sty.ltxml``. For packages that ship *with the paper* (common for
vendored graphics stacks like ``circuitikzgit``, ``colorblind``, custom
tikz libraries), that means environments and macros never get defined and
their bodies leak into the HTML as raw TikZ source -- exactly the
``(0,0) node[mixer]...`` garbage next to Keywords-adjacent figures.

LaTeXML's own ``circuitikz``/``pgfplots`` bindings are one-liners that
``InputDefinitions(..., noltxml => 1)`` the real ``.sty``. We do the same
for every ``*.sty`` sitting in latexmlc's cwd that lacks both a local and
a system ``.sty.ltxml``, so paper-vendored packages load for real and
TikZ/PGF can emit SVG.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

_SHIM_MARKER = "paper_reader/local_sty_patch.py"

_LTXML_SHIM = """# -*- mode: Perl -*-
# {marker} -- auto-bind paper-bundled {pkg}.sty for LaTeXML
package LaTeXML::Package::Pool;
use strict;
use warnings;
use LaTeXML::Package;

InputDefinitions('{pkg}', type => 'sty', noltxml => 1);

1;
"""

# Tiny TikZ fallbacks for shapes papers often assume exist but forget to
# define (or define only in an unpublished style file). Harmless no-ops
# when a real definition is already present.
_TIKZ_FALLBACKS_LTXML = r"""# -*- mode: Perl -*-
# paper_reader/local_sty_patch.py -- common TikZ shape fallbacks
package LaTeXML::Package::Pool;
use strict;
use warnings;
use LaTeXML::Package;

AtBeginDocument(<<'EOTeX');
\providecommand{\pr@tikzfallbacks}{}%
\ifdefined\tikzset
  \tikzset{
    registershape/.style={
      rectangle, draw, thick,
      minimum height=8mm, minimum width=11mm, align=center,
    },
  }%
\fi
EOTeX

1;
"""


@lru_cache(maxsize=1)
def _latexml_package_dirs() -> tuple[Path, ...]:
    """Locate LaTeXML's Package/ directory (system bindings live there)."""
    latexmlc = shutil.which("latexmlc")
    if not latexmlc:
        return ()
    # Homebrew: .../bin/latexmlc -> .../libexec/lib/perl5/LaTeXML/Package
    here = Path(latexmlc).resolve().parent
    candidates = [
        here / "../libexec/lib/perl5/LaTeXML/Package",
        here / "../lib/perl5/LaTeXML/Package",
        here / "../share/perl5/LaTeXML/Package",
    ]
    found: list[Path] = []
    for c in candidates:
        try:
            p = c.resolve()
        except OSError:
            continue
        if p.is_dir():
            found.append(p)
    # Also ask Perl, in case the install layout differs
    try:
        out = subprocess.run(
            ["perl", "-MLaTeXML::Package", "-e", "print $INC{'LaTeXML/Package.pm'}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if out:
            pkg_pm = Path(out)
            pkg_dir = pkg_pm.parent / "Package"
            if pkg_dir.is_dir() and pkg_dir not in found:
                found.append(pkg_dir)
    except (OSError, subprocess.SubprocessError):
        pass
    return tuple(found)


def _system_has_binding(pkg: str) -> bool:
    name = f"{pkg}.sty.ltxml"
    return any((d / name).is_file() for d in _latexml_package_dirs())


def _rewrite_registershape_t_keys(src_dir: Path) -> None:
    """``node[registershape, t=LABEL, ...] () {}`` puts the label in the
    circuitikz-style ``t=`` key. Our rectangle fallback has no ``t``
    handler, so move LABEL into the node body before LaTeXML sees it."""
    pattern = re.compile(
        r"node\[registershape,\s*t=([^,\]]+),\s*([^\]]*)\]\s*(\([^)]*\))?\s*\{\}",
        re.MULTILINE,
    )

    def repl(m: re.Match) -> str:
        label, rest, name = m.group(1).strip(), m.group(2).strip(), m.group(3) or ""
        opts = f"registershape, {rest}" if rest else "registershape"
        return f"node[{opts}] {name} {{{label}}}"

    for tex in src_dir.rglob("*.tex"):
        text = tex.read_text(encoding="utf-8", errors="ignore")
        new = pattern.sub(repl, text)
        if new != text:
            tex.write_text(new, encoding="utf-8")


def patch_source_tree(work_dir: Path) -> None:
    """Write InputDefinitions shims for paper-local ``.sty`` files, plus
    TikZ fallbacks. ``work_dir`` is latexmlc's cwd (usually the main
    ``.tex``'s directory)."""
    for sty in sorted(work_dir.glob("*.sty")):
        pkg = sty.name[: -len(".sty")] if sty.name.endswith(".sty") else sty.stem
        target = work_dir / f"{pkg}.sty.ltxml"
        if target.exists():
            existing = target.read_text(encoding="utf-8", errors="ignore")
            # Leave paper-bundled or other paper_reader shims alone; refresh ours.
            if _SHIM_MARKER not in existing:
                continue
        elif _system_has_binding(pkg):
            continue
        target.write_text(
            _LTXML_SHIM.format(marker=_SHIM_MARKER, pkg=pkg),
            encoding="utf-8",
        )

    preload = work_dir / "pr_tikz_fallbacks.ltxml"
    preload.write_text(_TIKZ_FALLBACKS_LTXML, encoding="utf-8")

    _rewrite_registershape_t_keys(work_dir)
