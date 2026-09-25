# main.py — SLD Posts server + web UI
# Deploy: fastapicloud / uvicorn main:app --host 0.0.0.0 --port 8000

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from pydantic import BaseModel
from typing import Optional, List
import time, random, os, html

app = FastAPI(title="SLD Posts", version="1.1")

# URL APK (положи файл рядом с main.py как app.apk — или вставь свой CDN URL)
APK_URL = "/download/apk"

users = {}   # uuid -> {uuid, nick}
posts = {}   # id (6-цифр) -> {id, author_uuid, author_nick, text, tags, photos, timestamp}

# ================== MODELS ==================
class RegisterReq(BaseModel):
    uuid: str
    nick: str

class UpdateNickReq(BaseModel):
    uuid: str
    nick: str

class CreatePostReq(BaseModel):
    author_uuid: str
    text: str = ""
    tags: List[str] = []
    photos: List[str] = []   # base64 JPEG

class UpdatePostReq(BaseModel):
    author_uuid: str
    text: Optional[str] = None
    tags: Optional[List[str]] = None
    photos: Optional[List[str]] = None

# ================== UTILS ==================
def now_ms() -> int:
    return int(time.time() * 1000)

def gen_id() -> str:
    for _ in range(500):
        s = f"{random.randint(100000, 999999):06d}"
        if s not in posts:
            return s
    raise HTTPException(500, "id generation failed")

def ensure_user(uid, nick=None):
    u = users.get(uid)
    if u is None:
        u = {"uuid": uid, "nick": nick or ("User-" + uid[:6])}
        users[uid] = u
    return u

def normalize_tags(tags: List[str]) -> List[str]:
    out = []
    for t in tags or []:
        t = (t or "").strip()
        if not t:
            continue
        if not t.startswith("#"):
            t = "#" + t
        if t not in out:
            out.append(t)
    return out[:20]

def fmt_time(ms: int) -> str:
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(ms / 1000.0))
    except Exception:
        return ""

def esc(s: str) -> str:
    return html.escape(s or "", quote=True)

# ================== HTML TEMPLATES ==================
BASE_CSS = """
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: #111418; color: #ffffff;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  min-height: 100vh;
}
a { color: #8fd3ff; text-decoration: none; }
.container { max-width: 720px; margin: 0 auto; padding: 24px 20px 60px; }

.header {
  background: #1f2530;
  padding: 18px 20px;
  display: flex; align-items: center; gap: 12px;
  border-bottom: 1px solid #0a1018;
}
.header .logo {
  width: 40px; height: 40px; border-radius: 50%;
  background: linear-gradient(135deg, #3a7bd5, #8f3ad5);
  display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 20px; color: #fff;
}
.header .title { font-weight: 700; font-size: 20px; }
.header .sub { color: #7b8794; font-size: 12px; margin-top: 2px; }

.card {
  background: #1a1f26;
  border-radius: 14px;
  padding: 18px;
  margin-top: 18px;
}

.section-label { color: #aab6c3; font-size: 13px; margin-bottom: 8px; }

.search-row { display: flex; gap: 8px; }
input[type="text"] {
  flex: 1;
  background: #0c1014;
  color: #fff;
  border: 1px solid #0a1018;
  border-radius: 10px;
  padding: 14px 14px;
  font-size: 18px;
  letter-spacing: 0.15em;
  outline: none;
  font-family: inherit;
}
input[type="text"]:focus { border-color: #3a7bd5; }
input[type="text"]::placeholder { color: #5e6a76; letter-spacing: normal; }

.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  border: none; border-radius: 10px;
  padding: 14px 22px;
  font-size: 15px; font-weight: 600;
  color: #fff; background: #2a3340;
  cursor: pointer; text-decoration: none;
  transition: transform 0.06s ease, filter 0.12s ease;
  font-family: inherit;
}
.btn:hover { filter: brightness(1.1); }
.btn:active { transform: scale(0.97); }
.btn-primary { background: #3a7bd5; }
.btn-block { width: 100%; }
.btn-svg { width: 20px; height: 20px; stroke: currentColor; fill: none;
           stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }

.hero {
  text-align: center;
  padding: 40px 20px 10px;
}
.hero .big-logo {
  width: 96px; height: 96px; border-radius: 50%;
  background: linear-gradient(135deg, #3a7bd5, #8f3ad5);
  display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 46px; color: #fff;
  margin: 0 auto 20px;
}
.hero h1 { font-size: 26px; margin: 0 0 8px; }
.hero p { color: #7b8794; margin: 0; font-size: 14px; }

.tag {
  display: inline-block;
  background: #222c3a; color: #8fd3ff;
  padding: 4px 10px; border-radius: 8px;
  font-size: 12px; margin: 0 6px 6px 0;
}

.id-badge {
  display: inline-block;
  background: #222c3a; color: #8fd3ff;
  padding: 6px 12px; border-radius: 8px;
  font-weight: 700; font-size: 14px;
  letter-spacing: 0.06em;
}

.post-meta { color: #7b8794; font-size: 12px; margin-top: 10px; }
.post-body { color: #e6ecf3; font-size: 15px; line-height: 1.5; margin-top: 14px; white-space: pre-wrap; word-wrap: break-word; }

.photo-grid {
  display: grid;
  gap: 4px;
  margin-top: 12px;
}
.photo-grid.cols-1 { grid-template-columns: 1fr; }
.photo-grid.cols-2 { grid-template-columns: 1fr 1fr; }
.photo-grid.cols-3 { grid-template-columns: 1fr 1fr 1fr; }
.photo-grid img {
  width: 100%; height: 100%;
  aspect-ratio: 1 / 1;
  object-fit: cover;
  border-radius: 8px;
  background: #0c1014;
  cursor: zoom-in;
  display: block;
}
.photo-grid.cols-1 img { aspect-ratio: 16 / 10; }

.error {
  color: #ff7676; font-size: 13px; margin-top: 10px;
  min-height: 18px;
}
.not-found {
  text-align: center; padding: 60px 20px;
}
.not-found .code {
  font-size: 72px; font-weight: 800; color: #ff7676;
  letter-spacing: 0.1em;
}
.not-found p { color: #7b8794; }

.footer {
  text-align: center; color: #5e6a76; font-size: 12px;
  padding: 30px 20px 10px;
}

/* Lightbox */
.lightbox {
  position: fixed; inset: 0; background: rgba(0,0,0,0.95);
  display: none; align-items: center; justify-content: center;
  z-index: 9999; padding: 20px;
}
.lightbox.open { display: flex; }
.lightbox img { max-width: 100%; max-height: 100%; object-fit: contain; }
.lightbox-close {
  position: absolute; top: 20px; right: 20px;
  background: rgba(255,255,255,0.1); color: #fff;
  border: none; border-radius: 50%;
  width: 44px; height: 44px; font-size: 22px;
  cursor: pointer;
}
"""

# SVG иконки
SVG_DOWNLOAD = '<svg class="btn-svg" viewBox="0 0 24 24"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 21h16"/></svg>'
SVG_SEARCH   = '<svg class="btn-svg" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M16.5 16.5L21 21"/></svg>'
SVG_BACK     = '<svg class="btn-svg" viewBox="0 0 24 24"><path d="M15 5L8 12l7 7"/></svg>'

def page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — SLD</title>
<style>{BASE_CSS}</style>
</head>
<body>
<div class="header">
  <div class="logo">S</div>
  <div>
    <div class="title"><a href="/" style="color:#fff">SLD</a></div>
    <div class="sub">Посты с сервером</div>
  </div>
</div>
{body}
<div class="footer">SLD · ru.sldshr.cleancam</div>
</body>
</html>"""

def index_page() -> str:
    body = f"""
<div class="container">
  <div class="hero">
    <div class="big-logo">S</div>
    <h1>SLD</h1>
    <p>Публикуй посты, делись с друзьями по 6-значному коду</p>
  </div>

  <a class="btn btn-primary btn-block" style="margin-top:24px" href="{APK_URL}">
    {SVG_DOWNLOAD}
    Скачать приложение
  </a>

  <div class="card">
    <div class="section-label">Найти пост по ID</div>
    <form class="search-row" method="get" onsubmit="event.preventDefault(); openCode();">
      <input id="code" type="text" inputmode="numeric" pattern="[0-9]{{6}}"
             maxlength="6" placeholder="6 цифр" autocomplete="off">
      <button type="submit" class="btn btn-primary">
        {SVG_SEARCH}
        Найти
      </button>
    </form>
    <div id="err" class="error"></div>
  </div>
</div>
<script>
function openCode() {{
  var v = document.getElementById('code').value.trim();
  var err = document.getElementById('err');
  if (!/^\\d{{6}}$/.test(v)) {{
    err.textContent = 'Введите ровно 6 цифр';
    return;
  }}
  err.textContent = '';
  window.location.href = '/' + v;
}}
document.getElementById('code').addEventListener('keydown', function(e) {{
  if (e.key === 'Enter') {{ e.preventDefault(); openCode(); }}
}});
</script>
"""
    return page("Главная", body)

def post_page(post: dict) -> str:
    # Фотографии
    photos_html = ""
    photos = post.get("photos") or []
    if photos:
        n = len(photos)
        cols = 1 if n == 1 else (2 if n in (2, 4) else 3)
        imgs = "".join(
            f'<img src="data:image/jpeg;base64,{p}" alt="" onclick="openLightbox(this.src)">'
            for p in photos
        )
        photos_html = f'<div class="photo-grid cols-{cols}">{imgs}</div>'

    # Теги
    tags = post.get("tags") or []
    tags_html = ""
    if tags:
        tags_html = "".join(f'<span class="tag">{esc(t)}</span>' for t in tags)
        tags_html = f'<div style="margin-top:14px">{tags_html}</div>'

    text = esc(post.get("text") or "")
    text_html = f'<div class="post-body">{text}</div>' if text.strip() else ""

    body = f"""
<div class="container">
  <div style="display:flex; gap:8px; margin-top:6px">
    <a class="btn" href="/">{SVG_BACK} На главную</a>
  </div>

  <div class="card">
    <div>
      <span class="id-badge">ID: {esc(post['id'])}</span>
    </div>
    <div class="post-meta">@{esc(post.get('author_nick') or '')} · {fmt_time(post.get('timestamp', 0))}</div>
    {text_html}
    {tags_html}
    {photos_html}
  </div>

  <a class="btn btn-primary btn-block" style="margin-top:20px" href="{APK_URL}">
    {SVG_DOWNLOAD}
    Скачать приложение
  </a>
</div>

<div class="lightbox" id="lightbox" onclick="closeLightbox()">
  <button class="lightbox-close" onclick="closeLightbox()">×</button>
  <img id="lightbox-img" src="" alt="">
</div>
<script>
function openLightbox(src) {{
  document.getElementById('lightbox-img').src = src;
  document.getElementById('lightbox').classList.add('open');
}}
function closeLightbox() {{
  document.getElementById('lightbox').classList.remove('open');
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') closeLightbox();
}});
</script>
"""
    return page(f"Пост {post['id']}", body)

def not_found_page(code: str) -> str:
    body = f"""
<div class="container">
  <div class="not-found">
    <div class="code">{esc(code)}</div>
    <p>Пост с таким ID не найден</p>
    <a class="btn btn-primary" style="margin-top:20px" href="/">{SVG_BACK} На главную</a>
  </div>
</div>
"""
    return page("Не найдено", body)

# ================== WEB ROUTES ==================
@app.get("/", response_class=HTMLResponse)
def web_index():
    return index_page()

@app.get("/download/apk")
def download_apk():
    # Если файл app.apk лежит рядом с main.py — отдаём его
    for name in ("app.apk", "sld.apk", "SLD.apk"):
        if os.path.exists(name):
            return FileResponse(name, media_type="application/vnd.android.package-archive",
                                filename="SLD.apk")
    # Иначе показываем заглушку
    return HTMLResponse(page("Скачать", """
<div class="container">
  <div class="card" style="text-align:center">
    <h2 style="margin-top:0">Файл APK ещё не загружен</h2>
    <p style="color:#7b8794">Положи файл <b>app.apk</b> рядом с main.py на сервере —
    и кнопка скачивания начнёт отдавать приложение.</p>
    <a class="btn btn-primary" href="/" style="margin-top:14px">На главную</a>
  </div>
</div>
"""), status_code=200)

# ================== API ROUTES ==================
@app.get("/health")
def health():
    return {"status": "healthy", "ts": now_ms()}

@app.get("/api")
def api_root():
    return {"ok": True, "service": "SLD Posts", "users": len(users), "posts": len(posts)}

@app.post("/register")
def register(r: RegisterReq):
    n = (r.nick or "").strip()
    if not n: raise HTTPException(400, "empty nick")
    if len(n) > 32: n = n[:32]
    u = ensure_user(r.uuid, n); u["nick"] = n
    return u

@app.post("/update_nick")
def update_nick(r: UpdateNickReq):
    if r.uuid not in users: raise HTTPException(404, "user not found")
    n = (r.nick or "").strip()
    if not n: raise HTTPException(400, "empty nick")
    if len(n) > 32: n = n[:32]
    users[r.uuid]["nick"] = n
    for p in posts.values():
        if p["author_uuid"] == r.uuid:
            p["author_nick"] = n
    return users[r.uuid]

@app.post("/post")
def create_post(r: CreatePostReq):
    if r.author_uuid not in users: raise HTTPException(404, "author not found")
    text = (r.text or "")[:10000]
    tags = normalize_tags(r.tags)
    photos = (r.photos or [])[:5]
    if not text.strip() and not photos:
        raise HTTPException(400, "empty post")
    pid = gen_id()
    p = {
        "id": pid,
        "author_uuid": r.author_uuid,
        "author_nick": users[r.author_uuid]["nick"],
        "text": text,
        "tags": tags,
        "photos": photos,
        "timestamp": now_ms(),
    }
    posts[pid] = p
    return p

@app.put("/post/{pid}")
def update_post(pid: str, r: UpdatePostReq):
    p = posts.get(pid)
    if p is None: raise HTTPException(404, "post not found")
    if p["author_uuid"] != r.author_uuid: raise HTTPException(403, "not owner")
    if r.text is not None: p["text"] = (r.text or "")[:10000]
    if r.tags is not None: p["tags"] = normalize_tags(r.tags)
    if r.photos is not None: p["photos"] = (r.photos or [])[:5]
    p["timestamp"] = now_ms()
    return p

@app.delete("/post/{pid}")
def delete_post(pid: str, author_uuid: str):
    p = posts.get(pid)
    if p is None: raise HTTPException(404, "post not found")
    if p["author_uuid"] != author_uuid: raise HTTPException(403, "not owner")
    posts.pop(pid, None)
    return {"ok": True}

@app.get("/post/{pid}")
def get_post(pid: str):
    p = posts.get(pid)
    if p is None: raise HTTPException(404, "post not found")
    return p

@app.get("/my_posts/{uid}")
def my_posts(uid: str):
    if uid not in users: raise HTTPException(404, "user not found")
    lst = [p for p in posts.values() if p["author_uuid"] == uid]
    lst.sort(key=lambda x: x["timestamp"], reverse=True)
    return lst

@app.get("/feed")
def feed(limit: int = 30, offset: int = 0):
    lst = list(posts.values())
    lst.sort(key=lambda x: x["timestamp"], reverse=True)
    return lst[offset:offset + limit]

@app.post("/reset/{uid}")
def reset(uid: str):
    if uid not in users: raise HTTPException(404, "user not found")
    users.pop(uid, None)
    for k in [k for k, v in posts.items() if v["author_uuid"] == uid]:
        posts.pop(k, None)
    return {"ok": True}

# ================== HTML POST ROUTE (в самом конце!) ==================
# ВАЖНО: этот маршрут должен идти после всех конкретных /post/... и /register и т.д.,
# иначе FastAPI перехватит их. Он ловит только 6-значные числовые пути.
@app.get("/{code}", response_class=HTMLResponse)
def web_post(code: str):
    # Отсекаем всё, что не 6 цифр
    if not (len(code) == 6 and code.isdigit()):
        return HTMLResponse(not_found_page(code), status_code=404)
    p = posts.get(code)
    if p is None:
        return HTMLResponse(not_found_page(code), status_code=404)
    return post_page(p)
