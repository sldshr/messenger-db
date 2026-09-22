# -*- coding: utf-8 -*-
"""
ВКонтактик 2008 — мини-соцсеть на FastAPI.
Всё хранится в оперативной памяти (словари), никаких файлов и БД.

Запуск:
    pip install fastapi uvicorn python-multipart
    python main.py
Открыть: http://127.0.0.1:8000
"""

import html
import hashlib
import secrets
import time
from datetime import datetime
from typing import Dict, Optional, Set

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="ВКонтактик 2008")

# =========================================================================
#  ХРАНИЛИЩЕ (всё в оперативке)
# =========================================================================

DB: Dict = {
    "users": {},        # id -> пользователь
    "logins": {},       # login(lower) -> id
    "sessions": {},     # sid -> id
    "posts": {},        # id -> пост
    "friends": {},      # id -> set(id) друзей
    "requests": {},     # id -> set(id) входящих заявок в друзья
    "next_user_id": 1,
    "next_post_id": 1,
}


def friends_of(uid: int) -> Set[int]:
    return DB["friends"].setdefault(uid, set())


def requests_of(uid: int) -> Set[int]:
    return DB["requests"].setdefault(uid, set())


def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def create_user(login, password, name, surname, city="", sex="m", about=""):
    uid = DB["next_user_id"]
    DB["next_user_id"] += 1
    salt = secrets.token_hex(8)
    user = {
        "id": uid,
        "login": login,
        "salt": salt,
        "pw": hash_pw(password, salt),
        "name": name,
        "surname": surname,
        "city": city,
        "sex": sex,
        "about": about,
        "created": time.time(),
    }
    DB["users"][uid] = user
    DB["logins"][login.lower()] = uid
    friends_of(uid)
    requests_of(uid)
    return user


def add_post(author_id: int, owner_id: int, text: str) -> dict:
    pid = DB["next_post_id"]
    DB["next_post_id"] += 1
    post = {
        "id": pid,
        "author_id": author_id,
        "owner_id": owner_id,
        "text": text,
        "created": time.time(),
        "likes": set(),
        "comments": [],
    }
    DB["posts"][pid] = post
    return post


def current_user(request: Request) -> Optional[dict]:
    sid = request.cookies.get("sid")
    if not sid:
        return None
    uid = DB["sessions"].get(sid)
    return DB["users"].get(uid) if uid else None


# =========================================================================
#  ВСПОМОГАТЕЛЬНОЕ
# =========================================================================

esc = html.escape

MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн",
          "июл", "авг", "сен", "окт", "ноя", "дек"]

AVA_COLORS = ["#45688e", "#5b7fa6", "#6d8ba7", "#7a6d9e",
              "#8a6a4a", "#4a7c59", "#8a4a5c", "#3f6b8a"]


def fmt_time(ts: float) -> str:
    now = datetime.now()
    d = datetime.fromtimestamp(ts)
    if d.date() == now.date():
        return "сегодня в " + d.strftime("%H:%M")
    if (now.date() - d.date()).days == 1:
        return "вчера в " + d.strftime("%H:%M")
    return "{0} {1} {2}".format(d.day, MONTHS[d.month - 1], d.year)


def avatar(u: dict, size: int = 50) -> str:
    color = AVA_COLORS[u["id"] % len(AVA_COLORS)]
    ini = esc((u["name"][:1] + u["surname"][:1]).upper())
    fs = max(12, size // 2)
    return (
        '<div class="ava" style="width:{w}px;height:{w}px;background:{c};'
        'line-height:{w}px;font-size:{f}px">{i}</div>'
    ).format(w=size, c=color, f=fs, i=ini)


def form_button(action: str, label: str, cls: str = "btn") -> str:
    return (
        '<form method="post" action="{a}" class="inline">'
        '<button class="{c}">{l}</button></form>'
    ).format(a=action, c=cls, l=label)


def back_url(request: Request, default: str = "/") -> str:
    ref = request.headers.get("referer")
    if ref and ref.startswith("http"):
        # оставляем только путь
        try:
            return "/" + ref.split("//", 1)[1].split("/", 1)[1]
        except Exception:
            return default
    return ref or default


def go(url: str, sid: Optional[str] = None) -> RedirectResponse:
    r = RedirectResponse(url, status_code=303)
    if sid:
        r.set_cookie("sid", sid, httponly=True, max_age=60 * 60 * 24 * 30)
    return r


# =========================================================================
#  ВЁРСТКА
# =========================================================================

CSS = """
* { box-sizing: border-box; }
html, body { margin:0; padding:0; }
body {
  background:#e8ebf0;
  font: 11px/1.45 Verdana, Tahoma, Arial, sans-serif;
  color:#2f2f2f;
}
a { color:#2b587a; text-decoration:none; }
a:hover { text-decoration:underline; }
.clear { clear:both; }
.cnt { color:#999; }
.inline { display:inline; }

/* ---------- шапка ---------- */
#header { background:#45688e; border-bottom:1px solid #2d4a6b; height:40px; }
#header .wrap { width:820px; margin:0 auto; height:40px; }
.logo {
  float:left; color:#fff; line-height:40px;
  font:bold 20px Tahoma, Verdana, sans-serif; letter-spacing:-0.5px;
}
.logo:hover { text-decoration:none; }
#header .search { float:left; margin:9px 0 0 30px; }
#header .search input[type=text] {
  width:180px; padding:3px 5px; border:1px solid #2d4a6b;
  font:11px Verdana; background:#fff; color:#000;
}
#header .search button {
  padding:3px 8px; border:1px solid #2d4a6b; background:#5c86b4;
  color:#fff; font:11px Verdana; cursor:pointer;
}
#header .search button:hover { background:#6d95c0; }
.huser { float:right; color:#dfe7f0; line-height:40px; }
.huser a { color:#fff; }
.huser a.logout { color:#c3d3e6; margin-left:8px; }

/* ---------- каркас ---------- */
.wrap { width:820px; margin:0 auto; }
#main { margin-top:12px; margin-bottom:40px; }
#left { float:left; width:160px; }
#content { margin-left:172px; }

.lmenu { background:#fff; border:1px solid #c9d3de; }
.lmenu a {
  display:block; padding:5px 8px; border-bottom:1px solid #eef1f5;
}
.lmenu a:last-child { border-bottom:none; }
.lmenu a:hover { background:#f0f4f9; text-decoration:none; }

/* ---------- блоки ---------- */
.box { background:#fff; border:1px solid #c9d3de; margin-bottom:12px; }
.box h2 {
  margin:0; padding:8px 12px; font:bold 12px Tahoma, Verdana, sans-serif;
  background:#f0f2f5; border-bottom:1px solid #dde3ea; color:#45688e;
}
.box .body { padding:12px; }

/* ---------- формы ---------- */
input[type=text], input[type=password], textarea, select {
  font:11px Verdana; border:1px solid #c0c9d3; padding:4px;
  width:100%; color:#000; background:#fff;
}
textarea { resize:vertical; }
.btn {
  display:inline-block; padding:4px 12px; background:#5c86b4; color:#fff;
  border:1px solid #45688e; cursor:pointer; font:11px Verdana;
}
.btn:hover { background:#45688e; text-decoration:none; }
.btn-sm { padding:3px 8px; }
.linkbtn {
  background:none; border:none; color:#2b587a; cursor:pointer;
  font:11px Verdana; padding:0;
}
.linkbtn:hover { text-decoration:underline; }
.lbl { display:block; margin:8px 0 3px; color:#666; }
.err {
  background:#ffe9e9; border:1px solid #e0a0a0; color:#a00;
  padding:6px 8px; margin-bottom:10px;
}

/* ---------- аватар ---------- */
.ava {
  display:block; color:#fff; text-align:center; font-family:Tahoma;
  font-weight:bold; border-radius:2px; overflow:hidden;
}

/* ---------- профиль ---------- */
.profile { background:#fff; border:1px solid #c9d3de; margin-bottom:12px; }
.ptop { padding:15px; overflow:hidden; }
.pava { float:left; margin-right:15px; }
.profile h1 { font:bold 16px Tahoma, Verdana, sans-serif; margin:0 0 10px; color:#2b587a; }
.pinfo div { padding:2px 0; }
.pinfo b { color:#777; font-weight:normal; }
.pbtns { margin-top:12px; }

/* ---------- записи ---------- */
.post { border-bottom:1px solid #e5e9ee; padding:12px; overflow:hidden; }
.post:last-child { border-bottom:none; }
.pava2 { float:left; }
.pbody { margin-left:62px; }
.pauthor { font-weight:bold; }
.ptime { color:#999; }
.ptext {
  margin:6px 0; white-space:pre-wrap; word-wrap:break-word;
  font-size:12px; line-height:1.5;
}
.pact { color:#999; padding:2px 0; }
.comments { margin-top:4px; }
.comment { border-top:1px solid #eef1f5; padding:6px 0 2px; }
.cname { font-weight:bold; }
.cform { margin-top:8px; }
.cform input { width:70%; display:inline-block; }
.empty { padding:12px; color:#999; }

/* ---------- авторизация ---------- */
.authwrap { width:400px; margin:50px auto; }
.authwrap .lbl { margin-top:10px; }
.radio { margin:4px 12px 4px 0; }
"""

PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div id="header">
  <div class="wrap">
    <a class="logo" href="/">ВКонтактик</a>
    <form class="search" method="get" action="/people">
      <input type="text" name="q" value="{q}" placeholder="Поиск людей">
      <button type="submit">Найти</button>
    </form>
    {userbox}
  </div>
</div>
<div id="main" class="wrap">
  {left}
  <div id="content">{content}</div>
  <div class="clear"></div>
</div>
</body>
</html>"""

AUTH_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div id="header"><div class="wrap"><a class="logo" href="/">ВКонтактик</a></div></div>
<div class="authwrap">
  <div class="box">
    <h2>{title}</h2>
    <div class="body">{content}</div>
  </div>
</div>
</body>
</html>"""


def layout(title: str, content: str, me: Optional[dict], q: str = "") -> str:
    if me:
        userbox = (
            '<div class="huser"><a href="/u/{uid}">{n} {s}</a>'
            '<a class="logout" href="/logout">выход</a></div>'
        ).format(uid=me["id"], n=esc(me["name"]), s=esc(me["surname"]))

        left = (
            '<div id="left"><div class="lmenu">'
            '<a href="/u/{uid}">Моя страница</a>'
            '<a href="/feed">Новости</a>'
            '<a href="/friends">Друзья <span class="cnt">{fc}</span></a>'
            '<a href="/people">Поиск людей</a>'
            '<a href="/settings">Настройки</a>'
            '</div></div>'
        ).format(uid=me["id"], fc=len(friends_of(me["id"])))
    else:
        userbox = ('<div class="huser"><a href="/login">Вход</a> | '
                   '<a href="/register">Регистрация</a></div>')
        left = ""

    return PAGE.format(title=esc(title), css=CSS, q=esc(q),
                       userbox=userbox, left=left, content=content)


def auth_layout(title: str, content: str) -> str:
    return AUTH_PAGE.format(title=esc(title), css=CSS, content=content)


def render_comment(c: dict, me: dict) -> str:
    a = DB["users"].get(c["author_id"])
    name = (a["name"] + " " + a["surname"]) if a else "Удалённый"
    return (
        '<div class="comment">'
        '<a class="cname" href="/u/{uid}">{n}</a> '
        '<span class="ptime">{t}</span>'
        '<div>{txt}</div></div>'
    ).format(uid=c["author_id"], n=esc(name),
             t=fmt_time(c["created"]), txt=esc(c["text"]))


def render_post(p: dict, me: dict) -> str:
    a = DB["users"].get(p["author_id"])
    if not a:
        return ""
    pid = p["id"]
    liked = me["id"] in p["likes"]
    like_txt = "Больше не нравится" if liked else "Мне нравится"
    can_del = (me["id"] == p["author_id"]) or (me["id"] == p["owner_id"])
    del_form = ""
    if can_del:
        del_form = (
            '<form method="post" action="/post/{p}/delete" class="inline">'
            '<button class="linkbtn">удалить</button></form>'
        ).format(p=pid)

    comments = "".join(render_comment(c, me) for c in p["comments"])

    return (
        '<div class="post">'
        '<div class="pava2">{ava}</div>'
        '<div class="pbody">'
        '<a class="pauthor" href="/u/{aid}">{an}</a> '
        '<span class="ptime">{t}</span>'
        '<div class="ptext">{txt}</div>'
        '<div class="pact">'
        '<form method="post" action="/post/{p}/like" class="inline">'
        '<button class="linkbtn">{lt}</button></form>'
        ' <span class="cnt">({lc})</span>'
        ' &nbsp;·&nbsp; {df}'
        '</div>'
        '<div class="comments">{cs}</div>'
        '<form method="post" action="/post/{p}/comment" class="cform">'
        '<input type="text" name="text" placeholder="Комментарий..." maxlength="500">'
        '<button class="btn btn-sm">Отправить</button>'
        '</form>'
        '</div>'
        '<div class="clear"></div>'
        '</div>'
    ).format(ava=avatar(a, 50), aid=a["id"],
             an=esc(a["name"] + " " + a["surname"]),
             t=fmt_time(p["created"]), txt=esc(p["text"]), p=pid,
             lt=like_txt, lc=len(p["likes"]), df=del_form, cs=comments)


def render_posts(posts, me: dict) -> str:
    if not posts:
        return '<div class="empty">Записей пока нет.</div>'
    return "".join(render_post(p, me) for p in posts)


def wall_form(owner_id: int) -> str:
    return (
        '<form method="post" action="/post">'
        '<input type="hidden" name="owner_id" value="{uid}">'
        '<textarea name="text" rows="3" maxlength="1000" '
        'placeholder="Что у вас нового?"></textarea>'
        '<div style="margin-top:6px"><button class="btn">Отправить</button></div>'
        '</form>'
    ).format(uid=owner_id)


# =========================================================================
#  ПРОФИЛЬ / СТЕНА
# =========================================================================

def profile_content(u: dict, me: dict) -> str:
    uid = u["id"]

    posts = [p for p in DB["posts"].values() if p["owner_id"] == uid]
    posts.sort(key=lambda p: p["created"], reverse=True)

    # кнопки действий
    btns = []
    if uid != me["id"]:
        if uid in friends_of(me["id"]):
            btns.append(form_button("/friends/remove/" + str(uid), "Удалить из друзей"))
        elif uid in requests_of(me["id"]):
            btns.append(form_button("/friends/accept/" + str(uid), "Принять заявку"))
        elif me["id"] in requests_of(uid):
            btns.append('<span class="cnt">Заявка отправлена</span>')
        else:
            btns.append(form_button("/friends/add/" + str(uid), "Добавить в друзья"))

    info = [
        ("Город:", u["city"] or "—"),
        ("Пол:", "мужской" if u["sex"] == "m" else "женский"),
        ("Друзей:", str(len(friends_of(uid)))),
        ("На сайте с:", datetime.fromtimestamp(u["created"]).strftime("%d.%m.%Y")),
    ]
    info_html = "".join(
        '<div><b>{k}</b> {v}</div>'.format(k=esc(k), v=esc(v)) for k, v in info
    )

    post_form = ('<div class="body">' + wall_form(uid) + '</div>') if True else ""

    about_box = ""
    if u["about"]:
        about_box = (
            '<div class="box"><h2>О себе</h2>'
            '<div class="body">{t}</div></div>'
        ).format(t=esc(u["about"]))

    return (
        '<div class="profile"><div class="ptop">'
        '<div class="pava">{ava}</div>'
        '<h1>{name}</h1>'
        '<div class="pinfo">{info}</div>'
        '<div class="pbtns">{btns}</div>'
        '</div></div>'
        '{about}'
        '<div class="box"><h2>Стена</h2>'
        '{pf}'
        '{wall}'
        '</div>'
    ).format(ava=avatar(u, 150),
             name=esc(u["name"] + " " + u["surname"]),
             info=info_html,
             btns=" ".join(btns),
             about=about_box,
             pf=post_form,
             wall=render_posts(posts, me))


# =========================================================================
#  МАРШРУТЫ: ГЛАВНАЯ / СТЕНА
# =========================================================================

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/u/{0}".format(me["id"]), status_code=303)


@app.get("/u/{uid}", response_class=HTMLResponse)
def profile(uid: int, request: Request):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    u = DB["users"].get(uid)
    if not u:
        content = ('<div class="box"><div class="body">'
                   'Пользователь не найден. <a href="/people">Все люди</a>'
                   '</div></div>')
        return HTMLResponse(layout("Не найдено", content, me), status_code=404)

    title = u["name"] + " " + u["surname"]
    return HTMLResponse(layout(title, profile_content(u, me), me))


@app.get("/feed", response_class=HTMLResponse)
def feed(request: Request):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    my_friends = friends_of(me["id"])
    allowed_authors = set(my_friends) | {me["id"]}

    posts = [p for p in DB["posts"].values() if p["author_id"] in allowed_authors]
    posts.sort(key=lambda p: p["created"], reverse=True)
    posts = posts[:50]

    content = (
        '<div class="box"><h2>Новости</h2>{posts}</div>'
    ).format(posts=render_posts(posts, me))

    return HTMLResponse(layout("Новости", content, me))


# =========================================================================
#  МАРШРУТЫ: ПОСТЫ
# =========================================================================

@app.post("/post")
def post_create(request: Request, text: str = Form(""), owner_id: int = Form(0)):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    text = text.strip()
    if text:
        if owner_id not in DB["users"]:
            owner_id = me["id"]
        add_post(me["id"], owner_id, text[:1000])

    return go(back_url(request, "/u/{0}".format(owner_id or me["id"])))


@app.post("/post/{pid}/like")
def post_like(pid: int, request: Request):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    p = DB["posts"].get(pid)
    if p:
        if me["id"] in p["likes"]:
            p["likes"].discard(me["id"])
        else:
            p["likes"].add(me["id"])
    return go(back_url(request))


@app.post("/post/{pid}/comment")
def post_comment(pid: int, request: Request, text: str = Form("")):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    p = DB["posts"].get(pid)
    text = text.strip()
    if p and text:
        p["comments"].append({
            "author_id": me["id"],
            "text": text[:500],
            "created": time.time(),
        })
    return go(back_url(request))


@app.post("/post/{pid}/delete")
def post_delete(pid: int, request: Request):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    p = DB["posts"].get(pid)
    if p and (me["id"] == p["author_id"] or me["id"] == p["owner_id"]):
        DB["posts"].pop(pid, None)
    return go(back_url(request))


# =========================================================================
#  МАРШРУТЫ: ДРУЗЬЯ
# =========================================================================

@app.get("/friends", response_class=HTMLResponse)
def friends_page(request: Request):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    my_friends = sorted(friends_of(me["id"]))
    incoming = sorted(requests_of(me["id"]))
    outgoing = sorted(u["id"] for u in DB["users"].values()
                      if me["id"] in requests_of(u["id"]))

    def user_row(uid, actions=""):
        u = DB["users"].get(uid)
        if not u:
            return ""
        return (
            '<div class="post" style="padding:8px 12px">'
            '<div class="pava2" style="margin-right:10px">{ava}</div>'
            '<div class="pbody" style="margin-left:52px;padding-top:6px">'
            '<a class="pauthor" href="/u/{uid}">{n} {s}</a>'
            '<div class="cnt">{city}</div>'
            '{act}'
            '</div><div class="clear"></div></div>'
        ).format(ava=avatar(u, 40), uid=uid, n=esc(u["name"]), s=esc(u["surname"]),
                 city=esc(u["city"] or ""), act=actions)

    fr_rows = "".join(
        user_row(uid, form_button("/friends/remove/" + str(uid),
                                  "Удалить из друзей", "linkbtn"))
        for uid in my_friends
    ) or '<div class="empty">У вас пока нет друзей. <a href="/people">Найти людей</a></div>'

    inc_rows = "".join(
        user_row(uid, form_button("/friends/accept/" + str(uid), "Принять заявку"))
        for uid in incoming
    ) or '<div class="empty">Новых заявок нет.</div>'

    out_rows = "".join(
        user_row(uid, '<span class="cnt">Заявка отправлена</span>')
        for uid in outgoing
    ) or '<div class="empty">Исходящих заявок нет.</div>'

    content = (
        '<div class="box"><h2>Мои друзья ({fc})</h2>{fr}</div>'
        '<div class="box"><h2>Заявки в друзья ({ic})</h2>{inc}</div>'
        '<div class="box"><h2>Я отправил заявки ({oc})</h2>{out}</div>'
    ).format(fc=len(my_friends), fr=fr_rows,
             ic=len(incoming), inc=inc_rows,
             oc=len(outgoing), out=out_rows)

    return HTMLResponse(layout("Друзья", content, me))


@app.post("/friends/add/{uid}")
def friend_add(uid: int, request: Request):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    if uid in DB["users"] and uid != me["id"] and uid not in friends_of(me["id"]):
        requests_of(uid).add(me["id"])
    return go(back_url(request))


@app.post("/friends/accept/{uid}")
def friend_accept(uid: int, request: Request):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    if uid in DB["users"] and me["id"] in requests_of(uid):
        requests_of(uid).discard(me["id"])
        friends_of(me["id"]).add(uid)
        friends_of(uid).add(me["id"])
    return go(back_url(request))


@app.post("/friends/remove/{uid}")
def friend_remove(uid: int, request: Request):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    friends_of(me["id"]).discard(uid)
    friends_of(uid).discard(me["id"])
    return go(back_url(request))


# =========================================================================
#  МАРШРУТЫ: ЛЮДИ
# =========================================================================

@app.get("/people", response_class=HTMLResponse)
def people(request: Request, q: str = ""):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    q = q.strip()
    users = sorted(DB["users"].values(), key=lambda u: u["id"])
    if q:
        ql = q.lower()
        users = [u for u in users
                 if ql in u["name"].lower()
                 or ql in u["surname"].lower()
                 or ql in u["login"].lower()]

    rows = []
    for u in users:
        act = ""
        if u["id"] != me["id"]:
            if u["id"] in friends_of(me["id"]):
                act = '<span class="cnt">у вас в друзьях</span>'
            elif u["id"] in requests_of(me["id"]):
                act = form_button("/friends/accept/" + str(u["id"]), "Принять заявку")
            elif me["id"] in requests_of(u["id"]):
                act = '<span class="cnt">заявка отправлена</span>'
            else:
                act = form_button("/friends/add/" + str(u["id"]), "Добавить в друзья")
        else:
            act = '<span class="cnt">это вы</span>'

        rows.append(
            '<div class="post" style="padding:10px 12px">'
            '<div class="pava2" style="margin-right:10px">{ava}</div>'
            '<div class="pbody" style="margin-left:62px">'
            '<a class="pauthor" href="/u/{uid}">{n} {s}</a>'
            '<div class="cnt">{city}</div>'
            '<div style="margin-top:6px">{act}</div>'
            '</div><div class="clear"></div></div>'
        ).format(ava=avatar(u, 50), uid=u["id"], n=esc(u["name"]),
                 s=esc(u["surname"]), city=esc(u["city"] or ""), act=act))

    body = "".join(rows) or '<div class="empty">Никого не найдено.</div>'
    title = "Поиск людей" + (": " + q if q else "")
    content = '<div class="box"><h2>{t}</h2>{b}</div>'.format(t=esc(title), b=body)

    return HTMLResponse(layout(title, content, me, q=q))


# =========================================================================
#  МАРШРУТЫ: НАСТРОЙКИ
# =========================================================================

@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request, ok: int = 0):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    msg = '<div class="err" style="background:#e8f5e9;border-color:#a5d6a7;color:#2e7d32">Изменения сохранены.</div>' if ok else ""

    form = (
        '{msg}'
        '<form method="post" action="/settings">'
        '<span class="lbl">Имя</span>'
        '<input type="text" name="name" value="{n}" maxlength="40" required>'
        '<span class="lbl">Фамилия</span>'
        '<input type="text" name="surname" value="{s}" maxlength="40" required>'
        '<span class="lbl">Город</span>'
        '<input type="text" name="city" value="{c}" maxlength="60">'
        '<span class="lbl">Пол</span>'
        '<label class="radio"><input type="radio" name="sex" value="m" {cm}> мужской</label>'
        '<label class="radio"><input type="radio" name="sex" value="f" {cf}> женский</label>'
        '<span class="lbl">О себе</span>'
        '<textarea name="about" rows="4" maxlength="500">{a}</textarea>'
        '<div style="margin-top:10px"><button class="btn">Сохранить</button></div>'
        '</form>'
    ).format(msg=msg, n=esc(me["name"]), s=esc(me["surname"]),
             c=esc(me["city"]), a=esc(me["about"]),
             cm="checked" if me["sex"] == "m" else "",
             cf="checked" if me["sex"] == "f" else "")

    content = (
        '<div class="box"><h2>Настройки</h2><div class="body">{f}</div></div>'
        '<div class="box"><h2>Аккаунт</h2><div class="body">'
        'Логин: <b>{login}</b><br>'
        'Зарегистрирован: {reg}'
        '</div></div>'
    ).format(f=form, login=esc(me["login"]),
             reg=datetime.fromtimestamp(me["created"]).strftime("%d.%m.%Y %H:%M"))

    return HTMLResponse(layout("Настройки", content, me))


@app.post("/settings")
def settings_post(
    request: Request,
    name: str = Form(""),
    surname: str = Form(""),
    city: str = Form(""),
    sex: str = Form("m"),
    about: str = Form(""),
):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    if name.strip():
        me["name"] = name.strip()[:40]
    if surname.strip():
        me["surname"] = surname.strip()[:40]
    me["city"] = city.strip()[:60]
    me["sex"] = "f" if sex == "f" else "m"
    me["about"] = about.strip()[:500]

    return RedirectResponse("/settings?ok=1", status_code=303)


# =========================================================================
#  МАРШРУТЫ: РЕГИСТРАЦИЯ / ВХОД / ВЫХОД
# =========================================================================

def register_form(error: str = "", vals: Optional[dict] = None) -> str:
    v = vals or {}
    err = '<div class="err">{0}</div>'.format(esc(error)) if error else ""
    return (
        '{err}'
        '<form method="post" action="/register">'
        '<span class="lbl">Логин (латиница/цифры, 3-20)</span>'
        '<input type="text" name="login" value="{login}" maxlength="20" required>'
        '<span class="lbl">Пароль (мин. 4 символа)</span>'
        '<input type="password" name="password" maxlength="60" required>'
        '<span class="lbl">Имя</span>'
        '<input type="text" name="name" value="{name}" maxlength="40" required>'
        '<span class="lbl">Фамилия</span>'
        '<input type="text" name="surname" value="{surname}" maxlength="40" required>'
        '<span class="lbl">Город</span>'
        '<input type="text" name="city" value="{city}" maxlength="60">'
        '<span class="lbl">Пол</span>'
        '<label class="radio"><input type="radio" name="sex" value="m" checked> мужской</label>'
        '<label class="radio"><input type="radio" name="sex" value="f"> женский</label>'
        '<div style="margin-top:14px"><button class="btn">Зарегистрироваться</button></div>'
        '<div style="margin-top:10px">Уже есть аккаунт? <a href="/login">Войти</a></div>'
        '</form>'
    ).format(err=err, login=esc(v.get("login", "")), name=esc(v.get("name", "")),
             surname=esc(v.get("surname", "")), city=esc(v.get("city", "")))


@app.get("/register", response_class=HTMLResponse)
def register_get():
    return HTMLResponse(auth_layout("Регистрация", register_form()))


@app.post("/register", response_class=HTMLResponse)
def register_post(
    request: Request,
    login: str = Form(""),
    password: str = Form(""),
    name: str = Form(""),
    surname: str = Form(""),
    city: str = Form(""),
    sex: str = Form("m"),
    about: str = Form(""),
):
    login = login.strip()
    name = name.strip()
    surname = surname.strip()

    vals = {"login": login, "name": name, "surname": surname, "city": city.strip()}

    error = ""
    if not (3 <= len(login) <= 20) or not login.replace("_", "").isalnum():
        error = "Логин: 3-20 символов, только буквы, цифры и подчёркивание."
    elif login.lower() in DB["logins"]:
        error = "Такой логин уже занят."
    elif len(password) < 4:
        error = "Пароль должен быть не короче 4 символов."
    elif not name or not surname:
        error = "Имя и фамилия обязательны."

    if error:
        return HTMLResponse(auth_layout("Регистрация", register_form(error, vals)),
                            status_code=400)

    user = create_user(login, password, name, surname,
                       city.strip()[:60], "f" if sex == "f" else "m",
                       about.strip()[:500])

    sid = secrets.token_urlsafe(24)
    DB["sessions"][sid] = user["id"]
    return go("/u/{0}".format(user["id"]), sid=sid)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, err: str = ""):
    if current_user(request):
        return RedirectResponse("/", status_code=303)

    err_html = '<div class="err">{0}</div>'.format(esc(err)) if err else ""
    content = (
        '{err}'
        '<form method="post" action="/login">'
        '<span class="lbl">Логин</span>'
        '<input type="text" name="login" maxlength="20" required>'
        '<span class="lbl">Пароль</span>'
        '<input type="password" name="password" maxlength="60" required>'
        '<div style="margin-top:14px"><button class="btn">Войти</button></div>'
        '<div style="margin-top:10px">Нет аккаунта? <a href="/register">Регистрация</a></div>'
        '</form>'
    ).format(err=err_html)
    return HTMLResponse(auth_layout("Вход", content))


@app.post("/login")
def login_post(
    request: Request,
    login: str = Form(""),
    password: str = Form(""),
):
    uid = DB["logins"].get(login.strip().lower())
    u = DB["users"].get(uid) if uid else None

    if not u or hash_pw(password, u["salt"]) != u["pw"]:
        return RedirectResponse("/login?err=Неверный+логин+или+пароль", status_code=303)

    sid = secrets.token_urlsafe(24)
    DB["sessions"][sid] = u["id"]
    return go("/u/{0}".format(u["id"]), sid=sid)


@app.get("/logout")
def logout(request: Request):
    sid = request.cookies.get("sid")
    if sid:
        DB["sessions"].pop(sid, None)
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("sid")
    return r


# =========================================================================
#  ДЕМО-ДАННЫЕ
# =========================================================================

def seed():
    if DB["users"]:
        return
    vasya = create_user("vasya", "1234", "Василий", "Пупкин",
                        "Москва", "m", "Люблю футбол, семечки и подъездную акустику.")
    masha = create_user("masha", "1234", "Мария", "Иванова",
                        "Санкт-Петербург", "f", "Кошки, гитара и дождливые вечера.")
    friends_of(vasya["id"]).add(masha["id"])
    friends_of(masha["id"]).add(vasya["id"])

    p1 = add_post(vasya["id"], vasya["id"], "Всем привет! Я тут новенький :)")
    p2 = add_post(masha["id"], masha["id"], "Кто идёт завтра на концерт в клуб?")
    p3 = add_post(vasya["id"], masha["id"], "Маша, с днём рождения! 🎉")

    p2["likes"].add(vasya["id"])
    p2["likes"].add(masha["id"])
    p1["likes"].add(masha["id"])

    p1["comments"].append({"author_id": masha["id"],
                           "text": "Добро пожаловать!",
                           "created": time.time()})
    p2["comments"].append({"author_id": vasya["id"],
                           "text": "Я иду, беру два билета",
                           "created": time.time()})


seed()

# =========================================================================
#  ТОЧКА ВХОДА
# =========================================================================

if __name__ == "__main__":
    print("=" * 56)
    print("  ВКонтактик 2008 запущен: http://127.0.0.1:8000")
    print("  Демо-аккаунты:  vasya / 1234   и   masha / 1234")
    print("=" * 56)
    uvicorn.run(app, host="127.0.0.1", port=8000)
