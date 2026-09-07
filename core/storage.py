"""
storage.py - 文件下载保存
按扩展名分子目录，按链接文本或 URL 路径命名，内容哈希去重
"""

import hashlib
from pathlib import Path

from utils.file_utils import safe_filename, ensure_dir

# 扩展名 -> 分类子目录名
EXT_CATEGORY = {
    "pdf":  "PDF",
    "doc":  "Word", "docx": "Word",
    "xls":  "Excel", "xlsx": "Excel",
    "ppt":  "PPT", "pptx": "PPT",
    "txt":  "TXT", "csv":  "TXT",
    "zip":  "Archive", "rar": "Archive", "7z": "Archive",
    "tar":  "Archive", "gz":  "Archive",
    "epub": "EBook", "mobi": "EBook",
    "odt":  "Word", "ods":  "Excel", "odp": "PPT",
}


def save_file(
    content: bytes,
    file_info: dict,
    output_dir: str | Path,
    seen_hashes: set,
) -> dict:
    """
    将下载内容保存到磁盘。

    Args:
        content:      文件二进制内容
        file_info:    {"url": str, "ext": str, "text": str}
        output_dir:   根输出目录
        seen_hashes:  已保存内容的 sha256 集合（用于去重，函数内更新）

    Returns:
        {"saved": bool, "path": str, "reason": str}
    """
    digest = hashlib.sha256(content).hexdigest()
    if digest in seen_hashes:
        return {"saved": False, "path": "", "reason": "duplicate"}
    seen_hashes.add(digest)

    ext = file_info.get("ext", "bin").lower()
    category = EXT_CATEGORY.get(ext, ext.upper())
    save_dir = Path(output_dir) / category
    ensure_dir(save_dir)

    # 文件名：优先用链接文字，其次取 URL 路径最后一段
    name_hint = file_info.get("text") or ""
    if not name_hint:
        from urllib.parse import urlparse
        name_hint = Path(urlparse(file_info["url"]).path).stem or "file"

    filename = safe_filename(name_hint, max_length=80) + f".{ext}"
    dest = _unique_path(save_dir / filename)
    dest.write_bytes(content)

    return {"saved": True, "path": str(dest), "reason": "ok"}


def _unique_path(path: Path) -> Path:
    """若目标路径已存在，追加 _1 _2 ... 直到找到可用名"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1
