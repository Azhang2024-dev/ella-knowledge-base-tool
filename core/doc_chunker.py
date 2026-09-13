"""
doc_chunker.py - 文档分段

支持：
  - 纯文字 PDF（pdfplumber）
  - 扫描版 PDF（paddleocr 本地 或 LLM 视觉）
  - Word (.docx)（python-docx）
  - 纯文本兜底（按字数 + 句子边界）
"""

import asyncio
import re
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Chunk:
    chunk_id: int
    page: int          # 页码，0 表示不可知
    text: str


def _split_by_sentences(text: str, chunk_size: int, overlap: int) -> list[str]:
    """按句子边界切分，不截断句子中间。"""
    # 句子边界：句号/问号/感叹号 + 空白，或真正的段落分隔（双换行）。
    # 注意：单个 \n 只是 OCR/版面的行内换行，不是句子边界，不能在此切断，
    # 否则会把同一句话切成残句。故此处用 \n\n+ 而非 \n+。
    sentences = re.split(r'(?<=[。？！.?!])\s+|\n\n+', text)
    chunks, current, current_len = [], [], 0
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if current_len + len(sent) > chunk_size and current:
            chunks.append("".join(current))
            # overlap：保留最后几个句子
            overlap_text = "".join(current)[-overlap:] if overlap > 0 else ""
            current = [overlap_text] if overlap_text else []
            current_len = len(overlap_text)
        current.append(sent + " ")
        current_len += len(sent) + 1
    if current:
        chunks.append("".join(current).strip())
    return [c for c in chunks if len(c.strip()) > 20]


def _chunks_from_paragraphs(paragraphs: list[tuple[int, str]], chunk_size: int, overlap: int) -> list[Chunk]:
    """
    paragraphs: list of (page, text)
    按段落分组，超过 chunk_size 时做句子级二次切分。
    chunk_size >= 999_000 为全文提取模式：保留 \\n\\n 段落结构，不做句子切分。
    """
    _full_text_mode = chunk_size >= 999_000

    result: list[Chunk] = []
    chunk_id = 0
    current_text, current_page, current_len = [], 0, 0

    for page, para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if current_len + len(para) > chunk_size and current_text:
            merged = "\n\n".join(current_text)
            subs = ([] if _full_text_mode else _split_by_sentences(merged, chunk_size, overlap))
            for sub in subs or [merged]:
                result.append(Chunk(chunk_id=chunk_id, page=current_page, text=sub.strip()))
                chunk_id += 1
            current_text, current_page, current_len = [], page, 0
        current_text.append(para)
        current_page = page
        current_len += len(para)

    if current_text:
        merged = "\n\n".join(current_text)
        subs = ([] if _full_text_mode else _split_by_sentences(merged, chunk_size, overlap))
        for sub in subs or [merged]:
            result.append(Chunk(chunk_id=chunk_id, page=current_page, text=sub.strip()))
            chunk_id += 1

    return result



# ── PDF 提取辅助：多栏排序 + 去重 ─────────────────────────────────────────

def _sort_blocks_by_reading_order(blocks: list, page_width: float) -> list[str]:
    """
    按阅读顺序排列文字块，支持多栏版式（报纸、杂志等）。
    blocks: list of (x0, y0, x1, y1, text)
    """
    if not blocks:
        return []

    # 宽度超过页宽 55% 视为跨栏块（大标题、通栏分割线等）
    spanning = [b for b in blocks if b[2] - b[0] > page_width * 0.55]
    columnar = [b for b in blocks if b[2] - b[0] <= page_width * 0.55]

    if not columnar:
        return [b[4] for b in sorted(blocks, key=lambda b: b[1])]

    # 用分栏块 x 中心的最大间距检测分栏点
    xc_vals = sorted((b[0] + b[2]) / 2 for b in columnar)
    split_x = None
    if len(xc_vals) >= 4:
        max_gap, split_candidate = 0, None
        for i in range(len(xc_vals) - 1):
            gap = xc_vals[i + 1] - xc_vals[i]
            if gap > max_gap:
                max_gap = gap
                split_candidate = (xc_vals[i] + xc_vals[i + 1]) / 2
        if max_gap > page_width * 0.25:
            split_x = split_candidate

    col_start_y = min(b[1] for b in columnar)

    if split_x is not None:
        left  = [b[4] for b in sorted(columnar, key=lambda b: b[1]) if (b[0] + b[2]) / 2 <  split_x]
        right = [b[4] for b in sorted(columnar, key=lambda b: b[1]) if (b[0] + b[2]) / 2 >= split_x]
        pre   = [b[4] for b in sorted(spanning,  key=lambda b: b[1]) if b[1] <  col_start_y]
        post  = [b[4] for b in sorted(spanning,  key=lambda b: b[1]) if b[1] >= col_start_y]
        return pre + left + right + post
    else:
        return [b[4] for b in sorted(blocks, key=lambda b: b[1])]


def _dedup_blocks(texts: list) -> list:
    """
    移除与上一块高度重叠的重复片段：若某块前 50 字出现在上一块末尾 200 字内，跳过。
    """
    result = []
    for text in texts:
        if result:
            probe = text[:50].strip()
            if probe and probe in result[-1][-200:]:
                continue
        result.append(text)
    return result


# ── PDF 提取 ──────────────────────────────────────────────────────────

def _extract_pdf_text(path: str, full_text_mode: bool = False) -> list[tuple[int, str]]:
    """
    提取文字型 PDF 段落。
    full_text_mode=True（skip/清洗模式）：get_text("text") 整页提取，保留原有结构，无重复片段。
    full_text_mode=False（chunk 模式）：get_text("blocks") 按段落块提取，供句子级切分使用。
    两种模式均降级到 pdfplumber。
    """
    # ── 优先尝试 pymupdf ────────────────────────────────────────────────
    try:
        import fitz  # pymupdf
        paragraphs: list[tuple[int, str]] = []
        doc = fitz.open(path)
        for page_num, page in enumerate(doc, start=1):
            if full_text_mode:
                # 全文模式：块级提取 + 多栏感知排序 + 相邻重复块去除
                raw_blocks = []
                for block in page.get_text("blocks"):
                    if len(block) < 7 or block[6] != 0:
                        continue
                    x0, y0, x1, y1 = block[0], block[1], block[2], block[3]
                    text = block[4].replace('­\n', '').replace('­', '').strip()
                    if len(text) > 10:
                        raw_blocks.append((x0, y0, x1, y1, text))
                for text in _dedup_blocks(_sort_blocks_by_reading_order(raw_blocks, page.rect.width)):
                    paragraphs.append((page_num, text))
            else:
                # block 模式：每个视觉块作为一个段落，适合后续句子级切分
                for block in page.get_text("blocks"):
                    if len(block) < 7 or block[6] != 0:
                        continue
                    text = block[4].strip()
                    if len(text) > 10:
                        paragraphs.append((page_num, text))
        doc.close()
        if paragraphs:
            return paragraphs
    except Exception:
        pass

    # ── 降级：pdfplumber ───────────────────────────────────────────────
    try:
        import pdfplumber
        paragraphs = []
        with pdfplumber.open(path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                for para in text.split("\n\n"):
                    para = para.strip()
                    if len(para) > 10:
                        paragraphs.append((page_num, para))
        return paragraphs
    except ImportError:
        return []


def _text_density(path: str) -> float:
    """估算 PDF 文字提取率（字符数/页数），低于阈值认为是扫描版。"""
    try:
        import pdfplumber
        total_chars, total_pages = 0, 0
        with pdfplumber.open(path) as pdf:
            total_pages = len(pdf.pages)
            for page in pdf.pages:
                total_chars += len(page.extract_text() or "")
        return total_chars / max(total_pages, 1)
    except Exception:
        return 0.0


_OCR_DPI            = 200    # 渲染分辨率；200 DPI 比 150 DPI 漏字明显减少
_OCR_PARA_GAP_RATIO = 1.5    # 行间距超过行高此倍数视为段落边界
_OCR_LINE_MIN_LEN   = 5      # 短于此字数的行跳过（页码、标点碎片）


def _normalize_ocr_line(s: str) -> str:
    """规范化 OCR 识别行：去除中文字间空格，压缩多余空白。"""
    s = re.sub(r'(?<=[一-鿿])\s+(?=[一-鿿])', '', s)  # 中文字间空格
    s = s.replace('　', ' ')   # 全角空格 → 普通空格
    s = re.sub(r'  +', ' ', s)     # 连续空格压缩为一个
    return s.strip()


def _ocr_pdf_paddle(path: str) -> list[tuple[int, str]]:
    """用 paddleocr 对 PDF 每页截图做 OCR。

    改进点（相比原实现）：
    - 渲染 DPI 150 → 200，减少漏字
    - PP-OCRv4 模型（paddleocr 2.x 需显式指定；3.x 已默认，自动兼容）
    - PIL 灰度化 + 对比度增强，改善低质量扫描件识别率
    - bbox Y 轴间距判断段落边界，替代 \\n\\n split
      （扫描版 PDF 几乎没有双换行，原方案整页挤成一段）
    - _normalize_ocr_line 规范化中文字间空格
    """
    import io
    import fitz  # pymupdf
    from paddleocr import PaddleOCR

    try:
        from PIL import Image, ImageEnhance
        _has_pil = True
    except ImportError:
        _has_pil = False

    # paddleocr 2.x 需显式传 ocr_version；3.x 默认已是 PP-OCRv4，不接受该参数
    try:
        ocr = PaddleOCR(use_angle_cls=True, lang="ch", ocr_version="PP-OCRv4", show_log=False)
    except TypeError:
        ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

    paragraphs: list[tuple[int, str]] = []
    doc = fitz.open(path)

    for page_num, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=_OCR_DPI)
        img_bytes = pix.tobytes("png")

        # PIL 图像增强：对比度提升，改善低质量扫描件。
        # 保持 RGB 三通道，paddleocr 的 use_angle_cls 不接受单通道灰度图。
        if _has_pil:
            try:
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                img = ImageEnhance.Contrast(img).enhance(1.5)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                img_bytes = buf.getvalue()
            except Exception:
                pass  # 增强失败则沿用原图，不中断 OCR

        result = ocr.ocr(img_bytes, cls=True)
        if not result or not result[0]:
            continue

        # ── bbox Y 轴间距判断段落边界 ────────────────────────────────────
        current_lines: list[str] = []
        prev_bottom: float | None = None

        for item in result[0]:
            if not item or len(item) < 2:
                continue
            box, (text_line, _conf) = item[0], item[1]
            text_line = _normalize_ocr_line(text_line)
            if len(text_line) < _OCR_LINE_MIN_LEN:
                continue

            top    = min(pt[1] for pt in box)
            bottom = max(pt[1] for pt in box)
            line_h = max(bottom - top, 1.0)  # 防止除零

            # 行间距 > 行高 × _OCR_PARA_GAP_RATIO → 刷新段落
            if prev_bottom is not None and (top - prev_bottom) > line_h * _OCR_PARA_GAP_RATIO:
                para = "".join(current_lines).strip()
                if len(para) > 10:
                    paragraphs.append((page_num, para))
                current_lines = []

            current_lines.append(text_line + "\n")
            prev_bottom = bottom

        # 页面最后一段
        if current_lines:
            para = "".join(current_lines).strip()
            if len(para) > 10:
                paragraphs.append((page_num, para))

    doc.close()
    return paragraphs


_VLM_UNSUPPORTED_KEYWORDS = (
    "does not support", "not support", "vision", "image", "multimodal",
    "cannot process image", "invalid content type", "unsupported content",
    "doesn't support", "no support for", "image_url",
    # 中转 API 拒绝多模态请求的常见错误关键词
    "invalid field", "bad_request", "bad request",
    "has invalid field", "request param validation error",
)

_OCR_BATCH_SIZE = 4      # 每批并发页数
_OCR_PAGE_TIMEOUT = 60   # 单页超时秒数


def _render_pdf_pages(path: str, dpi: int = 150) -> list[tuple[int, str]]:
    """同步：用 pymupdf 把每页渲染为 base64 PNG，在线程池中调用以避免阻塞事件循环。"""
    import base64
    import fitz
    doc = fitz.open(path)
    pages = []
    for page_num, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=dpi)
        b64 = base64.b64encode(pix.tobytes("png")).decode()
        pages.append((page_num, b64))
    doc.close()
    return pages


async def _ocr_pdf_llm_vision(path: str, config: dict, log=None) -> list[tuple[int, str]]:
    """用支持视觉的 LLM（GPT-4o / Claude 3.5 等）对 PDF 页面做 OCR。

    渲染在线程池执行（非阻塞），LLM 调用按批并发。
    """
    try:
        import litellm
        from core.smart_crawler import _build_full_provider, _build_extra_body
    except ImportError:
        if log:
            log("  [OCR] litellm 不可用，跳过 LLM 视觉 OCR")
        return []

    full_provider = _build_full_provider(config)
    api_key  = config.get("llm_api_key", "").strip() or None
    base_url = config.get("llm_base_url", "").strip() or None
    prompt   = "请识别并输出这张图片中的所有文字内容，保持原有段落结构，不要添加任何说明。"

    # ── Step 1：在线程池中渲染全部页面（避免阻塞事件循环） ──────────────
    loop = asyncio.get_event_loop()
    try:
        pages_b64: list[tuple[int, str]] = await loop.run_in_executor(
            None, _render_pdf_pages, path
        )
    except Exception as e:
        if log:
            log(f"  [OCR 渲染失败] {e}")
        return []

    if log:
        log(f"  渲染完成：{len(pages_b64)} 页，开始并发识别（每批 {_OCR_BATCH_SIZE} 页）…")

    # ── Step 2：并发批量 LLM 识别 ────────────────────────────────────────
    paragraphs: list[tuple[int, str]] = []
    abort = False   # 遇到"模型不支持视觉"时置 True

    async def _ocr_one_page(page_num: int, img_b64: str):
        kwargs = dict(
            model=full_provider,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
            api_key=api_key,
            max_tokens=2000,
            timeout=_OCR_PAGE_TIMEOUT,
        )
        eb = _build_extra_body(config)
        if eb:
            kwargs["extra_body"] = eb
        if base_url:
            kwargs["base_url"] = base_url
        resp = await litellm.acompletion(**kwargs)
        return page_num, resp.choices[0].message.content.strip()

    for batch_start in range(0, len(pages_b64), _OCR_BATCH_SIZE):
        if abort:
            break
        batch = pages_b64[batch_start:batch_start + _OCR_BATCH_SIZE]
        tasks = [_ocr_one_page(pn, b64) for pn, b64 in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            page_num = batch[i][0]
            if isinstance(result, Exception):
                err = str(result).lower()
                if any(kw in err for kw in _VLM_UNSUPPORTED_KEYWORDS):
                    if log:
                        log(
                            f"  [VLM 不支持] 当前模型「{full_provider}」不支持图像输入，已终止 OCR。\n"
                            "  请在「设置 → LLM 配置」中切换支持视觉的模型"
                            "（如 gpt-4o、claude-3-5-sonnet、gemini-1.5-flash 等），"
                            "或改用「本地 paddleocr」方案。"
                        )
                    abort = True
                    break
                if log:
                    log(f"  [OCR 第{page_num}页失败] {str(result).splitlines()[0]}")
            else:
                _, text = result
                for para in text.split("\n\n"):
                    if len(para.strip()) > 10:
                        paragraphs.append((page_num, para.strip()))

        done = min(batch_start + _OCR_BATCH_SIZE, len(pages_b64))
        if log and not abort:
            log(f"  OCR 进度：{done}/{len(pages_b64)} 页")

    return paragraphs


# ── 纯文本 / Markdown 提取 ───────────────────────────────────────────

def _read_text_file(path: str) -> str:
    """尝试多种编码读取文本文件。"""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb2312", "latin-1"):
        try:
            return Path(path).read_text(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _extract_txt_text(path: str) -> list[tuple[int, str]]:
    text = _read_text_file(path)
    if not text:
        return []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paras:
        paras = [l.strip() for l in text.splitlines() if l.strip()]
    return [(1, p) for p in paras if len(p) > 10]


def _extract_markdown_text(path: str) -> list[tuple[int, str]]:
    import re
    text = _read_text_file(path)
    if not text:
        return []
    text = re.sub(r'<[^>]+>', '', text)                          # strip HTML tags: <sub>, <sup>, etc.
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`{1,3}[^`\n]*`{1,3}', '', text)
    text = re.sub(r'!?\[([^\]]*)\]\([^\)]*\)', r'\1', text)
    text = re.sub(r'^[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'-{3,}|={3,}|\*{3,}', '', text)
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    return [(1, p) for p in paras if len(p) > 10]


def _extract_json_text(path: str) -> list[tuple[int, str]]:
    import json

    def _collect(obj, key_hint: str = "") -> list[str]:
        results = []
        if isinstance(obj, str):
            s = obj.strip()
            if len(s) > 10:
                results.append(f"{key_hint}: {s}" if key_hint else s)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                results.extend(_collect(v, str(k)))
        elif isinstance(obj, list):
            for item in obj:
                results.extend(_collect(item, key_hint))
        return results

    text = _read_text_file(path)
    if not text:
        return []
    try:
        data = json.loads(text)
        strings = _collect(data)
        return [(1, s) for s in strings]
    except json.JSONDecodeError:
        # 非标准 JSON，按文本处理
        return _extract_txt_text(path)


def _extract_excel_text(path: str) -> list[tuple[int, str]]:
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        # 尝试 xlrd 处理旧版 .xls
        try:
            import xlrd
            wb_xls = xlrd.open_workbook(path)
            paragraphs = []
            for sheet_idx in range(wb_xls.nsheets):
                ws = wb_xls.sheet_by_index(sheet_idx)
                rows_text = []
                for r in range(ws.nrows):
                    cells = [str(ws.cell_value(r, c)).strip()
                             for c in range(ws.ncols)
                             if str(ws.cell_value(r, c)).strip()]
                    if cells:
                        rows_text.append("  ".join(cells))
                for i in range(0, len(rows_text), 15):
                    para = "\n".join(rows_text[i:i + 15])
                    if len(para.strip()) > 10:
                        paragraphs.append((sheet_idx + 1, para))
            return paragraphs
        except ImportError:
            return []

    paragraphs = []
    for sheet_num, ws in enumerate(wb.worksheets, start=1):
        rows_text = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip() not in ("", "None")]
            if cells:
                rows_text.append("  ".join(cells))
        for i in range(0, len(rows_text), 15):
            para = "\n".join(rows_text[i:i + 15])
            if len(para.strip()) > 10:
                paragraphs.append((sheet_num, para))
    wb.close()
    return paragraphs


# ── Word 提取 ─────────────────────────────────────────────────────────

def _extract_docx_text(path: str) -> list[tuple[int, str]]:
    """用 python-docx 提取 Word 文档，按段落返回。"""
    from docx import Document
    doc = Document(path)
    paragraphs: list[tuple[int, str]] = []
    page = 1
    for para in doc.paragraphs:
        text = para.text.strip()
        if len(text) > 10:
            paragraphs.append((page, text))
    return paragraphs


# ── 主入口 ────────────────────────────────────────────────────────────

async def chunk_document(
    path: str,
    config: dict,
    chunk_size: int = 600,
    chunk_overlap: int = 50,
    ocr_backend: str = "auto",
    log=None,
) -> list[Chunk]:
    """
    对单个文档分段，返回 Chunk 列表。

    Args:
        path:         文件路径
        config:       含 LLM 配置的 dict
        chunk_size:   每段目标字数
        chunk_overlap: 段间重叠字数
        ocr_backend:  "paddleocr" | "llm_vision" | "auto"
        log:          日志回调
    """
    def _log(msg):
        if log:
            log(msg)

    suffix = Path(path).suffix.lower()

    # ── 纯文本 ────────────────────────────────────────────────────────
    if suffix == ".txt":
        paragraphs = _extract_txt_text(path)
        _log(f"  TXT 提取：{len(paragraphs)} 段")
        return _chunks_from_paragraphs(paragraphs, chunk_size, chunk_overlap)

    # ── Markdown ──────────────────────────────────────────────────────
    if suffix in (".md", ".markdown"):
        paragraphs = _extract_markdown_text(path)
        _log(f"  Markdown 提取：{len(paragraphs)} 段")
        return _chunks_from_paragraphs(paragraphs, chunk_size, chunk_overlap)

    # ── JSON ──────────────────────────────────────────────────────────
    if suffix == ".json":
        paragraphs = _extract_json_text(path)
        _log(f"  JSON 提取：{len(paragraphs)} 条记录")
        return _chunks_from_paragraphs(paragraphs, chunk_size, chunk_overlap)

    # ── Excel ─────────────────────────────────────────────────────────
    if suffix in (".xlsx", ".xls"):
        paragraphs = _extract_excel_text(path)
        _log(f"  Excel 提取：{len(paragraphs)} 段（跨 sheet）")
        return _chunks_from_paragraphs(paragraphs, chunk_size, chunk_overlap)

    # ── Word ──────────────────────────────────────────────────────────
    if suffix in (".docx", ".doc"):
        try:
            paragraphs = _extract_docx_text(path)
            _log(f"  Word 提取：{len(paragraphs)} 段")
            return _chunks_from_paragraphs(paragraphs, chunk_size, chunk_overlap)
        except Exception as e:
            if suffix == ".doc":
                _log(f"  [警告] 旧版 .doc 格式解析失败（{e}），建议另存为 .docx")
            else:
                _log(f"  [警告] python-docx 未安装或文件损坏：{e}")
            return []

    # ── PDF ───────────────────────────────────────────────────────────
    if suffix == ".pdf":
        density = _text_density(path)
        _log(f"  PDF 文字密度：{density:.0f} 字/页")

        if density >= 50:
            # 纯文字型 PDF
            try:
                paragraphs = _extract_pdf_text(path, full_text_mode=(chunk_size >= 999_000))
                _log(f"  PDF 提取：{len(paragraphs)} 段")
                return _chunks_from_paragraphs(paragraphs, chunk_size, chunk_overlap)
            except ImportError:
                _log("  [警告] pdfplumber 未安装")
                return []
        else:
            # 扫描版 PDF — 走 OCR
            _log("  检测为扫描版 PDF，启动 OCR…")
            backend = ocr_backend

            # auto：优先本地，降级 LLM 视觉
            if backend == "auto":
                try:
                    import paddleocr  # noqa: F401
                    backend = "paddleocr"
                except ImportError:
                    backend = "llm_vision"
                    _log("  paddleocr 未安装，使用 LLM 视觉 OCR")

            if backend == "paddleocr":
                try:
                    paragraphs = _ocr_pdf_paddle(path)
                    _log(f"  paddleocr 识别：{len(paragraphs)} 段")
                    return _chunks_from_paragraphs(paragraphs, chunk_size, chunk_overlap)
                except Exception as e:
                    _log(f"  [paddleocr 失败] {e}，尝试 LLM 视觉")
                    paragraphs = await _ocr_pdf_llm_vision(path, config, log)
                    if not paragraphs:
                        _log("  [提示] LLM 视觉 OCR 也未能提取内容，请检查模型是否支持图像输入")
                    return _chunks_from_paragraphs(paragraphs, chunk_size, chunk_overlap)
            else:
                paragraphs = await _ocr_pdf_llm_vision(path, config, log)
                if not paragraphs:
                    _log("  [提示] LLM 视觉 OCR 无结果，尝试降级到本地 paddleocr…")
                    try:
                        paragraphs = _ocr_pdf_paddle(path)
                        _log(f"  paddleocr 降级识别：{len(paragraphs)} 段")
                    except Exception as e2:
                        _log(f"  [paddleocr 降级失败] {e2}")
                        paragraphs = []
                else:
                    _log(f"  LLM 视觉 OCR：{len(paragraphs)} 段")
                return _chunks_from_paragraphs(paragraphs, chunk_size, chunk_overlap)

    _log(f"  [不支持的文件类型] {suffix}")
    return []


def _extract_markdown_text_from_str(text: str) -> list[tuple[int, str]]:
    """从 Markdown 字符串提取纯文本段落（与 _extract_markdown_text 逻辑一致）。"""
    text = re.sub(r'<[^>]+>', '', text)                          # strip HTML tags: <sub>, <sup>, etc.
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`{1,3}[^`\n]*`{1,3}', '', text)
    text = re.sub(r'!?\[([^\]]*)\]\([^\)]*\)', r'\1', text)
    text = re.sub(r'^[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'-{3,}|={3,}|\*{3,}', '', text)
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    return [(1, p) for p in paras if len(p) > 10]
