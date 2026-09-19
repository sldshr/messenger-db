"""
SldChat — простой мессенджер на Python + FastAPI.
Запуск:  python main.py     (или: uvicorn main:app --host 0.0.0.0 --port 8000)
Всё хранится только в оперативной памяти процесса.
"""

import secrets
import time
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(title="SldChat")

# ================== ХРАНИЛИЩЕ (в оперативке) ==================
users: Dict[str, dict] = {}            # nick -> {"password","created","last_seen"}
sessions: Dict[str, str] = {}          # token -> nick
messages: List[dict] = []              # {"id","from","to","text","time","deleted"}
reads: Dict[str, Dict[str, int]] = {}  # reader -> peer -> last_read_msg_id
deleted_ids: List[int] = []            # для синхронизации удалений
_msg_id = 0
ONLINE_WINDOW = 60                     # секунд до "оффлайн"


def current_user(request: Request) -> Optional[str]:
    token = request.cookies.get("session")
    return sessions.get(token) if token else None


def user_public_info(nick: str) -> dict:
    u = users.get(nick)
    if not u:
        return {}
    now = time.time()
    return {
        "nick": nick,
        "online": (now - u.get("last_seen", 0)) < ONLINE_WINDOW,
        "last_seen": u.get("last_seen", 0),
        "created": u.get("created", 0),
    }


@app.middleware("http")
async def update_last_seen(request: Request, call_next):
    nick = current_user(request)
    if nick and nick in users:
        users[nick]["last_seen"] = time.time()
    return await call_next(request)


# ================== API ==================
@app.post("/api/register")
async def api_register(nick: str = Form(...), password: str = Form(...)):
    nick = nick.strip()
    if not nick or not password:
        return JSONResponse({"ok": False, "error": "Заполните все поля"}, status_code=400)
    if not (3 <= len(nick) <= 20):
        return JSONResponse({"ok": False, "error": "Ник: 3–20 символов"}, status_code=400)
    if not nick.replace("_", "").isalnum():
        return JSONResponse({"ok": False, "error": "Ник: только буквы, цифры и _"}, status_code=400)
    if len(password) < 3:
        return JSONResponse({"ok": False, "error": "Пароль: минимум 3 символа"}, status_code=400)
    if nick in users:
        return JSONResponse({"ok": False, "error": "Ник уже занят"}, status_code=400)

    now = time.time()
    users[nick] = {"password": password, "created": now, "last_seen": now}

    # Автовход после регистрации
    token = secrets.token_hex(16)
    sessions[token] = nick
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/api/login")
async def api_login(nick: str = Form(...), password: str = Form(...)):
    nick = nick.strip()
    u = users.get(nick)
    if not u or u["password"] != password:
        return JSONResponse({"ok": False, "error": "Неверный ник или пароль"}, status_code=400)
    token = secrets.token_hex(16)
    sessions[token] = nick
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
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

    my_reads = reads.get(me, {})
    result = []
    for nick in users:
        if nick == me:
            continue
        last_msg = None
        unread = 0
        last_read_id = my_reads.get(nick, 0)
        for m in messages:
            if m.get("deleted"):
                continue
            if (m["from"] == nick and m["to"] == me) or (m["from"] == me and m["to"] == nick):
                last_msg = m
                if m["from"] == nick and m["to"] == me and m["id"] > last_read_id:
                    unread += 1
        info = user_public_info(nick)
        result.append({
            "nick": nick,
            "last": last_msg,
            "unread": unread,
            "online": info["online"],
        })
    result.sort(key=lambda u: (u["last"]["id"] if u["last"] else 0), reverse=True)
    return {"ok": True, "users": result}


@app.get("/api/user/{nick}/info")
async def api_user_info(nick: str, request: Request):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)
    if nick not in users:
        return JSONResponse({"ok": False, "error": "Не найден"}, status_code=404)
    info = user_public_info(nick)
    info["msg_count"] = sum(
        1 for m in messages
        if not m.get("deleted") and (
            (m["from"] == me and m["to"] == nick) or
            (m["from"] == nick and m["to"] == me)
        )
    )
    return {"ok": True, "info": info}


@app.get("/api/dialog/{nick}")
async def api_dialog(nick: str, request: Request, since: int = 0):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)

    # отмечаем прочитанным
    if nick in users:
        max_id = reads.setdefault(me, {}).get(nick, 0)
        for m in messages:
            if m["from"] == nick and m["to"] == me and m["id"] > max_id:
                max_id = m["id"]
        reads[me][nick] = max_id

    out = [
        m for m in messages
        if m["id"] > since and (
            (m["from"] == me and m["to"] == nick) or
            (m["from"] == nick and m["to"] == me)
        )
    ]
    return {"ok": True, "messages": out, "deleted": list(deleted_ids)}


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
        "deleted": False,
    })
    return {"ok": True, "id": _msg_id}


@app.delete("/api/message/{msg_id}")
async def api_delete_message(msg_id: int, request: Request):
    me = current_user(request)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)
    for m in messages:
        if m["id"] == msg_id:
            if m["from"] != me:
                return JSONResponse({"ok": False, "error": "Не ваше сообщение"}, status_code=403)
            m["deleted"] = True
            m["text"] = ""
            if msg_id not in deleted_ids:
                deleted_ids.append(msg_id)
            return {"ok": True}
    return JSONResponse({"ok": False, "error": "Не найдено"}, status_code=404)


# ================== HTML: ГЛАВНАЯ ==================
LANDING_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SldChat — простой мессенджер</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: Tahoma, Verdana, Arial, sans-serif;
    font-size: 14px; color: #2b3a4a; line-height: 1.55;
    background: #eef2f6;
  }
  a { color: #3f6fa8; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .container { max-width: 980px; margin: 0 auto; padding: 0 20px; }

  /* ---- Верхняя панель ---- */
  .topbar {
    background: linear-gradient(#fbfcfd, #dce3ea);
    border-bottom: 1px solid #b7c2cd;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    position: sticky; top: 0; z-index: 50;
  }
  .topbar-inner {
    display: flex; align-items: center; justify-content: space-between;
    height: 54px;
  }
  .logo {
    font-size: 22px; font-weight: bold; color: #2b3a4a;
    text-shadow: 0 1px 0 #fff; letter-spacing: 1px;
  }
  .logo span { color: #3f6fa8; }
  .nav { display: flex; align-items: center; gap: 6px; }
  .nav a.navlink {
    padding: 7px 10px; border-radius: 4px; color: #3a5169;
    font-size: 13px;
  }
  .nav a.navlink:hover { background: #e3e9ef; text-decoration: none; }

  .btn {
    display: inline-block; padding: 7px 14px; border-radius: 4px;
    border: 1px solid #8b97a3; font-size: 13px; cursor: pointer;
    font-family: inherit; text-shadow: 0 1px 0 rgba(255,255,255,0.6);
    text-decoration: none !important;
    background: linear-gradient(#fbfcfd, #cfd8e0); color: #2b3a4a;
  }
  .btn:hover { background: linear-gradient(#fff, #dbe3ea); }
  .btn-primary {
    background: linear-gradient(#5b8fc4, #3f6fa8);
    border-color: #35597f; color: #fff; text-shadow: 0 1px 0 rgba(0,0,0,0.2);
  }
  .btn-primary:hover { background: linear-gradient(#699bcd, #4577b1); }
  .btn-lg { padding: 11px 22px; font-size: 15px; }

  /* ---- Hero ---- */
  .hero {
    background:
      radial-gradient(circle at 20% 20%, #e3ecf5 0%, transparent 60%),
      linear-gradient(#d5dfe9, #b8c6d3);
    border-bottom: 1px solid #a8b5c2;
    padding: 70px 0 80px;
    text-align: center;
  }
  .hero h1 {
    font-size: 42px; font-weight: normal; margin: 0 0 16px;
    color: #23374b; text-shadow: 0 1px 0 #fff; line-height: 1.15;
    letter-spacing: 0.5px;
  }
  .hero p {
    max-width: 540px; margin: 0 auto 28px; color: #4a5f74; font-size: 16px;
  }
  .hero-actions { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }

  /* ---- Секции ---- */
  .section { padding: 55px 0; border-bottom: 1px solid #dbe1e7; }
  .section:nth-child(even) { background: #f6f8fa; }
  .section h2 {
    font-size: 26px; font-weight: normal; margin: 0 0 18px; color: #23374b;
    text-shadow: 0 1px 0 #fff;
  }
  .section p { margin: 0 0 12px; color: #47586c; }

  .cards {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px;
    margin-top: 24px;
  }
  .card {
    background: #fff; border: 1px solid #cfd7df; border-radius: 6px;
    padding: 18px 18px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05), inset 0 1px 0 #fff;
  }
  .card h3 {
    margin: 0 0 8px; font-size: 15px; color: #2b3a4a;
  }
  .card p { margin: 0; font-size: 13px; color: #5a6c80; }
  .card .ico {
    width: 34px; height: 34px; margin-bottom: 10px;
    border-radius: 6px;
    background: linear-gradient(#e4ebf2, #c8d3de);
    border: 1px solid #b7c2cd;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px; color: #3f6fa8;
  }

  /* ---- FAQ ---- */
  .faq { max-width: 720px; margin: 0 auto; }
  .faq details {
    background: #fff; border: 1px solid #cfd7df; border-radius: 5px;
    margin-bottom: 10px; padding: 0;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }
  .faq summary {
    padding: 12px 16px; cursor: pointer; font-weight: bold; color: #2b3a4a;
    outline: none; list-style: none; position: relative;
  }
  .faq summary::-webkit-details-marker { display: none; }
  .faq summary::before {
    content: '+'; display: inline-block; width: 18px;
    color: #3f6fa8; font-weight: bold;
  }
  .faq details[open] summary::before { content: '−'; }
  .faq .answer {
    padding: 0 16px 14px 34px; color: #55677b; font-size: 13px;
  }

  /* ---- Футер ---- */
  footer {
    background: #2b3a4a; color: #b8c4ce; padding: 28px 0; text-align: center;
    font-size: 12px;
  }
  footer a { color: #9db8d3; }

  @media (max-width: 760px) {
    .nav a.navlink { display: none; }
    .hero { padding: 45px 0 55px; }
    .hero h1 { font-size: 28px; }
    .hero p { font-size: 14px; }
    .cards { grid-template-columns: 1fr; }
    .section { padding: 35px 0; }
    .section h2 { font-size: 22px; }
  }
</style>
</head>
<body>

<header class="topbar">
  <div class="container topbar-inner">
    <a href="/" class="logo">Sld<span>Chat</span></a>
    <nav class="nav">
      <a class="navlink" href="#about">О нас</a>
      <a class="navlink" href="#app">О приложении</a>
      <a class="navlink" href="#faq">Ответы на вопросы</a>
      <a class="btn" href="/login">Войти</a>
      <a class="btn btn-primary" href="/register">Регистрация</a>
    </nav>
  </div>
</header>

<section class="hero">
  <div class="container">
    <h1>SldChat — простой<br>мессенджер без лишнего</h1>
    <p>Регистрация за 5 секунд — и сразу в чат. Пишите людям по нику, а всё лишнее мы убрали.</p>
    <div class="hero-actions">
      <a class="btn btn-primary btn-lg" href="/register">Создать аккаунт</a>
      <a class="btn btn-lg" href="/login">У меня уже есть аккаунт</a>
    </div>
  </div>
</section>

<section id="about" class="section">
  <div class="container">
    <h2>О нас</h2>
    <p>SldChat — небольшой открытый проект, сделанный на Python и FastAPI. Мы верим, что
    мессенджер не обязан быть перегруженным: достаточно ника, пароля и пары секунд на вход.</p>
    <p>Проект создан энтузиастами как учебный пример и как рабочий инструмент для общения
    внутри небольших команд и компаний друзей.</p>
  </div>
</section>

<section id="app" class="section">
  <div class="container">
    <h2>О приложении</h2>
    <p>Клиент работает прямо в браузере — на компьютере и на телефоне. Данные хранятся
    исключительно в оперативной памяти сервера, поэтому сразу после его остановки
    история не сохраняется.</p>

    <div class="cards">
      <div class="card">
        <div class="ico">👤</div>
        <h3>Простой вход</h3>
        <p>Ник и пароль. Зарегистрировался — и сразу в чат, никаких подтверждений по почте.</p>
      </div>
      <div class="card">
        <div class="ico">💬</div>
        <h3>Личные сообщения</h3>
        <p>Пишите любому пользователю по нику. Видите, кто онлайн, а кто давно не заходил.</p>
      </div>
      <div class="card">
        <div class="ico">📱</div>
        <h3>Везде и всюду</h3>
        <p>Адаптивный интерфейс: удобно и на большом мониторе, и на смартфоне.</p>
      </div>
    </div>
  </div>
</section>

<section id="faq" class="section">
  <div class="container">
    <h2>Ответы на вопросы</h2>
    <div class="faq">
      <details>
        <summary>Сколько стоит SldChat?</summary>
        <div class="answer">Нисколько. Проект полностью бесплатный и без рекламы.</div>
      </details>
      <details>
        <summary>Сохраняются ли мои сообщения?</summary>
        <div class="answer">Нет. Всё хранится в оперативной памяти сервера и исчезает при его перезапуске.</div>
      </details>
      <details>
        <summary>Можно ли писать человеку, которого я не знаю?</summary>
        <div class="answer">Да, достаточно знать его ник. Он отображается в вашем списке пользователей.</div>
      </details>
      <details>
        <summary>Как удалить сообщение?</summary>
        <div class="answer">Наведите курсор на своё сообщение и нажмите «×». Оно будет помечено как удалённое для всех.</div>
      </details>
      <details>
        <summary>Как выйти из аккаунта?</summary>
        <div class="answer">Кнопка «Выйти» находится в левом верхнем углу чата.</div>
      </details>
    </div>
  </div>
</section>

<footer>
  <div class="container">
    SldChat © 2026 · <a href="#about">О нас</a> · <a href="#faq">Ответы</a> · <a href="/register">Регистрация</a>
  </div>
</footer>

</body>
</html>
"""


# ================== HTML: АВТОРИЗАЦИЯ ==================
def render_auth(active: str) -> str:
    login_active = "active" if active == "login" else ""
    reg_active = "active" if active == "register" else ""
    login_style = "" if active == "login" else "display:none"
    reg_style = "" if active == "register" else "display:none"
    title = "Вход" if active == "login" else "Регистрация"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SldChat — {title}</title>
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; min-height: 100%; }}
  body {{
    font-family: Tahoma, Verdana, Arial, sans-serif;
    font-size: 13px; color: #2b3a4a;
    background:
      radial-gradient(circle at 30% 10%, #e6eef6 0%, transparent 55%),
      linear-gradient(#cfd9e3, #a8b6c4);
    min-height: 100vh;
    display: flex; flex-direction: column;
  }}
  a {{ color: #3f6fa8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  .topline {{
    padding: 14px 20px;
  }}
  .topline a.logo {{
    font-size: 20px; font-weight: bold; color: #2b3a4a;
    text-shadow: 0 1px 0 #fff; letter-spacing: 1px;
  }}
  .topline a.logo span {{ color: #3f6fa8; }}

  .wrap {{
    flex: 1;
    display: flex; align-items: center; justify-content: center;
    padding: 20px;
  }}

  .box {{
    width: 100%; max-width: 380px;
    background: #f4f6f8;
    border: 1px solid #8b97a3;
    border-radius: 6px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.22), inset 0 1px 0 #fff;
    padding: 22px 26px 26px;
  }}
  .box h1 {{
    text-align: center; font-size: 20px; font-weight: normal; margin: 0 0 18px;
    color: #23374b; text-shadow: 0 1px 0 #fff; letter-spacing: 1px;
  }}
  .tabs {{
    display: flex; margin-bottom: 16px;
    border-bottom: 1px solid #a8b2bc;
  }}
  .tabs button {{
    flex: 1; border: 1px solid #a8b2bc; border-bottom: none;
    background: linear-gradient(#eef1f4, #d3dae1);
    border-radius: 4px 4px 0 0;
    padding: 8px 0; margin-right: 4px; cursor: pointer;
    font-family: inherit; font-size: 13px; color: #445;
  }}
  .tabs button.active {{
    background: #f4f6f8; color: #223; font-weight: bold;
    position: relative; top: 1px;
  }}

  label {{ display: block; margin: 10px 0 4px; color: #445; }}
  input[type=text], input[type=password] {{
    width: 100%; padding: 8px 10px;
    font-family: inherit; font-size: 13px;
    border: 1px solid #9aa4ae; border-radius: 3px;
    background: #fff;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
    outline: none;
  }}
  input:focus {{ border-color: #5a7a9a; }}

  .btn {{
    display: block; width: 100%; margin-top: 18px;
    padding: 10px 0;
    border: 1px solid #7a8794; border-radius: 4px;
    background: linear-gradient(#fbfcfd, #ccd5de);
    font-family: inherit; font-size: 13px; color: #2b3a4a;
    cursor: pointer; text-shadow: 0 1px 0 #fff;
  }}
  .btn-primary {{
    background: linear-gradient(#5b8fc4, #3f6fa8);
    border-color: #35597f; color: #fff; text-shadow: 0 1px 0 rgba(0,0,0,0.2);
  }}
  .btn-primary:hover {{ background: linear-gradient(#699bcd, #4577b1); }}

  .error {{
    min-height: 18px; color: #c22; text-align: center; font-size: 12px;
    margin-bottom: 4px;
  }}
  .hint {{
    margin-top: 14px; text-align: center; color: #889; font-size: 11px;
  }}
  .back {{ text-align: center; margin-top: 14px; font-size: 12px; }}
</style>
</head>
<body>

<div class="topline"><a href="/" class="logo">Sld<span>Chat</span></a></div>

<div class="wrap">
  <div class="box">
    <h1>Добро пожаловать</h1>
    <div class="tabs">
      <button type="button" id="tabLogin" class="{login_active}">Вход</button>
      <button type="button" id="tabRegister" class="{reg_active}">Регистрация</button>
    </div>
    <div class="error" id="error"></div>

    <form id="formLogin" style="{login_style}">
      <label>Ник:</label>
      <input type="text" name="nick" autocomplete="username" maxlength="20">
      <label>Пароль:</label>
      <input type="password" name="password" autocomplete="current-password">
      <button type="submit" class="btn btn-primary">Войти</button>
    </form>

    <form id="formRegister" style="{reg_style}">
      <label>Ник:</label>
      <input type="text" name="nick" autocomplete="username" maxlength="20" placeholder="3–20 символов">
      <label>Пароль:</label>
      <input type="password" name="password" autocomplete="new-password" placeholder="минимум 3 символа">
      <button type="submit" class="btn btn-primary">Зарегистрироваться</button>
    </form>

    <div class="hint">Всё хранится только в оперативной памяти</div>
    <div class="back"><a href="/">← На главную</a></div>
  </div>
</div>

<script>
  const tabLogin = document.getElementById('tabLogin');
  const tabRegister = document.getElementById('tabRegister');
  const formLogin = document.getElementById('formLogin');
  const formRegister = document.getElementById('formRegister');
  const errorBox = document.getElementById('error');

  function showTab(which) {{
    if (which === 'login') {{
      tabLogin.classList.add('active'); tabRegister.classList.remove('active');
      formLogin.style.display = ''; formRegister.style.display = 'none';
    }} else {{
      tabRegister.classList.add('active'); tabLogin.classList.remove('active');
      formRegister.style.display = ''; formLogin.style.display = 'none';
    }}
    errorBox.textContent = '';
  }}
  tabLogin.onclick = () => {{ showTab('login'); history.replaceState(null, '', '/login'); }};
  tabRegister.onclick = () => {{ showTab('register'); history.replaceState(null, '', '/register'); }};

  async function submitForm(url, form) {{
    errorBox.textContent = '';
    const fd = new FormData(form);
    let d;
    try {{
      const r = await fetch(url, {{ method: 'POST', body: fd }});
      d = await r.json();
    }} catch (e) {{
      d = {{ ok:false, error:'Ошибка соединения' }};
    }}
    if (d.ok) {{ location.href = '/chat'; return; }}
    errorBox.textContent = d.error || 'Ошибка';
  }}

  formLogin.onsubmit = e => {{ e.preventDefault(); submitForm('/api/login', formLogin); }};
  formRegister.onsubmit = e => {{ e.preventDefault(); submitForm('/api/register', formRegister); }};
</script>
</body>
</html>
"""


# ================== HTML: ЧАТ ==================
CHAT_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>SldChat</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; overflow: hidden; }
  body {
    font-family: Tahoma, Verdana, Arial, sans-serif;
    font-size: 13px; color: #2b3a4a;
    background: #e9eef3;
  }

  /* ================== Layout ================== */
  .app {
    display: grid;
    grid-template-columns: 300px 1fr 280px;
    height: 100vh;
    width: 100vw;
    overflow: hidden;
    background: #e9eef3;
  }

  /* ================== Sidebar ================== */
  .sidebar {
    background: #f0f3f7;
    border-right: 1px solid #c8d1da;
    display: flex; flex-direction: column;
    min-width: 0;
  }
  .sb-header {
    padding: 9px 10px;
    background: linear-gradient(#fbfcfd, #d6dee5);
    border-bottom: 1px solid #b0bac4;
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px;
  }
  .sb-header .me {
    display: flex; align-items: center; gap: 8px; min-width: 0;
  }
  .sb-header .me .name {
    font-weight: bold; color: #2b3a4a; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .sb-header .logout {
    color: #52708c; cursor: pointer; font-size: 12px;
    text-decoration: underline; flex-shrink: 0;
    background: none; border: none; padding: 0;
    font-family: inherit;
  }
  .sb-header .logout:hover { color: #2b3a4a; }

  .search {
    padding: 7px 9px;
    border-bottom: 1px solid #d3dae0;
    background: #eef2f6;
  }
  .search input {
    width: 100%; padding: 6px 9px;
    font-family: inherit; font-size: 12px;
    border: 1px solid #b5bec8; border-radius: 14px;
    background: #fff; outline: none;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.06);
  }
  .search input:focus { border-color: #5a7a9a; }

  .user-list { flex: 1; overflow-y: auto; }
  .user-item {
    padding: 9px 11px;
    border-bottom: 1px solid #dde3e9;
    background: #f6f8fa;
    cursor: pointer;
    display: flex; gap: 9px; align-items: center;
    min-width: 0;
  }
  .user-item:hover  { background: #eaf0f6; }
  .user-item.active { background: #d3e0ec; }
  .user-item .avatar { flex-shrink: 0; }
  .user-item .body { min-width: 0; flex: 1; }
  .user-item .row1 {
    display: flex; justify-content: space-between; align-items: center; gap: 6px;
  }
  .user-item .nick {
    font-weight: bold; color: #2b3a4a;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-size: 13px;
  }
  .user-item .time {
    font-size: 10px; color: #8a94a0; flex-shrink: 0;
  }
  .user-item .preview {
    color: #7a8695; font-size: 11px; margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .user-item .badge {
    background: #3f6fa8; color: #fff; font-size: 10px;
    border-radius: 9px; padding: 1px 6px; min-width: 18px;
    text-align: center; margin-left: 4px; flex-shrink: 0;
  }

  .avatar {
    width: 34px; height: 34px; border-radius: 50%;
    background: linear-gradient(#cfdbe6, #a9bacb);
    border: 1px solid #9aa9b8;
    color: #fff; display: flex; align-items: center; justify-content: center;
    font-weight: bold; font-size: 14px; text-shadow: 0 1px 0 rgba(0,0,0,0.15);
    position: relative;
  }
  .avatar.sm { width: 28px; height: 28px; font-size: 12px; }
  .avatar.lg { width: 72px; height: 72px; font-size: 28px; }
  .dot {
    position: absolute; right: -1px; bottom: -1px;
    width: 10px; height: 10px; border-radius: 50%;
    border: 2px solid #f0f3f7; background: #9aa4ae;
  }
  .dot.on { background: #4caf50; }

  /* ================== Chat ================== */
  .chat {
    display: flex; flex-direction: column;
    background: #fff; min-width: 0;
  }
  .chat-header {
    padding: 9px 12px;
    background: linear-gradient(#fbfcfd, #d6dee5);
    border-bottom: 1px solid #b0bac4;
    display: flex; align-items: center; gap: 10px;
    min-height: 52px;
  }
  .chat-header .title {
    font-weight: bold; color: #2b3a4a; flex: 1;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .chat-header .sub {
    font-size: 11px; color: #7a8695; font-weight: normal;
  }
  .icon-btn {
    display: none;
    border: 1px solid #b5bec8; border-radius: 4px;
    background: linear-gradient(#fbfcfd, #dce3ea);
    padding: 5px 9px; cursor: pointer;
    font-family: inherit; font-size: 15px; color: #3a5169;
    line-height: 1;
  }

  .messages {
    flex: 1; overflow-y: auto;
    padding: 14px 18px;
    background:
      radial-gradient(circle at 80% 10%, #f6f9fc 0%, transparent 60%),
      #fbfcfd;
  }
  .empty {
    color: #a2aab3; text-align: center; margin-top: 60px;
    font-style: italic; font-size: 13px;
  }

  .date-sep { text-align: center; margin: 14px 0 10px; }
  .date-sep span {
    background: #eef2f6; color: #6f7c8b;
    padding: 3px 11px; border-radius: 10px; font-size: 11px;
    border: 1px solid #dde4eb;
  }

  .msg-row {
    display: flex; margin-bottom: 6px;
    align-items: flex-end; gap: 6px;
  }
  .msg-row.mine { justify-content: flex-end; }

  .msg-bubble {
    max-width: min(70%, 520px);
    padding: 7px 11px;
    border-radius: 14px 14px 14px 4px;
    background: #eef2f6;
    border: 1px solid #dde4eb;
    font-size: 13px; line-height: 1.4;
    word-wrap: break-word; white-space: pre-wrap;
    color: #22303e;
    position: relative;
  }
  .msg-row.mine .msg-bubble {
    background: #d3e6c8;
    border-color: #bed7ad;
    border-radius: 14px 14px 4px 14px;
  }
  .msg-meta {
    font-size: 10px; color: #8a94a0; margin-top: 3px;
  }
  .msg-row.mine .msg-meta { text-align: right; }
  .msg-row.mine .msg-meta .edited { color: #7a8695; }

  .msg-bubble.deleted {
    background: transparent;
    border: 1px dashed #c8d1da;
    color: #98a2ad; font-style: italic;
  }

  .msg-del {
    position: absolute; top: -7px; right: -7px;
    width: 18px; height: 18px; border-radius: 50%;
    background: #c33; color: #fff; border: 2px solid #fff;
    font-size: 11px; line-height: 1; padding: 0;
    cursor: pointer; display: none;
    align-items: center; justify-content: center;
    font-family: inherit;
  }
  .msg-row.mine:hover .msg-del { display: flex; }

  .input-area {
    border-top: 1px solid #c8d1da;
    padding: 10px 12px;
    background: #eef2f6;
    display: flex; gap: 8px;
    align-items: flex-end;
  }
  .input-area textarea {
    flex: 1; resize: none;
    padding: 9px 12px;
    font-family: inherit; font-size: 13px;
    border: 1px solid #b5bec8; border-radius: 16px;
    background: #fff;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.06);
    outline: none;
    max-height: 120px; min-height: 34px;
    line-height: 1.4;
  }
  .input-area textarea:focus { border-color: #5a7a9a; }
  .input-area button {
    padding: 8px 18px; border-radius: 16px;
    border: 1px solid #35597f;
    background: linear-gradient(#5b8fc4, #3f6fa8);
    color: #fff; cursor: pointer;
    font-family: inherit; font-size: 13px;
    text-shadow: 0 1px 0 rgba(0,0,0,0.2);
    flex-shrink: 0;
  }
  .input-area button:hover { background: linear-gradient(#699bcd, #4577b1); }

  /* ================== Info panel ================== */
  .info-panel {
    background: #f0f3f7;
    border-left: 1px solid #c8d1da;
    padding: 18px 16px;
    overflow-y: auto;
    min-width: 0;
  }
  .info-panel .close-btn { display: none; }
  .info-empty {
    color: #98a2ad; text-align: center; margin-top: 40px;
    font-size: 12px; font-style: italic;
  }
  .info-head {
    text-align: center;
    padding-bottom: 16px;
    border-bottom: 1px solid #dbe1e7;
    margin-bottom: 16px;
  }
  .info-head .avatar.lg { margin: 0 auto 10px; }
  .info-head .name {
    font-size: 16px; font-weight: bold; color: #23374b;
    word-break: break-all;
  }
  .info-head .status { font-size: 12px; margin-top: 4px; color: #7a8695; }
  .info-head .status.online { color: #2f8f3d; font-weight: bold; }
  .info-row {
    display: flex; justify-content: space-between;
    padding: 8px 0; border-bottom: 1px solid #e0e6ec;
    font-size: 12px; gap: 8px;
  }
  .info-row .k { color: #7a8695; flex-shrink: 0; }
  .info-row .v { color: #2b3a4a; text-align: right; word-break: break-word; }

  /* ================== Mobile ================== */
  @media (max-width: 900px) {
    .app {
      grid-template-columns: 1fr;
      position: relative;
    }
    .sidebar { border-right: none; }
    .chat { display: none; border-left: none; }
    .info-panel {
      position: fixed; top: 0; right: 0; bottom: 0;
      width: min(300px, 85vw);
      z-index: 40;
      transform: translateX(100%);
      transition: transform 0.22s ease;
      box-shadow: -4px 0 14px rgba(0,0,0,0.18);
      border-left: 1px solid #b0bac4;
    }
    .app.chat-open .sidebar { display: none; }
    .app.chat-open .chat { display: flex; }
    .app.info-open .info-panel { transform: translateX(0); }
    .icon-btn { display: inline-block; }
    .info-panel .close-btn {
      display: block; float: right; margin: -4px -4px 0 0;
    }
    .msg-bubble { max-width: 82%; }
  }

  @media (max-width: 400px) {
    .sb-header .me .name { font-size: 12px; }
  }

  /* Скроллбары — по-старому */
  .user-list::-webkit-scrollbar,
  .messages::-webkit-scrollbar,
  .info-panel::-webkit-scrollbar { width: 10px; }
  .user-list::-webkit-scrollbar-track,
  .messages::-webkit-scrollbar-track,
  .info-panel::-webkit-scrollbar-track { background: #eef2f6; }
  .user-list::-webkit-scrollbar-thumb,
  .messages::-webkit-scrollbar-thumb,
  .info-panel::-webkit-scrollbar-thumb {
    background: #c1cbd5; border-radius: 5px; border: 2px solid #eef2f6;
  }
</style>
</head>
<body>

<div class="app" id="app">

  <!-- ======== Левый сайдбар ======== -->
  <aside class="sidebar">
    <div class="sb-header">
      <div class="me">
        <div class="avatar sm" id="myAvatar">?</div>
        <div class="name" id="myNick">…</div>
      </div>
      <button class="logout" id="logoutBtn">Выйти</button>
    </div>

    <div class="search">
      <input type="text" id="searchInput" placeholder="Поиск по нику..." autocomplete="off">
    </div>

    <div class="user-list" id="userList"></div>
  </aside>

  <!-- ======== Чат ======== -->
  <main class="chat" id="chatMain">
    <header class="chat-header">
      <button class="icon-btn" id="backBtn" title="Назад">←</button>
      <div class="title">
        <div id="chatTitle">Выберите собеседника</div>
        <div class="sub" id="chatSub"></div>
      </div>
      <button class="icon-btn" id="infoBtn" title="Информация">ⓘ</button>
    </header>

    <div class="messages" id="messages">
      <div class="empty">Слева выберите пользователя, чтобы начать переписку</div>
    </div>

    <div class="input-area" id="inputArea" style="display:none">
      <textarea id="msgInput" placeholder="Введите сообщение..." rows="1"></textarea>
      <button id="sendBtn">Отправить</button>
    </div>
  </main>

  <!-- ======== Правая панель ======== -->
  <aside class="info-panel" id="infoPanel">
    <button class="icon-btn close-btn" id="infoCloseBtn">×</button>
    <div id="infoContent">
      <div class="info-empty">Информация о собеседнике появится здесь</div>
    </div>
  </aside>

</div>

<script>
/* ==================== Состояние ==================== */
let me = null;
let current = null;         // ник собеседника
const lastIds = {};         // ник -> last message id
const renderedIds = new Set();
let deletedSeen = new Set();
let polling = false;
let usersCache = [];
let searchQuery = '';

const $ = id => document.getElementById(id);
const app = $('app');

/* ==================== Утилиты ==================== */
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
function pad2(n) { return String(n).padStart(2, '0'); }
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}
function fmtDateTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear();
}
function fmtLastSeen(ts) {
  if (!ts) return 'давно';
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return 'только что';
  if (diff < 3600) return Math.floor(diff / 60) + ' мин назад';
  if (diff < 86400) return Math.floor(diff / 3600) + ' ч назад';
  return fmtDateTime(ts);
}
function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const yest = new Date(); yest.setDate(yest.getDate() - 1);
  const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (same(d, today)) return 'Сегодня';
  if (same(d, yest)) return 'Вчера';
  return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear();
}
function initials(nick) {
  return (nick || '?').slice(0, 1).toUpperCase();
}
function avatarHtml(nick, online, size) {
  const cls = size ? 'avatar ' + size : 'avatar';
  const dot = online === undefined ? '' :
    '<span class="dot' + (online ? ' on' : '') + '"></span>';
  return '<div class="' + cls + '">' + esc(initials(nick)) + dot + '</div>';
}
function scrollIfNearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 120;
}

/* ==================== Инициализация ==================== */
async function init() {
  const r = await fetch('/api/me');
  if (!r.ok) { location.href = '/'; return; }
  const d = await r.json();
  me = d.nick;
  $('myNick').textContent = me;
  $('myAvatar').textContent = initials(me);
  await loadUsers();
}

/* ==================== Список пользователей ==================== */
async function loadUsers() {
  const r = await fetch('/api/users');
  if (!r.ok) { location.href = '/'; return; }
  const d = await r.json();
  usersCache = d.users || [];
  renderUsers();
}

function renderUsers() {
  const list = $('userList');
  list.innerHTML = '';
  const q = searchQuery.trim().toLowerCase();
  const filtered = q
    ? usersCache.filter(u => u.nick.toLowerCase().includes(q))
    : usersCache;

  if (!filtered.length) {
    const e = document.createElement('div');
    e.style.padding = '14px';
    e.style.color = '#889';
    e.style.fontSize = '12px';
    e.style.textAlign = 'center';
    e.textContent = q ? 'Никого не найдено' : 'Других пользователей пока нет';
    list.appendChild(e);
    return;
  }

  for (const u of filtered) {
    const div = document.createElement('div');
    div.className = 'user-item' + (u.nick === current ? ' active' : '');
    let preview = 'Нет сообщений';
    if (u.last) {
      const prefix = u.last.from === me ? 'Вы: ' : '';
      preview = prefix + (u.last.deleted ? 'сообщение удалено' : u.last.text);
    }
    const timeStr = u.last ? fmtTime(u.last.time) : '';
    const unread = u.unread
      ? '<span class="badge">' + (u.unread > 99 ? '99+' : u.unread) + '</span>'
      : '';

    div.innerHTML =
      avatarHtml(u.nick, u.online) +
      '<div class="body">' +
        '<div class="row1">' +
          '<div class="nick">' + esc(u.nick) + '</div>' +
          '<div class="time">' + timeStr + unread + '</div>' +
        '</div>' +
        '<div class="preview">' + esc(preview) + '</div>' +
      '</div>';
    div.onclick = () => openDialog(u.nick);
    list.appendChild(div);
  }
}

/* ==================== Открытие диалога ==================== */
async function openDialog(nick) {
  current = nick;
  lastIds[nick] = 0;
  renderedIds.clear();
  deletedSeen = new Set();

  $('chatTitle').textContent = nick;
  $('chatSub').textContent = '';
  const box = $('messages');
  box.innerHTML = '';
  $('inputArea').style.display = 'flex';

  app.classList.add('chat-open');
  app.classList.remove('info-open');

  await loadInfo(nick);
  await refreshDialog();

  $('msgInput').focus();
  await loadUsers();
}

async function refreshDialog() {
  if (!current) return;
  const nick = current;
  const since = lastIds[nick] || 0;
  const r = await fetch('/api/dialog/' + encodeURIComponent(nick) + '?since=' + since);
  if (!r.ok) return;
  const d = await r.json();
  if (!d.ok || current !== nick) return;

  const box = $('messages');
  const wasNearBottom = scrollIfNearBottom(box);

  // удаления
  for (const id of d.deleted) {
    if (deletedSeen.has(id)) continue;
    deletedSeen.add(id);
    const el = box.querySelector('.msg-row[data-id="' + id + '"]');
    if (el) {
      const bubble = el.querySelector('.msg-bubble');
      bubble.classList.add('deleted');
      bubble.textContent = 'Сообщение удалено';
      const delBtn = el.querySelector('.msg-del');
      if (delBtn) delBtn.remove();
    }
  }

  // новые сообщения
  let lastDay = null;
  const existing = box.querySelectorAll('.msg-row');
  if (existing.length) {
    const lastRow = existing[existing.length - 1];
    const lastTs = parseFloat(lastRow.dataset.ts || '0');
    if (lastTs) lastDay = dayLabel(lastTs);
  }

  let added = false;
  for (const m of d.messages) {
    if (renderedIds.has(m.id)) continue;
    renderedIds.add(m.id);
    lastIds[nick] = Math.max(lastIds[nick] || 0, m.id);

    const day = dayLabel(m.time);
    if (day !== lastDay) {
      const sep = document.createElement('div');
      sep.className = 'date-sep';
      sep.innerHTML = '<span>' + esc(day) + '</span>';
      box.appendChild(sep);
      lastDay = day;
    }

    box.appendChild(renderMessage(m, nick));
    added = true;
  }

  if (added && (wasNearBottom || existing.length === 0)) {
    box.scrollTop = box.scrollHeight;
  }

  // обновим заголовок статуса
  updateChatSubtitle();
}

function renderMessage(m, peer) {
  const row = document.createElement('div');
  row.className = 'msg-row' + (m.from === me ? ' mine' : '');
  row.dataset.id = m.id;
  row.dataset.ts = m.time;

  const meta = m.deleted
    ? '<div class="msg-meta">' + fmtTime(m.time) + '</div>'
    : '<div class="msg-meta">' + fmtTime(m.time) + '</div>';

  const text = m.deleted ? 'Сообщение удалено' : esc(m.text);
  const delBtn = (!m.deleted && m.from === me)
    ? '<button class="msg-del" title="Удалить">×</button>'
    : '';

  row.innerHTML =
    '<div class="msg-bubble' + (m.deleted ? ' deleted' : '') + '">' +
      text +
      meta +
      delBtn +
    '</div>';

  if (delBtn) {
    row.querySelector('.msg-del').onclick = (e) => {
      e.stopPropagation();
      deleteMessage(m.id);
    };
  }
  return row;
}

async function deleteMessage(id) {
  if (!confirm('Удалить сообщение?')) return;
  const r = await fetch('/api/message/' + id, { method: 'DELETE' });
  if (!r.ok) return;
  const el = $('messages').querySelector('.msg-row[data-id="' + id + '"]');
  if (el) {
    const bubble = el.querySelector('.msg-bubble');
    bubble.classList.add('deleted');
    bubble.textContent = 'Сообщение удалено';
    const delBtn = el.querySelector('.msg-del');
    if (delBtn) delBtn.remove();
    deletedSeen.add(id);
  }
  await loadUsers();
  await loadInfo(current);
}

/* ==================== Отправка ==================== */
async function send() {
  if (!current) return;
  const inp = $('msgInput');
  const text = inp.value.trim();
  if (!text) return;

  const fd = new FormData();
  fd.append('to', current);
  fd.append('text', text);

  const r = await fetch('/api/send', { method: 'POST', body: fd });
  const d = await r.json().catch(() => ({ ok:false }));
  if (d.ok) {
    inp.value = '';
    inp.style.height = 'auto';
    await refreshDialog();
    await loadUsers();
    await loadInfo(current);
  } else {
    alert(d.error || 'Ошибка');
  }
}

/* ==================== Инфо о собеседнике ==================== */
async function loadInfo(nick) {
  if (!nick) {
    $('infoContent').innerHTML = '<div class="info-empty">Информация о собеседнике появится здесь</div>';
    return;
  }
  const r = await fetch('/api/user/' + encodeURIComponent(nick) + '/info');
  if (!r.ok) {
    $('infoContent').innerHTML = '<div class="info-empty">Пользователь не найден</div>';
    return;
  }
  const d = await r.json();
  if (!d.ok) return;
  const i = d.info;

  const status = i.online
    ? '<div class="status online">● В сети</div>'
    : '<div class="status">Был(а): ' + esc(fmtLastSeen(i.last_seen)) + '</div>';

  $('infoContent').innerHTML =
    '<div class="info-head">' +
      avatarHtml(i.nick, i.online, 'lg') +
      '<div class="name">' + esc(i.nick) + '</div>' +
      status +
    '</div>' +
    '<div class="info-row"><span class="k">Ник</span><span class="v">' + esc(i.nick) + '</span></div>' +
    '<div class="info-row"><span class="k">В SldChat с</span><span class="v">' + esc(fmtDateTime(i.created)) + '</span></div>' +
    '<div class="info-row"><span class="k">Последняя активность</span><span class="v">' + esc(fmtLastSeen(i.last_seen)) + '</span></div>' +
    '<div class="info-row"><span class="k">Сообщений в диалоге</span><span class="v">' + i.msg_count + '</span></div>';

  updateChatSubtitle(i.online, i.last_seen);
}

let currentInfo = null;
function updateChatSubtitle(online, lastSeen) {
  if (online !== undefined) currentInfo = { online, lastSeen };
  const el = $('chatSub');
  if (!current) { el.textContent = ''; return; }
  if (!currentInfo) { el.textContent = ''; return; }
  if (currentInfo.online) el.textContent = 'в сети';
  else el.textContent = 'был(а): ' + fmtLastSeen(currentInfo.lastSeen);
}

/* ==================== Polling ==================== */
async function poll() {
  if (!current || polling) return;
  polling = true;
  try {
    await refreshDialog();
  } finally {
    polling = false;
  }
}

/* ==================== UI события ==================== */
$('sendBtn').onclick = send;

$('msgInput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
$('msgInput').addEventListener('input', e => {
  const el = e.target;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
});

$('logoutBtn').onclick = async () => {
  await fetch('/api/logout', { method: 'POST' });
  location.href = '/';
};

$('backBtn').onclick = () => {
  current = null;
  app.classList.remove('chat-open');
  app.classList.remove('info-open');
  $('chatTitle').textContent = 'Выберите собеседника';
  $('chatSub').textContent = '';
  $('messages').innerHTML = '<div class="empty">Слева выберите пользователя, чтобы начать переписку</div>';
  $('inputArea').style.display = 'none';
  $('infoContent').innerHTML = '<div class="info-empty">Информация о собеседнике появится здесь</div>';
};

$('infoBtn').onclick = () => app.classList.toggle('info-open');
$('infoCloseBtn').onclick = () => app.classList.remove('info-open');

$('searchInput').addEventListener('input', e => {
  searchQuery = e.target.value;
  renderUsers();
});

/* ==================== Запуск ==================== */
setInterval(poll, 1500);
setInterval(async () => {
  await loadUsers();
  if (current) await loadInfo(current);
}, 5000);

init();
</script>
</body>
</html>
"""


# ================== МАРШРУТЫ ==================
@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    if current_user(request):
        return RedirectResponse("/chat")
    return LANDING_PAGE


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/chat")
    return render_auth("login")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if current_user(request):
        return RedirectResponse("/chat")
    return render_auth("register")


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/")
    return CHAT_PAGE


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
