# -*- coding: utf-8 -*-
"""
DSH Messenger — мессенджер с каналами в одном файле.
Стек: Python 3.9+ / FastAPI / Uvicorn.
Все данные хранятся В ОПЕРАТИВНОЙ ПАМЯТИ (теряются при перезапуске сервера).
UI: тёмная тема в духе Twitter Bootstrap 1.4.0 (topbar, hero-unit,
градиентные кнопки .btn.primary/.danger/..., alert-message, zebra-striped,
pills-табы, modal) — без Bootstrap 5 и без Tailwind.

Запуск:  python main.py   →  http://127.0.0.1:8000
"""

import hashlib
import os
import re
import secrets
import sys
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

import uvicorn

APP_VERSION = "1.0.0"
START_TIME = time.time()

# ======================================================================
#  ХРАНИЛИЩЕ В ОПЕРАТИВКЕ
# ======================================================================
STATE = {
    "users": {},        # id -> user
    "tokens": {},       # token -> user_id
    "channels": {},     # id -> channel
    "messages": {},     # channel_id -> [message]
    "events": {},       # user_id -> [event]  (последние события для клиента)
    "typing": {},       # channel_id -> {user_id: ts}
    "bans": {},         # username_lower -> {"until": ts, "reason": str}
    "rate": {},         # user_id -> [ts сообщений]
    "seq_ch": 0,        # счётчик id каналов
    "seq_msg": 0,       # счётчик id сообщений
    "seq_evt": 0,       # счётчик id событий
    "owner_id": None,
    "settings": {
        "name": "DSH Messenger",
        "motd": "Добро пожаловать! Это мессенджер, живущий в оперативной памяти.",
        "allow_registration": True,
        "maintenance_mode": False,
        "max_message_length": 2000,
        "history_limit": 500,          # сколько сообщений хранить на канал
        "rate_limit": 25,              # сообщений в минуту на пользователя
        "allow_editing": True,
        "allow_reactions": True,
        "allow_dm": True,
    },
}

ROLE_LEVEL = {"user": 0, "mod": 1, "admin": 2, "owner": 3}
ROLE_NAMES = {"owner": "Владелец", "admin": "Админ", "mod": "Модер", "user": "Пользователь"}
PALETTE = ["#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#1abc9c", "#3498db",
           "#9b59b6", "#e84393", "#fd79a8", "#00cec9", "#6c5ce7", "#fdcb6e"]

# ======================================================================
#  ХЕЛПЕРЫ
# ======================================================================
def now() -> float:
    return time.time()

def fmt_time(ts: float) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))

def sha256(text: str, salt: str = "") -> str:
    return hashlib.sha256((salt + text).encode("utf-8")).hexdigest()

def next_channel_id() -> int:
    STATE["seq_ch"] += 1
    return STATE["seq_ch"]

def role_level(role: str) -> int:
    return ROLE_LEVEL.get(role, 0)

def is_online(u: dict) -> bool:
    if u.get("banned_until", 0) > now():
        return False
    return (now() - u.get("last_seen", 0)) < 120

def public_user(u: dict) -> dict:
    return {
        "id": u["id"], "username": u["username"], "name": u["name"],
        "color": u["color"], "role": u["role"], "status": u["status"],
        "online": is_online(u), "muted": u.get("muted_until", 0) > now(),
        "banned": u.get("banned_until", 0) > now(),
        "created": u["created"],
    }

def all_user_ids():
    return list(STATE["users"].keys())

def get_channel(cid: int) -> dict:
    ch = STATE["channels"].get(cid)
    if not ch:
        raise HTTPException(404, "Канал не найден")
    return ch

def can_see(user: dict, ch: dict) -> bool:
    if ch["type"] == "public":
        return True
    if ch["type"] == "dm":
        # личные переписки видят только участники (даже админы не влезают)
        return user["id"] in ch.get("members", [])
    # приватные каналы: участники + админы/владелец (модерация)
    return user["id"] in ch.get("members", []) or role_level(user["role"]) >= 2

def can_read(user: dict, ch: dict) -> bool:
    return can_see(user, ch)

def can_manage_channel(user: dict, ch: dict) -> bool:
    return user["id"] == ch.get("owner_id") or role_level(user["role"]) >= 2

def channel_view(ch: dict, user: dict) -> dict:
    v = {
        "id": ch["id"], "name": ch["name"], "description": ch.get("description", ""),
        "type": ch["type"], "owner_id": ch.get("owner_id"), "owner_name": ch.get("owner_name"),
        "created": ch["created"], "pinned": ch.get("pinned", []),
        "has_password": bool(ch.get("password")), "activity": ch.get("activity", 0),
        "member_count": len(ch.get("members", [])),
        "members": [public_user(STATE["users"][m]) for m in ch.get("members", []) if m in STATE["users"]],
    }
    if ch["type"] == "dm":
        other = [m for m in ch.get("members", []) if m != user["id"]]
        ou = STATE["users"].get(other[0]) if other else None
        v["other_id"] = ou["id"] if ou else None
        v["other_name"] = ou["name"] if ou else "?"
        v["other_color"] = ou["color"] if ou else "#888"
        v["other_online"] = is_online(ou) if ou else False
    return v

def server_info() -> dict:
    s = STATE["settings"]
    return {
        "name": s["name"], "motd": s["motd"],
        "maintenance": s["maintenance_mode"],
        "allow_registration": s["allow_registration"],
        "allow_editing": s["allow_editing"],
        "allow_reactions": s["allow_reactions"],
        "allow_dm": s["allow_dm"],
        "max_message_length": s["max_message_length"],
        "history_limit": s["history_limit"],
        "rate_limit": s["rate_limit"],
        "uptime": int(now() - START_TIME),
        "users_total": len(STATE["users"]),
        "users_online": len([u for u in STATE["users"].values() if is_online(u)]),
        "channels_total": len(STATE["channels"]),
        "messages_total": sum(len(v) for v in STATE["messages"].values()),
        "version": APP_VERSION,
    }

def push_event(user_ids, etype: str, data: dict):
    STATE["seq_evt"] += 1
    ev = {"id": STATE["seq_evt"], "type": etype, "data": data, "ts": now()}
    for u in user_ids:
        lst = STATE["events"].setdefault(u, [])
        lst.append(ev)
        if len(lst) > 100:
            del lst[: len(lst) - 100]
    return ev

def make_message(cid: int, user, text: str, kind: str = "user") -> dict:
    STATE["seq_msg"] += 1
    m = {
        "id": STATE["seq_msg"],
        "cid": cid,
        "author_id": user["id"] if user else None,
        "author": user["name"] if user else "SYSTEM",
        "color": user["color"] if user else "#8a93a0",
        "kind": kind,
        "text": text,
        "ts": now(),
        "edited": False,
        "reactions": {},
    }
    lst = STATE["messages"].setdefault(cid, [])
    lst.append(m)
    limit = STATE["settings"]["history_limit"]
    if len(lst) > limit:
        del lst[: len(lst) - limit]
    STATE["channels"][cid]["activity"] = m["ts"]
    return m

def system_message(cid: int, text: str) -> dict:
    return make_message(cid, None, text, kind="system")

def typing_users(cid: int, me: dict):
    if not cid:
        return []
    res = []
    for uid_, ts in list(STATE["typing"].get(cid, {}).items()):
        if uid_ == me["id"]:
            continue
        if ts > now() - 4:
            u = STATE["users"].get(uid_)
            if u:
                res.append(u["name"])
    return res

def rate_ok(user: dict) -> bool:
    if role_level(user["role"]) >= 1:
        return True
    limit = STATE["settings"]["rate_limit"]
    lst = [t for t in STATE["rate"].get(user["id"], []) if t > now() - 60]
    STATE["rate"][user["id"]] = lst
    return len(lst) < limit

def bootstrap_channels():
    cid = next_channel_id()
    ch = {"id": cid, "name": "general", "description": "Общий канал",
          "type": "public", "owner_id": None, "owner_name": "Сервер",
          "created": now(), "pinned": [], "members": [],
          "password": None, "password_salt": "", "activity": 0}
    STATE["channels"][cid] = ch
    STATE["messages"][cid] = []
    system_message(cid, "Канал #general создан. Добро пожаловать на сервер!")

bootstrap_channels()

# ======================================================================
#  АВТОРИЗАЦИЯ
# ======================================================================
def get_token(req: Request) -> str:
    token = req.cookies.get("session") or ""
    if not token:
        h = req.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            token = h[7:].strip()
    if not token:
        token = str(req.query_params.get("token", ""))
    return token

def auth_user(req: Request) -> dict:
    token = get_token(req)
    uid_ = STATE["tokens"].get(token)
    if uid_ is None:
        raise HTTPException(401, "Требуется авторизация")
    user = STATE["users"].get(uid_)
    if user is None:
        raise HTTPException(401, "Пользователь не найден")
    if user.get("banned_until", 0) > now():
        raise HTTPException(403, "BANNED: " + str(user.get("ban_reason", "нарушение правил")))
    if user.get("kicked_at", 0) > user.get("last_login", 0):
        raise HTTPException(401, "Сессия завершена администратором")
    user["last_seen"] = now()
    return user

def require_role(user: dict, lvl: int):
    if role_level(user["role"]) < lvl:
        raise HTTPException(403, "Недостаточно прав")

def guard_maintenance(user: dict):
    if STATE["settings"]["maintenance_mode"] and role_level(user["role"]) < 2:
        raise HTTPException(503, "Сервер на обслуживании. Зайдите позже.")

# ======================================================================
#  ПРИЛОЖЕНИЕ
# ======================================================================
app = FastAPI(title="DSH Messenger", version=APP_VERSION)

# ------------------------------ АВТОРИЗАЦИЯ ------------------------------
@app.post("/api/register")
def api_register(req: Request, payload: dict):
    s = STATE["settings"]
    if not s["allow_registration"]:
        raise HTTPException(403, "Регистрация отключена администратором")
    if s["maintenance_mode"]:
        raise HTTPException(503, "Сервер на обслуживании")
    username = str(payload.get("username", "")).strip()
    name = str(payload.get("name", "")).strip() or username
    password = str(payload.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        raise HTTPException(400, "Логин: 3–20 символов (латиница, цифры, _)")
    if len(password) < 4:
        raise HTTPException(400, "Пароль: минимум 4 символа")
    if len(name) > 32:
        raise HTTPException(400, "Имя слишком длинное")
    low = username.lower()
    ban = STATE["bans"].get(low)
    if ban and ban["until"] > now():
        raise HTTPException(403, "Этот логин забанен: " + ban["reason"])
    for u in STATE["users"].values():
        if u["username"].lower() == low:
            raise HTTPException(409, "Логин уже занят")
    first = not STATE["users"]
    uid_ = STATE["seq_ch"] + 1000  # уникальный id (не пересекается с каналами)
    while uid_ in STATE["users"]:
        uid_ += 1
    salt = secrets.token_hex(8)
    user = {
        "id": uid_, "username": username, "name": name,
        "color": PALETTE[uid_ % len(PALETTE)],
        "role": "owner" if first else "user",
        "status": "online", "last_seen": now(), "created": now(),
        "muted_until": 0, "banned_until": 0, "ban_reason": "",
        "kicked_at": 0, "last_login": now(),
        "salt": salt, "hash": sha256(password, salt),
        "settings": {"sound": True, "compact": False, "show_system": True},
    }
    STATE["users"][uid_] = user
    if first:
        STATE["owner_id"] = uid_
        push_event([uid_], "broadcast",
                   {"text": "Вы первый пользователь и стали владельцем сервера!"})
    token = secrets.token_hex(24)
    STATE["tokens"][token] = uid_
    resp = JSONResponse({"token": token, "user": {**public_user(user), "settings": user["settings"]}})
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp

@app.post("/api/login")
def api_login(req: Request, payload: dict):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    low = username.lower()
    user = None
    for u in STATE["users"].values():
        if u["username"].lower() == low:
            user = u
            break
    if not user or sha256(password, user["salt"]) != user["hash"]:
        raise HTTPException(401, "Неверный логин или пароль")
    if user.get("banned_until", 0) > now():
        raise HTTPException(403, "Аккаунт забанен: " + str(user.get("ban_reason", "")))
    user["last_login"] = now()
    user["last_seen"] = now()
    if user["status"] == "offline":
        user["status"] = "online"
    token = secrets.token_hex(24)
    STATE["tokens"][token] = user["id"]
    resp = JSONResponse({"token": token, "user": {**public_user(user), "settings": user["settings"]}})
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp

@app.post("/api/logout")
def api_logout(req: Request):
    token = get_token(req)
    if token in STATE["tokens"]:
        uid_ = STATE["tokens"].pop(token)
        u = STATE["users"].get(uid_)
        if u:
            u["last_seen"] = now()
            if not any(v == uid_ for v in STATE["tokens"].values()):
                u["status"] = "offline"
    return {"ok": True}

@app.get("/api/me")
def api_me(req: Request):
    user = auth_user(req)
    return {**public_user(user), "settings": user["settings"]}

# ------------------------------ СИНХРОНИЗАЦИЯ ------------------------------
@app.get("/api/sync")
def api_sync(req: Request, channel: str = "", after_msg: int = 0, after_evt: int = 0):
    user = auth_user(req)
    s = STATE["settings"]
    if s["maintenance_mode"] and role_level(user["role"]) < 2:
        return {"status": "maintenance", "server": server_info()}
    cid = int(channel) if channel and channel.isdigit() else 0
    msgs = []
    ch = STATE["channels"].get(cid)
    if ch and can_read(user, ch):
        msgs = [m for m in STATE["messages"].get(cid, []) if m["id"] > after_msg][-100:]
    events = [e for e in STATE["events"].get(user["id"], []) if e["id"] > after_evt]
    return {
        "status": "ok",
        "server": server_info(),
        "me": {**public_user(user), "settings": user["settings"]},
        "users": [public_user(u) for u in STATE["users"].values()],
        "channels": [channel_view(c, user) for c in STATE["channels"].values() if can_see(user, c)],
        "messages": msgs,
        "typing": typing_users(cid, user),
        "events": events,
    }

# ------------------------------ КАНАЛЫ ------------------------------
@app.get("/api/channels")
def api_channels(req: Request):
    user = auth_user(req)
    return {"channels": [channel_view(c, user) for c in STATE["channels"].values() if can_see(user, c)]}

@app.post("/api/channels")
def api_create_channel(req: Request, payload: dict):
    user = auth_user(req)
    guard_maintenance(user)
    name = str(payload.get("name", "")).strip().lstrip("#")
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9 _\-\.]{2,40}", name):
        raise HTTPException(400, "Имя канала: 2–40 символов (буквы, цифры, пробел, - _ .)")
    if any(ch["name"].lower() == name.lower() for ch in STATE["channels"].values() if ch["type"] != "dm"):
        raise HTTPException(409, "Канал с таким именем уже существует")
    ctype = payload.get("type", "public")
    if ctype not in ("public", "private"):
        ctype = "public"
    password = str(payload.get("password", ""))
    ch = {"id": next_channel_id(), "name": name,
          "description": str(payload.get("description", ""))[:200],
          "type": ctype, "owner_id": user["id"], "owner_name": user["name"],
          "created": now(), "pinned": [],
          "members": [] if ctype == "public" else [user["id"]],
          "password": None, "password_salt": "", "activity": 0}
    if ctype == "private" and password:
        if len(password) < 4:
            raise HTTPException(400, "Пароль канала: минимум 4 символа")
        ch["password_salt"] = secrets.token_hex(8)
        ch["password"] = sha256(password, ch["password_salt"])
    STATE["channels"][ch["id"]] = ch
    STATE["messages"][ch["id"]] = []
    push_event(all_user_ids(), "channel_new", {"channel_id": ch["id"], "name": name})
    return {"channel": channel_view(ch, user)}

@app.post("/api/channels/join")
def api_join_by_name(req: Request, payload: dict):
    user = auth_user(req)
    guard_maintenance(user)
    name = str(payload.get("name", "")).strip().lstrip("#").lower()
    if not name:
        raise HTTPException(400, "Укажите имя канала")
    for ch in STATE["channels"].values():
        if ch["type"] == "private" and ch["name"].lower() == name:
            if user["id"] in ch["members"]:
                return {"channel": channel_view(ch, user)}
            if ch.get("password"):
                given = str(payload.get("password", ""))
                if sha256(given, ch["password_salt"]) != ch["password"]:
                    raise HTTPException(403, "Неверный пароль канала")
            ch["members"].append(user["id"])
            push_event([m for m in ch["members"] if m != user["id"]],
                       "member_joined",
                       {"channel_id": ch["id"], "user_id": user["id"], "name": user["name"]})
            system_message(ch["id"], f"{user['name']} присоединился к каналу")
            return {"channel": channel_view(ch, user)}
    raise HTTPException(404, "Приватный канал с таким именем не найден")

@app.post("/api/channels/{cid}/leave")
def api_leave_channel(req: Request, cid: int):
    user = auth_user(req)
    ch = get_channel(cid)
    if user["id"] not in ch["members"]:
        raise HTTPException(400, "Вы не участник этого канала")
    ch["members"].remove(user["id"])
    if not ch["members"]:
        STATE["channels"].pop(cid, None)
        STATE["messages"].pop(cid, None)
        push_event(all_user_ids(), "channel_deleted", {"channel_id": cid, "name": ch["name"]})
    else:
        if ch["owner_id"] == user["id"]:
            ch["owner_id"] = ch["members"][0]
            ch["owner_name"] = STATE["users"][ch["members"][0]]["name"]
        push_event(ch["members"], "member_left",
                   {"channel_id": cid, "user_id": user["id"], "name": user["name"]})
    return {"ok": True}

@app.patch("/api/channels/{cid}")
def api_update_channel(req: Request, cid: int, payload: dict):
    user = auth_user(req)
    ch = get_channel(cid)
    if not can_manage_channel(user, ch):
        raise HTTPException(403, "Только владелец или администратор")
    if "name" in payload:
        name = str(payload["name"]).strip().lstrip("#")
        if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9 _\-\.]{2,40}", name):
            raise HTTPException(400, "Некорректное имя канала")
        ch["name"] = name
    if "description" in payload:
        ch["description"] = str(payload["description"])[:200]
    push_event(all_user_ids(), "channel_updated", {"channel_id": cid})
    return {"channel": channel_view(ch, user)}

@app.delete("/api/channels/{cid}")
def api_delete_channel(req: Request, cid: int):
    user = auth_user(req)
    ch = get_channel(cid)
    if not can_manage_channel(user, ch):
        raise HTTPException(403, "Только владелец или администратор")
    STATE["channels"].pop(cid, None)
    STATE["messages"].pop(cid, None)
    push_event(all_user_ids(), "channel_deleted", {"channel_id": cid, "name": ch["name"]})
    return {"ok": True}

@app.post("/api/channels/{cid}/clear")
def api_clear_channel(req: Request, cid: int):
    user = auth_user(req)
    ch = get_channel(cid)
    if role_level(user["role"]) < 1 and user["id"] != ch.get("owner_id"):
        raise HTTPException(403, "Недостаточно прав")
    STATE["messages"][cid] = []
    ch["pinned"] = []
    push_event(all_user_ids(), "channel_cleared", {"channel_id": cid})
    return {"ok": True}

@app.post("/api/channels/{cid}/pin")
def api_toggle_pin(req: Request, cid: int, payload: dict):
    user = auth_user(req)
    ch = get_channel(cid)
    if role_level(user["role"]) < 1 and user["id"] != ch.get("owner_id"):
        raise HTTPException(403, "Недостаточно прав")
    mid = int(payload.get("message_id", 0))
    msg = next((m for m in STATE["messages"].get(cid, []) if m["id"] == mid), None)
    if not msg:
        raise HTTPException(404, "Сообщение не найдено")
    if any(p["id"] == mid for p in ch["pinned"]):
        ch["pinned"] = [p for p in ch["pinned"] if p["id"] != mid]
        pinned = False
    else:
        ch["pinned"].append({"id": mid, "text": msg["text"][:80], "author": msg["author"]})
        pinned = True
    return {"pinned": pinned, "list": ch["pinned"]}

@app.post("/api/dm")
def api_dm(req: Request, payload: dict):
    user = auth_user(req)
    guard_maintenance(user)
    if not STATE["settings"]["allow_dm"]:
        raise HTTPException(403, "Личные сообщения отключены")
    other_id = int(payload.get("user_id", 0))
    other = STATE["users"].get(other_id)
    if not other:
        raise HTTPException(404, "Пользователь не найден")
    if other_id == user["id"]:
        raise HTTPException(400, "Нельзя писать самому себе")
    for ch in STATE["channels"].values():
        if ch["type"] == "dm" and set(ch["members"]) == {user["id"], other_id}:
            return {"channel": channel_view(ch, user)}
    ch = {"id": next_channel_id(), "name": "dm", "description": "",
          "type": "dm", "owner_id": user["id"], "owner_name": user["name"],
          "created": now(), "pinned": [], "members": [user["id"], other_id],
          "password": None, "password_salt": "", "activity": 0}
    STATE["channels"][ch["id"]] = ch
    STATE["messages"][ch["id"]] = []
    return {"channel": channel_view(ch, user)}

# ------------------------------ СООБЩЕНИЯ ------------------------------
@app.get("/api/channels/{cid}/messages")
def api_messages(req: Request, cid: int, after: int = 0, before: int = 0, limit: int = 60):
    user = auth_user(req)
    ch = get_channel(cid)
    if not can_read(user, ch):
        raise HTTPException(403, "Нет доступа к каналу")
    limit = max(1, min(limit, 200))
    lst = STATE["messages"].get(cid, [])
    if before:
        lst = [m for m in lst if m["id"] < before][-limit:]
    elif after:
        lst = [m for m in lst if m["id"] > after][-limit:]
    else:
        lst = lst[-limit:]
    return {"messages": lst}

@app.post("/api/channels/{cid}/messages")
def api_send_message(req: Request, cid: int, payload: dict):
    user = auth_user(req)
    guard_maintenance(user)
    ch = get_channel(cid)
    if not can_read(user, ch):
        raise HTTPException(403, "Нет доступа к каналу")
    if user.get("muted_until", 0) > now():
        raise HTTPException(403, "Вы заглушены до " + fmt_time(user["muted_until"]))
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > STATE["settings"]["max_message_length"]:
        raise HTTPException(400, f"Сообщение длиннее {STATE['settings']['max_message_length']} символов")
    if not rate_ok(user):
        raise HTTPException(429, "Слишком часто. Лимит сообщений в минуту исчерпан.")
    STATE["rate"].setdefault(user["id"], []).append(now())
    msg = make_message(cid, user, text)
    return {"message": msg}

@app.patch("/api/messages/{mid}")
def api_edit_message(req: Request, mid: int, payload: dict):
    user = auth_user(req)
    if not STATE["settings"]["allow_editing"]:
        raise HTTPException(403, "Редактирование отключено")
    for cid, lst in STATE["messages"].items():
        msg = next((m for m in lst if m["id"] == mid), None)
        if msg:
            ch = STATE["channels"][cid]
            if not can_read(user, ch):
                raise HTTPException(403, "Нет доступа")
            if msg["author_id"] != user["id"] and role_level(user["role"]) < 2:
                raise HTTPException(403, "Можно редактировать только свои сообщения")
            text = str(payload.get("text", "")).strip()
            if not text:
                raise HTTPException(400, "Пустое сообщение")
            if len(text) > STATE["settings"]["max_message_length"]:
                raise HTTPException(400, "Сообщение слишком длинное")
            msg["text"] = text
            msg["edited"] = True
            return {"message": msg}
    raise HTTPException(404, "Сообщение не найдено")

@app.delete("/api/messages/{mid}")
def api_delete_message(req: Request, mid: int):
    user = auth_user(req)
    for cid, lst in STATE["messages"].items():
        msg = next((m for m in lst if m["id"] == mid), None)
        if msg:
            ch = STATE["channels"][cid]
            if not can_read(user, ch):
                raise HTTPException(403, "Нет доступа")
            if msg["author_id"] != user["id"] and role_level(user["role"]) < 1:
                raise HTTPException(403, "Можно удалять только свои сообщения")
            lst.remove(msg)
            ch["pinned"] = [p for p in ch.get("pinned", []) if p["id"] != mid]
            return {"ok": True}
    raise HTTPException(404, "Сообщение не найдено")

@app.post("/api/messages/{mid}/react")
def api_react(req: Request, mid: int, payload: dict):
    user = auth_user(req)
    guard_maintenance(user)
    if not STATE["settings"]["allow_reactions"]:
        raise HTTPException(403, "Реакции отключены")
    emoji = str(payload.get("emoji", "")).strip()
    if not emoji or len(emoji) > 8:
        raise HTTPException(400, "Некорректная реакция")
    for cid, lst in STATE["messages"].items():
        msg = next((m for m in lst if m["id"] == mid), None)
        if msg:
            if not can_read(user, STATE["channels"][cid]):
                raise HTTPException(403, "Нет доступа")
            r = msg.setdefault("reactions", {})
            ids = r.setdefault(emoji, [])
            if user["id"] in ids:
                ids.remove(user["id"])
                if not ids:
                    del r[emoji]
            else:
                ids.append(user["id"])
            return {"reactions": msg["reactions"]}
    raise HTTPException(404, "Сообщение не найдено")

@app.post("/api/channels/{cid}/typing")
def api_typing(req: Request, cid: int):
    user = auth_user(req)
    ch = get_channel(cid)
    if not can_read(user, ch):
        raise HTTPException(403, "Нет доступа")
    STATE["typing"].setdefault(cid, {})[user["id"]] = now()
    return {"ok": True}

# ------------------------------ ПОИСК / ЭКСПОРТ ------------------------------
@app.get("/api/search")
def api_search(req: Request, q: str = ""):
    user = auth_user(req)
    q = q.strip().lower()
    if len(q) < 2:
        return {"results": []}
    results = []
    for ch in STATE["channels"].values():
        if not can_read(user, ch):
            continue
        for m in STATE["messages"].get(ch["id"], []):
            if q in m["text"].lower():
                results.append({"id": m["id"], "cid": ch["id"], "channel": ch["name"],
                                "author": m["author"], "text": m["text"][:200], "ts": m["ts"]})
    results.sort(key=lambda r: r["ts"], reverse=True)
    return {"results": results[:100]}

@app.get("/api/channels/{cid}/export")
def api_export(req: Request, cid: int):
    user = auth_user(req)
    ch = get_channel(cid)
    if not can_read(user, ch):
        raise HTTPException(403, "Нет доступа")
    data = {"channel": channel_view(ch, user), "messages": STATE["messages"].get(cid, [])}
    resp = JSONResponse(data)
    resp.headers["Content-Disposition"] = f'attachment; filename="channel_{cid}.json"'
    return resp

# ------------------------------ НАСТРОЙКИ МЕНЯ ------------------------------
@app.patch("/api/me")
def api_update_me(req: Request, payload: dict):
    user = auth_user(req)
    if "name" in payload:
        name = str(payload["name"]).strip()
        if not name or len(name) > 32:
            raise HTTPException(400, "Имя: 1–32 символа")
        user["name"] = name
    if "color" in payload and str(payload["color"]) in PALETTE:
        user["color"] = payload["color"]
    if "status" in payload and str(payload["status"]) in ("online", "away", "busy"):
        user["status"] = payload["status"]
    if isinstance(payload.get("settings"), dict):
        st = user["settings"]
        for key in ("sound", "compact", "show_system"):
            if key in payload["settings"] and isinstance(payload["settings"][key], bool):
                st[key] = payload["settings"][key]
    return {**public_user(user), "settings": user["settings"]}

@app.post("/api/me/password")
def api_change_password(req: Request, payload: dict):
    user = auth_user(req)
    old = str(payload.get("old_password", ""))
    new = str(payload.get("new_password", ""))
    if sha256(old, user["salt"]) != user["hash"]:
        raise HTTPException(403, "Неверный текущий пароль")
    if len(new) < 4:
        raise HTTPException(400, "Новый пароль: минимум 4 символа")
    user["salt"] = secrets.token_hex(8)
    user["hash"] = sha256(new, user["salt"])
    return {"ok": True}

# ------------------------------ АДМИНКА ------------------------------
@app.get("/api/admin/stats")
def api_admin_stats(req: Request):
    user = auth_user(req)
    require_role(user, 2)
    return server_info()

@app.get("/api/admin/users")
def api_admin_users(req: Request):
    user = auth_user(req)
    require_role(user, 2)
    return {"users": [
        {**public_user(u), "muted_until": u.get("muted_until", 0),
         "banned_until": u.get("banned_until", 0)} for u in STATE["users"].values()
    ]}

@app.get("/api/admin/channels")
def api_admin_channels(req: Request):
    user = auth_user(req)
    require_role(user, 2)
    return {"channels": [
        {"id": c["id"], "name": c["name"], "type": c["type"],
         "owner_name": c.get("owner_name"), "created": c["created"],
         "messages": len(STATE["messages"].get(c["id"], [])),
         "members": len(c.get("members", []))}
        for c in STATE["channels"].values()
    ]}

@app.patch("/api/admin/users/{uid}")
def api_admin_user_action(req: Request, uid: int, payload: dict):
    admin = auth_user(req)
    require_role(admin, 2)
    target = STATE["users"].get(uid)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    if target["id"] == STATE["owner_id"] and admin["id"] != target["id"]:
        raise HTTPException(403, "Нельзя управлять владельцем")
    if target["id"] != admin["id"] and role_level(target["role"]) >= role_level(admin["role"]):
        raise HTTPException(403, "Нельзя управлять равным или выше по роли")
    # роль
    if "role" in payload:
        new_role = str(payload["role"])
        if new_role not in ROLE_LEVEL:
            raise HTTPException(400, "Неизвестная роль")
        if new_role in ("admin", "owner") and admin["role"] != "owner":
            raise HTTPException(403, "Только владелец может выдавать эту роль")
        target["role"] = new_role
        push_event([uid], "role_changed", {"role": new_role})
    # переименование
    if "name" in payload:
        name = str(payload["name"]).strip()
        if name and len(name) <= 32:
            target["name"] = name
    # мут / размут
    if "mute_minutes" in payload:
        m = int(payload["mute_minutes"])
        if m > 0:
            target["muted_until"] = now() + m * 60
            push_event([uid], "muted", {"until_str": fmt_time(target["muted_until"])})
        else:
            target["muted_until"] = 0
            push_event([uid], "unmuted", {})
    # бан / разбан
    if "ban" in payload:
        if payload["ban"]:
            target["banned_until"] = 1e15  # «навсегда» (JSON не любит inf)
            target["ban_reason"] = str(payload.get("reason", "нарушение правил"))
            STATE["bans"][target["username"].lower()] = {
                "until": target["banned_until"], "reason": target["ban_reason"]}
            for tok, uid_ in list(STATE["tokens"].items()):
                if uid_ == uid:
                    STATE["tokens"].pop(tok, None)
        else:
            target["banned_until"] = 0
            target["ban_reason"] = ""
            STATE["bans"].pop(target["username"].lower(), None)
    # кик (завершить сессии)
    if "kick" in payload:
        target["kicked_at"] = now()
        push_event([uid], "kicked", {"reason": str(payload.get("reason", "вы исключены"))})
    return {**public_user(target), "muted_until": target.get("muted_until", 0),
            "banned_until": target.get("banned_until", 0)}

@app.get("/api/admin/settings")
def api_admin_get_settings(req: Request):
    user = auth_user(req)
    require_role(user, 2)
    return {"settings": STATE["settings"]}

@app.patch("/api/admin/settings")
def api_admin_set_settings(req: Request, payload: dict):
    user = auth_user(req)
    require_role(user, 2)
    s = STATE["settings"]
    if "name" in payload:
        name = str(payload["name"]).strip()
        if 1 <= len(name) <= 40:
            s["name"] = name
    if "motd" in payload:
        s["motd"] = str(payload["motd"])[:300]
    if "max_message_length" in payload:
        s["max_message_length"] = max(10, min(int(payload["max_message_length"]), 10000))
    if "history_limit" in payload:
        s["history_limit"] = max(10, min(int(payload["history_limit"]), 5000))
    if "rate_limit" in payload:
        s["rate_limit"] = max(1, min(int(payload["rate_limit"]), 300))
    for key in ("allow_registration", "maintenance_mode", "allow_editing",
                "allow_reactions", "allow_dm"):
        if key in payload and isinstance(payload[key], bool):
            s[key] = payload[key]
    push_event(all_user_ids(), "settings_changed", {})
    return {"settings": s}

@app.post("/api/admin/broadcast")
def api_admin_broadcast(req: Request, payload: dict):
    user = auth_user(req)
    require_role(user, 2)
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "Пустой текст объявления")
    for cid in STATE["channels"]:
        system_message(cid, f"📢 {text}")
    push_event(all_user_ids(), "broadcast", {"text": text})
    return {"ok": True}

@app.post("/api/admin/reset")
def api_admin_reset(req: Request):
    user = auth_user(req)
    require_role(user, 2)
    my_id = user["id"]
    token = get_token(req)
    user["role"] = "owner"
    user["muted_until"] = 0
    user["banned_until"] = 0
    user["kicked_at"] = 0
    STATE["users"] = {my_id: user}
    STATE["tokens"] = {token: my_id} if token else {}
    STATE["channels"] = {}
    STATE["messages"] = {}
    STATE["events"] = {}
    STATE["typing"] = {}
    STATE["bans"] = {}
    STATE["rate"] = {}
    STATE["seq_ch"] = 0
    STATE["seq_msg"] = 0
    STATE["seq_evt"] = 0
    STATE["owner_id"] = my_id
    bootstrap_channels()
    return {"ok": True, "message": "Сервер сброшен. Вы остались владельцем."}

# ------------------------------ СЛУЖЕБНОЕ ------------------------------
@app.get("/api/ping")
def api_ping():
    return {"pong": True, "time": now(), "uptime": int(now() - START_TIME)}

@app.get("/api/health")
def api_health():
    return {"status": "ok", **server_info()}

@app.get("/api/version")
def api_version():
    return {"name": "DSH Messenger", "version": APP_VERSION,
            "ui": "Twitter Bootstrap 1.4.0 style (dark)", "storage": "RAM only"}

@app.get("/favicon.ico")
def favicon():
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
           "<text y='.9em' font-size='90'>💬</text></svg>")
    return Response(content=svg, media_type="image/svg+xml")

# ======================================================================
#  HTML / CSS / JS  (тёмная тема в духе Twitter Bootstrap 1.4.0)
# ======================================================================
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>__SERVER_NAME__</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>💬</text></svg>">
<style>
/* ===== база (Bootstrap 1.4.0 dark) ===== */
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: #101216;
  color: #c8cdd3;
  font: 13px/18px "Helvetica Neue", Helvetica, Arial, sans-serif;
  padding-top: 40px;
}
a { color: #5aa9e6; text-decoration: none; }
a:hover { color: #8cc4f0; }
h1, h2, h3, h4 { color: #fff; text-rendering: optimizelegibility; }
h2 { font-size: 22px; line-height: 30px; }
h3 { font-size: 18px; line-height: 27px; }
h4 { font-size: 14px; }
.muted-text { color: #6b7683; font-size: 12px; }
.container { width: 940px; margin: 0 auto; }

/* сетка 16 колонок как в 1.4.0: 40px колонка + 20px отступ */
.row { margin-left: -20px; zoom: 1; }
.row:after { display: block; clear: both; content: ""; }
.span1{width:40px}.span2{width:100px}.span3{width:160px}.span4{width:220px}
.span5{width:280px}.span6{width:340px}.span7{width:400px}.span8{width:460px}
.span9{width:520px}.span10{width:580px}.span11{width:640px}.span12{width:700px}
.span13{width:760px}.span14{width:820px}.span15{width:880px}.span16{width:940px}
[class*="span"] { float: left; margin-left: 20px; }

/* ===== topbar (чёрная шапка 1.4.0) ===== */
.topbar { position: fixed; top: 0; left: 0; right: 0; height: 40px; z-index: 1000;
  background-color: #000; background-image: linear-gradient(#181818, #050505);
  background-repeat: repeat-x; border-bottom: 1px solid #000;
  box-shadow: 0 1px 3px rgba(0,0,0,.6); }
.topbar-inner { padding: 0 20px; }
.topbar h3 a, .topbar .brand { float: left; display: block; padding: 8px 20px 8px 0;
  color: #fff; font-size: 18px; font-weight: bold; line-height: 24px; text-decoration: none; }
.topbar .nav { float: right; margin: 0; padding: 0; list-style: none; }
.topbar ul.nav li { float: left; display: block; }
.topbar ul.nav a { display: block; padding: 9px 12px; color: #bfbfbf; text-decoration: none; font-size: 13px; }
.topbar ul.nav a:hover { color: #fff; }
.topbar ul.nav li.active a { color: #fff; background: rgba(255,255,255,.15); border-radius: 4px; }
.topbar form { float: right; margin: 0; padding: 5px 0 5px 10px; }
.topbar form input[type=text] { background: #26292e; border: 1px solid #000; color: #ccc;
  border-radius: 4px; padding: 4px 8px; width: 150px; font-size: 13px; }
.topbar form input[type=text]:focus { background: #33383f; color: #fff; outline: none; }

/* ===== кнопки (градиенты 1.4.0) ===== */
.btn { display: inline-block; padding: 4px 12px; font-size: 13px; line-height: 18px;
  color: #ddd; text-shadow: 0 1px 1px rgba(0,0,0,.6); text-decoration: none;
  background-color: #3a3f46; background-image: linear-gradient(#4a5058, #2f343a);
  background-repeat: repeat-x; border: 1px solid #1b1e22; border-radius: 4px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.12), 0 1px 2px rgba(0,0,0,.4);
  cursor: pointer; vertical-align: middle; }
.btn:hover { background-image: linear-gradient(#555c65, #383e45); color: #fff; }
.btn:active { box-shadow: inset 0 2px 4px rgba(0,0,0,.5); }
.btn.primary { background-color: #0064cd; background-image: linear-gradient(#049cdb, #0064cd);
  border-color: #004b9a; color: #fff; text-shadow: 0 -1px 0 rgba(0,0,0,.3); }
.btn.primary:hover { background-image: linear-gradient(#0aa8e8, #006fe0); }
.btn.danger { background-color: #c43c35; background-image: linear-gradient(#ee5f5b, #c43c35);
  border-color: #8f2924; color: #fff; text-shadow: 0 -1px 0 rgba(0,0,0,.3); }
.btn.danger:hover { background-image: linear-gradient(#f47a76, #d14a44); }
.btn.success { background-color: #51a351; background-image: linear-gradient(#62c462, #51a351);
  border-color: #3c7c3c; color: #fff; text-shadow: 0 -1px 0 rgba(0,0,0,.3); }
.btn.success:hover { background-image: linear-gradient(#71cf71, #5caf5c); }
.btn.warning { background-color: #f89406; background-image: linear-gradient(#fbb450, #f89406);
  border-color: #b56e04; color: #fff; text-shadow: 0 -1px 0 rgba(0,0,0,.3); }
.btn.info { background-color: #2f96b4; background-image: linear-gradient(#5bc0de, #2f96b4);
  border-color: #226f86; color: #fff; text-shadow: 0 -1px 0 rgba(0,0,0,.3); }
.btn.small { padding: 2px 8px; font-size: 11px; }
.btn.mini { padding: 1px 6px; font-size: 10px; }
.btn:disabled, .btn.disabled { opacity: .5; cursor: default; }
.mini-btn { color: #7f8c98; text-decoration: none; margin-left: 6px; font-size: 13px; }
.mini-btn:hover { color: #049cdb; }

/* ===== alert-message (1.4.0) ===== */
#alerts { position: fixed; top: 48px; right: 12px; z-index: 1200; width: 340px; }
.alert-message { position: relative; padding: 7px 30px 7px 15px; margin-bottom: 10px;
  color: #fff; text-shadow: 0 -1px 0 rgba(0,0,0,.35); border-radius: 4px;
  border: 1px solid rgba(0,0,0,.4); box-shadow: inset 0 1px 0 rgba(255,255,255,.2), 0 2px 6px rgba(0,0,0,.5); }
.alert-message.error { background-color: #a9322b; background-image: linear-gradient(#d14a44, #8f2924); }
.alert-message.success { background-color: #3f7a3a; background-image: linear-gradient(#57a957, #356e30); }
.alert-message.info { background-color: #28748e; background-image: linear-gradient(#339bb9, #1f6078); }
.alert-message.warning { background-color: #b56e04; background-image: linear-gradient(#d98a10, #8f5704); }
.alert-close { position: absolute; top: 6px; right: 8px; color: rgba(255,255,255,.7);
  font-size: 16px; font-weight: bold; text-decoration: none; }
.alert-close:hover { color: #fff; }

/* ===== label-бейджи (1.4.0) ===== */
.label { padding: 1px 3px 2px; font-size: 10.5px; font-weight: bold; color: #fff;
  text-shadow: 0 -1px 0 rgba(0,0,0,.3); background-color: #8a93a0; border-radius: 3px;
  text-transform: uppercase; }
.label.important { background-color: #c43c35; }
.label.warning { background-color: #f89406; }
.label.success { background-color: #468847; }
.label.notice { background-color: #62cffc; color: #0a2233; text-shadow: none; }

/* ===== well / side-панели ===== */
.well { background: #171a1f; border: 1px solid #2a2e35; border-radius: 4px; padding: 10px; margin-bottom: 10px; }
.side { background: #171a1f; border: 1px solid #2a2e35; border-radius: 6px;
  box-shadow: 0 1px 3px rgba(0,0,0,.5); margin-bottom: 12px; }
.panel-head { padding: 8px 12px; background: #1c2026; border-bottom: 1px solid #2a2e35;
  border-radius: 6px 6px 0 0; font-weight: bold; color: #fff; }
.panel-body { padding: 10px; }
.chat-panel { display: flex; flex-direction: column; }

/* ===== списки каналов и пользователей ===== */
.chan-list, .user-list { max-height: 340px; overflow-y: auto; }
.chan-item, .user-item { padding: 6px 10px; cursor: pointer; color: #c8cdd3;
  border-bottom: 1px solid #22262c; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.chan-item:hover, .user-item:hover { background: #1e232a; }
.chan-item.active { background: #2a3038; color: #fff; box-shadow: inset 3px 0 0 #049cdb; }
.chan-icon { margin-right: 6px; color: #7f8c98; }
.chan-item.active .chan-icon { color: #049cdb; }
.chan-item .label { float: right; margin-top: 2px; }
.u-color { font-weight: bold; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 8px; margin-right: 6px; vertical-align: middle; }
.dot.online { background: #46a546; box-shadow: 0 0 4px #46a546; }
.dot.away { background: #f89406; }
.dot.busy { background: #c43c35; }
.dot.offline { background: #4a5158; }

/* ===== область сообщений ===== */
#msgs { flex: 1; overflow-y: auto; height: calc(100vh - 295px); min-height: 280px;
  padding: 8px; background: #101318; }
.msg { padding: 6px 8px; border-radius: 4px; }
.msg:hover { background: #1a1f26; }
.avatar { display: inline-block; width: 32px; height: 32px; line-height: 32px; text-align: center;
  border-radius: 4px; color: #fff; font-weight: bold; margin-right: 8px; float: left;
  text-shadow: 0 1px 1px rgba(0,0,0,.4); }
.msg-body { margin-left: 40px; }
.m-name { font-weight: bold; }
.m-time { color: #6b7683; font-size: 11px; margin-left: 6px; }
.m-text { white-space: pre-wrap; word-wrap: break-word; color: #d5dae0; }
.m-actions { float: right; opacity: 0; }
.msg:hover .m-actions { opacity: 1; }
.m-act { margin-left: 8px; color: #8a93a0; text-decoration: none; }
.m-act:hover { color: #049cdb; }
.msys { text-align: center; color: #8a93a0; font-style: italic; padding: 3px; }
.react-chip { display: inline-block; padding: 1px 7px; margin: 4px 4px 0 0; background: #232830;
  border: 1px solid #333a44; border-radius: 10px; font-size: 12px; cursor: pointer; }
.react-chip.mine { border-color: #049cdb; background: #0e2c3f; }
.react-add { color: #8a93a0; cursor: pointer; font-weight: bold; padding: 0 4px; }
.emoji-picker { margin-top: 4px; }
.emoji-picker span { cursor: pointer; font-size: 16px; margin-right: 6px; padding: 2px 4px; border-radius: 3px; }
.emoji-picker span:hover { background: #2a3038; }

/* ===== композер ===== */
.composer { padding: 8px; background: #171a1f; border-top: 1px solid #2a2e35; border-radius: 0 0 6px 6px; }
.composer textarea { width: 100%; resize: vertical; min-height: 46px; }
.emoji-row { margin-right: 8px; }
.emoji-row span { cursor: pointer; font-size: 16px; margin-right: 6px; padding: 2px 4px; border-radius: 3px; }
.emoji-row span:hover { background: #2a3038; }

/* ===== закреплённые ===== */
#pinnedBox { border-bottom: 1px solid #173a52; }
.pin-item { padding: 4px 10px; background: #0e2c3f; border-bottom: 1px solid #173a52;
  cursor: pointer; font-size: 12px; color: #9cc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pin-item:hover { background: #123b52; }

/* ===== формы (1.4.0) ===== */
label { display: block; margin-bottom: 5px; font-weight: bold; color: #aab3bd; }
.input { width: 220px; background: #121418; border: 1px solid #33383f; color: #dde1e6;
  border-radius: 4px; padding: 4px 6px; font-size: 13px; margin-bottom: 12px; }
.input:focus { border-color: rgba(82,168,236,.8); box-shadow: 0 0 8px rgba(82,168,236,.35); outline: none; }
textarea.input { width: 100%; height: auto; }
.input.xlarge { width: 270px; }
select.input { width: 220px; }
.clearfix { zoom: 1; margin-bottom: 18px; }
.clearfix:after { display: block; clear: both; content: ""; }
.actions { padding: 17px 0 0; margin-bottom: 0; }
input[type=checkbox] { vertical-align: middle; }
.checkbox-line { margin-bottom: 8px; color: #c8cdd3; }
.checkbox-line input { margin-right: 6px; }

/* ===== таблицы zebra (1.4.0) ===== */
table { width: 100%; border-collapse: collapse; margin-bottom: 18px; }
th, td { padding: 8px; line-height: 18px; text-align: left; border-top: 1px solid #2a2e34; }
th { font-weight: bold; color: #aab3bd; }
.zebra-striped tbody tr:nth-child(odd) td { background: #1a1d22; }
.zebra-striped tbody tr:hover td { background: #20252c; }

/* ===== pills-табы (1.4.0) ===== */
.pills { margin: 0 0 14px; padding: 0; list-style: none; zoom: 1; border-bottom: 1px solid #2a2e35; }
.pills:after { display: block; clear: both; content: ""; }
.pills li { float: left; }
.pills a { display: block; padding: 8px 12px; color: #aab3bd; text-decoration: none;
  border: 1px solid transparent; margin-bottom: -1px; }
.pills a:hover { color: #fff; }
.pills li.active a { color: #fff; background: #1b1e24; border: 1px solid #2a2e35;
  border-bottom-color: #1b1e24; border-radius: 4px 4px 0 0; }
.pill-content > div { display: none; }
.pill-content > div.active { display: block; }

/* ===== модалки (1.4.0) ===== */
.modal-backdrop { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: #000;
  opacity: .6; z-index: 1040; }
.modal { display: none; position: fixed; top: 50%; left: 50%; width: 560px;
  margin: -250px 0 0 -280px; background: #1b1e24; border: 1px solid #333a44;
  border-radius: 6px; box-shadow: 0 3px 7px rgba(0,0,0,.6); z-index: 1050;
  animation: modalIn .18s ease-out; }
@keyframes modalIn { from { opacity: 0; transform: translateY(-14px); } to { opacity: 1; transform: none; } }
.modal-header { padding: 9px 15px; border-bottom: 1px solid #2a2e35; }
.modal-header h3 { margin: 0; font-size: 18px; line-height: 27px; color: #fff; }
.modal-header .close { float: right; color: #8a93a0; font-size: 20px; font-weight: bold;
  text-decoration: none; margin-top: 2px; }
.modal-header .close:hover { color: #fff; }
.modal-body { padding: 15px; max-height: 68vh; overflow-y: auto; }
.modal-footer { padding: 14px 15px 15px; border-top: 1px solid #2a2e35; background: #171a1f;
  border-radius: 0 0 6px 6px; text-align: right; }
.modal-footer .btn { margin-left: 6px; }
#mAdmin { width: 860px; margin-left: -430px; }

/* ===== hero-unit (экран входа) ===== */
.hero-unit { padding: 50px 60px; background: #171a1f; border: 1px solid #2a2e35;
  border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,.5); margin: 40px 0 20px; }
.hero-unit h1 { font-size: 38px; line-height: 1; letter-spacing: -1px; margin-bottom: 14px; color: #fff; }
.hero-unit p { font-size: 15px; line-height: 22px; color: #aab3bd; }

/* ===== прочее ===== */
.swatch { display: inline-block; width: 22px; height: 22px; border-radius: 4px; margin: 0 4px 4px 0;
  cursor: pointer; border: 2px solid transparent; vertical-align: middle; }
.swatch.sel { border-color: #fff; }
.stat-card { background: #1c2026; border: 1px solid #2a2e35; border-radius: 6px;
  padding: 12px; text-align: center; }
.stat-card .num { font-size: 26px; font-weight: bold; color: #049cdb; }
.stat-card .cap { color: #8a93a0; font-size: 11px; text-transform: uppercase; }
.search-item { padding: 8px 10px; border-bottom: 1px solid #22262c; cursor: pointer; }
.search-item:hover { background: #1e232a; }
.fade { opacity: 0; transition: opacity .35s ease-in; }
.fade.in { opacity: 1; }
body.compact .msg { padding: 2px 8px; }
body.compact .avatar { width: 24px; height: 24px; line-height: 24px; font-size: 11px; }
body.compact .msg-body { margin-left: 30px; }
::-webkit-scrollbar { width: 9px; height: 9px; }
::-webkit-scrollbar-thumb { background: #33383f; border-radius: 5px; }
::-webkit-scrollbar-track { background: #12151a; }
</style>
</head>
<body>

<div class="topbar"><div class="topbar-inner container">
  <a class="brand" href="#" onclick="location.reload();return false">💬 <span id="brandName">__SERVER_NAME__</span></a>
  <form onsubmit="doSearch();return false"><input type="text" id="qSearch" placeholder="Поиск сообщений…"></form>
  <ul class="nav">
    <li><a href="#" id="topAdmin" style="display:none" onclick="openAdmin();return false">⚙ Админка</a></li>
    <li><a href="#" onclick="openSettings();return false">👤 <span id="meName"></span> <span class="dot offline" id="meDot"></span></a></li>
    <li><a href="#" onclick="logout();return false">Выйти</a></li>
  </ul>
</div></div>

<div id="alerts"></div>

<!-- ================= ЭКРАН ВХОДА ================= -->
<div id="screenAuth" class="container" style="display:none">
  <div class="hero-unit">
    <div style="text-align:center">
      <h1>💬 <span id="authName">__SERVER_NAME__</span></h1>
      <p id="authMotd">__MOTD__</p>
    </div>
    <div class="row" style="margin-top:26px">
      <div class="span7">
        <h2>Вход</h2>
        <form onsubmit="doLogin(event)">
          <div class="clearfix">
            <label for="lUser">Логин</label>
            <input class="input xlarge" id="lUser" type="text" autocomplete="username" required>
          </div>
          <div class="clearfix">
            <label for="lPass">Пароль</label>
            <input class="input xlarge" id="lPass" type="password" autocomplete="current-password" required>
          </div>
          <div class="actions"><button class="btn primary" type="submit">Войти</button></div>
        </form>
      </div>
      <div class="span7">
        <h2>Регистрация</h2>
        <form onsubmit="doRegister(event)">
          <div class="clearfix">
            <label for="rUser">Логин (3–20: латиница, цифры, _)</label>
            <input class="input xlarge" id="rUser" type="text" autocomplete="username" required>
          </div>
          <div class="clearfix">
            <label for="rName">Отображаемое имя</label>
            <input class="input xlarge" id="rName" type="text" placeholder="Как вас звать в чате">
          </div>
          <div class="clearfix">
            <label for="rPass">Пароль (минимум 4 символа)</label>
            <input class="input xlarge" id="rPass" type="password" autocomplete="new-password" required>
          </div>
          <div class="clearfix">
            <label for="rPass2">Пароль ещё раз</label>
            <input class="input xlarge" id="rPass2" type="password" autocomplete="new-password" required>
          </div>
          <div class="actions"><button class="btn success" type="submit">Создать аккаунт</button></div>
        </form>
      </div>
    </div>
    <p class="muted-text" style="text-align:center;margin-top:24px">
      Первый зарегистрированный становится <b>владельцем</b> сервера. Все данные живут в оперативной памяти и сбрасываются при перезапуске.
    </p>
  </div>
</div>

<!-- ================= ОСНОВНОЙ ЭКРАН ================= -->
<div id="screenApp" class="container" style="display:none">
  <div class="row" style="margin-top:14px">
    <div class="span4">
      <div class="side">
        <div class="panel-head">Каналы
          <span style="float:right">
            <a href="#" class="mini-btn" title="Создать канал" onclick="openNewChannel();return false">＋</a>
            <a href="#" class="mini-btn" title="Присоединиться по имени" onclick="openJoinByName();return false">➜</a>
          </span>
        </div>
        <div id="chanList" class="chan-list"></div>
      </div>
    </div>
    <div class="span9">
      <div class="side chat-panel">
        <div class="panel-head" id="chatHead">…</div>
        <div id="pinnedBox" style="display:none"></div>
        <div id="msgs"></div>
        <div class="composer">
          <div id="typing" class="muted-text" style="height:16px"></div>
          <textarea class="input" id="composerText" rows="3" style="margin-bottom:4px"
            placeholder="Сообщение… (Enter — отправить, Shift+Enter — новая строка)"></textarea>
          <div class="clearfix" style="margin-bottom:0">
            <div class="emoji-row" style="float:left;padding-top:3px">
              <span onclick="insEmoji('👍')">👍</span><span onclick="insEmoji('❤️')">❤️</span>
              <span onclick="insEmoji('😂')">😂</span><span onclick="insEmoji('😮')">😮</span>
              <span onclick="insEmoji('😢')">😢</span><span onclick="insEmoji('🔥')">🔥</span>
              <span onclick="insEmoji('👌')">👌</span><span onclick="insEmoji('✅')">✅</span>
              <span onclick="insEmoji('🤝')">🤝</span><span onclick="insEmoji('🎉')">🎉</span>
            </div>
            <button class="btn primary" style="float:right" onclick="sendMsg()">Отправить</button>
          </div>
        </div>
      </div>
    </div>
    <div class="span3">
      <div class="side">
        <div class="panel-head">Пользователи <span id="onlineCount" class="muted-text"></span></div>
        <div style="padding:8px"><input class="input" id="uSearch" style="width:100%;margin-bottom:0" placeholder="Фильтр…" oninput="renderUsers()"></div>
        <div id="userList" class="user-list"></div>
      </div>
      <div class="side">
        <div class="panel-head">Сервер</div>
        <div id="svInfo" class="panel-body muted-text"></div>
      </div>
    </div>
  </div>
</div>

<div id="backdrop" class="modal-backdrop" style="display:none"></div>

<!-- ================= МОДАЛКИ ================= -->
<div class="modal" id="mSettings">
  <div class="modal-header"><h3>Настройки профиля</h3><a class="close" href="#" onclick="hideModal('mSettings')">×</a></div>
  <div class="modal-body">
    <div class="clearfix"><label>Отображаемое имя</label><input class="input xlarge" id="sName"></div>
    <div class="clearfix">
      <label>Статус</label>
      <select class="input" id="sStatus">
        <option value="online">В сети</option><option value="away">Отошёл</option><option value="busy">Занят</option>
      </select>
    </div>
    <div class="clearfix"><label>Цвет аватара</label><div id="colorSwatches"></div></div>
    <div class="clearfix">
      <div class="checkbox-line"><input type="checkbox" id="sSound"> Звук новых сообщений</div>
      <div class="checkbox-line"><input type="checkbox" id="sCompact"> Компактный режим</div>
      <div class="checkbox-line"><input type="checkbox" id="sSys"> Показывать системные сообщения</div>
    </div>
    <div class="well">
      <label>Сменить пароль</label>
      <input class="input" id="sOldPass" type="password" placeholder="Текущий пароль">
      <input class="input" id="sNewPass" type="password" placeholder="Новый пароль" style="margin-bottom:0">
      <div class="actions" style="padding-top:10px"><button class="btn small" onclick="changePassword()">Сменить</button></div>
    </div>
  </div>
  <div class="modal-footer"><button class="btn" onclick="hideModal('mSettings')">Отмена</button>
    <button class="btn primary" onclick="saveSettings()">Сохранить</button></div>
</div>

<div class="modal" id="mNewChan">
  <div class="modal-header"><h3>Создать канал</h3><a class="close" href="#" onclick="hideModal('mNewChan')">×</a></div>
  <div class="modal-body">
    <div class="clearfix"><label>Имя канала</label><input class="input xlarge" id="cName" placeholder="например: новости"></div>
    <div class="clearfix"><label>Описание</label><input class="input xlarge" id="cDesc"></div>
    <div class="clearfix">
      <label>Тип</label>
      <select class="input" id="cType" onchange="document.getElementById('cPassWrap').style.display=this.value==='private'?'block':'none'">
        <option value="public">Публичный — виден всем</option>
        <option value="private">Приватный — вход по имени (и паролю)</option>
      </select>
    </div>
    <div class="clearfix" id="cPassWrap" style="display:none">
      <label>Пароль канала (необязательно)</label>
      <input class="input xlarge" id="cPass" type="password" placeholder="минимум 4 символа">
    </div>
  </div>
  <div class="modal-footer"><button class="btn" onclick="hideModal('mNewChan')">Отмена</button>
    <button class="btn primary" onclick="createChannel()">Создать</button></div>
</div>

<div class="modal" id="mJoin">
  <div class="modal-header"><h3>Присоединиться к приватному каналу</h3><a class="close" href="#" onclick="hideModal('mJoin')">×</a></div>
  <div class="modal-body">
    <div class="clearfix"><label>Имя канала</label><input class="input xlarge" id="jName"></div>
    <div class="clearfix"><label>Пароль (если есть)</label><input class="input xlarge" id="jPass" type="password"></div>
  </div>
  <div class="modal-footer"><button class="btn" onclick="hideModal('mJoin')">Отмена</button>
    <button class="btn primary" onclick="joinByName()">Войти в канал</button></div>
</div>

<div class="modal" id="mProfile">
  <div class="modal-header"><h3>Профиль</h3><a class="close" href="#" onclick="hideModal('mProfile')">×</a></div>
  <div class="modal-body">
    <div class="avatar" id="pAvatar" style="width:48px;height:48px;line-height:48px;font-size:20px;margin:0 auto;float:none;display:block">?</div>
    <div style="text-align:center;margin-top:8px">
      <b id="pName" style="font-size:16px;color:#fff"></b>
      <div id="pUser" class="muted-text"></div>
    </div>
    <div id="pInfo" class="well" style="margin-top:12px"></div>
    <div id="pAdminBox" style="display:none;margin-top:12px">
      <h4>Действия администратора</h4>
      <div id="pAdminButtons" style="margin-bottom:8px"></div>
      <div class="clearfix" style="margin-bottom:0">
        <label>Роль</label>
        <select class="input" id="pRole" onchange="adminUserAction(profileUid,{role:this.value})">
          <option value="user">Пользователь</option><option value="mod">Модер</option>
          <option value="admin">Админ</option>
        </select>
      </div>
    </div>
  </div>
  <div class="modal-footer"><button class="btn" onclick="hideModal('mProfile')">Закрыть</button>
    <button class="btn primary" id="btnDm" onclick="startDm()">Написать ЛС</button></div>
</div>

<div class="modal" id="mChanSettings">
  <div class="modal-header"><h3>Настройки канала</h3><a class="close" href="#" onclick="hideModal('mChanSettings')">×</a></div>
  <div class="modal-body">
    <div class="clearfix"><label>Имя</label><input class="input xlarge" id="csName"></div>
    <div class="clearfix"><label>Описание</label><input class="input xlarge" id="csDesc"></div>
    <div class="actions">
      <button class="btn small warning" onclick="clearActiveChannel()">Очистить историю</button>
      <button class="btn small danger" onclick="deleteActiveChannel()">Удалить канал</button>
    </div>
  </div>
  <div class="modal-footer"><button class="btn" onclick="hideModal('mChanSettings')">Отмена</button>
    <button class="btn primary" onclick="saveChanSettings()">Сохранить</button></div>
</div>

<div class="modal" id="mSearch">
  <div class="modal-header"><h3>Результаты поиска</h3><a class="close" href="#" onclick="hideModal('mSearch')">×</a></div>
  <div class="modal-body" id="searchResults"></div>
  <div class="modal-footer"><button class="btn" onclick="hideModal('mSearch')">Закрыть</button></div>
</div>

<div class="modal" id="mAdmin">
  <div class="modal-header"><h3>Панель администратора</h3><a class="close" href="#" onclick="hideModal('mAdmin')">×</a></div>
  <div class="modal-body">
    <ul class="pills">
      <li class="active" data-tab="Server"><a href="#" onclick="switchAdminTab('Server');return false">Сервер</a></li>
      <li data-tab="Users"><a href="#" onclick="switchAdminTab('Users');return false">Пользователи</a></li>
      <li data-tab="Channels"><a href="#" onclick="switchAdminTab('Channels');return false">Каналы</a></li>
      <li data-tab="Stats"><a href="#" onclick="switchAdminTab('Stats');return false">Статистика</a></li>
      <li data-tab="Danger"><a href="#" onclick="switchAdminTab('Danger');return false">Опасная зона</a></li>
    </ul>
    <div class="pill-content">
      <div id="tabServer" class="active">
        <div class="clearfix"><label>Название сервера</label><input class="input xlarge" id="asName"></div>
        <div class="clearfix"><label>MOTD (приветствие)</label><textarea class="input" id="asMotd" rows="2"></textarea></div>
        <div class="row" style="margin-left:0">
          <div style="float:left;width:220px;margin-right:20px">
            <label>Макс. длина сообщения</label><input class="input" id="asMaxLen" type="number" style="width:100%">
          </div>
          <div style="float:left;width:220px;margin-right:20px">
            <label>История на канал</label><input class="input" id="asHistory" type="number" style="width:100%">
          </div>
          <div style="float:left;width:220px">
            <label>Лимит сообщений/мин</label><input class="input" id="asRate" type="number" style="width:100%">
          </div>
        </div>
        <div class="clearfix" style="margin-top:8px">
          <div class="checkbox-line"><input type="checkbox" id="asReg"> Разрешить регистрацию</div>
          <div class="checkbox-line"><input type="checkbox" id="asMaint"> Режим обслуживания</div>
          <div class="checkbox-line"><input type="checkbox" id="asEdit"> Разрешить редактирование</div>
          <div class="checkbox-line"><input type="checkbox" id="asReact"> Разрешить реакции</div>
          <div class="checkbox-line"><input type="checkbox" id="asDm"> Разрешить личные сообщения</div>
        </div>
        <div class="actions"><button class="btn primary" onclick="saveServerSettings()">Сохранить настройки</button></div>
        <div class="well">
          <label>📢 Объявление на весь сервер</label>
          <input class="input" id="asBroadcast" style="width:100%;margin-bottom:8px" placeholder="Текст объявления…">
          <button class="btn warning" onclick="broadcast()">Отправить</button>
        </div>
      </div>
      <div id="tabUsers">
        <table class="zebra-striped">
          <thead><tr><th>Пользователь</th><th>Роль</th><th>Статус</th><th>Действия</th></tr></thead>
          <tbody id="adminUsers"></tbody>
        </table>
      </div>
      <div id="tabChannels">
        <table class="zebra-striped">
          <thead><tr><th>Канал</th><th>Тип</th><th>Сообщений</th><th>Участников</th><th>Владелец</th><th>Действия</th></tr></thead>
          <tbody id="adminChannels"></tbody>
        </table>
      </div>
      <div id="tabStats"><div class="row" id="adminStats" style="margin-left:0"></div></div>
      <div id="tabDanger">
        <div class="alert-message error">Внимание! Эти действия необратимы.</div>
        <div class="well">
          <b>Полный сброс сервера</b> — удаляются все пользователи, каналы и сообщения.
          Ваш аккаунт останется, вы станете владельцем.
          <div class="actions"><button class="btn danger" onclick="resetServer()">Сбросить всё</button></div>
        </div>
      </div>
    </div>
  </div>
  <div class="modal-footer"><button class="btn" onclick="hideModal('mAdmin')">Закрыть</button></div>
</div>

<script>
"use strict";
var $ = function(s){ return document.querySelector(s); };
var $$ = function(s){ return Array.prototype.slice.call(document.querySelectorAll(s)); };

var TOKEN = localStorage.getItem('dshtm_token') || '';
var ME = null, USERS = [], CHANNELS = [], MESSAGES = [];
var activeCid = null, lastMsgId = 0, lastEvtId = 0, SERVER = {};
var unread = {}, msgEls = {}, MSG_IDS = {}, typingSent = 0, pollTimer = null, profileUid = null;

var ROLE_NAMES = {owner:'Владелец', admin:'Админ', mod:'Модер', user:'Пользователь'};
var PALETTE = ['#e74c3c','#e67e22','#f1c40f','#2ecc71','#1abc9c','#3498db','#9b59b6','#e84393','#fd79a8','#00cec9','#6c5ce7','#fdcb6e'];
var QUICK_EMOJI = ['👍','❤️','😂','😮','😢','🔥','👌','✅'];

var lastSeen = {};
try { lastSeen = JSON.parse(localStorage.getItem('dshtm_seen')||'{}') || {}; } catch(e) { lastSeen = {}; }

function esc(s){
  return String(s==null?'':s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function fmtTime(ts){ var d=new Date(ts*1000), p=function(n){return String(n).padStart(2,'0');}; return p(d.getHours())+':'+p(d.getMinutes()); }
function fmtDate(ts){ var d=new Date(ts*1000), p=function(n){return String(n).padStart(2,'0');}; return p(d.getDate())+'.'+p(d.getMonth()+1)+'.'+d.getFullYear(); }
function fmtDur(sec){ sec=Math.max(0,Math.floor(sec)); var h=Math.floor(sec/3600), m=Math.floor(sec%3600/60), s=sec%60;
  return (h?h+'ч ':'')+(m?m+'м ':'')+s+'с'; }

function showAlert(type, text, ms){
  var box=$('#alerts'), el=document.createElement('div');
  el.className='alert-message '+type+' fade';
  el.innerHTML=esc(text)+'<a class="alert-close" href="#" onclick="this.parentNode.remove()">×</a>';
  box.appendChild(el);
  setTimeout(function(){ el.classList.add('in'); }, 10);
  setTimeout(function(){ el.classList.remove('in'); setTimeout(function(){ el.remove(); }, 400); }, ms||5000);
}

var AC=null;
function beep(freq){
  if(!ME || !ME.settings || !ME.settings.sound) return;
  try {
    AC=AC||new (window.AudioContext||window.webkitAudioContext)();
    if(AC.state==='suspended') AC.resume();
    var o=AC.createOscillator(), g=AC.createGain();
    o.type='sine'; o.frequency.value=freq||880;
    g.gain.setValueAtTime(0.05, AC.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, AC.currentTime+0.28);
    o.connect(g); g.connect(AC.destination);
    o.start(); o.stop(AC.currentTime+0.3);
  } catch(e) {}
}

function api(path, method, body){
  method = method || 'GET';
  var opt = {method: method, headers: {'Content-Type':'application/json', 'Authorization':'Bearer '+TOKEN}};
  if(body) opt.body = JSON.stringify(body);
  return fetch('/api'+path, opt).then(function(res){
    return res.json().catch(function(){ return null; }).then(function(data){
      if(res.status===401){ doLogout(); throw new Error('session'); }
      if(res.status===403 && data && String(data.detail||'').indexOf('BANNED')===0){
        doLogout('Вы забанены: '+String(data.detail).slice(7));
        throw new Error('session');
      }
      if(!res.ok){ throw new Error((data&&(data.detail||data.error))||('Ошибка '+res.status)); }
      return data;
    });
  }).catch(function(e){
    if(e.message!=='session') showAlert('error', e.message, 6000);
    throw e;
  });
}

function showScreen(which){
  $('#screenAuth').style.display = which==='auth' ? 'block' : 'none';
  $('#screenApp').style.display = which==='app' ? 'block' : 'none';
}

function doLogout(msg){
  TOKEN=''; localStorage.removeItem('dshtm_token');
  ME=null; CHANNELS=[]; MESSAGES=[]; activeCid=null; msgEls={}; MSG_IDS={};
  if(pollTimer){ clearInterval(pollTimer); pollTimer=null; }
  showScreen('auth');
  if(msg) showAlert('error', msg, 8000);
}

function logout(){ try { api('/logout','POST'); } catch(e){} doLogout(); }

/* ---------- вход / регистрация ---------- */
function doLogin(ev){
  ev.preventDefault();
  api('/login','POST',{username:$('#lUser').value.trim(), password:$('#lPass').value})
    .then(function(d){ TOKEN=d.token; localStorage.setItem('dshtm_token',TOKEN); ME=d.user;
      showScreen('app'); startApp(); })
    .catch(function(){});
}
function doRegister(ev){
  ev.preventDefault();
  if($('#rPass').value!==$('#rPass2').value){ showAlert('error','Пароли не совпадают'); return; }
  api('/register','POST',{username:$('#rUser').value.trim(), name:$('#rName').value.trim(),
      password:$('#rPass').value})
    .then(function(d){ TOKEN=d.token; localStorage.setItem('dshtm_token',TOKEN); ME=d.user;
      showScreen('app'); startApp(); })
    .catch(function(){});
}

/* ---------- основной цикл ---------- */
function startApp(){
  if(pollTimer) clearInterval(pollTimer);
  poll();
  pollTimer = setInterval(poll, 2000);
}
function poll(){
  if(!TOKEN) return;
  api('/sync?channel='+(activeCid||'')+'&after_msg='+lastMsgId+'&after_evt='+lastEvtId)
    .then(function(d){
      if(d.status==='maintenance'){ showAlert('warning','Сервер на обслуживании. Данные не обновляются.', 0); return; }
      SERVER=d.server; ME=d.me; USERS=d.users; CHANNELS=d.channels;
      $('#brandName').textContent=SERVER.name;
      $('#meName').textContent=ME.name;
      var dot=$('#meDot'); dot.className='dot '+(ME.status==='busy'?'busy':(ME.status==='away'?'away':'online'));
      $('#topAdmin').style.display=(ME.role==='admin'||ME.role==='owner')?'':'none';
      document.body.classList.toggle('compact', !!ME.settings.compact);
      document.title=SERVER.name;
      handleEvents(d.events||[]);
      computeUnread(); renderChannels(); renderUsers(); renderServerInfo();
      if(d.messages && d.messages.length) appendMessages(d.messages);
      renderTyping(d.typing||[]);
      if(activeCid===null && CHANNELS.length) switchChannel(CHANNELS[0].id);
      renderChatHeader();
    }).catch(function(){});
}
function handleEvents(events){
  if(!events || !events.length) return;
  var maxId=0; events.forEach(function(e){ if(e.id>maxId) maxId=e.id; });
  lastEvtId=Math.max(lastEvtId, maxId);
  events.forEach(function(ev){
    var d=ev.data||{};
    if(ev.type==='channel_deleted'){
      if(d.channel_id===activeCid){ activeCid=null; $('#msgs').innerHTML=''; }
    }
    if(ev.type==='channel_cleared' && d.channel_id===activeCid){
      MESSAGES=[]; MSG_IDS={}; msgEls={}; $('#msgs').innerHTML='<div class="muted-text" style="text-align:center;padding:20px">История очищена</div>';
    }
    if(ev.type==='kicked') doLogout('Вас исключили: '+(d.reason||''));
    if(ev.type==='banned') doLogout('Вы забанены: '+(d.reason||''));
    if(ev.type==='muted') showAlert('warning','Вы заглушены до '+d.until_str);
    if(ev.type==='unmuted') showAlert('success','Заглушение снято');
    if(ev.type==='broadcast') showAlert('info','📢 '+d.text);
    if(ev.type==='role_changed') showAlert('info','Ваша роль изменена на '+ROLE_NAMES[d.role]);
  });
}

/* ---------- каналы ---------- */
function displayName(ch){ return ch.type==='dm' ? ('ЛС: '+esc(ch.other_name||'?')) : ch.name; }
function computeUnread(){
  var nowTs=Math.floor(Date.now()/1000), seen={};
  CHANNELS.forEach(function(ch){
    if(ch.id===activeCid){ lastSeen[ch.id]=nowTs; }
    else if(ch.activity && ch.activity > (lastSeen[ch.id]||0)) seen[ch.id]=1;
  });
  unread=seen;
  localStorage.setItem('dshtm_seen', JSON.stringify(lastSeen));
}
function renderChannels(){
  var box=$('#chanList'); box.innerHTML='';
  CHANNELS.forEach(function(ch){
    var el=document.createElement('div');
    el.className='chan-item'+(ch.id===activeCid?' active':'');
    var icon = ch.type==='dm' ? '✉' : (ch.type==='private' ? '🔒' : '#');
    el.innerHTML='<span class="chan-icon">'+icon+'</span><span class="chan-name">'+displayName(ch)+'</span>'+
      (unread[ch.id]?'<span class="label important">'+unread[ch.id]+'</span>':'')+
      (ch.type==='private'&&ch.has_password?' <span class="muted-text">🔑</span>':'');
    el.onclick=function(){ switchChannel(ch.id); };
    box.appendChild(el);
  });
  if(!CHANNELS.length) box.innerHTML='<div class="muted-text" style="padding:8px">Каналов нет</div>';
}
function switchChannel(cid){
  if(cid===activeCid) return;
  activeCid=cid;
  MESSAGES=[]; MSG_IDS={}; msgEls={}; lastMsgId=0;
  $('#msgs').innerHTML='<div class="muted-text" style="text-align:center;padding:20px">Загрузка…</div>';
  lastSeen[cid]=Math.floor(Date.now()/1000);
  localStorage.setItem('dshtm_seen', JSON.stringify(lastSeen));
  computeUnread(); renderChannels(); renderChatHeader();
  api('/channels/'+cid+'/messages?limit=60').then(function(d){
    if(activeCid!==cid) return;
    MESSAGES=d.messages||[]; MSG_IDS={};
    MESSAGES.forEach(function(m){ MSG_IDS[m.id]=true; });
    lastMsgId = MESSAGES.length ? MESSAGES[MESSAGES.length-1].id : 0;
    renderAllMessages();
    var ta=$('#composerText'); if(ta) ta.focus();
  }).catch(function(){});
}
function renderChatHeader(){
  if(activeCid===null) return;
  var ch=null; CHANNELS.forEach(function(c){ if(c.id===activeCid) ch=c; });
  if(!ch){ $('#chatHead').innerHTML='…'; return; }
  var btns='';
  var canMan = (ME.role==='admin'||ME.role==='owner'||ch.owner_id===ME.id);
  if(canMan) btns+='<a href="#" class="mini-btn" title="Настройки канала" onclick="openChanSettings()">⚙</a>';
  btns+='<a href="#" class="mini-btn" title="Экспорт истории" onclick="exportChan()">⇩</a>';
  if(ch.type==='private' && canMan && ME.role!=='admin' && ME.role!=='owner') btns+='<a href="#" class="mini-btn" title="Покинуть канал" onclick="leaveActive()">🚪</a>';
  var typeName = ch.type==='public' ? 'публичный' : (ch.type==='private' ? 'приватный' : 'личные сообщения');
  var lbl = ch.type==='private' ? ' <span class="label warning">приватный</span>' : (ch.type==='dm'?' <span class="label notice">ЛС</span>':'');
  var title = ch.type==='dm' ? ('✉ '+displayName(ch)) : ('#'+ch.name);
  $('#chatHead').innerHTML='<span style="float:right">'+btns+'</span>'+esc(title)+lbl+
    '<div class="muted-text" style="font-weight:normal;font-size:11px">'+
    esc(ch.description||'')+' · '+ch.member_count+' уч. · '+typeName+'</div>';
  renderPinned(ch);
}
function renderPinned(ch){
  var box=$('#pinnedBox'), pins=ch.pinned||[];
  if(!pins.length){ box.style.display='none'; return; }
  box.style.display='block';
  var html='<div class="panel-head" style="border-radius:0">📌 Закреплено ('+pins.length+')</div>';
  pins.forEach(function(p){
    html+='<div class="pin-item" onclick="jumpToMsg('+activeCid+','+p.id+')"><b>'+esc(p.author)+':</b> '+esc(p.text)+'</div>';
  });
  box.innerHTML=html;
}
function openNewChannel(){ $('#cName').value=''; $('#cDesc').value=''; $('#cPass').value=''; showModal('mNewChan'); }
function createChannel(){
  var type=$('#cType').value;
  api('/channels','POST',{name:$('#cName').value, description:$('#cDesc').value, type:type, password:$('#cPass').value})
    .then(function(d){
      hideModal('mNewChan');
      showAlert('success','Канал создан');
      switchChannel(d.channel.id);
    }).catch(function(){});
}
function openJoinByName(){ $('#jName').value=''; $('#jPass').value=''; showModal('mJoin'); }
function joinByName(){
  api('/channels/join','POST',{name:$('#jName').value, password:$('#jPass').value})
    .then(function(d){ hideModal('mJoin'); switchChannel(d.channel.id); })
    .catch(function(){});
}
function openChanSettings(){
  var ch=null; CHANNELS.forEach(function(c){ if(c.id===activeCid) ch=c; });
  if(!ch) return;
  $('#csName').value=ch.name; $('#csDesc').value=ch.description||'';
  showModal('mChanSettings');
}
function saveChanSettings(){
  api('/channels/'+activeCid,'PATCH',{name:$('#csName').value, description:$('#csDesc').value})
    .then(function(){ hideModal('mChanSettings'); showAlert('success','Канал обновлён'); })
    .catch(function(){});
}
function clearActiveChannel(){
  if(!confirm('Очистить всю историю канала?')) return;
  api('/channels/'+activeCid+'/clear','POST')
    .then(function(){ hideModal('mChanSettings'); showAlert('success','История очищена'); })
    .catch(function(){});
}
function deleteActiveChannel(){
  if(!confirm('Удалить канал навсегда?')) return;
  api('/channels/'+activeCid,'DELETE')
    .then(function(){ hideModal('mChanSettings'); activeCid=null; showAlert('success','Канал удалён'); })
    .catch(function(){});
}
function leaveActive(){
  if(!confirm('Покинуть канал?')) return;
  api('/channels/'+activeCid+'/leave','POST')
    .then(function(){ activeCid=null; showAlert('info','Вы покинули канал'); })
    .catch(function(){});
}
function exportChan(){
  window.open('/api/channels/'+activeCid+'/export?token='+encodeURIComponent(TOKEN), '_blank');
}

/* ---------- сообщения ---------- */
function appendMessages(list){
  var added=false, needBeep=false;
  list.forEach(function(m){
    if(MSG_IDS[m.id]){ updateReacts(m); return; }
    MSG_IDS[m.id]=true; MESSAGES.push(m); added=true;
    if(m.author_id!==ME.id && m.kind!=='system') needBeep=true;
  });
  if(!added) return;
  MESSAGES.sort(function(a,b){ return a.id-b.id; });
  lastMsgId = Math.max(lastMsgId, MESSAGES[MESSAGES.length-1].id);
  renderAllMessages();
  if(needBeep) beep(880);
}
function updateReacts(m){
  var el=msgEls[m.id]; if(!el) return;
  var box=el.querySelector('.m-reacts'); if(!box) return;
  renderReacts(box, m);
}
function renderAllMessages(){
  var box=$('#msgs');
  var nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  box.innerHTML=''; msgEls={};
  MESSAGES.forEach(function(m){
    if(m.kind==='system' && ME.settings && !ME.settings.show_system) return;
    var el=msgEl(m); box.appendChild(el); msgEls[m.id]=el;
  });
  if(box.children.length===0) box.innerHTML='<div class="muted-text" style="text-align:center;padding:20px">Сообщений пока нет</div>';
  if(nearBottom || MESSAGES.length<2) box.scrollTop=box.scrollHeight;
}
function msgEl(m){
  var div=document.createElement('div');
  if(m.kind==='system'){
    div.className='msg msys';
    div.innerHTML='<span>📢 '+esc(m.text)+'</span>';
    return div;
  }
  div.className='msg'; div.dataset.id=m.id;
  var mine = m.author_id===ME.id;
  var canMod = (ME.role==='admin'||ME.role==='owner'||ME.role==='mod');
  var acts = '';
  if(mine || canMod){
    acts='<span class="m-actions">';
    if(mine) acts+='<a href="#" class="m-act" data-act="edit" title="Редактировать">✏</a>';
    acts+='<a href="#" class="m-act" data-act="del" title="Удалить">✕</a>';
    if(canMod) acts+='<a href="#" class="m-act" data-act="pin" title="Закрепить">📌</a>';
    acts+='</span>';
  }
  div.innerHTML=
    '<div class="avatar" style="background:'+esc(m.color)+'">'+esc((m.author||'?').charAt(0).toUpperCase())+'</div>'+
    '<div class="msg-body">'+
      '<div class="m-head"><b class="m-name" style="color:'+esc(m.color)+'">'+esc(m.author)+'</b>'+
      '<span class="m-time">'+fmtTime(m.ts)+(m.edited?' · изм.':'')+'</span>'+acts+'</div>'+
      '<div class="m-text">'+esc(m.text).replace(/\n/g,'<br>')+'</div>'+
      '<div class="m-reacts"></div>'+
    '</div>';
  renderReacts(div.querySelector('.m-reacts'), m);
  wireActions(div, m);
  return div;
}
function renderReacts(box, m){
  box.innerHTML='';
  var reacts=m.reactions||{};
  Object.keys(reacts).forEach(function(emo){
    var ids=reacts[emo]||[];
    if(!ids.length) return;
    var chip=document.createElement('span');
    chip.className='react-chip'+(ids.indexOf(ME.id)>=0?' mine':'');
    chip.textContent=emo+' '+ids.length;
    chip.title=ids.map(function(id){ var u=null; USERS.forEach(function(x){ if(x.id===id) u=x; }); return u?u.name:'?'; }).join(', ');
    chip.onclick=function(){ toggleReaction(m.id, emo); };
    box.appendChild(chip);
  });
  var add=document.createElement('span');
  add.className='react-add'; add.textContent='+';
  add.onclick=function(ev){
    ev.stopPropagation();
    var pick=document.createElement('span');
    pick.className='emoji-picker';
    QUICK_EMOJI.forEach(function(emo){
      var s=document.createElement('span'); s.textContent=emo;
      s.onclick=function(){ toggleReaction(m.id, emo); pick.remove(); };
      pick.appendChild(s);
    });
    box.appendChild(pick);
  };
  box.appendChild(add);
}
function toggleReaction(mid, emoji){
  api('/messages/'+mid+'/react','POST',{emoji:emoji}).catch(function(){});
}
function wireActions(div, m){
  div.querySelectorAll('.m-act').forEach(function(a){
    a.onclick=function(ev){
      ev.preventDefault();
      if(a.dataset.act==='edit') startEdit(div, m);
      if(a.dataset.act==='del') delMsg(m);
      if(a.dataset.act==='pin') pinMsg(m);
    };
  });
}
function startEdit(div, m){
  var textEl=div.querySelector('.m-text');
  var ta=document.createElement('textarea');
  ta.className='input'; ta.value=m.text; ta.style.width='100%'; ta.style.marginBottom='4px';
  textEl.replaceWith(ta); ta.focus();
  var done=false;
  function save(){
    if(done) return; done=true;
    var text=ta.value.trim();
    if(!text || text===m.text){ renderAllMessages(); return; }
    api('/messages/'+m.id,'PATCH',{text:text}).then(function(){ renderAllMessages(); }).catch(function(){ renderAllMessages(); });
  }
  ta.onkeydown=function(ev){
    if(ev.key==='Enter' && !ev.shiftKey){ ev.preventDefault(); save(); }
    if(ev.key==='Escape'){ done=true; renderAllMessages(); }
  };
  ta.onblur=save;
}
function delMsg(m){
  if(!confirm('Удалить сообщение?')) return;
  api('/messages/'+m.id,'DELETE').then(function(){
    var i=MESSAGES.indexOf(m); if(i>=0) MESSAGES.splice(i,1);
    delete MSG_IDS[m.id];
    renderAllMessages();
  }).catch(function(){});
}
function pinMsg(m){
  api('/channels/'+activeCid+'/pin','POST',{message_id:m.id}).catch(function(){});
}
function sendMsg(){
  var ta=$('#composerText'); var text=ta.value.trim();
  if(!text || !activeCid) return;
  ta.value='';
  api('/channels/'+activeCid+'/messages','POST',{text:text})
    .then(function(){})
    .catch(function(){ ta.value=text; });
}
function insEmoji(e){ var ta=$('#composerText'); ta.value+=e; ta.focus(); }
var lastTypingTs=0;
function onTyping(){
  if(!activeCid) return;
  var t=Date.now();
  if(t-lastTypingTs<2500) return;
  lastTypingTs=t;
  api('/channels/'+activeCid+'/typing','POST').catch(function(){});
}
function renderTyping(names){
  $('#typing').textContent = names.length ? (names.join(', ')+' печатает…') : '';
}
function jumpToMsg(cid, mid){
  hideModal('mSearch');
  var then=function(){
    api('/channels/'+cid+'/messages?before='+(mid+1)+'&limit=50').then(function(d){
      if(activeCid!==cid) return;
      MESSAGES=d.messages||[]; MSG_IDS={};
      MESSAGES.forEach(function(m){ MSG_IDS[m.id]=true; });
      lastMsgId=Math.max(lastMsgId, mid);
      renderAllMessages();
      var el=msgEls[mid];
      if(el) el.scrollIntoView({block:'center'});
    }).catch(function(){});
  };
  if(cid!==activeCid){ var old=activeCid; activeCid=cid; switchChannel(cid); }
  then();
}

/* ---------- пользователи ---------- */
function renderUsers(){
  var f=($('#uSearch').value||'').toLowerCase();
  var list=USERS.filter(function(u){
    return u.name.toLowerCase().indexOf(f)>=0 || u.username.toLowerCase().indexOf(f)>=0;
  });
  list.sort(function(a,b){ return (b.online-a.online) || a.name.localeCompare(b.name); });
  var box=$('#userList'); box.innerHTML='';
  list.forEach(function(u){
    var el=document.createElement('div'); el.className='user-item';
    var dot = u.banned ? 'offline' : (u.online ? (u.status==='busy'?'busy':(u.status==='away'?'away':'online')) : 'offline');
    el.innerHTML='<span class="dot '+dot+'"></span><span class="u-color" style="color:'+u.color+'">'+esc(u.name)+'</span>'+
      (u.role!=='user'?' <span class="label notice">'+ROLE_NAMES[u.role]+'</span>':'')+
      (u.muted?' <span class="label warning">mute</span>':'');
    el.onclick=function(){ openProfile(u.id); };
    box.appendChild(el);
  });
  var online=USERS.filter(function(u){ return u.online; }).length;
  $('#onlineCount').textContent='('+online+'/'+USERS.length+')';
}
function openProfile(uid){
  var u=null; USERS.forEach(function(x){ if(x.id===uid) u=x; });
  if(!u) return;
  profileUid=uid;
  $('#pAvatar').style.background=u.color;
  $('#pAvatar').textContent=u.name.charAt(0).toUpperCase();
  $('#pName').textContent=u.name;
  $('#pUser').textContent='@'+u.username;
  $('#pInfo').innerHTML='<b>Роль:</b> '+ROLE_NAMES[u.role]+'<br>'+
    '<b>Статус:</b> '+({online:'В сети',away:'Отошёл',busy:'Занят',offline:'Не в сети'}[u.status]||u.status)+'<br>'+
    '<b>В сети:</b> '+(u.online?'да':'нет')+'<br>'+
    '<b>Регистрация:</b> '+fmtDate(u.created)+' '+(fmtTime(u.created));
  $('#btnDm').style.display = (uid!==ME.id && SERVER.allow_dm!==false) ? '' : 'none';
  $('#pAdminBox').style.display = (ME.role==='admin'||ME.role==='owner') ? '' : 'none';
  $('#pRole').value = (u.role==='owner') ? 'admin' : u.role;
  $('#pRole').disabled = (u.role==='owner');
  var btns='';
  btns+='<button class="btn small warning" onclick="adminUserAction(profileUid,{mute_minutes:5})">Мут 5м</button> ';
  btns+='<button class="btn small warning" onclick="adminUserAction(profileUid,{mute_minutes:30})">Мут 30м</button> ';
  btns+='<button class="btn small" onclick="adminUserAction(profileUid,{mute_minutes:0})">Размут</button> ';
  if(!u.banned) btns+='<button class="btn small danger" onclick="adminUserAction(profileUid,{ban:true})">Бан</button> ';
  else btns+='<button class="btn small success" onclick="adminUserAction(profileUid,{ban:false})">Разбан</button> ';
  btns+='<button class="btn small info" onclick="adminUserAction(profileUid,{kick:true})">Кик</button> ';
  $('#pAdminButtons').innerHTML=btns;
  showModal('mProfile');
}
function adminUserAction(uid, patch){
  api('/admin/users/'+uid,'PATCH',patch).then(function(){
    showAlert('success','Готово');
    if(uid===ME.id && patch.kick) doLogout('Сессия завершена');
    loadAdminUsers();
  }).catch(function(){});
}
function startDm(){
  api('/dm','POST',{user_id:profileUid}).then(function(d){
    hideModal('mProfile');
    switchChannel(d.channel.id);
  }).catch(function(){});
}

/* ---------- профиль ---------- */
function openSettings(){
  $('#sName').value=ME.name;
  $('#sStatus').value=ME.status;
  $('#sSound').checked=ME.settings.sound;
  $('#sCompact').checked=ME.settings.compact;
  $('#sSys').checked=ME.settings.show_system;
  $('#sOldPass').value=''; $('#sNewPass').value='';
  var box=$('#colorSwatches'); box.innerHTML='';
  PALETTE.forEach(function(c){
    var el=document.createElement('span');
    el.className='swatch'+(c===ME.color?' sel':'');
    el.style.background=c;
    el.onclick=function(){ $$('#colorSwatches .swatch').forEach(function(x){ x.classList.remove('sel'); }); el.classList.add('sel'); };
    box.appendChild(el);
  });
  showModal('mSettings');
}
function saveSettings(){
  var sel=$('#colorSwatches .swatch.sel');
  var color=sel?sel.style.background:ME.color;
  api('/me','PATCH',{name:$('#sName').value.trim()||ME.name, status:$('#sStatus').value, color:color,
      settings:{sound:$('#sSound').checked, compact:$('#sCompact').checked, show_system:$('#sSys').checked}})
    .then(function(){ hideModal('mSettings'); showAlert('success','Настройки сохранены'); })
    .catch(function(){});
}
function changePassword(){
  api('/me/password','POST',{old_password:$('#sOldPass').value, new_password:$('#sNewPass').value})
    .then(function(){ showAlert('success','Пароль изменён'); $('#sOldPass').value=''; $('#sNewPass').value=''; })
    .catch(function(){});
}

/* ---------- серверная панель ---------- */
function renderServerInfo(){
  var html='<div><b>'+esc(SERVER.name)+'</b></div>';
  html+='<div style="margin-top:4px">'+esc(SERVER.motd||'')+'</div>';
  html+='<div style="margin-top:8px">⏱ Аптайм: <b>'+fmtDur(SERVER.uptime)+'</b></div>';
  html+='<div>👥 Онлайн: <b>'+SERVER.users_online+'</b> из '+SERVER.users_total+'</div>';
  html+='<div>💬 Сообщений: <b>'+SERVER.messages_total+'</b> · каналов: <b>'+SERVER.channels_total+'</b></div>';
  html+='<div>v'+SERVER.version+(SERVER.maintenance?' · <span class="label warning">обслуживание</span>':'')+'</div>';
  $('#svInfo').innerHTML=html;
}

/* ---------- поиск ---------- */
function doSearch(){
  var q=$('#qSearch').value.trim();
  if(q.length<2){ showAlert('warning','Минимум 2 символа'); return; }
  api('/search?q='+encodeURIComponent(q)).then(function(d){
    var box=$('#searchResults'); box.innerHTML='';
    if(!d.results.length){ box.innerHTML='<div class="muted-text" style="padding:10px">Ничего не найдено</div>'; }
    d.results.forEach(function(r){
      var el=document.createElement('div'); el.className='search-item';
      el.innerHTML='<b>#'+esc(r.channel)+'</b> <span class="muted-text">'+esc(r.author)+' · '+fmtDate(r.ts)+' '+fmtTime(r.ts)+'</span>'+
        '<div style="margin-top:2px">'+esc(r.text)+'</div>';
      el.onclick=function(){ jumpToMsg(r.cid, r.id); };
      box.appendChild(el);
    });
    showModal('mSearch');
  }).catch(function(){});
}

/* ---------- админка ---------- */
function openAdmin(){ if(ME.role==='admin'||ME.role==='owner'){ showModal('mAdmin'); loadAdminAll(); } }
function switchAdminTab(name){
  $$('#mAdmin .pills li').forEach(function(li){ li.classList.toggle('active', li.dataset.tab===name); });
  $$('#mAdmin .pill-content > div').forEach(function(d){ d.classList.toggle('active', d.id===('tab'+name)); });
}
function loadAdminAll(){ loadServerSettings(); loadAdminUsers(); loadAdminChannels(); loadAdminStats(); }

function loadServerSettings(){
  api('/admin/settings').then(function(d){
    var s=d.settings;
    $('#asName').value=s.name; $('#asMotd').value=s.motd;
    $('#asMaxLen').value=s.max_message_length; $('#asHistory').value=s.history_limit; $('#asRate').value=s.rate_limit;
    $('#asReg').checked=s.allow_registration; $('#asMaint').checked=s.maintenance_mode;
    $('#asEdit').checked=s.allow_editing; $('#asReact').checked=s.allow_reactions; $('#asDm').checked=s.allow_dm;
  }).catch(function(){});
}
function saveServerSettings(){
  api('/admin/settings','PATCH',{
    name:$('#asName').value, motd:$('#asMotd').value,
    max_message_length:parseInt($('#asMaxLen').value,10)||2000,
    history_limit:parseInt($('#asHistory').value,10)||500,
    rate_limit:parseInt($('#asRate').value,10)||25,
    allow_registration:$('#asReg').checked, maintenance_mode:$('#asMaint').checked,
    allow_editing:$('#asEdit').checked, allow_reactions:$('#asReact').checked, allow_dm:$('#asDm').checked
  }).then(function(){ showAlert('success','Настройки сервера сохранены'); }).catch(function(){});
}
function broadcast(){
  api('/admin/broadcast','POST',{text:$('#asBroadcast').value}).then(function(){
    $('#asBroadcast').value=''; showAlert('success','Объявление отправлено');
  }).catch(function(){});
}
function loadAdminUsers(){
  api('/admin/users').then(function(d){
    var box=$('#adminUsers'); box.innerHTML='';
    d.users.forEach(function(u){
      var tr=document.createElement('tr');
      var status = u.banned ? '<span class="label important">бан</span>' :
        (u.online ? (u.status==='busy'?'<span class="label notice">занят</span>':(u.status==='away'?'<span class="label warning">отошёл</span>':'<span class="label success">онлайн</span>'))
                  : '<span class="label">офлайн</span>');
      var roleSel='<select class="input" style="width:auto;margin-bottom:0" onchange="adminUserAction('+u.id+',{role:this.value})">';
      ['user','mod','admin'].forEach(function(r){
        roleSel+='<option value="'+r+'"'+(u.role===r||(u.role==='owner'&&r==='admin')?' selected':'')+'>'+ROLE_NAMES[r]+'</option>';
      });
      roleSel+='</select>';
      tr.innerHTML='<td><span class="dot '+(u.online?'online':'offline')+'"></span><b style="color:'+u.color+'">'+esc(u.name)+'</b><br><span class="muted-text">@'+esc(u.username)+'</span></td>'+
        '<td>'+roleSel+'</td><td>'+status+(u.muted?' <span class="label warning">mute</span>':'')+'</td>'+
        '<td><button class="btn mini warning" onclick="adminUserAction('+u.id+',{mute_minutes:5})">мут5м</button> '+
        '<button class="btn mini warning" onclick="adminUserAction('+u.id+',{mute_minutes:30})">мут30м</button> '+
        (u.banned?'<button class="btn mini success" onclick="adminUserAction('+u.id+',{ban:false})">разбан</button>'
                 :'<button class="btn mini danger" onclick="adminUserAction('+u.id+',{ban:true})">бан</button>')+
        ' <button class="btn mini info" onclick="adminUserAction('+u.id+',{kick:true})">кик</button></td>';
      box.appendChild(tr);
    });
  }).catch(function(){});
}
function loadAdminChannels(){
  api('/admin/channels').then(function(d){
    var box=$('#adminChannels'); box.innerHTML='';
    d.channels.forEach(function(c){
      var tr=document.createElement('tr');
      tr.innerHTML='<td>#'+esc(c.name)+'</td>'+
        '<td>'+(c.type==='public'?'публичный':(c.type==='private'?'приватный':'ЛС'))+'</td>'+
        '<td>'+c.messages+'</td><td>'+c.members+'</td><td>'+esc(c.owner_name||'—')+'</td>'+
        '<td><button class="btn mini warning" onclick="adminClearChan('+c.id+')">очистить</button> '+
        '<button class="btn mini danger" onclick="adminDelChan('+c.id+')">удалить</button></td>';
      box.appendChild(tr);
    });
  }).catch(function(){});
}
function adminClearChan(cid){
  if(!confirm('Очистить историю канала #'+cid+'?')) return;
  api('/channels/'+cid+'/clear','POST').then(function(){ showAlert('success','Очищено'); loadAdminChannels(); }).catch(function(){});
}
function adminDelChan(cid){
  if(!confirm('Удалить канал #'+cid+'?')) return;
  api('/channels/'+cid,'DELETE').then(function(){ showAlert('success','Удалён'); loadAdminChannels(); }).catch(function(){});
}
function loadAdminStats(){
  api('/admin/stats').then(function(s){
    var cards=[
      ['Аптайм', fmtDur(s.uptime)], ['Онлайн', s.users_online+' / '+s.users_total],
      ['Каналы', s.channels_total], ['Сообщения', s.messages_total],
      ['Версия', s.version], ['Лимит ист.', s.history_limit]
    ];
    var html='';
    cards.forEach(function(c){
      html+='<div style="float:left;width:130px;margin:0 8px 8px 0"><div class="stat-card"><div class="num">'+c[1]+'</div><div class="cap">'+c[0]+'</div></div></div>';
    });
    $('#adminStats').innerHTML=html;
  }).catch(function(){});
}
function resetServer(){
  if(!confirm('Сбросить ВСЁ? Пользователи, каналы и сообщения будут удалены.')) return;
  if(!confirm('Точно? Это необратимо!')) return;
  api('/admin/reset','POST').then(function(d){
    doLogout(d.message||'Сервер сброшен. Войдите заново.');
  }).catch(function(){});
}

/* ---------- модалки ---------- */
function showModal(id){ $('#backdrop').style.display='block'; $('#'+id).style.display='block'; }
function hideModal(id){ $('#'+id).style.display='none'; $('#backdrop').style.display='none'; }

/* ---------- старт ---------- */
$('#composerText').addEventListener('keydown', function(ev){
  if(ev.key==='Enter' && !ev.shiftKey){ ev.preventDefault(); sendMsg(); }
  else onTyping();
});
$('#composerText').addEventListener('input', onTyping);
document.addEventListener('keydown', function(ev){
  if(ev.key==='Escape'){
    $$('.modal').forEach(function(m){ m.style.display='none'; });
    $('#backdrop').style.display='none';
  }
});
fetch('/api/health').then(function(r){ return r.json(); }).then(function(h){
  $('#authName').textContent=h.name; $('#authMotd').textContent=h.motd;
}).catch(function(){});
if(TOKEN){ showScreen('app'); startApp(); } else { showScreen('auth'); }
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;").replace("'", "&#39;"))
    s = STATE["settings"]
    return HTML.replace("__SERVER_NAME__", esc(s["name"])).replace("__MOTD__", esc(s["motd"]))

# ======================================================================
#  ЗАПУСК
# ======================================================================
if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print("=" * 62)
    print("  DSH Messenger  v" + APP_VERSION)
    print("  UI: тёмная тема в духе Twitter Bootstrap 1.4.0")
    print("  Хранилище: оперативная память (сброс при перезапуске)")
    print("  Адрес:   http://" + host + ":" + str(port))
    print("  Остановить: Ctrl+C")
    print("=" * 62)
    uvicorn.run(app, host=host, port=port, log_level="info")
