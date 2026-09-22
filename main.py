# sldchat.py
# Запуск:  pip install fastapi uvicorn
#          python sldchat.py
# Открыть:  http://127.0.0.1:8000

import asyncio
import secrets
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ----------------------------- НАСТРОЙКИ -----------------------------
SESSION_COOKIE = "sldchat_sid"
SESSION_TTL    = 60 * 60 * 24 * 30      # 30 дней
ONLINE_WINDOW  = 60                     # «онлайн» = активность за 60 сек
MAX_POST_LEN   = 1000
MAX_COMMENT_LEN = 300
MAX_NAME_LEN   = 24
MAX_POSTS      = 300                    # сколько постов держим в памяти
MAX_COMMENTS   = 200                    # максимум комментариев на пост
POST_COOLDOWN  = 3.0                    # антифлуд, сек

# --------------------- ХРАНИЛИЩЕ В ОПЕРАТИВКЕ ------------------------
LOCK = threading.Lock()
SESSIONS: Dict[str, Dict[str, Any]] = {}   # sid -> данные сессии
POSTS: Dict[str, Dict[str, Any]] = {}      # id  -> пост


def now() -> float:
    return time.time()


def ordered_posts() -> List[Dict[str, Any]]:
    return sorted(POSTS.values(), key=lambda p: p["created"], reverse=True)


def online_count() -> int:
    t = now()
    return sum(1 for s in SESSIONS.values() if t - s["last_seen"] < ONLINE_WINDOW)


def public_post(p: Dict[str, Any], uid: str) -> Dict[str, Any]:
    return {
        "id": p["id"],
        "text": p["text"],
        "author": p["author"],
        "created": p["created"],
        "likes": len(p["likes"]),
        "liked": uid in p["likes"],
        "mine": p["owner"] == uid,
        "comments": [
            {
                "id": c["id"],
                "author": c["author"],
                "text": c["text"],
                "created": c["created"],
                "mine": c["owner"] == uid,
            }
            for c in p["comments"]
        ],
    }


async def janitor() -> None:
    """Раз в минуту подчищаем просроченные сессии."""
    while True:
        await asyncio.sleep(60)
        t = now()
        with LOCK:
            dead = [sid for sid, s in SESSIONS.items() if t - s["last_seen"] > SESSION_TTL]
            for sid in dead:
                SESSIONS.pop(sid, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(janitor())
    yield
    task.cancel()


app = FastAPI(title="sldchat", lifespan=lifespan)


# ------------------------- СЕССИИ И COOKIE ---------------------------
async def get_session(request: Request, response: Response) -> Dict[str, Any]:
    """
    Достаём сессию по cookie. Если её нет — создаём новую
    и кладём идентификатор в cookie (httponly, 30 дней).
    """
    sid = request.cookies.get(SESSION_COOKIE)
    sess = SESSIONS.get(sid) if sid else None

    if sess is None:
        sid = secrets.token_urlsafe(24)
        sess = {
            "sid": sid,
            "uid": "u-" + secrets.token_hex(6),
            "name": None,
            "created": now(),
            "last_seen": now(),
            "last_post": 0.0,
        }
        SESSIONS[sid] = sess
        response.set_cookie(
            key=SESSION_COOKIE,
            value=sid,
            max_age=SESSION_TTL,
            httponly=True,
            samesite="lax",
            path="/",
        )

    sess["last_seen"] = now()
    return sess


# ------------------------------ МОДЕЛИ -------------------------------
class PostIn(BaseModel):
    text: str = ""


class CommentIn(BaseModel):
    text: str = ""


class NameIn(BaseModel):
    name: str = ""


# ------------------------------- API ---------------------------------
@app.get("/api/state")
async def api_state(sess: Dict[str, Any] = Depends(get_session)):
    with LOCK:
        posts = [public_post(p, sess["uid"]) for p in ordered_posts()]
        online = online_count()
    return {
        "me": {
            "uid": sess["uid"],
            "name": sess["name"] or "Аноним",
            "created": sess["created"],
            "online": online,
        },
        "posts": posts,
    }


@app.post("/api/name")
async def api_set_name(payload: NameIn, sess: Dict[str, Any] = Depends(get_session)):
    name = (payload.name or "").strip()[:MAX_NAME_LEN]
    sess["name"] = name or None
    return {"ok": True, "name": sess["name"] or "Аноним"}


@app.post("/api/session/reset")
async def api_reset_session(
    sess: Dict[str, Any] = Depends(get_session),
    response: Response = None,
):
    """Убиваем текущую сессию и стираем cookie — при следующем запросе будет новая."""
    SESSIONS.pop(sess["sid"], None)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/posts")
async def api_create_post(payload: PostIn, sess: Dict[str, Any] = Depends(get_session)):
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(400, "Пустой пост")
    if len(text) > MAX_POST_LEN:
        raise HTTPException(400, f"Максимум {MAX_POST_LEN} символов")

    t = now()
    if t - sess["last_post"] < POST_COOLDOWN:
        raise HTTPException(429, "Слишком часто — подождите пару секунд")
    sess["last_post"] = t

    pid = secrets.token_hex(6)
    post = {
        "id": pid,
        "text": text,
        "author": sess["name"] or "Аноним",
        "owner": sess["uid"],
        "created": t,
        "likes": set(),
        "comments": [],
    }

    with LOCK:
        POSTS[pid] = post
        if len(POSTS) > MAX_POSTS:
            extra = len(POSTS) - MAX_POSTS
            for old in sorted(POSTS.values(), key=lambda p: p["created"])[:extra]:
                POSTS.pop(old["id"], None)

    return {"ok": True, "id": pid}


@app.delete("/api/posts/{pid}")
async def api_delete_post(pid: str, sess: Dict[str, Any] = Depends(get_session)):
    with LOCK:
        post = POSTS.get(pid)
        if not post:
            raise HTTPException(404, "Пост не найден")
        if post["owner"] != sess["uid"]:
            raise HTTPException(403, "Это не ваш пост")
        POSTS.pop(pid, None)
    return {"ok": True}


@app.post("/api/posts/{pid}/like")
async def api_like(pid: str, sess: Dict[str, Any] = Depends(get_session)):
    with LOCK:
        post = POSTS.get(pid)
        if not post:
            raise HTTPException(404, "Пост не найден")
        uid = sess["uid"]
        if uid in post["likes"]:
            post["likes"].discard(uid)
        else:
            post["likes"].add(uid)
        likes = len(post["likes"])
    return {"ok": True, "likes": likes}


@app.post("/api/posts/{pid}/comments")
async def api_add_comment(
    pid: str, payload: CommentIn, sess: Dict[str, Any] = Depends(get_session)
):
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(400, "Пустой комментарий")
    if len(text) > MAX_COMMENT_LEN:
        raise HTTPException(400, f"Максимум {MAX_COMMENT_LEN} символов")

    with LOCK:
        post = POSTS.get(pid)
        if not post:
            raise HTTPException(404, "Пост не найден")
        if len(post["comments"]) >= MAX_COMMENTS:
            raise HTTPException(400, "К этому посту уже слишком много комментариев")
        post["comments"].append(
            {
                "id": secrets.token_hex(4),
                "author": sess["name"] or "Аноним",
                "text": text,
                "owner": sess["uid"],
                "created": now(),
            }
        )
    return {"ok": True}


@app.delete("/api/posts/{pid}/comments/{cid}")
async def api_delete_comment(
    pid: str, cid: str, sess: Dict[str, Any] = Depends(get_session)
):
    with LOCK:
        post = POSTS.get(pid)
        if not post:
            raise HTTPException(404, "Пост не найден")
        comment = next((c for c in post["comments"] if c["id"] == cid), None)
        if not comment:
            raise HTTPException(404, "Комментарий не найден")
        if comment["owner"] != sess["uid"] and post["owner"] != sess["uid"]:
            raise HTTPException(403, "Нет прав")
        post["comments"] = [c for c in post["comments"] if c["id"] != cid]
    return {"ok": True}


# ------------------------------ СТРАНИЦА -----------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sldchat — текстовая соцсеть без регистрации</title>
<style>
:root{
  --bg:#0e0e13; --surface:#17171e; --surface2:#1e1e27; --text:#e8e8f0;
  --muted:#8a8a9c; --primary:#5b8cff; --border:#2a2a37; --ok:#4caf50; --danger:#ff5f5f;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg); color:var(--text);
  font-family:"Courier New",Courier,monospace; font-size:15px; line-height:1.45;
  display:flex; justify-content:center; padding:18px 14px 60px;
}
.wrapper{width:100%; max-width:720px; display:flex; flex-direction:column; gap:14px}
header{display:flex; flex-direction:column; gap:8px; border-bottom:2px solid var(--border); padding-bottom:12px}
.logo{font-size:1.9rem; font-weight:bold; color:var(--primary); letter-spacing:1px}
.logo small{color:var(--muted); font-size:.68rem; font-weight:normal; letter-spacing:0}
.pills{display:flex; gap:8px; flex-wrap:wrap; font-size:.72rem}
.pill{border:1px solid var(--border); border-radius:20px; padding:2px 10px; color:var(--muted); background:var(--surface)}
.pill.on{color:var(--ok); border-color:rgba(76,175,80,.35); background:rgba(76,175,80,.08)}
.card{background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px}
.notice{background:var(--surface2); border:1px dashed var(--border); border-radius:10px; padding:14px; font-size:.82rem; display:flex; flex-direction:column; gap:8px}
.notice b{color:var(--primary)}
.notice code{background:var(--bg); border:1px solid var(--border); border-radius:4px; padding:1px 5px; color:var(--primary)}
.muted{color:var(--muted)}
button{font-family:inherit; font-size:.85rem; cursor:pointer; border-radius:6px; border:1px solid var(--border);
       background:var(--surface2); color:var(--text); padding:8px 14px; font-weight:bold; transition:.15s}
button:hover{background:#262633}
button.primary{background:var(--primary); border-color:var(--primary); color:#fff}
button.primary:hover{filter:brightness(1.1)}
button.ghost{background:transparent}
input,textarea{width:100%; font-family:inherit; font-size:.92rem; background:var(--surface2);
       border:1px solid var(--border); border-radius:6px; color:var(--text); padding:10px; outline:none; resize:vertical}
input:focus,textarea:focus{border-color:var(--primary)}
.row{display:flex; gap:8px; align-items:center}
.between{justify-content:space-between}
.composer{display:flex; flex-direction:column; gap:10px}
.post{display:flex; flex-direction:column; gap:10px; margin-bottom:12px}
.post-head{display:flex; align-items:center; gap:8px; font-size:.78rem; border-bottom:1px dotted var(--border); padding-bottom:8px}
.author{color:var(--primary); font-weight:bold}
.post-text{white-space:pre-wrap; word-break:break-word}
.post-foot{display:flex; gap:8px; flex-wrap:wrap; border-top:1px solid var(--border); padding-top:10px}
.act{padding:5px 11px; font-size:.78rem}
.act.on{background:var(--primary); border-color:var(--primary); color:#fff}
.comments{display:none; flex-direction:column; gap:8px; border-top:1px dashed var(--border); padding-top:10px}
.comments.open{display:flex}
.comment{background:var(--surface2); border:1px solid var(--border); border-radius:6px; padding:8px; font-size:.85rem}
.comment .ch{display:flex; justify-content:space-between; gap:8px; font-size:.72rem; margin-bottom:4px}
.cform{display:flex; gap:6px}
.cform button{padding:8px 14px}
.empty{text-align:center; color:var(--muted); padding:24px; font-size:.85rem}
#toast{position:fixed; left:50%; bottom:20px; transform:translateX(-50%) translateY(20px);
       background:var(--surface2); border:1px solid var(--border); color:var(--text);
       padding:10px 18px; border-radius:8px; font-size:.82rem; opacity:0; pointer-events:none;
       transition:.2s; z-index:50}
#toast.show{opacity:1; transform:translateX(-50%) translateY(0)}
@media(max-width:520px){ .logo{font-size:1.5rem} body{padding:12px 10px 60px} }
</style>
</head>
<body>
<div class="wrapper">

  <header>
    <div class="logo">sldchat <small>// текстовая соцсеть без регистрации</small></div>
    <div class="pills">
      <span class="pill on" id="online">● — онлайн</span>
      <span class="pill" id="me-pill">вы: Аноним</span>
      <span class="pill" id="sid-pill">сессия: …</span>
    </div>
  </header>

  <div class="notice">
    <div><b>Про сессии и cookie.</b> Регистрации и паролей нет. При первом визите сервер создаёт
      <b>сессию</b> и кладёт её идентификатор в cookie <code>sldchat_sid</code> — именно поэтому
      вас «помнят» после перезагрузки страницы. Имя и права на посты живут внутри этой сессии.</div>
    <div><b>Про хранение.</b> Все посты, комментарии и лайки лежат <b>только в оперативной памяти</b>
      сервера. Перезапуск сервера — и всё исчезает безвозвратно. Фото и файлов нет:
      <b>только текст</b>.</div>
    <div class="row between" style="flex-wrap:wrap">
      <span class="muted" id="session-info">…</span>
      <button class="ghost" onclick="resetSession()">Сбросить сессию</button>
    </div>
  </div>

  <div class="card composer">
    <div class="row">
      <input id="name" maxlength="24" placeholder="Ваше имя (необязательно, по умолчанию «Аноним»)">
      <button onclick="saveName()">ОК</button>
    </div>
    <textarea id="text" rows="4" maxlength="1000" placeholder="Что происходит?"></textarea>
    <div class="row between">
      <span class="muted" id="counter">0 / 1000</span>
      <button class="primary" onclick="createPost()">Опубликовать</button>
    </div>
  </div>

  <div id="feed"><div class="empty">Загрузка…</div></div>
</div>
<div id="toast"></div>

<script>
const $ = (s) => document.querySelector(s);

let STATE = { me: {}, posts: [] };
let lastPostsJSON = "";
let lastTickError = 0;

/* ------------------------- утилиты ------------------------- */
function esc(s){
  return String(s ?? "").replace(/[&<>"']/g, m => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]
  ));
}

function toast(msg){
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2200);
}

async function api(path, method = "GET", body){
  const r = await fetch(path, {
    method,
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : null,
    credentials: "same-origin"
  });
  let data = null;
  try { data = await r.json(); } catch(e){}
  if(!r.ok) throw new Error((data && data.detail) || "Ошибка сервера");
  return data;
}

function ago(ts){
  const d = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if(d < 5)     return "только что";
  if(d < 60)    return d + " с назад";
  if(d < 3600)  return Math.floor(d/60) + " мин назад";
  if(d < 86400) return Math.floor(d/3600) + " ч назад";
  return new Date(ts*1000).toLocaleDateString("ru-RU");
}

/* ------------------------- шапка ------------------------- */
function updateHeader(){
  const me = STATE.me || {};
  $("#online").textContent   = "● " + (me.online ?? 0) + " онлайн";
  $("#me-pill").textContent  = "вы: " + (me.name || "Аноним");
  $("#sid-pill").textContent = "сессия: " + String(me.uid || "…").slice(0, 10) + "…";
  $("#session-info").textContent =
    "uid: " + (me.uid || "—") +
    " · сессия создана: " + (me.created ? new Date(me.created*1000).toLocaleString("ru-RU") : "—");

  if(document.activeElement !== $("#name")){
    $("#name").value = me.name || "";
  }
}

/* ------------------------- лента ------------------------- */
function renderPosts(){
  const feed = $("#feed");

  // сохраняем черновики комментариев и открытые ветки
  const drafts = {}, open = new Set();
  document.querySelectorAll(".cinput").forEach(i => {
    if(i.value.trim()) drafts[i.dataset.pid] = i.value;
  });
  document.querySelectorAll(".comments.open").forEach(c => open.add(c.dataset.pid));

  const active = document.activeElement;
  const focusPid = (active && active.classList && active.classList.contains("cinput"))
      ? active.dataset.pid : null;
  const caret = focusPid ? active.selectionStart : 0;

  if(!STATE.posts.length){
    feed.innerHTML = '<div class="empty">Пока пусто. Напишите первое сообщение!</div>';
    return;
  }

  feed.innerHTML = "";
  STATE.posts.forEach(p => {
    const el = document.createElement("div");
    el.className = "card post";

    const commentsHTML = p.comments.map(c => `
      <div class="comment">
        <div class="ch">
          <span class="author">${esc(c.author)}</span>
          <span class="muted">
            ${ago(c.created)}
            ${(c.mine || p.mine)
              ? `<button class="ghost" data-act="delcomment" data-pid="${p.id}" data-cid="${c.id}"
                   style="padding:0 4px;font-size:.72rem;border:none;color:var(--danger)">[×]</button>`
              : ""}
          </span>
        </div>
        <div>${esc(c.text)}</div>
      </div>`).join("");

    el.innerHTML = `
      <div class="post-head">
        <span class="author">${esc(p.author)}</span>
        <span class="muted">· ${ago(p.created)}</span>
        <span style="flex:1"></span>
        ${p.mine
          ? `<button class="ghost" data-act="delpost" data-pid="${p.id}"
               style="padding:2px 8px;font-size:.72rem;color:var(--danger)">[удалить]</button>`
          : ""}
      </div>
      <div class="post-text">${esc(p.text)}</div>
      <div class="post-foot">
        <button class="act ${p.liked ? "on" : ""}" data-act="like" data-pid="${p.id}">♥ ${p.likes}</button>
        <button class="act" data-act="toggle" data-pid="${p.id}">комментарии (${p.comments.length})</button>
      </div>
      <div class="comments ${open.has(p.id) ? "open" : ""}" data-pid="${p.id}">
        ${commentsHTML || '<div class="muted" style="font-size:.78rem">Комментариев пока нет.</div>'}
        <div class="cform">
          <input class="cinput" data-pid="${p.id}" maxlength="300" placeholder="Ваш комментарий...">
          <button data-act="comment" data-pid="${p.id}">→</button>
        </div>
      </div>`;

    feed.appendChild(el);
  });

  // восстанавливаем черновики
  document.querySelectorAll(".cinput").forEach(i => {
    const pid = i.dataset.pid;
    if(drafts[pid]) i.value = drafts[pid];
  });
  if(focusPid){
    const el = document.querySelector('.cinput[data-pid="' + focusPid + '"]');
    if(el){ el.focus(); try{ el.setSelectionRange(caret, caret); }catch(e){} }
  }
}

/* ------------------------- опрос сервера ------------------------- */
async function tick(){
  try{
    const s = await api("/api/state");
    STATE = s;
    updateHeader();
    const j = JSON.stringify(s.posts);
    if(j !== lastPostsJSON){ lastPostsJSON = j; renderPosts(); }
  }catch(e){
    if(Date.now() - lastTickError > 10000){
      lastTickError = Date.now();
      toast("Нет связи с сервером");
    }
  }
}

/* ------------------------- действия ------------------------- */
async function createPost(){
  const ta = $("#text");
  const text = ta.value.trim();
  if(!text) return toast("Введите текст");
  try{
    await api("/api/posts", "POST", { text });
    ta.value = "";
    $("#counter").textContent = "0 / 1000";
    await tick();
    toast("Опубликовано");
  }catch(e){ toast(e.message); }
}

async function saveName(){
  try{
    const r = await api("/api/name", "POST", { name: $("#name").value });
    STATE.me.name = r.name;
    updateHeader();
    toast("Имя сохранено: " + r.name);
  }catch(e){ toast(e.message); }
}

async function resetSession(){
  if(!confirm("Сбросить сессию? Имя и права на посты будут потеряны.")) return;
  try{
    await api("/api/session/reset", "POST");
    lastPostsJSON = "";
    await tick();
    toast("Сессия сброшена");
  }catch(e){ toast(e.message); }
}

/* ------------------------- события ------------------------- */
$("#feed").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]");
  if(!btn) return;
  const act = btn.dataset.act, pid = btn.dataset.pid;

  try{
    if(act === "like"){
      await api("/api/posts/" + pid + "/like", "POST");
      await tick();
    }
    else if(act === "toggle"){
      document.querySelector('.comments[data-pid="' + pid + '"]').classList.toggle("open");
    }
    else if(act === "comment"){
      const input = document.querySelector('.cinput[data-pid="' + pid + '"]');
      const text = input.value.trim();
      if(!text) return toast("Пустой комментарий");
      await api("/api/posts/" + pid + "/comments", "POST", { text });
      input.value = "";
      await tick();
    }
    else if(act === "delpost"){
      if(!confirm("Удалить пост?")) return;
      await api("/api/posts/" + pid, "DELETE");
      await tick();
    }
    else if(act === "delcomment"){
      if(!confirm("Удалить комментарий?")) return;
      await api("/api/posts/" + pid + "/comments/" + btn.dataset.cid, "DELETE");
      await tick();
    }
  }catch(err){ toast(err.message); }
});

$("#feed").addEventListener("keydown", (e) => {
  if(e.key === "Enter" && e.target.classList.contains("cinput")){
    e.preventDefault();
    const pid = e.target.dataset.pid;
    document.querySelector('[data-act="comment"][data-pid="' + pid + '"]').click();
  }
});

$("#text").addEventListener("input", (e) => {
  $("#counter").textContent = e.target.value.length + " / 1000";
});

$("#name").addEventListener("keydown", (e) => {
  if(e.key === "Enter") saveName();
});

/* ------------------------- старт ------------------------- */
tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index(sess: Dict[str, Any] = Depends(get_session)):
    # Возвращаем строку, а не Response — тогда cookie, выставленная
    # в зависимости get_session, не потеряется.
    return PAGE


if __name__ == "__main__":
    # workers=1 обязательно: данные живут в памяти процесса.
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
