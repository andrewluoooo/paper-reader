# paper-reader guide

A local library for reading research papers as clean, reflowable HTML.

## Start

```bash
paper-reader --library
```

Opens http://127.0.0.1:8765. Papers live in `~/.paper_reader_library`.

```bash
paper-reader --stop-library    # stop the server
paper-reader --rebuild-library # restyle every saved paper
```

## Add papers

Use the **+** button (bottom-right), or drag a file onto the library page.

| Format | Notes |
|--------|--------|
| `.tex` / `.zip` / `.tar.gz` | LaTeX source (e.g. arXiv “Other formats → Source”) |
| `.html` / `.htm` | Saved publisher page (Save Page As → Webpage, Complete) |
| `.pdf` | Needs [GROBID](https://github.com/kermitt2/grobid) running locally |
| `.epub` | Ebook → same reader format |

One-shot CLI (no library):

```bash
paper-reader path/to/paper.tar.gz -o paper.html
```

## Organize

- **Tabs:** Inbox · Later · Completed · Archive · Trash
- **Pin** papers for the sidebar; **tags** filter the list
- **Search** (`/` or sidebar) by title/author
- Drag a card onto a tab (or Pinned) to move it

## Read

Open a paper for the single-column reader:

- Click text to highlight; add optional notes
- Hover citations / figures / tables for previews
- Theme, font size, and column width in the reader chrome
- Highlights stay in browser `localStorage` (per paper)

## Sync (optional)

Preferences → paste a git remote → **Setup**, then **Sync Now** (or pull-to-refresh on the library) to backup/sync the library folder.
