"""
datasource_panel.py — 数据源选择 + 配置面板

根据选中的数据源类型，动态切换下方的配置区域（QStackedWidget）。
支持：Wikipedia FR/EN/DE、通用网页（现有 UrlPanel + ConfigPanel）
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QStackedWidget, QPlainTextEdit, QSpinBox, QRadioButton,
    QGroupBox, QFormLayout, QPushButton, QButtonGroup,
)


class DatasourcePanel(QWidget):
    """数据采集面板：数据源选择 + 动态配置区。

    外部通过 get_values() 获取完整配置 dict，其中 datasource 字段
    标识当前选择的数据源类型。
    """

    def __init__(self, config: dict, url_panel: QWidget, cfg_panel: QWidget,
                 parent=None):
        super().__init__(parent)
        self._config = config
        self._url_panel = url_panel
        self._cfg_panel = cfg_panel
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # ── 数据源选择行 ────────────────────────────────────────────────
        top = QHBoxLayout()
        top.addWidget(QLabel("数据源："))
        self._ds_combo = QComboBox()
        self._ds_combo.addItem("通用网页", "web")
        self._ds_combo.addItem("Wikipedia FR", "wikipedia_fr")
        self._ds_combo.addItem("Wikipedia EN", "wikipedia_en")
        self._ds_combo.addItem("Wikipedia DE", "wikipedia_de")
        self._ds_combo.currentIndexChanged.connect(self._on_datasource_changed)
        top.addWidget(self._ds_combo)
        top.addStretch()
        layout.addLayout(top)

        # ── 配置区（StackedWidget 切换） ─────────────────────────────────
        self._stack = QStackedWidget()
        self._stack.addWidget(self._url_panel)        # index 0: web（复用现有）
        self._stack.addWidget(self._build_wiki_page()) # index 1: wikipedia
        layout.addWidget(self._stack, stretch=1)

        # 初始状态
        ds = self._config.get("datasource", "web")
        idx = self._ds_combo.findData(ds)
        self._ds_combo.setCurrentIndex(max(idx, 0))
        self._on_datasource_changed()

    # ── Wikipedia 配置页 ────────────────────────────────────────────────

    def _build_wiki_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)

        # 枚举方式
        enum_box = QGroupBox("枚举方式")
        enum_lay = QHBoxLayout(enum_box)
        self._rb_cat = QRadioButton("按分类")
        self._rb_prefix = QRadioButton("按前缀")
        self._rb_titles = QRadioButton("指定标题列表")
        self._rb_cat.setChecked(True)
        self._enum_group = QButtonGroup()
        self._enum_group.addButton(self._rb_cat, 0)
        self._enum_group.addButton(self._rb_prefix, 1)
        self._enum_group.addButton(self._rb_titles, 2)
        self._enum_group.buttonClicked.connect(self._on_enum_mode_changed)
        enum_lay.addWidget(self._rb_cat)
        enum_lay.addWidget(self._rb_prefix)
        enum_lay.addWidget(self._rb_titles)
        enum_lay.addStretch()
        layout.addWidget(enum_box)

        # 枚举参数区（StackedWidget）
        self._enum_stack = QStackedWidget()

        # 0: 分类名
        cat_w = QWidget()
        cat_lay = QVBoxLayout(cat_w)
        cat_lay.setContentsMargins(0, 0, 0, 0)
        cat_lay.addWidget(QLabel("分类名（每行一个，不含 Catégorie: 前缀）："))
        self._wiki_cats = QPlainTextEdit()
        self._wiki_cats.setPlaceholderText("Physique\nMathématiques\nIntelligence artificielle")
        self._wiki_cats.setMaximumHeight(100)
        cats = self._config.get("wiki_categories", "")
        if isinstance(cats, str) and cats:
            self._wiki_cats.setPlainText(cats)
        cat_lay.addWidget(self._wiki_cats)
        self._enum_stack.addWidget(cat_w)  # index 0

        # 1: 前缀
        pre_w = QWidget()
        pre_lay = QFormLayout(pre_w)
        self._wiki_prefix = QPlainTextEdit()
        self._wiki_prefix.setPlaceholderText("Chat")
        self._wiki_prefix.setMaximumHeight(50)
        pre_lay.addRow("标题前缀：", self._wiki_prefix)
        self._enum_stack.addWidget(pre_w)  # index 1

        # 2: 标题列表
        tit_w = QWidget()
        tit_lay = QVBoxLayout(tit_w)
        tit_lay.setContentsMargins(0, 0, 0, 0)
        tit_lay.addWidget(QLabel("页面标题（每行一个）："))
        self._wiki_titles = QPlainTextEdit()
        self._wiki_titles.setPlaceholderText("Chat\nChien\nPhysique")
        self._wiki_titles.setMaximumHeight(100)
        tit_lay.addWidget(self._wiki_titles)
        self._enum_stack.addWidget(tit_w)  # index 2

        layout.addWidget(self._enum_stack)

        # 通用参数
        param_box = QGroupBox("采集参数")
        param_lay = QFormLayout(param_box)

        self._wiki_depth = QSpinBox()
        self._wiki_depth.setRange(1, 5)
        self._wiki_depth.setValue(int(self._config.get("wiki_category_depth", 1)))
        self._wiki_depth.setToolTip("仅「按分类」模式生效：递归获取子分类的深度")
        param_lay.addRow("子分类递归深度", self._wiki_depth)

        self._wiki_max = QSpinBox()
        self._wiki_max.setRange(1, 5000)
        self._wiki_max.setValue(int(self._config.get("wiki_max_per_cat", 500)))
        param_lay.addRow("每分类最多页面", self._wiki_max)

        # 内容模式
        content_row = QHBoxLayout()
        self._rb_full = QRadioButton("全文")
        self._rb_intro = QRadioButton("仅引言")
        self._rb_full.setChecked(
            self._config.get("wiki_content_mode", "full") == "full"
        )
        self._rb_intro.setChecked(
            self._config.get("wiki_content_mode", "full") != "full"
        )
        content_row.addWidget(QLabel("内容："))
        content_row.addWidget(self._rb_full)
        content_row.addWidget(self._rb_intro)
        content_row.addStretch()
        param_lay.addRow(content_row)

        layout.addWidget(param_box)
        layout.addStretch()
        return page

    # ── 槽 ──────────────────────────────────────────────────────────────

    def _on_datasource_changed(self) -> None:
        ds = self._ds_combo.currentData()
        if ds == "web":
            self._stack.setCurrentIndex(0)
        else:
            self._stack.setCurrentIndex(1)

    def _on_enum_mode_changed(self) -> None:
        self._enum_stack.setCurrentIndex(self._enum_group.checkedId())

    # ── 公共接口 ────────────────────────────────────────────────────────

    def get_values(self) -> dict:
        ds = self._ds_combo.currentData()

        result: dict = {"datasource": ds}

        if ds == "web":
            # 委托给现有的 UrlPanel / ConfigPanel
            # 调用方需要分别调用这两个面板
            pass
        else:
            # Wikipedia 系列
            result["wiki_enum_mode"] = (
                "category" if self._rb_cat.isChecked()
                else "prefix" if self._rb_prefix.isChecked()
                else "titles"
            )
            result["wiki_categories"] = self._wiki_cats.toPlainText().strip()
            result["wiki_prefix"] = self._wiki_prefix.toPlainText().strip()
            result["wiki_titles"] = self._wiki_titles.toPlainText().strip()
            result["wiki_category_depth"] = self._wiki_depth.value()
            result["wiki_max_per_cat"] = self._wiki_max.value()
            result["wiki_content_mode"] = "full" if self._rb_full.isChecked() else "intro"

        return result

    def current_datasource(self) -> str:
        return self._ds_combo.currentData()
