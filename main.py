#!/usr/bin/env python3
"""
SDM — sldshr's direct messanger
E2EE мессенджер 1-на-1. Один файл. Без федерации.

Запуск: python main.py
"""

import os
import sys
import subprocess
import importlib
import time

# ============================ TUI / BOOTSTRAP ============================
# Всё это выполняется ДО импорта FastAPI, чтобы красиво показать проверку зависимостей.

RESET  = "\x1b[0m"
BOLD   = "\x1b[1m"
DIM    = "\x1b[2m"
RED    = "\x1b[31m"
GREEN  = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE   = "\x1b[34m"
MAGENTA= "\x1b[35m"
CYAN   = "\x1b[36m"
WHITE  = "\x1b[97m"
BG_BLUE= "\x1b[44m"

# Принудительный UTF-8 вывод
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


def print_banner():
    print()
    print(_c(CYAN + BOLD, ASCII_SDM))
    print(" " * 3 + _c(DIM + WHITE, "sldshr's direct messanger"))
    print(" " * 3 + _c(DIM, "end-to-end encrypted · 1-on-1 · single file"))
    print()


def print_rule(char: str = "─", width: int = 56):
    print(_c(DIM, " " + char * width))


def print_section(title: str):
    print()
    print(" " + _c(BOLD + BLUE, "▎") + " " + _c(BOLD, title))
    print_rule()


def print_kv(key: str, value: str, note: str = "", ok: bool = True):
    marker = _c(GREEN, "●") if ok else _c(RED, "●")
    k = _c(WHITE, key.ljust(14))
    v = _c(CYAN, value)
    n = "  " + _c(DIM, note) if note else ""
    print(f"  {marker}  {k} {v}{n}")


def print_check(name: str, ok: bool, note: str = ""):
    if ok:
        print(f"  {_c(GREEN, '✓')}  {_c(WHITE, name.ljust(14))} {_c(DIM, note)}")
    else:
        print(f"  {_c(RED, '✗')}  {_c(WHITE, name.ljust(14))} {_c(RED, note)}")


def check_and_install_deps():
    """Проверяет зависимости, предлагает установку."""
    print_section("Проверка зависимостей")

    deps = [
        ("fastapi",  "fastapi",  "веб-фреймворк"),
        ("uvicorn",  "uvicorn",  "ASGI-сервер"),
    ]
    optional = [
        ("uvloop",   "uvloop",   "быстрый event loop (опционально)"),
    ]

    missing = []
    for mod, pkg, note in deps:
        try:
            importlib.import_module(mod)
            print_check(pkg, True, note)
        except ImportError:
            print_check(pkg, False, "не установлен")
            missing.append(pkg)

    for mod, pkg, note in optional:
        try:
            importlib.import_module(mod)
            print_check(pkg, True, note + " · +30% скорости")
        except ImportError:
            print_check(pkg, False, note + " · можно установить позже")

    if missing:
        print()
        print(" " + _c(YELLOW, "Не хватает обязательных библиотек: ") + _c(BOLD, ", ".join(missing)))
        try:
            ans = input(" " + _c(CYAN, "Установить сейчас через pip? ") +
                        _c(DIM, "[Y/n] ") + _c(WHITE, "")).strip().lower()
        except (KeyboardInterrupt, EOFError):
            ans = "n"
            print()
        if ans in ("", "y", "yes", "д", "да"):
            print()
            cmd = [sys.executable, "-m", "pip", "install"] + missing
            print(" " + _c(DIM, "$ " + " ".join(cmd)))
            print()
            try:
                rc = subprocess.call(cmd)
                if rc != 0:
                    print(" " + _c(RED, "pip завершился с ошибкой. Установи вручную и запусти снова."))
                    sys.exit(1)
            except Exception as e:
                print(" " + _c(RED, f"Не удалось выполнить pip: {e}"))
                sys.exit(1)
            print()
            # перезапуск процесса
            print(" " + _c(DIM, "Перезапуск..."))
            time.sleep(0.6)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            print(" " + _c(RED, "Отменено. Установи зависимости вручную и запусти снова."))
            sys.exit(1)


def print_config(host: str, port: int, history_max: int, uvloop_used: bool):
    print_section("Конфигурация сервера")
    print_kv("HOST",      host)
    print_kv("PORT",      str(port))
    print_kv("PAIR_MAX",  str(history_max), "сообщений на пару")
    print_kv("LOOP",      "uvloop" if uvloop_used else "asyncio", "event loop")
    print_kv("STORAGE",   "RAM", "всё в оперативной памяти")
    print_kv("E2EE",      "ECDH P-256 + AES-GCM-256", "ключи только у клиентов")
    print_kv("FEDERATION","off", "каждый сервер автономен")


def print_ready(host: str, port: int):
    print()
    print_rule("━")
    print(" " + _c(BOLD + GREEN, "● Сервер готов"))
    shown_host = host if host != "0.0.0.0" else "localhost"
    print(" " + _c(DIM, "Открой в браузере:"))
    print("   " + _c(BOLD + CYAN + BOLD, f"http://{shown_host}:{port}"))
    print()
    print(" " + _c(DIM, "Ctrl+C — остановить сервер"))
    print_rule("━")
    print()


def print_hints(host: str, port: int, uvloop_used: bool):
    print_section("Подсказки")
    if host == "0.0.0.0":
        print("  " + _c(DIM, "•") + " Сервер слушает все интерфейсы (0.0.0.0)")
        print("  " + _c(DIM, "•") + " В локальной сети доступен по " +
              _c(CYAN, f"http://<твой-ip>:{port}"))
    else:
        print("  " + _c(DIM, "•") + " Сервер слушает только " + _c(CYAN, host))
    if not uvloop_used:
        print("  " + _c(DIM, "•") + " Для +30% скорости: " +
              _c(CYAN, "pip install uvloop") + _c(DIM, " (Linux/macOS)"))
    print("  " + _c(DIM, "•") + " Переменные окружения: " +
          _c(CYAN, "HOST, PORT, PAIR_HISTORY_MAX"))
    print("  " + _c(DIM, "•") + " Пример: " +
          _c(CYAN, "PORT=9000 python main.py"))
    print()


# ---- Прогоняем bootstrap ----
print_banner()

# 1. Проверка зависимостей (может установить и перезапуститься)
check_and_install_deps()

# 2. Теперь можно импортировать всё остальное
import asyncio
import hashlib
import json
import secrets
import struct
import uuid
from collections import deque
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# 3. Читаем конфиг из env
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
PAIR_HISTORY_MAX = int(os.getenv("PAIR_HISTORY_MAX", "200"))
STATUS_HEARTBEAT = 30.0

try:
    import uvloop  # type: ignore
    _UVLOOP = True
except ImportError:
    _UVLOOP = False

# 4. Показываем конфиг
print_config(HOST, PORT, PAIR_HISTORY_MAX, _UVLOOP)
print_hints(HOST, PORT, _UVLOOP)

# ============================ PROTOCOL ============================
(T_REGISTER, T_AUTH, T_AUTH_OK, T_MSG, T_PING, T_PONG, T_ERROR,
 T_HELLO, T_STATUS, T_SYNC, T_CONTACT_REQ, T_CONTACT_OK, T_CONTACT_ADD,
 T_CHAT_END) = range(1, 15)
_HDR = struct.Struct(">BI")


def pack(t: int, o: Any) -> bytes:
    p = json.dumps(o, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _HDR.pack(t, len(p)) + p


def unpack(data: bytes):
    t, ln = _HDR.unpack_from(data, 0)
    return t, json.loads(data[5:5 + ln].decode("utf-8"))


# ============================ STATE ============================
users: dict[str, dict] = {}
online: dict[str, "Client"] = {}
watchers: dict[str, set] = {}
messages: dict[tuple, deque] = {}


def scrypt_hash(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def pair_key(a: str, b: str) -> tuple:
    return (a, b) if a < b else (b, a)


class Client:
    __slots__ = ("ws", "login", "lock")
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.login: Optional[str] = None
        self.lock = asyncio.Lock()

    async def send(self, t: int, o: Any):
        async with self.lock:
            try:
                await self.ws.send_bytes(pack(t, o))
            except Exception:
                pass


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
    login = (obj.get("login") or "").strip().lower()
    pw = obj.get("password") or ""
    pub = obj.get("pub") or ""
    if not login or not pw or not pub:
        return await c.send(T_ERROR, {"m": "Заполните все поля"})
    if not (3 <= len(login) <= 24):
        return await c.send(T_ERROR, {"m": "Логин: 3–24 символа"})
    if not all(ch.isalnum() or ch in "_-." for ch in login):
        return await c.send(T_ERROR, {"m": "Логин: только a-z 0-9 _ - ."})
    if len(pw) < 6:
        return await c.send(T_ERROR, {"m": "Пароль минимум 6 символов"})
    if login in users:
        return await c.send(T_ERROR, {"m": "Логин уже занят"})
    salt = os.urandom(16)
    users[login] = {"salt": salt, "pw": scrypt_hash(pw, salt), "pub": pub, "contacts": {}}
    watchers.setdefault(login, set())
    await _finish_auth(c, login)


async def handle_auth(c: Client, obj: dict):
    login = (obj.get("login") or "").strip().lower()
    pw = obj.get("password") or ""
    pub = (obj.get("pub") or "").strip()
    u = users.get(login)
    if not u or not secrets.compare_digest(u["pw"], scrypt_hash(pw, u["salt"])):
        return await c.send(T_ERROR, {"m": "Неверный логин или пароль"})
    if login in online:
        return await c.send(T_ERROR, {"m": "Вы уже вошли с другого устройства"})
    if pub and pub != u.get("pub"):
        u["pub"] = pub
    await _finish_auth(c, login)


async def _finish_auth(c: Client, login: str):
    c.login = login
    online[login] = c
    u = users[login]

    contacts_payload = [
        {"u": peer, "p": pub, "o": peer in online}
        for peer, pub in u["contacts"].items()
    ]

    hist = []
    for (a, b), dq in messages.items():
        if a == login or b == login:
            hist.extend(dq)
    hist.sort(key=lambda m: m["s"])

    await c.send(T_AUTH_OK, {
        "login": login,
        "contacts": contacts_payload,
        "history": hist,
    })
    await notify_watchers(login, True)


async def handle_msg(c: Client, obj: dict):
    to_login = (obj.get("t") or "").strip().lower()
    blob = obj.get("d") or ""
    if not to_login or not blob:
        return
    if to_login == c.login:
        return await c.send(T_ERROR, {"m": "Нельзя писать самому себе"})
    if to_login not in users:
        return await c.send(T_ERROR, {"m": "Пользователь не найден"})

    from_pub = users[c.login]["pub"]
    mid = obj.get("i") or uuid.uuid4().hex
    msg = {
        "i": mid, "f": c.login, "t": to_login,
        "d": blob, "p": from_pub,
        "s": int(time.time() * 1000),
    }

    key = pair_key(c.login, to_login)
    dq = messages.get(key)
    if dq is None:
        dq = deque(maxlen=PAIR_HISTORY_MAX)
        messages[key] = dq
    dq.append(msg)

    await c.send(T_MSG, msg)
    target = online.get(to_login)
    if target:
        await target.send(T_MSG, msg)


async def handle_contact_req(c: Client, obj: dict):
    peer = (obj.get("u") or "").strip().lower()
    if not peer:
        return await c.send(T_ERROR, {"m": "Пустой логин"})
    if peer == c.login:
        return await c.send(T_ERROR, {"m": "Это ваш собственный логин"})
    u = users.get(peer)
    if not u:
        return await c.send(T_ERROR, {"m": "Пользователь не найден"})

    pub = u["pub"]
    my_pub = users[c.login]["pub"]

    users[c.login]["contacts"][peer] = pub
    users[peer]["contacts"][c.login] = my_pub
    watchers.setdefault(peer, set()).add(c.login)
    watchers.setdefault(c.login, set()).add(peer)

    online_status = peer in online
    await c.send(T_CONTACT_OK, {"u": peer, "p": pub, "o": online_status})

    target = online.get(peer)
    if target:
        await target.send(T_CONTACT_ADD, {"u": c.login, "p": my_pub, "o": True})


async def handle_chat_end(c: Client, obj: dict):
    peer = (obj.get("u") or "").strip().lower()
    if not peer:
        return
    my = c.login

    users[my]["contacts"].pop(peer, None)
    if peer in users:
        users[peer]["contacts"].pop(my, None)

    w_me = watchers.get(my)
    if w_me: w_me.discard(peer)
    w_peer = watchers.get(peer)
    if w_peer: w_peer.discard(my)

    messages.pop(pair_key(my, peer), None)

    target = online.get(peer)
    if target:
        await target.send(T_CHAT_END, {"u": my})


# ============================ APP ============================
app = FastAPI()


@app.get("/")
async def index():
    return HTMLResponse(HTML_PAGE)


@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    c = Client(ws)
    await c.send(T_HELLO, {})
    try:
        while True:
            raw = await ws.receive_bytes()
            try:
                t, obj = unpack(raw)
            except Exception:
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
            elif t == T_PING:
                await c.send(T_PONG, {})
            elif t == T_SYNC and c.login:
                u = users[c.login]
                snapshot = [
                    {"u": peer, "o": peer in online}
                    for peer in u["contacts"].keys()
                ]
                await c.send(T_STATUS, {"snapshot": snapshot})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if c.login and online.get(c.login) is c:
            online.pop(c.login, None)
            await notify_watchers(c.login, False)


# ============================ HTML (client) ============================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>SDM · sldshr's direct messanger</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%235865f2'/%3E%3Cpath d='M20 4H4a1 1 0 0 0-1 1v14l3.5-3.5H20a1 1 0 0 0 1-1V5a1 1 0 0 0-1-1z' fill='white'/%3E%3C/svg%3E">
<style>
:root{
  --bg-primary:#ffffff;--bg-secondary:#f2f3f5;--bg-tertiary:#e3e5e8;
  --bg-hover:#e8eaed;--bg-active:#d7dae0;
  --text-normal:#2e3338;--text-muted:#747f8d;--border:#e3e5e8;
  --accent:#5865f2;--accent-hover:#4752c4;--green:#3ba55d;--red:#ed4245;
  --toast-bg:#2e3338;--toast-fg:#fff;--card-shadow:0 10px 30px rgba(0,0,0,.10);
}
:root[data-theme="dark"]{
  --bg-primary:#313338;--bg-secondary:#2b2d31;--bg-tertiary:#1e1f22;
  --bg-hover:#35373c;--bg-active:#404249;
  --text-normal:#dbdee1;--text-muted:#949ba4;--border:#26272b;
  --accent:#5865f2;--accent-hover:#4752c4;--green:#23a55a;--red:#f23f43;
  --toast-bg:#1e1f22;--toast-fg:#fff;--card-shadow:0 10px 30px rgba(0,0,0,.5);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{
  height:100%;overflow:hidden;overscroll-behavior:none;
  -webkit-user-select:none;-moz-user-select:none;-ms-user-select:none;user-select:none;
  -webkit-touch-callout:none;
}
input,textarea{-webkit-user-select:text;user-select:text}
*{scrollbar-width:none;-ms-overflow-style:none}
*::-webkit-scrollbar{width:0;height:0;display:none}
body{background:var(--bg-primary);color:var(--text-normal);
  font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  transition:background .15s,color .15s}
button{font:inherit;cursor:pointer;color:inherit}
input{font:inherit}

@keyframes spin{to{transform:rotate(360deg)}}
button.busy{position:relative;color:transparent !important}
button.busy::after{content:"";position:absolute;top:50%;left:50%;
  width:18px;height:18px;margin:-9px 0 0 -9px;
  border:2px solid rgba(255,255,255,.35);border-top-color:#fff;
  border-radius:50%;animation:spin .7s linear infinite}
button.btn-secondary.busy::after{border-color:rgba(128,128,128,.2);border-top-color:var(--accent)}

#login{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  padding:20px;background:var(--bg-secondary);z-index:100}
.card{width:100%;max-width:400px;background:var(--bg-primary);border-radius:14px;
  padding:26px;box-shadow:var(--card-shadow);position:relative}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:18px}
.brand-logo{width:42px;height:42px;border-radius:12px;background:var(--accent);
  display:flex;align-items:center;justify-content:center;flex-shrink:0}
.brand-text{display:flex;flex-direction:column;line-height:1.2}
.brand-title{font-weight:700;font-size:16px}
.brand-sub{font-size:11px;color:var(--text-muted)}
.theme-btn{position:absolute;top:14px;right:14px}
.tabs{display:flex;background:var(--bg-secondary);border-radius:9px;padding:3px;margin-bottom:18px}
.tabs button{flex:1;background:none;border:none;color:var(--text-muted);
  padding:9px;border-radius:7px;font-weight:500;transition:all .15s}
.tabs button.active{background:var(--bg-primary);color:var(--text-normal);
  box-shadow:0 1px 2px rgba(0,0,0,.08)}
.card input[type=text],.card input[type=password]{width:100%;background:var(--bg-secondary);
  border:1px solid transparent;color:var(--text-normal);padding:12px 14px;border-radius:8px;
  outline:none;font-size:16px;margin-bottom:10px;transition:border .12s}
.card input[type=text]:focus,.card input[type=password]:focus{border-color:var(--accent)}
.remember{display:flex;align-items:center;gap:8px;font-size:13px;
  color:var(--text-muted);margin:0 2px 12px;cursor:pointer}
.remember input{width:16px;height:16px;margin:0;accent-color:var(--accent);cursor:pointer}
#submitBtn{width:100%;background:var(--accent);color:#fff;border:none;padding:12px;
  border-radius:8px;font-weight:600;margin-top:4px}
#submitBtn:hover:not(:disabled){background:var(--accent-hover)}
#submitBtn:disabled{cursor:default}
#authErr{color:var(--red);font-size:13px;margin-top:10px;min-height:17px;text-align:center}
#authNote{color:var(--text-muted);font-size:12px;margin-top:4px;text-align:center;line-height:1.35}

#app{display:none;height:100dvh}
#app.on{display:flex}
#sidebar{width:320px;flex-shrink:0;background:var(--bg-secondary);
  display:flex;flex-direction:column;border-right:1px solid var(--border);transition:background .15s}
.sidebar-header{display:flex;align-items:center;gap:8px;padding:10px 12px;
  padding-top:calc(10px + env(safe-area-inset-top));
  border-bottom:1px solid var(--border);background:var(--bg-primary)}
.avatar{width:38px;height:38px;border-radius:50%;color:#fff;font-weight:600;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  font-size:15px;text-transform:uppercase}
.my-info{flex:1;min-width:0;cursor:pointer;border-radius:6px;padding:2px 4px;margin:-2px -4px;
  transition:background .12s}
.my-info:hover{background:var(--bg-hover)}
.my-info:active{background:var(--bg-active)}
.my-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:15px}
.my-sub{font-size:12px;color:var(--text-muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.icon-btn{background:none;border:none;color:var(--text-muted);padding:7px;
  border-radius:8px;display:flex;align-items:center;justify-content:center;
  transition:background .12s,color .12s;flex-shrink:0}
.icon-btn:hover{background:var(--bg-hover);color:var(--text-normal)}
.icon-btn.danger{color:var(--red)}
.icon-btn.danger:hover{background:rgba(237,66,69,.12);color:var(--red)}

.sidebar-title{padding:14px 16px 6px;font-size:11px;font-weight:700;
  color:var(--text-muted);text-transform:uppercase;letter-spacing:.02em}
#contacts{flex:1;overflow-y:auto;padding:0 8px 8px}
.empty-list{padding:32px 22px;text-align:center;color:var(--text-muted);
  font-size:13px;line-height:1.55}
.contact{display:flex;align-items:center;gap:11px;padding:8px 10px;
  border-radius:8px;cursor:pointer;transition:background .1s}
.contact:hover{background:var(--bg-hover)}
.contact.active{background:var(--bg-active)}
.avatar-wrap{position:relative;flex-shrink:0}
.status-dot{position:absolute;right:-2px;bottom:-2px;width:14px;height:14px;
  border-radius:50%;background:#b9bbbe;border:3px solid var(--bg-secondary)}
.status-dot.online{background:var(--green)}
.status-dot.unknown{background:#c7ccd1}
.contact.active .status-dot{border-color:var(--bg-active)}
.contact-info{flex:1;min-width:0}
.contact-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;color:var(--text-normal)}
.contact-preview{font-size:13px;color:var(--text-muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.contact.unread .contact-preview{color:var(--text-normal);font-weight:500}
.badge{background:var(--red);color:#fff;font-size:12px;font-weight:600;
  min-width:20px;height:20px;padding:0 7px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;line-height:1}

#chatPane{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg-primary)}
#emptyState{flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;color:var(--text-muted);gap:14px;padding:24px;text-align:center}
#emptyState svg{opacity:.28}
#emptyState p{font-size:14px}
#conversation{display:none;flex-direction:column;flex:1;min-height:0}
#chatPane.has-chat #conversation{display:flex}
#chatPane.has-chat #emptyState{display:none}
.conv-header{display:flex;align-items:center;gap:8px;padding:10px 14px;
  padding-top:calc(10px + env(safe-area-inset-top));
  border-bottom:1px solid var(--border);background:var(--bg-primary)}
#backBtn{display:none}
.conv-peer{display:flex;flex-direction:column;min-width:0;flex:1;
  cursor:pointer;border-radius:6px;padding:3px 6px;margin:-3px -6px;transition:background .12s}
.conv-peer:hover{background:var(--bg-hover)}
.conv-peer:active{background:var(--bg-active)}
.peer-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.peer-sub{font-size:12px;color:var(--text-muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.peer-sub.online{color:var(--green)}
#msgs{flex:1;overflow-y:auto;padding:16px 16px 6px;display:flex;
  flex-direction:column;gap:4px;scroll-behavior:smooth}
.m{max-width:70%;padding:8px 13px;border-radius:16px;background:var(--bg-secondary);
  align-self:flex-start;word-wrap:break-word;overflow-wrap:anywhere;
  animation:pop .13s ease-out;font-size:15px;line-height:1.4}
@keyframes pop{from{opacity:.4;transform:translateY(3px)}to{opacity:1;transform:none}}
.m.me{align-self:flex-end;background:var(--accent);color:#fff}
.m .ts{font-size:11px;opacity:.65;margin-top:2px;display:block;text-align:right}
.m.me .ts{opacity:.85}
.m.broken{background:transparent;border:1px dashed var(--border);
  color:var(--text-muted);font-style:italic}
.day-sep{align-self:center;font-size:11px;color:var(--text-muted);
  padding:6px 12px;background:var(--bg-secondary);border-radius:10px;margin:8px 0 4px}
#composer{display:flex;align-items:center;gap:8px;padding:10px 14px;
  padding-bottom:calc(10px + env(safe-area-inset-bottom));
  border-top:1px solid var(--border);background:var(--bg-primary)}
#inp{flex:1;background:var(--bg-secondary);border:1px solid transparent;
  color:var(--text-normal);padding:11px 16px;border-radius:20px;outline:none;
  font-size:16px;min-width:0;transition:border .12s}
#inp:focus{border-color:var(--accent)}
#sendBtn{width:42px;height:42px;border-radius:50%;background:var(--accent);
  color:#fff;border:none;display:flex;align-items:center;justify-content:center;
  flex-shrink:0;transition:background .12s,transform .06s}
#sendBtn:hover{background:var(--accent-hover)}
#sendBtn:active{transform:scale(.94)}

.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.5);
  display:none;align-items:center;justify-content:center;padding:20px;z-index:200}
.modal-backdrop.open{display:flex}
.modal{background:var(--bg-primary);border-radius:14px;padding:22px;width:100%;max-width:420px;
  box-shadow:var(--card-shadow)}
.modal h3{font-size:18px;margin-bottom:6px;font-weight:700}
.modal .hint{color:var(--text-muted);font-size:13px;margin-bottom:14px;line-height:1.5}
.modal input{width:100%;padding:12px 14px;border-radius:8px;
  border:1px solid var(--border);background:var(--bg-secondary);
  font-size:16px;outline:none;margin-bottom:10px;color:var(--text-normal)}
.modal input:focus{border-color:var(--accent)}
.modal .err{color:var(--red);font-size:13px;min-height:18px;margin-bottom:6px}
.modal-actions{display:flex;gap:8px;justify-content:flex-end}
.btn-secondary{background:var(--bg-secondary);border:none;padding:10px 16px;
  border-radius:8px;font-weight:500;color:var(--text-normal);transition:background .12s}
.btn-secondary:hover{background:var(--bg-hover)}
.btn-primary{background:var(--accent);color:#fff;border:none;
  padding:10px 22px;border-radius:8px;font-weight:600;transition:background .12s}
.btn-primary:hover:not(:disabled){background:var(--accent-hover)}
.btn-danger{background:var(--red);color:#fff;border:none;
  padding:10px 22px;border-radius:8px;font-weight:600;transition:opacity .12s}
.btn-danger:hover{opacity:.88}
.btn-primary:disabled,.btn-secondary:disabled,.btn-danger:disabled{cursor:default;opacity:.6}

#toast{position:fixed;left:50%;bottom:40px;transform:translateX(-50%) translateY(20px);
  background:var(--toast-bg);color:var(--toast-fg);padding:11px 18px;border-radius:8px;
  font-size:14px;opacity:0;pointer-events:none;transition:opacity .2s, transform .2s;z-index:300;
  box-shadow:0 8px 24px rgba(0,0,0,.25);max-width:80%;text-align:center;
  word-break:break-all;line-height:1.4}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

@media (max-width:720px){
  #app{position:relative;overflow:hidden}
  #sidebar{position:absolute;inset:0;width:100%;z-index:2;
    border-right:none;transition:transform .26s ease}
  #chatPane{position:absolute;inset:0;z-index:3;transform:translateX(100%);
    transition:transform .26s ease;box-shadow:-8px 0 24px rgba(0,0,0,.10)}
  #app.chat-open #sidebar{transform:translateX(-26%)}
  #app.chat-open #chatPane{transform:translateX(0)}
  #backBtn{display:flex}
  .m{max-width:86%}
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
          <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM7 9h10v2H7V9zm6 5H7v-2h6v2zm4-6H7V6h10v2z"/>
        </svg>
      </div>
      <div class="brand-text">
        <div class="brand-title">SDM</div>
        <div class="brand-sub">sldshr's direct messanger</div>
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
      <div class="avatar" id="myAvatar">?</div>
      <div class="my-info" id="myInfo" title="Нажмите, чтобы скопировать логин">
        <div class="my-name" id="myLogin">—</div>
        <div class="my-sub" id="myDomain">в сети</div>
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
    <div class="sidebar-title">Чаты</div>
    <div id="contacts"></div>
  </aside>

  <section id="chatPane">
    <div id="emptyState">
      <svg width="90" height="90" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM7 9h10v2H7V9zm6 5H7v-2h6v2zm4-6H7V6h10v2z"/>
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

<div id="toast"></div>

<script>
(() => {
"use strict";

const T = {REGISTER:1, AUTH:2, AUTH_OK:3, MSG:4, PING:5, PONG:6, ERROR:7,
           HELLO:8, STATUS:9, SYNC:10, CONTACT_REQ:11, CONTACT_OK:12,
           CONTACT_ADD:13, CHAT_END:14};
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

function b64(u8){ let s=""; for(let i=0;i<u8.length;i++) s+=String.fromCharCode(u8[i]); return btoa(s); }
function b64url(u8){ return b64(u8).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,""); }
function unb64(s){ const b=atob(s); const u=new Uint8Array(b.length); for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i); return u; }
function unb64url(s){
  s = s.replace(/-/g,"+").replace(/_/g,"/");
  while (s.length % 4) s += "=";
  return unb64(s);
}
function jwkToRawPub(jwk){
  if (!jwk || !jwk.x || !jwk.y) return null;
  const x = unb64url(jwk.x), y = unb64url(jwk.y);
  if (x.length !== 32 || y.length !== 32) return null;
  const raw = new Uint8Array(65);
  raw[0] = 4; raw.set(x,1); raw.set(y,33);
  return b64(raw);
}

async function genKeyPair(){ return crypto.subtle.generateKey({name:"ECDH",namedCurve:"P-256"},true,["deriveKey"]); }
async function exportPubRaw(pub){ return b64(new Uint8Array(await crypto.subtle.exportKey("raw", pub))); }
async function exportPrivJWK(priv){ return await crypto.subtle.exportKey("jwk", priv); }
async function importPrivJWK(jwk){ return crypto.subtle.importKey("jwk",jwk,{name:"ECDH",namedCurve:"P-256"},true,["deriveKey"]); }
async function importPubRaw(str){ return crypto.subtle.importKey("raw",unb64(str),{name:"ECDH",namedCurve:"P-256"},true,[]); }
async function deriveAesKey(priv, pub){
  return crypto.subtle.deriveKey({name:"ECDH",public:pub},priv,
    {name:"AES-GCM",length:256},false,["encrypt","decrypt"]);
}
async function encryptBlob(key, text){
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const buf = await crypto.subtle.encrypt({name:"AES-GCM",iv}, key, _enc.encode(text));
  const all = new Uint8Array(12 + buf.byteLength);
  all.set(iv, 0); all.set(new Uint8Array(buf), 12);
  return b64url(all);
}
async function decryptBlob(key, blob){
  const all = unb64url(blob);
  const iv = all.slice(0, 12);
  const ct = all.slice(12);
  const pt = await crypto.subtle.decrypt({name:"AES-GCM",iv}, key, ct);
  return _dec.decode(pt);
}

const LS_THEME = "sdm_theme";
const mq = window.matchMedia("(prefers-color-scheme: dark)");
function systemTheme(){ return mq.matches ? "dark" : "light"; }
function applyTheme(t){
  document.documentElement.setAttribute("data-theme", t);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", t === "dark" ? "#313338" : "#ffffff");
}
function getInitialTheme(){
  try {
    const saved = localStorage.getItem(LS_THEME);
    if (saved === "dark" || saved === "light") return saved;
  } catch(e){}
  return systemTheme();
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
  if (saved !== "dark" && saved !== "light"){
    applyTheme(e.matches ? "dark" : "light");
  }
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

let ws = null;
let me = null;
let myPrivKey = null;
let myPubRaw = null;
let sessionPassword = null;
let contacts = [];
const convKeys = {};
const threads = {};
const unread = {};
const seenIds = new Set();
let activePeer = null;
let reconnectAttempts = 0;
let heartbeatTimer = null;
let mode = "login";
let authPayload = null;
let pendingAdd = null;

const $ = id => document.getElementById(id);
const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
                   : Date.now().toString(36) + Math.random().toString(36).slice(2,10));

function avatarColor(uid){
  const colors = ["#5865f2","#3ba55d","#faa61a","#ed4245","#eb459e","#9b59b6","#1abc9c","#e67e22"];
  let h = 0;
  for (let i=0;i<uid.length;i++) h = (h * 31 + uid.charCodeAt(i)) | 0;
  return colors[Math.abs(h) % colors.length];
}
function avatarChar(uid){ return (uid || "?")[0].toUpperCase(); }

let toastTimer = null;
function toast(msg){
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 1800);
}

const LS_REMEMBER = "sdm_remember";
function privStoreKey(login){ return "sdm_priv_" + login + "@" + location.host; }
function privStoreKeyOld(login){ return "sdm_priv_" + login; }

function renderContacts(){
  const box = $("contacts");
  box.innerHTML = "";
  if (!contacts.length){
    const d = document.createElement("div");
    d.className = "empty-list";
    d.innerHTML = "Пока нет чатов.<br>Нажмите <b>+</b> сверху, чтобы добавить пользователя по логину.";
    box.appendChild(d);
    return;
  }
  const sorted = contacts.slice().sort((a,b) => {
    const ua = unread[a.uid] > 0 ? 1 : 0;
    const ub = unread[b.uid] > 0 ? 1 : 0;
    if (ua !== ub) return ub - ua;
    const la = threads[a.uid] || [], lb = threads[b.uid] || [];
    const ta = la.length ? la[la.length-1].ts : 0;
    const tb = lb.length ? lb[lb.length-1].ts : 0;
    if (ta !== tb) return tb - ta;
    const oa = a.online === true ? 1 : 0, ob = b.online === true ? 1 : 0;
    if (oa !== ob) return ob - oa;
    return a.uid.localeCompare(b.uid);
  });
  for (const c of sorted){
    const isActive = c.uid === activePeer;
    const hasUnread = unread[c.uid] > 0;
    const el = document.createElement("div");
    el.className = "contact" + (isActive ? " active" : "") + (hasUnread ? " unread" : "");
    el.onclick = () => selectPeer(c.uid);

    const wrap = document.createElement("div");
    wrap.className = "avatar-wrap";
    const av = document.createElement("div");
    av.className = "avatar";
    av.style.background = avatarColor(c.uid);
    av.textContent = avatarChar(c.uid);
    const dot = document.createElement("span");
    dot.className = "status-dot " + (c.online === true ? "online" : (c.online === false ? "" : "unknown"));
    wrap.append(av, dot);

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
      pv.textContent = (mine ? "Вы: " : "") + (last.broken ? "⚠ зашифровано" : last.text);
    } else {
      pv.textContent = c.online === true ? "в сети"
                    : (c.online === false ? "не в сети" : "статус неизвестен");
    }
    info.append(nm, pv);
    el.append(wrap, info);
    if (hasUnread){
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = unread[c.uid] > 99 ? "99+" : unread[c.uid];
      el.appendChild(b);
    }
    box.appendChild(el);
  }
}

function selectPeer(uid){
  activePeer = uid;
  unread[uid] = 0;
  $("peerName").textContent = uid;
  $("chatPane").classList.add("has-chat");
  $("app").classList.add("chat-open");
  renderContacts();
  refreshPeerSub();
  renderThread();
  setTimeout(() => $("inp").focus(), 60);
}
function goBack(){ $("app").classList.remove("chat-open"); }

function refreshPeerSub(){
  if (!activePeer) return;
  const c = contacts.find(x => x.uid === activePeer);
  const sub = $("peerSub");
  if (!c){ sub.textContent = ""; return; }
  if (c.online === true){ sub.textContent = "в сети"; sub.className = "peer-sub online"; }
  else if (c.online === false){ sub.textContent = "не в сети"; sub.className = "peer-sub"; }
  else { sub.textContent = "статус неизвестен"; sub.className = "peer-sub"; }
}

function fmtTime(ts){ return new Date(ts).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}); }
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
  el.className = "m" + (mine ? " me" : "") + (m.broken ? " broken" : "");
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

async function getConvKeyFromPub(uid, pub){
  if (!pub) return null;
  const cacheKey = uid + "|" + pub.slice(0, 24);
  if (convKeys[cacheKey]) return convKeys[cacheKey];
  try {
    const p = await importPubRaw(pub);
    const k = await deriveAesKey(myPrivKey, p);
    convKeys[cacheKey] = k;
    return k;
  } catch(e){ return null; }
}

async function decryptIncoming(m, peer){
  const pubs = [];
  if (m.p) pubs.push(m.p);
  const c = contacts.find(x => x.uid === peer);
  if (c && c.pub && c.pub !== m.p) pubs.push(c.pub);
  for (const pub of pubs){
    const key = await getConvKeyFromPub(peer, pub);
    if (!key) continue;
    try {
      const text = await decryptBlob(key, m.d);
      if (c && m.p && c.pub !== m.p) c.pub = m.p;
      return text;
    } catch(e){}
  }
  return null;
}

function connect(){
  return new Promise((resolve, reject) => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.binaryType = "arraybuffer";
    let settled = false;
    const to = setTimeout(() => {
      if (!settled){ settled = true; try{ws.close();}catch(e){}; reject(new Error("Таймаут")); }
    }, 10000);
    ws.onopen = () => ws.send(pack(mode === "register" ? T.REGISTER : T.AUTH, authPayload));
    ws.onmessage = async ev => {
      let type, obj;
      try { [type, obj] = unpack(ev.data); } catch(e){ return; }
      if (type === T.HELLO) return;
      if (!settled){
        if (type === T.AUTH_OK){ settled = true; clearTimeout(to); resolve(obj); return; }
        if (type === T.ERROR){ settled = true; clearTimeout(to);
          try{ws.close();}catch(e){}; reject(new Error(obj.m || "Ошибка")); return; }
      }
      await handleFrame(type, obj);
    };
    ws.onclose = () => {
      if (!settled){ settled = true; clearTimeout(to); reject(new Error("Соединение закрыто")); }
      else if (me) onDisconnect();
    };
    ws.onerror = () => {
      if (!settled){ settled = true; clearTimeout(to); reject(new Error("Ошибка соединения")); }
    };
  });
}

async function handleFrame(type, obj){
  switch(type){
    case T.MSG: await onIncomingMsg(obj); break;
    case T.CONTACT_OK: onContactOk(obj); break;
    case T.CONTACT_ADD: onContactAdd(obj); break;
    case T.CHAT_END: onChatEnded(obj.u); break;
    case T.STATUS:
      if (obj.snapshot){
        for (const it of obj.snapshot){
          const c = contacts.find(x => x.uid === it.u);
          if (c) c.online = it.o;
        }
        renderContacts();
        refreshPeerSub();
      } else {
        const c = contacts.find(x => x.uid === obj.u);
        if (c){ c.online = obj.o; renderContacts(); refreshPeerSub(); }
      }
      break;
    case T.PING: if (ws && ws.readyState === 1) ws.send(pack(T.PONG, {})); break;
    case T.PONG: break;
    case T.ERROR: {
      if (pendingAdd){ $("addErr").textContent = obj.m || "Ошибка"; setBusy($("addConfirm"), false); }
      console.warn("server:", obj.m);
      break;
    }
  }
}

function onContactOk(obj){
  const uid = obj.u;
  const ex = contacts.find(c => c.uid === uid);
  if (ex){ ex.pub = obj.p || ex.pub; ex.online = obj.o; }
  else contacts.push({uid, pub: obj.p || "", online: obj.o});
  renderContacts();
  if (pendingAdd === uid){ closeAddModal(); pendingAdd = null; selectPeer(uid); }
}

function onContactAdd(obj){
  const uid = obj.u;
  if (!uid || uid === me.uid) return;
  const ex = contacts.find(c => c.uid === uid);
  if (ex){
    if (obj.p && ex.pub !== obj.p) ex.pub = obj.p;
    if (obj.o !== undefined) ex.online = obj.o;
    return;
  }
  contacts.push({uid, pub: obj.p || "", online: obj.o === undefined ? true : obj.o});
  renderContacts();
  toast(uid + " добавил(а) вас в контакты");
}

async function onIncomingMsg(m){
  if (seenIds.has(m.i)) return;
  seenIds.add(m.i);
  const peer = m.f === me.uid ? m.t : m.f;

  let c = contacts.find(x => x.uid === peer);
  if (!c){
    c = {uid: peer, pub: m.p || "", online: null};
    contacts.push(c);
    renderContacts();
  } else if (m.f !== me.uid && m.p && c.pub !== m.p){
    c.pub = m.p;
  }

  const text = await decryptIncoming(m, peer);
  const msg = {
    id: m.i, from: m.f, to: m.t,
    text: text !== null ? text : "⚠ не удалось расшифровать",
    ts: m.s, broken: text === null,
  };
  (threads[peer] = threads[peer] || []).push(msg);
  if (activePeer === peer){
    appendMsg(msg);
    renderContacts();
  } else {
    unread[peer] = (unread[peer] || 0) + 1;
    renderContacts();
  }
}

function onChatEnded(peer){
  if (!peer) return;
  const wasActive = activePeer === peer;
  removeContact(peer);
  toast(peer + " завершил(а) чат");
  if (wasActive){
    $("chatPane").classList.remove("has-chat");
    $("app").classList.remove("chat-open");
    activePeer = null;
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

async function doAuth(login, password, remember){
  login = (login || "").trim().toLowerCase();
  if (!login || !password){ setErr("Заполните все поля"); return; }
  if (!/^[a-z0-9._-]{3,24}$/.test(login)){ setErr("Логин 3–24: a-z 0-9 . _ -"); return; }
  if (mode === "register" && password.length < 6){ setErr("Пароль минимум 6 символов"); return; }
  setErr(""); setNote("");

  try {
    if (mode === "register"){
      const kp = await genKeyPair();
      myPrivKey = kp.privateKey;
      const jwk = await exportPrivJWK(kp.privateKey);
      myPubRaw = await exportPubRaw(kp.publicKey);
      try {
        localStorage.setItem(privStoreKey(login), JSON.stringify(jwk));
        localStorage.removeItem(privStoreKeyOld(login));
      } catch(e){}
      authPayload = {login, password, pub: myPubRaw};
    } else {
      let saved = null;
      try {
        saved = localStorage.getItem(privStoreKey(login))
             || localStorage.getItem(privStoreKeyOld(login));
      } catch(e){}
      if (saved){
        try {
          const jwk = JSON.parse(saved);
          myPrivKey = await importPrivJWK(jwk);
          myPubRaw = jwkToRawPub(jwk);
        } catch(e){ setNote("⚠ Ключ повреждён."); }
      }
      if (!myPrivKey){
        setNote("⚠ Приватный ключ не найден — старые сообщения не расшифруются.");
        const kp = await genKeyPair();
        myPrivKey = kp.privateKey;
        const jwk = await exportPrivJWK(kp.privateKey);
        myPubRaw = await exportPubRaw(kp.publicKey);
        try { localStorage.setItem(privStoreKey(login), JSON.stringify(jwk)); } catch(e){}
      }
      authPayload = {login, password};
      if (myPubRaw) authPayload.pub = myPubRaw;
    }

    setBusy($("submitBtn"), true);
    const ok = await connect();
    sessionPassword = password;
    me = {login: ok.login, uid: ok.login};

    if (remember){
      try { localStorage.setItem(LS_REMEMBER, JSON.stringify({login: me.login, password})); }
      catch(e){}
    } else {
      try { localStorage.removeItem(LS_REMEMBER); } catch(e){}
    }

    contacts = (ok.contacts || []).map(c => ({uid: c.u, pub: c.p, online: c.o}));
    for (const k of Object.keys(threads)) delete threads[k];
    for (const m of (ok.history || [])){
      const peer = m.f === me.uid ? m.t : m.f;
      seenIds.add(m.i);
      const text = await decryptIncoming(m, peer);
      (threads[peer] = threads[peer] || []).push({
        id: m.i, from: m.f, to: m.t,
        text: text !== null ? text : "⚠ не удалось расшифровать",
        ts: m.s, broken: text === null,
      });
    }

    $("myLogin").textContent = me.login;
    $("myDomain").textContent = "в сети";
    $("myAvatar").textContent = avatarChar(me.login);
    $("myAvatar").style.background = avatarColor(me.uid);
    $("login").style.display = "none";
    $("app").classList.add("on");

    renderContacts();
    startHeartbeat();
    reconnectAttempts = 0;
  } catch(e){
    setErr(e.message || "Ошибка");
    setBusy($("submitBtn"), false);
  }
}

async function sendMessage(){
  if (!activePeer || !ws || ws.readyState !== 1) return;
  const inp = $("inp");
  const text = inp.value.trim();
  if (!text) return;
  const c = contacts.find(x => x.uid === activePeer);
  if (!c || !c.pub){ alert("Нет ключа получателя"); return; }
  const key = await getConvKeyFromPub(activePeer, c.pub);
  if (!key){ alert("Нет ключа получателя"); return; }
  const blob = await encryptBlob(key, text);
  const id = uuid();
  const localMsg = {id, from: me.uid, to: activePeer, text, ts: Date.now()};
  seenIds.add(id);
  (threads[activePeer] = threads[activePeer] || []).push(localMsg);
  appendMsg(localMsg);
  renderContacts();
  inp.value = "";
  inp.focus();
  ws.send(pack(T.MSG, {i: id, t: activePeer, d: blob}));
}

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
  if (ws && ws.readyState === 1) ws.send(pack(T.CHAT_END, {u: peer}));
  removeContact(peer);
  activePeer = null;
  $("chatPane").classList.remove("has-chat");
  $("app").classList.remove("chat-open");
  closeEndModal();
  toast("Чат завершён");
}

function openAddModal(){
  $("addInput").value = "";
  $("addErr").textContent = "";
  $("addModal").classList.add("open");
  setTimeout(() => $("addInput").focus(), 40);
}
function closeAddModal(){ $("addModal").classList.remove("open"); setBusy($("addConfirm"), false); pendingAdd = null; }

function addContactFromInput(){
  const raw = $("addInput").value.trim().toLowerCase();
  const err = $("addErr");
  err.textContent = "";
  if (!raw){ err.textContent = "Введите логин"; return; }
  if (!/^[a-z0-9._-]{3,24}$/.test(raw)){ err.textContent = "Неверный формат логина"; return; }
  if (raw === me.login){ err.textContent = "Это ваш собственный логин"; return; }
  if (contacts.find(c => c.uid === raw)){ err.textContent = "Уже добавлен"; return; }
  if (!ws || ws.readyState !== 1){ err.textContent = "Нет соединения"; return; }
  setBusy($("addConfirm"), true);
  pendingAdd = raw;
  ws.send(pack(T.CONTACT_REQ, {u: raw}));
}

function startHeartbeat(){
  clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => {
    if (ws && ws.readyState === 1) ws.send(pack(T.PING, {}));
  }, 30000);
}
async function onDisconnect(){
  clearInterval(heartbeatTimer);
  if (!sessionPassword) return;
  if (reconnectAttempts >= 5){ alert("Соединение потеряно. Обновите страницу."); return; }
  reconnectAttempts++;
  try {
    authPayload = {login: me.login, password: sessionPassword};
    if (myPubRaw) authPayload.pub = myPubRaw;
    mode = "login";
    const ok = await connect();
    me = {login: ok.login, uid: ok.login};
    contacts = (ok.contacts || []).map(c => ({uid: c.u, pub: c.p, online: c.o}));
    renderContacts();
    startHeartbeat();
    reconnectAttempts = 0;
  } catch(e){
    setTimeout(onDisconnect, 800 * reconnectAttempts);
  }
}

async function copyToClipboard(text){
  try { await navigator.clipboard.writeText(text); return true; }
  catch(e){
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      document.execCommand("copy");
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
  sessionPassword = null;
  try { localStorage.removeItem(LS_REMEMBER); } catch(e){}
  try { ws && ws.close(); } catch(e){}
  location.reload();
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

$("endChatBtn").onclick = openEndModal;
$("endCancel").onclick = closeEndModal;
$("endConfirm").onclick = confirmEndChat;
$("endModal").addEventListener("click", e => {
  if (e.target === $("endModal")) closeEndModal();
});

(function boot(){
  setTimeout(() => {
    let auto = null;
    try {
      const raw = localStorage.getItem(LS_REMEMBER);
      if (raw) auto = JSON.parse(raw);
    } catch(e){}
    if (auto && auto.login && auto.password){
      $("loginIn").value = auto.login;
      $("pwIn").value = auto.password;
      $("rememberIn").checked = true;
      setTimeout(() => {
        if (!$("submitBtn").disabled){
          doAuth(auto.login, auto.password, true).catch(() => {
            try { localStorage.removeItem(LS_REMEMBER); } catch(e){}
          });
        }
      }, 250);
    } else {
      $("loginIn").focus();
    }
  }, 80);
})();

})();
</script>
</body>
</html>
"""


# ============================ RUN ============================
def _run():
    print_ready(HOST, PORT)
    try:
        if _UVLOOP:
            import uvloop  # type: ignore
            uvloop.install()
            uvicorn.run(app, host=HOST, port=PORT, log_level="warning", access_log=False)
        else:
            uvicorn.run(app, host=HOST, port=PORT, log_level="warning", access_log=False)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        print(" " + _c(YELLOW, "●") + " " + _c(DIM, "Сервер остановлен"))
        print()


if __name__ == "__main__":
    _run()
