"""One-off: clean up MainWindow.apply_theme.

Two problems in the first draft:
  1. A leftover placeholder line (`app = ... if False else None` / `del app`).
  2. The stylesheet was applied to the window. Top-level dialogs such as
     QMessageBox and QFileDialog are separate top-level widgets, so applying the
     sheet on QApplication is the reliable way to style them too.
"""
import io
import pathlib

p = pathlib.Path(__file__).resolve().parents[1] / "app" / "gui" / "main_window.py"
with io.open(p, "r", encoding="utf-8", newline="") as f:
    s = f.read()

crlf = "\r\n" in s
text = s.replace("\r\n", "\n")

old = """    def apply_theme(self, name: str) -> None:
        self.palette_ = theme.palette_for(name)
        app = self.window().style().parent() if False else None  # \u5360\u4f4d, \u89c1\u4e0b
        self.setStyleSheet(theme.build_qss(self.palette_))
        for page in self._pages.values():
            page.set_palette_recursive(self.palette_)
        del app
"""

new = """    def apply_theme(self, name: str) -> None:
        self.palette_ = theme.palette_for(name)
        qss = theme.build_qss(self.palette_)
        # \u6837\u5f0f\u8868\u8bbe\u5728 QApplication \u800c\u4e0d\u662f\u7a97\u53e3\u4e0a: QMessageBox / QFileDialog
        # \u662f\u72ec\u7acb\u7684\u9876\u5c42\u7a97\u53e3, \u53ea\u7ed9\u4e3b\u7a97\u53e3\u4e0a\u6837\u5f0f\u65f6\u5b83\u4eec\u4f1a\u4fdd\u6301\u7cfb\u7edf\u9ed8\u8ba4\u5916\u89c2,
        # \u5728\u6df1\u8272\u4e3b\u9898\u4e0b\u663e\u5f97\u5f88\u7a81\u515c\u3002
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)
        else:
            self.setStyleSheet(qss)
        for page in self._pages.values():
            page.set_palette_recursive(self.palette_)
"""

if text.count(old) != 1:
    raise SystemExit("apply_theme block not found exactly once: %d"
                     % text.count(old))
text = text.replace(old, new, 1)

old_import = ("from PySide6.QtWidgets import (QButtonGroup, QFileDialog, QFrame, "
              "QHBoxLayout,\n"
              "                               QLabel, QMainWindow, QMessageBox, "
              "QPushButton,\n"
              "                               QStackedWidget, QVBoxLayout, QWidget)\n")
new_import = ("from PySide6.QtWidgets import (QApplication, QButtonGroup, "
              "QFileDialog, QFrame,\n"
              "                               QHBoxLayout, QLabel, QMainWindow,\n"
              "                               QMessageBox, QPushButton, "
              "QStackedWidget,\n"
              "                               QVBoxLayout, QWidget)\n")

if text.count(old_import) != 1:
    raise SystemExit("import block not found exactly once: %d"
                     % text.count(old_import))
text = text.replace(old_import, new_import, 1)

if crlf:
    text = text.replace("\n", "\r\n")
with io.open(p, "w", encoding="utf-8", newline="") as f:
    f.write(text)
print("patched main_window.py")
