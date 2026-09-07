"""
report_generator.py - LLM 汇总报告生成

爬取完成后，用 LLM 对已采集的页面内容做自然语言摘要，
与文件统计数据合并，写入 output/reports/YYYYMMDD_HHMMSS_report.md。
"""

import os
from collections import Counter
from datetime import datetime
from pathlib import Path


async def generate_report(
    saved_paths: list[str],
    page_contents: list[str],
    config: dict,
    output_dir: str,
    log=None,
) -> str | None:
    """
    生成汇总报告，返回报告文件路径；失败返回 None。

    Args:
        saved_paths:   已下载文件的本地路径列表
        page_contents: 各页面 fit_markdown 文本列表（来自 smart_crawl）
        config:        爬取配置 dict（需含 llm_provider/llm_model/llm_api_key 等）
        output_dir:    根输出目录
        log:           日志回调
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    # ── 统计区 ────────────────────────────────────────────────────────
    ext_counter: Counter = Counter()
    for p in saved_paths:
        ext = Path(p).suffix.lstrip(".").lower() or "unknown"
        ext_counter[ext] += 1

    start_urls = config.get("start_urls", [])
    site_label = start_urls[0] if start_urls else "（未知站点）"

    # ── LLM 摘要区 ────────────────────────────────────────────────────
    ai_summary = ""
    if page_contents:
        try:
            ai_summary = await _llm_summarize(page_contents, site_label, config, _log)
        except Exception as e:
            _log(f"  [警告] LLM 摘要失败：{e}")
            ai_summary = "（LLM 摘要生成失败，请检查 API Key 和网络连接）"

    # ── 组装 Markdown ─────────────────────────────────────────────────
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    file_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    mode_parts = ["智能模式（LLM 文件发现"]
    if config.get("use_best_first"):
        mode_parts.append("+ BestFirst 爬序")
    mode_str = " ".join(mode_parts) + "）"

    lines = [
        "# 知识采集报告",
        "",
        f"**时间**: {now_str}  ",
        f"**站点**: {site_label}  ",
        f"**模式**: {mode_str}  ",
        f"**下载文件**: {len(saved_paths)} 个  ",
        "",
    ]

    if ai_summary:
        lines += [
            "## AI 摘要",
            "",
            ai_summary,
            "",
        ]

    if ext_counter:
        lines += ["## 文件类型统计", ""]
        lines.append("| 类型 | 数量 |")
        lines.append("|------|------|")
        for ext, count in ext_counter.most_common():
            lines.append(f"| .{ext} | {count} |")
        lines.append("")

    if saved_paths:
        lines += ["## 文件清单", ""]
        by_ext: dict[str, list[str]] = {}
        for p in saved_paths:
            ext = Path(p).suffix.lstrip(".").lower() or "other"
            by_ext.setdefault(ext, []).append(p)
        for ext, paths in sorted(by_ext.items()):
            lines.append(f"### .{ext} ({len(paths)})")
            lines.append("")
            for p in paths:
                lines.append(f"- {Path(p).name}")
            lines.append("")

    report_content = "\n".join(lines)

    # ── 写入文件 ──────────────────────────────────────────────────────
    report_dir = Path(output_dir) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{file_stamp}_report.md"
    report_path.write_text(report_content, encoding="utf-8")

    return str(report_path)


async def _llm_summarize(
    page_contents: list[str],
    site_label: str,
    config: dict,
    log,
) -> str:
    """调用 LLM 对采集到的页面内容生成自然语言摘要。"""
    from crawl4ai.async_configs import LLMConfig

    provider_key = config.get("llm_provider", "openai")
    model        = config.get("llm_model", "gpt-4o-mini")
    api_key      = config.get("llm_api_key", "").strip() or None
    base_url     = config.get("llm_base_url", "").strip() or None
    prov_prefix  = "openai" if provider_key == "custom" else provider_key
    full_provider = f"{prov_prefix}/{model}"

    llm_cfg = LLMConfig(provider=full_provider, api_token=api_key, base_url=base_url)

    # 合并页面内容，限制总长度避免超 token
    combined = "\n\n---\n\n".join(page_contents)
    max_chars = 12000
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n\n…（内容已截断）"

    prompt = (
        f"以下是从 {site_label} 采集到的多个页面内容摘要：\n\n"
        f"{combined}\n\n"
        "请用中文回答以下问题（共 3-5 段）：\n"
        "1. 这个网站/页面主要提供什么类型的资料或内容？\n"
        "2. 最有价值或最常见的资源是什么？\n"
        "3. 这些内容适合哪类用户或使用场景？\n"
        "请直接给出摘要段落，不要使用编号或标题。"
    )

    log("  正在调用 LLM 生成摘要…")

    # 使用 litellm（crawl4ai 内置依赖）发送请求
    try:
        import litellm
        from core.smart_crawler import _build_extra_body
        kwargs = dict(
            model=full_provider,
            messages=[{"role": "user", "content": prompt}],
            api_key=api_key,
            max_tokens=800,
            temperature=0.3,
        )
        eb = _build_extra_body(config)
        if eb:
            kwargs["extra_body"] = eb
        if base_url:
            kwargs["base_url"] = base_url
        resp = await litellm.acompletion(**kwargs)
        return resp.choices[0].message.content.strip()
    except ImportError:
        # 降级：尝试直接用 openai/anthropic 客户端
        return await _fallback_llm_call(full_provider, prompt, api_key, base_url, config, log)


async def _fallback_llm_call(
    full_provider: str,
    prompt: str,
    api_key: str | None,
    base_url: str | None,
    config: dict,
    log,
) -> str:
    """litellm 不可用时的降级处理：直接调用 OpenAI / Anthropic SDK。"""
    provider_key = config.get("llm_provider", "openai")

    if provider_key in ("openai", "custom", "ollama", "deepseek"):
        try:
            import openai
            client = openai.AsyncOpenAI(
                api_key=api_key or "no-key",
                base_url=base_url or None,
            )
            resp = await client.chat.completions.create(
                model=config.get("llm_model", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
                temperature=0.3,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            raise RuntimeError(f"OpenAI 兼容接口调用失败：{e}") from e

    if provider_key == "anthropic":
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=api_key)
            resp = await client.messages.create(
                model=config.get("llm_model", "claude-haiku-4-5-20251001"),
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            raise RuntimeError(f"Anthropic 调用失败：{e}") from e

    raise RuntimeError(f"不支持的 provider：{provider_key}，请安装 litellm")
