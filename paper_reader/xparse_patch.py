"""
Same root cause as siunitx_patch.py/tcolorbox_patch.py -- LaTeXML's own
`xparse.sty.ltxml` binding has no real implementation, it just processes
the raw `xparse.sty`, which does `\\RequirePackage{expl3}`, which processes
the raw, 39,000-line `expl3-code.tex` kernel source as generic TeX macro
expansion and hits a genuine LaTeXML bug ("Negative argument for
\\prg_replicate:nn" inside expl3's Unicode codepoint-block setup). That
cascades into a many-minutes-long recoverable-error loop even when it
doesn't outright abort the conversion.

Checked whether there's a cheaper way around this: `expl3.pool.ltxml`
(LaTeXML's *other*, more substantial expl3-adjacent binding, autoloaded by
`\\ExplSyntaxOn`) loads a much smaller `expl3.ltx` (199 lines) instead of
`expl3-code.tex` (39,412 lines) and already contains real machinery for
`\\NewDocumentCommand`'s internals. That looked promising, but reading
`expl3.ltx` itself shows it unconditionally does `\\input expl3-code.tex`
(guarded only by "is expl3 already loaded", which it never is here) -- so
that path hits the exact same crash. No shortcut available; xparse's
argument-spec grammar has to be hand-translated to LaTeXML's own parameter
syntax.

That translation reuses the exact primitives LaTeXML's *own*
`\\newcommand`/`\\newenvironment` are built from (see `\\newcommand` and
`\\newenvironment` in LaTeXML's bundled LaTeX.pool.ltxml): `DefMacroI` to
install the new macro, and LaTeXML's own `{}`/`[]`/`[Default:...]`/
`OptionalMatch:*` parameter-spec syntax (fed through `parseParameters`) in
place of xparse's own `m`/`o`/`O{default}`/`s` argument-spec letters. An
environment defined via `\\NewDocumentEnvironment` is, structurally,
nothing more than a `\\name` macro (holding the begin-body, with xparse's
args) plus a parameterless `\\endname` macro (the end-body) -- LaTeXML's
own `\\newenvironment` binding does exactly this, so xparse's environment
commands do the same.

Supported argument-spec letters: `m` (mandatory), `o`/`g` (optional,
bracket-delimited), `s` (optional star), `t<char>` (optional single-token
match), `O{default}`/`G{default}` (optional with a literal default).
That covers the large majority of how xparse actually shows up in papers
(custom notation macros built with a handful of mandatory/optional args,
occasionally starred). The rarer letters (`d`/`D`/`r`/`R` custom-delimiter
arguments, `v` verbatim, `l`/`u` token-list arguments) fall back to being
treated as an ordinary mandatory `{}` argument -- not faithful to xparse's
real semantics for those, but it keeps the definition (and everything
after it) from breaking outright, which is what actually matters for
rendering a paper.

`\\IfBooleanTF`/`\\IfNoValueTF`/`\\IfValueTF` and their T/F-only variants
are implemented via a plain `\\ifx`-against-`\\@empty` check, since our
"optional argument, not given" and "boolean argument, star absent" both
simply produce an empty argument here (rather than xparse's real internal
-NoValue- sentinel) -- close enough for the common
`\\IfBooleanTF{#1}{starred}{unstarred}`-style bodies actually seen in
papers.
"""

from __future__ import annotations

from pathlib import Path

_LTXML_SHIM = r"""# -*- mode: Perl -*-
# Minimal, non-crashing replacement for xparse.sty.ltxml -- see
# paper_reader/xparse_patch.py for why this exists.
package LaTeXML::Package::Pool;
use strict;
use warnings;
use LaTeXML::Package;

# Translate an xparse argument-spec string (e.g. "s m o O{default}") into
# a LaTeXML prototype-fragment string (e.g. "OptionalMatch:* {} [] [Default:x]")
# by building the equivalent fragment for each recognized letter. DefMacroI
# parses a plain string paramlist itself (via its own internal call to
# parseParameters, same as \newcommand's convertLaTeXArgs ultimately feeds
# into) -- parseParameters isn't itself exported to the Pool namespace, so
# we hand DefMacroI the string and let it do that parsing.
sub xparse_argspec_to_params {
  my ($spec) = @_;
  $spec = '' unless defined $spec;
  my @chars = split //, $spec;
  my $n = scalar(@chars);
  my $i = 0;
  my @parts = ();
  while ($i < $n) {
    my $c = $chars[$i];
    if ($c =~ /\s/) { $i++; next; }
    if ($c eq 'm' || $c eq 'M') {
      push(@parts, '{}');
      $i++; }
    elsif ($c eq 'o' || $c eq 'g') {
      push(@parts, '[]');
      $i++; }
    elsif ($c eq 's' || $c eq 'S') {
      push(@parts, 'OptionalMatch:*');
      $i++; }
    elsif ($c eq 't' || $c eq 'T') {
      $i++;
      if ($i < $n) {
        push(@parts, 'OptionalMatch:' . $chars[$i]);
        $i++; }
      else {
        push(@parts, 'OptionalMatch:*'); } }
    elsif ($c eq 'O' || $c eq 'G') {
      $i++;
      if (($i < $n) && ($chars[$i] eq '{')) {
        my $depth   = 1;
        my $j       = $i + 1;
        my $default = '';
        while (($j < $n) && ($depth > 0)) {
          if    ($chars[$j] eq '{') { $depth++; }
          elsif ($chars[$j] eq '}') { $depth--; last if $depth == 0; }
          $default .= $chars[$j] if $depth > 0;
          $j++; }
        push(@parts, '[Default:' . $default . ']');
        $i = $j + 1; }
      else {
        push(@parts, '[]'); } }
    else {
      # Unsupported specifier (d/D/r/R custom-delimiter args, v verbatim,
      # l/u token-list args, ...): fall back to treating it as an ordinary
      # mandatory argument, so the definition still succeeds instead of
      # silently dropping arguments or misfiring downstream.
      $i++;
      if (($i < $n) && ($chars[$i] eq '{')) {    # e.g. R<>{default}
        my $depth = 1;
        my $j     = $i + 1;
        while (($j < $n) && ($depth > 0)) {
          if    ($chars[$j] eq '{') { $depth++; }
          elsif ($chars[$j] eq '}') { $depth--; }
          $j++; }
        $i = $j; }
      push(@parts, '{}'); } }
  my $protostr = join(' ', @parts);
  return ($protostr ? $protostr : undef);
}

DefPrimitive('\NewDocumentCommand DefToken {}{}', sub {
    my ($stomach, $cs, $argspec, $body) = @_;
    DefMacroI($cs, xparse_argspec_to_params(ToString($argspec)), $body);
    return; });
DefPrimitive('\RenewDocumentCommand DefToken {}{}', sub {
    my ($stomach, $cs, $argspec, $body) = @_;
    DefMacroI($cs, xparse_argspec_to_params(ToString($argspec)), $body);
    return; });
DefPrimitive('\ProvideDocumentCommand DefToken {}{}', sub {
    my ($stomach, $cs, $argspec, $body) = @_;
    return if IsDefined($cs);
    DefMacroI($cs, xparse_argspec_to_params(ToString($argspec)), $body);
    return; });
DefPrimitive('\DeclareDocumentCommand DefToken {}{}', sub {
    my ($stomach, $cs, $argspec, $body) = @_;
    DefMacroI($cs, xparse_argspec_to_params(ToString($argspec)), $body);
    return; });

DefPrimitive('\NewDocumentEnvironment {}{}{}{}', sub {
    my ($stomach, $name, $argspec, $begin, $end) = @_;
    $name = ToString(Expand($name));
    DefMacroI(T_CS("\\$name"), xparse_argspec_to_params(ToString($argspec)), $begin);
    DefMacroI(T_CS("\\end$name"), undef, $end);
    return; });
DefPrimitive('\RenewDocumentEnvironment {}{}{}{}', sub {
    my ($stomach, $name, $argspec, $begin, $end) = @_;
    $name = ToString(Expand($name));
    DefMacroI(T_CS("\\$name"), xparse_argspec_to_params(ToString($argspec)), $begin);
    DefMacroI(T_CS("\\end$name"), undef, $end);
    return; });
DefPrimitive('\ProvideDocumentEnvironment {}{}{}{}', sub {
    my ($stomach, $name, $argspec, $begin, $end) = @_;
    $name = ToString(Expand($name));
    return if IsDefined(T_CS("\\$name"));
    DefMacroI(T_CS("\\$name"), xparse_argspec_to_params(ToString($argspec)), $begin);
    DefMacroI(T_CS("\\end$name"), undef, $end);
    return; });
DefPrimitive('\DeclareDocumentEnvironment {}{}{}{}', sub {
    my ($stomach, $name, $argspec, $begin, $end) = @_;
    $name = ToString(Expand($name));
    DefMacroI(T_CS("\\$name"), xparse_argspec_to_params(ToString($argspec)), $begin);
    DefMacroI(T_CS("\\end$name"), undef, $end);
    return; });

# xparse also exposes "Expandable" variants (restricted to expansion-safe
# bodies in real TeX); LaTeXML doesn't distinguish that from the ordinary
# case for our purposes, so just alias them.
Let('\NewExpandableDocumentCommand',     '\NewDocumentCommand');
Let('\RenewExpandableDocumentCommand',   '\RenewDocumentCommand');
Let('\ProvideExpandableDocumentCommand', '\ProvideDocumentCommand');
Let('\DeclareExpandableDocumentCommand', '\DeclareDocumentCommand');

RawTeX(<<'EOTeX');
\def\@xp@ifempty#1{\def\@xp@tmp{#1}\ifx\@xp@tmp\@empty}
\newcommand{\IfBooleanTF}[3]{\@xp@ifempty{#1}#3\else#2\fi}
\newcommand{\IfBooleanT}[2]{\@xp@ifempty{#1}\else#2\fi}
\newcommand{\IfBooleanF}[2]{\@xp@ifempty{#1}#2\fi}
\newcommand{\IfNoValueTF}[3]{\@xp@ifempty{#1}#2\else#3\fi}
\newcommand{\IfNoValueT}[2]{\@xp@ifempty{#1}#2\fi}
\newcommand{\IfNoValueF}[2]{\@xp@ifempty{#1}\else#2\fi}
\newcommand{\IfValueTF}[3]{\@xp@ifempty{#1}#3\else#2\fi}
\newcommand{\IfValueT}[2]{\@xp@ifempty{#1}\else#2\fi}
\newcommand{\IfValueF}[2]{\@xp@ifempty{#1}#2\fi}
EOTeX

#======================================================================
1;
"""


def patch_source_tree(work_dir: Path) -> None:
    """Drop the shim into `work_dir` (the directory latexmlc is actually
    run from) unless something's already there -- a paper that bundles
    its own xparse.sty[.ltxml] for reproducibility should keep using it
    untouched rather than have this silently override it."""
    target = work_dir / "xparse.sty.ltxml"
    if target.exists() or (work_dir / "xparse.sty").exists():
        return
    target.write_text(_LTXML_SHIM, encoding="utf-8")
