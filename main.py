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
# Язык по IP (fallback — Accept-Language)
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
        "search_ph": "Поиск по постам",
        "search": "Поиск",
        "theme": "Сменить тему",
        "back": "В главное меню",
        "post_ph": "Написать пост (до 1000 символов)",
        "comment_ph": "Написать комментарий",
        "publish": "Опубликовать",
        "send_comment": "Отправить",
        "no_posts": "Постов пока нет",
        "not_found": "Пост не найден",
        "just_now": "только что",
        "sec_ago": "с",
        "min_ago": "мин",
        "hour_ago": "ч",
        "day_ago": "д",
    },
    "en": {
        "search_ph": "Search posts",
        "search": "Search",
        "theme": "Toggle theme",
        "back": "Back to main",
        "post_ph": "Write a post (up to 1000 chars)",
        "comment_ph": "Write a comment",
        "publish": "Publish",
        "send_comment": "Send",
        "no_posts": "No posts yet",
        "not_found": "Post not found",
        "just_now": "just now",
        "sec_ago": "s",
        "min_ago": "min",
        "hour_ago": "h",
        "day_ago": "d",
    },
}


# ------------------------------------------------------------------
# CSS
# ------------------------------------------------------------------
CSS = """
:root, [data-theme="light"] {
  --bg:#ebebeb;
  --card:#ffffff;
  --line:#d4d4d4;
  --line-strong:#a8a8a8;
  --text:#101010;
  --muted:#767676;
  --hover:#f0f0f0;
  --accent:#101010;
  --accent-fg:#ffffff;
  --up:#1f9d55;
  --down:#d84343;
  --comment-bg:#f6f6f6;
}
[data-theme="dark"] {
  --bg:#0a0a0a;
  --card:#141414;
  --line:#282828;
  --line-strong:#3a3a3a;
  --text:#ececec;
  --muted:#888888;
  --hover:#1e1e1e;
  --accent:#ececec;
  --accent-fg:#101010;
  --up:#2ecc71;
  --down:#e74c3c;
  --comment-bg:#1c1c1c;
}

* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  display: flex;
  justify-content: center;
  font-size: 14px;
  -webkit-font-smoothing: antialiased;
}

.app {
  width: 100%;
  max-width: 660px;
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--card);
  border-left: 1px solid var(--line);
  border-right: 1px solid var(--line);
}

/* ================= HEADER ================= */
header {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px;
  border-bottom: 1px solid var(--line);
  background: var(--card);
}

header input[type="search"] {
  flex: 1;
  min-width: 0;
  padding: 0 12px;
  height: 34px;
  border: 1px solid var(--line);
  border-radius: 0;
  background: transparent;
  color: var(--text);
  font-size: 14px;
  font-family: inherit;
  outline: none;
  transition: border-color .12s;
}
header input[type="search"]:focus { border-color: var(--line-strong); }

.icon-btn {
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid var(--line);
  border-radius: 0;
  color: var(--text);
  cursor: pointer;
  padding: 0;
  text-decoration: none;
  transition: background .12s, border-color .12s, color .12s;
}
.icon-btn:hover { background: var(--hover); border-color: var(--line-strong); }
.icon-btn svg { display: block; }

.spacer { flex: 1; }

/* ================= FEED ================= */
main {
  flex: 1 1 auto;
  overflow-y: auto;
  background: var(--card);
}

.empty {
  padding: 80px 20px;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
  letter-spacing: .3px;
}

.post {
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
  background: var(--card);
}

.post-text {
  font-size: 15px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: anywhere;
  color: var(--text);
}

.post-actions {
  display: flex;
  align-items: center;
  gap: 1px;
  margin-top: 10px;
  font-size: 12px;
  color: var(--muted);
}

.vote-btn, .comment-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  padding: 0 8px;
  background: transparent;
  border: none;
  border-radius: 0;
  color: var(--muted);
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  transition: color .12s, background .12s;
}
.vote-btn:hover, .comment-btn:hover { background: var(--hover); }
.vote-btn svg, .comment-btn svg { display: block; }
.vote-btn.up:hover { color: var(--up); }
.vote-btn.down:hover { color: var(--down); }
.vote-btn.up.active { color: var(--up); }
.vote-btn.down.active { color: var(--down); }
.comment-btn:hover { color: var(--text); }

.score {
  min-width: 18px;
  padding: 0 2px;
  text-align: center;
  font-weight: 600;
  font-size: 12px;
  color: var(--muted);
}
.score.up { color: var(--up); }
.score.down { color: var(--down); }

.time {
  margin-left: auto;
  font-size: 12px;
  color: var(--muted);
}

/* ================= COMMENTS ================= */
.comments {
  margin-top: 12px;
  border-top: 1px solid var(--line);
}
.comment {
  padding: 10px 12px;
  margin-top: 8px;
  background: var(--comment-bg);
  border-left: 2px solid var(--line-strong);
}
.comment-text {
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: anywhere;
  color: var(--text);
}
.comment-actions {
  display: flex;
  align-items: center;
  gap: 1px;
  margin-top: 6px;
  font-size: 11px;
  color: var(--muted);
}
.comment-actions .vote-btn { height: 22px; padding: 0 6px; font-size: 11px; }
.comment-actions .score { font-size: 11px; min-width: 14px; }

/* ================= FOOTER ================= */
footer {
  flex: 0 0 auto;
  border-top: 1px solid var(--line);
  background: var(--card);
  padding: 10px 12px;
}

footer textarea {
  display: block;
  width: 100%;
  min-height: 62px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 0;
  background: transparent;
  color: var(--text);
  font-family: inherit;
  font-size: 14px;
  line-height: 1.45;
  outline: none;
  resize: none;
  transition: border-color .12s;
}
footer textarea:focus { border-color: var(--line-strong); }
footer textarea::placeholder { color: var(--muted); }

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
}

.counter {
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.counter.warn { color: var(--down); }

button.send {
  height: 32px;
  padding: 0 18px;
  background: var(--accent);
  color: var(--accent-fg);
  border: 1px solid var(--accent);
  border-radius: 0;
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity .12s;
}
button.send:hover { opacity: .82; }
button.send:disabled { opacity: .28; cursor: default; }
"""


# ------------------------------------------------------------------
# SVG иконки
# ------------------------------------------------------------------
ICON_SEARCH = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>'
)
ICON_MOON = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
)
ICON_SUN = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/>'
    '<line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/>'
    '<line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/>'
    '<line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/>'
    '<line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/></svg>'
)
ICON_BACK = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>'
)
ICON_UP = (
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="6 15 12 9 18 15"/></svg>'
)
ICON_DOWN = (
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="6 9 12 15 18 9"/></svg>'
)
ICON_COMMENT = (
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>'
)


# ------------------------------------------------------------------
# JS
# ------------------------------------------------------------------
JS = r"""
var ICON_UP = __ICON_UP__;
var ICON_DOWN = __ICON_DOWN__;
var ICON_COMMENT = __ICON_COMMENT__;
var ICON_MOON = __ICON_MOON__;
var ICON_SUN = __ICON_SUN__;

var feed = document.getElementById('feed');
var inputEl = document.getElementById('newPost');
var sendBtn = document.getElementById('send');
var counter = document.getElementById('counter');
var searchEl = document.getElementById('search');
var searchBtn = document.getElementById('searchBtn');
var themeBtn = document.getElementById('themeBtn');

var maxLen = (MODE === 'post') ? MAX_COMMENT_LEN : MAX_POST_LEN;

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

/* ---------- THEME ---------- */
function applyTheme(theme){
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('sldchat_theme', theme);
  if (themeBtn) themeBtn.innerHTML = (theme === 'dark') ? ICON_SUN : ICON_MOON;
}
function toggleTheme(){
  var cur = document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}
applyTheme(localStorage.getItem('sldchat_theme') || 'light');

/* ---------- COUNTER ---------- */
function updateCounter(){
  var len = inputEl.value.length;
  counter.textContent = len + ' / ' + maxLen;
  counter.classList.toggle('warn', len >= maxLen);
  sendBtn.disabled = len === 0 || len > maxLen;
}

/* ---------- RENDER ---------- */
function scoreClass(up, down){
  var s = up - down;
  if (s > 0) return 'up';
  if (s < 0) return 'down';
  return '';
}

function renderPost(p, showComments){
  var score = p.upvotes - p.downvotes;
  var upCls = p.user_vote === 1 ? 'active' : '';
  var downCls = p.user_vote === -1 ? 'active' : '';
  var scCls = scoreClass(p.upvotes, p.downvotes);

  var commentBtn = '<button class="comment-btn" data-action="comment" data-post-id="' + p.id + '">'
    + ICON_COMMENT + '<span>' + p.comments.length + '</span></button>';

  var commentsHtml = '';
  if (showComments && p.comments.length){
    commentsHtml = '<div class="comments">'
      + p.comments.map(function(c){ return renderComment(c, p.id); }).join('')
      + '</div>';
  }

  return ''
    + '<div class="post" data-post-id="' + p.id + '">'
    +   '<div class="post-text">' + escapeHtml(p.text) + '</div>'
    +   '<div class="post-actions">'
    +     '<button class="vote-btn up ' + upCls + '" data-action="vote" data-post-id="' + p.id + '" data-dir="1">' + ICON_UP + '</button>'
    +     '<span class="score ' + scCls + '">' + score + '</span>'
    +     '<button class="vote-btn down ' + downCls + '" data-action="vote" data-post-id="' + p.id + '" data-dir="-1">' + ICON_DOWN + '</button>'
    +     commentBtn
    +     '<span class="time">' + timeAgo(p.created_at) + '</span>'
    +   '</div>'
    +   commentsHtml
    + '</div>';
}

function renderComment(c, postId){
  var score = c.upvotes - c.downvotes;
  var upCls = c.user_vote === 1 ? 'active' : '';
  var downCls = c.user_vote === -1 ? 'active' : '';
  var scCls = scoreClass(c.upvotes, c.downvotes);
  return ''
    + '<div class="comment" data-comment-id="' + c.id + '">'
    +   '<div class="comment-text">' + escapeHtml(c.text) + '</div>'
    +   '<div class="comment-actions">'
    +     '<button class="vote-btn up ' + upCls + '" data-action="vote-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-dir="1">' + ICON_UP + '</button>'
    +     '<span class="score ' + scCls + '">' + score + '</span>'
    +     '<button class="vote-btn down ' + downCls + '" data-action="vote-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-dir="-1">' + ICON_DOWN + '</button>'
    +     '<span class="time">' + timeAgo(c.created_at) + '</span>'
    +   '</div>'
    + '</div>';
}

/* ---------- LOAD ---------- */
async function load(){
  try {
    if (MODE === 'post'){
      var r = await fetch('/api/posts/' + POST_ID + '?client_id=' + encodeURIComponent(CLIENT_ID));
      if (!r.ok){
        feed.innerHTML = '<div class="empty">' + escapeHtml(T.not_found) + '</div>';
        return;
      }
      var p = await r.json();
      feed.innerHTML = renderPost(p, true);
    } else {
      var q = (searchEl && searchEl.value || '').trim();
      var url = '/api/posts?q=' + encodeURIComponent(q) + '&client_id=' + encodeURIComponent(CLIENT_ID);
      var r2 = await fetch(url);
      var d2 = await r2.json();
      if (!d2.posts.length){
        feed.innerHTML = '<div class="empty">' + escapeHtml(T.no_posts) + '</div>';
        return;
      }
      feed.innerHTML = d2.posts.map(function(p){ return renderPost(p, false); }).join('');
    }
  } catch(e){
    feed.innerHTML = '<div class="empty">—</div>';
  }
}

/* ---------- SEND ---------- */
async function send(){
  var text = inputEl.value.trim();
  if (!text) return;
  sendBtn.disabled = true;
  try {
    if (MODE === 'post'){
      var r = await fetch('/api/posts/' + POST_ID + '/comments', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text, client_id: CLIENT_ID})
      });
      if (r.ok){
        inputEl.value = '';
        updateCounter();
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

/* ---------- CLICK HANDLERS ---------- */
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
    window.open('/p/' + postId, '_blank');
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

if (searchEl){
  var tId;
  searchEl.addEventListener('input', function(){
    clearTimeout(tId);
    tId = setTimeout(load, 250);
  });
  searchEl.addEventListener('keydown', function(e){
    if (e.key === 'Enter'){ e.preventDefault(); load(); }
  });
}
if (searchBtn){
  searchBtn.addEventListener('click', function(e){
    e.preventDefault();
    load();
  });
}
if (themeBtn){
  themeBtn.addEventListener('click', toggleTheme);
}

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
            f'<a href="/" class="icon-btn" title="{t["back"]}">{ICON_BACK}</a>'
            '<div class="spacer"></div>'
            f'<button id="themeBtn" class="icon-btn" title="{t["theme"]}">{ICON_MOON}</button>'
            '</header>'
        )
        textarea_placeholder = t["comment_ph"]
        send_label = t["send_comment"]
        textarea_max = MAX_COMMENT_LEN
    else:
        header = (
            '<header>'
            f'<input id="search" type="search" placeholder="{t["search_ph"]}" autocomplete="off" spellcheck="false" />'
            f'<button id="searchBtn" class="icon-btn" title="{t["search"]}">{ICON_SEARCH}</button>'
            f'<button id="themeBtn" class="icon-btn" title="{t["theme"]}">{ICON_MOON}</button>'
            '</header>'
        )
        textarea_placeholder = t["post_ph"]
        send_label = t["publish"]
        textarea_max = MAX_POST_LEN

    footer = (
        '<footer>'
        f'<textarea id="newPost" maxlength="{textarea_max}" placeholder="{textarea_placeholder}"></textarea>'
        '<div class="row">'
        f'<div id="counter" class="counter">0 / {textarea_max}</div>'
        f'<button id="send" class="send" disabled>{send_label}</button>'
        '</div>'
        '</footer>'
    )

    js = (JS
          .replace("__ICON_UP__", json.dumps(ICON_UP))
          .replace("__ICON_DOWN__", json.dumps(ICON_DOWN))
          .replace("__ICON_COMMENT__", json.dumps(ICON_COMMENT))
          .replace("__ICON_MOON__", json.dumps(ICON_MOON))
          .replace("__ICON_SUN__", json.dumps(ICON_SUN)))

    return (
        '<!DOCTYPE html>\n'
        f'<html lang="{lang}" data-theme="light">\n'
        '<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '<meta name="color-scheme" content="light dark" />\n'
        '<title>sldChat</title>\n'
        '<style>' + CSS + '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="app">\n'
        + header
        + '<main id="feed"><div class="empty">…</div></main>\n'
        + footer
        + '</div>\n'
        '<script>\n'
        f'const LANG = {json.dumps(lang)};\n'
        f'const MODE = {json.dumps(mode)};\n'
        f'const POST_ID = {json.dumps(post_id)};\n'
        f'const MAX_POST_LEN = {MAX_POST_LEN};\n'
        f'const MAX_COMMENT_LEN = {MAX_COMMENT_LEN};\n'
        f'const T = {json.dumps(t, ensure_ascii=False)};\n'
        + js +
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
