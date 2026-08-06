with open("paper_reader/server.py", "r") as f:
    content = f.read()

content = content.replace(
    "    entry[\"status\"] = status\n    entry[\"deletedAt\"] = time.time() if status == \"trash\" else None\n    _save_index(items)\n    return entry",
    "    entry[\"status\"] = status\n    entry[\"deletedAt\"] = time.time() if status == \"trash\" else None\n    _save_index(items)\n    _trigger_git_sync()\n    return entry"
)

content = content.replace(
    "        self._send_json(entry)\n\n    def log_message",
    "        _trigger_git_sync()\n        self._send_json(entry)\n\n    def log_message"
)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("patched more")
