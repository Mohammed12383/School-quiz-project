"""Competition engine.

Holds classes and their question banks, knockout leagues, separate class
contests, the live state of running competitions, scoring and statistics.

Every method here is synchronous and runs on the event-loop thread, so state
changes are atomic. Network I/O lives in main.py: it calls a method, then
drains `outbox` and the dirty flags with `drain()` and sends the result.
"""

from __future__ import annotations

import copy
import hmac
import random
import re
import time
from collections import defaultdict

from .auth import DEFAULT_PASSWORD, current_password
from .sample_questions import SAMPLE_QUESTIONS
from .util import GameError, as_dict, clean_text, has_letters, new_id, new_token, to_int

_rng = random.SystemRandom()

GRADES = {9: "التاسع", 10: "العاشر", 11: "الحادي عشر", 12: "الثاني عشر"}
COUNTDOWN = 3.0        # seconds before every question (knockout)
GRACE = 0.6            # network allowance after a deadline
FEEDBACK_DELAY = 1.6   # pause between questions in separate contests
MIN_TIME, MAX_TIME, DEFAULT_TIME = 5, 120, 20
MAX_QUESTIONS = 200
MAX_PLAYERS = 5000
MAX_WRONG = 3
DISPLAY_MODES = ("auto", "join", "leaderboard", "bracket", "idle", "message")
KO_MAX_POINTS = 1000
CONTEST_MAX_POINTS = 5
FIELDS = {"nahw": "النحو والصرف", "balagha": "البلاغة", "adab": "الأدب", "imla": "الإملاء", "qiraa": "القراءة"}

DEFAULT_SITE = {
    "programName": "ضفاف العربية",
    "schoolName": "مدرسة يزيد بن حاتم الأزدي",
    "tagline": "مسابقات اللغة العربية بين الفصول",
    "welcome": "برنامج تربوي ثقافي يحتفي بلغتنا العربية، لغة القرآن والهوية، عبر منافسات حماسية بين الفصول.",
    "goals": [
        "الاعتزاز بالعربية وتعزيز انتماء الطلاب إلى لغتهم وهويتهم.",
        "تنمية المهارات اللغوية في النحو والصرف والبلاغة والإملاء والقراءة بأسلوب ممتع.",
        "إثراء الذائقة الأدبية والاطلاع على روائع الشعر والنثر العربي.",
        "روح الفريق والتنافس الشريف بين الفصول.",
        "توظيف التقنية الحديثة في خدمة تعلّم اللغة العربية.",
    ],
    "verse": "أنا البحرُ في أحشائه الدرُّ كامنٌ\nفهل سألوا الغوّاصَ عن صدَفاتي",
    "poet": "حافظ إبراهيم",
    "announcement": "",
}
SITE_LIMITS = {"programName": 40, "schoolName": 80, "tagline": 100, "welcome": 400,
               "verse": 200, "poet": 60, "announcement": 200}

_QID = re.compile(r"q[0-9a-f]{10}")


def now_ts() -> int:
    return int(time.time())


def ms(seconds: float) -> int:
    return max(0, int(seconds * 1000))


def default_settings() -> dict:
    return {
        "activeTournamentId": None, "displayMode": "auto", "allowLateJoin": False,
        "countdown": int(COUNTDOWN), "autoReveal": True, "reviewAfterContest": True,
        "showLeadersOnHome": True, "displayMessage": "", "site": copy.deepcopy(DEFAULT_SITE),
    }


def empty_db() -> dict:
    return {
        "version": 1,
        "classes": {},
        "players": {},
        "tournaments": {},
        "matches": {},
        "history": [],
        "stats": {"players": {}, "questions": {}},
        "settings": default_settings(),
    }


def clean_site(raw, base: dict | None = None) -> dict:
    """Validate the editable texts of the site (titles, welcome, goals, verse...)."""
    site = copy.deepcopy(base if isinstance(base, dict) else DEFAULT_SITE)
    for k, v in DEFAULT_SITE.items():
        site.setdefault(k, copy.deepcopy(v))
    raw = as_dict(raw)
    for key, limit in SITE_LIMITS.items():
        if key not in raw:
            continue
        value = clean_text(raw[key], limit, multiline=key in ("welcome", "verse", "announcement"))
        if key in ("programName", "schoolName") and not value:
            continue  # these two can never be empty
        site[key] = value
    if isinstance(raw.get("goals"), list):
        site["goals"] = [g for g in (clean_text(x, 160) for x in raw["goals"][:8]) if g]
    return site


def idle_contest() -> dict:
    return {
        "status": "idle", "questions": [], "players": {}, "lobby": [],
        "openedAt": None, "startedAt": None, "finishedAt": None, "result": None,
    }


def round_name(matches_in_round: int, index: int) -> str:
    names = {1: "النهائي", 2: "نصف النهائي", 4: "ربع النهائي", 8: "دور الستة عشر", 16: "دور الاثنين والثلاثين"}
    return names.get(matches_in_round, f"الدور {index + 1}")


def clean_question(raw, index: int | None = None) -> dict:
    q = as_dict(raw)
    label = f"السؤال {index + 1}: " if index is not None else ""
    text = clean_text(q.get("text"), 400, multiline=True)
    correct = clean_text(q.get("correct"), 150)
    wrong_raw = q.get("wrong") if isinstance(q.get("wrong"), list) else []
    wrong = [w for w in (clean_text(x, 150) for x in wrong_raw[:6]) if w]
    if not text:
        raise GameError(label + "نص السؤال فارغ")
    if not correct:
        raise GameError(label + "اكتب الإجابة الصحيحة")
    if not wrong:
        raise GameError(label + "أضف خيارًا خاطئًا واحدًا على الأقل")
    if len(wrong) > MAX_WRONG:
        raise GameError(label + "الحد الأقصى ثلاثة خيارات خاطئة")
    seen = {correct.casefold()}
    for w in wrong:
        if w.casefold() in seen:
            raise GameError(label + f"الخيار «{w}» مكرر")
        seen.add(w.casefold())
    qid = q.get("id")
    if not (isinstance(qid, str) and _QID.fullmatch(qid)):
        qid = new_id("q")
    return {
        "id": qid,
        "text": text,
        "correct": correct,
        "wrong": wrong,
        "time": to_int(q.get("time"), DEFAULT_TIME, MIN_TIME, MAX_TIME),
        "field": q.get("field") if q.get("field") in FIELDS else "",
    }


def options_of(q: dict) -> list[str]:
    """Canonical order: index 0 is always the correct answer."""
    return [q["correct"], *q["wrong"]]


def shuffled(n: int) -> list[int]:
    p = list(range(n))
    _rng.shuffle(p)
    return p


def score_key(s: dict) -> tuple:
    """Higher is better: average, then accuracy, then speed (lower time)."""
    t = s.get("avgTime")
    return (round(s.get("avg") or 0, 3), round(s.get("accuracy") or 0, 4), -(t if t is not None else 1e9))


def normalize_db(data) -> dict:
    db = empty_db()
    if not isinstance(data, dict):
        return db
    for key in ("players", "tournaments", "matches"):
        v = data.get(key)
        if isinstance(v, dict):
            db[key] = {k: x for k, x in v.items() if isinstance(k, str) and isinstance(x, dict)}
    if isinstance(data.get("history"), list):
        db["history"] = [h for h in data["history"] if isinstance(h, dict)][-1000:]
    stats = as_dict(data.get("stats"))
    db["stats"]["players"] = {k: v for k, v in as_dict(stats.get("players")).items() if isinstance(v, dict)}
    db["stats"]["questions"] = {k: v for k, v in as_dict(stats.get("questions")).items() if isinstance(v, dict)}
    settings = as_dict(data.get("settings"))
    db["settings"]["activeTournamentId"] = settings.get("activeTournamentId") if isinstance(settings.get("activeTournamentId"), str) else None
    if settings.get("displayMode") in DISPLAY_MODES:
        db["settings"]["displayMode"] = settings["displayMode"]
    db["settings"]["allowLateJoin"] = bool(settings.get("allowLateJoin", False))
    db["settings"]["countdown"] = to_int(settings.get("countdown"), int(COUNTDOWN), 3, 10)
    for key in ("autoReveal", "reviewAfterContest", "showLeadersOnHome"):
        db["settings"][key] = bool(settings.get(key, True))
    db["settings"]["displayMessage"] = clean_text(settings.get("displayMessage"), 200, multiline=True)
    db["settings"]["site"] = clean_site(settings.get("site"))

    for cid, c in as_dict(data.get("classes")).items():
        if not isinstance(c, dict) or not isinstance(cid, str):
            continue
        grade = to_int(c.get("grade"), 0, 0, 99)
        section = to_int(c.get("section"), 0, 0, 99)
        if grade not in GRADES or section < 1:
            continue
        questions, ids = [], set()
        for q in c.get("questions") or []:
            try:
                cq = clean_question(q)
            except GameError:
                continue
            if cq["id"] in ids:
                cq["id"] = new_id("q")
            ids.add(cq["id"])
            questions.append(cq)
        db["classes"][cid] = {
            "id": cid, "grade": grade, "section": section, "name": f"{grade}/{section}",
            "questions": questions[:MAX_QUESTIONS], "createdAt": c.get("createdAt") or now_ts(),
        }

    for pid, p in list(db["players"].items()):
        if not (isinstance(p.get("token"), str) and isinstance(p.get("name"), str) and p.get("classId") in db["classes"]):
            del db["players"][pid]
            continue
        p["id"] = pid

    for mid, m in list(db["matches"].items()):
        m["id"] = mid
        if m.get("tournamentId") not in db["tournaments"]:
            del db["matches"][mid]
            continue
        # a match that was running when the server stopped must be replayed
        if m.get("status") in ("open", "live"):
            m["status"] = "pending"
            m["winner"] = None
            m["result"] = None

    for tid, t in list(db["tournaments"].items()):
        t["id"] = tid
        if t.get("type") not in ("knockout", "separate"):
            del db["tournaments"][tid]
            continue
        t.setdefault("name", "مسابقة")
        t.setdefault("classIds", [])
        t.setdefault("createdAt", now_ts())
        if t["type"] == "knockout":
            t.setdefault("rounds", [])
            t.setdefault("usedQuestions", {})
            t.setdefault("championId", None)
            t.setdefault("status", "running")
            t["questionsPerMatch"] = to_int(t.get("questionsPerMatch"), 10, 1, 50)
        else:
            contests = as_dict(t.get("contests"))
            for cid in t["classIds"]:
                rec = contests.get(cid)
                if not isinstance(rec, dict):
                    contests[cid] = idle_contest()
                    continue
                for k, v in idle_contest().items():
                    rec.setdefault(k, v)
                if rec["status"] == "lobby":
                    contests[cid] = idle_contest()
                elif rec["status"] == "running":
                    # keep what was answered before the restart
                    rec["status"] = "finished"
                    rec["finishedAt"] = now_ts()
                    rec["interrupted"] = True
                    rec["result"] = contest_score(rec)
            t["contests"] = contests

    if db["settings"]["activeTournamentId"] not in db["tournaments"]:
        db["settings"]["activeTournamentId"] = None
    return db


def contest_score(rec: dict) -> dict:
    n = len(rec.get("questions") or [])
    ps = [pp for pp in as_dict(rec.get("players")).values() if not pp.get("kicked")]
    k = len(ps)
    total = sum(pp.get("points", 0) for pp in ps)
    correct = sum(pp.get("correct", 0) for pp in ps)
    answered = sum(pp.get("pos", 0) for pp in ps)
    times = [pp.get("time", 0.0) for pp in ps if pp.get("pos", 0) > 0]
    return {
        "avg": round(total / (k * n), 3) if k and n else 0.0,
        "liveAvg": round(total / answered, 3) if answered else 0.0,
        "accuracy": round(correct / (k * n), 4) if k and n else 0.0,
        "avgTime": round(sum(times) / len(times), 2) if times else None,
        "players": k,
        "finished": sum(1 for pp in ps if pp.get("finished")),
        "answered": answered,
        "questions": n,
    }


class LiveMatch:
    """In-memory state of the knockout match currently on stage."""

    def __init__(self, match: dict, n: int):
        self.match_id: str = match["id"]
        self.tid: str = match["tournamentId"]
        self.cls = {"A": match["classA"], "B": match["classB"]}
        self.n = n
        self.phase = "lobby"  # lobby → countdown → question → reveal → … → done
        self.qi = -1
        self.revealed = 0
        self.questions: dict[str, list[dict]] = {"A": [], "B": []}
        self.phase_end = 0.0
        self.q_start = 0.0
        self.deadline = {"A": 0.0, "B": 0.0}
        self.roster: dict[str, dict] = {}
        self.lobby_members: set[str] = set()
        self.answers: list[dict[str, dict]] = []
        self.perms: dict[str, list[int]] = {}
        self.display_perm: dict[str, list[int]] = {"A": [], "B": []}
        self.time_up = False  # time ended but the teacher reveals manually

    def side_of_class(self, cid: str) -> str | None:
        if cid == self.cls["A"]:
            return "A"
        if cid == self.cls["B"]:
            return "B"
        return None

    def active(self) -> list[tuple[str, dict]]:
        return [(pid, e) for pid, e in self.roster.items() if not e["kicked"]]


class Game:
    def __init__(self, store):
        self.store = store
        self.db = normalize_db(store.data)
        store.replace(self.db)
        self.sids: dict[str, dict] = {}
        self.player_sids: dict[str, set[str]] = defaultdict(set)
        self.live: LiveMatch | None = None
        self.crt: dict[str, dict[str, dict]] = {}  # contest runtime: "tid:cid" -> pid -> state
        self.outbox: list[tuple[str, object, str]] = []
        self.dirty_players: set[str] = set()
        self.dirty_display = True
        self.dirty_admin_live = True
        self.dirty_admin_data = True
        self._last_display = 0.0
        self._last_admin_live = 0.0
        self._last_admin_players = 0.0
        self.dirty_admin_players = True
        self._last_create: dict[str, float] = {}

    # ------------------------------------------------------------------ basics
    @property
    def classes(self) -> dict:
        return self.db["classes"]

    @property
    def players(self) -> dict:
        return self.db["players"]

    @property
    def tournaments(self) -> dict:
        return self.db["tournaments"]

    @property
    def matches(self) -> dict:
        return self.db["matches"]

    @property
    def settings(self) -> dict:
        return self.db["settings"]

    @property
    def stats(self) -> dict:
        return self.db["stats"]

    def save(self) -> None:
        self.store.mark_dirty()

    def touch_data(self) -> None:
        self.dirty_admin_data = True
        self.dirty_admin_live = True
        self.dirty_display = True
        self.save()

    def touch_live(self) -> None:
        self.dirty_admin_live = True
        self.dirty_display = True

    def emit(self, event: str, data, to: str) -> None:
        self.outbox.append((event, data, to))

    def notify_player(self, pid: str, text: str, kind: str = "info") -> None:
        for sid in self.player_sids.get(pid, ()):
            self.emit("notice", {"text": text, "kind": kind}, sid)

    def online(self, pid: str) -> bool:
        return bool(self.player_sids.get(pid))

    def class_name(self, cid) -> str:
        c = self.classes.get(cid)
        return c["name"] if c else "—"

    def countdown_seconds(self) -> float:
        return float(self.settings.get("countdown") or COUNTDOWN)

    def active_tournament(self) -> dict | None:
        return self.tournaments.get(self.settings.get("activeTournamentId"))

    def has_role(self, role: str) -> bool:
        return any(i.get("role") == role for i in self.sids.values())

    def is_admin(self, sid: str) -> bool:
        return bool(self.sids.get(sid, {}).get("admin"))

    def online_by_class(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for pid in self.player_sids:
            p = self.players.get(pid)
            if p:
                out[p["classId"]] += 1
        return dict(out)

    # ------------------------------------------------------------ connections
    def connect(self, sid: str, ip: str) -> None:
        self.sids[sid] = {"role": None, "pid": None, "ip": ip, "admin": False}

    def disconnect(self, sid: str) -> None:
        info = self.sids.pop(sid, None)
        self._last_create.pop(sid, None)
        if not info:
            return
        pid = info.get("pid")
        if pid:
            sids = self.player_sids.get(pid)
            if sids is not None:
                sids.discard(sid)
                if not sids:
                    self.player_sids.pop(pid, None)
                    self.touch_live()

    def set_role(self, sid: str, role: str, admin: bool = False) -> None:
        info = self.sids.setdefault(sid, {"role": None, "pid": None, "ip": "?", "admin": False})
        if role != "player":
            self._detach_player(sid)
        info["role"] = role
        if role == "admin":
            info["admin"] = admin

    def revoke_admins(self, keep_sid: str | None = None) -> list[str]:
        out = []
        for sid, info in self.sids.items():
            if info.get("admin") and sid != keep_sid:
                info["admin"] = False
                out.append(sid)
        return out

    def _detach_player(self, sid: str) -> None:
        info = self.sids.get(sid)
        if not info or not info.get("pid"):
            return
        pid = info["pid"]
        info["pid"] = None
        sids = self.player_sids.get(pid)
        if sids is not None:
            sids.discard(sid)
            if not sids:
                self.player_sids.pop(pid, None)
        self.touch_live()

    def _sid_player(self, sid: str) -> str:
        info = self.sids.get(sid) or {}
        pid = info.get("pid")
        if not pid or pid not in self.players:
            raise GameError("سجّل دخولك أولًا", "auth")
        return pid

    # ---------------------------------------------------------------- players
    def public_classes(self) -> list[dict]:
        rows = sorted(self.classes.values(), key=lambda c: (c["grade"], c["section"]))
        return [{"id": c["id"], "grade": c["grade"], "gradeName": GRADES[c["grade"]], "name": c["name"]} for c in rows]

    def player_join(self, sid: str, data) -> dict:
        data = as_dict(data)
        info = self.sids.get(sid)
        if info is None:
            raise GameError("انقطع الاتصال، أعد تحميل الصفحة")
        player = None
        pid, token = data.get("playerId"), data.get("token")
        if isinstance(pid, str) and isinstance(token, str):
            p = self.players.get(pid)
            if p and hmac.compare_digest(p["token"].encode(), token.encode()):
                player = p
            elif not data.get("classId"):
                raise GameError("انتهت جلستك، سجّل دخولك من جديد", "auth")
        if player is None:
            cid = data.get("classId")
            if not isinstance(cid, str) or cid not in self.classes:
                raise GameError("اختر فصلك من القائمة", "class")
            name = clean_text(data.get("name"), 30)
            if len(name) < 2 or not has_letters(name, 2):
                raise GameError("اكتب اسمك (حرفان على الأقل)", "name")
            t = time.monotonic()
            if t - self._last_create.get(sid, -99.0) < 2.0:
                raise GameError("انتظر لحظة ثم حاول مجددًا")
            if len(self.players) >= MAX_PLAYERS:
                raise GameError("تم بلوغ الحد الأقصى لعدد الطلاب، راجع المعلم")
            self._last_create[sid] = t
            player = {"id": new_id("p"), "token": new_token(), "name": name, "classId": cid, "createdAt": now_ts()}
            self.players[player["id"]] = player
            self.dirty_admin_players = True
            self.save()
        if info.get("pid") and info["pid"] != player["id"]:
            self._detach_player(sid)
        info["role"] = "player"
        info["pid"] = player["id"]
        self.player_sids[player["id"]].add(sid)
        self._player_online(player["id"])
        self.dirty_players.add(player["id"])
        c = self.classes[player["classId"]]
        return {"ok": True, "player": {
            "id": player["id"], "token": player["token"], "name": player["name"],
            "classId": c["id"], "className": c["name"], "grade": c["grade"],
        }}

    def _player_online(self, pid: str) -> None:
        p = self.players[pid]
        cid = p["classId"]
        allow_late = self.settings.get("allowLateJoin", False)
        L = self.live
        if L and L.phase != "done":
            side = L.side_of_class(cid)
            if side and pid not in L.roster:
                if L.phase == "lobby" or allow_late or pid in L.lobby_members:
                    L.roster[pid] = self._roster_entry(p, side)
                    if L.phase == "lobby":
                        L.lobby_members.add(pid)
                    if L.phase in ("question", "reveal"):
                        L.perms[pid] = shuffled(len(options_of(L.questions[side][L.qi])))
            self.touch_live()
        t = self.active_tournament()
        if t and t["type"] == "separate":
            rec = t["contests"].get(cid)
            if rec and rec["status"] in ("lobby", "running"):
                self._contest_add_player(t, cid, rec, pid)

    @staticmethod
    def _roster_entry(p: dict, side: str) -> dict:
        return {"name": p["name"], "side": side, "points": 0, "correct": 0, "answered": 0,
                "streak": 0, "best": 0, "times": [], "kicked": False, "last": None, "leaves": 0, "leftQ": -1}

    def player_leave(self, sid: str) -> dict:
        pid = self._sid_player(sid)
        p = self.players[pid]
        L = self.live
        if L and pid in L.roster and not L.roster[pid]["kicked"] and L.phase not in ("lobby", "done"):
            raise GameError("لا يمكنك تغيير حسابك أثناء المباراة. انتظر حتى تنتهي.")
        t = self.active_tournament()
        if t and t["type"] == "separate":
            rec = t["contests"].get(p["classId"])
            if rec and rec["status"] == "running":
                pp = rec["players"].get(pid)
                if pp and not pp["kicked"] and not pp["finished"]:
                    raise GameError("لا يمكنك تغيير حسابك قبل إنهاء أسئلة المسابقة.")
            if rec and rec["status"] == "lobby":
                rec["players"].pop(pid, None)
        if L and L.phase == "lobby":
            L.roster.pop(pid, None)
            L.lobby_members.discard(pid)
        self._logout_player(pid)
        self.touch_live()
        self.save()
        return {"ok": True}

    def _logout_player(self, pid: str) -> None:
        for s in list(self.player_sids.get(pid, ())):
            info = self.sids.get(s)
            if info:
                info["pid"] = None
            self.emit("player:loggedOut", {}, s)
        self.player_sids.pop(pid, None)
        self.players.pop(pid, None)
        self.dirty_players.discard(pid)
        self.dirty_admin_players = True

    def player_sync(self, sid: str) -> dict:
        pid = self._sid_player(sid)
        self.dirty_players.add(pid)
        return {"ok": True}

    def player_blur(self, sid: str) -> dict:
        """The student left the page while a question was open: tell the teacher."""
        pid = self._sid_player(sid)
        L = self.live
        e = L.roster.get(pid) if L else None
        if e and L.phase == "question" and pid not in L.answers[L.qi] and e.get("leftQ") != L.qi:
            e["leftQ"] = L.qi
            e["leaves"] = e.get("leaves", 0) + 1
            self.touch_live()
            return {"ok": True}
        p = self.players[pid]
        t = self.active_tournament()
        if t and t["type"] == "separate":
            rec = t["contests"].get(p["classId"])
            pp = rec["players"].get(pid) if rec and rec["status"] == "running" else None
            rt = self.crt.get(f"{t['id']}:{p['classId']}", {}).get(pid) if pp else None
            if pp and rt and rt.get("sentAt") is not None and pp.get("leftQ") != pp["pos"]:
                pp["leftQ"] = pp["pos"]
                pp["leaves"] = pp.get("leaves", 0) + 1
                self.touch_live()
                self.save()
        return {"ok": True}

    def player_answer(self, sid: str, data) -> dict:
        pid = self._sid_player(sid)
        data = as_dict(data)
        L = self.live
        if L and pid in L.roster:
            return self._ko_answer(pid, data)
        p = self.players[pid]
        t = self.active_tournament()
        if t and t["type"] == "separate":
            rec = t["contests"].get(p["classId"])
            if rec and rec["status"] == "running" and pid in rec["players"]:
                return self._contest_answer(t, p["classId"], rec, pid, data)
        raise GameError("لا يوجد سؤال مفتوح الآن", "late")

    # ------------------------------------------------------- students (admin)
    def admin_players(self) -> dict:
        def order(r):
            c = self.classes.get(r["classId"]) or {}
            return (c.get("grade", 0), c.get("section", 0), r["name"])

        rows = []
        for pid, p in self.players.items():
            sp = self.stats["players"].get(pid) or {}
            rows.append({"id": pid, "name": p["name"], "classId": p["classId"], "className": self.class_name(p["classId"]),
                         "createdAt": p.get("createdAt"), "answered": sp.get("answered", 0), "correct": sp.get("correct", 0)})
        rows.sort(key=order)
        return {"players": rows}

    def _player_busy(self, pid: str) -> bool:
        L = self.live
        if L and pid in L.roster and not L.roster[pid]["kicked"] and L.phase not in ("lobby", "done"):
            return True
        t = self.active_tournament()
        if t and t["type"] == "separate":
            for rec in t["contests"].values():
                pp = rec["players"].get(pid)
                if rec["status"] == "running" and pp and not pp["kicked"] and not pp["finished"]:
                    return True
        return False

    def _admin_player(self, data) -> tuple[str, dict]:
        pid = as_dict(data).get("playerId")
        p = self.players.get(pid) if isinstance(pid, str) else None
        if not p:
            raise GameError("الطالب غير موجود")
        return pid, p

    def admin_player_rename(self, data) -> dict:
        pid, p = self._admin_player(data)
        name = clean_text(as_dict(data).get("name"), 30)
        if len(name) < 2 or not has_letters(name, 2):
            raise GameError("اكتب اسمًا صحيحًا (حرفان على الأقل)")
        p["name"] = name
        if self.live and pid in self.live.roster:
            self.live.roster[pid]["name"] = name
        for t in self.tournaments.values():
            for rec in (t.get("contests") or {}).values():
                if pid in rec["players"]:
                    rec["players"][pid]["name"] = name
        if pid in self.stats["players"]:
            self.stats["players"][pid]["name"] = name
        self.notify_player(pid, f"عدّل المعلم اسمك إلى «{name}»")
        self.dirty_players.add(pid)
        self.dirty_admin_players = True
        self.touch_data()
        return {"ok": True}

    def admin_player_move(self, data) -> dict:
        pid, p = self._admin_player(data)
        cid = as_dict(data).get("classId")
        if cid not in self.classes:
            raise GameError("اختر الفصل الجديد")
        if p["classId"] == cid:
            return {"ok": True}
        if self._player_busy(pid):
            raise GameError("الطالب يشارك الآن في مسابقة جارية. استبعده أولًا أو انتظر حتى تنتهي.")
        L = self.live
        if L and pid in L.roster and L.phase == "lobby":
            del L.roster[pid]
            L.lobby_members.discard(pid)
        t = self.active_tournament()
        if t and t["type"] == "separate":
            rec = t["contests"].get(p["classId"])
            if rec and rec["status"] == "lobby":
                rec["players"].pop(pid, None)
        p["classId"] = cid
        if self.online(pid):
            self._player_online(pid)
        self.notify_player(pid, f"نقلك المعلم إلى فصل {self.class_name(cid)}")
        self.dirty_players.add(pid)
        self.dirty_admin_players = True
        self.touch_live()
        self.save()
        return {"ok": True}

    def admin_player_delete(self, data) -> dict:
        raw = as_dict(data).get("playerIds")
        ids = [x for x in dict.fromkeys(raw if isinstance(raw, list) else []) if isinstance(x, str) and x in self.players]
        if not ids:
            raise GameError("اختر طالبًا واحدًا على الأقل")
        t = self.active_tournament()
        for pid in ids:
            L = self.live
            if L and pid in L.roster:
                if L.phase == "lobby":
                    del L.roster[pid]
                    L.lobby_members.discard(pid)
                elif L.phase != "done":
                    L.roster[pid]["kicked"] = True
            if t and t["type"] == "separate":
                for cid, rec in t["contests"].items():
                    pp = rec["players"].get(pid)
                    if not pp:
                        continue
                    if rec["status"] == "lobby":
                        rec["players"].pop(pid, None)
                    elif rec["status"] == "running":
                        pp["kicked"] = True
                        self.crt.get(f"{t['id']}:{cid}", {}).pop(pid, None)
                        self._contest_maybe_done(t, cid, rec)
            self._logout_player(pid)
        self.touch_data()
        return {"ok": True, "count": len(ids)}

    def admin_players_clear(self, data) -> dict:
        data = as_dict(data)
        busy = self._busy()
        if busy:
            raise GameError(busy)
        cid = data.get("classId")
        offline_only = bool(data.get("offlineOnly"))
        ids = [pid for pid, p in self.players.items()
               if (not cid or p["classId"] == cid) and not (offline_only and self.online(pid))]
        for pid in ids:
            self._logout_player(pid)
        self.touch_data()
        return {"ok": True, "count": len(ids)}

    # ---------------------------------------------------------------- classes
    def class_create(self, data) -> dict:
        data = as_dict(data)
        grade = to_int(data.get("grade"), 0, 0, 99)
        if grade not in GRADES:
            raise GameError("اختر الصف (من التاسع إلى الثاني عشر)")
        a = to_int(data.get("from"), 1, 1, 30)
        b = to_int(data.get("to"), a, 1, 30)
        if b < a:
            a, b = b, a
        existing = {(c["grade"], c["section"]) for c in self.classes.values()}
        created = []
        for s in range(a, b + 1):
            if (grade, s) in existing:
                continue
            cid = new_id("c")
            self.classes[cid] = {"id": cid, "grade": grade, "section": s, "name": f"{grade}/{s}",
                                 "questions": [], "createdAt": now_ts()}
            created.append(f"{grade}/{s}")
        if not created:
            raise GameError("هذه الفصول موجودة مسبقًا")
        self.touch_data()
        return {"ok": True, "created": created}

    def class_delete(self, data) -> dict:
        cid = as_dict(data).get("classId")
        c = self.classes.get(cid)
        if not c:
            raise GameError("الفصل غير موجود")
        for t in self.tournaments.values():
            if cid in t.get("classIds", []):
                raise GameError(f"الفصل {c['name']} مشارك في «{t['name']}». احذف تلك المسابقة أولًا.")
        for pid in [pid for pid, p in self.players.items() if p["classId"] == cid]:
            self._logout_player(pid)
        del self.classes[cid]
        self.touch_data()
        return {"ok": True}

    def class_update(self, data) -> dict:
        data = as_dict(data)
        c = self.classes.get(data.get("classId"))
        if not c:
            raise GameError("الفصل غير موجود")
        grade = to_int(data.get("grade"), c["grade"], 0, 99)
        section = to_int(data.get("section"), c["section"], 0, 99)
        if grade not in GRADES:
            raise GameError("اختر الصف (من التاسع إلى الثاني عشر)")
        if not 1 <= section <= 30:
            raise GameError("رقم الشعبة من 1 إلى 30")
        if any(o["id"] != c["id"] and o["grade"] == grade and o["section"] == section for o in self.classes.values()):
            raise GameError(f"الفصل {grade}/{section} موجود مسبقًا")
        c.update(grade=grade, section=section, name=f"{grade}/{section}")
        self.touch_data()
        self._dirty_all_players()
        self.dirty_admin_players = True
        return {"ok": True}

    def class_save_questions(self, data) -> dict:
        data = as_dict(data)
        c = self.classes.get(data.get("classId"))
        if not c:
            raise GameError("الفصل غير موجود")
        raw = data.get("questions")
        if not isinstance(raw, list):
            raise GameError("قائمة الأسئلة غير صالحة")
        if len(raw) > MAX_QUESTIONS:
            raise GameError(f"الحد الأقصى {MAX_QUESTIONS} سؤالًا لكل فصل")
        cleaned, ids = [], set()
        for i, q in enumerate(raw):
            cq = clean_question(q, i)
            if cq["id"] in ids:
                cq["id"] = new_id("q")
            ids.add(cq["id"])
            cleaned.append(cq)
        c["questions"] = cleaned
        self.touch_data()
        return {"ok": True, "count": len(cleaned)}

    def class_copy_questions(self, data) -> dict:
        data = as_dict(data)
        src = self.classes.get(data.get("fromId"))
        if not src:
            raise GameError("اختر الفصل المصدر")
        if not src["questions"]:
            raise GameError("لا توجد أسئلة في الفصل المصدر")
        to_ids = data.get("toIds") if isinstance(data.get("toIds"), list) else []
        targets = [self.classes[c] for c in dict.fromkeys(to_ids) if isinstance(c, str) and c in self.classes and c != src["id"]]
        if not targets:
            raise GameError("اختر فصلًا واحدًا على الأقل للنسخ إليه")
        replace = data.get("mode") == "replace"
        for c in targets:
            base = [] if replace else list(c["questions"])
            texts = {q["text"] for q in base}
            for q in src["questions"]:
                if q["text"] in texts or len(base) >= MAX_QUESTIONS:
                    continue
                base.append({**copy.deepcopy(q), "id": new_id("q")})
            c["questions"] = base
        self.touch_data()
        return {"ok": True, "count": len(targets)}

    def class_add_samples(self, data) -> dict:
        c = self.classes.get(as_dict(data).get("classId"))
        if not c:
            raise GameError("الفصل غير موجود")
        texts = {q["text"] for q in c["questions"]}
        added = 0
        for q in SAMPLE_QUESTIONS:
            if q["text"] in texts or len(c["questions"]) >= MAX_QUESTIONS:
                continue
            c["questions"].append(clean_question({**q, "time": DEFAULT_TIME}))
            added += 1
        self.touch_data()
        return {"ok": True, "added": added}

    # ------------------------------------------------------------ tournaments
    def _busy(self) -> str | None:
        if self.live and self.live.phase != "done":
            return "توجد مباراة جارية الآن. أنهِها أو ألغِها أولًا."
        t = self.active_tournament()
        if t and t["type"] == "separate":
            for cid, rec in t["contests"].items():
                if rec["status"] in ("lobby", "running"):
                    return f"مسابقة الفصل {self.class_name(cid)} ما زالت مفتوحة. أنهِها أولًا."
        return None

    def tournament_create(self, data) -> dict:
        data = as_dict(data)
        ttype = data.get("type")
        if ttype not in ("knockout", "separate"):
            raise GameError("اختر نوع المسابقة")
        default_name = "دوري ضفاف العربية" if ttype == "knockout" else "مسابقات ضفاف العربية"
        name = clean_text(data.get("name"), 60) or default_name
        raw_ids = data.get("classIds") if isinstance(data.get("classIds"), list) else []
        class_ids = [c for c in dict.fromkeys(raw_ids) if isinstance(c, str) and c in self.classes]
        tid = new_id("t")
        t = {"id": tid, "name": name, "type": ttype, "createdAt": now_ts(), "classIds": class_ids}
        if ttype == "knockout":
            if len(class_ids) < 2:
                raise GameError("اختر فصلين على الأقل لدوري خروج المغلوب")
            if len(class_ids) > 64:
                raise GameError("الحد الأقصى 64 فصلًا")
            pairing = "random" if data.get("pairing") == "random" else "grade"
            t.update({
                "questionsPerMatch": to_int(data.get("questionsPerMatch"), 10, 1, 50),
                "pairing": pairing, "rounds": [], "usedQuestions": {},
                "championId": None, "status": "running",
            })
            self.tournaments[tid] = t
            self._build_bracket(t, pairing)
        else:
            if not class_ids:
                raise GameError("اختر فصلًا واحدًا على الأقل")
            t["contests"] = {cid: idle_contest() for cid in class_ids}
            self.tournaments[tid] = t
        if not self._busy():  # make the new competition the active one
            self._clear_live()
            self.settings["activeTournamentId"] = tid
            self._dirty_all_players()
        self.touch_data()
        return {"ok": True, "tournamentId": tid}

    def _build_bracket(self, t: dict, pairing: str) -> None:
        teams = [self.classes[c] for c in t["classIds"]]
        n = len(teams)
        size = 1
        while size < n:
            size *= 2
        byes = size - n
        slots: list[list[dict]] = []
        if pairing == "random":
            _rng.shuffle(teams)
            m = n - size // 2
            pairs = [[teams[2 * i], teams[2 * i + 1]] for i in range(m)]
            singles = [[x] for x in teams[2 * m:]]
            while pairs or singles:  # interleave so byes are spread out
                if pairs:
                    slots.append(pairs.pop(0))
                if singles:
                    slots.append(singles.pop(0))
        else:
            ordered = sorted(teams, key=lambda c: (c["grade"], c["section"]))
            by_grade: dict[int, list[dict]] = defaultdict(list)
            for c in ordered:
                by_grade[c["grade"]].append(c)
            pairs, singles = [], []
            for g in sorted(by_grade):
                group = by_grade[g]
                for i in range(0, len(group) - 1, 2):
                    pairs.append([group[i], group[i + 1]])
                if len(group) % 2:
                    singles.append(group[-1])
            if len(singles) <= byes:
                for _ in range((byes - len(singles)) // 2):  # split pairs to create byes
                    singles.extend(pairs.pop())
                bye_teams, cross = singles, []
            else:
                singles.sort(key=lambda c: (c["grade"], c["section"]))
                k = (len(singles) - byes) // 2
                cross = [[singles[2 * i], singles[2 * i + 1]] for i in range(k)]
                bye_teams = singles[2 * k:]
            slots = pairs + cross + [[x] for x in bye_teams]
            slots.sort(key=lambda s: min((c["grade"], c["section"]) for c in s))
        assert len(slots) == size // 2

        rounds: list[list[str]] = []
        count, r = size // 2, 0
        while count >= 1:
            ids = []
            for i in range(count):
                mid = new_id("m")
                self.matches[mid] = {
                    "id": mid, "tournamentId": t["id"], "round": r, "slot": i,
                    "roundName": round_name(count, r), "classA": None, "classB": None,
                    "status": "pending", "bye": False, "winner": None, "result": None,
                    "nextMatchId": None, "nextSlot": None, "finishedAt": None,
                }
                ids.append(mid)
            rounds.append(ids)
            count //= 2
            r += 1
        for r in range(len(rounds) - 1):
            for i, mid in enumerate(rounds[r]):
                self.matches[mid]["nextMatchId"] = rounds[r + 1][i // 2]
                self.matches[mid]["nextSlot"] = "A" if i % 2 == 0 else "B"
        t["rounds"] = rounds
        for i, slot in enumerate(slots):
            m = self.matches[rounds[0][i]]
            m["classA"] = slot[0]["id"]
            if len(slot) == 2:
                m["classB"] = slot[1]["id"]
            else:
                m.update({"bye": True, "status": "finished", "winner": slot[0]["id"], "finishedAt": now_ts()})
                self._advance(t, m)

    def tournament_activate(self, data) -> dict:
        tid = as_dict(data).get("tournamentId")
        if tid is not None and tid not in self.tournaments:
            raise GameError("المسابقة غير موجودة")
        if tid == self.settings.get("activeTournamentId"):
            return {"ok": True}
        busy = self._busy()
        if busy:
            raise GameError(busy)
        self._clear_live()
        self.settings["activeTournamentId"] = tid
        self.touch_data()
        self._dirty_all_players()
        return {"ok": True}

    def tournament_delete(self, data) -> dict:
        tid = as_dict(data).get("tournamentId")
        t = self.tournaments.get(tid)
        if not t:
            raise GameError("المسابقة غير موجودة")
        if self.settings.get("activeTournamentId") == tid:
            busy = self._busy()
            if busy:
                raise GameError(busy)
            self.settings["activeTournamentId"] = None
        if self.live and self.live.tid == tid:
            self._clear_live()
        for mid in [mid for mid, m in self.matches.items() if m["tournamentId"] == tid]:
            del self.matches[mid]
        for key in [k for k in self.crt if k.startswith(tid + ":")]:
            del self.crt[key]
        del self.tournaments[tid]
        self.touch_data()
        self._dirty_all_players()
        return {"ok": True}

    def tournament_set_questions(self, data) -> dict:
        data = as_dict(data)
        t = self.tournaments.get(data.get("tournamentId"))
        if not t or t["type"] != "knockout":
            raise GameError("المسابقة غير موجودة")
        if self.live and self.live.tid == t["id"] and self.live.phase != "done":
            raise GameError("لا يمكن التغيير أثناء مباراة جارية")
        t["questionsPerMatch"] = to_int(data.get("questionsPerMatch"), t["questionsPerMatch"], 1, 50)
        self.touch_data()
        return {"ok": True}

    def _clean_ids(self, raw) -> list[str]:
        return [c for c in dict.fromkeys(raw if isinstance(raw, list) else []) if isinstance(c, str) and c in self.classes]

    def tournament_update(self, data) -> dict:
        data = as_dict(data)
        t = self.tournaments.get(data.get("tournamentId"))
        if not t:
            raise GameError("المسابقة غير موجودة")
        if "name" in data:
            name = clean_text(data.get("name"), 60)
            if not name:
                raise GameError("اكتب اسم المسابقة")
            t["name"] = name
        if t["type"] == "knockout":
            if "questionsPerMatch" in data:
                if self.live and self.live.tid == t["id"] and self.live.phase != "done":
                    raise GameError("لا يمكن تغيير عدد الأسئلة أثناء مباراة جارية")
                t["questionsPerMatch"] = to_int(data.get("questionsPerMatch"), t["questionsPerMatch"], 1, 50)
            pairing = data.get("pairing") if data.get("pairing") in ("grade", "random") else None
            if "classIds" in data or pairing or data.get("redraw"):
                ids = self._clean_ids(data.get("classIds")) if "classIds" in data else list(t["classIds"])
                self._rebuild_bracket(t, ids, pairing or t.get("pairing", "grade"))
        elif "classIds" in data:
            ids = self._clean_ids(data.get("classIds"))
            if not ids:
                raise GameError("اختر فصلًا واحدًا على الأقل")
            for cid in t["classIds"]:
                if cid not in ids and t["contests"].get(cid, {}).get("status") in ("lobby", "running"):
                    raise GameError(f"مسابقة الفصل {self.class_name(cid)} مفتوحة الآن. أنهِها قبل إزالته.")
            for cid in list(t["contests"]):
                if cid not in ids:
                    t["contests"].pop(cid, None)
                    self.crt.pop(f"{t['id']}:{cid}", None)
            for cid in ids:
                t["contests"].setdefault(cid, idle_contest())
            t["classIds"] = ids
        self.touch_data()
        self._dirty_all_players()
        return {"ok": True}

    def _rebuild_bracket(self, t: dict, class_ids: list[str], pairing: str) -> None:
        if len(class_ids) < 2:
            raise GameError("اختر فصلين على الأقل لدوري خروج المغلوب")
        if len(class_ids) > 64:
            raise GameError("الحد الأقصى 64 فصلًا")
        mine = [m for m in self.matches.values() if m["tournamentId"] == t["id"]]
        if any(not m["bye"] and m["status"] != "pending" for m in mine):
            raise GameError("لا يمكن إعادة إنشاء الجدول بعد بدء أي مباراة. أعد ضبط المباريات الملعوبة أولًا.")
        for m in mine:
            del self.matches[m["id"]]
        t.update({"classIds": class_ids, "pairing": pairing, "rounds": [], "championId": None,
                  "status": "running", "usedQuestions": {}})
        self._build_bracket(t, pairing)

    def match_set_teams(self, data) -> dict:
        data = as_dict(data)
        m, t = self._knockout_match(data.get("matchId"))
        if m["bye"] or m["status"] != "pending":
            raise GameError("يمكن تعديل طرفي المباراة قبل لعبها فقط")
        a, b = data.get("classA"), data.get("classB")
        if a not in t["classIds"] or b not in t["classIds"]:
            raise GameError("اختر فصلين من الفصول المشاركة")
        if a == b:
            raise GameError("اختر فصلين مختلفين")
        for other_id in t["rounds"][m["round"]]:
            o = self.matches.get(other_id)
            if not o or o["id"] == m["id"]:
                continue
            if {a, b} & ({o["classA"], o["classB"]} - {None}):
                raise GameError(f"أحد الفصلين موجود في مباراة أخرى في {m['roundName']}")
        m["classA"], m["classB"] = a, b
        self.touch_data()
        return {"ok": True}

    def _dirty_all_players(self) -> None:
        self.dirty_players.update(self.player_sids.keys())

    # ------------------------------------------------------- knockout matches
    def _require_live(self, phases: tuple[str, ...] | None = None) -> LiveMatch:
        L = self.live
        if not L:
            raise GameError("لا توجد مباراة مفتوحة")
        if phases and L.phase not in phases:
            raise GameError("هذا الإجراء غير متاح في هذه المرحلة")
        return L

    def _clear_live(self) -> None:
        L = self.live
        if not L:
            return
        self.dirty_players.update(L.roster.keys())
        self.live = None
        self.touch_live()

    def _knockout_match(self, mid) -> tuple[dict, dict]:
        m = self.matches.get(mid) if isinstance(mid, str) else None
        if not m:
            raise GameError("المباراة غير موجودة")
        return m, self.tournaments[m["tournamentId"]]

    def match_open(self, data) -> dict:
        m, t = self._knockout_match(as_dict(data).get("matchId"))
        if self.settings.get("activeTournamentId") != t["id"]:
            raise GameError("فعّل هذا الدوري أولًا من تبويب «الدوري والجولات»")
        if self.live and self.live.phase != "done":
            raise GameError("توجد مباراة مفتوحة الآن. أنهِها أو ألغِها أولًا.")
        if m["status"] != "pending":
            raise GameError("هذه المباراة ليست بانتظار اللعب")
        if not m["classA"] or not m["classB"]:
            raise GameError("لم يتحدد طرفا المباراة بعد")
        self._clear_live()
        L = LiveMatch(m, t["questionsPerMatch"])
        self.live = L
        m["status"] = "open"
        self.settings["displayMode"] = "auto"
        for pid in list(self.player_sids):
            p = self.players.get(pid)
            side = L.side_of_class(p["classId"]) if p else None
            if side:
                L.roster[pid] = self._roster_entry(p, side)
                L.lobby_members.add(pid)
        self.dirty_players.update(L.roster.keys())
        self.touch_data()
        warning = self._bank_warning(m, t["questionsPerMatch"])
        return {"ok": True, "warning": warning}

    def _bank_warning(self, m: dict, n: int) -> str | None:
        short = [f"{c['name']} ({len(c['questions'])} سؤال)" for c in (self.classes.get(m["classA"]), self.classes.get(m["classB"]))
                 if c and len(c["questions"]) < n]
        if short:
            return f"تنبيه: كل فصل يحتاج {n} أسئلة على الأقل. الفصول الناقصة: " + "، ".join(short)
        return None

    def _pick_questions(self, t: dict, c: dict, n: int) -> list[dict]:
        used = set(t.setdefault("usedQuestions", {}).get(c["id"], []))
        fresh = [q for q in c["questions"] if q["id"] not in used]
        old = [q for q in c["questions"] if q["id"] in used]
        _rng.shuffle(fresh)
        _rng.shuffle(old)
        chosen = (fresh + old)[:n]
        t["usedQuestions"][c["id"]] = sorted(used | {q["id"] for q in chosen})
        return [copy.deepcopy(q) for q in chosen]

    def match_start(self, data=None) -> dict:
        L = self._require_live(("lobby",))
        m = self.matches.get(L.match_id)
        t = self.tournaments.get(L.tid)
        if not m or not t:
            self._clear_live()
            raise GameError("المباراة لم تعد موجودة")
        for side in "AB":
            c = self.classes.get(L.cls[side])
            if not c:
                raise GameError("أحد الفصلين محذوف")
            if len(c["questions"]) < L.n:
                raise GameError(
                    f"بنك أسئلة الفصل {c['name']} فيه {len(c['questions'])} سؤال فقط، والمطلوب {L.n}. "
                    "أضف أسئلة أو قلّل عدد أسئلة المباراة."
                )
        for side in "AB":
            L.questions[side] = self._pick_questions(t, self.classes[L.cls[side]], L.n)
        for pid in list(L.roster):  # students who left the lobby are not counted
            if not self.online(pid) and not L.roster[pid]["kicked"]:
                del L.roster[pid]
        L.phase = "countdown"
        L.qi = 0
        L.phase_end = time.monotonic() + self.countdown_seconds()
        m["status"] = "live"
        self.dirty_players.update(L.roster.keys())
        self.touch_data()
        return {"ok": True}

    def _begin_question(self, now: float) -> None:
        L = self.live
        L.phase = "question"
        L.q_start = now
        L.time_up = False
        while len(L.answers) <= L.qi:
            L.answers.append({})
        for side in "AB":
            q = L.questions[side][L.qi]
            L.deadline[side] = now + q["time"]
            L.display_perm[side] = shuffled(len(options_of(q)))
        L.perms = {}
        for pid, e in L.roster.items():
            e["last"] = None
            if not e["kicked"]:
                L.perms[pid] = shuffled(len(options_of(L.questions[e["side"]][L.qi])))
        self.dirty_players.update(L.roster.keys())
        self.touch_live()

    def _ko_answer(self, pid: str, data: dict) -> dict:
        L = self.live
        now = time.monotonic()
        if L.phase != "question":
            raise GameError("انتهى وقت السؤال", "late")
        e = L.roster.get(pid)
        if not e or e["kicked"]:
            raise GameError("لست مشاركًا في هذه المباراة")
        if to_int(data.get("q"), -2, -2, 999) != L.qi:
            raise GameError("هذا السؤال انتهى", "late")
        answers = L.answers[L.qi]
        if pid in answers:
            raise GameError("سُجّلت إجابتك مسبقًا", "dup")
        side = e["side"]
        if now > L.deadline[side] + GRACE:
            raise GameError("انتهى الوقت", "late")
        perm = L.perms.get(pid)
        choice = to_int(data.get("choice"), -1, -1, 99)
        if not perm or not 0 <= choice < len(perm):
            raise GameError("اختيار غير صالح")
        canon = perm[choice]
        q = L.questions[side][L.qi]
        T = float(q["time"])
        rt = min(max(now - L.q_start, 0.0), T)
        correct = canon == 0
        if correct:
            e["streak"] += 1
            e["best"] = max(e["best"], e["streak"])
            pts = round(KO_MAX_POINTS * (1 - (rt / T) / 2)) + min(e["streak"] - 1, 3) * 100
        else:
            e["streak"] = 0
            pts = 0
        answers[pid] = {"choice": canon, "time": round(rt, 3), "correct": correct, "points": pts}
        e["points"] += pts
        e["answered"] += 1
        e["correct"] += int(correct)
        e["times"].append(round(rt, 3))
        self.dirty_players.add(pid)
        self.touch_live()
        online = [p for p, x in L.active() if self.online(p)]
        if self.settings.get("autoReveal", True) and online and all(p in answers for p in online):
            self._reveal(now)
        return {"ok": True}

    def _reveal(self, now: float) -> None:
        L = self.live
        L.phase = "reveal"
        L.revealed = L.qi + 1
        answers = L.answers[L.qi]
        for pid, e in L.active():
            a = answers.get(pid)
            if a is None:
                e["streak"] = 0
                e["last"] = {"choice": None, "correct": False, "points": 0, "missed": True}
                if not self.online(pid):
                    continue
            else:
                e["last"] = a
            self._stat_answer(pid, e["name"], L.cls[e["side"]], L.questions[e["side"]][L.qi],
                              bool(a and a["correct"]), a["points"] if a else 0, "koPoints")
        self.dirty_players.update(L.roster.keys())
        self.touch_live()
        self.save()

    def match_reveal(self, data=None) -> dict:
        self._require_live(("question",))
        self._reveal(time.monotonic())
        return {"ok": True}

    def match_next(self, data=None) -> dict:
        L = self._require_live(("reveal",))
        if L.qi + 1 < L.n:
            L.qi += 1
            L.phase = "countdown"
            L.phase_end = time.monotonic() + self.countdown_seconds()
            self.dirty_players.update(L.roster.keys())
            self.touch_live()
        else:
            self._finish_match(early=False)
        return {"ok": True}

    def _side_score(self, L: LiveMatch, side: str) -> dict:
        es = [e for _, e in L.active() if e["side"] == side]
        n = len(es)
        total = sum(e["points"] for e in es)
        correct = sum(e["correct"] for e in es)
        times = [x for e in es for x in e["times"]]
        q_done = L.revealed
        return {
            "classId": L.cls[side], "name": self.class_name(L.cls[side]), "players": n,
            "avg": round(total / n, 2) if n else 0.0,
            "accuracy": round(correct / (n * q_done), 4) if n and q_done else 0.0,
            "avgTime": round(sum(times) / len(times), 2) if times else None,
            "total": total,
        }

    def _top_players(self, L: LiveMatch, k: int = 5) -> list[dict]:
        rows = sorted(L.active(), key=lambda pe: (-pe[1]["points"], -pe[1]["correct"]))[:k]
        return [{"name": e["name"], "className": self.class_name(L.cls[e["side"]]), "side": e["side"],
                 "points": e["points"], "correct": e["correct"], "streak": e["streak"]} for _, e in rows]

    def _finish_match(self, early: bool) -> None:
        L = self.live
        m = self.matches.get(L.match_id)
        t = self.tournaments.get(L.tid)
        res = {"A": self._side_score(L, "A"), "B": self._side_score(L, "B")}
        a, b = res["A"], res["B"]
        winner, how = None, "tie"
        if a["players"] == 0 and b["players"] > 0:
            winner, how = L.cls["B"], "forfeit"
        elif b["players"] == 0 and a["players"] > 0:
            winner, how = L.cls["A"], "forfeit"
        elif a["players"] or b["players"]:
            ka, kb = score_key(a), score_key(b)
            if ka != kb:
                winner = L.cls["A"] if ka > kb else L.cls["B"]
                how = "score" if ka[0] != kb[0] else "accuracy" if ka[1] != kb[1] else "speed"
        L.phase = "done"
        if m:
            m["result"] = {"A": a, "B": b, "questions": L.revealed, "decidedBy": how,
                           "tie": winner is None, "early": early, "top": self._top_players(L, 5)}
            m["status"] = "finished"
            m["finishedAt"] = now_ts()
            m["winner"] = winner
            if winner and t:
                self._advance(t, m)
            self._history_knockout(m, t)
        for pid, e in L.active():
            sp = self.stats["players"].get(pid)
            if sp:
                sp["sessions"] = sp.get("sessions", 0) + 1
        self.dirty_players.update(L.roster.keys())
        self.touch_data()

    def _advance(self, t: dict, m: dict) -> None:
        nid = m.get("nextMatchId")
        if not nid:
            changed = t.get("championId") != m["winner"]
            t["championId"] = m["winner"]
            t["status"] = "finished" if m["winner"] else "running"
            t["finishedAt"] = now_ts()
            if changed and m["winner"]:
                self.emit("display:champion", {"name": self.class_name(m["winner"]), "tournament": t["name"]}, "display")
            return
        nm = self.matches.get(nid)
        if nm:
            nm["classA" if m["nextSlot"] == "A" else "classB"] = m["winner"]

    def _history_knockout(self, m: dict, t: dict | None) -> None:
        res = m.get("result") or {}
        entry = {
            "kind": "knockout", "id": m["id"], "tournamentId": m["tournamentId"],
            "tournament": t["name"] if t else "", "round": m["roundName"], "at": now_ts(),
            "A": res.get("A") or {"classId": m["classA"], "name": self.class_name(m["classA"])},
            "B": res.get("B") or {"classId": m["classB"], "name": self.class_name(m["classB"])},
            "winnerId": m.get("winner"), "winner": self.class_name(m["winner"]) if m.get("winner") else None,
            "decidedBy": res.get("decidedBy"),
        }
        self.db["history"] = [h for h in self.db["history"] if h.get("id") != m["id"]][-999:] + [entry]

    def match_cancel(self, data=None) -> dict:
        L = self._require_live()
        if L.phase == "done":
            self._clear_live()
            return {"ok": True}
        m = self.matches.get(L.match_id)
        if m:
            m.update({"status": "pending", "winner": None, "result": None})
        for pid in L.roster:
            self.notify_player(pid, "أُلغيت المباراة. انتظر تعليمات المعلم.", "warn")
        self._clear_live()
        self.touch_data()
        return {"ok": True}

    def match_close(self, data=None) -> dict:
        L = self._require_live()
        if L.phase == "lobby":
            return self.match_cancel()
        if L.phase == "done":
            self._clear_live()
            self.touch_data()
            return {"ok": True}
        if L.phase == "question":
            self._reveal(time.monotonic())
        self._finish_match(early=True)
        return {"ok": True}

    def _downstream_free(self, m: dict) -> None:
        nid = m.get("nextMatchId")
        nm = self.matches.get(nid) if nid else None
        if nm and nm["status"] != "pending":
            raise GameError(f"المباراة التالية ({nm['roundName']}) بدأت أو انتهت. أعد ضبطها أولًا.")

    def match_set_winner(self, data) -> dict:
        data = as_dict(data)
        m, t = self._knockout_match(data.get("matchId"))
        cid = data.get("classId")
        if m["bye"]:
            raise GameError("هذا تأهل مباشر")
        if not m["classA"] or not m["classB"]:
            raise GameError("لم يتحدد طرفا المباراة بعد")
        if cid not in (m["classA"], m["classB"]):
            raise GameError("اختر أحد طرفي المباراة")
        L = self.live
        if L and L.match_id == m["id"]:
            if L.phase in ("countdown", "question", "reveal"):
                raise GameError("المباراة جارية. أنهِها أولًا ثم حدّد الفائز.")
            if L.phase == "lobby":
                self._clear_live()
                m["status"] = "pending"
        if m["status"] == "finished" and m.get("winner") and m["winner"] != cid:
            self._downstream_free(m)
        m["winner"] = cid
        m["status"] = "finished"
        m["finishedAt"] = now_ts()
        m["result"] = {**(m.get("result") or {}), "decidedBy": "manual", "tie": False}
        self._advance(t, m)
        self._history_knockout(m, t)
        self.touch_data()
        if L and L.match_id == m["id"]:
            self.dirty_players.update(L.roster.keys())
        return {"ok": True}

    def match_reset(self, data) -> dict:
        m, t = self._knockout_match(as_dict(data).get("matchId"))
        if m["bye"]:
            raise GameError("لا يمكن إعادة ضبط تأهل مباشر")
        if m["status"] == "finished":
            self._downstream_free(m)
        L = self.live
        if L and L.match_id == m["id"]:
            if L.phase != "done":
                for pid in L.roster:
                    self.notify_player(pid, "أعاد المعلم ضبط المباراة.", "warn")
            self._clear_live()
        if m["status"] == "finished":
            nid = m.get("nextMatchId")
            if nid and nid in self.matches:
                self.matches[nid]["classA" if m["nextSlot"] == "A" else "classB"] = None
            elif not nid:
                t["championId"] = None
                t["status"] = "running"
        m.update({"status": "pending", "winner": None, "result": None, "finishedAt": None})
        self.db["history"] = [h for h in self.db["history"] if h.get("id") != m["id"]]
        self.touch_data()
        return {"ok": True}

    def kick(self, data) -> dict:
        pid = as_dict(data).get("playerId")
        done = False
        L = self.live
        if L and pid in L.roster and not L.roster[pid]["kicked"]:
            L.roster[pid]["kicked"] = True
            L.perms.pop(pid, None)
            done = True
            if L.phase == "question" and self.settings.get("autoReveal", True):
                online = [p for p, _ in L.active() if self.online(p)]
                if online and all(p in L.answers[L.qi] for p in online):
                    self._reveal(time.monotonic())
        t = self.active_tournament()
        if t and t["type"] == "separate":
            for cid, rec in t["contests"].items():
                pp = rec["players"].get(pid)
                if pp and not pp["kicked"] and rec["status"] in ("lobby", "running", "finished"):
                    pp["kicked"] = True
                    self.crt.get(f"{t['id']}:{cid}", {}).pop(pid, None)
                    done = True
                    if rec["status"] == "finished":
                        rec["result"] = contest_score(rec)
                        self._history_contest(t, cid, rec)
                    else:
                        self._contest_maybe_done(t, cid, rec)
        if not done:
            raise GameError("الطالب غير موجود في المسابقة الحالية")
        self.notify_player(pid, "استبعدك المعلم من هذه المسابقة.", "warn")
        self.dirty_players.add(pid)
        self.touch_data()
        return {"ok": True}

    # ------------------------------------------------------ separate contests
    def _separate_active(self, data) -> dict:
        t = self.active_tournament()
        tid = as_dict(data).get("tournamentId")
        if not t or t["type"] != "separate" or (tid and tid != t["id"]):
            raise GameError("فعّل مسابقة «فصول منفصلة» أولًا من تبويب «الدوري والجولات»")
        return t

    def _ids_from(self, t: dict, data) -> list[str]:
        raw = as_dict(data).get("classIds")
        if raw is None and as_dict(data).get("classId"):
            raw = [as_dict(data)["classId"]]
        if not isinstance(raw, list):
            raw = []
        return [c for c in dict.fromkeys(raw) if isinstance(c, str) and c in t["contests"]]

    def contest_open(self, data) -> dict:
        t = self._separate_active(data)
        ids = self._ids_from(t, data)
        if not ids:
            raise GameError("اختر فصلًا واحدًا على الأقل")
        if self.live and self.live.phase != "done":
            raise GameError("توجد مباراة خروج مغلوب جارية")
        errors, opened = [], 0
        for cid in ids:
            rec = t["contests"][cid]
            c = self.classes.get(cid)
            if rec["status"] != "idle":
                errors.append(f"{self.class_name(cid)}: مفتوحة أو منتهية (أعد ضبطها لإعادتها)")
                continue
            if not c or not c["questions"]:
                errors.append(f"{self.class_name(cid)}: لا توجد أسئلة في بنك الفصل")
                continue
            rec.update(idle_contest())
            rec.update({"status": "lobby", "questions": copy.deepcopy(c["questions"]), "openedAt": now_ts()})
            for pid in list(self.player_sids):
                p = self.players.get(pid)
                if p and p["classId"] == cid:
                    self._contest_add_player(t, cid, rec, pid)
            opened += 1
        if not opened:
            raise GameError("\n".join(errors))
        self.settings["displayMode"] = "auto"
        self.touch_data()
        return {"ok": True, "warning": "\n".join(errors) or None}

    def contest_start(self, data) -> dict:
        t = self._separate_active(data)
        ids = self._ids_from(t, data) or [c for c, r in t["contests"].items() if r["status"] == "lobby"]
        started = 0
        now = time.monotonic()
        for cid in ids:
            rec = t["contests"][cid]
            if rec["status"] != "lobby":
                continue
            for pid in list(rec["players"]):
                if not self.online(pid) and not rec["players"][pid]["kicked"]:
                    del rec["players"][pid]
            rec["status"] = "running"
            rec["startedAt"] = now_ts()
            self.crt[f"{t['id']}:{cid}"] = {}
            for pid, pp in rec["players"].items():
                if not pp["kicked"]:
                    self._contest_send(t, cid, rec, pid, now)
            started += 1
        if not started:
            raise GameError("لا توجد فصول في وضع الانتظار. افتح الفصل أولًا.")
        self.touch_data()
        return {"ok": True}

    def contest_finish(self, data) -> dict:
        t = self._separate_active(data)
        ids = self._ids_from(t, data)
        if not ids:
            raise GameError("اختر الفصل")
        for cid in ids:
            rec = t["contests"][cid]
            if rec["status"] == "lobby":
                self._contest_reset(t, cid, notify="أُلغيت المسابقة قبل بدايتها.")
            elif rec["status"] == "running":
                self._contest_finish(t, cid, rec)
        self.touch_data()
        return {"ok": True}

    def contest_reset(self, data) -> dict:
        t = self._separate_active(data)
        ids = self._ids_from(t, data)
        if not ids:
            raise GameError("اختر الفصل")
        for cid in ids:
            self._contest_reset(t, cid, notify="أعاد المعلم ضبط مسابقة فصلك.")
        self.touch_data()
        return {"ok": True}

    def _contest_reset(self, t: dict, cid: str, notify: str | None = None) -> None:
        rec = t["contests"][cid]
        affected = list(rec["players"])
        t["contests"][cid] = idle_contest()
        key = f"{t['id']}:{cid}"
        self.crt.pop(key, None)
        self.db["history"] = [h for h in self.db["history"] if h.get("id") != key]
        for pid in affected:
            if notify:
                self.notify_player(pid, notify, "warn")
            self.dirty_players.add(pid)
        # students of the class who are online return to the waiting screen
        self.dirty_players.update(p for p in self.player_sids if self.players.get(p, {}).get("classId") == cid)

    def _contest_add_player(self, t: dict, cid: str, rec: dict, pid: str) -> None:
        pp = rec["players"].get(pid)
        if pp is None:
            if rec["status"] == "running" and not self.settings.get("allowLateJoin") and pid not in rec["lobby"]:
                return
            n = len(rec["questions"])
            pp = {"name": self.players[pid]["name"], "order": shuffled(n), "pos": 0, "points": 0.0,
                  "correct": 0, "time": 0.0, "answers": [], "finished": False, "kicked": False,
                  "joinedAt": now_ts(), "finishedAt": None}
            rec["players"][pid] = pp
            if rec["status"] == "lobby" and pid not in rec["lobby"]:
                rec["lobby"].append(pid)
            self.save()
        if rec["status"] == "running" and not pp["kicked"] and not pp["finished"]:
            rts = self.crt.setdefault(f"{t['id']}:{cid}", {})
            rt = rts.get(pid)
            if rt is None or (rt.get("sentAt") is None and rt.get("nextAt") is None):
                self._contest_send(t, cid, rec, pid, time.monotonic())
        self.dirty_players.add(pid)
        self.touch_live()

    def _contest_send(self, t: dict, cid: str, rec: dict, pid: str, now: float) -> None:
        pp = rec["players"][pid]
        if pp["pos"] >= len(rec["questions"]):
            pp["finished"] = True
            return
        qidx = pp["order"][pp["pos"]]
        q = rec["questions"][qidx]
        rts = self.crt.setdefault(f"{t['id']}:{cid}", {})
        prev = rts.get(pid) or {}
        rts[pid] = {"sentAt": now, "deadline": now + q["time"], "perm": shuffled(len(options_of(q))),
                    "qidx": qidx, "nextAt": None, "last": prev.get("last")}
        self.dirty_players.add(pid)
        self.touch_live()

    def _contest_answer(self, t: dict, cid: str, rec: dict, pid: str, data: dict) -> dict:
        pp = rec["players"][pid]
        if pp["kicked"]:
            raise GameError("استُبعدت من هذه المسابقة")
        if pp["finished"]:
            raise GameError("أنهيت جميع الأسئلة")
        rt = self.crt.get(f"{t['id']}:{cid}", {}).get(pid)
        now = time.monotonic()
        if not rt or rt.get("sentAt") is None:
            raise GameError("انتظر السؤال التالي", "late")
        if to_int(data.get("q"), -2, -2, 9999) != pp["pos"]:
            raise GameError("هذا السؤال انتهى", "late")
        if now > rt["deadline"] + GRACE:
            raise GameError("انتهى الوقت", "late")
        choice = to_int(data.get("choice"), -1, -1, 99)
        if not 0 <= choice < len(rt["perm"]):
            raise GameError("اختيار غير صالح")
        self._contest_record(t, cid, rec, pid, rt["perm"][choice], now)
        return {"ok": True}

    def _contest_record(self, t: dict, cid: str, rec: dict, pid: str, canon: int | None, now: float) -> None:
        pp = rec["players"][pid]
        rt = self.crt[f"{t['id']}:{cid}"][pid]
        q = rec["questions"][rt["qidx"]]
        T = float(q["time"])
        took = min(max(now - rt["sentAt"], 0.0), T) if canon is not None else T
        correct = canon == 0
        pts = round(CONTEST_MAX_POINTS * (1 - (took / T) / 2), 2) if correct else 0.0
        pp["answers"].append({"q": rt["qidx"], "c": canon, "ok": correct, "pts": pts, "t": round(took, 2)})
        pp["points"] = round(pp["points"] + pts, 2)
        pp["correct"] += int(correct)
        pp["time"] = round(pp["time"] + took, 2)
        pp["pos"] += 1
        self._stat_answer(pid, pp["name"], cid, q, correct, pts, "contestPoints")
        rt.update({"sentAt": None, "deadline": None,
                   "last": {"correct": correct, "points": pts, "timeout": canon is None}})
        if pp["pos"] >= len(rec["questions"]):
            pp["finished"] = True
            pp["finishedAt"] = now_ts()
            rt["nextAt"] = None
            sp = self.stats["players"].get(pid)
            if sp:
                sp["sessions"] = sp.get("sessions", 0) + 1
        else:
            rt["nextAt"] = now + FEEDBACK_DELAY
        self.dirty_players.add(pid)
        self.touch_live()
        self.save()
        self._contest_maybe_done(t, cid, rec)

    def _contest_maybe_done(self, t: dict, cid: str, rec: dict) -> None:
        if rec["status"] != "running":
            return
        ps = [pp for pp in rec["players"].values() if not pp["kicked"]]
        if ps and all(pp["finished"] for pp in ps):
            self._contest_finish(t, cid, rec)

    def _contest_finish(self, t: dict, cid: str, rec: dict) -> None:
        rec["status"] = "finished"
        rec["finishedAt"] = now_ts()
        rec["result"] = contest_score(rec)
        self.crt.pop(f"{t['id']}:{cid}", None)
        self._history_contest(t, cid, rec)
        # every finished class may now see its review (it waits for all contests to end)
        for other in t["contests"].values():
            self.dirty_players.update(other["players"].keys())
        self.touch_data()

    def _history_contest(self, t: dict, cid: str, rec: dict) -> None:
        key = f"{t['id']}:{cid}"
        entry = {"kind": "contest", "id": key, "tournamentId": t["id"], "tournament": t["name"],
                 "at": rec.get("finishedAt") or now_ts(), "classId": cid, "class": self.class_name(cid),
                 **(rec.get("result") or {})}
        self.db["history"] = [h for h in self.db["history"] if h.get("id") != key][-999:] + [entry]

    # ---------------------------------------------------------------- ticking
    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        L = self.live
        if L:
            if L.phase == "countdown" and now >= L.phase_end:
                self._begin_question(now)
            elif L.phase == "question" and now >= max(L.deadline.values()) + GRACE:
                if self.settings.get("autoReveal", True):
                    self._reveal(now)
                elif not L.time_up:
                    L.time_up = True
                    self.touch_live()
        for key in list(self.crt):
            tid, cid = key.split(":", 1)
            t = self.tournaments.get(tid)
            rec = t["contests"].get(cid) if t and t["type"] == "separate" else None
            if not rec or rec["status"] != "running":
                self.crt.pop(key, None)
                continue
            for pid, rt in list(self.crt.get(key, {}).items()):
                pp = rec["players"].get(pid)
                if not pp or pp["kicked"] or pp["finished"]:
                    continue
                if rt.get("sentAt") is not None and now > rt["deadline"] + GRACE:
                    self._contest_record(t, cid, rec, pid, None, now)
                    if rec["status"] != "running":
                        break
                elif rt.get("nextAt") is not None and now >= rt["nextAt"]:
                    rt["nextAt"] = None
                    if self.online(pid):
                        self._contest_send(t, cid, rec, pid, now)
                    # an offline student gets the next question when they reconnect

    # ----------------------------------------------------------------- stats
    def _stat_answer(self, pid: str, name: str, cid: str, q: dict, correct: bool, points, kind: str) -> None:
        sp = self.stats["players"].setdefault(pid, {
            "name": name, "classId": cid, "answered": 0, "correct": 0,
            "koPoints": 0, "contestPoints": 0.0, "sessions": 0,
        })
        sp["name"] = name
        sp["answered"] += 1
        sp["correct"] += int(correct)
        sp[kind] = round(sp.get(kind, 0) + points, 2)
        sq = self.stats["questions"].setdefault(q["id"], {"text": q["text"], "classId": cid, "answered": 0, "correct": 0})
        sq["text"] = q["text"]
        sq["field"] = q.get("field", "")
        sq["answered"] += 1
        sq["correct"] += int(correct)

    def student_stat_rows(self) -> list[dict]:
        players = []
        for pid, sp in self.stats["players"].items():
            ans = sp.get("answered", 0)
            players.append({
                "name": sp.get("name", "—"), "className": self.class_name(sp.get("classId")),
                "answered": ans, "correct": sp.get("correct", 0),
                "accuracy": round(sp.get("correct", 0) / ans, 3) if ans else 0,
                "koPoints": sp.get("koPoints", 0), "contestPoints": sp.get("contestPoints", 0),
            })
        players.sort(key=lambda r: (-r["correct"], -r["accuracy"], -r["koPoints"], -r["contestPoints"]))
        return players

    def stats_summary(self) -> dict:
        players = self.student_stat_rows()
        hardest = []
        for qid, sq in self.stats["questions"].items():
            if sq.get("answered", 0) >= 1:
                hardest.append({
                    "text": sq.get("text", ""), "className": self.class_name(sq.get("classId")),
                    "field": FIELDS.get(sq.get("field") or "", ""),
                    "answered": sq["answered"], "correct": sq.get("correct", 0),
                    "rate": round(sq.get("correct", 0) / sq["answered"], 3),
                })
        hardest.sort(key=lambda r: (r["rate"], -r["answered"]))
        standings: dict[str, dict] = {}

        def row(cid, name):
            return standings.setdefault(cid or name, {
                "name": name, "koPlayed": 0, "koWins": 0, "koLosses": 0, "koAvgSum": 0.0,
                "contests": 0, "contestAvgSum": 0.0,
            })

        for h in self.db["history"]:
            if h.get("kind") == "knockout":
                for side in ("A", "B"):
                    s = h.get(side) or {}
                    r = row(s.get("classId"), self.class_name(s.get("classId")) if s.get("classId") in self.classes else s.get("name", "—"))
                    r["koPlayed"] += 1
                    r["koAvgSum"] += s.get("avg") or 0
                    if h.get("winnerId") == s.get("classId"):
                        r["koWins"] += 1
                    elif h.get("winnerId"):
                        r["koLosses"] += 1
            elif h.get("kind") == "contest":
                r = row(h.get("classId"), self.class_name(h.get("classId")) if h.get("classId") in self.classes else h.get("class", "—"))
                r["contests"] += 1
                r["contestAvgSum"] += h.get("avg") or 0
        table = []
        for r in standings.values():
            table.append({
                "name": r["name"], "koPlayed": r["koPlayed"], "koWins": r["koWins"], "koLosses": r["koLosses"],
                "koAvg": round(r["koAvgSum"] / r["koPlayed"], 1) if r["koPlayed"] else None,
                "contests": r["contests"],
                "contestAvg": round(r["contestAvgSum"] / r["contests"], 2) if r["contests"] else None,
            })
        table.sort(key=lambda r: (-r["koWins"], -(r["contestAvg"] or 0), -(r["koAvg"] or 0)))
        fields = {k: {"key": k, "name": v, "answered": 0, "correct": 0} for k, v in FIELDS.items()}
        for sq in self.stats["questions"].values():
            f = fields.get(sq.get("field") or "")
            if f:
                f["answered"] += sq.get("answered", 0)
                f["correct"] += sq.get("correct", 0)
        by_field = [{**f, "rate": round(f["correct"] / f["answered"], 3) if f["answered"] else None} for f in fields.values()]
        return {
            "topStudents": players[:30],
            "byField": by_field,
            "hardest": hardest[:20],
            "history": list(reversed(self.db["history"]))[:100],
            "standings": table,
            "totals": {"players": len(self.players), "answers": sum(p["answered"] for p in players),
                       "questions": sum(len(c["questions"]) for c in self.classes.values())},
        }

    def stats_reset(self, data=None) -> dict:
        self.db["stats"] = {"players": {}, "questions": {}}
        self.db["history"] = []
        self.touch_data()
        return {"ok": True}

    def data_reset(self, data=None) -> dict:
        if as_dict(data).get("confirm") != "RESET":
            raise GameError("التأكيد مطلوب")
        self._replace_db(empty_db())
        return {"ok": True}

    def data_import(self, raw) -> dict:
        if not isinstance(raw, dict) or not isinstance(raw.get("classes"), dict):
            raise GameError("الملف لا يحتوي على بيانات صالحة لهذا الموقع")
        try:
            db = normalize_db(copy.deepcopy(raw))
        except Exception as exc:  # malformed nested data
            raise GameError(f"تعذّرت قراءة الملف: {exc}") from exc
        self._replace_db(db)
        return {"ok": True, "classes": len(db["classes"])}

    def export(self) -> dict:
        return copy.deepcopy(self.db)

    def _replace_db(self, db: dict) -> None:
        for pid in list(self.player_sids):
            if pid not in db["players"]:
                self._logout_player(pid)
        self.live = None
        self.crt.clear()
        self.db = db
        self.store.replace(db)
        self._dirty_all_players()
        self.touch_data()

    # -------------------------------------------------------------- settings
    def display_set(self, data) -> dict:
        mode = as_dict(data).get("mode")
        if mode not in DISPLAY_MODES:
            raise GameError("وضع عرض غير معروف")
        self.settings["displayMode"] = mode
        self.touch_data()
        return {"ok": True}

    def display_opening(self, data=None) -> dict:
        self.emit("display:opening", {}, "display")
        return {"ok": True}

    def set_late_join(self, data) -> dict:
        self.settings["allowLateJoin"] = bool(as_dict(data).get("allow"))
        self.touch_data()
        return {"ok": True}

    def settings_update(self, data) -> dict:
        data = as_dict(data)
        s = self.settings
        if "countdown" in data:
            s["countdown"] = to_int(data["countdown"], s.get("countdown", 3), 3, 10)
        for key in ("autoReveal", "reviewAfterContest", "showLeadersOnHome", "allowLateJoin"):
            if key in data:
                s[key] = bool(data[key])
        self.touch_data()
        self._dirty_all_players()
        return {"ok": True}

    def site_update(self, data) -> dict:
        self.settings["site"] = clean_site(data, self.settings.get("site"))
        self.touch_data()
        return {"ok": True}

    def display_message(self, data) -> dict:
        data = as_dict(data)
        if data.get("show", True):
            text = clean_text(data.get("text"), 200, multiline=True)
            if not text:
                raise GameError("اكتب الرسالة أولًا")
            self.settings["displayMessage"] = text
            self.settings["displayMode"] = "message"
        elif self.settings.get("displayMode") == "message":
            self.settings["displayMode"] = "auto"
        self.touch_data()
        return {"ok": True}

    # ------------------------------------------------------------ public info
    def public_site(self) -> dict:
        s = self.settings
        live = None
        L = self.live
        if L and L.phase != "done":
            m = self.matches.get(L.match_id) or {}
            live = {"kind": "knockout", "round": m.get("roundName", ""), "phase": L.phase,
                    "A": self.class_name(L.cls["A"]), "B": self.class_name(L.cls["B"])}
        else:
            t = self.active_tournament()
            if t and t["type"] == "separate":
                running = [self.class_name(c) for c in t["classIds"] if t["contests"].get(c, {}).get("status") in ("lobby", "running")]
                if running:
                    live = {"kind": "contest", "title": t["name"], "classes": running}
        champions, contest_leaders = [], []
        for t in sorted(self.tournaments.values(), key=lambda x: -(x.get("createdAt") or 0)):
            if t["type"] == "knockout" and t.get("championId"):
                champions.append({"tournament": t["name"], "name": self.class_name(t["championId"])})
            elif t["type"] == "separate":
                done = [r for r in self._contests_public(t) if r["rank"] == 1]
                if done:
                    contest_leaders.append({"tournament": t["name"], "name": done[0]["name"], "avg": done[0]["avg"]})
        return {
            "site": copy.deepcopy(s.get("site") or DEFAULT_SITE),
            "stats": {
                "classes": len(self.classes),
                "questions": sum(len(c["questions"]) for c in self.classes.values()),
                "tournaments": len(self.tournaments),
                "answers": sum(sp.get("answered", 0) for sp in self.stats["players"].values()),
                "students": len(self.players),
            },
            "live": live,
            "champions": champions[:6],
            "contestLeaders": contest_leaders[:6],
            "topStudents": [{"name": r["name"], "className": r["className"], "correct": r["correct"]}
                            for r in self._top_students(5)] if s.get("showLeadersOnHome", True) else [],
        }

    def csv_table(self, kind: str, class_id: str | None = None) -> tuple[list[str], list[list]]:
        if kind == "students":
            head = ["الطالب", "الفصل", "عدد الإجابات", "الإجابات الصحيحة", "الدقة %", "نقاط الدوري", "نقاط المسابقات"]
            rows = [[r["name"], r["className"], r["answered"], r["correct"], round(r["accuracy"] * 100),
                     r["koPoints"], r["contestPoints"]] for r in self.student_stat_rows()]
            return head, rows
        if kind == "history":
            head = ["التاريخ", "المسابقة", "النوع", "الجولة", "الفصل", "النتيجة", "المنافس", "نتيجة المنافس",
                    "الفائز", "الدقة %", "عدد الطلاب"]
            rows = []
            for h in self.db["history"]:
                at = time.strftime("%Y-%m-%d %H:%M", time.localtime(h.get("at") or 0))
                if h.get("kind") == "knockout":
                    a, b = h.get("A") or {}, h.get("B") or {}
                    rows.append([at, h.get("tournament", ""), "خروج المغلوب", h.get("round", ""), a.get("name", ""),
                                 a.get("avg", ""), b.get("name", ""), b.get("avg", ""), h.get("winner") or "تعادل",
                                 round((a.get("accuracy") or 0) * 100), (a.get("players") or 0) + (b.get("players") or 0)])
                else:
                    rows.append([at, h.get("tournament", ""), "فصول منفصلة", "", h.get("class", ""), h.get("avg", ""),
                                 "", "", "", round((h.get("accuracy") or 0) * 100), h.get("players", 0)])
            return head, rows
        if kind == "players":
            head = ["الطالب", "الفصل", "تاريخ التسجيل"]
            rows = [[r["name"], r["className"], time.strftime("%Y-%m-%d", time.localtime(r["createdAt"] or 0))]
                    for r in self.admin_players()["players"]]
            return head, rows
        if kind == "questions":
            c = self.classes.get(class_id or "")
            if not c:
                raise GameError("الفصل غير موجود")
            head = ["السؤال", "الإجابة الصحيحة", "خيار خاطئ 1", "خيار خاطئ 2", "خيار خاطئ 3", "الزمن", "المجال"]
            rows = [[q["text"], q["correct"], *(q["wrong"] + ["", "", ""])[:3], q["time"], FIELDS.get(q.get("field"), "")]
                    for q in c["questions"]]
            return head, rows
        raise GameError("نوع غير معروف")

    # ----------------------------------------------------------------- views
    def player_view(self, pid: str) -> dict:
        p = self.players.get(pid)
        if not p:
            return {"phase": "loggedOut"}
        cid = p["classId"]
        me = {"id": pid, "name": p["name"], "classId": cid, "className": self.class_name(cid)}
        L = self.live
        if L and pid in L.roster:
            return {"me": me, "mode": "knockout", **self._ko_view(L, pid)}
        t = self.active_tournament()
        if t and t["type"] == "separate":
            rec = t["contests"].get(cid)
            if rec and rec["status"] != "idle" and pid in rec["players"]:
                return {"me": me, "mode": "contest", **self._contest_view(t, cid, rec, pid)}
            if rec and rec["status"] == "running":
                return {"me": me, "mode": None, "phase": "waiting",
                        "note": "بدأت مسابقة فصلك قبل دخولك. اطلب من المعلم السماح بالانضمام المتأخر."}
        if L and L.side_of_class(cid) and L.phase not in ("lobby", "done"):
            return {"me": me, "mode": None, "phase": "waiting",
                    "note": "بدأت مباراة فصلك قبل دخولك. اطلب من المعلم السماح بالانضمام المتأخر."}
        return {"me": me, "mode": None, "phase": "waiting"}

    def _rank(self, items: list[tuple[str, float]], pid: str) -> tuple[int, int]:
        mine = dict(items).get(pid, 0)
        return 1 + sum(1 for _, v in items if v > mine), len(items)

    def _ko_view(self, L: LiveMatch, pid: str) -> dict:
        e = L.roster[pid]
        side = e["side"]
        other = "B" if side == "A" else "A"
        m = self.matches.get(L.match_id) or {}
        head = {"match": {"round": m.get("roundName", ""), "mine": self.class_name(L.cls[side]),
                          "other": self.class_name(L.cls[other])},
                "qIndex": L.qi, "qTotal": L.n}
        if e["kicked"]:
            return {**head, "phase": "kicked"}
        now = time.monotonic()
        if L.phase == "lobby":
            mates = sum(1 for p, x in L.active() if x["side"] == side and self.online(p))
            rivals = sum(1 for p, x in L.active() if x["side"] == other and self.online(p))
            return {**head, "phase": "lobby", "mates": mates, "rivals": rivals}
        if L.phase == "countdown":
            return {**head, "phase": "countdown", "remainingMs": ms(L.phase_end - now)}
        if L.phase == "question":
            q = L.questions[side][L.qi]
            if pid in L.answers[L.qi]:
                return {**head, "phase": "answered", "remainingMs": ms(L.deadline[side] - now)}
            perm = L.perms.get(pid)
            if perm is None:
                perm = L.perms[pid] = shuffled(len(options_of(q)))
            opts = options_of(q)
            return {**head, "phase": "question", "question": {
                "text": q["text"], "options": [opts[i] for i in perm], "field": FIELDS.get(q.get("field"), ""),
                "durationMs": q["time"] * 1000, "remainingMs": ms(L.deadline[side] - now)}}
        ranking = [(p, x["points"]) for p, x in L.active()]
        rank, of = self._rank(ranking, pid)
        if L.phase == "reveal":
            q = L.questions[side][L.qi]
            opts = options_of(q)
            last = e["last"] or {"choice": None, "correct": False, "points": 0, "missed": True}
            return {**head, "phase": "reveal", "result": {
                "correct": bool(last.get("correct")), "missed": last.get("choice") is None,
                "points": last.get("points", 0), "streak": e["streak"], "total": e["points"],
                "rank": rank, "of": of, "correctText": q["correct"],
                "yourText": opts[last["choice"]] if last.get("choice") is not None else None},
                "scores": {"mine": self._side_score(L, side)["avg"], "other": self._side_score(L, other)["avg"]}}
        res = m.get("result") or {}
        winner = m.get("winner")
        outcome = "win" if winner == L.cls[side] else ("lose" if winner else "tie")
        return {**head, "phase": "done", "outcome": outcome, "total": e["points"], "correct": e["correct"],
                "questions": L.revealed, "rank": rank, "of": of, "best": e["best"],
                "scores": {"mine": (res.get(side) or {}).get("avg", 0), "other": (res.get(other) or {}).get("avg", 0)}}

    def _contest_view(self, t: dict, cid: str, rec: dict, pid: str) -> dict:
        pp = rec["players"][pid]
        n = len(rec["questions"])
        head = {"title": t["name"], "qTotal": n, "qIndex": pp["pos"], "points": pp["points"], "correct": pp["correct"]}
        if pp["kicked"]:
            return {**head, "phase": "kicked"}
        if rec["status"] == "lobby":
            mates = sum(1 for p, x in rec["players"].items() if not x["kicked"] and self.online(p))
            return {**head, "phase": "lobby", "mates": mates}
        if rec["status"] == "finished" or pp["finished"]:
            ranking = [(p, x["points"]) for p, x in rec["players"].items() if not x["kicked"]]
            rank, of = self._rank(ranking, pid)
            res = rec.get("result") if rec["status"] == "finished" else None
            return {**head, "phase": "finished", "avg5": round(pp["points"] / n, 2) if n else 0,
                    "rank": rank, "of": of, "classDone": rec["status"] == "finished",
                    "classAvg": res["avg"] if res else None, "time": pp["time"],
                    "review": self._contest_review(t, rec, pp)}
        rt = self.crt.get(f"{t['id']}:{cid}", {}).get(pid) or {}
        now = time.monotonic()
        if rt.get("sentAt") is not None:
            q = rec["questions"][rt["qidx"]]
            opts = options_of(q)
            return {**head, "phase": "question", "question": {
                "text": q["text"], "options": [opts[i] for i in rt["perm"]], "field": FIELDS.get(q.get("field"), ""),
                "durationMs": q["time"] * 1000, "remainingMs": ms(rt["deadline"] - now)}}
        if rt.get("last"):
            return {**head, "phase": "feedback", "last": rt["last"],
                    "nextMs": ms(rt["nextAt"] - now) if rt.get("nextAt") else None}
        return {**head, "phase": "loading"}

    def _contest_review(self, t: dict, rec: dict, pp: dict) -> list[dict] | None:
        """Correct answers of the student's mistakes, shown only when no contest is still running."""
        if rec["status"] != "finished" or not self.settings.get("reviewAfterContest", True):
            return None
        if any(r["status"] in ("lobby", "running") for r in t["contests"].values()):
            return None
        out = []
        for a in pp["answers"]:
            if a.get("ok") or not 0 <= a.get("q", -1) < len(rec["questions"]):
                continue
            q = rec["questions"][a["q"]]
            opts = options_of(q)
            c = a.get("c")
            out.append({"text": q["text"], "correct": q["correct"],
                        "yours": opts[c] if isinstance(c, int) and 0 <= c < len(opts) else None})
        return out[:60]

    def _match_public(self, m: dict) -> dict:
        res = m.get("result") or {}
        return {
            "id": m["id"], "round": m["round"], "roundName": m["roundName"], "status": m["status"],
            "bye": m["bye"], "winner": m.get("winner"), "nextMatchId": m.get("nextMatchId"),
            "A": {"id": m["classA"], "name": self.class_name(m["classA"]) if m["classA"] else None,
                  "avg": (res.get("A") or {}).get("avg")},
            "B": {"id": m["classB"], "name": self.class_name(m["classB"]) if m["classB"] else None,
                  "avg": (res.get("B") or {}).get("avg")},
            "decidedBy": res.get("decidedBy"), "tie": res.get("tie", False),
            "isLive": bool(self.live and self.live.match_id == m["id"]),
        }

    def _tournament_public(self, t: dict, admin: bool = False) -> dict:
        out = {"id": t["id"], "name": t["name"], "type": t["type"], "createdAt": t.get("createdAt"),
               "classIds": t["classIds"], "classNames": [self.class_name(c) for c in t["classIds"]]}
        if t["type"] == "knockout":
            out.update({
                "status": t.get("status"), "questionsPerMatch": t["questionsPerMatch"], "pairing": t.get("pairing"),
                "champion": {"id": t["championId"], "name": self.class_name(t["championId"])} if t.get("championId") else None,
                "rounds": [[self._match_public(self.matches[mid]) for mid in r if mid in self.matches] for r in t["rounds"]],
            })
        else:
            out["contests"] = self._contests_public(t, admin)
            out["status"] = "finished" if all(r["status"] == "finished" for r in out["contests"]) else "running"
        return out

    def _contests_public(self, t: dict, admin: bool = False) -> list[dict]:
        rows = []
        for cid in t["classIds"]:
            rec = t["contests"].get(cid) or idle_contest()
            c = self.classes.get(cid)
            sc = rec["result"] if rec["status"] == "finished" and rec.get("result") else contest_score(rec)
            row = {
                "classId": cid, "name": self.class_name(cid), "grade": c["grade"] if c else 0,
                "status": rec["status"], "interrupted": bool(rec.get("interrupted")),
                "questions": len(rec["questions"]) if rec["status"] != "idle" else (len(c["questions"]) if c else 0),
                "online": sum(1 for p in rec["players"] if self.online(p) and not rec["players"][p]["kicked"]),
                **{k: sc[k] for k in ("avg", "liveAvg", "accuracy", "avgTime", "players", "finished", "answered")},
            }
            total = sc["players"] * sc["questions"]
            row["progress"] = round(sc["answered"] / total, 3) if total else 0
            if admin:
                row["roster"] = sorted(({
                    "id": p, "name": x["name"], "pos": x["pos"], "points": x["points"], "correct": x["correct"],
                    "finished": x["finished"], "kicked": x["kicked"], "online": self.online(p), "leaves": x.get("leaves", 0),
                } for p, x in rec["players"].items()), key=lambda r: (r["kicked"], -r["points"]))
            rows.append(row)
        order = {"finished": 0, "running": 1, "lobby": 2, "idle": 3}

        def key(r):
            value = r["avg"] if r["status"] == "finished" else r["liveAvg"]
            t_ = r["avgTime"] if r["avgTime"] is not None else 1e9
            return (order[r["status"]], -value, -r["accuracy"], t_)

        rows.sort(key=key)
        rank = 0
        for r in rows:
            if r["status"] == "finished":
                rank += 1
                r["rank"] = rank
            else:
                r["rank"] = None
        return rows

    def _live_public(self, admin: bool) -> dict | None:
        L = self.live
        if not L:
            return None
        m = self.matches.get(L.match_id) or {}
        now = time.monotonic()
        sides = []
        for side in "AB":
            es = [(p, e) for p, e in L.active() if e["side"] == side]
            online = [(p, e) for p, e in es if self.online(p)]
            s = {"side": side, "classId": L.cls[side], "name": self.class_name(L.cls[side]),
                 "online": len(online), "players": len(es), **{
                     k: v for k, v in self._side_score(L, side).items() if k in ("avg", "accuracy", "avgTime")}}
            if L.phase == "lobby":
                s["names"] = [e["name"] for _, e in online][:80]
            if L.phase in ("question", "reveal") and 0 <= L.qi < len(L.questions[side]):
                q = L.questions[side][L.qi]
                opts = options_of(q)
                dp = L.display_perm[side] or list(range(len(opts)))
                ans = L.answers[L.qi] if L.qi < len(L.answers) else {}
                s["question"] = {
                    "text": q["text"], "options": [opts[i] for i in dp], "durationMs": q["time"] * 1000,
                    "remainingMs": ms(L.deadline[side] - now) if L.phase == "question" else 0,
                    "field": FIELDS.get(q.get("field"), ""),
                }
                s["answered"] = sum(1 for p, _ in es if p in ans)
                s["expected"] = max(len(online), s["answered"])
                if L.phase == "reveal" or admin:
                    counts = [0] * len(opts)
                    for p, _ in es:
                        a = ans.get(p)
                        if a and a["choice"] is not None:
                            counts[dp.index(a["choice"])] += 1
                    s["counts"] = counts
                    s["correctIndex"] = dp.index(0)
            if admin:
                ans = L.answers[L.qi] if 0 <= L.qi < len(L.answers) else {}
                s["roster"] = sorted(({
                    "id": p, "name": e["name"], "points": e["points"], "correct": e["correct"],
                    "online": self.online(p), "kicked": e["kicked"], "answeredNow": p in ans, "leaves": e.get("leaves", 0),
                } for p, e in L.roster.items() if e["side"] == side), key=lambda r: (r["kicked"], -r["points"]))
                c = self.classes.get(L.cls[side])
                s["bank"] = len(c["questions"]) if c else 0
            sides.append(s)
        out = {
            "matchId": L.match_id, "roundName": m.get("roundName", ""), "phase": L.phase,
            "qIndex": L.qi, "qTotal": L.n, "timeUp": L.time_up,
            "remainingMs": ms(L.phase_end - now) if L.phase == "countdown" else 0,
            "sides": sides, "top": self._top_players(L, 5),
        }
        if L.phase == "done":
            res = m.get("result") or {}
            out["result"] = {"winnerId": m.get("winner"), "winner": self.class_name(m["winner"]) if m.get("winner") else None,
                             "tie": m.get("winner") is None, "decidedBy": res.get("decidedBy"),
                             "early": res.get("early", False), "A": res.get("A"), "B": res.get("B"),
                             "questions": res.get("questions", L.revealed)}
            t = self.tournaments.get(L.tid)
            out["isFinal"] = not m.get("nextMatchId")
            out["champion"] = self.class_name(t["championId"]) if t and t.get("championId") and out["isFinal"] else None
        return out

    def _leaders(self, t: dict | None) -> dict:
        classes = []
        if t and t["type"] == "knockout":
            rows: dict[str, dict] = {cid: {"classId": cid, "name": self.class_name(cid), "played": 0, "wins": 0,
                                           "avgSum": 0.0, "out": False} for cid in t["classIds"]}
            for r in t["rounds"]:
                for mid in r:
                    m = self.matches.get(mid)
                    if not m or m["status"] != "finished" or m["bye"]:
                        continue
                    res = m.get("result") or {}
                    for side, key in (("A", "classA"), ("B", "classB")):
                        cid = m[key]
                        if cid not in rows:
                            continue
                        rows[cid]["played"] += 1
                        rows[cid]["avgSum"] += (res.get(side) or {}).get("avg") or 0
                        if m.get("winner") == cid:
                            rows[cid]["wins"] += 1
                        elif m.get("winner"):
                            rows[cid]["out"] = True
            for r in rows.values():
                r["avg"] = round(r.pop("avgSum") / r["played"], 1) if r["played"] else 0
            classes = sorted(rows.values(), key=lambda r: (r["out"], -r["wins"], -r["avg"]))
        return {"classes": classes, "students": self._top_students(10)}

    def _top_students(self, k: int) -> list[dict]:
        rows = sorted(self.stats["players"].values(),
                      key=lambda sp: (-sp.get("correct", 0), -sp.get("koPoints", 0), -sp.get("contestPoints", 0)))[:k]
        return [{"name": sp.get("name", "—"), "className": self.class_name(sp.get("classId")),
                 "correct": sp.get("correct", 0), "answered": sp.get("answered", 0),
                 "koPoints": sp.get("koPoints", 0), "contestPoints": sp.get("contestPoints", 0)} for sp in rows]

    def display_state(self) -> dict:
        t = self.active_tournament()
        return {
            "mode": self.settings.get("displayMode", "auto"),
            "tournament": self._tournament_public(t) if t else None,
            "live": self._live_public(admin=False),
            "leaders": self._leaders(t),
            "onlineTotal": len(self.player_sids),
            "site": {k: v for k, v in (self.settings.get("site") or DEFAULT_SITE).items() if k != "goals"},
            "message": self.settings.get("displayMessage", ""),
        }

    def admin_data(self, password_info: dict) -> dict:
        classes = sorted(self.classes.values(), key=lambda c: (c["grade"], c["section"]))
        return {
            "grades": GRADES,
            "classes": classes,
            "tournaments": sorted((self._tournament_public(t, admin=True) for t in self.tournaments.values()),
                                  key=lambda t: -(t.get("createdAt") or 0)),
            "settings": dict(self.settings),
            "stats": self.stats_summary(),
            "password": password_info,
            "storage": {"temporary": self.store.temporary},
            "limits": {"minTime": MIN_TIME, "maxTime": MAX_TIME, "defaultTime": DEFAULT_TIME,
                       "maxQuestions": MAX_QUESTIONS, "maxWrong": MAX_WRONG},
            "fields": FIELDS,
            "defaultSite": DEFAULT_SITE,
        }

    def admin_live(self) -> dict:
        t = self.active_tournament()
        return {
            "live": self._live_public(admin=True),
            "liveQuestions": self._live_questions_admin(),
            "tournament": self._tournament_public(t, admin=True) if t else None,
            "online": self.online_by_class(),
            "onlineTotal": len(self.player_sids),
            "onlineIds": list(self.player_sids),
            "displays": sum(1 for i in self.sids.values() if i.get("role") == "display"),
        }

    def _live_questions_admin(self) -> dict | None:
        L = self.live
        if not L or L.qi < 0 or L.phase in ("lobby",):
            return None
        out = {}
        for side in "AB":
            if L.qi < len(L.questions[side]):
                q = L.questions[side][L.qi]
                out[side] = {"text": q["text"], "correct": q["correct"], "time": q["time"],
                             "field": FIELDS.get(q.get("field"), "")}
        return out

    # ----------------------------------------------------------------- drain
    def drain(self, force: bool = False) -> list[tuple[str, object, str]]:
        out, self.outbox = self.outbox, []
        for pid in self.dirty_players:
            sids = self.player_sids.get(pid)
            if not sids:
                continue
            view = self.player_view(pid)
            for sid in sids:
                out.append(("player:state", view, sid))
        self.dirty_players = set()
        now = time.monotonic()
        if self.dirty_display and (force or now - self._last_display >= 0.15):
            self.dirty_display = False
            self._last_display = now
            if self.has_role("display"):
                out.append(("display:state", self.display_state(), "display"))
        admins = any(i.get("admin") for i in self.sids.values())
        if self.dirty_admin_data:
            self.dirty_admin_data = False
            if admins:
                pw, src = current_password()
                out.append(("admin:data", self.admin_data({"source": src, "isDefault": pw == DEFAULT_PASSWORD}), "admin"))
        if self.dirty_admin_live and (force or now - self._last_admin_live >= 0.2):
            self.dirty_admin_live = False
            self._last_admin_live = now
            if admins:
                out.append(("admin:live", self.admin_live(), "admin"))
        if self.dirty_admin_players and (force or now - self._last_admin_players >= 1.0):
            self.dirty_admin_players = False
            self._last_admin_players = now
            if admins:
                out.append(("admin:players", self.admin_players(), "admin"))
        return out

