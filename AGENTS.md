# Ella 知识库数据集小工具 — 项目上下文（AGENTS.md）

> 本文件由 Claude Code 历史同步生成，供 Codex 在本项目中加载项目上下文。
> 全局 coding 准则见 ~/.codex/AGENTS.md（AI 八荣八耻），此处只记项目级信息。

## 项目概述

PyQt 桌面 GUI 工具，用于构建知识库检索评测数据集。核心能力：网页爬取 → 文档处理 → Query 生成 → 相关性评分 → Excel 输出 → 格式转换。

- 入口：main.py → 启动 GUI
- 框架：PyQt5 + asyncio + litellm（多模型 LLM 调用）
- Python：.venv（项目本地虚拟环境，所有命令前缀 .venv\Scripts\python）

## 目录结构与分层

```
app/          UI 层（PyQt widgets / dialogs / main_window）
  main_window.py          主窗口，Tab 编排（数据采集/文档处理/Query/格式转换）
  widgets/                各功能面板（url_panel/config_panel/doc_panel/datasource_panel 等）
core/         业务逻辑层（纯逻辑，不依赖 Qt）
  smart_crawler.py        智能爬虫（Playwright 渲染 + LLM 链接分类 + 多轮跟进）【核心，勿改】
  crawler.py              经典爬虫
  doc_processor.py        文档处理流水线 Stage1-4
  doc_cleaner.py          元信息清除
  doc_chunker.py          分段（含 thinking 模式禁用逻辑）
  md_converter.py         Markdown→PDF/DOCX/TXT/HTML 格式转换
  datasource.py           DataSource Protocol + CrawlPipeline 编排（新）
  datasource_wikipedia.py Wikipedia Adapter（新）
  datasource_web_crawler.py WebCrawlAdapter 薄包装（新）
  wikitext_cleaner.py     MediaWiki Wikitext→纯文本（新）
  excel_processor.py      Excel 输出（含 _auto_col_width）
  report_generator.py
workers/      QThread 封装层（crawl_worker / doc_worker / convert_worker）
config/       JSON 配置（doc_config.json / llm_config.json 不提交）
utils/        工具函数（file_utils / text_utils）
output/       运行产物（不提交）
```

分层约定：core/ 不依赖 Qt；workers/ 把 core 的 async 逻辑包成 QThread；app/ 只管 UI。改 core 不应引入 Qt import。

## 关键技术约定

### LLM 调用
- 统一走 litellm，5 个调用点（smart_crawler / doc_processor / doc_chunker 等）共用 _build_extra_body(config) 辅助函数（在 smart_crawler.py）。
- thinking 模式可配置：Gemini-3 等 thinking 模型会把输出消耗在 reasoning_tokens 导致 content 为空。通过 extra_body={'thinking':{'type':'disabled'}} 关闭。UI（llm_panel）有 thinking 模式下拉框，配置持久化。
- 模型选择是可编辑 QComboBox（非 QLineEdit），点"检测"后填充可用模型列表。

### DataSource 抽象（feature/datasource-wikipedia 分支）
- DataSource Protocol 只有 3 个方法：name / enumerate_items()→AsyncIterator[ItemRecord] / fetch_content(item)→str。
- enumerate_items 的 pause_checker 参数是同步 () -> bool（不是 async）。CrawlPipeline 传 _is_paused（sync）而非 _wait_if_paused（async）。这是踩过的坑。
- datasource 字段缺省 = "web"，100% 向后兼容，WebCrawlAdapter 是薄包装，smart_crawler.py 一行不改。

### Excel 输出
- 横向展开格式：固定列 + 动态列对（每 chunk 3 列：段落文本/LLM评分/人工标注），按 LLM 评分降序。
- LLM 评分 1-4 分，score>=2 等价于旧 relevant=true。

### 格式转换（md_converter）
- Word COM（pywin32）转 PDF，每个 PDF 批次只启动一次 Word 实例；Word 崩溃（COM 错误 0x80010108/0x800706BA）时自动重启重试，仍失败则退回逐文件模式。
- 临时文件用 tempfile.mkstemp 生成唯一名，try/finally 保证清理。
- parse_md_table 返回 None 时外层 while 必须 i += 1，否则无限循环（已修复的坑）。

## Git 信息

当前分支：feature/datasource-wikipedia（基于 master 971edbb）

提交历史（新→旧）：
1. fea1971 Step 6 — GUI 接入：DatasourcePanel + main_window 数据源切换
2. 46b0e24 Step 5 — CrawlWorker 增加 datasource 分发路由，web 模式不变
3. d177cf8 Step 4 — WebCrawlAdapter 占位（web 模式走原有分发逻辑）
4. 05bf99f Step 3 — WikipediaAdapter 实现 + CrawlPipeline pause_checker 修复
5. 80de72d Step 1+2 — DataSource Protocol, ItemRecord, CrawlPipeline, Wikitext→纯文本转换器
6. 8ce7391 docs: Wikipedia datasource 改造 Plan
7. 971edbb chore: 保存当前工作区状态 — Wikipedia datasource 改造前基线
8. f8e74f3 feat: Prompt 编辑 Tab 增加「保存 Prompt」按钮
9. 616483d fix: DEFAULT_RELEVANCE_PROMPT 改英文；Stage4 prompt.format 改 format_map 防 KeyError
10. 7e9fb54 feat: 重写 DEFAULT_QUERY_GEN_PROMPT（英文/3条/负面示例/snippet_chars占位符）
11. f0e31b4 init: 项目初始提交

工作区：config/doc_config.json 有未提交修改（stage1/convert 输入输出目录改为"用户回流EN多格式"路径，转换格式只启用 PDF 100%）。

## Claude Code 历史会话纪要

以下 5 个会话来自 ~/.claude/projects/d--Users-admin-Desktop-My-Project-Ella---------，2026-07-07 至 07-10。完整提取见 CLAUDE_CODE_HISTORY_EXTRACT.txt。

### 会话 1 ｜ e78a4969（07-07）— Excel 私活（中断）
统计 general_eval_final_CN_300.xlsx 每条 query 召回文件数量。因模型不可用中断（claude-sonnet-4-6 无访问权限）。

### 会话 2 ｜ 52c85e4c（07-07）— Excel 私活 + thinking 模式 + 模型下拉
- Excel 私活完成：300 行 × 516 列，第 2 列插入"文件数量"（非空文件名单元格计数），输出 _with_count.xlsx。
- 排查 Query 生成空响应：根因是 google/gemini-3-flash-preview 是 thinking 模型，输出全进 reasoning_tokens，content 为空。修复：加 extra_body={'thinking':{'type':'disabled'}}。
- 把 thinking 禁用做成可配置：新增 _build_extra_body(config) helper，5 个调用点统一走它；llm_panel 加 thinking 模式下拉框，配置持久化。
- 模型选择从 QLineEdit 改为可编辑 QComboBox，删掉 _PickDialog，检测后直接填充下拉列表。

### 会话 3 ｜ 46ad6412（07-08）— MD 格式转换 + GUI Tab + Review 修 9 bug
- 需求：500 个学术论文 .md 文件按 PDF 79.4%/DOCX 17.5%/TXT 2.8%/其他 0.3% 比例转换。
- 试水 v1（3 文件）→ 修复路径分隔符/Windows 长路径/Word COM 启停时序/PDF XML 非法字符。
- 试水 v2（4 文件含 HTML）全通过。表格调研结论：这批语料没有标准 Markdown 表格，| 是语言学/伪代码符号。
- 新建 core/md_converter.py、workers/convert_worker.py、app/widgets/convert_panel.py，修改 main_window.py 加"格式转换"Tab，精简 convert_formats.py 改成从 md_converter 导入。
- Review 修 9 个 bug：①②单行 |cell| 无限循环 ③段落内循环缺列表项 break ④试水数量切格式列表而非文件列表 ⑤_on_finished 不显示失败数 ⑥HTML title 未转义 ⑦Word COM 每文件启停改批次级 ⑧_section_box 改名 _collapsible_box 统一 ⑨临时文件名固定改 mkstemp。再补 Word 崩溃自动重启重试逻辑。

### 会话 4 ｜ d3c91be7（07-09）— Python 学习（列表/元组/字典）
系统学习 Python 数据结构：嵌套列表、operator 模块、迭代语法、min/max 字典序、元组解包、字典。笔记存到 桌面\python学习日志\01-列表与元组总结.md。非本项目代码，但记录用户学习轨迹。

### 会话 5 ｜ 21c292ad（07-10）— Wikipedia API 爬取 + DataSource 抽象（当前分支）
- 调研 Wikipedia FR API（action=parse&prop=wikitext、list=categorymembers 分页、generator=allpages）。
- 提出通用 DataSource 抽象：Protocol 3 方法，WikipediaAdapter（HTTP API）+ WebCrawlAdapter（包装 smart_crawler）。
- 按本地 plan PLAN_wikipedia_datasource.md 执行 6 个 Step，全部提交到 feature/datasource-wikipedia 分支。
- 踩坑：pause_checker 同步/异步语义混淆（已修）；wikitext_cleaner 嵌套模板残留 }（已修）。
- GUI Tab "网页爬虫"→"数据采集"，DatasourcePanel 做数据源切换，条件显示/隐藏左侧面板。

## 关键 Plan 文件（~/.claude/plans/）

- virtual-dazzling-eich.md — Excel 输出新增元信息列 + LLM 预评分 + 人工标注（横向展开格式）
- polymorphic-sparking-hopper.md — 文档处理模块（OCR/元信息清除/Query 生成/Excel）
- merry-crunching-fern.md — Firecrawl 二级链接跟进（深度队列 + links 格式）
- bug-compiled-swan.md — 评测工具末尾卡住 bug 修复（保存阶段前置日志 + 状态重置）
- 项目内 PLAN_wikipedia_datasource.md — Wikipedia datasource 改造 plan（6 Step，已实施）

## 禁改文件清单

- core/smart_crawler.py — 核心爬虫逻辑，WebCrawlAdapter 是薄包装，勿改本体
- core/crawler.py、core/doc_chunker.py、core/doc_cleaner.py、core/report_generator.py
- app/widgets/url_panel.py、app/widgets/config_panel.py

## 运行与验证

- 启动 GUI：.venv\Scripts\python main.py（或 启动.vbs / 启动调试.bat）
- 语法校验：.venv\Scripts\python -c "import ast; ast.parse(open('目标文件',encoding='utf-8').read())"
- 数据采集（Wikipedia）：GUI 选 Wikipedia FR，填分类名/深度/数量，跑端到端
