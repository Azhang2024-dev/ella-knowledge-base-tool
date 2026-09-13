# Ella 知识库数据集小工具

---

## 基本信息

| 字段               | 内容                                                                                                                                    |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| **作者**     | Azhang2024-dev                                                                                                                                  |
| **版本**     | 1.0.0                                                                                                                                   |
| **最后更新** | 2026-07-14                                                                                                                              |
| **业务场景** | 知识库检索评测数据集构建                                                                                                                |
| **输入格式** | URL 列表 / Wikipedia 分类名 / 本地文档目录（支持 .txt / .md / .pdf / .docx）                                                            |
| **输出格式** | Excel 评测数据集（.xlsx，含 Query / Passage / LLM 评分 / 人工标注列）；JSONL 中间数据；纯文本提取文件；转换后的 PDF / DOCX / TXT / HTML |

---

## 运行方式

```bash
# 1. 安装依赖
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. 安装 Playwright 浏览器（智能爬虫模式需要）
playwright install chromium

# 3. 启动 GUI
.venv\Scripts\python main.py

# 或使用快捷脚本
启动.vbs
```

---

## 业务逻辑说明

本工具用于构建**信息检索评测数据集**，完整流水线覆盖 6 个阶段：

### 1. 数据采集

支持两种数据源，通过 `DataSource` Protocol 抽象：

- **网页爬虫**：经典模式（requests + BeautifulSoup）或智能模式（Playwright 渲染 + LLM 自动分类链接）。智能模式会从起始页提取所有链接，交给 LLM 分为 `files`（可下载文件）和 `nav_links`（值得跟进的导航链接），多轮递归跟进。
- **Wikipedia**：通过 MediaWiki API 按分类/前缀/标题列表枚举页面，获取 Wikitext 并清洗为纯文本，无需浏览器。

所有数据源共用 `CrawlPipeline` 编排器：枚举 → 并发获取正文 → 清洗 → 落盘 JSONL + 纯文本文件。

### 2. 文档处理（Stage 1-4）

| Stage   | 功能           | 说明                                                    |
| ------- | -------------- | ------------------------------------------------------- |
| Stage 1 | 文本提取 + OCR | 支持 MinerU / PaddleOCR / LLM 视觉（auto 模式自动降级） |
| Stage 2 | 元信息清除     | 去除页眉页脚、页码、参考文献标记等噪音                  |
| Stage 3 | 文档分段       | 可配置 chunk_size / overlap，支持 thinking 模式开关     |
| Stage 4 | Query 生成     | 基于分段内容，LLM 生成 3 条评测 Query                   |

### 3. Query 生成

两种模式：

- **文档级 Query**：基于文档内容生成，5 种类型（关键词 / 问题 / 场景 / 模糊搜索 / 操作指令），每条文档恰好 3 条 Query。
- **通用 Query**：先对知识库做领域分析（LLM 聚类），再按领域盲生成 Query（跨文档 / 泛化 / 精确），不依赖文档内容。

### 4. 相关性评分

LLM 对每条 Query-Passage 对打 1-4 分：

| 分数 | 含义                                   |
| ---- | -------------------------------------- |
| 4    | 直接回答：段落直接且完整地回答了 Query |
| 3    | 部分回答：段落相关但不完整             |
| 2    | 背景相关：同主题领域，有参考价值       |
| 1    | 不相关                                 |

score ≥ 2 视为相关。支持人工标注列。

### 5. Excel 输出

输出 `eval_dataset.xlsx`，横向展开格式：固定列（Query / 文件ID / 文件名）+ 动态列对（每 chunk 3 列：段落文本 / LLM 评分 / 人工标注），按 LLM 评分降序排列。

### 6. 格式转换

将 Markdown 文件按比例随机转换为 PDF / DOCX / TXT / HTML。PDF 转换通过 Word COM 实现，批次级实例管理，崩溃自动重启重试。

---

## 项目结构

```
app/          UI 层（PySide6 widgets / dialogs / main_window）
core/         业务逻辑层（零 Qt 依赖，可独立运行）
workers/      QThread 封装层（把 core 的 async 逻辑包成 QThread）
config/       JSON 配置（doc_config.json / llm_config.json）
utils/        工具函数（file_utils / text_utils / http_utils）
```

**分层约定**：`core/` 不依赖 Qt；`workers/` 桥接 async 与 Qt 事件循环；`app/` 只管 UI。

---

## 依赖

### 运行环境

- Python 3.10+
- Windows 10/11（格式转换依赖 Word COM）

### Python 包

| 包                           | 用途                                            |
| ---------------------------- | ----------------------------------------------- |
| PySide6                      | GUI 框架                                        |
| pyqtdarktheme-fork           | 暗色主题                                        |
| crawl4ai                     | 智能爬虫引擎（Playwright 封装）                 |
| litellm                      | 统一 LLM 调用（OpenAI / Google / Anthropic 等） |
| openpyxl / pandas            | Excel 读写                                      |
| pdfplumber / pymupdf         | PDF 文本提取                                    |
| python-docx                  | Word 文档处理                                   |
| markdown                     | Markdown 渲染                                   |
| trafilatura / beautifulsoup4 | 网页内容提取                                    |
| requests                     | HTTP 请求                                       |

### 可选依赖

- PaddleOCR（~1-2GB）：OCR 后端，不安装时 auto 模式自动降级到 LLM 视觉

---

## 已知限制

1. **格式转换仅限 Windows**：PDF 转换依赖 Word COM（pywin32），macOS/Linux 无法使用
2. **智能爬虫需要浏览器**：Playwright 需要安装 Chromium（`playwright install chromium`），首次启动较慢
3. **Thinking 模型兼容**：Gemini-3 等 thinking 模型需要在 LLM 配置中手动关闭 thinking 模式，否则输出全进 `reasoning_tokens` 导致 `content` 为空
4. **Wikipedia 仅限文本**：Wikipedia 数据源只获取 Wikitext 正文，不包含图片/表格/信息框
5. **LLM 调用有成本**：所有 Query 生成和相关性评分都依赖 LLM API，大批量处理时注意 token 消耗
6. **Word COM 不稳定**：大批量 PDF 转换时 Word 可能崩溃，工具会自动重启重试，但仍可能失败
7. **并发限制**：默认并发数为 5，可在配置中调整，过高可能触发 API 限流
8. **Excel 行数限制**：横向展开格式在 chunk 数量很多时列数会很大，Excel 最大列数为 16384

---

## 配置说明

### LLM 配置

通过菜单 `设置 → LLM / 智能模式配置` 打开：

- **模型选择**：可编辑下拉框，点"检测"填充可用模型列表
- **Thinking 模式**：针对 Gemini-3 等 thinking 模型，选择 `disabled` 关闭推理输出
- **API Key**：支持 OpenAI / Google / Anthropic 等多厂商

### 文档处理配置（`config/doc_config.json`）

- `chunk_size` / `chunk_overlap`：分段参数
- `ocr_backend`：OCR 后端（`mineru_fallback` / `paddle` / `auto`）
- `query_snippet_chars`：Query 生成时截取的文档字符数（默认 7000）
- 所有 Prompt 模板均可在 UI 中编辑并持久化保存

---

## 贡献信息

### 主要贡献者

| 角色       | 姓名   | 职责                                                           |
| ---------- | ------ | -------------------------------------------------------------- |
| 项目负责人 | [已脱敏] | 项目管理，SOP建设，语料提供，需求对接，成果验收                |
| 开发者     | Azhang2024-dev | 整体架构设计、核心功能开发，GUI 开发、爬虫引擎、文档处理流水线 |
| 开发者     | Azhang2024-dev | LLM 集成、Excel 输出、格式转换                                 |

### 问题反馈

- **Bug 报告**：请通过 GitLab Issues 提交，标注 `bug` 标签
- **功能建议**：请通过 GitLab Issues 提交，标注 `enhancement` 标签
- **紧急问题**：联系项目负责人或者开发者 [已脱敏] / Azhang2024-dev

### 代码审查

- 所有代码变更需通过 Merge Request 提交
- 核心模块（`core/smart_crawler.py` / `core/datasource.py`）变更需项目负责人审核
- 提交前请确保：
  - 语法检查通过：`.venv\Scripts\python -c "import ast; ast.parse(open('文件路径',encoding='utf-8').read())"`
  - 遵循分层约定：`core/` 不依赖 Qt，`workers/` 桥接 async 与 Qt，`app/` 只管 UI
  - 不修改禁改文件（见 AGENTS.md）

### 开发规范

1. **分支管理**：功能分支命名 `feature/xxx`，修复分支命名 `fix/xxx`
2. **提交信息**：使用中文，格式 `类型: 简短描述`（如 `feat: 新增 Wikipedia 数据源`）
3. **配置管理**：`config/doc_config.json` 和 `config/llm_config.json` 不提交到仓库
4. **测试验证**：改完代码必须验证，不粉饰失败

### 变更记录

| 版本  | 日期       | 变更内容                                | 作者   |
| ----- | ---------- | --------------------------------------- | ------ |
| 1.0.0 | 2026-07-14 | 初始版本：完整流水线 + Wikipedia 数据源 | Azhang2024-dev |
