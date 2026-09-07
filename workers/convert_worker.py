"""
convert_worker.py — 格式转换后台线程。
"""

import os
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal


class ConvertWorker(QThread):
    log_signal      = Signal(str)
    progress_signal = Signal(int, int)   # current, total
    finished_signal = Signal(dict)       # {converted, failed, output_dir}
    error_signal    = Signal(str)

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config     = config
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        cfg = self._config
        input_dir  = os.path.normpath(cfg['input_dir'])
        output_dir = os.path.normpath(cfg['output_dir'])
        fmt_cfg    = cfg['formats']         # {fmt: {enabled, pct}}
        seed       = cfg.get('seed', 42)
        trial      = cfg.get('trial', False)
        trial_n    = cfg.get('trial_n', 4)

        try:
            from core.md_converter import convert_file
            import random

            # 扫描输入目录
            md_files = sorted([
                f for f in os.listdir(input_dir)
                if f.endswith('.md') and not f.startswith('_')
            ])
            md_files = [os.path.join(input_dir, f) for f in md_files]
            if not md_files:
                self.error_signal.emit(f'输入目录中未找到 .md 文件：{input_dir}')
                return

            random.seed(seed)
            random.shuffle(md_files)

            os.makedirs(output_dir, exist_ok=True)

            # 构建任务列表：(文件路径, 格式)
            tasks = []

            if trial:
                fmt_order = [f for f in ['pdf', 'docx', 'txt', 'html']
                             if fmt_cfg.get(f, {}).get('enabled', False)]
                if not fmt_order:
                    fmt_order = ['pdf']
                # 转前 trial_n 个文件，格式按 fmt_order 循环分配
                for j in range(trial_n):
                    if j >= len(md_files):
                        break
                    fmt = fmt_order[j % len(fmt_order)]
                    tasks.append((md_files[j], fmt))
            else:
                n = len(md_files)
                enabled_fmts = [f for f in ['pdf', 'docx', 'txt', 'html']
                                if fmt_cfg.get(f, {}).get('enabled', False)]
                counts = {}
                cumulative = 0
                for idx, fmt in enumerate(enabled_fmts):
                    if idx == len(enabled_fmts) - 1:
                        counts[fmt] = n - cumulative
                    else:
                        pct = fmt_cfg[fmt].get('pct', 0) / 100.0
                        counts[fmt] = round(n * pct)
                        cumulative += counts[fmt]

                file_idx = 0
                for fmt in enabled_fmts:
                    cnt = counts.get(fmt, 0)
                    for _ in range(cnt):
                        if file_idx >= len(md_files):
                            break
                        tasks.append((md_files[file_idx], fmt))
                        file_idx += 1

            total     = len(tasks)
            converted = 0
            failed    = 0

            self.log_signal.emit(f'共 {total} 个文件待转换，输出目录：{output_dir}')
            self.progress_signal.emit(0, total)

            # 若有 PDF 任务，提前启动 Word 并复用到所有 PDF 文件
            from core.md_converter import (
                convert_file, open_word_app, close_word_app,
            )
            has_pdf       = any(fmt == 'pdf' for _, fmt in tasks)
            word_app      = None
            pythoncom_mod = None
            if has_pdf:
                try:
                    word_app, pythoncom_mod = open_word_app()
                    self.log_signal.emit('Word 已启动（PDF 批量模式）')
                except Exception as e:
                    self.log_signal.emit(f'  [WARN] Word 启动失败，将逐文件启停：{e}')
                    word_app = None

            def _restart_word():
                """关闭旧实例，重启 Word；失败则返回 (None, None)。"""
                nonlocal word_app, pythoncom_mod
                try:
                    close_word_app(word_app, pythoncom_mod)
                except Exception:
                    pass
                word_app = pythoncom_mod = None
                try:
                    word_app, pythoncom_mod = open_word_app()
                    self.log_signal.emit('  [INFO] Word 已重启')
                    return True
                except Exception as restart_err:
                    self.log_signal.emit(f'  [WARN] Word 重启失败：{restart_err}，后续 PDF 将逐文件启停')
                    word_app = pythoncom_mod = None
                    return False

            def _is_com_error(exc: Exception) -> bool:
                """判断是否是 COM 断连 / RPC 不可用错误。"""
                com_codes = {-2147417848, -2147023174, -2147023170}
                args = getattr(exc, 'args', ())
                if args and isinstance(args[0], int) and args[0] in com_codes:
                    return True
                msg = str(exc).lower()
                return any(k in msg for k in ('rpc', 'disconnected', '断开', '不可用'))

            try:
                for src, fmt in tasks:
                    if self._stop_event.is_set():
                        self.log_signal.emit('── 已终止 ──')
                        break
                    stem = Path(src).stem
                    self.log_signal.emit(
                        f'[{converted + failed + 1}/{total}] {stem} → {fmt.upper()}')
                    try:
                        convert_file(src, fmt, output_dir, output_dir,
                                     word_app=word_app if fmt == 'pdf' else None)
                        converted += 1
                    except Exception as e:
                        if fmt == 'pdf' and word_app is not None and _is_com_error(e):
                            # Word 实例崩溃 → 重启后重试一次
                            self.log_signal.emit(f'  [WARN] COM 断连，尝试重启 Word 后重试…')
                            _restart_word()
                            try:
                                convert_file(src, fmt, output_dir, output_dir,
                                             word_app=word_app)
                                converted += 1
                            except Exception as e2:
                                failed += 1
                                self.log_signal.emit(f'  ERROR: {e2}')
                        else:
                            failed += 1
                            self.log_signal.emit(f'  ERROR: {e}')
                    self.progress_signal.emit(converted + failed, total)
            finally:
                if word_app is not None:
                    close_word_app(word_app, pythoncom_mod)
                    self.log_signal.emit('Word 已关闭')

            self.log_signal.emit(f'\n完成：转换 {converted}，失败 {failed}')
            self.finished_signal.emit({
                'converted':  converted,
                'failed':     failed,
                'output_dir': output_dir,
            })

        except Exception as e:
            self.error_signal.emit(str(e))
