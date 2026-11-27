"""Provider 接口定义（第二素材源预留位）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MediaItem:
    """归一化后的媒体条目——所有 provider 都产出这个形状。"""

    id: str
    kind: str  # sfx | music | image | video
    title: str
    author: str
    page_url: str
    download_url: str  # 直链（CDN）
    duration: int | None = None  # 秒（音频/视频）
    width: int | None = None
    height: int | None = None
    tags: list[str] = field(default_factory=list)
    view_count: int | None = None
    download_count: int | None = None
    license: str = "Pixabay Content License"
    extra: dict[str, Any] = field(default_factory=dict)

    def duration_text(self) -> str:
        if self.duration is None:
            return "-"
        m, s = divmod(int(self.duration), 60)
        return f"{m}:{s:02d}"


class Provider(ABC):
    """素材源接口。v1 只有 Pixabay 实现。"""

    name: str

    @abstractmethod
    def search(self, query: str, kind: str, page: int) -> list[MediaItem]: ...

    @abstractmethod
    def download_bytes(self, item: MediaItem) -> bytes: ...

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """doctor 用：返回各通道探测结果。"""
