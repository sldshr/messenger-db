from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from html import escape
from datetime import datetime
from urllib.parse import quote
import secrets
import hashlib
import re
import uvicorn


# ============================================================
#  SLD FORUM
#  RAM ONLY / ONE FILE
#  FastAPI + Uvicorn
# ============================================================

app = FastAPI(title="SldForuM")

# Session data is also kept in signed cookies.
# No filesystem/database is used.
app.add_middleware(
    SessionMiddleware,
    secret_key=secrets.token_hex(32),
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
)


# ============================================================
# RAM STORAGE
# ============================================================

users = {}
categories = {}
topics = {}
posts = {}
messages = {}
notifications = {}

next_category_id = 1
next_topic_id = 1
next_post_id = 1
next_message_id = 1
next_notification_id = 1


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now().strftime("%d.%m.%Y %H:%M")


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt.encode(),
        120000
    ).hex()
    return salt + ":" + digest


def check_password(password, stored):
    try:
        salt, digest = stored.split(":", 1)
        new_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            salt.encode(),
            120000
        ).hex()
        return secrets.compare_digest(new_digest, digest)
    except Exception:
        return False


def current_user(request):
    username = request.session.get("user")
    if not username:
        return None
    return users.get(username)


def is_admin(user):
    return user and user["role"] == "admin"


def is_mod(user):
    return user and user["role"] in ("admin", "moderator")


def csrf_token(request):
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_hex(24)
        request.session["csrf"] = token
    return token


def valid_csrf(request, token):
    saved = request.session.get("csrf")
    return bool(saved and token and secrets.compare_digest(saved, token))


def add_notification(username, text):
    global next_notification_id

    notifications[next_notification_id] = {
        "id": next_notification_id,
        "user": username,
        "text": text,
        "date": now(),
        "read": False,
    }

    next_notification_id += 1


def add_post(topic_id, author, content):
    global next_post_id

    posts[next_post_id] = {
        "id": next_post_id,
        "topic": topic_id,
        "author": author,
        "content": content,
        "date": now(),
        "edited": False,
        "likes": set(),
    }

    topic = topics[topic_id]
    topic["posts"].append(next_post_id)

    next_post_id += 1


def render_bbcode(text):
    text = escape(text)

    text = re.sub(
        r"\[b\](.*?)\[/b\]",
        r"<strong>\1</strong>",
        text,
        flags=re.I | re.S,
    )

    text = re.sub(
        r"\[i\](.*?)\[/i\]",
        r"<em>\1</em>",
        text,
        flags=re.I | re.S,
    )

    text = re.sub(
        r"\[u\](.*?)\[/u\]",
        r"<u>\1</u>",
        text,
        flags=re.I | re.S,
    )

    text = re.sub(
        r"\[quote\](.*?)\[/quote\]",
        r'<div class="quote">\1</div>',
        text,
        flags=re.I | re.S,
    )

    text = re.sub(
        r"\[code\](.*?)\[/code\]",
        r'<pre class="code">\1</pre>',
        text,
        flags=re.I | re.S,
    )

    text = re.sub(
        r"\[url=(https?://[^\]]+)\](.*?)\[/url\]",
        r'<a href="\1" target="_blank" rel="noopener">\2</a>',
        text,
        flags=re.I | re.S,
    )

    text = text.replace("\n", "<br>")

    return text


def page(title, body, request):
    user = current_user(request)
    token = csrf_token(request)

    unread = 0

    if user:
        unread = sum(
            1
            for n in notifications.values()
            if n["user"] == user["username"] and not n["read"]
        )

    if user:
        account = f"""
        <span class="welcome">
            Привет, <a href="/user/{quote(user['username'])}">
            {escape(user['username'])}</a>
            |
            <a href="/notifications">Уведомления ({unread})</a>
            |
            <a href="/messages">ЛС</a>
            |
            <a href="/logout">Выход</a>
        </span>
        """
    else:
        account = """
        <span class="welcome">
            <a href="/login">Войти</a> |
            <a href="/register">Регистрация</a>
        </span>
        """

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} - SldForum</title>

<style>
* {{
    box-sizing:border-box;
}}

body {{
    margin:0;
    background:#d9d9d9;
    color:#222;
    font-family:Tahoma,Arial,sans-serif;
    font-size:13px;
}}

a {{
    color:#003c91;
    text-decoration:none;
}}

a:hover {{
    text-decoration:underline;
}}

.header {{
    background:linear-gradient(#4776aa,#1d4778);
    border-bottom:3px solid #16385e;
    color:white;
}}

.header-inner {{
    max-width:1100px;
    margin:auto;
    padding:12px 14px;
}}

.logo {{
    font-size:27px;
    font-weight:bold;
    text-shadow:1px 1px #111;
}}

.logo small {{
    font-size:11px;
    font-weight:normal;
}}

.topbar {{
    margin-top:10px;
    background:#eee;
    border:1px solid #aaa;
    border-bottom:0;
    color:#222;
    padding:7px 10px;
}}

.topbar .welcome {{
    float:right;
}}

.nav {{
    max-width:1100px;
    margin:auto;
    background:#eee;
    border-left:1px solid #aaa;
    border-right:1px solid #aaa;
    border-bottom:1px solid #999;
    padding:7px 10px;
}}

.nav a {{
    margin-right:18px;
    font-weight:bold;
}}

.container {{
    max-width:1100px;
    margin:12px auto;
}}

.panel {{
    background:#fff;
    border:1px solid #aaa;
    margin-bottom:12px;
    box-shadow:0 1px 1px #bbb;
}}

.panel-title {{
    padding:7px 10px;
    color:#fff;
    font-weight:bold;
    background:linear-gradient(#668eb8,#355f8d);
    border-bottom:1px solid #24476b;
}}

.panel-body {{
    padding:10px;
}}

.forum-row {{
    display:grid;
    grid-template-columns:58px 1fr 120px 100px;
    gap:10px;
    align-items:center;
    border-bottom:1px solid #ddd;
    padding:10px 6px;
}}

.forum-row:last-child {{
    border-bottom:0;
}}

.icon {{
    width:44px;
    height:44px;
    background:#eee;
    border:1px solid #aaa;
    text-align:center;
    padding-top:12px;
    font-size:18px;
}}

.forum-name {{
    font-weight:bold;
    font-size:15px;
}}

.desc {{
    color:#666;
    margin-top:3px;
}}

.stats {{
    text-align:center;
    color:#666;
}}

.topic-row {{
    border-bottom:1px solid #ddd;
    padding:9px;
}}

.topic-row:last-child {{
    border-bottom:0;
}}

.topic-title {{
    font-size:14px;
    font-weight:bold;
}}

.meta {{
    color:#777;
    font-size:11px;
    margin-top:4px;
}}

.post {{
    display:grid;
    grid-template-columns:155px 1fr;
    border-bottom:1px solid #aaa;
    min-height:150px;
}}

.post:last-child {{
    border-bottom:0;
}}

.userbox {{
    background:#eee;
    border-right:1px solid #bbb;
    padding:10px;
    text-align:center;
}}

.avatar {{
    width:70px;
    height:70px;
    margin:0 auto 7px;
    background:#355f8d;
    color:white;
    border:2px solid #fff;
    outline:1px solid #888;
    display:flex;
    align-items:center;
    justify-content:center;
    font-size:28px;
    font-weight:bold;
}}

.username {{
    font-weight:bold;
}}

.role {{
    font-size:10px;
    color:#666;
    margin-top:3px;
}}

.postbody {{
    padding:10px;
}}

.posthead {{
    border-bottom:1px dotted #aaa;
    padding-bottom:5px;
    margin-bottom:9px;
    color:#777;
    font-size:11px;
}}

.posttext {{
    line-height:1.55;
    min-height:90px;
}}

.posttools {{
    margin-top:10px;
    text-align:right;
    font-size:11px;
}}

.quote {{
    border-left:4px solid #777;
    background:#eee;
    padding:8px;
    margin:7px 0;
    color:#555;
}}

.code {{
    background:#111;
    color:#eee;
    padding:10px;
    overflow:auto;
    border:1px solid #444;
}}

input, textarea, select {{
    width:100%;
    padding:7px;
    border:1px solid #999;
    background:#fff;
    font-family:Tahoma,Arial,sans-serif;
    font-size:13px;
}}

textarea {{
    min-height:180px;
    resize:vertical;
}}

button, .button {{
    display:inline-block;
    padding:7px 14px;
    border:1px solid #555;
    border-radius:2px;
    background:linear-gradient(#fafafa,#d2d2d2);
    color:#111;
    cursor:pointer;
    font-weight:bold;
}}

button:hover, .button:hover {{
    background:linear-gradient(#fff,#bbb);
    text-decoration:none;
}}

.form-row {{
    margin-bottom:10px;
}}

.form-row label {{
    display:block;
    font-weight:bold;
    margin-bottom:4px;
}}

.error {{
    padding:8px;
    background:#ffe5e5;
    border:1px solid #cc8888;
    color:#900;
    margin-bottom:10px;
}}

.success {{
    padding:8px;
    background:#e5ffe5;
    border:1px solid #88bb88;
    color:#175f17;
    margin-bottom:10px;
}}

.breadcrumbs {{
    margin-bottom:10px;
    color:#666;
}}

.search {{
    display:flex;
    gap:5px;
}}

.search input {{
    flex:1;
}}

.footer {{
    max-width:1100px;
    margin:20px auto;
    text-align:center;
    color:#666;
    font-size:11px;
}}

.profile {{
    display:grid;
    grid-template-columns:160px 1fr;
}}

.profile-side {{
    background:#eee;
    padding:15px;
    text-align:center;
    border-right:1px solid #bbb;
}}

.statbox {{
    display:inline-block;
    min-width:110px;
    padding:10px;
    margin:4px;
    background:#eee;
    border:1px solid #bbb;
    text-align:center;
}}

@media(max-width:700px) {{
    .welcome {{
        float:none !important;
        display:block;
        margin-top:5px;
    }}

    .forum-row {{
        grid-template-columns:48px 1fr;
    }}

    .forum-row .stats {{
        display:none;
    }}

    .post {{
        grid-template-columns:1fr;
    }}

    .userbox {{
        border-right:0;
        border-bottom:1px solid #bbb;
        text-align:left;
    }}

    .avatar {{
        display:inline-flex;
        margin-right:8px;
        vertical-align:middle;
    }}

    .profile {{
        grid-template-columns:1fr;
    }}

    .profile-side {{
        border-right:0;
        border-bottom:1px solid #bbb;
    }}
}}
</style>
</head>

<body>

<div class="header">
    <div class="header-inner">
        <div class="logo">
            SldForum
            <small>classic community board</small>
        </div>
        <div class="topbar">
            Главная &nbsp;|&nbsp;
            Форум &nbsp;|&nbsp;
            Участники
            {account}
            <div style="clear:both"></div>
        </div>
    </div>
</div>

<div class="nav">
    <a href="/">Главная</a>
    <a href="/search">Поиск</a>
    <a href="/users">Участники</a>
    <a href="/stats">Статистика</a>
    {"<a href='/admin'>Админ-панель</a>" if is_admin(user) else ""}
</div>

<div class="container">
{body}
</div>

<div class="footer">
    SldForum &copy; 2026<br>
    Работа форума: только RAM. Данные удаляются после перезапуска сервера.
</div>

</body>
</html>
"""


def redirect(url):
    return RedirectResponse(url=url, status_code=303)


# ============================================================
# INITIAL DATA
# ============================================================

def init_data():
    global next_category_id

    data = [
        (
            "Общение",
            "Общие разговоры, оффтопик и знакомство.",
            "💬"
        ),
        (
            "Новости",
            "Новости проекта и объявления администрации.",
            "📢"
        ),
        (
            "Игры",
            "Обсуждение игр, серверов и игровых проектов.",
            "🎮"
        ),
        (
            "Техника",
            "Компьютеры, Linux, железо и программы.",
            "💻"
        ),
        (
            "Помощь",
            "Вопросы и помощь пользователям.",
            "❓"
        ),
    ]

    for name, desc, icon in data:
        categories[next_category_id] = {
            "id": next_category_id,
            "name": name,
            "description": desc,
            "icon": icon,
            "topics": [],
        }
        next_category_id += 1


init_data()


# ============================================================
# HOME
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):

    rows = ""

    for category in categories.values():

        topics_count = len(category["topics"])

        posts_count = sum(
            len(topics[t]["posts"])
            for t in category["topics"]
            if t in topics
        )

        latest = None

        for tid in category["topics"]:
            topic = topics.get(tid)
            if topic:
                if latest is None or topic["created_sort"] > latest["created_sort"]:
                    latest = topic

        if latest:
            latest_html = (
                f'<a href="/topic/{latest["id"]}">'
                f'{escape(latest["title"][:45])}</a>'
            )
        else:
            latest_html = "Нет сообщений"

        rows += f"""
        <div class="forum-row">
            <div class="icon">{category["icon"]}</div>

            <div>
                <div class="forum-name">
                    <a href="/category/{category["id"]}">
                        {escape(category["name"])}
                    </a>
                </div>
                <div class="desc">
                    {escape(category["description"])}
                </div>
            </div>

            <div class="stats">
                Тем: {topics_count}<br>
                Сообщений: {posts_count}
            </div>

            <div class="stats">
                {latest_html}
            </div>
        </div>
        """

    body = f"""
    <div class="panel">
        <div class="panel-title">Форумы</div>
        <div class="panel-body" style="padding:0">
            {rows}
        </div>
    </div>

    <div class="panel">
        <div class="panel-title">Быстрый поиск</div>
        <div class="panel-body">
            <form action="/search" method="get" class="search">
                <input name="q" placeholder="Введите поисковый запрос...">
                <button>Найти</button>
            </form>
        </div>
    </div>

    <div class="panel">
        <div class="panel-title">Статистика</div>
        <div class="panel-body">
            <span class="statbox">
                <b>{len(users)}</b><br>пользователей
            </span>

            <span class="statbox">
                <b>{len(topics)}</b><br>тем
            </span>

            <span class="statbox">
                <b>{len(posts)}</b><br>сообщений
            </span>
        </div>
    </div>
    """

    return HTMLResponse(page("Главная", body, request))


# ============================================================
# REGISTER
# ============================================================

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):

    if current_user(request):
        return redirect("/")

    body = f"""
    <div class="panel">
        <div class="panel-title">Регистрация</div>
        <div class="panel-body">

            <form method="post">
                <input type="hidden" name="csrf" value="{csrf_token(request)}">

                <div class="form-row">
                    <label>Имя пользователя</label>
                    <input name="username" minlength="3" maxlength="24" required>
                </div>

                <div class="form-row">
                    <label>Пароль</label>
                    <input type="password" name="password" minlength="6" required>
                </div>

                <div class="form-row">
                    <label>Повторите пароль</label>
                    <input type="password" name="password2" minlength="6" required>
                </div>

                <button>Зарегистрироваться</button>
            </form>

        </div>
    </div>
    """

    return HTMLResponse(page("Регистрация", body, request))


@app.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    csrf: str = Form(...)
):

    if not valid_csrf(request, csrf):
        return HTMLResponse("CSRF error", 403)

    username = username.strip()

    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9_ -]{3,24}", username):
        return HTMLResponse(
            page(
                "Ошибка",
                '<div class="panel"><div class="panel-body error">'
                'Некорректное имя пользователя.</div></div>',
                request
            )
        )

    if username.lower() in {u.lower() for u in users}:
        return HTMLResponse(
            page(
                "Ошибка",
                '<div class="panel"><div class="panel-body error">'
                'Такой пользователь уже существует.</div></div>',
                request
            )
        )

    if len(password) < 6:
        return HTMLResponse(
            page(
                "Ошибка",
                '<div class="panel"><div class="panel-body error">'
                'Пароль должен содержать минимум 6 символов.</div></div>',
                request
            )
        )

    if password != password2:
        return HTMLResponse(
            page(
                "Ошибка",
                '<div class="panel"><div class="panel-body error">'
                'Пароли не совпадают.</div></div>',
                request
            )
        )

    users[username] = {
        "username": username,
        "password": hash_password(password),
        "role": "user",
        "registered": now(),
        "posts": 0,
        "likes": 0,
    }

    request.session["user"] = username

    return redirect("/")


# ============================================================
# LOGIN
# ============================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):

    body = f"""
    <div class="panel">
        <div class="panel-title">Вход</div>
        <div class="panel-body">

            <form method="post">
                <input type="hidden" name="csrf" value="{csrf_token(request)}">

                <div class="form-row">
                    <label>Имя пользователя</label>
                    <input name="username" required>
                </div>

                <div class="form-row">
                    <label>Пароль</label>
                    <input type="password" name="password" required>
                </div>

                <button>Войти</button>
            </form>

        </div>
    </div>
    """

    return HTMLResponse(page("Вход", body, request))


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(...)
):

    if not valid_csrf(request, csrf):
        return HTMLResponse("CSRF error", 403)

    user = users.get(username)

    if not user or not check_password(password, user["password"]):
        body = """
        <div class="panel">
            <div class="panel-body error">
                Неверное имя пользователя или пароль.
            </div>
        </div>
        """

        return HTMLResponse(page("Ошибка", body, request), 401)

    request.session["user"] = username

    return redirect("/")


@app.get("/logout")
async def logout(request: Request):

    request.session.clear()

    return redirect("/")


# ============================================================
# CATEGORY
# ============================================================

@app.get("/category/{category_id}", response_class=HTMLResponse)
async def category_page(request: Request, category_id: int):

    category = categories.get(category_id)

    if not category:
        return HTMLResponse("Категория не найдена", 404)

    rows = ""

    for tid in reversed(category["topics"]):

        topic = topics.get(tid)

        if not topic:
            continue

        author = topic["author"]

        rows += f"""
        <div class="topic-row">

            <div class="topic-title">
                {"📌 " if topic["pinned"] else ""}
                {"🔒 " if topic["closed"] else ""}
                <a href="/topic/{topic["id"]}">
                    {escape(topic["title"])}
                </a>
            </div>

            <div class="meta">
                Автор:
                <a href="/user/{quote(author)}">{escape(author)}</a>
                |
                Ответов: {len(topic["posts"]) - 1}
                |
                Просмотров: {topic["views"]}
                |
                {topic["created"]}
            </div>

        </div>
        """

    user = current_user(request)

    create = ""

    if user:
        create = f"""
        <a class="button" href="/new-topic/{category_id}">
            Новая тема
        </a>
        """

    body = f"""
    <div class="breadcrumbs">
        <a href="/">Главная</a> →
        {escape(category["name"])}
    </div>

    <div class="panel">
        <div class="panel-title">
            {category["icon"]} {escape(category["name"])}
        </div>

        <div class="panel-body">
            {create}
        </div>

        <div style="padding:0 10px 10px">
            {rows if rows else '<div class="meta">Тем пока нет.</div>'}
        </div>
    </div>
    """

    return HTMLResponse(page(category["name"], body, request))


# ============================================================
# NEW TOPIC
# ============================================================

@app.get("/new-topic/{category_id}", response_class=HTMLResponse)
async def new_topic_page(request: Request, category_id: int):

    user = current_user(request)

    if not user:
        return redirect("/login")

    category = categories.get(category_id)

    if not category:
        return HTMLResponse("Категория не найдена", 404)

    body = f"""
    <div class="panel">
        <div class="panel-title">
            Новая тема в разделе "{escape(category["name"])}"
        </div>

        <div class="panel-body">

            <form method="post">
                <input type="hidden" name="csrf"
                       value="{csrf_token(request)}">

                <div class="form-row">
                    <label>Заголовок</label>
                    <input name="title" maxlength="120" required>
                </div>

                <div class="form-row">
                    <label>Сообщение</label>
                    <textarea name="content" required></textarea>
                </div>

                <div class="meta">
                    BBCode:
                    [b]жирный[/b]
                    [i]курсив[/i]
                    [u]подчёркнутый[/u]
                    [quote]цитата[/quote]
                    [code]код[/code]
                </div>

                <br>

                <button>Создать тему</button>
            </form>

        </div>
    </div>
    """

    return HTMLResponse(page("Новая тема", body, request))


@app.post("/new-topic/{category_id}")
async def new_topic(
    request: Request,
    category_id: int,
    title: str = Form(...),
    content: str = Form(...),
    csrf: str = Form(...)
):

    global next_topic_id

    user = current_user(request)

    if not user:
        return redirect("/login")

    if not valid_csrf(request, csrf):
        return HTMLResponse("CSRF error", 403)

    category = categories.get(category_id)

    if not category:
        return HTMLResponse("Категория не найдена", 404)

    title = title.strip()
    content = content.strip()

    if not title or not content:
        return HTMLResponse("Заполните все поля", 400)

    topics[next_topic_id] = {
        "id": next_topic_id,
        "category": category_id,
        "title": title,
        "author": user["username"],
        "created": now(),
        "created_sort": next_topic_id,
        "posts": [],
        "views": 0,
        "pinned": False,
        "closed": False,
    }

    category["topics"].append(next_topic_id)

    add_post(
        next_topic_id,
        user["username"],
        content
    )

    user["posts"] += 1

    tid = next_topic_id
    next_topic_id += 1

    return redirect(f"/topic/{tid}")


# ============================================================
# TOPIC
# ============================================================

@app.get("/topic/{topic_id}", response_class=HTMLResponse)
async def topic_page(request: Request, topic_id: int):

    topic = topics.get(topic_id)

    if not topic:
        return HTMLResponse("Тема не найдена", 404)

    topic["views"] += 1

    category = categories.get(topic["category"])

    posts_html = ""

    user = current_user(request)

    for pid in topic["posts"]:

        post = posts.get(pid)

        if not post:
            continue

        author = users.get(post["author"])

        if author:
            initial = escape(author["username"][0].upper())
            role = author["role"]
            post_count = author["posts"]
        else:
            initial = "?"
            role = "user"
            post_count = 0

        liked = user and user["username"] in post["likes"]

        if liked:
            like_link = f'<span>♥ {len(post["likes"])}</span>'
        else:
            like_link = (
                f'<a href="/like/{pid}">♥ {len(post["likes"])}</a>'
                if user else
                f'<span>♥ {len(post["likes"])}</span>'
            )

        tools = []

        if user and user["username"] == post["author"]:
            tools.append(
                f'<a href="/edit-post/{pid}">Изменить</a>'
            )

        if is_mod(user):
            tools.append(
                f'<a href="/delete-post/{pid}">Удалить</a>'
            )

        post_tools = " | ".join(tools)

        if post_tools:
            post_tools += " | "

        posts_html += f"""
        <div class="post">

            <div class="userbox">

                <div class="avatar">{initial}</div>

                <div class="username">
                    <a href="/user/{quote(post["author"])}">
                        {escape(post["author"])}
                    </a>
                </div>

                <div class="role">
                    {escape(role)}
                </div>

                <div class="meta">
                    сообщений: {post_count}
                </div>

            </div>

            <div class="postbody">

                <div class="posthead">
                    #{post["id"]} |
                    {post["date"]}
                    {" | изменено" if post["edited"] else ""}
                </div>

                <div class="posttext">
                    {render_bbcode(post["content"])}
                </div>

                <div class="posttools">
                    {post_tools}
                    {like_link}
                </div>

            </div>

        </div>
        """

    reply = ""

    if user and not topic["closed"]:
        reply = f"""
        <div class="panel">
            <div class="panel-title">Ответить</div>

            <div class="panel-body">

                <form method="post"
                      action="/reply/{topic_id}">

                    <input type="hidden" name="csrf"
                           value="{csrf_token(request)}">

                    <textarea name="content"
                              placeholder="Ваш ответ..."
                              required></textarea>

                    <br><br>

                    <button>Отправить</button>
                </form>

            </div>
        </div>
        """

    elif topic["closed"]:
        reply = """
        <div class="panel">
            <div class="panel-body">
                <b>Тема закрыта.</b>
            </div>
        </div>
        """

    actions = ""

    if is_mod(user):
        actions = f"""
        <a class="button"
           href="/toggle-pin/{topic_id}">
           {"Открепить" if topic["pinned"] else "Закрепить"}
        </a>

        <a class="button"
           href="/toggle-close/{topic_id}">
           {"Открыть" if topic["closed"] else "Закрыть"}
        </a>
        """

    body = f"""
    <div class="breadcrumbs">
        <a href="/">Главная</a> →
        <a href="/category/{category["id"]}">
            {escape(category["name"])}
        </a> →
        {escape(topic["title"])}
    </div>

    <div class="panel">

        <div class="panel-title">
            {"📌 " if topic["pinned"] else ""}
            {"🔒 " if topic["closed"] else ""}
            {escape(topic["title"])}
        </div>

        <div class="panel-body">
            {actions}
        </div>

        {posts_html}

    </div>

    {reply}
    """

    return HTMLResponse(page(topic["title"], body, request))


# ============================================================
# REPLY
# ============================================================

@app.post("/reply/{topic_id}")
async def reply(
    request: Request,
    topic_id: int,
    content: str = Form(...),
    csrf: str = Form(...)
):

    user = current_user(request)

    if not user:
        return redirect("/login")

    if not valid_csrf(request, csrf):
        return HTMLResponse("CSRF error", 403)

    topic = topics.get(topic_id)

    if not topic:
        return HTMLResponse("Тема не найдена", 404)

    if topic["closed"]:
        return HTMLResponse("Тема закрыта", 403)

    content = content.strip()

    if not content:
        return redirect(f"/topic/{topic_id}")

    add_post(
        topic_id,
        user["username"],
        content
    )

    user["posts"] += 1

    if topic["author"] != user["username"]:
        add_notification(
            topic["author"],
            f'Пользователь {user["username"]} ответил в теме "{topic["title"]}"'
        )

    return redirect(f"/topic/{topic_id}")


# ============================================================
# LIKE
# ============================================================

@app.get("/like/{post_id}")
async def like(request: Request, post_id: int):

    user = current_user(request)

    if not user:
        return redirect("/login")

    post = posts.get(post_id)

    if not post:
        return HTMLResponse("Сообщение не найдено", 404)

    username = user["username"]

    if username in post["likes"]:
        post["likes"].remove(username)
    else:
        post["likes"].add(username)

        if post["author"] != username:
            add_notification(
                post["author"],
                f'{username} поставил лайк вашему сообщению.'
            )

    return redirect(request.headers.get("referer", "/"))


# ============================================================
# EDIT POST
# ============================================================

@app.get("/edit-post/{post_id}", response_class=HTMLResponse)
async def edit_post_page(request: Request, post_id: int):

    user = current_user(request)
    post = posts.get(post_id)

    if not user or not post:
        return HTMLResponse("Нет доступа", 403)

    if post["author"] != user["username"] and not is_mod(user):
        return HTMLResponse("Нет доступа", 403)

    body = f"""
    <div class="panel">
        <div class="panel-title">Редактирование сообщения</div>

        <div class="panel-body">

            <form method="post">

                <input type="hidden"
                       name="csrf"
                       value="{csrf_token(request)}">

                <textarea name="content"
                          required>{escape(post["content"])}</textarea>

                <br><br>

                <button>Сохранить</button>

            </form>

        </div>
    </div>
    """

    return HTMLResponse(page("Редактирование", body, request))


@app.post("/edit-post/{post_id}")
async def edit_post(
    request: Request,
    post_id: int,
    content: str = Form(...),
    csrf: str = Form(...)
):

    user = current_user(request)
    post = posts.get(post_id)

    if not user or not post:
        return HTMLResponse("Нет доступа", 403)

    if not valid_csrf(request, csrf):
        return HTMLResponse("CSRF error", 403)

    if post["author"] != user["username"] and not is_mod(user):
        return HTMLResponse("Нет доступа", 403)

    post["content"] = content.strip()
    post["edited"] = True

    return redirect(
        f'/topic/{post["topic"]}'
    )


# ============================================================
# DELETE POST
# ============================================================

@app.get("/delete-post/{post_id}")
async def delete_post(request: Request, post_id: int):

    user = current_user(request)
    post = posts.get(post_id)

    if not user or not post:
        return HTMLResponse("Нет доступа", 403)

    if not is_mod(user):
        return HTMLResponse("Нет доступа", 403)

    topic = topics.get(post["topic"])

    if topic and post_id in topic["posts"]:
        topic["posts"].remove(post_id)

    del posts[post_id]

    return redirect(
        f'/topic/{topic["id"]}'
        if topic else "/"
    )


# ============================================================
# PIN / CLOSE
# ============================================================

@app.get("/toggle-pin/{topic_id}")
async def toggle_pin(request: Request, topic_id: int):

    user = current_user(request)

    if not is_mod(user):
        return HTMLResponse("Нет доступа", 403)

    topic = topics.get(topic_id)

    if not topic:
        return HTMLResponse("Тема не найдена", 404)

    topic["pinned"] = not topic["pinned"]

    return redirect(f"/topic/{topic_id}")


@app.get("/toggle-close/{topic_id}")
async def toggle_close(request: Request, topic_id: int):

    user = current_user(request)

    if not is_mod(user):
        return HTMLResponse("Нет доступа", 403)

    topic = topics.get(topic_id)

    if not topic:
        return HTMLResponse("Тема не найдена", 404)

    topic["closed"] = not topic["closed"]

    return redirect(f"/topic/{topic_id}")


# ============================================================
# USERS
# ============================================================

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):

    rows = ""

    for user in users.values():

        rows += f"""
        <div class="topic-row">

            <a class="topic-title"
               href="/user/{quote(user["username"])}">
                {escape(user["username"])}
            </a>

            <div class="meta">
                Роль: {escape(user["role"])}
                |
                Сообщений: {user["posts"]}
                |
                Регистрация: {user["registered"]}
            </div>

        </div>
        """

    body = f"""
    <div class="panel">
        <div class="panel-title">Участники</div>

        <div class="panel-body">
            {rows if rows else "Пока никто не зарегистрирован."}
        </div>
    </div>
    """

    return HTMLResponse(page("Участники", body, request))


# ============================================================
# PROFILE
# ============================================================

@app.get("/user/{username}", response_class=HTMLResponse)
async def profile(request: Request, username: str):

    user = users.get(username)

    if not user:
        return HTMLResponse("Пользователь не найден", 404)

    current = current_user(request)

    pm = ""

    if current and current["username"] != username:
        pm = (
            f'<a class="button" '
            f'href="/message/{quote(username)}">'
            f'Написать сообщение</a>'
        )

    user_topics = [
        t for t in topics.values()
        if t["author"] == username
    ]

    body = f"""
    <div class="panel">

        <div class="panel-title">
            Профиль пользователя
        </div>

        <div class="profile">

            <div class="profile-side">

                <div class="avatar">
                    {escape(username[0].upper())}
                </div>

                <h3>{escape(username)}</h3>

                <div>{escape(user["role"])}</div>

            </div>

            <div class="panel-body">

                <p>
                    <b>Регистрация:</b>
                    {user["registered"]}
                </p>

                <p>
                    <b>Сообщений:</b>
                    {user["posts"]}
                </p>

                <p>
                    <b>Получено лайков:</b>
                    {user["likes"]}
                </p>

                {pm}

                <hr>

                <b>Созданные темы: {len(user_topics)}</b>

            </div>

        </div>

    </div>
    """

    return HTMLResponse(page(username, body, request))


# ============================================================
# SEARCH
# ============================================================

@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):

    q = q.strip()

    results = ""

    if q:

        q_lower = q.lower()

        for topic in topics.values():

            found = (
                q_lower in topic["title"].lower()
                or q_lower in topic["author"].lower()
            )

            if not found:
                for pid in topic["posts"]:
                    post = posts.get(pid)

                    if post and q_lower in post["content"].lower():
                        found = True
                        break

            if found:
                results += f"""
                <div class="topic-row">

                    <div class="topic-title">
                        <a href="/topic/{topic["id"]}">
                            {escape(topic["title"])}
                        </a>
                    </div>

                    <div class="meta">
                        Автор: {escape(topic["author"])}
                        |
                        Ответов: {len(topic["posts"]) - 1}
                    </div>

                </div>
                """

    body = f"""
    <div class="panel">

        <div class="panel-title">Поиск</div>

        <div class="panel-body">

            <form class="search" method="get">
                <input name="q"
                       value="{escape(q)}"
                       placeholder="Что ищем?">
                <button>Найти</button>
            </form>

        </div>

        {f'<div style="padding:10px">{results or "Ничего не найдено."}</div>' if q else ""}

    </div>
    """

    return HTMLResponse(page("Поиск", body, request))


# ============================================================
# PRIVATE MESSAGES
# ============================================================

@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):

    user = current_user(request)

    if not user:
        return redirect("/login")

    incoming = [
        m for m in messages.values()
        if m["to"] == user["username"]
    ]

    outgoing = [
        m for m in messages.values()
        if m["from"] == user["username"]
    ]

    rows = ""

    for m in reversed(incoming):

        rows += f"""
        <div class="topic-row">
            <b>От:</b>
            <a href="/user/{quote(m["from"])}">
                {escape(m["from"])}
            </a>

            <div class="meta">{m["date"]}</div>

            <div style="margin-top:7px">
                {render_bbcode(m["content"])}
            </div>
        </div>
        """

    body = f"""
    <div class="panel">

        <div class="panel-title">Личные сообщения</div>

        <div class="panel-body">

            <b>Входящие:</b> {len(incoming)}
            |
            <b>Отправленные:</b> {len(outgoing)}

            <hr>

            {rows if rows else "Сообщений нет."}

        </div>

    </div>
    """

    return HTMLResponse(page("Личные сообщения", body, request))


@app.get("/message/{username}", response_class=HTMLResponse)
async def message_page(request: Request, username: str):

    user = current_user(request)

    if not user:
        return redirect("/login")

    if username not in users:
        return HTMLResponse("Пользователь не найден", 404)

    body = f"""
    <div class="panel">

        <div class="panel-title">
            Сообщение для {escape(username)}
        </div>

        <div class="panel-body">

            <form method="post">

                <input type="hidden"
                       name="csrf"
                       value="{csrf_token(request)}">

                <div class="form-row">
                    <label>Сообщение</label>
                    <textarea name="content" required></textarea>
                </div>

                <button>Отправить</button>

            </form>

        </div>

    </div>
    """

    return HTMLResponse(page("Личное сообщение", body, request))


@app.post("/message/{username}")
async def send_message(
    request: Request,
    username: str,
    content: str = Form(...),
    csrf: str = Form(...)
):

    global next_message_id

    user = current_user(request)

    if not user:
        return redirect("/login")

    if not valid_csrf(request, csrf):
        return HTMLResponse("CSRF error", 403)

    if username not in users:
        return HTMLResponse("Пользователь не найден", 404)

    messages[next_message_id] = {
        "id": next_message_id,
        "from": user["username"],
        "to": username,
        "content": content.strip(),
        "date": now(),
    }

    add_notification(
        username,
        f'Новое личное сообщение от {user["username"]}.'
    )

    next_message_id += 1

    return redirect("/messages")


# ============================================================
# NOTIFICATIONS
# ============================================================

@app.get("/notifications", response_class=HTMLResponse)
async def notification_page(request: Request):

    user = current_user(request)

    if not user:
        return redirect("/login")

    user_notifications = [
        n for n in notifications.values()
        if n["user"] == user["username"]
    ]

    for n in user_notifications:
        n["read"] = True

    rows = ""

    for n in reversed(user_notifications):

        rows += f"""
        <div class="topic-row">
            {escape(n["text"])}
            <div class="meta">{n["date"]}</div>
        </div>
        """

    body = f"""
    <div class="panel">
        <div class="panel-title">Уведомления</div>
        <div class="panel-body">
            {rows if rows else "Уведомлений нет."}
        </div>
    </div>
    """

    return HTMLResponse(page("Уведомления", body, request))


# ============================================================
# STATISTICS
# ============================================================

@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):

    total_likes = sum(len(p["likes"]) for p in posts.values())

    body = f"""
    <div class="panel">

        <div class="panel-title">
            Статистика форума
        </div>

        <div class="panel-body">

            <span class="statbox">
                <b>{len(users)}</b><br>
                пользователей
            </span>

            <span class="statbox">
                <b>{len(categories)}</b><br>
                разделов
            </span>

            <span class="statbox">
                <b>{len(topics)}</b><br>
                тем
            </span>

            <span class="statbox">
                <b>{len(posts)}</b><br>
                сообщений
            </span>

            <span class="statbox">
                <b>{total_likes}</b><br>
                лайков
            </span>

        </div>

    </div>
    """

    return HTMLResponse(page("Статистика", body, request))


# ============================================================
# ADMIN
# ============================================================

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):

    user = current_user(request)

    if not is_admin(user):
        return HTMLResponse("Нет доступа", 403)

    rows = ""

    for u in users.values():

        if u["username"] == user["username"]:
            action = ""
        else:
            action = (
                f'<a href="/admin/toggle-role/'
                f'{quote(u["username"])}">'
                f'{"Сделать пользователем" if u["role"] == "moderator" else "Сделать модератором"}'
                f'</a>'
            )

        rows += f"""
        <div class="topic-row">
            <b>{escape(u["username"])}</b>
            |
            роль: {escape(u["role"])}
            |
            {action}
        </div>
        """

    body = f"""
    <div class="panel">

        <div class="panel-title">
            Администрация
        </div>

        <div class="panel-body">

            <b>Пользователи</b>

            <hr>

            {rows}

        </div>

    </div>
    """

    return HTMLResponse(page("Админ-панель", body, request))


@app.get("/admin/toggle-role/{username}")
async def toggle_role(request: Request, username: str):

    user = current_user(request)

    if not is_admin(user):
        return HTMLResponse("Нет доступа", 403)

    target = users.get(username)

    if not target:
        return HTMLResponse("Пользователь не найден", 404)

    if target["role"] == "user":
        target["role"] = "moderator"
    elif target["role"] == "moderator":
        target["role"] = "user"

    return redirect("/admin")


# ============================================================
# CREATE ADMIN COMMAND
# ============================================================

@app.get("/setup-admin/{username}")
async def setup_admin(request: Request, username: str):

    # Convenience endpoint for local/private installations.
    # Remove this route after creating the administrator.
    if username not in users:
        return HTMLResponse("Пользователь не найден", 404)

    users[username]["role"] = "admin"

    return HTMLResponse(
        f"Пользователь {escape(username)} теперь admin. "
        f"Удалите endpoint /setup-admin из production."
    )


# ============================================================
# ERROR HANDLER
# ============================================================

@app.exception_handler(404)
async def not_found(request: Request, exc):

    body = """
    <div class="panel">
        <div class="panel-title">404</div>
        <div class="panel-body">
            Страница не найдена.
            <br><br>
            <a class="button" href="/">На главную</a>
        </div>
    </div>
    """

    return HTMLResponse(
        page("404", body, request),
        status_code=404
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
