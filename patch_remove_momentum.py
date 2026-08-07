import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

# Remove momentum block variables
content = content.replace("    var momentumBlock = false;\n    var momentumTimer = null;", "")

# Replace the wheel event logic
old_wheel = """      var mainCol = document.querySelector(".main-col");
      var currentScrollY = mainCol ? mainCol.scrollTop : 0;
      
      if (momentumTimer) clearTimeout(momentumTimer);
      momentumTimer = setTimeout(function() { momentumBlock = false; }, 300);
      
      if (currentScrollY > 0) {
        momentumBlock = true;
        if (pullAmount) cancelPull();
        return;
      }
      if (momentumBlock) return;"""

new_wheel = """      var mainCol = document.querySelector(".main-col");
      var currentScrollY = mainCol ? mainCol.scrollTop : 0;
      
      if (currentScrollY > 0) {
        if (pullAmount) cancelPull();
        return;
      }"""

content = content.replace(old_wheel, new_wheel)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Momentum block removed")
