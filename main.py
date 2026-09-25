# main.py
# Запуск: python main.py  (или: uvicorn main:app --host 0.0.0.0 --port 8000)
# Открыть на телефоне: http://<IP-компьютера>:8000

import uuid
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn


app = FastAPI(title="Messenger")

# ---------- Хранилище в оперативке ----------
users: dict = {}      # username -> {"password": str}
tokens: dict = {}     # token -> username
messages: list = []   # {"from","to","text","ts","read"}


# ---------- Схемы ----------
class AuthData(BaseModel):
    username: str
    password: str


class MessageData(BaseModel):
    to: str
    text: str


# ---------- Авторизация ----------
def current_user(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Не авторизован")
    token = authorization[7:].strip()
    username = tokens.get(token)
    if not username:
        raise HTTPException(status_code=401, detail="Сессия истекла")
    return username


# ---------- API ----------
@app.post("/api/register")
async def register(data: AuthData):
    u = data.username.strip()
    if len(u) < 2:
        raise HTTPException(400, "Имя минимум 2 символа")
    if len(u) > 20:
        raise HTTPException(400, "Имя максимум 20 символов")
    if len(data.password) < 3:
        raise HTTPException(400, "Пароль минимум 3 символа")
    if u in users:
        raise HTTPException(400, "Такое имя уже занято")
    users[u] = {"password": data.password}
    token = uuid.uuid4().hex
    tokens[token] = u
    return {"token": token, "username": u}


@app.post("/api/login")
async def login(data: AuthData):
    u = data.username.strip()
    if u not in users or users[u]["password"] != data.password:
        raise HTTPException(400, "Неверное имя или пароль")
    token = uuid.uuid4().hex
    tokens[token] = u
    return {"token": token, "username": u}


@app.get("/api/users")
async def list_users(user: str = Depends(current_user)):
    result = []
    for name in users:
        if name == user:
            continue
        unread = sum(
            1 for m in messages
            if m["from"] == name and m["to"] == user and not m["read"]
        )
        result.append({"username": name, "unread": unread})
    result.sort(key=lambda x: x["username"].lower())
    return result


@app.get("/api/messages/{peer}")
async def get_messages(peer: str, user: str = Depends(current_user)):
    out = []
    for m in messages:
        if m["from"] == user and m["to"] == peer:
            out.append({"from": m["from"], "text": m["text"], "ts": m["ts"]})
        elif m["from"] == peer and m["to"] == user:
            m["read"] = True
            out.append({"from": m["from"], "text": m["text"], "ts": m["ts"]})
    return out


@app.post("/api/messages")
async def send_message(data: MessageData, user: str = Depends(current_user)):
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 2000:
        raise HTTPException(400, "Слишком длинное сообщение")
    if data.to not in users:
        raise HTTPException(404, "Получатель не найден")
    messages.append({
        "from": user, "to": data.to, "text": text,
        "ts": time.time(), "read": False,
    })
    return {"ok": True}


# ---------- Фронтенд ----------
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>Мессенджер</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{height:100%}
  body{
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
    background:#fff;color:#111;overflow:hidden;position:fixed;width:100%;height:100%;
    font-size:16px;-webkit-font-smoothing:antialiased;
  }
  .screen{position:absolute;inset:0;display:none;flex-direction:column;background:#fff}
  .screen.active{display:flex}

  /* ---------- Auth ---------- */
  #auth-screen{justify-content:center;align-items:center;padding:24px;overflow-y:auto}
  .auth-wrap{width:100%;max-width:380px;text-align:center;margin:auto}
  .logo{
    width:76px;height:76px;margin:0 auto 22px;background:#111;border-radius:24px;
    display:flex;align-items:center;justify-content:center;
    box-shadow:0 12px 30px rgba(0,0,0,.14);
  }
  .logo svg{width:38px;height:38px;stroke:#fff}
  h1{font-size:26px;font-weight:700;letter-spacing:-.5px;margin-bottom:6px}
  .subtitle{color:#888;font-size:14px;margin-bottom:28px}
  .tabs{display:flex;background:#f4f4f5;border-radius:12px;padding:4px;margin-bottom:22px}
  .tab{
    flex:1;padding:11px;border:none;background:transparent;border-radius:9px;
    font-size:14px;font-weight:600;color:#666;cursor:pointer;transition:all .2s;
    font-family:inherit;
  }
  .tab.active{background:#fff;color:#111;box-shadow:0 1px 3px rgba(0,0,0,.08)}
  .input-wrap{
    display:flex;align-items:center;gap:10px;background:#f4f4f5;border-radius:12px;
    padding:0 14px;margin-bottom:12px;border:1.5px solid transparent;
    transition:border-color .2s,background .2s;
  }
  .input-wrap:focus-within{background:#fff;border-color:#111}
  .input-wrap svg{width:18px;height:18px;stroke:#999;flex-shrink:0}
  .input-wrap input{
    flex:1;border:none;outline:none;background:transparent;padding:15px 0;
    font-size:16px;font-family:inherit;color:#111;min-width:0;
  }
  .input-wrap input::placeholder{color:#aaa}
  .btn-primary{
    width:100%;padding:16px;background:#111;color:#fff;border:none;border-radius:12px;
    font-size:16px;font-weight:600;cursor:pointer;margin-top:6px;
    transition:transform .1s,opacity .2s;font-family:inherit;
  }
  .btn-primary:active{transform:scale(.98)}
  .btn-primary:disabled{opacity:.5;cursor:default}
  .error{color:#e11d48;font-size:13px;margin-top:14px;min-height:18px}

  /* ---------- Header ---------- */
  header{
    display:flex;align-items:center;gap:8px;
    padding:10px 8px;
    padding-top:max(10px,env(safe-area-inset-top));
    border-bottom:1px solid #f0f0f0;background:#fff;flex-shrink:0;min-height:56px;
  }
  .header-title{
    flex:1;font-size:19px;font-weight:700;letter-spacing:-.3px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  }
  .header-title.center{text-align:center;font-size:17px}
  .icon-btn{
    width:44px;height:44px;border:none;background:transparent;
    display:flex;align-items:center;justify-content:center;
    border-radius:12px;cursor:pointer;color:#111;flex-shrink:0;transition:background .15s;
  }
  .icon-btn:active{background:#f4f4f5}
  .icon-btn svg{width:22px;height:22px;stroke:#111}

  /* ---------- Users list ---------- */
  .list{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}
  .user-row{
    display:flex;align-items:center;gap:14px;padding:12px 16px;
    cursor:pointer;transition:background .15s;
  }
  .user-row:active{background:#fafafa}
  .avatar{
    width:50px;height:50px;border-radius:50%;background:#f4f4f5;
    display:flex;align-items:center;justify-content:center;
    font-weight:700;font-size:18px;color:#555;flex-shrink:0;
  }
  .user-info{flex:1;min-width:0}
  .user-name{
    font-size:16px;font-weight:600;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  }
  .badge{
    min-width:22px;height:22px;padding:0 7px;border-radius:11px;
    background:#111;color:#fff;font-size:12px;font-weight:700;
    display:flex;align-items:center;justify-content:center;flex-shrink:0;
  }
  .empty{
    padding:60px 30px;text-align:center;color:#aaa;font-size:14px;line-height:1.5;
  }

  /* ---------- Chat ---------- */
  .messages{
    flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;
    padding:16px 14px 8px;display:flex;flex-direction:column;gap:6px;background:#fff;
  }
  .msg{
    max-width:80%;padding:9px 14px;border-radius:18px;font-size:15.5px;
    line-height:1.4;word-wrap:break-word;overflow-wrap:anywhere;white-space:pre-wrap;
    animation:pop .15s ease;
  }
  @keyframes pop{from{transform:scale(.94);opacity:0}to{transform:scale(1);opacity:1}}
  .msg.me{align-self:flex-end;background:#111;color:#fff;border-bottom-right-radius:6px}
  .msg.them{align-self:flex-start;background:#f4f4f5;color:#111;border-bottom-left-radius:6px}
  .day-sep{align-self:center;font-size:12px;color:#aaa;margin:10px 0 4px}

  .composer{
    display:flex;align-items:center;gap:8px;padding:10px 12px;
    padding-bottom:max(10px,env(safe-area-inset-bottom));
    border-top:1px solid #f0f0f0;background:#fff;flex-shrink:0;
  }
  .composer input{
    flex:1;padding:12px 18px;border:none;outline:none;background:#f4f4f5;
    border-radius:22px;font-size:16px;font-family:inherit;color:#111;min-width:0;
  }
  .composer input::placeholder{color:#aaa}
  .send-btn{
    width:44px;height:44px;border-radius:50%;border:none;background:#111;
    display:flex;align-items:center;justify-content:center;cursor:pointer;
    flex-shrink:0;transition:transform .1s,opacity .2s;
  }
  .send-btn:active{transform:scale(.92)}
  .send-btn svg{width:20px;height:20px;stroke:#fff;fill:none}
  .send-btn:disabled{opacity:.4}
</style>
</head>
<body>

<!-- ============ Экран авторизации ============ -->
<div id="auth-screen" class="screen">
  <div class="auth-wrap">
    <div class="logo">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
    </div>
    <h1>Мессенджер</h1>
    <p class="subtitle">Общайтесь без лишнего</p>

    <div class="tabs">
      <button type="button" class="tab active" data-tab="login">Вход</button>
      <button type="button" class="tab" data-tab="register">Регистрация</button>
    </div>

    <form id="auth-form" autocomplete="on">
      <div class="input-wrap">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
          <circle cx="12" cy="7" r="4"/>
        </svg>
        <input type="text" id="username" placeholder="Имя пользователя" autocomplete="username" maxlength="20">
      </div>
      <div class="input-wrap">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="11" width="18" height="11" rx="2"/>
          <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
        </svg>
        <input type="password" id="password" placeholder="Пароль" autocomplete="current-password">
      </div>
      <button type="submit" class="btn-primary" id="auth-submit">Войти</button>
      <div class="error" id="auth-error"></div>
    </form>
  </div>
</div>

<!-- ============ Список чатов ============ -->
<div id="list-screen" class="screen">
  <header>
    <div class="header-title">Чаты</div>
    <button class="icon-btn" id="logout-btn" aria-label="Выйти">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
        <polyline points="16 17 21 12 16 7"/>
        <line x1="21" y1="12" x2="9" y2="12"/>
      </svg>
    </button>
  </header>
  <div class="list" id="users-list"></div>
</div>

<!-- ============ Чат ============ -->
<div id="chat-screen" class="screen">
  <header>
    <button class="icon-btn" id="back-btn" aria-label="Назад">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="19" y1="12" x2="5" y2="12"/>
        <polyline points="12 19 5 12 12 5"/>
      </svg>
    </button>
    <div class="header-title center" id="chat-title">—</div>
    <div style="width:44px"></div>
  </header>
  <div class="messages" id="messages"></div>
  <div class="composer">
    <input type="text" id="msg-input" placeholder="Сообщение..." maxlength="2000" autocomplete="off">
    <button class="send-btn" id="send-btn" aria-label="Отправить">
      <svg viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="22" y1="2" x2="11" y2="13"/>
        <polygon points="22 2 15 22 11 13 2 9 22 2"/>
      </svg>
    </button>
  </div>
</div>

<script>
(function(){
  "use strict";
  const $ = s => document.querySelector(s);

  const state = {
    token: localStorage.getItem('token') || '',
    username: localStorage.getItem('username') || '',
    activePeer: null,
    renderedCount: 0,
    loading: false,
    chatTimer: null,
    listTimer: null,
  };

  const authHeaders = () => ({
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + state.token,
  });

  function showScreen(name){
    clearInterval(state.chatTimer); state.chatTimer = null;
    clearInterval(state.listTimer); state.listTimer = null;

    document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
    document.getElementById(name + '-screen').classList.add('active');

    if (name === 'list'){
      loadUsers();
      state.listTimer = setInterval(loadUsers, 3000);
    }
  }

  /* ---------------- Auth ---------------- */
  let authMode = 'login';

  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      authMode = tab.dataset.tab;
      $('#auth-submit').textContent = authMode === 'login' ? 'Войти' : 'Создать аккаунт';
      $('#auth-error').textContent = '';
    });
  });

  $('#auth-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = $('#username').value.trim();
    const password = $('#password').value;
    const errEl = $('#auth-error');
    errEl.textContent = '';

    if (!username || !password){
      errEl.textContent = 'Заполните все поля';
      return;
    }
    const btn = $('#auth-submit');
    btn.disabled = true;

    try {
      const res = await fetch('/api/' + authMode, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username, password}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Ошибка');

      state.token = data.token;
      state.username = data.username;
      localStorage.setItem('token', state.token);
      localStorage.setItem('username', state.username);
      $('#password').value = '';
      showScreen('list');
    } catch (err){
      errEl.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });

  function logout(){
    localStorage.removeItem('token');
    localStorage.removeItem('username');
    state.token = '';
    state.username = '';
    state.activePeer = null;
    state.renderedCount = 0;
    clearInterval(state.chatTimer); state.chatTimer = null;
    clearInterval(state.listTimer); state.listTimer = null;
    showScreen('auth');
  }

  $('#logout-btn').addEventListener('click', logout);

  /* ---------------- Users ---------------- */
  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  async function loadUsers(){
    if (!state.token) return;
    try {
      const res = await fetch('/api/users', { headers: authHeaders() });
      if (res.status === 401){ logout(); return; }
      const users = await res.json();
      const list = $('#users-list');
      list.innerHTML = '';

      if (!users.length){
        list.innerHTML = '<div class="empty">Пока никого нет.<br>Зарегистрируйте второго пользователя<br>в другом браузере или на телефоне.</div>';
        return;
      }

      users.forEach(u => {
        const row = document.createElement('div');
        row.className = 'user-row';
        const initial = (u.username[0] || '?').toUpperCase();
        row.innerHTML =
          '<div class="avatar">' + escapeHtml(initial) + '</div>' +
          '<div class="user-info"><div class="user-name">' + escapeHtml(u.username) + '</div></div>' +
          (u.unread ? '<div class="badge">' + u.unread + '</div>' : '');
        row.addEventListener('click', () => openChat(u.username));
        list.appendChild(row);
      });
    } catch(e){ /* игнорируем */ }
  }

  /* ---------------- Chat ---------------- */
  function openChat(peer){
    state.activePeer = peer;
    state.renderedCount = 0;
    $('#chat-title').textContent = peer;
    $('#messages').innerHTML = '';
    showScreen('chat');
    loadMessages(true);
    state.chatTimer = setInterval(() => loadMessages(false), 1500);
    setTimeout(() => $('#msg-input').focus(), 100);
  }

  async function loadMessages(forceScroll){
    if (!state.activePeer || state.loading) return;
    const peer = state.activePeer;
    state.loading = true;
    try {
      const res = await fetch('/api/messages/' + encodeURIComponent(peer), { headers: authHeaders() });
      if (!res.ok) return;
      const msgs = await res.json();
      if (peer !== state.activePeer) return;

      const box = $('#messages');
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;

      let appendedMine = false;
      for (let i = state.renderedCount; i < msgs.length; i++){
        const m = msgs[i];
        const el = document.createElement('div');
        el.className = 'msg ' + (m.from === state.username ? 'me' : 'them');
        el.textContent = m.text;
        box.appendChild(el);
        if (m.from === state.username) appendedMine = true;
      }
      const appended = msgs.length > state.renderedCount;
      state.renderedCount = msgs.length;

      if (forceScroll || (appended && (atBottom || appendedMine))){
        requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
      }
    } catch(e){
      // сеть отвалилась — молча
    } finally {
      state.loading = false;
    }
  }

  async function sendMessage(){
    const input = $('#msg-input');
    const text = input.value.trim();
    if (!text || !state.activePeer) return;
    input.value = '';

    try {
      const res = await fetch('/api/messages', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ to: state.activePeer, text }),
      });
      if (res.status === 401){ logout(); return; }
      await loadMessages(true);
    } catch(e){ /* игнорируем */ }
  }

  $('#send-btn').addEventListener('click', sendMessage);
  $('#msg-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter'){ e.preventDefault(); sendMessage(); }
  });

  $('#back-btn').addEventListener('click', () => {
    state.activePeer = null;
    state.renderedCount = 0;
    clearInterval(state.chatTimer); state.chatTimer = null;
    showScreen('list');
  });

  // Автоскролл при появлении клавиатуры на iOS
  if (window.visualViewport){
    window.visualViewport.addEventListener('resize', () => {
      if (state.activePeer){
        const box = $('#messages');
        box.scrollTop = box.scrollHeight;
      }
    });
  }

  /* ---------------- Инициализация ---------------- */
  if (state.token){
    fetch('/api/users', { headers: authHeaders() }).then(r => {
      if (r.ok){ showScreen('list'); } else { logout(); }
    }).catch(() => logout());
  } else {
    showScreen('auth');
  }
})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


if __name__ == "__main__":
    # host="0.0.0.0" — чтобы открывалось с телефона по локальной сети
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
