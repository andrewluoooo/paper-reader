"""
Override LaTeXML's built-in acronym.sty.ltxml.

The stock binding declares \\ifAC@nolist but never processes the package
option and always emits a visible <ltx:glossary> for the `{acronym}`
environment. Papers that use `\\usepackage[nolist]{acronym}` (the common
case -- define acronyms for \\ac/\\acs/\\acl without printing a list) then
get a full acronym DL dumped into the HTML, typically right next to the
Keywords block where `\\input{acro}` sits after \\maketitle.

This shim is the stock LaTeXML binding plus DeclareOption/ProcessOptions
so [nolist] actually sets \\AC@nolisttrue (the stock file declared the
conditional but never wired the option).

Suppressing the printed list inside LaTeXML's `{acronym}` environment
(e.g. switching to \\acrodef-only) hits a "Execution yielded non boxes"
error on \\end{acronym}, so the visible-list cleanup lives in
restyle._strip_acronym_lists instead. Inline \\ac/\\acs/\\acl expansion
is unchanged.
"""

from __future__ import annotations

from pathlib import Path

# Stock LaTeXML acronym.sty.ltxml (0.8.8), with option processing and a
# nolist-aware `{acronym}` environment. Marker comment is how
# patch_source_tree recognizes "our" shim on reconvert.
_LTXML_SHIM = r"""# -*- mode: Perl -*-
# paper_reader/acronym_patch.py -- shadows LaTeXML's acronym.sty.ltxml
package LaTeXML::Package::Pool;
use strict;
use warnings;
use LaTeXML::Package;

#======================================================================
DefMacro('\acsfont{}',  '#1');
DefMacro('\acffont{}',  '#1');
DefMacro('\acfsfont{}', '#1');

DefConditional('\ifAC@footnote');
DefConditional('\ifAC@nohyperlinks');
DefConditional('\ifAC@printonlyused');
DefConditional('\ifAC@withpage');
DefConditional('\ifAC@smaller');
DefConditional('\ifAC@dua');    # dua = don't use acronyms
DefConditional('\ifAC@nolist');
DefConditional('\ifAC@starred');

# Stock binding declared these conditionals but never wired the package
# options -- without this, [nolist] is a no-op and the list always prints.
DeclareOption('footnote',      sub { Digest(T_CS('\AC@footnotetrue')); });
DeclareOption('nohyperlinks',  sub { Digest(T_CS('\AC@nohyperlinkstrue')); });
DeclareOption('printonlyused', sub { Digest(T_CS('\AC@printonlyusedtrue')); });
DeclareOption('withpage',      sub { Digest(T_CS('\AC@withpagetrue')); });
DeclareOption('smaller',       sub { Digest(T_CS('\AC@smallertrue')); });
DeclareOption('dua',           sub { Digest(T_CS('\AC@duatrue')); });
DeclareOption('nolist',        sub { Digest(T_CS('\AC@nolisttrue')); });
DeclareOption(undef, sub {
  PassOptions('acronym', 'sty', ToString(Digest(T_CS('\CurrentOption')))); });
ProcessOptions();

DefMacro('\AC@placelabel{}', '');
#======================================================================
DefPrimitive('\lx@AC@used{}', sub {
    AssignValue('ACROUSED@' . ToString($_[1]) => 1, 'global'); });
DefPrimitive('\AC@logged{}', sub { });

DefMacro('\acused{}',      '\AC@logged{#1}');
DefMacro('\acronymused{}', '\AC@logged{#1}');
DefMacro('\acresetall',    '');

DefMacro('\lx@AC@if{}{}{}', sub {
    my ($gullet, $id, $short, $long) = @_;
    my $key = 'ACROUSED@' . ToString($_[1]);
    if (LookupValue($key)) {
      $short->unlist; }
    else {
      AssignValue($key => 1, 'global');
      $long->unlist; } });

#======================================================================
DefConstructor('\lx@acronym Undigested {}{}{}',
  "<ltx:glossaryref key='#2' inlist='#3' show='#4'/>",
  reversion => '#1{#2}');

DefMacro('\AC@acs{}',  '\lx@acronym{\acs}{#1}{acronym}{short}');
DefMacro('\AC@acl{}',  '\lx@acronym{\acl}{#1}{acronym}{long}');
DefMacro('\AC@acsp{}', '\lx@acronym{\acsp}{#1}{acronym}{short-plural}');
DefMacro('\AC@aclp{}', '\lx@acronym{\aclp}{#1}{acronym}{long-plural}');
DefMacro('\AC@acsi{}', '\lx@acronym{\acsi}{#1}{acronym}{short-indefinite}');
DefMacro('\AC@aclI{}', '\lx@acronym{\aclI}{#1}{acronym}{long-indefinite}');

DefMacro('\acs OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\acsa');
DefMacro('\acsa{}',              '\@acs{#1}');
DefMacro('\@acs{}',              '\acsfont{\AC@acs{#1}}\ifAC@starred\else\AC@logged{#1}\fi');

DefMacro('\acl OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\@acl');
DefMacro('\@acl{}',              '\acsfont{\AC@acl{#1}}\ifAC@starred\else\AC@logged{#1}\fi');

DefMacro('\acf OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\acfa');
DefMacro('\acfa{}',              '\@acf{#1}');
DefMacro('\@acf{}',
  '\ifAC@footnote\acsfont{\AC@acs{#1}}\footnote{\AC@placelabel{#1} \AC@acl{#1}}
  \else\acffont{\AC@placelabel{#1} \AC@acl{#1} \acfsfont{(\acsfont{\AC@acs{#1}})}}\fi
  \ifAC@starred\else\lx@AC@used{#1}\fi');

DefMacro('\acfi OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\acfia');
DefMacro('\acfia{}',              '{\itshape\AC@acl{#1} }(\ifAC@starred\acs*{#1}\else\acs{#1}\fi)');

DefMacro('\ac OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\@ac');
DefMacro('\@ac{}',
  '\lx@AC@if{#1}{\ifAC@starred\acs*{#1}\else\acs{#1}\fi}{\ifAC@starred\acf*{#1}\else\acf{#1}\fi}');

DefMacro('\iac OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\@iac');
DefMacro('\Iac OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\@Iac');

DefMacro('\@iac{}', '\@iaci{#1} \ifAC@starred\ac*{#1}\else\ac{#1}\fi');
DefMacro('\@Iac{}', '\@firstupper{\@iaci{#1}} \ifAC@starred\ac*{#1}\else\ac{#1}\fi');

DefMacro('\acsp OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\acspa');
DefMacro('\acspa{}',              '\@acsp{#1}');
DefMacro('\@acsp{}',              '\acsfont{\AC@acsp{#1}}\ifAC@starred\else\AC@logged{#1}\fi');

DefMacro('\aclp OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\@aclp');
DefMacro('\@aclp{}',              '\AC@aclp{#1}\ifAC@starred\else\AC@logged{#1}\fi');

DefMacro('\acfp OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\acfpa');
DefMacro('\acfpa{}',              '\@acfp{#1}');
DefMacro('\@acfp{}',
  '\ifAC@footnote\acsfont{\AC@acsp{#1}}\footnote{\AC@placelabel{#1} \AC@aclp{#1}}
 \else\acffont{\AC@placelabel{#1} \AC@aclp{#1} \acfsfont{(\acsfont{\AC@acsp{#1}})}}\fi
  \ifAC@starred\else\AC@logged{#1}\fi');

DefMacro('\acp OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\@acp');
DefMacro('\@acp{}', '\lx@AC@if{#1}{\AC@acsp{#1}}{\AC@aclp{#1}}\ifAC@starred\else\AC@logged{#1}\fi');

DefMacro('\acsu OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\acsua');
DefMacro('\acsua{}',              '\ifAC@starred\acs*{#1}\else\acs{#1}\fi\acused{#1}');
DefMacro('\aclu OptionalMatch:*', '\ifx.#1.\AC@starredfalse\else\AC@starredtrue\fi\aclua');
DefMacro('\aclua{}',              '\ifAC@starred\acl*{#1}\else\acl{#1}\fi\acused{#1}');

#======================================================================
# Defining Acronyms
# Keep stock list emission -- suppressing it here breaks \end{acronym}
# (non-boxes). restyle._strip_acronym_lists removes the printed DL.
DefEnvironment('{acronym}[]',
  "<ltx:glossary lists='acronym' class='ltx_acronym'>"
    . "<ltx:glossarylist>"
    . "#body"
    . "</ltx:glossarylist>"
    . "</ltx:glossary>",
  beforeDigest => sub { Let('\acro', '\lx@acro@item');
    Let('\acrodef', '\lx@acro@item'); },
  afterDigest     => sub { noteBackmatterElement($_[1], 'ltx:glossary'); },
  beforeConstruct => sub { adjustBackmatterElement($_[0], $_[1]); });

DefMacro('\acroextra{}', '#1');

DefMacro('\lx@acro@item{}[]{}',
  '\lx@acro@@item{#1}{\ifx.#2.#1\else#2\fi}{#3}');
DefMacro('\lx@acro@@item{}{}{}',
  '\lx@acro@@@item{#1}{#2}{#3}{{\let\acroextra\@gobble #2}}{{\let\acroextra\@gobble #3}}');
DefConstructor('\lx@acro@@@item{}{}{}{}{}',
  "<ltx:glossaryentry inlist='acronym' key='#1'>"
    . "<ltx:glossaryphrase role='label'>#2</ltx:glossaryphrase>"
    . "<ltx:glossaryphrase role='short'>#4</ltx:glossaryphrase>"
    . "<ltx:glossaryphrase role='long'>#5</ltx:glossaryphrase>"
    . "<ltx:glossaryphrase role='definition'>#3</ltx:glossaryphrase>"
    . "</ltx:glossaryentry>");
Tag('ltx:glossaryentry', afterClose => sub { GenerateID(@_, ''); });

DefMacro('\acrodef{}[]{}',
  '\lx@acro@@def{#1}{\ifx.#2.#1\else#2\fi}{#3}');
DefMacro('\lx@acro@@def{}{}{}',
  '\lx@acro@@@def{#1}{#2}{#3}{{\let\acroextra\@gobble #2}}{{\let\acroextra\@gobble #3}}');
DefConstructor('\lx@acro@@@def{}{}{}{}{}',
  "<ltx:glossarydefinition inlist='acronym' key='#1'>"
    . "<ltx:glossaryphrase role='label'>#2</ltx:glossaryphrase>"
    . "<ltx:glossaryphrase role='short'>#4</ltx:glossaryphrase>"
    . "<ltx:glossaryphrase role='long'>#5</ltx:glossaryphrase>"
    . "<ltx:glossaryphrase role='definition'>#3</ltx:glossaryphrase>"
    . "</ltx:glossarydefinition>");

Tag('ltx:glossarydefinition', afterClose => sub { GenerateID(@_, ''); });

Let('\newacro', '\acrodef');
Let('\acro',    '\acrodef');

DefMacro('\lx@acro@phrase{}{}{}', '{\let\acroextra\@gobble\lx@@acro@phrase{#1}{#2}{#3}}');
DefConstructor('\lx@@acro@phrase{}{}{}',
  "^ <ltx:glossarydefinition inlist='acronym' key='#1'>"
    . "<ltx:glossaryphrase role='#2'>#3</ltx:glossaryphrase>"
    . "</ltx:glossarydefinition>");

DefMacro('\acrodefindefinite{}{}{}',
  '\lx@acro@phrase{#1}{short-indefinite}{#2}\lx@acro@phrase{#1}{long-indefinite}{#3}');

Let('\acroindefinite',    '\acrodefindefinite');
Let('\newacroindefinite', '\acrodefindefinite');

DefMacro('\acrodefplural{}[]{}',
  '\lx@acro@phrase{#1}{short-plural}{\ifx.#2.#1\else#2\fi}\lx@acro@phrase{#1}{long-plural}{#3}');

Let('\acroplural',    '\acrodefplural');
Let('\newacroplural', '\acrodefplural');
#======================================================================
1;
"""

_SHIM_MARKER = "paper_reader/acronym_patch.py"


def patch_source_tree(work_dir: Path) -> None:
    """Drop the shim into `work_dir` (latexmlc's cwd) so it shadows
    LaTeXML's built-in acronym.sty.ltxml. Leave a paper-bundled binding
    untouched; overwrite our own shim on reconvert so binding fixes apply.
    """
    target = work_dir / "acronym.sty.ltxml"
    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="ignore")
        if _SHIM_MARKER not in existing:
            return  # paper-bundled binding -- don't clobber
    target.write_text(_LTXML_SHIM, encoding="utf-8")
