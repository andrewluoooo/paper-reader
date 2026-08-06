import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# 1. Update PIN_ICON to actual pin
new_pin_icon = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 11.2V6a3 3 0 0 0-6 0v5.2a2 2 0 0 1-1.11 1.35l-1.78.9A2 2 0 0 0 5 15.24Z"></path></svg>'
content = re.sub(
    r'var PIN_ICON = \'<svg width="16" height="16".*?</svg>\';',
    f"var PIN_ICON = '{new_pin_icon}';",
    content
)

# 2. Add green active color for pin button
content = content.replace(
    ".paper-action-btn.active { color: var(--accent); }",
    ".paper-action-btn.active { color: var(--accent); }\n.paper-action-btn.pin-btn.active { color: #10b981; fill: #10b981; }"
)

# 3. Add 'pin-btn' class to the pin action button
content = content.replace(
    'pinBtn2.className = "paper-action-btn" + (p.pinned ? " active" : "");',
    'pinBtn2.className = "paper-action-btn pin-btn" + (p.pinned ? " active" : "");'
)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Pin icon patched")
