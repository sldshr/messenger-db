# main.py
# Запуск: python main.py
# Установка: pip install fastapi uvicorn

import time
import uuid
from typing import Optional

from fastapi import (
    FastAPI, HTTPException, Header, Depends, WebSocket, WebSocketDisconnect, Query, Request,
)
from fastapi.responses import HTMLResponse, Response, JSONResponse
from pydantic import BaseModel
import uvicorn


app = FastAPI(title="SLD Messenger")

# ==================================================================
#                         ХРАНИЛИЩЕ
# ==================================================================
users: dict = {}         # username -> {"password": str}
tokens: dict = {}        # token -> username
messages: list = []      # {"id","from","to","text","ts","read"}
friends: dict = {}       # user -> set(user)
requests: dict = {}      # from_user -> set(to_user)
devices: dict = {}       # device_uuid -> username (доверенные устройства)


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


class DeviceData(BaseModel):
    uuid: str


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
#                    ДОВЕРЕННЫЕ УСТРОЙСТВА
# ==================================================================
@app.post("/api/device/login")
async def device_login(data: DeviceData):
    d = data.uuid.strip()
    if not d:
        raise HTTPException(400, "Не передан uuid")
    username = devices.get(d)
    if not username or username not in users:
        raise HTTPException(404, "Устройство не доверено")
    token = uuid.uuid4().hex
    tokens[token] = username
    return {"token": token, "username": username}


@app.post("/api/device/trust")
async def device_trust(data: DeviceData, user: str = Depends(current_user)):
    d = data.uuid.strip()
    if not d:
        raise HTTPException(400, "Не передан uuid")
    devices[d] = user
    return {"ok": True}


@app.post("/api/device/untrust")
async def device_untrust(data: DeviceData, user: str = Depends(current_user)):
    d = data.uuid.strip()
    if devices.get(d) == user:
        del devices[d]
    return {"ok": True}


@app.get("/api/device/status")
async def device_status(uuid: str = "", user: str = Depends(current_user)):
    return {"trusted": devices.get(uuid) == user}


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
const CACHE = 'sld-v3';
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
      if (fresh && fresh.status === 200 && url.origin === location.origin && url.pathname !== '/') {
        const clone = fresh.clone();
        caches.open(CACHE).then(c => c.put(req, clone)).catch(()=>{});
      }
      return fresh;
    } catch (err) {
      const cached = await caches.match(req, { ignoreSearch: true });
      if (cached) return cached;
      throw err;
    }
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
"""


@app.get("/sw.js")
async def sw():
    return Response(SW_JS, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "name": "SLD",
        "short_name": "SLD",
        "description": "Мессенджер SLD",
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
        '<text x="50%" y="54%" font-family="-apple-system,Segoe UI,Roboto,sans-serif" '
        'font-size="230" font-weight="800" fill="#fff" text-anchor="middle" '
        'dominant-baseline="middle">SLD</text></svg>'
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
<meta name="apple-mobile-web-app-title" content="SLD">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon.svg">
<title>SLD</title>
<style>
:root{
  --bg:#ffffff; --bg-elev:#f4f4f5; --bg-soft:#fafafa;
  --text:#111111; --text-dim:#8a8a8e; --border:#ececec;
  --accent:#111111; --accent-fg:#ffffff;
  --bubble-me:#111111; --bubble-me-fg:#ffffff;
  --bubble-them:#f2f2f3; --bubble-them-fg:#111111;
  --danger:#e11d48; --ok:#16a34a; --offline:#b0b0b5;
  --glass-bg:rgba(255,255,255,.72); --glass-border:rgba(255,255,255,.5);
  --shadow:0 8px 24px rgba(0,0,0,.06);
  --app-h:100dvh;
}
[data-theme="dark"]{
  --bg:#0b0b0c; --bg-elev:#17171a; --bg-soft:#101013;
  --text:#f5f5f7; --text-dim:#8e8e93; --border:#232326;
  --accent:#ffffff; --accent-fg:#111111;
  --bubble-me:#ffffff; --bubble-me-fg:#111111;
  --bubble-them:#1d1d20; --bubble-them-fg:#f5f5f7;
  --danger:#ff5c7a; --ok:#34d058; --offline:#5a5a5e;
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
.screen{position:absolute;inset:0;display:none;flex-direction:column;overflow:hidden;background:var(--bg)}
.screen.active{display:flex}

.topbar{
  flex-shrink:0; display:flex;align-items:center;gap:6px;
  padding:8px 8px; padding-top:calc(8px + env(safe-area-inset-top,0));
  border-bottom:1px solid var(--border); background:var(--bg);
  min-height:56px; z-index:5;
}
.topbar .title{
  flex:1;font-size:20px;font-weight:700;letter-spacing:-.3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  display:flex;align-items:center;gap:8px;min-width:0;
}
.topbar .title .title-text{overflow:hidden;text-overflow:ellipsis}
.conn-dot{width:9px;height:9px;border-radius:50%;background:var(--offline);
  flex-shrink:0;transition:background .25s, box-shadow .25s}
.conn-dot.online{background:var(--ok);box-shadow:0 0 0 3px rgba(22,163,74,.15)}
.icon-btn{
  width:44px;height:44px;flex-shrink:0;border:none;background:transparent;
  display:flex;align-items:center;justify-content:center;border-radius:12px;
  cursor:pointer;color:var(--text);transition:background .15s, transform .1s;
  touch-action:manipulation;
}
.icon-btn:active{background:var(--bg-elev);transform:scale(.94)}
.icon-btn svg{width:22px;height:22px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

[data-glass="on"] .glass{
  background:var(--glass-bg) !important;
  backdrop-filter:blur(22px) saturate(180%);
  -webkit-backdrop-filter:blur(22px) saturate(180%);
  border-color:var(--glass-border) !important;
}

.bottom-nav{
  flex-shrink:0;display:flex;padding:6px 6px;
  padding-bottom:calc(6px + env(safe-area-inset-bottom,0));
  border-top:1px solid var(--border);background:var(--bg);z-index:5;
}
.nav-btn{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;padding:8px 4px;background:transparent;border:none;cursor:pointer;
  color:var(--text-dim);font-family:inherit;font-size:11.5px;font-weight:600;
  border-radius:14px;transition:color .15s,background .15s,transform .1s;
  position:relative;touch-action:manipulation;
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

.page-area{flex:1;overflow:hidden;position:relative;min-height:0}
.page{position:absolute;inset:0;display:none;flex-direction:column;overflow:hidden}
.page.active{display:flex}
.page-scroll{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;min-height:0}

.segmented{display:flex;background:var(--bg-elev);border-radius:12px;padding:4px;margin:12px 16px 6px}
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
.avatar{
  position:relative;width:52px;height:52px;border-radius:50%;background:var(--bg-elev);
  display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:19px;color:var(--text-dim);flex-shrink:0;letter-spacing:.5px;
}
[data-theme="dark"] .avatar{color:var(--text)}
.avatar .online-dot{
  position:absolute;bottom:1px;right:1px;width:14px;height:14px;border-radius:50%;
  background:var(--offline);border:2.5px solid var(--bg);box-sizing:content-box;
}
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
  transition:transform .1s,opacity .15s;touch-action:manipulation}
.mini-btn:active{transform:scale(.95)}
.mini-btn.primary{background:var(--text);color:var(--bg)}
.mini-btn.ghost{background:var(--bg-elev);color:var(--text)}
.mini-btn.danger{background:transparent;color:var(--danger)}
.mini-btn-row{display:flex;gap:8px;flex-shrink:0}

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

.remember-row{
  display:none; align-items:center;gap:12px;
  padding:14px 14px; margin-bottom:10px;
  background:var(--bg-elev); border-radius:13px; cursor:pointer;
  user-select:none; -webkit-user-select:none; touch-action:manipulation;
  transition:background .15s;
}
.remember-row:active{background:var(--bg-soft)}
.remember-row.show{display:flex}
.remember-row .checkbox{
  width:24px;height:24px;border-radius:7px;border:2px solid var(--text-dim);
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  transition:background .15s,border-color .15s;
}
.remember-row .checkbox svg{width:16px;height:16px;stroke:#fff;fill:none;
  stroke-width:3;stroke-linecap:round;stroke-linejoin:round;opacity:0;transition:opacity .15s}
.remember-row.checked .checkbox{background:var(--text);border-color:var(--text)}
.remember-row.checked .checkbox svg{opacity:1;stroke:var(--bg)}
.remember-label{text-align:left;flex:1;min-width:0}
.remember-title{font-size:15px;font-weight:600}
.remember-sub{font-size:12.5px;color:var(--text-dim);margin-top:2px}

.btn-primary{width:100%;padding:17px;background:var(--text);color:var(--bg);border:none;
  border-radius:13px;font-size:16.5px;font-weight:600;cursor:pointer;
  margin-top:4px;transition:transform .1s,opacity .2s;font-family:inherit;touch-action:manipulation}
.btn-primary:active{transform:scale(.98)}
.btn-primary:disabled{opacity:.5;cursor:default}
.error{color:var(--danger);font-size:14px;margin-top:12px;min-height:20px}

.title-block{flex:1;min-width:0;text-align:center;overflow:hidden}
.title-name{font-size:17px;font-weight:600;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.15}
.title-status{font-size:12.5px;color:var(--text-dim);margin-top:1px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  min-height:15px;line-height:15px;transition:color .2s}
.title-status.online{color:var(--ok)}
.title-status.typing{color:var(--ok);font-style:italic}

.messages{
  flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;
  padding:16px 14px 8px;display:flex;flex-direction:column;gap:6px;
  background:var(--bg);min-height:0;
}
.msg{max-width:80%;padding:10px 15px;border-radius:20px;
  font-size:17.5px;line-height:1.42;word-wrap:break-word;overflow-wrap:anywhere;
  white-space:pre-wrap;animation:pop .14s ease}
@keyframes pop{from{transform:scale(.94);opacity:0}to{transform:scale(1);opacity:1}}
.msg.me{align-self:flex-end;background:var(--bubble-me);color:var(--bubble-me-fg);
  border-bottom-right-radius:6px}
.msg.them{align-self:flex-start;background:var(--bubble-them);color:var(--bubble-them-fg);
  border-bottom-left-radius:6px}

.composer{
  flex-shrink:0;display:flex;align-items:center;gap:8px;padding:10px 12px;
  padding-bottom:calc(10px + env(safe-area-inset-bottom,0));
  border-top:1px solid var(--border);background:var(--bg);z-index:5;
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
  touch-action:manipulation; -webkit-user-select:none;user-select:none;
}
.send-btn:active{transform:scale(.92)}
.send-btn svg{width:20px;height:20px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
.send-btn:disabled{opacity:.4}

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
  cursor:pointer;font-family:inherit;touch-action:manipulation}
.danger-btn:active{background:var(--bg-elev)}

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

/* ---- Offline banner ---- */
.offline-banner{
  position:fixed; top:0; left:0; right:0;
  background:#e11d48; color:#fff;
  text-align:center; padding:9px 12px;
  padding-top:calc(9px + env(safe-area-inset-top,0));
  font-size:13.5px; font-weight:600;
  transform:translateY(-110%); transition:transform .25s ease;
  z-index:99998; pointer-events:none;
}
.offline-banner.show{transform:translateY(0)}

/* ---- Boot / offline overlay ---- */
.boot{
  position:fixed;inset:0;background:var(--bg);z-index:99997;
  display:flex;align-items:center;justify-content:center;flex-direction:column;
  padding:24px;text-align:center;gap:16px;
}
.boot.hide{display:none}
.boot .logo{width:72px;height:72px;border-radius:22px;margin:0}
.boot-title{font-size:20px;font-weight:700;letter-spacing:-.3px}
.boot-sub{color:var(--text-dim);font-size:14px;max-width:280px;line-height:1.5}
.spinner{width:28px;height:28px;border-radius:50%;border:3px solid var(--border);
  border-top-color:var(--text);animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.retry-btn{
  margin-top:6px;padding:12px 22px;border-radius:12px;border:none;
  background:var(--text);color:var(--bg);font-weight:600;font-size:15px;
  font-family:inherit;cursor:pointer;
}
</style>
</head>
<body>

<!-- Offline banner -->
<div class="offline-banner" id="offline-banner">Нет подключения к интернету</div>

<!-- Boot overlay (используется при старте / отсутствии сети) -->
<div class="boot" id="boot">
  <div class="logo">
    <svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
  </div>
  <div class="boot-title">SLD</div>
  <div class="boot-sub" id="boot-sub">Подключение к серверу…</div>
  <div class="spinner" id="boot-spinner"></div>
  <button class="retry-btn" id="boot-retry" style="display:none">Повторить</button>
</div>

<div id="app">

  <!-- AUTH -->
  <div id="auth" class="screen">
    <div class="auth-wrap">
      <div class="logo">
        <svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      </div>
      <h1>SLD</h1>
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

        <div class="remember-row" id="remember-row">
          <div class="checkbox">
            <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
          </div>
          <div class="remember-label">
            <div class="remember-title">Запомнить устройство</div>
            <div class="remember-sub">Вход без пароля на этом устройстве</div>
          </div>
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
                <div class="set-hint" id="notif-hint">Всплывающие оповещения</div>
              </div>
              <div class="switch" id="set-notif"></div>
            </div>
          </div>

          <div class="settings-group" id="device-group" style="display:none">
            <div class="settings-user">
              <div class="avatar" style="background:transparent;color:var(--ok)">
                <svg viewBox="0 0 24 24" style="width:28px;height:28px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/></svg>
              </div>
              <div style="flex:1;min-width:0">
                <div class="settings-user-name" style="font-size:16px">Устройство доверено</div>
                <div class="settings-user-sub">Вход без пароля включён</div>
              </div>
            </div>
          </div>

          <div class="settings-group">
            <button class="danger-btn" id="forget-device-btn" style="display:none">Забыть это устройство</button>
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
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

/* =========================================================
   ОПРЕДЕЛЕНИЕ КЛИЕНТА / UUID
   ========================================================= */
const UA = navigator.userAgent || '';
const IS_SLD_APP = /SLDApp/i.test(UA) || !!window.AndroidBridge;

function readDeviceUuid(){
  try {
    if (window.AndroidBridge && typeof window.AndroidBridge.getUuid === 'function'){
      const u = window.AndroidBridge.getUuid();
      if (u && String(u).length > 0) return String(u);
    }
  } catch(e){}
  try {
    if (typeof window.__SLD_UUID__ === 'string' && window.__SLD_UUID__.length > 0){
      return window.__SLD_UUID__;
    }
  } catch(e){}
  try {
    if (typeof AndroidBridge !== 'undefined' && AndroidBridge.getUuid){
      const u = AndroidBridge.getUuid();
      if (u) return String(u);
    }
  } catch(e){}
  return '';
}

const DEVICE_UUID = readDeviceUuid();
const HAS_DEVICE = IS_SLD_APP && DEVICE_UUID.length > 0;

console.log('[SLD] UA app=', IS_SLD_APP, 'uuid=', DEVICE_UUID, 'hasDevice=', HAS_DEVICE);

/* =========================================================
   STATE
   ========================================================= */
const state = {
  token: localStorage.getItem('m_token') || '',
  username: localStorage.getItem('m_user') || '',
  theme: localStorage.getItem('m_theme') || 'light',
  glass: localStorage.getItem('m_glass') || 'on',
  notif: localStorage.getItem('m_notif') === '1',
  rememberDevice: localStorage.getItem('m_remember_device') !== '0',  // по умолчанию вкл
  ws: null,
  wsReady: false,
  reconnectTimer: null,
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
  activeMsgs: [],
  seenIds: new Set(),
  typing: {},
  typingTimers: {},
  deviceTrusted: false,
};

const ICONS = {
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/>',
};

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
}[c]));

function fmtTime(ts){
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString())
    return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return 'вчера';
  return String(d.getDate()).padStart(2,'0') + '.' + String(d.getMonth()+1).padStart(2,'0');
}

function toast(msg){
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 1800);
}

function isTyping(peer){
  const t = state.typing[peer];
  return t && (Date.now() - t) < 3500;
}
function isOnline(peer){ return state.onlineFriends.has(peer); }

/* =========================================================
   ТЕМА / СТЕКЛО / УВЕДОМЛЕНИЯ
   ========================================================= */
function applyTheme(){
  document.documentElement.setAttribute('data-theme', state.theme);
  const meta = document.querySelector('meta[name=theme-color]');
  if (meta) meta.setAttribute('content', state.theme === 'dark' ? '#0b0b0c' : '#ffffff');
  const icon = $('#quick-theme-icon');
  if (icon) icon.outerHTML = '<svg id="quick-theme-icon" viewBox="0 0 24 24">' +
    (state.theme === 'dark' ? ICONS.sun : ICONS.moon) + '</svg>';
  const sw = $('#set-theme'); if (sw) sw.classList.toggle('on', state.theme === 'dark');
}
function applyGlass(){
  document.documentElement.setAttribute('data-glass', state.glass);
  const sw = $('#set-glass'); if (sw) sw.classList.toggle('on', state.glass === 'on');
}
function applyNotifUI(){
  const sw = $('#set-notif'); if (!sw) return;
  const supported = 'Notification' in window;
  const granted = supported && Notification.permission === 'granted';
  sw.classList.toggle('on', granted && state.notif);
  const hint = $('#notif-hint');
  if (hint){
    if (!supported) hint.textContent = 'Не поддерживается браузером';
    else if (Notification.permission === 'denied') hint.textContent = 'Запрещено в браузере';
    else if (granted) hint.textContent = 'Уведомления включены';
    else hint.textContent = 'Нажмите, чтобы разрешить';
  }
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
   OFFLINE / ONLINE
   ========================================================= */
const offlineBanner = $('#offline-banner');
function setOffline(off){
  if (offlineBanner) offlineBanner.classList.toggle('show', !!off);
  if (!off && state.token && (!state.ws || state.ws.readyState > 1)){
    connectWS();
  }
}
window.addEventListener('offline', () => {
  setOffline(true);
  toast('Нет подключения');
});
window.addEventListener('online', () => {
  setOffline(false);
  if (state.token){
    if (!state.ws || state.ws.readyState > 1) connectWS();
    if (state.page === 'home') refreshChats();
    if (state.page === 'friends') refreshFriends();
  }
});
if (!navigator.onLine) setOffline(true);

/* =========================================================
   BOOT OVERLAY
   ========================================================= */
function showBoot(msg, showSpinner, showRetry){
  const b = $('#boot');
  b.classList.remove('hide');
  $('#boot-sub').textContent = msg;
  $('#boot-spinner').style.display = showSpinner ? '' : 'none';
  $('#boot-retry').style.display = showRetry ? '' : 'none';
}
function hideBoot(){ $('#boot').classList.add('hide'); }
$('#boot-retry').addEventListener('click', () => location.reload());

/* =========================================================
   API
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
  } catch(e){
    setOffline(!navigator.onLine);
    throw new Error('Нет соединения');
  }
  if (r.status === 401 && state.token){ doLogout(); throw new Error('unauth'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}

/* =========================================================
   AUTH FORM
   ========================================================= */
let authMode = 'login';
$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  authMode = t.dataset.tab;
  $('#auth-submit').textContent = authMode === 'login' ? 'Войти' : 'Создать аккаунт';
  $('#auth-error').textContent = '';
}));

/* remember device checkbox */
const rememberRow = $('#remember-row');
function updateRememberUI(){
  const show = HAS_DEVICE && authMode === 'login';
  rememberRow.classList.toggle('show', show);
  rememberRow.classList.toggle('checked', state.rememberDevice);
}
rememberRow.addEventListener('click', () => {
  state.rememberDevice = !state.rememberDevice;
  localStorage.setItem('m_remember_device', state.rememberDevice ? '1' : '0');
  updateRememberUI();
});

$('#auth-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = $('#username').value.trim();
  const password = $('#password').value;
  const errEl = $('#auth-error');
  errEl.textContent = '';
  if (!username || !password){ errEl.textContent = 'Заполните все поля'; return; }
  if (!navigator.onLine){ errEl.textContent = 'Нет подключения к интернету'; return; }

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

    // Запомнить устройство (только если включено и есть UUID)
    if (HAS_DEVICE && state.rememberDevice){
      try {
        await fetch('/api/device/trust', {
          method: 'POST',
          headers: {'Content-Type':'application/json', 'Authorization':'Bearer ' + state.token},
          body: JSON.stringify({uuid: DEVICE_UUID}),
        });
        state.deviceTrusted = true;
      } catch(e){}
    }

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
  state.friendsList = [];
  state.onlineFriends.clear();
  state.chats = [];
  state.incoming = [];
  state.outgoing = [];
  state.typing = {};
  state.deviceTrusted = false;
  if (state.ws){ try { state.ws.close(); } catch(e){} state.ws = null; }
  clearTimeout(state.reconnectTimer);
  state.wsReady = false;
  updateConnDot();
  showScreen('auth');
  updateRememberUI();
}
$('#logout-btn').addEventListener('click', doLogout);

$('#forget-device-btn').addEventListener('click', async () => {
  if (!HAS_DEVICE) return;
  if (!confirm('Забыть это устройство? При следующем входе потребуется пароль.')) return;
  try {
    await api('/api/device/untrust', {
      method: 'POST',
      body: JSON.stringify({uuid: DEVICE_UUID}),
    });
    state.deviceTrusted = false;
    toast('Устройство забыто');
    refreshDeviceUI();
  } catch(e){ toast(e.message || 'Ошибка'); }
});

/* =========================================================
   NAV
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
  $('#main-title').textContent = titles[page] || 'SLD';
  if (page === 'home') refreshChats();
  if (page === 'friends') refreshFriends();
  if (page === 'settings') refreshDeviceUI();
}
$$('.nav-btn').forEach(b => b.addEventListener('click', () => switchPage(b.dataset.page)));

/* =========================================================
   ENTER APP
   ========================================================= */
async function enterApp(){
  $('#me-avatar').textContent = (state.username[0] || '?').toUpperCase();
  $('#me-name').textContent = state.username;
  applyTheme(); applyGlass(); applyNotifUI();
  connectWS();
  switchPage('home');
  showScreen('main');
  hideBoot();
  updateViewport();

  if ('serviceWorker' in navigator && !IS_SLD_APP){
    try { await navigator.serviceWorker.register('/sw.js'); } catch(e){}
    navigator.serviceWorker.addEventListener('message', (e) => {
      if (e.data && e.data.type === 'open-chat' && e.data.peer){
        tryOpenChat(e.data.peer);
      }
    });
  }

  // Открытие чата по параметру ?open=
  const params = new URLSearchParams(location.search);
  const peer = params.get('open');
  if (peer){
    localStorage.setItem('m_open_after_login', peer);
    history.replaceState({}, '', '/');
  }
  const toOpen = localStorage.getItem('m_open_after_login');
  if (toOpen){
    localStorage.removeItem('m_open_after_login');
    setTimeout(() => tryOpenChat(toOpen), 300);
  }

  // проверяем статус устройства
  if (HAS_DEVICE){
    try {
      const r = await api('/api/device/status?uuid=' + encodeURIComponent(DEVICE_UUID));
      state.deviceTrusted = !!r.trusted;
    } catch(e){ state.deviceTrusted = false; }
    refreshDeviceUI();
  }
}
function tryOpenChat(peer){
  if (!peer) return;
  showScreen('main');
  openChat(peer);
}

function refreshDeviceUI(){
  const group = $('#device-group');
  const forget = $('#forget-device-btn');
  if (!group) return;
  const show = HAS_DEVICE && state.deviceTrusted;
  group.style.display = show ? '' : 'none';
  forget.style.display = show ? '' : 'none';
  if (show){
    group.querySelector('.settings-user-name').textContent = 'Устройство доверено';
  }
}

/* =========================================================
   WEBSOCKET
   ========================================================= */
function updateConnDot(){
  const dot = $('#conn-dot');
  if (!dot) return;
  dot.classList.toggle('online', !!state.wsReady);
  const st = $('#me-status');
  if (st){
    st.textContent = state.wsReady ? 'в сети' : 'нет соединения';
    st.style.color = state.wsReady ? 'var(--ok)' : 'var(--text-dim)';
  }
}

function connectWS(){
  if (!state.token) return;
  if (!navigator.onLine) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = proto + '://' + location.host + '/ws?token=' + encodeURIComponent(state.token);
  let ws;
  try { ws = new WebSocket(url); } catch(e){ return; }
  state.ws = ws;
  ws.onopen = () => { state.wsReady = true; updateConnDot(); };
  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(e.data); } catch(_) { return; }
    handleServerEvent(m);
  };
  ws.onclose = (e) => {
    state.wsReady = false; updateConnDot();
    if (state.ws === ws) state.ws = null;
    if (e.code === 4000) return;
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

/* =========================================================
   SERVER EVENTS
   ========================================================= */
function handleServerEvent(m){
  if (m.type === 'hello') return;

  if (m.type === 'presence'){
    if (m.online) state.onlineFriends.add(m.user);
    else state.onlineFriends.delete(m.user);
    if (state.page === 'home') renderChats();
    if (state.page === 'friends') renderFriends();
    if (state.activePeer === m.user) updateChatHeaderStatus();
    return;
  }

  if (m.type === 'message'){
    const peer = m.from === state.username ? m.to : m.from;
    const mine = m.from === state.username;

    if (state.activePeer === peer && !state.seenIds.has(m.id)){
      state.seenIds.add(m.id);
      state.activeMsgs.push({ id: m.id, from: m.from, text: m.text, ts: m.ts });
      appendMessage({ id: m.id, from: m.from, text: m.text, ts: m.ts });
      scrollMessages(true);
      if (!mine) wsSend({ type: 'read', peer });
    } else if (!mine){
      maybeNotify(m);
    }
    updateChatFromMessage(m);
    if (m.from === peer) delete state.typing[peer];
    return;
  }

  if (m.type === 'chat_read'){
    const c = state.chats.find(x => x.username === m.peer);
    if (c) c.unread = 0;
    if (state.page === 'home'){ renderChats(); updateDots(); }
    return;
  }

  if (m.type === 'friends_changed'){
    refreshFriends().then(() => {
      if (state.activePeer && !state.friendsList.includes(state.activePeer)){
        toast('Диалог закрыт');
        state.activePeer = null;
        showScreen('main');
        if (state.page === 'home') refreshChats();
      }
    });
    refreshChats();
    return;
  }

  if (m.type === 'typing'){
    if (m.from){
      state.typing[m.from] = Date.now();
      clearTimeout(state.typingTimers[m.from]);
      state.typingTimers[m.from] = setTimeout(() => {
        delete state.typing[m.from];
        if (state.activePeer === m.from) updateChatHeaderStatus();
        if (state.page === 'home') renderChats();
      }, 3500);
      if (state.activePeer === m.from) updateChatHeaderStatus();
      if (state.page === 'home') renderChats();
    }
    return;
  }
}

function updateChatFromMessage(m){
  const peer = m.from === state.username ? m.to : m.from;
  const mine = m.from === state.username;
  let c = state.chats.find(x => x.username === peer);
  if (!c){ refreshChats(); return; }
  c.lastText = m.text; c.lastTs = m.ts; c.lastFromMe = mine;
  if (!mine && state.activePeer !== peer) c.unread = (c.unread || 0) + 1;
  state.chats.sort((a,b) => (b.lastTs||0) - (a.lastTs||0) || a.username.localeCompare(b.username));
  if (state.page === 'home'){ renderChats(); updateDots(); }
  else updateDots();
}

function maybeNotify(m){
  if (IS_SLD_APP){
    try {
      if (window.AndroidBridge && typeof window.AndroidBridge.notify === 'function'){
        window.AndroidBridge.notify(m.from, m.text);
      }
    } catch(e){}
    return;
  }
  if (!state.notif) return;
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  if (!document.hidden && state.activePeer === m.from) return;
  const title = m.from;
  const body = m.text.length > 80 ? m.text.slice(0, 77) + '…' : m.text;
  try {
    const doShow = (reg) => {
      try {
        reg.showNotification(title, {
          body, tag: 'msg-' + m.from, renotify: true,
          icon: '/icon.svg', badge: '/icon.svg',
          data: { peer: m.from },
        });
      } catch(e){
        try { new Notification(title, { body, icon: '/icon.svg', data: { peer: m.from } }); } catch(_){}
      }
    };
    if (navigator.serviceWorker && navigator.serviceWorker.ready){
      navigator.serviceWorker.ready.then(doShow).catch(() => {
        try { new Notification(title, { body, icon: '/icon.svg' }); } catch(_){}
      });
    } else {
      new Notification(title, { body, icon: '/icon.svg' });
    }
  } catch(e){}
}

/* =========================================================
   CHATS
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
    const online = isOnline(c.username);
    const typing = isTyping(c.username);
    let sub;
    if (typing) sub = '<span class="row-sub typing" style="color:var(--ok);font-style:italic">печатает…</span>';
    else if (c.lastText) sub = '<span class="row-sub">' + esc((c.lastFromMe ? 'Вы: ' : '') + c.lastText) + '</span>';
    else sub = '<span class="row-sub">нет сообщений</span>';
    const badge = c.unread ? '<div class="badge">' + c.unread + '</div>' : '';
    const time = c.lastTs ? '<div class="row-time">' + esc(fmtTime(c.lastTs)) + '</div>' : '';
    return '<div class="row" data-peer="' + esc(c.username) + '">' +
      '<div class="avatar">' + esc(initial) +
        '<span class="online-dot ' + (online ? 'on' : '') + '"></span>' +
      '</div>' +
      '<div class="row-info"><div class="row-title">' + esc(c.username) + '</div>' +
        sub + '</div>' +
      '<div class="row-right">' + time + badge + '</div></div>';
  }).join('');
  box.querySelectorAll('.row').forEach(r => r.addEventListener('click', () => openChat(r.dataset.peer)));
}

/* =========================================================
   FRIENDS
   ========================================================= */
function updateDots(){
  const homeUnread = state.chats.reduce((s,c) => s + (c.unread||0), 0);
  const hd = $('#home-dot');
  if (homeUnread > 0){ hd.textContent = homeUnread > 99 ? '99+' : homeUnread; hd.style.display = 'flex'; }
  else hd.style.display = 'none';
  const pendingCount = state.incoming.length;
  const fd = $('#friends-dot');
  if (pendingCount > 0){ fd.textContent = pendingCount > 99 ? '99+' : pendingCount; fd.style.display = 'flex'; }
  else fd.style.display = 'none';
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
    state.onlineFriends = new Set(data.online || []);
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
  if (state.searchQuery){ renderSearch(box); return; }
  if (state.friendsTab === 'friends'){
    if (!state.friendsList.length){
      box.innerHTML = '<div class="empty">Пока нет друзей.<br>Найдите людей через поиск выше.</div>';
      return;
    }
    box.innerHTML = state.friendsList.map(name => {
      const initial = (name[0] || '?').toUpperCase();
      const online = isOnline(name);
      const typing = isTyping(name);
      const status = typing ? 'печатает…' : (online ? 'в сети' : 'не в сети');
      const cls = typing ? 'row-sub typing' : 'row-sub';
      return '<div class="row">' +
        '<div class="avatar">' + esc(initial) +
          '<span class="online-dot ' + (online ? 'on' : '') + '"></span>' +
        '</div>' +
        '<div class="row-info">' +
          '<div class="row-title">' + esc(name) + '</div>' +
          '<div class="' + cls + '">' + status + '</div>' +
        '</div>' +
        '<div class="mini-btn-row">' +
          '<button class="mini-btn primary" data-act="open" data-user="' + esc(name) + '">Написать</button>' +
          '<button class="mini-btn danger" data-act="unfriend" data-user="' + esc(name) + '" aria-label="Удалить">' +
            '<svg style="width:18px;height:18px;vertical-align:middle;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round" viewBox="0 0 24 24">' + ICONS.trash + '</svg>' +
          '</button>' +
        '</div></div>';
    }).join('');
    box.querySelectorAll('button[data-act]').forEach(b => {
      b.addEventListener('click', (e) => { e.stopPropagation(); friendAction(b.dataset.act, b.dataset.user); });
    });
    return;
  }

  const inc = state.incoming.map(name => rowPending(name, 'incoming')).join('');
  const out = state.outgoing.map(name => rowPending(name, 'outgoing')).join('');
  let html = '';
  if (inc) html += '<div class="section-label">Входящие</div>' + inc;
  if (out) html += '<div class="section-label">Исходящие</div>' + out;
  if (!html) html = '<div class="empty">Нет активных заявок.</div>';
  box.innerHTML = html;
  box.querySelectorAll('button[data-act]').forEach(b => {
    b.addEventListener('click', (e) => { e.stopPropagation(); friendAction(b.dataset.act, b.dataset.user); });
  });
}

function rowPending(name, kind){
  const initial = (name[0] || '?').toUpperCase();
  const online = isOnline(name);
  const btns = kind === 'incoming'
    ? '<button class="mini-btn primary" data-act="accept" data-user="' + esc(name) + '">Принять</button>' +
      '<button class="mini-btn ghost" data-act="decline" data-user="' + esc(name) + '">Отклонить</button>'
    : '<button class="mini-btn ghost" data-act="cancel" data-user="' + esc(name) + '">Отменить</button>';
  return '<div class="row">' +
    '<div class="avatar">' + esc(initial) +
      '<span class="online-dot ' + (online ? 'on' : '') + '"></span>' +
    '</div>' +
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
    const online = !!u.online;
    let btn = '';
    if (u.status === 'friend') btn = '<button class="mini-btn primary" data-act="open" data-user="' + esc(u.username) + '">Написать</button>';
    else if (u.status === 'outgoing') btn = '<button class="mini-btn ghost" data-act="cancel" data-user="' + esc(u.username) + '">Отменить</button>';
    else if (u.status === 'incoming') btn = '<button class="mini-btn primary" data-act="accept" data-user="' + esc(u.username) + '">Принять</button>';
    else btn = '<button class="mini-btn primary" data-act="request" data-user="' + esc(u.username) + '">Добавить</button>';
    return '<div class="row">' +
      '<div class="avatar">' + esc(initial) +
        '<span class="online-dot ' + (online ? 'on' : '') + '"></span>' +
      '</div>' +
      '<div class="row-info"><div class="row-title">' + esc(u.username) + '</div></div>' +
      '<div class="mini-btn-row">' + btn + '</div></div>';
  }).join('');
  box.querySelectorAll('button[data-act]').forEach(b => {
    b.addEventListener('click', (e) => { e.stopPropagation(); friendAction(b.dataset.act, b.dataset.user); });
  });
}

async function friendAction(act, user){
  try {
    if (act === 'open'){ openChat(user); return; }
    if (act === 'unfriend'){
      if (!confirm('Удалить ' + user + ' из друзей?\nВся переписка тоже будет удалена.')) return;
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
    await refreshChats();
    if (state.searchQuery) await runSearch(state.searchQuery);
  } catch(err){
    toast(err.message || 'Ошибка');
  }
}

/* =========================================================
   SEARCH
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
    state.searchResults = res;
    renderFriends();
  } catch(e){}
}

/* =========================================================
   CHAT
   ========================================================= */
function updateChatHeaderStatus(){
  const el = $('#chat-status');
  if (!el || !state.activePeer) return;
  if (isTyping(state.activePeer)){
    el.textContent = 'печатает…'; el.className = 'title-status typing'; return;
  }
  if (isOnline(state.activePeer)){
    el.textContent = 'в сети'; el.className = 'title-status online';
  } else {
    el.textContent = 'не в сети'; el.className = 'title-status';
  }
}

async function openChat(peer){
  state.activePeer = peer;
  state.activeMsgs = [];
  state.seenIds.clear();
  $('#chat-title').textContent = peer;
  $('#messages').innerHTML = '';
  updateChatHeaderStatus();
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
    wsSend({ type:'read', peer });
    const c = state.chats.find(x => x.username === peer);
    if (c){ c.unread = 0; renderChats(); updateDots(); }
  } catch(e){
    toast(e.message || 'Ошибка загрузки');
    if (e.message && e.message.indexOf('Не друзья') >= 0){
      state.activePeer = null; showScreen('main'); return;
    }
  }
  setTimeout(() => { try { $('#msg-input').focus({preventScroll:true}); } catch(_){} }, 50);
}

function appendMessage(m){
  const box = $('#messages');
  const el = document.createElement('div');
  el.className = 'msg ' + (m.from === state.username ? 'me' : 'them');
  el.textContent = m.text;
  box.appendChild(el);
}
function scrollMessages(){
  const box = $('#messages');
  requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
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
  if (!navigator.onLine){ toast('Нет интернета'); return; }
  const input = $('#msg-input');
  const text = input.value.trim();
  if (!text) return;
  const ok = wsSend({ type:'send', to: state.activePeer, text });
  if (!ok){ toast('Нет соединения'); return; }
  input.value = '';
  try { input.focus({preventScroll:true}); } catch(_){}
}

(function bindSend(){
  const btn = $('#send-btn');
  if (!btn) return;
  btn.addEventListener('pointerdown', (e) => { e.preventDefault(); });
  btn.addEventListener('click', (e) => { e.preventDefault(); sendMessage(); });
  btn.addEventListener('touchend', (e) => {
    if (!('PointerEvent' in window)){ e.preventDefault(); sendMessage(); }
  });
})();

$('#msg-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendMessage(); }
});
let typingTimer = null;
$('#msg-input').addEventListener('input', () => {
  if (!state.activePeer) return;
  clearTimeout(typingTimer);
  typingTimer = setTimeout(() => { wsSend({ type:'typing', to: state.activePeer }); }, 300);
});
if (window.visualViewport){
  window.visualViewport.addEventListener('resize', () => {
    if (state.activePeer) scrollMessages();
  });
}

/* =========================================================
   SETTINGS
   ========================================================= */
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
    state.notif = !state.notif;
    localStorage.setItem('m_notif', state.notif ? '1' : '0');
    applyNotifUI(); return;
  }
  if (Notification.permission === 'denied'){ toast('Запрещено в браузере'); return; }
  try {
    const p = await Notification.requestPermission();
    if (p === 'granted'){
      state.notif = true;
      localStorage.setItem('m_notif', '1');
      toast('Уведомления включены');
    } else toast('Разрешение не выдано');
  } catch(e){}
  applyNotifUI();
});

document.addEventListener('dblclick', e => e.preventDefault(), {passive:false});

/* =========================================================
   INIT
   ========================================================= */
async function init(){
  applyTheme();
  applyGlass();
  updateConnDot();
  updateRememberUI();

  // Нет интернета сразу
  if (!navigator.onLine){
    setOffline(true);
    showBoot('Нет подключения к интернету', false, true);
    return;
  }

  // Мгновенная попытка device-login (только для нашего приложения)
  if (HAS_DEVICE){
    try {
      const r = await fetch('/api/device/login', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({uuid: DEVICE_UUID}),
      });
      if (r.ok){
        const data = await r.json();
        state.token = data.token;
        state.username = data.username;
        state.deviceTrusted = true;
        localStorage.setItem('m_token', state.token);
        localStorage.setItem('m_user', state.username);
        await enterApp();
        return;
      }
    } catch(e){
      // сервер недоступен
    }
  }

  // Есть сохранённый токен — валидируем
  if (state.token){
    try {
      const r = await fetch('/api/chats', { headers: { 'Authorization': 'Bearer ' + state.token } });
      if (r.ok){
        await enterApp();
        return;
      } else {
        localStorage.removeItem('m_token');
        localStorage.removeItem('m_user');
        state.token = ''; state.username = '';
      }
    } catch(e){
      // Сервер недоступен, но интернет есть
      showBoot('Сервер недоступен', false, true);
      return;
    }
  }

  // Показываем авторизацию
  showScreen('auth');
  hideBoot();
  updateViewport();
  updateRememberUI();
}

init();

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
