"""
llm_dialog.py - LLM 配置二级对话框
封装 LLMPanel，点击确定后将配置写回 settings.json
"""

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout

from app.widgets.llm_panel import LLMPanel
from core.config_manager import save_config


class LLMConfigDialog(QDialog):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LLM / 智能模式配置")
        self.resize(520, 600)

        self._panel = LLMPanel(config, self)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._panel, stretch=1)
        layout.addWidget(btns)

    def _on_accept(self) -> None:
        self._values = self._panel.get_values()  # get_values 内部已触发持久化
        self.accept()

    def get_values(self) -> dict:
        """返回最后一次确认的配置，若用户取消则返回 {}。"""
        return getattr(self, "_values", {})
