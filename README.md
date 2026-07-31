# paper-reader

Convert arXiv-style LaTeX papers into a single-column, Readwise-Reader-style
HTML page (via [LaTeXML](https://dlmf.nist.gov/LaTeXML/), the same converter
arXiv itself uses for its HTML views), and read/highlight/annotate them
through a small local library web app.

Features: light/dark/auto theme, adjustable text size/font/column width,
resizable/collapsible sidebars, click-to-highlight with optional notes,
citation/figure/table hover previews, and Markdown export of your
highlights.

## Prerequisites

- Python 3.9+
- [LaTeXML](https://dlmf.nist.gov/LaTeXML/) (`latexmlc`)
- [Poppler](https://poppler.freedesktop.org/) (`pdftoppm`)
- [Ghostscript](https://www.ghostscript.com/) (`gs`)

On macOS, via Homebrew:

```bash
brew install latexml poppler ghostscript
```

Some papers use the `algorithm`/`algorithmic` LaTeX packages, which aren't
always preinstalled. If a conversion fails complaining about
`algorithmic.sty`, install it into your user TeX tree (no sudo needed):

```bash
tlmgr --usermode install algorithms
```

## Install

Clone the repo and install it as a command-line tool with
[pipx](https://pipx.pypa.io/) (recommended -- gives you a global
`paper-reader` command without touching your system Python):

```bash
git clone https://github.com/andrewluoooo/paper-reader.git
cd paper-reader
pipx install --editable .
```

`--editable` means future `git pull`s pick up new code immediately, no
reinstall needed.

Alternatively, install into a virtualenv with pip:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

**Library mode** -- a local web app where you drag and drop LaTeX sources
(`.tex`, `.zip`, `.tar.gz`, `.tgz`, `.tar` -- e.g. an arXiv "Other formats ->
Source" download) and get a searchable library of reader pages:

```bash
paper-reader --library
```

Opens `http://127.0.0.1:8765` in your browser. Papers are stored in
`~/.paper_reader_library`.

```bash
paper-reader --library --port 9000     # use a different port
paper-reader --library --no-browser    # don't auto-open a browser tab
paper-reader --rebuild-library         # re-render every saved paper with the
                                        # current styling, then exit
```

**One-shot mode** -- convert a single paper straight to an HTML file:

```bash
paper-reader path/to/paper.tar.gz -o paper.html
```

## Notes

- Highlights and notes are stored in your browser's `localStorage`, keyed
  per paper -- they live in whichever browser you read the paper in, not on
  the server or in any file.
- The library server is a plain `http.server` app meant for local/personal
  use (e.g. over Tailscale) -- there's no authentication.
