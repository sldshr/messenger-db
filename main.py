#!/usr/bin/env python3
"""
SDM — sldshr's direct messanger
Кроссплатформенный мессенджер 1-на-1.
Вся история и счётчики непрочитанных — на сервере.
"""

# ============================ CONFIG ============================
APP_NAME        = "SDM"
APP_SUB         = "sldshr's direct messenger"
DEFAULT_THEME   = "system"     # "system" | "light" | "dark"
WELCOME_MESSAGE = ""           # если не пусто — показывается при первом входе
# ================================================================

import os
import sys
import subprocess
import importlib
import time

# ============================ BOOT / DEPS ============================
RESET="\x1b[0m"; BOLD="\x1b[1m"; DIM="\x1b[2m"
RED="\x1b[31m"; GREEN="\x1b[32m"; YELLOW="\x1b[33m"
BLUE="\x1b[34m"; CYAN="\x1b[36m"; WHITE="\x1b[97m"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ASCII_SDM = r"""
   ███████╗██████╗ ███╗   ███╗
   ██╔════╝██╔══██╗████╗ ████║
   ███████╗██║  ██║██╔████╔██║
   ╚════██║██║  ██║██║╚██╔╝██║
   ███████║██████╔╝██║ ╚═╝ ██║
   ╚══════╝╚═════╝ ╚═╝     ╚═╝
"""


def _c(code: str, text: str) -> str:
    return f"{code}{text}{RESET}"


REQUIRED = [("fastapi", "fastapi"), ("uvicorn", "uvicorn")]
OPTIONAL = [("uvloop", "uvloop")]


def _print_banner():
    print()
    print(_c(CYAN + BOLD, ASCII_SDM))
    print("   " + _c(DIM + WHITE, APP_SUB))
    print()


def _check_deps() -> None:
    _print_banner()
    missing = []
    for mod, pkg in REQUIRED:
        try:
            importlib.import_module(mod)
            print(f"  {_c(GREEN, '✓')}  {pkg}")
        except ImportError:
            print(f"  {_c(RED, '✗')}  {pkg}")
            missing.append(pkg)
    for mod, pkg in OPTIONAL:
        try:
            importlib.import_module(mod)
            print(f"  {_c(GREEN, '✓')}  {pkg}")
        except ImportError:
            print(f"  {_c(DIM, '·')}  {_c(DIM, pkg)}")
    print()
    if not missing:
        return
    try:
        ans = input(f"  Установить {', '.join(missing)}? [Y/n] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        ans = "n"; print()
    if ans not in ("", "y", "yes", "д", "да"):
        print(_c(RED, "  Отменено."))
        sys.exit(1)
    rc = subprocess.call([sys.executable, "-m", "pip", "install"] + missing)
    if rc != 0:
        print(_c(RED, "  pip завершился с ошибкой."))
        sys.exit(1)
    print(_c(DIM, "\n  Перезапуск...\n"))
    time.sleep(0.5)
    os.execv(sys.executable, [sys.executable] + sys.argv)


_check_deps()

# ============================ IMPORTS ============================
import asyncio
import hashlib
import json
import secrets
import struct
import sys as _sys
import time as _time
import traceback
import uuid
from collections import deque
from typing import Any, Optional
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

try:
    import uvloop  # type: ignore
    _UVLOOP = True
except ImportError:
    _UVLOOP = False

# ============================ CONSTANTS ============================
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

PAIR_HISTORY_MAX   = 1000
MAX_CONTACTS       = 500
MAX_MSG_CHARS      = 4000
SESSION_TTL_MS     = 30 * 24 * 3600_000
AUTH_WINDOW        = 60
AUTH_MAX           = 15
MSG_WINDOW         = 10
MSG_MAX            = 30
CONN_WINDOW        = 10
CONN_MAX           = 20
MAX_WS_FRAME       = MAX_MSG_CHARS * 4 + 4096

SCRYPT_N        = 2 ** 15
SCRYPT_R        = 8
SCRYPT_P        = 1
SCRYPT_MAXMEM   = 256 * 1024 * 1024
SCRYPT_PARALLEL = 4


def log_err(tag: str, exc: BaseException):
    print(f"[{tag}] {type(exc).__name__}: {exc}", file=_sys.stderr)
    traceback.print_exc()


# ============================ PROTOCOL ============================
# [type:uint8][length:uint32 BE][JSON UTF-8]
(T_REGISTER, T_AUTH, T_AUTH_OK, T_MSG, T_PING, T_PONG, T_ERROR,
 T_HELLO, T_STATUS, T_CONTACT_REQ, T_CONTACT_OK, T_CONTACT_ADD,
 T_CHAT_END, T_LOGOUT, T_READ) = range(1, 16)

_HDR = struct.Struct(">BI")


def pack(t: int, o: Any) -> bytes:
    p = json.dumps(o, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _HDR.pack(t, len(p)) + p


def unpack(data: bytes):
    t, ln = _HDR.unpack_from(data, 0)
    return t, json.loads(data[5:5 + ln].decode("utf-8"))


# ============================ STATE (RAM) ============================
users: dict[str, dict] = {}
online: dict[str, "Client"] = {}
watchers: dict[str, set] = {}
messages: dict[tuple, deque] = {}
sessions: dict[str, dict] = {}
_auth_buckets: dict[str, deque] = {}
_conn_buckets: dict[str, deque] = {}
_scrypt_sem = asyncio.Semaphore(SCRYPT_PARALLEL)
_DUMMY_SALT = os.urandom(16)


# ============================ HELPERS ============================
def scrypt_raw(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        pw.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
        maxmem=SCRYPT_MAXMEM,
    )


async def scrypt_async(pw: str, salt: bytes) -> bytes:
    async with _scrypt_sem:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, scrypt_raw, pw, salt)


def pair_key(a: str, b: str) -> tuple:
    return (a, b) if a < b else (b, a)


def check_rate(bucket: dict, key: str, maxn: int, window: float) -> bool:
    now = _time.monotonic()
    dq = bucket.get(key)
    if dq is None:
        dq = deque()
        bucket[key] = dq
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(dq) >= maxn:
        return False
    dq.append(now)
    return True


def valid_login(login: str) -> bool:
    if not isinstance(login, str) or not (3 <= len(login) <= 24):
        return False
    return all(ch.isalnum() or ch in "_-." for ch in login)


def valid_pw(pw: str) -> bool:
    return isinstance(pw, str) and 8 <= len(pw) <= 128


def new_session(login: str) -> str:
    tok = secrets.token_urlsafe(32)
    sessions[tok] = {"login": login, "exp": int(_time.time() * 1000) + SESSION_TTL_MS}
    return tok


def resolve_session(tok: str) -> Optional[str]:
    s = sessions.get(tok)
    if not s:
        return None
    if s["exp"] < int(_time.time() * 1000):
        sessions.pop(tok, None)
        return None
    return s["login"]


def drop_session(tok: str):
    sessions.pop(tok, None)


def ws_ip(ws: WebSocket) -> str:
    return ws.client.host if ws.client else "unknown"


# ============================ CLIENT ============================
class Client:
    __slots__ = ("ws", "login", "lock", "ip")

    def __init__(self, ws: WebSocket, ip: str):
        self.ws = ws
        self.login: Optional[str] = None
        self.ip = ip
        self.lock = asyncio.Lock()

    async def send(self, t: int, o: Any):
        async with self.lock:
            try:
                await self.ws.send_bytes(pack(t, o))
            except Exception:
                pass

    async def send_error(self, code: str, msg: str, log: str = ""):
        await self.send(T_ERROR, {"m": msg, "c": code, "l": log[:4000]})


async def notify_watchers(login: str, is_online: bool):
    w = watchers.get(login)
    if not w:
        return
    frame = pack(T_STATUS, {"u": login, "o": is_online})
    for watcher in list(w):
        c = online.get(watcher)
        if c is None:
            continue
        async with c.lock:
            try:
                await c.ws.send_bytes(frame)
            except Exception:
                pass


# ============================ HANDLERS ============================
async def handle_register(c: Client, obj: dict):
    try:
        login = (obj.get("login") or "").strip().lower()
        pw = obj.get("password") or ""
        remember = bool(obj.get("remember"))

        if not valid_login(login):
            return await c.send_error("VALIDATION", "Логин: 3–24 символа, a-z 0-9 . _ -")
        if not valid_pw(pw):
            return await c.send_error("VALIDATION", "Пароль минимум 8 символов")
        if login in users:
            return await c.send_error("LOGIN_TAKEN", "Логин уже занят")

        salt = os.urandom(16)
        try:
            h = await scrypt_async(pw, salt)
        except Exception as e:
            log_err("scrypt.register", e)
            return await c.send_error("SERVER", "Ошибка хэширования пароля",
                                      f"{type(e).__name__}: {e}")

        users[login] = {"salt": salt, "pw": h, "contacts": set(), "unread": {}}
        watchers.setdefault(login, set())
        await _finish_auth(c, login, remember, new_pw_ok=True)
    except Exception as e:
        log_err("register", e)
        await c.send_error("SERVER", "Внутренняя ошибка регистрации",
                           f"{type(e).__name__}: {e}")


async def handle_auth(c: Client, obj: dict):
    try:
        tok = obj.get("token")
        if isinstance(tok, str) and tok:
            login = resolve_session(tok)
            if not login or login not in users:
                return await c.send_error("SESSION", "Сессия истекла, войдите заново")
            if login in online:
                old = online.get(login)
                try:
                    if old: await old.ws.close(code=4000, reason="replaced")
                except Exception:
                    pass
                online.pop(login, None)
            return await _finish_auth(c, login, remember=False, restore_token=tok)

        login = (obj.get("login") or "").strip().lower()
        pw = obj.get("password") or ""
        remember = bool(obj.get("remember"))

        if not check_rate(_auth_buckets, c.ip, AUTH_MAX, AUTH_WINDOW):
            return await c.send_error("RATE_LIMIT", "Слишком много попыток входа")

        u = users.get(login)
        if u is None:
            try:
                await scrypt_async(pw if isinstance(pw, str) else "", _DUMMY_SALT)
            except Exception:
                pass
            return await c.send_error("AUTH_FAIL", "Неверный логин или пароль")

        try:
            h = await scrypt_async(pw, u["salt"])
        except Exception as e:
            log_err("scrypt.auth", e)
            return await c.send_error("SERVER", "Ошибка сервера",
                                      f"{type(e).__name__}: {e}")

        if not secrets.compare_digest(u["pw"], h):
            return await c.send_error("AUTH_FAIL", "Неверный логин или пароль")

        if login in online:
            old = online.get(login)
            try:
                if old: await old.ws.close(code=4000, reason="replaced")
            except Exception:
                pass
            online.pop(login, None)

        await _finish_auth(c, login, remember)
    except Exception as e:
        log_err("auth", e)
        await c.send_error("SERVER", "Внутренняя ошибка аутентификации",
                           f"{type(e).__name__}: {e}")


async def _finish_auth(c: Client, login: str, remember: bool,
                       new_pw_ok: bool = False, restore_token: Optional[str] = None):
    c.login = login
    online[login] = c
    u = users[login]

    contacts_payload = [
        {"u": peer, "o": peer in online}
        for peer in u["contacts"]
    ]

    hist = []
    for (a, b), dq in messages.items():
        if a == login or b == login:
            hist.extend(dq)
    hist.sort(key=lambda m: m["s"])

    unread_payload = {p: n for p, n in u["unread"].items() if n > 0}

    payload = {
        "login": login,
        "contacts": contacts_payload,
        "history": hist,
        "unread": unread_payload,
    }

    if new_pw_ok or remember:
        payload["token"] = new_session(login)
    elif restore_token:
        payload["token"] = restore_token

    await c.send(T_AUTH_OK, payload)
    await notify_watchers(login, True)


async def handle_msg(c: Client, obj: dict):
    try:
        if not check_rate(_auth_buckets, "m:" + (c.login or ""), MSG_MAX, MSG_WINDOW):
            return await c.send_error("RATE_LIMIT", "Слишком много сообщений")

        to_login = (obj.get("t") or "").strip().lower()
        text = obj.get("x")
        if not isinstance(to_login, str) or not isinstance(text, str) or not text:
            return await c.send_error("VALIDATION", "Некорректное сообщение")
        if len(text) > MAX_MSG_CHARS:
            return await c.send_error("VALIDATION", "Сообщение слишком большое")
        if to_login == c.login:
            return await c.send_error("VALIDATION", "Нельзя писать самому себе")

        me = users[c.login]
        peer = users.get(to_login)
        if peer is None:
            return await c.send_error("NOT_FOUND", "Получатель не найден")
        if to_login not in me["contacts"] or c.login not in peer["contacts"]:
            return await c.send_error("NOT_CONTACT", "Получатель не в ваших контактах")

        mid = obj.get("i") or uuid.uuid4().hex
        msg = {
            "i": mid, "f": c.login, "t": to_login,
            "x": text,
            "s": int(_time.time() * 1000),
        }

        key = pair_key(c.login, to_login)
        dq = messages.get(key)
        if dq is None:
            dq = deque(maxlen=PAIR_HISTORY_MAX)
            messages[key] = dq
        dq.append(msg)

        # эхо отправителю (без n)
        await c.send(T_MSG, msg)

        # получателю — инкрементим счётчик и включаем абсолютное значение в T_MSG
        peer["unread"][c.login] = peer["unread"].get(c.login, 0) + 1
        n = peer["unread"][c.login]
        target = online.get(to_login)
        if target is not None:
            await target.send(T_MSG, {**msg, "n": n})
    except Exception as e:
        log_err("msg", e)
        await c.send_error("SERVER", "Не удалось доставить сообщение",
                           f"{type(e).__name__}: {e}")


async def handle_contact_req(c: Client, obj: dict):
    try:
        peer = (obj.get("u") or "").strip().lower()
        if not valid_login(peer):
            return await c.send_error("VALIDATION", "Неверный формат логина")
        if peer == c.login:
            return await c.send_error("VALIDATION", "Это ваш логин")
        u = users.get(peer)
        if not u:
            return await c.send_error("NOT_FOUND", "Пользователь не найден")

        me = users[c.login]
        if len(me["contacts"]) >= MAX_CONTACTS:
            return await c.send_error("CONTACT_LIMIT", "Достигнут лимит контактов")
        if len(u["contacts"]) >= MAX_CONTACTS:
            return await c.send_error("CONTACT_LIMIT", "У собеседника лимит контактов")

        me["contacts"].add(peer)
        u["contacts"].add(c.login)
        watchers.setdefault(peer, set()).add(c.login)
        watchers.setdefault(c.login, set()).add(peer)

        await c.send(T_CONTACT_OK, {"u": peer, "o": peer in online})

        target = online.get(peer)
        if target:
            await target.send(T_CONTACT_ADD, {"u": c.login, "o": True})
    except Exception as e:
        log_err("contact_req", e)
        await c.send_error("SERVER", "Не удалось добавить контакт",
                           f"{type(e).__name__}: {e}")


async def handle_chat_end(c: Client, obj: dict):
    try:
        peer = (obj.get("u") or "").strip().lower()
        if not valid_login(peer):
            return
        my = c.login

        users[my]["contacts"].discard(peer)
        users[my]["unread"].pop(peer, None)
        if peer in users:
            users[peer]["contacts"].discard(my)
            users[peer]["unread"].pop(my, None)

        w_me = watchers.get(my)
        if w_me: w_me.discard(peer)
        w_peer = watchers.get(peer)
        if w_peer: w_peer.discard(my)

        messages.pop(pair_key(my, peer), None)

        target = online.get(peer)
        if target:
            await target.send(T_CHAT_END, {"u": my})
    except Exception as e:
        log_err("chat_end", e)


async def handle_read(c: Client, obj: dict):
    """Пользователь открыл чат — обнуляем серверный счётчик непрочитанных."""
    try:
        peer = (obj.get("u") or "").strip().lower()
        if not valid_login(peer):
            return
        u = users.get(c.login)
        if u:
            u["unread"].pop(peer, None)
    except Exception as e:
        log_err("read", e)


async def handle_logout(c: Client, obj: dict):
    try:
        tok = obj.get("token")
        if isinstance(tok, str) and tok:
            drop_session(tok)
    except Exception as e:
        log_err("logout", e)


# ============================ SELF-TEST ============================
def _self_test() -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []

    # 1. scrypt
    try:
        h = hashlib.scrypt(b"x", salt=b"0" * 16, n=2 ** 15, r=8, p=1,
                           dklen=32, maxmem=256 * 1024 * 1024)
        checks.append(("scrypt-2^15", len(h) == 32))
    except Exception:
        checks.append(("scrypt-2^15", False))

    # 2. protocol round-trip x10000
    try:
        ok = True
        for i in range(10000):
            b = pack(T_MSG, {"i": str(i), "x": "hello" * (i % 20), "y": [1, 2, 3]})
            t, o = unpack(b)
            if t != T_MSG or o["i"] != str(i):
                ok = False
                break
        checks.append(("protocol-10k", ok))
    except Exception:
        checks.append(("protocol-10k", False))

    # 3. unicode (эмодзи, кириллица, RTL, surrogates)
    try:
        s = "привет 🌍 hello مرحبا 世界 \U0001F600"
        b = pack(T_MSG, {"x": s})
        _, o = unpack(b)
        checks.append(("unicode", o["x"] == s))
    except Exception:
        checks.append(("unicode", False))

    # 4. big payload
    try:
        big = "a" * 4000
        b = pack(T_MSG, {"x": big})
        _, o = unpack(b)
        checks.append(("payload-4k", o["x"] == big and len(b) > 4000))
    except Exception:
        checks.append(("payload-4k", False))

    # 5. rate limiter
    try:
        bucket = {}
        a = all(check_rate(bucket, "k", 5, 1.0) for _ in range(5))
        b = not check_rate(bucket, "k", 5, 1.0)
        checks.append(("rate_limiter", a and b))
    except Exception:
        checks.append(("rate_limiter", False))

    # 6. validation
    try:
        ok = (valid_login("alice") and valid_login("bob.42") and
              not valid_login("a") and not valid_login("bad name") and
              not valid_login("") and not valid_login("a" * 30) and
              valid_pw("pass1234") and not valid_pw("short") and
              not valid_pw("x" * 200))
        checks.append(("validation", ok))
    except Exception:
        checks.append(("validation", False))

    # 7. pair_key инвариант
    try:
        ok = (pair_key("a", "b") == pair_key("b", "a") == ("a", "b") and
              pair_key("a", "a") == ("a", "a"))
        checks.append(("pair_key", ok))
    except Exception:
        checks.append(("pair_key", False))

    # 8. constant-time сравнение
    try:
        ok = secrets.compare_digest(b"abc", b"abc") and not secrets.compare_digest(b"abc", b"abd")
        checks.append(("ct_compare", ok))
    except Exception:
        checks.append(("ct_compare", False))

    return checks


def _print_self_test(results):
    print(_c(BOLD, "  self-test:"))
    all_ok = True
    for name, ok in results:
        if ok:
            print(f"    {_c(GREEN, '✓')}  {name}")
        else:
            print(f"    {_c(RED, '✗')}  {name}")
            all_ok = False
    print()
    return all_ok


# ============================ APP ============================
app = FastAPI()


@app.get("/")
async def index():
    return HTMLResponse(HTML_PAGE)


@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    try:
        origin = ws.headers.get("origin", "")
        host = ws.headers.get("host", "")
        if origin:
            try:
                ohost = urlparse(origin).netloc.lower()
                if ohost and host and ohost != host.lower():
                    await ws.close(code=1008)
                    return
            except Exception:
                pass
    except Exception:
        pass

    ip = ws_ip(ws)
    if not check_rate(_conn_buckets, ip, CONN_MAX, CONN_WINDOW):
        try:
            await ws.accept()
            await ws.close(code=1013)
        except Exception:
            pass
        return

    try:
        await ws.accept()
    except Exception as e:
        log_err("ws.accept", e)
        return

    c = Client(ws, ip)
    try:
        await c.send(T_HELLO, {
            "app_name": APP_NAME,
            "app_sub": APP_SUB,
            "welcome": WELCOME_MESSAGE,
            "default_theme": DEFAULT_THEME,
        })
        while True:
            raw = await ws.receive_bytes()
            if len(raw) > MAX_WS_FRAME:
                await ws.close(code=1009)
                return
            if len(raw) < _HDR.size:
                continue
            try:
                t, obj = unpack(raw)
            except Exception as e:
                log_err("ws.unpack", e)
                continue

            if t == T_REGISTER:
                await handle_register(c, obj)
            elif t == T_AUTH:
                await handle_auth(c, obj)
            elif t == T_MSG and c.login:
                await handle_msg(c, obj)
            elif t == T_CONTACT_REQ and c.login:
                await handle_contact_req(c, obj)
            elif t == T_CHAT_END and c.login:
                await handle_chat_end(c, obj)
            elif t == T_READ and c.login:
                await handle_read(c, obj)
            elif t == T_LOGOUT and c.login:
                await handle_logout(c, obj)
            elif t == T_PING:
                await c.send(T_PONG, {})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log_err("ws.loop", e)
        try:
            await c.send_error("SERVER", "Внутренняя ошибка сервера",
                               f"{type(e).__name__}: {e}")
        except Exception:
            pass
    finally:
        if c.login and online.get(c.login) is c:
            online.pop(c.login, None)
            try:
                await notify_watchers(c.login, False)
            except Exception as e:
                log_err("notify_watchers", e)


# ============================ HTML ============================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>{{APP_NAME}}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='5' fill='%232563eb'/%3E%3Cpath d='M18 4H6a2 2 0 0 0-2 2v12l3-3h11a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2z' fill='white'/%3E%3C/svg%3E">
<script>
  window.__CFG = {{CFG_JSON}};
</script>
<style>
:root{
  --bg-app:#ffffff;
  --bg-sidebar:#f7f8fa;
  --bg-panel:#ffffff;
  --bg-hover:#eef0f3;
  --bg-active:#e4e8ee;
  --bg-input:#f2f4f7;
  --bg-bubble:#f2f4f7;
  --bg-bubble-me:#2563eb;
  --fg:#111827;
  --fg-muted:#6b7280;
  --border:#e5e7eb;
  --border-strong:#d1d5db;
  --accent:#2563eb;
  --accent-hover:#1d4ed8;
  --green:#16a34a;
  --red:#dc2626;
  --shadow-lg:0 20px 40px rgba(15,23,42,.10);
  --shadow-sm:0 1px 2px rgba(15,23,42,.06);
}
:root[data-theme="dark"]{
  --bg-app:#16181d;
  --bg-sidebar:#1c1f26;
  --bg-panel:#1c1f26;
  --bg-hover:#23272f;
  --bg-active:#2a2e37;
  --bg-input:#23272f;
  --bg-bubble:#23272f;
  --bg-bubble-me:#2563eb;
  --fg:#e5e7eb;
  --fg-muted:#9ca3af;
  --border:#2a2e37;
  --border-strong:#3a3f4b;
  --accent:#2563eb;
  --accent-hover:#3b82f6;
  --green:#22c55e;
  --red:#ef4444;
  --shadow-lg:0 20px 40px rgba(0,0,0,.45);
  --shadow-sm:0 1px 2px rgba(0,0,0,.35);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{
  height:100%;overflow:hidden;overscroll-behavior:none;
  -webkit-user-select:none;-moz-user-select:none;-ms-user-select:none;user-select:none;
  -webkit-touch-callout:none;
}
input,textarea,pre{-webkit-user-select:text;user-select:text}
*{scrollbar-width:none;-ms-overflow-style:none}
*::-webkit-scrollbar{width:0;height:0;display:none}
body{
  background:var(--bg-app);color:var(--fg);
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  transition:background .18s,color .18s;
  -webkit-font-smoothing:antialiased;
}
button{font:inherit;cursor:pointer;color:inherit;background:none;border:none}
input{font:inherit}

@keyframes spin{to{transform:rotate(360deg)}}
button.busy{position:relative;color:transparent !important}
button.busy::after{
  content:"";position:absolute;top:50%;left:50%;
  width:16px;height:16px;margin:-8px 0 0 -8px;
  border:2px solid rgba(255,255,255,.35);border-top-color:#fff;
  border-radius:50%;animation:spin .7s linear infinite;
}
button.btn-secondary.busy::after{
  border-color:rgba(128,128,128,.2);border-top-color:var(--accent);
}

#login{
  position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  padding:20px;background:var(--bg-sidebar);z-index:100;
}
.card{
  width:100%;max-width:400px;background:var(--bg-panel);border-radius:14px;
  padding:28px;box-shadow:var(--shadow-lg);position:relative;
  border:1px solid var(--border);
}
.brand{display:flex;align-items:center;gap:11px;margin-bottom:22px}
.brand-logo{
  width:40px;height:40px;border-radius:10px;background:var(--accent);
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
}
.brand-text{display:flex;flex-direction:column;line-height:1.15;min-width:0}
.brand-title{font-weight:700;font-size:16px;letter-spacing:-.01em;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.brand-sub{font-size:11px;color:var(--fg-muted);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}
.theme-btn{position:absolute;top:14px;right:14px}
.tabs{display:flex;background:var(--bg-input);border-radius:9px;padding:3px;margin-bottom:18px}
.tabs button{
  flex:1;color:var(--fg-muted);padding:9px;border-radius:7px;
  font-weight:500;font-size:13px;transition:all .15s;
}
.tabs button.active{
  background:var(--bg-panel);color:var(--fg);
  box-shadow:var(--shadow-sm);
}
.card input[type=text],.card input[type=password]{
  width:100%;background:var(--bg-input);border:1px solid transparent;
  color:var(--fg);padding:12px 14px;border-radius:8px;outline:none;
  font-size:15px;margin-bottom:10px;transition:border .12s;
}
.card input[type=text]:focus,.card input[type=password]:focus{border-color:var(--accent)}
.remember{
  display:flex;align-items:center;gap:8px;font-size:13px;
  color:var(--fg-muted);margin:0 2px 14px;cursor:pointer;
}
.remember input{width:15px;height:15px;margin:0;accent-color:var(--accent);cursor:pointer}
#submitBtn{
  width:100%;background:var(--accent);color:#fff;padding:12px;
  border-radius:8px;font-weight:600;font-size:14px;
  transition:background .12s;
}
#submitBtn:hover:not(:disabled){background:var(--accent-hover)}
#submitBtn:disabled{cursor:default}
#authErr{color:var(--red);font-size:13px;margin-top:12px;min-height:17px;text-align:center}
#authNote{color:var(--fg-muted);font-size:12px;margin-top:4px;text-align:center;line-height:1.4}

#app{display:none;height:100dvh}
#app.on{display:flex}

#sidebar{
  width:320px;flex-shrink:0;background:var(--bg-sidebar);
  display:flex;flex-direction:column;border-right:1px solid var(--border);
}
.sidebar-header{
  display:flex;align-items:center;gap:4px;padding:12px 14px;
  padding-top:calc(12px + env(safe-area-inset-top));
  border-bottom:1px solid var(--border);background:var(--bg-sidebar);
}
.my-info{
  flex:1;min-width:0;cursor:pointer;border-radius:6px;padding:4px 6px;
  transition:background .12s;
}
.my-info:hover{background:var(--bg-hover)}
.my-info:active{background:var(--bg-active)}
.my-name{
  font-weight:600;font-size:14px;letter-spacing:-.005em;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.my-sub{font-size:11.5px;color:var(--fg-muted);margin-top:1px;
  display:flex;align-items:center;gap:5px}
.dot-mini{width:7px;height:7px;border-radius:50%;background:var(--green);flex-shrink:0}
.icon-btn{
  color:var(--fg-muted);padding:8px;border-radius:8px;
  display:flex;align-items:center;justify-content:center;
  transition:background .12s,color .12s;flex-shrink:0;
  min-width:36px;min-height:36px;
}
.icon-btn:hover{background:var(--bg-hover);color:var(--fg)}
.icon-btn.danger{color:var(--red)}
.icon-btn.danger:hover{background:rgba(220,38,38,.10);color:var(--red)}

.sidebar-title{
  padding:16px 16px 6px;font-size:11px;font-weight:600;
  color:var(--fg-muted);text-transform:uppercase;letter-spacing:.06em;
  display:flex;justify-content:space-between;align-items:center;
}
.sidebar-title .count{font-weight:500;text-transform:none;letter-spacing:0;color:var(--fg-muted)}

#contacts{flex:1;overflow-y:auto;padding:0 8px 8px}
.empty-list{
  padding:36px 24px;text-align:center;color:var(--fg-muted);
  font-size:13px;line-height:1.6;
}

.contact{
  display:flex;align-items:center;gap:10px;padding:10px;
  border-radius:8px;cursor:pointer;transition:background .1s;
  position:relative;min-height:56px;
}
.contact:hover{background:var(--bg-hover)}
.contact.active{background:var(--bg-active)}
.status-line{
  width:8px;height:8px;border-radius:50%;flex-shrink:0;
  background:var(--border-strong);transition:background .15s;
}
.status-line.online{background:var(--green)}
.status-line.unknown{background:var(--border-strong)}
.contact-info{flex:1;min-width:0}
.contact-name{
  font-weight:600;font-size:14px;letter-spacing:-.005em;
  color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.contact-preview{
  font-size:12.5px;color:var(--fg-muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;margin-top:2px;
}
.contact.unread .contact-preview{color:var(--fg);font-weight:500}
.badge{
  background:var(--red);color:#fff;font-size:11px;font-weight:700;
  min-width:22px;height:22px;padding:0 7px;border-radius:11px;
  display:inline-flex;align-items:center;justify-content:center;
  flex-shrink:0;line-height:1;letter-spacing:-.01em;
}
.contact .pin-btn{
  color:var(--fg-muted);padding:5px;border-radius:6px;
  display:none;flex-shrink:0;align-items:center;justify-content:center;
  transition:all .12s;min-width:28px;min-height:28px;
}
.contact.pinned .pin-btn{display:flex;color:var(--accent)}
.contact:hover .pin-btn{display:flex}
.contact:hover .badge{display:none}
.contact .pin-btn:hover{background:var(--bg-active)}

#chatPane{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg-app)}
#emptyState{
  flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;color:var(--fg-muted);gap:14px;padding:24px;text-align:center;
}
#emptyState svg{opacity:.18}
#emptyState p{font-size:13px;letter-spacing:-.005em}
#conversation{display:none;flex-direction:column;flex:1;min-height:0}
#chatPane.has-chat #conversation{display:flex}
#chatPane.has-chat #emptyState{display:none}

.conv-header{
  display:flex;align-items:center;gap:6px;padding:11px 16px;
  padding-top:calc(11px + env(safe-area-inset-top));
  border-bottom:1px solid var(--border);background:var(--bg-app);
}
#backBtn{display:none}
.conv-peer{
  display:flex;flex-direction:column;min-width:0;flex:1;
  cursor:pointer;border-radius:6px;padding:3px 6px;margin:-3px -6px;
  transition:background .12s;
}
.conv-peer:hover{background:var(--bg-hover)}
.conv-peer:active{background:var(--bg-active)}
.peer-name{font-weight:600;font-size:14.5px;letter-spacing:-.005em;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.peer-sub{font-size:12px;color:var(--fg-muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.peer-sub.online{color:var(--green)}

#msgs{
  flex:1;overflow-y:auto;padding:18px 18px 8px;display:flex;
  flex-direction:column;gap:3px;
}
.m{
  max-width:68%;padding:8px 12px;border-radius:14px;
  background:var(--bg-bubble);align-self:flex-start;
  word-wrap:break-word;overflow-wrap:anywhere;
  font-size:14.5px;line-height:1.4;animation:fade .12s ease-out;
}
@keyframes fade{from{opacity:.5}to{opacity:1}}
.m.me{align-self:flex-end;background:var(--bg-bubble-me);color:#fff}
.m .ts{font-size:10.5px;opacity:.55;margin-top:3px;display:block;text-align:right}
.m.me .ts{opacity:.8}
.day-sep{
  align-self:center;font-size:11px;color:var(--fg-muted);
  padding:5px 12px;background:var(--bg-input);border-radius:10px;
  margin:10px 0 6px;
}

#composer{
  display:flex;align-items:center;gap:8px;padding:10px 14px;
  padding-bottom:calc(10px + env(safe-area-inset-bottom));
  border-top:1px solid var(--border);background:var(--bg-app);
}
#inp{
  flex:1;background:var(--bg-input);border:1px solid transparent;
  color:var(--fg);padding:11px 16px;border-radius:10px;outline:none;
  font-size:16px;min-width:0;transition:border .12s;
}
#inp:focus{border-color:var(--accent)}
#inp::placeholder{color:var(--fg-muted)}
#sendBtn{
  width:44px;height:44px;border-radius:10px;background:var(--accent);
  color:#fff;display:flex;align-items:center;justify-content:center;
  flex-shrink:0;transition:background .12s,transform .06s;
}
#sendBtn:hover{background:var(--accent-hover)}
#sendBtn:active{transform:scale(.95)}

.modal-backdrop{
  position:fixed;inset:0;background:rgba(15,23,42,.5);
  display:none;align-items:center;justify-content:center;padding:20px;z-index:200;
  backdrop-filter:blur(2px);
}
.modal-backdrop.open{display:flex;animation:fadein .12s}
@keyframes fadein{from{opacity:0}to{opacity:1}}
.modal{
  background:var(--bg-panel);border-radius:14px;padding:24px;
  width:100%;max-width:440px;box-shadow:var(--shadow-lg);
  max-height:calc(100dvh - 40px);overflow:auto;border:1px solid var(--border);
}
.modal h3{font-size:17px;margin-bottom:8px;font-weight:700;letter-spacing:-.01em}
.modal .hint{color:var(--fg-muted);font-size:13px;margin-bottom:16px;line-height:1.55}
.modal input{
  width:100%;padding:12px 14px;border-radius:8px;
  border:1px solid var(--border);background:var(--bg-input);
  font-size:16px;outline:none;margin-bottom:10px;color:var(--fg);
}
.modal input:focus{border-color:var(--accent)}
.modal .err{color:var(--red);font-size:13px;min-height:18px;margin-bottom:8px}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;margin-top:6px}
.btn-secondary{
  background:var(--bg-input);padding:11px 18px;border-radius:8px;
  font-weight:500;color:var(--fg);transition:background .12s;font-size:14px;
}
.btn-secondary:hover{background:var(--bg-hover)}
.btn-primary{
  background:var(--accent);color:#fff;padding:11px 22px;
  border-radius:8px;font-weight:600;font-size:14px;
  transition:background .12s;
}
.btn-primary:hover:not(:disabled){background:var(--accent-hover)}
.btn-danger{
  background:var(--red);color:#fff;padding:11px 22px;
  border-radius:8px;font-weight:600;font-size:14px;transition:opacity .12s;
}
.btn-danger:hover{opacity:.9}
.btn-primary:disabled,.btn-secondary:disabled,.btn-danger:disabled{cursor:default;opacity:.55}

.err-modal h3{color:var(--red)}
.err-meta{font-size:12px;color:var(--fg-muted);margin-bottom:10px}
.err-meta code{
  background:var(--bg-input);padding:3px 8px;border-radius:5px;
  color:var(--fg);font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
}
.err-desc{font-size:13.5px;line-height:1.5;color:var(--fg);margin-bottom:14px;word-wrap:break-word}
.err-log-label{
  font-size:11px;font-weight:700;color:var(--fg-muted);
  text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;
}
.err-modal pre.err-log{
  background:var(--bg-input);padding:10px 12px;border-radius:8px;
  font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.5;
  max-height:200px;overflow:auto;white-space:pre-wrap;word-break:break-all;
  color:var(--fg);margin-bottom:14px;border:1px solid var(--border);
}

#welcomeModal .modal h3{color:var(--accent)}
#welcomeBody{
  font-size:14px;line-height:1.6;color:var(--fg);margin-bottom:18px;
  white-space:pre-wrap;word-wrap:break-word;
}

#toast{
  position:fixed;left:50%;bottom:32px;transform:translateX(-50%) translateY(20px);
  background:var(--fg);color:var(--bg-app);padding:10px 16px;border-radius:8px;
  font-size:13.5px;font-weight:500;opacity:0;pointer-events:none;
  transition:opacity .18s,transform .18s;z-index:300;
  box-shadow:0 8px 24px rgba(0,0,0,.25);max-width:80%;text-align:center;
  word-break:break-word;line-height:1.4;
}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* ============ MOBILE ============ */
@media (max-width:720px){
  #app{position:relative;overflow:hidden}

  #sidebar{
    position:absolute;inset:0;width:100%;z-index:2;
    border-right:none;transition:transform .24s ease;
  }
  #chatPane{
    position:absolute;inset:0;z-index:3;transform:translateX(100%);
    transition:transform .24s ease;box-shadow:-8px 0 24px rgba(0,0,0,.10);
  }
  #app.chat-open #sidebar{transform:translateX(-22%)}
  #app.chat-open #chatPane{transform:translateX(0)}
  #backBtn{display:flex}

  /* увеличенные touch-target */
  .icon-btn{min-width:44px;min-height:44px;padding:10px}
  .sidebar-header{padding:10px 12px}
  .my-name{font-size:15px}
  .my-sub{font-size:12px}

  .contact{padding:14px 12px;min-height:68px;gap:12px}
  .contact-name{font-size:15px}
  .contact-preview{font-size:13px;margin-top:3px}
  .badge{min-width:24px;height:24px;font-size:12px;border-radius:12px}

  /* pin-btn в списке на мобиле не показываем — только через шапку чата */
  .contact .pin-btn{display:none !important}

  .conv-header{padding:10px 12px}
  .peer-name{font-size:16px}
  .peer-sub{font-size:12.5px}

  .m{max-width:84%;font-size:15px;padding:9px 13px}

  .modal{padding:22px 20px;border-radius:16px}
  .modal h3{font-size:18px}
  .modal input{padding:14px;font-size:16px}
  .btn-primary,.btn-secondary,.btn-danger{padding:13px 22px;font-size:15px;min-height:46px}

  #submitBtn{padding:14px;font-size:15px;min-height:48px}
  .tabs button{padding:11px;font-size:14px}
  .card{padding:24px 22px}
  .card input[type=text],.card input[type=password]{padding:14px;font-size:16px}

  #sendBtn{width:46px;height:46px}
  #inp{padding:12px 16px}

  #toast{bottom:calc(24px + env(safe-area-inset-bottom))}
}
</style>
</head>
<body>

<div id="login">
  <form class="card" id="authForm" autocomplete="on">
    <button type="button" class="icon-btn theme-btn" id="themeBtnLogin" title="Тема" aria-label="Тема">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.39 5.39 0 0 1-4.4 2.26 5.4 5.4 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z"/>
      </svg>
    </button>
    <div class="brand">
      <div class="brand-logo">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
          <path d="M18 4H6a2 2 0 0 0-2 2v12l3-3h11a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2z"/>
        </svg>
      </div>
      <div class="brand-text">
        <div class="brand-title" id="brandTitle">{{APP_NAME}}</div>
        <div class="brand-sub" id="brandSub">{{APP_SUB}}</div>
      </div>
    </div>
    <div class="tabs">
      <button type="button" id="tabLogin" class="active">Вход</button>
      <button type="button" id="tabReg">Регистрация</button>
    </div>
    <input id="loginIn" type="text" placeholder="Логин" autocapitalize="off"
           spellcheck="false" maxlength="24" autocomplete="username">
    <input id="pwIn" type="password" placeholder="Пароль" autocomplete="current-password">
    <label class="remember">
      <input type="checkbox" id="rememberIn">
      <span>Запомнить меня</span>
    </label>
    <button type="submit" id="submitBtn">Войти</button>
    <div id="authErr"></div>
    <div id="authNote"></div>
  </form>
</div>

<div id="app">
  <aside id="sidebar">
    <div class="sidebar-header">
      <div class="my-info" id="myInfo" title="Нажмите, чтобы скопировать логин">
        <div class="my-name" id="myLogin">—</div>
        <div class="my-sub"><span class="dot-mini"></span>в сети</div>
      </div>
      <button class="icon-btn" id="themeBtn" title="Тема" aria-label="Тема">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.39 5.39 0 0 1-4.4 2.26 5.4 5.4 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z"/>
        </svg>
      </button>
      <button class="icon-btn" id="addBtn" title="Добавить чат" aria-label="Добавить">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/>
        </svg>
      </button>
      <button class="icon-btn" id="logoutBtn" title="Выйти" aria-label="Выйти">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M16 13v-2H7V8l-5 4 5 4v-3zM20 3h-8v2h8v14h-8v2h8a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z"/>
        </svg>
      </button>
    </div>
    <div class="sidebar-title">
      <span>Чаты</span>
      <span class="count" id="chatCount"></span>
    </div>
    <div id="contacts"></div>
  </aside>

  <section id="chatPane">
    <div id="emptyState">
      <svg width="80" height="80" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M18 4H6a2 2 0 0 0-2 2v12l3-3h11a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zM8 9h8v1.5H8V9zm5 3.5H8V11h5v1.5z"/>
      </svg>
      <p>Выберите чат или добавьте новый</p>
    </div>
    <div id="conversation">
      <div class="conv-header">
        <button class="icon-btn" id="backBtn" aria-label="Назад">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/>
          </svg>
        </button>
        <div class="conv-peer" id="convPeer" title="Нажмите, чтобы скопировать логин">
          <div class="peer-name" id="peerName">—</div>
          <div class="peer-sub" id="peerSub"></div>
        </div>
        <button class="icon-btn" id="pinActiveBtn" title="Закрепить/открепить" aria-label="Закрепить">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M14 4v5c0 1.12.37 2.16 1 3H9c.65-.86 1-1.9 1-3V4h4m3-2H7c-.55 0-1 .45-1 1s.45 1 1 1h1v5c0 1.66-1.34 3-3 3v2h5.97v7l1 1 1-1v-7H19v-2c-1.66 0-3-1.34-3-3V4h1c.55 0 1-.45 1-1s-.45-1-1-1z"/></svg>
        </button>
        <button class="icon-btn danger" id="endChatBtn" title="Завершить чат" aria-label="Завершить чат">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/>
          </svg>
        </button>
      </div>
      <div id="msgs"></div>
      <div id="composer">
        <input id="inp" placeholder="Написать сообщение…" autocomplete="off"
               autocapitalize="sentences">
        <button id="sendBtn" aria-label="Отправить">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M2.01 21 23 12 2.01 3 2 10l15 2-15 2z"/>
          </svg>
        </button>
      </div>
    </div>
  </section>
</div>

<div class="modal-backdrop" id="addModal">
  <div class="modal">
    <h3>Новый чат</h3>
    <div class="hint">Введите логин пользователя этого сервера (например, <b>bob</b>).</div>
    <input id="addInput" placeholder="логин" autocapitalize="off" spellcheck="false" autocomplete="off">
    <div class="err" id="addErr"></div>
    <div class="modal-actions">
      <button class="btn-secondary" id="addCancel" type="button">Отмена</button>
      <button class="btn-primary" id="addConfirm" type="button">Добавить</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="endModal">
  <div class="modal">
    <h3>Завершить чат?</h3>
    <div class="hint" id="endHint">
      Переписка будет удалена у вас и у собеседника. Отменить это действие нельзя.
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" id="endCancel" type="button">Отмена</button>
      <button class="btn-danger" id="endConfirm" type="button">Завершить</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="welcomeModal">
  <div class="modal">
    <h3 id="welcomeTitle">Добро пожаловать</h3>
    <div id="welcomeBody"></div>
    <div class="modal-actions">
      <button class="btn-primary" id="welcomeClose" type="button">Понятно</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="errModal">
  <div class="modal err-modal">
    <h3>Ошибка</h3>
    <div class="err-meta">Код: <code id="errCode">—</code></div>
    <div class="err-desc" id="errDesc"></div>
    <div class="err-log-label" id="errLogLabel">Лог</div>
    <pre class="err-log" id="errLog"></pre>
    <div class="modal-actions">
      <button class="btn-secondary" id="errCopy" type="button">Копировать</button>
      <button class="btn-primary" id="errClose" type="button">Закрыть</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
(() => {
"use strict";

/* ================= ERROR MODAL ================= */
function showError(code, desc, log){
  try {
    document.getElementById("errCode").textContent = code || "UNKNOWN";
    document.getElementById("errDesc").textContent = desc || "Неизвестная ошибка";
    const lg = (log || "").toString();
    document.getElementById("errLog").textContent = lg || "(пусто)";
    document.getElementById("errLogLabel").style.display = lg ? "" : "none";
    document.getElementById("errLog").style.display = lg ? "" : "none";
    document.getElementById("errModal").classList.add("open");
  } catch(e){
    try { alert("[" + code + "] " + desc + "\n\n" + log); } catch(e2){}
  }
}
document.getElementById("errClose").onclick = () => {
  document.getElementById("errModal").classList.remove("open");
};
document.getElementById("errCopy").onclick = async () => {
  const txt = "[" + document.getElementById("errCode").textContent + "] "
            + document.getElementById("errDesc").textContent + "\n\n"
            + document.getElementById("errLog").textContent;
  try { await navigator.clipboard.writeText(txt); } catch(e){
    try {
      const ta = document.createElement("textarea");
      ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); document.execCommand("copy");
      document.body.removeChild(ta);
    } catch(e2){}
  }
};
window.addEventListener("error", e => {
  showError("CLIENT_ERR", e.message || "Ошибка в клиенте",
    (e.error && e.error.stack) ? e.error.stack
    : (e.filename ? `${e.filename}:${e.lineno}:${e.colno}` : ""));
});
window.addEventListener("unhandledrejection", e => {
  const r = e.reason;
  showError((r && r.code) ? r.code : "PROMISE_ERR",
            (r && r.message) ? r.message : String(r),
            (r && r.stack) ? r.stack : "");
});

/* ================= PROTOCOL ================= */
const T = {REGISTER:1, AUTH:2, AUTH_OK:3, MSG:4, PING:5, PONG:6, ERROR:7,
           HELLO:8, STATUS:9, CONTACT_REQ:10, CONTACT_OK:11, CONTACT_ADD:12,
           CHAT_END:13, LOGOUT:14, READ:15};
const _enc = new TextEncoder(), _dec = new TextDecoder();

function pack(type, obj){
  const p = _enc.encode(JSON.stringify(obj));
  const buf = new ArrayBuffer(5 + p.length);
  const dv = new DataView(buf);
  dv.setUint8(0, type);
  dv.setUint32(1, p.length, false);
  new Uint8Array(buf, 5).set(p);
  return buf;
}
function unpack(buf){
  const dv = new DataView(buf);
  const len = dv.getUint32(1, false);
  return [dv.getUint8(0), JSON.parse(_dec.decode(new Uint8Array(buf, 5, len)))];
}

/* ================= THEME ================= */
const LS_THEME = "sdm_theme";
const CFG = window.__CFG || {};
let defaultTheme = CFG.default_theme || "system";
const mq = window.matchMedia("(prefers-color-scheme: dark)");
function systemTheme(){ return mq.matches ? "dark" : "light"; }
function applyTheme(t){
  if (t === "system") t = systemTheme();
  document.documentElement.setAttribute("data-theme", t);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", t === "dark" ? "#16181d" : "#ffffff");
}
function getInitialTheme(){
  try {
    const saved = localStorage.getItem(LS_THEME);
    if (saved === "dark" || saved === "light") return saved;
  } catch(e){}
  return defaultTheme;
}
function toggleTheme(){
  const cur = document.documentElement.getAttribute("data-theme") || "light";
  const next = cur === "dark" ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem(LS_THEME, next); } catch(e){}
}
applyTheme(getInitialTheme());
mq.addEventListener("change", e => {
  let saved = null;
  try { saved = localStorage.getItem(LS_THEME); } catch(e){}
  if (saved !== "dark" && saved !== "light") applyTheme(e.matches ? "dark" : "light");
});

document.addEventListener("contextmenu", e => {
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
  e.preventDefault();
});
document.addEventListener("selectstart", e => {
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
  e.preventDefault();
});
document.addEventListener("dragstart", e => {
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
  e.preventDefault();
});

/* ================= STATE ================= */
let ws = null;
let me = null;
let sessionToken = null;
let sessionPassword = null;
let contacts = [];       // [{uid, online}]
const threads = {};      // uid -> [{id, from, to, text, ts}]
const unread = {};       // uid -> int
const seenIds = new Set();
let activePeer = null;
let reconnectAttempts = 0;
let heartbeatTimer = null;
let mode = "login";
let authPayload = null;
let pendingAdd = null;
let pinned = new Set();
let welcomeShown = false;

const $ = id => document.getElementById(id);
const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
                   : Date.now().toString(36) + Math.random().toString(36).slice(2,10));

let toastTimer = null;
function toast(msg){
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 1800);
}

const LS_SESSION = "sdm_session";
const LS_PINNED  = "sdm_pinned";
function lsSet(k, v){ try { localStorage.setItem(k, v); return true; } catch(e){ return false; } }
function lsGet(k){ try { return localStorage.getItem(k); } catch(e){ return null; } }
function lsDel(k){ try { localStorage.removeItem(k); } catch(e){} }

function pinnedKey(){ return LS_PINNED + "_" + (me ? me.login : "anon"); }
function loadPinned(){
  pinned = new Set();
  const raw = lsGet(pinnedKey());
  if (!raw) return;
  try { const arr = JSON.parse(raw); if (Array.isArray(arr)) arr.forEach(x => pinned.add(x)); } catch(e){}
}
function savePinned(){ lsSet(pinnedKey(), JSON.stringify([...pinned])); }

/* ================= RENDER ================= */
function renderContacts(){
  const box = $("contacts");
  box.innerHTML = "";
  $("chatCount").textContent = contacts.length ? contacts.length + " шт" : "";

  if (!contacts.length){
    const d = document.createElement("div");
    d.className = "empty-list";
    d.innerHTML = "Пока нет чатов.<br>Нажмите <b>+</b> сверху, чтобы добавить пользователя по логину.";
    box.appendChild(d);
    return;
  }

  const sorted = contacts.slice().sort((a,b) => {
    const pa = pinned.has(a.uid) ? 0 : 1;
    const pb = pinned.has(b.uid) ? 0 : 1;
    if (pa !== pb) return pa - pb;
    const ua = unread[a.uid] > 0 ? 0 : 1;
    const ub = unread[b.uid] > 0 ? 0 : 1;
    if (ua !== ub) return ua - ub;
    const la = threads[a.uid] || [], lb = threads[b.uid] || [];
    const ta = la.length ? la[la.length-1].ts : 0;
    const tb = lb.length ? lb[lb.length-1].ts : 0;
    if (ta !== tb) return tb - ta;
    const oa = a.online === true ? 0 : 1, ob = b.online === true ? 0 : 1;
    if (oa !== ob) return oa - ob;
    return a.uid.localeCompare(b.uid);
  });

  for (const c of sorted){
    const isActive = c.uid === activePeer;
    const hasUnread = unread[c.uid] > 0;
    const isPinned = pinned.has(c.uid);

    const el = document.createElement("div");
    el.className = "contact"
      + (isActive ? " active" : "")
      + (hasUnread ? " unread" : "")
      + (isPinned ? " pinned" : "");
    el.onclick = () => selectPeer(c.uid);

    const sl = document.createElement("span");
    sl.className = "status-line "
      + (c.online === true ? "online" : (c.online === false ? "" : "unknown"));

    const info = document.createElement("div");
    info.className = "contact-info";
    const nm = document.createElement("div");
    nm.className = "contact-name";
    nm.textContent = c.uid;
    const pv = document.createElement("div");
    pv.className = "contact-preview";
    const t = threads[c.uid];
    if (t && t.length){
      const last = t[t.length-1];
      const mine = last.from === me.uid;
      pv.textContent = (mine ? "Вы: " : "") + last.text;
    } else {
      pv.textContent = c.online === true ? "в сети"
                    : (c.online === false ? "не в сети" : "статус неизвестен");
    }
    info.append(nm, pv);

    const pb = document.createElement("button");
    pb.className = "pin-btn";
    pb.title = isPinned ? "Открепить" : "Закрепить";
    pb.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M14 4v5c0 1.12.37 2.16 1 3H9c.65-.86 1-1.9 1-3V4h4m3-2H7c-.55 0-1 .45-1 1s.45 1 1 1h1v5c0 1.66-1.34 3-3 3v2h5.97v7l1 1 1-1v-7H19v-2c-1.66 0-3-1.34-3-3V4h1c.55 0 1-.45 1-1s-.45-1-1-1z"/></svg>';
    pb.onclick = (ev) => {
      ev.stopPropagation(); ev.preventDefault();
      togglePin(c.uid);
    };

    el.append(sl, info);

    if (hasUnread){
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = unread[c.uid] > 99 ? "99+" : unread[c.uid];
      el.appendChild(b);
    }
    el.appendChild(pb);

    box.appendChild(el);
  }
}

function togglePin(uid){
  if (pinned.has(uid)) pinned.delete(uid);
  else pinned.add(uid);
  savePinned();
  renderContacts();
  updatePinActiveBtn();
}
function updatePinActiveBtn(){
  const b = $("pinActiveBtn");
  if (!b) return;
  if (activePeer && pinned.has(activePeer)){
    b.style.color = "var(--accent)";
    b.title = "Открепить";
  } else {
    b.style.color = "";
    b.title = "Закрепить";
  }
}

/* ================= SELECT ================= */
function selectPeer(uid){
  activePeer = uid;
  unread[uid] = 0;
  $("peerName").textContent = uid;
  $("chatPane").classList.add("has-chat");
  $("app").classList.add("chat-open");
  renderContacts();
  refreshPeerSub();
  renderThread();
  updatePinActiveBtn();
  if (ws && ws.readyState === 1){
    try { ws.send(pack(T.READ, {u: uid})); } catch(e){}
  }
  setTimeout(() => $("inp").focus(), 60);
}
function goBack(){
  $("app").classList.remove("chat-open");
}
function refreshPeerSub(){
  if (!activePeer) return;
  const c = contacts.find(x => x.uid === activePeer);
  const sub = $("peerSub");
  if (!c){ sub.textContent = ""; return; }
  if (c.online === true){ sub.textContent = "в сети"; sub.className = "peer-sub online"; }
  else if (c.online === false){ sub.textContent = "не в сети"; sub.className = "peer-sub"; }
  else { sub.textContent = "статус неизвестен"; sub.className = "peer-sub"; }
}

/* ================= THREAD ================= */
function fmtTime(ts){
  return new Date(ts).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
}
function fmtDay(ts){
  const d = new Date(ts), t = new Date();
  if (d.toDateString() === t.toDateString()) return "Сегодня";
  const y = new Date(t); y.setDate(y.getDate()-1);
  if (d.toDateString() === y.toDateString()) return "Вчера";
  return d.toLocaleDateString();
}
function buildMsgEl(m){
  const mine = m.from === me.uid;
  const el = document.createElement("div");
  el.className = "m" + (mine ? " me" : "");
  const txt = document.createElement("div");
  txt.textContent = m.text;
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = fmtTime(m.ts);
  el.append(txt, ts);
  return el;
}
function appendMsg(m){
  const box = $("msgs");
  const day = fmtDay(m.ts);
  const seps = box.querySelectorAll(".day-sep");
  if (!seps.length || seps[seps.length-1].textContent !== day){
    const s = document.createElement("div");
    s.className = "day-sep"; s.textContent = day;
    box.appendChild(s);
  }
  box.appendChild(buildMsgEl(m));
  box.scrollTop = box.scrollHeight;
}
function renderThread(){
  const box = $("msgs");
  box.innerHTML = "";
  if (!activePeer) return;
  const list = threads[activePeer] || [];
  let lastDay = "";
  for (const m of list){
    const day = fmtDay(m.ts);
    if (day !== lastDay){
      lastDay = day;
      const s = document.createElement("div");
      s.className = "day-sep"; s.textContent = day;
      box.appendChild(s);
    }
    box.appendChild(buildMsgEl(m));
  }
  box.scrollTop = box.scrollHeight;
}

/* ================= WS ================= */
function connect(){
  return new Promise((resolve, reject) => {
    let url;
    try {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      url = proto + "//" + location.host + "/ws";
    } catch(e){
      const err = new Error("Не удалось вычислить адрес WS: " + e);
      err.code = "WS_URL"; return reject(err);
    }
    try { ws = new WebSocket(url); }
    catch(e){
      const err = new Error("Не удалось открыть WebSocket: " + e);
      err.code = "WS_OPEN"; return reject(err);
    }
    ws.binaryType = "arraybuffer";
    let settled = false;
    const to = setTimeout(() => {
      if (!settled){
        settled = true; try{ ws.close(); }catch(e){}
        const err = new Error("Таймаут подключения");
        err.code = "WS_TIMEOUT"; reject(err);
      }
    }, 10000);

    ws.onopen = () => {
      try { ws.send(pack(mode === "register" ? T.REGISTER : T.AUTH, authPayload)); }
      catch(e){
        if (!settled){ settled = true; clearTimeout(to);
          const err = new Error("Не удалось отправить приветствие: " + e);
          err.code = "WS_SEND"; reject(err); }
      }
    };
    ws.onmessage = async ev => {
      let type, obj;
      try { [type, obj] = unpack(ev.data); }
      catch(e){
        showError("WS_PARSE", "Не удалось разобрать сообщение от сервера", String(e));
        return;
      }
      if (type === T.HELLO){
        if (obj.default_theme && !lsGet(LS_THEME)){
          defaultTheme = obj.default_theme;
          applyTheme(defaultTheme);
        }
        return;
      }
      if (!settled){
        if (type === T.AUTH_OK){ settled = true; clearTimeout(to); resolve(obj); return; }
        if (type === T.ERROR){
          settled = true; clearTimeout(to);
          try{ ws.close(); }catch(e){}
          const err = new Error(obj.m || "Ошибка");
          err.code = obj.c || "AUTH"; err.log = obj.l || "";
          reject(err); return;
        }
      }
      try { await handleFrame(type, obj); }
      catch(e){
        showError(e.code || "HANDLE", e.message || String(e), e.stack || String(e));
      }
    };
    ws.onclose = ev => {
      if (!settled){
        settled = true; clearTimeout(to);
        const err = new Error(
          ev.code === 1008 ? "Соединение отклонено"
          : ev.code === 1009 ? "Сообщение слишком большое"
          : ev.code === 1013 ? "Слишком много подключений"
          : ev.code === 4000 ? "Вошли с другого устройства"
          : "Соединение закрыто (" + ev.code + ")");
        err.code = "WS_CLOSED";
        err.log = "code=" + ev.code + " reason=" + (ev.reason || "");
        reject(err);
      } else if (me){
        if (ev.code === 4000){
          clearInterval(heartbeatTimer);
          showError("REPLACED", "Вход выполнен с другого устройства",
                    "Сессия завершена. Обновите страницу, чтобы войти снова.");
          return;
        }
        onDisconnect(ev.code, ev.reason);
      }
    };
    ws.onerror = () => {
      if (!settled){
        settled = true; clearTimeout(to);
        const err = new Error("Ошибка соединения");
        err.code = "WS_ERROR"; err.log = "url=" + url;
        reject(err);
      }
    };
  });
}

async function handleFrame(type, obj){
  switch(type){
    case T.MSG: onIncomingMsg(obj); break;
    case T.CONTACT_OK: onContactOk(obj); break;
    case T.CONTACT_ADD: onContactAdd(obj); break;
    case T.CHAT_END: onChatEnded(obj.u); break;
    case T.STATUS: {
      const c = contacts.find(x => x.uid === obj.u);
      if (c){ c.online = obj.o; renderContacts(); refreshPeerSub(); }
      break;
    }
    case T.PING: if (ws && ws.readyState === 1) ws.send(pack(T.PONG, {})); break;
    case T.PONG: break;
    case T.ERROR:
      if (pendingAdd){
        $("addErr").textContent = obj.m || "Ошибка";
        setBusy($("addConfirm"), false);
      } else if (obj.c || obj.m){
        showError(obj.c || "SERVER", obj.m || "Ошибка сервера", obj.l || "");
      }
      break;
  }
}

function onContactOk(obj){
  const uid = obj.u;
  const ex = contacts.find(c => c.uid === uid);
  if (ex){ ex.online = obj.o; }
  else contacts.push({uid, online: obj.o});
  renderContacts();
  if (pendingAdd === uid){ closeAddModal(); pendingAdd = null; selectPeer(uid); }
}

function onContactAdd(obj){
  const uid = obj.u;
  if (!uid || uid === me.uid) return;
  const ex = contacts.find(c => c.uid === uid);
  if (ex){
    if (obj.o !== undefined) ex.online = obj.o;
    return;
  }
  contacts.push({uid, online: obj.o === undefined ? true : obj.o});
  renderContacts();
  toast(uid + " добавил(а) вас в контакты");
}

function onIncomingMsg(m){
  if (seenIds.has(m.i)) return;
  seenIds.add(m.i);

  const peer = m.f === me.uid ? m.t : m.f;
  let c = contacts.find(x => x.uid === peer);
  if (!c){
    c = {uid: peer, online: null};
    contacts.push(c);
  }

  const msg = {id: m.i, from: m.f, to: m.t, text: m.x, ts: m.s};
  (threads[peer] = threads[peer] || []).push(msg);
  threads[peer].sort((a,b) => a.ts - b.ts);

  // Только для входящих: сервер прислал абсолютное значение unread
  if (m.f !== me.uid && typeof m.n === "number"){
    if (activePeer === peer){
      unread[peer] = 0;
      if (ws && ws.readyState === 1){
        try { ws.send(pack(T.READ, {u: peer})); } catch(e){}
      }
    } else {
      unread[peer] = m.n;
    }
  }

  if (activePeer === peer){
    appendMsg(msg);
  }
  renderContacts();
}

function onChatEnded(peer){
  if (!peer) return;
  const wasActive = activePeer === peer;
  removeContact(peer);
  pinned.delete(peer);
  savePinned();
  toast(peer + " завершил(а) чат");
  if (wasActive){
    $("chatPane").classList.remove("has-chat");
    $("app").classList.remove("chat-open");
    activePeer = null;
    updatePinActiveBtn();
  }
}
function removeContact(peer){
  const idx = contacts.findIndex(c => c.uid === peer);
  if (idx >= 0) contacts.splice(idx, 1);
  delete threads[peer];
  delete unread[peer];
  renderContacts();
}

function setErr(m){ $("authErr").textContent = m || ""; }
function setNote(m){ $("authNote").textContent = m || ""; }
function setBusy(btn, busy){ btn.disabled = busy; btn.classList.toggle("busy", busy); }

/* ================= AUTH ================= */
async function doAuth(login, password, remember){
  login = (login || "").trim().toLowerCase();
  if (!login || !password){ setErr("Заполните все поля"); return; }
  if (!/^[a-z0-9._-]{3,24}$/.test(login)){ setErr("Логин 3–24: a-z 0-9 . _ -"); return; }
  if (mode === "register" && password.length < 8){ setErr("Пароль минимум 8 символов"); return; }
  setErr(""); setNote("");

  try {
    authPayload = {login, password, remember};
    setBusy($("submitBtn"), true);
    const ok = await connect();
    sessionPassword = password;
    me = {login: ok.login, uid: ok.login};

    if (ok.token){
      sessionToken = ok.token;
      if (remember || mode === "register"){
        lsSet(LS_SESSION, JSON.stringify({login: me.login, token: ok.token}));
      } else {
        lsDel(LS_SESSION);
      }
    }

    loadPinned();

    contacts = (ok.contacts || []).map(c => ({uid: c.u, online: c.o}));
    for (const k of Object.keys(threads)) delete threads[k];
    for (const k of Object.keys(unread)) delete unread[k];
    for (const m of (ok.history || [])){
      const peer = m.f === me.uid ? m.t : m.f;
      seenIds.add(m.i);
      (threads[peer] = threads[peer] || []).push({
        id: m.i, from: m.f, to: m.t, text: m.x, ts: m.s,
      });
    }
    for (const k of Object.keys(threads)){
      threads[k].sort((a,b) => a.ts - b.ts);
    }
    if (ok.unread){
      for (const k of Object.keys(ok.unread)){
        unread[k] = ok.unread[k] | 0;
      }
    }

    $("myLogin").textContent = me.login;
    $("login").style.display = "none";
    $("app").classList.add("on");

    renderContacts();
    startHeartbeat();
    reconnectAttempts = 0;

    const wel = CFG.welcome || "";
    if (wel && !welcomeShown){
      welcomeShown = true;
      $("welcomeBody").textContent = wel;
      $("welcomeModal").classList.add("open");
    }
  } catch(e){
    setErr(e.message || "Ошибка");
    setBusy($("submitBtn"), false);
    showError(e.code || "AUTH", e.message || "Ошибка аутентификации",
              e.log || e.stack || "");
  }
}

/* ================= SEND ================= */
function sendMessage(){
  if (!activePeer || !ws || ws.readyState !== 1) return;
  const inp = $("inp");
  const text = inp.value.trim();
  if (!text) return;
  const id = uuid();
  const now = Date.now();
  const localMsg = {id, from: me.uid, to: activePeer, text, ts: now};
  seenIds.add(id);
  (threads[activePeer] = threads[activePeer] || []).push(localMsg);
  appendMsg(localMsg);
  renderContacts();
  inp.value = "";
  inp.focus();
  ws.send(pack(T.MSG, {i: id, t: activePeer, x: text}));
}

/* ================= END CHAT ================= */
function openEndModal(){
  if (!activePeer) return;
  $("endHint").textContent =
    "Переписка с " + activePeer + " будет удалена у вас и у собеседника. Отменить это действие нельзя.";
  $("endModal").classList.add("open");
}
function closeEndModal(){ $("endModal").classList.remove("open"); }
function confirmEndChat(){
  if (!activePeer) return closeEndModal();
  const peer = activePeer;
  if (ws && ws.readyState === 1){
    try { ws.send(pack(T.CHAT_END, {u: peer})); } catch(e){}
  }
  removeContact(peer);
  pinned.delete(peer);
  savePinned();
  activePeer = null;
  $("chatPane").classList.remove("has-chat");
  $("app").classList.remove("chat-open");
  closeEndModal();
  updatePinActiveBtn();
  toast("Чат завершён");
}

/* ================= ADD ================= */
function openAddModal(){
  $("addInput").value = "";
  $("addErr").textContent = "";
  $("addModal").classList.add("open");
  setTimeout(() => $("addInput").focus(), 40);
}
function closeAddModal(){
  $("addModal").classList.remove("open");
  setBusy($("addConfirm"), false);
  pendingAdd = null;
}
function addContactFromInput(){
  const raw = $("addInput").value.trim().toLowerCase();
  const err = $("addErr");
  err.textContent = "";
  if (!raw){ err.textContent = "Введите логин"; return; }
  if (!/^[a-z0-9._-]{3,24}$/.test(raw)){ err.textContent = "Неверный формат"; return; }
  if (raw === me.login){ err.textContent = "Это ваш логин"; return; }
  if (contacts.find(c => c.uid === raw)){ err.textContent = "Уже добавлен"; return; }
  if (!ws || ws.readyState !== 1){ err.textContent = "Нет соединения"; return; }
  setBusy($("addConfirm"), true);
  pendingAdd = raw;
  ws.send(pack(T.CONTACT_REQ, {u: raw}));
}

/* ================= RECONNECT ================= */
function startHeartbeat(){
  clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => {
    if (ws && ws.readyState === 1) ws.send(pack(T.PING, {}));
  }, 30000);
}
async function onDisconnect(code, reason){
  clearInterval(heartbeatTimer);
  if (reconnectAttempts >= 5){
    showError("WS_LOST", "Соединение потеряно. Перезагрузите страницу.",
              "code=" + code + " reason=" + (reason || ""));
    return;
  }
  reconnectAttempts++;
  if (sessionToken){
    authPayload = {token: sessionToken};
    mode = "login";
  } else if (sessionPassword){
    authPayload = {login: me.login, password: sessionPassword};
    mode = "login";
  } else { return; }
  try {
    const ok = await connect();
    me = {login: ok.login, uid: ok.login};
    contacts = (ok.contacts || []).map(c => ({uid: c.u, online: c.o}));
    for (const k of Object.keys(threads)) delete threads[k];
    for (const k of Object.keys(unread)) delete unread[k];
    for (const m of (ok.history || [])){
      const peer = m.f === me.uid ? m.t : m.f;
      seenIds.add(m.i);
      (threads[peer] = threads[peer] || []).push({
        id: m.i, from: m.f, to: m.t, text: m.x, ts: m.s,
      });
    }
    for (const k of Object.keys(threads)){
      threads[k].sort((a,b) => a.ts - b.ts);
    }
    if (ok.unread){
      for (const k of Object.keys(ok.unread)){
        unread[k] = ok.unread[k] | 0;
      }
    }
    renderContacts();
    if (activePeer && ws.readyState === 1){
      // пользователь всё ещё в чате — обнуляем счётчик для него
      unread[activePeer] = 0;
      try { ws.send(pack(T.READ, {u: activePeer})); } catch(e){}
    }
    startHeartbeat();
    reconnectAttempts = 0;
  } catch(e){
    setTimeout(() => onDisconnect(code, reason), 800 * reconnectAttempts);
  }
}

/* ================= COPY ================= */
async function copyToClipboard(text){
  try { await navigator.clipboard.writeText(text); return true; }
  catch(e){
    try {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); document.execCommand("copy");
      document.body.removeChild(ta);
      return true;
    } catch(e2){ return false; }
  }
}
async function copyMyUid(){
  if (!me) return;
  if (await copyToClipboard(me.login)) toast("Скопировано: " + me.login);
  else toast("Не удалось скопировать");
}
async function copyPeerUid(){
  if (!activePeer) return;
  if (await copyToClipboard(activePeer)) toast("Скопировано: " + activePeer);
  else toast("Не удалось скопировать");
}

/* ================= UI BIND ================= */
function setMode(m){
  mode = m;
  $("tabLogin").classList.toggle("active", m === "login");
  $("tabReg").classList.toggle("active", m === "register");
  $("submitBtn").textContent = m === "login" ? "Войти" : "Создать аккаунт";
  $("pwIn").autocomplete = m === "login" ? "current-password" : "new-password";
  setErr(""); setNote("");
}
$("themeBtn").onclick = toggleTheme;
$("themeBtnLogin").onclick = toggleTheme;

$("tabLogin").onclick = () => setMode("login");
$("tabReg").onclick   = () => setMode("register");
$("authForm").addEventListener("submit", e => {
  e.preventDefault();
  if ($("submitBtn").disabled) return;
  doAuth($("loginIn").value, $("pwIn").value, $("rememberIn").checked);
});

$("logoutBtn").onclick = () => {
  try { if (ws && ws.readyState === 1 && sessionToken) ws.send(pack(T.LOGOUT, {token: sessionToken})); } catch(e){}
  lsDel(LS_SESSION);
  sessionToken = null; sessionPassword = null;
  setTimeout(() => { try { ws && ws.close(); } catch(e){} location.reload(); }, 120);
};

$("addBtn").onclick = openAddModal;
$("addCancel").onclick = closeAddModal;
$("addConfirm").onclick = () => { if (!$("addConfirm").disabled) addContactFromInput(); };
$("addInput").addEventListener("keydown", e => {
  if (e.key === "Enter"){ e.preventDefault(); addContactFromInput(); }
  else if (e.key === "Escape"){ closeAddModal(); }
});
$("addModal").addEventListener("click", e => {
  if (e.target === $("addModal")) closeAddModal();
});

$("sendBtn").onclick = () => sendMessage();
$("inp").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); sendMessage(); }
});
$("backBtn").onclick = goBack;
$("myInfo").onclick = copyMyUid;
$("convPeer").onclick = copyPeerUid;
$("pinActiveBtn").onclick = () => { if (activePeer) togglePin(activePeer); };

$("endChatBtn").onclick = openEndModal;
$("endCancel").onclick = closeEndModal;
$("endConfirm").onclick = confirmEndChat;
$("endModal").addEventListener("click", e => {
  if (e.target === $("endModal")) closeEndModal();
});

$("welcomeClose").onclick = () => $("welcomeModal").classList.remove("open");
$("welcomeModal").addEventListener("click", e => {
  if (e.target === $("welcomeModal")) $("welcomeModal").classList.remove("open");
});

/* ================= AUTOLOGIN ================= */
(function boot(){
  setTimeout(async () => {
    try {
      let sess = null;
      const raw = lsGet(LS_SESSION);
      if (raw) { try { sess = JSON.parse(raw); } catch(e){} }

      if (sess && sess.login && sess.token){
        sessionToken = sess.token;
        mode = "login";
        authPayload = {token: sess.token};
        setBusy($("submitBtn"), true);
        try {
          const ok = await connect();
          me = {login: ok.login, uid: ok.login};
          if (ok.token) sessionToken = ok.token;

          loadPinned();

          contacts = (ok.contacts || []).map(c => ({uid: c.u, online: c.o}));
          for (const k of Object.keys(threads)) delete threads[k];
          for (const k of Object.keys(unread)) delete unread[k];
          for (const m of (ok.history || [])){
            const peer = m.f === me.uid ? m.t : m.f;
            seenIds.add(m.i);
            (threads[peer] = threads[peer] || []).push({
              id: m.i, from: m.f, to: m.t, text: m.x, ts: m.s,
            });
          }
          for (const k of Object.keys(threads)){
            threads[k].sort((a,b) => a.ts - b.ts);
          }
          if (ok.unread){
            for (const k of Object.keys(ok.unread)){
              unread[k] = ok.unread[k] | 0;
            }
          }

          $("myLogin").textContent = me.login;
          $("login").style.display = "none";
          $("app").classList.add("on");
          renderContacts();
          startHeartbeat();

          const wel = CFG.welcome || "";
          if (wel && !welcomeShown){
            welcomeShown = true;
            $("welcomeBody").textContent = wel;
            $("welcomeModal").classList.add("open");
          }
          return;
        } catch(e){
          lsDel(LS_SESSION);
          sessionToken = null;
          setBusy($("submitBtn"), false);
        }
      }
    } catch(e){
      showError(e.code || "BOOT", e.message || "Ошибка загрузки", e.stack || String(e));
    }
    $("loginIn").focus();
  }, 80);
})();

})();
</script>
</body>
</html>
"""


# ============================ РЕНДЕР HTML ============================
HTML_PAGE = (HTML_PAGE
    .replace("{{APP_NAME}}", APP_NAME)
    .replace("{{APP_SUB}}",  APP_SUB)
    .replace("{{CFG_JSON}}", json.dumps({
        "welcome": WELCOME_MESSAGE,
        "default_theme": DEFAULT_THEME,
    }, ensure_ascii=False))
)


# ============================ RUN ============================
def _run():
    if _UVLOOP:
        try:
            import uvloop  # type: ignore
            uvloop.install()
        except Exception as e:
            log_err("uvloop", e)

    print(_c(BOLD, "  sdm-server"))
    print(_c(DIM, f"  {APP_NAME} · {APP_SUB}"))
    print()
    ok = _print_self_test(_self_test())
    if not ok:
        print("  " + _c(RED, "self-test не пройден — сервер не будет запущен"))
        sys.exit(1)

    shown = HOST if HOST != "0.0.0.0" else "localhost"
    print("  " + _c(DIM, "слушает ") + _c(CYAN, f"http://{shown}:{PORT}"))
    print("  " + _c(DIM, "остановка: Ctrl+C"))
    print()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", access_log=False)


if __name__ == "__main__":
    try:
        _run()
    except KeyboardInterrupt:
        print()
    except Exception as e:
        log_err("startup", e)
        sys.exit(1)
