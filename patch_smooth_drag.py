import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# 1. Add setDragImage and prevent link dragging
new_dragstart = """      card.addEventListener("dragstart", function(e) {
        e.dataTransfer.setData("application/x-paper-id", p.id);
        e.dataTransfer.effectAllowed = "move";
        
        var rect = card.getBoundingClientRect();
        e.dataTransfer.setDragImage(card, e.clientX - rect.left, e.clientY - rect.top);
        
        setTimeout(function() { card.classList.add("dragging"); }, 0);
      });"""

content = content.replace(
    '      card.addEventListener("dragstart", function(e) {\n        e.dataTransfer.setData("application/x-paper-id", p.id);\n        e.dataTransfer.effectAllowed = "move";\n        setTimeout(function() { card.classList.add("dragging"); }, 0);\n      });',
    new_dragstart
)

# 2. Add a.draggable = false
content = content.replace(
    'a.href = "/library/" + encodeURIComponent(p.id) + ".html";',
    'a.href = "/library/" + encodeURIComponent(p.id) + ".html";\n      a.draggable = false;'
)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Smooth drag patched")
