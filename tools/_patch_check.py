"""One-off: fix the fake QC response in tools/check_boundary.py.

quality_check.check() requires 'defect_present' and reads the bottle flag from
'on_bottle'. The harness used 'defect_on_bottle' and omitted 'defect_present',
so every sample was correctly rejected with "未检出新增缺陷".

ASCII-only on purpose so no encoding conversion is needed.
"""
import pathlib

p = pathlib.Path(__file__).with_name("check_boundary.py")
with p.open("r", encoding="utf-8", newline="") as f:
    s = f.read()

old = '"only_local_change": True, "defect_on_bottle": True,'
new = ('"defect_present": True, "only_local_change": True,\r\n'
       '            "on_bottle": True,')

if s.count(old) != 1:
    raise SystemExit("pattern not found exactly once: %d" % s.count(old))

s = s.replace(old, new, 1)
with p.open("w", encoding="utf-8", newline="") as f:
    f.write(s)
print("patched check_boundary.py")
