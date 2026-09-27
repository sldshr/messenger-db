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

  /* прячем скроллбары, но скролл оставляем */
  ::-webkit-scrollbar{width:0;height:0;display:none}
  *{scrollbar-width:none;-ms-overflow-style:none}

  :root{
    --glass-bg:rgba(255,255,255,.06);
    --glass-bg-hi:rgba(255,255,255,.10);
    --glass-border:rgba(255,255,255,.10);
    --glass-border-hi:rgba(255,255,255,.18);
    --glass-highlight:rgba(255,255,255,.14);
    --text:#EDEDF2;
    --text-dim:#9A9AA6;
    --text-mute:#61616E;
    --danger:#F0A0A0;
    --ok:#9BDCB0;
  }

  html,body{
    margin:0;padding:0;
    background:#0b0b10;
    color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,"Helvetica Neue",Arial,sans-serif;
    -webkit-font-smoothing:antialiased;
    -moz-osx-font-smoothing:grayscale;
    font-size:14px;
    line-height:1.5;
    min-height:100%;
    overflow-x:hidden;
  }

  /* ---------- фон (статичный, дешёвый) ---------- */
  body::before{
    content:"";
    position:fixed;inset:0;z-index:-1;pointer-events:none;
    background:
      radial-gradient(ellipse 70% 55% at 15% 5%, rgba(124,94,214,.35), transparent 65%),
      radial-gradient(ellipse 65% 55% at 90% 95%, rgba(56,140,190,.30), transparent 65%),
      radial-gradient(ellipse 90% 60% at 50% 50%, rgba(20,20,30,.6), transparent 80%),
      #0b0b10;
  }

  /* ---------- каркас ---------- */
  .app{
    min-height:100dvh;
    display:flex;
    flex-direction:column;
    align-items:center;
    padding:clamp(44px,10vh,110px) 16px 48px;
    gap:24px;
  }

  /* ---------- кнопки ---------- */
  .menu{
    display:flex;
    gap:12px;
    justify-content:center;
    align-items:stretch;
    flex-wrap:nowrap;
    width:100%;
    max-width:520px;
  }

  .btn{
    flex:1 1 0;
    min-width:0;
    height:52px;                 /* одинаковая высота у обеих */
    display:inline-flex;
    align-items:center;
    justify-content:center;
    gap:9px;
    padding:0 20px;

    border:1px solid var(--glass-border);
    border-radius:100px;
    background:var(--glass-bg);
    color:var(--text);
    font:inherit;
    font-size:14.5px;
    font-weight:500;
    letter-spacing:.15px;
    cursor:pointer;
    user-select:none;
    -webkit-tap-highlight-color:transparent;

    backdrop-filter:blur(18px) saturate(150%);
    -webkit-backdrop-filter:blur(18px) saturate(150%);

    box-shadow:
      inset 0 1px 0 rgba(255,255,255,.10),
      0 6px 20px rgba(0,0,0,.28);

    transition:background .18s ease,border-color .18s ease,color .18s ease,transform .08s ease;
  }
  .btn svg{width:18px;height:18px;flex-shrink:0;display:block}
  .btn:hover{background:var(--glass-bg-hi);border-color:var(--glass-border-hi)}
  .btn:active{transform:scale(.98)}
  .btn.active{
    background:rgba(255,255,255,.92);
    color:#0b0b10;
    border-color:rgba(255,255,255,.95);
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,.6),
      0 6px 22px rgba(255,255,255,.10);
  }

  /* ---------- панели ---------- */
  .panel{
    width:100%;
    max-width:520px;
    padding:22px;

    border:1px solid var(--glass-border);
    border-radius:24px;
    background:var(--glass-bg);
    backdrop-filter:blur(22px) saturate(160%);
    -webkit-backdrop-filter:blur(22px) saturate(160%);
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,.09),
      0 18px 45px rgba(0,0,0,.35);

    animation:panelIn .22s cubic-bezier(.2,.9,.3,1);
  }
  .panel[hidden]{display:none}
  @keyframes panelIn{
    from{opacity:0;transform:translateY(-8px) scale(.98)}
    to{opacity:1;transform:none}
  }

  /* ---------- поля ---------- */
  .field{
    width:100%;
    background:rgba(255,255,255,.04);
    border:1px solid var(--glass-border);
    border-radius:14px;
    padding:13px 16px;
    color:var(--text);
    font:inherit;
    font-size:14.5px;
    outline:none;
    transition:border-color .15s ease,background .15s ease;
    -webkit-appearance:none;
    appearance:none;
  }
  .field + .field{margin-top:12px}
  .field::placeholder{color:var(--text-mute)}
  .field:focus{
    border-color:var(--glass-border-hi);
    background:rgba(255,255,255,.07);
  }

  textarea.field{
    min-height:132px;
    resize:vertical;
    line-height:1.55;
    font-family:inherit;
  }

  /* ---------- фото ---------- */
  .drop{
    margin-top:12px;
    border:1px dashed rgba(255,255,255,.18);
    border-radius:14px;
    padding:20px 16px;
    text-align:center;
    color:var(--text-dim);
    font-size:13.5px;
    cursor:pointer;
    background:rgba(255,255,255,.02);
    line-height:1.6;
    transition:border-color .15s ease,color .15s ease,background .15s ease;
  }
  .drop:hover{
    border-color:rgba(255,255,255,.32);
    color:var(--text);
    background:rgba(255,255,255,.05);
  }
  .drop.filled{
    border-style:solid;
    border-color:rgba(255,255,255,.25);
    color:var(--text);
  }

  .previews{
    display:grid;
    grid-template-columns:repeat(auto-fill,minmax(76px,1fr));
    gap:8px;
    margin-top:12px;
  }
  .preview{
    position:relative;
    aspect-ratio:1/1;
    border-radius:12px;
    overflow:hidden;
    background:rgba(255,255,255,.04);
    border:1px solid var(--glass-border);
  }
  .preview img{width:100%;height:100%;object-fit:cover;display:block}
  .preview button{
    position:absolute;top:4px;right:4px;
    width:22px;height:22px;
    border-radius:50%;
    border:1px solid rgba(255,255,255,.15);
    background:rgba(10,10,14,.72);
    color:var(--text);
    font-size:13px;line-height:1;
    cursor:pointer;
    display:flex;align-items:center;justify-content:center;
    -webkit-backdrop-filter:blur(8px);
    backdrop-filter:blur(8px);
  }
  .preview button:hover{background:rgba(70,26,26,.9)}

  .row{
    display:flex;
    gap:10px;
    margin-top:20px;
    justify-content:center;
    flex-wrap:wrap;
  }
  .row .btn{flex:0 1 auto;min-width:130px;padding:0 22px}

  /* ---------- OTP ---------- */
  .otp{
    display:flex;
    gap:8px;
    justify-content:center;
    align-items:center;
    margin:2px 0;
  }
  .otp-cell{
    width:clamp(38px,11vw,50px);
    height:clamp(50px,13vw,60px);
    padding:0;
    text-align:center;
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    font-size:clamp(18px,5vw,22px);
    font-weight:600;
    color:var(--text);
    background:rgba(255,255,255,.05);
    border:1.5px solid var(--glass-border);
    border-radius:14px;
    outline:none;
    caret-color:transparent;
    -webkit-appearance:none;
    appearance:none;
    transition:background .15s ease,border-color .15s ease,transform .1s ease,box-shadow .15s ease;
    -webkit-tap-highlight-color:transparent;
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
  #searchState,#createMsg{margin-top:14px}
  .center{text-align:center;padding:16px 0}
  .spinner{
    width:26px;height:26px;border-radius:50%;
    border:2.5px solid rgba(255,255,255,.12);
    border-top-color:rgba(255,255,255,.75);
    animation:spin .7s linear infinite;
    margin:0 auto;
  }
  @keyframes spin{to{transform:rotate(360deg)}}
  .spinner-label{margin-top:10px;font-size:12.5px;color:var(--text-dim);text-align:center}

  .msg{
    border-radius:14px;
    padding:12px 16px;
    font-size:13.5px;
    background:rgba(255,255,255,.05);
    color:var(--text);
    line-height:1.5;
    border:1px solid var(--glass-border);
  }
  .msg.err{background:rgba(120,40,40,.18);border-color:rgba(200,90,90,.28);color:var(--danger)}
  .msg.ok{background:rgba(30,80,50,.18);border-color:rgba(120,200,150,.25);color:var(--ok)}

  .result{
    margin-top:14px;
    padding:20px;
    border-radius:18px;
    background:rgba(255,255,255,.05);
    border:1px solid var(--glass-border);
    animation:panelIn .2s cubic-bezier(.2,.9,.3,1);
  }
  .result h3{margin:0 0 10px;font-size:18px;font-weight:600;line-height:1.3;color:var(--text)}
  .result .meta{
    font-size:12px;color:var(--text-dim);margin-bottom:14px;
    display:flex;gap:12px;flex-wrap:wrap;align-items:center;
  }
  .result .body{font-size:14px;line-height:1.6;color:#d8d8de;white-space:pre-wrap;word-break:break-word}
  .result .gallery{
    display:grid;
    grid-template-columns:repeat(auto-fill,minmax(110px,1fr));
    gap:8px;margin-top:16px;
  }
  .result .gallery img{
    width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:12px;
    cursor:zoom-in;background:rgba(255,255,255,.04);
    border:1px solid var(--glass-border);
    transition:transform .15s ease;
  }
  .result .gallery img:hover{transform:scale(1.02)}

  .code-chip{
    display:inline-flex;align-items:center;
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    font-size:13px;letter-spacing:3px;color:var(--text);
    background:rgba(255,255,255,.06);
    border:1px solid var(--glass-border);
    border-radius:10px;
    padding:4px 10px;
  }
  .big-code{
    font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    font-size:38px;font-weight:700;
    letter-spacing:12px;text-indent:12px;
    text-align:center;color:var(--text);
    margin:10px 0 4px;
  }
  .hint{font-size:12px;color:var(--text-dim);text-align:center;margin-top:4px}

  /* ---------- адаптив ---------- */
  @media (max-width:560px){
    .app{padding:44px 14px 40px;gap:18px}
    .menu{gap:10px}
    .btn{padding:0 14px;font-size:14px}
    .btn svg{width:16px;height:16px}
    .panel{padding:18px;border-radius:20px}
    .big-code{font-size:30px;letter-spacing:9px;text-indent:9px}
    .row .btn{min-width:0;flex:1}
  }
  @media (max-width:380px){
    .btn span{display:none}
    .btn{padding:0 12px}
  }
</style>
</head>
<body>

<main class="app">

  <nav class="menu">
    <button class="btn" id="btnCreate" type="button">
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
      <span>Создать пост</span>
    </button>
    <button class="btn" id="btnFind" type="button">
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
      <span>Найти пост</span>
    </button>
  </nav>

  <!-- ================= СОЗДАНИЕ ================= -->
  <section class="panel" id="createPanel" hidden>
    <input class="field" id="title" type="text" maxlength="120" placeholder="Название" autocomplete="off">
    <textarea class="field" id="content" placeholder="Содержимое"></textarea>

    <div class="drop" id="drop">Нажмите или перетащите фото (до 5)</div>
    <input type="file" id="fileInput" accept="image/*" multiple hidden>
    <div class="previews" id="previews"></div>

    <div class="row">
      <button class="btn" id="submitBtn" type="button">Опубликовать</button>
      <button class="btn" id="resetBtn" type="button">Очистить</button>
    </div>

    <div id="createMsg"></div>
  </section>

  <!-- ================= ПОИСК ================= -->
  <section class="panel" id="findPanel" hidden>
    <div class="otp" id="otp" autocomplete="off">
      <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 1">
      <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 2">
      <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 3">
      <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 4">
      <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 5">
      <input class="otp-cell" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="цифра 6">
    </div>
    <div id="searchState"></div>
  </section>

</main>

<script>
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);

  const createPanel = $("createPanel");
  const findPanel   = $("findPanel");
  const btnCreate   = $("btnCreate");
  const btnFind     = $("btnFind");

  /* ---------- переключение панелей (tab) ---------- */
  function closeAll() {
    createPanel.hidden = true;
    findPanel.hidden   = true;
    btnCreate.classList.remove("active");
    btnFind.classList.remove("active");
  }

  btnCreate.addEventListener("click", () => {
    const willOpen = createPanel.hidden;
    closeAll();
    if (willOpen) {
      createPanel.hidden = false;
      btnCreate.classList.add("active");
      setTimeout(() => $("title").focus(), 0);
    }
  });

  btnFind.addEventListener("click", () => {
    const willOpen = findPanel.hidden;
    closeAll();
    if (willOpen) {
      findPanel.hidden = false;
      btnFind.classList.add("active");
      setTimeout(() => otpCells[0].focus(), 0);
    }
  });

  /* =========================================================
     СОЗДАНИЕ ПОСТА
     ========================================================= */
  const MAX_PHOTOS = 5;
  let selectedFiles = [];

  const drop      = $("drop");
  const fileInput = $("fileInput");
  const previews  = $("previews");
  const createMsg = $("createMsg");
  const submitBtn = $("submitBtn");

  drop.addEventListener("click", () => fileInput.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.style.borderColor = "rgba(255,255,255,.4)"; });
  drop.addEventListener("dragleave", () => { drop.style.borderColor = ""; });
  drop.addEventListener("drop", (e) => {
    e.preventDefault(); drop.style.borderColor = "";
    addFiles(Array.from(e.dataTransfer.files || []));
  });
  fileInput.addEventListener("change", () => {
    addFiles(Array.from(fileInput.files || []));
    fileInput.value = "";
  });

  function addFiles(list) {
    let rejected = 0;
    for (const f of list) {
      if (!f.type.startsWith("image/")) { rejected++; continue; }
      if (selectedFiles.length >= MAX_PHOTOS) { rejected++; continue; }
      selectedFiles.push(f);
    }
    if (rejected > 0) showCreateMsg("err", rejected + " файл(ов) пропущено: только изображения и не больше " + MAX_PHOTOS + ".");
    else clearCreateMsg();
    renderPreviews();
  }

  function renderPreviews() {
    previews.innerHTML = "";
    selectedFiles.forEach((file, index) => {
      const url = URL.createObjectURL(file);
      const box = document.createElement("div"); box.className = "preview";
      const img = document.createElement("img");
      img.src = url; img.alt = file.name;
      img.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
      const rm = document.createElement("button");
      rm.type = "button"; rm.textContent = "×"; rm.title = "Убрать";
      rm.addEventListener("click", () => { selectedFiles.splice(index, 1); renderPreviews(); });
      box.append(img, rm); previews.appendChild(box);
    });
    drop.classList.toggle("filled", selectedFiles.length > 0);
    drop.textContent = selectedFiles.length
      ? "Выбрано: " + selectedFiles.length + " / " + MAX_PHOTOS
      : "Нажмите или перетащите фото (до 5)";
  }

  function showCreateMsg(kind, text) {
    createMsg.innerHTML = "";
    const d = document.createElement("div");
    d.className = "msg " + kind; d.textContent = text;
    createMsg.appendChild(d);
  }
  function clearCreateMsg() { createMsg.innerHTML = ""; }

  $("resetBtn").addEventListener("click", () => {
    $("title").value = ""; $("content").value = "";
    selectedFiles = []; renderPreviews(); clearCreateMsg();
    $("title").focus();
  });

  submitBtn.addEventListener("click", async () => {
    const title = $("title").value.trim();
    const content = $("content").value.trim();
    if (!title) { showCreateMsg("err", "Введите название поста."); $("title").focus(); return; }

    const fd = new FormData();
    fd.append("title", title);
    fd.append("content", content);
    selectedFiles.forEach((f) => fd.append("files", f, f.name));

    submitBtn.disabled = true;
    const oldLabel = submitBtn.textContent;
    submitBtn.textContent = "Публикация...";
    clearCreateMsg();

    try {
      const res = await fetch("/api/posts", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { showCreateMsg("err", data.detail || "Не удалось создать пост."); return; }

      createMsg.innerHTML = "";
      const ok = document.createElement("div");
      ok.className = "msg ok"; ok.textContent = "Пост создан. Сохраните код:";
      createMsg.appendChild(ok);

      const code = document.createElement("div");
      code.className = "big-code"; code.textContent = data.code;
      createMsg.appendChild(code);

      const hint = document.createElement("div");
      hint.className = "hint"; hint.textContent = "Сжатый размер в памяти: " + data.compressed_bytes + " байт";
      createMsg.appendChild(hint);

      const row = document.createElement("div"); row.className = "row";
      const copyBtn = document.createElement("button");
      copyBtn.className = "btn"; copyBtn.type = "button"; copyBtn.textContent = "Скопировать";
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(data.code);
          copyBtn.textContent = "Готово";
          setTimeout(() => (copyBtn.textContent = "Скопировать"), 1500);
        } catch { copyBtn.textContent = "Ошибка"; }
      });
      const againBtn = document.createElement("button");
      againBtn.className = "btn"; againBtn.type = "button"; againBtn.textContent = "Ещё";
      againBtn.addEventListener("click", () => $("resetBtn").click());
      row.append(copyBtn, againBtn); createMsg.appendChild(row);

      $("title").value = ""; $("content").value = "";
      selectedFiles = []; renderPreviews();
    } catch (e) {
      showCreateMsg("err", "Ошибка сети: " + e.message);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = oldLabel;
    }
  });

  /* =========================================================
     ПОИСК ПО 6-ЗНАЧНОМУ КОДУ (OTP-инпут)
     ========================================================= */
  const otp       = $("otp");
  const otpCells  = Array.from(document.querySelectorAll(".otp-cell"));
  const searchState = $("searchState");
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

  function maybeSearch() {
    const code = getCode();
    if (code.length === 6) {
      if (code === lastSubmitted) return;
      lastSubmitted = code;
      runSearch(code);
    } else {
      searchSeq++;
      searchState.innerHTML = "";
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
        } else if (cell.value) {
          // обычное удаление — обработчик input сделает всё
        }
        setTimeout(maybeSearch, 0);
      } else if (e.key === "ArrowLeft" && i > 0) {
        otpCells[i - 1].focus();
        e.preventDefault();
      } else if (e.key === "ArrowRight" && i < otpCells.length - 1) {
        otpCells[i + 1].focus();
        e.preventDefault();
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

  function renderSpinner() {
    searchState.innerHTML =
      '<div class="center"><div class="spinner"></div>' +
      '<div class="spinner-label">Ищем пост...</div></div>';
  }

  function renderError(text) {
    searchState.innerHTML = "";
    const d = document.createElement("div");
    d.className = "msg err"; d.textContent = text;
    searchState.appendChild(d);
  }

  function renderPost(post) {
    searchState.innerHTML = "";
    const box = document.createElement("div"); box.className = "result";

    const h = document.createElement("h3"); h.textContent = post.title;
    const meta = document.createElement("div"); meta.className = "meta";

    const chip = document.createElement("span");
    chip.className = "code-chip"; chip.textContent = post.code;
    const date = document.createElement("span");
    try { date.textContent = new Date(post.created).toLocaleString("ru-RU"); } catch {}
    meta.append(chip, date);

    if (post.photos && post.photos.length) {
      const cnt = document.createElement("span");
      cnt.textContent = "Фото: " + post.photos.length;
      meta.appendChild(cnt);
    }
    box.append(h, meta);

    if (post.content) {
      const body = document.createElement("div");
      body.className = "body"; body.textContent = post.content;
      box.appendChild(body);
    }

    if (post.photos && post.photos.length) {
      const g = document.createElement("div"); g.className = "gallery";
      post.photos.forEach((p) => {
        const img = document.createElement("img");
        img.src = "data:" + p.mime + ";base64," + p.data;
        img.alt = p.name || "photo"; img.title = p.name || "";
        img.addEventListener("click", () => window.open(img.src, "_blank"));
        g.appendChild(img);
      });
      box.appendChild(g);
    }
    searchState.appendChild(box);
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
        setTimeout(clearOtp, 300);
        return;
      }
      const post = await res.json();
      if (mySeq !== searchSeq) return;
      renderPost(post);
    } catch (e) {
      if (mySeq !== searchSeq) return;
      renderError("Ошибка сети: " + e.message);
      shakeOtp();
      setTimeout(clearOtp, 300);
    }
  }
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
