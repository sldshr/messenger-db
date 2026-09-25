"""
sldchat — анонимная блог-платформа на FastAPI.
Все данные хранятся в оперативной памяти и очищаются при перезапуске сервера.

Запуск:
    pip install fastapi uvicorn
    python main.py
"""

import asyncio
import json
import random
import string
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


# ----------------------------------------------------------------------------
# Состояние (в оперативной памяти)
# ----------------------------------------------------------------------------
START_TIME = time.time()
posts: Dict[str, Dict[str, Any]] = {}
presence: Dict[str, int] = {}
clients: List[WebSocket] = []

MAX_TEXT_LEN = 5000
MAX_TITLE_LEN = 80
MAX_COMMENT_LEN = 200
MAX_ASCII_LEN = 120000


# ----------------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def uptime_seconds() -> float:
    return time.time() - START_TIME


def gen_id(length: int = 5) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


async def broadcast(message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False)
    dead: List[WebSocket] = []
    for ws in list(clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in clients:
            clients.remove(ws)


async def broadcast_state() -> None:
    await broadcast({
        "type": "state",
        "posts": list(posts.values()),
        "uptime": uptime_seconds(),
    })


async def broadcast_stats() -> None:
    await broadcast({
        "type": "stats",
        "uptime": uptime_seconds(),
    })


async def stats_loop() -> None:
    while True:
        await asyncio.sleep(5)
        if clients:
            await broadcast_stats()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(stats_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="sldchat", lifespan=lifespan)


# ----------------------------------------------------------------------------
# Pydantic-модели
# ----------------------------------------------------------------------------
class CreatePostBody(BaseModel):
    title: str
    text: str = ""
    visibility: str = "public"
    ownerId: str
    asciiArt: str = ""


class RateBody(BaseModel):
    value: int
    uid: str


class CommentBody(BaseModel):
    text: str
    uid: str


# ----------------------------------------------------------------------------
# REST API
# ----------------------------------------------------------------------------
@app.get("/api/state")
async def api_state():
    return {
        "posts": list(posts.values()),
        "uptime": uptime_seconds(),
    }


@app.post("/api/posts")
async def api_create_post(body: CreatePostBody):
    title = (body.title or "").strip()[:MAX_TITLE_LEN]
    text = body.text or ""
    ascii_art = body.asciiArt or ""

    if not title:
        raise HTTPException(400, "Введите заголовок!")
    if not text.strip() and not ascii_art.strip():
        raise HTTPException(400, "Напишите что-нибудь или добавьте ASCII-изображение!")
    if len(text) > MAX_TEXT_LEN:
        raise HTTPException(400, f"Текст длиннее {MAX_TEXT_LEN} символов")
    if len(ascii_art) > MAX_ASCII_LEN:
        raise HTTPException(400, "ASCII-изображение слишком большое")
    if body.visibility not in ("public", "unlisted"):
        body.visibility = "public"

    pid = gen_id(5)
    while pid in posts:
        pid = gen_id(5)

    post = {
        "id": pid,
        "title": title,
        "text": text,
        "asciiArt": ascii_art,
        "author": "Аноним",
        "ownerId": body.ownerId,
        "createdAt": now_ms(),
        "visibility": body.visibility,
        "code": pid,
        "upvotes": 0,
        "downvotes": 0,
        "ratingUsers": {},
        "viewedUsers": {},
        "comments": [],
    }
    posts[pid] = post
    await broadcast_state()
    return post


@app.delete("/api/posts/{pid}")
async def api_delete_post(pid: str, uid: str = Query(...)):
    post = posts.get(pid)
    if not post:
        raise HTTPException(404, "Пост не найден")
    if post["ownerId"] != uid:
        raise HTTPException(403, "Нет доступа")
    del posts[pid]
    await broadcast_state()
    return {"ok": True}


@app.post("/api/posts/{pid}/rate")
async def api_rate_post(pid: str, body: RateBody):
    post = posts.get(pid)
    if not post:
        raise HTTPException(404, "Пост не найден")
    if post.get("visibility") == "unlisted":
        raise HTTPException(403, "Скрытые посты не оцениваются")
    if body.value not in (-1, 1):
        raise HTTPException(400, "Некорректная оценка")

    rating_users: Dict[str, int] = post.setdefault("ratingUsers", {})
    current = rating_users.get(body.uid, 0)
    if current == body.value:
        return post

    up = post.get("upvotes", 0)
    down = post.get("downvotes", 0)

    if body.value == 1:
        up += 1
        if current == -1:
            down = max(0, down - 1)
    else:
        down += 1
        if current == 1:
            up = max(0, up - 1)

    post["upvotes"] = up
    post["downvotes"] = down
    rating_users[body.uid] = body.value
    await broadcast_state()
    return post


@app.post("/api/posts/{pid}/view")
async def api_track_view(pid: str, uid: str = Query(...)):
    post = posts.get(pid)
    if not post:
        raise HTTPException(404)
    viewed: Dict[str, bool] = post.setdefault("viewedUsers", {})
    if uid not in viewed:
        viewed[uid] = True
        await broadcast_state()
    return {"ok": True, "views": len(viewed)}


@app.post("/api/posts/{pid}/comments")
async def api_add_comment(pid: str, body: CommentBody):
    post = posts.get(pid)
    if not post:
        raise HTTPException(404)
    text = (body.text or "").strip()[:MAX_COMMENT_LEN]
    if not text:
        raise HTTPException(400, "Пустой комментарий")
    comment = {
        "id": "c_" + gen_id(7),
        "author": "Аноним",
        "text": text,
        "createdAt": now_ms(),
        "ownerId": body.uid,
    }
    post.setdefault("comments", []).append(comment)
    await broadcast_state()
    return comment


@app.delete("/api/posts/{pid}/comments/{cid}")
async def api_delete_comment(pid: str, cid: str, uid: str = Query(...)):
    post = posts.get(pid)
    if not post:
        raise HTTPException(404)
    comments = post.get("comments", [])
    target = next((c for c in comments if c["id"] == cid), None)
    if not target:
        raise HTTPException(404)
    if target["ownerId"] != uid and post["ownerId"] != uid:
        raise HTTPException(403)
    post["comments"] = [c for c in comments if c["id"] != cid]
    await broadcast_state()
    return {"ok": True}


# ----------------------------------------------------------------------------
# WebSocket
# ----------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, uid: str = Query(...)):
    await websocket.accept()
    clients.append(websocket)
    presence[uid] = presence.get(uid, 0) + 1

    try:
        await websocket.send_text(json.dumps({
            "type": "state",
            "posts": list(posts.values()),
            "uptime": uptime_seconds(),
        }, ensure_ascii=False))

        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                try:
                    await websocket.send_text(json.dumps({"type": "pong"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if websocket in clients:
            clients.remove(websocket)
        if uid in presence:
            presence[uid] -= 1
            if presence[uid] <= 0:
                del presence[uid]


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>sldchat — Блог-платформа</title>
<meta property="og:title" content="sldchat">
<meta property="og:description" content="Анонимная блог-платформа">
<meta property="og:type" content="website">
<style>
:root {
    --bg: #f0f0f2;
    --surface: #ffffff;
    --text: #222222;
    --primary: #0055cc;
    --border: #888888;
    --active-bg: #dddddd;
    --error: #cc0000;
    --transition-speed: 0.2s;
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
.logo-text { font-size: 1.7rem; font-weight: bold; color: var(--primary); cursor: pointer; text-decoration: none; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.logo-sub { font-size: 0.8rem; color: var(--text); opacity: 0.7; margin-top: 2px; }

.uptime-counter { font-size: 0.75rem; color: var(--text); background: var(--active-bg); border: 1px solid var(--border); padding: 2px 8px; border-radius: 12px; font-weight: normal; display: inline-flex; align-items: center; gap: 4px; opacity: 0.85; }

.text-btn { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 6px 12px; border-radius: 4px; font-size: 0.85rem; font-weight: bold; cursor: pointer; text-decoration: none; text-align: center; }
.text-btn:hover { background: var(--active-bg); }

.main-layout { display: flex; gap: 15px; width: 100%; align-items: flex-start; }

.sidebar-left { width: 220px; flex-shrink: 0; display: flex; flex-direction: column; gap: 10px; }
.sidebar-left-menu { display: flex; flex-direction: column; gap: 6px; }

.nav-btn { background: var(--surface); border: 1px solid var(--border); padding: 10px; border-radius: 4px; color: var(--text); font-weight: bold; font-size: 0.85rem; cursor: pointer; text-align: left; width: 100%; }
.nav-btn.active { background: var(--primary); color: #ffffff; border-color: var(--primary); }
.nav-btn:hover:not(.active) { background: var(--active-bg); }

.sidebar-right { width: 220px; flex-shrink: 0; }
.info-header { font-weight: bold; font-size: 0.9rem; margin-bottom: 8px; border-bottom: 1px solid var(--border); padding-bottom: 4px; text-transform: uppercase; color: var(--primary); }
.info-text { font-size: 0.8rem; line-height: 1.4; margin-bottom: 12px; }

.content-area { flex-grow: 1; min-width: 0; width: 100%; overflow: hidden; }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 12px; width: 100%; overflow: hidden; }
.form-group { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; min-width: 0; }
label { font-size: 0.85rem; font-weight: bold; }
input[type="text"], textarea, select {
    width: 100%;
    max-width: 100%;
    padding: 10px;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.95rem;
    outline: none;
    font-family: inherit;
}
textarea {
    resize: vertical;
    min-height: 120px;
    max-height: 60vh;
    display: block;
}
input:focus, textarea:focus, select:focus { border-color: var(--primary); }

.char-counter { font-size: 0.75rem; opacity: 0.65; text-align: right; margin-top: -2px; }
.char-counter.limit { color: var(--error); opacity: 1; font-weight: bold; }

.hint { font-size: 0.72rem; opacity: 0.7; line-height: 1.5; padding: 6px 8px; background: var(--active-bg); border: 1px dashed var(--border); border-radius: 4px; }
.hint code { background: var(--surface); padding: 1px 4px; border-radius: 2px; }

.btn { background: var(--primary); color: #ffffff; border: 1px solid var(--primary); padding: 10px 16px; border-radius: 4px; font-weight: bold; cursor: pointer; font-size: 0.95rem; text-align: center; display: inline-block; width: 100%; }
.btn:active { opacity: 0.8; }
.btn-tonal { background: var(--active-bg); color: var(--text); border: 1px solid var(--border); }
.btn-tonal:hover { background: var(--surface); }

.view { display: none; }
.view.active { display: block; }

.post-author-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; font-size: 0.8rem; border-bottom: 1px dotted var(--border); padding-bottom: 6px; gap: 8px; flex-wrap: wrap; }
.author-name { font-weight: bold; color: var(--primary); }
.post-time { color: var(--text); opacity: 0.7; }
.post-title { font-size: 1.25rem; font-weight: bold; margin-bottom: 8px; word-break: break-word; }
.post-text { font-size: 0.95rem; line-height: 1.4; white-space: pre-wrap; margin-bottom: 12px; word-break: break-word; overflow-wrap: anywhere; }

/* ASCII art */
.ascii-art {
    font-family: "Courier New", Courier, monospace;
    font-size: 6px;
    line-height: 1.0;
    letter-spacing: 0;
    white-space: pre;
    overflow: auto;
    max-height: 480px;
    max-width: 100%;
    padding: 8px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: zoom-in;
    color: var(--text);
    margin-bottom: 10px;
    tab-size: 1;
}
.ascii-art:hover { border-color: var(--primary); }

.ascii-controls { display: grid; gap: 8px; padding: 10px; background: var(--active-bg); border-radius: 4px; border: 1px solid var(--border); margin-top: 6px; }
.ascii-controls label { font-size: 0.78rem; display: flex; justify-content: space-between; align-items: center; gap: 8px; font-weight: bold; }
.ascii-controls input[type="range"] { width: 100%; accent-color: var(--primary); }
.ascii-controls select { font-size: 0.85rem; padding: 6px; }
.ascii-controls .row { display: flex; flex-direction: column; gap: 3px; }
.ascii-controls .row-flex { display: flex; gap: 12px; flex-wrap: wrap; }
.ascii-controls .row-flex label { flex: 1; min-width: 130px; justify-content: flex-start; }
.ascii-controls .row-flex input[type="checkbox"] { accent-color: var(--primary); }

.post-footer { display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--border); padding-top: 10px; margin-top: 10px; flex-wrap: wrap; gap: 8px; }
.action-group { display: flex; gap: 6px; flex-wrap: wrap; }
.action-btn { background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 6px 12px; font-size: 0.8rem; font-weight: bold; color: var(--text); cursor: pointer; }
.action-btn:hover { background: var(--active-bg); }
.action-btn.active { background: var(--primary); color: #ffffff; border-color: var(--primary); }

.comments-section { display: none; margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 12px; }
.comments-section.open { display: block; }
.comment-item { margin-bottom: 8px; font-size: 0.9rem; background: var(--active-bg); padding: 8px; border-radius: 4px; border: 1px solid var(--border); overflow: hidden; }
.comment-header { display: flex; justify-content: space-between; font-weight: bold; font-size: 0.8rem; margin-bottom: 4px; gap: 6px; flex-wrap: wrap; }
.comment-text { line-height: 1.35; word-break: break-word; overflow-wrap: anywhere; }
.comment-form { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; padding: 10px; border: 1px dashed var(--border); border-radius: 4px; background: var(--surface); }

.toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg); padding: 10px 20px; border: 1px solid var(--border); border-radius: 4px; font-size: 0.85rem; font-weight: bold; opacity: 0; pointer-events: none; transition: opacity var(--transition-speed); z-index: 1000; max-width: 90vw; text-align: center; }
.toast.show { opacity: 1; }

.hidden { display: none !important; }

/* ASCII modal */
#ascii-modal {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.94);
    z-index: 10000;
    overflow: auto;
    padding: 70px 20px 30px;
}
#ascii-modal-toolbar {
    position: fixed; top: 14px; right: 14px;
    display: flex; gap: 8px; z-index: 10001;
    background: var(--surface);
    padding: 6px;
    border: 1px solid var(--border);
    border-radius: 6px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
}
#ascii-modal pre {
    display: block;
    width: fit-content;
    margin: 0 auto;
    font-family: "Courier New", Courier, monospace;
    line-height: 1.0;
    letter-spacing: 0;
    white-space: pre;
    color: #e8e8e8;
    background: #000;
    padding: 14px;
    border-radius: 6px;
    border: 1px solid #333;
    tab-size: 1;
}

@media (max-width: 650px) {
    body { padding-bottom: 70px; }
    .main-layout { flex-direction: column; gap: 10px; }
    .sidebar-left { position: fixed; bottom: 0; left: 0; width: 100%; background: var(--surface); border-top: 2px solid var(--border); padding: 10px; z-index: 999; box-shadow: 0px -4px 10px rgba(0,0,0,0.1); }
    .sidebar-left-menu { flex-direction: row !important; justify-content: space-around; width: 100%; }
    .sidebar-left-menu .nav-btn { flex: 1; text-align: center; padding: 8px; font-size: 0.8rem; }
    .sidebar-right { display: none; }
    .toast { bottom: 80px; }
    .ascii-art { font-size: 4px; }
}
</style>
</head>
<body>
    <div class="wrapper">
        <header>
            <div class="logo-block">
                <div class="logo-text" onclick="window.goHome()">
                    sldchat
                    <span id="uptime" class="uptime-counter">▲ 0ч 0м 0с</span>
                </div>
                <div class="logo-sub">посты и обсуждения</div>
            </div>
        </header>

        <div class="main-layout">
            <aside class="sidebar-left">
                <div class="sidebar-left-menu">
                    <button class="nav-btn active" id="btn-tab-feed" onclick="window.switchTab('feed')">Лента</button>
                    <button class="nav-btn" id="btn-tab-create" onclick="window.switchTab('create')">Создать</button>
                    <button class="nav-btn" id="btn-tab-my" onclick="window.switchTab('my')">Мои посты</button>
                </div>
            </aside>

            <main class="content-area">
                <div id="toast" class="toast">Сбой соединения.</div>

                <!-- FEED VIEW -->
                <section id="view-feed" class="view active">
                    <div id="feed-list"><div style="text-align:center; padding: 20px; opacity:0.6;">Лента пока пуста.</div></div>
                </section>

                <!-- SINGLE POST VIEW -->
                <section id="view-single" class="view">
                    <button class="text-btn" onclick="window.goHome()" style="margin-bottom: 12px; display: inline-block;">[Вернуться в ленту]</button>
                    <div id="single-post-container"></div>
                </section>

                <!-- CREATE VIEW -->
                <section id="view-create" class="view">
                    <div class="card">
                        <h2 style="margin-bottom: 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px;">Написать пост</h2>

                        <div class="form-group">
                            <label>Заголовок публикации</label>
                            <input type="text" id="post-title-input" placeholder="Введите название..." maxlength="80">
                        </div>

                        <div class="form-group">
                            <label>Текст публикации</label>
                            <textarea id="post-text-input" placeholder="Введите текст вашего сообщения..." rows="8" maxlength="5000"></textarea>
                            <div class="char-counter" id="post-text-counter">0 / 5000</div>
                            <div class="hint">
                                Цвета в тексте: <code>[c=red]красный[/c]</code>
                                <code>[c=#ff8800]оранжевый[/c]</code>
                                <code>[c=rgb(0,150,255)]синий[/c]</code>
                            </div>
                        </div>

                        <div class="form-group">
                            <label>ASCII-изображение (необязательно)</label>
                            <label class="btn btn-tonal" style="cursor: pointer; display: block; text-align: center;">
                                <input type="file" class="hidden" id="ascii-image-input" accept="image/*" onchange="window.handleAsciiImage(event)">
                                [Выбрать изображение]
                            </label>

                            <div id="ascii-controls" class="ascii-controls hidden">
                                <div class="row">
                                    <label>Ширина: <span id="ascii-width-val">80</span> симв.</label>
                                    <input type="range" id="ascii-width" min="20" max="180" step="2" value="80">
                                </div>
                                <div class="row">
                                    <label>Контраст: <span id="ascii-contrast-val">1.0</span></label>
                                    <input type="range" id="ascii-contrast" min="0.4" max="3" step="0.05" value="1.0">
                                </div>
                                <div class="row">
                                    <label>Яркость: <span id="ascii-brightness-val">0</span></label>
                                    <input type="range" id="ascii-brightness" min="-100" max="100" step="1" value="0">
                                </div>
                                <div class="row">
                                    <label>Набор символов</label>
                                    <select id="ascii-charset">
                                        <option value="classic">Классический (10)</option>
                                        <option value="detailed">Детальный (70)</option>
                                        <option value="blocks">Блоки ░▒▓█ (5)</option>
                                        <option value="dots">Точки .oO@ (5)</option>
                                        <option value="digits">Цифры (10)</option>
                                        <option value="letters">Буквы (26)</option>
                                    </select>
                                </div>
                                <div class="row-flex">
                                    <label><input type="checkbox" id="ascii-invert"> Инвертировать</label>
                                    <label><input type="checkbox" id="ascii-white-bg" checked> Белый фон</label>
                                </div>
                                <div style="font-size:0.72rem; opacity:0.7;">
                                    Размер: <span id="ascii-size">—</span>
                                </div>
                            </div>

                            <pre id="ascii-preview" class="ascii-art hidden" onclick="window.openAsciiViewer(this.textContent)"></pre>

                            <button id="ascii-clear-btn" class="btn btn-tonal hidden" onclick="window.clearAscii()" style="margin-top:6px;">[Убрать изображение]</button>
                        </div>

                        <div class="form-group">
                            <label>Режим приватности</label>
                            <select id="post-visibility">
                                <option value="public">В общую ленту</option>
                                <option value="unlisted">Только по ссылке (Скрытый)</option>
                            </select>
                        </div>

                        <button class="btn" onclick="window.createPost()">Опубликовать</button>
                    </div>
                </section>

                <!-- MY POSTS VIEW -->
                <section id="view-my" class="view">
                    <h2 style="margin-bottom: 12px; font-size: 1.1rem; border-bottom: 1px solid var(--border); padding-bottom: 6px;">Мои публикации</h2>
                    <div id="my-posts-list"><div style="text-align:center; padding: 20px; opacity:0.6;">У вас пока нет публикаций.</div></div>
                </section>
            </main>

            <aside class="sidebar-right">
                <div class="card" style="padding: 12px;">
                    <div class="info-header">О платформе</div>
                    <p class="info-text">sldchat — простой и свободный анонимный блог без ограничений.</p>

                    <div class="info-header">Цвета</div>
                    <p class="info-text">В тексте постов можно использовать <code>[c=red]...[/c]</code> — имена, HEX и rgb().</p>

                    <div class="info-header">ASCII-фото</div>
                    <p class="info-text">Изображения конвертируются в ASCII прямо в браузере. Настройте ширину, контраст и набор символов.</p>

                    <div class="info-header">Хранение</div>
                    <p class="info-text">Все данные хранятся в оперативной памяти сервера и очищаются при его перезапуске.</p>
                </div>
            </aside>
        </div>
    </div>

    <!-- ASCII viewer modal -->
    <div id="ascii-modal" class="hidden" onclick="if(event.target===this) window.closeAsciiViewer()">
        <div id="ascii-modal-toolbar">
            <button class="text-btn" onclick="window.asciiZoom(-1)">A−</button>
            <button class="text-btn" onclick="window.asciiZoom(1)">A+</button>
            <button class="text-btn" onclick="window.asciiCopy()">[Копировать]</button>
            <button class="text-btn" onclick="window.closeAsciiViewer()">[Закрыть]</button>
        </div>
        <pre id="ascii-modal-content"></pre>
    </div>

    <script>
        const MAX_TEXT_LEN = 5000;

        const ASCII_CHARSETS = {
            classic:  " .:-=+*#%@",
            detailed: " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",
            blocks:   " ░▒▓█",
            dots:     " .oO@",
            digits:   "0123456789",
            letters:  "abcdefghijklmnopqrstuvwxyz",
        };

        const localUid = (() => {
            let id = localStorage.getItem('sldchat_uid');
            if (!id) {
                id = 'u_' + Math.random().toString(36).substring(2, 10);
                localStorage.setItem('sldchat_uid', id);
            }
            return id;
        })();

        let allPosts = [];
        let ws = null;
        let uptimeBase = 0;
        let uptimeAt = Date.now();
        const trackedViews = new Set();

        // ---- ASCII конвертер ----
        let currentAsciiImage = null;
        let currentAsciiText = '';

        function escapeHtml(text) {
            const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
            return text ? String(text).replace(/[&<>"']/g, m => map[m]) : '';
        }

        // ---- Цвета ----
        const COLOR_NAMES = new Set([
            'black','silver','gray','grey','white','maroon','red','purple','fuchsia','green','lime',
            'olive','yellow','navy','blue','teal','aqua','cyan','magenta','orange','pink','brown',
            'gold','coral','salmon','crimson','tomato','khaki','plum','orchid','turquoise','beige',
            'ivory','azure','violet','indigo','tan','lavender'
        ]);
        function isValidColor(c) {
            c = String(c).trim().toLowerCase();
            if (COLOR_NAMES.has(c)) return true;
            if (/^#[0-9a-f]{3}$/i.test(c)) return true;
            if (/^#[0-9a-f]{6}$/i.test(c)) return true;
            if (/^rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\)$/i.test(c)) return true;
            return false;
        }
        function renderRichText(text) {
            if (!text) return '';
            const re = /\[c=([^\]]+)\]|\[\/c\]/gi;
            let out = '';
            const stack = [];
            let last = 0;
            let m;
            while ((m = re.exec(text)) !== null) {
                if (m.index > last) out += escapeHtml(text.slice(last, m.index));
                if (m[0].toLowerCase() === '[/c]') {
                    if (stack.length > 0) {
                        const wasOpened = stack.pop();
                        out += wasOpened ? '</span>' : '[/c]';
                    } else {
                        out += '[/c]';
                    }
                } else {
                    const color = m[1].trim();
                    if (isValidColor(color)) {
                        out += `<span style="color:${color.toLowerCase()}">`;
                        stack.push(true);
                    } else {
                        out += escapeHtml(m[0]);
                        stack.push(false);
                    }
                }
                last = re.lastIndex;
            }
            if (last < text.length) out += escapeHtml(text.slice(last));
            while (stack.length > 0) {
                const wasOpened = stack.pop();
                if (wasOpened) out += '</span>';
            }
            return out;
        }

        // ---- Аптайм ----
        function renderUptime() {
            const total = Math.floor(uptimeBase + (Date.now() - uptimeAt) / 1000);
            const d = Math.floor(total / 86400);
            const h = Math.floor((total % 86400) / 3600);
            const m = Math.floor((total % 3600) / 60);
            const s = total % 60;
            let text = '';
            if (d > 0) text += d + 'д ';
            text += `${h}ч ${m}м ${s}с`;
            document.getElementById('uptime').textContent = '▲ ' + text;
        }
        setInterval(renderUptime, 1000);
        renderUptime();

        function applyState(data) {
            allPosts = (data.posts || []).slice().sort((a, b) => b.createdAt - a.createdAt);
            if (typeof data.uptime === 'number') { uptimeBase = data.uptime; uptimeAt = Date.now(); }
            renderActiveViews();
        }

        async function fetchState() {
            try {
                const res = await fetch('/api/state');
                if (!res.ok) return;
                const data = await res.json();
                applyState(data);
            } catch (e) { /* ignore */ }
        }

        function connectWS() {
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${proto}//${location.host}/ws?uid=${encodeURIComponent(localUid)}`);
            ws.onmessage = (e) => {
                try {
                    const msg = JSON.parse(e.data);
                    if (msg.type === 'state') {
                        applyState(msg);
                    } else if (msg.type === 'stats') {
                        if (typeof msg.uptime === 'number') { uptimeBase = msg.uptime; uptimeAt = Date.now(); }
                    }
                } catch (err) { /* ignore */ }
            };
            ws.onclose = () => setTimeout(connectWS, 2000);
            ws.onerror = () => { /* silent */ };
        }

        setInterval(() => {
            if (ws && ws.readyState === 1) ws.send('ping');
        }, 25000);

        window.onpopstate = () => checkUrlParams();

        function checkUrlParams() {
            const params = new URLSearchParams(window.location.search);
            const pId = params.get('p');
            if (pId) {
                window.switchTab('single', pId);
            } else if (document.getElementById('view-single').classList.contains('active')) {
                window.switchTab('feed');
            }
        }

        window.goHome = () => {
            window.history.pushState({}, '', window.location.pathname);
            window.switchTab('feed');
        };

        function renderActiveViews() {
            if (document.getElementById('view-feed').classList.contains('active')) renderFeed();
            if (document.getElementById('view-my').classList.contains('active')) renderMyPosts();
            if (document.getElementById('view-single').classList.contains('active')) {
                const params = new URLSearchParams(window.location.search);
                if (params.get('p')) {
                    const postId = params.get('p');
                    renderSinglePost(postId);
                    trackUniqueView(postId);
                }
            }
        }

        // -------- Счётчик символов --------
        function updateCharCounter() {
            const el = document.getElementById('post-text-input');
            const counter = document.getElementById('post-text-counter');
            const len = el.value.length;
            counter.textContent = `${len} / ${MAX_TEXT_LEN}`;
            counter.classList.toggle('limit', len >= MAX_TEXT_LEN);
        }
        document.getElementById('post-text-input').addEventListener('input', updateCharCounter);
        updateCharCounter();

        // -------- ASCII: обработка файла --------
        window.handleAsciiImage = (e) => {
            const file = e.target.files && e.target.files[0];
            e.target.value = '';
            if (!file) return;
            if (!file.type.startsWith('image/')) {
                return window.showToast('Нужен файл-изображение');
            }
            const reader = new FileReader();
            reader.onload = (ev) => {
                const img = new Image();
                img.onload = () => {
                    currentAsciiImage = img;
                    document.getElementById('ascii-controls').classList.remove('hidden');
                    document.getElementById('ascii-preview').classList.remove('hidden');
                    document.getElementById('ascii-clear-btn').classList.remove('hidden');
                    window.updateAsciiPreview();
                };
                img.onerror = () => window.showToast('Не удалось прочитать изображение');
                img.src = ev.target.result;
            };
            reader.onerror = () => window.showToast('Ошибка чтения файла');
            reader.readAsDataURL(file);
        };

        window.clearAscii = () => {
            currentAsciiImage = null;
            currentAsciiText = '';
            document.getElementById('ascii-controls').classList.add('hidden');
            document.getElementById('ascii-preview').classList.add('hidden');
            document.getElementById('ascii-clear-btn').classList.add('hidden');
            document.getElementById('ascii-preview').textContent = '';
        };

        // -------- ASCII: конвертер --------
        function imageToAscii(img, opts) {
            const {
                width = 80,
                charset = ASCII_CHARSETS.classic,
                contrast = 1.0,
                brightness = 0,
                invert = false,
                whiteBg = true,
            } = opts;

            // Символы моношрифта примерно в 2 раза выше, чем шириной — компенсируем
            const CHAR_ASPECT = 0.5;

            const targetW = Math.max(4, Math.min(300, Math.round(width)));
            const targetH = Math.max(2, Math.round((img.height / img.width) * targetW * CHAR_ASPECT));

            const canvas = document.createElement('canvas');
            canvas.width = targetW;
            canvas.height = targetH;
            const ctx = canvas.getContext('2d', { willReadFrequently: true });

            // Фон для прозрачных картинок
            if (whiteBg) {
                ctx.fillStyle = '#ffffff';
                ctx.fillRect(0, 0, targetW, targetH);
            }
            ctx.imageSmoothingEnabled = true;
            ctx.imageSmoothingQuality = 'high';
            ctx.drawImage(img, 0, 0, targetW, targetH);

            let data;
            try {
                data = ctx.getImageData(0, 0, targetW, targetH).data;
            } catch (err) {
                return null;
            }

            const N = charset.length;
            const lines = [];

            for (let y = 0; y < targetH; y++) {
                const row = new Array(targetW);
                for (let x = 0; x < targetW; x++) {
                    const i = (y * targetW + x) * 4;
                    const r = data[i], g = data[i + 1], b = data[i + 2];
                    // Luma Rec.601 (хорошо для восприятия)
                    let lum = 0.299 * r + 0.587 * g + 0.114 * b;

                    // Контраст вокруг 128
                    lum = (lum - 128) * contrast + 128;
                    // Яркость (в диапазоне -100..100 → -255..255 / 2)
                    lum += brightness * 1.275;
                    // Клиппинг
                    if (lum < 0) lum = 0;
                    else if (lum > 255) lum = 255;

                    // Инверсия
                    if (invert) lum = 255 - lum;

                    // Маппинг на набор символов.
                    // Набор упорядочен от "тёмного" к "светлому" (или наоборот).
                    // Классический " .:-=+*#%@" — пробел=светлое, @=тёмное.
                    let idx = Math.floor((lum / 256) * N);
                    if (idx < 0) idx = 0;
                    else if (idx >= N) idx = N - 1;

                    row[x] = charset[idx];
                }
                lines.push(row.join(''));
            }
            return lines.join('\n');
        }

        window.updateAsciiPreview = () => {
            if (!currentAsciiImage) return;
            const width = parseInt(document.getElementById('ascii-width').value, 10);
            const contrast = parseFloat(document.getElementById('ascii-contrast').value);
            const brightness = parseInt(document.getElementById('ascii-brightness').value, 10);
            const charsetKey = document.getElementById('ascii-charset').value;
            const invert = document.getElementById('ascii-invert').checked;
            const whiteBg = document.getElementById('ascii-white-bg').checked;

            document.getElementById('ascii-width-val').textContent = width;
            document.getElementById('ascii-contrast-val').textContent = contrast.toFixed(2);
            document.getElementById('ascii-brightness-val').textContent = brightness;

            const charset = ASCII_CHARSETS[charsetKey] || ASCII_CHARSETS.classic;

            const art = imageToAscii(currentAsciiImage, {
                width, charset, contrast, brightness, invert, whiteBg,
            });
            if (art === null) {
                window.showToast('Не удалось получить пиксели изображения');
                return;
            }
            currentAsciiText = art;
            const preview = document.getElementById('ascii-preview');
            preview.textContent = art;
            const lines = art.split('\n');
            document.getElementById('ascii-size').textContent =
                `${lines[0].length}×${lines.length} (${art.length} симв.)`;
        };

        // Слушатели слайдеров
        ['ascii-width','ascii-contrast','ascii-brightness'].forEach(id => {
            document.getElementById(id).addEventListener('input', window.updateAsciiPreview);
        });
        ['ascii-charset','ascii-invert','ascii-white-bg'].forEach(id => {
            document.getElementById(id).addEventListener('change', window.updateAsciiPreview);
        });

        // -------- ASCII viewer --------
        let asciiFontSize = 10;
        let asciiOriginalText = '';

        window.openAsciiViewer = (text) => {
            asciiOriginalText = text;
            const modal = document.getElementById('ascii-modal');
            const content = document.getElementById('ascii-modal-content');
            content.textContent = text;
            asciiFontSize = 10;
            content.style.fontSize = asciiFontSize + 'px';
            modal.classList.remove('hidden');
            document.body.style.overflow = 'hidden';
        };
        window.closeAsciiViewer = () => {
            document.getElementById('ascii-modal').classList.add('hidden');
            document.body.style.overflow = '';
        };
        window.asciiZoom = (delta) => {
            asciiFontSize = Math.max(3, Math.min(40, asciiFontSize + delta * 1.5));
            document.getElementById('ascii-modal-content').style.fontSize = asciiFontSize + 'px';
        };
        window.asciiCopy = async () => {
            try {
                await navigator.clipboard.writeText(asciiOriginalText);
                window.showToast('ASCII скопирован!');
            } catch (e) {
                window.showToast('Не удалось скопировать');
            }
        };
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && !document.getElementById('ascii-modal').classList.contains('hidden')) {
                window.closeAsciiViewer();
            }
        });

        // -------- Создание поста --------
        window.createPost = async () => {
            const title = document.getElementById('post-title-input').value.trim();
            const textEl = document.getElementById('post-text-input');
            const text = textEl.value.trim();
            const visibility = document.getElementById('post-visibility').value;
            const asciiArt = currentAsciiText || '';

            if (!title) return window.showToast('Введите заголовок!');
            if (!text && !asciiArt) return window.showToast('Напишите что-нибудь!');
            if (text.length > MAX_TEXT_LEN) return window.showToast(`Максимум ${MAX_TEXT_LEN} символов`);

            try {
                const res = await fetch('/api/posts', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ title, text, visibility, ownerId: localUid, asciiArt })
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    return window.showToast(err.detail || 'Ошибка публикации');
                }
                const post = await res.json();
                document.getElementById('post-title-input').value = '';
                textEl.value = '';
                updateCharCounter();
                window.clearAscii();

                if (visibility === 'unlisted') {
                    window.showToast('Скрытый пост создан!');
                    window.history.pushState({}, '', `?p=${post.id}`);
                    window.switchTab('single', post.id);
                } else {
                    window.showToast('Опубликовано!');
                    window.switchTab('feed');
                }
            } catch (e) {
                window.showToast('Ошибка соединения');
            }
        };

        async function trackUniqueView(postId) {
            if (trackedViews.has(postId)) return;
            trackedViews.add(postId);
            try {
                await fetch(`/api/posts/${postId}/view?uid=${encodeURIComponent(localUid)}`, { method: 'POST' });
            } catch (e) { /* ignore */ }
        }

        window.ratePost = async (postId, value) => {
            try {
                await fetch(`/api/posts/${postId}/rate`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ value, uid: localUid })
                });
            } catch (e) { /* ignore */ }
        };

        window.deletePost = async (postId) => {
            if (!confirm('Удалить этот пост?')) return;
            try {
                const res = await fetch(`/api/posts/${postId}?uid=${encodeURIComponent(localUid)}`, { method: 'DELETE' });
                if (!res.ok) throw new Error();
                window.showToast('Удалено');
                if (document.getElementById('view-single').classList.contains('active')) window.goHome();
            } catch (e) {
                window.showToast('Не удалось удалить');
            }
        };

        window.copyLink = (postId) => {
            const link = window.location.origin + window.location.pathname + '?p=' + postId;
            const el = document.createElement('textarea');
            el.value = link; document.body.appendChild(el); el.select();
            try { document.execCommand('copy'); } catch (e) {}
            document.body.removeChild(el);
            window.showToast('Ссылка скопирована!');
        };

        window.toggleComments = (postId) => {
            const el = document.getElementById(`comments-sec-${postId}`);
            if (el) el.classList.toggle('open');
        };

        window.addComment = async (postId) => {
            const textInput = document.getElementById(`comm-text-${postId}`);
            const text = textInput.value.trim();
            if (!text) return window.showToast('Напишите текст комментария.');
            try {
                const res = await fetch(`/api/posts/${postId}/comments`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text, uid: localUid })
                });
                if (res.ok) textInput.value = '';
            } catch (e) { /* ignore */ }
        };

        window.deleteComment = async (postId, commId) => {
            if (!confirm('Удалить комментарий?')) return;
            try {
                await fetch(`/api/posts/${postId}/comments/${commId}?uid=${encodeURIComponent(localUid)}`, { method: 'DELETE' });
            } catch (e) { /* ignore */ }
        };

        function renderFeed() {
            const list = document.getElementById('feed-list');
            list.innerHTML = '';
            const publicPosts = allPosts.filter(p => p.visibility === 'public');
            if (publicPosts.length === 0) {
                list.innerHTML = '<div style="text-align:center; padding: 20px; opacity:0.6;">Лента пока пуста.</div>';
                return;
            }
            publicPosts.forEach(post => list.appendChild(createPostElement(post)));
        }

        function renderMyPosts() {
            const list = document.getElementById('my-posts-list');
            list.innerHTML = '';
            const myPosts = allPosts.filter(p => p.ownerId === localUid);
            if (myPosts.length === 0) {
                list.innerHTML = '<div style="text-align:center; padding: 20px; opacity:0.6;">У вас пока нет публикаций.</div>';
                return;
            }
            myPosts.forEach(post => list.appendChild(createPostElement(post)));
        }

        function renderSinglePost(postId) {
            const container = document.getElementById('single-post-container');
            const post = allPosts.find(p => p.id === postId);
            container.innerHTML = '';
            if (!post) {
                container.innerHTML = '<div class="card">Запись удалена или не существует.</div>';
                return;
            }
            const el = createPostElement(post);
            const commSec = el.querySelector('.comments-section');
            if (commSec) commSec.classList.add('open');
            container.appendChild(el);
        }

        function createPostElement(post) {
            const div = document.createElement('div');
            div.className = 'card'; div.id = `post-${post.id}`;
            const ratingUsers = post.ratingUsers || {};
            const myRating = ratingUsers[localUid] || 0;
            const isOwner = post.ownerId === localUid;
            const isUnlisted = post.visibility === 'unlisted';
            const uniqueViewCount = Object.keys(post.viewedUsers || {}).length;
            const commentsCount = (post.comments || []).length;

            let html = `
                <div class="post-author-row">
                    <div>
                        <span class="author-name">${escapeHtml(post.author)}</span>
                        • <span class="post-time">${new Date(post.createdAt).toLocaleDateString()}</span>
                    </div>
                    ${isOwner ? `<button class="text-btn" style="color:var(--error); padding: 2px 8px; font-size: 0.75rem;" onclick="window.deletePost('${post.id}')">[Удалить]</button>` : ''}
                </div>
                <div class="post-title">${escapeHtml(post.title)}</div>
            `;

            if (post.asciiArt) {
                // Store raw ascii in a data attribute for the viewer
                const id = 'ascii-' + post.id;
                html += `<pre class="ascii-art" id="${id}" data-ascii-id="${post.id}" onclick="window.openAsciiViewer(document.getElementById('${id}').textContent)">${escapeHtml(post.asciiArt)}</pre>`;
            }

            if (post.text) html += `<div class="post-text">${renderRichText(post.text)}</div>`;

            html += `
                <div class="post-footer">
                    ${isUnlisted ? `
                        <span style="font-size:0.8rem; font-weight:bold; opacity:0.7;">
                            [Просмотров: ${uniqueViewCount}]
                        </span>
                    ` : `
                        <div class="action-group">
                            <button class="action-btn ${myRating === 1 ? 'active' : ''}" onclick="window.ratePost('${post.id}', 1)">
                                [Лайков: ${post.upvotes || 0}]
                            </button>
                            <button class="action-btn ${myRating === -1 ? 'active' : ''}" onclick="window.ratePost('${post.id}', -1)">
                                [Дизлайков: ${post.downvotes || 0}]
                            </button>
                        </div>
                    `}
                    <div class="action-group">
                        <button class="action-btn" onclick="window.toggleComments('${post.id}')">
                            [Комментариев: ${commentsCount}]
                        </button>
                        <button class="action-btn" onclick="window.copyLink('${post.id}')" title="Поделиться">
                            [${isUnlisted ? 'Код: ' + post.code : 'Ссылка'}]
                        </button>
                    </div>
                </div>
                <div class="comments-section" id="comments-sec-${post.id}">
                    <div style="margin-top:8px;">
            `;

            if (post.comments && post.comments.length > 0) {
                post.comments.forEach(c => {
                    const isCommentOwner = c.ownerId === localUid;
                    html += `
                        <div class="comment-item">
                            <div class="comment-header">
                                <span>${escapeHtml(c.author)}</span>
                                <div>
                                    <span style="opacity:0.6; font-weight:normal; font-size:0.75rem;">${new Date(c.createdAt).toLocaleDateString()}</span>
                                    ${isCommentOwner || isOwner ? `<span style="color:var(--error); margin-left:6px; cursor:pointer;" onclick="window.deleteComment('${post.id}', '${c.id}')">[Удалить]</span>` : ''}
                                </div>
                            </div>
                            <div class="comment-text">${renderRichText(c.text)}</div>
                        </div>
                    `;
                });
            } else {
                html += `<p style="font-size:0.8rem; opacity:0.6; margin-bottom:8px; padding-left:2px;">Пока пусто.</p>`;
            }

            html += `
                    </div>
                    <div class="comment-form">
                        <input type="text" id="comm-text-${post.id}" placeholder="Ваш комментарий..." maxlength="200" required>
                        <button class="btn btn-tonal" style="padding:6px; font-size:0.8rem;" onclick="window.addComment('${post.id}')">Отправить</button>
                    </div>
                </div>
            `;

            div.innerHTML = html;
            return div;
        }

        window.switchTab = (tabId, payload = null) => {
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.getElementById(`view-${tabId}`).classList.add('active');

            if (tabId !== 'single') {
                const btnIndex = tabId === 'feed' ? 'btn-tab-feed' : tabId === 'create' ? 'btn-tab-create' : 'btn-tab-my';
                document.getElementById(btnIndex).classList.add('active');
                document.title = 'sldchat — Блог-платформа';
            }

            if (tabId === 'feed') renderFeed();
            if (tabId === 'my') renderMyPosts();
            if (tabId === 'single' && payload) {
                renderSinglePost(payload);
                trackUniqueView(payload);
                const p = allPosts.find(x => x.id === payload);
                if (p) document.title = `${p.title} — sldchat`;
            }

            window.scrollTo({ top: 0, behavior: 'auto' });
        };

        window.showToast = (msg) => {
            const t = document.getElementById('toast');
            t.innerText = msg; t.classList.add('show');
            setTimeout(() => t.classList.remove('show'), 2500);
        };

        async function init() {
            await fetchState();
            connectWS();
            checkUrlParams();
        }
        init();
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


# ----------------------------------------------------------------------------
# Точка входа
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
