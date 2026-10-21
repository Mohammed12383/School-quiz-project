"""Small helpers: input sanitising, ids, network addresses."""

from __future__ import annotations

import ipaddress
import re
import secrets
import socket
import unicodedata

# Control characters, zero-width characters and bidi overrides/isolates.
# Removing them stops invisible tricks in names and questions (e.g. text that
# renders reversed on the projector).
_INVISIBLE = re.compile(
    "[\u0000-\u0008\u000b-\u001f\u007f-\u009f​‎‏"
    "‪-‮⁦-⁩﻿]"
)
_SPACES = re.compile(r"[ \t  - 　]+")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


class GameError(Exception):
    """An expected error whose message (Arabic) is shown to the user."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def clean_text(value, max_len: int, *, multiline: bool = False) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    s = unicodedata.normalize("NFC", value[: max_len * 4])
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _INVISIBLE.sub("", s)
    if multiline:
        lines = [_SPACES.sub(" ", line).strip() for line in s.split("\n")]
        # keep at most 4 lines, drop empty runs
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        s = "\n".join(out[:4]).strip()
    else:
        s = _SPACES.sub(" ", s.replace("\n", " ")).strip()
    return s[:max_len].strip()


def has_letters(s: str, minimum: int = 2) -> bool:
    return len(_LETTER.findall(s)) >= minimum


def to_int(value, default: int, lo: int, hi: int) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, n))


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(5)


def new_token() -> str:
    return secrets.token_urlsafe(24)


def lan_ips() -> list[str]:
    """IPv4 addresses other devices on the same network can use to reach us."""
    found: set[str] = set()
    for target in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 1))
                found.add(s.getsockname()[0])
        except OSError:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass

    def usable(ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return not (a.is_loopback or a.is_link_local or a.is_unspecified)

    def rank(ip: str):
        if ip.startswith("192.168."):
            return (0, ip)
        if ip.startswith("10."):
            return (1, ip)
        if ipaddress.ip_address(ip).is_private:
            return (2, ip)
        return (3, ip)

    return sorted((ip for ip in found if usable(ip)), key=rank)
