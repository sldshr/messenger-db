"""
sldchat — веб-чат (IRC/TeamSpeak-like) на FastAPI.
Один файл. Всё состояние — в оперативной памяти.
Реалтайм — через WebSocket. Админка — /admin (пароль из ADMIN_PASS).

ENV:
  ADMIN_PASS   пароль для /admin. Если не задан — админка отключена.
  PORT         порт (по умолчанию 8080)

Запуск:
  pip install fastapi "uvicorn[standard]"
  ADMIN_PASS=secret python main.py
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
    Header, Depends, Cookie, Response,
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
        raise HTTPException(403, "Админка отключена (не задан ADMIN_PASS)")
    if not sld_admin or sld_admin not in admin_sessions:
        raise HTTPException(401, "Admin unauthorized")


def public_user(username: str) -> dict:
    u = users.get(username.lower())
    if not u:
        return {"username": username, "display_name": username}
    return {"username": u["username"], "display_name": u.get("display_name") or u["username"]}


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


# ============================================================
#  FASTAPI
# ============================================================
app = FastAPI(title="sldchat")


# ---------- AUTH ----------
@app.post("/api/register")
def register(body: RegisterBody):
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
        "created": time.time(),
    }
    token = secrets.token_urlsafe(24)
    tokens[token] = body.username
    return {"token": token, **public_user(body.username)}


@app.post("/api/login")
def login(body: LoginBody):
    key = body.username.strip().lower()
    u = users.get(key)
    if not u or u["hash"] != hash_pw(body.password, u["salt"]):
        raise HTTPException(401, "Неверный юзернейм или пароль")
    token = secrets.token_urlsafe(24)
    tokens[token] = u["username"]
    return {"token": token, **public_user(u["username"])}


@app.get("/api/me")
def me(user: str = Depends(require_user)):
    return public_user(user)


@app.get("/api/config")
def get_config():
    return {"allow_channel_creation": ALLOW_CHANNEL_CREATION}


# ---------- PRESENCE ----------
def _presence_counts() -> Dict[str, int]:
    return {cid: len(u) for cid, u in presence.items() if u}


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
    await broadcast_all({"type": "presence", "counts": _presence_counts()})


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
    return {"channels": out}


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

    channels[cid] = {"id": cid, "name": raw, "owner": user, "created": time.time()}
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
        "ts": time.time(),
    }
    lst = messages.setdefault(cid, [])
    lst.append(msg)
    if len(lst) > 2000:
        del lst[:-2000]
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
    presence.setdefault(cid, {})[username] = time.time()
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
    response.set_cookie(
        "sld_admin", token,
        httponly=True, samesite="lax", max_age=60 * 60 * 12,
    )
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
    online = sum(len(v) for v in presence.values())
    return {
        "users": len(users),
        "channels": len(channels),
        "messages": total_msgs,
        "online": online,
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
            "tokens": token_count,
            "messages": msg_count,
            "online_in": online_in,
        })
    out.sort(key=lambda x: x["created"], reverse=True)
    return {"users": out}


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
        lst = messages.get(cid) or []
        out.append({
            "id": c["id"],
            "name": c["name"],
            "owner": c["owner"],
            "created": c["created"],
            "messages": len(lst),
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
    channels[cid] = {"id": cid, "name": raw, "owner": "admin", "created": time.time()}
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
  --bg: #e6ebee; --panel: #ffffff; --panel-2: #f2f5f8; --border: #dfe5ea;
  --text: #222; --muted: #9aa5ad; --accent: #517da2; --accent-hover: #46708f;
  --bubble-in: #ffffff; --bubble-out: #eeffde; --bubble-out-time: #7d8b7d;
  --shadow: rgba(0,0,0,.08); --chat-bg: #e6ebee;
}
html[data-theme="dark"] {
  --bg: #0f1720; --panel: #17212b; --panel-2: #202b36; --border: #24313d;
  --text: #e7edf3; --muted: #8fa1b3; --accent: #4f87b8; --accent-hover: #5c96c8;
  --bubble-in: #1f2c38; --bubble-out: #2b5278; --bubble-out-time: #b9d0e6;
  --shadow: rgba(0,0,0,.35); --chat-bg: #0d141b;
}
* { box-sizing: border-box; margin: 0; padding: 0;
    -webkit-tap-highlight-color: transparent;
    -webkit-user-select: none; user-select: none;
    -webkit-touch-callout: none; }
input, textarea { -webkit-user-select: text; user-select: text; }
html, body { height: 100vh; height: 100dvh; overflow: hidden;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 14px; color: var(--text); background: var(--bg); }
button { font-family: inherit; cursor: pointer; border: none; background: none; color: inherit; }
input, textarea { font-family: inherit; }
svg { display: block; }

#boot { position: fixed; inset: 0; z-index: 200;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  background: linear-gradient(140deg, #5c88ae 0%, #3c6591 55%, #2e5379 100%); color: #fff; }
#boot .logo { font-size: 34px; font-weight: 300; letter-spacing: 4px; }
.spinner { margin-top: 26px; width: 40px; height: 40px;
  border: 3px solid rgba(255,255,255,.25); border-top-color: #fff; border-radius: 50%;
  animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

#auth-screen { position: fixed; inset: 0; z-index: 100; display: none;
  align-items: center; justify-content: center; padding: 20px;
  background: linear-gradient(140deg, #5c88ae 0%, #3c6591 55%, #2e5379 100%);
  overflow-y: auto; }
#auth-screen.visible { display: flex; }
.auth-card { width: 380px; max-width: 100%; background: var(--panel);
  border-radius: 14px; overflow: hidden;
  box-shadow: 0 24px 70px rgba(0,0,0,.4); color: var(--text); }
.auth-head { padding: 26px 26px 4px; text-align: center; }
.auth-logo { font-size: 32px; font-weight: 300; letter-spacing: 3px; color: var(--accent); }
.auth-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
.auth-tabs { display: flex; padding: 0 22px; margin-top: 18px; border-bottom: 1px solid var(--border); }
.auth-tab { flex: 1; padding: 12px 0; font-size: 13.5px; font-weight: 600;
  color: var(--muted); border-bottom: 2px solid transparent; transition: color .15s, border-color .15s; }
.auth-tab:hover { color: var(--accent); }
.auth-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.auth-body { padding: 18px 24px 24px; }
.auth-body input { width: 100%; padding: 12px 14px; margin-bottom: 12px;
  border: 1px solid var(--border); border-radius: 8px;
  font-size: 14px; outline: none; background: var(--panel-2); color: var(--text);
  transition: border-color .15s, box-shadow .15s, background .15s; }
.auth-body input:focus { border-color: var(--accent); background: var(--panel);
  box-shadow: 0 0 0 3px rgba(81,125,162,.14); }
.auth-error { color: #d64541; font-size: 12px; min-height: 16px; margin-bottom: 6px; }
.auth-submit { width: 100%; margin-top: 6px; padding: 12px;
  background: var(--accent); color: #fff; border-radius: 8px;
  font-size: 14px; font-weight: 600; transition: background .15s, transform .06s; }
.auth-submit:hover { background: var(--accent-hover); }
.auth-submit:active { transform: scale(.985); }
.auth-submit:disabled { opacity: .6; cursor: default; }
.auth-hint { font-size: 11px; color: var(--muted); margin-top: 8px; line-height: 1.5; }
.auth-hint a { color: var(--accent); text-decoration: none; }

#app { display: none; height: 100vh; height: 100dvh; }
#app.visible { display: flex; }

.sidebar { width: 300px; flex-shrink: 0; background: var(--panel);
  border-right: 1px solid var(--border); display: flex; flex-direction: column; }
.sidebar-header { height: 56px; flex-shrink: 0; background: var(--accent); color: #fff;
  display: flex; align-items: center; justify-content: space-between; padding: 0 10px 0 14px; }
.me { display: flex; align-items: center; gap: 10px; min-width: 0; }
.avatar { width: 34px; height: 34px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-weight: 600; font-size: 14px; text-transform: uppercase;
  flex-shrink: 0; color: #fff; }
#me-name { font-weight: 600; font-size: 14px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.icon-btn { width: 36px; height: 36px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  color: #fff; opacity: .85; flex-shrink: 0; transition: background .12s, opacity .12s; }
.icon-btn:hover { background: rgba(255,255,255,.14); opacity: 1; }

.search-wrap { position: relative; padding: 10px 12px;
  border-bottom: 1px solid var(--border); flex-shrink: 0; }
.search-icon { position: absolute; left: 22px; top: 50%; transform: translateY(-50%);
  color: var(--muted); pointer-events: none; }
.search-wrap input { width: 100%; padding: 9px 12px 9px 36px;
  background: var(--panel-2); border: 1px solid transparent; border-radius: 8px;
  font-size: 13px; outline: none; color: var(--text); }
.search-wrap input:focus { background: var(--panel); border-color: var(--border); }

.channel-list { flex: 1; overflow-y: auto; padding: 6px 0; }
.channel-item { display: flex; align-items: center; gap: 10px;
  padding: 10px 14px; cursor: pointer; transition: background .12s; }
.channel-item:hover { background: var(--panel-2); }
.channel-item.active { background: var(--accent); color: #fff; }
.channel-item.active .channel-meta,
.channel-item.active .channel-last { color: rgba(255,255,255,.8); }
.channel-hash { width: 34px; height: 34px; flex-shrink: 0; border-radius: 50%;
  background: var(--panel-2); color: var(--accent);
  display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 15px; }
.channel-item.active .channel-hash { background: rgba(255,255,255,.2); color: #fff; }
.channel-body { flex: 1; min-width: 0; }
.channel-row1 { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.channel-name { font-weight: 600; font-size: 14px; color: inherit;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.channel-meta { font-size: 11px; color: var(--muted); flex-shrink: 0; }
.channel-last { font-size: 12px; color: var(--muted); margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.new-channel { display: flex; gap: 8px; padding: 10px 12px;
  border-top: 1px solid var(--border); background: var(--panel); flex-shrink: 0; }
.new-channel input { flex: 1; min-width: 0; padding: 9px 12px;
  border: 1px solid var(--border); border-radius: 8px;
  font-size: 13px; outline: none; background: var(--panel-2); color: var(--text); }
.new-channel input:focus { border-color: var(--accent); }
.new-channel button { width: 38px; height: 38px; border-radius: 8px;
  background: var(--accent); color: #fff; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; }
.new-channel button:hover { background: var(--accent-hover); }

.chat { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.chat-header { height: 56px; flex-shrink: 0; background: var(--accent); color: #fff;
  display: flex; align-items: center; padding: 0 10px 0 14px; gap: 8px; }
#back-btn { display: none; width: 36px; height: 36px;
  align-items: center; justify-content: center; border-radius: 8px; color: #fff; }
#back-btn:hover { background: rgba(255,255,255,.14); }
.chat-title { display: flex; flex-direction: column; min-width: 0; flex: 1; }
#chat-name { font-weight: 600; font-size: 14px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-users { font-size: 11px; opacity: .85; }

.messages { flex: 1; min-height: 0; overflow-y: auto;
  padding: 16px 18px 8px; background: var(--chat-bg); }
.empty { text-align: center; color: var(--muted); margin-top: 60px; font-size: 13px; line-height: 1.6; }
.msg { display: flex; margin-bottom: 4px; align-items: flex-end; gap: 8px; }
.msg.in { justify-content: flex-start; }
.msg.out { justify-content: flex-end; }
.msg.same-user { margin-top: 2px; }
.msg.same-user .name { display: none; }
.msg.same-user .bubble-avatar { visibility: hidden; }
.bubble-avatar { width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 600; font-size: 11px; text-transform: uppercase; }
.msg.out .bubble-avatar { order: 2; }
.bubble { max-width: 72%; padding: 7px 11px 5px;
  border-radius: 12px; background: var(--bubble-in);
  box-shadow: 0 1px 2px var(--shadow);
  word-wrap: break-word; overflow-wrap: break-word; color: var(--text); }
.msg.in .bubble { border-top-left-radius: 4px; }
.msg.out .bubble { background: var(--bubble-out); border-top-right-radius: 4px; }
.msg.same-user.in .bubble,
.msg.same-user.out .bubble { border-top-left-radius: 12px; border-top-right-radius: 12px; }
.name { font-size: 12.5px; font-weight: 600; color: var(--accent); margin-bottom: 2px; }
.text { white-space: pre-wrap; line-height: 1.35; font-size: 14px; }
.text .mention { color: var(--accent); font-weight: 600; }
.text .mention.self { background: rgba(81,125,162,.25); padding: 0 3px; border-radius: 4px; }
.time { font-size: 10.5px; color: var(--muted); text-align: right; margin-top: 3px; margin-left: 12px; }
.msg.out .time { color: var(--bubble-out-time); }

.composer { display: flex; align-items: flex-end; gap: 10px;
  padding: 12px 16px calc(12px + env(safe-area-inset-bottom, 0));
  background: var(--panel); border-top: 1px solid var(--border); flex-shrink: 0;
  position: relative; }
#msg-input { flex: 1; min-width: 0; padding: 11px 14px;
  background: var(--panel-2); border: 1px solid transparent; border-radius: 22px;
  font-size: 14px; line-height: 1.4;
  max-height: 130px; min-height: 44px; resize: none; outline: none; color: var(--text); }
#msg-input:focus { background: var(--panel); border-color: var(--border); }
.send-btn { width: 44px; height: 44px; flex-shrink: 0;
  border-radius: 50%; background: var(--accent); color: #fff;
  display: flex; align-items: center; justify-content: center; }
.send-btn:hover { background: var(--accent-hover); }
.send-btn:disabled { background: #b7c6d2; cursor: default; }

.mention-menu { position: absolute; left: 12px; right: 12px; bottom: calc(100% + 6px);
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; box-shadow: 0 8px 24px var(--shadow);
  max-height: 220px; overflow-y: auto; z-index: 20; display: none; }
.mention-menu.visible { display: block; }
.mention-item { display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; cursor: pointer; font-size: 13px; }
.mention-item:hover { background: var(--panel-2); }
.mention-item .mini-avatar { width: 26px; height: 26px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 11px; font-weight: 700; text-transform: uppercase; }

@media (max-width: 800px) {
  #app.visible { display: block; position: relative; overflow: hidden; }
  .sidebar { position: absolute; inset: 0; width: 100%; border-right: none; }
  .chat { position: absolute; inset: 0; background: var(--chat-bg);
    transform: translateX(100%); transition: transform .24s ease; z-index: 5; }
  #app.chat-open .chat { transform: translateX(0); }
  #back-btn { display: flex; }
  .bubble { max-width: 78%; }
}
.channel-list::-webkit-scrollbar,
.messages::-webkit-scrollbar { width: 8px; height: 8px; }
.channel-list::-webkit-scrollbar-thumb,
.messages::-webkit-scrollbar-thumb { background: rgba(0,0,0,.12); border-radius: 4px; }
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
      <div class="me">
        <span class="avatar" id="me-avatar"></span>
        <span id="me-name">…</span>
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
    </div>
    <div class="messages" id="messages">
      <div class="empty">Выбери канал слева,<br>чтобы начать общение</div>
    </div>
    <div class="composer">
      <div class="mention-menu" id="mention-menu"></div>
      <textarea id="msg-input" placeholder="Написать сообщение..." rows="1"
                enterkeyhint="send"></textarea>
      <button class="send-btn" id="send-btn" aria-label="Отправить">
        <svg viewBox="0 0 24 24" width="22" height="22">
          <path fill="currentColor" d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
        </svg>
      </button>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const LS_THEME = 'sld_theme', LS_TOKEN = 'sld_token';
const state = {
  token: localStorage.getItem(LS_TOKEN) || null,
  username: null, display_name: null,
  channels: [], currentChannel: null,
  ws: null, reconnectTimer: null, pingTimer: null,
  profiles: {}, allowChannelCreation: true,
};
let authMode = 'login';

document.addEventListener('contextmenu', e => e.preventDefault());

/* THEME */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const icon = $('theme-icon');
  if (theme === 'dark') {
    icon.innerHTML = '<path fill="currentColor" d="M6.76 4.84l-1.8-1.79-1.41 1.41 1.79 1.79 1.42-1.41zM4 10.5H1v2h3v-2zm9-9.95h-2V3.5h2V.55zm7.45 3.91l-1.41-1.41-1.79 1.79 1.41 1.41 1.79-1.79zm-3.21 13.7l1.79 1.8 1.41-1.41-1.8-1.79-1.4 1.4zM20 10.5v2h3v-2h-3zm-8-5c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6zm-1 16.95h2V19.5h-2v2.95zm-7.45-3.91l1.41 1.41 1.79-1.8-1.41-1.41-1.79 1.8z"/>';
  } else {
    icon.innerHTML = '<path fill="currentColor" d="M20 8.69V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12 20 8.69zM12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6 6 2.69 6 6-2.69 6-6 6z"/>';
  }
}
applyTheme(localStorage.getItem(LS_THEME) || 'light');
$('theme-btn').addEventListener('click', () => {
  const next = (document.documentElement.getAttribute('data-theme') === 'dark') ? 'light' : 'dark';
  applyTheme(next);
  localStorage.setItem(LS_THEME, next);
});

/* API */
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers['X-Auth-Token'] = state.token;
  if (opts.body && typeof opts.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
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
  const t = new Date(ts * 1000);
  if (isNaN(t)) return '';
  return String(t.getHours()).padStart(2,'0') + ':' + String(t.getMinutes()).padStart(2,'0');
}
function initials(name) {
  name = (name || '?').trim();
  return name ? name[0].toUpperCase() : '?';
}
function colorFor(name) {
  const palette = ['#e57373','#f06292','#ba68c8','#9575cd','#7986cb',
    '#64b5f6','#4fc3f7','#4dd0e1','#4db6ac','#81c784',
    '#aed581','#dce775','#ffb74d','#ff8a65','#a1887f'];
  let h = 0;
  const s = (name || '').toLowerCase();
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return palette[h % palette.length];
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
  const errEl = $('auth-error');
  errEl.textContent = '';
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
    state.token = data.token;
    state.username = data.username;
    state.display_name = data.display_name || data.username;
    localStorage.setItem(LS_TOKEN, data.token);
    await enterApp();
  } catch (e) {
    errEl.textContent = e.message || 'Ошибка';
  } finally {
    $('auth-submit').disabled = false;
  }
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
    localStorage.removeItem(LS_TOKEN);
    state.token = null;
    showAuth();
  }
}
function showAuth() {
  $('boot').style.display = 'none';
  $('auth-screen').classList.add('visible');
  $('app').classList.remove('visible');
  setAuthMode('login');
}
function paintAvatar(el, name) {
  el.textContent = initials(name);
  el.style.background = colorFor(name);
}

async function enterApp() {
  try {
    const cfg = await api('/api/config');
    state.allowChannelCreation = !!cfg.allow_channel_creation;
  } catch (e) {}
  $('new-channel-wrap').style.display = state.allowChannelCreation ? '' : 'none';
  $('me-name').textContent = state.display_name || state.username;
  paintAvatar($('me-avatar'), state.display_name || state.username);
  await loadChannels();
  $('boot').style.display = 'none';
  $('auth-screen').classList.remove('visible');
  $('app').classList.add('visible');
}

/* CHANNELS */
async function loadChannels() {
  try {
    const data = await api('/api/channels');
    state.channels = data.channels;
    // Если текущий канал удалён — возврат в сайдбар
    if (state.currentChannel && !state.channels.some(c => c.id === state.currentChannel)) {
      state.currentChannel = null;
      $('app').classList.remove('chat-open');
      $('chat-name').textContent = 'Выбери канал';
      $('chat-users').textContent = '';
      renderMessages([]);
    }
    renderChannels();
    if (state.currentChannel) {
      const ch = state.channels.find(c => c.id === state.currentChannel);
      if (ch) $('chat-users').textContent = ch.online + ' онлайн';
    }
  } catch (e) {}
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
  $('chat-users').textContent = ch ? (ch.online + ' онлайн') : '';
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
}

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
  const empty = box.querySelector('.empty');
  if (empty) empty.remove();
  const out = m.user === state.username;
  const sameUser = opts.prevUser === m.user;
  const prof = userProfile(m.user);
  const div = document.createElement('div');
  div.className = 'msg ' + (out ? 'out' : 'in') + (sameUser ? ' same-user' : '');
  div.dataset.user = m.user;
  div.dataset.id = m.id;
  const avStyle = `background:${colorFor(prof.display_name || prof.username)}`;
  const avHtml = `<div class="bubble-avatar" style="${avStyle}">${initials(prof.display_name || prof.username)}</div>`;
  const nameHtml = out ? '' : `<div class="name">${escapeHtml(prof.display_name || prof.username)}</div>`;
  const textHtml = renderTextWithMentions(m.text, state.username);
  div.innerHTML = avHtml +
    `<div class="bubble">${nameHtml}<div class="text">${textHtml}</div><div class="time">${hhmm(m.ts)}</div></div>`;
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
      renderChannels();
      if (state.currentChannel) {
        const c = state.channels.find(x => x.id === state.currentChannel);
        if (c) $('chat-users').textContent = c.online + ' онлайн';
      }
    } else if (data.type === 'channels_changed') {
      loadChannels();
    } else if (data.type === 'message_deleted') {
      if (data.channel === state.currentChannel) {
        const el = $('messages').querySelector(`.msg[data-id="${data.id}"]`);
        if (el) el.remove();
      }
      loadChannels();
    } else if (data.type === 'channel_cleared') {
      if (data.channel === state.currentChannel) renderMessages([]);
      loadChannels();
    }
  };

  ws.onclose = (ev) => {
    if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
    if (ev && ev.code === 1008) {
      // Токен/канал стали недействительны — выходим
      logout();
      return;
    }
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
  const m = before.match(MENTION_QUERY_RE);
  return m ? m[1] : null;
}
function hideMentionMenu() { $('mention-menu').classList.remove('visible'); }
function showMentionMenu(query) {
  const menu = $('mention-menu');
  const lowerQ = (query || '').toLowerCase();
  const candidates = Object.values(state.profiles)
    .filter(p => p.username && p.username !== state.username &&
                 p.username.toLowerCase().startsWith(lowerQ))
    .slice(0, 8);
  if (state.username && state.username.toLowerCase().startsWith(lowerQ)) {
    candidates.unshift({ username: state.username, display_name: state.display_name });
  }
  if (!candidates.length) { hideMentionMenu(); return; }
  menu.innerHTML = candidates.map(p => {
    const label = p.display_name || p.username;
    return `<div class="mention-item" data-nick="${escapeHtml(p.username)}">
      <div class="mini-avatar" style="background:${colorFor(label)}">${initials(label)}</div>
      <div>${escapeHtml(label)} <span style="color:var(--muted)">@${escapeHtml(p.username)}</span></div>
    </div>`;
  }).join('');
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
  ta.focus();
  hideMentionMenu();
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
  input.value = '';
  input.style.height = 'auto';
  hideMentionMenu();
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
#  АДМИНКА (зелёная, PC/Tablet only)
# ============================================================
ADMIN_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1000">
<title>sldchat · admin</title>
<style>
:root {
  --bg: #071610;
  --bg2: #0a1e15;
  --panel: #0e2a1c;
  --panel2: #123322;
  --panel3: #17402a;
  --border: #1c4a31;
  --border2: #266041;
  --text: #d4f0dc;
  --muted: #7fae90;
  --accent: #34d399;
  --accent2: #10b981;
  --accent-dim: #0f766e;
  --danger: #f87171;
  --danger-dim: #7f1d1d;
  --warn: #fbbf24;
  --shadow: rgba(0,0,0,.5);
  --glow: 0 0 0 1px rgba(52,211,153,.15), 0 0 24px rgba(52,211,153,.08);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: "SF Mono", ui-monospace, Menlo, Consolas, "Segoe UI", monospace;
  background:
    radial-gradient(ellipse at top left, rgba(52,211,153,.06), transparent 50%),
    radial-gradient(ellipse at bottom right, rgba(16,185,129,.05), transparent 55%),
    var(--bg);
  color: var(--text);
  font-size: 13px;
  min-width: 960px;
  min-height: 100vh;
}
button { font-family: inherit; cursor: pointer; border: none; background: none; color: inherit; font-size: inherit; }
input { font-family: inherit; }
svg { display: block; }

/* ---------- LOGIN ---------- */
#login-view {
  position: fixed; inset: 0; z-index: 100;
  display: flex; align-items: center; justify-content: center;
}
.login-card {
  width: 380px; padding: 32px 28px;
  background: var(--panel);
  border: 1px solid var(--border); border-radius: 14px;
  box-shadow: 0 24px 60px var(--shadow), var(--glow);
}
.login-logo {
  font-size: 26px; font-weight: 600; color: var(--accent);
  letter-spacing: 2px; text-align: center;
  text-shadow: 0 0 20px rgba(52,211,153,.4);
}
.login-sub {
  font-size: 11px; color: var(--muted); text-align: center; margin-top: 4px;
  text-transform: uppercase; letter-spacing: 2px;
}
.login-fields { margin-top: 26px; }
.login-fields input {
  width: 100%; padding: 11px 14px;
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text);
  font-size: 14px; outline: none; letter-spacing: 1px;
}
.login-fields input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(52,211,153,.18);
}
.login-err {
  color: var(--danger); font-size: 12px; min-height: 16px;
  margin-top: 10px;
}
.login-btn {
  width: 100%; margin-top: 14px; padding: 12px;
  background: linear-gradient(180deg, var(--accent), var(--accent2));
  color: #052a1d; font-weight: 700; border-radius: 8px;
  letter-spacing: 1px; font-size: 13px;
  box-shadow: 0 4px 14px rgba(52,211,153,.28);
  transition: transform .06s, filter .15s;
}
.login-btn:hover { filter: brightness(1.06); }
.login-btn:active { transform: scale(.98); }
.login-btn:disabled { filter: grayscale(.5) brightness(.7); cursor: default; }

/* ---------- LAYOUT ---------- */
#panel-view { display: none; min-height: 100vh; }
#panel-view.visible { display: block; }

.topbar {
  height: 56px; padding: 0 22px;
  display: flex; align-items: center; justify-content: space-between;
  border-bottom: 1px solid var(--border);
  background: rgba(10,30,21,.85); backdrop-filter: blur(8px);
  position: sticky; top: 0; z-index: 10;
}
.topbar-l { display: flex; align-items: center; gap: 14px; }
.topbar-logo {
  font-size: 16px; font-weight: 700; color: var(--accent);
  letter-spacing: 1.5px;
  text-shadow: 0 0 12px rgba(52,211,153,.5);
}
.topbar-tag {
  font-size: 10.5px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 2px; padding: 3px 8px; border: 1px solid var(--border);
  border-radius: 4px;
}
.topbar-r { display: flex; gap: 8px; }

.ghost-btn {
  padding: 7px 12px; border: 1px solid var(--border2); border-radius: 6px;
  color: var(--muted); font-size: 12px; letter-spacing: .5px;
  transition: border-color .15s, color .15s, background .15s;
}
.ghost-btn:hover { border-color: var(--accent); color: var(--accent); background: rgba(52,211,153,.06); }
.ghost-btn.danger:hover { border-color: var(--danger); color: var(--danger); background: rgba(248,113,113,.06); }

.layout { display: grid; grid-template-columns: 220px 1fr; min-height: calc(100vh - 56px); }

.side { border-right: 1px solid var(--border); padding: 18px 12px; background: rgba(7,22,16,.6); }
.side-tab {
  display: flex; align-items: center; gap: 10px;
  width: 100%; padding: 11px 14px; border-radius: 8px;
  color: var(--muted); font-size: 13px; text-align: left;
  transition: color .12s, background .12s; margin-bottom: 4px;
}
.side-tab:hover { color: var(--text); background: var(--panel); }
.side-tab.active {
  color: var(--accent); background: var(--panel);
  box-shadow: inset 0 0 0 1px var(--border2);
}
.side-tab svg { width: 16px; height: 16px; flex-shrink: 0; }

.main { padding: 22px 26px; }

.page-title {
  font-size: 20px; font-weight: 600; letter-spacing: .5px;
  margin-bottom: 4px; color: var(--text);
}
.page-sub { font-size: 12px; color: var(--muted); margin-bottom: 20px; }

/* ---------- STATS ---------- */
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.stat {
  padding: 18px; background: var(--panel);
  border: 1px solid var(--border); border-radius: 10px;
  position: relative; overflow: hidden;
}
.stat::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: linear-gradient(180deg, var(--accent), var(--accent-dim));
}
.stat-value { font-size: 28px; font-weight: 700; color: var(--accent); line-height: 1; }
.stat-label {
  font-size: 11px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 1.5px; margin-top: 6px;
}

/* ---------- TOOLBAR ---------- */
.toolbar {
  display: flex; gap: 10px; align-items: center; margin-bottom: 16px;
  flex-wrap: wrap;
}
.toolbar input, .toolbar select {
  padding: 8px 12px; background: var(--panel); color: var(--text);
  border: 1px solid var(--border); border-radius: 7px;
  font-size: 12.5px; outline: none; min-width: 180px;
}
.toolbar input:focus, .toolbar select:focus {
  border-color: var(--accent); box-shadow: 0 0 0 3px rgba(52,211,153,.15);
}
.toolbar .grow { flex: 1; }
.toolbar button {
  padding: 8px 14px; border-radius: 7px;
  background: linear-gradient(180deg, var(--accent), var(--accent2));
  color: #052a1d; font-weight: 700; font-size: 12.5px; letter-spacing: .5px;
  transition: filter .15s, transform .06s;
}
.toolbar button:hover { filter: brightness(1.06); }
.toolbar button:active { transform: scale(.98); }

/* ---------- TABLE ---------- */
.table-wrap {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden;
}
table { width: 100%; border-collapse: collapse; }
thead th {
  text-align: left; padding: 12px 14px;
  font-size: 11px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 1.4px;
  background: var(--panel2); border-bottom: 1px solid var(--border);
  position: sticky; top: 0;
}
tbody td {
  padding: 11px 14px; font-size: 12.5px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
tbody tr:hover { background: rgba(52,211,153,.045); }
tbody tr:last-child td { border-bottom: none; }

.mono { font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace; }
.muted { color: var(--muted); }
.accent { color: var(--accent); }
.warn { color: var(--warn); }
.danger { color: var(--danger); }

.chip {
  display: inline-block; padding: 2px 8px; border-radius: 10px;
  background: var(--panel3); color: var(--accent);
  font-size: 10.5px; letter-spacing: .5px;
  border: 1px solid var(--border2);
}
.chip.muted { background: var(--panel2); color: var(--muted); border-color: var(--border); }
.chip.danger { background: rgba(248,113,113,.08); color: var(--danger); border-color: rgba(248,113,113,.3); }
.chip.warn { background: rgba(251,191,36,.08); color: var(--warn); border-color: rgba(251,191,36,.3); }

.msg-text { max-width: 480px; word-break: break-word; white-space: pre-wrap; }

.actions { display: flex; gap: 6px; }
.btn-mini {
  padding: 5px 10px; border-radius: 6px;
  border: 1px solid var(--border2); color: var(--muted);
  font-size: 11px; letter-spacing: .5px;
  transition: border-color .15s, color .15s, background .15s;
}
.btn-mini:hover { border-color: var(--accent); color: var(--accent); background: rgba(52,211,153,.06); }
.btn-mini.danger:hover { border-color: var(--danger); color: var(--danger); background: rgba(248,113,113,.06); }

.empty-state {
  padding: 60px 20px; text-align: center; color: var(--muted);
  font-size: 13px;
}
.empty-state b { color: var(--accent); }

/* ---------- TOASTS ---------- */
#toasts {
  position: fixed; right: 20px; bottom: 20px; z-index: 200;
  display: flex; flex-direction: column; gap: 8px;
}
.toast {
  padding: 11px 16px; border-radius: 8px;
  background: var(--panel2); border: 1px solid var(--border2);
  color: var(--text); font-size: 12.5px;
  box-shadow: 0 8px 24px var(--shadow);
  animation: slidein .22s ease;
}
.toast.ok { border-color: rgba(52,211,153,.5); }
.toast.err { border-color: rgba(248,113,113,.5); color: #fecaca; }
@keyframes slidein { from { transform: translateX(20px); opacity: 0; } to { transform: none; opacity: 1; } }

/* ---------- MISC ---------- */
.hidden { display: none !important; }
.loading { padding: 40px; text-align: center; color: var(--muted); font-size: 12.5px; }
.loading::after { content: "…"; animation: dots 1.2s steps(3) infinite; }
@keyframes dots { 0%{content:".";} 33%{content:"..";} 66%{content:"...";} }

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: var(--bg2); }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: var(--accent-dim); }
</style>
</head>
<body>

<!-- LOGIN -->
<div id="login-view">
  <div class="login-card">
    <div class="login-logo">sldchat</div>
    <div class="login-sub">admin panel</div>
    <div class="login-fields">
      <input id="admin-pass" type="password" placeholder="admin password"
             autocomplete="current-password" autofocus>
      <div class="login-err" id="login-err"></div>
      <button class="login-btn" id="login-btn">ВОЙТИ</button>
    </div>
  </div>
</div>

<!-- PANEL -->
<div id="panel-view">
  <header class="topbar">
    <div class="topbar-l">
      <div class="topbar-logo">sldchat</div>
      <div class="topbar-tag">admin</div>
    </div>
    <div class="topbar-r">
      <button class="ghost-btn" id="refresh-btn">ОБНОВИТЬ</button>
      <button class="ghost-btn danger" id="logout-btn">ВЫЙТИ</button>
    </div>
  </header>
  <div class="layout">
    <nav class="side">
      <button class="side-tab active" data-tab="dashboard">
        <svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"/></svg>
        Обзор
      </button>
      <button class="side-tab" data-tab="users">
        <svg viewBox="0 0 24 24"><path fill="currentColor" d="M16 11c1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3 1.34 3 3 3zm-8 0c1.66 0 3-1.34 3-3S9.66 5 8 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
        Пользователи
      </button>
      <button class="side-tab" data-tab="channels">
        <svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM9 11H7V9h2v2zm4 0h-2V9h2v2zm4 0h-2V9h2v2z"/></svg>
        Каналы
      </button>
      <button class="side-tab" data-tab="messages">
        <svg viewBox="0 0 24 24"><path fill="currentColor" d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM6 9h12v2H6V9zm8 5H6v-2h8v2zm4-6H6V6h12v2z"/></svg>
        Сообщения
      </button>
    </nav>
    <main class="main" id="main"></main>
  </div>
</div>

<div id="toasts"></div>

<script>
const $ = id => document.getElementById(id);
const state = { tab: 'dashboard', cache: {} };

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
function fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function toast(msg, kind = 'ok') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (opts.body && typeof opts.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, Object.assign({}, opts, { headers, credentials: 'same-origin' }));
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    const err = new Error(detail); err.status = r.status; throw err;
  }
  if (r.status === 204) return null;
  const text = await r.text();
  return text ? JSON.parse(text) : null;
}

/* -------- LOGIN -------- */
async function trySession() {
  try {
    await api('/api/admin/session');
    showPanel();
    return true;
  } catch (e) {
    return false;
  }
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
    $('admin-pass').value = '';
    showPanel();
  } catch (e) {
    $('login-err').textContent = e.message || 'Ошибка';
  } finally {
    $('login-btn').disabled = false;
  }
}

$('logout-btn').addEventListener('click', async () => {
  try { await api('/api/admin/logout', { method: 'POST' }); } catch (e) {}
  showLogin();
});
$('refresh-btn').addEventListener('click', () => {
  delete state.cache[state.tab];
  renderTab();
});

/* -------- TABS -------- */
document.querySelectorAll('.side-tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.side-tab').forEach(x =>
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

/* -------- DASHBOARD -------- */
async function renderDashboard(main) {
  main.innerHTML = `<div class="loading">Загружаю статистику</div>`;
  try {
    const s = await api('/api/admin/stats');
    const uptime = Math.floor(Date.now()/1000 - s.uptime_started);
    const h = Math.floor(uptime/3600), m = Math.floor((uptime%3600)/60), sec = uptime%60;
    main.innerHTML = `
      <div class="page-title">Обзор сервера</div>
      <div class="page-sub">Живая сводка по состоянию sldchat</div>
      <div class="stats">
        <div class="stat"><div class="stat-value">${s.users}</div><div class="stat-label">Пользователей</div></div>
        <div class="stat"><div class="stat-value">${s.channels}</div><div class="stat-label">Каналов</div></div>
        <div class="stat"><div class="stat-value">${s.messages}</div><div class="stat-label">Сообщений</div></div>
        <div class="stat"><div class="stat-value">${s.online}</div><div class="stat-label">Онлайн</div></div>
      </div>
      <div class="stats" style="margin-top:14px;">
        <div class="stat"><div class="stat-value">${s.tokens}</div><div class="stat-label">Активных токенов</div></div>
        <div class="stat"><div class="stat-value" style="font-size:22px;">${h}ч ${m}м ${sec}с</div><div class="stat-label">Uptime</div></div>
      </div>
    `;
  } catch (e) {
    if (e.status === 401) { showLogin(); return; }
    main.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
}

/* -------- USERS -------- */
async function renderUsers(main) {
  main.innerHTML = `<div class="loading">Загружаю пользователей</div>`;
  try {
    const data = await api('/api/admin/users');
    const rows = data.users.map(u => `
      <tr>
        <td class="mono accent">${escapeHtml(u.username)}</td>
        <td>${escapeHtml(u.display_name)}</td>
        <td class="muted mono">${fmtTs(u.created)}</td>
        <td>${u.tokens ? `<span class="chip">${u.tokens}</span>` : `<span class="chip muted">0</span>`}</td>
        <td>${u.messages}</td>
        <td>${u.online_in.length ? u.online_in.map(c => `<span class="chip">#${escapeHtml(c)}</span>`).join(' ') : `<span class="chip muted">offline</span>`}</td>
        <td><div class="actions"><button class="btn-mini danger" data-del="${escapeHtml(u.username)}">УДАЛИТЬ</button></div></td>
      </tr>
    `).join('');
    main.innerHTML = `
      <div class="page-title">Пользователи</div>
      <div class="page-sub">Всего: ${data.users.length}. Удаление выкидывает из чата и стирает сессии.</div>
      <div class="table-wrap">
        ${data.users.length ? `<table>
          <thead><tr><th>Юзернейм</th><th>Имя</th><th>Создан</th><th>Токены</th><th>Сообщений</th><th>В каналах</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>` : `<div class="empty-state">Нет зарегистрированных пользователей</div>`}
      </div>
    `;
    main.querySelectorAll('[data-del]').forEach(b => {
      b.addEventListener('click', () => deleteUser(b.dataset.del));
    });
  } catch (e) {
    if (e.status === 401) { showLogin(); return; }
    main.innerHTML = `<div class="empty-state">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
}

async function deleteUser(username) {
  if (!confirm(`Удалить пользователя "${username}"?\n\nЭто выкинет его из чата и удалит все его сессии.`)) return;
  try {
    const r = await api('/api/admin/users/' + encodeURIComponent(username), { method: 'DELETE' });
    toast(`Пользователь удалён (кик: ${r.kicked})`, 'ok');
    renderTab();
  } catch (e) { toast(e.message, 'err'); }
}

/* -------- CHANNELS -------- */
async function renderChannels(main) {
  main.innerHTML = `<div class="loading">Загружаю каналы</div>`;
  try {
    const data = await api('/api/admin/channels');
    const rows = data.channels.map(c => `
      <tr>
        <td class="mono">#${escapeHtml(c.id)}</td>
        <td>${escapeHtml(c.name)}</td>
        <td class="muted mono">${escapeHtml(c.owner)}</td>
        <td class="muted mono">${fmtTs(c.created)}</td>
        <td>${c.messages}</td>
        <td>${c.online ? `<span class="chip">${c.online} online</span>` : `<span class="chip muted">0</span>`}</td>
        <td>
          <div class="actions">
            <button class="btn-mini" data-clear="${escapeHtml(c.id)}">ОЧИСТИТЬ</button>
            <button class="btn-mini danger" data-del="${escapeHtml(c.id)}">УДАЛИТЬ</button>
          </div>
        </td>
      </tr>
    `).join('');
    main.innerHTML = `
      <div class="page-title">Каналы</div>
      <div class="page-sub">Создание, очистка и удаление каналов. Изменения видны всем клиентам мгновенно.</div>
      <div class="toolbar">
        <input id="new-ch" placeholder="имя нового канала" maxlength="32">
        <button id="new-ch-btn">СОЗДАТЬ КАНАЛ</button>
      </div>
      <div class="table-wrap">
        ${data.channels.length ? `<table>
          <thead><tr><th>ID</th><th>Название</th><th>Владелец</th><th>Создан</th><th>Сообщений</th><th>Онлайн</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>` : `<div class="empty-state">Нет каналов</div>`}
      </div>
    `;
    main.querySelectorAll('[data-del]').forEach(b => {
      b.addEventListener('click', () => deleteChannel(b.dataset.del));
    });
    main.querySelectorAll('[data-clear]').forEach(b => {
      b.addEventListener('click', () => clearChannel(b.dataset.clear));
    });
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
    toast('Канал создан', 'ok');
    renderTab();
  } catch (e) { toast(e.message, 'err'); }
}

async function deleteChannel(cid) {
  if (!confirm(`Удалить канал #${cid} и всю его историю?`)) return;
  try {
    await api('/api/admin/channels/' + encodeURIComponent(cid), { method: 'DELETE' });
    toast(`Канал #${cid} удалён`, 'ok');
    renderTab();
  } catch (e) { toast(e.message, 'err'); }
}

async function clearChannel(cid) {
  if (!confirm(`Очистить историю #${cid}?`)) return;
  try {
    await api('/api/admin/channels/' + encodeURIComponent(cid) + '/messages', { method: 'DELETE' });
    toast(`История #${cid} очищена`, 'ok');
    renderTab();
  } catch (e) { toast(e.message, 'err'); }
}

/* -------- MESSAGES -------- */
async function renderMessages(main) {
  main.innerHTML = `<div class="loading">Загружаю сообщения</div>`;
  try {
    const channelsList = (state.cache.channels = state.cache.channels ||
      (await api('/api/admin/channels')).channels);

    const options = [`<option value="">— все каналы —</option>`]
      .concat(channelsList.map(c => `<option value="${escapeHtml(c.id)}">#${escapeHtml(c.name)}</option>`))
      .join('');

    main.innerHTML = `
      <div class="page-title">Сообщения</div>
      <div class="page-sub">Просмотр и удаление отдельных сообщений.</div>
      <div class="toolbar">
        <select id="flt-channel">${options}</select>
        <input id="flt-user" class="mono" placeholder="автор (юзернейм)">
        <input id="flt-search" class="grow" placeholder="поиск по тексту...">
        <button id="flt-apply">ПРИМЕНИТЬ</button>
      </div>
      <div class="table-wrap" id="msgs-wrap"><div class="loading">Загружаю</div></div>
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
      wrap.innerHTML = `<div class="loading">Загружаю</div>`;
      try {
        const data = await api('/api/admin/messages?' + params.toString());
        const rows = data.messages.map(m => `
          <tr>
            <td class="muted mono">${fmtTs(m.ts)}</td>
            <td><span class="chip">#${escapeHtml(m.channel)}</span></td>
            <td class="mono accent">${escapeHtml(m.user)}</td>
            <td><div class="msg-text">${escapeHtml(m.text)}</div></td>
            <td><button class="btn-mini danger" data-mdel="${escapeHtml(m.id)}">X</button></td>
          </tr>
        `).join('');
        wrap.innerHTML = data.messages.length ? `
          <table>
            <thead><tr><th>Время</th><th>Канал</th><th>Автор</th><th>Текст</th><th></th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        ` : `<div class="empty-state">Ничего не найдено</div>`;
        wrap.querySelectorAll('[data-mdel]').forEach(b => {
          b.addEventListener('click', () => deleteMessage(b.dataset.mdel));
        });
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

async function deleteMessage(id) {
  try {
    await api('/api/admin/messages/' + encodeURIComponent(id), { method: 'DELETE' });
    toast('Сообщение удалено', 'ok');
    renderTab();
  } catch (e) { toast(e.message, 'err'); }
}

/* -------- BOOT -------- */
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
_STARTED_AT = time.time()


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    if not ADMIN_PASS:
        return HTMLResponse(
            "<h1 style='font-family:monospace;color:#f87171;padding:40px'>"
            "Админка отключена: не задан ADMIN_PASS</h1>",
            status_code=503,
        )
    return ADMIN_PAGE


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": time.time()}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
