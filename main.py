"""
sldchat — веб-чат в стиле IRC/Telegram. Один файл. Всё в оперативке.

ENV:
  APP_NAME     название приложения (по умолчанию "sldchat")
  ADMIN_PASS   пароль админки /admin. Если пусто — админка отключена.
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
APP_NAME = os.environ.get("APP_NAME", "sldchat").strip() or "sldchat"
ALLOW_CHANNEL_CREATION = True
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,24}$")
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_\-])@([A-Za-z0-9_\-]{3,24})")

LOGIN_WINDOW, LOGIN_MAX = 60, 8
WS_MSG_WINDOW, WS_MSG_MAX = 5, 12
MAX_MENTIONS_PER_USER = 200

# ============================================================
#  ХРАНИЛИЩЕ
# ============================================================
users: Dict[str, dict] = {}
tokens: Dict[str, str] = {}
channels: Dict[str, dict] = {}
messages: Dict[str, List[dict]] = {}
user_mentions: Dict[str, List[dict]] = {}  # username_lower -> [mention]

connections: Dict[WebSocket, dict] = {}
admin_ws: Set[WebSocket] = set()

login_attempts: Dict[str, List[float]] = {}
msg_attempts: Dict[str, List[float]] = {}
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


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    for h in ("x-real-ip", "cf-connecting-ip"):
        v = request.headers.get(h, "")
        if v:
            return v.strip()
    return request.client.host if request.client else "?"


def rate_check(bucket, key, window, limit, msg):
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
        raise HTTPException(403, "Admin disabled")
    if not sld_admin or sld_admin not in admin_sessions:
        raise HTTPException(401, "Unauthorized")


def public_user(username: str) -> dict:
    u = users.get(username.lower())
    if not u:
        return {"username": username, "display_name": username}
    return {"username": u["username"], "display_name": u.get("display_name") or u["username"]}


def _online_users() -> List[dict]:
    seen: Dict[str, dict] = {}
    for info in connections.values():
        if info.get("kind") != "chat":
            continue
        name = info.get("username")
        if name and name not in seen:
            seen[name] = public_user(name)
    return sorted(seen.values(), key=lambda p: p["display_name"].lower())


def _online_usernames() -> Set[str]:
    return {i["username"] for i in connections.values()
            if i.get("kind") == "chat" and i.get("username")}


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


async def notify_user(username: str, payload: dict) -> None:
    key = username.lower()
    for ws, info in list(connections.items()):
        if info.get("kind") == "chat" and (info.get("username") or "").lower() == key:
            await _send(ws, payload)


async def broadcast_presence() -> None:
    online = _online_users()
    await broadcast_chat({
        "type": "presence",
        "total": len(online),
        "users": online,
    })


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
    def v(cls, v: str) -> str:
        v = v.strip()
        if not USERNAME_RE.match(v):
            raise ValueError("Юзернейм: 3–24 символа, только A-Z a-z 0-9 _ -")
        return v


class LoginBody(BaseModel):
    username: str
    password: str


class ChannelBody(BaseModel):
    name: str


class ChannelRenameBody(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def v(cls, v: str) -> str:
        v = (v or "").strip().lstrip("#").strip()
        if not (1 <= len(v) <= 32):
            raise ValueError("Название: 1–32 символа")
        return v


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


class MentionsReadBody(BaseModel):
    channel: Optional[str] = None


# ============================================================
#  FASTAPI
# ============================================================
app = FastAPI(title=APP_NAME)


# ---------- AUTH ----------
def _record_login(username: str, request: Request) -> None:
    u = users.get(username.lower())
    if not u:
        return
    ua = (request.headers.get("user-agent") or "")[:220]
    ip = client_ip(request)
    u.setdefault("logins", []).append({"ts": _now(), "ua": ua, "ip": ip})
    if len(u["logins"]) > 200:
        u["logins"] = u["logins"][-200:]
    u["last_seen"] = _now()


@app.post("/api/register")
def register(body: RegisterBody, request: Request):
    ip = client_ip(request)
    rate_check(login_attempts, "reg:" + ip, LOGIN_WINDOW, LOGIN_MAX, "Слишком много попыток")
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
    ip = client_ip(request)
    rate_check(login_attempts, "login:" + ip, LOGIN_WINDOW, LOGIN_MAX, "Слишком много попыток")
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
    return {"allow_channel_creation": ALLOW_CHANNEL_CREATION, "app_name": APP_NAME}


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


# ---------- MENTIONS ----------
@app.get("/api/mentions")
def list_mentions(user: str = Depends(require_user)):
    key = user.lower()
    lst = user_mentions.get(key, [])
    out = []
    for m in lst:
        if m["channel"] not in channels:
            continue
        out.append({
            "id": m["id"], "channel": m["channel"],
            "from": m["from"], "ts": m["ts"],
            "read": m["read"], "text": m["text"],
        })
    out.sort(key=lambda x: x["ts"])
    unread_by_channel: Dict[str, int] = {}
    for m in out:
        if not m["read"]:
            unread_by_channel[m["channel"]] = unread_by_channel.get(m["channel"], 0) + 1
    return {"mentions": out, "unread_by_channel": unread_by_channel}


@app.post("/api/mentions/read")
async def mentions_read(body: MentionsReadBody, user: str = Depends(require_user)):
    key = user.lower()
    lst = user_mentions.get(key, [])
    changed = 0
    for m in lst:
        if m["read"]:
            continue
        if body.channel is None or m["channel"] == body.channel:
            m["read"] = True
            changed += 1
    if changed:
        await notify_user(user, {"type": "mentions_read", "channel": body.channel})
    return {"ok": True, "changed": changed}


# ---------- CHANNELS ----------
def _online_in_channel(cid: str) -> int:
    return sum(1 for i in connections.values()
               if i.get("kind") == "chat" and i.get("channel") == cid)


@app.get("/api/channels")
def list_channels(user: str = Depends(require_user)):
    key = user.lower()
    unread_by_channel: Dict[str, int] = {}
    for m in user_mentions.get(key, []):
        if not m["read"]:
            unread_by_channel[m["channel"]] = unread_by_channel.get(m["channel"], 0) + 1

    out = []
    for cid, c in channels.items():
        lst = messages.get(cid) or []
        last = lst[-1] if lst else None
        out.append({
            "id": c["id"], "name": c["name"], "owner": c["owner"],
            "mention_count": unread_by_channel.get(cid, 0),
            "last_message": ({"user": last["user"], "text": last["text"], "ts": last["ts"]}
                             if last else None),
        })
    out.sort(key=lambda x: (x["last_message"]["ts"] if x["last_message"] else 0), reverse=True)
    return {"channels": out, "total_online": len(_online_usernames())}


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
    if not cid:
        raise HTTPException(400, "Некорректное название")
    if cid in channels:
        raise HTTPException(409, "Канал уже существует")
    channels[cid] = {"id": cid, "name": raw, "owner": user, "created": _now()}
    messages[cid] = []
    await broadcast_chat({"type": "channels_changed"})
    return {"id": cid, "name": raw}


# ============================================================
#  WEBSOCKET (чат)
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
    u = users.get(username.lower())
    if u:
        u["last_seen"] = now
    await broadcast_chat({"type": "message", "message": msg})

    # Парсим упоминания
    mentioned = set(MENTION_RE.findall(text))
    for nick in mentioned:
        nk = nick.lower()
        if nk not in users or nk == username.lower():
            continue
        entry = {
            "id": msg["id"], "channel": cid,
            "from": username, "ts": now,
            "read": False, "text": text,
        }
        bucket = user_mentions.setdefault(nk, [])
        bucket.append(entry)
        if len(bucket) > MAX_MENTIONS_PER_USER:
            del bucket[:-MAX_MENTIONS_PER_USER]
        await notify_user(nick, {
            "type": "mentioned",
            "channel": cid,
            "message_id": msg["id"],
            "from": username,
            "text": text,
            "ts": now,
        })


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
                # обновим текст в mentions
                for bucket in user_mentions.values():
                    for mm in bucket:
                        if mm["id"] == mid:
                            mm["text"] = text
                return


async def _handle_delete_message(mid: str, username: str) -> None:
    for cid, lst in messages.items():
        for i, m in enumerate(lst):
            if m["id"] == mid:
                if m["user"] != username:
                    return
                lst.pop(i)
                for bucket in user_mentions.values():
                    bucket[:] = [mm for mm in bucket if mm["id"] != mid]
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
    u = users.get(username.lower())
    if u:
        u["last_seen"] = _now()
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
                u = users.get(username.lower())
                if u:
                    u["last_seen"] = _now()
                await _send(ws, {"type": "pong"})
            elif t == "join":
                cid = data.get("channel")
                if cid not in channels:
                    await _send(ws, {"type": "channel_not_found", "channel": cid})
                    continue
                info["channel"] = cid
                msgs = messages.get(cid, [])[-200:]
                mentioned: Set[str] = set()
                for m in msgs:
                    for nick in MENTION_RE.findall(m["text"]):
                        mentioned.add(nick.lower())
                profiles = {k: public_user(k) for k in mentioned if k in users}
                await _send(ws, {
                    "type": "channel_joined",
                    "channel": cid,
                    "messages": msgs,
                    "profiles": profiles,
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
#  ADMIN API
# ============================================================
@app.post("/api/admin/login")
def admin_login(body: AdminLoginBody, request: Request, response: Response):
    if not ADMIN_PASS:
        raise HTTPException(403, "Админка отключена")
    ip = client_ip(request)
    rate_check(login_attempts, "admin:" + ip, LOGIN_WINDOW, LOGIN_MAX, "Слишком много попыток")
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
        "online": len(_online_usernames()),
        "tokens": len(tokens),
        "uptime_started": _STARTED_AT,
    }


# ---- USERS ----
@app.get("/api/admin/users")
def admin_users(_: None = Depends(require_admin)):
    online = _online_usernames()
    out = []
    for u in users.values():
        uname = u["username"]
        msg_count = sum(1 for lst in messages.values() for m in lst if m["user"] == uname)
        token_count = sum(1 for n in tokens.values() if n == uname)
        out.append({
            "username": uname,
            "display_name": u.get("display_name") or uname,
            "created": u["created"],
            "last_seen": u.get("last_seen", 0),
            "logins_count": len(u.get("logins", [])),
            "tokens": token_count,
            "messages": msg_count,
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
            d = _day_str(m["ts"])
            per_day[d] = per_day.get(d, 0) + 1
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
        "is_online": uname in _online_usernames(),
        "total_messages": total,
        "messages_by_channel": by_channel,
        "messages_per_day": per_day,
        "logins_per_day": logins_per_day,
        "days": days,
        "logins": list(reversed(logins[-50:])),
        "recent_messages": recent[:25],
        "mentions_sent": sum(1 for bucket in user_mentions.values()
                             for m in bucket if m["from"] == uname),
        "mentions_received": len(user_mentions.get(uname.lower(), [])),
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


@app.post("/api/admin/users/{username}/kick")
async def admin_kick_user(username: str, _: None = Depends(require_admin)):
    """Отключает все WS-сессии пользователя, но сохраняет аккаунт."""
    u = users.get(username.lower())
    if not u:
        raise HTTPException(404, "Нет такого пользователя")
    uname = u["username"]
    kicked = 0
    for ws, info in list(connections.items()):
        if info.get("kind") == "chat" and info.get("username") == uname:
            connections.pop(ws, None)
            try:
                await ws.close(code=1008)
            except Exception:
                pass
            kicked += 1
    # отзываем токены
    for t in [t for t, n in tokens.items() if n == uname]:
        del tokens[t]
    await broadcast_presence()
    return {"ok": True, "kicked": kicked}


@app.delete("/api/admin/users/{username}")
async def admin_delete_user(username: str, _: None = Depends(require_admin)):
    u = users.get(username.lower())
    if not u:
        raise HTTPException(404, "Нет такого пользователя")
    uname = u["username"]

    removed: List[tuple] = []
    for cid, lst in messages.items():
        kept = []
        for m in lst:
            if m["user"] == uname:
                removed.append((cid, m["id"]))
            else:
                kept.append(m)
        messages[cid] = kept

    kicked = 0
    for ws, info in list(connections.items()):
        if info.get("kind") == "chat" and info.get("username") == uname:
            connections.pop(ws, None)
            try:
                await ws.close(code=1008)
            except Exception:
                pass
            kicked += 1

    del users[username.lower()]
    for t in [t for t, n in tokens.items() if n == uname]:
        del tokens[t]
    user_mentions.pop(uname.lower(), None)
    for bucket in user_mentions.values():
        bucket[:] = [m for m in bucket if m["from"] != uname]

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
            "online": _online_in_channel(cid),
        })
    out.sort(key=lambda x: x["created"])
    return {"channels": out}


@app.post("/api/admin/channels")
async def admin_create_channel(body: ChannelBody, _: None = Depends(require_admin)):
    raw = body.name.strip().lstrip("#").strip()
    if not (1 <= len(raw) <= 32):
        raise HTTPException(400, "Название: 1–32 символа")
    cid = _slug(raw)
    if not cid:
        raise HTTPException(400, "Некорректное название")
    if cid in channels:
        raise HTTPException(409, "Канал уже существует")
    channels[cid] = {"id": cid, "name": raw, "owner": "admin", "created": _now()}
    messages[cid] = []
    await broadcast_chat({"type": "channels_changed"})
    return {"id": cid, "name": raw}


@app.patch("/api/admin/channels/{cid}")
async def admin_rename_channel(cid: str, body: ChannelRenameBody, _: None = Depends(require_admin)):
    if cid not in channels:
        raise HTTPException(404, "Нет такого канала")
    channels[cid]["name"] = body.name
    await broadcast_chat({"type": "channels_changed"})
    return {"id": cid, "name": body.name}


@app.post("/api/admin/channels/{cid}/kickall")
async def admin_kick_all_from_channel(cid: str, _: None = Depends(require_admin)):
    if cid not in channels:
        raise HTTPException(404, "Нет такого канала")
    kicked = 0
    for ws, info in list(connections.items()):
        if info.get("kind") == "chat" and info.get("channel") == cid:
            info["channel"] = None
            await _send(ws, {"type": "channel_removed", "channel": cid})
            kicked += 1
    await broadcast_presence()
    return {"ok": True, "kicked": kicked}


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
    for bucket in user_mentions.values():
        bucket[:] = [m for m in bucket if m["channel"] != cid]
    await broadcast_chat({"type": "channels_changed"})
    await broadcast_presence()
    return {"ok": True}


@app.delete("/api/admin/channels/{cid}/user/{username}")
async def admin_delete_user_from_channel(cid: str, username: str,
                                          _: None = Depends(require_admin)):
    if cid not in channels:
        raise HTTPException(404, "Нет такого канала")
    u = users.get(username.lower())
    if not u:
        raise HTTPException(404, "Нет такого пользователя")
    uname = u["username"]
    lst = messages.get(cid, [])
    kept = []
    removed_ids = []
    for m in lst:
        if m["user"] == uname:
            removed_ids.append(m["id"])
        else:
            kept.append(m)
    messages[cid] = kept
    for bucket in user_mentions.values():
        bucket[:] = [m for m in bucket if m["id"] not in removed_ids]
    for mid in removed_ids:
        await broadcast_chat({"type": "message_deleted", "id": mid, "channel": cid})
    return {"ok": True, "removed": len(removed_ids)}


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
                for bucket in user_mentions.values():
                    for mm in bucket:
                        if mm["id"] == mid:
                            mm["text"] = body.text
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
                for bucket in user_mentions.values():
                    bucket[:] = [mm for mm in bucket if mm["id"] != mid]
                await broadcast_chat({"type": "message_deleted", "id": mid, "channel": cid})
                return {"ok": True}
    raise HTTPException(404, "Сообщение не найдено")


@app.delete("/api/admin/channels/{cid}/messages")
async def admin_clear_channel(cid: str, _: None = Depends(require_admin)):
    if cid not in messages:
        raise HTTPException(404, "Нет такого канала")
    messages[cid] = []
    for bucket in user_mentions.values():
        bucket[:] = [m for m in bucket if m["channel"] != cid]
    await broadcast_chat({"type": "channel_cleared", "channel": cid})
    return {"ok": True}


# ============================================================
#  HTML: КЛИЕНТ
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#2f5d8a">
<title>{{APP_NAME}}</title>
<style>
:root{
  --bg:#e4e9ed; --panel:#fff; --panel2:#f2f5f8; --border:#dbe1e6;
  --text:#1e2429; --muted:#7c8a95; --accent:#2f5d8a; --accent-h:#264c72;
  --bub-in:#fff; --bub-out:#e3f4d9; --bub-out-t:#5a7a5a;
  --bub-mention:#ffe8c2; --bub-mention-b:#ffb86b;
  --mention:#e08a1e;
  --shadow:0 1px 2px rgba(0,0,0,.08); --chat-bg:#e4e9ed;
  --app-h: 100vh;
}
html[data-theme=dark]{
  --bg:#131820; --panel:#1a2029; --panel2:#212832; --border:#2a323d;
  --text:#dde5ee; --muted:#8794a1; --accent:#3f79a8; --accent-h:#4f89b8;
  --bub-in:#1e2731; --bub-out:#2a4d70; --bub-out-t:#a8c3dc;
  --bub-mention:#4a3512; --bub-mention-b:#a87a2f;
  --mention:#f0a94a;
  --shadow:0 1px 2px rgba(0,0,0,.4); --chat-bg:#0f141a;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;
  -webkit-user-select:none;user-select:none;-webkit-touch-callout:none;}
input,textarea{-webkit-user-select:text;user-select:text;}
html,body{height:var(--app-h);overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:14px;color:var(--text);background:var(--bg);}
button{font-family:inherit;cursor:pointer;border:none;background:none;color:inherit;}
input,textarea{font-family:inherit;}
svg{display:block;}

/* ---------- BOOT ---------- */
#boot{position:fixed;inset:0;z-index:200;display:flex;flex-direction:column;
  align-items:center;justify-content:center;background:var(--bg);color:var(--muted);
  animation:fadein .2s ease;}
#boot .logo{font-size:22px;font-weight:600;color:var(--accent);letter-spacing:.5px;}
.spinner{margin-top:18px;width:26px;height:26px;border:2px solid var(--border);
  border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
@keyframes fadein{from{opacity:0;}to{opacity:1;}}

/* ---------- AUTH ---------- */
#auth-screen{position:fixed;inset:0;z-index:100;display:none;
  align-items:center;justify-content:center;padding:20px;overflow-y:auto;
  background:var(--bg);}
#auth-screen.visible{display:flex;}
.auth-card{width:340px;max-width:100%;background:var(--panel);
  border:1px solid var(--border);border-radius:6px;padding:26px 24px;
  box-shadow:0 4px 24px rgba(0,0,0,.06);animation:fadein .18s ease;}
html[data-theme=dark] .auth-card{box-shadow:0 4px 24px rgba(0,0,0,.4);}
.auth-head{text-align:center;margin-bottom:18px;}
.auth-logo{font-size:22px;font-weight:600;color:var(--accent);letter-spacing:.5px;}
.auth-sub{font-size:12px;color:var(--muted);margin-top:4px;}
.auth-tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:14px;}
.auth-tab{flex:1;padding:10px 0;font-size:13.5px;font-weight:500;
  color:var(--muted);border-bottom:2px solid transparent;transition:color .12s,border-color .12s;}
.auth-tab.active{color:var(--accent);border-bottom-color:var(--accent);}
.auth-body input{width:100%;padding:10px 12px;margin-bottom:10px;
  border:1px solid var(--border);border-radius:4px;font-size:14px;outline:none;
  background:var(--panel);color:var(--text);transition:border-color .12s,box-shadow .12s;}
.auth-body input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(47,93,138,.15);}
.auth-error{color:#c33;font-size:12px;min-height:16px;margin-bottom:6px;}
.auth-submit{width:100%;padding:11px;background:var(--accent);color:#fff;
  border-radius:4px;font-size:14px;font-weight:500;
  transition:background .12s,transform .06s;}
.auth-submit:hover{background:var(--accent-h);}
.auth-submit:active{transform:scale(.985);}
.auth-submit:disabled{opacity:.55;cursor:default;}
.auth-hint{font-size:11px;color:var(--muted);margin-top:8px;line-height:1.5;}

/* ---------- LAYOUT ---------- */
#app{display:none;height:var(--app-h);}
#app.visible{display:flex;}

.sidebar{width:270px;flex-shrink:0;background:var(--panel);
  border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:0;}
.sidebar-header{height:52px;flex-shrink:0;background:var(--accent);color:#fff;
  display:flex;align-items:center;justify-content:space-between;padding:0 8px 0 14px;}
.me{display:flex;flex-direction:column;justify-content:center;min-width:0;
  cursor:pointer;padding:4px 8px;border-radius:4px;transition:background .12s;}
.me:hover{background:rgba(255,255,255,.1);}
#me-name{font-weight:600;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;line-height:1.2;}
#me-user{font-size:11px;opacity:.75;}
.icon-btn{width:34px;height:34px;border-radius:6px;
  display:flex;align-items:center;justify-content:center;
  color:#fff;opacity:.9;flex-shrink:0;transition:background .12s,opacity .12s,transform .08s;}
.icon-btn:hover{background:rgba(255,255,255,.15);opacity:1;}
.icon-btn:active{transform:scale(.94);}

.search-wrap{position:relative;padding:8px 10px;flex-shrink:0;border-bottom:1px solid var(--border);}
.search-icon{position:absolute;left:20px;top:50%;transform:translateY(-50%);
  color:var(--muted);pointer-events:none;}
.search-wrap input{width:100%;padding:7px 10px 7px 32px;background:var(--panel2);
  border:1px solid transparent;border-radius:4px;font-size:13px;outline:none;
  color:var(--text);transition:border-color .12s,background .12s;}
.search-wrap input:focus{background:var(--panel);border-color:var(--border);}

.channel-list{flex:1;min-height:0;overflow-y:auto;padding:4px 0;}
.channel-item{display:flex;align-items:center;gap:10px;padding:8px 12px;cursor:pointer;
  transition:background .12s;position:relative;}
.channel-item:hover{background:var(--panel2);}
.channel-item.active{background:var(--accent);color:#fff;}
.channel-item.active .channel-last{color:rgba(255,255,255,.8);}
.channel-hash{width:28px;height:28px;flex-shrink:0;border-radius:50%;
  background:var(--panel2);color:var(--accent);
  display:flex;align-items:center;justify-content:center;font-weight:600;font-size:13px;
  transition:background .12s,color .12s;}
.channel-item.active .channel-hash{background:rgba(255,255,255,.2);color:#fff;}
.channel-body{flex:1;min-width:0;}
.channel-row1{display:flex;align-items:baseline;justify-content:space-between;gap:6px;}
.channel-name{font-weight:500;font-size:13.5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.channel-last{font-size:12px;color:var(--muted);margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}

.channel-mention{
  min-width:20px;height:20px;padding:0 6px;border-radius:10px;
  background:var(--mention);color:#fff;
  font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;
  flex-shrink:0;position:relative;
  animation:mbadge-in .25s ease;
}
.channel-mention::before{
  content:"";position:absolute;inset:0;border-radius:10px;
  background:var(--mention);opacity:.45;
  animation:mbadge-pulse 2s ease-out infinite;
  pointer-events:none;
}
@keyframes mbadge-in{from{transform:scale(.4);opacity:0;}to{transform:scale(1);opacity:1;}}
@keyframes mbadge-pulse{
  0%{transform:scale(1);opacity:.4;}
  70%{transform:scale(1.5);opacity:0;}
  100%{transform:scale(1.5);opacity:0;}
}

.new-channel{display:flex;gap:6px;padding:8px 10px;flex-shrink:0;
  border-top:1px solid var(--border);background:var(--panel);}
.new-channel input{flex:1;min-width:0;padding:8px 10px;
  border:1px solid var(--border);border-radius:4px;font-size:13px;
  outline:none;background:var(--panel2);color:var(--text);
  transition:border-color .12s,background .12s;}
.new-channel input:focus{border-color:var(--accent);background:var(--panel);}
.new-channel button{width:36px;height:36px;border-radius:4px;background:var(--accent);
  color:#fff;flex-shrink:0;display:flex;align-items:center;justify-content:center;
  transition:background .12s,transform .08s;}
.new-channel button:hover{background:var(--accent-h);}
.new-channel button:active{transform:scale(.94);}

/* ---------- CHAT ---------- */
.chat{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0;}
.chat-header{height:52px;flex-shrink:0;background:var(--accent);color:#fff;
  display:flex;align-items:center;padding:0 8px 0 12px;gap:8px;
  z-index:2;box-shadow:0 1px 0 rgba(0,0,0,.06);}
#back-btn{display:none;width:34px;height:34px;align-items:center;justify-content:center;
  border-radius:6px;color:#fff;flex-shrink:0;transition:background .12s;}
#back-btn:hover{background:rgba(255,255,255,.15);}
.chat-title{display:flex;flex-direction:column;min-width:0;flex:1;}
#chat-name{font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;line-height:1.2;}
.chat-users{font-size:11px;opacity:.85;}

.messages-wrap{flex:1;min-height:0;display:flex;}
.messages{flex:1;min-height:0;overflow-y:auto;padding:14px 16px 8px;background:var(--chat-bg);
  scroll-behavior:smooth;}
.empty{text-align:center;color:var(--muted);margin-top:60px;font-size:13px;line-height:1.6;}

.msg{display:flex;margin-bottom:5px;flex-direction:column;position:relative;
  animation:msg-in .18s ease-out;}
@keyframes msg-in{
  from{opacity:0;transform:translateY(4px);}
  to{opacity:1;transform:none;}
}
.msg.in{align-items:flex-start;}
.msg.out{align-items:flex-end;}
.msg.same-user{margin-top:-1px;}
.msg.same-user .name{display:none;}
.bubble{max-width:74%;padding:6px 10px 5px;border-radius:8px;
  background:var(--bub-in);box-shadow:var(--shadow);
  word-wrap:break-word;overflow-wrap:break-word;color:var(--text);
  transition:box-shadow .15s;}
.msg.in .bubble{border-top-left-radius:2px;}
.msg.out .bubble{background:var(--bub-out);border-top-right-radius:2px;}
.msg.same-user .bubble{border-top-left-radius:8px;border-top-right-radius:8px;}

/* сообщения, где меня упомянули */
.msg.mentioned-me .bubble{
  background:var(--bub-mention);
  border-left:3px solid var(--bub-mention-b);
}
.msg.out.mentioned-me .bubble{border-left:none;border-right:3px solid var(--bub-mention-b);}
.msg.mentioned-me .bubble:hover{box-shadow:0 2px 8px rgba(224,138,30,.35);}

.msg.flash .bubble{
  animation:flash-hl 1.4s ease-out;
}
@keyframes flash-hl{
  0%{box-shadow:0 0 0 0 rgba(224,138,30,.8);}
  60%{box-shadow:0 0 0 8px rgba(224,138,30,0);}
  100%{box-shadow:0 0 0 0 rgba(224,138,30,0);}
}

.name{font-size:12.5px;font-weight:600;color:var(--accent);margin-bottom:2px;}
.text{white-space:pre-wrap;line-height:1.35;font-size:14px;}
.text .mention{color:var(--accent);font-weight:600;}
.text .mention.self{
  background:rgba(224,138,30,.22);color:var(--mention);
  padding:0 4px;border-radius:3px;
}
.text .edited{color:var(--muted);font-size:11px;margin-left:4px;}
.time{font-size:10.5px;color:var(--muted);text-align:right;margin-top:2px;margin-left:12px;}
.msg.out .time{color:var(--bub-out-t);}
.msg.mine{cursor:context-menu;}

/* ---------- MEMBERS ---------- */
.members-panel{width:200px;flex-shrink:0;background:var(--panel);
  border-left:1px solid var(--border);display:none;flex-direction:column;min-height:0;
  animation:slidein .18s ease;}
.members-panel.visible{display:flex;}
@keyframes slidein{from{transform:translateX(20px);opacity:0;}to{transform:none;opacity:1;}}
.members-head{padding:10px 14px;border-bottom:1px solid var(--border);
  font-size:11px;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);
  display:flex;justify-content:space-between;flex-shrink:0;}
.members-head b{color:var(--text);font-weight:600;}
.members-list{flex:1;min-height:0;overflow-y:auto;padding:4px 0;}
.member-item{padding:7px 14px;font-size:13px;display:flex;align-items:center;gap:8px;
  animation:msg-in .15s ease-out;}
.member-item .dot{width:6px;height:6px;border-radius:50%;background:#4caf50;flex-shrink:0;
  box-shadow:0 0 0 0 rgba(76,175,80,.5);animation:dot-pulse 2s ease-in-out infinite;}
@keyframes dot-pulse{
  0%,100%{box-shadow:0 0 0 0 rgba(76,175,80,.5);}
  50%{box-shadow:0 0 0 4px rgba(76,175,80,0);}
}
.member-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.member-name.self{color:var(--accent);font-weight:600;}

/* ---------- COMPOSER ---------- */
.composer{display:flex;align-items:flex-end;gap:8px;flex-shrink:0;
  padding:10px 14px calc(10px + env(safe-area-inset-bottom,0));
  background:var(--panel);border-top:1px solid var(--border);position:relative;}
#msg-input{flex:1;min-width:0;padding:10px 14px;background:var(--panel2);
  border:1px solid transparent;border-radius:20px;font-size:14px;line-height:1.4;
  max-height:120px;min-height:42px;resize:none;outline:none;color:var(--text);
  transition:background .12s,border-color .12s;}
#msg-input:focus{background:var(--panel);border-color:var(--border);}
.send-btn{width:42px;height:42px;flex-shrink:0;border-radius:50%;
  background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;
  transition:background .12s,transform .08s;}
.send-btn:hover{background:var(--accent-h);}
.send-btn:active{transform:scale(.94);}
.send-btn:disabled{background:var(--border);cursor:default;}

/* Кнопка "перейти к упоминанию" */
.jump-mention{
  position:absolute;right:66px;bottom:calc(100% + 8px);
  width:38px;height:38px;border-radius:50%;
  background:var(--mention);color:#fff;
  display:none;align-items:center;justify-content:center;
  box-shadow:0 4px 12px rgba(224,138,30,.4);
  transition:transform .15s,background .12s;
  z-index:15;
  animation:mbadge-in .2s ease-out;
}
.jump-mention.visible{display:flex;}
.jump-mention:hover{background:#c97a15;transform:translateY(-2px) scale(1.05);}
.jump-mention:active{transform:scale(.94);}
.jump-mention svg{pointer-events:none;}
.jump-mention .jm-count{
  position:absolute;top:-4px;right:-4px;
  background:#c33;color:#fff;font-size:10px;font-weight:700;
  min-width:16px;height:16px;padding:0 4px;border-radius:8px;
  display:flex;align-items:center;justify-content:center;
  border:2px solid var(--panel);
}

/* ---------- MENTION MENU ---------- */
.mention-menu{position:absolute;left:10px;right:10px;bottom:calc(100% + 4px);
  background:var(--panel);border:1px solid var(--border);border-radius:6px;
  box-shadow:0 4px 16px rgba(0,0,0,.15);max-height:200px;overflow-y:auto;
  z-index:20;display:none;animation:menu-in .14s ease-out;}
.mention-menu.visible{display:block;}
@keyframes menu-in{
  from{opacity:0;transform:translateY(4px);}
  to{opacity:1;transform:none;}
}
.mention-item{padding:8px 12px;cursor:pointer;font-size:13px;transition:background .1s;}
.mention-item:hover{background:var(--panel2);}
.mention-item .u{color:var(--muted);margin-left:6px;}

/* ---------- CONTEXT MENU ---------- */
.ctx-menu{position:fixed;z-index:400;background:var(--panel);
  border:1px solid var(--border);border-radius:6px;padding:4px;
  box-shadow:0 6px 20px rgba(0,0,0,.2);min-width:150px;display:none;
  animation:ctx-in .12s ease-out;transform-origin:top left;}
.ctx-menu.visible{display:block;}
@keyframes ctx-in{
  from{opacity:0;transform:scale(.94);}
  to{opacity:1;transform:scale(1);}
}
.ctx-item{display:flex;align-items:center;gap:8px;padding:8px 12px;
  border-radius:4px;font-size:13px;cursor:pointer;transition:background .1s;}
.ctx-item:hover{background:var(--panel2);}
.ctx-item.danger{color:#c33;}
.ctx-item.danger:hover{background:rgba(204,51,51,.1);}

/* ---------- MODALS ---------- */
.modal-backdrop{position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.5);
  display:none;align-items:center;justify-content:center;padding:20px;
  animation:fadein .15s ease;}
.modal-backdrop.visible{display:flex;}
.modal{background:var(--panel);border-radius:8px;padding:20px 22px;
  width:380px;max-width:100%;color:var(--text);
  animation:modal-in .18s ease-out;}
@keyframes modal-in{
  from{opacity:0;transform:translateY(8px) scale(.97);}
  to{opacity:1;transform:none;}
}
.modal h3{font-size:15px;margin-bottom:14px;font-weight:600;}
.modal label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;}
.modal input,.modal textarea{width:100%;padding:9px 11px;border:1px solid var(--border);
  border-radius:4px;font-size:14px;outline:none;background:var(--panel2);
  color:var(--text);margin-bottom:10px;font-family:inherit;
  transition:border-color .12s,background .12s;}
.modal textarea{min-height:90px;resize:vertical;}
.modal input:focus,.modal textarea:focus{border-color:var(--accent);background:var(--panel);}
.modal .row{display:flex;justify-content:flex-end;gap:8px;margin-top:6px;}
.modal .btn2{padding:8px 14px;border-radius:4px;font-size:13px;
  background:var(--panel2);color:var(--text);transition:background .12s;}
.modal .btn2:hover{background:var(--border);}
.modal .btn2.primary{background:var(--accent);color:#fff;}
.modal .btn2.primary:hover{background:var(--accent-h);}
.modal-err{color:#c33;font-size:12px;min-height:16px;margin-bottom:6px;}

/* ---------- MOBILE ---------- */
@media (max-width:800px){
  #app.visible{display:block;position:relative;overflow:hidden;}
  .sidebar{position:absolute;inset:0;width:100%;border-right:none;}
  .chat{position:absolute;inset:0;background:var(--chat-bg);
    transform:translateX(100%);transition:transform .22s ease;z-index:5;
    display:flex;flex-direction:column;min-height:0;
    height:var(--app-h);}
  #app.chat-open .chat{transform:translateX(0);}
  #back-btn{display:flex;}
  .bubble{max-width:82%;}
  .members-panel{position:absolute;top:52px;right:0;bottom:0;width:200px;
    z-index:8;box-shadow:-4px 0 16px rgba(0,0,0,.15);}
  .jump-mention{bottom:calc(100% + 6px);right:60px;}
}

.channel-list::-webkit-scrollbar,.messages::-webkit-scrollbar,
.members-list::-webkit-scrollbar,.mention-menu::-webkit-scrollbar{width:6px;height:6px;}
.channel-list::-webkit-scrollbar-thumb,.messages::-webkit-scrollbar-thumb,
.members-list::-webkit-scrollbar-thumb{background:rgba(0,0,0,.15);border-radius:3px;}
</style>
</head>
<body>

<div id="boot"><div class="logo">{{APP_NAME}}</div><div class="spinner"></div></div>

<div id="auth-screen">
  <div class="auth-card">
    <div class="auth-head">
      <div class="auth-logo">{{APP_NAME}}</div>
      <div class="auth-sub">веб-чат</div>
    </div>
    <div class="auth-tabs">
      <button type="button" class="auth-tab active" data-mode="login">Вход</button>
      <button type="button" class="auth-tab" data-mode="register">Регистрация</button>
    </div>
    <div class="auth-body">
      <div class="auth-error" id="auth-error"></div>
      <input id="reg-display" type="text" placeholder="Отображаемое имя" maxlength="32" style="display:none;">
      <input id="auth-user" type="text" placeholder="Юзернейм" autocomplete="username"
             autocapitalize="none" spellcheck="false" maxlength="24">
      <input id="auth-pass" type="password" placeholder="Пароль"
             autocomplete="current-password" maxlength="128">
      <input id="reg-pass2" type="password" placeholder="Повтор пароля"
             autocomplete="new-password" maxlength="128" style="display:none;">
      <button type="button" class="auth-submit" id="auth-submit">Войти</button>
      <div class="auth-hint" id="reg-hint" style="display:none;">
        Юзернейм: 3–24, латиница, цифры, _ и -. Используется для @упоминаний.
      </div>
    </div>
  </div>
</div>

<div id="app">
  <aside class="sidebar">
    <div class="sidebar-header">
      <div class="me" id="me-block" title="Профиль">
        <div id="me-name">…</div>
        <div id="me-user"></div>
      </div>
      <div style="display:flex;gap:2px;">
        <button class="icon-btn" id="theme-btn" title="Тема">
          <svg id="theme-icon" viewBox="0 0 24 24" width="18" height="18"></svg>
        </button>
        <button class="icon-btn" id="logout-btn" title="Выйти">
          <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg>
        </button>
      </div>
    </div>
    <div class="search-wrap">
      <svg class="search-icon" viewBox="0 0 24 24" width="15" height="15"><path fill="currentColor" d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
      <input id="search" placeholder="Поиск каналов">
    </div>
    <div class="channel-list" id="channel-list"></div>
    <div class="new-channel" id="new-channel-wrap">
      <input id="new-channel-name" placeholder="Новый канал" maxlength="32">
      <button id="new-channel-btn" title="Создать">
        <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
      </button>
    </div>
  </aside>

  <main class="chat" id="chat">
    <div class="chat-header">
      <button class="icon-btn" id="back-btn">
        <svg viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg>
      </button>
      <div class="chat-title">
        <span id="chat-name">Выберите канал</span>
        <span class="chat-users" id="chat-users"></span>
      </div>
      <button class="icon-btn" id="members-btn" title="Участники">
        <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M16 11c1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3 1.34 3 3 3zm-8 0c1.66 0 3-1.34 3-3S9.66 5 8 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
      </button>
    </div>
    <div class="messages-wrap">
      <div class="messages" id="messages"><div class="empty">Выберите канал слева</div></div>
      <aside class="members-panel" id="members-panel">
        <div class="members-head"><span>Онлайн</span><b id="members-count">0</b></div>
        <div class="members-list" id="members-list"></div>
      </aside>
    </div>
    <div class="composer">
      <button class="jump-mention" id="jump-mention" title="Перейти к упоминанию">
        <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10h5v-2h-5c-4.34 0-8-3.66-8-8s3.66-8 8-8 8 3.66 8 8v1.43c0 .79-.71 1.57-1.5 1.57s-1.5-.78-1.5-1.57V12c0-2.76-2.24-5-5-5s-5 2.24-5 5 2.24 5 5 5c1.38 0 2.64-.56 3.54-1.47.65.89 1.77 1.47 2.96 1.47 1.97 0 3.5-1.6 3.5-3.57V12c0-5.52-4.48-10-10-10zm0 13c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3z"/></svg>
        <span class="jm-count" id="jm-count">0</span>
      </button>
      <div class="mention-menu" id="mention-menu"></div>
      <textarea id="msg-input" placeholder="Сообщение..." rows="1" enterkeyhint="send"></textarea>
      <button class="send-btn" id="send-btn" aria-label="Отправить">
        <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
      </button>
    </div>
  </main>
</div>

<div class="ctx-menu" id="ctx-menu">
  <div class="ctx-item" data-act="edit">
    <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34a.9959.9959 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
    Редактировать
  </div>
  <div class="ctx-item danger" data-act="del">
    <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
    Удалить
  </div>
</div>

<div class="modal-backdrop" id="profile-modal">
  <div class="modal">
    <h3>Профиль</h3>
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
    <p style="color:var(--muted);font-size:13px;margin-bottom:16px;" id="session-text">Войдите заново.</p>
    <div class="row"><button class="btn2 primary" id="session-ok">Ок</button></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const LS_TOKEN='sld_token', LS_THEME='sld_theme';
const state = {
  token: localStorage.getItem(LS_TOKEN) || null,
  username: null, display_name: null,
  channels: [], currentChannel: null,
  ws: null, wsReady: false,
  reconnectTimer: null, pingTimer: null,
  profiles: {}, allowChannelCreation: true,
  totalOnline: 0, onlineUsers: [],
  mentions: [],                    // все упоминания меня
  mentionByChannel: {},            // channel -> [mention]
  mentionIds: new Set(),           // ids всех сообщений, где меня упомянули
  _openInFlight: null, _lastOpenChannel: null, _lastOpenAt: 0,
  _jumpTarget: null,
};
let authMode = 'login';

/* ---------- VIEWPORT FIX (mobile keyboard) ---------- */
function fitViewport(){
  const h = (window.visualViewport ? window.visualViewport.height : window.innerHeight);
  document.documentElement.style.setProperty('--app-h', h + 'px');
}
if (window.visualViewport) window.visualViewport.addEventListener('resize', fitViewport);
window.addEventListener('orientationchange', fitViewport);
window.addEventListener('resize', fitViewport);
fitViewport();

/* ---------- Prevent right-click (кроме своих сообщений) ---------- */
document.addEventListener('contextmenu', e => {
  if (!e.target.closest('.msg.mine') && !e.target.closest('.ctx-menu')) e.preventDefault();
});

/* ---------- THEME ---------- */
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  const ic = $('theme-icon');
  if (t === 'dark') ic.innerHTML='<path fill="currentColor" d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z"/>';
  else ic.innerHTML='<path fill="currentColor" d="M20 8.69V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12 20 8.69zM12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6 6 2.69 6 6-2.69 6-6 6z"/>';
}
applyTheme(localStorage.getItem(LS_THEME) || 'light');
$('theme-btn').addEventListener('click', () => {
  const n = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  applyTheme(n); localStorage.setItem(LS_THEME, n);
});

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const h = Object.assign({}, opts.headers || {});
  if (state.token) h['X-Auth-Token'] = state.token;
  if (opts.body && typeof opts.body !== 'string') {
    h['Content-Type'] = 'application/json'; opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, Object.assign({}, opts, { headers: h }));
  if (!r.ok) {
    let d = r.statusText;
    try { d = (await r.json()).detail || d; } catch(e){}
    const err = new Error(d); err.status = r.status; throw err;
  }
  return r.json();
}

/* ---------- UTILS ---------- */
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function hhmm(ts){const t=new Date(ts*1000);if(isNaN(t))return '';return String(t.getHours()).padStart(2,'0')+':'+String(t.getMinutes()).padStart(2,'0');}
function renderTextWithMentions(text, self){
  const safe = escapeHtml(text);
  return safe.replace(/(^|[^A-Za-z0-9_\-])@([A-Za-z0-9_\-]{3,24})/g,(m,p1,nick)=>{
    const isSelf = self && nick.toLowerCase()===self.toLowerCase();
    return `${p1}<span class="mention${isSelf?' self':''}">@${nick}</span>`;
  });
}

/* ---------- SESSION MODAL ---------- */
function sessionModal(title, text){
  $('session-title').textContent = title;
  $('session-text').textContent = text;
  $('session-modal').classList.add('visible');
}
$('session-ok').addEventListener('click', () => {
  $('session-modal').classList.remove('visible');
  hardLogout();
});

/* ---------- AUTH ---------- */
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
  if (!u || !p) { e.textContent = 'Заполните поля'; return; }
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
  state.token = null; state.username = null; state.display_name = null;
  state.currentChannel = null; state.channels = [];
  state.mentions = []; state.mentionByChannel = {}; state.mentionIds = new Set();
  localStorage.removeItem(LS_TOKEN);
  $('app').classList.remove('visible','chat-open');
  $('auth-screen').classList.add('visible');
  $('auth-pass').value = ''; $('reg-pass2').value = ''; $('reg-display').value = '';
  setAuthMode('login');
}
$('logout-btn').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST' }); } catch(e){}
  hardLogout();
});

/* ---------- BOOT ---------- */
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
  await loadMentions();
  await loadChannels();
  $('boot').style.display = 'none';
  $('auth-screen').classList.remove('visible');
  $('app').classList.add('visible');
  connectWs();
}

/* ---------- MENTIONS ---------- */
async function loadMentions(){
  try {
    const d = await api('/api/mentions');
    state.mentions = d.mentions || [];
    state.mentionIds = new Set(state.mentions.map(m => m.id));
    state.mentionByChannel = {};
    for (const m of state.mentions) {
      (state.mentionByChannel[m.channel] = state.mentionByChannel[m.channel] || []).push(m);
    }
    updateJumpButton();
  } catch(e){}
}

function unreadMentionsFor(channel){
  const lst = state.mentionByChannel[channel] || [];
  return lst.filter(m => !m.read);
}

function updateJumpButton(){
  const c = state.currentChannel;
  if (!c) { $('jump-mention').classList.remove('visible'); return; }
  const unread = unreadMentionsFor(c);
  if (unread.length) {
    $('jm-count').textContent = unread.length;
    $('jump-mention').classList.add('visible');
  } else {
    $('jump-mention').classList.remove('visible');
  }
}

$('jump-mention').addEventListener('click', async () => {
  const c = state.currentChannel;
  if (!c) return;
  const unread = unreadMentionsFor(c);
  if (!unread.length) return;
  // Переходим к первому непрочитанному
  const target = unread[0];
  const el = $('messages').querySelector(`.msg[data-id="${CSS.escape(target.id)}"]`);
  if (el) {
    el.scrollIntoView({ behavior:'smooth', block:'center' });
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1600);
  } else {
    // сообщение вне текущего буфера — перезапросим канал
    if (state.ws && state.wsReady) {
      state.ws.send(JSON.stringify({type:'join', channel:c}));
      state._jumpTarget = target.id;
    }
  }
  // Помечаем прочитанным на сервере
  try {
    await api('/api/mentions/read', { method:'POST', body:{ channel: c } });
  } catch(e){}
  for (const m of unread) m.read = true;
  updateJumpButton();
  renderChannels();
});

/* ---------- PROFILE ---------- */
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

/* ---------- CHANNELS ---------- */
async function loadChannels(){
  try {
    const data = await api('/api/channels');
    state.channels = data.channels;
    state.totalOnline = data.total_online || 0;
    if (state.currentChannel && !state.channels.some(c=>c.id===state.currentChannel)) {
      state.currentChannel = null;
      $('app').classList.remove('chat-open');
      $('chat-name').textContent = 'Выберите канал';
      renderMessages([]);
      updateJumpButton();
    }
    renderChannels();
    updateHeader();
  } catch(e){}
}

function updateHeader(){
  if (state.currentChannel) {
    $('chat-users').textContent = state.totalOnline + ' онлайн';
  } else {
    $('chat-users').textContent = '';
  }
  $('members-count').textContent = state.onlineUsers.length || state.totalOnline || 0;
  renderMembers();
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
    const unread = (state.mentionByChannel[c.id] || []).filter(m=>!m.read).length;
    const badge = unread
      ? `<div class="channel-mention" title="${unread} упоминаний">@ ${unread}</div>` : '';
    el.innerHTML =
      `<div class="channel-hash">#</div>
       <div class="channel-body">
         <div class="channel-row1">
           <div class="channel-name">${escapeHtml(c.name)}</div>
         </div>
         ${last}
       </div>
       ${badge}`;
    el.addEventListener('click', () => openChannel(c.id));
    list.appendChild(el);
  }
}

function openChannel(id){
  const now = Date.now();
  if (state._openInFlight === id) return;
  if (id === state._lastOpenChannel && now - state._lastOpenAt < 500) return;
  state._lastOpenChannel = id; state._lastOpenAt = now;
  state._openInFlight = id;

  state.currentChannel = id;
  $('app').classList.add('chat-open');
  renderChannels();
  const ch = state.channels.find(c=>c.id===id);
  $('chat-name').textContent = ch ? '# '+ch.name : '# '+id;
  updateHeader();
  updateJumpButton();
  renderMessages([]);

  if (state.ws && state.wsReady) {
    state.ws.send(JSON.stringify({type:'join', channel:id}));
  }
  setTimeout(() => { state._openInFlight = null; }, 200);
}

/* ---------- MESSAGES ---------- */
function renderMessages(msgs){
  const box = $('messages');
  box.innerHTML = '';
  if (!msgs || !msgs.length) {
    box.innerHTML = '<div class="empty">Здесь пока нет сообщений</div>';
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
  const isMentioned = state.mentionIds.has(m.id);
  const div = document.createElement('div');
  div.className = 'msg ' + (out ? 'out' : 'in')
    + (same ? ' same-user' : '')
    + (out ? ' mine' : '')
    + (isMentioned ? ' mentioned-me' : '');
  div.dataset.user = m.user; div.dataset.id = m.id;
  const nameHtml = out ? '' : `<div class="name">${escapeHtml(prof.display_name||prof.username)}</div>`;
  const edited = m.edited ? ' <span class="edited">(изм.)</span>' : '';
  const textHtml = renderTextWithMentions(m.text, state.username);
  div.innerHTML =
    `<div class="bubble">${nameHtml}
       <div class="text">${textHtml}${edited}</div>
       <div class="time">${hhmm(m.ts)}</div>
     </div>`;
  if (out) {
    div.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      openCtxMenu(e.clientX, e.clientY, m.id, m.text);
    });
  }
  box.appendChild(div);
  if (!opts.skipScroll) box.scrollTop = box.scrollHeight;

  // Если ждали перехода к упоминанию — прыгаем
  if (state._jumpTarget && state._jumpTarget === m.id) {
    state._jumpTarget = null;
    setTimeout(() => {
      div.scrollIntoView({ behavior:'smooth', block:'center' });
      div.classList.add('flash');
      setTimeout(() => div.classList.remove('flash'), 1600);
    }, 50);
  }
}

/* ---------- CONTEXT MENU ---------- */
let ctxTarget = {id:null, text:null};
function openCtxMenu(x, y, id, text){
  ctxTarget = {id, text};
  const menu = $('ctx-menu');
  menu.style.left = '0px'; menu.style.top = '0px';
  menu.classList.add('visible');
  const mw = menu.offsetWidth || 160;
  const mh = menu.offsetHeight || 80;
  let px = x, py = y;
  if (px + mw > window.innerWidth - 8) px = window.innerWidth - mw - 8;
  if (py + mh > window.innerHeight - 8) py = window.innerHeight - mh - 8;
  if (px < 8) px = 8;
  if (py < 8) py = 8;
  menu.style.left = px + 'px';
  menu.style.top = py + 'px';
}
function closeCtxMenu(){ $('ctx-menu').classList.remove('visible'); }
document.addEventListener('click', e => { if (!e.target.closest('.ctx-menu')) closeCtxMenu(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeCtxMenu(); });
window.addEventListener('blur', closeCtxMenu);
document.addEventListener('scroll', closeCtxMenu, true);

document.querySelectorAll('#ctx-menu .ctx-item').forEach(it => {
  it.addEventListener('click', () => {
    const act = it.dataset.act;
    const {id, text} = ctxTarget;
    closeCtxMenu();
    if (act === 'edit') openEditMsg(id, text);
    else if (act === 'del') {
      if (confirm('Удалить это сообщение?')) {
        state.ws.send(JSON.stringify({type:'delete_message', id}));
      }
    }
  });
});

/* ---------- EDIT MSG ---------- */
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

/* ---------- MEMBERS (весь сервер) ---------- */
const MEMBERS_LS = 'sld_members_visible';
if (localStorage.getItem(MEMBERS_LS) === '1') $('members-panel').classList.add('visible');
$('members-btn').addEventListener('click', () => {
  const p = $('members-panel');
  p.classList.toggle('visible');
  localStorage.setItem(MEMBERS_LS, p.classList.contains('visible')?'1':'0');
});
function renderMembers(){
  const list = $('members-list');
  const users = state.onlineUsers || [];
  $('members-count').textContent = users.length;
  if (!users.length) {
    list.innerHTML = '<div style="padding:20px 14px;color:var(--muted);font-size:12px;text-align:center;">Пусто</div>';
    return;
  }
  list.innerHTML = '';
  for (const m of users) {
    const el = document.createElement('div');
    el.className = 'member-item';
    const self = m.username === state.username;
    el.innerHTML = `<div class="dot"></div><div class="member-name${self?' self':''}">${
      escapeHtml(m.display_name||m.username)}</div>`;
    el.title = '@' + m.username;
    list.appendChild(el);
  }
}

/* ---------- WS ---------- */
function connectWs(){
  if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch(e){} state.ws = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.ws = ws; state.wsReady = false;

  ws.onopen = () => {
    state.wsReady = true;
    if (state.currentChannel) ws.send(JSON.stringify({type:'join', channel: state.currentChannel}));
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
    state.onlineUsers = d.users || [];
    updateHeader();
    renderChannels();
  } else if (d.type === 'channel_joined') {
    if (d.channel !== state.currentChannel) return;
    if (d.profiles) Object.values(d.profiles).forEach(p => {
      state.profiles[(p.username||'').toLowerCase()] = p;
    });
    renderMessages(d.messages||[]);
    updateJumpButton();
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
  } else if (d.type === 'mentioned') {
    // меня упомянули
    const entry = {
      id: d.message_id, channel: d.channel,
      from: d.from, ts: d.ts, read: false, text: d.text,
    };
    state.mentions.push(entry);
    state.mentionIds.add(d.message_id);
    (state.mentionByChannel[d.channel] = state.mentionByChannel[d.channel] || []).push(entry);
    // пометить существующее сообщение оранжевым
    const el = $('messages').querySelector(`.msg[data-id="${CSS.escape(d.message_id)}"]`);
    if (el) el.classList.add('mentioned-me');
    updateJumpButton();
    renderChannels();
    // лёгкий звуковой/визуальный сигнал в заголовке
    if (document.hidden) document.title = '• ' + (state.mentions.length) + ' — {{APP_NAME}}';
  } else if (d.type === 'mentions_read') {
    // сервер подтвердил — можно ничего не делать
  } else if (d.type === 'message_edited') {
    if (d.channel !== state.currentChannel) return;
    const el = $('messages').querySelector(`.msg[data-id="${d.id}"] .text`);
    if (el) el.innerHTML = renderTextWithMentions(d.text, state.username) + ' <span class="edited">(изм.)</span>';
  } else if (d.type === 'message_deleted') {
    if (d.channel === state.currentChannel) {
      const el = $('messages').querySelector(`.msg[data-id="${d.id}"]`);
      if (el) el.remove();
    }
    state.mentions = state.mentions.filter(m => m.id !== d.id);
    state.mentionIds.delete(d.id);
    for (const k of Object.keys(state.mentionByChannel)) {
      state.mentionByChannel[k] = state.mentionByChannel[k].filter(m => m.id !== d.id);
    }
    updateJumpButton();
    loadChannels();
  } else if (d.type === 'channels_changed') {
    loadChannels();
  } else if (d.type === 'channel_cleared') {
    if (d.channel === state.currentChannel) renderMessages([]);
    loadChannels();
    loadMentions();
  } else if (d.type === 'channel_removed') {
    if (d.channel === state.currentChannel) {
      state.currentChannel = null;
      $('app').classList.remove('chat-open');
      $('chat-name').textContent = 'Выберите канал';
      renderMessages([]);
      updateJumpButton();
    }
    loadChannels();
    loadMentions();
  } else if (d.type === 'profile_updated') {
    state.profiles[(d.username||'').toLowerCase()] = {username:d.username, display_name:d.display_name};
    if (d.username === state.username) { state.display_name = d.display_name; paintMe(); }
    document.querySelectorAll(`.msg[data-user="${CSS.escape(d.username)}"] .name`)
      .forEach(el => { el.textContent = d.display_name; });
  } else if (d.type === 'user_deleted') {
    if (d.username === state.username) {
      sessionModal('Аккаунт удалён', 'Ваш аккаунт был удалён.');
      return;
    }
    document.querySelectorAll(`.msg[data-user="${CSS.escape(d.username)}"]`).forEach(el => el.remove());
    loadMentions();
  } else if (d.type === 'session_expired') {
    sessionModal('Сессия истекла', 'Ваша сессия недействительна.');
  } else if (d.type === 'channel_not_found') {
    state.currentChannel = null;
    $('app').classList.remove('chat-open');
    $('chat-name').textContent = 'Выберите канал';
    renderMessages([]);
    updateJumpButton();
  }
}

/* ---------- MENTIONS AUTOCOMPLETE ---------- */
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
  const cands = (state.onlineUsers || [])
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
  e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
  const q = currentMQ();
  if (q !== null) showMQ(q); else hideMQ();
});
$('msg-input').addEventListener('blur', ()=>setTimeout(hideMQ, 120));

/* ---------- SEND ---------- */
function sendMessage(){
  const inp = $('msg-input');
  const text = inp.value.trim();
  if (!text || !state.ws || !state.wsReady || !state.currentChannel) return;
  state.ws.send(JSON.stringify({type:'message', text}));
  inp.value=''; inp.style.height='auto';
  hideMQ();
  inp.focus();   // фокус остаётся на поле
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
  $('new-channel-btn').disabled = true;
  try {
    await api('/api/channels', { method:'POST', body:{name} });
    $('new-channel-name').value = '';
    await loadChannels();
  } catch(e){ alert(e.message); }
  finally { $('new-channel-btn').disabled = false; }
});
$('new-channel-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('new-channel-btn').click();
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) document.title = '{{APP_NAME}}';
});

boot();
</script>
</body>
</html>
"""

HTML_PAGE = HTML_PAGE.replace("{{APP_NAME}}", APP_NAME)


# ============================================================
#  HTML: АДМИНКА
# ============================================================
ADMIN_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=960">
<title>{{APP_NAME}} · admin</title>
<style>
:root{
  --bg:#f4f5f7;--panel:#fff;--panel2:#f5f6f8;--border:#d9dce1;--border2:#ebedf0;
  --text:#1a1a1a;--muted:#5c6670;--accent:#0066cc;--accent-h:#0055ad;
  --green:#3db83d;--red:#cc0000;--topbg:#22272e;--topfg:#eaecef;
  --side:#f4f5f7;--side-h:#e6e8eb;--side-a:#dde1e5;
}
html[data-theme=dark]{
  --bg:#161a1f;--panel:#1c2229;--panel2:#232a32;--border:#2e3641;--border2:#262d36;
  --text:#e0e6ed;--muted:#8b96a2;--accent:#3b82f6;--accent-h:#2563eb;
  --green:#22c55e;--red:#ef4444;--topbg:#0e1216;--topfg:#e0e6ed;
  --side:#181d23;--side-h:#232a32;--side-a:#2a323b;
}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;font-size:13px;
  color:var(--text);background:var(--bg);min-width:760px;}
button{font-family:inherit;font-size:inherit;cursor:pointer;border:none;background:none;color:inherit;}
input,select,textarea{font-family:inherit;}
svg{display:block;}
a{color:var(--accent);text-decoration:none;}
a:hover{text-decoration:underline;}

#login-view{position:fixed;inset:0;z-index:100;display:flex;align-items:center;justify-content:center;background:var(--bg);}
.login-card{width:340px;background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:26px 24px;}
.login-logo{font-size:22px;text-align:center;color:var(--text);font-weight:600;}
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

.content{flex:1;padding:18px 22px;background:var(--panel);min-width:0;overflow-x:hidden;}
.page-title{font-size:19px;font-weight:500;margin:0 0 4px;}
.page-sub{color:var(--muted);font-size:12.5px;margin-bottom:16px;}

.card{background:var(--panel);border:1px solid var(--border);border-radius:3px;padding:16px 18px;margin-bottom:14px;}
.card h3{font-size:14.5px;font-weight:500;margin:0 0 12px;}
.grid-2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;}
.grid-4{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;}
.kv{display:grid;grid-template-columns:150px 1fr;gap:6px 14px;font-size:13px;}
.kv .k{color:var(--muted);}
.metric{font-size:24px;font-weight:400;line-height:1;color:var(--text);}
.metric-label{font-size:12px;color:var(--muted);margin-top:6px;}

.health{color:var(--green);display:flex;align-items:center;gap:8px;font-size:13px;}
.usage-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:13px;}
.usage-row .label{width:70px;color:var(--muted);}
.usage-bar{flex:1;height:8px;background:var(--border2);border-radius:4px;overflow:hidden;}
.usage-bar>span{display:block;height:100%;background:var(--accent);border-radius:4px;}
.usage-row .val{min-width:90px;text-align:right;color:var(--muted);font-size:12px;}

table{width:100%;border-collapse:collapse;font-size:13px;table-layout:auto;}
th{text-align:left;padding:8px 10px;color:var(--muted);font-weight:500;font-size:12px;background:var(--side);border-bottom:1px solid var(--border);white-space:nowrap;}
td{padding:8px 10px;border-bottom:1px solid var(--border2);vertical-align:top;word-break:break-word;}
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
.chip.warn{background:#fff5e6;color:#8a5c1a;border-color:#f0d5b0;}
html[data-theme=dark] .chip.warn{background:#3a2d12;color:#f0c88a;border-color:#6b5228;}

.btn{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:3px;font-size:12px;transition:background .1s;}
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
.toast{padding:10px 14px;border-radius:3px;background:var(--topbg);color:var(--topfg);font-size:12.5px;border-left:3px solid var(--green);box-shadow:0 4px 12px rgba(0,0,0,.15);animation:toastin .18s ease-out;}
@keyframes toastin{from{opacity:0;transform:translateX(12px);}to{opacity:1;transform:none;}}
.toast.err{border-left-color:var(--red);}

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
    <div class="login-logo">{{APP_NAME}}</div>
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
      <span>admin@{{APP_NAME}}</span>
      <span class="sep">·</span>
      <span id="crumb-section">Обзор</span>
      <span class="tag">admin</span>
    </div>
    <div class="actions">
      <button class="btn-t" id="theme-btn" title="Тема"><svg id="theme-icon" viewBox="0 0 24 24" width="14" height="14"></svg></button>
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
      <div class="side-search"><input id="side-search" placeholder="Поиск"></div>
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

<div class="modal-backdrop" id="chan-edit-modal">
  <div class="modal">
    <h3>Переименовать канал</h3>
    <div class="modal-err" id="ce-err"></div>
    <label>Название</label>
    <input id="ce-name" type="text" maxlength="32">
    <div class="row">
      <button class="btn" id="ce-cancel">Отмена</button>
      <button class="btn primary" id="ce-save">Сохранить</button>
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
const state = { tab:'dashboard', subId:null, currentUser:null, currentChan:null, currentMsg:null,
                _isRendering:false, _lastRender:0 };

function escapeHtml(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmtTs(ts){if(!ts)return '—';const d=new Date(ts*1000);const p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;}
function fmtRel(ts){
  if(!ts) return '—';
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (s < 15) return 'только что';
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
  if (t === 'dark') ic.innerHTML='<path fill="currentColor" d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z"/>';
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
function showPanel(){ $('login-view').style.display='none'; $('panel-view').classList.add('visible'); renderTab(); }
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
  showLogin();
});
$('refresh-btn').addEventListener('click', () => renderTab({ force: true }));

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
  const label = { dashboard:'Обзор', users:'Пользователи', channels:'Каналы', messages:'Сообщения' }[tab] || tab;
  $('crumb-section').textContent = label;
}

function isTyping(){
  const a = document.activeElement;
  return a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT');
}

async function renderTab(opts = {}){
  if (state._isRendering && !opts.force) return;
  const now = Date.now();
  if (!opts.force && now - state._lastRender < 500) return;
  state._lastRender = now;
  state._isRendering = true;
  const { tab, sub } = parseHash();
  state.tab = tab; state.subId = sub;
  setActiveTab(tab);
  const main = $('main');
  try {
    if (tab === 'dashboard') await renderDashboard(main);
    else if (tab === 'users') {
      if (sub) await renderUserDetail(main, sub);
      else await renderUsers(main);
    }
    else if (tab === 'channels') await renderChannels(main);
    else if (tab === 'messages') await renderMessages(main);
  } catch(e){
    if (e.status === 401) { showLogin(); return; }
    main.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
  state._isRendering = false;
}

setInterval(() => {
  if (!$('panel-view').classList.contains('visible')) return;
  if (state._isRendering) return;
  if (isTyping()) return;
  if (document.querySelector('.modal-backdrop.visible')) return;
  renderTab({ force: true });
}, 15000);

/* DASHBOARD */
async function renderDashboard(main){
  const s = await api('/api/admin/stats');
  const up = Math.floor(Date.now()/1000 - s.uptime_started);
  const d = Math.floor(up/86400), h = Math.floor((up%86400)/3600),
        m = Math.floor((up%3600)/60), sec = up%60;
  main.innerHTML = `
    <h2 class="page-title">Обзор</h2>
    <div class="page-sub">Сводка состояния сервера</div>
    <div class="grid-2">
      <div class="card">
        <h3>Состояние</h3>
        <div class="health">
          <svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
          <span>Сервер работает</span>
        </div>
        <div class="kv" style="margin-top:14px;">
          <div class="k">Приложение</div><div class="v">{{APP_NAME}} · FastAPI</div>
          <div class="k">Uptime</div><div class="v">${d?d+' дн ':''}${h}ч ${m}м ${sec}с</div>
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
async function renderUsers(main){
  const data = await api('/api/admin/users');
  const rows = data.users.map(u => {
    const status = u.is_online
      ? '<span class="chip green">онлайн</span>'
      : '<span class="chip">offline</span>';
    const activity = u.is_online ? '<span class="green">сейчас</span>' : fmtRel(u.last_seen);
    return `<tr>
      <td><a href="#/users/${encodeURIComponent(u.username)}" class="mono accent">${escapeHtml(u.username)}</a></td>
      <td>${escapeHtml(u.display_name)}</td>
      <td class="muted mono">${fmtTs(u.created)}</td>
      <td class="muted">${activity}</td>
      <td class="muted">${u.logins_count}</td>
      <td>${u.messages}</td>
      <td>${status}</td>
      <td style="white-space:nowrap;">
        <button class="btn mini" data-edit-user="${escapeHtml(u.username)}" data-edit-name="${escapeHtml(u.display_name)}">Изменить</button>
        <button class="btn mini" data-kick-user="${escapeHtml(u.username)}">Кик</button>
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
  main.querySelectorAll('[data-kick-user]').forEach(b => {
    b.addEventListener('click', () => kickUser(b.dataset.kickUser));
  });
  main.querySelectorAll('[data-del-user]').forEach(b => {
    b.addEventListener('click', () => deleteUser(b.dataset.delUser));
  });
}

async function kickUser(username){
  if (!confirm(`Кикнуть "${username}"? Аккаунт останется, но сессии сбросятся.`)) return;
  try {
    const r = await api('/api/admin/users/' + encodeURIComponent(username) + '/kick', { method:'POST' });
    toast(`Кикнут (${r.kicked} сессий)`);
    renderTab({force:true});
  } catch(e){ toast(e.message,'err'); }
}

/* USER DETAIL */
async function renderUserDetail(main, username){
  const d = await api('/api/admin/users/' + encodeURIComponent(username));
  const days = d.days || [];
  const msgsPerDay = days.map(day => ({ label: day.slice(5), value: d.messages_per_day[day] || 0 }));
  const loginsPerDay = days.map(day => ({ label: day.slice(5), value: d.logins_per_day[day] || 0 }));
  const pie = Object.entries(d.messages_by_channel || {}).sort((a,b)=>b[1]-a[1]);
  const palette = ['#0066cc','#3db83d','#f0ad4e','#d9534f','#9b59b6','#16a085','#e67e22','#34495e'];
  const pieData = pie.map(([ch,n],i) => ({ label: '#' + ch, value: n, color: palette[i%palette.length] }));
  const status = d.is_online
    ? '<span class="chip green">онлайн сейчас</span>'
    : '<span class="chip">offline</span>';
  const activity = d.is_online
    ? '<span class="green">сейчас</span>'
    : fmtRel(d.last_seen) + ' <span class="muted">('+fmtTs(d.last_seen)+')</span>';

  const channelBreakdown = pie.length ? pie.map(([cid, n]) => `
    <tr>
      <td><span class="chip blue">#${escapeHtml(cid)}</span></td>
      <td>${n}</td>
      <td style="text-align:right;">
        <button class="btn mini danger" data-clear-uch="${escapeHtml(cid)}">Удалить сообщения в этом канале</button>
      </td>
    </tr>`).join('') : '';

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
          <div class="k">Последняя активность</div><div class="v">${activity}</div>
          <div class="k">Статус</div><div class="v">${status}</div>
          <div class="k">Сообщений</div><div class="v">${d.total_messages}</div>
          <div class="k">Входов</div><div class="v">${(d.logins||[]).length}</div>
          <div class="k">Упоминаний отправил</div><div class="v">${d.mentions_sent||0}</div>
          <div class="k">Упоминаний получил</div><div class="v">${d.mentions_received||0}</div>
        </div>
        <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap;">
          <button class="btn" id="det-edit">Редактировать</button>
          <button class="btn" id="det-kick">Кикнуть</button>
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
    <div class="card">
      <h3>Управление сообщениями по каналам</h3>
      ${channelBreakdown ? `<table>
        <thead><tr><th>Канал</th><th>Сообщений</th><th></th></tr></thead>
        <tbody>${channelBreakdown}</tbody>
      </table>` : `<div class="muted">Нет сообщений</div>`}
    </div>
    <div class="card"><h3>Сообщения по дням (14 дней)</h3>${barChartSvg(msgsPerDay, 900, 130)}</div>
    <div class="card"><h3>Входы по дням (14 дней)</h3>${barChartSvg(loginsPerDay, 900, 130, '#3db83d')}</div>
    <div class="card">
      <h3>История входов</h3>
      ${(d.logins && d.logins.length) ? `<table style="table-layout:fixed;">
        <colgroup><col style="width:150px;"><col style="width:160px;"><col></colgroup>
        <thead><tr><th>Когда</th><th>IP</th><th>User-Agent</th></tr></thead>
        <tbody>${d.logins.slice(0,30).map(l => `
          <tr>
            <td class="mono muted">${fmtTs(l.ts)}</td>
            <td class="mono">${escapeHtml(l.ip||'—')}</td>
            <td class="muted mono" style="font-size:12px;">${escapeHtml(l.ua||'—')}</td>
          </tr>`).join('')}
        </tbody></table>` : `<div class="muted">Нет записей</div>`}
    </div>
    <div class="card">
      <h3>Последние сообщения</h3>
      ${(d.recent_messages && d.recent_messages.length) ? `<table style="table-layout:fixed;">
        <colgroup><col style="width:150px;"><col style="width:140px;"><col style="width:100px;"><col></colgroup>
        <thead><tr><th>Когда</th><th>Канал</th><th>ID</th><th>Текст</th></tr></thead>
        <tbody>${d.recent_messages.slice(0,20).map(m => `
          <tr>
            <td class="mono muted">${fmtTs(m.ts)}</td>
            <td><span class="chip blue">#${escapeHtml(m.channel_name)}</span></td>
            <td class="mono muted" style="font-size:11px;">${escapeHtml(m.id)}</td>
            <td>${escapeHtml(m.text)}</td>
          </tr>`).join('')}
        </tbody></table>` : `<div class="muted">Сообщений нет</div>`}
    </div>`;
  $('det-edit').addEventListener('click', () => openUserEdit(d.username, d.display_name));
  $('det-kick').addEventListener('click', () => kickUser(d.username));
  $('det-del').addEventListener('click', () => deleteUser(d.username));
  main.querySelectorAll('[data-clear-uch]').forEach(b => {
    b.addEventListener('click', async () => {
      const cid = b.dataset.clearUch;
      if (!confirm(`Удалить все сообщения @${d.username} в #${cid}?`)) return;
      try {
        const r = await api(`/api/admin/channels/${encodeURIComponent(cid)}/user/${encodeURIComponent(d.username)}`,
                            { method:'DELETE' });
        toast(`Удалено сообщений: ${r.removed}`);
        renderTab({force:true});
      } catch(e){ toast(e.message,'err'); }
    });
  });
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
    toast('Сохранено');
    $('user-edit-modal').classList.remove('visible');
    renderTab({ force: true });
  } catch(e){ $('ue-err').textContent = e.message; }
});
async function deleteUser(username){
  if (!confirm(`Удалить пользователя "${username}"?\n\nВсе его сообщения, сессии и аккаунт будут удалены.`)) return;
  try {
    const r = await api('/api/admin/users/' + encodeURIComponent(username), { method:'DELETE' });
    toast(`Удалён (сообщений: ${r.messages_removed})`);
    renderTab({ force: true });
  } catch(e){ toast(e.message,'err'); }
}

/* CHANNELS */
async function renderChannels(main){
  const data = await api('/api/admin/channels');
  const rows = data.channels.map(c => `
    <tr>
      <td class="mono">#${escapeHtml(c.id)}</td>
      <td>${escapeHtml(c.name)}</td>
      <td class="muted mono">${escapeHtml(c.owner)}</td>
      <td class="muted mono">${fmtTs(c.created)}</td>
      <td>${c.messages}</td>
      <td>${c.online ? `<span class="chip green">${c.online}</span>` : `<span class="chip">0</span>`}</td>
      <td style="white-space:nowrap;">
        <button class="btn mini" data-rename="${escapeHtml(c.id)}" data-rename-name="${escapeHtml(c.name)}">Переименовать</button>
        <button class="btn mini" data-kickall="${escapeHtml(c.id)}">Кикнуть всех</button>
        <button class="btn mini" data-clear="${escapeHtml(c.id)}">Очистить</button>
        <button class="btn mini danger" data-del="${escapeHtml(c.id)}">Удалить</button>
      </td>
    </tr>`).join('');
  main.innerHTML = `
    <h2 class="page-title">Каналы</h2>
    <div class="page-sub">Изменения сразу видны клиентам.</div>
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
  main.querySelectorAll('[data-kickall]').forEach(b => b.addEventListener('click', ()=>kickAll(b.dataset.kickall)));
  main.querySelectorAll('[data-rename]').forEach(b =>
    b.addEventListener('click', ()=>openChanRename(b.dataset.rename, b.dataset.renameName)));
  $('new-ch-btn').addEventListener('click', createChannel);
  $('new-ch').addEventListener('keydown', e => { if (e.key==='Enter') createChannel(); });
}
async function createChannel(){
  const name = $('new-ch').value.trim();
  if (!name) return;
  try { await api('/api/admin/channels', { method:'POST', body:{name} }); toast('Канал создан'); renderTab({force:true}); }
  catch(e){ toast(e.message,'err'); }
}
async function deleteChannel(cid){
  if (!confirm(`Удалить канал #${cid}?`)) return;
  try { await api('/api/admin/channels/'+encodeURIComponent(cid), { method:'DELETE' }); toast('Удалён'); renderTab({force:true}); }
  catch(e){ toast(e.message,'err'); }
}
async function clearChannel(cid){
  if (!confirm(`Очистить #${cid}?`)) return;
  try { await api('/api/admin/channels/'+encodeURIComponent(cid)+'/messages', { method:'DELETE' }); toast('Очищено'); renderTab({force:true}); }
  catch(e){ toast(e.message,'err'); }
}
async function kickAll(cid){
  if (!confirm(`Кикнуть всех из #${cid}?`)) return;
  try {
    const r = await api('/api/admin/channels/'+encodeURIComponent(cid)+'/kickall', { method:'POST' });
    toast(`Кикнуто: ${r.kicked}`);
    renderTab({force:true});
  } catch(e){ toast(e.message,'err'); }
}
function openChanRename(cid, name){
  state.currentChan = cid;
  $('ce-err').textContent = '';
  $('ce-name').value = name || '';
  $('chan-edit-modal').classList.add('visible');
  setTimeout(()=>$('ce-name').focus(), 30);
}
$('ce-cancel').addEventListener('click', ()=>$('chan-edit-modal').classList.remove('visible'));
$('ce-save').addEventListener('click', async () => {
  const name = $('ce-name').value.trim();
  if (!name) { $('ce-err').textContent = 'Введите название'; return; }
  try {
    await api('/api/admin/channels/'+encodeURIComponent(state.currentChan),
              { method:'PATCH', body:{name} });
    toast('Переименовано');
    $('chan-edit-modal').classList.remove('visible');
    renderTab({force:true});
  } catch(e){ $('ce-err').textContent = e.message; }
});

/* MESSAGES */
async function renderMessages(main){
  const channelsData = await api('/api/admin/channels');
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
      wrap.innerHTML = data.messages.length ? `<table style="table-layout:fixed;">
        <colgroup><col style="width:140px;"><col style="width:120px;"><col style="width:130px;"><col><col style="width:170px;"></colgroup>
        <thead><tr><th>Время</th><th>Канал</th><th>Автор</th><th>Текст</th><th></th></tr></thead>
        <tbody>${data.messages.map(m => `<tr>
          <td class="mono muted">${fmtTs(m.ts)}</td>
          <td><span class="chip blue">#${escapeHtml(m.channel_name||m.channel)}</span></td>
          <td class="mono accent">${escapeHtml(m.user)}</td>
          <td>${escapeHtml(m.text)}${m.edited?' <span class="muted">(изм.)</span>':''}</td>
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
    toast('Обновлено');
    $('msg-edit-modal').classList.remove('visible');
    renderTab({force:true});
  } catch(e){ $('me-err').textContent = e.message; }
});
async function deleteMessage(id){
  if (!confirm('Удалить сообщение?')) return;
  try { await api('/api/admin/messages/'+encodeURIComponent(id), { method:'DELETE' }); toast('Удалено'); renderTab({force:true}); }
  catch(e){ toast(e.message,'err'); }
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
})();
</script>
</body>
</html>
"""

ADMIN_PAGE = ADMIN_PAGE.replace("{{APP_NAME}}", APP_NAME)


# ============================================================
#  РОУТЫ
# ============================================================
@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    if not ADMIN_PASS:
        return HTMLResponse(
            "<h1 style='font-family:sans-serif;color:#c33;padding:40px'>"
            f"Админка отключена: не задан ADMIN_PASS</h1>",
            status_code=503)
    return ADMIN_PAGE


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": _now()}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
