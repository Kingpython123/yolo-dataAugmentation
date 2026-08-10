"""One-off: append all new ignore rules accumulated this session to .gitignore.

Reads and writes explicit UTF-8 with no BOM and no newline translation, so the
existing content (and its encoding) is preserved exactly.
"""
import io
import pathlib

path = pathlib.Path(__file__).resolve().parents[1] / ".gitignore"
with io.open(path, "r", encoding="utf-8", newline="") as f:
    text = f.read()

addition = (
    "\n"
    "# ---- \u6866\u9762\u7a0b\u5e8f\u7684\u8fd0\u884c\u671f\u76ee\u5f55"
    "(\u5de5\u4f5c\u533a\u5728\u522b\u5904, \u8fd9\u4e9b\u53ea\u5728\u6e90\u7801"
    "\u8fd0\u884c\u65f6\u51fa\u73b0) ----\n"
    "logs/\n"
    "jobs/\n"
    "\n"
    "# ---- \u6253\u5305\u4ea7\u7269 ----\n"
    "build/\n"
    "dist/\n"
    "\n"
    "# ---- \u81ea\u68c0\u4e0e\u622a\u56fe\u7684\u4e2d\u95f4\u4ea7\u7269 ----\n"
    "tmp_gui/\n"
    "baseline/\n"
    "tmp_*.txt\n"
    "tmp_*.log\n"
    "\n"
    "# ---- \u5f62\u6001\u68c0\u7d22\u63cf\u8ff0\u5b50\u7f13\u5b58"
    "(\u53ef\u4ece catalog.json \u968f\u65f6\u91cd\u5efa, \u4e0d\u8fdb git) ----\n"
    "outputs/catalog/morphology.npz\n"
    "\n"
    "# ---- \u9a73\u56de\u6837\u672c\u91cd\u65b0\u751f\u6210\u7684\u4ea7\u7269"
    "(\u5f85\u4eba\u5de5\u590d\u6838, \u4e0d\u8fdb git) ----\n"
    "outputs/regenerated/\n"
    "\n"
    "# ---- \u4eba\u5de5\u9a73\u56de\u6837\u672c\u539f\u59cb\u6570\u636e / "
    "\u672c\u673a\u6d4b\u8bd5\u5de5\u4f5c\u533a(\u4e0d\u8fdb git) ----\n"
    "\u6ca1\u6253\u6807\u7b7e\u7684/\n"
    "/test/\n"
)

if addition.strip() in text:
    print("already appended, no-op")
else:
    with io.open(path, "a", encoding="utf-8", newline="") as f:
        f.write(addition)
    print("appended new ignore rules")
