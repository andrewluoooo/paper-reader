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
| `.pdf` | Docling (local) or MinerU Cloud — Preferences → PDF Parser (+ API token for MinerU) |
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

## Account & vaults

Accounts use an anonymous **secret key** (no email or username). Each key unlocks its own **encrypted vault** under `~/.paper_reader_library/vaults/<id>/`. On first launch, generate a key and save it somewhere safe — it is shown once and cannot be recovered. Sign in later by pasting the key. A new key starts an empty vault; an old key still opens its vault.

Account metadata (salts + key hash) lives in `~/.paper_reader_library/accounts.json`. Paper HTML and raw sources are stored as ciphertext; the decryption key is only held in memory while you are signed in. A legacy flat library (`index.json` / `*.html` / `raw/`) is claimed **once** into the first key that signs in or generates after upgrade — later keys stay empty.

**Export:** Preferences → **Export library** downloads a plaintext ZIP of unlocked HTML + `index.json`.

To leave the library open (no sign-in):

```bash
export PAPER_READER_DISABLE_AUTH=1
paper-reader --library
```
