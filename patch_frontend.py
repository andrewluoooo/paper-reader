import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# 1. CSS for completed card
css = """
.paper-card.completed { opacity: 0.65; }
.paper-card.completed:hover { opacity: 1; }
.nav-pinned-empty { padding: 0.5em 1.2em; font-size: 0.8em; color: var(--muted); }
"""
content = content.replace(
    ".paper-card.selected { border-color: var(--accent); }",
    ".paper-card.selected { border-color: var(--accent); }\n" + css
)

# 2. Add renderSidebarPinned
render_pinned_fn = """
  function renderSidebarPinned() {
    var wrap = document.getElementById("navPinned");
    var label = document.getElementById("navPinnedLabel");
    if (!wrap || !label) return;
    
    var pinnedPapers = papers.filter(function(p) { return p.pinned; });
    
    if (pinnedPapers.length > 0) {
      wrap.hidden = false;
      label.hidden = false;
    } else {
      wrap.hidden = true;
      label.hidden = true;
    }
    
    wrap.innerHTML = "";
    pinnedPapers.forEach(function(p) {
      var item = document.createElement("a");
      item.className = "nav-tag-item";
      item.href = "/library/" + encodeURIComponent(p.id) + ".html";
      item.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:0.4em"><path d="m21 16-5.5-5.5"></path><path d="M15.5 10.5 12 7l-5.5 5.5"></path><path d="m3 21 6-6"></path></svg><span>' + escHtml(p.title || "Untitled") + '</span>';
      wrap.appendChild(item);
    });
  }

  function updatePaper(p, updates) {
    fetch("/api/papers/" + encodeURIComponent(p.id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates)
    }).then(function (r) {
      if (r.ok) loadPapers();
    });
  }
"""
content = content.replace(
    "  function updateTags(p, newTags) {",
    render_pinned_fn + "\n  function updateTags(p, newTags) {"
)

# 3. Add to loadPapers
content = content.replace(
    "renderSidebarTags();",
    "renderSidebarTags();\n      renderSidebarPinned();"
)

# 4. Add items to more-menu
more_items = """
    var pinItem = document.createElement("button");
    pinItem.type = "button";
    pinItem.textContent = p.pinned ? "Unpin from sidebar" : "Pin to sidebar";
    pinItem.addEventListener("click", function (e) {
      e.preventDefault(); e.stopPropagation(); closeMoreMenu();
      updatePaper(p, { pinned: !p.pinned });
    });
    
    var completeItem = document.createElement("button");
    completeItem.type = "button";
    completeItem.textContent = p.completed ? "Mark as unread" : "Mark as complete";
    completeItem.addEventListener("click", function (e) {
      e.preventDefault(); e.stopPropagation(); closeMoreMenu();
      updatePaper(p, { completed: !p.completed });
    });
    
    menu.appendChild(infoItem);
    menu.appendChild(pinItem);
    menu.appendChild(completeItem);
"""
content = content.replace(
    "    menu.appendChild(infoItem);",
    more_items
)

# 5. Card CSS class + checkmark for completed
card_title_logic = """
      var titleEl = document.createElement("div");
      titleEl.className = "paper-title";
      if (p.completed) {
        var checkEl = document.createElement("span");
        checkEl.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="color: var(--accent); margin-right: 5px; vertical-align: -2px;"><polyline points="20 6 9 17 4 12"></polyline></svg>';
        titleEl.appendChild(checkEl);
      }
      titleEl.appendChild(document.createTextNode(p.title));
      a.appendChild(titleEl);
"""
content = content.replace(
    "card.className = \"paper-card\" + (selectedPaper && selectedPaper.id === p.id ? \" selected\" : \"\");",
    "card.className = \"paper-card\" + (selectedPaper && selectedPaper.id === p.id ? \" selected\" : \"\") + (p.completed ? \" completed\" : \"\");"
)
content = re.sub(
    r'var titleEl = document.createElement\("div"\);\s*titleEl.className = "paper-title";\s*titleEl.textContent = p.title;\s*a.appendChild\(titleEl\);',
    card_title_logic,
    content
)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Frontend patched")
