"""
Sld-Networking — посты с 6-значным кодом (без повторяющихся цифр).
Хранение: оперативная память, AES-256-GCM + zstd/gzip.
Запуск: pip install fastapi uvicorn python-multipart cryptography zstandard && python main.py
"""

from __future__ import annotations

import gzip
import json
import os
import secrets
import string
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

import uvicorn
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import HTMLResponse

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
            # secrets.sample — выборка без повторов
            code = "".join(secrets.sample(string.digits, 6))
            if code not in _store:
                return code
    raise HTTPException(503, "Storage overflow")


# ---------- приложение ----------
app = FastAPI(title="Sld-Networking", docs_url=None, redoc_url=None)


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
            raise HTTPException(400, f"File «{f.filename}» larger than {MAX_PHOTO_BYTES // (1024*1024)} MB")
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
    except (InvalidTag, ValueError, OSError):
        raise HTTPException(500, "Decryption failed")

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
    except (InvalidTag, ValueError):
        raise HTTPException(500, "Decryption failed")

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


# ---------- фронтенд ----------
FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E"
    "%3Cstop offset='0' stop-color='%23a78bfa'/%3E"
    "%3Cstop offset='1' stop-color='%2338bdf8'/%3E%3C/linearGradient%3E%3C/defs%3E"
    "%3Crect width='32' height='32' rx='8' fill='%230b0b10'/%3E"
    "%3Cg fill='none' stroke='url(%23g)' stroke-width='1.8' stroke-linecap='round'%3E"
    "%3Ccircle cx='16' cy='10' r='3'/%3E"
    "%3Ccircle cx='10' cy='22' r='3'/%3E"
    "%3Ccircle cx='22' cy='22' r='3'/%3E"
    "%3Cpath d='M14 12.5L11 19M18 12.5L21 19M13 22h6'/%3E"
    "%3C/g%3E%3C/svg%3E"
)

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1">
<meta name="theme-color" content="#0b0b10">
<title>Sld-Networking</title>
<link rel="icon" href="__FAVICON__">
<style>
  *,*::before,*::after{box-sizing:border-box}
  ::-webkit-scrollbar{width:0;height:0;display:none}
  *{scrollbar-width:none;-ms-overflow-style:none;-webkit-tap-highlight-color:transparent}

  html,body{user-select:none;-webkit-user-select:none;-moz-user-select:none;-ms-user-select:none}
  input,textarea,[contenteditable],.modal-code{user-select:text;-webkit-user-select:text;-moz-user-select:text;-ms-user-select:text}

  ::selection{background:rgba(167,139,250,.38);color:#fff}
  ::-moz-selection{background:rgba(167,139,250,.38);color:#fff}
  input::selection,textarea::selection{background:rgba(167,139,250,.45);color:#fff}
  input::-moz-selection,textarea::-moz-selection{background:rgba(167,139,250,.45);color:#fff}

  :root{
    --glass-bg:rgba(28,28,36,.55);
    --glass-bg-hi:rgba(38,38,48,.65);
    --glass-border:rgba(255,255,255,.10);
    --glass-border-hi:rgba(255,255,255,.22);
    --text:#EDEDF2;
    --text-dim:#9A9AA6;
    --text-mute:#61616E;
    --danger:#F0A0A0;
    --ok:#9BDCB0;
    --ease-out:cubic-bezier(.22,1,.36,1);
    --dur:.55s;
  }

  html,body{
    margin:0;padding:0;background:#0b0b10;color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,"Helvetica Neue",Arial,sans-serif;
    -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
    font-size:14px;line-height:1.5;min-height:100%;overflow-x:hidden;
  }

  body::before{
    content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
    background:
      radial-gradient(ellipse 70% 55% at 15% 5%, rgba(124,94,214,.30), transparent 65%),
      radial-gradient(ellipse 65% 55% at 90% 95%, rgba(56,140,190,.26), transparent 65%),
      #0b0b10;
  }

  .app{
    min-height:100dvh;
    display:flex;flex-direction:column;align-items:center;
    padding:clamp(44px,10vh,110px) 16px 48px;
    gap:22px;
  }

  /* ---------- кнопки ---------- */
  .menu{
    display:flex;gap:12px;justify-content:center;align-items:stretch;
    flex-wrap:nowrap;width:100%;max-width:520px;
  }
  .btn{
    flex:1 1 0;min-width:0;height:52px;
    display:inline-flex;align-items:center;justify-content:center;gap:9px;
    padding:0 20px;
    border:1px solid var(--glass-border);
    border-radius:100px;
    background:var(--glass-bg);
    color:var(--text);
    font:inherit;font-size:14.5px;font-weight:500;letter-spacing:.15px;
    cursor:pointer;user-select:none;-webkit-tap-highlight-color:transparent;
    backdrop-filter:blur(14px) saturate(140%);
    -webkit-backdrop-filter:blur(14px) saturate(140%);
    /* убрано inset 0 1px 0 — из-за него был артефакт на верхней кромке */
    box-shadow:0 6px 20px rgba(0,0,0,.28);
    transition:background .18s ease,border-color .18s ease,color .18s ease,transform .08s ease;
    transform:translateZ(0);
    -webkit-backface-visibility:hidden;
    backface-visibility:hidden;
  }
  .btn svg{width:18px;height:18px;flex-shrink:0;display:block}
  .btn:hover{background:var(--glass-bg-hi);border-color:var(--glass-border-hi)}
  .btn:active{transform:translateZ(0) scale(.98)}
  .btn.active{
    background:rgba(255,255,255,.92);color:#0b0b10;
    border-color:rgba(255,255,255,.95);
    box-shadow:0 6px 22px rgba(255,255,255,.10);
  }
  .btn.primary{
    background:rgba(255,255,255,.92);color:#0b0b10;
    border-color:rgba(255,255,255,.95);
  }
  .btn.primary:hover{background:#fff}

  /* ---------- сцена ---------- */
  .stage{
    position:relative;
    width:100%;max-width:520px;
    overflow:hidden;
    transition:height var(--dur) var(--ease-out);
    isolation:isolate;
    contain:paint layout style;
    transform:translateZ(0);
    -webkit-backface-visibility:hidden;
    backface-visibility:hidden;
  }
  .stage[hidden]{display:none}

  .panel{
    position:absolute;
    top:0;left:0;right:0;
    display:flex;flex-direction:column;gap:12px;
    opacity:0;
    pointer-events:none;
    transform:translate3d(var(--enter-x,26px),0,0) scale(.985);
    transition:
      opacity .30s cubic-bezier(.4,0,.2,1),
      transform var(--dur) var(--ease-out);
    will-change:transform,opacity;
    -webkit-backface-visibility:hidden;
    backface-visibility:hidden;
    isolation:isolate;
  }
  .panel.active{
    position:relative;
    opacity:1;
    pointer-events:auto;
    transform:translate3d(0,0,0) scale(1);
  }

  /* .frame — убран inset highlight, из-за которого была прямая линия сверху */
  .frame{
    border:1px solid var(--glass-border);
    border-radius:22px;
    background:var(--glass-bg);
    backdrop-filter:blur(16px) saturate(150%);
    -webkit-backdrop-filter:blur(16px) saturate(150%);
    box-shadow:0 14px 34px rgba(0,0,0,.32);
    padding:22px;
    transform:translateZ(0);
    -webkit-backface-visibility:hidden;
    backface-visibility:hidden;
    isolation:isolate;
  }

  @supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){
    .frame{background:rgba(28,28,36,.88)}
    .btn{background:rgba(28,28,36,.85)}
  }

  /* ---------- поля ---------- */
  .input-wrap{position:relative;display:flex}
  .input-wrap + .input-wrap{margin-top:12px}
  .input-wrap .iw-icon{
    position:absolute;left:14px;width:18px;height:18px;
    color:var(--text-dim);pointer-events:none;
    transition:color .15s ease;
  }
  .input-wrap input.field,
  .input-wrap textarea.field{padding-left:44px}
  .input-wrap input.field + .iw-icon,
  .input-wrap textarea.field + .iw-icon{top:14px}
  .input-wrap.textarea-wrap .iw-icon{top:15px}
  .input-wrap:focus-within .iw-icon{color:var(--text)}

  .field{
    width:100%;
    background:rgba(255,255,255,.04);
    border:1px solid var(--glass-border);
    border-radius:14px;
    padding:13px 16px;
    color:var(--text);font:inherit;font-size:14.5px;
    outline:none;
    transition:border-color .15s ease,background .15s ease;
    -webkit-appearance:none;appearance:none;
  }
  .field::placeholder{color:var(--text-mute)}
  .field:focus{border-color:var(--glass-border-hi);background:rgba(255,255,255,.07)}

  textarea.field{min-height:180px;resize:none;line-height:1.55;font-family:inherit}

  .drop{
    margin-top:12px;
    border:1px dashed rgba(255,255,255,.18);
    border-radius:14px;
    padding:20px 16px;
    text-align:center;
    color:var(--text-dim);
    cursor:pointer;
    background:rgba(255,255,255,.02);
    line-height:1.6;
    transition:border-color .15s ease,color .15s ease,background .15s ease;
    display:flex;flex-direction:column;align-items:center;gap:8px;
  }
  .drop .drop-icon{width:26px;height:26px;color:var(--text-dim);transition:color .15s ease}
  .drop .drop-label{font-size:13.5px;color:var(--text-dim);transition:color .15s ease}
  .drop .drop-hint{font-size:11.5px;color:var(--text-mute)}
  .drop:hover{border-color:rgba(255,255,255,.32);background:rgba(255,255,255,.05)}
  .drop:hover .drop-icon,.drop:hover .drop-label{color:var(--text)}
  .drop.filled{border-style:solid;border-color:rgba(255,255,255,.25)}
  .drop.filled .drop-icon,.drop.filled .drop-label{color:var(--text)}

  .previews{
    display:grid;grid-template-columns:repeat(auto-fill,minmax(76px,1fr));
    gap:8px;margin-top:12px;
  }
  .preview{
    position:relative;aspect-ratio:1/1;border-radius:12px;overflow:hidden;
    background:rgba(255,255,255,.04);
    border:1px solid var(--glass-border);
  }
  .preview img{width:100%;height:100%;object-fit:cover;display:block}
  .preview button{
    position:absolute;top:4px;right:4px;width:22px;height:22px;border-radius:50%;
    border:1px solid rgba(255,255,255,.15);
    background:rgba(10,10,14,.72);color:var(--text);
    font-size:13px;line-height:1;
    cursor:pointer;display:flex;align-items:center;justify-content:center;
    -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);
  }
  .preview button:hover{background:rgba(70,26,26,.9)}

  .row{display:flex;gap:10px;margin-top:20px;justify-content:center;flex-wrap:wrap}
  .row .btn{flex:0 1 auto;min-width:130px;padding:0 22px}

  /* ---------- OTP + copy ---------- */
  .otp-row{
    display:flex;
    gap:12px;
    justify-content:center;
    align-items:center;
    flex-wrap:nowrap;
  }
  .otp{display:flex;gap:8px;justify-content:center;align-items:center;margin:0}
  .otp-cell{
    width:clamp(34px,10vw,46px);height:clamp(46px,12vw,56px);
    padding:0;text-align:center;
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    font-size:clamp(17px,4.6vw,21px);font-weight:600;
    color:var(--text);
    background:rgba(255,255,255,.05);
    border:1.5px solid var(--glass-border);
    border-radius:14px;
    outline:none;caret-color:transparent;
    -webkit-appearance:none;appearance:none;
    transition:background .15s ease,border-color .15s ease,transform .1s ease,box-shadow .15s ease;
  }
  .otp-cell:hover{background:rgba(255,255,255,.08)}
  .otp-cell:focus{
    background:rgba(255,255,255,.11);
    border-color:rgba(255,255,255,.55);
    box-shadow:0 0 0 3px rgba(255,255,255,.08);
    transform:translateY(-1px);
  }

  /* кнопка copy рядом с кодом */
  .otp-copy{
    flex-shrink:0;
    width:clamp(46px,12vw,56px);
    height:clamp(46px,12vw,56px);
    border-radius:14px;
    border:1.5px solid var(--glass-border);
    background:rgba(255,255,255,.05);
    color:var(--text-dim);
    display:flex;align-items:center;justify-content:center;
    cursor:pointer;
    -webkit-appearance:none;appearance:none;
    transition:background .15s ease,border-color .15s ease,color .15s ease,transform .1s ease;
    position:relative;
  }
  .otp-copy svg{width:20px;height:20px;pointer-events:none}
  .otp-copy:hover:not(:disabled){
    background:rgba(255,255,255,.10);
    border-color:var(--glass-border-hi);
    color:var(--text);
  }
  .otp-copy:active:not(:disabled){transform:scale(.94)}
  .otp-copy:disabled{opacity:.35;cursor:not-allowed}
  .otp-copy.copied{
    color:var(--ok);
    border-color:rgba(120,200,150,.45);
    background:rgba(30,80,50,.20);
  }
  .otp-copy .icon-check{display:none}
  .otp-copy.copied .icon-copy{display:none}
  .otp-copy.copied .icon-check{display:block}

  @keyframes shake{
    0%,100%{transform:translateX(0)}
    20%{transform:translateX(-8px)}
    40%{transform:translateX(8px)}
    60%{transform:translateX(-5px)}
    80%{transform:translateX(5px)}
  }
  .otp.shake{animation:shake .34s ease}
  .otp.shake .otp-cell{border-color:rgba(200,90,90,.65);background:rgba(90,20,20,.15)}

  /* ---------- статусы ---------- */
  .center{text-align:center;padding:10px 0}
  .spinner{
    width:26px;height:26px;border-radius:50%;
    border:2.5px solid rgba(255,255,255,.12);
    border-top-color:rgba(255,255,255,.75);
    animation:spin .7s linear infinite;margin:0 auto;
  }
  @keyframes spin{to{transform:rotate(360deg)}}
  .spinner-label{margin-top:10px;font-size:12.5px;color:var(--text-dim);text-align:center}

  .msg{
    display:flex;align-items:flex-start;gap:10px;
    border-radius:14px;padding:12px 14px;font-size:13.5px;
    background:rgba(255,255,255,.05);color:var(--text);
    line-height:1.5;border:1px solid var(--glass-border);
    margin-top:14px;
  }
  .msg:first-child{margin-top:0}
  .msg svg{width:18px;height:18px;flex-shrink:0;margin-top:1px}
  .msg.err{background:rgba(120,40,40,.18);border-color:rgba(200,90,90,.28);color:var(--danger)}
  .msg.ok{background:rgba(30,80,50,.18);border-color:rgba(120,200,150,.25);color:var(--ok)}

  /* ---------- пост ---------- */
  .post-title{margin:0 0 10px;font-size:19px;font-weight:600;line-height:1.3;color:var(--text);word-break:break-word}
  .post-meta{font-size:12px;color:var(--text-dim);margin-bottom:14px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .post-body{font-size:14px;line-height:1.6;color:#d8d8de;white-space:pre-wrap;word-break:break-word}
  .post-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px;margin-top:16px}
  .post-gallery img{
    width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:12px;
    cursor:zoom-in;background:rgba(255,255,255,.04);
    border:1px solid var(--glass-border);
    transition:transform .15s ease,border-color .15s ease;
  }
  .post-gallery img:hover{transform:scale(1.02);border-color:var(--glass-border-hi)}

  .code-chip{
    display:inline-flex;align-items:center;
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    font-size:13px;letter-spacing:3px;color:var(--text);
    background:rgba(255,255,255,.06);
    border:1px solid var(--glass-border);
    border-radius:10px;padding:4px 10px;
  }

  /* ---------- лайтбокс ---------- */
  .lightbox{
    position:fixed;inset:0;z-index:1000;
    display:flex;align-items:center;justify-content:center;
    background:rgba(6,6,10,.9);
    backdrop-filter:blur(14px) saturate(140%);
    -webkit-backdrop-filter:blur(14px) saturate(140%);
    animation:lbIn .2s ease;
  }
  .lightbox[hidden]{display:none}
  @keyframes lbIn{from{opacity:0}to{opacity:1}}

  .lb-viewport{
    position:absolute;inset:0;
    display:flex;align-items:center;justify-content:center;
    overflow:hidden;cursor:default;
  }
  .lb-viewport.grabbing{cursor:grabbing}

  .lb-transform{
    display:flex;align-items:center;justify-content:center;
    transform-origin:center center;will-change:transform;
  }

  .lb-img{
    display:block;max-width:82vw;max-height:78vh;
    object-fit:contain;border-radius:12px;
    box-shadow:0 20px 60px rgba(0,0,0,.5);
    user-select:none;-webkit-user-select:none;-webkit-user-drag:none;
    background-color:#1c1c22;
    background-image:
      linear-gradient(45deg, rgba(255,255,255,.06) 25%, transparent 25%),
      linear-gradient(-45deg, rgba(255,255,255,.06) 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, rgba(255,255,255,.06) 75%),
      linear-gradient(-45deg, transparent 75%, rgba(255,255,255,.06) 75%);
    background-size:18px 18px;
    background-position:0 0, 0 9px, 9px -9px, -9px 0px;
  }

  .lb-btn{
    position:absolute;width:44px;height:44px;border-radius:50%;
    border:1px solid var(--glass-border);
    background:rgba(28,28,36,.7);
    color:var(--text);
    display:flex;align-items:center;justify-content:center;
    cursor:pointer;z-index:2;
    backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
    transition:background .15s ease,transform .1s ease;
    transform:translateZ(0);
  }
  .lb-btn svg{width:20px;height:20px;pointer-events:none}
  .lb-btn:hover{background:rgba(48,48,60,.85)}
  .lb-btn:active{transform:translateZ(0) scale(.94)}
  .lb-btn[hidden]{display:none}

  .lb-close{top:18px;right:18px}
  .lb-prev{left:18px;top:50%;transform:translateY(-50%) translateZ(0)}
  .lb-prev:active{transform:translateY(-50%) translateZ(0) scale(.94)}
  .lb-next{right:18px;top:50%;transform:translateY(-50%) translateZ(0)}
  .lb-next:active{transform:translateY(-50%) translateZ(0) scale(.94)}

  .lb-counter{
    position:absolute;bottom:20px;left:50%;transform:translateX(-50%);
    padding:6px 14px;border-radius:100px;
    background:rgba(28,28,36,.75);border:1px solid var(--glass-border);
    font-size:13px;color:var(--text);
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    letter-spacing:1px;
    z-index:2;pointer-events:none;
  }

  .lb-zoom-badge{
    position:absolute;top:18px;left:18px;
    padding:5px 12px;border-radius:100px;
    background:rgba(28,28,36,.75);border:1px solid var(--glass-border);
    font-size:12px;color:var(--text);
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    letter-spacing:1px;
    z-index:2;pointer-events:none;
    opacity:0;transition:opacity .15s ease;
  }
  .lb-zoom-badge.visible{opacity:1}

  .lb-hint{
    position:absolute;bottom:56px;left:50%;transform:translateX(-50%);
    font-size:11.5px;color:var(--text-mute);
    z-index:2;pointer-events:none;white-space:nowrap;
  }

  /* ---------- модалка ---------- */
  .modal{
    position:fixed;inset:0;z-index:900;
    display:flex;align-items:center;justify-content:center;
    background:rgba(6,6,10,.55);
    backdrop-filter:blur(10px) saturate(140%);
    -webkit-backdrop-filter:blur(10px) saturate(140%);
    animation:lbIn .2s ease;padding:20px;
  }
  .modal[hidden]{display:none}

  /* убран inset highlight — та же причина, что и у frame */
  .modal-card{
    width:100%;max-width:380px;
    padding:26px 24px;
    border:1px solid var(--glass-border);
    border-radius:22px;
    background:rgba(28,28,36,.94);
    box-shadow:0 20px 50px rgba(0,0,0,.5);
    text-align:center;
    animation:modalIn .32s var(--ease-out);
    transform:translateZ(0);
  }
  @keyframes modalIn{
    from{opacity:0;transform:translateY(12px) scale(.95) translateZ(0)}
    to{opacity:1;transform:translateY(0) scale(1) translateZ(0)}
  }

  .modal-icon{
    width:52px;height:52px;margin:0 auto 14px;
    border-radius:50%;
    display:flex;align-items:center;justify-content:center;
    background:rgba(120,200,150,.14);color:var(--ok);
    border:1px solid rgba(120,200,150,.28);
  }
  .modal-icon svg{width:26px;height:26px}

  .modal-title{font-size:17px;font-weight:600;margin:0 0 6px}
  .modal-sub{font-size:13px;color:var(--text-dim);margin:0 0 18px;line-height:1.5}
  .modal-code{
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    font-size:40px;font-weight:700;
    letter-spacing:12px;text-indent:12px;
    color:var(--text);margin:6px 0 6px;
  }
  .modal-hint{font-size:12px;color:var(--text-dim);margin-bottom:22px}
  .modal-actions{display:flex;gap:10px}
  .modal-actions .btn{flex:1;height:46px;padding:0 14px;font-size:14px}

  @media (max-width:560px){
    .app{padding:44px 14px 40px;gap:18px}
    .menu{gap:10px}
    .btn{padding:0 14px;font-size:14px}
    .btn svg{width:16px;height:16px}
    .frame{padding:18px;border-radius:20px}
    .row .btn{min-width:0;flex:1}
    .lb-prev{left:8px}
    .lb-next{right:8px}
    .lb-close{top:10px;right:10px}
    .lb-zoom-badge{top:10px;left:10px}
    .modal-code{font-size:34px;letter-spacing:9px;text-indent:9px}
    .lb-img{max-width:96vw;max-height:82vh}
    .lb-hint{display:none}
    .otp-row{gap:8px}
  }
  @media (max-width:380px){
    .btn span.btn-label{display:none}
    .btn{padding:0 12px}
  }
</style>
</head>
<body>

<main class="app">

  <nav class="menu">
    <button class="btn" id="btnCreate" type="button">
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
      <span class="btn-label" data-i18n="createPost">Create post</span>
    </button>
    <button class="btn" id="btnFind" type="button">
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
      <span class="btn-label" data-i18n="findPost">Find post</span>
    </button>
  </nav>

  <div class="stage" id="stage" hidden>

    <div class="panel" id="createPanel">
      <section class="frame">
        <div class="input-wrap">
          <input class="field" id="title" type="text" maxlength="120" autocomplete="off" spellcheck="false"
                 placeholder="Title" data-i18n-ph="titlePh">
          <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 7V5a1 1 0 011-1h14a1 1 0 011 1v2"/><path d="M9 20h6"/><path d="M12 4v16"/>
          </svg>
        </div>

        <div class="input-wrap textarea-wrap">
          <textarea class="field" id="content" placeholder="Content" data-i18n-ph="contentPh"></textarea>
          <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 6h16M4 12h16M4 18h10"/>
          </svg>
        </div>

        <div class="drop" id="drop">
          <svg class="drop-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="3"/>
            <circle cx="9" cy="9" r="2"/>
            <path d="M21 15l-5-5L5 21"/>
          </svg>
          <div class="drop-label" id="dropLabel" data-i18n="dropLabel">Click or drop photos</div>
          <div class="drop-hint" data-i18n="dropHint">up to 5 photos · Ctrl+V to paste</div>
        </div>
        <input type="file" id="fileInput" accept="image/*" multiple hidden>
        <div class="previews" id="previews"></div>

        <div class="row">
          <button class="btn primary" id="submitBtn" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px">
              <path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/>
            </svg>
            <span data-i18n="publish">Publish</span>
          </button>
          <button class="btn" id="resetBtn" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px">
              <path d="M3 6h18"/><path d="M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/>
            </svg>
            <span data-i18n="clear">Clear</span>
          </button>
        </div>

        <div id="createMsg"></div>
      </section>
    </div>

    <div class="panel" id="findPanel">
      <section class="frame">
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
                  data-i18n-title="copyCode" title="Copy code" aria-label="Copy code">
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

      <section class="frame" id="searchFrame" hidden></section>
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
    <h3 class="modal-title" data-i18n="postCreated">Post created</h3>
    <p class="modal-sub" data-i18n="postCreatedSub">Save the code — you can find the post anytime with it</p>
    <div class="modal-code" id="modalCode">000000</div>
    <div class="modal-hint" id="modalHint"></div>
    <div class="modal-actions">
      <button class="btn" id="modalCopyBtn" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px">
          <rect x="9" y="9" width="13" height="13" rx="2"/>
          <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
        </svg>
        <span data-i18n="copy">Copy</span>
      </button>
      <button class="btn primary" id="modalCloseBtn" type="button" data-i18n="done">Done</button>
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
      <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
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
  <div class="lb-hint" data-i18n="lbHint">wheel — zoom · RMB — 1×/2× · dblclick — reset · LMB — pan</div>
</div>

<script>
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);

  document.addEventListener("contextmenu", (e) => e.preventDefault());

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
      rejectedFiles: "{n} файл(ов) пропущено: только изображения и не больше {max}.",
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
      rejectedFiles: "{n} file(s) skipped: images only, max {max}.",
      lbHint: "wheel — zoom · RMB — 1×/2× · dblclick — reset · LMB — pan"
    }
  };

  let currentLang = "en";

  function t(key, params) {
    let s = (I18N[currentLang] && I18N[currentLang][key]) || I18N.en[key] || key;
    if (params) {
      for (const k in params) s = s.replace("{" + k + "}", params[k]);
    }
    return s;
  }

  function applyI18n(lang) {
    currentLang = SUPPORTED.includes(lang) ? lang : "en";
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
    const nav = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || "en"];
    for (const l of nav) {
      const code = String(l || "").toLowerCase().split("-")[0];
      if (SUPPORTED.includes(code)) return code;
    }
    return "en";
  }

  /* =========================================================
     Иконки
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
    s.textContent = text;
    d.appendChild(s);
    return d;
  }

  function formatBytes(b) {
    if (b < 1024) return b + " B";
    if (b < 1024 * 1024) return (b / 1024).toFixed(1).replace(".", ",") + " KB";
    if (b < 1024 * 1024 * 1024) return (b / (1024 * 1024)).toFixed(2).replace(".", ",") + " MB";
    return (b / (1024 * 1024 * 1024)).toFixed(2).replace(".", ",") + " GB";
  }

  /* =========================================================
     Табы
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
  window.addEventListener("resize", () => syncHeight(false));

  function setMode(next, instant = false) {
    if (next === mode && !instant) return;

    const firstShow = stage.hidden;
    const incoming  = next === "create" ? createPanel : findPanel;
    const outgoing  = next === "create" ? findPanel   : createPanel;

    const goLeft = (next === "create");
    const enterX = goLeft ? -26 : 26;
    const exitX  = goLeft ?  26 : -26;

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

    if (next === "create") setTimeout(() => $("title").focus(), 140);
    else setTimeout(() => otpCells[0].focus(), 140);
  }

  btnCreate.addEventListener("click", () => setMode("create"));
  btnFind.addEventListener("click", () => setMode("find"));

  /* =========================================================
     Создание поста
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
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.style.borderColor = "rgba(255,255,255,.4)"; });
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
  }
  function clearCreateMsg() { createMsg.innerHTML = ""; }

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
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showCreateMsg("err", data.detail || "Error");
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
     Модалка
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
     Поиск
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

  // Copy-кнопка рядом с OTP
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
    searchFrame.style.animation = "none";
    void searchFrame.offsetWidth;
    searchFrame.style.animation = "";
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
     Лайтбокс с зумом
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
    lbTransform.style.transform = `translate3d(${panX}px, ${panY}px, 0) scale(${zoom})`;
    lbViewport.style.cursor = (zoom > 1.001) ? (isPanning ? "grabbing" : "grab") : "default";
  }

  function showZoomBadge() {
    lbZoomBadge.textContent = Math.round(zoom * 100) + "%";
    lbZoomBadge.classList.add("visible");
    clearTimeout(badgeTimer);
    badgeTimer = setTimeout(() => lbZoomBadge.classList.remove("visible"), 900);
  }

  function resetZoom(animate) {
    if (animate) lbTransform.style.transition = "transform .22s cubic-bezier(.22,1,.36,1)";
    zoom = 1; panX = 0; panY = 0;
    applyTransform();
    if (animate) setTimeout(() => { lbTransform.style.transition = ""; }, 240);
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
     Отрисовка поста
     ========================================================= */
  function renderPost(post) {
    searchFrame.innerHTML = "";

    const title = document.createElement("h3");
    title.className = "post-title";
    title.textContent = post.title;

    const meta = document.createElement("div");
    meta.className = "post-meta";

    const chip = document.createElement("span");
    chip.className = "code-chip";
    chip.textContent = post.code;

    const date = document.createElement("span");
    try { date.textContent = new Date(post.created).toLocaleString(currentLang); } catch {}
    meta.append(chip, date);

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
    await new Promise((r) => setTimeout(r, 420));
    if (mySeq !== searchSeq) return;

    try {
      const res = await fetch("/api/posts/" + encodeURIComponent(code));
      if (mySeq !== searchSeq) return;
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        renderError(data.detail || t("notFound"));
        shakeOtp();
        setTimeout(clearOtp, 320);
        return;
      }
      const post = await res.json();
      if (mySeq !== searchSeq) return;
      renderPost(post);
    } catch (e) {
      if (mySeq !== searchSeq) return;
      renderError(t("networkError", { msg: e.message }));
      shakeOtp();
      setTimeout(clearOtp, 320);
    }
  }

  /* =========================================================
     Инициализация
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

PAGE = PAGE.replace("__FAVICON__", FAVICON)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(PAGE)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
