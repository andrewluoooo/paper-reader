import re

with open("paper_reader/latex_convert.py", "r") as f:
    content = f.read()

old_cmd = """    # latexmlc's own default timeout (600s) is too tight for long papers with
    # many figures/citations -- raise it rather than have a real, otherwise
    # fine conversion get cut off mid-way and silently produce an empty stub.
    cmd = ["latexmlc", f"--dest={out_html.name}", "--format=html5", "--timeout=1800", main_tex.name]"""

new_cmd = """    # latexmlc's own default timeout (600s) is too tight for long papers with
    # many figures/citations -- raise it rather than have a real, otherwise
    # fine conversion get cut off mid-way and silently produce an empty stub.
    relax_ltxml = main_tex.parent / "relax_errors.ltxml"
    relax_ltxml.write_text("package LaTeXML::Package::Pool;\\nAssignValue(MAX_ERRORS => 10000, 'global');\\n1;\\n", encoding="utf-8")
    cmd = ["latexmlc", f"--dest={out_html.name}", "--format=html5", "--timeout=1800", "--preload=relax_errors.ltxml", main_tex.name]"""

content = content.replace(old_cmd, new_cmd)

with open("paper_reader/latex_convert.py", "w") as f:
    f.write(content)
print("Latexmlc command patched")
