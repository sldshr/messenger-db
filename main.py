# main.py
# pip install fastapi uvicorn jinja2 python-multipart
# uvicorn main:app --reload

import base64
import secrets
from datetime import datetime

from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Template
import uvicorn

app = FastAPI(title="Community")

# ============================================================
#                     ХРАНИЛИЩЕ В ОПЕРАТИВКЕ
# ============================================================
STATE = {
    "settings": {
        "site_title": "Моё сообщество",
        "author_nick": "Автор",
        "channel_description": "Добро пожаловать в моё сообщество! Здесь вы найдёте самое интересное.",
        "author_tagline": "Автор и создатель",
        "footer_text": "© 2025 Моё сообщество",

        "author_photo": "",
        "main_photo": "",

        "primary_color": "#2a5db0",
        "bg_color": "#d9e4f5",
        "text_color": "#1a1a1a",
        "font_family": "Arial, Tahoma, Verdana, sans-serif",
        "border_radius": "6px",

        "info_content": "<h2>О проекте</h2><p>Здесь вы можете написать любую информацию о своём сообществе, используя HTML.</p>",
        "community_content": "<h2>Правила сообщества</h2><p>1. Будьте вежливы.<br>2. Не спамьте.<br>3. Уважайте других.</p>",

        "sections": {
            "main":      {"name": "Главная",    "visible": True, "order": 0},
            "info":      {"name": "Инфо",       "visible": True, "order": 1},
            "community": {"name": "Сообщество", "visible": True, "order": 2},
            "livechat":  {"name": "Лайв чат",   "visible": True, "order": 3},
        },

        "chat_welcome": "Добро пожаловать в чат!",
        "chat_max_history": 200,

        "admin_password": "admin",
    },
    "chat": [],
}

SESSIONS: set[str] = set()
CHAT_CLIENTS: set[WebSocket] = set()
SEC_URL = {"main": "/", "info": "/info", "community": "/community", "livechat": "/livechat"}


# ============================================================
#                     БАЗОВЫЙ ШАБЛОН (2010 style)
# ============================================================
BASE_TPL = Template("""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }} — {{ s.site_title }}</title>
<style>
:root{
  --primary: {{ s.primary_color }};
  --bg: {{ s.bg_color }};
  --text: {{ s.text_color }};
  --radius: {{ s.border_radius }};
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:{{ s.font_family }};
  font-size:13px;
  color:var(--text);
  background:var(--bg);
  background-image:
    linear-gradient(to bottom, rgba(255,255,255,.6), rgba(255,255,255,0) 220px),
    repeating-linear-gradient(0deg, rgba(0,0,0,.02) 0px, rgba(0,0,0,.02) 1px, transparent 1px, transparent 3px);
  min-height:100vh;
}
a{color:var(--primary);text-decoration:none}
a:hover{text-decoration:underline}

/* ---------- Header ---------- */
header{
  background:linear-gradient(to bottom, #6f92c9 0%, var(--primary) 50%, #1c3f7a 100%);
  border-bottom:3px solid #0e2547;
  box-shadow:0 2px 6px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.5);
  padding:0;
}
.header-inner{
  max-width:1000px;margin:0 auto;padding:14px 16px 0;
}
.brand{
  color:#fff;font-size:22px;font-weight:bold;
  text-shadow:1px 1px 0 rgba(0,0,0,.5);
  margin-bottom:10px;display:block;
  letter-spacing:.5px;
}
.brand .dot{color:#ffe86a}
nav{
  display:block;
  background:linear-gradient(to bottom, rgba(255,255,255,.25), rgba(255,255,255,.05));
  border-radius:6px 6px 0 0;
  border:1px solid #163a6e;border-bottom:none;
  padding:0 4px;
}
nav ul{list-style:none;display:flex;flex-wrap:wrap}
nav li{display:inline-block}
nav a{
  display:block;padding:8px 14px;color:#fff;font-weight:bold;
  text-decoration:none;font-size:13px;
  border-right:1px solid rgba(0,0,0,.2);
  text-shadow:1px 1px 0 rgba(0,0,0,.4);
  transition:none;
}
nav a:hover{background:rgba(255,255,255,.15);text-decoration:none}
nav a.active{
  background:linear-gradient(to bottom, #fdf3b5, #f5c842);
  color:#5a3a00 !important;
  text-shadow:0 1px 0 #fff;
}
nav a.admin-link{color:#ffe86a}

/* ---------- Layout ---------- */
main{
  max-width:1000px;margin:0 auto;
  padding:16px;
}
.card{
  background:#ffffff;
  border:1px solid #b8c5d9;
  border-radius:var(--radius);
  box-shadow:0 2px 4px rgba(0,0,0,.08);
  margin-bottom:14px;
  overflow:hidden;
}
.card-title{
  background:linear-gradient(to bottom, #eaf0f9, #cfdcee);
  border-bottom:1px solid #b8c5d9;
  padding:8px 14px;
  font-weight:bold;font-size:13px;color:#1c3f7a;
  text-shadow:0 1px 0 #fff;
}
.card-body{padding:14px}

/* ---------- Hero ---------- */
.hero{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.hero .avatar{
  width:120px;height:120px;border-radius:6px;object-fit:cover;
  border:1px solid #8a9cb8;padding:2px;background:#fff;
  box-shadow:0 1px 3px rgba(0,0,0,.2);flex-shrink:0;
}
.hero .avatar.ph{
  display:flex;align-items:center;justify-content:center;
  font-size:48px;color:#7a8ba5;background:#f3f6fb;
}
.hero h1{
  color:#1c3f7a;font-size:24px;margin-bottom:4px;
  font-family:Arial Black, Arial, sans-serif;
  text-shadow:0 1px 0 #fff;
}
.hero .tagline{color:#556a86;margin-bottom:8px;font-size:12px;font-style:italic}
.hero .desc{line-height:1.6;font-size:13px}
.cover{
  width:100%;max-height:340px;object-fit:cover;
  border-bottom:1px solid #b8c5d9;display:block;
}

/* ---------- Кнопки ---------- */
.btn{
  display:inline-block;
  background:linear-gradient(to bottom, #7ea7dd 0%, var(--primary) 50%, #1e4a8c 100%);
  color:#fff;font-weight:bold;
  border:1px solid #163a6e;
  padding:6px 14px;
  border-radius:5px;
  cursor:pointer;
  font-size:13px;font-family:inherit;
  text-shadow:1px 1px 0 rgba(0,0,0,.4);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.4), 0 1px 2px rgba(0,0,0,.2);
  text-decoration:none;
}
.btn:hover{filter:brightness(1.08);text-decoration:none}
.btn:active{box-shadow:inset 0 2px 4px rgba(0,0,0,.35);}
.btn.ghost{
  background:linear-gradient(to bottom, #ffffff, #dfe6f0);
  color:#1c3f7a;text-shadow:0 1px 0 #fff;
  border:1px solid #8a9cb8;
}
.btn.danger{
  background:linear-gradient(to bottom, #f08b8b 0%, #c32828 50%, #8b1414 100%);
  border-color:#5a0a0a;color:#fff;
}

/* ---------- Формы ---------- */
input,textarea,select{
  background:#ffffff;
  color:#1a1a1a;
  border:1px solid #8a9cb8;
  border-top-color:#5a6c88;
  padding:5px 7px;
  border-radius:3px;
  font-family:inherit;
  font-size:13px;
  width:100%;
  box-shadow:inset 0 1px 2px rgba(0,0,0,.06);
}
input:focus,textarea:focus,select:focus{
  outline:none;border-color:var(--primary);
  box-shadow:inset 0 1px 2px rgba(0,0,0,.06), 0 0 4px rgba(42,93,176,.5);
}
textarea{min-height:120px;resize:vertical;font-family:inherit}
label{display:block;margin-bottom:3px;font-weight:bold;font-size:12px;color:#33475f}
.field{margin-bottom:12px}
.checkbox-label{display:flex;align-items:center;gap:6px;font-weight:normal;margin:0}
.checkbox-label input{width:auto;box-shadow:none}

/* ---------- Чат ---------- */
.chat-box{
  background:#ffffff;
  border:1px solid #8a9cb8;
  border-top-color:#5a6c88;
  border-radius:3px;
  padding:8px;
  height:460px;overflow-y:auto;
  margin-bottom:10px;
  box-shadow:inset 0 1px 2px rgba(0,0,0,.08);
}
.msg{
  padding:5px 8px;margin-bottom:4px;
  border-radius:3px;
  background:linear-gradient(to bottom, #f6f9fd, #e8eff9);
  border:1px solid #d0dbea;
  word-wrap:break-word;line-height:1.45;
  font-size:13px;
}
.msg .nick{color:#1c3f7a;font-weight:bold;margin-right:5px}
.msg .time{font-size:11px;color:#7a8ba5;margin-left:6px}
.chat-input{display:flex;gap:6px;flex-wrap:wrap}
.chat-input input{flex:1;min-width:120px}

/* ---------- Сетки ---------- */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
@media(max-width:750px){.grid2,.grid3{grid-template-columns:1fr}}

/* ---------- Алерты ---------- */
.alert{padding:10px 14px;border-radius:4px;margin-bottom:14px;font-weight:bold}
.alert.ok{
  background:linear-gradient(to bottom, #e3f6d9, #c6eab4);
  border:1px solid #79b861;color:#2f5a1c;text-shadow:0 1px 0 #fff;
}
.alert.err{
  background:linear-gradient(to bottom, #fbe2e2, #f2c0c0);
  border:1px solid #c76a6a;color:#7a1414;text-shadow:0 1px 0 #fff;
}

.photo-preview{
  max-width:200px;max-height:200px;border-radius:4px;object-fit:cover;
  border:1px solid #8a9cb8;padding:2px;background:#fff;
  box-shadow:0 1px 3px rgba(0,0,0,.15);
  margin-bottom:8px;display:block;
}
.section-row{
  display:flex;gap:10px;align-items:center;
  padding:8px 0;border-bottom:1px dashed #c5d0e0;
}
.section-row:last-child{border-bottom:none}
.muted{color:#5d6f89;font-size:12px;font-style:italic}
code{
  background:#eef2f8;padding:1px 5px;border-radius:3px;
  font-family:Consolas, monospace;color:#a03030;font-size:12px;
  border:1px solid #d5dde9;
}
h2{color:#1c3f7a;font-size:16px;margin-bottom:10px;text-shadow:0 1px 0 #fff}
h3{color:#1c3f7a;font-size:14px;margin-bottom:8px}

/* ---------- Footer ---------- */
footer{
  text-align:center;padding:16px;color:#556a86;
  font-size:11px;
  border-top:1px solid #b8c5d9;
  margin-top:20px;
  background:linear-gradient(to bottom, rgba(255,255,255,0), rgba(255,255,255,.6));
}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <span class="brand">{{ s.site_title }} <span class="dot">●</span></span>
    <nav>
      <ul>
      {% for sec in sections if sec.visible %}
        <li><a href="{{ sec.url }}" class="{{ 'active' if sec.id == current else '' }}">{{ sec.name }}</a></li>
      {% endfor %}
        <li><a href="/admin" class="admin-link">⚙ Админ</a></li>
      </ul>
    </nav>
  </div>
</header>
<main>{{ content|safe }}</main>
<footer>{{ s.footer_text }}</footer>
</body>
</html>""")


def render_base(title: str, content: str, current: str) -> HTMLResponse:
    s = STATE["settings"]
    secs = [{"id": k, "name": v["name"], "url": SEC_URL[k],
             "visible": v["visible"], "order": v["order"]}
            for k, v in s["sections"].items()]
    secs.sort(key=lambda x: x["order"])
    html = BASE_TPL.render(title=title, s=s, sections=secs, current=current, content=content)
    return HTMLResponse(html)


# ============================================================
#                     СТРАНИЦЫ
# ============================================================
def page_main_html() -> str:
    s = STATE["settings"]
    if s["author_photo"]:
        avatar = f'<img class="avatar" src="{s["author_photo"]}" alt="avatar">'
    else:
        avatar = '<div class="avatar ph">👤</div>'
    cover = f'<img class="cover" src="{s["main_photo"]}" alt="cover">' if s["main_photo"] else ""
    return f"""
    <div class="card">
      {cover}
      <div class="card-title">О канале</div>
      <div class="card-body">
        <div class="hero">
          {avatar}
          <div style="flex:1;min-width:240px">
            <h1>{s['author_nick']}</h1>
            <div class="tagline">{s['author_tagline']}</div>
            <div class="desc">{s['channel_description']}</div>
          </div>
        </div>
      </div>
    </div>
    """


def page_info_html() -> str:
    return f"""
    <div class="card">
      <div class="card-title">📄 Инфо</div>
      <div class="card-body">{STATE['settings']['info_content']}</div>
    </div>
    """


def page_community_html() -> str:
    return f"""
    <div class="card">
      <div class="card-title">👥 Сообщество</div>
      <div class="card-body">{STATE['settings']['community_content']}</div>
    </div>
    """


def page_livechat_html() -> str:
    s = STATE["settings"]
    return f"""
    <div class="card">
      <div class="card-title">💬 Лайв чат</div>
      <div class="card-body">
        <p class="muted" style="margin-bottom:10px">{s['chat_welcome']}</p>
        <div class="chat-box" id="chat"></div>
        <div class="chat-input">
          <input id="nick" placeholder="Ваш ник" style="max-width:220px">
          <input id="msg" placeholder="Сообщение..." autocomplete="off">
          <button class="btn" onclick="send()">Отправить</button>
        </div>
      </div>
    </div>
    <script>
      const chat = document.getElementById('chat');
      const nickInput = document.getElementById('nick');
      const msgInput = document.getElementById('msg');
      nickInput.value = localStorage.getItem('chat_nick') || '';
      let ws;

      function connect() {{
        ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/chat');
        ws.onmessage = e => {{
          const d = JSON.parse(e.data);
          if (d.type === 'history') d.messages.forEach(addMsg);
          else if (d.type === 'message') addMsg(d.message);
        }};
        ws.onclose = () => setTimeout(connect, 2000);
      }}

      function addMsg(m) {{
        const el = document.createElement('div');
        el.className = 'msg';
        const n = document.createElement('span'); n.className = 'nick'; n.textContent = m.nick + ':';
        const t = document.createElement('span'); t.textContent = m.text;
        const tm = document.createElement('span'); tm.className = 'time'; tm.textContent = m.time;
        el.append(n, t, tm);
        chat.appendChild(el);
        chat.scrollTop = chat.scrollHeight;
      }}

      function send() {{
        const nick = (nickInput.value || 'Гость').trim().slice(0, 32);
        const text = msgInput.value.trim();
        if (!text || !ws || ws.readyState !== 1) return;
        localStorage.setItem('chat_nick', nick);
        ws.send(JSON.stringify({{ nick, text }}));
        msgInput.value = '';
      }}

      msgInput.addEventListener('keydown', e => {{ if (e.key === 'Enter') send(); }});
      connect();
    </script>
    """


# ============================================================
#                     РОУТЫ
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def route_main():
    return render_base("Главная", page_main_html(), "main")


@app.get("/info", response_class=HTMLResponse)
async def route_info():
    return render_base("Инфо", page_info_html(), "info")


@app.get("/community", response_class=HTMLResponse)
async def route_community():
    return render_base("Сообщество", page_community_html(), "community")


@app.get("/livechat", response_class=HTMLResponse)
async def route_livechat():
    return render_base("Лайв чат", page_livechat_html(), "livechat")


# ============================================================
#                     WEBSOCKET ЧАТ
# ============================================================
@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    CHAT_CLIENTS.add(ws)
    try:
        await ws.send_json({"type": "history", "messages": STATE["chat"][-100:]})
        while True:
            data = await ws.receive_json()
            nick = (str(data.get("nick") or "Гость")).strip()[:32] or "Гость"
            text = (str(data.get("text") or "")).strip()[:500]
            if not text:
                continue
            msg = {
                "nick": nick,
                "text": text,
                "time": datetime.now().strftime("%H:%M:%S"),
            }
            STATE["chat"].append(msg)
            limit = STATE["settings"].get("chat_max_history", 200)
            if len(STATE["chat"]) > limit:
                STATE["chat"] = STATE["chat"][-limit:]

            dead = []
            for c in list(CHAT_CLIENTS):
                try:
                    await c.send_json({"type": "message", "message": msg})
                except Exception:
                    dead.append(c)
            for d in dead:
                CHAT_CLIENTS.discard(d)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        CHAT_CLIENTS.discard(ws)


# ============================================================
#                     АДМИНКА
# ============================================================
def admin_page_html(logged: bool, message: str = "", error: str = "") -> str:
    s = STATE["settings"]
    if not logged:
        return f"""
        <div class="card" style="max-width:420px;margin:20px auto">
          <div class="card-title">🔐 Вход в админку</div>
          <div class="card-body">
            {f'<div class="alert err">{error}</div>' if error else ''}
            <form method="post" action="/admin/login">
              <div class="field">
                <label>Пароль</label>
                <input type="password" name="password" autofocus>
              </div>
              <button class="btn" type="submit">Войти</button>
            </form>
            <p class="muted" style="margin-top:12px">Пароль по умолчанию: <code>admin</code></p>
          </div>
        </div>
        """

    sections = s["sections"]
    sec_rows = ""
    for key, label in [("main","Главная"),("info","Инфо"),("community","Сообщество"),("livechat","Лайв чат")]:
        sd = sections[key]
        sec_rows += f"""
        <div class="section-row">
          <div style="width:120px"><b>{label}</b></div>
          <div style="flex:1"><input name="{key}_name" value="{sd['name']}" placeholder="Название"></div>
          <div style="width:90px"><input type="number" name="{key}_order" value="{sd['order']}" placeholder="Порядок"></div>
          <label class="checkbox-label">
            <input type="checkbox" name="{key}_visible" {'checked' if sd['visible'] else ''}>
            показ
          </label>
        </div>
        """

    author_photo_block = (
        f'<img class="photo-preview" src="{s["author_photo"]}">'
        if s["author_photo"] else '<p class="muted">не загружено</p>'
    )
    main_photo_block = (
        f'<img class="photo-preview" src="{s["main_photo"]}">'
        if s["main_photo"] else '<p class="muted">не загружено</p>'
    )

    return f"""
    <div class="card">
      <div class="card-title">⚙ Панель администратора</div>
      <div class="card-body">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
          <span>Управление сайтом. Все изменения хранятся в оперативной памяти.</span>
          <form method="post" action="/admin/logout" style="margin:0">
            <button class="btn ghost" type="submit">Выйти</button>
          </form>
        </div>
        {f'<div class="alert ok" style="margin-top:12px">{message}</div>' if message else ''}
        {f'<div class="alert err" style="margin-top:12px">{error}</div>' if error else ''}
      </div>
    </div>

    <form method="post" action="/admin/save">

      <div class="card">
        <div class="card-title">🏠 Основное</div>
        <div class="card-body">
          <div class="grid2">
            <div class="field"><label>Название сайта</label>
              <input name="site_title" value="{s['site_title']}"></div>
            <div class="field"><label>Ник автора</label>
              <input name="author_nick" value="{s['author_nick']}"></div>
          </div>
          <div class="field"><label>Подпись автора (tagline)</label>
            <input name="author_tagline" value="{s['author_tagline']}"></div>
          <div class="field"><label>Описание канала</label>
            <textarea name="channel_description">{s['channel_description']}</textarea></div>
          <div class="field"><label>Текст в подвале</label>
            <input name="footer_text" value="{s['footer_text']}"></div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">🎨 Стили</div>
        <div class="card-body">
          <div class="grid3">
            <div class="field"><label>Основной цвет</label>
              <input type="color" name="primary_color" value="{s['primary_color']}"></div>
            <div class="field"><label>Цвет фона</label>
              <input type="color" name="bg_color" value="{s['bg_color']}"></div>
            <div class="field"><label>Цвет текста</label>
              <input type="color" name="text_color" value="{s['text_color']}"></div>
          </div>
          <div class="grid2">
            <div class="field"><label>Шрифт</label>
              <input name="font_family" value="{s['font_family']}"></div>
            <div class="field"><label>Радиус скругления</label>
              <input name="border_radius" value="{s['border_radius']}"></div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">📄 Содержимое страниц</div>
        <div class="card-body">
          <div class="field"><label>Инфо (можно HTML)</label>
            <textarea name="info_content" style="min-height:160px">{s['info_content']}</textarea></div>
          <div class="field"><label>Сообщество (можно HTML)</label>
            <textarea name="community_content" style="min-height:160px">{s['community_content']}</textarea></div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">💬 Лайв чат</div>
        <div class="card-body">
          <div class="grid2">
            <div class="field"><label>Приветствие</label>
              <input name="chat_welcome" value="{s['chat_welcome']}"></div>
            <div class="field"><label>Макс. история сообщений</label>
              <input type="number" name="chat_max_history" value="{s['chat_max_history']}"></div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">🧩 Разделы меню</div>
        <div class="card-body">
          <p class="muted" style="margin-bottom:10px">Название, порядок и видимость пунктов в верхнем меню</p>
          {sec_rows}
        </div>
      </div>

      <div class="card">
        <div class="card-title">🔐 Пароль администратора</div>
        <div class="card-body">
          <div class="field"><label>Новый пароль (оставь пустым, чтобы не менять)</label>
            <input name="new_password" type="password"></div>
        </div>
      </div>

      <div class="card">
        <div class="card-body">
          <button class="btn" type="submit" style="width:100%;padding:10px;font-size:15px">💾 Сохранить всё</button>
        </div>
      </div>
    </form>

    <div class="card">
      <div class="card-title">🖼 Фотографии</div>
      <div class="card-body">
        <div class="grid2">
          <div>
            <label>Фото автора (аватар)</label>
            {author_photo_block}
            <form method="post" action="/admin/upload" enctype="multipart/form-data">
              <input type="hidden" name="field" value="author_photo">
              <div class="field"><input type="file" name="file" accept="image/*" required></div>
              <button class="btn" type="submit">Загрузить</button>
            </form>
            <form method="post" action="/admin/remove_photo" style="margin-top:8px">
              <input type="hidden" name="field" value="author_photo">
              <button class="btn danger" type="submit">Удалить</button>
            </form>
          </div>
          <div>
            <label>Обложка главной</label>
            {main_photo_block}
            <form method="post" action="/admin/upload" enctype="multipart/form-data">
              <input type="hidden" name="field" value="main_photo">
              <div class="field"><input type="file" name="file" accept="image/*" required></div>
              <button class="btn" type="submit">Загрузить</button>
            </form>
            <form method="post" action="/admin/remove_photo" style="margin-top:8px">
              <input type="hidden" name="field" value="main_photo">
              <button class="btn danger" type="submit">Удалить</button>
            </form>
          </div>
        </div>
      </div>
    </div>
    """


@app.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request, admin_token: str = Cookie(None)):
    msg = "Изменения сохранены ✔" if request.query_params.get("saved") else ""
    err = ""
    if request.query_params.get("error") == "1":
        err = "Неверный пароль"
    elif request.query_params.get("error") == "toobig":
        err = "Файл слишком большой (макс. 5 МБ)"
    logged = admin_token in SESSIONS
    # ВАЖНО: render_base уже возвращает HTMLResponse — не оборачиваем повторно
    return render_base("Админ", admin_page_html(logged, msg, err), "")


@app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    if password == STATE["settings"]["admin_password"]:
        token = secrets.token_urlsafe(32)
        SESSIONS.add(token)
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie("admin_token", token, httponly=True, samesite="lax")
        return resp
    return RedirectResponse("/admin?error=1", status_code=303)


@app.post("/admin/logout")
async def admin_logout(admin_token: str = Cookie(None)):
    SESSIONS.discard(admin_token)
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie("admin_token")
    return resp


@app.post("/admin/save")
async def admin_save(request: Request, admin_token: str = Cookie(None)):
    if admin_token not in SESSIONS:
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    s = STATE["settings"]

    def get(key, default=""):
        v = form.get(key)
        return v if v is not None else default

    s["site_title"] = get("site_title", s["site_title"])
    s["author_nick"] = get("author_nick", s["author_nick"])
    s["author_tagline"] = get("author_tagline", s["author_tagline"])
    s["channel_description"] = get("channel_description", s["channel_description"])
    s["footer_text"] = get("footer_text", s["footer_text"])

    s["primary_color"] = get("primary_color", s["primary_color"])
    s["bg_color"] = get("bg_color", s["bg_color"])
    s["text_color"] = get("text_color", s["text_color"])
    s["font_family"] = get("font_family", s["font_family"])
    s["border_radius"] = get("border_radius", s["border_radius"])

    s["info_content"] = get("info_content", s["info_content"])
    s["community_content"] = get("community_content", s["community_content"])

    s["chat_welcome"] = get("chat_welcome", s["chat_welcome"])
    try:
        s["chat_max_history"] = int(get("chat_max_history", s["chat_max_history"]))
    except (TypeError, ValueError):
        pass

    for key in ("main", "info", "community", "livechat"):
        s["sections"][key]["name"] = get(f"{key}_name", s["sections"][key]["name"]) or s["sections"][key]["name"]
        try:
            s["sections"][key]["order"] = int(get(f"{key}_order", s["sections"][key]["order"]))
        except (TypeError, ValueError):
            pass
        s["sections"][key]["visible"] = f"{key}_visible" in form

    new_pw = (get("new_password") or "").strip()
    if new_pw:
        s["admin_password"] = new_pw

    return RedirectResponse("/admin?saved=1", status_code=303)


@app.post("/admin/upload")
async def admin_upload(request: Request, admin_token: str = Cookie(None)):
    if admin_token not in SESSIONS:
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    field = form.get("field")
    up = form.get("file")
    if field in ("author_photo", "main_photo") and up is not None and hasattr(up, "read"):
        data = await up.read()
        if len(data) > 5 * 1024 * 1024:
            return RedirectResponse("/admin?error=toobig", status_code=303)
        mime = getattr(up, "content_type", None) or "image/png"
        b64 = base64.b64encode(data).decode()
        STATE["settings"][field] = f"data:{mime};base64,{b64}"
    return RedirectResponse("/admin?saved=1", status_code=303)


@app.post("/admin/remove_photo")
async def admin_remove_photo(request: Request, admin_token: str = Cookie(None)):
    if admin_token not in SESSIONS:
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    field = form.get("field")
    if field in ("author_photo", "main_photo"):
        STATE["settings"][field] = ""
    return RedirectResponse("/admin?saved=1", status_code=303)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
