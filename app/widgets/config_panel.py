"""
config_panel.py - 爬虫参数配置面板

布局：
  Tab 1「爬取设置」— 模式切换 / 基础参数 / [折叠] 高级选项
  Tab 2「文件类型」— 目标扩展名复选框
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QTabWidget, QFormLayout, QSpinBox, QDoubleSpinBox,
    QLineEdit, QCheckBox, QGroupBox, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QLabel, QRadioButton,
    QScrollArea, QFrame, QComboBox,
)

from app.widgets.collapsible_box import CollapsibleBox

# 扩展名分组
_EXT_GROUPS = [
    ("文档",  ["pdf", "doc", "docx", "odt"]),
    ("表格",  ["xls", "xlsx", "ods", "csv"]),
    ("演示",  ["ppt", "pptx", "odp"]),
    ("文本",  ["txt"]),
    ("压缩",  ["zip", "rar", "7z", "tar", "gz"]),
    ("电子书", ["epub", "mobi"]),
]


class ConfigPanel(QWidget):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._ext_checks: dict[str, QCheckBox] = {}
        self._build_ui(config)

    def _build_ui(self, cfg: dict) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(0)

        tabs = QTabWidget()
        tabs.addTab(self._build_crawl_tab(cfg), "爬取设置")
        tabs.addTab(self._build_types_tab(cfg), "文件类型")
        root.addWidget(tabs)

    # ── Tab 1：爬取设置 ───────────────────────────────────────────────

    def _build_crawl_tab(self, cfg: dict) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # 模式切换
        mode_box = QGroupBox("爬取模式")
        mode_lay = QHBoxLayout(mode_box)
        self._rb_classic  = QRadioButton("经典（静态 HTML）")
        self._rb_smart    = QRadioButton("智能（JS 渲染 + LLM）")
        self._rb_firecrawl = QRadioButton("Firecrawl（云端渲染）")
        crawl_mode = cfg.get("crawl_mode", "classic")
        self._rb_classic.setChecked(crawl_mode == "classic")
        self._rb_smart.setChecked(crawl_mode == "smart")
        self._rb_firecrawl.setChecked(crawl_mode == "firecrawl")
        mode_lay.addWidget(self._rb_classic)
        mode_lay.addWidget(self._rb_smart)
        mode_lay.addWidget(self._rb_firecrawl)
        mode_lay.addStretch()
        layout.addWidget(mode_box)

        # 基础参数
        basic_box = QGroupBox("基础参数")
        form = QFormLayout(basic_box)
        form.setVerticalSpacing(6)
        form.setHorizontalSpacing(10)

        self._max_files = QSpinBox()
        self._max_files.setRange(1, 10000)
        self._max_files.setValue(int(cfg.get("max_files", 50)))
        form.addRow("最大下载文件数", self._max_files)

        self._max_pages = QSpinBox()
        self._max_pages.setRange(1, 10000)
        self._max_pages.setValue(int(cfg.get("max_pages", 100)))
        form.addRow("最大扫描页数", self._max_pages)

        self._max_depth = QSpinBox()
        self._max_depth.setRange(0, 10)
        self._max_depth.setValue(int(cfg.get("max_depth", 2)))
        form.addRow("最大爬取深度", self._max_depth)

        self._same_domain = QCheckBox("仅爬取同域名链接")
        self._same_domain.setChecked(bool(cfg.get("same_domain_only", True)))
        form.addRow("", self._same_domain)

        dir_row = QHBoxLayout()
        self._output_dir = QLineEdit(cfg.get("output_dir", "output"))
        btn_browse = QPushButton("浏览…")
        btn_browse.setFixedWidth(52)
        btn_browse.clicked.connect(self._browse_dir)
        dir_row.addWidget(self._output_dir)
        dir_row.addWidget(btn_browse)
        form.addRow("输出目录", dir_row)

        layout.addWidget(basic_box)

        # 正文落盘
        content_box = QGroupBox("正文落盘（智能 / Firecrawl 模式）")
        content_lay = QVBoxLayout(content_box)
        content_lay.setSpacing(4)

        self._save_content = QCheckBox("保存页面正文（新闻 / 博客 / 公众号文章）")
        self._save_content.setChecked(bool(cfg.get("save_page_content", False)))
        content_lay.addWidget(self._save_content)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("格式："))
        self._fmt_checks: dict[str, QCheckBox] = {}
        active_fmts = set(cfg.get("content_formats", ["txt"]))
        for fmt, label in [("txt", "TXT（分篇）"), ("md", "MD（分篇）"), ("xlsx", "Excel（汇总）")]:
            cb = QCheckBox(label)
            cb.setChecked(fmt in active_fmts)
            self._fmt_checks[fmt] = cb
            fmt_row.addWidget(cb)
        fmt_row.addStretch()
        content_lay.addLayout(fmt_row)
        layout.addWidget(content_box)

        # Firecrawl 专属
        fc_box = QGroupBox("Firecrawl 设置（仅 Firecrawl 模式生效）")
        fc_form = QFormLayout(fc_box)
        fc_form.setVerticalSpacing(5)
        fc_form.setHorizontalSpacing(10)

        self._fc_search = QLineEdit(cfg.get("firecrawl_search", ""))
        self._fc_search.setPlaceholderText("留空则只抓起始 URL；填写则用 /search 发现页面")
        fc_form.addRow("搜索发现", self._fc_search)

        self._fc_map = QCheckBox("用 /map 扩展发现站内链接")
        self._fc_map.setChecked(bool(cfg.get("firecrawl_use_map", False)))
        fc_form.addRow("", self._fc_map)

        self._fc_proxy = QComboBox()
        for val, label in [("auto", "auto（自动升级，推荐）"),
                            ("basic", "basic（快速，无/轻反爬）"),
                            ("enhanced", "enhanced（慢但稳，强反爬 / 403 时用）")]:
            self._fc_proxy.addItem(label, val)
        saved_proxy = cfg.get("firecrawl_proxy", "auto")
        idx = next((i for i in range(self._fc_proxy.count())
                    if self._fc_proxy.itemData(i) == saved_proxy), 0)
        self._fc_proxy.setCurrentIndex(idx)
        fc_form.addRow("代理档位", self._fc_proxy)
        layout.addWidget(fc_box)

        # 高级选项（可折叠，默认收起）
        adv = CollapsibleBox("高级选项", collapsed=True)
        adv_form = QFormLayout()
        adv_form.setVerticalSpacing(5)
        adv_form.setHorizontalSpacing(10)

        self._interval = QDoubleSpinBox()
        self._interval.setRange(0.0, 60.0)
        self._interval.setSingleStep(0.5)
        self._interval.setDecimals(1)
        self._interval.setValue(float(cfg.get("request_interval", 1.0)))
        self._interval.setSuffix(" 秒")
        adv_form.addRow("请求间隔", self._interval)

        self._timeout = QSpinBox()
        self._timeout.setRange(1, 120)
        self._timeout.setValue(int(cfg.get("timeout", 15)))
        self._timeout.setSuffix(" 秒")
        adv_form.addRow("超时时间", self._timeout)

        self._max_retries = QSpinBox()
        self._max_retries.setRange(0, 10)
        self._max_retries.setValue(int(cfg.get("max_retries", 3)))
        adv_form.addRow("失败重试次数", self._max_retries)

        self._include_kw = QLineEdit(cfg.get("include_keywords", ""))
        self._include_kw.setPlaceholderText("逗号分隔，留空不过滤")
        adv_form.addRow("包含关键词", self._include_kw)

        self._exclude_kw = QLineEdit(cfg.get("exclude_keywords", ""))
        self._exclude_kw.setPlaceholderText("逗号分隔")
        adv_form.addRow("排除关键词", self._exclude_kw)

        self._ua = QLineEdit(cfg.get("user_agent", ""))
        self._ua.setPlaceholderText("留空自动随机轮转")
        adv_form.addRow("User-Agent", self._ua)

        self._proxy = QLineEdit(cfg.get("proxy", ""))
        self._proxy.setPlaceholderText("留空不走代理；如 http://host:port 或 socks5://host:port")
        adv_form.addRow("代理地址", self._proxy)

        self._anti_detect = QCheckBox("反检测增强（禁用自动化标志）")
        self._anti_detect.setChecked(bool(cfg.get("anti_detect", True)))
        adv_form.addRow("", self._anti_detect)

        adv_widget = QWidget()
        adv_widget.setLayout(adv_form)
        adv.add_widget(adv_widget)
        layout.addWidget(adv)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        return scroll

    # ── Tab 2：文件类型 ───────────────────────────────────────────────

    def _build_types_tab(self, cfg: dict) -> QScrollArea:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        active = set(cfg.get("target_extensions", ["pdf", "doc", "docx", "txt"]))
        for group_label, exts in _EXT_GROUPS:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{group_label}："))
            for ext in exts:
                cb = QCheckBox(f".{ext}")
                cb.setChecked(ext in active)
                self._ext_checks[ext] = cb
                row.addWidget(cb)
            row.addStretch()
            layout.addLayout(row)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        return scroll

    # ── 槽 ────────────────────────────────────────────────────────────

    def _browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self._output_dir.text().strip() or "."
        )
        if path:
            self._output_dir.setText(path)

    # ── 公共接口 ──────────────────────────────────────────────────────

    def get_values(self) -> dict:
        # 爬取模式
        if self._rb_firecrawl.isChecked():
            crawl_mode = "firecrawl"
        elif self._rb_smart.isChecked():
            crawl_mode = "smart"
        else:
            crawl_mode = "classic"

        return {
            "crawl_mode":        crawl_mode,
            "target_extensions": [e for e, cb in self._ext_checks.items() if cb.isChecked()],
            "max_files":         self._max_files.value(),
            "max_pages":         self._max_pages.value(),
            "max_depth":         self._max_depth.value(),
            "request_interval":  self._interval.value(),
            "timeout":           self._timeout.value(),
            "max_retries":       self._max_retries.value(),
            "same_domain_only":  self._same_domain.isChecked(),
            "include_keywords":  self._include_kw.text().strip(),
            "exclude_keywords":  self._exclude_kw.text().strip(),
            "output_dir":        self._output_dir.text().strip() or "output",
            "user_agent":        self._ua.text().strip(),
            # 正文落盘
            "save_page_content": self._save_content.isChecked(),
            "content_formats":   [f for f, cb in self._fmt_checks.items() if cb.isChecked()],
            # Firecrawl
            "firecrawl_search":  self._fc_search.text().strip(),
            "firecrawl_use_map": self._fc_map.isChecked(),
            "firecrawl_proxy":   self._fc_proxy.currentData(),
            # 代理 / 反检测
            "proxy":             self._proxy.text().strip(),
            "anti_detect":       self._anti_detect.isChecked(),
        }
