"""
crawl_worker.py - 爬虫后台线程
封装爬虫任务，通过 Qt Signal 与 GUI 通信
"""

import queue
import threading

from PySide6.QtCore import QThread, Signal

from core import crawler


class CrawlWorker(QThread):
    """
    在独立线程中执行爬虫，通过 Signal 通知 GUI。
    支持暂停（pause）、继续（resume）、终止（stop）三种控制。
    """

    log_signal = Signal(str)
    progress_signal = Signal(int, int)   # (成功数, 失败数)
    finished_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self._stop_flag = False
        # _resume_event 置位 = 运行中；清除 = 暂停中
        self._resume_event = threading.Event()
        self._resume_event.set()

    # ------------------------------------------------------------------
    # 控制接口（由主线程调用）
    # ------------------------------------------------------------------

    def request_pause(self) -> None:
        self._resume_event.clear()

    def request_resume(self) -> None:
        self._resume_event.set()

    def request_stop(self) -> None:
        self._stop_flag = True
        self._resume_event.set()   # 唤醒暂停中的线程，让它检测到 stop

    # ------------------------------------------------------------------
    # 线程入口
    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            # ── 新增：datasource 路由（Wikipedia / 未来其他 API 数据源） ──
            ds_type = self._config.get("datasource", "web")
            if ds_type != "web":
                import asyncio
                from core.datasource import CrawlPipeline
                adapter = _build_datasource_adapter(ds_type)
                pipeline = CrawlPipeline(adapter)
                result = asyncio.run(pipeline.run(
                    config=self._config,
                    log=self._on_log,
                    stop_checker=self._should_stop,
                    pause_checker=self._wait_if_paused,
                ))
                self.finished_signal.emit(result)
                return
            # ── 原有逻辑：web 模式，不受影响 ──────────────────────────

            mode = self._config.get("crawl_mode", "classic")
            if mode == "smart":
                import asyncio
                from core.smart_crawler import smart_crawl
                result = asyncio.run(smart_crawl(
                    config=self._config,
                    log_callback=self._on_log,
                    stop_checker=self._should_stop,
                    pause_checker=self._wait_if_paused,
                ))
            elif mode == "firecrawl":
                import asyncio
                from core.firecrawl_crawler import firecrawl_crawl
                result = asyncio.run(firecrawl_crawl(
                    config=self._config,
                    log_callback=self._on_log,
                    stop_checker=self._should_stop,
                    pause_checker=self._wait_if_paused,
                ))
            else:
                result = crawler.crawl(
                    config=self._config,
                    log_callback=self._on_log,
                    stop_checker=self._should_stop,
                    pause_checker=self._wait_if_paused,
                )
            self.finished_signal.emit(result)
        except Exception as e:
            self.error_signal.emit(str(e))

    # ------------------------------------------------------------------
    # 内部回调
    # ------------------------------------------------------------------

    def _on_log(self, msg: str) -> None:
        self.log_signal.emit(msg)

    def _should_stop(self) -> bool:
        return self._stop_flag

    def _wait_if_paused(self) -> bool:
        """
        在每个页面请求前调用：
        - 如果暂停则阻塞直到 resume 或 stop；
        - 返回 True 表示应立即终止（stop 被请求）。
        """
        self._resume_event.wait()   # 暂停时阻塞
        return self._stop_flag


# ════════════════════════════════════════════════════════════════════════
# Datasource Adapter 工厂
# ════════════════════════════════════════════════════════════════════════

def _build_datasource_adapter(ds_type: str):
    """根据 datasource 类型字符串返回对应的 Adapter 实例。

    命名约定：datasource 配置值 = Adapter.name 属性值。
    """
    if ds_type in ("wikipedia_fr", "wikipedia_en", "wikipedia_de", "wikipedia"):
        lang = ds_type.split("_")[-1] if "_" in ds_type else "fr"
        from core.datasource_wikipedia import WikipediaAdapter
        return WikipediaAdapter(lang=lang)
    raise ValueError(f"未知数据源类型: {ds_type}")
