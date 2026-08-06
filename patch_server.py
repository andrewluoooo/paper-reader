import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# 1. Add _trigger_git_sync before _delete_paper
sync_func = """
def _trigger_git_sync():
    import threading, subprocess
    lib = _get_library_dir()
    if not (lib / ".git").exists():
        return
    def sync_task():
        try:
            subprocess.run(["git", "add", "."], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "commit", "-m", "Auto-sync update"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "pull", "origin", "main", "--rebase", "--strategy-option=ours"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "push", "origin", "main"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    threading.Thread(target=sync_task, daemon=True).start()

def _delete_paper"""

content = content.replace("def _delete_paper", sync_func)

# 2. Add triggers inside mutations
content = content.replace(
    "    _save_index(remaining)\n    html_path =", 
    "    _save_index(remaining)\n    _trigger_git_sync()\n    html_path ="
)
content = content.replace(
    "    _save_index(items)\n    return entry", 
    "    _save_index(items)\n    _trigger_git_sync()\n    return entry"
)

# 3. Add Git endpoints to do_POST
post_endpoints = """
        elif parsed.path == "/api/git/setup":
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                import json, subprocess
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                url = body.get("url")
                if not url:
                    self._send_json({"error": "missing url"}, 400)
                    return
                lib = _get_library_dir()
                subprocess.run(["git", "init"], cwd=lib, check=False)
                subprocess.run(["git", "remote", "remove", "origin"], cwd=lib, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                res = subprocess.run(["git", "remote", "add", "origin", url], cwd=lib, capture_output=True, text=True)
                if res.returncode != 0:
                    self._send_json({"error": res.stderr}, 400)
                    return
                subprocess.run(["git", "fetch", "origin"], cwd=lib, check=False)
                subprocess.run(["git", "branch", "-M", "main"], cwd=lib, check=False)
                pull_res = subprocess.run(["git", "pull", "origin", "main", "--rebase"], cwd=lib, capture_output=True, text=True)
                if pull_res.returncode != 0 and "couldn't find remote ref" not in pull_res.stderr:
                    self._send_json({"error": pull_res.stderr}, 400)
                    return
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif parsed.path == "/api/git/sync":
            try:
                import subprocess
                lib = _get_library_dir()
                if not (lib / ".git").exists():
                    self._send_json({"error": "Not configured"}, 400)
                    return
                subprocess.run(["git", "add", "."], cwd=lib, check=False)
                subprocess.run(["git", "commit", "-m", "Manual sync update"], cwd=lib, check=False)
                pull = subprocess.run(["git", "pull", "origin", "main", "--rebase", "--strategy-option=ours"], cwd=lib, capture_output=True, text=True)
                push = subprocess.run(["git", "push", "origin", "main"], cwd=lib, capture_output=True, text=True)
                if push.returncode != 0:
                    self._send_json({"error": push.stderr}, 400)
                else:
                    self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:"""
content = content.replace("        else:\n            self._send_json({\"error\": \"not found\"}, 404)", post_endpoints, 1)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("patched")
