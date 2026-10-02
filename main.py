"""
СЛД·NET — форум на шести цифрах.
Всё хранится в оперативной памяти. AES-256-GCM + PBKDF2-SHA256.
Никаких файлов на диске, никакого localStorage.

Запуск: pip install fastapi uvicorn python-multipart cryptography zstandard && python main.py
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import os
import re
import secrets
import string
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import uvicorn
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import (
    Cookie, FastAPI, File, Form, HTTPException, Request, Response, UploadFile,
)
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sld")

# ============================================================
# КОНФИГ
# ============================================================

MAX_TITLE_LEN = 120
MAX_CONTENT_LEN = 20_000
MAX_COMMENT_LEN = 5_000
MAX_PHOTOS = 5
MAX_PHOTO_BYTES = 5 * 1024 * 1024       # исходный размер файла
TARGET_PHOTO_BYTES = 60 * 1024          # после сжатия на клиенте
USERNAME_MIN = 3
USERNAME_MAX = 20
PASSWORD_MIN = 6
PASSWORD_MAX = 128
SESSION_TTL = 30 * 24 * 3600            # 30 дней
FEED_LIMIT = 200

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

PUBLIC_BASE_URL = os.environ.get("SLD_BASE_URL", "").rstrip("/")

# ============================================================
# КРИПТО
# ============================================================

_AES = AESGCM(AESGCM.generate_key(bit_length=256))
_NONCE_LEN = 12


def _encrypt(data: bytes) -> bytes:
    nonce = secrets.token_bytes(_NONCE_LEN)
    return nonce + _AES.encrypt(nonce, data, None)


def _decrypt(blob: bytes) -> bytes:
    if len(blob) < _NONCE_LEN + 16:
        raise ValueError("too short")
    return _AES.decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], None)


def _enc_text(s: str) -> bytes:
    return _encrypt(s.encode("utf-8"))


def _dec_text(b: bytes) -> str:
    return _decrypt(b).decode("utf-8")


def _hash_password(password: str, salt: Optional[bytes] = None):
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt, digest


def _verify_password(password: str, salt: bytes, expected: bytes) -> bool:
    _, computed = _hash_password(password, salt)
    return secrets.compare_digest(computed, expected)

# ============================================================
# ПАМЯТЬ
# ============================================================

_users: Dict[str, dict] = {}          # user_key -> {display, salt, hash, created}
_sessions: Dict[str, dict] = {}       # token -> {user_key, created, expires}
_threads: Dict[str, dict] = {}        # thread_id -> {...}
_comments: Dict[str, list] = {}       # thread_id -> [comment, ...]
_photos: Dict[str, dict] = {}         # photo_id -> {...}

_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _user_key(username: str) -> str:
    return username.strip().lower()


def _gen_id(n: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _unique_thread_id() -> str:
    with _lock:
        for _ in range(64):
            tid = _gen_id(10)
            if tid not in _threads:
                return tid
    raise HTTPException(503, "Переполнение")


def _unique_photo_id() -> str:
    with _lock:
        for _ in range(64):
            pid = _gen_id(12)
            if pid not in _photos:
                return pid
    raise HTTPException(503, "Переполнение")


def _unique_comment_id(thread_id: str) -> str:
    with _lock:
        existing = {c["id"] for c in _comments.get(thread_id, [])}
        for _ in range(64):
            cid = _gen_id(8)
            if cid not in existing:
                return cid
    raise HTTPException(503, "Переполнение")


def _base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")

# ============================================================
# ВАЛИДАЦИЯ
# ============================================================


def _validate_username(raw: str) -> str:
    username = (raw or "").strip()
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
        raise HTTPException(400, f"Имя: {USERNAME_MIN}–{USERNAME_MAX} символов")
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Имя: латиница, цифры, _ и -")
    return username


def _validate_password(raw: str) -> str:
    pw = raw or ""
    if len(pw) < PASSWORD_MIN:
        raise HTTPException(400, f"Пароль минимум {PASSWORD_MIN} символов")
    if len(pw) > PASSWORD_MAX:
        raise HTTPException(400, "Пароль слишком длинный")
    return pw


def _validate_title(raw: str) -> str:
    t = (raw or "").strip()
    if not t:
        raise HTTPException(400, "Введите заголовок")
    if len(t) > MAX_TITLE_LEN:
        raise HTTPException(400, f"Заголовок длиннее {MAX_TITLE_LEN} символов")
    return t


def _validate_content(raw: str) -> str:
    c = (raw or "").strip()
    if len(c) > MAX_CONTENT_LEN:
        raise HTTPException(400, f"Текст длиннее {MAX_CONTENT_LEN} символов")
    return c


def _validate_comment(raw: str) -> str:
    c = (raw or "").strip()
    if not c:
        raise HTTPException(400, "Пустой комментарий")
    if len(c) > MAX_COMMENT_LEN:
        raise HTTPException(400, f"Комментарий длиннее {MAX_COMMENT_LEN} символов")
    return c

# ============================================================
# АВТОРИЗАЦИЯ
# ============================================================

SESSION_COOKIE = "sld_session"


def _issue_session(user_key: str) -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = {
            "user_key": user_key,
            "created": _now(),
            "expires": _now() + SESSION_TTL,
        }
    return token


def _drop_session(token: str) -> None:
    with _lock:
        _sessions.pop(token, None)


def _current_user(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    with _lock:
        sess = _sessions.get(token)
        if not sess:
            return None
        if sess["expires"] < _now():
            _sessions.pop(token, None)
            return None
        user = _users.get(sess["user_key"])
        if not user:
            return None
        return {"key": sess["user_key"], "display": user["display"]}


def _require_user(token: Optional[str]) -> dict:
    user = _current_user(token)
    if not user:
        raise HTTPException(401, "Требуется вход")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value="",
        max_age=0,
        httponly=True,
        samesite="lax",
        path="/",
    )

# ============================================================
# ПУБЛИЧНЫЕ ПРЕДСТАВЛЕНИЯ
# ============================================================


def _public_user(user_key: str) -> dict:
    u = _users.get(user_key)
    if not u:
        return {"username": user_key}
    return {"username": u["display"]}


def _public_thread(t: dict, with_content: bool = False) -> dict:
    out = {
        "id": t["id"],
        "title": t["title"],
        "author": _public_user(t["author"])["username"],
        "created": _iso(t["created"]),
        "photos": len(t["photos"]),
        "comments": t["comment_count"],
    }
    if with_content:
        try:
            out["content"] = _dec_text(t["content_enc"])
        except Exception:
            out["content"] = ""
        out["photo_ids"] = list(t["photos"])
    return out


def _public_comment(c: dict) -> dict:
    try:
        content = _dec_text(c["content_enc"])
    except Exception:
        content = ""
    return {
        "id": c["id"],
        "author": _public_user(c["author"])["username"],
        "content": content,
        "created": _iso(c["created"]),
    }

# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(title="Sld-Networking", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Внутренняя ошибка: {type(exc).__name__}"},
    )


class AuthIn(BaseModel):
    username: str
    password: str


class CommentIn(BaseModel):
    content: str


# ---------- регистрация / вход ----------

@app.post("/api/register")
async def api_register(data: AuthIn, response: Response):
    username = _validate_username(data.username)
    password = _validate_password(data.password)
    key = _user_key(username)

    with _lock:
        if key in _users:
            raise HTTPException(409, "Имя уже занято")
        salt, digest = _hash_password(password)
        _users[key] = {
            "display": username,
            "salt": salt,
            "hash": digest,
            "created": _now(),
        }

    token = _issue_session(key)
    _set_session_cookie(response, token)
    return {"ok": True, "username": username}


@app.post("/api/login")
async def api_login(data: AuthIn, response: Response):
    username = (data.username or "").strip()
    key = _user_key(username)
    with _lock:
        user = _users.get(key)
    if not user or not _verify_password(data.password or "", user["salt"], user["hash"]):
        raise HTTPException(401, "Неверное имя или пароль")
    token = _issue_session(key)
    _set_session_cookie(response, token)
    return {"ok": True, "username": user["display"]}


@app.post("/api/logout")
async def api_logout(
    response: Response,
    sld_session: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    if sld_session:
        _drop_session(sld_session)
    _clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/me")
async def api_me(sld_session: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    user = _current_user(sld_session)
    if not user:
        raise HTTPException(401, "Не авторизован")
    return {"username": user["display"]}


# ---------- темы ----------

@app.get("/api/threads")
async def api_threads(author: Optional[str] = None):
    with _lock:
        items = list(_threads.values())

    if author:
        key = _user_key(author)
        items = [t for t in items if t["author"] == key]

    items.sort(key=lambda t: t["created"], reverse=True)
    items = items[:FEED_LIMIT]

    return {
        "threads": [_public_thread(t) for t in items],
        "total": len(items),
    }


@app.get("/api/threads/{thread_id}")
async def api_thread(
    thread_id: str,
    sld_session: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    with _lock:
        t = _threads.get(thread_id)
        comments = list(_comments.get(thread_id, []))
    if not t:
        raise HTTPException(404, "Тема не найдена")

    user = _current_user(sld_session)
    data = _public_thread(t, with_content=True)
    data["comments"] = [_public_comment(c) for c in comments]
    data["can_edit"] = bool(user and user["key"] == t["author"])
    return data


@app.post("/api/threads")
async def api_create_thread(
    title: str = Form(...),
    content: str = Form(""),
    files: Optional[List[UploadFile]] = File(None),
    sld_session: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    user = _require_user(sld_session)
    title_v = _validate_title(title)
    content_v = _validate_content(content)
    files = [f for f in (files or []) if f and f.filename]
    if not content_v and not files:
        raise HTTPException(400, "Добавьте текст или хотя бы одно фото")
    if len(files) > MAX_PHOTOS:
        raise HTTPException(400, f"Максимум {MAX_PHOTOS} фото")

    photo_ids: List[str] = []
    for f in files:
        raw = await f.read()
        if len(raw) > MAX_PHOTO_BYTES:
            raise HTTPException(
                400,
                f"Файл «{f.filename}» больше {MAX_PHOTO_BYTES // (1024 * 1024)} МБ",
            )
        pid = _unique_photo_id()
        with _lock:
            _photos[pid] = {
                "owner": user["key"],
                "mime": f.content_type or "image/webp",
                "size": len(raw),
                "enc": _encrypt(raw),
            }
        photo_ids.append(pid)

    tid = _unique_thread_id()
    with _lock:
        _threads[tid] = {
            "id": tid,
            "author": user["key"],
            "title": title_v,
            "content_enc": _enc_text(content_v),
            "photos": photo_ids,
            "created": _now(),
            "comment_count": 0,
        }
        _comments[tid] = []

    return {"ok": True, "id": tid}


# ---------- комментарии ----------

@app.post("/api/threads/{thread_id}/comments")
async def api_create_comment(
    thread_id: str,
    data: CommentIn,
    sld_session: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    user = _require_user(sld_session)
    text = _validate_comment(data.content)
    with _lock:
        t = _threads.get(thread_id)
        if not t:
            raise HTTPException(404, "Тема не найдена")
        cid = _unique_comment_id(thread_id)
        comment = {
            "id": cid,
            "thread_id": thread_id,
            "author": user["key"],
            "content_enc": _enc_text(text),
            "created": _now(),
        }
        _comments[thread_id].append(comment)
        t["comment_count"] = len(_comments[thread_id])
    return {"ok": True, "comment": _public_comment(comment)}


# ---------- фото ----------

@app.get("/api/photos/{photo_id}")
async def api_photo(photo_id: str):
    with _lock:
        p = _photos.get(photo_id)
    if not p:
        raise HTTPException(404, "Фото не найдено")
    try:
        data = _decrypt(p["enc"])
    except (InvalidTag, ValueError):
        raise HTTPException(500, "Ошибка расшифровки")
    return Response(
        content=data,
        media_type=p["mime"],
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Length": str(len(data)),
        },
    )


# ---------- статистика ----------

@app.get("/api/stats")
async def api_stats():
    with _lock:
        return {
            "users": len(_users),
            "threads": len(_threads),
            "comments": sum(len(v) for v in _comments.values()),
        }

# ============================================================
# PWA
# ============================================================

PWA_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#c4c4c8"/>'
    '</linearGradient></defs>'
    '<rect width="512" height="512" rx="112" fill="#080808"/>'
    '<rect x="96" y="96" width="320" height="320" rx="72" fill="url(#g)"/>'
    '<g fill="none" stroke="#08080a" stroke-width="26" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="256" cy="186" r="34"/><circle cx="158" cy="336" r="34"/>'
    '<circle cx="354" cy="336" r="34"/>'
    '<path d="M232 208 178 314M280 208 334 314M192 336h128"/>'
    '</g></svg>'
)

PWA_MANIFEST = {
    "name": "СЛД·NET",
    "short_name": "СЛД·NET",
    "description": "Форум по 6-значному коду. Без метаданных, с шифрованием.",
    "start_url": "/app",
    "scope": "/",
    "display": "standalone",
    "display_override": ["standalone", "minimal-ui"],
    "orientation": "any",
    "background_color": "#080808",
    "theme_color": "#080808",
    "lang": "ru",
    "dir": "ltr",
    "categories": ["social", "productivity"],
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "maskable"},
    ],
}

PWA_SW = r"""
const CACHE = 'sld-net-v8';
const SHELL_URLS = ['/app', '/icon.svg'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL_URLS).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req).then((m) => m || caches.match('/app')))
    );
    return;
  }
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
        return res;
      }).catch(() => cached))
    );
    return;
  }
  if (url.pathname === '/icon.svg' || url.pathname === '/manifest.json') {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
        return res;
      }))
    );
    return;
  }
  event.respondWith(
    fetch(req).then((res) => {
      if (res.ok && res.type === 'basic') {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req))
  );
});
"""


@app.get("/manifest.json")
async def manifest():
    return JSONResponse(content=PWA_MANIFEST, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/sw.js")
async def service_worker():
    return PlainTextResponse(
        content=PWA_SW,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/icon.svg")
async def icon():
    return Response(
        content=PWA_ICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )

# ============================================================
# UI-ФРЕЙМВОРК
# ============================================================

UI_FRAMEWORK_JS = r"""
var UI = (function(){
  "use strict";

  function h(tag, attrs, children){
    var el = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        var v = attrs[k];
        if (v == null || v === false) continue;
        if (k === 'class' || k === 'className') {
          el.className = Array.isArray(v) ? v.filter(Boolean).join(' ') : v;
        } else if (k === 'style' && typeof v === 'object') {
          for (var s in v) el.style[s] = v[s];
        } else if (k === 'dataset' && typeof v === 'object') {
          for (var d in v) el.dataset[d] = v[d];
        } else if (k === 'on' && typeof v === 'object') {
          for (var e in v) el.addEventListener(e, v[e]);
        } else if (k === 'html') {
          el.innerHTML = v;
        } else if (k === 'text') {
          el.textContent = v;
        } else if (v === true) {
          el.setAttribute(k, '');
        } else {
          el.setAttribute(k, v);
        }
      }
    }
    append(el, children);
    return el;
  }

  function append(parent, children){
    if (children == null) return parent;
    if (!Array.isArray(children)) children = [children];
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c == null || c === false || c === true) continue;
      if (typeof c === 'string' || typeof c === 'number') {
        parent.appendChild(document.createTextNode(String(c)));
      } else if (c instanceof Node) {
        parent.appendChild(c);
      } else if (Array.isArray(c)) {
        append(parent, c);
      }
    }
    return parent;
  }

  function html(str){
    var t = document.createElement('template');
    t.innerHTML = str.trim();
    return t.content.firstElementChild;
  }
  function svg(str){ return html(str); }
  function on(el, evt, fn, opts){ el.addEventListener(evt, fn, opts); return el; }
  function off(el, evt, fn, opts){ el.removeEventListener(evt, fn, opts); return el; }
  function addClass(el){ for (var i = 1; i < arguments.length; i++) el.classList.add(arguments[i]); return el; }
  function removeClass(el){ for (var i = 1; i < arguments.length; i++) el.classList.remove(arguments[i]); return el; }
  function toggle(el, cls, force){ el.classList.toggle(cls, force); return el; }
  function hasClass(el, cls){ return el.classList.contains(cls); }
  function text(el, t){ el.textContent = t == null ? '' : String(t); return el; }
  function clear(el){ while (el.firstChild) el.removeChild(el.firstChild); return el; }
  function show(el){ el.hidden = false; return el; }
  function hide(el){ el.hidden = true; return el; }
  function qs(sel, root){ return (root || document).querySelector(sel); }
  function qsa(sel, root){ return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function state(initial){
    var value = initial, subs = [];
    return {
      get: function(){ return value; },
      set: function(v){
        if (v === value) return;
        var old = value; value = v;
        for (var i = 0; i < subs.length; i++) subs[i](value, old);
      },
      subscribe: function(fn){
        subs.push(fn); fn(value, undefined);
        return function(){
          var i = subs.indexOf(fn);
          if (i >= 0) subs.splice(i, 1);
        };
      }
    };
  }

  function ensureToastContainer(){
    var c = document.getElementById('uiToastContainer');
    if (c) return c;
    c = document.createElement('div');
    c.id = 'uiToastContainer';
    c.className = 'ui-toast-container';
    document.body.appendChild(c);
    return c;
  }
  function toast(message, opts){
    opts = opts || {};
    var c = ensureToastContainer();
    var el = document.createElement('div');
    el.className = 'ui-toast' + (opts.kind ? ' ui-toast--' + opts.kind : '');
    el.textContent = String(message);
    c.appendChild(el);
    requestAnimationFrame(function(){ el.classList.add('is-visible'); });
    var dur = opts.duration || 2400;
    setTimeout(function(){
      el.classList.remove('is-visible');
      setTimeout(function(){ if (el.parentNode) el.parentNode.removeChild(el); }, 320);
    }, dur);
    return el;
  }

  async function copy(value){
    try { await navigator.clipboard.writeText(value); return true; }
    catch(e){
      try {
        var ta = document.createElement('textarea');
        ta.value = value;
        ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
      } catch(err){ return false; }
    }
  }

  function debounce(fn, ms){
    var t = 0;
    return function(){
      var ctx = this, args = arguments;
      clearTimeout(t);
      t = setTimeout(function(){ fn.apply(ctx, args); }, ms);
    };
  }
  function throttle(fn, ms){
    var last = 0, timer = 0;
    return function(){
      var ctx = this, args = arguments;
      var now = Date.now();
      if (now - last >= ms) { last = now; fn.apply(ctx, args); }
      else if (!timer) {
        timer = setTimeout(function(){
          last = Date.now(); timer = 0; fn.apply(ctx, args);
        }, ms - (now - last));
      }
    };
  }

  return {
    h: h, svg: svg, html: html, append: append,
    on: on, off: off,
    addClass: addClass, removeClass: removeClass, toggle: toggle, hasClass: hasClass,
    text: text, clear: clear,
    show: show, hide: hide,
    qs: qs, qsa: qsa,
    state: state, toast: toast, copy: copy,
    debounce: debounce, throttle: throttle
  };
})();
"""

# ============================================================
# ОБЩИЕ SVG / CSS
# ============================================================

FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='8' fill='%23080808'/%3E"
    "%3Cg fill='none' stroke='%23f4f4f5' stroke-width='1.8' stroke-linecap='round'%3E"
    "%3Ccircle cx='16' cy='10' r='3'/%3E"
    "%3Ccircle cx='10' cy='22' r='3'/%3E"
    "%3Ccircle cx='22' cy='22' r='3'/%3E"
    "%3Cpath d='M14 12.5L11 19M18 12.5L21 19M13 22h6'/%3E"
    "%3C/g%3E%3C/svg%3E"
)

LOGO_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="12" cy="5" r="2.4"/><circle cx="5" cy="19" r="2.4"/>'
    '<circle cx="19" cy="19" r="2.4"/><path d="M12 7.4 6.4 16.6M12 7.4l5.6 9.2M7.4 19h9.2"/>'
    '</svg>'
)

ARROW_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M5 12h14M13 6l6 6-6 6"/></svg>'
)

SHELL_CSS = r"""
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080808;
  --input:#0e0e0e;
  --input-focus:#141414;
  --border:rgba(255,255,255,0.07);
  --border-2:rgba(255,255,255,0.12);
  --border-3:rgba(255,255,255,0.22);
  --text:#f4f4f5;
  --text-dim:#9a9a9e;
  --text-mute:#6a6a6e;
  --glass:rgba(255,255,255,0.035);
  --glass-hi:rgba(255,255,255,0.07);
  --radius:20px;
  --ease:cubic-bezier(.2,.8,.2,1);
  --ok:#7ec899;
  --warn:#c4a054;
  --err:#e08080;
}
html{scroll-behavior:smooth}
html,body{
  background:var(--bg);color:var(--text);
  font-family:'Manrope',system-ui,-apple-system,sans-serif;
  font-size:16px;line-height:1.55;
  overflow-x:clip;min-height:100vh;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  -webkit-user-select:none;-moz-user-select:none;-ms-user-select:none;user-select:none;
  -webkit-tap-highlight-color:transparent;
}
input,textarea,[contenteditable],.modal-code{
  -webkit-user-select:text;-moz-user-select:text;-ms-user-select:text;user-select:text;
}
[hidden]{display:none !important}
::selection{background:#fff;color:#000}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:#0a0a0a}
::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:8px;border:2px solid #0a0a0a}

@media (hover:hover) and (pointer:fine){
  *, *::before, *::after { cursor:none !important; }
}
.cur-dot,.cur-ring{
  position:fixed;top:0;left:0;pointer-events:none;z-index:99999;
  border-radius:50%;will-change:transform;
  opacity:0;transition:opacity .2s ease;
}
body.cursor-ready .cur-dot,
body.cursor-ready .cur-ring { opacity:1; }
.cur-dot{width:7px;height:7px;background:#fff;box-shadow:0 0 0 1px rgba(255,255,255,0.5),0 0 14px rgba(255,255,255,0.35)}
.cur-ring{
  width:34px;height:34px;border:1.5px solid rgba(255,255,255,0.4);
  transition:width .22s var(--ease),height .22s var(--ease),
             border-color .22s,background .22s,opacity .2s;
}
.cur-ring.hover{width:60px;height:60px;border-color:rgba(255,255,255,0.22);background:rgba(255,255,255,0.05);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
.cur-ring.click{width:24px;height:24px;background:rgba(255,255,255,0.12)}
@media (max-width:900px),(hover:none){.cur-dot,.cur-ring{display:none}}

.ui-toast-container{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
  z-index:9997;display:flex;flex-direction:column;gap:8px;
  pointer-events:none;align-items:center;
  padding:0 16px;max-width:100%;
}
.ui-toast{
  background:rgba(20,20,20,0.92);
  border:1px solid var(--border-2);
  color:var(--text);
  padding:10px 16px;border-radius:12px;
  font-size:13px;font-weight:500;
  backdrop-filter:blur(20px) saturate(160%);
  -webkit-backdrop-filter:blur(20px) saturate(160%);
  box-shadow:0 12px 40px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.08);
  opacity:0;transform:translateY(10px);
  transition:opacity .24s ease, transform .24s var(--ease);
  white-space:nowrap;max-width:calc(100vw - 32px);
  overflow:hidden;text-overflow:ellipsis;
  font-family:'JetBrains Mono',monospace;letter-spacing:0.02em;
}
.ui-toast.is-visible{opacity:1;transform:translateY(0);}
.ui-toast--ok{border-color:rgba(126,200,153,0.4);}
.ui-toast--err{border-color:rgba(224,128,128,0.4);}

.scroll-top{
  position:fixed;right:20px;bottom:20px;
  bottom:max(20px, env(safe-area-inset-bottom, 0px) + 12px);
  width:44px;height:44px;
  border-radius:12px;border:1px solid var(--border-2);
  background:rgba(15,15,15,0.85);
  backdrop-filter:blur(16px) saturate(160%);
  -webkit-backdrop-filter:blur(16px) saturate(160%);
  color:var(--text);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;z-index:250;
  opacity:0;transform:translateY(10px);pointer-events:none;
  transition:opacity .25s ease, transform .25s var(--ease),
             background .18s, border-color .18s, color .18s;
  -webkit-appearance:none;appearance:none;
  box-shadow:0 12px 32px -12px rgba(0,0,0,0.7), inset 0 1px 0 rgba(255,255,255,0.06);
}
.scroll-top.visible{opacity:1;transform:translateY(0);pointer-events:auto;}
.scroll-top:hover{background:rgba(25,25,25,0.95);border-color:var(--border-3);}
.scroll-top:active{transform:scale(.94);}
.scroll-top svg{width:18px;height:18px;pointer-events:none;display:block;}

.page-loader{
  position:fixed;inset:0;z-index:99998;background:var(--bg);
  display:grid;place-items:center;
  transition:opacity .45s ease,visibility .45s;
}
.page-loader.hidden{opacity:0;visibility:hidden;pointer-events:none}
.loader-inner{display:flex;flex-direction:column;align-items:center;gap:22px}
.loader-mark{
  width:52px;height:52px;border-radius:15px;
  background:linear-gradient(140deg,#fff,#c4c4c8);
  display:grid;place-items:center;
  box-shadow:0 8px 28px -8px rgba(255,255,255,0.35);
  animation:loaderPulse 1.3s ease-in-out infinite;
}
.loader-mark svg{width:24px;height:24px;color:#08080a;display:block}
@keyframes loaderPulse{
  0%,100%{transform:scale(1);box-shadow:0 8px 28px -8px rgba(255,255,255,0.35),0 0 0 0 rgba(255,255,255,0.35)}
  50%{transform:scale(1.06);box-shadow:0 12px 34px -8px rgba(255,255,255,0.45),0 0 0 0 rgba(255,255,255,0)}
}
.loader-bar{width:140px;height:2px;background:rgba(255,255,255,0.08);border-radius:2px;overflow:hidden;position:relative}
.loader-bar::after{
  content:'';position:absolute;left:0;top:0;bottom:0;
  width:40%;background:#fff;border-radius:2px;
  animation:loaderSlide 1.3s cubic-bezier(.5,0,.5,1) infinite;
}
@keyframes loaderSlide{0%{transform:translateX(-100%)}100%{transform:translateX(300%)}}

.bg{position:fixed;inset:0;z-index:-3;overflow:hidden;background:var(--bg)}
.halo{position:absolute;border-radius:50%;filter:blur(140px);opacity:.55}
.halo-1{width:780px;height:780px;background:radial-gradient(circle,rgba(255,255,255,0.09),transparent 65%);top:-280px;left:-160px;animation:drift1 30s ease-in-out infinite}
.halo-2{width:600px;height:600px;background:radial-gradient(circle,rgba(255,255,255,0.055),transparent 65%);top:42%;right:-200px;animation:drift2 36s ease-in-out infinite}
.halo-3{width:680px;height:680px;background:radial-gradient(circle,rgba(255,255,255,0.04),transparent 65%);bottom:-240px;left:30%;animation:drift3 42s ease-in-out infinite}
@keyframes drift1{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(120px,100px) scale(1.12)}}
@keyframes drift2{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(-140px,80px) scale(1.15)}}
@keyframes drift3{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(90px,-110px) scale(.92)}}
.cursor-glow{
  position:fixed;inset:0;z-index:-2;pointer-events:none;
  background:radial-gradient(700px circle at var(--mx,50%) var(--my,30%),rgba(255,255,255,0.03),transparent 60%);
}
.grid-bg{
  position:fixed;inset:0;z-index:-1;pointer-events:none;
  background-image:
    linear-gradient(rgba(255,255,255,0.022) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,0.022) 1px,transparent 1px);
  background-size:80px 80px;
  mask-image:radial-gradient(ellipse 95% 75% at 50% 0%,#000 15%,transparent 82%);
  -webkit-mask-image:radial-gradient(ellipse 95% 75% at 50% 0%,#000 15%,transparent 82%);
}
.grain{
  position:fixed;inset:0;z-index:9998;pointer-events:none;
  opacity:.032;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}

/* Общие компоненты */
.wrap{max-width:1220px;margin:0 auto;padding:0 32px}
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  font-family:'Manrope',sans-serif;font-weight:600;font-size:13.5px;
  letter-spacing:-0.005em;
  padding:11px 20px;height:44px;
  border-radius:12px;border:1px solid transparent;
  text-decoration:none;position:relative;overflow:hidden;white-space:nowrap;
  transition:background .28s, border-color .28s, color .28s, box-shadow .28s;
  -webkit-tap-highlight-color:transparent;user-select:none;
  cursor:pointer;
}
.btn svg{width:15px;height:15px;flex-shrink:0;display:block;transition:transform .35s var(--ease)}
.btn-primary{
  background:#f4f4f5;color:#08080a;
  box-shadow:0 1px 0 rgba(255,255,255,0.7) inset,0 -1px 0 rgba(0,0,0,0.12) inset,0 6px 22px -6px rgba(255,255,255,0.22);
}
.btn-primary:hover{
  background:#fff;
  box-shadow:0 1px 0 rgba(255,255,255,0.9) inset,0 -1px 0 rgba(0,0,0,0.15) inset,0 10px 32px -8px rgba(255,255,255,0.4);
}
.btn-primary:active{transform:scale(.985)}
.btn-primary:hover svg{transform:translateX(3px)}
.btn-ghost{
  background:var(--glass);color:#fff;border-color:var(--border-2);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.08),0 4px 14px rgba(0,0,0,0.25);
}
.btn-ghost:hover{
  background:var(--glass-hi);border-color:var(--border-3);
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.14),0 10px 26px rgba(0,0,0,0.4);
}
.btn-ghost:active{transform:scale(.985)}
.btn-block{width:100%}
.btn:disabled{opacity:.5;cursor:not-allowed}

.field{
  width:100%;background:var(--input);
  border:1px solid var(--border);border-radius:12px;
  padding:12px 15px;color:var(--text);font:inherit;font-size:14px;outline:none;
  transition:border-color .18s,background .18s,box-shadow .18s;
  -webkit-appearance:none;appearance:none;
}
.field::placeholder{color:var(--text-mute)}
.field:focus{border-color:var(--border-3);background:var(--input-focus);box-shadow:0 0 0 3px rgba(255,255,255,0.04)}
textarea.field{min-height:150px;resize:none;line-height:1.55;font-family:inherit;scrollbar-width:thin}

.input-wrap{position:relative;display:flex}
.input-wrap input.field,
.input-wrap textarea.field{padding-left:40px}
.input-wrap .iw-icon{
  position:absolute;left:13px;width:16px;height:16px;
  color:var(--text-mute);pointer-events:none;transition:color .18s;
}
.input-wrap:focus-within .iw-icon{color:var(--text)}
.input-wrap .input-count{
  position:absolute;right:14px;
  font-size:10.5px;
  font-family:'JetBrains Mono',monospace;
  letter-spacing:0.04em;
  pointer-events:none;
  color:var(--ok);opacity:0.75;
  transition:color .25s, opacity .2s;
  font-variant-numeric:tabular-nums;z-index:1;
}
.input-wrap input.field{ padding-right:76px; }
.input-wrap textarea.field{ padding-right:76px; padding-bottom:32px; }
.input-wrap:not(.textarea-wrap) .input-count{ top:50%; transform:translateY(-50%); }
.input-wrap.textarea-wrap .input-count{ bottom:10px; }
.input-wrap:focus-within .input-count{ opacity:1; }
.input-wrap.warn .input-count{ color:var(--warn); }
.input-wrap.max  .input-count{ color:var(--err); }
.input-wrap:not(.textarea-wrap) .iw-icon{ top:13px; }
.input-wrap.textarea-wrap .iw-icon{ top:14px; }

.form-message{margin-top:14px}
.msg{
  display:flex;align-items:flex-start;gap:10px;
  border-radius:11px;padding:12px 14px;
  font-size:13px;
  background:rgba(255,255,255,0.03);
  color:var(--text);line-height:1.5;
  border:1px solid var(--border);
  word-break:break-word;
  animation:msgIn .3s var(--ease);
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.03);
}
.msg-icon{flex-shrink:0;display:flex;}
.msg-icon svg{width:16px;height:16px;display:block;margin-top:1px;}
.msg-text{min-width:0;word-break:break-word;}
.msg.err{background:rgba(224,128,128,0.08);border-color:rgba(224,128,128,0.32);color:#eab8b8}
.msg.ok{background:rgba(126,200,153,0.08);border-color:rgba(126,200,153,0.3);color:#b1dfc2}
@keyframes msgIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}

/* Лендинг */
body > nav.top-nav{
  position:fixed;top:0;left:0;right:0;
  z-index:100;padding:0;
  transition:padding .55s var(--ease);
  pointer-events:none;
}
body > nav.top-nav.scrolled{ padding:20px; }
body > nav.top-nav .top-nav-inner{
  pointer-events:auto;
  display:flex;align-items:center;justify-content:space-between;gap:24px;
  max-width:100%;margin:0 auto;
  padding:20px 40px;
  background:rgba(10,10,10,0.66);
  backdrop-filter:blur(24px) saturate(160%);
  -webkit-backdrop-filter:blur(24px) saturate(160%);
  border:1px solid transparent;border-bottom-color:var(--border);
  border-radius:0;
  box-shadow:0 6px 30px rgba(0,0,0,0.32);
  transition:
    max-width .55s var(--ease),padding .55s var(--ease),
    border-radius .55s var(--ease),background .4s ease,
    box-shadow .4s ease,border-color .4s ease;
}
body > nav.top-nav.scrolled .top-nav-inner{
  max-width:1180px;padding:12px 14px 12px 22px;border-radius:20px;
  background:rgba(8,8,8,0.85);
  border-color:var(--border-2);
  box-shadow:0 16px 50px rgba(0,0,0,0.7),inset 0 1px 0 rgba(255,255,255,0.08);
}
.logo{
  display:inline-flex;align-items:center;gap:11px;
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:15px;letter-spacing:-0.01em;
  text-decoration:none;color:#fff;white-space:nowrap;flex-shrink:0;
}
.logo-mark{
  width:32px;height:32px;border-radius:10px;
  background:linear-gradient(140deg,#fff,#c4c4c8);
  display:grid;place-items:center;position:relative;overflow:hidden;
  box-shadow:0 4px 16px rgba(0,0,0,0.5),inset 0 -1px 0 rgba(0,0,0,0.15);
  transition:transform .35s var(--ease);flex-shrink:0;
}
.logo:hover .logo-mark{transform:rotate(-6deg) scale(1.06)}
.logo-mark::after{content:'';position:absolute;inset:0;background:linear-gradient(150deg,rgba(255,255,255,0.9),transparent 55%);pointer-events:none}
.logo-mark svg{width:16px;height:16px;position:relative;z-index:1;color:#08080a;display:block}
.logo-word{display:inline-flex;align-items:baseline;gap:1px}
.logo-word .ldot{color:var(--text-mute);font-weight:400;margin:0 2px}
.logo-word .lnet{color:var(--text-dim);font-weight:500}

.nav-links{display:flex;gap:2px;align-items:center}
.nav-links a{
  color:var(--text-dim);text-decoration:none;
  font-size:13.5px;font-weight:500;
  padding:9px 15px;border-radius:11px;
  transition:color .2s,background .2s;
}
.nav-links a:hover{color:#fff;background:rgba(255,255,255,0.05)}
.nav-right{display:flex;align-items:center;gap:10px;flex-shrink:0;}

@media (max-width:1000px){.nav-links{display:none}}
@media (max-width:720px){
  .wrap{padding:0 20px}
  body > nav.top-nav .top-nav-inner{padding:16px 20px;gap:12px;}
  body > nav.top-nav.scrolled{padding:10px;}
  body > nav.top-nav.scrolled .top-nav-inner{padding:9px 10px 9px 14px;border-radius:14px;}
  .logo{font-size:13.5px;}
  .logo-mark{width:28px;height:28px;}
  .logo-mark svg{width:14px;height:14px;}
}

footer{padding:80px 0 50px;border-top:1px solid var(--border);margin-top:100px}
.foot-top{display:flex;justify-content:space-between;align-items:flex-start;gap:40px;flex-wrap:wrap;margin-bottom:54px;}
.foot-brand{max-width:360px}
.foot-brand .logo{margin-bottom:18px}
.foot-brand p{color:var(--text-mute);font-size:13.5px;line-height:1.7}
.foot-cols{display:flex;gap:80px;flex-wrap:wrap}
.foot-col h4{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:var(--text-mute);margin-bottom:20px;font-weight:500;}
.foot-col a{display:block;color:var(--text-dim);text-decoration:none;font-size:14.5px;padding:6px 0;transition:color .25s;width:fit-content;}
.foot-col a:hover{color:#fff}
.foot-bottom{display:flex;justify-content:space-between;align-items:center;gap:24px;flex-wrap:wrap;padding-top:30px;border-top:1px solid var(--border);color:var(--text-mute);font-size:12.5px;font-family:'JetBrains Mono',monospace;letter-spacing:0.05em;}

@media (max-width:720px){
  footer{padding:50px 0 30px;margin-top:60px;}
  .foot-cols{gap:44px}
  .foot-bottom{justify-content:center;text-align:center;flex-direction:column;gap:16px}
}
"""

SHELL_JS = r"""
(function(){
  "use strict";

  document.addEventListener("contextmenu", function(e){ e.preventDefault(); });

  var dot = document.querySelector(".cur-dot");
  var ring = document.querySelector(".cur-ring");
  if (dot && ring) {
    var mx = innerWidth/2, my = innerHeight/2;
    var rx = mx, ry = my, lx = mx, ly = my, vel = 0;
    var cursorReady = false;

    addEventListener("mousemove", function(e){
      if (!cursorReady) { cursorReady = true; document.body.classList.add("cursor-ready"); }
      mx = e.clientX; my = e.clientY;
      document.documentElement.style.setProperty("--mx", mx + "px");
      document.documentElement.style.setProperty("--my", my + "px");
    }, { passive: true });

    (function loop(){
      dot.style.transform = "translate3d(" + mx + "px," + my + "px,0) translate(-50%,-50%)";
      rx += (mx - rx) * 0.26;
      ry += (my - ry) * 0.26;
      var dx = mx - lx, dy = my - ly;
      vel = Math.min(Math.hypot(dx, dy), 60);
      lx = mx; ly = my;
      var angle = Math.atan2(dy, dx) * 180 / Math.PI;
      var stretch = 1 + vel / 150;
      var squash  = 1 - vel / 220;
      var borderOp = Math.max(0.15, 0.4 - (vel / 60) * 0.25);
      ring.style.transform = "translate3d(" + rx + "px," + ry + "px,0) translate(-50%,-50%) rotate(" + angle + "deg) scale(" + stretch + "," + squash + ")";
      if (!ring.classList.contains("hover")) ring.style.borderColor = "rgba(255,255,255," + borderOp.toFixed(2) + ")";
      requestAnimationFrame(loop);
    })();

    addEventListener("mousedown", function(){ ring.classList.add("click"); });
    addEventListener("mouseup", function(){ ring.classList.remove("click"); });

    document.querySelectorAll('a, button, .glass, input, textarea, .tab, .drop').forEach(function(el){
      el.addEventListener("mouseenter", function(){ ring.classList.add("hover"); });
      el.addEventListener("mouseleave", function(){ ring.classList.remove("hover"); });
    });
  }

  var loader = document.getElementById("pageLoader");
  if (loader) {
    var hide = function(){ loader.classList.add("hidden"); };
    if (document.readyState === "complete") setTimeout(hide, 250);
    else { addEventListener("load", function(){ setTimeout(hide, 250); }); setTimeout(hide, 2500); }
  }

  var nav = document.getElementById("nav");
  if (nav) {
    var ticking = false;
    var applyState = function(){
      nav.classList.toggle("scrolled", (window.scrollY || window.pageYOffset || 0) > 80);
    };
    applyState();
    addEventListener("scroll", function(){
      if (!ticking) {
        requestAnimationFrame(function(){ applyState(); ticking = false; });
        ticking = true;
      }
    }, { passive: true });
  }

  var scrollTopBtn = document.getElementById("scrollTopBtn");
  if (scrollTopBtn) {
    var SCROLL_SHOW_AT = 240;
    function currentScrollTop(){
      return window.scrollY || window.pageYOffset || 0;
    }
    function applyScrollTopBtn(){
      scrollTopBtn.classList.toggle("visible", currentScrollTop() > SCROLL_SHOW_AT);
    }
    applyScrollTopBtn();
    var stTicking = false;
    addEventListener("scroll", function(){
      if (stTicking) return;
      stTicking = true;
      requestAnimationFrame(function(){ applyScrollTopBtn(); stTicking = false; });
    }, { passive: true });
    addEventListener("resize", applyScrollTopBtn, { passive: true });
    scrollTopBtn.addEventListener("click", function(){
      try { window.scrollTo({ top: 0, behavior: "smooth" }); } catch(e){ window.scrollTo(0, 0); }
      setTimeout(applyScrollTopBtn, 60);
    });
  }
})();
"""

# ============================================================
# ЛЕНДИНГ
# ============================================================

LANDING_CSS = r"""
.hero{padding:170px 0 0;position:relative;overflow:hidden}
.hero-inner{max-width:900px;margin:0 auto;text-align:center;position:relative;z-index:1;padding:0 8px;}

.hero-title{
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:clamp(30px,5.6vw,64px);line-height:1.06;
  letter-spacing:-0.04em;margin-bottom:28px;
  text-wrap:balance;
}
.hero-title .dim{color:var(--text-mute);font-weight:400;display:block;margin-top:6px;}

.lead{
  font-size:clamp(15px,1.65vw,17px);line-height:1.65;
  color:var(--text-dim);max-width:600px;margin:0 auto 40px;
}
.lead strong{color:#e8e8ea;font-weight:600;white-space:nowrap}
.lead code{
  font-family:'JetBrains Mono',monospace;font-size:.92em;color:var(--text);
  background:rgba(255,255,255,0.05);padding:3px 8px;border-radius:7px;
  border:1px solid var(--border);white-space:nowrap;
}

.hero-cta{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-bottom:80px;}
.hero-cta .btn{padding:15px 28px;height:auto;font-size:14.5px;border-radius:14px}
.hero-cta .btn svg{width:16px;height:16px}

.feature-grid{
  display:grid;grid-template-columns:repeat(3,1fr);gap:14px;
  max-width:1000px;margin:0 auto;
}
.feat{
  text-align:left;padding:24px;
  border-radius:18px;
  background:linear-gradient(155deg,rgba(255,255,255,0.05),rgba(255,255,255,0.012));
  border:1px solid var(--border);
  backdrop-filter:blur(20px) saturate(150%);
  -webkit-backdrop-filter:blur(20px) saturate(150%);
  transition:transform .5s var(--ease),border-color .3s;
}
.feat:hover{transform:translateY(-4px);border-color:var(--border-2);}
.feat h3{font-family:'Unbounded',sans-serif;font-weight:600;font-size:15px;letter-spacing:-0.02em;margin-bottom:8px;}
.feat p{color:var(--text-dim);font-size:13.5px;line-height:1.6;}
.feat-icon{
  width:40px;height:40px;border-radius:12px;
  background:linear-gradient(150deg,rgba(255,255,255,0.1),rgba(255,255,255,0.02));
  border:1px solid var(--border-2);color:var(--text-dim);
  display:grid;place-items:center;margin-bottom:16px;
}
.feat-icon svg{width:18px;height:18px;display:block;}

@media (max-width:900px){ .feature-grid{grid-template-columns:1fr;} }
@media (max-width:720px){
  .hero{padding:130px 0 0}
  .hero-cta{flex-direction:column;align-items:stretch}
  .hero-cta .btn{justify-content:center}
}
"""


def build_landing() -> str:
    body = r"""
<header class="hero">
  <div class="wrap">
    <div class="hero-inner">
      <h1 class="hero-title">
        Форум без лишнего.<br>
        <span class="dim">Регистрация — и вперёд.</span>
      </h1>
      <p class="lead">
        Темы, комментарии, фото. Всё в оперативной памяти, всё шифруется.
        Регистрация занимает <strong>десять секунд</strong>. Никаких подтверждений почты и телефона.
      </p>
      <div class="hero-cta">
        <a href="/auth?tab=register" class="btn btn-primary">
          <span>Создать аккаунт</span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
        </a>
        <a href="/auth?tab=login" class="btn btn-ghost">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h4a2 2 0 012 2v14a2 2 0 01-2 2h-4"/><path d="M10 17l5-5-5-5"/><path d="M15 12H3"/></svg>
          Войти
        </a>
      </div>

      <div class="feature-grid">
        <div class="feat">
          <div class="feat-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M7 8h10M7 12h10M7 16h6"/></svg>
          </div>
          <h3>Темы и комментарии</h3>
          <p>Публикуйте темы, отвечайте в ветках. До 20 000 символов в теме, до 5 000 в комментарии.</p>
        </div>
        <div class="feat">
          <div class="feat-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 4 6v6c0 5 3.4 9.3 8 10 4.6-.7 8-5 8-10V6l-8-4z"/><rect x="9" y="11" width="6" height="5" rx="1"/><path d="M10 11V9.5a2 2 0 0 1 4 0V11"/></svg>
          </div>
          <h3>Шифрование и RAM</h3>
          <p>Посты и фото шифруются AES-256-GCM. Данные живут только в оперативной памяти.</p>
        </div>
        <div class="feat">
          <div class="feat-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/><path d="M11 8v3.5l2.5 1.5"/></svg>
          </div>
          <h3>Живой интерфейс</h3>
          <p>Мгновенная загрузка, сжатие фото прямо в браузере, ничего лишнего.</p>
        </div>
      </div>
    </div>
  </div>
</header>
"""
    return render_shell(
        title="СЛД·NET — форум без лишнего",
        body=body,
        extra_css=LANDING_CSS,
        og='<meta name="description" content="Форум с регистрацией, темами и комментариями. Всё в оперативной памяти, всё шифруется.">',
    )

# ============================================================
# СТРАНИЦА ВХОДА / РЕГИСТРАЦИИ
# ============================================================

AUTH_CSS = r"""
.auth-stage{
  min-height:100dvh;
  display:flex;align-items:center;justify-content:center;
  padding:120px 20px 60px;
}
.auth-card{
  width:100%;max-width:420px;
  padding:28px 24px 24px;border-radius:20px;
  background:linear-gradient(155deg,rgba(20,20,20,0.9),rgba(12,12,12,0.9));
  border:1px solid var(--border-2);
  box-shadow:0 30px 80px -20px rgba(0,0,0,0.9),inset 0 1px 0 rgba(255,255,255,0.06);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
}
.auth-tabs{
  display:flex;gap:4px;padding:4px;border-radius:12px;
  background:rgba(255,255,255,0.028);border:1px solid var(--border);
  margin-bottom:24px;
  position:relative;
}
.auth-tab-pill{
  position:absolute;top:4px;bottom:4px;left:4px;width:calc(50% - 4px);
  border-radius:9px;background:rgba(255,255,255,0.09);
  border:1px solid rgba(255,255,255,0.13);
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.09);
  transition:transform .34s var(--ease);
  z-index:0;pointer-events:none;
}
.auth-tab{
  position:relative;z-index:1;flex:1;
  padding:10px 14px;border-radius:9px;
  background:transparent;border:1px solid transparent;
  color:var(--text-dim);font:inherit;font-size:13.5px;font-weight:600;
  cursor:pointer;
  -webkit-tap-highlight-color:transparent;
  transition:color .22s;
}
.auth-tab.active{color:#fff;}
.auth-title{
  font-family:'Unbounded',sans-serif;
  font-size:20px;font-weight:600;letter-spacing:-0.02em;
  margin-bottom:6px;color:#fff;
}
.auth-sub{font-size:13px;color:var(--text-dim);margin-bottom:20px;line-height:1.5;}

.auth-form{display:flex;flex-direction:column;gap:10px;}
.auth-form .btn{margin-top:6px;}

.auth-foot{
  margin-top:18px;padding-top:16px;border-top:1px solid var(--border);
  font-size:12.5px;color:var(--text-mute);text-align:center;line-height:1.5;
}

@media (max-width:480px){
  .auth-stage{padding:100px 14px 40px;}
  .auth-card{padding:22px 18px 18px;}
}
"""


def build_auth() -> str:
    body = r"""
<main class="auth-stage">
  <div class="auth-card">
    <div class="auth-tabs" id="authTabs">
      <span class="auth-tab-pill" id="authTabPill" aria-hidden="true"></span>
      <button class="auth-tab active" id="tabLogin" type="button">Вход</button>
      <button class="auth-tab" id="tabRegister" type="button">Регистрация</button>
    </div>

    <h1 class="auth-title" id="authTitle">С возвращением</h1>
    <p class="auth-sub" id="authSub">Введите имя и пароль</p>

    <form class="auth-form" id="authForm" autocomplete="on">
      <div class="input-wrap">
        <input class="field" id="username" type="text"
               autocomplete="username" spellcheck="false"
               maxlength="20" placeholder="Имя (3–20 символов)">
        <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
      </div>
      <div class="input-wrap">
        <input class="field" id="password" type="password"
               autocomplete="current-password"
               maxlength="128" placeholder="Пароль (минимум 6 символов)">
        <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V6a4 4 0 018 0v4"/></svg>
      </div>
      <button class="btn btn-primary btn-block" id="authSubmit" type="submit">
        <span id="authSubmitLabel">Войти</span>
      </button>
      <div class="form-message" id="authMsg"></div>
    </form>

    <div class="auth-foot">
      Регистрация без почты. Пароль хешируется PBKDF2-SHA256 с 200 000 итераций.
    </div>
  </div>
</main>
"""
    return render_shell(
        title="Вход — СЛД·NET",
        body=body,
        extra_css=AUTH_CSS,
        og='<meta name="description" content="Вход и регистрация в СЛД·NET.">',
        extra_js=AUTH_JS,
    )


AUTH_JS = r"""
(function(){
  "use strict";
  var $ = function(id){ return document.getElementById(id); };

  var tabLogin = $("tabLogin"), tabRegister = $("tabRegister");
  var authTabPill = $("authTabPill");
  var authTitle = $("authTitle"), authSub = $("authSub");
  var authForm = $("authForm"), authMsg = $("authMsg");
  var usernameEl = $("username"), passwordEl = $("password");
  var authSubmit = $("authSubmit"), authSubmitLabel = $("authSubmitLabel");

  var mode = "login";

  function applyTab(next){
    mode = next;
    tabLogin.classList.toggle("active", next === "login");
    tabRegister.classList.toggle("active", next === "register");
    authTabPill.style.transform = (next === "register")
      ? "translateX(calc(100% + 4px))"
      : "translateX(0)";
    if (next === "register") {
      authTitle.textContent = "Создать аккаунт";
      authSub.textContent = "Придумайте имя и пароль";
      authSubmitLabel.textContent = "Зарегистрироваться";
      passwordEl.setAttribute("autocomplete", "new-password");
    } else {
      authTitle.textContent = "С возвращением";
      authSub.textContent = "Введите имя и пароль";
      authSubmitLabel.textContent = "Войти";
      passwordEl.setAttribute("autocomplete", "current-password");
    }
    authMsg.innerHTML = "";
  }

  tabLogin.addEventListener("click", function(){ applyTab("login"); });
  tabRegister.addEventListener("click", function(){ applyTab("register"); });

  // query ?tab=register
  try {
    var p = new URLSearchParams(location.search);
    if (p.get("tab") === "register") applyTab("register");
  } catch(e){}

  // Проверка: если уже залогинен — на /app
  fetch("/api/me").then(function(r){
    if (r.ok) location.replace("/app");
  }).catch(function(){});

  function msg(kind, text){
    authMsg.innerHTML = "";
    var wrap = document.createElement("div");
    wrap.className = "msg " + kind;
    var txt = document.createElement("span");
    txt.className = "msg-text";
    txt.textContent = text;
    wrap.appendChild(txt);
    authMsg.appendChild(wrap);
  }

  authForm.addEventListener("submit", async function(e){
    e.preventDefault();
    var username = (usernameEl.value || "").trim();
    var password = passwordEl.value || "";
    if (!username || !password) { msg("err", "Заполните имя и пароль."); return; }

    authSubmit.disabled = true;
    var oldLabel = authSubmitLabel.textContent;
    authSubmitLabel.textContent = "...";
    authMsg.innerHTML = "";

    try {
      var endpoint = mode === "register" ? "/api/register" : "/api/login";
      var res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username, password: password })
      });
      if (!res.ok) {
        var data = await res.json().catch(function(){ return null; });
        msg("err", (data && data.detail) || "Ошибка " + res.status);
        return;
      }
      location.replace("/app");
    } catch (err) {
      msg("err", "Ошибка сети: " + err.message);
    } finally {
      authSubmit.disabled = false;
      authSubmitLabel.textContent = oldLabel;
    }
  });
})();
"""

# ============================================================
# ПРИЛОЖЕНИЕ (SPA)
# ============================================================

APP_CSS = r"""
/* Приложение использует SHELL_CSS как базу. Ниже — только специфика. */

/* --- Топбар --- */
.app-topbar{
  position:fixed;top:0;left:0;right:0;z-index:100;
  padding:14px;
  padding-top:max(14px, env(safe-area-inset-top, 0px) + 6px);
  pointer-events:none;
}
.app-topbar-inner{
  pointer-events:auto;
  display:grid;grid-template-columns:1fr auto 1fr;
  align-items:center;gap:12px;
  max-width:960px;margin:0 auto;
  min-height:52px;padding:8px 8px 8px 16px;
  background:rgba(10,10,10,0.72);
  backdrop-filter:blur(24px) saturate(160%);
  -webkit-backdrop-filter:blur(24px) saturate(160%);
  border:1px solid var(--border);border-radius:16px;
  box-shadow:0 12px 40px rgba(0,0,0,0.5),inset 0 1px 0 rgba(255,255,255,0.06);
}
.app-topbar .logo{ grid-column:1; justify-self:start; }
.app-nav{
  grid-column:2;justify-self:center;
  display:inline-flex;gap:4px;padding:4px;border-radius:12px;
  background:rgba(255,255,255,0.028);border:1px solid var(--border);
}
.app-nav a{
  display:inline-flex;align-items:center;gap:7px;
  padding:9px 14px;border-radius:9px;
  color:var(--text-dim);text-decoration:none;
  font-size:13px;font-weight:500;white-space:nowrap;
  transition:color .22s,background .22s;
  -webkit-tap-highlight-color:transparent;
}
.app-nav a svg{width:15px;height:15px;flex-shrink:0;display:block;}
.app-nav a:hover{color:#fff;}
.app-nav a.active{color:#fff;background:rgba(255,255,255,0.07);}
.app-user{grid-column:3;justify-self:end;display:flex;align-items:center;gap:8px;}
.user-chip{
  display:inline-flex;align-items:center;gap:8px;
  padding:5px 12px 5px 5px;border-radius:100px;
  background:rgba(255,255,255,0.03);border:1px solid var(--border);
  color:var(--text);text-decoration:none;
  font-size:13px;font-weight:500;white-space:nowrap;
  transition:background .2s,border-color .2s;
  max-width:180px;
}
.user-chip:hover{background:rgba(255,255,255,0.07);border-color:var(--border-2);}
.user-chip-name{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  min-width:0;max-width:120px;
}
.avatar{
  width:28px;height:28px;border-radius:50%;
  display:grid;place-items:center;flex-shrink:0;
  font-family:'Unbounded',sans-serif;font-weight:600;
  font-size:12px;color:#08080a;
  text-transform:uppercase;
  letter-spacing:-0.02em;
}
.avatar.avatar-lg{width:40px;height:40px;font-size:16px;}
.avatar.avatar-sm{width:24px;height:24px;font-size:10px;}

.logout-btn{
  width:38px;height:38px;border-radius:11px;
  display:grid;place-items:center;
  background:rgba(255,255,255,0.03);
  border:1px solid var(--border);
  color:var(--text-mute);cursor:pointer;
  transition:background .2s,border-color .2s,color .2s;
  -webkit-appearance:none;appearance:none;
}
.logout-btn:hover{background:rgba(224,128,128,0.08);border-color:rgba(224,128,128,0.4);color:#e0a0a0;}
.logout-btn svg{width:16px;height:16px;display:block;pointer-events:none;}

/* --- Основной layout --- */
.app{
  min-height:100dvh;
  padding:calc(var(--topbar-h, 90px) + 20px) 0 80px;
}
.app-col{
  width:100%;max-width:720px;
  margin:0 auto;padding:0 20px;
}

/* --- Хлебная крошка / back --- */
.crumb{
  display:inline-flex;align-items:center;gap:7px;
  margin-bottom:16px;padding:6px 10px 6px 8px;border-radius:9px;
  color:var(--text-dim);text-decoration:none;font-size:13px;
  transition:color .2s,background .2s;
}
.crumb:hover{color:#fff;background:rgba(255,255,255,0.05);}
.crumb svg{width:14px;height:14px;display:block;}

/* --- Карточки --- */
.card{
  position:relative;border-radius:16px;
  background:linear-gradient(155deg,rgba(255,255,255,0.045),rgba(255,255,255,0.012));
  border:1px solid var(--border);
  backdrop-filter:blur(20px) saturate(150%);
  -webkit-backdrop-filter:blur(20px) saturate(150%);
  box-shadow:0 16px 40px -24px rgba(0,0,0,0.8),inset 0 1px 0 rgba(255,255,255,0.05);
  padding:18px;
}
.card + .card{margin-top:12px;}

.section-head{
  display:flex;align-items:center;justify-content:space-between;
  gap:12px;margin-bottom:16px;
}
.section-title{
  font-family:'Unbounded',sans-serif;font-weight:600;
  font-size:18px;letter-spacing:-0.02em;color:#fff;
}
.section-meta{
  font-family:'JetBrains Mono',monospace;font-size:11.5px;
  color:var(--text-mute);letter-spacing:0.1em;text-transform:uppercase;
}

/* --- Лента --- */
.thread-list{display:flex;flex-direction:column;gap:8px;}
.thread-card{
  display:block;text-decoration:none;color:inherit;
  padding:16px 18px;border-radius:14px;
  background:rgba(255,255,255,0.02);
  border:1px solid var(--border);
  transition:background .2s,border-color .2s,transform .25s var(--ease);
}
.thread-card:hover{background:rgba(255,255,255,0.05);border-color:var(--border-2);transform:translateY(-1px);}
.thread-card-head{
  display:flex;align-items:center;gap:10px;
  margin-bottom:8px;
  font-size:12.5px;color:var(--text-dim);
  font-family:'JetBrains Mono',monospace;letter-spacing:0.01em;
  flex-wrap:wrap;
}
.thread-card-head .thread-author{
  color:var(--text);
  font-family:'Manrope',sans-serif;
  font-weight:600;font-size:13px;
  text-decoration:none;
  text-overflow:ellipsis;overflow:hidden;max-width:200px;
}
.thread-card-head .thread-author:hover{text-decoration:underline;}
.thread-card-head .dot{color:var(--text-mute);}
.thread-card-title{
  font-family:'Unbounded',sans-serif;font-weight:600;
  font-size:16.5px;letter-spacing:-0.02em;line-height:1.3;
  color:#fff;margin-bottom:6px;word-break:break-word;
}
.thread-card-preview{
  font-size:13.5px;color:var(--text-dim);
  line-height:1.55;
  overflow:hidden;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical;
  word-break:break-word;
}
.thread-card-stats{
  display:flex;gap:14px;align-items:center;margin-top:10px;
  font-family:'JetBrains Mono',monospace;font-size:11px;
  color:var(--text-mute);letter-spacing:0.05em;
}
.thread-card-stats span{display:inline-flex;align-items:center;gap:5px;}
.thread-card-stats svg{width:13px;height:13px;display:block;}

/* --- Профиль --- */
.profile-head{
  display:flex;align-items:center;gap:16px;
  margin-bottom:20px;
}
.profile-info{flex:1;min-width:0;}
.profile-name{
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:22px;letter-spacing:-0.02em;color:#fff;
  margin-bottom:4px;word-break:break-word;
}
.profile-meta{
  font-size:12.5px;color:var(--text-dim);
  font-family:'JetBrains Mono',monospace;letter-spacing:0.02em;
}

/* --- Просмотр темы --- */
.thread-view{display:flex;flex-direction:column;gap:14px;}
.thread-article{padding:22px;}
.thread-article-head{
  display:flex;align-items:center;gap:12px;margin-bottom:18px;
  flex-wrap:wrap;
}
.thread-article-author{
  font-size:13.5px;color:var(--text-dim);
  display:flex;align-items:center;gap:10px;
  text-decoration:none;
}
.thread-article-author:hover .thread-article-username{text-decoration:underline;}
.thread-article-username{
  color:#fff;font-weight:600;font-size:14px;
}
.thread-article-time{
  font-family:'JetBrains Mono',monospace;font-size:11.5px;
  color:var(--text-mute);letter-spacing:0.02em;
}
.thread-article h1{
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:clamp(20px,3vw,26px);line-height:1.2;
  letter-spacing:-0.025em;color:#fff;margin-bottom:14px;
  word-break:break-word;
}
.thread-article-body{
  font-size:15px;line-height:1.7;color:var(--text);
  white-space:pre-wrap;word-break:break-word;
}
.thread-article-gallery{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));
  gap:8px;margin-top:18px;
}
.thread-article-gallery img{
  width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:10px;
  cursor:zoom-in;background:var(--input);border:1px solid var(--border);
  transition:border-color .18s,transform .35s var(--ease);
}
.thread-article-gallery img:hover{border-color:var(--border-3);transform:translateY(-2px);}

.copy-btn{
  display:inline-flex;align-items:center;gap:7px;
  margin-left:auto;
  padding:7px 12px;border-radius:9px;
  border:1px solid var(--border);background:rgba(255,255,255,0.03);
  color:var(--text-dim);
  font:inherit;font-size:12px;font-weight:500;
  cursor:pointer;-webkit-appearance:none;appearance:none;
  transition:background .18s,border-color .18s,color .18s;
  white-space:nowrap;
}
.copy-btn:hover{background:rgba(255,255,255,0.06);border-color:var(--border-2);color:#fff;}
.copy-btn.copied{color:var(--ok);border-color:rgba(126,200,153,0.5);background:rgba(126,200,153,0.08);}
.copy-btn svg{width:13px;height:13px;display:block;pointer-events:none;}
.copy-btn .sl-check{display:none;}
.copy-btn.copied .sl-copy{display:none;}
.copy-btn.copied .sl-check{display:block;}

/* --- Комментарии --- */
.comments{padding:22px;}
.comments-title{
  font-family:'Unbounded',sans-serif;font-weight:600;
  font-size:16px;letter-spacing:-0.02em;
  color:#fff;margin-bottom:16px;
  display:flex;align-items:center;gap:10px;
}
.comments-title .count{
  font-family:'JetBrains Mono',monospace;
  font-size:12px;color:var(--text-mute);
  padding:3px 8px;border-radius:6px;
  background:rgba(255,255,255,0.04);border:1px solid var(--border);
  font-weight:400;letter-spacing:0.04em;
}
.comment-list{display:flex;flex-direction:column;gap:14px;}
.comment{
  display:flex;gap:12px;
  padding-top:14px;
  border-top:1px solid var(--border);
}
.comment:first-child{padding-top:0;border-top:none;}
.comment-main{flex:1;min-width:0;}
.comment-head{
  display:flex;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap;
}
.comment-author{
  font-size:13.5px;color:#fff;font-weight:600;
  text-decoration:none;
}
.comment-author:hover{text-decoration:underline;}
.comment-time{
  font-family:'JetBrains Mono',monospace;font-size:11px;
  color:var(--text-mute);letter-spacing:0.02em;
}
.comment-body{
  font-size:14px;line-height:1.65;color:var(--text);
  white-space:pre-wrap;word-break:break-word;
}

/* --- Пусто --- */
.empty{
  text-align:center;padding:60px 20px;
  color:var(--text-mute);font-size:14px;
}
.empty-title{
  font-family:'Unbounded',sans-serif;font-weight:600;
  font-size:17px;color:var(--text-dim);letter-spacing:-0.02em;
  margin-bottom:8px;
}
.empty-sub{
  font-size:13px;color:var(--text-mute);
  line-height:1.6;max-width:340px;margin:0 auto;
}

/* --- Формы (новая тема, комментарий) --- */
.form-grid{display:flex;flex-direction:column;gap:10px;}
.form-actions{
  display:flex;gap:8px;margin-top:6px;
}
.form-actions .btn{flex:0 0 auto;min-width:120px;}
.form-actions .btn.primary{flex:1;}

.drop{
  margin-top:4px;
  border:1px dashed var(--border-2);border-radius:12px;
  padding:20px 14px;text-align:center;color:var(--text-dim);cursor:pointer;
  background:var(--input);line-height:1.55;
  transition:border-color .18s,color .18s,background .18s;
  display:flex;flex-direction:column;align-items:center;gap:7px;
}
.drop .drop-icon{width:22px;height:22px;color:var(--text-mute);transition:color .18s;display:block;}
.drop .drop-label{font-size:13px;color:var(--text-dim);transition:color .18s;}
.drop .drop-hint{font-size:11.5px;color:var(--text-mute);}
.drop:hover{border-color:var(--border-3);background:var(--input-focus);}
.drop:hover .drop-icon,.drop:hover .drop-label{color:var(--text);}
.drop.filled{border-style:solid;border-color:var(--border-3);}
.drop.filled .drop-icon,.drop.filled .drop-label{color:var(--text);}
.drop.busy{pointer-events:none;opacity:.7;}

.previews{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(72px,1fr));
  gap:8px;margin-top:10px;
}
.preview{
  position:relative;aspect-ratio:1/1;border-radius:10px;overflow:hidden;
  background:var(--input);border:1px solid var(--border);
  animation:popIn .35s var(--ease);
}
@keyframes popIn{from{opacity:0;transform:scale(.9)}to{opacity:1;transform:scale(1)}}
.preview img{width:100%;height:100%;object-fit:cover;display:block;}
.preview .pv-badge{
  position:absolute;bottom:5px;left:5px;
  font-family:'JetBrains Mono',monospace;font-size:9.5px;
  padding:2px 6px;border-radius:5px;
  background:rgba(10,10,10,0.8);border:1px solid rgba(255,255,255,0.1);
  color:var(--text-dim);letter-spacing:0.02em;
}
.preview button{
  position:absolute;top:5px;right:5px;width:22px;height:22px;border-radius:7px;
  border:1px solid rgba(255,255,255,0.18);
  background:rgba(10,10,10,0.85);color:var(--text);
  font-size:12px;line-height:1;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:background .18s,border-color .18s;
}
.preview button:hover{background:rgba(224,128,128,0.25);border-color:rgba(224,128,128,0.5);}

/* --- Скелетон --- */
.skeleton{
  border-radius:14px;
  background:linear-gradient(90deg,
    rgba(255,255,255,0.03) 0%,
    rgba(255,255,255,0.06) 50%,
    rgba(255,255,255,0.03) 100%);
  background-size:200% 100%;
  animation:shimmer 1.4s infinite;
  height:90px;margin-bottom:8px;
}
@keyframes shimmer{0%{background-position:200% 0;}100%{background-position:-200% 0;}}

/* --- LIGHTBOX --- */
.lightbox{
  position:fixed;inset:0;z-index:1000;
  display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,0.96);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  user-select:none;-webkit-user-select:none;touch-action:none;
  transition:background-color .2s ease;
  overscroll-behavior:contain;
}
.lightbox[hidden]{display:none}
.lb-viewport{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden;cursor:default;touch-action:none;}
.lb-transform{display:flex;align-items:center;justify-content:center;transform-origin:center center;will-change:transform;}
.lb-img{
  display:block;max-width:82vw;max-height:78vh;
  object-fit:contain;border-radius:8px;
  user-select:none;-webkit-user-select:none;-webkit-user-drag:none;
  background-color:#1a1a1a;
  background-image:
    linear-gradient(45deg,rgba(255,255,255,.04) 25%,transparent 25%),
    linear-gradient(-45deg,rgba(255,255,255,.04) 25%,transparent 25%),
    linear-gradient(45deg,transparent 75%,rgba(255,255,255,.04) 75%),
    linear-gradient(-45deg,transparent 75%,rgba(255,255,255,.04) 75%);
  background-size:16px 16px;
  background-position:0 0,0 8px,8px -8px,-8px 0px;
  transition:opacity .18s ease;
}
.lb-img.loading{opacity:.25}
.lb-loading{
  position:absolute;top:50%;left:50%;
  width:30px;height:30px;margin:-15px 0 0 -15px;
  border:2px solid rgba(255,255,255,0.15);border-top-color:#fff;
  border-radius:50%;animation:spin .8s linear infinite;
  z-index:3;pointer-events:none;opacity:0;transition:opacity .18s ease;
}
.lightbox.loading .lb-loading{opacity:1}
@keyframes spin{to{transform:rotate(360deg)}}
.lb-btn{
  position:absolute;width:42px;height:42px;border-radius:12px;
  border:1px solid var(--border-2);background:rgba(15,15,15,0.8);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  color:var(--text);display:flex;align-items:center;justify-content:center;
  cursor:pointer;z-index:2;
  transition:background .18s,border-color .18s,transform .18s;
}
.lb-btn svg{width:18px;height:18px;pointer-events:none;display:block;}
.lb-btn:hover{background:rgba(30,30,30,0.95);border-color:var(--border-3);}
.lb-btn:active{transform:scale(.94);}
.lb-btn[hidden]{display:none;}
.lb-close{top:16px;right:16px;}
.lb-prev{left:16px;top:50%;transform:translateY(-50%);}
.lb-next{right:16px;top:50%;transform:translateY(-50%);}
.lb-counter{
  position:absolute;bottom:20px;left:50%;transform:translateX(-50%);
  padding:7px 16px;border-radius:11px;
  background:rgba(15,15,15,0.8);backdrop-filter:blur(12px);
  -webkit-backdrop-filter:blur(12px);
  border:1px solid var(--border-2);
  font-size:12.5px;color:var(--text);
  font-family:'JetBrains Mono',monospace;letter-spacing:.06em;
  z-index:2;pointer-events:none;
}
.lb-zoom-badge{
  position:absolute;top:16px;left:16px;padding:5px 11px;border-radius:10px;
  background:rgba(15,15,15,0.8);backdrop-filter:blur(12px);
  -webkit-backdrop-filter:blur(12px);
  border:1px solid var(--border-2);
  font-size:11.5px;color:var(--text);
  font-family:'JetBrains Mono',monospace;
  z-index:2;pointer-events:none;opacity:0;
  transition:opacity .18s ease;
}
.lb-zoom-badge.visible{opacity:1;}
.lb-hint{
  position:absolute;bottom:64px;left:50%;transform:translateX(-50%);
  font-size:11.5px;color:var(--text-mute);
  z-index:2;pointer-events:none;white-space:nowrap;
  font-family:'JetBrains Mono',monospace;letter-spacing:0.02em;
  text-align:center;padding:0 16px;
}
.lb-hint-mobile{display:none;}

/* --- Мобильное --- */
@media (max-width:820px){
  .app-col{padding:0 14px;}
  .app-topbar-inner{padding:8px 8px 8px 14px;}
}
@media (max-width:640px){
  .app{padding:calc(var(--topbar-h, 90px) + 12px) 0 60px;}
  .thread-article{padding:16px;border-radius:14px;}
  .comments{padding:16px;border-radius:14px;}
  .thread-card{padding:14px;}
  .profile-head{gap:12px;}
  .profile-name{font-size:19px;}
  .form-actions{flex-direction:column-reverse;}
  .form-actions .btn{min-width:0;width:100%;}
}
@media (max-width:720px){
  .app-topbar{padding:10px;padding-top:max(10px, env(safe-area-inset-top, 0px) + 4px);}
  .app-topbar-inner{
    min-height:48px;padding:6px 6px 6px 10px;gap:8px;border-radius:14px;
    grid-template-columns:1fr;
    justify-items:center;
  }
  .app-topbar .logo{display:none;}
  .app-nav{grid-column:1;justify-self:center;padding:3px;}
  .app-nav a{padding:9px 12px;font-size:12.5px;}
  .app-user{grid-column:1;justify-self:end;position:absolute;right:6px;}
  .user-chip{padding:4px 8px 4px 4px;}
  .user-chip-name{display:none;}
  .avatar{width:24px;height:24px;font-size:10px;}
  .logout-btn{width:34px;height:34px;}
  .logout-btn svg{width:14px;height:14px;}

  .thread-card-head .thread-author{max-width:120px;}
  .thread-card-title{font-size:15.5px;}

  .lb-btn{width:44px;height:44px;}
  .lb-close{top:12px;right:12px;}
  .lb-prev,.lb-next{
    top:auto;bottom:84px;transform:none;
    width:48px;height:48px;opacity:.92;
  }
  .lb-prev{left:20px;}
  .lb-next{right:20px;}
  .lb-counter{bottom:calc(20px + env(safe-area-inset-bottom, 0px));font-size:12px;padding:6px 14px;}
  .lb-zoom-badge{top:12px;left:12px;}
  .lb-img{max-width:96vw;max-height:72vh;}
  .lb-hint{display:none;}
  .lb-hint-mobile{
    display:block;position:absolute;bottom:60px;left:50%;
    transform:translateX(-50%);
    font-size:11px;color:var(--text-mute);
    font-family:'JetBrains Mono',monospace;letter-spacing:0.02em;
    z-index:2;pointer-events:none;white-space:nowrap;
    text-align:center;max-width:92vw;padding:0 12px;
  }
}
"""

APP = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#080808">
<meta name="application-name" content="СЛД·NET">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="СЛД·NET">
<meta name="mobile-web-app-capable" content="yes">
<title>СЛД·NET</title>
<!-- OG_TAGS -->
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.svg">
<link rel="icon" href="__FAVICON__">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;600;700&family=Manrope:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>__APP_CSS__</style>
</head>
<body>

<div class="page-loader" id="pageLoader" aria-hidden="true">
  <div class="loader-inner">
    <div class="loader-mark">__LOGO_SVG__</div>
    <div class="loader-bar"></div>
  </div>
</div>

<div class="cur-dot"></div>
<div class="cur-ring"></div>

<div class="bg">
  <div class="halo halo-1"></div>
  <div class="halo halo-2"></div>
  <div class="halo halo-3"></div>
</div>
<div class="cursor-glow"></div>
<div class="grid-bg"></div>
<div class="grain"></div>

<nav class="app-topbar" id="appTopbar">
  <div class="app-topbar-inner">
    <a href="/app" class="logo">
      <span class="logo-mark">__LOGO_SVG__</span>
      <span class="logo-word">СЛД<span class="ldot">·</span><span class="lnet">NET</span></span>
    </a>

    <div class="app-nav" id="appNav">
      <a href="/app" id="navFeed" class="active">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
        <span>Лента</span>
      </a>
      <a href="/app/new" id="navNew">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
        <span>Новая тема</span>
      </a>
    </div>

    <div class="app-user" id="appUser"></div>
  </div>
</nav>

<main class="app">
  <div class="app-col" id="appRoot">
    <div class="skeleton"></div>
    <div class="skeleton" style="height:110px"></div>
    <div class="skeleton"></div>
  </div>
</main>

<button class="scroll-top" id="scrollTopBtn" type="button" aria-label="Наверх">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M12 19V5M5 12l7-7 7 7"/>
  </svg>
</button>

<div class="lightbox" id="lightbox" hidden>
  <div class="lb-viewport" id="lbViewport">
    <div class="lb-transform" id="lbTransform">
      <img class="lb-img" id="lbImg" alt="" draggable="false">
    </div>
  </div>
  <div class="lb-loading" aria-hidden="true"></div>
  <div class="lb-zoom-badge" id="lbZoomBadge">100%</div>
  <button class="lb-btn lb-close" id="lbClose" type="button" aria-label="Закрыть">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
  </button>
  <button class="lb-btn lb-prev" id="lbPrev" type="button" aria-label="Предыдущее">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg>
  </button>
  <button class="lb-btn lb-next" id="lbNext" type="button" aria-label="Следующее">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6"/></svg>
  </button>
  <div class="lb-counter" id="lbCounter">1 / 1</div>
  <div class="lb-hint">колесо — зум · ПКМ — 1×/2× · 2× клик — сброс · ЛКМ — панорама</div>
  <div class="lb-hint-mobile">свайп — листать · 2× тап — зум · свайп вниз — закрыть</div>
</div>

<script>__UI_FRAMEWORK__</script>
<script>
(function(){
  "use strict";
  var $ = function(id){ return document.getElementById(id); };

  document.addEventListener("contextmenu", function(e){ e.preventDefault(); });

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function(){
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function(){});
    });
  }

  /* ============ CURSOR ============ */
  var dot = document.querySelector(".cur-dot");
  var ring = document.querySelector(".cur-ring");
  if (dot && ring) {
    var mx = innerWidth/2, my = innerHeight/2;
    var rx = mx, ry = my, lx = mx, ly = my, vel = 0;
    var cursorReady = false;
    addEventListener("mousemove", function(e){
      if (!cursorReady) { cursorReady = true; document.body.classList.add("cursor-ready"); }
      mx = e.clientX; my = e.clientY;
      document.documentElement.style.setProperty("--mx", mx + "px");
      document.documentElement.style.setProperty("--my", my + "px");
    }, { passive: true });
    (function loop(){
      dot.style.transform = "translate3d(" + mx + "px," + my + "px,0) translate(-50%,-50%)";
      rx += (mx - rx) * 0.26;
      ry += (my - ry) * 0.26;
      var dx = mx - lx, dy = my - ly;
      vel = Math.min(Math.hypot(dx, dy), 60);
      lx = mx; ly = my;
      var angle = Math.atan2(dy, dx) * 180 / Math.PI;
      var stretch = 1 + vel / 150;
      var squash  = 1 - vel / 220;
      var borderOp = Math.max(0.15, 0.4 - (vel / 60) * 0.25);
      ring.style.transform = "translate3d(" + rx + "px," + ry + "px,0) translate(-50%,-50%) rotate(" + angle + "deg) scale(" + stretch + "," + squash + ")";
      if (!ring.classList.contains("hover")) ring.style.borderColor = "rgba(255,255,255," + borderOp.toFixed(2) + ")";
      requestAnimationFrame(loop);
    })();
    addEventListener("mousedown", function(){ ring.classList.add("click"); });
    addEventListener("mouseup", function(){ ring.classList.remove("click"); });
    UI.qsa('a, button, input, textarea, label, .thread-card, .drop, .copy-btn, .user-chip, .avatar').forEach(function(el){
      UI.on(el, 'mouseenter', function(){ ring.classList.add("hover"); });
      UI.on(el, 'mouseleave', function(){ ring.classList.remove("hover"); });
    });
  }

  /* ============ LOADER ============ */
  var loader = $('pageLoader');
  if (loader) {
    var hideLoader = function(){ loader.classList.add('hidden'); };
    if (document.readyState === "complete") setTimeout(hideLoader, 250);
    else { addEventListener("load", function(){ setTimeout(hideLoader, 250); }); setTimeout(hideLoader, 2500); }
  }

  /* ============ TOPBAR HEIGHT ============ */
  var topbar = $('appTopbar');
  function measureTopbar(){
    document.documentElement.style.setProperty('--topbar-h', topbar.offsetHeight + 'px');
  }
  measureTopbar();
  addEventListener("resize", measureTopbar, { passive: true });
  addEventListener("orientationchange", function(){ setTimeout(measureTopbar, 300); });

  /* ============ SCROLL TOP ============ */
  var scrollTopBtn = $('scrollTopBtn');
  function applyScrollTopBtn(){
    var y = window.scrollY || window.pageYOffset || 0;
    scrollTopBtn.classList.toggle('visible', y > 240);
  }
  applyScrollTopBtn();
  var stTicking = false;
  addEventListener("scroll", function(){
    if (stTicking) return;
    stTicking = true;
    requestAnimationFrame(function(){ applyScrollTopBtn(); stTicking = false; });
  }, { passive: true });
  scrollTopBtn.addEventListener("click", function(){
    try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch(e){ window.scrollTo(0, 0); }
  });

  /* ============ ICONS ============ */
  var ICONS = {
    error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
    ok:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>',
    copy:  '<svg class="sl-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>',
    check: '<svg class="sl-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>',
    back:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5M11 6l-6 6 6 6"/></svg>',
    logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>',
    comments: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z"/></svg>',
    photos: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>'
  };

  var AVATAR_COLORS = [
    '#d4b8a0','#a0b8d4','#b8d4a0','#d4a0b8',
    '#c8b8a0','#a0c8c8','#c8a0a0','#b8a0d4',
    '#a0d4b8','#d4c8a0','#a0a0d4','#d4a0d0'
  ];
  function avatarColor(name){
    var h = 0;
    var s = String(name || '?');
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return AVATAR_COLORS[h % AVATAR_COLORS.length];
  }
  function makeAvatar(name, size){
    var cls = 'avatar' + (size === 'lg' ? ' avatar-lg' : (size === 'sm' ? ' avatar-sm' : ''));
    var initial = String(name || '?').trim().charAt(0) || '?';
    return UI.h('span', {
      class: cls,
      style: { background: avatarColor(name) },
      text: initial
    });
  }

  /* ============ HELPERS ============ */
  function makeMsg(kind, text){
    return UI.h('div', { class: 'msg ' + kind }, [
      UI.h('span', { class: 'msg-icon', html: kind === 'err' ? ICONS.error : ICONS.ok }),
      UI.h('span', { class: 'msg-text', text: String(text == null ? '' : text) })
    ]);
  }
  async function readError(res){
    var ct = (res.headers.get("content-type") || "").toLowerCase();
    if (ct.indexOf("application/json") >= 0) {
      var data = await res.json().catch(function(){ return null; });
      if (data) {
        if (typeof data.detail === "string" && data.detail.trim()) return data.detail;
        if (Array.isArray(data.detail) && data.detail.length) return (data.detail[0] || {}).msg || "Некорректные данные";
        if (typeof data.message === "string" && data.message.trim()) return data.message;
      }
    }
    var txt = await res.text().catch(function(){ return ""; });
    if (txt && txt.trim() && txt.length < 400) return txt.trim();
    return "Ошибка " + res.status;
  }
  function fmtTime(iso){
    try {
      var d = new Date(iso);
      var now = new Date();
      var diff = (now - d) / 1000;
      if (diff < 60) return "только что";
      if (diff < 3600) return Math.floor(diff/60) + " мин назад";
      if (diff < 86400) return Math.floor(diff/3600) + " ч назад";
      if (diff < 7*86400) return Math.floor(diff/86400) + " дн назад";
      return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: (d.getFullYear() === now.getFullYear() ? undefined : "numeric") });
    } catch(e){ return ""; }
  }
  function short(text, n){
    text = String(text || "").replace(/\s+/g, " ").trim();
    if (text.length <= n) return text;
    return text.slice(0, n - 1).trimEnd() + "…";
  }

  /* ============ STATE ============ */
  var me = null;               // { username }
  var appRoot = $('appRoot');
  var appUser = $('appUser');
  var navFeed = $('navFeed');
  var navNew = $('navNew');

  function setNavActive(view){
    navFeed.classList.toggle('active', view === 'feed' || view === 'thread');
    navNew.classList.toggle('active', view === 'new');
  }

  function renderUserChip(){
    UI.clear(appUser);
    if (!me) return;
    var username = me.username;
    var chip = UI.h('a', {
      href: '/u/' + encodeURIComponent(username),
      class: 'user-chip',
      title: username
    }, [
      makeAvatar(username),
      UI.h('span', { class: 'user-chip-name', text: username })
    ]);
    var logout = UI.h('button', {
      type: 'button', class: 'logout-btn',
      title: 'Выйти', 'aria-label': 'Выйти',
      html: ICONS.logout
    });
    UI.on(logout, 'click', async function(){
      logout.disabled = true;
      try {
        await fetch('/api/logout', { method: 'POST' });
      } catch(e){}
      location.replace('/auth');
    });
    appUser.append(chip, logout);
  }

  function requireMe(){
    return fetch('/api/me').then(function(r){
      if (!r.ok) { location.replace('/auth'); return Promise.reject('no-auth'); }
      return r.json();
    }).then(function(data){
      me = data;
      renderUserChip();
      return data;
    });
  }

  /* ============ LIGHTBOX ============ */
  var lightbox = $('lightbox'), lbViewport = $('lbViewport'), lbTransform = $('lbTransform');
  var lbImg = $('lbImg'), lbCounter = $('lbCounter'), lbPrev = $('lbPrev'), lbNext = $('lbNext');
  var lbClose = $('lbClose'), lbZoomBadge = $('lbZoomBadge');
  var lbPhotos = [], lbIndex = 0;
  var zoom = 1, panX = 0, panY = 0, swipeY = 0;
  var MIN_ZOOM = 1, MAX_ZOOM = 8;
  var isPanning = false, panStartX = 0, panStartY = 0, badgeTimer = null;
  var inertiaRaf = 0, lbLoadTimer = null;

  function lbStopLoading(){
    clearTimeout(lbLoadTimer); lbLoadTimer = null;
    lightbox.classList.remove('loading');
  }
  lbImg.addEventListener('load', lbStopLoading);
  lbImg.addEventListener('error', lbStopLoading);

  function applyTransform(){
    var totalY = panY + swipeY;
    lbTransform.style.transform = "translate(" + panX + "px," + totalY + "px) scale(" + zoom + ")";
    lbViewport.style.cursor = (zoom > 1.001) ? (isPanning ? "grabbing" : "grab") : "default";
    if (swipeY > 0) {
      var fade = Math.max(0.15, 1 - swipeY / 420);
      lightbox.style.background = "rgba(0,0,0," + (0.96 * fade).toFixed(3) + ")";
      lbTransform.style.opacity = Math.max(0.35, fade).toFixed(3);
    } else {
      lightbox.style.background = "";
      lbTransform.style.opacity = "";
    }
  }
  function stopInertia(){ if (inertiaRaf) { cancelAnimationFrame(inertiaRaf); inertiaRaf = 0; } }
  function showZoomBadge(){
    lbZoomBadge.textContent = Math.round(zoom * 100) + "%";
    lbZoomBadge.classList.add("visible");
    clearTimeout(badgeTimer);
    badgeTimer = setTimeout(function(){ lbZoomBadge.classList.remove("visible"); }, 900);
  }
  function animateTransformTo(targetZoom, targetPanX, targetPanY, duration){
    duration = duration || 220;
    var sZ = zoom, sX = panX, sY = panY;
    var t0 = performance.now();
    function step(){
      var t = Math.min(1, (performance.now() - t0) / duration);
      var eased = 1 - Math.pow(1 - t, 3);
      zoom = sZ + (targetZoom - sZ) * eased;
      panX = sX + (targetPanX - sX) * eased;
      panY = sY + (targetPanY - sY) * eased;
      applyTransform();
      if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  function resetZoom(animate){
    if (animate) animateTransformTo(1, 0, 0, 220);
    else { zoom = 1; panX = 0; panY = 0; swipeY = 0; applyTransform(); }
    showZoomBadge();
  }
  function zoomAt(clientX, clientY, newZoom, animate){
    newZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, newZoom));
    if (Math.abs(newZoom - zoom) < 1e-4) return;
    var vrect = lbViewport.getBoundingClientRect();
    var Cx = vrect.left + vrect.width / 2, Cy = vrect.top + vrect.height / 2;
    var dcx = clientX - Cx, dcy = clientY - Cy;
    var ratio = newZoom / zoom;
    var targetPanX = dcx - (dcx - panX) * ratio;
    var targetPanY = dcy - (dcy - panY) * ratio;
    if (animate) animateTransformTo(newZoom, targetPanX, targetPanY, 220);
    else { panX = targetPanX; panY = targetPanY; zoom = newZoom; lbTransform.style.transition = ""; applyTransform(); }
    showZoomBadge();
  }
  function openLightbox(photoIds, index){
    lbPhotos = photoIds; lbIndex = index;
    zoom = 1; panX = 0; panY = 0; swipeY = 0;
    lbTransform.style.transition = ""; applyTransform();
    lightbox.classList.add('loading');
    clearTimeout(lbLoadTimer);
    lbLoadTimer = setTimeout(lbStopLoading, 8000);
    lbImg.src = "/api/photos/" + encodeURIComponent(photoIds[index]);
    lbImg.alt = "";
    lbCounter.textContent = (index + 1) + " / " + photoIds.length;
    lbPrev.hidden = photoIds.length < 2; lbNext.hidden = photoIds.length < 2;
    lightbox.hidden = false;
  }
  function closeLightbox(){
    stopInertia();
    lightbox.hidden = true;
    lbImg.removeAttribute("src");
    lbPhotos = [];
    swipeY = 0; panX = 0; panY = 0; zoom = 1;
    lbTransform.style.transform = "";
    lbTransform.style.opacity = "";
    lightbox.style.background = "";
    lightbox.classList.remove('loading');
  }
  function lbStep(dir){
    if (lbPhotos.length < 2) return;
    lbIndex = (lbIndex + dir + lbPhotos.length) % lbPhotos.length;
    zoom = 1; panX = 0; panY = 0; swipeY = 0;
    lbTransform.style.transition = ""; applyTransform();
    lightbox.classList.add('loading');
    clearTimeout(lbLoadTimer);
    lbLoadTimer = setTimeout(lbStopLoading, 8000);
    lbImg.src = "/api/photos/" + encodeURIComponent(lbPhotos[lbIndex]);
    lbCounter.textContent = (lbIndex + 1) + " / " + lbPhotos.length;
  }
  lbPrev.addEventListener("click", function(e){ e.stopPropagation(); lbStep(-1); });
  lbNext.addEventListener("click", function(e){ e.stopPropagation(); lbStep(1); });
  lbClose.addEventListener("click", function(e){ e.stopPropagation(); closeLightbox(); });
  lbViewport.addEventListener("click", function(e){ if (e.target === lbViewport && zoom <= 1.001 && swipeY === 0) closeLightbox(); });
  lbViewport.addEventListener("wheel", function(e){
    e.preventDefault();
    var factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
    zoomAt(e.clientX, e.clientY, zoom * factor);
  }, { passive: false });
  lbViewport.addEventListener("mousedown", function(e){
    if (e.button === 2) { e.preventDefault(); if (zoom > 1.05) resetZoom(true); else zoomAt(e.clientX, e.clientY, 2); return; }
    if (e.button === 0 && zoom > 1.001) {
      e.preventDefault(); stopInertia();
      isPanning = true;
      panStartX = e.clientX - panX; panStartY = e.clientY - panY;
      lbTransform.style.transition = "none";
      lbViewport.style.cursor = "grabbing";
    }
  });
  lbViewport.addEventListener("dblclick", function(e){
    e.preventDefault();
    if (zoom > 1.05) resetZoom(true); else zoomAt(e.clientX, e.clientY, 2, true);
  });
  addEventListener("mousemove", function(e){
    if (!isPanning) return;
    panX = e.clientX - panStartX; panY = e.clientY - panStartY; applyTransform();
  });
  addEventListener("mouseup", function(e){
    if (e.button === 0 && isPanning) {
      isPanning = false; lbTransform.style.transition = "";
      lbViewport.style.cursor = zoom > 1.001 ? "grab" : "default";
    }
  });

  /* TOUCH */
  var tMode = 'idle';
  var tStartX = 0, tStartY = 0, tLastX = 0, tLastY = 0;
  var tStartTime = 0, tLastFrameTime = 0;
  var tPinchStartDist = 0, tPinchStartZoom = 1;
  var tPinchMidX = 0, tPinchMidY = 0;
  var tPinchStartPanX = 0, tPinchStartPanY = 0;
  var tVelX = 0, tVelY = 0;
  var tTapTime = 0, tTapX = 0, tTapY = 0;

  function startInertia(vx, vy){
    stopInertia();
    function step(){
      vx *= 0.94; vy *= 0.94;
      panX += vx; panY += vy;
      applyTransform();
      if (Math.abs(vx) > 0.5 || Math.abs(vy) > 0.5) inertiaRaf = requestAnimationFrame(step);
      else inertiaRaf = 0;
    }
    inertiaRaf = requestAnimationFrame(step);
  }

  lbViewport.addEventListener("touchstart", function(e){
    stopInertia();
    lbTransform.style.transition = "none";
    if (e.touches.length === 2) {
      tMode = 'pinch';
      tPinchStartDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      ) || 1;
      tPinchStartZoom = zoom;
      tPinchMidX = (e.touches[0].clientX + e.touches[1].clientX) / 2;
      tPinchMidY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      tPinchStartPanX = panX; tPinchStartPanY = panY;
    } else if (e.touches.length === 1) {
      tStartX = e.touches[0].clientX;
      tStartY = e.touches[0].clientY;
      tLastX = tStartX; tLastY = tStartY;
      tStartTime = performance.now();
      tLastFrameTime = tStartTime;
      tVelX = 0; tVelY = 0;
      swipeY = 0;
      tMode = (zoom <= 1.05) ? 'swipe-down' : 'pan';
    }
  }, { passive: true });

  lbViewport.addEventListener("touchmove", function(e){
    if (tMode === 'idle') return;
    e.preventDefault();
    if (tMode === 'pinch' && e.touches.length === 2) {
      var dist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      ) || 1;
      var scale = dist / tPinchStartDist;
      var newZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, tPinchStartZoom * scale));
      var midX = (e.touches[0].clientX + e.touches[1].clientX) / 2;
      var midY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      var vrect = lbViewport.getBoundingClientRect();
      var cx = vrect.left + vrect.width / 2;
      var cy = vrect.top + vrect.height / 2;
      var relX = tPinchMidX - cx - tPinchStartPanX;
      var relY = tPinchMidY - cy - tPinchStartPanY;
      var ratio = newZoom / tPinchStartZoom;
      panX = -relX * ratio + (midX - cx) + tPinchStartPanX;
      panY = -relY * ratio + (midY - cy) + tPinchStartPanY;
      zoom = newZoom;
      applyTransform(); showZoomBadge();
      return;
    }
    if (e.touches.length === 1) {
      var tx = e.touches[0].clientX;
      var ty = e.touches[0].clientY;
      var dx = tx - tLastX;
      var dy = ty - tLastY;
      var totalDx = tx - tStartX;
      var totalDy = ty - tStartY;
      if (tMode === 'swipe-down') {
        if (Math.abs(totalDx) > 12 && Math.abs(totalDx) > Math.abs(totalDy)) tMode = 'pan';
        else if (totalDy < -12 && Math.abs(totalDy) > Math.abs(totalDx)) tMode = 'pan';
      }
      if (tMode === 'swipe-down' && zoom <= 1.05) { swipeY = Math.max(0, totalDy); applyTransform(); }
      else if (tMode === 'pan') { panX += dx; panY += dy; applyTransform(); }
      var now = performance.now();
      var dt = now - tLastFrameTime;
      if (dt > 8) {
        tVelX = (tx - tLastX) / dt * 16;
        tVelY = (ty - tLastY) / dt * 16;
        tLastFrameTime = now; tLastX = tx; tLastY = ty;
      }
    }
  }, { passive: false });

  lbViewport.addEventListener("touchend", function(e){
    var modeAtEnd = tMode;
    var wasPinch = (modeAtEnd === 'pinch');
    tMode = 'idle';
    if (wasPinch) {
      if (zoom < 1) { animateTransformTo(1, 0, 0, 220); zoom = 1; }
      else if (zoom > MAX_ZOOM) animateTransformTo(MAX_ZOOM, panX, panY, 180);
      return;
    }
    var t = e.changedTouches[0];
    if (!t) return;
    var dx = t.clientX - tStartX;
    var dy = t.clientY - tStartY;
    var dt = performance.now() - tStartTime;
    var dist = Math.hypot(dx, dy);
    if (modeAtEnd === 'swipe-down' && swipeY > 0) {
      var velocity = dist / Math.max(1, dt) * 16;
      if (swipeY > 120 || (dy > 60 && velocity > 12)) { closeLightbox(); return; }
      var startSwipe = swipeY;
      var t0 = performance.now();
      (function easeBack(){
        var t2 = (performance.now() - t0) / 220;
        if (t2 >= 1) { swipeY = 0; applyTransform(); return; }
        var eased = 1 - Math.pow(1 - t2, 3);
        swipeY = startSwipe * (1 - eased);
        applyTransform();
        requestAnimationFrame(easeBack);
      })();
      return;
    }
    if (dt < 300 && dist < 12) {
      var now = performance.now();
      if (now - tTapTime < 320 && Math.hypot(t.clientX - tTapX, t.clientY - tTapY) < 44) {
        if (zoom > 1.05) resetZoom(true); else zoomAt(t.clientX, t.clientY, 2.4, true);
        tTapTime = 0; return;
      }
      tTapTime = now; tTapX = t.clientX; tTapY = t.clientY;
    }
    if (modeAtEnd === 'pan' && zoom <= 1.05 && dt < 600 && dist > 40) {
      if (Math.abs(dx) > Math.abs(dy) * 1.2 && Math.abs(dx) > 55) {
        if (dx < 0) lbStep(1); else lbStep(-1);
        return;
      }
    }
    if (modeAtEnd === 'pan' && zoom > 1.05 && (Math.abs(tVelX) > 1 || Math.abs(tVelY) > 1)) {
      startInertia(tVelX, tVelY);
    }
  }, { passive: true });

  document.addEventListener("keydown", function(e){
    if (!lightbox.hidden) {
      if (e.key === "Escape") { closeLightbox(); return; }
      if (e.key === "ArrowLeft")  { lbStep(-1); return; }
      if (e.key === "ArrowRight") { lbStep(1);  return; }
      if (e.key === "0") { resetZoom(true); return; }
    }
  });

  /* ============ IMAGE COMPRESSION ============ */
  var TARGET_PHOTO_BYTES = 60 * 1024;
  var SOURCE_MAX_BYTES = 5 * 1024 * 1024;

  var SUPPORTS_WEBP = (function(){
    try {
      var c = document.createElement('canvas');
      c.width = c.height = 1;
      return c.toDataURL('image/webp').indexOf('data:image/webp') === 0;
    } catch(e) { return false; }
  })();
  var OUT_MIME = SUPPORTS_WEBP ? 'image/webp' : 'image/jpeg';
  var OUT_EXT = SUPPORTS_WEBP ? '.webp' : '.jpg';

  async function compressImage(file, targetBytes){
    if (targetBytes === undefined) targetBytes = TARGET_PHOTO_BYTES;
    if (file.type.indexOf("image/") !== 0) return file;
    if (file.size <= targetBytes && (file.type === OUT_MIME)) return file;
    try {
      var bitmap = await createImageBitmap(file);
      var maxDim = 1600;
      var best = null;
      for (var attempt = 0; attempt < 3; attempt++) {
        var scale = Math.min(1, maxDim / Math.max(bitmap.width, bitmap.height));
        var w = Math.max(1, Math.round(bitmap.width * scale));
        var h = Math.max(1, Math.round(bitmap.height * scale));
        var canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        var ctx = canvas.getContext("2d");
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = "high";
        ctx.drawImage(bitmap, 0, 0, w, h);
        var lo = SUPPORTS_WEBP ? 0.4 : 0.35;
        var hi = SUPPORTS_WEBP ? 0.95 : 0.92;
        var candidate = null;
        for (var i = 0; i < 7; i++) {
          var q = (lo + hi) / 2;
          var blob = await new Promise(function(r){ canvas.toBlob(r, OUT_MIME, q); });
          if (!blob) break;
          if (blob.size <= targetBytes) { candidate = blob; lo = q; }
          else { hi = q; }
        }
        if (candidate) { best = candidate; break; }
        var fallback = await new Promise(function(r){ canvas.toBlob(r, OUT_MIME, SUPPORTS_WEBP ? 0.55 : 0.5); });
        if (fallback) best = fallback;
        maxDim = Math.round(maxDim * 0.72);
        if (maxDim < 500) break;
      }
      if (bitmap.close) bitmap.close();
      if (!best) return file;
      var newName = (file.name || 'photo').replace(/\.[^.]+$/, "") + OUT_EXT;
      return new File([best], newName, { type: OUT_MIME });
    } catch (e) {
      console.warn("compress failed:", e);
      return file;
    }
  }

  /* ============ VIEWS ============ */

  function renderEmpty(title, sub){
    appRoot.innerHTML = "";
    appRoot.appendChild(UI.h('div', { class: 'empty' }, [
      UI.h('div', { class: 'empty-title', text: title }),
      UI.h('div', { class: 'empty-sub', text: sub })
    ]));
  }

  function renderError(text){
    appRoot.innerHTML = "";
    var card = UI.h('div', { class: 'card' });
    card.appendChild(makeMsg('err', text));
    appRoot.appendChild(card);
  }

  function renderLoadingSkeleton(){
    appRoot.innerHTML = "";
    appRoot.appendChild(UI.h('div', { class: 'skeleton' }));
    appRoot.appendChild(UI.h('div', { class: 'skeleton', style: { height: '110px' } }));
    appRoot.appendChild(UI.h('div', { class: 'skeleton' }));
  }

  function threadStat(icon, value){
    return UI.h('span', {}, [
      UI.h('span', { html: icon, style: { display: 'inline-flex' } }),
      UI.h('span', { text: String(value) })
    ]);
  }

  function threadCard(t){
    var card = UI.h('a', {
      href: '/t/' + encodeURIComponent(t.id),
      class: 'thread-card'
    });
    var head = UI.h('div', { class: 'thread-card-head' });
    head.appendChild(makeAvatar(t.author, 'sm'));
    var authorLink = UI.h('a', {
      href: '/u/' + encodeURIComponent(t.author),
      class: 'thread-author',
      text: t.author
    });
    UI.on(authorLink, 'click', function(e){ e.stopPropagation(); });
    head.appendChild(authorLink);
    head.appendChild(UI.h('span', { class: 'dot', text: '·' }));
    head.appendChild(UI.h('span', { text: fmtTime(t.created) }));
    card.appendChild(head);
    card.appendChild(UI.h('div', { class: 'thread-card-title', text: t.title }));

    var stats = UI.h('div', { class: 'thread-card-stats' }, [
      threadStat(ICONS.comments, t.comments),
      threadStat(ICONS.photos, t.photos)
    ]);
    card.appendChild(stats);
    return card;
  }

  function renderFeed(threads){
    appRoot.innerHTML = "";

    var head = UI.h('div', { class: 'section-head' }, [
      UI.h('div', { class: 'section-title', text: 'Лента' }),
      UI.h('div', { class: 'section-meta', text: threads.length + ' ' + plural(threads.length, 'тема', 'темы', 'тем') })
    ]);
    appRoot.appendChild(head);

    if (!threads.length) {
      renderEmpty('Пока пусто', 'Ни одной темы ещё нет. Создайте первую — это займёт минуту.');
      return;
    }

    var list = UI.h('div', { class: 'thread-list' });
    threads.forEach(function(t){ list.appendChild(threadCard(t)); });
    appRoot.appendChild(list);
  }

  function plural(n, one, few, many){
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
    return many;
  }

  function renderProfile(username, threads){
    appRoot.innerHTML = "";

    var head = UI.h('div', { class: 'profile-head' }, [
      makeAvatar(username, 'lg'),
      UI.h('div', { class: 'profile-info' }, [
        UI.h('div', { class: 'profile-name', text: username }),
        UI.h('div', { class: 'profile-meta', text: threads.length + ' ' + plural(threads.length, 'тема', 'темы', 'тем') })
      ])
    ]);
    appRoot.appendChild(head);

    if (!threads.length) {
      renderEmpty('Тем нет', 'Пользователь ' + username + ' пока ничего не написал.');
      return;
    }
    var list = UI.h('div', { class: 'thread-list' });
    threads.forEach(function(t){ list.appendChild(threadCard(t)); });
    appRoot.appendChild(list);
  }

  function renderThread(data){
    appRoot.innerHTML = "";

    // Back link
    var crumb = UI.h('a', { href: '/app', class: 'crumb', html: ICONS.back + '<span>К ленте</span>' });
    appRoot.appendChild(crumb);

    // Article
    var article = UI.h('article', { class: 'card thread-article' });
    var articleHead = UI.h('div', { class: 'thread-article-head' });

    var authorLink = UI.h('a', {
      href: '/u/' + encodeURIComponent(data.author),
      class: 'thread-article-author'
    }, [
      makeAvatar(data.author),
      UI.h('span', { class: 'thread-article-username', text: data.author })
    ]);
    articleHead.appendChild(authorLink);
    articleHead.appendChild(UI.h('span', { class: 'thread-article-time', text: fmtTime(data.created) }));

    var copyBtn = UI.h('button', {
      type: 'button', class: 'copy-btn', title: 'Копировать ссылку',
      html: ICONS.copy + ICONS.check
    }, [ UI.h('span', { text: 'Ссылка' }) ]);
    var copyTimer = null;
    UI.on(copyBtn, 'click', async function(){
      var ok = await UI.copy(location.origin + '/t/' + data.id);
      copyBtn.classList.toggle('copied', ok);
      if (ok) UI.toast('Ссылка скопирована', { kind: 'ok', duration: 1400 });
      clearTimeout(copyTimer);
      copyTimer = setTimeout(function(){ copyBtn.classList.remove('copied'); }, 1500);
    });
    articleHead.appendChild(copyBtn);
    article.appendChild(articleHead);

    article.appendChild(UI.h('h1', { text: data.title }));
    if (data.content) {
      article.appendChild(UI.h('div', { class: 'thread-article-body', text: data.content }));
    }

    if (data.photo_ids && data.photo_ids.length) {
      var gallery = UI.h('div', { class: 'thread-article-gallery' });
      data.photo_ids.forEach(function(pid, idx){
        var img = UI.h('img', {
          src: '/api/photos/' + encodeURIComponent(pid),
          alt: '',
          loading: 'lazy',
          decoding: 'async',
          on: { click: function(){ openLightbox(data.photo_ids, idx); } }
        });
        gallery.appendChild(img);
      });
      article.appendChild(gallery);
    }
    appRoot.appendChild(article);

    // Comments
    var commentsCard = UI.h('section', { class: 'card comments' });
    var cTitle = UI.h('div', { class: 'comments-title' }, [
      UI.h('span', { text: 'Комментарии' }),
      UI.h('span', { class: 'count', text: String((data.comments || []).length) })
    ]);
    commentsCard.appendChild(cTitle);

    var commentList = UI.h('div', { class: 'comment-list' });
    (data.comments || []).forEach(function(c){
      var item = UI.h('div', { class: 'comment' }, [
        makeAvatar(c.author),
        UI.h('div', { class: 'comment-main' }, [
          UI.h('div', { class: 'comment-head' }, [
            UI.h('a', { href: '/u/' + encodeURIComponent(c.author), class: 'comment-author', text: c.author }),
            UI.h('span', { class: 'comment-time', text: fmtTime(c.created) })
          ]),
          UI.h('div', { class: 'comment-body', text: c.content })
        ])
      ]);
      commentList.appendChild(item);
    });
    commentsCard.appendChild(commentList);

    // Comment form
    var form = UI.h('form', { class: 'form-grid', style: { marginTop: '20px' } });
    var cWrap = UI.h('div', { class: 'input-wrap textarea-wrap' });
    var cInput = UI.h('textarea', {
      class: 'field',
      placeholder: 'Ваш комментарий...',
      maxlength: '5000',
      style: { minHeight: '100px' }
    });
    var cCount = UI.h('span', { class: 'input-count', text: '0/5000' });
    cWrap.appendChild(cInput);
    cWrap.appendChild(cCount);
    form.appendChild(cWrap);

    var msgBox = UI.h('div', { class: 'form-message' });
    form.appendChild(msgBox);

    var submitBtn = UI.h('button', {
      type: 'submit', class: 'btn btn-primary'
    }, [ UI.h('span', { text: 'Отправить' }) ]);
    var actions = UI.h('div', { class: 'form-actions', style: { marginTop: '4px' } }, [submitBtn]);
    form.appendChild(actions);

    function updateCount(){
      var len = cInput.value.length;
      cCount.textContent = len + '/5000';
      cWrap.classList.toggle('warn', len >= 4000 && len < 5000);
      cWrap.classList.toggle('max', len >= 5000);
    }
    UI.on(cInput, 'input', updateCount);
    updateCount();

    UI.on(form, 'submit', async function(e){
      e.preventDefault();
      var text = cInput.value.trim();
      if (!text) { 
        msgBox.innerHTML = ""; msgBox.appendChild(makeMsg('err', 'Введите комментарий')); return;
      }
      submitBtn.disabled = true;
      var lbl = submitBtn.querySelector('span');
      var old = lbl ? lbl.textContent : '';
      if (lbl) lbl.textContent = 'Отправка...';
      msgBox.innerHTML = '';
      try {
        var res = await fetch('/api/threads/' + encodeURIComponent(data.id) + '/comments', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: text })
        });
        if (!res.ok) {
          msgBox.appendChild(makeMsg('err', await readError(res)));
          return;
        }
        cInput.value = '';
        updateCount();
        UI.toast('Комментарий добавлен', { kind: 'ok', duration: 1400 });
        // Перезагружаем тему
        loadThread(data.id);
      } catch(err) {
        msgBox.appendChild(makeMsg('err', 'Ошибка сети: ' + err.message));
      } finally {
        submitBtn.disabled = false;
        if (lbl) lbl.textContent = old;
      }
    });

    commentsCard.appendChild(form);
    appRoot.appendChild(commentsCard);
  }

  function renderNewThread(){
    appRoot.innerHTML = "";

    var crumb = UI.h('a', { href: '/app', class: 'crumb', html: ICONS.back + '<span>К ленте</span>' });
    appRoot.appendChild(crumb);

    var card = UI.h('section', { class: 'card' });
    card.appendChild(UI.h('div', { class: 'section-title', text: 'Новая тема', style: { marginBottom: '16px' } }));

    var form = UI.h('form', { class: 'form-grid' });

    // Title
    var tWrap = UI.h('div', { class: 'input-wrap' });
    var tInput = UI.h('input', {
      class: 'field', type: 'text',
      placeholder: 'Заголовок темы',
      maxlength: '120', autocomplete: 'off', spellcheck: 'false'
    });
    var tCount = UI.h('span', { class: 'input-count', text: '0/120' });
    tWrap.appendChild(tInput);
    tWrap.appendChild(tCount);
    form.appendChild(tWrap);

    // Content
    var cWrap = UI.h('div', { class: 'input-wrap textarea-wrap' });
    var cInput = UI.h('textarea', {
      class: 'field',
      placeholder: 'Текст темы...',
      maxlength: '20000'
    });
    var cCount = UI.h('span', { class: 'input-count', text: '0/20000' });
    cWrap.appendChild(cInput);
    cWrap.appendChild(cCount);
    form.appendChild(cWrap);

    // Photos
    var drop = UI.h('div', { class: 'drop' }, [
      UI.h('span', { class: 'drop-icon', html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="9" cy="9" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg>' }),
      UI.h('span', { class: 'drop-label', text: 'Нажмите или перетащите фото' }),
      UI.h('span', { class: 'drop-hint', text: 'до 5 фото · до 5 МБ · сжатие до ~60 КБ' })
    ]);
    form.appendChild(drop);
    var fileInput = UI.h('input', { type: 'file', accept: 'image/*', multiple: true, hidden: true });
    form.appendChild(fileInput);
    var previews = UI.h('div', { class: 'previews' });
    form.appendChild(previews);

    var msgBox = UI.h('div', { class: 'form-message' });
    form.appendChild(msgBox);

    var submitBtn = UI.h('button', { type: 'submit', class: 'btn btn-primary' }, [
      UI.h('span', { text: 'Опубликовать' })
    ]);
    var cancelBtn = UI.h('button', { type: 'button', class: 'btn btn-ghost' }, [
      UI.h('span', { text: 'Отмена' })
    ]);
    UI.on(cancelBtn, 'click', function(){ location.href = '/app'; });
    var actions = UI.h('div', { class: 'form-actions' }, [cancelBtn, submitBtn]);
    form.appendChild(actions);

    var selectedFiles = [];
    var MAX_PHOTOS = 5;

    function updateTitleCount(){
      var len = tInput.value.length;
      tCount.textContent = len + '/120';
      tWrap.classList.toggle('warn', len >= 96 && len < 120);
      tWrap.classList.toggle('max', len >= 120);
    }
    function updateContentCount(){
      var len = cInput.value.length;
      cCount.textContent = len + '/20000';
      cWrap.classList.toggle('warn', len >= 16000 && len < 20000);
      cWrap.classList.toggle('max', len >= 20000);
    }
    UI.on(tInput, 'input', updateTitleCount);
    UI.on(cInput, 'input', updateContentCount);
    updateTitleCount();
    updateContentCount();

    function renderPreviews(){
      previews.innerHTML = '';
      selectedFiles.forEach(function(file, index){
        var url = URL.createObjectURL(file);
        var img = UI.h('img', { src: url, alt: file.name });
        img.addEventListener('load', function(){ URL.revokeObjectURL(url); }, { once: true });
        var kb = Math.max(1, Math.round(file.size / 1024));
        var rm = UI.h('button', {
          type: 'button', text: '×',
          on: { click: function(){ selectedFiles.splice(index, 1); renderPreviews(); } }
        });
        previews.appendChild(UI.h('div', { class: 'preview' }, [
          img,
          UI.h('div', { class: 'pv-badge', text: kb + ' КБ' }),
          rm
        ]));
      });
      drop.classList.toggle('filled', selectedFiles.length > 0);
      drop.querySelector('.drop-label').textContent = selectedFiles.length
        ? ('Выбрано: ' + selectedFiles.length + ' / ' + MAX_PHOTOS)
        : 'Нажмите или перетащите фото';
    }

    async function addFiles(list){
      var rejected = 0;
      var accepted = [];
      for (var i = 0; i < list.length; i++) {
        var f = list[i];
        if (f.type.indexOf('image/') !== 0) { rejected++; continue; }
        if (f.size > SOURCE_MAX_BYTES) { rejected++; continue; }
        if (selectedFiles.length + accepted.length >= MAX_PHOTOS) { rejected++; continue; }
        accepted.push(f);
      }
      if (rejected) UI.toast('Пропущено файлов: ' + rejected, { kind: 'err' });
      if (!accepted.length) return;

      drop.classList.add('busy');
      drop.querySelector('.drop-label').textContent = 'Сжимаем фото...';

      try {
        var CONCURRENCY = 3;
        var results = new Array(accepted.length);
        var idx = 0;
        async function worker(){
          while (true) {
            var i = idx++;
            if (i >= accepted.length) return;
            results[i] = await compressImage(accepted[i]);
          }
        }
        var workers = [];
        var lim = Math.min(CONCURRENCY, accepted.length);
        for (var w = 0; w < lim; w++) workers.push(worker());
        await Promise.all(workers);
        for (var j = 0; j < results.length; j++) {
          if (results[j]) selectedFiles.push(results[j]);
        }
        renderPreviews();
      } finally {
        drop.classList.remove('busy');
      }
    }

    drop.addEventListener('click', function(){ fileInput.click(); });
    drop.addEventListener('dragover', function(e){ e.preventDefault(); drop.style.borderColor = 'var(--border-3)'; });
    drop.addEventListener('dragleave', function(){ drop.style.borderColor = ''; });
    drop.addEventListener('drop', function(e){
      e.preventDefault();
      drop.style.borderColor = '';
      addFiles(Array.from(e.dataTransfer.files || []));
    });
    fileInput.addEventListener('change', function(){
      addFiles(Array.from(fileInput.files || []));
      fileInput.value = '';
    });
    document.addEventListener('paste', function(e){
      var items = (e.clipboardData || window.clipboardData) && (e.clipboardData || window.clipboardData).items;
      if (!items) return;
      var files = [];
      for (var i = 0; i < items.length; i++) {
        var item = items[i];
        if (item.kind === 'file' && item.type.indexOf('image/') === 0) {
          var f = item.getAsFile();
          if (f) {
            var ext = (f.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
            files.push(new File([f], 'pasted_' + Date.now() + '_' + (files.length + 1) + '.' + ext, { type: f.type }));
          }
        }
      }
      if (files.length) { e.preventDefault(); addFiles(files); }
    });

    UI.on(form, 'submit', async function(e){
      e.preventDefault();
      var title = tInput.value.trim();
      var content = cInput.value.trim();
      if (!title) { msgBox.innerHTML = ''; msgBox.appendChild(makeMsg('err', 'Введите заголовок')); return; }
      if (!content && !selectedFiles.length) {
        msgBox.innerHTML = ''; msgBox.appendChild(makeMsg('err', 'Добавьте текст или хотя бы одно фото')); return;
      }

      var fd = new FormData();
      fd.append('title', title);
      fd.append('content', content);
      selectedFiles.forEach(function(f){ fd.append('files', f, f.name); });

      submitBtn.disabled = true;
      var lbl = submitBtn.querySelector('span');
      var old = lbl ? lbl.textContent : '';
      if (lbl) lbl.textContent = 'Публикация...';
      msgBox.innerHTML = '';

      try {
        var res = await fetch('/api/threads', { method: 'POST', body: fd });
        if (!res.ok) { msgBox.appendChild(makeMsg('err', await readError(res))); return; }
        var data = await res.json();
        UI.toast('Тема создана', { kind: 'ok', duration: 1400 });
        location.href = '/t/' + data.id;
      } catch(err) {
        msgBox.appendChild(makeMsg('err', 'Ошибка сети: ' + err.message));
      } finally {
        submitBtn.disabled = false;
        if (lbl) lbl.textContent = old;
      }
    });

    card.appendChild(form);
    appRoot.appendChild(card);

    setTimeout(function(){ tInput.focus(); }, 80);
  }

  /* ============ ROUTING ============ */
  var currentView = null;

  function parseRoute(){
    var path = location.pathname;
    var m;
    if ((m = path.match(/^\/t\/([a-z0-9]+)\/?$/))) return { view: 'thread', id: m[1] };
    if ((m = path.match(/^\/u\/([^\/]+)\/?$/))) return { view: 'profile', username: decodeURIComponent(m[1]) };
    if (path === '/app/new' || path === '/app/new/') return { view: 'new' };
    if (path === '/app' || path === '/app/') return { view: 'feed' };
    return { view: 'feed' };
  }

  async function loadFeed(author){
    setNavActive('feed');
    renderLoadingSkeleton();
    try {
      var url = '/api/threads';
      if (author) url += '?author=' + encodeURIComponent(author);
      var res = await fetch(url);
      if (!res.ok) { renderError(await readError(res)); return; }
      var data = await res.json();
      if (author) renderProfile(author, data.threads || []);
      else renderFeed(data.threads || []);
    } catch(e) {
      renderError('Ошибка сети: ' + e.message);
    }
  }

  async function loadThread(id){
    setNavActive('thread');
    renderLoadingSkeleton();
    try {
      var res = await fetch('/api/threads/' + encodeURIComponent(id));
      if (!res.ok) {
        var msg = await readError(res);
        appRoot.innerHTML = '';
        var card = UI.h('div', { class: 'card' });
        card.appendChild(makeMsg('err', msg));
        var back = UI.h('a', { href: '/app', class: 'btn btn-ghost', style: { marginTop: '12px' }, text: 'К ленте' });
        card.appendChild(back);
        appRoot.appendChild(card);
        return;
      }
      var data = await res.json();
      // Меняем заголовок документа
      document.title = data.title + ' — СЛД·NET';
      renderThread(data);
    } catch(e) {
      renderError('Ошибка сети: ' + e.message);
    }
  }

  function renderNewThreadView(){
    setNavActive('new');
    document.title = 'Новая тема — СЛД·NET';
    renderNewThread();
  }

  function route(){
    var r = parseRoute();
    currentView = r.view;
    if (r.view === 'thread') loadThread(r.id);
    else if (r.view === 'profile') loadFeed(r.username);
    else if (r.view === 'new') renderNewThreadView();
    else { document.title = 'Лента — СЛД·NET'; loadFeed(); }
    // Обновить pill навигации
    setNavActive(r.view === 'profile' ? 'feed' : r.view);
  }

  // Перехватываем клики по внутренним ссылкам
  document.addEventListener('click', function(e){
    var a = e.target.closest && e.target.closest('a');
    if (!a) return;
    var href = a.getAttribute('href');
    if (!href) return;
    if (href.charAt(0) !== '/') return;
    if (a.target === '_blank' || e.ctrlKey || e.metaKey || e.shiftKey) return;
    // Открываем внутренние ссылки через history
    if (href === '/app' || href === '/app/new' || href.indexOf('/t/') === 0 || href.indexOf('/u/') === 0) {
      e.preventDefault();
      history.pushState(null, '', href);
      route();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  });
  window.addEventListener('popstate', route);

  /* ============ INIT ============ */
  requireMe().then(function(){
    route();
  }).catch(function(){ /* redirect to /auth already */ });
})();
</script>
</body>
</html>
"""

# ============================================================
# ОБЩИЙ КАРКАС
# ============================================================


def render_shell(title: str, body: str, extra_css: str = "", og: str = "", extra_js: str = "") -> str:
    return (
        '<!DOCTYPE html>\n<html lang="ru">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'<title>{title}</title>\n{og}'
        f'<link rel="icon" href="{FAVICON}">\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;600;700;800&family=Manrope:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">\n'
        '<style>\n' + SHELL_CSS + '\n/* PAGE CSS */\n' + extra_css + '\n</style>\n'
        '</head>\n<body>\n'
        '<div class="page-loader" id="pageLoader" aria-hidden="true">\n'
        '  <div class="loader-inner">\n'
        '    <div class="loader-mark">' + LOGO_SVG + '</div>\n'
        '    <div class="loader-bar"></div>\n'
        '  </div>\n'
        '</div>\n'
        '<div class="cur-dot"></div>\n<div class="cur-ring"></div>\n'
        '<div class="bg"><div class="halo halo-1"></div><div class="halo halo-2"></div><div class="halo halo-3"></div></div>\n'
        '<div class="cursor-glow"></div>\n<div class="grid-bg"></div>\n<div class="grain"></div>\n'
        '<nav id="nav" class="top-nav">\n'
        '  <div class="top-nav-inner">\n'
        '    <a href="/" class="logo">\n'
        '      <span class="logo-mark">' + LOGO_SVG + '</span>\n'
        '      <span class="logo-word">СЛД<span class="ldot">·</span><span class="lnet">NET</span></span>\n'
        '    </a>\n'
        '    <div class="nav-links"></div>\n'
        '    <div class="nav-right">\n'
        '      <a href="/auth?tab=login" class="btn btn-ghost">\n'
        '        <span>Войти</span>\n'
        '      </a>\n'
        '      <a href="/auth?tab=register" class="btn btn-primary">\n'
        '        <span>Регистрация</span>' + ARROW_SVG + '\n'
        '      </a>\n'
        '    </div>\n'
        '  </div>\n'
        '</nav>\n'
        + body +
        '<button class="scroll-top" id="scrollTopBtn" type="button" aria-label="Наверх">\n'
        '  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg>\n'
        '</button>\n'
        '<script>\n' + UI_FRAMEWORK_JS + '\n' + SHELL_JS + '\n' + (extra_js or '') + '\n</script>\n'
        '</body>\n</html>'
    )


def _build_og_tags(title: str, content: str, base: str, url: str, image: Optional[str] = None) -> str:
    t_esc = _html.escape(title or "СЛД·NET", quote=True)
    d_raw = (content or "").replace("\n", " ").strip()
    if len(d_raw) > 180:
        d_raw = d_raw[:177].rstrip() + "…"
    d_esc = _html.escape(d_raw, quote=True)
    lines = [
        '<meta property="og:type" content="article">',
        '<meta property="og:site_name" content="СЛД·NET">',
        f'<meta property="og:title" content="{t_esc}">',
        f'<meta property="og:description" content="{d_esc}">',
        f'<meta property="og:url" content="{_html.escape(url, quote=True)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{t_esc}">',
        f'<meta name="twitter:description" content="{d_esc}">',
    ]
    if image:
        img_esc = _html.escape(image, quote=True)
        lines.append(f'<meta property="og:image" content="{img_esc}">')
        lines.append(f'<meta name="twitter:image" content="{img_esc}">')
    lines.append(f'<link rel="canonical" href="{_html.escape(url, quote=True)}">')
    return "\n".join(lines)

# ============================================================
# СТРАНИЧНЫЕ МАРШРУТЫ
# ============================================================


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(build_landing())


@app.get("/auth", response_class=HTMLResponse)
async def auth_page():
    return HTMLResponse(build_auth())


def _render_app(og_tags: str = "", title_override: Optional[str] = None) -> str:
    html = (APP
            .replace("__FAVICON__", FAVICON)
            .replace("__LOGO_SVG__", LOGO_SVG)
            .replace("__APP_CSS__", APP_CSS)
            .replace("__UI_FRAMEWORK__", UI_FRAMEWORK_JS)
            .replace("<!-- OG_TAGS -->", og_tags))
    if title_override:
        html = html.replace(
            "<title>СЛД·NET</title>",
            f"<title>{_html.escape(title_override, quote=True)}</title>",
        )
    return html


@app.get("/app", response_class=HTMLResponse)
async def app_page():
    return HTMLResponse(_render_app())


@app.get("/app/new", response_class=HTMLResponse)
async def app_new_page():
    return HTMLResponse(_render_app())


@app.get("/u/{username}", response_class=HTMLResponse)
async def user_page(username: str):
    return HTMLResponse(_render_app())


@app.get("/t/{thread_id}", response_class=HTMLResponse)
async def thread_page(thread_id: str, request: Request):
    with _lock:
        t = _threads.get(thread_id)
    og = ""
    title_override = None
    if t:
        try:
            content = _dec_text(t["content_enc"])
        except Exception:
            content = ""
        base = _base_url(request)
        url = f"{base}/t/{thread_id}"
        image = f"{base}/api/photos/{t['photos'][0]}" if t["photos"] else None
        og = _build_og_tags(t["title"], content, base, url, image)
        title_override = t["title"] + " — СЛД·NET"
    return HTMLResponse(_render_app(og_tags=og, title_override=title_override))


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
