import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response

app = FastAPI()

SECRET = secrets.token_bytes(64)
USERS: dict = {}
CHATS: dict = {}
SOCKETS: dict = {}
RATE: dict = defaultdict(list)


def rl(ip: str, limit: int = 40, window: float = 60.0):
    now = time.time()
    RATE[ip] = [t for t in RATE[ip] if now - t < window]
    if len(RATE[ip]) >= limit:
        raise HTTPException(429, "rate limit")
    RATE[ip].append(now)


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict) -> str:
    data = b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(SECRET, data.encode(), hashlib.sha256).digest()
    return data + "." + b64e(sig)


def unsign(token: str) -> Optional[dict]:
    try:
        data, sig = token.split(".")
        expected = hmac.new(SECRET, data.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, b64d(sig)):
            return None
        p = json.loads(b64d(data))
        if p.get("exp", 0) < time.time():
            return None
        return p
    except Exception:
        return None


def phash(pw: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 250_000)


def auth_user(req: Request) -> str:
    h = req.headers.get("authorization", "")
    if not h.startswith("Bearer "):
        raise HTTPException(401)
    p = unsign(h[7:])
    if not p or p.get("nick") not in USERS:
        raise HTTPException(401)
    return p["nick"]


@app.post("/api/auth")
def auth(request: Request, data: dict):
    rl(request.client.host, 15, 60)
    nick = (data.get("nickname") or "").strip()
    pw = data.get("password") or ""
    device = data.get("device") or ""
    remember = bool(data.get("remember"))

    if not (2 <= len(nick) <= 20) or not all(c.isalnum() or c in "_-" for c in nick):
        raise HTTPException(400, "invalid nickname")
    if not (6 <= len(pw) <= 128):
        raise HTTPException(400, "password must be 6-128 chars")
    if not (32 <= len(device) <= 128) or not all(c in "0123456789abcdef" for c in device):
        raise HTTPException(400, "invalid device")

    dh = hashlib.sha256(device.encode()).hexdigest()
    u = USERS.get(nick)
    if u is None:
        salt = secrets.token_bytes(16)
        USERS[nick] = {
            "salt": salt,
            "pw": phash(pw, salt),
            "dev": dh,
            "created": time.time(),
        }
    else:
        if not hmac.compare_digest(u["dev"], dh):
            raise HTTPException(403, "device mismatch")
        if not hmac.compare_digest(phash(pw, u["salt"]), u["pw"]):
            raise HTTPException(401, "wrong password")

    exp = time.time() + (30 * 86400 if remember else 12 * 3600)
    token = sign({"nick": nick, "dev": dh, "exp": exp})
    return {"token": token, "nickname": nick}


@app.get("/api/chats")
def my_chats(request: Request):
    nick = auth_user(request)
    out = []
    for cid, c in CHATS.items():
        if nick in c["members"]:
            last = c["messages"][-1]["text"][:80] if c["messages"] else ""
            out.append({"id": cid, "name": c["name"], "last": last})
    out.sort(key=lambda x: x["id"], reverse=True)
    return out


@app.post("/api/chats")
def create_chat(request: Request, data: dict):
    nick = auth_user(request)
    rl(request.client.host, 20, 60)
    name = (data.get("name") or "").strip()[:40] or "Без названия"
    cid = secrets.token_hex(4)
    while cid in CHATS:
        cid = secrets.token_hex(4)
    CHATS[cid] = {"name": name, "members": {nick}, "messages": []}
    return {"id": cid, "name": name}


@app.post("/api/chats/join")
def join_chat(request: Request, data: dict):
    nick = auth_user(request)
    rl(request.client.host, 20, 60)
    cid = (data.get("id") or "").strip().lower()
    if len(cid) != 8 or not all(c in "0123456789abcdef" for c in cid):
        raise HTTPException(400, "invalid id")
    if cid not in CHATS:
        raise HTTPException(404, "not found")
    CHATS[cid]["members"].add(nick)
    return {"id": cid, "name": CHATS[cid]["name"]}


@app.websocket("/ws/{cid}")
async def chat_ws(ws: WebSocket, cid: str, token: str = ""):
    p = unsign(token)
    if not p or cid not in CHATS or p.get("nick") not in USERS:
        await ws.close(code=4001)
        return
    nick = p["nick"]
    CHATS[cid]["members"].add(nick)
    await ws.accept()
    SOCKETS.setdefault(cid, {})[nick] = ws
    try:
        for m in CHATS[cid]["messages"][-200:]:
            await ws.send_json({"type": "msg", **m})
        while True:
            data = await ws.receive_json()
            text = (data.get("text") or "").strip()[:2000]
            if not text:
                continue
            msg = {"nick": nick, "text": text, "ts": int(time.time() * 1000)}
            CHATS[cid]["messages"].append(msg)
            for n, sock in list(SOCKETS.get(cid, {}).items()):
                try:
                    await sock.send_json({"type": "msg", **msg})
                except Exception:
                    SOCKETS.get(cid, {}).pop(n, None)
    except WebSocketDisconnect:
        pass
    finally:
        if SOCKETS.get(cid, {}).get(nick) is ws:
            SOCKETS[cid].pop(nick, None)


@app.get("/sw.js")
def sw():
    return Response(SW, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/", response_class=HTMLResponse)
def root():
    nonce = secrets.token_urlsafe(16)
    csp = (
        f"default-src 'self'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'self' 'unsafe-inline'; "
        f"connect-src 'self' ws: wss:; "
        f"img-src 'self' data:; "
        f"font-src 'self'; "
        f"object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    return HTMLResponse(
        PAGE.replace("{{NONCE}}", nonce),
        headers={
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
            "Cross-Origin-Opener-Policy": "same-origin",
        },
    )


SW = r"""
const CACHE = 'chat-shell-v1';
self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(['/'])));
});
self.addEventListener('activate', e => {
  e.waitUntil(self.clients.claim());
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.startsWith('/api') || u.pathname.startsWith('/ws')) return;
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok && u.origin === location.origin) {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return r;
    }).catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});
"""


PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#141218">
<meta name="color-scheme" content="dark">
<title>Чаты</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23D0BCFF' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'/%3E%3C/svg%3E">
<style>
:root{
  --primary:#D0BCFF;--on-primary:#381E72;
  --primary-c:#4F378B;--on-primary-c:#EADDFF;
  --surface:#141218;--surface-c-lowest:#0F0D13;--surface-c-low:#1D1B20;
  --surface-c:#211F26;--surface-c-high:#2B2930;--surface-c-highest:#36343B;
  --on-surface:#E6E0E9;--on-surface-v:#CAC4D0;
  --outline:#938F99;--outline-v:#49454F;
  --error:#F2B8B5;--on-error:#601410;--error-c:#8C1D18;
  --scrim:rgba(0,0,0,.6);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overscroll-behavior:none}
body{
  font-family:'Roboto',system-ui,-apple-system,'Segoe UI',sans-serif;
  background:var(--surface);color:var(--on-surface);
  overflow:hidden;font-size:14px;
}
button{font-family:inherit;cursor:pointer;border:none;background:none;color:inherit}
input{font-family:inherit}
.hidden{display:none!important}
svg{display:block;flex-shrink:0}

/* login */
#loginScreen{
  position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  padding:24px;overflow-y:auto;background:var(--surface);
  background-image:radial-gradient(ellipse at top,rgba(208,188,255,.08),transparent 60%);
}
.login-card{
  width:100%;max-width:400px;display:flex;flex-direction:column;gap:20px;
  background:var(--surface-c-low);padding:32px 28px;border-radius:28px;
  border:1px solid var(--outline-v);
  box-shadow:0 8px 32px rgba(0,0,0,.4);
}
.brand{display:flex;align-items:center;gap:12px;justify-content:center;margin-bottom:8px}
.brand svg{width:36px;height:36px;color:var(--primary)}
.brand h1{font-size:28px;font-weight:400;letter-spacing:.3px;color:var(--primary)}
.tf{position:relative}
.tf input{
  width:100%;padding:16px;border:1px solid var(--outline);
  border-radius:8px;background:transparent;font-size:16px;
  color:var(--on-surface);outline:none;
  transition:border-color .15s,border-width .15s;
}
.tf input:focus{border:2px solid var(--primary);padding:15px}
.tf label{
  position:absolute;left:12px;top:50%;transform:translateY(-50%);
  background:var(--surface-c-low);padding:0 6px;font-size:16px;
  color:var(--on-surface-v);pointer-events:none;transition:.15s;
}
.tf input:focus+label,
.tf input:not(:placeholder-shown)+label{top:0;font-size:12px;color:var(--primary)}
.checkbox{
  display:flex;align-items:center;gap:12px;cursor:pointer;user-select:none;
  font-size:14px;color:var(--on-surface);
}
.checkbox input{position:absolute;opacity:0;width:0;height:0}
.checkbox-box{
  width:20px;height:20px;border-radius:4px;border:2px solid var(--on-surface-v);
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  transition:background .15s,border-color .15s;
}
.checkbox-box svg{width:14px;height:14px;color:var(--on-primary);opacity:0;transition:opacity .1s}
.checkbox input:checked+.checkbox-box{background:var(--primary);border-color:var(--primary)}
.checkbox input:checked+.checkbox-box svg{opacity:1}
.checkbox input:focus-visible+.checkbox-box{outline:2px solid var(--primary);outline-offset:2px}
.btn-filled{
  display:flex;align-items:center;justify-content:center;gap:10px;
  padding:16px 24px;border-radius:100px;
  background:var(--primary);color:var(--on-primary);
  font-size:15px;font-weight:500;letter-spacing:.1px;
  transition:box-shadow .15s,opacity .15s,background .15s;
}
.btn-filled:hover{box-shadow:0 2px 8px rgba(208,188,255,.35)}
.btn-filled:active{opacity:.85}
.btn-filled svg{width:18px;height:18px}
.btn-filled:disabled{opacity:.5;cursor:not-allowed}
.login-error{
  background:var(--error-c);color:#FFDAD6;padding:12px 16px;
  border-radius:12px;font-size:13px;line-height:1.4;
}
.login-note{font-size:12px;color:var(--on-surface-v);text-align:center;line-height:1.5}

/* app */
#appScreen{display:flex;height:100vh;height:100dvh;width:100vw;overflow:hidden}
.sidebar{
  width:380px;min-width:320px;flex-shrink:0;
  display:flex;flex-direction:column;
  background:var(--surface-c-low);
  border-right:1px solid var(--outline-v);
  position:relative;
}
.sidebar-head{
  display:flex;align-items:center;justify-content:space-between;
  padding:16px 20px;gap:12px;
  padding-top:calc(16px + env(safe-area-inset-top));
}
.brand-mini{display:flex;align-items:center;gap:10px;font-size:22px;font-weight:400;color:var(--primary)}
.brand-mini svg{width:24px;height:24px}
.head-actions{display:flex;align-items:center;gap:4px}
.icon-btn{
  width:40px;height:40px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  color:var(--on-surface-v);transition:background .15s;
}
.icon-btn:hover{background:var(--surface-c-highest)}
.icon-btn svg{width:20px;height:20px}
.user-avatar{
  width:36px;height:36px;border-radius:50%;margin-left:4px;
  background:var(--primary-c);color:var(--on-primary-c);
  display:flex;align-items:center;justify-content:center;
  font-size:14px;font-weight:500;
}
.chat-list{flex:1;overflow-y:auto;padding:8px 8px 96px;scrollbar-width:thin}
.chat-list::-webkit-scrollbar{width:8px}
.chat-list::-webkit-scrollbar-thumb{background:var(--outline-v);border-radius:4px}
.chat-item{
  display:flex;align-items:center;gap:14px;
  padding:12px 14px;border-radius:16px;cursor:pointer;
  transition:background .15s;margin-bottom:2px;
}
.chat-item:hover{background:var(--surface-c)}
.chat-item.active{background:var(--primary-c)}
.chat-item.active .chat-name{color:var(--on-primary-c)}
.chat-item.active .chat-last{color:var(--on-primary-c);opacity:.75}
.chat-avatar{
  width:44px;height:44px;border-radius:50%;flex-shrink:0;
  background:var(--primary-c);color:var(--on-primary-c);
  display:flex;align-items:center;justify-content:center;
  font-size:17px;font-weight:500;
}
.chat-info{flex:1;min-width:0}
.chat-name{
  font-size:15px;font-weight:500;margin-bottom:3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.chat-last{
  font-size:13px;color:var(--on-surface-v);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.empty-list{
  text-align:center;color:var(--on-surface-v);
  padding:56px 24px;font-size:14px;line-height:1.6;
}
.empty-list svg{width:56px;height:56px;margin:0 auto 16px;opacity:.4}
.fab{
  position:absolute;right:20px;
  bottom:calc(20px + env(safe-area-inset-bottom));
  width:56px;height:56px;border-radius:18px;
  background:var(--primary-c);color:var(--on-primary-c);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 4px 14px rgba(0,0,0,.5);
  transition:box-shadow .15s,transform .15s,background .15s;
}
.fab svg{width:24px;height:24px}
.fab:hover{background:#5C44A0;box-shadow:0 6px 20px rgba(0,0,0,.6)}
.fab:active{transform:scale(.94)}

/* chat area */
.chat-area{
  flex:1;display:flex;flex-direction:column;
  background:var(--surface);min-width:0;position:relative;
}
.empty-state{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:16px;color:var(--on-surface-v);text-align:center;padding:24px;
}
.empty-state svg{width:80px;height:80px;opacity:.35}
.empty-state p{font-size:15px;line-height:1.6}
.room-view{display:flex;flex-direction:column;height:100%;min-height:0}
.room-head{
  display:flex;align-items:center;gap:8px;
  padding:12px 16px;min-height:64px;
  padding-top:calc(12px + env(safe-area-inset-top));
  border-bottom:1px solid var(--outline-v);
  background:var(--surface-c-low);flex-shrink:0;
}
.room-title{flex:1;min-width:0}
.room-title h2{
  font-size:17px;font-weight:500;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;
}
.room-id{font-size:12px;color:var(--on-surface-v);margin-top:2px;font-family:ui-monospace,monospace}
.messages{
  flex:1;overflow-y:auto;padding:16px;
  display:flex;flex-direction:column;gap:6px;
  scrollbar-width:thin;
}
.messages::-webkit-scrollbar{width:8px}
.messages::-webkit-scrollbar-thumb{background:var(--outline-v);border-radius:4px}
.msg{
  max-width:min(72%,560px);padding:10px 14px;border-radius:18px;
  font-size:14px;line-height:1.4;word-wrap:break-word;
  white-space:pre-wrap;overflow-wrap:anywhere;
}
.msg.own{
  align-self:flex-end;background:var(--primary);color:var(--on-primary);
  border-bottom-right-radius:6px;
}
.msg.other{
  align-self:flex-start;background:var(--surface-c-high);
  border-bottom-left-radius:6px;
}
.msg .nick{
  font-size:12px;font-weight:500;color:var(--primary);
  margin-bottom:3px;display:block;
}
.msg-form{
  display:flex;gap:10px;padding:12px 16px;
  padding-bottom:calc(12px + env(safe-area-inset-bottom));
  background:var(--surface-c-low);flex-shrink:0;
  border-top:1px solid var(--outline-v);
}
.msg-form input{
  flex:1;border:none;background:var(--surface-c-high);
  border-radius:100px;padding:14px 20px;font-size:15px;
  outline:none;color:var(--on-surface);
  transition:background .15s,box-shadow .15s;
}
.msg-form input:focus{background:var(--surface-c-highest);box-shadow:0 0 0 1px var(--primary)}
.send-btn{
  width:48px;height:48px;border-radius:50%;flex-shrink:0;
  background:var(--primary);color:var(--on-primary);
  display:flex;align-items:center;justify-content:center;
  transition:box-shadow .15s,opacity .15s;
}
.send-btn:hover{box-shadow:0 2px 8px rgba(208,188,255,.4)}
.send-btn:active{opacity:.85}
.send-btn svg{width:20px;height:20px}

/* dialog */
.backdrop{
  position:fixed;inset:0;background:var(--scrim);
  display:flex;align-items:center;justify-content:center;
  padding:24px;z-index:200;backdrop-filter:blur(4px);
}
.dialog{
  background:var(--surface-c-high);border-radius:28px;padding:24px;
  width:100%;max-width:440px;max-height:90vh;overflow-y:auto;
  display:flex;flex-direction:column;gap:16px;
  border:1px solid var(--outline-v);
  box-shadow:0 8px 32px rgba(0,0,0,.5);
}
.dialog h3{font-size:20px;font-weight:400;margin-bottom:4px}
.dialog .tf input{background:var(--surface-c-low)}
.dialog .tf label{background:var(--surface-c-high)}
.dialog .tf input:focus+label,
.dialog .tf input:not(:placeholder-shown)+label{background:var(--surface-c-high)}
.dialog .btn-tonal{
  display:flex;align-items:center;justify-content:center;gap:8px;
  padding:14px 20px;border-radius:100px;
  background:var(--primary-c);color:var(--on-primary-c);
  font-size:14px;font-weight:500;transition:background .15s;
}
.dialog .btn-tonal:hover{background:#5C44A0}
.dialog .divider{height:1px;background:var(--outline-v);margin:4px 0}
.dialog .row{display:flex;justify-content:flex-end;gap:8px}
.btn-text{
  padding:12px 20px;border-radius:100px;color:var(--primary);
  font-size:14px;font-weight:500;transition:background .15s;
}
.btn-text:hover{background:rgba(208,188,255,.08)}

/* responsive */
@media (min-width:721px){
  .back-btn{display:none!important}
  .empty-state{display:flex!important}
  .room-view{display:flex!important}
}
@media (max-width:720px){
  .sidebar{
    position:absolute;inset:0;width:100%;min-width:0;
    border-right:none;z-index:5;
  }
  .chat-area{position:absolute;inset:0;z-index:6;background:var(--surface)}
  body:not(.in-room) .chat-area{display:none}
  body.in-room .sidebar{display:none}
  .chat-list{padding:8px 8px 96px}
  .fab{right:16px}
  .dialog{border-radius:24px;padding:20px}
  .msg{max-width:85%}
  .login-card{padding:24px 20px;border-radius:24px}
}
</style>
</head>
<body>

<div id="loginScreen">
  <div class="login-card">
    <div class="brand">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
      <h1>Чаты</h1>
    </div>
    <div class="tf">
      <input id="nickInput" maxlength="20" autocomplete="username" placeholder=" " spellcheck="false">
      <label for="nickInput">Ник</label>
    </div>
    <div class="tf">
      <input id="pwInput" type="password" maxlength="128" autocomplete="current-password" placeholder=" ">
      <label for="pwInput">Пароль</label>
    </div>
    <label class="checkbox">
      <input type="checkbox" id="rememberInput">
      <span class="checkbox-box">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
      </span>
      <span>Запомнить меня</span>
    </label>
    <button id="loginBtn" class="btn-filled">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>
        <polyline points="10 17 15 12 10 7"/>
        <line x1="15" y1="12" x2="3" y2="12"/>
      </svg>
      <span>Войти</span>
    </button>
    <div id="loginError" class="login-error hidden"></div>
    <div class="login-note">Ник привязывается к этому устройству.<br>Пароль нельзя восстановить.</div>
  </div>
</div>

<div id="appScreen" class="hidden">
  <aside class="sidebar">
    <header class="sidebar-head">
      <div class="brand-mini">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
        </svg>
        <span>Чаты</span>
      </div>
      <div class="head-actions">
        <button class="icon-btn" id="logoutBtn" title="Выйти" aria-label="Выйти">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
            <polyline points="16 17 21 12 16 7"/>
            <line x1="21" y1="12" x2="9" y2="12"/>
          </svg>
        </button>
        <div class="user-avatar" id="userAvatar">?</div>
      </div>
    </header>
    <div id="chatList" class="chat-list"></div>
    <button class="fab" id="fab" title="Создать или найти" aria-label="Создать или найти">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="12" y1="5" x2="12" y2="19"/>
        <line x1="5" y1="12" x2="19" y2="12"/>
      </svg>
    </button>
  </aside>

  <main class="chat-area">
    <div id="emptyState" class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
      <p>Выберите чат слева<br>или создайте новый</p>
    </div>
    <div id="roomView" class="room-view hidden">
      <header class="room-head">
        <button class="icon-btn back-btn" id="backBtn" aria-label="Назад">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="19" y1="12" x2="5" y2="12"/>
            <polyline points="12 19 5 12 12 5"/>
          </svg>
        </button>
        <div class="room-title">
          <h2 id="roomName"></h2>
          <div class="room-id" id="roomId"></div>
        </div>
        <button class="icon-btn" id="copyIdBtn" title="Скопировать ID" aria-label="Скопировать ID">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
          </svg>
        </button>
      </header>
      <div id="messages" class="messages"></div>
      <form id="msgForm" class="msg-form" autocomplete="off">
        <input id="msgInput" placeholder="Сообщение..." maxlength="2000" autocomplete="off">
        <button type="submit" class="send-btn" aria-label="Отправить">
          <svg viewBox="0 0 24 24" fill="currentColor">
            <path d="M2 21l21-9L2 3v7l15 2-15 2z"/>
          </svg>
        </button>
      </form>
    </div>
  </main>
</div>

<div id="dialog" class="backdrop hidden">
  <div class="dialog">
    <h3>Новый чат</h3>
    <div class="tf">
      <input id="chatName" placeholder=" " maxlength="40">
      <label for="chatName">Название</label>
    </div>
    <button class="btn-tonal" id="createBtn">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
      </svg>
      Создать
    </button>
    <div class="divider"></div>
    <h3>Найти чат по ID</h3>
    <div class="tf">
      <input id="findId" placeholder=" " maxlength="8" spellcheck="false" autocomplete="off">
      <label for="findId">ID чата (8 символов)</label>
    </div>
    <button class="btn-tonal" id="findBtn">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      Войти
    </button>
    <div class="row"><button class="btn-text" id="closeDialog">Закрыть</button></div>
  </div>
</div>

<script nonce="{{NONCE}}">
'use strict';
const $ = s => document.querySelector(s);
const app = { token:null, nick:null, device:null, chat:null, ws:null };

async function computeDevice() {
  const parts = [
    navigator.userAgent || '',
    navigator.language || '',
    (navigator.languages || []).join(','),
    screen.width + 'x' + screen.height + 'x' + screen.colorDepth,
    new Date().getTimezoneOffset(),
    navigator.hardwareConcurrency || 0,
    navigator.maxTouchPoints || 0,
    navigator.platform || '',
    '|v1'
  ].join('|');
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(parts));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2,'0')).join('');
}

async function api(path, method, body) {
  const headers = { 'Content-Type':'application/json' };
  if (app.token) headers['Authorization'] = 'Bearer ' + app.token;
  const r = await fetch(path, {
    method: method || 'GET',
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let msg = 'Ошибка ' + r.status;
    try { const j = await r.json(); if (j.detail) msg = j.detail; } catch(_){}
    throw new Error(msg);
  }
  return r.json();
}

function setToken(t, remember) {
  app.token = t;
  if (!t) {
    localStorage.removeItem('token');
    sessionStorage.removeItem('token');
    return;
  }
  if (remember) localStorage.setItem('token', t);
  else sessionStorage.setItem('token', t);
}
function getStoredToken() {
  return sessionStorage.getItem('token') || localStorage.getItem('token');
}

function showLogin() {
  $('#loginScreen').classList.remove('hidden');
  $('#appScreen').classList.add('hidden');
  document.body.classList.remove('in-room');
  setTimeout(() => $('#nickInput').focus(), 50);
}
function showApp() {
  $('#loginScreen').classList.add('hidden');
  $('#appScreen').classList.remove('hidden');
}

async function doLogin() {
  const nick = $('#nickInput').value.trim();
  const pw = $('#pwInput').value;
  const remember = $('#rememberInput').checked;
  const errEl = $('#loginError');
  errEl.classList.add('hidden');
  if (!nick || !pw) { errEl.textContent = 'Заполните все поля'; errEl.classList.remove('hidden'); return; }
  const btn = $('#loginBtn');
  btn.disabled = true;
  try {
    if (!app.device) app.device = await computeDevice();
    const r = await api('/api/auth', 'POST', {
      nickname: nick, password: pw, device: app.device, remember
    });
    setToken(r.token, remember);
    app.nick = r.nickname;
    startApp();
  } catch(e) {
    let msg = e.message;
    if (msg.includes('device mismatch')) msg = 'Этот ник привязан к другому устройству';
    else if (msg.includes('wrong password')) msg = 'Неверный пароль';
    else if (msg.includes('invalid nickname')) msg = 'Ник: 2-20 символов, буквы/цифры/_/-';
    else if (msg.includes('password must')) msg = 'Пароль: от 6 до 128 символов';
    else if (msg.includes('rate limit')) msg = 'Слишком много попыток, подождите';
    errEl.textContent = msg;
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
  }
}

function startApp() {
  showApp();
  const ch = (app.nick[0] || '?').toUpperCase();
  $('#userAvatar').textContent = ch;
  loadChats();
}

async function loadChats() {
  try {
    const list = await api('/api/chats');
    renderChats(list);
  } catch(e) {
    if (e.message.includes('401')) { logout(); }
  }
}

function renderChats(list) {
  const el = $('#chatList');
  el.innerHTML = '';
  if (!list.length) {
    const d = document.createElement('div');
    d.className = 'empty-list';
    d.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg><div>Пока нет чатов.<br>Нажмите + чтобы создать.</div>';
    el.appendChild(d);
    return;
  }
  for (const c of list) {
    const item = document.createElement('div');
    item.className = 'chat-item';
    if (app.chat && app.chat.id === c.id) item.classList.add('active');
    const av = document.createElement('div');
    av.className = 'chat-avatar';
    av.textContent = (c.name[0] || '?').toUpperCase();
    const info = document.createElement('div');
    info.className = 'chat-info';
    const nm = document.createElement('div');
    nm.className = 'chat-name';
    nm.textContent = c.name;
    const lt = document.createElement('div');
    lt.className = 'chat-last';
    lt.textContent = c.last || 'Нет сообщений';
    info.appendChild(nm);
    info.appendChild(lt);
    item.appendChild(av);
    item.appendChild(info);
    item.onclick = () => openRoom(c);
    item.dataset.id = c.id;
    el.appendChild(item);
  }
}

async function openRoom(chat) {
  app.chat = chat;
  document.querySelectorAll('.chat-item').forEach(e => {
    e.classList.toggle('active', e.dataset.id === chat.id);
  });
  $('#roomName').textContent = chat.name;
  $('#roomId').textContent = '#' + chat.id;
  $('#messages').innerHTML = '';
  $('#emptyState').classList.add('hidden');
  $('#roomView').classList.remove('hidden');
  document.body.classList.add('in-room');
  if (app.ws) { try { app.ws.close(); } catch(_){} app.ws = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = proto + '://' + location.host + '/ws/' + chat.id + '?token=' + encodeURIComponent(app.token);
  const ws = new WebSocket(url);
  app.ws = ws;
  ws.onmessage = e => {
    try {
      const d = JSON.parse(e.data);
      if (d.type === 'msg') addMessage(d);
    } catch(_){}
  };
  ws.onclose = ev => {
    if (app.ws === ws && ev.code !== 1000 && ev.code !== 4001 && app.chat) {
      setTimeout(() => { if (app.ws === ws && app.chat) openRoom(app.chat); }, 1500);
    }
  };
  setTimeout(() => $('#msgInput').focus(), 100);
}

function addMessage(d) {
  const box = $('#messages');
  const m = document.createElement('div');
  m.className = 'msg ' + (d.nick === app.nick ? 'own' : 'other');
  if (d.nick !== app.nick) {
    const n = document.createElement('span');
    n.className = 'nick';
    n.textContent = d.nick;
    m.appendChild(n);
  }
  const t = document.createTextNode(d.text);
  m.appendChild(t);
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  box.appendChild(m);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function closeRoom() {
  if (app.ws) { try { app.ws.close(); } catch(_){} app.ws = null; }
  app.chat = null;
  document.body.classList.remove('in-room');
  $('#emptyState').classList.remove('hidden');
  $('#roomView').classList.add('hidden');
  document.querySelectorAll('.chat-item').forEach(e => e.classList.remove('active'));
  loadChats();
}

function logout() {
  if (app.ws) { try { app.ws.close(); } catch(_){} app.ws = null; }
  setToken(null);
  app.nick = null;
  app.chat = null;
  document.body.classList.remove('in-room');
  $('#nickInput').value = '';
  $('#pwInput').value = '';
  $('#rememberInput').checked = false;
  showLogin();
}

$('#loginBtn').onclick = doLogin;
$('#nickInput').addEventListener('keydown', e => { if (e.key === 'Enter') $('#pwInput').focus(); });
$('#pwInput').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
$('#logoutBtn').onclick = logout;
$('#backBtn').onclick = closeRoom;

$('#fab').onclick = () => {
  $('#dialog').classList.remove('hidden');
  setTimeout(() => $('#chatName').focus(), 50);
};
$('#closeDialog').onclick = () => $('#dialog').classList.add('hidden');
$('#dialog').onclick = e => { if (e.target.id === 'dialog') $('#dialog').classList.add('hidden'); };

$('#createBtn').onclick = async () => {
  const name = $('#chatName').value.trim();
  if (!name) return;
  try {
    const c = await api('/api/chats', 'POST', { name });
    $('#chatName').value = '';
    $('#dialog').classList.add('hidden');
    await loadChats();
    openRoom(c);
  } catch(e) { alert(e.message); }
};

$('#findBtn').onclick = async () => {
  const id = $('#findId').value.trim().toLowerCase();
  if (!id) return;
  try {
    const c = await api('/api/chats/join', 'POST', { id });
    $('#findId').value = '';
    $('#dialog').classList.add('hidden');
    await loadChats();
    openRoom(c);
  } catch(e) {
    alert(e.message.includes('404') ? 'Чат не найден' : e.message);
  }
};

$('#copyIdBtn').onclick = async () => {
  if (!app.chat) return;
  try {
    await navigator.clipboard.writeText(app.chat.id);
    const btn = $('#copyIdBtn');
    const old = btn.innerHTML;
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
    setTimeout(() => { btn.innerHTML = old; }, 1000);
  } catch(_){}
};

$('#msgForm').onsubmit = e => {
  e.preventDefault();
  const v = $('#msgInput').value.trim();
  if (!v || !app.ws || app.ws.readyState !== 1) return;
  app.ws.send(JSON.stringify({ text: v }));
  $('#msgInput').value = '';
  $('#msgInput').focus();
};

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (!$('#dialog').classList.contains('hidden')) $('#dialog').classList.add('hidden');
  }
});

(async function init() {
  if ('serviceWorker' in navigator) {
    try { await navigator.serviceWorker.register('/sw.js'); } catch(_){}
  }
  app.device = await computeDevice();
  const t = getStoredToken();
  if (t) {
    app.token = t;
    try {
      const list = await api('/api/chats');
      const payload = JSON.parse(atob(t.split('.')[0] + '='.repeat((-t.split('.')[0].length) % 4)));
      app.nick = payload.nick;
      showApp();
      $('#userAvatar').textContent = (app.nick[0] || '?').toUpperCase();
      renderChats(list);
      return;
    } catch(_) {
      setToken(null);
    }
  }
  showLogin();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
