# paper-reader

Convert arXiv-style LaTeX papers (via [LaTeXML](https://dlmf.nist.gov/LaTeXML/),
the same converter arXiv itself uses for its HTML views) -- or an
already-rendered HTML paper page saved from a publisher's site (ACM DL, IEEE
Xplore, and similar) -- into a single-column, Readwise-Reader-style HTML page,
and read/highlight/annotate it through a small local library web app.

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
Source" download), a saved HTML paper page (`.html`/`.htm`), or a plain
`.pdf`, and get a searchable library of reader pages:

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
paper-reader path/to/saved-paper-page.html -o paper.html
```

### Importing an HTML paper page

For papers you only have as a publisher's web page rather than a LaTeX
source: in your browser, open the paper and use **Save Page As... ->
Webpage, Complete** (this saves the HTML plus an assets folder next to it),
then feed that saved `.html` file to `paper-reader` -- either via the CLI or
by dropping it on the library's home page.

paper-reader deliberately reads a file you've already saved rather than
fetching the URL itself. Many publisher sites (ACM DL among them) sit behind
bot-detection challenges that it would be inappropriate to try to script
around; saving the page yourself means whatever subscription/institutional
access your own browser session has already applies, same as reading it
normally.

The HTML parser tries a couple of extraction strategies to stay reasonably
publisher-agnostic (JATS-style landmark IDs used by ACM/IEEE/Springer-style
platforms, falling back to general-purpose readability extraction for
everything else), but it's still a best-effort heuristic -- unusual page
layouts may not extract perfectly. Author names in particular depend on the
page having rendered them server-side into the static HTML; some publisher
pages load the author list via client-side JavaScript after the fact, which
won't be present in a saved snapshot.

### Importing a PDF

When you don't have a LaTeX source or an HTML page -- just a `.pdf` -- drop
it on the library (or pass it to the CLI) directly. Unlike the LaTeX and
HTML paths, a PDF carries no semantic markup at all, so the title, authors,
section headings, abstract, and references are reconstructed from layout:
font size, boldness, and position on the page, with a two-column-aware
reading order for the common academic two-column layout. The resulting page
gets the same reflowable text, theming, and highlighting as any other paper
in the library.

This is inherently more heuristic than the LaTeX or HTML paths -- unusual
layouts, single-column papers with atypical heading styles, or PDFs that are
scanned images without a real text layer may not extract cleanly. Author
names are a rough split of whatever text sits directly under the title, with
no affiliation/email parsing.

## Notes

- Highlights and notes are stored in your browser's `localStorage`, keyed
  per paper -- they live in whichever browser you read the paper in, not on
  the server or in any file.
- The library server is a plain `http.server` app meant for local/personal
  use (e.g. over Tailscale) -- there's no authentication.
