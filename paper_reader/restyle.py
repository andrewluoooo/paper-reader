"""Re-skin LaTeXML's HTML5 output (the same `ltx_*` class scheme arXiv's
own HTML views use) into a single-column, generous-margin reader page,
without touching the underlying semantic markup: MathML equations,
figures, tables, citations, and links all pass through untouched, only
restyled and inlined into one self-contained file.
"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
import re

from bs4 import BeautifulSoup, NavigableString

# Toolbar/UI icons, in the style of Feather Icons (MIT licensed,
# https://feathericons.com/) -- thin-stroke, no fill, 24x24 grid. Used
# instead of emoji for every icon-only control so the reader has a
# consistent, professional look rather than relying on the OS's emoji
# font (which varies wildly in style and color across platforms).
ICONS = {
    "menu": '<line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/>'
    '<line x1="3" y1="18" x2="21" y2="18"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "arrow-left": '<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>',
    "sun": '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/>'
    '<line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>'
    '<line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/>'
    '<line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>'
    '<line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>',
    "moon": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    "circle-half": '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>',
    "message": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "chevron-right": '<polyline points="9 6 15 12 9 18"/>',
}


def _icon(name: str, size: int = 18) -> str:
    """Inline SVG markup for one of the ICONS paths above, sized to fit
    inside the existing 36px circular toolbar buttons. Uses currentColor
    so it inherits the button's text color (and theme) automatically."""
    body = ICONS[name]
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #5b5b5b;
  --link: #1a56db;
  --rule: #e3e0d8;
  --table-border: #e9e5de;
  --table-header-bg: #f7f6f3;
  --table-row-hover: #fbfaf8;
  --sidebar-bg: #fbfaf8;
  --control-bg: #ffffff;
  --control-hover-bg: #f2f0ec;
  --highlight-yellow: #fdf1a8;
  --highlight-green: #c8f0d4;
  --highlight-blue: #cfe6fb;
  --highlight-pink: #fbd9ea;
  --highlight-yellow-line: #e0ab0e;
  --highlight-green-line: #2fa056;
  --highlight-blue-line: #2a7de1;
  --highlight-pink-line: #d84c96;

  --reader-font-size: 19px;
  --reader-line-height: 1.65;
  --reader-max-width: 700px;
  --reader-font-serif: Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, "Times New Roman", serif;
  --reader-font-sans: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
  --reader-font-family: var(--reader-font-serif);

  --sidebar-left-width: 260px;
  --sidebar-right-width: 300px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161513;
    --fg: #ece9e2;
    --muted: #a9a49a;
    --link: #7fa7ff;
    --rule: #33322d;
    --table-border: #3a3934;
    --table-header-bg: #232220;
    --table-row-hover: #1c1b19;
    --sidebar-bg: #1b1a18;
    --control-bg: #201f1d;
    --control-hover-bg: #2a2926;
    --highlight-yellow: #6b5c17;
    --highlight-green: #1f5c37;
    --highlight-blue: #1f4a6b;
    --highlight-pink: #6b1f45;
    --highlight-yellow-line: #e0ab0e;
    --highlight-green-line: #3ecb7a;
    --highlight-blue-line: #5b9dea;
    --highlight-pink-line: #ea6bb3;
  }
}
/* explicit override, set by the theme toggle (wins over prefers-color-scheme either way) */
:root[data-theme="light"] {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5b5b5b; --link: #1a56db; --rule: #e3e0d8;
  --table-border: #e9e5de; --table-header-bg: #f7f6f3; --table-row-hover: #fbfaf8;
  --sidebar-bg: #fbfaf8; --control-bg: #ffffff; --control-hover-bg: #f2f0ec;
  --highlight-yellow: #fdf1a8; --highlight-green: #c8f0d4; --highlight-blue: #cfe6fb; --highlight-pink: #fbd9ea;
  --highlight-yellow-line: #e0ab0e; --highlight-green-line: #2fa056; --highlight-blue-line: #2a7de1; --highlight-pink-line: #d84c96;
}
:root[data-theme="dark"] {
  --bg: #161513; --fg: #ece9e2; --muted: #a9a49a; --link: #7fa7ff; --rule: #33322d;
  --table-border: #3a3934; --table-header-bg: #232220; --table-row-hover: #1c1b19;
  --sidebar-bg: #1b1a18; --control-bg: #201f1d; --control-hover-bg: #2a2926;
  --highlight-yellow: #6b5c17; --highlight-green: #1f5c37; --highlight-blue: #1f4a6b; --highlight-pink: #6b1f45;
  --highlight-yellow-line: #e0ab0e; --highlight-green-line: #3ecb7a; --highlight-blue-line: #5b9dea; --highlight-pink-line: #ea6bb3;
}
* { box-sizing: border-box; }
::selection { background-color: var(--highlight-blue); }
::-moz-selection { background-color: var(--highlight-blue); }
html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--fg);
}
body {
  font-family: var(--reader-font-family);
  line-height: var(--reader-line-height);
  font-size: var(--reader-font-size);
  -webkit-font-smoothing: antialiased;
}
/* A thin fixed bar at the very top of the viewport showing how far down
   the paper you've scrolled -- stays in view above everything else
   (highest z-index on the page) while you read. */
.reader-progress-track {
  position: fixed;
  top: 0; left: 0; right: 0;
  height: 3px;
  background: transparent;
  z-index: 50;
}
.reader-progress-fill {
  height: 100%;
  width: 0%;
  background: var(--link);
}

/* A vertical bar in the left margin marking which paragraph/figure is
   currently in reading focus, updated by an IntersectionObserver as you
   scroll -- same accent color as the progress bar so the two read as
   one "where am I" system. */
.reader-focus-bar {
  position: fixed;
  width: 3px;
  background: var(--link);
  border-radius: 2px;
  opacity: 0.6;
  z-index: 15;
  transition: top 0.15s ease, height 0.15s ease, left 0.15s ease;
  pointer-events: none;
}
.reader-focus-bar[hidden] { display: none; }

.reader-shell { display: flex; align-items: flex-start; min-height: 100vh; }
.page {
  flex: 1 1 auto;
  min-width: 0;
  max-width: var(--reader-max-width);
  margin: 0 auto;
  padding: 8vh 8vw 20vh;
}
@media (min-width: 1400px) {
  .page { padding-left: 12vw; padding-right: 12vw; }
}
a { color: var(--link); text-decoration: underline; text-underline-offset: 2px; word-break: break-word; }

/* ---- title / authors / abstract -------------------------------- */
h1.ltx_title_document {
  font-size: 2.1em;
  font-weight: 700;
  line-height: 1.25;
  margin: 0 0 0.5em;
}
.ltx_authors { margin: 0 0 1.6em; }
.ltx_creator { display: block; margin: 0 0 0.7em; }
.ltx_personname { font-size: 1.05em; }
.ltx_role_affiliation, .ltx_role_email, .ltx_role_email a {
  color: var(--muted);
  font-size: 0.85em;
}
.ltx_author_notes { display: block; margin-top: 0.1em; }
.ltx_dates, .ltx_classification { display: none; }

.ltx_abstract {
  margin: 1.8em 0 2.4em;
  padding: 1.2em 1.4em;
  border-left: 3px solid var(--rule);
}
.ltx_abstract .ltx_title_abstract {
  font-weight: 700;
  margin: 0 0 0.5em;
  display: block;
}
.ltx_abstract .ltx_p { text-align: justify; hyphens: auto; margin: 0; }

.ltx_keywords, .ltx_acmcategories { font-size: 0.88em; color: var(--muted); margin: 1em 0; }

/* ---- sections ----------------------------------------------------- */
h2.ltx_title_section { font-size: 1.35em; font-weight: 700; margin: 1.8em 0 0.6em; }
h3.ltx_title_subsection { font-size: 1.14em; font-weight: 700; margin: 1.5em 0 0.5em; }
h4.ltx_title_subsubsection { font-size: 1.03em; font-weight: 700; margin: 1.3em 0 0.4em; }
.ltx_bibliography h2.ltx_title_bibliography { font-size: 1.35em; font-weight: 700; margin: 1.8em 0 0.8em; }

p.ltx_p { margin: 0 0 1.15em; text-align: justify; hyphens: auto; }

/* ---- inline emphasis ------------------------------------------------ */
.ltx_font_bold { font-weight: 700; }
.ltx_font_italic { font-style: italic; }
.ltx_font_slanted { font-style: oblique; }
.ltx_font_upright { font-style: normal; }
.ltx_font_medium { font-weight: 400; }
.ltx_font_smallcaps { font-variant: small-caps; }
.ltx_font_typewriter {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.92em;
}

/* ---- lists ------------------------------------------------------------
   LaTeXML emits <li class="ltx_item" style="list-style-type:none;"> with
   its own bullet/number as a *sibling* <span class="ltx_tag_item"> before
   the item's actual content (often a nested <div class="ltx_para"> wrapping
   one or more <p class="ltx_p">, but sometimes just a bare paragraph or
   plain text) -- left unstyled, that tag sits inline while the content
   after it is block-level, so it drops to its own line with a gap above
   the text instead of hanging beside it. Taking the tag out of flow and
   reserving space for it with padding works regardless of what shape the
   item's content takes, unlike flexbox (which would fight multi-paragraph
   items by trying to lay every child out side by side). */
ul.ltx_itemize, ol.ltx_enumerate { list-style: none; margin: 0 0 1.15em; padding: 0; }
li.ltx_item { position: relative; padding-left: 1.6em; }
li.ltx_item > .ltx_tag_item { position: absolute; left: 0; top: 0; white-space: nowrap; }

/* ---- figures / tables ---------------------------------------------- */
figure.ltx_figure, figure.ltx_table, .ltx_float {
  margin: 2em auto;
  text-align: center;
  max-width: 100%;
}
figure.ltx_figure img, .ltx_graphics { max-width: 100%; height: auto; }
/* LaTeXML renders some diagrams (tikz etc.) as a native inline <svg> --
   _strip_latexml_scaling_wrappers() removes the ltx_transformed_* wrapper
   that would otherwise scale it, so without this the SVG paints at its
   literal width/height attributes and spills out of the reading column.
   A sized-but-viewBox-less root <svg> scales proportionally under CSS
   width/height constraints the same way an <img> does. */
svg.ltx_picture, .ltx_picture { max-width: 100%; height: auto; display: block; margin: 0 auto; }
figcaption.ltx_caption, .ltx_caption {
  font-size: 0.92em;
  color: var(--muted);
  margin-top: 0.8em;
  text-align: left;
  line-height: 1.5;
  display: block;
}
figure.ltx_table { text-align: center; }
.ltx_table_scroll {
  display: inline-block;
  vertical-align: top;
  max-width: 100%;
  overflow-x: auto;
  overflow-y: hidden;
  border: 1px solid var(--table-border);
  border-radius: 8px;
}
table.ltx_tabular {
  border-collapse: collapse;
  font-size: 0.86em;
}
table.ltx_tabular td, table.ltx_tabular th {
  padding: 0.25em 0.55em;
  border: 1px solid var(--table-border);
}
table.ltx_tabular > tr:first-child > td,
table.ltx_tabular > tr:first-child > th,
table.ltx_tabular thead td,
table.ltx_tabular thead th {
  background: var(--table-header-bg);
  font-weight: 600;
}
table.ltx_tabular > tr:first-child > td:first-child,
table.ltx_tabular > tr:first-child > th:first-child { border-top-left-radius: 7px; }
table.ltx_tabular > tr:first-child > td:last-child,
table.ltx_tabular > tr:first-child > th:last-child { border-top-right-radius: 7px; }
table.ltx_tabular > tr:last-child > td:first-child { border-bottom-left-radius: 7px; }
table.ltx_tabular > tr:last-child > td:last-child { border-bottom-right-radius: 7px; }
table.ltx_tabular tr:hover > td { background: var(--table-row-hover); }
table.ltx_tabular > tr:first-child:hover > td { background: var(--table-header-bg); }
.ltx_figure_panel { display: inline-block; vertical-align: top; margin: 0.5em; }

/* ---- equations ------------------------------------------------------ */
table.ltx_equationgroup, table.ltx_eqn_table, .ltx_equation {
  width: 100%;
  margin: 1.4em 0;
  border-collapse: collapse;
}
table.ltx_equationgroup td, table.ltx_eqn_table td { text-align: center; }
.ltx_eqn_num, .ltx_tag_equation { color: var(--muted); }
math { font-size: 1.05em; }

/* ---- algorithms / listings ------------------------------------------ */
figure.ltx_algorithm, figure.ltx_float_algorithm {
  text-align: left;
  border: 1px solid var(--rule);
  border-radius: 8px;
  padding: 0.7em 1em;
  margin: 1.4em 0;
  font-size: 0.9em;
}
.ltx_listing { text-align: left; }
.ltx_listing_scroll {
  overflow-x: auto;
  border: 1px solid var(--rule);
  border-radius: 8px;
  padding: 0.7em 1em;
  margin: 1.4em 0;
  font-size: 0.9em;
}
/* the enclosing algorithm float already draws its own box; don't nest a second one */
figure.ltx_algorithm .ltx_listing_scroll,
figure.ltx_float_algorithm .ltx_listing_scroll {
  border: none;
  border-radius: 0;
  padding: 0;
  margin: 0;
  font-size: 1em;
}
figure.ltx_float_algorithm figcaption.ltx_caption {
  margin: 0 0 0.4em;
  font-size: 1em;
  color: var(--fg);
  font-weight: 600;
}
.ltx_listingline { line-height: 1.25; }
.ltx_tag_listingline {
  display: inline-block;
  min-width: 1.6em;
  margin-right: 0.5em;
  color: var(--muted);
  font-size: 0.85em;
  text-align: right;
}

/* ---- references / footnotes / misc ----------------------------------- */
.ltx_bibliography .ltx_biblist { list-style: none; padding: 0; margin: 0; }
.ltx_bibliography .ltx_bibitem {
  margin: 0 0 0.9em;
  padding-left: 2.2em;
  text-indent: -2.2em;
  font-size: 0.93em;
  line-height: 1.55;
}
.ltx_note_mark { vertical-align: super; font-size: 0.72em; }
.ltx_note_outer, .ltx_note_content {
  font-size: 0.85em;
  color: var(--muted);
  font-style: italic;
  margin-left: 0.3em;
}
.ltx_author_before { display: none; }
.ltx_role_affiliation br { display: none; }
sup.ltx_note { font-size: 0.8em; }

.ltx_ERROR { display: none; }

.meta {
  color: var(--muted);
  font-family: var(--reader-font-sans);
  font-size: 0.85em;
  margin-bottom: 3em;
  padding-bottom: 1.5em;
  border-bottom: 1px solid var(--rule);
}

/* ==================== reader UI: outline / toolbar / popovers ========= */
.reader-sidebar {
  flex: 0 0 var(--sidebar-left-width);
  width: var(--sidebar-left-width);
  height: 100vh;
  position: sticky;
  top: 0;
  overflow-y: auto;
  background: var(--sidebar-bg);
  border-right: 1px solid var(--rule);
  padding: 1.5em 0.5em 2em 1.2em;
  font-family: var(--reader-font-sans);
  transition: flex-basis 0.22s ease, width 0.22s ease, padding-left 0.22s ease,
    padding-right 0.22s ease, border-right-color 0.22s ease, opacity 0.15s ease;
}
html.sidebar-left-collapsed .reader-sidebar {
  flex-basis: 0; width: 0; padding-left: 0; padding-right: 0; border-right-color: transparent;
  overflow: hidden; opacity: 0;
}
html.sidebar-resizing .reader-sidebar, html.sidebar-resizing .reader-sidebar-right { transition: none; }
.reader-sidebar-resize {
  position: absolute;
  top: 0; bottom: 0; right: -3px;
  width: 6px;
  cursor: col-resize;
  z-index: 5;
  touch-action: none;
}
.reader-sidebar-resize:hover, .reader-sidebar-resize.dragging { background: var(--link); opacity: 0.3; }
html.sidebar-left-collapsed .reader-sidebar-resize { display: none; }
.reader-sidebar-title {
  font-size: 0.72em;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  font-weight: 600;
  margin: 0 0 0.8em;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-right: 0.8em;
}
.reader-kbd-hint {
  display: inline-block;
  min-width: 1.3em;
  text-align: center;
  padding: 0.05em 0.35em;
  margin-left: 0.5em;
  border: 1px solid var(--rule);
  border-radius: 4px;
  background: var(--control-bg);
  color: var(--muted);
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.95em;
  font-weight: 600;
  text-transform: none;
  letter-spacing: 0;
  vertical-align: 1px;
}
.reader-sidebar-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: none;
  color: var(--muted);
  cursor: pointer;
  line-height: 1;
  padding: 0.2em;
  border-radius: 6px;
}
.reader-sidebar-close:hover { background: var(--control-hover-bg); color: var(--fg); }
.reader-outline { display: flex; flex-direction: column; }
.reader-outline a {
  color: var(--fg);
  text-decoration: none;
  font-size: 0.86em;
  line-height: 1.4;
  padding: 0.32em 0.8em 0.32em calc(0.8em + var(--lvl, 0) * 0.9em);
  border-radius: 6px;
  border-left: 2px solid transparent;
  white-space: normal;
}
.reader-outline a:hover { background: var(--control-hover-bg); }
.reader-outline a.active {
  border-left-color: var(--link);
  color: var(--link);
  font-weight: 600;
  background: var(--control-hover-bg);
}
.reader-sidebar-backdrop { display: none; }

.reader-toolbar {
  position: fixed;
  top: 1.1em;
  right: 1.4em;
  z-index: 40;
  display: flex;
  gap: 0.4em;
  transition: right 0.22s ease;
}
html.sidebar-resizing .reader-toolbar { transition: none; }
.reader-toolbar button, .reader-toolbar a, .reader-sidebar-toggle {
  width: 36px;
  height: 36px;
  border-radius: 999px;
  border: 1px solid var(--rule);
  background: var(--control-bg);
  color: var(--fg);
  font-family: var(--reader-font-sans);
  font-size: 0.95em;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 1px 4px rgba(0,0,0,0.08);
  text-decoration: none;
}
.reader-toolbar button:hover, .reader-toolbar a:hover, .reader-sidebar-toggle:hover { background: var(--control-hover-bg); }
.reader-toolbar svg, .reader-sidebar-toggle svg, .reader-sidebar-close svg, .reader-sidebar-close-right svg,
.reader-selection-toolbar svg { display: block; }
.reader-sidebar-toggle {
  position: fixed;
  top: 1.1em;
  left: 1.2em;
  z-index: 40;
  display: none;
}
html.sidebar-left-collapsed .reader-sidebar-toggle { display: inline-flex; }

.reader-popover {
  position: fixed;
  top: 3.4em;
  right: 1.4em;
  z-index: 41;
  background: var(--control-bg);
  border: 1px solid var(--rule);
  border-radius: 10px;
  box-shadow: 0 6px 24px rgba(0,0,0,0.16);
  padding: 1em;
  width: 250px;
  font-family: var(--reader-font-sans);
  font-size: 0.85em;
}
.reader-popover[hidden] { display: none; }
.reader-popover-wide { width: 290px; }
.reader-popover-section-label {
  color: var(--muted);
  font-size: 0.8em;
  font-weight: 600;
  margin: 0 0 0.6em;
}
.reader-popover-section-label + .reader-popover-section-label,
.reader-theme-grid + .reader-popover-section-label { margin-top: 1.1em; }
.reader-theme-grid { display: flex; gap: 0.6em; margin-bottom: 0.4em; }
.reader-theme-card {
  flex: 1;
  display: flex; flex-direction: column; align-items: center; gap: 0.5em;
  border: 2px solid var(--rule); border-radius: 9px;
  background: var(--control-bg); color: var(--fg);
  padding: 0.6em 0.3em 0.55em;
  cursor: pointer;
  font-family: var(--reader-font-sans);
  font-size: 0.85em;
}
.reader-theme-card:hover { border-color: var(--muted); }
.reader-theme-card.active { border-color: var(--link); }
.reader-theme-swatch {
  width: 100%; height: 34px; border-radius: 6px;
  display: flex; align-items: center; justify-content: center; gap: 0.2em;
}
.reader-theme-swatch-light { background: #ffffff; color: #1a1a1a; border: 1px solid var(--rule); }
.reader-theme-swatch-dark { background: #161513; color: #ece9e2; }
.reader-theme-swatch-auto {
  background: linear-gradient(90deg, #ffffff 50%, #161513 50%); color: #1a1a1a;
  border: 1px solid var(--rule);
}
.reader-theme-swatch-auto svg:last-child { color: #ece9e2; }
.reader-popover-card {
  border: 1px solid var(--rule); border-radius: 9px; padding: 0.85em 0.9em;
}
.reader-popover-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.9em;
}
.reader-popover-row:last-child { margin-bottom: 0; }
.reader-popover-label { color: var(--muted); }
.reader-popover-row-click { cursor: pointer; }
.reader-popover-value-btn {
  display: inline-flex; align-items: center; gap: 0.3em; color: var(--fg);
}
.reader-popover-value-btn svg { color: var(--muted); }
.reader-typeface-menu {
  display: flex; flex-direction: column; gap: 0.15em;
  margin: -0.3em 0 0.9em; padding-bottom: 0.7em; border-bottom: 1px solid var(--rule);
}
.reader-typeface-menu[hidden] { display: none; }
.reader-typeface-menu button {
  text-align: left; border: none; background: none; color: var(--fg);
  padding: 0.4em 0.5em; border-radius: 6px; cursor: pointer;
  font-family: var(--reader-font-sans); font-size: 0.95em;
}
.reader-typeface-menu button:hover { background: var(--control-hover-bg); }
.reader-typeface-menu button.active { background: var(--link); color: #fff; }
.reader-stepper { display: inline-flex; align-items: center; gap: 0.6em; }
.reader-stepper button {
  width: 26px; height: 26px; border-radius: 999px;
  border: 1px solid var(--rule); background: var(--control-bg); color: var(--fg);
  cursor: pointer; font-size: 1em; line-height: 1;
}
.reader-stepper button:hover { background: var(--control-hover-bg); }
.reader-stepper span { min-width: 2em; text-align: center; color: var(--fg); }

.reader-selection-toolbar {
  position: fixed;
  z-index: 42;
  display: flex;
  gap: 0.35em;
  background: var(--control-bg);
  border: 1px solid var(--rule);
  border-radius: 999px;
  box-shadow: 0 6px 20px rgba(0,0,0,0.2);
  padding: 0.4em;
}
.reader-selection-toolbar[hidden] { display: none; }
.reader-swatch {
  width: 24px; height: 24px; border-radius: 999px;
  border: 1px solid rgba(0,0,0,0.15);
  cursor: pointer;
  padding: 0;
}
.reader-selection-toolbar button.reader-remove,
.reader-selection-toolbar button.reader-note-btn {
  width: 24px; height: 24px; border-radius: 999px;
  border: 1px solid var(--rule); background: var(--control-bg); color: var(--fg);
  cursor: pointer; line-height: 1;
  display: inline-flex; align-items: center; justify-content: center;
}

/* Drag handles for adjusting an existing highlight's start/end boundary,
   shown only while its manage toolbar is open. Each is a small soft,
   translucent circle (tinted with the highlight's own light color, not
   its bolder underline accent) plus a thin stem (the ::after
   pseudo-element) connecting it back to the text edge it controls. */
.reader-hl-handle {
  position: fixed;
  z-index: 43;
  width: 16px;
  height: 16px;
  border-radius: 999px;
  background: var(--hl-handle-color, var(--highlight-yellow));
  opacity: 0.8;
  box-shadow: 0 1px 5px rgba(0,0,0,0.15);
  cursor: grab;
  touch-action: none;
  transition: transform 0.15s ease, opacity 0.15s ease;
}
.reader-hl-handle[hidden] { display: none; }
.reader-hl-handle:hover {
  opacity: 1;
  animation: reader-hl-handle-pulse 0.9s ease-in-out infinite;
}
@keyframes reader-hl-handle-pulse {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.3); }
}
/* While actively being dragged, the handle itself hides -- it would
   otherwise sit right under the pointer, blocking the view of exactly
   where the boundary is landing. */
.reader-hl-handle.dragging {
  cursor: grabbing;
  opacity: 0;
  pointer-events: none;
  animation: none;
}
.reader-hl-handle::after {
  content: "";
  position: absolute;
  left: 50%;
  width: 2px;
  height: 14px;
  background: var(--hl-handle-color, var(--highlight-yellow));
  transform: translateX(-50%);
}
.reader-hl-handle-start::after { top: 100%; }
.reader-hl-handle-end::after { bottom: 100%; }

/* Plain-text highlights are painted via the CSS Custom Highlight API
   (registered in JS as CSS.highlights.set("user-hl-<color>", new
   Highlight(...ranges))), not real DOM elements -- so there's nothing to
   select with a class, and the pseudo-element only supports a small
   property set (no border-radius, padding, or cursor -- but it does
   support text-decoration, which is what gives the highlighter-pen
   double-tone look: a light wash plus a bolder underline accent). */
::highlight(user-hl-yellow) {
  background-color: var(--highlight-yellow);
  text-decoration-line: underline;
  text-decoration-color: var(--highlight-yellow-line);
  text-decoration-thickness: 2.5px;
}
::highlight(user-hl-green) {
  background-color: var(--highlight-green);
  text-decoration-line: underline;
  text-decoration-color: var(--highlight-green-line);
  text-decoration-thickness: 2.5px;
}
::highlight(user-hl-blue) {
  background-color: var(--highlight-blue);
  text-decoration-line: underline;
  text-decoration-color: var(--highlight-blue-line);
  text-decoration-thickness: 2.5px;
}
::highlight(user-hl-pink) {
  background-color: var(--highlight-pink);
  text-decoration-line: underline;
  text-decoration-color: var(--highlight-pink-line);
  text-decoration-thickness: 2.5px;
}
::highlight(user-hl-flash) { background-color: var(--link); color: #ffffff; }

/* MathML and citations are wrapped from the outside in a real <mark>
   instead (see collectHighlightSegments in the script below) -- Chrome
   has a rendering bug where painting a ::highlight() background *through*
   MathML content shifts the equation's inter-symbol spacing, and this
   avoids ever doing that. Same double-tone look via border-bottom, since
   a real element can use whatever properties it likes. */
mark.user-highlight {
  background: var(--hl-color, var(--highlight-yellow));
  color: inherit;
  border-radius: 2px;
  border-bottom: 2.5px solid var(--hl-line-color, var(--highlight-yellow-line));
  padding-bottom: 1px;
}
mark.user-highlight.flash { outline: 2px solid var(--link); outline-offset: 2px; }

/* ---- right sidebar: info / highlights & notes ------------------------ */
.reader-sidebar-right {
  flex: 0 0 var(--sidebar-right-width);
  width: var(--sidebar-right-width);
  height: 100vh;
  position: sticky;
  top: 0;
  overflow-y: auto;
  background: var(--sidebar-bg);
  border-left: 1px solid var(--rule);
  padding: 1.5em 1.2em 2em;
  font-family: var(--reader-font-sans);
  transition: flex-basis 0.22s ease, width 0.22s ease, padding-left 0.22s ease,
    padding-right 0.22s ease, border-left-color 0.22s ease, opacity 0.15s ease;
}
html.sidebar-right-collapsed .reader-sidebar-right {
  flex-basis: 0; width: 0; padding-left: 0; padding-right: 0; border-left-color: transparent;
  overflow: hidden; opacity: 0;
}
.reader-sidebar-right .reader-sidebar-resize { right: auto; left: -3px; }
html.sidebar-right-collapsed .reader-sidebar-right .reader-sidebar-resize { display: none; }
.reader-sidebar-right-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.6em;
  font-size: 0.72em;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  font-weight: 600;
}
.reader-sidebar-close-right {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: none;
  color: var(--muted);
  cursor: pointer;
  line-height: 1;
  padding: 0.2em;
  border-radius: 6px;
}
.reader-sidebar-close-right:hover { background: var(--control-hover-bg); color: var(--fg); }
.reader-tabs { display: flex; gap: 0.4em; margin-bottom: 1.2em; }
.reader-tab-btn {
  flex: 1;
  padding: 0.5em 0.4em;
  border-radius: 7px;
  border: 1px solid var(--rule);
  background: var(--control-bg);
  color: var(--fg);
  cursor: pointer;
  font-size: 0.8em;
  font-family: var(--reader-font-sans);
}
.reader-tab-btn:hover { background: var(--control-hover-bg); }
.reader-tab-btn.active { background: var(--link); color: #fff; border-color: var(--link); }
.reader-tab-panel[hidden] { display: none; }

.reader-meta-title { font-size: 1em; font-weight: 700; line-height: 1.35; margin: 0 0 1em; }
.reader-meta-section { margin-bottom: 1.1em; }
.reader-meta-label {
  font-size: 0.7em; text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--muted); margin-bottom: 0.4em; display: block;
}
.reader-meta-author { font-size: 0.85em; margin-bottom: 0.35em; line-height: 1.4; }
.reader-meta-sub { color: var(--muted); }
.reader-abstract { font-size: 0.83em; line-height: 1.55; color: var(--fg); }
.reader-meta-row {
  display: flex; justify-content: space-between; gap: 0.8em;
  font-size: 0.82em; padding: 0.4em 0; border-bottom: 1px solid var(--rule);
}
.reader-meta-row .reader-meta-label { margin: 0; text-transform: none; letter-spacing: 0; font-size: 1em; color: var(--muted); flex-shrink: 0; }
.reader-meta-row:last-child { border-bottom: none; }
.reader-meta-row a { word-break: break-all; text-align: right; }

.reader-hl-btn-row { display: flex; gap: 0.5em; margin-bottom: 1em; }
.reader-export-btn {
  flex: 1; width: 100%; padding: 0.6em; border-radius: 7px; border: 1px solid var(--rule);
  background: var(--control-bg); color: var(--fg); cursor: pointer;
  font-size: 0.83em; font-family: var(--reader-font-sans); margin-bottom: 0;
}
.reader-export-btn:hover { background: var(--control-hover-bg); }

.reader-hl-empty { color: var(--muted); font-size: 0.83em; }
.reader-hl-item { border: 1px solid var(--rule); border-radius: 8px; padding: 0.7em; margin-bottom: 0.8em; }
.reader-hl-quote {
  border-left: 3px solid var(--hl-color, var(--highlight-yellow));
  padding-left: 0.6em; font-size: 0.85em; line-height: 1.45; margin-bottom: 0.5em;
  color: var(--fg); cursor: pointer;
  overflow-x: auto; max-width: 100%;
}
.reader-hl-quote math { font-size: 1em; }
.reader-hl-note {
  width: 100%; border: 1px solid var(--rule); border-radius: 6px;
  background: var(--bg); color: var(--fg); font-family: var(--reader-font-sans);
  font-size: 0.82em; padding: 0.5em; resize: vertical; min-height: 2.6em;
}
.reader-hl-actions { display: flex; justify-content: space-between; margin-top: 0.5em; }
.reader-hl-actions button {
  border: none; background: none; color: var(--muted); cursor: pointer;
  font-size: 0.78em; font-family: var(--reader-font-sans); padding: 0.2em 0.4em;
}
.reader-hl-actions button:hover { color: var(--fg); text-decoration: underline; }

.reader-notes-toggle { display: none; }
html.sidebar-right-collapsed .reader-notes-toggle { display: inline-flex; }

@media (min-width: 1151px) {
  .reader-toolbar { right: calc(var(--sidebar-right-width) + 1.4em); }
  html.sidebar-right-collapsed .reader-toolbar { right: 1.4em; }
}

@media (max-width: 900px) {
  .reader-sidebar {
    position: fixed;
    top: 0; left: 0;
    height: 100vh;
    z-index: 45;
    transform: translateX(-100%);
    transition: transform 0.2s ease;
    box-shadow: 0 0 30px rgba(0,0,0,0.25);
  }
  .reader-sidebar.open { transform: translateX(0); }
  .reader-sidebar-close { display: block; }
  .reader-sidebar-toggle { display: inline-flex; }
  .reader-sidebar-backdrop {
    display: none;
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.3);
    z-index: 44;
  }
  .reader-sidebar-backdrop.open { display: block; }
  .page { padding-left: 8vw; }
}
@media (max-width: 1150px) {
  .reader-sidebar-right {
    position: fixed;
    top: 0; right: 0;
    height: 100vh;
    z-index: 45;
    transform: translateX(100%);
    transition: transform 0.2s ease;
    box-shadow: 0 0 30px rgba(0,0,0,0.25);
  }
  .reader-sidebar-right.open { transform: translateX(0); }
  .reader-sidebar-close-right { display: block; }
  .reader-notes-toggle { display: inline-flex; }
  .reader-sidebar-backdrop-right {
    display: none;
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.3);
    z-index: 44;
  }
  .reader-sidebar-backdrop-right.open { display: block; }
}

/* ---- citation hover preview ------------------------------------------ */
.reader-citation-tooltip {
  position: fixed;
  z-index: 46;
  max-width: 340px;
  background: var(--control-bg);
  border: 1px solid var(--rule);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.2);
  padding: 0.7em 0.9em;
  font-family: var(--reader-font-sans);
  font-size: 0.82em;
  line-height: 1.5;
  color: var(--fg);
  pointer-events: none;
}
.reader-citation-tooltip[hidden] { display: none; }
.reader-citation-tooltip-num {
  display: block;
  font-weight: 600;
  color: var(--muted);
  margin-bottom: 0.3em;
  font-size: 0.9em;
}
.reader-citation-tooltip-text {
  display: -webkit-box;
  -webkit-line-clamp: 5;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
a.ltx_ref[href^="#bib."] { text-decoration-style: dotted; }

/* ---- figure/table hover preview --------------------------------------- */
.reader-ref-preview {
  position: fixed;
  z-index: 46;
  width: 320px;
  max-width: 88vw;
  background: var(--control-bg);
  border: 1px solid var(--rule);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.2);
  padding: 0.7em 0.8em;
  font-family: var(--reader-font-sans);
  pointer-events: none;
}
.reader-ref-preview[hidden] { display: none; }
.reader-ref-preview-body {
  display: flex;
  justify-content: center;
  overflow: hidden;
}
.reader-ref-preview-body img {
  max-width: 100%;
  max-height: 200px;
  object-fit: contain;
  display: block;
}
.reader-ref-preview-body table {
  border-collapse: collapse;
  transform-origin: top left;
}
.reader-ref-preview-caption {
  margin-top: 0.5em;
  font-size: 0.78em;
  line-height: 1.45;
  color: var(--muted);
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ---- whole-page drag-and-drop upload (matches the library home page) -- */
.reader-drop-overlay {
  position: fixed; inset: 0; background: rgba(26, 86, 219, 0.08);
  border: 3px dashed var(--link); z-index: 999;
  display: flex; align-items: center; justify-content: center; pointer-events: none;
}
.reader-drop-overlay[hidden] { display: none; }
.reader-drop-overlay-card {
  background: var(--control-bg); border: 1px solid var(--rule); border-radius: 14px; padding: 2.2em 3em;
  text-align: center; font-family: var(--reader-font-sans);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}
.reader-drop-overlay-card strong { display: block; font-size: 1.2em; margin-bottom: 0.4em; color: var(--fg); }
.reader-drop-overlay-card div { color: var(--muted); font-size: 0.88em; }

.reader-upload-toast {
  position: fixed; left: 1.2em; bottom: 1.2em; z-index: 200; max-width: 340px;
  background: var(--fg); color: var(--bg);
  border-radius: 10px; padding: 0.75em 1em;
  font-family: var(--reader-font-sans); font-size: 0.85em; line-height: 1.4;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
}
.reader-upload-toast[hidden] { display: none; }
.reader-upload-toast.error { color: #fff; background: #b3261e; }
.reader-upload-toast a { color: inherit; text-decoration: underline; text-underline-offset: 2px; }
.reader-upload-spinner {
  display: inline-block; width: 0.9em; height: 0.9em; border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: currentColor; border-radius: 50%; animation: readerUploadSpin 0.7s linear infinite;
  margin-right: 0.5em; vertical-align: -0.15em;
}
@keyframes readerUploadSpin { to { transform: rotate(360deg); } }

/* ---- margin comments: notes shown beside their highlight, Notion-style */
.reader-margin-comments {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 0;
  pointer-events: none;
  z-index: 20;
}
.reader-margin-comment {
  position: absolute;
  width: 220px;
  pointer-events: auto;
  background: var(--control-bg);
  border: 1px solid var(--rule);
  border-radius: 8px;
  padding: 0.6em 0.7em;
  font-family: var(--reader-font-sans);
  font-size: 0.8em;
  line-height: 1.45;
  color: var(--fg);
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  cursor: pointer;
  transition: box-shadow 0.15s ease, border-color 0.15s ease;
}
.reader-margin-comment:hover { border-color: var(--link); box-shadow: 0 4px 14px rgba(0,0,0,0.14); }
.reader-margin-comment-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.4em; }
.reader-margin-comment-quote {
  flex: 1;
  min-width: 0;
  font-size: 0.85em;
  color: var(--muted);
  margin-bottom: 0.35em;
  padding-left: 0.5em;
  border-left: 2px solid var(--hl-color, var(--highlight-yellow-line));
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.reader-margin-comment-actions { display: flex; gap: 0.15em; flex-shrink: 0; opacity: 0; transition: opacity 0.15s ease; }
.reader-margin-comment:hover .reader-margin-comment-actions { opacity: 1; }
.reader-margin-comment-actions button {
  border: none; background: none; color: var(--muted); cursor: pointer;
  padding: 0.2em; border-radius: 5px; display: inline-flex; align-items: center; justify-content: center;
}
.reader-margin-comment-actions button:hover { background: var(--control-hover-bg); color: var(--fg); }
.reader-margin-comment-note { white-space: pre-wrap; word-break: break-word; }
.reader-margin-comment-note-edit {
  width: 100%; border: 1px solid var(--rule); border-radius: 5px;
  background: var(--bg); color: var(--fg); font-family: var(--reader-font-sans);
  font-size: 1em; line-height: 1.45; padding: 0.35em; resize: vertical; min-height: 3.2em;
}
@media (max-width: 1150px) {
  .reader-margin-comments { display: none; }
}

@media print {
  .reader-sidebar, .reader-sidebar-right, .reader-toolbar, .reader-popover,
  .reader-selection-toolbar, .reader-sidebar-toggle, .reader-citation-tooltip,
  .reader-hl-handle, .reader-progress-track, .reader-ref-preview,
  .reader-margin-comments { display: none !important; }
}
"""

# Tables and code listings can be intrinsically wider than the reading
# column (many-column result tables, long pseudocode lines). Unlike an
# <img>, an HTML table/listing has no CSS-only "shrink to fit" behavior,
# and we can't know its rendered pixel width at build time (this is a
# static file, not a headless-browser render). So: measure each one in
# the browser at load time, and if it's wider than the column, scale it
# down with a CSS transform so it fits with no horizontal scrolling
# needed. Falls back to the CSS `overflow-x: auto` scroll if JS is
# unavailable.
FIT_SCRIPT = """
(function () {
  // clientWidth is 0 for inline elements (LaTeXML sometimes wraps a
  // figure's content in an inline <span class="ltx_transformed_inner">
  // of its own), so measure geometry with getBoundingClientRect instead
  // -- it works the same regardless of the element's display type.
  function contentBoxWidth(el) {
    var r = el.getBoundingClientRect();
    var cs = getComputedStyle(el);
    return r.width
      - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0)
      - (parseFloat(cs.borderLeftWidth) || 0) - (parseFloat(cs.borderRightWidth) || 0);
  }

  function fit(wrap) {
    var content = wrap.firstElementChild;
    if (!content) return;
    content.style.transform = "";
    content.style.transformOrigin = "";
    wrap.style.width = "";
    wrap.style.height = "";
    wrap.style.overflow = "";

    var avail = contentBoxWidth(wrap.parentElement);
    var wrapCS = getComputedStyle(wrap);
    var wrapPadX = (parseFloat(wrapCS.paddingLeft) || 0) + (parseFloat(wrapCS.paddingRight) || 0);
    var wrapBorderX = (parseFloat(wrapCS.borderLeftWidth) || 0) + (parseFloat(wrapCS.borderRightWidth) || 0);
    var contentAvail = avail - wrapPadX - wrapBorderX;
    var natural = content.scrollWidth;

    if (avail > 0 && contentAvail > 0 && natural > contentAvail + 1) {
      var scale = contentAvail / natural;
      content.style.transformOrigin = "top left";
      content.style.transform = "scale(" + scale.toFixed(4) + ")";
      var wrapPadY = (parseFloat(wrapCS.paddingTop) || 0) + (parseFloat(wrapCS.paddingBottom) || 0);
      var wrapBorderY = (parseFloat(wrapCS.borderTopWidth) || 0) + (parseFloat(wrapCS.borderBottomWidth) || 0);
      wrap.style.width = avail + "px";
      wrap.style.height = (content.scrollHeight * scale + wrapPadY + wrapBorderY) + "px";
      wrap.style.overflow = "hidden";
    }
  }

  function fitAll() {
    document.querySelectorAll(".ltx_fit_scroll").forEach(fit);
  }

  if (document.readyState === "complete") {
    fitAll();
  } else {
    window.addEventListener("load", fitAll);
  }
  var resizeTimer;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(fitAll, 150);
  });
})();
"""

# Applies persisted theme/text-style settings before first paint, inline
# at the top of <head> (blocking, on purpose) so there's no flash of the
# default styling before the user's saved preferences kick in.
HEAD_INIT_SCRIPT = """
(function () {
  try {
    var s = JSON.parse(localStorage.getItem("paper_reader_settings") || "{}");
    var root = document.documentElement;
    if (s.theme === "light" || s.theme === "dark") root.setAttribute("data-theme", s.theme);
    if (s.fontSize) root.style.setProperty("--reader-font-size", s.fontSize);
    if (s.fontFamily === "sans") root.style.setProperty("--reader-font-family", "var(--reader-font-sans)");
    if (s.fontFamily === "serif") root.style.setProperty("--reader-font-family", "var(--reader-font-serif)");
    if (s.maxWidth) root.style.setProperty("--reader-max-width", s.maxWidth);
    if (s.lineHeight) root.style.setProperty("--reader-line-height", s.lineHeight);
    if (s.sidebarLeftWidth) root.style.setProperty("--sidebar-left-width", s.sidebarLeftWidth);
    if (s.sidebarRightWidth) root.style.setProperty("--sidebar-right-width", s.sidebarRightWidth);
    if (s.sidebarLeftCollapsed) root.classList.add("sidebar-left-collapsed");
    if (s.sidebarRightCollapsed) root.classList.add("sidebar-right-collapsed");
  } catch (e) {}
})();
"""

# Readwise-Reader-ish reading UI: a left document outline (with
# scroll-spy), a light/dark/auto theme toggle, a text-style popover
# (font size, font family, column width), and basic click-to-highlight
# with localStorage persistence. Kept dependency-free (no bundler, no
# external libs) since the whole point is one self-contained HTML file.
READER_SCRIPT = """
(function () {
  var SETTINGS_KEY = "paper_reader_settings";
  var HIGHLIGHTS_KEY = "paper_reader_highlights::" + encodeURIComponent(document.title || location.pathname);
  var HL_COLORS = { yellow: "var(--highlight-yellow)", green: "var(--highlight-green)", blue: "var(--highlight-blue)", pink: "var(--highlight-pink)" };
  var HL_LINE_COLORS = { yellow: "var(--highlight-yellow-line)", green: "var(--highlight-green-line)", blue: "var(--highlight-blue-line)", pink: "var(--highlight-pink-line)" };
  var SVG_OPEN = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
  var THEME_ICONS = {
    auto: SVG_OPEN + '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/></svg>',
    light: SVG_OPEN + '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>' +
      '<line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>' +
      '<line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>' +
      '<line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>',
    dark: SVG_OPEN + '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
  };
  var SVG_OPEN_SM = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
  var MARGIN_EDIT_ICON = SVG_OPEN_SM + '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>';
  var MARGIN_DELETE_ICON = SVG_OPEN_SM + '<polyline points="3 6 5 6 21 6"/>' +
    '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>' +
    '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>';

  function loadSettings() {
    try { return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}"); } catch (e) { return {}; }
  }
  function saveSettings(patch) {
    var s = loadSettings();
    Object.assign(s, patch);
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (e) {}
    return s;
  }
  function loadHighlights() {
    try { return JSON.parse(localStorage.getItem(HIGHLIGHTS_KEY) || "[]"); } catch (e) { return []; }
  }
  function saveHighlights(list) {
    try { localStorage.setItem(HIGHLIGHTS_KEY, JSON.stringify(list)); } catch (e) {}
  }

  /* ---------------------------------------------------------------- theme */
  function applyTheme(theme) {
    var root = document.documentElement;
    if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
    else root.removeAttribute("data-theme");
    var btn = document.getElementById("themeToggleBtn");
    if (btn) btn.innerHTML = THEME_ICONS[theme] || THEME_ICONS.auto;
  }
  function initTheme() {
    var s = loadSettings();
    applyTheme(s.theme || "auto");
    var btn = document.getElementById("themeToggleBtn");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var cur = loadSettings().theme || "auto";
      var next = cur === "auto" ? "light" : cur === "light" ? "dark" : "auto";
      saveSettings({ theme: next });
      applyTheme(next);
    });
  }

  /* ---------------------------------------------------------- text style */
  var WIDTH_STEPS = [
    { value: "560px", label: "Narrow" },
    { value: "700px", label: "Default" },
    { value: "900px", label: "Wide" }
  ];
  var FAMILY_LABELS = { serif: "Serif", sans: "Sans-serif" };
  var LINE_HEIGHT_MIN = 1.3;
  var LINE_HEIGHT_MAX = 2.0;
  var LINE_HEIGHT_STEP = 0.05;

  function fmtLineHeight(v) {
    return (Math.round(v * 100) / 100).toString();
  }

  function initTextStyle() {
    var s = loadSettings();
    var sizeVal = parseInt(s.fontSize, 10) || 19;
    var family = s.fontFamily || "serif";
    var width = s.maxWidth || "700px";
    var lineHeightVal = parseFloat(s.lineHeight) || 1.65;
    var themeVal = s.theme || "auto";

    var btn = document.getElementById("textStyleBtn");
    var pop = document.getElementById("textStylePopover");
    var sizeLabel = document.getElementById("fontSizeLabel");
    var lineHeightLabel = document.getElementById("lineHeightLabel");
    var maxWidthLabel = document.getElementById("maxWidthLabel");
    var typefaceValueLabel = document.getElementById("typefaceValueLabel");
    var typefaceRow = document.getElementById("typefaceRow");
    var typefaceMenu = document.getElementById("typefaceMenu");
    var themeGrid = document.getElementById("themeGrid");

    if (sizeLabel) sizeLabel.textContent = sizeVal + "px";
    if (lineHeightLabel) lineHeightLabel.textContent = fmtLineHeight(lineHeightVal);
    var widthIdx = Math.max(0, WIDTH_STEPS.map(function (w) { return w.value; }).indexOf(width));
    if (maxWidthLabel) maxWidthLabel.textContent = WIDTH_STEPS[widthIdx].label;
    if (typefaceValueLabel) typefaceValueLabel.textContent = FAMILY_LABELS[family] || "Serif";

    function setActive(container, value) {
      container.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("active", b.dataset.value === value);
      });
    }
    if (themeGrid) setActive(themeGrid, themeVal);
    if (typefaceMenu) setActive(typefaceMenu, family);

    function closePopover() {
      pop.hidden = true;
      if (typefaceMenu) typefaceMenu.hidden = true;
    }
    if (btn && pop) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var willOpen = pop.hidden;
        pop.hidden = !pop.hidden;
        if (!willOpen && typefaceMenu) typefaceMenu.hidden = true;
      });
      document.addEventListener("click", function (e) {
        if (!pop.hidden && !pop.contains(e.target) && e.target !== btn) closePopover();
      });
    }

    if (themeGrid) {
      themeGrid.querySelectorAll("button").forEach(function (b) {
        b.addEventListener("click", function () {
          var val = b.dataset.value;
          themeVal = val;
          saveSettings({ theme: val });
          applyTheme(val);
          setActive(themeGrid, val);
        });
      });
    }

    if (typefaceRow && typefaceMenu) {
      typefaceRow.addEventListener("click", function (e) {
        e.stopPropagation();
        typefaceMenu.hidden = !typefaceMenu.hidden;
      });
      typefaceMenu.querySelectorAll("button").forEach(function (b) {
        b.addEventListener("click", function (e) {
          e.stopPropagation();
          var val = b.dataset.value;
          family = val;
          document.documentElement.style.setProperty(
            "--reader-font-family", val === "sans" ? "var(--reader-font-sans)" : "var(--reader-font-serif)"
          );
          saveSettings({ fontFamily: val });
          setActive(typefaceMenu, val);
          if (typefaceValueLabel) typefaceValueLabel.textContent = FAMILY_LABELS[val] || "Serif";
          typefaceMenu.hidden = true;
        });
      });
    }

    var decBtn = document.getElementById("fontSizeDec");
    var incBtn = document.getElementById("fontSizeInc");
    function changeSize(delta) {
      sizeVal = Math.max(14, Math.min(26, sizeVal + delta));
      document.documentElement.style.setProperty("--reader-font-size", sizeVal + "px");
      if (sizeLabel) sizeLabel.textContent = sizeVal + "px";
      saveSettings({ fontSize: sizeVal + "px" });
    }
    if (decBtn) decBtn.addEventListener("click", function () { changeSize(-1); });
    if (incBtn) incBtn.addEventListener("click", function () { changeSize(1); });

    var lhDecBtn = document.getElementById("lineHeightDec");
    var lhIncBtn = document.getElementById("lineHeightInc");
    function changeLineHeight(delta) {
      lineHeightVal = Math.max(LINE_HEIGHT_MIN, Math.min(LINE_HEIGHT_MAX, lineHeightVal + delta));
      lineHeightVal = Math.round(lineHeightVal * 100) / 100;
      document.documentElement.style.setProperty("--reader-line-height", String(lineHeightVal));
      if (lineHeightLabel) lineHeightLabel.textContent = fmtLineHeight(lineHeightVal);
      saveSettings({ lineHeight: String(lineHeightVal) });
    }
    if (lhDecBtn) lhDecBtn.addEventListener("click", function () { changeLineHeight(-LINE_HEIGHT_STEP); });
    if (lhIncBtn) lhIncBtn.addEventListener("click", function () { changeLineHeight(LINE_HEIGHT_STEP); });

    var mwDecBtn = document.getElementById("maxWidthDec");
    var mwIncBtn = document.getElementById("maxWidthInc");
    function changeMaxWidth(delta) {
      widthIdx = Math.max(0, Math.min(WIDTH_STEPS.length - 1, widthIdx + delta));
      var step = WIDTH_STEPS[widthIdx];
      document.documentElement.style.setProperty("--reader-max-width", step.value);
      if (maxWidthLabel) maxWidthLabel.textContent = step.label;
      saveSettings({ maxWidth: step.value });
      window.dispatchEvent(new Event("resize"));
    }
    if (mwDecBtn) mwDecBtn.addEventListener("click", function () { changeMaxWidth(-1); });
    if (mwIncBtn) mwIncBtn.addEventListener("click", function () { changeMaxWidth(1); });
  }

  /* -------------------------------------------------------------- sidebar */
  // Drag-to-resize for a sidebar's width, backed by a CSS variable and
  // persisted setting. `mirrored` flips the drag direction for the right
  // sidebar, whose handle sits on its left (inner) edge.
  function initSidebarResize(handle, cssVar, settingsKey, min, max, defaultPx, mirrored) {
    if (!handle) return;
    var dragging = false, startX = 0, startWidth = 0;
    handle.addEventListener("mousedown", function (e) {
      dragging = true;
      startX = e.clientX;
      startWidth = parseInt(getComputedStyle(document.documentElement).getPropertyValue(cssVar), 10) || defaultPx;
      handle.classList.add("dragging");
      // The open/close toggle transition is great for a keyboard/button
      // triggered collapse, but it would make a live drag feel laggy
      // (each mousemove frame re-easing behind the cursor) -- suspend it
      // for the duration of the drag.
      document.documentElement.classList.add("sidebar-resizing");
      document.body.style.userSelect = "none";
      e.preventDefault();
    });
    document.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      var delta = e.clientX - startX;
      if (mirrored) delta = -delta;
      var next = Math.max(min, Math.min(max, startWidth + delta));
      document.documentElement.style.setProperty(cssVar, next + "px");
      updateMarginComments(); // the column shifts as the sidebar is dragged, not just resized
    });
    document.addEventListener("mouseup", function () {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove("dragging");
      document.documentElement.classList.remove("sidebar-resizing");
      document.body.style.removeProperty("user-select");
      var patch = {};
      patch[settingsKey] = getComputedStyle(document.documentElement).getPropertyValue(cssVar).trim();
      saveSettings(patch);
    });
    handle.addEventListener("dblclick", function () {
      document.documentElement.style.setProperty(cssVar, defaultPx + "px");
      var patch = {};
      patch[settingsKey] = defaultPx + "px";
      saveSettings(patch);
      updateMarginComments();
    });
  }

  function initSidebar() {
    var sidebar = document.getElementById("readerSidebar");
    var toggle = document.getElementById("sidebarToggleBtn");
    var closeBtn = document.getElementById("sidebarCloseBtn");
    var backdrop = document.getElementById("sidebarBackdrop");
    if (!sidebar) return;
    function open() { sidebar.classList.add("open"); if (backdrop) backdrop.classList.add("open"); }
    function close() { sidebar.classList.remove("open"); if (backdrop) backdrop.classList.remove("open"); }
    // On desktop the sidebar is a permanently docked flex column, not an
    // overlay -- "collapse" there means shrinking it to zero width via
    // the sidebar-left-collapsed class, independent of the mobile
    // open/close overlay state above.
    function collapse(v) {
      document.documentElement.classList.toggle("sidebar-left-collapsed", v);
      saveSettings({ sidebarLeftCollapsed: v });
      updateMarginComments();
    }
    function toggleSidebar() {
      if (window.innerWidth <= 900) { sidebar.classList.contains("open") ? close() : open(); }
      else { collapse(!document.documentElement.classList.contains("sidebar-left-collapsed")); }
    }
    window.__readerToggleLeftSidebar = toggleSidebar;
    if (toggle) toggle.addEventListener("click", toggleSidebar);
    if (closeBtn) closeBtn.addEventListener("click", function () {
      if (window.innerWidth <= 900) close(); else collapse(true);
    });
    if (backdrop) backdrop.addEventListener("click", close);
    sidebar.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function () { if (window.innerWidth <= 900) close(); });
    });

    initSidebarResize(document.getElementById("sidebarResizeLeft"), "--sidebar-left-width", "sidebarLeftWidth", 200, 480, 260, false);

    var links = Array.prototype.slice.call(sidebar.querySelectorAll(".reader-outline a"));
    if (!links.length || !("IntersectionObserver" in window)) return;
    var targets = links.map(function (a) {
      return document.getElementById(decodeURIComponent(a.getAttribute("href").slice(1)));
    });
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        var idx = targets.indexOf(entry.target);
        if (idx === -1) return;
        if (entry.isIntersecting) {
          links.forEach(function (l) { l.classList.remove("active"); });
          links[idx].classList.add("active");
        }
      });
    }, { rootMargin: "0px 0px -70% 0px", threshold: 0 });
    targets.forEach(function (t) { if (t) observer.observe(t); });
  }

  /* ----------------------------------------------------------- highlights */
  var pageRoot;

  function nodePath(node, root) {
    var path = [];
    var cur = node;
    while (cur && cur !== root) {
      var parent = cur.parentNode;
      if (!parent) return null;
      path.unshift(Array.prototype.indexOf.call(parent.childNodes, cur));
      cur = parent;
    }
    return cur === root ? path : null;
  }
  function nodeFromPath(path, root) {
    var cur = root;
    for (var i = 0; i < path.length; i++) {
      if (!cur || !cur.childNodes || !cur.childNodes[path[i]]) return null;
      cur = cur.childNodes[path[i]];
    }
    return cur;
  }
  // Highlighting is a hybrid of two mechanisms, chosen per content type:
  //
  // 1. Plain text/links: the CSS Custom Highlight API (CSS.highlights /
  //    the Highlight constructor) -- the browser paints a background over
  //    a Range with zero DOM mutation, so nothing can corrupt.
  //
  // 2. MathML (<math>) and citation parentheticals (<cite>): wrapped from
  //    the *outside* in a real <mark> element instead. Two independent
  //    approaches were tried and rejected for these: inserting DOM nodes
  //    into a partially-selected Range corrupts MathML's content model
  //    (Range.extractContents() splits nested <mi>/<mo> open to preserve
  //    valid nesting), and Chrome has a rendering bug where painting a
  //    ::highlight() background *through* MathML content visibly changes
  //    the equation's inter-symbol spacing. Wrapping the whole element
  //    from outside sidesteps both: the <math> subtree is never touched
  //    and never has a highlight pseudo-element painted through it, so
  //    its layout is computed exactly as if nothing were highlighted --
  //    the <mark> just paints a background box behind the untouched math.
  var ATOMIC_HIGHLIGHT_SELECTOR = "math, cite";
  var HIGHLIGHT_COLOR_NAMES = ["yellow", "green", "blue", "pink"];
  var cssHighlightsSupported = typeof Highlight !== "undefined" && !!(window.CSS && CSS.highlights);
  var highlightRegistry = {}; // id -> { color, marks: [<mark>...], cssRanges: [Range...] }

  function ensureColorHighlightBuckets() {
    if (!cssHighlightsSupported) return;
    HIGHLIGHT_COLOR_NAMES.forEach(function (color) {
      if (!CSS.highlights.has("user-hl-" + color)) {
        CSS.highlights.set("user-hl-" + color, new Highlight());
      }
    });
  }

  // Splits a Range's content into: whole atomic elements it touches (any
  // overlap counts as "touches", per the all-or-nothing semantics below),
  // and the runs of plain content around/between them, each as its own
  // Range clipped to the original boundaries. Purely read-only -- no
  // Range API extraction/mutation involved, just walking the live tree.
  // Returns an ordered list of { type: "atomic", el } | { type: "run", range }
  // segments covering the range's content, document order preserved (needed
  // both to apply the highlight correctly and to build an accurate preview
  // for the sidebar/export).
  function collectHighlightSegments(range) {
    // If the *entire* range already sits inside one atomic element (e.g.
    // the user only selected part of one equation), commonAncestorContainer
    // is already somewhere deep inside it -- a top-down walk from there
    // would only ever see its descendants, never the <math>/<cite>
    // ancestor itself, so that case is checked separately up front.
    var caContainer = range.commonAncestorContainer;
    var caEl = caContainer.nodeType === 1 ? caContainer : caContainer.parentElement;
    var enclosingAtomic = caEl ? caEl.closest(ATOMIC_HIGHLIGHT_SELECTOR) : null;
    if (enclosingAtomic) {
      return [{ type: "atomic", el: enclosingAtomic }];
    }

    var segments = [];
    var currentRun = [];
    function flushRun() {
      if (currentRun.length) {
        segments.push({ type: "run", nodes: currentRun });
        currentRun = [];
      }
    }
    function visit(node) {
      if (!range.intersectsNode(node)) return;
      if (node.nodeType === 1 && node.matches && node.matches(ATOMIC_HIGHLIGHT_SELECTOR)) {
        flushRun();
        segments.push({ type: "atomic", el: node });
        return; // never descend into an atomic element
      }
      if (node.nodeType === 3) {
        if (node.nodeValue && node.nodeValue.length) currentRun.push(node);
        return;
      }
      var children = node.childNodes;
      for (var i = 0; i < children.length; i++) visit(children[i]);
    }
    visit(range.commonAncestorContainer);
    flushRun();

    var result = [];
    segments.forEach(function (s) {
      if (s.type === "atomic") { result.push(s); return; }
      var firstNode = s.nodes[0], lastNode = s.nodes[s.nodes.length - 1];
      var r = document.createRange();
      r.setStart(firstNode, firstNode === range.startContainer ? range.startOffset : 0);
      r.setEnd(lastNode, lastNode === range.endContainer ? range.endOffset : lastNode.nodeValue.length);
      if (!r.collapsed) result.push({ type: "run", range: r });
    });
    return result;
  }

  // Builds sidebar/export preview text+html directly from the same
  // segments that get highlighted, so a partial equation selection (which
  // the atomic rule expands to the whole equation) previews as the whole
  // equation too, not the narrower originally-dragged text.
  function buildHighlightPreview(segments) {
    var htmlParts = [], textParts = [];
    segments.forEach(function (seg) {
      if (seg.type === "atomic") {
        htmlParts.push(seg.el.outerHTML);
        textParts.push(seg.el.textContent);
      } else {
        var container = document.createElement("div");
        container.appendChild(seg.range.cloneContents());
        htmlParts.push(container.innerHTML);
        textParts.push(seg.range.toString());
      }
    });
    return { html: htmlParts.join(""), text: textParts.join("") };
  }

  function wrapMarkAround(el, color, id) {
    var mark = document.createElement("mark");
    mark.className = "user-highlight";
    mark.style.setProperty("--hl-color", HL_COLORS[color] || HL_COLORS.yellow);
    mark.style.setProperty("--hl-line-color", HL_LINE_COLORS[color] || HL_LINE_COLORS.yellow);
    mark.dataset.highlightId = id;
    el.parentNode.insertBefore(mark, el);
    mark.appendChild(el);
    return mark;
  }
  function unwrapMark(mark) {
    var parent = mark.parentNode;
    if (!parent) return;
    while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
    parent.removeChild(mark);
    parent.normalize();
  }

  function applySegments(segments, color, id) {
    var marks = [];
    var cssRanges = [];
    var bucket = cssHighlightsSupported ? CSS.highlights.get("user-hl-" + color) : null;
    segments.forEach(function (seg) {
      if (seg.type === "atomic") {
        if (seg.el.closest(".user-highlight")) return; // already covered (overlap edge case)
        marks.push(wrapMarkAround(seg.el, color, id));
      } else {
        if (bucket) bucket.add(seg.range);
        cssRanges.push(seg.range);
      }
    });
    highlightRegistry[id] = { color: color, marks: marks, cssRanges: cssRanges };
  }
  function applyHighlightVisual(range, color, id) {
    applySegments(collectHighlightSegments(range), color, id);
  }
  function removeHighlightVisual(id) {
    var entry = highlightRegistry[id];
    if (!entry) return;
    entry.marks.forEach(unwrapMark);
    if (cssHighlightsSupported) {
      var bucket = CSS.highlights.get("user-hl-" + entry.color);
      if (bucket) entry.cssRanges.forEach(function (r) { bucket.delete(r); });
    }
    delete highlightRegistry[id];
  }
  // A click on a <mark>-wrapped atomic element is a normal DOM hit test.
  // A click on a plain-text CSS-highlighted run has no element to hit --
  // it's answered by finding the caret position under the pointer and
  // checking it against every stored run range.
  function findHighlightIdAtPoint(x, y) {
    var el = document.elementFromPoint(x, y);
    var markEl = el && el.closest && el.closest("mark.user-highlight");
    if (markEl) return markEl.dataset.highlightId;

    var node, offset;
    if (document.caretPositionFromPoint) {
      var pos = document.caretPositionFromPoint(x, y);
      if (!pos) return null;
      node = pos.offsetNode;
      offset = pos.offset;
    } else if (document.caretRangeFromPoint) {
      var r = document.caretRangeFromPoint(x, y);
      if (!r) return null;
      node = r.startContainer;
      offset = r.startOffset;
    } else {
      return null;
    }
    for (var id in highlightRegistry) {
      var ranges = highlightRegistry[id].cssRanges;
      for (var i = 0; i < ranges.length; i++) {
        try {
          if (ranges[i].comparePoint(node, offset) === 0) return id;
        } catch (e) { /* point not in the same tree as this range; not a match */ }
      }
    }
    return null;
  }
  function removeHighlightById(id) {
    removeHighlightVisual(id);
    saveHighlights(loadHighlights().filter(function (h) { return h.id !== id; }));
    renderHighlightsList();
  }
  function restoreHighlights() {
    ensureColorHighlightBuckets();
    loadHighlights().forEach(function (h) {
      try {
        var startNode = nodeFromPath(h.startPath, pageRoot);
        var endNode = nodeFromPath(h.endPath, pageRoot);
        if (!startNode || !endNode) return;
        var range = document.createRange();
        range.setStart(startNode, h.startOffset);
        range.setEnd(endNode, h.endOffset);
        applyHighlightVisual(range, h.color, h.id);
      } catch (e) { /* skip a malformed/stale entry rather than fail the whole page */ }
    });
    renderHighlightsList();
  }

  /* -------------------------------------------------- highlights & notes panel */
  function comparePaths(a, b) {
    var len = Math.min(a.length, b.length);
    for (var i = 0; i < len; i++) {
      if (a[i] !== b[i]) return a[i] - b[i];
    }
    return a.length - b.length;
  }
  function jumpToHighlight(id) {
    var entry = highlightRegistry[id];
    if (!entry) return;
    var rect = entry.marks.length ? entry.marks[0].getBoundingClientRect()
      : entry.cssRanges.length ? entry.cssRanges[0].getBoundingClientRect()
      : null;
    if (!rect) return;
    window.scrollTo({ top: window.scrollY + rect.top - window.innerHeight / 3, behavior: "smooth" });
    entry.marks.forEach(function (m) { m.classList.add("flash"); });
    // brief flash for the CSS-highlighted portion: paint it in a dedicated
    // high-priority bucket for a moment
    var flashBucket = null;
    if (cssHighlightsSupported && entry.cssRanges.length) {
      flashBucket = CSS.highlights.get("user-hl-flash");
      if (!flashBucket) {
        flashBucket = new Highlight();
        flashBucket.priority = 10;
        CSS.highlights.set("user-hl-flash", flashBucket);
      }
      entry.cssRanges.forEach(function (r) { flashBucket.add(r); });
    }
    setTimeout(function () {
      entry.marks.forEach(function (m) { m.classList.remove("flash"); });
      if (flashBucket) entry.cssRanges.forEach(function (r) { flashBucket.delete(r); });
    }, 900);
  }
  function focusNoteFor(id) {
    if (window.__readerOpenRightSidebar) window.__readerOpenRightSidebar();
    var tabBtn = document.querySelector('.reader-tab-btn[data-tab="tabHighlights"]');
    if (tabBtn) tabBtn.click();
    setTimeout(function () {
      var ta = document.querySelector('.reader-hl-note[data-highlight-id="' + id + '"]');
      if (ta) ta.focus();
    }, 60);
  }
  function renderHighlightsList() {
    var container = document.getElementById("highlightsList");
    if (!container) return;
    var list = loadHighlights().slice().sort(function (a, b) { return comparePaths(a.startPath, b.startPath); });
    container.innerHTML = "";
    if (!list.length) {
      container.innerHTML = '<p class="reader-hl-empty">Select text in the paper to add a highlight.</p>';
      return;
    }
    list.forEach(function (h) {
      var item = document.createElement("div");
      item.className = "reader-hl-item";

      var quote = document.createElement("div");
      quote.className = "reader-hl-quote";
      quote.style.setProperty("--hl-color", HL_COLORS[h.color] || HL_COLORS.yellow);
      if (h.html) {
        quote.innerHTML = h.html; // preserves MathML/subscripts/etc, not just flattened text
      } else {
        quote.textContent = h.text || "";
      }
      quote.addEventListener("click", function () { jumpToHighlight(h.id); });

      var note = document.createElement("textarea");
      note.className = "reader-hl-note";
      note.placeholder = "Add a note...";
      note.rows = 2;
      note.value = h.note || "";
      note.dataset.highlightId = h.id;
      var saveTimer;
      note.addEventListener("input", function () {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(function () {
          var l = loadHighlights();
          l.forEach(function (x) { if (x.id === h.id) x.note = note.value; });
          saveHighlights(l);
          updateMarginComments();
        }, 300);
      });

      var actions = document.createElement("div");
      actions.className = "reader-hl-actions";
      var jumpBtn = document.createElement("button");
      jumpBtn.textContent = "Jump to →";
      jumpBtn.addEventListener("click", function () { jumpToHighlight(h.id); });
      var copyBtn = document.createElement("button");
      copyBtn.textContent = "Copy";
      copyBtn.addEventListener("click", function () {
        copyToClipboard(highlightMarkdown(h));
        flashButtonText(copyBtn, "Copied!");
      });
      var delBtn = document.createElement("button");
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", function () { removeHighlightById(h.id); });
      actions.appendChild(jumpBtn);
      actions.appendChild(copyBtn);
      actions.appendChild(delBtn);

      item.appendChild(quote);
      item.appendChild(note);
      item.appendChild(actions);
      container.appendChild(item);
    });
    updateMarginComments();
  }
  // Notion-style margin comments: any highlight with a note gets a small
  // card floating in the gutter between the reading column and the right
  // sidebar, vertically lined up with the highlight it belongs to.
  // Absolutely positioned (document-relative, not fixed) against the
  // initial containing block, so cards scroll along with the text with
  // no scroll-event bookkeeping needed -- only re-laid-out when the
  // column's own geometry changes (see the ResizeObserver in
  // initHighlighting).
  function updateMarginComments() {
    var container = document.getElementById("marginComments");
    if (!container || !pageRoot) return;
    container.innerHTML = "";
    return; // temporarily disabled
    if (window.innerWidth <= 1150) return; // matches the CSS breakpoint that hides this layer

    var pageRect = pageRoot.getBoundingClientRect();
    var sidebarRight = document.getElementById("readerSidebarRight");
    var sidebarLeftEdge = sidebarRight ? sidebarRight.getBoundingClientRect().left : window.innerWidth;
    var gutterLeft = pageRect.right + window.scrollX + 24;
    var available = sidebarLeftEdge - pageRect.right;
    if (available < 240) return; // not enough room for a readable card -- notes stay sidebar-only

    var list = loadHighlights().filter(function (h) { return h.note && h.note.trim(); });
    var placedBottoms = [];
    list.forEach(function (h) {
      var entry = highlightRegistry[h.id];
      var rect = entry && entry.marks.length ? entry.marks[0].getBoundingClientRect()
        : entry && entry.cssRanges.length ? entry.cssRanges[0].getBoundingClientRect()
        : null;
      if (!rect) return;

      var top = rect.top + window.scrollY;
      for (var i = 0; i < placedBottoms.length; i++) {
        if (top < placedBottoms[i] + 8) top = placedBottoms[i] + 8;
      }

      var card = document.createElement("div");
      card.className = "reader-margin-comment";
      card.style.top = top + "px";
      card.style.left = gutterLeft + "px";
      card.style.setProperty("--hl-color", HL_LINE_COLORS[h.color] || HL_LINE_COLORS.yellow);

      var header = document.createElement("div");
      header.className = "reader-margin-comment-header";
      var quote = document.createElement("div");
      quote.className = "reader-margin-comment-quote";
      quote.textContent = (h.text || "").replace(/\\s+/g, " ").trim();

      var actions = document.createElement("div");
      actions.className = "reader-margin-comment-actions";
      var editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.innerHTML = MARGIN_EDIT_ICON;
      editBtn.setAttribute("aria-label", "Edit note");
      editBtn.title = "Edit note";
      var delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.innerHTML = MARGIN_DELETE_ICON;
      delBtn.setAttribute("aria-label", "Delete note");
      delBtn.title = "Delete note";
      actions.appendChild(editBtn);
      actions.appendChild(delBtn);
      header.appendChild(quote);
      header.appendChild(actions);

      var note = document.createElement("div");
      note.className = "reader-margin-comment-note";
      note.textContent = h.note.trim();

      editBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        startEditingMarginNote(h.id, note);
      });
      delBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        var l = loadHighlights();
        l.forEach(function (x) { if (x.id === h.id) x.note = ""; });
        saveHighlights(l);
        renderHighlightsList(); // re-renders the sidebar list and this card layer together
      });

      card.appendChild(header);
      card.appendChild(note);
      card.addEventListener("click", function (e) {
        if (e.target.closest(".reader-margin-comment-actions") || e.target.tagName === "TEXTAREA") return;
        jumpToHighlight(h.id);
      });

      container.appendChild(card);
      placedBottoms.push(top + card.offsetHeight);
    });
  }
  // Swaps a margin comment's note text for an inline textarea. Saves are
  // debounced while typing (so storage isn't hit every keystroke) but the
  // margin/sidebar layers aren't re-rendered until blur -- re-rendering
  // mid-edit would tear down this very textarea and drop focus/cursor.
  function startEditingMarginNote(id, noteEl) {
    var h = findHighlight(id);
    if (!h) return;
    var textarea = document.createElement("textarea");
    textarea.className = "reader-margin-comment-note-edit";
    textarea.value = h.note || "";
    noteEl.replaceWith(textarea);
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);

    var saveTimer;
    function save() {
      clearTimeout(saveTimer);
      var l = loadHighlights();
      l.forEach(function (x) { if (x.id === id) x.note = textarea.value; });
      saveHighlights(l);
    }
    textarea.addEventListener("click", function (e) { e.stopPropagation(); });
    textarea.addEventListener("input", function () {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(save, 300);
    });
    textarea.addEventListener("blur", function () {
      save();
      renderHighlightsList(); // clean re-sync of the sidebar list and this card layer
    });
  }
  // Clipboard write, with a manual-selection fallback for browsers/contexts
  // (e.g. file:// pages) where the async Clipboard API isn't available.
  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function () { legacyCopy(text); });
    }
    legacyCopy(text);
    return Promise.resolve();
  }
  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
  }
  function flashButtonText(btn, msg) {
    var original = btn.textContent;
    btn.textContent = msg;
    btn.disabled = true;
    setTimeout(function () { btn.textContent = original; btn.disabled = false; }, 1200);
  }
  function highlightMarkdown(h) {
    var text = (h.text || "").replace(/\\s+/g, " ").trim();
    var lines = ["> " + text];
    if (h.note && h.note.trim()) {
      lines.push("");
      lines.push("**Note:** " + h.note.trim());
    }
    return lines.join("\\n");
  }
  function buildHighlightsMarkdown() {
    var list = loadHighlights().slice().sort(function (a, b) { return comparePaths(a.startPath, b.startPath); });
    var meta = window.__readerMeta || {};
    var lines = [];
    lines.push("# " + (meta.title || document.title || "Paper"));
    lines.push("");
    if (meta.authors && meta.authors.length) {
      lines.push("**Authors:** " + meta.authors.map(function (a) { return a.name; }).join(", "));
    }
    if (meta.venue) lines.push("**Venue:** " + meta.venue);
    if (meta.year) lines.push("**Year:** " + meta.year);
    if (meta.doi) lines.push("**DOI:** https://doi.org/" + meta.doi);
    if (meta.source) lines.push("**Source:** " + meta.source);
    lines.push("");
    lines.push("## Highlights & Notes");
    lines.push("");
    if (!list.length) {
      lines.push("_No highlights yet._");
    } else {
      list.forEach(function (h) {
        lines.push(highlightMarkdown(h));
        lines.push("");
      });
    }
    return lines.join("\\n");
  }
  function exportMarkdown() {
    var meta = window.__readerMeta || {};
    var blob = new Blob([buildHighlightsMarkdown()], { type: "text/markdown;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = (meta.title || document.title || "paper").replace(/[^a-z0-9]+/gi, "-").toLowerCase().slice(0, 80) + "-highlights.md";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  function copyAllHighlights(btn) {
    copyToClipboard(buildHighlightsMarkdown());
    flashButtonText(btn, "Copied!");
  }

  /* ------------------------------------------------------------- right sidebar */
  function initRightSidebar() {
    var metaEl = document.getElementById("readerMetaData");
    if (metaEl) { try { window.__readerMeta = JSON.parse(metaEl.textContent); } catch (e) { window.__readerMeta = {}; } }

    var sidebar = document.getElementById("readerSidebarRight");
    if (!sidebar) return;
    var toggle = document.getElementById("sidebarRightToggleBtn");
    var closeBtn = document.getElementById("sidebarRightCloseBtn");
    var backdrop = document.getElementById("sidebarRightBackdrop");
    function open() { sidebar.classList.add("open"); if (backdrop) backdrop.classList.add("open"); }
    function close() { sidebar.classList.remove("open"); if (backdrop) backdrop.classList.remove("open"); }
    // Same desktop-collapse / mobile-overlay split as the left sidebar.
    function collapse(v) {
      document.documentElement.classList.toggle("sidebar-right-collapsed", v);
      saveSettings({ sidebarRightCollapsed: v });
      updateMarginComments();
    }
    window.__readerOpenRightSidebar = function () {
      if (window.innerWidth <= 1150) open(); else collapse(false);
    };
    function toggleSidebar() {
      if (window.innerWidth <= 1150) { sidebar.classList.contains("open") ? close() : open(); }
      else { collapse(!document.documentElement.classList.contains("sidebar-right-collapsed")); }
    }
    window.__readerToggleRightSidebar = toggleSidebar;
    if (toggle) toggle.addEventListener("click", toggleSidebar);
    if (closeBtn) closeBtn.addEventListener("click", function () {
      if (window.innerWidth <= 1150) close(); else collapse(true);
    });
    if (backdrop) backdrop.addEventListener("click", close);

    initSidebarResize(document.getElementById("sidebarResizeRight"), "--sidebar-right-width", "sidebarRightWidth", 240, 560, 300, true);

    var tabBtns = sidebar.querySelectorAll(".reader-tab-btn");
    var panels = sidebar.querySelectorAll(".reader-tab-panel");
    tabBtns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        tabBtns.forEach(function (b) { b.classList.remove("active"); });
        panels.forEach(function (p) { p.hidden = true; });
        btn.classList.add("active");
        var panel = document.getElementById(btn.dataset.tab);
        if (panel) panel.hidden = false;
      });
    });

    var exportBtn = document.getElementById("exportMarkdownBtn");
    if (exportBtn) exportBtn.addEventListener("click", exportMarkdown);
    var copyAllBtn = document.getElementById("copyAllHighlightsBtn");
    if (copyAllBtn) copyAllBtn.addEventListener("click", function () { copyAllHighlights(copyAllBtn); });
  }

  var selToolbar, activeMarkId = null;
  var hlHandleStart, hlHandleEnd, hlDragState = null, hlDragRAF = null;
  var hoverMarkId = null;
  function hideSelToolbar() { if (selToolbar) selToolbar.hidden = true; activeMarkId = null; hoverMarkId = null; hideHandles(); }
  // Shows the drag handles for whatever highlight is under the cursor, so
  // they're discoverable without first clicking to open the manage
  // toolbar. Doesn't touch activeMarkId -- an actively-managed highlight
  // (opened by a click) keeps its handles regardless of where the mouse
  // wanders until the toolbar itself is closed.
  function updateHoverHandles(x, y) {
    if (activeMarkId || hlDragState) return;
    var hlId = findHighlightIdAtPoint(x, y);
    if (hlId === hoverMarkId) return;
    hoverMarkId = hlId;
    if (hlId) positionHandles(hlId);
    else hideHandles();
  }
  function positionToolbar(rect) {
    var top = Math.max(8, rect.top - 46);
    var left = Math.max(8, Math.min(window.innerWidth - 170, rect.left + rect.width / 2 - 80));
    selToolbar.style.top = top + "px";
    selToolbar.style.left = left + "px";
    selToolbar.hidden = false;
  }
  // The toolbar shows different things depending on what's selected: a
  // fresh text selection only offers highlight colors (nothing to manage
  // yet), while an existing highlight (just created, or clicked later)
  // only offers remove + add-a-note -- recoloring isn't exposed here.
  function setToolbarMode(hasMark) {
    var swatchDisplay = hasMark ? "none" : "";
    var manageDisplay = hasMark ? "" : "none";
    selToolbar.querySelectorAll(".reader-swatch").forEach(function (btn) { btn.style.display = swatchDisplay; });
    var noteBtn = selToolbar.querySelector(".reader-note-btn");
    var removeBtn = selToolbar.querySelector(".reader-remove");
    if (noteBtn) noteBtn.style.display = manageDisplay;
    if (removeBtn) removeBtn.style.display = manageDisplay;
  }

  /* ---------------------------------------- highlight boundary drag handles */
  function findHighlight(id, list) {
    var l = list || loadHighlights();
    for (var i = 0; i < l.length; i++) if (l[i].id === id) return l[i];
    return null;
  }
  function hideHandles() {
    if (hlHandleStart) hlHandleStart.hidden = true;
    if (hlHandleEnd) hlHandleEnd.hidden = true;
  }
  // Positions the two handles from a Range's client rects directly (not
  // just its bounding box, so a multi-line highlight's handles sit on the
  // first/last line rather than floating at the corners of the whole
  // block). Takes rects+color rather than a highlight id so a live,
  // in-progress drag range can be used mid-drag -- looking the highlight
  // back up by id would read its last-saved (pre-drag) position, since
  // the drag only persists to storage on release.
  function positionHandlesFromRects(rects, color) {
    if (!hlHandleStart || !hlHandleEnd) return;
    if (!rects.length) { hideHandles(); return; }
    var first = rects[0], last = rects[rects.length - 1];
    hlHandleStart.style.setProperty("--hl-handle-color", color);
    hlHandleEnd.style.setProperty("--hl-handle-color", color);
    hlHandleStart.style.top = (first.top - 28) + "px";
    hlHandleStart.style.left = (first.left - 7) + "px";
    hlHandleEnd.style.top = (last.bottom + 14) + "px";
    hlHandleEnd.style.left = (last.right - 7) + "px";
    hlHandleStart.hidden = false;
    hlHandleEnd.hidden = false;
  }
  function positionHandles(id) {
    var h = findHighlight(id);
    if (!h) { hideHandles(); return; }
    var startNode = nodeFromPath(h.startPath, pageRoot);
    var endNode = nodeFromPath(h.endPath, pageRoot);
    if (!startNode || !endNode) { hideHandles(); return; }
    var range = document.createRange();
    try {
      range.setStart(startNode, h.startOffset);
      range.setEnd(endNode, h.endOffset);
    } catch (e) { hideHandles(); return; }
    positionHandlesFromRects(range.getClientRects(), HL_COLORS[h.color] || HL_COLORS.yellow);
  }
  function pointFromEvent(e) {
    if (document.caretPositionFromPoint) {
      var p = document.caretPositionFromPoint(e.clientX, e.clientY);
      return p ? { node: p.offsetNode, offset: p.offset } : null;
    }
    if (document.caretRangeFromPoint) {
      var r = document.caretRangeFromPoint(e.clientX, e.clientY);
      return r ? { node: r.startContainer, offset: r.startOffset } : null;
    }
    return null;
  }
  function comparePositions(a, b) {
    var ra = document.createRange(); ra.setStart(a.node, a.offset); ra.collapse(true);
    var rb = document.createRange(); rb.setStart(b.node, b.offset); rb.collapse(true);
    return ra.compareBoundaryPoints(Range.START_TO_START, rb);
  }
  function beginHighlightDrag(which, id) {
    var h = findHighlight(id);
    if (!h) return;
    var fixedPath = which === "start" ? h.endPath : h.startPath;
    var fixedOffset = which === "start" ? h.endOffset : h.startOffset;
    var fixedNode = nodeFromPath(fixedPath, pageRoot);
    if (!fixedNode) return;
    // A live, collapsed Range at the un-dragged end -- the DOM keeps its
    // boundary point valid across the wrap/unwrap mutations below, which
    // a plain {node, offset} snapshot wouldn't survive (normalize() can
    // detach the exact text node it pointed into).
    var fixedRange = document.createRange();
    fixedRange.setStart(fixedNode, fixedOffset);
    fixedRange.collapse(true);
    hlDragState = { which: which, id: id, fixedRange: fixedRange };
    // Both handles hide (not just the one being dragged) and so does the
    // manage toolbar -- they'd otherwise sit right where the boundary is
    // moving to, blocking the view of exactly where it's landing.
    if (hlHandleStart) hlHandleStart.classList.add("dragging");
    if (hlHandleEnd) hlHandleEnd.classList.add("dragging");
    if (selToolbar) selToolbar.hidden = true;
    document.body.style.userSelect = "none";
  }
  function updateHighlightDrag(e) {
    if (!hlDragState) return;
    var h = findHighlight(hlDragState.id);
    if (!h) return;
    var pos = pointFromEvent(e);
    if (!pos || !pageRoot.contains(pos.node)) return; // outside the article -- ignore, keep last valid state

    var fixed = { node: hlDragState.fixedRange.startContainer, offset: hlDragState.fixedRange.startOffset };
    var startPos, endPos;
    if (hlDragState.which === "start") {
      if (comparePositions(pos, fixed) >= 0) return; // must stay strictly before the fixed end
      startPos = pos; endPos = fixed;
    } else {
      if (comparePositions(fixed, pos) >= 0) return; // must stay strictly after the fixed start
      startPos = fixed; endPos = pos;
    }

    var range = document.createRange();
    try {
      range.setStart(startPos.node, startPos.offset);
      range.setEnd(endPos.node, endPos.offset);
    } catch (err) { return; }
    if (range.collapsed) return;

    var segments = collectHighlightSegments(range);
    if (!segments.length) return;

    removeHighlightVisual(hlDragState.id);
    applySegments(segments, h.color, hlDragState.id);

    // `range`'s boundary points are live and auto-adjust across the
    // mutations above, unlike the startPos/endPos node references (which
    // e.g. a text-node normalize() during unwrap can detach) -- so read
    // the final position back from the range itself.
    hlDragState.pendingStartPath = nodePath(range.startContainer, pageRoot);
    hlDragState.pendingStartOffset = range.startOffset;
    hlDragState.pendingEndPath = nodePath(range.endContainer, pageRoot);
    hlDragState.pendingEndOffset = range.endOffset;
    hlDragState.pendingSegments = segments;

    // Handles and toolbar stay hidden for the duration of the drag (see
    // beginHighlightDrag), but keep their position updated live so they
    // snap in at the right spot the instant the drag ends instead of
    // visibly jumping there.
    positionHandlesFromRects(range.getClientRects(), HL_COLORS[h.color] || HL_COLORS.yellow);
  }
  function endHighlightDrag() {
    if (!hlDragState) return;
    var ds = hlDragState;
    hlDragState = null;
    if (hlHandleStart) hlHandleStart.classList.remove("dragging");
    if (hlHandleEnd) hlHandleEnd.classList.remove("dragging");
    document.body.style.removeProperty("user-select");
    if (ds.pendingStartPath && ds.pendingEndPath) {
      var list = loadHighlights();
      var h = findHighlight(ds.id, list);
      if (h) {
        h.startPath = ds.pendingStartPath; h.startOffset = ds.pendingStartOffset;
        h.endPath = ds.pendingEndPath; h.endOffset = ds.pendingEndOffset;
        var preview = buildHighlightPreview(ds.pendingSegments || []);
        h.text = preview.text; h.html = preview.html;
        saveHighlights(list);
        renderHighlightsList();
      }
    }
    positionHandles(ds.id);
    // Bring the manage toolbar back now that the drag is done, positioned
    // over the (possibly new) highlight bounds.
    if (selToolbar && activeMarkId === ds.id) {
      var entry = highlightRegistry[ds.id];
      var rect = entry && entry.marks.length ? entry.marks[0].getBoundingClientRect()
        : entry && entry.cssRanges.length ? entry.cssRanges[0].getBoundingClientRect()
        : null;
      if (rect) positionToolbar(rect);
    }
  }

  function initHighlighting() {
    pageRoot = document.querySelector(".page");
    selToolbar = document.getElementById("selectionToolbar");
    hlHandleStart = document.getElementById("hlHandleStart");
    hlHandleEnd = document.getElementById("hlHandleEnd");
    if (!pageRoot || !selToolbar) return;
    restoreHighlights();

    // Any layout change that moves highlighted text -- window resize,
    // sidebar collapse/resize, font size/family/column width -- needs
    // margin comments repositioned too. A ResizeObserver on the column
    // itself catches all of these in one place instead of hooking every
    // individual control that can trigger a reflow.
    if (window.ResizeObserver) {
      new ResizeObserver(function () { updateMarginComments(); }).observe(pageRoot);
    }

    [hlHandleStart, hlHandleEnd].forEach(function (handle, i) {
      if (!handle) return;
      handle.addEventListener("pointerdown", function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (activeMarkId) beginHighlightDrag(i === 0 ? "start" : "end", activeMarkId);
      });
    });
    var hlDragLastEvent = null;
    document.addEventListener("pointermove", function (e) {
      hlDragLastEvent = e;
      if (hlDragRAF) return;
      hlDragRAF = requestAnimationFrame(function () {
        hlDragRAF = null;
        if (!hlDragLastEvent) return;
        if (hlDragState) updateHighlightDrag(hlDragLastEvent);
        else updateHoverHandles(hlDragLastEvent.clientX, hlDragLastEvent.clientY);
      });
    });
    document.addEventListener("pointerup", function () { if (hlDragState) endHighlightDrag(); });
    // Handles are fixed-position, so they need to be re-anchored whenever
    // the page scrolls or resizes while a highlight is under management.
    window.addEventListener("scroll", function () {
      var id = activeMarkId || hoverMarkId;
      if (id && hlHandleStart && !hlHandleStart.hidden) positionHandles(id);
    }, { passive: true });
    window.addEventListener("resize", function () {
      var id = activeMarkId || hoverMarkId;
      if (id && hlHandleStart && !hlHandleStart.hidden) positionHandles(id);
    });
    // A pure viewport resize doesn't change .page's own box (it's capped
    // at --reader-max-width and just re-centers in the wider/narrower
    // flex space), so the ResizeObserver on it alone won't catch this --
    // needs its own listener.
    window.addEventListener("resize", updateMarginComments);

    if (!cssHighlightsSupported) {
      console.warn("Highlighting needs the CSS Custom Highlight API (recent Chrome/Edge/Safari; Firefox 140+), which this browser doesn't support -- selection colors won't do anything here.");
    }

    selToolbar.querySelectorAll(".reader-swatch").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        if (!cssHighlightsSupported) { hideSelToolbar(); return; }
        var color = btn.dataset.color;
        var sel = window.getSelection();
        if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return;
        var liveRange = sel.getRangeAt(0);
        if (!pageRoot.contains(liveRange.commonAncestorContainer)) return;
        var startPath = nodePath(liveRange.startContainer, pageRoot);
        var endPath = nodePath(liveRange.endContainer, pageRoot);
        if (!startPath || !endPath) return;
        var id = "hl-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
        var startOffset = liveRange.startOffset, endOffset = liveRange.endOffset;
        var toolbarRect = liveRange.getBoundingClientRect();
        // Figure out what's actually going to get highlighted *before*
        // mutating anything -- the atomic rule can expand a partial
        // equation selection to the whole equation, and the preview
        // should reflect that, not the narrower original drag.
        var segments = collectHighlightSegments(liveRange);
        var preview = buildHighlightPreview(segments);
        applySegments(segments, color, id);
        sel.removeAllRanges();
        var list2 = loadHighlights();
        list2.push({
          id: id, color: color, note: "", text: preview.text, html: preview.html,
          startPath: startPath, startOffset: startOffset, endPath: endPath, endOffset: endOffset
        });
        saveHighlights(list2);
        renderHighlightsList();
        // Keep the toolbar open on the new highlight so a note/remove is
        // one click away, but adding a note is never forced -- click
        // elsewhere and the highlight just stays as-is.
        activeMarkId = id;
        setToolbarMode(true);
        positionToolbar(toolbarRect);
        positionHandles(id);
      });
    });
    var noteBtn = selToolbar.querySelector(".reader-note-btn");
    if (noteBtn) noteBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (activeMarkId) focusNoteFor(activeMarkId);
      hideSelToolbar();
    });
    var removeBtn = selToolbar.querySelector(".reader-remove");
    if (removeBtn) removeBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (activeMarkId) removeHighlightById(activeMarkId);
      hideSelToolbar();
    });

    document.addEventListener("mouseup", function (e) {
      if (e.target.closest && e.target.closest(".reader-selection-toolbar, .reader-hl-handle")) return;
      setTimeout(function () {
        var sel = window.getSelection();
        if (!sel || sel.rangeCount === 0 || sel.isCollapsed) {
          // A plain click (no drag) on an existing highlight is handled by
          // the "click" listener below, which runs first and opens the
          // manage toolbar -- don't let this deferred check close it again.
          if (!findHighlightIdAtPoint(e.clientX, e.clientY)) hideSelToolbar();
          return;
        }
        var range = sel.getRangeAt(0);
        if (!pageRoot.contains(range.commonAncestorContainer)) { hideSelToolbar(); return; }
        var rect = range.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) { hideSelToolbar(); return; }
        activeMarkId = null;
        setToolbarMode(false);
        positionToolbar(rect);
        hideHandles();
      }, 0);
    });
    document.addEventListener("click", function (e) {
      if (e.target.closest && e.target.closest(".reader-selection-toolbar, .reader-hl-handle")) return;
      var hlId = findHighlightIdAtPoint(e.clientX, e.clientY);
      if (hlId) {
        var entry = highlightRegistry[hlId];
        var rect = entry.marks.length ? entry.marks[0].getBoundingClientRect()
          : entry.cssRanges.length ? entry.cssRanges[0].getBoundingClientRect()
          : null;
        if (!rect) { hideSelToolbar(); return; }
        activeMarkId = hlId;
        setToolbarMode(true);
        positionToolbar(rect);
        positionHandles(hlId);
        e.stopPropagation();
      } else {
        hideSelToolbar();
      }
    });
  }

  /* ------------------------------------------------- citation/ref preview */
  // Hovering a cross-reference link shows a small floating preview of
  // whatever it points to: the actual reference text for a bibliography
  // citation, or a scaled-down copy of the image/table itself for a
  // figure or table reference -- so you don't have to jump away from
  // your place in the text just to see what "Figure 3" looks like.
  function initRefPreviews() {
    var citeTooltip = document.getElementById("citationTooltip");
    var citeNumEl = citeTooltip && citeTooltip.querySelector(".reader-citation-tooltip-num");
    var citeTextEl = citeTooltip && citeTooltip.querySelector(".reader-citation-tooltip-text");
    var refTooltip = document.getElementById("refPreviewTooltip");
    var refBodyEl = document.getElementById("refPreviewBody");
    var refCaptionEl = document.getElementById("refPreviewCaption");
    var showTimer, hideTimer;

    function positionNear(tooltip, rect) {
      tooltip.style.left = Math.max(8, Math.min(window.innerWidth - tooltip.offsetWidth - 8, rect.left)) + "px";
      tooltip.style.top = (rect.bottom + 8) + "px";
      tooltip.hidden = false;
      // flip above the link if it would run off the bottom of the viewport
      var tRect = tooltip.getBoundingClientRect();
      if (tRect.bottom > window.innerHeight - 8) {
        tooltip.style.top = Math.max(8, rect.top - tRect.height - 8) + "px";
      }
    }
    function hideAll() {
      if (citeTooltip) citeTooltip.hidden = true;
      if (refTooltip) refTooltip.hidden = true;
    }
    function targetOf(link) {
      var href = link.getAttribute("href") || "";
      return href.indexOf("#") === 0 ? document.getElementById(href.slice(1)) : null;
    }
    function isFigureOrTable(el) {
      return !!el && el.tagName === "FIGURE" && (el.classList.contains("ltx_figure") || el.classList.contains("ltx_table"));
    }
    function classify(link) {
      var href = link.getAttribute("href") || "";
      if (href.indexOf("#bib.") === 0) return "cite";
      if (isFigureOrTable(targetOf(link))) return "ref";
      return null;
    }

    function showCitation(link) {
      if (!citeTooltip) return;
      var target = targetOf(link);
      if (!target) return;
      var block = target.querySelector(".ltx_bibblock");
      var text = (block || target).textContent.replace(/\\s+/g, " ").trim();
      if (!text) return;
      var tagEl = target.querySelector(".ltx_tag_bibitem");
      citeNumEl.textContent = tagEl ? tagEl.textContent.trim() : link.textContent.trim();
      citeTextEl.textContent = text;
      citeTooltip.hidden = false; // needs to be visible before offsetWidth/getBoundingClientRect are meaningful
      positionNear(citeTooltip, link.getBoundingClientRect());
    }

    // Scales a cloned, freshly-inserted element down to fit maxWidth,
    // the same "measure natural size, transform: scale() to fit" trick
    // FIT_SCRIPT uses for tables too wide for the reading column -- just
    // scoped to the much narrower preview tooltip instead.
    function fitToBox(el, maxWidth, maxHeight) {
      el.style.transform = "";
      var natW = el.scrollWidth, natH = el.scrollHeight;
      var scale = Math.min(1, natW > 0 ? maxWidth / natW : 1, natH > 0 ? maxHeight / natH : 1);
      if (scale < 1) el.style.transform = "scale(" + scale.toFixed(4) + ")";
      return natH * scale;
    }
    function showRef(link) {
      if (!refTooltip) return;
      var target = targetOf(link);
      if (!isFigureOrTable(target)) return;

      var captionEl = target.querySelector(".ltx_caption");
      var captionText = captionEl ? captionEl.textContent.replace(/\\s+/g, " ").trim() : "";
      refBodyEl.innerHTML = "";
      refBodyEl.style.height = "";

      if (target.classList.contains("ltx_figure")) {
        var img = target.querySelector("img");
        if (!img) return;
        var imgClone = document.createElement("img");
        imgClone.src = img.src;
        imgClone.alt = img.alt || "";
        refBodyEl.appendChild(imgClone);
      } else {
        var table = target.querySelector("table");
        if (!table) return;
        refBodyEl.appendChild(table.cloneNode(true));
      }

      refCaptionEl.textContent = captionText;
      refTooltip.hidden = false; // needs to be laid out before measuring/positioning
      var tableClone = refBodyEl.querySelector("table");
      if (tableClone) {
        refBodyEl.style.height = fitToBox(tableClone, refBodyEl.getBoundingClientRect().width, 220) + "px";
      }
      positionNear(refTooltip, link.getBoundingClientRect());
    }

    document.addEventListener("mouseover", function (e) {
      var link = e.target.closest && e.target.closest("a.ltx_ref");
      if (!link) return;
      var kind = classify(link);
      if (!kind) return;
      clearTimeout(hideTimer);
      clearTimeout(showTimer);
      showTimer = setTimeout(function () {
        if (kind === "cite") showCitation(link); else showRef(link);
      }, 150);
    });
    document.addEventListener("mouseout", function (e) {
      var link = e.target.closest && e.target.closest("a.ltx_ref");
      if (!link || !classify(link)) return;
      clearTimeout(showTimer);
      hideTimer = setTimeout(hideAll, 120);
    });
    document.addEventListener("scroll", hideAll, true);
    document.addEventListener("click", function (e) {
      if (e.target.closest && e.target.closest("a.ltx_ref")) hideAll();
    });
  }

  /* ------------------------------------------------------------ progress bar */
  function initProgressBar() {
    var fill = document.getElementById("readerProgressFill");
    if (!fill) return;
    var ticking = false;
    function update() {
      ticking = false;
      var doc = document.documentElement;
      var scrollable = doc.scrollHeight - window.innerHeight;
      var pct = scrollable > 0 ? Math.min(1, Math.max(0, window.scrollY / scrollable)) : 0;
      fill.style.width = (pct * 100) + "%";
    }
    function onScroll() {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(update);
    }
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    update();
  }

  /* ---------------------------------------------------------------- focus bar */
  // Tracks which paragraph/figure is currently in reading focus and
  // draws a bar beside it in the left margin -- an IntersectionObserver
  // keeps a running set of on-screen candidates, and on every change we
  // pick whichever one sits closest to a fixed "focus line" about a
  // third of the way down the viewport (roughly where your eye actually
  // rests while reading), rather than just the topmost visible one.
  function initFocusBar() {
    var bar = document.getElementById("readerFocusBar");
    if (!bar || !pageRoot || !("IntersectionObserver" in window)) return;
    var targets = Array.prototype.slice.call(pageRoot.querySelectorAll(".ltx_p, figure"));
    if (!targets.length) return;

    var visible = new Set();
    var raf = null;
    var leftSidebar = document.getElementById("readerSidebar");

    function positionBar(el) {
      var rect = el.getBoundingClientRect();
      var pageRect = pageRoot.getBoundingClientRect();
      var sidebarRightEdge = leftSidebar ? leftSidebar.getBoundingClientRect().right : 0;
      if (pageRect.left - sidebarRightEdge < 20) { bar.hidden = true; return; }
      bar.style.left = (pageRect.left - 16) + "px";
      bar.style.top = rect.top + "px";
      bar.style.height = rect.height + "px";
      bar.hidden = false;
    }
    function refresh() {
      raf = null;
      if (!visible.size) { bar.hidden = true; return; }
      var focusY = window.innerHeight * 0.3;
      var best = null, bestDist = Infinity;
      visible.forEach(function (el) {
        var r = el.getBoundingClientRect();
        var dist = Math.abs((r.top + r.bottom) / 2 - focusY);
        if (dist < bestDist) { bestDist = dist; best = el; }
      });
      if (best) positionBar(best);
    }
    function scheduleRefresh() {
      if (raf) return;
      raf = requestAnimationFrame(refresh);
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) visible.add(entry.target);
        else visible.delete(entry.target);
      });
      scheduleRefresh();
    }, { threshold: [0, 0.25, 0.5, 0.75, 1] });
    targets.forEach(function (t) { observer.observe(t); });

    window.addEventListener("resize", scheduleRefresh);
  }

  /* ------------------------------------------------------- reading position */
  // Remembers how far down the page you were and scrolls back there next
  // time this paper is opened, keyed per paper the same way highlights
  // are (by document title, in localStorage -- so it's per-browser, same
  // scope as highlights/notes).
  function initReadingPosition() {
    var key = "paper_reader_scroll::" + encodeURIComponent(document.title || location.pathname);

    function restore() {
      if (location.hash) return; // an explicit deep link wins over the saved position
      var saved = parseInt(localStorage.getItem(key) || "0", 10);
      if (!saved || saved <= 0) return;
      window.scrollTo(0, saved);
    }
    // Wait for the "load" event (registered after FIT_SCRIPT's own "load"
    // listener, so it runs after that -- see FIT_SCRIPT above): table and
    // figure fit-to-width scaling can change the page's total height, and
    // restoring scroll before that settles would land in the wrong spot.
    if (document.readyState === "complete") restore();
    else window.addEventListener("load", restore);

    var ticking = false;
    function save() {
      ticking = false;
      try { localStorage.setItem(key, String(Math.round(window.scrollY))); } catch (e) {}
    }
    function onScroll() {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(save);
    }
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("beforeunload", save);
  }

  /* ----------------------------------------------------- keyboard shortcuts */
  function initKeyboardShortcuts() {
    document.addEventListener("keydown", function (e) {
      if (e.key !== "[" && e.key !== "]") return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      var t = e.target;
      var tag = t && t.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
      e.preventDefault();
      if (e.key === "[" && window.__readerToggleLeftSidebar) window.__readerToggleLeftSidebar();
      else if (e.key === "]" && window.__readerToggleRightSidebar) window.__readerToggleRightSidebar();
    });
  }

  /* ------------------------------------------------------- in-page anchor nav */
  // Every internal "#id" link -- outline entries, citations, figure/table
  // cross-references, footnotes -- is really just a same-page scroll, but
  // the browser's default anchor navigation pushes a fresh history entry
  // for each one. Left alone, hitting Back after reading for a while just
  // unwinds through every link you happened to click instead of leaving
  // the page, which is what people actually expect. Scroll manually and
  // swap the hash in with replaceState (not pushState) so the entry that
  // was already on the stack -- wherever you arrived here from, normally
  // the library -- is still exactly one Back press away no matter how
  // much in-page jumping happened in between.
  function initAnchorNav() {
    document.addEventListener("click", function (e) {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var a = e.target.closest && e.target.closest('a[href^="#"]');
      if (!a) return;
      var hash = a.getAttribute("href");
      if (!hash || hash.length < 2) return;
      var target;
      try { target = document.getElementById(decodeURIComponent(hash.slice(1))); } catch (err) { target = null; }
      if (!target) return;
      e.preventDefault();
      target.scrollIntoView({ block: "start" });
      history.replaceState(null, "", hash);
    });
  }

  /* -------------------------------------------------- whole-page drop upload */
  // Same drag-and-drop-to-upload flow as the library home page, so adding
  // another paper doesn't require leaving whatever you're currently
  // reading. Uploads land in the library's inbox; this page keeps showing
  // whatever you had open.
  function initDropUpload() {
    var overlay = document.getElementById("readerDropOverlay");
    var toast = document.getElementById("readerUploadToast");
    if (!overlay || !toast) return;

    function hasFiles(e) {
      return !!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") !== -1);
    }
    var dragCounter = 0;
    window.addEventListener("dragenter", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragCounter++;
      overlay.hidden = false;
    });
    window.addEventListener("dragover", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
    });
    window.addEventListener("dragleave", function (e) {
      if (!hasFiles(e)) return;
      dragCounter--;
      if (dragCounter <= 0) { dragCounter = 0; overlay.hidden = true; }
    });
    window.addEventListener("drop", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragCounter = 0;
      overlay.hidden = true;
      var files = e.dataTransfer.files;
      if (files && files[0]) uploadToLibrary(files[0]);
    });

    var hideTimer = null;
    function showToast(html, cls, autoHideMs) {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      toast.innerHTML = html;
      toast.className = "reader-upload-toast" + (cls ? " " + cls : "");
      toast.hidden = false;
      if (autoHideMs) hideTimer = setTimeout(function () { toast.hidden = true; }, autoHideMs);
    }

    function esc(s) {
      var d = document.createElement("div");
      d.textContent = s;
      return d.innerHTML;
    }

    function uploadToLibrary(file) {
      showToast('<span class="reader-upload-spinner"></span>Parsing "' + esc(file.name) + '"… this can take up to a minute.', "loading");
      fetch("/api/upload?filename=" + encodeURIComponent(file.name), { method: "POST", body: file })
        .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
        .then(function (res) {
          if (!res.ok) {
            showToast('Could not parse "' + esc(file.name) + '": ' + esc(res.data.error || "unknown error"), "error", 8000);
            return;
          }
          var openHref = "/library/" + encodeURIComponent(res.data.id) + ".html";
          showToast('Added "' + esc(res.data.title || file.name) + '" to your library. <a href="' + openHref + '">Open it</a>', "success", 8000);
        })
        .catch(function (e) {
          showToast("Upload failed: " + esc(e.message), "error", 8000);
        });
    }
  }

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  ready(function () {
    initTheme();
    initTextStyle();
    initSidebar();
    initRightSidebar();
    initHighlighting();
    initRefPreviews();
    initProgressBar();
    // initFocusBar(); -- temporarily disabled
    initReadingPosition();
    initDropUpload();
    initKeyboardShortcuts();
    initAnchorNav();
  });
})();
"""


def _inline_images(article, base_dir: str) -> None:
    for img in article.find_all("img"):
        src = img.get("src", "")
        if not src or src.startswith("data:") or src.startswith("http"):
            continue
        path = os.path.normpath(os.path.join(base_dir, src))
        if not os.path.isfile(path):
            continue
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        img["src"] = f"data:{mime};base64,{b64}"


def _strip_latexml_scaling_wrappers(article) -> None:
    """LaTeXML sometimes pre-scales an oversized figure/table itself via
    nested `<span class="ltx_transformed_inner">` / `<div
    class="ltx_transformed_outer">` wrappers with a fixed pt-based width
    baked in at conversion time -- not responsive to our page's layout,
    and it fights with our own responsive fit-to-width scaling (see
    FIT_SCRIPT below) if left in place. Unwrap it so our version has a
    clean, unobstructed ancestor chain to measure against.
    """
    while True:
        el = article.select_one(".ltx_transformed_outer, .ltx_transformed_inner")
        if el is None:
            return
        el.unwrap()


def _wrap_tables(soup, article) -> None:
    """Wrap each data table in its own fit-to-width container, separate
    from its (LaTeXML-authored) caption, so a wide table can be scaled
    down without dragging the caption text along with it and the
    rounded-corner grid styling doesn't bleed onto the caption."""
    for table in article.find_all("table", class_="ltx_tabular"):
        wrapper = soup.new_tag("div", **{"class": "ltx_fit_scroll ltx_table_scroll"})
        table.wrap(wrapper)


def _wrap_listings(soup, article) -> None:
    """Wrap each algorithm/code listing body in its own fit-to-width
    container (the caption stays outside, at full size)."""
    for listing in article.find_all("div", class_="ltx_listing"):
        if "ltx_listing_scroll" in (listing.parent.get("class") or []):
            continue
        wrapper = soup.new_tag("div", **{"class": "ltx_fit_scroll ltx_listing_scroll"})
        listing.wrap(wrapper)


def _extract_metadata(article) -> dict:
    """Pull paper metadata for the right-sidebar Info tab: title, authors
    (with affiliation/email), abstract, venue/year/DOI (from acmart's
    front-matter notes -- must run before `_fix_notes` decomposes those),
    and a few structural counts. Best-effort throughout: any piece that
    isn't present in a given paper is just omitted."""
    meta: dict = {}

    title_el = article.find("h1", class_="ltx_title_document")
    meta["title"] = title_el.get_text(" ", strip=True) if title_el else ""

    authors = []
    for creator in article.select(".ltx_creator"):
        name_el = creator.select_one(".ltx_personname")
        name = name_el.get_text(" ", strip=True) if name_el else ""
        aff_el = creator.select_one(".ltx_role_affiliation")
        aff = aff_el.get_text(" ", strip=True) if aff_el else ""
        email_el = creator.select_one(".ltx_role_email a")
        email = email_el.get_text(strip=True) if email_el else ""
        if name:
            authors.append({"name": name, "affiliation": aff, "email": email})
    meta["authors"] = authors

    abstract_paras = article.select(".ltx_abstract .ltx_p")
    meta["abstract"] = " ".join(p.get_text(" ", strip=True) for p in abstract_paras)

    frontmatter = {}
    for note in article.select(".ltx_note_frontmatter"):
        role = next((c[len("ltx_role_") :] for c in note.get("class", []) if c.startswith("ltx_role_")), None)
        content = note.select_one(".ltx_note_content")
        if not (role and content):
            continue
        for stray in content.select(".ltx_note_type, .ltx_note_mark, .ltx_tag_note"):
            stray.extract()
        text = content.get_text(" ", strip=True)
        if text:
            frontmatter[role] = text
    meta["venue"] = frontmatter.get("conference") or frontmatter.get("booktitle") or ""
    meta["year"] = frontmatter.get("journalyear") or frontmatter.get("copyrightyear") or ""
    meta["doi"] = frontmatter.get("doi") or ""

    meta["n_figures"] = len(article.select("figure.ltx_figure"))
    meta["n_tables"] = len(article.select("figure.ltx_table"))
    meta["n_references"] = len(article.select(".ltx_bibitem"))
    meta["n_sections"] = len(article.select("h2.ltx_title_section"))
    return meta


def _render_info_tab_html(meta: dict, source_name: str) -> str:
    parts = []
    if meta.get("authors"):
        parts.append('<div class="reader-meta-section"><div class="reader-meta-label">Authors</div>')
        for a in meta["authors"]:
            line = html.escape(a["name"])
            if a.get("affiliation"):
                line += f'<span class="reader-meta-sub"> — {html.escape(a["affiliation"])}</span>'
            parts.append(f'<div class="reader-meta-author">{line}</div>')
        parts.append("</div>")

    if meta.get("abstract"):
        snippet = meta["abstract"]
        parts.append(
            '<div class="reader-meta-section"><div class="reader-meta-label">Abstract</div>'
            f'<div class="reader-abstract">{html.escape(snippet)}</div></div>'
        )

    rows = []
    if meta.get("venue"):
        rows.append(("Venue", meta["venue"]))
    if meta.get("year"):
        rows.append(("Year", meta["year"]))
    if meta.get("doi"):
        doi = meta["doi"]
        rows.append(("DOI", f'<a href="https://doi.org/{html.escape(doi, quote=True)}">{html.escape(doi)}</a>'))
    if source_name:
        rows.append(("Source", source_name))
    rows.append(("Sections", str(meta.get("n_sections", 0))))
    rows.append(("Figures", str(meta.get("n_figures", 0))))
    rows.append(("Tables", str(meta.get("n_tables", 0))))
    rows.append(("References", str(meta.get("n_references", 0))))

    if rows:
        row_html = "".join(
            f'<div class="reader-meta-row"><span class="reader-meta-label">{html.escape(label)}</span>'
            f"<span>{value if label == 'DOI' else html.escape(value)}</span></div>"
            for label, value in rows
        )
        parts.append(f'<div class="reader-meta-section">{row_html}</div>')

    return "\n".join(parts)


def _fix_notes(article) -> None:
    """LaTeXML renders footnotes as an inline marker plus a "popup" copy
    (`.ltx_note_outer`) meant to be shown on hover via its own JS/CSS,
    which we don't ship. ACM front-matter notes (journalyear, doi, isbn,
    ...) are copyright boilerplate irrelevant to a reader and can be
    dropped outright; genuine authorial footnotes carry real content, so
    instead of hiding them we drop the redundant duplicate marker and
    let the note text flow inline, in small muted text, right after its
    marker."""
    for note in article.find_all(class_="ltx_note"):
        classes = note.get("class", [])
        if "ltx_note_frontmatter" in classes:
            note.decompose()
            continue
        outer = note.find(class_="ltx_note_outer")
        if outer:
            for dup in outer.select(".ltx_note_mark, .ltx_tag_note"):
                dup.decompose()


def _fix_citations(article) -> None:
    """In-text \\cite links should show the bibliography's item number,
    but when LaTeXML can't fully resolve a *numeric* citation style it
    falls back to printing the raw BibTeX key plus a stray trailing comma
    meant for an empty page/note field. The correct number is already
    sitting right there in the bibliography list's own tag, so recover it
    from there instead of showing the raw key.

    Only applies when that tag is actually a plain number -- author-year
    styles (natbib citep/citet) put the full "Author (Year)" label there
    instead, and those citation links are already correct as LaTeXML
    rendered them, so overwriting them with the full label would corrupt
    already-good text.
    """
    bib_number = {}
    for li in article.select(".ltx_bibitem[id]"):
        tag = li.find(class_="ltx_tag_bibitem")
        if not tag:
            continue
        text = tag.get_text(strip=True).strip("()")
        if text.isdigit():
            bib_number[li["id"]] = text

    for cite in article.find_all("cite", class_="ltx_cite"):
        for a in cite.find_all("a", class_="ltx_ref"):
            href = a.get("href", "")
            target_id = href[1:] if href.startswith("#") else ""
            if target_id in bib_number and not a.get_text(strip=True).isdigit():
                a.string = bib_number[target_id]
            else:
                a.string = a.get_text().rstrip(", ").rstrip(",")


_CITE_AUTHOR_PREFIX_RE = re.compile(r"^(?P<lead>[(;]\s*)?(?P<authors>.*?)(?P<trail>,\s*|\s*\(\s*)?$", re.S)


def _abbreviate_author_names(authors: str) -> str:
    """'Mohammadi Makrani et al.' -> 'Mohammadi Makrani'; 'Liu and Schafer'
    -> 'Liu'; 'Smith, Jones, and Lee' -> 'Smith' (fully spelled-out
    multi-author lists, no "et al."). Keeps just the first author."""
    first = re.split(r"\s+et\s+al\.?", authors.strip(), maxsplit=1)[0]
    first = re.split(r"\s+and\s+", first, maxsplit=1)[0]
    first = first.split(",")[0]
    return first.strip()


def _abbreviate_author_year_citations(article) -> None:
    """Numeric citation styles ("[1]") are already compact, but
    author-year styles (natbib \\citep/\\citet, e.g. "(Smith et al.,
    2020; Jones and Lee, 2019)") spell out every author list in full --
    for a citation group with several references that can run to
    multiple lines in the narrow reading column. Trim each one down to
    just the first author's surname, keeping the year (still linked to
    its bibliography entry) so the citation stays identifiable and
    clickable, just shorter.

    Detected per citation by looking at everything between this
    bibliography link and the previous one (or the start of the <cite>):
    numeric styles have nothing there but punctuation ("(", "; "), while
    author-year styles have an actual name -- only the latter gets
    touched. That span can be more than one node: biblatex/natbib often
    wrap just the "et al." suffix in its own
    <span class="ltx_bib_etal">, so the full author text isn't always one
    single text node immediately before the link.
    """
    for cite in article.find_all("cite", class_="ltx_cite"):
        for a in cite.find_all("a", class_="ltx_ref"):
            nodes = []
            node = a.previous_sibling
            while node is not None:
                if getattr(node, "name", None) == "a" and "ltx_ref" in (node.get("class") or []):
                    break
                nodes.append(node)
                node = node.previous_sibling
            if not nodes:
                continue
            nodes.reverse()
            combined = "".join(n.get_text() if hasattr(n, "get_text") else str(n) for n in nodes)
            m = _CITE_AUTHOR_PREFIX_RE.match(combined)
            if not m:
                continue
            authors = m.group("authors")
            if not re.search(r"[A-Za-z]", authors):
                continue  # numeric style -- just punctuation here, leave alone
            abbreviated = _abbreviate_author_names(authors)
            if not abbreviated or abbreviated == authors.strip():
                continue
            new_text = (m.group("lead") or "") + abbreviated + (m.group("trail") or "")
            nodes[0].replace_with(NavigableString(new_text))
            for extra in nodes[1:]:
                extra.extract()


def _strip_leaked_preamble_junk(article) -> None:
    """Undefined LaTeX macros in the preamble (e.g. algorithm2e's \\SetKw,
    acmart's \\setcctype when its binding is missing) can cause LaTeXML to
    emit their arguments as a stray paragraph before the title. Anything
    that lands as a sibling before the document title is preamble noise,
    never real body content, so drop it."""
    title = article.find("h1", class_="ltx_title_document")
    if not title:
        return
    for sib in list(title.find_previous_siblings()):
        sib.decompose()


_OUTLINE_LEVELS = {
    "ltx_title_document": 0,
    "ltx_title_abstract": 1,
    "ltx_title_section": 1,
    "ltx_title_subsection": 2,
    "ltx_title_subsubsection": 3,
    "ltx_title_bibliography": 1,
}


def _build_outline(article) -> list[tuple[int, str, str]]:
    """Collect (level, anchor_id, text) for the document title, abstract,
    section/subsection/subsubsection headings, and the bibliography, and
    make sure each has an `id` to link to -- most already do (LaTeXML
    puts one on the containing section), but not all (e.g. the abstract
    heading), so backfill a generated one where needed."""
    selector = ", ".join(f".{cls}" for cls in _OUTLINE_LEVELS)
    items: list[tuple[int, str, str]] = []
    counter = 0
    for heading in article.select(selector):
        classes = set(heading.get("class", []))
        level = next((lvl for cls, lvl in _OUTLINE_LEVELS.items() if cls in classes), None)
        if level is None:
            continue
        anchor_id = heading.get("id")
        if not anchor_id:
            container = heading.find_parent(["section", "div", "figure"])
            if container is not None and container.get("id"):
                anchor_id = container["id"]
        if not anchor_id:
            counter += 1
            anchor_id = f"outline-{counter}"
            heading["id"] = anchor_id
        text = heading.get_text(" ", strip=True).rstrip(".")
        if text:
            items.append((level, anchor_id, text))
    return items


def _render_outline_html(items: list[tuple[int, str, str]]) -> str:
    if not items:
        return '<p style="color:var(--muted); font-size:0.85em;">No sections found.</p>'
    parts = []
    for level, anchor_id, text in items:
        parts.append(
            f'<a href="#{html.escape(anchor_id, quote=True)}" style="--lvl:{max(level - 1, 0)}">'
            f"{html.escape(text)}</a>"
        )
    return "\n".join(parts)


def restyle(html_path: str, source_name: str = "", back_link: str = "") -> tuple[str, dict]:
    """Returns (page_html, metadata) -- metadata is the same dict used to
    populate the reader's own Info tab, handed back so callers (e.g. the
    library server) don't need to re-parse the output HTML just to get a
    title/authors for a listing.

    back_link, if given, is an href for a "back to library" button in the
    reader toolbar (e.g. "/" for the library server's home page). Left
    empty for standalone CLI output, which has nowhere to go back to."""
    base_dir = os.path.dirname(html_path)
    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "lxml")

    article = soup.find("article", class_="ltx_document")
    if article is None:
        article = soup.body

    _strip_leaked_preamble_junk(article)

    for err in article.find_all(class_="ltx_ERROR"):
        err.decompose()
    for el in article.select(".ltx_page_logo, .ltx_navigation, nav"):
        el.decompose()

    metadata = _extract_metadata(article)  # must run before _fix_notes decomposes the ACM front-matter notes

    _fix_notes(article)
    _fix_citations(article)
    _abbreviate_author_year_citations(article)
    _strip_latexml_scaling_wrappers(article)
    _wrap_tables(soup, article)
    _wrap_listings(soup, article)
    _inline_images(article, base_dir)

    title_el = article.find("h1", class_="ltx_title_document")
    title_text = title_el.get_text(strip=True) if title_el else (source_name or "Paper")

    outline_items = _build_outline(article)  # mutates article: backfills missing ids
    outline_html = _render_outline_html(outline_items)
    info_tab_html = _render_info_tab_html(metadata, source_name)

    export_meta = {
        "title": metadata.get("title") or title_text,
        "authors": metadata.get("authors", []),
        "venue": metadata.get("venue", ""),
        "year": metadata.get("year", ""),
        "doi": metadata.get("doi", ""),
        "source": source_name,
    }
    meta_json = json.dumps(export_meta).replace("</", "<\\/")

    body_html = str(article)

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_text}</title>
<style>{CSS}</style>
<script>{HEAD_INIT_SCRIPT}</script>
</head>
<body>
<script type="application/json" id="readerMetaData">{meta_json}</script>
<div class="reader-progress-track" id="readerProgressTrack">
  <div class="reader-progress-fill" id="readerProgressFill"></div>
</div>
<div class="reader-focus-bar" id="readerFocusBar" hidden></div>
<button class="reader-sidebar-toggle" id="sidebarToggleBtn" aria-label="Show outline" title="Show outline ([)">{_icon("menu")}</button>
<div class="reader-sidebar-backdrop" id="sidebarBackdrop"></div>
<div class="reader-sidebar-backdrop-right" id="sidebarRightBackdrop"></div>
<div class="reader-margin-comments" id="marginComments"></div>
<div class="reader-shell">
<aside class="reader-sidebar" id="readerSidebar">
  <div class="reader-sidebar-title">
    <span>Outline <kbd class="reader-kbd-hint">[</kbd></span>
    <button class="reader-sidebar-close" id="sidebarCloseBtn" aria-label="Close outline" title="Close outline ([)">{_icon("x", 16)}</button>
  </div>
  <nav class="reader-outline">
{outline_html}
  </nav>
  <div class="reader-sidebar-resize" id="sidebarResizeLeft"></div>
</aside>
<main class="page">
{f'<div class="meta">Rendered from {source_name}</div>' if source_name else ''}
{body_html}
</main>
<aside class="reader-sidebar-right" id="readerSidebarRight">
  <div class="reader-sidebar-resize" id="sidebarResizeRight"></div>
  <div class="reader-sidebar-right-header">
    <span>Notes <kbd class="reader-kbd-hint">]</kbd></span>
    <button class="reader-sidebar-close-right" id="sidebarRightCloseBtn" aria-label="Close panel" title="Close panel (])">{_icon("x", 16)}</button>
  </div>
  <div class="reader-tabs">
    <button class="reader-tab-btn active" data-tab="tabInfo">Info</button>
    <button class="reader-tab-btn" data-tab="tabHighlights">Highlights</button>
  </div>
  <div class="reader-tab-panel" id="tabInfo">
    <div class="reader-meta-title">{html.escape(metadata.get("title") or title_text)}</div>
{info_tab_html}
  </div>
  <div class="reader-tab-panel" id="tabHighlights" hidden>
    <div class="reader-hl-btn-row">
      <button class="reader-export-btn" id="exportMarkdownBtn">Download as Markdown</button>
      <button class="reader-export-btn" id="copyAllHighlightsBtn">Copy all</button>
    </div>
    <div id="highlightsList"><p class="reader-hl-empty">Select text in the paper to add a highlight.</p></div>
  </div>
</aside>
</div>

<div class="reader-toolbar">
{f'<a class="reader-back-btn" href="{html.escape(back_link)}" aria-label="Back to library">{_icon("arrow-left")}</a>' if back_link else ''}
  <button id="themeToggleBtn" aria-label="Toggle theme">{_icon("circle-half")}</button>
  <button id="textStyleBtn" aria-label="Text style">Aa</button>
  <button class="reader-notes-toggle" id="sidebarRightToggleBtn" aria-label="Show highlights and notes" title="Show highlights and notes (])">{_icon("edit")}</button>
</div>

<div class="reader-popover reader-popover-wide" id="textStylePopover" hidden>
  <div class="reader-popover-section-label">System theme</div>
  <div class="reader-theme-grid" id="themeGrid">
    <button type="button" class="reader-theme-card" data-value="light" aria-label="Light theme">
      <span class="reader-theme-swatch reader-theme-swatch-light">{_icon("sun", 20)}</span>
      <span>Light</span>
    </button>
    <button type="button" class="reader-theme-card" data-value="dark" aria-label="Dark theme">
      <span class="reader-theme-swatch reader-theme-swatch-dark">{_icon("moon", 20)}</span>
      <span>Dark</span>
    </button>
    <button type="button" class="reader-theme-card" data-value="auto" aria-label="Auto theme">
      <span class="reader-theme-swatch reader-theme-swatch-auto">{_icon("sun", 16)}{_icon("moon", 16)}</span>
      <span>Auto</span>
    </button>
  </div>

  <div class="reader-popover-section-label">Text styles</div>
  <div class="reader-popover-card">
    <div class="reader-popover-row reader-popover-row-click" id="typefaceRow">
      <span class="reader-popover-label">Typeface</span>
      <span class="reader-popover-value-btn">
        <span id="typefaceValueLabel">Serif</span>{_icon("chevron-right", 14)}
      </span>
    </div>
    <div class="reader-typeface-menu" id="typefaceMenu" hidden>
      <button type="button" data-value="serif">Serif</button>
      <button type="button" data-value="sans">Sans-serif</button>
    </div>
    <div class="reader-popover-row">
      <span class="reader-popover-label">Font size</span>
      <span class="reader-stepper">
        <button id="fontSizeDec" aria-label="Decrease text size">&minus;</button>
        <span id="fontSizeLabel">19px</span>
        <button id="fontSizeInc" aria-label="Increase text size">&plus;</button>
      </span>
    </div>
    <div class="reader-popover-row">
      <span class="reader-popover-label">Line spacing</span>
      <span class="reader-stepper">
        <button id="lineHeightDec" aria-label="Decrease line spacing">&minus;</button>
        <span id="lineHeightLabel">1.65</span>
        <button id="lineHeightInc" aria-label="Increase line spacing">&plus;</button>
      </span>
    </div>
    <div class="reader-popover-row">
      <span class="reader-popover-label">Line width</span>
      <span class="reader-stepper">
        <button id="maxWidthDec" aria-label="Decrease line width">&minus;</button>
        <span id="maxWidthLabel">Default</span>
        <button id="maxWidthInc" aria-label="Increase line width">&plus;</button>
      </span>
    </div>
  </div>
</div>

<div class="reader-selection-toolbar" id="selectionToolbar" hidden>
  <button class="reader-swatch" data-color="yellow" style="background:var(--highlight-yellow)" aria-label="Highlight yellow"></button>
  <button class="reader-swatch" data-color="green" style="background:var(--highlight-green)" aria-label="Highlight green"></button>
  <button class="reader-swatch" data-color="blue" style="background:var(--highlight-blue)" aria-label="Highlight blue"></button>
  <button class="reader-swatch" data-color="pink" style="background:var(--highlight-pink)" aria-label="Highlight pink"></button>
  <button class="reader-remove" aria-label="Remove highlight">{_icon("x", 14)}</button>
  <button class="reader-note-btn" aria-label="Add note">{_icon("message", 14)}</button>
</div>

<div class="reader-hl-handle reader-hl-handle-start" id="hlHandleStart" hidden></div>
<div class="reader-hl-handle reader-hl-handle-end" id="hlHandleEnd" hidden></div>

<div class="reader-citation-tooltip" id="citationTooltip" hidden>
  <span class="reader-citation-tooltip-num"></span>
  <span class="reader-citation-tooltip-text"></span>
</div>

<div class="reader-ref-preview" id="refPreviewTooltip" hidden>
  <div class="reader-ref-preview-body" id="refPreviewBody"></div>
  <div class="reader-ref-preview-caption" id="refPreviewCaption"></div>
</div>

<div class="reader-drop-overlay" id="readerDropOverlay" hidden>
  <div class="reader-drop-overlay-card">
    <strong>Drop to add to your library</strong>
    <div>.tex, .zip, .tar.gz, .tgz, .pdf &mdash; or a saved .html paper page</div>
  </div>
</div>
<div class="reader-upload-toast" id="readerUploadToast" hidden></div>

<script>{FIT_SCRIPT}</script>
<script>{READER_SCRIPT}</script>
</body>
</html>
"""
    return page, metadata
