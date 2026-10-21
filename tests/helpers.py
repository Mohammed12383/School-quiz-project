"""Shared helpers for the test-suite."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never use a proxy for the local server


def http(url, method="GET", headers=None, data=None):
    """Return (status, headers, body-bytes) without raising on HTTP errors."""
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def add_classes(game, grade, sections, questions=6, time_limit=20):
    game.class_create({"grade": grade, "from": 1, "to": sections})
    ids = [c["id"] for c in game.classes.values() if c["grade"] == grade]
    for cid in ids:
        game.class_save_questions({"classId": cid, "questions": [
            {"text": f"سؤال {i} للفصل {game.classes[cid]['name']}", "correct": "صحيح",
             "wrong": ["خطأ 1", "خطأ 2", "خطأ 3"], "time": time_limit, "field": "nahw" if i % 2 else "adab"}
            for i in range(questions)]})
    return sorted(ids, key=lambda c: game.classes[c]["section"])


def join(game, sid, cid, name):
    game.connect(sid, "127.0.0.1")
    return game.player_join(sid, {"classId": cid, "name": name})["player"]["id"]


def correct_choice(game, pid):
    """Index of the correct option *as shown to this player* (server-side knowledge only)."""
    L = game.live
    e = L.roster[pid]
    q = L.questions[e["side"]][L.qi]
    return game.player_view(pid)["question"]["options"].index(q["correct"])


def start_question(game, countdown=3.0):
    game.tick(time.monotonic() + countdown + 0.01)


def play_knockout_match(game, match_id, answers):
    """answers: {sid: callable(game, pid) -> choice index} for every question."""
    game.match_open({"matchId": match_id})
    game.match_start()
    L = game.live
    for _ in range(L.n):
        start_question(game, game.countdown_seconds())
        for sid, pick in answers.items():
            pid = game.sids[sid]["pid"]
            if pid in L.roster and game.live.phase == "question":
                game.player_answer(sid, {"q": L.qi, "choice": pick(game, pid)})
        if game.live.phase == "question":
            game.match_reveal()
        game.match_next()
    return game.matches[match_id]


class SocketClient:
    """Minimal async Socket.IO client that records every event it receives."""

    def __init__(self, url, name="client"):
        import socketio

        self.url = url
        self.name = name
        self.sio = socketio.AsyncClient(reconnection=False)
        self.events: list[tuple[str, object]] = []
        self.state: dict = {}
        self.player = None

        @self.sio.on("*")
        async def _any(event, data=None):
            self.events.append((event, data))
            if event == "player:state":
                self.state = data
            else:
                self.state[event] = data

    async def connect(self):
        await self.sio.connect(self.url, transports=["websocket"])
        return self

    async def call(self, event, data=None, timeout=10):
        return await self.sio.call(event, data if data is not None else {}, timeout=timeout)

    async def close(self):
        try:
            await self.sio.disconnect()
        except Exception:
            pass

    def received_json(self) -> str:
        return json.dumps(self.events, ensure_ascii=False)


async def wait_for(pred, timeout=15.0, msg=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.03)
    raise AssertionError("timeout waiting for: " + msg)
