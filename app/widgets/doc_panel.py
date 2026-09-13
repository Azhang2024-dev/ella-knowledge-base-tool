"""
doc_panel.py - 文档处理面板（Stage 1: OCR/提取）
"""

import json
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QVBoxLayout, QScrollArea, QSplitter,
    QFormLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QComboBox,
    QLineEdit, QFileDialog,
    QWidget,
    QMessageBox, QProgressBar,
)

from app.widgets.log_panel import LogPanel
from app.widgets.llm_panel import _load as _load_llm_cfg

_DOC_CFG_PATH = Path(__file__).parent.parent.parent / "config" / "doc_config.json"

_DEFAULT_CFG = {
    "ocr_backend":      "auto",
    "stage1_out_dir":   "output/doc_results/extracted",
    "extract_formats":  ["txt"],
    "mineru_formats":   ["markdown", "json"],
}


def _load_doc_cfg() -> dict:
    cfg = dict(_DEFAULT_CFG)
    try:
        if _DOC_CFG_PATH.exists():
            saved = json.loads(_DOC_CFG_PATH.read_text("utf-8"))
            for k in _DEFAULT_CFG:
                if k in saved:
                    cfg[k] = saved[k]
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
        existing.update(values)
        _DOC_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DOC_CFG_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), "utf-8"
        )
    except Exception:
        pass


class DocPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = _load_doc_cfg()
        self._worker = [None]
        self._paused = [False]
        self._build_ui()
        self._apply_stylesheet()

    # ── Top-level ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        title = QLabel("文档处理 — OCR / 提取文本")
        title.setObjectName("dialogTitle")
        root.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧：可折叠 Section 滚动区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(8)
        lay.addWidget(self._build_section1())
        lay.addStretch()
        scroll.setWidget(container)

        # 右侧：共享日志面板
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(4, 0, 0, 0)
        right_lay.setSpacing(4)
        log_title = QLabel("运行日志")
        log_title.setObjectName("labelSmall")
        self._shared_log = LogPanel()
        right_lay.addWidget(log_title)
        right_lay.addWidget(self._shared_log, stretch=1)

        splitter.addWidget(scroll)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, stretch=1)

    # ── Section 1: OCR/Extract ───────────────────────────────────────────

    def _build_section1(self) -> QWidget:
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(6)

        self._list1 = QListWidget()
        self._list1.setMaximumHeight(100)
        self._list1.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._lbl_count1 = QLabel("0 个文件")
        self._lbl_count1.setObjectName("labelSmall")
        self._list1.keyPressEvent = (
            lambda e: self._list_key_press(e, self._list1, self._lbl_count1)
        )
        lay.addLayout(self._make_file_btn_row(self._list1, self._lbl_count1))
        lay.addWidget(self._list1)
        lay.addWidget(self._lbl_count1)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(4)
        self._ocr_combo = QComboBox()
        self._ocr_combo.addItem("自动（优先本地）", "auto")
        self._ocr_combo.addItem("本地 paddleocr",   "paddleocr")
        self._ocr_combo.addItem("LLM 视觉（云端）", "llm_vision")
        self._ocr_combo.addItem("强制 MinerU（高质量）", "mineru_fallback")
        saved_ocr = self._cfg.get("ocr_backend", "auto")
        for i in range(self._ocr_combo.count()):
            if self._ocr_combo.itemData(i) == saved_ocr:
                self._ocr_combo.setCurrentIndex(i)
                break
        form.addRow("OCR 方案", self._ocr_combo)

        # ── 普通 OCR 输出格式 ──────────────────────────────────────────────
        saved_efmts = set(self._cfg.get("extract_formats", ["txt"]))
        self._chk_extract_fmt: dict[str, "QCheckBox"] = {}
        efmt_row = QHBoxLayout()
        efmt_row.setSpacing(8)
        for key, label in [("txt", "TXT"), ("md", "Markdown"), ("json", "JSON")]:
            from PySide6.QtWidgets import QCheckBox
            cb = QCheckBox(label)
            cb.setChecked(key in saved_efmts)
            efmt_row.addWidget(cb)
            self._chk_extract_fmt[key] = cb
        efmt_row.addStretch()
        self._wgt_extract_fmt = QWidget()
        self._wgt_extract_fmt.setLayout(efmt_row)
        self._lbl_extract_fmt = QLabel("输出格式")
        form.addRow(self._lbl_extract_fmt, self._wgt_extract_fmt)

        # ── MinerU 输出格式（仅 MinerU 模式可见）─────────────────────────
        saved_mfmts = set(self._cfg.get("mineru_formats", ["markdown", "json"]))
        self._chk_mineru_fmt: dict[str, "QCheckBox"] = {}
        mfmt_row = QHBoxLayout()
        mfmt_row.setSpacing(8)
        for key, label in [("markdown", "Markdown"), ("json", "JSON"),
                            ("docx", "Word"), ("html", "HTML"), ("latex", "LaTeX")]:
            from PySide6.QtWidgets import QCheckBox
            cb = QCheckBox(label)
            cb.setChecked(key in saved_mfmts)
            mfmt_row.addWidget(cb)
            self._chk_mineru_fmt[key] = cb
        mfmt_row.addStretch()
        self._wgt_mineru_fmt = QWidget()
        self._wgt_mineru_fmt.setLayout(mfmt_row)
        self._lbl_mineru_fmt = QLabel("MinerU 格式")
        form.addRow(self._lbl_mineru_fmt, self._wgt_mineru_fmt)

        # OCR 方案切换时更新格式行可见性
        def _update_fmt_vis():
            is_mineru = self._ocr_combo.currentData() == "mineru_fallback"
            self._lbl_extract_fmt.setVisible(not is_mineru)
            self._wgt_extract_fmt.setVisible(not is_mineru)
            self._lbl_mineru_fmt.setVisible(is_mineru)
            self._wgt_mineru_fmt.setVisible(is_mineru)
        self._ocr_combo.currentIndexChanged.connect(lambda _: _update_fmt_vis())
        _update_fmt_vis()  # 初始化可见性

        lay.addLayout(form)

        self._outdir1 = QLineEdit(self._cfg.get("stage1_out_dir", "output/doc_results/extracted"))
        lay.addLayout(self._make_outdir_row(self._outdir1))

        self._btn_start1, self._btn_pause1, self._btn_stop1 = self._make_ctrl_btns("▶ 开始提取", 0)
        lay.addLayout(self._make_ctrl_row(self._btn_start1, self._btn_pause1, self._btn_stop1))

        self._pb1, self._status1 = self._make_progress_bar()
        lay.addLayout(self._make_progress_row(self._pb1, self._status1))

        return self._collapsible_box("① OCR / 提取文本", inner)

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

    # ── File list operations ─────────────────────────────────────────────

    def _file_filter(self) -> str:
        return (
            "支持的文档 (*.pdf *.docx *.doc *.txt *.md *.markdown *.json *.xlsx *.xls);;"
            "PDF (*.pdf);;Word (*.docx *.doc);;文本 (*.txt *.md);;所有文件 (*)"
        )

    def _folder_exts(self) -> tuple:
        return ("*.pdf", "*.docx", "*.doc", "*.txt", "*.md",
                "*.markdown", "*.json", "*.xlsx", "*.xls")

    def _add_files(self, list_w, count_lbl) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", self._file_filter())
        self._add_paths_to_list(paths, list_w, count_lbl)

    def _add_folder(self, list_w, count_lbl) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not folder:
            return
        paths = []
        for ext in self._folder_exts():
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

    # ── Config ───────────────────────────────────────────────────────────

    def _collect_config(self) -> dict:
        cfg = {
            "ocr_backend":    self._ocr_combo.currentData(),
            "stage1_out_dir": self._outdir1.text().strip() or "output/doc_results/extracted",
            "extract_formats": [k for k, cb in self._chk_extract_fmt.items() if cb.isChecked()]
                                or ["txt"],
            "mineru_formats":  [k for k, cb in self._chk_mineru_fmt.items() if cb.isChecked()]
                                or ["markdown"],
        }
        _save_doc_cfg(cfg)
        cfg.update(_load_llm_cfg())
        return cfg

    # ── Stage start / pause / stop ───────────────────────────────────────

    _STAGE_KEYS = ["extract"]

    def _starts(self):   return [self._btn_start1]
    def _pauses(self):   return [self._btn_pause1]
    def _stops(self):    return [self._btn_stop1]
    def _pbs(self):      return [self._pb1]
    def _statuses(self): return [self._status1]
    def _lists(self):    return [self._list1]

    _STAGE_LABELS = ["[① OCR]"]

    def _on_start(self, idx: int) -> None:
        if self._worker[idx] and self._worker[idx].isRunning():
            return
        file_paths = self._get_file_paths(self._lists()[idx])
        if not file_paths:
            QMessageBox.warning(self, "提示", "请先添加要处理的文件")
            return
        config = self._collect_config()

        if idx == 0 and config.get("ocr_backend") == "mineru_fallback":
            if not config.get("mineru_token"):
                QMessageBox.warning(
                    self, "提示",
                    "MinerU 方案需要 Token，请在「设置 → LLM / 智能模式配置」中填写 MinerU Token",
                )
                return

        lbl = self._STAGE_LABELS[idx]
        self._shared_log.append(f"\n{lbl} 开始…")
        self._pbs()[idx].setMaximum(len(file_paths))
        self._pbs()[idx].setValue(0)
        self._statuses()[idx].setText("处理中…")
        self._paused[idx] = False

        from workers.doc_worker import DocWorker
        w = DocWorker(self._STAGE_KEYS[idx], file_paths, config)
        w.log_signal.connect(lambda msg, l=lbl: self._shared_log.append(f"{l} {msg}"))
        w.progress_signal.connect(lambda c, t, i=idx: self._on_progress(i, c, t))
        w.finished_signal.connect(lambda r, i=idx: self._on_finished(i, r))
        w.error_signal.connect(lambda msg, i=idx: self._on_error(i, msg))
        self._worker[idx] = w
        w.start()
        self._starts()[idx].setEnabled(False)
        self._pauses()[idx].setEnabled(True)
        self._stops()[idx].setEnabled(True)

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

    def _on_progress(self, idx: int, current: int, total: int) -> None:
        self._pbs()[idx].setValue(current)
        if not self._paused[idx]:
            self._statuses()[idx].setText(f"{current}/{total}")

    def _on_finished(self, idx: int, result: dict) -> None:
        processed = result.get("processed", 0)
        failed    = result.get("failed", 0)
        lbl = self._STAGE_LABELS[idx]
        self._shared_log.append(f"\n{lbl} 完成：处理 {processed}，失败 {failed}")
        self._statuses()[idx].setText(f"完成 ✓  {processed}/{processed + failed}")
        if idx == 0 and "output_dir" in result:
            pass  # output_dir 备用，当前无下游消费
        self._paused[idx] = False
        self._starts()[idx].setEnabled(True)
        self._pauses()[idx].setEnabled(False)
        self._pauses()[idx].setText("⏸ 暂停")
        self._stops()[idx].setEnabled(False)

    def _on_error(self, idx: int, msg: str) -> None:
        lbl = self._STAGE_LABELS[idx]
        self._shared_log.append(f"{lbl} [FATAL] {msg}")
        QMessageBox.critical(self, "处理异常", msg)
        self._statuses()[idx].setText("出错")
        self._paused[idx] = False
        self._starts()[idx].setEnabled(True)
        self._pauses()[idx].setEnabled(False)
        self._pauses()[idx].setText("⏸ 暂停")
        self._stops()[idx].setEnabled(False)

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
            QLabel#dialogTitle { font-size: 15px; font-weight: bold; color: #2c3e6b; }
            QLabel#labelSmall  { font-size: 11px; color: #6c757d; }
            QListWidget { border: 1px solid #d0d3e0; border-radius: 4px; background: #fafbff; }
            QProgressBar {
                border: 1px solid #d0d3e0; border-radius: 3px;
                background: #eef0f8; text-align: center; font-size: 10px;
            }
            QProgressBar::chunk { background: #4472C4; border-radius: 2px; }
        """)
