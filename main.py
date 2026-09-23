"""
sldchat — минималистичный веб-чат (IRC/TeamSpeak-like) на FastAPI.
Всё в одном файле. Данные хранятся в оперативке.

Запуск:  python main.py
Открыть: http://localhost:8000
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, Dict, List
import hashlib
import secrets
import time
import uuid

app = FastAPI(title="sldchat")

# ============================================================
#  ХРАНИЛИЩЕ В ПАМЯТИ
# ============================================================
users: Dict[str, dict] = {}                # "username_lower" -> {username, salt, hash, created}
tokens: Dict[str, str] = {}                # token -> username
channels: Dict[str, dict] = {              # id -> {id, name, owner, created}
    "general": {"id": "general", "name": "General", "owner": "system", "created": time.time()},
    "random":  {"id": "random",  "name": "Random",  "owner": "system", "created": time.time()},
}
messages: Dict[str, List[dict]] = {"general": [], "random": []}
connections: Dict[WebSocket, dict] = {}    # ws -> {username, channel}


# ============================================================
#  УТИЛИТЫ
# ============================================================
def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def find_user_by_token(token: Optional[str]) -> Optional[str]:
    if token and token in tokens:
        return tokens[token]
    return None


def require_user(x_auth_token: Optional[str] = Header(None)) -> str:
    user = find_user_by_token(x_auth_token)
    if not user:
        raise HTTPException(401, "Unauthorized")
    return user


# ============================================================
#  МОДЕЛИ
# ============================================================
class AuthBody(BaseModel):
    username: str
    password: str


class ChannelBody(BaseModel):
    name: str


# ============================================================
#  REST API
# ============================================================
@app.post("/api/register")
def register(body: AuthBody):
    name = body.username.strip()
    if not (3 <= len(name) <= 24):
        raise HTTPException(400, "Username must be 3-24 characters")
    if not all(c.isalnum() or c in "_-" for c in name):
        raise HTTPException(400, "Allowed: letters, digits, _ and -")
    if len(body.password) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")

    key = name.lower()
    if key in users:
        raise HTTPException(409, "Username already taken")

    salt = secrets.token_hex(8)
    users[key] = {
        "username": name,
        "salt": salt,
        "hash": hash_pw(body.password, salt),
        "created": time.time(),
    }
    token = secrets.token_urlsafe(24)
    tokens[token] = name
    return {"token": token, "username": name}


@app.post("/api/login")
def login(body: AuthBody):
    key = body.username.strip().lower()
    u = users.get(key)
    if not u or u["hash"] != hash_pw(body.password, u["salt"]):
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_urlsafe(24)
    tokens[token] = u["username"]
    return {"token": token, "username": u["username"]}


@app.get("/api/me")
def me(user: str = Depends(require_user)):
    return {"username": user}


@app.get("/api/channels")
def list_channels(user: str = Depends(require_user)):
    out = []
    for cid, c in channels.items():
        online = sum(1 for info in connections.values() if info["channel"] == cid)
        msgs = messages.get(cid, [])
        last = msgs[-1] if msgs else None
        out.append({
            "id": cid,
            "name": c["name"],
            "owner": c["owner"],
            "online": online,
            "count": len(msgs),
            "last_message": last,
        })
    out.sort(key=lambda c: c["last_message"]["ts"] if c["last_message"] else 0, reverse=True)
    return {"channels": out}


@app.post("/api/channels")
def create_channel(body: ChannelBody, user: str = Depends(require_user)):
    name = body.name.strip().lstrip("#").strip()
    if not (1 <= len(name) <= 32):
        raise HTTPException(400, "Channel name must be 1-32 characters")
    cid = name.lower().replace(" ", "-")
    cid = "".join(c for c in cid if c.isalnum() or c in "-_")
    if not cid:
        raise HTTPException(400, "Invalid channel name")
    if cid in channels:
        raise HTTPException(409, "Channel already exists")
    channels[cid] = {"id": cid, "name": name, "owner": user, "created": time.time()}
    messages[cid] = []
    return {"id": cid, "name": name}


@app.get("/api/channels/{cid}/messages")
def channel_messages(cid: str, user: str = Depends(require_user), limit: int = 200):
    if cid not in channels:
        raise HTTPException(404, "Channel not found")
    return {"messages": messages.get(cid, [])[-limit:]}


# ============================================================
#  WEBSOCKET
# ============================================================
async def broadcast_channel(cid: str, payload: dict):
    dead = []
    for ws, info in list(connections.items()):
        if info["channel"] == cid:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
    for ws in dead:
        connections.pop(ws, None)


async def broadcast_presence(cid: str):
    users_online = sorted({i["username"] for i in connections.values() if i["channel"] == cid})
    await broadcast_channel(cid, {"type": "presence", "channel": cid, "users": users_online})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token")
    cid = ws.query_params.get("channel")

    username = find_user_by_token(token)
    if not username or not cid or cid not in channels:
        await ws.close(code=1008)
        return

    connections[ws] = {"username": username, "channel": cid}
    await broadcast_presence(cid)

    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "message":
                text = (data.get("text") or "").strip()
                if not text or len(text) > 4000:
                    continue
                msg = {
                    "id": uuid.uuid4().hex,
                    "channel": cid,
                    "user": username,
                    "text": text,
                    "ts": time.time(),
                }
                messages.setdefault(cid, []).append(msg)
                if len(messages[cid]) > 2000:
                    messages[cid] = messages[cid][-2000:]
                await broadcast_channel(cid, {"type": "message", "message": msg})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        connections.pop(ws, None)
        try:
            await broadcast_presence(cid)
        except Exception:
            pass


# ============================================================
#  ВСТРОЕННЫЙ КЛИЕНТ (HTML + CSS + JS)
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>sldchat</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  html, body {
    height: 100vh; height: 100dvh; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 14px; color: #222; background: #e6ebee;
  }
  button { font-family: inherit; cursor: pointer; }
  input, textarea { font-family: inherit; }

  /* ---------- AUTH ---------- */
  #auth-screen {
    position: fixed; inset: 0;
    display: flex; align-items: center; justify-content: center;
    background: #517da2; padding: 20px; z-index: 100;
  }
  .auth-card {
    background: #fff; padding: 28px 26px; border-radius: 6px;
    width: 340px; max-width: 100%;
    box-shadow: 0 6px 30px rgba(0,0,0,.25);
  }
  .auth-card h1 {
    color: #517da2; text-align: center; margin-bottom: 22px;
    font-weight: 300; font-size: 28px; letter-spacing: 1px;
  }
  .auth-card input {
    width: 100%; padding: 10px 12px; margin-bottom: 12px;
    border: 1px solid #d3dae0; border-radius: 3px;
    font-size: 14px; outline: none; transition: border-color .15s;
  }
  .auth-card input:focus { border-color: #517da2; }
  .auth-card button {
    width: 100%; padding: 11px; background: #517da2; color: #fff;
    border: none; border-radius: 3px; font-size: 14px; font-weight: 500;
    transition: background .15s;
  }
  .auth-card button:hover { background: #46708f; }
  .auth-error { color: #d64541; font-size: 12px; min-height: 16px; margin-bottom: 6px; }
  .auth-toggle { text-align: center; margin-top: 14px; font-size: 12px; color: #517da2; cursor: pointer; }
  .auth-toggle:hover { text-decoration: underline; }

  /* ---------- APP ---------- */
  #app { display: none; height: 100vh; height: 100dvh; }
  #app.visible { display: flex; }

  /* Sidebar */
  .sidebar {
    width: 300px; flex-shrink: 0; background: #fff;
    border-right: 1px solid #d7dde3;
    display: flex; flex-direction: column;
  }
  .sidebar-header {
    height: 54px; background: #517da2; color: #fff;
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 12px; flex-shrink: 0;
  }
  .me { display: flex; align-items: center; gap: 9px; font-weight: 600; overflow: hidden; }
  .avatar {
    width: 34px; height: 34px; border-radius: 50%; background: #7ea9cc;
    display: flex; align-items: center; justify-content: center;
    color: #fff; font-weight: 600; font-size: 13px; text-transform: uppercase;
    flex-shrink: 0;
  }
  #me-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #logout-btn {
    background: none; border: none; color: #fff; font-size: 18px;
    padding: 6px; opacity: .8; border-radius: 3px;
  }
  #logout-btn:hover { opacity: 1; background: rgba(255,255,255,.12); }

  .search-bar { padding: 8px 12px; border-bottom: 1px solid #e6ebee; flex-shrink: 0; }
  .search-bar input {
    width: 100%; padding: 7px 10px; border: none; outline: none;
    background: #f1f4f7; border-radius: 4px; font-size: 13px;
  }

  .channel-list { flex: 1; overflow-y: auto; }
  .channel-item {
    padding: 10px 14px; border-bottom: 1px solid #f2f5f7;
    cursor: pointer; transition: background .12s;
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
  }
  .channel-item:hover { background: #f5f8fa; }
  .channel-item.active { background: #517da2; color: #fff; }
  .channel-item.active .channel-meta { color: rgba(255,255,255,.75); }
  .channel-name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .channel-meta { font-size: 11px; color: #9aa5ad; flex-shrink: 0; }

  .new-channel {
    display: flex; gap: 6px; padding: 8px 12px;
    border-top: 1px solid #e6ebee; background: #fafbfc; flex-shrink: 0;
  }
  .new-channel input {
    flex: 1; padding: 7px 10px; border: 1px solid #d3dae0;
    border-radius: 4px; font-size: 13px; outline: none; min-width: 0;
  }
  .new-channel input:focus { border-color: #517da2; }
  .new-channel button {
    background: #517da2; color: #fff; border: none;
    border-radius: 4px; padding: 0 14px; font-size: 16px; font-weight: 600;
  }
  .new-channel button:hover { background: #46708f; }

  /* Chat */
  .chat { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .chat-header {
    height: 54px; background: #517da2; color: #fff;
    display: flex; align-items: center; padding: 0 14px; gap: 10px;
    flex-shrink: 0;
  }
  .chat-title { display: flex; flex-direction: column; min-width: 0; }
  #chat-name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chat-sub { font-size: 11px; opacity: .85; }
  #back-btn {
    display: none; background: none; border: none; color: #fff;
    font-size: 30px; line-height: 1; padding: 0 6px 4px 0;
  }

  .messages {
    flex: 1; min-height: 0; overflow-y: auto;
    padding: 14px 18px; background: #e6ebee;
  }
  .empty {
    text-align: center; color: #8a949c; margin-top: 40px;
    font-size: 13px;
  }
  .msg { display: flex; margin-bottom: 6px; }
  .msg.in  { justify-content: flex-start; }
  .msg.out { justify-content: flex-end; }
  .bubble {
    max-width: 72%; padding: 6px 10px 5px; border-radius: 8px;
    background: #fff; box-shadow: 0 1px 1px rgba(0,0,0,.06);
    word-wrap: break-word; overflow-wrap: break-word;
  }
  .msg.out .bubble { background: #eeffde; }
  .name { font-size: 12px; font-weight: 600; color: #517da2; margin-bottom: 2px; }
  .text { white-space: pre-wrap; line-height: 1.35; }
  .time { font-size: 10px; color: #9aa5ad; text-align: right; margin-top: 2px; }

  .composer {
    display: flex; align-items: flex-end; gap: 8px;
    padding: 10px 12px; background: #fff;
    border-top: 1px solid #d7dde3; flex-shrink: 0;
  }
  #msg-input {
    flex: 1; border: none; outline: none; resize: none;
    padding: 9px 11px; background: #f1f4f7; border-radius: 4px;
    font-size: 14px; line-height: 1.4; max-height: 130px; min-height: 36px;
  }
  #send-btn {
    background: #517da2; color: #fff; border: none;
    border-radius: 4px; padding: 9px 16px; font-size: 14px; font-weight: 500;
  }
  #send-btn:hover { background: #46708f; }
  #send-btn:disabled { background: #b7c6d2; cursor: default; }

  /* ---------- MOBILE ---------- */
  @media (max-width: 800px) {
    #app.visible { display: block; position: relative; overflow: hidden; }
    .sidebar {
      position: absolute; inset: 0; width: 100%; border-right: none;
    }
    .chat {
      position: absolute; inset: 0; background: #e6ebee;
      transform: translateX(100%); transition: transform .25s ease;
      z-index: 5;
    }
    #app.chat-open .chat { transform: translateX(0); }
    #back-btn { display: block; }
    .bubble { max-width: 82%; }
  }

  /* scrollbars */
  .channel-list::-webkit-scrollbar,
  .messages::-webkit-scrollbar { width: 6px; }
  .channel-list::-webkit-scrollbar-thumb,
  .messages::-webkit-scrollbar-thumb { background: rgba(0,0,0,.15); border-radius: 3px; }
</style>
</head>
<body>

<!-- AUTH -->
<div id="auth-screen">
  <div class="auth-card">
    <h1>sldchat</h1>
    <div id="auth-error" class="auth-error"></div>
    <input id="auth-user" placeholder="Username" autocomplete="username" autocapitalize="none" spellcheck="false">
    <input id="auth-pass" type="password" placeholder="Password" autocomplete="current-password">
    <button id="auth-btn">Sign In</button>
    <div class="auth-toggle" id="auth-toggle">Create account</div>
  </div>
</div>

<!-- APP -->
<div id="app">
  <aside class="sidebar">
    <div class="sidebar-header">
      <div class="me">
        <span class="avatar" id="me-avatar">?</span>
        <span id="me-name">user</span>
      </div>
      <button id="logout-btn" title="Log out">⎋</button>
    </div>
    <div class="search-bar">
      <input id="search" placeholder="Search channels">
    </div>
    <div class="channel-list" id="channel-list"></div>
    <div class="new-channel">
      <input id="new-channel-name" placeholder="New channel name">
      <button id="new-channel-btn" title="Create">+</button>
    </div>
  </aside>

  <main class="chat" id="chat">
    <div class="chat-header">
      <button id="back-btn">‹</button>
      <div class="chat-title">
        <span id="chat-name">Select a channel</span>
        <span class="chat-sub" id="chat-users"></span>
      </div>
    </div>
    <div class="messages" id="messages">
      <div class="empty">Select a channel on the left to start chatting</div>
    </div>
    <div class="composer">
      <textarea id="msg-input" placeholder="Write a message..." rows="1"></textarea>
      <button id="send-btn">Send</button>
    </div>
  </main>
</div>

<script>
const state = {
  token: localStorage.getItem('sld_token') || null,
  username: null,
  channels: [],
  currentChannel: null,
  ws: null,
};

/* ---------- helpers ---------- */
function $(id) { return document.getElementById(id); }

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers['X-Auth-Token'] = state.token;
  if (opts.body && typeof opts.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.json();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

/* ---------- AUTH ---------- */
let authMode = 'login';

function setAuthMode(mode) {
  authMode = mode;
  $('auth-btn').textContent = mode === 'login' ? 'Sign In' : 'Create account';
  $('auth-toggle').textContent = mode === 'login' ? 'Create account' : 'Have an account? Sign in';
  $('auth-error').textContent = '';
}

$('auth-toggle').onclick = () => setAuthMode(authMode === 'login' ? 'register' : 'login');

$('auth-btn').onclick = doAuth;
$('auth-pass').addEventListener('keydown', e => { if (e.key === 'Enter') doAuth(); });
$('auth-user').addEventListener('keydown', e => { if (e.key === 'Enter') $('auth-pass').focus(); });

async function doAuth() {
  const u = $('auth-user').value.trim();
  const p = $('auth-pass').value;
  const errEl = $('auth-error');
  errEl.textContent = '';
  if (!u || !p) { errEl.textContent = 'Fill in all fields'; return; }

  const path = authMode === 'login' ? '/api/login' : '/api/register';
  try {
    const data = await api(path, { method: 'POST', body: { username: u, password: p } });
    state.token = data.token;
    state.username = data.username;
    localStorage.setItem('sld_token', data.token);
    await startApp();
  } catch (e) {
    errEl.textContent = e.message || 'Error';
  }
}

function logout() {
  if (state.ws) { try { state.ws.close(); } catch (e) {} state.ws = null; }
  state.token = null;
  state.username = null;
  state.currentChannel = null;
  localStorage.removeItem('sld_token');
  $('auth-screen').style.display = '';
  $('app').classList.remove('visible', 'chat-open');
  $('auth-pass').value = '';
  setAuthMode('login');
}

/* ---------- APP ---------- */
async function startApp() {
  try {
    const me = await api('/api/me');
    state.username = me.username;
  } catch (e) {
    logout();
    return;
  }
  $('auth-screen').style.display = 'none';
  $('app').classList.add('visible');
  $('me-name').textContent = state.username;
  $('me-avatar').textContent = state.username[0] || '?';
  await loadChannels();
}

async function loadChannels() {
  const data = await api('/api/channels');
  state.channels = data.channels;
  renderChannels();
}

function renderChannels() {
  const list = $('channel-list');
  const filter = $('search').value.toLowerCase().trim();
  list.innerHTML = '';
  for (const c of state.channels) {
    if (filter && !c.name.toLowerCase().includes(filter)) continue;
    const el = document.createElement('div');
    el.className = 'channel-item' + (state.currentChannel === c.id ? ' active' : '');
    el.innerHTML =
      '<div class="channel-name"># ' + escapeHtml(c.name) + '</div>' +
      '<div class="channel-meta">' + (c.online || 0) + ' online</div>';
    el.onclick = () => openChannel(c.id);
    list.appendChild(el);
  }
}

async function openChannel(id) {
  state.currentChannel = id;
  $('app').classList.add('chat-open');
  renderChannels();

  const ch = state.channels.find(c => c.id === id);
  $('chat-name').textContent = ch ? ('# ' + ch.name) : id;

  try {
    const data = await api('/api/channels/' + encodeURIComponent(id) + '/messages');
    renderMessages(data.messages);
  } catch (e) {
    renderMessages([]);
  }
  connectWs(id);
}

function renderMessages(msgs) {
  const el = $('messages');
  el.innerHTML = '';
  if (!msgs || msgs.length === 0) {
    el.innerHTML = '<div class="empty">No messages yet. Say hi 👋</div>';
    return;
  }
  for (const m of msgs) appendMessage(m, true);
  el.scrollTop = el.scrollHeight;
}

function appendMessage(m, skipScroll) {
  const el = $('messages');
  const empty = el.querySelector('.empty');
  if (empty) empty.remove();

  const out = m.user === state.username;
  const div = document.createElement('div');
  div.className = 'msg ' + (out ? 'out' : 'in');
  const t = new Date(m.ts * 1000);
  const time = String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0');

  div.innerHTML =
    '<div class="bubble">' +
      (out ? '' : '<div class="name">' + escapeHtml(m.user) + '</div>') +
      '<div class="text">' + escapeHtml(m.text) + '</div>' +
      '<div class="time">' + time + '</div>' +
    '</div>';

  el.appendChild(div);
  if (!skipScroll) el.scrollTop = el.scrollHeight;
}

/* ---------- WebSocket ---------- */
function connectWs(channelId) {
  if (state.ws) {
    try { state.ws.onclose = null; state.ws.close(); } catch (e) {}
    state.ws = null;
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = proto + '://' + location.host + '/ws?token=' +
              encodeURIComponent(state.token) + '&channel=' + encodeURIComponent(channelId);
  const ws = new WebSocket(url);
  state.ws = ws;

  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    if (data.type === 'message') {
      if (data.message.channel === state.currentChannel) appendMessage(data.message);
      // update sidebar ordering
      const ch = state.channels.find(c => c.id === data.message.channel);
      if (ch) ch.last_message = data.message;
    } else if (data.type === 'presence') {
      if (data.channel === state.currentChannel) {
        $('chat-users').textContent = data.users.length + ' online';
      }
      const ch = state.channels.find(c => c.id === data.channel);
      if (ch) { ch.online = data.users.length; renderChannels(); }
    }
  };

  ws.onclose = () => {
    if (state.currentChannel === channelId) {
      setTimeout(() => { if (state.currentChannel === channelId) connectWs(channelId); }, 1800);
    }
  };
}

/* ---------- Composer ---------- */
function sendMessage() {
  const input = $('msg-input');
  const text = input.value.trim();
  if (!text) return;
  if (!state.ws || state.ws.readyState !== 1) return;
  state.ws.send(JSON.stringify({ type: 'message', text }));
  input.value = '';
  input.style.height = 'auto';
}

$('send-btn').onclick = sendMessage;
$('msg-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
$('msg-input').addEventListener('input', e => {
  e.target.style.height = 'auto';
  e.target.style.height = Math.min(e.target.scrollHeight, 130) + 'px';
});

$('logout-btn').onclick = logout;
$('back-btn').onclick = () => $('app').classList.remove('chat-open');
$('search').addEventListener('input', renderChannels);

$('new-channel-btn').onclick = async () => {
  const name = $('new-channel-name').value.trim();
  if (!name) return;
  try {
    await api('/api/channels', { method: 'POST', body: { name } });
    $('new-channel-name').value = '';
    await loadChannels();
  } catch (e) { alert(e.message); }
};
$('new-channel-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('new-channel-btn').click();
});

/* ---------- BOOT ---------- */
if (state.token) {
  startApp();
} else {
  setAuthMode('login');
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


# ============================================================
#  ЗАПУСК
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
