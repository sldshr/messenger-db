"""
Nexus DM — E2EE мессенджер 1-на-1 (как SMS).
- Логин/пароль (scrypt + salt) — сервер хранит только хеш.
- Переписка 1-на-1. Только личные сообщения, групповых нет.
- E2EE: ECDH P-256 + AES-GCM 256. Приватный ключ НИКОГДА не покидает браузер.
- Сервер видит только шифротекст (ct/iv) и метаданные (от кого/кому/когда).
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
# [type:uint8][length:uint32 BE][payload JSON UTF-8]
T_REGISTER, T_AUTH, T_AUTH_OK, T_MSG, T_USERS, T_PING, T_PONG, T_ERROR = range(1, 9)
_HDR = struct.Struct(">BI")


def pack(t: int, o: Any) -> bytes:
    p = json.dumps(o, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _HDR.pack(t, len(p)) + p


def unpack(data: bytes):
    t, ln = _HDR.unpack_from(data, 0)
    return t, json.loads(data[5:5 + ln].decode("utf-8"))


# ============================ СОСТОЯНИЕ (RAM) ============================
# users[login] = {"salt": bytes, "pw": bytes, "pub": base64-raw-ECDH-pubkey}
users: dict[str, dict] = {}

# Все сообщения (шифротекст!). Сервер не может их прочитать.
# {id, from, to, ct(b64), iv(b64), ts}
messages: deque = deque(maxlen=5000)

# online[login] = Client
online: dict[str, "Client"] = {}


# ============================ ХЕШ ПАРОЛЯ ============================
def _scrypt(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


# ============================ КЛИЕНТ ============================
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


# ============================ ОБРАБОТЧИКИ ============================
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
    # История только для этого пользователя (только его диалоги)
    hist = [m for m in messages if m["from"] == login or m["to"] == login]
    await c.send(T_AUTH_OK, {
        "login": login,
        "users": public_users(),
        "history": hist,
    })
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

    msg = {
        "id": mid,
        "from": c.login,
        "to": to,
        "ct": ct,
        "iv": iv,
        "ts": int(time.time() * 1000),
    }
    messages.append(msg)

    target = online.get(to)
    if target is not None:
        await target.send(T_MSG, msg)
    # Эхо отправителю (подтверждение + серверный ts)
    await c.send(T_MSG, msg)


# ============================ HTTP ============================
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


# ============================ HTML ============================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#0b0f14">
<title>Nexus DM</title>
<style>
:root{
  --bg:#0b0f14; --bg2:#131922; --bg3:#1c2330; --bg4:#242d3d;
  --fg:#e6edf3; --mut:#8b949e; --bord:#232b38;
  --acc:#6366f1; --acc-h:#4f46e5; --ok:#22c55e; --err:#ef4444;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;overscroll-behavior:none;
  background:var(--bg);color:var(--fg);
  font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
button{font:inherit;cursor:pointer;color:inherit}

/* ===== login ===== */
#login{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  padding:20px;background:radial-gradient(circle at 50% 0%,#1a2435,#0b0f14 65%);z-index:100}
.card{width:100%;max-width:380px;background:var(--bg2);border:1px solid var(--bord);
  border-radius:18px;padding:26px;box-shadow:0 20px 60px rgba(0,0,0,.55)}
.card h1{font-size:22px;margin-bottom:4px}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px;line-height:1.35}
.tabs{display:flex;gap:4px;background:var(--bg3);padding:4px;border-radius:11px;margin-bottom:14px}
.tabs button{flex:1;background:none;border:none;color:var(--mut);
  padding:9px;border-radius:8px;font-weight:500;transition:all .12s}
.tabs button.active{background:var(--bg4);color:var(--fg)}
.card input{width:100%;background:var(--bg3);border:1px solid var(--bord);color:var(--fg);
  padding:12px 14px;border-radius:10px;outline:none;font:inherit;margin-bottom:10px;
  font-size:16px}
.card input:focus{border-color:var(--acc)}
#submitBtn{width:100%;background:var(--acc);color:#fff;border:none;padding:13px;
  border-radius:10px;font-weight:600;margin-top:4px;transition:background .12s}
#submitBtn:active{background:var(--acc-h)}
#submitBtn:disabled{opacity:.55;cursor:default}
#authErr{color:var(--err);font-size:13px;margin-top:10px;min-height:17px;text-align:center}
#authNote{color:var(--mut);font-size:12px;margin-top:6px;text-align:center;line-height:1.3}

/* ===== app ===== */
#app{display:none;height:100dvh}
#app.on{display:flex}

#side{width:300px;background:var(--bg2);border-right:1px solid var(--bord);
  display:flex;flex-direction:column;flex-shrink:0}
.side-head{display:flex;align-items:center;gap:10px;padding:12px 14px;
  border-bottom:1px solid var(--bord);padding-top:calc(12px + env(safe-area-inset-top))}
.avatar{width:40px;height:40px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);
  display:flex;align-items:center;justify-content:center;font-weight:700;color:#fff;
  flex-shrink:0;font-size:15px;text-transform:uppercase}
.me-info{display:flex;align-items:center;gap:10px;flex:1;min-width:0}
.me-login{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.me-status{font-size:12px;color:var(--ok);display:flex;align-items:center;gap:5px}
.me-status::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--ok)}
#logoutBtn{background:none;border:none;color:var(--mut);font-size:20px;padding:6px;
  border-radius:8px;transition:all .12s}
#logoutBtn:hover{color:var(--fg);background:var(--bg3)}

#contacts{flex:1;overflow-y:auto;padding:6px}
.empty-list{padding:24px;text-align:center;color:var(--mut);font-size:13px}
.contact{display:flex;align-items:center;gap:11px;padding:10px 11px;
  border-radius:11px;cursor:pointer;user-select:none;transition:background .1s}
.contact:hover{background:var(--bg3)}
.contact.active{background:var(--bg4)}
.contact .info{flex:1;min-width:0}
.contact .name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.contact .sub{font-size:12px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;margin-top:1px}
.contact .badge{background:var(--err);color:#fff;font-size:11px;font-weight:700;
  padding:2px 8px;border-radius:10px;line-height:1.3;flex-shrink:0}

#chatPane{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg);
  position:relative}
#chatHead{padding:10px 16px;border-bottom:1px solid var(--bord);background:var(--bg2);
  display:flex;align-items:center;gap:10px;
  padding-top:calc(10px + env(safe-area-inset-top))}
#menuBtn{display:none;background:none;border:none;font-size:20px;padding:4px 8px;
  color:var(--fg)}
.peer-info{display:flex;flex-direction:column;min-width:0;flex:1}
.peer-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.peer-status{font-size:12px;color:var(--mut)}
.peer-status.online{color:var(--ok)}

#msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;
  gap:6px;scroll-behavior:smooth}
#msgs.hidden{display:none}
.m{max-width:78%;padding:8px 13px;border-radius:16px;background:var(--bg3);
  align-self:flex-start;word-wrap:break-word;overflow-wrap:anywhere;
  animation:pop .13s ease-out}
@keyframes pop{from{opacity:.4;transform:translateY(4px)}to{opacity:1;transform:none}}
.m.me{align-self:flex-end;background:var(--acc);color:#fff}
.m .ts{font-size:10px;opacity:.7;margin-top:3px;display:block;text-align:right}
.m.broken{opacity:.55;font-style:italic;background:transparent;border:1px dashed var(--bord)}
.day-sep{align-self:center;font-size:11px;color:var(--mut);padding:6px 12px;
  background:var(--bg3);border-radius:10px;margin:8px 0}

#emptyState{flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;color:var(--mut);text-align:center;padding:30px;gap:8px}
#emptyState .big{font-size:34px;opacity:.3}
#emptyState.hidden{display:none}

#composer{display:none;gap:8px;padding:10px 14px;border-top:1px solid var(--bord);
  background:var(--bg2);padding-bottom:calc(10px + env(safe-area-inset-bottom))}
#composer.on{display:flex}
#inp{flex:1;background:var(--bg3);border:1px solid var(--bord);color:var(--fg);
  padding:10px 16px;border-radius:22px;outline:none;font:inherit;min-width:0;font-size:16px}
#inp:focus{border-color:var(--acc)}
#sendBtn{background:var(--acc);color:#fff;border:none;border-radius:50%;
  width:44px;height:44px;flex-shrink:0;font-size:16px;transition:background .1s}
#sendBtn:active{background:var(--acc-h)}
#sendBtn:disabled{opacity:.5;cursor:default}

#backdrop{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40;
  opacity:0;pointer-events:none;transition:opacity .18s}
#backdrop.show{opacity:1;pointer-events:auto}

@media (max-width:720px){
  #side{position:fixed;left:0;top:0;bottom:0;z-index:50;
    width:min(82vw,320px);transform:translateX(-100%);transition:transform .2s}
  #side.open{transform:none}
  #menuBtn{display:block}
  .m{max-width:86%}
}
</style>
</head>
<body>

<!-- ============ ЛОГИН ============ -->
<div id="login">
  <form class="card" id="authForm">
    <h1>Nexus DM</h1>
    <div class="sub">Личные сообщения 1-на-1 с оконечным шифрованием (ECDH + AES-GCM). Ключи хранятся только у вас.</div>

    <div class="tabs">
      <button type="button" id="tabLogin" class="active">Вход</button>
      <button type="button" id="tabReg">Регистрация</button>
    </div>

    <input id="loginIn" placeholder="Логин" autocomplete="username"
           autocapitalize="off" spellcheck="false" maxlength="24">
    <input id="pwIn" type="password" placeholder="Пароль" autocomplete="current-password">

    <button type="submit" id="submitBtn">Войти</button>
    <div id="authErr"></div>
    <div id="authNote"></div>
  </form>
</div>

<!-- ============ ПРИЛОЖЕНИЕ ============ -->
<div id="app">
  <aside id="side">
    <div class="side-head">
      <div class="avatar" id="myAvatar">?</div>
      <div class="me-info">
        <div>
          <div class="me-login" id="myLogin">—</div>
          <div class="me-status">online</div>
        </div>
      </div>
      <button id="logoutBtn" title="Выйти">⎋</button>
    </div>
    <div id="contacts"></div>
  </aside>

  <div id="chatPane">
    <div id="chatHead">
      <button id="menuBtn" aria-label="Меню">☰</button>
      <div class="peer-info">
        <div class="peer-name" id="peerName">—</div>
        <div class="peer-status" id="peerStatus"></div>
      </div>
    </div>

    <div id="msgs" class="hidden"></div>
    <div id="emptyState">
      <div class="big">💬</div>
      <div>Выберите контакт слева,<br>чтобы начать переписку</div>
    </div>

    <div id="composer">
      <input id="inp" placeholder="Сообщение…" autocomplete="off" autocapitalize="sentences">
      <button id="sendBtn" aria-label="Отправить">➤</button>
    </div>
  </div>
</div>

<div id="backdrop"></div>

<script>
(() => {
"use strict";

/* ================= ПРОТОКОЛ ================= */
const T = {REGISTER:1, AUTH:2, AUTH_OK:3, MSG:4, USERS:5, PING:6, PONG:7, ERROR:8};
const _enc = new TextEncoder();
const _dec = new TextDecoder();

function pack(type, obj){
  const p = _enc.encode(JSON.stringify(obj));
  const buf = new ArrayBuffer(5 + p.length);
  const dv  = new DataView(buf);
  dv.setUint8(0, type);
  dv.setUint32(1, p.length, false);
  new Uint8Array(buf, 5).set(p);
  return buf;
}
function unpack(buf){
  const dv   = new DataView(buf);
  const type = dv.getUint8(0);
  const len  = dv.getUint32(1, false);
  const obj  = JSON.parse(_dec.decode(new Uint8Array(buf, 5, len)));
  return [type, obj];
}

/* ================= CRYPTO HELPERS ================= */
function b64(u8){
  let s = "";
  for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
  return btoa(s);
}
function unb64(str){
  const bin = atob(str);
  const u = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return u;
}

async function genKeyPair(){
  return crypto.subtle.generateKey(
    {name:"ECDH", namedCurve:"P-256"}, true, ["deriveKey"]
  );
}
async function exportPubRaw(pub){
  const raw = await crypto.subtle.exportKey("raw", pub);
  return b64(new Uint8Array(raw));
}
async function exportPrivJWK(priv){
  return await crypto.subtle.exportKey("jwk", priv);
}
async function importPrivJWK(jwk){
  return crypto.subtle.importKey(
    "jwk", jwk,
    {name:"ECDH", namedCurve:"P-256"},
    true, ["deriveKey"]
  );
}
async function importPubRaw(b64str){
  const raw = unb64(b64str);
  return crypto.subtle.importKey(
    "raw", raw,
    {name:"ECDH", namedCurve:"P-256"},
    true, []
  );
}
async function deriveAesKey(privKey, pubKey){
  return crypto.subtle.deriveKey(
    {name:"ECDH", public: pubKey},
    privKey,
    {name:"AES-GCM", length: 256},
    false, ["encrypt","decrypt"]
  );
}
async function aesEncrypt(key, text){
  const iv  = crypto.getRandomValues(new Uint8Array(12));
  const buf = await crypto.subtle.encrypt(
    {name:"AES-GCM", iv}, key, _enc.encode(text)
  );
  return {ct: b64(new Uint8Array(buf)), iv: b64(iv)};
}
async function aesDecrypt(key, ctB64, ivB64){
  const pt = await crypto.subtle.decrypt(
    {name:"AES-GCM", iv: unb64(ivB64)},
    key, unb64(ctB64)
  );
  return _dec.decode(pt);
}

/* ================= СОСТОЯНИЕ КЛИЕНТА ================= */
let ws = null;
let me = null;               // мой логин
let myPrivKey = null;        // CryptoKey (ECDH)
let sessionPassword = null;  // только в памяти, для реконнекта
let usersList = [];          // [{login, pub, online}]
const convKeys = {};         // login -> AES-GCM CryptoKey
const threads  = {};         // login -> [{id, from, to, text, ts, broken?}]
const unread   = {};         // login -> count
const seenIds  = new Set();
let activePeer = null;
let reconnectAttempts = 0;
let heartbeatTimer = null;

const $ = id => document.getElementById(id);

/* ================= UI ================= */
function showLogin(){
  $("login").style.display = "flex";
  $("app").classList.remove("on");
  clearInterval(heartbeatTimer);
}
function showApp(){
  $("login").style.display = "none";
  $("app").classList.add("on");
}
function setErr(msg){ $("authErr").textContent = msg || ""; }
function setNote(msg){ $("authNote").textContent = msg || ""; }

function av(login){ return (login || "?")[0].toUpperCase(); }

function renderContacts(){
  const box = $("contacts");
  box.innerHTML = "";
  const others = usersList.filter(u => u.login !== me)
                          .sort((a,b) => (b.online - a.online) || a.login.localeCompare(b.login));
  if (!others.length){
    const d = document.createElement("div");
    d.className = "empty-list";
    d.textContent = "Пока никого нет. Зарегистрируйте второго пользователя в другом браузере.";
    box.appendChild(d);
    return;
  }
  for (const u of others){
    const d = document.createElement("div");
    d.className = "contact" + (u.login === activePeer ? " active" : "");
    d.onclick = () => selectPeer(u.login);

    const a = document.createElement("div");
    a.className = "avatar";
    a.textContent = av(u.login);

    const info = document.createElement("div");
    info.className = "info";
    const nm = document.createElement("div");
    nm.className = "name"; nm.textContent = u.login;
    const sb = document.createElement("div");
    sb.className = "sub";
    const t = threads[u.login];
    sb.textContent = t && t.length
      ? (t[t.length-1].text.slice(0, 40))
      : (u.online ? "online" : "offline");
    info.append(nm, sb);

    d.append(a, info);

    if (unread[u.login] > 0){
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = unread[u.login] > 99 ? "99+" : unread[u.login];
      d.appendChild(b);
    }
    box.appendChild(d);
  }
}

function selectPeer(login){
  activePeer = login;
  unread[login] = 0;
  $("peerName").textContent = login;
  const u = usersList.find(x => x.login === login);
  const st = $("peerStatus");
  st.textContent = u && u.online ? "online" : "offline";
  st.className = "peer-status" + (u && u.online ? " online" : "");
  $("msgs").classList.remove("hidden");
  $("emptyState").classList.add("hidden");
  $("composer").classList.add("on");
  renderContacts();
  renderThread();
  closeSidebar();
  setTimeout(() => $("inp").focus(), 40);
}

function fmtTime(ts){
  const d = new Date(ts);
  return d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
}
function fmtDay(ts){
  const d = new Date(ts);
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  return same ? "Сегодня" : d.toLocaleDateString();
}

function renderThread(){
  const box = $("msgs");
  box.innerHTML = "";
  if (!activePeer){ return; }
  const list = threads[activePeer] || [];
  let lastDay = "";
  for (const m of list){
    const day = fmtDay(m.ts);
    if (day !== lastDay){
      lastDay = day;
      const sep = document.createElement("div");
      sep.className = "day-sep";
      sep.textContent = day;
      box.appendChild(sep);
    }
    box.appendChild(buildMsgEl(m));
  }
  box.scrollTop = box.scrollHeight;
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
  const last = box.lastElementChild;
  const day = fmtDay(m.ts);
  if (!last || last.className !== "day-sep" && box.querySelectorAll(".day-sep").length){
    // проверим: если предыдущий разделитель не соответствует дню — добавим
  }
  const days = box.querySelectorAll(".day-sep");
  if (!days.length || days[days.length-1].textContent !== day){
    const sep = document.createElement("div");
    sep.className = "day-sep";
    sep.textContent = day;
    box.appendChild(sep);
  }
  const el = buildMsgEl(m);
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

/* ================= SIDEBAR (mobile) ================= */
function openSidebar(){ $("side").classList.add("open"); $("backdrop").classList.add("show"); }
function closeSidebar(){ $("side").classList.remove("open"); $("backdrop").classList.remove("show"); }

/* ================= CRYPTO: получение ключа диалога ================= */
async function getConvKey(peer){
  if (convKeys[peer]) return convKeys[peer];
  const u = usersList.find(x => x.login === peer);
  if (!u || !u.pub) return null;
  try {
    const pub = await importPubRaw(u.pub);
    const key = await deriveAesKey(myPrivKey, pub);
    convKeys[peer] = key;
    return key;
  } catch(e){
    console.warn("derive fail", peer, e);
    return null;
  }
}

/* ================= WS ================= */
function connect(){
  return new Promise((resolve, reject) => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.binaryType = "arraybuffer";

    let settled = false;
    const to = setTimeout(() => {
      if (!settled){ settled = true; try{ws.close();}catch(e){}; reject(new Error("Таймаут")); }
    }, 8000);

    ws.onopen = () => ws.send(pack(mode === "register"
      ? T.REGISTER
      : T.AUTH, authPayload));

    ws.onmessage = async ev => {
      let type, obj;
      try { [type, obj] = unpack(ev.data); } catch(e){ return; }

      if (!settled){
        if (type === T.AUTH_OK){
          settled = true; clearTimeout(to); resolve(obj); return;
        }
        if (type === T.ERROR){
          settled = true; clearTimeout(to);
          try{ ws.close(); }catch(e){}
          reject(new Error(obj.msg || "Ошибка")); return;
        }
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
    case T.USERS: usersList = obj.users || []; renderContacts(); break;
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
  if (!key){
    text = "⚠ Нет ключа для расшифровки";
    broken = true;
  } else {
    try { text = await aesDecrypt(key, m.ct, m.iv); }
    catch(e){ text = "⚠ Не удалось расшифровать"; broken = true; }
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

/* ================= ЛОГИКА АВТОРИЗАЦИИ ================= */
let mode = "login";
let authPayload = null;

async function doAuth(login, password){
  login = login.trim().toLowerCase();
  if (!login || !password){ setErr("Заполните все поля"); return; }
  if (!/^[a-z0-9._-]{3,24}$/.test(login)){ setErr("Логин 3–24 симв.: a-z 0-9 . _ -"); return; }
  if (mode === "register" && password.length < 6){ setErr("Пароль минимум 6 символов"); return; }

  setErr(""); setNote("");

  // Готовим/загружаем ключ
  const lsKey = "nexus_dm_key_" + login;
  if (mode === "register"){
    const kp = await genKeyPair();
    myPrivKey = kp.privateKey;
    const jwk = await exportPrivJWK(kp.privateKey);
    localStorage.setItem(lsKey, JSON.stringify(jwk));
    const pubRaw = await exportPubRaw(kp.publicKey);
    authPayload = {login, password, pub: pubRaw};
  } else {
    const saved = localStorage.getItem(lsKey);
    if (!saved){
      setNote("⚠ Приватный ключ не найден в этом браузере — старые сообщения не удастся расшифровать.");
    } else {
      try { myPrivKey = await importPrivJWK(JSON.parse(saved)); }
      catch(e){ setNote("⚠ Ключ повреждён — расшифровка недоступна."); }
    }
    authPayload = {login, password};
  }

  $("submitBtn").disabled = true;
  try {
    const ok = await connect();
    sessionPassword = password;
    me = ok.login;
    usersList = ok.users || [];
    // Загрузка истории
    await loadHistory(ok.history || []);
    // UI
    $("myLogin").textContent = "@" + me;
    $("myAvatar").textContent = av(me);
    showApp();
    renderContacts();
    // Автовыбор первого собеседника если он есть
    const others = usersList.filter(u => u.login !== me);
    if (others.length && !activePeer) selectPeer(others[0].login);
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
    if (!byPeer[peer]) byPeer[peer] = [];
    byPeer[peer].push(m);
    seenIds.add(m.id);
  }
  for (const peer of Object.keys(byPeer)){
    const key = await getConvKey(peer);
    const list = byPeer[peer].sort((a,b) => a.ts - b.ts);
    const out = [];
    for (const m of list){
      let text, broken = false;
      if (!key){ text = "⚠ Нет ключа"; broken = true; }
      else {
        try { text = await aesDecrypt(key, m.ct, m.iv); }
        catch(e){ text = "⚠ Не удалось расшифровать"; broken = true; }
      }
      out.push({id:m.id, from:m.from, to:m.to, text, ts:m.ts, broken});
    }
    threads[peer] = out;
  }
}

/* ================= ОТПРАВКА ================= */
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

  // Оптимистично отрисовываем
  const localMsg = {id, from: me, to: activePeer, text, ts: Date.now()};
  seenIds.add(id);
  if (!threads[activePeer]) threads[activePeer] = [];
  threads[activePeer].push(localMsg);
  appendMsg(localMsg);
  renderContacts();

  inp.value = "";
  inp.focus();

  ws.send(pack(T.MSG, {id, to: activePeer, ct, iv}));
}

/* ================= HEARTBEAT / RECONNECT ================= */
function startHeartbeat(){
  clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => {
    if (ws && ws.readyState === 1) ws.send(pack(T.PING, {t: Date.now()}));
  }, 25000);
}

async function onDisconnect(){
  clearInterval(heartbeatTimer);
  if (!sessionPassword) { showLogin(); return; }
  if (reconnectAttempts >= 5){
    alert("Соединение потеряно. Обновите страницу.");
    return;
  }
  reconnectAttempts++;
  const login = me;
  const pw = sessionPassword;
  // Пробуем переподключиться
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

/* ================= ПРИВЯЗКА UI ================= */
function setMode(m){
  mode = m;
  $("tabLogin").classList.toggle("active", m === "login");
  $("tabReg").classList.toggle("active", m === "register");
  $("submitBtn").textContent = m === "login" ? "Войти" : "Создать аккаунт";
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
  try{ ws && ws.close(); }catch(e){}
  location.reload();
};

$("sendBtn").onclick = () => sendMessage();
$("inp").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); sendMessage(); }
});

$("menuBtn").onclick    = openSidebar;
$("backdrop").onclick   = closeSidebar;

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
