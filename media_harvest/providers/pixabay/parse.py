"""Pixabay 纯函数解析层：无 IO，全部可对 fixture 单测。

四类媒体共用：
- find_bootstrap_url: 从页面 HTML 提取 window.__BOOTSTRAP_URL__
- normalize_results:  bootstrap JSON 的 page.results[] → MediaItem[]
- normalize_video_html: 视频 SSR HTML → MediaItem[]（bootstrap JSON 端点重放不稳，视频不走）
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from ...provider_base import MediaItem

BOOTSTRAP_URL_RE = re.compile(
    r"""window\.__BOOTSTRAP_URL__\s*=\s*["']([^"']+)["']"""
)

SITE = "https://pixabay.com"
CDN_HOST = "cdn.pixabay.com"

# 搜索页路径模板（zh 前缀不影响机制）
SEARCH_PATHS = {
    "sfx": "/sound-effects/search/{q}/",
    "music": "/music/search/{q}/",
    "image": "/images/search/{q}/",
    "video": "/videos/search/{q}/",
}


def search_path(query: str, kind: str, page: int = 1) -> str:
    if kind not in SEARCH_PATHS:
        raise ValueError(f"unknown kind: {kind}")
    path = SEARCH_PATHS[kind].format(q=query.replace(" ", "%20"))
    # 真分页是 ?page=N 查询串；/N/ 路径形态返回 404 空壳页（2026-11-27 实测）
    return f"{path}?page={page}" if page > 1 else path


def find_bootstrap_url(html: str) -> str | None:
    m = BOOTSTRAP_URL_RE.search(html)
    if not m:
        return None
    url = m.group(1)
    return urljoin(SITE, url)


def _author(entry: dict) -> str:
    user = entry.get("user") or {}
    return user.get("username") or ""


def _page_url(entry: dict, kind: str) -> str:
    href = entry.get("href") or ""
    if href and not href.startswith("http"):
        href = urljoin(SITE, href)
    return href


def _download_url(entry: dict, kind: str) -> str:
    sources = entry.get("sources") or {}
    if kind in ("sfx", "music"):
        return sources.get("src") or ""
    if kind == "image":
        # 未登录天花板 1280px：优先 2x
        return sources.get("2x") or sources.get("1x") or ""
    return ""


def _tags(entry: dict) -> list[str]:
    raw = entry.get("tagList") or []
    tags = []
    for t in raw:
        if isinstance(t, str):
            tags.append(t)
        elif isinstance(t, list) and t:
            tags.append(str(t[0]))
    return tags


def normalize_results(payload: dict | str, kind: str) -> list[MediaItem]:
    """bootstrap JSON → MediaItem 列表。sfx/music/image 同构。"""
    if isinstance(payload, str):
        payload = json.loads(payload)
    page = payload.get("page") or {}
    results = page.get("results") or []
    items: list[MediaItem] = []
    for entry in results:
        if not entry.get("id"):
            continue
        items.append(
            MediaItem(
                id=str(entry["id"]),
                kind=kind,
                title=entry.get("name") or entry.get("title") or "",
                author=_author(entry),
                page_url=_page_url(entry, kind),
                download_url=_download_url(entry, kind),
                duration=entry.get("duration"),
                width=entry.get("width"),
                height=entry.get("height"),
                tags=_tags(entry),
                view_count=entry.get("viewCount"),
                download_count=entry.get("downloadCount"),
                extra={
                    "is_ai_generated": entry.get("isAiGenerated"),
                    "media_descriptive_type": entry.get("mediaDescriptiveType"),
                },
            )
        )
    return items


# ---- jina markdown 提取（三级降级的兜底数据源） ----

# jina 渲染代理输出 markdown：条目形如
# [Title](https://pixabay.com/sound-effects/slug-<id>/)[Author](https://pixabay.com/users/...)
_JINA_ENTRY_RE = re.compile(
    r"\[([^\]]+)\]\((https://pixabay\.com/(?:sound-effects|music|photos|images|illustrations|vectors|videos)/[a-z0-9%-]+-(\d+)/)\)"
    r"\[([^\]]*)\]\(https://pixabay\.com/users/[^)]+\)"
)


def kind_from_href(href: str) -> str:
    if "/sound-effects/" in href:
        return "sfx"
    if "/music/" in href:
        return "music"
    if "/videos/" in href:
        return "video"
    return "image"


def normalize_jina_markdown(text: str, kind: str) -> list[MediaItem]:
    """jina markdown → MediaItem[]（无直链；download_url 留空）。

    兜底用：playwright 与直连都挂时，至少还能列出候选（id/标题/作者），
    下载时由调用方决定是否用主通道重取详情。
    """
    items: list[MediaItem] = []
    seen: set[str] = set()
    for m in _JINA_ENTRY_RE.finditer(text):
        title, href, vid, author = m.group(1), m.group(2), m.group(3), m.group(4)
        if vid in seen:
            continue
        seen.add(vid)
        if kind_from_href(href) != kind:
            continue
        items.append(
            MediaItem(
                id=vid,
                kind=kind,
                title=title or f"media {vid}",
                author=author,
                page_url=href,
                download_url="",
                extra={"via": "jina"},
            )
        )
    return items


def extract_content_url(html: str) -> str | None:
    """详情页 HTML → ld+json contentUrl（CDN 直链）。

    jina HTML 模式下详情页数据在 <script type="application/ld+json"> 里：
    sfx/music=AudioObject，video=VideoObject，image=ImageObject。
    坏 JSON / 无 contentUrl 的块跳过；找不到返回 None。
    """
    for m in re.finditer(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
    ):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        url = data.get("contentUrl") if isinstance(data, dict) else None
        if url and CDN_HOST in url:
            return url
    return None


# ---- 图片 SSR/DOM 提取 ----

# 渲染后 DOM：条目 href 是 /photos|illustrations|vectors/...-<id>/
_IMG_HREF_RE = re.compile(
    r'href="(/(?:photos|images|illustrations|vectors)/[a-z0-9-]+-(\d+)/)"'
)
_IMG_SRC_RE = re.compile(
    r'<img[^>]+src="(https://cdn\.pixabay\.com/(?:photo|illustration|vector)/[^"]+)"'
)
_IMG_ALT_RE = re.compile(r'<img[^>]+alt="([^"]*)"')


def normalize_image_html(html: str, query: str = "") -> list[MediaItem]:
    """图片搜索页渲染后 HTML → MediaItem[]。

    DOM 里每个条目：href="/photos/slug-<id>/" + 紧邻 <img src="cdn..._<size>.jpg" alt="...">。
    直链就是可下载 URL（_640/_1280 免登录）。
    """
    items: list[MediaItem] = []
    seen: set[str] = set()
    pos = 0
    while True:
        m = _IMG_HREF_RE.search(html, pos)
        if not m:
            break
        href, vid = m.group(1), m.group(2)
        if vid in seen:
            pos = m.end()
            continue
        seen.add(vid)
        # href 后的窗口里找它的 img（blank.gif 占位跳过，取下一个 cdn img）
        window = html[m.end() : m.end() + 4000]
        src_m = _IMG_SRC_RE.search(window)
        alt_m = _IMG_ALT_RE.search(window)
        src = ""
        width = height = None
        if src_m:
            src = src_m.group(1).replace("&amp;", "&")
            # URL 尾段 _640 / _1280 → 推尺寸
            dm = re.search(r"_(\d+)\.(?:jpg|png|webp)$", src)
            if dm:
                # 只有宽度信息；高度未知（保持 None，宽给 URL 标称值）
                width = int(dm.group(1))
        title = alt_m.group(1).replace("Free ", "").replace(" photo and picture", "").replace(
            " illustration and picture", ""
        ) if alt_m else ""
        items.append(
            MediaItem(
                id=vid,
                kind="image",
                title=title or f"image {vid}",
                author="",
                page_url=urljoin(SITE, href),
                download_url=src,
                width=width,
                height=height,
                extra={"query": query},
            )
        )
        pos = m.end()
    return items

_VIDEO_HREF_RE = re.compile(r'href="(/videos/[a-z0-9-]+-(\d+)/)"')
# 封面：新格式 .../2024/12/04/244839_tiny.jpg（无 hash），旧格式 .../2023/03/04/153167-817186036_tiny.jpg
_CDN_COVER_RE = re.compile(
    r"https://cdn\.pixabay\.com/video/(\d{4}/\d{2}/\d{2})/(\d+(?:-\d+)?)_tiny\.jpg"
)
_DURATION_RE = re.compile(r">(\d+):(\d\d)<")


def normalize_video_html(html: str, query: str = "") -> list[MediaItem]:
    """视频搜索页 SSR HTML → MediaItem[]。

    直链构造：封面 cdn .../video/YYYY/MM/DD/<id>-<hash>_tiny.jpg 的路径段
    与 large.mp4 完全一致（_large.mp4 免登录实测 200）。
    """
    items: list[MediaItem] = []
    seen_ids: set[str] = set()

    for m in _VIDEO_HREF_RE.finditer(html):
        href, vid = m.group(1), m.group(2)
        if vid in seen_ids:
            continue
        seen_ids.add(vid)

        # 以该条目 href 为界，取其后的一个窗口找封面与时长（避免跨条目错配）
        window = html[m.end() : m.end() + 6000]
        cover = _CDN_COVER_RE.search(window)
        dur = _DURATION_RE.search(window)

        slug = href[len("/videos/") : -(len(vid) + 3)]
        title = slug.replace("-", " ") if slug else f"video {vid}"
        download_url = ""
        if cover:
            base = f"https://cdn.pixabay.com/video/{cover.group(1)}/{cover.group(2)}"
            download_url = f"{base}_large.mp4"
        duration = None
        if dur:
            duration = int(dur.group(1)) * 60 + int(dur.group(2))

        items.append(
            MediaItem(
                id=vid,
                kind="video",
                title=title,
                author="",  # SSR 卡片作者提取不稳定，留空
                page_url=urljoin(SITE, href),
                download_url=download_url,
                duration=duration,
                extra={"query": query},
            )
        )
    return items
