"""
Sld-Networking — посты с 6-значным кодом (без повторяющихся цифр).
Хранение: оперативная память, AES-256-GCM + zstd/gzip.

Маршруты:
  /      — лендинг
  /app   — рабочая область (создание / поиск постов)
  /api/* — REST API

Запуск: pip install fastapi uvicorn python-multipart cryptography zstandard && python main.py
"""

from __future__ import annotations

import gzip
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
from fastapi.responses import HTMLResponse, JSONResponse

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
MAX_PHOTO_BYTES = 12 * 1024 * 1024
MAX_TITLE_LEN = 120
MAX_CONTENT_LEN = 20_000


def _new_code() -> str:
    """6-значный код БЕЗ повторяющихся цифр."""
    with _lock:
        for _ in range(5000):
            digits = list(string.digits)
            code_chars = []
            for _ in range(6):
                idx = secrets.randbelow(len(digits))
                code_chars.append(digits.pop(idx))
            code = "".join(code_chars)
            if code not in _store:
                return code
    raise HTTPException(503, "Storage overflow")


# ---------- приложение ----------
app = FastAPI(title="Sld-Networking", docs_url=None, redoc_url=None)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"},
    )


@app.post("/api/posts")
async def create_post(
    title: str = Form(...),
    content: str = Form(""),
    files: Optional[List[UploadFile]] = File(None),
):
    title = (title or "").strip()
    content = (content or "").strip()

    if not title:
        raise HTTPException(400, "Title is required")
    if len(title) > MAX_TITLE_LEN:
        raise HTTPException(400, f"Title longer than {MAX_TITLE_LEN}")
    if len(content) > MAX_CONTENT_LEN:
        raise HTTPException(400, f"Content longer than {MAX_CONTENT_LEN}")

    files = [f for f in (files or []) if f and f.filename]
    if len(files) > MAX_PHOTOS:
        raise HTTPException(400, f"Max {MAX_PHOTOS} photos")

    photos = []
    total = 0
    for f in files:
        data = await f.read()
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(
                400,
                f"File «{f.filename}» larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB",
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
        }

    return {"code": code, "compressed_bytes": total, "photos": len(photos)}


@app.get("/api/posts/{code}")
async def get_post(code: str):
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(400, "Code must be 6 digits")

    with _lock:
        entry = _store.get(code)
    if entry is None:
        raise HTTPException(404, "Post not found")

    try:
        meta = _unpack_meta(entry["meta"])
    except (InvalidTag, ValueError, OSError) as e:
        raise HTTPException(500, f"Decryption failed: {e}")

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
        raise HTTPException(404, "Post not found")

    photos = entry["photos"]
    if idx < 0 or idx >= len(photos):
        raise HTTPException(404, "Photo not found")

    p = photos[idx]
    try:
        data = _decrypt(p["enc"])
    except (InvalidTag, ValueError) as e:
        raise HTTPException(500, f"Decryption failed: {e}")

    return Response(
        content=data,
        media_type=p["mime"],
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Length": str(len(data)),
        },
    )


@app.get("/api/stats")
async def stats():
    with _lock:
        return {"posts": len(_store)}


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

# ============================================================
# ЛЕНДИНГ  (/)
# ============================================================
LANDING = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>СЛД·NET — нетворкинг на 6 цифрах</title>
<link rel="icon" href="__FAVICON__">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;500;600;700;800&family=Manrope:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script>
  // редирект на /app, если в URL есть 6-значный код поста
  if (/^#\d{6}/.test(location.hash)) {
    location.replace('/app' + location.hash);
  }
</script>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

  :root{
    --bg:#080808;
    --border:rgba(255,255,255,0.07);
    --border-2:rgba(255,255,255,0.12);
    --border-3:rgba(255,255,255,0.2);
    --text:#f4f4f5;
    --text-dim:#9a9a9e;
    --text-mute:#6a6a6e;
    --glass:rgba(255,255,255,0.035);
    --glass-hi:rgba(255,255,255,0.07);
    --radius:20px;
  }

  html{scroll-behavior:smooth}

  body{
    background:var(--bg);
    color:var(--text);
    font-family:'Manrope',system-ui,sans-serif;
    font-size:16px;
    line-height:1.6;
    overflow-x:hidden;
    min-height:100vh;
    -webkit-font-smoothing:antialiased;
  }

  ::selection{background:#fff;color:#000}

  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-track{background:#0a0a0a}
  ::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:8px;border:2px solid #0a0a0a}
  ::-webkit-scrollbar-thumb:hover{background:#3a3a3a}

  @media (hover:hover) and (pointer:fine){
    body,a,button,input,textarea{cursor:none}
  }
  .cur-dot,.cur-ring{
    position:fixed;top:0;left:0;
    pointer-events:none;z-index:99999;
    border-radius:50%;
    will-change:transform;
  }
  .cur-dot{
    width:6px;height:6px;
    background:#fff;
    box-shadow:0 0 0 1px rgba(255,255,255,0.3);
  }
  .cur-ring{
    width:36px;height:36px;
    border:1px solid rgba(255,255,255,0.35);
    transition:width .25s cubic-bezier(.2,.8,.2,1),
               height .25s cubic-bezier(.2,.8,.2,1),
               border-color .25s,background .25s;
  }
  .cur-ring.hover{
    width:64px;height:64px;
    border-color:rgba(255,255,255,0.15);
    background:rgba(255,255,255,0.06);
    backdrop-filter:blur(4px);
    -webkit-backdrop-filter:blur(4px);
  }
  .cur-ring.click{width:26px;height:26px;background:rgba(255,255,255,0.15)}
  @media (max-width:900px),(hover:none){.cur-dot,.cur-ring{display:none}}

  .bg{position:fixed;inset:0;z-index:-2;overflow:hidden;background:var(--bg)}
  .halo{position:absolute;border-radius:50%;filter:blur(120px);opacity:.5}
  .halo-1{
    width:640px;height:640px;
    background:radial-gradient(circle,rgba(255,255,255,0.09),transparent 65%);
    top:-220px;left:-120px;
    animation:drift1 26s ease-in-out infinite;
  }
  .halo-2{
    width:520px;height:520px;
    background:radial-gradient(circle,rgba(255,255,255,0.055),transparent 65%);
    top:35%;right:-160px;
    animation:drift2 32s ease-in-out infinite;
  }
  .halo-3{
    width:560px;height:560px;
    background:radial-gradient(circle,rgba(255,255,255,0.04),transparent 65%);
    bottom:-180px;left:30%;
    animation:drift3 36s ease-in-out infinite;
  }
  @keyframes drift1{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(100px,80px) scale(1.12)}}
  @keyframes drift2{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(-120px,60px) scale(1.15)}}
  @keyframes drift3{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(70px,-90px) scale(.92)}}

  .grid-bg{
    position:fixed;inset:0;z-index:-1;pointer-events:none;
    background-image:
      linear-gradient(rgba(255,255,255,0.022) 1px,transparent 1px),
      linear-gradient(90deg,rgba(255,255,255,0.022) 1px,transparent 1px);
    background-size:72px 72px;
    mask-image:radial-gradient(ellipse 90% 70% at 50% 0%,#000 15%,transparent 80%);
    -webkit-mask-image:radial-gradient(ellipse 90% 70% at 50% 0%,#000 15%,transparent 80%);
  }

  .grain{
    position:fixed;inset:0;z-index:9998;pointer-events:none;
    opacity:.035;mix-blend-mode:overlay;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }

  .wrap{max-width:1200px;margin:0 auto;padding:0 28px}

  nav{
    position:fixed;top:20px;left:50%;
    transform:translateX(-50%);
    z-index:100;
    width:calc(100% - 40px);
    max-width:1160px;
    border-radius:18px;
    padding:11px 12px 11px 22px;
    display:flex;align-items:center;justify-content:space-between;
    gap:20px;
    background:rgba(15,15,15,0.6);
    backdrop-filter:blur(24px) saturate(160%);
    -webkit-backdrop-filter:blur(24px) saturate(160%);
    border:1px solid var(--border);
    box-shadow:0 12px 40px rgba(0,0,0,0.5),inset 0 1px 0 rgba(255,255,255,0.06);
    transition:background .3s,box-shadow .3s,border-color .3s;
  }
  nav.scrolled{
    background:rgba(8,8,8,0.82);
    border-color:var(--border-2);
    box-shadow:0 16px 50px rgba(0,0,0,0.65),inset 0 1px 0 rgba(255,255,255,0.08);
  }

  .logo{
    display:inline-flex;align-items:center;gap:11px;
    font-family:'Unbounded',sans-serif;
    font-weight:700;
    font-size:14.5px;
    letter-spacing:-0.01em;
    text-decoration:none;
    color:#fff;
    white-space:nowrap;
  }
  .logo-mark{
    width:30px;height:30px;
    border-radius:9px;
    background:linear-gradient(140deg,#fff,#c4c4c8);
    display:grid;place-items:center;
    position:relative;overflow:hidden;
    box-shadow:0 4px 14px rgba(0,0,0,0.5),inset 0 -1px 0 rgba(0,0,0,0.15);
    transition:transform .35s cubic-bezier(.2,.8,.2,1);
  }
  .logo:hover .logo-mark{transform:rotate(-6deg) scale(1.05)}
  .logo-mark::after{
    content:'';position:absolute;inset:0;
    background:linear-gradient(150deg,rgba(255,255,255,0.9),transparent 55%);
    pointer-events:none;
  }
  .logo-mark svg{width:15px;height:15px;position:relative;z-index:1;color:#08080a}
  .logo-word{display:inline-flex;align-items:baseline;gap:1px}
  .logo-word .ldot{color:var(--text-mute);font-weight:400;margin:0 2px}
  .logo-word .lnet{color:var(--text-dim);font-weight:500}

  .nav-links{display:flex;gap:2px;align-items:center}
  .nav-links a{
    color:var(--text-dim);text-decoration:none;
    font-size:13.5px;font-weight:500;
    padding:8px 14px;border-radius:10px;
    transition:color .2s,background .2s;
  }
  .nav-links a:hover{color:#fff;background:rgba(255,255,255,0.05)}

  .btn{
    display:inline-flex;align-items:center;justify-content:center;
    gap:8px;
    font-family:'Manrope',sans-serif;
    font-weight:600;font-size:13.5px;
    letter-spacing:-0.005em;
    padding:11px 20px;
    height:42px;
    border-radius:11px;
    border:1px solid transparent;
    text-decoration:none;
    position:relative;overflow:hidden;white-space:nowrap;
    transition:transform .3s cubic-bezier(.2,.8,.2,1),background .25s,border-color .25s,color .25s,box-shadow .3s;
    -webkit-tap-highlight-color:transparent;
    user-select:none;
  }
  .btn svg{width:15px;height:15px;flex-shrink:0;transition:transform .35s cubic-bezier(.2,.8,.2,1)}

  .btn-primary{
    background:#f4f4f5;color:#08080a;
    box-shadow:0 1px 0 rgba(255,255,255,0.7) inset,0 -1px 0 rgba(0,0,0,0.12) inset,0 6px 22px -6px rgba(255,255,255,0.22);
  }
  .btn-primary:hover{
    transform:translateY(-1px);
    background:#fff;
    box-shadow:0 1px 0 rgba(255,255,255,0.9) inset,0 -1px 0 rgba(0,0,0,0.15) inset,0 10px 32px -6px rgba(255,255,255,0.3);
  }
  .btn-primary:active{transform:translateY(0) scale(.985)}
  .btn-primary:hover svg{transform:translateX(2px)}

  .btn-ghost{
    background:var(--glass);color:#fff;border-color:var(--border-2);
    backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
    box-shadow:inset 0 1px 0 rgba(255,255,255,0.08),0 4px 14px rgba(0,0,0,0.25);
  }
  .btn-ghost:hover{
    background:var(--glass-hi);
    border-color:var(--border-3);
    transform:translateY(-1px);
    box-shadow:inset 0 1px 0 rgba(255,255,255,0.12),0 8px 22px rgba(0,0,0,0.35);
  }
  .btn-ghost:active{transform:translateY(0) scale(.985)}

  .hero{padding:200px 0 100px;position:relative}
  .hero-inner{max-width:820px;margin:0 auto;text-align:center}

  .badge{
    display:inline-flex;align-items:center;gap:9px;
    padding:6px 14px 6px 8px;
    border-radius:100px;
    background:var(--glass);border:1px solid var(--border-2);
    backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
    font-size:12.5px;font-weight:500;color:var(--text-dim);
    letter-spacing:-0.005em;margin-bottom:34px;
    animation:rise .8s cubic-bezier(.2,.8,.2,1) both;
  }
  .badge-dot{
    width:22px;height:22px;border-radius:50%;
    background:linear-gradient(140deg,#fff,#b5b5ba);
    display:grid;place-items:center;color:#0a0a0a;
    position:relative;flex-shrink:0;
  }
  .badge-dot::after{
    content:'';position:absolute;inset:-4px;border-radius:50%;
    border:1px solid rgba(255,255,255,0.25);
    animation:ping 2.2s ease-out infinite;
  }
  @keyframes ping{0%{transform:scale(1);opacity:1}100%{transform:scale(1.7);opacity:0}}
  .badge-dot svg{width:10px;height:10px}

  h1{
    font-family:'Unbounded',sans-serif;
    font-weight:700;
    font-size:clamp(38px,7.6vw,86px);
    line-height:0.98;letter-spacing:-0.045em;
    margin-bottom:26px;
    animation:rise .9s .05s cubic-bezier(.2,.8,.2,1) both;
  }
  h1 .dim{color:var(--text-mute);font-weight:400}

  .lead{
    font-size:clamp(15.5px,1.7vw,18px);
    line-height:1.65;color:var(--text-dim);
    max-width:580px;margin:0 auto 40px;
    animation:rise 1s .12s cubic-bezier(.2,.8,.2,1) both;
  }
  .lead strong{color:#e8e8ea;font-weight:600}

  .hero-cta{
    display:flex;gap:12px;justify-content:center;
    flex-wrap:wrap;
    animation:rise 1s .22s cubic-bezier(.2,.8,.2,1) both;
  }
  .hero-cta .btn{padding:14px 26px;height:auto;font-size:14.5px;border-radius:13px}
  .hero-cta .btn svg{width:16px;height:16px}

  @keyframes rise{from{opacity:0;transform:translateY(24px);filter:blur(6px)}to{opacity:1;transform:translateY(0);filter:blur(0)}}

  .code-show{
    margin:70px auto 0;max-width:520px;
    display:flex;gap:8px;justify-content:center;flex-wrap:nowrap;
    animation:rise 1s .32s cubic-bezier(.2,.8,.2,1) both;
  }
  .code-cell{
    flex:1 1 0;max-width:64px;aspect-ratio:2/3;
    border-radius:12px;
    background:linear-gradient(160deg,rgba(255,255,255,0.05),rgba(255,255,255,0.015));
    border:1px solid var(--border-2);
    backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
    display:grid;place-items:center;
    font-family:'JetBrains Mono',monospace;font-weight:600;
    font-size:clamp(20px,3.4vw,26px);color:#fff;
    box-shadow:inset 0 1px 0 rgba(255,255,255,0.1),0 8px 24px -10px rgba(0,0,0,0.6);
    animation:cellIn .6s cubic-bezier(.2,.8,.2,1) both;
    transition:transform .3s,border-color .3s;
  }
  .code-cell:nth-child(1){animation-delay:.4s}
  .code-cell:nth-child(2){animation-delay:.48s}
  .code-cell:nth-child(3){animation-delay:.56s}
  .code-cell:nth-child(4){animation-delay:.64s}
  .code-cell:nth-child(5){animation-delay:.72s}
  .code-cell:nth-child(6){animation-delay:.80s}
  .code-cell:hover{transform:translateY(-3px);border-color:var(--border-3)}
  @keyframes cellIn{from{opacity:0;transform:translateY(18px) scale(.9)}to{opacity:1;transform:translateY(0) scale(1)}}

  .code-caption{
    margin-top:18px;font-size:13px;color:var(--text-mute);
    letter-spacing:0.01em;
    animation:rise 1s .9s cubic-bezier(.2,.8,.2,1) both;
    display:inline-flex;align-items:center;gap:8px;
  }
  .code-caption svg{width:13px;height:13px;opacity:.6}

  .marquee{
    margin-top:100px;padding:22px 0;
    border-top:1px solid var(--border);border-bottom:1px solid var(--border);
    overflow:hidden;
    mask-image:linear-gradient(90deg,transparent,#000 10%,#000 90%,transparent);
    -webkit-mask-image:linear-gradient(90deg,transparent,#000 10%,#000 90%,transparent);
  }
  .marquee-track{
    display:flex;gap:52px;width:max-content;
    animation:scroll 40s linear infinite;
    will-change:transform;
  }
  .marquee-track span{
    font-family:'Unbounded',sans-serif;font-size:15px;font-weight:500;
    letter-spacing:0.05em;text-transform:uppercase;color:var(--text-mute);
    display:inline-flex;align-items:center;gap:52px;white-space:nowrap;
  }
  .marquee-track span::after{
    content:'';width:5px;height:5px;border-radius:50%;
    background:var(--text-mute);display:inline-block;opacity:.6;
  }
  @keyframes scroll{to{transform:translateX(-50%)}}

  section{padding:110px 0;position:relative}

  .sec-head{max-width:600px;margin-bottom:56px}
  .eyebrow{
    display:inline-flex;align-items:center;gap:10px;
    font-family:'JetBrains Mono',monospace;
    font-size:11.5px;font-weight:500;
    letter-spacing:0.12em;text-transform:uppercase;
    color:var(--text-mute);margin-bottom:20px;
  }
  .eyebrow::before{content:'';width:24px;height:1px;background:linear-gradient(90deg,var(--text-mute),transparent)}
  h2{
    font-family:'Unbounded',sans-serif;font-weight:700;
    font-size:clamp(28px,4.4vw,46px);line-height:1.05;
    letter-spacing:-0.035em;margin-bottom:16px;
  }
  h2 .dim{color:var(--text-mute);font-weight:400}
  .sec-head p{color:var(--text-dim);font-size:15.5px}

  .glass{
    position:relative;border-radius:var(--radius);
    background:linear-gradient(150deg,rgba(255,255,255,0.055),rgba(255,255,255,0.014));
    backdrop-filter:blur(22px) saturate(150%);
    -webkit-backdrop-filter:blur(22px) saturate(150%);
    box-shadow:0 24px 60px -28px rgba(0,0,0,0.75),inset 0 1px 0 rgba(255,255,255,0.07);
    overflow:hidden;
    transition:transform .45s cubic-bezier(.2,.8,.2,1),box-shadow .45s,border-color .3s;
    border:1px solid transparent;
  }
  .glass::before{
    content:'';position:absolute;inset:0;border-radius:inherit;padding:1px;
    background:linear-gradient(150deg,rgba(255,255,255,0.32),rgba(255,255,255,0.03) 35%,rgba(255,255,255,0.02) 65%,rgba(255,255,255,0.16));
    -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
    -webkit-mask-composite:xor;
    mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
    mask-composite:exclude;
    pointer-events:none;
  }
  .glass::after{
    content:'';position:absolute;top:-60%;left:-60%;width:100%;height:220%;
    background:linear-gradient(115deg,transparent 42%,rgba(255,255,255,0.06) 50%,transparent 58%);
    transform:translateX(0);
    transition:transform 1s cubic-bezier(.2,.8,.2,1);
    pointer-events:none;
  }
  .glass:hover::after{transform:translateX(120%)}
  .glass:hover{transform:translateY(-2px)}

  .bento{display:grid;grid-template-columns:repeat(6,1fr);gap:16px}
  .bento .glass{padding:30px;display:flex;flex-direction:column}
  .b-lg{grid-column:span 4;min-height:300px}
  .b-md{grid-column:span 3;min-height:250px}
  .b-sm{grid-column:span 2;min-height:240px}

  .icon-box{
    width:46px;height:46px;border-radius:13px;
    display:grid;place-items:center;
    background:linear-gradient(150deg,rgba(255,255,255,0.1),rgba(255,255,255,0.02));
    border:1px solid var(--border-2);margin-bottom:22px;color:var(--text-dim);
    transition:transform .4s cubic-bezier(.2,.8,.2,1),color .3s,border-color .3s;
    flex-shrink:0;
  }
  .icon-box svg{width:20px;height:20px}
  .glass:hover .icon-box{transform:translateY(-2px) scale(1.05);color:#fff;border-color:var(--border-3)}

  .bento h3{
    font-family:'Unbounded',sans-serif;font-weight:600;
    font-size:17.5px;letter-spacing:-0.02em;line-height:1.25;
    margin-bottom:10px;
  }
  .bento p{color:var(--text-dim);font-size:14px;line-height:1.6;max-width:48ch}
  .bento p code{font-family:'JetBrains Mono',monospace;color:var(--text-dim);font-size:.92em}

  .b-lg{justify-content:space-between}
  .b-lg .big-num{
    font-family:'Unbounded',sans-serif;font-weight:700;
    font-size:clamp(52px,8vw,96px);line-height:0.9;letter-spacing:-0.055em;
    background:linear-gradient(160deg,#fff 25%,rgba(255,255,255,0.22));
    -webkit-background-clip:text;background-clip:text;color:transparent;
    margin-top:auto;padding-top:24px;
  }
  .b-lg .big-num sup{
    font-size:0.32em;vertical-align:super;
    color:var(--text-mute);
    -webkit-text-fill-color:var(--text-mute);
    margin-left:2px;
  }

  .avatars{display:flex;margin-top:auto;padding-top:22px}
  .av{
    width:36px;height:36px;border-radius:50%;
    border:2px solid #0c0c0c;margin-left:-11px;
    display:grid;place-items:center;
    font-family:'Unbounded',sans-serif;font-size:11.5px;font-weight:700;
    color:#08080a;
    transition:transform .3s cubic-bezier(.2,.8,.2,1);
  }
  .av:first-child{margin-left:0}
  .glass:hover .av{transform:translateY(-3px)}
  .glass:hover .av:nth-child(2){transition-delay:.04s}
  .glass:hover .av:nth-child(3){transition-delay:.08s}
  .glass:hover .av:nth-child(4){transition-delay:.12s}
  .glass:hover .av:nth-child(5){transition-delay:.16s}
  .av-1{background:linear-gradient(140deg,#f0f0f2,#a8a8ae)}
  .av-2{background:linear-gradient(140deg,#d8d8dc,#8c8c92)}
  .av-3{background:linear-gradient(140deg,#c0c0c4,#70707a)}
  .av-4{background:linear-gradient(140deg,#e8e8ea,#98989e)}
  .av-5{background:linear-gradient(140deg,#b8b8bc,#68686e)}
  .av-more{
    background:rgba(255,255,255,0.08);color:#fff;
    backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
    font-size:10.5px;border-color:rgba(255,255,255,0.1);
  }

  .specs{
    display:grid;grid-template-columns:repeat(4,1fr);gap:1px;
    border-radius:var(--radius);overflow:hidden;
    background:var(--border);border:1px solid var(--border);
  }
  .spec{
    background:rgba(10,10,10,0.7);
    backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
    padding:38px 24px;text-align:center;
    transition:background .35s;
  }
  .spec:hover{background:rgba(18,18,18,0.85)}
  .spec-val{
    font-family:'Unbounded',sans-serif;font-weight:700;
    font-size:clamp(24px,3.4vw,36px);letter-spacing:-0.04em;
    background:linear-gradient(160deg,#fff,rgba(255,255,255,0.4));
    -webkit-background-clip:text;background-clip:text;color:transparent;
    line-height:1;margin-bottom:10px;font-variant-numeric:tabular-nums;
  }
  .spec-lbl{
    font-family:'JetBrains Mono',monospace;font-size:11.5px;
    color:var(--text-mute);letter-spacing:0.04em;text-transform:uppercase;
  }

  .steps{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
  .step{padding:30px;display:flex;flex-direction:column;position:relative}
  .step-num{
    font-family:'JetBrains Mono',monospace;font-size:11.5px;
    color:var(--text-mute);letter-spacing:0.1em;margin-bottom:20px;
  }
  .step-num::before{content:'— '}
  .step h3{
    font-family:'Unbounded',sans-serif;font-weight:600;
    font-size:18px;letter-spacing:-0.02em;margin-bottom:10px;
  }
  .step p{color:var(--text-dim);font-size:14px;line-height:1.6}

  .cta{
    position:relative;border-radius:32px;padding:80px 40px;
    text-align:center;overflow:hidden;
    background:linear-gradient(150deg,rgba(255,255,255,0.05),rgba(255,255,255,0.012));
    backdrop-filter:blur(26px) saturate(150%);
    -webkit-backdrop-filter:blur(26px) saturate(150%);
    border:1px solid var(--border-2);
    box-shadow:0 40px 100px -50px rgba(0,0,0,0.9),inset 0 1px 0 rgba(255,255,255,0.08);
  }
  .cta::before{
    content:'';position:absolute;
    width:800px;height:600px;border-radius:50%;
    background:radial-gradient(circle,rgba(255,255,255,0.09),transparent 65%);
    top:-350px;left:50%;transform:translateX(-50%);
    filter:blur(50px);pointer-events:none;
  }
  .cta > *{position:relative;z-index:1}
  .cta h2{font-size:clamp(30px,5vw,54px);margin-bottom:18px}
  .cta p{color:var(--text-dim);font-size:16px;max-width:460px;margin:0 auto 34px}
  .cta .btn{padding:16px 34px;height:auto;font-size:15px;border-radius:14px}
  .cta .btn svg{width:17px;height:17px}
  .cta-note{
    font-size:12.5px;color:var(--text-mute);
    margin-top:20px;margin-bottom:0;
    font-family:'JetBrains Mono',monospace;letter-spacing:0.02em;
  }

  footer{padding:70px 0 44px;border-top:1px solid var(--border);margin-top:80px}
  .foot-top{
    display:flex;justify-content:space-between;
    align-items:flex-start;gap:40px;flex-wrap:wrap;
    margin-bottom:48px;
  }
  .foot-brand{max-width:280px}
  .foot-brand .logo{margin-bottom:16px}
  .foot-brand p{color:var(--text-mute);font-size:13.5px;line-height:1.6}

  .foot-cols{display:flex;gap:70px;flex-wrap:wrap}
  .foot-col h4{
    font-family:'JetBrains Mono',monospace;font-size:11px;
    letter-spacing:0.16em;text-transform:uppercase;
    color:var(--text-mute);margin-bottom:18px;font-weight:500;
  }
  .foot-col a{
    display:block;color:var(--text-dim);text-decoration:none;
    font-size:14.5px;padding:6px 0;
    transition:color .25s,transform .25s;width:fit-content;
  }
  .foot-col a:hover{color:#fff;transform:translateX(3px)}

  .foot-bottom{
    display:flex;justify-content:space-between;
    align-items:center;gap:20px;flex-wrap:wrap;
    padding-top:26px;border-top:1px solid var(--border);
    color:var(--text-mute);font-size:12.5px;
  }
  .socials{display:flex;gap:8px}
  .socials a{
    width:36px;height:36px;border-radius:10px;
    display:grid;place-items:center;
    background:var(--glass);border:1px solid var(--border-2);
    color:var(--text-dim);
    transition:all .3s cubic-bezier(.2,.8,.2,1);
  }
  .socials a:hover{
    background:rgba(255,255,255,0.09);border-color:var(--border-3);
    color:#fff;transform:translateY(-2px);
  }
  .socials svg{width:15px;height:15px}

  .reveal{
    opacity:0;transform:translateY(30px);
    transition:opacity .9s cubic-bezier(.2,.8,.2,1),transform .9s cubic-bezier(.2,.8,.2,1);
    will-change:opacity,transform;
  }
  .reveal.in{opacity:1;transform:translateY(0)}

  @media (max-width:1000px){
    .bento{grid-template-columns:repeat(4,1fr)}
    .b-lg{grid-column:span 4}
    .b-md{grid-column:span 2}
    .b-sm{grid-column:span 2}
    .specs{grid-template-columns:repeat(2,1fr)}
    .steps{grid-template-columns:1fr}
    .nav-links{display:none}
  }
  @media (max-width:620px){
    .wrap{padding:0 18px}
    nav{padding:10px 10px 10px 16px;top:12px;width:calc(100% - 24px)}
    .logo{font-size:13.5px}
    .logo-mark{width:28px;height:28px}
    .hero{padding:150px 0 60px}
    section{padding:70px 0}
    .bento{grid-template-columns:1fr;gap:14px}
    .bento .glass{grid-column:span 1 !important;padding:24px;min-height:auto}
    .b-lg{min-height:auto}
    .specs{grid-template-columns:1fr}
    .spec{padding:30px 20px}
    .cta{padding:54px 22px;border-radius:24px}
    .foot-cols{gap:36px}
    .marquee-track span{font-size:13px;gap:32px}
    .marquee-track{gap:32px}
    .code-cell{max-width:48px}
    .hero-cta{flex-direction:column;align-items:stretch}
    .hero-cta .btn{justify-content:center}
  }
</style>
</head>
<body>

<div class="cur-dot"></div>
<div class="cur-ring"></div>

<div class="bg">
  <div class="halo halo-1"></div>
  <div class="halo halo-2"></div>
  <div class="halo halo-3"></div>
</div>
<div class="grid-bg"></div>
<div class="grain"></div>

<nav id="nav">
  <a href="/" class="logo">
    <span class="logo-mark">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="5" r="2.4"/>
        <circle cx="5" cy="19" r="2.4"/>
        <circle cx="19" cy="19" r="2.4"/>
        <path d="M12 7.4 6.4 16.6M12 7.4l5.6 9.2M7.4 19h9.2"/>
      </svg>
    </span>
    <span class="logo-word">СЛД<span class="ldot">·</span><span class="lnet">NET</span></span>
  </a>
  <div class="nav-links">
    <a href="#features">Возможности</a>
    <a href="#how">Как это работает</a>
    <a href="#specs">Технологии</a>
  </div>
  <a href="/app" class="btn btn-primary">
    <span>Войти</span>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M5 12h14M13 6l6 6-6 6"/>
    </svg>
  </a>
</nav>

<header class="hero">
  <div class="wrap">
    <div class="hero-inner">
      <div class="badge">
        <span class="badge-dot">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
            <path d="M13 2 3 14h8l-1 8 10-12h-8l1-8z"/>
          </svg>
        </span>
        Открыт ранний доступ · приглашения по коду
      </div>

      <h1>
        Нетворкинг<br>
        <span class="dim">на шести цифрах</span>
      </h1>

      <p class="lead">
        Публикуйте посты с фото и находите их по <strong>уникальному 6-значному коду</strong>.
        Без регистрации, без профиля, без лишнего. Только вы и люди, с которыми стоит поговорить.
      </p>

      <div class="hero-cta">
        <a href="/app" class="btn btn-primary">
          <span>Получить доступ</span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M5 12h14M13 6l6 6-6 6"/>
          </svg>
        </a>
        <a href="#how" class="btn btn-ghost">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="9"/>
            <path d="M12 8v8M8 12h8"/>
          </svg>
          Как это работает
        </a>
      </div>

      <div class="code-show" aria-hidden="true">
        <div class="code-cell">4</div>
        <div class="code-cell">8</div>
        <div class="code-cell">1</div>
        <div class="code-cell">6</div>
        <div class="code-cell">3</div>
        <div class="code-cell">9</div>
      </div>
      <div class="code-caption">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="4" y="10" width="16" height="10" rx="2"/>
          <path d="M8 10V6a4 4 0 018 0v4"/>
        </svg>
        Коды без повторяющихся цифр — 151 200 комбинаций
      </div>
    </div>
  </div>

  <div class="marquee">
    <div class="marquee-track">
      <span>AES-256-GCM</span>
      <span>6-значные коды</span>
      <span>Без аккаунтов</span>
      <span>Умное сжатие</span>
      <span>До 5 фото</span>
      <span>Мгновенный поиск</span>
      <span>AES-256-GCM</span>
      <span>6-значные коды</span>
      <span>Без аккаунтов</span>
      <span>Умное сжатие</span>
      <span>До 5 фото</span>
      <span>Мгновенный поиск</span>
    </div>
  </div>
</header>

<section id="features">
  <div class="wrap">
    <div class="sec-head reveal">
      <div class="eyebrow">Что внутри</div>
      <h2>Минимум интерфейса.<br><span class="dim">Максимум смысла.</span></h2>
      <p>Всё построено вокруг одной идеи: поделиться чем-то важным и получить короткий код, по которому вас найдут.</p>
    </div>

    <div class="bento">
      <div class="glass b-lg reveal">
        <div>
          <div class="icon-box">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="4" width="18" height="16" rx="2.5"/>
              <path d="M3 9h18"/>
              <path d="M9 4v5"/>
            </svg>
          </div>
          <h3>Посты без регистрации</h3>
          <p>Заголовок, текст, до пяти фотографий — публикуется за секунды. Никаких анкет и подтверждений на почту.</p>
        </div>
        <div class="big-num">5<sup>фото</sup></div>
        <div class="avatars">
          <div class="av av-1">С</div>
          <div class="av av-2">Л</div>
          <div class="av av-3">Д</div>
          <div class="av av-4">N</div>
          <div class="av av-5">T</div>
          <div class="av av-more">+∞</div>
        </div>
      </div>

      <div class="glass b-sm reveal">
        <div class="icon-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 3 3 8l9 5 9-5-9-5z"/>
            <path d="M3 13l9 5 9-5"/>
            <path d="M3 17.5l9 5 9-5"/>
          </svg>
        </div>
        <h3>Шифрование</h3>
        <p>Каждый пост и каждое фото шифруются AES-256-GCM перед попаданием в память сервера.</p>
      </div>

      <div class="glass b-sm reveal">
        <div class="icon-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="9"/>
            <path d="M12 7v5l3 2"/>
          </svg>
        </div>
        <h3>Мгновенно</h3>
        <p>Публикация и поиск работают за доли секунды — всё хранится в оперативной памяти.</p>
      </div>

      <div class="glass b-md reveal">
        <div class="icon-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="3"/>
            <circle cx="9" cy="9" r="1.7"/>
            <path d="M21 15l-5-5L5 21"/>
          </svg>
        </div>
        <h3>Галерея с зумом</h3>
        <p>Просмотр фотографий как в настоящем редакторе: колесо — зум, ПКМ — 1×/2×, двойной клик — сброс, ЛКМ — панорама.</p>
      </div>

      <div class="glass b-md reveal">
        <div class="icon-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 7V5a1 1 0 011-1h14a1 1 0 011 1v2"/>
            <path d="M9 20h6"/>
            <path d="M12 4v16"/>
          </svg>
        </div>
        <h3>Заголовок и текст</h3>
        <p>До 120 символов в заголовке и до 20 000 в теле поста. Достаточно для манифеста, вакансии или анонса.</p>
      </div>

      <div class="glass b-sm reveal">
        <div class="icon-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <path d="M13 2 3 14h8l-1 8 10-12h-8l1-8z"/>
          </svg>
        </div>
        <h3>Коды без повторов</h3>
        <p>Алгоритм исключает одинаковые цифры в коде — так его легче запомнить и невозможно перепутать.</p>
      </div>

      <div class="glass b-sm reveal">
        <div class="icon-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="9"/>
            <path d="M3 12h18M12 3a15 15 0 0 0 0 18M12 3a15 15 0 0 1 0 18"/>
          </svg>
        </div>
        <h3>RU / EN</h3>
        <p>Интерфейс автоматически подстраивается под язык браузера или параметр <code>?lang</code> в ссылке.</p>
      </div>

      <div class="glass b-sm reveal">
        <div class="icon-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 6 9 17l-5-5"/>
          </svg>
        </div>
        <h3>Ссылка на пост</h3>
        <p>Каждый пост живёт по адресу вида <code>/app#482163</code>. Отправьте — и человек сразу увидит его.</p>
      </div>

      <div class="glass b-sm reveal">
        <div class="icon-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 3v18M3 12h18"/>
          </svg>
        </div>
        <h3>Сжатие zstd / gzip</h3>
        <p>Метаданные сжимаются перед шифрованием — экономия памяти без потери качества.</p>
      </div>
    </div>
  </div>
</section>

<section id="how" style="padding-top:20px">
  <div class="wrap">
    <div class="sec-head reveal">
      <div class="eyebrow">Как это работает</div>
      <h2>Три шага <span class="dim">до нужного человека</span></h2>
      <p>Без онбордингов, прогресс-баров и подтверждений. Публикуете, запоминаете, находите.</p>
    </div>

    <div class="steps">
      <div class="glass step reveal">
        <div class="step-num">Шаг 01</div>
        <h3>Публикуете пост</h3>
        <p>Заголовок, описание, до пяти фото. Можно перетащить файлы, выбрать через диалог или вставить из буфера по Ctrl+V.</p>
      </div>
      <div class="glass step reveal">
        <div class="step-num">Шаг 02</div>
        <h3>Получаете код</h3>
        <p>Сервер генерирует уникальные 6 цифр без повторов и показывает их в модальном окне. Копируется одной кнопкой.</p>
      </div>
      <div class="glass step reveal">
        <div class="step-num">Шаг 03</div>
        <h3>Делитесь или ищете</h3>
        <p>Вставьте код на вкладке «Найти пост» — или откройте ссылку с этим кодом. Пост найдётся мгновенно.</p>
      </div>
    </div>
  </div>
</section>

<section id="specs" style="padding-top:20px">
  <div class="wrap">
    <div class="sec-head reveal">
      <div class="eyebrow">Технологии</div>
      <h2>Всё серьёзно</h2>
      <p>Мы не храним ничего лишнего и не храним ничего дольше, чем нужно.</p>
    </div>

    <div class="specs reveal">
      <div class="spec">
        <div class="spec-val">AES-256</div>
        <div class="spec-lbl">GCM шифрование</div>
      </div>
      <div class="spec">
        <div class="spec-val">151 200</div>
        <div class="spec-lbl">Уникальных кодов</div>
      </div>
      <div class="spec">
        <div class="spec-val">5</div>
        <div class="spec-lbl">Фото на пост</div>
      </div>
      <div class="spec">
        <div class="spec-val">12 МБ</div>
        <div class="spec-lbl">Лимит на файл</div>
      </div>
    </div>
  </div>
</section>

<section id="join" style="padding-top:20px">
  <div class="wrap">
    <div class="cta reveal">
      <h2>Войти в СЛД·NET</h2>
      <p>Публикуйте посты, находите людей по шести цифрам и делитесь ссылками.</p>

      <a href="/app" class="btn btn-primary">
        <span>Войти на сайт</span>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M5 12h14M13 6l6 6-6 6"/>
        </svg>
      </a>

      <div class="cta-note">/app</div>
    </div>
  </div>
</section>

<footer>
  <div class="wrap">
    <div class="foot-top">
      <div class="foot-brand">
        <a href="/" class="logo">
          <span class="logo-mark">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="5" r="2.4"/>
              <circle cx="5" cy="19" r="2.4"/>
              <circle cx="19" cy="19" r="2.4"/>
              <path d="M12 7.4 6.4 16.6M12 7.4l5.6 9.2M7.4 19h9.2"/>
            </svg>
          </span>
          <span class="logo-word">СЛД<span class="ldot">·</span><span class="lnet">NET</span></span>
        </a>
        <p>Соцсеть для тех, кто ценит короткие пути к нужным людям.</p>
      </div>

      <div class="foot-cols">
        <div class="foot-col">
          <h4>Продукт</h4>
          <a href="#features">Возможности</a>
          <a href="#how">Как это работает</a>
          <a href="#specs">Технологии</a>
        </div>
        <div class="foot-col">
          <h4>Компания</h4>
          <a href="/app">О проекте</a>
          <a href="/app">Блог</a>
          <a href="/app">Контакты</a>
        </div>
        <div class="foot-col">
          <h4>Правовое</h4>
          <a href="/app">Приватность</a>
          <a href="/app">Условия</a>
        </div>
      </div>
    </div>

    <div class="foot-bottom">
      <span>© 2026 СЛД·NET. Все права защищены.</span>
      <div class="socials">
        <a href="/app" aria-label="Telegram">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <path d="m22 2-7 20-4-9-9-4 20-7z"/>
          </svg>
        </a>
        <a href="/app" aria-label="VK">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 7c.5 5 3.5 10 8 10h1v-3.5c1.8.3 3.4 1.6 4 3.5h3c-.5-2.3-2-4.2-4-5 1.8-.9 3-2.5 3.5-5h-2.8c-.4 1.6-1.5 3-3.2 3.5V7h-1.2c-4 0-7 3.5-7.8 8"/>
          </svg>
        </a>
        <a href="/app" aria-label="Email">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="5" width="18" height="14" rx="3"/>
            <path d="m4 7 8 6 8-6"/>
          </svg>
        </a>
      </div>
    </div>
  </div>
</footer>

<script>
(() => {
  "use strict";
  const dot  = document.querySelector('.cur-dot');
  const ring = document.querySelector('.cur-ring');
  let mx = window.innerWidth / 2, my = window.innerHeight / 2;
  let rx = mx, ry = my, lastX = mx, lastY = my, vel = 0;

  window.addEventListener('mousemove', (e) => { mx = e.clientX; my = e.clientY; }, { passive: true });

  function loop() {
    dot.style.transform = `translate3d(${mx}px, ${my}px, 0) translate(-50%, -50%)`;
    rx += (mx - rx) * 0.18;
    ry += (my - ry) * 0.18;
    const dx = mx - lastX, dy = my - lastY;
    vel = Math.min(Math.hypot(dx, dy), 36);
    lastX = mx; lastY = my;
    const angle = Math.atan2(dy, dx) * 180 / Math.PI;
    const stretch = 1 + vel / 100;
    const squash = 1 - vel / 150;
    ring.style.transform = `translate3d(${rx}px, ${ry}px, 0) translate(-50%, -50%) rotate(${angle}deg) scale(${stretch}, ${squash})`;
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);

  document.addEventListener('mousedown', () => ring.classList.add('click'));
  document.addEventListener('mouseup', () => ring.classList.remove('click'));

  document.querySelectorAll('a, button, input, .glass, .code-cell').forEach(el => {
    el.addEventListener('mouseenter', () => ring.classList.add('hover'));
    el.addEventListener('mouseleave', () => ring.classList.remove('hover'));
  });

  const io = new IntersectionObserver((entries) => {
    entries.forEach((entry, i) => {
      if (entry.isIntersecting) {
        setTimeout(() => entry.target.classList.add('in'), i * 60);
        io.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1, rootMargin: '0px 0px -60px 0px' });
  document.querySelectorAll('.reveal').forEach(el => io.observe(el));

  const nav = document.getElementById('nav');
  let ticking = false;
  window.addEventListener('scroll', () => {
    if (!ticking) {
      requestAnimationFrame(() => {
        nav.classList.toggle('scrolled', window.scrollY > 40);
        ticking = false;
      });
      ticking = true;
    }
  }, { passive: true });

  document.querySelectorAll('a[href^="#"]').forEach(a => {
    a.addEventListener('click', (e) => {
      const href = a.getAttribute('href');
      if (href === '#' || href.length < 2) return;
      const target = document.querySelector(href);
      if (target) {
        e.preventDefault();
        const top = target.getBoundingClientRect().top + window.scrollY - 90;
        window.scrollTo({ top, behavior: 'smooth' });
      }
    });
  });
})();
</script>

</body>
</html>
"""

# ============================================================
# РАБОЧАЯ ОБЛАСТЬ  (/app)
# ============================================================
APP = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#080808">
<title>СЛД·NET — рабочая область</title>
<link rel="icon" href="__FAVICON__">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;600;700&family=Manrope:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

  :root{
    --bg:#080808;
    --surface:#0f0f0f;
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
    --danger:#e08080;
    --topbar-h:80px;
  }

  html{scroll-behavior:smooth}

  html,body{
    background:var(--bg);color:var(--text);
    font-family:'Manrope',system-ui,-apple-system,sans-serif;
    font-size:15px;line-height:1.55;
    min-height:100%;overflow-x:hidden;
    -webkit-font-smoothing:antialiased;
    -moz-osx-font-smoothing:grayscale;
  }

  ::selection{background:#fff;color:#000}

  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-track{background:#0a0a0a}
  ::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:8px;border:2px solid #0a0a0a}
  ::-webkit-scrollbar-thumb:hover{background:#3a3a3a}

  /* ========== CURSOR ========== */
  @media (hover:hover) and (pointer:fine){
    body,a,button,input,textarea{cursor:none}
  }
  .cur-dot,.cur-ring{
    position:fixed;top:0;left:0;pointer-events:none;z-index:99999;
    border-radius:50%;will-change:transform;
  }
  .cur-dot{
    width:6px;height:6px;background:#fff;
    box-shadow:0 0 0 1px rgba(255,255,255,0.3);
  }
  .cur-ring{
    width:36px;height:36px;
    border:1px solid rgba(255,255,255,0.35);
    transition:width .25s cubic-bezier(.2,.8,.2,1),height .25s cubic-bezier(.2,.8,.2,1),border-color .25s,background .25s;
  }
  .cur-ring.hover{
    width:60px;height:60px;
    border-color:rgba(255,255,255,0.15);
    background:rgba(255,255,255,0.06);
    backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);
  }
  .cur-ring.click{width:26px;height:26px;background:rgba(255,255,255,0.15)}
  @media (max-width:900px),(hover:none){.cur-dot,.cur-ring{display:none}}

  /* ========== BACKGROUND ========== */
  .bg{position:fixed;inset:0;z-index:-2;overflow:hidden;background:var(--bg)}
  .halo{position:absolute;border-radius:50%;filter:blur(130px);opacity:.5}
  .halo-1{
    width:600px;height:600px;
    background:radial-gradient(circle,rgba(255,255,255,0.07),transparent 65%);
    top:-240px;left:-140px;
    animation:drift1 30s ease-in-out infinite;
  }
  .halo-2{
    width:520px;height:520px;
    background:radial-gradient(circle,rgba(255,255,255,0.045),transparent 65%);
    bottom:-180px;right:-160px;
    animation:drift2 34s ease-in-out infinite;
  }
  @keyframes drift1{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(90px,80px) scale(1.1)}}
  @keyframes drift2{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(-100px,-70px) scale(1.12)}}

  .grid-bg{
    position:fixed;inset:0;z-index:-1;pointer-events:none;
    background-image:
      linear-gradient(rgba(255,255,255,0.02) 1px,transparent 1px),
      linear-gradient(90deg,rgba(255,255,255,0.02) 1px,transparent 1px);
    background-size:68px 68px;
    mask-image:radial-gradient(ellipse 95% 85% at 50% 0%,#000 25%,transparent 85%);
    -webkit-mask-image:radial-gradient(ellipse 95% 85% at 50% 0%,#000 25%,transparent 85%);
  }

  .grain{
    position:fixed;inset:0;z-index:9998;pointer-events:none;
    opacity:.03;mix-blend-mode:overlay;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }

  /* ========== TOPBAR ========== */
  .topbar{
    position:fixed;top:14px;left:50%;
    transform:translateX(-50%);
    z-index:100;
    width:calc(100% - 28px);
    max-width:1160px;
    border-radius:16px;
    padding:10px 12px 10px 18px;
    background:rgba(10,10,10,0.62);
    backdrop-filter:blur(24px) saturate(160%);
    -webkit-backdrop-filter:blur(24px) saturate(160%);
    border:1px solid var(--border);
    box-shadow:0 12px 40px rgba(0,0,0,0.5),inset 0 1px 0 rgba(255,255,255,0.06);
    transition:background .3s,box-shadow .3s,border-color .3s;
  }
  .topbar-inner{
    display:flex;align-items:center;gap:14px;
    justify-content:space-between;
    flex-wrap:nowrap;
  }

  .logo{
    display:inline-flex;align-items:center;gap:10px;
    font-family:'Unbounded',sans-serif;font-weight:700;
    font-size:13.5px;letter-spacing:-0.01em;
    text-decoration:none;color:#fff;white-space:nowrap;
    flex-shrink:0;
  }
  .logo-mark{
    width:28px;height:28px;border-radius:8px;
    background:linear-gradient(140deg,#fff,#c4c4c8);
    display:grid;place-items:center;
    position:relative;overflow:hidden;
    box-shadow:0 4px 14px rgba(0,0,0,0.5),inset 0 -1px 0 rgba(0,0,0,0.15);
    transition:transform .35s cubic-bezier(.2,.8,.2,1);
  }
  .logo:hover .logo-mark{transform:rotate(-6deg) scale(1.05)}
  .logo-mark::after{
    content:'';position:absolute;inset:0;
    background:linear-gradient(150deg,rgba(255,255,255,0.9),transparent 55%);
    pointer-events:none;
  }
  .logo-mark svg{width:14px;height:14px;position:relative;z-index:1;color:#08080a}
  .logo-word{display:inline-flex;align-items:baseline;gap:1px}
  .logo-word .ldot{color:var(--text-mute);font-weight:400;margin:0 2px}
  .logo-word .lnet{color:var(--text-dim);font-weight:500}

  /* menu (segmented control) */
  .menu{
    display:inline-flex;gap:4px;
    padding:4px;
    border-radius:12px;
    background:rgba(255,255,255,0.028);
    border:1px solid var(--border);
    flex-shrink:0;
  }
  .tab{
    display:inline-flex;align-items:center;gap:8px;
    padding:8px 15px;
    border-radius:9px;
    background:transparent;border:1px solid transparent;
    color:var(--text-dim);
    font:inherit;font-size:13px;font-weight:500;
    cursor:pointer;
    transition:background .22s,color .22s,border-color .22s,box-shadow .22s;
    -webkit-tap-highlight-color:transparent;
    white-space:nowrap;
  }
  .tab svg{width:15px;height:15px;flex-shrink:0}
  .tab:hover{color:#fff;background:rgba(255,255,255,0.05)}
  .tab.active{
    background:rgba(255,255,255,0.09);
    border-color:rgba(255,255,255,0.13);
    color:#fff;
    box-shadow:inset 0 1px 0 rgba(255,255,255,0.09);
  }

  /* back */
  .back-btn{
    display:inline-flex;align-items:center;gap:7px;
    color:var(--text-dim);text-decoration:none;
    font-size:13px;font-weight:500;
    padding:8px 12px;border-radius:10px;
    transition:color .2s,background .2s;
    white-space:nowrap;flex-shrink:0;
  }
  .back-btn svg{width:14px;height:14px}
  .back-btn:hover{color:#fff;background:rgba(255,255,255,0.05)}

  /* ========== APP ========== */
  .app{
    min-height:100dvh;
    display:flex;flex-direction:column;align-items:center;
    padding:calc(var(--topbar-h, 80px) + 24px) 20px 60px;
    gap:20px;
  }

  .stage{
    position:relative;
    width:100%;max-width:560px;
    overflow:hidden;
    transition:height .28s ease;
  }
  .stage[hidden]{display:none}

  .panel{
    position:absolute;top:0;left:0;right:0;
    display:flex;flex-direction:column;gap:12px;
    opacity:0;pointer-events:none;
    transform:translateX(var(--enter-x,20px));
    transition:opacity .22s ease,transform .28s ease;
  }
  .panel.active{
    position:relative;opacity:1;pointer-events:auto;
    transform:translateX(0);
  }

  /* ========== CARD ========== */
  .card{
    position:relative;
    border-radius:18px;
    background:linear-gradient(155deg,rgba(255,255,255,0.05),rgba(255,255,255,0.012));
    border:1px solid var(--border);
    backdrop-filter:blur(20px) saturate(150%);
    -webkit-backdrop-filter:blur(20px) saturate(150%);
    box-shadow:0 20px 50px -28px rgba(0,0,0,0.8),inset 0 1px 0 rgba(255,255,255,0.05);
    padding:18px;
  }

  /* ========== INPUTS ========== */
  .input-wrap{position:relative;display:flex}
  .input-wrap + .input-wrap{margin-top:10px}
  .input-wrap .iw-icon{
    position:absolute;left:13px;width:16px;height:16px;
    color:var(--text-mute);pointer-events:none;
    transition:color .18s ease;
  }
  .input-wrap input.field,
  .input-wrap textarea.field{padding-left:40px}
  .input-wrap input.field + .iw-icon,
  .input-wrap textarea.field + .iw-icon{top:13px}
  .input-wrap.textarea-wrap .iw-icon{top:14px}
  .input-wrap:focus-within .iw-icon{color:var(--text)}

  .field{
    width:100%;
    background:var(--input);
    border:1px solid var(--border);
    border-radius:12px;
    padding:12px 15px;
    color:var(--text);
    font:inherit;font-size:14px;
    outline:none;
    transition:border-color .18s ease,background .18s ease;
    -webkit-appearance:none;appearance:none;
  }
  .field::placeholder{color:var(--text-mute)}
  .field:focus{
    border-color:var(--border-3);
    background:var(--input-focus);
    box-shadow:0 0 0 3px rgba(255,255,255,0.04);
  }

  textarea.field{
    min-height:150px;resize:none;
    line-height:1.55;font-family:inherit;
  }

  /* ========== DROP ========== */
  .drop{
    margin-top:10px;
    border:1px dashed var(--border-2);
    border-radius:12px;
    padding:20px 14px;
    text-align:center;
    color:var(--text-dim);
    cursor:pointer;
    background:var(--input);
    line-height:1.55;
    transition:border-color .18s,color .18s,background .18s;
    display:flex;flex-direction:column;align-items:center;gap:7px;
  }
  .drop .drop-icon{width:22px;height:22px;color:var(--text-mute);transition:color .18s}
  .drop .drop-label{font-size:13px;color:var(--text-dim);transition:color .18s}
  .drop .drop-hint{font-size:11.5px;color:var(--text-mute)}
  .drop:hover{border-color:var(--border-3);background:var(--input-focus)}
  .drop:hover .drop-icon,.drop:hover .drop-label{color:var(--text)}
  .drop.filled{border-style:solid;border-color:var(--border-3)}
  .drop.filled .drop-icon,.drop.filled .drop-label{color:var(--text)}

  .previews{
    display:grid;
    grid-template-columns:repeat(auto-fill,minmax(72px,1fr));
    gap:8px;margin-top:10px;
  }
  .preview{
    position:relative;aspect-ratio:1/1;
    border-radius:10px;overflow:hidden;
    background:var(--input);
    border:1px solid var(--border);
  }
  .preview img{width:100%;height:100%;object-fit:cover;display:block}
  .preview button{
    position:absolute;top:5px;right:5px;
    width:22px;height:22px;border-radius:7px;
    border:1px solid rgba(255,255,255,0.18);
    background:rgba(10,10,10,0.85);color:var(--text);
    font-size:12px;line-height:1;
    cursor:pointer;display:flex;align-items:center;justify-content:center;
    transition:background .18s,border-color .18s;
    backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
  }
  .preview button:hover{background:rgba(224,128,128,0.25);border-color:rgba(224,128,128,0.5)}

  /* ========== ROW (buttons) ========== */
  .row{
    display:flex;gap:10px;
    margin-top:16px;
    flex-wrap:nowrap;
  }

  /* ========== BUTTONS ========== */
  .btn{
    flex:1 1 0;min-width:0;
    height:46px;
    display:inline-flex;align-items:center;justify-content:center;gap:8px;
    padding:0 18px;
    border-radius:12px;
    border:1px solid transparent;
    background:transparent;
    color:var(--text);
    font:inherit;font-size:13.5px;font-weight:600;
    cursor:pointer;user-select:none;
    -webkit-tap-highlight-color:transparent;
    transition:transform .25s cubic-bezier(.2,.8,.2,1),background .22s,border-color .22s,color .22s,box-shadow .25s;
    white-space:nowrap;
  }
  .btn svg{width:15px;height:15px;flex-shrink:0;transition:transform .3s cubic-bezier(.2,.8,.2,1)}
  .btn:disabled{opacity:.5;cursor:not-allowed}

  .btn.primary{
    background:#f4f4f5;color:#08080a;
    box-shadow:0 1px 0 rgba(255,255,255,0.7) inset,0 -1px 0 rgba(0,0,0,0.12) inset,0 6px 22px -6px rgba(255,255,255,0.22);
  }
  .btn.primary:hover:not(:disabled){
    transform:translateY(-1px);
    background:#fff;
    box-shadow:0 1px 0 rgba(255,255,255,0.9) inset,0 -1px 0 rgba(0,0,0,0.15) inset,0 10px 32px -6px rgba(255,255,255,0.28);
  }
  .btn.primary:active:not(:disabled){transform:translateY(0) scale(.985)}
  .btn.primary:hover:not(:disabled) svg{transform:translateX(2px)}

  .btn.ghost{
    background:var(--glass);
    color:var(--text);
    border-color:var(--border-2);
    backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
    box-shadow:inset 0 1px 0 rgba(255,255,255,0.06);
  }
  .btn.ghost:hover:not(:disabled){
    background:var(--glass-hi);
    border-color:var(--border-3);
    transform:translateY(-1px);
  }
  .btn.ghost:active:not(:disabled){transform:translateY(0) scale(.985)}

  /* ========== OTP ========== */
  .otp-row{
    display:flex;gap:10px;
    justify-content:center;align-items:center;
    flex-wrap:nowrap;
  }
  .otp{display:flex;gap:6px;justify-content:center;align-items:center;margin:0}
  .otp-cell{
    width:clamp(34px,9.5vw,46px);
    height:clamp(46px,12vw,56px);
    padding:0;text-align:center;
    font-family:'JetBrains Mono',monospace;
    font-size:clamp(17px,4.6vw,20px);font-weight:600;
    color:var(--text);
    background:var(--input);
    border:1px solid var(--border);
    border-radius:11px;
    outline:none;caret-color:transparent;
    -webkit-appearance:none;appearance:none;
    transition:border-color .18s,background .18s,box-shadow .18s;
  }
  .otp-cell:hover{background:var(--input-focus)}
  .otp-cell:focus{
    background:var(--input-focus);
    border-color:var(--border-3);
    box-shadow:0 0 0 3px rgba(255,255,255,0.05);
  }

  .otp-copy{
    flex-shrink:0;
    width:clamp(46px,12vw,56px);
    height:clamp(46px,12vw,56px);
    border-radius:11px;
    border:1px solid var(--border);
    background:var(--input);
    color:var(--text-mute);
    display:flex;align-items:center;justify-content:center;
    cursor:pointer;
    -webkit-appearance:none;appearance:none;
    transition:background .18s,border-color .18s,color .18s;
  }
  .otp-copy svg{width:18px;height:18px;pointer-events:none}
  .otp-copy:hover:not(:disabled){
    background:var(--input-focus);
    border-color:var(--border-3);
    color:var(--text);
  }
  .otp-copy:disabled{opacity:.35;cursor:not-allowed}
  .otp-copy.copied{
    color:var(--ok);
    border-color:rgba(126,200,153,0.5);
    background:rgba(126,200,153,0.08);
  }
  .otp-copy .icon-check{display:none}
  .otp-copy.copied .icon-copy{display:none}
  .otp-copy.copied .icon-check{display:block}

  @keyframes shake{
    0%,100%{transform:translateX(0)}
    20%{transform:translateX(-7px)}
    40%{transform:translateX(7px)}
    60%{transform:translateX(-4px)}
    80%{transform:translateX(4px)}
  }
  .otp.shake{animation:shake .32s ease}
  .otp.shake .otp-cell{border-color:rgba(224,128,128,0.7);background:rgba(224,128,128,0.08)}

  /* ========== LOADER ========== */
  .center{text-align:center;padding:8px 0}
  .spinner{
    width:22px;height:22px;border-radius:50%;
    border:2px solid var(--border-2);
    border-top-color:var(--text);
    animation:spin .7s linear infinite;margin:0 auto;
  }
  @keyframes spin{to{transform:rotate(360deg)}}
  .spinner-label{margin-top:10px;font-size:12px;color:var(--text-dim);text-align:center;font-family:'JetBrains Mono',monospace;letter-spacing:0.04em}

  /* ========== MESSAGES ========== */
  .msg{
    display:flex;align-items:flex-start;gap:8px;
    border-radius:12px;padding:11px 13px;font-size:13px;
    background:rgba(255,255,255,0.03);color:var(--text);
    line-height:1.5;border:1px solid var(--border);
    margin-top:12px;
    word-break:break-word;
  }
  .msg:first-child{margin-top:0}
  .msg svg{width:16px;height:16px;flex-shrink:0;margin-top:1px}
  .msg span{min-width:0;word-break:break-word}
  .msg.err{background:rgba(224,128,128,0.08);border-color:rgba(224,128,128,0.3);color:#e8b1b1}
  .msg.ok{background:rgba(126,200,153,0.08);border-color:rgba(126,200,153,0.3);color:#b1dfc2}

  /* ========== POST ========== */
  .post-title{
    margin:0 0 8px;
    font-family:'Unbounded',sans-serif;
    font-size:17px;font-weight:600;line-height:1.3;
    color:#fff;word-break:break-word;
    letter-spacing:-0.02em;
  }
  .post-meta{
    font-size:12px;color:var(--text-dim);
    margin-bottom:14px;
    display:flex;gap:12px;flex-wrap:wrap;align-items:center;
    font-family:'JetBrains Mono',monospace;
    letter-spacing:0.01em;
  }
  .post-body{
    font-size:14px;line-height:1.65;color:var(--text);
    white-space:pre-wrap;word-break:break-word;
  }
  .post-gallery{
    display:grid;
    grid-template-columns:repeat(auto-fill,minmax(100px,1fr));
    gap:8px;margin-top:16px;
  }
  .post-gallery img{
    width:100%;aspect-ratio:1/1;object-fit:cover;
    border-radius:10px;
    cursor:zoom-in;background:var(--input);
    border:1px solid var(--border);
    transition:border-color .18s,transform .35s cubic-bezier(.2,.8,.2,1);
  }
  .post-gallery img:hover{
    border-color:var(--border-3);
    transform:translateY(-2px);
  }

  /* ========== LIGHTBOX ========== */
  .lightbox{
    position:fixed;inset:0;z-index:1000;
    display:flex;align-items:center;justify-content:center;
    background:rgba(0,0,0,0.94);
    backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  }
  .lightbox[hidden]{display:none}

  .lb-viewport{
    position:absolute;inset:0;
    display:flex;align-items:center;justify-content:center;
    overflow:hidden;cursor:default;
  }

  .lb-transform{
    display:flex;align-items:center;justify-content:center;
    transform-origin:center center;will-change:transform;
  }

  .lb-img{
    display:block;max-width:82vw;max-height:78vh;
    object-fit:contain;border-radius:8px;
    user-select:none;-webkit-user-select:none;-webkit-user-drag:none;
    background-color:#1a1a1a;
    background-image:
      linear-gradient(45deg, rgba(255,255,255,.04) 25%, transparent 25%),
      linear-gradient(-45deg, rgba(255,255,255,.04) 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, rgba(255,255,255,.04) 75%),
      linear-gradient(-45deg, transparent 75%, rgba(255,255,255,.04) 75%);
    background-size:16px 16px;
    background-position:0 0, 0 8px, 8px -8px, -8px 0px;
  }

  .lb-btn{
    position:absolute;width:40px;height:40px;border-radius:11px;
    border:1px solid var(--border-2);
    background:rgba(15,15,15,0.75);
    backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
    color:var(--text);
    display:flex;align-items:center;justify-content:center;
    cursor:pointer;z-index:2;
    transition:background .18s,border-color .18s;
  }
  .lb-btn svg{width:18px;height:18px;pointer-events:none}
  .lb-btn:hover{background:rgba(30,30,30,0.9);border-color:var(--border-3)}
  .lb-btn[hidden]{display:none}

  .lb-close{top:16px;right:16px}
  .lb-prev{left:16px;top:50%;transform:translateY(-50%)}
  .lb-next{right:16px;top:50%;transform:translateY(-50%)}

  .lb-counter{
    position:absolute;bottom:18px;left:50%;transform:translateX(-50%);
    padding:6px 14px;border-radius:10px;
    background:rgba(15,15,15,0.75);
    backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
    border:1px solid var(--border-2);
    font-size:12px;color:var(--text);
    font-family:'JetBrains Mono',monospace;
    letter-spacing:.06em;
    z-index:2;pointer-events:none;
  }

  .lb-zoom-badge{
    position:absolute;top:16px;left:16px;
    padding:5px 11px;border-radius:10px;
    background:rgba(15,15,15,0.75);
    backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
    border:1px solid var(--border-2);
    font-size:11.5px;color:var(--text);
    font-family:'JetBrains Mono',monospace;
    z-index:2;pointer-events:none;
    opacity:0;transition:opacity .18s ease;
  }
  .lb-zoom-badge.visible{opacity:1}

  .lb-hint{
    position:absolute;bottom:56px;left:50%;transform:translateX(-50%);
    font-size:11.5px;color:var(--text-mute);
    z-index:2;pointer-events:none;white-space:nowrap;
    font-family:'JetBrains Mono',monospace;letter-spacing:0.02em;
  }

  /* ========== MODAL ========== */
  .modal{
    position:fixed;inset:0;z-index:900;
    display:flex;align-items:center;justify-content:center;
    background:rgba(0,0,0,0.72);
    backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
    padding:20px;
    animation:fadeIn .22s ease;
  }
  .modal[hidden]{display:none}
  @keyframes fadeIn{from{opacity:0}to{opacity:1}}

  .modal-card{
    width:100%;max-width:380px;
    padding:28px 24px 22px;
    border-radius:20px;
    background:linear-gradient(155deg,rgba(20,20,20,0.95),rgba(12,12,12,0.95));
    border:1px solid var(--border-2);
    box-shadow:0 30px 80px -20px rgba(0,0,0,0.9),inset 0 1px 0 rgba(255,255,255,0.06);
    text-align:center;
    animation:popIn .32s cubic-bezier(.2,.8,.2,1);
  }
  @keyframes popIn{from{opacity:0;transform:translateY(14px) scale(.96)}to{opacity:1;transform:translateY(0) scale(1)}}

  .modal-icon{
    width:48px;height:48px;margin:0 auto 14px;
    border-radius:50%;
    display:flex;align-items:center;justify-content:center;
    background:rgba(126,200,153,0.1);
    color:var(--ok);
    border:1px solid rgba(126,200,153,0.3);
  }
  .modal-icon svg{width:22px;height:22px}

  .modal-title{
    font-family:'Unbounded',sans-serif;
    font-size:16px;font-weight:600;margin:0 0 6px;color:#fff;
    letter-spacing:-0.02em;
  }
  .modal-sub{font-size:12.5px;color:var(--text-dim);margin:0 0 18px;line-height:1.5}
  .modal-code{
    font-family:'JetBrains Mono',monospace;
    font-size:38px;font-weight:600;
    letter-spacing:12px;text-indent:12px;
    color:#fff;margin:8px 0 8px;
  }
  .modal-hint{
    font-size:11.5px;color:var(--text-mute);
    margin-bottom:20px;
    font-family:'JetBrains Mono',monospace;letter-spacing:0.02em;
  }
  .modal-actions{display:flex;gap:8px}
  .modal-actions .btn{flex:1;height:42px;font-size:13px}

  /* ========== RESPONSIVE ========== */
  @media (max-width:820px){
    .topbar{padding:9px 10px 9px 14px}
    .logo-word{display:none}
    .back-btn span{display:none}
    .back-btn{padding:8px 10px}
    .menu{padding:3px}
    .tab{padding:8px 13px;font-size:12.5px}
  }

  @media (max-width:640px){
    .topbar-inner{
      flex-wrap:wrap;
      row-gap:8px;
    }
    .menu{
      order:3;
      width:100%;
      justify-content:center;
    }
    .tab{
      flex:1 1 0;
      justify-content:center;
    }
    .app{
      padding:calc(var(--topbar-h, 110px) + 18px) 14px 40px;
      gap:16px;
    }
    .card{padding:15px;border-radius:16px}
    .field{padding:11px 14px;font-size:14px}
    .input-wrap input.field,
    .input-wrap textarea.field{padding-left:38px}
    .btn{height:44px;font-size:13px;padding:0 14px}
    .row{gap:8px;margin-top:14px}
    .otp-row{gap:8px}
    .otp{gap:5px}
    .modal-card{padding:24px 18px 18px;border-radius:18px}
    .modal-code{font-size:32px;letter-spacing:9px;text-indent:9px}
    .lb-prev{left:8px}
    .lb-next{right:8px}
    .lb-close{top:10px;right:10px}
    .lb-zoom-badge{top:10px;left:10px}
    .lb-img{max-width:94vw;max-height:80vh}
    .lb-hint{display:none}
    .post-gallery{grid-template-columns:repeat(auto-fill,minmax(84px,1fr));gap:6px}
  }

  @media (max-width:380px){
    .tab span{display:none}
    .tab{padding:9px 12px}
    .tab svg{width:16px;height:16px}
  }
</style>
</head>
<body>

<div class="cur-dot"></div>
<div class="cur-ring"></div>

<div class="bg">
  <div class="halo halo-1"></div>
  <div class="halo halo-2"></div>
</div>
<div class="grid-bg"></div>
<div class="grain"></div>

<nav class="topbar" id="topbar">
  <div class="topbar-inner">
    <a href="/" class="logo">
      <span class="logo-mark">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="5" r="2.4"/>
          <circle cx="5" cy="19" r="2.4"/>
          <circle cx="19" cy="19" r="2.4"/>
          <path d="M12 7.4 6.4 16.6M12 7.4l5.6 9.2M7.4 19h9.2"/>
        </svg>
      </span>
      <span class="logo-word">СЛД<span class="ldot">·</span><span class="lnet">NET</span></span>
    </a>

    <div class="menu">
      <button class="tab" id="btnCreate" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 5v14M5 12h14"/>
        </svg>
        <span data-i18n="createPost">Создать пост</span>
      </button>
      <button class="tab" id="btnFind" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="7"/>
          <path d="m20 20-3.5-3.5"/>
        </svg>
        <span data-i18n="findPost">Найти пост</span>
      </button>
    </div>

    <a href="/" class="back-btn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M19 12H5M11 6l-6 6 6 6"/>
      </svg>
      <span>На главную</span>
    </a>
  </div>
</nav>

<main class="app">

  <div class="stage" id="stage" hidden>

    <div class="panel" id="createPanel">
      <section class="card">
        <div class="input-wrap">
          <input class="field" id="title" type="text" maxlength="120" autocomplete="off" spellcheck="false"
                 placeholder="Название" data-i18n-ph="titlePh">
          <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 7V5a1 1 0 011-1h14a1 1 0 011 1v2"/>
            <path d="M9 20h6"/>
            <path d="M12 4v16"/>
          </svg>
        </div>

        <div class="input-wrap textarea-wrap">
          <textarea class="field" id="content" placeholder="Содержимое" data-i18n-ph="contentPh"></textarea>
          <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 6h16M4 12h16M4 18h10"/>
          </svg>
        </div>

        <div class="drop" id="drop">
          <svg class="drop-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2.5"/>
            <circle cx="9" cy="9" r="1.6"/>
            <path d="M21 15l-5-5L5 21"/>
          </svg>
          <div class="drop-label" id="dropLabel" data-i18n="dropLabel">Нажмите или перетащите фото</div>
          <div class="drop-hint" data-i18n="dropHint">до 5 фото · Ctrl+V — вставить из буфера</div>
        </div>
        <input type="file" id="fileInput" accept="image/*" multiple hidden>
        <div class="previews" id="previews"></div>

        <div class="row">
          <button class="btn ghost" id="resetBtn" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 6h18"/>
              <path d="M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2"/>
              <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/>
            </svg>
            <span data-i18n="clear">Очистить</span>
          </button>
          <button class="btn primary" id="submitBtn" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M22 2L11 13"/>
              <path d="M22 2l-7 20-4-9-9-4 20-7z"/>
            </svg>
            <span data-i18n="publish">Опубликовать</span>
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
          <button class="otp-copy" id="otpCopyBtn" type="button" disabled
                  data-i18n-title="copyCode" title="Скопировать код" aria-label="Скопировать код">
            <svg class="icon-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2"/>
              <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
            </svg>
            <svg class="icon-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 6L9 17l-5-5"/>
            </svg>
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
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M20 6L9 17l-5-5"/>
      </svg>
    </div>
    <h3 class="modal-title" data-i18n="postCreated">Пост создан</h3>
    <p class="modal-sub" data-i18n="postCreatedSub">Сохраните код — по нему можно найти пост в любое время</p>
    <div class="modal-code" id="modalCode">000000</div>
    <div class="modal-hint" id="modalHint"></div>
    <div class="modal-actions">
      <button class="btn ghost" id="modalCopyBtn" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
          <rect x="9" y="9" width="13" height="13" rx="2"/>
          <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
        </svg>
        <span data-i18n="copy">Копировать</span>
      </button>
      <button class="btn primary" id="modalCloseBtn" type="button" data-i18n="done">Готово</button>
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
  <button class="lb-btn lb-close" id="lbClose" type="button" aria-label="Close">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <line x1="18" y1="6" x2="6" y2="18"/>
      <line x1="6" y1="6" x2="18" y2="18"/>
    </svg>
  </button>
  <button class="lb-btn lb-prev" id="lbPrev" type="button" aria-label="Prev">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="15 18 9 12 15 6"/>
    </svg>
  </button>
  <button class="lb-btn lb-next" id="lbNext" type="button" aria-label="Next">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="9 18 15 12 9 6"/>
    </svg>
  </button>
  <div class="lb-counter" id="lbCounter">1 / 1</div>
  <div class="lb-hint" data-i18n="lbHint">колесо — зум · ПКМ — 1×/2× · 2× клик — сброс · ЛКМ — панорама</div>
</div>

<script>
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);

  document.addEventListener("contextmenu", (e) => e.preventDefault());

  /* ========== CURSOR ========== */
  const dot  = document.querySelector('.cur-dot');
  const ring = document.querySelector('.cur-ring');
  let mx = window.innerWidth / 2, my = window.innerHeight / 2;
  let rx = mx, ry = my, lastX = mx, lastY = my, vel = 0;

  window.addEventListener('mousemove', (e) => { mx = e.clientX; my = e.clientY; }, { passive: true });

  function loop() {
    dot.style.transform = `translate3d(${mx}px, ${my}px, 0) translate(-50%, -50%)`;
    rx += (mx - rx) * 0.18;
    ry += (my - ry) * 0.18;
    const dx = mx - lastX, dy = my - lastY;
    vel = Math.min(Math.hypot(dx, dy), 36);
    lastX = mx; lastY = my;
    const angle = Math.atan2(dy, dx) * 180 / Math.PI;
    const stretch = 1 + vel / 100;
    const squash = 1 - vel / 150;
    ring.style.transform = `translate3d(${rx}px, ${ry}px, 0) translate(-50%, -50%) rotate(${angle}deg) scale(${stretch}, ${squash})`;
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);

  document.addEventListener('mousedown', () => ring.classList.add('click'));
  document.addEventListener('mouseup', () => ring.classList.remove('click'));

  document.querySelectorAll('a, button, input, textarea, .tab, .drop, .post-gallery img').forEach(el => {
    el.addEventListener('mouseenter', () => ring.classList.add('hover'));
    el.addEventListener('mouseleave', () => ring.classList.remove('hover'));
  });

  /* ========== TOPBAR HEIGHT ========== */
  const topbar = $('topbar');
  function measureTopbar() {
    const h = topbar.offsetHeight;
    document.documentElement.style.setProperty('--topbar-h', h + 'px');
  }
  window.addEventListener('resize', measureTopbar, { passive: true });
  measureTopbar();

  /* =========================================================
     i18n
     ========================================================= */
  const SUPPORTED = ["ru", "en"];

  const I18N = {
    ru: {
      createPost: "Создать пост",
      findPost: "Найти пост",
      titlePh: "Название",
      contentPh: "Содержимое",
      dropLabel: "Нажмите или перетащите фото",
      dropLabelFilled: "Выбрано: {n} / {max}",
      dropHint: "до 5 фото · Ctrl+V — вставить из буфера",
      publish: "Опубликовать",
      publishing: "Публикация...",
      clear: "Очистить",
      searching: "Ищем пост...",
      postCreated: "Пост создан",
      postCreatedSub: "Сохраните код — по нему можно найти пост в любое время",
      memoryUsage: "Занято в памяти: {size}",
      copy: "Копировать",
      copyCode: "Скопировать код",
      copied: "Скопировано",
      copyError: "Ошибка",
      done: "Готово",
      photoCount: "Фото: {n}",
      enterTitle: "Введите название поста.",
      notFound: "Пост не найден",
      networkError: "Ошибка сети: {msg}",
      httpError: "Ошибка {code}",
      rejectedFiles: "Пропущено файлов: {n}. Разрешены только изображения и не больше {max}.",
      lbHint: "колесо — зум · ПКМ — 1×/2× · 2× клик — сброс · ЛКМ — панорама"
    },
    en: {
      createPost: "Create post",
      findPost: "Find post",
      titlePh: "Title",
      contentPh: "Content",
      dropLabel: "Click or drop photos",
      dropLabelFilled: "Selected: {n} / {max}",
      dropHint: "up to 5 photos · Ctrl+V to paste",
      publish: "Publish",
      publishing: "Publishing...",
      clear: "Clear",
      searching: "Searching...",
      postCreated: "Post created",
      postCreatedSub: "Save the code — you can find the post anytime with it",
      memoryUsage: "Memory used: {size}",
      copy: "Copy",
      copyCode: "Copy code",
      copied: "Copied",
      copyError: "Error",
      done: "Done",
      photoCount: "Photos: {n}",
      enterTitle: "Please enter a title.",
      notFound: "Post not found",
      networkError: "Network error: {msg}",
      httpError: "Error {code}",
      rejectedFiles: "Skipped files: {n}. Images only, max {max}.",
      lbHint: "wheel — zoom · RMB — 1×/2× · dblclick — reset · LMB — pan"
    }
  };

  let currentLang = "ru";

  function t(key, params) {
    let s = (I18N[currentLang] && I18N[currentLang][key]) || I18N.en[key] || key;
    if (params) {
      for (const k in params) s = s.replace("{" + k + "}", params[k]);
    }
    return s;
  }

  function applyI18n(lang) {
    currentLang = SUPPORTED.includes(lang) ? lang : "ru";
    document.documentElement.lang = currentLang;

    document.querySelectorAll("[data-i18n]").forEach(el => {
      const key = el.getAttribute("data-i18n");
      const txt = (I18N[currentLang] && I18N[currentLang][key]) || I18N.en[key];
      if (txt) el.textContent = txt;
    });
    document.querySelectorAll("[data-i18n-ph]").forEach(el => {
      const key = el.getAttribute("data-i18n-ph");
      const txt = (I18N[currentLang] && I18N[currentLang][key]) || I18N.en[key];
      if (txt) el.placeholder = txt;
    });
    document.querySelectorAll("[data-i18n-title]").forEach(el => {
      const key = el.getAttribute("data-i18n-title");
      const txt = (I18N[currentLang] && I18N[currentLang][key]) || I18N.en[key];
      if (txt) { el.title = txt; el.setAttribute("aria-label", txt); }
    });

    dropLabel.textContent = defaultDropLabel();
    if (modalHint && modalHint.dataset.size) {
      modalHint.textContent = t("memoryUsage", { size: modalHint.dataset.size });
    }
  }

  function parseHash() {
    const h = (window.location.hash || "").replace(/^#/, "");
    if (!h) return { code: null, lang: null };
    const qIdx = h.indexOf("?");
    const codePart = (qIdx >= 0 ? h.slice(0, qIdx) : h).trim();
    const params = new URLSearchParams(qIdx >= 0 ? h.slice(qIdx + 1) : "");
    const code = /^\d{6}$/.test(codePart) ? codePart : null;
    const langRaw = (params.get("lang") || "").toLowerCase().split("-")[0];
    const lang = SUPPORTED.includes(langRaw) ? langRaw : null;
    return { code, lang };
  }

  function detectLang() {
    const { lang } = parseHash();
    if (lang) return lang;
    const nav = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || "ru"];
    for (const l of nav) {
      const code = String(l || "").toLowerCase().split("-")[0];
      if (SUPPORTED.includes(code)) return code;
    }
    return "ru";
  }

  /* =========================================================
     ICONS
     ========================================================= */
  const ICONS = {
    error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
    ok:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>'
  };

  function makeMsg(kind, text) {
    const d = document.createElement("div");
    d.className = "msg " + kind;
    d.innerHTML = (kind === "err" ? ICONS.error : ICONS.ok);
    const s = document.createElement("span");
    s.textContent = String(text == null ? "" : text);
    d.appendChild(s);
    return d;
  }

  async function readError(res) {
    const ct = (res.headers.get("content-type") || "").toLowerCase();
    if (ct.includes("application/json")) {
      const data = await res.json().catch(() => null);
      if (data) {
        if (typeof data.detail === "string" && data.detail.trim()) return data.detail;
        if (Array.isArray(data.detail) && data.detail.length) {
          const first = data.detail[0] || {};
          const loc = Array.isArray(first.loc) ? first.loc.filter(x => x !== "body").join(".") : "";
          const msg = first.msg || "Invalid input";
          return loc ? (loc + ": " + msg) : msg;
        }
        if (typeof data.message === "string" && data.message.trim()) return data.message;
      }
    }
    const txt = await res.text().catch(() => "");
    if (txt && txt.trim() && txt.length < 400) return txt.trim();
    const statusText = res.statusText ? (" — " + res.statusText) : "";
    return t("httpError", { code: res.status }) + statusText;
  }

  function formatBytes(b) {
    if (b < 1024) return b + " B";
    if (b < 1024 * 1024) return (b / 1024).toFixed(1).replace(".", ",") + " KB";
    if (b < 1024 * 1024 * 1024) return (b / (1024 * 1024)).toFixed(2).replace(".", ",") + " MB";
    return (b / (1024 * 1024 * 1024)).toFixed(2).replace(".", ",") + " GB";
  }

  /* =========================================================
     TABS
     ========================================================= */
  const stage       = $("stage");
  const createPanel = $("createPanel");
  const findPanel   = $("findPanel");
  const btnCreate   = $("btnCreate");
  const btnFind     = $("btnFind");

  let mode = null;

  function activePanel() { return mode === "create" ? createPanel : findPanel; }

  function syncHeight(animate = true) {
    if (mode === null || stage.hidden) return;
    if (!animate) stage.style.transition = "none";
    stage.style.height = activePanel().offsetHeight + "px";
    if (!animate) { void stage.offsetHeight; stage.style.transition = ""; }
  }

  const ro = new ResizeObserver(() => {
    if (!mode || stage.hidden) return;
    stage.style.height = activePanel().offsetHeight + "px";
  });
  ro.observe(createPanel);
  ro.observe(findPanel);
  window.addEventListener("resize", () => syncHeight(false), { passive: true });

  function setMode(next, instant = false) {
    if (next === mode && !instant) return;

    const firstShow = stage.hidden;
    const incoming  = next === "create" ? createPanel : findPanel;
    const outgoing  = next === "create" ? findPanel   : createPanel;

    const goLeft = (next === "create");
    const enterX = goLeft ? -20 : 20;
    const exitX  = goLeft ?  20 : -20;

    stage.hidden = false;
    stage.style.transition = "none";
    stage.style.height = incoming.offsetHeight + "px";

    if (firstShow || mode === null || instant) {
      incoming.style.setProperty("--enter-x", "0px");
      incoming.style.transition = "none";
      outgoing.classList.remove("active");
      incoming.classList.add("active");
      void incoming.offsetWidth;
      incoming.style.transition = "";
      void stage.offsetWidth;
      stage.style.transition = "";
    } else {
      incoming.style.setProperty("--enter-x", enterX + "px");
      outgoing.style.setProperty("--enter-x", exitX  + "px");
      void incoming.offsetWidth;
      outgoing.classList.remove("active");
      incoming.classList.add("active");
      void stage.offsetWidth;
      stage.style.transition = "";
      stage.style.height = incoming.offsetHeight + "px";
    }

    mode = next;
    btnCreate.classList.toggle("active", next === "create");
    btnFind.classList.toggle("active", next === "find");

    if (next === "create") setTimeout(() => $("title").focus(), 100);
    else setTimeout(() => otpCells[0].focus(), 100);

    requestAnimationFrame(() => syncHeight(false));
  }

  btnCreate.addEventListener("click", () => {
    if (window.location.hash) {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    setMode("create");
  });

  btnFind.addEventListener("click", () => setMode("find"));

  /* =========================================================
     CREATE
     ========================================================= */
  const MAX_PHOTOS = 5;
  let selectedFiles = [];

  const drop       = $("drop");
  const dropLabel  = $("dropLabel");
  const fileInput  = $("fileInput");
  const previews   = $("previews");
  const createMsg  = $("createMsg");
  const submitBtn  = $("submitBtn");
  const titleInput = $("title");
  const contentInput = $("content");

  function defaultDropLabel() {
    return selectedFiles.length
      ? t("dropLabelFilled", { n: selectedFiles.length, max: MAX_PHOTOS })
      : t("dropLabel");
  }

  drop.addEventListener("click", () => fileInput.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.style.borderColor = "var(--border-3)"; });
  drop.addEventListener("dragleave", () => { drop.style.borderColor = ""; });
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.style.borderColor = "";
    addFiles(Array.from(e.dataTransfer.files || []));
  });
  fileInput.addEventListener("change", () => {
    addFiles(Array.from(fileInput.files || []));
    fileInput.value = "";
  });

  document.addEventListener("paste", (e) => {
    if (mode !== "create") return;
    const items = (e.clipboardData || window.clipboardData)?.items;
    if (!items) return;
    const files = [];
    for (const item of items) {
      if (item.kind === "file" && item.type.startsWith("image/")) {
        const f = item.getAsFile();
        if (f) {
          const ext = (f.type.split("/")[1] || "png").replace("jpeg", "jpg");
          const named = new File(
            [f],
            "pasted_" + Date.now() + "_" + (files.length + 1) + "." + ext,
            { type: f.type }
          );
          files.push(named);
        }
      }
    }
    if (files.length) {
      e.preventDefault();
      addFiles(files);
    }
  });

  function addFiles(list) {
    let rejected = 0;
    for (const f of list) {
      if (!f.type.startsWith("image/")) { rejected++; continue; }
      if (selectedFiles.length >= MAX_PHOTOS) { rejected++; continue; }
      selectedFiles.push(f);
    }
    if (rejected > 0) {
      showCreateMsg("err", t("rejectedFiles", { n: rejected, max: MAX_PHOTOS }));
    } else {
      clearCreateMsg();
    }
    renderPreviews();
  }

  function renderPreviews() {
    previews.innerHTML = "";
    selectedFiles.forEach((file, index) => {
      const url = URL.createObjectURL(file);
      const box = document.createElement("div");
      box.className = "preview";
      const img = document.createElement("img");
      img.src = url; img.alt = file.name;
      img.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
      const rm = document.createElement("button");
      rm.type = "button"; rm.textContent = "×"; rm.title = "×";
      rm.addEventListener("click", () => {
        selectedFiles.splice(index, 1);
        renderPreviews();
      });
      box.append(img, rm);
      previews.appendChild(box);
    });
    drop.classList.toggle("filled", selectedFiles.length > 0);
    dropLabel.textContent = defaultDropLabel();
  }

  function showCreateMsg(kind, text) {
    createMsg.innerHTML = "";
    createMsg.appendChild(makeMsg(kind, text));
    requestAnimationFrame(() => syncHeight(false));
  }
  function clearCreateMsg() {
    createMsg.innerHTML = "";
    requestAnimationFrame(() => syncHeight(false));
  }

  $("resetBtn").addEventListener("click", () => {
    titleInput.value = "";
    contentInput.value = "";
    selectedFiles = [];
    renderPreviews();
    clearCreateMsg();
    titleInput.focus();
  });

  submitBtn.addEventListener("click", async () => {
    const title = titleInput.value.trim();
    const content = contentInput.value.trim();
    if (!title) {
      showCreateMsg("err", t("enterTitle"));
      titleInput.focus();
      return;
    }

    const fd = new FormData();
    fd.append("title", title);
    fd.append("content", content);
    selectedFiles.forEach((f) => fd.append("files", f, f.name));

    submitBtn.disabled = true;
    const oldHTML = submitBtn.innerHTML;
    submitBtn.textContent = t("publishing");
    clearCreateMsg();

    try {
      const res = await fetch("/api/posts", { method: "POST", body: fd });

      if (!res.ok) {
        const msg = await readError(res);
        showCreateMsg("err", msg);
        return;
      }

      const data = await res.json().catch(() => null);
      if (!data || !data.code) {
        showCreateMsg("err", "Invalid server response");
        return;
      }

      titleInput.value = "";
      contentInput.value = "";
      selectedFiles = [];
      renderPreviews();
      clearCreateMsg();

      const code = data.code;

      const newHash = "#" + code + "?lang=" + currentLang;
      if (window.location.hash !== newHash) {
        history.replaceState(null, "", newHash);
      }

      setMode("find");
      otpCells.forEach((c, i) => { c.value = code[i] || ""; });
      lastSubmitted = code;
      updateOtpCopyState();
      runSearch(code);

      showCreatedModal(code, data.compressed_bytes);
    } catch (e) {
      showCreateMsg("err", t("networkError", { msg: e.message }));
    } finally {
      submitBtn.disabled = false;
      submitBtn.innerHTML = oldHTML;
    }
  });

  /* =========================================================
     MODAL
     ========================================================= */
  const createdModal  = $("createdModal");
  const modalCard     = $("modalCard");
  const modalCode     = $("modalCode");
  const modalHint     = $("modalHint");
  const modalCopyBtn  = $("modalCopyBtn");
  const modalCloseBtn = $("modalCloseBtn");

  let modalCopyTimer = null;

  function showCreatedModal(code, bytes) {
    modalCode.textContent = code;
    modalHint.dataset.size = formatBytes(bytes);
    modalHint.textContent = t("memoryUsage", { size: formatBytes(bytes) });
    const label = modalCopyBtn.querySelector("span");
    if (label) label.textContent = t("copy");
    createdModal.hidden = false;
  }
  function closeCreatedModal() { createdModal.hidden = true; }

  modalCopyBtn.addEventListener("click", async () => {
    const label = modalCopyBtn.querySelector("span");
    try {
      await navigator.clipboard.writeText(modalCode.textContent || "");
      if (label) label.textContent = t("copied");
    } catch {
      if (label) label.textContent = t("copyError");
    }
    clearTimeout(modalCopyTimer);
    modalCopyTimer = setTimeout(() => { if (label) label.textContent = t("copy"); }, 1500);
  });

  modalCloseBtn.addEventListener("click", closeCreatedModal);
  createdModal.addEventListener("click", (e) => { if (e.target === createdModal) closeCreatedModal(); });
  modalCard.addEventListener("click", (e) => e.stopPropagation());

  /* =========================================================
     SEARCH
     ========================================================= */
  const otp         = $("otp");
  const otpCells    = Array.from(document.querySelectorAll(".otp-cell"));
  const otpCopyBtn  = $("otpCopyBtn");
  const searchFrame = $("searchFrame");

  let searchSeq = 0;
  let lastSubmitted = "";
  let otpCopyTimer = null;

  function getCode() { return otpCells.map(c => c.value).join(""); }

  function updateOtpCopyState() {
    otpCopyBtn.disabled = getCode().length !== 6;
  }

  function clearOtp() {
    otpCells.forEach(c => c.value = "");
    otpCells[0].focus();
    lastSubmitted = "";
    updateOtpCopyState();
  }

  function shakeOtp() {
    otp.classList.remove("shake");
    void otp.offsetWidth;
    otp.classList.add("shake");
    setTimeout(() => otp.classList.remove("shake"), 400);
  }

  function hideSearchFrame() {
    searchFrame.hidden = true;
    searchFrame.innerHTML = "";
  }

  function maybeSearch() {
    const code = getCode();
    if (code.length === 6) {
      if (code === lastSubmitted) return;
      lastSubmitted = code;
      runSearch(code);
    } else {
      searchSeq++;
      hideSearchFrame();
      lastSubmitted = "";
    }
    updateOtpCopyState();
  }

  otpCopyBtn.addEventListener("click", async () => {
    const code = getCode();
    if (code.length !== 6) return;
    try {
      await navigator.clipboard.writeText(code);
      otpCopyBtn.classList.add("copied");
      clearTimeout(otpCopyTimer);
      otpCopyTimer = setTimeout(() => otpCopyBtn.classList.remove("copied"), 1500);
    } catch {}
  });

  otpCells.forEach((cell, i) => {
    cell.addEventListener("focus", () => cell.select());
    cell.addEventListener("input", (e) => {
      const v = (e.target.value || "").replace(/\D/g, "");
      if (!v) { e.target.value = ""; maybeSearch(); return; }
      e.target.value = v.slice(-1);
      if (i < otpCells.length - 1) otpCells[i + 1].focus();
      maybeSearch();
    });
    cell.addEventListener("keydown", (e) => {
      if (e.key === "Backspace") {
        if (!cell.value && i > 0) {
          otpCells[i - 1].value = "";
          otpCells[i - 1].focus();
          e.preventDefault();
        }
        setTimeout(maybeSearch, 0);
      } else if (e.key === "ArrowLeft" && i > 0) {
        otpCells[i - 1].focus(); e.preventDefault();
      } else if (e.key === "ArrowRight" && i < otpCells.length - 1) {
        otpCells[i + 1].focus(); e.preventDefault();
      } else if (e.key === "Enter") {
        const c = getCode();
        if (c.length === 6) { lastSubmitted = c; runSearch(c); }
      }
    });
    cell.addEventListener("paste", (e) => {
      e.preventDefault();
      const text = (e.clipboardData || window.clipboardData).getData("text") || "";
      const digits = text.replace(/\D/g, "").slice(0, 6).split("");
      digits.forEach((d, j) => { if (otpCells[j]) otpCells[j].value = d; });
      const next = Math.min(digits.length, otpCells.length - 1);
      otpCells[next].focus();
      maybeSearch();
    });
  });

  updateOtpCopyState();

  function showSearchFrame() {
    searchFrame.hidden = false;
    requestAnimationFrame(() => syncHeight(false));
  }

  function renderSpinner() {
    searchFrame.innerHTML =
      '<div class="center"><div class="spinner"></div>' +
      '<div class="spinner-label">' + t("searching") + '</div></div>';
    showSearchFrame();
  }

  function renderError(text) {
    searchFrame.innerHTML = "";
    searchFrame.appendChild(makeMsg("err", text));
    showSearchFrame();
  }

  /* =========================================================
     LIGHTBOX
     ========================================================= */
  const lightbox    = $("lightbox");
  const lbViewport  = $("lbViewport");
  const lbTransform = $("lbTransform");
  const lbImg       = $("lbImg");
  const lbCounter   = $("lbCounter");
  const lbPrev      = $("lbPrev");
  const lbNext      = $("lbNext");
  const lbClose     = $("lbClose");
  const lbZoomBadge = $("lbZoomBadge");

  let lbCode = null;
  let lbPhotos = [];
  let lbIndex = 0;

  let zoom = 1, panX = 0, panY = 0;
  const MIN_ZOOM = 1, MAX_ZOOM = 8;
  let isPanning = false, panStartX = 0, panStartY = 0;
  let badgeTimer = null;

  function applyTransform() {
    lbTransform.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
    lbViewport.style.cursor = (zoom > 1.001) ? (isPanning ? "grabbing" : "grab") : "default";
  }

  function showZoomBadge() {
    lbZoomBadge.textContent = Math.round(zoom * 100) + "%";
    lbZoomBadge.classList.add("visible");
    clearTimeout(badgeTimer);
    badgeTimer = setTimeout(() => lbZoomBadge.classList.remove("visible"), 900);
  }

  function resetZoom(animate) {
    if (animate) lbTransform.style.transition = "transform .2s ease";
    zoom = 1; panX = 0; panY = 0;
    applyTransform();
    if (animate) setTimeout(() => { lbTransform.style.transition = ""; }, 220);
    showZoomBadge();
  }

  function zoomAt(clientX, clientY, newZoom) {
    newZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, newZoom));
    if (Math.abs(newZoom - zoom) < 1e-4) return;

    const vrect = lbViewport.getBoundingClientRect();
    const Cx = vrect.left + vrect.width  / 2;
    const Cy = vrect.top  + vrect.height / 2;

    const dcx = clientX - Cx;
    const dcy = clientY - Cy;

    const ratio = newZoom / zoom;
    panX = dcx - (dcx - panX) * ratio;
    panY = dcy - (dcy - panY) * ratio;
    zoom = newZoom;

    lbTransform.style.transition = "";
    applyTransform();
    showZoomBadge();
  }

  function openLightbox(code, photos, index) {
    lbCode = code;
    lbPhotos = photos;
    lbIndex = index;

    zoom = 1; panX = 0; panY = 0;
    lbTransform.style.transition = "";
    applyTransform();

    lbImg.src = "/api/photos/" + encodeURIComponent(code) + "/" + index;
    lbImg.alt = (photos[index] && photos[index].name) || "";

    lbCounter.textContent = (index + 1) + " / " + photos.length;
    lbPrev.hidden = photos.length < 2;
    lbNext.hidden = photos.length < 2;
    lightbox.hidden = false;
  }

  function closeLightbox() {
    lightbox.hidden = true;
    lbImg.removeAttribute("src");
    lbPhotos = [];
    lbCode = null;
  }

  function lbStep(dir) {
    if (lbPhotos.length < 2) return;
    lbIndex = (lbIndex + dir + lbPhotos.length) % lbPhotos.length;
    zoom = 1; panX = 0; panY = 0;
    lbTransform.style.transition = "";
    applyTransform();
    lbImg.src = "/api/photos/" + encodeURIComponent(lbCode) + "/" + lbIndex;
    lbImg.alt = (lbPhotos[lbIndex] && lbPhotos[lbIndex].name) || "";
    lbCounter.textContent = (lbIndex + 1) + " / " + lbPhotos.length;
  }

  lbPrev.addEventListener("click", (e) => { e.stopPropagation(); lbStep(-1); });
  lbNext.addEventListener("click", (e) => { e.stopPropagation(); lbStep(1); });
  lbClose.addEventListener("click", (e) => { e.stopPropagation(); closeLightbox(); });

  lbViewport.addEventListener("click", (e) => {
    if (e.target === lbViewport && zoom <= 1.001) closeLightbox();
  });

  lbViewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
    zoomAt(e.clientX, e.clientY, zoom * factor);
  }, { passive: false });

  lbViewport.addEventListener("mousedown", (e) => {
    if (e.button === 2) {
      e.preventDefault();
      if (zoom > 1.05) resetZoom(true);
      else zoomAt(e.clientX, e.clientY, 2);
      return;
    }
    if (e.button === 0 && zoom > 1.001) {
      e.preventDefault();
      isPanning = true;
      panStartX = e.clientX - panX;
      panStartY = e.clientY - panY;
      lbTransform.style.transition = "none";
      lbViewport.style.cursor = "grabbing";
    }
  });

  lbViewport.addEventListener("dblclick", (e) => {
    e.preventDefault();
    if (zoom > 1.05) resetZoom(true);
    else zoomAt(e.clientX, e.clientY, 2);
  });

  window.addEventListener("mousemove", (e) => {
    if (!isPanning) return;
    panX = e.clientX - panStartX;
    panY = e.clientY - panStartY;
    applyTransform();
  });

  window.addEventListener("mouseup", (e) => {
    if (e.button === 0 && isPanning) {
      isPanning = false;
      lbTransform.style.transition = "";
      lbViewport.style.cursor = zoom > 1.001 ? "grab" : "default";
    }
  });

  let touchStartDist = 0, touchStartZoom = 1;
  lbViewport.addEventListener("touchstart", (e) => {
    if (e.touches.length === 2) {
      touchStartDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      touchStartZoom = zoom;
    }
  }, { passive: true });

  lbViewport.addEventListener("touchmove", (e) => {
    if (e.touches.length === 2 && touchStartDist > 0) {
      e.preventDefault();
      const dist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      const cx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
      const cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      zoomAt(cx, cy, touchStartZoom * (dist / touchStartDist));
    }
  }, { passive: false });

  lbViewport.addEventListener("touchend", () => { touchStartDist = 0; });

  /* =========================================================
     POST RENDER
     ========================================================= */
  function renderPost(post) {
    searchFrame.innerHTML = "";

    const title = document.createElement("h3");
    title.className = "post-title";
    title.textContent = post.title;

    const meta = document.createElement("div");
    meta.className = "post-meta";

    const date = document.createElement("span");
    try { date.textContent = new Date(post.created).toLocaleString(currentLang); } catch {}
    meta.appendChild(date);

    if (post.photos && post.photos.length) {
      const cnt = document.createElement("span");
      cnt.textContent = t("photoCount", { n: post.photos.length });
      meta.appendChild(cnt);
    }

    searchFrame.append(title, meta);

    if (post.content) {
      const body = document.createElement("div");
      body.className = "post-body";
      body.textContent = post.content;
      searchFrame.appendChild(body);
    }

    if (post.photos && post.photos.length) {
      const gallery = document.createElement("div");
      gallery.className = "post-gallery";
      post.photos.forEach((p, idx) => {
        const img = document.createElement("img");
        img.src = "/api/photos/" + encodeURIComponent(post.code) + "/" + idx;
        img.alt = p.name || "photo";
        img.title = p.name || "";
        img.loading = "lazy";
        img.decoding = "async";
        img.addEventListener("click", () => openLightbox(post.code, post.photos, idx));
        gallery.appendChild(img);
      });
      searchFrame.appendChild(gallery);
    }

    showSearchFrame();
  }

  async function runSearch(code) {
    const mySeq = ++searchSeq;
    renderSpinner();
    await new Promise((r) => setTimeout(r, 380));
    if (mySeq !== searchSeq) return;

    try {
      const res = await fetch("/api/posts/" + encodeURIComponent(code));
      if (mySeq !== searchSeq) return;
      if (!res.ok) {
        const msg = await readError(res);
        renderError(msg || t("notFound"));
        shakeOtp();
        setTimeout(clearOtp, 320);
        return;
      }
      const post = await res.json().catch(() => null);
      if (mySeq !== searchSeq) return;
      if (!post) { renderError(t("notFound")); return; }
      renderPost(post);
    } catch (e) {
      if (mySeq !== searchSeq) return;
      renderError(t("networkError", { msg: e.message }));
      shakeOtp();
      setTimeout(clearOtp, 320);
    }
  }

  /* =========================================================
     INIT
     ========================================================= */
  function initFromUrl() {
    const { code, lang } = parseHash();
    const targetLang = lang || detectLang();
    applyI18n(targetLang);

    if (code) {
      setMode("find", true);
      otpCells.forEach((c, i) => { c.value = code[i] || ""; });
      lastSubmitted = code;
      updateOtpCopyState();
      runSearch(code);
    } else {
      setMode("create", true);
    }
    requestAnimationFrame(() => syncHeight(false));
  }

  initFromUrl();

  window.addEventListener("hashchange", () => {
    const { code, lang } = parseHash();
    if (lang && lang !== currentLang) {
      applyI18n(lang);
      if (mode === "find" && getCode().length === 6) {
        lastSubmitted = "";
        runSearch(getCode());
      }
    }
    if (code && mode !== "find") {
      setMode("find", true);
      otpCells.forEach((c, i) => { c.value = code[i] || ""; });
      lastSubmitted = code;
      updateOtpCopyState();
      runSearch(code);
    }
  });

  document.addEventListener("keydown", (e) => {
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


# ============================================================
# РОУТЫ
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(LANDING.replace("__FAVICON__", FAVICON))


@app.get("/app", response_class=HTMLResponse)
async def app_page():
    return HTMLResponse(APP.replace("__FAVICON__", FAVICON))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
