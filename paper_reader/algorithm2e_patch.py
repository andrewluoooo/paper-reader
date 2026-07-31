"""
LaTeXML has no binding for the `algorithm2e` package, so any paper using
it (a common choice for algorithm pseudocode) loses its \\KwIn/\\While/
\\For/\\If/\\Return keywords and nested indentation entirely -- they just
vanish as "undefined macro" errors.

LaTeXML *does* have solid, well-tested support for the older, far more
common `algorithm` + `algorithmic` packages (once those are actually
installed -- see latex_convert.py's caller). Rather than trying to patch
around the missing macros after the fact (the keyword text is simply
gone from the output by then), we transpile each algorithm2e body into
equivalent `algorithmic` syntax -- \\WHILE/\\ENDWHILE, \\FOR/\\ENDFOR,
\\IF/\\ENDIF, \\RETURN -- *before* invoking LaTeXML, so the real package
produces the real keywords with real nested indentation, i.e. what the
paper actually intended to show. \\KwIn/\\KwOut are rendered as plain
"Input:"/"Output:" text ahead of the environment rather than mapped to
algorithmic's own \\REQUIRE/\\ENSURE, which triggers a stray line-number
artifact in this LaTeXML binding.

This only handles the algorithm2e subset that's realistic to see in a
paper's pseudocode (\\While, \\For, \\ForEach, \\If, \\eIf, \\Repeat,
\\Return, \\KwIn, \\KwOut, \\tcp, \\tcp*, \\KwTo, \\SetKw-defined
keywords). Anything the transpiler doesn't recognize is passed through
unchanged, and if brace-matching ever fails outright the affected
`algorithm` environment is left untouched rather than risking a broken
build.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

USEPACKAGE_ALGORITHM2E_RE = re.compile(r"\\usepackage(?:\s*\[[^\]]*\])?\{algorithm2e\}")
ALGORITHM_ENV_RE = re.compile(r"\\begin\{algorithm\}(.*?)\\end\{algorithm\}", re.S)

# command -> (BEGIN keyword, END keyword) for algorithmic's block constructs
_BLOCK_CMDS = {
    "While": ("WHILE", "ENDWHILE"),
    "For": ("FOR", "ENDFOR"),
    "ForEach": ("FORALL", "ENDFOR"),
    "If": ("IF", "ENDIF"),
}
# command -> algorithmic command taking one argument, used as a full line
_SIMPLE_CMDS = {"Return": "RETURN"}
# bare keyword substitutions inside condition text (no arguments)
_KEYWORD_SUBS = {r"\KwTo": r"\TO", r"\KwOr": r"\OR", r"\KwAnd": r"\AND"}

PREAMBLE_SHIM = (
    "\\usepackage{algorithm}\n"
    "\\usepackage{algorithmic}\n"
    "\\renewcommand{\\algorithmiccomment}[1]{\\ \\textcolor{gray}{$\\triangleright$ #1}}\n"
    "\\providecommand{\\SetKw}[2]{\\expandafter\\newcommand\\csname #1\\endcsname{\\textbf{#2}}}\n"
)


class _BraceError(ValueError):
    pass


_SIZE_CMD_RE = re.compile(
    r"\\(tiny|scriptsize|footnotesize|small|normalsize|large|Large|LARGE|huge|Huge)(?![A-Za-z])\s*"
)


def _strip_latex_comments(text: str) -> str:
    """Drop everything from an unescaped `%` to end of line, so
    commented-out pseudocode (e.g. a leftover duplicate `\\Return`) never
    reaches the transpiler."""
    out_lines = []
    for line in text.split("\n"):
        buf = []
        i = 0
        while i < len(line):
            if line[i] == "\\" and i + 1 < len(line):
                buf.append(line[i : i + 2])
                i += 2
                continue
            if line[i] == "%":
                break
            buf.append(line[i])
            i += 1
        out_lines.append("".join(buf))
    return "\n".join(out_lines)


def _extract_float_frontmatter(text: str) -> tuple[str, str, str]:
    """Pull `\\caption{...}`, `\\label{...}`, bare font-size switches
    (`\\scriptsize` etc.), and `\\KwIn`/`\\KwOut` out of an algorithm body.

    caption/label/size are float/font metadata and must stay outside the
    `algorithmic` environment rather than being swallowed into a
    `\\STATE`. KwIn/KwOut (Input:/Output:) are rendered as plain text
    right before the environment rather than mapped to algorithmic's own
    \\REQUIRE/\\ENSURE, which -- at least in this LaTeXML binding --
    prints a stray, meaningless "0:" line-number tag next to them.

    Returns (float_frontmatter, io_text, remaining_body).
    """
    frontmatter: list[str] = []
    io_lines: list[str] = []

    def pull_command(s: str, name: str) -> tuple[str, Optional[str]]:
        pattern = re.compile(r"\\" + name + r"\s*\{")
        m = pattern.search(s)
        if not m:
            return s, None
        open_idx = m.end() - 1
        close_idx = _match_brace(s, open_idx)
        arg = s[open_idx + 1 : close_idx]
        return s[: m.start()] + s[close_idx + 1 :], arg

    text, caption_arg = pull_command(text, "caption")
    if caption_arg is not None:
        frontmatter.append(f"\\caption{{{caption_arg}}}")
    text, label_arg = pull_command(text, "label")
    if label_arg is not None:
        frontmatter.append(f"\\label{{{label_arg}}}")

    text, kwin_arg = pull_command(text, "KwIn")
    if kwin_arg is not None:
        io_lines.append(f"\\noindent\\textbf{{Input: }}{kwin_arg}\\par")
    text, kwout_arg = pull_command(text, "KwOut")
    if kwout_arg is not None:
        io_lines.append(f"\\noindent\\textbf{{Output: }}{kwout_arg}\\par")

    size_match = _SIZE_CMD_RE.search(text)
    if size_match:
        frontmatter.insert(0, "\\" + size_match.group(1))
        text = text[: size_match.start()] + text[size_match.end() :]

    return "\n".join(frontmatter), "\n".join(io_lines), text


def _match_brace(s: str, open_idx: int) -> int:
    """Index of the `}` matching the `{` at `s[open_idx]`."""
    if s[open_idx] != "{":
        raise _BraceError(f"expected '{{' at {open_idx}")
    depth = 0
    i = open_idx
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            i += 2  # skip escaped/control chars so `\{`/`\}` don't confuse depth
            continue
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _BraceError("unbalanced braces")


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _apply_keyword_subs(text: str) -> str:
    for old, new in _KEYWORD_SUBS.items():
        text = text.replace(old, new)
    return text


def _transpile_body(text: str) -> str:
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)

    def flush_statement() -> None:
        seg = "".join(buf).strip()
        buf.clear()
        if seg:
            out.append(f"\\STATE {seg}\n")

    while i < n:
        ch = text[i]

        if ch == "\\" and text[i : i + 2] == "\\\\":
            flush_statement()
            i += 2
            continue

        if ch == "\\":
            m = re.match(r"\\([A-Za-z]+)(\*?)", text[i:])
            if m:
                cmd, star = m.group(1), m.group(2)
                cmd_len = len(m.group(0))
                j = _skip_ws(text, i + cmd_len)

                if cmd in _BLOCK_CMDS and j < n and text[j] == "{":
                    cond_end = _match_brace(text, j)
                    cond = _apply_keyword_subs(text[j + 1 : cond_end])
                    j2 = _skip_ws(text, cond_end + 1)
                    if j2 < n and text[j2] == "{":
                        body_end = _match_brace(text, j2)
                        body = text[j2 + 1 : body_end]
                        flush_statement()
                        begin_kw, end_kw = _BLOCK_CMDS[cmd]
                        out.append(f"\\{begin_kw}{{{cond}}}\n")
                        out.append(_transpile_body(body))
                        out.append(f"\\{end_kw}\n")
                        i = body_end + 1
                        continue

                if cmd == "eIf" and j < n and text[j] == "{":
                    cond_end = _match_brace(text, j)
                    cond = _apply_keyword_subs(text[j + 1 : cond_end])
                    j2 = _skip_ws(text, cond_end + 1)
                    if j2 < n and text[j2] == "{":
                        then_end = _match_brace(text, j2)
                        then_body = text[j2 + 1 : then_end]
                        j3 = _skip_ws(text, then_end + 1)
                        if j3 < n and text[j3] == "{":
                            else_end = _match_brace(text, j3)
                            else_body = text[j3 + 1 : else_end]
                            flush_statement()
                            out.append(f"\\IF{{{cond}}}\n")
                            out.append(_transpile_body(then_body))
                            out.append("\\ELSE\n")
                            out.append(_transpile_body(else_body))
                            out.append("\\ENDIF\n")
                            i = else_end + 1
                            continue

                if cmd == "Repeat" and j < n and text[j] == "{":
                    body_end = _match_brace(text, j)
                    body = text[j + 1 : body_end]
                    j2 = _skip_ws(text, body_end + 1)
                    if j2 < n and text[j2] == "{":
                        cond_end = _match_brace(text, j2)
                        cond = _apply_keyword_subs(text[j2 + 1 : cond_end])
                        flush_statement()
                        out.append("\\REPEAT\n")
                        out.append(_transpile_body(body))
                        out.append(f"\\UNTIL{{{cond}}}\n")
                        i = cond_end + 1
                        continue

                if cmd in _SIMPLE_CMDS and j < n and text[j] == "{":
                    arg_end = _match_brace(text, j)
                    arg = text[j + 1 : arg_end]
                    flush_statement()
                    out.append(f"\\{_SIMPLE_CMDS[cmd]}{{{arg}}}\n")
                    i = arg_end + 1
                    continue

                if cmd in ("tcp", "tcc") and j < n and text[j] == "{":
                    arg_end = _match_brace(text, j)
                    arg = text[j + 1 : arg_end]
                    if star:
                        # `\tcp*` is algorithm2e's *trailing* comment: it
                        # attaches to the current statement and implicitly
                        # ends the line itself, so the source doesn't (and
                        # shouldn't need to) follow it with its own `\\`.
                        buf.append(f"\\COMMENT{{{arg}}}")
                        flush_statement()
                    else:
                        flush_statement()
                        out.append(f"\\STATE \\COMMENT{{{arg}}}\n")
                    i = arg_end + 1
                    continue

        buf.append(ch)
        i += 1

    flush_statement()
    return "".join(out)


def _rewrite_one_algorithm(match: re.Match) -> str:
    try:
        inner = _strip_latex_comments(match.group(1))
        frontmatter, io_text, body = _extract_float_frontmatter(inner)
        transpiled = _transpile_body(body)
    except _BraceError:
        return match.group(0)  # leave the original untouched rather than risk a broken build
    parts = ["\\begin{algorithm}\n"]
    if frontmatter:
        parts.append(frontmatter + "\n")
    if io_text:
        parts.append(io_text + "\n")
    parts.append("\\begin{algorithmic}\n")
    parts.append(transpiled)
    parts.append("\\end{algorithmic}\n\\end{algorithm}")
    return "".join(parts)


def patch_source_tree(src_dir: Path) -> None:
    """If any .tex file under `src_dir` loads algorithm2e, swap it for
    `algorithm`+`algorithmic` (which LaTeXML actually supports) and
    transpile every `algorithm` environment's body to match, in place."""
    tex_files = list(src_dir.rglob("*.tex"))
    uses_algorithm2e = any(
        USEPACKAGE_ALGORITHM2E_RE.search(f.read_text(encoding="utf-8", errors="ignore"))
        for f in tex_files
    )
    if not uses_algorithm2e:
        return

    for f in tex_files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        new_text = USEPACKAGE_ALGORITHM2E_RE.sub(lambda m: PREAMBLE_SHIM, text)
        new_text = ALGORITHM_ENV_RE.sub(_rewrite_one_algorithm, new_text)
        if new_text != text:
            f.write_text(new_text, encoding="utf-8")
