# -*- coding: utf-8 -*-
"""
СЛД — социальная сеть/форум в старом стиле (образец: ВКонтакте 2008).
Только посты. Регистрация, вход, сессии, CRUD постов.

Запуск:
    export SUPABASE_URL=...
    export SUPABASE_KEY=...      # service_role
    uvicorn main:app --reload --port 8000
"""

import os
import re
import html
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from supabase import create_client


# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Не заданы SUPABASE_URL и/или SUPABASE_KEY")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

SESSION_COOKIE = "sld_sid"
SESSION_DAYS = 30
POST_MAX = 2000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

app = FastAPI(title="СЛД", docs_url=None, redoc_url=None)


# ----------------------------------------------------------------------------
# Утилиты: пароли, сессии, форматирование
# ----------------------------------------------------------------------------

def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iters = 120_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iters)
    return f"pbkdf2_sha256${iters}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, hexhash = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iters)
        )
        return secrets.compare_digest(dk.hex(), hexhash)
    except Exception:
        return False


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн",
          "июл", "авг", "сен", "окт", "ноя", "дек"]


def fmt_dt(value: Any) -> str:
    dt = parse_dt(value)
    if not dt:
        return ""
    dt = dt.astimezone()
    now = datetime.now(timezone.utc).astimezone()
    if dt.date() == now.date():
        return f"сегодня в {dt:%H:%M}"
    if (now.date() - dt.date()).days == 1:
        return f"вчера в {dt:%H:%M}"
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year} в {dt:%H:%M}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def avatar_color(name: str) -> str:
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:6], 16)
    r = 80 + (h & 0x3F)
    g = 90 + ((h >> 6) & 0x3F)
    b = 110 + ((h >> 12) & 0x2F)
    return f"#{r:02x}{g:02x}{b:02x}"


def avatar_html(username: str) -> str:
    letter = esc((username or "?")[0].upper())
    color = avatar_color(username or "?")
    return f'<div class="av" style="background:{color}">{letter}</div>'


# ----------------------------------------------------------------------------
# Сессии
# ----------------------------------------------------------------------------

def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    sb.table("sessions").insert({
        "token": token,
        "user_id": user_id,
        "expires_at": expires.isoformat(),
    }).execute()
    return token


def set_session_cookie(resp: RedirectResponse, token: str) -> None:
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True, samesite="lax", path="/",
    )


def current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        rows = (sb.table("sessions")
                  .select("token,user_id,expires_at")
                  .eq("token", token).limit(1).execute().data)
    except Exception:
        return None
    if not rows:
        return None
    row = rows[0]
    exp = parse_dt(row.get("expires_at"))
    if not exp or exp < datetime.now(timezone.utc):
        try:
            sb.table("sessions").delete().eq("token", token).execute()
        except Exception:
            pass
        return None
    try:
        users = (sb.table("users")
                   .select("id,username,created_at")
                   .eq("id", row["user_id"]).limit(1).execute().data)
    except Exception:
        return None
    return users[0] if users else None


def destroy_session(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            sb.table("sessions").delete().eq("token", token).execute()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Вёрстка (стиль ~2008)
# ----------------------------------------------------------------------------

CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:#E9EDF0;font:11px/1.45 Verdana,Tahoma,Arial,sans-serif;color:#222}
a{color:#2B587A;text-decoration:none}
a:hover{text-decoration:underline}
img{border:0}
#top{background:#45688E;background:linear-gradient(#5D80A6,#45688E);border-bottom:1px solid #2B587A;height:44px}
#top .wrap{display:flex;align-items:center;height:44px}
.wrap{width:800px;margin:0 auto}
.logo{color:#fff;font:bold 22px/1 Verdana,Tahoma,sans-serif;letter-spacing:2px;text-shadow:0 1px 1px rgba(0,0,0,.35)}
.logo:hover{text-decoration:none}
.hmenu{margin-left:auto;color:#C6D6E6;font-size:11px}
.hmenu a{color:#fff}
.hmenu .sep{color:#8FAAC6;padding:0 4px}
#page{padding:12px 0 50px}
.cols{display:flex;gap:12px;align-items:flex-start}
#side{width:168px;flex:none}
#content{flex:1;min-width:0}
.box{background:#fff;border:1px solid #C5D0DA;border-radius:4px;margin-bottom:12px;overflow:hidden}
.box .bh{background:#E4EAF0;border-bottom:1px solid #C5D0DA;padding:5px 8px;font-weight:bold;color:#2B587A}
.box .bh .cnt{float:right;font-weight:normal;color:#7E8B99}
.box .bb{padding:9px}
.menu a{display:block;padding:5px 9px;border-bottom:1px solid #EDF0F3;color:#2B587A}
.menu a:last-child{border-bottom:none}
.menu a:hover{background:#F1F5F9;text-decoration:none}
.menu a.on{background:#E4EAF0;font-weight:bold}
.post{display:flex;gap:9px;padding:10px;border-bottom:1px solid #E9EEF2}
.post:last-child{border-bottom:none}
.post .av{width:50px;height:50px;flex:none;border:1px solid #A9B8C6;border-radius:3px;
          color:#fff;font:bold 22px/48px Verdana,sans-serif;text-align:center;
          text-shadow:0 1px 1px rgba(0,0,0,.3)}
.pbody{flex:1;min-width:0}
.pname{font-weight:bold}
.pdate{color:#8996A3;font-size:10px}
.ptext{margin-top:4px;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:break-word}
.pacts{margin-top:5px;font-size:10px;color:#8996A3}
.pacts a{color:#6C8199}
.pacts form{display:inline;margin:0}
.pacts button{background:none;border:0;padding:0;color:#6C8199;font:10px Verdana,sans-serif;cursor:pointer}
.pacts button:hover{text-decoration:underline}
textarea,input[type=text],input[type=password]{
  border:1px solid #A9B8C6;padding:4px 5px;font:11px/1.4 Verdana,Tahoma,sans-serif;
  border-radius:2px;background:#fff;color:#222}
textarea:focus,input[type=text]:focus,input[type=password]:focus{
  outline:none;border-color:#4A76A8;background:#FAFCFE}
textarea{width:100%;resize:vertical}
.btn{background:#E4EAF0;border:1px solid #A9B8C6;padding:4px 14px;font:11px Verdana,sans-serif;
     cursor:pointer;border-radius:2px;color:#2B587A}
.btn:hover{background:#D6E0EA}
.btn:active{background:#C9D6E2}
.err{background:#FBE3E3;border:1px solid #E5A9A9;color:#8A2A2A;padding:6px 8px;
     border-radius:3px;margin-bottom:9px}
.ok{background:#E6F2DE;border:1px solid #A9CFA0;color:#2F6B23;padding:6px 8px;
    border-radius:3px;margin-bottom:9px}
.field{margin-bottom:8px}
.field label{display:block;color:#55697A;margin-bottom:3px}
.field input{width:280px}
.muted{color:#8996A3}
.hint{color:#8996A3;font-size:10px;margin-top:3px}
.center{text-align:center}
h1.ph{margin:0 0 10px;font:bold 15px Verdana,sans-serif;color:#2B587A}
"""


def layout(title: str, user: Optional[dict], body: str, active: str = "") -> str:
    if user:
        un = esc(user["username"])
        auth = f'<a href="/u/{un}">{un}</a><span class="sep">·</span><a href="/logout">выход</a>'
    else:
        auth = '<a href="/login">вход</a><span class="sep">·</span><a href="/register">регистрация</a>'

    items = [("/", "Все записи", "feed")]
    if user:
        items.insert(0, (f"/u/{esc(user['username'])}", "Моя страница", "me"))
    items.append(("/people", "Люди", "people"))

    menu = "".join(
        f'<a href="{href}"{" class=\"on\"" if key == active else ""}>{esc(name)}</a>'
        for href, name, key in items
    )

    if user:
        side_footer = f'<div class="box"><div class="bb muted">Вы вошли как<br><b>{esc(user["username"])}</b></div></div>'
    else:
        side_footer = ('<div class="box"><div class="bb">'
                       '<a href="/login">Войти</a><br><a href="/register">Регистрация</a>'
                       '</div></div>')

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=820">
<title>{esc(title)} — СЛД</title>
<style>{CSS}</style>
</head>
<body>
<div id="top"><div class="wrap">
  <a class="logo" href="/">СЛД</a>
  <div class="hmenu">{auth}</div>
</div></div>
<div id="page"><div class="wrap"><div class="cols">
  <div id="side">
    <div class="box"><div class="menu">{menu}</div></div>
    {side_footer}
    <div class="box"><div class="bb muted center">СЛД &copy; 2008&ndash;2026<br>version 0.1</div></div>
  </div>
  <div id="content">{body}</div>
</div></div></div>
</body>
</html>"""


def render_posts(posts: list, me: Optional[dict], empty_text: str = "Пока ни одной записи.") -> str:
    if not posts:
        return f'<div class="bb muted">{esc(empty_text)}</div>'
    out = []
    for p in posts:
        u = p.get("users")
        if isinstance(u, list):
            u = u[0] if u else {}
        u = u or {}
        uname = u.get("username") or "удалён"
        pid = p.get("id")
        content = esc(p.get("content", ""))
        created = fmt_dt(p.get("created_at"))
        edited = ""
        if p.get("updated_at"):
            edited = f' <span class="muted">· ред. {esc(fmt_dt(p["updated_at"]))}</span>'

        actions = ""
        if me and p.get("user_id") == me["id"]:
            actions = (
                '<div class="pacts">'
                f'<a href="/posts/{pid}/edit">редактировать</a> · '
                f'<form method="post" action="/posts/{pid}/delete" '
                f'onsubmit="return confirm(\'Удалить запись?\')">'
                f'<button type="submit">удалить</button></form>'
                '</div>'
            )

        out.append(f"""<div class="post">
  {avatar_html(uname)}
  <div class="pbody">
    <div><span class="pname"><a href="/u/{esc(uname)}">{esc(uname)}</a></span>
         <span class="pdate">· {esc(created)}{edited}</span></div>
    <div class="ptext">{content}</div>
    {actions}
  </div>
</div>""")
    return "".join(out)


def page_feed(user: Optional[dict], posts: list, msg: str = "", err: str = "") -> str:
    alerts = ""
    if err:
        alerts += f'<div class="err">{esc(err)}</div>'
    if msg:
        alerts += f'<div class="ok">{esc(msg)}</div>'

    if user:
        compose = f"""<div class="box">
  <div class="bh">Что у вас нового?</div>
  <div class="bb">
    <form method="post" action="/posts">
      <textarea name="content" rows="3" maxlength="{POST_MAX}"
                placeholder="Напишите что-нибудь..."></textarea>
      <div style="margin-top:6px">
        <button class="btn" type="submit">Отправить</button>
        <span class="hint" style="margin-left:8px">до {POST_MAX} символов</span>
      </div>
    </form>
  </div>
</div>"""
    else:
        compose = ('<div class="box"><div class="bb">'
                   'Чтобы оставлять записи, <a href="/login">войдите</a> '
                   'или <a href="/register">зарегистрируйтесь</a>.'
                   '</div></div>')

    count = len(posts)
    body = f"""{alerts}
{compose}
<div class="box">
  <div class="bh">Лента <span class="cnt">{count} {esc(plural(count, "запись", "записи", "записей"))}</span></div>
  {render_posts(posts, user)}
</div>"""
    return layout("Все записи", user, body, active="feed")


def page_auth(title: str, user: Optional[dict], form: str, err: str = "") -> str:
    alerts = f'<div class="err">{esc(err)}</div>' if err else ""
    body = f'<div class="box" style="max-width:420px">' \
           f'<div class="bh">{esc(title)}</div>' \
           f'<div class="bb">{alerts}{form}</div></div>'
    return layout(title, user, body)


# ----------------------------------------------------------------------------
# Выборки
# ----------------------------------------------------------------------------

POST_SELECT = "id,user_id,content,created_at,updated_at,users(username)"


def fetch_posts(limit: int = 50, user_id: Optional[str] = None) -> list:
    q = sb.table("posts").select(POST_SELECT).order("created_at", desc=True).limit(limit)
    if user_id:
        q = q.eq("user_id", user_id)
    return q.execute().data or []


# ----------------------------------------------------------------------------
# Маршруты: лента
# ----------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str = "", err: str = ""):
    user = current_user(request)
    try:
        posts = fetch_posts()
    except Exception as e:
        posts = []
        err = err or f"Ошибка базы данных: {e}"
    return HTMLResponse(page_feed(user, posts, msg=msg, err=err))


# ----------------------------------------------------------------------------
# Маршруты: регистрация / вход / выход
# ----------------------------------------------------------------------------

@app.get("/register", response_class=HTMLResponse)
def register_get(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    form = """
<form method="post" action="/register">
  <div class="field"><label>Имя пользователя</label>
    <input type="text" name="username" maxlength="20" autocomplete="username" autofocus></div>
  <div class="hint" style="margin:-5px 0 8px">3&ndash;20 символов: латиница, цифры, «_»</div>
  <div class="field"><label>Пароль</label>
    <input type="password" name="password" autocomplete="new-password"></div>
  <div class="field"><label>Пароль ещё раз</label>
    <input type="password" name="password2" autocomplete="new-password"></div>
  <button class="btn" type="submit">Зарегистрироваться</button>
</form>"""
    return HTMLResponse(page_auth("Регистрация", None, form))


@app.post("/register", response_class=HTMLResponse)
def register_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
):
    if current_user(request):
        return RedirectResponse("/", status_code=303)

    username = username.strip()
    err = None

    if not USERNAME_RE.fullmatch(username):
        err = "Имя: 3–20 символов, только латиница, цифры и подчёркивание."
    elif len(password) < 6:
        err = "Пароль должен быть не короче 6 символов."
    elif password != password2:
        err = "Пароли не совпадают."
    else:
        try:
            exists = (sb.table("users").select("id")
                        .ilike("username", username).limit(1).execute().data)
            if exists:
                err = "Такое имя уже занято."
        except Exception as e:
            err = f"Ошибка базы данных: {e}"

    if err:
        form = f"""
<form method="post" action="/register">
  <div class="field"><label>Имя пользователя</label>
    <input type="text" name="username" maxlength="20" value="{esc(username)}"></div>
  <div class="field"><label>Пароль</label>
    <input type="password" name="password"></div>
  <div class="field"><label>Пароль ещё раз</label>
    <input type="password" name="password2"></div>
  <button class="btn" type="submit">Зарегистрироваться</button>
</form>"""
        return HTMLResponse(page_auth("Регистрация", None, form, err), status_code=400)

    try:
        row = sb.table("users").insert({
            "username": username,
            "password_hash": hash_password(password),
        }).execute().data[0]
    except Exception as e:
        form = """
<form method="post" action="/register">
  <div class="field"><label>Имя пользователя</label>
    <input type="text" name="username" maxlength="20"></div>
  <div class="field"><label>Пароль</label>
    <input type="password" name="password"></div>
  <div class="field"><label>Пароль ещё раз</label>
    <input type="password" name="password2"></div>
  <button class="btn" type="submit">Зарегистрироваться</button>
</form>"""
        return HTMLResponse(page_auth("Регистрация", None, form, f"Не удалось создать: {e}"), status_code=500)

    token = create_session(row["id"])
    resp = RedirectResponse("/", status_code=303)
    set_session_cookie(resp, token)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    form = """
<form method="post" action="/login">
  <div class="field"><label>Имя пользователя</label>
    <input type="text" name="username" maxlength="20" autocomplete="username" autofocus></div>
  <div class="field"><label>Пароль</label>
    <input type="password" name="password" autocomplete="current-password"></div>
  <button class="btn" type="submit">Войти</button>
</form>"""
    return HTMLResponse(page_auth("Вход", None, form))


@app.post("/login", response_class=HTMLResponse)
def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    if current_user(request):
        return RedirectResponse("/", status_code=303)

    username = username.strip()
    err = None
    row = None

    try:
        rows = (sb.table("users")
                  .select("id,username,password_hash")
                  .ilike("username", username).limit(1).execute().data)
        row = rows[0] if rows else None
    except Exception as e:
        err = f"Ошибка базы данных: {e}"

    if not err:
        if not row or not verify_password(password, row["password_hash"]):
            err = "Неверное имя пользователя или пароль."

    if err:
        form = f"""
<form method="post" action="/login">
  <div class="field"><label>Имя пользователя</label>
    <input type="text" name="username" maxlength="20" value="{esc(username)}"></div>
  <div class="field"><label>Пароль</label>
    <input type="password" name="password"></div>
  <button class="btn" type="submit">Войти</button>
</form>"""
        return HTMLResponse(page_auth("Вход", None, form, err), status_code=400)

    token = create_session(row["id"])
    resp = RedirectResponse("/", status_code=303)
    set_session_cookie(resp, token)
    return resp


@app.get("/logout")
def logout(request: Request):
    destroy_session(request)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ----------------------------------------------------------------------------
# Маршруты: посты
# ----------------------------------------------------------------------------

@app.post("/posts")
def create_post(request: Request, content: str = Form("")):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    content = (content or "").strip()
    if not content:
        return RedirectResponse("/?err=" + "Пустая+запись", status_code=303)
    if len(content) > POST_MAX:
        content = content[:POST_MAX]

    try:
        sb.table("posts").insert({
            "user_id": user["id"],
            "content": content,
        }).execute()
    except Exception as e:
        return RedirectResponse(f"/?err=Не+удалось+сохранить", status_code=303)

    return RedirectResponse("/?msg=Запись+опубликована", status_code=303)


@app.get("/posts/{post_id}/edit", response_class=HTMLResponse)
def edit_post_get(request: Request, post_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("posts").select("id,user_id,content")
              .eq("id", post_id).limit(1).execute().data)
    if not rows:
        return HTMLResponse(layout("Не найдено", user,
                                   '<div class="box"><div class="bb">Запись не найдена.</div></div>'),
                            status_code=404)
    post = rows[0]
    if post["user_id"] != user["id"]:
        return HTMLResponse(layout("Отказано", user,
                                   '<div class="box"><div class="bb">Это не ваша запись.</div></div>'),
                            status_code=403)

    body = f"""<div class="box">
  <div class="bh">Редактирование записи</div>
  <div class="bb">
    <form method="post" action="/posts/{post_id}/edit">
      <textarea name="content" rows="6" maxlength="{POST_MAX}">{esc(post['content'])}</textarea>
      <div style="margin-top:6px">
        <button class="btn" type="submit">Сохранить</button>
        <a href="/" style="margin-left:10px">отмена</a>
      </div>
    </form>
  </div>
</div>"""
    return HTMLResponse(layout("Редактирование", user, body))


@app.post("/posts/{post_id}/edit")
def edit_post_post(request: Request, post_id: int, content: str = Form("")):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("posts").select("id,user_id")
              .eq("id", post_id).limit(1).execute().data)
    if not rows or rows[0]["user_id"] != user["id"]:
        return RedirectResponse("/", status_code=303)

    content = (content or "").strip()
    if not content:
        return RedirectResponse(f"/posts/{post_id}/edit", status_code=303)
    if len(content) > POST_MAX:
        content = content[:POST_MAX]

    sb.table("posts").update({
        "content": content,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", post_id).execute()

    return RedirectResponse("/?msg=Запись+обновлена", status_code=303)


@app.post("/posts/{post_id}/delete")
def delete_post(request: Request, post_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("posts").select("id,user_id")
              .eq("id", post_id).limit(1).execute().data)
    if rows and rows[0]["user_id"] == user["id"]:
        sb.table("posts").delete().eq("id", post_id).execute()

    return RedirectResponse("/?msg=Запись+удалена", status_code=303)


# ----------------------------------------------------------------------------
# Маршруты: профиль и люди
# ----------------------------------------------------------------------------

@app.get("/u/{username}", response_class=HTMLResponse)
def profile(request: Request, username: str, msg: str = "", err: str = ""):
    me = current_user(request)
    rows = (sb.table("users").select("id,username,created_at")
              .ilike("username", username).limit(1).execute().data)
    if not rows:
        return HTMLResponse(
            layout("Не найдено", me,
                   '<div class="box"><div class="bb">Пользователь не найден.</div></div>'),
            status_code=404)

    owner = rows[0]
    try:
        posts = fetch_posts(user_id=owner["id"])
    except Exception as e:
        posts = []
        err = err or f"Ошибка базы данных: {e}"

    alerts = ""
    if err:
        alerts += f'<div class="err">{esc(err)}</div>'
    if msg:
        alerts += f'<div class="ok">{esc(msg)}</div>'

    is_me = bool(me and me["id"] == owner["id"])
    compose = ""
    if is_me:
        compose = f"""<div class="box">
  <div class="bh">Новая запись</div>
  <div class="bb">
    <form method="post" action="/posts">
      <textarea name="content" rows="3" maxlength="{POST_MAX}" placeholder="Напишите что-нибудь..."></textarea>
      <div style="margin-top:6px"><button class="btn" type="submit">Отправить</button></div>
    </form>
  </div>
</div>"""

    cnt = len(posts)
    body = f"""{alerts}
<div class="box">
  <div class="bb" style="display:flex;gap:12px;align-items:center">
    {avatar_html(owner['username']).replace('class="av"', 'class="av" style="width:80px;height:80px;line-height:78px;font-size:34px;"')}
    <div>
      <h1 class="ph" style="margin:0 0 4px">{esc(owner['username'])}</h1>
      <div class="muted">на сайте с {esc(fmt_dt(owner.get('created_at')))}</div>
      <div class="muted">{cnt} {esc(plural(cnt, "запись", "записи", "записей"))}</div>
    </div>
  </div>
</div>
{compose}
<div class="box">
  <div class="bh">Записи <span class="cnt">{cnt}</span></div>
  {render_posts(posts, me, empty_text=("Вы ещё ничего не написали." if is_me else "Записей нет."))}
</div>"""
    return HTMLResponse(layout(owner["username"], me, body, active="me" if is_me else ""))


@app.get("/people", response_class=HTMLResponse)
def people(request: Request):
    me = current_user(request)
    try:
        users = (sb.table("users").select("id,username,created_at")
                   .order("created_at", desc=True).limit(200).execute().data) or []
    except Exception as e:
        users = []
        me_err = str(e)
    else:
        me_err = ""

    rows = []
    for u in users:
        un = esc(u["username"])
        rows.append(f"""<div class="post">
  {avatar_html(u['username'])}
  <div class="pbody">
    <div><span class="pname"><a href="/u/{un}">{un}</a></span></div>
    <div class="pdate">на сайте с {esc(fmt_dt(u.get('created_at')))}</div>
  </div>
</div>""")

    inner = "".join(rows) if rows else '<div class="bb muted">Пусто.</div>'
    alerts = f'<div class="err">{esc(me_err)}</div>' if me_err else ""
    body = f"""{alerts}
<div class="box">
  <div class="bh">Люди <span class="cnt">{len(users)}</span></div>
  {inner}
</div>"""
    return HTMLResponse(layout("Люди", me, body, active="people"))


# ----------------------------------------------------------------------------
# Служебное
# ----------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
