"""One-off: the checkbox cell wrapper painted the window background.

ClassTable puts each QCheckBox inside a plain QWidget so it can be centred in the
cell. `QWidget { background: <bg> }` from the stylesheet therefore drew a darker
rectangle behind every checkbox, which looked like a stray extra box in the
column. The wrapper must be transparent so the row colour shows through.
"""
import io
import pathlib

path = (pathlib.Path(__file__).resolve().parents[1]
        / "app" / "gui" / "widgets.py")
with io.open(path, "r", encoding="utf-8", newline="") as f:
    raw = f.read()
crlf = "\r\n" in raw
text = raw.replace("\r\n", "\n")

old = """            holder = QWidget()
            box = QHBoxLayout(holder)
"""
new = """            holder = QWidget()
            # \u5305\u88c5\u5bb9\u5668\u5fc5\u987b\u900f\u660e: \u5426\u5219\u5b83\u4f1a\u753b\u51fa\u7a97\u53e3\u5e95\u8272,
            # \u5728\u8868\u683c\u91cc\u770b\u8d77\u6765\u50cf\u590d\u9009\u6846\u65c1\u8fb9\u591a\u4e86\u4e00\u4e2a\u7a7a\u65b9\u6846\u3002
            holder.setStyleSheet("background: transparent;")
            box = QHBoxLayout(holder)
"""

if text.count(old) != 1:
    raise SystemExit("pattern hit %d times (want 1)" % text.count(old))
text = text.replace(old, new, 1)

if crlf:
    text = text.replace("\n", "\r\n")
with io.open(path, "w", encoding="utf-8", newline="") as f:
    f.write(text)
print("patched app/gui/widgets.py")
