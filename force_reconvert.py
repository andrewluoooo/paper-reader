import sys, os
sys.path.insert(0, os.getcwd())
from paper_reader.server import _load_index, _save_index, restyle, LIBRARY_DIR
from paper_reader.latex_convert import convert

papers = _load_index()
entry = next(p for p in papers if p['id'] == '62707ff93b63')
src_tex = "/Users/andrewluo/.paper_reader_library/raw/62707ff93b63/src/conference_101719.tex"
workdir = "/Users/andrewluo/.paper_reader_library/raw/62707ff93b63"

# Re-run convert
print("Converting...")
raw_html_path = convert(src_tex, workdir)
print(f"Raw HTML saved to {raw_html_path}")

# Re-run restyle
print("Restyling...")
html_out, metadata = restyle(raw_html_path, source_name=entry['sourceFilename'], back_link="/")
(LIBRARY_DIR / f"{entry['id']}.html").write_text(html_out, encoding="utf-8")

# Update entry
entry['title'] = metadata.get('title') or entry['sourceFilename']
entry['authors'] = [a["name"] for a in metadata.get("authors", [])]
entry['venue'] = metadata.get('venue', '')
entry['summary'] = (metadata.get('abstract') or '').strip()[:320]

_save_index(papers)
print("Done")
