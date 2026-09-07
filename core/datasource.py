"""
datasource.py — 平台无关的数据源接口 + 爬取流水线 + JSONL 工具

设计：
  DataSource Protocol — 3 个方法，所有 Adapter 实现此接口
  ItemRecord          — 统一中间格式（采集 → 后续 Stage 的桥梁）
  CrawlPipeline       — 编排 enumerate → 并发 fetch → 清洗 → 落盘
  JSONL 读写          — 跨 Stage 数据交换格式
"""

import asyncio
import json
import os
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Protocol, runtime_checkable


# ════════════════════════════════════════════════════════════════════════
# ItemRecord — 统一中间格式
# ════════════════════════════════════════════════════════════════════════

@dataclass
class ItemRecord:
    """平台无关的内容条目，所有 DataSource Adapter 统一产出此格式。

    Fields:
        id:          全局唯一标识（建议 "{source}:{native_id}" 格式）
        title:       条目标题
        source_url:  原始 URL
        text:        正文纯文本（采集阶段填充）
        metadata:    平台特定元信息（lang, revid, categories, ...）
    """
    id: str
    title: str
    source_url: str
    text: str = ""
    metadata: dict = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════
# DataSource Protocol
# ════════════════════════════════════════════════════════════════════════

@runtime_checkable
class DataSource(Protocol):
    """数据源接口协议。

    每个平台（Wikipedia, Arxiv, 通用网页...）实现此协议的 3 个方法，
    CrawlPipeline 只依赖此协议，不感知具体平台。
    """

    @property
    def name(self) -> str:
        """数据源标识，如 "wikipedia_fr", "wikipedia_en", "arxiv", "web" """
        ...

    async def enumerate_items(
        self, config: dict, log, stop_checker, pause_checker
    ) -> AsyncIterator[ItemRecord]:
        """枚举所有待处理条目（不含正文，仅元信息）。

        Yields:
            ItemRecord: id / title / source_url / metadata 已填充，text 为空
        """
        ...

    async def fetch_content(
        self, item: ItemRecord, config: dict, log
    ) -> str:
        """根据 item 获取并清洗正文，返回纯文本。

        Args:
            item:    enumerate_items 产出的条目（含 id/title/source_url）
            config:  全局配置

        Returns:
            清洗后的纯文本字符串
        """
        ...


# ════════════════════════════════════════════════════════════════════════
# CrawlPipeline — 编排器
# ════════════════════════════════════════════════════════════════════════

class CrawlPipeline:
    """通用爬取流水线：枚举 → 并发获取 → 清洗 → 落盘 JSONL + 纯文本。

    所有 DataSource Adapter 共用此编排逻辑：
    - enumerate_items() 产出 item 流
    - 并发调用 fetch_content() 获取正文
    - 逐 item 落盘（extracted/{safe_title}.txt）
    - 汇总写入 items.jsonl
    """

    def __init__(self, datasource: DataSource):
        self._ds = datasource

    async def run(
        self,
        config: dict,
        log=None,
        stop_checker=None,
        pause_checker=None,
        progress_callback=None,
    ) -> dict:
        """执行完整流水线。

        Returns:
            {"output_dir", "jsonl_path", "total", "success", "failed", "items"}
        """
        _log = log or (lambda _: None)

        def _should_stop():
            return bool(stop_checker and stop_checker())

        def _is_paused():
            """同步版本：供 enumerate_items 在非 async 上下文中检测暂停。"""
            return bool(pause_checker and pause_checker())

        async def _wait_if_paused():
            if pause_checker:
                while pause_checker():
                    await asyncio.sleep(0.3)

        output_dir = self._make_output_dir(config)
        concurrency = int(config.get("datasource_concurrency", 5))
        request_interval = float(config.get("request_interval", 0.3))

        # ── Phase 1: 枚举 ────────────────────────────────────────────────
        _log(f"[{self._ds.name}] 开始枚举条目…")
        items: list[ItemRecord] = []
        async for item in self._ds.enumerate_items(
            config, _log, _should_stop, _is_paused
        ):
            if _should_stop():
                _log("已中断枚举")
                break
            items.append(item)

        total = len(items)
        _log(f"[{self._ds.name}] 枚举完成：{total} 个条目")

        if total == 0:
            _log("未枚举到任何条目，流水线结束")
            return {
                "output_dir": str(output_dir),
                "jsonl_path": "",
                "total": 0,
                "success": 0,
                "failed": 0,
                "items": [],
            }

        # ── Phase 2: 并发获取正文 ─────────────────────────────────────────
        _log(f"[{self._ds.name}] 开始获取正文（并发: {concurrency}）…")
        extracted_dir = output_dir / "extracted"
        extracted_dir.mkdir(parents=True, exist_ok=True)

        sem = asyncio.Semaphore(concurrency)
        seen_hashes: set[str] = set()
        success = failed = 0

        async def _process_one(idx: int, item: ItemRecord) -> ItemRecord:
            nonlocal success, failed
            async with sem:
                if _should_stop():
                    return item
                await _wait_if_paused()

                try:
                    text = await self._ds.fetch_content(item, config, _log)
                    if text and text.strip():
                        # 去重
                        digest = hashlib.sha256(
                            text.encode("utf-8", "ignore")
                        ).hexdigest()
                        if digest in seen_hashes:
                            _log(f"  [{idx}/{total}] {item.title[:50]} — 重复，跳过")
                            item.text = ""
                            failed += 1
                            return item
                        seen_hashes.add(digest)

                        item.text = text
                        # 落盘单篇纯文本
                        self._save_extracted(extracted_dir, item)
                        _log(f"  [{idx}/{total}] {item.title[:50]} — ✓ {len(text)} 字符")
                        success += 1
                    else:
                        _log(f"  [{idx}/{total}] {item.title[:50]} — 空正文，跳过")
                        failed += 1
                except Exception as e:
                    _log(f"  [{idx}/{total}] {item.title[:50]} — 失败: {e}")
                    failed += 1

                if progress_callback:
                    progress_callback(idx, total, success, failed)
                return item

        tasks = [
            asyncio.create_task(_process_one(i, item))
            for i, item in enumerate(items, start=1)
        ]
        items = await asyncio.gather(*tasks)
        items = list(items)

        # ── Phase 3: 写 JSONL 汇总 ────────────────────────────────────────
        jsonl_path = output_dir / "items.jsonl"
        write_items_jsonl(str(jsonl_path), [it for it in items if it.text])

        # ── Phase 4: 写元信息 ────────────────────────────────────────────
        meta = {
            "datasource": self._ds.name,
            "timestamp": datetime.now().isoformat(),
            "total": total,
            "success": success,
            "failed": failed,
            "output_dir": str(output_dir),
        }
        (output_dir / "crawl_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        _log(
            f"\n[{self._ds.name}] 完成：共 {total} 条目，"
            f"成功 {success}，失败 {failed}"
        )
        _log(f"输出目录：{output_dir}")

        return {
            "output_dir": str(output_dir),
            "jsonl_path": str(jsonl_path),
            "total": total,
            "success": success,
            "failed": failed,
            "items": items,
        }

    # ── 内部 ────────────────────────────────────────────────────────────

    def _make_output_dir(self, config: dict) -> Path:
        base = config.get("output_dir", "output")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = self._ds.name
        out = Path(base) / name / ts
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _save_extracted(self, extracted_dir: Path, item: ItemRecord) -> None:
        from utils.file_utils import safe_filename

        stem = safe_filename(item.title or item.id, max_length=80)
        dest = extracted_dir / f"{stem}.txt"
        # 避免同名覆盖
        i = 1
        while dest.exists():
            dest = extracted_dir / f"{stem}_{i}.txt"
            i += 1
        dest.write_text(item.text, encoding="utf-8")


# ════════════════════════════════════════════════════════════════════════
# JSONL 读写工具
# ════════════════════════════════════════════════════════════════════════

def write_items_jsonl(path: str, items: list[ItemRecord]) -> None:
    """将 ItemRecord 列表写入 JSONL 文件。

    每行一个 JSON 对象，字段与 ItemRecord 一致。
    后续 Stage（doc_processor）可直接读取此文件作为输入。
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            d = asdict(item)
            d["metadata"] = json.dumps(d.get("metadata", {}), ensure_ascii=False)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def read_items_jsonl(path: str) -> list[ItemRecord]:
    """从 JSONL 文件读取 ItemRecord 列表。

    用于后续 Stage 导入采集结果。
    """
    items: list[ItemRecord] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            meta = d.get("metadata", "{}")
            if isinstance(meta, str):
                meta = json.loads(meta)
            items.append(ItemRecord(
                id=d.get("id", ""),
                title=d.get("title", ""),
                source_url=d.get("source_url", ""),
                text=d.get("text", ""),
                metadata=meta if isinstance(meta, dict) else {},
            ))
    return items


def items_to_file_paths(items: list[ItemRecord], extracted_dir: str) -> list[str]:
    """将 ItemRecord 列表映射为 extracted/*.txt 文件路径列表。

    用于对接 doc_processor Stage1，它接受文件路径列表作为输入。
    """
    from utils.file_utils import safe_filename

    result: list[str] = []
    for item in items:
        if not item.text:
            continue
        stem = safe_filename(item.title or item.id, max_length=80)
        p = os.path.join(extracted_dir, f"{stem}.txt")
        if os.path.isfile(p):
            result.append(p)
    return result
