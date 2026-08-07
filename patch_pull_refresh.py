import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# Add momentum tracking variables
old_vars = """    var pullAmount = 0;
    var cooldownUntil = 0;
    var releaseTimer = null;
    var hideTimer = null;
    var refreshing = false;"""

new_vars = """    var pullAmount = 0;
    var cooldownUntil = 0;
    var releaseTimer = null;
    var hideTimer = null;
    var refreshing = false;
    var momentumBlock = false;
    var momentumTimer = null;"""

content = content.replace(old_vars, new_vars)

# Update wheel listener
old_wheel = """    window.addEventListener("wheel", function (e) {
      if (refreshing || Date.now() < cooldownUntil) return;
      if (e.target && e.target.closest && e.target.closest(".sidebar, .info-panel")) return;
      if (window.scrollY > 0) {
        if (pullAmount) cancelPull();
        return;
      }
      if (e.deltaY < 0) {"""

new_wheel = """    window.addEventListener("wheel", function (e) {
      if (refreshing || Date.now() < cooldownUntil) return;
      if (e.target && e.target.closest && e.target.closest(".sidebar, .info-panel")) return;
      
      if (momentumTimer) clearTimeout(momentumTimer);
      momentumTimer = setTimeout(function() { momentumBlock = false; }, 300);
      
      if (window.scrollY > 0) {
        momentumBlock = true;
        if (pullAmount) cancelPull();
        return;
      }
      if (momentumBlock) return;
      
      if (e.deltaY < 0) {"""

content = content.replace(old_wheel, new_wheel)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Pull refresh patched")
