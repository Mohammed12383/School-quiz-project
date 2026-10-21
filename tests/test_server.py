"""End-to-end tests against a real server process (HTTP + Socket.IO).

Needs:  pip install -r requirements-dev.txt
"""

import asyncio
import json
import os
import time

import pytest

from tests.conftest import ADMIN_PASSWORD
from tests.helpers import SocketClient, http, wait_for

pytest.importorskip("aiohttp", reason="pip install -r requirements-dev.txt")

QUESTION_KEYS = {"text", "options", "field", "durationMs", "remainingMs"}


def run(coro):
    return asyncio.run(coro)


async def admin_client(url):
    a = await SocketClient(url, "admin").connect()
    r = await a.call("admin:login", {"password": ADMIN_PASSWORD})
    assert r["ok"], r
    a.token = r["token"]
    await a.call("data:reset", {"confirm": "RESET"})
    await wait_for(lambda: "admin:data" in a.state and not a.state["admin:data"]["classes"], msg="reset")
    return a


async def setup_classes(a, specs, samples=True):
    for grade, n in specs:
        r = await a.call("class:create", {"grade": grade, "from": 1, "to": n})
        assert r["ok"], r
    await wait_for(lambda: len(a.state["admin:data"]["classes"]) == sum(n for _, n in specs), msg="classes")
    classes = a.state["admin:data"]["classes"]
    if samples:
        for c in classes:
            assert (await a.call("class:addSamples", {"classId": c["id"]}))["ok"]
    return [c["id"] for c in classes]


async def students(url, class_ids, per_class, prefix="طالب"):
    out = []
    for cid in class_ids:
        for i in range(per_class):
            s = await SocketClient(url, f"{cid}-{i}").connect()
            r = await s.call("player:join", {"classId": cid, "name": f"{prefix} {len(out) + 1} الفصل"})
            assert r["ok"], r
            s.player = r["player"]
            out.append(s)
    return out


# ------------------------------------------------------------------- HTTP
def test_pages_endpoints_and_security_headers(server):
    for path in ("/", "/about", "/play", "/display", "/admin"):
        status, headers, body = http(server.url + path)
        assert status == 200, path
        assert "text/html" in headers["content-type"]
        assert "default-src 'self'" in headers["content-security-policy"]
        assert headers["x-frame-options"] == "DENY"
        assert 'dir="rtl"' in body.decode()
    assert http(server.url + "/healthz")[0] == 200
    assert http(server.url + "/healthz", method="HEAD")[0] == 200
    info = json.loads(http(server.url + "/api/info")[2])
    assert isinstance(info["lanUrls"], list)
    status, headers, body = http(server.url + "/api/qr?u=" + "http%3A%2F%2F192.168.1.5%3A8000%2Fplay")
    assert status == 200 and headers["content-type"].startswith("image/svg+xml") and body.startswith(b"<svg")
    assert http(server.url + "/api/qr?u=javascript:alert(1)")[0] == 400
    assert http(server.url + "/api/qr?u=http://" + "a" * 400)[0] == 400
    site = json.loads(http(server.url + "/api/site")[2])
    assert {"site", "stats", "live", "champions", "topStudents"} <= set(site)
    assert json.loads(http(server.url + "/api/classes")[2])["classes"] is not None
    assert http(server.url + "/static/js/common.js")[0] == 200
    assert http(server.url + "/static/../app/admin_password.txt")[0] == 404


def test_admin_http_endpoints_require_token(server):
    for path in ("/api/admin/export", "/api/admin/export/students.csv"):
        assert http(server.url + path)[0] == 401
        assert http(server.url + path, headers={"X-Admin-Token": "forged"})[0] == 401
    assert http(server.url + "/api/admin/import", method="POST", data=b"{}")[0] == 401


# --------------------------------------------------------------- security
def test_admin_actions_need_login_and_password_is_checked(server):
    async def go():
        intruder = await SocketClient(server.url).connect()
        r = await intruder.call("class:create", {"grade": 10, "from": 1, "to": 1})
        assert not r["ok"] and r["code"] == "auth"
        r = await intruder.call("data:reset", {"confirm": "RESET"})
        assert not r["ok"] and r["code"] == "auth"
        r = await intruder.call("admin:login", {"password": "admin123"})  # the default is overridden by ADMIN_PASSWORD
        assert not r["ok"]
        r = await intruder.call("admin:login", {"password": ADMIN_PASSWORD})
        assert r["ok"]
        second = await SocketClient(server.url).connect()
        assert (await second.call("admin:login", {"token": r["token"]}))["ok"]  # remembered session
        await intruder.close()
        await second.close()
    run(go())


def test_flood_protection(server):
    async def go():
        c = await SocketClient(server.url).connect()
        res = await asyncio.gather(*(c.call("player:sync") for _ in range(90)))
        assert sum(1 for x in res if "طلبات" in (x.get("error") or "")) > 20
        await c.close()
    run(go())


def test_student_session_survives_reconnect_and_rejects_forgery(server):
    async def go():
        a = await admin_client(server.url)
        (cid,) = await setup_classes(a, [(12, 1)])
        (s,) = await students(server.url, [cid], 1)
        pid, token = s.player["id"], s.player["token"]
        await s.close()
        again = await SocketClient(server.url).connect()
        r = await again.call("player:join", {"playerId": pid, "token": token})
        assert r["ok"] and r["player"]["id"] == pid
        forged = await SocketClient(server.url).connect()
        r = await forged.call("player:join", {"playerId": pid, "token": "A" * len(token)})
        assert not r["ok"] and r["code"] == "auth"
        for c in (a, again, forged):
            await c.close()
    run(go())


# ------------------------------------------------------------ competitions
def test_full_knockout_match_over_sockets(server):
    async def go():
        a = await admin_client(server.url)
        ids = await setup_classes(a, [(10, 2)])
        r = await a.call("tournament:create", {"type": "knockout", "name": "دوري الاختبار", "classIds": ids, "questionsPerMatch": 2})
        assert r["ok"], r
        await wait_for(lambda: a.state["admin:data"]["tournaments"], msg="tournament")
        match = a.state["admin:data"]["tournaments"][0]["rounds"][0][0]
        side_of = {match["A"]["id"]: "A", match["B"]["id"]: "B"}
        display = await SocketClient(server.url, "display").connect()
        await display.call("display:join")
        studs = await students(server.url, ids, 3)
        studs[-1].player["name"] = "=HYPERLINK(1)"
        await a.call("admin:playerRename", {"playerId": studs[-1].player["id"], "name": "=HYPERLINK(1)"})

        assert (await a.call("match:open", {"matchId": match["id"]}))["ok"]
        await wait_for(lambda: all(s.state.get("phase") == "lobby" for s in studs), msg="lobby")
        assert (await a.call("match:start"))["ok"]
        for q in range(2):
            await wait_for(lambda: all(s.state.get("phase") == "question" for s in studs), msg=f"q{q}")
            await wait_for(lambda: (a.state.get("admin:live") or {}).get("liveQuestions"), msg="admin q")
            await asyncio.sleep(0.3)
            lq = a.state["admin:live"]["liveQuestions"]
            for k, s in enumerate(studs):
                assert set(s.state["question"]) == QUESTION_KEYS  # nothing marks the right option
                opts = s.state["question"]["options"]
                right = opts.index(lq[side_of[s.player["classId"]]]["correct"])
                pick = right if side_of[s.player["classId"]] == "A" or k % 3 == 0 else (right + 1) % len(opts)
                assert (await s.call("player:answer", {"q": q, "choice": pick}))["ok"]
                assert not (await s.call("player:answer", {"q": q, "choice": pick}))["ok"]
            await wait_for(lambda: all(s.state.get("phase") == "reveal" for s in studs), msg="reveal")
            assert (await a.call("match:next"))["ok"]
        await wait_for(lambda: all(s.state.get("phase") == "done" for s in studs), msg="done")
        assert {s.state["outcome"] for s in studs if side_of[s.player["classId"]] == "A"} == {"win"}

        # the display never received a correct answer before a reveal
        for ev, data in display.events:
            live = (data or {}).get("live") if ev == "display:state" else None
            if live and live["phase"] == "question":
                for side in live["sides"]:
                    assert "correctIndex" not in side and "counts" not in side

        # results reached the history, the champion is crowned, CSV export works
        await wait_for(lambda: a.state["admin:data"]["tournaments"][0].get("champion"), msg="champion")
        status, headers, body = http(server.url + "/api/admin/export/students.csv", headers={"X-Admin-Token": a.token})
        text = body.decode("utf-8")
        assert status == 200 and text.startswith("﻿") and "'=HYPERLINK(1)" in text
        site = json.loads(http(server.url + "/api/site")[2])
        assert site["champions"][0]["name"] == "10/1"
        for c in (a, display, *studs):
            await c.close()
    run(go())


def test_parallel_separate_contests_with_review(server):
    async def go():
        a = await admin_client(server.url)
        ids = await setup_classes(a, [(11, 2)], samples=False)
        for cid in ids:
            await a.call("class:saveQuestions", {"classId": cid, "questions": [
                {"text": f"سؤال {i}", "correct": "صحيح", "wrong": ["خطأ"], "time": 10, "field": "imla"} for i in range(3)]})
        r = await a.call("tournament:create", {"type": "separate", "classIds": ids})
        tid = r["tournamentId"]
        studs = await students(server.url, ids, 2)
        assert (await a.call("classCompetition:open", {"tournamentId": tid, "classIds": ids}))["ok"]
        await wait_for(lambda: all(s.state.get("phase") == "lobby" for s in studs), msg="lobby")
        assert (await a.call("classCompetition:start", {"tournamentId": tid}))["ok"]

        async def play(s, good):
            done = 0
            while done < 3:
                await wait_for(lambda: s.state.get("phase") == "question" and s.state["qIndex"] == done, timeout=10, msg="question")
                q = s.state["question"]
                assert set(q) == QUESTION_KEYS
                i = q["options"].index("صحيح")
                assert (await s.call("player:answer", {"q": done, "choice": i if good else 1 - i}))["ok"]
                done += 1

        await asyncio.gather(*(play(s, s.player["classId"] == ids[0]) for s in studs))
        # the last snapshot each phone receives must be the newest one (ordering bug regression)
        await wait_for(lambda: all(s.state.get("phase") == "finished" and s.state.get("review") is not None for s in studs), msg="review")
        bad = next(s for s in studs if s.player["classId"] == ids[1])
        assert len(bad.state["review"]) == 3 and bad.state["review"][0]["correct"] == "صحيح"
        await wait_for(lambda: all(r["status"] == "finished" for r in a.state["admin:data"]["tournaments"][0]["contests"]), msg="finished")
        ranking = a.state["admin:data"]["tournaments"][0]["contests"]
        assert ranking[0]["classId"] == ids[0] and ranking[0]["rank"] == 1
        for c in (a, *studs):
            await c.close()
    run(go())


def test_backup_roundtrip(server):
    async def go():
        a = await admin_client(server.url)
        await setup_classes(a, [(9, 2)])
        status, _, body = http(server.url + "/api/admin/export", headers={"X-Admin-Token": a.token})
        assert status == 200
        backup = json.loads(body)
        assert len(backup["classes"]) == 2
        await a.call("data:reset", {"confirm": "RESET"})
        await wait_for(lambda: not a.state["admin:data"]["classes"], msg="reset")
        status, _, body = http(server.url + "/api/admin/import", method="POST", data=json.dumps(backup).encode(),
                               headers={"X-Admin-Token": a.token, "Content-Type": "application/json"})
        assert status == 200 and json.loads(body)["classes"] == 2
        status, _, _ = http(server.url + "/api/admin/import", method="POST", data=b"not json",
                            headers={"X-Admin-Token": a.token})
        assert status == 400
        await a.close()
    run(go())


def test_restart_keeps_students_and_partial_results(live_server_factory, tmp_path):
    srv = live_server_factory(tmp_path / "restart").start()

    async def before():
        a = await admin_client(srv.url)
        (cid,) = await setup_classes(a, [(9, 1)])
        r = await a.call("tournament:create", {"type": "separate", "classIds": [cid]})
        (s,) = await students(srv.url, [cid], 1)
        await a.call("classCompetition:open", {"tournamentId": r["tournamentId"], "classIds": [cid]})
        await a.call("classCompetition:start", {"tournamentId": r["tournamentId"]})
        await wait_for(lambda: s.state.get("phase") == "question", msg="question")
        await s.call("player:answer", {"q": 0, "choice": 0})
        await asyncio.sleep(1.6)  # debounced save
        for c in (a, s):
            await c.close()
        return s.player

    player = run(before())
    srv.stop()
    srv2 = live_server_factory(tmp_path / "restart").start()

    async def after():
        s = await SocketClient(srv2.url).connect()
        r = await s.call("player:join", {"playerId": player["id"], "token": player["token"]})
        assert r["ok"]
        a = await SocketClient(srv2.url).connect()
        assert (await a.call("admin:login", {"password": ADMIN_PASSWORD}))["ok"]
        await wait_for(lambda: "admin:data" in a.state, msg="data")
        row = a.state["admin:data"]["tournaments"][0]["contests"][0]
        assert row["status"] == "finished" and row["interrupted"] and row["answered"] == 1
        for c in (a, s):
            await c.close()

    run(after())


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("RUN_SLOW"), reason="set RUN_SLOW=1 to run the load test")
def test_load_120_students(server):
    """120 phones answer the same question at once; every answer is accepted and the reveal is fast."""
    async def go():
        a = await admin_client(server.url)
        ids = await setup_classes(a, [(10, 2)])
        await a.call("tournament:create", {"type": "knockout", "classIds": ids, "questionsPerMatch": 1})
        await wait_for(lambda: a.state["admin:data"]["tournaments"], msg="tournament")
        match = a.state["admin:data"]["tournaments"][0]["rounds"][0][0]
        studs = []
        for batch in range(4):
            studs += await students(server.url, ids, 15, prefix=f"ط{batch}")
        await a.call("match:open", {"matchId": match["id"]})
        await a.call("match:start")
        await wait_for(lambda: all(s.state.get("phase") == "question" for s in studs), timeout=20, msg="question")
        t0 = time.monotonic()
        res = await asyncio.gather(*(s.call("player:answer", {"q": 0, "choice": 0}) for s in studs))
        answered = time.monotonic() - t0
        assert all(r["ok"] for r in res)
        await wait_for(lambda: all(s.state.get("phase") == "reveal" for s in studs), timeout=10, msg="reveal")
        total = time.monotonic() - t0
        print(f"\n120 answers accepted in {answered:.2f}s; everyone saw the result after {total:.2f}s")
        assert total < 5
        await asyncio.gather(*(c.close() for c in (a, *studs)))
    run(go())
