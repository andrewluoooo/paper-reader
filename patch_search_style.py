import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

old_search_html = """    <div class="search-row" id="searchRow" hidden>
      <input type="text" id="searchBox" placeholder="Search papers by title or author...">
      <button type="button" class="icon-btn" id="searchCancelBtn" aria-label="Cancel search" style="border: 1px solid var(--rule); border-radius: 6px; padding: 0 0.8em;">&times;</button>
    </div>"""

new_search_html = """    <div class="search-row" id="searchRow" style="position: relative;" hidden>
      <input type="text" id="searchBox" placeholder="Search papers by title or author..." style="padding-right: 32px;">
      <button type="button" id="searchCancelBtn" aria-label="Cancel search" style="position: absolute; right: 6px; top: 50%; transform: translateY(-50%); background: transparent; border: none; font-size: 1.2em; color: var(--muted); cursor: pointer; padding: 0.2em 0.4em; display: flex; align-items: center; justify-content: center;">&times;</button>
    </div>"""

content = content.replace(old_search_html, new_search_html)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Search style patched")
