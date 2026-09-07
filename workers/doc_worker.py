"""
doc_worker.py - 文档处理后台线程（通用阶段 worker）

stage 参数：
  "extract"          → run_stage1_extract
  "gen_queries"      → run_stage3_gen_queries
  "match_chunks"     → run_stage4_match_chunks   (需要 extra_data={"queries": {...}})
  "general_query_gen"→ run_stage_general_query_gen (直接接收 file_paths，无需 extra_data)
  "file_match"       → run_stage_file_match       (需要 extra_data={"general_queries": [...]})
"""

import asyncio
import threading

from PySide6.QtCore import QThread, Signal


class DocWorker(QThread):
    log_signal      = Signal(str)
    progress_signal = Signal(int, int)
    row_ready       = Signal(dict)    # gen_queries: (filename, query)；match_chunks: (query, passage, …)
    finished_signal = Signal(dict)
    error_signal    = Signal(str)

    def __init__(self, stage: str, file_paths: list, config: dict,
                 extra_data: dict | None = None, parent=None):
        super().__init__(parent)
        self._stage      = stage
        self._file_paths = file_paths
        self._config     = config
        self._extra_data = extra_data
        self._stop_event  = threading.Event()
        self._pause_event = threading.Event()   # set=运行中, clear=暂停中

    def request_stop(self) -> None:
        self._stop_event.set()
        self._pause_event.set()

    def request_pause(self) -> None:
        self._pause_event.clear()

    def request_resume(self) -> None:
        self._pause_event.set()

    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def run(self) -> None:
        self._pause_event.set()

        def log(msg: str) -> None:
            self.log_signal.emit(msg)

        def pause_checker() -> bool:
            return not self._pause_event.is_set()

        stop_event = self._stop_event

        def progress_callback(current: int, total: int, row_data=None) -> None:
            self.progress_signal.emit(current, total)
            if row_data:
                self.row_ready.emit(row_data)

        async def _main():
            if self._stage == "extract":
                from core.doc_processor import run_stage1_extract as fn
                result = await fn(
                    file_paths=self._file_paths, config=self._config,
                    log=log, stop_checker=lambda: stop_event.is_set(),
                    pause_checker=pause_checker, progress_callback=progress_callback,
                )
            elif self._stage == "gen_queries":
                from core.doc_processor import run_stage3_gen_queries as fn
                result = await fn(
                    file_paths=self._file_paths, config=self._config,
                    log=log, stop_checker=lambda: stop_event.is_set(),
                    pause_checker=pause_checker, progress_callback=progress_callback,
                )
            elif self._stage == "match_chunks":
                from core.doc_processor import run_stage4_match_chunks as fn
                result = await fn(
                    file_paths=self._file_paths, config=self._config,
                    log=log, stop_checker=lambda: stop_event.is_set(),
                    pause_checker=pause_checker, progress_callback=progress_callback,
                    extra_data=self._extra_data,
                )
            elif self._stage == "general_query_gen":
                from core.doc_processor import run_stage_general_query_gen as fn
                result = await fn(
                    file_paths=self._file_paths, config=self._config,
                    log=log, stop_checker=lambda: stop_event.is_set(),
                    pause_checker=pause_checker, progress_callback=progress_callback,
                    extra_data=self._extra_data,
                )
            else:  # file_match
                from core.doc_processor import run_stage_file_match as fn
                result = await fn(
                    file_paths=self._file_paths, config=self._config,
                    log=log, stop_checker=lambda: stop_event.is_set(),
                    pause_checker=pause_checker, progress_callback=progress_callback,
                    extra_data=self._extra_data,
                )
            return result

        async def _run_with_watcher():
            main_task  = asyncio.create_task(_main())

            async def _watch():
                while not stop_event.is_set():
                    await asyncio.sleep(0.2)
                main_task.cancel()

            watch_task = asyncio.create_task(_watch())
            try:
                result = await main_task
            except asyncio.CancelledError:
                log("── 已终止 ──")
                result = {"processed": 0, "failed": 0}
            finally:
                watch_task.cancel()
                try:
                    await watch_task
                except asyncio.CancelledError:
                    pass
            return result

        try:
            result = asyncio.run(_run_with_watcher())
            self.finished_signal.emit(result)
        except Exception as e:
            self.error_signal.emit(str(e))
