"""
СЛД — FastAPI + Supabase backend (всё в одном файле, включая фронтенд).

Переменные окружения:
    SUPABASE_URL  — Project URL
    SUPABASE_KEY  — service_role key (secret)
    JWT_SECRET    — (опционально) секрет для подписи сессионных токенов
"""

import io
import os
import uuid
import secrets
import logging
from datetime import datetime, timedelta
from typing import Optional, List

import httpx
import jwt
from fastapi import FastAPI, Request, Response, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from supabase import create_client, Client
from PIL import Image

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sld")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
JWT_SECRET   = os.environ.get("JWT_SECRET", secrets.token_hex(32))
JWT_ALG      = "HS256"
COOKIE_NAME  = "sld_token"
COOKIE_DAYS  = 30

# Ограничения
MAX_TEXT_LEN       = 10000
MAX_TITLE_LEN      = 200
MAX_TAGS           = 20
MAX_TAG_LEN        = 30
MAX_IMAGES         = 5
MAX_POST_IMAGE_B   = 95 * 1024     # 95 KB × 5 = 475 KB
MAX_AVATAR_B       = 150 * 1024    # 150 KB
AVATAR_DIM         = 512
POST_IMAGE_DIM     = 1024

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="СЛД", docs_url="/api/docs", redoc_url=None)


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #
def make_token(user_id: str, username: str) -> str:
    return jwt.encode(
        {"sub": user_id, "username": username,
         "exp": datetime.utcnow() + timedelta(days=COOKIE_DAYS)},
        JWT_SECRET, algorithm=JWT_ALG,
    )


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        return None


def current_user(request: Request) -> Optional[dict]:
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return None
    payload = decode_token(tok)
    if not payload:
        return None
    return {"id": payload["sub"], "username": payload["username"]}


def require_user(user: Optional[dict] = Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(401, "Не авторизован")
    return user


def set_auth_cookie(response: Response, token: str):
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=COOKIE_DAYS * 24 * 3600,
        httponly=True, samesite="lax", secure=False, path="/",
    )


def clear_auth_cookie(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")


# --------------------------------------------------------------------------- #
# Supabase auth via REST (service role)
# --------------------------------------------------------------------------- #
AUTH_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


async def sb_signup(email: str, password: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as cx:
        r = await cx.post(f"{SUPABASE_URL}/auth/v1/admin/users",
                          headers=AUTH_HEADERS,
                          json={"email": email, "password": password,
                                "email_confirm": True})
        if r.status_code >= 400:
            try:
                j = r.json()
                msg = j.get("msg") or j.get("message") or j.get("error_description") or r.text
            except Exception:
                msg = r.text
            raise HTTPException(400, f"Ошибка регистрации: {msg}")
        return r.json()


async def sb_login(email: str, password: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as cx:
        r = await cx.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        if r.status_code >= 400:
            raise HTTPException(400, "Неверное имя пользователя или пароль")
        return r.json()


# --------------------------------------------------------------------------- #
# Image compression
# --------------------------------------------------------------------------- #
def compress_image(data: bytes, max_dim: int, max_size: int):
    """
    Сжимает картинку до max_dim по длинной стороне и <= max_size байт.
    Порядок: AVIF → WebP → JPEG.
    Возвращает (bytes, mime, ext).
    """
    img = Image.open(io.BytesIO(data))
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    candidates = [("AVIF", "image/avif", "avif"),
                  ("WEBP", "image/webp", "webp"),
                  ("JPEG", "image/jpeg", "jpg")]

    for fmt, mime, ext in candidates:
        try:
            for q in (82, 72, 62, 52, 42, 34, 28):
                buf = io.BytesIO()
                kw = {"quality": q}
                if fmt == "AVIF":
                    kw["method"] = 4
                elif fmt == "WEBP":
                    kw["method"] = 6
                else:
                    kw["optimize"] = True
                img.save(buf, format=fmt, **kw)
                if buf.tell() <= max_size:
                    return buf.getvalue(), mime, ext
        except Exception as e:
            log.warning("format %s failed: %s", fmt, e)
            continue

    # Fallback — любой JPEG
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=30, optimize=True)
    return buf.getvalue(), "image/jpeg", "jpg"


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class AuthIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class PostIn(BaseModel):
    title: str = Field(default="", max_length=MAX_TITLE_LEN)
    text:  str = Field(default="", max_length=MAX_TEXT_LEN)
    tags:  List[str] = []
    font:  str = "font-serif-custom"
    image_urls: List[str] = []


class ProfileIn(BaseModel):
    name: str = ""
    avatar: str = "^_^"
    avatar_url: str = ""
    bio: str = Field(default="", max_length=1000)
    status: str = Field(default="", max_length=60)


# --------------------------------------------------------------------------- #
# Auth endpoints
# --------------------------------------------------------------------------- #
def _email(username: str) -> str:
    return f"{username.lower()}@sld.local"


@app.post("/api/auth/signup")
async def signup(body: AuthIn, response: Response):
    uname = body.username.strip()
    if not uname.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "Только буквы, цифры, _ и -")

    existing = supabase.table("profiles").select("id").eq("username", uname).execute()
    if existing.data:
        raise HTTPException(400, "Это имя уже занято")

    try:
        user = await sb_signup(_email(uname), body.password)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Ошибка: {e}")

    uid = user.get("id") or user.get("user", {}).get("id")
    if not uid:
        raise HTTPException(500, "Не удалось создать пользователя")

    try:
        supabase.table("profiles").insert({
            "id": uid, "username": uname, "name": uname,
            "avatar": "^_^", "bio": "", "status": "", "avatar_url": ""
        }).execute()
    except Exception as e:
        log.warning("profile insert failed: %s", e)

    set_auth_cookie(response, make_token(uid, uname))
    return {"ok": True, "id": uid, "username": uname}


@app.post("/api/auth/login")
async def login(body: AuthIn, response: Response):
    uname = body.username.strip()
    result = await sb_login(_email(uname), body.password)

    uid = result.get("user", {}).get("id")
    if not uid:
        raise HTTPException(500, "Не удалось получить профиль")

    prof = supabase.table("profiles").select("*").eq("id", uid).execute()
    if not prof.data:
        supabase.table("profiles").insert({
            "id": uid, "username": uname, "name": uname, "avatar": "^_^",
        }).execute()

    set_auth_cookie(response, make_token(uid, uname))
    return {"ok": True, "id": uid, "username": uname}


@app.post("/api/auth/logout")
async def logout(response: Response):
    clear_auth_cookie(response)
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: Optional[dict] = Depends(current_user)):
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "id": user["id"], "username": user["username"]}


# --------------------------------------------------------------------------- #
# Posts
# --------------------------------------------------------------------------- #
def _row_to_post(row: dict) -> dict:
    return {
        "id": row["id"],
        "author": row["author_username"],
        "author_id": row["author_id"],
        "title": row.get("title") or "",
        "text": row.get("text") or "",
        "tags": row.get("tags") or [],
        "font": row.get("font") or "font-serif-custom",
        "images": row.get("image_urls") or [],
        "timestamp": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _clean_tags(tags: List[str]) -> List[str]:
    out = []
    for t in tags:
        t = (t or "").strip().lower()
        if not t:
            continue
        if len(t) > MAX_TAG_LEN:
            t = t[:MAX_TAG_LEN]
        if t not in out:
            out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out


@app.get("/api/posts")
async def list_posts():
    res = supabase.table("posts").select("*").order("created_at", desc=True).limit(500).execute()
    return [_row_to_post(r) for r in (res.data or [])]


@app.get("/api/posts/{post_id}")
async def get_post(post_id: str):
    res = supabase.table("posts").select("*").eq("id", post_id).limit(1).execute()
    if not res.data:
        raise HTTPException(404, "Пост не найден")
    return _row_to_post(res.data[0])


@app.post("/api/posts")
async def create_post(body: PostIn, user: dict = Depends(require_user)):
    if len(body.text) > MAX_TEXT_LEN:
        raise HTTPException(400, f"Текст слишком длинный (макс {MAX_TEXT_LEN})")
    if len(body.image_urls) > MAX_IMAGES:
        raise HTTPException(400, f"Максимум {MAX_IMAGES} фото на пост")

    row = {
        "author_id": user["id"],
        "author_username": user["username"],
        "title": body.title.strip()[:MAX_TITLE_LEN],
        "text": body.text,
        "tags": _clean_tags(body.tags),
        "font": body.font,
        "image_urls": body.image_urls,
    }
    res = supabase.table("posts").insert(row).execute()
    return _row_to_post(res.data[0])


@app.put("/api/posts/{post_id}")
async def update_post(post_id: str, body: PostIn, user: dict = Depends(require_user)):
    if len(body.text) > MAX_TEXT_LEN:
        raise HTTPException(400, f"Текст слишком длинный (макс {MAX_TEXT_LEN})")
    if len(body.image_urls) > MAX_IMAGES:
        raise HTTPException(400, f"Максимум {MAX_IMAGES} фото на пост")

    check = supabase.table("posts").select("author_id").eq("id", post_id).limit(1).execute()
    if not check.data:
        raise HTTPException(404, "Пост не найден")
    if check.data[0]["author_id"] != user["id"]:
        raise HTTPException(403, "Нет прав")

    row = {
        "title": body.title.strip()[:MAX_TITLE_LEN],
        "text": body.text,
        "tags": _clean_tags(body.tags),
        "font": body.font,
        "image_urls": body.image_urls,
    }
    res = supabase.table("posts").update(row).eq("id", post_id).execute()
    return _row_to_post(res.data[0])


@app.delete("/api/posts/{post_id}")
async def delete_post(post_id: str, user: dict = Depends(require_user)):
    check = supabase.table("posts").select("author_id").eq("id", post_id).limit(1).execute()
    if not check.data:
        raise HTTPException(404, "Пост не найден")
    if check.data[0]["author_id"] != user["id"]:
        raise HTTPException(403, "Нет прав")
    supabase.table("posts").delete().eq("id", post_id).execute()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #
@app.get("/api/profiles/{username}")
async def get_profile(username: str):
    res = supabase.table("profiles").select("*").eq("username", username).limit(1).execute()
    if not res.data:
        return {"username": username, "name": username, "avatar": "^_^",
                "avatar_url": "", "bio": "", "status": ""}
    p = res.data[0]
    return {
        "id": p["id"], "username": p["username"],
        "name": p.get("name") or p["username"],
        "avatar": p.get("avatar") or "^_^",
        "avatar_url": p.get("avatar_url") or "",
        "bio": p.get("bio") or "",
        "status": p.get("status") or "",
    }


@app.get("/api/profiles")
async def all_profiles():
    res = supabase.table("profiles").select("*").execute()
    out = {}
    for p in (res.data or []):
        out[p["username"]] = {
            "name": p.get("name") or p["username"],
            "avatar": p.get("avatar") or "^_^",
            "avatar_url": p.get("avatar_url") or "",
            "bio": p.get("bio") or "",
            "status": p.get("status") or "",
        }
    return out


@app.put("/api/profiles/me")
async def update_my_profile(body: ProfileIn, user: dict = Depends(require_user)):
    if len(body.avatar) > 5:
        raise HTTPException(400, "Символьный аватар — до 5 знаков")
    if len(body.status) > 60:
        raise HTTPException(400, "Статус — до 60 символов")

    upd = {
        "name": body.name.strip()[:64] or user["username"],
        "avatar": body.avatar or "^_^",
        "avatar_url": body.avatar_url,
        "bio": body.bio[:1000],
        "status": body.status[:60],
    }
    res = supabase.table("profiles").update(upd).eq("id", user["id"]).execute()
    if not res.data:
        upd["id"] = user["id"]
        upd["username"] = user["username"]
        supabase.table("profiles").insert(upd).execute()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #
@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    kind: str = Form("post"),
    user: dict = Depends(require_user),
):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Пустой файл")

    if kind == "avatar":
        data, mime, ext = compress_image(raw, AVATAR_DIM, MAX_AVATAR_B)
    else:
        data, mime, ext = compress_image(raw, POST_IMAGE_DIM, MAX_POST_IMAGE_B)

    key = f"{user['id']}/{uuid.uuid4().hex}.{ext}"

    try:
        supabase.storage.from_("media").upload(
            key, data, {"content-type": mime, "upsert": "true"}
        )
    except Exception as e:
        raise HTTPException(500, f"Ошибка загрузки: {e}")

    url = supabase.storage.from_("media").get_public_url(key)
    return {"url": url, "size": len(data), "mime": mime}


# --------------------------------------------------------------------------- #
# Stats (Статус БД)
# --------------------------------------------------------------------------- #
@app.get("/api/stats")
async def stats():
    try:
        res = supabase.rpc("get_usage_stats").execute()
        data = res.data
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            data = {}
        return {
            "db_bytes":       int(data.get("db_bytes", 0)),
            "posts_bytes":    int(data.get("posts_bytes", 0)),
            "profiles_bytes": int(data.get("profiles_bytes", 0)),
            "storage_bytes":  int(data.get("storage_bytes", 0)),
            "storage_files":  int(data.get("storage_files", 0)),
        }
    except Exception as e:
        log.exception("stats error")
        raise HTTPException(500, f"Не удалось получить статистику: {e}")


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru" class="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>СЛД</title>
<meta name="description" content="СЛД — тексты, заметки и мысли.">
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config = { darkMode: 'class' };</script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;700&family=Inter:wght@400;500;600;700&family=Lora:ital,wght@0,400;0,500;1,400&family=Playfair+Display:ital,wght@0,400;0,600;1,400&family=JetBrains+Mono:wght@400;500;700&family=Caveat:wght@500;700&family=Montserrat:wght@400;500;600&family=Merriweather:ital,wght@0,300;0,400;1,300&display=swap');

* { box-sizing: border-box; }
html, body { max-width: 100vw; overflow-x: hidden; }
body {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  -webkit-font-smoothing: antialiased;
  -webkit-text-size-adjust: 100%;
}
img { max-width: 100%; height: auto; }

.font-sans-custom    { font-family:'Inter',sans-serif }
.font-serif-custom   { font-family:'Lora',serif }
.font-mono-custom    { font-family:'Fira Code',monospace }
.font-playfair       { font-family:'Playfair Display',serif }
.font-jetbrains      { font-family:'JetBrains Mono',monospace }
.font-caveat         { font-family:'Caveat',cursive; font-size:1.35rem; line-height:1.35 }
.font-montserrat     { font-family:'Montserrat',sans-serif }
.font-merriweather   { font-family:'Merriweather',serif }

/* ------ ФИКС textarea: только последние символы видны при повторяющихся буквах ------ */
textarea, .editable {
  overflow-x: hidden !important;
  overflow-y: auto;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: break-word;
  word-break: break-word;
  transform: translateZ(0);
  backface-visibility: hidden;
  -webkit-font-smoothing: antialiased;
  letter-spacing: normal;
  font-kerning: normal;
}
textarea.font-caveat { font-size: 1rem; line-height: 1.5; }

::-webkit-scrollbar { width:6px; height:6px }
::-webkit-scrollbar-track { background:transparent }
::-webkit-scrollbar-thumb { background:#cbd5e1; border-radius:9999px }
.dark ::-webkit-scrollbar-thumb { background:#334155 }
.no-scrollbar::-webkit-scrollbar { display:none }
.no-scrollbar { -ms-overflow-style:none; scrollbar-width:none }

.animate-fade-in { animation: fadeIn .25s cubic-bezier(.16,1,.3,1) forwards }
.animate-pop-in  { animation: popIn .2s cubic-bezier(.16,1,.3,1) forwards }
@keyframes fadeIn { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
@keyframes popIn  { from{opacity:0;transform:scale(.96)} to{opacity:1;transform:scale(1)} }

.spinner {
  width:46px; height:46px;
  border:3px solid rgba(148,163,184,.22);
  border-top-color:#3b82f6;
  border-radius:50%;
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg) } }

/* Иконки-кнопки в предпросмотре */
.icon-btn {
  display:inline-flex; align-items:center; justify-content:center;
  width: 42px; height: 42px;
  border-radius: 12px;
  background: rgba(255,255,255,.1);
  color: #fff;
  transition: background .15s ease;
}
.icon-btn:hover  { background: rgba(255,255,255,.22) }
.icon-btn:active { transform: scale(.95) }
.icon-btn svg    { width: 20px; height: 20px; stroke-width: 2 }

@media (max-width: 380px) {
  .icon-btn { width: 36px; height: 36px }
  .icon-btn svg { width: 16px; height: 16px }
}
</style>
</head>
<body class="bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100 transition-colors duration-200 flex flex-col min-h-screen pb-20 sm:pb-0">

<!-- ============ Loading overlay ============ -->
<div id="loadingOverlay" class="fixed inset-0 bg-gray-50 dark:bg-gray-950 z-[300] flex flex-col items-center justify-center gap-4">
  <div class="spinner"></div>
  <div class="text-xs text-gray-400 font-medium tracking-widest uppercase">Загрузка СЛД…</div>
</div>

<!-- ============ Toast ============ -->
<div id="toast" class="fixed top-5 left-1/2 -translate-x-1/2 sm:top-auto sm:bottom-6 sm:left-auto sm:right-6 sm:translate-x-0 bg-gray-900/95 dark:bg-gray-100/95 backdrop-blur-md text-white dark:text-gray-900 px-5 py-3 rounded-2xl text-xs sm:text-sm font-medium opacity-0 pointer-events-none transition-all duration-300 z-[100] shadow-2xl max-w-[90vw] truncate">…</div>

<!-- ============ Image preview ============ -->
<div id="imagePreviewModal" class="fixed inset-0 bg-black/95 backdrop-blur-xl z-[200] flex-col hidden">
  <div class="flex justify-between items-center p-2 sm:p-4 text-white bg-black/40 shrink-0 gap-2">
    <div class="flex gap-1.5 sm:gap-2 flex-wrap">
      <button onclick="zoomPreview(1.3)" class="icon-btn" title="Приблизить">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3M11 8v6M8 11h6"/></svg>
      </button>
      <button onclick="zoomPreview(0.77)" class="icon-btn" title="Отдалить">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3M8 11h6"/></svg>
      </button>
      <button onclick="rotatePreview(-90)" class="icon-btn" title="Повернуть влево">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.7 9.7 0 0 0-6.7 2.7L3 8"/><path d="M3 3v5h5"/></svg>
      </button>
      <button onclick="rotatePreview(90)" class="icon-btn" title="Повернуть вправо">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-9-9 9.7 9.7 0 0 1 6.7 2.7L21 8"/><path d="M21 3v5h-5"/></svg>
      </button>
      <button onclick="resetPreview()" class="icon-btn" title="Сброс">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>
      </button>
    </div>
    <button onclick="closeImagePreview()" class="icon-btn" style="background:rgba(239,68,68,.55)" onmouseover="this.style.background='rgba(239,68,68,.85)'" onmouseout="this.style.background='rgba(239,68,68,.55)'" title="Закрыть">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M6 18L18 6M6 6l12 12"/></svg>
    </button>
  </div>
  <div class="flex-1 overflow-auto flex items-center justify-center p-4 relative">
    <img id="previewImage" class="max-h-full max-w-full object-contain select-none transition-transform duration-150 origin-center" draggable="false" alt="">
  </div>
</div>

<!-- ============ Auth modal ============ -->
<div id="authModal" class="fixed inset-0 bg-black/50 backdrop-blur-sm z-[90] flex items-center justify-center p-4 hidden">
  <div class="bg-white dark:bg-gray-900 rounded-3xl p-6 sm:p-8 max-w-sm w-full shadow-2xl border border-gray-100 dark:border-gray-800 relative animate-pop-in">
    <button id="closeAuthModal" class="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 p-2">
      <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 18L18 6M6 6l12 12"/></svg>
    </button>
    <h3 class="text-2xl font-semibold mb-1">Вход / Регистрация</h3>
    <p class="text-xs text-gray-500 dark:text-gray-400 mb-6">Введите имя пользователя и пароль.</p>
    <form id="authForm">
      <div class="space-y-3">
        <div>
          <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Имя пользователя</label>
          <input type="text" id="authUsername" required minlength="2" maxlength="32" autocomplete="username"
                 class="w-full text-sm bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2.5 outline-none focus:border-gray-400"
                 placeholder="username">
        </div>
        <div>
          <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Пароль (мин. 6)</label>
          <input type="password" id="authPassword" required minlength="6" maxlength="128" autocomplete="current-password"
                 class="w-full text-sm bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2.5 outline-none focus:border-gray-400"
                 placeholder="••••••••">
        </div>
      </div>
      <div id="authError" class="hidden mt-3 text-[11px] text-red-500 bg-red-50 dark:bg-red-950/30 px-3 py-2 rounded-xl"></div>
      <button type="submit" id="authSubmit"
              class="mt-5 w-full bg-gray-900 dark:bg-white text-white dark:text-gray-900 py-3 rounded-xl text-sm font-medium active:scale-[0.98] transition disabled:opacity-50 disabled:cursor-not-allowed">
        Войти / Создать
      </button>
    </form>
  </div>
</div>

<!-- ============ Error modal ============ -->
<div id="errorModal" class="fixed inset-0 bg-black/60 backdrop-blur-md z-[90] flex items-center justify-center p-4 hidden">
  <div class="bg-white dark:bg-gray-900 rounded-3xl p-6 max-w-sm w-full text-center shadow-2xl border border-gray-100 dark:border-gray-800 animate-pop-in">
    <div class="w-12 h-12 rounded-2xl bg-red-50 dark:bg-red-950/40 text-red-500 flex items-center justify-center mx-auto mb-3">
      <svg class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 8v5M12 17h.01"/><circle cx="12" cy="12" r="10"/></svg>
    </div>
    <h3 class="text-lg font-semibold mb-1">Запись не найдена</h3>
    <p class="text-xs text-gray-500 dark:text-gray-400 mb-5">Публикация удалена или не существовала.</p>
    <button id="closeErrorBtn" class="w-full bg-gray-900 dark:bg-white text-white dark:text-gray-900 py-2.5 rounded-xl text-xs font-medium">На главную</button>
  </div>
</div>

<!-- ============ Stats modal ============ -->
<div id="statsModal" class="fixed inset-0 bg-black/60 backdrop-blur-md z-[90] flex items-center justify-center p-4 hidden">
  <div class="bg-white dark:bg-gray-900 rounded-3xl p-6 sm:p-8 max-w-md w-full shadow-2xl border border-gray-100 dark:border-gray-800 relative animate-pop-in">
    <button onclick="closeStats()" class="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 p-2">
      <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 18L18 6M6 6l12 12"/></svg>
    </button>
    <div class="flex items-center gap-3 mb-6">
      <div class="w-11 h-11 rounded-xl bg-blue-50 dark:bg-blue-900/30 flex items-center justify-center text-blue-500">
        <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/></svg>
      </div>
      <div>
        <h3 class="text-lg font-semibold leading-tight">Статус хранилища</h3>
        <p class="text-[11px] text-gray-500">Supabase · Free Tier</p>
      </div>
    </div>

    <div id="statsLoader" class="py-8 text-center text-gray-400 text-xs">
      <div class="spinner mx-auto" style="width:32px;height:32px;border-width:2px"></div>
      <div class="mt-3">Загрузка…</div>
    </div>

    <div id="statsBody" class="hidden space-y-5">
      <div>
        <div class="mb-1.5 flex justify-between items-end">
          <span class="text-xs font-medium text-gray-700 dark:text-gray-300">База данных</span>
          <span class="text-[10px] font-semibold text-blue-500"><span id="dbPercent">0%</span></span>
        </div>
        <div class="w-full bg-gray-100 dark:bg-gray-800 rounded-full h-2 mb-1.5 overflow-hidden border border-gray-200 dark:border-gray-700">
          <div id="dbProgressBar" class="bg-blue-500 h-2 rounded-full transition-all duration-700 ease-out" style="width:0%"></div>
        </div>
        <div class="text-[10px] text-gray-500 text-right">
          <span id="dbUsedMB">0</span> / 500 МБ
        </div>
      </div>

      <div>
        <div class="mb-1.5 flex justify-between items-end">
          <span class="text-xs font-medium text-gray-700 dark:text-gray-300">Файловое хранилище</span>
          <span class="text-[10px] font-semibold text-purple-500"><span id="filePercent">0%</span></span>
        </div>
        <div class="w-full bg-gray-100 dark:bg-gray-800 rounded-full h-2 mb-1.5 overflow-hidden border border-gray-200 dark:border-gray-700">
          <div id="fileProgressBar" class="bg-purple-500 h-2 rounded-full transition-all duration-700 ease-out" style="width:0%"></div>
        </div>
        <div class="text-[10px] text-gray-500 text-right">
          <span id="fileUsedMB">0</span> / 1024 МБ · <span id="fileCount">0</span> файл(ов)
        </div>
      </div>

      <div class="pt-4 border-t border-gray-100 dark:border-gray-800 text-[10px] text-gray-400 space-y-1">
        <div class="flex justify-between"><span>Постов в БД:</span><span id="postsBytes" class="font-medium text-gray-600 dark:text-gray-300">0 КБ</span></div>
        <div class="flex justify-between"><span>Профилей в БД:</span><span id="profilesBytes" class="font-medium text-gray-600 dark:text-gray-300">0 КБ</span></div>
      </div>
    </div>
  </div>
</div>

<!-- ============ Header (PC/Tablet) ============ -->
<div class="max-w-3xl w-full mx-auto px-4 py-4 sm:py-8 flex-1 flex flex-col min-h-0">

  <header class="hidden sm:flex justify-between items-center pb-5 mb-6 border-b border-gray-200 dark:border-gray-800">
    <div>
      <a href="#/" onclick="event.preventDefault(); goHome()" class="text-2xl sm:text-3xl font-semibold tracking-tight hover:opacity-80">СЛД</a>
      <p class="text-gray-500 dark:text-gray-400 mt-0.5 text-xs sm:text-sm">ещё один текст</p>
    </div>
    <div id="desktopUserProfileArea" class="flex items-center gap-2"></div>
  </header>

  <!-- Мобильная шапка -->
  <div class="sm:hidden mb-4 flex justify-between items-center pb-3 border-b border-gray-200 dark:border-gray-800">
    <div>
      <a href="#/" onclick="event.preventDefault(); goHome()" class="text-xl font-bold">СЛД</a>
      <p class="text-gray-500 dark:text-gray-400 text-[10px]">ещё один текст</p>
    </div>
  </div>

  <main id="appContent" class="flex-1 min-h-0"></main>
</div>

<!-- ============ Mobile bottom nav ============ -->
<nav id="mobileBottomNav" class="sm:hidden fixed bottom-0 left-0 right-0 bg-white/90 dark:bg-gray-900/90 backdrop-blur-lg border-t border-gray-200/80 dark:border-gray-800/80 z-40 px-3 py-2 flex justify-around items-center shadow-[0_-4px_15px_rgba(0,0,0,0.05)]"
     style="padding-bottom: max(8px, env(safe-area-inset-bottom));"></nav>

<!-- ============ Footer (PC/Tablet) ============ -->
<footer class="hidden sm:block mt-10 border-t border-gray-200 dark:border-gray-800 py-6 text-center text-xs text-gray-500 dark:text-gray-400">
  <div class="max-w-3xl mx-auto px-4 flex flex-col sm:flex-row justify-between items-center gap-3">
    <div><span class="font-semibold text-gray-800 dark:text-gray-200">СЛД</span> &copy; 2026</div>
    <div class="flex items-center gap-3 sm:gap-4 flex-wrap justify-center">
      <a href="#/" onclick="event.preventDefault(); goHome()" class="hover:underline">Главная</a>
      <span class="text-gray-300 dark:text-gray-700">·</span>
      <a href="#profile" onclick="if(!currentUser){event.preventDefault();openAuth('#profile')}" class="hover:underline">Профиль</a>
      <span class="text-gray-300 dark:text-gray-700">·</span>
      <a href="#create" onclick="if(!currentUser){event.preventDefault();openAuth('#create')}" class="hover:underline">Создать пост</a>
      <span class="text-gray-300 dark:text-gray-700">·</span>
      <a href="#" onclick="event.preventDefault(); openStats();" class="hover:text-blue-500 font-medium">Статус БД</a>
    </div>
    <div>powered by <span class="font-semibold">Supabase</span> &amp; <span class="font-semibold">FastAPICloud</span></div>
  </div>
</footer>

<script>
// =============================================================
//  State
// =============================================================
let currentUser = null;
let currentUserId = null;
let database = [];
let profilesData = {};
let currentSortOrder = 'new';
let pendingImages = [];
let isDataLoaded = false;
let editingPostId = null;
let activeCreateFont = 'font-serif-custom';
let previewScale = 1, previewRotation = 0;
let pendingHash = null;

const MAX_TEXT = 10000;
const MAX_IMAGES = 5;

// =============================================================
//  Helpers
// =============================================================
async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let detail = 'Ошибка ' + res.status;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

function sanitizeHTML(str) {
  if (str == null) return '';
  return String(str).replace(/[&<>'"]/g, t => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[t]||t));
}

function esc(s) { return sanitizeHTML(s); }
function escAttr(s) { return sanitizeHTML(s).replace(/`/g,'&#96;'); }

function showToast(text) {
  const t = document.getElementById('toast');
  t.innerText = text;
  t.classList.remove('opacity-0','pointer-events-none');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => t.classList.add('opacity-0','pointer-events-none'), 2600);
}

function fmtDate(s) {
  if (!s) return '';
  try { return new Date(s).toLocaleString('ru-RU',{day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'}); }
  catch { return String(s); }
}

function formatPostText(text) {
  if (!text) return '';
  let p = sanitizeHTML(text);
  p = p.replace(/!([a-zA-Z0-9_\-]{1,32})/g,
    '<a href="#/@$1" onclick="event.stopPropagation(); navigateToProfile(\'$1\'); return false;" class="text-blue-500 font-medium hover:underline">!$1</a>');
  return p.replace(/\n/g, '<br>');
}

function goHome() { location.hash = ''; router(); }
function navigateToProfile(u) { location.hash = '/@' + u; }
window.navigateToProfile = navigateToProfile;

// =============================================================
//  Image preview
// =============================================================
window.openImagePreview = function(url) {
  const m = document.getElementById('imagePreviewModal');
  document.getElementById('previewImage').src = url;
  previewScale = 1; previewRotation = 0; updatePreviewTransform();
  m.classList.remove('hidden'); m.classList.add('flex');
  document.body.style.overflow = 'hidden';
};
window.closeImagePreview = function() {
  const m = document.getElementById('imagePreviewModal');
  m.classList.add('hidden'); m.classList.remove('flex');
  document.body.style.overflow = '';
  document.getElementById('previewImage').src = '';
};
window.zoomPreview = f => {
  previewScale = Math.min(8, Math.max(0.3, previewScale * f));
  updatePreviewTransform();
};
window.rotatePreview = d => {
  previewRotation += d;
  updatePreviewTransform();
};
window.resetPreview = () => {
  previewScale = 1; previewRotation = 0; updatePreviewTransform();
};
function updatePreviewTransform() {
  document.getElementById('previewImage').style.transform =
    `scale(${previewScale}) rotate(${previewRotation}deg)`;
}
window.downloadImage = async function(url, index) {
  try {
    const r = await fetch(url, { mode: 'cors' });
    const b = await r.blob();
    const u = URL.createObjectURL(b);
    const a = document.createElement('a');
    a.href = u; a.download = `SLD_${index+1}.jpg`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(u), 1000);
    showToast('Загрузка начата');
  } catch {
    window.open(url, '_blank');
  }
};

// =============================================================
//  Carousel
// =============================================================
function generateCarouselHTML(postId, images) {
  if (!images || !images.length) return '';
  const slides = images.map((url, idx) => `
    <div class="w-full min-w-full flex-shrink-0 snap-center relative flex items-center justify-center cursor-pointer overflow-hidden p-1" onclick="openImagePreview('${escAttr(url)}')">
      <img src="${escAttr(url)}" class="max-h-[55vh] sm:max-h-[65vh] max-w-full object-contain rounded-xl bg-gray-100 dark:bg-gray-900" loading="lazy" alt="">
      <button onclick="event.stopPropagation(); downloadImage('${escAttr(url)}', ${idx})" title="Скачать"
              class="absolute top-2 right-2 bg-black/55 hover:bg-black/85 text-white p-2 rounded-xl backdrop-blur-sm">
        <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
      </button>
      ${images.length>1 ? `<div class="absolute bottom-3 left-1/2 -translate-x-1/2 bg-black/60 text-white text-[10px] px-3 py-1 rounded-full">${idx+1}/${images.length}</div>` : ''}
    </div>`).join('');

  return `
    <div class="relative bg-gray-100/50 dark:bg-[#0a0a0a] rounded-2xl mb-4 overflow-hidden border border-gray-100 dark:border-gray-800/50 group">
      <div id="carousel-${postId}" class="flex w-full overflow-x-auto snap-x snap-mandatory scroll-smooth no-scrollbar">${slides}</div>
      ${images.length>1 ? `
        <button onclick="event.stopPropagation(); const c=document.getElementById('carousel-${postId}'); c.scrollBy({left:-c.clientWidth,behavior:'smooth'})"
                class="hidden sm:flex absolute left-2 top-1/2 -translate-y-1/2 bg-white/85 dark:bg-black/60 hover:bg-white dark:hover:bg-black p-2 rounded-full opacity-0 group-hover:opacity-100 transition z-10">
          <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 19l-7-7 7-7"/></svg>
        </button>
        <button onclick="event.stopPropagation(); const c=document.getElementById('carousel-${postId}'); c.scrollBy({left:c.clientWidth,behavior:'smooth'})"
                class="hidden sm:flex absolute right-2 top-1/2 -translate-y-1/2 bg-white/85 dark:bg-black/60 hover:bg-white dark:hover:bg-black p-2 rounded-full opacity-0 group-hover:opacity-100 transition z-10">
          <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 5l7 7-7 7"/></svg>
        </button>` : ''}
    </div>`;
}

// =============================================================
//  Data loading
// =============================================================
async function fetchAllData() {
  try {
    const [posts, profiles] = await Promise.all([
      api('/api/posts'),
      api('/api/profiles'),
    ]);
    database = posts.map(p => ({ ...p, timestamp: fmtDate(p.timestamp) }));
    profilesData = profiles || {};
  } catch (e) {
    console.error('load error', e);
  }
  isDataLoaded = true;
  updateUserInterface();
  router();
}

// =============================================================
//  Router
// =============================================================
window.addEventListener('hashchange', router);

function router() {
  const hash = location.hash;
  updateMobileNav();

  if (hash === '#create' || hash.startsWith('#edit/')) {
    if (!currentUser) { openAuth(hash); return; }
    renderCreateOrEditPage();
    return;
  }
  if (hash.startsWith('#/@')) {
    renderProfilePage(decodeURIComponent(hash.substring(3)).trim(), false);
    return;
  }
  if (hash === '#profile' || hash === '#profile/edit') {
    if (!currentUser) { openAuth('#profile'); return; }
    renderProfilePage(currentUser, true);
    return;
  }
  if (hash.length > 1 && hash !== '#/') {
    const id = decodeURIComponent(hash.substring(1)).trim();
    let post = database.find(p => p.id === id);
    if (post) { renderSinglePost(post); return; }
    api(`/api/posts/${id}`).then(p => {
      p.timestamp = fmtDate(p.timestamp);
      if (!database.find(x => x.id === p.id)) database.unshift(p);
      renderSinglePost(p);
    }).catch(() => showPostNotFoundError());
    return;
  }
  renderMainFeed();
}

function showPostNotFoundError() {
  const m = document.getElementById('errorModal');
  m.classList.remove('hidden');
  document.getElementById('closeErrorBtn').onclick = () => { m.classList.add('hidden'); goHome(); };
}

// =============================================================
//  Mobile nav
// =============================================================
function updateMobileNav() {
  const nav = document.getElementById('mobileBottomNav');
  const hash = location.hash;

  const home = `<a href="#/" onclick="event.preventDefault();goHome()"
    class="flex flex-col items-center gap-0.5 flex-1 py-1 ${(!hash||hash==='#/'||hash==='#')?'text-gray-900 dark:text-white font-semibold':'text-gray-400'}">
    <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 12l2-2 7-7 7 7M5 10v10h4v-6h6v6h4V10"/>
    </svg>
    <span class="text-[10px] font-medium">Главная</span></a>`;

  const create = `<a href="#create" onclick="if(!currentUser){event.preventDefault();openAuth('#create')}"
    class="flex flex-col items-center gap-0.5 flex-1 py-1 ${hash==='#create'?'text-gray-900 dark:text-white font-semibold':'text-gray-400'}">
    <div class="w-9 h-9 rounded-xl bg-gray-900 dark:bg-white text-white dark:text-gray-900 flex items-center justify-center -mt-3 shadow-md">
      <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
    </div>
    <span class="text-[10px] font-medium">Создать</span></a>`;

  const stats = `<button onclick="openStats()"
    class="flex flex-col items-center gap-0.5 flex-1 py-1 text-gray-400">
    <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>
    </svg>
    <span class="text-[10px] font-medium">Статус</span></button>`;

  const profile = currentUser
    ? `<a href="#profile" class="flex flex-col items-center gap-0.5 flex-1 py-1 ${hash.startsWith('#profile')?'text-gray-900 dark:text-white font-semibold':'text-gray-400'}">
        <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0zM12 14a7 7 0 0 0-7 7h14a7 7 0 0 0-7-7z"/>
        </svg>
        <span class="text-[10px] font-medium">Профиль</span></a>`
    : `<button onclick="openAuth('#profile')" class="flex flex-col items-center gap-0.5 flex-1 py-1 text-gray-400">
        <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M11 16l-4-4m0 0l4-4m-4 4h14M18 20v1a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3h7a3 3 0 0 1 3 3v1"/>
        </svg>
        <span class="text-[10px] font-medium">Вход</span></button>`;

  nav.innerHTML = home + create + stats + profile;
}

// =============================================================
//  Main feed
// =============================================================
function renderMainFeed() {
  document.title = 'СЛД';
  const content = document.getElementById('appContent');
  content.innerHTML = `
    ${currentUser ? `
    <section class="hidden sm:block bg-white dark:bg-gray-900 rounded-3xl shadow-sm border border-gray-100 dark:border-gray-800 p-5 mb-6">
      <div class="flex items-center justify-between gap-3">
        <p class="text-sm text-gray-500">Хотите поделиться мыслью?</p>
        <a href="#create" class="shrink-0 bg-gray-900 dark:bg-white text-white dark:text-gray-900 px-5 py-2.5 rounded-xl text-sm font-medium">Написать пост</a>
      </div>
    </section>` : ''}

    <div class="flex flex-col sm:flex-row justify-between items-stretch sm:items-center mb-5 gap-3">
      <h2 class="text-base sm:text-lg font-medium">Записи <span id="statTotal" class="text-gray-400 text-sm ml-1 font-normal">(0)</span></h2>
      <div class="flex flex-wrap items-center gap-2">
        <select id="sortSelect" class="text-[11px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 outline-none font-medium">
          <option value="new">Сначала новые</option>
          <option value="old">Сначала старые</option>
        </select>
        <input type="text" id="searchInput" placeholder="Поиск…"
               class="text-[11px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 outline-none placeholder-gray-400 flex-1 sm:w-52 min-w-0">
      </div>
    </div>

    <div id="dbContainer" class="space-y-4 sm:space-y-5"></div>`;

  document.getElementById('sortSelect').addEventListener('change', e => {
    currentSortOrder = e.target.value; renderDBList();
  });
  document.getElementById('searchInput').addEventListener('input', renderDBList);
  renderDBList();
}

function renderDBList() {
  const box = document.getElementById('dbContainer');
  const stat = document.getElementById('statTotal');
  if (!box) return;
  const q = (document.getElementById('searchInput')?.value || '').toLowerCase();

  let filtered = database.filter(e =>
    ((e.title||'')+' '+e.text+' '+(e.tags||[]).join(' ')+' '+e.author).toLowerCase().includes(q)
  );

  filtered.sort((a,b) => {
    const ta = a.timestamp || '';
    const tb = b.timestamp || '';
    if (currentSortOrder === 'new') return tb.localeCompare(ta) || (b.id||'').localeCompare(a.id||'');
    return ta.localeCompare(tb) || (a.id||'').localeCompare(b.id||'');
  });

  if (!filtered.length) {
    box.innerHTML = `<div class="text-center py-12 text-gray-400 text-xs">Записей не найдено</div>`;
    if (stat) stat.innerText = '(0)';
    return;
  }

  box.innerHTML = filtered.map(entry => {
    const tags = (entry.tags||[]).map(t =>
      `<span class="inline-block bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px] px-2.5 py-0.5 rounded-full cursor-pointer hover:bg-gray-200 dark:hover:bg-gray-700"
             onclick="event.stopPropagation();setSearch('${escAttr(t)}')">#${esc(t)}</span>`
    ).join('');
    const car = generateCarouselHTML(entry.id, entry.images);
    const prof = profilesData[entry.author] || { avatar: '^_^', name: entry.author, avatar_url: '' };
    const isOwn = entry.author === currentUser;
    const av = prof.avatar_url
      ? `<img src="${escAttr(prof.avatar_url)}" class="w-9 h-9 rounded-xl object-cover" alt="">`
      : `<div class="w-9 h-9 rounded-xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono-custom text-xs font-bold">${esc(prof.avatar)}</div>`;

    return `
    <article id="post-${entry.id}" onclick="location.hash='${escAttr(entry.id)}'"
             class="bg-white dark:bg-gray-900 rounded-2xl sm:rounded-3xl p-4 sm:p-6 shadow-sm border border-gray-100 dark:border-gray-800 hover:shadow-md transition cursor-pointer animate-fade-in">
      <div class="flex justify-between items-center mb-3 sm:mb-4 gap-2">
        <div class="flex items-center gap-3 min-w-0">
          <div onclick="event.stopPropagation();navigateToProfile('${escAttr(entry.author)}')" class="shrink-0">${av}</div>
          <div class="min-w-0">
            <span onclick="event.stopPropagation();navigateToProfile('${escAttr(entry.author)}')"
                  class="text-sm font-semibold hover:underline cursor-pointer block truncate">
              ${esc(prof.name)} <span class="font-normal text-gray-500">(@${esc(entry.author)})</span>
            </span>
            <span class="text-[10px] text-gray-400 block">${esc(entry.timestamp)}</span>
          </div>
        </div>
        <div class="flex items-center gap-1.5 shrink-0">
          ${isOwn ? `<button onclick="event.stopPropagation();location.hash='edit/${escAttr(entry.id)}'" title="Редактировать"
                            class="text-gray-400 hover:text-blue-500 bg-gray-50 dark:bg-gray-800 p-2 rounded-xl transition">
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
              <path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
          </button>` : ''}
          <button onclick="event.stopPropagation();copyPostLink('${escAttr(entry.id)}')" title="Копировать ссылку"
                  class="text-gray-400 hover:text-gray-700 bg-gray-50 dark:bg-gray-800 p-2 rounded-xl transition">
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect width="14" height="14" x="8" y="8" rx="2"/>
              <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>
            </svg>
          </button>
        </div>
      </div>
      ${entry.title ? `<h3 class="text-base sm:text-lg font-bold mb-2 break-words">${esc(entry.title)}</h3>` : ''}
      ${car}
      ${entry.text ? `<div class="text-sm sm:text-base ${esc(entry.font)} leading-relaxed mb-2 line-clamp-4 break-words">${formatPostText(entry.text)}</div>` : ''}
      ${tags ? `<div class="flex flex-wrap gap-1.5 pt-2.5 border-t border-gray-50 dark:border-gray-800 mt-2">${tags}</div>` : ''}
    </article>`;
  }).join('');

  if (stat) stat.innerText = `(${filtered.length})`;
}

window.setSearch = function(q) {
  location.hash = '';
  setTimeout(() => {
    const i = document.getElementById('searchInput');
    if (i) { i.value = q; renderDBList(); }
  }, 60);
};

// =============================================================
//  Single post
// =============================================================
function renderSinglePost(entry) {
  document.title = `Запись от @${entry.author} — СЛД`;
  const prof = profilesData[entry.author] || { avatar: '^_^', name: entry.author, avatar_url: '' };
  const car = generateCarouselHTML(entry.id, entry.images);
  const tags = (entry.tags||[]).map(t =>
    `<span class="inline-block bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-xs px-3 py-1.5 rounded-full cursor-pointer hover:bg-gray-200 dark:hover:bg-gray-700"
           onclick="setSearch('${escAttr(t)}')">#${esc(t)}</span>`
  ).join('');
  const av = prof.avatar_url
    ? `<img src="${escAttr(prof.avatar_url)}" class="w-11 h-11 rounded-2xl object-cover" alt="">`
    : `<div class="w-11 h-11 rounded-2xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono-custom text-sm font-bold">${esc(prof.avatar)}</div>`;
  const isOwn = entry.author === currentUser;

  document.getElementById('appContent').innerHTML = `
    <div class="mb-4 sm:mb-6">
      <a href="#/" onclick="event.preventDefault();goHome()"
         class="inline-flex items-center gap-2 px-3 py-2 rounded-2xl bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800 transition">
        <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
        Назад к ленте
      </a>
    </div>
    <div class="bg-white dark:bg-gray-900 rounded-2xl sm:rounded-3xl p-4 sm:p-8 shadow-sm border border-gray-100 dark:border-gray-800 animate-fade-in">
      <div class="flex items-center justify-between mb-5 sm:mb-6 pb-4 border-b border-gray-100 dark:border-gray-800 gap-3 flex-wrap">
        <div class="flex items-center gap-3 cursor-pointer min-w-0" onclick="navigateToProfile('${escAttr(entry.author)}')">
          ${av}
          <div class="min-w-0">
            <span class="text-sm font-bold block truncate">${esc(prof.name)} <span class="font-normal text-gray-500">(@${esc(entry.author)})</span></span>
            <span class="text-[11px] text-gray-400 block mt-0.5">${esc(entry.timestamp)}</span>
          </div>
        </div>
        <div class="flex items-center gap-1.5">
          ${isOwn ? `
          <button onclick="location.hash='edit/${escAttr(entry.id)}'" title="Редактировать"
                  class="text-gray-400 hover:text-blue-500 bg-gray-50 dark:bg-gray-800 p-2.5 rounded-xl transition">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
              <path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
          </button>
          <button onclick="deletePost('${escAttr(entry.id)}', true)" title="Удалить"
                  class="text-red-400 hover:text-red-600 bg-red-50 dark:bg-red-950/30 p-2.5 rounded-xl transition">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 6h18M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>
            </svg>
          </button>` : ''}
          <button onclick="copyPostLink('${escAttr(entry.id)}')" title="Копировать"
                  class="text-gray-400 hover:text-gray-700 bg-gray-50 dark:bg-gray-800 p-2.5 rounded-xl transition">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect width="14" height="14" x="8" y="8" rx="2"/>
              <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>
            </svg>
          </button>
        </div>
      </div>
      ${entry.title ? `<h2 class="text-lg sm:text-2xl font-bold mb-4 break-words">${esc(entry.title)}</h2>` : ''}
      ${car}
      ${entry.text ? `<div class="text-base sm:text-lg ${esc(entry.font)} leading-relaxed mb-6 mt-2 break-words whitespace-pre-wrap">${formatPostText(entry.text)}</div>` : ''}
      ${tags ? `<div class="flex flex-wrap gap-2 pt-5 border-t border-gray-100 dark:border-gray-800 mt-4">${tags}</div>` : ''}
    </div>`;
}

// =============================================================
//  Create / Edit page
// =============================================================
function renderCreateOrEditPage() {
  const hash = location.hash;
  const isEdit = hash.startsWith('#edit/');
  editingPostId = isEdit ? hash.substring(6) : null;

  let post = null;
  if (isEdit) {
    post = database.find(p => p.id === editingPostId);
    if (!post) { showToast('Пост не найден'); goHome(); return; }
    if (post.author !== currentUser) { showToast('Нет прав'); goHome(); return; }
  }

  document.title = isEdit ? 'Редактирование — СЛД' : 'Новая запись — СЛД';
  activeCreateFont = post?.font || 'font-serif-custom';
  pendingImages = post ? [...(post.images || [])] : [];

  const fonts = [
    ['font-sans-custom','Sans'],['font-serif-custom','Serif'],['font-mono-custom','Mono'],
    ['font-playfair','Playfair'],['font-jetbrains','JetBrains'],['font-caveat','Caveat'],
    ['font-montserrat','Montserrat'],['font-merriweather','Merriweather']
  ];

  document.getElementById('appContent').innerHTML = `
    <div class="mb-4">
      <a href="#/" onclick="event.preventDefault();goHome()"
         class="inline-flex items-center gap-2 px-3 py-2 rounded-2xl bg-gray-100 dark:bg-gray-800 text-xs font-medium hover:bg-gray-200 dark:hover:bg-gray-700 transition">
        <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 19l-7-7 7-7"/></svg>
        Назад
      </a>
    </div>
    <section class="bg-white dark:bg-gray-900 rounded-2xl sm:rounded-3xl shadow-sm border border-gray-100 dark:border-gray-800 p-4 sm:p-6 animate-fade-in">
      <div class="flex justify-between items-center mb-4 border-b border-gray-100 dark:border-gray-800 pb-3 gap-2 flex-wrap">
        <h2 class="text-base font-semibold">${isEdit ? 'Редактирование' : 'Новая публикация'}</h2>
        <label id="photoLabel" class="cursor-pointer text-gray-600 dark:text-gray-300 hover:text-blue-500 dark:hover:text-blue-400 flex items-center gap-1.5 bg-gray-100 dark:bg-gray-800 px-3 py-1.5 rounded-xl text-[11px] font-medium transition">
          <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5L5 21"/>
          </svg>
          Фото
          <input type="file" id="imageUploadInput" accept="image/*" multiple class="hidden">
        </label>
      </div>

      <div id="createImagePreview" class="hidden mb-4">
        <div class="text-[10px] text-gray-400 mb-2">Фото: <span id="imageCount" class="font-medium text-gray-600 dark:text-gray-300">0</span> / 5</div>
        <div id="createImageSlider" class="flex gap-2 overflow-x-auto snap-x pb-2 no-scrollbar"></div>
      </div>

      <input type="text" id="postTitleInput" maxlength="200" value="${escAttr(post?.title||'')}"
             class="w-full text-base sm:text-lg font-bold bg-transparent outline-none placeholder-gray-400 mb-3 border-b border-transparent focus:border-gray-200 dark:focus:border-gray-800 transition-colors pb-1"
             placeholder="Заголовок (необязательно)">

      <div class="flex items-center justify-between mb-3 pb-3 border-b border-gray-100 dark:border-gray-800 gap-2 flex-wrap">
        <span class="text-xs text-gray-500 font-medium">Шрифт:</span>
        <div class="flex items-center bg-gray-100 dark:bg-gray-800 p-1 rounded-xl text-[11px] font-medium overflow-x-auto max-w-full no-scrollbar">
          ${fonts.map(([f,l]) => `<button type="button" data-font="${f}"
            class="create-font-option px-2.5 py-1 rounded-lg whitespace-nowrap transition ${f===activeCreateFont?'bg-white dark:bg-gray-700 shadow-sm':''}">${l}</button>`).join('')}
        </div>
      </div>

      <textarea id="dataInput" maxlength="${MAX_TEXT}"
                class="w-full h-40 sm:h-52 resize-none outline-none text-sm sm:text-base bg-transparent placeholder-gray-400 leading-relaxed ${activeCreateFont}"
                placeholder="Напишите текст… (можно вставить картинку Ctrl+V)">${esc(post?.text||'')}</textarea>

      <div class="text-right text-[10px] text-gray-400 mt-1">
        <span id="charCount">${(post?.text||'').length}</span> / ${MAX_TEXT}
      </div>

      <div class="flex flex-col sm:flex-row justify-between items-stretch sm:items-center mt-3 pt-3 border-t border-gray-100 dark:border-gray-800 gap-3">
        <input type="text" id="tagsInput" maxlength="600" value="${escAttr((post?.tags||[]).join(', '))}"
               class="outline-none text-xs sm:text-sm text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-800 px-3.5 py-2.5 rounded-xl w-full sm:flex-1 min-w-0"
               placeholder="Теги (через запятую, до 20)">
        <button id="saveBtn"
                class="bg-gray-900 dark:bg-white text-white dark:text-gray-900 px-6 py-2.5 rounded-xl text-xs sm:text-sm font-medium w-full sm:w-auto shrink-0 active:scale-[0.98] transition disabled:opacity-50">
          ${isEdit ? 'Сохранить' : 'Опубликовать'}
        </button>
      </div>
    </section>`;

  initCreateEvents();
  updateCreateImagePreview();
}

window.removePendingImage = function(i) {
  pendingImages.splice(i, 1);
  updateCreateImagePreview();
};

function updateCreateImagePreview() {
  const box = document.getElementById('createImagePreview');
  const slider = document.getElementById('createImageSlider');
  const cnt = document.getElementById('imageCount');
  const label = document.getElementById('photoLabel');
  if (!box) return;

  if (!pendingImages.length) {
    box.classList.add('hidden');
    slider.innerHTML = '';
  } else {
    box.classList.remove('hidden');
    cnt.innerText = pendingImages.length;
    slider.innerHTML = pendingImages.map((u,i) => `
      <div class="relative w-20 h-20 sm:w-24 sm:h-24 bg-gray-100 dark:bg-gray-800 rounded-xl flex-shrink-0 snap-start overflow-hidden border border-gray-200 dark:border-gray-700">
        <img src="${escAttr(u)}" class="w-full h-full object-cover" alt="">
        <button type="button" onclick="removePendingImage(${i})"
                class="absolute top-1 right-1 bg-red-500/90 hover:bg-red-500 text-white rounded-full p-1 transition">
          <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
      </div>`).join('');
  }

  // Блокируем добавление, если уже 5
  if (label) {
    if (pendingImages.length >= MAX_IMAGES) {
      label.classList.add('opacity-50','cursor-not-allowed');
      label.querySelector('input').disabled = true;
      label.title = 'Максимум 5 фото';
    } else {
      label.classList.remove('opacity-50','cursor-not-allowed');
      label.querySelector('input').disabled = false;
      label.title = '';
    }
  }
}

function initCreateEvents() {
  const dataInput = document.getElementById('dataInput');
  const charCount = document.getElementById('charCount');

  // ---------- Шрифты ----------
  document.querySelectorAll('.create-font-option').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.create-font-option')
        .forEach(b => b.classList.remove('bg-white','dark:bg-gray-700','shadow-sm'));
      btn.classList.add('bg-white','dark:bg-gray-700','shadow-sm');
      activeCreateFont = btn.dataset.font;
      dataInput.className = `w-full h-40 sm:h-52 resize-none outline-none text-sm sm:text-base bg-transparent placeholder-gray-400 leading-relaxed ${activeCreateFont}`;
      forceRepaint(dataInput);
    });
  });

  // ---------- Фикс бага textarea (последние 2 символа) ----------
  let rafId = null;
  dataInput.addEventListener('input', () => {
    if (charCount) charCount.innerText = dataInput.value.length;
    cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(() => forceRepaint(dataInput));
  });

  // также — при вставке
  dataInput.addEventListener('paste', () => setTimeout(() => forceRepaint(dataInput), 10));

  // ---------- Загрузка фото ----------
  const uploadFiles = async files => {
    files = files.filter(f => f && f.type && f.type.startsWith('image/'));
    if (!files.length) return;

    const space = MAX_IMAGES - pendingImages.length;
    if (space <= 0) { showToast(`Максимум ${MAX_IMAGES} фото`); return; }
    if (files.length > space) {
      files = files.slice(0, space);
      showToast(`Можно добавить ещё ${space}`);
    }

    const saveBtn = document.getElementById('saveBtn');
    if (saveBtn) saveBtn.disabled = true;
    showToast('Загрузка…');

    for (const f of files) {
      try {
        const fd = new FormData();
        fd.append('file', f);
        fd.append('kind', 'post');
        const r = await fetch('/api/upload', { method:'POST', body: fd, credentials:'include' });
        if (!r.ok) throw new Error((await r.json()).detail || 'Ошибка');
        const j = await r.json();
        pendingImages.push(j.url);
      } catch (e) {
        showToast('Ошибка: ' + e.message);
      }
    }

    if (saveBtn) saveBtn.disabled = false;
    updateCreateImagePreview();
    showToast('Готово');
  };

  document.getElementById('imageUploadInput').addEventListener('change', e => {
    uploadFiles(Array.from(e.target.files || []));
    e.target.value = '';
  });

  dataInput.addEventListener('paste', e => {
    const items = (e.clipboardData || window.clipboardData).items;
    const imgs = [];
    for (const k in items) {
      const it = items[k];
      if (it.kind === 'file' && it.type.startsWith('image/')) imgs.push(it.getAsFile());
    }
    if (imgs.length) { e.preventDefault(); uploadFiles(imgs); }
  });

  // ---------- Сохранить ----------
  document.getElementById('saveBtn').addEventListener('click', async () => {
    const text = dataInput.value;
    const title = document.getElementById('postTitleInput').value.trim();
    const tagsRaw = document.getElementById('tagsInput').value;

    if (text.length > MAX_TEXT) { showToast(`Текст > ${MAX_TEXT} символов`); return; }
    if (!text.trim() && !pendingImages.length && !title) { showToast('Пустой пост'); return; }

    const body = {
      title,
      text,
      tags: tagsRaw ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean) : [],
      font: activeCreateFont,
      image_urls: pendingImages,
    };

    const btn = document.getElementById('saveBtn');
    btn.disabled = true;
    try {
      if (editingPostId) {
        const updated = await api(`/api/posts/${editingPostId}`, {
          method:'PUT', body: JSON.stringify(body)
        });
        const idx = database.findIndex(p => p.id === editingPostId);
        updated.timestamp = fmtDate(updated.timestamp);
        if (idx >= 0) database[idx] = updated;
        showToast('Обновлено');
      } else {
        const created = await api('/api/posts', { method:'POST', body: JSON.stringify(body) });
        created.timestamp = fmtDate(created.timestamp);
        database.unshift(created);
        showToast('Опубликовано');
      }
      pendingImages = [];
      editingPostId = null;
      goHome();
    } catch (e) {
      showToast('Ошибка: ' + e.message);
      btn.disabled = false;
    }
  });
}

// Принудительный перерасчёт (лечит баг невидимых символов в textarea)
function forceRepaint(el) {
  if (!el) return;
  // чтение offsetHeight форсирует layout
  void el.offsetHeight;
  // держим GPU-слой
  el.style.transform = 'translateZ(0)';
}

// =============================================================
//  Profile page
// =============================================================
function renderProfilePage(username, isOwn) {
  const prof = profilesData[username] || { name: username, avatar: '^_^', bio: '', status: '', avatar_url: '' };
  const isEditing = isOwn && location.hash === '#profile/edit';
  document.title = `${prof.name} (@${username}) — СЛД`;

  const userPosts = database.filter(p => p.author === username);
  const avatarEl = prof.avatar_url
    ? `<img src="${escAttr(prof.avatar_url)}" class="w-16 h-16 sm:w-20 sm:h-20 rounded-2xl object-cover border border-gray-100 dark:border-gray-800" alt="">`
    : `<div class="w-16 h-16 sm:w-20 sm:h-20 rounded-2xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono-custom text-base sm:text-lg font-bold">${esc(prof.avatar)}</div>`;

  document.getElementById('appContent').innerHTML = `
    <div class="mb-4 hidden sm:block">
      <a href="#/" onclick="event.preventDefault();goHome()" class="text-xs font-medium text-gray-500 hover:underline">← Назад к ленте</a>
    </div>
    <div class="bg-white dark:bg-gray-900 rounded-2xl sm:rounded-3xl p-4 sm:p-8 shadow-sm border border-gray-100 dark:border-gray-800 mb-6 sm:mb-8 animate-fade-in">
      <div class="flex flex-col sm:flex-row items-start sm:items-center gap-4 sm:gap-6 justify-between">
        <div class="flex items-center gap-4 w-full sm:w-auto min-w-0">
          <div class="shrink-0">${avatarEl}</div>
          <div class="flex-1 min-w-0">
            <h2 class="text-lg sm:text-2xl font-bold truncate">${esc(prof.name)}</h2>
            <p class="text-xs text-gray-500 mb-1.5">@${esc(username)}</p>
            ${prof.status ? `<div class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400 text-[11px] font-medium mb-2 max-w-full truncate">
              <svg class="w-3 h-3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
              <span class="truncate">${esc(prof.status)}</span>
            </div>` : ''}
            <p class="text-xs sm:text-sm text-gray-600 dark:text-gray-300 break-words">${esc(prof.bio || 'Нет описания профиля.')}</p>
          </div>
        </div>
        <div class="flex items-center gap-2 w-full sm:w-auto justify-between sm:justify-end border-t sm:border-t-0 pt-4 sm:pt-0 border-gray-100 dark:border-gray-800 mt-2 sm:mt-0 shrink-0">
          ${isOwn ? `
            <a href="${isEditing ? '#profile' : '#profile/edit'}"
               class="flex-1 sm:flex-none text-center bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 px-5 py-2.5 rounded-xl text-xs sm:text-sm font-medium transition">
              ${isEditing ? 'Закрыть' : 'Настройки'}
            </a>
            <button onclick="logoutUser()" title="Выйти"
                    class="text-red-500 bg-red-50 dark:bg-red-950/30 hover:bg-red-100 dark:hover:bg-red-950/50 p-2.5 rounded-xl transition shrink-0">
              <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
                <polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>
              </svg>
            </button>` : ''}
        </div>
      </div>

      ${isEditing ? `
      <form id="editProfileForm" class="mt-6 pt-6 border-t border-gray-100 dark:border-gray-800 space-y-4 animate-fade-in">
        <h3 class="text-sm font-semibold">Настройки профиля</h3>

        <div class="flex items-center gap-4">
          <div id="avatarPreviewWrap" data-url="${escAttr(prof.avatar_url||'')}">
            ${prof.avatar_url
              ? `<img id="avatarPreview" src="${escAttr(prof.avatar_url)}" class="w-20 h-20 rounded-2xl object-cover border border-gray-200 dark:border-gray-700" alt="">`
              : `<div class="w-20 h-20 rounded-2xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono text-lg font-bold border border-gray-200 dark:border-gray-700">${esc(prof.avatar)}</div>`}
          </div>
          <div class="flex-1">
            <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1.5">Аватар — 512×512, ≤ 150 KB, AVIF/WebP</label>
            <div class="flex items-center gap-2 flex-wrap">
              <label class="inline-flex items-center gap-2 cursor-pointer bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 px-3.5 py-2.5 rounded-xl text-xs font-medium transition">
                <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
                </svg>
                Загрузить
                <input type="file" id="avatarInput" accept="image/*" class="hidden">
              </label>
              ${prof.avatar_url ? `<button type="button" onclick="removeAvatar()" class="text-xs text-red-500 hover:underline">Удалить</button>` : ''}
            </div>
          </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Имя</label>
            <input type="text" id="editName" maxlength="64" value="${escAttr(prof.name)}"
                   class="w-full text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 outline-none focus:border-gray-400">
          </div>
          <div>
            <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Статус (до 60)</label>
            <input type="text" id="editStatus" maxlength="60" value="${escAttr(prof.status)}"
                   class="w-full text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 outline-none focus:border-gray-400">
          </div>
          <div>
            <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Символ (до 5)</label>
            <input type="text" id="editAvatar" maxlength="5" value="${escAttr(prof.avatar)}"
                   class="w-full text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 outline-none font-mono focus:border-gray-400">
          </div>
        </div>
        <div>
          <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">О себе</label>
          <textarea id="editBio" maxlength="1000"
                    class="w-full text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-3 outline-none resize-none h-20 focus:border-gray-400">${esc(prof.bio)}</textarea>
        </div>
        <button type="submit"
                class="bg-gray-900 dark:bg-white text-white dark:text-gray-900 px-5 py-2.5 rounded-xl text-xs font-medium w-full sm:w-auto">
          Сохранить изменения
        </button>
      </form>` : ''}
    </div>

    <h3 class="text-base sm:text-lg font-medium mb-4">Публикации (${userPosts.length})</h3>
    <div class="space-y-4">
      ${userPosts.length === 0 ? `<div class="text-center py-10 text-gray-400 text-xs">Пока пусто</div>` : ''}
      ${userPosts.map(entry => `
        <article id="post-${entry.id}" onclick="location.hash='${escAttr(entry.id)}'"
                 class="bg-white dark:bg-gray-900 rounded-2xl sm:rounded-3xl p-4 sm:p-5 shadow-sm border border-gray-100 dark:border-gray-800 hover:shadow-md cursor-pointer animate-fade-in">
          <div class="flex justify-between items-center mb-3 gap-2">
            <span class="text-[11px] text-gray-400">${esc(entry.timestamp)}</span>
            <div class="flex items-center gap-1.5 shrink-0">
              ${isOwn ? `
              <button onclick="event.stopPropagation();location.hash='edit/${escAttr(entry.id)}'" title="Редактировать"
                      class="text-gray-400 hover:text-blue-500 bg-gray-50 dark:bg-gray-800 p-2 rounded-xl transition">
                <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                  <path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                </svg>
              </button>
              <button onclick="event.stopPropagation();deletePost('${escAttr(entry.id)}')" title="Удалить"
                      class="text-red-400 hover:text-red-600 bg-red-50 dark:bg-red-950/30 p-2 rounded-xl transition">
                <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M3 6h18M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>
                </svg>
              </button>` : ''}
            </div>
          </div>
          ${entry.title ? `<h3 class="text-base font-bold mb-2 break-words">${esc(entry.title)}</h3>` : ''}
          ${generateCarouselHTML(entry.id, entry.images)}
          ${entry.text ? `<div class="text-sm sm:text-base ${esc(entry.font)} leading-relaxed mb-2 line-clamp-3 break-words">${formatPostText(entry.text)}</div>` : ''}
        </article>`).join('')}
    </div>`;

  if (isEditing) initProfileEditEvents();
}

function initProfileEditEvents() {
  const avatarInput = document.getElementById('avatarInput');
  if (avatarInput) {
    avatarInput.addEventListener('change', async e => {
      const f = e.target.files?.[0];
      if (!f) return;
      showToast('Загрузка аватара…');
      try {
        const fd = new FormData();
        fd.append('file', f);
        fd.append('kind', 'avatar');
        const r = await fetch('/api/upload', { method:'POST', body: fd, credentials:'include' });
        if (!r.ok) throw new Error((await r.json()).detail || 'Ошибка');
        const j = await r.json();
        const wrap = document.getElementById('avatarPreviewWrap');
        wrap.innerHTML = `<img id="avatarPreview" src="${j.url}" class="w-20 h-20 rounded-2xl object-cover border border-gray-200 dark:border-gray-700" alt="">`;
        wrap.dataset.url = j.url;
        showToast('Аватар загружен');
      } catch (err) { showToast('Ошибка: ' + err.message); }
    });
  }

  document.getElementById('editProfileForm').addEventListener('submit', async e => {
    e.preventDefault();
    const wrap = document.getElementById('avatarPreviewWrap');
    const body = {
      name: document.getElementById('editName').value.trim(),
      avatar: document.getElementById('editAvatar').value.trim() || '^_^',
      avatar_url: wrap.dataset.url || '',
      status: document.getElementById('editStatus').value.trim(),
      bio: document.getElementById('editBio').value.trim(),
    };
    if (body.avatar.length > 5) { showToast('Символьный аватар — до 5 знаков'); return; }
    try {
      await api('/api/profiles/me', { method:'PUT', body: JSON.stringify(body) });
      profilesData[currentUser] = { ...(profilesData[currentUser]||{}), ...body, name: body.name || currentUser };
      showToast('Сохранено');
      location.hash = '#profile';
      router();
    } catch (err) { showToast('Ошибка: ' + err.message); }
  });
}

window.removeAvatar = function() {
  const wrap = document.getElementById('avatarPreviewWrap');
  if (!wrap) return;
  wrap.dataset.url = '';
  const emoji = document.getElementById('editAvatar')?.value || '^_^';
  wrap.innerHTML = `<div class="w-20 h-20 rounded-2xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono text-lg font-bold border border-gray-200 dark:border-gray-700">${esc(emoji)}</div>`;
};

// =============================================================
//  Delete / copy
// =============================================================
window.deletePost = async function(postId, fromSingle) {
  if (!confirm('Удалить запись?')) return;
  try {
    await api(`/api/posts/${postId}`, { method:'DELETE' });
    database = database.filter(p => p.id !== postId);
    showToast('Удалено');
    if (fromSingle) goHome();
    else router();
  } catch (e) { showToast('Ошибка: ' + e.message); }
};

window.copyPostLink = function(id) {
  const url = `${location.origin}${location.pathname}#${id}`;
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(url).then(
      () => showToast('Ссылка скопирована'),
      () => legacyCopy(url)
    );
  } else legacyCopy(url);
};

function legacyCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); showToast('Ссылка скопирована'); }
  catch { showToast('Не удалось скопировать'); }
  ta.remove();
}

// =============================================================
//  Stats modal
// =============================================================
window.openStats = async function() {
  const m = document.getElementById('statsModal');
  m.classList.remove('hidden');
  document.getElementById('statsLoader').classList.remove('hidden');
  document.getElementById('statsBody').classList.add('hidden');
  try {
    const s = await api('/api/stats');
    const DB_LIMIT = 500 * 1024 * 1024;   // 500 MB
    const FS_LIMIT = 1024 * 1024 * 1024;  // 1 GB

    const dbPct = Math.min(100, (s.db_bytes / DB_LIMIT) * 100);
    const fsPct = Math.min(100, (s.storage_bytes / FS_LIMIT) * 100);

    document.getElementById('dbPercent').innerText = dbPct.toFixed(2) + '%';
    document.getElementById('dbProgressBar').style.width = dbPct + '%';
    document.getElementById('dbUsedMB').innerText = fmtBytes(s.db_bytes);

    document.getElementById('filePercent').innerText = fsPct.toFixed(2) + '%';
    document.getElementById('fileProgressBar').style.width = fsPct + '%';
    document.getElementById('fileUsedMB').innerText = fmtBytes(s.storage_bytes);
    document.getElementById('fileCount').innerText = s.storage_files;

    document.getElementById('postsBytes').innerText = fmtBytes(s.posts_bytes);
    document.getElementById('profilesBytes').innerText = fmtBytes(s.profiles_bytes);

    document.getElementById('statsLoader').classList.add('hidden');
    document.getElementById('statsBody').classList.remove('hidden');
  } catch (e) {
    showToast('Не удалось: ' + e.message);
    document.getElementById('statsLoader').innerHTML = `<div class="text-red-500 text-xs py-3">Ошибка: ${esc(e.message)}</div>`;
  }
};
window.closeStats = function() {
  document.getElementById('statsModal').classList.add('hidden');
};

function fmtBytes(b) {
  if (b < 1024) return b + ' Б';
  if (b < 1024*1024) return (b/1024).toFixed(1) + ' КБ';
  if (b < 1024*1024*1024) return (b/1024/1024).toFixed(2) + ' МБ';
  return (b/1024/1024/1024).toFixed(2) + ' ГБ';
}

// =============================================================
//  Auth UI
// =============================================================
window.openAuth = function(hash) {
  pendingHash = hash || null;
  const m = document.getElementById('authModal');
  m.classList.remove('hidden');
  document.getElementById('authError').classList.add('hidden');
  setTimeout(() => document.getElementById('authUsername')?.focus(), 60);
};

document.getElementById('closeAuthModal').addEventListener('click', () => {
  document.getElementById('authModal').classList.add('hidden');
  pendingHash = null;
});

window.logoutUser = async function() {
  try { await api('/api/auth/logout', { method:'POST' }); } catch {}
  currentUser = null; currentUserId = null;
  updateUserInterface();
  goHome();
  showToast('Вы вышли');
};

function updateUserInterface() {
  const area = document.getElementById('desktopUserProfileArea');
  if (!area) return;
  if (currentUser) {
    const prof = profilesData[currentUser] || { avatar: '^_^', avatar_url: '' };
    const av = prof.avatar_url
      ? `<img src="${escAttr(prof.avatar_url)}" class="w-7 h-7 rounded-xl object-cover" alt="">`
      : `<div class="w-7 h-7 rounded-xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono text-[10px] font-bold">${esc(prof.avatar)}</div>`;
    area.innerHTML = `
      <div class="flex items-center gap-2">
        <a href="#profile" class="text-xs sm:text-sm hover:underline flex items-center gap-2 font-medium">
          ${av}<span class="max-w-[120px] truncate">${esc(currentUser)}</span>
        </a>
        <button onclick="logoutUser()" title="Выйти"
                class="text-gray-500 hover:text-red-500 p-2 rounded-xl bg-gray-100 dark:bg-gray-800 transition">
          <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
            <polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>
          </svg>
        </button>
      </div>`;
  } else {
    area.innerHTML = `
      <button onclick="openAuth()"
              class="bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200 border border-gray-200 dark:border-gray-700 px-4 py-2 rounded-xl text-xs sm:text-sm font-medium shadow-sm transition">
        Вход / Регистрация
      </button>`;
  }
}

// =============================================================
//  Auth form
// =============================================================
document.getElementById('authForm').addEventListener('submit', async e => {
  e.preventDefault();
  const u = document.getElementById('authUsername').value.trim();
  const p = document.getElementById('authPassword').value;
  const errBox = document.getElementById('authError');
  const btn = document.getElementById('authSubmit');
  errBox.classList.add('hidden');

  if (!u || p.length < 6) {
    errBox.innerText = 'Имя и пароль (≥6 символов) обязательны';
    errBox.classList.remove('hidden');
    return;
  }

  btn.disabled = true;
  btn.innerText = 'Подождите…';

  try {
    let loggedIn = false;
    let lastErr = null;

    // 1) Login
    try {
      await api('/api/auth/login', { method:'POST', body: JSON.stringify({ username: u, password: p }) });
      loggedIn = true;
    } catch (loginErr) {
      lastErr = loginErr;
      // 2) Регистрация
      try {
        await api('/api/auth/signup', { method:'POST', body: JSON.stringify({ username: u, password: p }) });
        loggedIn = true;
      } catch (signupErr) {
        lastErr = signupErr;
      }
    }

    if (!loggedIn) throw lastErr || new Error('Не удалось войти');

    // Проверяем, что cookie реально применился
    const me = await api('/api/auth/me');
    if (!me.authenticated) throw new Error('Сессия не создалась, попробуйте ещё раз');

    currentUser = me.username;
    currentUserId = me.id;

    document.getElementById('authModal').classList.add('hidden');
    document.getElementById('authForm').reset();
    showToast(`Привет, ${currentUser}!`);

    await fetchAllData();

    if (pendingHash) {
      const target = pendingHash;
      pendingHash = null;
      location.hash = target;
      router();
    }
  } catch (err) {
    errBox.innerText = err.message || 'Ошибка';
    errBox.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.innerText = 'Войти / Создать';
  }
});

// =============================================================
//  Предзагрузка шрифтов + boot
// =============================================================
const ALL_FONTS = ['Inter','Lora','Fira Code','Playfair Display','JetBrains Mono','Caveat','Montserrat','Merriweather'];
async function preloadFonts() {
  try {
    await Promise.all(ALL_FONTS.map(f =>
      document.fonts.load('16px "' + f + '"').catch(() => null)
    ));
    await document.fonts.ready;
  } catch {}
}

(async function boot() {
  if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    document.documentElement.classList.add('dark');
  }

  // Стартуем параллельно: проверка auth + предзагрузка шрифтов
  const [meResult] = await Promise.allSettled([
    api('/api/auth/me'),
    preloadFonts(),
  ]);

  if (meResult.status === 'fulfilled' && meResult.value?.authenticated) {
    currentUser = meResult.value.username;
    currentUserId = meResult.value.id;
  }

  // Плавно скрываем overlay
  const ov = document.getElementById('loadingOverlay');
  ov.style.transition = 'opacity .35s ease';
  ov.style.opacity = '0';
  setTimeout(() => ov.remove(), 380);

  await fetchAllData();
})();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/favicon.ico")
async def favicon():
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        b'<rect width="64" height="64" rx="14" fill="#111827"/>'
        b'<text x="32" y="43" font-family="sans-serif" font-size="26" font-weight="700" fill="#fff" text-anchor="middle">'
        b'\xd0\xa1\xd0\x9b\xd0\x94'
        b'</text></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
