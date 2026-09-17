"""
Nexus DM — E2EE мессенджер 1-на-1 (как SMS).
- Логин/пароль (scrypt + salt) — сервер хранит только хеш.
- Только личные сообщения 1-на-1. Групповых нет.
- E2EE: ECDH P-256 + AES-GCM 256. Приватный ключ НИКОГДА не покидает браузер.
- Сервер видит только шифротекст (ct/iv) + метаданные.
- Всё в оперативке. Ничего не пишется на диск.

Запуск:  python main.py   →   http://localhost:8000
"""

import asyncio
import hashlib
import json
import os
import secrets
import struct
import time
import uuid
from collections import deque
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ============================ ПРОТОКОЛ ============================
T_REGISTER, T_AUTH, T_AUTH_OK, T_MSG, T_USERS, T_PING, T_PONG, T_ERROR = range(1, 9)
_HDR = struct.Struct(">BI")


def pack(t: int, o: Any) -> bytes:
    p = json.dumps(o, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _HDR.pack(t, len(p)) + p


def unpack(data: bytes):
    t, ln = _HDR.unpack_from(data, 0)
    return t, json.loads(data[5:5 + ln].decode("utf-8"))


# ============================ СОСТОЯНИЕ (RAM) ============================
users: dict[str, dict] = {}      # login -> {salt, pw, pub}
messages: deque = deque(maxlen=5000)
online: dict[str, "Client"] = {}


def _scrypt(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


class Client:
    __slots__ = ("ws", "login", "lock")

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.login: Optional[str] = None
        self.lock = asyncio.Lock()

    async def send(self, t: int, o: Any):
        async with self.lock:
            try:
                await self.ws.send_bytes(pack(t, o))
            except Exception:
                pass


def public_users() -> list[dict]:
    return [
        {"login": l, "pub": u["pub"], "online": l in online}
        for l, u in users.items()
    ]


async def broadcast_users():
    if not online:
        return
    frame = pack(T_USERS, {"users": public_users()})
    for c in list(online.values()):
        async with c.lock:
            try:
                await c.ws.send_bytes(frame)
            except Exception:
                pass


async def handle_register(c: Client, obj: dict):
    login = (obj.get("login") or "").strip().lower()
    pw = obj.get("password") or ""
    pub = obj.get("pub") or ""

    if not login or not pw or not pub:
        return await c.send(T_ERROR, {"msg": "Заполните все поля"})
    if not (3 <= len(login) <= 24):
        return await c.send(T_ERROR, {"msg": "Логин: 3–24 символа"})
    if not all(ch.isalnum() or ch in "_-." for ch in login):
        return await c.send(T_ERROR, {"msg": "Логин: только буквы, цифры, _ - ."})
    if len(pw) < 6:
        return await c.send(T_ERROR, {"msg": "Пароль минимум 6 символов"})
    if login in users:
        return await c.send(T_ERROR, {"msg": "Логин уже занят"})

    salt = os.urandom(16)
    users[login] = {"salt": salt, "pw": _scrypt(pw, salt), "pub": pub}
    await _finish_auth(c, login)


async def handle_auth(c: Client, obj: dict):
    login = (obj.get("login") or "").strip().lower()
    pw = obj.get("password") or ""
    u = users.get(login)
    if not u or not secrets.compare_digest(u["pw"], _scrypt(pw, u["salt"])):
        return await c.send(T_ERROR, {"msg": "Неверный логин или пароль"})
    if login in online:
        return await c.send(T_ERROR, {"msg": "Вы уже вошли с другого устройства"})
    await _finish_auth(c, login)


async def _finish_auth(c: Client, login: str):
    c.login = login
    online[login] = c
    hist = [m for m in messages if m["from"] == login or m["to"] == login]
    await c.send(T_AUTH_OK, {"login": login, "users": public_users(), "history": hist})
    await broadcast_users()


async def handle_msg(c: Client, obj: dict):
    to = (obj.get("to") or "").strip().lower()
    ct = obj.get("ct") or ""
    iv = obj.get("iv") or ""
    mid = obj.get("id") or uuid.uuid4().hex

    if not to or not ct or not iv:
        return
    if to not in users:
        return await c.send(T_ERROR, {"msg": "Получатель не найден"})
    if to == c.login:
        return await c.send(T_ERROR, {"msg": "Нельзя писать самому себе"})

    msg = {"id": mid, "from": c.login, "to": to, "ct": ct, "iv": iv,
           "ts": int(time.time() * 1000)}
    messages.append(msg)

    target = online.get(to)
    if target is not None:
        await target.send(T_MSG, msg)
    await c.send(T_MSG, msg)


app = FastAPI()


@app.get("/")
async def index():
    return HTMLResponse(HTML_PAGE)


@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    c = Client(ws)
    try:
        while True:
            raw = await ws.receive_bytes()
            try:
                t, obj = unpack(raw)
            except Exception:
                continue
            if t == T_REGISTER:
                await handle_register(c, obj)
            elif t == T_AUTH:
                await handle_auth(c, obj)
            elif t == T_MSG and c.login:
                await handle_msg(c, obj)
            elif t == T_PING:
                await c.send(T_PONG, {"t": obj.get("t", 0)})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if c.login and online.get(c.login) is c:
            online.pop(c.login, None)
            await broadcast_users()


# ============================ HTML (Discord-white, SVG) ============================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>Messages</title>
<style>
:root{
  --bg-primary:#ffffff;
  --bg-secondary:#f2f3f5;
  --bg-tertiary:#e3e5e8;
  --bg-hover:#e8eaed;
  --bg-active:#d7dae0;
  --text-normal:#2e3338;
  --text-muted:#747f8d;
  --border:#e3e5e8;
  --accent:#5865f2;
  --accent-hover:#4752c4;
  --green:#3ba55d;
  --red:#ed4245;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;overscroll-behavior:none}
body{
  background:var(--bg-primary);color:var(--text-normal);
  font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
button{font:inherit;cursor:pointer;color:inherit}
input{font:inherit}

/* ================= LOGIN ================= */
#login{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  padding:20px;background:var(--bg-secondary);z-index:100}
.card{width:100%;max-width:400px;background:var(--bg-primary);border-radius:14px;
  padding:26px;box-shadow:0 10px 30px rgba(0,0,0,.10)}
.tabs{display:flex;background:var(--bg-secondary);border-radius:9px;padding:3px;margin-bottom:18px}
.tabs button{flex:1;background:none;border:none;color:var(--text-muted);
  padding:9px;border-radius:7px;font-weight:500;transition:all .15s}
.tabs button.active{background:var(--bg-primary);color:var(--text-normal);
  box-shadow:0 1px 2px rgba(0,0,0,.08)}
.card input{width:100%;background:var(--bg-secondary);border:1px solid transparent;
  color:var(--text-normal);padding:12px 14px;border-radius:8px;outline:none;
  font-size:16px;margin-bottom:10px;transition:border .12s}
.card input:focus{border-color:var(--accent)}
.card input::placeholder{color:var(--text-muted)}
#submitBtn{width:100%;background:var(--accent);color:#fff;border:none;padding:12px;
  border-radius:8px;font-weight:600;margin-top:4px;transition:background .12s}
#submitBtn:hover{background:var(--accent-hover)}
#submitBtn:disabled{opacity:.55;cursor:default}
#authErr{color:var(--red);font-size:13px;margin-top:10px;min-height:17px;text-align:center}
#authNote{color:var(--text-muted);font-size:12px;margin-top:4px;text-align:center;line-height:1.35}

/* ================= APP ================= */
#app{display:none;height:100dvh}
#app.on{display:flex}

/* ---------- SIDEBAR (chat list) ---------- */
#sidebar{width:320px;flex-shrink:0;background:var(--bg-secondary);
  display:flex;flex-direction:column;border-right:1px solid var(--border)}
.sidebar-header{display:flex;align-items:center;gap:10px;padding:12px 14px;
  padding-top:calc(12px + env(safe-area-inset-top));
  border-bottom:1px solid var(--border);background:var(--bg-primary)}
.avatar{width:40px;height:40px;border-radius:50%;color:#fff;font-weight:600;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  font-size:16px;text-transform:uppercase;user-select:none}
.my-info{flex:1;min-width:0}
.my-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.my-sub{font-size:12px;color:var(--text-muted);margin-top:1px}
.icon-btn{background:none;border:none;color:var(--text-muted);padding:6px;
  border-radius:6px;display:flex;align-items:center;justify-content:center;
  transition:background .12s,color .12s}
.icon-btn:hover{background:var(--bg-hover);color:var(--text-normal)}

.sidebar-title{padding:14px 16px 6px;font-size:11px;font-weight:700;
  color:var(--text-muted);text-transform:uppercase;letter-spacing:.02em}

#contacts{flex:1;overflow-y:auto;padding:0 8px 8px}
.empty-list{padding:32px 20px;text-align:center;color:var(--text-muted);
  font-size:13px;line-height:1.5}

.contact{display:flex;align-items:center;gap:11px;padding:8px 10px;
  border-radius:8px;cursor:pointer;transition:background .1s;user-select:none}
.contact:hover{background:var(--bg-hover)}
.contact.active{background:var(--bg-active)}
.avatar-wrap{position:relative;flex-shrink:0}
.status-dot{position:absolute;right:-2px;bottom:-2px;width:14px;height:14px;
  border-radius:50%;background:#b9bbbe;border:3px solid var(--bg-secondary)}
.status-dot.online{background:var(--green)}
.contact.active .status-dot{border-color:var(--bg-active)}
.contact-info{flex:1;min-width:0}
.contact-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;color:var(--text-normal)}
.contact-preview{font-size:13px;color:var(--text-muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.contact.unread .contact-name{color:#000}
.contact.unread .contact-preview{color:var(--text-normal);font-weight:500}
.badge{background:var(--red);color:#fff;font-size:12px;font-weight:600;
  min-width:20px;height:20px;padding:0 7px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;line-height:1}

/* ---------- CHAT PANE ---------- */
#chatPane{flex:1;display:flex;flex-direction:column;min-width:0;
  background:var(--bg-primary)}
#emptyState{flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;color:var(--text-muted);gap:14px;padding:24px;text-align:center}
#emptyState svg{opacity:.28}
#emptyState p{font-size:14px}

#conversation{display:none;flex-direction:column;flex:1;min-height:0}
#chatPane.has-chat #conversation{display:flex}
#chatPane.has-chat #emptyState{display:none}

.conv-header{display:flex;align-items:center;gap:8px;padding:10px 14px;
  padding-top:calc(10px + env(safe-area-inset-top));
  border-bottom:1px solid var(--border);background:var(--bg-primary)}
#backBtn{display:none}
.conv-peer{display:flex;flex-direction:column;min-width:0;flex:1}
.peer-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.peer-status{font-size:12px;color:var(--text-muted)}
.peer-status.online{color:var(--green)}

#msgs{flex:1;overflow-y:auto;padding:16px 16px 6px;display:flex;
  flex-direction:column;gap:4px;scroll-behavior:smooth}
.m{max-width:70%;padding:8px 13px;border-radius:16px;
  background:var(--bg-secondary);align-self:flex-start;
  word-wrap:break-word;overflow-wrap:anywhere;
  animation:pop .13s ease-out;color:var(--text-normal);font-size:15px;line-height:1.4}
@keyframes pop{from{opacity:.4;transform:translateY(3px)}to{opacity:1;transform:none}}
.m.me{align-self:flex-end;background:var(--accent);color:#fff}
.m .ts{font-size:11px;opacity:.65;margin-top:2px;display:block;text-align:right}
.m.me .ts{opacity:.85}
.m.broken{background:transparent;border:1px dashed var(--border);
  color:var(--text-muted);font-style:italic}
.day-sep{align-self:center;font-size:11px;color:var(--text-muted);
  padding:6px 12px;background:var(--bg-secondary);border-radius:10px;margin:8px 0 4px}

#composer{display:flex;align-items:center;gap:8px;padding:10px 14px;
  padding-bottom:calc(10px + env(safe-area-inset-bottom));
  border-top:1px solid var(--border);background:var(--bg-primary)}
#inp{flex:1;background:var(--bg-secondary);border:1px solid transparent;
  color:var(--text-normal);padding:11px 16px;border-radius:20px;
  outline:none;font-size:16px;min-width:0;transition:border .12s}
#inp:focus{border-color:var(--accent)}
#inp::placeholder{color:var(--text-muted)}
#sendBtn{width:42px;height:42px;border-radius:50%;background:var(--accent);
  color:#fff;border:none;display:flex;align-items:center;justify-content:center;
  flex-shrink:0;transition:background .12s,transform .06s}
#sendBtn:hover{background:var(--accent-hover)}
#sendBtn:active{transform:scale(.94)}

/* ================= MOBILE ================= */
@media (max-width:720px){
  #app{position:relative;overflow:hidden}
  #sidebar{position:absolute;inset:0;width:100%;z-index:2;
    border-right:none;transition:transform .26s ease}
  #chatPane{position:absolute;inset:0;z-index:3;transform:translateX(100%);
    transition:transform .26s ease;box-shadow:-8px 0 24px rgba(0,0,0,.10)}
  #app.chat-open #sidebar{transform:translateX(-26%)}
  #app.chat-open #chatPane{transform:translateX(0)}
  #backBtn{display:flex}
  .m{max-width:86%}
}
</style>
</head>
<body>

<!-- ================= ЛОГИН ================= -->
<div id="login">
  <form class="card" id="authForm" autocomplete="on">
    <div class="tabs">
      <button type="button" id="tabLogin" class="active">Вход</button>
      <button type="button" id="tabReg">Регистрация</button>
    </div>
    <input id="loginIn" placeholder="Логин" autocapitalize="off"
           spellcheck="false" maxlength="24" autocomplete="username">
    <input id="pwIn" type="password" placeholder="Пароль"
           autocomplete="current-password">
    <button type="submit" id="submitBtn">Войти</button>
    <div id="authErr"></div>
    <div id="authNote"></div>
  </form>
</div>

<!-- ================= ПРИЛОЖЕНИЕ ================= -->
<div id="app">
  <!-- Список чатов + мой ник сверху -->
  <aside id="sidebar">
    <div class="sidebar-header">
      <div class="avatar" id="myAvatar">?</div>
      <div class="my-info">
        <div class="my-name" id="myLogin">—</div>
        <div class="my-sub">в сети</div>
      </div>
      <button class="icon-btn" id="logoutBtn" title="Выйти" aria-label="Выйти">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path fill-rule="evenodd" clip-rule="evenodd"
            d="M5 3a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h6v-2H5V5h6V3H5zm12.28 4.22 5.5 5.5-5.5 5.5-1.42-1.42 3.09-3.08H9v-2h9.95l-3.09-3.08 1.42-1.42z"/>
        </svg>
      </button>
    </div>
    <div class="sidebar-title">Сообщения</div>
    <div id="contacts"></div>
  </aside>

  <!-- Правая панель / диалог -->
  <section id="chatPane">
    <div id="emptyState">
      <svg width="90" height="90" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM7 9h10v2H7V9zm6 5H7v-2h6v2zm4-6H7V6h10v2z"/>
      </svg>
      <p>Выберите чат, чтобы начать общение</p>
    </div>

    <div id="conversation">
      <div class="conv-header">
        <button class="icon-btn" id="backBtn" aria-label="Назад">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/>
          </svg>
        </button>
        <div class="conv-peer">
          <div class="peer-name" id="peerName">—</div>
          <div class="peer-status" id="peerStatus"></div>
        </div>
      </div>

      <div id="msgs"></div>

      <div id="composer">
        <input id="inp" placeholder="Написать сообщение…" autocomplete="off"
               autocapitalize="sentences">
        <button id="sendBtn" aria-label="Отправить">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M2.01 21 23 12 2.01 3 2 10l15 2-15 2z"/>
          </svg>
        </button>
      </div>
    </div>
  </section>
</div>

<script>
(() => {
"use strict";

/* ==================== ПРОТОКОЛ ==================== */
const T = {REGISTER:1, AUTH:2, AUTH_OK:3, MSG:4, USERS:5, PING:6, PONG:7, ERROR:8};
const _enc = new TextEncoder(), _dec = new TextDecoder();

function pack(type, obj){
  const p = _enc.encode(JSON.stringify(obj));
  const buf = new ArrayBuffer(5 + p.length);
  const dv = new DataView(buf);
  dv.setUint8(0, type);
  dv.setUint32(1, p.length, false);
  new Uint8Array(buf, 5).set(p);
  return buf;
}
function unpack(buf){
  const dv = new DataView(buf);
  const len = dv.getUint32(1, false);
  return [dv.getUint8(0), JSON.parse(_dec.decode(new Uint8Array(buf, 5, len)))];
}

/* ==================== CRYPTO ==================== */
function b64(u8){ let s=""; for(let i=0;i<u8.length;i++) s+=String.fromCharCode(u8[i]); return btoa(s); }
function unb64(s){ const b=atob(s); const u=new Uint8Array(b.length); for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i); return u; }

async function genKeyPair(){
  return crypto.subtle.generateKey({name:"ECDH",namedCurve:"P-256"},true,["deriveKey"]);
}
async function exportPubRaw(pub){
  return b64(new Uint8Array(await crypto.subtle.exportKey("raw", pub)));
}
async function exportPrivJWK(priv){
  return await crypto.subtle.exportKey("jwk", priv);
}
async function importPrivJWK(jwk){
  return crypto.subtle.importKey("jwk",jwk,{name:"ECDH",namedCurve:"P-256"},true,["deriveKey"]);
}
async function importPubRaw(str){
  return crypto.subtle.importKey("raw",unb64(str),{name:"ECDH",namedCurve:"P-256"},true,[]);
}
async function deriveAesKey(priv, pub){
  return crypto.subtle.deriveKey({name:"ECDH",public:pub},priv,
    {name:"AES-GCM",length:256},false,["encrypt","decrypt"]);
}
async function aesEncrypt(key, text){
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const buf = await crypto.subtle.encrypt({name:"AES-GCM",iv}, key, _enc.encode(text));
  return {ct: b64(new Uint8Array(buf)), iv: b64(iv)};
}
async function aesDecrypt(key, ct, iv){
  const pt = await crypto.subtle.decrypt({name:"AES-GCM",iv:unb64(iv)}, key, unb64(ct));
  return _dec.decode(pt);
}

/* ==================== СОСТОЯНИЕ КЛИЕНТА ==================== */
let ws = null;
let me = null;
let myPrivKey = null;
let sessionPassword = null;
let usersList = [];
const convKeys = {};
const threads = {};
const unread = {};
const seenIds = new Set();
let activePeer = null;
let reconnectAttempts = 0;
let heartbeatTimer = null;
let mode = "login";
let authPayload = null;

const $ = id => document.getElementById(id);

/* ==================== АВАТАРНЫЕ ЦВЕТА ==================== */
function avatarColor(login){
  const colors = ["#5865f2","#3ba55d","#faa61a","#ed4245","#eb459e","#9b59b6","#1abc9c","#e67e22"];
  let h = 0;
  for (let i=0;i<login.length;i++) h = (h * 31 + login.charCodeAt(i)) | 0;
  return colors[Math.abs(h) % colors.length];
}
function avatarChar(login){ return (login || "?")[0].toUpperCase(); }

/* ==================== РЕНДЕР СПИСКА ЧАТОВ ==================== */
function renderContacts(){
  const box = $("contacts");
  box.innerHTML = "";
  const others = usersList.filter(u => u.login !== me);
  if (!others.length){
    const d = document.createElement("div");
    d.className = "empty-list";
    d.textContent = "Пока никого нет. Зарегистрируйте второго пользователя в другом браузере.";
    box.appendChild(d);
    return;
  }
  // Сортировка: сначала те, у кого есть переписка (по времени), потом online, потом по алфавиту
  others.sort((a,b) => {
    const la = threads[a.login] || [], lb = threads[b.login] || [];
    const ta = la.length ? la[la.length-1].ts : 0;
    const tb = lb.length ? lb[lb.length-1].ts : 0;
    if (ta !== tb) return tb - ta;
    if (a.online !== b.online) return a.online ? -1 : 1;
    return a.login.localeCompare(b.login);
  });
  for (const u of others){
    const isActive = u.login === activePeer;
    const hasUnread = unread[u.login] > 0;
    const el = document.createElement("div");
    el.className = "contact" + (isActive ? " active" : "") + (hasUnread ? " unread" : "");
    el.onclick = () => selectPeer(u.login);

    const wrap = document.createElement("div");
    wrap.className = "avatar-wrap";
    const av = document.createElement("div");
    av.className = "avatar";
    av.style.background = avatarColor(u.login);
    av.textContent = avatarChar(u.login);
    const dot = document.createElement("span");
    dot.className = "status-dot" + (u.online ? " online" : "");
    wrap.append(av, dot);

    const info = document.createElement("div");
    info.className = "contact-info";
    const nm = document.createElement("div");
    nm.className = "contact-name";
    nm.textContent = u.login;
    const pv = document.createElement("div");
    pv.className = "contact-preview";
    const t = threads[u.login];
    if (t && t.length){
      const last = t[t.length-1];
      const mine = last.from === me;
      pv.textContent = (mine ? "Вы: " : "") + (last.broken ? "⚠ зашифровано" : last.text);
    } else {
      pv.textContent = u.online ? "в сети" : "не в сети";
    }
    info.append(nm, pv);

    el.append(wrap, info);

    if (hasUnread){
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = unread[u.login] > 99 ? "99+" : unread[u.login];
      el.appendChild(b);
    }
    box.appendChild(el);
  }
}

/* ==================== ВЫБОР ЧАТА ==================== */
function selectPeer(login){
  activePeer = login;
  unread[login] = 0;
  $("peerName").textContent = login;
  const u = usersList.find(x => x.login === login);
  const st = $("peerStatus");
  st.textContent = u && u.online ? "в сети" : "не в сети";
  st.className = "peer-status" + (u && u.online ? " online" : "");
  $("chatPane").classList.add("has-chat");
  $("app").classList.add("chat-open");   // для мобильной анимации
  renderContacts();
  renderThread();
  setTimeout(() => $("inp").focus(), 60);
}

function goBack(){
  $("app").classList.remove("chat-open");
}

/* ==================== РЕНДЕР ДИАЛОГА ==================== */
function fmtTime(ts){
  return new Date(ts).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
}
function fmtDay(ts){
  const d = new Date(ts), t = new Date();
  if (d.toDateString() === t.toDateString()) return "Сегодня";
  const y = new Date(t); y.setDate(y.getDate()-1);
  if (d.toDateString() === y.toDateString()) return "Вчера";
  return d.toLocaleDateString();
}
function buildMsgEl(m){
  const mine = m.from === me;
  const el = document.createElement("div");
  el.className = "m" + (mine ? " me" : "") + (m.broken ? " broken" : "");
  const txt = document.createElement("div");
  txt.textContent = m.text;
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = fmtTime(m.ts);
  el.append(txt, ts);
  return el;
}
function appendMsg(m){
  const box = $("msgs");
  const day = fmtDay(m.ts);
  const seps = box.querySelectorAll(".day-sep");
  if (!seps.length || seps[seps.length-1].textContent !== day){
    const s = document.createElement("div");
    s.className = "day-sep";
    s.textContent = day;
    box.appendChild(s);
  }
  box.appendChild(buildMsgEl(m));
  box.scrollTop = box.scrollHeight;
}
function renderThread(){
  const box = $("msgs");
  box.innerHTML = "";
  if (!activePeer) return;
  const list = threads[activePeer] || [];
  let lastDay = "";
  for (const m of list){
    const day = fmtDay(m.ts);
    if (day !== lastDay){
      lastDay = day;
      const s = document.createElement("div");
      s.className = "day-sep";
      s.textContent = day;
      box.appendChild(s);
    }
    box.appendChild(buildMsgEl(m));
  }
  box.scrollTop = box.scrollHeight;
}

/* ==================== КЛЮЧ ДИАЛОГА ==================== */
async function getConvKey(peer){
  if (convKeys[peer]) return convKeys[peer];
  const u = usersList.find(x => x.login === peer);
  if (!u || !u.pub) return null;
  try {
    const pub = await importPubRaw(u.pub);
    const key = await deriveAesKey(myPrivKey, pub);
    convKeys[peer] = key;
    return key;
  } catch(e){ return null; }
}

/* ==================== WS ==================== */
function connect(){
  return new Promise((resolve, reject) => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.binaryType = "arraybuffer";
    let settled = false;
    const to = setTimeout(() => {
      if (!settled){ settled = true; try{ws.close();}catch(e){}; reject(new Error("Таймаут")); }
    }, 8000);
    ws.onopen = () => ws.send(pack(mode === "register" ? T.REGISTER : T.AUTH, authPayload));
    ws.onmessage = async ev => {
      let type, obj;
      try { [type, obj] = unpack(ev.data); } catch(e){ return; }
      if (!settled){
        if (type === T.AUTH_OK){ settled = true; clearTimeout(to); resolve(obj); return; }
        if (type === T.ERROR){ settled = true; clearTimeout(to);
          try{ws.close();}catch(e){}; reject(new Error(obj.msg || "Ошибка")); return; }
      }
      await handleFrame(type, obj);
    };
    ws.onclose = () => {
      if (!settled){ settled = true; clearTimeout(to); reject(new Error("Соединение закрыто")); }
      else if (me) onDisconnect();
    };
    ws.onerror = () => {
      if (!settled){ settled = true; clearTimeout(to); reject(new Error("Ошибка соединения")); }
    };
  });
}

async function handleFrame(type, obj){
  switch(type){
    case T.MSG:   await onIncomingMsg(obj); break;
    case T.USERS: usersList = obj.users || []; renderContacts();
                  if (activePeer){
                    const u = usersList.find(x => x.login === activePeer);
                    const st = $("peerStatus");
                    if (u){
                      st.textContent = u.online ? "в сети" : "не в сети";
                      st.className = "peer-status" + (u.online ? " online" : "");
                    }
                  }
                  break;
    case T.PING:  if (ws && ws.readyState === 1) ws.send(pack(T.PONG, {})); break;
    case T.PONG:  break;
    case T.ERROR: console.warn("server error:", obj.msg); break;
  }
}

async function onIncomingMsg(m){
  if (!m || seenIds.has(m.id)) return;
  seenIds.add(m.id);
  const peer = m.from === me ? m.to : m.from;
  const key = await getConvKey(peer);
  let text, broken = false;
  if (!key){ text = "⚠ нет ключа для расшифровки"; broken = true; }
  else {
    try { text = await aesDecrypt(key, m.ct, m.iv); }
    catch(e){ text = "⚠ не удалось расшифровать"; broken = true; }
  }
  const msg = {id:m.id, from:m.from, to:m.to, text, ts:m.ts, broken};
  if (!threads[peer]) threads[peer] = [];
  threads[peer].push(msg);
  if (activePeer === peer){
    appendMsg(msg);
    renderContacts();
  } else {
    unread[peer] = (unread[peer] || 0) + 1;
    renderContacts();
  }
}

/* ==================== АВТОРИЗАЦИЯ ==================== */
function setErr(m){ $("authErr").textContent = m || ""; }
function setNote(m){ $("authNote").textContent = m || ""; }

async function doAuth(login, password){
  login = login.trim().toLowerCase();
  if (!login || !password){ setErr("Заполните все поля"); return; }
  if (!/^[a-z0-9._-]{3,24}$/.test(login)){ setErr("Логин 3–24: a-z 0-9 . _ -"); return; }
  if (mode === "register" && password.length < 6){ setErr("Пароль минимум 6 символов"); return; }
  setErr(""); setNote("");

  const lsKey = "nexus_dm_key_" + login;
  if (mode === "register"){
    const kp = await genKeyPair();
    myPrivKey = kp.privateKey;
    localStorage.setItem(lsKey, JSON.stringify(await exportPrivJWK(kp.privateKey)));
    const pubRaw = await exportPubRaw(kp.publicKey);
    authPayload = {login, password, pub: pubRaw};
  } else {
    const saved = localStorage.getItem(lsKey);
    if (!saved){
      setNote("⚠ Приватный ключ не найден в этом браузере — старые сообщения не расшифруются.");
    } else {
      try { myPrivKey = await importPrivJWK(JSON.parse(saved)); }
      catch(e){ setNote("⚠ Ключ повреждён."); }
    }
    authPayload = {login, password};
  }

  $("submitBtn").disabled = true;
  try {
    const ok = await connect();
    sessionPassword = password;
    me = ok.login;
    usersList = ok.users || [];
    await loadHistory(ok.history || []);
    $("myLogin").textContent = "@" + me;
    $("myAvatar").textContent = avatarChar(me);
    $("myAvatar").style.background = avatarColor(me);
    $("login").style.display = "none";
    $("app").classList.add("on");
    renderContacts();
    startHeartbeat();
    reconnectAttempts = 0;
  } catch(e){
    setErr(e.message || "Ошибка");
    $("submitBtn").disabled = false;
  }
}

async function loadHistory(history){
  const byPeer = {};
  for (const m of history){
    const peer = m.from === me ? m.to : m.from;
    (byPeer[peer] = byPeer[peer] || []).push(m);
    seenIds.add(m.id);
  }
  for (const peer of Object.keys(byPeer)){
    const key = await getConvKey(peer);
    const list = byPeer[peer].sort((a,b) => a.ts - b.ts);
    const out = [];
    for (const m of list){
      let text, broken = false;
      if (!key){ text = "⚠ нет ключа"; broken = true; }
      else {
        try { text = await aesDecrypt(key, m.ct, m.iv); }
        catch(e){ text = "⚠ не удалось расшифровать"; broken = true; }
      }
      out.push({id:m.id, from:m.from, to:m.to, text, ts:m.ts, broken});
    }
    threads[peer] = out;
  }
}

/* ==================== ОТПРАВКА ==================== */
async function sendMessage(){
  if (!activePeer || !ws || ws.readyState !== 1) return;
  const inp = $("inp");
  const text = inp.value.trim();
  if (!text) return;
  const key = await getConvKey(activePeer);
  if (!key){ alert("Нет ключа получателя"); return; }

  const {ct, iv} = await aesEncrypt(key, text);
  const id = (crypto.randomUUID && crypto.randomUUID()) ||
             (Date.now().toString(36) + Math.random().toString(36).slice(2,8));

  const localMsg = {id, from: me, to: activePeer, text, ts: Date.now()};
  seenIds.add(id);
  (threads[activePeer] = threads[activePeer] || []).push(localMsg);
  appendMsg(localMsg);
  renderContacts();

  inp.value = "";
  inp.focus();

  ws.send(pack(T.MSG, {id, to: activePeer, ct, iv}));
}

/* ==================== ПЕРЕПОДКЛЮЧЕНИЕ ==================== */
function startHeartbeat(){
  clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => {
    if (ws && ws.readyState === 1) ws.send(pack(T.PING, {t: Date.now()}));
  }, 25000);
}
async function onDisconnect(){
  clearInterval(heartbeatTimer);
  if (!sessionPassword) return;
  if (reconnectAttempts >= 5){ alert("Соединение потеряно. Обновите страницу."); return; }
  reconnectAttempts++;
  const login = me, pw = sessionPassword;
  try {
    authPayload = {login, password: pw};
    mode = "login";
    const ok = await connect();
    usersList = ok.users || [];
    renderContacts();
    startHeartbeat();
    reconnectAttempts = 0;
  } catch(e){
    setTimeout(onDisconnect, 800 * reconnectAttempts);
  }
}

/* ==================== UI BIND ==================== */
function setMode(m){
  mode = m;
  $("tabLogin").classList.toggle("active", m === "login");
  $("tabReg").classList.toggle("active", m === "register");
  $("submitBtn").textContent = m === "login" ? "Войти" : "Создать аккаунт";
  $("pwIn").autocomplete = m === "login" ? "current-password" : "new-password";
  setErr(""); setNote("");
}

$("tabLogin").onclick = () => setMode("login");
$("tabReg").onclick   = () => setMode("register");
$("authForm").addEventListener("submit", e => {
  e.preventDefault();
  doAuth($("loginIn").value, $("pwIn").value);
});

$("logoutBtn").onclick = () => {
  sessionPassword = null;
  try { ws && ws.close(); } catch(e){}
  location.reload();
};

$("sendBtn").onclick = () => sendMessage();
$("inp").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); sendMessage(); }
});
$("backBtn").onclick = goBack;

setTimeout(() => $("loginIn").focus(), 100);

})();
</script>
</body>
</html>
"""


# ============================ ЗАПУСК ============================
if __name__ == "__main__":
    try:
        import uvloop  # type: ignore
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning", loop="uvloop")
    except ImportError:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
