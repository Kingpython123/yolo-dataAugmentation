"""One-off: fix two real visual defects plus the screenshot harness.

1. theme.py - `QWidget { background: ... }` also paints QLabel/QCheckBox, so every
   label inside a card showed the darker window background as a horizontal bar.
   Labels must be transparent so the card surface shows through.

2. widgets.py / overview.py - word-wrapped labels holding long unbreakable strings
   (file paths) push up the minimum width, which made the two overview grid
   columns unequal. setMinimumWidth(1) lets them shrink.

3. check_gui.py - findChildren() takes a single type, not a tuple; and the
   offscreen Qt plugin ships no fonts, so point it at the Windows font directory
   or every glyph renders as a box.
"""
import io
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]


def patch(rel, pairs):
    path = REPO / rel
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    for old, new in pairs:
        if text.count(old) != 1:
            raise SystemExit("%s: pattern hit %d times (want 1): %r"
                             % (rel, text.count(old), old[:70]))
        text = text.replace(old, new, 1)
    if crlf:
        text = text.replace("\n", "\r\n")
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print("patched", rel)


# ---- 1. QSS: labels must not paint the window background ----
patch("app/gui/theme.py", [(
    """QWidget {{
    background: {bg};
    color: {text};
}}
""",
    """QWidget {{
    background: {bg};
    color: {text};
}}

/* QWidget \u7684 background \u4f1a\u8fde\u5e26\u4f5c\u7528\u5230\u6807\u7b7e\u7c7b\u63a7\u4ef6\u4e0a, \u4f7f\u5b83\u4eec\u5728\u5361\u7247\u91cc
   \u663e\u793a\u51fa\u66f4\u6df1\u7684\u7a97\u53e3\u5e95\u8272, \u5f62\u6210\u4e00\u6761\u6761\u6a2a\u6761\u3002\u8fd9\u7c7b\u63a7\u4ef6\u5fc5\u987b\u900f\u660e\u3002 */
QLabel, QCheckBox, QRadioButton, QGroupBox {{
    background: transparent;
}}
""")])

# ---- 2. 让自动换行的长文本标签可以收缩 ----
patch("app/gui/widgets.py", [(
    """        self._subtitle.setWordWrap(True)
        self._subtitle.setVisible(bool(subtitle))
""",
    """        self._subtitle.setWordWrap(True)
        # \u5141\u8bb8\u6536\u7f29: \u81ea\u52a8\u6362\u884c\u7684\u6807\u7b7e\u9047\u5230\u957f\u8def\u5f84\u8fd9\u79cd\u4e0d\u53ef\u6298\u884c\u7684\u5b57\u4e32\u65f6,
        # \u9ed8\u8ba4\u4f1a\u628a\u6700\u5c0f\u5bbd\u5ea6\u6491\u5230\u6574\u884c\u5bbd, \u8fdb\u800c\u628a\u7f51\u683c\u5217\u5bbd\u9876\u5f97\u4e0d\u5747\u5300\u3002
        self._subtitle.setMinimumWidth(1)
        self._subtitle.setVisible(bool(subtitle))
""")])

patch("app/gui/pages/overview.py", [(
    """        self.detail = QLabel("")
        self.detail.setObjectName("Hint")
        self.detail.setWordWrap(True)
        self.body().addWidget(self.detail)
""",
    """        self.detail = QLabel("")
        self.detail.setObjectName("Hint")
        self.detail.setWordWrap(True)
        # \u5361\u7247\u91cc\u8981\u663e\u793a\u5b8c\u6574\u8def\u5f84, \u4e0d\u9650\u5236\u6700\u5c0f\u5bbd\u5ea6\u7684\u8bdd\u4e24\u5217\u5bbd\u5ea6\u4f1a\u4e0d\u76f8\u7b49
        self.detail.setMinimumWidth(1)
        self.body().addWidget(self.detail)
""")])

# ---- 3. 截图脚本 ----
patch("tools/check_gui.py", [
    (
        '''os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONUTF8", "1")
''',
        '''os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONUTF8", "1")
# offscreen \u63d2\u4ef6\u81ea\u5df1\u4e0d\u5e26\u5b57\u4f53, \u4e0d\u6307\u5b9a\u7684\u8bdd\u6bcf\u4e2a\u5b57\u90fd\u6e32\u67d3\u6210\u65b9\u6846\u3002
# \u6307\u5411\u7cfb\u7edf\u5b57\u4f53\u76ee\u5f55, \u624d\u80fd\u770b\u51fa\u771f\u5b9e\u7684\u6587\u5b57\u6392\u7248\u3002
if sys.platform.startswith("win"):
    _fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if _fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(_fonts))
''',
    ),
    (
        """    interactive = (QPushButton, QLineEdit, QSpinBox, QComboBox, QCheckBox,
                   QTableWidget)
    missing = []
    for widget in window.findChildren(interactive):
        if not widget.isVisible() and widget.parent() is None:
            continue
        if not (widget.accessibleName() or widget.text()
                if isinstance(widget, QPushButton) else widget.accessibleName()):
            missing.append(f"{type(widget).__name__}:{widget.objectName()}")
""",
        """    interactive = (QPushButton, QLineEdit, QSpinBox, QComboBox, QCheckBox,
                   QTableWidget)
    missing = []
    for widget_type in interactive:
        # findChildren \u53ea\u63a5\u53d7\u5355\u4e2a\u7c7b\u578b, \u4e0d\u63a5\u53d7\u5143\u7ec4
        for widget in window.findChildren(widget_type):
            label = widget.accessibleName()
            if not label and isinstance(widget, QPushButton):
                label = widget.text()
            if not label:
                missing.append(f"{type(widget).__name__}:{widget.objectName()}")
""",
    ),
])

print("done")
