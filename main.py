# sldchat — single-file messenger
# deps: pip install fastapi uvicorn httpx pydantic
# env:  SUPABASE_URL, SUPABASE_KEY
# run:  python main.py   (или uvicorn main:app --host 0.0.0.0 --port 8000)

import asyncio
import hashlib
import os
import secrets
from typing import Optional

import httpx
import uvicorn
from fastapi import (Depends, FastAPI, HTTPException, Query, Request, Response,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# ------------------------------------------------------------------ config
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
REST = f"{SUPABASE_URL}/rest/v1"

http = httpx.AsyncClient(timeout=15.0)


def _h(prefer: Optional[str] = None) -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


async def sb_select(table: str, params: dict):
    r = await http.get(f"{REST}/{table}", headers=_h(), params=params)
    if r.status_code >= 300:
        raise HTTPException(500, f"db error: {r.text}")
    return r.json()


async def sb_insert(table: str, data):
    r = await http.post(f"{REST}/{table}", headers=_h("return=representation"), json=data)
    if r.status_code >= 300:
        raise HTTPException(500, f"db error: {r.text}")
    return r.json() if r.text else []


async def sb_delete(table: str, params: dict):
    r = await http.delete(f"{REST}/{table}", headers=_h(), params=params)
    if r.status_code >= 300:
        raise HTTPException(500, f"db error: {r.text}")
    return True


# --------------------------------------------------------------- passwords
def hash_password(pw: str, salt: Optional[str] = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${dk}"


def check_password(pw: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(pw, salt), stored)


# ------------------------------------------------------------------ auth
async def current_user(request: Request) -> dict:
    token = request.cookies.get("sldchat_token")
    if not token:
        raise HTTPException(401, "not authenticated")
    rows = await sb_select("sessions", {"token": f"eq.{token}", "select": "user_id"})
    if not rows:
        raise HTTPException(401, "session expired")
    uid = rows[0]["user_id"]
    users = await sb_select("users", {"id": f"eq.{uid}", "select": "id,username,display_name"})
    if not users:
        raise HTTPException(401, "user not found")
    return users[0]


# ------------------------------------------------------------- ws hub
class Hub:
    def __init__(self):
        self.conns: dict[str, set] = {}
        self.lock = asyncio.Lock()

    async def add(self, uid: str, ws: WebSocket):
        async with self.lock:
            self.conns.setdefault(uid, set()).add(ws)

    async def remove(self, uid: str, ws: WebSocket):
        async with self.lock:
            s = self.conns.get(uid)
            if s:
                s.discard(ws)
                if not s:
                    self.conns.pop(uid, None)

    async def send(self, uid: str, payload: dict):
        for ws in list(self.conns.get(uid, ())):
            try:
                await ws.send_json(payload)
            except Exception:
                pass

    async def send_many(self, uids, payload: dict):
        for u in uids:
            await self.send(u, payload)


hub = Hub()


# ------------------------------------------------------------------ models
class RegisterIn(BaseModel):
    username: str
    display_name: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class CreateChatIn(BaseModel):
    user_id: str


app = FastAPI(title="sldchat", docs_url=None, redoc_url=None)


# ------------------------------------------------------------------ routes
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/register")
async def register(body: RegisterIn, response: Response):
    username = body.username.strip().lower()
    display_name = body.display_name.strip()
    if not (3 <= len(username) <= 32) or not all(c.isalnum() or c == "_" for c in username):
        raise HTTPException(400, "invalid username")
    if not (1 <= len(display_name) <= 40):
        raise HTTPException(400, "invalid display name")
    if len(body.password) < 6:
        raise HTTPException(400, "password too short")

    existing = await sb_select("users", {"username": f"eq.{username}", "select": "id"})
    if existing:
        raise HTTPException(409, "username taken")

    rows = await sb_insert("users", {
        "username": username,
        "display_name": display_name,
        "password_hash": hash_password(body.password),
    })
    user = rows[0]
    token = secrets.token_urlsafe(32)
    await sb_insert("sessions", {"token": token, "user_id": user["id"]})
    response.set_cookie("sldchat_token", token, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
    return {"id": user["id"], "username": user["username"], "display_name": user["display_name"]}


@app.post("/api/login")
async def login(body: LoginIn, response: Response):
    username = body.username.strip().lower()
    rows = await sb_select("users", {
        "username": f"eq.{username}",
        "select": "id,username,display_name,password_hash",
    })
    if not rows or not check_password(body.password, rows[0]["password_hash"]):
        raise HTTPException(401, "invalid credentials")
    user = rows[0]
    token = secrets.token_urlsafe(32)
    await sb_insert("sessions", {"token": token, "user_id": user["id"]})
    response.set_cookie("sldchat_token", token, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
    return {"id": user["id"], "username": user["username"], "display_name": user["display_name"]}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("sldchat_token")
    if token:
        await sb_delete("sessions", {"token": f"eq.{token}"})
    response.delete_cookie("sldchat_token")
    return {"ok": True}


@app.get("/api/me")
async def me(user=Depends(current_user)):
    return user


@app.get("/api/users/search")
async def search_users(q: str = Query("", max_length=50), user=Depends(current_user)):
    q = q.strip()
    if len(q) < 1:
        return []
    q_esc = q.replace(",", "").replace("(", "").replace(")", "").replace("*", "")
    params = {
        "or": f"(username.ilike.*{q_esc}*,display_name.ilike.*{q_esc}*)",
        "select": "id,username,display_name",
        "limit": "20",
    }
    rows = await sb_select("users", params)
    return [r for r in rows if r["id"] != user["id"]]


@app.get("/api/chats")
async def list_chats(user=Depends(current_user)):
    me_id = user["id"]
    my_members = await sb_select("chat_members", {"user_id": f"eq.{me_id}", "select": "chat_id"})
    chat_ids = [m["chat_id"] for m in my_members]
    if not chat_ids:
        return []
    in_list = "(" + ",".join(chat_ids) + ")"

    chats = await sb_select("chats", {"id": f"in.{in_list}", "select": "id,is_group,title,created_at"})
    members = await sb_select("chat_members", {"chat_id": f"in.{in_list}", "select": "chat_id,user_id"})
    user_ids = list({m["user_id"] for m in members})
    users = await sb_select("users", {"id": f"in.({','.join(user_ids)})",
                                      "select": "id,username,display_name"})
    user_map = {u["id"]: u for u in users}

    members_by_chat: dict[str, list[str]] = {}
    for m in members:
        members_by_chat.setdefault(m["chat_id"], []).append(m["user_id"])

    async def last_msg(cid):
        r = await sb_select("messages", {
            "chat_id": f"eq.{cid}",
            "select": "text,created_at,user_id",
            "order": "created_at.desc",
            "limit": "1",
        })
        return cid, (r[0] if r else None)

    last_results = await asyncio.gather(*[last_msg(c) for c in chat_ids])
    last_map = dict(last_results)

    result = []
    for c in chats:
        cid = c["id"]
        member_ids = members_by_chat.get(cid, [])
        others = [user_map[uid] for uid in member_ids if uid != me_id and uid in user_map]
        if c["is_group"]:
            title = c.get("title") or "Group"
            uname = None
        else:
            title = others[0]["display_name"] if others else "Saved Messages"
            uname = others[0]["username"] if others else None
        lm = last_map.get(cid)
        result.append({
            "id": cid,
            "title": title,
            "username": uname,
            "is_group": c["is_group"],
            "last_message": (lm or {}).get("text", ""),
            "last_message_at": (lm or {}).get("created_at") or c["created_at"],
            "peer_id": others[0]["id"] if (others and not c["is_group"]) else None,
        })
    result.sort(key=lambda x: x["last_message_at"] or "", reverse=True)
    return result


@app.post("/api/chats")
async def create_chat(body: CreateChatIn, user=Depends(current_user)):
    me_id = user["id"]
    other = body.user_id
    if other == me_id:
        raise HTTPException(400, "cannot chat with yourself")
    ou = await sb_select("users", {"id": f"eq.{other}", "select": "id"})
    if not ou:
        raise HTTPException(404, "user not found")

    my_members = await sb_select("chat_members", {"user_id": f"eq.{me_id}", "select": "chat_id"})
    my_ids = [m["chat_id"] for m in my_members]
    if my_ids:
        shared = await sb_select("chat_members", {
            "chat_id": f"in.({','.join(my_ids)})",
            "user_id": f"eq.{other}",
            "select": "chat_id",
        })
        if shared:
            for s in shared:
                ch = await sb_select("chats", {"id": f"eq.{s['chat_id']}", "select": "is_group"})
                if ch and not ch[0]["is_group"]:
                    return {"chat_id": s["chat_id"]}

    ch = await sb_insert("chats", {"is_group": False})
    cid = ch[0]["id"]
    await sb_insert("chat_members", [
        {"chat_id": cid, "user_id": me_id},
        {"chat_id": cid, "user_id": other},
    ])
    await hub.send(other, {"type": "chat_created", "chat_id": cid})
    return {"chat_id": cid}


@app.get("/api/chats/{chat_id}/messages")
async def get_messages(chat_id: str, user=Depends(current_user)):
    m = await sb_select("chat_members", {
        "chat_id": f"eq.{chat_id}",
        "user_id": f"eq.{user['id']}",
        "select": "chat_id",
    })
    if not m:
        raise HTTPException(403, "not a member")
    msgs = await sb_select("messages", {
        "chat_id": f"eq.{chat_id}",
        "select": "id,user_id,text,created_at",
        "order": "created_at.asc",
        "limit": "500",
    })
    uids = list({x["user_id"] for x in msgs})
    user_map = {}
    if uids:
        users = await sb_select("users", {"id": f"in.({','.join(uids)})",
                                          "select": "id,username,display_name"})
        user_map = {u["id"]: u for u in users}
    for x in msgs:
        u = user_map.get(x["user_id"], {})
        x["username"] = u.get("username", "?")
        x["display_name"] = u.get("display_name", "?")
    return msgs


# ------------------------------------------------------------------ ws
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    token = ws.cookies.get("sldchat_token")
    if not token:
        await ws.close(code=4401)
        return
    rows = await sb_select("sessions", {"token": f"eq.{token}", "select": "user_id"})
    if not rows:
        await ws.close(code=4401)
        return
    uid = rows[0]["user_id"]
    users = await sb_select("users", {"id": f"eq.{uid}", "select": "id,username,display_name"})
    if not users:
        await ws.close(code=4401)
        return
    user = users[0]

    await ws.accept()
    await hub.add(uid, ws)
    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type")

            if t == "message":
                chat_id = data.get("chat_id")
                text = (data.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                text = text[:4000]
                m = await sb_select("chat_members", {
                    "chat_id": f"eq.{chat_id}",
                    "user_id": f"eq.{uid}",
                    "select": "chat_id",
                })
                if not m:
                    continue
                rows = await sb_insert("messages", {
                    "chat_id": chat_id, "user_id": uid, "text": text,
                })
                msg = rows[0]
                msg["username"] = user["username"]
                msg["display_name"] = user["display_name"]
                members = await sb_select("chat_members", {
                    "chat_id": f"eq.{chat_id}", "select": "user_id",
                })
                await hub.send_many([x["user_id"] for x in members],
                                    {"type": "message", "message": msg})

            elif t == "typing":
                chat_id = data.get("chat_id")
                if not chat_id:
                    continue
                members = await sb_select("chat_members", {
                    "chat_id": f"eq.{chat_id}", "select": "user_id",
                })
                for m in members:
                    if m["user_id"] != uid:
                        await hub.send(m["user_id"], {
                            "type": "typing",
                            "chat_id": chat_id,
                            "username": user["username"],
                            "display_name": user["display_name"],
                        })

            elif t == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("ws error:", e)
    finally:
        await hub.remove(uid, ws)


# ------------------------------------------------------------------ HTML
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>sldchat</title>
<style>
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 14px; color: #222; background: #e6ebee; overflow: hidden;
}
button { font: inherit; cursor: pointer; }
input, textarea { font: inherit; }

/* ---------- Login ---------- */
#login {
  position: fixed; inset: 0;
  display: flex; align-items: center; justify-content: center;
  background: linear-gradient(180deg, #5a8bb5 0%, #4a7ba3 100%);
}
.login-card {
  background: #fff; width: 340px; padding: 26px 24px 20px;
  border-radius: 4px; box-shadow: 0 8px 32px rgba(0,0,0,0.25);
}
.login-card h1 { margin: 0; font-size: 24px; color: #517da2; font-weight: 500; letter-spacing: .5px; }
.login-card .sub { color: #8a939b; font-size: 12px; margin: 2px 0 20px; }
.login-card input {
  width: 100%; padding: 10px 12px; margin-bottom: 10px;
  border: 1px solid #d0d7de; border-radius: 3px; outline: none;
  background: #fff; transition: border-color .15s;
}
.login-card input:focus { border-color: #517da2; }
.login-card button.primary {
  width: 100%; padding: 10px; background: #517da2; color: #fff;
  border: none; border-radius: 3px; font-weight: 500;
}
.login-card button.primary:hover { background: #46698a; }
.login-toggle { text-align: center; margin-top: 14px; font-size: 12px; color: #8a939b; }
.login-toggle a { color: #517da2; cursor: pointer; text-decoration: none; }
.login-error { color: #d9534f; font-size: 12px; margin-bottom: 10px; min-height: 14px; }

/* ---------- App ---------- */
#app { display: none; height: 100vh; }
.app-layout { display: flex; height: 100%; }

/* ---------- Sidebar ---------- */
.sidebar {
  width: 320px; min-width: 320px; background: #fff;
  border-right: 1px solid #dfe4e8; display: flex; flex-direction: column;
}
.sidebar-head {
  display: flex; align-items: center; padding: 8px 12px;
  background: #517da2; color: #fff; gap: 8px;
}
.sidebar-head .title { flex: 1; font-weight: 600; font-size: 15px; letter-spacing: .4px; }
.icon-btn {
  width: 30px; height: 30px; border-radius: 3px; border: none;
  background: transparent; color: inherit;
  display: inline-flex; align-items: center; justify-content: center;
}
.icon-btn:hover { background: rgba(255,255,255,0.16); }
.icon-btn svg { width: 18px; height: 18px; }

.search-wrap { position: relative; padding: 8px; background: #fff; border-bottom: 1px solid #eef1f3; }
.search-icon { position: absolute; left: 17px; top: 50%; transform: translateY(-50%);
  width: 14px; height: 14px; color: #9aa3ab; pointer-events: none; }
.search-wrap input {
  width: 100%; padding: 7px 10px 7px 30px;
  border: 1px solid #d7dde2; border-radius: 3px; outline: none; background: #fff;
}
.search-wrap input:focus { border-color: #517da2; }

.chat-list { flex: 1; overflow-y: auto; }
.chat-item {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 12px; cursor: pointer; border-bottom: 1px solid #f2f4f6;
}
.chat-item:hover { background: #f5f8fa; }
.chat-item.active { background: #e9f0f5; }
.avatar {
  width: 42px; height: 42px; min-width: 42px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 600; font-size: 16px; user-select: none;
}
.chat-item-body { flex: 1; min-width: 0; }
.chat-item-title { font-weight: 500; color: #222; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chat-item-sub { color: #8a939b; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
.list-section {
  padding: 8px 12px; font-size: 11px; text-transform: uppercase;
  color: #8a939b; font-weight: 600; letter-spacing: .5px; background: #f8fafb;
}
.empty { padding: 24px; text-align: center; color: #9aa3ab; font-size: 13px; }

/* ---------- Main ---------- */
.main { flex: 1; display: flex; flex-direction: column; background: #e6ebee; min-width: 0; }
.main-header {
  height: 48px; padding: 0 16px; background: #517da2; color: #fff;
  display: flex; align-items: center; gap: 10px;
}
.main-header .chat-title { font-weight: 600; font-size: 15px; }
.main-header .chat-status { font-size: 12px; opacity: .85; margin-left: auto; }

.messages {
  flex: 1; overflow-y: auto; padding: 14px 18px;
  display: flex; flex-direction: column;
}
.no-chat { margin: auto; color: #7f8a93; font-size: 14px; text-align: center; }
.date-sep {
  align-self: center; background: rgba(255,255,255,.75); color: #6b7680;
  font-size: 11px; padding: 3px 10px; border-radius: 10px; margin: 10px 0;
}
.msg { display: flex; margin: 1px 0; }
.msg.out { justify-content: flex-end; }
.bubble {
  max-width: 62%; padding: 6px 12px 18px; border-radius: 6px;
  background: #fff; box-shadow: 0 1px 1px rgba(0,0,0,.05);
  position: relative; word-wrap: break-word; white-space: pre-wrap;
  line-height: 1.35; margin: 1px 0;
}
.msg.out .bubble { background: #e1f2f7; }
.bubble .author { font-size: 12px; font-weight: 600; color: #517da2; margin-bottom: 2px; }
.bubble .time { position: absolute; right: 8px; bottom: 4px; font-size: 11px; color: #9aa3ab; }
.bubble .text { font-size: 14px; color: #222; }

.composer {
  display: flex; align-items: flex-end; gap: 8px;
  padding: 8px 12px; background: #fff; border-top: 1px solid #dfe4e8;
}
.composer textarea {
  flex: 1; resize: none; border: none; outline: none;
  padding: 8px 4px; max-height: 120px; min-height: 22px;
  background: transparent; font-size: 14px; line-height: 1.35;
}
.composer .send-btn {
  width: 36px; height: 36px; border-radius: 50%; border: none;
  background: #517da2; color: #fff;
  display: inline-flex; align-items: center; justify-content: center;
}
.composer .send-btn:hover { background: #46698a; }
.composer .send-btn:disabled { background: #c3ccd3; cursor: default; }
.composer .send-btn svg { width: 18px; height: 18px; margin-left: -2px; }

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: rgba(0,0,0,.15); border-radius: 4px; }
::-webkit-scrollbar-track { background: transparent; }
</style>
</head>
<body>

<div id="login">
  <div class="login-card">
    <h1>sldchat</h1>
    <div class="sub">Simple. Fast. Yours.</div>
    <div class="login-error" id="login-error"></div>
    <input id="in-username" placeholder="Username" autocomplete="username">
    <input id="in-display" placeholder="Display name" autocomplete="off" style="display:none">
    <input id="in-password" type="password" placeholder="Password" autocomplete="current-password">
    <button class="primary" id="btn-auth">Sign in</button>
    <div class="login-toggle">
      <span id="toggle-text">No account?</span>
      <a id="toggle-link">Sign up</a>
    </div>
  </div>
</div>

<div id="app">
  <div class="app-layout">
    <aside class="sidebar">
      <div class="sidebar-head">
        <div class="title">sldchat</div>
        <button class="icon-btn" id="btn-logout" title="Log out">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
        </button>
      </div>
      <div class="search-wrap">
        <div class="search-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.35-4.35"/></svg>
        </div>
        <input id="search" placeholder="Search">
      </div>
      <div class="chat-list" id="chat-list"></div>
    </aside>
    <main class="main">
      <div class="main-header">
        <div class="chat-title" id="chat-title">sldchat</div>
        <div class="chat-status" id="chat-status"></div>
      </div>
      <div class="messages" id="messages">
        <div class="no-chat">Select a chat to start messaging</div>
      </div>
      <div class="composer">
        <textarea id="input" placeholder="Write a message..." rows="1" disabled></textarea>
        <button class="send-btn" id="send" disabled>
          <svg viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
        </button>
      </div>
    </main>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let me = null;
let ws = null;
let chats = [];
let currentChatId = null;
let currentChat = null;
let messages = {};
let authMode = 'login';
let typingTimer = null;
let typingHideTimer = null;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function initials(name) {
  if (!name) return '?';
  const p = name.trim().split(/\s+/);
  return (((p[0] && p[0][0]) || '') + ((p[1] && p[1][0]) || '')).toUpperCase() || '?';
}
function avatarColor(id) {
  const colors = ['#e17076','#7bc862','#e5ca77','#65aadd','#a695e7','#ee7aae','#6ec9cb','#faa774'];
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return colors[h % colors.length];
}
async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {'Content-Type':'application/json'}}, opts || {}));
  if (!r.ok) {
    let msg = 'Request failed';
    try { msg = (await r.json()).detail || msg; } catch(_) {}
    throw new Error(msg);
  }
  if (r.status === 204) return null;
  return r.json();
}

// -------- auth --------
function setAuthMode(mode) {
  authMode = mode;
  if (mode === 'login') {
    $('in-display').style.display = 'none';
    $('btn-auth').textContent = 'Sign in';
    $('toggle-text').textContent = 'No account?';
    $('toggle-link').textContent = 'Sign up';
    $('in-password').setAttribute('autocomplete','current-password');
  } else {
    $('in-display').style.display = '';
    $('btn-auth').textContent = 'Create account';
    $('toggle-text').textContent = 'Already have an account?';
    $('toggle-link').textContent = 'Sign in';
    $('in-password').setAttribute('autocomplete','new-password');
  }
  $('login-error').textContent = '';
}
$('toggle-link').onclick = () => setAuthMode(authMode === 'login' ? 'register' : 'login');
$('btn-auth').onclick = async () => {
  const username = $('in-username').value.trim();
  const password = $('in-password').value;
  const display = $('in-display').value.trim();
  $('login-error').textContent = '';
  try {
    let user;
    if (authMode === 'login') {
      user = await api('/api/login', {method:'POST', body: JSON.stringify({username, password})});
    } else {
      user = await api('/api/register', {method:'POST', body: JSON.stringify({username, display_name: display, password})});
    }
    me = user;
    startApp();
  } catch(e) { $('login-error').textContent = e.message; }
};
$('in-password').addEventListener('keydown', e => { if (e.key === 'Enter') $('btn-auth').click(); });
$('btn-logout').onclick = async () => {
  try { await api('/api/logout', {method:'POST'}); } catch(_) {}
  location.reload();
};

// -------- app --------
async function startApp() {
  $('login').style.display = 'none';
  $('app').style.display = 'block';
  connectWS();
  await loadChats();
  $('search').addEventListener('input', onSearch);
  $('send').onclick = sendMessage;
  const inp = $('input');
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  inp.addEventListener('input', () => {
    inp.style.height = 'auto';
    inp.style.height = Math.min(inp.scrollHeight, 120) + 'px';
    if (currentChatId && ws && ws.readyState === 1 && !typingTimer) {
      ws.send(JSON.stringify({type: 'typing', chat_id: currentChatId}));
      typingTimer = setTimeout(() => typingTimer = null, 2500);
    }
  });
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onmessage = e => {
    let d; try { d = JSON.parse(e.data); } catch(_) { return; }
    if (d.type === 'message') handleIncoming(d.message);
    else if (d.type === 'chat_created') loadChats();
    else if (d.type === 'typing') showTyping(d);
  };
  ws.onclose = () => setTimeout(connectWS, 1500);
}

function showTyping(d) {
  if (d.chat_id !== currentChatId) return;
  const el = $('chat-status');
  el.textContent = (d.display_name || d.username) + ' is typing…';
  clearTimeout(typingHideTimer);
  typingHideTimer = setTimeout(() => { el.textContent = ''; }, 3000);
}

function handleIncoming(msg) {
  const cid = msg.chat_id;
  if (!messages[cid]) messages[cid] = [];
  if (!messages[cid].some(m => m.id === msg.id)) messages[cid].push(msg);
  if (cid === currentChatId) { appendMessage(msg); scrollBottom(); }
  const c = chats.find(x => x.id === cid);
  if (c) {
    c.last_message = msg.text;
    c.last_message_at = msg.created_at;
    chats.sort((a,b) => (b.last_message_at||'').localeCompare(a.last_message_at||''));
    renderChatList(chats);
  } else {
    loadChats();
  }
}

async function loadChats() {
  chats = await api('/api/chats');
  renderChatList(chats);
}

function renderChatList(list) {
  const el = $('chat-list');
  el.innerHTML = '';
  if (!list.length) {
    el.innerHTML = '<div class="empty">No chats yet.<br>Search for a user to start.</div>';
    return;
  }
  list.forEach(c => el.appendChild(chatItem(c)));
}

function chatItem(c) {
  const div = document.createElement('div');
  div.className = 'chat-item' + (c.id === currentChatId ? ' active' : '');
  const sub = c.last_message
    ? (c.last_message.length > 44 ? c.last_message.slice(0, 44) + '…' : c.last_message)
    : (c.username ? '@' + c.username : '');
  div.innerHTML =
    '<div class="avatar" style="background:' + avatarColor(c.id) + '">' + escapeHtml(initials(c.title)) + '</div>' +
    '<div class="chat-item-body">' +
      '<div class="chat-item-title">' + escapeHtml(c.title) + '</div>' +
      '<div class="chat-item-sub">' + escapeHtml(sub) + '</div>' +
    '</div>';
  div.onclick = () => openChat(c.id);
  return div;
}

async function onSearch() {
  const q = $('search').value.trim();
  const el = $('chat-list');
  if (!q) { renderChatList(chats); return; }
  const filtered = chats.filter(c =>
    c.title.toLowerCase().includes(q.toLowerCase()) ||
    (c.username || '').toLowerCase().includes(q.toLowerCase())
  );
  el.innerHTML = '';
  if (filtered.length) {
    const h = document.createElement('div'); h.className = 'list-section'; h.textContent = 'Chats';
    el.appendChild(h);
    filtered.forEach(c => el.appendChild(chatItem(c)));
  }
  try {
    const users = await api('/api/users/search?q=' + encodeURIComponent(q));
    if (users.length) {
      const h = document.createElement('div'); h.className = 'list-section'; h.textContent = 'Users';
      el.appendChild(h);
      users.forEach(u => {
        const d = document.createElement('div');
        d.className = 'chat-item';
        d.innerHTML =
          '<div class="avatar" style="background:' + avatarColor(u.id) + '">' + escapeHtml(initials(u.display_name)) + '</div>' +
          '<div class="chat-item-body">' +
            '<div class="chat-item-title">' + escapeHtml(u.display_name) + '</div>' +
            '<div class="chat-item-sub">@' + escapeHtml(u.username) + '</div>' +
          '</div>';
        d.onclick = () => startChat(u.id);
        el.appendChild(d);
      });
    }
  } catch(_) {}
  if (!el.children.length) el.innerHTML = '<div class="empty">Nothing found</div>';
}

async function startChat(userId) {
  try {
    const r = await api('/api/chats', {method:'POST', body: JSON.stringify({user_id: userId})});
    $('search').value = '';
    await loadChats();
    openChat(r.chat_id);
  } catch(e) { alert(e.message); }
}

async function openChat(cid) {
  currentChatId = cid;
  currentChat = chats.find(x => x.id === cid) || null;
  $('chat-title').textContent = currentChat ? currentChat.title : '';
  $('chat-status').textContent = '';
  $('input').disabled = false;
  $('send').disabled = false;
  $('input').focus();
  renderChatList(chats);
  if (!messages[cid]) {
    messages[cid] = await api('/api/chats/' + cid + '/messages');
  }
  renderMessages();
}

function renderMessages() {
  const el = $('messages');
  el.innerHTML = '';
  const list = messages[currentChatId] || [];
  if (!list.length) {
    el.innerHTML = '<div class="no-chat">No messages yet. Say hi!</div>';
    return;
  }
  let lastDate = '';
  list.forEach(m => {
    const d = new Date(m.created_at).toDateString();
    if (d !== lastDate) {
      lastDate = d;
      const sep = document.createElement('div');
      sep.className = 'date-sep';
      sep.textContent = fmtDate(m.created_at);
      el.appendChild(sep);
    }
    el.appendChild(msgEl(m));
  });
  scrollBottom();
}

function appendMessage(m) {
  const el = $('messages');
  const emptyEl = el.querySelector('.no-chat');
  if (emptyEl) emptyEl.remove();
  const list = messages[currentChatId] || [];
  const prev = list[list.length - 1];
  const needSep = !prev || new Date(prev.created_at).toDateString() !== new Date(m.created_at).toDateString();
  if (needSep) {
    const s = document.createElement('div');
    s.className = 'date-sep';
    s.textContent = fmtDate(m.created_at);
    el.appendChild(s);
  }
  el.appendChild(msgEl(m));
}

function msgEl(m) {
  const out = m.user_id === me.id;
  const div = document.createElement('div');
  div.className = 'msg ' + (out ? 'out' : 'in');
  let author = '';
  if (!out && currentChat && currentChat.is_group) {
    author = '<div class="author">' + escapeHtml(m.display_name || m.username) + '</div>';
  }
  div.innerHTML =
    '<div class="bubble">' + author +
      '<div class="text">' + escapeHtml(m.text) + '</div>' +
      '<div class="time">' + fmtTime(m.created_at) + '</div>' +
    '</div>';
  return div;
}

function scrollBottom() {
  const el = $('messages');
  el.scrollTop = el.scrollHeight;
}

function fmtTime(iso) {
  const d = new Date(iso);
  return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}
function fmtDate(iso) {
  const d = new Date(iso);
  const today = new Date();
  const yest = new Date(); yest.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === yest.toDateString()) return 'Yesterday';
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return d.getDate() + ' ' + months[d.getMonth()] +
         (d.getFullYear() !== today.getFullYear() ? ' ' + d.getFullYear() : '');
}

function sendMessage() {
  const inp = $('input');
  const text = inp.value.trim();
  if (!text || !currentChatId || !ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({type:'message', chat_id: currentChatId, text}));
  inp.value = '';
  inp.style.height = 'auto';
  typingTimer = null;
}

// -------- boot --------
(async () => {
  try {
    me = await api('/api/me');
    startApp();
  } catch(_) {
    setAuthMode('login');
  }
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
