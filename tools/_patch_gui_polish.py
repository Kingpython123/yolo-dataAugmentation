"""One-off polish pass on the GUI, driven by what the offscreen screenshots showed.

1. theme.py  - drop the custom QSpinBox up/down button styling. Overriding their
               background hid the native arrow glyphs, leaving a blank grey strip.
               Also left-align table headers so they line up with the cell text.
2. settings.py / generate.py - the spin boxes stretched across the whole grid
               column. Give them a sane fixed width.
3. check_gui.py - Qt's internal sub-widgets (objectName starting with "qt_", e.g.
               a QSpinBox's embedded line edit) are not ours to name; skip them.
               Also wait until no background task is in flight before grabbing.
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


patch("app/gui/theme.py", [
    (
        """QSpinBox::up-button, QSpinBox::down-button {{
    width: 16px;
    background: {surface_alt};
    border-left: 1px solid {border};
}}
""",
        """/* \u4e0d\u63a5\u7ba1 QSpinBox \u7684\u4e0a\u4e0b\u6309\u94ae: \u4e00\u65e6\u8986\u76d6\u5b83\u4eec\u7684 background, Qt \u5c31\u4e0d\u518d\u753b
   \u539f\u751f\u7684\u7bad\u5934\u56fe\u5f62, \u53ea\u5269\u4e0b\u4e00\u6761\u7a7a\u767d\u7684\u7070\u6761\u3002\u4fdd\u7559\u539f\u751f\u7ed8\u5236\u3002 */
""",
    ),
    (
        """QHeaderView::section {{
    background: {surface_alt};
    color: {text_dim};
    border: none;
    border-bottom: 1px solid {border};
    padding: 8px 10px;
    font-weight: 600;
}}
""",
        """QHeaderView::section {{
    background: {surface_alt};
    color: {text_dim};
    border: none;
    border-bottom: 1px solid {border};
    padding: 8px 10px;
    font-weight: 600;
    /* Qt \u9ed8\u8ba4\u628a\u8868\u5934\u6587\u5b57\u5c45\u4e2d, \u4e0e\u5de6\u5bf9\u9f50\u7684\u5355\u5143\u683c\u5185\u5bb9\u5bf9\u4e0d\u4e0a */
    text-align: left;
}}
""",
    ),
])

SPIN_WIDTH = """        self.count.setAccessibleName("每个类别生成的样本数量")
"""
patch("app/gui/pages/generate.py", [
    (
        SPIN_WIDTH,
        SPIN_WIDTH.replace(
            '        self.count.setAccessibleName',
            '        self.count.setFixedWidth(120)\n'
            '        self.count.setAccessibleName'),
    ),
    (
        """        self.workers.setAccessibleName("并发线程数")
""",
        """        self.workers.setFixedWidth(120)
        self.workers.setAccessibleName("并发线程数")
""",
    ),
])

patch("app/gui/pages/settings.py", [(
    """        self.workers.setAccessibleName("默认并发线程数")
""",
    """        self.workers.setFixedWidth(120)
        self.workers.setAccessibleName("默认并发线程数")
""")])

patch("tools/check_gui.py", [
    (
        """    for widget_type in interactive:
        # findChildren \u53ea\u63a5\u53d7\u5355\u4e2a\u7c7b\u578b, \u4e0d\u63a5\u53d7\u5143\u7ec4
        for widget in window.findChildren(widget_type):
            label = widget.accessibleName()
            if not label and isinstance(widget, QPushButton):
                label = widget.text()
            if not label:
                missing.append(f"{type(widget).__name__}:{widget.objectName()}")
""",
        """    for widget_type in interactive:
        # findChildren \u53ea\u63a5\u53d7\u5355\u4e2a\u7c7b\u578b, \u4e0d\u63a5\u53d7\u5143\u7ec4
        for widget in window.findChildren(widget_type):
            # Qt \u81ea\u5df1\u7684\u5185\u90e8\u5b50\u63a7\u4ef6(\u5982 QSpinBox \u5185\u5d4c\u7684\u884c\u7f16\u8f91\u5668)\u4e0d\u7531\u6211\u4eec\u547d\u540d
            if widget.objectName().startswith("qt_"):
                continue
            label = widget.accessibleName()
            if not label and isinstance(widget, QPushButton):
                label = widget.text()
            if not label:
                missing.append(f"{type(widget).__name__}:{widget.objectName()}")
""",
    ),
    (
        """def settle(app, ms: int = SETTLE_MS) -> None:
    \"\"\"跑一段事件循环, 让后台任务的结果回到界面。\"\"\"
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()
""",
        """def settle(app, ms: int = SETTLE_MS) -> None:
    \"\"\"跑一段事件循环, 让后台任务的结果回到界面。

    先等固定时长, 再等到在途任务清零 —— 只等固定时长的话, 扫描慢一点就会截到
    半空的界面, 那种截图看不出是界面有问题还是等得不够。
    \"\"\"
    from PySide6.QtCore import QEventLoop, QTimer

    from app.gui import tasks as gui_tasks

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()

    waited = 0
    while gui_tasks.active_count() and waited < 20000:
        inner = QEventLoop()
        QTimer.singleShot(200, inner.quit)
        inner.exec()
        waited += 200
    app.processEvents()
""",
    ),
])

print("done")
