"""doctor：三层通道自诊断 + 配置有效性校验。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .player import PlayerError, has_display, resolve_player


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str
    elapsed: float = 0.0
    optional: bool = False


@dataclass
class DoctorReport:
    probes: list[ProbeResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        """必检项全过即算健康；optional 探针失败只提示。"""
        return all(p.ok for p in self.probes if not p.optional)

    def add(self, probe: ProbeResult) -> None:
        self.probes.append(probe)


def _timed(fn: Callable[[], str]) -> tuple[bool, str, float]:
    start = time.monotonic()
    try:
        return True, fn(), time.monotonic() - start
    except Exception as exc:  # noqa: BLE001 - 探测需要宽捕获
        return False, f"{type(exc).__name__}: {exc}", time.monotonic() - start


def run_doctor(config: Config) -> DoctorReport:
    report = DoctorReport()

    # 1. 配置有效性（库根可写、目录可建、证书若填必须存在）
    def check_config() -> str:
        problems: list[str] = []
        if config.ssl_cert_file and not Path(config.ssl_cert_file).expanduser().is_file():
            problems.append(f"SSL 证书不存在: {config.ssl_cert_file}")
        dirs = (
            ("中央库", config.library_root),
            ("缓存", config.cache_dir),
            ("状态", config.state_dir),
        )
        for label, raw in dirs:
            path = Path(raw).expanduser()
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                problems.append(f"{label}目录不可创建: {path} ({exc})")
                continue
            if not os_access(path):
                problems.append(f"{label}目录不可写: {path}")
        if problems:
            raise RuntimeError("; ".join(problems))
        return f"config OK（库根 {config.library_root}）"

    ok, detail, t = _timed(check_config)
    report.add(ProbeResult("config", ok, detail, t))

    # 2. CDN 直连（免登录核心能力）
    ok, detail, t = _timed(lambda: _probe_cdn(config))
    report.add(ProbeResult("cdn-direct", ok, detail, t))

    # 3. Playwright + 代理主通道
    if config.channel == "playwright":
        ok, detail, t = _timed(lambda: _probe_playwright(config))
        report.add(ProbeResult("playwright+proxy", ok, detail, t))

    # 4. jina 兜底
    ok, detail, t = _timed(lambda: _probe_jina(config))
    report.add(ProbeResult("jina-fallback", ok, detail, t))

    # 5. 播放器（无图形会话时为可选：open 会落盘预览）
    ok, detail, t = _timed(lambda: _probe_player(config))
    report.add(ProbeResult("player", ok, detail, t, optional=True))

    return report


def os_access(path: Path) -> bool:
    import os

    try:
        return os.access(path, os.W_OK)
    except OSError:
        return False


def _probe_cdn(config: Config) -> str:
    """用一个已知的音频直链 HEAD 探测（不下载本体）。"""
    import httpx

    url = "https://cdn.pixabay.com/audio/2026/10/14/audio_775054915a.mp3"
    headers = {"User-Agent": "mediaharvest-doctor"}
    proxy = config.proxy or None
    with httpx.Client(headers=headers, proxy=proxy, timeout=15.0, follow_redirects=True, trust_env=False) as client:
        resp = client.head(url)
        if resp.status_code == 200:
            return f"CDN 可达 200（出口 {config.proxy or '直连'}）"
        raise RuntimeError(f"CDN HEAD status {resp.status_code}")


def _probe_playwright(config: Config) -> str:
    from .providers.pixabay.gateway import PixabayGateway

    gw = PixabayGateway(config)
    result = gw.fetch_page("/sound-effects/search/whoosh/")
    if "__BOOTSTRAP_URL__" in result.text:
        return f"通道 '{result.channel}' 拿到 bootstrap URL"
    raise RuntimeError(f"通道 '{result.channel}' 页面无 bootstrap URL")


def _probe_jina(config: Config) -> str:
    import httpx

    prefix = config.jina_prefix.rstrip("/")
    url = f"{prefix}/https://pixabay.com/sound-effects/search/whoosh/"
    proxy = config.proxy or None
    with httpx.Client(proxy=proxy, timeout=60.0, follow_redirects=True, trust_env=False) as client:
        resp = client.get(url)
        if resp.status_code == 200 and len(resp.text) > 1000:
            return f"jina 兜底可达（{len(resp.text)} 字符）"
        raise RuntimeError(f"jina status {resp.status_code}")


def _probe_player(config: Config) -> str:
    if not has_display():
        return "无图形会话，open 将落盘到缓存预览目录"
    try:
        backend = resolve_player(config.player_command)
    except PlayerError as exc:
        raise RuntimeError(str(exc)) from exc
    if backend is None:
        raise RuntimeError("有图形会话但未找到播放器（安装 ffmpeg/mpv/vlc 或配置 player.command）")
    return f"播放器 {Path(backend[0]).name}"


def format_report(report: DoctorReport) -> str:
    lines = []
    mark = {True: "OK  ", False: "FAIL"}
    for p in report.probes:
        suffix = " (可选)" if p.optional else ""
        lines.append(f"[{mark[p.ok]}] {p.name:<18} {p.elapsed:5.1f}s  {p.detail}{suffix}")
    return "\n".join(lines)
