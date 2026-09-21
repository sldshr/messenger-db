# main.py
# Запуск:  pip install fastapi uvicorn
#          uvicorn main:app --reload
# Открыть: http://127.0.0.1:8000

import time
import uuid
import json
import urllib.request
from typing import Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="sldChat")

# ------------------------------------------------------------------
# "База" в оперативке
# ------------------------------------------------------------------
POSTS: Dict[str, dict] = {}
MAX_POST_LEN = 1000
MAX_COMMENT_LEN = 500

RU_COUNTRIES = {"RU", "BY", "KZ", "UA", "KG", "TJ", "UZ", "AM", "AZ", "MD"}
_lang_cache: Dict[str, str] = {}


# ------------------------------------------------------------------
# Определение языка по IP (fallback — Accept-Language)
# ------------------------------------------------------------------
def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def detect_lang(ip: str, accept_language: str) -> str:
    key = ip or "unknown"
    if key in _lang_cache:
        return _lang_cache[key]
    lang = None
    private = (
        not ip
        or ip.startswith("127.")
        or ip.startswith("10.")
        or ip.startswith("192.168.")
        or ip.startswith("172.")
        or ip in ("::1", "localhost")
    )
    if not private:
        try:
            req = urllib.request.Request(
                f"http://ip-api.com/json/{ip}?fields=countryCode",
                headers={"User-Agent": "sldChat"},
            )
            with urllib.request.urlopen(req, timeout=2) as r:
                data = json.loads(r.read().decode())
                cc = (data.get("countryCode") or "").upper()
                if cc in RU_COUNTRIES:
                    lang = "ru"
                elif cc:
                    lang = "en"
        except Exception:
            pass
    if not lang:
        al = (accept_language or "").lower()
        lang = "ru" if (al.startswith("ru") or ",ru" in al or "ru-" in al) else "en"
    _lang_cache[key] = lang
    return lang


# ------------------------------------------------------------------
# Модели
# ------------------------------------------------------------------
class PostIn(BaseModel):
    text: str


class VoteIn(BaseModel):
    direction: int
    client_id: str


class CommentIn(BaseModel):
    text: str
    client_id: str = ""


# ------------------------------------------------------------------
# Сериализация
# ------------------------------------------------------------------
def serialize_post(p: dict, client_id: str = "") -> dict:
    up = sum(1 for v in p["votes"].values() if v == 1)
    down = sum(1 for v in p["votes"].values() if v == -1)
    user_vote = p["votes"].get(client_id, 0) if client_id else 0
    comments = []
    for c in p["comments"]:
        cup = sum(1 for v in c["votes"].values() if v == 1)
        cdown = sum(1 for v in c["votes"].values() if v == -1)
        cuv = c["votes"].get(client_id, 0) if client_id else 0
        comments.append({
            "id": c["id"],
            "text": c["text"],
            "created_at": c["created_at"],
            "upvotes": cup,
            "downvotes": cdown,
            "user_vote": cuv,
        })
    return {
        "id": p["id"],
        "text": p["text"],
        "created_at": p["created_at"],
        "upvotes": up,
        "downvotes": down,
        "user_vote": user_vote,
        "comments": comments,
    }


# ------------------------------------------------------------------
# API
# ------------------------------------------------------------------
@app.get("/api/posts")
def api_list(q: str = "", client_id: str = ""):
    items = list(POSTS.values())
    if q:
        needle = q.strip().lower()
        if needle:
            items = [p for p in items if needle in p["text"].lower()]
    items.sort(key=lambda p: p["created_at"], reverse=True)
    return {"posts": [serialize_post(p, client_id) for p in items]}


@app.get("/api/posts/{pid}")
def api_get(pid: str, client_id: str = ""):
    p = POSTS.get(pid)
    if not p:
        raise HTTPException(404, "not found")
    return serialize_post(p, client_id)


@app.post("/api/posts")
def api_create(payload: PostIn):
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_POST_LEN:
        raise HTTPException(400, "too long")
    pid = uuid.uuid4().hex[:10]
    p = {
        "id": pid,
        "text": text,
        "created_at": time.time(),
        "votes": {},
        "comments": [],
    }
    POSTS[pid] = p
    return serialize_post(p)


@app.post("/api/posts/{pid}/vote")
def api_vote_post(pid: str, v: VoteIn):
    p = POSTS.get(pid)
    if not p:
        raise HTTPException(404, "not found")
    if v.direction not in (-1, 1) or not v.client_id:
        raise HTTPException(400, "bad request")
    cur = p["votes"].get(v.client_id, 0)
    if cur == v.direction:
        p["votes"].pop(v.client_id, None)
    else:
        p["votes"][v.client_id] = v.direction
    return serialize_post(p, v.client_id)


@app.post("/api/posts/{pid}/comments")
def api_add_comment(pid: str, c: CommentIn):
    p = POSTS.get(pid)
    if not p:
        raise HTTPException(404, "not found")
    text = c.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_COMMENT_LEN:
        raise HTTPException(400, "too long")
    comment = {
        "id": uuid.uuid4().hex[:10],
        "text": text,
        "created_at": time.time(),
        "votes": {},
    }
    p["comments"].append(comment)
    return serialize_post(p, c.client_id)


@app.post("/api/posts/{pid}/comments/{cid}/vote")
def api_vote_comment(pid: str, cid: str, v: VoteIn):
    p = POSTS.get(pid)
    if not p:
        raise HTTPException(404, "not found")
    c = next((x for x in p["comments"] if x["id"] == cid), None)
    if not c:
        raise HTTPException(404, "not found")
    if v.direction not in (-1, 1) or not v.client_id:
        raise HTTPException(400, "bad request")
    cur = c["votes"].get(v.client_id, 0)
    if cur == v.direction:
        c["votes"].pop(v.client_id, None)
    else:
        c["votes"][v.client_id] = v.direction
    return serialize_post(p, v.client_id)


# ------------------------------------------------------------------
# Локализация
# ------------------------------------------------------------------
TEXTS = {
    "ru": {
        "search_ph": "Поиск по постам...",
        "search": "Поиск",
        "theme": "Сменить тему",
        "post_ph": "Что нового? (до 1000 символов)",
        "comment_ph": "Написать комментарий...",
        "publish": "Опубликовать",
        "send_comment": "Отправить",
        "cancel": "Отмена",
        "back": "В главное меню",
        "no_posts": "Пока нет постов. Будьте первым!",
        "not_found": "Пост не найден",
        "open": "Открыть",
        "just_now": "только что",
        "sec_ago": "с назад",
        "min_ago": "мин назад",
        "hour_ago": "ч назад",
        "day_ago": "дн назад",
    },
    "en": {
        "search_ph": "Search posts...",
        "search": "Search",
        "theme": "Toggle theme",
        "post_ph": "What's new? (up to 1000 chars)",
        "comment_ph": "Write a comment...",
        "publish": "Post",
        "send_comment": "Send",
        "cancel": "Cancel",
        "back": "Back to main",
        "no_posts": "No posts yet. Be the first!",
        "not_found": "Post not found",
        "open": "Open",
        "just_now": "just now",
        "sec_ago": "s ago",
        "min_ago": "min ago",
        "hour_ago": "h ago",
        "day_ago": "d ago",
    },
}


# ------------------------------------------------------------------
# CSS
# ------------------------------------------------------------------
CSS = """
:root, [data-theme="light"] {
  --bg:#e9eef3; --card:#fff; --line:#d3dbe3; --muted:#7b8794;
  --accent:#4a76a8; --accent-dark:#3b6089; --text:#2c3844;
  --hover:#f3f6f9; --up:#e67e22; --down:#5d6dbb; --comment-bg:#f5f7f9;
}
[data-theme="dark"] {
  --bg:#16181c; --card:#1f2228; --line:#2e333b; --muted:#8b95a1;
  --accent:#5a87c0; --accent-dark:#4a76a8; --text:#e1e5ea;
  --hover:#262a31; --up:#e67e22; --down:#7b8bd0; --comment-bg:#262a31;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{
  font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  background:var(--bg);color:var(--text);display:flex;justify-content:center;
  transition:background .15s,color .15s;
}
.app{width:100%;max-width:620px;height:100vh;display:flex;flex-direction:column;
  background:var(--card);border-left:1px solid var(--line);border-right:1px solid var(--line);}
header{padding:10px 14px;border-bottom:1px solid var(--line);background:var(--accent);
  color:#fff;display:flex;align-items:center;gap:8px;flex:0 0 auto;}
header input[type=search]{flex:1;padding:8px 12px;border-radius:16px;border:none;
  outline:none;font-size:14px;background:rgba(255,255,255,.92);color:#222;}
header input[type=search]:focus{background:#fff;}
header button,header .back-btn{background:rgba(255,255,255,.15);border:none;color:#fff;
  border-radius:16px;padding:8px 12px;cursor:pointer;font-size:14px;text-decoration:none;
  transition:background .15s;}
header button:hover,header .back-btn:hover{background:rgba(255,255,255,.28);}
header .spacer{flex:1;}
main{flex:1 1 auto;overflow-y:auto;padding:12px;background:var(--bg);}
.empty{text-align:center;color:var(--muted);margin-top:40px;font-size:14px;}
.post{background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 2px rgba(0,0,0,.04);
  transition:border-color .15s;}
.post.active-comment{border-color:var(--accent);box-shadow:0 0 0 2px rgba(74,118,168,.15);}
.post-text{font-size:15px;line-height:1.45;white-space:pre-wrap;
  word-wrap:break-word;overflow-wrap:anywhere;}
.post-actions{display:flex;align-items:center;gap:6px;margin-top:10px;
  font-size:13px;color:var(--muted);}
.vote{background:transparent;border:none;cursor:pointer;padding:4px 6px;
  border-radius:4px;color:var(--muted);font-size:13px;
  transition:background .12s,color .12s;}
.vote:hover{background:var(--hover);color:var(--text);}
.vote.active-up{color:var(--up);font-weight:bold;}
.vote.active-down{color:var(--down);font-weight:bold;}
.score{min-width:22px;text-align:center;font-weight:600;color:var(--text);font-size:13px;}
.comment-btn{background:transparent;border:none;cursor:pointer;padding:4px 8px;
  border-radius:4px;color:var(--muted);font-size:13px;
  transition:background .12s,color .12s;}
.comment-btn:hover{background:var(--hover);color:var(--text);}
.open-link{color:var(--muted);text-decoration:none;padding:4px 8px;
  border-radius:4px;font-size:13px;transition:background .12s,color .12s;}
.open-link:hover{background:var(--hover);color:var(--accent);}
.time{margin-left:auto;font-size:12px;color:var(--muted);}
.comments{margin-top:10px;padding-top:10px;border-top:1px dashed var(--line);}
.comment{padding:8px 10px;margin-bottom:6px;background:var(--comment-bg);
  border-radius:6px;font-size:14px;}
.comment:last-child{margin-bottom:0;}
.comment-text{line-height:1.4;white-space:pre-wrap;word-wrap:break-word;
  overflow-wrap:anywhere;}
.comment-actions{display:flex;align-items:center;gap:4px;margin-top:4px;
  font-size:12px;color:var(--muted);}
footer{flex:0 0 auto;border-top:1px solid var(--line);background:var(--card);
  padding:10px 12px;}
footer textarea{width:100%;min-height:60px;max-height:160px;resize:vertical;
  padding:10px 12px;border:1px solid var(--line);border-radius:8px;
  font-family:inherit;font-size:14px;outline:none;color:var(--text);
  background:var(--card);transition:border-color .15s;}
footer textarea:focus{border-color:var(--accent);}
.row{display:flex;align-items:center;justify-content:space-between;
  margin-top:8px;gap:8px;}
.counter{font-size:12px;color:var(--muted);}
.counter.warn{color:#c0392b;font-weight:600;}
.btns{display:flex;gap:6px;}
button.send{background:var(--accent);color:#fff;border:none;padding:8px 18px;
  border-radius:6px;font-size:14px;cursor:pointer;transition:background .15s;}
button.send:hover{background:var(--accent-dark);}
button.send:disabled{background:#a9b6c4;cursor:default;}
button.cancel{background:transparent;color:var(--muted);border:1px solid var(--line);
  padding:8px 14px;border-radius:6px;font-size:14px;cursor:pointer;}
button.cancel:hover{background:var(--hover);color:var(--text);}
"""


# ------------------------------------------------------------------
# JS
# ------------------------------------------------------------------
JS = r"""
function initTheme(){
  var theme = localStorage.getItem('sldchat_theme') || 'light';
  document.documentElement.setAttribute('data-theme', theme);
  var btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = theme === 'dark' ? '\u2600' : '\u263E';
}
function toggleTheme(){
  var cur = document.documentElement.getAttribute('data-theme') || 'light';
  var next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('sldchat_theme', next);
  var btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = next === 'dark' ? '\u2600' : '\u263E';
}

function getClientId(){
  var cid = localStorage.getItem('sldchat_cid');
  if (!cid){
    cid = (typeof crypto !== 'undefined' && crypto.randomUUID)
      ? crypto.randomUUID()
      : (Math.random().toString(36).slice(2) + Date.now().toString(36));
    localStorage.setItem('sldchat_cid', cid);
  }
  return cid;
}
var CLIENT_ID = getClientId();

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

function timeAgo(ts){
  var d = Math.floor(Date.now()/1000 - ts);
  if (d < 5) return T.just_now;
  if (d < 60) return d + ' ' + T.sec_ago;
  var m = Math.floor(d/60);
  if (m < 60) return m + ' ' + T.min_ago;
  var h = Math.floor(m/60);
  if (h < 24) return h + ' ' + T.hour_ago;
  var days = Math.floor(h/24);
  if (days < 30) return days + ' ' + T.day_ago;
  return new Date(ts*1000).toLocaleDateString();
}

var feed = document.getElementById('feed');
var inputEl = document.getElementById('newPost');
var sendBtn = document.getElementById('send');
var counter = document.getElementById('counter');
var cancelBtn = document.getElementById('cancel');
var searchEl = document.getElementById('search');
var searchBtn = document.getElementById('searchBtn');

var maxLen = MAX_POST_LEN;
var commentTarget = null;
var postDraft = '';

if (MODE === 'post'){
  commentTarget = POST_ID;
  inputEl.placeholder = T.comment_ph;
  inputEl.maxLength = MAX_COMMENT_LEN;
  maxLen = MAX_COMMENT_LEN;
  if (cancelBtn) cancelBtn.style.display = 'none';
  if (sendBtn) sendBtn.textContent = T.send_comment;
} else {
  inputEl.maxLength = MAX_POST_LEN;
  maxLen = MAX_POST_LEN;
  if (cancelBtn) cancelBtn.style.display = 'none';
}

function updateCounter(){
  var len = inputEl.value.length;
  counter.textContent = len + ' / ' + maxLen;
  counter.classList.toggle('warn', len >= maxLen);
  sendBtn.disabled = len === 0 || len > maxLen;
}

function renderPosts(posts){
  if (!posts.length){
    feed.innerHTML = '<div class="empty">' + escapeHtml(T.no_posts) + '</div>';
    return;
  }
  feed.innerHTML = posts.map(renderPost).join('');
}

function renderPost(p){
  var score = p.upvotes - p.downvotes;
  var upCls = p.user_vote === 1 ? 'active-up' : '';
  var downCls = p.user_vote === -1 ? 'active-down' : '';
  var commentsHtml = (p.comments || []).map(function(c){ return renderComment(c, p.id); }).join('');
  var activeCommentCls = (commentTarget === p.id) ? 'active-comment' : '';
  var openLink = '';
  if (MODE === 'main'){
    openLink = '<a class="open-link" href="/p/' + p.id + '" title="' + escapeHtml(T.open) + '">\u2197</a>';
  }
  var commentBtn = '';
  if (MODE === 'main'){
    commentBtn = '<button class="comment-btn" data-action="comment" data-post-id="' + p.id + '">'
      + '\uD83D\uDCAC ' + (p.comments ? p.comments.length : 0) + '</button>';
  }
  return ''
    + '<div class="post ' + activeCommentCls + '" data-post-id="' + p.id + '">'
    +   '<div class="post-text">' + escapeHtml(p.text) + '</div>'
    +   '<div class="post-actions">'
    +     '<button class="vote up ' + upCls + '" data-action="vote" data-post-id="' + p.id + '" data-dir="1">\u25B2</button>'
    +     '<span class="score">' + score + '</span>'
    +     '<button class="vote down ' + downCls + '" data-action="vote" data-post-id="' + p.id + '" data-dir="-1">\u25BC</button>'
    +     commentBtn
    +     openLink
    +     '<span class="time">' + timeAgo(p.created_at) + '</span>'
    +   '</div>'
    +   (commentsHtml ? '<div class="comments">' + commentsHtml + '</div>' : '')
    + '</div>';
}

function renderComment(c, postId){
  var score = c.upvotes - c.downvotes;
  var upCls = c.user_vote === 1 ? 'active-up' : '';
  var downCls = c.user_vote === -1 ? 'active-down' : '';
  return ''
    + '<div class="comment" data-comment-id="' + c.id + '">'
    +   '<div class="comment-text">' + escapeHtml(c.text) + '</div>'
    +   '<div class="comment-actions">'
    +     '<button class="vote up ' + upCls + '" data-action="vote-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-dir="1">\u25B2</button>'
    +     '<span class="score">' + score + '</span>'
    +     '<button class="vote down ' + downCls + '" data-action="vote-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-dir="-1">\u25BC</button>'
    +     '<span class="time">' + timeAgo(c.created_at) + '</span>'
    +   '</div>'
    + '</div>';
}

async function load(){
  try {
    if (MODE === 'post'){
      var r = await fetch('/api/posts/' + POST_ID + '?client_id=' + encodeURIComponent(CLIENT_ID));
      if (!r.ok){
        feed.innerHTML = '<div class="empty">' + escapeHtml(T.not_found) + '</div>';
        return;
      }
      var data = await r.json();
      renderPosts([data]);
    } else {
      var q = (searchEl && searchEl.value || '').trim();
      var url = '/api/posts?q=' + encodeURIComponent(q) + '&client_id=' + encodeURIComponent(CLIENT_ID);
      var r2 = await fetch(url);
      var d2 = await r2.json();
      renderPosts(d2.posts);
    }
  } catch(e){
    feed.innerHTML = '<div class="empty">Network error</div>';
  }
}

function enterCommentMode(postId){
  if (MODE !== 'main') return;
  if (commentTarget === postId){ exitCommentMode(); return; }
  if (commentTarget === null){ postDraft = inputEl.value; }
  commentTarget = postId;
  inputEl.value = '';
  inputEl.placeholder = T.comment_ph;
  inputEl.maxLength = MAX_COMMENT_LEN;
  maxLen = MAX_COMMENT_LEN;
  cancelBtn.style.display = '';
  sendBtn.textContent = T.send_comment;
  updateCounter();
  var posts = document.querySelectorAll('.post');
  for (var i=0;i<posts.length;i++){
    posts[i].classList.toggle('active-comment', posts[i].dataset.postId === postId);
  }
  inputEl.focus();
}

function exitCommentMode(){
  if (MODE !== 'main') return;
  commentTarget = null;
  inputEl.value = postDraft;
  inputEl.placeholder = T.post_ph;
  inputEl.maxLength = MAX_POST_LEN;
  maxLen = MAX_POST_LEN;
  cancelBtn.style.display = 'none';
  sendBtn.textContent = T.publish;
  updateCounter();
  var posts = document.querySelectorAll('.post');
  for (var i=0;i<posts.length;i++){
    posts[i].classList.remove('active-comment');
  }
}

async function send(){
  var text = inputEl.value.trim();
  if (!text) return;
  sendBtn.disabled = true;
  try {
    if (commentTarget){
      var r = await fetch('/api/posts/' + commentTarget + '/comments', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text, client_id: CLIENT_ID})
      });
      if (r.ok){
        inputEl.value = '';
        if (MODE === 'main'){ exitCommentMode(); } else { updateCounter(); }
        await load();
      } else {
        var e1 = await r.json().catch(function(){return {};});
        alert(e1.detail || 'Error');
      }
    } else {
      var r2 = await fetch('/api/posts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text})
      });
      if (r2.ok){
        inputEl.value = '';
        if (searchEl) searchEl.value = '';
        updateCounter();
        await load();
      } else {
        var e2 = await r2.json().catch(function(){return {};});
        alert(e2.detail || 'Error');
      }
    }
  } catch(e){
    alert('Network error');
  } finally {
    updateCounter();
  }
}

feed.addEventListener('click', async function(e){
  var btn = e.target.closest('[data-action]');
  if (!btn) return;
  e.preventDefault();
  var action = btn.dataset.action;
  var postId = btn.dataset.postId;
  var commentId = btn.dataset.commentId;
  var dir = parseInt(btn.dataset.dir || '0', 10);

  if (action === 'vote'){
    await fetch('/api/posts/' + postId + '/vote', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({direction: dir, client_id: CLIENT_ID})
    });
    load();
  } else if (action === 'vote-comment'){
    await fetch('/api/posts/' + postId + '/comments/' + commentId + '/vote', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({direction: dir, client_id: CLIENT_ID})
    });
    load();
  } else if (action === 'comment'){
    enterCommentMode(postId);
  }
});

inputEl.addEventListener('input', updateCounter);
inputEl.addEventListener('keydown', function(e){
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)){
    e.preventDefault();
    send();
  }
});
sendBtn.addEventListener('click', send);
if (cancelBtn) cancelBtn.addEventListener('click', exitCommentMode);

if (searchEl){
  var tId;
  searchEl.addEventListener('input', function(){
    clearTimeout(tId);
    tId = setTimeout(load, 250);
  });
}
if (searchBtn){
  searchBtn.addEventListener('click', function(e){
    e.preventDefault();
    load();
  });
}
document.getElementById('themeBtn').addEventListener('click', toggleTheme);

initTheme();
updateCounter();
load();
setInterval(function(){ if (!document.hidden) load(); }, 5000);
"""


# ------------------------------------------------------------------
# Рендер страницы
# ------------------------------------------------------------------
def render_page(lang: str, mode: str, post_id: str = "") -> str:
    t = TEXTS[lang]
    is_post = mode == "post"

    if is_post:
        header = (
            '<header>'
            f'<a href="/" class="back-btn">\u2190 {t["back"]}</a>'
            '<div class="spacer"></div>'
            f'<button id="themeBtn" title="{t["theme"]}">\u263E</button>'
            '</header>'
        )
        footer = (
            '<footer>'
            f'<textarea id="newPost" maxlength="{MAX_COMMENT_LEN}" placeholder="{t["comment_ph"]}"></textarea>'
            '<div class="row">'
            f'<div id="counter" class="counter">0 / {MAX_COMMENT_LEN}</div>'
            '<div class="btns">'
            f'<button id="cancel" class="cancel" style="display:none">{t["cancel"]}</button>'
            f'<button id="send" class="send" disabled>{t["send_comment"]}</button>'
            '</div>'
            '</div>'
            '</footer>'
        )
    else:
        header = (
            '<header>'
            f'<input id="search" type="search" placeholder="{t["search_ph"]}" autocomplete="off" />'
            f'<button id="searchBtn" title="{t["search"]}">\U0001F50D</button>'
            f'<button id="themeBtn" title="{t["theme"]}">\u263E</button>'
            '</header>'
        )
        footer = (
            '<footer>'
            f'<textarea id="newPost" maxlength="{MAX_POST_LEN}" placeholder="{t["post_ph"]}"></textarea>'
            '<div class="row">'
            f'<div id="counter" class="counter">0 / {MAX_POST_LEN}</div>'
            '<div class="btns">'
            f'<button id="cancel" class="cancel" style="display:none">{t["cancel"]}</button>'
            f'<button id="send" class="send" disabled>{t["publish"]}</button>'
            '</div>'
            '</div>'
            '</footer>'
        )

    return (
        '<!DOCTYPE html>\n'
        f'<html lang="{lang}" data-theme="light">\n'
        '<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '<title>sldChat</title>\n'
        '<style>' + CSS + '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="app">\n'
        + header
        + '<main id="feed"><div class="empty">...</div></main>\n'
        + footer
        + '</div>\n'
        '<script>\n'
        f'const LANG = {json.dumps(lang)};\n'
        f'const MODE = {json.dumps(mode)};\n'
        f'const POST_ID = {json.dumps(post_id)};\n'
        f'const MAX_POST_LEN = {MAX_POST_LEN};\n'
        f'const MAX_COMMENT_LEN = {MAX_COMMENT_LEN};\n'
        f'const T = {json.dumps(t, ensure_ascii=False)};\n'
        + JS +
        '\n</script>\n'
        '</body>\n'
        '</html>'
    )


# ------------------------------------------------------------------
# Страницы
# ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ip = get_client_ip(request)
    al = request.headers.get("accept-language", "")
    lang = detect_lang(ip, al)
    return render_page(lang, "main")


@app.get("/p/{post_id}", response_class=HTMLResponse)
def post_page(post_id: str, request: Request):
    if post_id not in POSTS:
        raise HTTPException(404, "not found")
    ip = get_client_ip(request)
    al = request.headers.get("accept-language", "")
    lang = detect_lang(ip, al)
    return render_page(lang, "post", post_id)
