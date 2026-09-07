"""
parser.py - 网页链接与文件扫描
"""

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# 支持的可下载文件扩展名（小写）
DOWNLOADABLE_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "txt", "csv", "zip", "rar", "7z", "tar", "gz",
    "epub", "mobi", "odt", "ods", "odp",
}


def extract_file_links(html: str, base_url: str, target_exts: set[str]) -> list[dict]:
    """
    扫描页面中所有指向目标扩展名文件的链接。
    同时识别两种模式：
      1. 路径含扩展名：href="/files/report.pdf"
      2. 动态下载 URL：href="/download?id=42"，依赖链接文字或 download 属性推断

    Returns:
        list of {"url": str, "ext": str, "text": str}
    """
    soup = BeautifulSoup(html, "lxml")
    found = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        if parsed.scheme not in ("http", "https"):
            continue

        path = parsed.path.lower()
        ext = path.rsplit(".", 1)[-1] if "." in path.split("/")[-1] else ""

        # 动态 URL 兜底：看 download 属性或链接文字末尾
        if ext not in target_exts:
            download_attr = (a.get("download") or "").lower()
            link_text = a.get_text(strip=True).lower()
            for candidate in (download_attr, link_text):
                if "." in candidate:
                    candidate_ext = candidate.rsplit(".", 1)[-1].strip()
                    if candidate_ext in target_exts:
                        ext = candidate_ext
                        break
            else:
                continue

        clean_url = parsed._replace(fragment="").geturl()
        if clean_url not in found:
            found[clean_url] = {
                "url":  clean_url,
                "ext":  ext,
                "text": a.get_text(strip=True) or "",
            }

    return list(found.values())


def extract_page_links(html: str, base_url: str) -> list[str]:
    """提取页面中所有可继续爬取的 HTTP/HTTPS 页面链接（排除文件链接）"""
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        if parsed.scheme not in ("http", "https"):
            continue

        path = parsed.path.lower()
        ext = path.rsplit(".", 1)[-1] if "." in path else ""
        if ext in DOWNLOADABLE_EXTS:
            continue  # 文件链接不作为页面链接递归

        clean = parsed._replace(fragment="").geturl()
        links.append(clean)
    return links


def extract_title(html: str, url: str = "") -> str:
    """提取页面标题"""
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("title")
    if tag and tag.get_text(strip=True):
        return tag.get_text(strip=True)
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if url:
        path = urlparse(url).path.rstrip("/")
        if path:
            return path.split("/")[-1]
    return "untitled"
