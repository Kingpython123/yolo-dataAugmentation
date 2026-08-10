"""One-off: rewrite .gitignore cleanly, de-duplicated, with correct characters.

Explicit UTF-8 read/write, no BOM, no newline translation.
"""
import io
import pathlib

path = pathlib.Path(__file__).resolve().parents[1] / ".gitignore"

content = """# ---- Python ----
.venv/
__pycache__/
*.py[cod]
*.egg-info/

# ---- IDE ----
.idea/
.vscode/

# ---- \u4e34\u65f6\u811a\u672c ----
_tmp_*.py
tmp_*.txt
tmp_*.log

# ---- \u6570\u636e\u96c6(\u4f53\u79ef\u5927, \u901a\u8fc7\u7f51\u76d8/\u5171\u4eab\u76d8\u5206\u53d1, \u4e0d\u8fdb git) ----
/\u5b9e\u62cd\u6b63\u6837\u672c\uff08\u6709\u7f3a\u9677\uff09/
/\u5b9e\u62cd\u8d1f\u6837\u672c\uff08\u65e0\u7f3a\u9677\uff09/
../\u5b9e\u62cd\u6b63\u6837\u672c\uff08\u6709\u7f3a\u9677\uff09/
../\u5b9e\u62cd\u8d1f\u6837\u672c\uff08\u65e0\u7f3a\u9677\uff09/

# ---- \u8fd0\u884c\u4ea7\u7269(\u6bcf\u4eba\u672c\u5730\u751f\u6210, \u4e0d\u8fdb git) ----
outputs/generated/
outputs/rejected/
outputs/masks/
outputs/debug/
outputs/debug_preview/
outputs/seg_preview/
outputs/selftest/
outputs/gen_ref_review/
outputs/structure_preview/
outputs/annotations.jsonl
outputs/annotations.jsonl.bak
outputs/fail_log.jsonl
outputs/catalog/catalog.json.bak

# ---- \u672c\u5730\u79c1\u6709\u914d\u7f6e(\u5b58 API key \u7528, \u6c38\u4e0d\u63d0\u4ea4) ----
config.local.yaml
.env

# ---- \u7ed3\u679c\u538b\u7f29\u5305(\u4ea4\u4ed8\u7528, \u4e0d\u8fdb git) ----
*.rar
*.zip
*.7z

# ---- \u684c\u9762\u7a0b\u5e8f\u7684\u8fd0\u884c\u671f\u76ee\u5f55(\u5de5\u4f5c\u533a\u5728\u522b\u5904, \u8fd9\u4e9b\u53ea\u5728\u6e90\u7801\u8fd0\u884c\u65f6\u51fa\u73b0) ----
logs/
jobs/

# ---- \u6253\u5305\u4ea7\u7269 ----
build/
dist/

# ---- \u81ea\u68c0\u4e0e\u622a\u56fe\u7684\u4e2d\u95f4\u4ea7\u7269 ----
tmp_gui/
baseline/

# ---- \u5f62\u6001\u68c0\u7d22\u63cf\u8ff0\u5b50\u7f13\u5b58(\u53ef\u4ece catalog.json \u968f\u65f6\u91cd\u5efa, \u4e0d\u8fdb git) ----
outputs/catalog/morphology.npz

# ---- \u9a73\u56de\u6837\u672c\u91cd\u65b0\u751f\u6210\u7684\u4ea7\u7269(\u5f85\u4eba\u5de5\u590d\u6838, \u4e0d\u8fdb git) ----
outputs/regenerated/

# ---- \u4eba\u5de5\u9a73\u56de\u6837\u672c\u539f\u59cb\u6570\u636e / \u672c\u673a\u6d4b\u8bd5\u5de5\u4f5c\u533a(\u4e0d\u8fdb git) ----
\u6ca1\u6253\u6807\u7b7e\u7684/
/test/
"""

with io.open(path, "w", encoding="utf-8", newline="") as f:
    f.write(content)
print("rewrote .gitignore cleanly, %d bytes" % len(content.encode("utf-8")))
