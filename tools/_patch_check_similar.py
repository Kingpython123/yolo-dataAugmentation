"""One-off: fix a wrong assertion in tools/check_similar_cli.py.

The test asserted the seed entry_id must not appear anywhere after the
"formal most similar" marker. But the command legitimately prints a usage hint
at the end containing `--similar-to-entry <seed_id>`, so the seed id shows up in
that hint even though the ranked results correctly exclude it.

Fix: only inspect the numbered result lines (those matching "  N. d=..."), not
the whole trailing text.

ASCII-only on purpose so no encoding conversion is needed.
"""
import io
import pathlib

path = pathlib.Path(__file__).resolve().parents[1] / "tools" / "check_similar_cli.py"
with io.open(path, "r", encoding="utf-8", newline="") as f:
    raw = f.read()
crlf = "\r\n" in raw
text = raw.replace("\r\n", "\n")

old = '''    if seed_id in text.split("\u5f62\u6001\u6700\u76f8\u4f3c\u7684")[-1]:
        problems.append("\u7528\u4f8b1 \u7ed3\u679c\u91cc\u4e0d\u5e94\u5305\u542b\u79cd\u5b50\u81ea\u8eab")
'''

new = '''    # \u53ea\u770b\u7f16\u53f7\u7ed3\u679c\u884c: \u672b\u5c3e\u7684\u7528\u6cd5\u63d0\u793a\u4f1a\u5408\u6cd5\u5730\u5e26\u4e0a\u79cd\u5b50 id
    # (--similar-to-entry <seed>), \u4e0d\u80fd\u62ff\u6574\u6bb5\u6587\u672c\u505a\u5224\u5b9a
    result_lines = [ln for ln in text.splitlines()
                    if ln.strip() and ln.strip()[0].isdigit()
                    and ". d=" in ln]
    if any(seed_id in ln for ln in result_lines):
        problems.append("\u7528\u4f8b1 \u7ed3\u679c\u91cc\u4e0d\u5e94\u5305\u542b\u79cd\u5b50\u81ea\u8eab")
    if len(result_lines) != 5:
        problems.append(f"\u7528\u4f8b1 \u5e94\u6709 5 \u6761\u7ed3\u679c\u884c, \u5b9e\u9645 {len(result_lines)}")
'''

if text.count(old) != 1:
    raise SystemExit("pattern hit %d times (want 1)" % text.count(old))
text = text.replace(old, new, 1)

if crlf:
    text = text.replace("\n", "\r\n")
with io.open(path, "w", encoding="utf-8", newline="") as f:
    f.write(text)
print("patched check_similar_cli.py")
