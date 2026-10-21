"""Postgres storage (used on hosting such as Render).

Run with a database:  TEST_DATABASE_URL=postgresql://user:pass@host:5432/db pytest -q tests/test_store_postgres.py
"""

import asyncio
import os
import shutil

import pytest

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="set TEST_DATABASE_URL to run the Postgres tests")


@pytest.fixture
def clean_db():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS difaf_store")
    yield URL


def test_save_and_reload(clean_db, tmp_path):
    from app.store import PostgresStore

    s = PostgresStore(clean_db, cache_path=tmp_path / "db.json")
    assert s.data == {}
    s.data["classes"] = {"c1": {"name": "10/1", "questions": [{"text": "مرحبا"}]}}
    s.mark_dirty()
    assert s.flush()
    s.close()
    again = PostgresStore(clean_db)
    assert again.data["classes"]["c1"]["questions"][0]["text"] == "مرحبا"
    assert "@" not in again.path  # the password never appears in logs
    again.close()


def test_only_newest_snapshot_is_kept(clean_db):
    from app.store import PostgresStore

    s = PostgresStore(clean_db)
    for i in range(50):
        s.data["n"] = i
        s.mark_dirty()
        s.save_now()
    assert s.flush()
    s.close()
    assert PostgresStore(clean_db).data["n"] == 49


def test_local_file_is_copied_on_first_use(clean_db, tmp_path):
    from app.store import PostgresStore, Store

    local = Store(tmp_path / "db.json")
    local.data["classes"] = {"old": 1}
    local.mark_dirty()
    local.save_now()
    s = PostgresStore(clean_db, cache_path=tmp_path / "db.json")
    assert s.data["classes"] == {"old": 1}
    s.close()


def test_unreachable_database_refuses_to_start():
    from app.store import PostgresStore

    with pytest.raises(RuntimeError):
        PostgresStore("postgresql://nobody:x@127.0.0.1:1/none", attempts=1)


def test_server_keeps_data_when_the_disk_is_wiped(clean_db, live_server_factory, tmp_path):
    """What happens on Render's free plan: the server sleeps, its disk is wiped, it wakes up."""
    from tests.conftest import ADMIN_PASSWORD, LiveServer
    from tests.helpers import SocketClient, wait_for

    data_dir = tmp_path / "disk"

    def server():
        srv = LiveServer(data_dir, env_extra={"DATABASE_URL": clean_db})
        return srv.start()

    async def create(url):
        a = await SocketClient(url).connect()
        assert (await a.call("admin:login", {"password": ADMIN_PASSWORD}))["ok"]
        assert (await a.call("class:create", {"grade": 11, "from": 1, "to": 3}))["ok"]
        await wait_for(lambda: len(a.state.get("admin:data", {}).get("classes", [])) == 3)
        await asyncio.sleep(1.5)  # debounced save
        await a.close()

    async def count(url):
        a = await SocketClient(url).connect()
        assert (await a.call("admin:login", {"password": ADMIN_PASSWORD}))["ok"]
        await wait_for(lambda: "admin:data" in a.state)
        n = len(a.state["admin:data"]["classes"])
        await a.close()
        return n

    srv = server()
    asyncio.run(create(srv.url))
    srv.stop()
    shutil.rmtree(data_dir)  # the free plan loses the local disk
    srv2 = server()
    try:
        assert asyncio.run(count(srv2.url)) == 3
    finally:
        srv2.stop()
