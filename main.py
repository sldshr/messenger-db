# main.py
# Запуск: python main.py
# Установка: pip install fastapi uvicorn

import time
import uuid
from typing import Optional

from fastapi import (
    FastAPI, HTTPException, Header, Depends, WebSocket, WebSocketDisconnect, Query,
)
from fastapi.responses import HTMLResponse, Response, JSONResponse
from pydantic import BaseModel
import uvicorn


app = FastAPI(title="Messenger")

# ==================================================================
#                         ХРАНИЛИЩЕ
# ==================================================================
users: dict = {}         # username -> {"password": str}
tokens: dict = {}        # token -> username
messages: list = []      # {"id","from","to","text","ts","read"}
friends: dict = {}       # user -> set(user)
requests: dict = {}      # from_user -> set(to_user)  (исходящие заявки)


# ==================================================================
#                         ХЕЛПЕРЫ
# ==================================================================
def are_friends(a: str, b: str) -> bool:
    return b in friends.get(a, set())


def add_friends(a: str, b: str):
    friends.setdefault(a, set()).add(b)
    friends.setdefault(b, set()).add(a)


def remove_friends(a: str, b: str):
    friends.get(a, set()).discard(b)
    friends.get(b, set()).discard(a)


def unread_count(user: str, peer: str) -> int:
    return sum(1 for m in messages if m["from"] == peer and m["to"] == user and not m["read"])


class WSManager:
    def __init__(self):
        self.conns: dict = {}

    async def add(self, user: str, ws: WebSocket):
        old = self.conns.get(user)
        self.conns[user] = ws
        if old is not None and old is not ws:
            try:
                await old.close(code=4000)
            except Exception:
                pass

    async def remove(self, user: str, ws: WebSocket):
        if self.conns.get(user) is ws:
            self.conns.pop(user, None)

    async def send(self, user: str, data: dict) -> bool:
        ws = self.conns.get(user)
        if ws is None:
            return False
        try:
            await ws.send_json(data)
            return True
        except Exception:
            return False


manager = WSManager()


# ==================================================================
#                           МОДЕЛИ
# ==================================================================
class AuthData(BaseModel):
    username: str
    password: str


class TargetData(BaseModel):
    target: str


def current_user(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Не авторизован")
    t = authorization[7:].strip()
    u = tokens.get(t)
    if not u:
        raise HTTPException(401, "Сессия истекла")
    return u


# ==================================================================
#                       АВТОРИЗАЦИЯ
# ==================================================================
@app.post("/api/register")
async def register(data: AuthData):
    u = data.username.strip()
    if not (2 <= len(u) <= 20):
        raise HTTPException(400, "Имя от 2 до 20 символов")
    if not all(c.isalnum() or c in "._-" for c in u):
        raise HTTPException(400, "Только буквы, цифры и . _ -")
    if len(data.password) < 3:
        raise HTTPException(400, "Пароль минимум 3 символа")
    if u in users:
        raise HTTPException(400, "Такое имя занято")
    users[u] = {"password": data.password}
    token = uuid.uuid4().hex
    tokens[token] = u
    return {"token": token, "username": u}


@app.post("/api/login")
async def login(data: AuthData):
    u = data.username.strip()
    if u not in users or users[u]["password"] != data.password:
        raise HTTPException(400, "Неверное имя или пароль")
    token = uuid.uuid4().hex
    tokens[token] = u
    return {"token": token, "username": u}


# ==================================================================
#                    ПОЛЬЗОВАТЕЛИ / ДРУЗЬЯ
# ==================================================================
@app.get("/api/users")
async def search_users(q: str = "", user: str = Depends(current_user)):
    q = q.strip().lower()
    out = []
    for name in users:
        if name == user:
            continue
        if q and q not in name.lower():
            continue
        if are_friends(user, name):
            status = "friend"
        elif name in requests.get(user, set()):
            status = "outgoing"
        elif user in requests.get(name, set()):
            status = "incoming"
        else:
            status = "none"
        out.append({"username": name, "status": status})
    out.sort(key=lambda x: x["username"].lower())
    return out[:100]


@app.get("/api/friends")
async def get_friends(user: str = Depends(current_user)):
    fl = sorted(friends.get(user, set()), key=str.lower)
    incoming = sorted([f for f, s in requests.items() if user in s], key=str.lower)
    outgoing = sorted(list(requests.get(user, set())), key=str.lower)
    return {"friends": fl, "incoming": incoming, "outgoing": outgoing}


@app.post("/api/friends/request")
async def friend_request(data: TargetData, user: str = Depends(current_user)):
    t = data.target
    if t == user or t not in users:
        raise HTTPException(400, "Пользователь не найден")
    if are_friends(user, t):
        raise HTTPException(400, "Уже друзья")
    if user in requests.get(t, set()):
        # встречная заявка — сразу друзья
        requests[t].discard(user)
        add_friends(user, t)
        await manager.send(t, {"type": "friends_changed"})
        await manager.send(user, {"type": "friends_changed"})
        return {"ok": True, "status": "friend"}
    requests.setdefault(user, set()).add(t)
    await manager.send(t, {"type": "friends_changed"})
    return {"ok": True, "status": "outgoing"}


@app.post("/api/friends/accept")
async def friend_accept(data: TargetData, user: str = Depends(current_user)):
    f = data.target
    if user not in requests.get(f, set()):
        raise HTTPException(400, "Нет запроса")
    requests[f].discard(user)
    add_friends(user, f)
    await manager.send(f, {"type": "friends_changed"})
    await manager.send(user, {"type": "friends_changed"})
    return {"ok": True}


@app.post("/api/friends/decline")
async def friend_decline(data: TargetData, user: str = Depends(current_user)):
    f = data.target
    if user in requests.get(f, set()):
        requests[f].discard(user)
    await manager.send(f, {"type": "friends_changed"})
    return {"ok": True}


@app.post("/api/friends/cancel")
async def friend_cancel(data: TargetData, user: str = Depends(current_user)):
    t = data.target
    requests.get(user, set()).discard(t)
    await manager.send(t, {"type": "friends_changed"})
    return {"ok": True}


@app.post("/api/friends/remove")
async def friend_remove(data: TargetData, user: str = Depends(current_user)):
    t = data.target
    remove_friends(user, t)
    await manager.send(t, {"type": "friends_changed"})
    await manager.send(user, {"type": "friends_changed"})
    return {"ok": True}


# ==================================================================
#                        ЧАТЫ / СООБЩЕНИЯ
# ==================================================================
@app.get("/api/chats")
async def get_chats(user: str = Depends(current_user)):
    result = []
    for f in friends.get(user, set()):
        last = None
        for m in reversed(messages):
            if (m["from"] == user and m["to"] == f) or (m["from"] == f and m["to"] == user):
                last = m
                break
        result.append({
            "username": f,
            "unread": unread_count(user, f),
            "lastText": (last["text"] if last else ""),
            "lastTs": (last["ts"] if last else 0),
            "lastFromMe": bool(last and last["from"] == user),
        })
    result.sort(key=lambda x: (-x["lastTs"], x["username"].lower()))
    return result


@app.get("/api/messages/{peer}")
async def get_messages(peer: str, user: str = Depends(current_user)):
    if not are_friends(user, peer):
        raise HTTPException(403, "Не друзья")
    out = []
    changed = False
    for m in messages:
        if m["from"] == user and m["to"] == peer:
            out.append({"id": m["id"], "from": m["from"], "text": m["text"], "ts": m["ts"]})
        elif m["from"] == peer and m["to"] == user:
            if not m["read"]:
                m["read"] = True
                changed = True
            out.append({"id": m["id"], "from": m["from"], "text": m["text"], "ts": m["ts"]})
    if changed:
        await manager.send(peer, {"type": "chat_read", "peer": user})
    return out


# ==================================================================
#                          WEBSOCKET
# ==================================================================
async def handle_ws(username: str, data: dict):
    t = data.get("type")
    if t == "send":
        to = (data.get("to") or "").strip()
        text = (data.get("text") or "").strip()
        if not text or len(text) > 2000:
            return
        if not to or not are_friends(username, to):
            return
        msg = {
            "id": uuid.uuid4().hex,
            "from": username,
            "to": to,
            "text": text,
            "ts": time.time(),
            "read": False,
        }
        messages.append(msg)
        payload = {"type": "message", **msg}
        await manager.send(to, payload)
        await manager.send(username, payload)
    elif t == "read":
        peer = (data.get("peer") or "").strip()
        if not peer:
            return
        changed = False
        for m in messages:
            if m["from"] == peer and m["to"] == username and not m["read"]:
                m["read"] = True
                changed = True
        if changed:
            await manager.send(peer, {"type": "chat_read", "peer": username})
    elif t == "typing":
        to = (data.get("to") or "").strip()
        if to and are_friends(username, to):
            await manager.send(to, {"type": "typing", "from": username})


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = Query("")):
    username = tokens.get(token)
    if not username:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await manager.add(username, websocket)
    try:
        await websocket.send_json({"type": "hello", "username": username})
        while True:
            data = await websocket.receive_json()
            if isinstance(data, dict):
                await handle_ws(username, data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await manager.remove(username, websocket)


# ==================================================================
#                     PWA / SERVICE WORKER / ИКОНКА
# ==================================================================
SW_JS = r"""
const CACHE = 'msgr-v1';
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.pathname === '/ws') return;
  e.respondWith((async () => {
    try {
      const fresh = await fetch(req);
      if (fresh && fresh.status === 200 && url.origin === location.origin) {
        const clone = fresh.clone();
        caches.open(CACHE).then(c => c.put(req, clone)).catch(()=>{});
      }
      return fresh;
    } catch (err) {
      const cached = await caches.match(req);
      if (cached) return cached;
      throw err;
    }
  })());
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil((async () => {
    const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of list) {
      if ('focus' in c) { await c.focus(); return; }
    }
    if (self.clients.openWindow) return self.clients.openWindow('/');
  })());
});
"""


@app.get("/sw.js")
async def sw():
    return Response(SW_JS, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "name": "Мессенджер",
        "short_name": "Мессенджер",
        "description": "Простой мессенджер",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}
        ],
    }, headers={"Cache-Control": "no-cache"})


@app.get("/icon.svg")
async def icon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        '<rect width="512" height="512" rx="112" fill="#111"/>'
        '<path d="M400 340a34 34 0 0 1-34 34H160l-72 72V106a34 34 0 0 1 34-34h244'
        'a34 34 0 0 1 34 34z" fill="none" stroke="#fff" stroke-width="28" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# ==================================================================
#                         ФРОНТЕНД
# ==================================================================
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Мессенджер">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon.svg">
<title>Мессенджер</title>
<style>
:root{
  --bg:#ffffff;
  --bg-elev:#f4f4f5;
  --bg-soft:#fafafa;
  --text:#111111;
  --text-dim:#8a8a8e;
  --border:#ececec;
  --accent:#111111;
  --accent-fg:#ffffff;
  --bubble-me:#111111;
  --bubble-me-fg:#ffffff;
  --bubble-them:#f2f2f3;
  --bubble-them-fg:#111111;
  --danger:#e11d48;
  --ok:#16a34a;
  --glass-bg:rgba(255,255,255,.72);
  --glass-border:rgba(255,255,255,.5);
  --shadow:0 8px 24px rgba(0,0,0,.06);
}
[data-theme="dark"]{
  --bg:#0b0b0c;
  --bg-elev:#17171a;
  --bg-soft:#101013;
  --text:#f5f5f7;
  --text-dim:#8e8e93;
  --border:#232326;
  --accent:#ffffff;
  --accent-fg:#111111;
  --bubble-me:#ffffff;
  --bubble-me-fg:#111111;
  --bubble-them:#1d1d20;
  --bubble-them-fg:#f5f5f7;
  --danger:#ff5c7a;
  --ok:#34d058;
  --glass-bg:rgba(15,15,17,.66);
  --glass-border:rgba(255,255,255,.08);
  --shadow:0 8px 24px rgba(0,0,0,.4);
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{
  height:100%;width:100%;
  background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
  font-size:17px;-webkit-font-smoothing:antialiased;
  overscroll-behavior:none;
}
body{
  position:fixed;top:0;left:0;right:0;bottom:0;
  overflow:hidden;
}
#app{
  position:fixed;top:0;left:0;right:0;
  height:var(--app-h,100dvh);
  display:block;overflow:hidden;
  background:var(--bg);
}
.screen{
  position:absolute;inset:0;display:none;flex-direction:column;
  overflow:hidden;background:var(--bg);
}
.screen.active{display:flex}

/* ---------- Общая шапка ---------- */
.topbar{
  flex-shrink:0;
  display:flex;align-items:center;gap:6px;
  padding:8px 8px;
  padding-top:calc(8px + env(safe-area-inset-top,0));
  border-bottom:1px solid var(--border);
  background:var(--bg);
  min-height:56px;
  z-index:5;
}
.topbar .title{
  flex:1;font-size:20px;font-weight:700;letter-spacing:-.3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.topbar .title.center{text-align:center;font-size:18px;font-weight:600}
.icon-btn{
  width:44px;height:44px;flex-shrink:0;border:none;background:transparent;
  display:flex;align-items:center;justify-content:center;
  border-radius:12px;cursor:pointer;color:var(--text);
  transition:background .15s, transform .1s;
}
.icon-btn:active{background:var(--bg-elev);transform:scale(.94)}
.icon-btn svg{width:22px;height:22px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

/* ---------- Glass ---------- */
[data-glass="on"] .glass{
  background:var(--glass-bg) !important;
  backdrop-filter:blur(22px) saturate(180%);
  -webkit-backdrop-filter:blur(22px) saturate(180%);
  border-color:var(--glass-border) !important;
}

/* ---------- Bottom nav ---------- */
.bottom-nav{
  flex-shrink:0;display:flex;
  padding:6px 6px;
  padding-bottom:calc(6px + env(safe-area-inset-bottom,0));
  border-top:1px solid var(--border);
  background:var(--bg);
  z-index:5;
}
.nav-btn{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;padding:8px 4px;background:transparent;border:none;cursor:pointer;
  color:var(--text-dim);font-family:inherit;font-size:11.5px;font-weight:600;
  border-radius:14px;transition:color .15s,background .15s,transform .1s;
  position:relative;
}
.nav-btn:active{transform:scale(.95)}
.nav-btn.active{color:var(--text)}
.nav-btn svg{width:24px;height:24px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.nav-btn .dot{
  position:absolute;top:6px;right:calc(50% - 18px);
  min-width:16px;height:16px;padding:0 4px;border-radius:8px;
  background:#ef4444;color:#fff;font-size:10px;font-weight:700;
  display:flex;align-items:center;justify-content:center;
}

/* ---------- Page area ---------- */
.page-area{flex:1;overflow:hidden;position:relative}
.page{
  position:absolute;inset:0;display:none;flex-direction:column;overflow:hidden;
}
.page.active{display:flex}
.page-scroll{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}

/* ---------- Segmented control ---------- */
.segmented{
  display:flex;background:var(--bg-elev);border-radius:12px;
  padding:4px;margin:12px 16px 6px;
}
.seg{
  flex:1;padding:10px;border:none;background:transparent;border-radius:9px;
  font-size:14.5px;font-weight:600;color:var(--text-dim);cursor:pointer;
  font-family:inherit;transition:all .2s;position:relative;
}
.seg.active{background:var(--bg);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.06)}
.seg .badge-inline{
  display:inline-block;min-width:18px;height:18px;padding:0 5px;border-radius:9px;
  background:#ef4444;color:#fff;font-size:11px;font-weight:700;
  margin-left:6px;line-height:18px;vertical-align:middle;
}

/* ---------- Rows ---------- */
.row{
  display:flex;align-items:center;gap:14px;
  padding:12px 16px;cursor:pointer;transition:background .15s;
  -webkit-user-select:none;user-select:none;
}
.row:active{background:var(--bg-soft)}
.avatar{
  width:52px;height:52px;border-radius:50%;background:var(--bg-elev);
  display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:19px;color:var(--text-dim);flex-shrink:0;
  letter-spacing:.5px;
}
[data-theme="dark"] .avatar{color:var(--text)}
.row-info{flex:1;min-width:0}
.row-title{
  font-size:16.5px;font-weight:600;color:var(--text);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.row-sub{
  font-size:14px;color:var(--text-dim);margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.row-sub.italic{font-style:italic;opacity:.85}
.row-right{display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex-shrink:0}
.badge{
  min-width:22px;height:22px;padding:0 7px;border-radius:11px;
  background:var(--text);color:var(--bg);font-size:12.5px;font-weight:700;
  display:flex;align-items:center;justify-content:center;
}
.row-time{font-size:12.5px;color:var(--text-dim)}
.empty{
  padding:60px 30px;text-align:center;color:var(--text-dim);
  font-size:14.5px;line-height:1.55;
}
.section-label{
  padding:14px 16px 6px;font-size:12.5px;font-weight:700;
  color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px;
}

/* ---------- Small action buttons ---------- */
.mini-btn{
  border:none;font-family:inherit;font-size:13.5px;font-weight:600;
  padding:8px 14px;border-radius:10px;cursor:pointer;transition:transform .1s,opacity .15s;
}
.mini-btn:active{transform:scale(.95)}
.mini-btn.primary{background:var(--text);color:var(--bg)}
.mini-btn.ghost{background:var(--bg-elev);color:var(--text)}
.mini-btn.danger{background:transparent;color:var(--danger)}
.mini-btn-row{display:flex;gap:8px;flex-shrink:0}

/* ---------- Auth ---------- */
#auth{justify-content:center;align-items:center;padding:24px;overflow-y:auto}
.auth-wrap{width:100%;max-width:380px;text-align:center;margin:auto}
.logo{
  width:84px;height:84px;margin:0 auto 22px;background:var(--text);border-radius:26px;
  display:flex;align-items:center;justify-content:center;box-shadow:var(--shadow);
}
.logo svg{width:42px;height:42px;stroke:var(--bg);fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
.auth-wrap h1{font-size:27px;font-weight:700;letter-spacing:-.5px;margin-bottom:6px}
.subtitle{color:var(--text-dim);font-size:14.5px;margin-bottom:26px}
.tabs{display:flex;background:var(--bg-elev);border-radius:12px;padding:4px;margin-bottom:20px}
.tab{
  flex:1;padding:11px;border:none;background:transparent;border-radius:9px;
  font-size:14.5px;font-weight:600;color:var(--text-dim);cursor:pointer;
  transition:all .2s;font-family:inherit;
}
.tab.active{background:var(--bg);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.08)}
.input-wrap{
  display:flex;align-items:center;gap:10px;background:var(--bg-elev);
  border-radius:13px;padding:0 14px;margin-bottom:11px;
  border:1.5px solid transparent;transition:border-color .2s,background .2s;
}
.input-wrap:focus-within{background:var(--bg);border-color:var(--text)}
.input-wrap svg{width:19px;height:19px;stroke:var(--text-dim);flex-shrink:0;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.input-wrap input{
  flex:1;border:none;outline:none;background:transparent;padding:16px 0;
  font-size:16.5px;font-family:inherit;color:var(--text);min-width:0;
}
.input-wrap input::placeholder{color:var(--text-dim)}
.btn-primary{
  width:100%;padding:17px;background:var(--text);color:var(--bg);border:none;
  border-radius:13px;font-size:16.5px;font-weight:600;cursor:pointer;
  margin-top:4px;transition:transform .1s,opacity .2s;font-family:inherit;
}
.btn-primary:active{transform:scale(.98)}
.btn-primary:disabled{opacity:.5;cursor:default}
.error{color:var(--danger);font-size:14px;margin-top:12px;min-height:20px}

/* ---------- Chat ---------- */
.messages{
  flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;
  padding:16px 14px 8px;display:flex;flex-direction:column;gap:6px;
  background:var(--bg);
}
.msg{
  max-width:80%;padding:10px 15px;border-radius:20px;
  font-size:17.5px;line-height:1.42;word-wrap:break-word;overflow-wrap:anywhere;
  white-space:pre-wrap;animation:pop .14s ease;
}
@keyframes pop{from{transform:scale(.94);opacity:0}to{transform:scale(1);opacity:1}}
.msg.me{align-self:flex-end;background:var(--bubble-me);color:var(--bubble-me-fg);
  border-bottom-right-radius:6px}
.msg.them{align-self:flex-start;background:var(--bubble-them);color:var(--bubble-them-fg);
  border-bottom-left-radius:6px}
.day-sep{align-self:center;font-size:12.5px;color:var(--text-dim);margin:8px 0 4px}
.typing{align-self:flex-start;font-size:13px;color:var(--text-dim);
  padding:2px 12px;height:16px;font-style:italic;opacity:0;transition:opacity .2s}
.typing.on{opacity:1}

.composer{
  flex-shrink:0;display:flex;align-items:center;gap:8px;padding:10px 12px;
  padding-bottom:calc(10px + env(safe-area-inset-bottom,0));
  border-top:1px solid var(--border);background:var(--bg);
  z-index:5;
}
.composer input{
  flex:1;padding:13px 18px;border:none;outline:none;background:var(--bg-elev);
  border-radius:24px;font-size:17px;font-family:inherit;color:var(--text);min-width:0;
}
.composer input::placeholder{color:var(--text-dim)}
.send-btn{
  width:46px;height:46px;border-radius:50%;border:none;background:var(--text);
  display:flex;align-items:center;justify-content:center;cursor:pointer;
  flex-shrink:0;transition:transform .1s,opacity .2s;color:var(--bg);
}
.send-btn:active{transform:scale(.92)}
.send-btn svg{width:20px;height:20px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.send-btn:disabled{opacity:.4}

/* ---------- Settings ---------- */
.settings-group{margin:14px 12px;background:var(--bg-elev);border-radius:14px;overflow:hidden}
.set-row{display:flex;align-items:center;justify-content:space-between;
  padding:15px 16px;gap:12px}
.set-row + .set-row{border-top:1px solid var(--border)}
.set-label{font-size:16px;font-weight:500}
.set-hint{font-size:13px;color:var(--text-dim);margin-top:2px}
.switch{
  width:52px;height:31px;border-radius:16px;background:var(--border);
  position:relative;cursor:pointer;transition:background .2s;flex-shrink:0;
}
.switch::after{
  content:'';position:absolute;top:2.5px;left:2.5px;
  width:26px;height:26px;border-radius:50%;background:#fff;
  transition:left .2s;box-shadow:0 1px 3px rgba(0,0,0,.2);
}
.switch.on{background:var(--ok)}
.switch.on::after{left:23.5px}
.settings-user{display:flex;align-items:center;gap:14px;padding:18px 16px}
.settings-user .avatar{width:58px;height:58px;font-size:22px}
.settings-user-name{font-size:18px;font-weight:700}
.settings-user-sub{font-size:13.5px;color:var(--text-dim);margin-top:2px}
.danger-btn{
  width:100%;padding:15px;background:transparent;color:var(--danger);
  border:none;border-radius:14px;font-size:16px;font-weight:600;
  cursor:pointer;font-family:inherit;
}
.danger-btn:active{background:var(--bg-elev)}

/* Search */
.search-wrap{
  display:flex;align-items:center;gap:10px;background:var(--bg-elev);
  border-radius:13px;padding:0 14px;margin:12px 16px 0;
}
.search-wrap svg{width:19px;height:19px;stroke:var(--text-dim);fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}
.search-wrap input{
  flex:1;border:none;outline:none;background:transparent;padding:13px 0;
  font-size:16.5px;font-family:inherit;color:var(--text);min-width:0;
}
.search-wrap input::placeholder{color:var(--text-dim)}

.footnote{
  text-align:center;font-size:12px;color:var(--text-dim);
  padding:20px 30px 8px;line-height:1.5;
}

/* ---------- Toast ---------- */
.toast{
  position:fixed;left:50%;bottom:calc(90px + env(safe-area-inset-bottom,0));
  transform:translateX(-50%) translateY(20px);
  background:var(--text);color:var(--bg);
  padding:11px 18px;border-radius:22px;font-size:14.5px;font-weight:600;
  opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;
  z-index:9999;max-width:90vw;text-align:center;
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
</style>
</head>
<body>
<div id="app">

  <!-- ===================== AUTH ===================== -->
  <div id="auth" class="screen active">
    <div class="auth-wrap">
      <div class="logo">
        <svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      </div>
      <h1>Мессенджер</h1>
      <p class="subtitle">Общайтесь без лишнего</p>

      <div class="tabs">
        <button type="button" class="tab active" data-tab="login">Вход</button>
        <button type="button" class="tab" data-tab="register">Регистрация</button>
      </div>

      <form id="auth-form" autocomplete="on">
        <div class="input-wrap">
          <svg viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
          <input type="text" id="username" placeholder="Имя пользователя" autocomplete="username" maxlength="20">
        </div>
        <div class="input-wrap">
          <svg viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          <input type="password" id="password" placeholder="Пароль" autocomplete="current-password">
        </div>
        <button type="submit" class="btn-primary" id="auth-submit">Войти</button>
        <div class="error" id="auth-error"></div>
      </form>
    </div>
  </div>

  <!-- ===================== MAIN ===================== -->
  <div id="main" class="screen">
    <header class="topbar glass">
      <div class="title" id="main-title">Чаты</div>
      <button class="icon-btn" id="quick-theme" aria-label="Тема">
        <svg id="quick-theme-icon" viewBox="0 0 24 24"></svg>
      </button>
    </header>

    <div class="page-area">
      <!-- HOME -->
      <div class="page active" id="page-home">
        <div class="page-scroll" id="home-list"></div>
      </div>

      <!-- FRIENDS -->
      <div class="page" id="page-friends">
        <div class="search-wrap">
          <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input type="text" id="friend-search" placeholder="Найти пользователя..." autocomplete="off">
        </div>
        <div class="segmented" id="friends-seg">
          <button class="seg active" data-tab="friends">Друзья</button>
          <button class="seg" data-tab="pending">Ожидание<span id="pending-badge"></span></button>
        </div>
        <div class="page-scroll" id="friends-list"></div>
      </div>

      <!-- SETTINGS -->
      <div class="page" id="page-settings">
        <div class="page-scroll">
          <div class="settings-group">
            <div class="settings-user">
              <div class="avatar" id="me-avatar">?</div>
              <div>
                <div class="settings-user-name" id="me-name">—</div>
                <div class="settings-user-sub">в сети</div>
              </div>
            </div>
          </div>

          <div class="settings-group">
            <div class="set-row">
              <div>
                <div class="set-label">Тёмная тема</div>
                <div class="set-hint">Ночной режим интерфейса</div>
              </div>
              <div class="switch" id="set-theme"></div>
            </div>
            <div class="set-row">
              <div>
                <div class="set-label">Liquid Glass</div>
                <div class="set-hint">Полупрозрачные панели с блюром</div>
              </div>
              <div class="switch" id="set-glass"></div>
            </div>
            <div class="set-row">
              <div>
                <div class="set-label">Уведомления</div>
                <div class="set-hint" id="notif-hint">Всплывающие оповещения о новых сообщениях</div>
              </div>
              <div class="switch" id="set-notif"></div>
            </div>
          </div>

          <div class="settings-group">
            <button class="danger-btn" id="logout-btn">Выйти из аккаунта</button>
          </div>

          <div class="footnote">
            Данные хранятся в оперативной памяти сервера.<br>
            Перезапуск — и всё исчезнет.
          </div>
        </div>
      </div>
    </div>

    <nav class="bottom-nav glass">
      <button class="nav-btn active" data-page="home">
        <svg viewBox="0 0 24 24"><path d="M3 12l9-9 9 9"/><path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10"/></svg>
        <span>Главная</span>
        <span class="dot" id="home-dot" style="display:none"></span>
      </button>
      <button class="nav-btn" data-page="friends">
        <svg viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        <span>Друзья</span>
        <span class="dot" id="friends-dot" style="display:none"></span>
      </button>
      <button class="nav-btn" data-page="settings">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9c.14.6.65 1 1.26 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        <span>Настройки</span>
      </button>
    </nav>
  </div>

  <!-- ===================== CHAT ===================== -->
  <div id="chat" class="screen">
    <header class="topbar glass">
      <button class="icon-btn" id="chat-back" aria-label="Назад">
        <svg viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
      </button>
      <div class="title center" id="chat-title">—</div>
      <div style="width:44px"></div>
    </header>
    <div class="messages" id="messages"></div>
    <div class="typing" id="typing"></div>
    <div class="composer glass">
      <input type="text" id="msg-input" placeholder="Сообщение..." maxlength="2000" autocomplete="off" enterkeyhint="send">
      <button class="send-btn" id="send-btn" aria-label="Отправить">
        <svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      </button>
    </div>
  </div>

</div>
<div class="toast" id="toast"></div>

<script>
(function(){
"use strict";

const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

const state = {
  token: localStorage.getItem('m_token') || '',
  username: localStorage.getItem('m_user') || '',
  theme: localStorage.getItem('m_theme') || 'light',
  glass: localStorage.getItem('m_glass') || 'on',
  notif: localStorage.getItem('m_notif') === '1',
  ws: null,
  wsReady: false,
  reconnectTimer: null,
  page: 'home',
  friendsTab: 'friends',
  chats: [],
  friendsList: [],
  incoming: [],
  outgoing: [],
  searchResults: [],
  searchQuery: '',
  activePeer: null,
  activeMsgs: [],
  seenIds: new Set(),
  friendCache: new Set(),
};

// ---------- SVG icons ----------
const ICONS = {
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/>',
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
};

// ---------- Helpers ----------
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
}[c]));

function fmtTime(ts){
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0');
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return 'вчера';
  const dd = d.getDate().toString().padStart(2,'0');
  const mm = (d.getMonth()+1).toString().padStart(2,'0');
  return dd + '.' + mm;
}

function toast(msg){
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 1800);
}

function applyTheme(){
  document.documentElement.setAttribute('data-theme', state.theme);
  const meta = document.querySelector('meta[name=theme-color]');
  if (meta) meta.setAttribute('content', state.theme === 'dark' ? '#0b0b0c' : '#ffffff');
  // quick toggle icon
  const icon = $('#quick-theme-icon');
  icon.outerHTML = '<svg id="quick-theme-icon" viewBox="0 0 24 24">' + (state.theme === 'dark' ? ICONS.sun : ICONS.moon) + '</svg>';
  const setSw = $('#set-theme');
  if (setSw) setSw.classList.toggle('on', state.theme === 'dark');
}

function applyGlass(){
  document.documentElement.setAttribute('data-glass', state.glass);
  const setSw = $('#set-glass');
  if (setSw) setSw.classList.toggle('on', state.glass === 'on');
}

function applyNotifUI(){
  const setSw = $('#set-notif');
  if (!setSw) return;
  const supported = 'Notification' in window;
  const granted = supported && Notification.permission === 'granted';
  setSw.classList.toggle('on', granted && state.notif);
  const hint = $('#notif-hint');
  if (hint){
    if (!supported) hint.textContent = 'Не поддерживается браузером';
    else if (Notification.permission === 'denied') hint.textContent = 'Запрещено в браузере';
    else if (granted) hint.textContent = 'Уведомления включены';
    else hint.textContent = 'Нажмите, чтобы разрешить';
  }
}

// ---------- Viewport fix (keyboard) ----------
function updateViewport(){
  const vv = window.visualViewport;
  const h = vv ? vv.height : window.innerHeight;
  document.documentElement.style.setProperty('--app-h', h + 'px');
}
if (window.visualViewport){
  window.visualViewport.addEventListener('resize', updateViewport);
  window.visualViewport.addEventListener('scroll', () => {
    updateViewport();
    window.scrollTo(0,0);
  });
}
window.addEventListener('orientationchange', () => setTimeout(updateViewport, 200));
updateViewport();

// ---------- Auth ----------
async function api(path, opts){
  opts = opts || {};
  opts.headers = Object.assign({
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + state.token,
  }, opts.headers || {});
  const r = await fetch(path, opts);
  if (r.status === 401 && state.token){ doLogout(); throw new Error('unauth'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}

let authMode = 'login';
$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  authMode = t.dataset.tab;
  $('#auth-submit').textContent = authMode === 'login' ? 'Войти' : 'Создать аккаунт';
  $('#auth-error').textContent = '';
}));

$('#auth-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = $('#username').value.trim();
  const password = $('#password').value;
  const errEl = $('#auth-error');
  errEl.textContent = '';
  if (!username || !password){ errEl.textContent = 'Заполните все поля'; return; }
  const btn = $('#auth-submit');
  btn.disabled = true;
  try {
    const res = await fetch('/api/' + authMode, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username, password}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Ошибка');
    state.token = data.token;
    state.username = data.username;
    localStorage.setItem('m_token', state.token);
    localStorage.setItem('m_user', state.username);
    $('#password').value = '';
    await enterApp();
  } catch (err){
    errEl.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

function doLogout(){
  localStorage.removeItem('m_token');
  localStorage.removeItem('m_user');
  state.token = '';
  state.username = '';
  state.activePeer = null;
  state.activeMsgs = [];
  state.seenIds.clear();
  state.friendCache.clear();
  if (state.ws){ try { state.ws.close(); } catch(e){} state.ws = null; }
  clearTimeout(state.reconnectTimer);
  showScreen('auth');
}

$('#logout-btn').addEventListener('click', doLogout);

// ---------- Screen / Page navigation ----------
function showScreen(name){
  $$('.screen').forEach(s => s.classList.remove('active'));
  const el = document.getElementById(name);
  if (el) el.classList.add('active');
}

function switchPage(page){
  state.page = page;
  $$('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.page === page));
  $$('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + page));
  const titles = { home: 'Чаты', friends: 'Друзья', settings: 'Настройки' };
  $('#main-title').textContent = titles[page] || 'Мессенджер';
  if (page === 'home') refreshChats();
  if (page === 'friends') refreshFriends();
}

$$('.nav-btn').forEach(b => b.addEventListener('click', () => switchPage(b.dataset.page)));

// ---------- Enter app ----------
async function enterApp(){
  $('#me-avatar').textContent = (state.username[0] || '?').toUpperCase();
  $('#me-name').textContent = state.username;
  applyTheme(); applyGlass(); applyNotifUI();
  connectWS();
  switchPage('home');
  showScreen('main');
  updateViewport();
  // register SW
  if ('serviceWorker' in navigator){
    navigator.serviceWorker.register('/sw.js').catch(()=>{});
  }
}

// ---------- WS ----------
function connectWS(){
  if (!state.token) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = proto + '://' + location.host + '/ws?token=' + encodeURIComponent(state.token);
  let ws;
  try { ws = new WebSocket(url); } catch(e){ return; }
  state.ws = ws;

  ws.onopen = () => {
    state.wsReady = true;
  };
  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(e.data); } catch(_) { return; }
    handleServerEvent(m);
  };
  ws.onclose = (e) => {
    state.wsReady = false;
    if (state.ws === ws) state.ws = null;
    if (e.code === 4000) return; // replaced by another tab
    if (state.token){
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = setTimeout(connectWS, 1200 + Math.random() * 800);
    }
  };
  ws.onerror = () => {};
}

function wsSend(obj){
  if (state.ws && state.ws.readyState === 1){
    try { state.ws.send(JSON.stringify(obj)); return true; } catch(e){}
  }
  return false;
}

// ---------- Server events ----------
function handleServerEvent(m){
  if (m.type === 'hello'){
    return;
  }
  if (m.type === 'message'){
    // сохраняем только если диалог с этим пользователем открыт
    const peer = m.from === state.username ? m.to : m.from;
    if (state.activePeer === peer && !state.seenIds.has(m.id)){
      state.seenIds.add(m.id);
      state.activeMsgs.push({ id: m.id, from: m.from, text: m.text, ts: m.ts });
      appendMessage({ id: m.id, from: m.from, text: m.text, ts: m.ts });
      scrollMessages(true);
      if (m.from !== state.username){
        wsSend({ type: 'read', peer });
      }
    } else if (state.activePeer !== peer && m.from !== state.username){
      // уведомление
      maybeNotify(m);
    }
    // обновим список чатов
    if (state.page === 'home') refreshChats();
    return;
  }
  if (m.type === 'chat_read'){
    if (state.page === 'home') refreshChats();
    return;
  }
  if (m.type === 'friends_changed'){
    refreshFriends();
    refreshChats();
    return;
  }
  if (m.type === 'typing'){
    if (m.from === state.activePeer){
      const el = $('#typing');
      el.textContent = 'печатает…';
      el.classList.add('on');
      clearTimeout(el._t);
      el._t = setTimeout(() => el.classList.remove('on'), 2500);
    }
  }
}

function maybeNotify(m){
  if (!state.notif) return;
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  // не показываем если вкладка активна И открыт чат с этим пользователем
  if (!document.hidden && state.activePeer === m.from) return;
  const title = m.from;
  const body = m.text.length > 80 ? m.text.slice(0, 77) + '…' : m.text;
  try {
    if (navigator.serviceWorker && navigator.serviceWorker.ready){
      navigator.serviceWorker.ready.then(reg => {
        reg.showNotification(title, {
          body,
          tag: 'msg-' + m.from,
          renotify: true,
          icon: '/icon.svg',
          badge: '/icon.svg',
          data: { peer: m.from },
        }).catch(()=>{});
      }).catch(()=>{});
    } else {
      new Notification(title, { body, icon: '/icon.svg' });
    }
  } catch(e){}
}

// ---------- Chats (Home) ----------
async function refreshChats(){
  if (!state.token) return;
  try {
    const list = await api('/api/chats');
    state.chats = list;
    renderChats();
    updateDots();
  } catch(e){}
}

function renderChats(){
  const box = $('#home-list');
  if (!state.chats.length){
    box.innerHTML = '<div class="empty">Пока нет чатов.<br>Перейдите во вкладку «Друзья»,<br>чтобы найти собеседников.</div>';
    return;
  }
  box.innerHTML = state.chats.map(c => {
    const initial = (c.username[0] || '?').toUpperCase();
    const sub = c.lastText
      ? (c.lastFromMe ? 'Вы: ' : '') + c.lastText
      : '<span class="italic" style="color:var(--text-dim)">Нет сообщений</span>';
    const badge = c.unread ? '<div class="badge">' + c.unread + '</div>' : '';
    const time = c.lastTs ? '<div class="row-time">' + esc(fmtTime(c.lastTs)) + '</div>' : '';
    return '<div class="row" data-peer="' + esc(c.username) + '">' +
      '<div class="avatar">' + esc(initial) + '</div>' +
      '<div class="row-info"><div class="row-title">' + esc(c.username) + '</div>' +
      '<div class="row-sub">' + sub + '</div></div>' +
      '<div class="row-right">' + time + badge + '</div></div>';
  }).join('');
  box.querySelectorAll('.row').forEach(r => r.addEventListener('click', () => openChat(r.dataset.peer)));
}

// ---------- Friends ----------
function updateDots(){
  const homeUnread = state.chats.reduce((s,c) => s + (c.unread||0), 0);
  const homeDot = $('#home-dot');
  if (homeUnread > 0){ homeDot.textContent = homeUnread > 99 ? '99+' : homeUnread; homeDot.style.display = 'flex'; }
  else homeDot.style.display = 'none';

  const pendingCount = state.incoming.length;
  const friendsDot = $('#friends-dot');
  if (pendingCount > 0){ friendsDot.textContent = pendingCount > 99 ? '99+' : pendingCount; friendsDot.style.display = 'flex'; }
  else friendsDot.style.display = 'none';

  const pb = $('#pending-badge');
  if (pendingCount > 0) pb.innerHTML = '<span class="badge-inline">' + pendingCount + '</span>';
  else pb.innerHTML = '';
}

async function refreshFriends(){
  if (!state.token) return;
  try {
    const data = await api('/api/friends');
    state.friendsList = data.friends || [];
    state.incoming = data.incoming || [];
    state.outgoing = data.outgoing || [];
    state.friendCache = new Set(state.friendsList);
    updateDots();
    renderFriends();
  } catch(e){}
}

$$('#friends-seg .seg').forEach(b => b.addEventListener('click', () => {
  state.friendsTab = b.dataset.tab;
  $$('#friends-seg .seg').forEach(x => x.classList.toggle('active', x === b));
  renderFriends();
}));

function renderFriends(){
  const box = $('#friends-list');

  // Если есть запрос на поиск — показываем результаты
  if (state.searchQuery){
    renderSearch(box);
    return;
  }
  if (state.friendsTab === 'friends'){
    if (!state.friendsList.length){
      box.innerHTML = '<div class="empty">Пока нет друзей.<br>Найдите людей через поиск выше.</div>';
      return;
    }
    box.innerHTML = state.friendsList.map(name => {
      const initial = (name[0] || '?').toUpperCase();
      return '<div class="row">' +
        '<div class="avatar">' + esc(initial) + '</div>' +
        '<div class="row-info"><div class="row-title">' + esc(name) + '</div></div>' +
        '<div class="mini-btn-row">' +
          '<button class="mini-btn primary" data-act="open" data-user="' + esc(name) + '">Написать</button>' +
          '<button class="mini-btn danger" data-act="unfriend" data-user="' + esc(name) + '">' +
            '<svg style="width:18px;height:18px;vertical-align:middle;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round" viewBox="0 0 24 24">' + ICONS.trash + '</svg>' +
          '</button>' +
        '</div></div>';
    }).join('');
    box.querySelectorAll('button[data-act]').forEach(b => {
      b.addEventListener('click', () => friendAction(b.dataset.act, b.dataset.user));
    });
    return;
  }

  // pending
  const inc = state.incoming.map(name => rowPending(name, 'incoming')).join('');
  const out = state.outgoing.map(name => rowPending(name, 'outgoing')).join('');
  let html = '';
  if (inc) html += '<div class="section-label">Входящие</div>' + inc;
  if (out) html += '<div class="section-label">Исходящие</div>' + out;
  if (!html) html = '<div class="empty">Нет активных заявок.</div>';
  box.innerHTML = html;
  box.querySelectorAll('button[data-act]').forEach(b => {
    b.addEventListener('click', () => friendAction(b.dataset.act, b.dataset.user));
  });
}

function rowPending(name, kind){
  const initial = (name[0] || '?').toUpperCase();
  const btns = kind === 'incoming'
    ? '<button class="mini-btn primary" data-act="accept" data-user="' + esc(name) + '">Принять</button>' +
      '<button class="mini-btn ghost" data-act="decline" data-user="' + esc(name) + '">Отклонить</button>'
    : '<button class="mini-btn ghost" data-act="cancel" data-user="' + esc(name) + '">Отменить</button>';
  return '<div class="row">' +
    '<div class="avatar">' + esc(initial) + '</div>' +
    '<div class="row-info"><div class="row-title">' + esc(name) + '</div>' +
    '<div class="row-sub">' + (kind === 'incoming' ? 'хочет добавить вас' : 'заявка отправлена') + '</div></div>' +
    '<div class="mini-btn-row">' + btns + '</div></div>';
}

function renderSearch(box){
  if (!state.searchResults.length){
    box.innerHTML = '<div class="empty">Ничего не найдено.</div>';
    return;
  }
  box.innerHTML = state.searchResults.map(u => {
    const initial = (u.username[0] || '?').toUpperCase();
    let btn = '';
    if (u.status === 'friend') btn = '<div class="mini-btn ghost" style="opacity:.6">Друзья</div>';
    else if (u.status === 'outgoing') btn = '<button class="mini-btn ghost" data-act="cancel" data-user="' + esc(u.username) + '">Отменить</button>';
    else if (u.status === 'incoming') btn = '<button class="mini-btn primary" data-act="accept" data-user="' + esc(u.username) + '">Принять</button>';
    else btn = '<button class="mini-btn primary" data-act="request" data-user="' + esc(u.username) + '">Добавить</button>';
    return '<div class="row">' +
      '<div class="avatar">' + esc(initial) + '</div>' +
      '<div class="row-info"><div class="row-title">' + esc(u.username) + '</div></div>' +
      '<div class="mini-btn-row">' + btn + '</div></div>';
  }).join('');
  box.querySelectorAll('button[data-act]').forEach(b => {
    b.addEventListener('click', () => friendAction(b.dataset.act, b.dataset.user));
  });
}

async function friendAction(act, user){
  try {
    if (act === 'open'){ openChat(user); return; }
    if (act === 'unfriend'){
      if (!confirm('Удалить ' + user + ' из друзей?')) return;
      await api('/api/friends/remove', { method: 'POST', body: JSON.stringify({target:user}) });
      toast('Удалено');
    } else if (act === 'request'){
      await api('/api/friends/request', { method: 'POST', body: JSON.stringify({target:user}) });
      toast('Заявка отправлена');
    } else if (act === 'accept'){
      await api('/api/friends/accept', { method: 'POST', body: JSON.stringify({target:user}) });
      toast('Теперь вы друзья');
    } else if (act === 'decline'){
      await api('/api/friends/decline', { method: 'POST', body: JSON.stringify({target:user}) });
    } else if (act === 'cancel'){
      await api('/api/friends/cancel', { method: 'POST', body: JSON.stringify({target:user}) });
    }
    await refreshFriends();
    if (state.searchQuery) await runSearch(state.searchQuery);
  } catch(err){
    toast(err.message || 'Ошибка');
  }
}

// ---------- Search ----------
let searchTimer = null;
$('#friend-search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  state.searchQuery = q;
  if (!q){ state.searchResults = []; renderFriends(); return; }
  searchTimer = setTimeout(() => runSearch(q), 220);
});

async function runSearch(q){
  try {
    const res = await api('/api/users?q=' + encodeURIComponent(q));
    state.searchResults = res;
    renderFriends();
  } catch(e){}
}

// ---------- Chat ----------
async function openChat(peer){
  state.activePeer = peer;
  state.activeMsgs = [];
  state.seenIds.clear();
  $('#chat-title').textContent = peer;
  $('#messages').innerHTML = '';
  $('#typing').classList.remove('on');
  showScreen('chat');
  updateViewport();

  try {
    const list = await api('/api/messages/' + encodeURIComponent(peer));
    state.activeMsgs = list;
    for (const m of list){
      state.seenIds.add(m.id);
      appendMessage({ id:m.id, from:m.from, text:m.text, ts:m.ts });
    }
    scrollMessages(true);
    // помечаем прочитанным ещё раз через WS
    wsSend({ type:'read', peer });
    // локально сбросим badge
    const c = state.chats.find(x => x.username === peer);
    if (c){ c.unread = 0; renderChats(); updateDots(); }
  } catch(e){
    toast('Ошибка загрузки');
  }
  setTimeout(() => $('#msg-input').focus({preventScroll:true}), 50);
}

function appendMessage(m){
  const box = $('#messages');
  const el = document.createElement('div');
  el.className = 'msg ' + (m.from === state.username ? 'me' : 'them');
  el.textContent = m.text;
  box.appendChild(el);
}

function scrollMessages(smooth){
  const box = $('#messages');
  requestAnimationFrame(() => {
    box.scrollTop = box.scrollHeight;
  });
}

$('#chat-back').addEventListener('click', () => {
  state.activePeer = null;
  state.activeMsgs = [];
  state.seenIds.clear();
  showScreen('main');
  if (state.page === 'home') refreshChats();
  setTimeout(updateViewport, 50);
});

function sendMessage(){
  if (!state.activePeer) return;
  const input = $('#msg-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  const ok = wsSend({ type:'send', to: state.activePeer, text });
  if (!ok){
    toast('Нет соединения');
    input.value = text;
    return;
  }
  // не теряем фокус, сохраняем клавиатуру
  requestAnimationFrame(() => input.focus({preventScroll:true}));
}

$('#send-btn').addEventListener('click', sendMessage);
$('#send-btn').addEventListener('mousedown', e => e.preventDefault());
$('#send-btn').addEventListener('touchstart', e => e.preventDefault(), {passive:false});

$('#msg-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey){
    e.preventDefault();
    sendMessage();
  }
});

// typing indicator (мы печатаем)
let typingTimer = null;
$('#msg-input').addEventListener('input', () => {
  if (!state.activePeer) return;
  clearTimeout(typingTimer);
  typingTimer = setTimeout(() => {
    wsSend({ type:'typing', to: state.activePeer });
  }, 300);
});

// автоскролл при появлении клавиатуры
if (window.visualViewport){
  window.visualViewport.addEventListener('resize', () => {
    if (state.activePeer) scrollMessages();
  });
}

// ---------- Settings toggles ----------
$('#set-theme').addEventListener('click', () => {
  state.theme = state.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('m_theme', state.theme);
  applyTheme();
});
$('#quick-theme').addEventListener('click', () => {
  state.theme = state.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('m_theme', state.theme);
  applyTheme();
});
$('#set-glass').addEventListener('click', () => {
  state.glass = state.glass === 'on' ? 'off' : 'on';
  localStorage.setItem('m_glass', state.glass);
  applyGlass();
});
$('#set-notif').addEventListener('click', async () => {
  if (!('Notification' in window)){ toast('Не поддерживается'); return; }
  if (Notification.permission === 'granted'){
    // toggle
    state.notif = !state.notif;
    localStorage.setItem('m_notif', state.notif ? '1' : '0');
    applyNotifUI();
    return;
  }
  if (Notification.permission === 'denied'){ toast('Запрещено в браузере'); return; }
  try {
    const p = await Notification.requestPermission();
    if (p === 'granted'){
      state.notif = true;
      localStorage.setItem('m_notif', '1');
      toast('Уведомления включены');
    } else {
      toast('Разрешение не выдано');
    }
  } catch(e){}
  applyNotifUI();
});

// отключаем зум двойным тапом
document.addEventListener('dblclick', e => e.preventDefault(), {passive:false});

// ---------- Init ----------
applyTheme();
applyGlass();

if (state.token){
  // Проверяем валидность токена
  fetch('/api/chats', { headers: { 'Authorization': 'Bearer ' + state.token } }).then(r => {
    if (r.ok){ enterApp(); }
    else { doLogout(); }
  }).catch(() => doLogout());
} else {
  showScreen('auth');
  updateViewport();
}

// На случай если приложение вернулось из фона
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.token){
    if (!state.ws || state.ws.readyState > 1) connectWS();
    if (state.page === 'home') refreshChats();
    if (state.page === 'friends') refreshFriends();
  }
});

})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
