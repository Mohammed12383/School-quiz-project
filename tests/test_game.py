"""Unit tests for the competition engine.  Run:  pip install pytest && pytest -q"""

import json
import time

import pytest

from app.game import COUNTDOWN, Game, normalize_db
from app.store import Store
from app.util import GameError


@pytest.fixture
def game(tmp_path):
    return Game(Store(tmp_path / "db.json"))


def add_classes(game, grade, sections, questions=6, time_limit=20):
    game.class_create({"grade": grade, "from": 1, "to": sections})
    ids = [c["id"] for c in game.classes.values() if c["grade"] == grade]
    for cid in ids:
        game.class_save_questions({"classId": cid, "questions": [
            {"text": f"سؤال {i}", "correct": "صحيح", "wrong": ["خطأ 1", "خطأ 2", "خطأ 3"], "time": time_limit}
            for i in range(questions)]})
    return sorted(ids, key=lambda c: game.classes[c]["section"])


def join(game, sid, cid, name):
    game.connect(sid, "127.0.0.1")
    return game.player_join(sid, {"classId": cid, "name": name})["player"]["id"]


def correct_choice(game, pid):
    """The index of the correct option *as shown to this player* (server-side knowledge)."""
    L = game.live
    e = L.roster[pid]
    q = L.questions[e["side"]][L.qi]
    shown = game.player_view(pid)["question"]["options"]
    return shown.index(q["correct"])


@pytest.mark.parametrize("n", range(2, 13))
def test_bracket_shapes(game, n):
    ids = add_classes(game, 10, n)
    game.tournament_create({"type": "knockout", "classIds": ids, "questionsPerMatch": 3})
    t = next(iter(game.tournaments.values()))
    size = 1
    while size < n:
        size *= 2
    first = [game.matches[m] for m in t["rounds"][0]]
    assert len(first) == size // 2
    assert sum(1 for m in first if m["bye"]) == size - n
    placed = [c for m in first for c in (m["classA"], m["classB"]) if c]
    assert sorted(placed) == sorted(ids)  # every class exactly once
    assert len(t["rounds"][-1]) == 1 and game.matches[t["rounds"][-1][0]]["roundName"] == "النهائي"


def test_grade_pairing_keeps_grades_together(game):
    a = add_classes(game, 10, 2)
    b = add_classes(game, 11, 2)
    game.tournament_create({"type": "knockout", "classIds": a + b, "pairing": "grade"})
    t = next(iter(game.tournaments.values()))
    for mid in t["rounds"][0]:
        m = game.matches[mid]
        assert game.classes[m["classA"]]["grade"] == game.classes[m["classB"]]["grade"]


def test_knockout_scoring_and_no_leak(game):
    a, b = add_classes(game, 10, 2, questions=3)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    p1 = join(game, "s1", a, "أحمد سالم")
    p2 = join(game, "s2", b, "خالد علي")
    mid = next(iter(game.matches))
    game.match_open({"matchId": mid})
    game.match_start()
    view = game.player_view(p1)
    assert view["phase"] == "countdown" and "question" not in view
    game.tick(time.monotonic() + COUNTDOWN + 0.01)
    v1 = game.player_view(p1)
    assert v1["phase"] == "question"
    assert "صحيح" in v1["question"]["options"]
    assert "correct" not in json.dumps(v1["question"])  # correct answer is never marked
    display = json.dumps(game.display_state(), ensure_ascii=False)
    assert "correctIndex" not in display

    game.live.q_start = time.monotonic() - 10  # answered after 10 of 20 seconds
    game.player_answer("s1", {"q": 0, "choice": correct_choice(game, p1)})
    with pytest.raises(GameError):  # one answer per question
        game.player_answer("s1", {"q": 0, "choice": 0})
    wrong = (correct_choice(game, p2) + 1) % 4
    game.player_answer("s2", {"q": 0, "choice": wrong})
    assert game.live.phase == "reveal"  # everyone answered
    r1 = game.player_view(p1)["result"]
    assert r1["correct"] and 740 <= r1["points"] <= 750  # 1000 * (1 - 0.5/2) = 750
    assert game.player_view(p2)["result"]["points"] == 0

    game.match_next()
    game.tick(time.monotonic() + COUNTDOWN + 0.01)
    game.live.q_start = time.monotonic()
    game.player_answer("s1", {"q": 1, "choice": correct_choice(game, p1)})
    game.player_answer("s2", {"q": 1, "choice": correct_choice(game, p2)})
    assert game.player_view(p1)["result"]["streak"] == 2
    assert game.player_view(p1)["result"]["points"] >= 1090  # ~1000 + streak bonus 100
    game.match_next()
    m = game.matches[mid]
    assert m["status"] == "finished" and m["winner"] == a
    assert game.player_view(p1)["outcome"] == "win"
    t = next(iter(game.tournaments.values()))
    assert t["championId"] == a  # two classes: the match was the final


def test_late_answer_rejected(game):
    a, b = add_classes(game, 10, 2, questions=2, time_limit=5)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 1})
    join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    game.tick(time.monotonic() + COUNTDOWN + 0.01)
    game.live.deadline = {"A": time.monotonic() - 5, "B": time.monotonic() - 5}
    with pytest.raises(GameError):
        game.player_answer("s1", {"q": 0, "choice": 0})


def test_late_join_blocked_by_default(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    late = join(game, "s9", a, "متأخر جدا")
    assert late not in game.live.roster
    assert game.player_view(late)["phase"] == "waiting"


def test_leave_blocked_during_match(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    with pytest.raises(GameError):
        game.player_leave("s1")


def test_separate_contest_flow(game):
    a, b = add_classes(game, 11, 2, questions=3)
    game.tournament_create({"type": "separate", "classIds": [a, b]})
    t = next(iter(game.tournaments.values()))
    p1 = join(game, "s1", a, "سالم محمد")
    p2 = join(game, "s2", b, "يوسف أحمد")
    game.contest_open({"classIds": [a, b]})
    game.contest_start({})
    for _ in range(3):
        for sid, pid, cid in (("s1", p1, a), ("s2", p2, b)):
            v = game.player_view(pid)
            assert v["phase"] == "question"
            assert "correct" not in json.dumps(v["question"])
            idx = v["question"]["options"].index("صحيح")
            game.player_answer(sid, {"q": v["qIndex"], "choice": idx if cid == a else (idx + 1) % 4})
            assert game.player_view(pid)["phase"] in ("feedback", "finished")
        game.tick(time.monotonic() + 5)  # skip the feedback pause
    assert t["contests"][a]["status"] == "finished"
    assert t["contests"][a]["result"]["avg"] > 4.9
    assert t["contests"][b]["result"]["avg"] == 0
    ranking = game._contests_public(t)
    assert ranking[0]["classId"] == a and ranking[0]["rank"] == 1
    game.contest_reset({"classId": b})
    assert t["contests"][b]["status"] == "idle"


def test_contest_timeout_counts_as_wrong(game):
    (a,) = add_classes(game, 9, 1, questions=2, time_limit=5)
    game.tournament_create({"type": "separate", "classIds": [a]})
    pid = join(game, "s1", a, "سالم محمد")
    game.contest_open({"classIds": [a]})
    game.contest_start({})
    game.tick(time.monotonic() + 10)
    v = game.player_view(pid)
    assert v["phase"] == "feedback" and v["last"]["timeout"]


def test_input_is_sanitised(game):
    (a,) = add_classes(game, 12, 1)
    game.connect("s1", "x")
    res = game.player_join("s1", {"classId": a, "name": "<b>علي</b>‮​   حسن"})
    assert res["player"]["name"] == "<b>علي</b> حسن"  # text only; rendered with textContent
    with pytest.raises(GameError):
        game.class_save_questions({"classId": a, "questions": [{"text": "س", "correct": "أ", "wrong": []}]})


def test_restart_recovers_interrupted_state(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    join(game, "s1", a, "أحمد سالم")
    mid = next(iter(game.matches))
    game.match_open({"matchId": mid})
    game.match_start()
    db = normalize_db(json.loads(json.dumps(game.db)))
    assert db["matches"][mid]["status"] == "pending"


def test_leaving_the_page_is_reported_once_per_question(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    pid = join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    game.player_blur("s1")  # during the countdown: not counted
    game.tick(time.monotonic() + COUNTDOWN + 0.01)
    game.player_blur("s1")
    game.player_blur("s1")
    assert game.live.roster[pid]["leaves"] == 1
    roster = game.admin_live()["live"]["sides"][0]["roster"]
    assert roster[0]["leaves"] == 1
