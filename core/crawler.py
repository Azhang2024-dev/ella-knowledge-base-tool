"""
crawler.py - 文件爬虫核心逻辑

流程：
  BFS 遍历页面  →  每页扫描文件链接  →  下载 + 分类保存
  页面本身不保存，只作为发现文件链接的跳板
"""

import time
from collections import deque
from urllib.parse import urlparse

import requests

from core import parser, storage
from utils.url_utils import normalize_url, is_same_domain, filter_url
from utils.http_utils import (
    random_ua,
    browser_headers,
    download_headers,
    jitter_sleep,
    get_with_retry,
    ext_from_content_type,
)


def crawl(
    config: dict,
    log_callback=None,
    stop_checker=None,
    pause_checker=None,
) -> dict:
    """
    Returns:
        {
            "downloaded": int,
            "skipped":    int,
            "failed":     int,
            "saved":      list,
        }
    """

    def log(msg: str) -> None:
        if log_callback:
            log_callback(msg)

    def check_pause_or_stop() -> bool:
        if pause_checker:
            return pause_checker()
        return bool(stop_checker and stop_checker())

    # ── 配置 ──────────────────────────────────────────────────────────
    start_urls  = config.get("start_urls", [])
    max_files   = int(config.get("max_files", 50))
    max_pages   = int(config.get("max_pages", 100))
    max_depth   = int(config.get("max_depth", 2))
    interval    = float(config.get("request_interval", 1.0))
    timeout     = int(config.get("timeout", 15))
    max_retries = int(config.get("max_retries", 3))
    same_domain = bool(config.get("same_domain_only", True))
    exclude_kw  = [k.strip() for k in config.get("exclude_keywords", "").split(",") if k.strip()]
    include_kw  = [k.strip() for k in config.get("include_keywords", "").split(",") if k.strip()]
    output_dir  = config.get("output_dir", "output")
    ua_override = config.get("user_agent", "").strip()
    target_exts = set(config.get("target_extensions", ["pdf", "doc", "docx", "txt"]))
    proxy       = config.get("proxy", "").strip()

    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
        log(f"使用代理：{proxy}")

    def _ua() -> str:
        return ua_override if ua_override else random_ua()

    # ── 预热：用首个起始 URL 建立 session cookie ──────────────────────
    if start_urls:
        seed = normalize_url(start_urls[0])
        if seed:
            log(f"预热 session：{seed}")
            resp = get_with_retry(
                session, seed,
                browser_headers(seed, ua=_ua()),
                timeout, max_retries, log,
            )
            if resp:
                log("  session 预热完成")
            jitter_sleep(interval)

    # ── 状态 ──────────────────────────────────────────────────────────
    page_queue    = deque()
    visited_pages = set()
    queued_files  = set()
    seen_hashes   = set()
    downloaded = skipped = failed = 0
    saved_paths: list[str] = []
    pages_crawled = 0

    for u in start_urls:
        u = normalize_url(u)
        if u:
            page_queue.append((u, 0))

    log(f"开始扫描，起始页 {len(start_urls)} 个，目标类型：{', '.join(sorted(target_exts))}")

    # ── 阶段一：BFS 扫描页面，收集文件链接 ───────────────────────────
    file_queue: list[dict] = []

    while page_queue and pages_crawled < max_pages and len(file_queue) < max_files * 3:
        if check_pause_or_stop():
            log("已中断扫描")
            break

        url, depth = page_queue.popleft()
        if url in visited_pages:
            continue
        if not filter_url(url, exclude_kw):
            continue
        visited_pages.add(url)
        pages_crawled += 1

        log(f"[扫描页面 {pages_crawled}] {url}")

        resp = get_with_retry(
            session, url,
            browser_headers(url, ua=_ua()),
            timeout, max_retries, log,
        )
        if resp is None:
            failed += 1
            jitter_sleep(interval)
            continue

        html = resp.text

        # 扫描文件链接
        file_links = parser.extract_file_links(html, url, target_exts)
        new_files = 0
        for fl in file_links:
            if fl["url"] not in queued_files:
                combined = fl["text"] + " " + fl["url"]
                if include_kw and not any(kw.lower() in combined.lower() for kw in include_kw):
                    continue
                queued_files.add(fl["url"])
                fl["source_page"] = url   # 记录来源页，下载时作 Referer
                file_queue.append(fl)
                new_files += 1

        if new_files:
            log(f"  发现 {new_files} 个文件链接（累计 {len(file_queue)}）")

        # 递归页面
        if depth < max_depth:
            for link in parser.extract_page_links(html, url):
                link = normalize_url(link)
                if not link or link in visited_pages:
                    continue
                if same_domain and not is_same_domain(link, url):
                    continue
                if not filter_url(link, exclude_kw):
                    continue
                page_queue.append((link, depth + 1))

        jitter_sleep(interval)

    log(f"\n扫描完成，共发现 {len(file_queue)} 个文件，开始下载……\n")

    # ── 阶段二：下载文件 ─────────────────────────────────────────────
    for idx, fi in enumerate(file_queue, start=1):
        if check_pause_or_stop():
            log("已中断下载")
            break
        if downloaded >= max_files:
            log(f"已达最大下载数 {max_files}，停止")
            break

        log(f"[下载 {idx}/{len(file_queue)}] {fi['url']}")

        resp = get_with_retry(
            session, fi["url"],
            download_headers(fi["url"], fi.get("source_page", ""), ua=_ua()),
            timeout, max_retries, log,
            stream=True,
        )
        if resp is None:
            failed += 1
            jitter_sleep(interval)
            continue

        content = resp.content
        if not content:
            skipped += 1
            log("  [SKIP] 响应内容为空")
            continue

        actual_ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if actual_ct in ("text/html", "application/xhtml+xml"):
            skipped += 1
            log("  [SKIP] 服务器返回了 HTML 页面（非文件），跳过")
            continue

        # 若路径无扩展名，用 Content-Type 兜底推断
        if not fi.get("ext"):
            fi["ext"] = ext_from_content_type(actual_ct)
        if fi["ext"] not in target_exts:
            skipped += 1
            log(f"  [SKIP] 类型 .{fi['ext']} 不在目标列表")
            continue

        result = storage.save_file(content, fi, output_dir, seen_hashes)
        if result["saved"]:
            downloaded += 1
            saved_paths.append(result["path"])
            log(f"  [OK] → {result['path']}")
        else:
            skipped += 1
            log("  [SKIP] 重复文件")

        jitter_sleep(interval)

    log(f"\n全部完成：下载 {downloaded}，跳过 {skipped}，失败 {failed}")
    return {
        "downloaded": downloaded,
        "skipped":    skipped,
        "failed":     failed,
        "saved":      saved_paths,
    }
