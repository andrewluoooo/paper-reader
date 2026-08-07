import re

with open("paper_reader/server.py", "r") as f:
    content = f.read()

tabs_js_old = """  var tabs = document.querySelectorAll(".tab-btn");
  if (tabs.length >= 4) {
    tabs[0].innerHTML = INBOX_ICON + "<span>Inbox</span>";
    tabs[1].innerHTML = LATER_ICON + "<span>Later</span>";
    tabs[2].innerHTML = ARCHIVE_ICON + "<span>Archive</span>";
    tabs[3].innerHTML = TRASH_ICON + "<span>Trash</span>";
  }"""

tabs_js_new = """  var tabs = document.querySelectorAll(".tab-btn");
  if (tabs.length >= 5) {
    tabs[0].innerHTML = INBOX_ICON + "<span>Inbox</span>";
    tabs[1].innerHTML = LATER_ICON + "<span>Later</span>";
    tabs[2].innerHTML = CHECK_ICON + "<span>Completed</span>";
    tabs[3].innerHTML = ARCHIVE_ICON + "<span>Archive</span>";
    tabs[4].innerHTML = TRASH_ICON + "<span>Trash</span>";
  }"""

content = content.replace(tabs_js_old, tabs_js_new)

with open("paper_reader/server.py", "w") as f:
    f.write(content)
print("Tabs JS patched")
