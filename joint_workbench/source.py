"""对站点/办公室数据源的薄客户端。"""
from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import SourceError


class SourceClient:
    """带状态码访问的数据源客户端。

    所有方法都不吞掉 4xx/5xx：需要区分 404（未到）、410（入口关闭）、
    409（代次替换）和 429（整包名额用尽）的调用方可以自行判断。
    """

    def __init__(self, base: str, timeout: float = 5.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    # -- 底层 ---------------------------------------------------------
    def fetch(self, path: str, payload: Any = None, raw: bool = False) -> tuple[int, Any, dict[str, str]]:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, headers=headers)
        try:
            response = urlopen(request, timeout=self.timeout)
            status = response.status
            kind = response.headers.get("Content-Type", "")
            body = response.read()
            headers = dict(response.headers.items())
        except HTTPError as error:
            status = error.code
            kind = error.headers.get("Content-Type", "") if error.headers else ""
            headers = dict(error.headers.items()) if error.headers else {}
            body = error.read()
        except URLError as error:
            raise SourceError(0, str(error.reason), path) from error
        if raw or not kind.startswith("application/json"):
            return status, body, headers
        try:
            return status, json.loads(body), headers
        except ValueError:
            return status, body, headers

    def get(self, path: str, query: dict[str, Any] | None = None) -> tuple[int, Any]:
        if query:
            path = path + "?" + urlencode(query)
        status, body, _ = self.fetch(path)
        return status, body

    def get_json(self, path: str, query: dict[str, Any] | None = None) -> Any:
        status, body = self.get(path, query)
        if status != 200:
            raise SourceError(status, body, path)
        return body

    def post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        status, body, _ = self.fetch(path, payload)
        return status, body

    # -- 便捷读取 -----------------------------------------------------
    def window(self) -> dict[str, Any]:
        return self.get_json("/")

    def campaigns(self) -> list[dict[str, Any]]:
        return self.get_json("/campaigns")["campaigns"]

    def cards(self) -> list[dict[str, Any]]:
        return self.get_json("/cards")["cards"]

    def catalog_page(self, station: str, season: str, edition: str | None = None, after: int = 0):
        query = {"station": station, "season": season, "after": after}
        if edition is not None:
            query["edition"] = edition
        return self.get("/catalog", query)

    def receipts(self) -> list[dict[str, Any]]:
        return self.get_json("/office/receipts")["receipts"]

    def object(self, ident: str):
        return self.fetch(f"/object/{ident}", raw=True)

    def archive(self, ident: str):
        return self.fetch(f"/archive/{ident}", raw=True)

    def submit(self, payload: dict[str, Any]):
        return self.post("/office/submit", payload)

    def advance(self, window: int):
        status, body = self.post("/clock", {"window": window})
        if status != 200:
            raise SourceError(status, body, "/clock")
        return body
