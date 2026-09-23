"""
sldchat — веб-чат (IRC/TeamSpeak-like) + поддержка.
Один файл. Всё в оперативке. Реалтайм через WebSocket.

ENV:
  ADMIN_PASS   пароль админки. Если пусто — /admin отключён.
  PORT         порт (по умолчанию 8080)
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from typing import Optional, Dict, List, Set

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect, HTTPException,
    Header, Depends, Cookie, Response, Request,
)
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, field_validator


# ============================================================
#  КОНФИГ
# ============================================================
ALLOW_CHANNEL_CREATION = True
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,24}$")
MENTION_RE  = re.compile(r"(?<![A-Za-z0-9_\-])@([A-Za-z0-9_\-]{3,24})")
EMAIL_RE    = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,120}\.[^@\s]{2,20}$")

LOGIN_WINDOW   = 60
LOGIN_MAX      = 8
WS_MSG_WINDOW  = 5
WS_MSG_MAX     = 12


# ============================================================
#  ХРАНИЛИЩЕ
# ============================================================
users:    Dict[str, dict] = {}
tokens:   Dict[str, str]  = {}
channels: Dict[str, dict] = {}
messages: Dict[str, List[dict]] = {}

# connections: ws -> {kind: "chat"|"support"|"admin", username?, channel?, sid?, admin_tok?}
connections: Dict[WebSocket, dict] = {}
admin_ws: Set[WebSocket] = set()

# support
support_sessions: Dict[str, dict] = {}     # sid -> session
support_keys: Dict[str, str] = {}          # sid -> secret key

login_attempts: Dict[str, List[float]] = {}
msg_attempts:   Dict[str, List[float]] = {}
admin_sessions: Set[str] = set()

_STARTED_AT = time.time()


def _seed() -> None:
    for cid, name in (("general", "General"), ("random", "Random")):
        channels[cid] = {"id": cid, "name": name, "owner": "system", "created": time.time()}
        messages[cid] = []


_seed()


# ============================================================
#  УТИЛИТЫ
# ============================================================
def _now() -> float:
    return time.time()


def _day_str(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def rate_check(bucket: Dict[str, List[float]], key: str, window: int, limit: int, msg: str) -> None:
    now = _now()
    b = bucket.setdefault(key, [])
    while b and b[0] < now - window:
        b.pop(0)
    if len(b) >= limit:
        raise HTTPException(429, msg)
    b.append(now)


def require_user(x_auth_token: Optional[str] = Header(None)) -> str:
    u = tokens.get(x_auth_token) if x_auth_token else None
    if not u:
        raise HTTPException(401, "Unauthorized")
    return u


def require_admin(sld_admin: Optional[str] = Cookie(None)) -> None:
    if not ADMIN_PASS:
        raise HTTPException(403, "Админка отключена")
    if not sld_admin or sld_admin not in admin_sessions:
        raise HTTPException(401, "Admin unauthorized")


def public_user(username: str) -> dict:
    u = users.get(username.lower())
    if not u:
        return {"username": username, "display_name": username}
    return {"username": u["username"], "display_name": u.get("display_name") or u["username"]}


def _all_online_usernames() -> Set[str]:
    return {i["username"] for i in connections.values()
            if i.get("kind") == "chat" and i.get("username")}


def _channel_members(cid: str) -> List[str]:
    return sorted({i["username"] for i in connections.values()
                   if i.get("kind") == "chat" and i.get("channel") == cid and i.get("username")})


def _mentioned_profiles(msgs: List[dict]) -> Dict[str, dict]:
    mentioned: Set[str] = set()
    for m in msgs:
        for nick in MENTION_RE.findall(m["text"]):
            mentioned.add(nick.lower())
    return {k: public_user(k) for k in mentioned if k in users}


async def _send(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_json(payload)
    except Exception:
        pass


async def broadcast_chat(payload: dict) -> None:
    dead = []
    for ws, info in list(connections.items()):
        if info.get("kind") != "chat":
            continue
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connections.pop(ws, None)


async def broadcast_presence() -> None:
    online = _all_online_usernames()
    by_ch = {cid: len(_channel_members(cid)) for cid in channels}
    await broadcast_chat({
        "type": "presence",
        "total": len(online),
        "channels": by_ch,
        "online_usernames": sorted(online),
    })


async def broadcast_admin_support(payload: dict) -> None:
    dead = []
    for ws in list(admin_ws):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        admin_ws.discard(ws)


# ============================================================
#  МОДЕЛИ
# ============================================================
class RegisterBody(BaseModel):
    username: str
    display_name: str = ""
    password: str
    password2: str

    @field_validator("username")
    @classmethod
    def v_username(cls, v: str) -> str:
        v = v.strip()
        if not USERNAME_RE.match(v):
            raise ValueError("Юзернейм: 3–24 символа, только a-z A-Z 0-9 _ -")
        return v


class LoginBody(BaseModel):
    username: str
    password: str


class ChannelBody(BaseModel):
    name: str


class AdminLoginBody(BaseModel):
    password: str


class ProfileBody(BaseModel):
    display_name: str

    @field_validator("display_name")
    @classmethod
    def v(cls, v: str) -> str:
        v = (v or "").strip()
        if not (1 <= len(v) <= 32):
            raise ValueError("Имя: 1–32 символа")
        return v


class AdminUserEdit(BaseModel):
    display_name: Optional[str] = None
    password: Optional[str] = None


class AdminMessageEdit(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def v(cls, v: str) -> str:
        v = (v or "").strip()
        if not (1 <= len(v) <= 4000):
            raise ValueError("Текст: 1–4000 символов")
        return v


class SupportStartBody(BaseModel):
    name: str
    email: str

    @field_validator("name")
    @classmethod
    def v(cls, v: str) -> str:
        v = (v or "").strip()
        if not (1 <= len(v) <= 40):
            raise ValueError("Имя: 1–40 символов")
        return v

    @field_validator("email")
    @classmethod
    def v2(cls, v: str) -> str:
        v = (v or "").strip()
        if not EMAIL_RE.match(v):
            raise ValueError("Некорректный email")
        return v


class SupportMsgBody(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def v(cls, v: str) -> str:
        v = (v or "").strip()
        if not (1 <= len(v) <= 2000):
            raise ValueError("Текст: 1–2000 символов")
        return v


# ============================================================
#  FASTAPI
# ============================================================
app = FastAPI(title="sldchat")


# ---------- AUTH ----------
def _record_login(username: str, request: Request) -> None:
    u = users.get(username.lower())
    if not u:
        return
    ua = (request.headers.get("user-agent") or "")[:160]
    ip = request.client.host if request.client else ""
    u.setdefault("logins", []).append({"ts": _now(), "ua": ua, "ip": ip})
    if len(u["logins"]) > 200:
        u["logins"] = u["logins"][-200:]
    u["last_seen"] = _now()


@app.post("/api/register")
def register(body: RegisterBody, request: Request):
    ip = request.client.host if request.client else "?"
    rate_check(login_attempts, "reg:" + ip, LOGIN_WINDOW, LOGIN_MAX,
               "Слишком много попыток, попробуйте позже")
    if body.password != body.password2:
        raise HTTPException(400, "Пароли не совпадают")
    if len(body.password) < 6:
        raise HTTPException(400, "Пароль: минимум 6 символов")
    key = body.username.lower()
    if key in users:
        raise HTTPException(409, "Такой юзернейм уже занят")
    salt = secrets.token_hex(8)
    users[key] = {
        "username": body.username,
        "display_name": body.display_name or body.username,
        "salt": salt,
        "hash": hash_pw(body.password, salt),
        "created": _now(),
        "logins": [],
        "last_seen": _now(),
    }
    _record_login(body.username, request)
    token = secrets.token_urlsafe(24)
    tokens[token] = body.username
    return {"token": token, **public_user(body.username)}


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "?"
    rate_check(login_attempts, "login:" + ip, LOGIN_WINDOW, LOGIN_MAX,
               "Слишком много попыток, попробуйте позже")
    key = body.username.strip().lower()
    u = users.get(key)
    if not u or u["hash"] != hash_pw(body.password, u["salt"]):
        raise HTTPException(401, "Неверный юзернейм или пароль")
    _record_login(u["username"], request)
    token = secrets.token_urlsafe(24)
    tokens[token] = u["username"]
    return {"token": token, **public_user(u["username"])}


@app.post("/api/logout")
def logout(x_auth_token: Optional[str] = Header(None)):
    if x_auth_token and x_auth_token in tokens:
        del tokens[x_auth_token]
    return {"ok": True}


@app.get("/api/me")
def me(user: str = Depends(require_user)):
    u = users.get(user.lower(), {})
    u["last_seen"] = _now()
    return public_user(user)


@app.get("/api/config")
def get_config():
    return {"allow_channel_creation": ALLOW_CHANNEL_CREATION}


@app.patch("/api/profile")
async def update_profile(body: ProfileBody, user: str = Depends(require_user)):
    u = users.get(user.lower())
    if not u:
        raise HTTPException(401, "Unauthorized")
    u["display_name"] = body.display_name
    await broadcast_chat({
        "type": "profile_updated",
        "username": u["username"],
        "display_name": body.display_name,
    })
    return public_user(u["username"])


# ---------- CHANNELS ----------
@app.get("/api/channels")
def list_channels(user: str = Depends(require_user)):
    out = []
    for cid, c in channels.items():
        lst = messages.get(cid) or []
        last = lst[-1] if lst else None
        out.append({
            "id": c["id"], "name": c["name"], "owner": c["owner"],
            "online": len(_channel_members(cid)),
            "last_message": ({"user": last["user"], "text": last["text"], "ts": last["ts"]}
                             if last else None),
        })
    out.sort(key=lambda x: (x["last_message"]["ts"] if x["last_message"] else 0), reverse=True)
    return {"channels": out, "total_online": len(_all_online_usernames())}


def _slug(raw: str) -> str:
    return "".join(c for c in raw.lower().replace(" ", "-") if c.isalnum() or c in "-_")


@app.post("/api/channels")
async def create_channel(body: ChannelBody, user: str = Depends(require_user)):
    if not ALLOW_CHANNEL_CREATION:
        raise HTTPException(403, "Создание каналов отключено")
    raw = body.name.strip().lstrip("#").strip()
    if not (1 <= len(raw) <= 32):
        raise HTTPException(400, "Название: 1–32 символа")
    cid = _slug(raw)
    if not cid or cid in channels:
        raise HTTPException(409, "Канал уже существует или некорректен")
    channels[cid] = {"id": cid, "name": raw, "owner": user, "created": _now()}
    messages[cid] = []
    await broadcast_chat({"type": "channels_changed"})
    return {"id": cid, "name": raw}


# ---------- SUPPORT (client) ----------
@app.post("/api/support/start")
def support_start(body: SupportStartBody, request: Request):
    ip = request.client.host if request.client else "?"
    rate_check(login_attempts, "support:" + ip, 300, 6, "Слишком много обращений")
    sid = secrets.token_hex(8)
    key = secrets.token_urlsafe(24)
    ua = (request.headers.get("user-agent") or "")[:180]
    support_sessions[sid] = {
        "id": sid,
        "name": body.name,
        "email": body.email,
        "ip": ip,
        "ua": ua,
        "created": _now(),
        "ended": False,
        "ended_at": None,
        "messages": [],
        "unread_admin": 0,
        "unread_user": 0,
    }
    support_keys[sid] = key
    return {"sid": sid, "key": key, "session": _support_public(sid)}


def _support_public(sid: str) -> dict:
    s = support_sessions.get(sid)
    if not s:
        return {}
    return {
        "id": s["id"], "name": s["name"], "email": s["email"],
        "ip": s["ip"], "ua": s["ua"], "created": s["created"],
        "ended": s["ended"], "ended_at": s["ended_at"],
        "messages": s["messages"],
        "unread_admin": s["unread_admin"], "unread_user": s["unread_user"],
    }


@app.get("/api/support/session")
def support_session(sid: str, key: str):
    if support_keys.get(sid) != key:
        raise HTTPException(401, "Unauthorized")
    return _support_public(sid)


@app.post("/api/support/message")
async def support_message(body: SupportMsgBody, sid: str, key: str):
    s = support_sessions.get(sid)
    if not s or support_keys.get(sid) != key:
        raise HTTPException(401, "Unauthorized")
    if s["ended"]:
        raise HTTPException(400, "Чат завершён")
    msg = {"id": secrets.token_hex(8), "from": "user", "text": body.text, "ts": _now()}
    s["messages"].append(msg)
    s["unread_admin"] += 1
    await _notify_support(sid, msg, "user")
    return msg


async def _notify_support(sid: str, msg: dict, origin: str):
    payload = {"type": "support_message", "sid": sid, "message": msg, "origin": origin}
    # отдать клиенту support по WS
    for ws, info in list(connections.items()):
        if info.get("kind") == "support" and info.get("sid") == sid:
            await _send(ws, payload)
    await broadcast_admin_support(payload)


# ============================================================
#  WEBSOCKET — CHAT
# ============================================================
async def _handle_chat_message(cid: str, username: str, text: str) -> None:
    text = (text or "").strip()
    if not text or len(text) > 4000:
        return
    now = _now()
    b = msg_attempts.setdefault(username, [])
    while b and b[0] < now - WS_MSG_WINDOW:
        b.pop(0)
    if len(b) >= WS_MSG_MAX:
        return
    b.append(now)
    msg = {"id": secrets.token_hex(8), "channel": cid, "user": username,
           "text": text, "ts": now}
    lst = messages.setdefault(cid, [])
    lst.append(msg)
    if len(lst) > 2000:
        del lst[:-2000]
    users.get(username.lower(), {})["last_seen"] = now
    await broadcast_chat({"type": "message", "message": msg})


async def _handle_edit_message(mid: str, username: str, text: str) -> None:
    text = (text or "").strip()
    if not text or len(text) > 4000:
        return
    for cid, lst in messages.items():
        for i, m in enumerate(lst):
            if m["id"] == mid:
                if m["user"] != username:
                    return
                lst[i] = {**m, "text": text, "edited": _now()}
                await broadcast_chat({
                    "type": "message_edited", "id": mid, "channel": cid,
                    "text": text, "edited": lst[i]["edited"],
                })
                return


async def _handle_delete_message(mid: str, username: str) -> None:
    for cid, lst in messages.items():
        for i, m in enumerate(lst):
            if m["id"] == mid:
                if m["user"] != username:
                    return
                lst.pop(i)
                await broadcast_chat({"type": "message_deleted", "id": mid, "channel": cid})
                return


@app.websocket("/ws")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token")
    username = tokens.get(token) if token else None
    if not username:
        await _send(ws, {"type": "session_expired"})
        await ws.close(code=1008)
        return
    users.get(username.lower(), {})["last_seen"] = _now()
    connections[ws] = {"kind": "chat", "username": username, "channel": None}
    await broadcast_presence()
    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type")
            info = connections.get(ws)
            if not info:
                break
            if t == "ping":
                users.get(username.lower(), {})["last_seen"] = _now()
                await _send(ws, {"type": "pong"})
            elif t == "join":
                cid = data.get("channel")
                if cid not in channels:
                    await _send(ws, {"type": "channel_not_found", "channel": cid})
                    continue
                info["channel"] = cid
                msgs = messages.get(cid, [])[-200:]
                await _send(ws, {
                    "type": "channel_joined",
                    "channel": cid,
                    "messages": msgs,
                    "profiles": _mentioned_profiles(msgs),
                    "members": [public_user(u) for u in _channel_members(cid)],
                })
                await broadcast_presence()
            elif t == "leave":
                info["channel"] = None
                await broadcast_presence()
            elif t == "message":
                cid = info.get("channel")
                if cid:
                    await _handle_chat_message(cid, username, data.get("text"))
            elif t == "edit_message":
                await _handle_edit_message(data.get("id"), username, data.get("text"))
            elif t == "delete_message":
                await _handle_delete_message(data.get("id"), username)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        connections.pop(ws, None)
        try:
            await broadcast_presence()
        except Exception:
            pass


# ============================================================
#  WEBSOCKET — SUPPORT (client)
# ============================================================
@app.websocket("/ws/support")
async def ws_support(ws: WebSocket):
    await ws.accept()
    sid = ws.query_params.get("sid")
    key = ws.query_params.get("key")
    s = support_sessions.get(sid)
    if not s or support_keys.get(sid) != key:
        await ws.close(code=1008)
        return
    connections[ws] = {"kind": "support", "sid": sid}
    await _send(ws, {"type": "support_init", "session": _support_public(sid)})
    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ping":
                await _send(ws, {"type": "pong"})
            elif data.get("type") == "message":
                if s["ended"]:
                    await _send(ws, {"type": "error", "error": "ended"})
                    continue
                text = (data.get("text") or "").strip()[:2000]
                if not text:
                    continue
                msg = {"id": secrets.token_hex(8), "from": "user", "text": text, "ts": _now()}
                s["messages"].append(msg)
                s["unread_admin"] += 1
                await _notify_support(sid, msg, "user")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        connections.pop(ws, None)


# ============================================================
#  WEBSOCKET — SUPPORT (admin)
# ============================================================
@app.websocket("/ws/admin_support")
async def ws_admin_support(ws: WebSocket, sld_admin: Optional[str] = Cookie(None)):
    await ws.accept()
    if not ADMIN_PASS or not sld_admin or sld_admin not in admin_sessions:
        await ws.close(code=1008)
        return
    admin_ws.add(ws)
    connections[ws] = {"kind": "admin"}
    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ping":
                await _send(ws, {"type": "pong"})
            elif data.get("type") == "reply":
                sid = data.get("sid")
                text = (data.get("text") or "").strip()[:2000]
                s = support_sessions.get(sid)
                if s and not s["ended"] and text:
                    msg = {"id": secrets.token_hex(8), "from": "admin", "text": text, "ts": _now()}
                    s["messages"].append(msg)
                    s["unread_user"] += 1
                    await _notify_support(sid, msg, "admin")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        admin_ws.discard(ws)
        connections.pop(ws, None)


# ============================================================
#  ADMIN API
# ============================================================
@app.post("/api/admin/login")
def admin_login(body: AdminLoginBody, request: Request, response: Response):
    if not ADMIN_PASS:
        raise HTTPException(403, "Админка отключена")
    ip = request.client.host if request.client else "?"
    rate_check(login_attempts, "admin:" + ip, LOGIN_WINDOW, LOGIN_MAX,
               "Слишком много попыток")
    if not secrets.compare_digest(body.password, ADMIN_PASS):
        raise HTTPException(401, "Неверный пароль")
    token = secrets.token_urlsafe(32)
    admin_sessions.add(token)
    response.set_cookie("sld_admin", token, httponly=True, samesite="strict",
                        max_age=60 * 60 * 12)
    return {"ok": True}


@app.post("/api/admin/logout")
def admin_logout(response: Response, sld_admin: Optional[str] = Cookie(None)):
    if sld_admin:
        admin_sessions.discard(sld_admin)
    response.delete_cookie("sld_admin")
    return {"ok": True}


@app.get("/api/admin/session")
def admin_session(_: None = Depends(require_admin)):
    return {"ok": True}


@app.get("/api/admin/stats")
def admin_stats(_: None = Depends(require_admin)):
    return {
        "users": len(users),
        "channels": len(channels),
        "messages": sum(len(v) for v in messages.values()),
        "online": len(_all_online_usernames()),
        "tokens": len(tokens),
        "support_open": sum(1 for s in support_sessions.values() if not s["ended"]),
        "uptime_started": _STARTED_AT,
    }


# ---- USERS ----
@app.get("/api/admin/users")
def admin_users(_: None = Depends(require_admin)):
    online = _all_online_usernames()
    out = []
    for u in users.values():
        uname = u["username"]
        msg_count = sum(1 for lst in messages.values() for m in lst if m["user"] == uname)
        token_count = sum(1 for n in tokens.values() if n == uname)
        online_in = [cid for cid in channels if uname in _channel_members(cid)]
        out.append({
            "username": uname,
            "display_name": u.get("display_name") or uname,
            "created": u["created"],
            "last_seen": u.get("last_seen", 0),
            "logins_count": len(u.get("logins", [])),
            "tokens": token_count,
            "messages": msg_count,
            "online_in": online_in,
            "is_online": uname in online,
        })
    out.sort(key=lambda x: x["created"], reverse=True)
    return {"users": out}


@app.get("/api/admin/users/{username}")
def admin_user_detail(username: str, _: None = Depends(require_admin)):
    u = users.get(username.lower())
    if not u:
        raise HTTPException(404, "Нет такого пользователя")
    uname = u["username"]

    total = 0
    by_channel: Dict[str, int] = {}
    per_day: Dict[str, int] = {}
    recent: List[dict] = []
    for cid, lst in messages.items():
        for m in lst:
            if m["user"] != uname:
                continue
            total += 1
            by_channel[cid] = by_channel.get(cid, 0) + 1
            per_day[_day_str(m["ts"])] = per_day.get(_day_str(m["ts"]), 0) + 1
            recent.append({**m, "channel_name": channels.get(cid, {}).get("name", cid)})
    recent.sort(key=lambda m: m["ts"], reverse=True)

    logins = u.get("logins", [])
    logins_per_day: Dict[str, int] = {}
    for l in logins:
        d = _day_str(l["ts"])
        logins_per_day[d] = logins_per_day.get(d, 0) + 1

    days: List[str] = []
    now = _now()
    for i in range(13, -1, -1):
        days.append(_day_str(now - i * 86400))

    return {
        "username": uname,
        "display_name": u.get("display_name") or uname,
        "created": u["created"],
        "last_seen": u.get("last_seen", 0),
        "is_online": uname in _all_online_usernames(),
        "total_messages": total,
        "messages_by_channel": by_channel,
        "messages_per_day": per_day,
        "logins_per_day": logins_per_day,
        "days": days,
        "logins": list(reversed(logins[-50:])),
        "recent_messages": recent[:25],
        "online_in": [cid for cid in channels if uname in _channel_members(cid)],
    }


@app.patch("/api/admin/users/{username}")
async def admin_edit_user(username: str, body: AdminUserEdit, _: None = Depends(require_admin)):
    u = users.get(username.lower())
    if not u:
        raise HTTPException(404, "Нет такого пользователя")
    if body.display_name is not None:
        dn = body.display_name.strip()
        if not (1 <= len(dn) <= 32):
            raise HTTPException(400, "Имя: 1–32 символа")
        u["display_name"] = dn
    if body.password:
        if len(body.password) < 6:
            raise HTTPException(400, "Пароль: минимум 6 символов")
        u["salt"] = secrets.token_hex(8)
        u["hash"] = hash_pw(body.password, u["salt"])
        for t in [t for t, n in tokens.items() if n == u["username"]]:
            del tokens[t]
    await broadcast_chat({
        "type": "profile_updated",
        "username": u["username"],
        "display_name": u["display_name"],
    })
    return public_user(u["username"])


@app.delete("/api/admin/users/{username}")
async def admin_delete_user(username: str, _: None = Depends(require_admin)):
    u = users.get(username.lower())
    if not u:
        raise HTTPException(404, "Нет такого пользователя")
    uname = u["username"]

    # Удаляем все сообщения этого пользователя
    removed: List[tuple] = []
    for cid, lst in messages.items():
        kept = []
        for m in lst:
            if m["user"] == uname:
                removed.append((cid, m["id"]))
            else:
                kept.append(m)
        messages[cid] = kept

    # Кик из WS чата
    kicked = 0
    for ws, info in list(connections.items()):
        if info.get("kind") == "chat" and info.get("username") == uname:
            connections.pop(ws, None)
            try:
                await ws.close(code=1008)
            except Exception:
                pass
            kicked += 1

    # Удаляем аккаунт, токены
    del users[username.lower()]
    for t in [t for t, n in tokens.items() if n == uname]:
        del tokens[t]

    # Оповещаем
    for cid, mid in removed:
        await broadcast_chat({"type": "message_deleted", "id": mid, "channel": cid})
    await broadcast_chat({"type": "user_deleted", "username": uname})
    await broadcast_presence()

    return {"ok": True, "kicked": kicked, "messages_removed": len(removed)}


# ---- CHANNELS ----
@app.get("/api/admin/channels")
def admin_channels(_: None = Depends(require_admin)):
    out = []
    for cid, c in channels.items():
        out.append({
            "id": c["id"], "name": c["name"], "owner": c["owner"],
            "created": c["created"],
            "messages": len(messages.get(cid) or []),
            "online": len(_channel_members(cid)),
        })
    out.sort(key=lambda x: x["created"])
    return {"channels": out}


@app.post("/api/admin/channels")
async def admin_create_channel(body: ChannelBody, _: None = Depends(require_admin)):
    raw = body.name.strip().lstrip("#").strip()
    if not (1 <= len(raw) <= 32):
        raise HTTPException(400, "Название: 1–32 символа")
    cid = _slug(raw)
    if not cid or cid in channels:
        raise HTTPException(409, "Канал уже существует или некорректен")
    channels[cid] = {"id": cid, "name": raw, "owner": "admin", "created": _now()}
    messages[cid] = []
    await broadcast_chat({"type": "channels_changed"})
    return {"id": cid, "name": raw}


@app.delete("/api/admin/channels/{cid}")
async def admin_delete_channel(cid: str, _: None = Depends(require_admin)):
    if cid not in channels:
        raise HTTPException(404, "Нет такого канала")
    for ws, info in list(connections.items()):
        if info.get("kind") == "chat" and info.get("channel") == cid:
            info["channel"] = None
            await _send(ws, {"type": "channel_removed", "channel": cid})
    del channels[cid]
    messages.pop(cid, None)
    await broadcast_chat({"type": "channels_changed"})
    await broadcast_presence()
    return {"ok": True}


# ---- MESSAGES ----
@app.get("/api/admin/messages")
def admin_messages(_: None = Depends(require_admin),
                   channel: Optional[str] = None,
                   user: Optional[str] = None,
                   search: Optional[str] = None,
                   limit: int = 500):
    out = []
    for cid, lst in messages.items():
        if channel and cid != channel:
            continue
        for m in lst:
            if user and m["user"].lower() != user.lower():
                continue
            if search and search.lower() not in m["text"].lower():
                continue
            out.append({**m, "channel_name": channels.get(cid, {}).get("name", cid)})
    out.sort(key=lambda m: m["ts"], reverse=True)
    return {"messages": out[:max(1, min(limit, 2000))]}


@app.patch("/api/admin/messages/{mid}")
async def admin_edit_message(mid: str, body: AdminMessageEdit, _: None = Depends(require_admin)):
    for cid, lst in messages.items():
        for i, m in enumerate(lst):
            if m["id"] == mid:
                lst[i] = {**m, "text": body.text, "edited": _now()}
                await broadcast_chat({
                    "type": "message_edited", "id": mid, "channel": cid,
                    "text": body.text, "edited": lst[i]["edited"],
                })
                return lst[i]
    raise HTTPException(404, "Сообщение не найдено")


@app.delete("/api/admin/messages/{mid}")
async def admin_delete_message(mid: str, _: None = Depends(require_admin)):
    for cid, lst in messages.items():
        for i, m in enumerate(lst):
            if m["id"] == mid:
                lst.pop(i)
                await broadcast_chat({"type": "message_deleted", "id": mid, "channel": cid})
                return {"ok": True}
    raise HTTPException(404, "Сообщение не найдено")


@app.delete("/api/admin/channels/{cid}/messages")
async def admin_clear_channel(cid: str, _: None = Depends(require_admin)):
    if cid not in messages:
        raise HTTPException(404, "Нет такого канала")
    messages[cid] = []
    await broadcast_chat({"type": "channel_cleared", "channel": cid})
    return {"ok": True}


# ---- SUPPORT (admin) ----
@app.get("/api/admin/support")
def admin_support_list(_: None = Depends(require_admin)):
    out = []
    for s in support_sessions.values():
        last = s["messages"][-1] if s["messages"] else None
        out.append({
            "id": s["id"], "name": s["name"], "email": s["email"],
            "ip": s["ip"], "ua": s["ua"],
            "created": s["created"], "ended": s["ended"], "ended_at": s["ended_at"],
            "unread_admin": s["unread_admin"], "messages": len(s["messages"]),
            "last_message": ({"text": last["text"], "ts": last["ts"], "from": last["from"]}
                             if last else None),
        })
    out.sort(key=lambda x: x["created"], reverse=True)
    return {"sessions": out}


@app.get("/api/admin/support/{sid}")
def admin_support_session(sid: str, _: None = Depends(require_admin)):
    s = support_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Нет такой сессии")
    s["unread_admin"] = 0
    return _support_public(sid)


@app.post("/api/admin/support/{sid}/end")
async def admin_support_end(sid: str, _: None = Depends(require_admin)):
    s = support_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Нет такой сессии")
    if not s["ended"]:
        s["ended"] = True
        s["ended_at"] = _now()
        sys_msg = {"id": secrets.token_hex(8), "from": "admin",
                   "text": "— Чат завершён администратором —", "ts": _now()}
        s["messages"].append(sys_msg)
        await _notify_support(sid, sys_msg, "admin")
    await broadcast_admin_support({"type": "support_list_changed"})
    return {"ok": True}


@app.delete("/api/admin/support/{sid}")
async def admin_support_delete(sid: str, _: None = Depends(require_admin)):
    if sid not in support_sessions:
        raise HTTPException(404, "Нет такой сессии")
    del support_sessions[sid]
    support_keys.pop(sid, None)
    await broadcast_admin_support({"type": "support_list_changed"})
    return {"ok": True}


# ============================================================
#  РОУТЫ
# ============================================================
from fastapi.responses import HTMLResponse as _HR  # noqa


# Загружаем HTML-страницы из отдельных констант ниже
@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    if not ADMIN_PASS:
        return HTMLResponse(
            "<h1 style='font-family:sans-serif;color:#cc0000;padding:40px'>"
            "Админка отключена: не задан ADMIN_PASS</h1>",
            status_code=503)
    return ADMIN_PAGE


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": _now()}


# ============================================================
#  HTML: ЧАТ
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#517da2">
<title>sldchat</title>
<style>
:root{--bg:#e6ebee;--panel:#fff;--panel2:#f2f5f8;--border:#dfe5ea;--text:#222;--muted:#9aa5ad;--accent:#517da2;--accent-h:#46708f;--bub-in:#fff;--bub-out:#eeffde;--bub-out-t:#7d8b7d;--shadow:rgba(0,0,0,.08);--chat-bg:#e6ebee;}
html[data-theme=dark]{--bg:#0f1720;--panel:#17212b;--panel2:#202b36;--border:#24313d;--text:#e7edf3;--muted:#8fa1b3;--accent:#4f87b8;--accent-h:#5c96c8;--bub-in:#1f2c38;--bub-out:#2b5278;--bub-out-t:#b9d0e6;--shadow:rgba(0,0,0,.35);--chat-bg:#0d141b;}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;}
input,textarea{-webkit-user-select:text;user-select:text;}
html,body{height:100vh;height:100dvh;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:14px;color:var(--text);background:var(--bg);}
button{font-family:inherit;cursor:pointer;border:none;background:none;color:inherit;}
input,textarea{font-family:inherit;}
svg{display:block;}
#boot{position:fixed;inset:0;z-index:200;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff;background:linear-gradient(140deg,#5c88ae 0%,#3c6591 55%,#2e5379 100%);}
#boot .logo{font-size:34px;font-weight:300;letter-spacing:4px;}
.spinner{margin-top:26px;width:40px;height:40px;border:3px solid rgba(255,255,255,.25);border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
#auth-screen{position:fixed;inset:0;z-index:100;display:none;align-items:center;justify-content:center;padding:20px;overflow-y:auto;background:linear-gradient(140deg,#5c88ae 0%,#3c6591 55%,#2e5379 100%);}
#auth-screen.visible{display:flex;}
.auth-card{width:380px;max-width:100%;background:var(--panel);border-radius:14px;overflow:hidden;box-shadow:0 24px 70px rgba(0,0,0,.4);color:var(--text);}
.auth-head{padding:26px 26px 4px;text-align:center;}
.auth-logo{font-size:32px;font-weight:300;letter-spacing:3px;color:var(--accent);}
.auth-sub{font-size:12px;color:var(--muted);margin-top:4px;}
.auth-tabs{display:flex;padding:0 22px;margin-top:18px;border-bottom:1px solid var(--border);}
.auth-tab{flex:1;padding:12px 0;font-size:13.5px;font-weight:600;color:var(--muted);border-bottom:2px solid transparent;}
.auth-tab.active{color:var(--accent);border-bottom-color:var(--accent);}
.auth-body{padding:18px 24px 24px;}
.auth-body input{width:100%;padding:12px 14px;margin-bottom:12px;border:1px solid var(--border);border-radius:8px;font-size:14px;outline:none;background:var(--panel2);color:var(--text);}
.auth-body input:focus{border-color:var(--accent);background:var(--panel);box-shadow:0 0 0 3px rgba(81,125,162,.14);}
.auth-error{color:#d64541;font-size:12px;min-height:16px;margin-bottom:6px;}
.auth-submit{width:100%;margin-top:6px;padding:12px;background:var(--accent);color:#fff;border-radius:8px;font-size:14px;font-weight:600;}
.auth-submit:hover{background:var(--accent-h);}
.auth-submit:disabled{opacity:.6;cursor:default;}
.auth-hint{font-size:11px;color:var(--muted);margin-top:8px;line-height:1.5;}
#app{display:none;height:100vh;height:100dvh;}
#app.visible{display:flex;}
.sidebar{width:280px;flex-shrink:0;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;}
.sidebar-header{height:56px;flex-shrink:0;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:space-between;padding:0 10px 0 14px;}
.me{display:flex;align-items:center;min-width:0;cursor:pointer;padding:6px 8px;border-radius:6px;}
.me:hover{background:rgba(255,255,255,.1);}
#me-name{font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
#me-user{font-size:11px;opacity:.75;}
.icon-btn{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#fff;opacity:.85;flex-shrink:0;}
.icon-btn:hover{background:rgba(255,255,255,.14);opacity:1;}
.search-wrap{position:relative;padding:10px 12px;border-bottom:1px solid var(--border);flex-shrink:0;}
.search-icon{position:absolute;left:22px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none;}
.search-wrap input{width:100%;padding:9px 12px 9px 36px;background:var(--panel2);border:1px solid transparent;border-radius:8px;font-size:13px;outline:none;color:var(--text);}
.search-wrap input:focus{background:var(--panel);border-color:var(--border);}
.channel-list{flex:1;overflow-y:auto;padding:6px 0;}
.channel-item{display:flex;align-items:center;gap:10px;padding:9px 14px;cursor:pointer;}
.channel-item:hover{background:var(--panel2);}
.channel-item.active{background:var(--accent);color:#fff;}
.channel-item.active .channel-last{color:rgba(255,255,255,.8);}
.channel-hash{width:30px;height:30px;flex-shrink:0;border-radius:50%;background:var(--panel2);color:var(--accent);display:flex;align-items:center;justify-content:center;font-weight:600;font-size:14px;}
.channel-item.active .channel-hash{background:rgba(255,255,255,.2);color:#fff;}
.channel-body{flex:1;min-width:0;}
.channel-name{font-weight:600;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.channel-last{font-size:12px;color:var(--muted);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.new-channel{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--border);background:var(--panel);flex-shrink:0;}
.new-channel input{flex:1;min-width:0;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:13px;outline:none;background:var(--panel2);color:var(--text);}
.new-channel input:focus{border-color:var(--accent);}
.new-channel button{width:38px;height:38px;border-radius:8px;background:var(--accent);color:#fff;flex-shrink:0;display:flex;align-items:center;justify-content:center;}
.new-channel button:hover{background:var(--accent-h);}
.chat{flex:1;min-width:0;display:flex;flex-direction:column;}
.chat-header{height:56px;flex-shrink:0;background:var(--accent);color:#fff;display:flex;align-items:center;padding:0 10px 0 14px;gap:8px;}
#back-btn{display:none;width:36px;height:36px;align-items:center;justify-content:center;border-radius:8px;color:#fff;}
#back-btn:hover{background:rgba(255,255,255,.14);}
.chat-title{display:flex;flex-direction:column;min-width:0;flex:1;}
#chat-name{font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.chat-users{font-size:11px;opacity:.85;}
.messages-wrap{flex:1;min-height:0;display:flex;}
.messages{flex:1;min-height:0;overflow-y:auto;padding:14px 18px 8px;background:var(--chat-bg);}
.empty{text-align:center;color:var(--muted);margin-top:60px;font-size:13px;line-height:1.6;}
.msg{display:flex;margin-bottom:6px;flex-direction:column;position:relative;}
.msg.in{align-items:flex-start;}
.msg.out{align-items:flex-end;}
.msg.same-user{margin-top:-2px;}
.msg.same-user .name{display:none;}
.bubble{max-width:74%;padding:6px 11px 5px;border-radius:10px;background:var(--bub-in);box-shadow:0 1px 2px var(--shadow);word-wrap:break-word;overflow-wrap:break-word;color:var(--text);position:relative;}
.msg.in .bubble{border-top-left-radius:3px;}
.msg.out .bubble{background:var(--bub-out);border-top-right-radius:3px;}
.msg.same-user.in .bubble,.msg.same-user.out .bubble{border-top-left-radius:10px;border-top-right-radius:10px;}
.name{font-size:12.5px;font-weight:600;color:var(--accent);margin-bottom:2px;}
.text{white-space:pre-wrap;line-height:1.35;font-size:14px;}
.text .mention{color:var(--accent);font-weight:600;}
.text .mention.self{background:rgba(81,125,162,.25);padding:0 3px;border-radius:4px;}
.text .edited{color:var(--muted);font-size:11px;margin-left:4px;}
.time{font-size:10.5px;color:var(--muted);text-align:right;margin-top:2px;margin-left:12px;}
.msg.out .time{color:var(--bub-out-t);}
.msg-tools{display:none;position:absolute;top:-4px;gap:4px;}
.msg.in .msg-tools{right:-64px;}
.msg.out .msg-tools{left:-64px;}
.msg:hover .msg-tools{display:flex;}
.msg-tool{width:26px;height:26px;border-radius:6px;background:var(--panel);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;color:var(--muted);}
.msg-tool:hover{color:var(--accent);border-color:var(--accent);}
.members-panel{width:200px;flex-shrink:0;background:var(--panel);border-left:1px solid var(--border);display:none;flex-direction:column;overflow:hidden;}
.members-panel.visible{display:flex;}
.members-head{padding:12px 14px;border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase;letter-spacing:1.4px;color:var(--muted);}
.members-head b{color:var(--text);font-weight:600;}
.members-list{flex:1;overflow-y:auto;padding:6px 0;}
.member-item{padding:8px 14px;font-size:13px;display:flex;align-items:center;gap:8px;}
.member-item .dot{width:7px;height:7px;border-radius:50%;background:#4caf50;flex-shrink:0;}
.member-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.member-name.self{color:var(--accent);font-weight:600;}
.composer{display:flex;align-items:flex-end;gap:10px;padding:12px 16px calc(12px + env(safe-area-inset-bottom, 0));background:var(--panel);border-top:1px solid var(--border);flex-shrink:0;position:relative;}
#msg-input{flex:1;min-width:0;padding:11px 14px;background:var(--panel2);border:1px solid transparent;border-radius:22px;font-size:14px;line-height:1.4;max-height:130px;min-height:44px;resize:none;outline:none;color:var(--text);}
#msg-input:focus{background:var(--panel);border-color:var(--border);}
.send-btn{width:44px;height:44px;flex-shrink:0;border-radius:50%;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;}
.send-btn:hover{background:var(--accent-h);}
.mention-menu{position:absolute;left:12px;right:12px;bottom:calc(100% + 6px);background:var(--panel);border:1px solid var(--border);border-radius:8px;box-shadow:0 8px 24px var(--shadow);max-height:220px;overflow-y:auto;z-index:20;display:none;}
.mention-menu.visible{display:block;}
.mention-item{padding:8px 12px;cursor:pointer;font-size:13px;}
.mention-item:hover{background:var(--panel2);}
.mention-item .u{color:var(--muted);margin-left:6px;}
.modal-backdrop{position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.5);display:none;align-items:center;justify-content:center;padding:20px;}
.modal-backdrop.visible{display:flex;}
.modal{background:var(--panel);border-radius:12px;padding:22px 24px;width:380px;max-width:100%;color:var(--text);box-shadow:0 24px 60px rgba(0,0,0,.4);}
.modal h3{font-size:16px;margin-bottom:16px;font-weight:600;}
.modal label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px;}
.modal input,.modal textarea{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;outline:none;background:var(--panel2);color:var(--text);margin-bottom:12px;font-family:inherit;}
.modal textarea{min-height:100px;resize:vertical;}
.modal input:focus,.modal textarea:focus{border-color:var(--accent);background:var(--panel);}
.modal .row{display:flex;justify-content:flex-end;gap:8px;}
.modal .btn2{padding:9px 16px;border-radius:8px;font-size:13px;background:var(--panel2);color:var(--text);}
.modal .btn2:hover{background:var(--border);}
.modal .btn2.primary{background:var(--accent);color:#fff;}
.modal .btn2.primary:hover{background:var(--accent-h);}
.modal-err{color:#d64541;font-size:12px;min-height:16px;margin-bottom:8px;}
#support-fab{position:fixed;right:20px;bottom:20px;z-index:150;padding:11px 18px;border-radius:24px;background:var(--accent);color:#fff;font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px;box-shadow:0 8px 24px rgba(0,0,0,.25);}
#support-fab:hover{background:var(--accent-h);}
.support-chat-box{display:flex;flex-direction:column;max-height:420px;}
.support-msgs{flex:1;overflow-y:auto;padding:10px 0;display:flex;flex-direction:column;gap:8px;min-height:200px;}
.sp-msg{padding:7px 11px;border-radius:10px;max-width:85%;font-size:13.5px;line-height:1.4;}
.sp-msg.user{align-self:flex-end;background:var(--bub-out);color:var(--text);}
.sp-msg.admin{align-self:flex-start;background:var(--bub-in);border:1px solid var(--border);color:var(--text);}
.sp-msg.sys{align-self:center;color:var(--muted);font-size:11.5px;background:none;}
.sp-row{display:flex;gap:8px;margin-top:8px;}
.sp-row input{flex:1;margin:0;}
.sp-send{padding:0 14px;background:var(--accent);color:#fff;border-radius:8px;}
@media (max-width:800px){
  #app.visible{display:block;position:relative;overflow:hidden;}
  .sidebar{position:absolute;inset:0;width:100%;border-right:none;}
  .chat{position:absolute;inset:0;background:var(--chat-bg);transform:translateX(100%);transition:transform .24s ease;z-index:5;}
  #app.chat-open .chat{transform:translateX(0);}
  #back-btn{display:flex;}
  .bubble{max-width:82%;}
  .members-panel{position:absolute;top:56px;right:0;bottom:0;width:220px;z-index:8;box-shadow:-8px 0 24px var(--shadow);}
}
.channel-list::-webkit-scrollbar,.messages::-webkit-scrollbar,.members-list::-webkit-scrollbar,.support-msgs::-webkit-scrollbar{width:8px;height:8px;}
.channel-list::-webkit-scrollbar-thumb,.messages::-webkit-scrollbar-thumb,.members-list::-webkit-scrollbar-thumb,.support-msgs::-webkit-scrollbar-thumb{background:rgba(0,0,0,.12);border-radius:4px;}
</style>
</head>
<body>

<div id="boot"><div class="logo">sldchat</div><div class="spinner"></div></div>

<div id="auth-screen">
  <div class="auth-card">
    <div class="auth-head">
      <div class="auth-logo">sldchat</div>
      <div class="auth-sub">простой веб-чат</div>
    </div>
    <div class="auth-tabs">
      <button type="button" class="auth-tab active" data-mode="login">Вход</button>
      <button type="button" class="auth-tab" data-mode="register">Регистрация</button>
    </div>
    <div class="auth-body">
      <div class="auth-error" id="auth-error"></div>
      <input id="reg-display" type="text" placeholder="Отображаемое имя" maxlength="32" style="display:none;">
      <input id="auth-user" type="text" placeholder="Юзернейм (латиница)" autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="24">
      <input id="auth-pass" type="password" placeholder="Пароль" autocomplete="current-password" maxlength="128">
      <input id="reg-pass2" type="password" placeholder="Повтор пароля" autocomplete="new-password" maxlength="128" style="display:none;">
      <button type="button" class="auth-submit" id="auth-submit">Войти</button>
      <div class="auth-hint" id="reg-hint" style="display:none;">Юзернейм: 3–24, только a-z A-Z 0-9 _ -.</div>
    </div>
  </div>
</div>

<button id="support-fab" style="display:none;">
  <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 17h-2v-2h2v2zm2.07-7.75l-.9.92C13.45 12.9 13 13.5 13 15h-2v-.5c0-1.1.45-2.1 1.17-2.83l1.24-1.26c.37-.36.59-.86.59-1.41 0-1.1-.9-2-2-2s-2 .9-2 2H8c0-2.21 1.79-4 4-4s4 1.79 4 4c0 .88-.36 1.68-.93 2.25z"/></svg>
  Поддержка
</button>

<div id="app">
  <aside class="sidebar">
    <div class="sidebar-header">
      <div class="me" id="me-block" title="Профиль">
        <div style="min-width:0;">
          <div id="me-name">…</div>
          <div id="me-user"></div>
        </div>
      </div>
      <div style="display:flex;gap:2px;">
        <button class="icon-btn" id="theme-btn" title="Тема"><svg id="theme-icon" viewBox="0 0 24 24" width="20" height="20"></svg></button>
        <button class="icon-btn" id="logout-btn" title="Выйти"><svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg></button>
      </div>
    </div>
    <div class="search-wrap">
      <svg class="search-icon" viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
      <input id="search" placeholder="Поиск каналов">
    </div>
    <div class="channel-list" id="channel-list"></div>
    <div class="new-channel" id="new-channel-wrap">
      <input id="new-channel-name" placeholder="Новый канал" maxlength="32">
      <button id="new-channel-btn" title="Создать"><svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg></button>
    </div>
  </aside>

  <main class="chat" id="chat">
    <div class="chat-header">
      <button class="icon-btn" id="back-btn"><svg viewBox="0 0 24 24" width="24" height="24"><path fill="currentColor" d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg></button>
      <div class="chat-title">
        <span id="chat-name">Выбери канал</span>
        <span class="chat-users" id="chat-users"></span>
      </div>
      <button class="icon-btn" id="members-btn" title="Участники">
        <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M16 11c1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3 1.34 3 3 3zm-8 0c1.66 0 3-1.34 3-3S9.66 5 8 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
      </button>
    </div>
    <div class="messages-wrap">
      <div class="messages" id="messages"><div class="empty">Выбери канал слева</div></div>
      <aside class="members-panel" id="members-panel">
        <div class="members-head">Участники <b id="members-count">0</b></div>
        <div class="members-list" id="members-list"></div>
      </aside>
    </div>
    <div class="composer">
      <div class="mention-menu" id="mention-menu"></div>
      <textarea id="msg-input" placeholder="Написать сообщение..." rows="1" enterkeyhint="send"></textarea>
      <button class="send-btn" id="send-btn" aria-label="Отправить">
        <svg viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
      </button>
    </div>
  </main>
</div>

<!-- MODALS -->
<div class="modal-backdrop" id="profile-modal">
  <div class="modal">
    <h3>Редактировать профиль</h3>
    <div class="modal-err" id="profile-err"></div>
    <label>Отображаемое имя</label>
    <input id="profile-display" type="text" maxlength="32">
    <div class="row">
      <button class="btn2" id="profile-cancel">Отмена</button>
      <button class="btn2 primary" id="profile-save">Сохранить</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="edit-msg-modal">
  <div class="modal">
    <h3>Редактировать сообщение</h3>
    <div class="modal-err" id="edit-msg-err"></div>
    <textarea id="edit-msg-text"></textarea>
    <div class="row">
      <button class="btn2" id="edit-msg-cancel">Отмена</button>
      <button class="btn2 primary" id="edit-msg-save">Сохранить</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="session-modal">
  <div class="modal">
    <h3 id="session-title">Сессия истекла</h3>
    <p style="color:var(--muted);font-size:13px;margin-bottom:16px;" id="session-text">
      Вы были отключены. Войдите заново.
    </p>
    <div class="row">
      <button class="btn2 primary" id="session-ok">Понятно</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="support-modal">
  <div class="modal">
    <h3>Поддержка</h3>
    <div class="modal-err" id="support-err"></div>
    <div id="support-start-form">
      <label>Как к вам обращаться?</label>
      <input id="support-name" type="text" maxlength="40" placeholder="Ваше имя">
      <label>Email для связи</label>
      <input id="support-email" type="email" maxlength="200" placeholder="you@example.com">
      <div class="row">
        <button class="btn2" id="support-cancel">Отмена</button>
        <button class="btn2 primary" id="support-start">Начать чат</button>
      </div>
    </div>
    <div id="support-chat-view" class="support-chat-box" style="display:none;">
      <div class="support-msgs" id="support-msgs"></div>
      <div class="sp-row">
        <input id="support-input" type="text" placeholder="Сообщение..." maxlength="2000">
        <button class="btn2 primary sp-send" id="support-send">Отправить</button>
      </div>
      <div class="row" style="margin-top:10px;">
        <button class="btn2" id="support-close-chat">Свернуть</button>
      </div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const LS_TOKEN='sld_token', LS_THEME='sld_theme', LS_SUPPORT='sld_support';
const state = {
  token: localStorage.getItem(LS_TOKEN) || null,
  username: null, display_name: null,
  channels: [], currentChannel: null,
  ws: null, wsReady: false, reconnectTimer: null, pingTimer: null,
  profiles: {}, allowChannelCreation: true, totalOnline: 0,
  onlineUsernames: new Set(),
  supportWs: null, supportSid: null, supportKey: null, supportPingTimer: null,
};
let authMode = 'login';

document.addEventListener('contextmenu', e => e.preventDefault());

/* THEME */
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  const ic = $('theme-icon');
  if (t === 'dark') ic.innerHTML='<path fill="currentColor" d="M6.76 4.84l-1.8-1.79-1.41 1.41 1.79 1.79 1.42-1.41zM4 10.5H1v2h3v-2zm9-9.95h-2V3.5h2V.55zm7.45 3.91l-1.41-1.41-1.79 1.79 1.41 1.41 1.79-1.79zm-3.21 13.7l1.79 1.8 1.41-1.41-1.8-1.79-1.4 1.4zM20 10.5v2h3v-2h-3zm-8-5c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6zm-1 16.95h2V19.5h-2v2.95zm-7.45-3.91l1.41 1.41 1.79-1.8-1.41-1.41-1.79 1.8z"/>';
  else ic.innerHTML='<path fill="currentColor" d="M20 8.69V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12 20 8.69zM12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6 6 2.69 6 6-2.69 6-6 6z"/>';
}
applyTheme(localStorage.getItem(LS_THEME) || 'light');
$('theme-btn').addEventListener('click', () => {
  const n = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  applyTheme(n); localStorage.setItem(LS_THEME, n);
});

/* API */
async function api(path, opts = {}) {
  const h = Object.assign({}, opts.headers || {});
  if (state.token) h['X-Auth-Token'] = state.token;
  if (opts.body && typeof opts.body !== 'string') { h['Content-Type']='application/json'; opts.body=JSON.stringify(opts.body); }
  const r = await fetch(path, Object.assign({}, opts, { headers: h }));
  if (!r.ok) {
    let d = r.statusText;
    try { d = (await r.json()).detail || d; } catch(e){}
    const err = new Error(d); err.status = r.status; throw err;
  }
  return r.json();
}

/* UTILS */
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function hhmm(ts){const t=new Date(ts*1000);if(isNaN(t))return '';return String(t.getHours()).padStart(2,'0')+':'+String(t.getMinutes()).padStart(2,'0');}
function renderTextWithMentions(text, self){
  const safe = escapeHtml(text);
  return safe.replace(/(^|[^A-Za-z0-9_\-])@([A-Za-z0-9_\-]{3,24})/g,(m,p1,nick)=>{
    const isSelf = self && nick.toLowerCase()===self.toLowerCase();
    return `${p1}<span class="mention${isSelf?' self':''}">@${nick}</span>`;
  });
}

/* SESSION MODAL */
function sessionModal(title, text){
  $('session-title').textContent = title;
  $('session-text').textContent = text;
  $('session-modal').classList.add('visible');
}
$('session-ok').addEventListener('click', () => {
  $('session-modal').classList.remove('visible');
  hardLogout();
});

/* AUTH */
function setAuthMode(m){
  authMode = m;
  document.querySelectorAll('.auth-tab').forEach(t=>t.classList.toggle('active',t.dataset.mode===m));
  const isReg = m==='register';
  $('reg-display').style.display = isReg?'':'none';
  $('reg-pass2').style.display = isReg?'':'none';
  $('reg-hint').style.display = isReg?'':'none';
  $('auth-submit').textContent = isReg?'Создать аккаунт':'Войти';
  $('auth-error').textContent = '';
}
document.querySelectorAll('.auth-tab').forEach(t=>t.addEventListener('click',()=>setAuthMode(t.dataset.mode)));
$('auth-submit').addEventListener('click', doAuth);
$('auth-user').addEventListener('keydown', e => { if (e.key === 'Enter') $('auth-pass').focus(); });
$('auth-pass').addEventListener('keydown', e => { if (e.key === 'Enter') authMode==='register'?$('reg-pass2').focus():doAuth(); });
$('reg-pass2').addEventListener('keydown', e => { if (e.key === 'Enter') doAuth(); });

async function doAuth(){
  const u = $('auth-user').value.trim();
  const p = $('auth-pass').value;
  const e = $('auth-error'); e.textContent = '';
  if (!u || !p) { e.textContent = 'Заполни все поля'; return; }
  $('auth-submit').disabled = true;
  try {
    let data;
    if (authMode === 'login') {
      data = await api('/api/login', { method:'POST', body:{username:u, password:p} });
    } else {
      const p2 = $('reg-pass2').value;
      const dn = $('reg-display').value.trim();
      if (p !== p2) { e.textContent = 'Пароли не совпадают'; return; }
      data = await api('/api/register', { method:'POST', body:{username:u, display_name:dn, password:p, password2:p2} });
    }
    state.token = data.token;
    state.username = data.username;
    state.display_name = data.display_name || data.username;
    localStorage.setItem(LS_TOKEN, data.token);
    await enterApp();
  } catch(err){ e.textContent = err.message || 'Ошибка'; }
  finally { $('auth-submit').disabled = false; }
}

function hardLogout(){
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch(e){} state.ws = null; }
  if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
  if (state.supportWs) { try { state.supportWs.close(); } catch(e){} state.supportWs = null; }
  state.token = null; state.username = null; state.display_name = null;
  state.currentChannel = null; state.channels = [];
  localStorage.removeItem(LS_TOKEN);
  $('app').classList.remove('visible','chat-open');
  $('auth-screen').classList.add('visible');
  $('support-fab').style.display = 'flex';
  $('auth-pass').value = ''; $('reg-pass2').value = ''; $('reg-display').value = '';
  setAuthMode('login');
}
$('logout-btn').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST' }); } catch(e){}
  hardLogout();
});

/* BOOT */
async function boot(){
  if (!state.token) { showAuth(); return; }
  try {
    const me = await api('/api/me');
    state.username = me.username;
    state.display_name = me.display_name || me.username;
    await enterApp();
  } catch(e){ localStorage.removeItem(LS_TOKEN); state.token = null; showAuth(); }
}
function showAuth(){
  $('boot').style.display = 'none';
  $('auth-screen').classList.add('visible');
  $('app').classList.remove('visible');
  $('support-fab').style.display = 'flex';
  setAuthMode('login');
}
function paintMe(){
  $('me-name').textContent = state.display_name || state.username;
  $('me-user').textContent = '@' + state.username;
}

async function enterApp(){
  try { const cfg = await api('/api/config'); state.allowChannelCreation = !!cfg.allow_channel_creation; } catch(e){}
  $('new-channel-wrap').style.display = state.allowChannelCreation ? '' : 'none';
  paintMe();
  await loadChannels();
  $('boot').style.display = 'none';
  $('auth-screen').classList.remove('visible');
  $('support-fab').style.display = 'none';
  $('app').classList.add('visible');
  connectWs();
}

/* PROFILE */
$('me-block').addEventListener('click', () => {
  $('profile-display').value = state.display_name || state.username;
  $('profile-err').textContent = '';
  $('profile-modal').classList.add('visible');
  setTimeout(() => $('profile-display').focus(), 30);
});
$('profile-cancel').addEventListener('click', () => $('profile-modal').classList.remove('visible'));
$('profile-save').addEventListener('click', async () => {
  const dn = $('profile-display').value.trim();
  if (!dn) { $('profile-err').textContent = 'Имя не может быть пустым'; return; }
  try {
    const r = await api('/api/profile', { method:'PATCH', body:{display_name:dn} });
    state.display_name = r.display_name;
    paintMe();
    $('profile-modal').classList.remove('visible');
  } catch(e){ $('profile-err').textContent = e.message; }
});

/* CHANNELS */
async function loadChannels(){
  try {
    const data = await api('/api/channels');
    state.channels = data.channels;
    state.totalOnline = data.total_online || 0;
    if (state.currentChannel && !state.channels.some(c=>c.id===state.currentChannel)) {
      state.currentChannel = null;
      $('app').classList.remove('chat-open');
      $('chat-name').textContent = 'Выбери канал';
      renderMessages([]);
      renderMembers([]);
    }
    renderChannels();
    updateHeader();
  } catch(e){}
}
function updateHeader(){
  $('chat-users').textContent = state.currentChannel
    ? (state.totalOnline + ' онлайн на сервере') : '';
}
function renderChannels(){
  const list = $('channel-list');
  const q = $('search').value.toLowerCase().trim();
  list.innerHTML = '';
  for (const c of state.channels) {
    if (q && !c.name.toLowerCase().includes(q)) continue;
    const el = document.createElement('div');
    el.className = 'channel-item' + (state.currentChannel===c.id?' active':'');
    el.dataset.id = c.id;
    const last = c.last_message
      ? `<div class="channel-last">${escapeHtml(c.last_message.user)}: ${escapeHtml(c.last_message.text)}</div>` : '';
    el.innerHTML =
      `<div class="channel-hash">#</div>
       <div class="channel-body">
         <div class="channel-name">${escapeHtml(c.name)}</div>
         ${last}
       </div>`;
    el.addEventListener('click', () => openChannel(c.id));
    list.appendChild(el);
  }
}

function openChannel(id){
  state.currentChannel = id;
  $('app').classList.add('chat-open');
  renderChannels();
  const ch = state.channels.find(c=>c.id===id);
  $('chat-name').textContent = ch ? '# '+ch.name : '# '+id;
  updateHeader();
  renderMessages([]);
  renderMembers([]);
  if (state.ws && state.wsReady) {
    state.ws.send(JSON.stringify({type:'join', channel:id}));
  }
}

/* MESSAGES */
function renderMessages(msgs){
  const box = $('messages');
  box.innerHTML = '';
  if (!msgs || !msgs.length) {
    box.innerHTML = '<div class="empty">Пока нет сообщений.<br>Напиши первым</div>';
    return;
  }
  let prevUser = null;
  for (const m of msgs) { appendMessage(m, {skipScroll:true, prevUser}); prevUser = m.user; }
  box.scrollTop = box.scrollHeight;
}
function userProfile(name){
  return state.profiles[(name||'').toLowerCase()] ||
    (name === state.username
      ? {username:state.username, display_name:state.display_name}
      : {username:name, display_name:name});
}
function appendMessage(m, opts = {}){
  const box = $('messages');
  const empty = box.querySelector('.empty'); if (empty) empty.remove();
  const out = m.user === state.username;
  const same = opts.prevUser === m.user;
  const prof = userProfile(m.user);
  const div = document.createElement('div');
  div.className = 'msg ' + (out ? 'out' : 'in') + (same ? ' same-user' : '');
  div.dataset.user = m.user; div.dataset.id = m.id;
  const nameHtml = out ? '' : `<div class="name">${escapeHtml(prof.display_name||prof.username)}</div>`;
  const edited = m.edited ? ' <span class="edited">(изм.)</span>' : '';
  const textHtml = renderTextWithMentions(m.text, state.username);
  const tools = out
    ? `<div class="msg-tools">
         <button class="msg-tool" data-tool="edit" title="Редактировать">
           <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34a.9959.9959 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
         </button>
         <button class="msg-tool" data-tool="del" title="Удалить">
           <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
         </button>
       </div>` : '';
  div.innerHTML =
    `<div class="bubble">${nameHtml}
       <div class="text">${textHtml}${edited}</div>
       <div class="time">${hhmm(m.ts)}</div>
     </div>${tools}`;
  box.appendChild(div);
  if (!opts.skipScroll) box.scrollTop = box.scrollHeight;

  div.querySelectorAll('.msg-tool').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const tool = btn.dataset.tool;
      if (tool === 'edit') openEditMsg(m.id, m.text);
      else if (tool === 'del') {
        if (confirm('Удалить это сообщение?')) {
          state.ws.send(JSON.stringify({type:'delete_message', id:m.id}));
        }
      }
    });
  });
}

/* EDIT MSG */
let editMsgId = null;
function openEditMsg(id, text){
  editMsgId = id;
  $('edit-msg-text').value = text;
  $('edit-msg-err').textContent = '';
  $('edit-msg-modal').classList.add('visible');
  setTimeout(()=>$('edit-msg-text').focus(), 30);
}
$('edit-msg-cancel').addEventListener('click', ()=>$('edit-msg-modal').classList.remove('visible'));
$('edit-msg-save').addEventListener('click', () => {
  const t = $('edit-msg-text').value.trim();
  if (!t) { $('edit-msg-err').textContent = 'Пустой текст'; return; }
  state.ws.send(JSON.stringify({type:'edit_message', id:editMsgId, text:t}));
  $('edit-msg-modal').classList.remove('visible');
});

/* MEMBERS */
const MEMBERS_LS = 'sld_members_visible';
if (localStorage.getItem(MEMBERS_LS) === '1') $('members-panel').classList.add('visible');
$('members-btn').addEventListener('click', () => {
  const p = $('members-panel');
  p.classList.toggle('visible');
  localStorage.setItem(MEMBERS_LS, p.classList.contains('visible')?'1':'0');
});
function renderMembers(members){
  $('members-count').textContent = members.length;
  const l = $('members-list');
  if (!members.length) {
    l.innerHTML = '<div style="padding:20px 14px;color:var(--muted);font-size:12px;text-align:center;">Пока никого</div>';
    return;
  }
  l.innerHTML = '';
  for (const m of members) {
    const el = document.createElement('div');
    el.className = 'member-item';
    const self = m.username === state.username;
    el.innerHTML = `<div class="dot"></div><div class="member-name${self?' self':''}">${
      escapeHtml(m.display_name||m.username)}</div>`;
    el.title = '@' + m.username;
    l.appendChild(el);
  }
}

/* WS */
function connectWs(){
  if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch(e){} state.ws = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.ws = ws; state.wsReady = false;

  ws.onopen = () => {
    state.wsReady = true;
    if (state.currentChannel) {
      ws.send(JSON.stringify({type:'join', channel: state.currentChannel}));
    }
    state.pingTimer = setInterval(() => {
      try { ws.send(JSON.stringify({type:'ping'})); } catch(e){}
    }, 25000);
  };

  ws.onmessage = (ev) => {
    let d; try { d = JSON.parse(ev.data); } catch(e){ return; }
    handleWsEvent(d);
  };

  ws.onclose = (ev) => {
    state.wsReady = false;
    if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
    if (ev && ev.code === 1008) {
      sessionModal('Сессия истекла', 'Вы были отключены. Войдите заново.');
      return;
    }
    state.reconnectTimer = setTimeout(connectWs, 1500);
  };
}

function handleWsEvent(d){
  if (d.type === 'presence') {
    state.totalOnline = d.total || 0;
    state.onlineUsernames = new Set(d.online_usernames || []);
    for (const c of state.channels) c.online = (d.channels||{})[c.id] || 0;
    updateHeader();
    renderChannels();
    if (state.currentChannel) refreshMembers();
  } else if (d.type === 'channel_joined') {
    if (d.channel !== state.currentChannel) return;
    if (d.profiles) Object.values(d.profiles).forEach(p => {
      state.profiles[(p.username||'').toLowerCase()] = p;
    });
    renderMessages(d.messages||[]);
    renderMembers(d.members||[]);
  } else if (d.type === 'message') {
    const m = d.message;
    const ch = state.channels.find(c=>c.id===m.channel);
    if (ch) ch.last_message = {user:m.user, text:m.text, ts:m.ts};
    if (m.channel === state.currentChannel) {
      const box = $('messages');
      const last = box.querySelector('.msg:last-child');
      appendMessage(m, {prevUser: last ? last.dataset.user : null});
    }
    renderChannels();
  } else if (d.type === 'message_edited') {
    if (d.channel !== state.currentChannel) return;
    const el = $('messages').querySelector(`.msg[data-id="${d.id}"] .text`);
    if (el) el.innerHTML = renderTextWithMentions(d.text, state.username) + ' <span class="edited">(изм.)</span>';
  } else if (d.type === 'message_deleted') {
    if (d.channel === state.currentChannel) {
      const el = $('messages').querySelector(`.msg[data-id="${d.id}"]`);
      if (el) el.remove();
    }
    loadChannels();
  } else if (d.type === 'channels_changed') {
    loadChannels();
  } else if (d.type === 'channel_cleared') {
    if (d.channel === state.currentChannel) renderMessages([]);
    loadChannels();
  } else if (d.type === 'channel_removed') {
    if (d.channel === state.currentChannel) {
      state.currentChannel = null;
      $('app').classList.remove('chat-open');
      $('chat-name').textContent = 'Выбери канал';
      renderMessages([]); renderMembers([]);
    }
    loadChannels();
  } else if (d.type === 'profile_updated') {
    state.profiles[(d.username||'').toLowerCase()] = {username:d.username, display_name:d.display_name};
    if (d.username === state.username) { state.display_name = d.display_name; paintMe(); }
    document.querySelectorAll(`.msg[data-user="${CSS.escape(d.username)}"] .name`)
      .forEach(el => { el.textContent = d.display_name; });
    if (state.currentChannel) refreshMembers();
  } else if (d.type === 'user_deleted') {
    if (d.username === state.username) {
      sessionModal('Аккаунт удалён', 'Ваш аккаунт был удалён администратором.');
      return;
    }
    document.querySelectorAll(`.msg[data-user="${CSS.escape(d.username)}"]`).forEach(el => el.remove());
  } else if (d.type === 'session_expired') {
    sessionModal('Сессия истекла', 'Ваша сессия недействительна.');
  } else if (d.type === 'channel_not_found') {
    state.currentChannel = null;
    $('app').classList.remove('chat-open');
    $('chat-name').textContent = 'Выбери канал';
    renderMessages([]); renderMembers([]);
  }
}

async function refreshMembers(){
  if (!state.currentChannel) return;
  const ch = state.channels.find(c=>c.id===state.currentChannel);
  if (!ch) return;
  // Используем online_usernames + фильтр? Нет, отдельный запрос.
  // Просто перезапросим заново через join событие.
  // На самом деле можно локально: получаем из presence для канала нельзя — там только counts.
  // Поэтому запросим через отдельный REST.
  try {
    // простой способ — включить в presence список users для каждого канала.
    // У нас только online_usernames общий. Используем его как приближение:
    // отфильтровать по state.currentChannel невозможно без доп данных.
    // Оставим только тех, кто реально в канале — их нам пришлёт 'channel_joined'.
    // На presence просто обновляем online-число.
  } catch(e){}
}

/* MENTIONS */
const MQ_RE = /(?:^|\s)@([A-Za-z0-9_\-]{0,24})$/;
function currentMQ(){
  const ta = $('msg-input');
  const b = ta.value.slice(0, ta.selectionStart);
  const m = b.match(MQ_RE); return m ? m[1] : null;
}
function hideMQ(){ $('mention-menu').classList.remove('visible'); }
function showMQ(q){
  const m = $('mention-menu');
  const lq = (q||'').toLowerCase();
  const cands = Object.values(state.profiles)
    .filter(p=>p.username && p.username!==state.username && p.username.toLowerCase().startsWith(lq))
    .slice(0, 8);
  if (state.username && state.username.toLowerCase().startsWith(lq))
    cands.unshift({username:state.username, display_name:state.display_name});
  if (!cands.length) { hideMQ(); return; }
  m.innerHTML = cands.map(p=>`
    <div class="mention-item" data-nick="${escapeHtml(p.username)}">
      ${escapeHtml(p.display_name||p.username)} <span class="u">@${escapeHtml(p.username)}</span>
    </div>`).join('');
  m.classList.add('visible');
  m.querySelectorAll('.mention-item').forEach(el=>{
    el.addEventListener('mousedown', e => { e.preventDefault(); insertMention(el.dataset.nick); });
  });
}
function insertMention(nick){
  const ta = $('msg-input');
  const b = ta.value.slice(0, ta.selectionStart);
  const a = ta.value.slice(ta.selectionStart);
  const r = b.replace(/@([A-Za-z0-9_\-]{0,24})$/, '@'+nick+' ');
  ta.value = r + a;
  ta.selectionStart = ta.selectionEnd = r.length;
  ta.focus(); hideMQ();
}
$('msg-input').addEventListener('input', e => {
  e.target.style.height = 'auto';
  e.target.style.height = Math.min(e.target.scrollHeight, 130) + 'px';
  const q = currentMQ();
  if (q !== null) showMQ(q); else hideMQ();
});
$('msg-input').addEventListener('blur', ()=>setTimeout(hideMQ, 120));

function sendMessage(){
  const inp = $('msg-input');
  const text = inp.value.trim();
  if (!text || !state.ws || !state.wsReady || !state.currentChannel) return;
  state.ws.send(JSON.stringify({type:'message', text}));
  inp.value=''; inp.style.height='auto'; hideMQ();
}
$('send-btn').addEventListener('click', sendMessage);
$('msg-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  else if (e.key === 'Escape') hideMQ();
});

$('back-btn').addEventListener('click', ()=>$('app').classList.remove('chat-open'));
$('search').addEventListener('input', renderChannels);
$('new-channel-btn').addEventListener('click', async () => {
  const name = $('new-channel-name').value.trim();
  if (!name) return;
  try {
    await api('/api/channels', { method:'POST', body:{name} });
    $('new-channel-name').value = '';
    await loadChannels();
  } catch(e){ alert(e.message); }
});
$('new-channel-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('new-channel-btn').click();
});

/* SUPPORT */
function openSupportModal(){
  $('support-modal').classList.add('visible');
  const saved = localStorage.getItem(LS_SUPPORT);
  if (saved) {
    try {
      const s = JSON.parse(saved);
      state.supportSid = s.sid; state.supportKey = s.key;
      showSupportChat();
      return;
    } catch(e){}
  }
  $('support-start-form').style.display = '';
  $('support-chat-view').style.display = 'none';
}
function closeSupportModal(){ $('support-modal').classList.remove('visible'); }

$('support-fab').addEventListener('click', openSupportModal);
$('support-cancel').addEventListener('click', closeSupportModal);
$('support-close-chat').addEventListener('click', closeSupportModal);

$('support-start').addEventListener('click', async () => {
  const name = $('support-name').value.trim();
  const email = $('support-email').value.trim();
  const e = $('support-err'); e.textContent = '';
  if (!name || !email) { e.textContent = 'Заполните оба поля'; return; }
  try {
    const r = await api('/api/support/start', { method:'POST', body:{name, email} });
    state.supportSid = r.sid; state.supportKey = r.key;
    localStorage.setItem(LS_SUPPORT, JSON.stringify({sid:r.sid, key:r.key}));
    showSupportChat();
  } catch(err){ e.textContent = err.message; }
});

async function showSupportChat(){
  $('support-start-form').style.display = 'none';
  $('support-chat-view').style.display = 'flex';
  try {
    const s = await api(`/api/support/session?sid=${encodeURIComponent(state.supportSid)}&key=${encodeURIComponent(state.supportKey)}`);
    renderSupportMsgs(s.messages || []);
  } catch(e){
    localStorage.removeItem(LS_SUPPORT);
    state.supportSid = state.supportKey = null;
    $('support-start-form').style.display = '';
    $('support-chat-view').style.display = 'none';
    return;
  }
  connectSupportWs();
}
function renderSupportMsgs(msgs){
  const box = $('support-msgs');
  box.innerHTML = '';
  for (const m of msgs) {
    const el = document.createElement('div');
    el.className = 'sp-msg ' + (m.from === 'user' ? 'user' : m.from === 'admin' ? 'admin' : 'sys');
    el.textContent = m.text;
    box.appendChild(el);
  }
  box.scrollTop = box.scrollHeight;
}
function connectSupportWs(){
  if (state.supportWs) { try { state.supportWs.close(); } catch(e){} state.supportWs = null; }
  if (state.supportPingTimer) { clearInterval(state.supportPingTimer); state.supportPingTimer = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws/support?sid=${encodeURIComponent(state.supportSid)}&key=${encodeURIComponent(state.supportKey)}`;
  const ws = new WebSocket(url);
  state.supportWs = ws;
  ws.onopen = () => {
    state.supportPingTimer = setInterval(()=>{ try{ ws.send(JSON.stringify({type:'ping'})); }catch(e){} }, 25000);
  };
  ws.onmessage = (ev) => {
    let d; try { d = JSON.parse(ev.data); } catch(e){ return; }
    if (d.type === 'support_init') {
      renderSupportMsgs((d.session.messages)||[]);
    } else if (d.type === 'support_message') {
      const box = $('support-msgs');
      const el = document.createElement('div');
      el.className = 'sp-msg ' + (d.message.from === 'user' ? 'user' : d.message.from === 'admin' ? 'admin' : 'sys');
      el.textContent = d.message.text;
      box.appendChild(el);
      box.scrollTop = box.scrollHeight;
    }
  };
  ws.onclose = () => {
    if (state.supportPingTimer) { clearInterval(state.supportPingTimer); state.supportPingTimer = null; }
    setTimeout(() => { if (state.supportWs === ws && state.supportSid) connectSupportWs(); }, 2000);
  };
}
$('support-send').addEventListener('click', () => {
  const inp = $('support-input');
  const text = inp.value.trim();
  if (!text || !state.supportWs || state.supportWs.readyState !== 1) return;
  state.supportWs.send(JSON.stringify({type:'message', text}));
  inp.value = '';
});
$('support-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('support-send').click(); });

boot();
</script>
</body>
</html>
"""


# ============================================================
#  HTML: АДМИНКА
# ============================================================
ADMIN_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=960">
<title>sldchat · admin</title>
<style>
:root{
  --bg:#f4f5f7;--panel:#fff;--panel2:#f2f5f8;--border:#d9dce1;--border2:#ebedf0;
  --text:#1a1a1a;--muted:#5c6670;--accent:#0066cc;--accent-h:#0055ad;
  --green:#3db83d;--red:#cc0000;--topbg:#22272e;--topfg:#eaecef;--side:#f4f5f7;--side-h:#e6e8eb;--side-a:#dde1e5;
}
html[data-theme=dark]{
  --bg:#161a1f;--panel:#1c2229;--panel2:#232a32;--border:#2e3641;--border2:#262d36;
  --text:#e0e6ed;--muted:#8b96a2;--accent:#3b82f6;--accent-h:#2563eb;
  --green:#22c55e;--red:#ef4444;--topbg:#0e1216;--topfg:#e0e6ed;--side:#181d23;--side-h:#232a32;--side-a:#2a323b;
}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:13px;color:var(--text);background:var(--bg);min-width:760px;}
button{font-family:inherit;font-size:inherit;cursor:pointer;border:none;background:none;color:inherit;}
input,select,textarea{font-family:inherit;}
svg{display:block;}
a{color:var(--accent);text-decoration:none;}
a:hover{text-decoration:underline;}

#login-view{position:fixed;inset:0;z-index:100;display:flex;align-items:center;justify-content:center;background:var(--bg);}
.login-card{width:340px;background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:26px 24px;}
.login-logo{font-size:22px;text-align:center;color:var(--text);}
.login-sub{font-size:11px;color:var(--muted);text-align:center;margin-top:4px;text-transform:uppercase;letter-spacing:1.4px;}
.login-fields{margin-top:22px;}
.login-fields label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;}
.login-fields input{width:100%;padding:8px 11px;background:var(--panel);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:14px;outline:none;}
.login-fields input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(0,102,204,.15);}
.login-err{color:var(--red);font-size:12px;min-height:16px;margin-top:8px;}
.login-btn{width:100%;margin-top:6px;padding:9px;background:var(--accent);color:#fff;border-radius:3px;font-size:13px;font-weight:500;}
.login-btn:hover{background:var(--accent-h);}
.login-btn:disabled{opacity:.55;cursor:default;}

#panel-view{display:none;min-height:100vh;}
#panel-view.visible{display:block;}
.topbar{height:44px;background:var(--topbg);color:var(--topfg);display:flex;align-items:center;justify-content:space-between;padding:0 14px;position:sticky;top:0;z-index:10;}
.crumb{font-size:13px;font-weight:500;display:flex;align-items:center;gap:8px;}
.crumb .sep{color:#5c6670;}
.crumb .tag{font-size:10.5px;color:#9aa5ad;letter-spacing:1.4px;text-transform:uppercase;border:1px solid #3f4550;padding:2px 7px;border-radius:2px;}
.topbar .actions{display:flex;align-items:center;gap:6px;}
.btn-t{padding:5px 10px;border:1px solid #3f4550;border-radius:3px;color:var(--topfg);font-size:12px;display:flex;align-items:center;gap:5px;}
.btn-t:hover{background:#2f3640;}
.btn-t.danger:hover{border-color:var(--red);color:#ff9b9b;}

.layout{display:flex;min-height:calc(100vh - 44px);}
.sidebar{width:180px;flex-shrink:0;background:var(--side);border-right:1px solid var(--border);padding:10px 0;}
.side-search{padding:0 12px 10px;}
.side-search input{width:100%;padding:6px 9px;border:1px solid var(--border);border-radius:3px;font-size:12px;outline:none;background:var(--panel);color:var(--text);}
.side-search input:focus{border-color:var(--accent);}
nav a{display:flex;align-items:center;gap:9px;padding:8px 16px;color:var(--text);font-size:13px;cursor:pointer;border-left:3px solid transparent;}
nav a:hover{background:var(--side-h);text-decoration:none;}
nav a.active{background:var(--side-a);border-left-color:var(--accent);color:var(--accent);font-weight:500;}
nav a svg{width:15px;height:15px;flex-shrink:0;}
nav .badge{margin-left:auto;font-size:10px;padding:1px 6px;border-radius:8px;background:var(--accent);color:#fff;font-weight:600;}
nav .badge.hide{display:none;}

.content{flex:1;padding:18px 22px;background:var(--panel);min-width:0;overflow-x:hidden;}
.page-title{font-size:19px;font-weight:400;margin:0 0 4px;}
.page-sub{color:var(--muted);font-size:12.5px;margin-bottom:16px;}

.card{background:var(--panel);border:1px solid var(--border);border-radius:3px;padding:16px 18px;margin-bottom:14px;}
.card h3{font-size:14.5px;font-weight:500;margin:0 0 12px;}
.grid-2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;}
.grid-3{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;}
.grid-4{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;}
.kv{display:grid;grid-template-columns:130px 1fr;gap:6px 14px;font-size:13px;}
.kv .k{color:var(--muted);}
.metric{font-size:24px;font-weight:400;line-height:1;color:var(--text);}
.metric-label{font-size:12px;color:var(--muted);margin-top:6px;}

.health{color:var(--green);display:flex;align-items:center;gap:8px;font-size:13px;}
.usage-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:13px;}
.usage-row .label{width:70px;color:var(--muted);}
.usage-bar{flex:1;height:8px;background:var(--border2);border-radius:4px;overflow:hidden;}
.usage-bar>span{display:block;height:100%;background:var(--accent);border-radius:4px;}
.usage-row .val{min-width:90px;text-align:right;color:var(--muted);font-size:12px;}

table{width:100%;border-collapse:collapse;font-size:13px;}
th{text-align:left;padding:8px 10px;color:var(--muted);font-weight:500;font-size:12px;background:var(--side);border-bottom:1px solid var(--border);white-space:nowrap;}
td{padding:8px 10px;border-bottom:1px solid var(--border2);vertical-align:middle;}
tbody tr:hover td{background:var(--side);}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;}
.muted{color:var(--muted);}
.accent{color:var(--accent);}
.green{color:var(--green);}
.red{color:var(--red);}
.chip{display:inline-block;padding:1px 7px;border-radius:10px;background:var(--side);color:var(--muted);font-size:11px;border:1px solid var(--border);margin-right:3px;}
.chip.blue{background:#e6f0fa;color:#004a99;border-color:#b8d5ef;}
html[data-theme=dark] .chip.blue{background:#1e3a5f;color:#90c2f0;border-color:#2c527f;}
.chip.green{background:#e6f5e6;color:#1a6b1a;border-color:#b8deb8;}
html[data-theme=dark] .chip.green{background:#16351a;color:#8adf8a;border-color:#2c6b2c;}
.chip.red{background:#fdeaea;color:#8a1a1a;border-color:#efb8b8;}
html[data-theme=dark] .chip.red{background:#3a1a1a;color:#f0a0a0;border-color:#7f2828;}

.btn{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:3px;font-size:12px;}
.btn:hover{background:var(--side);}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
.btn.primary:hover{background:var(--accent-h);}
.btn.danger{color:var(--red);}
.btn.danger:hover{background:#fdeaea;}
html[data-theme=dark] .btn.danger:hover{background:#3a1a1a;}
.btn.mini{padding:3px 8px;font-size:11.5px;}

.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap;}
.toolbar input,.toolbar select{padding:6px 10px;border:1px solid var(--border);border-radius:3px;font-size:12.5px;outline:none;background:var(--panel);color:var(--text);min-width:140px;}
.toolbar input:focus,.toolbar select:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(0,102,204,.15);}
.toolbar .grow{flex:1;}
.empty-state{padding:40px 20px;text-align:center;color:var(--muted);font-size:13px;}
.loading{padding:32px;text-align:center;color:var(--muted);font-size:12.5px;}

.modal-backdrop{position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.4);display:none;align-items:center;justify-content:center;padding:20px;}
.modal-backdrop.visible{display:flex;}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:18px 20px;width:400px;max-width:100%;box-shadow:0 8px 32px rgba(0,0,0,.2);}
.modal h3{font-size:15px;font-weight:500;margin-bottom:14px;}
.modal label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;}
.modal input,.modal textarea{width:100%;padding:8px 11px;border:1px solid var(--border);border-radius:3px;font-size:13px;outline:none;background:var(--panel);color:var(--text);margin-bottom:10px;font-family:inherit;}
.modal textarea{min-height:100px;resize:vertical;}
.modal input:focus,.modal textarea:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(0,102,204,.15);}
.modal .row{display:flex;justify-content:flex-end;gap:8px;margin-top:6px;}
.modal-err{color:var(--red);font-size:12px;min-height:16px;margin-bottom:6px;}

.chart-wrap{display:flex;gap:16px;align-items:center;flex-wrap:wrap;}
.pie-legend{font-size:12px;flex:1;min-width:180px;}
.pie-legend .row2{display:flex;align-items:center;gap:8px;margin-bottom:5px;}
.pie-legend .swatch{width:12px;height:12px;border-radius:2px;flex-shrink:0;}
.pie-legend .val{margin-left:auto;color:var(--muted);}

#toasts{position:fixed;right:20px;bottom:20px;z-index:300;display:flex;flex-direction:column;gap:8px;}
.toast{padding:10px 14px;border-radius:3px;background:var(--topbg);color:var(--topfg);font-size:12.5px;border-left:3px solid var(--green);box-shadow:0 4px 12px rgba(0,0,0,.15);}
.toast.err{border-left-color:var(--red);}

/* support */
.support-layout{display:grid;grid-template-columns:280px 1fr;gap:14px;height:calc(100vh - 130px);min-height:400px;}
.support-list{background:var(--panel);border:1px solid var(--border);border-radius:3px;overflow:hidden;display:flex;flex-direction:column;}
.support-list-head{padding:10px 12px;border-bottom:1px solid var(--border);font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1.2px;font-weight:500;}
.support-items{overflow-y:auto;flex:1;}
.support-item{padding:10px 12px;border-bottom:1px solid var(--border2);cursor:pointer;}
.support-item:hover{background:var(--side);}
.support-item.active{background:var(--side-a);}
.support-item .sname{font-weight:500;font-size:13px;display:flex;justify-content:space-between;}
.support-item .semail{font-size:11.5px;color:var(--muted);margin-top:2px;}
.support-item .spreview{font-size:11.5px;color:var(--muted);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.support-item .sbadge{background:var(--accent);color:#fff;font-size:10px;padding:1px 6px;border-radius:8px;font-weight:600;}
.support-item.ended .sname{color:var(--muted);text-decoration:line-through;}
.support-chat{background:var(--panel);border:1px solid var(--border);border-radius:3px;display:flex;flex-direction:column;overflow:hidden;}
.support-chat-head{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:10px;}
.support-chat-head .info{font-size:12px;color:var(--muted);}
.support-chat-head .info b{color:var(--text);font-weight:500;font-size:13px;display:block;margin-bottom:2px;}
.support-chat-body{flex:1;overflow-y:auto;padding:14px;}
.support-chat-msgs{display:flex;flex-direction:column;gap:8px;}
.sm{padding:7px 11px;border-radius:10px;max-width:70%;font-size:13px;line-height:1.4;word-wrap:break-word;white-space:pre-wrap;}
.sm.user{align-self:flex-start;background:var(--side);}
.sm.admin{align-self:flex-end;background:var(--accent);color:#fff;}
.sm.sys{align-self:center;color:var(--muted);font-size:11.5px;background:none;padding:0;}
.support-chat-foot{border-top:1px solid var(--border);padding:10px 12px;display:flex;gap:8px;}
.support-chat-foot input{flex:1;padding:8px 11px;border:1px solid var(--border);border-radius:3px;font-size:13px;outline:none;background:var(--panel);color:var(--text);}
.support-chat-foot input:focus{border-color:var(--accent);}

.hidden{display:none !important;}
::-webkit-scrollbar{width:9px;height:9px;}
::-webkit-scrollbar-track{background:var(--side);}
::-webkit-scrollbar-thumb{background:#c8cdd4;border-radius:4px;}
html[data-theme=dark] ::-webkit-scrollbar-thumb{background:#39424e;}
</style>
</head>
<body>

<div id="login-view">
  <div class="login-card">
    <div class="login-logo">sldchat</div>
    <div class="login-sub">admin panel</div>
    <div class="login-fields">
      <label>Пароль администратора</label>
      <input id="admin-pass" type="password" autocomplete="current-password" autofocus>
      <div class="login-err" id="login-err"></div>
      <button class="login-btn" id="login-btn">Войти</button>
    </div>
  </div>
</div>

<div id="panel-view">
  <header class="topbar">
    <div class="crumb">
      <span>admin@sldchat</span>
      <span class="sep">·</span>
      <span id="crumb-section">Обзор</span>
      <span class="tag">admin</span>
    </div>
    <div class="actions">
      <button class="btn-t" id="theme-btn" title="Тема">
        <svg id="theme-icon" viewBox="0 0 24 24" width="14" height="14"></svg>
      </button>
      <button class="btn-t" id="refresh-btn">
        <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M17.65 6.35A7.958 7.958 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
        Обновить
      </button>
      <button class="btn-t danger" id="logout-btn">
        <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg>
        Выйти
      </button>
    </div>
  </header>
  <div class="layout">
    <aside class="sidebar">
      <div class="side-search">
        <input id="side-search" placeholder="Поиск">
      </div>
      <nav id="side-nav">
        <a data-tab="dashboard" class="active">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"/></svg>
          Обзор
        </a>
        <a data-tab="users">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M16 11c1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3 1.34 3 3 3zm-8 0c1.66 0 3-1.34 3-3S9.66 5 8 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
          Пользователи
        </a>
        <a data-tab="channels">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>
          Каналы
        </a>
        <a data-tab="messages">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM6 9h12v2H6V9zm8 5H6v-2h8v2zm4-6H6V6h12v2z"/></svg>
          Сообщения
        </a>
        <a data-tab="support">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/></svg>
          Поддержка
          <span class="badge hide" id="support-badge">0</span>
        </a>
      </nav>
    </aside>
    <main class="content" id="main"></main>
  </div>
</div>

<div class="modal-backdrop" id="user-edit-modal">
  <div class="modal">
    <h3>Редактировать пользователя</h3>
    <div class="modal-err" id="ue-err"></div>
    <label>Отображаемое имя</label>
    <input id="ue-display" type="text" maxlength="32">
    <label>Новый пароль (пусто — не менять)</label>
    <input id="ue-pass" type="text" placeholder="мин. 6 символов">
    <div class="row">
      <button class="btn" id="ue-cancel">Отмена</button>
      <button class="btn primary" id="ue-save">Сохранить</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="msg-edit-modal">
  <div class="modal">
    <h3>Редактировать сообщение</h3>
    <div class="modal-err" id="me-err"></div>
    <label>Текст</label>
    <textarea id="me-text"></textarea>
    <div class="row">
      <button class="btn" id="me-cancel">Отмена</button>
      <button class="btn primary" id="me-save">Сохранить</button>
    </div>
  </div>
</div>

<div id="toasts"></div>

<script>
const $ = id => document.getElementById(id);
const LS_THEME = 'sld_admin_theme';
const state = { tab:'dashboard', subId:null, currentUser:null, currentMsg:null,
                supportList:[], currentSupportSid:null, supportWs:null, supportPingTimer:null,
                lastSupportCount:0 };

function escapeHtml(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmtTs(ts){if(!ts)return '—';const d=new Date(ts*1000);const p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;}
function fmtRel(ts){
  if(!ts) return '—';
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (s < 10) return 'только что';
  if (s < 60) return s + ' с назад';
  if (s < 3600) return Math.floor(s/60) + ' мин назад';
  if (s < 86400) return Math.floor(s/3600) + ' ч назад';
  return Math.floor(s/86400) + ' дн назад';
}
function toast(msg, kind='ok'){
  const el=document.createElement('div');
  el.className='toast '+(kind==='err'?'err':'');
  el.textContent=msg;
  $('toasts').appendChild(el);
  setTimeout(()=>el.remove(), 3000);
}

/* THEME */
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  const ic = $('theme-icon');
  if (t === 'dark') ic.innerHTML='<path fill="currentColor" d="M6.76 4.84l-1.8-1.79-1.41 1.41 1.79 1.79 1.42-1.41zM4 10.5H1v2h3v-2zm9-9.95h-2V3.5h2V.55zm7.45 3.91l-1.41-1.41-1.79 1.79 1.41 1.41 1.79-1.79zm-3.21 13.7l1.79 1.8 1.41-1.41-1.8-1.79-1.4 1.4zM20 10.5v2h3v-2h-3zm-8-5c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6zm-1 16.95h2V19.5h-2v2.95zm-7.45-3.91l1.41 1.41 1.79-1.8-1.41-1.41-1.79 1.8z"/>';
  else ic.innerHTML='<path fill="currentColor" d="M20 8.69V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12 20 8.69zM12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6 6 2.69 6 6-2.69 6-6 6z"/>';
}
applyTheme(localStorage.getItem(LS_THEME) || 'light');
$('theme-btn').addEventListener('click', () => {
  const n = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  applyTheme(n); localStorage.setItem(LS_THEME, n);
});

/* API */
async function api(path, opts = {}) {
  const h = Object.assign({}, opts.headers||{});
  if (opts.body && typeof opts.body !== 'string') { h['Content-Type']='application/json'; opts.body=JSON.stringify(opts.body); }
  const r = await fetch(path, Object.assign({}, opts, { headers:h, credentials:'same-origin' }));
  if (!r.ok) {
    let d = r.statusText;
    try { d = (await r.json()).detail || d; } catch(e){}
    const err = new Error(d); err.status = r.status; throw err;
  }
  if (r.status === 204) return null;
  const t = await r.text(); return t ? JSON.parse(t) : null;
}

/* LOGIN */
async function trySession(){ try { await api('/api/admin/session'); showPanel(); return true; } catch(e){ return false; } }
function showLogin(){ $('login-view').style.display=''; $('panel-view').classList.remove('visible'); }
function showPanel(){ $('login-view').style.display='none'; $('panel-view').classList.add('visible'); connectSupportWs(); renderTab(); }
$('login-btn').addEventListener('click', doLogin);
$('admin-pass').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
async function doLogin(){
  const p = $('admin-pass').value;
  $('login-err').textContent = '';
  if (!p) { $('login-err').textContent = 'Введите пароль'; return; }
  $('login-btn').disabled = true;
  try {
    await api('/api/admin/login', { method:'POST', body:{password:p} });
    $('admin-pass').value='';
    showPanel();
  } catch(e){ $('login-err').textContent = e.message || 'Ошибка'; }
  finally { $('login-btn').disabled = false; }
}
$('logout-btn').addEventListener('click', async () => {
  try { await api('/api/admin/logout', { method:'POST' }); } catch(e){}
  if (state.supportWs) { try { state.supportWs.close(); } catch(e){} state.supportWs=null; }
  showLogin();
});
$('refresh-btn').addEventListener('click', () => renderTab());

/* NAV */
function parseHash(){
  const h = location.hash.replace(/^#\/?/, '');
  const parts = h.split('/').filter(Boolean);
  if (!parts.length) return { tab: 'dashboard', sub: null };
  return { tab: parts[0], sub: parts[1] ? decodeURIComponent(parts[1]) : null };
}
function navigate(tab, sub){
  const h = '#/' + tab + (sub ? '/' + encodeURIComponent(sub) : '');
  if (location.hash !== h) location.hash = h;
  else renderTab();
}
document.querySelectorAll('#side-nav a').forEach(t => {
  t.addEventListener('click', () => navigate(t.dataset.tab, null));
});
window.addEventListener('hashchange', () => renderTab());

function setActiveTab(tab){
  document.querySelectorAll('#side-nav a').forEach(x =>
    x.classList.toggle('active', x.dataset.tab === tab));
  const label = { dashboard:'Обзор', users:'Пользователи', channels:'Каналы', messages:'Сообщения', support:'Поддержка' }[tab] || tab;
  $('crumb-section').textContent = label;
}

/* SNAPSHOT of inputs (для автообновления) */
function snapshotInputs(root){
  const out = {};
  root.querySelectorAll('input,select,textarea').forEach(el => { if (el.id) out[el.id] = el.value; });
  return out;
}
function restoreInputs(root, s){
  for (const [id, v] of Object.entries(s||{})) {
    const el = document.getElementById(id);
    if (el && el.tagName.match(/INPUT|TEXTAREA|SELECT/)) el.value = v;
  }
}

function isTyping(){
  const a = document.activeElement;
  return a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT');
}

async function renderTab(opts = {}){
  const { tab, sub } = parseHash();
  state.tab = tab; state.subId = sub;
  setActiveTab(tab);
  const main = $('main');
  const snap = opts.silent ? snapshotInputs(main) : null;
  try {
    if (tab === 'dashboard') await renderDashboard(main, opts);
    else if (tab === 'users') {
      if (sub) await renderUserDetail(main, sub, opts);
      else await renderUsers(main, opts);
    }
    else if (tab === 'channels') await renderChannels(main, opts);
    else if (tab === 'messages') await renderMessages(main, opts);
    else if (tab === 'support') await renderSupport(main, opts);
  } catch(e){
    if (e.status === 401) { showLogin(); return; }
    main.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
  if (snap) restoreInputs(main, snap);
}

/* AUTO REFRESH */
setInterval(async () => {
  if (!$('panel-view').classList.contains('visible')) return;
  if (isTyping()) return;
  await renderTab({ silent: true });
}, 10000);

/* DASHBOARD */
async function renderDashboard(main, opts){
  if (!opts.silent) main.innerHTML = `<div class="loading">Загрузка…</div>`;
  const s = await api('/api/admin/stats');
  const up = Math.floor(Date.now()/1000 - s.uptime_started);
  const d = Math.floor(up/86400), h = Math.floor((up%86400)/3600),
        m = Math.floor((up%3600)/60), sec = up%60;
  main.innerHTML = `
    <h2 class="page-title">Обзор</h2>
    <div class="page-sub">Сводка по состоянию сервера sldchat</div>
    <div class="grid-2">
      <div class="card">
        <h3>Здоровье</h3>
        <div class="health">
          <svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
          <span>Система работает нормально</span>
        </div>
        <div class="kv" style="margin-top:14px;">
          <div class="k">Сервер</div><div class="v">sldchat · FastAPI</div>
          <div class="k">Uptime</div><div class="v">${d?d+' дн ':''}${h}ч ${m}м ${sec}с</div>
          <div class="k">Открытых тикетов</div><div class="v">${s.support_open}</div>
        </div>
      </div>
      <div class="card">
        <h3>Использование</h3>
        <div class="usage-row"><div class="label">Онлайн</div>
          <div class="usage-bar"><span style="width:${Math.min(100, s.online*2)}%"></span></div>
          <div class="val">${s.online} чел.</div></div>
        <div class="usage-row"><div class="label">Токены</div>
          <div class="usage-bar"><span style="width:${Math.min(100, s.tokens*2)}%"></span></div>
          <div class="val">${s.tokens}</div></div>
        <div class="usage-row"><div class="label">Сообщ.</div>
          <div class="usage-bar"><span style="width:${Math.min(100, s.messages/10)}%"></span></div>
          <div class="val">${s.messages}</div></div>
      </div>
    </div>
    <div class="grid-4" style="margin-top:14px;">
      <div class="card"><div class="metric">${s.users}</div><div class="metric-label">Пользователей</div></div>
      <div class="card"><div class="metric">${s.channels}</div><div class="metric-label">Каналов</div></div>
      <div class="card"><div class="metric">${s.messages}</div><div class="metric-label">Сообщений</div></div>
      <div class="card"><div class="metric">${s.online}</div><div class="metric-label">Онлайн</div></div>
    </div>`;
}

/* USERS */
async function renderUsers(main, opts){
  if (!opts.silent) main.innerHTML = `<div class="loading">Загрузка…</div>`;
  const data = await api('/api/admin/users');
  const rows = data.users.map(u => {
    const status = u.is_online
      ? '<span class="chip green">онлайн</span>'
      : (u.online_in.length
          ? u.online_in.map(c=>`<span class="chip green">#${escapeHtml(c)}</span>`).join('')
          : '<span class="chip">offline</span>');
    const activity = u.is_online ? '<span class="green">сейчас</span>' : fmtRel(u.last_seen);
    return `<tr>
      <td><a href="#/users/${encodeURIComponent(u.username)}" class="mono accent" data-user="${escapeHtml(u.username)}">${escapeHtml(u.username)}</a></td>
      <td>${escapeHtml(u.display_name)}</td>
      <td class="muted mono">${fmtTs(u.created)}</td>
      <td class="muted">${activity}</td>
      <td class="muted">${u.logins_count}</td>
      <td>${u.messages}</td>
      <td>${status}</td>
      <td style="white-space:nowrap;">
        <button class="btn mini" data-edit-user="${escapeHtml(u.username)}" data-edit-name="${escapeHtml(u.display_name)}">Изменить</button>
        <button class="btn mini danger" data-del-user="${escapeHtml(u.username)}">Удалить</button>
      </td>
    </tr>`;
  }).join('');
  main.innerHTML = `
    <h2 class="page-title">Пользователи</h2>
    <div class="page-sub">Всего: ${data.users.length}. Клик по юзернейму — детали.</div>
    <div class="card" style="padding:0;overflow-x:auto;">
      ${data.users.length ? `<table>
        <thead><tr><th>Юзернейм</th><th>Имя</th><th>Создан</th><th>Активность</th><th>Входов</th><th>Сообщ.</th><th>Статус</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>` : `<div class="empty-state">Нет пользователей</div>`}
    </div>`;
  main.querySelectorAll('[data-edit-user]').forEach(b => {
    b.addEventListener('click', e => { e.preventDefault(); openUserEdit(b.dataset.editUser, b.dataset.editName); });
  });
  main.querySelectorAll('[data-del-user]').forEach(b => {
    b.addEventListener('click', () => deleteUser(b.dataset.delUser));
  });
}

/* USER DETAIL */
async function renderUserDetail(main, username, opts){
  if (!opts.silent) main.innerHTML = `<div class="loading">Загрузка профиля…</div>`;
  const d = await api('/api/admin/users/' + encodeURIComponent(username));
  const days = d.days || [];
  const msgsPerDay = days.map(day => ({ label: day.slice(5), value: d.messages_per_day[day] || 0 }));
  const loginsPerDay = days.map(day => ({ label: day.slice(5), value: d.logins_per_day[day] || 0 }));
  const pie = Object.entries(d.messages_by_channel || {}).sort((a,b)=>b[1]-a[1]);
  const palette = ['#0066cc','#3db83d','#f0ad4e','#d9534f','#9b59b6','#16a085','#e67e22','#34495e'];
  const pieData = pie.map(([ch,n],i) => ({
    label: '#' + ((window.__chCache||[]).find(c=>c.id===ch)?.name || ch),
    value: n, color: palette[i%palette.length],
  }));
  const status = d.is_online
    ? '<span class="chip green">онлайн сейчас</span>'
    : (d.online_in.length
        ? d.online_in.map(c=>`<span class="chip green">#${escapeHtml(c)}</span>`).join('')
        : '<span class="chip">offline</span>');
  main.innerHTML = `
    <h2 class="page-title">Профиль: ${escapeHtml(d.display_name)}</h2>
    <div class="page-sub"><a href="#/users">← к списку</a></div>
    <div class="grid-2">
      <div class="card">
        <h3>Учётная запись</h3>
        <div class="kv">
          <div class="k">Юзернейм</div><div class="v mono">@${escapeHtml(d.username)}</div>
          <div class="k">Имя</div><div class="v">${escapeHtml(d.display_name)}</div>
          <div class="k">Создан</div><div class="v">${fmtTs(d.created)}</div>
          <div class="k">Последняя активность</div>
          <div class="v">${d.is_online ? '<span class="green">сейчас</span>' : fmtRel(d.last_seen) + ' <span class="muted">('+fmtTs(d.last_seen)+')</span>'}</div>
          <div class="k">Статус</div><div class="v">${status}</div>
          <div class="k">Сообщений</div><div class="v">${d.total_messages}</div>
          <div class="k">Входов</div><div class="v">${(d.logins||[]).length}</div>
        </div>
        <div style="margin-top:14px;display:flex;gap:8px;">
          <button class="btn" id="det-edit">Редактировать</button>
          <button class="btn danger" id="det-del">Удалить аккаунт</button>
        </div>
      </div>
      <div class="card">
        <h3>Сообщений по каналам</h3>
        ${pieData.length ? `
          <div class="chart-wrap">
            <div>${pieChartSvg(pieData, 140)}</div>
            <div class="pie-legend">
              ${pieData.map(p=>`<div class="row2">
                <span class="swatch" style="background:${p.color}"></span>
                <span>${escapeHtml(p.label)}</span>
                <span class="val">${p.value}</span>
              </div>`).join('')}
            </div>
          </div>` : `<div class="muted">Пользователь не писал сообщений</div>`}
      </div>
    </div>
    <div class="card"><h3>Сообщения по дням (14 дней)</h3>${barChartSvg(msgsPerDay, 900, 130)}</div>
    <div class="card"><h3>Входы по дням (14 дней)</h3>${barChartSvg(loginsPerDay, 900, 130, '#3db83d')}</div>
    <div class="grid-2">
      <div class="card">
        <h3>История входов</h3>
        ${(d.logins && d.logins.length) ? `<div style="overflow-x:auto;"><table>
          <thead><tr><th>Когда</th><th>IP</th><th>UA</th></tr></thead>
          <tbody>${d.logins.slice(0,20).map(l => `
            <tr><td class="mono muted" style="white-space:nowrap;">${fmtTs(l.ts)}</td>
                <td class="mono">${escapeHtml(l.ip||'—')}</td>
                <td class="muted" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(l.ua||'')}">${escapeHtml((l.ua||'—').slice(0,70))}</td>
            </tr>`).join('')}
          </tbody></table></div>` : `<div class="muted">Нет записей</div>`}
      </div>
      <div class="card">
        <h3>Последние сообщения</h3>
        ${(d.recent_messages && d.recent_messages.length) ? `<div style="overflow-x:auto;"><table>
          <thead><tr><th>Когда</th><th>Канал</th><th>Текст</th></tr></thead>
          <tbody>${d.recent_messages.slice(0,15).map(m => `
            <tr><td class="mono muted" style="white-space:nowrap;">${fmtTs(m.ts)}</td>
                <td><span class="chip blue">#${escapeHtml(m.channel_name)}</span></td>
                <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(m.text)}">${escapeHtml(m.text)}</td>
            </tr>`).join('')}
          </tbody></table></div>` : `<div class="muted">Сообщений нет</div>`}
      </div>
    </div>`;
  $('det-edit').addEventListener('click', () => openUserEdit(d.username, d.display_name));
  $('det-del').addEventListener('click', () => deleteUser(d.username));
}

/* CHARTS */
function pieChartSvg(data, size){
  const total = data.reduce((s,d)=>s+d.value,0) || 1;
  const cx=size/2, cy=size/2, r=size/2-4;
  if (data.length === 1) return `<svg width="${size}" height="${size}"><circle cx="${cx}" cy="${cy}" r="${r}" fill="${data[0].color}"/></svg>`;
  let a = -Math.PI/2, out = '';
  for (const d of data){
    const sweep = (d.value/total) * Math.PI * 2;
    const x1=cx+r*Math.cos(a), y1=cy+r*Math.sin(a);
    const x2=cx+r*Math.cos(a+sweep), y2=cy+r*Math.sin(a+sweep);
    const large = sweep > Math.PI ? 1 : 0;
    out += `<path d="M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z" fill="${d.color}" stroke="#fff" stroke-width="1"/>`;
    a += sweep;
  }
  return `<svg width="${size}" height="${size}">${out}</svg>`;
}
function barChartSvg(data, w, h, color='#0066cc'){
  const max = Math.max(1, ...data.map(d=>d.value));
  const pL=30, pR=10, pT=10, pB=24;
  const iW=w-pL-pR, iH=h-pT-pB;
  const bw = iW / Math.max(1, data.length);
  let s = '';
  for (let i=0;i<=3;i++){
    const y = pT + iH*(i/3);
    const val = Math.round(max*(1-i/3));
    s += `<line x1="${pL}" y1="${y}" x2="${w-pR}" y2="${y}" stroke="var(--border2)" stroke-width="1"/>`;
    s += `<text x="${pL-5}" y="${y+3}" font-size="10" fill="var(--muted)" text-anchor="end">${val}</text>`;
  }
  for (let i=0;i<data.length;i++){
    const d = data[i];
    const bh = (d.value/max)*iH;
    const x = pL + i*bw + 2;
    const y = pT + iH - bh;
    s += `<rect x="${x}" y="${y}" width="${bw-4}" height="${bh}" fill="${color}" rx="1"/>`;
    s += `<text x="${x+(bw-4)/2}" y="${h-8}" font-size="9" fill="var(--muted)" text-anchor="middle">${escapeHtml(d.label)}</text>`;
  }
  return `<svg width="${w}" height="${h}">${s}</svg>`;
}

/* USER EDIT */
function openUserEdit(username, displayName){
  state.currentUser = username;
  $('ue-err').textContent = '';
  $('ue-display').value = displayName || '';
  $('ue-pass').value = '';
  $('user-edit-modal').classList.add('visible');
  setTimeout(()=>$('ue-display').focus(), 30);
}
$('ue-cancel').addEventListener('click', ()=>$('user-edit-modal').classList.remove('visible'));
$('ue-save').addEventListener('click', async () => {
  const body = {};
  const dn = $('ue-display').value.trim();
  const pw = $('ue-pass').value.trim();
  if (dn) body.display_name = dn;
  if (pw) body.password = pw;
  if (!Object.keys(body).length) { $('user-edit-modal').classList.remove('visible'); return; }
  try {
    await api('/api/admin/users/' + encodeURIComponent(state.currentUser), { method:'PATCH', body });
    toast('Пользователь обновлён');
    $('user-edit-modal').classList.remove('visible');
    renderTab();
  } catch(e){ $('ue-err').textContent = e.message; }
});
async function deleteUser(username){
  if (!confirm(`Удалить пользователя "${username}"?\n\nЭто выкинет его из чата, удалит все его сессии И ВСЕ ЕГО СООБЩЕНИЯ.`)) return;
  try {
    const r = await api('/api/admin/users/' + encodeURIComponent(username), { method:'DELETE' });
    toast(`Удалён. Сообщений удалено: ${r.messages_removed}`);
    renderTab();
  } catch(e){ toast(e.message,'err'); }
}

/* CHANNELS */
async function renderChannels(main, opts){
  if (!opts.silent) main.innerHTML = `<div class="loading">Загрузка…</div>`;
  const data = await api('/api/admin/channels');
  window.__chCache = data.channels;
  const rows = data.channels.map(c => `
    <tr>
      <td class="mono">#${escapeHtml(c.id)}</td>
      <td>${escapeHtml(c.name)}</td>
      <td class="muted mono">${escapeHtml(c.owner)}</td>
      <td class="muted mono">${fmtTs(c.created)}</td>
      <td>${c.messages}</td>
      <td>${c.online ? `<span class="chip green">${c.online}</span>` : `<span class="chip">0</span>`}</td>
      <td style="white-space:nowrap;">
        <button class="btn mini" data-clear="${escapeHtml(c.id)}">Очистить</button>
        <button class="btn mini danger" data-del="${escapeHtml(c.id)}">Удалить</button>
      </td>
    </tr>`).join('');
  main.innerHTML = `
    <h2 class="page-title">Каналы</h2>
    <div class="page-sub">Изменения видны клиентам мгновенно.</div>
    <div class="toolbar">
      <input id="new-ch" placeholder="имя нового канала" maxlength="32">
      <button class="btn primary" id="new-ch-btn">Создать</button>
    </div>
    <div class="card" style="padding:0;overflow-x:auto;">
      ${data.channels.length ? `<table>
        <thead><tr><th>ID</th><th>Название</th><th>Владелец</th><th>Создан</th><th>Сообщ.</th><th>Онлайн</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>` : `<div class="empty-state">Нет каналов</div>`}
    </div>`;
  main.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', ()=>deleteChannel(b.dataset.del)));
  main.querySelectorAll('[data-clear]').forEach(b => b.addEventListener('click', ()=>clearChannel(b.dataset.clear)));
  $('new-ch-btn').addEventListener('click', createChannel);
  $('new-ch').addEventListener('keydown', e => { if (e.key==='Enter') createChannel(); });
}
async function createChannel(){
  const name = $('new-ch').value.trim();
  if (!name) return;
  try { await api('/api/admin/channels', { method:'POST', body:{name} }); toast('Канал создан'); renderTab(); }
  catch(e){ toast(e.message,'err'); }
}
async function deleteChannel(cid){
  if (!confirm(`Удалить канал #${cid}?`)) return;
  try { await api('/api/admin/channels/'+encodeURIComponent(cid), { method:'DELETE' }); toast('Удалён'); renderTab(); }
  catch(e){ toast(e.message,'err'); }
}
async function clearChannel(cid){
  if (!confirm(`Очистить #${cid}?`)) return;
  try { await api('/api/admin/channels/'+encodeURIComponent(cid)+'/messages', { method:'DELETE' }); toast('Очищено'); renderTab(); }
  catch(e){ toast(e.message,'err'); }
}

/* MESSAGES */
async function renderMessages(main, opts){
  if (!opts.silent) main.innerHTML = `<div class="loading">Загрузка…</div>`;
  const channelsData = await api('/api/admin/channels');
  window.__chCache = channelsData.channels;
  const options = ['<option value="">— все каналы —</option>']
    .concat(channelsData.channels.map(c => `<option value="${escapeHtml(c.id)}">#${escapeHtml(c.name)}</option>`)).join('');
  main.innerHTML = `
    <h2 class="page-title">Сообщения</h2>
    <div class="page-sub">Просмотр, редактирование и удаление.</div>
    <div class="toolbar">
      <select id="flt-channel">${options}</select>
      <input id="flt-user" class="mono" placeholder="автор">
      <input id="flt-search" class="grow" placeholder="поиск по тексту…">
      <button class="btn primary" id="flt-apply">Применить</button>
    </div>
    <div class="card" style="padding:0;overflow-x:auto;" id="msgs-wrap"><div class="loading">Загрузка…</div></div>`;
  const load = async () => {
    const p = new URLSearchParams();
    const ch=$('flt-channel').value, u=$('flt-user').value.trim(), q=$('flt-search').value.trim();
    if (ch) p.set('channel', ch);
    if (u) p.set('user', u);
    if (q) p.set('search', q);
    p.set('limit','500');
    const wrap = $('msgs-wrap');
    wrap.innerHTML = `<div class="loading">Загрузка…</div>`;
    try {
      const data = await api('/api/admin/messages?'+p.toString());
      wrap.innerHTML = data.messages.length ? `<table>
        <thead><tr><th>Время</th><th>Канал</th><th>Автор</th><th>Текст</th><th></th></tr></thead>
        <tbody>${data.messages.map(m => `<tr>
          <td class="mono muted" style="white-space:nowrap;">${fmtTs(m.ts)}</td>
          <td><span class="chip blue">#${escapeHtml(m.channel_name||m.channel)}</span></td>
          <td class="mono accent">${escapeHtml(m.user)}</td>
          <td style="max-width:520px;word-break:break-word;">${escapeHtml(m.text)}${m.edited?' <span class="muted">(изм.)</span>':''}</td>
          <td style="white-space:nowrap;">
            <button class="btn mini" data-medit="${escapeHtml(m.id)}" data-mtext="${escapeHtml(m.text)}">Изменить</button>
            <button class="btn mini danger" data-mdel="${escapeHtml(m.id)}">Удалить</button>
          </td>
        </tr>`).join('')}</tbody></table>` : `<div class="empty-state">Ничего не найдено</div>`;
      wrap.querySelectorAll('[data-medit]').forEach(b=>b.addEventListener('click',()=>openMsgEdit(b.dataset.medit, b.dataset.mtext)));
      wrap.querySelectorAll('[data-mdel]').forEach(b=>b.addEventListener('click',()=>deleteMessage(b.dataset.mdel)));
    } catch(e){
      if (e.status === 401) { showLogin(); return; }
      wrap.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
    }
  };
  $('flt-apply').addEventListener('click', load);
  $('flt-search').addEventListener('keydown', e => { if (e.key==='Enter') load(); });
  await load();
  if (opts.silent && state.__fltState) {
    $('flt-channel').value = state.__fltState.ch || '';
    $('flt-user').value = state.__fltState.u || '';
    $('flt-search').value = state.__fltState.q || '';
  }
  state.__fltState = { ch: $('flt-channel').value, u: $('flt-user').value, q: $('flt-search').value };
}

function openMsgEdit(id, text){
  state.currentMsg = id;
  $('me-err').textContent = '';
  $('me-text').value = text;
  $('msg-edit-modal').classList.add('visible');
  setTimeout(()=>$('me-text').focus(), 30);
}
$('me-cancel').addEventListener('click', ()=>$('msg-edit-modal').classList.remove('visible'));
$('me-save').addEventListener('click', async () => {
  const t = $('me-text').value.trim();
  if (!t) { $('me-err').textContent = 'Пустой текст'; return; }
  try {
    await api('/api/admin/messages/'+encodeURIComponent(state.currentMsg), { method:'PATCH', body:{text:t} });
    toast('Сообщение обновлено');
    $('msg-edit-modal').classList.remove('visible');
    renderTab();
  } catch(e){ $('me-err').textContent = e.message; }
});
async function deleteMessage(id){
  if (!confirm('Удалить сообщение?')) return;
  try { await api('/api/admin/messages/'+encodeURIComponent(id), { method:'DELETE' }); toast('Удалено'); renderTab(); }
  catch(e){ toast(e.message,'err'); }
}

/* SUPPORT */
let supportBadge = 0;
async function refreshSupportBadge(){
  try {
    const data = await api('/api/admin/support');
    const open = data.sessions.filter(s=>!s.ended && s.unread_admin>0).length;
    const totalOpen = data.sessions.filter(s=>!s.ended).length;
    supportBadge = totalOpen;
    const b = $('support-badge');
    if (totalOpen > 0) { b.textContent = totalOpen; b.classList.remove('hide'); }
    else b.classList.add('hide');
    state.supportList = data.sessions;
  } catch(e){}
}

async function renderSupport(main, opts){
  if (!opts.silent) main.innerHTML = `<div class="loading">Загрузка…</div>`;
  await refreshSupportBadge();
  const sessions = state.supportList || [];
  const itemsHtml = sessions.map(s => `
    <div class="support-item ${s.ended?'ended':''} ${state.subId===s.id?'active':''}" data-sid="${escapeHtml(s.id)}">
      <div class="sname">
        <span>${escapeHtml(s.name)}</span>
        ${(!s.ended && s.unread_admin>0)?`<span class="sbadge">${s.unread_admin}</span>`:(s.ended?'<span class="chip">закрыт</span>':'')}
      </div>
      <div class="semail">${escapeHtml(s.email)}</div>
      <div class="spreview">${s.last_message?escapeHtml(s.last_message.text.slice(0,60)):'—'}</div>
    </div>`).join('');
  main.innerHTML = `
    <h2 class="page-title">Поддержка</h2>
    <div class="page-sub">Чаты с пользователями. Всего: ${sessions.length}.</div>
    <div class="support-layout">
      <div class="support-list">
        <div class="support-list-head">Тикеты</div>
        <div class="support-items">
          ${sessions.length ? itemsHtml : `<div class="empty-state">Нет обращений</div>`}
        </div>
      </div>
      <div class="support-chat" id="support-chat-pane">
        <div class="empty-state">Выберите тикет</div>
      </div>
    </div>`;
  main.querySelectorAll('.support-item').forEach(el => {
    el.addEventListener('click', () => navigate('support', el.dataset.sid));
  });
  if (state.subId) await renderSupportChat();
}

async function renderSupportChat(){
  const pane = $('support-chat-pane');
  if (!pane) return;
  let s;
  try { s = await api('/api/admin/support/'+encodeURIComponent(state.subId)); }
  catch(e){
    pane.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
    return;
  }
  pane.innerHTML = `
    <div class="support-chat-head">
      <div class="info">
        <b>${escapeHtml(s.name)} · ${escapeHtml(s.email)}</b>
        IP: <span class="mono">${escapeHtml(s.ip)}</span> · создан: ${fmtTs(s.created)}
        ${s.ended?' · <span class="red">завершён '+fmtTs(s.ended_at)+'</span>':''}
      </div>
      <div style="display:flex;gap:6px;">
        ${!s.ended ? `<button class="btn" id="sup-end">Завершить</button>`:''}
        <button class="btn danger" id="sup-del">Удалить</button>
      </div>
    </div>
    <div class="support-chat-body" id="sup-body">
      <div class="support-chat-msgs" id="sup-msgs">
        ${s.messages.map(m => `<div class="sm ${escapeHtml(m.from)}">${escapeHtml(m.text)}<div style="font-size:10px;opacity:.6;margin-top:3px;text-align:right;">${new Date(m.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</div></div>`).join('')}
      </div>
    </div>
    ${!s.ended ? `<div class="support-chat-foot">
      <input id="sup-input" placeholder="Ответ..." maxlength="2000">
      <button class="btn primary" id="sup-send">Отправить</button>
    </div>`:''}`;
  const body = $('sup-body');
  if (body) body.scrollTop = body.scrollHeight;

  if (!s.ended) {
    $('sup-send').addEventListener('click', () => {
      const inp = $('sup-input');
      const t = inp.value.trim();
      if (!t || !state.supportWs || state.supportWs.readyState !== 1) return;
      state.supportWs.send(JSON.stringify({type:'reply', sid: state.subId, text:t}));
      inp.value = '';
    });
    $('sup-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('sup-send').click(); });
  }
  const endBtn = $('sup-end');
  if (endBtn) endBtn.addEventListener('click', async () => {
    if (!confirm('Завершить чат?')) return;
    try { await api(`/api/admin/support/${encodeURIComponent(state.subId)}/end`, { method:'POST' }); toast('Завершён'); renderTab(); }
    catch(e){ toast(e.message,'err'); }
  });
  $('sup-del').addEventListener('click', async () => {
    if (!confirm('Удалить тикет? Сообщения будут потеряны.')) return;
    try {
      await api(`/api/admin/support/${encodeURIComponent(state.subId)}`, { method:'DELETE' });
      navigate('support', null);
    } catch(e){ toast(e.message,'err'); }
  });
}

/* SUPPORT WS */
function connectSupportWs(){
  if (state.supportWs) { try { state.supportWs.close(); } catch(e){} state.supportWs = null; }
  if (state.supportPingTimer) { clearInterval(state.supportPingTimer); state.supportPingTimer = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/admin_support`);
  state.supportWs = ws;
  ws.onopen = () => {
    state.supportPingTimer = setInterval(()=>{ try{ ws.send(JSON.stringify({type:'ping'})); }catch(e){} }, 25000);
  };
  ws.onmessage = (ev) => {
    let d; try { d = JSON.parse(ev.data); } catch(e){ return; }
    if (d.type === 'support_message' || d.type === 'support_list_changed') {
      refreshSupportBadge();
      if (state.tab === 'support') {
        if (d.type === 'support_message' && d.sid === state.subId) {
          // append live
          const box = $('sup-msgs');
          if (box) {
            const el = document.createElement('div');
            el.className = 'sm ' + d.message.from;
            el.innerHTML = `${escapeHtml(d.message.text)}<div style="font-size:10px;opacity:.6;margin-top:3px;text-align:right;">${new Date(d.message.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</div>`;
            box.appendChild(el);
            const body = $('sup-body');
            if (body) body.scrollTop = body.scrollHeight;
          }
        } else {
          renderTab({ silent:true });
        }
      }
    }
  };
  ws.onclose = () => {
    if (state.supportPingTimer) { clearInterval(state.supportPingTimer); state.supportPingTimer = null; }
    setTimeout(() => { if ($('panel-view').classList.contains('visible')) connectSupportWs(); }, 2000);
  };
}

/* SIDE SEARCH */
$('side-search').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('#side-nav a').forEach(a => {
    a.style.display = a.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
});

/* BOOT */
(async () => {
  if (!await trySession()) showLogin();
  refreshSupportBadge();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
