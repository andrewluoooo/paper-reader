import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# 1. Add Completed button
content = content.replace(
    '<button type="button" class="tab-btn" role="tab" data-status="later">Later</button>',
    '<button type="button" class="tab-btn" role="tab" data-status="later">Later</button>\n        <button type="button" class="tab-btn" role="tab" data-status="completed">Completed</button>'
)

# 2. Add TAB_EMPTY_MESSAGES entry
content = content.replace(
    'later: "No papers saved for later."',
    'later: "No papers saved for later.",\n    completed: "No completed papers yet."'
)

# 3. Update filter logic
new_filter_logic = """
    var filtered = papers.filter(function (p) {
      if (currentTab === "completed") {
        if (!p.completed || p.status === "trash") return false;
      } else {
        if ((p.status || "inbox") !== currentTab) return false;
        if (p.completed && (currentTab === "inbox" || currentTab === "later")) return false;
      }
      if (q) {
"""

content = re.sub(
    r'    var filtered = papers\.filter\(function \(p\) \{\s*if \(\(p\.status \|\| "inbox"\) !== currentTab\) return false;\s*if \(q\) \{',
    new_filter_logic,
    content
)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Tab patched")
