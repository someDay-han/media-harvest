"""中央素材库：manifest 读写、文件落盘、used_by 回写。"""

from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .provider_base import MediaItem

KIND_DIR = {"sfx": "audio", "music": "audio", "image": "photo", "video": "video"}
MANIFEST_NAME = "manifest.json"


def kind_dir_name(kind: str) -> str:
    if kind not in KIND_DIR:
        raise ValueError(f"unknown kind: {kind}")
    return KIND_DIR[kind]


def safe_filename(item: MediaItem) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (item.title or "media").lower()).strip("-")[:60]
    ext = _guess_ext(item)
    return f"{item.id}-{slug}{ext}" if slug else f"{item.id}{ext}"


def _guess_ext(item: MediaItem) -> str:
    url = item.download_url.lower()
    for ext in (".mp3", ".mp4", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".webm"):
        if ext in url:
            return ext
    return {"sfx": ".mp3", "music": ".mp3", "image": ".jpg", "video": ".mp4"}[item.kind]


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.manifest_path = self.root / MANIFEST_NAME

    def _ensure_dirs(self) -> None:
        for d in KIND_DIR.values():
            (self.root / d).mkdir(parents=True, exist_ok=True)

    # ---- manifest ----

    def load_manifest(self) -> list[dict[str, Any]]:
        if not self.manifest_path.is_file():
            return []
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def save_manifest(self, records: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def find_record(self, item_id: str) -> dict[str, Any] | None:
        for rec in self.load_manifest():
            if str(rec.get("id")) == str(item_id):
                return rec
        return None

    # ---- 落库 ----

    def store(self, item: MediaItem, data: bytes) -> Path:
        self._ensure_dirs()
        target = self.root / kind_dir_name(item.kind) / safe_filename(item)
        target.write_bytes(data)
        self.upsert_record(item, target)
        return target

    def upsert_record(self, item: MediaItem, file_path: Path) -> dict[str, Any]:
        records = self.load_manifest()
        existing = None
        for rec in records:
            if str(rec.get("id")) == str(item.id):
                existing = rec
                break
        record = existing or {}
        record.update(
            {
                "id": str(item.id),
                "title": item.title,
                "author": item.author,
                "kind": item.kind,
                "page_url": item.page_url,
                "download_url": item.download_url,
                "license": item.license,
                "downloaded_at": record.get(
                    "downloaded_at", _dt.datetime.now().isoformat(timespec="seconds")
                ),
                "file_path": str(file_path),
                "duration": item.duration,
                "width": item.width,
                "height": item.height,
                "tags": item.tags,
                "used_by": record.get("used_by", []),
            }
        )
        if existing is None:
            records.append(record)
        self.save_manifest(records)
        return record

    def mark_used(self, item_id: str, dest_dir: Path) -> dict[str, Any] | None:
        records = self.load_manifest()
        rec = next((r for r in records if str(r.get("id")) == str(item_id)), None)
        if rec is None:
            return None
        dest = str(Path(dest_dir).resolve())
        entry = {"path": dest, "date": _dt.date.today().isoformat()}
        if not any(u.get("path") == dest for u in rec.get("used_by", [])):
            rec.setdefault("used_by", []).append(entry)
            self.save_manifest(records)
        return rec

    # ---- 复制到项目 ----

    def copy_to(self, item_id: str, dest_dir: Path) -> Path | None:
        rec = self.find_record(item_id)
        if rec is None:
            return None
        src = Path(rec["file_path"])
        if not src.is_file():
            raise FileNotFoundError(f"library file missing: {src}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(rec["file_path"]).name
        shutil.copy2(src, dest)
        self.mark_used(item_id, dest_dir)
        return dest

    # ---- 查询 ----

    def list_records(self, kind: str | None = None) -> list[dict[str, Any]]:
        records = self.load_manifest()
        if kind:
            records = [r for r in records if r.get("kind") == kind]
        return records
