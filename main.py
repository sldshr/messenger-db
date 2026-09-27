"""
PostVault — посты с 6-значным кодом.
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
# Ключ шифрования генерируется при старте приложения.
# После перезапуска все ранее созданные посты прочитать невозможно.
_fernet = Fernet(Fernet.generate_key())

# code -> зашифрованный+сжатый блоб
_store: Dict[str, bytes] = {}
_lock = threading.Lock()

MAX_PHOTOS = 5
MAX_PHOTO_BYTES = 8 * 1024 * 1024      # 8 МБ на одно фото
MAX_TITLE_LEN = 120
MAX_CONTENT_LEN = 20_000


def _pack(payload: dict) -> bytes:
    """dict -> JSON -> gzip -> Fernet(encrypt)."""
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
app = FastAPI(title="PostVault", docs_url=None, redoc_url=None)


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
        "size_raw": 0,
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
<title>PostVault</title>
<style>
  *{box-sizing:border-box}
  :root{
    --bg:#0a0a0c;
    --panel:#141417;
    --panel-2:#1b1b20;
    --field:#0f0f12;
    --border:#2a2a31;
    --border-hi:#45454f;
    --text:#e9e9ec;
    --muted:#85858f;
    --accent:#31313a;
  }
  html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
    -webkit-font-smoothing:antialiased}
  body{min-height:100vh;background:
    radial-gradient(900px 480px at 50% -12%, #191920 0%, rgba(10,10,12,0) 70%)}

  header{max-width:840px;margin:0 auto;padding:34px 20px 10px;
    display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between}
  .brand{font-size:19px;font-weight:600;letter-spacing:.4px;display:flex;align-items:center;gap:10px}
  .brand span.dot{width:9px;height:9px;border-radius:50%;background:#6f6f7d;
    box-shadow:0 0 12px #6f6f7d}
  .actions{display:flex;gap:10px;flex-wrap:wrap}

  .btn{
    border:1px solid var(--border);
    background:var(--panel-2);
    color:var(--text);
    padding:11px 18px;
    border-radius:14px;
    font-size:14px;
    font-family:inherit;
    cursor:pointer;
    transition:background .15s,border-color .15s,transform .08s;
    white-space:nowrap;
  }
  .btn:hover{background:var(--accent);border-color:var(--border-hi)}
  .btn:active{transform:scale(.98)}
  .btn.active{background:#33333d;border-color:#55555f}
  .btn.primary{background:#2c2c35}
  .btn.primary:hover{background:#3a3a45}
  .btn:disabled{opacity:.5;cursor:not-allowed}

  main{max-width:840px;margin:0 auto;padding:0 20px 80px}

  .panel{
    background:linear-gradient(180deg,#16161a 0%,#131316 100%);
    border:1px solid var(--border);
    border-radius:22px;
    padding:24px;
    margin-top:16px;
    animation:pop .2s ease;
  }
  @keyframes pop{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
  .panel.hidden{display:none}
  .panel h2{margin:0 0 4px;font-size:16px;font-weight:600;letter-spacing:.2px}
  .panel .sub{margin:0 0 8px;font-size:13px;color:var(--muted)}

  label{display:block;font-size:12.5px;color:var(--muted);margin:16px 0 7px;
    text-transform:uppercase;letter-spacing:.7px}

  input[type=text],textarea{
    width:100%;background:var(--field);border:1px solid var(--border);
    border-radius:14px;padding:13px 15px;color:var(--text);font-size:14.5px;
    font-family:inherit;outline:none;transition:border-color .15s,background .15s;
  }
  input[type=text]:focus,textarea:focus{border-color:var(--border-hi);background:#121216}
  input[type=text]::placeholder,textarea::placeholder{color:#5b5b66}
  textarea{min-height:130px;resize:vertical;line-height:1.5}

  .drop{
    border:1px dashed #35353f;border-radius:16px;padding:20px;text-align:center;
    color:var(--muted);font-size:13.5px;cursor:pointer;background:#101013;
    transition:border-color .15s,color .15s,background .15s;line-height:1.6;
  }
  .drop:hover{border-color:var(--border-hi);color:#b6b6c0;background:#131318}
  .drop.filled{border-style:solid;border-color:#3a3a45;color:#a9a9b4}

  .previews{display:grid;grid-template-columns:repeat(auto-fill,minmax(88px,1fr));
    gap:10px;margin-top:12px}
  .preview{position:relative;aspect-ratio:1/1;border-radius:14px;overflow:hidden;
    border:1px solid var(--border);background:#0f0f12;animation:pop .18s ease}
  .preview img{width:100%;height:100%;object-fit:cover;display:block}
  .preview button{
    position:absolute;top:5px;right:5px;width:22px;height:22px;border-radius:50%;
    border:none;background:rgba(10,10,12,.78);color:#e9e9ec;font-size:14px;
    line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;
    backdrop-filter:blur(4px);transition:background .15s;
  }
  .preview button:hover{background:rgba(90,20,20,.9)}

  .row{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}

  .code-input{
    width:100%;text-align:center;font-size:36px;font-weight:600;
    letter-spacing:16px;text-indent:16px;
    background:var(--field);border:1px solid var(--border);border-radius:18px;
    padding:16px 10px;color:var(--text);outline:none;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    transition:border-color .15s,background .15s;
  }
  .code-input:focus{border-color:var(--border-hi);background:#121216}
  .code-input::placeholder{color:#3d3d47;letter-spacing:16px}

  #searchState{margin-top:6px}
  .center{text-align:center;padding:26px 0}

  .spinner{
    width:30px;height:30px;border-radius:50%;
    border:3px solid #2c2c34;border-top-color:#9a9aa8;
    animation:spin .75s linear infinite;margin:0 auto;
  }
  @keyframes spin{to{transform:rotate(360deg)}}
  .spinner-label{margin-top:12px;font-size:13px;color:var(--muted)}

  .msg{border-radius:16px;padding:14px 16px;font-size:14px;margin-top:14px;
    border:1px solid var(--border);background:var(--panel-2)}
  .msg.err{border-color:#5a2b2b;background:#1e1414;color:#f0b4b4}
  .msg.ok{border-color:#2e4a34;background:#131b15;color:#a9e0b8}

  .result{margin-top:18px;border:1px solid var(--border);border-radius:20px;
    background:var(--panel-2);padding:22px;animation:pop .2s ease}
  .result h3{margin:0 0 6px;font-size:20px;font-weight:600;line-height:1.3}
  .result .meta{font-size:12.5px;color:var(--muted);margin-bottom:14px;
    display:flex;gap:14px;flex-wrap:wrap;align-items:center}
  .result .body{font-size:14.5px;line-height:1.65;color:#d3d3da;white-space:pre-wrap;
    word-break:break-word}
  .result .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));
    gap:10px;margin-top:18px}
  .result .gallery img{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:14px;
    border:1px solid var(--border);cursor:zoom-in;transition:transform .15s,border-color .15s}
  .result .gallery img:hover{transform:scale(1.02);border-color:var(--border-hi)}

  .code-chip{
    display:inline-flex;align-items:center;gap:10px;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    font-size:15px;letter-spacing:4px;color:#cfcfd8;
    background:#101013;border:1px solid var(--border);
    border-radius:12px;padding:6px 12px;
  }
  .big-code{
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    font-size:44px;font-weight:700;letter-spacing:14px;text-indent:14px;
    text-align:center;color:#e9e9ec;margin:10px 0 4px;
    text-shadow:0 0 26px rgba(160,160,190,.18);
  }
  .hint{font-size:12.5px;color:var(--muted);text-align:center}

  .footer{max-width:840px;margin:0 auto;padding:0 20px 40px;
    font-size:12px;color:#4e4e58;text-align:center}

  @media (max-width:560px){
    header{padding-top:24px}
    .brand{width:100%}
    .actions{width:100%}
    .actions .btn{flex:1;text-align:center}
    .code-input{font-size:28px;letter-spacing:12px;text-indent:12px}
    .big-code{font-size:34px;letter-spacing:10px;text-indent:10px}
  }
</style>
</head>
<body>

<header>
  <div class="brand"><span class="dot"></span> PostVault</div>
  <div class="actions">
    <button class="btn" id="btnCreate">＋ Создать пост</button>
    <button class="btn" id="btnFind">⌕ Найти пост по коду</button>
  </div>
</header>

<main>
  <!-- ================= СОЗДАНИЕ ================= -->
  <section class="panel hidden" id="createPanel">
    <h2>Новый пост</h2>
    <p class="sub">Заполните поля и при желании прикрепите до 5 фотографий.</p>

    <label for="title">Название</label>
    <input id="title" type="text" maxlength="120" placeholder="Например: Отчёт за неделю" autocomplete="off">

    <label for="content">Содержимое</label>
    <textarea id="content" placeholder="Введите текст поста..."></textarea>

    <label>Фотографии <span style="color:#5b5b66">— до 5 шт.</span></label>
    <div class="drop" id="drop">Нажмите, чтобы выбрать фото<br><span style="color:#5b5b66">JPG · PNG · WEBP · GIF</span></div>
    <input type="file" id="fileInput" accept="image/*" multiple hidden>
    <div class="previews" id="previews"></div>

    <div class="row">
      <button class="btn primary" id="submitBtn">Опубликовать</button>
      <button class="btn" id="resetBtn">Очистить</button>
    </div>

    <div id="createMsg"></div>
  </section>

  <!-- ================= ПОИСК ================= -->
  <section class="panel hidden" id="findPanel">
    <h2>Поиск поста</h2>
    <p class="sub">Введите 6-значный код — поиск начнётся автоматически.</p>

    <input id="codeInput" class="code-input" inputmode="numeric" autocomplete="off"
           maxlength="6" placeholder="––––––">

    <div id="searchState"></div>
  </section>
</main>

<div class="footer">Данные хранятся в оперативной памяти в сжатом и зашифрованном виде.</div>

<script>
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const createPanel = $("createPanel");
  const findPanel   = $("findPanel");
  const btnCreate   = $("btnCreate");
  const btnFind     = $("btnFind");

  /* ---------------- переключение панелей ---------------- */
  function syncButtons() {
    btnCreate.classList.toggle("active", !createPanel.classList.contains("hidden"));
    btnFind.classList.toggle("active", !findPanel.classList.contains("hidden"));
  }

  btnCreate.addEventListener("click", () => {
    createPanel.classList.toggle("hidden");
    syncButtons();
    if (!createPanel.classList.contains("hidden")) $("title").focus();
  });

  btnFind.addEventListener("click", () => {
    findPanel.classList.toggle("hidden");
    syncButtons();
    if (!findPanel.classList.contains("hidden")) $("codeInput").focus();
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
    drop.style.borderColor = "#55555f";
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
      ? "Выбрано фото: " + selectedFiles.length + " / " + MAX_PHOTOS + "  ·  нажмите, чтобы добавить ещё"
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
      ok.innerHTML =
        "Пост создан и зашифрован.<br>Сохраните код — по нему его можно найти:";
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
      row.style.justifyContent = "center";

      const copyBtn = document.createElement("button");
      copyBtn.className = "btn";
      copyBtn.textContent = "Скопировать код";
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(data.code);
          copyBtn.textContent = "Скопировано ✓";
          setTimeout(() => (copyBtn.textContent = "Скопировать код"), 1500);
        } catch { copyBtn.textContent = "Не удалось"; }
      });

      const againBtn = document.createElement("button");
      againBtn.className = "btn";
      againBtn.textContent = "Создать ещё";
      againBtn.addEventListener("click", () => $("resetBtn").click());

      row.append(copyBtn, againBtn);
      createMsg.appendChild(row);

      // очищаем форму, но оставляем результат на экране
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
      searchSeq++;              // отменяем предыдущий поиск
      searchState.innerHTML = "";
    }
  });

  codeInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && codeInput.value.length === 6) {
      runSearch(codeInput.value);
    }
  });

  function renderSpinner() {
    searchState.innerHTML =
      '<div class="center"><div class="spinner"></div>' +
      '<div class="spinner-label">Ищем пост…</div></div>';
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

    const photosCount = document.createElement("span");
    photosCount.textContent = "Фото: " + (post.photos ? post.photos.length : 0);

    meta.append(chip, date, photosCount);

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

    // небольшая задержка, чтобы колесико было видно даже при мгновенном ответе
    await new Promise((r) => setTimeout(r, 550));
    if (mySeq !== searchSeq) return;

    try {
      const res = await fetch("/api/posts/" + encodeURIComponent(code));
      if (mySeq !== searchSeq) return;

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        renderError(data.detail || "Пост не найден.");
        return;
      }

      const post = await res.json();
      if (mySeq !== searchSeq) return;
      renderPost(post);
    } catch (e) {
      if (mySeq !== searchSeq) return;
      renderError("Ошибка сети: " + e.message);
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
