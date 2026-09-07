"""
logger_manager.py - 日志系统管理
创建同时输出到文件和 Qt Signal 队列的 logger
"""

import logging
import os
from datetime import datetime
from pathlib import Path


def setup_logger(output_dir: str, queue=None) -> logging.Logger:
    """
    创建一个命名 logger，同时写入日志文件和可选的线程安全队列。

    Args:
        output_dir: 输出根目录，日志存放于其 logs/ 子目录
        queue:      线程安全队列（queue.Queue），GUI 通过轮询读取日志行
    Returns:
        配置好的 logging.Logger 实例
    """
    log_dir = Path(output_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"crawler_{timestamp}.log"

    logger = logging.getLogger(f"crawler_{timestamp}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "[%(levelname)s] %(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # 文件 handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # 队列 handler（供 GUI 读取）
    if queue is not None:
        qh = _QueueHandler(queue, formatter)
        logger.addHandler(qh)

    return logger


class _QueueHandler(logging.Handler):
    """将日志记录以格式化字符串放入 queue.Queue，供 GUI 线程消费"""

    def __init__(self, queue, formatter: logging.Formatter):
        super().__init__()
        self._queue = queue
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._queue.put_nowait(msg)
        except Exception:
            self.handleError(record)
