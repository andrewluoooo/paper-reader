"""
Same technique as siunitx_patch.py/tcolorbox_patch.py, for a different
failure mode: `makecell` (and the tightly-related `\\cellcolor`, from the
`colortbl`/`xcolor` "table" option) isn't a LaTeXML binding crash -- on a
minimal texlive install, the package can be entirely absent, so LaTeXML
has nothing to load at all and `\\makecell`/`\\cellcolor` are simply
undefined.

That's a much worse failure mode inside a table than it sounds: `\\makecell
[pos]{line1\\\\line2}` relies on makecell locally rescoping `\\\\` to mean
"line break within this cell" for the duration of its argument. Left
undefined, that inner `\\\\` isn't rescoped by anything -- it falls
through to the *enclosing* tabular environment's own meaning of `\\\\`
("end this row"), splicing a phantom row break into the middle of a
table cell. That desyncs the table's row/column structure for
everything after it, which is why this can visibly corrupt content well
past the table itself, not just the cell in question.

The fix doesn't need makecell's own implementation -- TeX's own
`\\shortstack` primitive (core LaTeX, always available) already does
exactly "stack short lines vertically inside a confined box" with `\\\\`
correctly scoped to just that box, which is all `\\makecell` is really
providing. `\\cellcolor` has no plain-substitution equivalent (it's a
background color, not content) and is dropped to a no-op -- losing the
highlight but keeping whatever content follows it intact, which is what
actually matters for reading the table.
"""

from __future__ import annotations

from pathlib import Path

_LTXML_SHIM = r"""# -*- mode: Perl -*-
# Minimal, non-crashing replacement for makecell.sty.ltxml -- see
# paper_reader/makecell_patch.py for why this exists.
package LaTeXML::Package::Pool;
use strict;
use warnings;
use LaTeXML::Package;

DefMacro('\makecell [Default:c] {}', '\shortstack[#1]{#2}');
DefMacro('\thead [Default:c] {}',    '\textbf{\shortstack[#1]{#2}}');
DefMacro('\theadfont {}',            '\textbf{#1}');

DefMacro('\cellcolor [] {}', '');   # background color -- no plain-text equivalent, drop it
DefMacro('\rowcolor [] {}',  '');
DefMacro('\columncolor [] {}', '');

#======================================================================
1;
"""


def patch_source_tree(work_dir: Path) -> None:
    """Drop the shim into `work_dir` (the directory latexmlc is actually
    run from) unless something's already there -- a paper that bundles
    its own makecell.sty[.ltxml] for reproducibility should keep using
    it untouched rather than have this silently override it."""
    target = work_dir / "makecell.sty.ltxml"
    if target.exists() or (work_dir / "makecell.sty").exists():
        return
    target.write_text(_LTXML_SHIM, encoding="utf-8")
