"""
error_logger.py - MinerU / 文档处理失败日志

记录每个失败文件的原因、时间、路径、文件名，写入 CSV 便于排查。
线程安全（加锁），幂等（重复调用追加）。
"""

import csv
import os
import threading
from datetime import datetime
from pathlib import Path

_LOCK = threading.Lock()

# CSV 表头
_HEADERS = ["timestamp", "stage", "filename", "path", "reason"]


def log_error(
    log_path: str | os.PathLike,
    stage: str,
    path: str,
    reason: str,
) -> None:
    """
    追加一条失败记录到 CSV。

    Args:
        log_path: CSV 文件路径；父目录会自动创建。
        stage:    失败阶段，如 "mineru_upload" / "mineru_parse" /
                  "mineru_save" / "mineru_batch" / "doc_extract"。
        path:     原始文件绝对路径。
        reason:   失败原因（异常信息或状态描述）。
    """
    p = Path(log_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            need_header = not p.exists() or p.stat().st_size == 0
            with open(p, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                if need_header:
                    writer.writerow(_HEADERS)
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    stage,
                    Path(path).name if path else "",
                    path or "",
                    reason,
                ])
    except Exception:
        # 日志本身失败不应影响主流程
        pass


def error_log_path(output_dir: str | os.PathLike) -> Path:
    """根据输出目录返回错误日志 CSV 路径。"""
    return Path(output_dir) / "errors.csv"
