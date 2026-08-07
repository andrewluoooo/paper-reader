import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

old_code = """      if (momentumTimer) clearTimeout(momentumTimer);
      momentumTimer = setTimeout(function() { momentumBlock = false; }, 300);
      
      if (window.scrollY > 0) {"""

new_code = """      var mainCol = document.querySelector(".main-col");
      var currentScrollY = mainCol ? mainCol.scrollTop : 0;
      
      if (momentumTimer) clearTimeout(momentumTimer);
      momentumTimer = setTimeout(function() { momentumBlock = false; }, 300);
      
      if (currentScrollY > 0) {"""

content = content.replace(old_code, new_code)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Scroll checked patched")
