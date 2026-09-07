"""
text_utils.py - 文本处理工具
提供文本清洗等辅助函数（为后续 chunk/classify 扩展预留）
"""

import re


def clean_text(text: str) -> str:
    """
    基础文本清洗：合并多余空行、去除首尾空白
    """
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate_text(text: str, max_chars: int = 10000) -> str:
    """截断文本到指定字符数，避免保存超大内容"""
    if len(text) > max_chars:
        return text[:max_chars] + "\n...[内容已截断]"
    return text
