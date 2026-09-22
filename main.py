# main.py — sldchat
# Запуск:  pip install fastapi uvicorn
#          python main.py
# Сервер:  http://127.0.0.1:8000

import time
import random
import string
import secrets
from html import escape
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

app = FastAPI(title="sldchat")

# ─────────────────────────── ХРАНИЛИЩЕ В ОПЕРАТИВКЕ ───────────────────────────

SESSIONS: dict = {}   # sid -> {"uid": str, "created": float, "last_active": float}
POSTS: dict = {}      # code -> { ... }

ONLINE_WINDOW = 60          # сек. — окно активности для счётчика онлайна
MAX_TEXT = 10000            # лимит символов в тексте поста
MAX_TITLE = 80
MAX_AUTHOR = 40
MAX_COMMENT = 500

# ──────────────────────────────── УТИЛИТЫ ────────────────────────────────────

def gen_code() -> str:
    """Генерирует уникальный 5-символьный код поста."""
    chars = string.ascii_letters + string.digits
    while True:
        code = "".join(random.choices(chars, k=5))
        if code not in POSTS:
            return code


def esc(value) -> str:
    """HTML-экранирование."""
    return escape(str(value)) if value is not None else ""


def online_count() -> int:
    now = time.time()
    return sum(1 for s in SESSIONS.values() if now - s.get("last_active", 0) < ONLINE_WINDOW)


def safe_referer(request: Request, fallback: str = "/") -> str:
    ref = request.headers.get("referer", "")
    if ref:
        p = urlparse(ref)
        if p.path:
            return p.path + (("?" + p.query) if p.query else "")
    return fallback


def fmt_time(ts: float) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))


# ─────────────────────────── MIDDLEWARE: СЕССИИ ──────────────────────────────

@app.middleware("http")
async def session_middleware(request: Request, call_next):
    sid = request.cookies.get("sldchat_sid")
    is_new = False

    if not sid or sid not in SESSIONS:
        sid = secrets.token_urlsafe(24)
        SESSIONS[sid] = {
            "uid": "u_" + secrets.token_hex(8),
            "created": time.time(),
            "last_active": time.time(),
        }
        is_new = True
    else:
        SESSIONS[sid]["last_active"] = time.time()

    request.state.sid = sid
    request.state.uid = SESSIONS[sid]["uid"]

    response = await call_next(request)

    if is_new:
        response.set_cookie(
            "sldchat_sid", sid,
            max_age=60 * 60 * 24 * 30,
            httponly=True, samesite="lax", path="/",
        )
    return response


# ───────────────────────────────── CSS ───────────────────────────────────────

CSS = """
:root {
    --bg: #f0f0f2;
    --surface: #ffffff;
    --text: #222222;
    --primary: #0055cc;
    --border: #888888;
    --active-bg: #dddddd;
    --error: #cc0000;
}
[data-theme="dark"] {
    --bg: #121212;
    --surface: #1e1e1e;
    --text: #e0e0e0;
    --primary: #3388ff;
    --border: #555555;
    --active-bg: #2d2d2d;
}
* { box-sizing: border-box; margin: 0; padding: 0; font-family: "Courier New", Courier, monospace, sans-serif; }
body { background: var(--bg); color: var(--text); padding: 15px; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }

.wrapper { width: 100%; max-width: 1050px; display: flex; flex-direction: column; }

header { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 2px solid var(--border); margin-bottom: 12px; }
.logo-block { display: flex; flex-direction: column; }
.logo-text { font-size: 1.7rem; font-weight: bold; color: var(--primary); cursor: pointer; text-decoration: none; display: flex; align-items: center; gap: 8px; }
.logo-sub { font-size: 0.8rem; color: var(--text); opacity: 0.7; margin-top: 2px; }

.online-counter { font-size: 0.75rem; color: #4caf50; background: rgba(76, 175, 80, 0.1); border: 1px solid rgba(76, 175, 80, 0.3); padding: 2px 8px; border-radius: 12px; font-weight: normal; display: inline-flex; align-items: center; gap: 4px; }

.text-btn { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 6px 12px; border-radius: 4px; font-size: 0.85rem; font-weight: bold; cursor: pointer; text-decoration: none; text-align: center; display: inline-block; }
.text-btn:hover { background: var(--active-bg); }

.main-layout { display: flex; gap: 15px; width: 100%; align-items: flex-start; }

.sidebar-left { width: 220px; flex-shrink: 0; display: flex; flex-direction: column; gap: 10px; }
.sidebar-left-menu { display: flex; flex-direction: column; gap: 6px; }

.nav-btn { background: var(--surface); border: 1px solid var(--border); padding: 10px; border-radius: 4px; color: var(--text); font-weight: bold; font-size: 0.85rem; cursor: pointer; text-align: left; width: 100%; display: block; text-decoration: none; }
.nav-btn.active { background: var(--primary); color: #ffffff; border-color: var(--primary); }
.nav-btn:hover:not(.active) { background: var(--active-bg); }

.sidebar-right { width: 220px; flex-shrink: 0; }
.info-header { font-weight: bold; font-size: 0.9rem; margin-bottom: 8px; border-bottom: 1px solid var(--border); padding-bottom: 4px; text-transform: uppercase; color: var(--primary); }
.info-text { font-size: 0.8rem; line-height: 1.4; margin-bottom: 12px; }

.content-area { flex-grow: 1; min-width: 0; width: 100%; overflow: hidden; }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 12px; width: 100%; overflow: hidden; }
.form-group { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
label { font-size: 0.85rem; font-weight: bold; }
input[type="text"], textarea, select { width: 100%; padding: 10px; border-radius: 4px; border: 1px solid var(--border); background: var(--surface); color: var(--text); font-size: 0.95rem; outline: none; }
input:focus, textarea:focus, select:focus { border-color: var(--primary); }

.btn { background: var(--primary); color: #ffffff; border: 1px solid var(--primary); padding: 10px 16px; border-radius: 4px; font-weight: bold; cursor: pointer; font-size: 0.95rem; text-align: center; display: inline-block; width: 100%; text-decoration: none; }
.btn:active { opacity: 0.8; }
.btn-tonal { background: var(--active-bg); color: var(--text); border: 1px solid var(--border); }
.btn-tonal:hover { background: var(--surface); }

.post-author-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; font-size: 0.8rem; border-bottom: 1px dotted var(--border); padding-bottom: 6px; gap: 8px; }
.author-name { font-weight: bold; color: var(--primary); }
.post-time { color: var(--text); opacity: 0.7; }
.post-title { font-size: 1.25rem; font-weight: bold; margin-bottom: 8px; word-break: break-word; }
.post-text { font-size: 0.95rem; line-height: 1.4; white-space: pre-wrap; margin-bottom: 12px; word-break: break-word; overflow-wrap: break-word; }

.post-footer { display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--border); padding-top: 10px; margin-top: 10px; flex-wrap: wrap; gap: 8px; }
.action-group { display: flex; gap: 6px; flex-wrap: wrap; }
.action-btn { background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 6px 12px; font-size: 0.8rem; font-weight: bold; color: var(--text); cursor: pointer; text-decoration: none; display: inline-block; }
.action-btn:hover { background: var(--active-bg); }
.action-btn.active { background: var(--primary); color: #ffffff; border-color: var(--primary); }

.comments-section { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 12px; }
.comment-item { margin-bottom: 8px; font-size: 0.9rem; background: var(--active-bg); padding: 8px; border-radius: 4px; border: 1px solid var(--border); overflow: hidden; }
.comment-header { display: flex; justify-content: space-between; font-weight: bold; font-size: 0.8rem; margin-bottom: 4px; gap: 6px; flex-wrap: wrap; }
.comment-text { line-height: 1.35; word-break: break-word; overflow-wrap: break-word; }
.comment-form { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; padding: 10px; border: 1px dashed var(--border); border-radius: 4px; background: var(--surface); }

.empty-msg { text-align: center; padding: 20px; opacity: 0.6; }

.del-inline { background: none; border: none; color: var(--error); cursor: pointer; font-weight: bold; font-size: 0.75rem; padding: 0 2px; font-family: inherit; }

@media (max-width: 650px) {
    body { padding-bottom: 80px; }
    .main-layout { flex-direction: column; gap: 10px; }
    .sidebar-left { position: fixed; bottom: 0; left: 0; width: 100%; background: var(--surface); border-top: 2px solid var(--border); padding: 10px; z-index: 999; box-shadow: 0px -4px 10px rgba(0,0,0,0.1); }
    .sidebar-left-menu { flex-direction: row; justify-content: space-around; width: 100%; }
    .sidebar-left-menu .nav-btn { flex: 1; text-align: center; padding: 8px; font-size: 0.8rem; }
    .sidebar-right { display: none; }
}
"""

# ─────────────────────────── JS (счётчик символов) ───────────────────────────

CHAR_COUNTER_JS = """
<script>
(function () {
    var ta = document.getElementById('post-text');
    var cc = document.getElementById('char-count');
    if (!ta || !cc) return;
    function upd() { cc.textContent = ta.value.length + ' / 10000'; }
    ta.addEventListener('input', upd);
    upd();
})();
</script>
"""

# ───────────────────────────── ШАБЛОН СТРАНИЦЫ ───────────────────────────────

def render_page(content_html: str, active: str, online: int,
                title: str = "sldchat — Блог-платформа") -> str:
    feed_a = "active" if active == "feed" else ""
    create_a = "active" if active == "create" else ""
    my_a = "active" if active == "my" else ""

    return f"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrapper">
    <header>
        <div class="logo-block">
            <a class="logo-text" href="/">sldchat <span class="online-counter">● {online} онлайн</span></a>
            <div class="logo-sub">посты и обсуждения</div>
        </div>
    </header>

    <div class="main-layout">
        <aside class="sidebar-left">
            <div class="sidebar-left-menu">
                <a class="nav-btn {feed_a}" href="/">Лента</a>
                <a class="nav-btn {create_a}" href="/create">Создать</a>
                <a class="nav-btn {my_a}" href="/my">Мои посты</a>
            </div>
        </aside>

        <main class="content-area">
            {content_html}
        </main>

        <aside class="sidebar-right">
            <div class="card" style="padding: 12px;">
                <div class="info-header">О платформе</div>
                <p class="info-text">sldchat — простой и свободный текстовый блог без лишнего.</p>

                <div class="info-header">Правила</div>
                <p class="info-text">Пишите вежливо, делитесь мыслями. Максимум 10 000 символов в одном посте.</p>

                <div class="info-header">Хранение</div>
                <p class="info-text">Все сессии и посты живут в оперативной памяти сервера и исчезают при перезапуске.</p>
            </div>
        </aside>
    </div>
</div>
</body>
</html>"""


# ─────────────────────────── РЕНДЕР ОДНОГО ПОСТА ─────────────────────────────

def render_post(post: dict, uid: str, comments_open: bool = False) -> str:
    code = post["code"]
    is_owner = post["owner_id"] == uid
    my_rating = post["rating_users"].get(uid, 0)
    is_unlisted = post["visibility"] == "unlisted"
    comments = post.get("comments", [])
    views = len(post.get("viewed_users", set()))

    up_cls = "active" if my_rating == 1 else ""
    down_cls = "active" if my_rating == -1 else ""

    h = []
    h.append('<div class="card">')

    # Шапка поста
    h.append('<div class="post-author-row"><div>')
    h.append(f'<span class="author-name">{esc(post["author"])}</span> • '
             f'<span class="post-time">{fmt_time(post["created_at"])}</span>')
    h.append('</div>')
    if is_owner:
        h.append(
            f'<form method="post" action="/post/{code}/delete" '
            f'onsubmit="return confirm(\'Удалить этот пост?\');" style="display:inline;">'
            f'<button type="submit" class="text-btn" '
            f'style="color:var(--error); padding:2px 8px; font-size:0.75rem;">[Удалить]</button>'
            f'</form>'
        )
    h.append('</div>')

    # Заголовок и текст
    h.append(f'<div class="post-title">{esc(post["title"])}</div>')
    if post["text"]:
        h.append(f'<div class="post-text">{esc(post["text"])}</div>')

    # Футер
    h.append('<div class="post-footer">')

    if is_unlisted:
        h.append(f'<span style="font-size:0.8rem; font-weight:bold; opacity:0.7;">'
                 f'[Просмотров: {views}]</span>')
    else:
        h.append('<div class="action-group">')
        h.append(
            f'<form method="post" action="/rate/{code}" style="display:inline;">'
            f'<input type="hidden" name="value" value="1">'
            f'<button type="submit" class="action-btn {up_cls}">[Лайков: {post["upvotes"]}]</button>'
            f'</form>'
        )
        h.append(
            f'<form method="post" action="/rate/{code}" style="display:inline;">'
            f'<input type="hidden" name="value" value="-1">'
            f'<button type="submit" class="action-btn {down_cls}">[Дизлайков: {post["downvotes"]}]</button>'
            f'</form>'
        )
        h.append('</div>')

    h.append('<div class="action-group">')
    h.append(f'<a class="action-btn" href="/p/{code}">[Комментариев: {len(comments)}]</a>')
    if is_unlisted:
        h.append(f'<span class="action-btn" style="cursor:default;">[Код: {esc(code)}]</span>')
    h.append('</div>')

    h.append('</div>')  # /post-footer

    # Комментарии
    if comments_open:
        h.append('<div class="comments-section">')
        h.append('<div style="margin-top:8px;">')

        if comments:
            for c in comments:
                can_del = (c["owner_id"] == uid) or is_owner
                h.append('<div class="comment-item">')
                h.append('<div class="comment-header">')
                h.append(f'<span>{esc(c["author"])}</span>')
                h.append('<div>')
                h.append(f'<span style="opacity:0.6; font-weight:normal; font-size:0.75rem;">'
                         f'{fmt_time(c["created_at"])}</span>')
                if can_del:
                    h.append(
                        f'<form method="post" action="/comment/{code}/delete/{c["id"]}" '
                        f'onsubmit="return confirm(\'Удалить комментарий?\');" '
                        f'style="display:inline; margin-left:6px;">'
                        f'<button type="submit" class="del-inline">[Удалить]</button>'
                        f'</form>'
                    )
                h.append('</div>')
                h.append('</div>')
                h.append(f'<div class="comment-text">{esc(c["text"])}</div>')
                h.append('</div>')
        else:
            h.append('<p style="font-size:0.8rem; opacity:0.6; margin-bottom:8px; '
                     'padding-left:2px;">Пока пусто.</p>')

        h.append('</div>')

        h.append(
            f'<form method="post" action="/comment/{code}" class="comment-form">'
            f'<input type="text" name="author" placeholder="Имя (Аноним)" maxlength="24">'
            f'<input type="text" name="text" placeholder="Ваш комментарий..." '
            f'maxlength="{MAX_COMMENT}" required>'
            f'<button type="submit" class="btn btn-tonal" '
            f'style="padding:6px; font-size:0.8rem;">Отправить</button>'
            f'</form>'
        )
        h.append('</div>')  # /comments-section

    h.append('</div>')  # /card
    return "".join(h)


# ──────────────────────────── ФОРМА СОЗДАНИЯ ─────────────────────────────────

CREATE_FORM_HTML = f"""
<div class="card">
    <h2 style="margin-bottom: 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px;">Написать пост</h2>

    <form method="post" action="/create">
        <div class="form-group">
            <label>Ваше имя</label>
            <input type="text" name="author" placeholder="Аноним" maxlength="{MAX_AUTHOR}">
        </div>

        <div class="form-group">
            <label>Заголовок публикации</label>
            <input type="text" name="title" placeholder="Введите название..." maxlength="{MAX_TITLE}" required>
        </div>

        <div class="form-group">
            <label>Текст публикации</label>
            <textarea id="post-text" name="text" placeholder="Введите текст вашего сообщения..."
                      rows="10" maxlength="{MAX_TEXT}"></textarea>
            <div id="char-count" style="font-size:0.75rem; opacity:0.7; text-align:right;">0 / {MAX_TEXT}</div>
        </div>

        <div class="form-group">
            <label>Режим приватности</label>
            <select name="visibility">
                <option value="public">В общую ленту</option>
                <option value="unlisted">Только по ссылке (Скрытый)</option>
            </select>
        </div>

        <button class="btn" type="submit">Опубликовать</button>
    </form>
</div>
{CHAR_COUNTER_JS}
"""


# ──────────────────────────────── МАРШРУТЫ ───────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def route_feed(request: Request):
    uid = request.state.uid
    posts = [p for p in POSTS.values() if p["visibility"] == "public"]
    posts.sort(key=lambda p: p["created_at"], reverse=True)

    if posts:
        body = "".join(render_post(p, uid, comments_open=False) for p in posts)
    else:
        body = '<div class="empty-msg">Лента пока пуста.</div>'

    return HTMLResponse(render_page(body, "feed", online_count()))


@app.get("/create", response_class=HTMLResponse)
async def route_create_get(request: Request):
    return HTMLResponse(render_page(CREATE_FORM_HTML, "create", online_count()))


@app.post("/create")
async def route_create_post(
    request: Request,
    author: str = Form(""),
    title: str = Form(""),
    text: str = Form(""),
    visibility: str = Form("public"),
):
    uid = request.state.uid
    author = author.strip()[:MAX_AUTHOR] or "Аноним"
    title = title.strip()[:MAX_TITLE]
    text = text.strip()[:MAX_TEXT]

    if not title or not text:
        return RedirectResponse("/create", status_code=303)

    if visibility not in ("public", "unlisted"):
        visibility = "public"

    code = gen_code()
    POSTS[code] = {
        "code": code,
        "title": title,
        "text": text,
        "author": author,
        "owner_id": uid,
        "created_at": time.time(),
        "visibility": visibility,
        "upvotes": 0,
        "downvotes": 0,
        "rating_users": {},
        "viewed_users": set(),
        "comments": [],
    }

    if visibility == "unlisted":
        return RedirectResponse(f"/p/{code}", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.get("/my", response_class=HTMLResponse)
async def route_my(request: Request):
    uid = request.state.uid
    posts = [p for p in POSTS.values() if p["owner_id"] == uid]
    posts.sort(key=lambda p: p["created_at"], reverse=True)

    head = ('<h2 style="margin-bottom: 12px; font-size: 1.1rem; '
            'border-bottom: 1px solid var(--border); padding-bottom: 6px;">Мои публикации</h2>')

    if posts:
        body = head + "".join(render_post(p, uid, comments_open=False) for p in posts)
    else:
        body = head + '<div class="empty-msg">У вас пока нет публикаций.</div>'

    return HTMLResponse(render_page(body, "my", online_count()))


@app.get("/p/{code}", response_class=HTMLResponse)
async def route_single(request: Request, code: str):
    uid = request.state.uid
    post = POSTS.get(code)

    back = '<a class="text-btn" href="/" style="margin-bottom: 12px;">[Вернуться в ленту]</a>'

    if not post:
        content = back + '<div class="card">Запись удалена или не существует.</div>'
        return HTMLResponse(render_page(content, "", online_count(),
                                        title="Запись не найдена — sldchat"))

    post["viewed_users"].add(uid)
    content = back + render_post(post, uid, comments_open=True)
    return HTMLResponse(render_page(content, "", online_count(),
                                    title=f'{post["title"]} — sldchat'))


@app.post("/rate/{code}")
async def route_rate(request: Request, code: str, value: int = Form(...)):
    uid = request.state.uid
    back = safe_referer(request, "/")

    post = POSTS.get(code)
    if not post or post["visibility"] == "unlisted" or value not in (1, -1):
        return RedirectResponse(back, status_code=303)

    current = post["rating_users"].get(uid, 0)
    if current == value:
        return RedirectResponse(back, status_code=303)

    if value == 1:
        post["upvotes"] += 1
        if current == -1:
            post["downvotes"] -= 1
    else:
        post["downvotes"] += 1
        if current == 1:
            post["upvotes"] -= 1

    post["rating_users"][uid] = value
    return RedirectResponse(back, status_code=303)


@app.post("/comment/{code}")
async def route_add_comment(
    request: Request,
    code: str,
    author: str = Form(""),
    text: str = Form(""),
):
    uid = request.state.uid
    post = POSTS.get(code)
    if not post:
        return RedirectResponse("/", status_code=303)

    author = author.strip()[:24] or "Аноним"
    text = text.strip()[:MAX_COMMENT]

    if text:
        post["comments"].append({
            "id": "c_" + secrets.token_hex(4),
            "author": author,
            "text": text,
            "created_at": time.time(),
            "owner_id": uid,
        })

    return RedirectResponse(f"/p/{code}", status_code=303)


@app.post("/comment/{code}/delete/{comment_id}")
async def route_delete_comment(request: Request, code: str, comment_id: str):
    uid = request.state.uid
    post = POSTS.get(code)
    if not post:
        return RedirectResponse("/", status_code=303)

    post["comments"] = [
        c for c in post["comments"]
        if not (c["id"] == comment_id and (c["owner_id"] == uid or post["owner_id"] == uid))
    ]
    return RedirectResponse(f"/p/{code}", status_code=303)


@app.post("/post/{code}/delete")
async def route_delete_post(request: Request, code: str):
    uid = request.state.uid
    post = POSTS.get(code)

    back = safe_referer(request, "/my")
    if f"/p/{code}" in back:
        back = "/my"

    if post and post["owner_id"] == uid:
        del POSTS[code]

    return RedirectResponse(back, status_code=303)


# ──────────────────────────────── ЗАПУСК ─────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
