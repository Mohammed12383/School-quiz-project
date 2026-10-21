"""Admin password and login sessions."""

from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict
from pathlib import Path

from .util import GameError, new_token

APP_DIR = Path(__file__).resolve().parent
PASSWORD_FILE = APP_DIR / "admin_password.txt"
DEFAULT_PASSWORD = "admin123"

TOKEN_TTL = 12 * 3600
MAX_FAILURES = 8
FAILURE_WINDOW = 10 * 60


def current_password() -> tuple[str, str]:
    """Return (password, source) where source is env, file or default."""
    env = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env:
        return env, "env"
    try:
        text = PASSWORD_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text, "file"
    except OSError:
        pass
    return DEFAULT_PASSWORD, "default"


class AdminAuth:
    def __init__(self) -> None:
        self.tokens: dict[str, float] = {}
        self.failures: dict[str, list[float]] = defaultdict(list)

    def _recent_failures(self, ip: str, now: float) -> list[float]:
        fails = [t for t in self.failures[ip] if now - t < FAILURE_WINDOW]
        self.failures[ip] = fails
        return fails

    def login(self, password, token, ip: str) -> str:
        now = time.time()
        if isinstance(token, str) and token and self.check_token(token):
            return token
        if not isinstance(password, str) or not password:
            raise GameError("اكتب كلمة المرور", "auth")
        if len(self._recent_failures(ip, now)) >= MAX_FAILURES:
            raise GameError("محاولات كثيرة خاطئة. انتظر عشر دقائق ثم حاول مجددًا.")
        expected, _ = current_password()
        if not hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
            self.failures[ip].append(now)
            left = MAX_FAILURES - len(self.failures[ip])
            raise GameError(f"كلمة المرور غير صحيحة (المحاولات المتبقية: {max(left, 0)})", "auth")
        self.failures.pop(ip, None)
        self._purge(now)
        tok = new_token()
        self.tokens[tok] = now + TOKEN_TTL
        return tok

    def _purge(self, now: float) -> None:
        for tok, exp in list(self.tokens.items()):
            if exp < now:
                del self.tokens[tok]

    def check_token(self, token) -> bool:
        if not isinstance(token, str) or not token:
            return False
        exp = self.tokens.get(token)
        return bool(exp and exp > time.time())

    def logout(self, token) -> None:
        if isinstance(token, str):
            self.tokens.pop(token, None)

    def change_password(self, current, new, keep_token) -> None:
        expected, source = current_password()
        if source == "env":
            raise GameError("كلمة المرور محددة من متغير البيئة ADMIN_PASSWORD على الخادم، ولا يمكن تغييرها من هنا.")
        if not isinstance(current, str) or not hmac.compare_digest(
            current.encode("utf-8"), expected.encode("utf-8")
        ):
            raise GameError("كلمة المرور الحالية غير صحيحة")
        if not isinstance(new, str):
            raise GameError("كلمة المرور الجديدة غير صالحة")
        new = new.strip()
        if len(new) < 6 or len(new) > 64:
            raise GameError("يجب أن تتكون كلمة المرور الجديدة من 6 إلى 64 حرفًا")
        if any(ch.isspace() for ch in new):
            raise GameError("لا تستخدم مسافات في كلمة المرور")
        try:
            PASSWORD_FILE.write_text(new + "\n", encoding="utf-8")
        except OSError as exc:
            raise GameError(f"تعذّر حفظ كلمة المرور: {exc}") from exc
        # sign out every other session
        self.tokens = {t: e for t, e in self.tokens.items() if t == keep_token}
