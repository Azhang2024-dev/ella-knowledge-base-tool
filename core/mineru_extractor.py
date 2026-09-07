"""
mineru_extractor.py - MinerU 提取编排层
将本地文件批量提交 MinerU API，下载并保存指定格式的结果。
同时返回每个文件的 Markdown 文本，供后续切段管道使用。
对外只暴露一个函数：extract_files()
"""

import asyncio
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.mineru_api import request_upload_urls, upload_file, poll_batch
from utils.error_logger import log_error, error_log_path
from utils.file_utils import get_pdf_page_count, split_pdf

_EXECUTOR = ThreadPoolExecutor(max_workers=4)

_HTML_SUFFIXES = frozenset((".html", ".htm"))

# 单批最多提交文件数，超过则分批申请上传链接，降低触发 429 限流的概率
_BATCH_SIZE = 20

# PDF 页数上限（MinerU API 硬限制 200 页，超过则自动拆分）
_MAX_PDF_PAGES = 200

# 每批总页数上限（与 _BATCH_SIZE 双约束，防止拆分件把单批负载撑爆）
_BATCH_PAGE_BUDGET = 800

# MinerU ZIP 内各格式对应的文件后缀
_FORMAT_EXT = {
    "markdown": ".md",
    "json":     ".json",
    "docx":     ".docx",
    "html":     ".html",
    "latex":    ".tex",
}


def _is_html(path: str) -> bool:
    return Path(path).suffix.lower() in _HTML_SUFFIXES


# Windows 文件名非法字符及替换映射
_WIN_INVALID = '<>:"/\\|?*'


def _sanitize_stem(stem: str) -> str:
    """清理文件名（stem）：替换 Windows 非法字符，去首尾空格/点，限长避免超 MAX_PATH。"""
    cleaned = []
    for ch in stem:
        cleaned.append("_" if ch in _WIN_INVALID else ch)
    name = "".join(cleaned).strip().rstrip(".")
    if not name:
        name = "unnamed"
    # 限制 stem 长度，给后缀和目录留出空间，避免触发 Windows MAX_PATH(260)
    return name[:120]


async def _in_executor(fn):
    """在线程池中运行无参可调用（配合 lambda 使用）。"""
    return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, fn)


def _download_bytes(url: str) -> bytes:
    import requests
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.content


def _unpack_zip(zip_bytes: bytes, formats: list[str]) -> dict[str, str]:
    """解包 ZIP，按 formats 提取内容，返回 {format: text}。"""
    out = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for fmt in formats:
            ext  = _FORMAT_EXT.get(fmt)
            name = next((n for n in zf.namelist() if n.endswith(ext)), None) if ext else None
            if name:
                out[fmt] = zf.read(name).decode("utf-8", errors="replace")
    return out


def _estimate_pages(path, known):
    """估算单个文件的页数，用于分批时的负载均衡。"""
    if path in known:
        return known[path]
    if Path(path).suffix.lower() == ".pdf":
        n = get_pdf_page_count(path)
        return n if n > 0 else 200
    return 1


def _build_batches(paths, known_pages):
    """
    按文件数和总页数双约束分批，同一 PDF 的拆分件 round-robin 分散到不同批次。
    """
    regular = [p for p in paths if "_part" not in Path(p).stem]
    parts = [p for p in paths if "_part" in Path(p).stem]

    parts_sorted = sorted(parts, key=lambda p: Path(p).name)
    interleaved = []
    if parts_sorted:
        slot_count = max(len(regular), 1)
        stride = max(slot_count // (len(parts_sorted) + 1), 1)
        pi = 0
        for i, rp in enumerate(regular):
            interleaved.append(rp)
            if pi < len(parts_sorted) and (i + 1) % stride == 0:
                interleaved.append(parts_sorted[pi])
                pi += 1
        interleaved.extend(parts_sorted[pi:])
    else:
        interleaved = regular

    batches = []
    cur = []
    cur_pages = 0
    for p in interleaved:
        pg = _estimate_pages(p, known_pages)
        if cur and (len(cur) >= _BATCH_SIZE or cur_pages + pg > _BATCH_PAGE_BUDGET):
            batches.append(cur)
            cur = []
            cur_pages = 0
        cur.append(p)
        cur_pages += pg
    if cur:
        batches.append(cur)
    return batches


async def extract_files(
    file_paths: list[str],
    config: dict,
    output_dir: str,
    save_formats: list[str],
    log=None,
    stop_checker=None,
    progress_cb=None,
) -> dict[str, dict]:
    """
    通过 MinerU API 批量提取本地文件。
    若 PDF 页数超过 _MAX_PDF_PAGES（200），自动拆分为子文件分别提交，
    最后将各子文件的 Markdown 合并回原始文件路径。

    Args:
        save_formats: 要保存到磁盘的格式列表，如 ["markdown", "json"]。
                      Markdown 始终会从 ZIP 中读取供切段用，
                      但只在 save_formats 包含 "markdown" 时才写入磁盘。
    Returns:
        {
            file_path: {
                "markdown": str,          # 始终存在，供切段管道使用
                "json":     str,          # 若 save_formats 包含 json
                ...
                "saved":    {fmt: path},  # 已写入磁盘的格式 -> 绝对路径
            }
        }
    """
    def _log(m):
        if log: log(m)

    token          = config.get("mineru_token", "").strip()
    model_version  = config.get("mineru_model_version", "vlm")
    enable_formula = bool(config.get("mineru_enable_formula", True))
    enable_table   = bool(config.get("mineru_enable_table",   True))
    language       = config.get("mineru_language", "ch")
    extra_formats  = [f for f in save_formats if f in ("docx", "html", "latex")]

    # 从 ZIP 中始终取 markdown；其余按 save_formats 决定
    fetch_formats = list(dict.fromkeys(["markdown"] + save_formats))

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    err_log = str(error_log_path(out_dir))

    results: dict[str, dict] = {}

    # ── PDF 页数预检查：超限的自动拆分 ──
    expanded_paths: list[str] = []
    split_map: dict[str, list[str]] = {}  # original_path -> [part_paths]
    for p in file_paths:
        if Path(p).suffix.lower() == ".pdf":
            try:
                page_count = get_pdf_page_count(p)
                if page_count > _MAX_PDF_PAGES:
                    parts = split_pdf(p, max_pages=_MAX_PDF_PAGES)
                    if len(parts) > 1:
                        split_map[p] = parts
                        expanded_paths.extend(parts)
                        _log(f"  [自动拆分] {Path(p).name}: {page_count} 页 -> {len(parts)} 个子文件")
                        continue
            except Exception as e:
                _log(f"  [拆分检查失败] {Path(p).name}: {e}，将原样提交")
        expanded_paths.append(p)


    # 智能分批
    known_pages = {}
    all_split_parts = set()
    for parts in split_map.values():
        all_split_parts.update(parts)
    for p in expanded_paths:
        if p in all_split_parts:
            known_pages[p] = _MAX_PDF_PAGES
        elif Path(p).suffix.lower() == ".pdf":
            n = get_pdf_page_count(p)
            known_pages[p] = n if n > 0 else 200
        else:
            known_pages[p] = 1

    # HTML 文件必须单独一批
    groups = [
        ([p for p in expanded_paths if not _is_html(p)], model_version),
        ([p for p in expanded_paths if _is_html(p)],     "MinerU-HTML"),
    ]
    for group_paths, mv in groups:
        if not group_paths or (stop_checker and stop_checker()):
            continue
        batches = _build_batches(group_paths, known_pages)
        _log("  分批计划：" + str(len(batches)) + " 批，文件数 " + str([len(b) for b in batches]))
        await _process_group(
            batches, mv, extra_formats,
            enable_formula, enable_table, language, token,
            fetch_formats, save_formats, out_dir,
            results, _log, stop_checker, progress_cb, len(file_paths),
            err_log,
        )

    # ── 合并拆分结果：将子文件的 Markdown 拼接回原始文件 ──
    for orig_path, parts in split_map.items():
        part_results = [results.get(p, {}) for p in parts]
        all_failed = all(r.get("error") for r in part_results)

        if all_failed and not any(r.get("markdown") for r in part_results):
            # 所有子文件全部失败，记录第一个错误
            first_err = next(r["error"] for r in part_results if r.get("error"))
            results[orig_path] = {"error": first_err, "markdown": ""}
            log_error(err_log, "mineru_split_failed", orig_path, first_err)
            # 移除子文件条目
            for p in parts:
                results.pop(p, None)
            continue

        # 按顺序合并 markdown
        md_parts = []
        for i, r in enumerate(part_results):
            md = r.get("markdown", "")
            if md:
                md_parts.append(f"<!-- Part {i+1} -->\n\n{md}")
        combined_md = "\n\n".join(md_parts)

        # 以第一个成功的 part 为基础构建 result（保留 saved 等字段）
        first_ok = next((r for r in part_results if not r.get("error")), None)
        merged = dict(first_ok) if first_ok else {}
        merged["markdown"] = combined_md

        # 保存合并后的 markdown 到磁盘（覆盖拆分的子文件 .md）
        if combined_md and "markdown" in save_formats:
            stem = Path(orig_path).stem
            dest = out_dir / f"{_sanitize_stem(stem)}.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(combined_md, encoding="utf-8")
            if "saved" not in merged:
                merged["saved"] = {}
            merged["saved"]["markdown"] = str(dest)

        results[orig_path] = merged
        _log(f"  [合并完成] {Path(orig_path).name}: {len(parts)} 个子文件 -> {len(combined_md)} 字符")

        # 从 results 中移除子文件条目，并清理拆分产生的临时 .md 文件
        for p in parts:
            results.pop(p, None)
            part_stem = _sanitize_stem(Path(p).stem)
            part_md = out_dir / f"{part_stem}.md"
            if part_md.exists():
                try:
                    part_md.unlink()
                except Exception:
                    pass

    return results


async def _process_group(
    batches, model_version, extra_formats,
    enable_formula, enable_table, language, token,
    fetch_formats, save_formats, out_dir,
    results, log, stop_checker, progress_cb, total, err_log,
):
    for batch_paths in batches:
        if stop_checker and stop_checker():
            break
        try:
            await _process_batch(
                batch_paths, model_version, extra_formats,
                enable_formula, enable_table, language, token,
                fetch_formats, save_formats, out_dir,
                results, log, stop_checker, progress_cb, total, err_log,
            )
        except InterruptedError:
            raise
        except Exception as e:
            log("  [批次失败，跳过 " + str(len(batch_paths)) + " 个文件] " + str(e))
            for p in batch_paths:
                if p not in results:
                    results[p] = {"error": str(e), "markdown": ""}
                    log_error(err_log, "mineru_batch", p, str(e))
            if progress_cb:
                for p in batch_paths:
                    progress_cb(0, 0)


async def _process_batch(
    paths, model_version, extra_formats,
    enable_formula, enable_table, language, token,
    fetch_formats, save_formats, out_dir,
    results, log, stop_checker, progress_cb, total, err_log,
):
    log(f"  申请上传链接（{len(paths)} 个文件，model={model_version}）…")
    files_meta = [{"name": Path(p).name} for p in paths]

    batch_id, presigned_urls = await _in_executor(lambda: request_upload_urls(
        files_meta, model_version, extra_formats,
        enable_formula, enable_table, language, token,
    ))
    log(f"  batch_id: {batch_id}")

    if len(presigned_urls) != len(paths):
        raise RuntimeError(
            f"MinerU 返回的上传链接数（{len(presigned_urls)}）"
            f"与提交文件数（{len(paths)}）不一致"
        )

    # 并发上传
    log(f"  上传中（{len(paths)} 个）…")
    upload_tasks = [
        _in_executor(lambda u=url, p=path: upload_file(u, p))
        for url, path in zip(presigned_urls, paths)
    ]
    upload_results = await asyncio.gather(*upload_tasks, return_exceptions=True)
    for path, res in zip(paths, upload_results):
        if isinstance(res, Exception):
            log(f"  [上传失败] {Path(path).name}: {res}")
            log_error(err_log, "mineru_upload", path, str(res))
        else:
            log(f"  [已上传]   {Path(path).name}")

    # 轮询
    log("  等待 MinerU 解析…")
    poll_results = await _in_executor(
        lambda: poll_batch(batch_id, token, stop_checker, log)
    )

    # 下载 ZIP 并按格式解包保存
    current = len(results)

    # 按 file_name 匹配结果，不假设返回顺序与提交顺序一致
    name_to_path = {Path(p).name: p for p in paths}
    handled_paths = set()

    for res in poll_results:
        fname = res.get("file_name", "")
        path = name_to_path.get(fname)
        if path is None:
            log("  [警告] MinerU 返回未知文件: " + fname)
            continue
        handled_paths.add(path)
        stem = Path(path).stem

        if res.get("state") != "done":
            err_msg = res.get("err_msg", "failed")
            log("  [解析失败] " + Path(path).name + ": " + str(err_msg))
            results[path] = {"error": err_msg, "markdown": ""}
            log_error(err_log, "mineru_parse", path, str(err_msg))
            current += 1
            if progress_cb:
                progress_cb(current, total)
            continue

        try:
            zip_bytes = await _in_executor(lambda u=res["full_zip_url"]: _download_bytes(u))
            content = _unpack_zip(zip_bytes, fetch_formats)

            saved = {}
            for fmt in save_formats:
                if fmt in content:
                    ext = _FORMAT_EXT.get(fmt, "." + fmt)
                    dest = out_dir / (_sanitize_stem(stem) + ext)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content[fmt], encoding="utf-8")
                    saved[fmt] = str(dest)

            results[path] = {**content, "saved": saved}
            log("  ✓ " + Path(path).name + "  保存：" + str(list(saved.values())))
        except Exception as e:
            log("  [保存失败] " + Path(path).name + ": " + str(e))
            results[path] = {"error": str(e), "markdown": ""}
            log_error(err_log, "mineru_save", path, str(e))

        current += 1
        if progress_cb:
            progress_cb(current, total)

    # 处理超时未返回的文件
    for path in paths:
        if path in handled_paths:
            continue
        results[path] = {"error": "MinerU 超时未返回结果", "markdown": ""}
        log_error(err_log, "mineru_timeout", path, "超时未返回结果")
        current += 1
        if progress_cb:
            progress_cb(current, total)
        log("  [超时跳过] " + Path(path).name)
