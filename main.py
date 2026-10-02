"""
Sld-Networking — посты с 6-значным кодом (без повторяющихся цифр).
Хранение: оперативная память, AES-256-GCM + zstd/gzip.

Главная страница содержит панораму скриншотов интерфейса: кадры сняты
в headless-браузере и вшиты в HTML как data:image/webp;base64 —
без внешних файлов, статики и CDN (см. PANORAMA_IMAGES).

Маршруты:
  /           — лендинг (включая панораму скриншотов)
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
import hashlib
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
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sld")

# ---------- сжатие ----------
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


# ---------- шифрование ----------
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


# ---------- память ----------
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


def _etag_for_post(code: str, entry: dict) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(code.encode("ascii"))
    h.update(entry["meta"])
    h.update(str(len(entry["photos"])).encode("ascii"))
    for p in entry["photos"]:
        h.update(p["name"].encode("utf-8", "ignore"))
        h.update(str(p["size"]).encode("ascii"))
    return '"' + h.hexdigest() + '"'


def _etag_for_photo(code: str, idx: int, entry: dict) -> str:
    h = hashlib.blake2b(digest_size=12)
    h.update(code.encode("ascii"))
    h.update(str(idx).encode("ascii"))
    h.update(entry["photos"][idx]["enc"][:64])
    return '"' + h.hexdigest() + '"'


app = FastAPI(title="Sld-Networking", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=500)


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
    return JSONResponse(
        content={
            "code": code,
            "compressed_bytes": total,
            "photos": len(photos),
            "share_url": f"{base}/p/{code}",
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/posts/{code}")
async def get_post(code: str, request: Request):
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(400, "Код должен содержать 6 цифр")

    with _lock:
        entry = _store.get(code)
    if entry is None:
        raise HTTPException(404, "Пост не найден")

    etag = _etag_for_post(code, entry)
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "private, max-age=300, must-revalidate",
            },
        )

    try:
        meta = _unpack_meta(entry["meta"])
    except (InvalidTag, ValueError, OSError) as e:
        raise HTTPException(500, f"Ошибка расшифровки: {e}")

    meta["code"] = code
    meta["photos"] = [
        {"idx": i, "name": p["name"], "mime": p["mime"], "size": p["size"]}
        for i, p in enumerate(entry["photos"])
    ]

    return JSONResponse(
        content=meta,
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=300, must-revalidate",
        },
    )


@app.get("/api/photos/{code}/{idx}")
async def get_photo(code: str, idx: int, request: Request):
    with _lock:
        entry = _store.get(code)
    if entry is None:
        raise HTTPException(404, "Пост не найден")

    photos = entry["photos"]
    if idx < 0 or idx >= len(photos):
        raise HTTPException(404, "Фото не найдено")

    p = photos[idx]

    etag = _etag_for_photo(code, idx, entry)
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    try:
        data = _decrypt(p["enc"])
    except (InvalidTag, ValueError) as e:
        raise HTTPException(500, f"Ошибка расшифровки: {e}")

    return Response(
        content=data,
        media_type=p["mime"],
        headers={
            "ETag": etag,
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
                return JSONResponse(
                    content={"code": code},
                    headers={"Cache-Control": "no-store"},
                )
    raise HTTPException(503, "Хранилище переполнено")


@app.get("/api/stats")
async def stats():
    with _lock:
        return {"posts": len(_store)}


# ============================================================
# PWA
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
    "name": "СЛД·NET",
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
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "maskable"},
    ],
}

PWA_SW = r"""
const CACHE = 'sld-net-v7';
const SHELL_URLS = ['/app', '/icon.svg'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL_URLS).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;

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
        } else if (k === 'ref' && typeof v === 'function') {
          v(el);
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
  function frag(){
    var f = document.createDocumentFragment();
    append(f, Array.prototype.slice.call(arguments));
    return f;
  }
  function on(el, evt, fn, opts){ el.addEventListener(evt, fn, opts); return el; }
  function off(el, evt, fn, opts){ el.removeEventListener(evt, fn, opts); return el; }
  function addClass(el){ for (var i = 1; i < arguments.length; i++) el.classList.add(arguments[i]); return el; }
  function removeClass(el){ for (var i = 1; i < arguments.length; i++) el.classList.remove(arguments[i]); return el; }
  function toggle(el, cls, force){ el.classList.toggle(cls, force); return el; }
  function hasClass(el, cls){ return el.classList.contains(cls); }
  function attr(el, name, val){
    if (val === undefined) return el.getAttribute(name);
    if (val == null) el.removeAttribute(name); else el.setAttribute(name, val);
    return el;
  }
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
    h: h, svg: svg, html: html, frag: frag, append: append,
    on: on, off: off,
    addClass: addClass, removeClass: removeClass, toggle: toggle, hasClass: hasClass,
    attr: attr, text: text, clear: clear,
    show: show, hide: hide,
    qs: qs, qsa: qsa,
    state: state, toast: toast, copy: copy,
    debounce: debounce, throttle: throttle
  };
})();
"""


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

SEARCH_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>'
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
    '.008-.145.03-1.52.03-1.67.002-.512.167-3.63-.024-5.545z"/></svg>'
)

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
  --ok:#7ec899;
  --warn:#c4a054;
  --err:#e08080;
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
/* Разрешено выделение только там, где это реально нужно (поля ввода и код в модалке) */
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

/* ============================================================
   КНОПКА «НАВЕРХ» — только иконка, появляется при скролле
   ============================================================ */
.scroll-top{
  position:fixed;
  right:20px;
  bottom:20px;
  bottom:max(20px, env(safe-area-inset-bottom, 0px) + 12px);
  width:44px;height:44px;
  border-radius:12px;
  border:1px solid var(--border-2);
  background:rgba(15,15,15,0.85);
  backdrop-filter:blur(16px) saturate(160%);
  -webkit-backdrop-filter:blur(16px) saturate(160%);
  color:var(--text);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;
  z-index:250;
  opacity:0;
  transform:translateY(10px);
  pointer-events:none;
  transition:opacity .25s ease, transform .25s var(--ease),
             background .18s, border-color .18s, color .18s;
  -webkit-appearance:none;appearance:none;
  box-shadow:0 12px 32px -12px rgba(0,0,0,0.7), inset 0 1px 0 rgba(255,255,255,0.06);
}
.scroll-top.visible{ opacity:1; transform:translateY(0); pointer-events:auto; }
.scroll-top:hover{ background:rgba(25,25,25,0.95); border-color:var(--border-3); }
.scroll-top:active{ transform:scale(.94); }
.scroll-top svg{ width:18px; height:18px; pointer-events:none; display:block; }

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
.nav-links a.active{color:#fff;background:rgba(255,255,255,0.06)}

.nav-right{display:flex;align-items:center;flex-shrink:0;}
body > nav.top-nav .nav-extra{
  max-width:220px;opacity:1;overflow:hidden;white-space:nowrap;margin-right:10px;
  transition:max-width .55s var(--ease),margin-right .55s var(--ease),
             opacity .3s ease,padding .55s var(--ease),height .55s var(--ease),
             font-size .55s var(--ease),border-radius .55s var(--ease),
             background .28s,border-color .28s,color .28s,box-shadow .28s;
}
body > nav.top-nav.scrolled .nav-extra{
  max-width:0;margin-right:0;opacity:0;padding-left:0;padding-right:0;pointer-events:none;
}

body > nav.top-nav .logo{font-size:17px;transition:font-size .55s var(--ease);}
body > nav.top-nav .logo-mark{
  width:40px;height:40px;border-radius:12px;
  transition:width .55s var(--ease),height .55s var(--ease),border-radius .55s var(--ease),transform .35s var(--ease);
}
body > nav.top-nav .logo-mark svg{width:20px;height:20px;transition:width .55s var(--ease),height .55s var(--ease);}
body > nav.top-nav .nav-links a{
  padding:12px 20px;font-size:15px;border-radius:13px;
  transition:color .2s,background .2s,padding .55s var(--ease),font-size .55s var(--ease),border-radius .55s var(--ease);
}
body > nav.top-nav .btn{
  height:50px;padding:0 26px;font-size:14.5px;border-radius:14px;
  transition:background .28s,border-color .28s,color .28s,box-shadow .28s,
             height .55s var(--ease),padding .55s var(--ease),
             font-size .55s var(--ease),border-radius .55s var(--ease);
}
body > nav.top-nav .btn svg{width:17px;height:17px;transition:transform .35s var(--ease),width .55s var(--ease),height .55s var(--ease);}

body > nav.top-nav.scrolled .logo{font-size:15px;}
body > nav.top-nav.scrolled .logo-mark{width:32px;height:32px;border-radius:10px;}
body > nav.top-nav.scrolled .logo-mark svg{width:16px;height:16px;}
body > nav.top-nav.scrolled .nav-links a{padding:9px 15px;font-size:13.5px;border-radius:11px;}
body > nav.top-nav.scrolled .btn{height:44px;padding:0 20px;font-size:13.5px;border-radius:12px;}
body > nav.top-nav.scrolled .btn svg{width:15px;height:15px;}

.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
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
  display:flex;justify-content:space-between;align-items:flex-start;
  gap:40px;flex-wrap:wrap;margin-bottom:54px;
}
.foot-brand{max-width:360px}
.foot-brand .logo{margin-bottom:18px}
.foot-brand p{color:var(--text-mute);font-size:13.5px;line-height:1.7}
.foot-brand .dev{
  display:inline-flex;align-items:center;gap:8px;
  margin-top:14px;padding:7px 12px 7px 10px;border-radius:100px;
  background:rgba(255,255,255,0.04);border:1px solid var(--border);
  font-family:'JetBrains Mono',monospace;
  font-size:11px;letter-spacing:0.06em;color:var(--text-dim);
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
  gap:24px;flex-wrap:wrap;padding-top:30px;border-top:1px solid var(--border);
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
.foot-socials a:hover{background:var(--glass-hi);border-color:var(--border-3);color:#fff;}
.foot-socials svg{width:18px;height:18px;display:block}

.reveal{
  opacity:0;
  transition:opacity .8s var(--ease),transform .8s var(--ease),filter .8s var(--ease);
  will-change:opacity,transform,filter;
}
.reveal.in{opacity:1;transform:none;filter:none;}
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
  body > nav.top-nav .top-nav-inner{padding:16px 20px;gap:12px;}
  body > nav.top-nav.scrolled{padding:10px;}
  body > nav.top-nav.scrolled .top-nav-inner{padding:9px 10px 9px 14px;border-radius:14px;}
  .logo{font-size:13.5px;}
  .logo-mark{width:28px;height:28px;}
  .logo-mark svg{width:14px;height:14px;}
  body > nav.top-nav .logo{font-size:15px;}
  body > nav.top-nav .logo-mark{width:34px;height:34px;border-radius:10px;}
  body > nav.top-nav .logo-mark svg{width:17px;height:17px;}
  body > nav.top-nav .btn{height:44px;padding:0 18px;font-size:13px;}
  body > nav.top-nav .nav-extra{display:none;}
  body > nav.top-nav.scrolled .logo{font-size:13.5px;}
  body > nav.top-nav.scrolled .logo-mark{width:28px;height:28px;border-radius:8px;}
  body > nav.top-nav.scrolled .logo-mark svg{width:14px;height:14px;}
  body > nav.top-nav.scrolled .btn{height:38px;padding:0 14px;font-size:12.5px;}
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

    document.querySelectorAll('a, button, .glass, .code-cell, .stat, .mock, label.checkbox-wrap, input, textarea, .tab, .drop').forEach(function(el){
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
    var SCROLL_THRESHOLD = 80;
    var applyState = function(){
      nav.classList.toggle("scrolled", (window.scrollY || window.pageYOffset || 0) > SCROLL_THRESHOLD);
    };
    applyState();
    addEventListener("scroll", function(){
      if (!ticking) {
        requestAnimationFrame(function(){ applyState(); ticking = false; });
        ticking = true;
      }
    }, { passive: true });
    addEventListener("resize", applyState, { passive: true });
  }

  /* ============================================================
     КНОПКА «НАВЕРХ»
     Работает и для window-скролла (десктоп), и для внутреннего
     скролла .stage (мобильный).
     ============================================================ */
  var scrollTopBtn = document.getElementById("scrollTopBtn");
  if (scrollTopBtn) {
    var stageEl = document.getElementById("stage");
    var SCROLL_SHOW_AT = 240;

    function currentScrollTop(){
      var a = window.scrollY || window.pageYOffset || 0;
      var b = stageEl ? stageEl.scrollTop : 0;
      return Math.max(a, b);
    }
    function applyScrollTopBtn(){
      scrollTopBtn.classList.toggle("visible", currentScrollTop() > SCROLL_SHOW_AT);
    }
    applyScrollTopBtn();

    var stTicking = false;
    function onAnyScroll(){
      if (stTicking) return;
      stTicking = true;
      requestAnimationFrame(function(){ applyScrollTopBtn(); stTicking = false; });
    }
    addEventListener("scroll", onAnyScroll, { passive: true });
    if (stageEl) stageEl.addEventListener("scroll", onAnyScroll, { passive: true });
    addEventListener("resize", applyScrollTopBtn, { passive: true });

    scrollTopBtn.addEventListener("click", function(){
      try { window.scrollTo({ top: 0, behavior: "smooth" }); } catch(e){ window.scrollTo(0, 0); }
      if (stageEl && stageEl.scrollTop > 0) {
        try { stageEl.scrollTo({ top: 0, behavior: "smooth" }); } catch(e){ stageEl.scrollTop = 0; }
      }
      // Мгновенно пересчитать состояние (не ждать smooth-скролла)
      setTimeout(applyScrollTopBtn, 60);
    });
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
      scrollTo({ top: tg.getBoundingClientRect().top + scrollY - 110, behavior: "smooth" });
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
    '          <a href="/#panorama">Скриншоты</a>\n'
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


def render_shell(
    title: str,
    body: str,
    extra_css: str = "",
    og: str = "",
    active: str = "",
    extra_js: str = "",
) -> str:
    nav_links = [
        ("/", "Главная"),
        ("/#panorama", "Скриншоты"),
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
        '  <div class="top-nav-inner">\n'
        '    <a href="/" class="logo">\n'
        '      <span class="logo-mark">' + LOGO_SVG + '</span>\n'
        '      <span class="logo-word">СЛД<span class="ldot">·</span><span class="lnet">NET</span></span>\n'
        '    </a>\n'
        '    <div class="nav-links">' + links_html + '</div>\n'
        '    <div class="nav-right">\n'
        '      <a href="/app?mode=find" class="btn btn-ghost nav-extra">\n'
        + SEARCH_SVG + '<span>Найти</span>\n'
        '      </a>\n'
        '      <a href="/app?mode=create" class="btn btn-primary">\n'
        '        <span>Открыть приложение</span>' + ARROW_SVG + '\n'
        '      </a>\n'
        '    </div>\n'
        '  </div>\n'
        '</nav>\n'
        + body +
        FOOTER_HTML +
        '<button class="scroll-top" id="scrollTopBtn" type="button" aria-label="Наверх">\n'
        '  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">\n'
        '    <path d="M12 19V5M5 12l7-7 7 7"/>\n'
        '  </svg>\n'
        '</button>\n'
        '<script>\n' + UI_FRAMEWORK_JS + '\n' + SHELL_JS + '\n</script>\n'
        + (('<script>\n' + extra_js + '\n</script>\n') if extra_js else '') +
        '</body>\n</html>'
    )


# ============================================================
# ПАНОРАМА СКРИНШОТОВ
# ------------------------------------------------------------
# Каждый кадр — настоящий скриншот приложения, снятый в headless-
# браузере и вшитый в файл как data:image/webp;base64. Никаких
# внешних файлов, запросов и CDN: картинки живут прямо в HTML.
# ============================================================

PANORAMA_IMAGES: List[Dict[str, object]] = [
    # ==== PANORAMA_DATA:BEGIN ====
    {
        "id": "01-landing-hero",
        "cat": "landing",
        "title": "Главная · первый экран",
        "caption": "Заголовок, живой код и призывы к действию",
        "w": 1120,
        "h": 700,
        "bytes": 20364,
        "data": (
            "data:image/webp;base64,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"
            "AAAAAAA"
        ),
    },
    {
        "id": "02-landing-code",
        "cat": "landing",
        "title": "Код из шести цифр",
        "caption": "Живая генерация кода без повторяющихся цифр",
        "w": 1120,
        "h": 700,
        "bytes": 20398,
        "data": (
            "data:image/webp;base64,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"
        ),
    },
    {
        "id": "03-landing-features",
        "cat": "landing",
        "title": "Возможности · публикация",
        "caption": "Плитки bento с эффектами наведения",
        "w": 1120,
        "h": 700,
        "bytes": 20144,
        "data": (
            "data:image/webp;base64,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"
            "AA="
        ),
    },
    {
        "id": "04-landing-bento",
        "cat": "landing",
        "title": "Технологии и хранение",
        "caption": "AES-256-GCM, zstd/gzip, хранение в RAM",
        "w": 1120,
        "h": 700,
        "bytes": 22874,
        "data": (
            "data:image/webp;base64,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"
        ),
    },
    {
        "id": "05-landing-showcase",
        "cat": "landing",
        "title": "Как выглядит пост",
        "caption": "Карточка поста рядом с описанием",
        "w": 1120,
        "h": 700,
        "bytes": 23794,
        "data": (
            "data:image/webp;base64,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"
        ),
    },
    {
        "id": "06-landing-stats",
        "cat": "landing",
        "title": "Цифры сервиса",
        "caption": "Кодов, символов, лимиты и сжатие",
        "w": 1120,
        "h": 700,
        "bytes": 20676,
        "data": (
            "data:image/webp;base64,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"
            "2xv27nv6/BHtAGLyqnSvufbaLoFltUAAAAAAAAA"
        ),
    },
    {
        "id": "07-landing-how",
        "cat": "landing",
        "title": "Как это работает",
        "caption": "Три шага от идеи до кода",
        "w": 1120,
        "h": 700,
        "bytes": 22144,
        "data": (
            "data:image/webp;base64,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"
        ),
    },
    {
        "id": "08-landing-specs",
        "cat": "landing",
        "title": "Технологии",
        "caption": "Шифрование, сжатие, память, OpenGraph",
        "w": 1120,
        "h": 700,
        "bytes": 18030,
        "data": (
            "data:image/webp;base64,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"
        ),
    },
    {
        "id": "09-landing-cta",
        "cat": "landing",
        "title": "Призыв к действию",
        "caption": "Финальный блок с переходом в приложение",
        "w": 1120,
        "h": 700,
        "bytes": 14808,
        "data": (
            "data:image/webp;base64,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"
        ),
    },
    {
        "id": "10-landing-footer",
        "cat": "landing",
        "title": "Подвал",
        "caption": "Ссылки, контакты и соцсети",
        "w": 1120,
        "h": 700,
        "bytes": 12316,
        "data": (
            "data:image/webp;base64,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"
            "pCdwRL530YhuCPHnYMXgAAAAAAAAA=="
        ),
    },
    {
        "id": "12-app-create-empty",
        "cat": "app",
        "title": "Создание поста",
        "caption": "Пустая форма: название, текст, фото",
        "w": 1120,
        "h": 700,
        "bytes": 9986,
        "data": (
            "data:image/webp;base64,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"
        ),
    },
    {
        "id": "13-app-create-filled",
        "cat": "app",
        "title": "Форма заполнена",
        "caption": "Автосжатие фото прямо в браузере",
        "w": 1120,
        "h": 700,
        "bytes": 15986,
        "data": (
            "data:image/webp;base64,UklGRmo+AABXRUJQVlA4IF4+AAAwjwGdASpgBLwCPolEn0ulJCMlIbVIaKARCWlu9qFgI8vwZ"
            "a734nMdeIvzWJ8yNt/3Glv3DXPAab3/Jem7vvn0J/cfyS8GP8J/ef2q/v/qP+J/Nv4P8uv7f7Ymdvru1F/lv3a/X/4X93fWr"
            "/K/4r91P8T6I/kn6l/qf7v+TvyBfjH8x/0X9q9bv5Psmcz/2H/j/w/sEenfzX/j/5X8o/cX9b/z3989TPy3+x/6v+/fjb9gH"
            "8d/o3+3/w34+/Jf+H/3Pip/Z/9j7AP8z/sP/F/xX+s/cL6T/5n/0f6L82Paz+ef5r/2f6j/VfIL/N/7V/4f8f20fSaCkbWCI"
            "cyQvgz7yqcydgW0ATAIdVD4AmwTNjFsEzMis843mA7RuZT8SFRJ5OCFy2op2BbRqhBlQO7vAD7jeX+dnW8vimweuPVI8titD"
            "MzSxKI0FhzMjLp43KvoS0RFDn3t5x1bGysqmwn+buozSq0E//9ARl08bck0WlJ6kztSmWqYXMNS+P+tNzg75bkOI97ZH5V8C"
            "xj2CnZIhQN8Eqj2Jiz2lSDCZZr7MDxpwGSNaYv0w/+chA3qe8ZKWrJAOagVnT/wdHdMmhzTLNgCw4QWaWF/ctCAAQUK0H09N"
            "CSWyo9DwbYv0xG8lVM+eJsCs+SzonUNq3r8jNYXcMDUBDy+P3NbxKP5hXlsTC/XqUZ7w6seesHaZnaYoqjdh4GQ+gGM6PfUd"
            "v2FWJ/SWcSzOOrsOi83+m5Wbd6tdEZ2NwTNjFpb2XrYaNRCe6H2l6U6e+zw5QLPWQgfa7+KzAaHCD1NG8glVRvIPEqbyD1CJ"
            "tMVQpvIPU0byD1Mpwg8K7k/mFeW8eAIz3BsoQGCQ2VIDJODZQgMEhsrFsqQGSY8Wk9icBknAYJDZUgMkx4sRGihAXnAZJ3tU"
            "QgguptOkCgY3KwRkYMtol08blYIy6eNysEZdPG5WCMuiDkIafNjOpBQNsX6Yfyo/lR/Kj+VH8qP5Ufyo/lR/Kj+aaEDkIG2L"
            "9MRBCkDkInF+VH8qP5Uj+qaN5B6mjeQepo3kHqZzKHIQNsX6Yfyo/lSIIUgchA2xfph/Kj/8hu2q/JLvCC2BVSBd9G950fRv"
            "eRqV3sIxR/Kj+VH8qP5UgARMNMYBV4G2L9M/jrRBxm30zsWbGKf4QULyD1NFwg2Lk1cueQgciNZmQOQgiefqYv/OQgbYwBmx"
            "MgeXRpFfx4A4ToK5OUVJsqEFIjgE2JNZ3sAU6jxweapXg6ft5WQEag8xoRHAImggAf+AbvfPpTKJrdl9FuQ4OBXxxWYRSYPY"
            "nBj/KC5xALcq+FpMDWunQLxvXQirW+NieYhYCAO2EDkInF+VaeQQ46Bti/VtCByKdEG4RI0r9SpuS/UqbyD1NG8g3a0U6s+S"
            "mL9MP5Ufyo/lUq4QochA3dyo/lSS1eT9TYG2VNrdLgOQdYYVyEXY4m1vNHMruP18ARTtBmFlGWymWU9AOQcMjJVtSK01tSY9"
            "L5qUjzIzk5BrRSP+0EwbZVXdbUH0ExaPwTCqBwEx3tHCEofw6nWdMX6Yfyo/lR/KpVwhQ5CBti/TD+VfoDS+UiQtKyAqpZGN"
            "AS+gXIC5uAHIGLtBsaCf4WJa7QPFUXI+P23URKjPbvew7hfFPDe2LoRoYHoARy32U9nwbg+fYBwPrFoxdYpIHCv370AcFWJU"
            "xsOoBr7w0eL1pR8JzabgLwx0biLM0/SCnTDyd5CByEDbF+mH8qlXCFDkIG2L9MP/pcwc9Ojl9cY9Jy6sKR/2pEOhXDFxXkCD"
            "GZbV++AKV3P/0YK/zr+GBeFUmwL7pngOKRMF75AVbtGQRQYEJDCfSzQmXpmQyKU9lPuq3l+o6ITP8bJoy93AekYP/8vRSBA6"
            "/TF+mH8qP5UfzCvLYQOQgbYv0w/nDeZLGJ8PX1NDlz9riGJ5qfpAjTWLB9fJonlrPmwgDT2sEZdRwbk/lR/Kj+VH8qP9K/nk"
            "IHIQNsX6YgDe/mbCAFuVgjLp43KyHVqaaEDkIG2L9MP9K/nkIHIQNsX6torSr4whJCUDurKzzLUDI5KQwypgPFQucbQrGEYD"
            "+51n5hV2PaWC3Xe6TidRCyv//V9XbXaAO8QcQpCTIVlRViEA/9UXgXBRcJeXcPU2Tj3eMDnNsuUQJ2NFYv0NdABiqfMb1G9h"
            "r1HhyKATnmjPFRxyXOi1YV/IxbN1nMqHIQNsX6Yfyo/0r+eQgchA2xfpiAN7a2gciMXyV2E1KcDBRuing5h8ZcFcjsVDr4IN"
            "x1JErthiKIarfjIjT7IIBblTs9burAbAQxclDUJ2iMIsyGwgchA2xfph/KkQRBKYv0zMgchA20hsM0MllKsRNpfqUTYr/xaz"
            "0b3nR6m7yCOZEiMUMg5CBu7lR/Kj+arXxA5wUxfph/KkAb2nl5qtG8X9SfRMLPaRiVabwaJF/nOINh7alR/Kj+VH8qP5VKuE"
            "KHIQNsX6Yfyr9Agf/0BGWwBeZp1YySfCXs3/QEZdHazvyUxfpiN446Bti/ydE8hA5CBti/TEAb38zYQAs5vmhZ+sKZ1Hk4SN"
            "psv0M3XyQEBV/MZDJzsJJZaKP/rUPSPpi/TD+VH8qP5hXlvHTF+mH/zkIJDacZkZa9Cyztx8S6Vaa4aBUuNyhACAFBSJZL8T"
            "XvKyHVr5yEDbF+mH8qP9K/nkIHIQNsX6YgDe/mbB/2IXAUC+PECOOHkRMmENL74C5DRDIP/E8qRCJnaEoVd3WL3lZDq1Kj+V"
            "H8qP5UfyqVcIUOQgh/YUOQgkNm4Z3KvlLEYKUEeFheEcW9l6R9endPv5yrc6Z+dH0b3nRgL41V9DYQOQicX5Ufyo/0r+eQgc"
            "hA2xfpiAN7H190x9WJfdphE/101VJmqt0aNhKKmrfqoNynxcjpDjgA82eEPq+3Painl3SvxFlCE+MIASTYMeQgchA2xfph/M"
            "K8thA5CBti/TEb8vIsMSvaBcwssuAZQWXXdm/DBYJxnWA2kpyNy6cXfKEeSnIQNsX6Yfyo/lUq76OOgbYz+3MNNIa72/MuJP"
            "ShvZ/8zFYm5McRkDV+HsEpicnK16U6y4Cva/dSsAwTE2jUf/nHh4A5aw/lR/Kj+VH8q08ghx41iN446BtjT1SFT7k55hPFwG"
            "kKV0lhIlC85Uwu8Y4ZEND8eozL9ZFz6ea7iRnq5KbARVOK9Hqo0F8urgf0a8wx21X6rgIaILB9US9pAXDYRSQNsX6Yfyo/0r"
            "+eQgchA2xgDNitKooqhWoYsBSwmW3BmjvIPUJxgGWXwHr9k/lR/NNCByEDbLeZlR/Kj+VH8qP5w3qwtmyTRTacHi1MXD2bJX"
            "UiXP9RSAD93m4qWtFKCXmSD6tniyAAmDZrfZo1q9najOfj1uR4iTtOWpWEwAhPsm0kYEaS90iGP8nhr4BJSDAU0Xe3Hg80Bm"
            "Ol5cWdp/6BKvIoZj9kGW6KBEGE5mot3CX3gXYcDiK6M7DeuQRQBlay3254LHEJ3bmGmL9MP5UfypEEKkXMNMX6tuOmNPWIXx"
            "OSCAC+Xz7DARAqF1u3c4FZBuIcxCSYMBmvaxjdz4NwjI68Hz66VuA1BqPseyMLas2cVDbvUOeXeLIr5438w4o/lR/KsyByED"
            "cx2fph/Kj+VH8qQBvewJ3RP7roO4wC6DuMAupR9FLtC4S+APaJR/Kj+VH8qP5UiCFIHIQNsX6YfypJaoIXI9qdrsxWYK+u2z"
            "fomk/4EMKTGL9NG9UluDOFFEHyLJJWiRcX5Ufyo/lR/Kj/Sv55CByEDbF+mIA3v5mcK1XQyD9Nf4PZA0Scr4PVpIWqXvY7Pw"
            "qNSIIFjDuzat7HtBzCf5sPH4BDKxZd/Xp3+AUlGeg8XNYl5HXUOo5KbdS7U1vROQgbYv0zMgchA+13X6Yfyo/lR/KklhsGIW"
            "08X5Zfxa/aBK2tRh8UCuXxH7jW7AU4H6aN6moT5Fo/TT7q4ivjeYQ2qS8G1oaYv0w/lR/Kj+YV5bCByEDbGANTCQt7S4wkmT"
            "BG3s98WBqJwYUSpcIPUyrlSHthA5CBti/TD+VH+lfzyEDkIG2L9MP5QtgrDYpsIAYurBGXRFUeuT+VH8qP5Ufyo/0r+eRIua"
            "gbmIO2EDyEfqy0UgC3KwRl046iUkDd3Kkbxx0DbF/k6J5CByEDbF+mH8qSWjxuVgjLp43KwOVxxR/Kj+VH8qP5UfzCvLYQOQ"
            "gbYv0w/lR/KAyo/lR/Kj+VH8qP5Ufyo/lR/Kj+VH8qP5VmRSjWH/znCHbEi74rMgjcA7eOmL9W0IHIQN3cqP5UjeRr9MXgAA"
            "P79dRprVOXEZ7SFcGsl8LYgSALk9Boe7Tf+ilcwgOcCnMONDNaHfMuOI4gtl2ZlOAWJ6Z6zYNPhlwDTPr9nUOQ/ltBrFGMTF"
            "X2nOTk8b7rFvJJGoD0462v+kDSL/YvnC3R3i4coPGfCtqiTZDilLFnCN67irR07RdW9xU2AMAcp9RPx8v5Ib0Kt5gX0RXFWt"
            "O7DsGzXtLCBYEM6Talx/cgR0+t87xF64h0llAzTO6QrEHLvIOCGP3PxZkiuG5er9An7yaeB0iEJlK75H5zwJGCIJSxjpBAp3"
            "D9h3TjXulegJyNxKuCbj2+H8SYl/ksm/AnbmcfETu/cfvK0i9/Tkl/OqT+LeDtdWCh6H9xJc526EIp/yjWq3gyP+679P3nd7"
            "aVCl5hTVIeBRbIl7jqNVkE7Z9P8XA4c/+fWt9wzAqjz8NvnT4GEdq0pZUYe71q/hm/Y4bYjjU/pSKP9l9iHCvn3kdWWDcjQK"
            "uQAdtt+wkKGar+7RVRQatItA6Z9N0eY9NR07hgRu7keRAVLgB0hwA0FJj6EqFDSaoS4aWoCvAhtfNJJs0YEQ+cDjTbPGEWQo"
            "bZ7/wfNcukMAtAwOS9ji9f2nmlCZ8CI8T9rfRwZvcZ/IVMD2GcZJsan+FNn1GNCbQgU+iQ3rliunE9V1ZSc2evVQf9mYpX4w"
            "Lw0o1+DcIit/8FD+jrqwPkwDpQLyjNUR3BHH/RQJME4nT7JWuE5wGUUQ4jplRnJs0cI48K2+qSgbRUoG6/O3MZUH5VxVcfpE"
            "eidcDKUIODucY2/A2g1GqNS5b0bU/MscAapw0L7k+fvfHyRVIGWHp/xfV62vTq8gMmNLi8bbt+RVc8YmqjLXJ6NXmnLpZeaw"
            "l+PdMKvZKnDQ2ddd4yDajDgRRfil39ANm1U/sWNkPtWTKV6+F3aRMtP34/tymlIz3alnCBAlL5N7T71BhBIclX4R0oh5phbC"
            "BXEeanSlGLYPGyJbl8CURqfC0wem805AluMcEteSRO+Lqv+i4NCXU+gbTEDfDJ0i1mrrHOccZ5MDtGUBRYUgzdE9Ly0xN74A"
            "ZAz+vN/zaZP+ByJY0kV/D623JywBqlhJVbSQosA8MG8Tn4f/VGCro9NSAFBiCjj2jGCp0H/ccGKyvocgMGk5i34RHFUpWDp/"
            "GQaFlBWJYJ49rNJyyJDrkLPjs7T+B+7Mhtj4/OWqM3b1u3J8IKq9DXjSuYCS0YVCttnwWVDKYJDIo5NDv+wRhhZdwNrBA3kS"
            "ZVu562t9NvfWOax+yLF2eNeSoNuJOOrSawNnsr4moAnfg+mTCrwelLYqcDNU6tJy8S7hV5+HwNflkhisRxx69bJwvHUE4Hoo"
            "0aeM4bYSj8Ga6ztu8J3fxdWCl24TIcr72gYQWTSsNBuq11UY8AiLFLqeQ7woSmRgpYYsyYDL3H3vIpa1XvXUMW4d+0jpSb5D"
            "YS3YknlRNEpjAs9Xru9/ETYQtriMEaoeCbT8EVIRMmrJVWBiD5aoCxDZTzOHjGs5U9veKxGHH3SS1TVkCuklrPTFZjhhBENL"
            "mfPKvIQzGIKRCTCXDh0y6TgbzPMz7O4dIudMst2YWGpwOWgBrCZs1AFKRe2lGSVFtPIfkjF+Xyh3aWTgLfhuSpqVAWEbDiqV"
            "yqX7ELkAHY9rL0Su4LrYxLJDzSpVDaDclTFyrbYFnaLIUAAAAIQAAAIHQAAAAAAAYpgABviH2IlJBxZApmHnD2axxSlW1+iu"
            "ixgrlLvzKLXTm7suDNEHObftgOTAJiFXal9xKKcjKKDqGmRZ/Q7wGzjsbaiP+763LzUPk1aR6ALtZRAqWXcu5gsJrOapdbHn"
            "/Jj7AqEwkKNbsPAAu1QxDu2XWYLLBSFAbiR5P15jDxdvm188rOrp187lEpOhk+eHTnr7l1/GmnKBJ5zCMzWI58wImL/ZwWQv"
            "bVlSwhY3/m0Q3Rt1g3Uz1uuH1wstReIv9UWvlSU3eJm9cHP1J0XEY7sWyLNdctmb4FaZ7TTnb1Ecw8Nf/A22zjLvDBvOJYEf"
            "rUNdNm5UyrfgPFiiE0Zn+docjHy0/UJKESJ1GPxli6dhGrcODHGx2Myz5UvKOjsWW9lRq082x82FoSUvkmLUcdGK6/VlfwAb"
            "Q9DhSnE34Qew6dv2Wb5ytGeUYsEv6uIiYKROBMMeaOmcQBZ13y3OUGFV5Ai6pN9FjVqNwV3ZztBuAoQO3jiWHPExalo3FiLt"
            "K2DLskvyCZXXBlTmm9FKln9Fii+LDzgvCummJMSMi9/DgVzXZNlSwA6jZcrBCzx4XH16jW5tg1Y128bleEp3vmjTUlzlI2pk"
            "YzE7GIfxZ+0Cqz/Fi8UModHk2dctYTC0PWLjHveZcz9iJYN+dVuePug9Ww/BaI1OP5ataZb3JZ30PznQNm2oIqfh9SJ5wYgu"
            "8N2rEAvFZq7j/kHWBDLSkzwjqz0UQ+rl4viI+NqF4/bIXHE/SlWyjoYYzlceHHc7suIvu/3xmuN+xTEo4pSTgVOt13mDDLcm"
            "0O3m1YVoETKamI5JYK+VLRGbNCxCAKi4p/wIqYzxV3IphRVQz5IFU1SiVHDIEdVa5ZMsVK1j2z66dBLkLbCMqjBj3nlUKL6f"
            "WBaSO0WiiP78GVpxmwPCXhrl4qjZs4mGsn72mC9DKQveTphDI4xjsxQKfgLN+b1HSsKsnfs7XBoPFFJ6RLXGgc/Psc1QT+w6"
            "nCg+kUvZA1slp0oHreZXhD2OsnEieFNfFq+NHpxLdkow14a5rNr0vIY15tA4yryymTBRF5Er5kdw5MqyDdhqDwQ1m3FQwltZ"
            "SKFfBbaiZw2mcfI/iS4/jjiLnEyKY4IAUymDSFy9zoXCQthXoRBYzBax8jTXx/sAClswS/rppEUWTfTbGezwtEwp7h1tYerY"
            "f3IweJ48qC+A2GLr+A4bLzHcVeJ73tsdwCMsqt26h0TLs4gOUdh15bpA2nLEbDSks89SHp81GCdQ/67waLtHGlC27pso6VW7"
            "u32+fADUXQwv013E/6brKEnpK3e2Zqlk/MD9EwNgnj6saNsqUWuLtubV5StOaqUASywuz+cVtiNVQvhUYcYE08DGJuYeBHzD"
            "PF1mhzmWeFdxhLlzjjv96/yNa0FHPAAGM8BaG6ebZkVrWdVzCH9xw0UEV1zQwA+MhzVAyR81xHtJzN0f2G3GENhVuTeM/bYv"
            "e4JWKZW2Vfoi+FCYCv2TGXL5Vx4cbRBDsmSqAyumPtteM1R2fGKlrZ2gr5mqiNaDeLcIkJd9wpPdd75/nQWVD9j4Cn5ZnPIZ"
            "wyWvF4rpZX9J6rGsF3Cy/r0J3sxHmJuKL9LRBayvMYj/Jf5B1vov1qWyXOB097U/j1NLbKNdsXX/4+gHsT/Zj1k1xiKIjUtQ"
            "kSXiaAWnGX6O2p4AdjfR+ImrfN5ts98igSEfgJlBVByY3/aizi5p1oJRv9B0dlgTUoZhImikQheshojycucpvIWKyyyweBR4"
            "qi+euVJ9pKT+wlG/0c40bQP7f8iUwybN+kzFoMeFKF4m5HZ/KKMzQNhpPb+yasWHjjmNzuivTKUpiJTxPNHRdoDlsvaplpeH"
            "gxitoExv1duXh5iuBjxMLuDEbs4toG+iFFoPUxdbcOXR7WO469XIkx4mYbxdGsdEQeHVAGbBBKeJ8Yg9HV5H8wOEO32rYj/U"
            "4vqU0DiX9LjVV+0ybxaRsz7gdCDhksoXdBoqNd+5KFP7FBsn0FdQ2PQcZK6wQvjYBu9e/2VjUR657VuABoafz7g6yKDbcywC"
            "JDozhZba0LQmpNTcLHpJNhhYu2Vx6SA8OGPUgPLZjIw8PDxVX8UL1tqxzxP1IbanWKeeAQTEZFmoPS3eSCm/0n/4remxIAJf"
            "T/doQPY/L0VnJR2o6ZLp20Ik9RwXUegpee8b90Uy/3SzAZ5S1jo32gSv7kpZcz37Q9YGx4X25cbykZ857oSBBe0YwnqvB/6E"
            "YkBbNSJh6DH47ZQYpxXpXnAv8IBT5YICIIo7wC5vt5NjkUKrd14GnymRvuqarQgEDowKZEgFrmEPbK9/+2/e6WskhMRQGSy9"
            "g3f80aX3+uWn5SauutxfsE/bHTzN2HN1x07NzMFML01o/mrqSposmrqRzClpwKsdW8wPO+G4Maa8536JGfkXjSI7iR7Wfy2r"
            "Jk2y4avJxGWPqT9aHzLdZa8rlf8hay9FK6j45rufn/1HPX57/lxm6GNBm9y+MgMgPnMTSaWdxpjASHCERZ26ioSXxsDCGipa"
            "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"
            "AAAFxAAAAAAAiwAAAAAAAAAAAA="
        ),
    },
    {
        "id": "14-app-create-success",
        "cat": "app",
        "title": "Пост опубликован",
        "caption": "Модальное окно с кодом и ссылкой",
        "w": 1120,
        "h": 700,
        "bytes": 8040,
        "data": (
            "data:image/webp;base64,UklGRmAfAABXRUJQVlA4IFQfAABQMAGdASpgBLwCPolEnUwlI6MoIJDoUQARCWlu4V4cNJfcL"
            "/+k/6B23f5fxN/GvnH8H+ZP9z9rXOv13alnyr7u/p/7Z5x/7zwF+Dn9t6gX5H/LP8dvRek/7D9gPYC9Vvp3/N/xXiw/0PoR9"
            "ZP+H/fPgA/kv9R/4Xqz/pP+Z4ov4L/OewB/SP7l/3v8x7rH9b/8/9b58f0P/P/+j/WfAN/Nv7H/3f8Z22vSTBSU5cuXLly5c"
            "uXLly5cuXLl/7/Lly5cuXLlIhp5zNmvUYY6QWpemviFLNPP6Do7KbDvcxLlTLcqZblTtWZkAt/oUo0bVLtupdt1vWlgTtD9w"
            "q2CnU9GjsOMCiYGrFnaslD3+e5GaL0GSSKBQKBQKBNS1RLdH7VpFV1I5Ql23WwU7tBn/CQ7brYelwqFugHUbd8/qCAgIZ7Eq"
            "EhooerVAGhQXcy1i3TZuVv4FdbmXlj9Nypms5tgukgkET3hoECC1apQlv9MMhkCPeQ3Wc+LmwlvpnhR26AyhSi7bVIaLqXbd"
            "S8/DfRwwUHEo7ViQ7qQ0fO3UnDBQeDw1gqWSdSg9FLs/2kunToZ1K3mpatguaJ1hjwjSdM3yn+bUW7lOGy1G5dwk30f3KmW5"
            "Uy3KmW5Uy3KmW5Uy3KmW5Uy3KmW5Uy3KmW5Uy3K4TBfDcqZblTLcqZblTLcqZblTLcqZblTLcqZblTLcqZblTLcrgugMqZbl"
            "TLcqZblTLcqZblTLcqZblTLcqZblTLcqZblTLcqZbn5uVMtypluVMtypluVMtypluVMtypluVMtypluVMtypluVMtyuC6l23"
            "Uu26l23Uu26l23Uu26l23Uu26l23Uu26l23Uu26l23U18u3KmW5Uy3KmW5XBdIkC8iH3WqUOQyGQyGQsoNrQqZduVMtypluV"
            "Mtyplufm5Uy3KmW5Uy3K53x/zcDkTpZFytsfE+5TlwuyvO6l23Uu26l5+G5+blTLcqZblTLcr6MoaG6J1Qjm4PzTTqGHiP3K"
            "gtS4pcKZLtypluVMtyplxR23Uu26l23Uu3A2EXyDh27ZcSsAcBX0B/RAewBs2dZyV6b6Q01LcqZblTLcqZbn5uVMtypluVMt"
            "0EMkiTDHG8Gh2OAhE1C2fZuY9SZG9FIupdt1Ltupdt+mVMtypluVMtyp6ZAaR1JGJaeHCXXuUYvj/+TAylC+sUx2gSvvKkdw"
            "QJhkaTXcDon86HG01qPtJF2wbX41LIy4udopGCpdt1Ltupdt1NfLtypluVMtyplyT4QSqszIvkImrKDcnSSe9em48B+lQ+iO"
            "arV4aDeSG4SrkbSSX9xVYpEMaYjmQtq9QDMOi/ay4D5iZBNxXQmE/VPsFEhkXoWmvp6VsQa5D1D8jrHs+owy5lrN0Mvrlx0Y"
            "FdS3KmW5Uy3KmW5+blTLcqZblTLcr6AX7tx7aAJSSEIpuEgOqlSR+rgBaq7m+EuB9BhpGOr+yQIZKo0MAvxsjrsRyTLcqZbl"
            "TLcqZcUdt1Ltupdt1LuGnFHOgLlbP7gta/yb0jfU0FDbqXbdS7bqXcI7lTLcqZblTLdGdBboeRTX13kaFhwyT5EIpIcF2kLw"
            "RV+ACzWoL6TBGCVGotjtOnPkhSTDF5SDlsyVcg8Z+ou26l23Uu26l5+G5Uy3KmW5Uy5JTGVskS4Dfzkz+isKvUIbxmQEmvBL"
            "zQkecAfkwcmq2ENpzJDkAtCcOEAth5KOny+MzVjJhW/KY/kF5ptsErNHXWpYXZhhMvghzxajdNPwYXupdt1Ltupdt1NfLtyp"
            "luVMtypm3k5AkKMhEmlbUAAAAA7Q4yM4MFKMtypluVMtyplxR23Uu26l23Uu4wASzMyXqBhWSthm2YxRIe256MjLKq7sht3A"
            "o488MUXOty7E2w79jAZDR+36dVeKCu863KZblTLcqZblTNKsFMtypluVMt0ZdyC/6Rh7FhrhGPN8JV6AGMPr+Z0MM/ygA3Dw"
            "MscgkJay6cI6zE2gAW8U8TqotlmADVi9P7qBIYob6NhgqykNa0gQtypluVMtyplufm5Uy3KmW5Uy3Rl5ux1L4hhtwi7d5+qs"
            "xm99LroIhmUNllhFmKeBetfpjoIupBetvAi6CKf8kjV0AxtTFr4j/pDx1Ltupdt1LtupefhuVMtypluVMuSVNoI5/A6/sYDn"
            "TMEfAQG7FoFOBpSjoYj04u5AiwACSlsoWb2ggeXbdS7bqXbdS7hHcqZblTLcqZboy5oSRUaNrdhPZ654ziCOFdzSVoggAAEE"
            "AEEgC+Ih03TGO2updt1Ltupdt1Lz8NypluVMtyplySpmGKV0yWQefr8P22zYlGz4O6uAA2z78oUAwPJQMQ0X/OiqkIgBmKeh"
            "hJYozA5eq5YVbX5Til5UT497AAJCBOXwyITLSPLL33Yxle/y7cqZblTLcqZcUdt1Ltupdt1LuMAM4k86AxCndvZjMYn2zIoE"
            "6pZSzomBbLY1Go1LZaqHr7E81WSLrpn/XPbdS7bqXbdS7b9MqZblTLcqZblfH+fwwsOQyGQnHcyGM2y6I2/X/rntupdt1Ltu"
            "pdt+mVMtypluVMtyvj/OsL2yb5Tg5KRGf0iq7wDuL/updt1Ltupdt1NfLtypluVMtypm3zedIeBjn8RZJYM7v2Adt1J23+Hj"
            "qXbdS7bqXbdS8/DcqZblTLcqZciWg5aaC5JPQ6o95XUZDIGDmfixlVLtupdt1LtutHpjcqZblTLcqZboy7CTH2zE1MmixIdX"
            "cYNFiQ4WJm0kArZdt1Ltupdt1Lt8FeXbdS7bqXbdS7bLBAbdSdt1NfLtZUy4o7bpLtupdt1Ltupdt1NfLtypluVMtyplyTt6"
            "imy1IL43U2oLKsFv3Uu26l23Uu26l5+G5Uy3KmW5Uy3KkC6AyplujO0BNc746l23Uu26l23Uu26l23U18u3KmW5Uy3KmW5Uy"
            "3Kndd/O6l24OikXUu26l23Uu26l23Uu2/TKmW5Uy3KmW5Uy3KmW6MtiOc1XrXNOEa1zThGrOYBQzdlMtypluVMtypluVMtyp"
            "1Kl23Uu26l23Uu26l236YBzjOojixT2xlT80IdqhPEFThS9E/BvqRnhJ2IGQIPlxhQ7rBYAZgGhNKMVzRjlvlnf2onVK9bBV"
            "eCpluVMtypluVMtyplxR23Uu26l23Uu26l23U4AjFtjD7J4rFYrPT/////8naM9RLFON1Ltupdt1Ltupdt1LuCbLTLcqZblT"
            "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"
        ),
    },
    {
        "id": "15-app-find-empty",
        "cat": "app",
        "title": "Поиск по коду",
        "caption": "Шесть ячеек для ввода",
        "w": 1120,
        "h": 700,
        "bytes": 3828,
        "data": (
            "data:image/webp;base64,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"
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        ),
    },
    {
        "id": "16-app-find-result",
        "cat": "app",
        "title": "Пост найден",
        "caption": "Карточка подгрузилась по коду",
        "w": 1120,
        "h": 700,
        "bytes": 14476,
        "data": (
            "data:image/webp;base64,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"
            "AABb4BJo4nwAAAAAAAAAAAAAAAAAA=="
        ),
    },
    {
        "id": "17-post-view",
        "cat": "app",
        "title": "Страница поста",
        "caption": "Единая ссылка /p/{код} с галереей",
        "w": 1120,
        "h": 700,
        "bytes": 14684,
        "data": (
            "data:image/webp;base64,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"
            "AAAAAAAAAAAAAAAAAA="
        ),
    },
    {
        "id": "18-post-lightbox",
        "cat": "app",
        "title": "Просмотр фото",
        "caption": "Зум, панорама и перелистывание",
        "w": 1120,
        "h": 700,
        "bytes": 10634,
        "data": (
            "data:image/webp;base64,UklGRoIpAABXRUJQVlA4IHYpAACwbQGdASpgBLwCPolEnkulJCMiohII8KARCWdu6ebYBVA2f"
            "BWaOzK6AE8Jib2K6Rv+fSyz8u8cpP5f9285v+g/aP3a/oT2AP895WXq3/rf+/9Q/9B/3Hrcf9f1Tf7r1AP931PH919QD9sPW"
            "V/93s5/2//t8Hr04/m397/1/hD/m9oqvR/J+BXUD+xP998n/gtqEXGe1f1LzC/XH6559H1HnN/ef5z2AP8txGWMH55/xPYN/"
            "nm8NfscIa95Dpc28N1jHvIdLm3husY95Dpc28N1jHvIdLm3hujZguvsf2pZCO4rEOmrHgWFc28N1jHvIdLm3h0jLcD5t4brG"
            "PeQomnvGdBL0K2qVApcE7YMRGaVTSWSizmJM74h3Yx7yHS5utBEu8N1jHvIdLm3hxR4cg9DbXD2n7aJ45ereG6yQmt7kH0ub"
            "eG6xj4gZhcIU7yHS5t4brGQPt6IfS5t4brGQNTGHLTsenVUtJ/yejWE7olOqpaT/k9GsJ3RKdVS0n/J6NYTuiU6qlpP+T0aw"
            "ndEp1VLSf8no1hO6JTqkbijCjsHqUH0ubeG6xj0siS2kRH/RYEyZyOfFCir0f5Qzl3ndYfD9MRz2weWLwM5d52I7koU9e8h0"
            "ubeG6fQcKYWPfINkhRhQfyVhO6JTqqWk/4TEzwfq2ipaSJ3PrCcjDB6PlJXP95Dpc28N1i57Icoi87T10/SXAhHzWlOqpaT/"
            "hoH9Za2qN4kiE7oClSb1AR0M84BLPIambaHSI8E+aJ7iWgiHXkA+TKmk7yHS5t4YoK+pVfdoJCbhODup7qvC3+Kq0xP/hKHF"
            "rIPD10A9oDJwQzCmqIlQFll7saW/pUmaQeyL8NV92geHt4h3Yx7yHS5YGnAE9nMg1qDhHoS1AWoWmRYRCYIS/w4+TRqkmEdX"
            "Nb2aF1YGf7zi2NUcK3ZuBFEWxwrDXMWlD3T2KzxXqTndjHvIdLm3fPVv95Dpc28MUlZO7FArds0MUf2qfJXReGQ0uBnIE7u0"
            "pxDdsun9x0QWBqgTW7GPeQ6XNvUkg8uXPflfSAZLFffzV3ia2dP0e4yeA/gsl+y0X0r3MreyDRORzfaKQKAxFroA78Q6XNvD"
            "dYx6SLiJXg7QLJFs25Neb+7QUNPGcIH8brUDmsCz8vQCBsZhAbOxoFbmharBmyfw3WMe8h0uc5fcTzR8hdIUNkA3x0Nv/V57"
            "PfkREH7pMSFKx7rzRiiF8BaTSV7RspsUAvQw+Pm3kSu+4pDuxj3kOlzcnoAjLnpaJL3ru+mb2J7hkwEaf6NFWGOaQcbYS1k8"
            "/7Yj+u6Js1EpdH1uBlfH7T1xXBuxj3kOlzbw473FWZEE73U05J6HXxq79dcK8cOnVUtJ/wzgbH9Z2zzqjyTYE03zHpFMyGAf"
            "MBZPzlNFPZhotREWoiLURA85qx7yHS5t4bvBJ4KOsWWH5+DsVLuIfXrSxg6o8sgWG8SYUpOFBwDv6vM70zX16YtnYqFkCpyi"
            "3loAXRLR9AWoC1AWoC1AWoC1AWoC1ATmVNJ3kOlzbwwQ3Ud8C026Pv/+0YIFjSZTTtA9uyTT3v6Go9pz9LDQuehusZBBQo3r"
            "d92gj9LEbB5ToCyXNvDdYx7yGEI5ksY9zXuY8i7TpmvaDzDERc8ZVf563AyvkJkbYPLo+twMr4/TEN0tnli9qaHX4SMe8h0u"
            "beG7v6APvwcKb5EtbC79n49f19qqpaTwvKTuUEbevRSA7IaAu3/X0IMtupUGdsJdDGIZ/5jdmSqONb+q9UnId2Me8h1GC97J"
            "Buxj3kLCXyQlzbvsStdhHwZ957yU+9K+NkHcOvt69m4QyibNiqe5sWGoZG03ZohnskiCTMHpDUcJpO8h0ubeG6wKNdKw3eRL"
            "PKRcnNTGALYuLsyMYxD9JXMRun1u8de57dADQCH53Xp4RHupUvz7DGZTgYzTFwSGEYx074h3Yx7y5Nic7sYyRBdRbig/rxFA"
            "Wot7zQQImkDPeDmJgOeQ+5fpgjLFBrzbt1UqbUeiErNKZrGGWJFcrOqzcWcnBsAy6xEGNfJlr+7k68ynhDh+DoPam09vEO1o"
            "LBd+Mk/a6rUIYFDdYx8HXaF/jSHFdrKpYsn/wn4VZjrVCPyKOujvV9i5CQFt5KQQA7ABgoDEuenAk2hXM4zTEz3Jw/cXjF40"
            "iiemVNJ4zHS5t4cc+QDnm9J5nbnUsbB33HAEO3IyUeyPQa4IjpH2RPWSgPglA1f1nJSDQ/5UbYYfe8M98m+iD8Vljjmumk+Q"
            "nAu1VgrHS8ET6urCa3Yx7yHS5t6jdbMxkWmBk50b09JzK4cNi3i8aQ0wj/j9HReH/NNR70CYZdCV20qdkj85P09Zey/oEoo6"
            "M7yHS5t4brHrjyfWEkqFCXtM3lEUXnSumtLsKFiUV38RssoFQj8HbZb35q6MlLKZ7lkEhAcTL4sdJP8aRR24bWqD6XNvDdYx"
            "8HW3x3kM3o1hNXAOpExZL6IySkuHwDxqg6D27rh/yRWhhWjh5IdBEAeS5dQzoxHNIZyGfwesFIS4FynhSJa95Dpc28N1j2Of"
            "40ijtw2LeLxpFHbhsW8XjPv2grbD/Gkf3tnlTJpVHxsBdUGPi+0htRL2doIE3PowtPHjFtbYusfXF5Dpc28N1jIAveygVCPy"
            "KO3DYt4vGkUduGxaM74N81kdwMRxLS3GXZ2xVQ38mKg6o/U/+5AqtRaf7gtyqBNsVySOQOj2xbxeNuxO+Id2Me8h1YxVlwP0"
            "6bqCQqTn/KEQjw3Lxdpb5b6xXWlR/emTTnPnWuc43J+rH0IMPjxlpbrS7EnMESumLcTW4puj5t4brGPeQ/S3ZQJVyLKCLFOd"
            "MeQEfKLgIIb5wyoM9uvOk9CswM+kY95Dpc2HU+zp+rkenHnsLdxSH2mrHvIdLm3hu9eXOFITHWo0z4fJ59lehBL8ct4jSdJh"
            "v5tBdY+RsW8WtSsoB81Gzhh8AC49YAh8rarNaPm3husY95D57vIAaRPMFITG2ORnfZov8ZjJnFujoaquLeTzNoK1W9+WiRRQ"
            "v8ECNCxjI4NAIf/DO+Id2Me8h0xgPqNRPMFISm5aLsUX00fyUJ78eqm/c5eDD8PeNP6CJ+u//lBGhKJdTf/WWr28Q7sY95Dp"
            "csreZb+6aqTkLHh1/VsBp/Y167DgIXi+jkmE32WrmtUQi78q8vrEcuoF+EMxa71uS92KJCreK9dPGF4ah6roiTvIdLm3husZ"
            "W8Un96Yut5JEx83FhQyGoI+Ri+n6qalcQmi3kQ//fRs8Z8iHotRR1b0NxEn8bjqpRub1mvl1QYYYIMHt4h3Yx7yHS5ytkV1w"
            "E4HDJbXQS+WoaKLK9KIBzC6kIs4/BFELigShDJZCs5LivGiRAYpPs9VBDxnSHoKsQ6XNvDdYxrxx+6jund687QUgIF6I/PD8"
            "vjfj0mBDavTh/APHpaeN0Bg1UPJmBt3EQ0mHyQGbxBnt6KjN6NYTugHw3SA9DMnMu1yqiT3L8no1X4IIlH52nKx+m5YeOXA5"
            "Duxj3kOlzcOI9mGi1ERaiIp+gWohJbAOIDZGdd1im0JaNx+ctsFr0SNBsFT/i1n4tFEWiicvBOXgnLkkO7GPeQ6XNvDdYx7y"
            "HS5VBaCCMX6CHSXAvmZgQhGB2/KjjNe2AUjKQ1XdGoFkEUdNz4MW4KhJNsfG2jdMFh2UiOhUASfkYnevze1AecvEuhzw9ghP"
            "2u3o0BCgMWRAecCHBkaSKb1Z+bdFW7GPeQ6XNvDdYx7yHS5t4brGPeQ6Xa0S8kNRynPHBkfRjyhM5+ISrr2LiLYbWVZ9jD6X"
            "NvDdYx7yHS5t4brGPeQ6XNvDdYx7yHS4MqYK3kGcPBZffB18FKgx5vQwjdYx7yHS5t4brGPeQ6XNvDdYx7yHS5t4brGPeQ6X"
            "OsRvWMe8h0ubeG6xj3kOlzbcAAA/v8GcAAAAAAADJsx8eXPMC0o3Z75GZAABJU7EhoUqABv2AnxiQfUyJiK+xYS5iJpvP9pK"
            "JNrh4ovPRCc8/orhbmo2V+bq3/Ox//DBdGC9bc4fEgv9AETRAd1DDe7NUwAO4kicAIWl5h2O7sAICzxixRgjqlx5dU5UoCgB"
            "dF5JJ8PNcSgKE97D7JkOGMWOhqchd0IBrSTyQZrAAB2NV1HAAAAANb8dvGhYFhBHA9RRh6nhn4ET5qU8lDWdbyln5yB5KDTq"
            "EdZ1vKWfnIHXVbwIk8York9UsMAESD40trQvhQvRRf0A3jQMpPW929JfrbPfraiZB1XNUHjNUFCJ/Vc1QeM1QUIn9VzU9SJG"
            "akX61kwD+55s7efOanqjh/c9WsmAf3PVrISLET+q5qeqOHzA0ngxp8J/nyBbKiYN3Jk4AI4kGgMzXxRSEYaL8m6QRelLmD0D"
            "pvW1zvvg8fn0OJTDxK/FQIZgCLB+TMQU1EeRHi6b9Xzbf8xo3RocenXLGvEg1rE1lVzADgguPbSIvkVeaFKAFmNBxpMecAP7"
            "Re3t5DAEarpc8k5Nb9lcc+GpxUhApch2u8X7IfS9TfPSgkxG1rc5H+SSF0DFSXrNSbnAHqkbtCS19wLwWsQkIQXRQA7P35fZ"
            "EMqWX8dHc39jFOZSeS6JJiop7eBnzd/91m73zZelLuVpCh6aF4uIb7/Pghru29LHmjSyO7D9tUsmzBZKrlyFizeCLcJSRvHn"
            "fpdKgZLbsq6MexcZz7Z9BmzTyEq4xPfXKKxWr8R56Jlvva5KG68Km0BGFQBCGOT2vGSVXMyF9QHm4i8nNsmdLLh5etczgazr"
            "LXNmg4GdEyfmrWFwBUQiTPWagSu/UWuJ/ql7Yp3v2drIEZ1YUuqmxeodujaTRLUtvZYlLidvVsIjffIdVgJTX/l9JKB4DEPK"
            "hTA8hlCA7Bb4AKBf+YM+grhzkG6qqj4qg98MXZT69zPPtrAVhnPjnI45DP4IgpI8JT7jR2bj2e4ZSXpsBS5GrtiPyLyp6OXz"
            "LVoNgcDbj5MOmfLqq5lwOxG4vJDqvNcfbQ9iyYqtli4ysvUAY0AYl9UpH1nI2tW1932ICIoyHmdv8t5k4hZ0J+xj/ADJx5k1"
            "uyXFj8VIV05H5JHzebeLOHywE9CLH6WSP5QzCHVFHwPRyQeGl30aQOrwsVSXmuiv//hNwPIi9YNOvJT703a2AEayAAAEDcY2"
            "4Ci9EU6FhhUr2iSPHLTxQs2yZpRvk4y2aZcrItlYNgYzc2GwiRKB3DoSve+DbYK2RYvdqnLgullhO5QIlTZb90ryW75ZVBrE"
            "XcgZlt6ApIaEveJIeV0CXF2wMYrKJjswkPqqaDoUPrZwRFop8GYk33or3cJHh+zJMO0RuCEJ6xkA8+YceK9cCOog5KLunk3f"
            "+IyVuY10KMJgBMZHCLzMzMzMzMn/FmsthD/LkHx96iuwk5HbEEhZ86MbE/QPOKmQfpIpClb710IJ4zUblm8IrQbvWpElfCWf"
            "T6zTyz466apveDlH2V81y6HTVrsMxLDB0paNv5IeTCAvYWXFQhYUs0sDDASeevw5iNfpX3MZ4tc2+JZvIlLmfys+ETKAC7KZ"
            "VAxwej6d96Ay+o3PpmdinnzCeNxCE9HJSGk64qx6+y8HIS0ExhmhLvAx9Yf767tjs1xiqlKtgEfrjLlCGSnt43xpUyKHD1gR"
            "npc5UQDTOoe+1ZmdzpWxjel+2YSGHFkiddVWQG/ArAo5yZU/2o8J5WvKobQoADYBF9ERVxapH146FX9R17JOqxMArEFVpKit"
            "YciMsj6q7hCRQJk2aJwq8zLkSKbf8fPXQhultAxdkKXNH6XHPP+syJloxziC0aAt6CpC5uPNRznHlWouBESWbeuj/5UaCLZN"
            "rioVVgbNe+zjyMuAAinSB62KWZqj1B7sDYH2JEIVKdKGDkXT0vdJa+4wK4L6kb8UcuzeinM8WNGUWtDdxU/8hFpNekN6O36j"
            "spBUhqXCei/r8YiMlLraZvZ8RfdUUP2wK0hquhGUyuPUtEa6XG4/tf9gfjcs5Pb/cBjgy3+an9Z5sWdoFzLSyXvbRJxp4D6v"
            "zlGOVZYCXDGxeOP9lf+WPVu6rSPc2kLxAPZl3pvV9r2d86YbFbZScWkDPtlESRPiJc+ivI3eQesC1OABDWrJoHLxzgZbHp6n"
            "SQrSqwFcIHzAGbw5sxdodhg9eUCcoyy+1JJJnxkCsoTKON/oKIjX8lrzs2ifU6kvjkLsJO5vXCY7Rge4wQI9YP/Utuzt3HuN"
            "z65rb9val+0N5o7d9uFroDdHk+/LRp7PhvXi2SDIBDcWR9iYEQYtxHNKaHpWcYQP0d5B/XZH5y6u1AqkSExifXEAc1yZT/0r"
            "+gQtz+fNwZJ3Cn6b2O7H/D7TIZop3pP9N+/sZLFmzAIiLr7benkF7g9yI1JD9huaBhusaMREdYVE9/LBRO5LZSC/GsTsK/0w"
            "2SpuMjOz+tRASa4vujv0h9uA+vvbAPbyPmvlylsYtuCjDxFnpyMt3PuyaJe0Q1UMNdr+z3+acs//g4Lu27805Z//A/X+zjg3"
            "fdJ57cHBA79aN6X0gFnp/yo3AWBmH3IkwdpTz84eR0qfpZ6lUCroeHl/OY/dKFvbkGgcQBGfWlAW7T3aqkZzxsQzm+WLb6FN"
            "GQLDnB4BXPDrH3vsAGSsBePLNWrMLhRScL7DtT8bFOFrk+tBEdPAxF/THUkfKgCbs5uiHw9iwzXA+HxX+ZtQjMtPosM/H2XF"
            "Ems1ExGKHT12dgednCF2hsBq9H22KolrjF1YdR6HEQhvKGSi7EBkbkjwQWxk/2mfxh7v/lWbtIAEXiY3VKLzsSLU+UlOdGKC"
            "+bVTKF+pFkA8P654JDCGJG9cl8GxRxqeJJBIAAAAAtyc938aHfd8W/SSJtTDkilK60/jOzlBEENF7ukMccj3ST52IuVvv/IL"
            "Vt/FC/9bHiMR8kWnJlPVyRB7dVl+zrJczL7sVr8c4//0b79KyXzmYCNjGvexVwzx7sgW3o0dLAgC5OLMfu0vqlLMRLqB2jh4"
            "NjhvoM18XXydRDxDlRyc9U2EGvVVhs2gmze7G2T8Q8AKbCno2PsBPuuAxKQBL8QuvdUVfSaw0CWNZ63zpcT20eKqPZT2tJ3F"
            "bfgUL8okTwdTo2wNdPoB3sK2ST873tDyB+qHYPT6vWwt9MtD92rbDsB2Zhi5PzSOFiEO9wZmYkpm22M4FQYIAZdSPEnuqV4S"
            "+qxr/2sD+TK8VTWT+F2b3XnawKFzW95P5DiFnZ+FLOR3YLhIpCdXRJBbJCByA04mf9Wd0IJOhlKyef0boKIAGaOEjDkW3TEW"
            "A/kN52KKu2qXhKDL1EG2oqnPfm9OOtMYuuQHHda8Oil6Pzd0fkG9vI9S7yVGCctjfs8RmWZkUKqhncxZC6XXcfgaU3nAUOMU"
            "zZK8S7vZoVizTWDtnxPx4FhDVPfPnyDuWtWE3C7ANwA6c0Zml1QZrYiST9Je7DVQoMMoQbOm5dEPynCVH0Dbfq5q7qMH+Jiy"
            "2b08UxYri3Zz4koEZRrTQNkAMtCt63bOqj4DFDNncUYiOX5QxiDLsuHtVmJlAwfp61aXhWfOWOc6o4g9vHjncHvj0BAKzZXF"
            "ucqnGKrNhnoJQ2pscIAE0MaraCz33plCo0kbj0+82RjgJMMKcdYYfpKLcRI2oScSzsJgY6XRMGtJ53A4zxMVzXxwKTZ9aFdZ"
            "W+jVbYlzgGjMo7epsiSE0Wgf43AZJ0+JRHYihxYxuJ/t5eelwlCXEZ8VzitN/P1jaU9CHGmgISacq+tlqez2wih93m6kPH8k"
            "1t0aTkRkyugDpBQt9DFM/65ovVX2LN5I2lx4M04n/8zDLL15O+UJGDBYGxRl93saRzrFPjMyOBuGt5VIc0b1bJOAfX+4w+TC"
            "HeDgtasmeUmQoLuFiQxAcbQ+zGFqbhbuxMTY4CqMT02ReM2VQQQg/o/1f7TgYLbMpU8mMkp+zIxVYUMaqjjnkm7wHGBQ2Mfy"
            "3DdjMqpk85G/S3HDMNEILbekavnRV6nq3uus/8ywHp18vrQH9wpH+La7iCSt2K0GZlcS8r+HJC6I+qxa7gose50pUO9fKE2N"
            "lwz9DaDuvmPrq3dt1MOEYdZp459vDd7CYeiFrCKWZjvUbjrtw+D/k+ozy5By9TtDfTV57DEU9z94CHuF+CmlIygtJNMlppE8"
            "mwpjnfZqZq8x2vlCco8f6EmmZq0bLneaktyxy7D1lzmRAre7ULuZwLynjRbuG8zzCH5gg6tGV63/Wh3p8oH+/Vc0uMJmnX1z"
            "L5+8VJoDNXkceY7Ang5gnLFrx7z+SL0yslOhTpSUBWjTHkCOgAAAKB2xnQOnOlD4Fd5erEoITUWlBoazsgnSBhg7oJDVUB4g"
            "fgraGRhKfrwagwmkm4kn+i8tqBQ4qbkuIUN8VwhQhyhgXK1D0aQ42iIfFFT0jwOUzw8ND0hOH08T4zaVRn0jCmmqRfHUs/4v"
            "yNtZnxDRDHbAunngACJledh99MNYqTZTDnPoK3XuXsLkW8O1GBp3f89DWFZ7s8lzARfTxmPVqoOyhirlq3751yor7khWBCNf"
            "6cdyiSd5VoPFnidc73ngHSyn/uM4OIEXWf+5OcxCRZah7cAssHW+VX4rOYrteY7fK63e4Icza+SWumFIUuqsNfdbYAJJv1dT"
            "9GHxoH4nuLOfoyQHSN1xZls9HjOFPRLCxgYPbIlQC8OWkiSwFTw1J8yivRoaQG4iRCpckV2d94eCICjwXARpZZR4TyL6f66c"
            "ruAra65q6GSU7KG4bsUaC9IhyTo5tf5bv5VrgXAz6ziEBfB+thYIdwpiTpaAAB7SX8067eLGUxFDil/1DbEAAqVOo9Ddw7z/"
            "VXrZMep0MhqBlMG3K4QsFcu4n1jPJbRWHH5KPZ2ziJ30j9JBRqu0msiQ4Dw7S7f6pvZBlX/91nqnnuGok55+XMjcrW4ZCl2T"
            "1r6ZMqAi6xTnLZRWqTitrYsNucpPV3T2K+CbQ5P9difTnZ55svGSVUBzN1BJDHW3gBxDG3NB2I+8g02UhdYDxSvKdH8H7ggB"
            "8JeguWEJwT19e/O8cEO5B9KGVwmXg+GjLrX+NV8n/H1C7IOia2/hSF+ZLjy2IcJdpn1dpKuWBJltCfkQvFO/2x/rWHJq7GEl"
            "uxE1ui+FhRnIbYRyMarrezlcHOhuRAtoV9MnbGHmOSQDbuheyhgWc9wdDIFQ+g0QaTF3CW1DH3wst172gH15whs/yOC5RETx"
            "C+R8lAHS6h02rdGXemgCG9/z1zpjaNj2A/g0yg0xAWab/SmALM13LsHczRIVj1rAiCTwIxdkK08JXv0Ww+r2TF25Cy7xIvSJ"
            "ZENuwF7xmaebh8H3OFpEb0fs/Gy6pZbdkM+KD8pWxeYdqUvffFZrlBfP1/51OuPawHWRYZ4owCFdkievhylC8PeB7Pn36YaR"
            "EdQ0FaSLiYk/Z5pRKiuXeaLHleRVB2S6kigMpG7nYZooWKAvX+vKt4SG9/R3IBRp9s/QbvTAD4ejs/8nSV9zc8sxy2Wl178f"
            "7ZTIz9kmoXhE2JaUou3SjnkPtZAVreFChozrhj8xRz9+i9HCXnTxeI56b8DbMGrswaeMEbrmqxaFqLsH3gy9pqDFPnWgKNvU"
            "uz9C6HxUF7/D/TmP1LyowrywhEWITGTpPNA90EHk2TeYldFlSB38fUJuesi7gxYwDfh9Q3IhFmVRS3kotDCvL/s1MTL4vX58"
            "ZXWJQRVOJbauaUA5hmAIMQAvi6Pjq+wD1cWZM7N0p9ndXFyuZG5G7/EyEwdbnuDfhJFTdCv4NW9xHSCGIzYuKbinqpJ/4N2z"
            "O8aA/fTZ+1u+Ht7pB9Xb/9GYsfPKTksLHeWVxCzLMsj5XeAQvkhqZQ+jt3TGsntztdT3kKsKOpptjOc2HioYejmFf+AvOb5R"
            "RPQ8qrOJ4WOatMawPL3VVHusV0ElwQ/cdVXxSfkp6kl1Mrn4jkTDkeRby1csuTh6ZLuxmfDx861BVjovo+TElBf2jYnPlF40"
            "AOQFUUUAAAAGnAoN4uoKU8TFD05/eXRSQA82/AohyvMFOxhcw6uyW2H2LArSvRe9fj/98kTLxsARHdx3zmjq+RRERyEyRlX0"
            "Fp+GOFECNvMk/cxGz4CbA9aF1+fG8USiBQf/s3pGXc+7NZj1G+XqqT6LNuszrrpdNwVdqScI0ET5TudIPah9oYEtWBR8I+Cp"
            "Mhvij0NfJMj4IR34lITforZ6Nu/HUdjTQA2/MBAHmEDevFrMZLooer4U0rKAI16HSQx+Be2WM2wFw7t7IQkoAQ5eLYAAJniV"
            "4YIRSj/CYL2C+3QwyjTI1Jr4FEvljf6oRRAIXieuWIDny+Y5wZMjRSuTvJd/sbH0opIa4MRGlcb6/2B5I2YtsPumq0HFHmJm"
            "uKfM13kHwk2iBffpaeB7M2Hv/vuMHqD8pAZUSvjpC8yLdBNXte8A6kNaq4chkt4WoGxpifRWYhQ8h8nt6gKFNtWSrzKUBrmh"
            "JFU5YdSWzZwSPiW6Srm5Wy3x7aj9Hkz9Qn90WSS/W9njyGVm3LSxSOc32fyaASP7H1dieneBzTVAFtz40koCBHQU9lwv+uvb"
            "OnozDtsMEPgwxNvEtlFz8YoTm8e7o1grb9ITGL/j6ChUKikmpYVq1YYYeudbAp2I2518NKaXqJ30tzjTYabxOJHK54V8idoZ"
            "hQxiZfjvaUR1vBv1SanUUZTqX/SWQ2U6QUJasfxywndWgcRX9OQ2wkHE/ovNGec2Vok+2sf603a63Nmj9VkXNCYQAf7Cftyd"
            "uTtyduTtydztuIzO/0jlV4bMPwegA+g8iEhhmWcoUlzhS9R8EUCiw6PbgIjETk9ow7ImmyDKeDeEm/mhPybDNsnyMAKxey+R"
            "Zorgck/9XXjy+Qwpv3U6QENHrf8OxAQaDo5z4sn6O0eF/lSQhvyiWV46JkFY7T/Noe83IkBoAByBBsxNwmWEF5Au7Br5nSt8"
            "fOqMBfUr2CBnBQPuICUD72kfaUCL4ZDqaQcjTJ0ecpVUvGxcXY7re3zRtkJD5Wnha5MDiQFRqv0U9W5vqLfrqXg9FzZ/alhc"
            "8TtwWgqOtTRxYrb05Wj00RT9EiZuVdKXx4m2k6rS2st3I4nIco6DZQwaLw2GxlFAhcDcbaCJX7t91Ow5iivvvF2YH15euHmf"
            "w/fW8m0ZlD3G88+u1uge5zNSYPQ0bygWRvlxdix1rbmPABZq6mMOmSVwlGKNU7sgA0Iqhto7VI4uHykImuO4oQAuuIoEiKSB"
            "lq9cVYzfl2/7tQ/vSyoqlo6VYmVWdlZFqXc7KkdV7UAR9jLb6WksLo6YWz3gpWgJsTCG4uKXSEi6h+nNSVWgAW0HCnFAD0mm"
            "EywY16NcnhT698NqPUv+P/pIQIl60jaMAY55CxHqekx0jePkdEJgoZ0AI0HYQ7FDWO8bi0mq9fFrusMwtIFbfWFTzU069+jb"
            "tgM7bG2KZcpFl5BsPEU+FML6Ohvn+5rCO5TudIBy1XMu6Omh6PMpmqjymN6/cKq17CpgRYU+mL01R3Ll+jBVdbY7zsb2RZkd"
            "QtIt84weuRAmZ/4CiTAD365h8ZWSfxHeSmPhvsVe0728ZVxdBuO95cFwi3jvT8ujnJPWvS/aE7KfEujTDcO6SWNoF36Rnobp"
            "3uRrzRuywssfsL6WzEwu01IocYLweRUt8jrUE2ZlUisYnnnkO8apofaTvFuePA46ojwQbt9BhMbOJMvON8xIi7XAvjBAXYyZ"
            "Q1ZjiCO8lMrQ6mmKpvzgKFX4wP7rGxA8jA1wOSO7w8gAzGZJ4ABhrvvQ1xJfQjEbhv1bpm3zFBOXCJFNOgTeKhU1GTyt7Lar"
            "18JRKl/nS9EgaHyzeee9QNHdRSWxGyA1mAdlIXR0htP0zs5lZoynLExzAhqmpoQS+XCqRkEGclvpET/0qVZTwky/rseu0Wfs"
            "s8XV0JYePir4JjhFeDjlSnXttxuwImTzLHHrB/L8XmweLIyYemPerSj2N7hhG2CB40XU1heWnx4Om7mFhyIUIo22bcw4SZXl"
            "sX+4qhHysAczpgzNkD8vzYYHJxa4LWm2Tvilj6MTcNMNrkOcpyX4DLXFOyrb3vemb/YziEiWTZq/phHhBTfBWqyC1rT09jhO"
            "PIE5otqiNDdhf7/vgTWszRsOTdbNOsdQzAaCGQlWtFkFsgdfJHzRAHiWtnH+iKty8ABBWW/Uk01Zskeyn4KeS2+XnioM4bU5"
            "mvimt7SdosTE76VGEXkevtsQnedigqSjemVcKTuXZjMYBsgZu5QpQDxXDTK73eOJqYZ0KI8mhaE3GBik5B05UZnbXSdTMve2"
            "OqdtEya+RkqOS0jo/asfFa6Ud3xLpty6wnZz9ghX/3Krpz/uY6zgBbnXnSFGnrqNzmVxi/YNakTcOY38tAhbjMPP2T2FrE1O"
            "nej9f4esXb4U7nt1RStBAQufGIU7wP3nFGSvSwcNSzTWD3eboZF52Yy2sV7wDrgFc5sl7QkAEEAD8j6RKA+9EhEXwiiDSPey"
            "XKqnDDHygAFRg0yWeiPb9anKMFNkW0sVzC20eCdqnrIQivvoiz21Db0Q1B0wP6CcNpYcQyoPoTJWWas4Hn7DryIj1udsLsGp"
            "Vh8NqYrJgN+g+7Iuxg5Qg2O3yOPC0wcjlk7Q9er2YteC44UnuKrNDnz5vESLlH9WnaEPgJpxT5/fb2Zh3/RCK6O4VXadGhTa"
            "xM6CtHVsH1q/aEReZShH3mi/w92H4ep6BtwJ4YJAiACJd0VVGK/XxV0P566FrfHTOY+urIdM1xMd5e9gAnH2/amDbYzlBWOb"
            "StnH+QfEtj3uZt3El05B2fO88cYKtkRm0+vGRvToutHXqoe7fq+5AldeQp2v2UQ1UP1xSEW2XDvrRN8szecKaVjLbCEYr2qW"
            "C+oXlz6guPKHdruIi6z9dtGAM5dw5dj/j1mJHzfTLLyW1RoxXcHGs9uyJt+mYv4QM/95bOChG723iJe7Go3/GL2LZMrvNj0q"
            "2fb4C3I/JsrP5IrIU4RCv5HwqFCoymnu/o/wBPf8wOZWwWchhXZQD0HazBYc+Jl2MO2v9S4Auz/kUpdrT8TU0dgApgW+HRYC"
            "CNJEnxrMOjBuZLlcnCGV/ISwqeY3SVrQjN/U4JxoYQ3cAAAAAAEn0aehG5myHUpj9z5ydAfEjk8ffI3E1nZsN5XDrgAAAAAA"
            "ABGpA0RoZt1cNZI/nGUrWi9pZ6viVKi25LsqImqZYxmMnVgfp9nSLurySqj5gY4ujW9m0iPf+/UylImTyfsetw6gao87OPr2"
            "mAltOdCplyhUG3nfYaRyUnzVD+SpcOpoGRDXJ68SRMF/QLI1W8QKHxmW7lae2k3GNmfaEABByJmdPt5NnFch0m427WFnmQTY"
            "+y4q2YWfiM9q+UkDcgSU6F0lPYjL93/q9V9mzFq21MaaUCkYdNiu5AQT4UpcWfvv1QW4cEwtqgZAD9vBq10NDXctkXpK9B1O"
            "dNvWuOCYOt16QPSgONDAHS2MTb7EdEvRxOf0gjLCpxaWIozSN2rejDkodh307L47VqXG0G3Y+vk+LtktN9DFzzSZ+/SsFZ+y"
            "2E9/rVtvgu1Ex76o5/E1XCFXZDnSJ5rwBPw7kEemh4jJJwUkTgQoS9xqwzk/EzOr9i9fiUn+7yCp0nzItN334lLpwZEkknyu"
            "R9NWuhogmDEHpiQ2A+mXpI0R1I+H4MOZF8AHVK29rMsQ4xAXbir1Iv8Ctxjbce5BDOWDp0JejT4YLkV3jXOAfetFEZJjPj4g"
            "YTz2sIVHtVNQX/bT6Ht2rUuNXVc9AH7VGj7arEePJPC1psvYGSSvIiSd9ph2hg8dMb529E0Lad3esZOIk7hozagSf7GYE0MZ"
            "CBeHLIniPPb26zxbfurdC8waPpKyDepuMiXtUn1t2+2gy9A89QLn7JYBlgQ1DFQmCcHSSeX+7OvjN2IZfRNvATda0C2LntZt"
            "o3t043v2JvmXJOe9aSgRj8mKtP/5h9c2a8r6hkwQiQedjTXqdgmcZViAAAAAAA1qVdOPZz0XrDPe+88/LMpnhAi9FsEWBXXt"
            "J1Kd2izir+nX8MKCDNG0yXQ2jfI5kEY+2BtuIM/Q5VW0sRlFf8efdRBfMc/SonWLzMLlWehrI1jnVYVlUPAkqTxBCAAAAAAA"
            "ADkKgwTDx6+vPf/FyxvKk2MdNxJ+j3K3+i6SPoC5xm60FYNnne0Fiq/MLSzVPuu20exgFE+uAAAAAAAAAAAAAAAAAA="
        ),
    },
    {
        "id": "19-app-find-result-scroll",
        "cat": "app",
        "title": "Пост · прокрутка",
        "caption": "Галерея и текст поста ниже сгиба",
        "w": 1120,
        "h": 700,
        "bytes": 14494,
        "data": (
            "data:image/webp;base64,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"
        ),
    },
    {
        "id": "20-mobile-landing",
        "cat": "mobile",
        "title": "Мобильная главная",
        "caption": "390 × 844, первый экран",
        "w": 460,
        "h": 995,
        "bytes": 23268,
        "data": (
            "data:image/webp;base64,UklGRtxaAABXRUJQVlA4INBaAABQoQGdASrMAeMDPolAm0slI6YlIrKKQMARCWlu/mq9GhgFO"
            "gwn+cn6l57/38f7Bkmd81/3f8o/el8d/Yv75/i/2z8//xT51+3/3f9rP7x+0nS+9U/vvND+Qfar8n/d/8z/xP8H+9/xj/nv8"
            "5+KHo/8fP7f9e/cF/I/5X/kP7V+7H+D9YXZB61+6HqC+rP0X/ef4P/Vf/L/VeiT/Uf4r/K/+r3K/Rv8j/xv8X+7/+V+wH+S/"
            "1P/Y/4D96P8/////r9//6PwaPyH/I/5f++/Lz7Av5X/ev+z/j/9p+5/0vf1v/p/135j+4n83/03/l/1PwF/zb+1f9b/F/6Xu"
            "i/vh7P49htSrfXqOSnfSslO/uGunQn06dCn1hIlF/tZpgbN4S4tOnPd6ar13Mg+0dsqcBfUk8Sc8UhI5XOwcPBu5+VLNnzD/"
            "e+UcBSRdSpRGWTmqB6YW6Z0J6KAv2OgCNxMbNIS0ekDeO45lEFjlq4WnnGMcBz5bV2R2w00xzPClePhZOTAoF7NeRNCOlZ5k"
            "LnpKK2Zxb4ifiWXADfdfRlRnPMdwbrvm0PBCIAyMgGgMzbGK+ZWj0Me3O0YBUjXbDcT0IGefDj21NyX0jO6WfDyO+9ODHPs0"
            "yImbanHIf/pnU4mGTWVFWGFy1Zx5r+4ykb5VzkJ/KfTfFjgyZPja8gy48B4oVWXRlriM9JPoPEW4owbfTV6zSCriN6ucBfUl"
            "CD622xZ5mvj7z2d2bmXq6O+/EUxjGEdQpGl7nOc5znOM51YWyx6iIrTM5QEau5upadqvj4Zyvssp9coOXChtSxgk9dOhPoMg"
            "tlBAdoy5bKCA7Rly2UE57QhT+1xWnoolEoieI/DW3GKdfzyBQrk6UYYPo25aVsYxjGMYxjGMYm+tDkYxgkrHLheg4iSn3CZz"
            "HjImdJkrOgSdTe/geyGP2t8O/A8hKgb5Zfvnju9JjOpwSLvYfNL2HVgvtwgDy46YN3Q0JmVW4ARHvN8+wSy4TvcG+dYtPD+n"
            "T4Uu3NGM2qwlOyADV5N9LHUCkZY1qTMk33C9sPvElgn6XxgyrRh7XyvTuKzUXBdwPA0bSlOawA7caTgH77tGWvbbhBh168Gf"
            "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"
            "oAB4tZAAAAAAAADTmd2VBDEil5PPBg1IQrDW+AA"
        ),
    },
    {
        "id": "21-mobile-landing-features",
        "cat": "mobile",
        "title": "Мобильные возможности",
        "caption": "Одноколоночная раскладка",
        "w": 460,
        "h": 995,
        "bytes": 23364,
        "data": (
            "data:image/webp;base64,UklGRjxbAABXRUJQVlA4IDBbAABwhQGdASrMAeMDPolCnEslI6MlIxJJ+KARCWlu+98aJvZw5"
            "PAJjt0LlbtUr0OmERvzpfR/9Y/JT3sfGv2T+w/3H9r/7R6d/jX0T+A/tf7Y/4T2hf87/Dea/1H+k/1P+T9Tf5V9ofyf92/d3"
            "83vkD/N/4T8c/SX5Hf1v3M/IL+Tfyr/Lf3D90f856nf+72wenf8b/Zf572BfXf6T/wv8r+9/+n9F7+l/vn+U9if0H+/f6j/G"
            "flJ9gH8b/qv/C/uH5T/Qn+T8H38J/uv2k+AP+af2n/tf6H/X/C5/Vf+v/V/6X90/cT+d/6X/2/6H/W/IZ/PP7V/3P8j+Vfgv"
            "9H0c4IhILlIH/+71i9ZRexKDj/7l2CZUsrMPAASAQl3nGuIXsZTi5Imb9cSX/KU7wNc87UyPA+3zmAec/SJXy409eh24d4/Q"
            "Kdz1qHLwdy73zYRtX5ZlRCpp7KNHZwTkcu+HBTl7owMllw2n3yPcrv4xD2ybepuh4SWfOjOEgWELy6VXdoHsFap5dshutqyj"
            "p4Gg2891h3cvLxDKdZUm/7ssls3Oq4U/wwITXQZZ7ZR1kZZLSg6nbcL+qqFtohItD7JZRP9KJpV6+Dsga3XI0vhajWWKsZOJ"
            "0IcnjW4c1qO+zgsbMK4ZFBZq/wM1VLhIjaApXl3CjHmt6USVFU0ERNV3DRlmHxW2Zo5Sfx01RemQUFgiE/fV+u335QINoK/e"
            "dp4aMstXGEygtERNvzJqi8xx+qkfYrAEykDufd1zKMg9XpK9ZmZnuNGZmZmZmZmo/v/+71i9ZRexKDj/7mLKLUC6oqqqqqqq"
            "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqprJWry7znlChpGi0rkpMXnPKOWJ3d1FWr0leszMzMzMzMzMzMzMzMzMzMXzQEVBId"
            "yFOqQzZvbMHituqfDd5WuaAbFoYZ5sTvSdC7Wi/lJ3gqcsWb6Tp6SeGjgGSkOGbGV5169JRnLCGU3Jbhs8M0vvOeUcsTu7u7"
            "wycSzfJQlErTpGi1ekr1ixnW+P61EcsTu7u7u7oKaHPHyXu97G2ioyLdYODS+1SaBxyxliH/CLIxR5ixf+QZuGdHSif9ubRh"
            "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"
            "Bp6guv4JS1y+HG5KlNpvoYtf+3fE2xA6FqlGUKd+VQlWsTuKDpe7YamnM9xloEF/oqAutqd/f4dCOJ7rZgsluGRiuKMEwitz"
            "D/yUgAl+RVWD5ZNADVGT0bDsJd746jHJbRvTTVpF/6kU2/tn2cKFsN9B4TlAMduJKwnxGwYsz7lEYrIQxMCc3stSIfzAoh5v"
            "/zBtglaU2Sjn/9qEJv9qz+jhf7ut+srNuq56oreN1yDZhFcBrQliBXAStHjMbRBMz5ofjIfh3yBsihFQ/69FwuRyYORBthPu"
            "cunp5GEEahBS5nFOBHT5kkOie730HhseNMJY4Yx2/+YBRwYL41NchsPQd1Cxg7zulco1u8C1OGx3bhrhcqeDEdafEU5kMpH7"
            "JwzQSwPqHzc4s376LMpXbX9KorDTlllbPxz9dlRBPVNHph4Wq2DxOmU3s5YqL7fKpueOdGHKpD8woa2n4OPyazaEjxsvCQnV"
            "1xQU5goJNmJ8ZSh8u3sE+Q+4RipMhHccwQEziwQKIONTI1sfxsHvLCW+iWToP1dBGr4s9TLhLbl07ynyEupMZlS2lvLYYjz4"
            "kyJT4xiSbkETNI7+Xsj8f8k9xYczueGR9J9oxxde7zGfevMANbw1S2I+qkuR5cuZNSlWLW2Ha89aVISLxdF8swdqiLOpQv9C"
            "rCAIH5Uki4gxGBx+3Tyf6U1rA1xP0MKuvzgskvgv871Of1coYW2sVtAQ9qpMEjLrkK1vqQ55Ff9xstzWIw383D60fJU71Cwv"
            "PBPSam6HcQSgJnQFxQx9PAy+EjL+bvZHW20NBl2AYX+uRm76zz0sVj4+to1lMCqeHTv0Y7B9bmF0zXAuTptCs3LAsUkdbBiv"
            "Ni/kcWtcOh4OXKJyzrn6+upme9+d5400wkwMHqDQ7IsRH7nEwuf4WamV8+jtWUcbXrhVKhjiCjWg/CVGlCQi1QLGhBtdmwb1"
            "GAGxBjGISW47Fd94NL9Q9Ue+qnx+8LzaDT9Q/w25vwTrBlZ9WHNoY7vIZSP5zVXU0U14Dir70Hfy+X9p8ZcP3Xn4PkC1OZwN"
            "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"
        ),
    },
    {
        "id": "22-mobile-app-create",
        "cat": "mobile",
        "title": "Мобильная форма",
        "caption": "Создание поста на телефоне",
        "w": 460,
        "h": 995,
        "bytes": 13338,
        "data": (
            "data:image/webp;base64,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"
        ),
    },
    {
        "id": "23-mobile-post",
        "cat": "mobile",
        "title": "Мобильный пост",
        "caption": "Карточка поста на телефоне",
        "w": 460,
        "h": 995,
        "bytes": 21470,
        "data": (
            "data:image/webp;base64,UklGRtZTAABXRUJQVlA4IMpTAAAwhwGdASrMAeMDPolEnUulI6MlIfPJeKARCWlu2B0tNJjlB"
            "hM6bfVPQB328fPmY8lmHPGi9u5MIZjrp5jxHXjQ0Pch9V36eGj0uf77drf13oh/TN/xN+A/mPqAedt6zX+1ycf0n/h/VH8d/"
            "a/7Z/e/8F/g/PH8U+dftX93/Zn+3/th8h+UfsP/uPQ3+S/ZX79/dv3B/vfuX/lP8t+3H9d9IfkF/c+oL+MfyP/Bf3L93/8V6"
            "o+zFzv/mf8L1AvUT5v/uf7Z/ov2U9Hn+S/wX7pe5v6P/hf9l/e/yA+wH+Tf0H/Qf3397f8D////p98/9D/peMl+I/2X/S9wL"
            "+U/1//of4H/YftR9Lf8//6P9X/t/3d9u/5n/of+//pPy7+wf+W/2P/of43/Tftd4VPSP/eoU7GUGEPqjjeojjeojjeojjeod"
            "70sAgFLKAECA8qNksPQgTbY7fPfFi7eDi5cWE3qQltE1uk7lZDPflR8G4S8ZxxTvgYFvqtMpMqMgNWNiUP7MPTFv3JSH+K1d"
            "GcXUcu7/ARirhAoRLKwS0avWsLd9BguuWVQizgJBGirZteg9/eqr0JIXJ3goD+GioTpqwFyjFCCVWi16JYCSFZmEo3DJ9WUz"
            "7HMTBa4T+bkyCTHwWdb4Y1/ECWDooIfxucMH4l5KTSPGHwGeGSBSnKjVJ+c1JA83kMGus+DDQLaMm8koMDZd8o0HEINMDw7z"
            "Tep3TxvL4ONdmKzCO/zC53zC50HC7b+izEweoCnnp+WNIOBUPaytmkkHAqHtZWzSSDgVD2srZpJBwKh7WVs0kg4FQ9rKrpEb"
            "JNh8CHUkgKsWvRmAM2zysZqEOpJAVGxn9kTHesGfarGRs494jRaCkTpOxivsque0IgnAdFst4eEWBYe+1WFdmbgm642zysZq"
            "EQPzeojjeojjeojjeojjcoTh+D4+bbAP8JcLPWileojjZUXjQoHjLdkOdgDefQSb3gHOmQsPp4ZT3WG/Re2kCfWFIxxPC2NZ"
            "NuHLzRMgp80r23a+OLPC2SbF/RXExtWdU/sY3ChgknryM5uikX/AktzxWgisusb93YAwCmRzdKWT+mVR7ip9WsQrb4XDWyyX"
            "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"
        ),
    },
    {
        "id": "24-mobile-lightbox",
        "cat": "mobile",
        "title": "Мобильный просмотрщик",
        "caption": "Жесты: свайп, двойной тап, pinch",
        "w": 460,
        "h": 995,
        "bytes": 7534,
        "data": (
            "data:image/webp;base64,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"
        ),
    },
    # ==== PANORAMA_DATA:END ====
]

# Группы фильтра панорамы (id → подпись чипа)
PANORAMA_GROUPS = [
    ("all", "Все кадры"),
    ("landing", "Главная"),
    ("app", "Приложение"),
    ("mobile", "Мобильный"),
]

PANORAMA_CSS = r"""
/* ============================================================
   ПАНОРАМА СКРИНШОТОВ
   ============================================================ */
.panorama{padding:96px 0 88px;position:relative}
section[id],footer{scroll-margin-top:112px}
.pano-head{
  display:flex;align-items:flex-end;justify-content:space-between;
  gap:32px;flex-wrap:wrap;margin-bottom:30px;
}
.pano-head .sec-head{margin-bottom:0}
.pano-count{
  font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:500;
  letter-spacing:0.14em;text-transform:uppercase;color:var(--text-mute);
  display:inline-flex;align-items:center;gap:10px;white-space:nowrap;
  padding:9px 15px;border-radius:100px;
  border:1px solid var(--border);background:rgba(255,255,255,0.03);
}
.pano-count b{color:#fff;font-weight:600}
.pano-count i{
  width:7px;height:7px;border-radius:50%;background:#fff;display:inline-block;
  box-shadow:0 0 0 3px rgba(255,255,255,0.1);animation:dotPulse 2.4s ease-in-out infinite;
}
.pano-chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:22px}
.pano-chip{
  font-family:'Manrope',sans-serif;font-size:13px;font-weight:600;line-height:1;
  color:var(--text-dim);padding:11px 17px;border-radius:100px;
  border:1px solid var(--border-2);background:rgba(255,255,255,0.03);
  cursor:pointer;-webkit-appearance:none;appearance:none;
  transition:color .25s,background .25s,border-color .25s,transform .25s var(--ease);
}
.pano-chip:hover{color:#fff;background:rgba(255,255,255,0.07);border-color:var(--border-3)}
.pano-chip:active{transform:scale(.97)}
.pano-chip.active{background:#f4f4f5;color:#08080a;border-color:#f4f4f5}
.pano-chip em{font-style:normal;opacity:.55;margin-left:6px;font-family:'JetBrains Mono',monospace;font-size:11px}

.pano-stage{position:relative}
.pano-viewport{
  overflow-x:auto;overflow-y:hidden;
  scroll-snap-type:x proximity;
  padding:8px 0 20px;
  scrollbar-width:none;-ms-overflow-style:none;
  cursor:grab;touch-action:pan-y;
  -webkit-overflow-scrolling:touch;
  mask-image:linear-gradient(90deg,transparent,#000 2.4%,#000 97.6%,transparent);
  -webkit-mask-image:linear-gradient(90deg,transparent,#000 2.4%,#000 97.6%,transparent);
  overscroll-behavior-x:contain;
}
.pano-viewport::-webkit-scrollbar{display:none;height:0}
.pano-viewport.dragging{cursor:grabbing;scroll-snap-type:none}
.pano-track{display:flex;gap:18px;width:max-content;position:relative;padding:0 6px}
.pano-item{scroll-snap-align:center;position:relative}
.pano-shot{
  position:relative;height:clamp(210px,24vw,330px);width:auto;border-radius:18px;
  overflow:hidden;cursor:pointer;border:1px solid var(--border-2);
  background:linear-gradient(150deg,rgba(255,255,255,0.07),rgba(255,255,255,0.015));
  box-shadow:0 26px 60px -32px rgba(0,0,0,0.9),inset 0 1px 0 rgba(255,255,255,0.07);
  transition:transform .55s var(--ease),box-shadow .55s var(--ease),border-color .3s;
}
.pano-shot img{
  width:100%;height:100%;object-fit:cover;display:block;background:#0b0b0b;
  pointer-events:none;-webkit-user-drag:none;user-select:none;
}
.pano-item:hover .pano-shot{
  transform:translateY(-7px);border-color:var(--border-3);
  box-shadow:0 40px 88px -32px rgba(0,0,0,0.95),0 0 0 1px rgba(255,255,255,0.05),inset 0 1px 0 rgba(255,255,255,0.1);
}
.pano-shot:focus-visible{outline:2px solid rgba(255,255,255,0.55);outline-offset:3px}
.pano-num{
  position:absolute;top:11px;left:11px;z-index:3;
  font-family:'JetBrains Mono',monospace;font-size:10.5px;letter-spacing:0.1em;
  padding:5px 9px;border-radius:8px;color:var(--text-dim);
  background:rgba(6,6,6,0.72);border:1px solid var(--border-2);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
}
.pano-live{
  position:absolute;top:11px;right:11px;z-index:3;
  display:inline-flex;align-items:center;gap:6px;
  font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:0.12em;
  text-transform:uppercase;color:var(--text-dim);
  padding:5px 9px;border-radius:8px;
  background:rgba(6,6,6,0.72);border:1px solid var(--border-2);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  opacity:0;transform:translateY(-4px);
  transition:opacity .3s var(--ease),transform .3s var(--ease);
}
.pano-live::before{content:'';width:5px;height:5px;border-radius:50%;background:var(--ok);display:block}
.pano-item:hover .pano-live{opacity:1;transform:none}
.pano-cap{
  position:absolute;left:0;right:0;bottom:0;z-index:2;
  padding:32px 13px 11px;display:flex;flex-direction:column;gap:3px;
  background:linear-gradient(180deg,transparent,rgba(4,4,5,0.62) 45%,rgba(4,4,5,0.93));
  transition:padding-bottom .35s var(--ease);
}
.pano-cap b{
  font-size:12.5px;font-weight:700;color:#fff;letter-spacing:-0.01em;line-height:1.3;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
}
.pano-cap span{
  font-size:10.5px;line-height:1.4;color:var(--text-dim);
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
}
.pano-cta{
  display:inline-flex;align-items:center;gap:6px;margin-top:6px;
  font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:0.1em;
  text-transform:uppercase;color:rgba(255,255,255,0.75);
  max-height:0;opacity:0;overflow:hidden;
  transition:max-height .35s var(--ease),opacity .3s ease;
}
.pano-item:hover .pano-cta{max-height:16px;opacity:1}
.pano-cta svg{width:11px;height:11px;display:block}

.pano-nav{
  position:absolute;top:calc(50% - 22px);transform:translateY(-50%);z-index:6;
  width:46px;height:46px;border-radius:50%;display:grid;place-items:center;
  cursor:pointer;border:1px solid var(--border-2);color:#fff;
  background:rgba(12,12,12,0.82);
  backdrop-filter:blur(14px) saturate(150%);-webkit-backdrop-filter:blur(14px) saturate(150%);
  box-shadow:0 16px 36px -16px rgba(0,0,0,0.9),inset 0 1px 0 rgba(255,255,255,0.08);
  transition:opacity .3s,background .25s,border-color .25s,transform .3s var(--ease);
}
.pano-nav svg{width:18px;height:18px;display:block}
.pano-nav:hover{background:rgba(28,28,28,0.95);border-color:var(--border-3);transform:translateY(-50%) scale(1.05)}
.pano-nav[disabled]{opacity:0;pointer-events:none}
.pano-prev{left:-12px}
.pano-next{right:-12px}

.pano-foot{display:flex;align-items:center;gap:18px;margin-top:4px}
.pano-counter{
  font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:0.1em;
  color:var(--text-dim);min-width:74px;font-variant-numeric:tabular-nums;
}
.pano-progress{
  flex:1;height:2px;border-radius:2px;overflow:hidden;position:relative;
  background:rgba(255,255,255,0.08);
}
.pano-progress-fill{
  position:absolute;left:0;top:0;bottom:0;width:10%;border-radius:2px;
  background:linear-gradient(90deg,rgba(255,255,255,0.45),#fff);
  transition:width .18s linear;
}
.pano-hint{
  font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:0.06em;
  color:var(--text-mute);white-space:nowrap;
}

/* ---------- полноэкранный просмотр ---------- */
.pano-viewer{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;padding:4vh 4vw}
.pano-vbackdrop{
  position:absolute;inset:0;background:rgba(4,4,5,0.9);
  backdrop-filter:blur(18px) saturate(140%);-webkit-backdrop-filter:blur(18px) saturate(140%);
  animation:panoFade .3s ease both;
}
@keyframes panoFade{from{opacity:0}to{opacity:1}}
.pano-vfig{
  position:relative;z-index:2;display:flex;flex-direction:column;align-items:center;gap:15px;
  max-width:min(1400px,94vw);animation:panoPop .42s var(--ease) both;
}
@keyframes panoPop{from{opacity:0;transform:scale(.96) translateY(14px)}to{opacity:1;transform:none}}
.pano-vfig img{
  max-width:100%;max-height:74vh;display:block;border-radius:16px;
  border:1px solid var(--border-2);background:#0b0b0b;
  box-shadow:0 50px 120px -40px rgba(0,0,0,1);
}
.pano-vmeta{display:flex;flex-direction:column;gap:5px;text-align:center;max-width:760px}
.pano-vmeta b{font-family:'Unbounded',sans-serif;font-size:16px;font-weight:600;letter-spacing:-0.02em;color:#fff}
.pano-vmeta span{font-size:13.5px;color:var(--text-dim);line-height:1.5}
.pano-vbtn{
  position:absolute;z-index:3;width:52px;height:52px;border-radius:50%;
  display:grid;place-items:center;cursor:pointer;color:#fff;
  border:1px solid var(--border-2);background:rgba(14,14,14,0.8);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  box-shadow:0 18px 40px -18px rgba(0,0,0,0.9);
  transition:background .25s,border-color .25s,transform .25s var(--ease);
}
.pano-vbtn:hover{background:rgba(32,32,32,0.95);border-color:var(--border-3);transform:scale(1.06)}
.pano-vbtn svg{width:20px;height:20px;display:block}
.pano-vclose{top:22px;right:22px}
.pano-vprev{left:22px;top:50%;margin-top:-26px}
.pano-vnext{right:22px;top:50%;margin-top:-26px}
.pano-vcounter{
  position:absolute;bottom:22px;left:50%;transform:translateX(-50%);z-index:3;
  font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:0.12em;
  color:var(--text-dim);padding:7px 15px;border-radius:100px;
  border:1px solid var(--border);background:rgba(10,10,10,0.72);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  font-variant-numeric:tabular-nums;
}
body.pano-locked{overflow:hidden}

@media (max-width:1000px){
  .panorama{padding:76px 0 70px}
  .pano-shot{height:clamp(200px,36vw,290px)}
}
@media (max-width:720px){
  .panorama{padding:60px 0 56px}
  .pano-head{gap:18px;margin-bottom:22px}
  .pano-nav{display:none}
  .pano-shot{height:clamp(186px,54vw,260px)}
  .pano-track{gap:14px}
  .pano-foot{gap:12px}
  .pano-hint{display:none}
  .pano-vbtn{width:44px;height:44px}
  .pano-vbtn svg{width:18px;height:18px}
  .pano-vclose{top:12px;right:12px}
  .pano-vprev{left:8px}.pano-vnext{right:8px}
  .pano-vfig img{max-height:66vh}
  .pano-vmeta b{font-size:14.5px}
  .pano-vmeta span{font-size:12.5px}
}
"""

PANORAMA_JS = r"""
(function(){
  "use strict";
  var root = document.getElementById('panorama');
  if (!root) return;

  var viewport = root.querySelector('.pano-viewport');
  var track = root.querySelector('.pano-track');
  var fill = root.querySelector('.pano-progress-fill');
  var counter = root.querySelector('.pano-counter');
  var prevBtn = root.querySelector('.pano-prev');
  var nextBtn = root.querySelector('.pano-next');
  var chips = Array.prototype.slice.call(root.querySelectorAll('.pano-chip'));
  var items = Array.prototype.slice.call(track.querySelectorAll('.pano-item'));
  var visible = items.slice();
  if (!viewport || !items.length) return;

  var viewer = document.getElementById('panoViewer');
  var vImg = document.getElementById('panoViewerImg');
  var vTitle = document.getElementById('panoViewerTitle');
  var vCap = document.getElementById('panoViewerCaption');
  var vCounter = document.getElementById('panoViewerCounter');
  var vIndex = 0;
  var suppressClick = false;

  function pad(n){ return (n < 10 ? '0' : '') + n; }

  function activeIndex(){
    var vr = viewport.getBoundingClientRect();
    var best = 0, bestD = Infinity;
    for (var i = 0; i < visible.length; i++){
      var r = visible[i].getBoundingClientRect();
      var d = Math.abs(r.left - vr.left - 6);
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  }

  function refresh(){
    var max = viewport.scrollWidth - viewport.clientWidth;
    var pct = max > 2 ? (viewport.scrollLeft / max) * 100 : 100;
    if (fill) fill.style.width = Math.max(6, pct).toFixed(2) + '%';
    if (counter) counter.textContent = pad(activeIndex() + 1) + ' / ' + pad(visible.length);
    if (prevBtn) prevBtn.disabled = viewport.scrollLeft <= 2;
    if (nextBtn) nextBtn.disabled = max <= 2 || viewport.scrollLeft >= max - 2;
  }

  function goTo(i){
    if (!visible.length) return;
    i = Math.max(0, Math.min(visible.length - 1, i));
    var target = Math.max(0, visible[i].offsetLeft - 6);
    try { viewport.scrollTo({ left: target, behavior: 'smooth' }); }
    catch(e){ viewport.scrollLeft = target; }
    setTimeout(refresh, 420);
  }

  viewport.addEventListener('scroll', function(){
    if (viewport._raf) return;
    viewport._raf = requestAnimationFrame(function(){ viewport._raf = 0; refresh(); });
  }, { passive: true });
  addEventListener('resize', refresh, { passive: true });
  addEventListener('load', refresh);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function(){ setTimeout(refresh, 60); });
  setTimeout(refresh, 120);

  if (prevBtn) prevBtn.addEventListener('click', function(){ goTo(activeIndex() - 1); });
  if (nextBtn) nextBtn.addEventListener('click', function(){ goTo(activeIndex() + 1); });

  /* ---------- фильтр ---------- */
  chips.forEach(function(chip){
    chip.addEventListener('click', function(){
      var cat = chip.getAttribute('data-cat');
      chips.forEach(function(c){ c.classList.toggle('active', c === chip); });
      visible = [];
      items.forEach(function(it){
        var ok = (cat === 'all' || it.getAttribute('data-cat') === cat);
        it.hidden = !ok;
        if (ok) visible.push(it);
      });
      viewport.scrollLeft = 0;
      refresh();
      setTimeout(refresh, 60);
    });
  });

  /* ---------- перетаскивание мышью ---------- */
  var down = false, startX = 0, startScroll = 0, moved = 0;
  var lastX = 0, lastT = 0, speed = 0, raf = 0;

  function stopMomentum(){ if (raf) { cancelAnimationFrame(raf); raf = 0; } }

  viewport.addEventListener('pointerdown', function(e){
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    down = true; moved = 0;
    startX = e.clientX; startScroll = viewport.scrollLeft;
    lastX = e.clientX; lastT = Date.now(); speed = 0;
    stopMomentum();
    viewport.classList.add('dragging');
    try { viewport.setPointerCapture(e.pointerId); } catch(err){}
  });

  viewport.addEventListener('pointermove', function(e){
    if (!down) return;
    var dx = e.clientX - startX;
    if (Math.abs(dx) > moved) moved = Math.abs(dx);
    viewport.scrollLeft = startScroll - dx;
    var now = Date.now(), dt = now - lastT;
    if (dt > 0) { speed = (e.clientX - lastX) / dt; lastX = e.clientX; lastT = now; }
  });

  function endDrag(){
    if (!down) return;
    down = false;
    viewport.classList.remove('dragging');
    if (moved > 6) { suppressClick = true; setTimeout(function(){ suppressClick = false; }, 60); }
    var v = speed * 15;
    if (Math.abs(v) < 1.2) { refresh(); return; }
    stopMomentum();
    (function momentum(){
      v *= 0.93;
      viewport.scrollLeft -= v;
      if (Math.abs(v) > 0.6) raf = requestAnimationFrame(momentum);
      else { raf = 0; refresh(); }
    })();
  }
  viewport.addEventListener('pointerup', endDrag);
  viewport.addEventListener('pointercancel', endDrag);
  viewport.addEventListener('pointerleave', endDrag);
  viewport.addEventListener('dragstart', function(e){ e.preventDefault(); });

  /* ---------- просмотрщик ---------- */
  function renderViewer(){
    var item = visible[vIndex];
    if (!item) return;
    var img = item.querySelector('img');
    if (vImg && img) {
      vImg.src = img.getAttribute('src');
      vImg.alt = img.getAttribute('alt') || '';
    }
    if (vTitle) vTitle.textContent = item.getAttribute('data-title') || '';
    if (vCap) vCap.textContent = item.getAttribute('data-caption') || '';
    if (vCounter) vCounter.textContent = pad(vIndex + 1) + ' / ' + pad(visible.length);
  }
  function openViewer(item){
    if (!viewer) return;
    var i = visible.indexOf(item);
    if (i < 0) return;
    vIndex = i;
    renderViewer();
    viewer.hidden = false;
    document.body.classList.add('pano-locked');
  }
  function closeViewer(){
    if (!viewer) return;
    viewer.hidden = true;
    document.body.classList.remove('pano-locked');
  }
  function navViewer(dir){
    if (!visible.length) return;
    vIndex = (vIndex + dir + visible.length) % visible.length;
    renderViewer();
  }

  items.forEach(function(item){
    var shot = item.querySelector('.pano-shot');
    if (!shot) return;
    shot.addEventListener('click', function(){
      if (suppressClick) return;
      openViewer(item);
    });
    shot.addEventListener('keydown', function(e){
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault();
        openViewer(item);
      }
    });
  });

  if (viewer) {
    root.querySelectorAll('[data-pano-close]').forEach(function(el){
      el.addEventListener('click', closeViewer);
    });
    root.querySelectorAll('[data-pano-nav]').forEach(function(el){
      el.addEventListener('click', function(e){
        e.stopPropagation();
        navViewer(parseInt(el.getAttribute('data-pano-nav'), 10));
      });
    });
    addEventListener('keydown', function(e){
      if (viewer.hidden) return;
      if (e.key === 'Escape') { closeViewer(); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); navViewer(1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); navViewer(-1); }
    });
  }
})();
"""


def build_panorama_section() -> str:
    """Секция «Панорама» со скриншотами, вшитыми в base64."""
    if not PANORAMA_IMAGES:
        return ""

    counts: Dict[str, int] = {}
    for img in PANORAMA_IMAGES:
        cat = str(img.get("cat") or "app")
        counts[cat] = counts.get(cat, 0) + 1

    chips = []
    for key, label in PANORAMA_GROUPS:
        n = len(PANORAMA_IMAGES) if key == "all" else counts.get(key, 0)
        if not n:
            continue
        chips.append(
            '<button class="pano-chip{active}" type="button" data-cat="{key}">'
            '{label}<em>{n}</em></button>'.format(
                active=" active" if key == "all" else "",
                key=key,
                label=_html.escape(label),
                n=n,
            )
        )

    cards = []
    for i, img in enumerate(PANORAMA_IMAGES):
        title = str(img.get("title") or "Скриншот")
        caption = str(img.get("caption") or "")
        cards.append(
            '<figure class="pano-item" data-cat="{cat}" data-title="{title}" data-caption="{caption}">'
            '<div class="pano-shot" role="button" tabindex="0" aria-label="Открыть скриншот: {title}" '
            'style="aspect-ratio:{w} / {h}">'
            '<img src="{src}" alt="{title}" loading="lazy" decoding="async">'
            '<span class="pano-num">{num}</span>'
            '<span class="pano-live">скриншот</span>'
            '<figcaption class="pano-cap">'
            '<b>{title}</b><span>{caption}</span>'
            '<span class="pano-cta">Открыть<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="M5 12h14M13 6l6 6-6 6"/></svg></span>'
            '</figcaption>'
            '</div></figure>'.format(
                cat=_html.escape(str(img.get("cat") or "app")),
                title=_html.escape(title, quote=True),
                caption=_html.escape(caption, quote=True),
                w=img.get("w") or 1200,
                h=img.get("h") or 750,
                src=img.get("data") or "",
                num=f"{i + 1:02d}",
            )
        )

    total_mb = sum(int(img.get("bytes") or 0) for img in PANORAMA_IMAGES) / (1024 * 1024)

    return (
        '\n<section id="panorama" class="panorama">\n'
        '  <div class="wrap">\n'
        '    <div class="pano-head reveal reveal--up">\n'
        '      <div class="sec-head">\n'
        '        <div class="eyebrow">Панорама интерфейса</div>\n'
        '        <h2>Как это выглядит <span class="dim">на самом деле</span></h2>\n'
        '        <p>Каждый кадр снят в headless-браузере на живом приложении и вшит в страницу '
        'как <code>base64</code> — без внешних файлов и CDN. Потяните ленту мышью, '
        'выберите раздел или откройте кадр во весь экран.</p>\n'
        '      </div>\n'
        '      <div class="pano-count"><i></i><b>' + str(len(PANORAMA_IMAGES)) + '</b> кадров · '
        + f"{total_mb:.2f}".replace(".", ",") + ' МБ base64</div>\n'
        '    </div>\n'
        '    <div class="pano-chips reveal reveal--up" data-delay="80">' + "".join(chips) + '</div>\n'
        '    <div class="pano-stage reveal reveal--up" data-delay="140">\n'
        '      <button class="pano-nav pano-prev" type="button" aria-label="Назад">\n'
        '        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<polyline points="15 18 9 12 15 6"/></svg>\n'
        '      </button>\n'
        '      <div class="pano-viewport" id="panoViewport">\n'
        '        <div class="pano-track">' + "".join(cards) + '</div>\n'
        '      </div>\n'
        '      <button class="pano-nav pano-next" type="button" aria-label="Вперёд">\n'
        '        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<polyline points="9 18 15 12 9 6"/></svg>\n'
        '      </button>\n'
        '    </div>\n'
        '    <div class="pano-foot reveal reveal--up" data-delay="200">\n'
        '      <div class="pano-counter">01 / ' + f"{len(PANORAMA_IMAGES):02d}" + '</div>\n'
        '      <div class="pano-progress"><div class="pano-progress-fill"></div></div>\n'
        '      <div class="pano-hint">перетащите · стрелки · Esc — закрыть</div>\n'
        '    </div>\n'
        '  </div>\n'
        '\n'
        '  <div class="pano-viewer" id="panoViewer" hidden>\n'
        '    <div class="pano-vbackdrop" data-pano-close></div>\n'
        '    <button class="pano-vbtn pano-vclose" type="button" aria-label="Закрыть" data-pano-close>\n'
        '      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>\n'
        '    </button>\n'
        '    <button class="pano-vbtn pano-vprev" type="button" aria-label="Предыдущий кадр" data-pano-nav="-1">\n'
        '      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<polyline points="15 18 9 12 15 6"/></svg>\n'
        '    </button>\n'
        '    <button class="pano-vbtn pano-vnext" type="button" aria-label="Следующий кадр" data-pano-nav="1">\n'
        '      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<polyline points="9 18 15 12 9 6"/></svg>\n'
        '    </button>\n'
        '    <figure class="pano-vfig">\n'
        '      <img id="panoViewerImg" alt="" draggable="false">\n'
        '      <figcaption class="pano-vmeta">\n'
        '        <b id="panoViewerTitle"></b>\n'
        '        <span id="panoViewerCaption"></span>\n'
        '      </figcaption>\n'
        '    </figure>\n'
        '    <div class="pano-vcounter" id="panoViewerCounter">1 / 1</div>\n'
        '  </div>\n'
        '</section>\n'
    )


# ============================================================
# ЛЕНДИНГ
# ============================================================

LANDING_CSS = r"""
.hero{padding:170px 0 0;position:relative;overflow:hidden}
.hero-inner{max-width:1080px;margin:0 auto;text-align:center;position:relative;z-index:1;padding:0 8px;}

.badge{
  display:inline-flex;align-items:center;gap:10px;
  padding:8px 16px 8px 9px;border-radius:100px;
  background:rgba(255,255,255,0.055);
  border:1px solid rgba(255,255,255,0.2);
  backdrop-filter:blur(14px) saturate(160%);
  -webkit-backdrop-filter:blur(14px) saturate(160%);
  font-size:12.5px;font-weight:600;color:#fff;
  letter-spacing:-0.005em;margin-bottom:34px;
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

.hero-title{
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:clamp(28px,5.2vw,60px);line-height:1.08;
  letter-spacing:-0.04em;margin-bottom:28px;
  display:flex;flex-direction:column;gap:8px;
  position:relative;z-index:1;perspective:1000px;
  text-wrap:balance;max-width:100%;overflow-wrap:break-word;
}
.h1-row{display:block;text-align:center;}
.h1-row.dim{color:var(--text-mute);font-weight:400}
.hero-title .word{
  display:inline-block;opacity:0;
  transform:translateY(40%) rotateX(-55deg) scale(.96);
  filter:blur(6px);transform-origin:bottom center;
  animation:wordIn .75s var(--ease) both;
  animation-delay:calc(var(--i,0) * 75ms + 140ms);
  will-change:transform,opacity,filter;
  margin-right:0.24em;
}
.hero-title .word:last-child{margin-right:0}
@keyframes wordIn{
  0%{opacity:0;transform:translateY(40%) rotateX(-55deg) scale(.96);filter:blur(6px)}
  55%{filter:blur(1.5px)}
  100%{opacity:1;transform:translateY(0) rotateX(0) scale(1);filter:blur(0)}
}

.lead{
  font-size:clamp(15px,1.65vw,17px);line-height:1.65;
  color:var(--text-dim);max-width:600px;margin:0 auto 40px;
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
  margin:64px auto 0;max-width:540px;
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
  flex:1 1 0;max-width:66px;aspect-ratio:2/3;border-radius:13px;
  background:linear-gradient(160deg,rgba(255,255,255,0.065),rgba(255,255,255,0.018));
  border:1px solid var(--border-2);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  display:grid;place-items:center;
  font-family:'JetBrains Mono',monospace;font-weight:600;
  font-size:clamp(20px,3.2vw,25px);color:#fff;
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
  margin-top:20px;font-size:12.5px;color:var(--text-mute);letter-spacing:0.01em;
  opacity:0;transform:translateY(12px);
  animation:leadIn .8s var(--ease) 1.3s both;
  display:inline-flex;align-items:center;gap:9px;
  font-family:'JetBrains Mono',monospace;
}
.code-caption svg{width:13px;height:13px;opacity:.6;display:block}

.marquee{
  margin-top:100px;padding:22px 0;
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

section{padding:110px 0;position:relative}
.sec-head{max-width:660px;margin-bottom:52px}
h2{
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:clamp(26px,4.2vw,44px);line-height:1.08;
  letter-spacing:-0.035em;margin-bottom:18px;text-wrap:balance;
}
h2 .dim{color:var(--text-mute);font-weight:400}
.sec-head p{color:var(--text-dim);font-size:15.5px;line-height:1.65}

.tile-groups{display:flex;flex-direction:column;gap:56px}
.tile-group{display:flex;flex-direction:column;gap:18px}
.group-head{
  display:flex;align-items:baseline;justify-content:space-between;
  gap:18px;flex-wrap:wrap;padding-bottom:14px;border-bottom:1px solid var(--border);
}
.group-title{
  display:inline-flex;align-items:center;gap:12px;
  font-family:'Unbounded',sans-serif;font-weight:600;
  font-size:clamp(18px,2.4vw,22px);letter-spacing:-0.02em;color:#fff;
}
.group-title::before{
  content:'';width:10px;height:10px;border-radius:50%;
  background:#fff;box-shadow:0 0 0 4px rgba(255,255,255,0.08);
  flex-shrink:0;animation:dotPulse 2.4s ease-in-out infinite;
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
  transition:transform .55s var(--ease),box-shadow .55s var(--ease),
             opacity .55s var(--ease),filter .55s var(--ease),border-color .3s;
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
.bento .glass{padding:30px;display:flex;flex-direction:column}
.b-lg{grid-column:span 4;min-height:290px}
.b-md{grid-column:span 3;min-height:240px}
.b-sm{grid-column:span 2;min-height:230px}
.icon-box{
  width:48px;height:48px;border-radius:14px;
  display:grid;place-items:center;
  background:linear-gradient(150deg,rgba(255,255,255,0.1),rgba(255,255,255,0.02));
  border:1px solid var(--border-2);margin-bottom:22px;color:var(--text-dim);
  transition:color .3s,border-color .3s;flex-shrink:0;
}
.icon-box svg{width:21px;height:21px;display:block}
.glass:hover .icon-box{color:#fff;border-color:var(--border-3)}
.bento h3{font-family:'Unbounded',sans-serif;font-weight:600;font-size:17px;letter-spacing:-0.02em;line-height:1.3;margin-bottom:10px}
.bento p{color:var(--text-dim);font-size:14px;line-height:1.65;max-width:48ch}
.bento p code,.step p code,.showcase-list code{
  font-family:'JetBrains Mono',monospace;color:var(--text);font-size:.92em;
  background:rgba(255,255,255,0.04);padding:2px 7px;border-radius:6px;
  border:1px solid var(--border);white-space:nowrap;
}

.feature-list{
  list-style:none;display:flex;flex-direction:column;gap:12px;
  margin-top:22px;padding-top:20px;border-top:1px solid var(--border);
}
.feature-list li{display:flex;align-items:center;gap:12px;font-size:13.5px;color:var(--text-dim);line-height:1.5;}
.feature-list li::before{
  content:'';flex-shrink:0;width:6px;height:6px;border-radius:50%;
  background:#fff;box-shadow:0 0 0 3px rgba(255,255,255,0.08);
}
.feature-list li strong{color:#fff;font-weight:600}

.showcase{display:grid;grid-template-columns:1fr 1fr;gap:56px;align-items:center}
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

.stats{
  display:grid;grid-template-columns:repeat(4,1fr);gap:1px;
  border-radius:var(--radius);overflow:hidden;
  background:var(--border);border:1px solid var(--border);
}
.stat{
  background:rgba(10,10,10,0.7);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  padding:30px 20px;text-align:center;transition:background .35s;
}
.stat:hover{background:rgba(18,18,18,0.85)}
.stat-val{
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:clamp(20px,2.4vw,30px);letter-spacing:-0.035em;
  background:linear-gradient(160deg,#fff,rgba(255,255,255,0.5));
  -webkit-background-clip:text;background-clip:text;color:transparent;
  line-height:1.1;margin-bottom:8px;font-variant-numeric:tabular-nums;
}
.stat-lbl{
  font-family:'JetBrains Mono',monospace;font-size:10.5px;
  color:var(--text-mute);letter-spacing:0.08em;text-transform:uppercase;line-height:1.35;
}

.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;position:relative}
.steps::before{
  content:'';position:absolute;top:80px;left:12%;right:12%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.15) 20%,rgba(255,255,255,0.15) 80%,transparent);
  pointer-events:none;z-index:0;
}
.step{padding:30px;display:flex;flex-direction:column;position:relative;z-index:1}
.step-num{
  font-family:'JetBrains Mono',monospace;font-size:11.5px;
  color:var(--text-mute);letter-spacing:0.14em;margin-bottom:20px;text-transform:uppercase;
  display:inline-flex;align-items:center;gap:8px;
}
.step-num::before{content:'';width:8px;height:8px;border-radius:50%;background:#fff;box-shadow:0 0 0 4px rgba(255,255,255,0.1);display:inline-block;flex-shrink:0}
.step h3{font-family:'Unbounded',sans-serif;font-weight:600;font-size:17.5px;letter-spacing:-0.02em;margin-bottom:10px}
.step p{color:var(--text-dim);font-size:14px;line-height:1.65}

.cta{
  position:relative;border-radius:36px;padding:80px 40px;
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
.cta h2{font-size:clamp(28px,4.4vw,48px);margin-bottom:18px;letter-spacing:-0.04em}
.cta p{color:var(--text-dim);font-size:16px;max-width:500px;margin:0 auto 34px;line-height:1.65}
.cta .btn{padding:16px 34px;height:auto;font-size:15px;border-radius:14px}
.cta .btn svg{width:17px;height:17px}
.cta-note{font-size:12.5px;color:var(--text-mute);margin-top:22px;margin-bottom:0;font-family:'JetBrains Mono',monospace;letter-spacing:0.06em}

@media (max-width:1080px){
  .showcase{grid-template-columns:1fr;gap:44px}
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
  .hero{padding:140px 0 0}
  section{padding:70px 0}
  .hero-inner{padding:0}
  .tile-groups{gap:36px}
  .bento{grid-template-columns:1fr;gap:14px}
  .bento .glass{grid-column:span 1 !important;padding:26px;min-height:auto}
  .b-lg{min-height:auto}
  .stats{grid-template-columns:1fr 1fr}
  .stat{padding:24px 14px}
  .stat-val{font-size:clamp(18px,4.4vw,24px)}
  .cta{padding:56px 24px;border-radius:26px}
  .cta .btn{padding:15px 28px;font-size:14px;width:100%}
  .hero-cta{flex-direction:column;align-items:stretch}
  .hero-cta .btn{justify-content:center;width:100%}
  .marquee-track span{font-size:12px;gap:36px}
  .marquee-track{gap:36px}
  .marquee{margin-top:70px}
  .code-show{margin-top:46px;gap:6px;max-width:100%}
  .code-cell{max-width:42px;border-radius:11px}
  .badge{font-size:11.5px;padding:7px 13px 7px 8px;margin-bottom:28px;gap:8px}
  .badge-dot{width:18px;height:18px}
  .badge-dot svg{width:9px;height:9px}
  .group-head{flex-direction:column;gap:8px;align-items:flex-start}
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
                <path d="M8 3v5H3"/><path d="M16 3v5h5"/>
                <path d="M8 21v-5H3"/><path d="M16 21v-5h5"/>
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
                <path d="M6 13l3-2 2 2 3-3 4 4"/><path d="M8 17h8"/>
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
                <circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/><path d="M11 8v3.5l2.5 1.5"/>
              </svg>
            </div>
            <h3>Мгновенный поиск</h3>
            <p>Пост подгружается по мере ввода кода. Ввели последнюю цифру — пост уже на экране.</p>
          </div>
          <div class="glass b-sm tile-float-r reveal reveal--up" data-delay="140">
            <div class="icon-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 3v11"/><path d="M8 10l4 4 4-4"/><path d="M4 15v3a2 2 0 002 2h12a2 2 0 002-2v-3"/>
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
                <circle cx="17.5" cy="17.5" r="3.5"/><path d="m21 21-1.8-1.8"/>
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
          <li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg><span><strong>Кнопка «копировать ссылку»</strong> — в шапке каждой карточки</span></li>
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
        <p>Заголовок, текст и до 5 фото. Перетащите файлы, выберите через диалог или вставьте из буфера.</p>
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

    # Панорама со скриншотами — сразу после первого экрана
    body = body.replace(
        '<section id="features">',
        build_panorama_section() + '\n<section id="features">',
        1,
    )

    return render_shell(
        title="СЛД·NET — посты по 6-значному коду",
        body=body,
        extra_css=LANDING_CSS + PANORAMA_CSS,
        extra_js=PANORAMA_JS,
        og='<meta name="description" content="Публикуйте посты с фото, делитесь шестью цифрами. '
           'Без аккаунтов, с шифрованием и автосжатием.">',
    )


# ============================================================
# РАБОЧАЯ ОБЛАСТЬ
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
  --warn:#c4a054;
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
.cur-ring.hover{width:58px;height:58px;border-color:rgba(255,255,255,0.22);background:rgba(255,255,255,0.05);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
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

/* Кнопка «Наверх» */
.scroll-top{
  position:fixed;
  right:20px;
  bottom:20px;
  bottom:max(20px, env(safe-area-inset-bottom, 0px) + 12px);
  width:44px;height:44px;
  border-radius:12px;
  border:1px solid var(--border-2);
  background:rgba(15,15,15,0.85);
  backdrop-filter:blur(16px) saturate(160%);
  -webkit-backdrop-filter:blur(16px) saturate(160%);
  color:var(--text);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;
  z-index:250;
  opacity:0;
  transform:translateY(10px);
  pointer-events:none;
  transition:opacity .25s ease, transform .25s var(--ease),
             background .18s, border-color .18s, color .18s;
  -webkit-appearance:none;appearance:none;
  box-shadow:0 12px 32px -12px rgba(0,0,0,0.7), inset 0 1px 0 rgba(255,255,255,0.06);
}
.scroll-top.visible{ opacity:1; transform:translateY(0); pointer-events:auto; }
.scroll-top:hover{ background:rgba(25,25,25,0.95); border-color:var(--border-3); }
.scroll-top:active{ transform:scale(.94); }
.scroll-top svg{ width:18px; height:18px; pointer-events:none; display:block; }

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
  position:fixed;top:0;left:0;right:0;
  z-index:100;
  padding:14px;
  padding-top:max(14px, env(safe-area-inset-top, 0px) + 6px);
  pointer-events:none;
}
.topbar-inner{
  pointer-events:auto;
  display:grid;
  grid-template-columns:1fr auto 1fr;
  align-items:center;
  gap:14px;
  max-width:1160px;
  margin:0 auto;
  min-height:52px;
  padding:8px 8px 8px 16px;
  background:rgba(10,10,10,0.72);
  backdrop-filter:blur(24px) saturate(160%);
  -webkit-backdrop-filter:blur(24px) saturate(160%);
  border:1px solid var(--border);
  border-radius:16px;
  box-shadow:0 12px 40px rgba(0,0,0,0.5),inset 0 1px 0 rgba(255,255,255,0.06);
}

.logo{
  grid-column:1;
  justify-self:start;
  display:inline-flex;align-items:center;gap:10px;
  font-family:'Unbounded',sans-serif;font-weight:700;
  font-size:13.5px;letter-spacing:-0.01em;
  text-decoration:none;color:#fff;white-space:nowrap;
}
.logo-mark{
  width:28px;height:28px;border-radius:8px;
  background:linear-gradient(140deg,#fff,#c4c4c8);
  display:grid;place-items:center;position:relative;overflow:hidden;
  box-shadow:0 4px 14px rgba(0,0,0,0.5),inset 0 -1px 0 rgba(0,0,0,0.15);
  transition:transform .35s var(--ease);
  flex-shrink:0;
}
.logo:hover .logo-mark{transform:rotate(-6deg) scale(1.05)}
.logo-mark::after{content:'';position:absolute;inset:0;background:linear-gradient(150deg,rgba(255,255,255,0.9),transparent 55%);pointer-events:none}
.logo-mark svg{width:14px;height:14px;position:relative;z-index:1;color:#08080a;display:block}
.logo-word{display:inline-flex;align-items:baseline;gap:1px}
.logo-word .ldot{color:var(--text-mute);font-weight:400;margin:0 2px}
.logo-word .lnet{color:var(--text-dim);font-weight:500}

.menu{
  grid-column:2;
  justify-self:center;
  display:inline-flex;
  gap:4px;
  padding:4px;
  border-radius:12px;
  background:rgba(255,255,255,0.028);
  border:1px solid var(--border);
  flex-shrink:0;
}
.menu-switch{
  position:relative;
  display:inline-flex;
  gap:4px;
}
.menu-pill{
  position:absolute;
  top:0;bottom:0;left:0;
  width:0;
  border-radius:9px;
  background:rgba(255,255,255,0.09);
  border:1px solid rgba(255,255,255,0.13);
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.09);
  transform:translateX(0);
  transition:transform .34s var(--ease), width .34s var(--ease);
  z-index:0;pointer-events:none;
}
.tab{
  position:relative;z-index:1;
  display:inline-flex;align-items:center;gap:7px;
  padding:9px 14px;
  border-radius:9px;
  background:transparent;
  border:1px solid transparent;
  color:var(--text-dim);
  font:inherit;font-size:13px;font-weight:500;
  cursor:pointer;
  text-decoration:none;
  white-space:nowrap;
  -webkit-tap-highlight-color:transparent;
  transition:color .22s, background .22s, border-color .22s;
}
.tab svg{width:15px;height:15px;flex-shrink:0;display:block}
.tab:hover{ color:#fff; }
.tab:active{ transform:scale(.98); }
.tab.active{ color:#fff; }

.tab-exit-mobile{ display:none; }

.back-btn{
  grid-column:3;
  justify-self:end;
  display:inline-flex;align-items:center;gap:7px;
  padding:9px 14px;
  border-radius:10px;
  border:1px solid var(--border-2);
  background:var(--glass);
  color:var(--text-dim);
  font:inherit;font-size:13px;font-weight:500;
  text-decoration:none;white-space:nowrap;
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  transition:color .2s, background .2s, border-color .2s;
  -webkit-tap-highlight-color:transparent;
}
.back-btn svg{width:14px;height:14px;display:block}
.back-btn:hover{ color:#fff; background:var(--glass-hi); border-color:var(--border-3); }
.back-btn:active{ transform:scale(.98); }

@media (display-mode: standalone), (display-mode: fullscreen), (display-mode: minimal-ui) {
  .tab-exit-mobile { display:none !important; }
  .back-btn { display:none !important; }
  .logo { pointer-events:none; }
}

.app{
  min-height:100dvh;
  display:flex;flex-direction:column;align-items:center;
  padding:calc(var(--topbar-h, 80px) + 24px) 20px 40px;
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
.input-wrap .iw-icon{
  position:absolute;left:13px;width:16px;height:16px;
  color:var(--text-mute);pointer-events:none;transition:color .18s;
}
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

.input-wrap input.field{ padding-right:76px; }
.input-wrap textarea.field{ padding-right:76px; padding-bottom:32px; }

.input-count{
  position:absolute;
  right:14px;top:50%;
  transform:translateY(-50%);
  font-size:10.5px;
  font-family:'JetBrains Mono',monospace;
  letter-spacing:0.04em;
  pointer-events:none;
  color:var(--ok);
  opacity:0.75;
  transition:color .25s, opacity .2s;
  font-variant-numeric:tabular-nums;
  z-index:1;
}
.input-wrap.textarea-wrap .input-count{ top:auto; bottom:10px; transform:none; }
.input-wrap:focus-within .input-count{ opacity:1; }
.input-wrap.warn .input-count{ color:var(--warn); }
.input-wrap.max  .input-count{ color:var(--err); }

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

/* ============================================================
   ПОСТ (текст не выделяется)
   ============================================================ */
.post-header{
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  margin-bottom:14px;
}
.post-code{
  font-family:'JetBrains Mono',monospace;
  font-size:11.5px;font-weight:600;
  letter-spacing:0.14em;
  color:var(--text-mute);
  text-transform:uppercase;
  padding:6px 10px;
  border-radius:8px;
  border:1px solid var(--border);
  background:rgba(255,255,255,0.02);
}
.post-copy-btn{
  display:inline-flex;align-items:center;gap:7px;
  padding:7px 12px;
  border-radius:9px;
  border:1px solid var(--border);
  background:rgba(255,255,255,0.03);
  color:var(--text-dim);
  font:inherit;font-size:12px;font-weight:500;
  cursor:pointer;
  -webkit-tap-highlight-color:transparent;
  transition:background .18s, border-color .18s, color .18s;
  white-space:nowrap;
  -webkit-appearance:none;appearance:none;
}
.post-copy-btn:hover{ background:rgba(255,255,255,0.06); border-color:var(--border-2); color:#fff; }
.post-copy-btn.copied{ color:var(--ok); border-color:rgba(126,200,153,0.5); background:rgba(126,200,153,0.08); }
.post-copy-btn svg{ width:13px; height:13px; display:block; pointer-events:none; }
.post-copy-btn .sl-check{ display:none; }
.post-copy-btn.copied .sl-copy{ display:none; }
.post-copy-btn.copied .sl-check{ display:block; }

.post-title{
  margin:0 0 8px;
  font-family:'Unbounded',sans-serif;font-size:17px;font-weight:600;
  line-height:1.3;color:#fff;word-break:break-word;letter-spacing:-0.02em;
}
.post-meta{
  font-size:12px;color:var(--text-dim);
  margin-bottom:14px;display:flex;gap:12px;flex-wrap:wrap;align-items:center;
  font-family:'JetBrains Mono',monospace;letter-spacing:0.01em;
}
.post-body{
  font-size:14px;line-height:1.65;color:var(--text);
  white-space:pre-wrap;word-break:break-word;
}
.post-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px;margin-top:16px}
.post-gallery img{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:10px;cursor:zoom-in;background:var(--input);border:1px solid var(--border);transition:border-color .18s,transform .35s var(--ease)}
.post-gallery img:hover{border-color:var(--border-3);transform:translateY(-2px)}

/* LIGHTBOX */
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
  transition:opacity .18s ease;
}
.lb-img.loading{opacity:0.25}
.lb-loading{
  position:absolute;top:50%;left:50%;
  width:30px;height:30px;margin:-15px 0 0 -15px;
  border:2px solid rgba(255,255,255,0.15);
  border-top-color:#fff;
  border-radius:50%;animation:spin .8s linear infinite;
  z-index:3;pointer-events:none;opacity:0;transition:opacity .18s ease;
}
.lightbox.loading .lb-loading{opacity:1}
.lb-btn{position:absolute;width:42px;height:42px;border-radius:12px;border:1px solid var(--border-2);background:rgba(15,15,15,0.8);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:var(--text);display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:2;transition:background .18s,border-color .18s,transform .18s}
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
  text-align:center;animation:modalPop .32s var(--ease);
}
@keyframes modalPop{from{opacity:0;transform:translateY(14px) scale(.96)}to{opacity:1;transform:translateY(0) scale(1)}}
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
  .topbar-inner{padding:8px 8px 8px 14px;}
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

@media (max-width: 720px){
  html, body {
    height: 100%;overflow: hidden;overscroll-behavior: none;
    background: var(--bg);
  }
  .app {
    position: fixed;inset: 0;height: 100dvh;min-height: 0;overflow: hidden;
    padding: calc(var(--topbar-h, 100px) + 12px) 14px calc(18px + env(safe-area-inset-bottom, 0px));
    display: flex;flex-direction: column;align-items: center;
  }
  .stage {
    width: 100%;max-width: 560px;max-height: 100%;
    overflow-y: auto;overflow-x: hidden;
    -webkit-overflow-scrolling: touch;overscroll-behavior: contain;
    scrollbar-width: none;
    margin: auto 0;padding: 4px 0;
  }
  .stage::-webkit-scrollbar{ display:none; }

  .topbar{padding:10px;padding-top:max(10px, env(safe-area-inset-top, 0px) + 4px);}
  .topbar-inner{
    min-height:48px;
    padding:6px 6px 6px 10px;
    gap:10px;
    border-radius:14px;
    grid-template-columns: 1fr;
    justify-items: center;
  }
  .topbar .logo { display: none; }
  .topbar .menu { grid-column:1; justify-self:center; padding:3px; }
  .topbar .back-btn { display: none; }
  .tab-exit-mobile{ display: inline-flex; }

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

  /* На мобильном кнопка наверх компактнее и над тостами */
  .scroll-top{
    right: 16px;
    bottom: max(16px, env(safe-area-inset-bottom, 0px) + 12px);
    width: 42px; height: 42px;
  }
}
@media (max-width: 480px){
  .tab span{ display: inline; }
  .tab svg{ width: 14px; height: 14px; }
  .tab{ padding: 9px 10px; font-size: 12.5px; }
}
@media (max-width: 380px){
  .tab span{ display: none; }
  .tab{ padding: 10px 11px; }
  .tab svg{ width: 16px; height: 16px; }
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
      <div class="menu-switch" id="menuSwitch">
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
      <a href="/" class="tab tab-exit-mobile" aria-label="Выйти на главную">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>
        <span>Выйти</span>
      </a>
    </div>

    <a href="/" class="back-btn" aria-label="На главную">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5M11 6l-6 6 6 6"/></svg>
      <span>На главную</span>
    </a>
  </div>
</nav>

<main class="app">
  <div class="stage" id="stage" hidden>

    <div class="panel" id="createPanel">
      <section class="card">
        <div class="input-wrap" id="titleWrap">
          <input class="field" id="title" type="text" maxlength="120" autocomplete="off" spellcheck="false" placeholder="Название">
          <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7V5a1 1 0 011-1h14a1 1 0 011 1v2"/><path d="M9 20h6"/><path d="M12 4v16"/></svg>
          <span class="input-count" id="titleCount">0/120</span>
        </div>

        <div class="input-wrap textarea-wrap" id="contentWrap">
          <textarea class="field" id="content" maxlength="20000" placeholder="Содержимое"></textarea>
          <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
          <span class="input-count" id="contentCount">0/20000</span>
        </div>

        <div class="drop" id="drop">
          <svg class="drop-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="9" cy="9" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg>
          <div class="drop-label" id="dropLabel">Нажмите или перетащите фото</div>
          <div class="drop-hint">до 5 фото · до 5 МБ · сжатие до ~60 КБ</div>
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

<button class="scroll-top" id="scrollTopBtn" type="button" aria-label="Наверх">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M12 19V5M5 12l7-7 7 7"/>
  </svg>
</button>

<script>__UI_FRAMEWORK__</script>
<script>
(function(){
  "use strict";
  var $ = function(id){ return document.getElementById(id); };

  document.addEventListener("contextmenu", function(e){ e.preventDefault(); });

  /* ============ SERVICE WORKER ============ */
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function(){
      navigator.serviceWorker.register('/sw.js', { scope: '/' })
        .catch(function(err){ console.warn('SW register failed:', err); });
    });
  }

  /* ============ PWA detection ============ */
  try {
    var standalone = window.matchMedia('(display-mode: standalone)').matches
                  || window.matchMedia('(display-mode: fullscreen)').matches
                  || window.navigator.standalone === true;
    if (standalone) document.documentElement.classList.add('standalone');
  } catch(e) {}

  /* ============ CURSOR ============ */
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
    if (!ring.classList.contains('hover')) ring.style.borderColor = 'rgba(255,255,255,' + borderOp.toFixed(2) + ')';
    requestAnimationFrame(loop);
  })();

  addEventListener("mousedown", function(){ ring.classList.add("click"); });
  addEventListener("mouseup", function(){ ring.classList.remove("click"); });
  UI.qsa('a, button, input, textarea, label, .tab, .drop, .post-gallery img, .post-copy-btn').forEach(function(el){
    UI.on(el, 'mouseenter', function(){ ring.classList.add('hover'); });
    UI.on(el, 'mouseleave', function(){ ring.classList.remove('hover'); });
  });

  /* ============ LOADER ============ */
  var loader = $('pageLoader');
  if (loader) {
    var hideLoader = function(){ loader.classList.add('hidden'); };
    if (document.readyState === 'complete') setTimeout(hideLoader, 250);
    else { addEventListener('load', function(){ setTimeout(hideLoader, 250); }); setTimeout(hideLoader, 2500); }
  }

  /* ============ TOPBAR HEIGHT ============ */
  var topbar = $('topbar');
  function measureTopbar(){
    document.documentElement.style.setProperty('--topbar-h', topbar.offsetHeight + 'px');
  }
  measureTopbar();
  addEventListener('resize', measureTopbar, { passive: true });
  addEventListener('orientationchange', function(){ setTimeout(measureTopbar, 300); });

  /* ============ КНОПКА «НАВЕРХ» ============ */
  var scrollTopBtn = $('scrollTopBtn');
  var stageEl = $('stage');
  var SCROLL_SHOW_AT = 240;

  function currentScrollTop(){
    var a = window.scrollY || window.pageYOffset || 0;
    var b = stageEl ? stageEl.scrollTop : 0;
    return Math.max(a, b);
  }
  function applyScrollTopBtn(){
    scrollTopBtn.classList.toggle('visible', currentScrollTop() > SCROLL_SHOW_AT);
  }
  applyScrollTopBtn();

  var stTicking = false;
  function onAnyScroll(){
    if (stTicking) return;
    stTicking = true;
    requestAnimationFrame(function(){ applyScrollTopBtn(); stTicking = false; });
  }
  addEventListener('scroll', onAnyScroll, { passive: true });
  if (stageEl) stageEl.addEventListener('scroll', onAnyScroll, { passive: true });
  addEventListener('resize', applyScrollTopBtn, { passive: true });

  scrollTopBtn.addEventListener('click', function(){
    try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch(e){ window.scrollTo(0, 0); }
    if (stageEl && stageEl.scrollTop > 0) {
      try { stageEl.scrollTo({ top: 0, behavior: 'smooth' }); } catch(e){ stageEl.scrollTop = 0; }
    }
    setTimeout(applyScrollTopBtn, 60);
  });

  /* ============ ICONS ============ */
  var ICONS = {
    error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
    ok:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>',
    copy:  '<svg class="sl-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>',
    check: '<svg class="sl-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>'
  };

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
  var menuEl = $("menu"), menuSwitch = $("menuSwitch"), menuPill = $("menuPill");
  var mode = null;

  function updateMenuPill(){
    if (!menuPill || !menuSwitch) return;
    var activeTab = mode === "find" ? btnFind : btnCreate;
    menuPill.style.width = activeTab.offsetWidth + "px";
    menuPill.style.transform = "translateX(" + activeTab.offsetLeft + "px)";
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
      var newName = file.name.replace(/\.[^.]+$/, "") + OUT_EXT;
      return new File([best], newName, { type: OUT_MIME });
    } catch (e) {
      console.warn("compress failed:", e);
      return file;
    }
  }

  /* ============ CREATE ============ */
  var MAX_PHOTOS = 5;
  var MAX_TITLE = 120;
  var MAX_CONTENT = 20000;
  var selectedFiles = [];
  var drop = $("drop"), dropLabel = $("dropLabel"), fileInput = $("fileInput");
  var previews = $("previews"), createMsg = $("createMsg"), submitBtn = $("submitBtn");
  var titleInput = $("title"), contentInput = $("content"), ogCheckbox = $("ogEnabled");
  var titleWrap = $("titleWrap"), contentWrap = $("contentWrap");
  var titleCount = $("titleCount"), contentCount = $("contentCount");

  function updateCounter(input, countEl, wrapEl, max){
    var len = input.value.length;
    if (len > max) { input.value = input.value.slice(0, max); len = max; }
    countEl.textContent = len + "/" + max;
    var ratio = len / max;
    wrapEl.classList.toggle('warn', ratio >= 0.8 && len < max);
    wrapEl.classList.toggle('max',  len >= max);
  }
  function refreshCounters(){
    updateCounter(titleInput, titleCount, titleWrap, MAX_TITLE);
    updateCounter(contentInput, contentCount, contentWrap, MAX_CONTENT);
  }
  UI.on(titleInput, 'input', refreshCounters);
  UI.on(contentInput, 'input', refreshCounters);
  refreshCounters();

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
    if (rejected > 0) {
      var msg = "Пропущено: " + rejected + ". Только изображения, не больше " + MAX_PHOTOS + " и не тяжелее 5 МБ.";
      showCreateMsg("err", msg);
      UI.toast(msg, { kind: 'err' });
    } else clearCreateMsg();
    if (!accepted.length) return;

    drop.classList.add("busy");
    dropLabel.textContent = "Сжимаем фото...";

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
      drop.classList.remove("busy");
      dropLabel.textContent = defaultDropLabel();
    }
  }

  function renderPreviews(){
    previews.innerHTML = "";
    selectedFiles.forEach(function(file, index){
      var url = URL.createObjectURL(file);
      var img = UI.h('img', { src: url, alt: file.name });
      img.addEventListener("load", function(){ URL.revokeObjectURL(url); }, { once: true });
      var kb = Math.max(1, Math.round(file.size / 1024));
      var rm = UI.h('button', {
        type: 'button', text: '×', title: '×',
        on: { click: function(){ selectedFiles.splice(index, 1); renderPreviews(); } }
      });
      previews.appendChild(UI.h('div', { class: 'preview' }, [
        img,
        UI.h('div', { class: 'pv-badge', text: kb + ' КБ' }),
        rm
      ]));
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
    renderPreviews(); clearCreateMsg(); refreshCounters(); titleInput.focus();
  });

  async function postForm(fd, attempt){
    if (attempt === undefined) attempt = 0;
    try {
      var res = await fetch("/api/posts", { method: "POST", body: fd });
      return res;
    } catch (e) {
      if (attempt < 1) {
        await new Promise(function(r){ setTimeout(r, 400); });
        return postForm(fd, attempt + 1);
      }
      throw e;
    }
  }

  async function doPublish(){
    var title = titleInput.value.trim();
    var content = contentInput.value.trim();
    if (!title) { showCreateMsg("err", "Введите название поста."); titleInput.focus(); return; }

    var fd = new FormData();
    fd.append("title", title);
    fd.append("content", content);
    fd.append("og_enabled", ogCheckbox.checked ? "true" : "false");
    selectedFiles.forEach(function(f){ fd.append("files", f, f.name); });

    var label = submitBtn.querySelector("span");
    submitBtn.disabled = true;
    var oldLabel = label ? label.textContent : "";
    if (label) label.textContent = "Публикация...";
    clearCreateMsg();

    try {
      var res = await postForm(fd, 0);
      if (!res.ok) { showCreateMsg("err", await readError(res)); return; }

      var data = await res.json().catch(function(){ return null; });
      if (!data || !data.code) { showCreateMsg("err", "Некорректный ответ сервера"); return; }

      titleInput.value = ""; contentInput.value = ""; ogCheckbox.checked = true;
      selectedFiles = []; renderPreviews(); clearCreateMsg(); refreshCounters();

      var code = data.code;
      setUrlForCode(code);
      setMode("find");
      otpCells.forEach(function(c, i){ c.value = code[i] || ""; });
      lastSubmitted = code;
      updateOtpCopyState();
      runSearch(code);
      showCreatedModal(code, data.compressed_bytes, data.share_url || postUrl(code));
      UI.toast("Пост создан · #" + code, { kind: 'ok' });
    } catch (e) {
      showCreateMsg("err", "Ошибка сети: " + e.message);
    } finally {
      submitBtn.disabled = false;
      if (label) label.textContent = oldLabel;
    }
  }

  submitBtn.addEventListener("click", doPublish);

  document.addEventListener("keydown", function(e){
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && mode === "create") {
      e.preventDefault(); doPublish();
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
    var ok = await UI.copy(modalShareUrl);
    if (label) label.textContent = ok ? "Скопировано" : "Ошибка";
    if (ok) UI.toast("Ссылка скопирована", { kind: 'ok', duration: 1400 });
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
  var postCache = new Map();
  var postCacheETag = new Map();

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
    var ok = await UI.copy(code);
    if (ok) {
      otpCopyBtn.classList.add("copied");
      UI.toast("Код скопирован", { kind: 'ok', duration: 1200 });
      clearTimeout(otpCopyTimer);
      otpCopyTimer = setTimeout(function(){ otpCopyBtn.classList.remove("copied"); }, 1500);
    }
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
  function openLightbox(code, photos, index){
    lbCode = code; lbPhotos = photos; lbIndex = index;
    zoom = 1; panX = 0; panY = 0; swipeY = 0;
    lbTransform.style.transition = ""; applyTransform();
    lightbox.classList.add('loading');
    clearTimeout(lbLoadTimer);
    lbLoadTimer = setTimeout(lbStopLoading, 8000);
    lbImg.src = "/api/photos/" + encodeURIComponent(code) + "/" + index;
    lbImg.alt = (photos[index] && photos[index].name) || "";
    lbCounter.textContent = (index + 1) + " / " + photos.length;
    lbPrev.hidden = photos.length < 2; lbNext.hidden = photos.length < 2;
    lightbox.hidden = false;
  }
  function closeLightbox(){
    stopInertia();
    lightbox.hidden = true;
    lbImg.removeAttribute("src");
    lbPhotos = []; lbCode = null;
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
    lbImg.src = "/api/photos/" + encodeURIComponent(lbCode) + "/" + lbIndex;
    lbImg.alt = (lbPhotos[lbIndex] && lbPhotos[lbIndex].name) || "";
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
      e.preventDefault();
      stopInertia();
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
  var tStartPanX = 0, tStartPanY = 0;
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
      tPinchStartPanX = panX;
      tPinchStartPanY = panY;
    } else if (e.touches.length === 1) {
      tStartX = e.touches[0].clientX;
      tStartY = e.touches[0].clientY;
      tLastX = tStartX; tLastY = tStartY;
      tStartTime = performance.now();
      tLastFrameTime = tStartTime;
      tStartPanX = panX; tStartPanY = panY;
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
        tLastFrameTime = now;
        tLastX = tx; tLastY = ty;
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
      tTapTime = now;
      tTapX = t.clientX; tTapY = t.clientY;
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

  /* ============ POST RENDER ============ */
  function renderPost(post){
    UI.clear(searchFrame);

    var code = post.code;
    var url = postUrl(code);

    // --- Верхняя шапка: код + кнопка «Копировать ссылку» ---
    var copyBtn = UI.h('button', {
      type: 'button',
      class: 'post-copy-btn',
      title: 'Копировать ссылку на пост',
      'aria-label': 'Копировать ссылку на пост',
      html: ICONS.copy + ICONS.check
    }, [ UI.h('span', { text: 'Копировать ссылку' }) ]);
    var copyBtnTimer = null;
    UI.on(copyBtn, 'click', async function(e){
      e.stopPropagation();
      var ok = await UI.copy(url);
      copyBtn.classList.toggle('copied', ok);
      if (ok) UI.toast('Ссылка скопирована', { kind: 'ok', duration: 1400 });
      clearTimeout(copyBtnTimer);
      copyBtnTimer = setTimeout(function(){ copyBtn.classList.remove('copied'); }, 1500);
    });

    var header = UI.h('div', { class: 'post-header' }, [
      UI.h('span', { class: 'post-code', text: '#' + code }),
      copyBtn
    ]);
    searchFrame.appendChild(header);

    // --- Заголовок поста ---
    searchFrame.appendChild(UI.h('h3', { class: 'post-title', text: post.title }));

    // --- Метаданные ---
    var meta = UI.h('div', { class: 'post-meta' });
    try {
      meta.appendChild(UI.h('span', { text: new Date(post.created).toLocaleString('ru-RU') }));
    } catch(e){}
    if (post.photos && post.photos.length) {
      meta.appendChild(UI.h('span', { text: 'Фото: ' + post.photos.length }));
    }
    searchFrame.appendChild(meta);

    // --- Тело ---
    if (post.content) {
      searchFrame.appendChild(UI.h('div', { class: 'post-body', text: post.content }));
    }

    // --- Галерея ---
    if (post.photos && post.photos.length) {
      var gallery = UI.h('div', { class: 'post-gallery' });
      post.photos.forEach(function(p, idx){
        var img = UI.h('img', {
          src: '/api/photos/' + encodeURIComponent(code) + '/' + idx,
          alt: p.name || 'photo',
          title: p.name || '',
          loading: 'lazy',
          decoding: 'async',
          on: { click: function(){ openLightbox(code, post.photos, idx); } }
        });
        gallery.appendChild(img);
      });
      searchFrame.appendChild(gallery);
    }

    showSearchFrame();
  }

  async function runSearch(code){
    var mySeq = ++searchSeq;
    renderSpinner();
    await new Promise(function(r){ setTimeout(r, 300); });
    if (mySeq !== searchSeq) return;

    try {
      if (postCache.has(code)) {
        renderPost(postCache.get(code));
        (async function(){
          try {
            var etag = postCacheETag.get(code);
            var h = {};
            if (etag) h["If-None-Match"] = etag;
            var r = await fetch("/api/posts/" + encodeURIComponent(code), { headers: h });
            if (r.status === 304) return;
            if (r.ok) {
              var fresh = await r.json();
              if (fresh && fresh.code) {
                postCache.set(code, fresh);
                var newTag = r.headers.get("etag");
                if (newTag) postCacheETag.set(code, newTag);
                if (mySeq === searchSeq) renderPost(fresh);
              }
            } else if (r.status === 404) {
              postCache.delete(code);
              postCacheETag.delete(code);
              if (mySeq === searchSeq) renderError("Пост не найден");
            }
          } catch(e){}
        })();
        return;
      }

      var res = await fetch("/api/posts/" + encodeURIComponent(code));
      if (mySeq !== searchSeq) return;
      if (!res.ok) {
        var errText = (await readError(res)) || "Пост не найден";
        renderError(errText);
        UI.toast(errText, { kind: 'err' });
        shakeOtp(); setTimeout(clearOtp, 320);
        return;
      }
      var post = await res.json().catch(function(){ return null; });
      if (mySeq !== searchSeq) return;
      if (!post) { renderError("Пост не найден"); return; }

      postCache.set(code, post);
      var tag = res.headers.get("etag");
      if (tag) postCacheETag.set(code, tag);

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
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(updateMenuPill);
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


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(build_landing())


@app.get("/app", response_class=HTMLResponse)
async def app_page():
    return HTMLResponse(_render_app())


@app.get("/p/{code}", response_class=HTMLResponse)
async def post_page(code: str, request: Request):
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return HTMLResponse(_render_app())

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

    return HTMLResponse(_render_app(og_tags=og_tags, title_override=title_override))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
