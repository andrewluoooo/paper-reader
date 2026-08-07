import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

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

content = content.replace(
    "  setupDragDropZones();\n\n  loadPapers();",
    js + "\n  setupDragDropZones();\n\n  loadPapers();"
)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Collapsible JS patched")
