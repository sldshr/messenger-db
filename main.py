"""
SldChat — простой мессенджер на Python + FastAPI + WebSocket.
Запуск:  python main.py
"""

import asyncio
import json
import secrets
import time
from typing import Dict, List, Optional, Set

import uvicorn
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

app = FastAPI(title="SldChat")

START_TIME = time.time()

# ================== ХРАНИЛИЩЕ ==================
users: Dict[str, dict] = {}
sessions: Dict[str, str] = {}
contacts: Dict[str, Set[str]] = {}
messages: List[dict] = []
reads: Dict[str, Dict[str, int]] = {}
active_ws: Dict[str, Set[WebSocket]] = {}
_msg_id = 0


def current_user(request: Request) -> Optional[str]:
    token = request.cookies.get("session")
    return sessions.get(token) if token else None


def is_online(nick: str) -> bool:
    return bool(active_ws.get(nick))


def user_public_info(nick: str) -> dict:
    u = users.get(nick)
    if not u:
        return {}
    return {"nick": nick, "online": is_online(nick),
            "last_seen": u.get("last_seen", 0), "created": u.get("created", 0)}


async def send_ws(nick: str, payload: dict):
    socks = list(active_ws.get(nick, ()))
    if not socks:
        return
    text = json.dumps(payload, ensure_ascii=False)
    await asyncio.gather(*(_safe_send(ws, text) for ws in socks), return_exceptions=True)


async def _safe_send(ws: WebSocket, text: str):
    try:
        await ws.send_text(text)
    except Exception:
        pass


async def notify_contacts(nick: str, payload: dict):
    for c in contacts.get(nick, set()):
        await send_ws(c, payload)


def err(key: str, status: int = 400):
    """Единый формат ошибки. error_key — для клиентской локализации."""
    return JSONResponse({"ok": False, "error_key": key}, status_code=status)


# ================== БЕЗОПАСНОСТЬ ==================
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    nick = current_user(request)
    if nick and nick in users:
        users[nick]["last_seen"] = time.time()
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "interest-cohort=()"
    if request.url.path in ("/", "/chat", "/login", "/register", "/privacy", "/support"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ================== WEBSOCKET ==================
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    token = websocket.cookies.get("session")
    nick = sessions.get(token) if token else None
    if not nick or nick not in users:
        await websocket.close(code=1008)
        return
    users[nick]["last_seen"] = time.time()
    active_ws.setdefault(nick, set()).add(websocket)
    await notify_contacts(nick, {"type": "presence", "nick": nick, "online": True})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        socks = active_ws.get(nick)
        if socks:
            socks.discard(websocket)
            if not socks:
                active_ws.pop(nick, None)
                users[nick]["last_seen"] = time.time()
                await notify_contacts(nick, {"type": "presence", "nick": nick, "online": False})


# ================== API ==================
@app.post("/api/register")
async def api_register(request: Request, nick: str = Form(...), password: str = Form(...)):
    nick = nick.strip()
    if not nick or not password:
        return err("fill_all")
    if not (3 <= len(nick) <= 20):
        return err("nick_len")
    if not nick.replace("_", "").isalnum():
        return err("nick_chars")
    if len(password) < 3:
        return err("pass_len")
    if nick in users:
        return err("nick_taken")
    now = time.time()
    users[nick] = {"password": password, "created": now, "last_seen": now}
    contacts[nick] = set()
    token = secrets.token_hex(32)
    sessions[token] = nick
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", token, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https",
                    max_age=60 * 60 * 24 * 30, path="/")
    return resp


@app.post("/api/login")
async def api_login(request: Request, nick: str = Form(...), password: str = Form(...)):
    nick = nick.strip()
    u = users.get(nick)
    if not u or u["password"] != password:
        return err("bad_login")
    token = secrets.token_hex(32)
    sessions[token] = nick
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", token, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https",
                    max_age=60 * 60 * 24 * 30, path="/")
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    token = request.cookies.get("session")
    if token:
        sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session", path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    nick = current_user(request)
    if not nick:
        return JSONResponse({"ok": False}, status_code=401)
    return {"ok": True, "nick": nick}


@app.get("/api/server-info")
async def api_server_info():
    return {"ok": True, "started": START_TIME}


@app.post("/api/contacts/add")
async def api_contacts_add(request: Request, nick: str = Form(...)):
    me = current_user(request)
    if not me:
        return err("not_authorized", 401)
    nick = nick.strip()
    if not nick:
        return err("enter_nick")
    if nick == me:
        return err("cant_add_self")
    if nick not in users:
        return err("user_not_found", 404)
    contacts.setdefault(me, set())
    contacts.setdefault(nick, set())
    if nick in contacts[me]:
        return err("already_contact")
    contacts[me].add(nick)
    contacts[nick].add(me)
    info = user_public_info(nick)
    await asyncio.gather(
        send_ws(nick, {"type": "contact_added", "nick": me}),
        send_ws(me, {"type": "contact_added", "nick": nick}),
    )
    return {"ok": True, "contact": {"nick": nick, "last": None, "unread": 0,
                                    "online": info["online"], "last_seen": info["last_seen"]}}


@app.post("/api/contacts/remove")
async def api_contacts_remove(request: Request, nick: str = Form(...)):
    me = current_user(request)
    if not me:
        return err("not_authorized", 401)
    nick = nick.strip()
    if nick not in contacts.get(me, set()):
        return err("not_in_contacts")
    contacts.get(me, set()).discard(nick)
    contacts.get(nick, set()).discard(me)
    await asyncio.gather(
        send_ws(nick, {"type": "contact_removed", "nick": me}),
        send_ws(me, {"type": "contact_removed", "nick": nick}),
    )
    return {"ok": True}


@app.get("/api/contacts")
async def api_contacts(request: Request):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)
    my_reads = reads.get(me, {})
    out = []
    for nick in contacts.get(me, set()):
        if nick not in users:
            continue
        last_msg = None
        unread = 0
        last_read_id = my_reads.get(nick, 0)
        for m in messages:
            if (m["from"] == nick and m["to"] == me) or (m["from"] == me and m["to"] == nick):
                last_msg = m
                if m["from"] == nick and m["to"] == me and m["id"] > last_read_id:
                    unread += 1
        out.append({"nick": nick, "last": last_msg, "unread": unread,
                    "online": is_online(nick), "last_seen": users[nick].get("last_seen", 0)})
    out.sort(key=lambda u: (u["last"]["id"] if u["last"] else 0), reverse=True)
    return {"ok": True, "contacts": out}


@app.get("/api/user/{nick}/info")
async def api_user_info(nick: str, request: Request):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)
    if nick not in users:
        return err("user_not_found", 404)
    info = user_public_info(nick)
    info["msg_count"] = sum(1 for m in messages if (
        (m["from"] == me and m["to"] == nick) or (m["from"] == nick and m["to"] == me)
    ))
    info["in_contacts"] = nick in contacts.get(me, set())
    return {"ok": True, "info": info}


@app.get("/api/dialog/{nick}")
async def api_dialog(nick: str, request: Request, since: int = 0):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)
    if nick not in contacts.get(me, set()):
        return err("not_in_contacts", 403)
    max_id = reads.setdefault(me, {}).get(nick, 0)
    for m in messages:
        if m["from"] == nick and m["to"] == me and m["id"] > max_id:
            max_id = m["id"]
    reads[me][nick] = max_id
    out = [m for m in messages if m["id"] > since and (
        (m["from"] == me and m["to"] == nick) or (m["from"] == nick and m["to"] == me)
    )]
    return {"ok": True, "messages": out}


@app.post("/api/send")
async def api_send(request: Request, to: str = Form(...), text: str = Form(...)):
    global _msg_id
    me = current_user(request)
    if not me:
        return err("not_authorized", 401)
    to = to.strip()
    text = text.strip()
    if not text:
        return err("empty_msg")
    if len(text) > 4000:
        text = text[:4000]
    if to not in contacts.get(me, set()):
        return err("not_in_contacts", 403)
    _msg_id += 1
    msg = {"id": _msg_id, "from": me, "to": to, "text": text, "time": time.time()}
    messages.append(msg)
    payload = {"type": "message", "message": msg}
    await asyncio.gather(send_ws(to, payload), send_ws(me, payload))
    return {"ok": True, "message": msg}


# ================== ICONS ==================
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#3f6fa8"/>'
    '<path d="M50 28a13 13 0 0 1-14.7 12.8l-8.1 4.8v-6A13 13 0 0 1 14 28a13 13 0 0 1 13-12.8h10A13 13 0 0 1 50 28z"'
    ' fill="none" stroke="#fff" stroke-width="3.6" stroke-linejoin="round"/>'
    '</svg>'
)

OG_IMAGE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630" width="1200" height="630">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#d5dfe9"/><stop offset="1" stop-color="#a8b6c4"/>'
    '</linearGradient></defs>'
    '<rect width="1200" height="630" fill="url(#g)"/>'
    '<g font-family="Tahoma, Arial, sans-serif" text-anchor="middle">'
    '<text x="600" y="290" font-size="104" font-weight="bold" fill="#3f6fa8">SldChat</text>'
    '<text x="600" y="380" font-size="36" fill="#2b3a4a">Fast messenger for teams</text>'
    '<text x="600" y="446" font-size="22" fill="#5a6c80">WebSocket · add by nick · no ads</text>'
    '</g></svg>'
)


@app.get("/favicon.svg")
async def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
async def favicon_ico():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/og-image.svg")
async def og_image():
    return Response(content=OG_IMAGE_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# ================== SHARED CSS ==================
SHARED_CSS = """
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: Tahoma, Verdana, Arial, sans-serif;
    font-size: 14px; color: #2b3a4a; line-height: 1.6;
    background: #eef2f6;
    -webkit-user-select: none; -moz-user-select: none; user-select: none;
    -webkit-font-smoothing: antialiased;
  }
  input, textarea, .selectable { -webkit-user-select: text; -moz-user-select: text; user-select: text; }
  * { scrollbar-width: none; -ms-overflow-style: none; }
  *::-webkit-scrollbar { width: 0; height: 0; display: none; }

  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
  @keyframes popIn { 0% { opacity: 0; transform: scale(0.94); } 60% { opacity: 1; transform: scale(1.02); } 100% { opacity: 1; transform: scale(1); } }
  @keyframes dotPulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(76,175,80,0.5); }
    50%      { box-shadow: 0 0 0 4px rgba(76,175,80,0); }
  }

  a { color: #3f6fa8; text-decoration: none; transition: color 0.14s ease; }
  a:hover { text-decoration: underline; }
  .container { max-width: 1080px; margin: 0 auto; padding: 0 22px; }

  .icon {
    width: 16px; height: 16px; flex-shrink: 0;
    stroke: currentColor; fill: none;
    stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
    vertical-align: -3px;
  }
  .icon.lg  { width: 22px; height: 22px; }
  .icon.xl  { width: 28px; height: 28px; stroke-width: 1.7; }
  .icon.huge{ width: 42px; height: 42px; stroke-width: 1.5; }

  .topbar {
    background: linear-gradient(#fbfcfd, #dce3ea);
    border-bottom: 1px solid #b7c2cd;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    position: sticky; top: 0; z-index: 50;
  }
  .topbar-inner { display: flex; align-items: center; gap: 12px; height: 56px; }
  .logo {
    display: flex; align-items: center; gap: 7px;
    font-size: 21px; font-weight: bold; color: #2b3a4a;
    text-shadow: 0 1px 0 #fff; letter-spacing: 0.5px;
    transition: transform 0.15s ease;
  }
  .logo:hover { transform: translateY(-1px); text-decoration: none; }
  .logo span { color: #3f6fa8; }
  .logo .icon { color: #3f6fa8; width: 22px; height: 22px; }

  .nav { display: flex; align-items: center; gap: 4px; margin-left: auto; }
  .nav a.navlink {
    padding: 7px 10px; border-radius: 4px; color: #3a5169; font-size: 13px;
    transition: background 0.14s ease, transform 0.14s ease;
  }
  .nav a.navlink:hover { background: #e3e9ef; text-decoration: none; transform: translateY(-1px); }

  .btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 14px; border-radius: 4px;
    border: 1px solid #8b97a3; font-size: 13px; cursor: pointer;
    font-family: inherit; text-shadow: 0 1px 0 rgba(255,255,255,0.6);
    text-decoration: none !important; color: #2b3a4a;
    background: linear-gradient(#fbfcfd, #cfd8e0);
    white-space: nowrap;
    transition: transform 0.1s ease, background 0.15s ease, box-shadow 0.15s ease;
  }
  .btn:hover { background: linear-gradient(#fff, #dbe3ea); transform: translateY(-1px); box-shadow: 0 2px 5px rgba(0,0,0,0.08); }
  .btn:active { transform: translateY(0) scale(0.98); box-shadow: inset 0 1px 2px rgba(0,0,0,0.15); }
  .btn-primary {
    background: linear-gradient(#5b8fc4, #3f6fa8);
    border-color: #35597f; color: #fff;
    text-shadow: 0 1px 0 rgba(0,0,0,0.2);
  }
  .btn-primary:hover { background: linear-gradient(#699bcd, #4577b1); box-shadow: 0 2px 8px rgba(63,111,168,0.35); }
  .btn-lg { padding: 11px 22px; font-size: 15px; }

  .section { padding: 64px 0; border-bottom: 1px solid #dbe1e7; }
  .section:nth-of-type(even) { background: #f6f8fa; }
  .section h2 { font-size: 27px; font-weight: normal; margin: 0 0 8px; color: #23374b; text-shadow: 0 1px 0 #fff; }
  .section .lead { color: #6f7c8b; font-size: 14px; margin: 0 0 30px; }
  .section p { margin: 0 0 12px; color: #47586c; }

  .cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; margin-top: 10px; }
  .card {
    background: #fff; border: 1px solid #cfd7df; border-radius: 6px;
    padding: 22px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05), inset 0 1px 0 #fff;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }
  .card:hover { transform: translateY(-3px); box-shadow: 0 8px 20px rgba(0,0,0,0.10), inset 0 1px 0 #fff; }
  .card .ico {
    width: 46px; height: 46px; margin-bottom: 14px;
    border-radius: 8px;
    background: linear-gradient(#e4ebf2, #c8d3de);
    border: 1px solid #b7c2cd;
    display: flex; align-items: center; justify-content: center;
    color: #3f6fa8; box-shadow: inset 0 1px 0 #fff;
    transition: transform 0.2s ease;
  }
  .card:hover .ico { transform: scale(1.06) rotate(-2deg); }
  .card h3 { margin: 0 0 6px; font-size: 15px; color: #2b3a4a; }
  .card p  { margin: 0; font-size: 13px; color: #5a6c80; }

  .faq { max-width: 760px; margin: 0 auto; }
  .faq details {
    background: #fff; border: 1px solid #cfd7df; border-radius: 5px;
    margin-bottom: 10px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
    overflow: hidden;
  }
  .faq details[open] { border-color: #a8b8ca; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
  .faq summary {
    padding: 13px 18px; cursor: pointer; font-weight: bold; color: #2b3a4a;
    outline: none; list-style: none;
    display: flex; align-items: center; gap: 10px;
    transition: background 0.15s ease;
  }
  .faq summary::-webkit-details-marker { display: none; }
  .faq summary:hover { background: #f6f9fc; }
  .faq summary::before {
    content: '+'; display: inline-block;
    color: #3f6fa8; font-weight: bold; font-size: 16px;
    width: 12px; text-align: center; transition: transform 0.25s ease;
  }
  .faq details[open] summary::before { content: '−'; transform: rotate(180deg); }
  .faq .answer-wrap { display: grid; grid-template-rows: 0fr; transition: grid-template-rows 0.28s ease; }
  .faq details[open] .answer-wrap { grid-template-rows: 1fr; }
  .faq .answer-wrap > .answer {
    overflow: hidden;
    padding: 0 18px 0 40px; color: #55677b; font-size: 13px;
    transition: padding 0.28s ease;
  }
  .faq details[open] .answer-wrap > .answer { padding: 0 18px 15px 40px; }

  .dd { position: relative; display: inline-block; }
  .dd-toggle {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 7px 12px; border-radius: 4px;
    border: 1px solid #4d5f72; cursor: pointer;
    background: rgba(255,255,255,0.06);
    font-family: inherit; font-size: 13px; color: #dbe5ef;
    transition: background 0.15s ease, border-color 0.15s ease;
    white-space: nowrap;
  }
  .dd-toggle:hover { background: rgba(255,255,255,0.12); border-color: #6b7f93; }
  .dd-toggle .icon { width: 14px; height: 14px; color: #9db8d3; transition: transform 0.22s ease; }
  .dd.open .dd-toggle .icon.chev { transform: rotate(180deg); }
  .dd-toggle .dd-label { color: #8fa3b6; }
  .dd-toggle .dd-value { color: #fff; font-weight: bold; }

  .dd-menu {
    position: absolute; bottom: calc(100% + 6px); right: 0; left: auto;
    min-width: 170px;
    background: #fff; color: #2b3a4a;
    border: 1px solid #b7c2cd; border-radius: 6px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.28);
    padding: 5px; margin: 0; list-style: none;
    opacity: 0; visibility: hidden;
    transform: translateY(6px) scale(0.97);
    transform-origin: bottom right;
    transition: opacity 0.15s ease, transform 0.15s ease, visibility 0.15s;
    z-index: 200;
  }
  .dd.open .dd-menu { opacity: 1; visibility: visible; transform: translateY(0) scale(1); }
  .dd-menu li {
    padding: 8px 12px; border-radius: 4px; cursor: pointer;
    font-size: 13px; color: #2b3a4a;
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    transition: background 0.1s ease;
  }
  .dd-menu li:hover { background: #eef2f6; }
  .dd-menu li.active { background: #e4ebf3; font-weight: bold; }
  .dd-menu li.active::after { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #3f6fa8; }

  footer { background: #2b3a4a; color: #b8c4ce; padding: 44px 0 26px; font-size: 13px; }
  footer .foot-cols { display: grid; grid-template-columns: 1.4fr 1fr 1fr 1fr; gap: 30px; margin-bottom: 30px; }
  footer .foot-brand .brand-name {
    display: flex; align-items: center; gap: 8px;
    font-size: 17px; color: #fff; font-weight: bold; margin-bottom: 10px;
  }
  footer .foot-brand .brand-name span { color: #9db8d3; }
  footer .foot-brand .brand-name .icon { color: #9db8d3; width: 22px; height: 22px; }
  footer .foot-brand p { margin: 0; color: #8fa3b6; font-size: 12px; line-height: 1.6; max-width: 280px; }
  footer .foot-col h4 {
    margin: 0 0 12px; font-size: 12px; text-transform: uppercase;
    letter-spacing: 1px; color: #8fa3b6; font-weight: bold;
  }
  footer .foot-col ul { list-style: none; padding: 0; margin: 0; }
  footer .foot-col li { margin-bottom: 7px; }
  footer .foot-col a { color: #b8c4ce; font-size: 13px; transition: color 0.14s ease; }
  footer .foot-col a:hover { color: #fff; }
  footer .foot-controls {
    display: flex; align-items: center; justify-content: space-between;
    gap: 16px; flex-wrap: wrap;
    padding: 16px 0;
    border-top: 1px solid #3d4d5d;
    border-bottom: 1px solid #3d4d5d;
  }
  footer .foot-status {
    display: flex; align-items: center; gap: 20px; flex-wrap: wrap;
    font-size: 12px; color: #8fa3b6;
  }
  footer .foot-status b { color: #b8c4ce; font-weight: normal; }
  footer .foot-status code {
    font-family: Consolas, "Courier New", monospace;
    background: #1f2c39; padding: 2px 8px; border-radius: 3px;
    color: #cfe0f0; font-size: 11px;
  }
  footer .foot-status .dot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: #4caf50; margin-right: 6px;
    animation: dotPulse 2s ease-in-out infinite;
    vertical-align: 1px;
  }
  footer .foot-bottom {
    padding-top: 20px;
    display: flex; align-items: center; justify-content: space-between;
    gap: 14px; flex-wrap: wrap;
    font-size: 12px; color: #7a8b9c;
  }
  footer .foot-bottom .legal { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  footer .foot-bottom a { color: #9db8d3; }

  .page-wrap { max-width: 820px; margin: 0 auto; padding: 40px 20px 60px; }
  .page-head { margin-bottom: 28px; }
  .page-head h1 { font-size: 32px; font-weight: normal; color: #23374b; margin: 0 0 6px; text-shadow: 0 1px 0 #fff; }
  .page-head p { margin: 0; color: #6f7c8b; font-size: 14px; }
  .page-card {
    background: #fff; border: 1px solid #cfd7df; border-radius: 6px;
    padding: 26px 28px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05), inset 0 1px 0 #fff;
    margin-bottom: 16px;
  }
  .page-card h2 { font-size: 18px; margin: 0 0 12px; color: #23374b; font-weight: bold; }
  .page-card p, .page-card li { color: #47586c; margin: 0 0 10px; }
  .page-card ul { padding-left: 22px; margin: 0 0 10px; }
  .page-card li { margin: 0 0 6px; }
  .page-card b { color: #23374b; }
  .page-card code {
    background: #eef2f6; padding: 1px 6px; border-radius: 3px;
    font-family: Consolas, "Courier New", monospace; font-size: 12px;
    color: #2b3a4a;
  }
  .link-arrow {
    display: inline-flex; align-items: center; gap: 6px;
    color: #3f6fa8; font-weight: bold; font-size: 13px;
    margin-top: 6px; transition: gap 0.15s ease;
  }
  .link-arrow:hover { gap: 10px; text-decoration: none; }

  @media (max-width: 820px) {
    .nav a.navlink { display: none; }
    .topbar-inner { gap: 8px; }
    .cards { grid-template-columns: 1fr; }
    .section { padding: 42px 0; }
    .section h2 { font-size: 22px; }
    .page-head h1 { font-size: 24px; }
    .page-card { padding: 20px 18px; }
    footer .foot-cols { grid-template-columns: 1fr 1fr; gap: 22px; }
    footer .foot-brand { grid-column: 1 / -1; }
    footer .foot-controls { flex-direction: column; align-items: flex-start; gap: 12px; }
    footer .foot-status { gap: 14px; }
    .dd-menu { left: 0; right: auto; transform-origin: bottom left; }
  }
"""


# ================== FOOTER ==================
FOOTER_HTML = """
<footer>
  <div class="container">
    <div class="foot-cols">
      <div class="foot-brand">
        <div class="brand-name">
          <svg class="icon" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
          Sld<span>Chat</span>
        </div>
        <p data-i18n="foot_brand">SldChat — независимый мессенджер от SldChat Team.</p>
      </div>
      <div class="foot-col">
        <h4 data-i18n="foot_product">Продукт</h4>
        <ul>
          <li><a href="/#features" data-i18n="nav_features">Возможности</a></li>
          <li><a href="/#servers" data-i18n="nav_servers">Серверы</a></li>
          <li><a href="/register" data-i18n="btn_register">Регистрация</a></li>
          <li><a href="/login" data-i18n="btn_login">Вход</a></li>
        </ul>
      </div>
      <div class="foot-col">
        <h4 data-i18n="foot_company">Компания</h4>
        <ul>
          <li><a href="/privacy" data-i18n="foot_privacy">Приватность</a></li>
          <li><a href="/support" data-i18n="foot_support">Поддержка</a></li>
          <li><a href="/#faq" data-i18n="foot_faq">FAQ</a></li>
        </ul>
      </div>
      <div class="foot-col">
        <h4 data-i18n="foot_contact">Связь</h4>
        <ul>
          <li><a href="mailto:sldshr.confirmation@gmail.com" data-i18n="foot_mail">Почта</a></li>
          <li><a href="https://discord.com/users/sldshr" target="_blank" rel="noopener">Discord: sldshr</a></li>
        </ul>
      </div>
    </div>

    <div class="foot-controls">
      <div class="foot-status">
        <span><span class="dot"></span><b data-i18n="foot_status">Сервер:</b> <code id="srvHost">—</code></span>
        <span><b data-i18n="foot_uptime">Аптайм:</b> <code id="srvUptime">—</code></span>
      </div>

      <div class="dd dd-up" id="footLangDd">
        <button class="dd-toggle" type="button" aria-haspopup="listbox">
          <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
          <span class="dd-label" data-i18n="lang_short">Язык:</span>
          <span class="dd-value" id="footLangValue">Русский</span>
          <svg class="icon chev" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <ul class="dd-menu" id="footLangMenu">
          <li data-value="ru">Русский</li>
          <li data-value="en">English</li>
        </ul>
      </div>
    </div>

    <div class="foot-bottom">
      <div data-i18n="foot_copy">© 2026 SldChat Team. Все права защищены.</div>
      <div class="legal">
        <a href="/privacy" data-i18n="foot_privacy">Приватность</a>
        <a href="/support" data-i18n="foot_support">Поддержка</a>
      </div>
    </div>
  </div>
</footer>

<script>
(function(){
  function fmtUp(sec){
    sec = Math.floor(sec);
    var u = window.__sldUnits || {d:'d',h:'h',m:'m',s:'s'};
    var d = Math.floor(sec/86400); sec %= 86400;
    var h = Math.floor(sec/3600);  sec %= 3600;
    var m = Math.floor(sec/60);    sec %= 60;
    var out = '';
    if (d) out += d + u.d + ' ';
    if (d || h) out += h + u.h + ' ';
    if (d || h || m) out += m + u.m + ' ';
    out += sec + u.s;
    return out;
  }
  var started = null;
  function tick(){
    var el = document.getElementById('srvUptime');
    if (!el || started === null) return;
    el.textContent = fmtUp((Date.now()/1000) - started);
  }
  var hostEl = document.getElementById('srvHost');
  if (hostEl) hostEl.textContent = location.host;
  fetch('/api/server-info').then(function(r){return r.json();}).then(function(d){
    if (d && d.ok) { started = d.started; tick(); setInterval(tick, 1000); }
  }).catch(function(){});

  function softenUrls(){
    document.querySelectorAll('.server-card .url').forEach(function(el){
      if (el.dataset.wbr === '1') return;
      el.dataset.wbr = '1';
      var parts = el.textContent.split('.');
      el.innerHTML = parts.map(function(p, i){
        return i < parts.length - 1 ? p + '.<wbr>' : p;
      }).join('');
    });
  }

  function wireDropdown(root, onSelect){
    if (!root) return;
    var toggle = root.querySelector('.dd-toggle');
    var menu = root.querySelector('.dd-menu');
    var valueEl = root.querySelector('.dd-value');
    if (!toggle || !menu) return;
    toggle.addEventListener('click', function(e){
      e.stopPropagation();
      document.querySelectorAll('.dd.open').forEach(function(o){ if (o !== root) o.classList.remove('open'); });
      root.classList.toggle('open');
    });
    menu.querySelectorAll('li').forEach(function(li){
      li.addEventListener('click', function(){
        var v = li.dataset.value;
        root.classList.remove('open');
        if (valueEl) valueEl.textContent = li.textContent.trim();
        if (onSelect) onSelect(v);
      });
    });
    document.addEventListener('click', function(){ root.classList.remove('open'); });
  }

  function markActive(root, val){
    if (!root) return;
    var menu = root.querySelector('.dd-menu');
    var valueEl = root.querySelector('.dd-value');
    if (!menu) return;
    menu.querySelectorAll('li').forEach(function(li){
      if (li.dataset.value === val) {
        li.classList.add('active');
        if (valueEl) valueEl.textContent = li.textContent.trim();
      } else {
        li.classList.remove('active');
      }
    });
  }

  window.SldFooter = {
    wire: wireDropdown,
    markActive: markActive,
    softenUrls: softenUrls,
    tick: tick
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', softenUrls);
  } else {
    softenUrls();
  }
})();
</script>
"""


HEAD_COMMON = """
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
<link rel="mask-icon" href="/favicon.svg" color="#3f6fa8">
<meta name="theme-color" content="#3f6fa8">
"""


# ================== I18N (единый словарь) ==================
# ВАЖНО: набор ключей в ru и en идентичен — правь оба при добавлении.
I18N_JS = """
var I18N = {
  ru: {
    /* ==== Common / nav ==== */
    nav_features: "Возможности", nav_servers: "Серверы", nav_privacy: "Приватность",
    nav_faq: "FAQ", nav_support: "Поддержка",
    btn_login: "Войти", btn_register: "Регистрация",
    lang_short: "Язык:",

    /* ==== Landing: hero ==== */
    hero_eyebrow: "Быстрая доставка через WebSocket",
    hero_title: "Мессенджер <b>SldChat</b> —<br>общайтесь по-простому",
    hero_sub: "Никаких лишних настроек. Регистрация за 5 секунд, добавление по нику, мгновенная доставка сообщений. Работает на телефоне и на компьютере.",
    hero_cta1: "Создать аккаунт", hero_cta2: "У меня уже есть аккаунт",
    hero_m1: "Без e-mail и телефона", hero_m2: "Без рекламы", hero_m3: "Бесплатно",

    /* ==== Landing: features ==== */
    feat_h: "Возможности", feat_lead: "Всё, что нужно для быстрого общения — и ничего лишнего.",
    feat_1_h: "Мгновенная доставка", feat_1_p: "Сообщения летят через WebSocket — собеседник видит их за миллисекунды, без перезагрузок.",
    feat_2_h: "Добавление по нику", feat_2_p: "Никаких публичных каталогов. Ввели ник — и вы с человеком сразу друг у друга в контактах.",
    feat_3_h: "Статус «в сети»", feat_3_p: "Видите, кто онлайн прямо сейчас, а кто заходил недавно — без лишних деталей.",
    feat_4_h: "На любом устройстве", feat_4_p: "Один и тот же интерфейс на компьютере и на смартфоне — с удобной адаптацией под экран.",
    feat_5_h: "Поиск по контактам", feat_5_p: "Быстрый поиск среди ваших собеседников прямо в списке — с фильтрацией по мере ввода.",
    feat_6_h: "Приватность по умолчанию", feat_6_p: "Минимум собираемых данных. Одна cookie для сессии, никакой аналитики и трекеров.",

    /* ==== Landing: servers ==== */
    srv_h: "Серверы SldChat",
    srv_lead: "Проект работает на двух независимых серверах. Выбирайте любой — данные между ними не передаются.",
    srv_main: "Основной", srv_alt: "Резерв", srv_unstable: "Unstable",
    srv_notice: "<b>Это разные серверы.</b> У каждого своя база пользователей и сообщений — данные между ними <b>не передаются</b>. Чтобы общаться на двух серверах сразу, зарегистрируйтесь на каждом отдельно.",

    /* ==== Landing: privacy ==== */
    priv_h: "Приватность", priv_lead: "Коротко о самом главном. Подробности — на отдельной странице.",
    priv_1_h: "Ничего не пишем на диск", priv_1_p: "Сообщения и данные аккаунта существуют, пока работает сервер.",
    priv_2_h: "Без аналитики и рекламы", priv_2_p: "Никаких трекеров, пикселей и сторонних скриптов.",
    priv_3_h: "Одна сессионная cookie", priv_3_p: "HttpOnly и SameSite — только для того, чтобы вы остались в аккаунте.",
    priv_4_h: "Минимум данных", priv_4_p: "Только ник и пароль. Ни e-mail, ни телефона, ни IP-логов.",
    priv_more: "Почитать подробнее",

    /* ==== Landing: faq ==== */
    faq_h: "Ответы на вопросы",
    faq_q1: "Сколько стоит SldChat?", faq_a1: "Нисколько. Проект полностью бесплатный, без рекламы и подписок.",
    faq_q2: "Сохраняются ли мои сообщения?", faq_a2: "Нет. Всё живёт в оперативной памяти сервера и исчезает при его перезапуске. На диск ничего не пишется.",
    faq_q3: "Почему я вижу только своих контактов?", faq_a3: "В SldChat нет публичного каталога пользователей. Чтобы начать общение, нажмите «Добавить контакт» и введите ник собеседника.",
    faq_q4: "Как удалить контакт?", faq_a4: "Откройте диалог с человеком и нажмите «Удалить контакт» в панели справа.",
    faq_q5: "Чем серверы отличаются друг от друга?", faq_a5: "Это два независимых развёртывания. У каждого своя база пользователей и сообщений, между собой они не связаны.",
    faq_q6: "Как выйти из аккаунта?", faq_a6: "Кнопка выхода — в шапке приложения, справа от списка контактов.",

    /* ==== Auth ==== */
    auth_welcome: "Добро пожаловать",
    auth_tab_login: "Вход", auth_tab_register: "Регистрация",
    auth_nick: "Ник:", auth_password: "Пароль:",
    auth_login_btn: "Войти", auth_register_btn: "Зарегистрироваться",
    auth_nick_ph: "3–20 символов", auth_pass_ph: "минимум 3 символа",
    auth_hint: "Регистрация занимает меньше минуты",
    auth_back: "← На главную",

    /* ==== Footer ==== */
    foot_brand: "SldChat — независимый мессенджер от SldChat Team.",
    foot_product: "Продукт", foot_company: "Компания", foot_contact: "Связь",
    foot_privacy: "Приватность", foot_support: "Поддержка", foot_faq: "FAQ", foot_mail: "Почта",
    foot_status: "Сервер:", foot_uptime: "Аптайм:", foot_copy: "© 2026 SldChat Team. Все права защищены."
  },
  en: {
    nav_features: "Features", nav_servers: "Servers", nav_privacy: "Privacy",
    nav_faq: "FAQ", nav_support: "Support",
    btn_login: "Sign in", btn_register: "Sign up",
    lang_short: "Language:",

    hero_eyebrow: "Instant delivery via WebSocket",
    hero_title: "Messenger <b>SldChat</b> —<br>just talk, simply",
    hero_sub: "No useless settings. Register in 5 seconds, add people by nick, messages delivered instantly. Works on phone and desktop.",
    hero_cta1: "Create account", hero_cta2: "I already have an account",
    hero_m1: "No email or phone", hero_m2: "No ads", hero_m3: "Free",

    feat_h: "Features", feat_lead: "Everything you need for quick messaging — and nothing more.",
    feat_1_h: "Instant delivery", feat_1_p: "Messages travel over WebSocket — the other side sees them in milliseconds, no reloads.",
    feat_2_h: "Add by nick", feat_2_p: "No public directory. Type a nick — and you're in each other's contacts.",
    feat_3_h: "Online status", feat_3_p: "See who's online right now and who was here recently — without clutter.",
    feat_4_h: "Any device", feat_4_p: "Same interface on desktop and phone — nicely adapted to the screen.",
    feat_5_h: "Contact search", feat_5_p: "Fast search among your contacts right in the list — filtering as you type.",
    feat_6_h: "Privacy by default", feat_6_p: "Minimum data collected. One session cookie, no trackers.",

    srv_h: "SldChat servers",
    srv_lead: "The project runs on two independent servers. Pick any — data isn't shared between them.",
    srv_main: "Primary", srv_alt: "Backup", srv_unstable: "Unstable",
    srv_notice: "<b>These are different servers.</b> Each has its own users and messages — data is <b>not shared</b>. To use both, register on each separately.",

    priv_h: "Privacy", priv_lead: "The essentials. Full details on a dedicated page.",
    priv_1_h: "Nothing on disk", priv_1_p: "Messages and account data exist only while the server is running.",
    priv_2_h: "No analytics or ads", priv_2_p: "No trackers, pixels or third-party scripts.",
    priv_3_h: "A single session cookie", priv_3_p: "HttpOnly and SameSite — only to keep you signed in.",
    priv_4_h: "Minimal data", priv_4_p: "Just nick and password. No email, phone or IP logs.",
    priv_more: "Read more",

    faq_h: "FAQ",
    faq_q1: "How much does SldChat cost?", faq_a1: "Nothing. Completely free, no ads or subscriptions.",
    faq_q2: "Are my messages saved?", faq_a2: "No. Everything lives in server RAM and disappears on restart. Nothing is written to disk.",
    faq_q3: "Why do I only see my contacts?", faq_a3: "SldChat has no public user directory. Press 'Add contact' and enter the nick.",
    faq_q4: "How do I remove a contact?", faq_a4: "Open the dialog and press 'Remove contact' in the right panel.",
    faq_q5: "How do servers differ?", faq_a5: "Two independent deployments. Each has its own users and messages; they're not connected.",
    faq_q6: "How do I log out?", faq_a6: "The logout button is at the top of the chat, next to the contact list.",

    auth_welcome: "Welcome",
    auth_tab_login: "Sign in", auth_tab_register: "Sign up",
    auth_nick: "Nick:", auth_password: "Password:",
    auth_login_btn: "Sign in", auth_register_btn: "Create account",
    auth_nick_ph: "3–20 characters", auth_pass_ph: "min 3 characters",
    auth_hint: "Registration takes less than a minute",
    auth_back: "← Back to home",

    foot_brand: "SldChat — an independent messenger by SldChat Team.",
    foot_product: "Product", foot_company: "Company", foot_contact: "Contact",
    foot_privacy: "Privacy", foot_support: "Support", foot_faq: "FAQ", foot_mail: "Email",
    foot_status: "Server:", foot_uptime: "Uptime:", foot_copy: "© 2026 SldChat Team. All rights reserved."
  }
};

/* Error messages — identical set on both languages */
var ERRORS = {
  ru: {
    fill_all: "Заполните все поля",
    nick_len: "Ник: 3–20 символов",
    nick_chars: "Ник: только буквы, цифры и _",
    pass_len: "Пароль: минимум 3 символа",
    nick_taken: "Ник уже занят",
    bad_login: "Неверный ник или пароль",
    not_authorized: "Не авторизован",
    enter_nick: "Введите ник",
    cant_add_self: "Нельзя добавить себя",
    user_not_found: "Пользователь не найден",
    already_contact: "Уже в контактах",
    not_in_contacts: "Не в контактах",
    empty_msg: "Пустое сообщение",
    send_failed: "Не удалось отправить",
    error: "Ошибка",
    conn_error: "Ошибка соединения"
  },
  en: {
    fill_all: "Fill in all fields",
    nick_len: "Nick: 3–20 characters",
    nick_chars: "Nick: letters, digits and _ only",
    pass_len: "Password: min 3 characters",
    nick_taken: "Nick already taken",
    bad_login: "Invalid nick or password",
    not_authorized: "Not authorized",
    enter_nick: "Enter a nick",
    cant_add_self: "You can't add yourself",
    user_not_found: "User not found",
    already_contact: "Already in contacts",
    not_in_contacts: "Not in contacts",
    empty_msg: "Empty message",
    send_failed: "Failed to send",
    error: "Error",
    conn_error: "Connection error"
  }
};

var UPTIME_UNITS = {
  ru: {d:'д', h:'ч', m:'м', s:'с'},
  en: {d:'d', h:'h', m:'m', s:'s'}
};

window.SldLang = (function(){
  function pickInitial(){
    var url = new URL(location.href);
    var q = url.searchParams.get('lang');
    if (q && (q === 'ru' || q === 'en')) return q;
    var ls = localStorage.getItem('sld_lang');
    if (ls === 'ru' || ls === 'en') return ls;
    var nav = (navigator.language || 'ru').toLowerCase();
    return nav.indexOf('ru') === 0 ? 'ru' : 'en';
  }
  function applyTo(el, dict){
    el.querySelectorAll('[data-i18n]').forEach(function(e){
      var k = e.getAttribute('data-i18n');
      if (dict[k] != null) e.textContent = dict[k];
    });
    el.querySelectorAll('[data-i18n-html]').forEach(function(e){
      var k = e.getAttribute('data-i18n-html');
      if (dict[k] != null) e.innerHTML = dict[k];
    });
    el.querySelectorAll('[data-i18n-ph]').forEach(function(e){
      var k = e.getAttribute('data-i18n-ph');
      if (dict[k] != null) e.setAttribute('placeholder', dict[k]);
    });
  }
  function propagateLinks(lang){
    document.querySelectorAll('a[href]').forEach(function(a){
      var raw = a.getAttribute('href');
      if (!raw || raw.charAt(0) !== '/') return;
      try {
        var u = new URL(raw, location.origin);
        u.searchParams.set('lang', lang);
        a.setAttribute('href', u.pathname + (u.search ? u.search : '') + (u.hash ? u.hash : ''));
      } catch (e) {}
    });
  }
  function updateUrl(lang){
    try {
      var u = new URL(location.href);
      if (u.searchParams.get('lang') !== lang) {
        u.searchParams.set('lang', lang);
        history.replaceState(null, '', u.pathname + u.search + u.hash);
      }
    } catch (e) {}
  }
  function setGlobalUnits(lang){
    window.__sldUnits = UPTIME_UNITS[lang] || UPTIME_UNITS.en;
  }
  function tr(key, lang){
    var d = ERRORS[lang] || ERRORS.en;
    return d[key] || (ERRORS.en[key] || key);
  }
  function errText(payload, lang){
    if (!payload) return tr('error', lang);
    if (payload.error_key) return tr(payload.error_key, lang);
    if (payload.error) return payload.error;
    return tr('error', lang);
  }
  return {
    I18N: I18N,
    ERRORS: ERRORS,
    pickInitial: pickInitial,
    applyTo: applyTo,
    propagateLinks: propagateLinks,
    updateUrl: updateUrl,
    setGlobalUnits: setGlobalUnits,
    tr: tr,
    errText: errText
  };
})();
"""


# ================== LANDING ==================
LANDING_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SldChat — быстрый мессенджер для команд</title>
<meta name="description" content="SldChat — независимый мессенджер: регистрация за 5 секунд, добавление по нику, мгновенная доставка через WebSocket.">
__HEAD_COMMON__
<meta property="og:type" content="website">
<meta property="og:site_name" content="SldChat">
<meta property="og:title" content="SldChat — быстрый мессенджер для команд">
<meta property="og:description" content="Регистрация за 5 секунд, добавление по нику, мгновенная доставка через WebSocket.">
<meta property="og:url" content="__BASE_URL__">
<meta property="og:image" content="__BASE_URL__og-image.svg">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="ru_RU">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="SldChat — быстрый мессенджер для команд">
<meta name="twitter:description" content="Регистрация за 5 секунд, добавление по нику, мгновенная доставка.">
<meta name="twitter:image" content="__BASE_URL__og-image.svg">
<style>
__SHARED_CSS__

  .hero {
    background: radial-gradient(circle at 20% 20%, #e6eef7 0%, transparent 60%),
                linear-gradient(#d5dfe9, #b8c6d3);
    border-bottom: 1px solid #a8b5c2;
    padding: 84px 0 92px; text-align: center; position: relative; overflow: hidden;
  }
  .hero::after { content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 1px; background: rgba(255,255,255,0.6); }
  .hero .eyebrow {
    display: inline-flex; align-items: center; gap: 8px;
    background: rgba(255,255,255,0.6); border: 1px solid #b7c8db;
    border-radius: 20px; padding: 5px 14px 5px 12px;
    font-size: 11px; letter-spacing: 0.5px; text-transform: uppercase;
    font-weight: bold; color: #3a5169; margin-bottom: 24px;
  }
  .hero .eyebrow .icon { width: 13px; height: 13px; color: #3f6fa8; }
  .hero h1 {
    font-size: 46px; font-weight: normal; margin: 0 0 18px;
    color: #23374b; text-shadow: 0 1px 0 #fff; line-height: 1.12;
    letter-spacing: 0.4px;
  }
  .hero h1 b { color: #3f6fa8; font-weight: bold; }
  .hero p { max-width: 620px; margin: 0 auto 32px; color: #4a5f74; font-size: 16px; }
  .hero-actions { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
  .hero-meta {
    margin-top: 26px; font-size: 12px; color: #5a6f83;
    display: flex; gap: 18px; justify-content: center; flex-wrap: wrap;
  }
  .hero-meta span { display: inline-flex; align-items: center; gap: 6px; }
  .hero-meta .icon { width: 13px; height: 13px; color: #3f6fa8; }

  .servers { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 10px; }
  .server-card {
    background: #fff; border: 1px solid #cfd7df; border-radius: 6px;
    padding: 20px 22px; display: flex; align-items: center; gap: 14px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05), inset 0 1px 0 #fff;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
    min-width: 0;
  }
  .server-card:hover { transform: translateY(-3px); box-shadow: 0 8px 20px rgba(0,0,0,0.10); }
  .server-card .ico {
    width: 46px; height: 46px; border-radius: 8px;
    background: linear-gradient(#e4ebf2, #c8d3de);
    border: 1px solid #b7c2cd;
    display: flex; align-items: center; justify-content: center;
    color: #3f6fa8; flex-shrink: 0;
    box-shadow: inset 0 1px 0 #fff;
  }
  .server-card .body { min-width: 0; flex: 1; }
  .server-card .url {
    font-family: Consolas, "Courier New", monospace;
    font-size: 15px; color: #2b3a4a; font-weight: bold;
    line-height: 1.3; overflow-wrap: anywhere;
  }
  .server-card .desc { font-size: 12px; color: #7a8695; margin-top: 3px; }
  .server-card .tags { display: flex; flex-direction: column; align-items: flex-end; gap: 5px; flex-shrink: 0; }
  .server-card .tag {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px;
    font-weight: bold; padding: 2px 8px; border-radius: 10px;
    white-space: nowrap;
  }
  .server-card .tag.main { color: #2f8f3d; background: #e5f3e7; }
  .server-card .tag.alt  { color: #3f6fa8; background: #e6eef7; }
  .server-card .tag.unstable { color: #a05a00; background: #fdefd6; }

  .notice {
    margin-top: 22px;
    background: #fff8e1; border: 1px solid #ecdc9b; border-radius: 6px;
    padding: 14px 16px; display: flex; gap: 12px; align-items: flex-start;
    color: #6f5a13; font-size: 13px;
  }
  .notice .icon { color: #b8860b; flex-shrink: 0; margin-top: 2px; }
  .notice b { color: #4f3e07; }

  .privacy-mini { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; margin-bottom: 20px; }
  .privacy-item {
    background: #fff; border: 1px solid #cfd7df; border-radius: 6px;
    padding: 16px 18px; display: flex; gap: 12px; align-items: flex-start;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    transition: transform 0.18s ease, box-shadow 0.18s ease;
  }
  .privacy-item:hover { transform: translateY(-2px); box-shadow: 0 6px 14px rgba(0,0,0,0.08); }
  .privacy-item .ico {
    width: 36px; height: 36px; flex-shrink: 0; border-radius: 8px;
    background: linear-gradient(#e4ebf2, #c8d3de); border: 1px solid #b7c2cd;
    display: flex; align-items: center; justify-content: center; color: #3f6fa8;
  }
  .privacy-item h3 { margin: 0 0 3px; font-size: 13px; color: #2b3a4a; }
  .privacy-item p { margin: 0; font-size: 12px; color: #5a6c80; }

  @media (max-width: 820px) {
    .hero { padding: 54px 0 64px; }
    .hero h1 { font-size: 30px; }
    .hero p { font-size: 14px; }
    .servers { grid-template-columns: 1fr; }
    .privacy-mini { grid-template-columns: 1fr; }
  }
  @media (max-width: 460px) {
    .server-card { padding: 16px 16px; gap: 10px; align-items: flex-start; }
    .server-card .ico { width: 40px; height: 40px; }
    .server-card .ico .icon { width: 22px; height: 22px; }
    .server-card .url { font-size: 13px; }
    .server-card .desc { font-size: 11px; }
  }
</style>
</head>
<body>

<header class="topbar">
  <div class="container topbar-inner">
    <a href="/" class="logo">
      <svg class="icon" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
      Sld<span>Chat</span>
    </a>
    <nav class="nav">
      <a class="navlink" href="#features" data-i18n="nav_features">Возможности</a>
      <a class="navlink" href="#servers" data-i18n="nav_servers">Серверы</a>
      <a class="navlink" href="#privacy" data-i18n="nav_privacy">Приватность</a>
      <a class="navlink" href="#faq" data-i18n="nav_faq">FAQ</a>
      <a class="navlink" href="/support" data-i18n="nav_support">Поддержка</a>
      <a class="btn" href="/login" data-i18n="btn_login">Войти</a>
      <a class="btn btn-primary" href="/register" data-i18n="btn_register">Регистрация</a>
    </nav>
  </div>
</header>

<section class="hero">
  <div class="container">
    <span class="eyebrow">
      <svg class="icon" viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
      <span data-i18n="hero_eyebrow">Быстрая доставка через WebSocket</span>
    </span>
    <h1 data-i18n-html="hero_title">Мессенджер <b>SldChat</b> —<br>общайтесь по-простому</h1>
    <p data-i18n="hero_sub">Никаких лишних настроек. Регистрация за 5 секунд, добавление по нику, мгновенная доставка сообщений. Работает на телефоне и на компьютере.</p>
    <div class="hero-actions">
      <a class="btn btn-primary btn-lg" href="/register" data-i18n="hero_cta1">Создать аккаунт</a>
      <a class="btn btn-lg" href="/login" data-i18n="hero_cta2">У меня уже есть аккаунт</a>
    </div>
    <div class="hero-meta">
      <span><svg class="icon" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg><span data-i18n="hero_m1">Без e-mail и телефона</span></span>
      <span><svg class="icon" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg><span data-i18n="hero_m2">Без рекламы</span></span>
      <span><svg class="icon" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg><span data-i18n="hero_m3">Бесплатно</span></span>
    </div>
  </div>
</section>

<section id="features" class="section">
  <div class="container">
    <h2 data-i18n="feat_h">Возможности</h2>
    <p class="lead" data-i18n="feat_lead">Всё, что нужно для быстрого общения — и ничего лишнего.</p>
    <div class="cards">
      <div class="card"><div class="ico"><svg class="icon xl" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg></div>
        <h3 data-i18n="feat_1_h"></h3><p data-i18n="feat_1_p"></p></div>
      <div class="card"><div class="ico"><svg class="icon xl" viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg></div>
        <h3 data-i18n="feat_2_h"></h3><p data-i18n="feat_2_p"></p></div>
      <div class="card"><div class="ico"><svg class="icon xl" viewBox="0 0 24 24"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg></div>
        <h3 data-i18n="feat_3_h"></h3><p data-i18n="feat_3_p"></p></div>
      <div class="card"><div class="ico"><svg class="icon xl" viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg></div>
        <h3 data-i18n="feat_4_h"></h3><p data-i18n="feat_4_p"></p></div>
      <div class="card"><div class="ico"><svg class="icon xl" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div>
        <h3 data-i18n="feat_5_h"></h3><p data-i18n="feat_5_p"></p></div>
      <div class="card"><div class="ico"><svg class="icon xl" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></div>
        <h3 data-i18n="feat_6_h"></h3><p data-i18n="feat_6_p"></p></div>
    </div>
  </div>
</section>

<section id="servers" class="section">
  <div class="container">
    <h2 data-i18n="srv_h">Серверы SldChat</h2>
    <p class="lead" data-i18n="srv_lead">Проект работает на двух независимых серверах. Выбирайте любой — данные между ними не передаются.</p>

    <div class="servers">
      <div class="server-card">
        <div class="ico"><svg class="icon xl" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg></div>
        <div class="body">
          <div class="url">sldchat.fastapicloud.dev</div>
          <div class="desc">FastAPI Cloud</div>
        </div>
        <div class="tags"><span class="tag main" data-i18n="srv_main">Основной</span></div>
      </div>
      <div class="server-card">
        <div class="ico"><svg class="icon xl" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg></div>
        <div class="body">
          <div class="url">sldchat.onrunxbuild.com</div>
          <div class="desc">RunxBuild</div>
        </div>
        <div class="tags">
          <span class="tag alt" data-i18n="srv_alt">Резерв</span>
          <span class="tag unstable" data-i18n="srv_unstable">Unstable</span>
        </div>
      </div>
    </div>

    <div class="notice">
      <svg class="icon lg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
      <div data-i18n-html="srv_notice"></div>
    </div>
  </div>
</section>

<section id="privacy" class="section">
  <div class="container">
    <h2 data-i18n="priv_h">Приватность</h2>
    <p class="lead" data-i18n="priv_lead">Коротко о самом главном. Подробности — на отдельной странице.</p>
    <div class="privacy-mini">
      <div class="privacy-item"><div class="ico"><svg class="icon" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></div>
        <div><h3 data-i18n="priv_1_h"></h3><p data-i18n="priv_1_p"></p></div></div>
      <div class="privacy-item"><div class="ico"><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg></div>
        <div><h3 data-i18n="priv_2_h"></h3><p data-i18n="priv_2_p"></p></div></div>
      <div class="privacy-item"><div class="ico"><svg class="icon" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div>
        <div><h3 data-i18n="priv_3_h"></h3><p data-i18n="priv_3_p"></p></div></div>
      <div class="privacy-item"><div class="ico"><svg class="icon" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></div>
        <div><h3 data-i18n="priv_4_h"></h3><p data-i18n="priv_4_p"></p></div></div>
    </div>
    <a class="link-arrow" href="/privacy">
      <span data-i18n="priv_more">Почитать подробнее</span>
      <svg class="icon" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
    </a>
  </div>
</section>

<section id="faq" class="section">
  <div class="container">
    <h2 data-i18n="faq_h">Ответы на вопросы</h2>
    <div class="faq">
      <details><summary data-i18n="faq_q1"></summary><div class="answer-wrap"><div class="answer" data-i18n="faq_a1"></div></div></details>
      <details><summary data-i18n="faq_q2"></summary><div class="answer-wrap"><div class="answer" data-i18n="faq_a2"></div></div></details>
      <details><summary data-i18n="faq_q3"></summary><div class="answer-wrap"><div class="answer" data-i18n="faq_a3"></div></div></details>
      <details><summary data-i18n="faq_q4"></summary><div class="answer-wrap"><div class="answer" data-i18n="faq_a4"></div></div></details>
      <details><summary data-i18n="faq_q5"></summary><div class="answer-wrap"><div class="answer" data-i18n="faq_a5"></div></div></details>
      <details><summary data-i18n="faq_q6"></summary><div class="answer-wrap"><div class="answer" data-i18n="faq_a6"></div></div></details>
    </div>
  </div>
</section>

__FOOTER__

<script>
__I18N_JS__

(function(){
  document.querySelectorAll('.faq details').forEach(function(d){
    d.addEventListener('toggle', function(){
      if (d.open) {
        document.querySelectorAll('.faq details').forEach(function(o){
          if (o !== d && o.open) o.open = false;
        });
      }
    });
  });

  var lang = SldLang.pickInitial();
  localStorage.setItem('sld_lang', lang);
  SldLang.setGlobalUnits(lang);
  SldLang.applyTo(document, SldLang.I18N[lang]);
  SldLang.updateUrl(lang);
  SldLang.propagateLinks(lang);

  SldFooter.wire(document.getElementById('footLangDd'), function(v){
    localStorage.setItem('sld_lang', v);
    SldLang.setGlobalUnits(v);
    SldLang.applyTo(document, SldLang.I18N[v]);
    SldLang.updateUrl(v);
    SldLang.propagateLinks(v);
    SldFooter.markActive(document.getElementById('footLangDd'), v);
    document.documentElement.lang = v;
    updateTitle(v);
  });
  SldFooter.markActive(document.getElementById('footLangDd'), lang);
  document.documentElement.lang = lang;
  updateTitle(lang);

  function updateTitle(l){
    if (l === 'en') {
      document.title = 'SldChat — fast messenger for teams';
      var d = document.querySelector('meta[name=description]');
      if (d) d.setAttribute('content','SldChat — an independent messenger: register in 5 seconds, add people by nick, instant delivery via WebSocket.');
    } else {
      document.title = 'SldChat — быстрый мессенджер для команд';
      var d2 = document.querySelector('meta[name=description]');
      if (d2) d2.setAttribute('content','SldChat — независимый мессенджер: регистрация за 5 секунд, добавление по нику, мгновенная доставка через WebSocket.');
    }
  }
})();
</script>
</body>
</html>
"""


# ================== PRIVACY ==================
PRIVACY_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SldChat — Политика приватности</title>
__HEAD_COMMON__
<style>
__SHARED_CSS__
</style>
</head>
<body>

<header class="topbar">
  <div class="container topbar-inner">
    <a href="/" class="logo">
      <svg class="icon" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
      Sld<span>Chat</span>
    </a>
    <nav class="nav">
      <a class="navlink" href="/" data-i18n="nav_home">На главную</a>
      <a class="navlink" href="/support" data-i18n="nav_support">Поддержка</a>
      <a class="btn btn-primary" href="/register" data-i18n="btn_register">Регистрация</a>
    </nav>
  </div>
</header>

<div class="page-wrap" data-i18n-html-scope="privacy">
  <div class="page-head">
    <h1 data-i18n="priv_page_h">Политика приватности</h1>
    <p data-i18n="priv_page_sub">Что мы собираем, что нет, и почему SldChat по-настоящему прост.</p>
  </div>

  <div class="page-card selectable">
    <h2 data-i18n="priv_page_short_h">Коротко</h2>
    <p data-i18n-html="priv_page_short_p">SldChat собирает <b>минимум данных</b>. Мы не хотим знать о вас больше, чем нужно для работы мессенджера. Всё хранится в оперативной памяти сервера и стирается при его перезапуске.</p>
  </div>

  <div class="page-card selectable">
    <h2 data-i18n="priv_page_store_h">Что мы храним</h2>
    <ul>
      <li data-i18n="priv_page_store_1"></li>
      <li data-i18n="priv_page_store_2"></li>
      <li data-i18n="priv_page_store_3"></li>
      <li data-i18n="priv_page_store_4"></li>
      <li data-i18n="priv_page_store_5"></li>
    </ul>
    <p data-i18n="priv_page_store_foot"></p>
  </div>

  <div class="page-card selectable">
    <h2 data-i18n="priv_page_dont_h">Чего мы не делаем</h2>
    <ul>
      <li data-i18n="priv_page_dont_1"></li>
      <li data-i18n="priv_page_dont_2"></li>
      <li data-i18n="priv_page_dont_3"></li>
      <li data-i18n="priv_page_dont_4"></li>
    </ul>
  </div>

  <div class="page-card selectable">
    <h2 data-i18n="priv_page_cookie_h">Cookie</h2>
    <p data-i18n-html="priv_page_cookie_p"></p>
    <ul>
      <li data-i18n="priv_page_cookie_1"></li>
      <li data-i18n="priv_page_cookie_2"></li>
      <li data-i18n="priv_page_cookie_3"></li>
      <li data-i18n="priv_page_cookie_4"></li>
    </ul>
  </div>

  <div class="page-card selectable">
    <h2 data-i18n="priv_page_life_h">Сколько данные живут</h2>
    <p data-i18n="priv_page_life_p"></p>
  </div>

  <div class="page-card selectable">
    <h2 data-i18n="priv_page_del_h">Как удалить свои данные</h2>
    <ul>
      <li data-i18n="priv_page_del_1"></li>
      <li data-i18n="priv_page_del_2"></li>
      <li data-i18n="priv_page_del_3"></li>
    </ul>
  </div>

  <div class="page-card selectable">
    <h2 data-i18n="priv_page_srv_h">Два независимых сервера</h2>
    <p data-i18n-html="priv_page_srv_p"></p>
  </div>

  <div class="page-card selectable">
    <h2 data-i18n="priv_page_sec_h">Безопасность</h2>
    <ul>
      <li data-i18n="priv_page_sec_1"></li>
      <li data-i18n="priv_page_sec_2"></li>
      <li data-i18n="priv_page_sec_3"></li>
      <li data-i18n="priv_page_sec_4"></li>
    </ul>
    <p data-i18n="priv_page_sec_foot"></p>
  </div>

  <a class="link-arrow" href="/" style="margin-top:8px">
    <svg class="icon" viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
    <span data-i18n="nav_home">На главную</span>
  </a>
</div>

__FOOTER__

<script>
__I18N_JS__

/* Extend I18N with privacy keys */
I18N.ru.nav_home = "На главную";
I18N.en.nav_home = "Home";

I18N.ru.priv_page_h = "Политика приватности";
I18N.en.priv_page_h = "Privacy policy";
I18N.ru.priv_page_sub = "Что мы собираем, что нет, и почему SldChat по-настоящему прост.";
I18N.en.priv_page_sub = "What we collect, what we don't, and why SldChat is truly simple.";

I18N.ru.priv_page_short_h = "Коротко";
I18N.en.priv_page_short_h = "In short";
I18N.ru.priv_page_short_p = "SldChat собирает <b>минимум данных</b>. Мы не хотим знать о вас больше, чем нужно для работы мессенджера. Всё хранится в оперативной памяти сервера и стирается при его перезапуске.";
I18N.en.priv_page_short_p = "SldChat collects the <b>bare minimum</b>. We don't want to know more about you than needed to run a messenger. Everything lives in server RAM and is erased on restart.";

I18N.ru.priv_page_store_h = "Что мы храним";
I18N.en.priv_page_store_h = "What we store";
I18N.ru.priv_page_store_1 = "Ник — то, как вас видят другие пользователи.";
I18N.en.priv_page_store_1 = "Nick — how other users see you.";
I18N.ru.priv_page_store_2 = "Пароль — только в оперативной памяти.";
I18N.en.priv_page_store_2 = "Password — only in RAM.";
I18N.ru.priv_page_store_3 = "Сообщения — тексты и время отправки.";
I18N.en.priv_page_store_3 = "Messages — texts and timestamps.";
I18N.ru.priv_page_store_4 = "Список контактов — с кем вы общаетесь.";
I18N.en.priv_page_store_4 = "Contact list — whom you talk to.";
I18N.ru.priv_page_store_5 = "Время последней активности — для статуса «в сети».";
I18N.en.priv_page_store_5 = "Last activity time — for the online status.";
I18N.ru.priv_page_store_foot = "Всё это живёт исключительно в ОЗУ процесса. На диск ничего не записывается.";
I18N.en.priv_page_store_foot = "All of it lives only in the process RAM. Nothing is written to disk.";

I18N.ru.priv_page_dont_h = "Чего мы не делаем";
I18N.en.priv_page_dont_h = "What we don't do";
I18N.ru.priv_page_dont_1 = "Не собираем e-mail, телефон и другие персональные данные.";
I18N.en.priv_page_dont_1 = "We don't collect email, phone, or other personal data.";
I18N.ru.priv_page_dont_2 = "Не ведём логи IP-адресов и не отслеживаем вас между сессиями.";
I18N.en.priv_page_dont_2 = "We don't log IPs or track you between sessions.";
I18N.ru.priv_page_dont_3 = "Не используем аналитику, трекеры, пиксели и сторонние скрипты.";
I18N.en.priv_page_dont_3 = "We don't use analytics, trackers, pixels, or third-party scripts.";
I18N.ru.priv_page_dont_4 = "Не показываем рекламу и не передаём данные третьим лицам.";
I18N.en.priv_page_dont_4 = "We show no ads and share no data with third parties.";

I18N.ru.priv_page_cookie_h = "Cookie";
I18N.en.priv_page_cookie_h = "Cookies";
I18N.ru.priv_page_cookie_p = "Мы используем <b>одну-единственную cookie</b> — <code>session</code>. Она нужна только для того, чтобы вы оставались в аккаунте между запросами.";
I18N.en.priv_page_cookie_p = "We use <b>a single cookie</b> — <code>session</code>. It only keeps you signed in between requests.";
I18N.ru.priv_page_cookie_1 = "HttpOnly — недоступна из JavaScript.";
I18N.en.priv_page_cookie_1 = "HttpOnly — not accessible from JavaScript.";
I18N.ru.priv_page_cookie_2 = "SameSite=Lax — снижает риск CSRF-атак.";
I18N.en.priv_page_cookie_2 = "SameSite=Lax — reduces CSRF risks.";
I18N.ru.priv_page_cookie_3 = "Secure — при работе сайта по HTTPS.";
I18N.en.priv_page_cookie_3 = "Secure — when the site runs over HTTPS.";
I18N.ru.priv_page_cookie_4 = "Срок жизни — до 30 дней или до выхода из аккаунта.";
I18N.en.priv_page_cookie_4 = "Lifetime — up to 30 days or until you log out.";

I18N.ru.priv_page_life_h = "Сколько данные живут";
I18N.en.priv_page_life_h = "How long data lives";
I18N.ru.priv_page_life_p = "Ровно столько, сколько работает сервер. Как только он перезапускается, вся информация исчезает безвозвратно.";
I18N.en.priv_page_life_p = "Exactly as long as the server runs. Once it restarts, all information disappears permanently.";

I18N.ru.priv_page_del_h = "Как удалить свои данные";
I18N.en.priv_page_del_h = "How to delete your data";
I18N.ru.priv_page_del_1 = "Выйти из аккаунта — удалит активную сессию на этом устройстве.";
I18N.en.priv_page_del_1 = "Log out — deletes the active session on this device.";
I18N.ru.priv_page_del_2 = "Дождаться перезапуска сервера — удалит всё остальное.";
I18N.en.priv_page_del_2 = "Wait for a server restart — that removes the rest.";
I18N.ru.priv_page_del_3 = "Написать в поддержку, чтобы ускорить процесс.";
I18N.en.priv_page_del_3 = "Contact support to speed up the process.";

I18N.ru.priv_page_srv_h = "Два независимых сервера";
I18N.en.priv_page_srv_h = "Two independent servers";
I18N.ru.priv_page_srv_p = "У SldChat есть <b>два отдельных развёртывания</b> — <code>sldchat.fastapicloud.dev</code> и <code>sldchat.onrunxbuild.com</code>. Это разные серверы с разными базами пользователей. <b>Данные между ними не передаются.</b>";
I18N.en.priv_page_srv_p = "SldChat has <b>two separate deployments</b> — <code>sldchat.fastapicloud.dev</code> and <code>sldchat.onrunxbuild.com</code>. They are different servers with different user bases. <b>Data is not shared between them.</b>";

I18N.ru.priv_page_sec_h = "Безопасность";
I18N.en.priv_page_sec_h = "Security";
I18N.ru.priv_page_sec_1 = "Пароли не возвращаются через API.";
I18N.en.priv_page_sec_1 = "Passwords are never returned via the API.";
I18N.ru.priv_page_sec_2 = "Все проверки доступа — на стороне сервера.";
I18N.en.priv_page_sec_2 = "All access checks happen server-side.";
I18N.ru.priv_page_sec_3 = "Заголовки безопасности: X-Frame-Options, X-Content-Type-Options, Referrer-Policy.";
I18N.en.priv_page_sec_3 = "Security headers: X-Frame-Options, X-Content-Type-Options, Referrer-Policy.";
I18N.ru.priv_page_sec_4 = "Приватные страницы не кэшируются браузером.";
I18N.en.priv_page_sec_4 = "Private pages are not cached by the browser.";
I18N.ru.priv_page_sec_foot = "Полноценной end-to-end криптографии у нас нет. Не отправляйте через SldChat ничего, что боитесь потерять.";
I18N.en.priv_page_sec_foot = "We don't have full end-to-end encryption. Don't send anything through SldChat that you'd hate to lose.";

(function(){
  var lang = SldLang.pickInitial();
  localStorage.setItem('sld_lang', lang);
  SldLang.setGlobalUnits(lang);
  SldLang.applyTo(document, SldLang.I18N[lang]);
  SldLang.updateUrl(lang);
  SldLang.propagateLinks(lang);
  document.documentElement.lang = lang;
  SldFooter.markActive(document.getElementById('footLangDd'), lang);
  SldFooter.wire(document.getElementById('footLangDd'), function(v){
    localStorage.setItem('sld_lang', v);
    SldLang.setGlobalUnits(v);
    SldLang.applyTo(document, SldLang.I18N[v]);
    SldLang.updateUrl(v);
    SldLang.propagateLinks(v);
    SldFooter.markActive(document.getElementById('footLangDd'), v);
    document.documentElement.lang = v;
  });
})();
</script>
</body>
</html>
"""


# ================== SUPPORT ==================
SUPPORT_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SldChat — Поддержка</title>
__HEAD_COMMON__
<style>
__SHARED_CSS__

  .contact-card {
    display: flex; align-items: center; gap: 16px;
    background: #fff; border: 1px solid #cfd7df; border-radius: 6px;
    padding: 18px 20px; margin-bottom: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04), inset 0 1px 0 #fff;
    transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
    text-decoration: none !important;
  }
  .contact-card:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(0,0,0,0.09); border-color: #a8b8ca; text-decoration: none; }
  .contact-card .ico {
    width: 48px; height: 48px; flex-shrink: 0; border-radius: 10px;
    background: linear-gradient(#e4ebf2, #c8d3de); border: 1px solid #b7c2cd;
    display: flex; align-items: center; justify-content: center;
    color: #3f6fa8; box-shadow: inset 0 1px 0 #fff;
  }
  .contact-card .body { min-width: 0; flex: 1; }
  .contact-card .label { font-size: 11px; color: #7a8695; text-transform: uppercase; letter-spacing: 0.5px; }
  .contact-card .value { font-size: 16px; color: #2b3a4a; font-weight: bold; word-break: break-all; margin-top: 2px; }
  .contact-card .value.mono { font-family: Consolas, "Courier New", monospace; font-size: 15px; }
  .contact-card .arrow { color: #a8b8ca; flex-shrink: 0; transition: transform 0.2s ease; }
  .contact-card:hover .arrow { color: #3f6fa8; transform: translateX(3px); }
</style>
</head>
<body>

<header class="topbar">
  <div class="container topbar-inner">
    <a href="/" class="logo">
      <svg class="icon" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
      Sld<span>Chat</span>
    </a>
    <nav class="nav">
      <a class="navlink" href="/" data-i18n="nav_home">На главную</a>
      <a class="navlink" href="/privacy" data-i18n="nav_privacy">Приватность</a>
      <a class="btn btn-primary" href="/register" data-i18n="btn_register">Регистрация</a>
    </nav>
  </div>
</header>

<div class="page-wrap">
  <div class="page-head">
    <h1 data-i18n="sup_h">Поддержка</h1>
    <p data-i18n="sup_sub">Возникла проблема или есть предложение? Напишите нам — мы обязательно ответим.</p>
  </div>

  <div class="page-card">
    <h2 data-i18n="sup_contact_h">Связаться</h2>

    <a class="contact-card" href="mailto:sldshr.confirmation@gmail.com">
      <div class="ico"><svg class="icon xl" viewBox="0 0 24 24"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg></div>
      <div class="body">
        <div class="label">E-mail</div>
        <div class="value mono">sldshr.confirmation@gmail.com</div>
      </div>
      <svg class="icon arrow" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
    </a>

    <a class="contact-card" href="https://discord.com/users/sldshr" target="_blank" rel="noopener noreferrer">
      <div class="ico"><svg class="icon xl" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></div>
      <div class="body">
        <div class="label">Discord</div>
        <div class="value">sldshr</div>
      </div>
      <svg class="icon arrow" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
    </a>
  </div>

  <div class="page-card selectable">
    <h2 data-i18n="sup_before_h">Перед обращением</h2>
    <ul>
      <li data-i18n="sup_before_1"></li>
      <li data-i18n="sup_before_2"></li>
      <li data-i18n="sup_before_3"></li>
      <li data-i18n="sup_before_4"></li>
    </ul>
  </div>
</div>

__FOOTER__

<script>
__I18N_JS__

I18N.ru.nav_home = "На главную";
I18N.en.nav_home = "Home";

I18N.ru.sup_h = "Поддержка";
I18N.en.sup_h = "Support";
I18N.ru.sup_sub = "Возникла проблема или есть предложение? Напишите нам — мы обязательно ответим.";
I18N.en.sup_sub = "Found a problem or have a suggestion? Write to us — we'll definitely reply.";

I18N.ru.sup_contact_h = "Связаться";
I18N.en.sup_contact_h = "Contact us";

I18N.ru.sup_before_h = "Перед обращением";
I18N.en.sup_before_h = "Before contacting";
I18N.ru.sup_before_1 = "Убедитесь, что вы на нужном сервере: sldchat.fastapicloud.dev или sldchat.onrunxbuild.com — данные между ними не передаются.";
I18N.en.sup_before_1 = "Make sure you're on the right server: sldchat.fastapicloud.dev or sldchat.onrunxbuild.com — data is not shared between them.";
I18N.ru.sup_before_2 = "Если не получается войти — проверьте, что ник введён точно так же, как при регистрации.";
I18N.en.sup_before_2 = "If you can't log in — make sure the nick matches exactly what you registered.";
I18N.ru.sup_before_3 = "Сообщения не сохраняются после перезапуска сервера — это нормально.";
I18N.en.sup_before_3 = "Messages don't survive a server restart — that's normal.";
I18N.ru.sup_before_4 = "Опишите проблему подробно: что делали, что ожидали, что получилось.";
I18N.en.sup_before_4 = "Describe the issue in detail: what you did, what you expected, what actually happened.";

(function(){
  var lang = SldLang.pickInitial();
  localStorage.setItem('sld_lang', lang);
  SldLang.setGlobalUnits(lang);
  SldLang.applyTo(document, SldLang.I18N[lang]);
  SldLang.updateUrl(lang);
  SldLang.propagateLinks(lang);
  document.documentElement.lang = lang;
  SldFooter.markActive(document.getElementById('footLangDd'), lang);
  SldFooter.wire(document.getElementById('footLangDd'), function(v){
    localStorage.setItem('sld_lang', v);
    SldLang.setGlobalUnits(v);
    SldLang.applyTo(document, SldLang.I18N[v]);
    SldLang.updateUrl(v);
    SldLang.propagateLinks(v);
    SldFooter.markActive(document.getElementById('footLangDd'), v);
    document.documentElement.lang = v;
  });
})();
</script>
</body>
</html>
"""


# ================== AUTH (полностью i18n) ==================
AUTH_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SldChat</title>
__HEAD_COMMON__
<style>
__SHARED_CSS__
  body {
    min-height: 100vh; display: flex; flex-direction: column;
    background: radial-gradient(circle at 30% 10%, #e6eef6 0%, transparent 55%),
                linear-gradient(#cfd9e3, #a8b6c4);
  }
  .topline { padding: 16px 22px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .topline a.logo { font-size: 20px; }
  .wrap { flex: 1; display: flex; align-items: center; justify-content: center; padding: 20px; }
  .box {
    width: 100%; max-width: 380px;
    background: #f4f6f8;
    border: 1px solid #8b97a3; border-radius: 6px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.22), inset 0 1px 0 #fff;
    padding: 22px 26px 26px; animation: popIn 0.4s ease both;
  }
  .box h1 { text-align: center; font-size: 20px; font-weight: normal; margin: 0 0 18px; color: #23374b; text-shadow: 0 1px 0 #fff; letter-spacing: 0.5px; }
  .tabs { display: flex; margin-bottom: 16px; border-bottom: 1px solid #a8b2bc; }
  .tabs button {
    flex: 1; border: 1px solid #a8b2bc; border-bottom: none;
    background: linear-gradient(#eef1f4, #d3dae1);
    border-radius: 4px 4px 0 0; padding: 8px 0; margin-right: 4px; cursor: pointer;
    font-family: inherit; font-size: 13px; color: #445;
    transition: background 0.15s ease, color 0.15s ease;
  }
  .tabs button:last-child { margin-right: 0; }
  .tabs button:hover { background: linear-gradient(#f4f6f8, #dae0e6); }
  .tabs button.active { background: #f4f6f8; color: #223; font-weight: bold; position: relative; top: 1px; }
  label { display: block; margin: 10px 0 4px; color: #445; font-size: 13px; }
  input[type=text], input[type=password] {
    width: 100%; padding: 8px 10px; font-family: inherit; font-size: 13px;
    border: 1px solid #9aa4ae; border-radius: 3px; background: #fff; outline: none;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  input:focus { border-color: #5a7a9a; box-shadow: inset 0 1px 2px rgba(0,0,0,0.08), 0 0 0 3px rgba(90,122,154,0.15); }
  .btn2 {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    width: 100%; margin-top: 18px; padding: 10px 0; border-radius: 4px;
    font-family: inherit; font-size: 13px; cursor: pointer;
    background: linear-gradient(#fbfcfd, #ccd5de);
    border: 1px solid #7a8794; color: #2b3a4a; text-shadow: 0 1px 0 #fff;
    transition: transform 0.1s ease, box-shadow 0.15s ease, background 0.15s ease;
  }
  .btn2-primary {
    background: linear-gradient(#5b8fc4, #3f6fa8);
    border-color: #35597f; color: #fff;
    text-shadow: 0 1px 0 rgba(0,0,0,0.2);
  }
  .btn2-primary:hover { background: linear-gradient(#699bcd, #4577b1); box-shadow: 0 4px 12px rgba(63,111,168,0.35); }
  .btn2:active { transform: scale(0.98); }
  .error { min-height: 18px; color: #c22; text-align: center; font-size: 12px; margin-bottom: 4px; }
  .hint { margin-top: 14px; text-align: center; color: #889; font-size: 11px; }
  .back { text-align: center; margin-top: 14px; font-size: 12px; }
  .form { display: block; }
  .form.hidden { display: none; }

  /* Light dropdown in topline */
  .dd.dd-light .dd-toggle {
    background: linear-gradient(#fbfcfd, #cfd8e0);
    border-color: #8b97a3; color: #2b3a4a;
    text-shadow: 0 1px 0 rgba(255,255,255,0.6);
    padding: 6px 10px; font-size: 12px;
  }
  .dd.dd-light .dd-toggle:hover { background: linear-gradient(#fff, #dbe3ea); }
  .dd.dd-light .dd-toggle .icon { color: #4a5b6d; }
  .dd.dd-light .dd-toggle .dd-value { color: #2b3a4a; }
  .dd.dd-light .dd-menu {
    top: calc(100% + 6px); bottom: auto;
    transform-origin: top right;
    transform: translateY(-6px) scale(0.97);
  }
  .dd.dd-light.open .dd-menu { transform: translateY(0) scale(1); }
</style>
</head>
<body>

<div class="topline">
  <a href="/" class="logo">
    <svg class="icon" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
    Sld<span>Chat</span>
  </a>
  <div class="dd dd-light" id="authLangDd">
    <button class="dd-toggle" type="button" aria-haspopup="listbox">
      <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
      <span class="dd-value" id="authLangValue">RU</span>
      <svg class="icon chev" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
    </button>
    <ul class="dd-menu">
      <li data-value="ru">Русский</li>
      <li data-value="en">English</li>
    </ul>
  </div>
</div>

<div class="wrap">
  <div class="box">
    <h1 data-i18n="auth_welcome">Добро пожаловать</h1>
    <div class="tabs">
      <button type="button" id="tabLogin" data-i18n="auth_tab_login">Вход</button>
      <button type="button" id="tabRegister" data-i18n="auth_tab_register">Регистрация</button>
    </div>
    <div class="error" id="error"></div>

    <form id="formLogin" class="form hidden">
      <label data-i18n="auth_nick">Ник:</label>
      <input type="text" name="nick" autocomplete="username" maxlength="20">
      <label data-i18n="auth_password">Пароль:</label>
      <input type="password" name="password" autocomplete="current-password">
      <button type="submit" class="btn2 btn2-primary" data-i18n="auth_login_btn">Войти</button>
    </form>

    <form id="formRegister" class="form hidden">
      <label data-i18n="auth_nick">Ник:</label>
      <input type="text" name="nick" autocomplete="username" maxlength="20" data-i18n-ph="auth_nick_ph" placeholder="3–20 символов">
      <label data-i18n="auth_password">Пароль:</label>
      <input type="password" name="password" autocomplete="new-password" data-i18n-ph="auth_pass_ph" placeholder="минимум 3 символа">
      <button type="submit" class="btn2 btn2-primary" data-i18n="auth_register_btn">Зарегистрироваться</button>
    </form>

    <div class="hint" data-i18n="auth_hint">Регистрация занимает меньше минуты</div>
    <div class="back"><a href="/" data-i18n="auth_back">← На главную</a></div>
  </div>
</div>

<script>
__I18N_JS__

(function(){
  var tabLogin = document.getElementById('tabLogin');
  var tabRegister = document.getElementById('tabRegister');
  var formLogin = document.getElementById('formLogin');
  var formRegister = document.getElementById('formRegister');
  var errorBox = document.getElementById('error');

  var initialTab = "__ACTIVE_TAB__";

  function showTab(which){
    if (which === 'login') {
      tabLogin.classList.add('active'); tabRegister.classList.remove('active');
      formLogin.classList.remove('hidden'); formRegister.classList.add('hidden');
    } else {
      tabRegister.classList.add('active'); tabLogin.classList.remove('active');
      formRegister.classList.remove('hidden'); formLogin.classList.add('hidden');
    }
    errorBox.textContent = '';
  }
  tabLogin.onclick = function(){ showTab('login'); };
  tabRegister.onclick = function(){ showTab('register'); };

  function submitForm(url, form){
    errorBox.textContent = '';
    var fd = new FormData(form);
    fetch(url, { method: 'POST', body: fd })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) {
          var l = localStorage.getItem('sld_lang') || 'ru';
          location.href = '/chat?lang=' + l;
          return;
        }
        errorBox.textContent = SldLang.errText(d, lang);
      })
      .catch(function(){ errorBox.textContent = SldLang.tr('conn_error', lang); });
  }
  formLogin.onsubmit = function(e){ e.preventDefault(); submitForm('/api/login', formLogin); };
  formRegister.onsubmit = function(e){ e.preventDefault(); submitForm('/api/register', formRegister); };

  /* ---------- language ---------- */
  var lang = SldLang.pickInitial();
  localStorage.setItem('sld_lang', lang);
  SldLang.setGlobalUnits(lang);
  SldLang.applyTo(document, SldLang.I18N[lang]);
  SldLang.updateUrl(lang);
  SldLang.propagateLinks(lang);
  document.documentElement.lang = lang;
  setLangValue(lang);
  showTab(initialTab);
  updateTitle(lang);

  function setLangValue(v){
    var el = document.getElementById('authLangValue');
    if (el) el.textContent = (v === 'ru') ? 'RU' : 'EN';
    var menu = document.querySelector('#authLangDd .dd-menu');
    if (menu) {
      menu.querySelectorAll('li').forEach(function(li){
        if (li.dataset.value === v) li.classList.add('active'); else li.classList.remove('active');
      });
    }
  }

  function updateTitle(l){
    document.title = (l === 'en') ? 'SldChat — Sign in' : 'SldChat — Вход';
  }

  var authDd = document.getElementById('authLangDd');
  if (authDd) {
    var toggle = authDd.querySelector('.dd-toggle');
    toggle.addEventListener('click', function(e){
      e.stopPropagation();
      authDd.classList.toggle('open');
    });
    authDd.querySelectorAll('.dd-menu li').forEach(function(li){
      li.addEventListener('click', function(){
        var v = li.dataset.value;
        lang = v;
        localStorage.setItem('sld_lang', v);
        SldLang.setGlobalUnits(v);
        SldLang.applyTo(document, SldLang.I18N[v]);
        SldLang.updateUrl(v);
        SldLang.propagateLinks(v);
        document.documentElement.lang = v;
        setLangValue(v);
        updateTitle(v);
        authDd.classList.remove('open');
        errorBox.textContent = '';
      });
    });
    document.addEventListener('click', function(){ authDd.classList.remove('open'); });
  }
})();
</script>
</body>
</html>
"""


def render_auth(active: str) -> str:
    return (AUTH_TEMPLATE
        .replace("__SHARED_CSS__", SHARED_CSS)
        .replace("__HEAD_COMMON__", HEAD_COMMON)
        .replace("__I18N_JS__", I18N_JS)
        .replace("__ACTIVE_TAB__", "login" if active == "login" else "register"))


# ================== CHAT ==================
CHAT_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>SldChat</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
<meta name="theme-color" content="#3f6fa8">
<style>
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; padding: 0; height: 100%; overflow: hidden; overscroll-behavior: none; }
  body {
    font-family: Tahoma, Verdana, Arial, sans-serif;
    font-size: 13px; color: #2b3a4a; background: #e9eef3;
    -webkit-user-select: none; -moz-user-select: none; user-select: none;
    -webkit-font-smoothing: antialiased;
  }
  input, textarea, .msg-bubble { -webkit-user-select: text; -moz-user-select: text; user-select: text; }
  * { scrollbar-width: none; -ms-overflow-style: none; }
  *::-webkit-scrollbar { width: 0; height: 0; display: none; }

  @keyframes msgIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes toastIn { from { opacity: 0; transform: translateX(24px); } to { opacity: 1; transform: translateX(0); } }
  @keyframes dotPulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(76,175,80,0.5); }
    50%      { box-shadow: 0 0 0 4px rgba(76,175,80,0); }
  }

  .icon {
    width: 16px; height: 16px; flex-shrink: 0;
    stroke: currentColor; fill: none;
    stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
    vertical-align: -3px;
  }
  .icon.lg { width: 20px; height: 20px; }
  .icon.huge { width: 44px; height: 44px; stroke-width: 1.5; color: #c1cbd5; }

  .app {
    display: grid;
    grid-template-columns: 280px 1fr 260px;
    height: 100vh; width: 100vw; overflow: hidden;
    background: #e9eef3;
  }

  /* ===== Dropdown ===== */
  .dd { position: relative; display: inline-block; }
  .dd-toggle {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 9px; border-radius: 4px;
    border: 1px solid #b5bec8; cursor: pointer;
    background: linear-gradient(#fbfcfd, #dce3ea);
    font-family: inherit; font-size: 12px; color: #3a5169;
    text-shadow: 0 1px 0 #fff;
    transition: background 0.15s ease;
  }
  .dd-toggle:hover { background: linear-gradient(#fff, #dbe3ea); }
  .dd-toggle .icon { width: 13px; height: 13px; }
  .dd-toggle .icon.chev { transition: transform 0.22s ease; }
  .dd.open .dd-toggle .icon.chev { transform: rotate(180deg); }
  .dd-toggle .dd-value { font-weight: bold; }

  .dd-menu {
    position: absolute; top: calc(100% + 6px); left: 0;
    min-width: 160px;
    background: #fff;
    border: 1px solid #b7c2cd; border-radius: 6px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.22);
    padding: 5px; margin: 0; list-style: none;
    opacity: 0; visibility: hidden;
    transform: translateY(-6px) scale(0.97);
    transform-origin: top left;
    transition: opacity 0.15s ease, transform 0.15s ease, visibility 0.15s;
    z-index: 200;
  }
  .dd.open .dd-menu { opacity: 1; visibility: visible; transform: translateY(0) scale(1); }
  .dd-menu li {
    padding: 8px 12px; border-radius: 4px; cursor: pointer;
    font-size: 13px; color: #2b3a4a;
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    transition: background 0.1s ease;
  }
  .dd-menu li:hover { background: #eef2f6; }
  .dd-menu li.active { background: #e4ebf3; font-weight: bold; }
  .dd-menu li.active::after { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #3f6fa8; }

  .sidebar {
    background: #f0f3f7; border-right: 1px solid #c8d1da;
    display: flex; flex-direction: column; min-width: 0;
  }
  .sb-header {
    padding: 8px 10px;
    background: linear-gradient(#fbfcfd, #d6dee5);
    border-bottom: 1px solid #b0bac4;
    display: flex; align-items: center; justify-content: space-between;
    gap: 6px;
  }
  .logo-mini {
    display: flex; align-items: center; gap: 6px;
    font-size: 15px; font-weight: bold; color: #2b3a4a;
    text-shadow: 0 1px 0 #fff;
  }
  .logo-mini .icon { width: 18px; height: 18px; color: #3f6fa8; }
  .logo-mini span { color: #3f6fa8; }
  .sb-actions { display: flex; align-items: center; gap: 6px; }

  .me-line {
    padding: 6px 10px 8px;
    background: #e8edf3; border-bottom: 1px solid #d3dae0;
    font-size: 12px; color: #5a6c80;
  }
  .me-line .nick { font-weight: bold; color: #2b3a4a; }

  .icon-btn {
    border: 1px solid #b5bec8; border-radius: 4px;
    background: linear-gradient(#fbfcfd, #dce3ea);
    padding: 4px 7px; cursor: pointer; line-height: 1;
    color: #3a5169;
    display: inline-flex; align-items: center; justify-content: center;
    transition: transform 0.12s ease, background 0.15s ease;
    font-family: inherit;
  }
  .icon-btn:hover { background: linear-gradient(#fff, #dbe3ea); transform: translateY(-1px); }
  .icon-btn:active { transform: translateY(0) scale(0.94); }

  .add-wrap { padding: 8px 9px; border-bottom: 1px solid #dbe1e7; }
  .add-btn {
    width: 100%; padding: 8px 10px;
    display: flex; align-items: center; justify-content: center; gap: 7px;
    border: 1px solid #7a8794; border-radius: 4px;
    background: linear-gradient(#fbfcfd, #cfd8e0);
    font-family: inherit; font-size: 12px; color: #2b3a4a;
    cursor: pointer; text-shadow: 0 1px 0 #fff;
    transition: transform 0.1s ease, background 0.15s ease;
  }
  .add-btn:hover { background: linear-gradient(#fff, #dbe3ea); transform: translateY(-1px); }
  .add-btn:active { transform: translateY(0) scale(0.98); }

  .add-form { display: none; gap: 6px; margin-top: 6px; }
  .add-form.open { display: flex; }
  .add-form input {
    flex: 1; min-width: 0;
    padding: 6px 9px; font-family: inherit; font-size: 12px;
    border: 1px solid #9aa4ae; border-radius: 3px;
    background: #fff; outline: none;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.06);
  }
  .add-form input:focus { border-color: #5a7a9a; }
  .add-form button {
    padding: 0 10px; border-radius: 3px;
    border: 1px solid #35597f; cursor: pointer;
    background: linear-gradient(#5b8fc4, #3f6fa8);
    color: #fff; font-family: inherit; font-size: 12px;
    display: flex; align-items: center; justify-content: center;
  }
  .add-msg { font-size: 11px; margin-top: 5px; min-height: 14px; color: #7a8695; }
  .add-msg.err { color: #c22; }
  .add-msg.ok  { color: #2f8f3d; }

  .search { padding: 7px 9px; border-bottom: 1px solid #d3dae0; background: #eef2f6; }
  .search-wrap { position: relative; }
  .search-wrap .icon {
    position: absolute; left: 8px; top: 50%; transform: translateY(-50%);
    color: #98a2ad; width: 13px; height: 13px;
  }
  .search input {
    width: 100%; padding: 6px 9px 6px 26px;
    font-family: inherit; font-size: 12px;
    border: 1px solid #b5bec8; border-radius: 14px;
    background: #fff; outline: none;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.06);
  }
  .search input:focus { border-color: #5a7a9a; }

  .user-list { flex: 1; overflow-y: auto; }
  .user-item {
    padding: 8px 11px; border-bottom: 1px solid #dde3e9;
    background: #f6f8fa; cursor: pointer; min-width: 0;
    transition: background 0.12s ease;
  }
  .user-item:hover  { background: #eaf0f6; }
  .user-item.active { background: #d3e0ec; }
  .user-item .row1 { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
  .user-item .nick {
    font-weight: bold; color: #2b3a4a;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    display: flex; align-items: center; gap: 6px; min-width: 0;
  }
  .user-item .nick .dot { width: 7px; height: 7px; border-radius: 50%; background: #b5bec8; flex-shrink: 0; }
  .user-item .nick .dot.on { background: #4caf50; animation: dotPulse 2s ease-in-out infinite; }
  .user-item .time { font-size: 10px; color: #8a94a0; flex-shrink: 0; }
  .user-item .preview {
    color: #7a8695; font-size: 11px; margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .user-item .badge {
    background: #3f6fa8; color: #fff; font-size: 10px;
    border-radius: 9px; padding: 1px 6px; min-width: 18px;
    text-align: center; margin-left: 4px;
  }

  .empty-list {
    padding: 22px 14px; text-align: center; color: #98a2ad;
    font-size: 12px;
    display: flex; flex-direction: column; align-items: center; gap: 10px;
  }

  .chat { display: flex; flex-direction: column; background: #fff; min-width: 0; }
  .chat-header {
    padding: 8px 12px; min-height: 52px;
    background: linear-gradient(#fbfcfd, #d6dee5);
    border-bottom: 1px solid #b0bac4;
    display: flex; align-items: center; gap: 10px;
  }
  .chat-header .title {
    flex: 1; min-width: 0;
    font-weight: bold; color: #2b3a4a;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .chat-header .sub { font-size: 11px; color: #7a8695; font-weight: normal; margin-top: 1px; }
  .chat-header .sub.online { color: #2f8f3d; font-weight: bold; }
  .mobile-only { display: none; }

  .messages {
    flex: 1; overflow-y: auto; padding: 12px 16px 8px;
    background: radial-gradient(circle at 80% 10%, #f6f9fc 0%, transparent 60%), #fbfcfd;
    -webkit-overflow-scrolling: touch;
  }
  .empty {
    color: #a2aab3; text-align: center; margin-top: 60px; font-size: 13px;
    display: flex; flex-direction: column; align-items: center; gap: 14px;
  }

  .date-sep { text-align: center; margin: 10px 0 8px; }
  .date-sep span {
    background: #eef2f6; color: #6f7c8b;
    padding: 2px 10px; border-radius: 10px; font-size: 10px;
    border: 1px solid #dde4eb;
  }

  .msg-row { display: flex; margin-bottom: 2px; align-items: flex-end; gap: 4px; animation: msgIn 0.16s ease-out both; }
  .msg-row.mine { justify-content: flex-end; }
  .msg-bubble {
    max-width: min(72%, 560px);
    padding: 5px 10px 4px;
    border-radius: 12px 12px 12px 3px;
    background: #eef2f6; border: 1px solid #e3e9ef;
    font-size: 13px; line-height: 1.32;
    word-wrap: break-word; white-space: pre-wrap;
    color: #22303e;
  }
  .msg-row.mine .msg-bubble { background: #d3e6c8; border-color: #c5dcb5; border-radius: 12px 12px 3px 12px; }
  .msg-meta {
    display: inline-block; font-size: 9px; color: #8a94a0; margin-left: 8px;
    vertical-align: bottom; opacity: 0.85; white-space: nowrap;
    float: right; margin-top: 4px;
  }

  .input-area {
    border-top: 1px solid #c8d1da; padding: 8px 10px;
    background: #eef2f6;
    display: flex; gap: 8px; align-items: flex-end;
  }
  .input-area textarea {
    flex: 1; resize: none;
    padding: 8px 12px; font-family: inherit; font-size: 13px;
    border: 1px solid #b5bec8; border-radius: 18px;
    background: #fff; outline: none;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.06);
    max-height: 120px; min-height: 36px; line-height: 1.4;
    height: 36px;
  }
  .input-area textarea:focus { border-color: #5a7a9a; }
  .input-area button {
    height: 36px;
    padding: 0 18px;
    border-radius: 18px;
    border: 1px solid #35597f; cursor: pointer;
    background: linear-gradient(#5b8fc4, #3f6fa8);
    color: #fff; font-family: inherit; font-size: 13px;
    text-shadow: 0 1px 0 rgba(0,0,0,0.2);
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    transition: transform 0.1s ease, background 0.15s ease;
    flex-shrink: 0;
  }
  .input-area button .icon { width: 15px; height: 15px; }
  .input-area button:hover { background: linear-gradient(#699bcd, #4577b1); }
  .input-area button:active { transform: scale(0.97); }

  .info-panel {
    background: #f0f3f7; border-left: 1px solid #c8d1da;
    padding: 16px; overflow-y: auto; min-width: 0;
  }
  .info-panel .close-btn { display: none; }
  .info-empty {
    color: #98a2ad; text-align: center; margin-top: 40px; font-size: 12px;
    display: flex; flex-direction: column; align-items: center; gap: 12px;
  }
  .info-head { text-align: center; padding-bottom: 14px; border-bottom: 1px solid #dbe1e7; margin-bottom: 14px; }
  .info-head .name { font-size: 18px; font-weight: bold; color: #23374b; word-break: break-all; }
  .info-head .status { font-size: 12px; margin-top: 4px; color: #7a8695; }
  .info-head .status.online { color: #2f8f3d; font-weight: bold; }
  .info-row {
    display: flex; justify-content: space-between;
    padding: 8px 0; border-bottom: 1px solid #e0e6ec;
    font-size: 12px; gap: 8px;
  }
  .info-row .k { color: #7a8695; flex-shrink: 0; }
  .info-row .v { color: #2b3a4a; text-align: right; word-break: break-word; }

  .remove-btn {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    width: 100%; margin-top: 18px;
    padding: 10px 12px; border-radius: 5px;
    border: 1px solid #a85a5a; cursor: pointer;
    background: linear-gradient(#fbfcfd, #e9d8d8);
    color: #8f2b2b; font-family: inherit; font-size: 13px;
    text-shadow: 0 1px 0 #fff;
    transition: transform 0.1s ease, background 0.15s ease, box-shadow 0.15s ease;
  }
  .remove-btn:hover { background: linear-gradient(#fff, #f0dcdc); transform: translateY(-1px); box-shadow: 0 2px 6px rgba(168,90,90,0.25); }
  .remove-btn:active { transform: translateY(0) scale(0.98); }
  .remove-btn .icon { width: 15px; height: 15px; }

  .toast-wrap {
    position: fixed; bottom: 16px; right: 16px; z-index: 100;
    display: flex; flex-direction: column; gap: 8px; pointer-events: none;
  }
  .toast {
    background: #2b3a4a; color: #fff;
    border-radius: 6px; padding: 10px 14px;
    font-size: 12px; max-width: 300px;
    box-shadow: 0 6px 18px rgba(0,0,0,0.25);
    animation: toastIn 0.22s ease-out both;
    transition: opacity 0.25s ease, transform 0.25s ease;
  }
  .toast.out { opacity: 0; transform: translateX(24px); }

  @media (max-width: 900px) {
    .app { grid-template-columns: 1fr; position: relative; }
    .sidebar { border-right: none; padding-bottom: env(safe-area-inset-bottom); }
    .chat { display: none; }
    .info-panel {
      position: fixed; top: 0; right: 0; bottom: 0;
      width: min(360px, 92vw); z-index: 40;
      transform: translateX(100%);
      transition: transform 0.3s cubic-bezier(0.22, 1, 0.36, 1);
      box-shadow: -8px 0 24px rgba(0,0,0,0.25);
      border-left: 1px solid #b0bac4;
      padding-top: calc(16px + env(safe-area-inset-top));
      padding-bottom: calc(16px + env(safe-area-inset-bottom));
    }
    .app.chat-open .sidebar { display: none; }
    .app.chat-open .chat { display: flex; }
    .app.info-open .info-panel { transform: translateX(0); }
    .info-panel .close-btn { display: inline-flex; float: right; margin: -4px -4px 0 0; padding: 8px; }
    .info-panel .close-btn .icon { width: 18px; height: 18px; }
    .mobile-only { display: inline-flex; }

    .sb-header { padding: 12px 14px; padding-top: calc(12px + env(safe-area-inset-top)); }
    .logo-mini { font-size: 18px; gap: 8px; }
    .logo-mini .icon { width: 22px; height: 22px; }
    .sb-header .icon-btn { padding: 9px; }
    .sb-header .icon-btn .icon { width: 20px; height: 20px; }
    .dd-toggle { padding: 8px 12px; font-size: 13px; }

    .me-line { padding: 8px 14px; font-size: 13px; }

    .add-wrap { padding: 12px 14px; }
    .add-btn { padding: 12px 14px; font-size: 15px; border-radius: 6px; min-height: 46px; }
    .add-btn .icon { width: 18px; height: 18px; }
    .add-form { margin-top: 8px; gap: 8px; }
    .add-form input { padding: 11px 14px; font-size: 15px; border-radius: 6px; min-height: 46px; }
    .add-form button { padding: 0 16px; min-height: 46px; border-radius: 6px; }
    .add-form button .icon { width: 18px; height: 18px; }

    .search { padding: 10px 14px; }
    .search input { padding: 11px 14px 11px 36px; font-size: 15px; border-radius: 20px; min-height: 42px; }
    .search-wrap .icon { left: 12px; width: 16px; height: 16px; }

    .user-item { padding: 14px 16px; }
    .user-item .nick { font-size: 15px; gap: 8px; }
    .user-item .nick .dot { width: 9px; height: 9px; }
    .user-item .time { font-size: 11px; }
    .user-item .preview { font-size: 13px; margin-top: 4px; }
    .user-item .badge { font-size: 11px; padding: 2px 7px; min-width: 20px; }

    .empty-list { padding: 50px 20px; font-size: 14px; }
    .empty-list .icon.huge { width: 52px; height: 52px; }

    .chat-header {
      padding: 10px 12px;
      padding-top: calc(10px + env(safe-area-inset-top));
      min-height: 60px; gap: 8px;
    }
    .chat-header .title { font-size: 16px; }
    .chat-header .sub { font-size: 12px; margin-top: 2px; }
    .chat-header .icon-btn { padding: 9px; }
    .chat-header .icon-btn .icon { width: 22px; height: 22px; }

    .messages { padding: 14px 12px 8px; }
    .msg-bubble { max-width: 85%; font-size: 14px; padding: 7px 12px 5px; border-radius: 14px 14px 14px 3px; }
    .msg-row.mine .msg-bubble { border-radius: 14px 14px 3px 14px; }
    .msg-meta { font-size: 10px; }
    .date-sep { margin: 12px 0 8px; }
    .date-sep span { font-size: 11px; padding: 3px 11px; }

    .empty { margin-top: 40px; font-size: 14px; }
    .empty .icon.huge { width: 52px; height: 52px; }

    .input-area {
      padding: 8px 10px;
      padding-bottom: calc(8px + env(safe-area-inset-bottom));
      gap: 6px; align-items: flex-end;
    }
    .input-area textarea {
      padding: 10px 14px; font-size: 15px;
      min-height: 44px; height: 44px;
      border-radius: 22px; max-height: 100px;
    }
    .input-area button {
      width: 44px; height: 44px;
      padding: 0; border-radius: 50%;
      justify-content: center;
    }
    .input-area button .btn-label { display: none; }
    .input-area button .icon { width: 18px; height: 18px; }

    .info-head .name { font-size: 20px; }
    .info-head .status { font-size: 13px; margin-top: 6px; }
    .info-row { font-size: 13px; padding: 10px 0; }
    .remove-btn { padding: 13px 12px; font-size: 15px; }

    .toast-wrap { bottom: calc(16px + env(safe-area-inset-bottom)); right: 12px; left: 12px; }
    .toast { max-width: none; }
  }

  @media (max-width: 380px) {
    .me-line { display: none; }
  }
</style>
</head>
<body>

<div class="app" id="app">

  <aside class="sidebar">
    <div class="sb-header">
      <div class="logo-mini">
        <svg class="icon" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
        Sld<span>Chat</span>
      </div>
      <div class="sb-actions">
        <div class="dd" id="chatLangDd">
          <button class="dd-toggle" type="button" title="Language">
            <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
            <span class="dd-value" id="chatLangValue">RU</span>
            <svg class="icon chev" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <ul class="dd-menu">
            <li data-value="ru">Русский</li>
            <li data-value="en">English</li>
          </ul>
        </div>
        <button class="icon-btn" id="logoutBtn" title="Logout">
          <svg class="icon" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
        </button>
      </div>
    </div>

    <div class="me-line"><span data-ci18n="you">Вы вошли как</span> <span class="nick" id="myNick">…</span></div>

    <div class="add-wrap">
      <button class="add-btn" id="addBtn">
        <svg class="icon" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        <span data-ci18n="add_contact">Добавить контакт</span>
      </button>
      <form class="add-form" id="addForm">
        <input type="text" id="addInput" placeholder="Ник собеседника" data-ci18n-ph="nick_ph" autocomplete="off" maxlength="20">
        <button type="submit" title="OK">
          <svg class="icon" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
        </button>
      </form>
      <div class="add-msg" id="addMsg"></div>
    </div>

    <div class="search">
      <div class="search-wrap">
        <svg class="icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input type="text" id="searchInput" placeholder="Поиск по контактам" data-ci18n-ph="search_ph" autocomplete="off">
      </div>
    </div>

    <div class="user-list" id="userList"></div>
  </aside>

  <main class="chat" id="chatMain">
    <header class="chat-header">
      <button class="icon-btn mobile-only" id="backBtn" title="Back">
        <svg class="icon" viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
      </button>
      <div class="title">
        <div id="chatTitle" data-ci18n="pick_peer">Выберите собеседника</div>
        <div class="sub" id="chatSub"></div>
      </div>
      <button class="icon-btn mobile-only" id="infoBtn" title="Info">
        <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
      </button>
    </header>

    <div class="messages" id="messages">
      <div class="empty">
        <svg class="icon huge" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
        <div data-ci18n="empty_pick">Слева выберите контакт, чтобы начать переписку</div>
      </div>
    </div>

    <div class="input-area" id="inputArea" style="display:none">
      <textarea id="msgInput" placeholder="Введите сообщение..." data-ci18n-ph="msg_ph" rows="1"></textarea>
      <button id="sendBtn" title="Send">
        <svg class="icon" viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        <span class="btn-label" data-ci18n="send">Отправить</span>
      </button>
    </div>
  </main>

  <aside class="info-panel" id="infoPanel">
    <button class="icon-btn close-btn" id="infoCloseBtn" title="Close">
      <svg class="icon" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
    <div id="infoContent">
      <div class="info-empty">
        <svg class="icon huge" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
        <div data-ci18n="info_empty">Информация о собеседнике появится здесь</div>
      </div>
    </div>
  </aside>

</div>

<div class="toast-wrap" id="toastWrap"></div>

<script>
__I18N_JS__

/* ==== Chat-specific words ==== */
I18N.ru.you = "Вы вошли как";
I18N.en.you = "Signed in as";
I18N.ru.add_contact = "Добавить контакт";
I18N.en.add_contact = "Add contact";
I18N.ru.nick_ph = "Ник собеседника";
I18N.en.nick_ph = "Contact nick";
I18N.ru.search_ph = "Поиск по контактам";
I18N.en.search_ph = "Search contacts";
I18N.ru.pick_peer = "Выберите собеседника";
I18N.en.pick_peer = "Pick a contact";
I18N.ru.online = "в сети";
I18N.en.online = "online";
I18N.ru.was_online = "был(а):";
I18N.en.was_online = "was online:";
I18N.ru.just_now = "только что";
I18N.en.just_now = "just now";
I18N.ru.min_ago = "мин назад";
I18N.en.min_ago = "min ago";
I18N.ru.hour_ago = "ч назад";
I18N.en.hour_ago = "h ago";
I18N.ru.long_ago = "давно";
I18N.en.long_ago = "long ago";
I18N.ru.today = "Сегодня";
I18N.en.today = "Today";
I18N.ru.yesterday = "Вчера";
I18N.en.yesterday = "Yesterday";
I18N.ru.info_empty = "Информация о собеседнике появится здесь";
I18N.en.info_empty = "Contact info will appear here";
I18N.ru.nick = "Ник";
I18N.en.nick = "Nick";
I18N.ru.since = "В SldChat с";
I18N.en.since = "On SldChat since";
I18N.ru.last_activity = "Последняя активность";
I18N.en.last_activity = "Last activity";
I18N.ru.msg_count = "Сообщений в диалоге";
I18N.en.msg_count = "Messages in dialog";
I18N.ru.remove_contact = "Удалить контакт";
I18N.en.remove_contact = "Remove contact";
I18N.ru.send = "Отправить";
I18N.en.send = "Send";
I18N.ru.msg_ph = "Введите сообщение...";
I18N.en.msg_ph = "Type a message...";
I18N.ru.in_network = "В сети";
I18N.en.in_network = "Online";
I18N.ru.empty_pick = "Слева выберите контакт, чтобы начать переписку";
I18N.en.empty_pick = "Pick a contact on the left to start chatting";
I18N.ru.no_contacts = "Пока нет контактов.<br>Нажмите «Добавить контакт».";
I18N.en.no_contacts = "No contacts yet.<br>Press Add contact.";
I18N.ru.not_found = "Ничего не найдено";
I18N.en.not_found = "Nothing found";
I18N.ru.no_messages = "Нет сообщений";
I18N.en.no_messages = "No messages";
I18N.ru.you_prefix = "Вы: ";
I18N.en.you_prefix = "You: ";
I18N.ru.confirm_remove = "Удалить контакт {nick}?";
I18N.en.confirm_remove = "Remove contact {nick}?";
I18N.ru.removed = "Контакт удалён: ";
I18N.en.removed = "Contact removed: ";
I18N.ru.added = "Добавлено: ";
I18N.en.added = "Added: ";
I18N.ru.new_contact = "Новый контакт: ";
I18N.en.new_contact = "New contact: ";

/* ==================== state ==================== */
var lang = SldLang.pickInitial();
localStorage.setItem('sld_lang', lang);

function T(key){
  var d = I18N[lang] || I18N.ru;
  return d[key] != null ? d[key] : (I18N.ru[key] || key);
}
function E(key){
  return SldLang.tr(key, lang);
}
function errText(payload){
  return SldLang.errText(payload, lang);
}

function applyCI18n(){
  document.documentElement.lang = lang;
  document.querySelectorAll('[data-ci18n]').forEach(function(el){
    var k = el.getAttribute('data-ci18n');
    if (el.id === 'chatTitle' && current) return;
    if (el.id === 'chatTitle') { el.textContent = T('pick_peer'); return; }
    if (I18N[lang][k] != null) el.textContent = I18N[lang][k];
  });
  document.querySelectorAll('[data-ci18n-ph]').forEach(function(el){
    var k = el.getAttribute('data-ci18n-ph');
    if (I18N[lang][k] != null) el.setAttribute('placeholder', I18N[lang][k]);
  });
  var v = document.getElementById('chatLangValue');
  if (v) v.textContent = (lang === 'ru') ? 'RU' : 'EN';
}

var me = null;
var current = null;
var lastIds = {};
var renderedIds = {};
var usersCache = [];
var searchQuery = '';
var currentInfo = null;
var ws = null;
var wsReconnectTimer = null;
var lastContactsSig = '';
var lastInfoSig = '';

function $(id){ return document.getElementById(id); }
var app = $('app');

function esc(s) {
  return String(s).replace(/[&<>"']/g, function(c){
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
  });
}
function pad2(n){ return (n < 10 ? '0' : '') + n; }
function fmtTime(ts){ var d = new Date(ts * 1000); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }
function fmtDateTime(ts){ if (!ts) return '—'; var d = new Date(ts * 1000); return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear(); }
function fmtLastSeen(ts){
  if (!ts) return T('long_ago');
  var diff = Date.now() / 1000 - ts;
  if (diff < 60) return T('just_now');
  if (diff < 3600) return Math.floor(diff / 60) + ' ' + T('min_ago');
  if (diff < 86400) return Math.floor(diff / 3600) + ' ' + T('hour_ago');
  return fmtDateTime(ts);
}
function dayLabel(ts){
  var d = new Date(ts * 1000);
  var today = new Date();
  var yest = new Date(); yest.setDate(yest.getDate() - 1);
  var same = function(a, b){ return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); };
  if (same(d, today)) return T('today');
  if (same(d, yest)) return T('yesterday');
  return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear();
}
function scrollIfNearBottom(el){ return el.scrollHeight - el.scrollTop - el.clientHeight < 160; }
function toast(msg){
  var el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  $('toastWrap').appendChild(el);
  setTimeout(function(){ el.classList.add('out'); setTimeout(function(){ el.remove(); }, 250); }, 3000);
}

function contactsSignature(data){
  var s = '';
  for (var i = 0; i < data.length; i++) {
    var u = data[i];
    s += u.nick + '|' + (u.online ? 1 : 0) + '|' + (u.unread || 0) + '|' + (u.last ? u.last.id : 0) + ';';
  }
  return s;
}
function infoSignature(i){
  return i.nick + '|' + (i.online ? 1 : 0) + '|' + Math.floor(i.last_seen || 0) + '|' + (i.msg_count || 0);
}

function connectWS(){
  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try { ws = new WebSocket(proto + '//' + location.host + '/ws'); } catch (e) { scheduleReconnect(); return; }
  ws.onopen = function(){
    if (ws._pingTimer) clearInterval(ws._pingTimer);
    ws._pingTimer = setInterval(function(){
      if (ws && ws.readyState === 1) { try { ws.send('ping'); } catch (e) {} }
    }, 25000);
  };
  ws.onmessage = function(e){
    var d; try { d = JSON.parse(e.data); } catch (_) { return; }
    handleWS(d);
  };
  ws.onclose = function(){ if (ws && ws._pingTimer) clearInterval(ws._pingTimer); scheduleReconnect(); };
  ws.onerror = function(){ try { ws.close(); } catch (e) {} };
}
function scheduleReconnect(){
  if (wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(function(){ wsReconnectTimer = null; connectWS(); }, 1200);
}

function handleWS(d){
  if (d.type === 'message') {
    var m = d.message;
    var peer = m.from === me ? m.to : m.from;
    if (current === peer) {
      if (!renderedIds[m.id]) {
        renderedIds[m.id] = true;
        appendMessage(m);
        lastIds[current] = Math.max(lastIds[current] || 0, m.id);
      }
      if (m.from !== me) markRead(peer);
      loadInfo(current);
    }
    loadContacts();
  } else if (d.type === 'presence') {
    var u;
    for (var i = 0; i < usersCache.length; i++) if (usersCache[i].nick === d.nick) u = usersCache[i];
    if (u) { u.online = d.online; lastContactsSig = ''; renderContacts(); }
    if (current === d.nick) {
      currentInfo = Object.assign({}, currentInfo || {}, { online: d.online });
      updateChatSubtitle();
    }
  } else if (d.type === 'contact_added') {
    toast(T('new_contact') + d.nick);
    lastContactsSig = '';
    loadContacts();
  } else if (d.type === 'contact_removed') {
    toast(T('removed') + d.nick);
    if (current === d.nick) resetChatToEmpty();
    lastContactsSig = '';
    loadContacts();
  }
}

async function init(){
  var r = await fetch('/api/me');
  if (!r.ok) { location.href = '/'; return; }
  var d = await r.json();
  me = d.nick;
  $('myNick').textContent = me;
  connectWS();
  await loadContacts();
}

async function loadContacts(){
  var r = await fetch('/api/contacts');
  if (!r.ok) { location.href = '/'; return; }
  var d = await r.json();
  usersCache = d.contacts || [];
  var sig = contactsSignature(usersCache);
  if (sig === lastContactsSig) return;
  lastContactsSig = sig;
  renderContacts();
}

function renderContacts(){
  var list = $('userList');
  var scrollTop = list.scrollTop;
  list.innerHTML = '';
  var q = searchQuery.trim().toLowerCase();
  var filtered = [];
  for (var i = 0; i < usersCache.length; i++) {
    if (!q || usersCache[i].nick.toLowerCase().indexOf(q) >= 0) filtered.push(usersCache[i]);
  }
  if (!filtered.length) {
    var e = document.createElement('div');
    e.className = 'empty-list';
    e.innerHTML = '<svg class="icon huge" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg><div>' + (q ? T('not_found') : T('no_contacts')) + '</div>';
    list.appendChild(e);
    list.scrollTop = scrollTop;
    return;
  }
  for (var j = 0; j < filtered.length; j++) {
    var u = filtered[j];
    var div = document.createElement('div');
    div.className = 'user-item' + (u.nick === current ? ' active' : '');
    var preview = T('no_messages');
    if (u.last) {
      var prefix = u.last.from === me ? T('you_prefix') : '';
      preview = prefix + u.last.text;
    }
    var timeStr = u.last ? fmtTime(u.last.time) : '';
    var unread = u.unread ? '<span class="badge">' + (u.unread > 99 ? '99+' : u.unread) + '</span>' : '';
    div.innerHTML = '<div class="row1"><div class="nick"><span class="dot' + (u.online ? ' on' : '') + '"></span>' + esc(u.nick) + '</div><div class="time">' + timeStr + unread + '</div></div><div class="preview">' + esc(preview) + '</div>';
    (function(nick){ div.onclick = function(){ openDialog(nick); }; })(u.nick);
    list.appendChild(div);
  }
  list.scrollTop = scrollTop;
}

async function openDialog(nick){
  if (current === nick) {
    app.classList.remove('info-open');
    $('msgInput').focus();
    return;
  }
  current = nick;
  lastIds[nick] = 0;
  renderedIds = {};
  lastInfoSig = '';
  $('chatTitle').textContent = nick;
  $('chatSub').textContent = '';
  $('messages').innerHTML = '';
  $('inputArea').style.display = 'flex';
  app.classList.add('chat-open');
  app.classList.remove('info-open');
  await Promise.all([loadInfo(nick), refreshDialog()]);
  await loadContacts();
  $('msgInput').focus();
}

function resetChatToEmpty(){
  current = null;
  lastInfoSig = '';
  app.classList.remove('chat-open');
  app.classList.remove('info-open');
  $('chatTitle').textContent = T('pick_peer');
  $('chatSub').textContent = '';
  $('messages').innerHTML =
    '<div class="empty">' +
      '<svg class="icon huge" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>' +
      '<div>' + esc(T('empty_pick')) + '</div>' +
    '</div>';
  $('inputArea').style.display = 'none';
  renderInfoEmpty();
}

async function refreshDialog(){
  if (!current) return;
  var nick = current;
  var since = lastIds[nick] || 0;
  var r = await fetch('/api/dialog/' + encodeURIComponent(nick) + '?since=' + since);
  if (!r.ok) return;
  var d = await r.json();
  if (!d.ok || current !== nick) return;
  if (!d.messages || !d.messages.length) return;
  var box = $('messages');
  var wasNearBottom = scrollIfNearBottom(box);
  var added = false;
  for (var i = 0; i < d.messages.length; i++) {
    var m = d.messages[i];
    if (renderedIds[m.id]) continue;
    renderedIds[m.id] = true;
    lastIds[nick] = Math.max(lastIds[nick] || 0, m.id);
    appendMessage(m);
    added = true;
  }
  if (added && (wasNearBottom || !box.querySelector('.msg-row'))) box.scrollTop = box.scrollHeight;
}

function appendMessage(m){
  var box = $('messages');
  var lastRow = box.querySelector('.msg-row:last-of-type');
  var lastTs = lastRow ? parseFloat(lastRow.dataset.ts || '0') : 0;
  var newDay = dayLabel(m.time);
  if (!lastTs || dayLabel(lastTs) !== newDay) {
    var sep = document.createElement('div');
    sep.className = 'date-sep';
    sep.innerHTML = '<span>' + esc(newDay) + '</span>';
    box.appendChild(sep);
  }
  var row = document.createElement('div');
  row.className = 'msg-row' + (m.from === me ? ' mine' : '');
  row.dataset.id = m.id;
  row.dataset.ts = m.time;
  row.innerHTML = '<div class="msg-bubble">' + esc(m.text) + '<span class="msg-meta">' + fmtTime(m.time) + '</span></div>';
  box.appendChild(row);
  var nearBottom = scrollIfNearBottom(box) || m.from === me;
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

async function markRead(nick){
  try { await fetch('/api/dialog/' + encodeURIComponent(nick) + '?since=0'); } catch (e) {}
  loadContacts();
}

async function send(){
  if (!current) return;
  var inp = $('msgInput');
  var text = inp.value.trim();
  if (!text) return;
  var fd = new FormData();
  fd.append('to', current);
  fd.append('text', text);
  inp.value = '';
  inp.style.height = '36px';
  var r = await fetch('/api/send', { method: 'POST', body: fd });
  var d = await r.json().catch(function(){ return { ok:false }; });
  if (!d.ok) { toast(errText(d) || E('send_failed')); return; }
  if (!renderedIds[d.message.id]) {
    renderedIds[d.message.id] = true;
    lastIds[current] = Math.max(lastIds[current] || 0, d.message.id);
    appendMessage(d.message);
  }
  loadContacts();
  loadInfo(current);
}

function renderInfoEmpty(){
  $('infoContent').innerHTML =
    '<div class="info-empty">' +
      '<svg class="icon huge" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>' +
      '<div>' + esc(T('info_empty')) + '</div>' +
    '</div>';
}

async function loadInfo(nick){
  if (!nick) { renderInfoEmpty(); lastInfoSig = ''; return; }
  var r = await fetch('/api/user/' + encodeURIComponent(nick) + '/info');
  if (!r.ok) return;
  var d = await r.json();
  if (!d.ok) return;
  var i = d.info;
  var sig = infoSignature(i);
  if (sig === lastInfoSig) return;
  lastInfoSig = sig;
  currentInfo = { online: i.online, lastSeen: i.last_seen };
  var status = i.online
    ? '<div class="status online">' + esc(T('in_network')) + '</div>'
    : '<div class="status">' + esc(T('was_online')) + ' ' + esc(fmtLastSeen(i.last_seen)) + '</div>';
  var html =
    '<div class="info-head"><div class="name">' + esc(i.nick) + '</div>' + status + '</div>' +
    '<div class="info-row"><span class="k">' + esc(T('nick')) + '</span><span class="v">' + esc(i.nick) + '</span></div>' +
    '<div class="info-row"><span class="k">' + esc(T('since')) + '</span><span class="v">' + esc(fmtDateTime(i.created)) + '</span></div>' +
    '<div class="info-row"><span class="k">' + esc(T('last_activity')) + '</span><span class="v">' + esc(fmtLastSeen(i.last_seen)) + '</span></div>' +
    '<div class="info-row"><span class="k">' + esc(T('msg_count')) + '</span><span class="v">' + i.msg_count + '</span></div>';
  if (i.in_contacts) {
    html += '<button class="remove-btn" id="removeBtn">' +
      '<svg class="icon" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>' +
      esc(T('remove_contact')) + '</button>';
  }
  $('infoContent').innerHTML = html;
  updateChatSubtitle();
  var btn = document.getElementById('removeBtn');
  if (btn) btn.onclick = function(){ removeContact(nick); };
}

async function removeContact(nick){
  if (!confirm(T('confirm_remove').replace('{nick}', nick))) return;
  var fd = new FormData();
  fd.append('nick', nick);
  var r = await fetch('/api/contacts/remove', { method: 'POST', body: fd });
  var d = await r.json().catch(function(){ return { ok:false }; });
  if (!d.ok) { toast(errText(d) || E('error')); return; }
  toast(T('removed') + nick);
  if (current === nick) resetChatToEmpty();
  lastContactsSig = '';
  loadContacts();
}

function updateChatSubtitle(){
  var el = $('chatSub');
  if (!current || !currentInfo) { el.textContent = ''; return; }
  if (currentInfo.online) { el.textContent = T('online'); el.classList.add('online'); }
  else { el.textContent = T('was_online') + ' ' + fmtLastSeen(currentInfo.lastSeen); el.classList.remove('online'); }
}

function wireDropdown(root, onSelect){
  if (!root) return;
  var toggle = root.querySelector('.dd-toggle');
  var menu = root.querySelector('.dd-menu');
  if (!toggle || !menu) return;
  toggle.addEventListener('click', function(e){
    e.stopPropagation();
    var opened = document.querySelectorAll('.dd.open');
    for (var i = 0; i < opened.length; i++) if (opened[i] !== root) opened[i].classList.remove('open');
    root.classList.toggle('open');
  });
  menu.querySelectorAll('li').forEach(function(li){
    li.addEventListener('click', function(){
      var v = li.dataset.value;
      root.classList.remove('open');
      if (onSelect) onSelect(v);
    });
  });
  document.addEventListener('click', function(){ root.classList.remove('open'); });
}
function markActive(root, val){
  if (!root) return;
  var menu = root.querySelector('.dd-menu');
  if (!menu) return;
  menu.querySelectorAll('li').forEach(function(li){
    if (li.dataset.value === val) li.classList.add('active'); else li.classList.remove('active');
  });
}

$('addBtn').onclick = function(){
  var f = $('addForm');
  f.classList.toggle('open');
  if (f.classList.contains('open')) setTimeout(function(){ $('addInput').focus(); }, 60);
  $('addMsg').textContent = '';
};

$('addForm').onsubmit = async function(e){
  e.preventDefault();
  var inp = $('addInput');
  var msg = $('addMsg');
  var nick = inp.value.trim();
  if (!nick) return;
  msg.className = 'add-msg';
  msg.textContent = '...';
  var fd = new FormData();
  fd.append('nick', nick);
  var r = await fetch('/api/contacts/add', { method: 'POST', body: fd });
  var d = await r.json().catch(function(){ return { ok:false }; });
  if (d.ok) {
    msg.className = 'add-msg ok';
    msg.textContent = T('added') + nick;
    inp.value = '';
    lastContactsSig = '';
    await loadContacts();
    setTimeout(function(){ $('addForm').classList.remove('open'); $('addMsg').textContent = ''; }, 800);
    openDialog(nick);
  } else {
    msg.className = 'add-msg err';
    msg.textContent = errText(d) || E('error');
  }
};

$('sendBtn').onclick = send;
$('msgInput').addEventListener('keydown', function(e){
  if (e.key === 'Enter' && !e.shiftKey && window.innerWidth > 900) { e.preventDefault(); send(); }
});
$('msgInput').addEventListener('input', function(e){
  var el = e.target;
  el.style.height = '36px';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
});
$('logoutBtn').onclick = async function(){
  if (ws) { try { ws.close(); } catch (e) {} }
  await fetch('/api/logout', { method: 'POST' });
  location.href = '/';
};
$('backBtn').onclick = resetChatToEmpty;
$('infoBtn').onclick = function(){ app.classList.toggle('info-open'); };
$('infoCloseBtn').onclick = function(){ app.classList.remove('info-open'); };
$('searchInput').addEventListener('input', function(e){ searchQuery = e.target.value; renderContacts(); });

wireDropdown($('chatLangDd'), function(v){
  lang = v;
  localStorage.setItem('sld_lang', v);
  SldLang.setGlobalUnits(v);
  try {
    var u = new URL(location.href);
    u.searchParams.set('lang', v);
    history.replaceState(null, '', u.pathname + u.search + u.hash);
  } catch (e) {}
  applyCI18n();
  markActive($('chatLangDd'), v);
  lastContactsSig = '';
  lastInfoSig = '';
  renderContacts();
  if (current) { loadInfo(current); updateChatSubtitle(); }
  else { $('chatTitle').textContent = T('pick_peer'); }
});

applyCI18n();
markActive($('chatLangDd'), lang);
SldLang.setGlobalUnits(lang);

setInterval(async function(){
  if (ws && ws.readyState === 1) return;
  await loadContacts();
  if (current) { await refreshDialog(); await loadInfo(current); }
}, 30000);

init();
</script>
</body>
</html>
"""


# ================== МАРШРУТЫ ==================
def render_page(html: str, base_url: str = "") -> str:
    return (html
        .replace("__SHARED_CSS__", SHARED_CSS)
        .replace("__FOOTER__", FOOTER_HTML)
        .replace("__HEAD_COMMON__", HEAD_COMMON)
        .replace("__I18N_JS__", I18N_JS)
        .replace("__BASE_URL__", base_url))


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    if current_user(request):
        return RedirectResponse("/chat")
    return render_page(LANDING_PAGE, str(request.base_url))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/chat")
    return render_auth("login")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if current_user(request):
        return RedirectResponse("/chat")
    return render_auth("register")


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/")
    return CHAT_PAGE


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return render_page(PRIVACY_PAGE)


@app.get("/support", response_class=HTMLResponse)
async def support_page():
    return render_page(SUPPORT_PAGE)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
