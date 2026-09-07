"""
log_panel.py - 日志输出面板
只读文本区，支持追加、清空、自动滚底
"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QLabel,
)
from PySide6.QtGui import QTextCursor
from PySide6.QtCore import Qt


class LogPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(QLabel("运行日志"))
        header.addStretch()
        btn_clear = QPushButton("清空日志")
        btn_clear.clicked.connect(self.clear)
        header.addWidget(btn_clear)
        layout.addLayout(header)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._text, stretch=1)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def append(self, msg: str) -> None:
        """追加一行日志并滚动到底部"""
        self._text.appendPlainText(msg)
        self._text.moveCursor(QTextCursor.MoveOperation.End)

    def clear(self) -> None:
        self._text.clear()
