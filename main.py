"""
sldchat — веб-чат (IRC/TeamSpeak-like) на FastAPI.
Всё состояние в оперативной памяти. Реалтайм — через WebSocket.
Админка — /admin (пароль из ADMIN_PASS), стиль Cockpit/CentOS.

ENV:
  ADMIN_PASS   пароль для /admin. Если не задан — админка отключена.
  PORT         порт (по умолчанию 8080)
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from typing import Optional, Dict, List

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect, HTTPException,
    Header, Depends, Cookie, Response, Request,
)
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, field_validator


# ============================================================
#  КОНФИГ
# ============================================================
ALLOW_CHANNEL_CREATION = False
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,24}$")
MENTION_RE  = re.compile(r"(?<![A-Za-z0-9_\-])@([A-Za-z0-9_\-]{3,24})")


# ============================================================
#  ХРАНИЛИЩЕ В ПАМЯТИ
# ============================================================
users:    Dict[str, dict] = {}
tokens:   Dict[str, str]  = {}
channels: Dict[str, dict] = {}
messages: Dict[str, List[dict]] = {}
connections: Dict[WebSocket, dict] = {}
presence:    Dict[str, Dict[str, float]] = {}
admin_sessions: set[str] = set()

_STARTED_AT = time.time()


def _seed_channels() -> None:
    for cid, name in (("general", "General"), ("random", "Random")):
        channels[cid] = {"id": cid, "name": name, "owner": "system", "created": time.time()}
        messages[cid] = []


_seed_channels()


# ============================================================
#  УТИЛИТЫ
# ============================================================
def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def require_user(x_auth_token: Optional[str] = Header(None)) -> str:
    username = tokens.get(x_auth_token) if x_auth_token else None
    if not username:
        raise HTTPException(401, "Unauthorized")
    return username


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


def _now() -> float:
    return time.time()


def _day_str(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


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

    @field_validator("display_name")
    @classmethod
    def v_display(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > 32:
            raise ValueError("Имя: до 32 символов")
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
    def v_display(cls, v: str) -> str:
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
    def v_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not (1 <= len(v) <= 4000):
            raise ValueError("Текст: 1–4000 символов")
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
    key = body.username.strip().lower()
    u = users.get(key)
    if not u or u["hash"] != hash_pw(body.password, u["salt"]):
        raise HTTPException(401, "Неверный юзернейм или пароль")
    _record_login(u["username"], request)
    token = secrets.token_urlsafe(24)
    tokens[token] = u["username"]
    return {"token": token, **public_user(u["username"])}


@app.get("/api/me")
def me(user: str = Depends(require_user)):
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
    await broadcast_all({
        "type": "profile_updated",
        "username": u["username"],
        "display_name": body.display_name,
    })
    return public_user(u["username"])


# ---------- PRESENCE ----------
def _presence_counts() -> Dict[str, int]:
    return {cid: len(u) for cid, u in presence.items() if u}


def _total_online() -> int:
    seen = set()
    for bucket in presence.values():
        seen.update(bucket.keys())
    return len(seen)


async def broadcast_all(payload: dict) -> None:
    dead = []
    for ws in list(connections.keys()):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connections.pop(ws, None)


async def broadcast_presence() -> None:
    await broadcast_all({
        "type": "presence",
        "counts": _presence_counts(),
        "total": _total_online(),
    })


# ---------- CHANNELS ----------
@app.get("/api/channels")
def list_channels(user: str = Depends(require_user)):
    counts = _presence_counts()
    out = []
    for cid, c in channels.items():
        lst = messages.get(cid) or []
        last = lst[-1] if lst else None
        out.append({
            "id": c["id"],
            "name": c["name"],
            "owner": c["owner"],
            "online": counts.get(cid, 0),
            "last_message": (
                {"user": last["user"], "text": last["text"], "ts": last["ts"]}
                if last else None
            ),
        })
    out.sort(key=lambda x: (x["last_message"]["ts"] if x["last_message"] else 0), reverse=True)
    return {"channels": out, "total_online": _total_online()}


@app.get("/api/channels/{cid}/members")
def channel_members(cid: str, user: str = Depends(require_user)):
    if cid not in channels:
        raise HTTPException(404, "Канал не найден")
    bucket = presence.get(cid) or {}
    members = [public_user(u) for u in sorted(bucket.keys())]
    return {"members": members}


def _slugify_channel(raw: str) -> str:
    return "".join(c for c in raw.lower().replace(" ", "-") if c.isalnum() or c in "-_")


@app.post("/api/channels")
async def create_channel(body: ChannelBody, user: str = Depends(require_user)):
    if not ALLOW_CHANNEL_CREATION:
        raise HTTPException(403, "Создание каналов отключено")
    raw = body.name.strip().lstrip("#").strip()
    if not (1 <= len(raw) <= 32):
        raise HTTPException(400, "Название: 1–32 символа")
    cid = _slugify_channel(raw)
    if not cid:
        raise HTTPException(400, "Некорректное название")
    if cid in channels:
        raise HTTPException(409, "Канал уже существует")
    channels[cid] = {"id": cid, "name": raw, "owner": user, "created": _now()}
    messages[cid] = []
    await broadcast_all({"type": "channels_changed"})
    return {"id": cid, "name": raw}


@app.get("/api/channels/{cid}/messages")
def channel_messages(cid: str, user: str = Depends(require_user), limit: int = 200):
    if cid not in channels:
        raise HTTPException(404, "Канал не найден")
    msgs = messages.get(cid, [])[-limit:]
    mentioned = set()
    for m in msgs:
        for nick in MENTION_RE.findall(m["text"]):
            mentioned.add(nick.lower())
    profiles = {k: public_user(k) for k in mentioned if k in users}
    return {"messages": msgs, "profiles": profiles}


# ---------- WEBSOCKET ----------
async def handle_message(cid: str, username: str, text: str) -> None:
    text = (text or "").strip()
    if not text or len(text) > 4000:
        return
    msg = {
        "id": secrets.token_hex(8),
        "channel": cid,
        "user": username,
        "text": text,
        "ts": _now(),
    }
    lst = messages.setdefault(cid, [])
    lst.append(msg)
    if len(lst) > 2000:
        del lst[:-2000]
    users.get(username.lower(), {})["last_seen"] = _now()
    await broadcast_all({"type": "message", "message": msg})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token")
    cid = ws.query_params.get("channel")
    username = tokens.get(token) if token else None
    if not username or not cid or cid not in channels:
        await ws.close(code=1008)
        return
    connections[ws] = {"username": username, "channel": cid}
    presence.setdefault(cid, {})[username] = _now()
    await broadcast_presence()
    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type")
            if t == "message":
                await handle_message(cid, username, data.get("text"))
            elif t == "ping":
                try:
                    await ws.send_json({"type": "pong"})
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        connections.pop(ws, None)
        bucket = presence.get(cid) or {}
        bucket.pop(username, None)
        if not bucket:
            presence.pop(cid, None)
        try:
            await broadcast_presence()
        except Exception:
            pass


# ============================================================
#  ADMIN API
# ============================================================
@app.post("/api/admin/login")
def admin_login(body: AdminLoginBody, response: Response):
    if not ADMIN_PASS:
        raise HTTPException(403, "Админка отключена")
    if not secrets.compare_digest(body.password, ADMIN_PASS):
        raise HTTPException(401, "Неверный пароль")
    token = secrets.token_urlsafe(32)
    admin_sessions.add(token)
    response.set_cookie("sld_admin", token, httponly=True, samesite="lax", max_age=60 * 60 * 12)
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
    total_msgs = sum(len(v) for v in messages.values())
    return {
        "users": len(users),
        "channels": len(channels),
        "messages": total_msgs,
        "online": _total_online(),
        "tokens": len(tokens),
        "uptime_started": _STARTED_AT,
    }


# ---- USERS ----
@app.get("/api/admin/users")
def admin_users(_: None = Depends(require_admin)):
    out = []
    for u in users.values():
        uname = u["username"]
        msg_count = sum(1 for lst in messages.values() for m in lst if m["user"] == uname)
        token_count = sum(1 for n in tokens.values() if n == uname)
        online_in = [cid for cid, us in presence.items() if uname in us]
        out.append({
            "username": uname,
            "display_name": u.get("display_name") or uname,
            "created": u["created"],
            "last_seen": u.get("last_seen", 0),
            "logins_count": len(u.get("logins", [])),
            "tokens": token_count,
            "messages": msg_count,
            "online_in": online_in,
        })
    out.sort(key=lambda x: x["created"], reverse=True)
    return {"users": out}


@app.get("/api/admin/users/{username}")
def admin_user_detail(username: str, _: None = Depends(require_admin)):
    key = username.lower()
    u = users.get(key)
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
            recent.append({
                **m,
                "channel_name": channels.get(cid, {}).get("name", cid),
            })
    recent.sort(key=lambda m: m["ts"], reverse=True)

    logins = u.get("logins", [])
    logins_per_day: Dict[str, int] = {}
    for l in logins:
        d = _day_str(l["ts"])
        logins_per_day[d] = logins_per_day.get(d, 0) + 1

    # Заполняем последние 14 дней нулями для графиков
    days: List[str] = []
    now = time.time()
    for i in range(13, -1, -1):
        days.append(_day_str(now - i * 86400))

    return {
        "username": uname,
        "display_name": u.get("display_name") or uname,
        "created": u["created"],
        "last_seen": u.get("last_seen", 0),
        "total_messages": total,
        "messages_by_channel": by_channel,
        "messages_per_day": per_day,
        "logins_per_day": logins_per_day,
        "days": days,
        "logins": list(reversed(logins[-50:])),
        "recent_messages": recent[:25],
        "online_in": [cid for cid, us in presence.items() if uname in us],
    }


@app.patch("/api/admin/users/{username}")
async def admin_edit_user(username: str, body: AdminUserEdit, _: None = Depends(require_admin)):
    key = username.lower()
    u = users.get(key)
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
        # отзываем все сессии этого пользователя
        for t in [t for t, n in tokens.items() if n == u["username"]]:
            del tokens[t]

    await broadcast_all({
        "type": "profile_updated",
        "username": u["username"],
        "display_name": u["display_name"],
    })
    return public_user(u["username"])


@app.delete("/api/admin/users/{username}")
async def admin_delete_user(username: str, _: None = Depends(require_admin)):
    key = username.lower()
    u = users.get(key)
    if not u:
        raise HTTPException(404, "Нет такого пользователя")
    uname = u["username"]
    kicked = 0
    for ws, info in list(connections.items()):
        if info["username"] == uname:
            connections.pop(ws, None)
            try:
                await ws.close(code=1008)
            except Exception:
                pass
            kicked += 1
    del users[key]
    for t in [t for t, n in tokens.items() if n == uname]:
        del tokens[t]
    for cid in list(presence.keys()):
        presence[cid].pop(uname, None)
        if not presence[cid]:
            presence.pop(cid, None)
    await broadcast_presence()
    return {"ok": True, "kicked": kicked}


# ---- CHANNELS ----
@app.get("/api/admin/channels")
def admin_channels(_: None = Depends(require_admin)):
    out = []
    for cid, c in channels.items():
        out.append({
            "id": c["id"],
            "name": c["name"],
            "owner": c["owner"],
            "created": c["created"],
            "messages": len(messages.get(cid) or []),
            "online": len(presence.get(cid) or {}),
        })
    out.sort(key=lambda x: x["created"])
    return {"channels": out}


@app.post("/api/admin/channels")
async def admin_create_channel(body: ChannelBody, _: None = Depends(require_admin)):
    raw = body.name.strip().lstrip("#").strip()
    if not (1 <= len(raw) <= 32):
        raise HTTPException(400, "Название: 1–32 символа")
    cid = _slugify_channel(raw)
    if not cid:
        raise HTTPException(400, "Некорректное название")
    if cid in channels:
        raise HTTPException(409, "Канал уже существует")
    channels[cid] = {"id": cid, "name": raw, "owner": "admin", "created": _now()}
    messages[cid] = []
    await broadcast_all({"type": "channels_changed"})
    return {"id": cid, "name": raw}


@app.delete("/api/admin/channels/{cid}")
async def admin_delete_channel(cid: str, _: None = Depends(require_admin)):
    if cid not in channels:
        raise HTTPException(404, "Нет такого канала")
    for ws, info in list(connections.items()):
        if info["channel"] == cid:
            connections.pop(ws, None)
            try:
                await ws.close(code=1008)
            except Exception:
                pass
    del channels[cid]
    messages.pop(cid, None)
    presence.pop(cid, None)
    await broadcast_all({"type": "channels_changed"})
    await broadcast_presence()
    return {"ok": True}


# ---- MESSAGES ----
@app.get("/api/admin/messages")
def admin_messages(
    _: None = Depends(require_admin),
    channel: Optional[str] = None,
    user: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
):
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
                await broadcast_all({
                    "type": "message_edited",
                    "id": mid, "channel": cid, "text": body.text,
                })
                return lst[i]
    raise HTTPException(404, "Сообщение не найдено")


@app.delete("/api/admin/messages/{mid}")
async def admin_delete_message(mid: str, _: None = Depends(require_admin)):
    for cid, lst in messages.items():
        for i, m in enumerate(lst):
            if m["id"] == mid:
                lst.pop(i)
                await broadcast_all({"type": "message_deleted", "id": mid, "channel": cid})
                return {"ok": True}
    raise HTTPException(404, "Сообщение не найдено")


@app.delete("/api/admin/channels/{cid}/messages")
async def admin_clear_channel(cid: str, _: None = Depends(require_admin)):
    if cid not in messages:
        raise HTTPException(404, "Нет такого канала")
    messages[cid] = []
    await broadcast_all({"type": "channel_cleared", "channel": cid})
    return {"ok": True}


# ============================================================
#  КЛИЕНТ ЧАТА
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#517da2">
<title>sldchat</title>
<style>
:root {
  --bg:#e6ebee; --panel:#fff; --panel-2:#f2f5f8; --border:#dfe5ea;
  --text:#222; --muted:#9aa5ad; --accent:#517da2; --accent-hover:#46708f;
  --bubble-in:#fff; --bubble-out:#eeffde; --bubble-out-time:#7d8b7d;
  --chat-bg:#e6ebee; --shadow:rgba(0,0,0,.08);
}
html[data-theme="dark"] {
  --bg:#0f1720; --panel:#17212b; --panel-2:#202b36; --border:#24313d;
  --text:#e7edf3; --muted:#8fa1b3; --accent:#4f87b8; --accent-hover:#5c96c8;
  --bubble-in:#1f2c38; --bubble-out:#2b5278; --bubble-out-time:#b9d0e6;
  --chat-bg:#0d141b; --shadow:rgba(0,0,0,.35);
}
* { box-sizing:border-box; margin:0; padding:0;
    -webkit-tap-highlight-color:transparent;
    -webkit-user-select:none; user-select:none;
    -webkit-touch-callout:none; }
input, textarea { -webkit-user-select:text; user-select:text; }
html, body { height:100vh; height:100dvh; overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:14px; color:var(--text); background:var(--bg); }
button { font-family:inherit; cursor:pointer; border:none; background:none; color:inherit; }
input, textarea { font-family:inherit; }
svg { display:block; }

#boot { position:fixed; inset:0; z-index:200; display:flex; flex-direction:column;
  align-items:center; justify-content:center; color:#fff;
  background:linear-gradient(140deg,#5c88ae 0%,#3c6591 55%,#2e5379 100%); }
#boot .logo { font-size:34px; font-weight:300; letter-spacing:4px; }
.spinner { margin-top:26px; width:40px; height:40px;
  border:3px solid rgba(255,255,255,.25); border-top-color:#fff; border-radius:50%;
  animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }

#auth-screen { position:fixed; inset:0; z-index:100; display:none;
  align-items:center; justify-content:center; padding:20px; overflow-y:auto;
  background:linear-gradient(140deg,#5c88ae 0%,#3c6591 55%,#2e5379 100%); }
#auth-screen.visible { display:flex; }
.auth-card { width:380px; max-width:100%; background:var(--panel);
  border-radius:14px; overflow:hidden; box-shadow:0 24px 70px rgba(0,0,0,.4); color:var(--text); }
.auth-head { padding:26px 26px 4px; text-align:center; }
.auth-logo { font-size:32px; font-weight:300; letter-spacing:3px; color:var(--accent); }
.auth-sub { font-size:12px; color:var(--muted); margin-top:4px; }
.auth-tabs { display:flex; padding:0 22px; margin-top:18px; border-bottom:1px solid var(--border); }
.auth-tab { flex:1; padding:12px 0; font-size:13.5px; font-weight:600; color:var(--muted);
  border-bottom:2px solid transparent; transition:color .15s, border-color .15s; }
.auth-tab:hover { color:var(--accent); }
.auth-tab.active { color:var(--accent); border-bottom-color:var(--accent); }
.auth-body { padding:18px 24px 24px; }
.auth-body input { width:100%; padding:12px 14px; margin-bottom:12px;
  border:1px solid var(--border); border-radius:8px; font-size:14px; outline:none;
  background:var(--panel-2); color:var(--text); }
.auth-body input:focus { border-color:var(--accent); background:var(--panel);
  box-shadow:0 0 0 3px rgba(81,125,162,.14); }
.auth-error { color:#d64541; font-size:12px; min-height:16px; margin-bottom:6px; }
.auth-submit { width:100%; margin-top:6px; padding:12px; background:var(--accent);
  color:#fff; border-radius:8px; font-size:14px; font-weight:600; }
.auth-submit:hover { background:var(--accent-hover); }
.auth-submit:disabled { opacity:.6; cursor:default; }
.auth-hint { font-size:11px; color:var(--muted); margin-top:8px; line-height:1.5; }

#app { display:none; height:100vh; height:100dvh; }
#app.visible { display:flex; }

.sidebar { width:290px; flex-shrink:0; background:var(--panel);
  border-right:1px solid var(--border); display:flex; flex-direction:column; }
.sidebar-header { height:56px; flex-shrink:0; background:var(--accent); color:#fff;
  display:flex; align-items:center; justify-content:space-between; padding:0 10px 0 14px; }
.me { display:flex; align-items:center; gap:10px; min-width:0; cursor:pointer;
  padding:4px 6px; border-radius:6px; }
.me:hover { background:rgba(255,255,255,.1); }
#me-name { font-weight:600; font-size:14px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#me-username { font-size:11px; opacity:.75; }
.icon-btn { width:36px; height:36px; border-radius:8px;
  display:flex; align-items:center; justify-content:center;
  color:#fff; opacity:.85; flex-shrink:0; }
.icon-btn:hover { background:rgba(255,255,255,.14); opacity:1; }

.search-wrap { position:relative; padding:10px 12px; border-bottom:1px solid var(--border); flex-shrink:0; }
.search-icon { position:absolute; left:22px; top:50%; transform:translateY(-50%);
  color:var(--muted); pointer-events:none; }
.search-wrap input { width:100%; padding:9px 12px 9px 36px;
  background:var(--panel-2); border:1px solid transparent; border-radius:8px;
  font-size:13px; outline:none; color:var(--text); }
.search-wrap input:focus { background:var(--panel); border-color:var(--border); }

.channel-list { flex:1; overflow-y:auto; padding:6px 0; }
.channel-item { display:flex; align-items:center; gap:10px; padding:9px 14px;
  cursor:pointer; transition:background .12s; }
.channel-item:hover { background:var(--panel-2); }
.channel-item.active { background:var(--accent); color:#fff; }
.channel-item.active .channel-meta,
.channel-item.active .channel-last { color:rgba(255,255,255,.8); }
.channel-hash { width:30px; height:30px; flex-shrink:0; border-radius:50%;
  background:var(--panel-2); color:var(--accent);
  display:flex; align-items:center; justify-content:center; font-weight:600; font-size:14px; }
.channel-item.active .channel-hash { background:rgba(255,255,255,.2); color:#fff; }
.channel-body { flex:1; min-width:0; }
.channel-row1 { display:flex; align-items:baseline; justify-content:space-between; gap:8px; }
.channel-name { font-weight:600; font-size:13.5px; color:inherit;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.channel-meta { font-size:11px; color:var(--muted); flex-shrink:0; }
.channel-last { font-size:12px; color:var(--muted); margin-top:2px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

.new-channel { display:flex; gap:8px; padding:10px 12px; border-top:1px solid var(--border);
  background:var(--panel); flex-shrink:0; }
.new-channel input { flex:1; min-width:0; padding:9px 12px;
  border:1px solid var(--border); border-radius:8px; font-size:13px;
  outline:none; background:var(--panel-2); color:var(--text); }
.new-channel input:focus { border-color:var(--accent); }
.new-channel button { width:38px; height:38px; border-radius:8px; background:var(--accent);
  color:#fff; flex-shrink:0; display:flex; align-items:center; justify-content:center; }
.new-channel button:hover { background:var(--accent-hover); }

.chat { flex:1; min-width:0; display:flex; flex-direction:column; }
.chat-header { height:56px; flex-shrink:0; background:var(--accent); color:#fff;
  display:flex; align-items:center; padding:0 10px 0 14px; gap:8px; }
#back-btn { display:none; width:36px; height:36px;
  align-items:center; justify-content:center; border-radius:8px; color:#fff; }
#back-btn:hover { background:rgba(255,255,255,.14); }
.chat-title { display:flex; flex-direction:column; min-width:0; flex:1; }
#chat-name { font-weight:600; font-size:14px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.chat-users { font-size:11px; opacity:.85; }

.messages-wrap { flex:1; min-height:0; display:flex; }
.messages { flex:1; min-height:0; overflow-y:auto; padding:14px 18px 8px; background:var(--chat-bg); }
.empty { text-align:center; color:var(--muted); margin-top:60px; font-size:13px; line-height:1.6; }

.msg { display:flex; margin-bottom:6px; flex-direction:column; }
.msg.in { align-items:flex-start; }
.msg.out { align-items:flex-end; }
.msg.same-user { margin-top:-2px; }
.msg.same-user .name { display:none; }
.bubble { max-width:74%; padding:6px 11px 5px; border-radius:10px;
  background:var(--bubble-in); box-shadow:0 1px 2px var(--shadow);
  word-wrap:break-word; overflow-wrap:break-word; color:var(--text); }
.msg.in .bubble { border-top-left-radius:3px; }
.msg.out .bubble { background:var(--bubble-out); border-top-right-radius:3px; }
.msg.same-user.in .bubble, .msg.same-user.out .bubble {
  border-top-left-radius:10px; border-top-right-radius:10px; }
.name { font-size:12.5px; font-weight:600; color:var(--accent); margin-bottom:2px; cursor:pointer; }
.name:hover { text-decoration:underline; }
.text { white-space:pre-wrap; line-height:1.35; font-size:14px; }
.text .mention { color:var(--accent); font-weight:600; }
.text .mention.self { background:rgba(81,125,162,.25); padding:0 3px; border-radius:4px; }
.text .edited { color:var(--muted); font-size:11px; margin-left:4px; }
.time { font-size:10.5px; color:var(--muted); text-align:right; margin-top:2px; margin-left:12px; }
.msg.out .time { color:var(--bubble-out-time); }

/* -------- MEMBERS -------- */
.members-panel {
  width:210px; flex-shrink:0; background:var(--panel);
  border-left:1px solid var(--border);
  display:none; flex-direction:column; overflow:hidden;
}
.members-panel.visible { display:flex; }
.members-head { padding:12px 14px; border-bottom:1px solid var(--border);
  font-size:11px; text-transform:uppercase; letter-spacing:1.4px; color:var(--muted);
  display:flex; justify-content:space-between; align-items:center; }
.members-head b { color:var(--text); font-weight:600; }
.members-list { flex:1; overflow-y:auto; padding:6px 0; }
.member-item { padding:8px 14px; font-size:13px; cursor:default;
  display:flex; align-items:center; gap:8px; }
.member-item:hover { background:var(--panel-2); }
.member-item .dot { width:7px; height:7px; border-radius:50%; background:#4caf50; flex-shrink:0; }
.member-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.member-name.self { color:var(--accent); font-weight:600; }

.composer { display:flex; align-items:flex-end; gap:10px;
  padding:12px 16px calc(12px + env(safe-area-inset-bottom, 0));
  background:var(--panel); border-top:1px solid var(--border); flex-shrink:0; position:relative; }
#msg-input { flex:1; min-width:0; padding:11px 14px;
  background:var(--panel-2); border:1px solid transparent; border-radius:22px;
  font-size:14px; line-height:1.4; max-height:130px; min-height:44px;
  resize:none; outline:none; color:var(--text); }
#msg-input:focus { background:var(--panel); border-color:var(--border); }
.send-btn { width:44px; height:44px; flex-shrink:0; border-radius:50%;
  background:var(--accent); color:#fff;
  display:flex; align-items:center; justify-content:center; }
.send-btn:hover { background:var(--accent-hover); }

.mention-menu { position:absolute; left:12px; right:12px; bottom:calc(100% + 6px);
  background:var(--panel); border:1px solid var(--border); border-radius:8px;
  box-shadow:0 8px 24px var(--shadow); max-height:220px; overflow-y:auto; z-index:20; display:none; }
.mention-menu.visible { display:block; }
.mention-item { padding:8px 12px; cursor:pointer; font-size:13px; }
.mention-item:hover { background:var(--panel-2); }
.mention-item .u { color:var(--muted); margin-left:6px; }

/* -------- MODAL -------- */
.modal-backdrop { position:fixed; inset:0; z-index:300;
  background:rgba(0,0,0,.45); display:none;
  align-items:center; justify-content:center; padding:20px; }
.modal-backdrop.visible { display:flex; }
.modal { background:var(--panel); border-radius:12px; padding:22px 24px;
  width:360px; max-width:100%; color:var(--text);
  box-shadow:0 24px 60px rgba(0,0,0,.4); }
.modal h3 { font-size:16px; margin-bottom:16px; font-weight:600; }
.modal label { display:block; font-size:12px; color:var(--muted); margin-bottom:6px; }
.modal input { width:100%; padding:10px 12px; border:1px solid var(--border); border-radius:8px;
  font-size:14px; outline:none; background:var(--panel-2); color:var(--text); margin-bottom:14px; }
.modal input:focus { border-color:var(--accent); background:var(--panel); }
.modal .row { display:flex; justify-content:flex-end; gap:8px; }
.modal .btn2 { padding:9px 16px; border-radius:8px; font-size:13px; font-weight:500;
  background:var(--panel-2); color:var(--text); }
.modal .btn2:hover { background:var(--border); }
.modal .btn2.primary { background:var(--accent); color:#fff; }
.modal .btn2.primary:hover { background:var(--accent-hover); }
.modal-err { color:#d64541; font-size:12px; min-height:16px; margin-bottom:8px; }

@media (max-width:800px) {
  #app.visible { display:block; position:relative; overflow:hidden; }
  .sidebar { position:absolute; inset:0; width:100%; border-right:none; }
  .chat { position:absolute; inset:0; background:var(--chat-bg);
    transform:translateX(100%); transition:transform .24s ease; z-index:5; }
  #app.chat-open .chat { transform:translateX(0); }
  #back-btn { display:flex; }
  .bubble { max-width:82%; }
  .members-panel { position:absolute; top:56px; right:0; bottom:0;
    width:220px; z-index:8; box-shadow:-8px 0 24px var(--shadow); }
}
.channel-list::-webkit-scrollbar,
.messages::-webkit-scrollbar,
.members-list::-webkit-scrollbar { width:8px; height:8px; }
.channel-list::-webkit-scrollbar-thumb,
.messages::-webkit-scrollbar-thumb,
.members-list::-webkit-scrollbar-thumb { background:rgba(0,0,0,.12); border-radius:4px; }
</style>
</head>
<body>

<div id="boot">
  <div class="logo">sldchat</div>
  <div class="spinner"></div>
</div>

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
      <input id="auth-user" type="text" placeholder="Юзернейм (латиница)"
             autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="24">
      <input id="auth-pass" type="password" placeholder="Пароль"
             autocomplete="current-password" maxlength="128">
      <input id="reg-pass2" type="password" placeholder="Повтор пароля"
             autocomplete="new-password" maxlength="128" style="display:none;">
      <button type="button" class="auth-submit" id="auth-submit">Войти</button>
      <div class="auth-hint" id="reg-hint" style="display:none;">
        Юзернейм: 3–24, только a-z A-Z 0-9 _ -. Он используется для упоминаний @username.
      </div>
    </div>
  </div>
</div>

<div id="app">
  <aside class="sidebar">
    <div class="sidebar-header">
      <div class="me" id="me-block" title="Редактировать профиль">
        <div style="min-width:0;">
          <div id="me-name">…</div>
          <div id="me-username"></div>
        </div>
      </div>
      <div style="display:flex;gap:2px;">
        <button class="icon-btn" id="theme-btn" title="Тема" aria-label="Тема">
          <svg id="theme-icon" viewBox="0 0 24 24" width="20" height="20"></svg>
        </button>
        <button class="icon-btn" id="logout-btn" title="Выйти" aria-label="Выйти">
          <svg viewBox="0 0 24 24" width="20" height="20">
            <path fill="currentColor" d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/>
          </svg>
        </button>
      </div>
    </div>
    <div class="search-wrap">
      <svg class="search-icon" viewBox="0 0 24 24" width="16" height="16">
        <path fill="currentColor" d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/>
      </svg>
      <input id="search" placeholder="Поиск каналов">
    </div>
    <div class="channel-list" id="channel-list"></div>
    <div class="new-channel" id="new-channel-wrap">
      <input id="new-channel-name" placeholder="Новый канал" maxlength="32">
      <button id="new-channel-btn" title="Создать" aria-label="Создать канал">
        <svg viewBox="0 0 24 24" width="20" height="20">
          <path fill="currentColor" d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/>
        </svg>
      </button>
    </div>
  </aside>

  <main class="chat" id="chat">
    <div class="chat-header">
      <button class="icon-btn" id="back-btn" aria-label="Назад">
        <svg viewBox="0 0 24 24" width="24" height="24">
          <path fill="currentColor" d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>
        </svg>
      </button>
      <div class="chat-title">
        <span id="chat-name">Выбери канал</span>
        <span class="chat-users" id="chat-users"></span>
      </div>
      <button class="icon-btn" id="members-btn" title="Участники" aria-label="Участники">
        <svg viewBox="0 0 24 24" width="20" height="20">
          <path fill="currentColor" d="M16 11c1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3 1.34 3 3 3zm-8 0c1.66 0 3-1.34 3-3S9.66 5 8 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/>
        </svg>
      </button>
    </div>
    <div class="messages-wrap">
      <div class="messages" id="messages">
        <div class="empty">Выбери канал слева,<br>чтобы начать общение</div>
      </div>
      <aside class="members-panel" id="members-panel">
        <div class="members-head">
          <span>Участники <b id="members-count">0</b></span>
        </div>
        <div class="members-list" id="members-list"></div>
      </aside>
    </div>
    <div class="composer">
      <div class="mention-menu" id="mention-menu"></div>
      <textarea id="msg-input" placeholder="Написать сообщение..." rows="1" enterkeyhint="send"></textarea>
      <button class="send-btn" id="send-btn" aria-label="Отправить">
        <svg viewBox="0 0 24 24" width="22" height="22">
          <path fill="currentColor" d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
        </svg>
      </button>
    </div>
  </main>
</div>

<!-- PROFILE MODAL -->
<div class="modal-backdrop" id="profile-modal">
  <div class="modal">
    <h3>Редактировать профиль</h3>
    <div class="modal-err" id="profile-err"></div>
    <label>Отображаемое имя</label>
    <input id="profile-display" type="text" maxlength="32" placeholder="Как вас показывать">
    <div class="row">
      <button class="btn2" id="profile-cancel">Отмена</button>
      <button class="btn2 primary" id="profile-save">Сохранить</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const LS_THEME = 'sld_theme', LS_TOKEN = 'sld_token';
const state = {
  token: localStorage.getItem(LS_TOKEN) || null,
  username: null, display_name: null,
  channels: [], currentChannel: null,
  ws: null, reconnectTimer: null, pingTimer: null,
  profiles: {}, allowChannelCreation: true, totalOnline: 0,
};
let authMode = 'login';

document.addEventListener('contextmenu', e => e.preventDefault());

/* THEME */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const icon = $('theme-icon');
  if (theme === 'dark') icon.innerHTML = '<path fill="currentColor" d="M6.76 4.84l-1.8-1.79-1.41 1.41 1.79 1.79 1.42-1.41zM4 10.5H1v2h3v-2zm9-9.95h-2V3.5h2V.55zm7.45 3.91l-1.41-1.41-1.79 1.79 1.41 1.41 1.79-1.79zm-3.21 13.7l1.79 1.8 1.41-1.41-1.8-1.79-1.4 1.4zM20 10.5v2h3v-2h-3zm-8-5c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6zm-1 16.95h2V19.5h-2v2.95zm-7.45-3.91l1.41 1.41 1.79-1.8-1.41-1.41-1.79 1.8z"/>';
  else icon.innerHTML = '<path fill="currentColor" d="M20 8.69V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12 20 8.69zM12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6 6 2.69 6 6-2.69 6-6 6z"/>';
}
applyTheme(localStorage.getItem(LS_THEME) || 'light');
$('theme-btn').addEventListener('click', () => {
  const next = (document.documentElement.getAttribute('data-theme') === 'dark') ? 'light' : 'dark';
  applyTheme(next); localStorage.setItem(LS_THEME, next);
});

/* API */
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers['X-Auth-Token'] = state.token;
  if (opts.body && typeof opts.body !== 'string') {
    headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    const err = new Error(detail); err.status = r.status; throw err;
  }
  return r.json();
}

/* UTILS */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
function hhmm(ts) {
  const t = new Date(ts * 1000); if (isNaN(t)) return '';
  return String(t.getHours()).padStart(2,'0') + ':' + String(t.getMinutes()).padStart(2,'0');
}
function renderTextWithMentions(text, selfUsername) {
  const safe = escapeHtml(text);
  return safe.replace(/(^|[^A-Za-z0-9_\-])@([A-Za-z0-9_\-]{3,24})/g,
    (m, p1, nick) => {
      const self = selfUsername && nick.toLowerCase() === selfUsername.toLowerCase();
      return `${p1}<span class="mention${self ? ' self' : ''}">@${nick}</span>`;
    });
}

/* AUTH */
function setAuthMode(mode) {
  authMode = mode;
  document.querySelectorAll('.auth-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.mode === mode));
  const isReg = mode === 'register';
  $('reg-display').style.display = isReg ? '' : 'none';
  $('reg-pass2').style.display = isReg ? '' : 'none';
  $('reg-hint').style.display = isReg ? '' : 'none';
  $('auth-submit').textContent = isReg ? 'Создать аккаунт' : 'Войти';
  $('auth-pass').setAttribute('autocomplete', isReg ? 'new-password' : 'current-password');
  $('auth-error').textContent = '';
}
document.querySelectorAll('.auth-tab').forEach(tab =>
  tab.addEventListener('click', () => setAuthMode(tab.dataset.mode)));
$('auth-submit').addEventListener('click', doAuth);
$('auth-user').addEventListener('keydown', e => { if (e.key === 'Enter') $('auth-pass').focus(); });
$('auth-pass').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  if (authMode === 'register') $('reg-pass2').focus(); else doAuth();
});
$('reg-pass2').addEventListener('keydown', e => { if (e.key === 'Enter') doAuth(); });

async function doAuth() {
  const u = $('auth-user').value.trim();
  const p = $('auth-pass').value;
  const errEl = $('auth-error'); errEl.textContent = '';
  if (!u || !p) { errEl.textContent = 'Заполни все поля'; return; }
  $('auth-submit').disabled = true;
  try {
    let data;
    if (authMode === 'login') {
      data = await api('/api/login', { method: 'POST', body: { username: u, password: p } });
    } else {
      const p2 = $('reg-pass2').value;
      const dn = $('reg-display').value.trim();
      if (p !== p2) { errEl.textContent = 'Пароли не совпадают'; return; }
      data = await api('/api/register', {
        method: 'POST',
        body: { username: u, display_name: dn, password: p, password2: p2 },
      });
    }
    state.token = data.token; state.username = data.username;
    state.display_name = data.display_name || data.username;
    localStorage.setItem(LS_TOKEN, data.token);
    await enterApp();
  } catch (e) { errEl.textContent = e.message || 'Ошибка'; }
  finally { $('auth-submit').disabled = false; }
}

function logout() {
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch (e) {} state.ws = null; }
  if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
  state.token = null; state.username = null; state.display_name = null;
  state.currentChannel = null; state.channels = [];
  localStorage.removeItem(LS_TOKEN);
  $('app').classList.remove('visible', 'chat-open');
  $('auth-screen').classList.add('visible');
  $('auth-pass').value = ''; $('reg-pass2').value = ''; $('reg-display').value = '';
  setAuthMode('login');
}

/* BOOT */
async function boot() {
  if (!state.token) { showAuth(); return; }
  try {
    const me = await api('/api/me');
    state.username = me.username;
    state.display_name = me.display_name || me.username;
    await enterApp();
  } catch (e) {
    localStorage.removeItem(LS_TOKEN); state.token = null; showAuth();
  }
}
function showAuth() {
  $('boot').style.display = 'none';
  $('auth-screen').classList.add('visible');
  $('app').classList.remove('visible');
  setAuthMode('login');
}

async function enterApp() {
  try {
    const cfg = await api('/api/config');
    state.allowChannelCreation = !!cfg.allow_channel_creation;
  } catch (e) {}
  $('new-channel-wrap').style.display = state.allowChannelCreation ? '' : 'none';
  paintMe();
  await loadChannels();
  $('boot').style.display = 'none';
  $('auth-screen').classList.remove('visible');
  $('app').classList.add('visible');
}

function paintMe() {
  $('me-name').textContent = state.display_name || state.username;
  $('me-username').textContent = '@' + state.username;
}

/* PROFILE EDIT */
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
    const r = await api('/api/profile', { method: 'PATCH', body: { display_name: dn } });
    state.display_name = r.display_name;
    paintMe();
    $('profile-modal').classList.remove('visible');
  } catch (e) { $('profile-err').textContent = e.message; }
});

/* CHANNELS */
async function loadChannels() {
  try {
    const data = await api('/api/channels');
    state.channels = data.channels;
    state.totalOnline = data.total_online || 0;
    if (state.currentChannel && !state.channels.some(c => c.id === state.currentChannel)) {
      state.currentChannel = null;
      $('app').classList.remove('chat-open');
      $('chat-name').textContent = 'Выбери канал';
      $('chat-users').textContent = '';
      renderMessages([]);
      $('members-list').innerHTML = '';
      $('members-count').textContent = '0';
    }
    renderChannels();
    updateChatHeaderCounters();
  } catch (e) {}
}

function updateChatHeaderCounters() {
  // В шапке канала: полное число онлайн-участников по серверу
  const total = state.totalOnline;
  if (state.currentChannel) {
    $('chat-users').textContent = total + ' участников онлайн';
  } else {
    $('chat-users').textContent = '';
  }
}

function renderChannels() {
  const list = $('channel-list');
  const filter = $('search').value.toLowerCase().trim();
  list.innerHTML = '';
  for (const c of state.channels) {
    if (filter && !c.name.toLowerCase().includes(filter)) continue;
    const el = document.createElement('div');
    el.className = 'channel-item' + (state.currentChannel === c.id ? ' active' : '');
    el.dataset.id = c.id;
    const last = c.last_message
      ? `<div class="channel-last">${escapeHtml(c.last_message.user)}: ${escapeHtml(c.last_message.text)}</div>`
      : '';
    el.innerHTML =
      `<div class="channel-hash">#</div>
       <div class="channel-body">
         <div class="channel-row1">
           <span class="channel-name">${escapeHtml(c.name)}</span>
           <span class="channel-meta">${c.online || 0}</span>
         </div>
         ${last}
       </div>`;
    el.addEventListener('click', () => openChannel(c.id));
    list.appendChild(el);
  }
}

async function openChannel(id) {
  state.currentChannel = id;
  $('app').classList.add('chat-open');
  renderChannels();
  const ch = state.channels.find(c => c.id === id);
  $('chat-name').textContent = ch ? ('# ' + ch.name) : ('# ' + id);
  updateChatHeaderCounters();
  try {
    const data = await api('/api/channels/' + encodeURIComponent(id) + '/messages');
    if (data.profiles) {
      Object.values(data.profiles).forEach(p => {
        state.profiles[(p.username || '').toLowerCase()] = p;
      });
    }
    renderMessages(data.messages);
  } catch (e) { renderMessages([]); }
  reconnectWs(id);
  loadMembers(id);
}

async function loadMembers(id) {
  try {
    const data = await api('/api/channels/' + encodeURIComponent(id) + '/members');
    renderMembers(data.members);
  } catch (e) { renderMembers([]); }
}

function renderMembers(members) {
  $('members-count').textContent = members.length;
  const list = $('members-list');
  if (!members.length) {
    list.innerHTML = '<div style="padding:20px 14px;color:var(--muted);font-size:12px;text-align:center;">Пока никого</div>';
    return;
  }
  list.innerHTML = '';
  for (const m of members) {
    const el = document.createElement('div');
    el.className = 'member-item';
    const self = m.username === state.username;
    el.innerHTML = `<div class="dot"></div><div class="member-name ${self ? 'self' : ''}">${
      escapeHtml(m.display_name || m.username)
    }</div>`;
    el.title = '@' + m.username;
    list.appendChild(el);
  }
}

/* MEMBERS TOGGLE */
const MEMBERS_LS = 'sld_members_visible';
if (localStorage.getItem(MEMBERS_LS) === '1') $('members-panel').classList.add('visible');
$('members-btn').addEventListener('click', () => {
  const p = $('members-panel');
  p.classList.toggle('visible');
  localStorage.setItem(MEMBERS_LS, p.classList.contains('visible') ? '1' : '0');
});

/* MESSAGES */
function renderMessages(msgs) {
  const box = $('messages');
  box.innerHTML = '';
  if (!msgs || !msgs.length) {
    box.innerHTML = '<div class="empty">Пока нет сообщений.<br>Напиши первым</div>';
    return;
  }
  let prevUser = null;
  for (const m of msgs) { appendMessage(m, { skipScroll: true, prevUser }); prevUser = m.user; }
  box.scrollTop = box.scrollHeight;
}

function userProfile(username) {
  return state.profiles[(username || '').toLowerCase()] ||
         (username === state.username
            ? { username: state.username, display_name: state.display_name }
            : { username, display_name: username });
}

function appendMessage(m, opts = {}) {
  const box = $('messages');
  const empty = box.querySelector('.empty'); if (empty) empty.remove();
  const out = m.user === state.username;
  const sameUser = opts.prevUser === m.user;
  const prof = userProfile(m.user);
  const div = document.createElement('div');
  div.className = 'msg ' + (out ? 'out' : 'in') + (sameUser ? ' same-user' : '');
  div.dataset.user = m.user;
  div.dataset.id = m.id;
  const nameHtml = out ? '' : `<div class="name" title="@${escapeHtml(prof.username)}">${
    escapeHtml(prof.display_name || prof.username)}</div>`;
  const edited = m.edited ? ' <span class="edited">(изменено)</span>' : '';
  const textHtml = renderTextWithMentions(m.text, state.username);
  div.innerHTML =
    `<div class="bubble">${nameHtml}
       <div class="text">${textHtml}${edited}</div>
       <div class="time">${hhmm(m.ts)}</div>
     </div>`;
  box.appendChild(div);
  if (!opts.skipScroll) box.scrollTop = box.scrollHeight;
}

/* WEBSOCKET */
function reconnectWs(channelId) {
  if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
  if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch (e) {} state.ws = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws?token=${encodeURIComponent(state.token)}&channel=${encodeURIComponent(channelId)}`;
  const ws = new WebSocket(url);
  state.ws = ws;
  ws.onopen = () => {
    state.pingTimer = setInterval(() => {
      try { ws.send(JSON.stringify({ type: 'ping' })); } catch (e) {}
    }, 25000);
  };
  ws.onmessage = (ev) => {
    let data; try { data = JSON.parse(ev.data); } catch (e) { return; }
    if (data.type === 'message') {
      const m = data.message;
      const ch = state.channels.find(c => c.id === m.channel);
      if (ch) ch.last_message = { user: m.user, text: m.text, ts: m.ts };
      if (m.channel === state.currentChannel) {
        const box = $('messages');
        const last = box.querySelector('.msg:last-child');
        const prevUser = last ? last.dataset.user : null;
        appendMessage(m, { prevUser });
      }
      renderChannels();
    } else if (data.type === 'presence') {
      const counts = data.counts || {};
      for (const c of state.channels) c.online = counts[c.id] || 0;
      state.totalOnline = data.total || 0;
      renderChannels();
      updateChatHeaderCounters();
      if (state.currentChannel) loadMembers(state.currentChannel);
    } else if (data.type === 'channels_changed') {
      loadChannels();
    } else if (data.type === 'message_deleted') {
      if (data.channel === state.currentChannel) {
        const el = $('messages').querySelector(`.msg[data-id="${data.id}"]`);
        if (el) el.remove();
      }
      loadChannels();
    } else if (data.type === 'message_edited') {
      if (data.channel === state.currentChannel) {
        const el = $('messages').querySelector(`.msg[data-id="${data.id}"] .text`);
        if (el) {
          const mentionsHtml = renderTextWithMentions(data.text, state.username);
          el.innerHTML = mentionsHtml + ' <span class="edited">(изменено)</span>';
        }
      }
    } else if (data.type === 'channel_cleared') {
      if (data.channel === state.currentChannel) renderMessages([]);
      loadChannels();
    } else if (data.type === 'profile_updated') {
      state.profiles[(data.username || '').toLowerCase()] = {
        username: data.username, display_name: data.display_name,
      };
      if (data.username === state.username) {
        state.display_name = data.display_name;
        paintMe();
      }
      // Обновляем все сообщения этого пользователя
      document.querySelectorAll(`.msg[data-user="${CSS.escape(data.username)}"] .name`)
        .forEach(el => { el.textContent = data.display_name; });
      if (state.currentChannel) loadMembers(state.currentChannel);
    }
  };
  ws.onclose = (ev) => {
    if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
    if (ev && ev.code === 1008) { logout(); return; }
    if (state.currentChannel === channelId) {
      state.reconnectTimer = setTimeout(() => {
        if (state.currentChannel === channelId) reconnectWs(channelId);
      }, 1500);
    }
  };
}

/* MENTIONS */
const MENTION_QUERY_RE = /(?:^|\s)@([A-Za-z0-9_\-]{0,24})$/;
function currentMentionQuery() {
  const ta = $('msg-input');
  const before = ta.value.slice(0, ta.selectionStart);
  const m = before.match(MENTION_QUERY_RE); return m ? m[1] : null;
}
function hideMentionMenu() { $('mention-menu').classList.remove('visible'); }
function showMentionMenu(query) {
  const menu = $('mention-menu');
  const lowerQ = (query || '').toLowerCase();
  const candidates = Object.values(state.profiles)
    .filter(p => p.username && p.username !== state.username &&
                 p.username.toLowerCase().startsWith(lowerQ)).slice(0, 8);
  if (state.username && state.username.toLowerCase().startsWith(lowerQ)) {
    candidates.unshift({ username: state.username, display_name: state.display_name });
  }
  if (!candidates.length) { hideMentionMenu(); return; }
  menu.innerHTML = candidates.map(p => `
    <div class="mention-item" data-nick="${escapeHtml(p.username)}">
      ${escapeHtml(p.display_name || p.username)}
      <span class="u">@${escapeHtml(p.username)}</span>
    </div>`).join('');
  menu.classList.add('visible');
  menu.querySelectorAll('.mention-item').forEach(el => {
    el.addEventListener('mousedown', (e) => { e.preventDefault(); insertMention(el.dataset.nick); });
  });
}
function insertMention(nick) {
  const ta = $('msg-input');
  const before = ta.value.slice(0, ta.selectionStart);
  const after  = ta.value.slice(ta.selectionStart);
  const replaced = before.replace(/@([A-Za-z0-9_\-]{0,24})$/, '@' + nick + ' ');
  ta.value = replaced + after;
  ta.selectionStart = ta.selectionEnd = replaced.length;
  ta.focus(); hideMentionMenu();
}
$('msg-input').addEventListener('input', e => {
  e.target.style.height = 'auto';
  e.target.style.height = Math.min(e.target.scrollHeight, 130) + 'px';
  const q = currentMentionQuery();
  if (q !== null) showMentionMenu(q); else hideMentionMenu();
});
$('msg-input').addEventListener('blur', () => setTimeout(hideMentionMenu, 120));

function sendMessage() {
  const input = $('msg-input');
  const text = input.value.trim();
  if (!text) return;
  if (!state.ws || state.ws.readyState !== 1) return;
  state.ws.send(JSON.stringify({ type: 'message', text }));
  input.value = ''; input.style.height = 'auto'; hideMentionMenu();
}
$('send-btn').addEventListener('click', sendMessage);
$('msg-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  else if (e.key === 'Escape') hideMentionMenu();
});

/* MISC */
$('logout-btn').addEventListener('click', logout);
$('back-btn').addEventListener('click', () => $('app').classList.remove('chat-open'));
$('search').addEventListener('input', renderChannels);
$('new-channel-btn').addEventListener('click', async () => {
  const name = $('new-channel-name').value.trim();
  if (!name) return;
  try {
    await api('/api/channels', { method: 'POST', body: { name } });
    $('new-channel-name').value = '';
    await loadChannels();
  } catch (e) { alert(e.message); }
});
$('new-channel-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('new-channel-btn').click();
});

boot();
</script>
</body>
</html>
"""


# ============================================================
#  АДМИНКА (стиль Cockpit/CentOS)
# ============================================================
ADMIN_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1100">
<title>sldchat · admin</title>
<style>
:root {
  --topbg: #22272e;
  --topfg: #eaecef;
  --side: #f4f5f7;
  --side-hover: #e6e8eb;
  --side-active: #dde1e5;
  --border: #d9dce1;
  --border-soft: #ebedf0;
  --text: #1a1a1a;
  --muted: #5c6670;
  --accent: #0066cc;
  --accent-hover: #0055ad;
  --green: #3db83d;
  --red: #cc0000;
  --bg: #f4f5f7;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 13px; color: var(--text); background: var(--bg);
  min-width: 1000px;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
button { font-family: inherit; font-size: inherit; cursor: pointer; border: none; background: none; color: inherit; }
input, select { font-family: inherit; }
svg { display: block; }

/* ---------- LOGIN ---------- */
#login-view {
  position: fixed; inset: 0; z-index: 100;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg);
}
.login-card {
  width: 360px; background: #fff; border: 1px solid var(--border);
  border-radius: 3px; padding: 28px 26px;
  box-shadow: 0 2px 8px rgba(0,0,0,.06);
}
.login-logo {
  font-size: 22px; color: var(--text); font-weight: 400;
  text-align: center; letter-spacing: .5px;
}
.login-sub {
  font-size: 11px; color: var(--muted); text-align: center;
  margin-top: 4px; text-transform: uppercase; letter-spacing: 1.5px;
}
.login-fields { margin-top: 24px; }
.login-fields label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 5px; }
.login-fields input {
  width: 100%; padding: 8px 11px; background: #fff;
  border: 1px solid var(--border); border-radius: 3px;
  color: var(--text); font-size: 14px; outline: none;
}
.login-fields input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(0,102,204,.15); }
.login-err { color: var(--red); font-size: 12px; min-height: 16px; margin-top: 8px; }
.login-btn {
  width: 100%; margin-top: 6px; padding: 9px;
  background: var(--accent); color: #fff; border-radius: 3px;
  font-size: 13px; font-weight: 500;
}
.login-btn:hover { background: var(--accent-hover); }
.login-btn:disabled { opacity: .55; cursor: default; }

/* ---------- LAYOUT ---------- */
#panel-view { display: none; min-height: 100vh; }
#panel-view.visible { display: block; }

.topbar {
  height: 46px; background: var(--topbg); color: var(--topfg);
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 16px;
  position: sticky; top: 0; z-index: 10;
}
.crumb { font-size: 13px; font-weight: 500; display: flex; align-items: center; gap: 8px; }
.crumb .sep { color: #5c6670; }
.crumb .tag {
  font-size: 10.5px; color: #9aa5ad; letter-spacing: 1.5px;
  text-transform: uppercase; border: 1px solid #3f4550;
  padding: 2px 7px; border-radius: 2px;
}
.topbar .actions { display: flex; align-items: center; gap: 8px; }
.topbar .btn-t {
  padding: 5px 12px; border: 1px solid #3f4550; border-radius: 3px;
  color: var(--topfg); font-size: 12px;
}
.topbar .btn-t:hover { background: #2f3640; }
.topbar .btn-t.danger:hover { border-color: #cc0000; color: #ff9b9b; }

.layout { display: flex; min-height: calc(100vh - 46px); }

.sidebar {
  width: 220px; flex-shrink: 0; background: var(--side);
  border-right: 1px solid var(--border); padding: 14px 0;
}
.side-search { padding: 0 14px 12px; }
.side-search input {
  width: 100%; padding: 6px 10px; border: 1px solid var(--border);
  border-radius: 3px; font-size: 12px; outline: none; background: #fff;
}
.side-search input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(0,102,204,.15); }

nav a {
  display: block; padding: 7px 18px; color: var(--text);
  font-size: 13px; cursor: pointer;
  border-left: 3px solid transparent;
}
nav a:hover { background: var(--side-hover); text-decoration: none; }
nav a.active {
  background: var(--side-active);
  border-left-color: var(--accent); color: var(--accent);
  font-weight: 500;
}

.content { flex: 1; padding: 22px 28px; background: #fff; min-width: 0; }

/* ---------- PAGE HEADER ---------- */
.page-title { font-size: 20px; font-weight: 400; margin: 0 0 4px; color: var(--text); }
.page-sub { color: var(--muted); font-size: 12.5px; margin-bottom: 20px; }

/* ---------- CARDS ---------- */
.card {
  background: #fff; border: 1px solid var(--border);
  border-radius: 3px; padding: 18px 20px; margin-bottom: 16px;
}
.card h3 {
  font-size: 15px; font-weight: 500; color: var(--text);
  margin: 0 0 12px;
}
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }

.kv { display: grid; grid-template-columns: 140px 1fr; gap: 8px 16px; font-size: 13px; }
.kv .k { color: var(--muted); }
.kv .v { color: var(--text); }

.metric { font-size: 26px; font-weight: 400; color: var(--text); line-height: 1; }
.metric-label { font-size: 12px; color: var(--muted); margin-top: 6px; }
.metric-card { padding: 18px 20px; }
.metric-card .metric { font-size: 26px; }

.health { color: var(--green); display: flex; align-items: center; gap: 8px; font-size: 13px; }
.health svg { width: 14px; height: 14px; flex-shrink: 0; }

.usage-row { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; font-size: 13px; }
.usage-row .label { width: 60px; color: var(--muted); }
.usage-bar { flex: 1; height: 8px; background: var(--border-soft); border-radius: 4px; overflow: hidden; }
.usage-bar > span { display: block; height: 100%; background: var(--accent); border-radius: 4px; }
.usage-row .val { width: 120px; text-align: right; color: var(--muted); font-size: 12px; }

/* ---------- TABLES ---------- */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; padding: 9px 12px; color: var(--muted);
  font-weight: 500; font-size: 12px; background: var(--side);
  border-bottom: 1px solid var(--border); white-space: nowrap;
}
td { padding: 9px 12px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
tbody tr:hover td { background: #fafbfc; }
tbody tr:last-child td { border-bottom: none; }

.mono { font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace; font-size: 12.5px; }
.muted { color: var(--muted); }
.accent { color: var(--accent); }
.green { color: var(--green); }
.red { color: var(--red); }

.chip {
  display: inline-block; padding: 2px 7px; border-radius: 10px;
  background: var(--side); color: var(--muted); font-size: 11px;
  border: 1px solid var(--border); margin-right: 3px;
}
.chip.blue { background: #e6f0fa; color: #004a99; border-color: #b8d5ef; }
.chip.green { background: #e6f5e6; color: #1a6b1a; border-color: #b8deb8; }
.chip.red { background: #fdeaea; color: #8a1a1a; border-color: #efb8b8; }

/* ---------- BUTTONS ---------- */
.btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 12px; background: #fff; color: var(--text);
  border: 1px solid var(--border); border-radius: 3px;
  font-size: 12px; line-height: 1.4; text-decoration: none;
}
.btn:hover { background: var(--side); text-decoration: none; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn.primary:hover { background: var(--accent-hover); }
.btn.danger { color: var(--red); }
.btn.danger:hover { background: #fdeaea; border-color: var(--red); }
.btn.mini { padding: 3px 9px; font-size: 11.5px; }

.toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
.toolbar input, .toolbar select {
  padding: 6px 10px; border: 1px solid var(--border); border-radius: 3px;
  font-size: 12.5px; outline: none; background: #fff; min-width: 160px;
}
.toolbar input:focus, .toolbar select:focus { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(0,102,204,.15); }
.toolbar .grow { flex: 1; }

.empty-state { padding: 50px 20px; text-align: center; color: var(--muted); font-size: 13px; }
.loading { padding: 40px; text-align: center; color: var(--muted); font-size: 12.5px; }

/* ---------- MODAL ---------- */
.modal-backdrop {
  position: fixed; inset: 0; z-index: 200;
  background: rgba(0,0,0,.4); display: none;
  align-items: center; justify-content: center; padding: 20px;
}
.modal-backdrop.visible { display: flex; }
.modal {
  background: #fff; border: 1px solid var(--border); border-radius: 3px;
  padding: 20px 22px; width: 400px; max-width: 100%;
  box-shadow: 0 8px 32px rgba(0,0,0,.18);
}
.modal h3 { font-size: 16px; font-weight: 500; margin-bottom: 16px; }
.modal label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 5px; }
.modal input, .modal textarea {
  width: 100%; padding: 8px 11px; border: 1px solid var(--border);
  border-radius: 3px; font-size: 13px; outline: none;
  background: #fff; color: var(--text); margin-bottom: 12px;
  font-family: inherit;
}
.modal textarea { min-height: 100px; resize: vertical; }
.modal input:focus, .modal textarea:focus {
  border-color: var(--accent); box-shadow: 0 0 0 2px rgba(0,102,204,.15);
}
.modal .row { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
.modal-err { color: var(--red); font-size: 12px; min-height: 16px; margin-bottom: 8px; }

/* ---------- CHARTS ---------- */
.chart-wrap { display: flex; gap: 20px; align-items: center; }
.pie-legend { font-size: 12px; }
.pie-legend .row2 { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.pie-legend .swatch { width: 12px; height: 12px; border-radius: 2px; flex-shrink: 0; }
.pie-legend .name { color: var(--text); }
.pie-legend .val { color: var(--muted); margin-left: auto; }
.pie-legend .row2 { min-width: 200px; }

.bar-chart { width: 100%; overflow: hidden; }

/* ---------- TOASTS ---------- */
#toasts {
  position: fixed; right: 20px; bottom: 20px; z-index: 300;
  display: flex; flex-direction: column; gap: 8px;
}
.toast {
  padding: 10px 14px; border-radius: 3px;
  background: #22272e; color: #eaecef; font-size: 12.5px;
  border-left: 3px solid var(--green);
  box-shadow: 0 4px 12px rgba(0,0,0,.15);
}
.toast.err { border-left-color: var(--red); }

.hidden { display: none !important; }

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: #f4f5f7; }
::-webkit-scrollbar-thumb { background: #c8cdd4; border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: #a8b0ba; }
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
      <span class="tag">admin</span>
    </div>
    <div class="actions">
      <button class="btn-t" id="refresh-btn">Обновить</button>
      <button class="btn-t danger" id="logout-btn">Выйти</button>
    </div>
  </header>
  <div class="layout">
    <aside class="sidebar">
      <div class="side-search">
        <input id="side-search" placeholder="Поиск">
      </div>
      <nav id="side-nav">
        <a data-tab="dashboard" class="active">Обзор</a>
        <a data-tab="users">Пользователи</a>
        <a data-tab="channels">Каналы</a>
        <a data-tab="messages">Сообщения</a>
      </nav>
    </aside>
    <main class="content" id="main"></main>
  </div>
</div>

<!-- MODALS -->
<div class="modal-backdrop" id="user-edit-modal">
  <div class="modal">
    <h3>Редактировать пользователя</h3>
    <div class="modal-err" id="user-edit-err"></div>
    <label>Отображаемое имя</label>
    <input id="ue-display" type="text" maxlength="32">
    <label>Новый пароль (оставь пустым, чтобы не менять)</label>
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
    <div class="modal-err" id="msg-edit-err"></div>
    <label>Текст сообщения</label>
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
const state = { tab: 'dashboard', currentUser: null, currentMessage: null };

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
function fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtRel(ts) {
  if (!ts) return '—';
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 60) return s + ' с назад';
  if (s < 3600) return Math.floor(s/60) + ' мин назад';
  if (s < 86400) return Math.floor(s/3600) + ' ч назад';
  return Math.floor(s/86400) + ' дн назад';
}
function toast(msg, kind = 'ok') {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind === 'err' ? 'err' : '');
  el.textContent = msg;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (opts.body && typeof opts.body !== 'string') {
    headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, Object.assign({}, opts, { headers, credentials: 'same-origin' }));
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    const err = new Error(detail); err.status = r.status; throw err;
  }
  if (r.status === 204) return null;
  const t = await r.text(); return t ? JSON.parse(t) : null;
}

/* LOGIN */
async function trySession() {
  try { await api('/api/admin/session'); showPanel(); return true; }
  catch (e) { return false; }
}
function showLogin() {
  $('login-view').style.display = '';
  $('panel-view').classList.remove('visible');
}
function showPanel() {
  $('login-view').style.display = 'none';
  $('panel-view').classList.add('visible');
  renderTab();
}
$('login-btn').addEventListener('click', doLogin);
$('admin-pass').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
async function doLogin() {
  const p = $('admin-pass').value;
  $('login-err').textContent = '';
  if (!p) { $('login-err').textContent = 'Введите пароль'; return; }
  $('login-btn').disabled = true;
  try {
    await api('/api/admin/login', { method: 'POST', body: { password: p } });
    $('admin-pass').value = ''; showPanel();
  } catch (e) { $('login-err').textContent = e.message || 'Ошибка'; }
  finally { $('login-btn').disabled = false; }
}
$('logout-btn').addEventListener('click', async () => {
  try { await api('/api/admin/logout', { method: 'POST' }); } catch (e) {}
  showLogin();
});
$('refresh-btn').addEventListener('click', () => renderTab());

/* TABS */
document.querySelectorAll('#side-nav a').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('#side-nav a').forEach(x =>
      x.classList.toggle('active', x === t));
    state.tab = t.dataset.tab;
    renderTab();
  });
});

function renderTab() {
  const main = $('main');
  if (state.tab === 'dashboard') renderDashboard(main);
  else if (state.tab === 'users') renderUsers(main);
  else if (state.tab === 'channels') renderChannels(main);
  else if (state.tab === 'messages') renderMessages(main);
}

/* DASHBOARD */
async function renderDashboard(main) {
  main.innerHTML = `<div class="loading">Загрузка…</div>`;
  try {
    const s = await api('/api/admin/stats');
    const uptime = Math.floor(Date.now()/1000 - s.uptime_started);
    const d = Math.floor(uptime/86400), h = Math.floor((uptime%86400)/3600),
          m = Math.floor((uptime%3600)/60), sec = uptime%60;
    main.innerHTML = `
      <h2 class="page-title">Обзор</h2>
      <div class="page-sub">Сводка по состоянию сервера sldchat</div>
      <div class="grid-2">
        <div class="card">
          <h3>Здоровье</h3>
          <div class="health">
            <svg viewBox="0 0 24 24"><path fill="currentColor" d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
            <span>Система работает нормально</span>
          </div>
          <div class="kv" style="margin-top:14px;">
            <div class="k">Сервер</div><div class="v">sldchat · FastAPI</div>
            <div class="k">Uptime</div><div class="v">${d ? d + ' дн ' : ''}${h} ч ${m} мин ${sec} с</div>
          </div>
        </div>
        <div class="card">
          <h3>Использование</h3>
          <div class="usage-row">
            <div class="label">Онлайн</div>
            <div class="usage-bar"><span style="width:${Math.min(100, s.online*2)}%"></span></div>
            <div class="val">${s.online} чел.</div>
          </div>
          <div class="usage-row">
            <div class="label">Токены</div>
            <div class="usage-bar"><span style="width:${Math.min(100, s.tokens*2)}%"></span></div>
            <div class="val">${s.tokens}</div>
          </div>
          <div class="usage-row">
            <div class="label">Сообщ.</div>
            <div class="usage-bar"><span style="width:${Math.min(100, s.messages/10)}%"></span></div>
            <div class="val">${s.messages}</div>
          </div>
        </div>
      </div>
      <div class="grid-4" style="margin-top:16px;">
        <div class="card metric-card"><div class="metric">${s.users}</div><div class="metric-label">Пользователей</div></div>
        <div class="card metric-card"><div class="metric">${s.channels}</div><div class="metric-label">Каналов</div></div>
        <div class="card metric-card"><div class="metric">${s.messages}</div><div class="metric-label">Сообщений</div></div>
        <div class="card metric-card"><div class="metric">${s.online}</div><div class="metric-label">Онлайн</div></div>
      </div>
    `;
  } catch (e) {
    if (e.status === 401) { showLogin(); return; }
    main.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
}

/* USERS */
async function renderUsers(main) {
  main.innerHTML = `<div class="loading">Загрузка…</div>`;
  try {
    const data = await api('/api/admin/users');
    const rows = data.users.map(u => `
      <tr>
        <td><a href="#" data-open-user="${escapeHtml(u.username)}" class="mono accent">${escapeHtml(u.username)}</a></td>
        <td>${escapeHtml(u.display_name)}</td>
        <td class="muted mono">${fmtTs(u.created)}</td>
        <td class="muted">${fmtRel(u.last_seen)}</td>
        <td class="muted">${u.logins_count}</td>
        <td>${u.messages}</td>
        <td>${u.online_in.length ? u.online_in.map(c => `<span class="chip green">#${escapeHtml(c)}</span>`).join('') : `<span class="chip">offline</span>`}</td>
        <td>
          <button class="btn mini" data-edit-user="${escapeHtml(u.username)}">Изменить</button>
          <button class="btn mini danger" data-del-user="${escapeHtml(u.username)}">Удалить</button>
        </td>
      </tr>
    `).join('');
    main.innerHTML = `
      <h2 class="page-title">Пользователи</h2>
      <div class="page-sub">Всего: ${data.users.length}. Клик по юзернейму — детальная статистика.</div>
      <div class="card" style="padding:0;">
        ${data.users.length ? `<table>
          <thead><tr><th>Юзернейм</th><th>Имя</th><th>Создан</th><th>Был онлайн</th><th>Входов</th><th>Сообщений</th><th>Каналы</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>` : `<div class="empty-state">Нет зарегистрированных пользователей</div>`}
      </div>
    `;
    main.querySelectorAll('[data-open-user]').forEach(a => {
      a.addEventListener('click', e => { e.preventDefault(); renderUserDetail(a.dataset.openUser); });
    });
    main.querySelectorAll('[data-edit-user]').forEach(b => {
      b.addEventListener('click', () => openUserEdit(b.dataset.editUser));
    });
    main.querySelectorAll('[data-del-user]').forEach(b => {
      b.addEventListener('click', () => deleteUser(b.dataset.delUser));
    });
  } catch (e) {
    if (e.status === 401) { showLogin(); return; }
    main.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
}

/* USER DETAIL */
async function renderUserDetail(username) {
  const main = $('main');
  main.innerHTML = `<div class="loading">Загрузка профиля…</div>`;
  try {
    const d = await api('/api/admin/users/' + encodeURIComponent(username));
    const days = d.days || [];
    const msgsPerDay = days.map(day => ({ label: day.slice(5), value: d.messages_per_day[day] || 0 }));
    const loginsPerDay = days.map(day => ({ label: day.slice(5), value: d.logins_per_day[day] || 0 }));

    const pieEntries = Object.entries(d.messages_by_channel || {})
      .sort((a, b) => b[1] - a[1]);
    const palette = ['#0066cc', '#3db83d', '#f0ad4e', '#d9534f', '#9b59b6', '#16a085', '#e67e22', '#34495e'];
    const pieData = pieEntries.map(([ch, n], i) => ({
      label: '#' + (channelsMapName(ch) || ch),
      value: n,
      color: palette[i % palette.length],
    }));

    main.innerHTML = `
      <h2 class="page-title">Профиль: ${escapeHtml(d.display_name)}</h2>
      <div class="page-sub">
        <a href="#" id="back-to-users">← к списку пользователей</a>
      </div>
      <div class="grid-2">
        <div class="card">
          <h3>Учётная запись</h3>
          <div class="kv">
            <div class="k">Юзернейм</div><div class="v mono">@${escapeHtml(d.username)}</div>
            <div class="k">Отображаемое имя</div><div class="v">${escapeHtml(d.display_name)}</div>
            <div class="k">Создан</div><div class="v">${fmtTs(d.created)}</div>
            <div class="k">Последняя активность</div><div class="v">${fmtRel(d.last_seen)} <span class="muted">(${fmtTs(d.last_seen)})</span></div>
            <div class="k">Статус</div><div class="v">${d.online_in.length
              ? d.online_in.map(c => `<span class="chip green">онлайн в #${escapeHtml(c)}</span>`).join('')
              : `<span class="chip">offline</span>`}</div>
            <div class="k">Всего сообщений</div><div class="v">${d.total_messages}</div>
            <div class="k">Всего входов</div><div class="v">${(d.logins||[]).length + (d.logins_per_day ? '' : '')}</div>
          </div>
          <div style="margin-top:14px;">
            <button class="btn" id="detail-edit">Редактировать</button>
            <button class="btn danger" id="detail-del">Удалить аккаунт</button>
          </div>
        </div>
        <div class="card">
          <h3>Сообщений по каналам</h3>
          ${pieData.length ? `
            <div class="chart-wrap">
              <div>${pieChartSvg(pieData, 150)}</div>
              <div class="pie-legend" style="flex:1;">
                ${pieData.map(p => `
                  <div class="row2">
                    <span class="swatch" style="background:${p.color}"></span>
                    <span class="name">${escapeHtml(p.label)}</span>
                    <span class="val">${p.value}</span>
                  </div>`).join('')}
              </div>
            </div>
          ` : `<div class="muted">Пользователь не писал сообщений</div>`}
        </div>
      </div>
      <div class="card">
        <h3>Сообщения по дням (14 дней)</h3>
        ${barChartSvg(msgsPerDay, 900, 140)}
      </div>
      <div class="card">
        <h3>Входы по дням (14 дней)</h3>
        ${barChartSvg(loginsPerDay, 900, 140, '#3db83d')}
      </div>
      <div class="grid-2">
        <div class="card">
          <h3>История входов</h3>
          ${(d.logins && d.logins.length) ? `<table>
            <thead><tr><th>Когда</th><th>IP</th><th>User-Agent</th></tr></thead>
            <tbody>
              ${d.logins.slice(0, 20).map(l => `
                <tr>
                  <td class="mono muted">${fmtTs(l.ts)}</td>
                  <td class="mono">${escapeHtml(l.ip || '—')}</td>
                  <td class="muted" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(l.ua||'')}">${escapeHtml((l.ua||'—').slice(0,80))}</td>
                </tr>`).join('')}
            </tbody>
          </table>` : `<div class="muted">Нет записей</div>`}
        </div>
        <div class="card">
          <h3>Последние сообщения</h3>
          ${(d.recent_messages && d.recent_messages.length) ? `<table>
            <thead><tr><th>Когда</th><th>Канал</th><th>Текст</th></tr></thead>
            <tbody>
              ${d.recent_messages.slice(0, 15).map(m => `
                <tr>
                  <td class="mono muted" style="white-space:nowrap;">${fmtTs(m.ts)}</td>
                  <td><span class="chip blue">#${escapeHtml(m.channel_name)}</span></td>
                  <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(m.text)}">${escapeHtml(m.text)}</td>
                </tr>`).join('')}
            </tbody>
          </table>` : `<div class="muted">Сообщений нет</div>`}
        </div>
      </div>
    `;
    $('back-to-users').addEventListener('click', e => {
      e.preventDefault();
      document.querySelectorAll('#side-nav a').forEach(x =>
        x.classList.toggle('active', x.dataset.tab === 'users'));
      state.tab = 'users';
      renderTab();
    });
    $('detail-edit').addEventListener('click', () => openUserEdit(d.username, d.display_name));
    $('detail-del').addEventListener('click', () => deleteUser(d.username));
  } catch (e) {
    if (e.status === 401) { showLogin(); return; }
    main.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
}

function channelsMapName(cid) {
  const c = (window.__channelsCache || []).find(x => x.id === cid);
  return c ? c.name : cid;
}

/* PIE CHART */
function pieChartSvg(data, size) {
  const total = data.reduce((s, d) => s + d.value, 0) || 1;
  const cx = size/2, cy = size/2, r = size/2 - 4;
  let angle = -Math.PI/2, out = '';
  if (data.length === 1) {
    return `<svg width="${size}" height="${size}"><circle cx="${cx}" cy="${cy}" r="${r}" fill="${data[0].color}"/></svg>`;
  }
  for (const d of data) {
    const a = (d.value/total) * Math.PI * 2;
    const x1 = cx + r * Math.cos(angle), y1 = cy + r * Math.sin(angle);
    const x2 = cx + r * Math.cos(angle + a), y2 = cy + r * Math.sin(angle + a);
    const large = a > Math.PI ? 1 : 0;
    out += `<path d="M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z" fill="${d.color}" stroke="#fff" stroke-width="1"/>`;
    angle += a;
  }
  return `<svg width="${size}" height="${size}">${out}</svg>`;
}

/* BAR CHART */
function barChartSvg(data, w, h, color = '#0066cc') {
  const max = Math.max(1, ...data.map(d => d.value));
  const padL = 30, padR = 10, padT = 10, padB = 24;
  const innerW = w - padL - padR, innerH = h - padT - padB;
  const bw = innerW / Math.max(1, data.length);
  let bars = '';
  // Y-линии
  const yTicks = 3;
  for (let i = 0; i <= yTicks; i++) {
    const y = padT + innerH * (i/yTicks);
    const val = Math.round(max * (1 - i/yTicks));
    bars += `<line x1="${padL}" y1="${y}" x2="${w-padR}" y2="${y}" stroke="#ebedf0" stroke-width="1"/>`;
    bars += `<text x="${padL-5}" y="${y+3}" font-size="10" fill="#5c6670" text-anchor="end">${val}</text>`;
  }
  for (let i = 0; i < data.length; i++) {
    const d = data[i];
    const bh = (d.value/max) * innerH;
    const x = padL + i * bw + 2;
    const y = padT + innerH - bh;
    bars += `<rect x="${x}" y="${y}" width="${bw - 4}" height="${bh}" fill="${color}" rx="1"/>`;
    bars += `<text x="${x + (bw-4)/2}" y="${h - 8}" font-size="9" fill="#5c6670" text-anchor="middle">${escapeHtml(d.label)}</text>`;
  }
  return `<svg width="${w}" height="${h}" class="bar-chart">${bars}</svg>`;
}

/* USER EDIT MODAL */
function openUserEdit(username, displayName) {
  state.currentUser = username;
  $('user-edit-err').textContent = '';
  $('ue-display').value = displayName || '';
  $('ue-pass').value = '';
  $('user-edit-modal').classList.add('visible');
  setTimeout(() => $('ue-display').focus(), 30);
}
$('ue-cancel').addEventListener('click', () => $('user-edit-modal').classList.remove('visible'));
$('ue-save').addEventListener('click', async () => {
  const body = {};
  const dn = $('ue-display').value.trim();
  const pw = $('ue-pass').value.trim();
  if (dn) body.display_name = dn;
  if (pw) body.password = pw;
  if (!Object.keys(body).length) {
    $('user-edit-modal').classList.remove('visible'); return;
  }
  try {
    await api('/api/admin/users/' + encodeURIComponent(state.currentUser),
              { method: 'PATCH', body });
    toast('Пользователь обновлён');
    $('user-edit-modal').classList.remove('visible');
    renderTab();
  } catch (e) { $('user-edit-err').textContent = e.message; }
});

async function deleteUser(username) {
  if (!confirm(`Удалить пользователя "${username}"?\n\nЭто выкинет его из чата и удалит все его сессии.`)) return;
  try {
    const r = await api('/api/admin/users/' + encodeURIComponent(username), { method: 'DELETE' });
    toast(`Пользователь удалён (кик: ${r.kicked})`);
    renderTab();
  } catch (e) { toast(e.message, 'err'); }
}

/* CHANNELS */
async function renderChannels(main) {
  main.innerHTML = `<div class="loading">Загрузка…</div>`;
  try {
    const data = await api('/api/admin/channels');
    window.__channelsCache = data.channels;
    const rows = data.channels.map(c => `
      <tr>
        <td class="mono">#${escapeHtml(c.id)}</td>
        <td>${escapeHtml(c.name)}</td>
        <td class="muted mono">${escapeHtml(c.owner)}</td>
        <td class="muted mono">${fmtTs(c.created)}</td>
        <td>${c.messages}</td>
        <td>${c.online ? `<span class="chip green">${c.online}</span>` : `<span class="chip">0</span>`}</td>
        <td>
          <button class="btn mini" data-clear="${escapeHtml(c.id)}">Очистить</button>
          <button class="btn mini danger" data-del="${escapeHtml(c.id)}">Удалить</button>
        </td>
      </tr>
    `).join('');
    main.innerHTML = `
      <h2 class="page-title">Каналы</h2>
      <div class="page-sub">Создание, очистка и удаление каналов. Изменения видны клиентам мгновенно.</div>
      <div class="toolbar">
        <input id="new-ch" placeholder="имя нового канала" maxlength="32">
        <button class="btn primary" id="new-ch-btn">Создать канал</button>
      </div>
      <div class="card" style="padding:0;">
        ${data.channels.length ? `<table>
          <thead><tr><th>ID</th><th>Название</th><th>Владелец</th><th>Создан</th><th>Сообщений</th><th>Онлайн</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>` : `<div class="empty-state">Нет каналов</div>`}
      </div>
    `;
    main.querySelectorAll('[data-del]').forEach(b =>
      b.addEventListener('click', () => deleteChannel(b.dataset.del)));
    main.querySelectorAll('[data-clear]').forEach(b =>
      b.addEventListener('click', () => clearChannel(b.dataset.clear)));
    $('new-ch-btn').addEventListener('click', createChannel);
    $('new-ch').addEventListener('keydown', e => { if (e.key === 'Enter') createChannel(); });
  } catch (e) {
    if (e.status === 401) { showLogin(); return; }
    main.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
}

async function createChannel() {
  const name = $('new-ch').value.trim();
  if (!name) return;
  try {
    await api('/api/admin/channels', { method: 'POST', body: { name } });
    toast('Канал создан'); renderTab();
  } catch (e) { toast(e.message, 'err'); }
}
async function deleteChannel(cid) {
  if (!confirm(`Удалить канал #${cid} и всю его историю?`)) return;
  try {
    await api('/api/admin/channels/' + encodeURIComponent(cid), { method: 'DELETE' });
    toast(`Канал #${cid} удалён`); renderTab();
  } catch (e) { toast(e.message, 'err'); }
}
async function clearChannel(cid) {
  if (!confirm(`Очистить историю #${cid}?`)) return;
  try {
    await api('/api/admin/channels/' + encodeURIComponent(cid) + '/messages', { method: 'DELETE' });
    toast(`История #${cid} очищена`); renderTab();
  } catch (e) { toast(e.message, 'err'); }
}

/* MESSAGES */
async function renderMessages(main) {
  main.innerHTML = `<div class="loading">Загрузка…</div>`;
  try {
    // Всегда тянем свежий список каналов, чтобы новые каналы сразу появлялись в фильтре
    const channelsData = await api('/api/admin/channels');
    window.__channelsCache = channelsData.channels;
    const options = [`<option value="">— все каналы —</option>`]
      .concat(channelsData.channels.map(c => `<option value="${escapeHtml(c.id)}">#${escapeHtml(c.name)}</option>`))
      .join('');

    main.innerHTML = `
      <h2 class="page-title">Сообщения</h2>
      <div class="page-sub">Просмотр, редактирование и удаление сообщений.</div>
      <div class="toolbar">
        <select id="flt-channel">${options}</select>
        <input id="flt-user" class="mono" placeholder="автор (юзернейм)">
        <input id="flt-search" class="grow" placeholder="поиск по тексту…">
        <button class="btn primary" id="flt-apply">Применить</button>
      </div>
      <div class="card" style="padding:0;" id="msgs-wrap"><div class="loading">Загрузка…</div></div>
    `;

    const load = async () => {
      const params = new URLSearchParams();
      const ch = $('flt-channel').value;
      const u = $('flt-user').value.trim();
      const q = $('flt-search').value.trim();
      if (ch) params.set('channel', ch);
      if (u) params.set('user', u);
      if (q) params.set('search', q);
      params.set('limit', '500');
      const wrap = $('msgs-wrap');
      wrap.innerHTML = `<div class="loading">Загрузка…</div>`;
      try {
        const data = await api('/api/admin/messages?' + params.toString());
        const rows = data.messages.map(m => `
          <tr>
            <td class="muted mono" style="white-space:nowrap;">${fmtTs(m.ts)}</td>
            <td><span class="chip blue">#${escapeHtml(m.channel_name || m.channel)}</span></td>
            <td class="mono accent">${escapeHtml(m.user)}</td>
            <td style="max-width:520px;word-break:break-word;">${escapeHtml(m.text)}${m.edited?' <span class="muted">(изм.)</span>':''}</td>
            <td style="white-space:nowrap;">
              <button class="btn mini" data-medit="${escapeHtml(m.id)}" data-mtext="${escapeHtml(m.text)}">Изменить</button>
              <button class="btn mini danger" data-mdel="${escapeHtml(m.id)}">Удалить</button>
            </td>
          </tr>
        `).join('');
        wrap.innerHTML = data.messages.length ? `
          <table>
            <thead><tr><th>Время</th><th>Канал</th><th>Автор</th><th>Текст</th><th></th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        ` : `<div class="empty-state">Ничего не найдено</div>`;
        wrap.querySelectorAll('[data-medit]').forEach(b =>
          b.addEventListener('click', () => openMessageEdit(b.dataset.medit, b.dataset.mtext)));
        wrap.querySelectorAll('[data-mdel]').forEach(b =>
          b.addEventListener('click', () => deleteMessage(b.dataset.mdel)));
      } catch (e) {
        if (e.status === 401) { showLogin(); return; }
        wrap.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
      }
    };

    $('flt-apply').addEventListener('click', load);
    $('flt-search').addEventListener('keydown', e => { if (e.key === 'Enter') load(); });
    load();
  } catch (e) {
    if (e.status === 401) { showLogin(); return; }
    main.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
}

function openMessageEdit(id, text) {
  state.currentMessage = id;
  $('msg-edit-err').textContent = '';
  $('me-text').value = text;
  $('msg-edit-modal').classList.add('visible');
  setTimeout(() => $('me-text').focus(), 30);
}
$('me-cancel').addEventListener('click', () => $('msg-edit-modal').classList.remove('visible'));
$('me-save').addEventListener('click', async () => {
  const text = $('me-text').value.trim();
  if (!text) { $('msg-edit-err').textContent = 'Текст не может быть пустым'; return; }
  try {
    await api('/api/admin/messages/' + encodeURIComponent(state.currentMessage),
              { method: 'PATCH', body: { text } });
    toast('Сообщение обновлено');
    $('msg-edit-modal').classList.remove('visible');
    renderTab();
  } catch (e) { $('msg-edit-err').textContent = e.message; }
});

async function deleteMessage(id) {
  if (!confirm('Удалить сообщение?')) return;
  try {
    await api('/api/admin/messages/' + encodeURIComponent(id), { method: 'DELETE' });
    toast('Сообщение удалено'); renderTab();
  } catch (e) { toast(e.message, 'err'); }
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
  const ok = await trySession();
  if (!ok) showLogin();
})();
</script>
</body>
</html>
"""


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
            "<h1 style='font-family:sans-serif;color:#cc0000;padding:40px'>"
            "Админка отключена: не задан ADMIN_PASS</h1>",
            status_code=503,
        )
    return ADMIN_PAGE


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": _now()}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
