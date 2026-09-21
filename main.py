# main.py
# Запуск:  pip install fastapi uvicorn
#          uvicorn main:app --reload
# Открыть: http://127.0.0.1:8000

import time
import uuid
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="AnonSocial")

# ------------------------------------------------------------------
# "База данных" в оперативной памяти
# ------------------------------------------------------------------
POSTS: List[dict] = []
MAX_LEN = 1000


# ------------------------------------------------------------------
# Модели
# ------------------------------------------------------------------
class PostIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_LEN)


class PostOut(BaseModel):
    id: str
    text: str
    created_at: float


# ------------------------------------------------------------------
# API
# ------------------------------------------------------------------
@app.get("/api/posts")
def get_posts(q: Optional[str] = None):
    """Список постов. Если q задан — поиск по подстроке (без регистра)."""
    items = POSTS
    if q:
        needle = q.strip().lower()
        if needle:
            items = [p for p in items if needle in p["text"].lower()]
    # свежие сверху
    items = sorted(items, key=lambda p: p["created_at"], reverse=True)
    return {"count": len(items), "posts": items}


@app.post("/api/posts")
def create_post(payload: PostIn):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустой пост")
    if len(text) > MAX_LEN:
        raise HTTPException(status_code=400, detail=f"Максимум {MAX_LEN} символов")
    post = {
        "id": uuid.uuid4().hex,
        "text": text,
        "created_at": time.time(),
    }
    POSTS.append(post)
    return JSONResponse(post, status_code=201)


# ------------------------------------------------------------------
# Фронтенд (одна страница)
# ------------------------------------------------------------------
PAGE = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AnonSocial</title>
<style>
  :root {
    --bg: #e9eef3;
    --card: #ffffff;
    --line: #d3dbe3;
    --muted: #7b8794;
    --accent: #4a76a8;
    --accent-dark: #3b6089;
    --text: #2c3844;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    display: flex;
    justify-content: center;
  }
  .app {
    width: 100%;
    max-width: 620px;
    height: 100vh;
    display: flex;
    flex-direction: column;
    background: var(--card);
    border-left: 1px solid var(--line);
    border-right: 1px solid var(--line);
  }

  /* Шапка с поиском */
  header {
    padding: 10px 14px;
    border-bottom: 1px solid var(--line);
    background: var(--accent);
    color: #fff;
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 0 0 auto;
  }
  header .logo {
    font-weight: 700;
    font-size: 17px;
    letter-spacing: .3px;
    white-space: nowrap;
  }
  header input[type=search] {
    flex: 1;
    padding: 8px 12px;
    border-radius: 16px;
    border: none;
    outline: none;
    font-size: 14px;
    background: rgba(255,255,255,.92);
    color: #222;
  }
  header input[type=search]:focus { background: #fff; }

  /* Лента */
  main {
    flex: 1 1 auto;
    overflow-y: auto;
    padding: 12px;
    background: var(--bg);
  }
  .empty {
    text-align: center;
    color: var(--muted);
    margin-top: 40px;
    font-size: 14px;
  }
  .post {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 10px;
    box-shadow: 0 1px 2px rgba(0,0,0,.04);
    animation: fade .18s ease-out;
  }
  @keyframes fade {
    from { opacity: 0; transform: translateY(-4px); }
    to   { opacity: 1; transform: none; }
  }
  .post .meta {
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 6px;
    display: flex;
    justify-content: space-between;
  }
  .post .text {
    font-size: 15px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-wrap: break-word;
    overflow-wrap: anywhere;
  }

  /* Подвал с созданием поста */
  footer {
    flex: 0 0 auto;
    border-top: 1px solid var(--line);
    background: var(--card);
    padding: 10px 12px;
  }
  footer textarea {
    width: 100%;
    min-height: 60px;
    max-height: 160px;
    resize: vertical;
    padding: 10px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-family: inherit;
    font-size: 14px;
    outline: none;
    color: var(--text);
  }
  footer textarea:focus { border-color: var(--accent); }
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 8px;
  }
  .counter { font-size: 12px; color: var(--muted); }
  .counter.warn { color: #c0392b; font-weight: 600; }
  button.send {
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 8px 18px;
    border-radius: 6px;
    font-size: 14px;
    cursor: pointer;
    transition: background .15s;
  }
  button.send:hover { background: var(--accent-dark); }
  button.send:disabled { background: #a9b6c4; cursor: default; }
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="logo">AnonSocial</div>
    <input id="search" type="search" placeholder="Поиск по постам..." autocomplete="off" />
  </header>

  <main id="feed">
    <div class="empty">Загрузка...</div>
  </main>

  <footer>
    <textarea id="newPost" maxlength="1000" placeholder="Что нового? (до 1000 символов)"></textarea>
    <div class="row">
      <div id="counter" class="counter">0 / 1000</div>
      <button id="send" class="send" disabled>Опубликовать</button>
    </div>
  </footer>
</div>

<script>
  const feed     = document.getElementById('feed');
  const searchEl = document.getElementById('search');
  const inputEl  = document.getElementById('newPost');
  const sendBtn  = document.getElementById('send');
  const counter  = document.getElementById('counter');
  const MAX = 1000;

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    })[c]);
  }

  function timeAgo(ts) {
    const d = Math.floor(Date.now()/1000 - ts);
    if (d < 5)     return 'только что';
    if (d < 60)    return d + ' с назад';
    const m = Math.floor(d/60);
    if (m < 60)    return m + ' мин назад';
    const h = Math.floor(m/60);
    if (h < 24)    return h + ' ч назад';
    const days = Math.floor(h/24);
    if (days < 30) return days + ' дн назад';
    return new Date(ts*1000).toLocaleDateString();
  }

  function render(posts) {
    if (!posts.length) {
      feed.innerHTML = '<div class="empty">Пока нет постов. Будьте первым!</div>';
      return;
    }
    feed.innerHTML = posts.map(p => `
      <div class="post">
        <div class="meta">
          <span>Аноним</span>
          <span>${timeAgo(p.created_at)}</span>
        </div>
        <div class="text">${escapeHtml(p.text)}</div>
      </div>
    `).join('');
  }

  async function load() {
    const q = searchEl.value.trim();
    const url = '/api/posts' + (q ? '?q=' + encodeURIComponent(q) : '');
    try {
      const r = await fetch(url);
      const data = await r.json();
      render(data.posts);
    } catch (e) {
      feed.innerHTML = '<div class="empty">Ошибка загрузки :(</div>';
    }
  }

  function updateCounter() {
    const len = inputEl.value.length;
    counter.textContent = len + ' / ' + MAX;
    counter.classList.toggle('warn', len >= MAX);
    sendBtn.disabled = len === 0 || len > MAX;
  }

  async function send() {
    const text = inputEl.value.trim();
    if (!text) return;
    sendBtn.disabled = true;
    try {
      const r = await fetch('/api/posts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text})
      });
      if (!r.ok) {
        const err = await r.json().catch(()=>({}));
        alert(err.detail || 'Ошибка публикации');
      } else {
        inputEl.value = '';
        updateCounter();
        searchEl.value = '';   // чтобы точно увидеть свой пост
        await load();
        feed.scrollTop = 0;
      }
    } catch (e) {
      alert('Сеть недоступна');
    } finally {
      updateCounter();
    }
  }

  // автообновление
  setInterval(() => { if (!document.hidden) load(); }, 4000);
  // поиск с задержкой
  let tId;
  searchEl.addEventListener('input', () => {
    clearTimeout(tId);
    tId = setTimeout(load, 250);
  });
  inputEl.addEventListener('input', updateCounter);
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
  });
  sendBtn.addEventListener('click', send);

  updateCounter();
  load();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE
