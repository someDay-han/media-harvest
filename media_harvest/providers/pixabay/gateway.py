"""PixabayGateway —— 全工具唯一的对外 IO 缝。

四能力：
1. fetch_page:      搜索页 HTML（三级降级：playwright+代理 → 直连 → jina）
2. fetch_bootstrap: bootstrap JSON（先从 HTML 发现 __BOOTSTRAP_URL__）
3. fetch_bytes:     CDN 字节（永远直连优先，失败才走代理）
4. 限速：请求间隔 ≥ min_interval

测试时整类替换为假实现（唯一 seam，用户已确认）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from ...config import Config
from ..base import ChannelError, FetchResult
from .parse import (
    CDN_HOST,
    extract_content_url,
    find_bootstrap_url,
    normalize_image_html,
    normalize_jina_markdown,
    normalize_results,
    normalize_video_html,
    search_path,
)

JUA_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class PixabayGateway:
    def __init__(self, config: Config):
        self.config = config
        self._last_request_ts: float = 0.0
        # Playwright 页面里的 Cloudflare cookie（__cf_bm/_sp2_*）；
        # bootstrap JSON 裸请求必 403，带上它才 200（2026-11-27 实测）
        self._cf_cookies: dict[str, str] = {}

    # ---- 限速 ----

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self.config.min_interval - (now - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    # ---- 能力 1+2: 页面与 bootstrap（三级降级） ----

    def fetch_page(self, path: str) -> FetchResult:
        """抓站点页面 HTML。channel 顺序由 config，默认 playwright → direct → jina。

        playwright 是主通道但 CF challenge 有间歇性，先在同层重试一次再降级。
        """
        errors: list[str] = []
        if self.config.channel == "playwright":
            for attempt in (1, 2):
                try:
                    html = self._fetch_via_playwright(path)
                    return FetchResult(html, channel="playwright")
                except Exception as exc:  # noqa: BLE001 - 降级链需要宽捕获
                    errors.append(f"playwright#{attempt}: {exc}")
        try:
            html = self._fetch_direct(path)
            return FetchResult(html, channel="direct")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"direct: {exc}")
        html = self._fetch_jina(path)
        return FetchResult(html, channel="jina", errors=errors)

    def fetch_bootstrap(self, path: str) -> tuple[list[dict[str, Any]], FetchResult]:
        """搜索页 → bootstrap URL → JSON → page.results[]。"""
        page = self.fetch_page(path)
        if page.channel != "playwright":
            # jina 返回的是 markdown 化文本，bootstrap URL 可能仍可提取
            boot_url = find_bootstrap_url(page.text)
            if boot_url is None:
                raise RuntimeError(
                    f"no __BOOTSTRAP_URL__ in page via '{page.channel}'"
                    + (f" ({'; '.join(page.errors)})" if page.errors else "")
                )
        else:
            boot_url = find_bootstrap_url(page.text)
            if boot_url is None:
                raise RuntimeError("no __BOOTSTRAP_URL__ in search page HTML")

        payload = self._fetch_json(boot_url, via_proxy=page.channel != "playwright")
        results = payload.get("page", {}).get("results", [])
        return results, page

    # ---- 能力 3: CDN 字节 ----

    def fetch_bytes(self, url: str) -> bytes:
        """CDN 直链下载：免登录免 token；代理（若配置）与直连各试一次。"""
        if CDN_HOST not in url:
            raise ValueError(f"refusing non-CDN download: {url}")
        self._throttle()
        last_exc: Exception | None = None
        for use_proxy in (True, False):
            try:
                return self._http_get_bytes(url, use_proxy=use_proxy)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        raise ChannelError(f"CDN download failed for {url}: {last_exc}") from last_exc

    def resolve_download_url(self, item_page_url: str) -> str:
        """jina 兜底条目无直链：经 jina 取详情页，解析 ld+json 的 contentUrl
        抠 CDN 直链。

        通道顺序（2026-11-27 实测）：
        1. GET html 模式（X-Timeout=60 让 jina 浏览器过 CF，4/4；无此头 3/5）
        2. POST 模式（缓存键与 GET 独立：GET 缓存住的 CF 壳无法 bust，
           query/路径变体全无效；POST 稳定成功）
        long-slug 页面在 jina 可能缓存死锁，末两段短 slug（canonical）作候选。"""
        path = item_page_url.replace("https://pixabay.com", "") if item_page_url.startswith("http") else item_page_url
        candidates = [path]
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) == 2 and parts[1].count("-") > 2:
            slug = parts[1].rstrip("/")
            tail = "-".join(slug.split("-")[-3:])
            candidates.append(f"/{parts[0]}/{tail}/")
        problems: list[str] = []
        for candidate in candidates:
            for use_post in (False, True):
                try:
                    html = self._fetch_jina(candidate, html_mode=True, post=use_post)
                except Exception as exc:  # noqa: BLE001 - 通道故障换下一个
                    problems.append(f"{candidate} {'POST' if use_post else 'GET'}: {exc}")
                    continue
                url = extract_content_url(html)
                if url:
                    return url
                is_shell = "Just a moment" in html[:5000]
                problems.append(f"{candidate} {'POST' if use_post else 'GET'}: {'cf shell' if is_shell else 'no contentUrl'}")
        raise ChannelError(f"no ld+json contentUrl on detail page: {item_page_url} ({'; '.join(problems)})")

    # ---- search 门面 ----

    def search(self, query: str, kind: str, page: int) -> tuple[list[Any], FetchResult]:
        """sfx/music 走 bootstrap JSON；image/video 走浏览器 DOM 收集
        （image/video 的 bootstrap JSON 端点被 CF 独立 challenge，实测 403）。"""
        path = search_path(query, kind, page=page)
        if kind in ("image", "video"):
            page_result = self.fetch_page(path)
            if kind == "video":
                items = normalize_video_html(page_result.text, query)
            else:
                items = normalize_image_html(page_result.text, query)
            if not items and page_result.channel == "jina":
                items = normalize_jina_markdown(page_result.text, kind)
            return items, page_result
        try:
            results, page_result = self.fetch_bootstrap(path)
            items = normalize_results({"page": {"results": results}}, kind)
            return items, page_result
        except Exception as exc:
            page_result = self._jina_fallback(path, errors=[str(exc)])
            items = normalize_jina_markdown(page_result.text, kind)
            if not items:
                raise
            return items, page_result

    def _jina_fallback(self, path: str, errors: list[str]) -> FetchResult:
        """jina 兜底 + 一次瞬态重试（jina 5xx/代理拥堵是间歇性的）。

        两次都挂时抛带完整链条的错误：bootstrap 原因 + 两轮 jina 失败详情，
        满足"立刻知道断在哪一层"。
        """
        for attempt in (1, 2):
            try:
                return FetchResult(self._fetch_jina(path), channel="jina", errors=errors)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"jina#{attempt}: {exc}")
        raise ChannelError("all channels down: " + "; ".join(errors)) from None

    # ---- 传输细节 ----

    def _http_client(self, use_proxy: bool) -> httpx.Client:
        headers = {"User-Agent": JUA_UA, "Referer": "https://pixabay.com/"}
        proxy = self.config.proxy if use_proxy and self.config.proxy else None
        return httpx.Client(
            headers=headers,
            proxy=proxy,
            timeout=45.0,
            follow_redirects=True,
            trust_env=False,  # 机器有带认证的全局代理环境变量，一律忽略
        )

    def _http_get_bytes(self, url: str, use_proxy: bool, cookies: dict[str, str] | None = None) -> bytes:
        self._throttle()
        with self._http_client(use_proxy) as client:
            resp = client.get(url, cookies=cookies or None)
            resp.raise_for_status()
            return resp.content

    def _fetch_direct(self, path: str) -> str:
        self._throttle()
        with self._http_client(use_proxy=True) as client:
            resp = client.get(f"https://pixabay.com{path}")
            resp.raise_for_status()
            if "Just a moment" in resp.text[:2000]:
                raise RuntimeError("cloudflare challenge (direct)")
            return resp.text

    def _fetch_jina(self, path: str, html_mode: bool = False, engine: str | None = None, post: bool = False) -> str:
        self._throttle()
        prefix = self.config.jina_prefix.rstrip("/")
        target = f"https://pixabay.com{path}"
        headers = {"User-Agent": JUA_UA, "Accept": "text/html"}
        if html_mode:
            # X-Timeout=60：详情页过 CF 需要时间，无此头实测 3/5 被 CF 壳卡（2026-11-27）
            headers["X-Return-Format"] = "html"
            headers["X-Timeout"] = "60"
        if engine:
            headers["X-Engine"] = engine
        proxy = self.config.proxy or None
        with httpx.Client(headers=headers, proxy=proxy, timeout=90.0, follow_redirects=True) as client:
            if post:
                # POST 缓存键与 GET 独立：GET 被缓存 CF 壳污染时，POST 能重新抓取
                headers["Content-Type"] = "application/json"
                resp = client.post(prefix + "/", json={"url": target})
            else:
                resp = client.get(f"{prefix}/{target}")
            resp.raise_for_status()
            return resp.text

    def _launch_browser(self, p):
        """launch：本机 Chrome 过 CF 最稳（Playwright 自带 Chromium 指纹会被 challenge 卡死，
        实测 2026-11-26）；channel=chrome 失败再退回自带 chromium。"""
        proxy = {"server": self.config.proxy} if self.config.proxy else None
        try:
            return p.chromium.launch(headless=True, channel="chrome", proxy=proxy)
        except Exception as exc:  # noqa: BLE001 - 本机无 Chrome 时退回自带浏览器
            try:
                return p.chromium.launch(headless=True, proxy=proxy)
            except Exception as fallback_exc:
                raise RuntimeError(
                    "无可用浏览器：安装 google-chrome（推荐）或执行 playwright install chromium"
                    f"（chrome: {exc}; chromium: {fallback_exc}）"
                ) from fallback_exc

    def _fetch_via_playwright(self, path: str) -> str:
        self._throttle()
        url = f"https://pixabay.com{path}"
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = self._launch_browser(p)
            try:
                context = browser.new_context(user_agent=JUA_UA, locale="en-US")
                pg = context.new_page()
                pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                # CF challenge 轮询求解：challenge 页自己会重试跳转，最多 60s。
                # 关键：挑战求解后页面会自动 reload 成真页面。
                for _ in range(20):
                    pg.wait_for_timeout(3000)
                    html = pg.content()
                    title = ""
                    tm = re.search(r"<title>([^<]*)</title>", html[:5000])
                    if tm:
                        title = tm.group(1)
                    if "Just a moment" not in title and len(html) > 50000:
                        break
                # 图片/视频页：bootstrap JSON 端点被 CF 独立拦截，数据靠前端渲染。
                # 等 hydration 完成 + 条目卡片出现（最多 30s），再取 HTML。
                try:
                    pg.wait_for_function(
                        "() => document.body.className.includes('hydration-complete')",
                        timeout=20000,
                    )
                except Exception:  # noqa: BLE001, S110 - 有些页面没有该标记，静默继续
                    pass
                for _ in range(6):
                    if "/photos/" in html or "/videos/" in html or "/sound-effects/" in html or "/music/" in html:
                        break
                    pg.wait_for_timeout(5000)
                    html = pg.content()
                html = pg.content()
                self._cf_cookies = {c["name"]: c["value"] for c in context.cookies()}
                context.close()
            finally:
                browser.close()
        if "__BOOTSTRAP_URL__" not in html and "Just a moment" in html[:2000]:
            raise RuntimeError("cloudflare challenge survived (playwright)")
        return html

    def _fetch_json(self, url: str, via_proxy: bool) -> dict[str, Any]:
        """bootstrap JSON 端点：httpx 直取（可重放，实测 sfx/music 均可）；失败重试一轮再 playwright。"""
        if url.startswith("/"):
            url = f"https://pixabay.com{url}"
        last_exc: Exception | None = None
        cookies = self._cf_cookies or None
        # 两轮：代理/直连各两次，容忍代理间歇拥堵（连接超时）
        for round_no in range(2):
            for use_proxy in (True, False):
                try:
                    content = self._http_get_bytes(url, use_proxy=use_proxy, cookies=cookies)
                    return json.loads(content)
                except Exception as exc:  # noqa: BLE001 - 收集后走 playwright 兜底
                    last_exc = exc
        try:
            return self._fetch_json_via_playwright(url)
        except Exception:  # noqa: BLE001
            raise ChannelError(f"bootstrap JSON fetch failed: {last_exc}") from last_exc

    def _fetch_json_via_playwright(self, url: str) -> dict[str, Any]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = self._launch_browser(p)
            try:
                context = browser.new_context(user_agent=JUA_UA)
                if self._cf_cookies:
                    context.add_cookies(
                        [
                            {"name": k, "value": v, "domain": ".pixabay.com", "path": "/"}
                            for k, v in self._cf_cookies.items()
                        ]
                    )
                # 用 API request context（浏览器导航到 JSON 会 ERR_HTTP_RESPONSE_CODE_FAILURE）
                req = context.request
                resp = req.get(url, headers={"User-Agent": JUA_UA, "Referer": "https://pixabay.com/"}, fail_on_status_code=False, timeout=45000)
                if resp.status >= 400:
                    raise RuntimeError(f"bootstrap endpoint status {resp.status}")
                body = resp.text()
                context.close()
            finally:
                browser.close()
        return json.loads(body)
