# main.py
# Запуск: pip install fastapi uvicorn jinja2 python-multipart
#         uvicorn main:app --reload

import asyncio
import base64
import secrets
from datetime import datetime

from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from jinja2 import Template
import uvicorn

app = FastAPI(title="Community")

# ============================================================
#                     ХРАНИЛИЩЕ В ОПЕРАТИВКЕ
# ============================================================
STATE = {
    "settings": {
        # --- Основные ---
        "site_title": "Моё сообщество",
        "author_nick": "Автор",
        "channel_description": "Добро пожаловать в моё сообщество! Здесь вы найдёте самое интересное.",
        "author_tagline": "Автор и создатель",
        "footer_text": "© 2025 Моё сообщество",

        # --- Фото (data-URL) ---
        "author_photo": "",
        "main_photo": "",

        # --- Стили ---
        "primary_color": "#6366f1",
        "bg_color": "#0f172a",
        "text_color": "#e2e8f0",
        "font_family": "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
        "border_radius": "16px",

        # --- Содержимое страниц ---
        "info_content": "<h2>О проекте</h2><p>Здесь вы можете написать любую информацию о своём сообществе, используя HTML.</p>",
        "community_content": "<h2>Правила сообщества</h2><p>1. Будьте вежливы.<br>2. Не спамьте.<br>3. Уважайте других.</p>",

        # --- Разделы (видимость / название / порядок) ---
        "sections": {
            "main":      {"name": "Главная",    "visible": True, "order": 0},
            "info":      {"name": "Инфо",       "visible": True, "order": 1},
            "community": {"name": "Сообщество", "visible": True, "order": 2},
            "livechat":  {"name": "Лайв чат",   "visible": True, "order": 3},
        },

        # --- Чат ---
        "chat_welcome": "Добро пожаловать в чат!",
        "chat_max_history": 200,

        # --- Админ ---
        "admin_password": "admin",
    },
    "chat": [],
}

SESSIONS: set[str] = set()
CHAT_CLIENTS: set[WebSocket] = set()
SEC_URL = {"main": "/", "info": "/info", "community": "/community", "livechat": "/livechat"}


# ============================================================
#                     БАЗОВЫЙ ШАБЛОН
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
body{font-family:{{ s.font_family }};background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column}
header{background:rgba(0,0,0,.35);backdrop-filter:blur(10px);padding:.9rem 2rem;border-bottom:1px solid rgba(255,255,255,.1);position:sticky;top:0;z-index:10}
nav{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;max-width:1200px;margin:0 auto}
nav .brand{font-weight:700;font-size:1.25rem;margin-right:auto;color:var(--primary)}
nav a{color:var(--text);text-decoration:none;padding:.5rem 1rem;border-radius:10px;transition:.2s;font-size:.95rem}
nav a:hover{background:rgba(255,255,255,.1)}
nav a.active{background:var(--primary);color:#fff}
main{flex:1;padding:2rem;max-width:1200px;margin:0 auto;width:100%}
footer{text-align:center;padding:1.5rem;opacity:.5;font-size:.9rem;border-top:1px solid rgba(255,255,255,.1)}
.card{background:rgba(255,255,255,.05);border-radius:var(--radius);padding:2rem;margin-bottom:1.5rem;border:1px solid rgba(255,255,255,.1)}
.hero{display:flex;gap:2rem;align-items:center;flex-wrap:wrap}
.hero .avatar{width:130px;height:130px;border-radius:50%;object-fit:cover;border:3px solid var(--primary);flex-shrink:0;background:rgba(0,0,0,.3)}
.hero h1{color:var(--primary);font-size:2.4rem;margin-bottom:.3rem;line-height:1.1}
.hero .tagline{opacity:.6;margin-bottom:.7rem;font-size:.95rem}
.hero .desc{opacity:.9;line-height:1.6;font-size:1.1rem}
.cover{width:100%;max-height:400px;object-fit:cover;border-radius:calc(var(--radius) - 4px);margin-bottom:1.5rem}
.btn{background:var(--primary);color:#fff;border:none;padding:.7rem 1.4rem;border-radius:10px;cursor:pointer;font-size:1rem;font-weight:500;transition:.2s;font-family:inherit}
.btn:hover{filter:brightness(1.15)}
.btn.ghost{background:transparent;border:1px solid var(--primary);color:var(--primary)}
.btn.danger{background:#ef4444}
input,textarea,select{background:rgba(0,0,0,.3);color:var(--text);border:1px solid rgba(255,255,255,.2);padding:.7rem;border-radius:10px;width:100%;font-family:inherit;font-size:1rem}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--primary)}
textarea{min-height:130px;resize:vertical;font-family:inherit}
label{display:block;margin-bottom:.4rem;font-weight:500;opacity:.9;font-size:.95rem}
.field{margin-bottom:1rem}
.chat-box{background:rgba(0,0,0,.3);border-radius:var(--radius);padding:1rem;height:500px;overflow-y:auto;margin-bottom:1rem}
.msg{padding:.55rem .85rem;margin-bottom:.5rem;border-radius:10px;background:rgba(255,255,255,.05);word-wrap:break-word;line-height:1.4}
.msg .nick{color:var(--primary);font-weight:700;margin-right:.5rem}
.msg .time{font-size:.75rem;opacity:.5;margin-left:.5rem}
.chat-input{display:flex;gap:.5rem;flex-wrap:wrap}
.chat-input input{flex:1;min-width:120px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem}
@media(max-width:750px){.grid2,.grid3{grid-template-columns:1fr}}
.alert{padding:1rem;border-radius:10px;margin-bottom:1rem}
.alert.ok{background:rgba(34,197,94,.2);border:1px solid #22c55e}
.alert.err{background:rgba(239,68,68,.2);border:1px solid #ef4444}
.photo-preview{max-width:200px;max-height:200px;border-radius:10px;object-fit:cover;border:1px solid rgba(255,255,255,.2);margin-bottom:.5rem;display:block}
.section-row{display:flex;gap:1rem;align-items:center;padding:.6rem 0;border-bottom:1px solid rgba(255,255,255,.07)}
.section-row .check{width:auto}
h2{margin-bottom:1rem}
.muted{opacity:.6;font-size:.9rem}
</style>
</head>
<body>
<header>
<nav>
  <span class="brand">{{ s.site_title }}</span>
  {% for sec in sections if sec.visible %}
    <a href="{{ sec.url }}" class="{{ 'active' if sec.id == current else '' }}">{{ sec.name }}</a>
  {% endfor %}
  <a href="/admin">⚙ Админ</a>
</nav>
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
        avatar = ('<div class="avatar" style="display:flex;align-items:center;'
                  'justify-content:center;font-size:3.5rem">👤</div>')
    cover = f'<img class="cover" src="{s["main_photo"]}" alt="cover">' if s["main_photo"] else ""
    return f"""
    <div class="card">
      {cover}
      <div class="hero">
        {avatar}
        <div style="flex:1;min-width:250px">
          <h1>{s['author_nick']}</h1>
          <div class="tagline">{s['author_tagline']}</div>
          <div class="desc">{s['channel_description']}</div>
        </div>
      </div>
    </div>
    """


def page_info_html() -> str:
    return f"""
    <div class="card">
      <h1 style="color:var(--primary);margin-bottom:1rem">Инфо</h1>
      <div>{STATE['settings']['info_content']}</div>
    </div>
    """


def page_community_html() -> str:
    return f"""
    <div class="card">
      <h1 style="color:var(--primary);margin-bottom:1rem">Сообщество</h1>
      <div>{STATE['settings']['community_content']}</div>
    </div>
    """


def page_livechat_html() -> str:
    s = STATE["settings"]
    return f"""
    <div class="card">
      <h1 style="color:var(--primary);margin-bottom:1rem">Лайв чат</h1>
      <p class="muted" style="margin-bottom:1rem">{s['chat_welcome']}</p>
      <div class="chat-box" id="chat"></div>
      <div class="chat-input">
        <input id="nick" placeholder="Ваш ник" style="max-width:220px">
        <input id="msg" placeholder="Сообщение..." autocomplete="off">
        <button class="btn" onclick="send()">Отправить</button>
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
        const n = document.createElement('span'); n.className = 'nick'; n.textContent = m.nick;
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
        <div class="card" style="max-width:420px;margin:2rem auto">
          <h2>Вход в админку</h2>
          {f'<div class="alert err">{error}</div>' if error else ''}
          <form method="post" action="/admin/login">
            <div class="field">
              <label>Пароль</label>
              <input type="password" name="password" autofocus>
            </div>
            <button class="btn" type="submit">Войти</button>
          </form>
          <p class="muted" style="margin-top:1rem">Пароль по умолчанию: <code>admin</code></p>
        </div>
        """

    sections = s["sections"]
    sec_rows = ""
    for key, label in [("main","Главная"),("info","Инфо"),("community","Сообщество"),("livechat","Лайв чат")]:
        sd = sections[key]
        sec_rows += f"""
        <div class="section-row">
          <div style="width:130px"><b>{label}</b></div>
          <div style="flex:1"><input name="{key}_name" value="{sd['name']}" placeholder="Название"></div>
          <div style="width:100px"><input type="number" name="{key}_order" value="{sd['order']}" placeholder="Порядок"></div>
          <label style="display:flex;align-items:center;gap:.4rem;margin:0">
            <input class="check" type="checkbox" name="{key}_visible" {'checked' if sd['visible'] else ''}>
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
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
        <h2>⚙ Панель администратора</h2>
        <form method="post" action="/admin/logout" style="margin:0">
          <button class="btn ghost" type="submit">Выйти</button>
        </form>
      </div>
      {f'<div class="alert ok">{message}</div>' if message else ''}
      {f'<div class="alert err">{error}</div>' if error else ''}
    </div>

    <form method="post" action="/admin/save">

      <div class="card">
        <h2>🏠 Основное</h2>
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

      <div class="card">
        <h2>🎨 Стили</h2>
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

      <div class="card">
        <h2>📄 Содержимое страниц</h2>
        <div class="field"><label>Инфо (можно HTML)</label>
          <textarea name="info_content" style="min-height:180px">{s['info_content']}</textarea></div>
        <div class="field"><label>Сообщество (можно HTML)</label>
          <textarea name="community_content" style="min-height:180px">{s['community_content']}</textarea></div>
      </div>

      <div class="card">
        <h2>💬 Лайв чат</h2>
        <div class="grid2">
          <div class="field"><label>Приветствие</label>
            <input name="chat_welcome" value="{s['chat_welcome']}"></div>
          <div class="field"><label>Макс. история сообщений</label>
            <input type="number" name="chat_max_history" value="{s['chat_max_history']}"></div>
        </div>
      </div>

      <div class="card">
        <h2>🧩 Разделы</h2>
        <p class="muted" style="margin-bottom:1rem">Название, порядок и видимость в верхнем меню</p>
        {sec_rows}
      </div>

      <div class="card">
        <h2>🔐 Пароль администратора</h2>
        <div class="field"><label>Новый пароль (оставь пустым, чтобы не менять)</label>
          <input name="new_password" type="password"></div>
      </div>

      <div class="card" style="position:sticky;bottom:1rem">
        <button class="btn" type="submit" style="width:100%;padding:1rem;font-size:1.1rem">💾 Сохранить всё</button>
      </div>
    </form>

    <div class="card">
      <h2>🖼 Фотографии</h2>
      <div class="grid2">
        <div>
          <label>Фото автора (аватар)</label>
          {author_photo_block}
          <form method="post" action="/admin/upload" enctype="multipart/form-data">
            <input type="hidden" name="field" value="author_photo">
            <div class="field"><input type="file" name="file" accept="image/*" required></div>
            <button class="btn" type="submit">Загрузить</button>
          </form>
          <form method="post" action="/admin/remove_photo" style="margin-top:.5rem">
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
          <form method="post" action="/admin/remove_photo" style="margin-top:.5rem">
            <input type="hidden" name="field" value="main_photo">
            <button class="btn danger" type="submit">Удалить</button>
          </form>
        </div>
      </div>
    </div>
    """


@app.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request, admin_token: str = Cookie(None)):
    msg = request.query_params.get("saved") and "Изменения сохранены ✔" or ""
    err = ""
    if request.query_params.get("error") == "1":
        err = "Неверный пароль"
    if request.query_params.get("error") == "toobig":
        err = "Файл слишком большой (макс. 5 МБ)"
    logged = admin_token in SESSIONS
    return HTMLResponse(render_base("Админ", admin_page_html(logged, msg or "", err), ""))


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

    # Основное
    s["site_title"] = get("site_title", s["site_title"])
    s["author_nick"] = get("author_nick", s["author_nick"])
    s["author_tagline"] = get("author_tagline", s["author_tagline"])
    s["channel_description"] = get("channel_description", s["channel_description"])
    s["footer_text"] = get("footer_text", s["footer_text"])

    # Стили
    s["primary_color"] = get("primary_color", s["primary_color"])
    s["bg_color"] = get("bg_color", s["bg_color"])
    s["text_color"] = get("text_color", s["text_color"])
    s["font_family"] = get("font_family", s["font_family"])
    s["border_radius"] = get("border_radius", s["border_radius"])

    # Контент
    s["info_content"] = get("info_content", s["info_content"])
    s["community_content"] = get("community_content", s["community_content"])

    # Чат
    s["chat_welcome"] = get("chat_welcome", s["chat_welcome"])
    try:
        s["chat_max_history"] = int(get("chat_max_history", s["chat_max_history"]))
    except (TypeError, ValueError):
        pass

    # Разделы
    for key in ("main", "info", "community", "livechat"):
        s["sections"][key]["name"] = get(f"{key}_name", s["sections"][key]["name"]) or s["sections"][key]["name"]
        try:
            s["sections"][key]["order"] = int(get(f"{key}_order", s["sections"][key]["order"]))
        except (TypeError, ValueError):
            pass
        s["sections"][key]["visible"] = f"{key}_visible" in form

    # Пароль
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


# ============================================================
#                     ЗАПУСК
# ============================================================
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
