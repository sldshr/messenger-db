"""
Закрытый постер — админ-панель.
FastAPI + Uvicorn, всё хранится в оперативной памяти (при перезапуске данные сбрасываются).

Установка:
    pip install fastapi uvicorn python-multipart

Запуск:
    python app.py

Админка:  http://127.0.0.1:8000/admin
Публичная страница: http://127.0.0.1:8000/
Логин/пароль по умолчанию: admin / admin
"""

import html
import secrets
from datetime import datetime
from typing import List, Optional

import uvicorn
from fastapi import Cookie, Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

# ============================== НАСТРОЙКИ ==============================
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin"
SESSION_TTL = 60 * 60 * 24  # 24 часа

# ============================== ХРАНИЛИЩЕ ==============================
sessions: dict[str, str] = {}   # token -> username
posts: List[dict] = []          # список постов
_id_seq = 0


def next_id() -> int:
    global _id_seq
    _id_seq += 1
    return _id_seq


def e(value) -> str:
    """HTML-экранирование."""
    return html.escape(str(value))


def now_str() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M")


# ============================== МОДЕЛИ ==============================
class PostIn(BaseModel):
    title: str
    content: str
    author: str = "admin"
    published: bool = True
    locked: bool = False


# ============================== АВТОРИЗАЦИЯ ==============================
def current_user(session: Optional[str] = Cookie(default=None)) -> Optional[str]:
    if session and session in sessions:
        return sessions[session]
    return None


def require_user(session: Optional[str] = Cookie(default=None)) -> str:
    user = current_user(session)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


app = FastAPI(title="Closed Poster Admin")


# ============================== HTML: СТРАНИЦА ВХОДА ==============================
LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — Админ-панель</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: radial-gradient(1200px 600px at 50% -10%, #1b2233 0%, #0f1117 60%);
    color: #e6e8ee; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 20px;
  }
  .card {
    background: #171a23; padding: 40px; border-radius: 16px;
    border: 1px solid #262b38; width: 100%; max-width: 380px;
    box-shadow: 0 20px 60px rgba(0,0,0,.55);
  }
  .logo { display: flex; align-items: center; gap: 10px; margin-bottom: 22px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #2ecc71; box-shadow: 0 0 10px #2ecc71; }
  .logo span { font-size: 13px; color: #8b93a7; letter-spacing: .4px; text-transform: uppercase; }
  h1 { font-size: 22px; margin-bottom: 6px; }
  p.sub { color: #8b93a7; font-size: 13px; margin-bottom: 24px; }
  label { display: block; font-size: 11px; color: #8b93a7; margin-bottom: 6px;
          text-transform: uppercase; letter-spacing: .6px; }
  input {
    width: 100%; padding: 12px 14px; background: #0f1117; border: 1px solid #262b38;
    border-radius: 8px; color: #e6e8ee; font-size: 14px; margin-bottom: 16px;
    outline: none; transition: border-color .2s;
  }
  input:focus { border-color: #4f7cff; }
  button {
    width: 100%; padding: 12px; background: #4f7cff; color: #fff; border: none;
    border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; transition: .2s;
  }
  button:hover { background: #3d68e8; }
  .error {
    color: #ff8080; background: #2a1717; border: 1px solid #3a2222;
    padding: 10px 12px; border-radius: 8px; font-size: 13px; margin-bottom: 16px;
  }
</style>
</head>
<body>
  <form class="card" method="post" action="/admin/login">
    <div class="logo"><div class="dot"></div><span>Закрытый постер</span></div>
    <h1>Вход</h1>
    <p class="sub">Панель управления сообществом</p>
    {{ERROR}}
    <label>Логин</label>
    <input name="username" autocomplete="username" autofocus required>
    <label>Пароль</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Войти</button>
  </form>
</body>
</html>
"""


# ============================== HTML: АДМИН-ПАНЕЛЬ ==============================
ADMIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Админ-панель — Закрытый постер</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:#0f1117; --panel:#171a23; --panel2:#1d2130; --border:#262b38;
    --text:#e6e8ee; --muted:#8b93a7; --accent:#4f7cff; --danger:#ff5c5c; --ok:#2ecc71;
  }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         background:var(--bg); color:var(--text); min-height:100vh; }
  header {
    display:flex; justify-content:space-between; align-items:center; gap:16px;
    padding:14px 28px; border-bottom:1px solid var(--border); background:rgba(23,26,35,.9);
    backdrop-filter:blur(8px); position:sticky; top:0; z-index:10;
  }
  .logo { display:flex; align-items:center; gap:10px; font-weight:600; font-size:14px; }
  .dot { width:8px; height:8px; border-radius:50%; background:var(--ok); box-shadow:0 0 8px var(--ok); }
  main { max-width:960px; margin:0 auto; padding:28px 20px 60px; }
  .toolbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; gap:12px; }
  h2 { font-size:18px; }
  button { font-family:inherit; font-size:14px; cursor:pointer; border:none;
           border-radius:8px; padding:10px 16px; font-weight:600; transition:.2s; }
  .btn-primary { background:var(--accent); color:#fff; }
  .btn-primary:hover { background:#3d68e8; }
  .btn-ghost { background:transparent; color:var(--muted); border:1px solid var(--border); }
  .btn-ghost:hover { color:var(--text); border-color:#3a4152; }
  .btn-danger { background:transparent; color:var(--danger); border:1px solid #3a2222; }
  .btn-danger:hover { background:#2a1717; }
  .btn-sm { padding:6px 12px; font-size:12px; }
  .posts { display:flex; flex-direction:column; gap:12px; }
  .post { background:var(--panel); border:1px solid var(--border); border-radius:12px;
          padding:18px 20px; display:flex; justify-content:space-between; gap:16px; }
  .post h3 { font-size:15px; margin-bottom:8px; }
  .post p { color:var(--muted); font-size:13px; line-height:1.55; white-space:pre-wrap;
            overflow:hidden; }
  .meta { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }
  .tag { font-size:11px; padding:3px 9px; border-radius:20px; background:var(--panel2);
         color:var(--muted); border:1px solid var(--border); }
  .tag.ok { color:#7ee2a8; border-color:#1f3a2c; background:#14251c; }
  .tag.warn { color:#ffcf70; border-color:#3a3220; background:#241f14; }
  .actions { display:flex; gap:8px; flex-shrink:0; align-items:flex-start; }
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.65); backdrop-filter:blur(4px);
           display:none; align-items:center; justify-content:center; padding:20px; z-index:100; }
  .modal.open { display:flex; }
  .modal-card { background:var(--panel); border:1px solid var(--border); border-radius:16px;
                width:100%; max-width:520px; padding:26px; max-height:90vh; overflow:auto; }
  .modal-card h3 { margin-bottom:18px; font-size:17px; }
  label { display:block; font-size:11px; color:var(--muted); margin-bottom:6px;
          text-transform:uppercase; letter-spacing:.6px; }
  input[type=text], textarea {
    width:100%; padding:11px 13px; background:var(--bg); border:1px solid var(--border);
    border-radius:8px; color:var(--text); font-size:14px; font-family:inherit;
    margin-bottom:14px; outline:none; transition:border-color .2s;
  }
  input[type=text]:focus, textarea:focus { border-color:var(--accent); }
  textarea { resize:vertical; min-height:110px; }
  .checks { display:flex; gap:18px; margin-bottom:20px; flex-wrap:wrap; }
  .check { display:flex; align-items:center; gap:8px; font-size:13px; color:var(--muted);
           cursor:pointer; text-transform:none; letter-spacing:0; margin:0; }
  .check input { accent-color:var(--accent); width:16px; height:16px; }
  .modal-actions { display:flex; gap:10px; justify-content:flex-end; }
  .empty { text-align:center; padding:60px 20px; color:var(--muted);
           border:1px dashed var(--border); border-radius:12px; font-size:14px; }
  .toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%) translateY(80px);
           background:var(--panel2); border:1px solid var(--border); padding:12px 22px;
           border-radius:10px; font-size:13px; opacity:0; transition:.3s; pointer-events:none; }
  .toast.show { transform:translateX(-50%) translateY(0); opacity:1; }
</style>
</head>
<body>
  <header>
    <div class="logo"><span class="dot"></span> Закрытый постер · Админ</div>
    <a href="/admin/logout" style="text-decoration:none">
      <button class="btn-ghost btn-sm" type="button">Выйти</button>
    </a>
  </header>

  <main>
    <div class="toolbar">
      <h2>Посты</h2>
      <button class="btn-primary" onclick="openModal()">+ Новый пост</button>
    </div>
    <div class="posts" id="posts"></div>
  </main>

  <div class="modal" id="modal">
    <div class="modal-card">
      <h3 id="modalTitle">Новый пост</h3>
      <form id="postForm">
        <input type="hidden" id="postId">
        <label>Заголовок</label>
        <input type="text" id="title" required>
        <label>Автор</label>
        <input type="text" id="author" value="admin">
        <label>Содержимое</label>
        <textarea id="content" required></textarea>
        <div class="checks">
          <label class="check"><input type="checkbox" id="published" checked> Опубликован</label>
          <label class="check"><input type="checkbox" id="locked"> Только для участников</label>
        </div>
        <div class="modal-actions">
          <button type="button" class="btn-ghost" onclick="closeModal()">Отмена</button>
          <button type="submit" class="btn-primary">Сохранить</button>
        </div>
      </form>
    </div>
  </div>

  <div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let posts = [];

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

async function load() {
  const r = await fetch('/api/posts');
  if (r.status === 401) { location.reload(); return; }
  posts = await r.json();
  render();
}

function render() {
  const box = $('posts');
  if (!posts.length) {
    box.innerHTML = '<div class="empty">Пока нет постов. Создайте первый.</div>';
    return;
  }
  box.innerHTML = posts.map(p => `
    <div class="post">
      <div style="min-width:0">
        <h3>${esc(p.title)}</h3>
        <p>${esc(p.content.slice(0, 220))}${p.content.length > 220 ? '…' : ''}</p>
        <div class="meta">
          <span class="tag">#${p.id}</span>
          <span class="tag">${esc(p.author)}</span>
          <span class="tag">${esc(p.created_at)}</span>
          <span class="tag ${p.published ? 'ok' : 'warn'}">${p.published ? 'опубликован' : 'черновик'}</span>
          ${p.locked ? '<span class="tag warn">🔒 для участников</span>' : ''}
        </div>
      </div>
      <div class="actions">
        <button class="btn-ghost btn-sm" onclick="edit(${p.id})">Изменить</button>
        <button class="btn-danger btn-sm" onclick="del(${p.id})">Удалить</button>
      </div>
    </div>
  `).join('');
}

function openModal(p) {
  $('modalTitle').textContent = p ? 'Редактировать пост' : 'Новый пост';
  $('postId').value = p ? p.id : '';
  $('title').value = p ? p.title : '';
  $('author').value = p ? p.author : 'admin';
  $('content').value = p ? p.content : '';
  $('published').checked = p ? p.published : true;
  $('locked').checked = p ? p.locked : false;
  $('modal').classList.add('open');
  setTimeout(() => $('title').focus(), 50);
}

function closeModal() { $('modal').classList.remove('open'); }
function edit(id) { openModal(posts.find(p => p.id === id)); }

async function del(id) {
  if (!confirm('Удалить пост #' + id + '?')) return;
  await fetch('/api/posts/' + id, { method: 'DELETE' });
  toast('Удалено');
  load();
}

$('postForm').addEventListener('submit', async ev => {
  ev.preventDefault();
  const id = $('postId').value;
  const body = {
    title: $('title').value,
    author: $('author').value || 'admin',
    content: $('content').value,
    published: $('published').checked,
    locked: $('locked').checked
  };
  const url = id ? '/api/posts/' + id : '/api/posts';
  const method = id ? 'PUT' : 'POST';
  const r = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (r.ok) {
    closeModal();
    toast(id ? 'Сохранено' : 'Создано');
    load();
  } else {
    toast('Ошибка сохранения');
  }
});

$('modal').addEventListener('click', ev => { if (ev.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', ev => { if (ev.key === 'Escape') closeModal(); });

load();
</script>
</body>
</html>
"""


# ============================== ПУБЛИЧНАЯ СТРАНИЦА ==============================
def render_public(user: Optional[str]) -> str:
    visible = [p for p in sorted(posts, key=lambda x: x["id"], reverse=True) if p["published"]]

    cards = []
    for p in visible:
        hidden = p["locked"] and not user
        lock_tag = '<span class="tag warn">🔒 участники</span>' if p["locked"] else ""
        body_text = "🔒 Содержимое доступно только участникам сообщества." if hidden else e(p["content"])
        body_cls = "body locked" if hidden else "body"
        cards.append(f"""
        <article class="post">
          <div class="meta">
            <span class="tag">#{p['id']}</span>
            <span class="tag">{e(p['author'])}</span>
            <span class="tag">{e(p['created_at'])}</span>
            {lock_tag}
          </div>
          <h2>{e(p['title'])}</h2>
          <p class="{body_cls}">{body_text}</p>
        </article>""")

    if not cards:
        cards.append('<div class="empty">Пока нет публикаций.</div>')

    status = (
        f'<span class="who">Вы вошли как <b>{e(user)}</b> · <a href="/admin">админка</a> · '
        f'<a href="/admin/logout">выйти</a></span>'
        if user else
        '<span class="who">Гостевой доступ · <a href="/admin">вход для админа</a></span>'
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Закрытый постер</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: radial-gradient(1000px 500px at 50% -10%, #1b2233 0%, #0f1117 55%);
    color: #e6e8ee; min-height: 100vh;
  }}
  header {{
    max-width: 760px; margin: 0 auto; padding: 32px 20px 12px;
    display: flex; justify-content: space-between; align-items: baseline; gap: 16px; flex-wrap: wrap;
  }}
  h1 {{ font-size: 24px; letter-spacing: -0.3px; }}
  .who {{ font-size: 12px; color: #8b93a7; }}
  .who a {{ color: #4f7cff; text-decoration: none; }}
  .who a:hover {{ text-decoration: underline; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 16px 20px 80px;
          display: flex; flex-direction: column; gap: 14px; }}
  .post {{
    background: #171a23; border: 1px solid #262b38; border-radius: 14px; padding: 22px 24px;
  }}
  .post h2 {{ font-size: 17px; margin: 12px 0 10px; }}
  .body {{ color: #a9b1c4; font-size: 14px; line-height: 1.65; white-space: pre-wrap; }}
  .body.locked {{ color: #ffcf70; font-style: italic; }}
  .meta {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .tag {{ font-size: 11px; padding: 3px 9px; border-radius: 20px; background: #1d2130;
          color: #8b93a7; border: 1px solid #262b38; }}
  .tag.warn {{ color: #ffcf70; border-color: #3a3220; background: #241f14; }}
  .empty {{ text-align: center; padding: 60px 20px; color: #8b93a7;
            border: 1px dashed #262b38; border-radius: 12px; }}
</style>
</head>
<body>
  <header>
    <h1>Закрытый постер</h1>
    {status}
  </header>
  <main>{''.join(cards)}</main>
</body>
</html>"""


# ============================== РОУТЫ ==============================
@app.get("/", response_class=HTMLResponse)
def public_index(session: Optional[str] = Cookie(default=None)):
    return HTMLResponse(render_public(current_user(session)))


@app.get("/admin", response_class=HTMLResponse)
def admin_page(session: Optional[str] = Cookie(default=None)):
    if not current_user(session):
        return HTMLResponse(LOGIN_HTML.replace("{{ERROR}}", ""))
    return HTMLResponse(ADMIN_HTML)


@app.post("/admin/login")
def admin_login(username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        token = secrets.token_urlsafe(32)
        sessions[token] = username
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(
            "session", token,
            httponly=True, samesite="lax", max_age=SESSION_TTL,
        )
        return resp
    error = '<div class="error">Неверный логин или пароль</div>'
    return HTMLResponse(LOGIN_HTML.replace("{{ERROR}}", error), status_code=401)


@app.get("/admin/logout")
def admin_logout(session: Optional[str] = Cookie(default=None)):
    if session and session in sessions:
        sessions.pop(session, None)
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie("session")
    return resp


# ------------------------------ API ------------------------------
@app.get("/api/posts")
def api_list(user: str = Depends(require_user)):
    return sorted(posts, key=lambda p: p["id"], reverse=True)


@app.post("/api/posts", status_code=201)
def api_create(data: PostIn, user: str = Depends(require_user)):
    ts = now_str()
    post = {
        "id": next_id(),
        "title": data.title.strip(),
        "content": data.content.strip(),
        "author": (data.author or user).strip(),
        "published": bool(data.published),
        "locked": bool(data.locked),
        "created_at": ts,
        "updated_at": ts,
    }
    posts.append(post)
    return post


@app.put("/api/posts/{pid}")
def api_update(pid: int, data: PostIn, user: str = Depends(require_user)):
    for p in posts:
        if p["id"] == pid:
            p["title"] = data.title.strip()
            p["content"] = data.content.strip()
            p["author"] = (data.author or user).strip()
            p["published"] = bool(data.published)
            p["locked"] = bool(data.locked)
            p["updated_at"] = now_str()
            return p
    raise HTTPException(status_code=404, detail="Post not found")


@app.delete("/api/posts/{pid}")
def api_delete(pid: int, user: str = Depends(require_user)):
    for i, p in enumerate(posts):
        if p["id"] == pid:
            posts.pop(i)
            return {"ok": True, "deleted": pid}
    raise HTTPException(status_code=404, detail="Post not found")


# ============================== СТАРТОВЫЕ ДАННЫЕ ==============================
def seed():
    ts = now_str()
    posts.append({
        "id": next_id(),
        "title": "Добро пожаловать в закрытый постер",
        "content": (
            "Это закрытое сообщество. Здесь публикуются анонсы, материалы и обсуждения.\n\n"
            "Часть постов помечена как «только для участников» — их содержимое видят "
            "лишь авторизованные пользователи."
        ),
        "author": "admin",
        "published": True,
        "locked": False,
        "created_at": ts,
        "updated_at": ts,
    })
    posts.append({
        "id": next_id(),
        "title": "Материалы для участников (закрытый доступ)",
        "content": (
            "Этот пост доступен только участникам сообщества.\n\n"
            "Здесь может быть внутренняя информация, ссылки, документы и т.п."
        ),
        "author": "admin",
        "published": True,
        "locked": True,
        "created_at": ts,
        "updated_at": ts,
    })
    posts.append({
        "id": next_id(),
        "title": "Черновик: следующий анонс",
        "content": "Этот пост ещё не опубликован и виден только в админ-панели.",
        "author": "admin",
        "published": False,
        "locked": False,
        "created_at": ts,
        "updated_at": ts,
    })


seed()


# ============================== ЗАПУСК ==============================
if __name__ == "__main__":
    print("=" * 56)
    print("  Закрытый постер · Админ-панель")
    print("  Публичная:  http://127.0.0.1:8000/")
    print("  Админка:    http://127.0.0.1:8000/admin")
    print(f"  Логин/пароль: {ADMIN_USERNAME} / {ADMIN_PASSWORD}")
    print("=" * 56)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
