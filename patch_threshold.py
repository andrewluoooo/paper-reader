import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

content = content.replace("var PULL_THRESHOLD = 170;", "var PULL_THRESHOLD = 300;")

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Threshold patched")
