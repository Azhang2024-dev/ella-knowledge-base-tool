"""
llm_panel.py - LLM 配置面板
- Provider 下拉 + 自定义 Base URL / API Key
- 模型下拉框（可编辑），一键检测后自动填充可用模型列表
- Thinking 模式控制（auto / disabled / enabled）
- 配置持久化到 config/llm_config.json（由 get_values 触发）
"""

import json
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

# ── 持久化路径 ────────────────────────────────────────────────────────
_LLM_CFG_PATH = Path(__file__).parent.parent.parent / "config" / "llm_config.json"

# key -> (显示名, 默认 base_url, 默认 model, 需要 key)
_PROVIDERS = {
    "openai":    ("OpenAI",          "https://api.openai.com/v1",    "gpt-4o-mini",               True),
    "anthropic": ("Anthropic",       "",                              "claude-haiku-4-5-20251001", True),
    "gemini":    ("Google Gemini",   "",                              "gemini-2.0-flash",          True),
    "deepseek":  ("DeepSeek",        "https://api.deepseek.com/v1",  "deepseek-chat",             True),
    "ollama":    ("Ollama（本地）",  "http://localhost:11434/v1",    "llama3",                    False),
    "custom":    ("自定义兼容接口",  "",                              "",                          True),
}

_DEFAULT_INSTRUCTION = (
    "请分析页面内容，返回两类信息：\n\n"
    "【files】找出所有可直接下载的独立文件链接，包括：\n"
    "- 直接下载链接（.pdf/.docx/.xlsx/.zip 等扩展名）\n"
    "- 隐含的下载按钮（href 或 data-url 指向文件）\n"
    "- 没有扩展名但实际指向文件的 URL（如 /download?id=xxx）\n"
    "排除：'Download Page as PDF'、'Save as PDF'、'Print to PDF' "
    "等将当前网页本身导出为 PDF 的功能按钮。\n\n"
    "【nav_links】找出值得深入跟进的内容子页面链接，包括：\n"
    "- 书籍详情页、课程内容页、章节页\n"
    "- 明显会包含可下载资源的子目录页\n"
    "排除：顶部导航栏、搜索、登录、关于我们等无实质内容的链接。\n\n"
    "所有 URL 若为相对路径请补全为绝对路径。\n"
    "若某类无内容，对应数组返回空列表。"
)

_LLM_KEYS = {
    "llm_provider", "llm_model", "llm_api_key", "llm_base_url",
    "llm_instruction", "use_best_first", "generate_report",
    "mineru_token", "mineru_model_version",
    "mineru_enable_formula", "mineru_enable_table", "mineru_language",
    "thinking_mode",
}


# ── 后台拉取模型列表 ──────────────────────────────────────────────────
class _FetchThread(QThread):
    done  = Signal(list)
    error = Signal(str)

    def __init__(self, provider: str, base_url: str, api_key: str):
        super().__init__()
        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key  = api_key

    def run(self) -> None:
        try:
            self.done.emit(self._fetch())
        except Exception as e:
            self.error.emit(str(e))

    def _fetch(self) -> list[str]:
        # 静态列表 providers
        _STATIC = {
            "anthropic": [
                "claude-opus-4-8", "claude-sonnet-4-6",
                "claude-haiku-4-5-20251001", "claude-3-5-sonnet-20241022",
                "claude-3-haiku-20240307",
            ],
            "gemini": [
                "gemini-2.0-flash", "gemini-2.0-flash-lite",
                "gemini-1.5-pro", "gemini-1.5-flash",
            ],
        }
        if self._provider in _STATIC:
            return _STATIC[self._provider]

        import urllib.request
        req = urllib.request.Request(f"{self._base_url}/models")
        if self._api_key:
            req.add_header("Authorization", f"Bearer {self._api_key}")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        if "data" in data:
            return sorted(item["id"] for item in data["data"])
        if "models" in data:
            return sorted(item.get("name", "") for item in data["models"])
        return []


# ── 主面板 ────────────────────────────────────────────────────────────
class LLMPanel(QWidget):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        # llm_config.json 优先覆盖 settings
        merged = {**config, **_load()}
        self._thread: _FetchThread | None = None
        self._build_ui(merged)

    def _build_ui(self, cfg: dict) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # ── 连接配置 ──────────────────────────────────────────────────
        conn = QGroupBox("连接配置")
        form = QFormLayout(conn)
        form.setVerticalSpacing(6)
        form.setHorizontalSpacing(10)

        self._provider_combo = QComboBox()
        for key, (label, *_) in _PROVIDERS.items():
            self._provider_combo.addItem(label, userData=key)
        saved = cfg.get("llm_provider", "openai")
        idx = next((i for i, k in enumerate(_PROVIDERS) if k == saved), 0)
        self._provider_combo.setCurrentIndex(idx)
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("服务商", self._provider_combo)

        self._base_url_edit = QLineEdit(cfg.get("llm_base_url", ""))
        self._base_url_edit.setPlaceholderText("留空使用 provider 默认端点")
        form.addRow("Base URL", self._base_url_edit)

        key_row = QHBoxLayout()
        self._key_edit = QLineEdit(cfg.get("llm_api_key", ""))
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("API Key（Ollama 可留空）")
        show_btn = QPushButton("👁")
        show_btn.setFixedWidth(30)
        show_btn.setCheckable(True)
        show_btn.toggled.connect(
            lambda on: self._key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        key_row.addWidget(self._key_edit)
        key_row.addWidget(show_btn)
        form.addRow("API Key", key_row)

        model_row = QHBoxLayout()
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        saved_model = cfg.get("llm_model", "")
        if saved_model:
            self._model_combo.addItem(saved_model)
            self._model_combo.setCurrentText(saved_model)
        self._model_combo.setMinimumWidth(200)
        self._model_combo.lineEdit().setPlaceholderText("模型名，或点击检测后选择")
        self._detect_btn = QPushButton("检测")
        self._detect_btn.setFixedWidth(52)
        self._detect_btn.clicked.connect(self._on_detect)
        model_row.addWidget(self._model_combo, 1)
        model_row.addWidget(self._detect_btn)
        form.addRow("模型", model_row)

        # Thinking 模式（Gemini 3 等推理模型）
        self._thinking_combo = QComboBox()
        for label, val in [
            ("自动（Gemini 3 自动禁用，其他不管）", "auto"),
            ("强制禁用", "disabled"),
            ("交由模型决定", "enabled"),
        ]:
            self._thinking_combo.addItem(label, userData=val)
        saved_tm = cfg.get("thinking_mode", "auto")
        tm_idx = next((i for i in range(self._thinking_combo.count())
                       if self._thinking_combo.itemData(i) == saved_tm), 0)
        self._thinking_combo.setCurrentIndex(tm_idx)
        form.addRow("Thinking 模式", self._thinking_combo)

        root.addWidget(conn)

        # ── 提取指令 ──────────────────────────────────────────────────
        instr = QGroupBox("提取指令")
        instr_lay = QVBoxLayout(instr)
        self._instr_edit = QTextEdit()
        self._instr_edit.setFixedHeight(80)
        self._instr_edit.setPlaceholderText("告诉 LLM 要找什么内容…")
        self._instr_edit.setPlainText(cfg.get("llm_instruction", _DEFAULT_INSTRUCTION))
        instr_lay.addWidget(self._instr_edit)
        root.addWidget(instr)

        # ── 功能开关 ──────────────────────────────────────────────────
        opts = QGroupBox("智能功能")
        opts_lay = QVBoxLayout(opts)
        self._best_first_cb = QCheckBox("BestFirst 优先爬序（按关键词相关性排序）")
        self._best_first_cb.setChecked(bool(cfg.get("use_best_first", True)))
        self._report_cb = QCheckBox("爬取完成后生成 AI 汇总报告")
        self._report_cb.setChecked(bool(cfg.get("generate_report", True)))
        opts_lay.addWidget(self._best_first_cb)
        opts_lay.addWidget(self._report_cb)
        root.addWidget(opts)

        # ── MinerU 文档解析 ───────────────────────────────────────────
        mineru = QGroupBox("MinerU 文档解析")
        mineru_form = QFormLayout(mineru)
        mineru_form.setVerticalSpacing(6)
        mineru_form.setHorizontalSpacing(10)

        token_row = QHBoxLayout()
        self._mineru_token = QLineEdit(cfg.get("mineru_token", ""))
        self._mineru_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._mineru_token.setPlaceholderText("在 mineru.net API 管理页创建")
        show_mineru = QPushButton("👁")
        show_mineru.setFixedWidth(30)
        show_mineru.setCheckable(True)
        show_mineru.toggled.connect(
            lambda on: self._mineru_token.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        token_row.addWidget(self._mineru_token)
        token_row.addWidget(show_mineru)
        mineru_form.addRow("Token", token_row)

        self._mineru_model = QComboBox()
        for label, val in [("vlm（推荐）", "vlm"), ("pipeline", "pipeline")]:
            self._mineru_model.addItem(label, userData=val)
        saved_mv = cfg.get("mineru_model_version", "vlm")
        self._mineru_model.setCurrentIndex(0 if saved_mv == "vlm" else 1)
        mineru_form.addRow("模型版本", self._mineru_model)

        self._mineru_lang = QLineEdit(cfg.get("mineru_language", "ch"))
        self._mineru_lang.setPlaceholderText("ch / en / ...")
        mineru_form.addRow("语言", self._mineru_lang)

        self._mineru_formula = QCheckBox("启用公式识别")
        self._mineru_formula.setChecked(bool(cfg.get("mineru_enable_formula", True)))
        self._mineru_table   = QCheckBox("启用表格识别")
        self._mineru_table.setChecked(bool(cfg.get("mineru_enable_table", True)))
        feat_row = QHBoxLayout()
        feat_row.addWidget(self._mineru_formula)
        feat_row.addWidget(self._mineru_table)
        feat_row.addStretch()
        mineru_form.addRow("识别选项", feat_row)

        root.addWidget(mineru)

        root.addStretch()

        # 触发一次 provider 联动（设置 base_url placeholder 和 key 状态）
        self._on_provider_changed(idx)

    # ── 槽 ────────────────────────────────────────────────────────────

    def _on_provider_changed(self, index: int) -> None:
        key = list(_PROVIDERS.keys())[index]
        _, default_url, default_model, needs_key = _PROVIDERS[key]
        self._key_edit.setEnabled(needs_key)
        if default_url:
            self._base_url_edit.setPlaceholderText(default_url)
        else:
            self._base_url_edit.setPlaceholderText("留空使用 provider 默认端点")
        if not self._model_combo.currentText().strip() and default_model:
            self._model_combo.setCurrentText(default_model)

    def _on_detect(self) -> None:
        if self._thread and self._thread.isRunning():
            return
        provider = list(_PROVIDERS.keys())[self._provider_combo.currentIndex()]
        base_url = self._base_url_edit.text().strip() or \
                   _PROVIDERS[provider][1]  # fallback to default
        if not base_url and provider not in ("anthropic", "gemini"):
            QMessageBox.warning(self, "提示", "请先填写 Base URL")
            return
        self._detect_btn.setText("…")
        self._detect_btn.setEnabled(False)
        self._thread = _FetchThread(provider, base_url, self._key_edit.text().strip())
        self._thread.done.connect(self._on_fetched)
        self._thread.error.connect(self._on_fetch_err)
        self._thread.start()

    def _on_fetched(self, models: list[str]) -> None:
        self._detect_btn.setText("检测")
        self._detect_btn.setEnabled(True)
        if not models:
            QMessageBox.information(self, "结果", "未发现可用模型")
            return
        current = self._model_combo.currentText()
        self._model_combo.clear()
        for m in models:
            self._model_combo.addItem(m)
        if current and current in models:
            self._model_combo.setCurrentText(current)

    def _on_fetch_err(self, msg: str) -> None:
        self._detect_btn.setText("检测")
        self._detect_btn.setEnabled(True)
        QMessageBox.critical(self, "检测失败", msg)

    # ── 公共接口 ──────────────────────────────────────────────────────

    def get_values(self) -> dict:
        key  = list(_PROVIDERS.keys())[self._provider_combo.currentIndex()]
        model = self._model_combo.currentText().strip() or _PROVIDERS[key][2] or "gpt-4o-mini"
        values = {
            "llm_provider":    key,
            "llm_model":       model,
            "llm_api_key":     self._key_edit.text().strip(),
            "llm_base_url":    self._base_url_edit.text().strip(),
            "llm_instruction": self._instr_edit.toPlainText().strip(),
            "use_best_first":  self._best_first_cb.isChecked(),
            "generate_report": self._report_cb.isChecked(),
            "mineru_token":          self._mineru_token.text().strip(),
            "mineru_model_version":  self._mineru_model.currentData(),
            "mineru_language":       self._mineru_lang.text().strip() or "ch",
            "mineru_enable_formula": self._mineru_formula.isChecked(),
            "mineru_enable_table":   self._mineru_table.isChecked(),
            "thinking_mode":         self._thinking_combo.currentData(),
        }
        _save(values)
        return values


# ── 持久化 ────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        if _LLM_CFG_PATH.exists():
            return json.loads(_LLM_CFG_PATH.read_text("utf-8"))
    except Exception:
        pass
    return {}


def _save(values: dict) -> None:
    try:
        _LLM_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = _load()
        data.update({k: v for k, v in values.items() if k in _LLM_KEYS})
        _LLM_CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass
