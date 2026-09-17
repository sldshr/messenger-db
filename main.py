"""
SemiCursors — FastAPI + WebSocket сервер общих курсоров.
Всё в одном файле: HTTP API, WebSocket-хаб и встроенный фронтенд.
Запуск:  uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import secrets
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="SemiCursors")


# ============================================================
# МОДЕЛЬ КОМНАТЫ
# ============================================================
class Room:
    __slots__ = ("code", "type", "url", "clients")

    def __init__(self, code: str, room_type: str, url: str = "") -> None:
        self.code = code
        self.type = room_type          # "sandbox" | "site"
        self.url = url
        # sid -> {"ws": WebSocket, "name": str, "color": str}
        self.clients: Dict[str, dict] = {}

    def is_empty(self) -> bool:
        return not self.clients


rooms: Dict[str, Room] = {}

_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def gen_code() -> str:
    while True:
        code = "".join(secrets.choice(_CODE_CHARS) for _ in range(6))
        if code not in rooms:
            return code


# ============================================================
# HTTP API
# ============================================================
@app.post("/api/rooms")
async def create_room(payload: dict):
    rtype = str(payload.get("type", "sandbox"))
    url = str(payload.get("url", "")).strip()

    if rtype not in ("sandbox", "site"):
        return JSONResponse({"error": "invalid room type"}, status_code=400)
    if rtype == "site" and not url:
        return JSONResponse({"error": "url required for site room"}, status_code=400)

    code = gen_code()
    rooms[code] = Room(code, rtype, url)
    return {"code": code, "type": rtype, "url": url}


@app.get("/api/rooms/{code}")
async def get_room(code: str):
    room = rooms.get(code.upper())
    if not room:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "code": room.code,
        "type": room.type,
        "url": room.url,
        "count": len(room.clients),
    }


# ============================================================
# WEBSOCKET
# ============================================================
async def _send(ws: WebSocket, msg: dict) -> None:
    try:
        await ws.send_json(msg)
    except Exception:
        pass


async def _broadcast(room: Room, msg: dict, except_sid: Optional[str] = None) -> None:
    tasks = []
    for sid, c in list(room.clients.items()):
        if sid == except_sid:
            continue
        tasks.append(_send(c["ws"], msg))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@app.websocket("/ws/{code}")
async def ws_endpoint(websocket: WebSocket, code: str):
    code = code.upper()
    room = rooms.get(code)
    if room is None:
        await websocket.close(code=4004, reason="room not found")
        return

    await websocket.accept()

    # --- Первое сообщение: hello ---
    try:
        hello = await websocket.receive_json()
    except Exception:
        await websocket.close(code=4001, reason="hello required")
        return

    if not isinstance(hello, dict) or hello.get("t") != "hello":
        await websocket.close(code=4001, reason="hello required")
        return

    name = str(hello.get("name", "Гость"))[:16].strip() or "Гость"
    color = str(hello.get("color", "#ffffff"))[:9]
    sid = secrets.token_urlsafe(8)

    # --- Welcome ---
    peers = [
        {"id": s, "name": c["name"], "color": c["color"]}
        for s, c in room.clients.items()
    ]
    await _send(websocket, {
        "t": "welcome",
        "id": sid,
        "room": {"type": room.type, "url": room.url},
        "peers": peers,
    })

    room.clients[sid] = {"ws": websocket, "name": name, "color": color}

    await _broadcast(
        room,
        {"t": "join", "peer": {"id": sid, "name": name, "color": color}},
        except_sid=sid,
    )

    try:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            t = data.get("t")

            if t == "cursor":
                try:
                    x = float(data.get("x", 0))
                    y = float(data.get("y", 0))
                except (TypeError, ValueError):
                    continue
                if not (0 <= x <= 1 and 0 <= y <= 1):
                    continue
                msg = {"t": "cursor", "id": sid, "x": x, "y": y}

            elif t == "stroke-start":
                try:
                    msg = {
                        "t": "stroke-start",
                        "id": sid,
                        "sid": str(data.get("sid", ""))[:24],
                        "color": str(data.get("color", "#fff"))[:9],
                        "w": float(data.get("w", 3)),
                        "x": float(data.get("x", 0)),
                        "y": float(data.get("y", 0)),
                    }
                except (TypeError, ValueError):
                    continue

            elif t == "stroke-point":
                try:
                    msg = {
                        "t": "stroke-point",
                        "id": sid,
                        "sid": str(data.get("sid", ""))[:24],
                        "x": float(data.get("x", 0)),
                        "y": float(data.get("y", 0)),
                    }
                except (TypeError, ValueError):
                    continue

            elif t == "stroke-end":
                msg = {"t": "stroke-end", "id": sid, "sid": str(data.get("sid", ""))[:24]}

            elif t == "clear":
                msg = {"t": "clear", "id": sid}

            else:
                continue

            await _broadcast(room, msg, except_sid=sid)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[ws] error in room {code}: {exc}")
    finally:
        room.clients.pop(sid, None)
        await _broadcast(room, {"t": "bye", "id": sid})

        if room.is_empty():
            await asyncio.sleep(90)
            if room.is_empty() and rooms.get(code) is room:
                rooms.pop(code, None)


# ============================================================
# ВСТРОЕННЫЙ ФРОНТЕНД
# ============================================================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>SemiCursors — общие курсоры</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0b0e14; --panel:#121824; --panel2:#1a2231; --border:#26303f;
  --text:#e6edf6; --muted:#8b98ab; --accent:#7c5cff; --accent2:#22d3ee;
  --danger:#ef4444;
}
html,body{height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,sans-serif;
  background:var(--bg); color:var(--text); overflow:hidden;
  -webkit-font-smoothing:antialiased;
}
button{cursor:pointer;font-family:inherit;border:none;background:none;color:inherit}
input{font-family:inherit}
.hidden{display:none !important}

/* ---------- LOBBY ---------- */
#lobby{
  height:100vh; overflow-y:auto;
  display:flex; align-items:center; justify-content:center; padding:32px 16px;
  background:
    radial-gradient(900px 500px at 15% -10%, rgba(124,92,255,.18), transparent 60%),
    radial-gradient(800px 500px at 90% 110%, rgba(34,211,238,.14), transparent 60%),
    var(--bg);
}
.lobby-inner{width:100%;max-width:520px}
.logo{
  font-size:38px;font-weight:800;letter-spacing:-.03em;text-align:center;
  background:linear-gradient(100deg,#fff 10%,var(--accent2) 55%,var(--accent) 95%);
  -webkit-background-clip:text;background-clip:text;color:transparent;
}
.tagline{text-align:center;color:var(--muted);font-size:14px;margin:10px 0 26px;line-height:1.55}
.card{
  background:rgba(18,24,36,.85); border:1px solid var(--border);
  border-radius:16px; padding:18px; backdrop-filter:blur(12px);
  box-shadow:0 18px 50px rgba(0,0,0,.45);
}
.field{margin-bottom:14px}
.field:last-child{margin-bottom:0}
.field>label{display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:7px;text-transform:uppercase;letter-spacing:.06em}
input[type=text],input:not([type]){
  width:100%; padding:11px 13px; border-radius:10px;
  background:var(--panel2); border:1px solid var(--border); color:var(--text);
  font-size:14px; outline:none; transition:border-color .15s, box-shadow .15s;
}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,92,255,.18)}
input::placeholder{color:#5b6879}
.types{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.type{
  text-align:left;padding:13px;border-radius:12px;border:1.5px solid var(--border);
  background:var(--panel2); transition:.15s; display:flex;flex-direction:column;gap:3px;
}
.type:hover{border-color:#3a4759}
.type.active{border-color:var(--accent);background:rgba(124,92,255,.13);box-shadow:0 0 0 3px rgba(124,92,255,.12)}
.type .ico{font-size:20px;line-height:1}
.type .tt{font-weight:700;font-size:14px;margin-top:3px}
.type .td{font-size:11px;color:var(--muted);line-height:1.4}
.btn{
  padding:11px 18px;border-radius:10px;background:var(--panel2);
  border:1px solid var(--border);font-size:14px;font-weight:600;
  transition:.15s; white-space:nowrap;
}
.btn:hover{background:#222c3d;border-color:#3a4759}
.btn.primary{
  background:linear-gradient(100deg,var(--accent),#5b8cff); border-color:transparent;
  color:#fff; box-shadow:0 8px 24px rgba(124,92,255,.32);
}
.btn.primary:hover{filter:brightness(1.1)}
.btn.big{width:100%;padding:13px;font-size:15px}
.btn.danger{color:#ffb4b4;border-color:#4a2529;background:#241318}
.btn.danger:hover{background:#331a20;border-color:#6b3038}
.btn:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:10px}
.row input{flex:1}
.or{display:flex;align-items:center;gap:14px;color:#4a5666;font-size:12px;margin:16px 0}
.or::before,.or::after{content:'';flex:1;height:1px;background:var(--border)}
.hint{text-align:center;color:#5b6879;font-size:12px;margin-top:18px;line-height:1.6}

/* ---------- ROOM ---------- */
#room{height:100vh;display:flex;flex-direction:column}
header{
  display:flex;align-items:center;gap:10px;padding:9px 14px;flex-shrink:0;
  background:var(--panel);border-bottom:1px solid var(--border);flex-wrap:wrap;
}
.brand{font-weight:800;font-size:16px;letter-spacing:-.02em;white-space:nowrap}
.brand span{background:linear-gradient(100deg,var(--accent2),var(--accent));-webkit-background-clip:text;background-clip:text;color:transparent}
.badge{
  font-size:11px;font-weight:700;padding:5px 10px;border-radius:20px;
  background:rgba(124,92,255,.16);border:1px solid rgba(124,92,255,.35);color:#c3b5ff;
  white-space:nowrap;
}
.chip{
  font-size:12px;font-weight:600;padding:6px 11px;border-radius:8px;
  background:var(--panel2);border:1px solid var(--border);transition:.15s;white-space:nowrap;
}
.chip:hover{background:#222c3d;border-color:#3a4759}
.chip b{color:var(--accent2);letter-spacing:.12em;font-family:ui-monospace,monospace}
.spacer{flex:1}
.avatars{display:flex;align-items:center}
.av{
  width:26px;height:26px;border-radius:50%;margin-left:-7px;
  border:2px solid var(--panel);display:flex;align-items:center;justify-content:center;
  font-size:10px;font-weight:800;color:#0b0e14;
}
.av:first-child{margin-left:0}
.dot{width:8px;height:8px;border-radius:50%;background:#4ade80;box-shadow:0 0 8px #4ade80;flex-shrink:0}
.dot.off{background:#64748b;box-shadow:none}

/* ---------- STAGE ---------- */
#stage{position:relative;flex:1;overflow:hidden;background:#080b11;cursor:none}
#sandbox{
  position:absolute;inset:0;
  background-image:radial-gradient(circle,#1f2836 1px,transparent 1px);
  background-size:26px 26px;
}
#board{position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none}
#siteFrame{position:absolute;inset:0;width:100%;height:100%;border:0;background:#fff}
#capture{position:absolute;inset:0;z-index:4;background:transparent}
#capture.off{pointer-events:none;cursor:auto}
#cursorLayer{position:absolute;inset:0;z-index:6;pointer-events:none;overflow:hidden}

.cursor{
  position:absolute;left:0;top:0;pointer-events:none;will-change:transform;
  transition:transform .09s linear;
}
.cursor.self{transition:none}
.cursor svg{display:block;margin:-2px 0 0 -4px;filter:drop-shadow(0 2px 4px rgba(0,0,0,.5))}
.clabel{
  position:absolute;left:15px;top:15px;padding:2.5px 8px;border-radius:7px;
  font-size:11px;font-weight:700;color:#0b0e14;white-space:nowrap;line-height:1.45;
  box-shadow:0 3px 10px rgba(0,0,0,.35);
}

#tools{
  position:absolute;left:50%;bottom:18px;transform:translateX(-50%);z-index:8;
  display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:14px;
  background:rgba(18,24,36,.92);border:1px solid var(--border);
  backdrop-filter:blur(14px);box-shadow:0 14px 40px rgba(0,0,0,.5);cursor:default;
}
.sw{width:22px;height:22px;border-radius:50%;border:2px solid transparent;transition:.15s;padding:0}
.sw:hover{transform:scale(1.12)}
.sw.active{border-color:#fff;transform:scale(1.12)}
.tdiv{width:1px;height:20px;background:var(--border);margin:0 2px}
#tools .btn{padding:6px 11px;font-size:12px;border-radius:8px}

#toasts{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:999;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none;max-width:90vw}
.toast{
  padding:10px 16px;border-radius:10px;font-size:13px;font-weight:600;
  background:rgba(18,24,36,.96);border:1px solid var(--border);color:var(--text);
  box-shadow:0 10px 30px rgba(0,0,0,.5);animation:tin .25s ease;text-align:center;
}
.toast.err{border-color:#5b2a30;color:#ffb4b4}
@keyframes tin{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}

@media (max-width:640px){
  .types{grid-template-columns:1fr}
  .brand{font-size:14px}
  .chip{font-size:11px;padding:5px 8px}
  .logo{font-size:30px}
}
</style>
</head>
<body>

<!-- ================= LOBBY ================= -->
<div id="lobby">
  <div class="lobby-inner">
    <div class="logo">SemiCursors</div>
    <p class="tagline">Общие курсоры для тебя и друзей — в песочнице или поверх любого сайта</p>

    <div class="card">
      <div class="field">
        <label>Твоё имя</label>
        <input id="nameInput" maxlength="16" placeholder="Например, Алекс" autocomplete="off">
      </div>

      <div class="field">
        <label>Тип комнаты</label>
        <div class="types">
          <button class="type active" data-type="sandbox">
            <span class="ico">🎨</span>
            <span class="tt">Sandbox</span>
            <span class="td">Свободное поле: рисуй и показывай курсор</span>
          </button>
          <button class="type" data-type="site">
            <span class="ico">🌐</span>
            <span class="tt">Site</span>
            <span class="td">Курсоры поверх сайта по ссылке</span>
          </button>
        </div>
      </div>

      <div class="field hidden" id="urlField">
        <label>Адрес сайта</label>
        <input id="urlInput" placeholder="https://example.com" autocomplete="off">
      </div>

      <button class="btn primary big" id="createBtn">Создать комнату</button>
    </div>

    <div class="or"><span>или</span></div>

    <div class="card">
      <div class="field" style="margin-bottom:0">
        <label>Код приглашения</label>
        <div class="row">
          <input id="codeInput" maxlength="6" placeholder="ABC123" autocomplete="off"
                 style="text-transform:uppercase;letter-spacing:.22em;font-weight:800">
          <button class="btn primary" id="joinBtn">Войти</button>
        </div>
      </div>
    </div>

    <p class="hint">Открой ссылку с кодом на другом устройстве — курсоры синхронизируются в реальном времени.</p>
  </div>
</div>

<!-- ================= ROOM ================= -->
<div id="room" class="hidden">
  <header>
    <div class="brand">Semi<span>Cursors</span></div>
    <span class="dot off" id="statusDot" title="Соединение"></span>
    <span class="badge" id="typeBadge">Sandbox</span>
    <button class="chip" id="codeChip" title="Нажми, чтобы скопировать">Код: <b id="codeText">—</b></button>
    <button class="chip" id="copyLink">🔗 Пригласить</button>
    <div class="spacer"></div>
    <div class="avatars" id="avatars"></div>
    <button class="chip hidden" id="modeToggle">🖱 Взаимодействие</button>
    <button class="btn danger" id="leaveBtn">Выйти</button>
  </header>

  <main id="stage">
    <div id="sandbox">
      <canvas id="board"></canvas>
    </div>
    <iframe id="siteFrame" class="hidden" src="about:blank" referrerpolicy="no-referrer"
            allow="fullscreen; clipboard-read; clipboard-write"></iframe>
    <div id="capture" class="hidden"></div>
    <div id="cursorLayer"></div>

    <div id="tools" class="hidden">
      <button class="sw" data-color="#ff6b81" style="background:#ff6b81"></button>
      <button class="sw" data-color="#ffb347" style="background:#ffb347"></button>
      <button class="sw" data-color="#4ade80" style="background:#4ade80"></button>
      <button class="sw" data-color="#38bdf8" style="background:#38bdf8"></button>
      <button class="sw" data-color="#a78bfa" style="background:#a78bfa"></button>
      <button class="sw active" data-color="#ffffff" style="background:#ffffff"></button>
      <span class="tdiv"></span>
      <button class="btn" id="clearBtn">🗑 Очистить</button>
    </div>
  </main>
</div>

<div id="toasts"></div>

<script>
/* ==========================================================
   УТИЛИТЫ
========================================================== */
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const PALETTE = ['#ff6b81','#ffb347','#4ade80','#38bdf8','#a78bfa',
                 '#f472b6','#22d3ee','#facc15','#fb923c','#34d399'];
const randPick = a => a[Math.floor(Math.random() * a.length)];

function toast(msg, isErr, ms){
  const t = document.createElement('div');
  t.className = 'toast' + (isErr ? ' err' : '');
  t.textContent = msg;
  $('#toasts').appendChild(t);
  setTimeout(() => {
    t.style.transition = 'opacity .3s';
    t.style.opacity = '0';
    setTimeout(() => t.remove(), 320);
  }, ms || 2800);
}

/* ==========================================================
   СОСТОЯНИЕ
========================================================== */
let myName    = 'Гость';
let myColor   = randPick(PALETTE);
let roomType  = 'sandbox';
let roomUrl   = '';
let roomCode  = '';
let inRoom    = false;
let interacting = false;

let ws        = null;
let myId      = null;

const cursors   = new Map();
let selfCursor  = null;
let selfEntry   = null;

let stageW = 1, stageH = 1;
let localX = 0.5, localY = 0.5;

const stage       = $('#stage');
const cursorLayer = $('#cursorLayer');
const capture     = $('#capture');
const sandboxEl   = $('#sandbox');
const canvas      = $('#board');
const siteFrame   = $('#siteFrame');

/* ==========================================================
   КУРСОРЫ
========================================================== */
function makeCursorEl(name, color){
  const el = document.createElement('div');
  el.className = 'cursor';
  el.innerHTML =
    '<svg viewBox="0 0 24 24" width="22" height="22">' +
      '<path d="M4 2 L4 20 L9 15.2 L12.2 22 L15.2 20.8 L12 14.2 L19 14 Z" ' +
        'fill="' + color + '" stroke="rgba(0,0,0,.55)" stroke-width="1.4" stroke-linejoin="round"/>' +
    '</svg>' +
    '<span class="clabel" style="background:' + color + '">' + esc(name) + '</span>';
  return el;
}

function placeEntry(e){
  e.el.style.transform = 'translate3d(' + (e.x * stageW) + 'px,' + (e.y * stageH) + 'px,0)';
}

function measure(){
  const r = stage.getBoundingClientRect();
  stageW = r.width  || 1;
  stageH = r.height || 1;
}

function addCursor(id, name, color){
  let e = cursors.get(id);
  if (e){
    e.name = name; e.color = color;
    e.el.querySelector('.clabel').textContent = name;
    e.el.querySelector('.clabel').style.background = color;
    e.el.querySelector('path').setAttribute('fill', color);
    return e;
  }
  const el = makeCursorEl(name, color);
  cursorLayer.appendChild(el);
  e = { el, x: 0.5, y: 0.5, name, color };
  placeEntry(e);
  cursors.set(id, e);
  renderAvatars();
  toast(name + ' присоединился');
  return e;
}

function moveCursor(id, x, y){
  const e = cursors.get(id);
  if (!e) return;
  e.x = x; e.y = y;
  placeEntry(e);
}

function removeCursor(id){
  const e = cursors.get(id);
  if (!e) return;
  const nm = e.name;
  e.el.remove();
  cursors.delete(id);
  renderAvatars();
  toast(nm + ' вышел');
}

function renderAvatars(){
  const box = $('#avatars');
  const list = [{ name: myName, color: myColor, me: true }];
  cursors.forEach(c => list.push({ name: c.name, color: c.color }));
  box.innerHTML = list.slice(0, 7).map(p =>
    '<div class="av" style="background:' + p.color + '" title="' +
      esc(p.name) + (p.me ? ' (вы)' : '') + '">' +
      esc(p.name.trim().charAt(0).toUpperCase() || '?') +
    '</div>'
  ).join('');
}

function setStatus(on){
  $('#statusDot').className = 'dot' + (on ? '' : ' off');
}

/* ==========================================================
   HTTP API
========================================================== */
async function createRoomRemote(type, url){
  const r = await fetch('/api/rooms', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ type, url })
  });
  if (!r.ok){
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || 'create failed');
  }
  return r.json();
}

async function fetchRoomInfo(code){
  const r = await fetch('/api/rooms/' + encodeURIComponent(code));
  if (r.status === 404) return null;
  if (!r.ok) throw new Error('lookup failed');
  return r.json();
}

/* ==========================================================
   WEBSOCKET
========================================================== */
function wsSend(obj){
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try { ws.send(JSON.stringify(obj)); } catch(e){}
}

function connectWS(code){
  return new Promise((resolve, reject) => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = proto + '://' + location.host + '/ws/' + encodeURIComponent(code);
    const socket = new WebSocket(url);

    let welcomed = false;
    const timeout = setTimeout(() => {
      if (!welcomed){
        try { socket.close(); } catch(e){}
        reject(new Error('timeout'));
      }
    }, 10000);

    socket.onopen = () => {
      socket.send(JSON.stringify({ t: 'hello', name: myName, color: myColor }));
    };

    socket.onmessage = ev => {
      let data;
      try { data = JSON.parse(ev.data); } catch(e){ return; }

      if (!welcomed && data.t === 'welcome'){
        welcomed = true;
        clearTimeout(timeout);
        ws = socket;
        myId = data.id;
        if (data.room){
          roomType = data.room.type || 'sandbox';
          roomUrl  = data.room.url  || '';
        }
        (data.peers || []).forEach(p => addCursor(p.id, p.name, p.color));
        setStatus(true);
        resolve();
        return;
      }

      handleMessage(data);
    };

    socket.onerror = () => {
      if (!welcomed){ clearTimeout(timeout); reject(new Error('error')); }
    };

    socket.onclose = () => {
      if (!welcomed){
        clearTimeout(timeout);
        reject(new Error('closed'));
        return;
      }
      if (ws === socket){
        setStatus(false);
        if (inRoom){
          toast('Соединение с сервером потеряно', true);
          leaveRoom();
        }
      }
    };
  });
}

/* ==========================================================
   СООБЩЕНИЯ ОТ СЕРВЕРА
========================================================== */
const remoteStrokes = new Map();

function handleMessage(data){
  switch(data.t){
    case 'join':
      addCursor(data.peer.id, data.peer.name, data.peer.color);
      break;

    case 'bye':
      removeCursor(data.id);
      break;

    case 'cursor':
      moveCursor(data.id, data.x, data.y);
      break;

    case 'stroke-start': {
      const s = {
        key: data.id + ':' + data.sid,
        pts: [[data.x, data.y]],
        color: data.color,
        w: data.w
      };
      remoteStrokes.set(s.key, s);
      strokes.push(s);
      redraw();
      break;
    }

    case 'stroke-point': {
      const key = data.id + ':' + data.sid;
      const s = remoteStrokes.get(key);
      if (s){
        s.pts.push([data.x, data.y]);
        redraw();
      }
      break;
    }

    case 'stroke-end': {
      remoteStrokes.delete(data.id + ':' + data.sid);
      break;
    }

    case 'clear':
      strokes.length = 0;
      remoteStrokes.clear();
      redraw();
      break;
  }
}

/* ==========================================================
   ОТПРАВКА КУРСОРА (троттлинг 40 мс)
========================================================== */
let lastSent = 0, pendingPos = null, pendingTimer = null;

function pushCursor(x, y){
  if (!inRoom || interacting) return;
  const now = performance.now();
  if (now - lastSent >= 40){
    lastSent = now;
    wsSend({ t: 'cursor', x, y });
  } else {
    pendingPos = { x, y };
    if (!pendingTimer){
      pendingTimer = setTimeout(() => {
        pendingTimer = null;
        if (pendingPos){
          lastSent = performance.now();
          wsSend({ t: 'cursor', x: pendingPos.x, y: pendingPos.y });
          pendingPos = null;
        }
      }, 40);
    }
  }
}

/* ==========================================================
   SANDBOX — РИСОВАНИЕ
========================================================== */
const strokes = [];
let curStroke = null;
let drawColor = '#ffffff';

function resizeCanvas(){
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width  = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  redraw();
}

function redraw(){
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  strokes.forEach(s => paintStroke(ctx, s));
}

function paintStroke(ctx, s){
  const W = canvas.clientWidth, H = canvas.clientHeight;
  if (!s.pts || !s.pts.length) return;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.strokeStyle = s.color;
  ctx.fillStyle = s.color;
  ctx.lineWidth = s.w;
  if (s.pts.length === 1){
    ctx.beginPath();
    ctx.arc(s.pts[0][0] * W, s.pts[0][1] * H, s.w / 2, 0, Math.PI * 2);
    ctx.fill();
    return;
  }
  ctx.beginPath();
  ctx.moveTo(s.pts[0][0] * W, s.pts[0][1] * H);
  for (let i = 1; i < s.pts.length; i++){
    ctx.lineTo(s.pts[i][0] * W, s.pts[i][1] * H);
  }
  ctx.stroke();
}

/* ==========================================================
   СОБЫТИЯ СЦЕНЫ
========================================================== */
function stagePoint(e){
  const r = stage.getBoundingClientRect();
  return {
    x: (e.clientX - r.left) / r.width,
    y: (e.clientY - r.top)  / r.height
  };
}

stage.addEventListener('pointermove', e => {
  if (!inRoom) return;
  const p = stagePoint(e);
  if (p.x < 0 || p.x > 1 || p.y < 0 || p.y > 1) return;

  localX = p.x; localY = p.y;

  if (selfEntry && !interacting){
    selfEntry.x = localX; selfEntry.y = localY;
    placeEntry(selfEntry);
  }
  pushCursor(localX, localY);

  if (roomType === 'sandbox' && curStroke){
    curStroke.pts.push([p.x, p.y]);
    wsSend({
      t: 'stroke-point',
      sid: curStroke.sid,
      x: p.x, y: p.y
    });
    redraw();
  }
}, { passive: true });

stage.addEventListener('pointerleave', () => {
  if (selfCursor) selfCursor.style.opacity = '0';
});
stage.addEventListener('pointerenter', () => {
  if (selfCursor) selfCursor.style.opacity = '1';
});

canvas.addEventListener('pointerdown', e => {
  if (roomType !== 'sandbox') return;
  canvas.setPointerCapture(e.pointerId);
  const p = stagePoint(e);
  const sid = Math.random().toString(36).slice(2, 10);
  curStroke = { sid, pts: [[p.x, p.y]], color: drawColor, w: 3 };
  strokes.push(curStroke);
  wsSend({
    t: 'stroke-start',
    sid,
    color: drawColor,
    w: 3,
    x: p.x, y: p.y
  });
  redraw();
});

function endStroke(){
  if (!curStroke) return;
  wsSend({ t: 'stroke-end', sid: curStroke.sid });
  curStroke = null;
}
canvas.addEventListener('pointerup', endStroke);
canvas.addEventListener('pointercancel', endStroke);

/* ==========================================================
   UI: ЛОББИ
========================================================== */
let chosenType = 'sandbox';

document.querySelectorAll('.type').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.type').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    chosenType = btn.dataset.type;
    $('#urlField').classList.toggle('hidden', chosenType !== 'site');
  });
});
$('#urlField').classList.add('hidden');

$('#createBtn').addEventListener('click', async () => {
  const name = $('#nameInput').value.trim();
  myName  = (name || 'Гость').slice(0, 16);
  myColor = randPick(PALETTE);

  roomType = chosenType;
  roomUrl  = '';

  if (roomType === 'site'){
    let u = $('#urlInput').value.trim();
    if (!u){ toast('Укажи адрес сайта', true); return; }
    if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
    roomUrl = u;
  }

  const btn = $('#createBtn');
  btn.disabled = true; btn.textContent = 'Создаю комнату…';

  try {
    const r = await createRoomRemote(roomType, roomUrl);
    roomCode = r.code;
    await connectWS(roomCode);
    enterRoom();
    toast('Комната создана! Код: ' + roomCode);
  } catch (e){
    console.error(e);
    toast('Не удалось создать комнату', true);
  } finally {
    btn.disabled = false; btn.textContent = 'Создать комнату';
  }
});

$('#joinBtn').addEventListener('click', doJoin);
$('#codeInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') doJoin();
});

async function doJoin(){
  const name = $('#nameInput').value.trim();
  myName  = (name || 'Гость').slice(0, 16);
  myColor = randPick(PALETTE);

  const code = $('#codeInput').value.trim().toUpperCase();
  if (code.length < 4){ toast('Введи код комнаты', true); return; }

  const btn = $('#joinBtn');
  btn.disabled = true; btn.textContent = 'Подключаюсь…';

  try {
    const info = await fetchRoomInfo(code);
    if (!info){
      toast('Комната не найдена', true);
      return;
    }
    roomCode = code;
    roomType = info.type;
    roomUrl  = info.url;

    await connectWS(code);
    enterRoom();
    toast('Ты в комнате ' + code);
  } catch (e){
    console.error(e);
    toast('Ошибка подключения к серверу', true);
  } finally {
    btn.disabled = false; btn.textContent = 'Войти';
  }
}

/* ==========================================================
   UI: КОМНАТА
========================================================== */
function enterRoom(){
  if (inRoom) return;
  inRoom = true;

  $('#lobby').classList.add('hidden');
  $('#room').classList.remove('hidden');

  $('#codeText').textContent = roomCode;
  $('#typeBadge').textContent = roomType === 'sandbox' ? '🎨 Sandbox' : '🌐 Site';

  measure();

  if (roomType === 'sandbox'){
    sandboxEl.classList.remove('hidden');
    siteFrame.classList.add('hidden');
    capture.classList.add('hidden');
    $('#modeToggle').classList.add('hidden');
    $('#tools').classList.remove('hidden');
    requestAnimationFrame(() => resizeCanvas());
  } else {
    sandboxEl.classList.add('hidden');
    siteFrame.classList.remove('hidden');
    capture.classList.remove('hidden');
    capture.classList.remove('off');
    interacting = false;
    $('#modeToggle').classList.remove('hidden');
    $('#modeToggle').textContent = '🖱 Взаимодействие';
    $('#tools').classList.add('hidden');
    if (roomUrl){
      try { siteFrame.src = roomUrl; } catch(e){ console.warn(e); }
    }
  }

  selfCursor = makeCursorEl(myName + ' (вы)', myColor);
  selfCursor.classList.add('self');
  cursorLayer.appendChild(selfCursor);
  selfEntry = { el: selfCursor, x: localX, y: localY };
  placeEntry(selfEntry);

  renderAvatars();
}

function leaveRoom(){
  inRoom = false;

  try { if (ws) ws.close(); } catch(e){}
  ws = null;
  myId = null;

  cursors.forEach(c => c.el.remove());
  cursors.clear();

  if (selfCursor){ selfCursor.remove(); selfCursor = null; selfEntry = null; }

  strokes.length = 0;
  remoteStrokes.clear();
  curStroke = null;

  try { siteFrame.src = 'about:blank'; } catch(e){}

  $('#room').classList.add('hidden');
  $('#lobby').classList.remove('hidden');
  setStatus(false);
}

$('#leaveBtn').addEventListener('click', leaveRoom);

$('#codeChip').addEventListener('click', () => copy(roomCode, 'Код скопирован'));

$('#copyLink').addEventListener('click', () => {
  const url = location.origin + location.pathname + '?room=' + roomCode;
  copy(url, 'Ссылка-приглашение скопирована');
});

function copy(text, msg){
  const done = () => toast(msg);
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(done).catch(() => fallback());
  } else {
    fallback();
  }
  function fallback(){
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch(e){ toast(text); }
    ta.remove();
  }
}

$('#modeToggle').addEventListener('click', () => {
  interacting = !interacting;
  capture.classList.toggle('off', interacting);
  $('#modeToggle').textContent = interacting
    ? '🎯 Режим курсоров'
    : '🖱 Взаимодействие';
  if (selfCursor) selfCursor.style.opacity = interacting ? '0' : '1';
  toast(interacting
    ? 'Клики идут на сайт, твой курсор скрыт'
    : 'Курсоры снова видны');
});

document.querySelectorAll('.sw').forEach(sw => {
  sw.addEventListener('click', () => {
    document.querySelectorAll('.sw').forEach(s => s.classList.remove('active'));
    sw.classList.add('active');
    drawColor = sw.dataset.color;
  });
});

$('#clearBtn').addEventListener('click', () => {
  strokes.length = 0;
  remoteStrokes.clear();
  redraw();
  wsSend({ t: 'clear' });
  toast('Поле очищено');
});

/* ==========================================================
   РЕСАЙЗ
========================================================== */
let rt = null;
window.addEventListener('resize', () => {
  clearTimeout(rt);
  rt = setTimeout(() => {
    measure();
    cursors.forEach(placeEntry);
    if (selfEntry) placeEntry(selfEntry);
    if (roomType === 'sandbox' && inRoom) resizeCanvas();
  }, 90);
});

/* ==========================================================
   СТАРТ
========================================================== */
(function init(){
  const params = new URLSearchParams(location.search);
  const r = params.get('room');
  if (r){
    $('#codeInput').value = r.toUpperCase().slice(0, 6);
    $('#nameInput').focus();
    setTimeout(() => toast('Введи имя и нажми «Войти»'), 400);
  } else {
    $('#nameInput').focus();
  }
  measure();
})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)
