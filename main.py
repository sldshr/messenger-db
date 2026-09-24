# -*- coding: utf-8 -*-
"""
СЛД — социальная сеть / форум в старом стиле.
Один файл. Регистрация, вход, профиль с фото, темы, комментарии.
БД — Supabase (service_role).

Запуск:
    export SUPABASE_URL=...
    export SUPABASE_KEY=...
    uvicorn main:app --reload --port 8000
"""

import os
import io
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
from PIL import Image


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
ABOUT_MAX = 2000

# Ограничения на аватар
AVATAR_UPLOAD_MAX = 10 * 1024 * 1024     # принимаем на вход до 10 МБ
AVATAR_TARGET_BYTES = 150 * 1024         # на выходе — не более 150 КБ
AVATAR_MAX_SIDE = 512                    # ресайз: длинная сторона ≤ 512 px

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
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}"


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
    r = 110 + (h & 0x4F)
    g = 120 + ((h >> 8) & 0x4F)
    b = 140 + ((h >> 16) & 0x3F)
    return f"#{r:02x}{g:02x}{b:02x}"


def avatar_html(username: str, avatar: Optional[str], size: int = 50) -> str:
    if avatar:
        return (f'<img class="av" src="{esc(avatar)}" '
                f'width="{size}" height="{size}" alt="">')
    letter = esc((username or "?")[0].upper())
    color = avatar_color(username or "?")
    return (f'<div class="av av-letter" style="background:{color};'
            f'width:{size}px;height:{size}px;line-height:{size}px;'
            f'font-size:{int(size * 0.45)}px">{letter}</div>')


def process_avatar(raw: bytes) -> Optional[str]:
    """
    Принимает байты изображения, возвращает data:URL JPEG.
    - ресайз: длинная сторона ≤ AVATAR_MAX_SIDE (512) с сохранением пропорций
    - подбором качества гарантируем размер ≤ AVATAR_TARGET_BYTES (150 КБ)
    - если никакое качество не помогает — дополнительно уменьшаем
    """
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None

    # Приводим к RGB (прозрачность ложим на белый фон)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    # Первый ресайз
    img.thumbnail((AVATAR_MAX_SIDE, AVATAR_MAX_SIDE), Image.LANCZOS)

    def encode(im: Image.Image, q: int) -> bytes:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=q, optimize=True, progressive=True)
        return buf.getvalue()

    # Пробуем уменьшать качество
    for q in (88, 82, 76, 70, 64, 58, 52, 46, 40, 34, 28):
        data = encode(img, q)
        if len(data) <= AVATAR_TARGET_BYTES:
            b64 = base64.b64encode(data).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"

    # Если всё равно много — пропорционально уменьшаем размер
    w, h = img.size
    for scale in (0.85, 0.7, 0.6, 0.5, 0.4, 0.3):
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        small = img.resize((nw, nh), Image.LANCZOS)
        data = encode(small, 70)
        if len(data) <= AVATAR_TARGET_BYTES:
            b64 = base64.b64encode(data).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"

    # Совсем крайний случай
    data = encode(img.resize((128, 128), Image.LANCZOS), 55)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


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
# Стиль: спокойный старый форум
# ---------------------------------------------------------------------------

CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font:12px/1.5 Verdana,Tahoma,Arial,sans-serif;
  color:#2A2A2A;
  background:#E4E4DE;
  padding:16px 0 40px;
}
a{color:#2B587A;text-decoration:none}
a:hover{text-decoration:underline}
img{border:0}

.wrap{width:880px;margin:0 auto}

/* Шапка */
.head{
  background:#4A6886;
  color:#fff;
  border:1px solid #354E66;
  border-bottom:none;
  padding:12px 16px;
  overflow:hidden;
}
.head .logo{
  font:bold 26px/1 Verdana,Tahoma,sans-serif;
  letter-spacing:4px;color:#fff;text-decoration:none;
  text-shadow:0 1px 1px rgba(0,0,0,.25);
  float:left;
}
.head .logo:hover{text-decoration:none;color:#F0F4F8}
.head .tagline{
  float:left;margin-left:14px;padding-top:8px;
  font-size:11px;color:#C8D6E2;font-style:italic;
}
.head .right{
  float:right;text-align:right;font-size:11px;color:#D4DEE8;padding-top:6px;
}
.head .right a{color:#FFFFFF}
.head .right b a{font-weight:bold}

/* Навигация */
.nav{
  background:#EFEDE5;
  border:1px solid #C7C3B4;
  border-top:none;
  padding:5px 12px;
  font-size:11px;
}
.nav a{color:#2B587A;padding:1px 2px}
.nav a.on{font-weight:bold;color:#111;text-decoration:underline}
.nav .sep{color:#A8A392;padding:0 6px}

/* Основной контейнер */
.main{background:#F7F5EF;border:1px solid #C7C3B4;border-top:none;padding:12px}
.cols{display:flex;gap:12px;align-items:flex-start}
.side{width:198px;flex:none}
.content{flex:1;min-width:0}

/* Коробки */
.box{background:#fff;border:1px solid #C7C3B4;margin-bottom:12px}
.box-title{
  background:#E5E1D3;
  border-bottom:1px solid #C7C3B4;
  padding:5px 10px;font-weight:bold;color:#2F3B48;font-size:11px;
}
.box-title .cnt{float:right;font-weight:normal;color:#7A7462;font-size:11px}
.box-body{padding:10px}

/* Боковое меню */
.smenu a{
  display:block;padding:5px 10px;border-bottom:1px solid #EFEDE5;
  color:#2B587A;text-decoration:none;
}
.smenu a:last-child{border-bottom:none}
.smenu a:hover{background:#F4F2EA;text-decoration:none}
.smenu a.on{background:#E5E1D3;font-weight:bold}

/* Аватары */
.av{display:block;border:1px solid #9A9684;background:#EEE}
.av-letter{
  color:#fff;text-align:center;font-weight:bold;
  text-shadow:1px 1px 1px rgba(0,0,0,.35);
  font-family:Verdana,sans-serif;
}

/* Карточка пользователя */
.ucard{display:flex;gap:8px;align-items:flex-start}
.ucard .meta{font-size:11px;color:#7A7462;margin-top:2px}

/* Записи */
.post{display:flex;gap:10px;padding:11px 10px;border-bottom:1px solid #EFEDE5}
.post:last-child{border-bottom:none}
.pbody{flex:1;min-width:0}
.pname{font-weight:bold}
.pdate{color:#8A8574;font-size:11px}
.ptext{margin-top:4px;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:break-word}
.pacts{margin-top:6px;font-size:11px;color:#8A8574;
       padding-top:5px;border-top:1px dashed #EFEDE5}
.pacts a{color:#5A7085}
.pacts button{
  background:none;border:none;padding:0;color:#5A7085;cursor:pointer;
  font:11px Verdana,sans-serif;text-decoration:none;
}
.pacts button:hover{text-decoration:underline;color:#2B587A}
.pacts form{display:inline;margin:0}

/* Формы */
textarea,input[type=text],input[type=password],input[type=file]{
  font:12px/1.5 Verdana,Tahoma,Arial,sans-serif;color:#2A2A2A;
  border:1px solid #B6B2A2;background:#fff;padding:4px 6px;
  border-radius:2px;
}
textarea:focus,input:focus{outline:none;border-color:#4A6886;background:#FCFCF9}
textarea{width:100%;resize:vertical}

button,.btn{
  font:12px Verdana,Tahoma,Arial,sans-serif;
  background:#E5E1D3;color:#2F3B48;
  border:1px solid #B6B2A2;border-radius:2px;
  padding:4px 14px;cursor:pointer;
}
button:hover,.btn:hover{background:#DAD6C6}
button:active,.btn:active{background:#CFCAB6}

.field{margin-bottom:9px}
.field label{display:block;color:#5A5646;margin-bottom:3px}
.field input[type=text],
.field input[type=password]{width:320px}

.err{background:#F7E3E3;border:1px solid #CE9C9C;color:#7A2A2A;
     padding:6px 10px;margin-bottom:10px;border-radius:2px}
.ok{background:#E6F0DC;border:1px solid #A7C493;color:#325A22;
    padding:6px 10px;margin-bottom:10px;border-radius:2px}

.muted{color:#8A8574;font-size:11px}
.hint{color:#8A8574;font-size:11px}
.center{text-align:center}

h1.ph{margin:0 0 4px;font:bold 20px/1.2 Verdana,sans-serif;color:#2F3B48}
h2.pht{
  margin:0 0 8px;font:bold 13px/1.2 Verdana,sans-serif;color:#2F3B48;
  border-bottom:1px solid #E5E1D3;padding-bottom:3px;
}

.profile-card{display:flex;gap:14px;padding:12px}
.profile-card .info{flex:1;min-width:0}
.status{font-style:italic;color:#4E4A3C;margin:3px 0 8px}
.info-table{width:100%;border-collapse:collapse}
.info-table td{padding:3px 6px 3px 0;vertical-align:top;
               border-bottom:1px dotted #E5E1D3}
.info-table td.k{color:#7A7462;width:150px;white-space:nowrap}

.comment{display:flex;gap:9px;padding:10px;border-bottom:1px solid #EFEDE5}
.comment:last-child{border-bottom:none}

.foot{
  background:#E5E1D3;border:1px solid #C7C3B4;border-top:none;
  padding:8px 12px;font-size:11px;color:#6E6A5A;
}
.foot a{color:#2B587A}
.clear{clear:both}
"""


def layout(title: str, user: Optional[dict], body: str, active: str = "") -> str:
    if user:
        un = esc(user["username"])
        head_right = (f'Вы вошли как <b><a href="/u/{un}">{un}</a></b>'
                      f' &nbsp;·&nbsp; <a href="/settings">настройки</a>'
                      f' &nbsp;·&nbsp; <a href="/logout">выход</a>')
    else:
        head_right = '<a href="/login">вход</a> &nbsp;·&nbsp; <a href="/register">регистрация</a>'

    nav = [("/", "Главная", "feed"), ("/people", "Участники", "people")]
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
                 f'<div class="meta">{esc(fmt_dt(user.get("created_at")))}</div></div>'
                 f'</div></div></div>')
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

    side += ('<div class="box"><div class="box-title">О сайте</div>'
             '<div class="box-body muted">'
             'СЛД — маленькая соцсеть с форумом.<br>'
             'Темы, комментарии, профили.<br>'
             'Без лишнего.'
             '</div></div>')

    year = datetime.now().year
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=880">
<title>{esc(title)} — СЛД</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

  <div class="head">
    <a class="logo" href="/">СЛД</a>
    <div class="tagline">соцсеть и форум</div>
    <div class="right">{head_right}</div>
    <div class="clear"></div>
  </div>
  <div class="nav">{nav_html}</div>

  <div class="main">
    <div class="cols">
      <div class="side">{side}</div>
      <div class="content">{body}</div>
    </div>
  </div>

  <div class="foot">
    <div class="clear">
      &copy; {year} СЛД. Все права защищены.
      &nbsp;·&nbsp; <a href="/">На главную</a>
    </div>
  </div>

</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Рендер постов
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
                f'onsubmit="return confirm(\'Удалить запись?\')">'
                f'<button type="submit">удалить</button></form>'
            )
        actions_html = " · ".join(actions)

        out.append(f"""<div class="post">
  {avatar_html(uname, u.get("avatar"), 50)}
  <div class="pbody">
    <div><span class="pname"><a href="/u/{esc(uname)}">{esc(uname)}</a></span>
      <span class="pdate">· {esc(created)}{edited}</span></div>
    <div class="ptext">{content}</div>
    <div class="pacts">
      <a href="/posts/{pid}">{cnt} {esc(plural(cnt, "комментарий", "комментария", "комментариев"))}</a>
      {(' &nbsp;·&nbsp; ' + actions_html) if actions_html else ''}
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
# Главная
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
        placeholder="Расскажите что-нибудь..."></textarea>
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

def _auth_form(action: str, username: str = "") -> str:
    if action == "/register":
        return f"""
<form method="post" action="/register">
  <div class="field"><label>Имя пользователя</label>
    <input type="text" name="username" maxlength="20" value="{esc(username)}" autofocus></div>
  <div class="hint" style="margin:-4px 0 9px">3&ndash;20 символов: латиница, цифры, подчёркивание.</div>
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
            f'<div class="box-body">{alert}{_auth_form(action, username)}</div></div>'
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
# Темы
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
        sb.table("posts").insert({"user_id": me["id"], "content": content}).execute()
    except Exception:
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

    post_actions = ""
    if me and p.get("user_id") == me["id"]:
        post_actions = (
            f'<div style="margin-top:6px;font-size:11px">'
            f'<a href="/posts/{post_id}/edit">редактировать</a> · '
            f'<form method="post" action="/posts/{post_id}/delete" '
            f'onsubmit="return confirm(\'Удалить тему?\')" style="display:inline">'
            f'<button type="submit">удалить</button></form></div>'
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
                f'<button type="submit">удалить</button></form>'
            )
        items.append(f"""<div class="comment">
  {avatar_html(cname, cu.get("avatar"), 40)}
  <div class="pbody">
    <div><span class="pname"><a href="/u/{esc(cname)}">{esc(cname)}</a></span>
      <span class="pdate">· {esc(fmt_dt(c.get('created_at')))}</span></div>
    <div class="ptext">{esc(c.get('content', ''))}</div>
    <div class="pacts" style="border:none;padding-top:2px;margin-top:3px">
      <a href="#c{c['id']}" id="c{c['id']}">#{c['id']}</a>{del_btn}
    </div>
  </div>
</div>""")

    comments_html = "".join(items) if items else '<div class="box-body muted">Комментариев пока нет.</div>'

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
    return "".join(out) if out else \
        '<tr><td colspan="2" class="muted">Информация не заполнена.</td></tr>'


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
          JPEG, PNG или GIF. Фото автоматически сжимается
          до 512&times;512 и веса не более 150 КБ.<br>
          <input type="file" name="avatar" accept="image/*" style="margin-top:6px">
          <label style="display:block;margin-top:6px">
            <input type="checkbox" name="avatar_remove" value="1">
            удалить текущую фотографию
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
        <textarea name="about" rows="7" maxlength="{ABOUT_MAX}"
          placeholder="Пара слов о себе...">{esc(me.get('about') or '')}</textarea></div>

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
        "status":   (status.strip()[:200] or None),
        "city":     (city.strip()[:100] or None),
        "about":    (about.strip()[:ABOUT_MAX] or None),
        "site":     (site.strip()[:200] or None),
        "birthday": (birthday.strip()[:20] or None),
    }

    if avatar_remove:
        update["avatar"] = None
    elif avatar and avatar.filename:
        raw = await avatar.read()
        if not raw:
            return RedirectResponse("/settings?err=Пустой+файл", status_code=303)
        if len(raw) > AVATAR_UPLOAD_MAX:
            return RedirectResponse("/settings?err=Файл+слишком+большой", status_code=303)

        data_url = process_avatar(raw)
        if not data_url:
            return RedirectResponse(
                "/settings?err=Не+удалось+обработать+изображение", status_code=303)
        update["avatar"] = data_url

    try:
        sb.table("users").update(update).eq("id", me["id"]).execute()
    except Exception:
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
        db_err = ""
    except Exception as e:
        users = []
        db_err = str(e)

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
