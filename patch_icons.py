import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# 1. Define icons
icons = """
  var PIN_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21 16-5.5-5.5"></path><path d="M15.5 10.5 12 7l-5.5 5.5"></path><path d="m3 21 6-6"></path></svg>';
  var CHECK_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"></polyline></svg>';
"""
content = content.replace(
    'var STATUS_ACTIONS = [',
    icons + '\n  var STATUS_ACTIONS = ['
)

# 2. Add buttons to actions bar
new_buttons = """
      var completeBtn = document.createElement("button");
      completeBtn.type = "button";
      completeBtn.className = "paper-action-btn" + (p.completed ? " active" : "");
      completeBtn.setAttribute("aria-label", p.completed ? "Mark as unread" : "Mark as complete");
      completeBtn.title = p.completed ? "Mark as unread" : "Mark as complete";
      completeBtn.innerHTML = CHECK_ICON;
      completeBtn.addEventListener("click", function(e) {
        e.preventDefault(); e.stopPropagation();
        updatePaper(p, { completed: !p.completed });
      });
      actions.appendChild(completeBtn);
      
      var pinBtn2 = document.createElement("button");
      pinBtn2.type = "button";
      pinBtn2.className = "paper-action-btn" + (p.pinned ? " active" : "");
      pinBtn2.setAttribute("aria-label", p.pinned ? "Unpin from sidebar" : "Pin to sidebar");
      pinBtn2.title = p.pinned ? "Unpin from sidebar" : "Pin to sidebar";
      pinBtn2.innerHTML = PIN_ICON;
      pinBtn2.addEventListener("click", function(e) {
        e.preventDefault(); e.stopPropagation();
        updatePaper(p, { pinned: !p.pinned });
      });
      actions.appendChild(pinBtn2);
"""

content = content.replace(
    'actions.appendChild(moreWrap);',
    'actions.appendChild(moreWrap);\n' + new_buttons
)

# 3. Remove buttons from more-menu
# This regex removes the previously injected pinItem and completeItem block.
remove_more_buttons = re.compile(
    r'\s*var pinItem = document.createElement\("button"\);.*?menu\.appendChild\(completeItem\);',
    re.DOTALL
)
content = remove_more_buttons.sub('', content)


with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Icons added")
