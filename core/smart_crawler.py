"""
smart_crawler.py - 基于 crawl4ai 的智能模式爬虫

架构：
  - Playwright 渲染页面（处理 JS、动态内容）
  - 从 result.links 提取所有链接（已结构化，几 KB）
  - 将紧凑链接列表发给 LLM 分类（files / nav_links）
  - 多轮跟进：起始页 → nav_links → 再一轮
  - 文件下载复用 storage.save_file()
"""

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests

from core import storage
from utils.http_utils import download_headers, random_ua, ext_from_content_type, jitter_sleep
from utils.url_utils import normalize_url

_DEFAULT_INSTRUCTION = (
    "请分析以下链接列表，返回两类信息：\n\n"
    "【files】可直接下载的文件链接，包括：\n"
    "- 有文件扩展名的链接（.pdf/.docx/.xlsx/.zip/.epub 等）\n"
    "- 链接文字含「下载」、「Download」、「PDF」等提示的链接\n"
    "- 没有扩展名但指向文件的动态 URL（如 /download?id=xxx）\n"
    "排除：将当前网页本身导出为 PDF 的功能按钮（Download Page as PDF、Save as PDF）\n\n"
    "【nav_links】所有值得深入跟进的页面链接，包括：\n"
    "- 搜索结果列表中每一个条目的链接（资源详情页、课程页、资料页）\n"
    "- 书籍详情页、课程内容页、章节页、教材目录页\n"
    "- 下一页、更多内容等分页链接\n"
    "- 明显会包含可下载资源的子目录页\n"
    "- 新闻文章、博客文章、公众号文章等内容详情页\n"
    "排除：顶部导航栏、搜索框、登录、注册、关于、联系、语言切换等功能性链接\n\n"
    "只返回 JSON，格式：{\"files\": [{\"url\":\"...\",\"text\":\"...\",\"file_type\":\"pdf\"},...], "
    "\"nav_links\": [{\"url\":\"...\",\"text\":\"...\"},...] }\n"
)

# 正文抓取模式专用指令：目标是新闻/博客/公众号文章正文，不强求含下载文件
_CONTENT_INSTRUCTION = (
    "请分析以下链接列表，返回两类信息：\n\n"
    "【files】可直接下载的文件链接（.pdf/.docx 等），若无则返回空列表。\n\n"
    "【nav_links】所有值得抓取正文的页面链接，包括：\n"
    "- 新闻文章、博客文章、公众号文章、评论文章等内容详情页\n"
    "- 搜索结果或列表页中每一个条目的链接\n"
    "- 下一页、更多文章等分页链接\n"
    "- 专题页、栏目页、标签页等会包含更多文章的聚合页\n"
    "排除：顶部导航栏、登录、注册、关于、联系、语言切换、广告等功能性链接\n\n"
    "只返回 JSON，格式：{\"files\": [], "
    "\"nav_links\": [{\"url\":\"...\",\"text\":\"...\"},...] }\n"
)


def _build_full_provider(config: dict) -> str:
    provider = config.get("llm_provider", "openai")
    model    = config.get("llm_model", "")
    if not model:
        model = {
            "anthropic": "claude-haiku-4-5-20251001",
            "openai":    "gpt-4o-mini",
            "gemini":    "gemini-2.0-flash",
            "deepseek":  "deepseek-chat",
            "ollama":    "llama3",
        }.get(provider, "gpt-4o-mini")
    if provider == "custom":
        return f"openai/{model}"
    return f"{provider}/{model}"


def _build_extra_body(config: dict) -> dict | None:
    """根据 thinking_mode 构建 litellm extra_body，控制 Gemini 3 思考模式。

    thinking_mode 取值：
      - "auto" (默认): Gemini 3 模型自动禁用 thinking（否则 content 可能为空）
      - "disabled":   强制禁用所有模型的 thinking
      - "enabled":    交由模型自行决定（Gemini 3 默认会启用 thinking）
    返回 None 表示无需额外参数。
    """
    model = config.get("llm_model", "")
    mode  = config.get("thinking_mode", "auto")

    if mode == "disabled":
        return {"thinking": {"type": "disabled"}}
    if mode == "enabled":
        return None
    # auto: 仅对 gemini-3 系列自动禁用
    if "gemini-3" in model.lower():
        return {"thinking": {"type": "disabled"}}
    return None


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lstrip(".").lower()
    return suffix if len(suffix) <= 5 else ""


def _resolve_url(url: str, page_url: str) -> str | None:
    url = url.strip()
    if not url or url.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/") or not url.startswith("http"):
        try:
            return urljoin(page_url, url)
        except Exception:
            return None
    return url


async def _llm_classify_links(
    links: list[dict],
    page_url: str,
    instruction: str,
    config: dict,
    log,
) -> dict:
    """
    把链接列表（紧凑文本，几 KB）发给 LLM 分类。
    返回 {"files": [...], "nav_links": [...]}
    """
    if not links:
        return {"files": [], "nav_links": []}

    # 构造紧凑的链接列表文本
    lines = [f"页面：{page_url}", f"共 {len(links)} 个链接：", ""]
    for i, lk in enumerate(links, 1):
        text = (lk.get("text") or "").strip()[:60]
        url  = lk.get("href") or lk.get("url", "")
        lines.append(f"{i}. [{text}] {url}")

    link_text = "\n".join(lines)
    prompt = f"{instruction}\n\n{link_text}"

    full_provider = _build_full_provider(config)
    api_key  = config.get("llm_api_key", "").strip() or None
    base_url = config.get("llm_base_url", "").strip() or None

    raw = ""
    try:
        import litellm
        kwargs = dict(
            model=full_provider,
            messages=[{"role": "user", "content": prompt}],
            api_key=api_key,
            max_tokens=2000,
            temperature=0.0,
        )
        eb = _build_extra_body(config)
        if eb:
            kwargs["extra_body"] = eb
        if base_url:
            kwargs["base_url"] = base_url
        resp = await litellm.acompletion(**kwargs)
        raw = resp.choices[0].message.content.strip()
    except Exception as e:
        log(f"  [LLM 错误] {e}")
        return {"files": [], "nav_links": []}

    # 调试：显示 LLM 原始回复前 300 字符
    log(f"  [LLM 回复] {raw[:300]}")

    # 解析 LLM 返回的 JSON
    try:
        # 提取 JSON 块（LLM 可能加 markdown 代码块）
        m = re.search(r'\{[\s\S]*\}', raw)
        if m:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else {"files": [], "nav_links": []}
    except Exception:
        pass
    return {"files": [], "nav_links": []}


def _filter_files(
    items: list,
    page_url: str,
    target_exts: set,
    include_kw: list,
    exclude_kw: list,
    seen_urls: set,
) -> list[dict]:
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = _resolve_url(item.get("url", ""), page_url)
        if not url or url in seen_urls:
            continue

        ext = (item.get("file_type") or "").strip(".").lower() or _ext_from_url(url)

        combined = (item.get("text", "") + " " + url).lower()
        if exclude_kw and any(k.lower() in combined for k in exclude_kw):
            continue
        if include_kw and not any(k.lower() in combined for k in include_kw):
            if ext not in target_exts:
                continue

        seen_urls.add(url)
        results.append({
            "url":  url,
            "text": item.get("text", ""),
            "ext":  ext,
            "source_page": page_url,
        })
    return results


def _save_page_content(
    title: str,
    url: str,
    text: str,
    output_dir: str,
    formats: list[str],
    seen_hashes: set,
    rows: list[dict],
    log,
) -> list[str]:
    """
    把单页正文按 formats 落盘，返回已写入的路径列表。
    - txt/md：分篇存到 output_dir/Articles/{safe_title}.{ext}
    - xlsx：不在此处写，只收集到 rows，由调用方汇总写一次
    用 sha256 去重，重复正文不重复落盘。
    """
    import hashlib
    from utils.file_utils import safe_filename, ensure_dir

    saved: list[str] = []
    if not text or not text.strip():
        return saved
    digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
    if digest in seen_hashes:
        log(f"  [正文] 重复，跳过：{title[:40]}")
        return saved
    seen_hashes.add(digest)

    rows.append({"title": title, "url": url, "text": text})

    articles_dir = Path(output_dir) / "Articles"
    ensure_dir(articles_dir)
    stem = safe_filename(title or "untitled", max_length=80)
    # 避免同名覆盖
    dest_base = articles_dir / stem
    i = 1
    while any((dest_base.with_name(f"{dest_base.stem}{'_'+str(i) if i>1 else ''}.{fmt}")).exists()
              for fmt in formats if fmt in ("txt", "md")):
        i += 1
    final_stem = dest_base.stem if i == 1 else f"{dest_base.stem}_{i}"

    for fmt in formats:
        if fmt == "txt":
            p = articles_dir / f"{final_stem}.txt"
            p.write_text(text, encoding="utf-8")
            saved.append(str(p))
        elif fmt == "md":
            md = f"# {title}\n\n> 来源：{url}\n\n{text}"
            p = articles_dir / f"{final_stem}.md"
            p.write_text(md, encoding="utf-8")
            saved.append(str(p))
        # xlsx 由调用方汇总，此处不写
    if saved:
        log(f"  [正文] 已存：{', '.join(Path(s).name for s in saved)}")
    return saved


def _write_content_xlsx(rows: list[dict], output_dir: str, log) -> str | None:
    """把收集的正文汇总写成一个 xlsx，返回路径；失败返回 None。"""
    if not rows:
        return None
    try:
        from openpyxl import Workbook
    except ImportError as e:
        log(f"  [正文 xlsx] openpyxl 未安装：{e}")
        return None
    from utils.file_utils import ensure_dir
    wb = Workbook()
    ws = wb.active
    ws.title = "Articles"
    ws.append(["title", "url", "text"])
    for r in rows:
        ws.append([r.get("title", ""), r.get("url", ""), r.get("text", "")])
    out = Path(output_dir) / "Articles.xlsx"
    ensure_dir(out.parent)
    wb.save(str(out))
    log(f"  [正文] xlsx 汇总：{out}")
    return str(out)


async def smart_crawl(
    config: dict,
    log_callback=None,
    stop_checker=None,
    pause_checker=None,
) -> dict:
    """
    智能模式主入口。
    Returns:
        {"downloaded", "skipped", "failed", "saved", "report_path", "page_contents"}
    """
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
    from crawl4ai.async_configs import BrowserConfig

    def log(msg: str) -> None:
        if log_callback:
            log_callback(msg)

    def should_stop() -> bool:
        return bool(stop_checker and stop_checker())

    def wait_if_paused() -> bool:
        return bool(pause_checker and pause_checker())

    # ── 读取配置 ─────────────────────────────────────────────────────
    start_urls   = [normalize_url(u) for u in config.get("start_urls", []) if normalize_url(u)]
    max_files    = int(config.get("max_files", 50))
    max_pages    = int(config.get("max_pages", 100))
    max_depth    = int(config.get("max_depth", 2))
    interval     = float(config.get("request_interval", 1.0))
    timeout      = int(config.get("timeout", 20))
    max_retries  = int(config.get("max_retries", 3))
    same_domain  = bool(config.get("same_domain_only", True))
    include_kw   = [k.strip() for k in config.get("include_keywords", "").split(",") if k.strip()]
    exclude_kw   = [k.strip() for k in config.get("exclude_keywords", "").split(",") if k.strip()]
    output_dir   = config.get("output_dir", "output")
    target_exts  = set(config.get("target_extensions", ["pdf", "doc", "docx", "txt"]))
    generate_report = bool(config.get("generate_report", True))
    # 有自定义指令时用自定义；否则按模式自动选：
    # 正文抓取模式 → _CONTENT_INSTRUCTION；文件下载模式 → _DEFAULT_INSTRUCTION
    _auto_instruction = _CONTENT_INSTRUCTION if config.get("save_page_content") else _DEFAULT_INSTRUCTION
    instruction  = config.get("llm_instruction", "").strip() or _auto_instruction

    # ── 正文落盘配置 ─────────────────────────────────────────────────
    # save_page_content: 是否把每页正文单独存文件
    # content_formats:    落盘格式子集，如 ["txt","md","xlsx"]（txt/md 分篇存，xlsx 汇总）
    save_page_content = bool(config.get("save_page_content", False))
    content_formats   = [f.strip().lower() for f in config.get("content_formats", ["txt"]) if f.strip()]
    content_rows: list[dict] = []   # 供 xlsx 汇总：{title, url, text}

    # ── 代理 / 反检测配置 ─────────────────────────────────────────────
    proxy       = config.get("proxy", "").strip()
    anti_detect = bool(config.get("anti_detect", True))
    extra_args  = []
    if anti_detect:
        extra_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]

    full_provider = _build_full_provider(config)

    # ── 浏览器配置（只渲染，不做 LLM 提取） ──────────────────────────
    browser_cfg = BrowserConfig(
        headless=True,
        enable_stealth=True,
        user_agent_mode="random",
        java_script_enabled=True,
        proxy=proxy or None,
        extra_args=extra_args or None,
    )

    # 不挂 extraction_strategy，只拿渲染后的链接
    run_cfg = CrawlerRunConfig(
        scan_full_page=True,
        wait_until="domcontentloaded",
        page_timeout=timeout * 1000,
        cache_mode=CacheMode.BYPASS,
        verbose=False,
    )

    # ── 状态 ─────────────────────────────────────────────────────────
    queued_files: list[dict] = []
    seen_file_urls: set[str] = set()
    seen_nav_urls: set[str] = set()
    seen_hashes: set[str] = set()
    page_contents: list[str] = []
    downloaded = skipped = failed = 0
    saved_paths: list[str] = []
    pages_crawled = 0

    seed_domain = urlparse(start_urls[0]).netloc if start_urls else ""

    log(f"[智能模式] 启动 Playwright 浏览器…")
    log(f"[智能模式] LLM 服务：{full_provider}")
    log(f"[智能模式] 起始 URL：{len(start_urls)} 个，最大深度：{max_depth}")

    for u in start_urls:
        seen_nav_urls.add(u)

    # ── 内部扫描函数 ──────────────────────────────────────────────────
    async def _scan_pages(web_crawler, urls: list[str], round_label: str) -> list[str]:
        nonlocal pages_crawled, failed
        new_nav: list[str] = []

        for url in urls:
            if should_stop() or wait_if_paused():
                break
            if pages_crawled >= max_pages or len(queued_files) >= max_files * 3:
                break

            log(f"\n[{round_label}] {url}")
            try:
                result = await web_crawler.arun(url, config=run_cfg)
            except Exception as e:
                log(f"  [ERROR] Playwright 失败：{e}")
                failed += 1
                continue

            results = result if isinstance(result, list) else [result]

            for page_result in results:
                if should_stop():
                    break
                if pages_crawled >= max_pages:
                    break

                pages_crawled += 1
                page_url = getattr(page_result, "url", url)
                log(f"[扫描页面 {pages_crawled}] {page_url}")

                try:
                    if not getattr(page_result, "success", False):
                        err = str(getattr(page_result, "error_message", "未知错误")).splitlines()[0]
                        log(f"  [SKIP] 加载失败：{err}")
                        failed += 1
                        continue

                    # 收集页面正文
                    md = getattr(page_result, "markdown", None)
                    fit_md = ""
                    if md is not None:
                        fit_md = md.fit_markdown if hasattr(md, "fit_markdown") else (md if isinstance(md, str) else "")
                    if fit_md:
                        page_contents.append(f"## 来源：{page_url}\n\n{fit_md[:3000]}")
                        # 正文落盘（全文，不截断）
                        if save_page_content and content_formats:
                            from core.parser import extract_title
                            _title = extract_title(
                                getattr(page_result, "html", "") or "", page_url
                            )
                            _save_page_content(
                                _title, page_url, fit_md, output_dir,
                                content_formats, seen_hashes, content_rows, log,
                            )

                    # 从 result.links 取结构化链接表
                    links_data = getattr(page_result, "links", {}) or {}
                    all_links: list[dict] = []
                    for group in ("internal", "external"):
                        for lk in links_data.get(group, []):
                            href = _resolve_url(lk.get("href", ""), page_url)
                            if href:
                                all_links.append({"href": href, "text": lk.get("text", "")})

                    log(f"  页面链接：{len(all_links)} 个，发送 LLM 分类…")

                    # LLM 分类（只发链接列表，快速）
                    data = await _llm_classify_links(
                        all_links, page_url, instruction, config, log
                    )

                    # 文件链接
                    new_files = _filter_files(
                        data.get("files", []), page_url, target_exts,
                        include_kw, exclude_kw, seen_file_urls,
                    )
                    queued_files.extend(new_files)
                    if new_files:
                        log(f"  文件：{len(new_files)} 个（累计 {len(queued_files)}）")

                    # 导航链接
                    for nav in data.get("nav_links", []):
                        nav_url = _resolve_url(nav.get("url", ""), page_url)
                        if not nav_url or nav_url in seen_nav_urls:
                            continue
                        if same_domain and urlparse(nav_url).netloc != seed_domain:
                            continue
                        seen_nav_urls.add(nav_url)
                        new_nav.append(nav_url)

                    log(f"  LLM 分类结果：files={len(data.get('files',[]))}，nav_links={len(data.get('nav_links',[]))}，新增跟进={len(new_nav)}")

                except Exception as e:
                    log(f"  [SKIP] 页面处理出错：{e}")
                    failed += 1

                await asyncio.sleep(interval * 0.5)

        return new_nav

    # ── 阶段一：多轮扫描 ─────────────────────────────────────────────
    async with AsyncWebCrawler(config=browser_cfg) as web_crawler:
        nav_queue = await _scan_pages(web_crawler, start_urls, "智能扫描·第1轮")

        for depth in range(2, max_depth + 1):
            if not nav_queue or should_stop():
                break
            if pages_crawled >= max_pages or len(queued_files) >= max_files * 3:
                break
            log(f"\n[智能模式] 第 {depth} 轮跟进，{len(nav_queue)} 个子页面…")
            nav_queue = await _scan_pages(web_crawler, nav_queue, f"智能扫描·第{depth}轮")

    log(f"\n扫描完成，共发现 {len(queued_files)} 个文件链接，开始下载…\n")

    # ── 阶段二：下载文件 ─────────────────────────────────────────────
    from utils.http_utils import get_with_retry
    session = requests.Session()

    for idx, fi in enumerate(queued_files, start=1):
        if should_stop():
            log("已中断下载")
            break
        if wait_if_paused():
            log("已中断下载")
            break
        if downloaded >= max_files:
            log(f"已达最大下载数 {max_files}，停止")
            break

        log(f"[下载 {idx}/{len(queued_files)}] {fi['url']}")

        resp = get_with_retry(
            session, fi["url"],
            download_headers(fi["url"], fi.get("source_page", ""), ua=random_ua()),
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

        if not fi.get("ext"):
            fi["ext"] = _ext_from_url(fi["url"]) or ext_from_content_type(actual_ct)

        real_ext = ext_from_content_type(actual_ct)
        if real_ext and fi.get("ext") != real_ext:
            log(f"  [提示] 扩展名修正 .{fi.get('ext')} → .{real_ext}")
            fi["ext"] = real_ext

        if fi.get("ext") and fi["ext"] not in target_exts:
            skipped += 1
            log(f"  [SKIP] 类型 .{fi['ext']} 不在目标列表")
            continue

        save_result = storage.save_file(content, fi, output_dir, seen_hashes)
        if save_result["saved"]:
            downloaded += 1
            saved_paths.append(save_result["path"])
            log(f"  [OK] → {save_result['path']}")
        else:
            skipped += 1
            log("  [SKIP] 重复文件")

        jitter_sleep(interval * 0.5)

    # ── 阶段三：生成报告（可选） ──────────────────────────────────────
    report_path = None
    if generate_report and (saved_paths or page_contents):
        try:
            from core.report_generator import generate_report as gen_report
            log("\n[报告] 正在用 LLM 生成汇总报告…")
            report_path = await gen_report(
                saved_paths=saved_paths,
                page_contents=page_contents,
                config=config,
                output_dir=output_dir,
                log=log,
            )
            if report_path:
                log(f"[报告] 已生成：{report_path}")
        except Exception as e:
            log(f"[警告] 报告生成失败：{e}")

    # ── 正文 xlsx 汇总（可选） ────────────────────────────────────────
    content_xlsx_path = None
    if save_page_content and "xlsx" in content_formats and content_rows:
        content_xlsx_path = _write_content_xlsx(content_rows, output_dir, log)

    log(f"\n全部完成：下载 {downloaded}，跳过 {skipped}，失败 {failed}")
    return {
        "downloaded":    downloaded,
        "skipped":       skipped,
        "failed":        failed,
        "saved":         saved_paths,
        "report_path":   report_path,
        "page_contents": page_contents,
        "content_xlsx":  content_xlsx_path,
    }
