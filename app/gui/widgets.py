"""自定义控件。

无障碍要求(NFR-6)在这里集中落实:
  - 每个交互控件都设置 accessibleName
  - 状态一律"颜色 + 文字"双通道, 不靠颜色单独传达
  - 焦点可见描边由 theme.qss 统一提供
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
                               QProgressBar, QPushButton, QSizePolicy,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from . import theme

# 日志视图最多保留的行数。长跑任务能产出上万行, 不设上限会让界面内存持续增长。
LOG_MAX_LINES = 5000

LEVEL_TONE = {"info": "muted", "warn": "warning", "error": "error"}
LEVEL_LABEL = {"info": "信息", "warn": "警告", "error": "错误"}


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    return line


class StatusDot(QWidget):
    """状态指示: 一个圆点 + 一段文字。

    文字是必需的而不是可选的 —— 只有颜色的话, 色觉障碍用户与黑白打印都读不出
    状态, 这是 WCAG 明确要求避免的。
    """

    DIAMETER = 10

    def __init__(self, text: str = "", tone: str = "muted",
                 palette: theme.Palette | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette or theme.DARK
        self._tone = tone

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._dot = _Dot(self._palette, tone)
        self._label = QLabel(text)
        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        layout.addStretch(1)
        self._apply()

    def set_state(self, text: str, tone: str = "muted") -> None:
        self._tone = tone
        self._label.setText(text)
        self._dot.set_tone(tone)
        self._apply()

    def set_palette(self, palette: theme.Palette) -> None:
        self._palette = palette
        self._dot.set_palette(palette)
        self._apply()

    def _apply(self) -> None:
        color = theme.tone_color(self._palette, self._tone)
        self._label.setStyleSheet(f"color: {color};")
        self.setAccessibleName(f"状态: {self._label.text()}")


class _Dot(QWidget):
    def __init__(self, palette: theme.Palette, tone: str) -> None:
        super().__init__()
        self._palette = palette
        self._tone = tone
        self.setFixedSize(StatusDot.DIAMETER + 2, StatusDot.DIAMETER + 2)

    def set_tone(self, tone: str) -> None:
        self._tone = tone
        self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._palette = palette
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(theme.tone_color(self._palette, self._tone))
        painter.setBrush(color)
        painter.setPen(QPen(color))
        d = StatusDot.DIAMETER
        painter.drawEllipse(1, (self.height() - d) // 2, d, d)


class Card(QFrame):
    """带标题的卡片容器。内容通过 body() 拿到的布局添加。"""

    def __init__(self, title: str = "", subtitle: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)

        self._header = QHBoxLayout()
        self._header.setSpacing(8)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self._title = QLabel(title)
        self._title.setObjectName("CardTitle")
        titles.addWidget(self._title)
        self._subtitle = QLabel(subtitle)
        self._subtitle.setObjectName("CardSubtitle")
        self._subtitle.setWordWrap(True)
        # 允许收缩: 自动换行的标签遇到长路径这种不可折行的字串时,
        # 默认会把最小宽度撑到整行宽, 进而把网格列宽顶得不均匀。
        self._subtitle.setMinimumWidth(1)
        self._subtitle.setVisible(bool(subtitle))
        titles.addWidget(self._subtitle)
        self._header.addLayout(titles, 1)
        outer.addLayout(self._header)

        self._body = QVBoxLayout()
        self._body.setSpacing(10)
        outer.addLayout(self._body)
        if title:
            self.setAccessibleName(title)

    def body(self) -> QVBoxLayout:
        return self._body

    def header(self) -> QHBoxLayout:
        return self._header

    def set_subtitle(self, text: str) -> None:
        self._subtitle.setText(text)
        self._subtitle.setVisible(bool(text))

    def add_header_widget(self, widget: QWidget) -> None:
        self._header.addWidget(widget, 0, Qt.AlignmentFlag.AlignTop)


class StatCard(QFrame):
    """一个大数字 + 一行说明。用于进度页的计数块。"""

    def __init__(self, label: str, value: str = "0", tone: str = "accent",
                 palette: theme.Palette | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        self._tone = tone
        self._palette = palette or theme.DARK

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(2)
        self._value = QLabel(value)
        self._value.setObjectName("StatValue")
        self._label = QLabel(label)
        self._label.setObjectName("StatLabel")
        layout.addWidget(self._value)
        layout.addWidget(self._label)
        self._apply()

    def set_value(self, value) -> None:
        self._value.setText(str(value))
        self.setAccessibleName(f"{self._label.text()}: {value}")

    def set_palette(self, palette: theme.Palette) -> None:
        self._palette = palette
        self._apply()

    def _apply(self) -> None:
        color = theme.tone_color(self._palette, self._tone)
        self._value.setStyleSheet(f"color: {color};")
        self.setAccessibleName(f"{self._label.text()}: {self._value.text()}")


class Banner(QFrame):
    """行内提示条。用于展示阻塞原因、警告与操作结果。"""

    def __init__(self, palette: theme.Palette | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        self._palette = palette or theme.DARK
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        self._dot = StatusDot("", "info", self._palette)
        self._text = QLabel("")
        self._text.setWordWrap(True)
        layout.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._text, 1)
        self.setVisible(False)

    def show_message(self, text: str, tone: str = "info",
                     prefix: str | None = None) -> None:
        label = prefix if prefix is not None else {
            "ok": "就绪", "warning": "注意", "error": "无法继续",
            "info": "提示",
        }.get(tone, "提示")
        self._dot.set_state(label, tone)
        self._text.setText(text)
        self._text.setStyleSheet(
            f"color: {theme.tone_color(self._palette, 'info' if tone == 'info' else tone)};"
            if tone in ("error", "warning") else "")
        self.setAccessibleName(f"{label}: {text}")
        self.setVisible(bool(text))

    def clear(self) -> None:
        self._text.clear()
        self.setVisible(False)

    def set_palette(self, palette: theme.Palette) -> None:
        self._palette = palette
        self._dot.set_palette(palette)


class LogView(QPlainTextEdit):
    """只读日志视图, 带级别着色与行数上限。"""

    def __init__(self, palette: theme.Palette | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LogView")
        self.setReadOnly(True)
        self.setMaximumBlockCount(LOG_MAX_LINES)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setAccessibleName("运行日志")
        self._palette = palette or theme.DARK

    def set_palette(self, palette: theme.Palette) -> None:
        self._palette = palette

    def append_line(self, text: str, level: str = "info") -> None:
        text = (text or "").rstrip("\n")
        if not text:
            return
        tone = LEVEL_TONE.get(level, "muted")
        color = (self._palette.text if tone == "muted"
                 else theme.tone_color(self._palette, tone))
        escaped = (text.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
        prefix = ""
        if level in ("warn", "error"):
            prefix = f"[{LEVEL_LABEL[level]}] "
        self.appendHtml(
            f'<span style="color:{color}; white-space:pre;">{prefix}{escaped}</span>')

    def scroll_to_end(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


class ClassTable(QTableWidget):
    """类别选择表: 复选框 + 类别名 + 干净图数 + 已生成 + 已驳回。"""

    COLUMNS = ("选择", "类别", "干净图", "已生成", "已驳回")
    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.setAccessibleName("类别选择表")

        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, 56)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in range(2, len(self.COLUMNS)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

        self._checks: dict[str, QCheckBox] = {}

    def set_classes(self, classes, preselect=None) -> None:
        """classes: 具备 name/image_count/done_count/rejected_count 的对象序列。"""
        preselect = set(preselect or ())
        self._checks.clear()
        self.setRowCount(0)
        for info in classes:
            row = self.rowCount()
            self.insertRow(row)

            holder = QWidget()
            # 包装容器必须透明: 否则它会画出窗口底色,
            # 在表格里看起来像复选框旁边多了一个空方框。
            holder.setStyleSheet("background: transparent;")
            box = QHBoxLayout(holder)
            box.setContentsMargins(0, 0, 0, 0)
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            check = QCheckBox()
            check.setChecked(info.name in preselect)
            check.setAccessibleName(f"选择类别 {info.name}")
            check.stateChanged.connect(lambda *_: self.selection_changed.emit())
            box.addWidget(check)
            self.setCellWidget(row, 0, holder)
            self._checks[info.name] = check

            self._set_text(row, 1, info.name)
            self._set_text(row, 2, str(info.image_count),
                          Qt.AlignmentFlag.AlignRight)
            self._set_text(row, 3, str(getattr(info, "done_count", 0)),
                          Qt.AlignmentFlag.AlignRight)
            self._set_text(row, 4, str(getattr(info, "rejected_count", 0)),
                          Qt.AlignmentFlag.AlignRight)
        self.selection_changed.emit()

    def _set_text(self, row: int, column: int, text: str,
                  align=Qt.AlignmentFlag.AlignLeft) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
        self.setItem(row, column, item)

    def selected_classes(self) -> list:
        return [name for name, box in self._checks.items() if box.isChecked()]

    def set_all(self, checked: bool) -> None:
        for box in self._checks.values():
            box.setChecked(checked)


class LabeledProgress(QWidget):
    """进度条 + 右侧文字说明。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName("生成进度")
        layout.addWidget(self.bar)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.left = QLabel("尚未开始")
        self.right = QLabel("")
        self.right.setObjectName("Hint")
        row.addWidget(self.left, 1)
        row.addWidget(self.right, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(row)

    def update_progress(self, percent: float, left_text: str,
                        right_text: str = "") -> None:
        self.bar.setValue(int(max(0, min(100, percent))))
        self.left.setText(left_text)
        self.right.setText(right_text)
        self.bar.setAccessibleDescription(f"{left_text} {right_text}".strip())


def title_block(title: str, subtitle: str = "") -> QWidget:
    """页面标题区。"""
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    label = QLabel(title)
    label.setObjectName("PageTitle")
    layout.addWidget(label)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("PageSubtitle")
        sub.setWordWrap(True)
        layout.addWidget(sub)
    return holder


def bold(text: str) -> QLabel:
    label = QLabel(text)
    font = QFont()
    font.setBold(True)
    label.setFont(font)
    return label


def primary_button(text: str, accessible: str | None = None) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("Primary")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setAccessibleName(accessible or text)
    return button


def link_button(text: str, accessible: str | None = None) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("Link")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setAccessibleName(accessible or text)
    return button
