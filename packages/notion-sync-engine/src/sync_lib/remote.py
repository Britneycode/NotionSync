"""Notion API 封装：令牌桶限流、指数退避重试与 code block 读写。"""

import random
import time
from threading import Lock

from notion_client import Client

from .canonical import split_rich_text

# Notion 单个 code block 的 rich_text 上限：100 个元素 × 每元素 2000 UTF-16 码元。
# 正文超出该容量时必须拆成多个 code block，读回时按块顺序拼接还原。
MAX_RICH_TEXT_ELEMENTS = 100
MAX_UNITS_PER_ELEMENT = 2000
MAX_BLOCK_UNITS = MAX_RICH_TEXT_ELEMENTS * MAX_UNITS_PER_ELEMENT


def split_into_blocks(content: str) -> list[str]:
    """按单块容量把正文切成若干 code block 的文本（顺序拼接可无损还原）"""
    return split_rich_text(content, max_units=MAX_BLOCK_UNITS)


def _rich_text(text: str) -> list[dict]:
    """切分为 Notion rich_text 数组（每元素 ≤2000 UTF-16 码元）"""
    return [
        {"type": "text", "text": {"content": chunk}}
        for chunk in split_rich_text(text, max_units=MAX_UNITS_PER_ELEMENT)
    ]


class RateLimiter:
    """令牌桶限流器"""
    def __init__(self, rate: float, burst: int = 1, waiter=None):
        self.rate = rate  # 每秒令牌数
        self.burst = burst  # 桶容量
        self.tokens = burst
        self.last_update = time.time()
        self.lock = Lock()
        # 等待函数可注入：同步流程传 CancelToken.sleep，等待期间可被取消。
        # 传 None 时在构造处解析 time.sleep，这样 `patch("time.sleep")` 仍然生效。
        self.waiter = waiter or time.sleep

    def wait(self):
        """等待直到有可用令牌"""
        with self.lock:
            while True:
                now = time.time()
                elapsed = now - self.last_update
                self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
                self.last_update = now

                if self.tokens >= 1:
                    self.tokens -= 1
                    return

                # 等待到下一个令牌
                sleep_time = (1 - self.tokens) / self.rate
                self.waiter(sleep_time)


def retry_with_backoff(func, max_retries=6, base_delay=1.0, waiter=None):
    """指数退避重试"""
    # 同上：在调用处解析 time.sleep，保证 `patch("time.sleep")` 依然拦得住
    waiter = waiter or time.sleep
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            # 检查是否可重试
            error_msg = str(e)
            if "401" in error_msg:
                raise  # token 无效，不重试
            if "400" in error_msg:
                raise  # 请求参数错误，不重试

            # 最后一次尝试
            if attempt == max_retries - 1:
                raise

            # 计算延迟
            if hasattr(e, "headers") and "Retry-After" in e.headers:
                delay = int(e.headers["Retry-After"])
            else:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)

            waiter(delay)

    raise Exception("Max retries exceeded")


class NotionRemote:
    def __init__(self, client: Client, database_id: str, data_source_id: str, cancel=None):
        self.client = client
        self.database_id = database_id
        self.data_source_id = data_source_id
        # cancel 传入 CancelToken 时，限流与退避的等待都能被立刻打断
        self._waiter = cancel.sleep if cancel is not None else None
        self.limiter = RateLimiter(rate=2.5, waiter=self._waiter)

    def _retry(self, func, **kwargs):
        """retry_with_backoff 的实例包装：统一带上可取消的等待函数。"""
        return retry_with_backoff(func, waiter=self._waiter, **kwargs)

    def _code_block(self, text: str, language: str) -> dict:
        """构造 code block（正文超出单块容量时由调用方拆成多个）"""
        return {
            "object": "block",
            "type": "code",
            "code": {
                "rich_text": _rich_text(text),
                "language": language,
            },
        }

    def create_page(self, path, content, file_type, directory, chars):
        """创建页面（正文超出单块容量时拆成多个 code block）"""
        self.limiter.wait()

        language = self._map_language(file_type)
        children = [self._code_block(chunk, language) for chunk in split_into_blocks(content)]

        response = self._retry(lambda: self.client.pages.create(
            parent={"data_source_id": self.data_source_id},
            properties={
                "Name": {"title": [{"text": {"content": path}}]},
                "Path": {"rich_text": [{"text": {"content": path}}]},
                "Type": {"select": {"name": file_type}},
                "Directory": {"rich_text": [{"text": {"content": directory}}]},
                "Chars": {"number": chars},
            },
            children=children,
        ))
        return response["id"]

    def query_candidates(self, since_iso):
        """分页拉取 data source 下全部页面，返回候选列表。

        since_iso 非 None 时按 last_edited_time 过滤（含边界）。
        返回每页 {"id", "path", "last_edited_time", "in_trash"}。
        """
        results = []
        start_cursor = None
        while True:
            self.limiter.wait()
            body = {}
            if since_iso is not None:
                body["filter"] = {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"on_or_after": since_iso},
                }
            if start_cursor is not None:
                body["start_cursor"] = start_cursor

            response = self._retry(lambda: self.client.data_sources.query(
                self.data_source_id, **body,
            ))

            for page in response["results"]:
                results.append({
                    "id": page["id"],
                    "path": self._extract_path(page),
                    "last_edited_time": page.get("last_edited_time"),
                    "in_trash": page.get("in_trash", False),
                })

            if not response.get("has_more"):
                break
            start_cursor = response.get("next_cursor")

        return results

    def query_by_paths(self, paths):
        """按 Path 精确查 page_id，返回 {path: page_id}，未命中不含该键。"""
        mapping = {}
        for path in paths:
            self.limiter.wait()
            response = self._retry(lambda: self.client.data_sources.query(
                self.data_source_id,
                filter={"property": "Path", "rich_text": {"equals": path}},
            ))
            results = response["results"]
            if results:
                mapping[path] = results[0]["id"]
        return mapping

    def archive_page(self, page_id):
        """归档页面（移入回收站）"""
        self.limiter.wait()
        return self._retry(lambda: self.client.pages.update(
            page_id=page_id, in_trash=True,
        ))

    def update_page_properties(self, page_id, properties):
        """更新页面属性（RENAME 用：Name / Path / Directory）"""
        self.limiter.wait()
        return self._retry(lambda: self.client.pages.update(
            page_id=page_id, properties=properties,
        ))

    @staticmethod
    def _extract_path(page):
        """取 page.properties["Path"] 首个 rich_text 的 plain_text，缺失/空 → None"""
        rich_text = page.get("properties", {}).get("Path", {}).get("rich_text", [])
        if not rich_text:
            return None
        plain = rich_text[0].get("plain_text")
        return plain if plain else None

    @staticmethod
    def _page_title(page) -> str:
        """取页面的标题：properties 里第一个 type == "title" 的属性，拼 plain_text。

        search 返回的 page 对象没有顶层 title——标题是 properties 里那个 title 型属性
        （独立页面叫 title，挂在数据库下的行叫 Name）。取不到就给空串。
        """
        for prop in page.get("properties", {}).values():
            if prop.get("type") != "title":
                continue
            return "".join(rt.get("plain_text", "") for rt in prop.get("title", []))
        return ""

    def _list_children(self, page_id: str) -> list:
        """分页拉全页面的全部子块（按文档顺序）"""
        blocks = []
        start_cursor = None
        while True:
            self.limiter.wait()
            kwargs = {"block_id": page_id, "page_size": 100}
            if start_cursor is not None:
                kwargs["start_cursor"] = start_cursor

            response = self._retry(
                lambda: self.client.blocks.children.list(**kwargs),
            )
            blocks.extend(response["results"])

            if not response.get("has_more"):
                break
            start_cursor = response.get("next_cursor")

        return blocks

    def read_page(self, page_id):
        """读取页面正文（分页拉全 children，按块顺序拼接后做结构校验）"""
        blocks = self._list_children(page_id)

        # 结构校验：正文必须非空且全部由 code block 组成
        if not blocks or any(b["type"] != "code" for b in blocks):
            return None, None, False

        content = "".join(
            rt["plain_text"]
            for block in blocks
            for rt in block["code"]["rich_text"]
        )

        # 首块 id 作为状态里的 block_id：就地替换时保持稳定
        return content, blocks[0]["id"], True

    def update_block(self, block_id: str, content: str) -> dict:
        """替换单个 code block 的全部 rich_text 内容（按 UTF-16 码元切分，≤2000/段）"""
        self.limiter.wait()
        rich_text = _rich_text(content)
        return self._retry(
            lambda: self.client.blocks.update(block_id=block_id, code={"rich_text": rich_text}),
        )

    def update_page_content(self, page_id: str, content: str):
        """把页面正文整体替换为 content，必要时增删 code block。

        返回写响应体；只有单块整页时才覆盖全文（可直接用于重算 BR），
        涉及多块或增删块时返回 None，由调用方读回远端重算。
        """
        existing = self._list_children(page_id)
        if not existing or any(b["type"] != "code" for b in existing):
            return None

        chunks = split_into_blocks(content)
        language = existing[0]["code"].get("language") or "plain text"

        # 就地替换已有块：块数不变时首块 id 保持稳定，状态里的 block_id 仍然有效
        resp = None
        for block, chunk in zip(existing, chunks):
            resp = self.update_block(block["id"], chunk)

        # 内容变长 → 追加新块；变短 → 删除多余块
        for chunk in chunks[len(existing):]:
            self._append_code_block(page_id, chunk, language)
        for block in existing[len(chunks):]:
            self._delete_block(block["id"])

        if len(existing) == 1 and len(chunks) == 1:
            return resp
        return None

    def _append_code_block(self, page_id: str, text: str, language: str) -> dict:
        """在页面末尾追加一个 code block"""
        self.limiter.wait()
        return self._retry(lambda: self.client.blocks.children.append(
            block_id=page_id,
            children=[self._code_block(text, language)],
        ))

    def _delete_block(self, block_id: str) -> dict:
        """删除子块（正文变短时清理多余的 code block）"""
        self.limiter.wait()
        return self._retry(lambda: self.client.blocks.delete(block_id=block_id))

    def create_database(self, parent_page_id: str) -> tuple[str, str]:
        """在指定 page 下创建同步数据库，返回 (database_id, data_source_id)"""
        self.limiter.wait()

        # notion_client 3.1.0 / API 2025-09-03：database 下挂 data_source，
        # 属性需置于 initial_data_source.properties（顶层无 properties 参数）。
        response = self._retry(lambda: self.client.databases.create(
            parent={"type": "page_id", "page_id": parent_page_id},
            title=[{"type": "text", "text": {"content": "Notion Sync"}}],
            initial_data_source={"properties": {
                "Name": {"title": {}},
                "Path": {"rich_text": {}},
                "Type": {"select": {"options": [
                    {"name": n} for n in ("markdown", "python", "yaml", "toml", "text")
                ]}},
                "Directory": {"rich_text": {}},
                "Chars": {"number": {}},
            }},
        ))

        database_id = response["id"]
        data_source_id = response["data_sources"][0]["id"]
        return database_id, data_source_id

    def search_pages(self) -> list[dict]:
        """列出 integration 能访问的全部页面，返回 [{"id", "title", "url"}]。

        只读，给界面的「选择目标页面」用。search 只返回**被显式连接给 integration**
        的页面，所以列表为空通常意味着还没在页面右上角把 integration 连上。
        按标题排序，让选择列表稳定好找（search 自身只能按 last_edited_time 排）。
        """
        pages = []
        start_cursor = None
        while True:
            self.limiter.wait()
            body = {
                "filter": {"property": "object", "value": "page"},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
                "page_size": 100,
            }
            if start_cursor is not None:
                body["start_cursor"] = start_cursor

            response = self._retry(lambda: self.client.search(**body))

            for page in response["results"]:
                pages.append({
                    "id": page["id"],
                    "title": self._page_title(page),
                    "url": page.get("url", ""),
                })

            if not response.get("has_more"):
                break
            start_cursor = response.get("next_cursor")

        pages.sort(key=lambda p: p["title"].casefold())
        return pages

    def _map_language(self, file_type):
        """映射文件类型到 Notion language"""
        mapping = {
            "markdown": "markdown",
            "python": "python",
            "yaml": "yaml",
            "toml": "toml",
        }
        return mapping.get(file_type, "plain text")
