import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# Remove the HTML button
content = re.sub(
    r'\s*<button type="button" class="icon-btn" id="infoCloseBtn" aria-label="Close info panel">&times;</button>',
    '',
    content
)

# Remove the event listener block
event_listener_regex = r'\s*document\.getElementById\("infoCloseBtn"\)\.addEventListener\("click", function \(\) \{\s*closeInfoPanel\(\);\s*\}\);'
content = re.sub(event_listener_regex, '', content)

# Remove the icon injection
content = re.sub(
    r'\s*document\.getElementById\("infoCloseBtn"\)\.innerHTML = X_ICON;',
    '',
    content
)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Close button removed")
