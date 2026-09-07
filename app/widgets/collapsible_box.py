"""
collapsible_box.py - 可折叠容器组件
点击标题行展开/收起内部内容
"""

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QToolButton, QLabel, QSizePolicy, QFrame,
)


class CollapsibleBox(QWidget):
    """带折叠按钮的分组容器，内部放任意 widget。"""

    def __init__(self, title: str = "", collapsed: bool = False, parent=None):
        super().__init__(parent)
        self._collapsed = not collapsed  # 先取反，_toggle 会翻转

        # 标题行
        header = QFrame()
        header.setFrameShape(QFrame.Shape.StyledPanel)
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(6, 4, 6, 4)

        self._arrow = QToolButton()
        self._arrow.setStyleSheet("QToolButton { border: none; }")
        self._arrow.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._title_label = QLabel(title)

        header_layout.addWidget(self._arrow)
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()

        # 内容区
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(4)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(header)
        root.addWidget(self._content)

        header.mousePressEvent = lambda _: self._toggle()
        self._arrow.clicked.connect(self._toggle)

        self._toggle()  # 设置初始状态

    def _toggle(self) -> None:
        self._collapsed = not self._collapsed
        self._content.setVisible(not self._collapsed)
        self._arrow.setText("▶" if self._collapsed else "▼")

    def add_widget(self, widget: QWidget) -> None:
        self._content_layout.addWidget(widget)

    def content_layout(self) -> QVBoxLayout:
        return self._content_layout
