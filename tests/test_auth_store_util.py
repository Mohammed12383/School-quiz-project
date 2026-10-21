"""Tests for the admin password/login, the JSON store and input cleaning."""

import json

import pytest

from app import auth
from app.store import Store, resolve_data_dir
from app.util import GameError, clean_text, has_letters, to_int


# ------------------------------------------------------------------- auth
@pytest.fixture
def pwfile(tmp_path, monkeypatch):
    path = tmp_path / "admin_password.txt"
    monkeypatch.setattr(auth, "PASSWORD_FILE", path)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    return path


def test_password_sources(pwfile, monkeypatch):
    assert auth.current_password() == ("admin123", "default")
    pwfile.write_text("from-file\n", encoding="utf-8")
    assert auth.current_password() == ("from-file", "file")
    monkeypatch.setenv("ADMIN_PASSWORD", "from-env")
    assert auth.current_password() == ("from-env", "env")


def test_login_and_tokens(pwfile):
    a = auth.AdminAuth()
    with pytest.raises(GameError):
        a.login("wrong", None, "1.1.1.1")
    tok = a.login("admin123", None, "1.1.1.1")
    assert a.check_token(tok)
    assert a.login(None, tok, "1.1.1.1") == tok  # token login
    a.logout(tok)
    assert not a.check_token(tok)


def test_login_lockout_after_failures(pwfile):
    a = auth.AdminAuth()
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(GameError):
            a.login("nope", None, "9.9.9.9")
    with pytest.raises(GameError) as e:
        a.login("admin123", None, "9.9.9.9")  # even the right password is refused for a while
    assert "انتظر" in str(e.value)
    assert a.login("admin123", None, "8.8.8.8")  # other devices are not affected


def test_change_password(pwfile):
    a = auth.AdminAuth()
    keep = a.login("admin123", None, "ip")
    other = a.login("admin123", None, "ip2")
    with pytest.raises(GameError):
        a.change_password("bad", "new-password", keep)
    with pytest.raises(GameError):
        a.change_password("admin123", "123", keep)
    with pytest.raises(GameError):
        a.change_password("admin123", "has space", keep)
    a.change_password("admin123", "new-password", keep)
    assert pwfile.read_text(encoding="utf-8").strip() == "new-password"
    assert a.check_token(keep) and not a.check_token(other)  # other sessions signed out


def test_env_password_cannot_be_changed(pwfile, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "server-secret")
    with pytest.raises(GameError):
        auth.AdminAuth().change_password("server-secret", "another-one", None)


# ------------------------------------------------------------------- store
def test_store_saves_atomically_and_reloads(tmp_path):
    s = Store(tmp_path / "db.json")
    s.data["x"] = "مرحبا"
    s.mark_dirty()
    s.save_now()
    assert json.loads((tmp_path / "db.json").read_text(encoding="utf-8"))["x"] == "مرحبا"
    assert not (tmp_path / "db.tmp").exists()
    assert Store(tmp_path / "db.json").data["x"] == "مرحبا"


def test_store_recovers_from_corrupted_file(tmp_path):
    path = tmp_path / "db.json"
    s = Store(path)
    s.data["classes"] = {"a": 1}
    s.mark_dirty()
    s.save_now()
    Store(path)  # a normal start keeps a backup copy
    path.write_text("{ broken", encoding="utf-8")
    recovered = Store(path)
    assert recovered.data["classes"] == {"a": 1}
    assert list(tmp_path.glob("db.broken-*.json"))


def test_data_dir_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "custom"))
    assert resolve_data_dir(tmp_path) == tmp_path / "custom"
    monkeypatch.delenv("DATA_DIR")
    expected = {tmp_path / "data", __import__("pathlib").Path("/data")}
    assert resolve_data_dir(tmp_path) in expected


# ------------------------------------------------------------------- util
def test_clean_text():
    assert clean_text("  أحمد \t  علي  ", 30) == "أحمد علي"
    assert clean_text("abc‮def​", 30) == "abcdef"  # bidi override & zero-width removed
    assert clean_text("a\nb", 30) == "a b"
    assert clean_text("a\n\n\nb\nc\nd\ne", 50, multiline=True) == "a\n\nb\nc"
    assert clean_text(None, 5) == "" and clean_text(True, 5) == "" and clean_text(12, 5) == "12"
    assert len(clean_text("x" * 500, 30)) == 30


def test_to_int_and_letters():
    assert to_int("7", 0, 1, 10) == 7
    assert to_int("99", 0, 1, 10) == 10
    assert to_int("abc", 3, 1, 10) == 3
    assert to_int(True, 3, 1, 10) == 3
    assert has_letters("أح") and not has_letters("12") and not has_letters("أ")
