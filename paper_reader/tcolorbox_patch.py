"""
Same fix as siunitx_patch.py, for a different package that turned out to
trip the exact same fatal LaTeXML/expl3 crash ("Negative argument for
\\prg_replicate:nn", deep in expl3's Unicode codepoint-block setup):
`tcolorbox`. It's a heavily expl3-based package (built on pgfkeys/
l3keys2e for its box-styling options), and LaTeXML's bundled binding for
it hits the same fatal error just from being loaded -- independent of
siunitx, and independent of anything unusual the paper's own text does.

See siunitx_patch.py for the full mechanism this relies on (a local
`<name>.sty.ltxml` in the document's own directory transparently shadows
LaTeXML's bundled, broken one, regardless of how the package ends up
being loaded).

tcolorbox is fundamentally a "draw a styled, colored, bordered box"
package -- there's no way to replicate that visual styling through
plain macro substitution the way siunitx's unit formatting could be
approximated. Instead, every tcolorbox-derived environment (both
`\\newtcolorbox`-defined custom ones and the base `tcolorbox` environment
used directly) is aliased to plain LaTeX's own `quote` environment,
which LaTeXML has always handled natively -- so it renders as a plain
indented block instead of a colored/bordered callout, and instead of not
rendering at all.
"""

from __future__ import annotations

from pathlib import Path

_LTXML_SHIM = r"""# -*- mode: Perl -*-
# Minimal, non-crashing replacement for tcolorbox.sty.ltxml -- see
# paper_reader/tcolorbox_patch.py for why this exists.
package LaTeXML::Package::Pool;
use strict;
use warnings;
use LaTeXML::Package;

RawTeX(<<'EOTeX');
\newenvironment{tcolorbox}[1][]{\begin{quote}}{\end{quote}}
EOTeX

# \newtcolorbox{name}{options} -- tcolorbox's real signature also allows
# an optional leading [box name to extend] and an optional [numargs][default]
# pair before the trailing {options}; tolerated here but ignored, same as
# the options themselves.
DefMacro('\newtcolorbox [] {} [] [] {}',
  '\newenvironment{#2}{\begin{quote}}{\end{quote}}');
DefMacro('\renewtcolorbox [] {} [] [] {}',
  '\renewenvironment{#2}{\begin{quote}}{\end{quote}}');

DefMacro('\tcbset {}', '');          # global style configuration -- no-op
DefMacro('\tcbuselibrary {}', '');   # library loader -- nothing to load

#======================================================================
1;
"""


def patch_source_tree(work_dir: Path) -> None:
    """Drop the shim into `work_dir` (the directory latexmlc is actually
    run from) unless something's already there -- a paper that bundles
    its own tcolorbox.sty[.ltxml] for reproducibility should keep using
    it untouched rather than have this silently override it."""
    target = work_dir / "tcolorbox.sty.ltxml"
    if target.exists() or (work_dir / "tcolorbox.sty").exists():
        return
    target.write_text(_LTXML_SHIM, encoding="utf-8")
