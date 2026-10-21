"""Storage with debounced saves: a JSON file, or one JSON document in Postgres.

Locally the data lives in data/db.json. On hosting where the disk is wiped
(e.g. Render's free plan) set DATABASE_URL and the same document is kept in
Postgres instead, so nothing is lost when the server sleeps or restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

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

    def close(self) -> None:
        self.save_now()


class PostgresStore(Store):
    """The whole database as one JSON document in a Postgres table.

    Saves are serialised on the event-loop thread (a consistent snapshot) and
    written by a background thread, so a slow network never pauses the game.
    """

    KEY = "main"

    def __init__(self, url: str, cache_path: Path | None = None, attempts: int = 8):
        import psycopg  # only needed when DATABASE_URL is set

        self._psycopg = psycopg
        self.url = url
        parts = urlsplit(url)
        self.path = f"postgres://{parts.hostname or '?'}/{parts.path.lstrip('/')}"  # no password in logs
        self._cache = cache_path
        self._dirty = False
        self._handle = None
        self._conn = None
        self._cv = threading.Condition()
        self._pending: str | None = None
        self._writing = False
        self._closed = False
        self.data = self._load(attempts)
        self._thread = threading.Thread(target=self._writer, name="difaf-db-writer", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------- connection
    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._psycopg.connect(self.url, autocommit=True, connect_timeout=10)
        return self._conn

    def _load(self, attempts: int) -> dict:
        last = None
        for i in range(attempts):
            try:
                conn = self._connect()
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS difaf_store ("
                    " id text PRIMARY KEY, data jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())"
                )
                row = conn.execute("SELECT data FROM difaf_store WHERE id = %s", (self.KEY,)).fetchone()
                if row is not None:
                    data = row[0]
                    if isinstance(data, str):
                        data = json.loads(data)
                    log.info("loaded data from %s", self.path)
                    return data if isinstance(data, dict) else {}
                # first start with this database: bring over a local file if there is one
                data = {}
                if self._cache and self._cache.exists():
                    try:
                        data = self._read(self._cache)
                        log.warning("copied %s into the database", self._cache)
                    except Exception as exc:
                        log.error("local data file unreadable: %s", exc)
                self._write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
                return data
            except Exception as exc:  # network/database not ready yet
                last = exc
                self._conn = None
                wait = min(2 + i * 2, 8)
                log.warning("database not reachable (%s); retrying in %ss", exc, wait)
                time.sleep(wait)
        # Never start empty: that would overwrite the real data with nothing.
        raise RuntimeError(f"cannot reach the database at {self.path}: {last}")

    def _write(self, payload: str) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT INTO difaf_store (id, data, updated_at) VALUES (%s, %s::jsonb, now()) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
            (self.KEY, payload),
        )

    # -------------------------------------------------------------- saving
    def save_now(self) -> None:
        if not self._dirty:
            return
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        self._dirty = False
        with self._cv:
            self._pending = payload  # only the newest snapshot matters
            self._cv.notify()

    def _writer(self) -> None:
        while True:
            with self._cv:
                while self._pending is None and not self._closed:
                    self._cv.wait()
                if self._pending is None and self._closed:
                    return
                payload, self._pending = self._pending, None
                self._writing = True
            try:
                self._write(payload)
            except Exception as exc:
                log.error("could not save to the database (%s); retrying", exc)
                self._conn = None
                with self._cv:
                    if self._pending is None:
                        self._pending = payload  # keep it unless something newer arrived
                time.sleep(2)
            finally:
                with self._cv:
                    self._writing = False
                    self._cv.notify_all()

    def flush(self, timeout: float = 15.0) -> bool:
        """Wait until everything has been written (used at shutdown and in tests)."""
        self.save_now()
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._pending is not None or self._writing:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True

    def close(self) -> None:
        ok = self.flush()
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        if not ok:
            log.error("some changes could not be written to the database before shutdown")
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass


def open_store(base_dir: Path) -> Store:
    """Postgres when DATABASE_URL is set, otherwise the local JSON file."""
    path = resolve_data_dir(base_dir) / "db.json"
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return PostgresStore(url, cache_path=path)
    return Store(path)
