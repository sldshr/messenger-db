"""
Sld-Networking — посты с 6-значным кодом (без повторяющихся цифр).
Хранение: оперативная память, AES-256-GCM + zstd/gzip.

Маршруты:
  /           — лендинг
  /app        — рабочая область (SPA)
  /p/{code}   — единая ссылка на пост (с OpenGraph-метатегами)
  /api/*      — REST API
  /manifest.json  — PWA-манифест
  /sw.js          — service worker
  /icon.svg       — иконка приложения

Запуск: pip install fastapi uvicorn python-multipart cryptography zstandard && python main.py
"""

from __future__ import annotations

import gzip
import html as _html
import json
import logging
import os
import secrets
import string
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

import uvicorn
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sld")

try:
    import zstandard as _zstd_mod
    _HAS_ZSTD = True
except ImportError:
    _HAS_ZSTD = False

_ZSTD_C = _zstd_mod.ZstdCompressor(level=3) if _HAS_ZSTD else None
_ZSTD_D = _zstd_mod.ZstdDecompressor() if _HAS_ZSTD else None


def _compress(b: bytes) -> bytes:
    if _HAS_ZSTD:
        return b"Z" + _ZSTD_C.compress(b)
    return b"G" + gzip.compress(b, compresslevel=6)


def _decompress(b: bytes) -> bytes:
    marker, payload = b[:1], b[1:]
    if marker == b"Z" and _HAS_ZSTD:
        return _ZSTD_D.decompress(payload)
    if marker == b"G":
        return gzip.decompress(payload)
    raise ValueError("unknown codec")


_AES = AESGCM(AESGCM.generate_key(bit_length=256))
_NONCE = 12


def _encrypt(data: bytes) -> bytes:
    nonce = os.urandom(_NONCE)
    return nonce + _AES.encrypt(nonce, data, None)


def _decrypt(blob: bytes) -> bytes:
    if len(blob) < _NONCE + 16:
        raise ValueError("too short")
    return _AES.decrypt(blob[:_NONCE], blob[_NONCE:], None)


def _pack_meta(meta: dict) -> bytes:
    raw = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _encrypt(_compress(raw))


def _unpack_meta(blob: bytes) -> dict:
    return json.loads(_decompress(_decrypt(blob)).decode("utf-8"))


_store: Dict[str, dict] = {}
_lock = threading.Lock()

MAX_PHOTOS = 5
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_TITLE_LEN = 120
MAX_CONTENT_LEN = 20_000

PUBLIC_BASE_URL = os.environ.get("SLD_BASE_URL", "").rstrip("/")


def _gen_code() -> str:
    digits = list(string.digits)
    chars = []
    for _ in range(6):
        idx = secrets.randbelow(len(digits))
        chars.append(digits.pop(idx))
    return "".join(chars)


def _new_code() -> str:
    with _lock:
        for _ in range(5000):
            code = _gen_code()
            if code not in _store:
                return code
    raise HTTPException(503, "Хранилище переполнено")


def _base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


app = FastAPI(title="Sld-Networking", docs_url=None, redoc_url=None)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Внутренняя ошибка: {type(exc).__name__}"},
    )


@app.post("/api/posts")
async def create_post(
    request: Request,
    title: str = Form(...),
    content: str = Form(""),
    og_enabled: str = Form("true"),
    files: Optional[List[UploadFile]] = File(None),
):
    title = (title or "").strip()
    content = (content or "").strip()
    og_flag = og_enabled.strip().lower() in ("1", "true", "on", "yes", "да")

    if not title:
        raise HTTPException(400, "Требуется название поста")
    if len(title) > MAX_TITLE_LEN:
        raise HTTPException(400, f"Название длиннее {MAX_TITLE_LEN} символов")
    if len(content) > MAX_CONTENT_LEN:
        raise HTTPException(400, f"Содержимое длиннее {MAX_CONTENT_LEN} символов")

    files = [f for f in (files or []) if f and f.filename]
    if len(files) > MAX_PHOTOS:
        raise HTTPException(400, f"Максимум {MAX_PHOTOS} фото")

    photos = []
    total = 0
    for f in files:
        data = await f.read()
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(
                400,
                f"Файл «{f.filename}» больше {MAX_PHOTO_BYTES // (1024 * 1024)} МБ",
            )
        enc = _encrypt(data)
        total += len(enc)
        photos.append({
            "name": f.filename,
            "mime": f.content_type or "image/jpeg",
            "size": len(data),
            "enc": enc,
        })

    created = datetime.now(timezone.utc).isoformat()
    meta = {
        "title": title,
        "content": content,
        "created": created,
        "og_enabled": og_flag,
        "photos": [{"name": p["name"], "mime": p["mime"], "size": p["size"]} for p in photos],
    }
    enc_meta = _pack_meta(meta)
    total += len(enc_meta)

    code = _new_code()
    with _lock:
        _store[code] = {
            "meta": enc_meta,
            "photos": photos,
            "created": created,
            "size": total,
            "og_enabled": og_flag,
        }

    base = _base_url(request)
    return {
        "code": code,
        "compressed_bytes": total,
        "photos": len(photos),
        "share_url": f"{base}/p/{code}",
    }


@app.get("/api/posts/{code}")
async def get_post(code: str):
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(400, "Код должен содержать 6 цифр")

    with _lock:
        entry = _store.get(code)
    if entry is None:
        raise HTTPException(404, "Пост не найден")

    try:
        meta = _unpack_meta(entry["meta"])
    except (InvalidTag, ValueError, OSError) as e:
        raise HTTPException(500, f"Ошибка расшифровки: {e}")

    meta["code"] = code
    meta["photos"] = [
        {"idx": i, "name": p["name"], "mime": p["mime"], "size": p["size"]}
        for i, p in enumerate(entry["photos"])
    ]
    return meta


@app.get("/api/photos/{code}/{idx}")
async def get_photo(code: str, idx: int):
    with _lock:
        entry = _store.get(code)
    if entry is None:
        raise HTTPException(404, "Пост не найден")

    photos = entry["photos"]
    if idx < 0 or idx >= len(photos):
        raise HTTPException(404, "Фото не найдено")

    p = photos[idx]
    try:
        data = _decrypt(p["enc"])
    except (InvalidTag, ValueError) as e:
        raise HTTPException(500, f"Ошибка расшифровки: {e}")

    return Response(
        content=data,
        media_type=p["mime"],
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Length": str(len(data)),
        },
    )


@app.get("/api/random-code")
async def random_code():
    with _lock:
        for _ in range(5000):
            code = _gen_code()
            if code not in _store:
                return {"code": code}
    raise HTTPException(503, "Хранилище переполнено")


@app.get("/api/stats")
async def stats():
    with _lock:
        return {"posts": len(_store)}


# ============================================================
# PWA — манифест, service worker, иконка
# ============================================================

PWA_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    '<defs>'
    '<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#ffffff"/>'
    '<stop offset="1" stop-color="#c4c4c8"/>'
    '</linearGradient>'
    '</defs>'
    '<rect width="512" height="512" rx="112" fill="#080808"/>'
    '<rect x="96" y="96" width="320" height="320" rx="72" fill="url(#g)"/>'
    '<g fill="none" stroke="#08080a" stroke-width="26" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="256" cy="186" r="34"/>'
    '<circle cx="158" cy="336" r="34"/>'
    '<circle cx="354" cy="336" r="34"/>'
    '<path d="M232 208 178 314M280 208 334 314M192 336h128"/>'
    '</g>'
    '</svg>'
)

PWA_MANIFEST = {
    "name": "СЛД·NET — нетворкинг",
    "short_name": "СЛД·NET",
    "description": "Посты по 6-значному коду. Без аккаунтов, с шифрованием.",
    "start_url": "/app",
    "scope": "/",
    "display": "standalone",
    "display_override": ["standalone", "minimal-ui"],
    "orientation": "any",
    "background_color": "#080808",
    "theme_color": "#080808",
    "lang": "ru",
    "dir": "ltr",
    "categories": ["social", "productivity", "utilities"],
    "icons": [
        {
            "src": "/icon.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any",
        },
        {
            "src": "/icon.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "maskable",
        },
    ],
}

PWA_SW = r"""
// СЛД·NET service worker — кеш оболочки + офлайн-доступ к /app
const CACHE = 'sld-net-v2';
const SHELL_URLS = [
  '/app',
  '/icon.svg',
];

// Установка — кешируем оболочку
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL_URLS).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

// Активация — удаляем старые кеши
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// Стратегии
self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Только GET
  if (req.method !== 'GET') return;

  // API — всегда сеть (никогда не кешируем посты)
  if (url.pathname.startsWith('/api/')) return;

  // Навигация — сеть в приоритете, кеш как fallback
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((m) => m || caches.match('/app')))
    );
    return;
  }

  // Шрифты Google — cache-first
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return res;
        }).catch(() => cached);
      })
    );
    return;
  }

  // Иконка, манифест — cache-first
  if (url.pathname === '/icon.svg' || url.pathname === '/manifest.json') {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }))
    );
    return;
  }

  // Остальное — сеть, но кеш как fallback
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req))
  );
});
"""


@app.get("/manifest.json")
async def manifest():
    return JSONResponse(
        content=PWA_MANIFEST,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/sw.js")
async def service_worker():
    return PlainTextResponse(
        content=PWA_SW,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/icon.svg")
async def icon():
    return Response(
        content=PWA_ICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ============================================================
# ФРОНТЕНД
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

YOUTUBE_SVG = (
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
    '<path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545'
    's-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814'
    'a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505'
    'a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814z'
    'M9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>'
)

MASTODON_SVG = (
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
    '<path d="M23.268 5.313c-.35-2.578-2.617-4.61-5.304-5.004C17.51.242 15.792 0 11.813 0'
    'h-.03c-3.98 0-4.835.242-5.288.309C3.882.692 1.496 2.518.917 5.127'
    '.64 6.412.61 7.837.661 9.143c.074 1.874.088 3.745.26 5.611'
    '.118 1.24.325 2.47.62 3.68.55 2.237 2.777 4.098 4.96 4.857'
    '2.336.792 4.849.923 7.256.38.265-.061.527-.132.786-.213'
    '.585-.184 1.27-.39 1.774-.753a.057.057 0 0 0 .023-.043v-1.809'
    'a.052.052 0 0 0-.02-.041.053.053 0 0 0-.046-.01 20.282 20.282 0 0 1-4.709.545'
    'c-2.73 0-3.463-1.284-3.674-1.818a5.593 5.593 0 0 1-.319-1.433'
    '.053.053 0 0 1 .066-.054c1.517.363 3.072.546 4.632.546'
    '.376 0 .75 0 1.125-.01 1.57-.044 3.224-.124 4.768-.422'
    '.038-.008.077-.015.11-.024 2.435-.464 4.753-1.92 4.989-5.604'
    '.008-.145.03-1.52.03-1.67.002-.512.167-3.63-.024-5.545z'
    'm-3.748 9.195h-2.561V8.29c0-1.309-.55-1.976-1.67-1.976'
    '-1.23 0-1.846.79-1.846 2.35v3.403h-2.546V8.663c0-1.56-.617-2.35-1.848-2.35'
    '-1.112 0-1.668.668-1.67 1.977v6.218H4.822V8.102'
    'c0-1.31.337-2.35 1.011-3.12.696-.77 1.608-1.164 2.74-1.164'
    '1.311 0 2.302.5 2.962 1.498l.638 1.06.638-1.06c.66-.999 1.65-1.498 2.96-1.498'
    '1.13 0 2.043.395 2.74 1.164.675.77 1.012 1.81 1.012 3.12z"/></svg>'
)

# ============================================================
# ОБЩИЙ CSS
# ============================================================
SHELL_CSS = r"""
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080808;
  --border:rgba(255,255,255,0.07);
  --border-2:rgba(255,255,255,0.12);
  --border-3:rgba(255,255,255,0.22);
  --text:#f4f4f5;
  --text-dim:#9a9a9e;
  --text-mute:#6a6a6e;
  --glass:rgba(255,255,255,0.035);
  --glass-hi:rgba(255,255,255,0.07);
  --radius:22px;
  --ease:cubic-bezier(.2,.8,.2,1);
}
html{scroll-behavior:smooth}
html,body{
  background:var(--bg);color:var(--text);
  font-family:'Manrope',system-ui,-apple-system,sans-serif;
  font-size:16px;line-height:1.6;
  overflow-x:clip;min-height:100vh;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  -webkit-user-select:none;-moz-user-select:none;-ms-user-select:none;user-select:none;
  -webkit-tap-highlight-color:transparent;
}
input,textarea,[contenteditable],pre,code,.post-body,.post-title,.modal-code,.otp-cell,.share-link-url{
  -webkit-user-select:text;-moz-user-select:text;-ms-user-select:text;user-select:text;
}
[hidden]{display:none !important}
::selection{background:#fff;color:#000}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:#0a0a0a}
::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:8px;border:2px solid #0a0a0a}
::-webkit-scrollbar-thumb:hover{background:#3a3a3a}

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
.cur-dot{
  width:7px;height:7px;background:#fff;
  box-shadow:0 0 0 1px rgba(255,255,255,0.5),
             0 0 14px rgba(255,255,255,0.35);
}
.cur-ring{
  width:34px;height:34px;
  border:1.5px solid rgba(255,255,255,0.4);
  transition:width .22s var(--ease),height .22s var(--ease),
             border-color .22s,background .22s,opacity .2s;
}
.cur-ring.hover{
  width:60px;height:60px;
  border-color:rgba(255,255,255,0.22);
  background:rgba(255,255,255,0.05);
  backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);
}
.cur-ring.click{width:24px;height:24px;background:rgba(255,255,255,0.12)}
@media (max-width:900px),(hover:none){.cur-dot,.cur-ring{display:none}}

.page-loader{
  position:fixed;inset:0;z-index:99998;
  background:var(--bg);
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
  50%{transform:scale(1.06);box-shadow:0 12px 34px -8px rgba(255,255,255,0.45),0 0 0 14px rgba(255,255,255,0)}
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

.wrap{max-width:1220px;margin:0 auto;padding:0 32px}

body > nav.top-nav{
  position:fixed;top:20px;left:50%;transform:translateX(-50%);
  z-index:100;
  width:calc(100% - 40px);max-width:1180px;
  border-radius:20px;padding:12px 14px 12px 22px;
  display:flex;align-items:center;justify-content:space-between;gap:20px;
  background:rgba(15,15,15,0.6);
  backdrop-filter:blur(24px) saturate(160%);
  -webkit-backdrop-filter:blur(24px) saturate(160%);
  border:1px solid var(--border);
  box-shadow:0 12px 40px rgba(0,0,0,0.5),inset 0 1px 0 rgba(255,255,255,0.06);
  transition:background .3s,box-shadow .3s,border-color .3s;
}
body > nav.top-nav.scrolled{
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
  display:grid;place-items:center;
  position:relative;overflow:hidden;
  box-shadow:0 4px 16px rgba(0,0,0,0.5),inset 0 -1px 0 rgba(0,0,0,0.15);
  transition:transform .35s var(--ease);flex-shrink:0;
}
.logo:hover .logo-mark{transform:rotate(-6deg) scale(1.06)}
.logo-mark::after{
  content:'';position:absolute;inset:0;
  background:linear-gradient(150deg,rgba(255,255,255,0.9),transparent 55%);pointer-events:none;
}
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
.nav-links a.active{color:#fff;background:rgba(255,255,255,0.06)}

.btn{
  display:inline-flex;align-items:center;justify-content:center;
  gap:8px;
  font-family:'Manrope',sans-serif;font-weight:600;font-size:13.5px;
  letter-spacing:-0.005em;
  padding:11px 20px;height:44px;
  border-radius:12px;border:1px solid transparent;
  text-decoration:none;position:relative;overflow:hidden;white-space:nowrap;
  transition:background .28s, border-color .28s, color .28s, box-shadow .28s;
  -webkit-tap-highlight-color:transparent;user-select:none;
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

footer{padding:80px 0 50px;border-top:1px solid var(--border);margin-top:100px}
.foot-top{
  display:flex;justify-content:space-between;
  align-items:flex-start;gap:40px;flex-wrap:wrap;margin-bottom:54px;
}
.foot-brand{max-width:360px}
.foot-brand .logo{margin-bottom:18px}
.foot-brand p{color:var(--text-mute);font-size:13.5px;line-height:1.7}
.foot-brand .dev{
  display:inline-flex;align-items:center;gap:8px;
  margin-top:14px;padding:7px 12px 7px 10px;
  border-radius:100px;
  background:rgba(255,255,255,0.04);
  border:1px solid var(--border);
  font-family:'JetBrains Mono',monospace;
  font-size:11px;letter-spacing:0.06em;
  color:var(--text-dim);
  text-transform:uppercase;
}
.foot-brand .dev svg{width:12px;height:12px;color:var(--text-mute);display:block}
.foot-brand .dev strong{color:#fff;font-weight:600}

.foot-cols{display:flex;gap:80px;flex-wrap:wrap}
.foot-col h4{
  font-family:'JetBrains Mono',monospace;font-size:11px;
  letter-spacing:0.18em;text-transform:uppercase;
  color:var(--text-mute);margin-bottom:20px;font-weight:500;
}
.foot-col a{
  display:block;color:var(--text-dim);text-decoration:none;
  font-size:14.5px;padding:6px 0;
  transition:color .25s,padding-left .25s;width:fit-content;
  word-break:break-word;
}
.foot-col a:hover{color:#fff;padding-left:4px}

.foot-bottom{
  display:flex;justify-content:space-between;align-items:center;
  gap:24px;flex-wrap:wrap;
  padding-top:30px;border-top:1px solid var(--border);
  color:var(--text-mute);font-size:12.5px;
  font-family:'JetBrains Mono',monospace;letter-spacing:0.05em;
}
.foot-bottom .group{color:var(--text-dim)}
.foot-bottom .group strong{color:#fff;font-weight:600}

.foot-socials{display:flex;gap:8px;align-items:center}
.foot-socials a{
  width:38px;height:38px;border-radius:11px;
  display:grid;place-items:center;
  background:var(--glass);border:1px solid var(--border-2);
  color:var(--text-dim);
  transition:background .3s, border-color .3s, color .3s;
  text-decoration:none;
}
.foot-socials a:hover{
  background:var(--glass-hi);border-color:var(--border-3);
  color:#fff;
}
.foot-socials svg{width:18px;height:18px;display:block}

.reveal{
  opacity:0;
  transition:
    opacity .8s var(--ease),
    transform .8s var(--ease),
    filter .8s var(--ease);
  will-change: opacity, transform, filter;
}
.reveal.in{opacity:1;transform:none;filter:none}

.reveal--up{transform:translateY(56px)}
.reveal--down{transform:translateY(-48px)}
.reveal--left{transform:translateX(-64px)}
.reveal--right{transform:translateX(64px)}
.reveal--zoom{transform:scale(.9)}
.reveal--blur{filter:blur(14px)}
.reveal--rotate{transform:rotate(-3deg) translateY(30px)}
.reveal--flip{transform:perspective(1000px) rotateX(-28deg);transform-origin:center top}
.reveal--elastic{transform:scale(.85);transition-timing-function:cubic-bezier(.34,1.56,.64,1)}
.reveal--skew{transform:skewY(-2.5deg) translateY(40px)}

.eyebrow{
  display:inline-flex;align-items:center;gap:10px;
  font-family:'JetBrains Mono',monospace;
  font-size:11.5px;font-weight:500;
  letter-spacing:0.16em;text-transform:uppercase;
  color:var(--text-mute);margin-bottom:22px;
}
.eyebrow::before{content:'';width:26px;height:1px;background:linear-gradient(90deg,var(--text-mute),transparent)}

@media (max-width:1000px){.nav-links{display:none}}
@media (max-width:720px){
  .wrap{padding:0 20px}
  body > nav.top-nav{padding:10px 10px 10px 16px;top:12px;width:calc(100% - 24px);border-radius:16px}
  .logo{font-size:13.5px}
  .logo-mark{width:28px;height:28px}
  .logo-mark svg{width:14px;height:14px}
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
      if (!cursorReady) {
        cursorReady = true;
        document.body.classList.add("cursor-ready");
      }
      mx = e.clientX; my = e.clientY;
      document.documentElement.style.setProperty("--mx", mx + "px");
      document.documentElement.style.setProperty("--my", my + "px");
    }, { passive: true });

    // Физика: чем быстрее движение — тем сильнее расползается кольцо
    (function loop(){
      dot.style.transform = "translate3d(" + mx + "px," + my + "px,0) translate(-50%,-50%)";
      rx += (mx - rx) * 0.26;
      ry += (my - ry) * 0.26;

      var dx = mx - lx, dy = my - ly;
      // Ограничение скорости — 60 (было 40)
      vel = Math.min(Math.hypot(dx, dy), 60);
      lx = mx; ly = my;

      var angle = Math.atan2(dy, dx) * 180 / Math.PI;
      // Сильнее выраженная деформация: до 1.4 при максимальной скорости
      var stretch = 1 + vel / 150;
      var squash  = 1 - vel / 220;
      // Чем быстрее — тем прозрачнее рамка
      var borderOp = 0.4 - (vel / 60) * 0.25;

      ring.style.transform = "translate3d(" + rx + "px," + ry + "px,0) translate(-50%,-50%) rotate(" + angle + "deg) scale(" + stretch + "," + squash + ")";

      if (!ring.classList.contains("hover")) {
        ring.style.borderColor = "rgba(255,255,255," + Math.max(0.15, borderOp).toFixed(2) + ")";
      }
      requestAnimationFrame(loop);
    })();

    addEventListener("mousedown", function(){ ring.classList.add("click"); });
    addEventListener("mouseup", function(){ ring.classList.remove("click"); });

    document.querySelectorAll('a, button, .glass, .code-cell, .stat, .mock, label.checkbox-wrap, input, textarea, .tab, .drop, .share-link-copy').forEach(function(el){
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
    addEventListener("scroll", function(){
      if (!ticking) {
        requestAnimationFrame(function(){
          nav.classList.toggle("scrolled", scrollY > 40);
          ticking = false;
        });
        ticking = true;
      }
    }, { passive: true });
  }

  var io = new IntersectionObserver(function(es){
    es.forEach(function(e){
      if (e.isIntersecting) {
        var el = e.target;
        var delay = parseInt(el.getAttribute("data-delay") || "0", 10);
        setTimeout(function(){ el.classList.add("in"); }, delay);
        io.unobserve(el);
      }
    });
  }, { threshold: 0.12, rootMargin: "0px 0px -50px 0px" });
  document.querySelectorAll(".reveal").forEach(function(el){ io.observe(el); });

  document.querySelectorAll('a[href^="#"]').forEach(function(a){
    a.addEventListener("click", function(e){
      var h = a.getAttribute("href");
      if (h === "#" || h.length < 2) return;
      var tg = document.querySelector(h);
      if (!tg) return;
      e.preventDefault();
      scrollTo({ top: tg.getBoundingClientRect().top + scrollY - 90, behavior: "smooth" });
    });
  });

  var codeEls = document.querySelectorAll("[data-code]");
  if (codeEls.length) {
    fetch("/api/random-code", { cache: "no-store" })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (!d || !/^\d{6}$/.test(d.code)) return;
        codeEls.forEach(function(el){ el.textContent = d.code; });
        var cells = document.querySelectorAll(".code-cell");
        cells.forEach(function(c, i){
          c.textContent = d.code[i] || "0";
          c.classList.remove("flip"); void c.offsetWidth; c.classList.add("flip");
          c.style.animationDelay = (i * 0.06) + "s";
        });
        setTimeout(function(){
          cells.forEach(function(c){ c.style.animationDelay = ""; c.classList.remove("flip"); });
        }, 1200);
      }).catch(function(){});
  }
})();
"""

FOOTER_HTML = (
    '\n<footer>\n  <div class="wrap">\n    <div class="foot-top">\n'
    '      <div class="foot-brand">\n'
    '        <a href="/" class="logo">\n'
    '          <span class="logo-mark">' + LOGO_SVG + '</span>\n'
    '          <span class="logo-word">СЛД<span class="ldot">·</span><span class="lnet">NET</span></span>\n'
    '        </a>\n'
    '        <p>Нетворкинг на шести цифрах. Без аккаунтов, без лишнего — публикуйте посты, находите людей, делитесь ссылкой.</p>\n'
    '        <div class="dev">\n'
    '          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/></svg>\n'
    '          <span>Разработчик · <strong>слдшр</strong></span>\n'
    '        </div>\n'
    '      </div>\n'
    '      <div class="foot-cols">\n'
    '        <div class="foot-col">\n'
    '          <h4>Продукт</h4>\n'
    '          <a href="/#features">Возможности</a>\n'
    '          <a href="/#how">Как это работает</a>\n'
    '          <a href="/#specs">Технологии</a>\n'
    '        </div>\n'
    '        <div class="foot-col">\n'
    '          <h4>Приложение</h4>\n'
    '          <a href="/app?mode=create">Создать пост</a>\n'
    '          <a href="/app?mode=find">Найти пост</a>\n'
    '        </div>\n'
    '        <div class="foot-col">\n'
    '          <h4>Контакты</h4>\n'
    '          <a href="mailto:sldshr.confirmation@gmail.com">sldshr.confirmation@gmail.com</a>\n'
    '          <a rel="me" href="https://mastodon.social/@ru_sldshr" target="_blank">Mastodon</a>\n'
    '        </div>\n'
    '      </div>\n'
    '    </div>\n'
    '    <div class="foot-bottom">\n'
    '      <span>© 2026 СЛД·NET. Все права защищены.</span>\n'
    '      <div class="foot-socials">\n'
    '        <a href="https://www.youtube.com/@слдшр" target="_blank" rel="noopener" aria-label="YouTube">' + YOUTUBE_SVG + '</a>\n'
    '        <a rel="me" href="https://mastodon.social/@ru_sldshr" target="_blank" aria-label="Mastodon">' + MASTODON_SVG + '</a>\n'
    '      </div>\n'
    '      <span class="group">Сделано · <strong>слдшр</strong></span>\n'
    '    </div>\n'
    '  </div>\n</footer>\n'
)


def render_shell(title: str, body: str, extra_css: str = "", og: str = "", active: str = "") -> str:
    nav_links = [
        ("/", "Главная"),
        ("/#features", "Возможности"),
        ("/#how", "Как это работает"),
    ]
    links_html = "".join(
        f'<a href="{href}"{" class=\"active\"" if active == href else ""}>{label}</a>'
        for href, label in nav_links
    )

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
        '  <a href="/" class="logo">\n'
        '    <span class="logo-mark">' + LOGO_SVG + '</span>\n'
        '    <span class="logo-word">СЛД<span class="ldot">·</span><span class="lnet">NET</span></span>\n'
        '  </a>\n'
        '  <div class="nav-links">' + links_html + '</div>\n'
        '  <a href="/app?mode=create" class="btn btn-primary">\n'
        '    <span>Открыть приложение</span>' + ARROW_SVG + '\n'
        '  </a>\n'
        '</nav>\n'
        + body +
        FOOTER_HTML +
        '<script>' + SHELL_JS + '</script>\n'
        '</body>\n</html>'
    )


# ============================================================
# ЛЕНДИНГ
# ============================================================

LANDING_CSS = r"""
.hero{padding:170px 0 0;position:relative;overflow:hidden}
.hero-inner{max-width:900px;margin:0 auto;text-align:center;position:relative;z-index:1}

.badge{
  display:inline-flex;align-items:center;gap:10px;
  padding:8px 16px 8px 9px;
  border-radius:100px;
  background:rgba(255,255,255,0.055);
  border:1px solid rgba(255,255,255,0.2);
  backdrop-filter:blur(14px) saturate(160%);
  -webkit-backdrop-filter:blur(14px) saturate(160%);
  font-size:12.5px;font-weight:600;color:#fff;
  letter-spacing:-0.005em;margin-bottom:38px;
  box-shadow:0 10px 30px -14px rgba(0,0,0,0.7),inset 0 1px 0 rgba(255,255,255,0.1);
  opacity:0;transform:translateY(-14px);
  animation:badgeIn .75s var(--ease) .05s both;
  position:relative;z-index:2;max-width:100%;
}
.badge-dot{
  width:20px;height:20px;border-radius:50%;
  background:linear-gradient(140deg,#fff,#c8c8cc);
  display:grid;place-items:center;color:#0a0a0a;
  position:relative;flex-shrink:0;
  box-shadow:0 2px 8px rgba(0,0,0,0.5);
}
.badge-dot::after{
  content:'';position:absolute;inset:-3px;border-radius:50%;
  border:1px solid rgba(255,255,255,0.28);
  animation:ping 2.6s ease-out infinite;
}
@keyframes ping{0%{transform:scale(1);opacity:1}100%{transform:scale(1.55);opacity:0}}
.badge-dot svg{width:10px;height:10px;display:block}
.badge-sep{color:rgba(255,255,255,0.3);font-weight:400;margin:0 -1px}
@keyframes badgeIn{from{opacity:0;transform:translateY(-14px);filter:blur(5px)}to{opacity:1;transform:translateY(0);filter:blur(0)}}

/* Заголовок — анимация по словам, компактнее по времени */
.hero-title{
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:clamp(38px,7.2vw,84px);line-height:0.98;
  letter-spacing:-0.045em;margin-bottom:30px;
  display:flex;flex-direction:column;gap:4px;
  text-wrap:balance;position:relative;z-index:1;
  perspective:900px;
}
.h1-row{display:block;white-space:nowrap;text-align:center}
.h1-row.dim{color:var(--text-mute);font-weight:400}

.hero-title .word{
  display:inline-block;
  opacity:0;
  transform:translateY(50%) rotateX(-60deg) scale(.95);
  filter:blur(7px);
  transform-origin:bottom center;
  animation:wordIn .8s var(--ease) both;
  animation-delay:calc(var(--i,0) * 80ms + 140ms);
  will-change:transform,opacity,filter;
}
.hero-title .word + .word{margin-left:0.22em}
@keyframes wordIn{
  0%{opacity:0;transform:translateY(50%) rotateX(-60deg) scale(.95);filter:blur(7px)}
  55%{filter:blur(1.5px)}
  100%{opacity:1;transform:translateY(0) rotateX(0) scale(1);filter:blur(0)}
}

.lead{
  font-size:clamp(15px,1.7vw,17.5px);line-height:1.65;
  color:var(--text-dim);max-width:620px;margin:0 auto 40px;
  opacity:0;transform:translateY(18px);
  animation:leadIn .9s var(--ease) .75s both;
  position:relative;z-index:1;
}
.lead strong{color:#e8e8ea;font-weight:600;white-space:nowrap}
.lead code{
  font-family:'JetBrains Mono',monospace;font-size:.92em;color:var(--text);
  background:rgba(255,255,255,0.05);padding:3px 8px;border-radius:7px;
  border:1px solid var(--border);white-space:nowrap;
}
@keyframes leadIn{from{opacity:0;transform:translateY(18px);filter:blur(4px)}to{opacity:1;transform:translateY(0);filter:blur(0)}}

.hero-cta{
  display:flex;gap:12px;justify-content:center;flex-wrap:wrap;
  opacity:0;transform:translateY(18px);
  animation:leadIn .9s var(--ease) .95s both;
  position:relative;z-index:1;
}
.hero-cta .btn{padding:15px 28px;height:auto;font-size:14.5px;border-radius:14px}
.hero-cta .btn svg{width:16px;height:16px}

.code-show{
  margin:70px auto 0;max-width:560px;
  display:flex;gap:9px;justify-content:center;flex-wrap:nowrap;
  opacity:0;transform:translateY(24px);
  animation:leadIn .9s var(--ease) 1.1s both;
  position:relative;z-index:1;
}
.code-show::before{
  content:'';position:absolute;inset:-40px -60px;
  background:radial-gradient(ellipse at center,rgba(255,255,255,0.08),transparent 70%);
  filter:blur(40px);pointer-events:none;z-index:-1;
}
.code-cell{
  flex:1 1 0;max-width:70px;aspect-ratio:2/3;border-radius:13px;
  background:linear-gradient(160deg,rgba(255,255,255,0.065),rgba(255,255,255,0.018));
  border:1px solid var(--border-2);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  display:grid;place-items:center;
  font-family:'JetBrains Mono',monospace;font-weight:600;
  font-size:clamp(21px,3.4vw,27px);color:#fff;
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.12),0 12px 28px -12px rgba(0,0,0,0.7);
  animation:cellIn .6s var(--ease) both;
  transition:transform .35s var(--ease),border-color .3s,box-shadow .35s,color .25s;
}
.code-cell:nth-child(1){animation-delay:.4s}
.code-cell:nth-child(2){animation-delay:.48s}
.code-cell:nth-child(3){animation-delay:.56s}
.code-cell:nth-child(4){animation-delay:.64s}
.code-cell:nth-child(5){animation-delay:.72s}
.code-cell:nth-child(6){animation-delay:.80s}
.code-cell:hover{
  transform:translateY(-5px);border-color:rgba(255,255,255,0.3);
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.15),0 18px 34px -12px rgba(0,0,0,0.8);
}
.code-cell.flip{animation:flipIn .55s var(--ease) both}
@keyframes flipIn{0%{opacity:.35;transform:translateY(-8px) rotateX(-70deg);color:transparent}100%{opacity:1;transform:translateY(0) rotateX(0);color:#fff}}
@keyframes cellIn{from{opacity:0;transform:translateY(20px) scale(.88)}to{opacity:1;transform:translateY(0) scale(1)}}

.code-caption{
  margin-top:22px;font-size:12.5px;color:var(--text-mute);letter-spacing:0.01em;
  opacity:0;transform:translateY(12px);
  animation:leadIn .8s var(--ease) 1.3s both;
  display:inline-flex;align-items:center;gap:9px;
  font-family:'JetBrains Mono',monospace;
}
.code-caption svg{width:13px;height:13px;opacity:.6;display:block}

.marquee{
  margin-top:110px;padding:22px 0;
  border-top:1px solid var(--border);border-bottom:1px solid var(--border);
  overflow:hidden;
  mask-image:linear-gradient(90deg,transparent,#000 10%,#000 90%,transparent);
  -webkit-mask-image:linear-gradient(90deg,transparent,#000 10%,#000 90%,transparent);
}
.marquee-track{display:flex;gap:56px;width:max-content;animation:scroll 46s linear infinite;will-change:transform}
.marquee-track span{
  font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:500;
  letter-spacing:0.14em;text-transform:uppercase;color:var(--text-mute);
  display:inline-flex;align-items:center;gap:56px;white-space:nowrap;
}
.marquee-track span::after{content:'';width:5px;height:5px;border-radius:50%;background:var(--text-mute);display:inline-block;opacity:.6}
@keyframes scroll{to{transform:translateX(-50%)}}

section{padding:120px 0;position:relative}
.sec-head{max-width:660px;margin-bottom:56px}
h2{
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:clamp(28px,4.6vw,50px);line-height:1.05;
  letter-spacing:-0.035em;margin-bottom:18px;text-wrap:balance;
}
h2 .dim{color:var(--text-mute);font-weight:400}
.sec-head p{color:var(--text-dim);font-size:15.5px;line-height:1.65}

.tile-groups{display:flex;flex-direction:column;gap:56px}
.tile-group{display:flex;flex-direction:column;gap:18px}
.group-head{
  display:flex;align-items:baseline;justify-content:space-between;
  gap:18px;flex-wrap:wrap;
  padding-bottom:14px;
  border-bottom:1px solid var(--border);
}
.group-title{
  display:inline-flex;align-items:center;gap:12px;
  font-family:'Unbounded',sans-serif;font-weight:600;
  font-size:clamp(18px,2.4vw,22px);
  letter-spacing:-0.02em;color:#fff;
}
.group-title::before{
  content:'';width:10px;height:10px;border-radius:50%;
  background:#fff;box-shadow:0 0 0 4px rgba(255,255,255,0.08);
  flex-shrink:0;
  animation:dotPulse 2.4s ease-in-out infinite;
}
@keyframes dotPulse{
  0%,100%{box-shadow:0 0 0 4px rgba(255,255,255,0.08)}
  50%{box-shadow:0 0 0 8px rgba(255,255,255,0.02)}
}
.group-meta{
  font-family:'JetBrains Mono',monospace;
  font-size:11.5px;letter-spacing:0.1em;text-transform:uppercase;
  color:var(--text-mute);
}

.glass{
  position:relative;border-radius:var(--radius);
  background:linear-gradient(150deg,rgba(255,255,255,0.055),rgba(255,255,255,0.014));
  backdrop-filter:blur(22px) saturate(150%);
  -webkit-backdrop-filter:blur(22px) saturate(150%);
  box-shadow:0 24px 60px -28px rgba(0,0,0,0.75),inset 0 1px 0 rgba(255,255,255,0.07);
  overflow:hidden;border:1px solid transparent;
  transition:
    transform .55s var(--ease),
    box-shadow .55s var(--ease),
    opacity .55s var(--ease),
    filter .55s var(--ease),
    border-color .3s;
  will-change:transform;
}
.glass::before{
  content:'';position:absolute;inset:0;border-radius:inherit;padding:1px;
  background:linear-gradient(150deg,rgba(255,255,255,0.32),rgba(255,255,255,0.03) 35%,rgba(255,255,255,0.02) 65%,rgba(255,255,255,0.16));
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
  -webkit-mask-composite:xor;
  mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
  mask-composite:exclude;pointer-events:none;
}
.glass::after{
  content:'';position:absolute;top:-60%;left:-60%;width:100%;height:220%;
  background:linear-gradient(115deg,transparent 42%,rgba(255,255,255,0.07) 50%,transparent 58%);
  transform:translateX(0);
  transition:transform 1s var(--ease);pointer-events:none;
}
.glass:hover::after{transform:translateX(120%)}

.tile-pop:hover{transform:translateY(-8px) scale(1.01);box-shadow:0 36px 80px -30px rgba(0,0,0,0.9),inset 0 1px 0 rgba(255,255,255,0.1)}
.tile-tilt-l:hover{transform:perspective(1000px) rotateY(-4deg) rotateX(2deg);box-shadow:0 36px 80px -30px rgba(0,0,0,0.9)}
.tile-tilt-r:hover{transform:perspective(1000px) rotateY(4deg) rotateX(-2deg);box-shadow:0 36px 80px -30px rgba(0,0,0,0.9)}
.tile-zoom:hover{transform:scale(1.03);box-shadow:0 36px 80px -30px rgba(0,0,0,0.9)}
.tile-float-l:hover{transform:translateY(-6px) rotate(-1.5deg);box-shadow:0 36px 80px -30px rgba(0,0,0,0.9)}
.tile-float-r:hover{transform:translateY(-6px) rotate(1.5deg);box-shadow:0 36px 80px -30px rgba(0,0,0,0.9)}
.tile-slide:hover{transform:translateX(10px);box-shadow:0 36px 80px -30px rgba(0,0,0,0.9)}
.tile-glow:hover{transform:translateY(-4px);box-shadow:0 0 44px -8px rgba(255,255,255,0.22),0 36px 80px -30px rgba(0,0,0,0.9),inset 0 1px 0 rgba(255,255,255,0.12)}
.tile-rotate:hover{transform:rotate(-1.8deg) scale(1.01);box-shadow:0 36px 80px -30px rgba(0,0,0,0.9)}
.tile-skew:hover{transform:perspective(1000px) rotateX(3deg) translateY(-3px);box-shadow:0 36px 80px -30px rgba(0,0,0,0.9)}

.bento{display:grid;grid-template-columns:repeat(6,1fr);gap:16px}
.bento .glass{padding:32px;display:flex;flex-direction:column}
.b-lg{grid-column:span 4;min-height:300px}
.b-md{grid-column:span 3;min-height:250px}
.b-sm{grid-column:span 2;min-height:240px}
.icon-box{
  width:48px;height:48px;border-radius:14px;
  display:grid;place-items:center;
  background:linear-gradient(150deg,rgba(255,255,255,0.1),rgba(255,255,255,0.02));
  border:1px solid var(--border-2);margin-bottom:24px;color:var(--text-dim);
  transition:color .3s,border-color .3s;
  flex-shrink:0;
}
.icon-box svg{width:21px;height:21px;display:block}
.glass:hover .icon-box{color:#fff;border-color:var(--border-3)}
.bento h3{font-family:'Unbounded',sans-serif;font-weight:600;font-size:17.5px;letter-spacing:-0.02em;line-height:1.3;margin-bottom:11px}
.bento p{color:var(--text-dim);font-size:14px;line-height:1.65;max-width:48ch}
.bento p code,.step p code,.showcase-list code{
  font-family:'JetBrains Mono',monospace;color:var(--text);font-size:.92em;
  background:rgba(255,255,255,0.04);padding:2px 7px;border-radius:6px;
  border:1px solid var(--border);white-space:nowrap;
}

.feature-list{
  list-style:none;display:flex;flex-direction:column;gap:12px;
  margin-top:24px;padding-top:22px;
  border-top:1px solid var(--border);
}
.feature-list li{
  display:flex;align-items:center;gap:12px;
  font-size:13.5px;color:var(--text-dim);line-height:1.5;
}
.feature-list li::before{
  content:'';flex-shrink:0;
  width:6px;height:6px;border-radius:50%;
  background:#fff;box-shadow:0 0 0 3px rgba(255,255,255,0.08);
}
.feature-list li strong{color:#fff;font-weight:600}

.showcase{display:grid;grid-template-columns:1fr 1fr;gap:60px;align-items:center}
.showcase-text h2{margin-bottom:20px}
.showcase-text p{color:var(--text-dim);font-size:15.5px;line-height:1.7;margin-bottom:28px;max-width:48ch}
.showcase-list{display:flex;flex-direction:column;gap:14px;list-style:none}
.showcase-list li{display:flex;align-items:flex-start;gap:12px;color:var(--text-dim);font-size:14.5px;line-height:1.6}
.showcase-list li svg{width:18px;height:18px;flex-shrink:0;margin-top:2px;color:#fff;display:block}
.showcase-list li strong{color:#fff;font-weight:600}

.mock{
  position:relative;border-radius:24px;
  background:linear-gradient(150deg,rgba(255,255,255,0.06),rgba(255,255,255,0.015));
  border:1px solid var(--border-2);
  backdrop-filter:blur(24px) saturate(150%);-webkit-backdrop-filter:blur(24px) saturate(150%);
  padding:26px;
  box-shadow:0 40px 90px -30px rgba(0,0,0,0.85),inset 0 1px 0 rgba(255,255,255,0.08);
  transition:transform .8s var(--ease);will-change:transform;
}
.mock:hover{transform:perspective(1200px) rotateY(2deg) rotateX(-1deg)}
.mock::before{
  content:'';position:absolute;inset:0;border-radius:inherit;padding:1px;
  background:linear-gradient(150deg,rgba(255,255,255,0.4),rgba(255,255,255,0.03) 40%,rgba(255,255,255,0.2));
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
  -webkit-mask-composite:xor;
  mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
  mask-composite:exclude;pointer-events:none;
}
.mock-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
.mock-code{
  font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;letter-spacing:0.12em;
  padding:5px 11px;border-radius:8px;background:rgba(255,255,255,0.05);
  border:1px solid var(--border-2);color:var(--text);
}
.mock-dots{display:flex;gap:6px}
.mock-dots span{width:9px;height:9px;border-radius:50%;background:rgba(255,255,255,0.12)}
.mock-title{font-family:'Unbounded',sans-serif;font-size:19px;font-weight:600;letter-spacing:-0.02em;color:#fff;margin-bottom:10px}
.mock-body{font-size:13.5px;line-height:1.65;color:var(--text-dim);margin-bottom:18px}
.mock-gallery{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.mock-thumb{
  aspect-ratio:1/1;border-radius:10px;
  background:linear-gradient(140deg,rgba(255,255,255,0.14),rgba(255,255,255,0.04));
  border:1px solid var(--border);position:relative;overflow:hidden;
}
.mock-thumb::after{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(255,255,255,0.1),transparent 60%)}
.mock-thumb:nth-child(2)::after{background:linear-gradient(45deg,rgba(255,255,255,0.08),transparent 60%)}
.mock-thumb:nth-child(3)::after{background:linear-gradient(160deg,rgba(255,255,255,0.12),transparent 60%)}

/* Компактный блок статистики */
.stats{
  display:grid;grid-template-columns:repeat(4,1fr);gap:1px;
  border-radius:var(--radius);overflow:hidden;
  background:var(--border);border:1px solid var(--border);
}
.stat{
  background:rgba(10,10,10,0.7);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  padding:34px 22px;text-align:center;transition:background .35s;
}
.stat:hover{background:rgba(18,18,18,0.85)}
.stat-val{
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:clamp(22px,2.8vw,34px);letter-spacing:-0.035em;
  background:linear-gradient(160deg,#fff,rgba(255,255,255,0.5));
  -webkit-background-clip:text;background-clip:text;color:transparent;
  line-height:1.05;margin-bottom:8px;font-variant-numeric:tabular-nums;
}
.stat-lbl{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--text-mute);letter-spacing:0.08em;text-transform:uppercase;line-height:1.35}

.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;position:relative}
.steps::before{
  content:'';position:absolute;top:80px;left:12%;right:12%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.15) 20%,rgba(255,255,255,0.15) 80%,transparent);
  pointer-events:none;z-index:0;
}
.step{padding:32px;display:flex;flex-direction:column;position:relative;z-index:1}
.step-num{
  font-family:'JetBrains Mono',monospace;font-size:11.5px;
  color:var(--text-mute);letter-spacing:0.14em;margin-bottom:22px;text-transform:uppercase;
  display:inline-flex;align-items:center;gap:8px;
}
.step-num::before{content:'';width:8px;height:8px;border-radius:50%;background:#fff;box-shadow:0 0 0 4px rgba(255,255,255,0.1);display:inline-block;flex-shrink:0}
.step h3{font-family:'Unbounded',sans-serif;font-weight:600;font-size:18px;letter-spacing:-0.02em;margin-bottom:11px}
.step p{color:var(--text-dim);font-size:14px;line-height:1.65}

.cta{
  position:relative;border-radius:36px;padding:90px 40px;
  text-align:center;overflow:hidden;
  background:linear-gradient(150deg,rgba(255,255,255,0.055),rgba(255,255,255,0.012));
  backdrop-filter:blur(26px) saturate(150%);-webkit-backdrop-filter:blur(26px) saturate(150%);
  border:1px solid var(--border-2);
  box-shadow:0 40px 100px -50px rgba(0,0,0,0.9),inset 0 1px 0 rgba(255,255,255,0.08);
}
.cta::before{
  content:'';position:absolute;width:900px;height:700px;border-radius:50%;
  background:radial-gradient(circle,rgba(255,255,255,0.1),transparent 65%);
  top:-400px;left:50%;transform:translateX(-50%);filter:blur(60px);pointer-events:none;
}
.cta > *{position:relative;z-index:1}
.cta h2{font-size:clamp(32px,5.2vw,58px);margin-bottom:20px;letter-spacing:-0.04em}
.cta p{color:var(--text-dim);font-size:16.5px;max-width:500px;margin:0 auto 36px;line-height:1.65}
.cta .btn{padding:17px 36px;height:auto;font-size:15px;border-radius:15px}
.cta .btn svg{width:17px;height:17px}
.cta-note{font-size:12.5px;color:var(--text-mute);margin-top:24px;margin-bottom:0;font-family:'JetBrains Mono',monospace;letter-spacing:0.06em}

@media (max-width:1080px){
  .showcase{grid-template-columns:1fr;gap:48px}
  .mock{max-width:520px;margin:0 auto}
  .mock:hover{transform:none}
}
@media (max-width:1000px){
  .bento{grid-template-columns:repeat(4,1fr)}
  .b-lg{grid-column:span 4}
  .b-md{grid-column:span 4}
  .b-sm{grid-column:span 2}
  .stats{grid-template-columns:repeat(2,1fr)}
  .steps{grid-template-columns:1fr}
  .steps::before{display:none}
}
@media (max-width:720px){
  .hero{padding:130px 0 0}
  section{padding:80px 0}
  .tile-groups{gap:36px}
  .bento{grid-template-columns:1fr;gap:14px}
  .bento .glass{grid-column:span 1 !important;padding:26px;min-height:auto}
  .b-lg{min-height:auto}
  .stats{grid-template-columns:1fr 1fr}
  .stat{padding:26px 16px}
  .stat-val{font-size:clamp(20px,5vw,28px)}
  .cta{padding:60px 24px;border-radius:26px}
  .cta .btn{padding:15px 28px;font-size:14px;width:100%}
  .hero-cta{flex-direction:column;align-items:stretch}
  .hero-cta .btn{justify-content:center;width:100%}
  .marquee-track span{font-size:12px;gap:36px}
  .marquee-track{gap:36px}
  .marquee{margin-top:80px}
  .code-show{margin-top:52px;gap:6px}
  .code-cell{max-width:44px;border-radius:11px}
  .badge{font-size:11.5px;padding:7px 13px 7px 8px;margin-bottom:32px;gap:8px}
  .badge-dot{width:18px;height:18px}
  .badge-dot svg{width:9px;height:9px}
  .group-head{flex-direction:column;gap:8px;align-items:flex-start}
  .h1-row{white-space:normal}
}
@media (max-width:420px){
  .stats{grid-template-columns:1fr}
  .mock{padding:20px}
  .mock-title{font-size:17px}
  .mock-gallery{gap:5px}
}
"""


def build_landing() -> str:
    body = r"""
<header class="hero">
  <div class="wrap">
    <div class="hero-inner">
      <div class="badge">
        <span class="badge-dot">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M13 2 3 14h8l-1 8 10-12h-8l1-8z"/>
          </svg>
        </span>
        <span>Без регистрации</span>
        <span class="badge-sep">·</span>
        <span>до 5 фото</span>
        <span class="badge-sep">·</span>
        <span>код из 6 цифр</span>
      </div>

      <h1 class="hero-title">
        <span class="h1-row">
          <span class="word" style="--i:0">Публикуйте</span><span class="word" style="--i:1">посты.</span>
        </span>
        <span class="h1-row dim">
          <span class="word" style="--i:2">Делитесь</span><span class="word" style="--i:3">шестью</span><span class="word" style="--i:4">цифрами.</span>
        </span>
      </h1>

      <p class="lead">
        Заголовок, текст, до 5 фотографий — сервер вернёт <strong>уникальный 6-значный&nbsp;код</strong>.
        Отправьте код или ссылку — пост откроется с превью прямо в мессенджере.
        Ни аккаунтов, ни паролей, ни подтверждений.
      </p>

      <div class="hero-cta">
        <a href="/app?mode=create" class="btn btn-primary">
          <span>Создать пост</span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
        </a>
        <a href="#features" class="btn btn-ghost">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></svg>
          Что внутри
        </a>
      </div>

      <div class="code-show" id="codeShow" aria-hidden="true">
        <div class="code-cell">4</div>
        <div class="code-cell">8</div>
        <div class="code-cell">1</div>
        <div class="code-cell">6</div>
        <div class="code-cell">3</div>
        <div class="code-cell">9</div>
      </div>
      <div class="code-caption">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="4" y="10" width="16" height="10" rx="2"/>
          <path d="M8 10V6a4 4 0 018 0v4"/>
        </svg>
        <span>151 200 комбинаций · цифры не повторяются</span>
      </div>
    </div>
  </div>

  <div class="marquee">
    <div class="marquee-track">
      <span>OpenGraph-превью</span><span>AES-256-GCM</span><span>6-значные коды</span>
      <span>Автосжатие фото</span><span>zstd / gzip</span><span>До 5 фото</span>
      <span>OpenGraph-превью</span><span>AES-256-GCM</span><span>6-значные коды</span>
      <span>Автосжатие фото</span><span>zstd / gzip</span><span>До 5 фото</span>
    </div>
  </div>
</header>

<section id="features">
  <div class="wrap">
    <div class="sec-head reveal reveal--up">
      <div class="eyebrow">Что умеет СЛД·NET</div>
      <h2>Всё по разделам.<br><span class="dim">Ничего лишнего.</span></h2>
      <p>Четыре группы — от публикации до технологий и экспорта. Всё, что действительно есть в сервисе.</p>
    </div>

    <div class="tile-groups">

      <div class="tile-group">
        <div class="group-head reveal reveal--left" data-delay="0">
          <div class="group-title">Публикация</div>
          <div class="group-meta">Без аккаунтов · без подтверждений</div>
        </div>
        <div class="bento">
          <div class="glass b-lg tile-pop reveal reveal--up" data-delay="60">
            <div>
              <div class="icon-box">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M14 3v4a1 1 0 001 1h4"/>
                  <path d="M17 21H7a2 2 0 01-2-2V5a2 2 0 012-2h7l5 5v11a2 2 0 01-2 2z"/>
                  <path d="M12 12v6M9 15h6"/>
                </svg>
              </div>
              <h3>Публикация без аккаунта</h3>
              <p>Отправили — получили код. Ни почты, ни пароля, ни подтверждений. Всё, что нужно для поста — уже внутри формы.</p>
            </div>
            <ul class="feature-list">
              <li><strong>120</strong> символов в заголовке</li>
              <li><strong>20 000</strong> символов в теле поста</li>
              <li><strong>5 фото</strong> · до 5 МБ каждое</li>
            </ul>
          </div>

          <div class="glass b-sm tile-tilt-l reveal reveal--right" data-delay="140">
            <div class="icon-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <rect x="3" y="3" width="18" height="18" rx="3"/>
                <circle cx="8.5" cy="8.5" r="1.2" fill="currentColor" stroke="none"/>
                <circle cx="15.5" cy="8.5" r="1.2" fill="currentColor" stroke="none"/>
                <circle cx="8.5" cy="15.5" r="1.2" fill="currentColor" stroke="none"/>
                <circle cx="15.5" cy="15.5" r="1.2" fill="currentColor" stroke="none"/>
                <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none"/>
              </svg>
            </div>
            <h3>Коды без повторов</h3>
            <p>Все шесть цифр кода — разные. Легко продиктовать голосом, трудно перепутать при наборе.</p>
          </div>
        </div>
      </div>

      <div class="tile-group">
        <div class="group-head reveal reveal--right" data-delay="0">
          <div class="group-title">Технологии и хранение</div>
          <div class="group-meta">AES-256 · zstd · RAM</div>
        </div>
        <div class="bento">
          <div class="glass b-sm tile-tilt-r reveal reveal--zoom" data-delay="60">
            <div class="icon-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 2 4 6v6c0 5 3.4 9.3 8 10 4.6-.7 8-5 8-10V6l-8-4z"/>
                <rect x="9" y="11" width="6" height="5" rx="1"/>
                <path d="M10 11V9.5a2 2 0 0 1 4 0V11"/>
              </svg>
            </div>
            <h3>AES-256-GCM</h3>
            <p>Каждый пост и каждое фото шифруются уникальным nonce прямо в памяти сервера. Диск не используется вовсе.</p>
          </div>

          <div class="glass b-sm tile-float-l reveal reveal--left" data-delay="140">
            <div class="icon-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M8 3v5H3"/>
                <path d="M16 3v5h5"/>
                <path d="M8 21v-5H3"/>
                <path d="M16 21v-5h5"/>
              </svg>
            </div>
            <h3>Сжатие zstd / gzip</h3>
            <p>Метаданные сжимаются перед шифрованием. Экономия памяти без потерь данных.</p>
          </div>

          <div class="glass b-sm tile-glow reveal reveal--blur" data-delay="220">
            <div class="icon-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <rect x="6" y="6" width="12" height="12" rx="2"/>
                <rect x="9.5" y="9.5" width="5" height="5" rx="0.5"/>
                <path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>
              </svg>
            </div>
            <h3>Хранение в RAM</h3>
            <p>Данные живут только в оперативной памяти. Перезапуск сервера полностью очищает все посты.</p>
          </div>
        </div>
      </div>

      <div class="tile-group">
        <div class="group-head reveal reveal--up" data-delay="0">
          <div class="group-title">Ссылки и превью</div>
          <div class="group-meta">OpenGraph · единый адрес</div>
        </div>
        <div class="bento">
          <div class="glass b-md tile-tilt-l reveal reveal--flip" data-delay="60">
            <div class="icon-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <rect x="3" y="3" width="18" height="18" rx="2.5"/>
                <rect x="6" y="6" width="12" height="7" rx="1"/>
                <circle cx="9" cy="9" r="1" fill="currentColor" stroke="none"/>
                <path d="M6 13l3-2 2 2 3-3 4 4"/>
                <path d="M8 17h8"/>
              </svg>
            </div>
            <h3>OpenGraph-превью</h3>
            <p>Ссылка <code>/p/<span data-code>482163</span></code> разворачивается в Telegram и Discord: заголовок, краткое описание и первое фото.</p>
          </div>

          <div class="glass b-md tile-slide reveal reveal--right" data-delay="140">
            <div class="icon-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M10 13a5 5 0 007.07 0l3-3a5 5 0 00-7.07-7.07l-1.5 1.5"/>
                <path d="M14 11a5 5 0 00-7.07 0l-3 3a5 5 0 007.07 7.07l1.5-1.5"/>
              </svg>
            </div>
            <h3>Единая ссылка на пост</h3>
            <p>Каждый пост живёт по одному адресу — <code>/p/<span data-code>482163</span></code>. Одна ссылка и для копирования, и для мессенджеров, и для перехода.</p>
          </div>
        </div>
      </div>

      <div class="tile-group">
        <div class="group-head reveal reveal--left" data-delay="0">
          <div class="group-title">Удобство</div>
          <div class="group-meta">Скорость · галерея · сжатие</div>
        </div>
        <div class="bento">
          <div class="glass b-sm tile-zoom reveal reveal--elastic" data-delay="60">
            <div class="icon-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <circle cx="11" cy="11" r="7"/>
                <path d="m20 20-3.5-3.5"/>
                <path d="M11 8v3.5l2.5 1.5"/>
              </svg>
            </div>
            <h3>Мгновенный поиск</h3>
            <p>Пост подгружается по мере ввода кода. Ввели последнюю цифру — пост уже на экране.</p>
          </div>

          <div class="glass b-sm tile-float-r reveal reveal--up" data-delay="140">
            <div class="icon-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 3v11"/>
                <path d="M8 10l4 4 4-4"/>
                <path d="M4 15v3a2 2 0 002 2h12a2 2 0 002-2v-3"/>
              </svg>
            </div>
            <h3>Автосжатие до 60 КБ</h3>
            <p>Фото пережимаются прямо в браузере перед отправкой — читаемость сохраняется, вес падает до ~60 КБ.</p>
          </div>

          <div class="glass b-sm tile-rotate reveal reveal--rotate" data-delay="220">
            <div class="icon-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <rect x="3" y="3" width="14" height="14" rx="2"/>
                <circle cx="7.5" cy="7.5" r="1.2" fill="currentColor" stroke="none"/>
                <path d="M3 13l3-3 3 3 3-3 5 5"/>
                <circle cx="17.5" cy="17.5" r="3.5"/>
                <path d="m21 21-1.8-1.8"/>
              </svg>
            </div>
            <h3>Галерея с зумом</h3>
            <p>Колесо мыши, двойной клик, правая кнопка мыши, панорама по левой — как в настоящем просмотрщике. На тач-экранах — pinch.</p>
          </div>
        </div>
      </div>

    </div>
  </div>
</section>

<section id="showcase">
  <div class="wrap">
    <div class="showcase">
      <div class="showcase-text reveal reveal--left">
        <div class="eyebrow">Как выглядит пост</div>
        <h2>Заголовок, текст <span class="dim">и до пяти фото</span></h2>
        <p>Каждый пост — это карточка с названием, описанием и галереей. Ничего лишнего, ничего отвлекающего.</p>
        <ul class="showcase-list">
          <li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg><span><strong>Заголовок</strong> — до 120 символов, чтобы передать суть в одну строку</span></li>
          <li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg><span><strong>Тело</strong> — до 20 000 символов, с сохранением переносов строк</span></li>
          <li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg><span><strong>Галерея</strong> — до 5 фото с зумом и панорамированием</span></li>
          <li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg><span><strong>Кнопка «копировать ссылку»</strong> — рядом с каждым найденным постом</span></li>
        </ul>
      </div>

      <div class="mock reveal reveal--right" data-delay="120" aria-hidden="true">
        <div class="mock-head">
          <span class="mock-code">#<span data-code>482163</span></span>
          <div class="mock-dots"><span></span><span></span><span></span></div>
        </div>
        <div class="mock-title">Как подготовить питч за 5 минут</div>
        <div class="mock-body">Если у вас есть всего одна минута на то, чтобы объяснить идею — используйте структуру «проблема → решение → результат». Работает в 9 из 10 случаев.</div>
        <div class="mock-gallery"><div class="mock-thumb"></div><div class="mock-thumb"></div><div class="mock-thumb"></div></div>
      </div>
    </div>
  </div>
</section>

<section id="stats" style="padding-top:0">
  <div class="wrap">
    <div class="stats reveal reveal--zoom">
      <div class="stat"><div class="stat-val">151 200</div><div class="stat-lbl">Уникальных кодов</div></div>
      <div class="stat"><div class="stat-val">20 000</div><div class="stat-lbl">Символов в теле</div></div>
      <div class="stat"><div class="stat-val">5 МБ</div><div class="stat-lbl">Лимит на файл</div></div>
      <div class="stat"><div class="stat-val">60 КБ</div><div class="stat-lbl">После сжатия</div></div>
    </div>
  </div>
</section>

<section id="how" style="padding-top:0">
  <div class="wrap">
    <div class="sec-head reveal reveal--up">
      <div class="eyebrow">Как это работает</div>
      <h2>Три шага от идеи <span class="dim">до кода</span></h2>
      <p>Никаких онбордингов и прогресс-баров. Заполнили, получили код, отправили.</p>
    </div>

    <div class="steps">
      <div class="glass step tile-pop reveal reveal--up" data-delay="60">
        <div class="step-num">Шаг 01</div>
        <h3>Заполняете пост</h3>
        <p>Заголовок, текст и до 5 фото. Перетащите файлы, выберите через диалог или вставьте из буфера по <code>Ctrl+V</code>.</p>
      </div>
      <div class="glass step tile-tilt-l reveal reveal--up" data-delay="140">
        <div class="step-num">Шаг 02</div>
        <h3>Получаете код</h3>
        <p>Сервер генерирует шесть уникальных цифр и показывает их в модальном окне. Копируется одной кнопкой.</p>
      </div>
      <div class="glass step tile-tilt-r reveal reveal--up" data-delay="220">
        <div class="step-num">Шаг 03</div>
        <h3>Делитесь ссылкой</h3>
        <p>Отправьте ссылку <code>/p/<span data-code>482163</span></code> — она развернётся в превью, а переход откроет сам пост.</p>
      </div>
    </div>
  </div>
</section>

<section id="specs" style="padding-top:0">
  <div class="wrap">
    <div class="sec-head reveal reveal--up">
      <div class="eyebrow">Технологии</div>
      <h2>Всё серьёзно</h2>
      <p>Шифрование, сжатие и мгновенный поиск — под капотом работают промышленные алгоритмы.</p>
    </div>

    <div class="stats reveal reveal--zoom">
      <div class="stat"><div class="stat-val">AES-256</div><div class="stat-lbl">GCM шифрование</div></div>
      <div class="stat"><div class="stat-val">zstd</div><div class="stat-lbl">Сжатие метаданных</div></div>
      <div class="stat"><div class="stat-val">RAM</div><div class="stat-lbl">Хранение в памяти</div></div>
      <div class="stat"><div class="stat-val">OG</div><div class="stat-lbl">OpenGraph-превью</div></div>
    </div>
  </div>
</section>

<section id="join" style="padding-top:0">
  <div class="wrap">
    <div class="cta reveal reveal--skew">
      <h2>Опубликовать первый пост</h2>
      <p>Заголовок, текст, до пяти фото — и шесть цифр, чтобы поделиться результатом.</p>
      <a href="/app?mode=create" class="btn btn-primary">
        <span>Открыть приложение</span>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
      </a>
      <div class="cta-note">/app</div>
    </div>
  </div>
</section>
"""
    return render_shell(
        title="СЛД·NET — посты по 6-значному коду",
        body=body,
        extra_css=LANDING_CSS,
        og='<meta name="description" content="Публикуйте посты с фото, делитесь шестью цифрами. Без аккаунтов, с шифрованием и автосжатием.">',
    )


# ============================================================
# РАБОЧАЯ ОБЛАСТЬ  (/app, /p/{code})
# ============================================================

APP_CSS = r"""
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
  --ok:#7ec899;
  --err:#e08080;
  --ease:cubic-bezier(.2,.8,.2,1);
  --topbar-h:80px;
}
html,body{
  background:var(--bg);color:var(--text);
  font-family:'Manrope',system-ui,-apple-system,sans-serif;
  font-size:15px;line-height:1.55;
  min-height:100%;overflow-x:clip;
  -webkit-font-smoothing:antialiased;
  -webkit-user-select:none;-moz-user-select:none;-ms-user-select:none;user-select:none;
  -webkit-tap-highlight-color:transparent;
}
input,textarea,[contenteditable],.modal-code,.otp-cell,.post-body,.post-title,.share-link-url{
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
.cur-ring.hover{width:58px;height:58px;border-color:rgba(255,255,255,0.22);background:rgba(255,255,255,0.05);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
.cur-ring.click{width:24px;height:24px;background:rgba(255,255,255,0.12)}
@media (max-width:900px),(hover:none){.cur-dot,.cur-ring{display:none}}

.page-loader{
  position:fixed;inset:0;z-index:99998;
  background:var(--bg);
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
  50%{transform:scale(1.06);box-shadow:0 12px 34px -8px rgba(255,255,255,0.45),0 0 0 14px rgba(255,255,255,0)}
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
.halo-1{width:660px;height:660px;background:radial-gradient(circle,rgba(255,255,255,0.07),transparent 65%);top:-260px;left:-160px;animation:drift1 32s ease-in-out infinite}
.halo-2{width:560px;height:560px;background:radial-gradient(circle,rgba(255,255,255,0.045),transparent 65%);bottom:-200px;right:-180px;animation:drift2 36s ease-in-out infinite}
@keyframes drift1{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(100px,80px) scale(1.1)}}
@keyframes drift2{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(-110px,-80px) scale(1.12)}}
.cursor-glow{position:fixed;inset:0;z-index:-2;pointer-events:none;background:radial-gradient(560px circle at var(--mx,50%) var(--my,50%),rgba(255,255,255,0.03),transparent 60%)}
.grid-bg{
  position:fixed;inset:0;z-index:-1;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,0.02) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,0.02) 1px,transparent 1px);
  background-size:68px 68px;
  mask-image:radial-gradient(ellipse 95% 85% at 50% 0%,#000 25%,transparent 85%);
  -webkit-mask-image:radial-gradient(ellipse 95% 85% at 50% 0%,#000 25%,transparent 85%);
}
.grain{position:fixed;inset:0;z-index:9998;pointer-events:none;opacity:.03;mix-blend-mode:overlay;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}

.topbar{
  position:fixed;top:14px;left:50%;transform:translateX(-50%);
  z-index:100;width:calc(100% - 28px);max-width:1160px;border-radius:16px;
  padding:10px 12px 10px 18px;
  background:rgba(10,10,10,0.72);
  backdrop-filter:blur(24px) saturate(160%);-webkit-backdrop-filter:blur(24px) saturate(160%);
  border:1px solid var(--border);
  box-shadow:0 12px 40px rgba(0,0,0,0.5),inset 0 1px 0 rgba(255,255,255,0.06);
  /* Безопасные зоны на iPhone */
  padding-top: max(10px, env(safe-area-inset-top, 0px) + 4px);
}
.topbar-inner{display:flex;align-items:center;gap:14px;justify-content:space-between;flex-wrap:nowrap}
.logo{
  display:inline-flex;align-items:center;gap:10px;
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:13.5px;letter-spacing:-0.01em;
  text-decoration:none;color:#fff;white-space:nowrap;flex-shrink:0;
}
.logo-mark{
  width:28px;height:28px;border-radius:8px;
  background:linear-gradient(140deg,#fff,#c4c4c8);
  display:grid;place-items:center;position:relative;overflow:hidden;
  box-shadow:0 4px 14px rgba(0,0,0,0.5),inset 0 -1px 0 rgba(0,0,0,0.15);
  transition:transform .35s var(--ease);
}
.logo:hover .logo-mark{transform:rotate(-6deg) scale(1.05)}
.logo-mark::after{content:'';position:absolute;inset:0;background:linear-gradient(150deg,rgba(255,255,255,0.9),transparent 55%);pointer-events:none}
.logo-mark svg{width:14px;height:14px;position:relative;z-index:1;color:#08080a;display:block}
.logo-word{display:inline-flex;align-items:baseline;gap:1px}
.logo-word .ldot{color:var(--text-mute);font-weight:400;margin:0 2px}
.logo-word .lnet{color:var(--text-dim);font-weight:500}

.menu{
  position:relative;display:inline-flex;gap:4px;padding:4px;
  border-radius:12px;background:rgba(255,255,255,0.028);
  border:1px solid var(--border);flex-shrink:0;
}
.menu-pill{
  position:absolute;top:4px;bottom:4px;left:0;width:0;
  border-radius:9px;background:rgba(255,255,255,0.09);
  border:1px solid rgba(255,255,255,0.13);
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.09);
  transform:translateX(0);
  transition:transform .34s var(--ease),width .34s var(--ease);
  z-index:0;pointer-events:none;
}
.tab{
  position:relative;z-index:1;
  display:inline-flex;align-items:center;gap:8px;
  padding:8px 15px;border-radius:9px;
  background:transparent;border:1px solid transparent;
  color:var(--text-dim);font:inherit;font-size:13px;font-weight:500;
  cursor:pointer;transition:color .22s;white-space:nowrap;
  -webkit-tap-highlight-color:transparent;
}
.tab svg{width:15px;height:15px;flex-shrink:0;display:block}
.tab:hover{color:#fff}
.tab.active{color:#fff}
.back-btn{
  display:inline-flex;align-items:center;gap:7px;
  color:var(--text-dim);text-decoration:none;
  font-size:13px;font-weight:500;
  padding:8px 12px;border-radius:10px;
  transition:color .2s,background .2s;white-space:nowrap;flex-shrink:0;
}
.back-btn svg{width:14px;height:14px;display:block}
.back-btn:hover{color:#fff;background:rgba(255,255,255,0.05)}

.exit-btn{
  display:none;
  align-items:center;gap:7px;
  padding:9px 14px;border-radius:10px;
  background:rgba(200,70,70,0.92);
  border:1px solid rgba(240,110,110,0.9);
  color:#fff;font:inherit;font-size:13px;font-weight:600;
  cursor:pointer;text-decoration:none;white-space:nowrap;
  transition:background .22s,border-color .22s,box-shadow .22s;
  box-shadow:0 6px 18px -8px rgba(200,70,70,0.5),inset 0 1px 0 rgba(255,255,255,0.18);
}
.exit-btn svg{width:14px;height:14px;display:block}
.exit-btn:hover,.exit-btn:active{
  background:rgba(220,80,80,1);border-color:rgba(255,130,130,1);
  box-shadow:0 10px 24px -8px rgba(200,70,70,0.6),inset 0 1px 0 rgba(255,255,255,0.22);
}
.exit-btn:active{transform:scale(.98)}

.app{
  min-height:100dvh;
  display:flex;flex-direction:column;align-items:center;
  padding:calc(var(--topbar-h, 80px) + 28px) 20px 40px;
}
.stage{width:100%;max-width:560px;margin:auto 0}
.stage[hidden]{display:none}
.panel{
  position:absolute;top:0;left:0;right:0;
  display:flex;flex-direction:column;gap:12px;
  opacity:0;pointer-events:none;visibility:hidden;
  transition:opacity .22s ease;
}
.panel.active{position:relative;opacity:1;pointer-events:auto;visibility:visible}

.card{
  position:relative;border-radius:18px;
  background:linear-gradient(155deg,rgba(255,255,255,0.05),rgba(255,255,255,0.012));
  border:1px solid var(--border);
  backdrop-filter:blur(20px) saturate(150%);-webkit-backdrop-filter:blur(20px) saturate(150%);
  box-shadow:0 20px 50px -28px rgba(0,0,0,0.8),inset 0 1px 0 rgba(255,255,255,0.05);
  padding:18px;
}

.input-wrap{position:relative;display:flex}
.input-wrap + .input-wrap{margin-top:10px}
.input-wrap .iw-icon{position:absolute;left:13px;width:16px;height:16px;color:var(--text-mute);pointer-events:none;transition:color .18s}
.input-wrap input.field,.input-wrap textarea.field{padding-left:40px}
.input-wrap input.field + .iw-icon,.input-wrap textarea.field + .iw-icon{top:13px}
.input-wrap.textarea-wrap .iw-icon{top:14px}
.input-wrap:focus-within .iw-icon{color:var(--text)}
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

.checkbox-wrap{
  display:flex;align-items:flex-start;gap:11px;
  margin-top:12px;padding:12px 14px;border-radius:12px;
  background:var(--input);border:1px solid var(--border);
  cursor:pointer;user-select:none;transition:border-color .18s,background .18s;
}
.checkbox-wrap:hover{border-color:var(--border-2);background:var(--input-focus)}
.checkbox-wrap input{position:absolute;opacity:0;pointer-events:none}
.checkbox-box{
  flex-shrink:0;width:20px;height:20px;border-radius:6px;
  border:1.5px solid var(--border-3);background:transparent;
  display:grid;place-items:center;margin-top:1px;
  transition:background .2s,border-color .2s;
}
.checkbox-box svg{width:12px;height:12px;color:#08080a;opacity:0;transform:scale(.6);transition:opacity .18s,transform .18s;display:block}
.checkbox-wrap input:checked + .checkbox-box{background:#fff;border-color:#fff}
.checkbox-wrap input:checked + .checkbox-box svg{opacity:1;transform:scale(1)}
.checkbox-label{font-size:13px;color:var(--text-dim);line-height:1.5;min-width:0}
.checkbox-label strong{color:var(--text);font-weight:600}

.drop{
  margin-top:10px;border:1px dashed var(--border-2);border-radius:12px;
  padding:20px 14px;text-align:center;color:var(--text-dim);cursor:pointer;
  background:var(--input);line-height:1.55;
  transition:border-color .18s,color .18s,background .18s;
  display:flex;flex-direction:column;align-items:center;gap:7px;
}
.drop .drop-icon{width:22px;height:22px;color:var(--text-mute);transition:color .18s;display:block}
.drop .drop-label{font-size:13px;color:var(--text-dim);transition:color .18s}
.drop .drop-hint{font-size:11.5px;color:var(--text-mute)}
.drop:hover{border-color:var(--border-3);background:var(--input-focus)}
.drop:hover .drop-icon,.drop:hover .drop-label{color:var(--text)}
.drop.filled{border-style:solid;border-color:var(--border-3)}
.drop.filled .drop-icon,.drop.filled .drop-label{color:var(--text)}
.drop.busy{pointer-events:none;opacity:.7}

.previews{display:grid;grid-template-columns:repeat(auto-fill,minmax(72px,1fr));gap:8px;margin-top:10px}
.preview{position:relative;aspect-ratio:1/1;border-radius:10px;overflow:hidden;background:var(--input);border:1px solid var(--border);animation:popIn .35s var(--ease)}
@keyframes popIn{from{opacity:0;transform:scale(.9)}to{opacity:1;transform:scale(1)}}
.preview img{width:100%;height:100%;object-fit:cover;display:block}
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
  font-size:12px;line-height:1;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:background .18s,border-color .18s;
}
.preview button:hover{background:rgba(224,128,128,0.25);border-color:rgba(224,128,128,0.5)}

.row{display:flex;gap:10px;margin-top:16px;flex-wrap:nowrap}
.btn{
  flex:1 1 0;min-width:0;height:46px;
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:0 18px;border-radius:12px;border:1px solid transparent;
  background:transparent;color:var(--text);
  font:inherit;font-size:13.5px;font-weight:600;
  cursor:pointer;user-select:none;
  -webkit-tap-highlight-color:transparent;
  transition:background .25s, border-color .25s, color .25s, box-shadow .25s;
  white-space:nowrap;
}
.btn svg{width:15px;height:15px;flex-shrink:0;display:block;transition:transform .3s var(--ease)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.primary{
  background:#f4f4f5;color:#08080a;
  box-shadow:0 1px 0 rgba(255,255,255,0.7) inset,0 -1px 0 rgba(0,0,0,0.12) inset,0 6px 22px -6px rgba(255,255,255,0.22);
}
.btn.primary:hover:not(:disabled){
  background:#fff;
  box-shadow:0 1px 0 rgba(255,255,255,0.9) inset,0 -1px 0 rgba(0,0,0,0.15) inset,0 10px 32px -8px rgba(255,255,255,0.4);
}
.btn.primary:active:not(:disabled){transform:scale(.985)}
.btn.primary:hover:not(:disabled) svg{transform:translateX(3px)}
.btn.ghost{
  background:var(--glass);color:var(--text);border-color:var(--border-2);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.06);
}
.btn.ghost:hover:not(:disabled){background:var(--glass-hi);border-color:var(--border-3)}
.btn.ghost:active:not(:disabled){transform:scale(.985)}

.otp-row{display:flex;gap:10px;justify-content:center;align-items:center;flex-wrap:nowrap}
.otp{display:flex;gap:6px;justify-content:center;align-items:center;margin:0}
.otp-cell{
  width:clamp(34px,9.5vw,46px);height:clamp(46px,12vw,56px);
  padding:0;text-align:center;
  font-family:'JetBrains Mono',monospace;font-size:clamp(17px,4.6vw,20px);font-weight:600;
  color:var(--text);background:var(--input);
  border:1px solid var(--border);border-radius:11px;
  outline:none;caret-color:transparent;
  -webkit-appearance:none;appearance:none;
  transition:border-color .18s,background .18s,box-shadow .18s;
}
.otp-cell:hover{background:var(--input-focus)}
.otp-cell:focus{background:var(--input-focus);border-color:var(--border-3);box-shadow:0 0 0 3px rgba(255,255,255,0.05)}
.otp-copy{
  flex-shrink:0;width:clamp(46px,12vw,56px);height:clamp(46px,12vw,56px);
  border-radius:11px;border:1px solid var(--border);background:var(--input);
  color:var(--text-mute);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;-webkit-appearance:none;appearance:none;
  transition:background .18s,border-color .18s,color .18s;
}
.otp-copy svg{width:18px;height:18px;pointer-events:none;display:block}
.otp-copy:hover:not(:disabled){background:var(--input-focus);border-color:var(--border-3);color:var(--text)}
.otp-copy:disabled{opacity:.35;cursor:not-allowed}
.otp-copy.copied{color:var(--ok);border-color:rgba(126,200,153,0.5);background:rgba(126,200,153,0.08)}
.otp-copy .icon-check{display:none}
.otp-copy.copied .icon-copy{display:none}
.otp-copy.copied .icon-check{display:block}
@keyframes shake{0%,100%{transform:translateX(0)}20%{transform:translateX(-7px)}40%{transform:translateX(7px)}60%{transform:translateX(-4px)}80%{transform:translateX(4px)}}
.otp.shake{animation:shake .32s ease}
.otp.shake .otp-cell{border-color:rgba(224,128,128,0.7);background:rgba(224,128,128,0.08)}

.center{text-align:center;padding:8px 0}
.spinner{width:22px;height:22px;border-radius:50%;border:2px solid var(--border-2);border-top-color:var(--text);animation:spin .7s linear infinite;margin:0 auto}
@keyframes spin{to{transform:rotate(360deg)}}
.spinner-label{margin-top:10px;font-size:12px;color:var(--text-dim);text-align:center;font-family:'JetBrains Mono',monospace;letter-spacing:0.04em}

#createMsg:not(:empty){margin-top:14px}
#createMsg .msg{margin-top:0}

.msg{
  display:flex;align-items:flex-start;gap:10px;
  border-radius:11px;
  padding:12px 14px;
  font-size:13px;
  background:rgba(255,255,255,0.03);
  color:var(--text);
  line-height:1.5;
  border:1px solid var(--border);
  word-break:break-word;
  animation:msgIn .3s var(--ease);
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.03);
}
.msg svg{width:16px;height:16px;flex-shrink:0;margin-top:1px;display:block}
.msg span{min-width:0;word-break:break-word}
.msg.err{background:rgba(224,128,128,0.08);border-color:rgba(224,128,128,0.32);color:#eab8b8}
.msg.ok{background:rgba(126,200,153,0.08);border-color:rgba(126,200,153,0.3);color:#b1dfc2}
@keyframes msgIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}

.post-title{margin:0 0 8px;font-family:'Unbounded',sans-serif;font-size:17px;font-weight:600;line-height:1.3;color:#fff;word-break:break-word;letter-spacing:-0.02em}
.post-meta{font-size:12px;color:var(--text-dim);margin-bottom:14px;display:flex;gap:12px;flex-wrap:wrap;align-items:center;font-family:'JetBrains Mono',monospace;letter-spacing:0.01em}
.post-body{font-size:14px;line-height:1.65;color:var(--text);white-space:pre-wrap;word-break:break-word}
.post-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px;margin-top:16px}
.post-gallery img{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:10px;cursor:zoom-in;background:var(--input);border:1px solid var(--border);transition:border-color .18s,transform .35s var(--ease)}
.post-gallery img:hover{border-color:var(--border-3);transform:translateY(-2px)}

.share-link{
  margin-top:14px;padding:10px 12px 10px 14px;border-radius:12px;
  background:rgba(255,255,255,0.03);border:1px solid var(--border);
  display:flex;align-items:center;gap:10px;
  font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--text-dim);
  transition:border-color .2s,background .2s;
}
.share-link:hover{border-color:var(--border-2);background:rgba(255,255,255,0.045)}
.share-link-icon{width:15px;height:15px;flex-shrink:0;color:var(--text-mute);display:block}
.share-link-url{flex:1 1 auto;min-width:0;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.share-link-copy{
  flex-shrink:0;width:34px;height:34px;border-radius:9px;
  border:1px solid var(--border);background:var(--input);
  color:var(--text-mute);
  display:grid;place-items:center;cursor:pointer;
  -webkit-appearance:none;appearance:none;
  transition:background .18s,border-color .18s,color .18s;
}
.share-link-copy:hover{background:var(--input-focus);border-color:var(--border-3);color:#fff}
.share-link-copy.copied{color:var(--ok);border-color:rgba(126,200,153,0.5);background:rgba(126,200,153,0.08)}
.share-link-copy svg{width:14px;height:14px;pointer-events:none;display:block}
.share-link-copy .sl-check{display:none}
.share-link-copy.copied .sl-copy{display:none}
.share-link-copy.copied .sl-check{display:block}

.lightbox{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.96);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);user-select:none;-webkit-user-select:none;touch-action:none}
.lightbox[hidden]{display:none}
.lb-viewport{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden;cursor:default;touch-action:none}
.lb-transform{display:flex;align-items:center;justify-content:center;transform-origin:center center;will-change:transform}
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
}
.lb-btn{position:absolute;width:42px;height:42px;border-radius:12px;border:1px solid var(--border-2);background:rgba(15,15,15,0.8);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:var(--text);display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:2;transition:background .18s,border-color .18s}
.lb-btn svg{width:18px;height:18px;pointer-events:none;display:block}
.lb-btn:hover{background:rgba(30,30,30,0.95);border-color:var(--border-3)}
.lb-btn:active{transform:scale(.94)}
.lb-btn[hidden]{display:none}
.lb-close{top:16px;right:16px}
.lb-prev{left:16px;top:50%;transform:translateY(-50%)}
.lb-next{right:16px;top:50%;transform:translateY(-50%)}
.lb-counter{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);padding:7px 16px;border-radius:11px;background:rgba(15,15,15,0.8);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:1px solid var(--border-2);font-size:12.5px;color:var(--text);font-family:'JetBrains Mono',monospace;letter-spacing:.06em;z-index:2;pointer-events:none}
.lb-zoom-badge{position:absolute;top:16px;left:16px;padding:5px 11px;border-radius:10px;background:rgba(15,15,15,0.8);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:1px solid var(--border-2);font-size:11.5px;color:var(--text);font-family:'JetBrains Mono',monospace;z-index:2;pointer-events:none;opacity:0;transition:opacity .18s ease}
.lb-zoom-badge.visible{opacity:1}
.lb-hint{position:absolute;bottom:64px;left:50%;transform:translateX(-50%);font-size:11.5px;color:var(--text-mute);z-index:2;pointer-events:none;white-space:nowrap;font-family:'JetBrains Mono',monospace;letter-spacing:0.02em;text-align:center;padding:0 16px}
.lb-hint-mobile{display:none}

.modal{position:fixed;inset:0;z-index:900;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.72);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);padding:20px;animation:fadeIn .22s ease}
.modal[hidden]{display:none}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.modal-card{
  width:100%;max-width:400px;
  padding:28px 24px 22px;border-radius:20px;
  background:linear-gradient(155deg,rgba(20,20,20,0.95),rgba(12,12,12,0.95));
  border:1px solid var(--border-2);
  box-shadow:0 30px 80px -20px rgba(0,0,0,0.9),inset 0 1px 0 rgba(255,255,255,0.06);
  text-align:center;animation:popIn .32s var(--ease);
}
@keyframes popIn{from{opacity:0;transform:translateY(14px) scale(.96)}to{opacity:1;transform:translateY(0) scale(1)}}
.modal-icon{width:48px;height:48px;margin:0 auto 14px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:rgba(126,200,153,0.1);color:var(--ok);border:1px solid rgba(126,200,153,0.3)}
.modal-icon svg{width:22px;height:22px;display:block}
.modal-title{font-family:'Unbounded',sans-serif;font-size:16px;font-weight:600;margin:0 0 6px;color:#fff;letter-spacing:-0.02em}
.modal-sub{font-size:12.5px;color:var(--text-dim);margin:0 0 18px;line-height:1.5}
.modal-code{font-family:'JetBrains Mono',monospace;font-size:38px;font-weight:600;letter-spacing:12px;text-indent:12px;color:#fff;margin:8px 0 10px}
.modal-url{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--text);padding:10px 12px;border-radius:10px;background:rgba(255,255,255,0.03);border:1px solid var(--border);word-break:break-all;margin-bottom:8px;-webkit-user-select:text;-moz-user-select:text;-ms-user-select:text;user-select:text}
.modal-hint{font-size:11.5px;color:var(--text-mute);margin-bottom:20px;font-family:'JetBrains Mono',monospace;letter-spacing:0.02em}
.modal-actions{display:flex;gap:8px}
.modal-actions .btn{flex:1;height:42px;font-size:13px}

@media (max-width:820px){
  .topbar{padding:9px 10px 9px 14px}
}
@media (max-width:640px){
  .card{padding:15px;border-radius:16px}
  .field{padding:11px 14px;font-size:14px}
  .input-wrap input.field,.input-wrap textarea.field{padding-left:38px}
  .btn{height:44px;font-size:13px;padding:0 14px}
  .row{gap:8px;margin-top:14px}
  .otp-row{gap:8px}
  .otp{gap:5px}
  .modal-card{padding:24px 18px 18px;border-radius:18px}
  .modal-code{font-size:32px;letter-spacing:9px;text-indent:9px}
}
@media (max-width:380px){
  .tab span{display:none}
  .tab{padding:9px 12px}
  .tab svg{width:16px;height:16px}
}

@media (max-width: 720px){
  html, body {
    height: 100%;
    overflow: hidden;
    overscroll-behavior: none;
    background: var(--bg);
  }
  .app {
    position: fixed;
    inset: 0;
    height: 100dvh;
    min-height: 0;
    overflow: hidden;
    padding: calc(var(--topbar-h, 100px) + 12px) 14px calc(18px + env(safe-area-inset-bottom, 0px));
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  .stage {
    width: 100%;
    max-width: 560px;
    max-height: 100%;
    overflow-y: auto;
    overflow-x: hidden;
    -webkit-overflow-scrolling: touch;
    overscroll-behavior: contain;
    scrollbar-width: none;
    margin: auto 0;
    padding: 4px 0;
  }
  .stage::-webkit-scrollbar{ display:none; }
  .topbar{ top: max(10px, env(safe-area-inset-top, 0px) + 6px); width: calc(100% - 20px); padding: 8px 8px 8px 10px; border-radius: 14px; }
  .topbar-inner{ flex-wrap: nowrap; gap: 8px; justify-content: space-between; }
  .topbar .logo,
  .topbar .back-btn{ display: none !important; }
  .topbar .menu{ flex: 1 1 auto; min-width: 0; justify-content: center; padding: 3px; }
  .topbar .menu .tab{ flex: 1 1 0; justify-content: center; padding: 9px 10px; font-size: 12.5px; }
  .topbar .exit-btn{ display: inline-flex; flex-shrink: 0; padding: 9px 13px; font-size: 12.5px; }

  .lb-btn{ width:44px; height:44px; }
  .lb-close{ top:12px; right:12px; }
  .lb-prev, .lb-next{
    top: auto; bottom: 84px; transform: none;
    width: 48px; height: 48px; opacity: .92;
  }
  .lb-prev{ left: 20px; }
  .lb-next{ right: 20px; }
  .lb-counter{ bottom: calc(20px + env(safe-area-inset-bottom, 0px)); font-size: 12px; padding: 6px 14px; }
  .lb-zoom-badge{ top:12px; left:12px; }
  .lb-img{ max-width: 96vw; max-height: 72vh; }
  .lb-hint{ display: none; }
  .lb-hint-mobile{
    display: block; position: absolute; bottom: 60px; left: 50%;
    transform: translateX(-50%);
    font-size: 11px; color: var(--text-mute);
    font-family:'JetBrains Mono',monospace; letter-spacing: 0.02em;
    z-index: 2; pointer-events: none; white-space: nowrap;
    text-align: center; max-width: 92vw; padding: 0 12px;
  }
}
@media (max-width: 480px){
  .topbar .tab span{ display: inline; }
  .topbar .tab svg{ width: 14px; height: 14px; }
  .topbar .tab{ padding: 9px 8px; font-size: 12px; }
  .topbar .exit-btn{ padding: 9px 11px; font-size: 12px; }
}
@media (max-width: 360px){
  .topbar .tab span{ display: none; }
  .topbar .tab{ padding: 10px 12px; }
  .topbar .tab svg{ width: 16px; height: 16px; }
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
<title>СЛД·NET — рабочая область</title>
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
</div>
<div class="cursor-glow"></div>
<div class="grid-bg"></div>
<div class="grain"></div>

<nav class="topbar" id="topbar">
  <div class="topbar-inner">
    <a href="/" class="logo">
      <span class="logo-mark">__LOGO_SVG__</span>
      <span class="logo-word">СЛД<span class="ldot">·</span><span class="lnet">NET</span></span>
    </a>

    <div class="menu" id="menu">
      <span class="menu-pill" id="menuPill" aria-hidden="true"></span>
      <button class="tab" id="btnCreate" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
        <span>Создать</span>
      </button>
      <button class="tab" id="btnFind" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <span>Найти</span>
      </button>
    </div>

    <a href="/" class="back-btn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5M11 6l-6 6 6 6"/></svg>
      <span>На главную</span>
    </a>

    <a href="/" class="exit-btn" aria-label="Выйти на главную">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>
      <span>Выйти</span>
    </a>
  </div>
</nav>

<main class="app">

  <div class="stage" id="stage" hidden>

    <div class="panel" id="createPanel">
      <section class="card">
        <div class="input-wrap">
          <input class="field" id="title" type="text" maxlength="120" autocomplete="off" spellcheck="false" placeholder="Название">
          <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7V5a1 1 0 011-1h14a1 1 0 011 1v2"/><path d="M9 20h6"/><path d="M12 4v16"/></svg>
        </div>

        <div class="input-wrap textarea-wrap">
          <textarea class="field" id="content" placeholder="Содержимое"></textarea>
          <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
        </div>

        <div class="drop" id="drop">
          <svg class="drop-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="9" cy="9" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg>
          <div class="drop-label" id="dropLabel">Нажмите или перетащите фото</div>
          <div class="drop-hint">до 5 фото · до 5 МБ · сжатие до ~60 КБ · Ctrl+V</div>
        </div>
        <input type="file" id="fileInput" accept="image/*" multiple hidden>
        <div class="previews" id="previews"></div>

        <label class="checkbox-wrap" for="ogEnabled">
          <input type="checkbox" id="ogEnabled" checked>
          <span class="checkbox-box">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>
          </span>
          <span class="checkbox-label">
            <strong>Превью для ссылок (OpenGraph)</strong> — при отправке в Telegram или Discord покажет заголовок, краткое описание и первое фото.
          </span>
        </label>

        <div class="row">
          <button class="btn ghost" id="resetBtn" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>
            <span>Очистить</span>
          </button>
          <button class="btn primary" id="submitBtn" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
            <span>Опубликовать</span>
          </button>
        </div>

        <div id="createMsg"></div>
      </section>
    </div>

    <div class="panel" id="findPanel">
      <section class="card">
        <div class="otp-row">
          <div class="otp" id="otp" autocomplete="off">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="1">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="2">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="3">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="4">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="5">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="6">
          </div>
          <button class="otp-copy" id="otpCopyBtn" type="button" disabled title="Скопировать код" aria-label="Скопировать код">
            <svg class="icon-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
            <svg class="icon-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>
          </button>
        </div>
      </section>

      <section class="card" id="searchFrame" hidden></section>
    </div>

  </div>
</main>

<div class="modal" id="createdModal" hidden>
  <div class="modal-card" id="modalCard">
    <div class="modal-icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>
    </div>
    <h3 class="modal-title">Пост создан</h3>
    <p class="modal-sub">Скопируйте ссылку — она развернётся в превью и откроет пост в приложении</p>
    <div class="modal-code" id="modalCode">000000</div>
    <div class="modal-url" id="modalUrl">https://.../p/000000</div>
    <div class="modal-hint" id="modalHint"></div>
    <div class="modal-actions">
      <button class="btn ghost" id="modalCopyBtn" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
        <span>Копировать ссылку</span>
      </button>
      <button class="btn primary" id="modalCloseBtn" type="button">Готово</button>
    </div>
  </div>
</div>

<div class="lightbox" id="lightbox" hidden>
  <div class="lb-viewport" id="lbViewport">
    <div class="lb-transform" id="lbTransform">
      <img class="lb-img" id="lbImg" alt="" draggable="false">
    </div>
  </div>
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

<script>
(function(){
  "use strict";
  var $ = function(id){ return document.getElementById(id); };

  document.addEventListener("contextmenu", function(e){ e.preventDefault(); });

  /* ============ SERVICE WORKER (PWA) ============ */
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function(){
      navigator.serviceWorker.register('/sw.js', { scope: '/' })
        .catch(function(err){ console.warn('SW register failed:', err); });
    });
  }

  /* ============ CURSOR — с «физикой»: кольцо расползается на скорости ============ */
  var dot  = document.querySelector('.cur-dot');
  var ring = document.querySelector('.cur-ring');
  var mx = innerWidth/2, my = innerHeight/2;
  var rx = mx, ry = my, lastX = mx, lastY = my, vel = 0;
  var cursorReady = false;

  addEventListener('mousemove', function(e){
    if (!cursorReady) { cursorReady = true; document.body.classList.add('cursor-ready'); }
    mx = e.clientX; my = e.clientY;
    document.documentElement.style.setProperty('--mx', e.clientX + 'px');
    document.documentElement.style.setProperty('--my', e.clientY + 'px');
  }, { passive: true });

  (function loop(){
    dot.style.transform = 'translate3d(' + mx + 'px,' + my + 'px,0) translate(-50%,-50%)';
    rx += (mx - rx) * 0.26;
    ry += (my - ry) * 0.26;

    var dx = mx - lastX, dy = my - lastY;
    vel = Math.min(Math.hypot(dx, dy), 60);
    lastX = mx; lastY = my;

    var angle = Math.atan2(dy, dx) * 180 / Math.PI;
    var stretch = 1 + vel / 150;
    var squash  = 1 - vel / 220;
    var borderOp = Math.max(0.15, 0.4 - (vel / 60) * 0.25);

    ring.style.transform = 'translate3d(' + rx + 'px,' + ry + 'px,0) translate(-50%,-50%) rotate(' + angle + 'deg) scale(' + stretch + ',' + squash + ')';
    if (!ring.classList.contains('hover')) {
      ring.style.borderColor = 'rgba(255,255,255,' + borderOp.toFixed(2) + ')';
    }
    requestAnimationFrame(loop);
  })();

  addEventListener('mousedown', function(){ ring.classList.add('click'); });
  addEventListener('mouseup', function(){ ring.classList.remove('click'); });
  document.querySelectorAll('a, button, input, textarea, label, .tab, .drop, .post-gallery img, .share-link-copy').forEach(function(el){
    el.addEventListener('mouseenter', function(){ ring.classList.add('hover'); });
    el.addEventListener('mouseleave', function(){ ring.classList.remove('hover'); });
  });

  /* ============ LOADER ============ */
  var loader = document.getElementById('pageLoader');
  if (loader) {
    var hide = function(){ loader.classList.add('hidden'); };
    if (document.readyState === 'complete') setTimeout(hide, 250);
    else { addEventListener('load', function(){ setTimeout(hide, 250); }); setTimeout(hide, 2500); }
  }

  /* ============ TOPBAR HEIGHT ============ */
  var topbar = $('topbar');
  function measureTopbar(){ document.documentElement.style.setProperty('--topbar-h', topbar.offsetHeight + 'px'); }
  addEventListener('resize', measureTopbar, { passive: true });
  addEventListener('orientationchange', function(){ setTimeout(measureTopbar, 300); });
  measureTopbar();

  /* ============ HELPERS ============ */
  var ICONS = {
    error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
    ok:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>'
  };
  function makeMsg(kind, text){
    var d = document.createElement("div");
    d.className = "msg " + kind;
    d.innerHTML = (kind === "err" ? ICONS.error : ICONS.ok);
    var s = document.createElement("span");
    s.textContent = String(text == null ? "" : text);
    d.appendChild(s);
    return d;
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
  function formatBytes(b){
    if (b < 1024) return b + " Б";
    if (b < 1024*1024) return (b/1024).toFixed(1).replace(".", ",") + " КБ";
    if (b < 1024*1024*1024) return (b/(1024*1024)).toFixed(2).replace(".", ",") + " МБ";
    return (b/(1024*1024*1024)).toFixed(2).replace(".", ",") + " ГБ";
  }
  function postUrl(code){ return location.origin + "/p/" + code; }

  function parseLocation(){
    var m = location.pathname.match(/^\/p\/(\d{6})\/?$/);
    if (m) return { code: m[1] };
    var params = new URLSearchParams(location.search);
    var mp = params.get("mode");
    return { code: null, mode: (mp === "find" || mp === "create") ? mp : null };
  }
  function setUrlForCode(code){
    var t = "/p/" + code;
    if (location.pathname !== t) history.replaceState(null, "", t);
  }
  function setUrlForApp(mode){
    var target = "/app" + (mode ? ("?mode=" + mode) : "");
    if (location.pathname + location.search !== target) {
      history.replaceState(null, "", target);
    }
  }

  /* ============ TABS ============ */
  var stage = $("stage"), createPanel = $("createPanel"), findPanel = $("findPanel");
  var btnCreate = $("btnCreate"), btnFind = $("btnFind");
  var menuEl = $("menu"), menuPill = $("menuPill");
  var mode = null;

  function updateMenuPill(){
    if (!menuPill || !menuEl) return;
    var activeTab = mode === "find" ? btnFind : btnCreate;
    var menuRect = menuEl.getBoundingClientRect();
    var tabRect = activeTab.getBoundingClientRect();
    menuPill.style.transform = "translateX(" + (tabRect.left - menuRect.left) + "px)";
    menuPill.style.width = tabRect.width + "px";
  }

  function setMode(next, instant){
    if (next === mode && !instant) return;
    var incoming = next === "create" ? createPanel : findPanel;
    var outgoing = next === "create" ? findPanel : createPanel;
    stage.hidden = false;
    outgoing.classList.remove("active");
    incoming.classList.add("active");
    mode = next;
    btnCreate.classList.toggle("active", next === "create");
    btnFind.classList.toggle("active", next === "find");
    requestAnimationFrame(updateMenuPill);
    setTimeout(updateMenuPill, 60);
    if (next === "create") setTimeout(function(){ $("title").focus(); }, 80);
    else setTimeout(function(){ otpCells[0].focus(); }, 80);
  }

  btnCreate.addEventListener("click", function(){ setUrlForApp("create"); setMode("create"); });
  btnFind.addEventListener("click", function(){ setUrlForApp("find"); setMode("find"); });

  addEventListener("resize", updateMenuPill, { passive: true });
  addEventListener("orientationchange", function(){ setTimeout(updateMenuPill, 300); });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(updateMenuPill);

  /* ============ IMAGE COMPRESSION ============ */
  var TARGET_PHOTO_BYTES = 60 * 1024;
  var SOURCE_MAX_BYTES = 5 * 1024 * 1024;
  async function compressImage(file, targetBytes){
    if (targetBytes === undefined) targetBytes = TARGET_PHOTO_BYTES;
    if (file.type.indexOf("image/") !== 0) return file;
    try {
      var bitmap = await createImageBitmap(file);
      var maxDim = 1600, best = null;
      for (var attempt = 0; attempt < 3; attempt++) {
        var scale = Math.min(1, maxDim / Math.max(bitmap.width, bitmap.height));
        var w = Math.max(1, Math.round(bitmap.width * scale));
        var h = Math.max(1, Math.round(bitmap.height * scale));
        var canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        var ctx = canvas.getContext("2d");
        ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
        ctx.drawImage(bitmap, 0, 0, w, h);
        var lo = 0.35, hi = 0.92, candidate = null;
        for (var i = 0; i < 7; i++) {
          var q = (lo + hi) / 2;
          var blob = await new Promise(function(r){ canvas.toBlob(r, "image/jpeg", q); });
          if (!blob) break;
          if (blob.size <= targetBytes) { candidate = blob; lo = q; } else { hi = q; }
        }
        if (candidate) { best = candidate; break; }
        var fallback = await new Promise(function(r){ canvas.toBlob(r, "image/jpeg", 0.5); });
        if (fallback) best = fallback;
        maxDim = Math.round(maxDim * 0.72);
        if (maxDim < 500) break;
      }
      if (bitmap.close) bitmap.close();
      if (!best) return file;
      return new File([best], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
    } catch (e) {
      console.warn("compress failed:", e);
      return file;
    }
  }

  /* ============ CREATE ============ */
  var MAX_PHOTOS = 5;
  var selectedFiles = [];
  var drop = $("drop"), dropLabel = $("dropLabel"), fileInput = $("fileInput");
  var previews = $("previews"), createMsg = $("createMsg"), submitBtn = $("submitBtn");
  var titleInput = $("title"), contentInput = $("content"), ogCheckbox = $("ogEnabled");

  function defaultDropLabel(){
    return selectedFiles.length
      ? "Выбрано: " + selectedFiles.length + " / " + MAX_PHOTOS
      : "Нажмите или перетащите фото";
  }
  drop.addEventListener("click", function(){ fileInput.click(); });
  drop.addEventListener("dragover", function(e){ e.preventDefault(); drop.style.borderColor = "var(--border-3)"; });
  drop.addEventListener("dragleave", function(){ drop.style.borderColor = ""; });
  drop.addEventListener("drop", function(e){
    e.preventDefault();
    drop.style.borderColor = "";
    addFiles(Array.from(e.dataTransfer.files || []));
  });
  fileInput.addEventListener("change", function(){
    addFiles(Array.from(fileInput.files || []));
    fileInput.value = "";
  });
  document.addEventListener("paste", function(e){
    if (mode !== "create") return;
    var items = (e.clipboardData || window.clipboardData) && (e.clipboardData || window.clipboardData).items;
    if (!items) return;
    var files = [];
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      if (item.kind === "file" && item.type.indexOf("image/") === 0) {
        var f = item.getAsFile();
        if (f) {
          var ext = (f.type.split("/")[1] || "png").replace("jpeg", "jpg");
          files.push(new File([f], "pasted_" + Date.now() + "_" + (files.length + 1) + "." + ext, { type: f.type }));
        }
      }
    }
    if (files.length) { e.preventDefault(); addFiles(files); }
  });

  async function addFiles(list){
    var rejected = 0, accepted = [];
    for (var i = 0; i < list.length; i++) {
      var f = list[i];
      if (f.type.indexOf("image/") !== 0) { rejected++; continue; }
      if (f.size > SOURCE_MAX_BYTES) { rejected++; continue; }
      if (selectedFiles.length + accepted.length >= MAX_PHOTOS) { rejected++; continue; }
      accepted.push(f);
    }
    if (rejected > 0) showCreateMsg("err", "Пропущено: " + rejected + ". Только изображения, не больше " + MAX_PHOTOS + " и не тяжелее 5 МБ.");
    else clearCreateMsg();
    if (!accepted.length) return;

    drop.classList.add("busy");
    dropLabel.textContent = "Сжимаем фото...";
    try {
      var compressed = await Promise.all(accepted.map(function(f){ return compressImage(f); }));
      for (var j = 0; j < compressed.length; j++) if (compressed[j]) selectedFiles.push(compressed[j]);
      renderPreviews();
    } finally {
      drop.classList.remove("busy");
      dropLabel.textContent = defaultDropLabel();
    }
  }

  function renderPreviews(){
    previews.innerHTML = "";
    selectedFiles.forEach(function(file, index){
      var url = URL.createObjectURL(file);
      var box = document.createElement("div");
      box.className = "preview";
      var img = document.createElement("img");
      img.src = url; img.alt = file.name;
      img.addEventListener("load", function(){ URL.revokeObjectURL(url); }, { once: true });
      var kb = Math.max(1, Math.round(file.size / 1024));
      var badge = document.createElement("div");
      badge.className = "pv-badge"; badge.textContent = kb + " КБ";
      var rm = document.createElement("button");
      rm.type = "button"; rm.textContent = "×"; rm.title = "×";
      rm.addEventListener("click", function(){
        selectedFiles.splice(index, 1);
        renderPreviews();
      });
      box.append(img, badge, rm);
      previews.appendChild(box);
    });
    drop.classList.toggle("filled", selectedFiles.length > 0);
    dropLabel.textContent = defaultDropLabel();
  }

  function showCreateMsg(kind, text){
    createMsg.innerHTML = "";
    createMsg.appendChild(makeMsg(kind, text));
  }
  function clearCreateMsg(){ createMsg.innerHTML = ""; }

  $("resetBtn").addEventListener("click", function(){
    titleInput.value = ""; contentInput.value = ""; selectedFiles = []; ogCheckbox.checked = true;
    renderPreviews(); clearCreateMsg(); titleInput.focus();
  });

  submitBtn.addEventListener("click", async function(){
    var title = titleInput.value.trim();
    var content = contentInput.value.trim();
    if (!title) { showCreateMsg("err", "Введите название поста."); titleInput.focus(); return; }
    var fd = new FormData();
    fd.append("title", title);
    fd.append("content", content);
    fd.append("og_enabled", ogCheckbox.checked ? "true" : "false");
    selectedFiles.forEach(function(f){ fd.append("files", f, f.name); });

    submitBtn.disabled = true;
    var oldHTML = submitBtn.innerHTML;
    submitBtn.textContent = "Публикация...";
    clearCreateMsg();

    try {
      var res = await fetch("/api/posts", { method: "POST", body: fd });
      if (!res.ok) { showCreateMsg("err", await readError(res)); return; }
      var data = await res.json().catch(function(){ return null; });
      if (!data || !data.code) { showCreateMsg("err", "Некорректный ответ сервера"); return; }

      titleInput.value = ""; contentInput.value = ""; ogCheckbox.checked = true;
      selectedFiles = []; renderPreviews(); clearCreateMsg();

      var code = data.code;
      setUrlForCode(code);
      setMode("find");
      otpCells.forEach(function(c, i){ c.value = code[i] || ""; });
      lastSubmitted = code;
      updateOtpCopyState();
      runSearch(code);
      showCreatedModal(code, data.compressed_bytes, data.share_url || postUrl(code));
    } catch (e) {
      showCreateMsg("err", "Ошибка сети: " + e.message);
    } finally {
      submitBtn.disabled = false;
      submitBtn.innerHTML = oldHTML;
    }
  });

  /* ============ MODAL ============ */
  var createdModal = $("createdModal"), modalCard = $("modalCard"), modalCode = $("modalCode");
  var modalUrl = $("modalUrl"), modalHint = $("modalHint"), modalCopyBtn = $("modalCopyBtn");
  var modalCloseBtn = $("modalCloseBtn");
  var modalCopyTimer = null, modalShareUrl = "";

  function showCreatedModal(code, bytes, shareUrl){
    modalCode.textContent = code;
    modalShareUrl = shareUrl || postUrl(code);
    modalUrl.textContent = modalShareUrl;
    modalHint.textContent = "Занято в памяти: " + formatBytes(bytes);
    var label = modalCopyBtn.querySelector("span");
    if (label) label.textContent = "Копировать ссылку";
    createdModal.hidden = false;
  }
  function closeCreatedModal(){ createdModal.hidden = true; }

  modalCopyBtn.addEventListener("click", async function(){
    var label = modalCopyBtn.querySelector("span");
    try { await navigator.clipboard.writeText(modalShareUrl); if (label) label.textContent = "Скопировано"; }
    catch { if (label) label.textContent = "Ошибка"; }
    clearTimeout(modalCopyTimer);
    modalCopyTimer = setTimeout(function(){ if (label) label.textContent = "Копировать ссылку"; }, 1500);
  });
  modalCloseBtn.addEventListener("click", closeCreatedModal);
  createdModal.addEventListener("click", function(e){ if (e.target === createdModal) closeCreatedModal(); });
  modalCard.addEventListener("click", function(e){ e.stopPropagation(); });

  /* ============ SEARCH ============ */
  var otp = $("otp");
  var otpCells = Array.prototype.slice.call(document.querySelectorAll(".otp-cell"));
  var otpCopyBtn = $("otpCopyBtn"), searchFrame = $("searchFrame");
  var searchSeq = 0, lastSubmitted = "", otpCopyTimer = null;

  function getCode(){ return otpCells.map(function(c){ return c.value; }).join(""); }
  function updateOtpCopyState(){ otpCopyBtn.disabled = getCode().length !== 6; }
  function clearOtp(){
    otpCells.forEach(function(c){ c.value = ""; });
    otpCells[0].focus();
    lastSubmitted = "";
    updateOtpCopyState();
  }
  function shakeOtp(){
    otp.classList.remove("shake"); void otp.offsetWidth; otp.classList.add("shake");
    setTimeout(function(){ otp.classList.remove("shake"); }, 400);
  }
  function hideSearchFrame(){ searchFrame.hidden = true; searchFrame.innerHTML = ""; }
  function maybeSearch(){
    var code = getCode();
    if (code.length === 6) {
      setUrlForCode(code);
      if (code === lastSubmitted) return;
      lastSubmitted = code;
      runSearch(code);
    } else {
      searchSeq++;
      hideSearchFrame();
      lastSubmitted = "";
      if (location.pathname !== "/app") setUrlForApp(mode || "find");
    }
    updateOtpCopyState();
  }
  otpCopyBtn.addEventListener("click", async function(){
    var code = getCode();
    if (code.length !== 6) return;
    try {
      await navigator.clipboard.writeText(code);
      otpCopyBtn.classList.add("copied");
      clearTimeout(otpCopyTimer);
      otpCopyTimer = setTimeout(function(){ otpCopyBtn.classList.remove("copied"); }, 1500);
    } catch(e){}
  });
  otpCells.forEach(function(cell, i){
    cell.addEventListener("focus", function(){ cell.select(); });
    cell.addEventListener("input", function(e){
      var v = (e.target.value || "").replace(/\D/g, "");
      if (!v) { e.target.value = ""; maybeSearch(); return; }
      e.target.value = v.slice(-1);
      if (i < otpCells.length - 1) otpCells[i + 1].focus();
      maybeSearch();
    });
    cell.addEventListener("keydown", function(e){
      if (e.key === "Backspace") {
        if (!cell.value && i > 0) { otpCells[i - 1].value = ""; otpCells[i - 1].focus(); e.preventDefault(); }
        setTimeout(maybeSearch, 0);
      } else if (e.key === "ArrowLeft" && i > 0) { otpCells[i - 1].focus(); e.preventDefault(); }
      else if (e.key === "ArrowRight" && i < otpCells.length - 1) { otpCells[i + 1].focus(); e.preventDefault(); }
      else if (e.key === "Enter") { var c = getCode(); if (c.length === 6) { lastSubmitted = c; runSearch(c); } }
    });
    cell.addEventListener("paste", function(e){
      e.preventDefault();
      var text = (e.clipboardData || window.clipboardData).getData("text") || "";
      var digits = text.replace(/\D/g, "").slice(0, 6).split("");
      digits.forEach(function(d, j){ if (otpCells[j]) otpCells[j].value = d; });
      otpCells[Math.min(digits.length, otpCells.length - 1)].focus();
      maybeSearch();
    });
  });
  updateOtpCopyState();

  function showSearchFrame(){ searchFrame.hidden = false; }
  function renderSpinner(){
    searchFrame.innerHTML = '<div class="center"><div class="spinner"></div><div class="spinner-label">Ищем пост...</div></div>';
    showSearchFrame();
  }
  function renderError(text){
    searchFrame.innerHTML = "";
    searchFrame.appendChild(makeMsg("err", text));
    showSearchFrame();
  }

  /* ============ LIGHTBOX ============ */
  var lightbox = $("lightbox"), lbViewport = $("lbViewport"), lbTransform = $("lbTransform");
  var lbImg = $("lbImg"), lbCounter = $("lbCounter"), lbPrev = $("lbPrev"), lbNext = $("lbNext");
  var lbClose = $("lbClose"), lbZoomBadge = $("lbZoomBadge");
  var lbCode = null, lbPhotos = [], lbIndex = 0;
  var zoom = 1, panX = 0, panY = 0;
  var MIN_ZOOM = 1, MAX_ZOOM = 8;
  var isPanning = false, panStartX = 0, panStartY = 0, badgeTimer = null;

  function applyTransform(){
    lbTransform.style.transform = "translate(" + panX + "px," + panY + "px) scale(" + zoom + ")";
    lbViewport.style.cursor = (zoom > 1.001) ? (isPanning ? "grabbing" : "grab") : "default";
  }
  function showZoomBadge(){
    lbZoomBadge.textContent = Math.round(zoom * 100) + "%";
    lbZoomBadge.classList.add("visible");
    clearTimeout(badgeTimer);
    badgeTimer = setTimeout(function(){ lbZoomBadge.classList.remove("visible"); }, 900);
  }
  function resetZoom(animate){
    if (animate) lbTransform.style.transition = "transform .2s ease";
    zoom = 1; panX = 0; panY = 0; applyTransform();
    if (animate) setTimeout(function(){ lbTransform.style.transition = ""; }, 220);
    showZoomBadge();
  }
  function zoomAt(clientX, clientY, newZoom){
    newZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, newZoom));
    if (Math.abs(newZoom - zoom) < 1e-4) return;
    var vrect = lbViewport.getBoundingClientRect();
    var Cx = vrect.left + vrect.width / 2, Cy = vrect.top + vrect.height / 2;
    var dcx = clientX - Cx, dcy = clientY - Cy;
    var ratio = newZoom / zoom;
    panX = dcx - (dcx - panX) * ratio;
    panY = dcy - (dcy - panY) * ratio;
    zoom = newZoom;
    lbTransform.style.transition = "";
    applyTransform(); showZoomBadge();
  }
  function openLightbox(code, photos, index){
    lbCode = code; lbPhotos = photos; lbIndex = index;
    zoom = 1; panX = 0; panY = 0; lbTransform.style.transition = ""; applyTransform();
    lbImg.src = "/api/photos/" + encodeURIComponent(code) + "/" + index;
    lbImg.alt = (photos[index] && photos[index].name) || "";
    lbCounter.textContent = (index + 1) + " / " + photos.length;
    lbPrev.hidden = photos.length < 2; lbNext.hidden = photos.length < 2;
    lightbox.hidden = false;
  }
  function closeLightbox(){ lightbox.hidden = true; lbImg.removeAttribute("src"); lbPhotos = []; lbCode = null; }
  function lbStep(dir){
    if (lbPhotos.length < 2) return;
    lbIndex = (lbIndex + dir + lbPhotos.length) % lbPhotos.length;
    zoom = 1; panX = 0; panY = 0; lbTransform.style.transition = ""; applyTransform();
    lbImg.src = "/api/photos/" + encodeURIComponent(lbCode) + "/" + lbIndex;
    lbImg.alt = (lbPhotos[lbIndex] && lbPhotos[lbIndex].name) || "";
    lbCounter.textContent = (lbIndex + 1) + " / " + lbPhotos.length;
  }
  lbPrev.addEventListener("click", function(e){ e.stopPropagation(); lbStep(-1); });
  lbNext.addEventListener("click", function(e){ e.stopPropagation(); lbStep(1); });
  lbClose.addEventListener("click", function(e){ e.stopPropagation(); closeLightbox(); });
  lbViewport.addEventListener("click", function(e){ if (e.target === lbViewport && zoom <= 1.001) closeLightbox(); });
  lbViewport.addEventListener("wheel", function(e){
    e.preventDefault();
    var factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
    zoomAt(e.clientX, e.clientY, zoom * factor);
  }, { passive: false });
  lbViewport.addEventListener("mousedown", function(e){
    if (e.button === 2) { e.preventDefault(); if (zoom > 1.05) resetZoom(true); else zoomAt(e.clientX, e.clientY, 2); return; }
    if (e.button === 0 && zoom > 1.001) {
      e.preventDefault();
      isPanning = true;
      panStartX = e.clientX - panX; panStartY = e.clientY - panY;
      lbTransform.style.transition = "none";
      lbViewport.style.cursor = "grabbing";
    }
  });
  lbViewport.addEventListener("dblclick", function(e){
    e.preventDefault();
    if (zoom > 1.05) resetZoom(true); else zoomAt(e.clientX, e.clientY, 2);
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

  var touchStartDist = 0, touchStartZoom = 1;
  var tStartX = 0, tStartY = 0, tStartTime = 0;
  var tActive = false, tPinch = false;
  var lastTapTime = 0, lastTapX = 0, lastTapY = 0;
  var lastPanX = 0, lastPanY = 0;

  lbViewport.addEventListener("touchstart", function(e){
    if (e.touches.length === 2) {
      touchStartDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      touchStartZoom = zoom;
      tPinch = true;
      tActive = false;
    } else if (e.touches.length === 1) {
      tStartX = e.touches[0].clientX;
      tStartY = e.touches[0].clientY;
      tStartTime = Date.now();
      tActive = true;
      tPinch = false;
      lastPanX = e.touches[0].clientX;
      lastPanY = e.touches[0].clientY;
    }
  }, { passive: true });

  lbViewport.addEventListener("touchmove", function(e){
    if (e.touches.length === 2 && tPinch && touchStartDist > 0) {
      e.preventDefault();
      var dist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      var cx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
      var cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      zoomAt(cx, cy, touchStartZoom * (dist / touchStartDist));
    } else if (e.touches.length === 1 && zoom > 1.001 && !tPinch) {
      var dx = e.touches[0].clientX - lastPanX;
      var dy = e.touches[0].clientY - lastPanY;
      panX += dx;
      panY += dy;
      lastPanX = e.touches[0].clientX;
      lastPanY = e.touches[0].clientY;
      applyTransform();
    }
  }, { passive: false });

  lbViewport.addEventListener("touchend", function(e){
    if (tPinch) {
      tPinch = false;
      touchStartDist = 0;
      return;
    }
    if (!tActive) return;
    tActive = false;
    if (!e.changedTouches.length) return;
    var t = e.changedTouches[0];
    var dx = t.clientX - tStartX;
    var dy = t.clientY - tStartY;
    var dt = Date.now() - tStartTime;
    var dist = Math.hypot(dx, dy);

    if (zoom <= 1.05 && dt < 600 && dist > 40) {
      if (Math.abs(dx) > Math.abs(dy) * 1.2 && Math.abs(dx) > 55) {
        if (dx < 0) lbStep(1); else lbStep(-1);
        return;
      }
      if (dy > 90 && Math.abs(dy) > Math.abs(dx)) {
        closeLightbox();
        return;
      }
    }
    if (dt < 260 && dist < 12) {
      var now = Date.now();
      if (now - lastTapTime < 320 &&
          Math.abs(t.clientX - lastTapX) < 44 &&
          Math.abs(t.clientY - lastTapY) < 44) {
        if (zoom > 1.05) resetZoom(true);
        else zoomAt(t.clientX, t.clientY, 2.4);
        lastTapTime = 0;
      } else {
        lastTapTime = now;
        lastTapX = t.clientX;
        lastTapY = t.clientY;
      }
    }
  }, { passive: true });

  /* ============ POST RENDER ============ */
  function renderPost(post){
    searchFrame.innerHTML = "";
    var title = document.createElement("h3");
    title.className = "post-title"; title.textContent = post.title;
    var meta = document.createElement("div");
    meta.className = "post-meta";
    var date = document.createElement("span");
    try { date.textContent = new Date(post.created).toLocaleString("ru-RU"); } catch(e){}
    meta.appendChild(date);
    if (post.photos && post.photos.length) {
      var cnt = document.createElement("span");
      cnt.textContent = "Фото: " + post.photos.length;
      meta.appendChild(cnt);
    }
    searchFrame.append(title, meta);
    if (post.content) {
      var body = document.createElement("div");
      body.className = "post-body"; body.textContent = post.content;
      searchFrame.appendChild(body);
    }
    if (post.photos && post.photos.length) {
      var gallery = document.createElement("div");
      gallery.className = "post-gallery";
      post.photos.forEach(function(p, idx){
        var img = document.createElement("img");
        img.src = "/api/photos/" + encodeURIComponent(post.code) + "/" + idx;
        img.alt = p.name || "photo"; img.title = p.name || "";
        img.loading = "lazy"; img.decoding = "async";
        img.addEventListener("click", function(){ openLightbox(post.code, post.photos, idx); });
        gallery.appendChild(img);
      });
      searchFrame.appendChild(gallery);
    }
    var shareBox = document.createElement("div");
    shareBox.className = "share-link";
    var shareIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    shareIcon.setAttribute("viewBox", "0 0 24 24");
    shareIcon.setAttribute("fill", "none");
    shareIcon.setAttribute("stroke", "currentColor");
    shareIcon.setAttribute("stroke-width", "1.9");
    shareIcon.setAttribute("stroke-linecap", "round");
    shareIcon.setAttribute("stroke-linejoin", "round");
    shareIcon.setAttribute("aria-hidden", "true");
    shareIcon.setAttribute("class", "share-link-icon");
    shareIcon.innerHTML = '<path d="M10 13a5 5 0 007.07 0l3-3a5 5 0 00-7.07-7.07l-1.5 1.5"/><path d="M14 11a5 5 0 00-7.07 0l-3 3a5 5 0 007.07 7.07l1.5-1.5"/>';
    var shareSpan = document.createElement("span");
    shareSpan.className = "share-link-url";
    var url = postUrl(post.code);
    shareSpan.textContent = url; shareSpan.title = url;
    var copyBtn = document.createElement("button");
    copyBtn.type = "button"; copyBtn.className = "share-link-copy";
    copyBtn.title = "Копировать ссылку";
    copyBtn.setAttribute("aria-label", "Копировать ссылку");
    copyBtn.innerHTML = '<svg class="sl-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg><svg class="sl-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>';
    var copyTimer = null;
    copyBtn.addEventListener("click", async function(e){
      e.stopPropagation();
      try { await navigator.clipboard.writeText(url); copyBtn.classList.add("copied"); }
      catch (err) { copyBtn.classList.remove("copied"); }
      clearTimeout(copyTimer);
      copyTimer = setTimeout(function(){ copyBtn.classList.remove("copied"); }, 1500);
    });
    shareBox.append(shareIcon, shareSpan, copyBtn);
    searchFrame.appendChild(shareBox);
    showSearchFrame();
  }

  async function runSearch(code){
    var mySeq = ++searchSeq;
    renderSpinner();
    await new Promise(function(r){ setTimeout(r, 340); });
    if (mySeq !== searchSeq) return;
    try {
      var res = await fetch("/api/posts/" + encodeURIComponent(code));
      if (mySeq !== searchSeq) return;
      if (!res.ok) {
        renderError((await readError(res)) || "Пост не найден");
        shakeOtp(); setTimeout(clearOtp, 320);
        return;
      }
      var post = await res.json().catch(function(){ return null; });
      if (mySeq !== searchSeq) return;
      if (!post) { renderError("Пост не найден"); return; }
      renderPost(post);
    } catch (e) {
      if (mySeq !== searchSeq) return;
      renderError("Ошибка сети: " + e.message);
      shakeOtp(); setTimeout(clearOtp, 320);
    }
  }

  /* ============ INIT ============ */
  function initFromUrl(){
    var r = parseLocation();
    if (r.code) {
      setMode("find", true);
      otpCells.forEach(function(c, i){ c.value = r.code[i] || ""; });
      lastSubmitted = r.code;
      updateOtpCopyState();
      runSearch(r.code);
    } else if (r.mode === "find") {
      setMode("find", true);
    } else if (r.mode === "create") {
      setMode("create", true);
    } else {
      setMode("create", true);
    }
    requestAnimationFrame(updateMenuPill);
    setTimeout(updateMenuPill, 120);
  }
  initFromUrl();

  document.addEventListener("keydown", function(e){
    if (!lightbox.hidden) {
      if (e.key === "Escape") { closeLightbox(); return; }
      if (e.key === "ArrowLeft")  { lbStep(-1); return; }
      if (e.key === "ArrowRight") { lbStep(1);  return; }
      if (e.key === "0") { resetZoom(true); return; }
      return;
    }
    if (!createdModal.hidden && e.key === "Escape") { closeCreatedModal(); return; }
  });
})();
</script>
</body>
</html>
"""


def _build_og_tags(code: str, meta: dict, photos_count: int, base: str) -> str:
    title = str(meta.get("title", "")).strip() or f"Пост #{code}"
    content_raw = str(meta.get("content", "")).strip()
    if content_raw:
        desc = content_raw.replace("\n", " ").strip()
        if len(desc) > 180:
            desc = desc[:177].rstrip() + "…"
    else:
        desc = f"Пост #{code} в СЛД·NET. Откройте, чтобы прочитать полностью."

    title_esc = _html.escape(title, quote=True)
    desc_esc = _html.escape(desc, quote=True)
    url = f"{base}/p/{code}"

    lines = [
        '<meta property="og:type" content="article">',
        '<meta property="og:site_name" content="СЛД·NET">',
        f'<meta property="og:title" content="{title_esc}">',
        f'<meta property="og:description" content="{desc_esc}">',
        f'<meta property="og:url" content="{_html.escape(url, quote=True)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{title_esc}">',
        f'<meta name="twitter:description" content="{desc_esc}">',
    ]
    if photos_count > 0:
        img = f"{base}/api/photos/{code}/0"
        img_esc = _html.escape(img, quote=True)
        lines.append(f'<meta property="og:image" content="{img_esc}">')
        lines.append(f'<meta property="og:image:alt" content="{title_esc}">')
        lines.append(f'<meta name="twitter:image" content="{img_esc}">')
    lines.append(f'<link rel="canonical" href="{_html.escape(url, quote=True)}">')
    return "\n".join(lines)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(build_landing())


@app.get("/app", response_class=HTMLResponse)
async def app_page():
    html = (APP
            .replace("__FAVICON__", FAVICON)
            .replace("__LOGO_SVG__", LOGO_SVG)
            .replace("__APP_CSS__", APP_CSS)
            .replace("<!-- OG_TAGS -->", ""))
    return HTMLResponse(html)


@app.get("/p/{code}", response_class=HTMLResponse)
async def post_page(code: str, request: Request):
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        html = (APP
                .replace("__FAVICON__", FAVICON)
                .replace("__LOGO_SVG__", LOGO_SVG)
                .replace("__APP_CSS__", APP_CSS)
                .replace("<!-- OG_TAGS -->", ""))
        return HTMLResponse(html)

    og_tags = ""
    title_override = None
    with _lock:
        entry = _store.get(code)
    if entry is not None:
        try:
            meta = _unpack_meta(entry["meta"])
            if meta.get("og_enabled", True):
                og_tags = _build_og_tags(code, meta, len(entry["photos"]), _base_url(request))
                t = str(meta.get("title", "")).strip()
                if t:
                    title_override = t
        except Exception:
            pass

    html = (APP
            .replace("__FAVICON__", FAVICON)
            .replace("__LOGO_SVG__", LOGO_SVG)
            .replace("__APP_CSS__", APP_CSS)
            .replace("<!-- OG_TAGS -->", og_tags))

    if title_override:
        html = html.replace(
            "<title>СЛД·NET — рабочая область</title>",
            f"<title>{_html.escape(title_override, quote=True)} — СЛД·NET</title>",
        )
    return HTMLResponse(html)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
