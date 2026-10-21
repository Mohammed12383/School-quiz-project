"""ضفاف العربية — FastAPI + Socket.IO server.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import qrcode
import socketio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import DEFAULT_PASSWORD, AdminAuth, current_password
from .game import Game
from .store import Store, resolve_data_dir
from .util import GameError, as_dict, lan_ips

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("difaf")

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
PAGES_DIR = APP_DIR / "pages"
TICK = 0.1

store = Store(resolve_data_dir(BASE_DIR) / "db.json")
game = Game(store)
auth = AdminAuth()

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=None,  # same origin only
    ping_interval=15,
    ping_timeout=20,
    max_http_buffer_size=1_000_000,
)


# ---------------------------------------------------------------- utilities
class RateLimiter:
    """Token bucket per socket: protects the server from event floods."""

    def __init__(self, capacity: float = 40, refill: float = 15):
        self.capacity, self.refill = capacity, refill
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, sid: str) -> bool:
        now = time.monotonic()
        tokens, last = self.buckets.get(sid, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill)
        if tokens < 1:
            self.buckets[sid] = (tokens, now)
            return False
        self.buckets[sid] = (tokens - 1, now)
        return True

    def forget(self, sid: str) -> None:
        self.buckets.pop(sid, None)


limiter = RateLimiter()


# Views are computed and sent under one lock so that two concurrent flushes can
# never deliver an older snapshot after a newer one to the same screen.
_send_lock = asyncio.Lock()


async def flush(force: bool = False) -> None:
    async with _send_lock:
        for event, data, to in game.drain(force):
            await sio.emit(event, data, to=to)


def password_info() -> dict:
    pw, src = current_password()
    return {"source": src, "isDefault": pw == DEFAULT_PASSWORD}


def guarded(event: str, *, admin: bool = False):
    """Register a Socket.IO handler with rate limiting, auth and error handling."""

    def deco(fn):
        async def handler(sid, data=None):
            if not limiter.allow(sid):
                return {"ok": False, "error": "طلبات كثيرة متتالية، تمهّل قليلًا"}
            if admin and not game.is_admin(sid):
                return {"ok": False, "error": "انتهت جلسة الدخول، سجّل الدخول من جديد", "code": "auth"}
            try:
                result = fn(sid, data)
                if asyncio.iscoroutine(result):
                    result = await result
            except GameError as exc:
                result = {"ok": False, "error": str(exc), "code": exc.code}
            except Exception:
                log.exception("error in %s", event)
                result = {"ok": False, "error": "حدث خطأ غير متوقع في الخادم"}
            await flush()
            return result if isinstance(result, dict) else {"ok": True}

        sio.on(event, handler)
        return fn

    return deco


# ------------------------------------------------------------ socket events
@sio.event
async def connect(sid, environ, auth_data=None):
    game.connect(sid, environ.get("REMOTE_ADDR") or "?")


@sio.event
async def disconnect(sid, *args):
    game.disconnect(sid)
    limiter.forget(sid)
    await flush()


@guarded("player:join")
def on_player_join(sid, data):
    return game.player_join(sid, data)


@guarded("player:answer")
def on_player_answer(sid, data):
    return game.player_answer(sid, data)


@guarded("player:leave")
def on_player_leave(sid, data):
    return game.player_leave(sid)


@guarded("player:blur")
def on_player_blur(sid, data):
    return game.player_blur(sid)


@guarded("player:sync")
def on_player_sync(sid, data):
    return game.player_sync(sid)


@guarded("display:join")
async def on_display_join(sid, data):
    game.set_role(sid, "display")
    await sio.enter_room(sid, "display")
    async with _send_lock:
        await sio.emit("display:state", game.display_state(), to=sid)
    return {"ok": True}


@guarded("admin:login")
async def on_admin_login(sid, data):
    data = as_dict(data)
    ip = game.sids.get(sid, {}).get("ip", "?")
    token = auth.login(data.get("password"), data.get("token"), ip)
    game.set_role(sid, "admin", admin=True)
    game.sids[sid]["token"] = token
    await sio.enter_room(sid, "admin")
    async with _send_lock:
        await sio.emit("admin:data", game.admin_data(password_info()), to=sid)
        await sio.emit("admin:live", game.admin_live(), to=sid)
        await sio.emit("admin:players", game.admin_players(), to=sid)
    return {"ok": True, "token": token}


@guarded("admin:logout", admin=True)
async def on_admin_logout(sid, data):
    auth.logout(game.sids.get(sid, {}).get("token"))
    game.sids[sid]["admin"] = False
    await sio.leave_room(sid, "admin")
    return {"ok": True}


@guarded("admin:password", admin=True)
async def on_admin_password(sid, data):
    data = as_dict(data)
    keep = game.sids.get(sid, {}).get("token")
    auth.change_password(data.get("current"), data.get("new"), keep)
    for other in game.revoke_admins(keep_sid=sid):
        await sio.leave_room(other, "admin")
        await sio.emit("admin:loggedOut", {"reason": "تم تغيير كلمة المرور"}, to=other)
    game.dirty_admin_data = True
    return {"ok": True}


ADMIN_ACTIONS = {
    "class:create": game.class_create,
    "class:delete": game.class_delete,
    "class:update": game.class_update,
    "class:saveQuestions": game.class_save_questions,
    "class:copyQuestions": game.class_copy_questions,
    "class:addSamples": game.class_add_samples,
    "tournament:create": game.tournament_create,
    "tournament:activate": game.tournament_activate,
    "tournament:delete": game.tournament_delete,
    "tournament:setQuestions": game.tournament_set_questions,
    "tournament:update": game.tournament_update,
    "match:setTeams": game.match_set_teams,
    "match:open": game.match_open,
    "match:start": game.match_start,
    "match:reveal": game.match_reveal,
    "match:next": game.match_next,
    "match:cancel": game.match_cancel,
    "match:close": game.match_close,
    "match:setWinner": game.match_set_winner,
    "match:reset": game.match_reset,
    "match:kick": game.kick,
    "classCompetition:open": game.contest_open,
    "classCompetition:start": game.contest_start,
    "classCompetition:finish": game.contest_finish,
    "classCompetition:reset": game.contest_reset,
    "display:set": game.display_set,
    "display:opening": game.display_opening,
    "settings:lateJoin": game.set_late_join,
    "settings:update": game.settings_update,
    "settings:site": game.site_update,
    "display:message": game.display_message,
    "admin:playerRename": game.admin_player_rename,
    "admin:playerMove": game.admin_player_move,
    "admin:playerDelete": game.admin_player_delete,
    "admin:playersClear": game.admin_players_clear,
    "stats:reset": game.stats_reset,
    "data:reset": game.data_reset,
}


def _register_admin_action(event, method):
    guarded(event, admin=True)(lambda sid, data: method(data))


for _event, _method in ADMIN_ACTIONS.items():
    _register_admin_action(_event, _method)


async def ticker() -> None:
    while True:
        await asyncio.sleep(TICK)
        try:
            game.tick()
            await flush()
        except Exception:
            log.exception("ticker error")


# ------------------------------------------------------------------- HTTP
def _port() -> str:
    args = sys.argv
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--port="):
            return a.split("=", 1)[1]
    return os.environ.get("PORT", "8000")


def print_banner() -> None:
    port = _port()
    lines = ["", "=" * 60, "  Difaf Al-Arabiya is running", "=" * 60,
             f"  On this computer : http://localhost:{port}/"]
    for ip in lan_ips():
        lines.append(f"  Students (Wi-Fi) : http://{ip}:{port}/play")
    lines += [f"  Display screen   : http://localhost:{port}/display",
              f"  Admin panel      : http://localhost:{port}/admin",
              f"  Data file        : {store.path}", "=" * 60, ""]
    print("\n".join(lines), flush=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(ticker())
    print_banner()
    try:
        yield
    finally:
        task.cancel()
        store.save_now()


api = FastAPI(title="Difaf Al-Arabiya", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self' ws: wss:; font-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@api.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = CSP
    path = request.url.path
    if path.startswith("/static/fonts/") or path.startswith("/static/vendor/"):
        response.headers["Cache-Control"] = "public, max-age=604800"
    elif path.startswith("/static/") or not path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    else:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def page(name: str):
    path = PAGES_DIR / f"{name}.html"

    async def handler():
        return FileResponse(path, media_type="text/html; charset=utf-8")

    return handler


for _route, _name in (("/", "index"), ("/about", "about"), ("/play", "play"),
                      ("/display", "display"), ("/admin", "admin")):
    api.add_api_route(_route, page(_name), methods=["GET", "HEAD"], include_in_schema=False)


@api.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "img" / "favicon.png", media_type="image/png")


@api.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    return {"status": "ok"}


@api.get("/api/info")
async def info(request: Request):
    port = request.url.port or int(os.environ.get("PORT", "8000"))
    ips = lan_ips()
    return {"lanUrls": [f"http://{ip}:{port}" for ip in ips], "port": port}


@api.get("/api/classes")
async def classes():
    return {"classes": game.public_classes()}


@api.get("/api/site")
async def site():
    """Public texts and highlights for the main page (never contains answers)."""
    return game.public_site()


@lru_cache(maxsize=64)
def qr_svg(text: str) -> str:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    parts = []
    for y, row in enumerate(matrix):
        x = 0
        while x < size:
            if row[x]:
                start = x
                while x < size and row[x]:
                    x += 1
                parts.append(f"M{start} {y}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'shape-rendering="crispEdges"><rect width="{size}" height="{size}" fill="#ffffff"/>'
            f'<path fill="#192552" d="{"".join(parts)}"/></svg>')


@api.get("/api/qr")
async def qr(u: str = ""):
    if not (u.startswith("http://") or u.startswith("https://")) or len(u) > 300:
        return JSONResponse({"error": "invalid url"}, status_code=400)
    return Response(qr_svg(u), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


def _admin_token_ok(request: Request) -> bool:
    return auth.check_token(request.headers.get("x-admin-token", ""))


@api.get("/api/admin/export")
async def export_data(request: Request):
    if not _admin_token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = json.dumps(game.export(), ensure_ascii=False, indent=1)
    stamp = time.strftime("%Y-%m-%d_%H-%M")
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="difaf-backup-{stamp}.json"'})


@api.get("/api/admin/export/{kind}.csv")
async def export_csv(kind: str, request: Request, classId: str = ""):
    if not _admin_token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        head, rows = game.csv_table(kind, classId or None)
    except GameError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(head)
    for row in rows:
        # neutralise spreadsheet formulas typed by students (CSV injection)
        writer.writerow([("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v for v in row])
    body = "\ufeff" + buf.getvalue()  # BOM so Excel shows Arabic correctly
    stamp = time.strftime("%Y-%m-%d")
    return Response(body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="difaf-{kind}-{stamp}.csv"'})


@api.post("/api/admin/import")
async def import_data(request: Request):
    if not _admin_token_ok(request):
        return JSONResponse({"ok": False, "error": "انتهت جلسة الدخول"}, status_code=401)
    raw = await request.body()
    if len(raw) > 20_000_000:
        return JSONResponse({"ok": False, "error": "الملف كبير جدًا"}, status_code=413)
    try:
        data = json.loads(raw.decode("utf-8"))
        result = game.data_import(data)
    except GameError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"ok": False, "error": "الملف ليس بصيغة JSON صحيحة"}, status_code=400)
    await flush(force=True)
    return result


app = socketio.ASGIApp(sio, other_asgi_app=api)
