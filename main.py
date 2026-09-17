import os
import time
import secrets
import hashlib
from typing import Dict, List, Any

import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

SERVER_NAME = os.environ.get("SLDCHAT_DOMAIN", "localhost:8000")

app = FastAPI(title="SLDCHAT", version="0.2")

# ---------------- RAM STORAGE ----------------
USERS: Dict[str, Dict[str, str]] = {}     # username -> {salt, pw_hash, token}
MESSAGES: List[Dict[str, Any]] = []       # ВСЕ сообщения, что видел этот сервер


# ---------------- Models ----------------
class RegisterReq(BaseModel):
    username: str
    password: str


class LoginReq(BaseModel):
    username: str
    password: str


class SendReq(BaseModel):
    to: str
    body: str
    subject: str = ""


class FederationMsg(BaseModel):
    msg_id: str
    from_addr: str
    to_addr: str
    subject: str = ""
    body: str
    ts: float


# ---------------- Utils ----------------
def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def auth(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization[7:].strip()
    for u, data in USERS.items():
        if secrets.compare_digest(data.get("token", ""), token):
            return u
    raise HTTPException(401, "Invalid token")


def full(u: str) -> str:
    return f"{u}@{SERVER_NAME}"


# ---------------- Discovery ----------------
@app.get("/.well-known/sldchat")
async def well_known():
    return {"protocol": "sldchat", "version": 1, "server": SERVER_NAME}


# ---------------- Federation ----------------
@app.post("/federation/receive")
async def federation_receive(msg: FederationMsg):
    to_user, _, to_domain = msg.to_addr.rpartition("@")
    if to_domain != SERVER_NAME:
        raise HTTPException(400, "Wrong server")
    if to_user not in USERS:
        # сервер чужой, пользователя нет — просто игнорируем или 404
        raise HTTPException(404, f"No such user: {to_user}")
    MESSAGES.append(msg.model_dump())
    return {"status": "ok"}


async def federate(to_domain: str, payload: dict) -> tuple[bool, str]:
    """
    Важно: follow_redirects=False + ручной обход,
    чтобы 301/302 не превратили POST в GET (иначе ловим 405).
    """
    last_err = ""
    for scheme in ("https", "http"):
        url = f"{scheme}://{to_domain}/federation/receive"
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                current = url
                r = None
                for _ in range(6):
                    r = await client.post(current, json=payload)
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("location")
                        if not loc:
                            break
                        current = str(httpx.URL(current).join(loc))
                        continue
                    break
            if r is None:
                last_err = "no response"
                continue
            if r.status_code < 400:
                return True, ""
            last_err = f"{r.status_code} {r.text[:300]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, last_err


# ---------------- User API ----------------
@app.post("/api/register")
async def register(req: RegisterReq):
    u = req.username.strip().lower()
    if not u or not req.password:
        raise HTTPException(400, "username/password required")
    if not u.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "Bad username")
    if u in USERS:
        raise HTTPException(409, "User exists")
    salt = secrets.token_hex(8)
    USERS[u] = {
        "salt": salt,
        "pw_hash": hash_pw(req.password, salt),
        "token": secrets.token_urlsafe(24),
    }
    return {"status": "ok", "address": full(u), "server": SERVER_NAME}


@app.post("/api/login")
async def login(req: LoginReq):
    u = req.username.strip().lower()
    data = USERS.get(u)
    if not data or data["pw_hash"] != hash_pw(req.password, data["salt"]):
        raise HTTPException(401, "Bad credentials")
    data["token"] = secrets.token_urlsafe(24)
    return {"status": "ok", "token": data["token"], "username": u, "server": SERVER_NAME}


@app.get("/api/me")
async def me(authorization: str | None = Header(None)):
    u = auth(authorization)
    return {"username": u, "server": SERVER_NAME, "address": full(u)}


@app.get("/api/users")
async def users():
    """Локальные пользователи этого сервера."""
    return {"users": [full(u) for u in USERS]}


@app.get("/api/conversations")
async def conversations(authorization: str | None = Header(None)):
    u = auth(authorization)
    me_addr = full(u)
    peers: Dict[str, dict] = {}
    for m in MESSAGES:
        if m["from_addr"] == me_addr:
            peer = m["to_addr"]
        elif m["to_addr"] == me_addr:
            peer = m["from_addr"]
        else:
            continue
        prev = peers.get(peer)
        if not prev or m["ts"] > prev["last_ts"]:
            peers[peer] = {
                "peer": peer,
                "last_ts": m["ts"],
                "preview": (m.get("body") or "")[:80],
                "last_from": m["from_addr"],
            }
    return {"conversations": sorted(peers.values(), key=lambda x: -x["last_ts"])}


@app.get("/api/messages")
async def messages(peer: str = Query(...), authorization: str | None = Header(None)):
    u = auth(authorization)
    me_addr = full(u)
    result = [
        m for m in MESSAGES
        if (m["from_addr"] == me_addr and m["to_addr"] == peer)
        or (m["from_addr"] == peer and m["to_addr"] == me_addr)
    ]
    result.sort(key=lambda x: x["ts"])
    return {"messages": result}


@app.post("/api/send")
async def send(req: SendReq, authorization: str | None = Header(None)):
    u = auth(authorization)
    to = req.to.strip().lower()
    if "@" not in to:
        raise HTTPException(400, "Address must be user@domain")
    to_user, to_domain = to.rsplit("@", 1)

    from_addr = full(u)
    msg = {
        "msg_id": secrets.token_hex(16),
        "from_addr": from_addr,
        "to_addr": to,
        "subject": req.subject,
        "body": req.body,
        "ts": time.time(),
    }

    if to_domain == SERVER_NAME:
        if to_user not in USERS:
            raise HTTPException(404, f"Нет пользователя {to_user} на этом сервере")
        MESSAGES.append(msg)
        return {"status": "delivered", "local": True}

    ok, err = await federate(to_domain, msg)
    if not ok:
        raise HTTPException(502, f"Federation to {to_domain} failed: {err}")
    # сохраняем и у себя, чтобы видеть историю "исходящих"
    MESSAGES.append(msg)
    return {"status": "delivered", "local": False, "via": to_domain}


# ---------------- UI ----------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SLDCHAT</title>
<style>
:root{
  --bg:#12141a; --bg2:#181b23; --bg3:#1f232d;
  --fg:#e6e8ee; --fg2:#9aa0ae; --line:#262b36;
  --accent:#7c5cff; --accent2:#5a44d6;
  --bubble-me:#2a2f42; --bubble-them:#1f232d;
  --ok:#4ade80; --err:#f87171;
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,sans-serif;
  -webkit-font-smoothing:antialiased;
}
a{color:var(--accent)}

/* --- auth --- */
.auth-wrap{
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
}
.auth-card{
  width:100%;max-width:380px;background:var(--bg2);
  border:1px solid var(--line);border-radius:14px;padding:24px;
}
.auth-card h1{
  font-size:20px;margin:0 0 4px 0;letter-spacing:.5px;
}
.auth-card .sub{color:var(--fg2);margin-bottom:18px;font-size:12px}
.field{margin-bottom:10px}
.field label{display:block;font-size:12px;color:var(--fg2);margin-bottom:4px}
input,textarea{
  width:100%;background:var(--bg3);color:var(--fg);
  border:1px solid var(--line);border-radius:8px;
  padding:9px 11px;font:inherit;outline:none;
}
input:focus,textarea:focus{border-color:var(--accent)}
button{
  background:var(--accent);color:white;border:0;border-radius:8px;
  padding:9px 14px;font:inherit;font-weight:600;cursor:pointer;
}
button:hover{background:var(--accent2)}
button.ghost{background:transparent;color:var(--fg2);border:1px solid var(--line)}
button.ghost:hover{background:var(--bg3);color:var(--fg)}
.tabs{display:flex;gap:6px;margin-bottom:14px}
.tabs button{flex:1;background:var(--bg3);color:var(--fg2)}
.tabs button.active{background:var(--accent);color:#fff}
.msg-err{color:var(--err);font-size:12px;margin-top:8px;min-height:16px}
.msg-ok{color:var(--ok);font-size:12px;margin-top:8px}

/* --- app --- */
.app{display:grid;grid-template-columns:280px 1fr;height:100vh}
.sidebar{
  background:var(--bg2);border-right:1px solid var(--line);
  display:flex;flex-direction:column;min-height:0;
}
.me{
  padding:14px;border-bottom:1px solid var(--line);
  display:flex;justify-content:space-between;align-items:center;gap:8px;
}
.me .name{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.me .srv{color:var(--fg2);font-size:11px}
.side-section{padding:10px 14px 4px;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--fg2)}
.side-list{flex:1;overflow:auto;padding:4px 8px 8px}
.side-list .empty{color:var(--fg2);font-size:12px;padding:8px}
.conv{
  padding:9px 10px;border-radius:8px;cursor:pointer;margin-bottom:2px;
  display:flex;flex-direction:column;gap:2px;
}
.conv:hover{background:var(--bg3)}
.conv.active{background:var(--bg3)}
.conv .peer{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.conv .prev{color:var(--fg2);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.new-chat{padding:8px;border-top:1px solid var(--line);display:flex;gap:6px}
.new-chat input{flex:1}

.main{display:flex;flex-direction:column;min-height:0;background:var(--bg)}
.chat-head{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:10px}
.chat-head .who{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chat-head .status{color:var(--fg2);font-size:12px}
.messages{flex:1;overflow:auto;padding:18px;display:flex;flex-direction:column;gap:8px}
.messages .placeholder{color:var(--fg2);text-align:center;margin-top:40px}
.bubble{
  max-width:70%;padding:8px 12px;border-radius:14px;word-wrap:break-word;
  white-space:pre-wrap;font-size:14px;
}
.bubble .meta{font-size:11px;color:var(--fg2);margin-top:4px}
.bubble.me{align-self:flex-end;background:var(--bubble-me);border-bottom-right-radius:4px}
.bubble.them{align-self:flex-start;background:var(--bubble-them);border:1px solid var(--line);border-bottom-left-radius:4px}
.composer{border-top:1px solid var(--line);padding:12px;display:flex;gap:8px;align-items:flex-end}
.composer textarea{resize:none;min-height:44px;max-height:160px;font-family:inherit}
.composer button{height:44px;padding:0 18px}
.hidden{display:none !important}
</style>
</head>
<body>

<!-- AUTH -->
<div id="auth_view" class="auth-wrap">
  <div class="auth-card">
    <h1>SLDCHAT</h1>
    <div class="sub">federated messenger · server: <b>__SERVER__</b></div>

    <div class="tabs">
      <button id="tab_login" class="active" onclick="switchTab('login')">Вход</button>
      <button id="tab_reg" onclick="switchTab('reg')">Регистрация</button>
    </div>

    <div id="form_login">
      <div class="field"><label>Логин</label><input id="log_user" autocomplete="username"></div>
      <div class="field"><label>Пароль</label><input id="log_pass" type="password" autocomplete="current-password"></div>
      <button style="width:100%" onclick="doLogin()">Войти</button>
    </div>

    <div id="form_reg" class="hidden">
      <div class="field"><label>Логин</label><input id="reg_user" autocomplete="username"></div>
      <div class="field"><label>Пароль</label><input id="reg_pass" type="password" autocomplete="new-password"></div>
      <button style="width:100%" onclick="doRegister()">Создать аккаунт</button>
    </div>

    <div id="auth_status" class="msg-err"></div>
  </div>
</div>

<!-- APP -->
<div id="app_view" class="app hidden">
  <aside class="sidebar">
    <div class="me">
      <div>
        <div class="name" id="me_name">—</div>
        <div class="srv" id="me_srv">@__SERVER__</div>
      </div>
      <button class="ghost" onclick="doLogout()" title="Выйти">⎋</button>
    </div>
    <div class="side-section">Диалоги</div>
    <div class="side-list" id="conv_list"><div class="empty">Пока пусто</div></div>
    <div class="new-chat">
      <input id="new_peer" placeholder="user@domain">
      <button onclick="openNewChat()">→</button>
    </div>
  </aside>

  <main class="main">
    <div class="chat-head">
      <div class="who" id="peer_title">Выберите диалог</div>
      <div class="status" id="peer_status"></div>
    </div>
    <div class="messages" id="messages">
      <div class="placeholder">Выберите диалог слева или введите адрес внизу</div>
    </div>
    <div class="composer">
      <textarea id="composer" placeholder="Написать сообщение…" onkeydown="composerKey(event)"></textarea>
      <button onclick="doSend()">Отправить</button>
    </div>
  </main>
</div>

<script>
const SERVER_DOMAIN = "__SERVER__";
let token = localStorage.getItem('sldchat_token') || '';
let username = localStorage.getItem('sldchat_user') || '';
let currentPeer = null;
let pollTimer = null;

/* ---------- helpers ---------- */
async function api(path, opts={}){
  opts.headers = Object.assign({'Content-Type':'application/json'}, opts.headers||{});
  if (token) opts.headers['Authorization'] = 'Bearer ' + token;
  const r = await fetch(path, opts);
  const data = await r.json().catch(()=>({}));
  if (!r.ok) {
    const err = new Error(data.detail || r.statusText);
    err.status = r.status;
    if (r.status === 401) doLogout();
    throw err;
  }
  return data;
}
function esc(s){
  return (s||'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtTime(ts){
  const d = new Date(ts*1000);
  return d.toLocaleString([], {hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'});
}
function setAuthStatus(t, ok){
  const el = document.getElementById('auth_status');
  el.textContent = t || '';
  el.className = ok ? 'msg-ok' : 'msg-err';
}

/* ---------- auth ---------- */
function switchTab(which){
  document.getElementById('tab_login').classList.toggle('active', which==='login');
  document.getElementById('tab_reg').classList.toggle('active', which==='reg');
  document.getElementById('form_login').classList.toggle('hidden', which!=='login');
  document.getElementById('form_reg').classList.toggle('hidden', which!=='reg');
  setAuthStatus('');
}

async function doRegister(){
  const u = document.getElementById('reg_user').value.trim();
  const p = document.getElementById('reg_pass').value;
  if(!u || !p) return setAuthStatus('Заполните поля');
  try{
    const d = await api('/api/register', {method:'POST', body:JSON.stringify({username:u, password:p})});
    setAuthStatus('Готово: ' + d.address + '. Теперь войдите.', true);
    document.getElementById('log_user').value = u;
    switchTab('login');
  }catch(e){ setAuthStatus(e.message); }
}

async function doLogin(){
  const u = document.getElementById('log_user').value.trim();
  const p = document.getElementById('log_pass').value;
  if(!u || !p) return setAuthStatus('Заполните поля');
  try{
    const d = await api('/api/login', {method:'POST', body:JSON.stringify({username:u, password:p})});
    token = d.token; username = d.username;
    localStorage.setItem('sldchat_token', token);
    localStorage.setItem('sldchat_user', username);
    render();
    startPolling();
  }catch(e){ setAuthStatus(e.message); }
}

function doLogout(){
  token = ''; username = ''; currentPeer = null;
  localStorage.removeItem('sldchat_token');
  localStorage.removeItem('sldchat_user');
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  render();
}

/* ---------- render ---------- */
function render(){
  const logged = !!token;
  document.getElementById('auth_view').classList.toggle('hidden', logged);
  document.getElementById('app_view').classList.toggle('hidden', !logged);
  if (logged){
    document.getElementById('me_name').textContent = username;
    document.getElementById('me_srv').textContent = '@' + SERVER_DOMAIN;
    refreshConversations();
    if (currentPeer) loadMessages(currentPeer);
  }
}

/* ---------- conversations ---------- */
async function refreshConversations(){
  try{
    const d = await api('/api/conversations');
    const box = document.getElementById('conv_list');
    if(!d.conversations.length){
      box.innerHTML = '<div class="empty">Пока пусто</div>';
      return;
    }
    box.innerHTML = d.conversations.map(c => `
      <div class="conv ${c.peer===currentPeer?'active':''}" onclick="openChat('${esc(c.peer)}')">
        <div class="peer">${esc(c.peer)}</div>
        <div class="prev">${c.last_from.startsWith(username+'@') ? 'Вы: ' : ''}${esc(c.preview)}</div>
      </div>
    `).join('');
  }catch(e){ /* ignore */ }
}

function openChat(peer){
  currentPeer = peer;
  document.getElementById('peer_title').textContent = peer;
  document.getElementById('peer_status').textContent = '';
  document.getElementById('messages').innerHTML = '';
  loadMessages(peer);
  refreshConversations();
}

function openNewChat(){
  const v = document.getElementById('new_peer').value.trim();
  if(!v) return;
  if(!v.includes('@')) return alert('Формат: user@domain');
  document.getElementById('new_peer').value = '';
  openChat(v.toLowerCase());
}

async function loadMessages(peer){
  try{
    const d = await api('/api/messages?peer=' + encodeURIComponent(peer));
    const box = document.getElementById('messages');
    if(!d.messages.length){
      box.innerHTML = '<div class="placeholder">Нет сообщений. Напишите первым.</div>';
      return;
    }
    const me = username + '@' + SERVER_DOMAIN;
    box.innerHTML = d.messages.map(m => {
      const mine = m.from_addr === me;
      return `
        <div class="bubble ${mine?'me':'them'}">
          <div>${esc(m.body)}</div>
          <div class="meta">${mine ? 'Вы' : esc(m.from_addr)} · ${fmtTime(m.ts)}</div>
        </div>`;
    }).join('');
    box.scrollTop = box.scrollHeight;
  }catch(e){
    document.getElementById('messages').innerHTML = '<div class="placeholder">'+esc(e.message)+'</div>';
  }
}

/* ---------- send ---------- */
async function doSend(){
  const ta = document.getElementById('composer');
  const body = ta.value.trim();
  if(!currentPeer){ alert('Сначала выберите получателя'); return; }
  if(!body) return;
  try{
    const d = await api('/api/send', {method:'POST', body:JSON.stringify({to: currentPeer, body})});
    ta.value = '';
    ta.style.height = 'auto';
    loadMessages(currentPeer);
    refreshConversations();
  }catch(e){
    alert('Ошибка отправки: ' + e.message);
  }
}

function composerKey(e){
  if(e.key === 'Enter' && !e.shiftKey){
    e.preventDefault();
    doSend();
  }
}

/* ---------- polling ---------- */
function startPolling(){
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!token) return;
    refreshConversations();
    if (currentPeer) loadMessages(currentPeer);
  }, 4000);
}

/* ---------- init ---------- */
render();
if (token) startPolling();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE.replace("__SERVER__", SERVER_NAME)
