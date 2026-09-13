"""
doc_query_panel.py - 指定文档问答 Query 处理面板（Stage 3+4）

③ Query 生成（盲文档）  →  ④ Chunk 匹配 → Excel
"""

import json
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QVBoxLayout, QSplitter, QScrollArea,
    QFormLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QSpinBox,
    QLineEdit, QFileDialog, QTabWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
    QWidget, QMessageBox, QProgressBar, QPlainTextEdit,
)

from app.widgets.log_panel import LogPanel
from app.widgets.llm_panel import _load as _load_llm_cfg
from core.doc_processor import DEFAULT_QUERY_GEN_PROMPT, DEFAULT_RELEVANCE_PROMPT

_DOC_CFG_PATH = Path(__file__).parent.parent.parent / "config" / "doc_config.json"

_DEFAULT_CFG = {
    "chunk_size":          600,
    "chunk_overlap":       50,
    "chunk_batch_size":    20,
    "query_snippet_chars": 1500,
    "concurrency":         3,
    "stage3_out_dir":      "output/doc_results/queries",
    "stage4_out_dir":      "output/doc_results",
    "query_gen_prompt":    DEFAULT_QUERY_GEN_PROMPT,
    "relevance_prompt":    DEFAULT_RELEVANCE_PROMPT,
}

_PROMPT_KEYS = ("query_gen_prompt", "relevance_prompt")


def _hash_prompt(s: str) -> str:
    """MD5 前12位，用于记录「保存时 DEFAULT 是什么版本」。"""
    import hashlib
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def _load_doc_cfg() -> dict:
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
                    # _is_default=False 表示用户曾主动编辑，保留其内容；
                    # True / 缺失表示从未改过，自动同步到最新 DEFAULT。
                    if not saved.get(f"{pk}_is_default", True):
                        cfg[pk] = saved_val
                    # else: 保持 _DEFAULT_CFG 中的当前值（自动同步）
                else:
                    # ── 新格式：hash 比对 ────────────────────────────────
                    # saved_default_hash 记录的是「保存时 DEFAULT 的哈希」。
                    # 若 saved_val 的哈希 == 当时的 DEFAULT 哈希 →
                    #   用户未曾自定义，自动同步到当前最新 DEFAULT。
                    # 若不同 → 用户真实修改过，保留其版本。
                    if _hash_prompt(saved_val) != saved_default_hash:
                        cfg[pk] = saved_val
    except Exception:
        pass
    return cfg


def _save_doc_cfg(values: dict) -> None:
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
                # 记录「本次保存时 DEFAULT 的哈希」，用于下次加载时判断用户是否自定义
                existing[f"{k}_default_hash"] = _hash_prompt(_DEFAULT_CFG[k])
                existing.pop(f"{k}_is_default", None)   # 清理旧字段
        _DOC_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DOC_CFG_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), "utf-8"
        )
    except Exception:
        pass


class DocQueryPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = _load_doc_cfg()
        self._worker = [None, None]
        self._paused = [False, False]
        self._generated_queries: dict[str, list[str]] = {}
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
        lay.addWidget(self._build_section3())
        lay.addWidget(self._build_section4())
        lay.addStretch()
        scroll.setWidget(container)
        return scroll

    # ── Section 3: Query Generation ──────────────────────────────────────

    def _build_section3(self) -> QWidget:
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(6)

        btn_fill = QPushButton("← 读取第①步输出目录")
        btn_fill.setObjectName("btnSecondary")
        btn_fill.setFixedHeight(26)
        btn_fill.clicked.connect(
            lambda: self._autofill_from_stage1(self._list3, self._lbl_count3)
        )
        lay.addWidget(btn_fill)

        self._list3 = QListWidget()
        self._list3.setMaximumHeight(90)
        self._list3.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._lbl_count3 = QLabel("0 个文件")
        self._lbl_count3.setObjectName("labelSmall")
        self._list3.keyPressEvent = (
            lambda e: self._list_key_press(e, self._list3, self._lbl_count3)
        )
        lay.addLayout(self._make_file_btn_row(self._list3, self._lbl_count3, "txt_pdf"))
        lay.addWidget(self._list3)
        lay.addWidget(self._lbl_count3)

        self._outdir3 = QLineEdit(self._cfg.get("stage3_out_dir", "output/doc_results/queries"))
        lay.addLayout(self._make_outdir_row(self._outdir3))

        snippet_row = QHBoxLayout()
        snippet_row.setContentsMargins(0, 0, 0, 0)
        snippet_row.setSpacing(4)
        snippet_lbl = QLabel("片段字数：")
        snippet_lbl.setFixedWidth(60)
        self._snippet_chars = QSpinBox()
        self._snippet_chars.setRange(300, 10000)
        self._snippet_chars.setSingleStep(500)
        self._snippet_chars.setValue(int(self._cfg.get("query_snippet_chars", 1500)))
        self._snippet_chars.setSuffix(" 字")
        snippet_row.addWidget(snippet_lbl)
        snippet_row.addWidget(self._snippet_chars)
        snippet_row.addStretch()
        lay.addLayout(snippet_row)

        self._btn_start3, self._btn_pause3, self._btn_stop3 = self._make_ctrl_btns("▶ 开始生成", 0)
        lay.addLayout(self._make_ctrl_row(self._btn_start3, self._btn_pause3, self._btn_stop3))

        self._pb3, self._status3 = self._make_progress_bar()
        lay.addLayout(self._make_progress_row(self._pb3, self._status3))

        lay.addWidget(QLabel("生成结果（可双击编辑）："))
        self._query_table = QTableWidget()
        self._query_table.setColumnCount(4)
        self._query_table.setHorizontalHeaderLabels(["文件名", "Query", "Query类型", "Query领域"])
        hdr_q = self._query_table.horizontalHeader()
        hdr_q.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr_q.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr_q.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr_q.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._query_table.setAlternatingRowColors(True)
        self._query_table.verticalHeader().setDefaultSectionSize(24)
        self._query_table.setMinimumHeight(100)
        self._query_table.setMaximumHeight(180)
        lay.addWidget(self._query_table)

        btn_row = QHBoxLayout()
        self._lbl_queries_ready = QLabel("Queries：待生成")
        self._lbl_queries_ready.setObjectName("labelSmall")
        self._btn_one_click = QPushButton("⚡ 一键运行 ③+④")
        self._btn_one_click.setObjectName("btnOneClick")
        self._btn_one_click.setFixedHeight(26)
        self._btn_one_click.clicked.connect(self._on_one_click)
        btn_row.addWidget(self._lbl_queries_ready)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_one_click)
        lay.addLayout(btn_row)

        return self._collapsible_box("③ Query 生成", inner)

    # ── Section 4: Chunk Matching ─────────────────────────────────────────

    def _build_section4(self) -> QWidget:
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(6)

        btn_fill = QPushButton("← 与第③步使用相同文件")
        btn_fill.setObjectName("btnSecondary")
        btn_fill.setFixedHeight(26)
        btn_fill.clicked.connect(
            lambda: self._autofill_from_stage3(self._list4, self._lbl_count4)
        )
        lay.addWidget(btn_fill)

        self._list4 = QListWidget()
        self._list4.setMaximumHeight(90)
        self._list4.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._lbl_count4 = QLabel("0 个文件")
        self._lbl_count4.setObjectName("labelSmall")
        self._list4.keyPressEvent = (
            lambda e: self._list_key_press(e, self._list4, self._lbl_count4)
        )
        lay.addLayout(self._make_file_btn_row(self._list4, self._lbl_count4, "txt_pdf"))
        lay.addWidget(self._list4)
        lay.addWidget(self._lbl_count4)

        self._lbl_queries_loaded = QLabel("Queries：待生成（请先运行第③步）")
        self._lbl_queries_loaded.setObjectName("labelSmall")
        self._lbl_queries_loaded.setWordWrap(True)
        lay.addWidget(self._lbl_queries_loaded)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(4)

        self._chunk_size = QSpinBox()
        self._chunk_size.setRange(100, 3000)
        self._chunk_size.setSingleStep(100)
        self._chunk_size.setValue(int(self._cfg.get("chunk_size", 600)))
        self._chunk_size.setSuffix(" 字")
        form.addRow("分段大小", self._chunk_size)

        self._chunk_overlap = QSpinBox()
        self._chunk_overlap.setRange(0, 200)
        self._chunk_overlap.setSingleStep(10)
        self._chunk_overlap.setValue(int(self._cfg.get("chunk_overlap", 50)))
        self._chunk_overlap.setSuffix(" 字")
        form.addRow("段间重叠", self._chunk_overlap)

        self._chunk_batch_size = QSpinBox()
        self._chunk_batch_size.setRange(5, 100)
        self._chunk_batch_size.setSingleStep(5)
        self._chunk_batch_size.setValue(int(self._cfg.get("chunk_batch_size", 20)))
        self._chunk_batch_size.setSuffix(" 块/批")
        form.addRow("检索批大小", self._chunk_batch_size)

        self._concurrency = QSpinBox()
        self._concurrency.setRange(1, 20)
        self._concurrency.setSingleStep(1)
        self._concurrency.setValue(int(self._cfg.get("concurrency", 3)))
        self._concurrency.setSuffix(" 个")
        self._concurrency.setToolTip("同时发送的 LLM 请求数；越大速度越快，但会更快消耗 API 限额")
        form.addRow("并发请求数", self._concurrency)

        lay.addLayout(form)

        self._outdir4 = QLineEdit(self._cfg.get("stage4_out_dir", "output/doc_results"))
        lay.addLayout(self._make_outdir_row(self._outdir4))

        self._btn_start4, self._btn_pause4, self._btn_stop4 = self._make_ctrl_btns("▶ 开始匹配", 1)
        lay.addLayout(self._make_ctrl_row(self._btn_start4, self._btn_pause4, self._btn_stop4))

        self._pb4, self._status4 = self._make_progress_bar()
        lay.addLayout(self._make_progress_row(self._pb4, self._status4))

        return self._collapsible_box("④ Chunk 匹配 → Excel", inner)

    # ── Right panel ──────────────────────────────────────────────────────

    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("评测数据预览"))
        hdr.addStretch()
        self._lbl_row_count = QLabel("0 条")
        self._lbl_row_count.setObjectName("labelSmall")
        hdr.addWidget(self._lbl_row_count)
        lay.addLayout(hdr)

        tabs = QTabWidget()
        tabs.addTab(self._build_eval_table(), "评测数据预览")
        tabs.addTab(self._build_prompt_tab(), "Prompt 编辑")
        tabs.addTab(self._build_log_tab(),    "运行日志")
        lay.addWidget(tabs, stretch=1)
        return w

    def _build_eval_table(self) -> QWidget:
        self._eval_table = QTableWidget()
        self._eval_table.setColumnCount(9)
        self._eval_table.setHorizontalHeaderLabels(
            ["文件ID", "文件名", "Query", "Query类型", "Query领域",
             "chunk_id", "页码", "段落文本", "LLM评分"]
        )
        hdr = self._eval_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)
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
            "Query 生成 Prompt 可用占位符：<b>{filename}</b>（文件名）、"
            "<b>{snippet}</b>（文档前 N 字有效内容）、<b>{snippet_chars}</b>（字数 N，由左侧「片段字数」控制）；"
            "相关性判断 Prompt 用 <b>{query}</b> 和 <b>{chunks}</b>。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("labelSmall")
        lay.addWidget(hint)

        lay.addWidget(QLabel("③ Query 生成 Prompt："))
        self._prompt_query_gen = QPlainTextEdit()
        self._prompt_query_gen.setPlainText(
            self._cfg.get("query_gen_prompt", DEFAULT_QUERY_GEN_PROMPT)
        )
        self._prompt_query_gen.setMinimumHeight(160)
        lay.addWidget(self._prompt_query_gen)

        btn_reset_gen = QPushButton("恢复默认")
        btn_reset_gen.setObjectName("btnSecondary")
        btn_reset_gen.clicked.connect(
            lambda: self._prompt_query_gen.setPlainText(DEFAULT_QUERY_GEN_PROMPT)
        )
        row1 = QHBoxLayout()
        row1.addStretch()
        row1.addWidget(btn_reset_gen)
        lay.addLayout(row1)

        lay.addWidget(QLabel("④ 相关性判断 Prompt："))
        self._prompt_relevance = QPlainTextEdit()
        self._prompt_relevance.setPlainText(
            self._cfg.get("relevance_prompt", DEFAULT_RELEVANCE_PROMPT)
        )
        self._prompt_relevance.setMinimumHeight(160)
        lay.addWidget(self._prompt_relevance)

        btn_reset_rel = QPushButton("恢复默认")
        btn_reset_rel.setObjectName("btnSecondary")
        btn_reset_rel.clicked.connect(
            lambda: self._prompt_relevance.setPlainText(DEFAULT_RELEVANCE_PROMPT)
        )
        row2 = QHBoxLayout()
        row2.addStretch()
        row2.addWidget(btn_reset_rel)
        lay.addLayout(row2)

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

    def _make_file_btn_row(self, list_w, count_lbl, mode: str = "txt_pdf") -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        b_files = QPushButton("＋ 文件")
        b_files.setFixedHeight(26)
        b_files.clicked.connect(lambda: self._add_files(list_w, count_lbl, mode))
        b_folder = QPushButton("＋ 文件夹")
        b_folder.setFixedHeight(26)
        b_folder.clicked.connect(lambda: self._add_folder(list_w, count_lbl, mode))
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

    # ── File list operations ─────────────────────────────────────────────

    def _add_files(self, list_w, count_lbl, mode: str) -> None:
        flt = (
            "文本/PDF (*.txt *.md *.markdown *.pdf);;"
            "文本 (*.txt *.md *.markdown);;PDF (*.pdf);;所有文件 (*)"
        )
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", flt)
        self._add_paths_to_list(paths, list_w, count_lbl)

    def _add_folder(self, list_w, count_lbl, mode: str) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not folder:
            return
        paths = []
        for ext in ("*.txt", "*.md", "*.markdown", "*.pdf"):
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

    def _autofill_from_stage1(self, list_w, count_lbl) -> None:
        try:
            saved = json.loads(_DOC_CFG_PATH.read_text("utf-8")) if _DOC_CFG_PATH.exists() else {}
            out_dir = saved.get("stage1_out_dir", "").strip()
        except Exception:
            out_dir = ""
        if not out_dir or not Path(out_dir).exists():
            QMessageBox.information(self, "提示", "未找到第①步输出目录，请手动添加文件")
            return
        paths = []
        for ext in ("*.txt", "*.md", "*.markdown", "*.pdf"):
            paths.extend(sorted(str(p) for p in Path(out_dir).glob(ext)))
        if not paths:
            QMessageBox.information(self, "提示", f"在 {out_dir} 中未找到文件")
            return
        self._clear_list(list_w, count_lbl)
        self._add_paths_to_list(paths, list_w, count_lbl)

    def _autofill_from_stage3(self, list_w, count_lbl) -> None:
        paths = self._get_file_paths(self._list3)
        if not paths:
            QMessageBox.information(self, "提示", "第③步文件列表为空，请先在第③步添加文件")
            return
        self._clear_list(list_w, count_lbl)
        self._add_paths_to_list(paths, list_w, count_lbl)

    def _get_file_paths(self, list_w) -> list:
        return [list_w.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(list_w.count())]

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

    # ── One-click ③+④ ─────────────────────────────────────────────────────

    def _on_one_click(self) -> None:
        if (self._worker[0] and self._worker[0].isRunning()) or \
           (self._worker[1] and self._worker[1].isRunning()):
            return
        if not self._get_file_paths(self._list3):
            QMessageBox.warning(self, "提示", "请先在第③步添加要处理的文件")
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
            "chunk_size":          self._chunk_size.value(),
            "chunk_overlap":       self._chunk_overlap.value(),
            "chunk_batch_size":    self._chunk_batch_size.value(),
            "query_snippet_chars": self._snippet_chars.value(),
            "concurrency":         self._concurrency.value(),
            "stage3_out_dir":      self._outdir3.text().strip() or "output/doc_results/queries",
            "stage4_out_dir":      self._outdir4.text().strip() or "output/doc_results",
            "query_gen_prompt": (
                self._prompt_query_gen.toPlainText().strip() or DEFAULT_QUERY_GEN_PROMPT
            ),
            "relevance_prompt": (
                self._prompt_relevance.toPlainText().strip() or DEFAULT_RELEVANCE_PROMPT
            ),
        }
        _save_doc_cfg(cfg)
        cfg.update(_load_llm_cfg())
        return cfg

    # ── Stage start / pause / stop ───────────────────────────────────────

    _STAGE_KEYS   = ["gen_queries", "match_chunks"]
    _STAGE_LABELS = ["[③ Query生成]", "[④ Chunk匹配]"]

    def _starts(self):   return [self._btn_start3, self._btn_start4]
    def _pauses(self):   return [self._btn_pause3, self._btn_pause4]
    def _stops(self):    return [self._btn_stop3,  self._btn_stop4]
    def _pbs(self):      return [self._pb3,     self._pb4]
    def _statuses(self): return [self._status3, self._status4]
    def _lists(self):    return [self._list3,   self._list4]

    def _on_start(self, idx: int) -> None:
        if self._worker[idx] and self._worker[idx].isRunning():
            return
        file_paths = self._get_file_paths(self._lists()[idx])
        if not file_paths:
            QMessageBox.warning(self, "提示", "请先添加要处理的文件")
            return
        config = self._collect_config()
        provider = config.get("llm_provider", "")
        api_key  = config.get("llm_api_key", "").strip()
        if not api_key and provider not in ("ollama",):
            QMessageBox.warning(self, "提示", "请先在「设置 → LLM 配置」中填写 API Key")
            return
        if idx == 1 and not self._generated_queries:
            QMessageBox.warning(self, "提示", "请先运行第③步生成 queries")
            return

        lbl = self._STAGE_LABELS[idx]
        self._shared_log.append(f"\n{lbl} 开始…")
        self._pbs()[idx].setMaximum(len(file_paths))
        self._pbs()[idx].setValue(0)
        self._statuses()[idx].setText("处理中…")
        if idx == 0:
            self._query_table.setRowCount(0)
        if idx == 1:
            self._eval_table.setRowCount(0)
            self._lbl_row_count.setText("0 条")
        self._paused[idx] = False

        extra = {"queries": self._generated_queries} if idx == 1 else None
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
        self._pbs()[idx].setValue(current)
        if not self._paused[idx]:
            self._statuses()[idx].setText(f"{current}/{total}")

    def _on_query_row_ready(self, row: dict) -> None:
        t = self._query_table
        r = t.rowCount()
        t.insertRow(r)
        t.setItem(r, 0, QTableWidgetItem(row.get("filename", "")))
        t.setItem(r, 1, QTableWidgetItem(row.get("query", "")))
        t.setItem(r, 2, QTableWidgetItem(row.get("type", "")))
        t.setItem(r, 3, QTableWidgetItem(row.get("domain", "")))

    def _on_eval_row_ready(self, row: dict) -> None:
        t = self._eval_table
        r = t.rowCount()
        t.insertRow(r)
        for col, key in enumerate(
            ["file_id", "filename", "query", "query_type", "query_domain",
             "chunk_id", "page", "passage", "llm_score"]
        ):
            item = QTableWidgetItem(str(row.get(key, "")))
            item.setToolTip(str(row.get(key, "")))
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
            self._generated_queries = result.get("queries", {})
            total = sum(len(v) for v in self._generated_queries.values())
            if self._generated_queries:
                self._lbl_queries_ready.setText(
                    f"Queries 已就绪：{len(self._generated_queries)} 个文件，共 {total} 条"
                )
                self._lbl_queries_loaded.setText(
                    f"Queries 已载入：{len(self._generated_queries)} 个文件，共 {total} 条"
                )
            if result.get("queries_file"):
                self._shared_log.append(f"{lbl} queries → {result['queries_file']}")
            if self._one_click_mode:
                if self._generated_queries:
                    # 直接从 list3 同步文件列表到 list4，用内存 queries 启动 Stage 4
                    files = self._get_file_paths(self._list3)
                    self._clear_list(self._list4, self._lbl_count4)
                    self._add_paths_to_list(files, self._list4, self._lbl_count4)
                    self._on_start(1)
                else:
                    self._one_click_mode = False
                    self._btn_one_click.setEnabled(True)
                    QMessageBox.warning(self, "提示", "Query 生成未产出结果，已中止第④步")

        if idx == 1:
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
        _save_doc_cfg({
            "query_gen_prompt": (
                self._prompt_query_gen.toPlainText().strip() or DEFAULT_QUERY_GEN_PROMPT
            ),
            "relevance_prompt": (
                self._prompt_relevance.toPlainText().strip() or DEFAULT_RELEVANCE_PROMPT
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
