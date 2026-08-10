"""One-off: make tools/migrate_generate.py work on Python 3.12.

Path.read_text(newline=...) / write_text(newline=...) only exist on 3.13+.
Kept ASCII-only on purpose so it can be edited by any tool without encoding risk.
"""
import pathlib

p = pathlib.Path(__file__).with_name("migrate_generate.py")
with p.open("r", encoding="utf-8", newline="") as f:
    s = f.read()

pairs = [
    (
        'raw = TARGET.read_text(encoding="utf-8", newline="")',
        'with TARGET.open("r", encoding="utf-8", newline="") as fh:\n'
        '        raw = fh.read()',
    ),
    (
        'TARGET.write_text(text, encoding="utf-8", newline="")',
        'with TARGET.open("w", encoding="utf-8", newline="") as fh:\n'
        '        fh.write(text)',
    ),
]

for old, new in pairs:
    if s.count(old) != 1:
        raise SystemExit("pattern not found exactly once: %r (%d)" % (old, s.count(old)))
    s = s.replace(old, new, 1)

with p.open("w", encoding="utf-8", newline="") as f:
    f.write(s)
print("patched migrate_generate.py")
