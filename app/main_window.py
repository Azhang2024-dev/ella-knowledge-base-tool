"""
main_window.py - 主窗口
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QPushButton, QLabel, QStatusBar, QMessageBox, QTabWidget,
    QApplication,
)

from app.widgets.url_panel import UrlPanel
from app.widgets.config_panel import ConfigPanel
from app.widgets.log_panel import LogPanel
from app.widgets.llm_panel import _load as _load_llm_cfg
from app.widgets.doc_panel import DocPanel
from app.widgets.doc_query_panel import DocQueryPanel
from app.widgets.general_query_panel import GeneralQueryPanel
from app.widgets.convert_panel import ConvertPanel
from app.widgets.datasource_panel import DatasourcePanel
from core.config_manager import load_config, save_config
from workers.crawl_worker import CrawlWorker


class MainWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Web Crawler")
        self.setMinimumSize(860, 540)

        # 按可用屏幕面积的 88% 定初始大小，避免 HiDPI 缩放下溢出
        screen = QApplication.primaryScreen().availableGeometry()
        w = min(1200, int(screen.width()  * 0.88))
        h = min(760,  int(screen.height() * 0.88))
        self.resize(w, h)
        self.move(
            screen.x() + (screen.width()  - w) // 2,
            screen.y() + (screen.height() - h) // 2,
        )

        self._worker: CrawlWorker | None = None
        self._paused = False
        self._config = load_config()

        self._build_menu()
        self._build_ui()

    # ── 菜单栏 ────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        bar = self.menuBar()

        # 文件
        file_menu = bar.addMenu("文件(&F)")
        act_save = QAction("保存配置(&S)", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self._on_save_config)
        act_open_out = QAction("打开输出目录(&O)", self)
        act_open_out.triggered.connect(self._open_output_dir)
        act_quit = QAction("退出(&Q)", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_save)
        file_menu.addAction(act_open_out)
        file_menu.addSeparator()
        file_menu.addAction(act_quit)

        # 设置
        settings_menu = bar.addMenu("设置(&S)")
        act_llm = QAction("LLM / 智能模式配置…(&L)", self)
        act_llm.triggered.connect(self._open_llm_dialog)
        settings_menu.addAction(act_llm)

        # 帮助
        help_menu = bar.addMenu("帮助(&H)")
        act_about = QAction("关于(&A)", self)
        act_about.triggered.connect(self._show_about)
        act_playwright = QAction("安装 Playwright 浏览器…", self)
        act_playwright.triggered.connect(self._show_playwright_guide)
        help_menu.addAction(act_about)
        help_menu.addAction(act_playwright)

    # ── 主界面 ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(4)

        tabs = QTabWidget()
        root.addWidget(tabs, stretch=1)

        # Tab 0: 数据采集
        crawler_tab = QWidget()
        crawler_lay = QVBoxLayout(crawler_tab)
        crawler_lay.setContentsMargins(0, 4, 0, 0)
        crawler_lay.setSpacing(6)

        # ── 工具栏按钮行 ─────────────────────────────────────────────
        btn_bar = QHBoxLayout()
        self._btn_start = QPushButton("▶  开始")
        self._btn_pause = QPushButton("⏸  暂停")
        self._btn_end   = QPushButton("⏹  结束")
        self._btn_pause.setEnabled(False)
        self._btn_end.setEnabled(False)
        self._lbl_status = QLabel("就绪")

        self._btn_start.clicked.connect(self._on_start)
        self._btn_pause.clicked.connect(self._on_pause_resume)
        self._btn_end.clicked.connect(self._on_end)

        for btn in (self._btn_start, self._btn_pause, self._btn_end):
            btn.setFixedHeight(30)
            btn_bar.addWidget(btn)
        btn_bar.addStretch()
        btn_bar.addWidget(self._lbl_status)
        crawler_lay.addLayout(btn_bar)

        # ── 数据源选择 + 配置 ────────────────────────────────────────
        self._url_panel = UrlPanel(self._config)
        self._cfg_panel = ConfigPanel(self._config)
        self._ds_panel = DatasourcePanel(
            self._config, self._url_panel, self._cfg_panel
        )

        # 左：数据源选择区 + URL/配置；右：日志
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(4)

        left_lay.addWidget(self._ds_panel)
        # 通用网页配置区（仅在 datasource=web 时显示）
        self._web_config_widget = QWidget()
        web_cfg_lay = QVBoxLayout(self._web_config_widget)
        web_cfg_lay.setContentsMargins(0, 0, 0, 0)
        web_cfg_lay.setSpacing(4)
        web_cfg_lay.addWidget(self._url_panel, stretch=1)
        web_cfg_lay.addWidget(self._cfg_panel, stretch=3)
        left_lay.addWidget(self._web_config_widget, stretch=1)

        self._log_panel = LogPanel()

        splitter.addWidget(left)
        splitter.addWidget(self._log_panel)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        crawler_lay.addWidget(splitter, stretch=1)

        # 数据源切换时 显示/隐藏 通用网页配置区
        self._ds_panel._ds_combo.currentIndexChanged.connect(
            self._on_ds_ui_changed
        )
        self._on_ds_ui_changed()

        # Tab 1: 文档处理
        tabs.addTab(crawler_tab,          "数据采集")
        tabs.addTab(DocPanel(),           "文档处理")
        tabs.addTab(DocQueryPanel(),      "指定文档问答Query")
        tabs.addTab(GeneralQueryPanel(),  "通用问答Query")
        tabs.addTab(ConvertPanel(),       "格式转换")

        self.setStatusBar(QStatusBar())

    # ── 数据源 UI 切换 ────────────────────────────────────────────────

    def _on_ds_ui_changed(self) -> None:
        """数据源切换时显示/隐藏通用网页配置区。"""
        ds = self._ds_panel.current_datasource()
        self._web_config_widget.setVisible(ds == "web")

    # ── 配置收集 ──────────────────────────────────────────────────────

    def _collect_config(self) -> dict:
        cfg = dict(self._config)
        cfg.update(self._url_panel.get_values())
        cfg.update(self._cfg_panel.get_values())
        # 数据源面板的值（datasource, wiki_* 等）
        cfg.update(self._ds_panel.get_values())
        # 智能模式：合入 llm_config.json 中已保存的 LLM 配置
        if cfg.get("crawl_mode") == "smart":
            cfg.update(_load_llm_cfg())
        return cfg

    # ── 按钮槽 ────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        if self._worker and self._worker.isRunning():
            return

        config = self._collect_config()
        ds = config.get("datasource", "web")

        # Wikipedia 等 API 数据源不需要 start_urls
        if ds == "web" and not config.get("start_urls"):
            QMessageBox.warning(self, "提示", "请先输入至少一个起始 URL")
            return

        if config.get("crawl_mode") == "smart" and not self._check_playwright():
            return

        self._paused = False
        self._log_panel.clear()
        self._log_panel.append("任务已启动……")

        self._worker = CrawlWorker(config)
        self._worker.log_signal.connect(self._log_panel.append)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.finished_signal.connect(self._on_finished)
        self._worker.error_signal.connect(self._on_error)
        self._worker.start()

        self._btn_start.setEnabled(False)
        self._btn_pause.setEnabled(True)
        self._btn_end.setEnabled(True)
        self._lbl_status.setText("爬取中…")

    def _on_pause_resume(self) -> None:
        if not self._worker:
            return
        if not self._paused:
            self._worker.request_pause()
            self._paused = True
            self._btn_pause.setText("▶  继续")
            self._lbl_status.setText("已暂停")
            self._log_panel.append("── 已暂停 ──")
        else:
            self._worker.request_resume()
            self._paused = False
            self._btn_pause.setText("⏸  暂停")
            self._lbl_status.setText("爬取中…")
            self._log_panel.append("── 已继续 ──")

    def _on_end(self) -> None:
        if self._worker:
            self._worker.request_stop()
        self._btn_pause.setEnabled(False)
        self._btn_end.setEnabled(False)
        self._lbl_status.setText("正在结束…")
        self._log_panel.append("── 已请求结束 ──")

    def _on_save_config(self) -> None:
        cfg = self._collect_config()
        save_config(cfg)
        self._config = cfg
        self.statusBar().showMessage("配置已保存", 3000)

    def _open_llm_dialog(self) -> None:
        from app.dialogs.llm_dialog import LLMConfigDialog
        dlg = LLMConfigDialog(self._collect_config(), self)
        dlg.exec()  # 确认时 LLMPanel.get_values() 已自动持久化

    def _open_output_dir(self) -> None:
        import os, subprocess
        path = self._cfg_panel.get_values().get("output_dir", "output")
        os.makedirs(path, exist_ok=True)
        subprocess.Popen(f'explorer "{os.path.abspath(path)}"')

    def _show_about(self) -> None:
        QMessageBox.about(
            self, "关于",
            "Web Crawler GUI\n\n"
            "经典模式：requests + BeautifulSoup\n"
            "智能模式：crawl4ai (Playwright) + LLM 提取",
        )

    def _show_playwright_guide(self) -> None:
        QMessageBox.information(
            self, "安装 Playwright 浏览器",
            "在终端执行以下命令安装 Chromium：\n\n"
            "    pip install crawl4ai\n"
            "    playwright install chromium",
        )

    def _check_playwright(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                b.close()
            return True
        except Exception:
            self._show_playwright_guide()
            return False

    # ── Worker 信号槽 ─────────────────────────────────────────────────

    def _on_progress(self, success: int, failed: int) -> None:
        if not self._paused:
            self._lbl_status.setText(f"已下载 {success}  失败 {failed}")

    def _on_finished(self, result: dict) -> None:
        # 兼容两种 result 格式：
        # - web 模式: {downloaded, skipped, failed, report_path}
        # - datasource 模式: {total, success, failed, output_dir, jsonl_path}
        if "total" in result:
            total, ok, fail = result.get("total", 0), result.get("success", 0), result.get("failed", 0)
            self._log_panel.append(f"\n完成：共 {total} 条目，成功 {ok}，失败 {fail}")
            if out := result.get("output_dir"):
                self._log_panel.append(f"输出：{out}")
            self._lbl_status.setText(f"完成 — ✓{ok}  ✗{fail}")
        else:
            dl, skip, fail = result.get("downloaded", 0), result.get("skipped", 0), result.get("failed", 0)
            self._log_panel.append(f"\n完成：下载 {dl}，跳过 {skip}（重复），失败 {fail}")
            if rp := result.get("report_path"):
                self._log_panel.append(f"AI 报告：{rp}")
            self._lbl_status.setText(f"完成 — ↓{dl}  ✗{fail}")
        self._paused = False
        self._btn_start.setEnabled(True)
        self._btn_pause.setEnabled(False)
        self._btn_pause.setText("⏸  暂停")
        self._btn_end.setEnabled(False)

    def _on_error(self, msg: str) -> None:
        self._log_panel.append(f"[FATAL] {msg}")
        QMessageBox.critical(self, "爬虫异常", msg)
        self._paused = False
        self._btn_start.setEnabled(True)
        self._btn_pause.setEnabled(False)
        self._btn_pause.setText("⏸  暂停")
        self._btn_end.setEnabled(False)
        self._lbl_status.setText("出错")
