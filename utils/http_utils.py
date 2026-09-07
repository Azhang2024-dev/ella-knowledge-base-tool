"""
http_utils.py - 反反爬工具集
提供 UA 轮转、完整浏览器请求头、随机间隔、指数退让重试
"""

import random
import time
from typing import Callable

import requests

# ── User-Agent 池 ─────────────────────────────────────────────────────
_UA_POOL = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Firefox macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Safari macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# Content-Type 前缀 → 扩展名（用于无路径扩展名的动态下载 URL）
CONTENT_TYPE_EXT = {
    "application/pdf":                                              "pdf",
    "application/msword":                                           "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":  "docx",
    "application/vnd.ms-excel":                                     "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":        "xlsx",
    "application/vnd.ms-powerpoint":                                "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "text/plain":                                                   "txt",
    "text/csv":                                                     "csv",
    "application/zip":                                              "zip",
    "application/x-rar-compressed":                                 "rar",
    "application/x-7z-compressed":                                  "7z",
    "application/epub+zip":                                         "epub",
    "application/vnd.oasis.opendocument.text":                      "odt",
    "application/vnd.oasis.opendocument.spreadsheet":               "ods",
    "application/vnd.oasis.opendocument.presentation":              "odp",
}


def random_ua() -> str:
    return random.choice(_UA_POOL)


def browser_headers(url: str, referer: str = "", ua: str = "") -> dict:
    """
    构造接近真实浏览器的请求头。
    Sec-Fetch-* 对大多数 CDN/WAF 有效。
    """
    from urllib.parse import urlparse
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    headers = {
        "User-Agent":      ua or random_ua(),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest":  "document",
        "Sec-Fetch-Mode":  "navigate",
        "Sec-Fetch-Site":  "none" if not referer else "same-origin",
        "Sec-Fetch-User":  "?1",
        "Cache-Control":   "max-age=0",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def download_headers(file_url: str, page_url: str, ua: str = "") -> dict:
    """下载文件时的请求头：带 Referer、Accept 偏向二进制"""
    headers = browser_headers(file_url, referer=page_url, ua=ua)
    headers["Accept"] = "*/*"
    headers["Sec-Fetch-Dest"] = "document"
    return headers


def jitter_sleep(base: float) -> None:
    """在 base × [0.7, 1.5] 范围内随机等待，避免规律性请求"""
    time.sleep(base * random.uniform(0.7, 1.5))


def get_with_retry(
    session: requests.Session,
    url: str,
    headers: dict,
    timeout: int,
    max_retries: int,
    log: Callable[[str], None],
    stream: bool = False,
) -> requests.Response | None:
    """
    带指数退让重试的 GET 请求。
    - 429 / 503：读取 Retry-After 或指数退让
    - 其他 4xx：不重试（大概率是权限/不存在）
    - 5xx / 网络错误：重试
    返回 None 表示最终失败。
    """
    delay = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, headers=headers, timeout=timeout, stream=stream)

            if resp.status_code == 429 or resp.status_code == 503:
                retry_after = float(resp.headers.get("Retry-After", delay))
                wait = min(retry_after, 60.0)
                log(f"  [限速 {resp.status_code}] 等待 {wait:.0f}s 后重试（{attempt}/{max_retries}）")
                time.sleep(wait)
                delay *= 2
                continue

            if 400 <= resp.status_code < 500:
                log(f"  [HTTP {resp.status_code}] 不重试")
                return None

            resp.raise_for_status()
            return resp

        except requests.exceptions.ConnectionError as e:
            log(f"  [连接错误] {e}，{delay:.0f}s 后重试（{attempt}/{max_retries}）")
        except requests.exceptions.Timeout:
            log(f"  [超时] {delay:.0f}s 后重试（{attempt}/{max_retries}）")
        except Exception as e:
            log(f"  [ERROR] {e}")
            return None

        time.sleep(delay)
        delay = min(delay * 2, 60.0)

    log(f"  [GIVE UP] 重试 {max_retries} 次后放弃：{url}")
    return None


def ext_from_content_type(content_type: str) -> str:
    """从 Content-Type 推断扩展名，找不到返回空字符串"""
    ct = content_type.split(";")[0].strip().lower()
    return CONTENT_TYPE_EXT.get(ct, "")
