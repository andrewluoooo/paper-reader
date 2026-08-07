import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# 1. Update HTML
old_pinned_html = '<div class="nav-section-label" id="navPinnedLabel" hidden>Pinned</div>'
new_pinned_html = '<div class="nav-section-label" id="navPinnedLabel" style="cursor:pointer; display:flex; justify-content:space-between; align-items:center; user-select:none;" hidden>Pinned <svg class="collapse-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg></div>'
content = content.replace(old_pinned_html, new_pinned_html)

old_tags_html = '<div class="nav-section-label">Tags</div>'
new_tags_html = '<div class="nav-section-label" id="navTagsLabel" style="cursor:pointer; display:flex; justify-content:space-between; align-items:center; user-select:none;">Tags <svg class="collapse-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg></div>'
content = content.replace(old_tags_html, new_tags_html)

# 2. Add CSS
css = """
.collapse-icon { transition: transform 0.2s ease; opacity: 0.5; }
.nav-section-label:hover .collapse-icon { opacity: 1; }
.nav-section-label.collapsed .collapse-icon { transform: rotate(-90deg); }
"""
content = content.replace(
    ".nav-tags-empty { padding: 0.4em 0.6em; font-size: 0.82em; color: var(--muted); }",
    ".nav-tags-empty { padding: 0.4em 0.6em; font-size: 0.82em; color: var(--muted); }\n" + css
)

# 3. Add JS
js = """
  function initCollapsible(labelId, contentId, storageKey) {
    var label = document.getElementById(labelId);
    var content = document.getElementById(contentId);
    if (!label || !content) return;
    
    var isCollapsed = localStorage.getItem(storageKey) === "true";
    if (isCollapsed) {
      label.classList.add("collapsed");
      content.style.display = "none";
    }
    
    label.addEventListener("click", function() {
      isCollapsed = !isCollapsed;
      localStorage.setItem(storageKey, isCollapsed);
      if (isCollapsed) {
        label.classList.add("collapsed");
        content.style.display = "none";
      } else {
        label.classList.remove("collapsed");
        content.style.display = "";
      }
    });
  }
  
  initCollapsible("navPinnedLabel", "navPinned", "paper_reader_pinned_collapsed");
  initCollapsible("navTagsLabel", "navTags", "paper_reader_tags_collapsed");
"""

content = content.replace("  setupDragDropZones();\n  loadPapers();", js + "\n  setupDragDropZones();\n  loadPapers();")

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Collapsible sections patched")
