with open("paper_reader/server.py", "r") as f:
    s_content = f.read()

s_content = s_content.replace(
    "            self._send_html(HOME_PAGE_HTML)",
    "            from .palette import get_palette_html\n            self._send_html(HOME_PAGE_HTML.replace('</body>', get_palette_html('home') + '</body>'))"
)
s_content = s_content.replace(
    "            self._send_html(PIPELINE_PAGE_HTML)",
    "            from .palette import get_palette_html\n            self._send_html(PIPELINE_PAGE_HTML.replace('</body>', get_palette_html('home') + '</body>'))"
)

with open("paper_reader/server.py", "w") as f:
    f.write(s_content)


with open("paper_reader/restyle.py", "r") as f:
    r_content = f.read()

r_content = r_content.replace(
    "<script>{READER_JS}</script>\n</body>",
    "{get_palette_html('reader')}\n<script>{READER_JS}</script>\n</body>"
)
r_content = r_content.replace(
    "from typing import Any",
    "from typing import Any\nfrom .palette import get_palette_html"
)

with open("paper_reader/restyle.py", "w") as f:
    f.write(r_content)

print("Injected palette into server and restyle")
