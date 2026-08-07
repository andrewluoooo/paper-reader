import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# Remove the event listener block
event_listener_regex = r'\s*document\.getElementById\("infoCloseBtn"\)\.addEventListener\("click", function \(\) \{\s*closeInfoPanel\(\);\s*render\(document\.getElementById\("searchBox"\)\.value\);\s*\}\);'
content = re.sub(event_listener_regex, '', content)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Close button JS removed")
