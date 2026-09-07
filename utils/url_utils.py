"""
url_utils.py - URL 处理工具
提供规范化、域名比较、过滤等辅助函数
"""

from urllib.parse import urlparse, urlunparse


def normalize_url(url: str) -> str:
    """
    规范化 URL：去除首尾空格、fragment，过滤非 HTTP/HTTPS scheme
    """
    url = url.strip()
    if not url:
        return ""
    # 过滤非网页链接
    for prefix in ("mailto:", "javascript:", "tel:", "#"):
        if url.startswith(prefix):
            return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    # 去除 fragment
    clean = parsed._replace(fragment="")
    return urlunparse(clean)


def is_same_domain(url: str, base_url: str) -> bool:
    """判断 url 与 base_url 是否属于同一域名（含子域名策略：完全匹配 netloc）"""
    try:
        return urlparse(url).netloc == urlparse(base_url).netloc
    except Exception:
        return False


def filter_url(url: str, exclude_keywords: list) -> bool:
    """
    返回 True 表示该 URL 应被保留（未命中排除关键词）
    返回 False 表示应被过滤掉
    """
    if not exclude_keywords:
        return True
    lower_url = url.lower()
    return not any(kw.lower() in lower_url for kw in exclude_keywords)
