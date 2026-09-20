"""
doc_processor.py - 文档处理主流水线

零 Qt 依赖，可独立运行，后期可封装为 MCP Server。

两种模式（use_chunking 控制）：
  chunk 模式：提取 → 清洗 → 分段 → 每 chunk 生成 queries → 每条 query 独立成行输出
  直接模式：提取 → 清洗 → 全文截断 → 生成 queries → 每条 query 独立成行输出

输出格式（eval_dataset.xlsx）：query | 文件ID | 文件名 | chunk_id | 页码 | passage
"""

import asyncio
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime

from core.doc_chunker import chunk_document, _extract_markdown_text_from_str, _chunks_from_paragraphs
from utils.error_logger import log_error, error_log_path

_EXECUTOR = ThreadPoolExecutor(max_workers=2)


async def _read_text(path: str, config: dict, log=None) -> str:
    """Read text from .txt/.md or extract via OCR from .pdf/.docx/etc."""
    p = Path(path)
    if p.suffix.lower() in (".txt", ".md", ".markdown"):
        return p.read_text(encoding="utf-8")
    if log:
        log(f"  {p.suffix.upper()} → 提取文本…")
    chunks = await chunk_document(
        path, config, 999_999, 0, config.get("ocr_backend", "auto"), log,
    )
    return "\n\n".join(c.text for c in chunks) if chunks else ""


class _SafeFormatDict(dict):
    """format_map() 使用：未知占位符原样保留而不抛 KeyError。

    示例：
        "{snippet} {unknown}".format_map(_SafeFormatDict(snippet="hi"))
        → "hi {unknown}"
    """
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


# 噪音行：图片占位符、横线分隔符、单行 HTML 注释
_SNIPPET_NOISE = re.compile(
    r'^('
    r'!\[.*?\]\(.*?\)'      # Markdown 图片：![alt](url)
    r'|[-=_*]{3,}'          # 横线：---  ===  ___  ***
    r'|<!--.*?-->'          # HTML 注释（单行）
    r')\s*$'
)


def _extract_snippet(full_text: str, max_chars: int = 1500) -> str:
    """从文档全文提取前 max_chars 个有意义字符。

    逐行跳过噪音行（图片占位符、纯横线等），保留标题、正文段落。
    通用于任意文档类型，无需感知文档结构。
    """
    parts: list[str] = []
    total = 0
    for raw in full_text.splitlines():
        line = raw.strip()
        if not line or _SNIPPET_NOISE.match(line):
            continue
        parts.append(line)
        total += len(line) + 1   # +1 for newline separator
        if total >= max_chars:
            break
    return "\n".join(parts)[:max_chars]


DEFAULT_QUERY_GEN_PROMPT = """\
You are an information retrieval evaluation dataset construction assistant.
Core principle: Start from the document, not from imagination — not "what would users search for", \
but "what queries would retrieve this document".

Based on the document metadata below, generate exactly 3 queries. \
Choose the 3 query types that best fit this document's content from the 5 types defined below. \
Assume users have varied backgrounds: some are beginners, some are practitioners, \
some are ordinary people who stumbled upon a problem.

【5 Query Types — Definition & Examples (using a "C pointers" document as reference)】

▌Keyword — core concepts or terms from the document; very short phrases or single words, \
as if typed into a search box. Abbreviations and incomplete syntax are allowed.
  ✅ Good: "pointer arithmetic", "null pointer", "memory address C"
  ❌ Bad: "explanation of pointers in C programming language" (too formal, not how people type)

▌Question — user arrives with a clear question; complete sentence, contains a question word
  ✅ Good: "what is a pointer?", "how do I avoid null pointer errors?", "pointer vs array difference?"
  ❌ Bad: "Please explain the concept of pointers and their relationship to memory addresses." (too formal)

▌Scenario — describes a use case or goal, not asking about a concept directly
  ✅ Good: "pass large struct between functions", "dynamic memory allocation", "linked list implementation"
  ❌ Bad: "Scenario: I need to use pointers to manage memory in my C program." (not a real search query)

▌Vague/Casual — how users search when they half-remember something; imprecise, ambiguous, colloquial
  ✅ Good: "that thing that points to an address", "why didn't my variable change", "address vs value passing"
  ❌ Bad: "I'm not sure but I think it's something about memory management" (too verbose)

▌Command — user wants to perform a specific action on a document; operational intent
  ✅ Good: "summarize this document", "extract key points", "translate to Chinese", "list the main findings"
  ❌ Bad: "Can you please provide a comprehensive summary of this document for me?" (too polite/verbose)

【Constraints】
- All queries in English
- Do NOT quote words from the filename or title directly (derive from document semantics, not filename)
- Generate exactly 3 queries — no more, no less
- Choose the 3 types that best match this document (not random — pick based on content fit)
- Also infer the document's knowledge domain (e.g., C programming, contract law, cardiology — be specific)

Return a JSON array, each item containing query, type, and domain:
[
  {{"query": "pointer arithmetic", "type": "Keyword", "domain": "C programming"}},
  {{"query": "how do I avoid null pointer errors?", "type": "Question", "domain": "C programming"}},
  {{"query": "pass large struct between functions", "type": "Scenario", "domain": "C programming"}}
]
Return only the JSON array. No explanation, no other content.

Filename: {filename}
Document snippet (first {snippet_chars} chars):
{snippet}"""

DEFAULT_RELEVANCE_PROMPT = """\
You are a relevance assessor for an information retrieval evaluation dataset.
Judge how relevant each passage below is to the given query, using the 1–4 scale.

【Scoring rubric】
4 = Direct answer: the passage directly and completely answers the query
3 = Partial answer: the passage is related and partially answers the query, but not fully
2 = Background: the passage covers the same topic area and has reference value, but does not directly answer
1 = Not relevant: the passage content is unrelated to the query

Query: {query}

Passages:
{chunks}

Return only passages scored 2 or above as a JSON array of id and score, e.g.:
[{{"id": 0, "score": 4}}, {{"id": 2, "score": 3}}, {{"id": 5, "score": 2}}]
If all passages score below 2, return []. No explanation, no other content."""

DEFAULT_GENERAL_QUERY_GEN_PROMPT = """\
你是一个信息检索评测数据集构建助手。
核心原则：盲生成——你看不到任何文档内容，只知道知识库覆盖的领域名称，请纯凭领域知识生成真实用户会搜索的 query。

以下是知识库涵盖的领域列表：
{domains}

请为每个领域生成 {n} 条查询，覆盖三种类型：

▌跨文档型 — 宽泛，能召回该领域多篇文档
  示例（"C语言/编程"领域）：
  - "内存管理"
  - "编程入门"
  - "数据结构基础"

▌泛化型 — 用户只记得大概方向，笼统提问
  示例：
  - "C语言基础"
  - "怎么写程序"
  - "计算机知识"

▌否定/边界型 — 不应被该领域召回的 query（测准度用，正确系统不应召回）
  说明：看起来相关但领域不匹配
  示例（"C语言/编程"领域）：
  - "Java指针"
  - "Python内存管理"

【要求】
- 每个领域生成 {n} 条（跨文档型和泛化型为主，至少 1 条否定/边界型）
- 全部中文
- candidate_files 一律填 []（此阶段不知道文档内容）

返回 JSON 数组，每条包含四个字段：
[
  {{"domain": "C语言/编程", "type": "跨文档型",   "query": "内存管理",   "candidate_files": []}},
  {{"domain": "C语言/编程", "type": "泛化型",     "query": "C语言基础", "candidate_files": []}},
  {{"domain": "C语言/编程", "type": "否定/边界型", "query": "Java指针",  "candidate_files": []}},
  ...
]
只返回 JSON 数组，不要其他内容。"""

DEFAULT_FILE_MATCH_PROMPT = """\
以下是候选文件列表，请判断每个文件与查询的相关程度，给出 1-4 分评分。

【评分标准】
4 = 直接回答：该文件很可能直接包含对查询的完整回答
3 = 部分回答：该文件与查询相关，可能部分回答
2 = 背景相关：该文件涉及查询所属话题，有参考价值，但不直接回答
1 = 不相关：该文件与查询无关

查询：{query}

文件列表（编号 | 文件名: 首行内容）：
{files}

只返回评分 2 分及以上的文件编号和分数（JSON 数组），例如：
[{{"id": 0, "score": 4}}, {{"id": 2, "score": 3}}]
若全部低于 2 分，返回 []。不要解释，不要其他内容。"""

# ── LLM 调用 ─────────────────────────────────────────────────────────

async def _llm_call(prompt: str, config: dict, max_tokens: int = 800,
                    temperature: float = 0.1, log=None) -> str:
    """调用 LLM，对 429 限流和 5xx 服务错误自动指数退避重试。

    重试次数由 config["llm_max_retries"]（默认 3）控制。
    等待时间：5s → 10s → 20s → … 上限 60s。
    其他错误（4xx 非限流、网络断开等）立即抛出，不重试。
    """
    from core.smart_crawler import _build_full_provider, _build_extra_body
    import litellm

    full_provider = _build_full_provider(config)
    api_key     = config.get("llm_api_key", "").strip() or None
    base_url    = config.get("llm_base_url", "").strip() or None
    max_retries = int(config.get("llm_max_retries", 3))

    kwargs = dict(
        model=full_provider,
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    eb = _build_extra_body(config)
    if eb:
        kwargs["extra_body"] = eb
    if base_url:
        kwargs["base_url"] = base_url

    _wlog = log or (lambda _: None)

    for attempt in range(max_retries + 1):
        try:
            resp = await litellm.acompletion(**kwargs)  # type: ignore[misc]
            return resp.choices[0].message.content.strip()  # type: ignore[union-attr]
        except Exception as e:
            err_str  = str(e)
            err_type = type(e).__name__
            # 优先用 isinstance 检测 litellm 明确异常类（精确，不受消息内容干扰）；
            # 字符串匹配作为兜底，以兼容 litellm 版本差异或自定义 provider 包装层。
            is_rate_limit = (
                isinstance(e, litellm.RateLimitError)           # type: ignore[attr-defined]
                or "429" in err_str
                or "rate limit" in err_str.lower()
                or "RateLimit" in err_type
            )
            is_server_err = (
                isinstance(e, (litellm.ServiceUnavailableError,  # type: ignore[attr-defined]
                               litellm.InternalServerError))    # type: ignore[attr-defined]
                or "ServiceUnavailable" in err_type
                or "InternalServer" in err_type
                or re.search(r'\b(500|502|503|504)\b', err_str) is not None  # 词边界，避免误判
            )
            if attempt < max_retries and (is_rate_limit or is_server_err):
                wait = min(5.0 * (2 ** attempt), 60.0)   # 5s → 10s → 20s → … ≤60s
                kind = "429 限流" if is_rate_limit else "服务错误"
                _wlog(f"    [RETRY {attempt + 1}/{max_retries}] {kind}，等待 {wait:.0f}s 后重试…")
                await asyncio.sleep(wait)
            else:
                raise
    raise RuntimeError("LLM 调用失败（已超过最大重试次数）")  # 逻辑上不可达


def _extract_json_array(raw: str, greedy: bool = True) -> list | None:
    """从 LLM 响应中提取第一个 JSON 数组，解析失败返回 None。

    greedy=True  使用贪心匹配（适合对象数组，内含嵌套方括号）
    greedy=False 使用非贪心匹配（适合纯字符串/数字数组）
    """
    pattern = r'\[[\s\S]*\]' if greedy else r'\[[\s\S]*?\]'
    m = re.search(pattern, raw)
    if m:
        try:
            result = json.loads(m.group(0))
            if isinstance(result, list):
                return result
        except Exception:
            pass
    return None


def _parse_json_list(raw: str) -> list[str]:
    result = _extract_json_array(raw, greedy=False)
    if result is not None:
        return [str(x).strip() for x in result if str(x).strip()]
    lines = [l.strip().lstrip('"-•·').strip() for l in raw.splitlines() if l.strip()]
    return [l for l in lines if len(l) > 5][:3]


def _parse_query_list(raw: str) -> list[dict]:
    """解析 Stage3 结构化格式：[{"query":..., "type":..., "domain":...}, ...]

    兼容旧纯字符串数组格式：若元素为 str，则 type/domain 补空字符串。
    返回 list[dict]，每条保证含 "query" / "type" / "domain" 三个键。
    """
    result = _extract_json_array(raw, greedy=True)
    if result is not None:
        out: list[dict] = []
        for item in result:
            if isinstance(item, dict) and item.get("query", "").strip():
                out.append({
                    "query":  str(item.get("query", "")).strip(),
                    "type":   str(item.get("type", "")).strip(),
                    "domain": str(item.get("domain", "")).strip(),
                })
            elif isinstance(item, str) and item.strip():
                # 兼容旧纯字符串格式
                out.append({"query": item.strip(), "type": "", "domain": ""})
        return out
    # fallback：按行分割，当纯字符串处理
    lines = [l.strip().lstrip('"-•·').strip() for l in raw.splitlines() if l.strip()]
    return [{"query": l, "type": "", "domain": ""} for l in lines if len(l) > 5][:12]


def _parse_relevance_scores(raw: str) -> list[dict]:
    """解析相关性格式：[{"id": 0, "score": 4}, ...]

    要求显式 score：缺 score 的 dict、非 dict（含旧纯编号 [0,2,5]）一律跳过
    （fail-closed，不再发明默认 score=3）。返回 list[dict]，每条含 id/score。
    score >= 2 等价于原 relevant=true（阈值语义由调用方保持不变）。
    """
    result = _extract_json_array(raw, greedy=True)
    if result is not None:
        out: list[dict] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            if "score" not in item:
                continue
            try:
                idx   = int(item.get("id", item.get("idx", -1)))
                score = int(item["score"])
                if idx >= 0 and 1 <= score <= 4:
                    out.append({"id": idx, "score": score})
            except (ValueError, TypeError):
                pass
        return out
    return []

def _write_excel_sync(
    eval_rows: list[dict],
    query_file_order: list[tuple],
    output_dir: str,
    log=None,
) -> str:
    """指定文档问答 eval 写入：一行一个 (query, file)，chunk 横向展开为列对。

    固定列：文件ID | 文件名 | Query | Query类型 | Query领域
    动态列（按 LLM评分降序）：段落文本N | LLM评分N | 人工标注N

    query_file_order 决定行顺序并保证 0-match (query, file) 组合也出现在输出中（段落列留空）。
    """
    import openpyxl
    from collections import defaultdict

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ── 按 (query, file_id, filename, query_type, query_domain) 聚合 chunks ──
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in eval_rows:
        key = (
            row.get("query", ""),
            row.get("file_id", ""),
            row.get("filename", ""),
            row.get("query_type", ""),
            row.get("query_domain", ""),
        )
        groups[key].append({
            "passage":   row.get("passage", ""),
            "chunk_id":  row.get("chunk_id", ""),
            "page":      row.get("page", ""),
            "llm_score": row.get("llm_score", 0),
        })

    # 每组内按 llm_score 降序
    for key in groups:
        groups[key].sort(key=lambda c: c["llm_score"], reverse=True)

    max_chunks = max((len(v) for v in groups.values()), default=0)

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "eval_dataset"

    # 表头
    fixed = ["文件ID", "文件名", "Query", "Query类型", "Query领域", "匹配Chunk数"]
    dynamic: list[str] = []
    for i in range(1, max_chunks + 1):
        dynamic += [f"段落文本{i}", f"LLM评分{i}", f"人工标注{i}"]
    ws.append(fixed + dynamic)
    _style_header(ws)

    # 数据行：以 query_file_order 为权威顺序，0-match 组合仍写入（段落列留空）
    _wlog = log or (lambda _: None)
    skipped_rows = 0
    for (query, file_id, filename, qtype, qdomain) in query_file_order:
        chunks = groups.get((query, file_id, filename, qtype, qdomain), [])
        row_data: list = [
            _sanitize_cell(file_id,     "file_id",      _wlog),
            _sanitize_cell(filename,    "filename",     _wlog),
            _sanitize_cell(query[:500], "query",        _wlog),
            _sanitize_cell(qtype,       "query_type",   _wlog),
            _sanitize_cell(qdomain,     "query_domain", _wlog),
            len(chunks),
        ]
        for chunk in chunks:
            row_data += [
                _sanitize_cell(chunk["passage"][:2000], "passage", _wlog),
                chunk["llm_score"],
                "",
            ]
        try:
            ws.append(row_data)
        except Exception as _row_err:
            skipped_rows += 1
            # 极端情况（sanitize 未能覆盖的字符）：跳过该行，不让整个文件崩掉
            _wlog(f"  [WARN][Excel] 跳过一行: query={query[:60]!r} file={filename!r} err={_row_err}")
    if skipped_rows:
        _wlog(f"  [WARN][Excel] 共跳过 {skipped_rows} 行（字符不合法），其余数据已正常写入")

    _auto_width(ws)
    eval_path = str(out / f"{ts}_eval.xlsx")
    wb.save(eval_path)
    return eval_path


async def _write_excel(
    eval_rows: list[dict],
    query_file_order: list[tuple],
    output_dir: str,
    log=None,
) -> str:
    """把同步写入扔到线程池，不阻塞事件循环。"""
    import functools
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _EXECUTOR,
        functools.partial(_write_excel_sync, eval_rows, query_file_order, output_dir, log),
    )


def _write_file_match_excel_sync(
    eval_rows: list[dict],
    query_order: list[tuple],
    output_dir: str,
    log=None,
) -> str:
    """通用问答 eval 写入：一行一个 query，匹配文件横向展开为列对。

    固定列：Query | Query类型 | Query领域
    动态列（按 LLM评分降序）：文件名N | LLM评分N | 人工标注N

    query_order 决定行顺序并保证 0-match query 也出现在输出中（文件列为空）。
    """
    import openpyxl
    from collections import defaultdict

    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ── 按 (query, query_type, query_domain) 聚合匹配文件 ────────────
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in eval_rows:
        key = (
            row.get("query", ""),
            row.get("query_type", ""),
            row.get("query_domain", ""),
        )
        groups[key].append({
            "filename":  row.get("filename", ""),
            "llm_score": row.get("llm_score", 0),
        })

    # 每组内按 llm_score 降序
    for key in groups:
        groups[key].sort(key=lambda f: f["llm_score"], reverse=True)

    max_files = max((len(v) for v in groups.values()), default=0)

    wb  = openpyxl.Workbook()
    ws  = wb.active
    assert ws is not None
    ws.title = "general_eval"

    # 表头
    fixed = ["Query", "Query类型", "Query领域", "匹配文件数"]
    dynamic: list[str] = []
    for i in range(1, max_files + 1):
        dynamic += [f"文件名{i}", f"LLM评分{i}", f"人工标注{i}"]
    ws.append(fixed + dynamic)
    _style_header(ws)

    # 数据行：以 query_order 为权威顺序，0-match query 仍写入（文件列留空）
    _wlog = log or (lambda _: None)
    skipped_rows = 0
    for (query, q_type, domain) in query_order:
        files    = groups.get((query, q_type, domain), [])
        row_data: list = [
            _sanitize_cell(query[:500], "query",        _wlog),
            _sanitize_cell(q_type,      "query_type",   _wlog),
            _sanitize_cell(domain,      "query_domain", _wlog),
            len(files),
        ]
        for f in files:
            row_data += [
                _sanitize_cell(f["filename"], "filename", _wlog),
                f["llm_score"],
                "",
            ]
        try:
            ws.append(row_data)
        except Exception as _row_err:
            skipped_rows += 1
            _wlog(f"  [WARN][Excel] 跳过一行: query={query[:60]!r} err={_row_err}")
    if skipped_rows:
        _wlog(f"  [WARN][Excel] 共跳过 {skipped_rows} 行（字符不合法），其余数据已正常写入")

    _auto_width(ws)
    path = str(out / f"{ts}_general_eval.xlsx")
    wb.save(path)
    return path


async def _write_file_match_excel(
    eval_rows: list[dict],
    query_order: list[tuple],
    output_dir: str,
    log=None,
) -> str:
    import functools
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _EXECUTOR,
        functools.partial(_write_file_match_excel_sync, eval_rows, query_order, output_dir, log),
    )


# XML 1.0 非法控制字符：\x00-\x08 \x0b \x0c \x0e-\x1f（openpyxl 会拒绝写入）
_ILLEGAL_XML_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _sanitize_cell(value: object, field: str = "", log=None) -> object:
    """将字符串中 XML 1.0 非法控制字符替换为空格。
    非字符串类型原样返回；若有替换则通过 log 发出 WARN。"""
    if not isinstance(value, str):
        return value
    cleaned = _ILLEGAL_XML_CHARS.sub(' ', value)
    if cleaned != value and log:
        log(f"  [WARN] 字段 {field!r} 含非法控制字符，已替换为空格")
    return cleaned


def _style_header(ws) -> None:
    from openpyxl.styles import Font, PatternFill, Alignment
    fill = PatternFill("solid", fgColor="4472C4")
    font = Font(bold=True, color="FFFFFF")
    for cell in ws[1]:
        cell.font = font
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")


def _auto_width(ws) -> None:
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 60)

# ════════════════════════════════════════════════════════════════════════
# 三阶段独立 API
# ════════════════════════════════════════════════════════════════════════

def _make_stage_helpers(log, stop_checker, pause_checker):
    """返回各阶段通用的三个辅助函数，避免在每个 stage 中重复定义。"""
    def _log(msg):
        if log:
            log(msg)

    def _should_stop() -> bool:
        return bool(stop_checker and stop_checker())

    async def _wait_if_paused() -> None:
        if pause_checker:
            while pause_checker():
                await asyncio.sleep(0.3)

    return _log, _should_stop, _wait_if_paused

async def run_stage1_extract(
    file_paths: list[str],
    config: dict,
    log=None,
    stop_checker=None,
    pause_checker=None,
    progress_callback=None,
) -> dict:
    """
    阶段①：OCR/提取文本。
    输入：原始文档；输出：{base_dir}/extracted/{stem}_extracted.txt
    返回：{"output_dir", "output_files", "processed", "failed"}
    """
    _log, _should_stop, _wait_if_paused = _make_stage_helpers(log, stop_checker, pause_checker)

    ocr_backend = config.get("ocr_backend", "auto")
    output_dir  = Path(config.get("stage1_out_dir", "output/doc_results/extracted"))
    output_dir.mkdir(parents=True, exist_ok=True)
    skip_existing = config.get("skip_existing_files", True)
    max_retries   = int(config.get("max_retries", 2))
    _err_log    = str(error_log_path(str(output_dir)))

    output_files: list[str] = []
    processed = failed = 0
    total = len(file_paths)
    failed_paths: list[str] = []   # 记录失败文件，用于二轮兜底

    # MinerU：PDF 走云端批量解析，非 PDF 走本地提取
    use_mineru = ocr_backend == "mineru_fallback"
    mineru_md: dict[str, str] = {}
    if use_mineru:
        from core.mineru_extractor import extract_files

        mineru_paths = [p for p in file_paths if Path(p).suffix.lower() == ".pdf"]
        if mineru_paths:
            _log(f"\nMinerU 云端解析 {len(mineru_paths)} 个 PDF…")
            save_formats = list(dict.fromkeys(
                ["markdown"] + config.get("mineru_formats", ["markdown"])
            ))
            try:
                results = await extract_files(
                    mineru_paths, config, str(output_dir), save_formats,
                    log=_log, stop_checker=_should_stop,
                    progress_cb=lambda c, t: progress_callback(c, t) if progress_callback else None,
                )
                for p, r in results.items():
                    md = r.get("markdown", "")
                    if md:
                        mineru_md[p] = md
                    elif r.get("error"):
                        _log(f"  [MinerU 失败] {Path(p).name}: {r['error']}")
                        log_error(_err_log, "mineru_extract", p, r["error"])
            except Exception as e:
                _log(f"  [MinerU 批量异常] {e}")
                log_error(_err_log, "mineru_batch", "", str(e))

    for idx, path in enumerate(file_paths, start=1):
        if _should_stop():
            _log("已中断")
            break
        await _wait_if_paused()

        filename = Path(path).name
        _log(f"\n[{idx}/{total}] {filename}")
        
        # 检查是否跳过已存在的文件
        if skip_existing:
            stem = Path(path).stem
            formats = config.get("extract_formats") or ["txt"]
            _EXT = {"txt": ".txt", "md": ".md", "json": ".json"}
            existing_files = []
            
            # 检查 extract_formats 对应的文件
            for fmt in formats:
                ext = _EXT.get(fmt, ".txt")
                check_path = output_dir / f"{stem}_extracted{ext}"
                if check_path.exists() and check_path.stat().st_size > 0:
                    existing_files.append(str(check_path))
            
            # 如果是 MinerU PDF，还要检查 mineru_formats 对应的文件
            if use_mineru and Path(path).suffix.lower() == ".pdf":
                from core.mineru_extractor import _sanitize_stem
                sanitized_stem = _sanitize_stem(stem)
                mineru_fmts = config.get("mineru_formats", ["markdown"])
                _MINERU_EXT = {"markdown": ".md", "txt": ".txt", "json": ".json"}
                for mfmt in mineru_fmts:
                    mext = _MINERU_EXT.get(mfmt, f".{mfmt}")
                    mpath = output_dir / f"{sanitized_stem}{mext}"
                    if mpath.exists() and mpath.stat().st_size > 0:
                        existing_files.append(str(mpath))
            
            # 只要有任意输出文件存在就跳过
            if existing_files:
                _log("  ⏭️ 已存在，跳过")
                output_files.extend(existing_files)
                processed += 1
                if progress_callback:
                    progress_callback(idx, total)
                continue
        
        file_success = False
        for attempt in range(1, max_retries + 2):  # max_retries + 1 次总尝试
            try:
                if use_mineru and path in mineru_md:
                    paragraphs = _extract_markdown_text_from_str(mineru_md[path])
                    chunks = _chunks_from_paragraphs(paragraphs, 999_999, 0)
                    _log(f"  MinerU 提取：{len(chunks)} 段")
                elif use_mineru and Path(path).suffix.lower() == ".pdf":
                    # MinerU 解析失败（如拆分后仍失败），回退到本地 OCR
                    _log(f"  MinerU 解析失败，回退到本地 OCR…")
                    chunks = await chunk_document(path, config, 999_999, 0, ocr_backend, _log)
                    if not chunks:
                        _log("  [SKIP] 本地 OCR 也未提取到内容")
                        log_error(_err_log, "mineru_and_local_both_failed", path, "MinerU 和本地 OCR 均未能提取内容")
                        raise ValueError("MinerU 和本地 OCR 均未提取内容")
                else:
                    chunks = await chunk_document(path, config, 999_999, 0, ocr_backend, _log)
                if not chunks:
                    _log("  [SKIP] 未提取到任何内容")
                    log_error(_err_log, "extract_empty", path, "提取阶段未得到任何内容")
                    raise ValueError("提取阶段未得到任何内容")

                full_text = "\n\n".join(c.text for c in chunks)
                _log(f"  提取完成：{len(full_text)} 字符")

                stem    = Path(path).stem
                # MinerU 模式下，格式文件已由 extract_files() 写入 output_dir，
                # 此处仅需补充写普通 OCR 的纯文本输出；MinerU 成功处理的 PDF 跳过。
                if use_mineru and path in mineru_md:
                    # MinerU 格式文件已保存，收集已写入的文件路径
                    from core.mineru_extractor import _sanitize_stem
                    stem_m = _sanitize_stem(Path(path).stem)
                    mineru_fmts = config.get("mineru_formats", ["markdown"])
                    _MINERU_EXT = {"markdown": ".md", "txt": ".txt", "json": ".json"}
                    for mfmt in mineru_fmts:
                        mext = _MINERU_EXT.get(mfmt, f".{mfmt}")
                        mpath = output_dir / f"{stem_m}{mext}"
                        if mpath.exists() and mpath.stat().st_size > 0:
                            output_files.append(str(mpath))
                else:
                    formats = config.get("extract_formats") or ["txt"]
                    _EXT = {"txt": ".txt", "md": ".md", "json": ".json"}
                    for fmt in formats:
                        ext      = _EXT.get(fmt, ".txt")
                        out_path = output_dir / f"{stem}_extracted{ext}"
                        if fmt == "json":
                            import json as _json
                            out_path.write_text(
                                _json.dumps(
                                    {"filename": filename, "text": full_text},
                                    ensure_ascii=False, indent=2,
                                ),
                                encoding="utf-8",
                            )
                        else:
                            out_path.write_text(full_text, encoding="utf-8")
                        output_files.append(str(out_path))
                        _log(f"  ✓ 已保存：{out_path.name}")
                processed += 1
                file_success = True
                break  # 成功，跳出重试循环

            except Exception as e:
                if attempt <= max_retries:
                    _log(f"  [ERROR] 第 {attempt} 次尝试失败：{e}，3秒后重试（共{max_retries}次重试）…")
                    await asyncio.sleep(3)
                else:
                    _log(f"  [ERROR] 已重试 {max_retries} 次仍失败：{e}")
                    log_error(_err_log, "stage1_extract", path, f"重试{max_retries}次后仍失败: {e}")

        if file_success:
            if progress_callback:
                progress_callback(idx, total)
        else:
            failed += 1
            failed_paths.append(path)
            if progress_callback:
                progress_callback(idx, total)

    # ── 二轮兜底：对仍然失败的文件再尝试一次 ──
    if failed_paths and not _should_stop():
        _log(f"\n═══ 二轮兜底：{len(failed_paths)} 个文件 ═══")
        retry2_failed: list[str] = []
        for ridx, rpath in enumerate(failed_paths, start=1):
            if _should_stop():
                break
            await _wait_if_paused()
            rfilename = Path(rpath).name
            _log(f"\n[兜底 {ridx}/{len(failed_paths)}] {rfilename}")
            try:
                chunks = await chunk_document(rpath, config, 999_999, 0, ocr_backend, _log)
                if not chunks:
                    _log("  [SKIP] 兜底仍未提取到内容")
                    retry2_failed.append(rpath)
                    continue
                full_text = "\n\n".join(c.text for c in chunks)
                _log(f"  兜底提取完成：{len(full_text)} 字符")
                stem = Path(rpath).stem
                formats = config.get("extract_formats") or ["txt"]
                _EXT = {"txt": ".txt", "md": ".md", "json": ".json"}
                for fmt in formats:
                    ext = _EXT.get(fmt, ".txt")
                    out_path = output_dir / f"{stem}_extracted{ext}"
                    if fmt == "json":
                        import json as _json
                        out_path.write_text(
                            _json.dumps(
                                {"filename": Path(rpath).name, "text": full_text},
                                ensure_ascii=False, indent=2,
                            ),
                            encoding="utf-8",
                        )
                    else:
                        out_path.write_text(full_text, encoding="utf-8")
                    output_files.append(str(out_path))
                    _log(f"  ✓ 兜底成功：{out_path.name}")
                processed += 1
                failed -= 1
                if progress_callback:
                    progress_callback(len(file_paths), len(file_paths))
            except Exception as e:
                _log(f"  [ERROR] 兜底也失败：{e}")
                log_error(_err_log, "stage1_retry2", rpath, str(e))
                retry2_failed.append(rpath)

        if retry2_failed:
            _log(f"\n⚠ {len(retry2_failed)} 个文件最终仍失败")
        else:
            _log(f"\n✓ 二轮兜底全部成功")

    _log(f"\n完成：处理 {processed}，失败 {failed}")
    if failed:
        _log(f"失败详情见：{_err_log}")
    return {
        "output_dir":   str(output_dir),
        "output_files": output_files,
        "processed":    processed,
        "failed":       failed,
    }







async def run_stage3_gen_queries(
    file_paths: list[str],
    config: dict,
    log=None,
    stop_checker=None,
    pause_checker=None,
    progress_callback=None,
) -> dict:
    """
    阶段③：Query 生成（仅读元信息，不读文档内容）。
    输入：txt 文件列表；
    输出：{"queries": {filename: [q1,q2,q3]}, "processed", "failed"}

    config["concurrency"] 控制同时飞行的 LLM 请求数（默认 3）。
    """
    _log, _should_stop, _wait_if_paused = _make_stage_helpers(log, stop_checker, pause_checker)

    prompt_tpl = (
        config.get("query_gen_prompt", DEFAULT_QUERY_GEN_PROMPT).strip()
        or DEFAULT_QUERY_GEN_PROMPT
    )
    snippet_chars = int(config.get("query_snippet_chars", 1500))
    stage3_out    = config.get("stage3_out_dir", "output/doc_results/queries").strip()
    concurrency   = int(config.get("concurrency", 3))
    _err_log      = str(error_log_path(stage3_out))

    queries: dict[str, list[dict]] = {}   # filename -> [{"query","type","domain"}, ...]
    # 用列表收 (idx, filename, qs)，保证最终日志 / JSON 顺序稳定
    results_buf: list[tuple[int, str, list[dict] | None]] = []
    total = len(file_paths)

    sem = asyncio.Semaphore(concurrency)
    _log(f"并发数：{concurrency}")

    async def _process_one(idx: int, path: str) -> None:
        async with sem:
            if _should_stop():
                results_buf.append((idx, Path(path).name, None))
                return
            await _wait_if_paused()

            filename = Path(path).name
            _log(f"\n[{idx}/{total}] {filename}")
            try:
                snippet = ""
                try:
                    _full   = await _read_text(path, config)
                    snippet = _extract_snippet(_full, snippet_chars)
                except Exception as e:
                    _log(f"  [WARN] 读取文件失败，以文件名作兜底: {e}")

                if not snippet:
                    snippet = Path(path).stem

                prompt = prompt_tpl.format_map(_SafeFormatDict(
                    filename=Path(path).stem,
                    snippet=snippet,
                    snippet_chars=snippet_chars,
                    title=snippet.splitlines()[0][:200] if snippet else Path(path).stem,
                ))
                _log(f"  片段：{len(snippet)} 字符")
                _log("  LLM 生成 queries…")

                raw = await _llm_call(prompt, config, max_tokens=800, temperature=0.7, log=_log)
                qs  = _parse_query_list(raw)[:12]
                if not qs:
                    _log(f"  [WARN] LLM 未返回有效 query，跳过（原始响应前200字符：{raw[:200]!r}）")
                    results_buf.append((idx, filename, None))
                    return

                for i, q in enumerate(qs, 1):
                    _log(f"  Q{i}[{q['type']}]：{q['query']}")
                if progress_callback:
                    # 以第一条 query 触发进度回调（保持与旧版行为一致）
                    progress_callback(idx, total, row_data={
                        "filename": filename,
                        "query":    qs[0]["query"],
                        "type":     qs[0]["type"],
                        "domain":   qs[0]["domain"],
                    })
                    for q in qs[1:]:
                        progress_callback(idx, total, row_data={
                            "filename": filename,
                            "query":    q["query"],
                            "type":     q["type"],
                            "domain":   q["domain"],
                        })
                results_buf.append((idx, filename, qs))

            except Exception as e:
                _log(f"  [ERROR] {e}")
                log_error(_err_log, "stage3_gen_queries", path, str(e))
                results_buf.append((idx, filename, None))

    tasks = [
        asyncio.create_task(_process_one(i, p))
        for i, p in enumerate(file_paths, start=1)
    ]
    await asyncio.gather(*tasks)

    # 按原始顺序整理结果
    results_buf.sort(key=lambda x: x[0])
    processed = failed = 0
    for _, filename, qs in results_buf:
        if qs is not None:
            queries[filename] = qs
            processed += 1
        else:
            failed += 1

    q_file = ""
    if stage3_out and queries:
        ts_now = datetime.now().strftime("%Y%m%d_%H%M%S")
        q_out  = Path(stage3_out)
        q_out.mkdir(parents=True, exist_ok=True)
        q_file = str(q_out / f"{ts_now}_queries.json")
        Path(q_file).write_text(
            json.dumps(queries, ensure_ascii=False, indent=2), "utf-8"
        )
        _log(f"\nQueries 已保存：{q_file}")
    _log(f"\n完成：处理 {processed}，失败 {failed}")
    return {"queries": queries, "processed": processed, "failed": failed, "queries_file": q_file}


async def run_stage4_match_chunks(
    file_paths: list[str],
    config: dict,
    log=None,
    stop_checker=None,
    pause_checker=None,
    progress_callback=None,
    extra_data: dict | None = None,
) -> dict:
    """
    阶段④：Chunk 匹配。
    输入：txt 文件列表 + extra_data["queries"] = {filename: [q1,q2,q3]}
    逻辑：切 chunk → 每条 query 分批发给 LLM 判断相关性 → 合并
    输出：{"eval_path", "eval_rows", "processed", "failed"}

    config["concurrency"] 控制同时飞行的 LLM 请求数（默认 3）。
    Excel 行序由预先注册的 query_file_order 保证，并发结果写入后再按顺序输出。
    """
    _log, _should_stop, _wait_if_paused = _make_stage_helpers(log, stop_checker, pause_checker)

    queries_map: dict[str, list[dict]] = (extra_data or {}).get("queries", {})
    chunk_size    = int(config.get("chunk_size", 600))
    chunk_overlap = int(config.get("chunk_overlap", 50))
    batch_size    = int(config.get("chunk_batch_size", 20))
    max_chunks    = int(config.get("max_chunks_per_doc", 200))
    output_dir    = config.get("stage4_out_dir", "output/doc_results")
    concurrency   = int(config.get("concurrency", 3))
    _err_log      = str(error_log_path(output_dir))
    prompt_tpl    = (
        config.get("relevance_prompt", DEFAULT_RELEVANCE_PROMPT).strip()
        or DEFAULT_RELEVANCE_PROMPT
    )

    from core.doc_chunker import _chunks_from_paragraphs

    # ── Phase 1: 预注册行顺序（串行扫描，O(N) 极快）────────────────────
    # 必须在并发开始前完成，保证 query_file_order 顺序稳定（按原始文件顺序）
    query_file_order: list[tuple] = []
    seen_qf:         set[tuple]  = set()
    file_id_map:     dict[str, str] = {}   # path -> file_id

    total = len(file_paths)
    for path in file_paths:
        filename = Path(path).name
        qs_dicts = queries_map.get(filename, [])
        fid = uuid.uuid4().hex[:8]
        file_id_map[path] = fid
        for qd in qs_dicts:
            q_ = qd.get("query", "").strip()
            if not q_:
                continue
            key = (q_, fid, filename, qd.get("type", ""), qd.get("domain", ""))
            if key not in seen_qf:
                seen_qf.add(key)
                query_file_order.append(key)

    # ── Phase 2: 并发处理每个文件 ────────────────────────────────────────
    all_rows: list[dict] = []
    processed_flags: dict[str, bool] = {}  # path -> True/False
    sem = asyncio.Semaphore(concurrency)
    _log(f"并发数：{concurrency}")

    async def _process_file(idx: int, path: str) -> None:
        async with sem:
            if _should_stop():
                processed_flags[path] = False
                return
            await _wait_if_paused()

            filename = Path(path).name
            file_id  = file_id_map[path]
            qs_dicts = queries_map.get(filename, [])
            _log(f"\n[{idx}/{total}] {filename}  ({len(qs_dicts)} queries, ID: {file_id})")

            if not qs_dicts:
                _log("  [SKIP] 没有对应的 queries，请先运行第③步")
                processed_flags[path] = False
                return

            try:
                text   = await _read_text(path, config, _log)
                paras  = [(0, p) for p in text.split("\n\n") if p.strip()]
                chunks = _chunks_from_paragraphs(paras, chunk_size, chunk_overlap)
                if not chunks:
                    _log("  [SKIP] 分段后无内容")
                    processed_flags[path] = False
                    return
                _log(f"  切分：{len(chunks)} 个 chunk")
                if len(chunks) > max_chunks:
                    _log(f"  ⚠ chunk 数({len(chunks)})超过上限({max_chunks})，均匀采样 {max_chunks} 个")
                    step = len(chunks) / max_chunks
                    chunks = [chunks[int(i * step)] for i in range(max_chunks)]

                # 对该文件的所有 query 并发发起 LLM 调用
                # 注意：sem 已被本文件占用，query 内部的 LLM 调用不再套 sem，
                # 避免死锁；实际并发上限由 sem 粒度（文件级）控制。
                async def _match_query(qd: dict) -> list[dict]:
                    q        = qd.get("query", "").strip()
                    q_type   = qd.get("type", "")
                    q_domain = qd.get("domain", "")
                    if not q:
                        return []
                    _log(f"  匹配 query：{q[:60]}")
                    relevant_items: list[dict] = []
                    total_batches = (len(chunks) + batch_size - 1) // batch_size
                    for batch_idx, b_start in enumerate(range(0, len(chunks), batch_size), 1):
                        if _should_stop():
                            break
                        if batch_idx % 10 == 1 or batch_idx == total_batches:
                            _log(f"    进度：batch {batch_idx}/{total_batches}")
                        batch = chunks[b_start: b_start + batch_size]
                        chunk_lines = "\n".join(
                            f"[{b_start + i}] {c.text[:300]}"
                            for i, c in enumerate(batch)
                        )
                        prompt = prompt_tpl.format_map(
                            _SafeFormatDict(query=q, chunks=chunk_lines)
                        )
                        try:
                            raw   = await _llm_call(prompt, config, max_tokens=300, temperature=0.0, log=_log)
                            items = _parse_relevance_scores(raw)
                            for item in items:
                                n = item["id"]
                                if b_start <= n < b_start + len(batch):
                                    relevant_items.append(item)
                        except Exception as e:
                            _log(f"    [WARN] batch {b_start} LLM 调用失败：{e}")
                    _log(f"    相关 chunk：{len(relevant_items)} 个")
                    rows: list[dict] = []
                    for item in relevant_items:
                        chunk = chunks[item["id"]]
                        row   = {
                            "query":        q,
                            "query_type":   q_type,
                            "query_domain": q_domain,
                            "file_id":      file_id,
                            "filename":     filename,
                            "chunk_id":     chunk.chunk_id,
                            "page":         chunk.page,
                            "passage":      chunk.text,
                            "llm_score":    item["score"],
                        }
                        rows.append(row)
                        if progress_callback:
                            progress_callback(idx, total, row_data=row)
                    return rows

                query_results = await asyncio.gather(*[_match_query(qd) for qd in qs_dicts])
                for rows in query_results:
                    all_rows.extend(rows)

                processed_flags[path] = True
                _log("  ✓ 完成")

            except Exception as e:
                _log(f"  [ERROR] {e}")
                log_error(_err_log, "stage4_match_chunks", path, str(e))
                processed_flags[path] = False

    tasks = [
        asyncio.create_task(_process_file(i, p))
        for i, p in enumerate(file_paths, start=1)
    ]
    await asyncio.gather(*tasks)

    processed = sum(1 for v in processed_flags.values() if v)
    failed    = sum(1 for v in processed_flags.values() if not v)

    eval_path = ""
    if query_file_order:
        _log("\n正在写入 Excel…")
        try:
            eval_path = await _write_excel(all_rows, query_file_order, output_dir, log)
            _log(f"  eval → {eval_path}（共 {len(all_rows)} 条匹配，{len(query_file_order)} 个 query×file 组合）")
        except Exception as e:
            _log(f"  [Excel 写入失败] {e}")

    _log(f"\n完成：处理 {processed}，失败 {failed}")
    return {
        "eval_path": eval_path,
        "eval_rows": all_rows,
        "processed": processed,
        "failed":    failed,
    }


# ── 通用问答 Query 流水线 ─────────────────────────────────────────────

async def run_stage_general_query_gen(
    file_paths: list[str],
    config: dict,
    log=None,
    stop_checker=None,
    pause_checker=None,
    progress_callback=None,
    extra_data: dict | None = None,
) -> dict:
    """通用Query流水线 Stage B：接收领域名列表，盲生成 query（不读任何文档内容）。
    file_paths 此处传入的是领域名字符串列表（非文件路径），由 UI 层提供。

    config["concurrency"] 控制同时飞行的 LLM 请求数（默认 3）。
    """
    _log, _should_stop, _wait_if_paused = _make_stage_helpers(log, stop_checker, pause_checker)

    # file_paths 在此函数里是领域名列表
    domains: list[str] = [d.strip() for d in file_paths if d.strip()]
    if not domains:
        _log("[ERROR] 未收到领域列表，请在 B 步输入框填写领域名称")
        return {"general_queries": [], "queries_file": "", "processed": 0, "failed": 0}

    queries_per_domain = int(config.get("queries_per_domain", 3))
    stage_out          = config.get("general_query_out_dir", "output/doc_results/general").strip()
    concurrency        = int(config.get("concurrency", 3))
    _err_log           = str(error_log_path(stage_out))
    prompt_tpl         = (
        config.get("general_query_gen_prompt", DEFAULT_GENERAL_QUERY_GEN_PROMPT).strip()
        or DEFAULT_GENERAL_QUERY_GEN_PROMPT
    )

    _TOKENS_PER_QUERY = 60
    _log(f"盲生成 query，共 {len(domains)} 个领域，并发数：{concurrency}")

    # 结果缓冲：(idx, domain, items_or_None)，gather 后按 idx 排序保证顺序
    results_buf: list[tuple[int, str, list[dict] | None]] = []
    sem = asyncio.Semaphore(concurrency)

    async def _gen_domain(idx: int, domain: str) -> None:
        async with sem:
            if _should_stop():
                results_buf.append((idx, domain, None))
                return
            await _wait_if_paused()

            _log(f"  [{idx}/{len(domains)}] {domain}")
            domain_str = f"- {domain}"
            try:
                prompt = prompt_tpl.format(n=queries_per_domain, domains=domain_str)
            except KeyError:
                _log("  [WARN] Prompt 含旧占位符，已回退到默认 Prompt")
                prompt = DEFAULT_GENERAL_QUERY_GEN_PROMPT.format(
                    n=queries_per_domain, domains=domain_str
                )

            domain_max_tokens = queries_per_domain * _TOKENS_PER_QUERY + 500

            _PARSE_RETRIES = 2  # 空响应/解析失败时的额外重试次数
            items: list[dict] = []
            last_raw = ""
            last_exc: Exception | None = None
            for parse_attempt in range(_PARSE_RETRIES + 1):
                if parse_attempt > 0:
                    wait_s = 5 * parse_attempt
                    _log(f"  [RETRY {parse_attempt}/{_PARSE_RETRIES}] [{domain}] 空响应，等待 {wait_s}s 后重试…")
                    await asyncio.sleep(wait_s)
                    if _should_stop():
                        break
                    await _wait_if_paused()
                try:
                    raw = await _llm_call(prompt, config, max_tokens=domain_max_tokens, temperature=0.7, log=_log)
                    last_raw = raw
                    last_exc = None  # 本次 LLM 调用成功，清除之前的异常记录
                    parsed = _extract_json_array(raw, greedy=True) or []
                    cur_items: list[dict] = []
                    for item in parsed:
                        if isinstance(item, dict) and item.get("query", "").strip():
                            cur_items.append({
                                "domain":          str(item.get("domain", "")),
                                "type":            str(item.get("type", "")),
                                "query":           str(item.get("query", "")).strip(),
                                "candidate_files": [str(f) for f in item.get("candidate_files", [])],
                            })
                    if cur_items:
                        items = cur_items
                        break  # 成功，退出重试循环
                    # LLM 返回成功但解析为空，继续下一次重试
                except Exception as e:
                    last_exc = e
                    _log(f"  [ERROR] [{domain}] LLM 调用失败（attempt {parse_attempt + 1}）：{e}")
                    # 若还有重试机会则继续，否则退出循环后统一记录

            if items:
                _log(f"  [{domain}] 生成 {len(items)} 条")
                results_buf.append((idx, domain, items))
            else:
                # 穷尽所有重试后仍失败：打印诊断信息，异常只记一次
                if last_exc is not None:
                    _log(f"  [WARN] [{domain}] 未返回有效 query，最后一次异常：{last_exc}")
                    log_error(_err_log, "general_query_gen", domain, str(last_exc))
                else:
                    _log(f"  [WARN] [{domain}] 未返回有效 query（原始响应前200字符：{last_raw[:200]!r}）")
                results_buf.append((idx, domain, None))

    tasks = [
        asyncio.create_task(_gen_domain(i, d))
        for i, d in enumerate(domains, start=1)
    ]
    await asyncio.gather(*tasks)

    # 按原始顺序整理
    results_buf.sort(key=lambda x: x[0])
    general_queries: list[dict] = []
    failed = 0
    for _, domain, items in results_buf:
        if items is not None:
            general_queries.extend(items)
        else:
            failed += 1

    # 领域分布统计
    domain_counts: dict[str, int] = {}
    for q in general_queries:
        domain_counts[q["domain"]] = domain_counts.get(q["domain"], 0) + 1

    _log(f"\n生成 {len(general_queries)} 条 query，覆盖 {len(domain_counts)} 个领域")
    for domain, cnt in domain_counts.items():
        _log(f"  [{domain}] {cnt} 条")

    for i, q in enumerate(general_queries, start=1):
        _log(f"  [{q['domain']}][{q['type']}] {q['query']}")
        if progress_callback:
            progress_callback(i, len(general_queries), row_data=q)

    queries_file = ""
    if stage_out and general_queries:
        ts_now = datetime.now().strftime("%Y%m%d_%H%M%S")
        out    = Path(stage_out)
        out.mkdir(parents=True, exist_ok=True)
        queries_file = str(out / f"{ts_now}_general_queries.json")
        Path(queries_file).write_text(
            json.dumps(general_queries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _log(f"\n通用 Queries 已保存：{queries_file}")

    return {
        "general_queries": general_queries,
        "queries_file":    queries_file,
        "processed":       len(general_queries),
        "failed":          failed,
    }


async def run_stage_file_match(
    file_paths: list[str],
    config: dict,
    log=None,
    stop_checker=None,
    pause_checker=None,
    progress_callback=None,
    extra_data: dict | None = None,
) -> dict:
    """通用Query流水线 Stage C：文件级别匹配。
    extra_data["general_queries"] = Stage B 产出的 query 列表。
    输出 Excel：query | file_id | filename | domain。

    config["concurrency"] 控制同时飞行的 LLM 请求数（默认 3）。
    Excel 行序由预先注册的 query_order 保证。
    """
    _log, _should_stop, _wait_if_paused = _make_stage_helpers(log, stop_checker, pause_checker)

    general_queries: list[dict] = (extra_data or {}).get("general_queries", [])
    if not general_queries:
        _log("[ERROR] 未收到 query 数据，请先运行 Query 生成步骤")
        return {"eval_path": "", "eval_rows": [], "processed": 0, "failed": 0}

    output_dir      = config.get("general_match_out_dir", "output/doc_results").strip()
    concurrency     = int(config.get("concurrency", 3))
    _err_log        = str(error_log_path(output_dir))
    prompt_tpl      = (
        config.get("file_match_prompt", DEFAULT_FILE_MATCH_PROMPT).strip()
        or DEFAULT_FILE_MATCH_PROMPT
    )
    file_batch_size = int(config.get("file_batch_size", 50))

    # ── Phase 1: 并发读取文件首行元信息 ─────────────────────────────────
    _log(f"读取 {len(file_paths)} 个文件的元信息（并发）…")
    fname_to_first: dict[str, str] = {}
    read_sem = asyncio.Semaphore(concurrency)

    async def _read_first_line(path: str) -> None:
        async with read_sem:
            fname = Path(path).name
            try:
                text = await _read_text(path, config)
                first_line = ""
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        first_line = line[:150]
                        break
                fname_to_first[fname] = first_line or fname
            except Exception as e:
                _log(f"  [WARN] {fname}: {e}")
                fname_to_first[fname] = fname

    await asyncio.gather(*[
        asyncio.create_task(_read_first_line(p)) for p in file_paths
    ])

    # ── Phase 2: 预注册 query 行顺序 ─────────────────────────────────────
    query_order: list[tuple] = []
    seen_q:      set[tuple]  = set()
    for gq in general_queries:
        query  = gq.get("query", "").strip()
        q_type = gq.get("type", "")
        domain = gq.get("domain", "")
        if not query:
            continue
        qkey = (query, q_type, domain)
        if qkey not in seen_q:
            seen_q.add(qkey)
            query_order.append(qkey)

    # ── Phase 3: 并发处理每条 query ──────────────────────────────────────
    all_rows: list[dict] = []
    total    = len(general_queries)
    # 去重后的 query 列表，每条只处理一次
    seen_processed: set[tuple] = set()
    results_buf: list[tuple[int, list[dict], bool]] = []  # (idx, rows, ok)
    sem = asyncio.Semaphore(concurrency)
    _log(f"并发数：{concurrency}")

    async def _match_query(idx: int, gq: dict) -> None:
        async with sem:
            domain = gq.get("domain", "")
            q_type = gq.get("type", "")
            query  = gq.get("query", "").strip()
            cand   = gq.get("candidate_files", [])
            _log(f"\n[{idx}/{total}] [{domain}][{q_type}] {query[:60]}")

            if not query:
                _log("  [SKIP] 空 query，已跳过")
                return

            # 去重：同一 (query, type, domain) 只处理一次
            qkey = (query, q_type, domain)
            if qkey in seen_processed:
                return
            seen_processed.add(qkey)

            if _should_stop():
                return
            await _wait_if_paused()

            if cand:
                candidates = [(fn, fname_to_first[fn]) for fn in cand if fn in fname_to_first]
            else:
                candidates = list(fname_to_first.items())

            if not candidates:
                _log("  [SKIP] 候选文件不在已读取列表中")
                results_buf.append((idx, [], False))
                return

            all_items: list[dict] = []
            for b_start in range(0, len(candidates), file_batch_size):
                if _should_stop():
                    break
                batch_cands = candidates[b_start: b_start + file_batch_size]
                file_lines  = "\n".join(
                    f"[{b_start + i}] {fn}: {fl[:120]}"
                    for i, (fn, fl) in enumerate(batch_cands)
                )
                prompt = prompt_tpl.format(query=query, files=file_lines)
                try:
                    raw   = await _llm_call(prompt, config, max_tokens=800, temperature=0.0, log=_log)
                    items = _parse_relevance_scores(raw)
                    # fail-closed: do not invent score=3 from bare id lists
                    for item in items:
                        n = item["id"]
                        if b_start <= n < b_start + len(batch_cands):
                            all_items.append(item)
                except Exception as e:
                    _log(f"  [WARN] batch {b_start} LLM 调用失败：{e}")
                    log_error(_err_log, "file_match", query[:50], str(e))

            rows: list[dict] = []
            for item in all_items:
                n = item["id"]
                if 0 <= n < len(candidates):
                    fn, _ = candidates[n]
                    row   = {
                        "query":        query,
                        "query_type":   q_type,
                        "query_domain": domain,
                        "filename":     fn,
                        "llm_score":    item["score"],
                    }
                    rows.append(row)
                    if progress_callback:
                        progress_callback(idx, total, row_data=row)
            _log(f"  匹配文件：{len(rows)} 个")
            results_buf.append((idx, rows, True))

    tasks = [
        asyncio.create_task(_match_query(i, gq))
        for i, gq in enumerate(general_queries, start=1)
    ]
    await asyncio.gather(*tasks)

    processed = sum(1 for _, _, ok in results_buf if ok)
    failed    = sum(1 for _, _, ok in results_buf if not ok)
    for _, rows, _ in results_buf:
        all_rows.extend(rows)

    eval_path = ""
    if query_order:
        _log("\n正在写入 Excel…")
        try:
            eval_path = await _write_file_match_excel(all_rows, query_order, output_dir, log)
            _log(f"  eval → {eval_path}（共 {len(all_rows)} 条匹配，{len(query_order)} 个 query）")
        except Exception as e:
            _log(f"  [Excel 写入失败] {e}")

    _log(f"\n完成：处理 {processed}，失败 {failed}")
    return {
        "eval_path": eval_path,
        "eval_rows": all_rows,
        "processed": processed,
        "failed":    failed,
    }
