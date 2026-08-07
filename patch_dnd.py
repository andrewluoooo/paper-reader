import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# 1. Update renderSidebarPinned
old_render_pinned = """  function renderSidebarPinned() {
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
  }"""

new_render_pinned = """  function renderSidebarPinned() {
    var wrap = document.getElementById("navPinned");
    var label = document.getElementById("navPinnedLabel");
    if (!wrap || !label) return;
    
    var pinnedPapers = papers.filter(function(p) { return p.pinned; });
    wrap.hidden = false;
    label.hidden = false;
    
    wrap.innerHTML = "";
    if (pinnedPapers.length > 0) {
      pinnedPapers.forEach(function(p) {
        var item = document.createElement("a");
        item.className = "nav-tag-item";
        item.href = "/library/" + encodeURIComponent(p.id) + ".html";
        item.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:0.4em"><path d="m21 16-5.5-5.5"></path><path d="M15.5 10.5 12 7l-5.5 5.5"></path><path d="m3 21 6-6"></path></svg><span>' + escHtml(p.title || "Untitled") + '</span>';
        wrap.appendChild(item);
      });
    } else {
      wrap.innerHTML = '<div class="nav-pinned-empty">Drop papers here to pin</div>';
    }
  }"""

content = content.replace(old_render_pinned, new_render_pinned)

# 2. Add draggable logic to cards
old_card_init = 'card.className = "paper-card" + (selectedPaper && selectedPaper.id === p.id ? " selected" : "") + (p.completed ? " completed" : "");\n      card.dataset.paperId = p.id;'
new_card_init = """card.className = "paper-card" + (selectedPaper && selectedPaper.id === p.id ? " selected" : "") + (p.completed ? " completed" : "");
      card.dataset.paperId = p.id;
      card.draggable = true;
      card.addEventListener("dragstart", function(e) {
        e.dataTransfer.setData("application/x-paper-id", p.id);
        e.dataTransfer.effectAllowed = "move";
        setTimeout(function() { card.classList.add("dragging"); }, 0);
      });
      card.addEventListener("dragend", function(e) {
        card.classList.remove("dragging");
      });"""

content = content.replace(old_card_init, new_card_init)

# 3. Add dragdrop zones setup
setup_dnd = """
  function setupDragDropZones() {
    var zones = [];
    document.querySelectorAll(".tab-btn").forEach(function(el) { zones.push(el); });
    var navPinned = document.getElementById("navPinned");
    var navPinnedLabel = document.getElementById("navPinnedLabel");
    if (navPinned) zones.push(navPinned);
    if (navPinnedLabel) zones.push(navPinnedLabel);

    zones.forEach(function(zone) {
      zone.addEventListener("dragenter", function(e) {
        if (e.dataTransfer.types.indexOf("application/x-paper-id") !== -1) {
          e.preventDefault();
          zone.classList.add("drag-over");
        }
      });
      zone.addEventListener("dragover", function(e) {
        if (e.dataTransfer.types.indexOf("application/x-paper-id") !== -1) {
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
        }
      });
      zone.addEventListener("dragleave", function(e) {
        zone.classList.remove("drag-over");
      });
      zone.addEventListener("drop", function(e) {
        zone.classList.remove("drag-over");
        var paperId = e.dataTransfer.getData("application/x-paper-id");
        if (!paperId) return;
        
        var p = papers.filter(function(x) { return x.id === paperId; })[0];
        if (!p) return;
        
        if (zone === navPinned || zone === navPinnedLabel) {
          if (!p.pinned) updatePaper(p, { pinned: true });
        } else if (zone.classList.contains("tab-btn")) {
          var targetStatus = zone.dataset.status;
          if (targetStatus === "completed") {
            if (!p.completed) updatePaper(p, { completed: true });
          } else {
            if (p.status !== targetStatus) updatePaperStatus(p, targetStatus);
          }
        }
      });
    });
  }
  setupDragDropZones();
"""
content = content.replace("loadPapers();\n})();", setup_dnd + "\n  loadPapers();\n})();")

# 4. Add CSS
css = """
.paper-card.dragging { opacity: 0.5; }
.tab-btn.drag-over, #navPinned.drag-over, #navPinnedLabel.drag-over { background-color: var(--rule); border-color: var(--accent); }
#navPinned.drag-over { border-radius: 6px; border: 1px dashed var(--accent); }
"""
content = content.replace(
    ".paper-card.completed:hover { opacity: 1; }",
    ".paper-card.completed:hover { opacity: 1; }\n" + css
)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Drag and drop patched")
