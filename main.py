"""
Sld-Networking — посты с 6-значным кодом.
Хранение: оперативная память, сжатие (gzip) + шифрование (Fernet/AES).

Запуск:
    pip install fastapi uvicorn python-multipart cryptography
    python main.py

Откроется на http://127.0.0.1:8000
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

# --------------------------------------------------------------------------
#  Хранилище (только в оперативке)
# --------------------------------------------------------------------------
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
    raise HTTPException(status_code=503, detail="Хранилище переполнено")


# --------------------------------------------------------------------------
#  Приложение
# --------------------------------------------------------------------------
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
        raise HTTPException(status_code=400, detail="Название не может быть пустым")
    if len(title) > MAX_TITLE_LEN:
        raise HTTPException(status_code=400, detail=f"Название длиннее {MAX_TITLE_LEN} символов")
    if len(content) > MAX_CONTENT_LEN:
        raise HTTPException(status_code=400, detail=f"Содержимое длиннее {MAX_CONTENT_LEN} символов")

    files = [f for f in (files or []) if f and f.filename]
    if len(files) > MAX_PHOTOS:
        raise HTTPException(status_code=400, detail=f"Максимум {MAX_PHOTOS} фото")

    photos = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Файл «{f.filename}» больше {MAX_PHOTO_BYTES // (1024 * 1024)} МБ",
            )
        photos.append(
            {
                "name": f.filename,
                "mime": f.content_type or "image/jpeg",
                "data": base64.b64encode(data).decode("ascii"),
            }
        )

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

    return {
        "code": code,
        "compressed_bytes": len(blob),
        "photos": len(photos),
    }


@app.get("/api/posts/{code}")
async def get_post(code: str):
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=400, detail="Код должен состоять из 6 цифр")

    with _lock:
        blob = _store.get(code)

    if blob is None:
        raise HTTPException(status_code=404, detail="Пост с таким кодом не найден")

    try:
        payload = _unpack(blob)
    except (InvalidToken, OSError, ValueError):
        raise HTTPException(status_code=500, detail="Не удалось расшифровать пост")

    payload["code"] = code
    return payload


@app.get("/api/stats")
async def stats():
    with _lock:
        return {"posts": len(_store)}


# --------------------------------------------------------------------------
#  Фронтенд
# --------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sld-Networking</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%23101012'/%3E%3Crect x='7.5' y='7.5' width='17' height='17' rx='3' fill='none' stroke='%23e8e8ea' stroke-width='1.6'/%3E%3Ccircle cx='16' cy='16' r='2.6' fill='%23e8e8ea'/%3E%3C/svg%3E">
<style>
  *{box-sizing:border-box}
  :root{
    --bg:#101012;
    --panel:#18181b;
    --panel-2:#202024;
    --field:#141416;
    --border:#2a2a2e;
    --border-hi:#3d3d44;
    --text:#e8e8ea;
    --muted:#83838c;
  }

  /* убираем скроллбары везде */
  ::-webkit-scrollbar{width:0;height:0;display:none}
  *{scrollbar-width:none;-ms-overflow-style:none}

  html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;font-size:14px}

  body{
    min-height:100vh;
    display:flex;
    align-items:center;
    justify-content:center;
    padding:32px 20px;
  }

  .wrap{
    width:100%;
    max-width:600px;
    display:flex;
    flex-direction:column;
    align-items:stretch;
    gap:22px;
  }

  .brand{
    text-align:center;
    font-size:12px;
    letter-spacing:3px;
    text-transform:uppercase;
    color:var(--muted);
    user-select:none;
  }

  .actions{
    display:flex;
    gap:10px;
    justify-content:center;
    flex-wrap:wrap;
  }

  .btn{
    display:inline-flex;align-items:center;justify-content:center;gap:8px;
    border:1px solid var(--border);
    background:var(--panel);
    color:var(--text);
    padding:11px 20px;
    border-radius:10px;
    font:inherit;font-size:14px;
    cursor:pointer;
    transition:background .12s,border-color .12s;
    white-space:nowrap;
  }
  .btn svg{width:16px;height:16px;stroke:currentColor;stroke-width:1.8;
    fill:none;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}
  .btn:hover{background:var(--panel-2);border-color:var(--border-hi)}
  .btn:active{background:#26262a}
  .btn.primary{background:var(--panel-2)}
  .btn.primary:hover{background:#2a2a2f}
  .btn:disabled{opacity:.5;cursor:not-allowed}

  .panel{
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:12px;
    padding:20px;
    animation:fade .15s ease;
  }
  @keyframes fade{from{opacity:0}to{opacity:1}}
  .panel[hidden]{display:none}
  .panel h2{margin:0 0 16px;font-size:14px;font-weight:600;letter-spacing:.3px;text-align:center;color:var(--text)}

  label{display:block;font-size:11.5px;color:var(--muted);margin:14px 0 6px;
    text-transform:uppercase;letter-spacing:.8px}

  input[type=text],textarea{
    width:100%;background:var(--field);border:1px solid var(--border);
    border-radius:10px;padding:11px 13px;color:var(--text);font-size:14px;
    font-family:inherit;outline:none;transition:border-color .12s;
  }
  input[type=text]:focus,textarea:focus{border-color:var(--border-hi)}
  input[type=text]::placeholder,textarea::placeholder{color:#4e4e56}
  textarea{min-height:120px;resize:vertical;line-height:1.55}

  .drop{
    border:1px dashed #35353b;border-radius:10px;padding:18px;text-align:center;
    color:var(--muted);font-size:13px;cursor:pointer;background:var(--field);
    transition:border-color .12s,color .12s;line-height:1.6;
  }
  .drop:hover{border-color:var(--border-hi);color:#b6b6bc}
  .drop.filled{border-style:solid;border-color:var(--border-hi);color:#a9a9b0}

  .previews{display:grid;grid-template-columns:repeat(auto-fill,minmax(80px,1fr));
    gap:8px;margin-top:10px}
  .preview{position:relative;aspect-ratio:1/1;border-radius:10px;overflow:hidden;
    border:1px solid var(--border);background:var(--field)}
  .preview img{width:100%;height:100%;object-fit:cover;display:block}
  .preview button{
    position:absolute;top:4px;right:4px;width:20px;height:20px;border-radius:5px;
    border:1px solid var(--border);background:rgba(16,16,18,.85);color:#e8e8ea;
    font-size:13px;line-height:1;cursor:pointer;display:flex;align-items:center;
    justify-content:center;font-family:inherit;
  }
  .preview button:hover{background:#2a1414;border-color:#4a2020}

  .row{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap;justify-content:center}

  .code-input{
    width:100%;text-align:center;font-size:34px;font-weight:600;
    letter-spacing:14px;text-indent:14px;
    background:var(--field);border:1px solid var(--border);border-radius:12px;
    padding:14px 10px;color:var(--text);outline:none;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    transition:border-color .12s;
  }
  .code-input:focus{border-color:var(--border-hi)}
  .code-input::placeholder{color:#33333a;letter-spacing:14px}

  /* анимация неверного кода */
  @keyframes shake{
    0%,100%{transform:translateX(0)}
    20%{transform:translateX(-9px)}
    40%{transform:translateX(9px)}
    60%{transform:translateX(-6px)}
    80%{transform:translateX(6px)}
  }
  .shake{
    animation:shake .34s ease;
    border-color:#6a2a2a !important;
  }

  #searchState{margin-top:4px}
  .center{text-align:center;padding:22px 0}

  .spinner{
    width:26px;height:26px;border-radius:50%;
    border:2.5px solid var(--border);border-top-color:#9a9aa2;
    animation:spin .7s linear infinite;margin:0 auto;
  }
  @keyframes spin{to{transform:rotate(360deg)}}
  .spinner-label{margin-top:10px;font-size:12.5px;color:var(--muted);text-align:center}

  .msg{border-radius:10px;padding:12px 14px;font-size:13.5px;margin-top:12px;
    border:1px solid var(--border);background:var(--panel-2);line-height:1.5}
  .msg.err{border-color:#4a2424;background:#1a1212;color:#e0a8a8}
  .msg.ok{border-color:#24402c;background:#111a14;color:#a3d9b4}

  .result{margin-top:16px;border:1px solid var(--border);border-radius:12px;
    background:var(--panel-2);padding:20px;animation:fade .15s ease}
  .result h3{margin:0 0 8px;font-size:18px;font-weight:600;line-height:1.3}
  .result .meta{font-size:12px;color:var(--muted);margin-bottom:14px;
    display:flex;gap:12px;flex-wrap:wrap;align-items:center}
  .result .body{font-size:14px;line-height:1.6;color:#d0d0d6;white-space:pre-wrap;
    word-break:break-word}
  .result .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));
    gap:8px;margin-top:16px}
  .result .gallery img{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:10px;
    border:1px solid var(--border);cursor:zoom-in}

  .code-chip{
    display:inline-flex;align-items:center;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:13px;letter-spacing:3px;color:#cfcfd6;
    background:var(--field);border:1px solid var(--border);
    border-radius:8px;padding:4px 10px;
  }
  .big-code{
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:38px;font-weight:700;letter-spacing:12px;text-indent:12px;
    text-align:center;color:var(--text);margin:8px 0 2px;
  }
  .hint{font-size:12px;color:var(--muted);text-align:center;margin-top:4px}

  @media (max-width:560px){
    body{padding:20px 14px}
    .actions{flex-direction:column}
    .actions .btn{width:100%}
    .code-input{font-size:26px;letter-spacing:11px;text-indent:11px}
    .big-code{font-size:30px;letter-spacing:9px;text-indent:9px}
  }
</style>
</head>
<body>

<div class="wrap">
  <div class="brand">Sld-Networking</div>

  <div class="actions">
    <button class="btn" id="btnCreate">
      <svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      Создать пост
    </button>
    <button class="btn" id="btnFind">
      <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg>
      Найти пост
    </button>
  </div>

  <!-- ================= СОЗДАНИЕ ================= -->
  <section class="panel" id="createPanel" hidden>
    <h2>Новый пост</h2>

    <label for="title">Название</label>
    <input id="title" type="text" maxlength="120" placeholder="Введите название" autocomplete="off">

    <label for="content">Содержимое</label>
    <textarea id="content" placeholder="Введите текст поста"></textarea>

    <label>Фотографии — до 5 шт.</label>
    <div class="drop" id="drop">Нажмите, чтобы выбрать фото<br><span style="color:#4e4e56">JPG · PNG · WEBP · GIF</span></div>
    <input type="file" id="fileInput" accept="image/*" multiple hidden>
    <div class="previews" id="previews"></div>

    <div class="row">
      <button class="btn primary" id="submitBtn">Опубликовать</button>
      <button class="btn" id="resetBtn">Очистить</button>
    </div>

    <div id="createMsg"></div>
  </section>

  <!-- ================= ПОИСК ================= -->
  <section class="panel" id="findPanel" hidden>
    <h2>Поиск поста</h2>

    <input id="codeInput" class="code-input" inputmode="numeric" autocomplete="off"
           maxlength="6" placeholder="––––––">

    <div id="searchState"></div>
  </section>
</div>

<script>
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const createPanel = $("createPanel");
  const findPanel   = $("findPanel");
  const btnCreate   = $("btnCreate");
  const btnFind     = $("btnFind");

  /* ---------------- переключение панелей ----------------
     Открытие одной панели скрывает другую.
     Данные внутри скрытой панели сохраняются. */
  btnCreate.addEventListener("click", () => {
    const willOpen = createPanel.hidden;
    createPanel.hidden = !willOpen;
    if (willOpen) {
      findPanel.hidden = true;
      setTimeout(() => $("title").focus(), 0);
    }
  });

  btnFind.addEventListener("click", () => {
    const willOpen = findPanel.hidden;
    findPanel.hidden = !willOpen;
    if (willOpen) {
      createPanel.hidden = true;   // прячем создание, не очищая
      setTimeout(() => codeInput.focus(), 0);
    }
  });

  /* =========================================================
     СОЗДАНИЕ ПОСТА
     ========================================================= */
  const MAX_PHOTOS = 5;
  let selectedFiles = [];

  const drop       = $("drop");
  const fileInput  = $("fileInput");
  const previews   = $("previews");
  const createMsg  = $("createMsg");
  const submitBtn  = $("submitBtn");

  drop.addEventListener("click", () => fileInput.click());

  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.style.borderColor = "#3d3d44";
  });
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
      img.src = url;
      img.alt = file.name;
      img.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });

      const rm = document.createElement("button");
      rm.type = "button";
      rm.textContent = "×";
      rm.title = "Убрать";
      rm.addEventListener("click", () => {
        selectedFiles.splice(index, 1);
        renderPreviews();
      });

      box.append(img, rm);
      previews.appendChild(box);
    });

    drop.classList.toggle("filled", selectedFiles.length > 0);
    drop.firstChild.textContent = selectedFiles.length
      ? "Выбрано фото: " + selectedFiles.length + " / " + MAX_PHOTOS + " — нажмите, чтобы добавить ещё"
      : "Нажмите, чтобы выбрать фото";
    drop.firstChild.nodeValue = drop.firstChild.textContent;
  }

  function showCreateMsg(kind, text) {
    createMsg.innerHTML = "";
    const div = document.createElement("div");
    div.className = "msg " + kind;
    div.textContent = text;
    createMsg.appendChild(div);
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
    submitBtn.textContent = "Публикация...";
    clearCreateMsg();

    try {
      const res = await fetch("/api/posts", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));

      if (!res.ok) {
        showCreateMsg("err", data.detail || "Не удалось создать пост.");
        return;
      }

      createMsg.innerHTML = "";
      const ok = document.createElement("div");
      ok.className = "msg ok";
      ok.textContent = "Пост создан и зашифрован. Сохраните код:";
      createMsg.appendChild(ok);

      const code = document.createElement("div");
      code.className = "big-code";
      code.textContent = data.code;
      createMsg.appendChild(code);

      const hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent = "Сжатый размер в памяти: " + data.compressed_bytes + " байт";
      createMsg.appendChild(hint);

      const row = document.createElement("div");
      row.className = "row";

      const copyBtn = document.createElement("button");
      copyBtn.className = "btn";
      copyBtn.textContent = "Скопировать код";
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(data.code);
          copyBtn.textContent = "Скопировано";
          setTimeout(() => (copyBtn.textContent = "Скопировать код"), 1500);
        } catch { copyBtn.textContent = "Не удалось"; }
      });

      const againBtn = document.createElement("button");
      againBtn.className = "btn";
      againBtn.textContent = "Создать ещё";
      againBtn.addEventListener("click", () => $("resetBtn").click());

      row.append(copyBtn, againBtn);
      createMsg.appendChild(row);

      // очищаем только форму ввода, результат оставляем
      $("title").value = "";
      $("content").value = "";
      selectedFiles = [];
      renderPreviews();
    } catch (e) {
      showCreateMsg("err", "Ошибка сети: " + e.message);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Опубликовать";
    }
  });

  /* =========================================================
     ПОИСК ПО КОДУ
     ========================================================= */
  const codeInput   = $("codeInput");
  const searchState = $("searchState");

  let searchSeq = 0;

  codeInput.addEventListener("input", () => {
    const cleaned = codeInput.value.replace(/\D/g, "").slice(0, 6);
    if (cleaned !== codeInput.value) codeInput.value = cleaned;

    if (cleaned.length === 6) {
      runSearch(cleaned);
    } else {
      searchSeq++;
      searchState.innerHTML = "";
    }
  });

  codeInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && codeInput.value.length === 6) {
      runSearch(codeInput.value);
    }
  });

  function triggerShakeAndClear() {
    codeInput.value = "";
    codeInput.classList.remove("shake");
    // рестарт анимации
    void codeInput.offsetWidth;
    codeInput.classList.add("shake");
    setTimeout(() => codeInput.classList.remove("shake"), 400);
  }

  function renderSpinner() {
    searchState.innerHTML =
      '<div class="center"><div class="spinner"></div>' +
      '<div class="spinner-label">Ищем пост...</div></div>';
  }

  function renderError(text) {
    searchState.innerHTML = "";
    const div = document.createElement("div");
    div.className = "msg err";
    div.textContent = text;
    searchState.appendChild(div);
  }

  function renderPost(post) {
    searchState.innerHTML = "";

    const box = document.createElement("div");
    box.className = "result";

    const h = document.createElement("h3");
    h.textContent = post.title;

    const meta = document.createElement("div");
    meta.className = "meta";

    const chip = document.createElement("span");
    chip.className = "code-chip";
    chip.textContent = post.code;

    const date = document.createElement("span");
    try {
      date.textContent = new Date(post.created).toLocaleString("ru-RU");
    } catch { date.textContent = ""; }

    meta.append(chip, date);

    if (post.photos && post.photos.length) {
      const cnt = document.createElement("span");
      cnt.textContent = "Фото: " + post.photos.length;
      meta.appendChild(cnt);
    }

    box.append(h, meta);

    if (post.content) {
      const body = document.createElement("div");
      body.className = "body";
      body.textContent = post.content;
      box.appendChild(body);
    }

    if (post.photos && post.photos.length) {
      const gallery = document.createElement("div");
      gallery.className = "gallery";
      post.photos.forEach((p) => {
        const img = document.createElement("img");
        img.src = "data:" + p.mime + ";base64," + p.data;
        img.alt = p.name || "photo";
        img.title = p.name || "";
        img.addEventListener("click", () => window.open(img.src, "_blank"));
        gallery.appendChild(img);
      });
      box.appendChild(gallery);
    }

    searchState.appendChild(box);
  }

  async function runSearch(code) {
    const mySeq = ++searchSeq;

    renderSpinner();

    // небольшая задержка, чтобы спиннер был заметен
    await new Promise((r) => setTimeout(r, 500));
    if (mySeq !== searchSeq) return;

    try {
      const res = await fetch("/api/posts/" + encodeURIComponent(code));
      if (mySeq !== searchSeq) return;

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        renderError(data.detail || "Пост не найден.");
        triggerShakeAndClear();
        return;
      }

      const post = await res.json();
      if (mySeq !== searchSeq) return;
      renderPost(post);
    } catch (e) {
      if (mySeq !== searchSeq) return;
      renderError("Ошибка сети: " + e.message);
      triggerShakeAndClear();
    }
  }
})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(PAGE)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
