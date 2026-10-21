"""Unit tests for the competition engine (no network).  Run:  pytest -q"""

import json
import time

import pytest

from app.game import COUNTDOWN, DEFAULT_SITE, normalize_db
from app.util import GameError
from tests.helpers import add_classes, correct_choice, join, play_knockout_match, start_question

RIGHT = lambda g, pid: correct_choice(g, pid)             # noqa: E731
WRONG = lambda g, pid: (correct_choice(g, pid) + 1) % 4   # noqa: E731


# ------------------------------------------------------------------ brackets
@pytest.mark.parametrize("n", list(range(2, 14)) + [16, 17, 33])
def test_bracket_shapes(game, n):
    ids = add_classes(game, 10, min(n, 30))
    if n > 30:
        ids += add_classes(game, 11, n - 30)
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


def test_knockout_needs_two_classes(game):
    (a,) = add_classes(game, 9, 1)
    with pytest.raises(GameError):
        game.tournament_create({"type": "knockout", "classIds": [a]})


# ------------------------------------------------------------------ scoring
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
    start_question(game)
    v1 = game.player_view(p1)
    assert v1["phase"] == "question"
    assert "صحيح" in v1["question"]["options"]
    assert "correct" not in json.dumps(v1["question"])  # the correct answer is never marked
    assert "correctIndex" not in json.dumps(game.display_state(), ensure_ascii=False)

    game.live.q_start = time.monotonic() - 10  # answered after 10 of 20 seconds
    game.player_answer("s1", {"q": 0, "choice": correct_choice(game, p1)})
    with pytest.raises(GameError):  # one answer per question
        game.player_answer("s1", {"q": 0, "choice": 0})
    game.player_answer("s2", {"q": 0, "choice": (correct_choice(game, p2) + 1) % 4})
    assert game.live.phase == "reveal"  # everyone answered
    r1 = game.player_view(p1)["result"]
    assert r1["correct"] and 740 <= r1["points"] <= 750  # 1000 × (1 − 0.5/2) = 750
    assert game.player_view(p2)["result"]["points"] == 0

    game.match_next()
    start_question(game)
    game.live.q_start = time.monotonic()
    game.player_answer("s1", {"q": 1, "choice": correct_choice(game, p1)})
    game.player_answer("s2", {"q": 1, "choice": correct_choice(game, p2)})
    assert game.player_view(p1)["result"]["streak"] == 2
    assert game.player_view(p1)["result"]["points"] >= 1090  # ~1000 + streak bonus 100
    game.match_next()
    m = game.matches[mid]
    assert m["status"] == "finished" and m["winner"] == a
    assert game.player_view(p1)["outcome"] == "win"
    assert next(iter(game.tournaments.values()))["championId"] == a  # two classes → that was the final


def test_streak_bonus_caps_at_300(game):
    a, b = add_classes(game, 10, 2, questions=6)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 6})
    join(game, "s1", a, "أحمد سالم")
    join(game, "s2", b, "خالد علي")
    m = play_knockout_match(game, next(iter(game.matches)), {"s1": RIGHT, "s2": WRONG})
    # streak bonuses 0,100,200,300,300,300 = 1200 on top of the speed points
    total = m["result"]["A"]["total"]
    assert 6 * 990 + 1200 <= total <= 6 * 1000 + 1200


def test_class_score_is_an_average(game):
    a, b = add_classes(game, 10, 2, questions=2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 1})
    for i in range(3):
        join(game, f"a{i}", a, f"طالب أ {i}")
    join(game, "b0", b, "طالب ب")
    picks = {"a0": RIGHT, "a1": WRONG, "a2": WRONG, "b0": RIGHT}
    m = play_knockout_match(game, next(iter(game.matches)), picks)
    assert m["result"]["A"]["players"] == 3
    assert m["result"]["A"]["avg"] == pytest.approx(m["result"]["A"]["total"] / 3, abs=0.01)
    assert m["winner"] == b  # one excellent student beats a class of three with one right answer


def test_tie_needs_manual_winner(game):
    a, b = add_classes(game, 10, 2, questions=2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 1})
    mid = next(iter(game.matches))
    game.match_open({"matchId": mid})
    game.match_start()
    game.match_close()  # nobody joined → tie
    assert game.matches[mid]["winner"] is None and game.matches[mid]["result"]["tie"]
    game.match_set_winner({"matchId": mid, "classId": b})
    assert game.matches[mid]["winner"] == b


def test_forfeit_when_one_class_absent(game):
    a, b = add_classes(game, 10, 2, questions=2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 1})
    join(game, "s1", a, "أحمد سالم")
    m = play_knockout_match(game, next(iter(game.matches)), {"s1": WRONG})
    assert m["winner"] == a and m["result"]["decidedBy"] == "forfeit"


def test_late_answer_rejected(game):
    a, b = add_classes(game, 10, 2, questions=2, time_limit=5)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 1})
    join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    start_question(game)
    game.live.deadline = {"A": time.monotonic() - 5, "B": time.monotonic() - 5}
    with pytest.raises(GameError):
        game.player_answer("s1", {"q": 0, "choice": 0})


def test_answer_for_wrong_question_index_rejected(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    start_question(game)
    with pytest.raises(GameError):
        game.player_answer("s1", {"q": 1, "choice": 0})
    with pytest.raises(GameError):
        game.player_answer("s1", {"q": 0, "choice": 9})


def test_options_are_shuffled_per_student(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 1})
    pids = [join(game, f"s{i}", a, f"طالب رقم {i}") for i in range(12)]
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    start_question(game)
    orders = {tuple(game.player_view(p)["question"]["options"]) for p in pids}
    assert len(orders) > 1


# ------------------------------------------------------------- join & leave
def test_late_join_blocked_by_default_and_allowed_by_setting(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    late = join(game, "s9", a, "متأخر جدا")
    assert late not in game.live.roster
    assert game.player_view(late)["phase"] == "waiting"
    game.settings_update({"allowLateJoin": True})
    late2 = join(game, "s10", a, "متأخر آخر")
    assert late2 in game.live.roster


def test_leave_blocked_during_match(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    with pytest.raises(GameError):
        game.player_leave("s1")


def test_kicked_student_does_not_count(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 1})
    join(game, "s1", a, "أحمد سالم")
    bad = join(game, "s2", a, "مخرب")
    game.match_open({"matchId": next(iter(game.matches))})
    game.kick({"playerId": bad})
    assert game.player_view(bad)["phase"] == "kicked"
    game.match_start()
    assert game.live.roster[bad]["kicked"]
    start_question(game)
    with pytest.raises(GameError):
        game.player_answer("s2", {"q": 0, "choice": 0})


def test_token_spoofing_rejected(game):
    (a,) = add_classes(game, 12, 1)
    pid = join(game, "s1", a, "علي حسن")
    game.connect("s2", "x")
    with pytest.raises(GameError) as e:
        game.player_join("s2", {"playerId": pid, "token": "x" * 32})
    assert e.value.code == "auth"


def test_leaving_the_page_is_reported_once_per_question(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    pid = join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    game.player_blur("s1")  # during the countdown: not counted
    start_question(game)
    game.player_blur("s1")
    game.player_blur("s1")
    assert game.live.roster[pid]["leaves"] == 1
    assert game.admin_live()["live"]["sides"][0]["roster"][0]["leaves"] == 1


# ---------------------------------------------------------- separate contests
def test_separate_contest_flow_ranking_and_review(game):
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
            assert v["phase"] == "question" and v["question"]["field"] in ("النحو والصرف", "الأدب")
            assert "correct" not in json.dumps(v["question"])
            idx = v["question"]["options"].index("صحيح")
            game.player_answer(sid, {"q": v["qIndex"], "choice": idx if cid == a else (idx + 1) % 4})
            fb = game.player_view(pid)
            assert fb["phase"] in ("feedback", "finished")
            assert "صحيح" not in json.dumps(fb.get("last"), ensure_ascii=False)  # feedback never names the answer
        game.tick(time.monotonic() + 5)  # skip the feedback pause
    assert t["contests"][a]["status"] == "finished"
    assert t["contests"][a]["result"]["avg"] > 4.9
    assert t["contests"][b]["result"]["avg"] == 0
    ranking = game._contests_public(t)
    assert ranking[0]["classId"] == a and ranking[0]["rank"] == 1
    review = game.player_view(p2)["review"]
    assert len(review) == 3 and all(r["correct"] == "صحيح" for r in review)
    assert game.player_view(p1)["review"] == []
    game.contest_reset({"classId": b})
    assert t["contests"][b]["status"] == "idle"


def test_review_hidden_while_another_contest_runs(game):
    a, b = add_classes(game, 11, 2, questions=1)
    game.tournament_create({"type": "separate", "classIds": [a, b]})
    p1 = join(game, "s1", a, "سالم محمد")
    join(game, "s2", b, "يوسف أحمد")
    game.contest_open({"classIds": [a, b]})
    game.contest_start({})
    game.player_answer("s1", {"q": 0, "choice": 0})
    assert game.player_view(p1)["review"] is None  # class b is still answering
    game.settings_update({"reviewAfterContest": False})
    game.contest_finish({"classIds": [b]})
    assert game.player_view(p1)["review"] is None  # disabled by the teacher


def test_contest_timeout_counts_as_wrong(game):
    (a,) = add_classes(game, 9, 1, questions=2, time_limit=5)
    game.tournament_create({"type": "separate", "classIds": [a]})
    pid = join(game, "s1", a, "سالم محمد")
    game.contest_open({"classIds": [a]})
    game.contest_start({})
    game.tick(time.monotonic() + 10)
    v = game.player_view(pid)
    assert v["phase"] == "feedback" and v["last"]["timeout"]


# --------------------------------------------------------- teacher editing
def test_class_rename_and_uniqueness(game):
    a, b = add_classes(game, 10, 2)
    game.class_update({"classId": a, "grade": 11, "section": 4})
    assert game.classes[a]["name"] == "11/4"
    with pytest.raises(GameError):
        game.class_update({"classId": b, "grade": 11, "section": 4})
    with pytest.raises(GameError):
        game.class_update({"classId": b, "grade": 8, "section": 1})


def test_question_fields_are_validated(game):
    (a,) = add_classes(game, 9, 1, questions=0)
    game.class_save_questions({"classId": a, "questions": [
        {"text": "س", "correct": "أ", "wrong": ["ب"], "field": "balagha"},
        {"text": "س2", "correct": "أ", "wrong": ["ب"], "field": "<script>"}]})
    assert [q["field"] for q in game.classes[a]["questions"]] == ["balagha", ""]


def test_admin_student_management(game):
    a, b = add_classes(game, 10, 2)
    pid = join(game, "s1", a, "احمد")
    game.admin_player_rename({"playerId": pid, "name": "أحمد سالم"})
    assert game.players[pid]["name"] == "أحمد سالم"
    assert any(e == ("notice", {"text": "عدّل المعلم اسمك إلى «أحمد سالم»", "kind": "info"}, "s1") for e in game.outbox)
    game.admin_player_move({"playerId": pid, "classId": b})
    assert game.players[pid]["classId"] == b
    assert game.admin_players()["players"][0]["className"] == "10/2"
    game.admin_player_delete({"playerIds": [pid]})
    assert pid not in game.players
    assert ("player:loggedOut", {}, "s1") in game.outbox


def test_cannot_move_student_during_match(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 1})
    pid = join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    with pytest.raises(GameError):
        game.admin_player_move({"playerId": pid, "classId": b})
    with pytest.raises(GameError):
        game.admin_players_clear({})


def test_clear_offline_students(game):
    (a,) = add_classes(game, 9, 1)
    keep = join(game, "s1", a, "متصل الآن")
    gone = join(game, "s2", a, "غير متصل")
    game.disconnect("s2")
    assert game.admin_players_clear({"offlineOnly": True})["count"] == 1
    assert keep in game.players and gone not in game.players


def test_tournament_edit_and_redraw(game):
    ids = add_classes(game, 10, 4)
    game.tournament_create({"type": "knockout", "classIds": ids[:3]})
    t = next(iter(game.tournaments.values()))
    game.tournament_update({"tournamentId": t["id"], "name": "الدوري الكبير", "classIds": ids, "questionsPerMatch": 4})
    assert t["name"] == "الدوري الكبير" and t["questionsPerMatch"] == 4
    assert len(t["rounds"][0]) == 2 and not any(game.matches[m]["bye"] for m in t["rounds"][0])
    game.tournament_update({"tournamentId": t["id"], "redraw": True, "pairing": "random"})
    assert t["pairing"] == "random"
    # once a match is played the bracket is locked
    m = game.matches[t["rounds"][0][0]]
    game.match_set_winner({"matchId": m["id"], "classId": m["classA"]})
    with pytest.raises(GameError):
        game.tournament_update({"tournamentId": t["id"], "redraw": True})


def test_match_set_teams(game):
    ids = add_classes(game, 10, 4)
    game.tournament_create({"type": "knockout", "classIds": ids})
    t = next(iter(game.tournaments.values()))
    m1, m2 = (game.matches[x] for x in t["rounds"][0])
    with pytest.raises(GameError):  # a class can't play twice in the same round
        game.match_set_teams({"matchId": m1["id"], "classA": ids[0], "classB": m2["classA"]})
    game.match_set_teams({"matchId": m1["id"], "classA": m1["classB"], "classB": m1["classA"]})
    with pytest.raises(GameError):
        game.match_set_teams({"matchId": m1["id"], "classA": ids[0], "classB": ids[0]})


def test_separate_tournament_class_list_edit(game):
    a, b, c = add_classes(game, 9, 3)
    game.tournament_create({"type": "separate", "classIds": [a, b]})
    t = next(iter(game.tournaments.values()))
    game.contest_open({"classIds": [a]})
    with pytest.raises(GameError):  # a's contest is open
        game.tournament_update({"tournamentId": t["id"], "classIds": [b, c]})
    game.tournament_update({"tournamentId": t["id"], "classIds": [a, c]})
    assert t["classIds"] == [a, c] and b not in t["contests"] and t["contests"][c]["status"] == "idle"


def test_manual_reveal_when_auto_reveal_is_off(game):
    a, b = add_classes(game, 10, 2, time_limit=5)
    game.settings_update({"autoReveal": False, "countdown": 5})
    assert game.countdown_seconds() == 5
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 1})
    p1 = join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    start_question(game, 5)
    game.player_answer("s1", {"q": 0, "choice": correct_choice(game, p1)})
    assert game.live.phase == "question"  # all answered but no auto reveal
    game.tick(time.monotonic() + 30)
    assert game.live.phase == "question" and game.live.time_up
    game.match_reveal()
    assert game.live.phase == "reveal"


def test_site_texts_and_display_message(game):
    game.site_update({"programName": "", "tagline": "شعار جديد", "goals": ["هدف أول", "  ", "هدف ثانٍ"],
                      "announcement": "تبدأ المسابقة‮ غدًا"})
    site = game.settings["site"]
    assert site["programName"] == DEFAULT_SITE["programName"]  # cannot be emptied
    assert site["tagline"] == "شعار جديد" and site["goals"] == ["هدف أول", "هدف ثانٍ"]
    assert "‮" not in site["announcement"]
    assert game.public_site()["site"]["tagline"] == "شعار جديد"
    game.display_message({"text": "استراحة خمس دقائق", "show": True})
    assert game.display_state()["mode"] == "message" and game.display_state()["message"] == "استراحة خمس دقائق"
    game.display_message({"show": False})
    assert game.settings["displayMode"] == "auto"
    with pytest.raises(GameError):
        game.display_message({"text": "  ", "show": True})


def test_public_site_never_contains_answers(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    join(game, "s1", a, "أحمد سالم")
    game.match_open({"matchId": next(iter(game.matches))})
    game.match_start()
    start_question(game)
    blob = json.dumps(game.public_site(), ensure_ascii=False)
    assert "صحيح" not in blob and "خطأ 1" not in blob
    assert game.public_site()["live"]["kind"] == "knockout"


# ------------------------------------------------------------ stats & export
def test_stats_by_field_and_csv(game):
    a, b = add_classes(game, 10, 2, questions=2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    join(game, "s1", a, "أحمد سالم")
    join(game, "s2", b, "=HYPERLINK(1)")
    play_knockout_match(game, next(iter(game.matches)), {"s1": RIGHT, "s2": WRONG})
    by = {f["key"]: f for f in game.stats_summary()["byField"]}
    assert by["nahw"]["answered"] + by["adab"]["answered"] == 4
    head, rows = game.csv_table("students")
    assert head[0] == "الطالب" and {r[0] for r in rows} == {"أحمد سالم", "=HYPERLINK(1)"}
    assert game.csv_table("history")[1][0][2] == "خروج المغلوب"
    assert len(game.csv_table("questions", a)[1]) == 2
    with pytest.raises(GameError):
        game.csv_table("nope")


# ------------------------------------------------------------- persistence
def test_input_is_sanitised(game):
    (a,) = add_classes(game, 12, 1)
    game.connect("s1", "x")
    res = game.player_join("s1", {"classId": a, "name": "<b>علي</b>‮​   حسن"})
    assert res["player"]["name"] == "<b>علي</b> حسن"  # text only; pages render it with textContent
    with pytest.raises(GameError):
        game.class_save_questions({"classId": a, "questions": [{"text": "س", "correct": "أ", "wrong": []}]})
    with pytest.raises(GameError):
        game.player_join("s1", {"classId": a, "name": "12"})


def test_restart_recovers_interrupted_state(game):
    a, b = add_classes(game, 10, 2)
    game.tournament_create({"type": "knockout", "classIds": [a, b], "questionsPerMatch": 2})
    join(game, "s1", a, "أحمد سالم")
    mid = next(iter(game.matches))
    game.match_open({"matchId": mid})
    game.match_start()
    db = normalize_db(json.loads(json.dumps(game.db)))
    assert db["matches"][mid]["status"] == "pending"


def test_import_rejects_garbage_and_keeps_valid_data(game):
    with pytest.raises(GameError):
        game.data_import({"hello": 1})
    raw = {"classes": {"c1": {"grade": 10, "section": 1, "questions": [
        {"text": "س", "correct": "أ", "wrong": ["ب"]}, {"text": "", "correct": "x", "wrong": ["y"]}]},
        "c2": {"grade": 99, "section": 1}},
        "settings": {"countdown": 99, "site": {"programName": "اسم"}}}
    game.data_import(raw)
    assert list(game.classes) == ["c1"] and len(game.classes["c1"]["questions"]) == 1
    assert game.settings["countdown"] == 10 and game.settings["site"]["programName"] == "اسم"


def test_data_reset_requires_confirmation(game):
    add_classes(game, 9, 1)
    with pytest.raises(GameError):
        game.data_reset({})
    game.data_reset({"confirm": "RESET"})
    assert not game.classes
