# main.py
# Мини-чат в стиле IRC на FastAPI + WebSocket.
# Всё в памяти, ничего не создаёт на диске.
# Запуск:  python main.py
# Админ:    admin / admin

import hashlib
import html
import secrets
import time
from datetime import datetime
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

# ────────────────────────────────────────────────────────────
#  ХРАНИЛИЩЕ (in-memory)
# ────────────────────────────────────────────────────────────

USERS: Dict[str, dict] = {}
SESSIONS: Dict[str, str] = {}            # token -> username
CHANNELS: Dict[str, dict] = {}           # name -> {topic}
MESSAGES: Dict[str, List[dict]] = {}     # channel -> [msg]
WS_CONNECTIONS: Dict[str, Dict[WebSocket, str]] = {}  # channel -> {ws: username}

CONFIG = {
    "server_name": "MiniChat",
    "motd": "Добро пожаловать в MiniChat!",
    "max_msg_len": 500,
    "allow_registration": True,
}


def hash_pw(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def add_channel(name: str, topic: str = "") -> bool:
    name = name.strip().lower().replace(" ", "-")
    if not name or name in CHANNELS:
        return False
    CHANNELS[name] = {"topic": topic or "Без описания"}
    MESSAGES[name] = []
    return True


# стартовые каналы
add_channel("general", "Общий чат")
add_channel("random", "Всякое")

# админ
USERS["admin"] = {
    "password": hash_pw("admin"),
    "is_admin": True,
    "joined": time.time(),
    "banned": False,
}


# ────────────────────────────────────────────────────────────
#  ХЕЛПЕРЫ
# ────────────────────────────────────────────────────────────

def current_user(request: Request) -> Optional[str]:
    token = request.cookies.get("session")
    if token and token in SESSIONS:
        return SESSIONS[token]
    return None


def current_user_ws(ws: WebSocket) -> Optional[str]:
    token = ws.cookies.get("session")
    if token and token in SESSIONS:
        return SESSIONS[token]
    return None


async def broadcast(channel: str, payload: dict):
    conns = WS_CONNECTIONS.get(channel, {})
    dead = []
    for ws in list(conns.keys()):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        conns.pop(ws, None)


async def send_users(channel: str):
    conns = WS_CONNECTIONS.get(channel, {})
    users = sorted(set(conns.values()))
    await broadcast(channel, {"type": "users", "users": users})


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# ────────────────────────────────────────────────────────────
#  SVG-ИКОНКИ (feather-style)
# ────────────────────────────────────────────────────────────

def svg(body: str, cls: str = "icon") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</svg>')


ICON_CHAT = svg('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>')
ICON_HASH = svg('<line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/>'
                '<line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/>')
ICON_USERS = svg('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
                 '<circle cx="9" cy="7" r="4"/>'
                 '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>'
                 '<path d="M16 3.13a4 4 0 0 1 0 7.75"/>')
ICON_LOGOUT = svg('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
                  '<polyline points="16 17 21 12 16 7"/>'
                  '<line x1="21" y1="12" x2="9" y2="12"/>')
ICON_SETTINGS = svg('<circle cx="12" cy="12" r="3"/>'
                    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09A1.65 1.65 0 0 0 15 4.6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>')
ICON_SEND = svg('<line x1="22" y1="2" x2="11" y2="13"/>'
                '<polygon points="22 2 15 22 11 13 2 9 22 2"/>')
ICON_PLUS = svg('<line x1="12" y1="5" x2="12" y2="19"/>'
                '<line x1="5" y1="12" x2="19" y2="12"/>')
ICON_TRASH = svg('<polyline points="3 6 5 6 21 6"/>'
                 '<path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/>'
                 '<path d="M10 11v6M14 11v6"/>')
ICON_KEY = svg('<path d="M21 2l-2 2m-7.6 7.6a5 5 0 1 1-7 7 5 5 0 0 1 7-7z"/>'
               '<path d="M15.5 8.5l3 3L22 8l-3-3"/>')


# ────────────────────────────────────────────────────────────
#  CSS (общий, 2015 — плоско и минималистично)
# ────────────────────────────────────────────────────────────

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
     background:#eef1f5;color:#2c2f38;-webkit-font-smoothing:antialiased}
a{color:#3a7bd5;text-decoration:none}
a:hover{text-decoration:underline}
button{font:inherit;cursor:pointer}
input,select,textarea{font:inherit;padding:8px 10px;border:1px solid #d5dae0;
     border-radius:3px;background:#fff;outline:none;width:100%;color:inherit}
input:focus,select:focus,textarea:focus{border-color:#3a7bd5}
.btn{padding:8px 14px;border:1px solid transparent;border-radius:3px;
     background:#3a7bd5;color:#fff;transition:background .15s}
.btn:hover{background:#2f66b5}
.btn.secondary{background:#fff;border-color:#d5dae0;color:#2c2f38}
.btn.secondary:hover{background:#f2f4f7}
.btn.danger{background:#e74c3c}
.btn.danger:hover{background:#c0392b}
.icon{width:16px;height:16px;vertical-align:-2px}
.icon-lg{width:22px;height:22px}
.muted{color:#8891a0}
.small{font-size:12px}

/* ---------- LOGIN ---------- */
.auth-wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.auth-box{width:100%;max-width:340px;background:#fff;border:1px solid #e1e5eb;
     border-radius:4px;padding:28px 24px 22px;box-shadow:0 1px 3px rgba(20,25,40,.04)}
.auth-logo{display:flex;align-items:center;gap:8px;justify-content:center;
     margin-bottom:6px;color:#3a7bd5}
.auth-logo .icon{width:28px;height:28px}
.auth-title{text-align:center;font-size:20px;font-weight:600;margin-bottom:4px}
.auth-sub{text-align:center;font-size:12px;color:#8891a0;margin-bottom:20px}
.auth-box input{margin-bottom:10px}
.auth-box .btn{width:100%;padding:10px}
.auth-foot{text-align:center;margin-top:14px;font-size:13px}
.auth-err{background:#fdeceb;color:#c0392b;border:1px solid #f5c6c2;
     border-radius:3px;padding:8px 10px;font-size:13px;margin-bottom:12px}

/* ---------- CHAT ---------- */
.app{display:flex;height:100vh;background:#fff;overflow:hidden}
.sidebar{width:230px;flex:0 0 230px;background:#f7f8fa;border-right:1px solid #e1e5eb;
     display:flex;flex-direction:column}
.brand{display:flex;align-items:center;gap:8px;padding:16px;font-weight:600;
     font-size:15px;border-bottom:1px solid #e1e5eb;color:#2c2f38}
.brand .icon{color:#3a7bd5;width:18px;height:18px}
.section-title{padding:14px 16px 6px;font-size:11px;text-transform:uppercase;
     letter-spacing:.6px;color:#8891a0;font-weight:600}
.channels{list-style:none;flex:1;overflow-y:auto;padding-bottom:8px}
.channels a{display:flex;align-items:center;gap:8px;padding:7px 16px;color:#4b5563;font-size:13px}
.channels a:hover{background:#eef1f5;text-decoration:none}
.channels a.active{background:#e4ecfa;color:#2f66b5;font-weight:600}
.channels .hash{color:#a3acbb;font-weight:400}
.channels a.active .hash{color:#3a7bd5}
.user-box{display:flex;align-items:center;gap:10px;padding:10px 12px;
     border-top:1px solid #e1e5eb;background:#f0f2f5}
.avatar{width:30px;height:30px;border-radius:50%;background:#3a7bd5;color:#fff;
     display:flex;align-items:center;justify-content:center;font-weight:600;font-size:13px}
.user-meta{flex:1;min-width:0}
.user-name{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.user-role{font-size:11px;color:#8891a0}
.user-actions{display:flex;gap:4px;align-items:center}
.user-actions a{display:inline-flex;padding:5px;border-radius:3px;color:#6b7480}
.user-actions a:hover{background:#e1e5eb;text-decoration:none;color:#2c2f38}

.chat{flex:1;display:flex;flex-direction:column;min-width:0}
.chat-head{display:flex;align-items:center;gap:10px;padding:14px 18px;
     border-bottom:1px solid #e1e5eb;background:#fff}
.chat-head h2{font-size:16px;font-weight:600}
.chat-head .hash{color:#a3acbb;font-size:18px}
.chat-head .topic{color:#8891a0;font-size:13px;margin-left:6px;
     white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.chat-head .user-count{display:inline-flex;align-items:center;gap:5px;
     color:#6b7480;font-size:12px;background:#f2f4f7;padding:4px 9px;border-radius:12px}

.messages{flex:1;overflow-y:auto;padding:14px 18px;background:#fff}
.msg{padding:6px 0;font-size:14px}
.msg-user{font-weight:600;color:#2f66b5}
.msg-time{font-size:11px;color:#a3acbb;margin-left:6px}
.msg-text{margin-top:1px;word-wrap:break-word;overflow-wrap:anywhere}
.system-msg{font-size:12px;color:#8891a0;padding:5px 0;font-style:italic}
.system-msg::before{content:"— ";color:#c3cad4}

.composer{display:flex;gap:8px;padding:12px 14px;border-top:1px solid #e1e5eb;background:#fafbfc}
.composer input{flex:1;padding:10px 12px;border-radius:4px;background:#fff}
.composer button{background:#3a7bd5;color:#fff;border:none;border-radius:4px;
     padding:0 16px;display:flex;align-items:center;justify-content:center;transition:.15s}
.composer button:hover{background:#2f66b5}

.users-panel{width:200px;flex:0 0 200px;border-left:1px solid #e1e5eb;
     background:#f7f8fa;display:flex;flex-direction:column}
.users-panel ul{list-style:none;padding:0 6px 12px;overflow-y:auto;flex:1}
.users-panel li{padding:6px 10px;font-size:13px;color:#4b5563;border-radius:3px;
     white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.users-panel li::before{content:"●";color:#4caf7d;margin-right:7px;font-size:9px;
     vertical-align:2px}

/* ---------- ADMIN ---------- */
.admin-wrap{max-width:960px;margin:0 auto;padding:26px 20px}
.admin-head{display:flex;align-items:center;justify-content:space-between;
     margin-bottom:18px;gap:14px}
.admin-head h1{font-size:20px;font-weight:600;display:flex;align-items:center;gap:8px}
.admin-head h1 .icon{color:#3a7bd5}
.tabs{display:flex;gap:4px;border-bottom:1px solid #d5dae0;margin-bottom:18px}
.tabs a{padding:9px 14px;color:#6b7480;font-size:13px;border-bottom:2px solid transparent;
     margin-bottom:-1px}
.tabs a:hover{color:#2c2f38;text-decoration:none}
.tabs a.active{color:#3a7bd5;border-bottom-color:#3a7bd5;font-weight:600}
.card{background:#fff;border:1px solid #e1e5eb;border-radius:4px;padding:18px;margin-bottom:16px;
     box-shadow:0 1px 2px rgba(20,25,40,.03)}
.card h3{font-size:14px;font-weight:600;margin-bottom:12px}
.row{display:flex;gap:8px;align-items:center}
.row input{flex:1}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:720px){.grid{grid-template-columns:1fr}}
label.fld{display:block;margin-bottom:10px}
label.fld span{display:block;font-size:12px;color:#6b7480;margin-bottom:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #eef1f5}
th{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#8891a0;font-weight:600}
tr:last-child td{border-bottom:none}
.tag{display:inline-block;padding:2px 7px;font-size:11px;border-radius:10px;
     background:#e4ecfa;color:#2f66b5}
.tag.admin{background:#fbeecf;color:#a37506}
.tag.banned{background:#fdeceb;color:#c0392b}
td form{display:inline}
td .btn{padding:4px 9px;font-size:12px}
.flash{background:#eaf4ed;color:#2b7a4b;border:1px solid #c6e3cf;
     padding:9px 12px;border-radius:3px;margin-bottom:14px;font-size:13px}
"""


# ────────────────────────────────────────────────────────────
#  СТРАНИЦЫ
# ────────────────────────────────────────────────────────────

def page(title: str, body: str, extra_css: str = "") -> HTMLResponse:
    doc = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}{extra_css}</style></head>
<body>{body}</body></html>"""
    return HTMLResponse(doc)


def render_login(err: str = "") -> HTMLResponse:
    err_html = f'<div class="auth-err">{html.escape(err)}</div>' if err else ""
    body = f"""
<div class="auth-wrap"><div class="auth-box">
  <div class="auth-logo">{ICON_CHAT}<span style="font-size:20px;font-weight:600;color:#2c2f38">MiniChat</span></div>
  <div class="auth-sub">{html.escape(CONFIG['server_name'])}</div>
  {err_html}
  <form method="post" action="/login">
    <input name="username" placeholder="Логин" required autofocus>
    <input name="password" type="password" placeholder="Пароль" required>
    <button class="btn" type="submit">Войти</button>
  </form>
  <div class="auth-foot">Нет аккаунта? <a href="/register">Зарегистрироваться</a></div>
</div></div>"""
    return page("Вход — MiniChat", body)


def render_register(err: str = "") -> HTMLResponse:
    err_html = f'<div class="auth-err">{html.escape(err)}</div>' if err else ""
    if not CONFIG["allow_registration"]:
        body = f"""
<div class="auth-wrap"><div class="auth-box">
  <div class="auth-logo">{ICON_CHAT}</div>
  <div class="auth-title">Регистрация закрыта</div>
  <div class="auth-sub">Администратор отключил регистрацию.</div>
  <div class="auth-foot"><a href="/login">← Вернуться ко входу</a></div>
</div></div>"""
        return page("Регистрация — MiniChat", body)
    body = f"""
<div class="auth-wrap"><div class="auth-box">
  <div class="auth-logo">{ICON_CHAT}<span style="font-size:20px;font-weight:600;color:#2c2f38">MiniChat</span></div>
  <div class="auth-sub">Создание аккаунта</div>
  {err_html}
  <form method="post" action="/register">
    <input name="username" placeholder="Логин" required autofocus pattern="[A-Za-z0-9_\\-]{{3,20}}"
           title="3-20 символов: буквы, цифры, _ и -">
    <input name="password" type="password" placeholder="Пароль" required minlength="3">
    <button class="btn" type="submit">Зарегистрироваться</button>
  </form>
  <div class="auth-foot">Уже есть аккаунт? <a href="/login">Войти</a></div>
</div></div>"""
    return page("Регистрация — MiniChat", body)


def render_chat(username: str, channel: str) -> HTMLResponse:
    user = USERS[username]
    chan = CHANNELS[channel]

    channels_html = ""
    for cname in CHANNELS:
        active = " active" if cname == channel else ""
        channels_html += (
            f'<li><a class="channel{active}" href="/?c={html.escape(cname)}">'
            f'<span class="hash">#</span>{html.escape(cname)}</a></li>'
        )

    admin_btn = (
        f'<a href="/admin" title="Админ-панель">{ICON_SETTINGS}</a>'
        if user["is_admin"] else ""
    )

    initial = html.escape(username[0].upper())
    role = "admin" if user["is_admin"] else "участник"

    body = f"""
<div class="app">
  <aside class="sidebar">
    <div class="brand">{ICON_CHAT}<span>{html.escape(CONFIG['server_name'])}</span></div>
    <div class="section-title">Каналы</div>
    <ul class="channels">{channels_html}</ul>
    <div class="user-box">
      <div class="avatar">{initial}</div>
      <div class="user-meta">
        <div class="user-name">{html.escape(username)}</div>
        <div class="user-role">{role}</div>
      </div>
      <div class="user-actions">
        {admin_btn}
        <a href="/logout" title="Выйти">{ICON_LOGOUT}</a>
      </div>
    </div>
  </aside>

  <main class="chat">
    <header class="chat-head">
      <span class="hash">#</span>
      <h2>{html.escape(channel)}</h2>
      <span class="topic">{html.escape(chan['topic'])}</span>
      <span class="user-count">{ICON_USERS}<span id="user-count">0</span></span>
    </header>
    <div class="messages" id="messages"></div>
    <form class="composer" id="composer" autocomplete="off">
      <input id="msg-input" placeholder="Написать сообщение..."
             maxlength="{CONFIG['max_msg_len']}" autofocus>
      <button type="submit" title="Отправить">{ICON_SEND}</button>
    </form>
  </main>

  <aside class="users-panel">
    <div class="section-title">Участники</div>
    <ul id="user-list"></ul>
  </aside>
</div>

<script>
(function(){{
  const CHANNEL = {channel!r};
  const msgBox   = document.getElementById('messages');
  const userList = document.getElementById('user-list');
  const userCnt  = document.getElementById('user-count');
  const input    = document.getElementById('msg-input');
  const form     = document.getElementById('composer');
  let ws = null, retry = 0;

  const esc = s => String(s).replace(/[&<>"']/g,
    c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);

  function addMessage(m){{
    const d = document.createElement('div');
    d.className = 'msg';
    d.innerHTML = '<span class="msg-user">' + esc(m.user) + '</span>' +
                  '<span class="msg-time">' + esc(m.time) + '</span>' +
                  '<div class="msg-text">' + esc(m.text) + '</div>';
    msgBox.appendChild(d);
    msgBox.scrollTop = msgBox.scrollHeight;
  }}
  function addSystem(t){{
    const d = document.createElement('div');
    d.className = 'system-msg';
    d.textContent = t;
    msgBox.appendChild(d);
    msgBox.scrollTop = msgBox.scrollHeight;
  }}
  function setUsers(list){{
    userList.innerHTML = '';
    list.forEach(u => {{
      const li = document.createElement('li');
      li.textContent = u;
      userList.appendChild(li);
    }});
    userCnt.textContent = list.length;
  }}

  function connect(){{
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/ws/' + encodeURIComponent(CHANNEL));
    ws.onopen = () => {{ retry = 0; }};
    ws.onmessage = e => {{
      let d; try {{ d = JSON.parse(e.data); }} catch(_) {{ return; }}
      if (d.type === 'history') {{ msgBox.innerHTML=''; d.messages.forEach(addMessage); }}
      else if (d.type === 'message') addMessage(d);
      else if (d.type === 'system') addSystem(d.text);
      else if (d.type === 'users') setUsers(d.users);
    }};
    ws.onclose = () => {{
      addSystem('Соединение потеряно, переподключение...');
      const delay = Math.min(5000, 800 * Math.pow(1.6, retry++));
      setTimeout(connect, delay);
    }};
  }}

  form.addEventListener('submit', e => {{
    e.preventDefault();
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({{ type: 'message', text }}));
    input.value = '';
  }});

  connect();
}})();
</script>"""
    return page(f"#{channel} — {CONFIG['server_name']}", body)


def render_admin(username: str, tab: str = "channels") -> HTMLResponse:
    tabs = [("channels", "Каналы"), ("users", "Пользователи"), ("settings", "Настройки")]
    tabs_html = ""
    for key, label in tabs:
        cls = " active" if key == tab else ""
        tabs_html += f'<a class="{cls}" href="/admin?tab={key}">{label}</a>'

    content = ""
    if tab == "channels":
        rows = ""
        for cname, cinfo in CHANNELS.items():
            count = len(MESSAGES.get(cname, []))
            online = len(set(WS_CONNECTIONS.get(cname, {{}}).values())) if cname in WS_CONNECTIONS else 0
            rows += f"""
<tr>
  <td><b>#{html.escape(cname)}</b></td>
  <td>
    <form method="post" action="/admin/channels/topic" class="row">
      <input type="hidden" name="name" value="{html.escape(cname)}">
      <input name="topic" value="{html.escape(cinfo['topic'])}" maxlength="120">
      <button class="btn secondary" type="submit">OK</button>
    </form>
  </td>
  <td class="muted small">{count} сообщ.<br>{online} online</td>
  <td>
    <form method="post" action="/admin/channels/clear" onsubmit="return confirm('Очистить #{html.escape(cname)}?')">
      <input type="hidden" name="name" value="{html.escape(cname)}">
      <button class="btn secondary" type="submit">Очистить</button>
    </form>
    <form method="post" action="/admin/channels/delete" onsubmit="return confirm('Удалить #{html.escape(cname)}?')">
      <input type="hidden" name="name" value="{html.escape(cname)}">
      <button class="btn danger" type="submit">{ICON_TRASH}</button>
    </form>
  </td>
</tr>"""
        content = f"""
<div class="card">
  <h3>Создать канал</h3>
  <form method="post" action="/admin/channels/create" class="row">
    <input name="name" placeholder="название (a-z, 0-9, -)" required pattern="[A-Za-z0-9_\\-]{{2,30}}">
    <input name="topic" placeholder="описание" maxlength="120">
    <button class="btn" type="submit">{ICON_PLUS} Создать</button>
  </form>
</div>
<div class="card">
  <h3>Существующие каналы ({len(CHANNELS)})</h3>
  <table><thead><tr><th>Канал</th><th>Описание</th><th>Статистика</th><th></th></tr></thead>
  <tbody>{rows}</tbody></table>
</div>"""

    elif tab == "users":
        rows = ""
        for uname, udata in sorted(USERS.items(), key=lambda kv: kv[0].lower()):
            tags = ""
            if udata["is_admin"]: tags += ' <span class="tag admin">admin</span>'
            if udata.get("banned"): tags += ' <span class="tag banned">бан</span>'
            joined = datetime.fromtimestamp(udata["joined"]).strftime("%d.%m.%Y")
            admin_btn = (
                f'<form method="post" action="/admin/users/demote">'
                f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                f'<button class="btn secondary" type="submit">Снять админа</button></form>'
                if udata["is_admin"] and uname != "admin" else ""
            )
            promote_btn = (
                f'<form method="post" action="/admin/users/promote">'
                f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                f'<button class="btn secondary" type="submit">Сделать админом</button></form>'
                if not udata["is_admin"] else ""
            )
            ban_btn = ""
            if uname != "admin":
                if udata.get("banned"):
                    ban_btn = (
                        f'<form method="post" action="/admin/users/unban">'
                        f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                        f'<button class="btn secondary" type="submit">Разбанить</button></form>'
                    )
                else:
                    ban_btn = (
                        f'<form method="post" action="/admin/users/ban" '
                        f'onsubmit="return confirm(\'Забанить {html.escape(uname)}?\')">'
                        f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                        f'<button class="btn danger" type="submit">Забанить</button></form>'
                    )
            delete_btn = ""
            if uname != "admin":
                delete_btn = (
                    f'<form method="post" action="/admin/users/delete" '
                    f'onsubmit="return confirm(\'Удалить {html.escape(uname)} навсегда?\')">'
                    f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                    f'<button class="btn danger" type="submit">{ICON_TRASH}</button></form>'
                )
            rows += f"""<tr>
  <td><b>{html.escape(uname)}</b>{tags}</td>
  <td class="muted small">{joined}</td>
  <td>{admin_btn} {promote_btn} {ban_btn} {delete_btn}</td>
</tr>"""
        content = f"""
<div class="card">
  <h3>Все пользователи ({len(USERS)})</h3>
  <table><thead><tr><th>Пользователь</th><th>Регистрация</th><th>Действия</th></tr></thead>
  <tbody>{rows}</tbody></table>
</div>"""

    else:  # settings
        content = f"""
<div class="card">
  <h3>Общие настройки сервера</h3>
  <form method="post" action="/admin/settings">
    <label class="fld"><span>Название сервера</span>
      <input name="server_name" value="{html.escape(CONFIG['server_name'])}" maxlength="60" required>
    </label>
    <label class="fld"><span>Приветствие (MOTD)</span>
      <input name="motd" value="{html.escape(CONFIG['motd'])}" maxlength="200">
    </label>
    <div class="grid">
      <label class="fld"><span>Максимум символов в сообщении</span>
        <input type="number" name="max_msg_len" min="50" max="5000"
               value="{CONFIG['max_msg_len']}" required>
      </label>
      <label class="fld"><span>Регистрация новых пользователей</span>
        <select name="allow_registration">
          <option value="1" {"selected" if CONFIG['allow_registration'] else ""}>Разрешена</option>
          <option value="0" {"" if CONFIG['allow_registration'] else "selected"}>Запрещена</option>
        </select>
      </label>
    </div>
    <button class="btn" type="submit">Сохранить настройки</button>
  </form>
</div>"""

    body = f"""
<div class="admin-wrap">
  <div class="admin-head">
    <h1>{ICON_SETTINGS} Админ-панель</h1>
    <div class="row">
      <a class="btn secondary" href="/">← К чату</a>
    </div>
  </div>
  <div class="tabs">{tabs_html}</div>
  {content}
</div>"""
    return page("Админ — MiniChat", body)


# ────────────────────────────────────────────────────────────
#  AUTH-РОУТЫ
# ────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page(request: Request):
    if current_user(request):
        return redirect("/")
    return render_login()


@app.post("/login")
async def login_post(username: str = Form(...), password: str = Form(...)):
    uname = username.strip()
    user = USERS.get(uname)
    if not user or user["password"] != hash_pw(password):
        return render_login("Неверный логин или пароль")
    if user.get("banned"):
        return render_login("Аккаунт заблокирован")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = uname
    resp = redirect("/")
    resp.set_cookie("session", token, httponly=True, max_age=86400 * 7, samesite="lax")
    return resp


@app.get("/register")
async def register_page(request: Request):
    if current_user(request):
        return redirect("/")
    return render_register()


@app.post("/register")
async def register_post(username: str = Form(...), password: str = Form(...)):
    if not CONFIG["allow_registration"]:
        return render_register("Регистрация закрыта")
    uname = username.strip()
    if not (3 <= len(uname) <= 20) or not all(c.isalnum() or c in "_-" for c in uname):
        return render_register("Некорректный логин (3-20: буквы, цифры, _ -)")
    if uname.lower() == "admin" or uname in USERS:
        return render_register("Такой логин уже занят")
    if len(password) < 3:
        return render_register("Пароль слишком короткий")
    USERS[uname] = {
        "password": hash_pw(password),
        "is_admin": False,
        "joined": time.time(),
        "banned": False,
    }
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = uname
    resp = redirect("/")
    resp.set_cookie("session", token, httponly=True, max_age=86400 * 7, samesite="lax")
    return resp


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        SESSIONS.pop(token, None)
    resp = redirect("/login")
    resp.delete_cookie("session")
    return resp


# ────────────────────────────────────────────────────────────
#  ОСНОВНОЙ ЧАТ
# ────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    user = current_user(request)
    if not user or user not in USERS or USERS[user].get("banned"):
        return redirect("/login")

    ch = request.query_params.get("c")
    if not ch or ch not in CHANNELS:
        ch = next(iter(CHANNELS), None)
    if not ch:
        # нет ни одного канала — создаём общий
        add_channel("general", "Общий чат")
        ch = "general"
    return render_chat(user, ch)


@app.websocket("/ws/{channel}")
async def ws_route(ws: WebSocket, channel: str):
    username = current_user_ws(ws)
    if not username or username not in USERS or USERS[username].get("banned"):
        await ws.close(code=1008)
        return
    if channel not in CHANNELS:
        await ws.close(code=1008)
        return

    await ws.accept()
    WS_CONNECTIONS.setdefault(channel, {})
    conns = WS_CONNECTIONS[channel]
    already_here = username in conns.values()
    conns[ws] = username

    # история
    await ws.send_json({"type": "history", "messages": MESSAGES.get(channel, [])[-100:]})
    # MOTD
    if CONFIG["motd"]:
        await ws.send_json({"type": "system", "text": CONFIG["motd"]})

    if not already_here:
        await broadcast(channel, {"type": "system",
                                  "text": f"{username} присоединился к #{channel}"})
    await send_users(channel)

    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "message":
                text = (data.get("text") or "").strip()
                if not text:
                    continue
                text = text[:CONFIG["max_msg_len"]]
                msg = {
                    "user": username,
                    "text": text,
                    "time": datetime.now().strftime("%H:%M"),
                }
                MESSAGES.setdefault(channel, []).append(msg)
                if len(MESSAGES[channel]) > 500:
                    MESSAGES[channel] = MESSAGES[channel][-500:]
                await broadcast(channel, {"type": "message", **msg})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        conns.pop(ws, None)
        if username not in conns.values():
            await broadcast(channel, {"type": "system",
                                      "text": f"{username} покинул #{channel}"})
        await send_users(channel)


# ────────────────────────────────────────────────────────────
#  АДМИН-ДЕЙСТВИЯ
# ────────────────────────────────────────────────────────────

def require_admin(request: Request):
    user = current_user(request)
    if not user or user not in USERS or not USERS[user]["is_admin"]:
        return None
    return user


@app.get("/admin")
async def admin_page(request: Request, tab: str = "channels"):
    if not require_admin(request):
        return redirect("/login")
    user = current_user(request)
    if tab not in ("channels", "users", "settings"):
        tab = "channels"
    return render_admin(user, tab)


@app.post("/admin/channels/create")
async def admin_channel_create(request: Request, name: str = Form(...), topic: str = Form("")):
    if not require_admin(request):
        return redirect("/login")
    add_channel(name, topic.strip())
    return redirect("/admin?tab=channels")


@app.post("/admin/channels/topic")
async def admin_channel_topic(request: Request, name: str = Form(...), topic: str = Form("")):
    if not require_admin(request):
        return redirect("/login")
    if name in CHANNELS:
        CHANNELS[name]["topic"] = topic.strip()[:120] or "Без описания"
    return redirect("/admin?tab=channels")


@app.post("/admin/channels/clear")
async def admin_channel_clear(request: Request, name: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if name in MESSAGES:
        MESSAGES[name] = []
    return redirect("/admin?tab=channels")


@app.post("/admin/channels/delete")
async def admin_channel_delete(request: Request, name: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    CHANNELS.pop(name, None)
    MESSAGES.pop(name, None)
    # отключаем всех в удалённом канале
    for ws in list(WS_CONNECTIONS.pop(name, {}).keys()):
        try:
            await ws.close(code=1001)
        except Exception:
            pass
    return redirect("/admin?tab=channels")


@app.post("/admin/users/promote")
async def admin_user_promote(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if username in USERS:
        USERS[username]["is_admin"] = True
    return redirect("/admin?tab=users")


@app.post("/admin/users/demote")
async def admin_user_demote(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if username in USERS and username != "admin":
        USERS[username]["is_admin"] = False
    return redirect("/admin?tab=users")


@app.post("/admin/users/ban")
async def admin_user_ban(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if username in USERS and username != "admin":
        USERS[username]["banned"] = True
        # выкидываем из сессий и WS
        for tok, un in list(SESSIONS.items()):
            if un == username:
                SESSIONS.pop(tok, None)
        for ch, conns in WS_CONNECTIONS.items():
            for ws, un in list(conns.items()):
                if un == username:
                    conns.pop(ws, None)
                    try:
                        await ws.close(code=1008)
                    except Exception:
                        pass
            await send_users(ch)
    return redirect("/admin?tab=users")


@app.post("/admin/users/unban")
async def admin_user_unban(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if username in USERS:
        USERS[username]["banned"] = False
    return redirect("/admin?tab=users")


@app.post("/admin/users/delete")
async def admin_user_delete(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if username in USERS and username != "admin":
        USERS.pop(username, None)
        for tok, un in list(SESSIONS.items()):
            if un == username:
                SESSIONS.pop(tok, None)
        for ch, conns in WS_CONNECTIONS.items():
            for ws, un in list(conns.items()):
                if un == username:
                    conns.pop(ws, None)
                    try:
                        await ws.close(code=1001)
                    except Exception:
                        pass
            await send_users(ch)
    return redirect("/admin?tab=users")


@app.post("/admin/settings")
async def admin_settings(
    request: Request,
    server_name: str = Form(...),
    motd: str = Form(""),
    max_msg_len: int = Form(...),
    allow_registration: str = Form("1"),
):
    if not require_admin(request):
        return redirect("/login")
    CONFIG["server_name"] = server_name.strip()[:60] or "MiniChat"
    CONFIG["motd"] = motd.strip()[:200]
    CONFIG["max_msg_len"] = max(50, min(5000, int(max_msg_len)))
    CONFIG["allow_registration"] = allow_registration == "1"
    return redirect("/admin?tab=settings")


# ────────────────────────────────────────────────────────────
#  ТОЧКА ВХОДА
# ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 52)
    print("  MiniChat запущен")
    print("  Открой:   http://127.0.0.1:8000")
    print("  Админ:    admin / admin")
    print("=" * 52)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
