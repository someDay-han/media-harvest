"""Gateway 层公共类型（provider 之间共享的最小 IO 抽象）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FetchResult:
    """一次页面获取的结果与所用通道。"""

    text: str
    channel: str  # playwright | direct | jina
    errors: list[str] = field(default_factory=list)


class ChannelError(RuntimeError):
    """传输通道全挂（bootstrap/CDN/jina 降级链走完）。CLI 映射为 exit 2。"""
