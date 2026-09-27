"""
Sld-Networking — посты с 6-значным кодом.
Хранение: оперативная память, сжатие (gzip) + шифрование (Fernet/AES).
Запуск: pip install fastapi uvicorn python-multipart cryptography && python main.py
"""

from __future__ import annotations

import base64
import gzip
import json
import secrets
import string
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

import uvicorn
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

# ---------- память ----------
_fernet = Fernet(Fernet.generate_key())
_store: Dict[str, bytes] = {}
_lock = threading.Lock()

MAX_PHOTOS = 5
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_TITLE_LEN = 120
MAX_CONTENT_LEN = 20_000


def _pack(payload: dict) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _fernet.encrypt(gzip.compress(raw, compresslevel=9))


def _unpack(blob: bytes) -> dict:
    return json.loads(gzip.decompress(_fernet.decrypt(blob)).decode("utf-8"))


def _new_code() -> str:
    with _lock:
        for _ in range(5000):
            code = "".join(secrets.choice(string.digits) for _ in range(6))
            if code not in _store:
                return code
    raise HTTPException(503, "Хранилище переполнено")


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
        raise HTTPException(400, "Название не может быть пустым")
    if len(title) > MAX_TITLE_LEN:
        raise HTTPException(400, f"Название длиннее {MAX_TITLE_LEN} символов")
    if len(content) > MAX_CONTENT_LEN:
        raise HTTPException(400, f"Содержимое длиннее {MAX_CONTENT_LEN} символов")

    files = [f for f in (files or []) if f and f.filename]
    if len(files) > MAX_PHOTOS:
        raise HTTPException(400, f"Максимум {MAX_PHOTOS} фото")

    photos = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(400, f"Файл «{f.filename}» больше {MAX_PHOTO_BYTES // (1024*1024)} МБ")
        photos.append({
            "name": f.filename,
            "mime": f.content_type or "image/jpeg",
            "data": base64.b64encode(data).decode("ascii"),
        })

    payload = {
        "title": title,
        "content": content,
        "photos": photos,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    blob = _pack(payload)
    code = _new_code()
    with _lock:
        _store[code] = blob
    return {"code": code, "compressed_bytes": len(blob), "photos": len(photos)}


@app.get("/api/posts/{code}")
async def get_post(code: str):
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(400, "Код должен состоять из 6 цифр")
    with _lock:
        blob = _store.get(code)
    if blob is None:
        raise HTTPException(404, "Пост с таким кодом не найден")
    try:
        payload = _unpack(blob)
    except (InvalidToken, OSError, ValueError):
        raise HTTPException(500, "Не удалось расшифровать пост")
    payload["code"] = code
    return payload


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
<html lang="ru">
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
  input,textarea,[contenteditable]{user-select:text;-webkit-user-select:text;-moz-user-select:text;-ms-user-select:text}

  :root{
    --glass-bg:rgba(255,255,255,.055);
    --glass-bg-hi:rgba(255,255,255,.10);
    --glass-border:rgba(255,255,255,.10);
    --glass-border-hi:rgba(255,255,255,.22);
    --text:#EDEDF2;
    --text-dim:#9A9AA6;
    --text-mute:#61616E;
    --danger:#F0A0A0;
    --ok:#9BDCB0;
    --ease:cubic-bezier(.32,.72,0,1);
    --dur:.48s;
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
      radial-gradient(ellipse 70% 55% at 15% 5%, rgba(124,94,214,.35), transparent 65%),
      radial-gradient(ellipse 65% 55% at 90% 95%, rgba(56,140,190,.30), transparent 65%),
      radial-gradient(ellipse 90% 60% at 50% 50%, rgba(20,20,30,.6), transparent 80%),
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
    backdrop-filter:blur(18px) saturate(150%);
    -webkit-backdrop-filter:blur(18px) saturate(150%);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.10),0 6px 20px rgba(0,0,0,.28);
    transition:background .18s ease,border-color .18s ease,color .18s ease,transform .08s ease;
  }
  .btn svg{width:18px;height:18px;flex-shrink:0;display:block}
  .btn:hover{background:var(--glass-bg-hi);border-color:var(--glass-border-hi)}
  .btn:active{transform:scale(.98)}
  .btn.active{
    background:rgba(255,255,255,.92);color:#0b0b10;
    border-color:rgba(255,255,255,.95);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.6),0 6px 22px rgba(255,255,255,.10);
  }
  .btn.primary{
    background:rgba(255,255,255,.92);color:#0b0b10;
    border-color:rgba(255,255,255,.95);
  }
  .btn.primary:hover{background:#fff}

  /* ---------- сцена ---------- */
  .stage{
    width:100%;max-width:520px;
    overflow:hidden;position:relative;
    transition:height var(--dur) var(--ease);
  }
  .stage[hidden]{display:none}

  .track{
    display:flex;align-items:flex-start;width:100%;
    transition:transform var(--dur) var(--ease);
    will-change:transform;
  }

  .panel{
    flex:0 0 100%;min-width:0;width:100%;
    display:flex;flex-direction:column;gap:12px;
  }

  .frame{
    border:1px solid var(--glass-border);
    border-radius:22px;
    background:var(--glass-bg);
    backdrop-filter:blur(22px) saturate(160%);
    -webkit-backdrop-filter:blur(22px) saturate(160%);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.09),0 14px 34px rgba(0,0,0,.32);
    padding:22px;
    animation:frameIn .28s var(--ease);
  }
  .frame[hidden]{display:none}
  @keyframes frameIn{
    from{opacity:0;transform:translateY(6px) scale(.985)}
    to{opacity:1;transform:none}
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
  textarea.field{min-height:132px;resize:vertical;line-height:1.55;font-family:inherit}

  /* ---------- зона фото ---------- */
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
    animation:frameIn .2s ease;
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

  .row{
    display:flex;gap:10px;margin-top:20px;justify-content:center;flex-wrap:wrap;
  }
  .row .btn{flex:0 1 auto;min-width:130px;padding:0 22px}

  /* ---------- OTP ---------- */
  .otp{
    display:flex;gap:8px;justify-content:center;align-items:center;
    margin:2px 0;
  }
  .otp-cell{
    width:clamp(38px,11vw,50px);height:clamp(50px,13vw,60px);
    padding:0;text-align:center;
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    font-size:clamp(18px,5vw,22px);font-weight:600;
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

  @keyframes shake{
    0%,100%{transform:translateX(0)}
    20%{transform:translateX(-8px)}
    40%{transform:translateX(8px)}
    60%{transform:translateX(-5px)}
    80%{transform:translateX(5px)}
  }
  .otp.shake{animation:shake .34s ease}
  .otp.shake .otp-cell{
    border-color:rgba(200,90,90,.65);
    background:rgba(90,20,20,.15);
  }

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

  /* ---------- результат ---------- */
  .post-title{
    margin:0 0 10px;font-size:19px;font-weight:600;line-height:1.3;color:var(--text);
    word-break:break-word;
  }
  .post-meta{
    font-size:12px;color:var(--text-dim);margin-bottom:14px;
    display:flex;gap:10px;flex-wrap:wrap;align-items:center;
  }
  .post-body{
    font-size:14px;line-height:1.6;color:#d8d8de;
    white-space:pre-wrap;word-break:break-word;
  }
  .post-gallery{
    display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));
    gap:8px;margin-top:16px;
  }
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
  .big-code{
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    font-size:38px;font-weight:700;
    letter-spacing:12px;text-indent:12px;
    text-align:center;color:var(--text);margin:10px 0 4px;
  }
  .hint{font-size:12px;color:var(--text-dim);text-align:center;margin-top:4px}

  /* ---------- лайтбокс ---------- */
  .lightbox{
    position:fixed;inset:0;z-index:1000;
    display:flex;align-items:center;justify-content:center;
    background:rgba(6,6,10,.86);
    backdrop-filter:blur(16px) saturate(140%);
    -webkit-backdrop-filter:blur(16px) saturate(140%);
    animation:lbIn .2s ease;padding:60px 70px;
  }
  .lightbox[hidden]{display:none}
  @keyframes lbIn{from{opacity:0}to{opacity:1}}

  .lb-img{
    max-width:100%;max-height:100%;
    object-fit:contain;border-radius:14px;
    box-shadow:0 20px 60px rgba(0,0,0,.5);
    /* шахматка для прозрачных изображений */
    background-color:#1c1c22;
    background-image:
      linear-gradient(45deg, rgba(255,255,255,.06) 25%, transparent 25%),
      linear-gradient(-45deg, rgba(255,255,255,.06) 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, rgba(255,255,255,.06) 75%),
      linear-gradient(-45deg, transparent 75%, rgba(255,255,255,.06) 75%);
    background-size:18px 18px;
    background-position:0 0, 0 9px, 9px -9px, -9px 0px;
    animation:lbImgIn .25s var(--ease);
  }
  @keyframes lbImgIn{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:none}}

  .lb-btn{
    position:absolute;width:44px;height:44px;border-radius:50%;
    border:1px solid var(--glass-border);
    background:rgba(255,255,255,.08);
    color:var(--text);
    display:flex;align-items:center;justify-content:center;
    cursor:pointer;
    backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
    transition:background .15s ease,transform .1s ease;
  }
  .lb-btn svg{width:20px;height:20px}
  .lb-btn:hover{background:rgba(255,255,255,.16)}
  .lb-btn:active{transform:scale(.94)}
  .lb-btn[hidden]{display:none}

  .lb-close{top:18px;right:18px}
  .lb-prev{left:18px;top:50%;transform:translateY(-50%)}
  .lb-prev:active{transform:translateY(-50%) scale(.94)}
  .lb-next{right:18px;top:50%;transform:translateY(-50%)}
  .lb-next:active{transform:translateY(-50%) scale(.94)}

  .lb-counter{
    position:absolute;bottom:20px;left:50%;transform:translateX(-50%);
    padding:6px 14px;border-radius:100px;
    background:rgba(255,255,255,.10);
    border:1px solid var(--glass-border);
    font-size:13px;color:var(--text);
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    letter-spacing:1px;
    backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  }

  /* ---------- модальное окно (код созданного поста) ---------- */
  .modal{
    position:fixed;inset:0;z-index:900;
    display:flex;align-items:center;justify-content:center;
    background:rgba(6,6,10,.55);
    backdrop-filter:blur(10px) saturate(140%);
    -webkit-backdrop-filter:blur(10px) saturate(140%);
    animation:lbIn .2s ease;padding:20px;
  }
  .modal[hidden]{display:none}

  .modal-card{
    width:100%;max-width:380px;
    padding:26px 24px;
    border:1px solid var(--glass-border);
    border-radius:22px;
    background:rgba(28,28,36,.92);
    backdrop-filter:blur(22px) saturate(160%);
    -webkit-backdrop-filter:blur(22px) saturate(160%);
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,.09),
      0 20px 50px rgba(0,0,0,.5);
    text-align:center;
    animation:panelIn .3s var(--ease);
  }
  @keyframes panelIn{
    from{opacity:0;transform:translateY(10px) scale(.96)}
    to{opacity:1;transform:none}
  }

  .modal-icon{
    width:52px;height:52px;margin:0 auto 14px;
    border-radius:50%;
    display:flex;align-items:center;justify-content:center;
    background:rgba(120,200,150,.14);
    color:var(--ok);
    border:1px solid rgba(120,200,150,.28);
  }
  .modal-icon svg{width:26px;height:26px}

  .modal-title{font-size:17px;font-weight:600;margin:0 0 6px}
  .modal-sub{font-size:13px;color:var(--text-dim);margin:0 0 18px;line-height:1.5}

  .modal-code{
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    font-size:40px;font-weight:700;
    letter-spacing:12px;text-indent:12px;
    color:var(--text);
    margin:6px 0 6px;
    user-select:text;-webkit-user-select:text;
  }

  .modal-hint{font-size:12px;color:var(--text-dim);margin-bottom:22px}

  .modal-actions{display:flex;gap:10px}
  .modal-actions .btn{flex:1;height:46px;padding:0 14px;font-size:14px}

  /* ---------- адаптив ---------- */
  @media (max-width:560px){
    .app{padding:44px 14px 40px;gap:18px}
    .menu{gap:10px}
    .btn{padding:0 14px;font-size:14px}
    .btn svg{width:16px;height:16px}
    .frame{padding:18px;border-radius:20px}
    .big-code{font-size:30px;letter-spacing:9px;text-indent:9px}
    .row .btn{min-width:0;flex:1}
    .lightbox{padding:54px 12px 74px}
    .lb-prev{left:8px}
    .lb-next{right:8px}
    .lb-close{top:10px;right:10px}
    .modal-code{font-size:34px;letter-spacing:9px;text-indent:9px}
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
      <span class="btn-label">Создать пост</span>
    </button>
    <button class="btn" id="btnFind" type="button">
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
      <span class="btn-label">Найти пост</span>
    </button>
  </nav>

  <div class="stage" id="stage" hidden>
    <div class="track" id="track">

      <!-- ================= СОЗДАНИЕ ================= -->
      <div class="panel" id="createPanel">
        <section class="frame">

          <div class="input-wrap">
            <input class="field" id="title" type="text" maxlength="120" placeholder="Название" autocomplete="off" spellcheck="false">
            <svg class="iw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4 7V5a1 1 0 011-1h14a1 1 0 011 1v2"/><path d="M9 20h6"/><path d="M12 4v16"/>
            </svg>
          </div>

          <div class="input-wrap textarea-wrap">
            <textarea class="field" id="content" placeholder="Содержимое"></textarea>
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
            <div class="drop-label" id="dropLabel">Нажмите или перетащите фото</div>
            <div class="drop-hint">до 5 фото · Ctrl+V — вставить из буфера</div>
          </div>
          <input type="file" id="fileInput" accept="image/*" multiple hidden>
          <div class="previews" id="previews"></div>

          <div class="row">
            <button class="btn primary" id="submitBtn" type="button">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px">
                <path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/>
              </svg>
              <span>Опубликовать</span>
            </button>
            <button class="btn" id="resetBtn" type="button">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px">
                <path d="M3 6h18"/><path d="M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/>
              </svg>
              <span>Очистить</span>
            </button>
          </div>

          <div id="createMsg"></div>
        </section>
      </div>

      <!-- ================= ПОИСК ================= -->
      <div class="panel" id="findPanel">
        <section class="frame">
          <div class="otp" id="otp" autocomplete="off">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 1">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 2">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 3">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 4">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 5">
            <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 6">
          </div>
        </section>

        <section class="frame" id="searchFrame" hidden></section>
      </div>

    </div>
  </div>
</main>

<!-- ================= МОДАЛКА (код созданного поста) ================= -->
<div class="modal" id="createdModal" hidden>
  <div class="modal-card" id="modalCard">
    <div class="modal-icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M20 6L9 17l-5-5"/>
      </svg>
    </div>
    <h3 class="modal-title">Пост создан</h3>
    <p class="modal-sub">Сохраните код — по нему можно найти пост в любое время</p>
    <div class="modal-code" id="modalCode">000000</div>
    <div class="modal-hint" id="modalHint"></div>
    <div class="modal-actions">
      <button class="btn" id="modalCopyBtn" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px">
          <rect x="9" y="9" width="13" height="13" rx="2"/>
          <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
        </svg>
        <span>Копировать</span>
      </button>
      <button class="btn primary" id="modalCloseBtn" type="button">Готово</button>
    </div>
  </div>
</div>

<!-- ================= ЛАЙТБОКС ================= -->
<div class="lightbox" id="lightbox" hidden>
  <button class="lb-btn lb-close" id="lbClose" type="button" aria-label="Закрыть">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
    </svg>
  </button>
  <button class="lb-btn lb-prev" id="lbPrev" type="button" aria-label="Предыдущее">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="15 18 9 12 15 6"/>
    </svg>
  </button>
  <img class="lb-img" id="lbImg" alt="">
  <button class="lb-btn lb-next" id="lbNext" type="button" aria-label="Следующее">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="9 18 15 12 9 6"/>
    </svg>
  </button>
  <div class="lb-counter" id="lbCounter">1 / 1</div>
</div>

<script>
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);

  /* ---------- глобально отключаем ПКМ ---------- */
  document.addEventListener("contextmenu", (e) => e.preventDefault());

  /* ---------- иконки ---------- */
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

  /* ---------- форматирование размера ---------- */
  function formatBytes(b) {
    if (b < 1024) return b + " Б";
    if (b < 1024 * 1024) return (b / 1024).toFixed(1).replace(".", ",") + " КБ";
    if (b < 1024 * 1024 * 1024) return (b / (1024 * 1024)).toFixed(2).replace(".", ",") + " МБ";
    return (b / (1024 * 1024 * 1024)).toFixed(2).replace(".", ",") + " ГБ";
  }

  /* =========================================================
     ПЕРЕКЛЮЧЕНИЕ ПАНЕЛЕЙ (сдвиг ленты + плавная высота)
     ========================================================= */
  const stage       = $("stage");
  const track       = $("track");
  const createPanel = $("createPanel");
  const findPanel   = $("findPanel");
  const btnCreate   = $("btnCreate");
  const btnFind     = $("btnFind");

  let mode = null;  // 'create' | 'find' | null

  function activePanel() {
    return mode === "create" ? createPanel : findPanel;
  }

  function syncHeight() {
    if (mode === null || stage.hidden) return;
    stage.style.height = activePanel().offsetHeight + "px";
  }

  // Любое изменение размера панели (появление контента) → синхронизация высоты сцены
  const ro = new ResizeObserver(() => syncHeight());
  ro.observe(createPanel);
  ro.observe(findPanel);
  window.addEventListener("resize", syncHeight);

  function setMode(next) {
    const target = (next === mode) ? null : next;
    const wasHidden = stage.hidden;

    if (target === null) {
      stage.hidden = true;
      mode = null;
      btnCreate.classList.remove("active");
      btnFind.classList.remove("active");
      return;
    }

    mode = target;
    btnCreate.classList.toggle("active", target === "create");
    btnFind.classList.toggle("active", target === "find");

    if (wasHidden) {
      // первое появление — без анимации сдвига
      track.style.transition = "none";
      stage.style.transition = "none";
      stage.hidden = false;
      track.style.transform = (target === "create") ? "translateX(0)" : "translateX(-100%)";
      stage.style.height = activePanel().offsetHeight + "px";
      void track.offsetWidth;
      track.style.transition = "";
      stage.style.transition = "";
    } else {
      // обычное переключение — с анимацией
      track.style.transform = (target === "create") ? "translateX(0)" : "translateX(-100%)";
      syncHeight();
    }

    if (target === "create") setTimeout(() => $("title").focus(), 100);
    else setTimeout(() => otpCells[0].focus(), 100);
  }

  btnCreate.addEventListener("click", () => setMode("create"));
  btnFind.addEventListener("click", () => setMode("find"));

  /* =========================================================
     СОЗДАНИЕ ПОСТА
     ========================================================= */
  const MAX_PHOTOS = 5;
  let selectedFiles = [];

  const drop       = $("drop");
  const dropLabel  = $("dropLabel");
  const fileInput  = $("fileInput");
  const previews   = $("previews");
  const createMsg  = $("createMsg");
  const submitBtn  = $("submitBtn");

  function defaultDropLabel() {
    return selectedFiles.length
      ? "Выбрано: " + selectedFiles.length + " / " + MAX_PHOTOS
      : "Нажмите или перетащите фото";
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

  // Ctrl+V — вставка изображения из буфера
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
      showCreateMsg("err", rejected + " файл(ов) пропущено: только изображения и не больше " + MAX_PHOTOS + ".");
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
      rm.type = "button"; rm.textContent = "×"; rm.title = "Убрать";
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
    $("title").value = "";
    $("content").value = "";
    selectedFiles = [];
    renderPreviews();
    clearCreateMsg();
    $("title").focus();
  });

  submitBtn.addEventListener("click", async () => {
    const title = $("title").value.trim();
    const content = $("content").value.trim();
    if (!title) {
      showCreateMsg("err", "Введите название поста.");
      $("title").focus();
      return;
    }

    const fd = new FormData();
    fd.append("title", title);
    fd.append("content", content);
    selectedFiles.forEach((f) => fd.append("files", f, f.name));

    submitBtn.disabled = true;
    const oldHTML = submitBtn.innerHTML;
    submitBtn.textContent = "Публикация...";
    clearCreateMsg();

    try {
      const res = await fetch("/api/posts", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showCreateMsg("err", data.detail || "Не удалось создать пост.");
        return;
      }

      // очищаем форму
      $("title").value = "";
      $("content").value = "";
      selectedFiles = [];
      renderPreviews();
      clearCreateMsg();

      // авто-переход в режим поиска + подстановка кода + поиск
      const code = data.code;
      setMode("find");
      otpCells.forEach((c, i) => { c.value = code[i] || ""; });
      lastSubmitted = code;
      runSearch(code);

      // модалка с кодом и кнопкой "копировать"
      showCreatedModal(code, data.compressed_bytes);
    } catch (e) {
      showCreateMsg("err", "Ошибка сети: " + e.message);
    } finally {
      submitBtn.disabled = false;
      submitBtn.innerHTML = oldHTML;
    }
  });

  /* =========================================================
     МОДАЛЬНОЕ ОКНО (код созданного поста)
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
    modalHint.textContent = "Занято в памяти: " + formatBytes(bytes);
    const label = modalCopyBtn.querySelector("span");
    if (label) label.textContent = "Копировать";
    createdModal.hidden = false;
  }

  function closeCreatedModal() {
    createdModal.hidden = true;
  }

  modalCopyBtn.addEventListener("click", async () => {
    const label = modalCopyBtn.querySelector("span");
    try {
      await navigator.clipboard.writeText(modalCode.textContent || "");
      if (label) label.textContent = "Скопировано";
      clearTimeout(modalCopyTimer);
      modalCopyTimer = setTimeout(() => { if (label) label.textContent = "Копировать"; }, 1500);
    } catch {
      if (label) label.textContent = "Ошибка";
      clearTimeout(modalCopyTimer);
      modalCopyTimer = setTimeout(() => { if (label) label.textContent = "Копировать"; }, 1500);
    }
  });

  modalCloseBtn.addEventListener("click", closeCreatedModal);

  createdModal.addEventListener("click", (e) => {
    if (e.target === createdModal) closeCreatedModal();
  });
  modalCard.addEventListener("click", (e) => e.stopPropagation());

  /* =========================================================
     ПОИСК ПО КОДУ (OTP)
     ========================================================= */
  const otp         = $("otp");
  const otpCells    = Array.from(document.querySelectorAll(".otp-cell"));
  const searchFrame = $("searchFrame");

  let searchSeq = 0;
  let lastSubmitted = "";

  function getCode() { return otpCells.map(c => c.value).join(""); }

  function clearOtp() {
    otpCells.forEach(c => c.value = "");
    otpCells[0].focus();
    lastSubmitted = "";
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
  }

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

  function showSearchFrame() {
    searchFrame.hidden = false;
    searchFrame.style.animation = "none";
    void searchFrame.offsetWidth;
    searchFrame.style.animation = "";
    // синхронизируем высоту сцены после вставки контента
    requestAnimationFrame(syncHeight);
  }

  function renderSpinner() {
    searchFrame.innerHTML =
      '<div class="center"><div class="spinner"></div>' +
      '<div class="spinner-label">Ищем пост...</div></div>';
    showSearchFrame();
  }

  function renderError(text) {
    searchFrame.innerHTML = "";
    searchFrame.appendChild(makeMsg("err", text));
    showSearchFrame();
  }

  /* ---------- лайтбокс ---------- */
  const lightbox  = $("lightbox");
  const lbImg     = $("lbImg");
  const lbCounter = $("lbCounter");
  const lbPrev    = $("lbPrev");
  const lbNext    = $("lbNext");
  const lbClose   = $("lbClose");

  let lbPhotos = [];
  let lbIndex = 0;

  function openLightbox(photos, index) {
    lbPhotos = photos;
    lbIndex = index;
    lbImg.src = "data:" + photos[index].mime + ";base64," + photos[index].data;
    lbCounter.textContent = (index + 1) + " / " + photos.length;
    lbPrev.hidden = photos.length < 2;
    lbNext.hidden = photos.length < 2;
    lightbox.hidden = false;
  }

  function closeLightbox() {
    lightbox.hidden = true;
    lbImg.src = "";
    lbPhotos = [];
  }

  function lbStep(dir) {
    if (lbPhotos.length < 2) return;
    lbIndex = (lbIndex + dir + lbPhotos.length) % lbPhotos.length;
    lbImg.style.animation = "none";
    void lbImg.offsetWidth;
    lbImg.style.animation = "";
    lbImg.src = "data:" + lbPhotos[lbIndex].mime + ";base64," + lbPhotos[lbIndex].data;
    lbCounter.textContent = (lbIndex + 1) + " / " + lbPhotos.length;
  }

  lbPrev.addEventListener("click", (e) => { e.stopPropagation(); lbStep(-1); });
  lbNext.addEventListener("click", (e) => { e.stopPropagation(); lbStep(1); });
  lbClose.addEventListener("click", (e) => { e.stopPropagation(); closeLightbox(); });
  lightbox.addEventListener("click", (e) => { if (e.target === lightbox) closeLightbox(); });

  /* ---------- отрисовка поста ---------- */
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
    try { date.textContent = new Date(post.created).toLocaleString("ru-RU"); } catch {}
    meta.append(chip, date);

    if (post.photos && post.photos.length) {
      const cnt = document.createElement("span");
      cnt.textContent = "Фото: " + post.photos.length;
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
        img.src = "data:" + p.mime + ";base64," + p.data;
        img.alt = p.name || "photo";
        img.title = p.name || "";
        img.addEventListener("click", () => openLightbox(post.photos, idx));
        gallery.appendChild(img);
      });
      searchFrame.appendChild(gallery);
    }

    showSearchFrame();
  }

  async function runSearch(code) {
    const mySeq = ++searchSeq;
    renderSpinner();
    await new Promise((r) => setTimeout(r, 480));
    if (mySeq !== searchSeq) return;

    try {
      const res = await fetch("/api/posts/" + encodeURIComponent(code));
      if (mySeq !== searchSeq) return;
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        renderError(data.detail || "Пост не найден.");
        shakeOtp();
        setTimeout(clearOtp, 320);
        return;
      }
      const post = await res.json();
      if (mySeq !== searchSeq) return;
      renderPost(post);
    } catch (e) {
      if (mySeq !== searchSeq) return;
      renderError("Ошибка сети: " + e.message);
      shakeOtp();
      setTimeout(clearOtp, 320);
    }
  }

  /* ---------- общий Esc ---------- */
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!lightbox.hidden) { closeLightbox(); return; }
    if (!createdModal.hidden) { closeCreatedModal(); return; }
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
