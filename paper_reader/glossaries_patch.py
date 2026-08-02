"""
Same technique as makecell_patch.py: on a minimal texlive install,
`glossaries` can be entirely absent (not a binding crash -- there's
nothing for LaTeXML to load at all), leaving `\\gls`/`\\newacronym` and
friends undefined.

Unlike makecell, an undefined `\\gls{...}` doesn't corrupt surrounding
structure the way an undefined `\\makecell` can (its argument has no
embedded `\\\\` to leak) -- but it's typically used many dozens of times
across a whole paper (every acronym reference), so leaving it undefined
means that many separate broken points throughout the body text instead
of one.

This implements just the common core -- \\newacronym to register a
term, \\gls/\\Gls/\\glspl to reference it -- with real first-use-expands-
then-abbreviates behavior (matching what glossaries actually does by
default), using plain TeX's own \\csname-based dynamic macro definition
(the same "build a macro name from an argument" trick already used in
algorithm2e_patch.py's PREAMBLE_SHIM) rather than LaTeXML's Perl API, so
this only needs LaTeXML to process ordinary TeX macros, not a real
glossaries implementation. It doesn't attempt the rest of glossaries'
API (custom entries beyond acronyms, printed glossary lists, etc.) --
those are considerably less common than plain inline \\gls references.
"""

from __future__ import annotations

from pathlib import Path

_LTXML_SHIM = r"""# -*- mode: Perl -*-
# Minimal, non-crashing replacement for glossaries.sty.ltxml -- see
# paper_reader/glossaries_patch.py for why this exists.
package LaTeXML::Package::Pool;
use strict;
use warnings;
use LaTeXML::Package;

RawTeX(<<'EOTeX');
\newcommand{\newacronym}[4][]{%
  \expandafter\def\csname gls@short@#2\endcsname{#3}%
  \expandafter\def\csname gls@long@#2\endcsname{#4}%
  \expandafter\let\csname gls@used@#2\endcsname\relax
}
\newcommand{\@gls@show}[1]{%
  \expandafter\ifx\csname gls@used@#1\endcsname\relax
    \csname gls@long@#1\endcsname\ (\csname gls@short@#1\endcsname)%
    \expandafter\def\csname gls@used@#1\endcsname{1}%
  \else
    \csname gls@short@#1\endcsname
  \fi
}
\newcommand{\gls}[1]{\@gls@show{#1}}
\newcommand{\Gls}[1]{\@gls@show{#1}}
\newcommand{\GLS}[1]{\@gls@show{#1}}
\newcommand{\glspl}[1]{\@gls@show{#1}s}
\newcommand{\Glspl}[1]{\@gls@show{#1}s}
\newcommand{\acrfull}[1]{\@gls@show{#1}}
\newcommand{\acrshort}[1]{\csname gls@short@#1\endcsname}
\newcommand{\acrlong}[1]{\csname gls@long@#1\endcsname}
\newcommand{\glsresetall}{}
\newcommand{\glsdisablehyper}{}
\newcommand{\loadglsentries}[2][]{\input{#2}}
EOTeX

#======================================================================
1;
"""


def patch_source_tree(work_dir: Path) -> None:
    """Drop the shim into `work_dir` (the directory latexmlc is actually
    run from) unless something's already there -- a paper that bundles
    its own glossaries.sty[.ltxml] for reproducibility should keep using
    it untouched rather than have this silently override it."""
    target = work_dir / "glossaries.sty.ltxml"
    if target.exists() or (work_dir / "glossaries.sty").exists():
        return
    target.write_text(_LTXML_SHIM, encoding="utf-8")
