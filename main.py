# -*- coding: utf-8 -*-
"""
СЛД — старая социальная сеть / форум образца начала 2000-х.
Один файл. Регистрация, вход, профиль с фото, темы (посты),
комментарии. БД — Supabase (service_role).

Запуск:
    export SUPABASE_URL=...
    export SUPABASE_KEY=...
    uvicorn main:app --reload --port 8000
"""

import os
import re
import html
import base64
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from supabase import create_client


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Не заданы SUPABASE_URL и/или SUPABASE_KEY")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

SESSION_COOKIE = "sld_sid"
SESSION_DAYS = 30

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
POST_MAX = 5000
COMMENT_MAX = 2000
AVATAR_MAX = 150_000   # байт, ~150 КБ

app = FastAPI(title="СЛД", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iters = 120_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), iters)
    return f"pbkdf2_sha256${iters}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, hexhash = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), int(iters))
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


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]


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
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year} г."


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
    h = int(hashlib.md5((name or "?").encode("utf-8")).hexdigest()[:6], 16)
    r = 100 + (h & 0x4F)
    g = 110 + ((h >> 8) & 0x4F)
    b = 130 + ((h >> 16) & 0x3F)
    return f"#{r:02x}{g:02x}{b:02x}"


def avatar_html(username: str, avatar: Optional[str], size: int = 50) -> str:
    """Аватар: либо загруженная картинка, либо цветной квадрат с буквой."""
    if avatar:
        return (f'<img class="av" src="{esc(avatar)}" '
                f'width="{size}" height="{size}" alt="">')
    letter = esc((username or "?")[0].upper())
    color = avatar_color(username or "?")
    return (f'<div class="av av-letter" style="background:{color};'
            f'width:{size}px;height:{size}px;line-height:{size}px;'
            f'font-size:{int(size * 0.45)}px">{letter}</div>')


def user_card(u: dict, size: int = 50) -> str:
    """Компактная карточка пользователя (аватар + ник), для списков."""
    return (f'<div class="ucard">{avatar_html(u.get("username", ""), u.get("avatar"), size)}'
            f'<div><a href="/u/{esc(u.get("username"))}">{esc(u.get("username"))}</a>'
            f'<div class="muted">{esc(fmt_dt(u.get("created_at")))}</div></div></div>')


# ---------------------------------------------------------------------------
# Сессии
# ---------------------------------------------------------------------------

def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    sb.table("sessions").insert({
        "token": token, "user_id": user_id,
        "expires_at": expires.isoformat(),
    }).execute()
    return token


def set_session_cookie(resp: RedirectResponse, token: str) -> None:
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 24 * 3600,
                    httponly=True, samesite="lax", path="/")


def current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        rows = (sb.table("sessions").select("token,user_id,expires_at")
                .eq("token", token).limit(1).execute().data)
    except Exception:
        return None
    if not rows:
        return None
    exp = parse_dt(rows[0].get("expires_at"))
    if not exp or exp < datetime.now(timezone.utc):
        try:
            sb.table("sessions").delete().eq("token", token).execute()
        except Exception:
            pass
        return None
    try:
        u = (sb.table("users").select("*")
             .eq("id", rows[0]["user_id"]).limit(1).execute().data)
    except Exception:
        return None
    return u[0] if u else None


def destroy_session(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            sb.table("sessions").delete().eq("token", token).execute()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Стиль: старая сеть, ~2003 год
# ---------------------------------------------------------------------------

CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:"Times New Roman",Times,Georgia,serif;
  font-size:14px;
  color:#1A1A1A;
  background:#4E6478;
  background-image:
    repeating-linear-gradient(45deg, rgba(255,255,255,.025) 0 1px, transparent 1px 5px),
    repeating-linear-gradient(-45deg, rgba(0,0,0,.035) 0 1px, transparent 1px 5px);
  padding:14px 0 40px;
}
a{color:#000099;text-decoration:underline}
a:visited{color:#551A8B}
a:hover{color:#CC0000}
img{border:0}

.wrap{width:860px;margin:0 auto}

/* Шапка */
.head{
  border:1px solid #1A2E42;
  border-bottom:none;
  background:#2B4A66;
  background-image:linear-gradient(#3F6486,#203A52);
}
.head-top{padding:10px 16px 6px;border-bottom:1px solid #142536}
.logo{
  font-family:Georgia,"Times New Roman",serif;
  font-size:34px;letter-spacing:8px;font-weight:bold;
  color:#FFD966;text-decoration:none;
  text-shadow:2px 2px 0 #0A1620;
}
.logo:hover{color:#FFEEA8;text-decoration:none}
.tagline{color:#B8CCDD;font-size:12px;font-style:italic;margin-top:2px}
.head-right{float:right;color:#C4D4E3;font-size:12px;text-align:right;padding-top:6px}
.head-right a{color:#FFD966}

/* Навигация */
.nav{
  background:#E4DCC4;
  border:1px solid #A89A78;
  border-top:none;border-bottom:1px solid #A89A78;
  padding:4px 12px;
  font-size:13px;
}
.nav a{color:#2B4A66;text-decoration:none;padding:1px 2px}
.nav a:hover{color:#CC0000;text-decoration:underline}
.nav a.on{font-weight:bold;color:#000;text-decoration:underline}
.nav .sep{color:#8A7A58;padding:0 6px}
.nav .right{float:right}

/* Основной контейнер */
.main{background:#F5F1E5;border:1px solid #A89A78;border-top:none;padding:12px}
.cols{display:flex;gap:12px;align-items:flex-start}
.side{width:200px;flex:none}
.content{flex:1;min-width:0}

/* Коробки */
.box{background:#FFFFFF;border:1px solid #A89A78;margin-bottom:12px}
.box-title{
  background:#D8CFB8;
  background-image:linear-gradient(#EAE2C9,#C8BEA0);
  border-bottom:1px solid #A89A78;
  padding:4px 10px;font-weight:bold;color:#2B2418;font-size:13px;
}
.box-title .cnt{float:right;font-weight:normal;color:#6A5F44;font-size:12px}
.box-body{padding:10px}

/* Ссылки в боковом меню */
.smenu a{
  display:block;padding:4px 10px;border-bottom:1px dashed #D8CFB8;
  color:#2B4A66;text-decoration:none;
}
.smenu a:last-child{border-bottom:none}
.smenu a:hover{background:#F0EAD5;color:#CC0000}
.smenu a.on{background:#E4DCC4;font-weight:bold}

/* Аватары */
.av{display:block;border:1px solid #6A5F44;background:#999}
.av-letter{
  color:#fff;text-align:center;font-weight:bold;
  text-shadow:1px 1px 1px rgba(0,0,0,.5);
  font-family:Georgia,serif;
}

/* Пользователь */
.ucard{display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-bottom:1px dotted #D8CFB8}
.ucard:last-child{border-bottom:none}

/* Записи (темы) */
.post{display:flex;gap:10px;padding:12px 10px;border-bottom:1px solid #E8E0C8}
.post:last-child{border-bottom:none}
.pbody{flex:1;min-width:0}
.pname{font-weight:bold;font-size:14px}
.pdate{color:#7A6F50;font-size:12px}
.ptext{margin-top:5px;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:break-word;line-height:1.5}
.pacts{margin-top:7px;font-size:12px;color:#7A6F50;padding-top:5px;border-top:1px dotted #E8E0C8}
.pacts a,.pacts button{color:#000099}
.pacts form{display:inline;margin:0}
.pacts button{
  background:none;border:none;padding:0;color:#000099;cursor:pointer;
  font:12px "Times New Roman",serif;text-decoration:underline;
}
.pacts button:hover{color:#CC0000}

/* Формы */
textarea,input[type=text],input[type=password],input[type=url],input[type=date]{
  font-family:"Times New Roman",Times,serif;font-size:13px;
  border:2px inset #D8CFB8;
  border-color:#8A7A58 #F0E8D0 #F0E8D0 #8A7A58;
  padding:3px 5px;background:#FFFFFF;color:#1A1A1A;
  border-radius:0;
}
textarea:focus,input:focus{outline:none;background:#FFFFF0}
textarea{width:100%;resize:vertical;line-height:1.5}

button,.btn{
  font-family:"Times New Roman",serif;font-size:13px;
  padding:3px 16px;cursor:pointer;
  background:#D8CFB8;color:#2B2418;
  border:2px solid;
  border-color:#F0E8D0 #8A7A58 #8A7A58 #F0E8D0;
  border-radius:0;
}
button:hover,.btn:hover{background:#E4DCC4}
button:active,.btn:active{
  border-color:#8A7A58 #F0E8D0 #F0E8D0 #8A7A58;
}

.field{margin-bottom:8px}
.field label{display:block;color:#4A4028;margin-bottom:3px;font-weight:bold}
.field input[type=text],
.field input[type=password],
.field input[type=url],
.field input[type=date]{width:300px}

.err{background:#FBE3E3;border:1px solid #C47878;color:#8A2A2A;padding:6px 10px;margin-bottom:10px}
.ok{background:#E6F2DE;border:1px solid #88B078;color:#2F6B23;padding:6px 10px;margin-bottom:10px}

.muted{color:#7A6F50;font-size:12px}
.hint{color:#7A6F50;font-size:12px;margin-top:3px}
.center{text-align:center}

h1.ph{
  margin:0 0 6px;font-family:Georgia,serif;font-size:22px;
  color:#2B4A66;font-weight:bold;letter-spacing:1px;
}
h2.pht{
  margin:0 0 8px;font-family:Georgia,serif;font-size:16px;
  color:#2B4A66;border-bottom:1px solid #D8CFB8;padding-bottom:4px;
}

.profile-card{display:flex;gap:14px;align-items:flex-start;padding:14px}
.profile-card .info{flex:1;min-width:0}
.status{font-style:italic;color:#4A4028;margin:3px 0 8px}
.info-table{width:100%;border-collapse:collapse}
.info-table td{padding:3px 6px;vertical-align:top;border-bottom:1px dotted #E8E0C8}
.info-table td.k{color:#7A6F50;width:140px;white-space:nowrap}

.comment{display:flex;gap:9px;padding:10px;border-bottom:1px solid #E8E0C8}
.comment:last-child{border-bottom:none}

/* Подвал */
.foot{
  border:1px solid #A89A78;border-top:none;
  background:#E4DCC4;padding:8px 12px;font-size:12px;color:#5A5038;
}
.foot .left{float:left}
.foot .right{float:right;text-align:right}
.counter{
  display:inline-block;
  background:#000;color:#00FF00;
  font-family:"Courier New",monospace;font-size:12px;letter-spacing:2px;
  padding:1px 6px;border:1px solid #333;
}
.old-note{
  margin-top:8px;color:#5A5038;font-size:11px;font-style:italic;
}
.clear{clear:both}
"""


def layout(title: str, user: Optional[dict], body: str, active: str = "") -> str:
    if user:
        un = esc(user["username"])
        head_right = (f'Вы вошли как <b><a href="/u/{un}">{un}</a></b> · '
                      f'<a href="/settings">настройки</a> · '
                      f'<a href="/logout">выход</a>')
    else:
        head_right = '<a href="/login">Вход</a> · <a href="/register">Регистрация</a>'

    nav = [
        ("/", "Главная", "feed"),
        ("/people", "Участники", "people"),
    ]
    if user:
        nav.append((f"/u/{esc(user['username'])}", "Мой профиль", "me"))
        nav.append(("/settings", "Настройки", "settings"))

    nav_html = '<span class="sep">|</span>'.join(
        f'<a href="{h}"{" class=\"on\"" if k == active else ""}>{esc(n)}</a>'
        for h, n, k in nav
    )

    # Боковая панель
    side = ""
    if user:
        side += (f'<div class="box"><div class="box-title">Пользователь</div>'
                 f'<div class="box-body">'
                 f'<div class="ucard">{avatar_html(user["username"], user.get("avatar"), 60)}'
                 f'<div><a href="/u/{esc(user["username"])}"><b>{esc(user["username"])}</b></a>'
                 f'<div class="muted">{esc(fmt_dt(user.get("created_at")))}</div></div></div>'
                 f'</div></div>')
        side += ('<div class="box"><div class="box-title">Меню</div>'
                 '<div class="smenu">'
                 '<a href="/">Все темы</a>'
                 f'<a href="/u/{esc(user["username"])}">Мои записи</a>'
                 '<a href="/people">Участники</a>'
                 '<a href="/settings">Настройки профиля</a>'
                 '<a href="/logout">Выход</a>'
                 '</div></div>')
    else:
        side += ('<div class="box"><div class="box-title">Вход</div>'
                 '<div class="box-body">'
                 '<form method="post" action="/login">'
                 '<div class="field"><label>Имя</label>'
                 '<input type="text" name="username" maxlength="20" style="width:100%"></div>'
                 '<div class="field"><label>Пароль</label>'
                 '<input type="password" name="password" style="width:100%"></div>'
                 '<button type="submit" style="width:100%">Войти</button>'
                 '</form>'
                 '<div style="margin-top:8px;text-align:center">'
                 'или <a href="/register">зарегистрируйтесь</a>'
                 '</div></div></div>')

    side += ('<div class="box"><div class="box-title">Статистика</div>'
             '<div class="box-body muted">СЛД · форум<br>'
             'основан в 2004 году<br>'
             'движок: SLDengine 0.9.3<br>'
             'сегодня: ' + datetime.now().strftime("%d.%m.%Y") +
             '</div></div>')

    side += ('<div class="box"><div class="box-title">Реклама</div>'
             '<div class="box-body center muted">'
             '<div style="border:1px dashed #A89A78;padding:12px 4px;font-size:12px">'
             'Здесь могла быть<br>ваша реклама<br>'
             '<b>8 (095) 123-45-67</b></div></div></div>')

    year = datetime.now().year
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=880">
<meta name="generator" content="SLDengine 0.9.3">
<title>{esc(title)} — СЛД</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

  <div class="head">
    <div class="head-top">
      <div class="head-right">{head_right}</div>
      <a class="logo" href="/">С&nbsp;Л&nbsp;Д</a>
      <div class="tagline">Сообщество. Люди. Дискуссии. — с 2004 года</div>
    </div>
    <div class="nav">{nav_html}</div>
  </div>

  <div class="main">
    <div class="cols">
      <div class="side">{side}</div>
      <div class="content">{body}</div>
    </div>
  </div>

  <div class="foot">
    <div class="right">Вы посетитель №<span class="counter">{datetime.now().strftime('%H%M%S')}</span><br>
      &copy; 2004&ndash;{year} СЛД. Все права защищены.</div>
    <div class="left">
      <b>СЛД</b> — старая добрая сеть.<br>
      Оптимально смотреть в <i>Internet Explorer 5.5</i> или <i>Netscape</i>, разрешение 1024&times;768.<br>
      При использовании материалов ссылка на сайт обязательна.
    </div>
    <div class="clear"></div>
    <div class="old-note">Сайт работает на SLDengine 0.9.3 (c) 2004—2008. Хостинг: sld.ru. Без JavaScript вы всё равно всё увидите.</div>
  </div>

</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Помощники рендера
# ---------------------------------------------------------------------------

def render_posts(posts: list, me: Optional[dict], comment_counts: dict,
                 empty_text: str = "Здесь пока ничего не написано.") -> str:
    if not posts:
        return f'<div class="box-body muted">{esc(empty_text)}</div>'

    out = []
    for p in posts:
        u = p.get("users") or {}
        if isinstance(u, list):
            u = u[0] if u else {}
        uname = u.get("username") or "удалён"
        pid = p.get("id")
        content = esc(p.get("content", ""))
        created = fmt_dt(p.get("created_at"))
        cnt = comment_counts.get(pid, 0)
        edited = ""
        if p.get("updated_at"):
            edited = f' · ред. {esc(fmt_dt(p["updated_at"]))}'

        actions = []
        if me and p.get("user_id") == me["id"]:
            actions.append(f'<a href="/posts/{pid}/edit">редактировать</a>')
            actions.append(
                f'<form method="post" action="/posts/{pid}/delete" '
                f'onsubmit="return confirm(\'Удалить запись?\')" style="display:inline">'
                f'<button type="submit">удалить</button></form>'
            )
        actions_html = (" · ".join(actions)) if actions else ""

        out.append(f"""<div class="post">
  {avatar_html(uname, u.get("avatar"), 50)}
  <div class="pbody">
    <div><span class="pname"><a href="/u/{esc(uname)}">{esc(uname)}</a></span>
      <span class="pdate">· {esc(created)}{edited}</span></div>
    <div class="ptext">{content}</div>
    <div class="pacts">
      <a href="/posts/{pid}">{cnt} {esc(plural(cnt, "комментарий", "комментария", "комментариев"))}</a>
      {(' · ' + actions_html) if actions_html else ''}
    </div>
  </div>
</div>""")
    return "".join(out)


def comment_counts_for(post_ids: list) -> dict:
    if not post_ids:
        return {}
    try:
        rows = (sb.table("comments").select("post_id")
                .in_("post_id", post_ids).execute().data) or []
    except Exception:
        return {}
    d: dict = {}
    for r in rows:
        d[r["post_id"]] = d.get(r["post_id"], 0) + 1
    return d


POST_SELECT = "id,user_id,content,created_at,updated_at,users(username,avatar)"


def fetch_posts(limit: int = 50, user_id: Optional[str] = None) -> list:
    q = sb.table("posts").select(POST_SELECT).order("created_at", desc=True).limit(limit)
    if user_id:
        q = q.eq("user_id", user_id)
    return q.execute().data or []


# ---------------------------------------------------------------------------
# Маршруты: главная
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str = "", err: str = ""):
    me = current_user(request)
    try:
        posts = fetch_posts()
    except Exception as e:
        posts = []
        err = err or f"Ошибка БД: {e}"

    counts = comment_counts_for([p["id"] for p in posts])

    alerts = ""
    if err:
        alerts += f'<div class="err">{esc(err)}</div>'
    if msg:
        alerts += f'<div class="ok">{esc(msg)}</div>'

    if me:
        compose = f"""<div class="box">
  <div class="box-title">Новая тема</div>
  <div class="box-body">
    <form method="post" action="/posts">
      <textarea name="content" rows="5" maxlength="{POST_MAX}"
        placeholder="Расскажите что-нибудь. Темы длиннее {POST_MAX} символов не принимаются."></textarea>
      <div style="margin-top:7px">
        <button type="submit">Отправить</button>
        <span class="hint" style="margin-left:10px">не более {POST_MAX} символов</span>
      </div>
    </form>
  </div>
</div>"""
    else:
        compose = ('<div class="box"><div class="box-body">'
                   'Чтобы открывать темы, нужно <a href="/login">войти</a> '
                   'или <a href="/register">зарегистрироваться</a>.'
                   '</div></div>')

    n = len(posts)
    body = f"""{alerts}
{compose}
<div class="box">
  <div class="box-title">Последние темы <span class="cnt">всего: {n}</span></div>
  {render_posts(posts, me, counts, empty_text="Тем пока нет. Будьте первым.")}
</div>"""
    return HTMLResponse(layout("Главная", me, body, active="feed"))


# ---------------------------------------------------------------------------
# Регистрация / вход / выход
# ---------------------------------------------------------------------------

def _auth_form(action: str, title: str, username: str = "") -> str:
    if action == "/register":
        return f"""
<form method="post" action="/register">
  <div class="field"><label>Имя пользователя</label>
    <input type="text" name="username" maxlength="20" value="{esc(username)}" autofocus></div>
  <div class="hint" style="margin:-4px 0 8px">3&ndash;20 символов: латиница, цифры, подчёркивание.</div>
  <div class="field"><label>Пароль</label>
    <input type="password" name="password"></div>
  <div class="field"><label>Пароль ещё раз</label>
    <input type="password" name="password2"></div>
  <button type="submit">Зарегистрироваться</button>
</form>"""
    return f"""
<form method="post" action="/login">
  <div class="field"><label>Имя пользователя</label>
    <input type="text" name="username" maxlength="20" value="{esc(username)}" autofocus></div>
  <div class="field"><label>Пароль</label>
    <input type="password" name="password"></div>
  <button type="submit">Войти</button>
</form>"""


def _auth_page(title: str, action: str, err: str = "", username: str = "") -> str:
    alert = f'<div class="err">{esc(err)}</div>' if err else ""
    inner = f'<div class="box"><div class="box-title">{esc(title)}</div>' \
            f'<div class="box-body">{alert}{_auth_form(action, title, username)}</div></div>'
    return layout(title, None, inner)


@app.get("/register", response_class=HTMLResponse)
def register_get(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_auth_page("Регистрация", "/register"))


@app.post("/register", response_class=HTMLResponse)
def register_post(request: Request,
                  username: str = Form(""),
                  password: str = Form(""),
                  password2: str = Form("")):
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
            err = f"Ошибка БД: {e}"

    if err:
        return HTMLResponse(_auth_page("Регистрация", "/register", err, username),
                            status_code=400)

    try:
        row = sb.table("users").insert({
            "username": username,
            "password_hash": hash_password(password),
        }).execute().data[0]
    except Exception as e:
        return HTMLResponse(_auth_page("Регистрация", "/register",
                                       f"Не удалось создать пользователя: {e}", username),
                            status_code=500)

    token = create_session(row["id"])
    resp = RedirectResponse("/", status_code=303)
    set_session_cookie(resp, token)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_auth_page("Вход", "/login"))


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request,
               username: str = Form(""),
               password: str = Form("")):
    if current_user(request):
        return RedirectResponse("/", status_code=303)

    username = username.strip()
    err = None
    row = None
    try:
        rows = (sb.table("users").select("*")
                .ilike("username", username).limit(1).execute().data)
        row = rows[0] if rows else None
    except Exception as e:
        err = f"Ошибка БД: {e}"

    if not err and (not row or not verify_password(password, row["password_hash"])):
        err = "Неверное имя пользователя или пароль."

    if err:
        return HTMLResponse(_auth_page("Вход", "/login", err, username),
                            status_code=400)

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


# ---------------------------------------------------------------------------
# Темы (посты)
# ---------------------------------------------------------------------------

@app.post("/posts")
def create_post(request: Request, content: str = Form("")):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    content = (content or "").strip()
    if not content:
        return RedirectResponse("/?err=Пустое+сообщение", status_code=303)
    if len(content) > POST_MAX:
        content = content[:POST_MAX]

    try:
        sb.table("posts").insert({
            "user_id": me["id"], "content": content,
        }).execute()
    except Exception as e:
        return RedirectResponse("/?err=Не+удалось+сохранить", status_code=303)

    return RedirectResponse("/?msg=Тема+опубликована", status_code=303)


@app.get("/posts/{post_id}", response_class=HTMLResponse)
def post_detail(request: Request, post_id: int, msg: str = "", err: str = ""):
    me = current_user(request)

    try:
        rows = (sb.table("posts").select(POST_SELECT)
                .eq("id", post_id).limit(1).execute().data)
    except Exception as e:
        return HTMLResponse(layout("Ошибка", me,
                                   f'<div class="box"><div class="box-body err">Ошибка БД: {esc(e)}</div></div>'),
                            status_code=500)
    if not rows:
        return HTMLResponse(layout("Не найдено", me,
                                   '<div class="box"><div class="box-body">Тема не найдена.</div></div>'),
                            status_code=404)

    p = rows[0]
    u = p.get("users") or {}
    if isinstance(u, list):
        u = u[0] if u else {}
    uname = u.get("username") or "удалён"

    try:
        comments = (sb.table("comments")
                    .select("id,content,created_at,user_id,users(username,avatar)")
                    .eq("post_id", post_id)
                    .order("created_at", desc=False).execute().data) or []
    except Exception:
        comments = []

    # Заголовок темы
    post_actions = ""
    if me and p.get("user_id") == me["id"]:
        post_actions = (
            f'<div style="margin-top:6px;font-size:12px">'
            f'<a href="/posts/{post_id}/edit">редактировать</a> · '
            f'<form method="post" action="/posts/{post_id}/delete" '
            f'onsubmit="return confirm(\'Удалить тему?\')" style="display:inline">'
            f'<button type="submit" style="background:none;border:none;padding:0;'
            f'color:#000099;text-decoration:underline;cursor:pointer;'
            f'font:12px \'Times New Roman\',serif">удалить</button></form></div>'
        )

    post_box = f"""<div class="box">
  <div class="box-title">Тема #{post_id}</div>
  <div class="post" style="border-bottom:none;padding:14px">
    {avatar_html(uname, u.get("avatar"), 70)}
    <div class="pbody">
      <div><span class="pname"><a href="/u/{esc(uname)}">{esc(uname)}</a></span>
        <span class="pdate">· {esc(fmt_dt(p.get("created_at")))}</span></div>
      <div class="ptext" style="margin-top:8px">{esc(p.get('content', ''))}</div>
      {post_actions}
    </div>
  </div>
</div>"""

    # Комментарии
    items = []
    for c in comments:
        cu = c.get("users") or {}
        if isinstance(cu, list):
            cu = cu[0] if cu else {}
        cname = cu.get("username") or "удалён"
        del_btn = ""
        if me and c.get("user_id") == me["id"]:
            del_btn = (
                f' · <form method="post" action="/comments/{c["id"]}/delete" '
                f'onsubmit="return confirm(\'Удалить комментарий?\')" style="display:inline">'
                f'<button type="submit" style="background:none;border:none;padding:0;'
                f'color:#000099;text-decoration:underline;cursor:pointer;'
                f'font:12px \'Times New Roman\',serif">удалить</button></form>'
            )
        items.append(f"""<div class="comment">
  {avatar_html(cname, cu.get("avatar"), 40)}
  <div class="pbody">
    <div><span class="pname"><a href="/u/{esc(cname)}">{esc(cname)}</a></span>
      <span class="pdate">· {esc(fmt_dt(c.get('created_at')))}</span></div>
    <div class="ptext">{esc(c.get('content', ''))}</div>
    <div class="pacts" style="border:none;padding-top:2px;margin-top:4px">
      <a href="#c{c['id']}" id="c{c['id']}">#{c['id']}</a>{del_btn}
    </div>
  </div>
</div>""")

    if items:
        comments_html = "".join(items)
    else:
        comments_html = '<div class="box-body muted">Комментариев пока нет.</div>'

    # Форма комментария
    if me:
        form_html = f"""<div class="box">
  <div class="box-title">Ваш комментарий</div>
  <div class="box-body">
    <form method="post" action="/posts/{post_id}/comments">
      <textarea name="content" rows="4" maxlength="{COMMENT_MAX}"
        placeholder="Написать комментарий..."></textarea>
      <div style="margin-top:7px">
        <button type="submit">Отправить</button>
        <span class="hint" style="margin-left:10px">не более {COMMENT_MAX} символов</span>
      </div>
    </form>
  </div>
</div>"""
    else:
        form_html = ('<div class="box"><div class="box-body">'
                     'Чтобы оставить комментарий, <a href="/login">войдите</a> '
                     'или <a href="/register">зарегистрируйтесь</a>.'
                     '</div></div>')

    alerts = ""
    if err:
        alerts += f'<div class="err">{esc(err)}</div>'
    if msg:
        alerts += f'<div class="ok">{esc(msg)}</div>'

    cnt = len(comments)
    body = f"""{alerts}
{post_box}
<div class="box">
  <div class="box-title">Комментарии <span class="cnt">{cnt}</span></div>
  {comments_html}
</div>
{form_html}
<div style="margin-top:8px"><a href="/">&larr; На главную</a></div>"""
    return HTMLResponse(layout(f"Тема #{post_id}", me, body))


@app.get("/posts/{post_id}/edit", response_class=HTMLResponse)
def post_edit_get(request: Request, post_id: int):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("posts").select("id,user_id,content")
            .eq("id", post_id).limit(1).execute().data)
    if not rows:
        return HTMLResponse(layout("Не найдено", me,
                                   '<div class="box"><div class="box-body">Тема не найдена.</div></div>'),
                            status_code=404)
    p = rows[0]
    if p["user_id"] != me["id"]:
        return HTMLResponse(layout("Отказано", me,
                                   '<div class="box"><div class="box-body">Это не ваша тема.</div></div>'),
                            status_code=403)

    body = f"""<div class="box">
  <div class="box-title">Редактирование темы #{post_id}</div>
  <div class="box-body">
    <form method="post" action="/posts/{post_id}/edit">
      <textarea name="content" rows="10" maxlength="{POST_MAX}">{esc(p['content'])}</textarea>
      <div style="margin-top:7px">
        <button type="submit">Сохранить</button>
        <a href="/posts/{post_id}" style="margin-left:12px">отмена</a>
      </div>
    </form>
  </div>
</div>"""
    return HTMLResponse(layout("Редактирование", me, body))


@app.post("/posts/{post_id}/edit")
def post_edit_post(request: Request, post_id: int, content: str = Form("")):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("posts").select("id,user_id")
            .eq("id", post_id).limit(1).execute().data)
    if not rows or rows[0]["user_id"] != me["id"]:
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

    return RedirectResponse(f"/posts/{post_id}?msg=Тема+обновлена", status_code=303)


@app.post("/posts/{post_id}/delete")
def post_delete(request: Request, post_id: int):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("posts").select("id,user_id")
            .eq("id", post_id).limit(1).execute().data)
    if rows and rows[0]["user_id"] == me["id"]:
        sb.table("posts").delete().eq("id", post_id).execute()

    return RedirectResponse("/?msg=Тема+удалена", status_code=303)


# ---------------------------------------------------------------------------
# Комментарии
# ---------------------------------------------------------------------------

@app.post("/posts/{post_id}/comments")
def comment_create(request: Request, post_id: int, content: str = Form("")):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    content = (content or "").strip()
    if not content:
        return RedirectResponse(f"/posts/{post_id}?err=Пустой+комментарий", status_code=303)
    if len(content) > COMMENT_MAX:
        content = content[:COMMENT_MAX]

    # убедимся, что тема есть
    exists = (sb.table("posts").select("id").eq("id", post_id)
              .limit(1).execute().data)
    if not exists:
        return RedirectResponse("/", status_code=303)

    sb.table("comments").insert({
        "post_id": post_id, "user_id": me["id"], "content": content,
    }).execute()

    return RedirectResponse(f"/posts/{post_id}?msg=Комментарий+добавлен", status_code=303)


@app.post("/comments/{comment_id}/delete")
def comment_delete(request: Request, comment_id: int):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("comments").select("id,user_id,post_id")
            .eq("id", comment_id).limit(1).execute().data)
    if not rows:
        return RedirectResponse("/", status_code=303)

    c = rows[0]
    if c["user_id"] == me["id"]:
        sb.table("comments").delete().eq("id", comment_id).execute()

    return RedirectResponse(f"/posts/{c['post_id']}?msg=Комментарий+удалён",
                            status_code=303)


# ---------------------------------------------------------------------------
# Профиль
# ---------------------------------------------------------------------------

def _profile_fields_html(u: dict) -> str:
    rows = [
        ("Имя", u.get("username")),
        ("Город", u.get("city")),
        ("День рождения", u.get("birthday")),
        ("Сайт", u.get("site")),
        ("На сайте с", fmt_dt(u.get("created_at"))),
    ]
    out = []
    for k, v in rows:
        if not v:
            continue
        if k == "Сайт":
            link = esc(v)
            if not link.startswith("http"):
                link = "http://" + link
            v_html = f'<a href="{link}" target="_blank" rel="noopener">{esc(v)}</a>'
        else:
            v_html = esc(v)
        out.append(f'<tr><td class="k">{esc(k)}:</td><td>{v_html}</td></tr>')
    return "".join(out) if out else '<tr><td colspan="2" class="muted">Информация не заполнена.</td></tr>'


@app.get("/u/{username}", response_class=HTMLResponse)
def profile(request: Request, username: str, msg: str = "", err: str = ""):
    me = current_user(request)

    rows = (sb.table("users").select("*")
            .ilike("username", username).limit(1).execute().data)
    if not rows:
        return HTMLResponse(layout("Не найдено", me,
                                   '<div class="box"><div class="box-body">Пользователь не найден.</div></div>'),
                            status_code=404)
    owner = rows[0]
    is_me = bool(me and me["id"] == owner["id"])

    try:
        posts = fetch_posts(user_id=owner["id"])
    except Exception as e:
        posts = []
        err = err or f"Ошибка БД: {e}"
    counts = comment_counts_for([p["id"] for p in posts])

    status_line = esc(owner.get("status") or "")
    if not status_line:
        status_line = '<span class="muted">Статус не указан.</span>'

    edit_link = ''
    if is_me:
        edit_link = ('<div style="margin-top:8px">'
                     '<a href="/settings">[ редактировать профиль ]</a></div>')

    about = owner.get("about")
    about_block = ""
    if about:
        about_block = (f'<div class="box"><div class="box-title">О себе</div>'
                       f'<div class="box-body" style="white-space:pre-wrap">{esc(about)}</div></div>')

    n = len(posts)
    cnt_word = plural(n, "запись", "записи", "записей")

    alerts = ""
    if err:
        alerts += f'<div class="err">{esc(err)}</div>'
    if msg:
        alerts += f'<div class="ok">{esc(msg)}</div>'

    body = f"""{alerts}
<div class="box">
  <div class="box-title">Профиль участника</div>
  <div class="profile-card">
    {avatar_html(owner["username"], owner.get("avatar"), 140)}
    <div class="info">
      <h1 class="ph">{esc(owner['username'])}</h1>
      <div class="status">&laquo;{status_line}&raquo;</div>
      <table class="info-table">
        <tr><td colspan="2"><h2 class="pht">Личные данные</h2></td></tr>
        {_profile_fields_html(owner)}
      </table>
      {edit_link}
    </div>
  </div>
</div>

{about_block}

<div class="box">
  <div class="box-title">Записи участника <span class="cnt">{n} {esc(cnt_word)}</span></div>
  {render_posts(posts, me, counts,
                empty_text=("Вы ещё ничего не написали." if is_me else "Записей нет."))}
</div>"""
    return HTMLResponse(layout(owner["username"], me, body, active="me" if is_me else ""))


# ---------------------------------------------------------------------------
# Настройки профиля
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request, msg: str = "", err: str = ""):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    alerts = ""
    if err:
        alerts += f'<div class="err">{esc(err)}</div>'
    if msg:
        alerts += f'<div class="ok">{esc(msg)}</div>'

    body = f"""{alerts}
<div class="box">
  <div class="box-title">Настройки профиля</div>
  <div class="box-body">
    <form method="post" action="/settings" enctype="multipart/form-data">

      <h2 class="pht">Фотография</h2>
      <div style="display:flex;gap:14px;align-items:flex-start;margin-bottom:10px">
        <div>{avatar_html(me["username"], me.get("avatar"), 120)}</div>
        <div class="muted" style="flex:1">
          Загрузите фотографию (JPEG, PNG или GIF, не больше 150 КБ).<br>
          <input type="file" name="avatar" accept="image/*" style="margin-top:6px">
          <div class="hint">Чтобы удалить текущую фотографию, поставьте галочку ниже
            и сохраните профиль.</div>
          <label style="display:block;margin-top:5px">
            <input type="checkbox" name="avatar_remove" value="1"> удалить фотографию
          </label>
        </div>
      </div>

      <h2 class="pht">Основное</h2>
      <div class="field"><label>Статус</label>
        <input type="text" name="status" maxlength="200" style="width:100%"
               value="{esc(me.get('status') or '')}"
               placeholder="Короткая фраза о себе"></div>
      <div class="field"><label>Город</label>
        <input type="text" name="city" maxlength="100"
               value="{esc(me.get('city') or '')}"></div>
      <div class="field"><label>День рождения</label>
        <input type="text" name="birthday" maxlength="20"
               value="{esc(me.get('birthday') or '')}"
               placeholder="например, 12 мая"></div>
      <div class="field"><label>Сайт</label>
        <input type="text" name="site" maxlength="200"
               value="{esc(me.get('site') or '')}"
               placeholder="http://..."></div>
      <div class="field"><label>О себе</label>
        <textarea name="about" rows="7" maxlength="2000"
          placeholder="Пара слов о себе. Не более 2000 символов.">{esc(me.get('about') or '')}</textarea></div>

      <div style="margin-top:10px">
        <button type="submit">Сохранить</button>
        <a href="/u/{esc(me['username'])}" style="margin-left:12px">отмена</a>
      </div>
    </form>
  </div>
</div>

<div class="box">
  <div class="box-title">Смена пароля</div>
  <div class="box-body">
    <form method="post" action="/settings/password">
      <div class="field"><label>Текущий пароль</label>
        <input type="password" name="old_password"></div>
      <div class="field"><label>Новый пароль</label>
        <input type="password" name="new_password"></div>
      <div class="field"><label>Повтор нового</label>
        <input type="password" name="new_password2"></div>
      <button type="submit">Сменить пароль</button>
    </form>
  </div>
</div>"""
    return HTMLResponse(layout("Настройки", me, body, active="settings"))


@app.post("/settings")
async def settings_post(
    request: Request,
    status: str = Form(""),
    city: str = Form(""),
    about: str = Form(""),
    site: str = Form(""),
    birthday: str = Form(""),
    avatar_remove: str = Form(""),
    avatar: UploadFile = File(None),
):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    update = {
        "status": (status.strip()[:200] or None),
        "city": (city.strip()[:100] or None),
        "about": (about.strip()[:2000] or None),
        "site": (site.strip()[:200] or None),
        "birthday": (birthday.strip()[:20] or None),
    }

    if avatar_remove:
        update["avatar"] = None
    elif avatar and avatar.filename:
        data = await avatar.read()
        if len(data) == 0:
            return RedirectResponse("/settings?err=Пустой+файл", status_code=303)
        if len(data) > AVATAR_MAX:
            return RedirectResponse(
                "/settings?err=Файл+больше+150+КБ", status_code=303)
        mime = (avatar.content_type or "").lower()
        if not mime.startswith("image/"):
            return RedirectResponse("/settings?err=Нужно+изображение", status_code=303)
        b64 = base64.b64encode(data).decode("ascii")
        update["avatar"] = f"data:{mime};base64,{b64}"

    try:
        sb.table("users").update(update).eq("id", me["id"]).execute()
    except Exception as e:
        return RedirectResponse("/settings?err=Не+удалось+сохранить", status_code=303)

    return RedirectResponse("/settings?msg=Профиль+сохранён", status_code=303)


@app.post("/settings/password")
def settings_password(request: Request,
                      old_password: str = Form(""),
                      new_password: str = Form(""),
                      new_password2: str = Form("")):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    if not verify_password(old_password, me["password_hash"]):
        return RedirectResponse("/settings?err=Неверный+текущий+пароль", status_code=303)
    if len(new_password) < 6:
        return RedirectResponse("/settings?err=Пароль+короче+6+символов", status_code=303)
    if new_password != new_password2:
        return RedirectResponse("/settings?err=Пароли+не+совпадают", status_code=303)

    sb.table("users").update({
        "password_hash": hash_password(new_password),
    }).eq("id", me["id"]).execute()

    # Разлогиним все сессии, кроме текущей
    token = request.cookies.get(SESSION_COOKIE)
    try:
        sb.table("sessions").delete().eq("user_id", me["id"]).neq("token", token).execute()
    except Exception:
        pass

    return RedirectResponse("/settings?msg=Пароль+изменён", status_code=303)


# ---------------------------------------------------------------------------
# Участники
# ---------------------------------------------------------------------------

@app.get("/people", response_class=HTMLResponse)
def people(request: Request):
    me = current_user(request)
    try:
        users = (sb.table("users")
                 .select("id,username,avatar,city,status,created_at")
                 .order("created_at", desc=True).limit(200).execute().data) or []
    except Exception as e:
        users = []
        db_err = str(e)
    else:
        db_err = ""

    items = []
    for u in users:
        un = esc(u["username"])
        city = esc(u.get("city") or "")
        st = esc(u.get("status") or "")
        meta = ""
        if city:
            meta += f'город: {city}'
        if st:
            meta += (" · " if meta else "") + f'&laquo;{st}&raquo;'
        items.append(f"""<div class="post">
  {avatar_html(u['username'], u.get('avatar'), 50)}
  <div class="pbody">
    <div><span class="pname"><a href="/u/{un}">{un}</a></span></div>
    <div class="pdate">{esc(fmt_dt(u.get('created_at')))}</div>
    <div class="ptext muted">{meta or '&nbsp;'}</div>
  </div>
</div>""")

    inner = "".join(items) if items else '<div class="box-body muted">Пока никого.</div>'
    alerts = f'<div class="err">{esc(db_err)}</div>' if db_err else ""
    body = f"""{alerts}
<div class="box">
  <div class="box-title">Участники <span class="cnt">{len(users)}</span></div>
  {inner}
</div>"""
    return HTMLResponse(layout("Участники", me, body, active="people"))


# ---------------------------------------------------------------------------
# Служебное
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
