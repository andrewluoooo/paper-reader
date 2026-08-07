import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# Add button HTML
old_search_html = """    <div class="search-row" id="searchRow" hidden>
      <input type="text" id="searchBox" placeholder="Search papers by title or author...">
    </div>"""

new_search_html = """    <div class="search-row" id="searchRow" hidden>
      <input type="text" id="searchBox" placeholder="Search papers by title or author...">
      <button type="button" class="icon-btn" id="searchCancelBtn" aria-label="Cancel search" style="border: 1px solid var(--rule); border-radius: 6px; padding: 0 0.8em;">&times;</button>
    </div>"""

content = content.replace(old_search_html, new_search_html)

# Add event listener
old_js = """  document.getElementById("navSearchBtn").addEventListener("click", function () {
    searchOpen = !searchOpen;
    searchRow.hidden = !searchOpen;
    if (searchOpen) searchBox.focus();
    else { searchBox.value = ""; render(""); }
  });"""

new_js = """  document.getElementById("navSearchBtn").addEventListener("click", function () {
    searchOpen = !searchOpen;
    searchRow.hidden = !searchOpen;
    if (searchOpen) searchBox.focus();
    else { searchBox.value = ""; render(""); }
  });
  document.getElementById("searchCancelBtn").addEventListener("click", function () {
    searchOpen = false;
    searchRow.hidden = true;
    searchBox.value = "";
    render("");
  });"""

content = content.replace(old_js, new_js)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Search cancel button patched")
