"""mediaharvest CLI 入口。

退出码约定：成功 0、无结果 1、通道故障 2、用法错误 3。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .config import Config, apply_ssl_cert
from .library import Library
from .player import PlayerError
from .providers.base import ChannelError
from .providers.pixabay.parse import SEARCH_PATHS

if TYPE_CHECKING:
    from .app import App

EXIT_OK = 0
EXIT_NO_RESULT = 1
EXIT_CHANNEL = 2
EXIT_USAGE = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mediaharvest",
        description="Pixabay 免费素材采集 CLI（sfx/music/image/video，中央库 + 许可留痕）",
    )
    parser.add_argument("--version", action="version", version=f"mediaharvest {__version__}")
    parser.add_argument("--config", help="config.yaml 路径（默认向上查找）")
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="搜索素材")
    p_search.add_argument("query")
    p_search.add_argument("--kind", choices=sorted(SEARCH_PATHS), required=True)
    p_search.add_argument("--page", type=int, default=1)
    p_search.add_argument("--json", action="store_true", help="机器可读输出")
    p_search.add_argument("--no-cache", action="store_true")

    p_open = sub.add_parser("open", help="试听/查看（有图形会话走外部播放器，否则落盘预览）")
    p_open.add_argument("ref", help="序号（最近一次 search）或全局 id")

    p_dl = sub.add_parser("download", help="下载入中央库")
    p_dl.add_argument("ref", help="序号或全局 id")
    p_dl.add_argument("--to", dest="to_dir", help="复制到目标目录（如视频项目 assets）")
    p_dl.add_argument("--json", action="store_true")

    p_lst = sub.add_parser("list", help="翻检中央库")
    p_lst.add_argument("--kind", choices=["sfx", "music", "image", "video"])
    p_lst.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="单条详情")
    p_show.add_argument("item_id")

    sub.add_parser("doctor", help="通道自诊断")
    sub.add_parser("providers", help="列出素材源及状态")

    return parser


def _make_app(args: argparse.Namespace) -> App:
    """App 构造点（测试注入唯一 seam 用）。"""
    config = Config.load(getattr(args, "config", None))
    apply_ssl_cert(config)
    from .app import App

    return App(config, Library(config.library_root))


def _harden_console_encoding() -> None:
    """locale 非 UTF-8（或缺字形的终端）遇 \\u200b 等字符会崩——原位改为替换容错。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(errors="replace")
        except (ValueError, OSError):  # 已关闭/不可变的流
            pass


def main(argv: list[str] | None = None) -> int:
    _harden_console_encoding()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    app = _make_app(args)
    try:
        return _dispatch(app, args)
    except ChannelError as exc:
        print(f"[通道故障] {exc}", file=sys.stderr)
        return EXIT_CHANNEL
    except NoResultError as exc:
        print(f"[无结果] {exc}", file=sys.stderr)
        return EXIT_NO_RESULT
    except PlayerError as exc:
        print(f"[播放器] {exc}", file=sys.stderr)
        return EXIT_CHANNEL
    except LookupError as exc:
        print(f"[未找到] {exc}", file=sys.stderr)
        return EXIT_NO_RESULT


class NoResultError(RuntimeError):
    pass


def _dispatch(app: App, args: argparse.Namespace) -> int:
    if args.command == "search":
        return _cmd_search(app, args)
    if args.command == "open":
        return _cmd_open(app, args)
    if args.command == "download":
        return _cmd_download(app, args)
    if args.command == "list":
        return _cmd_list(app, args)
    if args.command == "show":
        return _cmd_show(app, args)
    if args.command == "doctor":
        return _cmd_doctor(app)
    if args.command == "providers":
        return _cmd_providers(app)
    return EXIT_USAGE


def _cmd_search(app: App, args: argparse.Namespace) -> int:
    items = app.search(args.query, args.kind, args.page, use_cache=not args.no_cache)
    if not items:
        raise NoResultError(f"{args.kind} '{args.query}' page {args.page}")
    app.note_last_search(items)
    if args.json:
        print(json.dumps([app.item_json(i) for i in items], ensure_ascii=False, indent=2))
        return EXIT_OK
    print(_format_items(items))
    print(f"\n共 {len(items)} 条 | open/download 用序号或全局 id")
    return EXIT_OK


def _format_items(items) -> str:
    headers = ["#", "id", "title", "author", "dur/size", "views", "url"]
    rows = []
    for idx, it in enumerate(items, 1):
        size = f"{it.width}x{it.height}" if it.width else it.duration_text()
        url_disp = _truncate(it.download_url, 48) if it.download_url else "(via-detail)"
        rows.append(
            [str(idx), it.id, _truncate(it.title, 38), _truncate(it.author, 16),
             size, str(it.view_count or "-"), url_disp]
        )
    widths = [max(len(r[i]) for r in [headers] + rows) for i in range(len(headers))]
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append("  ".join("-" * w for w in widths))
    for r in rows:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    return "\n".join(out)


def _truncate(s: str, n: int) -> str:
    s = s or "-"
    return s if len(s) <= n else s[: n - 1] + "…"


def _cmd_open(app: App, args: argparse.Namespace) -> int:
    item = app.resolve_ref(args.ref)
    preview = app.open_item(item)
    if preview is not None:
        print(f"无图形会话，已保存预览: {preview}")
        if item.download_url:
            print(f"直链: {item.download_url}")
    return EXIT_OK


def _cmd_download(app: App, args: argparse.Namespace) -> int:
    item = app.resolve_ref(args.ref)
    saved = app.download(item, to_dir=Path(args.to_dir) if args.to_dir else None)
    if args.json:
        print(json.dumps(app.item_json(item) | {"saved": str(saved)}, ensure_ascii=False, indent=2))
    else:
        print(f"已入库: {saved}")
        if args.to_dir:
            print(f"已复制并登记 used_by: {args.to_dir}")
    return EXIT_OK


def _cmd_list(app: App, args: argparse.Namespace) -> int:
    records = app.list_library(args.kind)
    if not records:
        raise NoResultError("中央库为空" + (f"（kind={args.kind}）" if args.kind else ""))
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"{'id':<12} {'kind':<6} {'title':<36} {'author':<16} {'used_by':<8} file")
    for r in records:
        print(
            f"{r.get('id')!s:<12} {r.get('kind', ''):<6} "
            f"{_truncate(r.get('title', ''), 36):<36} {_truncate(r.get('author', ''), 16):<16} "
            f"{len(r.get('used_by', [])):<8} {r.get('file_path', '')}"
        )
    return EXIT_OK


def _cmd_show(app: App, args: argparse.Namespace) -> int:
    record = app.show(args.item_id)
    if record is None:
        raise NoResultError(f"id {args.item_id} 不在中央库")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return EXIT_OK


def _cmd_doctor(app: App) -> int:
    from .doctor import format_report, run_doctor

    report = run_doctor(app.config)
    print(format_report(report))
    return EXIT_OK if report.all_ok else EXIT_CHANNEL


def _cmd_providers(app: App) -> int:
    print("providers:")
    print("  pixabay  v1 已实现（sfx/music/image/video；音频走 bootstrap 逆向，无官方 API）")
    print("  (预留)   第二素材源位（Freesound/Openverse 等，见 provider_base.Provider）")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
