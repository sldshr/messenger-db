# main.py — SLD Posts (FastAPI + Supabase)
import os, time, random, html
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SB_REST = f"{SUPABASE_URL}/rest/v1"

MAX_POST_BYTES  = 350 * 1024
MAX_IMAGE_BYTES = 280 * 1024

app = FastAPI(title="SLD Posts", version="2.0")

# ================= SUPABASE HELPERS =================
def sb_headers(prefer=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h

def sb_get(path, params=None):
    with httpx.Client(timeout=20) as cli:
        r = cli.get(f"{SB_REST}{path}", headers=sb_headers(), params=params or {})
    r.raise_for_status()
    return r.json()

def sb_post(path, data):
    with httpx.Client(timeout=20) as cli:
        r = cli.post(f"{SB_REST}{path}", headers=sb_headers("return=representation"), json=data)
    r.raise_for_status()
    return r.json()

def sb_patch(path, data, params=None):
    with httpx.Client(timeout=20) as cli:
        r = cli.patch(f"{SB_REST}{path}", headers=sb_headers("return=representation"),
                      params=params or {}, json=data)
    r.raise_for_status()
    return r.json()

def sb_delete(path, params=None):
    with httpx.Client(timeout=20) as cli:
        r = cli.delete(f"{SB_REST}{path}", headers=sb_headers(), params=params or {})
    r.raise_for_status()
    return True

# ================= UTILS =================
def now_ms(): return int(time.time() * 1000)

def gen_id():
    for _ in range(30):
        s = f"{random.randint(100000, 999999):06d}"
        exists = sb_get("/posts", {"id": f"eq.{s}", "select": "id", "limit": 1})
        if not exists:
            return s
    raise HTTPException(500, "id generation failed")

def normalize_tags(tags):
    out = []
    for t in tags or []:
        t = (t or "").strip()
        if not t: continue
        if not t.startswith("#"): t = "#" + t
        if t not in out: out.append(t)
    return out[:20]

def fmt_time(ms):
    try: return time.strftime("%d.%m.%Y %H:%M", time.localtime(ms / 1000.0))
    except Exception: return ""

def esc(s): return html.escape(s or "", quote=True)

def row_to_post(row):
    """Приводим строку из Supabase к JSON для клиента."""
    return {
        "id": row.get("id"),
        "author_uuid": row.get("author_uuid"),
        "text": row.get("content") or "",
        "tags": row.get("tags") or [],
        "image": row.get("image_base64"),
        "timestamp": row.get("created_at") or 0,
    }

# ================= MODELS =================
class CreatePostReq(BaseModel):
    author_uuid: str
    text: str = ""
    tags: List[str] = []
    image: Optional[str] = None

class UpdatePostReq(BaseModel):
    author_uuid: str
    text: Optional[str] = None
    tags: Optional[List[str]] = None
    image: Optional[str] = None

# ================= HTML =================
CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#111418;color:#fff;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;min-height:100vh}
a{color:#8fd3ff;text-decoration:none}
.container{max-width:680px;margin:0 auto;padding:20px 18px 60px}
.header{background:#1f2530;padding:16px 20px;display:flex;align-items:center;gap:12px;
  border-bottom:1px solid #0a1018}
.header .logo{width:40px;height:40px;border-radius:50%;
  background:linear-gradient(135deg,#3a7bd5,#8f3ad5);
  display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:20px;color:#fff}
.header .title{font-weight:700;font-size:20px;color:#fff}
.header .sub{color:#7b8794;font-size:12px;margin-top:2px}
.hero{text-align:center;padding:36px 16px 8px}
.hero .big-logo{width:96px;height:96px;border-radius:50%;
  background:linear-gradient(135deg,#3a7bd5,#8f3ad5);
  display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:46px;color:#fff;margin:0 auto 18px}
.hero h1{font-size:26px;margin:0 0 8px}
.hero p{color:#7b8794;margin:0;font-size:14px}
.card{background:#1a1f26;border-radius:14px;padding:18px;margin-top:16px}
.section-label{color:#aab6c3;font-size:13px;margin-bottom:8px}
.search-row{display:flex;gap:8px}
input[type=text]{flex:1;background:#0c1014;color:#fff;border:1px solid #0a1018;
  border-radius:10px;padding:14px 14px;font-size:18px;letter-spacing:.15em;
  outline:none;font-family:inherit}
input[type=text]:focus{border-color:#3a7bd5}
input[type=text]::placeholder{color:#5e6a76;letter-spacing:normal}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;
  border:none;border-radius:10px;padding:14px 22px;font-size:15px;font-weight:600;
  color:#fff;background:#2a3340;cursor:pointer;text-decoration:none;
  transition:transform .06s ease,filter .12s ease;font-family:inherit}
.btn:hover{filter:brightness(1.1)}
.btn:active{transform:scale(.97)}
.btn-primary{background:#3a7bd5}
.btn-block{width:100%}
.btn-svg{width:20px;height:20px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.tag{display:inline-block;background:#222c3a;color:#8fd3ff;
  padding:4px 10px;border-radius:8px;font-size:12px;margin:0 6px 6px 0}
.id-badge{display:inline-block;background:#222c3a;color:#8fd3ff;
  padding:6px 12px;border-radius:8px;font-weight:700;font-size:14px;letter-spacing:.06em}
.post-meta{color:#7b8794;font-size:12px;margin-top:10px}
.post-body{color:#e6ecf3;font-size:15px;line-height:1.55;margin-top:14px;
  white-space:pre-wrap;word-wrap:break-word}
.post-img{width:100%;border-radius:12px;margin-top:14px;display:block;background:#0c1014}
.error{color:#ff7676;font-size:13px;margin-top:10px;min-height:18px}
.not-found{text-align:center;padding:60px 16px}
.not-found .code{font-size:72px;font-weight:800;color:#ff7676;letter-spacing:.1em}
.not-found p{color:#7b8794}
.footer{text-align:center;color:#5e6a76;font-size:12px;padding:30px 16px 10px}
.legal{color:#c5cfdb;font-size:14px;line-height:1.6}
.legal h2{color:#fff;font-size:18px;margin:22px 0 8px}
"""

SVG_DL     = '<svg class="btn-svg" viewBox="0 0 24 24"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 21h16"/></svg>'
SVG_SEARCH = '<svg class="btn-svg" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M16.5 16.5L21 21"/></svg>'
SVG_BACK   = '<svg class="btn-svg" viewBox="0 0 24 24"><path d="M15 5L8 12l7 7"/></svg>'

def page(title, body):
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — SLD</title><style>{CSS}</style></head><body>
<div class="header">
  <div class="logo">S</div>
  <div><div class="title"><a href="/" style="color:#fff">SLD</a></div>
  <div class="sub">Анонимные посты</div></div>
</div>
{body}
<div class="footer">SLD · <a href="/privacy">Политика конфиденциальности</a></div>
</body></html>"""

DOWNLOAD_JS = """
function downloadSoon(btn){
  var old = btn.innerHTML;
  btn.innerHTML = 'Скоро!';
  btn.disabled = true;
  alert('Скоро!');
  setTimeout(function(){ btn.innerHTML = old; btn.disabled = false; }, 1600);
}
"""

def index_page():
    body = f"""
<div class="container">
  <div class="hero">
    <div class="big-logo">S</div>
    <h1>SLD</h1>
    <p>Анонимные посты. Найди по 6-значному коду</p>
  </div>

  <button class="btn btn-primary btn-block" style="margin-top:24px"
          onclick="downloadSoon(this)">
    {SVG_DL} Скачать приложение
  </button>

  <div class="card">
    <div class="section-label">Найти пост по ID</div>
    <form class="search-row" onsubmit="event.preventDefault(); openCode();">
      <input id="code" type="text" inputmode="numeric" pattern="[0-9]{{6}}"
             maxlength="6" placeholder="6 цифр" autocomplete="off">
      <button type="submit" class="btn btn-primary">{SVG_SEARCH} Найти</button>
    </form>
    <div id="err" class="error"></div>
  </div>
</div>
<script>
{DOWNLOAD_JS}
function openCode() {{
  var v = document.getElementById('code').value.trim();
  var e = document.getElementById('err');
  if (!/^\\d{{6}}$/.test(v)) {{ e.textContent = 'Введите ровно 6 цифр'; return; }}
  e.textContent = '';
  window.location.href = '/' + v;
}}
document.getElementById('code').addEventListener('keydown', function(ev){{
  if (ev.key === 'Enter') {{ ev.preventDefault(); openCode(); }}
}});
</script>
"""
    return page("Главная", body)

def post_page(p):
    tags_html = ""
    if p.get("tags"):
        tags_html = '<div style="margin-top:12px">' + "".join(
            f'<span class="tag">{esc(t)}</span>' for t in p["tags"]) + '</div>'

    text_html = ""
    if (p.get("text") or "").strip():
        text_html = f'<div class="post-body">{esc(p["text"])}</div>'

    img_html = ""
    if p.get("image"):
        img_html = f'<img class="post-img" src="data:image/jpeg;base64,{p["image"]}" alt="">'

    body = f"""
<div class="container">
  <a class="btn" href="/" style="margin-top:6px">{SVG_BACK} На главную</a>

  <div class="card">
    <div><span class="id-badge">ID: {esc(p['id'])}</span></div>
    <div class="post-meta">{fmt_time(p.get('timestamp', 0))} · анонимно</div>
    {text_html}
    {tags_html}
    {img_html}
  </div>

  <button class="btn btn-primary btn-block" style="margin-top:20px"
          onclick="downloadSoon(this)">
    {SVG_DL} Скачать приложение
  </button>
</div>
<script>{DOWNLOAD_JS}</script>
"""
    return page(f"Пост {p['id']}", body)

def not_found_page(code):
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

def privacy_page():
    body = """
<div class="container">
  <a class="btn" href="/" style="margin-top:6px">Назад</a>
  <div class="card">
    <h1 style="margin-top:0">Политика конфиденциальности</h1>
    <div class="legal">
      <p>Сервис SLD («мы») предоставляет анонимную платформу для публикации постов.
      Эта страница описывает, какие данные мы собираем и как их используем.</p>

      <h2>1. Какие данные мы храним</h2>
      <ul>
        <li><b>Анонимный идентификатор устройства (UUID)</b> — генерируется на
        основе системного идентификатора вашего устройства. Используется
        исключительно для того, чтобы вы могли видеть и редактировать
        собственные посты. Ни имя, ни номер телефона, ни email не запрашиваются.</li>
        <li><b>Содержимое постов</b> — текст, теги и (при наличии) композитное
        изображение, которое вы публикуете добровольно.</li>
        <li><b>Дата и время публикации</b> — для сортировки.</li>
      </ul>

      <h2>2. Чего мы НЕ делаем</h2>
      <ul>
        <li>Не запрашиваем регистрацию, имя, телефон или почту.</li>
        <li>Не используем рекламные трекеры и сторонние SDK аналитики.</li>
        <li>Не передаём данные третьим лицам, кроме хостинга Supabase,
        который хранит содержимое базы данных на своих серверах.</li>
      </ul>

      <h2>3. Где хранятся данные</h2>
      <p>Данные хранятся в облачной базе данных Supabase. Передача между
      приложением и сервером идёт по HTTPS.</p>

      <h2>4. Ваши права</h2>
      <ul>
        <li>Вы можете в любой момент удалить свой пост — он немедленно удаляется из базы.</li>
        <li>Вы можете сбросить аккаунт в настройках приложения — все ваши посты
        будут удалены.</li>
      </ul>

      <h2>5. Контакты</h2>
      <p>По вопросам: <a href="mailto:privacy@sldchat.fastapicloud.dev">
      privacy@sldchat.fastapicloud.dev</a></p>

      <p style="color:#7b8794;margin-top:24px">Обновлено: 2025</p>
    </div>
  </div>
</div>
"""
    return page("Политика конфиденциальности", body)

# ================= API ROUTES =================
@app.get("/health")
def health():
    return {"status": "ok", "ts": now_ms()}

@app.get("/api")
def api_root():
    return {"ok": True, "service": "SLD Posts"}

@app.post("/post")
def create_post(r: CreatePostReq):
    text = (r.text or "")[:10000]
    tags = normalize_tags(r.tags)
    img = r.image or None

    if not text.strip() and not img:
        raise HTTPException(400, "empty post")

    if img and len(img) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"image too large: {len(img)} > {MAX_IMAGE_BYTES}")

    total = len(text.encode("utf-8")) + len(img or "")
    if total > MAX_POST_BYTES:
        raise HTTPException(413, f"post too large: {total} > {MAX_POST_BYTES}")

    pid = gen_id()
    row = {
        "id": pid,
        "author_uuid": r.author_uuid,
        "content": text,
        "tags": tags,
        "image_base64": img,
        "created_at": now_ms(),
    }
    created = sb_post("/posts", row)
    return row_to_post(created[0] if created else row)

@app.put("/post/{pid}")
def update_post(pid: str, r: UpdatePostReq):
    rows = sb_get("/posts", {"id": f"eq.{pid}", "limit": 1})
    if not rows:
        raise HTTPException(404, "not found")
    if rows[0]["author_uuid"] != r.author_uuid:
        raise HTTPException(403, "not owner")

    upd = {}
    if r.text is not None: upd["content"] = (r.text or "")[:10000]
    if r.tags is not None: upd["tags"] = normalize_tags(r.tags)
    if r.image is not None:
        if r.image and len(r.image) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "image too large")
        upd["image_base64"] = r.image or None
    upd["created_at"] = now_ms()

    total = len((upd.get("content") or "").encode("utf-8")) + len(upd.get("image_base64") or "")
    if total > MAX_POST_BYTES:
        raise HTTPException(413, "post too large")

    out = sb_patch("/posts", upd, {"id": f"eq.{pid}"})
    return row_to_post(out[0] if out else upd)

@app.delete("/post/{pid}")
def delete_post(pid: str, author_uuid: str):
    rows = sb_get("/posts", {"id": f"eq.{pid}", "limit": 1})
    if not rows:
        raise HTTPException(404, "not found")
    if rows[0]["author_uuid"] != author_uuid:
        raise HTTPException(403, "not owner")
    sb_delete("/posts", {"id": f"eq.{pid}"})
    return {"ok": True}

@app.get("/post/{pid}")
def get_post(pid: str):
    rows = sb_get("/posts", {"id": f"eq.{pid}", "limit": 1})
    if not rows:
        raise HTTPException(404, "not found")
    return row_to_post(rows[0])

@app.get("/my_posts/{uid}")
def my_posts(uid: str):
    rows = sb_get("/posts", {"author_uuid": f"eq.{uid}",
                             "order": "created_at.desc"})
    return [row_to_post(r) for r in rows]

@app.post("/reset/{uid}")
def reset(uid: str):
    sb_delete("/posts", {"author_uuid": f"eq.{uid}"})
    return {"ok": True}

# ================= WEB ROUTES =================
@app.get("/", response_class=HTMLResponse)
def web_index(): return index_page()

@app.get("/privacy", response_class=HTMLResponse)
def web_privacy(): return privacy_page()

# ВАЖНО: последний маршрут — ловит 6-значные коды
@app.get("/{code}", response_class=HTMLResponse)
def web_post(code: str):
    if not (len(code) == 6 and code.isdigit()):
        return HTMLResponse(not_found_page(code), status_code=404)
    rows = sb_get("/posts", {"id": f"eq.{code}", "limit": 1})
    if not rows:
        return HTMLResponse(not_found_page(code), status_code=404)
    return post_page(row_to_post(rows[0]))
