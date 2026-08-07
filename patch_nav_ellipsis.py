import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

content = content.replace(
    ".nav-tag-item.active { background: var(--accent); color: #fff; }",
    ".nav-tag-item.active { background: var(--accent); color: #fff; }\n.nav-tag-item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }"
)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Ellipsis patched")
