"""One-off: drop OpenCV's bundled FFmpeg DLL from the frozen build.

Measured on the first successful build: opencv_videoio_ffmpeg4140_64.dll is
29.1 MB, the second largest file in the whole distribution. It only backs
cv2.VideoCapture / VideoWriter, and this project uses neither (verified by
grepping every cv2.* call: cvtColor, GaussianBlur, threshold, morphologyEx,
getStructuringElement, createCLAHE, split, merge, resize, erode, bitwise_and,
connectedComponents(WithStats), seamlessClone).

Excluding it is therefore a pure size win with no functional impact.
"""
import io
import pathlib

path = pathlib.Path(__file__).resolve().parents[1] / "packaging" / "app.spec"
with io.open(path, "r", encoding="utf-8", newline="") as f:
    raw = f.read()
crlf = "\r\n" in raw
text = raw.replace("\r\n", "\n")

old = '''RUNTIME_HOOKS = [str(SPEC_DIR / "runtime_hook_utf8.py")]
'''

new = '''RUNTIME_HOOKS = [str(SPEC_DIR / "runtime_hook_utf8.py")]

# \u5b9e\u6d4b\u7b2c\u4e00\u6b21\u6784\u5efa: opencv \u81ea\u5e26\u7684 FFmpeg DLL \u5360 29.1 MB, \u662f\u6574\u4e2a\u53d1\u884c\u7248\u91cc
# \u7b2c\u4e8c\u5927\u7684\u6587\u4ef6\u3002\u5b83\u53ea\u670d\u52a1 cv2.VideoCapture / VideoWriter, \u800c\u672c\u9879\u76ee\u4e00\u4e2a\u90fd\u6ca1\u7528,
# \u56e0\u6b64\u53bb\u6389\u662f\u7eaf\u4f53\u79ef\u6536\u76ca\u3002
EXCLUDED_BINARY_PATTERNS = (
    "opencv_videoio_ffmpeg",
)


def _strip_binaries(binaries):
    """\u6309\u6587\u4ef6\u540d\u524d\u7f00\u5254\u9664\u7528\u4e0d\u5230\u7684\u52a8\u6001\u5e93\u3002"""
    kept = []
    for entry in binaries:
        name = Path(entry[0]).name.lower()
        if any(name.startswith(p) for p in EXCLUDED_BINARY_PATTERNS):
            continue
        kept.append(entry)
    return kept
'''

if text.count(old) != 1:
    raise SystemExit("RUNTIME_HOOKS anchor hit %d times" % text.count(old))
text = text.replace(old, new, 1)

old2 = '''gui_pyz = PYZ(gui_analysis.pure)
'''
new2 = '''gui_analysis.binaries = _strip_binaries(gui_analysis.binaries)
cli_analysis.binaries = _strip_binaries(cli_analysis.binaries)

gui_pyz = PYZ(gui_analysis.pure)
'''
if text.count(old2) != 1:
    raise SystemExit("PYZ anchor hit %d times" % text.count(old2))
text = text.replace(old2, new2, 1)

if crlf:
    text = text.replace("\n", "\r\n")
with io.open(path, "w", encoding="utf-8", newline="") as f:
    f.write(text)
print("patched packaging/app.spec")
