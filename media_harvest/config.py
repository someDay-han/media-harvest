"""配置加载：config.yaml（可选）+ 内建默认值合并（Linux / XDG 约定）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

APP_NAME = "media_harvest"
DEFAULT_CONFIG_YAML = "config.yaml"


def _xdg_dir(env_var: str, fallback: str) -> Path:
    base = os.environ.get(env_var)
    return Path(base) if base else Path.home() / fallback


def default_library_root() -> Path:
    return Path.home() / "media" / "pixabay"


def default_cache_dir() -> Path:
    return _xdg_dir("XDG_CACHE_HOME", ".cache") / APP_NAME


def default_state_dir() -> Path:
    return _xdg_dir("XDG_STATE_HOME", ".local/state") / APP_NAME


def default_config_path() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / APP_NAME / DEFAULT_CONFIG_YAML


_BUILTIN_DEFAULTS: dict[str, Any] = {
    "pixabay": {"api_key": ""},
    "fetch": {
        "channel": "playwright",
        "proxy": "",  # 本机直连；如需代理填 http://host:port
        "ssl_cert_file": "",  # 仅 MITM 代理环境需要证书链
        "min_interval": 1.0,
        "jina_prefix": "https://r.jina.ai/",
    },
    "player": {"command": ""},  # 空 = 自动探测 ffplay / mpv / vlc
    "library": {"root": str(default_library_root())},
    "cache": {"dir": str(default_cache_dir()), "ttl_hours": 24},
    "state": {"dir": str(default_state_dir())},
}


@dataclass
class Config:
    api_key: str = ""
    channel: str = "playwright"
    proxy: str = ""
    ssl_cert_file: str = ""
    min_interval: float = 1.0
    jina_prefix: str = "https://r.jina.ai/"
    player_command: str = ""
    library_root: Path = field(default_factory=default_library_root)
    cache_dir: Path = field(default_factory=default_cache_dir)
    state_dir: Path = field(default_factory=default_state_dir)
    ttl_hours: int = 24
    source_path: Path | None = None

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> Config:
        path = Path(config_path).expanduser() if config_path else _find_default_config()
        raw: dict[str, Any] = {}
        if path and path.is_file():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        def sec(name: str) -> dict[str, Any]:
            value = raw.get(name)
            return {**_BUILTIN_DEFAULTS.get(name, {}), **(value or {})}

        def as_path(value: Any, fallback: Path) -> Path:
            return Path(str(value)).expanduser() if value else fallback

        pixabay = sec("pixabay")
        fetch = sec("fetch")
        player = sec("player")
        library = sec("library")
        cache = sec("cache")
        state = sec("state")

        return cls(
            api_key=str(pixabay.get("api_key") or ""),
            channel=str(fetch.get("channel") or "playwright"),
            proxy=str(fetch.get("proxy") or ""),
            ssl_cert_file=str(fetch.get("ssl_cert_file") or "").strip(),
            min_interval=float(fetch.get("min_interval") or 1.0),
            jina_prefix=str(fetch.get("jina_prefix") or "https://r.jina.ai/"),
            player_command=str(player.get("command") or "").strip(),
            library_root=as_path(library.get("root"), default_library_root()),
            cache_dir=as_path(cache.get("dir"), default_cache_dir()),
            state_dir=as_path(state.get("dir"), default_state_dir()),
            ttl_hours=int(cache.get("ttl_hours") or 24),
            source_path=path if (path and path.is_file()) else None,
        )


def _find_default_config() -> Path | None:
    """查找顺序：./config.yaml 向上两级 → XDG 配置目录。"""
    cwd = Path.cwd()
    for candidate in (cwd, cwd.parent, cwd.parent.parent):
        p = candidate / DEFAULT_CONFIG_YAML
        if p.is_file():
            return p
    return default_config_path()


def apply_ssl_cert(config: Config) -> None:
    """MITM 代理环境：httpx 需要额外证书链（默认不启用）。"""
    if config.ssl_cert_file and Path(config.ssl_cert_file).expanduser().is_file():
        os.environ.setdefault("SSL_CERT_FILE", config.ssl_cert_file)
