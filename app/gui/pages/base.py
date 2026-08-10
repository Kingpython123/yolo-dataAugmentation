"""页面基类。

统一三件事:
  1. 页面标题区的排版
  2. 主题切换时向下传递调色板(自定义控件的颜色不走 QSS, 需要手动同步)
  3. refresh() 约定 —— 切换到某页时由主窗口调用, 页面据此拉取最新数据
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .. import theme, widgets

CONTENT_MARGIN = 28
CONTENT_SPACING = 18


class Page(QWidget):
    """带滚动区的页面骨架。内容加到 self.content_layout。"""

    request_navigate = Signal(str)      # 请求主窗口切换到某页
    toast = Signal(str, str)            # (文本, 语义) 由主窗口统一展示

    title = ""
    subtitle = ""
    nav_label = ""

    def __init__(self, palette: theme.Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.palette_ = palette

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        holder = QWidget()
        self.content_layout = QVBoxLayout(holder)
        self.content_layout.setContentsMargins(
            CONTENT_MARGIN, CONTENT_MARGIN, CONTENT_MARGIN, CONTENT_MARGIN)
        self.content_layout.setSpacing(CONTENT_SPACING)
        holder.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        scroll.setWidget(holder)
        self._scroll = scroll

        if self.title:
            self.content_layout.addWidget(
                widgets.title_block(self.title, self.subtitle))

    # ---- 子类可覆写 ----

    def refresh(self) -> None:
        """切到本页时调用。默认什么都不做。"""

    def on_palette_changed(self, palette: theme.Palette) -> None:
        """主题切换。子类需要把调色板转给自定义控件。"""

    # ---- 工具 ----

    def set_palette_recursive(self, palette: theme.Palette) -> None:
        self.palette_ = palette
        for child in self.findChildren(QWidget):
            setter = getattr(child, "set_palette", None)
            if callable(setter):
                setter(palette)
        self.on_palette_changed(palette)

    def add_stretch(self) -> None:
        self.content_layout.addStretch(1)
