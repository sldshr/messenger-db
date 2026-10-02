# main.py
# Мини-соцсеть: FastAPI + Material Design, всё хранится в оперативной памяти.
# Запуск:  pip install fastapi uvicorn python-multipart
#          uvicorn main:app --reload
# Открыть: http://127.0.0.1:8000

import hashlib
import html
import secrets
import uuid
from datetime import datetime
from typing import Optional

from fastapi import Cookie, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="MiniNet")

# --------------------------------------------------------------------------
# ХРАНИЛИЩЕ (в оперативной памяти)
# --------------------------------------------------------------------------
USERS: dict = {}      # username -> {"salt": str, "hash": str, "created": float}
POSTS: list = []      # [{"id","author","text","ts","likes": set()}]
SESSIONS: dict = {}   # token -> username

MAX_POST = 500


# --------------------------------------------------------------------------
# ХЕЛПЕРЫ
# --------------------------------------------------------------------------
def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def current_user(session: Optional[str]) -> Optional[str]:
    if session and session in SESSIONS:
        return SESSIONS[session]
    return None


def back(request: Request) -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


def avatar(name: str) -> str:
    return f'<div class="avatar">{html.escape(name[0].upper())}</div>'


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y в %H:%M")


# --------------------------------------------------------------------------
# ВЁРСТКА
# --------------------------------------------------------------------------
def layout(title: str, content: str, user: Optional[str] = None) -> str:
    if user:
        nav = (
            f'<li><a href="/u/{html.escape(user)}">'
            f'<i class="material-icons left">person</i>{html.escape(user)}</a></li>'
            '<li><form action="/logout" method="post">'
            '<button type="submit" class="btn-flat white-text waves-effect">Выйти</button>'
            "</form></li>"
        )
    else:
        nav = (
            '<li><a href="/login">Войти</a></li>'
            '<li><a href="/register">Регистрация</a></li>'
        )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · MiniNet</title>
<link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
<link rel="stylesheet"
      href="https://cdnjs.cloudflare.com/ajax/libs/materialize/1.0.0/css/materialize.min.css">
<style>
  body {{ background:#eceff1; }}
  .brand-logo {{ font-weight:700; letter-spacing:-0.5px; }}
  nav .brand-logo {{ padding-left:12px; }}
  nav ul li form {{ margin:0; }}
  nav ul li form button {{ height:56px; line-height:56px; text-transform:none;
                           font-size:1rem; padding:0 15px; }}
  .card {{ border-radius:14px; }}
  .card .card-action {{ border-top:1px solid #eee; padding:6px 12px; }}
  .avatar {{ width:46px; height:46px; border-radius:50%; background:#3f51b5; color:#fff;
             display:flex; align-items:center; justify-content:center;
             font-weight:600; font-size:20px; }}
  .post-text {{ white-space:pre-wrap; word-wrap:break-word; font-size:1.05rem; }}
  .btn-flat {{ text-transform:none; }}
  .empty {{ padding:28px; text-align:center; color:#90a4ae; }}
  .col.narrow {{ max-width:720px; margin:0 auto; float:none; }}
  main {{ padding-bottom:60px; }}
</style>
</head>
<body>
<nav class="indigo">
  <div class="nav-wrapper container">
    <a href="/" class="brand-logo">MiniNet</a>
    <ul class="right">{nav}</ul>
  </div>
</nav>
<main class="container" style="margin-top:26px">{content}</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/materialize/1.0.0/js/materialize.min.js"></script>
</body>
</html>"""


def render_post(post: dict, user: Optional[str]) -> str:
    liked = user is not None and user in post["likes"]
    likes_n = len(post["likes"])

    if user:
        like_html = f"""<form action="/like/{post['id']}" method="post" style="display:inline">
  <button class="btn-flat waves-effect {'pink-text' if liked else 'grey-text text-darken-1'}"
          type="submit">
    <i class="material-icons left">{'favorite' if liked else 'favorite_border'}</i>{likes_n}
  </button></form>"""
    else:
        like_html = (f'<span class="grey-text text-darken-1" style="margin-left:8px">'
                     f'<i class="material-icons left">favorite_border</i>{likes_n}</span>')

    delete_html = ""
    if user == post["author"]:
        delete_html = f"""<form action="/delete/{post['id']}" method="post" style="display:inline;float:right">
  <button class="btn-flat waves-effect red-text" type="submit" title="Удалить">
    <i class="material-icons">delete_outline</i></button></form>"""

    return f"""
<div class="card">
  <div class="card-content">
    <div class="row valign-wrapper" style="margin-bottom:10px">
      <div class="col s2 m1">{avatar(post['author'])}</div>
      <div class="col s10 m11">
        <a href="/u/{html.escape(post['author'])}" class="indigo-text text-darken-3">
          <b>{html.escape(post['author'])}</b></a><br>
        <small class="grey-text">{fmt_time(post['ts'])}</small>
      </div>
    </div>
    <div class="post-text">{html.escape(post['text'])}</div>
  </div>
  <div class="card-action">
    {like_html}
    {delete_html}
  </div>
</div>"""


def feed_html(user: Optional[str]) -> str:
    if user:
        composer = f"""
<div class="card">
  <div class="card-content">
    <form action="/post" method="post">
      <div class="input-field" style="margin-top:0">
        <textarea id="text" name="text" class="materialize-textarea"
                  maxlength="{MAX_POST}" required
                  placeholder="Что нового, {html.escape(user)}?"></textarea>
        <label for="text" class="active">Новый пост</label>
      </div>
      <button class="btn indigo waves-effect" type="submit">
        <i class="material-icons left">send</i>Опубликовать
      </button>
    </form>
  </div>
</div>"""
    else:
        composer = """
<div class="card"><div class="card-content center-align">
  <p class="grey-text">Войдите или зарегистрируйтесь, чтобы публиковать посты.</p>
  <a href="/login" class="btn indigo waves-effect">Войти</a>
  <a href="/register" class="btn-flat waves-effect">Регистрация</a>
</div></div>"""

    if POSTS:
        posts = "".join(render_post(p, user) for p in POSTS)
    else:
        posts = '<div class="card"><div class="empty">Пока нет ни одного поста 🙈</div></div>'

    stats = (f'<p class="grey-text" style="margin:0 0 12px 4px">'
             f'Постов: {len(POSTS)} · Пользователей: {len(USERS)}</p>')

    return layout("Лента", composer + stats + posts, user)


def auth_card(title: str, fields: str, action: str, submit: str,
              footer: str, error: Optional[str] = None) -> str:
    err = (f'<div class="card-panel red lighten-4 red-text text-darken-4" '
           f'style="border-radius:10px">{html.escape(error)}</div>') if error else ""
    return f"""
<div class="row"><div class="col s12 m7 l5 narrow">
  <h4 style="font-weight:300">{html.escape(title)}</h4>
  {err}
  <div class="card"><div class="card-content">
    <form action="{action}" method="post">
      {fields}
      <button class="btn indigo waves-effect full-width" type="submit"
              style="width:100%">{html.escape(submit)}</button>
    </form>
  </div></div>
  <p class="center grey-text">{footer}</p>
</div></div>"""


def field(name: str, label: str, type_: str = "text", icon: str = "") -> str:
    ic = f'<i class="material-icons prefix">{icon}</i>' if icon else ""
    return f"""<div class="input-field">
  {ic}
  <input id="{name}" name="{name}" type="{type_}" required>
  <label for="{name}">{html.escape(label)}</label>
</div>"""


# --------------------------------------------------------------------------
# МАРШРУТЫ
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(session: Optional[str] = Cookie(default=None)):
    return HTMLResponse(feed_html(current_user(session)))


@app.get("/register", response_class=HTMLResponse)
def register_page(session: Optional[str] = Cookie(default=None)):
    if current_user(session):
        return RedirectResponse("/", status_code=303)
    fields = (field("username", "Имя пользователя", icon="person") +
              field("password", "Пароль", "password", "lock") +
              field("password2", "Повторите пароль", "password", "lock_outline"))
    body = auth_card("Регистрация", fields, "/register", "Создать аккаунт",
                     'Уже есть аккаунт? <a href="/login">Войти</a>')
    return HTMLResponse(layout("Регистрация", body))


@app.post("/register")
def register(username: str = Form(...),
             password: str = Form(...),
             password2: str = Form(...)):
    username = username.strip()
    err = None
    if not (3 <= len(username) <= 20) or not username.replace("_", "").isalnum():
        err = "Имя: 3–20 символов, только буквы, цифры и «_»."
    elif len(password) < 4:
        err = "Пароль должен быть не короче 4 символов."
    elif password != password2:
        err = "Пароли не совпадают."
    elif any(u.lower() == username.lower() for u in USERS):
        err = "Такое имя уже занято."

    if err:
        fields = (field("username", "Имя пользователя", icon="person") +
                  field("password", "Пароль", "password", "lock") +
                  field("password2", "Повторите пароль", "password", "lock_outline"))
        body = auth_card("Регистрация", fields, "/register", "Создать аккаунт",
                         'Уже есть аккаунт? <a href="/login">Войти</a>', err)
        return HTMLResponse(layout("Регистрация", body), status_code=400)

    salt = secrets.token_hex(16)
    USERS[username] = {"salt": salt,
                       "hash": hash_password(password, salt),
                       "created": datetime.now().timestamp()}

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True, max_age=7 * 24 * 3600, samesite="lax")
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(session: Optional[str] = Cookie(default=None)):
    if current_user(session):
        return RedirectResponse("/", status_code=303)
    fields = (field("username", "Имя пользователя", icon="person") +
              field("password", "Пароль", "password", "lock"))
    body = auth_card("Вход", fields, "/login", "Войти",
                     'Нет аккаунта? <a href="/register">Зарегистрироваться</a>')
    return HTMLResponse(layout("Вход", body))


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    user = USERS.get(username)
    ok = False
    if user:
        ok = secrets.compare_digest(user["hash"], hash_password(password, user["salt"]))
    if not ok:
        fields = (field("username", "Имя пользователя", icon="person") +
                  field("password", "Пароль", "password", "lock"))
        body = auth_card("Вход", fields, "/login", "Войти",
                         'Нет аккаунта? <a href="/register">Зарегистрироваться</a>',
                         "Неверное имя пользователя или пароль.")
        return HTMLResponse(layout("Вход", body), status_code=401)

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True, max_age=7 * 24 * 3600, samesite="lax")
    return resp


@app.post("/logout")
def logout(session: Optional[str] = Cookie(default=None)):
    if session:
        SESSIONS.pop(session, None)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session")
    return resp


@app.post("/post")
def create_post(request: Request,
                text: str = Form(...),
                session: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    if not user:
        return RedirectResponse("/login", status_code=303)
    text = text.strip()[:MAX_POST]
    if text:
        POSTS.insert(0, {
            "id": uuid.uuid4().hex[:12],
            "author": user,
            "text": text,
            "ts": datetime.now().timestamp(),
            "likes": set(),
        })
    return back(request)


@app.post("/like/{post_id}")
def like(post_id: str,
         request: Request,
         session: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    if not user:
        return RedirectResponse("/login", status_code=303)
    for p in POSTS:
        if p["id"] == post_id:
            if user in p["likes"]:
                p["likes"].discard(user)
            else:
                p["likes"].add(user)
            break
    return back(request)


@app.post("/delete/{post_id}")
def delete_post(post_id: str,
                request: Request,
                session: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    if not user:
        return RedirectResponse("/login", status_code=303)
    for i, p in enumerate(POSTS):
        if p["id"] == post_id and p["author"] == user:
            POSTS.pop(i)
            break
    return back(request)


@app.get("/u/{username}", response_class=HTMLResponse)
def profile(username: str, session: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    target = next((u for u in USERS if u.lower() == username.lower()), None)

    if not target:
        body = ('<div class="card"><div class="empty">'
                "Пользователь не найден 🤷</div></div>")
        return HTMLResponse(layout("404", body, user), status_code=404)

    user_posts = [p for p in POSTS if p["author"] == target]
    likes_total = sum(len(p["likes"]) for p in user_posts)

    header = f"""
<div class="card">
  <div class="card-content">
    <div class="row valign-wrapper" style="margin-bottom:0">
      <div class="col s2 m1">{avatar(target)}</div>
      <div class="col s10 m11">
        <h5 style="margin:0;font-weight:400">{html.escape(target)}</h5>
        <span class="grey-text">Постов: {len(user_posts)} · Лайков получено: {likes_total}</span>
      </div>
    </div>
  </div>
</div>"""

    if user_posts:
        posts = "".join(render_post(p, user) for p in user_posts)
    else:
        posts = '<div class="card"><div class="empty">Постов пока нет</div></div>'

    return HTMLResponse(layout(f"@{target}", header + posts, user))
