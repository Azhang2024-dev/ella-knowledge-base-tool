"""
url_panel.py - URL 输入面板
支持多行 URL 输入、添加/清空操作
"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
)


class UrlPanel(QWidget):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._build_ui(config)

    def _build_ui(self, config: dict) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        layout.addWidget(QLabel("起始 URL（每行一个）"))

        self._text = QPlainTextEdit()
        self._text.setPlaceholderText("https://example.com\nhttps://another.com")
        urls = config.get("start_urls", [])
        if isinstance(urls, list):
            self._text.setPlainText("\n".join(urls))
        layout.addWidget(self._text, stretch=1)

        btn_row = QHBoxLayout()
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._text.clear)
        btn_row.addStretch()
        btn_row.addWidget(btn_clear)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get_values(self) -> dict:
        raw = self._text.toPlainText()
        urls = [line.strip() for line in raw.splitlines() if line.strip()]
        return {"start_urls": urls}
