"""
main.py - 程序入口
"""

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

try:
    import qdarktheme
    _HAS_DARK = True
except ImportError:
    _HAS_DARK = False

from app.main_window import MainWindow

_ICON_PATH = Path(__file__).parent / "assets" / "app_icon.ico"


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("网页爬虫工具")

    if _ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(_ICON_PATH)))

    if _HAS_DARK:
        qdarktheme.setup_theme("auto")

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
