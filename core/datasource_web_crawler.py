"""
datasource_web_crawler.py — 通用网页 DataSource Adapter

包装现有的 smart_crawler.py / crawler.py，使其符合 DataSource Protocol。
不修改 smart_crawler.py 任何代码，仅做接口适配。

smart_crawl / classic_crawl 本身已是完整的端到端流程（扫描+下载），
不适合拆成 enumerate + fetch 两步。因此本 Adapter 采用简化策略：
  - enumerate_items(): 直接调用完整爬取流程
  - fetch_content():   返回已爬取的内容（不重复请求）

这保持了向后兼容性——当 datasource 未设置或为 "web" 时，
CrawlWorker 仍走原有分发逻辑，不经过 CrawlPipeline。
"""

from typing import AsyncIterator

from core.datasource import ItemRecord


class WebCrawlAdapter:
    """通用网页爬取 Adapter（包装现有 smart_crawler / classic_crawler）。

    此 Adapter 不拆分 enumerate/fetch —— 因为现有爬虫逻辑已是完整的
    单次调用流程（Playwright 渲染 + LLM 分类 + 文件下载），强行拆开
    反而破坏原有优化（如链接分类结果复用）。

    当 datasource="web" 时，CrawlWorker 直接调用 smart_crawl / crawl，
    不经过 CrawlPipeline。此 Adapter 作为协议占位，提供统一的 name 属性。
    """

    def __init__(self):
        pass

    @property
    def name(self) -> str:
        return "web"

    async def enumerate_items(
        self, config: dict, log, stop_checker, pause_checker
    ) -> AsyncIterator[ItemRecord]:
        """Web 模式不拆 enumerate/fetch，此方法不应被调用。

        CrawlWorker 检测到 datasource="web" 时直接走原有分发逻辑。
        """
        # 不 yield 任何 item——CrawlWorker 不会走到这里
        return
        yield  # type: ignore[unreachable]

    async def fetch_content(self, item: ItemRecord, config: dict, log) -> str:
        """Web 模式不拆 enumerate/fetch，此方法不应被调用。"""
        return ""
