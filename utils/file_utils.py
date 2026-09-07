"""
file_utils.py - 文件系统工具
提供目录创建、安全文件名生成、PDF 拆分等辅助函数
"""

import os
import re
import tempfile
from pathlib import Path


def ensure_dir(path) -> None:
    """确保目录存在，不存在时递归创建"""
    Path(path).mkdir(parents=True, exist_ok=True)


def safe_filename(name: str, max_length: int = 80) -> str:
    """
    将字符串转为安全文件名：
    - 替换非法字符为下划线
    - 限制最大长度
    - 去除首尾空格和点
    """
    # 替换 Windows/Linux 均非法的字符
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    # 压缩连续下划线
    name = re.sub(r"_+", "_", name)
    name = name.strip("_. ")
    return name[:max_length] if name else "unnamed"


def get_pdf_page_count(file_path: str) -> int:
    """快速读取 PDF 页数（仅读元数据，不渲染页面）。失败时返回 -1。"""
    try:
        import fitz  # pymupdf
        doc = fitz.open(file_path)
        count = doc.page_count
        doc.close()
        return count
    except Exception:
        return -1


def split_pdf(file_path: str, max_pages: int = 200) -> list[str]:
    """
    将超过 max_pages 页的 PDF 拆分为多个子 PDF。
    子文件写入系统临时目录，文件名格式：{stem}_part{NNN}.pdf
    返回子文件路径列表。若页数 <= max_pages，返回 [file_path]（不拆分）。
    """
    import fitz  # pymupdf

    doc = fitz.open(file_path)
    total_pages = doc.page_count
    if total_pages <= max_pages:
        doc.close()
        return [file_path]

    stem = Path(file_path).stem
    tmp_dir = tempfile.mkdtemp(prefix="mineru_split_")
    parts: list[str] = []

    for start in range(0, total_pages, max_pages):
        end = min(start + max_pages, total_pages)
        part_idx = start // max_pages + 1
        part_name = f"{stem}_part{part_idx:03d}.pdf"
        part_path = str(Path(tmp_dir) / part_name)

        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=start, to_page=end - 1)
        new_doc.save(part_path)
        new_doc.close()
        parts.append(part_path)

    doc.close()
    return parts
