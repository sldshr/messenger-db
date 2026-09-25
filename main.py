# main.py — SLD Posts (FastAPI + Supabase via stdlib urllib)
# Никаких внешних зависимостей кроме fastapi/uvicorn.
import os, time, random, html, json, ssl, traceback
from typing import Optional, List
from urllib import request as urlreq
from urllib import parse as urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# ================= ENV =================
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY") or "")

def env_state():
    return {
        "SUPABASE_URL_set": bool(SUPABASE_URL),
        "SUPABASE_URL_prefix": SUPABASE_URL[:40] if SUPABASE_URL else None,
        "SUPABASE_KEY_set": bool(SUPABASE_KEY),
        "SUPABASE_KEY_len": len(SUPABASE_KEY) if SUPABASE_KEY else 0,
    }

# ================= LIMITS =================
MAX_POST_BYTES  = 350 * 1024
MAX_IMAGE_BYTES = 280 * 1024

app = FastAPI(title="SLD Posts", version="2.1")

# ================= SUPABASE (stdlib) =================
def sb_headers(prefer=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h

def sb_request(method, path, params=None, body=None, prefer=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase env vars not set (SUPABASE_URL / SUPABASE_KEY)")

    url = f"{SUPABASE_URL}/rest/v1{path}"
    if params:
        url += "?" + urlparse.urlencode(params)

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urlreq.Request(url, data=data, method=method, headers=sb_headers(prefer))
    ctx = ssl.create_default_context()
    try:
        with urlreq.urlopen(req, timeout=20, context=ctx) as r:
            raw = r.read().decode("utf-8")
            if not raw:
                return []
            try:
                return json.loads(raw)
            except Exception:
                return raw
    except urlreq.HTTPError as he:
        err_body = ""
        try: err_body = he.read().decode("utf-8")
        except Exception: pass
        raise RuntimeError(f"Supabase {he.code}: {err_body[:300]}")
    except Exception as e:
        raise RuntimeError(f"Supabase request failed: {e}")

def sb_get(path, params=None):
    return sb_request("GET", path, params=params)

def sb_post(path, data):
    return sb_request("POST", path, body=data, prefer="return=representation")

def sb_patch(path, data, params=None):
    return sb_request("PATCH", path, params=params, body=data, prefer="return=representation")

def sb_delete(path, params=None):
    sb_request("DELETE", path, params=params)
    return True

# ================= UTILS =================
def now_ms(): return int(time.time() * 1000)

def gen_id():
    for _ in range(30):
        s = f"{random.randint(100000, 999999):06d}"
        rows = sb_get("/posts", {"id": f"eq.{s}", "select": "id", "limit": 1})
        if not rows:
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
.banner{background:#3a2a2a;color:#ffd0d0;padding:12px 16px;border-radius:10px;
  margin-top:16px;font-size:13px;border:1px solid #6a3a3a}
"""

SVG_DL     = '<svg class="btn-svg" viewBox="0 0 24 24"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 21h16"/></svg>'
SVG_SEARCH = '<svg class="btn-svg" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M16.5 16.5L21 21"/></svg>'
SVG_BACK   = '<svg class="btn-svg" viewBox="0 0 24 24"><path d="M15 5L8 12l7 7"/></svg>'

DOWNLOAD_JS = """
function downloadSoon(btn){
  var old = btn.innerHTML;
  btn.innerHTML = 'Скоро!';
  btn.disabled = true;
  setTimeout(function(){ btn.innerHTML = old; btn.disabled = false; }, 1600);
}
"""

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

def index_page(err_banner=""):
    banner = f'<div class="banner">{esc(err_banner)}</div>' if err_banner else ""
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

  {banner}

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
      <p>Сервис SLD («мы») предоставляет анонимную платформу для публикации постов.</p>
      <h2>1. Какие данные мы храним</h2>
      <ul>
        <li><b>Анонимный UUID</b> — генерируется на основе системного идентификатора устройства.
        Используется, чтобы вы могли видеть и редактировать собственные посты.</li>
        <li><b>Содержимое постов</b> — текст, теги и композитное изображение.</li>
        <li><b>Дата и время публикации</b>.</li>
      </ul>
      <h2>2. Чего мы НЕ делаем</h2>
      <ul>
        <li>Не запрашиваем регистрацию, имя, телефон или почту.</li>
        <li>Не используем рекламные трекеры.</li>
        <li>Не передаём данные третьим лицам, кроме хостинга Supabase.</li>
      </ul>
      <h2>3. Где хранятся данные</h2>
      <p>Данные в Supabase. Передача по HTTPS.</p>
      <h2>4. Ваши права</h2>
      <ul>
        <li>Удалить свой пост можно в любой момент.</li>
        <li>Сбросить аккаунт можно в настройках приложения — все ваши посты удалятся.</li>
      </ul>
      <h2>5. Контакты</h2>
      <p>privacy@sldchat.fastapicloud.dev</p>
      <p style="color:#7b8794;margin-top:24px">Обновлено: 2025</p>
    </div>
  </div>
</div>
"""
    return page("Политика конфиденциальности", body)

# ================= DIAGNOSTIC ROUTES (идут раньше /{code}) =================
@app.get("/health")
def health():
    """Общая проверка — сервер жив?"""
    return {"status": "ok", "ts": now_ms(), "env": env_state()}

@app.get("/diag")
def diag():
    """Полная диагностика: env + попытка запроса к Supabase."""
    result = {"ts": now_ms(), "env": env_state(), "supabase": None}
    try:
        rows = sb_get("/posts", {"select": "id", "limit": 1})
        result["supabase"] = {"ok": True, "sample": rows}
    except Exception as e:
        result["supabase"] = {"ok": False, "error": str(e),
                              "trace": traceback.format_exc()[-800:]}
    return JSONResponse(result)

@app.get("/api")
def api_root():
    return {"ok": True, "service": "SLD Posts", "env": env_state()}

# ================= API ROUTES =================
@app.post("/post")
def create_post(r: CreatePostReq):
    text = (r.text or "")[:10000]
    tags = normalize_tags(r.tags)
    img = r.image or None

    if not text.strip() and not img:
        raise HTTPException(400, "empty post")
    if img and len(img) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"image too large: {len(img)}")
    total = len(text.encode("utf-8")) + len(img or "")
    if total > MAX_POST_BYTES:
        raise HTTPException(413, f"post too large: {total}")

    try:
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"supabase: {e}")

@app.put("/post/{pid}")
def update_post(pid: str, r: UpdatePostReq):
    try:
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"supabase: {e}")

@app.delete("/post/{pid}")
def delete_post(pid: str, author_uuid: str):
    try:
        rows = sb_get("/posts", {"id": f"eq.{pid}", "limit": 1})
        if not rows:
            raise HTTPException(404, "not found")
        if rows[0]["author_uuid"] != author_uuid:
            raise HTTPException(403, "not owner")
        sb_delete("/posts", {"id": f"eq.{pid}"})
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"supabase: {e}")

@app.get("/post/{pid}")
def get_post(pid: str):
    try:
        rows = sb_get("/posts", {"id": f"eq.{pid}", "limit": 1})
        if not rows:
            raise HTTPException(404, "not found")
        return row_to_post(rows[0])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"supabase: {e}")

@app.get("/my_posts/{uid}")
def my_posts(uid: str):
    try:
        rows = sb_get("/posts", {"author_uuid": f"eq.{uid}", "order": "created_at.desc"})
        return [row_to_post(r) for r in rows]
    except Exception as e:
        raise HTTPException(500, f"supabase: {e}")

@app.post("/reset/{uid}")
def reset(uid: str):
    try:
        sb_delete("/posts", {"author_uuid": f"eq.{uid}"})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"supabase: {e}")

# ================= WEB ROUTES =================
@app.get("/", response_class=HTMLResponse)
def web_index():
    """Главная страница — рендерится ВСЕГДА, даже если Supabase лежит."""
    banner = ""
    try:
        # Простая проверка доступности БД — не блокирует рендер при ошибке
        if not SUPABASE_URL or not SUPABASE_KEY:
            banner = ("Внимание: не заданы SUPABASE_URL / SUPABASE_KEY. "
                      "Поиск по ID работать не будет. Проверь /diag.")
        else:
            sb_get("/posts", {"select": "id", "limit": 1})
    except Exception as e:
        banner = f"Supabase недоступен: {e}"
    return index_page(banner)

@app.get("/privacy", response_class=HTMLResponse)
def web_privacy(): return privacy_page()

# ВАЖНО: последний маршрут — catch-all для 6-значных кодов
@app.get("/{code}", response_class=HTMLResponse)
def web_post(code: str):
    if not (len(code) == 6 and code.isdigit()):
        return HTMLResponse(not_found_page(code), status_code=404)
    try:
        rows = sb_get("/posts", {"id": f"eq.{code}", "limit": 1})
    except Exception as e:
        return HTMLResponse(page("Ошибка", f"""
<div class="container"><div class="card">
  <h2 style="margin-top:0">Не удалось получить пост</h2>
  <p style="color:#7b8794">{esc(str(e))}</p>
  <a class="btn btn-primary" href="/" style="margin-top:12px">На главную</a>
</div></div>"""), status_code=500)
    if not rows:
        return HTMLResponse(not_found_page(code), status_code=404)
    return post_page(row_to_post(rows[0]))

# ================= STARTUP LOG =================
@app.on_event("startup")
def on_startup():
    print("=" * 60)
    print("SLD Posts server started")
    print("env:", env_state())
    print("=" * 60)
