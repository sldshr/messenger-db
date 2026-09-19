"""
SldChat — простой мессенджер на Python + FastAPI + WebSocket.
Запуск:  python main.py     (или: uvicorn main:app --host 0.0.0.0 --port 8000)
Всё хранится только в оперативной памяти процесса.
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
users: Dict[str, dict] = {}                  # nick -> {"password","created","last_seen"}
sessions: Dict[str, str] = {}                # token -> nick
contacts: Dict[str, Set[str]] = {}           # nick -> {друзья}
messages: List[dict] = []                    # {"id","from","to","text","time"}
reads: Dict[str, Dict[str, int]] = {}        # reader -> peer -> last_read_id
active_ws: Dict[str, Set[WebSocket]] = {}    # nick -> set(websocket)
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
    """Мгновенно шлём JSON всем открытым WS-сессиям пользователя."""
    socks = list(active_ws.get(nick, ()))
    if not socks:
        return
    text = json.dumps(payload, ensure_ascii=False)
    await asyncio.gather(
        *(_safe_send(ws, text) for ws in socks),
        return_exceptions=True,
    )


async def _safe_send(ws: WebSocket, text: str):
    try:
        await ws.send_text(text)
    except Exception:
        pass


async def notify_contacts(nick: str, payload: dict):
    for c in contacts.get(nick, set()):
        await send_ws(c, payload)


@app.middleware("http")
async def update_last_seen(request: Request, call_next):
    nick = current_user(request)
    if nick and nick in users:
        users[nick]["last_seen"] = time.time()
    return await call_next(request)


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

    # сообщаем контактам, что пользователь в сети
    await notify_contacts(nick, {"type": "presence", "nick": nick, "online": True})

    try:
        while True:
            # нас интересует только факт соединения; клиент присылает "ping"
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


# ================== API: АУТЕНТИФИКАЦИЯ ==================
@app.post("/api/register")
async def api_register(nick: str = Form(...), password: str = Form(...)):
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

    token = secrets.token_hex(16)
    sessions[token] = nick
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/api/login")
async def api_login(nick: str = Form(...), password: str = Form(...)):
    nick = nick.strip()
    u = users.get(nick)
    if not u or u["password"] != password:
        return JSONResponse({"ok": False, "error": "Неверный ник или пароль"}, status_code=400)
    token = secrets.token_hex(16)
    sessions[token] = nick
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    token = request.cookies.get("session")
    if token:
        sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    nick = current_user(request)
    if not nick:
        return JSONResponse({"ok": False}, status_code=401)
    return {"ok": True, "nick": nick}


# ================== API: КОНТАКТЫ ==================
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

    # двусторонняя связь
    contacts[me].add(nick)
    contacts[nick].add(me)

    info = user_public_info(nick)
    # моментально уведомляем обоих
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


# ================== API: ДИАЛОГ ==================
@app.get("/api/user/{nick}/info")
async def api_user_info(nick: str, request: Request):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)
    if nick not in users:
        return JSONResponse({"ok": False, "error": "Не найден"}, status_code=404)

    info = user_public_info(nick)
    info["msg_count"] = sum(1 for m in messages if (
        (m["from"] == me and m["to"] == nick) or
        (m["from"] == nick and m["to"] == me)
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

    # помечаем прочитанным
    max_id = reads.setdefault(me, {}).get(nick, 0)
    for m in messages:
        if m["from"] == nick and m["to"] == me and m["id"] > max_id:
            max_id = m["id"]
    reads[me][nick] = max_id

    out = [
        m for m in messages
        if m["id"] > since and (
            (m["from"] == me and m["to"] == nick) or
            (m["from"] == nick and m["to"] == me)
        )
    ]
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
    if to not in contacts.get(me, set()):
        return JSONResponse({"ok": False, "error": "Не в контактах"}, status_code=403)

    _msg_id += 1
    msg = {"id": _msg_id, "from": me, "to": to, "text": text, "time": time.time()}
    messages.append(msg)

    payload = {"type": "message", "message": msg}
    # мгновенная доставка через WebSocket (и получателю, и нам на все вкладки)
    await asyncio.gather(
        send_ws(to, payload),
        send_ws(me, payload),
    )
    return {"ok": True, "message": msg}


# ================== HTML: ГЛАВНАЯ ==================
LANDING_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SldChat — простой мессенджер</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: Tahoma, Verdana, Arial, sans-serif;
    font-size: 14px; color: #2b3a4a; line-height: 1.6;
    background: #eef2f6;
  }
  a { color: #3f6fa8; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .container { max-width: 1000px; margin: 0 auto; padding: 0 20px; }

  /* SVG иконки */
  .icon {
    width: 16px; height: 16px; flex-shrink: 0;
    stroke: currentColor; fill: none;
    stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
    vertical-align: -3px;
  }
  .icon.lg  { width: 22px; height: 22px; }
  .icon.xl  { width: 28px; height: 28px; stroke-width: 1.7; }
  .icon.huge{ width: 42px; height: 42px; stroke-width: 1.5; }

  /* Верхняя панель */
  .topbar {
    background: linear-gradient(#fbfcfd, #dce3ea);
    border-bottom: 1px solid #b7c2cd;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    position: sticky; top: 0; z-index: 50;
  }
  .topbar-inner {
    display: flex; align-items: center; gap: 12px;
    height: 56px;
  }
  .logo {
    display: flex; align-items: center; gap: 7px;
    font-size: 21px; font-weight: bold; color: #2b3a4a;
    text-shadow: 0 1px 0 #fff; letter-spacing: 0.5px;
  }
  .logo span { color: #3f6fa8; }
  .logo .icon { color: #3f6fa8; width: 22px; height: 22px; }

  .badge-closed {
    display: inline-flex; align-items: center; gap: 5px;
    background: #2b3a4a; color: #dbe5ef;
    border-radius: 12px;
    padding: 3px 10px 3px 8px;
    font-size: 10px; letter-spacing: 0.7px; text-transform: uppercase;
    font-weight: bold; font-family: Tahoma, sans-serif;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
  }
  .badge-closed .icon { width: 11px; height: 11px; stroke-width: 2.4; }

  .nav { display: flex; align-items: center; gap: 4px; margin-left: auto; }
  .nav a.navlink {
    padding: 7px 10px; border-radius: 4px; color: #3a5169; font-size: 13px;
  }
  .nav a.navlink:hover { background: #e3e9ef; text-decoration: none; }

  .btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 14px; border-radius: 4px;
    border: 1px solid #8b97a3; font-size: 13px; cursor: pointer;
    font-family: inherit; text-shadow: 0 1px 0 rgba(255,255,255,0.6);
    text-decoration: none !important; color: #2b3a4a;
    background: linear-gradient(#fbfcfd, #cfd8e0);
    white-space: nowrap;
  }
  .btn:hover { background: linear-gradient(#fff, #dbe3ea); }
  .btn-primary {
    background: linear-gradient(#5b8fc4, #3f6fa8);
    border-color: #35597f; color: #fff;
    text-shadow: 0 1px 0 rgba(0,0,0,0.2);
  }
  .btn-primary:hover { background: linear-gradient(#699bcd, #4577b1); }
  .btn-lg { padding: 11px 22px; font-size: 15px; }

  /* Hero */
  .hero {
    background:
      radial-gradient(circle at 20% 20%, #e6eef7 0%, transparent 60%),
      linear-gradient(#d5dfe9, #b8c6d3);
    border-bottom: 1px solid #a8b5c2;
    padding: 78px 0 88px;
    text-align: center;
    position: relative;
    overflow: hidden;
  }
  .hero::after {
    content: ''; position: absolute; left: 0; right: 0; bottom: 0;
    height: 1px; background: rgba(255,255,255,0.6);
  }
  .hero .badge-closed {
    background: #b7c8db; color: #2b3a4a;
    font-size: 11px; padding: 4px 12px 4px 10px;
    box-shadow: inset 0 1px 0 #fff, 0 1px 0 rgba(0,0,0,0.05);
    margin-bottom: 22px;
  }
  .hero .badge-closed .icon { width: 12px; height: 12px; }
  .hero h1 {
    font-size: 46px; font-weight: normal; margin: 0 0 18px;
    color: #23374b; text-shadow: 0 1px 0 #fff; line-height: 1.1;
    letter-spacing: 0.4px;
  }
  .hero h1 b { color: #3f6fa8; font-weight: bold; }
  .hero p {
    max-width: 580px; margin: 0 auto 30px;
    color: #4a5f74; font-size: 16px;
  }
  .hero-actions { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }

  /* Секции */
  .section { padding: 60px 0; border-bottom: 1px solid #dbe1e7; }
  .section:nth-child(even) { background: #f6f8fa; }
  .section h2 {
    font-size: 27px; font-weight: normal; margin: 0 0 8px; color: #23374b;
    text-shadow: 0 1px 0 #fff;
  }
  .section .lead {
    color: #6f7c8b; font-size: 14px; margin: 0 0 26px;
  }
  .section p { margin: 0 0 12px; color: #47586c; }

  /* Сетка фич */
  .cards {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px;
    margin-top: 10px;
  }
  .card {
    background: #fff; border: 1px solid #cfd7df; border-radius: 6px;
    padding: 22px 20px 22px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05), inset 0 1px 0 #fff;
  }
  .card .ico {
    width: 46px; height: 46px; margin-bottom: 14px;
    border-radius: 8px;
    background: linear-gradient(#e4ebf2, #c8d3de);
    border: 1px solid #b7c2cd;
    display: flex; align-items: center; justify-content: center;
    color: #3f6fa8;
    box-shadow: inset 0 1px 0 #fff;
  }
  .card h3 { margin: 0 0 6px; font-size: 15px; color: #2b3a4a; }
  .card p  { margin: 0; font-size: 13px; color: #5a6c80; }

  /* Серверы */
  .servers {
    display: grid; grid-template-columns: 1fr 1fr; gap: 18px;
    margin-top: 10px;
  }
  .server-card {
    background: #fff; border: 1px solid #cfd7df; border-radius: 6px;
    padding: 20px 22px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05), inset 0 1px 0 #fff;
    display: flex; align-items: center; gap: 14px;
  }
  .server-card .ico {
    width: 46px; height: 46px; border-radius: 8px;
    background: linear-gradient(#e4ebf2, #c8d3de);
    border: 1px solid #b7c2cd;
    display: flex; align-items: center; justify-content: center;
    color: #3f6fa8; flex-shrink: 0;
    box-shadow: inset 0 1px 0 #fff;
  }
  .server-card .body { min-width: 0; }
  .server-card .url {
    font-family: Consolas, "Courier New", monospace;
    font-size: 15px; color: #2b3a4a; font-weight: bold;
    word-break: break-all;
  }
  .server-card .desc { font-size: 12px; color: #7a8695; margin-top: 3px; }

  .notice {
    margin-top: 22px;
    background: #fff8e1; border: 1px solid #ecdc9b; border-radius: 6px;
    padding: 14px 16px;
    display: flex; gap: 12px; align-items: flex-start;
    color: #6f5a13;
  }
  .notice .icon { color: #b8860b; flex-shrink: 0; margin-top: 2px; }
  .notice b { color: #4f3e07; }

  /* FAQ */
  .faq { max-width: 760px; margin: 0 auto; }
  .faq details {
    background: #fff; border: 1px solid #cfd7df; border-radius: 5px;
    margin-bottom: 10px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }
  .faq summary {
    padding: 13px 18px; cursor: pointer; font-weight: bold; color: #2b3a4a;
    outline: none; list-style: none; position: relative;
    display: flex; align-items: center; gap: 10px;
  }
  .faq summary::-webkit-details-marker { display: none; }
  .faq summary::before {
    content: '+'; display: inline-block;
    color: #3f6fa8; font-weight: bold; font-size: 16px;
    width: 12px; text-align: center;
  }
  .faq details[open] summary::before { content: '−'; }
  .faq .answer { padding: 0 18px 15px 40px; color: #55677b; font-size: 13px; }

  /* Футер */
  footer {
    background: #2b3a4a; color: #b8c4ce;
    padding: 30px 0; font-size: 12px;
  }
  footer .foot-inner {
    display: flex; align-items: center; justify-content: space-between;
    gap: 14px; flex-wrap: wrap;
  }
  footer a { color: #9db8d3; }
  footer .brand {
    display: flex; align-items: center; gap: 8px;
    font-size: 14px; color: #dbe5ef; font-weight: bold;
  }
  footer .brand .icon { color: #9db8d3; }

  @media (max-width: 820px) {
    .nav a.navlink { display: none; }
    .topbar-inner { gap: 8px; }
    .badge-closed { display: none; }
    .hero { padding: 50px 0 60px; }
    .hero h1 { font-size: 30px; }
    .hero p { font-size: 14px; }
    .cards { grid-template-columns: 1fr; }
    .servers { grid-template-columns: 1fr; }
    .section { padding: 40px 0; }
    .section h2 { font-size: 22px; }
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
    <span class="badge-closed">
      <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      ClosedSource
    </span>
    <nav class="nav">
      <a class="navlink" href="#about">О нас</a>
      <a class="navlink" href="#app">О приложении</a>
      <a class="navlink" href="#servers">Серверы</a>
      <a class="navlink" href="#faq">FAQ</a>
      <a class="btn" href="/login">Войти</a>
      <a class="btn btn-primary" href="/register">Регистрация</a>
    </nav>
  </div>
</header>

<section class="hero">
  <div class="container">
    <span class="badge-closed">
      <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      ClosedSource
    </span>
    <h1>Мессенджер <b>SldChat</b> —<br>общайтесь по-простому</h1>
    <p>Никаких лишних настроек. Регистрация за 5 секунд, добавление по нику,
    мгновенная доставка сообщений через WebSocket.</p>
    <div class="hero-actions">
      <a class="btn btn-primary btn-lg" href="/register">Создать аккаунт</a>
      <a class="btn btn-lg" href="/login">У меня уже есть аккаунт</a>
    </div>
  </div>
</section>

<section id="about" class="section">
  <div class="container">
    <h2>О нас</h2>
    <p class="lead">Небольшая команда энтузиастов, которой надоели перегруженные мессенджеры.</p>
    <p>SldChat — закрытый по исходникам проект на Python и FastAPI. Мы не храним ничего лишнего
    и не собираем ваши данные: сервер живёт в оперативной памяти, а сессии — только в cookie.</p>
    <p>Проект создан как инструмент для общения внутри небольших команд и компаний друзей,
    которым не нужны стикеры, реакции и «истории».</p>
  </div>
</section>

<section id="app" class="section">
  <div class="container">
    <h2>О приложении</h2>
    <p class="lead">Клиент работает прямо в браузере — на компьютере и на смартфоне.</p>
    <div class="cards">
      <div class="card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
        </div>
        <h3>Мгновенная доставка</h3>
        <p>Сообщения летят через WebSocket — собеседник видит их за миллисекунды, без всяких «обновлений».</p>
      </div>
      <div class="card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>
        </div>
        <h3>Добавление по нику</h3>
        <p>Никаких глобальных списков. Введите ник — и вы с человеком сразу друг у друга.</p>
      </div>
      <div class="card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        </div>
        <h3>Приватность</h3>
        <p>Сообщения не сохраняются на диске. История живёт, пока жив сервер, — и только в памяти.</p>
      </div>
    </div>
  </div>
</section>

<section id="servers" class="section">
  <div class="container">
    <h2>Серверы SldChat</h2>
    <p class="lead">Проект работает на двух независимых серверах. Выбирайте любой.</p>

    <div class="servers">
      <div class="server-card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
        </div>
        <div class="body">
          <div class="url">sldchat.fastapicloud.dev</div>
          <div class="desc">Основной сервер · FastAPI Cloud</div>
        </div>
      </div>

      <div class="server-card">
        <div class="ico">
          <svg class="icon xl" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
        </div>
        <div class="body">
          <div class="url">sldchat.onrunxbuild.com</div>
          <div class="desc">Резервный сервер · onrunxbuild</div>
        </div>
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

<section id="faq" class="section">
  <div class="container">
    <h2>Ответы на вопросы</h2>
    <div class="faq">
      <details>
        <summary>Сколько стоит SldChat?</summary>
        <div class="answer">Нисколько. Проект полностью бесплатный, без рекламы и подписок.</div>
      </details>
      <details>
        <summary>Почему ClosedSource?</summary>
        <div class="answer">Исходный код проекта не публикуется. Это осознанное решение —
        мы не хотим, чтобы под именем SldChat появлялись сторонние клоны.</div>
      </details>
      <details>
        <summary>Сохраняются ли мои сообщения?</summary>
        <div class="answer">Нет. Всё хранится только в оперативной памяти сервера и исчезает
        при его перезапуске. На диск ничего не пишется.</div>
      </details>
      <details>
        <summary>Почему я вижу только своих контактов?</summary>
        <div class="answer">В SldChat нет публичного каталога пользователей. Чтобы начать общение,
        нажмите «Добавить контакт» и введите ник собеседника. После этого вы появитесь
        в его списке контактов, а он — в вашем.</div>
      </details>
      <details>
        <summary>Можно ли удалять сообщения?</summary>
        <div class="answer">Нет. Отправленное сообщение остаётся в истории до перезапуска сервера.
        Так что пишите осознанно :)</div>
      </details>
      <details>
        <summary>Как выйти из аккаунта?</summary>
        <div class="answer">Кнопка выхода — в шапке приложения (значок стрелки из двери).</div>
      </details>
    </div>
  </div>
</section>

<footer>
  <div class="container foot-inner">
    <div class="brand">
      <svg class="icon" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
      SldChat © 2026 · ClosedSource
    </div>
    <div>
      <a href="#about">О нас</a> · <a href="#faq">FAQ</a> ·
      <a href="/register">Регистрация</a>
    </div>
  </div>
</footer>

</body>
</html>
"""


# ================== HTML: АВТОРИЗАЦИЯ ==================
def render_auth(active: str) -> str:
    login_active = "active" if active == "login" else ""
    reg_active = "active" if active == "register" else ""
    login_style = "" if active == "login" else "display:none"
    reg_style = "" if active == "register" else "display:none"
    title = "Вход" if active == "login" else "Регистрация"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SldChat — {title}</title>
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; min-height: 100%; }}
  body {{
    font-family: Tahoma, Verdana, Arial, sans-serif;
    font-size: 13px; color: #2b3a4a;
    background:
      radial-gradient(circle at 30% 10%, #e6eef6 0%, transparent 55%),
      linear-gradient(#cfd9e3, #a8b6c4);
    min-height: 100vh;
    display: flex; flex-direction: column;
  }}
  a {{ color: #3f6fa8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  .icon {{
    width: 18px; height: 18px;
    stroke: currentColor; fill: none;
    stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
    vertical-align: -3px;
  }}

  .topline {{
    padding: 16px 22px;
    display: flex; align-items: center; gap: 10px;
  }}
  .topline a.logo {{
    display: flex; align-items: center; gap: 7px;
    font-size: 20px; font-weight: bold; color: #2b3a4a;
    text-shadow: 0 1px 0 #fff; letter-spacing: 0.5px;
  }}
  .topline a.logo span {{ color: #3f6fa8; }}
  .topline a.logo .icon {{ width: 22px; height: 22px; color: #3f6fa8; }}

  .badge-closed {{
    display: inline-flex; align-items: center; gap: 5px;
    background: #2b3a4a; color: #dbe5ef;
    border-radius: 12px; padding: 3px 10px 3px 8px;
    font-size: 10px; letter-spacing: 0.7px; text-transform: uppercase;
    font-weight: bold;
  }}
  .badge-closed .icon {{ width: 11px; height: 11px; stroke-width: 2.4; }}

  .wrap {{
    flex: 1; display: flex; align-items: center; justify-content: center;
    padding: 20px;
  }}

  .box {{
    width: 100%; max-width: 380px;
    background: #f4f6f8;
    border: 1px solid #8b97a3; border-radius: 6px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.22), inset 0 1px 0 #fff;
    padding: 22px 26px 26px;
  }}
  .box h1 {{
    text-align: center; font-size: 20px; font-weight: normal;
    margin: 0 0 18px; color: #23374b; text-shadow: 0 1px 0 #fff;
    letter-spacing: 1px;
  }}
  .tabs {{
    display: flex; margin-bottom: 16px;
    border-bottom: 1px solid #a8b2bc;
  }}
  .tabs button {{
    flex: 1; border: 1px solid #a8b2bc; border-bottom: none;
    background: linear-gradient(#eef1f4, #d3dae1);
    border-radius: 4px 4px 0 0;
    padding: 8px 0; margin-right: 4px; cursor: pointer;
    font-family: inherit; font-size: 13px; color: #445;
  }}
  .tabs button.active {{
    background: #f4f6f8; color: #223; font-weight: bold;
    position: relative; top: 1px;
  }}

  label {{ display: block; margin: 10px 0 4px; color: #445; }}
  input[type=text], input[type=password] {{
    width: 100%; padding: 8px 10px;
    font-family: inherit; font-size: 13px;
    border: 1px solid #9aa4ae; border-radius: 3px;
    background: #fff;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
    outline: none;
  }}
  input:focus {{ border-color: #5a7a9a; }}

  .btn {{
    display: flex; align-items: center; justify-content: center; gap: 8px;
    width: 100%; margin-top: 18px;
    padding: 10px 0; border-radius: 4px;
    font-family: inherit; font-size: 13px; cursor: pointer;
    background: linear-gradient(#fbfcfd, #ccd5de);
    border: 1px solid #7a8794; color: #2b3a4a;
    text-shadow: 0 1px 0 #fff;
  }}
  .btn-primary {{
    background: linear-gradient(#5b8fc4, #3f6fa8);
    border-color: #35597f; color: #fff;
    text-shadow: 0 1px 0 rgba(0,0,0,0.2);
  }}
  .btn-primary:hover {{ background: linear-gradient(#699bcd, #4577b1); }}

  .error {{
    min-height: 18px; color: #c22; text-align: center;
    font-size: 12px; margin-bottom: 4px;
  }}
  .hint {{
    margin-top: 14px; text-align: center; color: #889; font-size: 11px;
  }}
  .back {{ text-align: center; margin-top: 14px; font-size: 12px; }}
</style>
</head>
<body>

<div class="topline">
  <a href="/" class="logo">
    <svg class="icon" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
    Sld<span>Chat</span>
  </a>
  <span class="badge-closed">
    <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
    ClosedSource
  </span>
</div>

<div class="wrap">
  <div class="box">
    <h1>Добро пожаловать</h1>
    <div class="tabs">
      <button type="button" id="tabLogin" class="{login_active}">Вход</button>
      <button type="button" id="tabRegister" class="{reg_active}">Регистрация</button>
    </div>
    <div class="error" id="error"></div>

    <form id="formLogin" style="{login_style}">
      <label>Ник:</label>
      <input type="text" name="nick" autocomplete="username" maxlength="20">
      <label>Пароль:</label>
      <input type="password" name="password" autocomplete="current-password">
      <button type="submit" class="btn btn-primary">Войти</button>
    </form>

    <form id="formRegister" style="{reg_style}">
      <label>Ник:</label>
      <input type="text" name="nick" autocomplete="username" maxlength="20" placeholder="3–20 символов">
      <label>Пароль:</label>
      <input type="password" name="password" autocomplete="new-password" placeholder="минимум 3 символа">
      <button type="submit" class="btn btn-primary">Зарегистрироваться</button>
    </form>

    <div class="hint">Всё хранится только в оперативной памяти</div>
    <div class="back"><a href="/">← На главную</a></div>
  </div>
</div>

<script>
  const tabLogin = document.getElementById('tabLogin');
  const tabRegister = document.getElementById('tabRegister');
  const formLogin = document.getElementById('formLogin');
  const formRegister = document.getElementById('formRegister');
  const errorBox = document.getElementById('error');

  function showTab(which) {{
    if (which === 'login') {{
      tabLogin.classList.add('active'); tabRegister.classList.remove('active');
      formLogin.style.display = ''; formRegister.style.display = 'none';
    }} else {{
      tabRegister.classList.add('active'); tabLogin.classList.remove('active');
      formRegister.style.display = ''; formLogin.style.display = 'none';
    }}
    errorBox.textContent = '';
  }}
  tabLogin.onclick = () => {{ showTab('login'); history.replaceState(null, '', '/login'); }};
  tabRegister.onclick = () => {{ showTab('register'); history.replaceState(null, '', '/register'); }};

  async function submitForm(url, form) {{
    errorBox.textContent = '';
    const fd = new FormData(form);
    let d;
    try {{
      const r = await fetch(url, {{ method: 'POST', body: fd }});
      d = await r.json();
    }} catch (e) {{
      d = {{ ok:false, error:'Ошибка соединения' }};
    }}
    if (d.ok) {{ location.href = '/chat'; return; }}
    errorBox.textContent = d.error || 'Ошибка';
  }}

  formLogin.onsubmit = e => {{ e.preventDefault(); submitForm('/api/login', formLogin); }};
  formRegister.onsubmit = e => {{ e.preventDefault(); submitForm('/api/register', formRegister); }};
</script>
</body>
</html>
"""


# ================== HTML: ЧАТ ==================
CHAT_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>SldChat</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; overflow: hidden; }
  body {
    font-family: Tahoma, Verdana, Arial, sans-serif;
    font-size: 13px; color: #2b3a4a; background: #e9eef3;
  }

  /* SVG */
  .icon {
    width: 16px; height: 16px; flex-shrink: 0;
    stroke: currentColor; fill: none;
    stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
    vertical-align: -3px;
  }
  .icon.lg { width: 20px; height: 20px; }
  .icon.huge { width: 44px; height: 44px; stroke-width: 1.5; color: #c1cbd5; }

  /* ====== Layout ====== */
  .app {
    display: grid;
    grid-template-columns: 280px 1fr 260px;
    height: 100vh; width: 100vw; overflow: hidden;
    background: #e9eef3;
  }

  /* ====== Сайдбар ====== */
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
    background: #e8edf3;
    border-bottom: 1px solid #d3dae0;
    display: flex; align-items: center; justify-content: space-between;
    gap: 6px; font-size: 12px; color: #5a6c80;
  }
  .me-line .nick { font-weight: bold; color: #2b3a4a; }

  .icon-btn {
    border: 1px solid #b5bec8; border-radius: 4px;
    background: linear-gradient(#fbfcfd, #dce3ea);
    padding: 4px 7px; cursor: pointer; line-height: 1;
    color: #3a5169;
    display: inline-flex; align-items: center; justify-content: center;
  }
  .icon-btn:hover { background: linear-gradient(#fff, #dbe3ea); }
  .icon-btn:active { box-shadow: inset 0 1px 2px rgba(0,0,0,0.15); }

  .add-wrap { padding: 8px 9px; border-bottom: 1px solid #dbe1e7; }
  .add-btn {
    width: 100%; padding: 8px 10px;
    display: flex; align-items: center; justify-content: center; gap: 7px;
    border: 1px solid #7a8794; border-radius: 4px;
    background: linear-gradient(#fbfcfd, #cfd8e0);
    font-family: inherit; font-size: 12px; color: #2b3a4a;
    cursor: pointer; text-shadow: 0 1px 0 #fff;
  }
  .add-btn:hover { background: linear-gradient(#fff, #dbe3ea); }

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
  .add-msg {
    font-size: 11px; margin-top: 5px; min-height: 14px; color: #7a8695;
  }
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
  }
  .user-item:hover  { background: #eaf0f6; }
  .user-item.active { background: #d3e0ec; }
  .user-item .row1 {
    display: flex; justify-content: space-between; align-items: center;
    gap: 6px;
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
  .user-item .nick .dot.on { background: #4caf50; }
  .user-item .time {
    font-size: 10px; color: #8a94a0; flex-shrink: 0;
  }
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

  /* ====== Чат ====== */
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
  }
  .input-area button:hover { background: linear-gradient(#699bcd, #4577b1); }

  /* ====== Инфо-панель ====== */
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
    color: #3f6fa8; box-shadow: inset 0 1px 0 #fff;
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

  /* ====== Тост ====== */
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
    animation: toast-in 0.18s ease-out;
  }
  @keyframes toast-in {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  /* ====== Mobile ====== */
  @media (max-width: 900px) {
    .app { grid-template-columns: 1fr; position: relative; }
    .sidebar { border-right: none; }
    .chat { display: none; }
    .info-panel {
      position: fixed; top: 0; right: 0; bottom: 0;
      width: min(300px, 85vw); z-index: 40;
      transform: translateX(100%);
      transition: transform 0.22s ease;
      box-shadow: -4px 0 14px rgba(0,0,0,0.18);
      border-left: 1px solid #b0bac4;
    }
    .app.chat-open .sidebar { display: none; }
    .app.chat-open .chat { display: flex; }
    .app.info-open .info-panel { transform: translateX(0); }
    .info-panel .close-btn {
      display: inline-flex; float: right; margin: -4px -4px 0 0;
    }
    .mobile-only { display: inline-flex; }
    .msg-bubble { max-width: 82%; }
  }

  .user-list::-webkit-scrollbar,
  .messages::-webkit-scrollbar,
  .info-panel::-webkit-scrollbar { width: 10px; }
  .user-list::-webkit-scrollbar-track,
  .messages::-webkit-scrollbar-track,
  .info-panel::-webkit-scrollbar-track { background: #eef2f6; }
  .user-list::-webkit-scrollbar-thumb,
  .messages::-webkit-scrollbar-thumb,
  .info-panel::-webkit-scrollbar-thumb {
    background: #c1cbd5; border-radius: 5px; border: 2px solid #eef2f6;
  }
</style>
</head>
<body>

<div class="app" id="app">

  <!-- ======== Сайдбар ======== -->
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

    <div class="me-line">
      Вы вошли как <span class="nick" id="myNick">…</span>
    </div>

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

  <!-- ======== Чат ======== -->
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
      <button id="sendBtn">
        <svg class="icon" viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        Отправить
      </button>
    </div>
  </main>

  <!-- ======== Инфо-панель ======== -->
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
/* ==================== Состояние ==================== */
let me = null;
let current = null;
const lastIds = {};
const renderedIds = new Set();
let usersCache = [];
let searchQuery = '';
let currentInfo = null;
let ws = null;
let wsReconnectTimer = null;

const $ = id => document.getElementById(id);
const app = $('app');

/* ==================== Утилиты ==================== */
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
  return el.scrollHeight - el.scrollTop - el.clientHeight < 140;
}
function toast(msg) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  $('toastWrap').appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

/* ==================== WebSocket ==================== */
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try {
    ws = new WebSocket(proto + '//' + location.host + '/ws');
  } catch (e) {
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    // периодический ping — держим соединение живым
    if (ws._pingTimer) clearInterval(ws._pingTimer);
    ws._pingTimer = setInterval(() => {
      if (ws && ws.readyState === 1) {
        try { ws.send('ping'); } catch (e) {}
      }
    }, 25000);
  };

  ws.onmessage = e => {
    let d;
    try { d = JSON.parse(e.data); } catch (_) { return; }
    handleWS(d);
  };

  ws.onclose = () => {
    if (ws && ws._pingTimer) clearInterval(ws._pingTimer);
    scheduleReconnect();
  };
  ws.onerror = () => {
    try { ws.close(); } catch (e) {}
  };
}

function scheduleReconnect() {
  if (wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    connectWS();
  }, 1200);
}

function handleWS(d) {
  if (d.type === 'message') {
    const m = d.message;
    const peer = m.from === me ? m.to : m.from;
    // если открыт диалог с этим человеком — сразу показываем
    if (current === peer) {
      if (!renderedIds.has(m.id)) {
        renderedIds.add(m.id);
        appendMessage(m);
        lastIds[current] = Math.max(lastIds[current] || 0, m.id);
      }
      // если сообщение от него — оно прочитано
      if (m.from !== me) markRead(peer);
    }
    // обновим список
    loadContacts();
  } else if (d.type === 'presence') {
    updatePresence(d.nick, d.online);
    loadContacts();
  } else if (d.type === 'contact_added') {
    toast('Новый контакт: ' + d.nick);
    loadContacts();
  }
}

function updatePresence(nick, online) {
  const u = usersCache.find(x => x.nick === nick);
  if (u) u.online = online;
  renderContacts();
  if (current === nick) {
    currentInfo = { ...(currentInfo || {}), online };
    updateChatSubtitle();
  }
}

/* ==================== Инициализация ==================== */
async function init() {
  const r = await fetch('/api/me');
  if (!r.ok) { location.href = '/'; return; }
  const d = await r.json();
  me = d.nick;
  $('myNick').textContent = me;
  connectWS();
  await loadContacts();
}

/* ==================== Контакты ==================== */
async function loadContacts() {
  const r = await fetch('/api/contacts');
  if (!r.ok) { location.href = '/'; return; }
  const d = await r.json();
  usersCache = d.contacts || [];
  renderContacts();
}

function renderContacts() {
  const list = $('userList');
  list.innerHTML = '';
  const q = searchQuery.trim().toLowerCase();
  const filtered = q
    ? usersCache.filter(u => u.nick.toLowerCase().includes(q))
    : usersCache;

  if (!filtered.length) {
    const e = document.createElement('div');
    e.className = 'empty-list';
    e.innerHTML =
      '<svg class="icon huge" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>' +
      '<div>' + (q ? 'Ничего не найдено' : 'Пока нет контактов.<br>Нажмите «Добавить контакт».') + '</div>';
    list.appendChild(e);
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
}

/* ==================== Диалог ==================== */
async function openDialog(nick) {
  current = nick;
  lastIds[nick] = 0;
  renderedIds.clear();

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
    '<div class="msg-bubble">' +
      esc(m.text) +
      '<div class="msg-meta">' + fmtTime(m.time) + '</div>' +
    '</div>';
  box.appendChild(row);

  const nearBottom = scrollIfNearBottom(box) || m.from === me;
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

async function markRead(nick) {
  // достаточно вызвать /api/dialog — он проставляет read
  try {
    await fetch('/api/dialog/' + encodeURIComponent(nick) + '?since=0', { method: 'GET' });
  } catch (e) {}
  loadContacts();
}

/* ==================== Отправка ==================== */
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
  if (!d.ok) {
    toast(d.error || 'Не удалось отправить');
    return;
  }
  // На случай если WS-эхо не пришло моментально — добавим сами
  if (!renderedIds.has(d.message.id)) {
    renderedIds.add(d.message.id);
    lastIds[current] = Math.max(lastIds[current] || 0, d.message.id);
    appendMessage(d.message);
  }
  loadContacts();
  loadInfo(current);
}

/* ==================== Инфо о собеседнике ==================== */
async function loadInfo(nick) {
  if (!nick) {
    $('infoContent').innerHTML =
      '<div class="info-empty">' +
        '<svg class="icon huge" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>' +
        '<div>Информация о собеседнике появится здесь</div>' +
      '</div>';
    return;
  }
  const r = await fetch('/api/user/' + encodeURIComponent(nick) + '/info');
  if (!r.ok) return;
  const d = await r.json();
  if (!d.ok) return;
  const i = d.info;
  currentInfo = { online: i.online, lastSeen: i.last_seen };

  const status = i.online
    ? '<div class="status online">В сети</div>'
    : '<div class="status">Был(а): ' + esc(fmtLastSeen(i.last_seen)) + '</div>';

  $('infoContent').innerHTML =
    '<div class="info-head">' +
      '<div class="avatar-none">' +
        '<svg class="icon" style="width:26px;height:26px" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>' +
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

/* ==================== Добавление контакта ==================== */
$('addBtn').onclick = () => {
  const f = $('addForm');
  f.classList.toggle('open');
  if (f.classList.contains('open')) $('addInput').focus();
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
    await loadContacts();
    setTimeout(() => { $('addForm').classList.remove('open'); $('addMsg').textContent = ''; }, 900);
    openDialog(nick);
  } else {
    msg.className = 'add-msg err';
    msg.textContent = d.error || 'Ошибка';
  }
};

/* ==================== UI события ==================== */
$('sendBtn').onclick = send;

$('msgInput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
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

$('searchInput').addEventListener('input', e => {
  searchQuery = e.target.value;
  renderContacts();
});

/* ==================== Поллинг (страховка) ==================== */
setInterval(async () => {
  await loadContacts();
  if (current) {
    await refreshDialog();
    await loadInfo(current);
  }
}, 6000);

init();
</script>
</body>
</html>
"""


# ================== МАРШРУТЫ ==================
@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    if current_user(request):
        return RedirectResponse("/chat")
    return LANDING_PAGE


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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
