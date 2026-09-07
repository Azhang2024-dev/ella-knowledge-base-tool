"""
datasource_wikipedia.py — Wikipedia DataSource Adapter

支持 Wikipedia FR / EN / 其他语言版本。
通过 MediaWiki API 枚举页面并获取正文，无需浏览器。

枚举方式：
  - 按分类（category）：list=categorymembers + 递归子分类
  - 按前缀（prefix）：generator=allpages
  - 按标题列表（titles）：直接指定页面标题

用法：
  adapter = WikipediaAdapter(lang="fr")
  async for item in adapter.enumerate_items(config, log, stop, pause):
      print(item.title)
"""

import asyncio
from typing import AsyncIterator

import requests

from core.datasource import ItemRecord
from core.wikitext_cleaner import wikitext_to_text

# MediaWiki 礼貌请求间隔（秒）
_DEFAULT_INTERVAL = 0.3
# 单次 API 请求最大返回数（普通用户 500）
_MAX_PER_REQUEST = 500
# 递归子分类最大深度
_MAX_CATEGORY_DEPTH = 5

_USER_AGENT = (
    "Ella-KnowledgeBase-Tool/1.0 "
    "(https://github.com/example/ella; research data collection)"
)


class WikipediaAdapter:
    """Wikipedia MediaWiki API Adapter。

    Config keys:
        wiki_lang:           语言代码（fr, en, de, ...），默认 "fr"
        wiki_enum_mode:      枚举方式："category" | "prefix" | "titles"
        wiki_categories:     分类名列表（每行一个），不含 "Catégorie:" 前缀
        wiki_category_depth: 子分类递归深度，默认 1
        wiki_prefix:         标题前缀
        wiki_titles:         标题列表（每行一个）
        wiki_max_per_cat:    每分类最多页面数，默认 500
        wiki_content_mode:   "full" (wikitext全文) | "intro" (仅引言)，默认 "full"
        request_interval:    请求间隔秒数，默认 0.3
    """

    def __init__(self, lang: str = "fr"):
        self._lang = lang
        self._base = f"https://{lang}.wikipedia.org/w/api.php"
        self._session: requests.Session | None = None

    @property
    def name(self) -> str:
        return f"wikipedia_{self._lang}"

    # ── enumerate_items ──────────────────────────────────────────────────

    async def enumerate_items(
        self, config: dict, log, stop_checker, pause_checker
    ) -> AsyncIterator[ItemRecord]:
        mode = config.get("wiki_enum_mode", "category")

        if mode == "category":
            async for item in self._enum_by_category(config, log, stop_checker, pause_checker):
                yield item
        elif mode == "prefix":
            async for item in self._enum_by_prefix(config, log, stop_checker, pause_checker):
                yield item
        elif mode == "titles":
            async for item in self._enum_by_titles(config, log, stop_checker, pause_checker):
                yield item
        else:
            if log:
                log(f"[{self.name}] 未知枚举方式: {mode}，回退到 category")
            async for item in self._enum_by_category(config, log, stop_checker, pause_checker):
                yield item

    # ── fetch_content ────────────────────────────────────────────────────

    async def fetch_content(self, item: ItemRecord, config: dict, log) -> str:
        content_mode = config.get("wiki_content_mode", "full")

        if content_mode == "intro":
            return await self._fetch_intro(item, config, log)
        else:
            return await self._fetch_full(item, config, log)

    # ════════════════════════════════════════════════════════════════════
    # 枚举方式实现
    # ════════════════════════════════════════════════════════════════════

    async def _enum_by_category(
        self, config: dict, log, stop_checker, pause_checker
    ) -> AsyncIterator[ItemRecord]:
        """按分类枚举：先递归收集子分类，再枚举每个分类的成员页面。"""
        raw_cats = config.get("wiki_categories", "")
        if isinstance(raw_cats, str):
            cat_names = [c.strip() for c in raw_cats.splitlines() if c.strip()]
        else:
            cat_names = [str(c).strip() for c in (raw_cats or []) if str(c).strip()]

        if not cat_names:
            if log:
                log(f"[{self.name}] 未指定分类名，请填写 wiki_categories")
            return

        depth = int(config.get("wiki_category_depth", 1))
        max_per_cat = int(config.get("wiki_max_per_cat", 500))

        # 递归收集所有分类（含子分类）
        all_cats: list[str] = []
        for cat_name in cat_names:
            if stop_checker and stop_checker():
                break
            if log:
                log(f"[{self.name}] 发现分类：{cat_name}")
            all_cats.append(cat_name)
            if depth > 1:
                subcats = await self._get_subcategories(
                    cat_name, depth - 1, log, stop_checker, pause_checker
                )
                all_cats.extend(subcats)

        # 去重
        all_cats = list(dict.fromkeys(all_cats))
        if log:
            log(f"[{self.name}] 共 {len(all_cats)} 个分类（含子分类 {depth} 层）")

        # 枚举每个分类的页面
        seen_ids: set[str] = set()
        for cat_name in all_cats:
            if stop_checker and stop_checker():
                break
            if pause_checker:
                while pause_checker():
                    await asyncio.sleep(0.3)

            full_title = f"Catégorie:{cat_name}" if not cat_name.startswith("Catégorie:") else cat_name
            if log:
                log(f"[{self.name}] 枚举分类：{full_title}")

            count = 0
            cmcontinue = None
            while count < max_per_cat:
                if stop_checker and stop_checker():
                    break

                params = {
                    "action": "query",
                    "list": "categorymembers",
                    "cmtitle": full_title,
                    "cmtype": "page",
                    "cmlimit": min(_MAX_PER_REQUEST, max_per_cat - count),
                    "format": "json",
                }
                if cmcontinue:
                    params["cmcontinue"] = cmcontinue

                data = self._api_get(params, log)
                if not data:
                    break

                pages = data.get("query", {}).get("categorymembers", [])
                for p in pages:
                    pid = str(p.get("pageid", ""))
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    count += 1
                    yield ItemRecord(
                        id=f"{self.name}:{pid}",
                        title=p.get("title", ""),
                        source_url=f"https://{self._lang}.wikipedia.org/wiki/{p['title'].replace(' ', '_')}",
                        metadata={
                            "lang": self._lang,
                            "pageid": pid,
                            "ns": p.get("ns", 0),
                            "category": cat_name,
                        },
                    )

                cmcontinue = data.get("continue", {}).get("cmcontinue")
                if not cmcontinue:
                    break
                await asyncio.sleep(_DEFAULT_INTERVAL)

    async def _enum_by_prefix(
        self, config: dict, log, stop_checker, pause_checker
    ) -> AsyncIterator[ItemRecord]:
        """按标题前缀枚举所有页面。"""
        prefix = config.get("wiki_prefix", "").strip()
        if not prefix:
            if log:
                log(f"[{self.name}] 未指定前缀，请填写 wiki_prefix")
            return

        max_pages = int(config.get("wiki_max_per_cat", 500))
        if log:
            log(f"[{self.name}] 枚举前缀：{prefix}（最多 {max_pages} 页）")

        count = 0
        gapcontinue = None
        seen_ids: set[str] = set()

        while count < max_pages:
            if stop_checker and stop_checker():
                break

            params = {
                "action": "query",
                "generator": "allpages",
                "gapprefix": prefix,
                "gapfilterredir": "nonredirects",
                "gaplimit": min(_MAX_PER_REQUEST, max_pages - count),
                "prop": "info",
                "format": "json",
            }
            if gapcontinue:
                params["gapcontinue"] = gapcontinue

            data = self._api_get(params, log)
            if not data:
                break

            pages = data.get("query", {}).get("pages", {})
            for pid, p in pages.items():
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                count += 1
                yield ItemRecord(
                    id=f"{self.name}:{pid}",
                    title=p.get("title", ""),
                    source_url=f"https://{self._lang}.wikipedia.org/wiki/{p['title'].replace(' ', '_')}",
                    metadata={
                        "lang": self._lang,
                        "pageid": pid,
                        "ns": p.get("ns", 0),
                        "prefix": prefix,
                    },
                )

            gapcontinue = data.get("continue", {}).get("gapcontinue")
            if not gapcontinue:
                break
            await asyncio.sleep(_DEFAULT_INTERVAL)

    async def _enum_by_titles(
        self, config: dict, log, stop_checker, pause_checker
    ) -> AsyncIterator[ItemRecord]:
        """按指定标题列表枚举。"""
        raw = config.get("wiki_titles", "")
        if isinstance(raw, str):
            titles = [t.strip() for t in raw.splitlines() if t.strip()]
        else:
            titles = [str(t).strip() for t in (raw or []) if str(t).strip()]

        if not titles:
            if log:
                log(f"[{self.name}] 未指定标题，请填写 wiki_titles")
            return

        if log:
            log(f"[{self.name}] 枚举指定标题：{len(titles)} 个")

        # 批量查询：每次 50 个标题
        for batch_start in range(0, len(titles), 50):
            if stop_checker and stop_checker():
                break
            batch = titles[batch_start: batch_start + 50]
            params = {
                "action": "query",
                "titles": "|".join(batch),
                "prop": "info",
                "format": "json",
            }
            data = self._api_get(params, log)
            if not data:
                continue

            pages = data.get("query", {}).get("pages", {})
            for pid, p in pages.items():
                if pid == "-1":
                    continue  # 页面不存在
                yield ItemRecord(
                    id=f"{self.name}:{pid}",
                    title=p.get("title", ""),
                    source_url=f"https://{self._lang}.wikipedia.org/wiki/{p['title'].replace(' ', '_')}",
                    metadata={
                        "lang": self._lang,
                        "pageid": pid,
                        "ns": p.get("ns", 0),
                    },
                )
            await asyncio.sleep(_DEFAULT_INTERVAL)

    # ════════════════════════════════════════════════════════════════════
    # 内容获取
    # ════════════════════════════════════════════════════════════════════

    async def _fetch_full(self, item: ItemRecord, config: dict, log) -> str:
        """获取完整 Wikitext → 清洗为纯文本。"""
        title = item.title
        params = {
            "action": "parse",
            "page": title,
            "prop": "wikitext",
            "format": "json",
            "formatversion": "2",
        }
        data = self._api_get(params, log)
        if not data:
            return ""

        wikitext = data.get("parse", {}).get("wikitext", "")
        if not wikitext:
            return ""

        return wikitext_to_text(wikitext)

    async def _fetch_intro(self, item: ItemRecord, config: dict, log) -> str:
        """仅获取引言（exintro）。"""
        title = item.title
        params = {
            "action": "query",
            "prop": "extracts",
            "exintro": "1",
            "explaintext": "1",
            "titles": title,
            "format": "json",
            "formatversion": "2",
        }
        data = self._api_get(params, log)
        if not data:
            return ""

        pages = data.get("query", {}).get("pages", [])
        if pages:
            return pages[0].get("extract", "")
        return ""

    # ════════════════════════════════════════════════════════════════════
    # 子分类递归
    # ════════════════════════════════════════════════════════════════════

    async def _get_subcategories(
        self, cat_name: str, depth: int, log, stop_checker, pause_checker
    ) -> list[str]:
        """递归获取子分类名列表。"""
        if depth <= 0:
            return []

        full_title = f"Catégorie:{cat_name}" if not cat_name.startswith("Catégorie:") else cat_name
        subcats: list[str] = []

        cmcontinue = None
        while True:
            if stop_checker and stop_checker():
                break

            params = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": full_title,
                "cmtype": "subcat",
                "cmlimit": _MAX_PER_REQUEST,
                "format": "json",
            }
            if cmcontinue:
                params["cmcontinue"] = cmcontinue

            data = self._api_get(params, log)
            if not data:
                break

            members = data.get("query", {}).get("categorymembers", [])
            for m in members:
                name = m.get("title", "")
                # 去掉 "Catégorie:" 前缀
                if name.startswith("Catégorie:"):
                    name = name[len("Catégorie:"):]
                if name and name not in subcats:
                    subcats.append(name)

            cmcontinue = data.get("continue", {}).get("cmcontinue")
            if not cmcontinue:
                break
            await asyncio.sleep(_DEFAULT_INTERVAL)

        if log and subcats:
            log(f"[{self.name}]   ├─ {cat_name} → {len(subcats)} 个子分类")

        # 递归获取更深层子分类
        if depth > 1:
            for subcat in list(subcats):
                if pause_checker:
                    while pause_checker():
                        await asyncio.sleep(0.3)
                deeper = await self._get_subcategories(
                    subcat, depth - 1, log, stop_checker, pause_checker
                )
                subcats.extend(deeper)

        return list(dict.fromkeys(subcats))  # 去重保序

    # ════════════════════════════════════════════════════════════════════
    # HTTP 工具
    # ════════════════════════════════════════════════════════════════════

    def _api_get(self, params: dict, log) -> dict | None:
        """同步 GET MediaWiki API，返回解析后的 JSON dict；失败返回 None。"""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": _USER_AGENT})

        full_params = {**params, "format": "json"}
        try:
            resp = self._session.get(
                self._base, params=full_params, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                err = data["error"]
                msg = err.get("info", str(err))
                if log:
                    log(f"  [API 错误] {msg}")
                return None
            return data
        except Exception as e:
            if log:
                log(f"  [HTTP 错误] {e}")
            return None
