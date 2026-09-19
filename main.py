"""
Мессенджер на Python + FastAPI (одним файлом).
Запуск:  python main.py     (или: uvicorn main:app --host 0.0.0.0 --port 8000)
Всё хранится только в оперативной памяти — при перезапуске данные исчезают.
"""

import secrets
import time
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(title="Мессенджер")

# ================== ХРАНИЛИЩЕ (в оперативке) ==================
users: Dict[str, dict] = {}        # nick -> {"password": str, "created": float}
sessions: Dict[str, str] = {}      # token -> nick
messages: List[dict] = []          # {"id", "from", "to", "text", "time"}
_msg_id = 0


def current_user(request: Request) -> Optional[str]:
    token = request.cookies.get("session")
    if not token:
        return None
    return sessions.get(token)


# ================== API ==================
@app.post("/api/register")
async def api_register(nick: str = Form(...), password: str = Form(...)):
    nick = nick.strip()
    if not nick or not password:
        return JSONResponse({"ok": False, "error": "Заполните все поля"}, status_code=400)
    if not (3 <= len(nick) <= 20):
        return JSONResponse({"ok": False, "error": "Ник: 3–20 символов"}, status_code=400)
    if not nick.replace("_", "").isalnum():
        return JSONResponse({"ok": False, "error": "Только буквы, цифры и _" }, status_code=400)
    if len(password) < 3:
        return JSONResponse({"ok": False, "error": "Пароль: минимум 3 символа"}, status_code=400)
    if nick in users:
        return JSONResponse({"ok": False, "error": "Ник уже занят"}, status_code=400)

    users[nick] = {"password": password, "created": time.time()}
    return {"ok": True}


@app.post("/api/login")
async def api_login(nick: str = Form(...), password: str = Form(...)):
    nick = nick.strip()
    user = users.get(nick)
    if not user or user["password"] != password:
        return JSONResponse({"ok": False, "error": "Неверный ник или пароль"}, status_code=400)

    token = secrets.token_hex(16)
    sessions[token] = nick
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", token, httponly=True, samesite="lax")
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


@app.get("/api/users")
async def api_users(request: Request):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)

    result = []
    for nick in users:
        if nick == me:
            continue
        last = None
        for m in messages:
            if (m["from"] == nick and m["to"] == me) or (m["from"] == me and m["to"] == nick):
                last = m
        result.append({"nick": nick, "last": last})

    # сортировка: с кем последний раз общались — наверх
    result.sort(key=lambda u: (u["last"]["id"] if u["last"] else 0), reverse=True)
    return {"ok": True, "users": result}


@app.get("/api/dialog/{nick}")
async def api_dialog(nick: str, request: Request, since: int = 0):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)

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
    if to == me:
        return JSONResponse({"ok": False, "error": "Нельзя писать себе"}, status_code=400)
    if to not in users:
        return JSONResponse({"ok": False, "error": "Получатель не найден"}, status_code=404)

    _msg_id += 1
    messages.append({
        "id": _msg_id,
        "from": me,
        "to": to,
        "text": text,
        "time": time.time(),
    })
    return {"ok": True, "id": _msg_id}


# ================== СТРАНИЦЫ ==================
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Мессенджер — вход</title>
<style>
  html, body { margin:0; padding:0; }
  body {
    font-family: Tahoma, Arial, sans-serif;
    font-size: 13px;
    color: #333;
    background: #c9d4de;
    background: linear-gradient(#dde5ec, #b4c2ce);
    min-height: 100vh;
    padding: 60px 0;
  }
  .box {
    width: 340px;
    margin: 0 auto;
    background: #f4f6f8;
    border: 1px solid #8b97a3;
    border-radius: 5px;
    box-shadow: 0 3px 10px rgba(0,0,0,0.25), inset 0 1px 0 #fff;
    padding: 22px 24px 24px;
  }
  h1 {
    text-align: center;
    font-size: 22px;
    font-weight: normal;
    color: #3a5169;
    margin: 0 0 18px;
    text-shadow: 0 1px 0 #fff;
    letter-spacing: 1px;
  }
  .tabs {
    display: flex;
    margin-bottom: 14px;
    border-bottom: 1px solid #a8b2bc;
  }
  .tabs button {
    flex: 1;
    border: 1px solid #a8b2bc;
    border-bottom: none;
    background: linear-gradient(#eef1f4, #d3dae1);
    border-radius: 4px 4px 0 0;
    padding: 7px 0;
    margin-right: 4px;
    cursor: pointer;
    font-family: inherit;
    font-size: 13px;
    color: #445;
  }
  .tabs button.active {
    background: #fff;
    color: #223;
    font-weight: bold;
    position: relative;
    top: 1px;
  }
  label { display: block; margin: 10px 0 4px; color: #445; }
  input[type=text], input[type=password] {
    width: 100%;
    box-sizing: border-box;
    padding: 6px 8px;
    font-family: inherit;
    font-size: 13px;
    border: 1px solid #9aa4ae;
    border-radius: 3px;
    background: #fff;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
    outline: none;
  }
  input:focus { border-color: #5a7a9a; }
  .btn {
    display: block;
    width: 100%;
    margin-top: 16px;
    padding: 8px 0;
    border: 1px solid #7a8794;
    border-radius: 4px;
    background: linear-gradient(#fbfcfd, #ccd5de);
    font-family: inherit;
    font-size: 13px;
    color: #2b3a4a;
    cursor: pointer;
    text-shadow: 0 1px 0 #fff;
  }
  .btn:hover { background: linear-gradient(#fff, #dbe3ea); }
  .btn:active { background: linear-gradient(#c2ccd6, #e3e9ee); box-shadow: inset 0 1px 3px rgba(0,0,0,0.2); }
  .error {
    min-height: 18px;
    color: #c22;
    margin-bottom: 4px;
    text-align: center;
    font-size: 12px;
  }
  .hint {
    margin-top: 14px;
    text-align: center;
    color: #889;
    font-size: 11px;
  }
</style>
</head>
<body>
<div class="box">
  <h1>Мессенджер</h1>
  <div class="tabs">
    <button type="button" id="tabLogin" class="active">Вход</button>
    <button type="button" id="tabRegister">Регистрация</button>
  </div>
  <div class="error" id="error"></div>

  <form id="formLogin">
    <label>Ник:</label>
    <input type="text" name="nick" autocomplete="off" maxlength="20">
    <label>Пароль:</label>
    <input type="password" name="password" autocomplete="off">
    <button type="submit" class="btn">Войти</button>
  </form>

  <form id="formRegister" style="display:none">
    <label>Ник:</label>
    <input type="text" name="nick" autocomplete="off" maxlength="20">
    <label>Пароль:</label>
    <input type="password" name="password" autocomplete="off">
    <button type="submit" class="btn">Зарегистрироваться</button>
  </form>

  <div class="hint">Всё хранится в оперативной памяти сервера</div>
</div>

<script>
  const tabLogin    = document.getElementById('tabLogin');
  const tabRegister = document.getElementById('tabRegister');
  const formLogin   = document.getElementById('formLogin');
  const formRegister= document.getElementById('formRegister');
  const errorBox    = document.getElementById('error');

  tabLogin.onclick = () => {
    tabLogin.classList.add('active'); tabRegister.classList.remove('active');
    formLogin.style.display = ''; formRegister.style.display = 'none';
    errorBox.textContent = '';
  };
  tabRegister.onclick = () => {
    tabRegister.classList.add('active'); tabLogin.classList.remove('active');
    formRegister.style.display = ''; formLogin.style.display = 'none';
    errorBox.textContent = '';
  };

  async function submitForm(url, form) {
    errorBox.textContent = '';
    const fd = new FormData(form);
    let d;
    try {
      const r = await fetch(url, { method: 'POST', body: fd });
      d = await r.json();
    } catch (e) {
      d = {ok:false, error:'Ошибка соединения'};
    }
    if (d.ok) { location.href = '/chat'; return; }
    errorBox.textContent = d.error || 'Ошибка';
  }

  formLogin.onsubmit    = e => { e.preventDefault(); submitForm('/api/login', formLogin); };
  formRegister.onsubmit = e => { e.preventDefault(); submitForm('/api/register', formRegister); };
</script>
</body>
</html>
"""


CHAT_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Мессенджер</title>
<style>
  html, body { margin:0; padding:0; height:100%; }
  body {
    font-family: Tahoma, Arial, sans-serif;
    font-size: 13px;
    color: #333;
    background: #b8c4ce;
    background: linear-gradient(#cfd9e2, #a8b6c2);
    padding: 10px;
    box-sizing: border-box;
    height: 100vh;
    overflow: hidden;
  }
  .app {
    display: flex;
    height: 100%;
    background: #f4f6f8;
    border: 1px solid #8b97a3;
    border-radius: 5px;
    box-shadow: 0 3px 10px rgba(0,0,0,0.25);
    overflow: hidden;
  }
  .sidebar {
    width: 230px; min-width: 230px;
    background: #e9eef2;
    border-right: 1px solid #b0bac4;
    display: flex; flex-direction: column;
  }
  .me {
    padding: 9px 10px;
    background: linear-gradient(#fbfcfd, #d6dee5);
    border-bottom: 1px solid #b0bac4;
    color: #2b3a4a;
    display: flex; justify-content: space-between; align-items: center;
    text-shadow: 0 1px 0 #fff;
  }
  .logout {
    color: #52708c; cursor: pointer; text-decoration: underline;
    font-size: 12px; text-shadow: none;
  }
  .logout:hover { color: #2b3a4a; }
  .user-list { flex: 1; overflow-y: auto; }
  .user-item {
    padding: 7px 10px;
    border-bottom: 1px solid #d3dae0;
    cursor: pointer;
    background: #eef2f5;
  }
  .user-item:hover  { background: #e0e8ef; }
  .user-item.active { background: #c6d4e2; }
  .user-item .nick  { color: #2b3a4a; font-weight: bold; }
  .user-item .preview {
    color: #7a8695; font-size: 11px; margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .chat { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .chat-header {
    padding: 9px 12px;
    background: linear-gradient(#fbfcfd, #d6dee5);
    border-bottom: 1px solid #b0bac4;
    color: #2b3a4a; font-weight: bold; text-shadow: 0 1px 0 #fff;
  }
  .messages {
    flex: 1; overflow-y: auto;
    padding: 10px 12px;
    background: #fff;
  }
  .empty { color: #a2aab3; text-align: center; margin-top: 40px; font-style: italic; }
  .msg { margin-bottom: 8px; line-height: 1.35; word-wrap: break-word; }
  .msg .head { color: #7a8695; font-size: 11px; }
  .msg .author { font-weight: bold; color: #3a5169; }
  .msg.mine .author { color: #2f6b34; }
  .msg .text { color: #222; white-space: pre-wrap; }
  .input-area {
    border-top: 1px solid #b0bac4;
    padding: 8px;
    background: #e9eef2;
    display: flex; gap: 6px;
  }
  .input-area input[type=text] {
    flex: 1;
    padding: 7px 9px;
    font-family: inherit; font-size: 13px;
    border: 1px solid #9aa4ae; border-radius: 3px;
    background: #fff;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
    outline: none;
  }
  .input-area input[type=text]:focus { border-color: #5a7a9a; }
  .input-area button {
    padding: 0 18px;
    border: 1px solid #7a8794; border-radius: 4px;
    background: linear-gradient(#fbfcfd, #ccd5de);
    font-family: inherit; font-size: 13px;
    color: #2b3a4a; cursor: pointer; text-shadow: 0 1px 0 #fff;
  }
  .input-area button:hover  { background: linear-gradient(#fff, #dbe3ea); }
  .input-area button:active { background: linear-gradient(#c2ccd6, #e3e9ee); box-shadow: inset 0 1px 3px rgba(0,0,0,0.2); }
</style>
</head>
<body>
<div class="app">
  <div class="sidebar">
    <div class="me">
      <span>Вы: <b id="myNick">…</b></span>
      <span class="logout" id="logout">Выйти</span>
    </div>
    <div class="user-list" id="userList"></div>
  </div>
  <div class="chat">
    <div class="chat-header" id="chatHeader">Выберите собеседника</div>
    <div class="messages" id="messages">
      <div class="empty">Слева выберите пользователя, чтобы начать переписку</div>
    </div>
    <div class="input-area" id="inputArea" style="display:none">
      <input type="text" id="msgInput" placeholder="Сообщение..." autocomplete="off" maxlength="1000">
      <button id="sendBtn">Отправить</button>
    </div>
  </div>
</div>

<script>
  let me = null;
  let current = null;
  const lastIds = {};
  let polling = false;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }
  function fmtTime(t) {
    const d = new Date(t * 1000);
    return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
  }
  function renderMessage(m) {
    const el = document.createElement('div');
    el.className = 'msg' + (m.from === me ? ' mine' : '');
    el.innerHTML =
      '<div class="head"><span class="author">' + esc(m.from) + '</span> · ' + fmtTime(m.time) + '</div>' +
      '<div class="text">' + esc(m.text) + '</div>';
    return el;
  }

  async function init() {
    const r = await fetch('/api/me');
    if (!r.ok) { location.href = '/'; return; }
    const d = await r.json();
    me = d.nick;
    document.getElementById('myNick').textContent = me;
    await loadUsers();
  }

  async function loadUsers() {
    const r = await fetch('/api/users');
    if (!r.ok) { location.href = '/'; return; }
    const d = await r.json();
    const list = document.getElementById('userList');
    list.innerHTML = '';

    if (!d.users.length) {
      const e = document.createElement('div');
      e.style.padding = '10px';
      e.style.color = '#889';
      e.style.fontSize = '12px';
      e.textContent = 'Других пользователей нет';
      list.appendChild(e);
      return;
    }
    for (const u of d.users) {
      const div = document.createElement('div');
      div.className = 'user-item' + (u.nick === current ? ' active' : '');
      const prev = u.last ? ((u.last.from === me ? 'Вы: ' : '') + u.last.text) : 'Нет сообщений';
      div.innerHTML =
        '<div class="nick">' + esc(u.nick) + '</div>' +
        '<div class="preview">' + esc(prev) + '</div>';
      div.onclick = () => openDialog(u.nick);
      list.appendChild(div);
    }
  }

  async function openDialog(nick) {
    current = nick;
    lastIds[nick] = 0;
    document.getElementById('chatHeader').textContent = 'Диалог с ' + nick;
    const box = document.getElementById('messages');
    box.innerHTML = '';
    document.getElementById('inputArea').style.display = 'flex';
    document.getElementById('msgInput').focus();

    const r = await fetch('/api/dialog/' + encodeURIComponent(nick) + '?since=0');
    if (r.ok) {
      const d = await r.json();
      if (d.ok && current === nick) {
        box.innerHTML = '';
        for (const m of d.messages) {
          box.appendChild(renderMessage(m));
          lastIds[nick] = m.id;
        }
        box.scrollTop = box.scrollHeight;
      }
    }
    await loadUsers();
  }

  async function pollMessages() {
    if (!current || polling) return;
    polling = true;
    const nick = current;
    const since = lastIds[nick] || 0;
    try {
      const r = await fetch('/api/dialog/' + encodeURIComponent(nick) + '?since=' + since);
      if (!r.ok) return;
      const d = await r.json();
      if (!d.ok || current !== nick) return;
      const box = document.getElementById('messages');
      let added = false;
      for (const m of d.messages) {
        box.appendChild(renderMessage(m));
        lastIds[nick] = m.id;
        added = true;
      }
      if (added) box.scrollTop = box.scrollHeight;
    } finally {
      polling = false;
    }
  }

  async function send() {
    if (!current) return;
    const inp = document.getElementById('msgInput');
    const text = inp.value.trim();
    if (!text) return;

    const fd = new FormData();
    fd.append('to', current);
    fd.append('text', text);

    const r = await fetch('/api/send', { method: 'POST', body: fd });
    const d = await r.json().catch(() => ({ok:false}));
    if (d.ok) {
      inp.value = '';
      await pollMessages();
      await loadUsers();
    } else {
      alert(d.error || 'Ошибка');
    }
  }

  document.getElementById('sendBtn').onclick = send;
  document.getElementById('msgInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); send(); }
  });
  document.getElementById('logout').onclick = async () => {
    await fetch('/api/logout', { method: 'POST' });
    location.href = '/';
  };

  setInterval(pollMessages, 1500);
  setInterval(loadUsers,    4000);

  init();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if current_user(request):
        return RedirectResponse("/chat")
    return LOGIN_PAGE


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/")
    return CHAT_PAGE


if __name__ == "__main__":
    # ВАЖНО: один воркер — данные в памяти процесса.
    uvicorn.run(app, host="0.0.0.0", port=8000)
