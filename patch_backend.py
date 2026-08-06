import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# 1. Add fields to new papers
content = content.replace(
    '"status": "inbox",',
    '"status": "inbox",\n            "pinned": False,\n            "completed": False,'
)

# 2. Add _update_paper function
update_func = """
def _update_paper(paper_id: str, updates: dict) -> dict | None:
    items = _load_index()
    entry = next((e for e in items if e["id"] == paper_id), None)
    if entry is None:
        return None
    for k, v in updates.items():
        if k in ("pinned", "completed"):
            entry[k] = bool(v)
    _save_index(items)
    _trigger_git_sync()
    return entry
"""
content = content.replace(
    "def _set_paper_status",
    update_func + "\n\ndef _set_paper_status"
)

# 3. Add do_PATCH method
patch_method = """
    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/papers/"):
            paper_id = os.path.basename(parsed.path[len("/api/papers/") :])
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json({"error": "invalid JSON body"}, 400)
                return
            entry = _update_paper(paper_id, body)
            if entry is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json(entry)
        else:
            self._send_json({"error": "not found"}, 404)
"""
content = content.replace(
    "    def do_DELETE",
    patch_method + "\n    def do_DELETE"
)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Backend patched")
