"""
Nexus Chat — быстрый мессенджер на одном файле.
Запуск:  python main.py
Открой:  http://localhost:8000

Протокол (бинарный, минимальный оверхед):
  [ type:uint8 ][ length:uint32 BE ][ payload: JSON UTF-8 ]
  type: 1=AUTH 2=AUTH_OK 3=MSG 4=HISTORY 5=USERS 6=PING 7=PONG 8=ERROR
"""
import asyncio
import json
import struct
import time
import uuid
from collections import deque
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ======================= ПРОТОКОЛ =======================
T_AUTH, T_AUTH_OK, T_MSG, T_HISTORY, T_USERS, T_PING, T_PONG, T_ERROR = range(1, 9)
_HDR = struct.Struct(">BI")  # type (1B) + length (4B, big-endian)


def pack(mtype: int, obj: Any) -> bytes:
    p = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _HDR.pack(mtype, len(p)) + p


def unpack(data: bytes):
    mtype, ln = _HDR.unpack_from(data, 0)
    off = _HDR.size
    return mtype, json.loads(data[off: off + ln].decode("utf-8"))


# ======================= СОСТОЯНИЕ =======================
class Client:
    __slots__ = ("ws", "id", "name", "send_lock")

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.id = uuid.uuid4().hex[:8]
        self.name: Optional[str] = None
        self.send_lock = asyncio.Lock()

    async def send(self, mtype: int, obj: Any):
        async with self.send_lock:
            try:
                await self.ws.send_bytes(pack(mtype, obj))
            except Exception:
                pass


clients: dict[str, Client] = {}       # id   -> Client
by_name: dict[str, Client] = {}       # name -> Client
history: deque = deque(maxlen=500)    # последние сообщения (ring buffer)
hist_lock = asyncio.Lock()


def user_list() -> list[str]:
    return sorted(by_name.keys())


async def broadcast(mtype: int, obj: Any, exclude: Optional[Client] = None):
    if not clients:
        return
    frame = pack(mtype, obj)  # кодируем ОДИН раз для всех

    async def _one(c: Client):
        async with c.send_lock:
            try:
                await c.ws.send_bytes(frame)
            except Exception:
                pass

    await asyncio.gather(*(_one(c) for c in list(clients.values()) if c is not exclude))


# ======================= ОБРАБОТКА СООБЩЕНИЙ =======================
async def handle_msg(c: Client, obj: dict):
    text = (obj.get("text") or "").strip()[:2000]
    if not text:
        return
    to = obj.get("to") or "*"
    msg = {
        "from": c.name,
        "to": to,
        "text": text,
        "ts": int(time.time() * 1000),
        "cid": obj.get("cid"),
    }
    async with hist_lock:
        history.append(msg)

    if to == "*":
        await broadcast(T_MSG, msg)
    else:
        target = by_name.get(to)
        if target is None:
            await c.send(T_ERROR, {"msg": f"Пользователь «{to}» не в сети"})
            return
        await target.send(T_MSG, msg)
        if target is not c:
            await c.send(T_MSG, msg)


# ======================= HTTP =======================
app = FastAPI()


@app.get("/")
async def index():
    return HTMLResponse(HTML_PAGE)


@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    c = Client(ws)

    # --- рукопожатие ---
    try:
        raw = await asyncio.wait_for(ws.receive_bytes(), timeout=15)
        mtype, obj = unpack(raw)
    except Exception:
        await ws.close()
        return

    if mtype != T_AUTH:
        await c.send(T_ERROR, {"msg": "AUTH required"})
        await ws.close()
        return

    name = (obj.get("name") or "").strip()[:24]
    if not name:
        await c.send(T_ERROR, {"msg": "Пустое имя"})
        await ws.close()
        return
    if name in by_name:
        await c.send(T_ERROR, {"msg": "Имя уже занято"})
        await ws.close()
        return

    c.name = name
    clients[c.id] = c
    by_name[name] = c

    async with hist_lock:
        hist = list(history)

    await c.send(T_AUTH_OK, {
        "id": c.id, "name": name,
        "history": hist,
        "users": user_list(),
    })
    await broadcast(T_USERS, {"users": user_list()})

    try:
        while True:
            raw = await ws.receive_bytes()
            try:
                mtype, obj = unpack(raw)
            except Exception:
                continue

            if mtype == T_MSG:
                asyncio.create_task(handle_msg(c, obj))
            elif mtype == T_PING:
                await c.send(T_PONG, {"t": obj.get("t", 0)})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        clients.pop(c.id, None)
        by_name.pop(c.name, None)
        await broadcast(T_USERS, {"users": user_list()})


# ======================= HTML =======================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#0e1116">
<title>Nexus Chat</title>
<style>
:root{
  --bg:#0e1116; --bg2:#161b22; --bg3:#1f2630;
  --fg:#e6edf3; --mut:#8b949e;
  --acc:#3b82f6; --acc2:#2563eb;
  --ok:#22c55e; --err:#ef4444;
  --bord:#30363d;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;overscroll-behavior:none}
body{
  font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:var(--bg);color:var(--fg);
}
#app{display:flex;height:100dvh;width:100%}

/* ---------- сайдбар ---------- */
#side{
  width:270px;flex-shrink:0;background:var(--bg2);
  border-right:1px solid var(--bord);
  display:flex;flex-direction:column;
}
.side-head{
  padding:14px 16px;border-bottom:1px solid var(--bord);
}
.side-head h1{font-size:15px;font-weight:700;letter-spacing:.3px}
.side-head .me{font-size:12px;color:var(--mut);margin-top:2px}
#users{flex:1;overflow-y:auto;padding:6px}
.user{
  display:flex;align-items:center;gap:10px;padding:10px 12px;
  border-radius:9px;cursor:pointer;user-select:none;
  transition:background .12s;
}
.user:hover{background:var(--bg3)}
.user.active{background:var(--acc);color:#fff}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok);flex-shrink:0}
.user.active .dot{background:#fff}
.lbl{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{
  background:var(--err);color:#fff;font-size:11px;font-weight:700;
  padding:1px 7px;border-radius:10px;line-height:1.4;
}

/* ---------- основная часть ---------- */
#main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg)}
#head{
  padding:10px 14px;border-bottom:1px solid var(--bord);
  display:flex;align-items:center;gap:10px;background:var(--bg2);
  padding-top:calc(10px + env(safe-area-inset-top));
}
#menuBtn{
  display:none;background:none;border:none;color:var(--fg);
  font-size:22px;cursor:pointer;padding:4px 8px;line-height:1;
}
#titleTxt{font-weight:600}
#sub{font-size:12px;color:var(--mut);margin-left:8px;font-weight:400}

#msgs{
  flex:1;overflow-y:auto;padding:14px 16px;
  display:flex;flex-direction:column;gap:6px;
  scroll-behavior:smooth;
}
.m{
  max-width:78%;padding:7px 12px;border-radius:14px;
  background:var(--bg3);align-self:flex-start;
  word-wrap:break-word;overflow-wrap:anywhere;
  animation:pop .12s ease-out;
}
@keyframes pop{from{opacity:.3;transform:translateY(3px)}to{opacity:1;transform:none}}
.m.me{align-self:flex-end;background:var(--acc)}
.m.dm:not(.me){background:linear-gradient(135deg,#5b21b6,#4c1d95)}
.m.me.dm{background:linear-gradient(135deg,#8b5cf6,#7c3aed)}
.m .meta{font-size:11px;color:var(--mut);margin-bottom:2px}
.m.me .meta{color:rgba(255,255,255,.8)}
.m .txt{white-space:pre-wrap}

#bar{
  padding:9px 12px;border-top:1px solid var(--bord);
  display:flex;gap:8px;background:var(--bg2);
  padding-bottom:calc(9px + env(safe-area-inset-bottom));
}
#inp{
  flex:1;background:var(--bg3);border:1px solid var(--bord);
  color:var(--fg);padding:10px 14px;border-radius:22px;
  outline:none;font:inherit;min-width:0;
}
#inp:focus{border-color:var(--acc)}
#send{
  background:var(--acc);color:#fff;border:none;padding:0 18px;
  border-radius:22px;cursor:pointer;font-weight:600;font:inherit;
  transition:background .12s;
}
#send:active{background:var(--acc2)}

/* ---------- логин ---------- */
#login{
  position:fixed;inset:0;background:var(--bg);z-index:100;
  display:flex;align-items:center;justify-content:center;padding:20px;
}
#login .box{
  width:100%;max-width:360px;background:var(--bg2);
  border:1px solid var(--bord);border-radius:16px;padding:24px;
}
#login h2{margin-bottom:6px;font-size:20px}
#login .hint{color:var(--mut);font-size:13px;margin-bottom:16px}
#login input{
  width:100%;background:var(--bg3);border:1px solid var(--bord);
  color:var(--fg);padding:12px 14px;border-radius:10px;
  outline:none;font:inherit;margin-bottom:10px;
}
#login input:focus{border-color:var(--acc)}
#login button{
  width:100%;background:var(--acc);color:#fff;border:none;
  padding:12px;border-radius:10px;font-weight:600;cursor:pointer;font:inherit;
}
#login button:disabled{opacity:.5;cursor:default}
#loginErr{color:var(--err);font-size:13px;margin-top:8px;min-height:17px}
#loginStatus{color:var(--mut);font-size:12px;margin-top:4px}

#backdrop{
  position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40;
  opacity:0;pointer-events:none;transition:opacity .18s;
}
#backdrop.show{opacity:1;pointer-events:auto}

@media (max-width:720px){
  #side{
    position:fixed;left:0;top:0;bottom:0;z-index:50;
    transform:translateX(-100%);transition:transform .2s;
    width:min(80vw,300px);
    padding-top:env(safe-area-inset-top);
  }
  #side.open{transform:translateX(0)}
  #menuBtn{display:block}
  .m{max-width:88%}
}
</style>
</head>
<body>

<!-- логин -->
<div id="login">
  <div class="box">
    <h2>Nexus Chat</h2>
    <div class="hint">Быстрый мессенджер · бинарный протокол</div>
    <input id="nameInput" maxlength="24" placeholder="Ваше имя" autocomplete="off" autocapitalize="off" spellcheck="false">
    <button id="joinBtn">Войти</button>
    <div id="loginErr"></div>
    <div id="loginStatus"></div>
  </div>
</div>

<div id="backdrop"></div>

<div id="app" style="display:none">
  <aside id="side">
    <div class="side-head">
      <h1>Nexus</h1>
      <div class="me" id="myName"></div>
    </div>
    <div id="users"></div>
  </aside>
  <div id="main">
    <div id="head">
      <button id="menuBtn" aria-label="Меню">☰</button>
      <div><span id="titleTxt">Общий чат</span><span id="sub"></span></div>
    </div>
    <div id="msgs"></div>
    <div id="bar">
      <input id="inp" placeholder="Сообщение…" autocomplete="off" autocapitalize="sentences">
      <button id="send" aria-label="Отправить">➤</button>
    </div>
  </div>
</div>

<script>
(() => {
"use strict";

// ====== protocol ======
const T = {AUTH:1, AUTH_OK:2, MSG:3, HISTORY:4, USERS:5, PING:6, PONG:7, ERROR:8};
const _enc = new TextEncoder();
const _dec = new TextDecoder();

function pack(type, obj){
  const p = _enc.encode(JSON.stringify(obj));
  const buf = new ArrayBuffer(5 + p.length);
  const dv = new DataView(buf);
  dv.setUint8(0, type);
  dv.setUint32(1, p.length, false);          // big-endian
  new Uint8Array(buf, 5).set(p);
  return buf;
}
function unpack(buf){
  const dv = new DataView(buf);
  const type = dv.getUint8(0);
  const len  = dv.getUint32(1, false);
  const obj  = JSON.parse(_dec.decode(new Uint8Array(buf, 5, len)));
  return [type, obj];
}

// ====== state ======
const $ = id => document.getElementById(id);
let ws = null;
let myName = "";
let users = [];
let currentTo = "*";
const chats = { "*": [] };       // key -> [messages]
const unread = {};               // key -> count
let heartbeatTimer = null;
let reconnectAttempts = 0;

// ====== UI helpers ======
function setStatus(t){ $("loginStatus").textContent = t; }

function renderUsers(){
  const box = $("users");
  box.innerHTML = "";
  box.appendChild(userRow("*", "Общий чат"));
  for (const u of users){
    if (u === myName) continue;
    box.appendChild(userRow(u, u));
  }
}

function userRow(key, label){
  const d = document.createElement("div");
  d.className = "user" + (key === currentTo ? " active" : "");
  const dot = document.createElement("span");
  dot.className = "dot";
  const lbl = document.createElement("span");
  lbl.className = "lbl";
  lbl.textContent = label;
  d.append(dot, lbl);
  const b = unread[key] || 0;
  if (b > 0){
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = b > 99 ? "99+" : b;
    d.appendChild(badge);
  }
  d.onclick = () => selectChat(key);
  return d;
}

function selectChat(key){
  currentTo = key;
  unread[key] = 0;
  $("titleTxt").textContent = key === "*" ? "Общий чат" : key;
  $("sub").textContent = key === "*" ? "" : "(личные сообщения)";
  $("inp").placeholder = key === "*" ? "Сообщение в общий чат…" : "Личное сообщение…";
  renderUsers();
  renderMsgs();
  closeSidebar();
  setTimeout(() => $("inp").focus(), 30);
}

function renderMsgs(){
  const box = $("msgs");
  box.innerHTML = "";
  const arr = chats[currentTo] || [];
  for (const m of arr) appendMsg(m, false);
  box.scrollTop = box.scrollHeight;
}

function appendMsg(m, scroll = true){
  const box = $("msgs");
  const mine = m.from === myName;
  const dm = m.to !== "*";
  const el = document.createElement("div");
  el.className = "m" + (mine ? " me" : "") + (dm ? " dm" : "");

  const meta = document.createElement("div");
  meta.className = "meta";
  const t = new Date(m.ts).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  meta.textContent = (mine ? "Вы" : m.from) + " · " + t;

  const txt = document.createElement("div");
  txt.className = "txt";
  txt.textContent = m.text;

  el.append(meta, txt);
  box.appendChild(el);
  if (scroll) box.scrollTop = box.scrollHeight;
}

function openSidebar(){ $("side").classList.add("open"); $("backdrop").classList.add("show"); }
function closeSidebar(){ $("side").classList.remove("open"); $("backdrop").classList.remove("show"); }

// ====== messages ======
function keyFor(m){
  if (m.to === "*") return "*";
  return m.from === myName ? m.to : m.from;
}

function onMessage(m){
  const key = keyFor(m);
  if (!chats[key]) chats[key] = [];
  chats[key].push(m);
  if (key === currentTo){
    appendMsg(m);
  } else {
    unread[key] = (unread[key] || 0) + 1;
    renderUsers();
  }
}

function handleFrame(type, obj){
  switch(type){
    case T.MSG:      onMessage(obj); break;
    case T.USERS:    users = obj.users || []; renderUsers(); break;
    case T.PING:     if (ws && ws.readyState === 1) ws.send(pack(T.PONG, {})); break;
    case T.PONG:     /* ok */ break;
    case T.ERROR:    console.warn("server error:", obj.msg); break;
  }
}

// ====== connection ======
function connect(name){
  return new Promise((resolve, reject) => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.binaryType = "arraybuffer";

    let settled = false;
    const timeout = setTimeout(() => {
      if (!settled){ settled = true; try{ ws.close(); }catch(e){}; reject(new Error("Таймаут подключения")); }
    }, 8000);

    ws.onopen = () => ws.send(pack(T.AUTH, {name}));

    ws.onmessage = ev => {
      let type, obj;
      try { [type, obj] = unpack(ev.data); } catch(e){ return; }

      if (!settled){
        if (type === T.AUTH_OK){
          settled = true; clearTimeout(timeout);
          resolve(obj); return;
        }
        if (type === T.ERROR){
          settled = true; clearTimeout(timeout);
          try{ ws.close(); }catch(e){}
          reject(new Error(obj.msg || "Ошибка авторизации")); return;
        }
      }
      handleFrame(type, obj);
    };

    ws.onclose = () => {
      if (!settled){
        settled = true; clearTimeout(timeout);
        reject(new Error("Соединение закрыто"));
      } else {
        onDisconnect();
      }
    };
    ws.onerror = () => {
      if (!settled){
        settled = true; clearTimeout(timeout);
        reject(new Error("Ошибка соединения"));
      }
    };
  });
}

function onDisconnect(){
  clearInterval(heartbeatTimer);
  if (reconnectAttempts < 5){
    reconnectAttempts++;
    setStatus("Переподключение…");
    setTimeout(() => {
      tryReconnect();
    }, 1000 * reconnectAttempts);
  }
}

async function tryReconnect(){
  try {
    const ok = await connect(myName);
    reconnectAttempts = 0;
    setStatus("");
    users = ok.users || [];
    renderUsers();
    startHeartbeat();
  } catch(e){
    onDisconnect();
  }
}

function startHeartbeat(){
  clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => {
    if (ws && ws.readyState === 1) ws.send(pack(T.PING, {t: Date.now()}));
  }, 25000);
}

// ====== send ======
function sendMsg(){
  const text = $("inp").value.trim();
  if (!text) return;
  if (!ws || ws.readyState !== 1) return;
  const cid = Math.random().toString(36).slice(2, 10);
  ws.send(pack(T.MSG, {to: currentTo, text, cid}));
  $("inp").value = "";
  $("inp").focus();
}

// ====== login ======
async function doJoin(){
  const name = $("nameInput").value.trim();
  if (!name){ $("loginErr").textContent = "Введите имя"; return; }
  $("joinBtn").disabled = true;
  $("loginErr").textContent = "";
  setStatus("Подключение…");
  try {
    const ok = await connect(name);
    myName = name;
    users = ok.users || [];
    // seed local history
    for (const m of (ok.history || [])){
      if (m.to === "*" || m.from === myName || m.to === myName){
        const k = keyFor(m);
        if (!chats[k]) chats[k] = [];
        chats[k].push(m);
      }
    }
    $("myName").textContent = "@" + name;
    $("login").style.display = "none";
    $("app").style.display = "flex";
    selectChat("*");
    renderUsers();
    startHeartbeat();
    reconnectAttempts = 0;
  } catch(e){
    $("loginErr").textContent = e.message || "Ошибка";
    $("joinBtn").disabled = false;
    setStatus("");
  }
}

// ====== bind ======
$("joinBtn").onclick = doJoin;
$("nameInput").addEventListener("keydown", e => { if (e.key === "Enter") doJoin(); });
$("send").onclick = sendMsg;
$("inp").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); sendMsg(); }
});
$("menuBtn").onclick = openSidebar;
$("backdrop").onclick = closeSidebar;

// автофокус на имя
setTimeout(() => $("nameInput").focus(), 100);

})();
</script>
</body>
</html>
"""


# ======================= ЗАПУСК =======================
if __name__ == "__main__":
    # loop="uvloop" если установлен — даст ещё +30% к скорости сокетов
    try:
        import uvloop  # type: ignore
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning", loop="uvloop")
    except ImportError:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
