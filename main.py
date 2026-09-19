"""
SldChat — простой мессенджер на Python + FastAPI + WebSocket.
Запуск:  python main.py     (или: uvicorn main:app --host 0.0.0.0 --port 8000)
Все данные живут в оперативной памяти процесса.
"""

import asyncio
import json
import secrets
import time
from typing import Dict, List, Optional, Set

import uvicorn
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(title="SldChat")

# ================== ХРАНИЛИЩЕ (в оперативке) ==================
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
    return {
        "nick": nick,
        "online": is_online(nick),
        "last_seen": u.get("last_seen", 0),
        "created": u.get("created", 0),
    }


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
        return JSONResponse({"ok": False, "error": "Заполните все поля"}, status_code=400)
    if not (3 <= len(nick) <= 20):
        return JSONResponse({"ok": False, "error": "Ник: 3–20 символов"}, status_code=400)
    if not nick.replace("_", "").isalnum():
        return JSONResponse({"ok": False, "error": "Ник: только буквы, цифры и _"}, status_code=400)
    if len(password) < 3:
        return JSONResponse({"ok": False, "error": "Пароль: минимум 3 символа"}, status_code=400)
    if nick in users:
        return JSONResponse({"ok": False, "error": "Ник уже занят"}, status_code=400)

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
        return JSONResponse({"ok": False, "error": "Неверный ник или пароль"}, status_code=400)
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


@app.post("/api/contacts/add")
async def api_contacts_add(request: Request, nick: str = Form(...)):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    nick = nick.strip()
    if not nick:
        return JSONResponse({"ok": False, "error": "Введите ник"}, status_code=400)
    if nick == me:
        return JSONResponse({"ok": False, "error": "Нельзя добавить себя"}, status_code=400)
    if nick not in users:
        return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=404)

    contacts.setdefault(me, set())
    contacts.setdefault(nick, set())
    if nick in contacts[me]:
        return JSONResponse({"ok": False, "error": "Уже в контактах"}, status_code=400)

    contacts[me].add(nick)
    contacts[nick].add(me)

    info = user_public_info(nick)
    await asyncio.gather(
        send_ws(nick, {"type": "contact_added", "nick": me}),
        send_ws(me, {"type": "contact_added", "nick": nick}),
    )
    return {"ok": True, "contact": {"nick": nick, "last": None, "unread": 0,
                                    "online": info["online"], "last_seen": info["last_seen"]}}


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
        out.append({
            "nick": nick,
            "last": last_msg,
            "unread": unread,
            "online": is_online(nick),
            "last_seen": users[nick].get("last_seen", 0),
        })
    out.sort(key=lambda u: (u["last"]["id"] if u["last"] else 0), reverse=True)
    return {"ok": True, "contacts": out}


@app.get("/api/user/{nick}/info")
async def api_user_info(nick: str, request: Request):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)
    if nick not in users:
        return JSONResponse({"ok": False, "error": "Не найден"}, status_code=404)
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
        return JSONResponse({"ok": False, "error": "Не в контактах"}, status_code=403)

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
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    to = to.strip()
    text = text.strip()
    if not text:
        return JSONResponse({"ok": False, "error": "Пустое сообщение"}, status_code=400)
    if len(text) > 4000:
        text = text[:4000]
    if to not in contacts.get(me, set()):
        return JSONResponse({"ok": False, "error": "Не в контактах"}, status_code=403)

    _msg_id += 1
    msg = {"id": _msg_id, "from": me, "to": to, "text": text, "time": time.time()}
    messages.append(msg)

    payload = {"type": "message", "message": msg}
    await asyncio.gather(send_ws(to, payload), send_ws(me, payload))
    return {"ok": True, "message": msg}


# ================== SHARED CSS ==================
SHARED_CSS = r"""
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: Tahoma, Verdana, Arial, sans-serif;
    font-size: 14px; color: #2b3a4a; line-height: 1.6;
    background: #eef2f6;
    -webkit-user-select: none; -moz-user-select: none; user-select: none;
    -webkit-font-smoothing: antialiased;
    animation: fadeIn 0.35s ease both;
  }
  input, textarea, .selectable { -webkit-user-select: text; -moz-user-select: text; user-select: text; }

  * { scrollbar-width: none; -ms-overflow-style: none; }
  *::-webkit-scrollbar { width: 0; height: 0; display: none; }

  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(12px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  @keyframes popIn {
    0%   { opacity: 0; transform: scale(0.94); }
    60%  { opacity: 1; transform: scale(1.02); }
    100% { opacity: 1; transform: scale(1); }
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
  .btn:hover { background: linear-gradient(#fff, #dbe3ea); transform: translateY(-1px);
               box-shadow: 0 2px 5px rgba(0,0,0,0.08); }
  .btn:active { transform: translateY(0) scale(0.98); box-shadow: inset 0 1px 2px rgba(0,0,0,0.15); }
  .btn-primary {
    background: linear-gradient(#5b8fc4, #3f6fa8);
    border-color: #35597f; color: #fff;
    text-shadow: 0 1px 0 rgba(0,0,0,0.2);
  }
  .btn-primary:hover { background: linear-gradient(#699bcd, #4577b1);
                       box-shadow: 0 2px 8px rgba(63,111,168,0.35); }
  .btn-lg { padding: 11px 22px; font-size: 15px; }

  .section { padding: 64px 0; border-bottom: 1px solid #dbe1e7; }
  .section:nth-of-type(even) { background: #f6f8fa; }
  .section h2 {
    font-size: 27px; font-weight: normal; margin: 0 0 8px; color: #23374b;
    text-shadow: 0 1px 0 #fff;
  }
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
    color: #3f6fa8;
    box-shadow: inset 0 1px 0 #fff;
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
    width: 12px; text-align: center;
    transition: transform 0.25s ease;
  }
  .faq details[open] summary::before { content: '−'; transform: rotate(180deg); }
  .faq .answer-wrap {
    display: grid; grid-template-rows: 0fr;
    transition: grid-template-rows 0.28s ease;
  }
  .faq details[open] .answer-wrap { grid-template-rows: 1fr; }
  .faq .answer-wrap > .answer {
    overflow: hidden;
    padding: 0 18px 0 40px; color: #55677b; font-size: 13px;
    transition: padding 0.28s ease;
  }
  .faq details[open] .answer-wrap > .answer { padding: 0 18px 15px 40px; }

  footer {
    background: #2b3a4a; color: #b8c4ce;
    padding: 44px 0 26px; font-size: 13px;
  }
  footer .foot-cols {
    display: grid;
    grid-template-columns: 1.4fr 1fr 1fr 1fr;
    gap: 30px;
    margin-bottom: 34px;
  }
  footer .foot-brand .brand-name {
    display: flex; align-items: center; gap: 8px;
    font-size: 17px; color: #fff; font-weight: bold;
    margin-bottom: 10px;
  }
  footer .foot-brand .brand-name span { color: #9db8d3; }
  footer .foot-brand .brand-name .icon { color: #9db8d3; width: 22px; height: 22px; }
  footer .foot-brand p {
    margin: 0; color: #8fa3b6; font-size: 12px; line-height: 1.6;
    max-width: 260px;
  }
  footer .foot-col h4 {
    margin: 0 0 12px; font-size: 12px; text-transform: uppercase;
    letter-spacing: 1px; color: #8fa3b6; font-weight: bold;
  }
  footer .foot-col ul { list-style: none; padding: 0; margin: 0; }
  footer .foot-col li { margin-bottom: 7px; }
  footer .foot-col a { color: #b8c4ce; font-size: 13px; transition: color 0.14s ease; }
  footer .foot-col a:hover { color: #fff; }
  footer .foot-bottom {
    border-top: 1px solid #3d4d5d;
    padding-top: 20px;
    display: flex; align-items: center; justify-content: space-between;
    gap: 14px; flex-wrap: wrap;
    font-size: 12px; color: #7a8b9c;
  }
  footer .foot-bottom .legal { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  footer .foot-bottom a { color: #9db8d3; }

  .page-wrap { max-width: 820px; margin: 0 auto; padding: 40px 20px 60px; }
  .page-head { margin-bottom: 28px; }
  .page-head h1 {
    font-size: 32px; font-weight: normal; color: #23374b;
    margin: 0 0 6px; text-shadow: 0 1px 0 #fff;
  }
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
    margin-top: 6px;
    transition: gap 0.15s ease;
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
  }
"""


FOOTER_HTML = """
<footer>
  <div class="container">
    <div class="foot-cols">
      <div class="foot-brand">
        <div class="brand-name">
          <svg class="icon" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
          Sld<span>Chat</span>
        </div>
        <p>SldChat — независимый мессенджер для небольших команд, созданный командой SldChat Team.</p>
      </div>
      <div class="foot-col">
        <h4>Продукт</h4>
        <ul>
          <li><a href="/#features">Возможности</a></li>
          <li><a href="/#servers">Серверы</a></li>
          <li><a href="/register">Регистрация</a></li>
          <li><a href="/login">Вход</a></li>
        </ul>
      </div>
      <div class="foot-col">
        <h4>Компания</h4>
        <ul>
          <li><a href="/privacy">Приватность</a></li>
          <li><a href="/support">Поддержка</a></li>
          <li><a href="/#faq">FAQ</a></li>
        </ul>
      </div>
      <div class="foot-col">
        <h4>Связь</h4>
        <ul>
          <li><a href="mailto:sldshr.confirmation@gmail.com">Почта</a></li>
          <li><a href="https://discord.com/users/sldshr" target="_blank" rel="noopener">Discord: sldshr</a></li>
        </ul>
      </div>
    </div>
    <div class="foot-bottom">
      <div>© 2026 SldChat Team. Все права защищены.</div>
      <div class="legal">
        <a href="/privacy">Политика приватности</a>
        <a href="/support">Поддержка</a>
      </div>
    </div>
  </div>
</footer>
"""


# ================== LANDING ==================
LANDING_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SldChat — быстрый мессенджер для команд</title>
<meta name="description" content="SldChat — независимый мессенджер: регистрация за 5 секунд, добавление по нику, мгновенная доставка через WebSocket.">
<style>
__SHARED_CSS__

  .hero {
    background:
      radial-gradient(circle at 20% 20%, #e6eef7 0%, transparent 60%),
      linear-gradient(#d5dfe9, #b8c6d3);
    border-bottom: 1px solid #a8b5c2;
    padding: 84px 0 92px;
    text-align: center; position: relative; overflow: hidden;
  }
  .hero::after {
    content: ''; position: absolute; left: 0; right: 0; bottom: 0;
    height: 1px; background: rgba(255,255,255,0.6);
  }
  .hero .eyebrow {
    display: inline-flex; align-items: center; gap: 8px;
    background: rgba(255,255,255,0.6);
    border: 1px solid #b7c8db;
    border-radius: 20px;
    padding: 5px 14px 5px 12px;
    font-size: 11px; letter-spacing: 0.5px;
    text-transform: uppercase; font-weight: bold;
    color: #3a5169;
    margin-bottom: 24px;
  }
  .hero .eyebrow .icon { width: 13px; height: 13px; color: #3f6fa8; }
  .hero h1 {
    font-size: 46px; font-weight: normal; margin: 0 0 18px;
    color: #23374b; text-shadow: 0 1px 0 #fff; line-height: 1.12;
    letter-spacing: 0.4px;
  }
  .hero h1 b { color: #3f6fa8; font-weight: bold; }
  .hero p {
    max-width: 620px; margin: 0 auto 32px;
    color: #4a5f74; font-size: 16px;
  }
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
    word-break: break-all;
  }
  .server-card .desc { font-size: 12px; color: #7a8695; margin-top: 3px; }
  .server-card .tag {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px;
    color: #2f8f3d; font-weight: bold;
    padding: 2px 8px; border-radius: 10px; background: #e5f3e7;
    align-self: flex-start; flex-shrink: 0;
  }
  .server-card .tag.alt { color: #3f6fa8; background: #e6eef7; }

  .notice {
    margin-top: 22px;
    background: #fff8e1; border: 1px solid #ecdc9b; border-radius: 6px;
    padding: 14px 16px;
    display: flex; gap: 12px; align-items: flex-start;
    color: #6f5a13; font-size: 13px;
  }
  .notice .icon { color: #b8860b; flex-shrink: 0; margin-top: 2px; }
  .notice b { color: #4f3e07; }

  .privacy-mini {
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px;
    margin-bottom: 20px;
  }
  .privacy-item {
    background: #fff; border: 1px solid #cfd7df; border-radius: 6px;
    padding: 16px 18px;
    display: flex; gap: 12px; align-items: flex-start;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    transition: transform 0.18s ease, box-shadow 0.18s ease;
  }
  .privacy-item:hover { transform: translateY(-2px); box-shadow: 0 6px 14px rgba(0,0,0,0.08); }
  .privacy-item .ico {
    width: 36px; height: 36px; flex-shrink: 0;
    border-radius: 8px;
    background: linear-gradient(#e4ebf2, #c8d3de);
    border: 1px solid #b7c2cd;
    display: flex; align-items: center; justify-content: center;
    color: #3f6fa8;
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
      <a class="navlink" href="#features">Возможности</a>
      <a class="navlink" href="#servers">Серверы</a>
      <a class="navlink" href="#privacy">Приватность</a>
      <a class="navlink" href="#faq">FAQ</a>
      <a class="navlink" href="/support">Поддержка</a>
      <a class="btn" href="/login">Войти</a>
      <a class="btn btn-primary" href="/register">Регистрация</a>
    </nav>
  </div>
</header>

<section class="hero">
  <div class="container">
    <span class="eyebrow">
      <svg class="icon" viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
      Быстрая доставка через WebSocket
    </span>
    <h1>Мессенджер <b>SldChat</b> —<br>общайтесь по-простому</h1>
    <p>Никаких лишних настроек. Регистрация за 5 секунд, добавление по нику,
    мгновенная доставка сообщений. Работает на телефоне и на компьютере.</p>
    <div class="hero-actions">
      <a class="btn btn-primary btn-lg" href="/register">Создать аккаунт</a>
      <a class="btn btn-lg" href="/login">У меня уже есть аккаунт</a>
    </div>
    <div class="hero-meta">
      <span>
        <svg class="icon" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
        Без e-mail и телефона
      </span>
      <span>
        <svg class="icon" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
        Без рекламы
      </span>
      <span>
        <svg class="icon" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
        Бесплатно
      </span>
    </div>
  </div>
</section>

<section id="features" class="section">
  <div class="container">
    <h2>Возможности</h2>
    <p class="lead">Всё, что нужно для быстрого общения — и ничего лишнего.</p>
    <div class="cards">
      <div class="card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
        </div>
        <h3>Мгновенная доставка</h3>
        <p>Сообщения летят через WebSocket — собеседник видит их за миллисекунды, без перезагрузок.</p>
      </div>
      <div class="card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>
        </div>
        <h3>Добавление по нику</h3>
        <p>Никаких публичных каталогов. Ввели ник — и вы с человеком сразу друг у друга в контактах.</p>
      </div>
      <div class="card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
        </div>
        <h3>Статус «в сети»</h3>
        <p>Видите, кто онлайн прямо сейчас, а кто заходил недавно — без лишних деталей.</p>
      </div>
      <div class="card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>
        </div>
        <h3>На любом устройстве</h3>
        <p>Один и тот же интерфейс на компьютере и на смартфоне — с удобной адаптацией под экран.</p>
      </div>
      <div class="card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        </div>
        <h3>Поиск по контактам</h3>
        <p>Быстрый поиск среди ваших собеседников прямо в списке — с фильтрацией по мере ввода.</p>
      </div>
      <div class="card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        </div>
        <h3>Приватность по умолчанию</h3>
        <p>Минимум собираемых данных. Одна cookie для сессии, никакой аналитики и трекеров.</p>
      </div>
    </div>
  </div>
</section>

<section id="servers" class="section">
  <div class="container">
    <h2>Серверы SldChat</h2>
    <p class="lead">Проект работает на двух независимых серверах. Выбирайте любой — данные между ними не передаются.</p>

    <div class="servers">
      <div class="server-card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
        </div>
        <div class="body">
          <div class="url">sldchat.fastapicloud.dev</div>
          <div class="desc">FastAPI Cloud</div>
        </div>
        <span class="tag">Основной</span>
      </div>
      <div class="server-card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
        </div>
        <div class="body">
          <div class="url">sldchat.onrunxbuild.com</div>
          <div class="desc">onrunxbuild</div>
        </div>
        <span class="tag alt">Резерв</span>
      </div>
    </div>

    <div class="notice">
      <svg class="icon lg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
      <div>
        <b>Это разные серверы.</b> У каждого своя база пользователей и сообщений —
        данные между ними <b>не передаются</b>. Чтобы общаться на двух серверах сразу,
        зарегистрируйтесь на каждом отдельно.
      </div>
    </div>
  </div>
</section>

<section id="privacy" class="section">
  <div class="container">
    <h2>Приватность</h2>
    <p class="lead">Коротко о самом главном. Подробности — на отдельной странице.</p>

    <div class="privacy-mini">
      <div class="privacy-item">
        <div class="ico">
          <svg class="icon" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        </div>
        <div>
          <h3>Ничего не пишем на диск</h3>
          <p>Сообщения и данные аккаунта существуют, пока работает сервер.</p>
        </div>
      </div>
      <div class="privacy-item">
        <div class="ico">
          <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
        </div>
        <div>
          <h3>Без аналитики и рекламы</h3>
          <p>Никаких трекеров, пикселей и сторонних скриптов.</p>
        </div>
      </div>
      <div class="privacy-item">
        <div class="ico">
          <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        </div>
        <div>
          <h3>Одна сессионная cookie</h3>
          <p>HttpOnly и SameSite — только для того, чтобы вы остались в аккаунте.</p>
        </div>
      </div>
      <div class="privacy-item">
        <div class="ico">
          <svg class="icon" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
        </div>
        <div>
          <h3>Минимум данных</h3>
          <p>Только ник и пароль. Ни e-mail, ни телефона, ни IP-логов.</p>
        </div>
      </div>
    </div>

    <a class="link-arrow" href="/privacy">
      Почитать подробнее
      <svg class="icon" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
    </a>
  </div>
</section>

<section id="faq" class="section">
  <div class="container">
    <h2>Ответы на вопросы</h2>
    <div class="faq">
      <details>
        <summary>Сколько стоит SldChat?</summary>
        <div class="answer-wrap"><div class="answer">Нисколько. Проект полностью бесплатный, без рекламы и подписок.</div></div>
      </details>
      <details>
        <summary>Сохраняются ли мои сообщения?</summary>
        <div class="answer-wrap"><div class="answer">Нет. Всё живёт в оперативной памяти сервера и исчезает при его перезапуске.
        На диск ничего не пишется.</div></div>
      </details>
      <details>
        <summary>Почему я вижу только своих контактов?</summary>
        <div class="answer-wrap"><div class="answer">В SldChat нет публичного каталога пользователей. Чтобы начать общение,
        нажмите «Добавить контакт» и введите ник собеседника. После этого вы появитесь
        в его списке контактов, а он — в вашем.</div></div>
      </details>
      <details>
        <summary>Можно ли удалять сообщения?</summary>
        <div class="answer-wrap"><div class="answer">Нет. Отправленное сообщение остаётся в истории до перезапуска сервера.
        Пишите осознанно.</div></div>
      </details>
      <details>
        <summary>Чем серверы отличаются друг от друга?</summary>
        <div class="answer-wrap"><div class="answer">Это два независимых развёртывания. У каждого своя база
        пользователей и сообщений, между собой они не связаны. Регистрируйтесь там, где удобно.</div></div>
      </details>
      <details>
        <summary>Как выйти из аккаунта?</summary>
        <div class="answer-wrap"><div class="answer">Кнопка выхода — в шапке приложения, справа от списка контактов.</div></div>
      </details>
    </div>
  </div>
</section>

__FOOTER__

<script>
document.querySelectorAll('.faq details').forEach(d => {
  d.addEventListener('toggle', () => {
    if (d.open) {
      document.querySelectorAll('.faq details').forEach(o => {
        if (o !== d && o.open) o.open = false;
      });
    }
  });
});
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
      <a class="navlink" href="/">На главную</a>
      <a class="navlink" href="/support">Поддержка</a>
      <a class="btn btn-primary" href="/register">Регистрация</a>
    </nav>
  </div>
</header>

<div class="page-wrap">
  <div class="page-head">
    <h1>Политика приватности</h1>
    <p>Что мы собираем, что нет, и почему SldChat по-настоящему прост.</p>
  </div>

  <div class="page-card selectable">
    <h2>Коротко</h2>
    <p>SldChat собирает <b>минимум данных</b>. Мы не хотим знать о вас больше, чем нужно для работы
    мессенджера. Всё хранится в оперативной памяти сервера и стирается при его перезапуске.</p>
  </div>

  <div class="page-card selectable">
    <h2>Что мы храним</h2>
    <ul>
      <li><b>Ник</b> — то, как вас видят другие пользователи.</li>
      <li><b>Пароль</b> — в том виде, в котором вы его ввели, но только в оперативной памяти.</li>
      <li><b>Сообщения</b> — тексты, которые вы отправляете, и время их отправки.</li>
      <li><b>Список контактов</b> — с кем вы общаетесь.</li>
      <li><b>Время последней активности</b> — для отображения статуса «в сети / был(а) недавно».</li>
    </ul>
    <p>Всё это живёт исключительно в ОЗУ процесса. На диск ничего не записывается.</p>
  </div>

  <div class="page-card selectable">
    <h2>Чего мы не делаем</h2>
    <ul>
      <li>Не собираем e-mail, телефон и другие персональные данные.</li>
      <li>Не ведём логи IP-адресов и не отслеживаем вас между сессиями.</li>
      <li>Не используем аналитику, трекеры, пиксели и сторонние скрипты.</li>
      <li>Не показываем рекламу и не передаём данные третьим лицам.</li>
    </ul>
  </div>

  <div class="page-card selectable">
    <h2>Cookie</h2>
    <p>Мы используем <b>одну-единственную cookie</b> — <code>session</code>. Она нужна только для того,
    чтобы вы оставались в аккаунте между запросами.</p>
    <ul>
      <li>HttpOnly — недоступна из JavaScript.</li>
      <li>SameSite=Lax — снижает риск CSRF-атак.</li>
      <li>Secure — при работе сайта по HTTPS.</li>
      <li>Срок жизни — до 30 дней или до выхода из аккаунта.</li>
    </ul>
  </div>

  <div class="page-card selectable">
    <h2>Сколько данные живут</h2>
    <p>Ровно столько, сколько работает сервер. Как только сервер перезапускается, вся информация
    исчезает безвозвратно. У нас физически нет ни ваших старых сообщений, ни старых паролей,
    ни старых сессий.</p>
  </div>

  <div class="page-card selectable">
    <h2>Как удалить свои данные</h2>
    <ul>
      <li>Выйти из аккаунта — удалит активную сессию на этом устройстве.</li>
      <li>Дождаться перезапуска сервера — удалит всё остальное.</li>
      <li>При желании — написать в <a href="/support">поддержку</a>, и мы ускорим процесс.</li>
    </ul>
  </div>

  <div class="page-card selectable">
    <h2>Два независимых сервера</h2>
    <p>У SldChat есть <b>два отдельных развёртывания</b> — <code>sldchat.fastapicloud.dev</code>
    и <code>sldchat.onrunxbuild.com</code>. Это разные серверы с разными базами пользователей.
    <b>Данные между ними не передаются.</b> Регистрируясь на одном, вы не появляетесь на другом.</p>
  </div>

  <div class="page-card selectable">
    <h2>Безопасность</h2>
    <ul>
      <li>Пароли не возвращаются через API.</li>
      <li>Все проверки доступа выполняются на стороне сервера.</li>
      <li>Заголовки безопасности: X-Frame-Options, X-Content-Type-Options, Referrer-Policy.</li>
      <li>Страницы с приватными данными не кэшируются браузером.</li>
    </ul>
    <p>Полноценной end-to-end криптографии у нас нет — это осознанный выбор простого проекта.
    Не отправляйте через SldChat ничего, что боитесь потерять.</p>
  </div>

  <a class="link-arrow" href="/" style="margin-top:8px">
    <svg class="icon" viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
    На главную
  </a>
</div>

__FOOTER__

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
  .contact-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 20px rgba(0,0,0,0.09);
    border-color: #a8b8ca;
    text-decoration: none;
  }
  .contact-card .ico {
    width: 48px; height: 48px; flex-shrink: 0;
    border-radius: 10px;
    background: linear-gradient(#e4ebf2, #c8d3de);
    border: 1px solid #b7c2cd;
    display: flex; align-items: center; justify-content: center;
    color: #3f6fa8;
    box-shadow: inset 0 1px 0 #fff;
  }
  .contact-card .body { min-width: 0; flex: 1; }
  .contact-card .label { font-size: 11px; color: #7a8695; text-transform: uppercase; letter-spacing: 0.5px; }
  .contact-card .value {
    font-size: 16px; color: #2b3a4a; font-weight: bold;
    word-break: break-all; margin-top: 2px;
  }
  .contact-card .value.mono {
    font-family: Consolas, "Courier New", monospace; font-size: 15px;
  }
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
      <a class="navlink" href="/">На главную</a>
      <a class="navlink" href="/privacy">Приватность</a>
      <a class="btn btn-primary" href="/register">Регистрация</a>
    </nav>
  </div>
</header>

<div class="page-wrap">
  <div class="page-head">
    <h1>Поддержка</h1>
    <p>Возникла проблема или есть предложение? Напишите нам — мы обязательно ответим.</p>
  </div>

  <div class="page-card">
    <h2>Связаться</h2>

    <a class="contact-card" href="mailto:sldshr.confirmation@gmail.com">
      <div class="ico">
        <svg class="icon xl" viewBox="0 0 24 24"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
      </div>
      <div class="body">
        <div class="label">E-mail</div>
        <div class="value mono">sldshr.confirmation@gmail.com</div>
      </div>
      <svg class="icon arrow" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
    </a>

    <a class="contact-card" href="https://discord.com/users/sldshr" target="_blank" rel="noopener noreferrer">
      <div class="ico">
        <svg class="icon xl" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      </div>
      <div class="body">
        <div class="label">Discord</div>
        <div class="value">sldshr</div>
      </div>
      <svg class="icon arrow" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
    </a>
  </div>

  <div class="page-card selectable">
    <h2>Перед обращением</h2>
    <ul>
      <li>Убедитесь, что вы на нужном сервере: <code>sldchat.fastapicloud.dev</code>
          или <code>sldchat.onrunxbuild.com</code> — данные между ними не передаются.</li>
      <li>Если не получается войти — проверьте, что ник введён точно так же, как при регистрации.</li>
      <li>Сообщения не сохраняются после перезапуска сервера — это нормальное поведение.</li>
      <li>Опишите проблему подробно: что делали, что ожидали, что получилось.</li>
    </ul>
  </div>
</div>

__FOOTER__

</body>
</html>
"""


# ================== AUTH ==================
AUTH_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
__SHARED_CSS__

  body {
    min-height: 100vh; display: flex; flex-direction: column;
    background:
      radial-gradient(circle at 30% 10%, #e6eef6 0%, transparent 55%),
      linear-gradient(#cfd9e3, #a8b6c4);
  }
  .topline {
    padding: 16px 22px;
    display: flex; align-items: center;
  }
  .topline a.logo { font-size: 20px; }
  .wrap {
    flex: 1; display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .box {
    width: 100%; max-width: 380px;
    background: #f4f6f8;
    border: 1px solid #8b97a3; border-radius: 6px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.22), inset 0 1px 0 #fff;
    padding: 22px 26px 26px;
    animation: popIn 0.4s ease both;
  }
  .box h1 {
    text-align: center; font-size: 20px; font-weight: normal;
    margin: 0 0 18px; color: #23374b; text-shadow: 0 1px 0 #fff;
    letter-spacing: 0.5px;
  }
  .tabs {
    display: flex; margin-bottom: 16px;
    border-bottom: 1px solid #a8b2bc;
  }
  .tabs button {
    flex: 1;
    border: 1px solid #a8b2bc; border-bottom: none;
    background: linear-gradient(#eef1f4, #d3dae1);
    border-radius: 4px 4px 0 0;
    padding: 8px 0; margin-right: 4px; cursor: pointer;
    font-family: inherit; font-size: 13px; color: #445;
    transition: background 0.15s ease, color 0.15s ease;
  }
  .tabs button:last-child { margin-right: 0; }
  .tabs button:hover { background: linear-gradient(#f4f6f8, #dae0e6); }
  .tabs button.active {
    background: #f4f6f8; color: #223; font-weight: bold;
    position: relative; top: 1px;
  }
  label { display: block; margin: 10px 0 4px; color: #445; font-size: 13px; }
  input[type=text], input[type=password] {
    width: 100%; padding: 8px 10px;
    font-family: inherit; font-size: 13px;
    border: 1px solid #9aa4ae; border-radius: 3px;
    background: #fff; outline: none;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  input:focus {
    border-color: #5a7a9a;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.08), 0 0 0 3px rgba(90,122,154,0.15);
  }

  .btn2 {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    width: 100%; margin-top: 18px;
    padding: 10px 0; border-radius: 4px;
    font-family: inherit; font-size: 13px; cursor: pointer;
    background: linear-gradient(#fbfcfd, #ccd5de);
    border: 1px solid #7a8794; color: #2b3a4a;
    text-shadow: 0 1px 0 #fff;
    transition: transform 0.1s ease, box-shadow 0.15s ease, background 0.15s ease;
  }
  .btn2-primary {
    background: linear-gradient(#5b8fc4, #3f6fa8);
    border-color: #35597f; color: #fff;
    text-shadow: 0 1px 0 rgba(0,0,0,0.2);
  }
  .btn2-primary:hover {
    background: linear-gradient(#699bcd, #4577b1);
    box-shadow: 0 4px 12px rgba(63,111,168,0.35);
  }
  .btn2:active { transform: scale(0.98); }

  .error {
    min-height: 18px; color: #c22; text-align: center;
    font-size: 12px; margin-bottom: 4px;
  }
  .hint { margin-top: 14px; text-align: center; color: #889; font-size: 11px; }
  .back { text-align: center; margin-top: 14px; font-size: 12px; }

  .form { display: block; }
  .form.hidden { display: none; }
</style>
</head>
<body>

<div class="topline">
  <a href="/" class="logo">
    <svg class="icon" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
    Sld<span>Chat</span>
  </a>
</div>

<div class="wrap">
  <div class="box">
    <h1>Добро пожаловать</h1>
    <div class="tabs">
      <button type="button" id="tabLogin" class="__LOGIN_ACTIVE__">Вход</button>
      <button type="button" id="tabRegister" class="__REG_ACTIVE__">Регистрация</button>
    </div>
    <div class="error" id="error"></div>

    <form id="formLogin" class="form __LOGIN_HIDDEN__">
      <label>Ник:</label>
      <input type="text" name="nick" autocomplete="username" maxlength="20">
      <label>Пароль:</label>
      <input type="password" name="password" autocomplete="current-password">
      <button type="submit" class="btn2 btn2-primary">Войти</button>
    </form>

    <form id="formRegister" class="form __REG_HIDDEN__">
      <label>Ник:</label>
      <input type="text" name="nick" autocomplete="username" maxlength="20" placeholder="3–20 символов">
      <label>Пароль:</label>
      <input type="password" name="password" autocomplete="new-password" placeholder="минимум 3 символа">
      <button type="submit" class="btn2 btn2-primary">Зарегистрироваться</button>
    </form>

    <div class="hint">Регистрация занимает меньше минуты</div>
    <div class="back"><a href="/">← На главную</a></div>
  </div>
</div>

<script>
  var tabLogin = document.getElementById('tabLogin');
  var tabRegister = document.getElementById('tabRegister');
  var formLogin = document.getElementById('formLogin');
  var formRegister = document.getElementById('formRegister');
  var errorBox = document.getElementById('error');

  function showTab(which) {
    if (which === 'login') {
      tabLogin.classList.add('active');
      tabRegister.classList.remove('active');
      formLogin.classList.remove('hidden');
      formRegister.classList.add('hidden');
    } else {
      tabRegister.classList.add('active');
      tabLogin.classList.remove('active');
      formRegister.classList.remove('hidden');
      formLogin.classList.add('hidden');
    }
    errorBox.textContent = '';
  }

  tabLogin.onclick = function() { showTab('login'); history.replaceState(null, '', '/login'); };
  tabRegister.onclick = function() { showTab('register'); history.replaceState(null, '', '/register'); };

  function submitForm(url, form) {
    errorBox.textContent = '';
    var fd = new FormData(form);
    fetch(url, { method: 'POST', body: fd })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.ok) { location.href = '/chat'; return; }
        errorBox.textContent = d.error || 'Ошибка';
      })
      .catch(function() {
        errorBox.textContent = 'Ошибка соединения';
      });
  }

  formLogin.onsubmit = function(e) { e.preventDefault(); submitForm('/api/login', formLogin); };
  formRegister.onsubmit = function(e) { e.preventDefault(); submitForm('/api/register', formRegister); };
</script>
</body>
</html>
"""


def render_auth(active: str) -> str:
    return (AUTH_TEMPLATE
        .replace("__SHARED_CSS__", SHARED_CSS)
        .replace("__TITLE__", "SldChat — Вход" if active == "login" else "SldChat — Регистрация")
        .replace("__LOGIN_ACTIVE__", "active" if active == "login" else "")
        .replace("__REG_ACTIVE__", "active" if active == "register" else "")
        .replace("__LOGIN_HIDDEN__", "" if active == "login" else "hidden")
        .replace("__REG_HIDDEN__", "" if active == "register" else "hidden"))


# ================== CHAT ==================
CHAT_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>SldChat</title>
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

  @keyframes msgIn {
    from { opacity: 0; transform: translateY(6px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  @keyframes toastIn {
    from { opacity: 0; transform: translateX(24px); }
    to   { opacity: 1; transform: translateX(0); }
  }
  @keyframes popIn { 0% { transform: scale(0.5); opacity: 0; } 100% { transform: scale(1); opacity: 1; } }
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

  /* ===== Sidebar ===== */
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
    transition: transform 0.12s ease, background 0.15s ease, box-shadow 0.15s ease;
    font-family: inherit;
  }
  .icon-btn:hover { background: linear-gradient(#fff, #dbe3ea); transform: translateY(-1px); }
  .icon-btn:active { transform: translateY(0) scale(0.94); box-shadow: inset 0 1px 2px rgba(0,0,0,0.15); }

  .add-wrap { padding: 8px 9px; border-bottom: 1px solid #dbe1e7; }
  .add-btn {
    width: 100%; padding: 8px 10px;
    display: flex; align-items: center; justify-content: center; gap: 7px;
    border: 1px solid #7a8794; border-radius: 4px;
    background: linear-gradient(#fbfcfd, #cfd8e0);
    font-family: inherit; font-size: 12px; color: #2b3a4a;
    cursor: pointer; text-shadow: 0 1px 0 #fff;
    transition: transform 0.1s ease, background 0.15s ease, box-shadow 0.15s ease;
  }
  .add-btn:hover { background: linear-gradient(#fff, #dbe3ea); transform: translateY(-1px); box-shadow: 0 2px 5px rgba(0,0,0,0.08); }
  .add-btn:active { transform: translateY(0) scale(0.98); }

  .add-form {
    display: none; gap: 6px; margin-top: 6px;
  }
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
    padding: 8px 11px;
    border-bottom: 1px solid #dde3e9;
    background: #f6f8fa; cursor: pointer;
    min-width: 0;
    transition: background 0.12s ease;
  }
  .user-item:hover  { background: #eaf0f6; }
  .user-item.active { background: #d3e0ec; }
  .user-item .row1 {
    display: flex; justify-content: space-between; align-items: center; gap: 6px;
  }
  .user-item .nick {
    font-weight: bold; color: #2b3a4a;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    display: flex; align-items: center; gap: 6px; min-width: 0;
  }
  .user-item .nick .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: #b5bec8; flex-shrink: 0;
  }
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

  /* ===== Chat ===== */
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
  .chat-header .sub {
    font-size: 11px; color: #7a8695; font-weight: normal; margin-top: 1px;
  }
  .chat-header .sub.online { color: #2f8f3d; font-weight: bold; }
  .mobile-only { display: none; }

  .messages {
    flex: 1; overflow-y: auto; padding: 14px 18px;
    background:
      radial-gradient(circle at 80% 10%, #f6f9fc 0%, transparent 60%),
      #fbfcfd;
    -webkit-overflow-scrolling: touch;
  }
  .empty {
    color: #a2aab3; text-align: center; margin-top: 60px; font-size: 13px;
    display: flex; flex-direction: column; align-items: center; gap: 14px;
  }

  .date-sep { text-align: center; margin: 14px 0 10px; }
  .date-sep span {
    background: #eef2f6; color: #6f7c8b;
    padding: 3px 11px; border-radius: 10px; font-size: 11px;
    border: 1px solid #dde4eb;
  }

  .msg-row {
    display: flex; margin-bottom: 4px; align-items: flex-end; gap: 6px;
    animation: msgIn 0.18s ease-out both;
  }
  .msg-row.mine { justify-content: flex-end; }
  .msg-bubble {
    max-width: min(72%, 540px);
    padding: 7px 12px;
    border-radius: 14px 14px 14px 4px;
    background: #eef2f6; border: 1px solid #dde4eb;
    font-size: 13px; line-height: 1.4;
    word-wrap: break-word; white-space: pre-wrap;
    color: #22303e;
  }
  .msg-row.mine .msg-bubble {
    background: #d3e6c8; border-color: #bed7ad;
    border-radius: 14px 14px 4px 14px;
  }
  .msg-meta { font-size: 10px; color: #8a94a0; margin-top: 3px; }
  .msg-row.mine .msg-meta { text-align: right; }

  .input-area {
    border-top: 1px solid #c8d1da; padding: 10px 12px;
    background: #eef2f6;
    display: flex; gap: 8px; align-items: flex-end;
  }
  .input-area textarea {
    flex: 1; resize: none;
    padding: 9px 12px; font-family: inherit; font-size: 13px;
    border: 1px solid #b5bec8; border-radius: 16px;
    background: #fff; outline: none;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.06);
    max-height: 120px; min-height: 34px; line-height: 1.4;
  }
  .input-area textarea:focus { border-color: #5a7a9a; }
  .input-area button {
    padding: 8px 16px; border-radius: 16px;
    border: 1px solid #35597f; cursor: pointer;
    background: linear-gradient(#5b8fc4, #3f6fa8);
    color: #fff; font-family: inherit; font-size: 13px;
    text-shadow: 0 1px 0 rgba(0,0,0,0.2);
    display: flex; align-items: center; gap: 6px;
    transition: transform 0.1s ease, background 0.15s ease;
  }
  .input-area button:hover { background: linear-gradient(#699bcd, #4577b1); }
  .input-area button:active { transform: scale(0.96); }

  /* ===== Info panel ===== */
  .info-panel {
    background: #f0f3f7; border-left: 1px solid #c8d1da;
    padding: 16px 16px; overflow-y: auto; min-width: 0;
  }
  .info-panel .close-btn { display: none; }
  .info-empty {
    color: #98a2ad; text-align: center; margin-top: 40px; font-size: 12px;
    display: flex; flex-direction: column; align-items: center; gap: 12px;
  }
  .info-head {
    text-align: center; padding-bottom: 16px;
    border-bottom: 1px solid #dbe1e7; margin-bottom: 16px;
  }
  .info-head .avatar-none {
    width: 54px; height: 54px; margin: 0 auto 10px;
    border-radius: 12px;
    background: linear-gradient(#e4ebf2, #c8d3de);
    border: 1px solid #b7c2cd;
    display: flex; align-items: center; justify-content: center;
    color: #3f6fa8;
    box-shadow: inset 0 1px 0 #fff;
  }
  .info-head .name {
    font-size: 16px; font-weight: bold; color: #23374b;
    word-break: break-all;
  }
  .info-head .status { font-size: 12px; margin-top: 4px; color: #7a8695; }
  .info-head .status.online { color: #2f8f3d; font-weight: bold; }
  .info-row {
    display: flex; justify-content: space-between;
    padding: 8px 0; border-bottom: 1px solid #e0e6ec;
    font-size: 12px; gap: 8px;
  }
  .info-row .k { color: #7a8695; flex-shrink: 0; }
  .info-row .v { color: #2b3a4a; text-align: right; word-break: break-word; }

  .toast-wrap {
    position: fixed; bottom: 16px; right: 16px; z-index: 100;
    display: flex; flex-direction: column; gap: 8px;
    pointer-events: none;
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

  /* =============================================
     МОБИЛЬНАЯ ВЕРСИЯ
     ============================================= */
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
    .info-panel .close-btn {
      display: inline-flex; float: right; margin: -4px -4px 0 0;
      padding: 8px;
    }
    .info-panel .close-btn .icon { width: 18px; height: 18px; }
    .mobile-only { display: inline-flex; }

    /* === Sidebar === */
    .sb-header {
      padding: 12px 14px;
      padding-top: calc(12px + env(safe-area-inset-top));
    }
    .logo-mini { font-size: 18px; gap: 8px; }
    .logo-mini .icon { width: 22px; height: 22px; }
    .sb-header .icon-btn { padding: 9px; }
    .sb-header .icon-btn .icon { width: 20px; height: 20px; }

    .me-line { padding: 8px 14px; font-size: 13px; }

    .add-wrap { padding: 12px 14px; }
    .add-btn {
      padding: 12px 14px; font-size: 15px;
      border-radius: 6px;
      min-height: 46px;
    }
    .add-btn .icon { width: 18px; height: 18px; }
    .add-form { margin-top: 8px; gap: 8px; }
    .add-form input {
      padding: 11px 14px; font-size: 15px; border-radius: 6px;
      min-height: 46px;
    }
    .add-form button { padding: 0 16px; min-height: 46px; border-radius: 6px; }
    .add-form button .icon { width: 18px; height: 18px; }

    .search { padding: 10px 14px; }
    .search input {
      padding: 11px 14px 11px 36px; font-size: 15px; border-radius: 20px;
      min-height: 42px;
    }
    .search-wrap .icon { left: 12px; width: 16px; height: 16px; }

    .user-item { padding: 14px 16px; border-bottom: 1px solid #d3dae0; }
    .user-item .nick { font-size: 15px; gap: 8px; }
    .user-item .nick .dot { width: 9px; height: 9px; }
    .user-item .time { font-size: 11px; }
    .user-item .preview { font-size: 13px; margin-top: 4px; }
    .user-item .badge { font-size: 11px; padding: 2px 7px; min-width: 20px; }

    .empty-list { padding: 50px 20px; font-size: 14px; }
    .empty-list .icon.huge { width: 52px; height: 52px; }

    /* === Chat === */
    .chat-header {
      padding: 10px 12px;
      padding-top: calc(10px + env(safe-area-inset-top));
      min-height: 60px;
      gap: 8px;
    }
    .chat-header .title { font-size: 16px; }
    .chat-header .sub { font-size: 12px; margin-top: 2px; }
    .chat-header .icon-btn { padding: 9px; }
    .chat-header .icon-btn .icon { width: 22px; height: 22px; }

    .messages {
      padding: 16px 12px;
      padding-bottom: 8px;
    }
    .msg-bubble {
      max-width: 85%;
      font-size: 14px;
      padding: 9px 13px;
      border-radius: 16px 16px 16px 4px;
    }
    .msg-row.mine .msg-bubble { border-radius: 16px 16px 4px 16px; }
    .msg-meta { font-size: 11px; margin-top: 4px; }
    .date-sep { margin: 16px 0 12px; }
    .date-sep span { font-size: 12px; padding: 4px 12px; }

    .empty { margin-top: 40px; font-size: 14px; }
    .empty .icon.huge { width: 52px; height: 52px; }

    .input-area {
      padding: 8px 10px;
      padding-bottom: calc(8px + env(safe-area-inset-bottom));
      gap: 6px;
      align-items: flex-end;
    }
    .input-area textarea {
      padding: 10px 14px;
      font-size: 15px;
      min-height: 42px;
      border-radius: 21px;
      max-height: 100px;
    }
    .input-area button {
      width: 42px; height: 42px;
      padding: 0;
      border-radius: 50%;
      justify-content: center;
      flex-shrink: 0;
    }
    .input-area button .btn-label { display: none; }
    .input-area button .icon { width: 18px; height: 18px; }

    /* === Info panel === */
    .info-head .avatar-none { width: 64px; height: 64px; }
    .info-head .name { font-size: 18px; }
    .info-head .status { font-size: 13px; margin-top: 6px; }
    .info-row { font-size: 13px; padding: 10px 0; }

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
      <button class="icon-btn" id="logoutBtn" title="Выйти">
        <svg class="icon" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
      </button>
    </div>

    <div class="me-line">Вы вошли как <span class="nick" id="myNick">…</span></div>

    <div class="add-wrap">
      <button class="add-btn" id="addBtn">
        <svg class="icon" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        Добавить контакт
      </button>
      <form class="add-form" id="addForm">
        <input type="text" id="addInput" placeholder="Ник собеседника" autocomplete="off" maxlength="20">
        <button type="submit" title="Добавить">
          <svg class="icon" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
        </button>
      </form>
      <div class="add-msg" id="addMsg"></div>
    </div>

    <div class="search">
      <div class="search-wrap">
        <svg class="icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input type="text" id="searchInput" placeholder="Поиск по контактам" autocomplete="off">
      </div>
    </div>

    <div class="user-list" id="userList"></div>
  </aside>

  <main class="chat" id="chatMain">
    <header class="chat-header">
      <button class="icon-btn mobile-only" id="backBtn" title="Назад">
        <svg class="icon" viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
      </button>
      <div class="title">
        <div id="chatTitle">Выберите собеседника</div>
        <div class="sub" id="chatSub"></div>
      </div>
      <button class="icon-btn mobile-only" id="infoBtn" title="Информация">
        <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
      </button>
    </header>

    <div class="messages" id="messages">
      <div class="empty">
        <svg class="icon huge" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
        <div>Слева выберите контакт, чтобы начать переписку</div>
      </div>
    </div>

    <div class="input-area" id="inputArea" style="display:none">
      <textarea id="msgInput" placeholder="Введите сообщение..." rows="1"></textarea>
      <button id="sendBtn" title="Отправить">
        <svg class="icon" viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        <span class="btn-label">Отправить</span>
      </button>
    </div>
  </main>

  <aside class="info-panel" id="infoPanel">
    <button class="icon-btn close-btn" id="infoCloseBtn" title="Закрыть">
      <svg class="icon" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
    <div id="infoContent">
      <div class="info-empty">
        <svg class="icon huge" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
        <div>Информация о собеседнике появится здесь</div>
      </div>
    </div>
  </aside>

</div>

<div class="toast-wrap" id="toastWrap"></div>

<script>
let me = null;
let current = null;
const lastIds = {};
const renderedIds = new Set();
let usersCache = [];
let searchQuery = '';
let currentInfo = null;
let ws = null;
let wsReconnectTimer = null;

/* signature-кэш чтобы не перерисовывать DOM без причины */
let lastContactsSig = '';
let lastInfoSig = '';

const $ = id => document.getElementById(id);
const app = $('app');

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
function pad2(n) { return String(n).padStart(2, '0'); }
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}
function fmtDateTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear();
}
function fmtLastSeen(ts) {
  if (!ts) return 'давно';
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return 'только что';
  if (diff < 3600) return Math.floor(diff / 60) + ' мин назад';
  if (diff < 86400) return Math.floor(diff / 3600) + ' ч назад';
  return fmtDateTime(ts);
}
function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const yest = new Date(); yest.setDate(yest.getDate() - 1);
  const same = (a, b) => a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (same(d, today)) return 'Сегодня';
  if (same(d, yest)) return 'Вчера';
  return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear();
}
function scrollIfNearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 160;
}
function toast(msg) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  $('toastWrap').appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 250);
  }, 3000);
}

/* ---------- подписи состояния ---------- */
function contactsSignature(data) {
  let s = '';
  for (const u of data) {
    s += u.nick + '|' + (u.online ? 1 : 0) + '|' + (u.unread || 0) + '|'
       + (u.last ? u.last.id : 0) + '\n';
  }
  return s;
}
function infoSignature(i) {
  return i.nick + '|' + (i.online ? 1 : 0) + '|' + Math.floor(i.last_seen || 0)
       + '|' + (i.msg_count || 0);
}

/* ---------- WebSocket ---------- */
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try { ws = new WebSocket(proto + '//' + location.host + '/ws'); }
  catch (e) { scheduleReconnect(); return; }

  ws.onopen = () => {
    if (ws._pingTimer) clearInterval(ws._pingTimer);
    ws._pingTimer = setInterval(() => {
      if (ws && ws.readyState === 1) { try { ws.send('ping'); } catch (e) {} }
    }, 25000);
  };
  ws.onmessage = e => {
    let d; try { d = JSON.parse(e.data); } catch (_) { return; }
    handleWS(d);
  };
  ws.onclose = () => {
    if (ws && ws._pingTimer) clearInterval(ws._pingTimer);
    scheduleReconnect();
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

function scheduleReconnect() {
  if (wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(() => { wsReconnectTimer = null; connectWS(); }, 1200);
}

function handleWS(d) {
  if (d.type === 'message') {
    const m = d.message;
    const peer = m.from === me ? m.to : m.from;
    if (current === peer) {
      if (!renderedIds.has(m.id)) {
        renderedIds.add(m.id);
        appendMessage(m);
        lastIds[current] = Math.max(lastIds[current] || 0, m.id);
      }
      if (m.from !== me) markRead(peer);
    }
    loadContacts();
  } else if (d.type === 'presence') {
    const u = usersCache.find(x => x.nick === d.nick);
    if (u) {
      u.online = d.online;
      lastContactsSig = '';   // форсим обновление
      renderContacts();
    }
    if (current === d.nick) {
      currentInfo = Object.assign({}, currentInfo || {}, { online: d.online });
      updateChatSubtitle();
    }
  } else if (d.type === 'contact_added') {
    toast('Новый контакт: ' + d.nick);
    loadContacts();
  }
}

/* ---------- init ---------- */
async function init() {
  const r = await fetch('/api/me');
  if (!r.ok) { location.href = '/'; return; }
  const d = await r.json();
  me = d.nick;
  $('myNick').textContent = me;
  connectWS();
  await loadContacts();
}

/* ---------- контакты ---------- */
async function loadContacts() {
  const r = await fetch('/api/contacts');
  if (!r.ok) { location.href = '/'; return; }
  const d = await r.json();
  usersCache = d.contacts || [];
  const sig = contactsSignature(usersCache);
  if (sig === lastContactsSig) return;   // ничего не поменялось — не трогаем DOM
  lastContactsSig = sig;
  renderContacts();
}

function renderContacts() {
  const list = $('userList');
  const scrollTop = list.scrollTop;   // сохраняем позицию

  list.innerHTML = '';
  const q = searchQuery.trim().toLowerCase();
  const filtered = q ? usersCache.filter(u => u.nick.toLowerCase().includes(q)) : usersCache;

  if (!filtered.length) {
    const e = document.createElement('div');
    e.className = 'empty-list';
    e.innerHTML =
      '<svg class="icon huge" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>' +
      '<div>' + (q ? 'Ничего не найдено' : 'Пока нет контактов.<br>Нажмите «Добавить контакт».') + '</div>';
    list.appendChild(e);
    list.scrollTop = scrollTop;
    return;
  }
  for (const u of filtered) {
    const div = document.createElement('div');
    div.className = 'user-item' + (u.nick === current ? ' active' : '');
    let preview = 'Нет сообщений';
    if (u.last) {
      const prefix = u.last.from === me ? 'Вы: ' : '';
      preview = prefix + u.last.text;
    }
    const timeStr = u.last ? fmtTime(u.last.time) : '';
    const unread = u.unread
      ? '<span class="badge">' + (u.unread > 99 ? '99+' : u.unread) + '</span>'
      : '';
    div.innerHTML =
      '<div class="row1">' +
        '<div class="nick"><span class="dot' + (u.online ? ' on' : '') + '"></span>' + esc(u.nick) + '</div>' +
        '<div class="time">' + timeStr + unread + '</div>' +
      '</div>' +
      '<div class="preview">' + esc(preview) + '</div>';
    div.onclick = () => openDialog(u.nick);
    list.appendChild(div);
  }
  list.scrollTop = scrollTop;
}

/* ---------- диалог ---------- */
async function openDialog(nick) {
  current = nick;
  lastIds[nick] = 0;
  renderedIds.clear();
  lastInfoSig = '';   // форсим обновление инфы при смене собеседника

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

async function refreshDialog() {
  if (!current) return;
  const nick = current;
  const since = lastIds[nick] || 0;
  const r = await fetch('/api/dialog/' + encodeURIComponent(nick) + '?since=' + since);
  if (!r.ok) return;
  const d = await r.json();
  if (!d.ok || current !== nick) return;
  if (!d.messages || !d.messages.length) return;   // ничего нового — не трогаем DOM

  const box = $('messages');
  const wasNearBottom = scrollIfNearBottom(box);
  let added = false;
  for (const m of d.messages) {
    if (renderedIds.has(m.id)) continue;
    renderedIds.add(m.id);
    lastIds[nick] = Math.max(lastIds[nick] || 0, m.id);
    appendMessage(m);
    added = true;
  }
  if (added && (wasNearBottom || !box.querySelector('.msg-row'))) {
    box.scrollTop = box.scrollHeight;
  }
}

function appendMessage(m) {
  const box = $('messages');
  const lastRow = box.querySelector('.msg-row:last-of-type');
  const lastTs = lastRow ? parseFloat(lastRow.dataset.ts || '0') : 0;
  const newDay = dayLabel(m.time);
  if (!lastTs || dayLabel(lastTs) !== newDay) {
    const sep = document.createElement('div');
    sep.className = 'date-sep';
    sep.innerHTML = '<span>' + esc(newDay) + '</span>';
    box.appendChild(sep);
  }
  const row = document.createElement('div');
  row.className = 'msg-row' + (m.from === me ? ' mine' : '');
  row.dataset.id = m.id;
  row.dataset.ts = m.time;
  row.innerHTML =
    '<div class="msg-bubble">' + esc(m.text) +
      '<div class="msg-meta">' + fmtTime(m.time) + '</div>' +
    '</div>';
  box.appendChild(row);
  const nearBottom = scrollIfNearBottom(box) || m.from === me;
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

async function markRead(nick) {
  try { await fetch('/api/dialog/' + encodeURIComponent(nick) + '?since=0'); } catch (e) {}
  loadContacts();
}

/* ---------- отправка ---------- */
async function send() {
  if (!current) return;
  const inp = $('msgInput');
  const text = inp.value.trim();
  if (!text) return;
  const fd = new FormData();
  fd.append('to', current);
  fd.append('text', text);
  inp.value = '';
  inp.style.height = 'auto';
  const r = await fetch('/api/send', { method: 'POST', body: fd });
  const d = await r.json().catch(() => ({ ok:false }));
  if (!d.ok) { toast(d.error || 'Не удалось отправить'); return; }
  if (!renderedIds.has(d.message.id)) {
    renderedIds.add(d.message.id);
    lastIds[current] = Math.max(lastIds[current] || 0, d.message.id);
    appendMessage(d.message);
  }
  loadContacts();
}

/* ---------- инфо ---------- */
async function loadInfo(nick) {
  if (!nick) {
    $('infoContent').innerHTML =
      '<div class="info-empty">' +
        '<svg class="icon huge" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>' +
        '<div>Информация о собеседнике появится здесь</div>' +
      '</div>';
    lastInfoSig = '';
    return;
  }
  const r = await fetch('/api/user/' + encodeURIComponent(nick) + '/info');
  if (!r.ok) return;
  const d = await r.json();
  if (!d.ok) return;
  const i = d.info;
  const sig = infoSignature(i);
  if (sig === lastInfoSig) return;   // не перерисовываем, если не поменялось
  lastInfoSig = sig;
  currentInfo = { online: i.online, lastSeen: i.last_seen };

  const status = i.online
    ? '<div class="status online">В сети</div>'
    : '<div class="status">Был(а): ' + esc(fmtLastSeen(i.last_seen)) + '</div>';
  $('infoContent').innerHTML =
    '<div class="info-head">' +
      '<div class="avatar-none">' +
        '<svg class="icon" style="width:30px;height:30px" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>' +
      '</div>' +
      '<div class="name">' + esc(i.nick) + '</div>' +
      status +
    '</div>' +
    '<div class="info-row"><span class="k">Ник</span><span class="v">' + esc(i.nick) + '</span></div>' +
    '<div class="info-row"><span class="k">В SldChat с</span><span class="v">' + esc(fmtDateTime(i.created)) + '</span></div>' +
    '<div class="info-row"><span class="k">Последняя активность</span><span class="v">' + esc(fmtLastSeen(i.last_seen)) + '</span></div>' +
    '<div class="info-row"><span class="k">Сообщений в диалоге</span><span class="v">' + i.msg_count + '</span></div>';
  updateChatSubtitle();
}

function updateChatSubtitle() {
  const el = $('chatSub');
  if (!current || !currentInfo) { el.textContent = ''; return; }
  if (currentInfo.online) {
    el.textContent = 'в сети';
    el.classList.add('online');
  } else {
    el.textContent = 'был(а): ' + fmtLastSeen(currentInfo.lastSeen);
    el.classList.remove('online');
  }
}

/* ---------- добавление ---------- */
$('addBtn').onclick = () => {
  const f = $('addForm');
  f.classList.toggle('open');
  if (f.classList.contains('open')) setTimeout(() => $('addInput').focus(), 60);
  $('addMsg').textContent = '';
};

$('addForm').onsubmit = async e => {
  e.preventDefault();
  const inp = $('addInput');
  const msg = $('addMsg');
  const nick = inp.value.trim();
  if (!nick) return;
  msg.className = 'add-msg';
  msg.textContent = '...';
  const fd = new FormData();
  fd.append('nick', nick);
  const r = await fetch('/api/contacts/add', { method: 'POST', body: fd });
  const d = await r.json().catch(() => ({ ok:false }));
  if (d.ok) {
    msg.className = 'add-msg ok';
    msg.textContent = 'Добавлено: ' + nick;
    inp.value = '';
    lastContactsSig = '';
    await loadContacts();
    setTimeout(() => { $('addForm').classList.remove('open'); $('addMsg').textContent = ''; }, 800);
    openDialog(nick);
  } else {
    msg.className = 'add-msg err';
    msg.textContent = d.error || 'Ошибка';
  }
};

/* ---------- UI ---------- */
$('sendBtn').onclick = send;
$('msgInput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && window.innerWidth > 900) {
    e.preventDefault(); send();
  }
});
$('msgInput').addEventListener('input', e => {
  const el = e.target;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
});
$('logoutBtn').onclick = async () => {
  if (ws) { try { ws.close(); } catch (e) {} }
  await fetch('/api/logout', { method: 'POST' });
  location.href = '/';
};
$('backBtn').onclick = () => {
  current = null;
  lastInfoSig = '';
  app.classList.remove('chat-open');
  app.classList.remove('info-open');
  $('chatTitle').textContent = 'Выберите собеседника';
  $('chatSub').textContent = '';
  $('messages').innerHTML =
    '<div class="empty">' +
      '<svg class="icon huge" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>' +
      '<div>Слева выберите контакт, чтобы начать переписку</div>' +
    '</div>';
  $('inputArea').style.display = 'none';
  $('infoContent').innerHTML =
    '<div class="info-empty">' +
      '<svg class="icon huge" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>' +
      '<div>Информация о собеседнике появится здесь</div>' +
    '</div>';
};
$('infoBtn').onclick = () => app.classList.toggle('info-open');
$('infoCloseBtn').onclick = () => app.classList.remove('info-open');
$('searchInput').addEventListener('input', e => { searchQuery = e.target.value; renderContacts(); });

/* ---------- лёгкий фон-поллинг (страховка на случай обрыва WS) ---------- */
setInterval(async () => {
  if (ws && ws.readyState === 1) return;   // WS жив — не дублируем нагрузку
  await loadContacts();
  if (current) { await refreshDialog(); await loadInfo(current); }
}, 30000);

init();
</script>
</body>
</html>
"""


# ================== МАРШРУТЫ ==================
def render_page(html: str) -> str:
    return html.replace("__SHARED_CSS__", SHARED_CSS).replace("__FOOTER__", FOOTER_HTML)


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    if current_user(request):
        return RedirectResponse("/chat")
    return render_page(LANDING_PAGE)


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
