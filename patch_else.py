with open("paper_reader/server.py", "r") as f:
    content = f.read()

content = content.replace("        else:\n\n    def do_DELETE", "        else:\n            self._send_json({\"error\": \"not found\"}, 404)\n\n    def do_DELETE")

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("patched else")
