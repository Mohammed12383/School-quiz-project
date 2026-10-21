"""Fixtures: an in-memory game engine, and a real server running in a subprocess."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN_PASSWORD = "test-pass-123"


@pytest.fixture
def game(tmp_path):
    from app.game import Game
    from app.store import Store

    return Game(Store(tmp_path / "db.json"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    """Runs `uvicorn app.main:app` exactly like production, with its own data folder."""

    def __init__(self, data_dir: Path, env_extra: dict | None = None):
        self.data_dir = data_dir
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = dict(os.environ, DATA_DIR=str(data_dir), ADMIN_PASSWORD=ADMIN_PASSWORD, **(env_extra or {}))
        self.proc = None

    @property
    def log_path(self) -> Path:
        return self.data_dir / "server.log"

    def log(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def start(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=log,
        )
        log.close()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited: " + self.log()[-2000:])
            try:
                with _opener.open(self.url + "/healthz", timeout=1) as r:
                    if r.status == 200:
                        return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    pytest.importorskip("aiohttp", reason="pip install -r requirements-dev.txt")
    srv = LiveServer(tmp_path_factory.mktemp("srv")).start()
    yield srv
    srv.stop()


@pytest.fixture
def live_server_factory(tmp_path):
    """For tests that need to stop/restart a server of their own."""
    pytest.importorskip("aiohttp", reason="pip install -r requirements-dev.txt")
    servers = []

    def make(data_dir=None):
        srv = LiveServer(data_dir or tmp_path / "data")
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.stop()
