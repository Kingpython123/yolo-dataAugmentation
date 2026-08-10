"""配色与样式表。

颜色集中在这里, 控件代码里不出现硬编码色值 —— 否则换主题就得全项目搜色号。
QSS 用模板 + 占位符生成, 深浅两套主题共用同一份布局规则。

对比度: 正文色与背景色的对比度不低于 4.5:1(WCAG AA), 次要文字不低于 4.5:1,
状态色仅作辅助, 任何状态都同时有文字标签(不靠颜色单独传达信息)。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

# 字体优先级: Windows 中文界面首选微软雅黑 UI, 回退到 Segoe UI
FONT_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", "Courier New", monospace'

BASE_FONT_SIZE = 13
SIDEBAR_WIDTH = 216
RADIUS = 8


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str             # 窗口底色
    surface: str        # 卡片底色
    surface_alt: str    # 悬浮/选中态
    border: str
    text: str           # 正文
    text_dim: str       # 次要文字
    text_faint: str     # 更弱的说明文字
    accent: str
    accent_hover: str
    accent_text: str    # 强调色按钮上的文字
    success: str
    warning: str
    danger: str
    info: str
    sidebar: str
    input_bg: str
    log_bg: str


DARK = Palette(
    name="dark",
    bg="#14181f",
    surface="#1b212b",
    surface_alt="#232c39",
    border="#2e394a",
    text="#e6edf7",
    text_dim="#a3b1c6",
    text_faint="#78889e",
    accent="#3d9cf5",
    accent_hover="#5cb0fb",
    accent_text="#0b1220",
    success="#3ecf7a",
    warning="#eab453",
    danger="#f2706f",
    info="#7aa7d9",
    sidebar="#111620",
    input_bg="#121821",
    log_bg="#0f141c",
)

LIGHT = Palette(
    name="light",
    bg="#f2f4f8",
    surface="#ffffff",
    surface_alt="#e9eef6",
    border="#d3dae5",
    text="#1c2430",
    text_dim="#53616f",
    text_faint="#75818f",
    accent="#1668c4",
    accent_hover="#1f79dc",
    accent_text="#ffffff",
    success="#1a8f4c",
    warning="#95610a",
    danger="#c22f2f",
    info="#2b5f96",
    sidebar="#e6ebf3",
    input_bg="#ffffff",
    log_bg="#f7f9fc",
)

PALETTES = {DARK.name: DARK, LIGHT.name: LIGHT}


def palette_for(name: str) -> Palette:
    return PALETTES.get((name or "").lower(), DARK)


# 状态语义 -> 调色板字段名。界面各处统一用语义名, 不直接选颜色。
TONE_FIELDS = {
    "ok": "success",
    "warning": "warning",
    "error": "danger",
    "info": "info",
    "muted": "text_faint",
    "accent": "accent",
}


def tone_color(palette: Palette, tone: str) -> str:
    return getattr(palette, TONE_FIELDS.get(tone, "text_dim"))


QSS_TEMPLATE = """
* {{
    font-family: {font_family};
    font-size: {font_size}px;
}}

QWidget {{
    background: {bg};
    color: {text};
}}

/* QWidget 的 background 会连带作用到标签类控件上, 使它们在卡片里
   显示出更深的窗口底色, 形成一条条横条。这类控件必须透明。 */
QLabel, QCheckBox, QRadioButton, QGroupBox {{
    background: transparent;
}}

QToolTip {{
    background: {surface_alt};
    color: {text};
    border: 1px solid {border};
    padding: 6px 8px;
    border-radius: 4px;
}}

/* ---------------- 侧边导航 ---------------- */

#Sidebar {{
    background: {sidebar};
    border-right: 1px solid {border};
}}

#BrandName {{
    font-size: {h2}px;
    font-weight: 600;
    color: {text};
}}

#BrandVersion {{
    color: {text_faint};
    font-size: {small}px;
}}

QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-radius: {radius}px;
    padding: 10px 14px;
    text-align: left;
    color: {text_dim};
    font-size: {font_size}px;
}}

QPushButton#NavButton:hover {{
    background: {surface_alt};
    color: {text};
}}

QPushButton#NavButton:checked {{
    background: {surface_alt};
    color: {text};
    font-weight: 600;
    border-left: 3px solid {accent};
    padding-left: 11px;
}}

QPushButton#NavButton:focus {{
    outline: none;
    border: 1px solid {accent};
}}

/* ---------------- 卡片 ---------------- */

QFrame#Card {{
    background: {surface};
    border: 1px solid {border};
    border-radius: {radius}px;
}}

QLabel#CardTitle {{
    font-size: {h3}px;
    font-weight: 600;
    color: {text};
}}

QLabel#CardSubtitle {{
    color: {text_dim};
}}

QFrame#StatCard {{
    background: {surface};
    border: 1px solid {border};
    border-radius: {radius}px;
}}

QLabel#StatValue {{
    font-size: {stat}px;
    font-weight: 600;
}}

QLabel#StatLabel {{
    color: {text_dim};
    font-size: {small}px;
}}

QLabel#PageTitle {{
    font-size: {h1}px;
    font-weight: 600;
}}

QLabel#PageSubtitle {{
    color: {text_dim};
}}

QLabel#Hint {{
    color: {text_faint};
}}

QLabel#SectionLabel {{
    color: {text_dim};
    font-weight: 600;
}}

/* ---------------- 按钮 ---------------- */

QPushButton {{
    background: {surface_alt};
    color: {text};
    border: 1px solid {border};
    border-radius: {radius}px;
    padding: 8px 16px;
    min-height: 20px;
}}

QPushButton:hover {{
    border-color: {accent};
}}

QPushButton:focus {{
    border: 2px solid {accent};
    padding: 7px 15px;
}}

QPushButton:disabled {{
    color: {text_faint};
    background: {surface};
    border-color: {border};
}}

QPushButton#Primary {{
    background: {accent};
    color: {accent_text};
    border: 1px solid {accent};
    font-weight: 600;
}}

QPushButton#Primary:hover {{
    background: {accent_hover};
    border-color: {accent_hover};
}}

QPushButton#Primary:disabled {{
    background: {surface_alt};
    color: {text_faint};
    border-color: {border};
}}

QPushButton#Danger {{
    background: transparent;
    color: {danger};
    border: 1px solid {danger};
}}

QPushButton#Danger:hover {{
    background: {danger};
    color: {accent_text};
}}

QPushButton#Link {{
    background: transparent;
    border: none;
    color: {accent};
    padding: 4px 2px;
    text-align: left;
}}

QPushButton#Link:hover {{
    color: {accent_hover};
    text-decoration: underline;
}}

/* ---------------- 输入 ---------------- */

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
    background: {input_bg};
    color: {text};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 7px 10px;
    selection-background-color: {accent};
    selection-color: {accent_text};
}}

QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 2px solid {accent};
    padding: 6px 9px;
}}

QLineEdit:disabled, QSpinBox:disabled {{
    color: {text_faint};
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}

QComboBox QAbstractItemView {{
    background: {surface};
    color: {text};
    border: 1px solid {border};
    selection-background-color: {accent};
    selection-color: {accent_text};
    outline: none;
}}

/* 不接管 QSpinBox 的上下按钮: 一旦覆盖它们的 background, Qt 就不再画
   原生的箭头图形, 只剩下一条空白的灰条。保留原生绘制。 */

QCheckBox {{
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {border};
    border-radius: 4px;
    background: {input_bg};
}}

QCheckBox::indicator:checked {{
    background: {accent};
    border-color: {accent};
}}

QCheckBox:focus {{
    border: none;
}}

/* ---------------- 表格 ---------------- */

QTableWidget, QTableView {{
    background: {surface};
    alternate-background-color: {surface_alt};
    border: 1px solid {border};
    border-radius: {radius}px;
    gridline-color: {border};
    selection-background-color: {accent};
    selection-color: {accent_text};
    outline: none;
}}

QHeaderView::section {{
    background: {surface_alt};
    color: {text_dim};
    border: none;
    border-bottom: 1px solid {border};
    padding: 8px 10px;
    font-weight: 600;
    /* Qt 默认把表头文字居中, 与左对齐的单元格内容对不上 */
    text-align: left;
}}

QTableWidget::item {{
    padding: 6px 10px;
}}

QTableWidget::item:focus {{
    outline: 1px solid {accent};
}}

/* ---------------- 进度条 ---------------- */

QProgressBar {{
    background: {surface_alt};
    border: 1px solid {border};
    border-radius: 6px;
    height: 12px;
    text-align: center;
    color: {text};
}}

QProgressBar::chunk {{
    background: {accent};
    border-radius: 5px;
}}

/* ---------------- 日志视图 ---------------- */

QPlainTextEdit#LogView {{
    background: {log_bg};
    border: 1px solid {border};
    font-family: {font_mono};
    font-size: {small}px;
}}

/* ---------------- 滚动条 ---------------- */

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}

QScrollBar::handle:vertical {{
    background: {border};
    border-radius: 5px;
    min-height: 28px;
}}

QScrollBar::handle:vertical:hover {{
    background: {text_faint};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
    height: 0;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}

QScrollBar::handle:horizontal {{
    background: {border};
    border-radius: 5px;
    min-width: 28px;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
    width: 0;
}}

QScrollArea {{
    border: none;
}}

/* ---------------- 分隔与提示条 ---------------- */

QFrame#Divider {{
    background: {border};
    max-height: 1px;
    border: none;
}}

QFrame#Banner {{
    border-radius: 6px;
    border: 1px solid {border};
    background: {surface_alt};
}}
"""


def build_qss(palette: Palette) -> str:
    values = asdict(palette)
    values.update(
        font_family=FONT_FAMILY,
        font_mono=FONT_MONO,
        font_size=BASE_FONT_SIZE,
        small=BASE_FONT_SIZE - 1,
        h1=BASE_FONT_SIZE + 8,
        h2=BASE_FONT_SIZE + 3,
        h3=BASE_FONT_SIZE + 1,
        stat=BASE_FONT_SIZE + 14,
        radius=RADIUS,
    )
    return QSS_TEMPLATE.format(**values)
