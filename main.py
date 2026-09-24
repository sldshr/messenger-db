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
from fastapi import FastAPI, Request, Response, HTTPException, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
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
# Supabase auth (через REST, чтобы использовать service role)
# --------------------------------------------------------------------------- #
AUTH_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


async def sb_signup(email: str, password: str) -> dict:
    """Создать пользователя (email подтверждён автоматически)."""
    async with httpx.AsyncClient(timeout=15) as cx:
        r = await cx.post(f"{SUPABASE_URL}/auth/v1/admin/users",
                          headers=AUTH_HEADERS,
                          json={"email": email, "password": password,
                                "email_confirm": True})
        if r.status_code >= 400:
            try:
                msg = r.json().get("msg") or r.json().get("message") or r.text
            except Exception:
                msg = r.text
            raise HTTPException(400, f"Ошибка регистрации: {msg}")
        return r.json()


async def sb_login(email: str, password: str) -> dict:
    """Проверить пароль через токен-эндпоинт."""
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
def compress_image(data: bytes, max_dim: int = 512, max_size: int = 150 * 1024):
    """
    Сжимает картинку до max_dim по длинной стороне и <= max_size байт.
    Пытается AVIF → WebP → JPEG. Возвращает (bytes, mime, ext).
    """
    img = Image.open(io.BytesIO(data))
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    candidates = [("AVIF", "image/avif", "avif"),
                  ("WEBP", "image/webp", "webp"),
                  ("JPEG", "image/jpeg", "jpg")]

    for fmt, mime, ext in candidates:
        try:
            for q in (80, 70, 60, 50, 40, 30):
                buf = io.BytesIO()
                img.save(buf, format=fmt, quality=q, method=4 if fmt == "AVIF" else 6)
                if buf.tell() <= max_size:
                    return buf.getvalue(), mime, ext
        except Exception as e:
            log.warning("format %s failed: %s", fmt, e)
            continue

    # fallback: любой JPEG, даже если чуть больше
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=35, optimize=True)
    return buf.getvalue(), "image/jpeg", "jpg"


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class AuthIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class PostIn(BaseModel):
    title: str = ""
    text: str = ""
    tags: List[str] = []
    font: str = "font-serif-custom"
    image_urls: List[str] = []


class ProfileIn(BaseModel):
    name: str = ""
    avatar: str = "^_^"
    avatar_url: str = ""
    bio: str = ""
    status: str = ""


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

    # Проверка занятости username
    existing = supabase.table("profiles").select("id").eq("username", uname).execute()
    if existing.data:
        raise HTTPException(400, "Имя занято")

    try:
        user = await sb_signup(_email(uname), body.password)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Ошибка: {e}")

    uid = user.get("id") or user.get("user", {}).get("id")
    if not uid:
        raise HTTPException(500, "Не удалось создать пользователя")

    # Создаём профиль
    supabase.table("profiles").insert({
        "id": uid, "username": uname, "name": uname, "avatar": "^_^",
        "bio": "", "status": "", "avatar_url": ""
    }).execute()

    set_auth_cookie(response, make_token(uid, uname))
    return {"ok": True, "id": uid, "username": uname}


@app.post("/api/auth/login")
async def login(body: AuthIn, response: Response):
    uname = body.username.strip()
    try:
        result = await sb_login(_email(uname), body.password)
    except HTTPException:
        raise

    uid = result.get("user", {}).get("id")
    if not uid:
        raise HTTPException(500, "Не удалось получить профиль")

    # Если профиля нет — создаём (на случай ручного создания в Supabase)
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
    row = {
        "author_id": user["id"],
        "author_username": user["username"],
        "title": body.title.strip(),
        "text": body.text,
        "tags": [t.strip().lower() for t in body.tags if t.strip()],
        "font": body.font,
        "image_urls": body.image_urls,
    }
    res = supabase.table("posts").insert(row).execute()
    return _row_to_post(res.data[0])


@app.put("/api/posts/{post_id}")
async def update_post(post_id: str, body: PostIn, user: dict = Depends(require_user)):
    check = supabase.table("posts").select("author_id").eq("id", post_id).limit(1).execute()
    if not check.data:
        raise HTTPException(404, "Пост не найден")
    if check.data[0]["author_id"] != user["id"]:
        raise HTTPException(403, "Нет прав")

    row = {
        "title": body.title.strip(),
        "text": body.text,
        "tags": [t.strip().lower() for t in body.tags if t.strip()],
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
        raise HTTPException(400, "Аватар до 5 символов")
    if len(body.status) > 60:
        raise HTTPException(400, "Статус до 60 символов")

    upd = {
        "name": body.name.strip() or user["username"],
        "avatar": body.avatar or "^_^",
        "avatar_url": body.avatar_url,
        "bio": body.bio,
        "status": body.status,
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
async def upload(file: UploadFile = File(...), user: dict = Depends(require_user)):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Пустой файл")

    data, mime, ext = compress_image(raw)
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
# Frontend
# --------------------------------------------------------------------------- #
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru" class="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>СЛД</title>
<meta name="description" content="СЛД — тексты, заметки и мысли.">
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config = { darkMode: 'class' };</script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;700&family=Inter:wght@400;500;600;700&family=Lora:ital,wght@0,400;0,500;1,400&family=Playfair+Display:ital,wght@0,400;0,600;1,400&family=JetBrains+Mono:wght@400;500;700&family=Caveat:wght@500;700&family=Montserrat:wght@400;500;600&family=Merriweather:ital,wght@0,300;0,400;1,300&display=swap');
body { font-family:'Inter',sans-serif; }
.font-sans-custom{font-family:'Inter',sans-serif}
.font-serif-custom{font-family:'Lora',serif}
.font-mono-custom{font-family:'Fira Code',monospace}
.font-playfair{font-family:'Playfair Display',serif}
.font-jetbrains{font-family:'JetBrains Mono',monospace}
.font-caveat{font-family:'Caveat',cursive;font-size:1.35rem}
.font-montserrat{font-family:'Montserrat',sans-serif}
.font-merriweather{font-family:'Merriweather',serif}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:9999px}
.dark ::-webkit-scrollbar-thumb{background:#334155}
.no-scrollbar::-webkit-scrollbar{display:none}
.no-scrollbar{-ms-overflow-style:none;scrollbar-width:none}
.animate-fade-in{animation:fadeIn .25s cubic-bezier(.16,1,.3,1) forwards}
.animate-pop-in{animation:popIn .2s cubic-bezier(.16,1,.3,1) forwards}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
@keyframes popIn{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:scale(1)}}
.spinner{width:44px;height:44px;border:3px solid rgba(148,163,184,.25);border-top-color:#3b82f6;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body class="bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100 antialiased transition-colors duration-200 flex flex-col min-h-screen pb-20 sm:pb-0">

<!-- Loading overlay -->
<div id="loadingOverlay" class="fixed inset-0 bg-gray-50 dark:bg-gray-950 z-[300] flex flex-col items-center justify-center gap-4">
  <div class="spinner"></div>
  <div class="text-xs text-gray-400 font-medium tracking-wider">Загрузка СЛД…</div>
</div>

<div id="toast" class="fixed top-5 left-1/2 -translate-x-1/2 sm:top-auto sm:bottom-6 sm:left-auto sm:right-6 sm:translate-x-0 bg-gray-900/90 dark:bg-gray-100/90 backdrop-blur-md text-white dark:text-gray-900 px-5 py-3 rounded-2xl text-xs sm:text-sm font-medium opacity-0 pointer-events-none transition-all duration-300 z-[100] shadow-2xl">…</div>

<!-- Image preview -->
<div id="imagePreviewModal" class="fixed inset-0 bg-black/95 backdrop-blur-xl z-[100] flex-col hidden">
  <div class="flex justify-between items-center p-3 sm:p-5 text-white bg-black/40">
    <div class="flex gap-2">
      <button onclick="zoomPreview(1.3)" class="p-2 bg-white/10 hover:bg-white/20 rounded-xl">＋</button>
      <button onclick="zoomPreview(0.77)" class="p-2 bg-white/10 hover:bg-white/20 rounded-xl">−</button>
      <button onclick="rotatePreview(-90)" class="p-2 bg-white/10 hover:bg-white/20 rounded-xl">↺</button>
      <button onclick="rotatePreview(90)" class="p-2 bg-white/10 hover:bg-white/20 rounded-xl">↻</button>
    </div>
    <button onclick="closeImagePreview()" class="px-3 py-2 bg-red-500/80 hover:bg-red-500 rounded-xl text-sm font-medium">Закрыть</button>
  </div>
  <div class="flex-1 overflow-auto flex items-center justify-center p-4">
    <img id="previewImage" class="max-h-full max-w-full object-contain select-none" draggable="false">
  </div>
</div>

<!-- Auth modal -->
<div id="authModal" class="fixed inset-0 bg-black/50 backdrop-blur-sm z-[90] flex items-center justify-center p-4 hidden">
  <div class="bg-white dark:bg-gray-900 rounded-3xl p-6 sm:p-8 max-w-sm w-full shadow-2xl border border-gray-100 dark:border-gray-800 relative animate-pop-in">
    <button id="closeAuthModal" class="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 text-xl p-2">✕</button>
    <h3 class="text-2xl font-semibold mb-1">Вход / Регистрация</h3>
    <p class="text-xs text-gray-500 mb-6">Введите имя пользователя и пароль.</p>
    <form id="authForm">
      <div class="space-y-3">
        <div>
          <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Имя пользователя</label>
          <input type="text" id="authUsername" required minlength="2" autocomplete="username"
                 class="w-full text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2.5 outline-none focus:border-gray-400" placeholder="username">
        </div>
        <div>
          <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Пароль (мин. 6)</label>
          <input type="password" id="authPassword" required minlength="6" autocomplete="current-password"
                 class="w-full text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2.5 outline-none focus:border-gray-400" placeholder="••••••••">
        </div>
      </div>
      <div id="authError" class="hidden mt-3 text-[11px] text-red-500 bg-red-50 dark:bg-red-950/30 px-3 py-2 rounded-xl"></div>
      <button type="submit" id="authSubmit" class="mt-5 w-full bg-gray-900 dark:bg-white text-white dark:text-gray-900 py-3 rounded-xl text-sm font-medium active:scale-[0.98] transition-transform disabled:opacity-50">Войти / Создать</button>
    </form>
  </div>
</div>

<!-- Error modal -->
<div id="errorModal" class="fixed inset-0 bg-black/60 backdrop-blur-md z-[90] flex items-center justify-center p-4 hidden">
  <div class="bg-white dark:bg-gray-900 rounded-3xl p-6 max-w-sm w-full text-center shadow-2xl border border-gray-100 dark:border-gray-800 animate-pop-in">
    <div class="w-12 h-12 rounded-2xl bg-red-50 dark:bg-red-950/40 text-red-500 flex items-center justify-center mx-auto mb-3 font-bold text-xl">!</div>
    <h3 class="text-lg font-semibold mb-1">Запись не найдена</h3>
    <p class="text-xs text-gray-500 mb-5">Публикация удалена или не существовала.</p>
    <button id="closeErrorBtn" class="w-full bg-gray-900 dark:bg-white text-white dark:text-gray-900 py-2.5 rounded-xl text-xs font-medium">На главную</button>
  </div>
</div>

<div class="max-w-3xl w-full mx-auto px-4 py-4 sm:py-10 flex-1 flex flex-col">
  <header class="hidden sm:flex justify-between items-center pb-6 mb-8 border-b border-gray-200 dark:border-gray-800">
    <div>
      <a href="#/" onclick="event.preventDefault(); goHome()" class="text-2xl sm:text-3xl font-semibold tracking-tight hover:opacity-80">СЛД</a>
      <p class="text-gray-500 mt-0.5 text-xs sm:text-sm">ещё один текст</p>
    </div>
    <div id="desktopUserProfileArea" class="flex items-center gap-2"></div>
  </header>

  <main id="appContent" class="flex-1"></main>
</div>

<nav id="mobileBottomNav" class="sm:hidden fixed bottom-0 left-0 right-0 bg-white/85 dark:bg-gray-900/85 backdrop-blur-lg border-t border-gray-200/80 dark:border-gray-800/80 z-40 px-6 py-2.5 flex justify-around items-center"></nav>

<footer class="hidden sm:block mt-12 border-t border-gray-200 dark:border-gray-800 py-8 text-center text-xs text-gray-500">
  <div class="max-w-3xl mx-auto px-4 flex flex-col sm:flex-row justify-between items-center gap-4">
    <div><span class="font-semibold text-gray-800 dark:text-gray-200">СЛД</span> &copy; 2026</div>
    <div class="flex items-center gap-4 flex-wrap justify-center">
      <a href="#/" onclick="event.preventDefault(); goHome()" class="hover:underline">Главная</a>
      <span class="text-gray-300 dark:text-gray-700">|</span>
      <span>powered by <span class="font-semibold">Supabase</span> &amp; <span class="font-semibold">FastAPICloud</span></span>
    </div>
  </div>
</footer>

<script>
// =============================================================
// State
// =============================================================
let currentUser = null;
let currentUserId = null;
let database = [];
let profilesData = {};
let currentSortOrder = 'new';
let pendingImages = [];
let isDataLoaded = false;
let editingPostId = null;
let previewScale = 1, previewRotation = 0;

// =============================================================
// API helper
// =============================================================
async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let detail = 'Ошибка';
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// =============================================================
// Utilities
// =============================================================
function sanitizeHTML(str) {
  if (!str) return '';
  return String(str).replace(/[&<>'"]/g, t => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[t]||t));
}
function showToast(text) {
  const t = document.getElementById('toast');
  t.innerText = text;
  t.classList.remove('opacity-0','pointer-events-none');
  setTimeout(() => t.classList.add('opacity-0','pointer-events-none'), 2500);
}
function fmtDate(s) {
  if (!s) return '';
  try { return new Date(s).toLocaleString('ru-RU',{day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'}); }
  catch { return s; }
}
function formatPostText(text) {
  if (!text) return '';
  let p = sanitizeHTML(text);
  p = p.replace(/!([a-zA-Z0-9_-]+)/g, '<a href="#/@$1" onclick="event.stopPropagation(); navigateToProfile(\'$1\'); return false;" class="text-blue-500 font-medium hover:underline">!$1</a>');
  return p.replace(/\n/g, '<br>');
}
function goHome() { location.hash = ''; router(); }

// =============================================================
// Image preview
// =============================================================
window.openImagePreview = function(url) {
  const m = document.getElementById('imagePreviewModal');
  document.getElementById('previewImage').src = url;
  previewScale = 1; previewRotation = 0; updatePreviewTransform();
  m.classList.remove('hidden'); m.classList.add('flex');
};
window.closeImagePreview = function() {
  const m = document.getElementById('imagePreviewModal');
  m.classList.add('hidden'); m.classList.remove('flex');
  document.getElementById('previewImage').src = '';
};
window.zoomPreview = f => { previewScale = Math.min(8, Math.max(0.3, previewScale*f)); updatePreviewTransform(); };
window.rotatePreview = d => { previewRotation += d; updatePreviewTransform(); };
function updatePreviewTransform() {
  document.getElementById('previewImage').style.transform = `scale(${previewScale}) rotate(${previewRotation}deg)`;
}
window.downloadImage = function(url, index) {
  const a = document.createElement('a');
  a.href = url; a.download = `SLD_${index+1}.jpg`; a.target = '_blank';
  document.body.appendChild(a); a.click(); a.remove();
  showToast('Загрузка начата');
};

// =============================================================
// Carousel
// =============================================================
function generateCarouselHTML(postId, images) {
  if (!images || images.length === 0) return '';
  const slides = images.map((url, idx) => `
    <div class="w-full min-w-full flex-shrink-0 snap-center relative flex items-center justify-center cursor-pointer overflow-hidden p-1" onclick="openImagePreview('${url}')">
      <img src="${url}" class="max-h-[50vh] sm:max-h-[60vh] max-w-full object-contain rounded-xl bg-gray-100 dark:bg-gray-900" loading="lazy">
      <button onclick="event.stopPropagation(); downloadImage('${url}', ${idx})" title="Скачать" class="absolute top-3 right-3 bg-black/50 hover:bg-black/80 text-white p-2 rounded-xl backdrop-blur">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
      </button>
      ${images.length>1?`<div class="absolute bottom-4 left-1/2 -translate-x-1/2 bg-black/60 text-white text-[10px] px-3 py-1 rounded-full">${idx+1}/${images.length}</div>`:''}
    </div>`).join('');
  return `
    <div class="relative bg-gray-100/50 dark:bg-[#0a0a0a] rounded-2xl mb-4 overflow-hidden border border-gray-100 dark:border-gray-800/50 group">
      <div id="carousel-${postId}" class="flex w-full overflow-x-auto snap-x snap-mandatory scroll-smooth no-scrollbar">${slides}</div>
      ${images.length>1?`
        <button onclick="event.stopPropagation(); const c=document.getElementById('carousel-${postId}');c.scrollBy({left:-c.clientWidth,behavior:'smooth'})" class="hidden sm:flex absolute left-3 top-1/2 -translate-y-1/2 bg-white/80 dark:bg-black/60 p-2 rounded-full opacity-0 group-hover:opacity-100 z-10">‹</button>
        <button onclick="event.stopPropagation(); const c=document.getElementById('carousel-${postId}');c.scrollBy({left:c.clientWidth,behavior:'smooth'})" class="hidden sm:flex absolute right-3 top-1/2 -translate-y-1/2 bg-white/80 dark:bg-black/60 p-2 rounded-full opacity-0 group-hover:opacity-100 z-10">›</button>`:''}
    </div>`;
}

// =============================================================
// Data loading
// =============================================================
async function fetchAllData() {
  try {
    const [posts, profiles] = await Promise.all([
      api('/api/posts'),
      api('/api/profiles'),
    ]);
    database = posts.map(p => ({ ...p, timestamp: fmtDate(p.timestamp) }));
    profilesData = profiles;
  } catch (e) {
    console.error('load error', e);
  }
  isDataLoaded = true;
  updateUserInterface();
  router();
}

// =============================================================
// Router
// =============================================================
window.addEventListener('hashchange', router);
function router() {
  const hash = location.hash;
  updateMobileNav();

  if (hash === '#create' || hash.startsWith('#edit/')) {
    if (!currentUser) { openAuth(); location.hash = ''; return; }
    renderCreateOrEditPage();
    return;
  }
  if (hash.startsWith('#/@')) {
    renderProfilePage(decodeURIComponent(hash.substring(3)).trim(), false);
    return;
  }
  if (hash === '#profile' || hash === '#profile/edit') {
    if (!currentUser) { openAuth(); location.hash = ''; return; }
    renderProfilePage(currentUser, true);
    return;
  }
  if (hash && hash.length > 1) {
    const id = decodeURIComponent(hash.substring(1)).trim();
    let post = database.find(p => p.id === id);
    if (post) { renderSinglePost(post); return; }
    api(`/api/posts/${id}`).then(p => {
      p.timestamp = fmtDate(p.timestamp);
      database.unshift(p);
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

function navigateToProfile(u) { location.hash = '/@' + u; }
window.navigateToProfile = navigateToProfile;

// =============================================================
// Mobile nav
// =============================================================
function updateMobileNav() {
  const nav = document.getElementById('mobileBottomNav');
  const hash = location.hash;
  if (currentUser) {
    nav.innerHTML = `
      <a href="#/" onclick="event.preventDefault();goHome()" class="flex flex-col items-center gap-1 ${(!hash||hash==='#/')?'text-gray-900 dark:text-white font-semibold':'text-gray-400'}">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 12l2-2m0 0l7-7 7 7M5 10v10h3m10-11l2 2m-2-2v10h-3m-6 0v-4h2v4"/></svg>
        <span class="text-[10px] font-medium">Лента</span></a>
      <a href="#create" class="flex flex-col items-center gap-1 ${hash==='#create'?'text-gray-900 dark:text-white font-semibold':'text-gray-400'}">
        <div class="w-8 h-8 rounded-xl bg-gray-900 dark:bg-white text-white dark:text-gray-900 flex items-center justify-center -mt-2 shadow-md">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>
        </div>
        <span class="text-[10px] font-medium">Создать</span></a>
      <a href="#profile" class="flex flex-col items-center gap-1 ${hash.startsWith('#profile')?'text-gray-900 dark:text-white font-semibold':'text-gray-400'}">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>
        <span class="text-[10px] font-medium">Профиль</span></a>`;
  } else {
    nav.innerHTML = `
      <a href="#/" onclick="event.preventDefault();goHome()" class="flex flex-col items-center gap-1 text-gray-400">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 12l2-2m0 0l7-7 7 7M5 10v10h3m10-11l2 2m-2-2v10h-3m-6 0v-4h2v4"/></svg>
        <span class="text-[10px] font-medium">Лента</span></a>
      <button onclick="openAuth()" class="flex flex-col items-center gap-1 text-gray-400">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1"/></svg>
        <span class="text-[10px] font-medium">Вход</span></button>`;
  }
}

// =============================================================
// Main feed
// =============================================================
function renderMainFeed() {
  document.title = 'СЛД';
  const content = document.getElementById('appContent');
  content.innerHTML = `
    <div class="sm:hidden mb-6 flex justify-between items-center pb-4 border-b border-gray-200 dark:border-gray-800">
      <div><h1 class="text-xl font-bold">СЛД</h1><p class="text-gray-500 text-[11px]">ещё один текст</p></div>
    </div>

    <section id="createSectionDesktop" class="hidden ${currentUser?'sm:block':''} bg-white dark:bg-gray-900 rounded-3xl shadow-sm border border-gray-100 dark:border-gray-800 p-4 sm:p-6 mb-6">
      <div class="text-center py-4">
        <p class="text-sm text-gray-500 mb-3">Хотите поделиться мыслью?</p>
        <a href="#create" class="inline-block bg-gray-900 dark:bg-white text-white dark:text-gray-900 px-6 py-2.5 rounded-xl text-sm font-medium">Написать пост</a>
      </div>
    </section>

    <div class="flex flex-col sm:flex-row justify-between items-stretch sm:items-center mb-6 gap-3">
      <h2 class="text-base sm:text-lg font-medium">Записи <span id="statTotal" class="text-gray-400 text-sm ml-1 font-normal">(0)</span></h2>
      <div class="flex flex-wrap items-center gap-2">
        <select id="sortSelect" class="text-[11px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 outline-none font-medium">
          <option value="new">Сначала новые</option>
          <option value="old">Сначала старые</option>
        </select>
        <input type="text" id="searchInput" class="text-[11px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 outline-none placeholder-gray-400 flex-1 sm:w-48" placeholder="Поиск…">
      </div>
    </div>

    <div id="dbContainer" class="space-y-5"></div>`;

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
  filtered.sort((a,b) => currentSortOrder==='new'
    ? (b.timestamp||'').localeCompare(a.timestamp||'') || b.id.localeCompare(a.id)
    : (a.timestamp||'').localeCompare(b.timestamp||'') || a.id.localeCompare(b.id));

  if (!filtered.length) {
    box.innerHTML = `<div class="text-center py-10 text-gray-400 text-xs">Записей не найдено</div>`;
    if (stat) stat.innerText = '(0)';
    return;
  }

  box.innerHTML = filtered.map(entry => {
    const tags = (entry.tags||[]).map(t =>
      `<span class="inline-block bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px] px-2.5 py-0.5 rounded-full cursor-pointer" onclick="event.stopPropagation();setSearch('${sanitizeHTML(t)}')">#${sanitizeHTML(t)}</span>`
    ).join('');
    const car = generateCarouselHTML(entry.id, entry.images);
    const prof = profilesData[entry.author] || { avatar: '^_^', name: entry.author };
    const isOwn = entry.author === currentUser;
    const av = prof.avatar_url
      ? `<img src="${prof.avatar_url}" class="w-9 h-9 rounded-xl object-cover">`
      : `<div class="w-9 h-9 rounded-xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono-custom text-xs font-bold">${sanitizeHTML(prof.avatar)}</div>`;

    return `
    <article id="post-${entry.id}" onclick="location.hash='${entry.id}'" class="bg-white dark:bg-gray-900 rounded-3xl p-4 sm:p-6 shadow-sm border border-gray-100 dark:border-gray-800 hover:shadow-md transition-all cursor-pointer animate-fade-in">
      <div class="flex justify-between items-center mb-4">
        <div class="flex items-center gap-3">
          <div onclick="event.stopPropagation();navigateToProfile('${sanitizeHTML(entry.author)}')" class="shrink-0">${av}</div>
          <div>
            <span onclick="event.stopPropagation();navigateToProfile('${sanitizeHTML(entry.author)}')" class="text-sm font-semibold hover:underline cursor-pointer block">${sanitizeHTML(prof.name)} <span class="font-normal text-gray-500">(@${sanitizeHTML(entry.author)})</span></span>
            <span class="text-[10px] text-gray-400 block">${entry.timestamp}</span>
          </div>
        </div>
        <div class="flex items-center gap-1.5">
          ${isOwn ? `
          <button onclick="event.stopPropagation();location.hash='edit/${entry.id}'" title="Редактировать" class="text-gray-400 hover:text-blue-500 bg-gray-50 dark:bg-gray-800 p-2 rounded-xl">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          </button>` : ''}
          <button onclick="event.stopPropagation();copyPostLink('${entry.id}')" title="Копировать ссылку" class="text-gray-400 hover:text-gray-700 bg-gray-50 dark:bg-gray-800 p-2 rounded-xl">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
          </button>
        </div>
      </div>
      ${entry.title?`<h3 class="text-lg font-bold mb-2">${sanitizeHTML(entry.title)}</h3>`:''}
      ${car}
      ${entry.text?`<div class="text-sm sm:text-base ${entry.font} leading-relaxed mb-3 line-clamp-4">${formatPostText(entry.text)}</div>`:''}
      ${tags?`<div class="flex flex-wrap gap-1.5 pt-2.5 border-t border-gray-50 dark:border-gray-800 mt-2">${tags}</div>`:''}
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
// Single post
// =============================================================
function renderSinglePost(entry) {
  document.title = `Запись от @${entry.author} — СЛД`;
  const prof = profilesData[entry.author] || { avatar: '^_^', name: entry.author };
  const car = generateCarouselHTML(entry.id, entry.images);
  const tags = (entry.tags||[]).map(t =>
    `<span class="inline-block bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-xs px-3 py-1.5 rounded-full cursor-pointer" onclick="setSearch('${sanitizeHTML(t)}')">#${sanitizeHTML(t)}</span>`
  ).join('');
  const av = prof.avatar_url
    ? `<img src="${prof.avatar_url}" class="w-11 h-11 rounded-2xl object-cover">`
    : `<div class="w-11 h-11 rounded-2xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono-custom text-sm font-bold">${sanitizeHTML(prof.avatar)}</div>`;
  const isOwn = entry.author === currentUser;

  document.getElementById('appContent').innerHTML = `
    <div class="mb-4 sm:mb-6">
      <a href="#/" onclick="event.preventDefault();goHome()" class="inline-flex items-center p-2.5 rounded-2xl bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 text-xs font-medium gap-2">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
        Назад к ленте
      </a>
    </div>
    <div class="bg-white dark:bg-gray-900 rounded-3xl p-5 sm:p-8 shadow-sm border border-gray-100 dark:border-gray-800 animate-fade-in">
      <div class="flex items-center justify-between mb-6 pb-4 border-b border-gray-100 dark:border-gray-800">
        <div class="flex items-center gap-3 cursor-pointer" onclick="navigateToProfile('${sanitizeHTML(entry.author)}')">
          ${av}
          <div>
            <span class="text-sm font-bold block">${sanitizeHTML(prof.name)} <span class="font-normal text-gray-500">(@${sanitizeHTML(entry.author)})</span></span>
            <span class="text-[11px] text-gray-400 block mt-0.5">${entry.timestamp}</span>
          </div>
        </div>
        <div class="flex items-center gap-1.5">
          ${isOwn?`
          <button onclick="location.hash='edit/${entry.id}'" title="Редактировать" class="text-gray-400 hover:text-blue-500 bg-gray-50 dark:bg-gray-800 p-3 rounded-xl">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          </button>
          <button onclick="deletePost('${entry.id}', true)" title="Удалить" class="text-red-400 hover:text-red-600 bg-red-50 dark:bg-red-950/30 p-3 rounded-xl">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
          </button>`:''}
          <button onclick="copyPostLink('${entry.id}')" title="Копировать" class="text-gray-400 hover:text-gray-700 bg-gray-50 dark:bg-gray-800 p-3 rounded-xl">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
          </button>
        </div>
      </div>
      ${entry.title?`<h2 class="text-xl sm:text-2xl font-bold mb-4">${sanitizeHTML(entry.title)}</h2>`:''}
      ${car}
      ${entry.text?`<div class="text-base sm:text-lg ${entry.font} leading-relaxed mb-6 mt-4 break-words whitespace-pre-wrap">${formatPostText(entry.text)}</div>`:''}
      ${tags?`<div class="flex flex-wrap gap-2 pt-5 border-t border-gray-100 dark:border-gray-800 mt-4">${tags}</div>`:''}
    </div>`;
}

// =============================================================
// Create / Edit page
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
  pendingImages = post ? [...(post.images||[])] : [];

  const fonts = [
    ['font-sans-custom','Sans'],['font-serif-custom','Serif'],['font-mono-custom','Mono'],
    ['font-playfair','Playfair'],['font-jetbrains','JetBrains'],['font-caveat','Caveat'],
    ['font-montserrat','Montserrat'],['font-merriweather','Merriweather']
  ];

  document.getElementById('appContent').innerHTML = `
    <div class="mb-4">
      <a href="#/" onclick="event.preventDefault();goHome()" class="inline-flex items-center p-2.5 rounded-2xl bg-gray-100 dark:bg-gray-800 text-xs font-medium gap-2">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M15 19l-7-7 7-7"/></svg> Назад
      </a>
    </div>
    <section class="bg-white dark:bg-gray-900 rounded-3xl shadow-sm border border-gray-100 dark:border-gray-800 p-4 sm:p-6 animate-fade-in">
      <div class="flex justify-between items-center mb-4 border-b border-gray-100 dark:border-gray-800 pb-3 gap-2 flex-wrap">
        <h2 class="text-base font-semibold">${isEdit?'Редактирование':'Новая публикация'}</h2>
        <label class="cursor-pointer text-gray-600 dark:text-gray-300 hover:text-blue-500 flex items-center gap-1.5 bg-gray-100 dark:bg-gray-800 px-3 py-1.5 rounded-xl text-[11px] font-medium">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
          Фото
          <input type="file" id="imageUploadInput" accept="image/*" multiple class="hidden">
        </label>
      </div>

      <div id="createImagePreview" class="hidden mb-4">
        <div class="text-[10px] text-gray-400 mb-2">Фото: <span id="imageCount" class="font-medium text-gray-600 dark:text-gray-300">0</span></div>
        <div id="createImageSlider" class="flex gap-3 overflow-x-auto snap-x pb-2 no-scrollbar"></div>
      </div>

      <input type="text" id="postTitleInput" value="${sanitizeHTML(post?.title||'')}" class="w-full text-base sm:text-lg font-bold bg-transparent outline-none placeholder-gray-400 mb-3 border-b border-transparent focus:border-gray-200 dark:focus:border-gray-800 pb-1" placeholder="Заголовок (необязательно)">

      <div class="flex items-center justify-between mb-3 pb-3 border-b border-gray-100 dark:border-gray-800 gap-2 flex-wrap">
        <span class="text-xs text-gray-500 font-medium">Шрифт:</span>
        <div class="flex items-center bg-gray-100 dark:bg-gray-800 p-1 rounded-xl text-[11px] font-medium overflow-x-auto max-w-full">
          ${fonts.map(([f,l]) => `<button data-font="${f}" class="create-font-option px-2 py-1 rounded-lg ${f===activeCreateFont?'bg-white dark:bg-gray-700 shadow-sm':''}">${l}</button>`).join('')}
        </div>
      </div>

      <textarea id="dataInput" class="w-full h-40 sm:h-48 resize-none outline-none text-sm sm:text-base bg-transparent placeholder-gray-400 leading-relaxed ${activeCreateFont}" placeholder="Напишите текст… (можно вставить картинку Ctrl+V)">${sanitizeHTML(post?.text||'')}</textarea>

      <div class="flex flex-col sm:flex-row justify-between items-stretch sm:items-center mt-3 pt-3 border-t border-gray-100 dark:border-gray-800 gap-3">
        <input type="text" id="tagsInput" value="${(post?.tags||[]).join(', ')}" class="outline-none text-xs sm:text-sm bg-gray-50 dark:bg-gray-800 px-3.5 py-2.5 rounded-xl w-full sm:flex-1" placeholder="Теги (через запятую)">
        <button id="saveBtn" class="bg-gray-900 dark:bg-white text-white dark:text-gray-900 px-6 py-2.5 rounded-xl text-xs sm:text-sm font-medium w-full sm:w-auto">${isEdit?'Сохранить':'Опубликовать'}</button>
      </div>
    </section>`;

  initCreateEvents();
}

window.removePendingImage = i => { pendingImages.splice(i,1); updateCreateImagePreview(); };
function updateCreateImagePreview() {
  const box = document.getElementById('createImagePreview');
  const slider = document.getElementById('createImageSlider');
  const cnt = document.getElementById('imageCount');
  if (!box) return;
  if (!pendingImages.length) { box.classList.add('hidden'); slider.innerHTML=''; return; }
  box.classList.remove('hidden');
  cnt.innerText = pendingImages.length;
  slider.innerHTML = pendingImages.map((u,i) => `
    <div class="relative w-24 h-24 sm:w-32 sm:h-32 bg-gray-100 dark:bg-gray-800 rounded-xl flex-shrink-0 snap-start overflow-hidden border border-gray-200 dark:border-gray-700">
      <img src="${u}" class="w-full h-full object-cover">
      <button onclick="removePendingImage(${i})" class="absolute top-1.5 right-1.5 bg-red-500/90 text-white rounded-full p-1.5">
        <svg class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M6 18L18 6M6 6l12 12"/></svg>
      </button>
    </div>`).join('');
}

let activeCreateFont = 'font-serif-custom';

function initCreateEvents() {
  const dataInput = document.getElementById('dataInput');
  document.querySelectorAll('.create-font-option').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.create-font-option').forEach(b => b.classList.remove('bg-white','dark:bg-gray-700','shadow-sm'));
      btn.classList.add('bg-white','dark:bg-gray-700','shadow-sm');
      activeCreateFont = btn.dataset.font;
      dataInput.className = `w-full h-40 sm:h-48 resize-none outline-none text-sm sm:text-base bg-transparent placeholder-gray-400 leading-relaxed ${activeCreateFont}`;
    });
  });

  const uploadFiles = async files => {
    if (!files.length) return;
    showToast('Загрузка…');
    const saveBtn = document.getElementById('saveBtn');
    saveBtn.disabled = true;
    for (const f of files) {
      try {
        const fd = new FormData();
        fd.append('file', f);
        const r = await fetch('/api/upload', { method: 'POST', body: fd, credentials: 'include' });
        if (!r.ok) throw new Error((await r.json()).detail || 'Ошибка');
        const j = await r.json();
        pendingImages.push(j.url);
      } catch (e) { showToast('Ошибка загрузки: ' + e.message); }
    }
    saveBtn.disabled = false;
    updateCreateImagePreview();
    showToast('Готово');
  };

  document.getElementById('imageUploadInput').addEventListener('change', e => {
    uploadFiles(Array.from(e.target.files));
    e.target.value = '';
  });

  dataInput.addEventListener('paste', e => {
    const items = (e.clipboardData||window.clipboardData).items;
    const imgs = [];
    for (const k in items) {
      const it = items[k];
      if (it.kind === 'file' && it.type.startsWith('image/')) imgs.push(it.getAsFile());
    }
    if (imgs.length) { e.preventDefault(); uploadFiles(imgs); }
  });

  document.getElementById('saveBtn').addEventListener('click', async () => {
    const text = dataInput.value.trim();
    const title = document.getElementById('postTitleInput').value.trim();
    const tagsRaw = document.getElementById('tagsInput').value;
    if (!text && !pendingImages.length && !title) return;

    const body = {
      title, text,
      tags: tagsRaw ? tagsRaw.split(',').map(t=>t.trim()).filter(Boolean) : [],
      font: activeCreateFont,
      image_urls: pendingImages,
    };

    const btn = document.getElementById('saveBtn');
    btn.disabled = true;
    try {
      if (editingPostId) {
        const updated = await api(`/api/posts/${editingPostId}`, { method:'PUT', body: JSON.stringify(body) });
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

  updateCreateImagePreview();
}

// =============================================================
// Profile page
// =============================================================
function renderProfilePage(username, isOwn) {
  const prof = profilesData[username] || { name: username, avatar: '^_^', bio: '', status: '', avatar_url: '' };
  const isEditing = isOwn && location.hash === '#profile/edit';
  document.title = `${prof.name} (@${username}) — СЛД`;

  const userPosts = database.filter(p => p.author === username);
  const avatarEl = prof.avatar_url
    ? `<img src="${prof.avatar_url}" class="w-16 h-16 sm:w-20 sm:h-20 rounded-2xl object-cover">`
    : `<div class="w-16 h-16 sm:w-20 sm:h-20 rounded-2xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono-custom text-base sm:text-lg font-bold">${sanitizeHTML(prof.avatar)}</div>`;

  document.getElementById('appContent').innerHTML = `
    <div class="mb-4 hidden sm:block">
      <a href="#/" onclick="event.preventDefault();goHome()" class="text-xs font-medium text-gray-500 hover:underline">← Назад к ленте</a>
    </div>
    <div class="bg-white dark:bg-gray-900 rounded-3xl p-5 sm:p-8 shadow-sm border border-gray-100 dark:border-gray-800 mb-8 animate-fade-in">
      <div class="flex flex-col sm:flex-row items-start sm:items-center gap-4 sm:gap-6 justify-between">
        <div class="flex items-center gap-4 w-full sm:w-auto">
          ${avatarEl}
          <div class="flex-1">
            <h2 class="text-xl sm:text-2xl font-bold">${sanitizeHTML(prof.name)}</h2>
            <p class="text-xs text-gray-500 mb-1.5">@${sanitizeHTML(username)}</p>
            ${prof.status?`<div class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400 text-[11px] font-medium mb-2">${sanitizeHTML(prof.status)}</div>`:''}
            <p class="text-xs sm:text-sm text-gray-600 dark:text-gray-300 max-w-md">${sanitizeHTML(prof.bio||'Нет описания профиля.')}</p>
          </div>
        </div>
        <div class="flex items-center gap-2 w-full sm:w-auto justify-between sm:justify-end border-t sm:border-t-0 pt-4 sm:pt-0 border-gray-100 dark:border-gray-800 mt-2 sm:mt-0">
          ${isOwn?`
            <a href="${isEditing?'#profile':'#profile/edit'}" class="flex-1 sm:flex-none text-center bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 px-5 py-2.5 rounded-xl text-xs sm:text-sm font-medium">
              ${isEditing?'Закрыть':'Настройки'}
            </a>
            <button onclick="logoutUser()" title="Выйти" class="text-red-500 bg-red-50 dark:bg-red-950/30 p-2.5 rounded-xl">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
            </button>`:''}
        </div>
      </div>

      ${isEditing?`
      <form id="editProfileForm" class="mt-6 pt-6 border-t border-gray-100 dark:border-gray-800 space-y-4 animate-fade-in">
        <h3 class="text-sm font-semibold">Настройки профиля</h3>

        <div class="flex items-center gap-4">
          <div id="avatarPreviewWrap">
            ${prof.avatar_url
              ? `<img id="avatarPreview" src="${prof.avatar_url}" class="w-20 h-20 rounded-2xl object-cover border border-gray-200 dark:border-gray-700">`
              : `<div id="avatarPreviewEmoji" class="w-20 h-20 rounded-2xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono text-lg font-bold border border-gray-200 dark:border-gray-700">${sanitizeHTML(prof.avatar)}</div>`}
          </div>
          <div class="flex-1">
            <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Аватар (картинка — до 512×512, ≤150KB, AVIF/WebP)</label>
            <label class="inline-flex items-center gap-2 cursor-pointer bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 px-3.5 py-2.5 rounded-xl text-xs font-medium">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>
              Загрузить
              <input type="file" id="avatarInput" accept="image/*" class="hidden">
            </label>
            ${prof.avatar_url?`<button type="button" onclick="removeAvatar()" class="ml-2 text-xs text-red-500 hover:underline">Удалить</button>`:''}
          </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Имя</label>
            <input type="text" id="editName" value="${sanitizeHTML(prof.name)}" class="w-full text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 outline-none">
          </div>
          <div>
            <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Статус (до 60)</label>
            <input type="text" id="editStatus" maxlength="60" value="${sanitizeHTML(prof.status)}" class="w-full text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 outline-none">
          </div>
          <div>
            <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">Символ (если нет картинки, до 5)</label>
            <input type="text" id="editAvatar" maxlength="5" value="${sanitizeHTML(prof.avatar)}" class="w-full text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 outline-none font-mono">
          </div>
        </div>
        <div>
          <label class="block text-[11px] font-medium text-gray-700 dark:text-gray-300 mb-1">О себе</label>
          <textarea id="editBio" class="w-full text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-3 outline-none resize-none h-20">${sanitizeHTML(prof.bio)}</textarea>
        </div>
        <button type="submit" class="bg-gray-900 dark:bg-white text-white dark:text-gray-900 px-5 py-2.5 rounded-xl text-xs font-medium w-full sm:w-auto">Сохранить изменения</button>
      </form>`:''}
    </div>

    <h3 class="text-base sm:text-lg font-medium mb-4">Публикации (${userPosts.length})</h3>
    <div class="space-y-4">
      ${userPosts.length===0?`<div class="text-center py-10 text-gray-400 text-xs">Пока пусто</div>`:''}
      ${userPosts.map(entry => `
        <article id="post-${entry.id}" onclick="location.hash='${entry.id}'" class="bg-white dark:bg-gray-900 rounded-3xl p-4 sm:p-5 shadow-sm border border-gray-100 dark:border-gray-800 hover:shadow-md cursor-pointer animate-fade-in">
          <div class="flex justify-between items-center mb-3">
            <span class="text-[11px] text-gray-400">${entry.timestamp}</span>
            <div class="flex items-center gap-1.5">
              ${isOwn?`
              <button onclick="event.stopPropagation();location.hash='edit/${entry.id}'" title="Редактировать" class="text-gray-400 hover:text-blue-500 bg-gray-50 dark:bg-gray-800 p-2 rounded-xl">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
              </button>
              <button onclick="event.stopPropagation();deletePost('${entry.id}')" title="Удалить" class="text-red-400 hover:text-red-600 bg-red-50 dark:bg-red-950/30 p-2 rounded-xl">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
              </button>`:''}
            </div>
          </div>
          ${entry.title?`<h3 class="text-base font-bold mb-2">${sanitizeHTML(entry.title)}</h3>`:''}
          ${generateCarouselHTML(entry.id, entry.images)}
          ${entry.text?`<div class="text-sm sm:text-base ${entry.font} leading-relaxed mb-3 line-clamp-3">${formatPostText(entry.text)}</div>`:''}
        </article>`).join('')}
    </div>`;

  if (isEditing) initProfileEditEvents();
}

function initProfileEditEvents() {
  const avatarInput = document.getElementById('avatarInput');
  if (avatarInput) {
    avatarInput.addEventListener('change', async e => {
      const f = e.target.files[0];
      if (!f) return;
      showToast('Загрузка аватара…');
      try {
        const fd = new FormData();
        fd.append('file', f);
        const r = await fetch('/api/upload', { method: 'POST', body: fd, credentials: 'include' });
        if (!r.ok) throw new Error((await r.json()).detail || 'Ошибка');
        const j = await r.json();
        const wrap = document.getElementById('avatarPreviewWrap');
        wrap.innerHTML = `<img id="avatarPreview" src="${j.url}" class="w-20 h-20 rounded-2xl object-cover border border-gray-200 dark:border-gray-700">`;
        wrap.dataset.url = j.url;
        showToast('Аватар загружен');
      } catch (err) { showToast('Ошибка: ' + err.message); }
    });
  }

  document.getElementById('editProfileForm').addEventListener('submit', async e => {
    e.preventDefault();
    const body = {
      name: document.getElementById('editName').value.trim(),
      avatar: document.getElementById('editAvatar').value.trim() || '^_^',
      avatar_url: document.getElementById('avatarPreviewWrap').dataset.url || profilesData[currentUser]?.avatar_url || '',
      status: document.getElementById('editStatus').value.trim(),
      bio: document.getElementById('editBio').value.trim(),
    };
    if (body.avatar.length > 5) return showToast('Символ до 5 знаков');
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
  wrap.innerHTML = `<div class="w-20 h-20 rounded-2xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono text-lg font-bold border border-gray-200 dark:border-gray-700">${sanitizeHTML(emoji)}</div>`;
};

// =============================================================
// Delete & copy
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
  navigator.clipboard?.writeText(url).then(
    () => showToast('Ссылка скопирована'),
    () => { const ta=document.createElement('textarea'); ta.value=url; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); showToast('Ссылка скопирована'); }
  );
};

// =============================================================
// Auth UI
// =============================================================
function openAuth() {
  document.getElementById('authModal').classList.remove('hidden');
  document.getElementById('authError').classList.add('hidden');
  setTimeout(() => document.getElementById('authUsername').focus(), 50);
}
window.openAuth = openAuth;
window.logoutUser = async function() {
  await api('/api/auth/logout', { method:'POST' });
  currentUser = null; currentUserId = null;
  updateUserInterface();
  goHome();
  showToast('Вы вышли');
};

function updateUserInterface() {
  const area = document.getElementById('desktopUserProfileArea');
  if (!area) return;
  if (currentUser) {
    const prof = profilesData[currentUser] || { avatar: '^_^' };
    const av = prof.avatar_url
      ? `<img src="${prof.avatar_url}" class="w-7 h-7 rounded-xl object-cover">`
      : `<div class="w-7 h-7 rounded-xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center font-mono text-[10px] font-bold">${sanitizeHTML(prof.avatar)}</div>`;
    area.innerHTML = `
      <div class="flex items-center gap-3">
        <a href="#profile" class="text-xs sm:text-sm hover:underline flex items-center gap-2 font-medium">
          ${av}<span>${sanitizeHTML(currentUser)}</span>
        </a>
        <button onclick="logoutUser()" title="Выйти" class="text-gray-500 hover:text-red-500 p-2 rounded-xl bg-gray-100 dark:bg-gray-800">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
        </button>
      </div>`;
  } else {
    area.innerHTML = `<button onclick="openAuth()" class="bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200 border border-gray-200 dark:border-gray-700 px-4 py-2 rounded-xl text-xs sm:text-sm font-medium shadow-sm">Вход / Регистрация</button>`;
  }
}

// =============================================================
// Auth form
// =============================================================
document.getElementById('closeAuthModal').addEventListener('click', () => {
  document.getElementById('authModal').classList.add('hidden');
});

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
    // Пробуем login → если не вышло, регистрация
    let ok = false;
    try {
      await api('/api/auth/login', { method:'POST', body: JSON.stringify({ username: u, password: p }) });
      ok = true;
    } catch (loginErr) {
      try {
        await api('/api/auth/signup', { method:'POST', body: JSON.stringify({ username: u, password: p }) });
        ok = true;
      } catch (signupErr) {
        throw new Error(signupErr.message || loginErr.message);
      }
    }
    if (ok) {
      document.getElementById('authModal').classList.add('hidden');
      document.getElementById('authForm').reset();
      showToast(`Добро пожаловать, ${u}!`);
      await fetchAllData();
    }
  } catch (err) {
    errBox.innerText = err.message;
    errBox.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.innerText = 'Войти / Создать';
  }
});

// =============================================================
// Boot
// =============================================================
(async function boot() {
  if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    document.documentElement.classList.add('dark');
  }

  try {
    const me = await api('/api/auth/me');
    if (me.authenticated) {
      currentUser = me.username;
      currentUserId = me.id;
    }
  } catch (e) {
    console.warn('auth check failed', e);
  }

  // Скрываем overlay c плавным фейдом
  const ov = document.getElementById('loadingOverlay');
  ov.style.transition = 'opacity .4s ease';
  ov.style.opacity = '0';
  setTimeout(() => ov.remove(), 420);

  await fetchAllData();
  if (!location.hash) router();
})();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/favicon.ico")
async def favicon():
    # Простая SVG-иконка "СЛД"
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#111827"/><text x="32" y="43" font-family="sans-serif" font-size="28" font-weight="700" fill="#fff" text-anchor="middle">\xd0\xa1\xd0\x9b\xd0\x94</text></svg>'''
    return Response(content=svg, media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
