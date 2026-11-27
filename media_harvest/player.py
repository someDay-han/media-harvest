"""外部播放/看图（Linux）：有图形会话走 ffplay/mpv/xdg-open，无则落盘预览。"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

AUDIO_KINDS = {"sfx", "music"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus"}


class PlayerError(RuntimeError):
    pass


def has_display() -> bool:
    """是否有图形会话（决定「能不能打开给人看/听」）。"""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def resolve_player(command: str = "") -> list[str] | None:
    """播放器命令：配置优先，其次自动探测 ffplay / mpv / vlc；都没有返回 None。"""
    if command.strip():
        argv = shlex.split(command)
        if not argv:
            return None
        exe = argv[0] if "/" in argv[0] and Path(argv[0]).is_file() else shutil.which(argv[0])
        if exe is None:
            raise PlayerError(f"player.command 不可执行: {argv[0]}")
        return [exe, *argv[1:]]
    for name in ("ffplay", "mpv", "vlc"):
        exe = shutil.which(name)
        if exe:
            return [exe]
    return None


def open_with_default_viewer(file_path: Path) -> None:
    """图片：交给系统看图程序（xdg-open / gio）。"""
    if not has_display():
        raise PlayerError("无图形会话（DISPLAY/WAYLAND_DISPLAY 为空）")
    xdg = shutil.which("xdg-open")
    if xdg:
        argv = [xdg, str(file_path)]
    else:
        gio = shutil.which("gio")
        if not gio:
            raise PlayerError("未找到看图程序（安装 xdg-utils）")
        argv = [gio, "open", str(file_path)]
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_preview(directory: Path, filename: str, data: bytes) -> Path:
    """预览文件落盘（同名覆盖，避免重复试听堆满缓存）。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    target.write_bytes(data)
    return target


def _is_audio(target: str, kind: str) -> bool:
    if kind in AUDIO_KINDS:
        return True
    if kind in {"image", "video"}:
        return False
    return Path(target.split("?", 1)[0]).suffix.lower() in AUDIO_EXTS


class Player:
    def __init__(self, command: str = ""):
        self.command = command

    def backend(self) -> list[str] | None:
        return resolve_player(self.command)

    def available(self) -> bool:
        """是否真能「打开」：既有图形会话又有播放器。"""
        return has_display() and self.backend() is not None

    def _spawn(self, target: str, kind: str) -> None:
        if not has_display():
            raise PlayerError("无图形会话（DISPLAY/WAYLAND_DISPLAY 为空）")
        backend = self.backend()
        if backend is None:
            raise PlayerError(
                "未找到播放器：安装 ffmpeg(ffplay)/mpv/vlc，或在 config.yaml 设置 player.command"
            )
        name = Path(backend[0]).name
        if name.startswith("ffplay"):
            args = ["-autoexit", "-loglevel", "error", "-window_title", "media_harvest"]
            if _is_audio(target, kind):
                args.append("-nodisp")
            argv = [*backend, *args, target]
        elif name.startswith("mpv"):
            argv = [*backend, "--really-quiet", target]
        else:
            argv = [*backend, target]
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stream(self, url: str, kind: str = "") -> None:
        """流式播放 URL（音频/视频），不落盘。"""
        self._spawn(url, kind)

    def play_file(self, file_path: Path, kind: str = "") -> None:
        self._spawn(str(file_path), kind)
