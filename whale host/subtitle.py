# -*- coding: utf-8 -*-
"""subtitle.py —— 字幕：一个单行悬浮窗，把我正在说的话逐字显示出来。

移植自 Tour guide 的 plugin/subtitle/view.py，按本工具的需要简化：

  · **单行**（原来有两行：agent 与用户；这里只有我说的话）
  · 置顶、无边框、半透明、可拖动、无关闭按钮
  · 字号 18 / 微软雅黑 / 白色 + 黑色阴影
  · 宽度固定 1000，内容超宽自动截断
"""

import os
import sys

_TOUR = r"C:\Users\computer\Desktop\B2\Tour guide"
_LIB = os.path.join(_TOUR, "library")
_PYQT = os.path.join(_LIB, "PyQt6")
for _p in (_PYQT, _LIB):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QGraphicsDropShadowEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)

# ── 尺寸与外观（原样沿用）──────────────────────────────────────────────
PADDING_V = 14
FONT_SIZE = 18
FONT_FAMILY = "Microsoft YaHei"
FIXED_WIDTH = 1000
SIDE_MARGIN = 10

# 背景透明度：聚焦时加深，保证能看清
IDLE_ALPHA = 0
FOCUS_ALPHA = 100
BG_RGB = (30, 30, 30)

SHADOW_COLOR = QColor(0, 0, 0, 200)
SHADOW_BLUR = 6
SHADOW_OFFSET = (2, 2)


def _make_label(parent: QWidget) -> QLabel:
    label = QLabel("", parent)
    label.setFont(QFont(FONT_FAMILY, FONT_SIZE))
    label.setStyleSheet("QLabel { color: #FFFFFF; background: transparent; }")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    shadow = QGraphicsDropShadowEffect(label)
    shadow.setColor(SHADOW_COLOR)
    shadow.setBlurRadius(SHADOW_BLUR)
    shadow.setOffset(*SHADOW_OFFSET)
    label.setGraphicsEffect(shadow)
    return label


class SubtitleWindow(QWidget):
    """单行字幕悬浮窗。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._drag_pos = None
        self._text = ""
        self.selected = False
        # 锚点：尺寸变化时据此重摆，避免往下长到任务栏上
        self._anchor_bottom_y = None
        self._anchor_center_x = None

        self._build_ui()
        self._refresh()

    # ── 界面 ────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.container = QWidget(self)
        self.container.setObjectName("container")
        outer.addWidget(self.container)

        inner = QVBoxLayout(self.container)
        inner.setContentsMargins(SIDE_MARGIN, PADDING_V, SIDE_MARGIN, PADDING_V)
        inner.setSpacing(0)

        self.label = _make_label(self.container)
        inner.addWidget(self.label)
        self._apply_style()

    def _apply_style(self) -> None:
        alpha = FOCUS_ALPHA if self.selected else IDLE_ALPHA
        color = "rgba(%d, %d, %d, %d)" % (BG_RGB[0], BG_RGB[1], BG_RGB[2], alpha)
        self.container.setStyleSheet("#container { background-color: %s; }" % color)

    def set_selected(self, value: bool) -> None:
        value = bool(value)
        if self.selected == value:
            return
        self.selected = value
        self._apply_style()

    def _on_focus_changed(self, _old, now) -> None:
        self.set_selected(now is self or now is self.container or now is self.label)

    # ── 内容 ────────────────────────────────────────────────────────────
    @staticmethod
    def _flatten(text: str) -> str:
        """换行换成空格：字幕是单行，含 \\n 会被 QLabel 按多行排版并裁掉。"""
        if not text:
            return text or ""
        return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")

    def set_text(self, text: str) -> None:
        self._text = self._flatten(text)
        self._refresh()

    def get_text(self) -> str:
        return self._text

    def clear(self) -> None:
        self._text = ""
        self._refresh()

    def push_char(self, ch: str) -> bool:
        """追加一个字；超宽则清空重来。返回 True 表示发生了清空。"""
        if ch in "\n\r":
            ch = " "
        candidate = self._text + ch
        if not self._fits(candidate):
            self.set_text(ch)
            return True
        self.set_text(candidate)
        return False

    def fits(self, text: str) -> bool:
        """放得下吗（不改状态，给节奏层预判用）。"""
        return self._fits(text or "")

    def _max_width(self) -> float:
        return FIXED_WIDTH - SIDE_MARGIN * 2

    def _fits(self, text: str) -> bool:
        metrics = QFontMetrics(self.label.font())
        return metrics.horizontalAdvance(text) <= self._max_width()

    def _refresh(self) -> None:
        self.label.setText(self._text)
        self.label.setVisible(True)

        metrics = QFontMetrics(self.label.font())
        line_h = metrics.height()
        height = line_h + PADDING_V * 4

        self.container.setFixedSize(FIXED_WIDTH, height)
        self.setFixedSize(FIXED_WIDTH, height)
        self._reposition()

    # ── 拖动 ────────────────────────────────────────────────────────────
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.set_selected(True)
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None
            self._anchor_bottom_y = self.y() + self.height()
            self._anchor_center_x = self.x() + self.width() // 2
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    # ── 定位 ────────────────────────────────────────────────────────────
    def move_to_bottom_center(self, margin: int = 40) -> None:
        """摆到桌面中间正下方、任务栏上方。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        rect = screen.availableGeometry()
        self._anchor_bottom_y = rect.bottom() - margin
        self._anchor_center_x = rect.left() + rect.width() // 2
        self._reposition()

    def _reposition(self) -> None:
        if self._anchor_bottom_y is None:
            return
        x = (self._anchor_center_x - self.width() // 2
             if self._anchor_center_x is not None else self.x())
        self.move(x, self._anchor_bottom_y - self.height())


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SubtitleWindow()
    window.set_text("这是字幕窗口的测试文字。")
    window.show()
    window.move_to_bottom_center()
    sys.exit(app.exec())
