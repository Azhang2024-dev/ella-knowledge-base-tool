"""
wikitext_cleaner.py — MediaWiki Wikitext → 纯文本转换

处理 Wikipedia API (action=parse&prop=wikitext) 返回的原始 Wikitext，
逐层剥离标记，输出适合后续 doc_chunker / doc_cleaner 使用的纯文本。

转换顺序（按依赖关系）：
  1. 去掉 HTML 注释 <!-- ... -->
  2. 展开 {{lang|fr|texte}} → texte
  3. 去掉引用 <ref>...</ref> / {{sfn|...}} 等
  4. 去掉模板 {{...}}（Infobox, 导航框, 标注模板等）
  5. 解析内部链接 [[...]] → 显示文字
  6. 解析外部链接 [...] → 显示文字
  7. 去掉粗/斜体 ''' / ''
  8. 去掉列表标记 * / # / : / ;
  9. 去掉标题 = ... = → 纯文本行
  10. 去掉表格 {| ... |}
  11. 去掉 HTML 标签
  12. 去掉分类、语言链接、文件嵌入
  13. 压缩多余空行
"""

import re


# ════════════════════════════════════════════════════════════════════════
# Wikitext → 纯文本
# ════════════════════════════════════════════════════════════════════════

def wikitext_to_text(wikitext: str) -> str:
    """主入口：MediaWiki Wikitext → 纯文本。

    Args:
        wikitext: Wikipedia API action=parse&prop=wikitext 返回的原始内容

    Returns:
        清洗后的纯文本，段落由 \\n\\n 分隔
    """
    t = wikitext

    # ── 1. HTML 注释 ────────────────────────────────────────────────────
    t = re.sub(r'<!--.*?-->', '', t, flags=re.DOTALL)

    # ── 2. 语言标注 {{lang|xx|text}} → text ─────────────────────────────
    t = re.sub(r'\{\{lang(?:ue)?\|[a-z\-]+\|([^|}]+(?:\|[^|}]+)*)\}\}',
               r'\1', t)
    # 处理 {{lang-xx|text}} 形式
    t = re.sub(r'\{\{lang-[a-z\-]+\|([^|}]+)\}\}', r'\1', t)

    # ── 3. 引用 ─────────────────────────────────────────────────────────
    t = re.sub(r'<ref[^>]*?>(.*?)</ref\s*>', '', t, flags=re.DOTALL)
    t = re.sub(r'<ref[^>]*?/\s*>', '', t)                    # 自闭合 <ref name="x" />
    # 短引用模板 {{sfn|...}} {{harv|...}} {{rp|...}}
    t = re.sub(r'\{\{(?:sfn|harv|harvnb|harvcol|rp)\|.*?\}\}', '', t)
    # 引用组 {{reflist|...}} {{notelist|...}}
    t = re.sub(r'\{\{(?:reflist|notelist|références|references)\|?.*?\}\}',
               '', t, flags=re.DOTALL)
    # 注脚 {{efn|...}} {{note|...}}
    t = re.sub(r'\{\{(?:efn|note)\|.*?\}\}', '', t, flags=re.DOTALL)

    # ── 4. 模板 ─────────────────────────────────────────────────────────
    t = _remove_templates(t)

    # ── 5. 内部链接 [[target|display]] → display ────────────────────────
    # [[target|display]] → display
    t = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', t)
    # [[target]] → target
    t = re.sub(r'\[\[([^\]]+)\]\]', r'\1', t)

    # ── 6. 外部链接 [url text] → text ──────────────────────────────────
    # [url text] → text
    t = re.sub(r'\[https?://[^\]\s]+\s+([^\]]+)\]', r'\1', t)
    # [url] → 去掉
    t = re.sub(r'\[https?://[^\]]+\]', '', t)

    # ── 7. 粗体/斜体 ────────────────────────────────────────────────────
    t = re.sub(r"'''''(.+?)'''''", r'\1', t)     # 粗斜体 5'
    t = re.sub(r"'''(.+?)'''", r'\1', t)          # 粗体 3'
    t = re.sub(r"''(.+?)''", r'\1', t)            # 斜体 2'

    # ── 8. 列表标记 ─────────────────────────────────────────────────────
    # 无序列表 * / 有序列表 # / 定义列表 ; :
    for _ in range(3):  # 多级嵌套，多次 pass
        t = re.sub(r'^[\*\#\;\:]{1,4}\s*', '', t, flags=re.MULTILINE)

    # ── 9. 章节标题 = ... = → 纯文本行 ──────────────────────────────────
    t = re.sub(r'^={1,6}\s*(.+?)\s*={1,6}\s*$', r'\n\1\n', t, flags=re.MULTILINE)

    # ── 10. 表格 {| ... |} ──────────────────────────────────────────────
    t = _remove_tables(t)

    # ── 11. HTML 标签 ────────────────────────────────────────────────────
    # 先保留 <br /> 为换行
    t = re.sub(r'<br\s*/?\s*>', '\n', t, flags=re.IGNORECASE)
    # 去掉其余所有 HTML 标签
    t = re.sub(r'<[^>]+>', '', t)

    # ── 12. 分类 / 语言链接 / 文件嵌入 ──────────────────────────────────
    t = re.sub(r'^\[\[(?:Catégorie|Category|Fichier|File|Image|Media|Aide|Aide|Modèle|Template|Portail|Projet):[^\]]+\]\]',
               '', t, flags=re.MULTILINE | re.IGNORECASE)
    # 行尾的语言链接 [[xx:...]]
    t = re.sub(r'^\[\[[a-z]{2,3}:[^\]]+\]\]', '', t, flags=re.MULTILINE)

    # ── 13. 特殊实体 / 转义 ──────────────────────────────────────────────
    t = t.replace('&nbsp;', ' ')
    t = t.replace('&amp;', '&')
    t = t.replace('&lt;', '<')
    t = t.replace('&gt;', '>')
    t = t.replace('&quot;', '"')

    # ── 14. 清除孤立花括号（模板移除后残留） ──────────────────────────
    # 去掉行首孤立 } 或 {（Infobox/模板嵌套剥离后的碎片）
    t = re.sub(r'^\s*[\}\{]\s*$', '', t, flags=re.MULTILINE)

    # ── 15. 空白整理 ────────────────────────────────────────────────────
    # 行内多余空白
    t = re.sub(r'[ \t]+', ' ', t)
    # 3 个以上空行 → 2 个空行（段落分隔）
    t = re.sub(r'\n{3,}', '\n\n', t)
    # 去掉首尾空白
    t = t.strip()

    return t


# ════════════════════════════════════════════════════════════════════════
# 内部辅助
# ════════════════════════════════════════════════════════════════════════

def _remove_templates(text: str) -> str:
    """去除 {{...}} 模板，处理嵌套。

    策略：逐对匹配 {{ }}，保留模板间的内容。
    """
    result: list[str] = []
    i = 0
    while i < len(text):
        if text[i:i+2] == '{{':
            depth = 1
            j = i + 2
            while j < len(text) and depth > 0:
                if text[j:j+2] == '{{':
                    depth += 1
                    j += 1
                elif text[j:j+2] == '}}':
                    depth -= 1
                    j += 1
                else:
                    j += 1
            i = j + 1 if depth == 0 else j
        else:
            result.append(text[i])
            i += 1
    return ''.join(result)


def _remove_tables(text: str) -> str:
    """去除 {| ... |} 表格块。"""
    result: list[str] = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('{|'):
            in_table = True
            continue
        if in_table and stripped.startswith('|}'):
            in_table = False
            continue
        if not in_table:
            result.append(line)
    return '\n'.join(result)
