"""搜索缓存：TTL 24h（ToS 要求），键 = query+kind+page。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class SearchCache:
    def __init__(self, cache_dir: Path, ttl_hours: int = 24):
        self.dir = Path(cache_dir)
        self.ttl = ttl_hours * 3600

    def _key(self, query: str, kind: str, page: int) -> Path:
        raw = f"{query}|{kind}|{page}".lower()
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return self.dir / f"search-{kind}-{digest}.json"

    def get(self, query: str, kind: str, page: int) -> list[dict[str, Any]] | None:
        path = self._key(query, kind, page)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if time.time() - payload.get("ts", 0) > self.ttl:
            return None
        return payload.get("items")

    def put(self, query: str, kind: str, page: int, items: list[dict[str, Any]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._key(query, kind, page)
        path.write_text(
            json.dumps({"ts": time.time(), "items": items}, ensure_ascii=False),
            encoding="utf-8",
        )
