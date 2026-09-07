"""
doc_cleaner.py - 元信息清除

支持多种文档类型的元信息识别与清除：
  - 学术论文：DOI / ISSN / 作者 / 机构 / 投稿日期 / 基金 / 版权
  - 课件：讲次 / 主讲人 / 课程编号 / 制作单位
  - 新闻稿：发稿时间 / 责任编辑 / 来源 / 稿件编号
  - 通用：页眉页脚 / 页码 / confidential 声明

clean_mode:
  "rule"  — 纯正则，快速
  "llm"   — LLM识别首尾边界，精准
  "both"  — 规则先跑，LLM二次处理边界区域
"""

import re

# ── 元信息正则规则（顺序无关，全文匹配） ─────────────────────────────
_RULES: list[tuple[str, str]] = [
    # ── 学术 ──────────────────────────────────────────────────────────
    ("doi",        r'(?i)\bDOI\s*[:\-]?\s*10\.\d{4,}/\S+'),
    ("issn",       r'(?i)\bISSN\s*[:\-]?\s*[\dX]{4}-[\dX]{4}'),
    ("isbn",       r'(?i)\bISBN\s*[:\-]?\s*[\d\-X]{10,}'),
    ("received",   r'(?i)(Received|Accepted|Published|Revised)\s*[:\-]?\s*\d{1,2}\s+\w+\s+\d{4}'),
    ("fund",       r'(?i)(Fund|Grant|Project\s+No|基金|资助|支持)\s*[:\-：]?\s*[\w\-()（）,，\s]{4,60}'),
    ("copyright",  r'(?i)(Copyright|©|All\s+rights\s+reserved|版权所有)[^\n]{0,80}'),
    ("author_kw",  r'(?i)^(Author[s]?|通讯作者|Corresponding\s+author|作者)\s*[:\-：].*$'),
    ("affil",      r'(?i)^.*(University|Institute|Department|College|School|Laboratory|研究院|大学|学院|研究所|实验室)[^\n]{0,60}$'),
    # ── 课件 ──────────────────────────────────────────────────────────
    ("lecture",    r'(?i)(第\s*\d+\s*讲|Lecture\s+\d+|课程编号|主讲人|制作单位|学\s*时)[^\n]{0,60}'),
    # ── 新闻稿 ────────────────────────────────────────────────────────
    ("news_meta",  r'(?i)(发稿时间|责任编辑|来\s*源\s*[：:]\s*\S+|稿件编号|字数[：:]\s*\d+)'),
    # ── 通用页眉页脚 ──────────────────────────────────────────────────
    ("page_num",   r'(?i)(第\s*\d+\s*页|Page\s+\d+\s*(of\s*\d+)?|\-\s*\d+\s*\-)'),
    ("confidential", r'(?i)(Confidential|Internal\s+Use|For\s+Internal\s+Distribution|内部资料|保密)[^\n]{0,40}'),
    ("filepath",   r'[A-Za-z]:\\[^\n]{5,80}\.\w{2,5}'),
    # ── 学术数据库下载标记（OUP / JSTOR / Springer 等页脚） ───────────────
    ("dl_notice",  r'(?i)Downloaded\s+from\s+https?://\S+'),
    # ── 期刊页眉：Author | 页码  或  页码 | 标题 ──────────────────────────
    ("jnl_hdr1",   r'^\s*.{1,80}\s*\|\s*\d{1,4}\s*$'),
    ("jnl_hdr2",   r'^\s*\d{1,4}\s*\|\s*.{1,80}$'),
    # ── 报纸版头（如：北京商报/2025 年/5 月/14 日/第004 版 商业） ──────────────
    ("news_hdr",    r'^\S{2,10}/\d{4}\s*年/\d{1,2}\s*月/\d{1,2}\s*日'),
    ("edition_hdr", r'^\s*第\s*\d{1,4}\s*版\s*\S{0,8}\s*$'),
]

_COMPILED: list[tuple[str, re.Pattern]] = [
    (name, re.compile(pat, re.MULTILINE)) for name, pat in _RULES
]


def clean_by_rule(text: str) -> str:
    """
    用正则规则清除元信息，返回清洗后的文本。
    对每个命中的行整行删除（避免破坏正文句子结构）。
    """
    lines = text.splitlines()
    cleaned: list[str] = []
    for line in lines:
        matched = False
        for _, pat in _COMPILED:
            if pat.search(line):
                matched = True
                break
        if not matched:
            cleaned.append(line)

    # 合并并清理多余空行（连续 3 行以上空行压缩为 2 行）
    result = "\n".join(cleaned)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


async def clean_by_llm(text: str, config: dict, log=None) -> str:
    """
    用 LLM 识别并去除整篇文档的元信息，返回清洗后的全文。
    """
    if not text.strip():
        return text

    prompt = (
        "以下是一份文档的完整内容，请识别并去除其中的元信息部分。\n"
        "元信息包括但不限于：DOI/ISSN/ISBN、作者、机构、投稿日期、基金编号、版权声明、"
        "课件的讲次/主讲人、新闻的发稿时间/责任编辑、页眉页脚、页码、"
        "数据库下载水印（含 Downloaded from 字样）等。\n\n"
        "请直接返回**去除元信息后**的正文内容，不要解释，不要添加任何说明。\n\n"
        f"【文档内容】\n{text}"
    )

    # 动态估算输出 token 上限：输入字符 / 2，限制在 [1000, 16000]
    max_out = max(1000, min(16000, len(text) // 2))

    if log:
        log(f"  [LLM清除] 发送 {len(text)} 字符…")

    try:
        import litellm
        from core.smart_crawler import _build_full_provider, _build_extra_body
        full_provider = _build_full_provider(config)
        api_key  = config.get("llm_api_key", "").strip() or None
        base_url = config.get("llm_base_url", "").strip() or None
        kwargs = dict(
            model=full_provider,
            messages=[{"role": "user", "content": prompt}],
            api_key=api_key,
            max_tokens=max_out,
            temperature=0.0,
            timeout=120,
        )
        eb = _build_extra_body(config)
        if eb:
            kwargs["extra_body"] = eb
        if base_url:
            kwargs["base_url"] = base_url
        resp = await litellm.acompletion(**kwargs)
        cleaned = resp.choices[0].message.content.strip()

        if log:
            log(f"  [LLM清除] 收到 {len(cleaned)} 字符")

        if len(cleaned) < 10:
            if log:
                log("  [LLM清除] 返回内容过短，退回规则模式")
            return clean_by_rule(text)

        return cleaned

    except Exception as e:
        if log:
            log(f"  [LLM清除失败，退回规则模式] {e}")
        return clean_by_rule(text)


async def clean_text(text: str, config: dict, clean_mode: str = "rule", log=None) -> str:
    """
    主入口。

    Args:
        text:       原始提取文本
        config:     含 LLM 配置的 dict
        clean_mode: "rule" | "llm" | "both"
        log:        日志回调

    Returns:
        清洗后的文本
    """
    if clean_mode == "none":
        return text

    if clean_mode == "rule":
        return clean_by_rule(text)

    if clean_mode == "llm":
        return await clean_by_llm(text, config, log)

    # both：规则先跑，再用 LLM 处理边界
    after_rule = clean_by_rule(text)
    return await clean_by_llm(after_rule, config, log)
