"""
convert_panel.py — 格式转换面板。

功能：
- 指定输入目录（含 .md 文件）和输出目录
- 自定义启用格式及各格式占比（实时预估文件数）
- 试水模式（只转少量文件验证效果）
- 后台转换 + 进度条 + 日志
"""

import json
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QVBoxLayout, QSplitter, QScrollArea,
    QFormLayout, QPushButton, QLabel,
    QDoubleSpinBox, QSpinBox, QCheckBox,
    QLineEdit, QFileDialog,
    QWidget, QMessageBox, QProgressBar,
)

from app.widgets.log_panel import LogPanel

_DOC_CFG_PATH = Path(__file__).parent.parent.parent / "config" / "doc_config.json"

_DEFAULT_FORMATS = {
    'pdf':  {'enabled': True,  'pct': 79.4},
    'docx': {'enabled': True,  'pct': 17.5},
    'txt':  {'enabled': True,  'pct':  2.8},
    'html': {'enabled': True,  'pct':  0.3},
}

_FMT_LABELS = {
    'pdf':  'PDF',
    'docx': 'DOCX (Word)',
    'txt':  'TXT',
    'html': 'HTML',
}


def _load_cfg() -> dict:
    defaults = {
        'convert_input_dir':  '',
        'convert_output_dir': '',
        'convert_formats':    _DEFAULT_FORMATS,
        'convert_seed':       42,
        'convert_trial':      False,
        'convert_trial_n':    4,
    }
    try:
        if _DOC_CFG_PATH.exists():
            saved = json.loads(_DOC_CFG_PATH.read_text('utf-8'))
            for k in defaults:
                if k in saved:
                    defaults[k] = saved[k]
    except Exception:
        pass
    return defaults


def _save_cfg(values: dict) -> None:
    try:
        existing: dict = {}
        if _DOC_CFG_PATH.exists():
            try:
                existing = json.loads(_DOC_CFG_PATH.read_text('utf-8'))
            except Exception:
                pass
        existing.update(values)
        _DOC_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DOC_CFG_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), 'utf-8'
        )
    except Exception:
        pass


class ConvertPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg       = _load_cfg()
        self._worker    = None
        self._md_count  = 0        # 输入目录中 .md 文件数（实时扫）
        self._build_ui()
        self._apply_stylesheet()
        # 初始扫一次
        self._rescan_input_dir()

    # ── 构建 UI ─────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        title = QLabel("格式转换 — Markdown → PDF / DOCX / TXT / HTML")
        title.setObjectName("dialogTitle")
        root.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── 左侧设置区 ───────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        container = QWidget()
        left_lay = QVBoxLayout(container)
        left_lay.setContentsMargins(0, 0, 4, 0)
        left_lay.setSpacing(10)

        left_lay.addWidget(self._build_dir_section())
        left_lay.addWidget(self._build_format_section())
        left_lay.addWidget(self._build_options_section())
        left_lay.addWidget(self._build_control_section())
        left_lay.addStretch()

        scroll.setWidget(container)

        # ── 右侧日志区 ──────────────────────────────────────────────────
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(4, 0, 0, 0)
        right_lay.setSpacing(4)
        log_title = QLabel("运行日志")
        log_title.setObjectName("labelSmall")
        self._log = LogPanel()
        right_lay.addWidget(log_title)
        right_lay.addWidget(self._log, stretch=1)

        splitter.addWidget(scroll)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, stretch=1)

    # ── Section：目录 ────────────────────────────────────────────────────

    def _build_dir_section(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(6)

        self._input_dir  = QLineEdit(self._cfg.get('convert_input_dir', ''))
        self._output_dir = QLineEdit(self._cfg.get('convert_output_dir', ''))

        # 输入目录变化时重新扫描文件数
        self._input_dir.editingFinished.connect(self._rescan_input_dir)

        lay.addLayout(self._make_dir_row('输入目录（.md）：', self._input_dir,
                                         browse_cb=self._browse_input))
        lay.addLayout(self._make_dir_row('输出目录：       ', self._output_dir,
                                         browse_cb=self._browse_output,
                                         open_cb=self._open_output))

        self._lbl_md_count = QLabel('（尚未扫描）')
        self._lbl_md_count.setObjectName('labelSmall')
        lay.addWidget(self._lbl_md_count)

        return self._collapsible_box('① 目录', w)

    def _make_dir_row(self, label_text: str, line_edit: QLineEdit,
                      browse_cb=None, open_cb=None) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        lbl = QLabel(label_text)
        lbl.setFixedWidth(110)
        row.addWidget(lbl)
        row.addWidget(line_edit, stretch=1)
        if browse_cb:
            btn = QPushButton('浏览…')
            btn.setFixedWidth(54)
            btn.setFixedHeight(24)
            btn.setObjectName('btnSecondary')
            btn.clicked.connect(browse_cb)
            row.addWidget(btn)
        if open_cb:
            btn2 = QPushButton('📂')
            btn2.setFixedWidth(28)
            btn2.setFixedHeight(24)
            btn2.setObjectName('btnSecondary')
            btn2.clicked.connect(open_cb)
            row.addWidget(btn2)
        return row

    # ── Section：格式与占比 ───────────────────────────────────────────────

    def _build_format_section(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(4)

        saved_fmts = self._cfg.get('convert_formats', _DEFAULT_FORMATS)

        self._fmt_checks: dict[str, QCheckBox]      = {}
        self._fmt_spins:  dict[str, QDoubleSpinBox] = {}
        self._fmt_count_labels: dict[str, QLabel]   = {}

        for fmt in ['pdf', 'docx', 'txt', 'html']:
            fcfg = saved_fmts.get(fmt, _DEFAULT_FORMATS[fmt])
            row  = QHBoxLayout()
            row.setSpacing(6)

            chk = QCheckBox(_FMT_LABELS[fmt])
            chk.setChecked(fcfg.get('enabled', True))
            chk.setFixedWidth(90)

            spin = QDoubleSpinBox()
            spin.setRange(0.0, 100.0)
            spin.setDecimals(1)
            spin.setSuffix(' %')
            spin.setValue(fcfg.get('pct', _DEFAULT_FORMATS[fmt]['pct']))
            spin.setFixedWidth(80)
            spin.setEnabled(chk.isChecked())

            count_lbl = QLabel('~0 个')
            count_lbl.setObjectName('labelSmall')
            count_lbl.setFixedWidth(55)

            # 联动
            chk.toggled.connect(lambda checked, s=spin: s.setEnabled(checked))
            chk.toggled.connect(self._on_pct_changed)
            spin.valueChanged.connect(self._on_pct_changed)

            row.addWidget(chk)
            row.addWidget(spin)
            row.addWidget(count_lbl)
            row.addStretch()
            lay.addLayout(row)

            self._fmt_checks[fmt]       = chk
            self._fmt_spins[fmt]        = spin
            self._fmt_count_labels[fmt] = count_lbl

        # 合计行
        total_row = QHBoxLayout()
        total_row.setSpacing(6)
        self._lbl_total_pct = QLabel('总计：100.0%')
        self._lbl_total_pct.setObjectName('labelSmall')
        btn_balance = QPushButton('⚖ 自动均衡')
        btn_balance.setFixedHeight(24)
        btn_balance.setObjectName('btnSecondary')
        btn_balance.clicked.connect(self._auto_balance)
        total_row.addWidget(self._lbl_total_pct)
        total_row.addWidget(btn_balance)
        total_row.addStretch()
        lay.addLayout(total_row)

        self._update_count_labels()
        return self._collapsible_box('② 格式与占比', w)

    # ── Section：选项 ────────────────────────────────────────────────────

    def _build_options_section(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(6, 4, 6, 6)
        form.setVerticalSpacing(6)

        self._seed_spin = QSpinBox()
        self._seed_spin.setRange(0, 99999)
        self._seed_spin.setValue(self._cfg.get('convert_seed', 42))
        form.addRow('随机种子', self._seed_spin)

        trial_row = QHBoxLayout()
        self._chk_trial = QCheckBox('只转换前')
        self._chk_trial.setChecked(self._cfg.get('convert_trial', False))
        self._trial_n_spin = QSpinBox()
        self._trial_n_spin.setRange(1, 20)
        self._trial_n_spin.setValue(self._cfg.get('convert_trial_n', 4))
        self._trial_n_spin.setFixedWidth(60)
        self._trial_n_spin.setEnabled(self._chk_trial.isChecked())
        self._chk_trial.toggled.connect(self._trial_n_spin.setEnabled)
        trial_lbl = QLabel('个文件（试水）')
        trial_row.addWidget(self._chk_trial)
        trial_row.addWidget(self._trial_n_spin)
        trial_row.addWidget(trial_lbl)
        trial_row.addStretch()
        trial_widget = QWidget()
        trial_widget.setLayout(trial_row)
        form.addRow('试水模式', trial_widget)

        return self._collapsible_box('③ 选项', w)

    # ── Section：控制 ────────────────────────────────────────────────────

    def _build_control_section(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(6)

        btn_row = QHBoxLayout()
        self._btn_start = QPushButton('▶ 开始转换')
        self._btn_stop  = QPushButton('■ 停止')
        self._btn_stop.setEnabled(False)
        self._btn_stop.setObjectName('btnSecondary')
        for b in (self._btn_start, self._btn_stop):
            b.setFixedHeight(28)
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)
        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_stop)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        pb_row = QHBoxLayout()
        self._pb = QProgressBar()
        self._pb.setFixedHeight(12)
        self._pb.setTextVisible(True)
        self._pb.setFormat('%v / %m')
        self._pb.setValue(0)
        self._lbl_status = QLabel('就绪')
        self._lbl_status.setObjectName('labelSmall')
        pb_row.addWidget(self._pb, stretch=1)
        pb_row.addWidget(self._lbl_status)
        lay.addLayout(pb_row)

        return self._collapsible_box('④ 控制', w)

    # ── 折叠 Box（与 DocPanel._collapsible_box 同实现，待后续统一到基类）─────

    def _collapsible_box(self, title: str, inner: QWidget) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        btn = QPushButton(f'▼  {title}')
        btn.setObjectName('sectionHeader')
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

    # ── 目录操作 ─────────────────────────────────────────────────────────

    def _browse_input(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, '选择输入目录', self._input_dir.text() or '.')
        if path:
            self._input_dir.setText(path)
            self._rescan_input_dir()

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, '选择输出目录', self._output_dir.text() or '.')
        if path:
            self._output_dir.setText(path)

    def _open_output(self) -> None:
        path = self._output_dir.text().strip() or 'output'
        os.makedirs(path, exist_ok=True)
        subprocess.Popen(f'explorer "{os.path.abspath(path)}"')

    def _rescan_input_dir(self) -> None:
        path = self._input_dir.text().strip()
        if path and os.path.isdir(path):
            try:
                count = sum(
                    1 for f in os.listdir(path)
                    if f.endswith('.md') and not f.startswith('_')
                )
                self._md_count = count
                self._lbl_md_count.setText(f'共找到 {count} 个 .md 文件')
            except Exception:
                self._md_count = 0
                self._lbl_md_count.setText('（读取目录失败）')
        else:
            self._md_count = 0
            self._lbl_md_count.setText('（请先填写输入目录）')
        self._update_count_labels()

    # ── 占比联动 ─────────────────────────────────────────────────────────

    def _on_pct_changed(self) -> None:
        total = sum(
            self._fmt_spins[f].value()
            for f in ['pdf', 'docx', 'txt', 'html']
            if self._fmt_checks[f].isChecked()
        )
        color = '#c0392b' if abs(total - 100.0) > 0.05 else '#27ae60'
        self._lbl_total_pct.setText(f'总计：{total:.1f}%')
        self._lbl_total_pct.setStyleSheet(f'color: {color}; font-size: 11px;')
        self._update_count_labels()

    def _auto_balance(self) -> None:
        enabled = [f for f in ['pdf', 'docx', 'txt', 'html']
                   if self._fmt_checks[f].isChecked()]
        if not enabled:
            return
        each = round(100.0 / len(enabled), 1)
        remainder = round(100.0 - each * (len(enabled) - 1), 1)
        for i, fmt in enumerate(enabled):
            val = remainder if i == len(enabled) - 1 else each
            self._fmt_spins[fmt].blockSignals(True)
            self._fmt_spins[fmt].setValue(val)
            self._fmt_spins[fmt].blockSignals(False)
        self._on_pct_changed()

    def _update_count_labels(self) -> None:
        n = self._md_count
        for fmt in ['pdf', 'docx', 'txt', 'html']:
            if self._fmt_checks[fmt].isChecked():
                pct  = self._fmt_spins[fmt].value() / 100.0
                cnt  = round(n * pct)
                self._fmt_count_labels[fmt].setText(f'~{cnt} 个')
            else:
                self._fmt_count_labels[fmt].setText('—')

    # ── 配置收集 ─────────────────────────────────────────────────────────

    def _collect_config(self) -> dict:
        fmt_cfg = {}
        for fmt in ['pdf', 'docx', 'txt', 'html']:
            fmt_cfg[fmt] = {
                'enabled': self._fmt_checks[fmt].isChecked(),
                'pct':     self._fmt_spins[fmt].value(),
            }
        cfg = {
            'input_dir':  self._input_dir.text().strip(),
            'output_dir': self._output_dir.text().strip(),
            'formats':    fmt_cfg,
            'seed':       self._seed_spin.value(),
            'trial':      self._chk_trial.isChecked(),
            'trial_n':    self._trial_n_spin.value(),
        }
        # 持久化到 doc_config.json
        _save_cfg({
            'convert_input_dir':  cfg['input_dir'],
            'convert_output_dir': cfg['output_dir'],
            'convert_formats':    fmt_cfg,
            'convert_seed':       cfg['seed'],
            'convert_trial':      cfg['trial'],
            'convert_trial_n':    cfg['trial_n'],
        })
        return cfg

    # ── Worker 控制 ───────────────────────────────────────────────────────

    def _on_start(self) -> None:
        if self._worker and self._worker.isRunning():
            return

        # 校验
        input_dir = self._input_dir.text().strip()
        if not input_dir or not os.path.isdir(input_dir):
            QMessageBox.warning(self, '提示', '请先选择有效的输入目录')
            return
        output_dir = self._output_dir.text().strip()
        if not output_dir:
            QMessageBox.warning(self, '提示', '请先填写输出目录')
            return
        enabled_fmts = [f for f in ['pdf', 'docx', 'txt', 'html']
                        if self._fmt_checks[f].isChecked()]
        if not enabled_fmts:
            QMessageBox.warning(self, '提示', '请至少勾选一种输出格式')
            return
        total_pct = sum(self._fmt_spins[f].value() for f in enabled_fmts)
        if abs(total_pct - 100.0) > 0.5:
            QMessageBox.warning(
                self, '占比不合计',
                f'已勾选格式的百分比总和为 {total_pct:.1f}%，应为 100%。\n'
                '请手动调整，或点击「⚖ 自动均衡」。'
            )
            return

        config = self._collect_config()
        self._log.clear()
        self._log.append('开始转换…')
        self._pb.setValue(0)
        self._lbl_status.setText('转换中…')
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)

        from workers.convert_worker import ConvertWorker
        self._worker = ConvertWorker(config)
        self._worker.log_signal.connect(self._log.append)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.finished_signal.connect(self._on_finished)
        self._worker.error_signal.connect(self._on_error)
        self._worker.start()

    def _on_stop(self) -> None:
        if self._worker:
            self._worker.request_stop()
        self._btn_stop.setEnabled(False)
        self._lbl_status.setText('正在停止…')

    def _on_progress(self, current: int, total: int) -> None:
        self._pb.setMaximum(total)
        self._pb.setValue(current)
        self._lbl_status.setText(f'{current} / {total}')

    def _on_finished(self, result: dict) -> None:
        converted  = result.get('converted', 0)
        failed     = result.get('failed', 0)
        output_dir = result.get('output_dir', '')
        if failed:
            self._lbl_status.setText(f'完成  {converted} 个（失败 {failed} 个）')
        else:
            self._lbl_status.setText(f'完成 ✓  {converted} 个')
        self._log.append(f'输出目录：{output_dir}')
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)

    def _on_error(self, msg: str) -> None:
        self._log.append(f'[FATAL] {msg}')
        QMessageBox.critical(self, '转换异常', msg)
        self._lbl_status.setText('出错')
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)

    # ── 样式 ─────────────────────────────────────────────────────────────

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
            QProgressBar {
                border: 1px solid #d0d3e0; border-radius: 3px;
                background: #eef0f8; text-align: center; font-size: 10px;
            }
            QProgressBar::chunk { background: #4472C4; border-radius: 2px; }
        """)
