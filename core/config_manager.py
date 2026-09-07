"""
config_manager.py - 配置文件读写管理
负责从 config/settings.json 加载、保存、重置配置
"""

import json
import os
from pathlib import Path


# 默认配置
DEFAULT_CONFIG = {
    "start_urls": [],
    "output_dir": "output",
    "target_extensions": ["pdf", "doc", "docx", "txt"],
    "max_files": 50,
    "max_pages": 100,
    "max_depth": 2,
    "request_interval": 1.0,
    "timeout": 15,
    "max_retries": 3,
    "same_domain_only": True,
    "include_keywords": "",
    "exclude_keywords": "login,signup,privacy,terms",
    "user_agent": "",
}

# 配置文件路径（相对于项目根目录）
CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.json"


def get_default_config() -> dict:
    """返回默认配置的副本"""
    return dict(DEFAULT_CONFIG)


def load_config() -> dict:
    """
    从 settings.json 加载配置；
    文件不存在时自动创建并返回默认配置
    """
    if not CONFIG_PATH.exists():
        save_config(get_default_config())
        return get_default_config()

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 用默认值补全缺失字段
        merged = get_default_config()
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return get_default_config()


def save_config(config: dict) -> None:
    """将配置写入 settings.json"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
