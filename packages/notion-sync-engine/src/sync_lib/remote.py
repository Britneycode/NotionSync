"""Notion API 封装：令牌桶限流、指数退避重试与 code block 读写。"""

import random
import time
from threading import Lock

from notion_client import Client

from .canonical import split_rich_text


class RateLimiter:
    """令牌桶限流器"""
    def __init__(self, rate: float, burst: int = 1):
        self.rate = rate  # 每秒令牌数
        self.burst = burst  # 桶容量
        self.tokens = burst
        self.last_update = time.time()
        self.lock = Lock()

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
                time.sleep(sleep_time)


def retry_with_backoff(func, max_retries=6, base_delay=1.0):
    """指数退避重试"""
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

            time.sleep(delay)

    raise Exception("Max retries exceeded")


class NotionRemote:
    def __init__(self, client: Client, database_id: str, data_source_id: str):
        self.client = client
        self.database_id = database_id
        self.data_source_id = data_source_id
        self.limiter = RateLimiter(rate=2.5)

    def create_page(self, path, content, file_type, directory, chars):
        """创建页面"""
        self.limiter.wait()

        # 切分 rich_text
        chunks = split_rich_text(content)
        rich_text = [{"type": "text", "text": {"content": chunk}} for chunk in chunks]

        response = retry_with_backoff(lambda: self.client.pages.create(
            parent={"data_source_id": self.data_source_id},
            properties={
                "Name": {"title": [{"text": {"content": path}}]},
                "Path": {"rich_text": [{"text": {"content": path}}]},
                "Type": {"select": {"name": file_type}},
                "Directory": {"rich_text": [{"text": {"content": directory}}]},
                "Chars": {"number": chars},
            },
            children=[{
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": rich_text,
                    "language": self._map_language(file_type),
                },
            }],
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

            response = retry_with_backoff(lambda: self.client.data_sources.query(
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
            response = retry_with_backoff(lambda: self.client.data_sources.query(
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
        return retry_with_backoff(lambda: self.client.pages.update(
            page_id=page_id, in_trash=True,
        ))

    def update_page_properties(self, page_id, properties):
        """更新页面属性（RENAME 用：Name / Path / Directory）"""
        self.limiter.wait()
        return retry_with_backoff(lambda: self.client.pages.update(
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

    def read_page(self, page_id):
        """读取页面正文（分页拉全 children 后做结构校验）"""
        blocks = []
        start_cursor = None
        while True:
            self.limiter.wait()
            kwargs = {"block_id": page_id, "page_size": 100}
            if start_cursor is not None:
                kwargs["start_cursor"] = start_cursor

            response = retry_with_backoff(
                lambda: self.client.blocks.children.list(**kwargs),
            )
            blocks.extend(response["results"])

            if not response.get("has_more"):
                break
            start_cursor = response.get("next_cursor")

        code_blocks = [b for b in blocks if b["type"] == "code"]

        # 结构校验
        if len(code_blocks) != 1 or len(blocks) != 1:
            return None, None, False

        block = code_blocks[0]
        rich_text = block["code"]["rich_text"]
        content = "".join(rt["plain_text"] for rt in rich_text)

        return content, block["id"], True

    def update_block(self, block_id: str, content: str) -> dict:
        """替换 code block 的全部 rich_text 内容（按 UTF-16 码元切分，≤2000/段）"""
        from .canonical import split_rich_text
        self.limiter.wait()
        chunks = split_rich_text(content, max_units=2000)
        rich_text = [{"type": "text", "text": {"content": c}} for c in chunks]
        return retry_with_backoff(
            lambda: self.client.blocks.update(block_id=block_id, code={"rich_text": rich_text}),
        )

    def create_database(self, parent_page_id: str) -> tuple[str, str]:
        """在指定 page 下创建同步数据库，返回 (database_id, data_source_id)"""
        self.limiter.wait()

        # notion_client 3.1.0 / API 2025-09-03：database 下挂 data_source，
        # 属性需置于 initial_data_source.properties（顶层无 properties 参数）。
        response = retry_with_backoff(lambda: self.client.databases.create(
            parent={"type": "page_id", "page_id": parent_page_id},
            title=[{"type": "text", "text": {"content": "Notion Sync"}}],
            initial_data_source={"properties": {"Name": {"title": {}}}},
        ))

        database_id = response["id"]
        data_source_id = response["data_sources"][0]["id"]
        return database_id, data_source_id

    def _map_language(self, file_type):
        """映射文件类型到 Notion language"""
        mapping = {
            "markdown": "markdown",
            "python": "python",
            "yaml": "yaml",
            "toml": "toml",
        }
        return mapping.get(file_type, "plain text")
