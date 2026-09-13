"""
general_query_panel.py - 通用问答 Query 流水线面板（Stage B+C）

B 通用 Query 生成（盲生成，输入领域名）  →  C 文件级匹配 → Excel
"""

import json
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QVBoxLayout, QSplitter, QScrollArea,
    QListWidget, QListWidgetItem,
    QPushButton, QLabel, QSpinBox,
    QLineEdit, QFileDialog, QTabWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
    QWidget, QMessageBox, QProgressBar, QPlainTextEdit,
)

from app.widgets.log_panel import LogPanel
from app.widgets.llm_panel import _load as _load_llm_cfg
from core.doc_processor import (
    DEFAULT_GENERAL_QUERY_GEN_PROMPT,
    DEFAULT_FILE_MATCH_PROMPT,
)

_DOC_CFG_PATH = Path(__file__).parent.parent.parent / "config" / "doc_config.json"

_DEFAULT_CFG = {
    "queries_per_domain":    3,
    "concurrency":           3,
    "general_query_out_dir": "output/doc_results/general",
    "general_match_out_dir": "output/doc_results",
    "general_query_gen_prompt": DEFAULT_GENERAL_QUERY_GEN_PROMPT,
    "file_match_prompt":        DEFAULT_FILE_MATCH_PROMPT,
}

_PROMPT_KEYS = ("general_query_gen_prompt", "file_match_prompt")


def _hash_prompt(s: str) -> str:
    """MD5 前12位，用于记录「保存时 DEFAULT 是什么版本」。"""
    import hashlib
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def _load_gen_cfg() -> dict:
    cfg = dict(_DEFAULT_CFG)
    try:
        if _DOC_CFG_PATH.exists():
            saved = json.loads(_DOC_CFG_PATH.read_text("utf-8"))
            for k in _DEFAULT_CFG:
                if k not in _PROMPT_KEYS and k in saved:
                    cfg[k] = saved[k]
            for pk in _PROMPT_KEYS:
                if pk not in saved:
                    continue
                saved_val          = saved[pk]
                saved_default_hash = saved.get(f"{pk}_default_hash")

                if saved_default_hash is None:
                    # ── 旧格式迁移：沿用 _is_default 标志 ──────────────────
                    if not saved.get(f"{pk}_is_default", True):
                        # 旧版还额外过滤了含旧占位符的缓存 prompt
                        candidate = saved_val
                        if "{domains_json}" not in candidate and "{file_list}" not in candidate:
                            cfg[pk] = candidate
                    # else: 保持 _DEFAULT_CFG 中的当前值（自动同步）
                else:
                    # ── 新格式：hash 比对 ────────────────────────────────
                    if _hash_prompt(saved_val) != saved_default_hash:
                        cfg[pk] = saved_val
    except Exception:
        pass
    return cfg


def _save_gen_cfg(values: dict) -> None:
    try:
        existing: dict = {}
        if _DOC_CFG_PATH.exists():
            try:
                existing = json.loads(_DOC_CFG_PATH.read_text("utf-8"))
            except Exception:
                pass
        for k, v in values.items():
            existing[k] = v
            if k in _PROMPT_KEYS:
                existing[f"{k}_default_hash"] = _hash_prompt(_DEFAULT_CFG[k])
                existing.pop(f"{k}_is_default", None)   # 清理旧字段
        _DOC_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DOC_CFG_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), "utf-8"
        )
    except Exception:
        pass


class GeneralQueryPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = _load_gen_cfg()
        self._worker = [None, None]
        self._paused = [False, False]
        self._general_queries: list[dict] = []
        self._one_click_mode = False
        self._build_ui()
        self._apply_stylesheet()

    # ── Top-level ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter)

    # ── Left panel ───────────────────────────────────────────────────────

    def _build_left(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(8)
        lay.addWidget(self._build_section_b())
        lay.addWidget(self._build_section_c())
        lay.addStretch()
        scroll.setWidget(container)
        return scroll

    # ── Section B: General Query Gen（盲生成，输入领域名）────────────────

    def _build_section_b(self) -> QWidget:
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(6)

        hint = QLabel("每行输入一个领域名称（盲生成，不接触文档内容）：")
        hint.setObjectName("labelSmall")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self._domain_edit = QPlainTextEdit()
        self._domain_edit.setPlaceholderText(
            "示例：\n国际政治/外交\n体育赛事/竞技运动\n区域发展/国内政策"
        )
        self._domain_edit.setMinimumHeight(100)
        self._domain_edit.setMaximumHeight(150)
        lay.addWidget(self._domain_edit)

        from PySide6.QtWidgets import QFormLayout
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(4)
        self._queries_per_domain = QSpinBox()
        self._queries_per_domain.setRange(1, 200)
        self._queries_per_domain.setValue(int(self._cfg.get("queries_per_domain", 3)))
        self._queries_per_domain.setSuffix(" 条/领域")
        form.addRow("生成数量", self._queries_per_domain)

        self._concurrency = QSpinBox()
        self._concurrency.setRange(1, 20)
        self._concurrency.setSingleStep(1)
        self._concurrency.setValue(int(self._cfg.get("concurrency", 3)))
        self._concurrency.setSuffix(" 个")
        self._concurrency.setToolTip("同时发送的 LLM 请求数；越大速度越快，但会更快消耗 API 限额")
        form.addRow("并发请求数", self._concurrency)

        lay.addLayout(form)

        self._outdirB = QLineEdit(
            self._cfg.get("general_query_out_dir", "output/doc_results/general")
        )
        lay.addLayout(self._make_outdir_row(self._outdirB))

        self._btn_startB, self._btn_pauseB, self._btn_stopB = self._make_ctrl_btns("▶ 开始生成", 0)
        lay.addLayout(self._make_ctrl_row(self._btn_startB, self._btn_pauseB, self._btn_stopB))

        self._pbB, self._statusB = self._make_progress_bar()
        lay.addLayout(self._make_progress_row(self._pbB, self._statusB))

        lay.addWidget(QLabel("生成 Query："))
        self._query_table = QTableWidget()
        self._query_table.setColumnCount(4)
        self._query_table.setHorizontalHeaderLabels(["领域", "Query类型", "Query", "候选文件"])
        hdr = self._query_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._query_table.setAlternatingRowColors(True)
        self._query_table.verticalHeader().setDefaultSectionSize(24)
        self._query_table.setMinimumHeight(100)
        self._query_table.setMaximumHeight(200)
        lay.addWidget(self._query_table)

        self._lbl_queries_ready = QLabel("Query 数据：待生成")
        self._lbl_queries_ready.setObjectName("labelSmall")
        lay.addWidget(self._lbl_queries_ready)

        return self._collapsible_box("B 通用 Query 生成（盲）", inner)

    # ── Section C: File Match ─────────────────────────────────────────────

    def _build_section_c(self) -> QWidget:
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(6)

        self._listC = QListWidget()
        self._listC.setMaximumHeight(90)
        self._listC.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._lbl_countC = QLabel("0 个文件")
        self._lbl_countC.setObjectName("labelSmall")
        self._listC.keyPressEvent = (
            lambda e: self._list_key_press(e, self._listC, self._lbl_countC)
        )
        lay.addLayout(self._make_file_btn_row(self._listC, self._lbl_countC))
        lay.addWidget(self._listC)
        lay.addWidget(self._lbl_countC)

        self._lbl_queries_loaded = QLabel("Query 数据：待载入（请先运行 B 步 Query 生成）")
        self._lbl_queries_loaded.setObjectName("labelSmall")
        self._lbl_queries_loaded.setWordWrap(True)
        lay.addWidget(self._lbl_queries_loaded)

        self._outdirC = QLineEdit(
            self._cfg.get("general_match_out_dir", "output/doc_results")
        )
        lay.addLayout(self._make_outdir_row(self._outdirC))

        self._btn_startC, self._btn_pauseC, self._btn_stopC = self._make_ctrl_btns("▶ 开始匹配", 1)
        lay.addLayout(self._make_ctrl_row(self._btn_startC, self._btn_pauseC, self._btn_stopC))

        self._pbC, self._statusC = self._make_progress_bar()
        lay.addLayout(self._make_progress_row(self._pbC, self._statusC))

        self._btn_one_click = QPushButton("⚡ 一键运行 B+C")
        self._btn_one_click.setObjectName("btnOneClick")
        self._btn_one_click.setFixedHeight(28)
        self._btn_one_click.clicked.connect(self._on_one_click)
        lay.addWidget(self._btn_one_click)

        return self._collapsible_box("C 文件匹配 → Excel", inner)

    # ── Right panel ──────────────────────────────────────────────────────

    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("文件匹配结果预览"))
        hdr.addStretch()
        self._lbl_row_count = QLabel("0 条")
        self._lbl_row_count.setObjectName("labelSmall")
        hdr.addWidget(self._lbl_row_count)
        lay.addLayout(hdr)

        tabs = QTabWidget()
        tabs.addTab(self._build_eval_table(), "匹配结果预览")
        tabs.addTab(self._build_prompt_tab(), "Prompt 编辑")
        tabs.addTab(self._build_log_tab(),    "运行日志")
        lay.addWidget(tabs, stretch=1)
        return w

    def _build_eval_table(self) -> QWidget:
        self._eval_table = QTableWidget()
        self._eval_table.setColumnCount(6)
        self._eval_table.setHorizontalHeaderLabels(
            ["Query", "Query类型", "Query领域", "文件名", "LLM评分", "候选文件"]
        )
        hdr = self._eval_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._eval_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._eval_table.setAlternatingRowColors(True)
        self._eval_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._eval_table.verticalHeader().setDefaultSectionSize(24)
        return self._eval_table

    def _build_prompt_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        hint = QLabel(
            "B Prompt 用 <b>{n}</b>（每领域条数）和 <b>{domains}</b>（领域名列表）；"
            "C Prompt 用 <b>{query}</b> 和 <b>{files}</b>。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("labelSmall")
        lay.addWidget(hint)

        self._prompt_editors: list[QPlainTextEdit] = []
        prompts = [
            ("B Query 生成 Prompt：", "general_query_gen_prompt", DEFAULT_GENERAL_QUERY_GEN_PROMPT),
            ("C 文件匹配 Prompt：",   "file_match_prompt",        DEFAULT_FILE_MATCH_PROMPT),
        ]
        for label, key, default in prompts:
            lay.addWidget(QLabel(label))
            editor = QPlainTextEdit()
            editor.setPlainText(self._cfg.get(key, default))
            editor.setMinimumHeight(120)
            lay.addWidget(editor)
            btn_reset = QPushButton("恢复默认")
            btn_reset.setObjectName("btnSecondary")
            btn_reset.clicked.connect(lambda _, e=editor, d=default: e.setPlainText(d))
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(btn_reset)
            lay.addLayout(row)
            self._prompt_editors.append(editor)

        # ── 底部：保存按钮 + 状态标签 ─────────────────────────
        save_row = QHBoxLayout()
        self._lbl_prompt_saved = QLabel("")
        self._lbl_prompt_saved.setObjectName("labelSmall")
        btn_save_prompt = QPushButton("💾 保存 Prompt")
        btn_save_prompt.setFixedHeight(28)
        btn_save_prompt.clicked.connect(self._on_save_prompts)
        save_row.addWidget(self._lbl_prompt_saved, stretch=1)
        save_row.addWidget(btn_save_prompt)
        lay.addLayout(save_row)

        return w

    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_clear = QPushButton("清空日志")
        btn_clear.setObjectName("btnSecondary")
        btn_clear.setFixedHeight(24)
        btn_clear.clicked.connect(lambda: self._shared_log.clear())
        btn_row.addWidget(btn_clear)
        lay.addLayout(btn_row)
        self._shared_log = LogPanel()
        lay.addWidget(self._shared_log, stretch=1)
        return w

    # ── Shared UI helpers ────────────────────────────────────────────────

    def _make_outdir_row(self, line_edit: QLineEdit) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        lbl = QLabel("输出目录：")
        lbl.setFixedWidth(60)
        btn_browse = QPushButton("浏览…")
        btn_browse.setFixedWidth(54)
        btn_browse.setFixedHeight(24)
        btn_browse.setObjectName("btnSecondary")
        btn_browse.clicked.connect(lambda: self._browse_dir(line_edit))
        btn_open = QPushButton("📂")
        btn_open.setFixedWidth(28)
        btn_open.setFixedHeight(24)
        btn_open.setObjectName("btnSecondary")
        btn_open.clicked.connect(lambda: self._open_dir(line_edit))
        row.addWidget(lbl)
        row.addWidget(line_edit, stretch=1)
        row.addWidget(btn_browse)
        row.addWidget(btn_open)
        return row

    def _make_file_btn_row(self, list_w, count_lbl) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        b_files = QPushButton("＋ 文件")
        b_files.setFixedHeight(26)
        b_files.clicked.connect(lambda: self._add_files(list_w, count_lbl))
        b_folder = QPushButton("＋ 文件夹")
        b_folder.setFixedHeight(26)
        b_folder.clicked.connect(lambda: self._add_folder(list_w, count_lbl))
        b_clear = QPushButton("清空")
        b_clear.setFixedHeight(26)
        b_clear.setObjectName("btnSecondary")
        b_clear.clicked.connect(lambda: self._clear_list(list_w, count_lbl))
        row.addWidget(b_files)
        row.addWidget(b_folder)
        row.addWidget(b_clear)
        row.addStretch()
        return row

    def _make_ctrl_btns(self, start_label: str, stage_idx: int):
        btn_start = QPushButton(start_label)
        btn_pause = QPushButton("⏸ 暂停")
        btn_stop  = QPushButton("■ 停止")
        btn_pause.setEnabled(False)
        btn_stop.setEnabled(False)
        btn_pause.setObjectName("btnSecondary")
        btn_stop.setObjectName("btnSecondary")
        for b in (btn_start, btn_pause, btn_stop):
            b.setFixedHeight(28)
        btn_start.clicked.connect(lambda: self._on_start(stage_idx))
        btn_pause.clicked.connect(lambda: self._on_pause_resume(stage_idx))
        btn_stop.clicked.connect(lambda: self._on_stop(stage_idx))
        return btn_start, btn_pause, btn_stop

    def _make_ctrl_row(self, btn_start, btn_pause, btn_stop) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(btn_start)
        row.addWidget(btn_pause)
        row.addWidget(btn_stop)
        row.addStretch()
        return row

    def _make_progress_bar(self):
        pb = QProgressBar()
        pb.setFixedHeight(12)
        pb.setTextVisible(True)
        pb.setFormat("%v / %m")
        pb.setValue(0)
        lbl = QLabel("就绪")
        lbl.setObjectName("labelSmall")
        return pb, lbl

    def _make_progress_row(self, pb, lbl) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(pb, stretch=1)
        row.addWidget(lbl)
        return row

    # ── File list operations (Section C only) ────────────────────────────

    def _add_files(self, list_w, count_lbl) -> None:
        flt = (
            "支持的文档 (*.pdf *.docx *.doc *.txt *.md *.markdown *.json *.xlsx *.xls);;"
            "PDF (*.pdf);;Word (*.docx *.doc);;文本 (*.txt *.md);;所有文件 (*)"
        )
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", flt)
        self._add_paths_to_list(paths, list_w, count_lbl)

    def _add_folder(self, list_w, count_lbl) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not folder:
            return
        paths = []
        for ext in ("*.txt", "*.md", "*.markdown", "*.pdf", "*.docx", "*.doc",
                    "*.json", "*.xlsx", "*.xls"):
            paths.extend(str(p) for p in Path(folder).rglob(ext))
        self._add_paths_to_list(sorted(paths), list_w, count_lbl)

    def _add_paths_to_list(self, paths, list_w, count_lbl) -> None:
        existing = {
            list_w.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(list_w.count())
        }
        for p in paths:
            if p not in existing:
                item = QListWidgetItem(Path(p).name)
                item.setData(Qt.ItemDataRole.UserRole, p)
                item.setToolTip(p)
                list_w.addItem(item)
        count_lbl.setText(f"{list_w.count()} 个文件")

    def _clear_list(self, list_w, count_lbl) -> None:
        list_w.clear()
        count_lbl.setText("0 个文件")

    def _list_key_press(self, event, list_w, count_lbl) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            for item in list_w.selectedItems():
                list_w.takeItem(list_w.row(item))
            count_lbl.setText(f"{list_w.count()} 个文件")
        else:
            QListWidget.keyPressEvent(list_w, event)

    def _get_file_paths(self, list_w) -> list:
        return [list_w.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(list_w.count())]

    def _get_domains(self) -> list[str]:
        """从 B 步文本框读取领域名列表（每行一个，过滤空行）。"""
        text = self._domain_edit.toPlainText()
        return [line.strip() for line in text.splitlines() if line.strip()]

    # ── Dir helpers ───────────────────────────────────────────────────────

    def _browse_dir(self, line_edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择输出目录", line_edit.text() or "."
        )
        if path:
            line_edit.setText(path)

    def _open_dir(self, line_edit: QLineEdit) -> None:
        path = line_edit.text().strip() or "output/doc_results"
        os.makedirs(path, exist_ok=True)
        subprocess.Popen(f'explorer "{os.path.abspath(path)}"')

    # ── One-click B+C ─────────────────────────────────────────────────────

    def _on_one_click(self) -> None:
        if any(w and w.isRunning() for w in self._worker):
            return
        if not self._get_domains():
            QMessageBox.warning(self, "提示", "请先在 B 步输入框填写领域名称（每行一个）")
            return
        if not self._get_file_paths(self._listC):
            QMessageBox.warning(self, "提示", "请先在 C 步添加用于文件匹配的文件")
            return
        config   = self._collect_config()
        provider = config.get("llm_provider", "")
        api_key  = config.get("llm_api_key", "").strip()
        if not api_key and provider not in ("ollama",):
            QMessageBox.warning(self, "提示", "请先在「设置 → LLM 配置」中填写 API Key")
            return
        self._one_click_mode = True
        self._btn_one_click.setEnabled(False)
        self._on_start(0)

    # ── Config ───────────────────────────────────────────────────────────

    def _collect_config(self) -> dict:
        cfg = {
            "queries_per_domain":    self._queries_per_domain.value(),
            "concurrency":           self._concurrency.value(),
            "general_query_out_dir": self._outdirB.text().strip() or "output/doc_results/general",
            "general_match_out_dir": self._outdirC.text().strip() or "output/doc_results",
            "general_query_gen_prompt": (
                self._prompt_editors[0].toPlainText().strip() or DEFAULT_GENERAL_QUERY_GEN_PROMPT
            ),
            "file_match_prompt": (
                self._prompt_editors[1].toPlainText().strip() or DEFAULT_FILE_MATCH_PROMPT
            ),
        }
        _save_gen_cfg(cfg)
        cfg.update(_load_llm_cfg())
        return cfg

    # ── Stage start / pause / stop ───────────────────────────────────────

    _STAGE_KEYS   = ["general_query_gen", "file_match"]
    _STAGE_LABELS = ["[B Query生成]", "[C 文件匹配]"]

    def _starts(self):   return [self._btn_startB, self._btn_startC]
    def _pauses(self):   return [self._btn_pauseB, self._btn_pauseC]
    def _stops(self):    return [self._btn_stopB,  self._btn_stopC]
    def _pbs(self):      return [self._pbB,    self._pbC]
    def _statuses(self): return [self._statusB, self._statusC]

    def _on_start(self, idx: int) -> None:
        if self._worker[idx] and self._worker[idx].isRunning():
            return

        config = self._collect_config()
        provider = config.get("llm_provider", "")
        api_key  = config.get("llm_api_key", "").strip()
        if not api_key and provider not in ("ollama",):
            QMessageBox.warning(self, "提示", "请先在「设置 → LLM 配置」中填写 API Key")
            return

        if idx == 0:
            # Stage B：file_paths 传领域名列表（字符串），不是文件路径
            domains = self._get_domains()
            if not domains:
                QMessageBox.warning(self, "提示", "请先在 B 步输入框填写领域名称（每行一个）")
                return
            file_paths = domains   # DocWorker 直接传给 run_stage_general_query_gen
            extra = None
        else:  # idx == 1, file_match
            file_paths = self._get_file_paths(self._listC)
            if not file_paths:
                QMessageBox.warning(self, "提示", "请先在 C 步添加文件")
                return
            if not self._general_queries:
                QMessageBox.warning(self, "提示", "请先运行 B 步 Query 生成")
                return
            extra = {"general_queries": self._general_queries}

        lbl = self._STAGE_LABELS[idx]
        self._shared_log.append(f"\n{lbl} 开始…")
        self._pbs()[idx].setMaximum(max(len(file_paths), 1))
        self._pbs()[idx].setValue(0)
        self._statuses()[idx].setText("处理中…")
        self._paused[idx] = False
        if idx == 0:
            self._query_table.setRowCount(0)
        if idx == 1:
            self._eval_table.setRowCount(0)
            self._lbl_row_count.setText("0 条")

        from workers.doc_worker import DocWorker
        w = DocWorker(self._STAGE_KEYS[idx], file_paths, config, extra_data=extra)
        w.log_signal.connect(lambda msg, l=lbl: self._shared_log.append(f"{l} {msg}"))
        w.progress_signal.connect(lambda c, t, i=idx: self._on_progress(i, c, t))
        if idx == 0:
            w.row_ready.connect(self._on_query_row_ready)
        if idx == 1:
            w.row_ready.connect(self._on_eval_row_ready)
        w.finished_signal.connect(lambda r, i=idx: self._on_finished(i, r))
        w.error_signal.connect(lambda msg, i=idx: self._on_error(i, msg))
        self._worker[idx] = w
        w.start()
        self._starts()[idx].setEnabled(False)
        self._pauses()[idx].setEnabled(True)
        self._stops()[idx].setEnabled(True)
        self._btn_one_click.setEnabled(False)

    def _on_pause_resume(self, idx: int) -> None:
        w = self._worker[idx]
        if not w:
            return
        lbl = self._STAGE_LABELS[idx]
        if not self._paused[idx]:
            w.request_pause()
            self._paused[idx] = True
            self._pauses()[idx].setText("▶ 继续")
            self._statuses()[idx].setText("已暂停")
            self._shared_log.append(f"{lbl} ── 已暂停 ──")
        else:
            w.request_resume()
            self._paused[idx] = False
            self._pauses()[idx].setText("⏸ 暂停")
            self._statuses()[idx].setText("处理中…")
            self._shared_log.append(f"{lbl} ── 已继续 ──")

    def _on_stop(self, idx: int) -> None:
        w = self._worker[idx]
        if w:
            w.request_stop()
        self._pauses()[idx].setEnabled(False)
        self._stops()[idx].setEnabled(False)
        self._statuses()[idx].setText("正在停止…")

    # ── Worker signal handlers ───────────────────────────────────────────

    def _on_progress(self, idx: int, current: int, total: int) -> None:
        self._pbs()[idx].setMaximum(max(total, 1))
        self._pbs()[idx].setValue(current)
        if not self._paused[idx]:
            self._statuses()[idx].setText(f"{current}/{total}")

    def _on_query_row_ready(self, row: dict) -> None:
        t = self._query_table
        r = t.rowCount()
        t.insertRow(r)
        t.setItem(r, 0, QTableWidgetItem(row.get("domain", "")))
        t.setItem(r, 1, QTableWidgetItem(row.get("type", "")))
        t.setItem(r, 2, QTableWidgetItem(row.get("query", "")))
        cand = row.get("candidate_files", [])
        t.setItem(r, 3, QTableWidgetItem("、".join(cand) if cand else "（盲）"))

    def _on_eval_row_ready(self, row: dict) -> None:
        t = self._eval_table
        r = t.rowCount()
        t.insertRow(r)
        for col, key in enumerate(
            ["query", "query_type", "query_domain", "filename", "llm_score", "candidate_files"]
        ):
            val = row.get(key, "")
            if isinstance(val, list):
                val = "、".join(str(v) for v in val)
            item = QTableWidgetItem(str(val))
            item.setToolTip(str(val))
            t.setItem(r, col, item)
        t.scrollToBottom()
        self._lbl_row_count.setText(f"{r + 1} 条")

    def _on_finished(self, idx: int, result: dict) -> None:
        processed = result.get("processed", 0)
        failed    = result.get("failed", 0)
        lbl = self._STAGE_LABELS[idx]
        self._shared_log.append(f"\n{lbl} 完成：处理 {processed}，失败 {failed}")
        self._statuses()[idx].setText(f"完成 ✓  {processed}/{processed + failed}")

        if idx == 0:
            queries = result.get("general_queries", [])
            self._general_queries = queries
            if queries:
                self._lbl_queries_ready.setText(
                    f"Query 数据已就绪：{len(queries)} 条"
                )
                self._lbl_queries_loaded.setText(
                    f"Query 数据已载入：{len(queries)} 条"
                )
            if result.get("queries_file"):
                self._shared_log.append(f"{lbl} queries → {result['queries_file']}")
            if self._one_click_mode:
                if queries:
                    self._on_start(1)
                else:
                    self._one_click_mode = False
                    self._btn_one_click.setEnabled(True)
                    QMessageBox.warning(self, "提示", "Query 生成未产出结果，已中止文件匹配步骤")

        elif idx == 1:
            if result.get("eval_path"):
                self._shared_log.append(f"{lbl} eval → {result['eval_path']}")
            if self._one_click_mode:
                self._one_click_mode = False

        self._paused[idx] = False
        self._starts()[idx].setEnabled(True)
        self._pauses()[idx].setEnabled(False)
        self._pauses()[idx].setText("⏸ 暂停")
        self._stops()[idx].setEnabled(False)
        if not any(w and w.isRunning() for w in self._worker):
            self._btn_one_click.setEnabled(True)

    def _on_error(self, idx: int, msg: str) -> None:
        lbl = self._STAGE_LABELS[idx]
        self._shared_log.append(f"{lbl} [FATAL] {msg}")
        QMessageBox.critical(self, "处理异常", msg)
        self._statuses()[idx].setText("出错")
        if self._one_click_mode:
            self._one_click_mode = False
        self._paused[idx] = False
        self._starts()[idx].setEnabled(True)
        self._pauses()[idx].setEnabled(False)
        self._pauses()[idx].setText("⏸ 暂停")
        self._stops()[idx].setEnabled(False)
        self._btn_one_click.setEnabled(True)

    def _on_save_prompts(self) -> None:
        """立即把当前 Prompt 文本框内容持久化到 config/doc_config.json。"""
        _save_gen_cfg({
            "general_query_gen_prompt": (
                self._prompt_editors[0].toPlainText().strip() or DEFAULT_GENERAL_QUERY_GEN_PROMPT
            ),
            "file_match_prompt": (
                self._prompt_editors[1].toPlainText().strip() or DEFAULT_FILE_MATCH_PROMPT
            ),
        })
        self._lbl_prompt_saved.setText("✓ 已保存")
        QTimer.singleShot(2500, lambda: self._lbl_prompt_saved.setText(""))

    # ── Collapsible box ──────────────────────────────────────────────────

    def _collapsible_box(self, title: str, inner: QWidget) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        btn = QPushButton(f"▼  {title}")
        btn.setObjectName("sectionHeader")
        btn.setCheckable(True)
        btn.setChecked(True)
        btn.setFixedHeight(24)

        def _toggle(checked: bool) -> None:
            inner.setVisible(checked)
            btn.setText(f"{'▼' if checked else '▶'}  {title}")

        btn.toggled.connect(_toggle)
        vlay.addWidget(btn)
        vlay.addWidget(inner)
        return w

    # ── Stylesheet ───────────────────────────────────────────────────────

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet("""
            QWidget { font-family: "Microsoft YaHei", sans-serif; }
            QPushButton#sectionHeader {
                background: #c8cde0; color: #2c3e6b;
                border: 1px solid #c0c4d8; border-radius: 4px;
                text-align: left; padding: 0 0 0 10px;
                font-weight: bold; font-size: 12px;
            }
            QPushButton#sectionHeader:hover { background: #d0d3e5; }
            QPushButton {
                background: #4472C4; color: white;
                border: none; border-radius: 4px;
                padding: 4px 12px; font-size: 12px;
            }
            QPushButton:hover    { background: #5585d5; }
            QPushButton:pressed  { background: #3360b0; }
            QPushButton:disabled { background: #aab0c8; color: #e0e0e0; }
            QPushButton#btnSecondary { background: #6c757d; }
            QPushButton#btnSecondary:hover { background: #7e8a94; }
            QPushButton#btnOneClick { background: #217a45; }
            QPushButton#btnOneClick:hover { background: #278f50; }
            QPushButton#btnOneClick:disabled { background: #aab0c8; color: #e0e0e0; }
            QLabel#labelSmall { font-size: 11px; color: #6c757d; }
            QTableWidget {
                border: 1px solid #d0d3e0; border-radius: 4px;
                gridline-color: #e8eaf0; selection-background-color: #cce0ff;
                alternate-background-color: #f8f9fd;
            }
            QHeaderView::section {
                background: #4472C4; color: white;
                font-weight: bold; padding: 4px; border: none;
            }
            QListWidget { border: 1px solid #d0d3e0; border-radius: 4px; background: #fafbff; }
            QProgressBar {
                border: 1px solid #d0d3e0; border-radius: 3px;
                background: #eef0f8; text-align: center; font-size: 10px;
            }
            QProgressBar::chunk { background: #4472C4; border-radius: 2px; }
            QTabWidget::pane {
                border: 1px solid #d0d3e0; border-radius: 4px; background: #ffffff;
            }
            QTabBar::tab {
                background: #e8eaf0; color: #3a3f5c;
                padding: 6px 16px;
                border-top-left-radius: 4px; border-top-right-radius: 4px;
                margin-right: 2px;
            }
            QTabBar::tab:selected { background: #4472C4; color: white; }
            QPlainTextEdit {
                border: 1px solid #d0d3e0; border-radius: 4px;
                background: #fafbff;
                font-family: Consolas, monospace; font-size: 12px;
            }
        """)
