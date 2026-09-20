"""
firecrawl_crawler.py - 基于 Firecrawl 云端渲染的正文爬虫

走 Firecrawl REST API（Path E，不装 CLI），云端托管 JS 渲染 + 反检测 + 代理轮转。
适合强反爬 / 公众号 / SPA / 不想本机开浏览器的场景。

流程：
  起始 URL（或搜索词）→ /scrape 取正文落盘
                      → /map 发现站内链接 → 逐个 /scrape 落盘
正文落盘复用 smart_crawler 的 _save_page_content / _write_content_xlsx，保持一致。
"""

import asyncio
import os
import json
from pathlib import Path
from urllib.parse import urlparse

import requests

from core.smart_crawler import _save_page_content, _write_content_xlsx
from utils.url_utils import normalize_url
from utils.http_utils import jitter_sleep

_BASE = "https://api.firecrawl.dev/v2"
_POLL = 0.3


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _scrape_one(url: str, api_key: str, timeout: int, log, proxy: str = "auto") -> dict | None:
    """POST /scrape 单页，返回 {url, title, markdown, links} 或 None。
    proxy: "basic" | "enhanced" | "auto"（Firecrawl 内置代理档位，默认 auto）
    """
    try:
        resp = requests.post(
            f"{_BASE}/scrape",
            headers=_headers(api_key),
            json={"url": url, "formats": ["markdown", "links"], "onlyMainContent": True, "proxy": proxy},
            timeout=timeout,
        )
        if resp.status_code != 200:
            log(f"  [Firecrawl {resp.status_code}] {url}: {resp.text[:200]}")
            return None
        data = resp.json().get("data", {})
        md = data.get("markdown", "") or ""
        title = data.get("metadata", {}).get("title", "") or ""
        # 正文内容疑似错误页（极短 或 标题含 403/404/Forbidden/Not Found）时跳过
        status_code_in_title = any(kw in title for kw in ("403", "404", "Forbidden", "Not Found", "Access Denied"))
        if not md.strip() or (len(md.strip()) < 100 and status_code_in_title):
            if status_code_in_title:
                log(f"  [SKIP] 疑似错误页（{title}）")
            return None
        return {"url": url, "title": title, "markdown": md, "links": data.get("links", [])}
    except Exception as e:
        log(f"  [Firecrawl 异常] {url}: {e}")
        return None


def _map_site(url: str, api_key: str, timeout: int, limit: int, log) -> list[str]:
    """POST /map 发现站内链接，返回 URL 列表。"""
    try:
        resp = requests.post(
            f"{_BASE}/map",
            headers=_headers(api_key),
            json={"url": url, "limit": limit},
            timeout=timeout,
        )
        if resp.status_code != 200:
            log(f"  [Firecrawl map {resp.status_code}] {resp.text[:200]}")
            return []
        return resp.json().get("data", []) or []
    except Exception as e:
        log(f"  [Firecrawl map 异常] {e}")
        return []


def _search(query: str, api_key: str, timeout: int, limit: int, log) -> list[str]:
    """POST /search 按关键词发现页面，返回 URL 列表。"""
    try:
        resp = requests.post(
            f"{_BASE}/search",
            headers=_headers(api_key),
            json={"query": query, "limit": limit},
            timeout=timeout,
        )
        if resp.status_code != 200:
            log(f"  [Firecrawl search {resp.status_code}] {resp.text[:200]}")
            return []
        data = resp.json().get("data", []) or []
        return [d.get("url", "") for d in data if d.get("url")]
    except Exception as e:
        log(f"  [Firecrawl search 异常] {e}")
        return []


async def firecrawl_crawl(
    config: dict,
    log_callback=None,
    stop_checker=None,
    pause_checker=None,
) -> dict:
    """
    Firecrawl 模式主入口。
    Returns:
        {"scraped", "skipped", "failed", "saved", "content_xlsx"}
    """
    def log(msg: str) -> None:
        if log_callback:
            log_callback(msg)

    def should_stop() -> bool:
        return bool(stop_checker and stop_checker())

    def wait_if_paused() -> bool:
        return bool(pause_checker and pause_checker())

    # ── 配置 ─────────────────────────────────────────────────────────
    start_urls    = [normalize_url(u) for u in config.get("start_urls", []) if normalize_url(u)]
    search_query  = config.get("firecrawl_search", "").strip()
    api_key = (
        (config.get("firecrawl_api_key") or "").strip()
        or os.environ.get("FIRECRAWL_API_KEY", "").strip()
    )
    if not api_key:
        raise ValueError(
            "Firecrawl API key missing: set config['firecrawl_api_key'] "
            "or environment variable FIRECRAWL_API_KEY (no hardcoded default)."
        )
    output_dir    = config.get("output_dir", "output")
    timeout       = int(config.get("timeout", 60))
    max_pages     = int(config.get("max_pages", 50))
    interval      = float(config.get("request_interval", 1.0))
    same_domain   = bool(config.get("same_domain_only", True))
    content_formats = [f.strip().lower() for f in config.get("content_formats", ["txt"]) if f.strip()]
    use_map       = bool(config.get("firecrawl_use_map", False))   # 是否用 /map 扩展发现
    map_limit     = int(config.get("firecrawl_map_limit", 100))
    max_depth     = int(config.get("max_depth", 2))
    fc_proxy      = config.get("firecrawl_proxy", "auto")   # basic / enhanced / auto

    seen_hashes: set[str] = set()
    seen_urls: set[str] = set()
    content_rows: list[dict] = []
    saved_paths: list[str] = []
    scraped = skipped = failed = 0

    log(f"[Firecrawl 模式] 云端渲染 + 正文提取，目标格式：{content_formats}")

    # ── 组装待抓 URL 列表 ─────────────────────────────────────────────
    targets: list[str] = []
    for u in start_urls:
        if u not in seen_urls:
            seen_urls.add(u)
            targets.append(u)

    if search_query:
        log(f"[Firecrawl] 搜索发现：{search_query}")
        found = _search(search_query, api_key, timeout, max_pages, log)
        for u in found:
            if u and u not in seen_urls:
                seen_urls.add(u)
                targets.append(u)

    # 用 /map 扩展每个起始 URL 的站内链接
    if use_map:
        for seed in list(start_urls):
            if should_stop() or wait_if_paused():
                break
            log(f"[Firecrawl] map 发现站内链接：{seed}")
            links = _map_site(seed, api_key, timeout, map_limit, log)
            seed_domain = urlparse(seed).netloc
            for u in links:
                if not u or u in seen_urls:
                    continue
                if same_domain and urlparse(u).netloc != seed_domain:
                    continue
                seen_urls.add(u)
                targets.append(u)
            log(f"  map 新增 {len(links)} 个候选，待抓总数 {len(targets)}")

    # 截断到 max_pages，初始深度为 0
    seed_domain = urlparse(start_urls[0]).netloc if start_urls else ""
    # depth_queue: [(url, depth), ...]
    depth_queue: list[tuple[str, int]] = [(u, 0) for u in targets[:max_pages]]
    log(f"[Firecrawl] 待抓种子 {len(depth_queue)} 个页面，最大深度 {max_depth}")

    # ── 深度队列：逐页 scrape + 落盘 + 跟进子链接 ────────────────────────
    total_scraped_count = 0
    while depth_queue:
        if should_stop() or wait_if_paused():
            log("已中断")
            break
        if total_scraped_count >= max_pages:
            log(f"已达最大页数 {max_pages}，停止")
            break

        url, depth = depth_queue.pop(0)
        total_scraped_count += 1
        log(f"[抓取 {total_scraped_count} | 深度{depth}] {url}")
        result = _scrape_one(url, api_key, timeout, log, proxy=fc_proxy)
        if result is None:
            failed += 1
            jitter_sleep(interval)
            continue

        title = result["title"] or url
        text  = result["markdown"]
        saved = _save_page_content(
            title, url, text, output_dir,
            content_formats, seen_hashes, content_rows, log,
        )
        if saved:
            scraped += 1
            saved_paths.extend(saved)
        else:
            skipped += 1

        # 未达最大深度时，把本页发现的链接推入队列
        # Firecrawl links 字段可能是 list[str] 或 list[dict]，两种都兼容
        if depth < max_depth:
            for lk in result.get("links", []):
                if isinstance(lk, str):
                    child = lk.strip()
                elif isinstance(lk, dict):
                    child = (lk.get("url") or "").strip()
                else:
                    continue
                if not child or child in seen_urls:
                    continue
                if same_domain and urlparse(child).netloc != seed_domain:
                    continue
                seen_urls.add(child)
                depth_queue.append((child, depth + 1))

        jitter_sleep(interval)

    # ── xlsx 汇总 ─────────────────────────────────────────────────────
    content_xlsx_path = None
    if "xlsx" in content_formats and content_rows:
        content_xlsx_path = _write_content_xlsx(content_rows, output_dir, log)

    log(f"\n全部完成：抓取 {scraped}，跳过 {skipped}，失败 {failed}")
    return {
        "scraped":       scraped,
        "skipped":       skipped,
        "failed":        failed,
        "saved":         saved_paths,
        "content_xlsx":  content_xlsx_path,
    }
