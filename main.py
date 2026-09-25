"""
EDEX://MESSENGER
Одностраничный мессенджер в стиле edex-ui.
Запуск:  python main.py   ->  http://127.0.0.1:8000
"""

import asyncio
import time
from collections import deque
from typing import Deque, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

MAX_HISTORY = 200
MAX_MSG_LEN = 2000
MAX_NAME_LEN = 24
DEFAULT_ROOMS = ["general", "random", "tech", "offtopic"]

app = FastAPI(title="edex-msg")


# ────────────────────────────── HUB ──────────────────────────────
class Hub:
    def __init__(self) -> None:
        self.history: Dict[str, Deque[dict]] = {r: deque(maxlen=MAX_HISTORY) for r in DEFAULT_ROOMS}
        self.rooms: Dict[str, Dict[WebSocket, str]] = {r: {} for r in DEFAULT_ROOMS}
        self.lock = asyncio.Lock()

    async def ensure(self, room: str) -> None:
        async with self.lock:
            self.history.setdefault(room, deque(maxlen=MAX_HISTORY))
            self.rooms.setdefault(room, {})

    async def add(self, ws: WebSocket, name: str, room: str) -> None:
        await self.ensure(room)
        async with self.lock:
            self.rooms[room][ws] = name

    async def remove(self, ws: WebSocket) -> Optional[str]:
        async with self.lock:
            for room, users in self.rooms.items():
                if ws in users:
                    del users[ws]
                    return room
        return None

    async def move(self, ws: WebSocket, name: str, frm: str, to: str) -> None:
        await self.ensure(to)
        async with self.lock:
            if frm in self.rooms:
                self.rooms[frm].pop(ws, None)
            self.rooms[to][ws] = name

    def users(self, room: str) -> List[str]:
        return sorted(set(self.rooms.get(room, {}).values()))

    def room_list(self) -> List[str]:
        return sorted(self.rooms.keys())

    async def send_room(self, room: str, payload: dict, skip: Optional[WebSocket] = None) -> None:
        dead = []
        for ws in list(self.rooms.get(room, {}).keys()):
            if ws is skip:
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.remove(ws)

    async def send_all(self, payload: dict) -> None:
        for room in list(self.rooms.keys()):
            await self.send_room(room, payload)


hub = Hub()


def ts_now() -> float:
    return time.time()


async def push_users(room: str) -> None:
    await hub.send_room(room, {"type": "users", "users": hub.users(room)})


async def push_rooms() -> None:
    await hub.send_all({"type": "rooms", "rooms": hub.room_list()})


# ────────────────────────────── ROUTES ──────────────────────────────
@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    username: Optional[str] = None
    room: Optional[str] = None

    try:
        first = await ws.receive_json()
        if first.get("type") != "join":
            await ws.close()
            return

        username = (str(first.get("username") or "anon").strip()[:MAX_NAME_LEN]) or "anon"
        room = str(first.get("room") or "general").strip()[:32] or "general"

        await hub.add(ws, username, room)
        await ws.send_json({
            "type": "history",
            "room": room,
            "messages": list(hub.history.get(room, [])),
        })
        await ws.send_json({"type": "rooms", "rooms": hub.room_list(), "current": room})
        await ws.send_json({"type": "users", "users": hub.users(room)})
        await hub.send_room(
            room,
            {"type": "system", "text": f"{username} подключился к # {room}", "ts": ts_now()},
            skip=ws,
        )
        await push_users(room)

        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "message":
                text = str(msg.get("text") or "").strip()
                if not text:
                    continue
                text = text[:MAX_MSG_LEN]
                payload = {
                    "type": "message",
                    "user": username,
                    "text": text,
                    "ts": ts_now(),
                }
                hub.history.setdefault(room, deque(maxlen=MAX_HISTORY)).append(payload)
                await hub.send_room(room, payload)

            elif mtype == "switch_room":
                new_room = str(msg.get("room") or "").strip()[:32]
                if not new_room or new_room == room:
                    continue
                await hub.ensure(new_room)
                old_room = room
                await hub.move(ws, username, old_room, new_room)
                await hub.send_room(
                    old_room,
                    {"type": "system", "text": f"{username} покинул канал", "ts": ts_now()},
                    skip=ws,
                )
                await push_users(old_room)
                room = new_room
                await ws.send_json({
                    "type": "history",
                    "room": room,
                    "messages": list(hub.history.get(room, [])),
                })
                await ws.send_json({"type": "rooms", "rooms": hub.room_list(), "current": room})
                await ws.send_json({"type": "users", "users": hub.users(room)})
                await hub.send_room(
                    room,
                    {"type": "system", "text": f"{username} подключился к # {room}", "ts": ts_now()},
                    skip=ws,
                )
                await push_users(room)
                await push_rooms()

            elif mtype == "create_room":
                new_room = str(msg.get("room") or "").strip()[:32]
                if not new_room:
                    continue
                await hub.ensure(new_room)
                await push_rooms()
                await ws.send_json({"type": "system", "text": f"канал # {new_room} доступен", "ts": ts_now()})

            elif mtype == "typing":
                await hub.send_room(room, {"type": "typing", "user": username}, skip=ws)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if username:
            left = await hub.remove(ws)
            if left:
                await hub.send_room(
                    left,
                    {"type": "system", "text": f"{username} отключился", "ts": ts_now()},
                )
                await push_users(left)


# ────────────────────────────── FRONTEND ──────────────────────────────
INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EDEX://MESSENGER</title>
<style>
  :root{
    --bg:#04070a;
    --bg-2:#08111a;
    --panel:#0a1219;
    --panel-hi:#0d1a24;
    --border:#12303d;
    --border-hi:#00d9ff;
    --text:#a9c6d4;
    --dim:#4a6a78;
    --accent:#00d9ff;
    --accent-2:#01ff70;
    --warn:#ffb000;
    --pink:#ff2e88;
    --glow:0 0 8px rgba(0,217,255,.55);
    --glow-soft:0 0 12px rgba(0,217,255,.25);
  }
  *{box-sizing:border-box;}
  html,body{height:100%;margin:0;overflow:hidden;}
  body{
    background:
      radial-gradient(1200px 800px at 20% -10%, rgba(0,217,255,.06), transparent 60%),
      radial-gradient(900px 700px at 90% 110%, rgba(1,255,112,.05), transparent 60%),
      var(--bg);
    color:var(--text);
    font-family:'JetBrains Mono','Fira Code','Cascadia Code',Consolas,monospace;
    font-size:13px;
    letter-spacing:.2px;
  }
  body::after{
    content:'';position:fixed;inset:0;pointer-events:none;z-index:9999;
    background:repeating-linear-gradient(0deg,rgba(0,0,0,0) 0 2px,rgba(0,217,255,.018) 3px 4px);
    mix-blend-mode:screen;
  }
  body::before{
    content:'';position:fixed;inset:0;pointer-events:none;z-index:9998;
    background:radial-gradient(ellipse at center, transparent 40%, rgba(0,0,0,.55) 100%);
  }

  /* ── layout ── */
  #app{
    display:grid;
    grid-template-columns:250px 1fr;
    grid-template-rows:46px 1fr;
    grid-template-areas:"top top" "side main";
    height:100vh;
    gap:10px;
    padding:10px;
  }

  /* ── topbar ── */
  #topbar{
    grid-area:top;
    display:flex;align-items:center;justify-content:space-between;
    padding:0 14px;
    border:1px solid var(--border);
    background:linear-gradient(180deg,var(--panel-hi),var(--panel));
    position:relative;
  }
  #topbar::after{
    content:'';position:absolute;left:0;right:0;bottom:-1px;height:1px;
    background:linear-gradient(90deg,transparent,var(--accent),transparent);
    box-shadow:var(--glow);
    opacity:.65;
  }
  .brand{
    font-weight:700;letter-spacing:3px;font-size:14px;
    color:var(--accent);
    text-shadow:var(--glow);
  }
  .brand span{color:var(--pink);text-shadow:0 0 8px rgba(255,46,136,.55);}
  .top-stats{display:flex;gap:18px;font-size:11px;letter-spacing:1.5px;}
  .stat{display:flex;gap:8px;align-items:center;}
  .stat .lbl{color:var(--dim);}
  .stat span:last-child{color:var(--accent);text-shadow:var(--glow-soft);}
  .stat #status.on{color:var(--accent-2);text-shadow:0 0 8px rgba(1,255,112,.55);}
  .stat #status.off{color:var(--pink);text-shadow:0 0 8px rgba(255,46,136,.55);}
  .stat #status.conn{color:var(--warn);text-shadow:0 0 8px rgba(255,176,0,.55);}

  /* ── panels ── */
  .panel{
    position:relative;
    border:1px solid var(--border);
    background:linear-gradient(180deg,rgba(13,26,36,.75),rgba(4,7,10,.75));
    padding:16px 12px 12px;
    backdrop-filter:blur(2px);
  }
  .panel::before{
    content:attr(data-title);
    position:absolute;top:-1px;left:12px;
    transform:translateY(-50%);
    background:var(--bg);
    padding:0 6px;
    font-size:10px;letter-spacing:2.5px;
    color:var(--accent);text-shadow:var(--glow-soft);
  }
  .panel::after{
    content:'';position:absolute;inset:0;pointer-events:none;
    background:
      linear-gradient(var(--accent),var(--accent)) left 0 top 0/8px 1px no-repeat,
      linear-gradient(var(--accent),var(--accent)) left 0 top 0/1px 8px no-repeat,
      linear-gradient(var(--accent),var(--accent)) right 0 bottom 0/8px 1px no-repeat,
      linear-gradient(var(--accent),var(--accent)) right 0 bottom 0/1px 8px no-repeat;
    opacity:.55;
  }

  /* ── sidebar ── */
  #sidebar{grid-area:side;display:flex;flex-direction:column;gap:10px;min-height:0;}
  #sidebar .panel:first-child{flex:0 0 auto;}
  #sidebar .panel:last-child{flex:1;min-height:0;display:flex;flex-direction:column;}
  #sidebar ul{list-style:none;margin:0;padding:0;overflow-y:auto;}
  #sidebar ul li{
    padding:5px 8px;
    cursor:pointer;
    color:var(--text);
    font-size:12px;
    border-left:2px solid transparent;
    transition:.12s;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  }
  #sidebar ul li:hover{background:rgba(0,217,255,.06);color:#fff;}
  #sidebar ul li.active{
    color:var(--accent);
    border-left-color:var(--accent);
    background:rgba(0,217,255,.08);
    text-shadow:var(--glow-soft);
  }
  #users li::before{
    content:'● ';
    color:var(--accent-2);
    text-shadow:0 0 6px rgba(1,255,112,.7);
    font-size:9px;
  }
  .newroom{display:flex;gap:6px;margin-top:8px;}
  .newroom input{
    flex:1;background:#030609;border:1px solid var(--border);
    color:var(--text);padding:5px 8px;font:inherit;font-size:11px;outline:none;
  }
  .newroom input:focus{border-color:var(--accent);box-shadow:var(--glow-soft);}
  .newroom button{
    background:transparent;border:1px solid var(--border);
    color:var(--accent);width:28px;cursor:pointer;font:inherit;
    transition:.15s;
  }
  .newroom button:hover{background:rgba(0,217,255,.1);border-color:var(--accent);}

  /* ── main ── */
  #main{grid-area:main;display:flex;flex-direction:column;gap:8px;min-height:0;}
  #chat{flex:1;min-height:0;display:flex;flex-direction:column;padding:16px 12px 12px;}
  #messages{flex:1;overflow-y:auto;min-height:0;padding-right:6px;}
  #messages::-webkit-scrollbar{width:6px;}
  #messages::-webkit-scrollbar-thumb{background:var(--border);}
  #messages::-webkit-scrollbar-thumb:hover{background:var(--accent);}

  .msg{
    display:flex;gap:8px;padding:2px 0;
    font-size:12.5px;line-height:1.55;
    word-break:break-word;
    animation:slide .18s ease-out;
  }
  @keyframes slide{from{opacity:0;transform:translateX(-4px);}to{opacity:1;transform:none;}}
  .msg .ts{color:var(--dim);flex:0 0 auto;}
  .msg .user{font-weight:700;flex:0 0 auto;text-shadow:0 0 6px currentColor;}
  .msg .sep{color:var(--dim);flex:0 0 auto;}
  .msg .text{color:#d5e8f0;white-space:pre-wrap;}
  .msg.sys{
    color:var(--dim);font-style:italic;font-size:11.5px;padding-left:2px;
  }
  .msg.sys .text{color:var(--warn);text-shadow:0 0 6px rgba(255,176,0,.35);}
  .msg.me .user{color:var(--accent-2)!important;}

  #typing{
    min-height:16px;padding:0 12px;
    font-size:11px;color:var(--dim);letter-spacing:1px;
  }
  #typing .dot{
    display:inline-block;width:4px;height:4px;margin-left:2px;border-radius:50%;
    background:var(--accent);box-shadow:var(--glow);
    animation:blink 1s infinite alternate;
  }
  #typing .dot:nth-child(2){animation-delay:.2s;}
  #typing .dot:nth-child(3){animation-delay:.4s;}
  @keyframes blink{from{opacity:.15;}to{opacity:1;}}

  /* ── composer ── */
  #composer{
    display:flex;align-items:center;gap:10px;
    border:1px solid var(--border);
    background:linear-gradient(180deg,var(--panel-hi),var(--panel));
    padding:8px 12px;
    position:relative;
  }
  #composer::after{
    content:'';position:absolute;left:0;right:0;top:-1px;height:1px;
    background:linear-gradient(90deg,transparent,var(--accent),transparent);
    opacity:.5;
  }
  #composer .prompt{
    color:var(--accent);text-shadow:var(--glow);
    font-weight:700;font-size:16px;
  }
  #input{
    flex:1;background:transparent;border:none;outline:none;
    color:#e3f5ff;font:inherit;font-size:13px;
    caret-color:var(--accent);
  }
  #input::placeholder{color:var(--dim);}
  #send{
    background:transparent;border:1px solid var(--border);
    color:var(--accent);padding:5px 14px;cursor:pointer;
    font:inherit;font-size:11px;letter-spacing:2px;
    transition:.15s;
  }
  #send:hover{background:rgba(0,217,255,.1);border-color:var(--accent);box-shadow:var(--glow-soft);}

  /* ── login ── */
  #login{
    position:fixed;inset:0;z-index:10000;
    display:flex;align-items:center;justify-content:center;
    background:rgba(2,5,8,.88);
    backdrop-filter:blur(4px);
  }
  #login.hidden{display:none;}
  .login-box{
    width:380px;padding:28px 24px 22px;
    text-align:center;
  }
  .login-box h1{
    margin:0 0 6px;font-size:18px;letter-spacing:4px;
    color:var(--accent);text-shadow:var(--glow);
  }
  .login-box h1 span{color:var(--pink);text-shadow:0 0 8px rgba(255,46,136,.55);}
  .login-box p{color:var(--dim);font-size:11px;letter-spacing:2px;margin:0 0 18px;}
  .login-box input{
    width:100%;background:#030609;border:1px solid var(--border);
    color:#e3f5ff;padding:10px 12px;font:inherit;font-size:13px;outline:none;
    text-align:center;letter-spacing:2px;
  }
  .login-box input:focus{border-color:var(--accent);box-shadow:var(--glow-soft);}
  .login-box button{
    margin-top:14px;width:100%;
    background:transparent;border:1px solid var(--accent);
    color:var(--accent);padding:10px;cursor:pointer;
    font:inherit;font-size:12px;letter-spacing:3px;
    transition:.15s;text-shadow:var(--glow);
  }
  .login-box button:hover{background:rgba(0,217,255,.1);box-shadow:var(--glow);}
  .login-box .hint{margin-top:10px;font-size:10px;color:var(--dim);letter-spacing:2px;}
</style>
</head>
<body>

<div id="app">
  <header id="topbar">
    <div class="brand">EDEX<span>://</span>MESSENGER</div>
    <div class="top-stats">
      <div class="stat"><span class="lbl">CH</span><span id="rname">#general</span></div>
      <div class="stat"><span class="lbl">USR</span><span id="ucount">0</span></div>
      <div class="stat"><span class="lbl">NET</span><span id="status" class="off">OFFLINE</span></div>
      <div class="stat"><span class="lbl">SYS</span><span id="clock">--:--:--</span></div>
    </div>
  </header>

  <aside id="sidebar">
    <section class="panel" data-title="CHANNELS">
      <ul id="channels"></ul>
      <div class="newroom">
        <input id="newroom-input" placeholder="new channel" maxlength="24">
        <button id="newroom-btn" title="создать">+</button>
      </div>
    </section>
    <section class="panel" data-title="OPERATORS">
      <ul id="users"></ul>
    </section>
  </aside>

  <main id="main">
    <section id="chat" class="panel" data-title="TRANSMISSION">
      <div id="messages"></div>
    </section>
    <div id="typing"></div>
    <div id="composer">
      <span class="prompt">›</span>
      <input id="input" autocomplete="off" spellcheck="false" placeholder="передача сообщения...">
      <button id="send">SEND</button>
    </div>
  </main>
</div>

<div id="login">
  <div class="login-box panel" data-title="AUTH">
    <h1>EDEX<span>://</span>MESSENGER</h1>
    <p>ВВЕДИТЕ ПОЗЫВНОЙ</p>
    <input id="login-name" maxlength="24" placeholder="nickname" autofocus>
    <button id="login-btn">CONNECT</button>
    <div class="hint">ENTER — ПОДКЛЮЧИТЬСЯ</div>
  </div>
</div>

<script>
(() => {
  const $ = (id) => document.getElementById(id);
  const messagesEl = $('messages');
  const channelsEl = $('channels');
  const usersEl = $('users');
  const inputEl = $('input');
  const typingEl = $('typing');
  const clockEl = $('clock');
  const statusEl = $('status');
  const ucountEl = $('ucount');
  const rnameEl = $('rname');
  const loginEl = $('login');

  let ws = null;
  let username = '';
  let currentRoom = 'general';
  let lastTypingSent = 0;
  const typingTimers = {};

  // ── clock ──
  setInterval(() => {
    const d = new Date();
    clockEl.textContent = d.toTimeString().slice(0, 8);
  }, 250);

  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 8);
  }

  function userColor(name) {
    const palette = ['#00d9ff','#01ff70','#ffb000','#ff2e88','#b388ff','#00ffa3','#ff7a45','#5ac8fa'];
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return palette[h % palette.length];
  }

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = cls;
  }

  // ── rendering ──
  function renderMessage(m) {
    const el = document.createElement('div');
    el.className = 'msg' + (m.user === username ? ' me' : '');
    const ts = document.createElement('span'); ts.className = 'ts'; ts.textContent = fmtTime(m.ts);
    const u = document.createElement('span'); u.className = 'user'; u.textContent = m.user;
    u.style.color = userColor(m.user);
    const sep = document.createElement('span'); sep.className = 'sep'; sep.textContent = '›';
    const t = document.createElement('span'); t.className = 'text'; t.textContent = m.text;
    el.append(ts, u, sep, t);
    return el;
  }

  function renderSystem(text, ts) {
    const el = document.createElement('div');
    el.className = 'msg sys';
    const tsEl = document.createElement('span'); tsEl.className = 'ts'; tsEl.textContent = fmtTime(ts || Date.now()/1000);
    const t = document.createElement('span'); t.className = 'text'; t.textContent = '* ' + text;
    el.append(tsEl, t);
    return el;
  }

  function appendMsg(node) {
    const atBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 60;
    messagesEl.appendChild(node);
    if (atBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function clearMessages() { messagesEl.innerHTML = ''; }

  // ── sidebar ──
  function renderChannels(rooms, current) {
    channelsEl.innerHTML = '';
    rooms.forEach(r => {
      const li = document.createElement('li');
      li.textContent = '# ' + r;
      if (r === current) li.classList.add('active');
      li.addEventListener('click', () => switchRoom(r));
      channelsEl.appendChild(li);
    });
  }

  function renderUsers(users) {
    usersEl.innerHTML = '';
    users.forEach(u => {
      const li = document.createElement('li');
      li.textContent = u + (u === username ? ' (you)' : '');
      li.style.color = userColor(u);
      usersEl.appendChild(li);
    });
    ucountEl.textContent = users.length;
  }

  // ── typing indicator ──
  function showTyping(user) {
    const el = document.createElement('div');
    el.className = 'typing-line';
    el.textContent = user + ' печатает';
    for (let i = 0; i < 3; i++) {
      const d = document.createElement('span'); d.className = 'dot'; el.appendChild(d);
    }
    typingEl.innerHTML = '';
    typingEl.appendChild(el);
    clearTimeout(typingTimers[user]);
    typingTimers[user] = setTimeout(() => {
      if (typingEl.firstChild === el) typingEl.innerHTML = '';
    }, 1800);
  }

  // ── websocket ──
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    setStatus('CONNECT...', 'conn');
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
      setStatus('ONLINE', 'on');
      ws.send(JSON.stringify({ type: 'join', username, room: currentRoom }));
    };

    ws.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }
      handle(data);
    };

    ws.onclose = () => {
      setStatus('OFFLINE', 'off');
      setTimeout(connect, 1500);
    };

    ws.onerror = () => setStatus('ERROR', 'off');
  }

  function handle(data) {
    switch (data.type) {
      case 'history':
        currentRoom = data.room || currentRoom;
        rnameEl.textContent = '#' + currentRoom;
        clearMessages();
        (data.messages || []).forEach(m => appendMsg(renderMessage(m)));
        messagesEl.scrollTop = messagesEl.scrollHeight;
        break;
      case 'message':
        appendMsg(renderMessage(data));
        if (typingEl.firstChild) typingEl.innerHTML = '';
        break;
      case 'system':
        appendMsg(renderSystem(data.text, data.ts));
        break;
      case 'users':
        renderUsers(data.users || []);
        break;
      case 'rooms':
        if (data.current) {
          currentRoom = data.current;
          rnameEl.textContent = '#' + currentRoom;
        }
        renderChannels(data.rooms || [], currentRoom);
        break;
      case 'typing':
        showTyping(data.user);
        break;
    }
  }

  // ── actions ──
  function sendMessage() {
    const text = inputEl.value.trim();
    if (!text || !ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({ type: 'message', text }));
    inputEl.value = '';
  }

  function switchRoom(room) {
    if (room === currentRoom || !ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({ type: 'switch_room', room }));
  }

  function createRoom() {
    const inp = $('newroom-input');
    const name = inp.value.trim().replace(/\s+/g, '-').toLowerCase();
    if (!name) return;
    ws.send(JSON.stringify({ type: 'create_room', room: name }));
    inp.value = '';
    setTimeout(() => switchRoom(name), 120);
  }

  // ── events ──
  $('send').addEventListener('click', sendMessage);

  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  inputEl.addEventListener('input', () => {
    const now = Date.now();
    if (now - lastTypingSent > 1200 && ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: 'typing' }));
      lastTypingSent = now;
    }
  });

  $('newroom-btn').addEventListener('click', createRoom);
  $('newroom-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); createRoom(); }
  });

  function doLogin() {
    const name = $('login-name').value.trim().slice(0, 24);
    if (!name) return;
    username = name;
    loginEl.classList.add('hidden');
    inputEl.focus();
    connect();
  }
  $('login-btn').addEventListener('click', doLogin);
  $('login-name').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doLogin();
  });
  $('login-name').focus();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
