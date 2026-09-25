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
messages: list = []      # {"id","cid","from","to","text","ts","read"}
friends: dict = {}       # user -> set(user)
requests: dict = {}      # from_user -> set(to_user)


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


def delete_dialog(a: str, b: str):
    messages[:] = [m for m in messages if not (
        (m["from"] == a and m["to"] == b) or (m["from"] == b and m["to"] == a)
    )]


class WSManager:
    def __init__(self):
        self.conns: dict = {}

    def is_online(self, user: str) -> bool:
        return user in self.conns

    async def add(self, user: str, ws: WebSocket) -> bool:
        old = self.conns.get(user)
        was_online = old is not None
        self.conns[user] = ws
        if old is not None and old is not ws:
            try:
                await old.close(code=4000)
            except Exception:
                pass
        return not was_online

    async def remove(self, user: str, ws: WebSocket) -> bool:
        if self.conns.get(user) is ws:
            self.conns.pop(user, None)
            return True
        return False

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


async def broadcast_presence(username: str, online: bool):
    payload = {"type": "presence", "user": username, "online": online}
    for f in friends.get(username, set()):
        await manager.send(f, payload)


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
        out.append({
            "username": name,
            "status": status,
            "online": manager.is_online(name),
        })
    out.sort(key=lambda x: x["username"].lower())
    return out[:100]


@app.get("/api/friends")
async def get_friends(user: str = Depends(current_user)):
    fl = sorted(friends.get(user, set()), key=str.lower)
    incoming = sorted([f for f, s in requests.items() if user in s], key=str.lower)
    outgoing = sorted(list(requests.get(user, set())), key=str.lower)
    return {
        "friends": fl,
        "online": [f for f in fl if manager.is_online(f)],
        "incoming": incoming,
        "outgoing": outgoing,
    }


@app.post("/api/friends/request")
async def friend_request(data: TargetData, user: str = Depends(current_user)):
    t = data.target
    if t == user or t not in users:
        raise HTTPException(400, "Пользователь не найден")
    if are_friends(user, t):
        raise HTTPException(400, "Уже друзья")
    if user in requests.get(t, set()):
        requests[t].discard(user)
        add_friends(user, t)
        await manager.send(t, {"type": "friends_changed"})
        await manager.send(user, {"type": "friends_changed"})
        if manager.is_online(user):
            await manager.send(t, {"type": "presence", "user": user, "online": True})
        if manager.is_online(t):
            await manager.send(user, {"type": "presence", "user": t, "online": True})
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
    if manager.is_online(user):
        await manager.send(f, {"type": "presence", "user": user, "online": True})
    if manager.is_online(f):
        await manager.send(user, {"type": "presence", "user": f, "online": True})
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
    delete_dialog(user, t)
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
            "online": manager.is_online(f),
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
            out.append({
                "id": m["id"], "cid": m.get("cid"), "from": m["from"],
                "text": m["text"], "ts": m["ts"],
            })
        elif m["from"] == peer and m["to"] == user:
            if not m["read"]:
                m["read"] = True
                changed = True
            out.append({
                "id": m["id"], "cid": m.get("cid"), "from": m["from"],
                "text": m["text"], "ts": m["ts"],
            })
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
        cid = (data.get("cid") or "").strip()[:40] or None
        if not text or len(text) > 2000:
            return
        if not to or not are_friends(username, to):
            return
        msg = {
            "id": uuid.uuid4().hex,
            "cid": cid,
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
    just_online = await manager.add(username, websocket)
    try:
        await websocket.send_json({"type": "hello", "username": username})

        for f in friends.get(username, set()):
            if manager.is_online(f):
                await websocket.send_json({"type": "presence", "user": f, "online": True})

        if just_online:
            await broadcast_presence(username, True)

        while True:
            data = await websocket.receive_json()
            if isinstance(data, dict):
                await handle_ws(username, data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        went_offline = await manager.remove(username, websocket)
        if went_offline:
            await broadcast_presence(username, False)


# ==================================================================
#                     PWA / SERVICE WORKER / ИКОНКА
# ==================================================================
SW_JS = r"""
const CACHE = 'msgr-v3';
const PRECACHE = ['/', '/icon.svg', '/manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    try { await caches.open(CACHE).then(c => c.addAll(PRECACHE)); } catch (_) {}
    await self.skipWaiting();
  })());
});

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
  if (url.origin !== location.origin) return;
  if (url.pathname === '/ws') return;

  // Навигационные запросы: network-first, fallback на кэш
  if (req.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html'){
    e.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        const clone = fresh.clone();
        caches.open(CACHE).then(c => c.put('/', clone)).catch(()=>{});
        return fresh;
      } catch (_) {
        const cached = await caches.match('/');
        if (cached) return cached;
        return new Response(
          '<!doctype html><meta charset=utf-8><title>Offline</title>',
          { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
        );
      }
    })());
    return;
  }

  // Остальное: stale-while-revalidate
  e.respondWith((async () => {
    const cached = await caches.match(req, { ignoreSearch: true });
    const network = fetch(req).then(resp => {
      if (resp && resp.status === 200){
        const clone = resp.clone();
        caches.open(CACHE).then(c => c.put(req, clone)).catch(()=>{});
      }
      return resp;
    }).catch(() => null);
    return cached || (await network) || Response.error();
  })());
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const peer = (e.notification.data && e.notification.data.peer) || '';
  e.waitUntil((async () => {
    const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of list) {
      try {
        c.postMessage({ type: 'open-chat', peer });
        if ('focus' in c) { await c.focus(); return; }
      } catch (_) {}
    }
    if (self.clients.openWindow) {
      const url = peer ? '/?open=' + encodeURIComponent(peer) : '/';
      return self.clients.openWindow(url);
    }
  })());
});

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'skip-waiting') self.skipWaiting();
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
  --bg:#ffffff; --bg-elev:#f4f4f5; --bg-soft:#fafafa;
  --text:#111111; --text-dim:#8a8a8e; --border:#ececec;
  --bubble-me:#111111; --bubble-me-fg:#ffffff;
  --bubble-them:#f2f2f3; --bubble-them-fg:#111111;
  --danger:#e11d48; --ok:#16a34a; --warn:#f59e0b; --offline:#b0b0b5;
  --glass-bg:rgba(255,255,255,.72); --glass-border:rgba(255,255,255,.5);
  --shadow:0 8px 24px rgba(0,0,0,.06);
  --app-h:100dvh;
}
[data-theme="dark"]{
  --bg:#0b0b0c; --bg-elev:#17171a; --bg-soft:#101013;
  --text:#f5f5f7; --text-dim:#8e8e93; --border:#232326;
  --bubble-me:#ffffff; --bubble-me-fg:#111111;
  --bubble-them:#1d1d20; --bubble-them-fg:#f5f5f7;
  --danger:#ff5c7a; --ok:#34d058; --warn:#fbbf24; --offline:#5a5a5e;
  --glass-bg:rgba(15,15,17,.66); --glass-border:rgba(255,255,255,.08);
  --shadow:0 8px 24px rgba(0,0,0,.4);
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{
  position:fixed; top:0; left:0; right:0; bottom:0;
  width:100%; height:100%;
  background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
  font-size:17px;-webkit-font-smoothing:antialiased;
  overflow:hidden; overscroll-behavior:none;
}
body{ touch-action:manipulation; }
#app{
  position:fixed; top:0; left:0; right:0;
  height:var(--app-h); overflow:hidden; background:var(--bg);
}
.screen{position:absolute;inset:0;display:none;flex-direction:column;
  overflow:hidden;background:var(--bg)}
.screen.active{display:flex}

.topbar{flex-shrink:0;display:flex;align-items:center;gap:6px;
  padding:8px 8px;
  padding-top:calc(8px + env(safe-area-inset-top,0));
  border-bottom:1px solid var(--border); background:var(--bg);
  min-height:56px; z-index:5}
.topbar .title{flex:1;font-size:20px;font-weight:700;letter-spacing:-.3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  display:flex;align-items:center;gap:8px;min-width:0}
.topbar .title .title-text{overflow:hidden;text-overflow:ellipsis}
.conn-dot{width:9px;height:9px;border-radius:50%;
  background:var(--offline);flex-shrink:0;
  transition:background .25s, box-shadow .25s}
.conn-dot.online{background:var(--ok);box-shadow:0 0 0 3px rgba(22,163,74,.15)}
.conn-dot.connecting{background:var(--warn);animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}

.icon-btn{width:44px;height:44px;flex-shrink:0;border:none;background:transparent;
  display:flex;align-items:center;justify-content:center;
  border-radius:12px;cursor:pointer;color:var(--text);
  transition:background .15s, transform .1s; touch-action:manipulation}
.icon-btn:active{background:var(--bg-elev);transform:scale(.94)}
.icon-btn svg{width:22px;height:22px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

[data-glass="on"] .glass{
  background:var(--glass-bg) !important;
  backdrop-filter:blur(22px) saturate(180%);
  -webkit-backdrop-filter:blur(22px) saturate(180%);
  border-color:var(--glass-border) !important;
}

.bottom-nav{flex-shrink:0;display:flex;padding:6px 6px;
  padding-bottom:calc(6px + env(safe-area-inset-bottom,0));
  border-top:1px solid var(--border); background:var(--bg); z-index:5}
.nav-btn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;padding:8px 4px;background:transparent;border:none;cursor:pointer;
  color:var(--text-dim);font-family:inherit;font-size:11.5px;font-weight:600;
  border-radius:14px;transition:color .15s,background .15s,transform .1s;
  position:relative;touch-action:manipulation}
.nav-btn:active{transform:scale(.95)}
.nav-btn.active{color:var(--text)}
.nav-btn svg{width:24px;height:24px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.nav-btn .dot{position:absolute;top:6px;right:calc(50% - 18px);
  min-width:16px;height:16px;padding:0 4px;border-radius:8px;
  background:#ef4444;color:#fff;font-size:10px;font-weight:700;
  display:flex;align-items:center;justify-content:center}

.page-area{flex:1;overflow:hidden;position:relative;min-height:0}
.page{position:absolute;inset:0;display:none;flex-direction:column;overflow:hidden}
.page.active{display:flex}
.page-scroll{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;
  overscroll-behavior:contain;min-height:0}

.segmented{display:flex;background:var(--bg-elev);border-radius:12px;
  padding:4px;margin:12px 16px 6px}
.seg{flex:1;padding:10px;border:none;background:transparent;border-radius:9px;
  font-size:14.5px;font-weight:600;color:var(--text-dim);cursor:pointer;
  font-family:inherit;transition:all .2s;position:relative;touch-action:manipulation}
.seg.active{background:var(--bg);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.06)}
.seg .badge-inline{display:inline-block;min-width:18px;height:18px;padding:0 5px;
  border-radius:9px;background:#ef4444;color:#fff;font-size:11px;font-weight:700;
  margin-left:6px;line-height:18px;vertical-align:middle}

.row{display:flex;align-items:center;gap:14px;padding:12px 16px;
  cursor:pointer;transition:background .15s;-webkit-user-select:none;user-select:none}
.row:active{background:var(--bg-soft)}
.avatar{position:relative;width:52px;height:52px;border-radius:50%;
  background:var(--bg-elev);display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:19px;color:var(--text-dim);flex-shrink:0;letter-spacing:.5px}
[data-theme="dark"] .avatar{color:var(--text)}
.avatar .online-dot{position:absolute;bottom:1px;right:1px;
  width:14px;height:14px;border-radius:50%;background:var(--offline);
  border:2.5px solid var(--bg);box-sizing:content-box;
  transition:background .2s}
.avatar .online-dot.on{background:var(--ok)}
.row-info{flex:1;min-width:0}
.row-title{font-size:16.5px;font-weight:600;color:var(--text);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row-sub{font-size:14px;color:var(--text-dim);margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row-sub.typing{color:var(--ok);font-style:italic}
.row-right{display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex-shrink:0}
.badge{min-width:22px;height:22px;padding:0 7px;border-radius:11px;
  background:var(--text);color:var(--bg);font-size:12.5px;font-weight:700;
  display:flex;align-items:center;justify-content:center}
.row-time{font-size:12.5px;color:var(--text-dim)}
.empty{padding:60px 30px;text-align:center;color:var(--text-dim);
  font-size:14.5px;line-height:1.55}
.section-label{padding:14px 16px 6px;font-size:12.5px;font-weight:700;
  color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px}

.mini-btn{border:none;font-family:inherit;font-size:13.5px;font-weight:600;
  padding:8px 14px;border-radius:10px;cursor:pointer;
  transition:transform .1s,opacity .15s;touch-action:manipulation;
  display:inline-flex;align-items:center;justify-content:center;gap:6px}
.mini-btn:active{transform:scale(.95)}
.mini-btn.primary{background:var(--text);color:var(--bg)}
.mini-btn.ghost{background:var(--bg-elev);color:var(--text)}
.mini-btn.danger{background:transparent;color:var(--danger)}
.mini-btn svg{width:16px;height:16px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
.mini-btn-row{display:flex;gap:8px;flex-shrink:0;align-items:center}

#auth{justify-content:center;align-items:center;padding:24px;overflow-y:auto}
.auth-wrap{width:100%;max-width:380px;text-align:center;margin:auto}
.logo{width:84px;height:84px;margin:0 auto 22px;background:var(--text);border-radius:26px;
  display:flex;align-items:center;justify-content:center;box-shadow:var(--shadow)}
.logo svg{width:42px;height:42px;stroke:var(--bg);fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
.auth-wrap h1{font-size:27px;font-weight:700;letter-spacing:-.5px;margin-bottom:6px}
.subtitle{color:var(--text-dim);font-size:14.5px;margin-bottom:26px}
.tabs{display:flex;background:var(--bg-elev);border-radius:12px;padding:4px;margin-bottom:20px}
.tab{flex:1;padding:11px;border:none;background:transparent;border-radius:9px;
  font-size:14.5px;font-weight:600;color:var(--text-dim);cursor:pointer;
  transition:all .2s;font-family:inherit;touch-action:manipulation}
.tab.active{background:var(--bg);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.08)}
.input-wrap{display:flex;align-items:center;gap:10px;background:var(--bg-elev);
  border-radius:13px;padding:0 14px;margin-bottom:11px;
  border:1.5px solid transparent;transition:border-color .2s,background .2s}
.input-wrap:focus-within{background:var(--bg);border-color:var(--text)}
.input-wrap svg{width:19px;height:19px;stroke:var(--text-dim);flex-shrink:0;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.input-wrap input{flex:1;border:none;outline:none;background:transparent;padding:16px 0;
  font-size:16.5px;font-family:inherit;color:var(--text);min-width:0}
.input-wrap input::placeholder{color:var(--text-dim)}
.btn-primary{width:100%;padding:17px;background:var(--text);color:var(--bg);border:none;
  border-radius:13px;font-size:16.5px;font-weight:600;cursor:pointer;
  margin-top:4px;transition:transform .1s,opacity .2s;font-family:inherit;touch-action:manipulation;
  display:inline-flex;align-items:center;justify-content:center;gap:8px}
.btn-primary:active{transform:scale(.98)}
.btn-primary:disabled{opacity:.5;cursor:default}
.btn-primary svg{width:18px;height:18px;stroke:currentColor;fill:none;
  stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.error{color:var(--danger);font-size:14px;margin-top:12px;min-height:20px}

.title-block{flex:1;min-width:0;text-align:center;overflow:hidden}
.title-name{font-size:17px;font-weight:600;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.15}
.title-status{font-size:12.5px;color:var(--text-dim);margin-top:1px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  min-height:15px;line-height:15px;transition:color .2s}
.title-status.online{color:var(--ok)}
.title-status.typing{color:var(--ok);font-style:italic}

.messages{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;
  overscroll-behavior:contain;padding:16px 14px 8px;
  display:flex;flex-direction:column;gap:6px;
  background:var(--bg);min-height:0}
.msg{max-width:80%;padding:10px 15px;border-radius:20px;
  font-size:17.5px;line-height:1.42;word-wrap:break-word;overflow-wrap:anywhere;
  white-space:pre-wrap;animation:pop .14s ease;
  position:relative;display:inline-block;align-self:flex-start}
@keyframes pop{from{transform:scale(.94);opacity:0}to{transform:scale(1);opacity:1}}
.msg.me{align-self:flex-end;background:var(--bubble-me);color:var(--bubble-me-fg);
  border-bottom-right-radius:6px}
.msg.them{align-self:flex-start;background:var(--bubble-them);color:var(--bubble-them-fg);
  border-bottom-left-radius:6px}
.msg.pending{opacity:.72}
.msg-status{display:inline-block;margin-left:6px;vertical-align:-2px;
  width:14px;height:14px;opacity:.7}
.msg-status svg{width:14px;height:14px;stroke:currentColor;fill:none;
  stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round;display:block}

.composer{flex-shrink:0;display:flex;align-items:center;gap:8px;padding:10px 12px;
  padding-bottom:calc(10px + env(safe-area-inset-bottom,0));
  border-top:1px solid var(--border);background:var(--bg);z-index:5}
.composer input{flex:1;padding:13px 18px;border:none;outline:none;
  background:var(--bg-elev);border-radius:24px;font-size:17px;
  font-family:inherit;color:var(--text);min-width:0}
.composer input::placeholder{color:var(--text-dim)}
.composer input:disabled{opacity:.55}
.send-btn{width:46px;height:46px;border-radius:50%;border:none;background:var(--text);
  display:flex;align-items:center;justify-content:center;cursor:pointer;
  flex-shrink:0;transition:transform .1s,opacity .2s;color:var(--bg);
  touch-action:manipulation;-webkit-user-select:none;user-select:none}
.send-btn:active{transform:scale(.92)}
.send-btn svg{width:20px;height:20px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
.send-btn:disabled{opacity:.4;cursor:default}

.settings-group{margin:14px 12px;background:var(--bg-elev);border-radius:14px;overflow:hidden}
.set-row{display:flex;align-items:center;justify-content:space-between;padding:15px 16px;gap:12px}
.set-row + .set-row{border-top:1px solid var(--border)}
.set-label{font-size:16px;font-weight:500}
.set-hint{font-size:13px;color:var(--text-dim);margin-top:2px}
.switch{width:52px;height:31px;border-radius:16px;background:var(--border);
  position:relative;cursor:pointer;transition:background .2s;flex-shrink:0;touch-action:manipulation}
.switch::after{content:'';position:absolute;top:2.5px;left:2.5px;
  width:26px;height:26px;border-radius:50%;background:#fff;
  transition:left .2s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
.switch.on{background:var(--ok)}
.switch.on::after{left:23.5px}
.settings-user{display:flex;align-items:center;gap:14px;padding:18px 16px}
.settings-user .avatar{width:58px;height:58px;font-size:22px}
.settings-user-name{font-size:18px;font-weight:700}
.settings-user-sub{font-size:13.5px;color:var(--text-dim);margin-top:2px}
.danger-btn{width:100%;padding:15px;background:transparent;color:var(--danger);
  border:none;border-radius:14px;font-size:16px;font-weight:600;
  cursor:pointer;font-family:inherit;touch-action:manipulation;
  display:inline-flex;align-items:center;justify-content:center;gap:8px}
.danger-btn:active{background:var(--bg-elev)}
.danger-btn svg{width:18px;height:18px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

.search-wrap{display:flex;align-items:center;gap:10px;background:var(--bg-elev);
  border-radius:13px;padding:0 14px;margin:12px 16px 0}
.search-wrap svg{width:19px;height:19px;stroke:var(--text-dim);fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}
.search-wrap input{flex:1;border:none;outline:none;background:transparent;padding:13px 0;
  font-size:16.5px;font-family:inherit;color:var(--text);min-width:0}
.search-wrap input::placeholder{color:var(--text-dim)}

.footnote{text-align:center;font-size:12px;color:var(--text-dim);
  padding:20px 30px 8px;line-height:1.5}

.toast{position:fixed;left:50%;bottom:calc(90px + env(safe-area-inset-bottom,0));
  transform:translateX(-50%) translateY(20px);
  background:var(--text);color:var(--bg);
  padding:11px 18px;border-radius:22px;font-size:14.5px;font-weight:600;
  opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;
  z-index:9999;max-width:90vw;text-align:center}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* ---------- Офлайн-оверлей ---------- */
.offline-overlay{
  position:fixed; inset:0; z-index:10000;
  background:var(--bg);
  display:flex;align-items:center;justify-content:center;
  padding:24px;
  opacity:0; pointer-events:none;
  transition:opacity .25s ease;
}
.offline-overlay.show{opacity:1;pointer-events:auto}
.offline-card{max-width:340px;width:100%;text-align:center}
.offline-icon-wrap{
  width:96px;height:96px;margin:0 auto 22px;
  border-radius:28px;background:var(--bg-elev);
  display:flex;align-items:center;justify-content:center;
  animation:bob 2.4s ease-in-out infinite;
}
@keyframes bob{
  0%,100%{transform:translateY(0)}
  50%{transform:translateY(-6px)}
}
.offline-icon-wrap svg{width:48px;height:48px;stroke:var(--text);
  fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.offline-card h2{font-size:22px;font-weight:700;margin-bottom:8px;letter-spacing:-.3px}
.offline-card p{color:var(--text-dim);font-size:14.5px;line-height:1.5;margin-bottom:24px}
.offline-retry{
  padding:14px 26px;border-radius:14px;border:none;
  background:var(--text);color:var(--bg);
  font-family:inherit;font-size:15.5px;font-weight:600;cursor:pointer;
  display:inline-flex;align-items:center;gap:9px;
  transition:transform .1s,opacity .2s;touch-action:manipulation;
}
.offline-retry:active{transform:scale(.96)}
.offline-retry:disabled{opacity:.5}
.offline-retry svg{width:18px;height:18px;stroke:currentColor;fill:none;
  stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.offline-retry.spin svg{animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.offline-sub{font-size:12.5px;color:var(--text-dim);margin-top:16px;
  min-height:16px}

/* Тонкий баннер реконнекта сверху */
.reconnect-banner{
  position:fixed;top:0;left:0;right:0;z-index:9998;
  padding:calc(env(safe-area-inset-top,0) + 8px) 16px 8px;
  background:var(--warn);color:#111;
  font-size:13px;font-weight:600;text-align:center;
  transform:translateY(-120%);transition:transform .3s ease;
}
.reconnect-banner.show{transform:translateY(0)}

/* Уменьшаем визуальный шум на маленьких экранах */
@media (max-width:360px){
  .msg{font-size:16.5px}
  .row-title{font-size:15.5px}
  .row-sub{font-size:13.5px}
}
</style>
</head>
<body>

<!-- Баннер переподключения -->
<div class="reconnect-banner" id="reconnect-banner">Переподключение…</div>

<!-- Полноэкранный офлайн -->
<div class="offline-overlay" id="offline-overlay">
  <div class="offline-card">
    <div class="offline-icon-wrap" id="offline-icon-wrap">
      <!-- иконка вставится из JS -->
    </div>
    <h2 id="offline-title">Нет подключения</h2>
    <p id="offline-text">Проверьте интернет и попробуйте снова</p>
    <button class="offline-retry" id="offline-retry" type="button">
      <span id="offline-retry-icon"></span>
      <span>Повторить</span>
    </button>
    <div class="offline-sub" id="offline-sub"></div>
  </div>
</div>

<div id="app">

  <!-- AUTH -->
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

  <!-- MAIN -->
  <div id="main" class="screen">
    <header class="topbar glass">
      <div class="title">
        <span class="conn-dot" id="conn-dot" title="Соединение"></span>
        <span class="title-text" id="main-title">Чаты</span>
      </div>
      <button class="icon-btn" id="quick-theme" aria-label="Тема">
        <svg id="quick-theme-icon" viewBox="0 0 24 24"></svg>
      </button>
    </header>

    <div class="page-area">
      <div class="page active" id="page-home">
        <div class="page-scroll" id="home-list"></div>
      </div>

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

      <div class="page" id="page-settings">
        <div class="page-scroll">
          <div class="settings-group">
            <div class="settings-user">
              <div class="avatar" id="me-avatar">?</div>
              <div>
                <div class="settings-user-name" id="me-name">—</div>
                <div class="settings-user-sub" id="me-status">не в сети</div>
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
            <div class="set-row">
              <div>
                <div class="set-label">Вибрация</div>
                <div class="set-hint">Отклик при отправке</div>
              </div>
              <div class="switch" id="set-haptic"></div>
            </div>
          </div>

          <div class="settings-group">
            <button class="danger-btn" id="logout-btn">
              <svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
              <span>Выйти из аккаунта</span>
            </button>
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

  <!-- CHAT -->
  <div id="chat" class="screen">
    <header class="topbar glass">
      <button class="icon-btn" id="chat-back" aria-label="Назад">
        <svg viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
      </button>
      <div class="title-block">
        <div class="title-name" id="chat-title">—</div>
        <div class="title-status" id="chat-status"></div>
      </div>
      <div style="width:44px"></div>
    </header>
    <div class="messages" id="messages"></div>
    <div class="composer glass">
      <input type="text" id="msg-input" placeholder="Сообщение..." maxlength="2000" autocomplete="off" enterkeyhint="send">
      <button class="send-btn" id="send-btn" type="button" aria-label="Отправить">
        <svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      </button>
    </div>
  </div>

</div>
<div class="toast" id="toast"></div>

<script>
(function(){
"use strict";

/* =========================================================
   КОНСТАНТЫ
   ========================================================= */
const $  = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

const STORAGE = {
  token:  'm_token',
  user:   'm_user',
  theme:  'm_theme',
  glass:  'm_glass',
  notif:  'm_notif',
  haptic: 'm_haptic',
  open:   'm_open_after_login',
};

const RECONNECT_BASE = 800;
const RECONNECT_MAX  = 8000;
const RECONNECT_FAIL_THRESHOLD = 3; // сколько неудач подряд → показать офлайн
const TYPING_TTL = 3500;

/* Иконки (Feather-style, 24x24) */
const ICONS = {
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/>',
  wifiOff: '<line x1="1" y1="1" x2="23" y2="23"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/><path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/><path d="M10.71 5.05A16 16 0 0 1 22.58 9"/><path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>',
  refresh: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/><path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14"/>',
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  alert: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  send: '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
};

/* =========================================================
   СОСТОЯНИЕ
   ========================================================= */
const state = {
  token:  localStorage.getItem(STORAGE.token) || '',
  username: localStorage.getItem(STORAGE.user) || '',
  theme:  localStorage.getItem(STORAGE.theme) || 'light',
  glass:  localStorage.getItem(STORAGE.glass) || 'on',
  notif:  localStorage.getItem(STORAGE.notif) === '1',
  haptic: localStorage.getItem(STORAGE.haptic) !== '0',

  ws: null,
  wsReady: false,
  wsReconnectAttempts: 0,
  reconnectTimer: null,
  connState: 'offline', // 'online' | 'connecting' | 'offline'

  page: 'home',
  friendsTab: 'friends',

  chats: [],
  friendsList: [],
  onlineFriends: new Set(),
  incoming: [],
  outgoing: [],
  searchResults: [],
  searchQuery: '',

  activePeer: null,
  seenIds: new Set(),
  pendingCids: new Map(),   // cid -> DOM element

  typing: {},               // peer -> timestamp
  typingTimers: {},
  typingSendTimer: null,

  renderQueued: false,
  swReady: false,
};

/* =========================================================
   ХЕЛПЕРЫ
   ========================================================= */
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
}[c]));

function fmtTime(ts){
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString())
    return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return 'вчера';
  return String(d.getDate()).padStart(2,'0') + '.' + String(d.getMonth()+1).padStart(2,'0');
}

let _toastTimer = null;
function toast(msg){
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
}

function haptic(ms){
  if (!state.haptic) return;
  try { navigator.vibrate && navigator.vibrate(ms || 10); } catch(_){}
}

function isTyping(peer){
  const t = state.typing[peer];
  return t && (Date.now() - t) < TYPING_TTL;
}

function isOnline(peer){ return state.onlineFriends.has(peer); }

function makeCid(){
  return 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

/* rAF-батч рендера */
function scheduleRender(fn){
  if (state.renderQueued) return;
  state.renderQueued = true;
  requestAnimationFrame(() => {
    state.renderQueued = false;
    try { fn(); } catch(e){ console.warn(e); }
  });
}

/* =========================================================
   ТЕМА / СТЕКЛО / НАСТРОЙКИ
   ========================================================= */
function applyTheme(){
  document.documentElement.setAttribute('data-theme', state.theme);
  const meta = document.querySelector('meta[name=theme-color]');
  if (meta) meta.setAttribute('content', state.theme === 'dark' ? '#0b0b0c' : '#ffffff');

  const icon = $('#quick-theme-icon');
  if (icon) {
    icon.innerHTML = state.theme === 'dark' ? ICONS.sun : ICONS.moon;
  }
  const sw = $('#set-theme');
  if (sw) sw.classList.toggle('on', state.theme === 'dark');
}

function applyGlass(){
  document.documentElement.setAttribute('data-glass', state.glass);
  const sw = $('#set-glass');
  if (sw) sw.classList.toggle('on', state.glass === 'on');
}

function applyNotifUI(){
  const sw = $('#set-notif'); if (!sw) return;
  const supported = 'Notification' in window;
  const granted = supported && Notification.permission === 'granted';
  sw.classList.toggle('on', granted && state.notif);
  const hint = $('#notif-hint');
  if (!hint) return;
  if (!supported) hint.textContent = 'Не поддерживается браузером';
  else if (Notification.permission === 'denied') hint.textContent = 'Запрещено в браузере';
  else if (granted) hint.textContent = state.notif ? 'Уведомления включены' : 'Уведомления отключены';
  else hint.textContent = 'Нажмите, чтобы разрешить';
}

function applyHapticUI(){
  const sw = $('#set-haptic');
  if (sw) sw.classList.toggle('on', state.haptic);
}

/* =========================================================
   VIEWPORT / КЛАВИАТУРА
   ========================================================= */
function updateViewport(){
  const vv = window.visualViewport;
  const h = vv ? vv.height : window.innerHeight;
  document.documentElement.style.setProperty('--app-h', h + 'px');
  const app = document.getElementById('app');
  if (app){
    const top = vv ? Math.max(0, vv.offsetTop) : 0;
    app.style.transform = top > 0 ? 'translateY(' + top + 'px)' : '';
  }
}

if (window.visualViewport){
  window.visualViewport.addEventListener('resize', () => {
    updateViewport();
    if (state.activePeer) setTimeout(() => scrollMessages(), 30);
  });
  window.visualViewport.addEventListener('scroll', () => {
    updateViewport();
    window.scrollTo(0, 0);
  });
}
window.addEventListener('orientationchange', () => setTimeout(updateViewport, 200));
document.addEventListener('focusin', () => setTimeout(() => window.scrollTo(0, 0), 80));
updateViewport();

/* =========================================================
   СОЕДИНЕНИЕ / OFFLINE UI
   ========================================================= */
const connUI = {
  dot: () => $('#conn-dot'),
  banner: () => $('#reconnect-banner'),
  overlay: () => $('#offline-overlay'),
  title: () => $('#offline-title'),
  text: () => $('#offline-text'),
  sub: () => $('#offline-sub'),
  retry: () => $('#offline-retry'),
  retryIcon: () => $('#offline-retry-icon'),
  iconWrap: () => $('#offline-icon-wrap'),
};

let _overlayHideTimer = null;

function setConnState(next){
  if (state.connState === next) return;
  state.connState = next;

  const dot = connUI.dot();
  if (dot){
    dot.classList.toggle('online', next === 'online');
    dot.classList.toggle('connecting', next === 'connecting');
  }

  const st = $('#me-status');
  if (st){
    if (next === 'online'){
      st.textContent = 'в сети';
      st.style.color = 'var(--ok)';
    } else if (next === 'connecting'){
      st.textContent = 'подключение…';
      st.style.color = 'var(--text-dim)';
    } else {
      st.textContent = 'нет соединения';
      st.style.color = 'var(--text-dim)';
    }
  }

  const banner = connUI.banner();
  if (banner) banner.classList.toggle('show', next === 'connecting' && !state.wsReady);

  const overlay = connUI.overlay();
  if (!overlay) return;

  clearTimeout(_overlayHideTimer);

  if (next === 'offline'){
    // Показываем офлайн-оверлей
    const iconWrap = connUI.iconWrap();
    if (iconWrap && !iconWrap.dataset.filled){
      iconWrap.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.wifiOff + '</svg>';
      iconWrap.dataset.filled = '1';
    }
    const ri = connUI.retryIcon();
    if (ri) ri.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.refresh + '</svg>';

    const t = connUI.title(); if (t) t.textContent = 'Нет подключения';
    const p = connUI.text();  if (p) p.textContent = 'Проверьте интернет и попробуйте снова';
    const s = connUI.sub();   if (s) s.textContent = navigator.onLine ? '' : 'Устройство не в сети';

    overlay.classList.add('show');
  } else {
    // Плавно скрываем
    overlay.classList.remove('show');
  }
}

function onBrowserOnline(){
  if (state.connState === 'online') return;
  setConnState('connecting');
  // Форсируем реконнект
  clearTimeout(state.reconnectTimer);
  state.wsReconnectAttempts = 0;
  connectWS();
}

function onBrowserOffline(){
  setConnState('offline');
  // Пытаться бессмысленно, но WS сам закроется
  try { state.ws && state.ws.close(); } catch(_){}
}

window.addEventListener('online', onBrowserOnline);
window.addEventListener('offline', onBrowserOffline);

/* =========================================================
   API (fetch)
   ========================================================= */
async function api(path, opts){
  opts = opts || {};
  opts.headers = Object.assign({
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + state.token,
  }, opts.headers || {});
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e){
    setConnState('offline');
    throw new Error('Нет соединения');
  }
  if (r.status === 401 && state.token){ doLogout(); throw new Error('unauth'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}

/* =========================================================
   АВТОРИЗАЦИЯ
   ========================================================= */
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
    localStorage.setItem(STORAGE.token, state.token);
    localStorage.setItem(STORAGE.user, state.username);
    $('#password').value = '';
    haptic(15);
    await enterApp();
  } catch (err){
    errEl.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

function doLogout(){
  localStorage.removeItem(STORAGE.token);
  localStorage.removeItem(STORAGE.user);
  state.token = '';
  state.username = '';
  state.activePeer = null;
  state.seenIds.clear();
  state.pendingCids.clear();
  state.friendsList = [];
  state.onlineFriends.clear();
  state.chats = [];
  state.incoming = [];
  state.outgoing = [];
  state.typing = {};
  if (state.ws){ try { state.ws.close(); } catch(e){} state.ws = null; }
  clearTimeout(state.reconnectTimer);
  state.wsReady = false;
  state.wsReconnectAttempts = 0;
  setConnState('offline');
  showScreen('auth');
}
$('#logout-btn').addEventListener('click', doLogout);

/* =========================================================
   НАВИГАЦИЯ
   ========================================================= */
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

/* =========================================================
   СТАРТ ПОСЛЕ ЛОГИНА
   ========================================================= */
async function enterApp(){
  $('#me-avatar').textContent = (state.username[0] || '?').toUpperCase();
  $('#me-name').textContent = state.username;
  applyTheme(); applyGlass(); applyNotifUI(); applyHapticUI();
  setConnState('connecting');
  connectWS();
  switchPage('home');
  showScreen('main');
  updateViewport();

  // SW
  if ('serviceWorker' in navigator){
    try {
      const reg = await navigator.serviceWorker.register('/sw.js');
      state.swReady = true;
      try { reg.update(); } catch(_){}
    } catch(e){}
    navigator.serviceWorker.addEventListener('message', (e) => {
      if (e.data && e.data.type === 'open-chat' && e.data.peer){
        tryOpenChat(e.data.peer);
      }
    });
  }

  // Открыть чат, если пришли по клику на уведомление
  const params = new URLSearchParams(location.search);
  const peerFromUrl = params.get('open');
  if (peerFromUrl){
    localStorage.setItem(STORAGE.open, peerFromUrl);
    history.replaceState({}, '', '/');
  }
  const pendingOpen = localStorage.getItem(STORAGE.open);
  if (pendingOpen){
    localStorage.removeItem(STORAGE.open);
    setTimeout(() => tryOpenChat(pendingOpen), 350);
  }
}

function tryOpenChat(peer){
  if (!peer) return;
  showScreen('main');
  // небольшая задержка, чтобы friendsList успел подгрузиться
  setTimeout(() => openChat(peer), 100);
}

/* =========================================================
   WEBSOCKET
   ========================================================= */
function connectWS(){
  if (!state.token) return;
  if (!navigator.onLine){ setConnState('offline'); return; }

  if (state.ws && (state.ws.readyState === 0 || state.ws.readyState === 1)) return;

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = proto + '://' + location.host + '/ws?token=' + encodeURIComponent(state.token);
  let ws;
  try { ws = new WebSocket(url); } catch(e){
    scheduleReconnect();
    return;
  }
  state.ws = ws;

  ws.onopen = () => {
    state.wsReady = true;
    state.wsReconnectAttempts = 0;
    setConnState('online');
  };

  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(e.data); } catch(_){ return; }
    handleServerEvent(m);
  };

  ws.onclose = (e) => {
    state.wsReady = false;
    if (state.ws === ws) state.ws = null;
    if (e.code === 4000) return; // заменён другой вкладкой
    if (!state.token) return;

    if (!navigator.onLine){
      setConnState('offline');
      return;
    }
    scheduleReconnect();
  };

  ws.onerror = () => {};
}

function scheduleReconnect(){
  state.wsReconnectAttempts++;
  if (state.wsReconnectAttempts >= RECONNECT_FAIL_THRESHOLD){
    setConnState('offline');
  } else {
    setConnState('connecting');
  }
  const delay = Math.min(
    RECONNECT_MAX,
    RECONNECT_BASE * Math.pow(1.6, state.wsReconnectAttempts - 1)
  ) + Math.random() * 400;

  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = setTimeout(() => {
    if (!state.token) return;
    connectWS();
  }, delay);
}

function wsSend(obj){
  if (state.ws && state.ws.readyState === 1){
    try { state.ws.send(JSON.stringify(obj)); return true; } catch(e){}
  }
  return false;
}

/* =========================================================
   ОБРАБОТКА СОБЫТИЙ СЕРВЕРА
   ========================================================= */
function handleServerEvent(m){
  switch (m.type){
    case 'hello': return;

    case 'presence': {
      if (m.online) state.onlineFriends.add(m.user);
      else state.onlineFriends.delete(m.user);
      // Обновление в открытом чате
      if (state.activePeer === m.user) updateChatHeaderStatus();
      // Обновление видимых списков
      updatePresenceInPlace(m.user);
      return;
    }

    case 'message': {
      const mine = m.from === state.username;
      const peer = mine ? m.to : m.from;

      // Апдейт в открытом чате
      if (state.activePeer === peer){
        if (m.cid && state.pendingCids.has(m.cid)){
          markDelivered(m.cid);
        } else if (m.id && !state.seenIds.has(m.id)){
          state.seenIds.add(m.id);
          appendMessage({ id: m.id, from: m.from, text: m.text, ts: m.ts, delivered: true });
          scrollMessages(true);
          if (!mine) wsSend({ type: 'read', peer });
        }
      } else if (!mine){
        maybeNotify(m);
      }

      // Обновим локальный кэш чатов
      updateChatFromMessage(m);

      // Сбросить "печатает" у собеседника, раз он прислал сообщение
      if (!mine && state.typing[peer]){
        delete state.typing[peer];
        if (state.activePeer === peer) updateChatHeaderStatus();
      }
      return;
    }

    case 'chat_read': {
      const c = state.chats.find(x => x.username === m.peer);
      if (c) c.unread = 0;
      scheduleRender(() => {
        if (state.page === 'home') renderChats();
        updateDots();
      });
      return;
    }

    case 'friends_changed': {
      refreshFriends().then(() => {
        if (state.activePeer && !state.friendsList.includes(state.activePeer)){
          toast('Диалог закрыт');
          state.activePeer = null;
          showScreen('main');
        }
      });
      refreshChats();
      return;
    }

    case 'typing': {
      if (!m.from) return;
      state.typing[m.from] = Date.now();
      clearTimeout(state.typingTimers[m.from]);
      state.typingTimers[m.from] = setTimeout(() => {
        delete state.typing[m.from];
        if (state.activePeer === m.from) updateChatHeaderStatus();
        updateTypingInPlace(m.from);
      }, TYPING_TTL);
      if (state.activePeer === m.from) updateChatHeaderStatus();
      updateTypingInPlace(m.from);
      return;
    }
  }
}

/* Точечное обновление «онлайн» без полного ре-рендера */
function updatePresenceInPlace(user){
  // аватары
  document.querySelectorAll('.row[data-peer="' + CSS.escape(user) + '"] .online-dot').forEach(d => {
    d.classList.toggle('on', state.onlineFriends.has(user));
  });
  document.querySelectorAll('.row[data-user="' + CSS.escape(user) + '"] .online-dot').forEach(d => {
    d.classList.toggle('on', state.onlineFriends.has(user));
  });
  // статус под именем в списке друзей
  const fr = document.querySelector('#friends-list .row[data-user="' + CSS.escape(user) + '"] .row-sub');
  if (fr && !isTyping(user)){
    fr.textContent = isOnline(user) ? 'в сети' : 'не в сети';
    fr.classList.remove('typing');
  }
}

function updateTypingInPlace(user){
  const typing = isTyping(user);
  // в чатах
  const cr = document.querySelector('#home-list .row[data-peer="' + CSS.escape(user) + '"] .row-sub');
  if (cr){
    if (typing){
      cr.classList.add('typing');
      cr.textContent = 'печатает…';
    } else {
      cr.classList.remove('typing');
      const c = state.chats.find(x => x.username === user);
      if (c) cr.textContent = c.lastText ? (c.lastFromMe ? 'Вы: ' : '') + c.lastText : 'нет сообщений';
    }
  }
  // в друзьях
  const fr = document.querySelector('#friends-list .row[data-user="' + CSS.escape(user) + '"] .row-sub');
  if (fr){
    if (typing){
      fr.classList.add('typing');
      fr.textContent = 'печатает…';
    } else {
      fr.classList.remove('typing');
      fr.textContent = isOnline(user) ? 'в сети' : 'не в сети';
    }
  }
}

/* =========================================================
   ЛОКАЛЬНЫЙ КЭШ ЧАТОВ
   ========================================================= */
function updateChatFromMessage(m){
  const mine = m.from === state.username;
  const peer = mine ? m.to : m.from;
  let c = state.chats.find(x => x.username === peer);
  if (!c){
    // чата ещё нет локально — обновим список с сервера
    refreshChats();
    return;
  }
  c.lastText = m.text;
  c.lastTs = m.ts;
  c.lastFromMe = mine;
  if (!mine && state.activePeer !== peer) c.unread = (c.unread || 0) + 1;

  state.chats.sort((a,b) =>
    (b.lastTs || 0) - (a.lastTs || 0) ||
    a.username.localeCompare(b.username)
  );

  scheduleRender(() => {
    if (state.page === 'home') renderChats();
    updateDots();
  });
}

/* =========================================================
   УВЕДОМЛЕНИЯ
   ========================================================= */
function maybeNotify(m){
  if (!state.notif) return;
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  if (!document.hidden && state.activePeer === m.from) return;

  const title = m.from;
  const body = m.text.length > 80 ? m.text.slice(0, 77) + '…' : m.text;
  const opts = {
    body,
    tag: 'msg-' + m.from,
    renotify: true,
    icon: '/icon.svg',
    badge: '/icon.svg',
    data: { peer: m.from },
  };
  try {
    if (state.swReady && navigator.serviceWorker && navigator.serviceWorker.ready){
      navigator.serviceWorker.ready
        .then(reg => { try { reg.showNotification(title, opts); } catch(_){ fallbackNotify(title, opts); } })
        .catch(() => fallbackNotify(title, opts));
    } else {
      fallbackNotify(title, opts);
    }
  } catch(e){}
}
function fallbackNotify(title, opts){
  try { new Notification(title, opts); } catch(_){}
}

/* =========================================================
   СПИСОК ЧАТОВ
   ========================================================= */
async function refreshChats(){
  if (!state.token) return;
  try {
    const list = await api('/api/chats');
    state.chats = list;
    for (const c of list){
      if (c.online) state.onlineFriends.add(c.username);
      else state.onlineFriends.delete(c.username);
    }
    scheduleRender(() => { renderChats(); updateDots(); });
  } catch(e){}
}

function renderChats(){
  const box = $('#home-list');
  if (!state.chats.length){
    box.innerHTML = '<div class="empty">Пока нет чатов.<br>Перейдите во вкладку «Друзья»,<br>чтобы найти собеседников.</div>';
    return;
  }

  const frag = document.createDocumentFragment();
  for (const c of state.chats){
    const initial = (c.username[0] || '?').toUpperCase();
    const online = isOnline(c.username);
    const typing = isTyping(c.username);

    const row = document.createElement('div');
    row.className = 'row';
    row.dataset.peer = c.username;

    let subHTML;
    if (typing){
      subHTML = '<div class="row-sub typing">печатает…</div>';
    } else if (c.lastText){
      subHTML = '<div class="row-sub">' + esc((c.lastFromMe ? 'Вы: ' : '') + c.lastText) + '</div>';
    } else {
      subHTML = '<div class="row-sub">нет сообщений</div>';
    }

    row.innerHTML =
      '<div class="avatar">' + esc(initial) +
        '<span class="online-dot ' + (online ? 'on' : '') + '"></span>' +
      '</div>' +
      '<div class="row-info">' +
        '<div class="row-title">' + esc(c.username) + '</div>' + subHTML +
      '</div>' +
      '<div class="row-right">' +
        (c.lastTs ? '<div class="row-time">' + esc(fmtTime(c.lastTs)) + '</div>' : '') +
        (c.unread ? '<div class="badge">' + c.unread + '</div>' : '') +
      '</div>';

    frag.appendChild(row);
  }
  box.replaceChildren(frag);
}

/* =========================================================
   ДРУЗЬЯ
   ========================================================= */
function updateDots(){
  const homeUnread = state.chats.reduce((s,c) => s + (c.unread || 0), 0);
  const hd = $('#home-dot');
  if (homeUnread > 0){
    hd.textContent = homeUnread > 99 ? '99+' : homeUnread;
    hd.style.display = 'flex';
  } else hd.style.display = 'none';

  const pendingCount = state.incoming.length;
  const fd = $('#friends-dot');
  if (pendingCount > 0){
    fd.textContent = pendingCount > 99 ? '99+' : pendingCount;
    fd.style.display = 'flex';
  } else fd.style.display = 'none';

  const pb = $('#pending-badge');
  pb.innerHTML = pendingCount > 0
    ? '<span class="badge-inline">' + pendingCount + '</span>'
    : '';
}

async function refreshFriends(){
  if (!state.token) return;
  try {
    const data = await api('/api/friends');
    state.friendsList = data.friends || [];
    state.incoming = data.incoming || [];
    state.outgoing = data.outgoing || [];
    state.onlineFriends = new Set(data.online || []);
    scheduleRender(() => { updateDots(); renderFriends(); });
  } catch(e){}
}

$$('#friends-seg .seg').forEach(b => b.addEventListener('click', () => {
  state.friendsTab = b.dataset.tab;
  $$('#friends-seg .seg').forEach(x => x.classList.toggle('active', x === b));
  renderFriends();
}));

function renderFriends(){
  const box = $('#friends-list');
  if (state.searchQuery){ renderSearch(box); return; }

  if (state.friendsTab === 'friends'){
    if (!state.friendsList.length){
      box.innerHTML = '<div class="empty">Пока нет друзей.<br>Найдите людей через поиск выше.</div>';
      return;
    }

    const frag = document.createDocumentFragment();
    for (const name of state.friendsList){
      const initial = (name[0] || '?').toUpperCase();
      const online = isOnline(name);
      const typing = isTyping(name);
      const status = typing ? 'печатает…' : (online ? 'в сети' : 'не в сети');
      const statusCls = typing ? 'row-sub typing' : 'row-sub';

      const row = document.createElement('div');
      row.className = 'row';
      row.dataset.user = name;
      row.innerHTML =
        '<div class="avatar">' + esc(initial) +
          '<span class="online-dot ' + (online ? 'on' : '') + '"></span>' +
        '</div>' +
        '<div class="row-info">' +
          '<div class="row-title">' + esc(name) + '</div>' +
          '<div class="' + statusCls + '">' + status + '</div>' +
        '</div>' +
        '<div class="mini-btn-row">' +
          '<button class="mini-btn primary" data-act="open" data-user="' + esc(name) + '">Написать</button>' +
          '<button class="mini-btn danger" data-act="unfriend" data-user="' + esc(name) + '" aria-label="Удалить">' +
            '<svg viewBox="0 0 24 24">' + ICONS.trash + '</svg>' +
          '</button>' +
        '</div>';
      frag.appendChild(row);
    }
    box.replaceChildren(frag);
    return;
  }

  // pending
  const frag = document.createDocumentFragment();
  if (state.incoming.length){
    frag.appendChild(makeLabel('Входящие'));
    for (const name of state.incoming) frag.appendChild(rowPending(name, 'incoming'));
  }
  if (state.outgoing.length){
    frag.appendChild(makeLabel('Исходящие'));
    for (const name of state.outgoing) frag.appendChild(rowPending(name, 'outgoing'));
  }
  if (!frag.childNodes.length){
    box.innerHTML = '<div class="empty">Нет активных заявок.</div>';
    return;
  }
  box.replaceChildren(frag);
}

function makeLabel(text){
  const el = document.createElement('div');
  el.className = 'section-label';
  el.textContent = text;
  return el;
}

function rowPending(name, kind){
  const initial = (name[0] || '?').toUpperCase();
  const online = isOnline(name);
  const row = document.createElement('div');
  row.className = 'row';
  row.dataset.user = name;

  const btns = kind === 'incoming'
    ? '<button class="mini-btn primary" data-act="accept" data-user="' + esc(name) + '">Принять</button>' +
      '<button class="mini-btn ghost" data-act="decline" data-user="' + esc(name) + '">Отклонить</button>'
    : '<button class="mini-btn ghost" data-act="cancel" data-user="' + esc(name) + '">Отменить</button>';

  row.innerHTML =
    '<div class="avatar">' + esc(initial) +
      '<span class="online-dot ' + (online ? 'on' : '') + '"></span>' +
    '</div>' +
    '<div class="row-info">' +
      '<div class="row-title">' + esc(name) + '</div>' +
      '<div class="row-sub">' + (kind === 'incoming' ? 'хочет добавить вас' : 'заявка отправлена') + '</div>' +
    '</div>' +
    '<div class="mini-btn-row">' + btns + '</div>';
  return row;
}

function renderSearch(box){
  if (!state.searchResults.length){
    box.innerHTML = '<div class="empty">Ничего не найдено.</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const u of state.searchResults){
    const initial = (u.username[0] || '?').toUpperCase();
    const online = !!u.online;
    let btn = '';
    if (u.status === 'friend')
      btn = '<button class="mini-btn primary" data-act="open" data-user="' + esc(u.username) + '">Написать</button>';
    else if (u.status === 'outgoing')
      btn = '<button class="mini-btn ghost" data-act="cancel" data-user="' + esc(u.username) + '">Отменить</button>';
    else if (u.status === 'incoming')
      btn = '<button class="mini-btn primary" data-act="accept" data-user="' + esc(u.username) + '">Принять</button>';
    else
      btn = '<button class="mini-btn primary" data-act="request" data-user="' + esc(u.username) + '">Добавить</button>';

    const row = document.createElement('div');
    row.className = 'row';
    row.dataset.user = u.username;
    row.innerHTML =
      '<div class="avatar">' + esc(initial) +
        '<span class="online-dot ' + (online ? 'on' : '') + '"></span>' +
      '</div>' +
      '<div class="row-info"><div class="row-title">' + esc(u.username) + '</div></div>' +
      '<div class="mini-btn-row">' + btn + '</div>';
    frag.appendChild(row);
  }
  box.replaceChildren(frag);
}

/* =========================================================
   ДЕЛЕГИРОВАНИЕ КЛИКОВ ПО СПИСКАМ
   ========================================================= */
function attachListDelegate(container, onRow, onAction){
  container.addEventListener('click', (e) => {
    const actionEl = e.target.closest('button[data-act]');
    if (actionEl){
      e.stopPropagation();
      onAction(actionEl.dataset.act, actionEl.dataset.user);
      return;
    }
    const rowEl = e.target.closest('.row');
    if (!rowEl) return;
    const peer = rowEl.dataset.peer || rowEl.dataset.user;
    if (peer) onRow(peer);
  });
}

attachListDelegate(
  $('#home-list'),
  (peer) => openChat(peer),
  (act, user) => friendAction(act, user),
);

attachListDelegate(
  $('#friends-list'),
  () => {},
  (act, user) => friendAction(act, user),
);

/* =========================================================
   ДЕЙСТВИЯ ДРУЖБЫ
   ========================================================= */
async function friendAction(act, user){
  try {
    if (act === 'open'){ openChat(user); return; }
    if (act === 'unfriend'){
      if (!confirm('Удалить ' + user + ' из друзей?\nВся переписка тоже будет удалена.')) return;
      await api('/api/friends/remove', { method: 'POST', body: JSON.stringify({target:user}) });
      if (state.activePeer === user){
        state.activePeer = null;
        showScreen('main');
      }
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
    await refreshChats();
    if (state.searchQuery) await runSearch(state.searchQuery);
  } catch(err){
    toast(err.message || 'Ошибка');
  }
}

/* =========================================================
   ПОИСК
   ========================================================= */
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
    if (state.searchQuery !== q) return; // устаревший ответ
    state.searchResults = res;
    scheduleRender(() => renderFriends());
  } catch(e){}
}

/* =========================================================
   ЧАТ
   ========================================================= */
function updateChatHeaderStatus(){
  const el = $('#chat-status');
  if (!el || !state.activePeer) return;
  if (isTyping(state.activePeer)){
    el.textContent = 'печатает…';
    el.className = 'title-status typing';
    return;
  }
  if (isOnline(state.activePeer)){
    el.textContent = 'в сети';
    el.className = 'title-status online';
  } else {
    el.textContent = 'не в сети';
    el.className = 'title-status';
  }
}

async function openChat(peer){
  state.activePeer = peer;
  state.seenIds.clear();
  state.pendingCids.clear();

  $('#chat-title').textContent = peer;
  $('#messages').replaceChildren();
  updateChatHeaderStatus();
  showScreen('chat');
  updateViewport();

  try {
    const list = await api('/api/messages/' + encodeURIComponent(peer));
    const frag = document.createDocumentFragment();
    for (const m of list){
      state.seenIds.add(m.id);
      const el = buildMessageEl({
        id: m.id, from: m.from, text: m.text, ts: m.ts,
        delivered: true,
      });
      frag.appendChild(el);
    }
    $('#messages').replaceChildren(frag);
    scrollMessages();
    wsSend({ type:'read', peer });

    const c = state.chats.find(x => x.username === peer);
    if (c){ c.unread = 0; renderChats(); updateDots(); }
  } catch(e){
    toast(e.message || 'Ошибка загрузки');
    if (e.message && e.message.indexOf('Не друзья') >= 0){
      state.activePeer = null;
      showScreen('main');
      return;
    }
  }
  setTimeout(() => {
    try { $('#msg-input').focus({preventScroll:true}); } catch(_){}
  }, 50);
}

function buildMessageEl(m){
  const el = document.createElement('div');
  el.className = 'msg ' + (m.from === state.username ? 'me' : 'them');
  if (m.cid && state.pendingCids.has(m.cid) === false && !m.delivered) el.classList.add('pending');

  const textNode = document.createElement('span');
  textNode.textContent = m.text;
  el.appendChild(textNode);

  if (m.from === state.username){
    const status = document.createElement('span');
    status.className = 'msg-status';
    status.innerHTML = '<svg viewBox="0 0 24 24">' +
      (m.delivered ? ICONS.check : ICONS.clock) + '</svg>';
    el.appendChild(status);
    if (m.cid) el.dataset.cid = m.cid;
  }
  return el;
}

function appendMessage(m){
  const box = $('#messages');
  const el = buildMessageEl(m);
  if (m.cid){
    el.dataset.cid = m.cid;
    // если это наше оптимистично добавленное сообщение, тут уже есть
    if (state.pendingCids.has(m.cid)){
      // уже в дереве
      return;
    }
  }
  box.appendChild(el);
}

function markDelivered(cid){
  const el = state.pendingCids.get(cid);
  if (!el) return;
  el.classList.remove('pending');
  const st = el.querySelector('.msg-status');
  if (st) st.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.check + '</svg>';
  state.pendingCids.delete(cid);
}

function scrollMessages(){
  const box = $('#messages');
  requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
}

$('#chat-back').addEventListener('click', () => {
  state.activePeer = null;
  state.seenIds.clear();
  state.pendingCids.clear();
  showScreen('main');
  if (state.page === 'home') refreshChats();
  setTimeout(updateViewport, 50);
});

/* -------- Отправка -------- */
function sendMessage(){
  if (!state.activePeer) return;
  const input = $('#msg-input');
  const text = input.value.trim();
  if (!text) return;

  if (!state.wsReady){
    toast('Нет соединения');
    haptic([8, 40, 8]);
    return;
  }

  const cid = makeCid();
  const optimistic = {
    from: state.username,
    text,
    ts: Date.now() / 1000,
    delivered: false,
    cid,
  };
  const el = buildMessageEl(optimistic);
  el.classList.add('pending');
  el.dataset.cid = cid;
  $('#messages').appendChild(el);
  state.pendingCids.set(cid, el);
  scrollMessages();

  const ok = wsSend({ type:'send', to: state.activePeer, text, cid });
  if (!ok){
    el.classList.remove('pending');
    state.pendingCids.delete(cid);
    toast('Нет соединения');
    haptic([8, 40, 8]);
    return;
  }

  input.value = '';
  try { input.focus({preventScroll:true}); } catch(_){}
  haptic(10);

  // локальный кэш чата (мгновенное обновление списка)
  updateChatFromMessage({
    from: state.username,
    to: state.activePeer,
    text,
    ts: optimistic.ts,
  });
}

/* Кнопка отправки — фиксим мобильные */
(function bindSend(){
  const btn = $('#send-btn');
  if (!btn) return;
  btn.addEventListener('pointerdown', (e) => {
    // не забираем фокус с поля
    e.preventDefault();
  });
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    sendMessage();
  });
  if (!('PointerEvent' in window)){
    btn.addEventListener('touchend', (e) => {
      e.preventDefault();
      sendMessage();
    }, {passive:false});
  }
})();

$('#msg-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey){
    e.preventDefault();
    sendMessage();
  }
});

$('#msg-input').addEventListener('input', () => {
  if (!state.activePeer) return;
  clearTimeout(state.typingSendTimer);
  state.typingSendTimer = setTimeout(() => {
    wsSend({ type:'typing', to: state.activePeer });
  }, 300);
});

/* Автоскролл при появлении клавиатуры */
if (window.visualViewport){
  window.visualViewport.addEventListener('resize', () => {
    if (state.activePeer) scrollMessages();
  });
}

/* =========================================================
   НАСТРОЙКИ
   ========================================================= */
function toggleTheme(){
  state.theme = state.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem(STORAGE.theme, state.theme);
  applyTheme();
  haptic(8);
}

$('#set-theme').addEventListener('click', toggleTheme);
$('#quick-theme').addEventListener('click', toggleTheme);

$('#set-glass').addEventListener('click', () => {
  state.glass = state.glass === 'on' ? 'off' : 'on';
  localStorage.setItem(STORAGE.glass, state.glass);
  applyGlass();
  haptic(8);
});

$('#set-haptic').addEventListener('click', () => {
  state.haptic = !state.haptic;
  localStorage.setItem(STORAGE.haptic, state.haptic ? '1' : '0');
  applyHapticUI();
  if (state.haptic) haptic(15);
});

$('#set-notif').addEventListener('click', async () => {
  if (!('Notification' in window)){ toast('Не поддерживается'); return; }
  if (Notification.permission === 'granted'){
    state.notif = !state.notif;
    localStorage.setItem(STORAGE.notif, state.notif ? '1' : '0');
    applyNotifUI();
    haptic(8);
    return;
  }
  if (Notification.permission === 'denied'){ toast('Запрещено в браузере'); return; }
  try {
    const p = await Notification.requestPermission();
    if (p === 'granted'){
      state.notif = true;
      localStorage.setItem(STORAGE.notif, '1');
      toast('Уведомления включены');
    } else {
      toast('Разрешение не выдано');
    }
  } catch(e){}
  applyNotifUI();
});

/* =========================================================
   ОФЛАЙН-КНОПКА "ПОВТОРИТЬ"
   ========================================================= */
(function bindOfflineRetry(){
  const btn = connUI.retry();
  if (!btn) return;
  btn.addEventListener('click', () => {
    if (!navigator.onLine){
      toast('Интернета всё ещё нет');
      haptic([8, 40, 8]);
      return;
    }
    btn.classList.add('spin');
    btn.disabled = true;
    haptic(10);

    // Разбудим SW + попробуем реконнект
    try { fetch('/icon.svg', { cache: 'no-store' }); } catch(_){}
    state.wsReconnectAttempts = 0;
    clearTimeout(state.reconnectTimer);

    setTimeout(() => {
      btn.classList.remove('spin');
      btn.disabled = false;
      onBrowserOnline();
    }, 500);
  });
})();

/* =========================================================
   ПРОЧИЕ СЛУШАТЕЛИ
   ========================================================= */
document.addEventListener('dblclick', e => e.preventDefault(), {passive:false});

// Возврат из фона
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.token){
    if (!state.ws || state.ws.readyState > 1) connectWS();
    if (state.page === 'home') refreshChats();
    if (state.page === 'friends') refreshFriends();
  }
});

/* =========================================================
   СТАРТ
   ========================================================= */
applyTheme();
applyGlass();
applyNotifUI();
applyHapticUI();

if (!navigator.onLine){
  setConnState('offline');
} else {
  setConnState('connecting');
}

if (state.token){
  fetch('/api/chats', { headers: { 'Authorization': 'Bearer ' + state.token } })
    .then(r => { if (r.ok) enterApp(); else doLogout(); })
    .catch(() => {
      if (!navigator.onLine){ setConnState('offline'); }
      else doLogout();
    });
} else {
  showScreen('auth');
  updateViewport();
}

// Периодический watchdog: если WS не поднялся, но интернет есть — попробуем
setInterval(() => {
  if (!state.token) return;
  if (!navigator.onLine){
    if (state.connState !== 'offline') setConnState('offline');
    return;
  }
  if (!state.wsReady && state.connState !== 'connecting'){
    setConnState('connecting');
    connectWS();
  }
}, 15000);

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
