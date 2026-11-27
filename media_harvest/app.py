"""App：CLI 之下的应用服务层——串起 gateway/cache/library/player。

可注入假 gateway（唯一 seam），CLI 端到端测试全程无网络。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .cache import SearchCache
from .config import Config
from .library import Library, safe_filename
from .player import Player, PlayerError, has_display, open_with_default_viewer, write_preview
from .provider_base import MediaItem
from .providers.pixabay.gateway import PixabayGateway

LAST_SEARCH_FILE = "last-search.json"


class App:
    def __init__(self, config: Config, library: Library, gateway=None):
        self.config = config
        self.library = library
        self.gateway = gateway or PixabayGateway(config)
        self.cache = SearchCache(config.cache_dir, config.ttl_hours)
        self.player = Player(config.player_command)

    # ---- search ----

    def search(self, query: str, kind: str, page: int, use_cache: bool = True) -> list[MediaItem]:
        if use_cache:
            cached = self.cache.get(query, kind, page)
            if cached:  # 空结果视为 miss（避免通道抖动时缓存住空页）
                return [self._item_from_json(c) for c in cached]
        items, _fetch = self.gateway.search(query, kind, page)
        if items:  # 只缓存非空结果
            self.cache.put(query, kind, page, [self.item_json(i) for i in items])
        return items

    def note_last_search(self, items: list[MediaItem]) -> None:
        path = self._last_search_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([self.item_json(i) for i in items], ensure_ascii=False),
            encoding="utf-8",
        )

    def _last_search_path(self) -> Path:
        return self.config.state_dir / LAST_SEARCH_FILE

    # ---- 引用解析：序号（最近搜索）优先，其次全局 id（清单→库） ----

    def resolve_ref(self, ref: str) -> MediaItem:
        last = self._load_last_search()
        if last and ref.isdigit() and 1 <= int(ref) <= len(last):
            return self._item_from_json(last[int(ref) - 1])
        by_id = next((i for i in last if i["id"] == ref), None)
        if by_id:
            return self._item_from_json(by_id)
        rec = self.library.find_record(ref)
        if rec:
            return self._item_from_json(rec)
        raise LookupError(f"无法解析引用 '{ref}'（序号失效请重新 search，或用全局 id）")

    def _load_last_search(self) -> list[dict[str, Any]]:
        p = self._last_search_path()
        if not p.is_file():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _ensure_download_url(self, item: MediaItem) -> MediaItem:
        """jina 兜底条目无直链：经详情页解析补上。"""
        if item.download_url:
            return item
        url = self.gateway.resolve_download_url(item.page_url)
        item.download_url = url
        return item

    # ---- open ----

    def open_item(self, item: MediaItem) -> Path | None:
        """试听/查看。

        返回 None 表示已交给外部播放器/看图程序；
        返回 Path 表示当前无图形会话，已落盘预览（CLI 打印路径与直链）。
        """
        item = self._ensure_download_url(item)
        preview_dir = self.config.cache_dir / "preview"

        if not has_display():
            data = self.gateway.fetch_bytes(item.download_url)
            return write_preview(preview_dir, safe_filename(item), data)

        if item.kind == "image":
            data = self.gateway.fetch_bytes(item.download_url)
            path = write_preview(preview_dir, safe_filename(item), data)
            open_with_default_viewer(path)
            return None

        # 音频/视频：先流式，播放器起不来再落盘用本地文件播
        try:
            self.player.stream(item.download_url, kind=item.kind)
            return None
        except PlayerError:
            data = self.gateway.fetch_bytes(item.download_url)
            path = write_preview(preview_dir, safe_filename(item), data)
            self.player.play_file(path, kind=item.kind)
            return None

    # ---- download ----

    def download(self, item: MediaItem, to_dir: Path | None = None) -> Path:
        item = self._ensure_download_url(item)
        existing = self.library.find_record(item.id)
        if existing and Path(existing["file_path"]).is_file():
            saved = Path(existing["file_path"])
        else:
            data = self.gateway.fetch_bytes(item.download_url)
            saved = self.library.store(item, data)
        if to_dir:
            self.library.copy_to(item.id, to_dir)
        return saved

    # ---- list / show ----

    def list_library(self, kind: str | None) -> list[dict[str, Any]]:
        return self.library.list_records(kind)

    def show(self, item_id: str) -> dict[str, Any] | None:
        return self.library.find_record(item_id)

    # ---- 序列化 ----

    def item_json(self, item: MediaItem) -> dict[str, Any]:
        if is_dataclass(item):
            return asdict(item)
        return dict(item)

    def _item_from_json(self, data: dict[str, Any]) -> MediaItem:
        known = {f for f in MediaItem.__dataclass_fields__}
        extra = {k: v for k, v in data.items() if k not in known}
        fields = {k: v for k, v in data.items() if k in known}
        # manifest 旧记录可能缺 download_url 等必有字段：占位空串（用时经详情页补）
        for name, f in MediaItem.__dataclass_fields__.items():
            if name not in fields and (f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING):
                fields[name] = ""
        if extra:
            fields["extra"] = {**fields.get("extra", {}), **extra}
        return MediaItem(**fields)
