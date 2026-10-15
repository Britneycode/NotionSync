"""Notion 风格的主题覆盖（设计 v2 §9 P4/P5）。

配色照 Notion 真身来：白底、暖灰副表面、暖黑文字、蓝色强调色（浅色
#FFFFFF / #F7F6F3 / #37352F / #2383E2；深色 #191919 / #2F2F2F / #D4D4D2）。

**机制上有个坑**：qfluentwidgets 的组件几乎都是自绘（QPainter），窗口级 QSS 打不
进去——`CardWidget { background-color: ... }` 这类规则是死配置。能改的只有三处：

1. `setThemeColor()`：全局强调色。
2. `FluentWindow.setCustomBackgroundColor(light, dark)`：窗口底色。卡片/内容区都是
   半透明白叠加，底色一变它们跟着走——这是最有效的一招。前提是关掉 Mica（开着
   毛玻璃时它返回透明，底色被整个冲掉）。
3. `setCustomStyleSheet()`：给**单个** Fluent 控件补规则。它把我们的 QSS 接在
   Fluent 自己那套后面（同优先级后写的赢），且控件已注册到样式管理器，切主题时
   会连同 Fluent 的一起重刷——所以侧栏、内容区这两处覆盖得住。

卡片/按钮的形状仍是 Fluent 自绘的，改不了也不该改。
"""

from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, isDarkTheme, setTheme, setThemeColor
from qfluentwidgets.common.style_sheet import setCustomStyleSheet

# ---------------------------------------------------------------- Notion 色板

# 强调色：Notion 的链接/主按钮蓝
ACCENT = "#2383E2"

LIGHT = {
    "bg": "#FFFFFF",  # 窗口底
    "surface": "#F7F6F3",  # 侧栏/副表面
    "surface_hover": "#EFEDEA",
    "surface_pressed": "#E9E6E1",
    "text": "#37352F",  # Notion 的暖黑
    "text_secondary": "#6F6E69",
    "text_tertiary": "#9B9A97",
    "border": "#E9E7E4",
    "divider": "#F0EEEA",
}

DARK = {
    "bg": "#191919",
    "surface": "#2F2F2F",
    "surface_hover": "#3F3F3F",
    "surface_pressed": "#4A4A4A",
    "text": "#D4D4D2",
    "text_secondary": "#9B9B98",
    "text_tertiary": "#6F6F6C",
    "border": "#3A3A3A",
    "divider": "#2A2A2A",
}

# 自绘组件吃不到 QSS，这里只列真会生效的：原生控件与文字
_QSS = """
QLabel, QToolTip, QMenu {{
    color: {text};
}}
CaptionLabel {{
    color: {text_secondary};
}}
QSplitter::handle {{
    background-color: {divider};
}}
QScrollBar:vertical, QScrollBar:horizontal {{
    background: transparent;
    width: 10px;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {border};
    border-radius: 5px;
    min-height: 20px;
    min-width: 20px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
    background: {text_tertiary};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}
"""

# 侧栏：Notion 是暖灰副表面。Fluent 只在 `menu=true`（浮层态）给面板上色，
# 常驻的 `menu=false` 是透明的——它想跟窗口底色一致，而 Notion 不是。
_NAV_QSS = """
NavigationPanel[menu=true], NavigationPanel[menu=false] {{
    background-color: {surface};
    border: none;
    border-right: 1px solid {divider};
    border-top-right-radius: 0;
    border-bottom-right-radius: 0;
}}
"""

# 内容区：Fluent 默认盖了一层 3% 白 + 圆角 + 边框，Notion 的内容区和窗口是齐平的。
# 那层 3% 白在浅色下看不出来，深色下会把 #191919 抬成 #202020。
_STACK_QSS = """
StackedWidget {
    background-color: transparent;
    border: none;
    border-top-left-radius: 0;
}
"""


def apply_theme(name: str) -> None:
    """应用主题名（light/dark/auto）+ Notion 配色。切主题时也走这里。"""
    setTheme({"light": Theme.LIGHT, "dark": Theme.DARK}.get(name, Theme.AUTO))
    app = QApplication.instance()
    if app is None:
        return
    setThemeColor(ACCENT)
    colors = DARK if isDarkTheme() else LIGHT
    app.setStyleSheet(_QSS.format(**colors))


def install(window, theme_name: str = "auto") -> None:
    """把 Notion 的观感装到窗口上。窗口构造完之后、show 之前调用一次。

    除了配色，这里还把侧栏展开成「图标 + 文字」：Fluent 默认是 48px 的图标条，
    Notion 的侧栏是带文字的常驻栏。窗口宽度不够时 Fluent 自己会收回图标条。

    窗口底色的亮/暗两套值一次给全，Fluent 切主题时会自己换，不用每次重设。
    Mica 在这里关掉，而不是让调用方记着关：开着毛玻璃时窗口底色返回透明，
    设进去的颜色会被整个冲掉——这种失败没有任何报错，只是"看着不像"。
    """
    window.setMicaEffectEnabled(False)  # Notion 是纯色底
    window.setCustomBackgroundColor(LIGHT["bg"], DARK["bg"])

    panel = getattr(getattr(window, "navigationInterface", None), "panel", None)
    if panel is not None:
        panel.setAcrylicEnabled(False)  # 毛玻璃会把侧栏底色冲淡
        panel.setExpandWidth(240)
        panel.expand(useAni=False)
        # Fluent 把导航的样式表挂在面板上（不是 NavigationInterface），所以覆盖
        # 也得挂在面板上：挂在祖先上会被面板自己那套盖掉。
        setCustomStyleSheet(panel, _NAV_QSS.format(**LIGHT), _NAV_QSS.format(**DARK))

    stack = getattr(window, "stackedWidget", None)
    if stack is not None:
        setCustomStyleSheet(stack, _STACK_QSS, _STACK_QSS)
    apply_theme(theme_name)
