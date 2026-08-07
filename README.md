# paper-reader

Convert arXiv-style LaTeX papers (via [LaTeXML](https://dlmf.nist.gov/LaTeXML/),
the same converter arXiv itself uses for its HTML views) -- or an
already-rendered HTML paper page saved from a publisher's site (ACM DL, IEEE
Xplore, and similar), a PDF, or an EPUB -- into a single-column,
Readwise-Reader-style HTML page, and read/highlight/annotate it through a
small local library web app.

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

See **[GUIDE.md](GUIDE.md)** for a short how-to (also available in the library
UI under **Guide** next to GitHub).

**Library mode** -- a local web app where you drag and drop LaTeX sources
(`.tex`, `.zip`, `.tar.gz`, `.tgz`, `.tar` -- e.g. an arXiv "Other formats ->
Source" download), a saved HTML paper page (`.html`/`.htm`), a plain
`.pdf`, or an `.epub`, and get a searchable library of reader pages:

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

**Account & vaults** -- anonymous secret-key sign-in by default. Each key
unlocks its own encrypted vault; a new key starts empty (papers are never
shared across keys). Legacy flat libraries are claimed once into the first
key that signs in or generates after upgrade. Only salts and a key hash are
kept in `accounts.json`. Export a plaintext ZIP from Preferences. Log out
from Preferences. To skip auth entirely:

```bash
export PAPER_READER_DISABLE_AUTH=1
paper-reader --library
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

Drop a `.pdf` on the library (or pass it to the CLI). Structure is extracted
by one of two backends (Preferences → **PDF Parser**):

- **Docling** (default) -- runs locally (`pip install docling`)
- **MinerU Cloud** -- free-tier [MinerU API](https://mineru.net/apiManage/docs).
  Create a token at https://mineru.net/user-center/api-token, paste it in
  Preferences (or `export MINERU_API_TOKEN=…`), then choose **MinerU Cloud**.
  The PDF is uploaded to MinerU; markdown + images come back into the same
  local restyle pipeline. Caps: 200 MB / ~200 pages per file; daily free
  high-priority page quota on their side.

PDF import is best-effort for scanned or unusual layouts.

### Importing an EPUB

Drop an `.epub` on the library (or pass it to the CLI) the same way as other
formats. The converter reads the package document (OPF) for title/authors and
spine reading order, concatenates the XHTML chapters, and maps them into the
same `ltx_*` HTML structure the LaTeX / HTML / PDF paths produce -- so you get
the same reflowable reader, theming, highlighting, and outline.

Images embedded in the book are preserved; a chapter that looks like a
references list is turned into the shared bibliography section when possible.
DRM-protected EPUBs and heavily scripted fixed-layout books won't convert.

## Notes

- Highlights and notes are stored in your browser's `localStorage`, keyed
  per paper -- they live in whichever browser you read the paper in, not on
  the server or in any file.
- The library server is a plain `http.server` app meant for local/personal
  use (e.g. over Tailscale). Sign-in uses an anonymous secret key by default;
  set `PAPER_READER_DISABLE_AUTH=1` if you want it open.
