import html

def get_palette_html(context: str = "home") -> str:
    return f"""
<style>
#cmdOverlay {{
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
  z-index: 10000;
  display: none;
  font-family: var(--reader-font-sans, system-ui, -apple-system, sans-serif);
}}
#cmdModal {{
  position: absolute; top: 15%; left: 50%; transform: translateX(-50%);
  width: 90%; max-width: 600px;
  background: var(--control-bg, var(--bg, #fff));
  border: 1px solid var(--rule, #ddd);
  border-radius: 12px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.3);
  overflow: hidden;
  display: flex; flex-direction: column;
}}
#cmdInput {{
  width: 100%; border: none; background: transparent;
  padding: 16px 20px; font-size: 1.1em; color: var(--fg, #000);
  outline: none;
  border-bottom: 1px solid var(--rule, #ddd);
}}
#cmdList {{
  max-height: 400px; overflow-y: auto;
  padding: 8px 0;
}}
.cmd-item {{
  padding: 10px 20px; cursor: pointer; color: var(--fg, #000);
  font-size: 0.95em;
  display: flex; justify-content: space-between; align-items: center;
}}
.cmd-item.selected, .cmd-item:hover {{
  background: var(--control-hover-bg, rgba(0, 0, 0, 0.05));
}}
.cmd-context {{
  font-size: 0.8em; color: var(--muted, #888);
}}
</style>

<div id="cmdOverlay">
  <div id="cmdModal">
    <input type="text" id="cmdInput" placeholder="Search commands..." autocomplete="off">
    <div id="cmdList"></div>
  </div>
</div>

<script>
(function() {{
  var overlay = document.getElementById("cmdOverlay");
  var input = document.getElementById("cmdInput");
  var list = document.getElementById("cmdList");
  var context = "{context}";
  var selectedIdx = 0;
  var visibleCmds = [];

  var commands = [
    {{ id: "home", title: "Go to Library Home", ctx: "global", action: function() {{ window.location.href = "/"; }} }},
    {{ id: "theme", title: "Toggle Theme (Light/Dark/Auto)", ctx: "global", action: function() {{
        var s = (typeof loadSettings === "function" ? loadSettings() : JSON.parse(localStorage.getItem("paper_reader_settings") || "{{}}"));
        var cur = s.theme || "auto";
        var next = cur === "auto" ? "light" : (cur === "light" ? "dark" : "auto");
        s.theme = next;
        if (typeof saveSettings === "function") saveSettings(s);
        else localStorage.setItem("paper_reader_settings", JSON.stringify(s));
        if (typeof applyTheme === "function") applyTheme(next);
        if (typeof setActiveTheme === "function") setActiveTheme(next);
        else window.location.reload();
    }} }},
    {{ id: "vim", title: "Toggle Vim Navigation", ctx: "global", action: function() {{
        var s = (typeof loadSettings === "function" ? loadSettings() : JSON.parse(localStorage.getItem("paper_reader_settings") || "{{}}"));
        s.vimNav = !s.vimNav;
        if (typeof saveSettings === "function") saveSettings(s);
        else localStorage.setItem("paper_reader_settings", JSON.stringify(s));
        var t = document.getElementById("prefsVimNav");
        if (t) t.checked = s.vimNav;
        if (context === "reader") window.location.reload();
    }} }}
  ];

  if (context === "home") {{
    commands.push({{ id: "prefs", title: "Open Preferences", ctx: "home", action: function() {{ var b = document.getElementById("prefsBtn"); if(b) b.click(); }} }});
    commands.push({{ id: "sync", title: "Sync with GitHub", ctx: "home", action: function() {{ var b = document.getElementById("prefsGitSyncBtn"); if(b) b.click(); }} }});
    commands.push({{ id: "pipe", title: "Pipeline Status", ctx: "home", action: function() {{ window.location.href = "/pipeline"; }} }});
    commands.push({{ id: "search", title: "Focus Search", ctx: "home", action: function() {{ var s = document.getElementById("searchInput"); if(s) s.focus(); }} }});
  }} else {{
    commands.push({{ id: "outline", title: "Toggle Outline Sidebar", ctx: "reader", action: function() {{ var b = document.getElementById("sidebarToggleBtn"); if(b) b.click(); else {{ var c = document.getElementById("sidebarCloseBtn"); if(c) c.click(); }} }} }});
    commands.push({{ id: "notes", title: "Toggle Notes Sidebar", ctx: "reader", action: function() {{ var b = document.getElementById("sidebarRightToggleBtn"); if(b) b.click(); else {{ var c = document.getElementById("sidebarRightCloseBtn"); if(c) c.click(); }} }} }});
    commands.push({{ id: "dl_md", title: "Download Highlights as Markdown", ctx: "reader", action: function() {{ var b = document.getElementById("exportMarkdownBtn"); if(b) b.click(); }} }});
    commands.push({{ id: "cp_hl", title: "Copy all Highlights", ctx: "reader", action: function() {{ var b = document.getElementById("copyAllHighlightsBtn"); if(b) b.click(); }} }});
  }}

  function render() {{
    var q = input.value.toLowerCase().trim();
    visibleCmds = commands.filter(function(c) {{ return c.title.toLowerCase().indexOf(q) !== -1; }});
    if (selectedIdx >= visibleCmds.length) selectedIdx = Math.max(0, visibleCmds.length - 1);
    
    list.innerHTML = "";
    visibleCmds.forEach(function(c, i) {{
      var div = document.createElement("div");
      div.className = "cmd-item" + (i === selectedIdx ? " selected" : "");
      div.innerHTML = "<span>" + escapeHtml(c.title) + "</span><span class='cmd-context'>" + escapeHtml(c.ctx) + "</span>";
      div.addEventListener("click", function() {{ close(); c.action(); }});
      div.addEventListener("mousemove", function() {{
        if (selectedIdx !== i) {{
          selectedIdx = i;
          renderListSelection();
        }}
      }});
      list.appendChild(div);
    }});
    renderListSelection();
  }}

  function escapeHtml(s) {{ return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }}

  function renderListSelection() {{
    var items = list.querySelectorAll(".cmd-item");
    items.forEach(function(el, i) {{
      if (i === selectedIdx) el.classList.add("selected");
      else el.classList.remove("selected");
    }});
    if (items[selectedIdx]) items[selectedIdx].scrollIntoView({{ block: "nearest" }});
  }}

  function open() {{
    overlay.style.display = "block";
    input.value = "";
    selectedIdx = 0;
    render();
    setTimeout(function() {{ input.focus(); }}, 10);
  }}

  function close() {{
    overlay.style.display = "none";
    input.blur();
  }}

  overlay.addEventListener("click", function(e) {{
    if (e.target === overlay) close();
  }});

  input.addEventListener("input", function() {{ selectedIdx = 0; render(); }});
  input.addEventListener("keydown", function(e) {{
    if (e.key === "ArrowDown") {{ e.preventDefault(); selectedIdx = Math.min(selectedIdx + 1, visibleCmds.length - 1); renderListSelection(); }}
    else if (e.key === "ArrowUp") {{ e.preventDefault(); selectedIdx = Math.max(selectedIdx - 1, 0); renderListSelection(); }}
    else if (e.key === "Enter") {{
      e.preventDefault();
      if (visibleCmds[selectedIdx]) {{
        var action = visibleCmds[selectedIdx].action;
        close();
        action();
      }}
    }}
    else if (e.key === "Escape") {{ close(); }}
  }});

  document.addEventListener("keydown", function(e) {{
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.isContentEditable) return;
    var s = (typeof loadSettings === "function" ? loadSettings() : JSON.parse(localStorage.getItem("paper_reader_settings") || "{{}}"));
    var triggerKey = (s.paletteShortcut || "p").toLowerCase();
    
    if (e.key.toLowerCase() === triggerKey && !e.ctrlKey && !e.metaKey && !e.altKey) {{
      e.preventDefault();
      open();
    }} else if (e.key === "Escape" && overlay.style.display === "block") {{
      close();
    }}
  }});
}})();
</script>
"""
