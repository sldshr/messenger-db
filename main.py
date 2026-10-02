# main.py
# Мини-соцсеть: FastAPI + кастомный Material-подобный UI.
# Тёмная/светлая тема, уведомления, упоминания @user, поиск, мобильная вёрстка.
# Запуск:  pip install fastapi uvicorn
#          uvicorn main:app --reload
# Открыть: http://127.0.0.1:8000

import hashlib
import html
import re
import secrets
import uuid
from datetime import datetime
from typing import Optional

from fastapi import Cookie, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="MiniNet")

# --------------------------------------------------------------------------
# ХРАНИЛИЩЕ В ОПЕРАТИВНОЙ ПАМЯТИ
# --------------------------------------------------------------------------
USERS: dict = {}          # username -> {"salt", "hash", "created"}
POSTS: list = []          # [{"id","author","text","ts","likes": set()}]
SESSIONS: dict = {}       # token -> username
NOTIFICATIONS: list = []  # [{"id","user","from","kind","post_id","ts","read"}]

MAX_POST = 500
MENTION_RE = re.compile(r"@([A-Za-z0-9_]{3,20})")


# --------------------------------------------------------------------------
# ХЕЛПЕРЫ
# --------------------------------------------------------------------------
def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def current_user(session: Optional[str]) -> Optional[str]:
    if session and session in SESSIONS:
        return SESSIONS[session]
    return None


def find_user(name: str) -> Optional[str]:
    return next((u for u in USERS if u.lower() == name.lower()), None)


def add_notification(recipient: str, kind: str, from_user: str,
                     post_id: Optional[str] = None) -> None:
    if not recipient or recipient == from_user:
        return
    NOTIFICATIONS.insert(0, {
        "id": uuid.uuid4().hex[:12],
        "user": recipient,
        "from": from_user,
        "kind": kind,
        "post_id": post_id,
        "ts": datetime.now().timestamp(),
        "read": False,
    })


def unread_count(user: Optional[str]) -> int:
    if not user:
        return 0
    return sum(1 for n in NOTIFICATIONS if n["user"] == user and not n["read"])


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def back(request: Request) -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


def render_text(text: str) -> str:
    """Экранирует HTML и превращает @username в ссылки."""
    escaped = html.escape(text)

    def repl(m):
        name = m.group(1)
        actual = find_user(name)
        if actual:
            return f'<a class="mention" href="/u/{html.escape(actual)}">@{html.escape(actual)}</a>'
        return m.group(0)

    return MENTION_RE.sub(repl, escaped)


def avatar(name: str, cls: str = "avatar") -> str:
    return f'<div class="{cls}">{html.escape(name[0].upper())}</div>'


# --------------------------------------------------------------------------
# ВЁРСТКА
# --------------------------------------------------------------------------
CSS = """
* { box-sizing: border-box; }
:root {
  --bg: #eceff1; --surface: #ffffff; --surface-2: #f5f7f9;
  --text: #1c1c1c; --muted: #6b7785; --border: #e3e7eb;
  --primary: #3f51b5; --primary-hover: #32408f; --on-primary: #ffffff;
  --nav-bg: #3f51b5; --nav-text: #ffffff;
  --like: #e91e63; --danger: #e53935;
  --shadow: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.04);
  --shadow-hover: 0 3px 10px rgba(0,0,0,.10);
}
[data-theme="dark"] {
  --bg: #0f1115; --surface: #1a1d23; --surface-2: #23272e;
  --text: #e8eaed; --muted: #9aa3ad; --border: #2c3138;
  --primary: #8b98e0; --primary-hover: #a3aee8; --on-primary: #12141a;
  --nav-bg: #151821; --nav-text: #e8eaed;
  --like: #f06292; --danger: #ef5350;
  --shadow: 0 1px 3px rgba(0,0,0,.5);
  --shadow-hover: 0 3px 12px rgba(0,0,0,.6);
}
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font-family: Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
  font-size: 15px; line-height: 1.5;
  min-height: 100vh;
  transition: background .2s ease, color .2s ease;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
.mention { color: var(--primary); font-weight: 500; }

/* ------- Navbar ------- */
.navbar {
  background: var(--nav-bg); color: var(--nav-text);
  position: sticky; top: 0; z-index: 100;
  box-shadow: 0 2px 8px rgba(0,0,0,.15);
}
.nav-inner {
  max-width: 1080px; margin: 0 auto;
  padding: 0 12px; height: 56px;
  display: flex; align-items: center; gap: 10px;
}
.brand {
  color: var(--nav-text); font-weight: 700; font-size: 1.2rem;
  letter-spacing: -.4px; text-decoration: none; flex-shrink: 0;
}
.brand:hover { text-decoration: none; opacity: .9; }
.search-form {
  flex: 1; max-width: 420px; min-width: 0;
  display: flex; align-items: center;
  background: rgba(255,255,255,.14);
  border-radius: 22px; height: 38px; padding: 0 12px;
  transition: background .15s;
}
.search-form:focus-within { background: rgba(255,255,255,.24); }
.search-form i { font-size: 20px; color: var(--nav-text); opacity: .85; margin-right: 6px; }
.search-form input {
  flex: 1; min-width: 0; background: none; border: none; outline: none;
  color: var(--nav-text); font-family: inherit; font-size: .95rem;
  height: 100%; padding: 0;
}
.search-form input::placeholder { color: var(--nav-text); opacity: .65; }
.nav-actions { display: flex; align-items: center; gap: 2px; margin-left: auto; }
.icon-btn {
  width: 42px; height: 42px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 50%; border: none; background: none;
  color: var(--nav-text); cursor: pointer; position: relative;
  padding: 0; text-decoration: none; font-family: inherit;
  transition: background .15s;
}
.icon-btn:hover { background: rgba(255,255,255,.14); text-decoration: none; }
.icon-btn i { font-size: 24px; }
.avatar-btn {
  background: var(--primary); color: var(--on-primary);
  font-weight: 700; font-size: 1rem;
}
.avatar-btn:hover { background: var(--primary-hover); }
.badge {
  position: absolute; top: 6px; right: 6px;
  background: #f44336; color: #fff;
  font-size: 10px; font-weight: 700; line-height: 1;
  min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
}
.logout-form { display: inline-flex; }

/* ------- Layout ------- */
.container { max-width: 720px; margin: 0 auto; padding: 18px 12px 60px; }
.page-title { font-size: 1.35rem; font-weight: 500; margin: 0 0 16px 4px; }
.muted { color: var(--muted); }
.small { font-size: .85rem; }

/* ------- Cards ------- */
.card {
  background: var(--surface); border-radius: 14px;
  box-shadow: var(--shadow); margin-bottom: 14px; overflow: hidden;
  transition: box-shadow .15s;
}
.card-body { padding: 16px; }
.card-footer {
  padding: 4px 8px; border-top: 1px solid var(--border);
  display: flex; align-items: center; gap: 4px;
}
.card-header-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.avatar {
  width: 46px; height: 46px; border-radius: 50%;
  background: var(--primary); color: var(--on-primary);
  display: flex; align-items: center; justify-content: center;
  font-weight: 600; font-size: 20px; flex-shrink: 0;
  user-select: none;
}
.avatar-lg { width: 72px; height: 72px; font-size: 30px; }
.post-author { color: var(--text); font-weight: 600; }
.post-author:hover { color: var(--primary); text-decoration: none; }
.post-time { font-size: .82rem; color: var(--muted); }
.post-text {
  white-space: pre-wrap; word-wrap: break-word;
  font-size: 1rem; line-height: 1.55;
}

/* ------- Buttons ------- */
.btn {
  display: inline-flex; align-items: center; justify-content: center;
  gap: 6px; padding: 9px 18px;
  border-radius: 22px; border: none; cursor: pointer;
  font-family: inherit; font-size: .92rem; font-weight: 500;
  text-decoration: none; line-height: 1;
  background: var(--primary); color: var(--on-primary);
  transition: background .15s, opacity .15s;
}
.btn:hover { background: var(--primary-hover); text-decoration: none; }
.btn:disabled { opacity: .6; cursor: default; }
.btn-block { width: 100%; }
.btn i { font-size: 18px; }
.btn-icon {
  width: 38px; height: 38px; padding: 0;
  border-radius: 50%; background: none; color: var(--muted);
  border: none; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  transition: background .15s, color .15s;
}
.btn-icon:hover { background: var(--surface-2); }
.btn-icon i { font-size: 20px; }
.btn-icon.liked { color: var(--like); }
.btn-icon.danger:hover { color: var(--danger); }
.btn-flat {
  background: none; color: var(--primary); padding: 8px 14px;
}
.btn-flat:hover { background: var(--surface-2); }

/* ------- Inputs ------- */
.input-field { margin-bottom: 14px; }
.input-field label {
  display: block; font-size: .82rem; color: var(--muted);
  margin-bottom: 6px; font-weight: 500;
}
input[type="text"], input[type="password"], textarea {
  width: 100%; background: var(--surface-2); color: var(--text);
  border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 14px; font-family: inherit; font-size: .95rem;
  outline: none; transition: border-color .15s, box-shadow .15s;
}
input[type="text"]:focus, input[type="password"]:focus, textarea:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 20%, transparent);
}
textarea { resize: vertical; min-height: 88px; line-height: 1.5; }
.composer textarea { min-height: 68px; }

/* ------- Composer ------- */
.composer-footer {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; margin-top: 10px;
}
.hint { font-size: .8rem; color: var(--muted); }
.counter { font-size: .8rem; color: var(--muted); }

/* ------- Notification item ------- */
.notif {
  display: flex; gap: 12px; align-items: flex-start;
  padding: 14px 16px; border-bottom: 1px solid var(--border);
  color: var(--text); text-decoration: none;
  transition: background .15s;
}
.notif:last-child { border-bottom: none; }
.notif:hover { background: var(--surface-2); text-decoration: none; }
.notif.unread { background: rgba(63,81,181,.07); }
[data-theme="dark"] .notif.unread { background: rgba(139,152,224,.10); }
.notif-icon {
  width: 40px; height: 40px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: var(--surface-2); flex-shrink: 0;
}
.notif-icon i { font-size: 20px; color: var(--primary); }
.notif-content { flex: 1; min-width: 0; }
.notif-text { font-size: .95rem; }
.notif-text b { color: var(--text); }
.notif-time { font-size: .78rem; color: var(--muted); margin-top: 2px; }

/* ------- Empty / 404 ------- */
.empty {
  padding: 44px 20px; text-align: center; color: var(--muted);
}
.empty i { font-size: 52px; opacity: .35; display: block; margin-bottom: 10px; }

/* ------- Profile header ------- */
.profile-header {
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
}
.profile-name { margin: 0; font-weight: 500; font-size: 1.4rem; }
.profile-stats { color: var(--muted); font-size: .9rem; margin-top: 4px; }
.profile-actions { margin-left: auto; }

/* ------- Auth ------- */
.auth-wrap { max-width: 420px; margin: 0 auto; }
.auth-wrap h1 { font-weight: 300; font-size: 1.6rem; margin: 4px 0 20px; }
.auth-alt { text-align: center; color: var(--muted); font-size: .9rem; margin-top: 14px; }
.error-box {
  background: rgba(229,57,53,.1); color: var(--danger);
  border: 1px solid rgba(229,57,53,.25);
  padding: 10px 14px; border-radius: 10px; margin-bottom: 14px;
  font-size: .9rem;
}

/* ------- Search result ------- */
.user-chip {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px; border-bottom: 1px solid var(--border);
  color: var(--text); text-decoration: none;
}
.user-chip:last-child { border-bottom: none; }
.user-chip:hover { background: var(--surface-2); text-decoration: none; }
.user-chip .avatar { width: 40px; height: 40px; font-size: 17px; }

/* ------- Responsive ------- */
@media (max-width: 640px) {
  .brand { display: none; }
  .nav-inner { gap: 6px; padding: 0 8px; }
  .icon-btn { width: 40px; height: 40px; }
  .icon-btn i { font-size: 22px; }
  .avatar-btn { font-size: .95rem; }
  .search-form { height: 36px; }
  .container { padding: 12px 8px 60px; }
  .card-body { padding: 14px; }
  .post-text { font-size: .97rem; }
  .profile-header { gap: 12px; }
  .profile-name { font-size: 1.2rem; }
}
@media (max-width: 380px) {
  .nav-actions { gap: 0; }
  .icon-btn { width: 36px; height: 36px; }
  .icon-btn i { font-size: 20px; }
}
"""


def layout(title: str, content: str, user: Optional[str] = None,
           theme: str = "light") -> str:
    theme = "dark" if theme == "dark" else "light"
    unread = unread_count(user)

    # ----- левая часть навбара + поиск -----
    search = (
        '<form class="search-form" action="/search" method="get">'
        '<i class="material-icons">search</i>'
        '<input type="text" name="q" placeholder="Поиск людей и постов" autocomplete="off">'
        "</form>"
    )

    # ----- правые иконки -----
    bell_icon = "notifications" if unread else "notifications_none"
    badge = f'<span class="badge">{unread if unread < 100 else "99+"}</span>' if unread else ""

    if user:
        avatar_btn = (
            f'<a href="/u/{html.escape(user)}" class="icon-btn avatar-btn" '
            f'title="{html.escape(user)}">{html.escape(user[0].upper())}</a>'
        )
        logout = (
            '<form class="logout-form" action="/logout" method="post">'
            '<button class="icon-btn" type="submit" title="Выйти">'
            '<i class="material-icons">logout</i></button></form>'
        )
    else:
        avatar_btn = (
            '<a href="/login" class="icon-btn" title="Войти">'
            '<i class="material-icons">person_outline</i></a>'
        )
        logout = ""

    theme_icon = "light_mode" if theme == "dark" else "dark_mode"

    nav_actions = (
        f'<a href="/#composer" class="icon-btn" title="Новый пост">'
        f'<i class="material-icons">add</i></a>'
        f'<a href="/notifications" class="icon-btn" title="Уведомления">'
        f'<i class="material-icons">{bell_icon}</i>{badge}</a>'
        f"{avatar_btn}"
        f'<a href="/toggle-theme" class="icon-btn" title="Сменить тему">'
        f'<i class="material-icons">{theme_icon}</i></a>'
        f"{logout}"
    )

    return f"""<!DOCTYPE html>
<html lang="ru" data-theme="{theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)} · MiniNet</title>
<link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<nav class="navbar">
  <div class="nav-inner">
    <a href="/" class="brand">MiniNet</a>
    {search}
    <div class="nav-actions">{nav_actions}</div>
  </div>
</nav>
<main class="container">{content}</main>
<script>
  // Счётчик символов в композере
  document.querySelectorAll('textarea[maxlength]').forEach(function (ta) {{
    var counter = ta.parentElement.querySelector('.counter');
    if (!counter) return;
    function upd() {{ counter.textContent = ta.value.length + ' / ' + ta.maxLength; }}
    ta.addEventListener('input', upd); upd();
  }});
</script>
</body>
</html>"""


# --------------------------------------------------------------------------
# КОМПОНЕНТЫ
# --------------------------------------------------------------------------
def render_post(post: dict, user: Optional[str], show_delete: bool = True) -> str:
    liked = user is not None and user in post["likes"]
    likes_n = len(post["likes"])

    if user:
        like_html = (
            f'<form action="/like/{post["id"]}" method="post" style="display:inline">'
            f'<button class="btn-icon {"liked" if liked else ""}" type="submit" '
            f'title="{"Убрать лайк" if liked else "Нравится"}">'
            f'<i class="material-icons">{"favorite" if liked else "favorite_border"}</i>'
            f"</button></form>"
        )
    else:
        like_html = (
            f'<a href="/login" class="btn-icon" title="Войдите, чтобы лайкать">'
            f'<i class="material-icons">favorite_border</i></a>'
        )

    likes_label = f'<span class="small muted" style="margin-left:2px">{likes_n}</span>'

    delete_html = ""
    if show_delete and user == post["author"]:
        delete_html = (
            f'<form action="/delete/{post["id"]}" method="post" '
            f'style="display:inline;margin-left:auto" '
            f'onsubmit="return confirm(\'Удалить этот пост?\')">'
            f'<button class="btn-icon danger" type="submit" title="Удалить">'
            f'<i class="material-icons">delete_outline</i></button></form>'
        )

    return f"""
<div class="card" id="post-{post['id']}">
  <div class="card-body">
    <div class="card-header-row">
      {avatar(post['author'])}
      <div style="min-width:0">
        <a class="post-author" href="/u/{html.escape(post['author'])}">{html.escape(post['author'])}</a><br>
        <span class="post-time">{fmt_time(post['ts'])}</span>
      </div>
    </div>
    <div class="post-text">{render_text(post['text'])}</div>
  </div>
  <div class="card-footer">
    {like_html}{likes_label}
    <a class="btn-icon" href="/post/{post['id']}" title="Открыть пост">
      <i class="material-icons">chat_bubble_outline</i>
    </a>
    {delete_html}
  </div>
</div>"""


def composer_html(user: str) -> str:
    return f"""
<div class="card" id="composer">
  <div class="card-body composer">
    <form action="/post" method="post">
      <textarea name="text" maxlength="{MAX_POST}" required
                placeholder="Что нового, {html.escape(user)}?"></textarea>
      <div class="composer-footer">
        <span class="hint">Используйте @username для упоминания</span>
        <div style="display:flex;align-items:center;gap:12px">
          <span class="counter">0 / {MAX_POST}</span>
          <button class="btn" type="submit">
            <i class="material-icons">send</i>Опубликовать
          </button>
        </div>
      </div>
    </form>
  </div>
</div>"""


def guest_composer() -> str:
    return """
<div class="card"><div class="card-body" style="text-align:center">
  <p class="muted" style="margin:0 0 12px">Войдите или зарегистрируйтесь, чтобы публиковать.</p>
  <a class="btn" href="/login">Войти</a>
  <a class="btn-flat" href="/register" style="margin-left:6px">Регистрация</a>
</div></div>"""


def empty_card(text: str, icon: str = "inbox") -> str:
    return f'<div class="card"><div class="empty">' \
           f'<i class="material-icons">{icon}</i>{html.escape(text)}</div></div>'


# --------------------------------------------------------------------------
# СТРАНИЦЫ
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(session: Optional[str] = Cookie(default=None),
          theme: Optional[str] = Cookie(default=None)):
    user = current_user(session)

    head = composer_html(user) if user else guest_composer()

    if POSTS:
        posts_html = "".join(render_post(p, user) for p in POSTS)
    else:
        posts_html = empty_card("Пока нет ни одного поста", "article")

    stats = (f'<p class="muted small" style="margin:0 0 12px 4px">'
             f'Постов: {len(POSTS)} · Пользователей: {len(USERS)}</p>')

    return HTMLResponse(layout("Лента", head + stats + posts_html, user, theme or "light"))


@app.get("/toggle-theme")
def toggle_theme(request: Request, theme: Optional[str] = Cookie(default=None)):
    new_theme = "light" if theme == "dark" else "dark"
    resp = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    resp.set_cookie("theme", new_theme, max_age=365 * 24 * 3600, samesite="lax")
    return resp


# ------------------------------ Авторизация ------------------------------
def auth_field(name: str, label: str, type_: str = "text") -> str:
    return (f'<div class="input-field">'
            f'<label for="{name}">{html.escape(label)}</label>'
            f'<input id="{name}" name="{name}" type="{type_}" required></div>')


def auth_page(title: str, fields_html: str, action: str, submit: str,
              alt_html: str, error: Optional[str] = None) -> str:
    err = f'<div class="error-box">{html.escape(error)}</div>' if error else ""
    return f"""
<div class="auth-wrap">
  <h1>{html.escape(title)}</h1>
  {err}
  <div class="card"><div class="card-body">
    <form action="{action}" method="post">
      {fields_html}
      <button class="btn btn-block" type="submit" style="margin-top:6px">{html.escape(submit)}</button>
    </form>
  </div></div>
  <p class="auth-alt">{alt_html}</p>
</div>"""


@app.get("/register", response_class=HTMLResponse)
def register_page(session: Optional[str] = Cookie(default=None),
                  theme: Optional[str] = Cookie(default=None)):
    if current_user(session):
        return RedirectResponse("/", status_code=303)
    fields = (auth_field("username", "Имя пользователя") +
              auth_field("password", "Пароль", "password") +
              auth_field("password2", "Повторите пароль", "password"))
    body = auth_page("Регистрация", fields, "/register", "Создать аккаунт",
                     'Уже есть аккаунт? <a href="/login">Войти</a>')
    return HTMLResponse(layout("Регистрация", body, None, theme or "light"))


@app.post("/register")
def register(username: str = Form(...),
             password: str = Form(...),
             password2: str = Form(...),
             theme: Optional[str] = Cookie(default=None)):
    username = username.strip()
    err = None
    if not (3 <= len(username) <= 20) or not username.replace("_", "").isalnum():
        err = "Имя: 3–20 символов, только буквы, цифры и «_»."
    elif len(password) < 4:
        err = "Пароль должен быть не короче 4 символов."
    elif password != password2:
        err = "Пароли не совпадают."
    elif find_user(username):
        err = "Такое имя уже занято."

    if err:
        fields = (auth_field("username", "Имя пользователя") +
                  auth_field("password", "Пароль", "password") +
                  auth_field("password2", "Повторите пароль", "password"))
        body = auth_page("Регистрация", fields, "/register", "Создать аккаунт",
                         'Уже есть аккаунт? <a href="/login">Войти</a>', err)
        return HTMLResponse(layout("Регистрация", body, None, theme or "light"),
                            status_code=400)

    salt = secrets.token_hex(16)
    USERS[username] = {
        "salt": salt,
        "hash": hash_password(password, salt),
        "created": datetime.now().timestamp(),
    }

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True,
                    max_age=7 * 24 * 3600, samesite="lax")
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(session: Optional[str] = Cookie(default=None),
               theme: Optional[str] = Cookie(default=None)):
    if current_user(session):
        return RedirectResponse("/", status_code=303)
    fields = (auth_field("username", "Имя пользователя") +
              auth_field("password", "Пароль", "password"))
    body = auth_page("Вход", fields, "/login", "Войти",
                     'Нет аккаунта? <a href="/register">Зарегистрироваться</a>')
    return HTMLResponse(layout("Вход", body, None, theme or "light"))


@app.post("/login")
def login(username: str = Form(...),
          password: str = Form(...),
          theme: Optional[str] = Cookie(default=None)):
    username = username.strip()
    user = find_user(username)
    ok = False
    if user:
        ok = secrets.compare_digest(USERS[user]["hash"],
                                    hash_password(password, USERS[user]["salt"]))
    if not ok:
        fields = (auth_field("username", "Имя пользователя") +
                  auth_field("password", "Пароль", "password"))
        body = auth_page("Вход", fields, "/login", "Войти",
                         'Нет аккаунта? <a href="/register">Зарегистрироваться</a>',
                         "Неверное имя пользователя или пароль.")
        return HTMLResponse(layout("Вход", body, None, theme or "light"),
                            status_code=401)

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = user
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True,
                    max_age=7 * 24 * 3600, samesite="lax")
    return resp


@app.post("/logout")
def logout(session: Optional[str] = Cookie(default=None)):
    if session:
        SESSIONS.pop(session, None)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session")
    return resp


# ------------------------------ Посты ------------------------------
@app.post("/post")
def create_post(request: Request,
                text: str = Form(...),
                session: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    if not user:
        return RedirectResponse("/login", status_code=303)

    text = text.strip()[:MAX_POST]
    if text:
        post = {
            "id": uuid.uuid4().hex[:12],
            "author": user,
            "text": text,
            "ts": datetime.now().timestamp(),
            "likes": set(),
        }
        POSTS.insert(0, post)

        # уведомления об упоминаниях
        for name in set(MENTION_RE.findall(text)):
            actual = find_user(name)
            if actual:
                add_notification(actual, "mention", user, post["id"])

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
                add_notification(p["author"], "like", user, p["id"])
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


@app.get("/post/{post_id}", response_class=HTMLResponse)
def post_page(post_id: str,
              session: Optional[str] = Cookie(default=None),
              theme: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    post = next((p for p in POSTS if p["id"] == post_id), None)
    if not post:
        return HTMLResponse(
            layout("Пост не найден",
                   empty_card("Пост не найден или был удалён", "search_off"),
                   user, theme or "light"),
            status_code=404)
    return HTMLResponse(layout("Пост", render_post(post, user), user, theme or "light"))


# ------------------------------ Профиль ------------------------------
@app.get("/u/{username}", response_class=HTMLResponse)
def profile(username: str,
            session: Optional[str] = Cookie(default=None),
            theme: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    target = find_user(username)

    if not target:
        body = empty_card("Пользователь не найден", "person_off")
        return HTMLResponse(layout("404", body, user, theme or "light"), status_code=404)

    user_posts = [p for p in POSTS if p["author"] == target]
    likes_total = sum(len(p["likes"]) for p in user_posts)

    logout_btn = ""
    if user == target:
        logout_btn = (
            '<form class="profile-actions" action="/logout" method="post">'
            '<button class="btn-flat" type="submit">Выйти</button></form>'
        )

    header = f"""
<div class="card"><div class="card-body profile-header">
  {avatar(target, "avatar avatar-lg")}
  <div>
    <h1 class="profile-name">{html.escape(target)}</h1>
    <div class="profile-stats">Постов: {len(user_posts)} · Лайков получено: {likes_total}</div>
  </div>
  {logout_btn}
</div></div>"""

    if user_posts:
        posts_html = "".join(render_post(p, user) for p in user_posts)
    else:
        posts_html = empty_card("Постов пока нет", "article")

    return HTMLResponse(layout(f"@{target}", header + posts_html, user, theme or "light"))


# ------------------------------ Уведомления ------------------------------
@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(session: Optional[str] = Cookie(default=None),
                       theme: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    if not user:
        return RedirectResponse("/login", status_code=303)

    mine = [n for n in NOTIFICATIONS if n["user"] == user]

    # помечаем как прочитанные
    for n in mine:
        n["read"] = True

    if not mine:
        body = empty_card("Уведомлений пока нет", "notifications_none")
    else:
        items = []
        for n in mine:
            if n["kind"] == "mention":
                text = f'<b>{html.escape(n["from"])}</b> упомянул вас в посте'
                icon = "alternate_email"
            elif n["kind"] == "like":
                text = f'<b>{html.escape(n["from"])}</b> оценил ваш пост'
                icon = "favorite"
            else:
                text = f'<b>{html.escape(n["from"])}</b>'
                icon = "notifications"
            href = f'/post/{n["post_id"]}' if n.get("post_id") else "/"
            cls = "notif" + ("" if n["read"] else " unread")
            items.append(f"""
<a class="{cls}" href="{href}">
  <div class="notif-icon"><i class="material-icons">{icon}</i></div>
  <div class="notif-content">
    <div class="notif-text">{text}</div>
    <div class="notif-time">{fmt_time(n['ts'])}</div>
  </div>
</a>""")
        body = '<div class="card">' + "".join(items) + "</div>"

    return HTMLResponse(layout("Уведомления", body, user, theme or "light"))


# ------------------------------ Поиск ------------------------------
@app.get("/search", response_class=HTMLResponse)
def search(q: str = "",
           session: Optional[str] = Cookie(default=None),
           theme: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    q = q.strip()

    if not q:
        body = empty_card("Введите запрос в поле поиска", "search")
        return HTMLResponse(layout("Поиск", body, user, theme or "light"))

    ql = q.lower()
    users_found = [u for u in USERS if ql in u.lower()]
    posts_found = [p for p in POSTS if ql in p["text"].lower()
                   or ql in p["author"].lower()]

    parts = [f'<h2 class="page-title">Результаты: «{html.escape(q)}»</h2>']

    if users_found:
        chips = "".join(
            f'<a class="user-chip" href="/u/{html.escape(u)}">'
            f'{avatar(u)}<div><b>{html.escape(u)}</b>'
            f'<div class="small muted">Постов: '
            f'{sum(1 for p in POSTS if p["author"] == u)}</div></div></a>'
            for u in users_found
        )
        parts.append(f'<h3 class="small muted" style="margin:8px 4px">Люди</h3>'
                     f'<div class="card">{chips}</div>')

    if posts_found:
        parts.append('<h3 class="small muted" style="margin:16px 4px 8px">Посты</h3>')
        parts.append("".join(render_post(p, user, show_delete=False) for p in posts_found))

    if not users_found and not posts_found:
        parts.append(empty_card("Ничего не найдено", "search_off"))

    return HTMLResponse(layout("Поиск", "".join(parts), user, theme or "light"))
