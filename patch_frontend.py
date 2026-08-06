with open("paper_reader/server.py", "r") as f:
    content = f.read()

pull_to_refresh_logic = """
      arrow.hidden = true;
      spinner.hidden = false;
      spinner.classList.add("spinning");
      label.hidden = false;
      label.textContent = "Refreshing…";

      // Trigger sync if configured, then refresh papers
      if (loadSettings().gitUrl) {
        label.textContent = "Syncing & Refreshing…";
        fetch("/api/git/sync", { method: "POST" })
          .then(function() { loadPapers(); })
          .catch(function() { loadPapers(); })
          .finally(function() {
            setTimeout(function () {
              refreshing = false;
              hide(300);
            }, 10);
          });
      } else {
        loadPapers();
        // Hold the "Refreshing..." state briefly even though the fetch
        // itself is usually near-instant, so it doesn't just flash by.
        setTimeout(function () {
          refreshing = false;
          hide(300);
        }, 10);
      }
"""

old_logic = """
      arrow.hidden = true;
      spinner.hidden = false;
      spinner.classList.add("spinning");
      label.hidden = false;
      label.textContent = "Refreshing\\u2026";
      loadPapers();
      // Hold the "Refreshing..." state briefly even though the fetch
      // itself is usually near-instant, so it doesn't just flash by.
      setTimeout(function () {
        refreshing = false;
        hide(300);
      }, 10);
"""

# Note: The delay might be different, let's verify what it was
content = content.replace("      arrow.hidden = true;\n      spinner.hidden = false;\n      spinner.classList.add(\"spinning\");\n      label.hidden = false;\n      label.textContent = \"Refreshing\\u2026\";\n      loadPapers();\n      // Hold the \"Refreshing...\" state briefly even though the fetch\n      // itself is usually near-instant, so it doesn't just flash by.\n      setTimeout(function () {\n        refreshing = false;\n        hide(300);\n      }, 10);", pull_to_refresh_logic)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("patched frontend")
