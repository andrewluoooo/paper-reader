"""
LaTeXML's own bundled `siunitx.sty.ltxml` binding can hit a fatal,
essentially unrecoverable error deep in expl3's Unicode codepoint-block
setup ("Negative argument for \\prg_replicate:nn", cascading through
`l__codepoint_grapheme_block_clist`), which kills the entire conversion
before it produces any output. This isn't triggered by anything unusual
the paper's own text does -- it happens purely from *loading* the
package, so a paper doesn't need to use a single \\num or \\SI for this
to blow up the whole build.

siunitx also doesn't have to be requested by the paper's own \\usepackage
list to end up loaded -- many journal/conference document classes probe
for it and load it conditionally (`\\IfFileExists{siunitx.sty}{...}`-style
idioms, each class spelling this differently), so trying to detect "does
this paper use siunitx" by pattern-matching the .tex source is a losing
game -- there's no reliable way to see every class's own indirection.

Instead this works one layer down, at the mechanism LaTeXML itself uses
to resolve *any* \\usepackage{siunitx} call, however it gets triggered:
LaTeXML always searches for a matching `<name>.sty.ltxml` binding before
ever considering a raw `.sty` file, and it searches the document's own
directory before its own bundled Package/ directory. Dropping a minimal,
non-crashing `siunitx.sty.ltxml` binding into the document's directory
transparently shadows LaTeXML's broken bundled one -- verified directly:
LaTeXML's log shows it loading the local file, not
`Package/siunitx.sty.ltxml`, regardless of whether the paper or its class
is the one asking for the package.

This shim doesn't attempt siunitx's real locale-aware number formatting
or rounding -- it covers the common commands (\\num, \\si, \\SI, \\qty,
\\ang, ranges, lists, \\sisetup, \\DeclareSIUnit) and a broad set of unit
and prefix macros with plain substitution, which is enough to render
sensibly instead of not rendering at all. A paper leaning on siunitx's
fancier formatting options will get simplified (not identical) output;
one that never touches siunitx at all is completely unaffected either
way, since the shim just sits there unused.
"""

from __future__ import annotations

from pathlib import Path

_LTXML_SHIM = r"""# -*- mode: Perl -*-
# Minimal, non-crashing replacement for siunitx.sty.ltxml -- see
# paper_reader/siunitx_patch.py for why this exists.
package LaTeXML::Package::Pool;
use strict;
use warnings;
use LaTeXML::Package;

# ---- core formatting commands (all take an optional [key=value,...] first
#      arg, like real siunitx, which is simply ignored here) --------------
DefMacro('\num [] {}',            '#2');
DefMacro('\ang [] {}',            '#2\ensuremath{{}^{\circ}}');
DefMacro('\si [] {}',             '#2');
DefMacro('\SI [] {} {}',          '#2\,#3');
DefMacro('\qty [] {} {}',         '#2\,#3');   # siunitx 3.x alias for \SI
DefMacro('\numrange [] {} {}',    '#2--#3');
DefMacro('\SIrange [] {} {} {}',  '#2--#3\,#4');
DefMacro('\qtyrange [] {} {} {}', '#2--#3\,#4');
DefMacro('\numlist [] {}',        '#2');
DefMacro('\SIlist [] {} {}',      '#2\,#3');
DefMacro('\sisetup {}',           '');    # configuration call -- no-op
DefMacro('\DeclareSIUnit [] {} {}', '');  # custom unit declarations -- swallow, don't crash
DefMacro('\sinceversion {}',      '');

# ---- prefixes (concatenate with whatever unit macro follows) ------------
DefMacro('\yocto', 'y'); DefMacro('\zepto', 'z'); DefMacro('\atto', 'a');
DefMacro('\femto', 'f'); DefMacro('\pico', 'p');  DefMacro('\nano', 'n');
DefMacro('\micro', '\ensuremath{\mu}');            DefMacro('\milli', 'm');
DefMacro('\centi', 'c'); DefMacro('\deci', 'd');   DefMacro('\deca', 'da');
DefMacro('\hecto', 'h'); DefMacro('\kilo', 'k');   DefMacro('\mega', 'M');
DefMacro('\giga', 'G');  DefMacro('\tera', 'T');   DefMacro('\peta', 'P');
DefMacro('\exa', 'E');   DefMacro('\zetta', 'Z');  DefMacro('\yotta', 'Y');

# ---- modifiers (approximated as text rather than true superscripts,
#      since correctly reordering "\square\meter" -> "m^2" instead of
#      "^2 m" needs look-ahead token surgery that isn't worth the
#      complexity for how rarely these show up) ---------------------------
DefMacro('\per',   '/');
DefMacro('\square', 'sq.\ ');
DefMacro('\cubic',  'cu.\ ');
DefMacro('\raiseto {} {}', '#2\ensuremath{^{#1}}');

# ---- base SI units --------------------------------------------------
DefMacro('\metre', 'm');    DefMacro('\meter', 'm');
DefMacro('\gram', 'g');     DefMacro('\second', 's');
DefMacro('\ampere', 'A');   DefMacro('\kelvin', 'K');
DefMacro('\mole', 'mol');   DefMacro('\candela', 'cd');

# ---- common derived / accepted units ---------------------------------
DefMacro('\hertz', 'Hz');   DefMacro('\newton', 'N');
DefMacro('\pascal', 'Pa');  DefMacro('\joule', 'J');
DefMacro('\watt', 'W');     DefMacro('\coulomb', 'C');
DefMacro('\volt', 'V');     DefMacro('\farad', 'F');
DefMacro('\ohm', '\ensuremath{\Omega}');
DefMacro('\siemens', 'S');  DefMacro('\weber', 'Wb');
DefMacro('\tesla', 'T');    DefMacro('\henry', 'H');
DefMacro('\celsius', '\ensuremath{{}^{\circ}\mathrm{C}}');
DefMacro('\lumen', 'lm');   DefMacro('\lux', 'lx');
DefMacro('\becquerel', 'Bq'); DefMacro('\gray', 'Gy');
DefMacro('\sievert', 'Sv'); DefMacro('\katal', 'kat');
DefMacro('\percent', '\%'); DefMacro('\degree', '\ensuremath{{}^{\circ}}');
DefMacro('\arcminute', '\ensuremath{{}^{\prime}}');
DefMacro('\arcsecond', '\ensuremath{{}^{\prime\prime}}');
DefMacro('\minute', 'min'); DefMacro('\hour', 'h');
DefMacro('\day', 'd');      DefMacro('\litre', 'L');
DefMacro('\liter', 'L');    DefMacro('\tonne', 't');
DefMacro('\electronvolt', 'eV');
DefMacro('\bel', 'B');      DefMacro('\decibel', 'dB');
DefMacro('\byte', 'B');     DefMacro('\bit', 'bit');

#======================================================================
1;
"""


def patch_source_tree(work_dir: Path) -> None:
    """Drop the shim into `work_dir` (the directory latexmlc is actually
    run from) unless something's already there -- a paper that bundles
    its own siunitx.sty[.ltxml] for reproducibility should keep using it
    untouched rather than have this silently override it."""
    target = work_dir / "siunitx.sty.ltxml"
    if target.exists() or (work_dir / "siunitx.sty").exists():
        return
    target.write_text(_LTXML_SHIM, encoding="utf-8")
