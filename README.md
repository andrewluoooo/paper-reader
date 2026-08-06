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

Opens `http://127.0.0.1:8765` in your browser and runs in the background, returning control to your terminal immediately. If the server is already running, it opens a browser tab without spawning a second instance. Papers are stored in `~/.paper_reader_library`.

```bash
paper-reader --library --port 9000     # use a different port
paper-reader --library --no-browser    # don't auto-open a browser tab
paper-reader --library --foreground    # run in foreground (hold terminal prompt)
paper-reader --stop-library            # stop the background library server
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
it on the library (or pass it to the CLI) directly. PDF structure (title,
authors, section headings, abstract, in-text citations, and the
bibliography) is extracted by [GROBID](https://github.com/kermitt2/grobid),
a machine-learning service purpose-built for parsing scholarly PDFs, then
mapped into the same structure the LaTeX and HTML paths produce. The
resulting page gets the same reflowable text, theming, highlighting, outline,
and citation hover-previews as any other paper in the library -- including
real, clickable citation links, since GROBID resolves in-text references
against its own parsed bibliography.

**GROBID has to be running first.** It's a separate local service, not a
Python dependency -- the easiest way to run it is with
[Colima](https://github.com/abiosoft/colima) (a lightweight, GUI-free Docker
runtime for macOS):

```bash
brew install colima docker
colima start --cpu 4 --memory 4
docker run --rm -d -p 8070:8070 --name grobid grobid/grobid:0.9.0-crf
```

That `-crf` tag is the CRF-only build (~500MB) rather than the `-full` build
(~10GB) that also bundles GROBID's optional deep-learning models -- the CRF
models are what GROBID uses by default either way, so unless you've got a
GPU and specifically want the deep-learning models, `-crf` gets you the same
extraction quality for a much smaller download.

The first `docker run` still pulls an image, so give it a minute or two.
After that, `docker start grobid` / `docker stop grobid` bring it back up or
down without pulling again. paper-reader looks for GROBID at
`http://localhost:8070` by default; point it elsewhere with the `GROBID_URL`
environment variable if you're running it on a different host or port. If
GROBID isn't reachable when you drop a PDF, you'll get a clear error telling
you to start it.

This is still best-effort -- scanned-image PDFs with no real text layer, or
unusual layouts GROBID doesn't recognize well, may extract incompletely.
Figures and tables come through as captions only (no embedded images).

## Notes

- Highlights and notes are stored in your browser's `localStorage`, keyed
  per paper -- they live in whichever browser you read the paper in, not on
  the server or in any file.
- The library server is a plain `http.server` app meant for local/personal
  use (e.g. over Tailscale) -- there's no authentication.
