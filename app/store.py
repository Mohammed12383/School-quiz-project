"""JSON file storage with debounced, atomic saves."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger("difaf.store")

SAVE_DELAY = 1.0  # seconds


def resolve_data_dir(base_dir: Path) -> Path:
    env = os.environ.get("DATA_DIR", "").strip()
    if env:
        return Path(env)
    server_disk = Path("/data")
    if server_disk.is_dir() and os.access(server_disk, os.W_OK):
        return server_disk
    return base_dir / "data"


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict = self._load()
        self._dirty = False
        self._handle: asyncio.TimerHandle | None = None

    def _read(self, path: Path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("root is not an object")
        return data

    def _load(self) -> dict:
        backup = self.path.with_name(self.path.stem + ".backup.json")
        if self.path.exists():
            try:
                data = self._read(self.path)
                shutil.copyfile(self.path, backup)  # last good copy from previous run
                return data
            except Exception as exc:  # corrupted file: keep it aside, try the backup
                broken = self.path.with_name(f"{self.path.stem}.broken-{int(time.time())}.json")
                log.error("db file unreadable (%s); moved to %s", exc, broken.name)
                os.replace(self.path, broken)
        if backup.exists():
            try:
                log.warning("restoring data from %s", backup.name)
                return self._read(backup)
            except Exception as exc:
                log.error("backup unreadable too: %s", exc)
        return {}

    def mark_dirty(self) -> None:
        self._dirty = True
        if self._handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (tests); call save_now() explicitly
        self._handle = loop.call_later(SAVE_DELAY, self._on_timer)

    def _on_timer(self) -> None:
        self._handle = None
        self.save_now()

    def replace(self, data: dict) -> None:
        self.data = data
        self.mark_dirty()

    def save_now(self) -> None:
        if not self._dirty:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError as exc:
            log.error("could not save data: %s", exc)
            # try again a bit later
            try:
                loop = asyncio.get_running_loop()
                if self._handle is None:
                    self._handle = loop.call_later(5.0, self._on_timer)
            except RuntimeError:
                pass
