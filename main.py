"""
Federated E2EE Messenger — 1-на-1 чаты с федерацией серверов.
- Логин/пароль (scrypt). E2EE: ECDH P-256 + AES-GCM-256.
- Приватный ключ ТОЛЬКО в браузере. Сервер видит лишь шифротекст.
- Федерация: alice@server1.com <-> bob@server2.com.
- Взаимное добавление контактов, "запомнить меня", клик по нику — копирование uid.

Установка:  pip install fastapi uvicorn httpx
Запуск:     python main.py
Прод:       DOMAIN=chat.example.com SCHEME=https python main.py
Тест фед.:  DOMAIN=localhost:8000 SCHEME=http PORT=8000 python main.py
            DOMAIN=localhost:8001 SCHEME=http PORT=8001 python main.py
"""

import asyncio
import hashlib
import json
import os
import secrets
import struct
import time
import uuid
from collections import deque
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

# ============================ CONFIG ============================
PUBLIC_DOMAIN = os.getenv("DOMAIN", "").strip().lower().rstrip("/")
FED_SCHEME_ENV = os.getenv("SCHEME", "").strip().lower()
FED_TIMEOUT = 6.0
PORT = int(os.getenv("PORT", "8000"))


def fed_scheme_for(domain: str) -> str:
    if FED_SCHEME_ENV:
        return FED_SCHEME_ENV
    if domain.startswith("localhost") or domain.startswith("127."):
        return "http"
    return "https"


# ============================ PROTOCOL ============================
(T_REGISTER, T_AUTH, T_AUTH_OK, T_MSG, T_PING, T_PONG, T_ERROR,
 T_HELLO, T_USER_STATUS, T_SYNC, T_USERS, T_INTRO, T_CONTACT_ADD) = range(1, 14)
_HDR = struct.Struct(">BI")


def pack(t: int, o: Any) -> bytes:
    p = json.dumps(o, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _HDR.pack(t, len(p)) + p


def unpack(data: bytes):
    t, ln = _HDR.unpack_from(data, 0)
    return t, json.loads(data[5:5 + ln].decode("utf-8"))


# ============================ STATE (RAM) ============================
users: dict[str, dict] = {}            # login -> {salt, pw, pub}
online: dict[str, "Client"] = {}       # login -> Client
messages: deque = deque(maxlen=5000)
pending_intros: dict[str, list] = {}

_canonical_domain: Optional[str] = PUBLIC_DOMAIN or None


def self_domain(scope) -> str:
    global _canonical_domain
    if _canonical_domain:
        return _canonical_domain
    host = (scope.headers.get("host") or "localhost").lower()
    _canonical_domain = host
    return host


def scrypt_hash(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def parse_uid(uid: str) -> tuple[str, str]:
    uid = (uid or "").strip().lower()
    if "@" in uid:
        login, d = uid.split("@", 1)
        return login, d.strip()
    return uid, ""


def make_uid(login: str, domain: str) -> str:
    return f"{login}@{domain}"


# ============================ CLIENT ============================
class Client:
    __slots__ = ("ws", "login", "domain", "lock")

    def __init__(self, ws: WebSocket, domain: str):
        self.ws = ws
        self.login: Optional[str] = None
        self.domain = domain
        self.lock = asyncio.Lock()

    async def send(self, t: int, o: Any):
        async with self.lock:
            try:
                await self.ws.send_bytes(pack(t, o))
            except Exception:
                pass


async def broadcast_status(uid: str, is_online: bool):
    if not online:
        return
    frame = pack(T_USER_STATUS, {"uid": uid, "online": is_online})
    for c in list(online.values()):
        async with c.lock:
            try:
                await c.ws.send_bytes(frame)
            except Exception:
                pass


# ============================ HANDLERS ============================
async def handle_register(c: Client, obj: dict):
    login = (obj.get("login") or "").strip().lower()
    pw = obj.get("password") or ""
    pub = obj.get("pub") or ""
    if not login or not pw or not pub:
        return await c.send(T_ERROR, {"msg": "Заполните все поля"})
    if not (3 <= len(login) <= 24):
        return await c.send(T_ERROR, {"msg": "Логин: 3–24 символа"})
    if not all(ch.isalnum() or ch in "_-." for ch in login):
        return await c.send(T_ERROR, {"msg": "Логин: только a-z 0-9 _ - ."})
    if len(pw) < 6:
        return await c.send(T_ERROR, {"msg": "Пароль минимум 6 символов"})
    if login in users:
        return await c.send(T_ERROR, {"msg": "Логин уже занят"})
    salt = os.urandom(16)
    users[login] = {"salt": salt, "pw": scrypt_hash(pw, salt), "pub": pub}
    await _finish_auth(c, login)


async def handle_auth(c: Client, obj: dict):
    login = (obj.get("login") or "").strip().lower()
    pw = obj.get("password") or ""
    pub = (obj.get("pub") or "").strip()   # клиент всегда шлёт актуальный pub
    u = users.get(login)
    if not u or not secrets.compare_digest(u["pw"], scrypt_hash(pw, u["salt"])):
        return await c.send(T_ERROR, {"msg": "Неверный логин или пароль"})
    if login in online:
        return await c.send(T_ERROR, {"msg": "Вы уже вошли с другого устройства"})
    # обновляем pub если он есть и отличается — иначе останется устаревший
    if pub and pub != u.get("pub"):
        u["pub"] = pub
    await _finish_auth(c, login)


async def _finish_auth(c: Client, login: str):
    c.login = login
    online[login] = c
    my_uid = make_uid(login, c.domain)
    hist = [m for m in messages if m["from"] == my_uid or m["to"] == my_uid]
    intros = pending_intros.pop(login, [])
    await c.send(T_AUTH_OK, {
        "login": login, "uid": my_uid,
        "domain": c.domain, "history": hist,
        "pending_intros": intros,
    })
    await broadcast_status(my_uid, True)


async def handle_sync(c: Client, obj: dict):
    uids = obj.get("uids") or []
    result = []
    for uid in uids:
        uid = (uid or "").strip().lower()
        login, domain = parse_uid(uid)
        if not domain or domain == c.domain:
            result.append({"uid": uid, "online": login in online})
        else:
            result.append({"uid": uid, "online": None})
    await c.send(T_USERS, {"users": result})


async def handle_msg(c: Client, obj: dict):
    to_uid = (obj.get("to") or "").strip().lower()
    to_login, to_domain = parse_uid(to_uid)
    if not to_login:
        return
    if to_login == c.login and (not to_domain or to_domain == c.domain):
        return await c.send(T_ERROR, {"msg": "Нельзя писать самому себе"})
    ct = obj.get("ct") or ""
    iv = obj.get("iv") or ""
    if not ct or not iv:
        return
    mid = obj.get("id") or uuid.uuid4().hex
    from_uid = make_uid(c.login, c.domain)
    # from_pub берём либо из сообщения, либо из регистрационных данных
    from_pub = obj.get("from_pub") or (users.get(c.login, {}) or {}).get("pub", "")
    msg = {
        "id": mid, "from": from_uid, "to": to_uid,
        "ct": ct, "iv": iv,
        "from_pub": from_pub,
        "ts": int(time.time() * 1000),
    }
    messages.append(msg)
    await c.send(T_MSG, msg)
    if not to_domain or to_domain == c.domain:
        target = online.get(to_login)
        if target:
            await target.send(T_MSG, msg)
    else:
        asyncio.create_task(fed_send(to_domain, msg))


async def handle_intro(c: Client, obj: dict):
    to_uid = (obj.get("to") or "").strip().lower()
    to_login, to_domain = parse_uid(to_uid)
    if not to_login or not to_domain or to_domain == c.domain:
        return
    u = users.get(c.login)
    from_pub = u["pub"] if u else ""
    from_uid = make_uid(c.login, c.domain)
    asyncio.create_task(fed_intro(to_domain, from_uid, from_pub, to_uid))


async def fed_send(domain: str, msg: dict):
    url = f"{fed_scheme_for(domain)}://{domain}/fed/msg"
    try:
        async with httpx.AsyncClient(timeout=FED_TIMEOUT) as cx:
            r = await cx.post(url, json=msg)
            if r.status_code != 200:
                print(f"[fed] {domain} -> {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[fed] send to {domain} failed: {e}")


async def fed_intro(domain: str, from_uid: str, from_pub: str, to_uid: str):
    url = f"{fed_scheme_for(domain)}://{domain}/fed/intro"
    try:
        async with httpx.AsyncClient(timeout=FED_TIMEOUT) as cx:
            await cx.post(url, json={
                "from_uid": from_uid, "from_pub": from_pub, "to_uid": to_uid,
            })
    except Exception as e:
        print(f"[fed] intro to {domain} failed: {e}")


# ============================ HTTP / WS ============================
app = FastAPI()


@app.get("/")
async def index():
    return HTMLResponse(HTML_PAGE)


@app.get("/api/pubkey")
async def api_pubkey(uid: str, request: Request):
    uid = (uid or "").strip().lower()
    login, domain = parse_uid(uid)
    my_domain = self_domain(request)
    if not login:
        return JSONResponse({"error": "Пустой uid"}, status_code=400)
    if not domain or domain == my_domain:
        u = users.get(login)
        if not u:
            return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
        return {"uid": make_uid(login, my_domain), "pub": u["pub"],
                "online": login in online}
    url = f"{fed_scheme_for(domain)}://{domain}/fed/pub/{login}"
    try:
        async with httpx.AsyncClient(timeout=FED_TIMEOUT) as cx:
            r = await cx.get(url)
            if r.status_code != 200:
                return JSONResponse({"error": "Пользователь не найден на удалённом сервере"},
                                    status_code=404)
            data = r.json()
            return {"uid": make_uid(login, domain), "pub": data["pub"], "online": None}
    except Exception as e:
        return JSONResponse({"error": f"Сервер недоступен: {e}"}, status_code=502)


@app.get("/fed/pub/{login}")
async def fed_pub(login: str):
    u = users.get(login.lower())
    if not u:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"login": login.lower(), "pub": u["pub"]}


@app.post("/fed/msg")
async def fed_msg(payload: dict, request: Request):
    try:
        to_uid = (payload.get("to") or "").strip().lower()
        from_uid = (payload.get("from") or "").strip().lower()
        to_login, to_domain = parse_uid(to_uid)
        from_login, from_domain = parse_uid(from_uid)
        my_domain = self_domain(request)
        if to_domain != my_domain:
            return JSONResponse({"error": "wrong domain"}, status_code=400)
        if not from_domain or from_domain == my_domain:
            return JSONResponse({"error": "invalid source"}, status_code=400)
        if to_login not in users:
            return JSONResponse({"error": "no such user"}, status_code=404)
        msg = {
            "id": payload.get("id") or uuid.uuid4().hex,
            "from": from_uid, "to": to_uid,
            "ct": payload["ct"], "iv": payload["iv"],
            "from_pub": payload.get("from_pub", ""),
            "ts": payload.get("ts") or int(time.time() * 1000),
        }
        messages.append(msg)
        target = online.get(to_login)
        if target:
            await target.send(T_MSG, msg)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/fed/intro")
async def fed_intro_ep(payload: dict, request: Request):
    from_uid = (payload.get("from_uid") or "").strip().lower()
    from_pub = payload.get("from_pub") or ""
    to_uid = (payload.get("to_uid") or "").strip().lower()
    from_login, from_domain = parse_uid(from_uid)
    to_login, to_domain = parse_uid(to_uid)
    my_domain = self_domain(request)
    if not from_login or not from_domain or from_domain == my_domain:
        return JSONResponse({"error": "invalid from"}, status_code=400)
    if not to_login or to_domain != my_domain:
        return JSONResponse({"error": "invalid to"}, status_code=400)
    if to_login not in users:
        return JSONResponse({"error": "no such user"}, status_code=404)
    entry = {"uid": from_uid, "pub": from_pub}
    target = online.get(to_login)
    if target is not None:
        await target.send(T_CONTACT_ADD, entry)
    else:
        lst = pending_intros.setdefault(to_login, [])
        if not any(e["uid"] == from_uid for e in lst):
            lst.append(entry)
    return {"ok": True}


@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    my_domain = self_domain(ws)
    c = Client(ws, my_domain)
    await c.send(T_HELLO, {"domain": my_domain})
    try:
        while True:
            raw = await ws.receive_bytes()
            try:
                t, obj = unpack(raw)
            except Exception:
                continue
            if t == T_REGISTER:
                await handle_register(c, obj)
            elif t == T_AUTH:
                await handle_auth(c, obj)
            elif t == T_MSG and c.login:
                await handle_msg(c, obj)
            elif t == T_SYNC and c.login:
                await handle_sync(c, obj)
            elif t == T_INTRO and c.login:
                await handle_intro(c, obj)
            elif t == T_PING:
                await c.send(T_PONG, {"t": obj.get("t", 0)})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if c.login and online.get(c.login) is c:
            online.pop(c.login, None)
            await broadcast_status(make_uid(c.login, c.domain), False)


# ============================ HTML ============================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>Messages</title>
<style>
:root{
  --bg-primary:#ffffff;--bg-secondary:#f2f3f5;--bg-tertiary:#e3e5e8;
  --bg-hover:#e8eaed;--bg-active:#d7dae0;
  --text-normal:#2e3338;--text-muted:#747f8d;--border:#e3e5e8;
  --accent:#5865f2;--accent-hover:#4752c4;--green:#3ba55d;--red:#ed4245;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;overscroll-behavior:none}
body{background:var(--bg-primary);color:var(--text-normal);
  font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
button{font:inherit;cursor:pointer;color:inherit}
input{font:inherit}
@keyframes spin{to{transform:rotate(360deg)}}
button.busy{position:relative;color:transparent !important}
button.busy::after{content:"";position:absolute;top:50%;left:50%;
  width:18px;height:18px;margin:-9px 0 0 -9px;
  border:2px solid rgba(255,255,255,.35);border-top-color:#fff;
  border-radius:50%;animation:spin .7s linear infinite}
button.btn-secondary.busy::after{border-color:rgba(0,0,0,.15);border-top-color:var(--accent)}

#login{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  padding:20px;background:var(--bg-secondary);z-index:100}
.card{width:100%;max-width:400px;background:var(--bg-primary);border-radius:14px;
  padding:26px;box-shadow:0 10px 30px rgba(0,0,0,.10)}
.tabs{display:flex;background:var(--bg-secondary);border-radius:9px;padding:3px;margin-bottom:18px}
.tabs button{flex:1;background:none;border:none;color:var(--text-muted);
  padding:9px;border-radius:7px;font-weight:500;transition:all .15s}
.tabs button.active{background:var(--bg-primary);color:var(--text-normal);
  box-shadow:0 1px 2px rgba(0,0,0,.08)}
.card input[type=text],.card input[type=password]{width:100%;background:var(--bg-secondary);
  border:1px solid transparent;color:var(--text-normal);padding:12px 14px;border-radius:8px;
  outline:none;font-size:16px;margin-bottom:10px;transition:border .12s}
.card input[type=text]:focus,.card input[type=password]:focus{border-color:var(--accent)}
.remember{display:flex;align-items:center;gap:8px;font-size:13px;
  color:var(--text-muted);margin:0 2px 12px;cursor:pointer;user-select:none}
.remember input{width:16px;height:16px;margin:0;accent-color:var(--accent);cursor:pointer}
#submitBtn{width:100%;background:var(--accent);color:#fff;border:none;padding:12px;
  border-radius:8px;font-weight:600;margin-top:4px}
#submitBtn:hover:not(:disabled){background:var(--accent-hover)}
#submitBtn:disabled{cursor:default}
#authErr{color:var(--red);font-size:13px;margin-top:10px;min-height:17px;text-align:center}
#authNote{color:var(--text-muted);font-size:12px;margin-top:4px;text-align:center;line-height:1.35}

#app{display:none;height:100dvh}
#app.on{display:flex}
#sidebar{width:320px;flex-shrink:0;background:var(--bg-secondary);
  display:flex;flex-direction:column;border-right:1px solid var(--border)}
.sidebar-header{display:flex;align-items:center;gap:8px;padding:10px 12px;
  padding-top:calc(10px + env(safe-area-inset-top));
  border-bottom:1px solid var(--border);background:var(--bg-primary)}
.avatar{width:38px;height:38px;border-radius:50%;color:#fff;font-weight:600;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  font-size:15px;text-transform:uppercase;user-select:none}
.my-info{flex:1;min-width:0;cursor:pointer;border-radius:6px;padding:2px 4px;margin:-2px -4px;
  transition:background .12s}
.my-info:hover{background:var(--bg-hover)}
.my-info:active{background:var(--bg-active)}
.my-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:15px}
.my-sub{font-size:12px;color:var(--text-muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.icon-btn{background:none;border:none;color:var(--text-muted);padding:7px;
  border-radius:8px;display:flex;align-items:center;justify-content:center;
  transition:background .12s,color .12s;flex-shrink:0}
.icon-btn:hover{background:var(--bg-hover);color:var(--text-normal)}
.sidebar-title{padding:14px 16px 6px;font-size:11px;font-weight:700;
  color:var(--text-muted);text-transform:uppercase;letter-spacing:.02em}
#contacts{flex:1;overflow-y:auto;padding:0 8px 8px}
.empty-list{padding:32px 22px;text-align:center;color:var(--text-muted);
  font-size:13px;line-height:1.55}
.contact{display:flex;align-items:center;gap:11px;padding:8px 10px;
  border-radius:8px;cursor:pointer;transition:background .1s;user-select:none}
.contact:hover{background:var(--bg-hover)}
.contact.active{background:var(--bg-active)}
.avatar-wrap{position:relative;flex-shrink:0}
.status-dot{position:absolute;right:-2px;bottom:-2px;width:14px;height:14px;
  border-radius:50%;background:#b9bbbe;border:3px solid var(--bg-secondary)}
.status-dot.online{background:var(--green)}
.status-dot.unknown{background:#c7ccd1}
.contact.active .status-dot{border-color:var(--bg-active)}
.contact-info{flex:1;min-width:0}
.contact-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;color:var(--text-normal);display:flex;align-items:baseline;gap:4px}
.contact-name .dom{color:var(--text-muted);font-weight:400;font-size:12px}
.contact-preview{font-size:13px;color:var(--text-muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.contact.unread .contact-name{color:#000}
.contact.unread .contact-preview{color:var(--text-normal);font-weight:500}
.badge{background:var(--red);color:#fff;font-size:12px;font-weight:600;
  min-width:20px;height:20px;padding:0 7px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;line-height:1}

#chatPane{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg-primary)}
#emptyState{flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;color:var(--text-muted);gap:14px;padding:24px;text-align:center}
#emptyState svg{opacity:.28}
#emptyState p{font-size:14px}
#conversation{display:none;flex-direction:column;flex:1;min-height:0}
#chatPane.has-chat #conversation{display:flex}
#chatPane.has-chat #emptyState{display:none}
.conv-header{display:flex;align-items:center;gap:8px;padding:10px 14px;
  padding-top:calc(10px + env(safe-area-inset-top));
  border-bottom:1px solid var(--border);background:var(--bg-primary)}
#backBtn{display:none}
.conv-peer{display:flex;flex-direction:column;min-width:0;flex:1}
.peer-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.peer-sub{font-size:12px;color:var(--text-muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.peer-sub.online{color:var(--green)}
#msgs{flex:1;overflow-y:auto;padding:16px 16px 6px;display:flex;
  flex-direction:column;gap:4px;scroll-behavior:smooth}
.m{max-width:70%;padding:8px 13px;border-radius:16px;background:var(--bg-secondary);
  align-self:flex-start;word-wrap:break-word;overflow-wrap:anywhere;
  animation:pop .13s ease-out;font-size:15px;line-height:1.4}
@keyframes pop{from{opacity:.4;transform:translateY(3px)}to{opacity:1;transform:none}}
.m.me{align-self:flex-end;background:var(--accent);color:#fff}
.m .ts{font-size:11px;opacity:.65;margin-top:2px;display:block;text-align:right}
.m.me .ts{opacity:.85}
.m.broken{background:transparent;border:1px dashed var(--border);
  color:var(--text-muted);font-style:italic}
.day-sep{align-self:center;font-size:11px;color:var(--text-muted);
  padding:6px 12px;background:var(--bg-secondary);border-radius:10px;margin:8px 0 4px}
#composer{display:flex;align-items:center;gap:8px;padding:10px 14px;
  padding-bottom:calc(10px + env(safe-area-inset-bottom));
  border-top:1px solid var(--border);background:var(--bg-primary)}
#inp{flex:1;background:var(--bg-secondary);border:1px solid transparent;
  color:var(--text-normal);padding:11px 16px;border-radius:20px;outline:none;
  font-size:16px;min-width:0;transition:border .12s}
#inp:focus{border-color:var(--accent)}
#sendBtn{width:42px;height:42px;border-radius:50%;background:var(--accent);
  color:#fff;border:none;display:flex;align-items:center;justify-content:center;
  flex-shrink:0;transition:background .12s,transform .06s}
#sendBtn:hover{background:var(--accent-hover)}
#sendBtn:active{transform:scale(.94)}

.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.5);
  display:none;align-items:center;justify-content:center;padding:20px;z-index:200}
.modal-backdrop.open{display:flex}
.modal{background:#fff;border-radius:14px;padding:22px;width:100%;max-width:420px;
  box-shadow:0 20px 60px rgba(0,0,0,.25)}
.modal h3{font-size:18px;margin-bottom:6px;font-weight:700}
.modal .hint{color:var(--text-muted);font-size:13px;margin-bottom:14px;line-height:1.5}
.modal input{width:100%;padding:12px 14px;border-radius:8px;
  border:1px solid var(--border);background:var(--bg-secondary);
  font-size:16px;outline:none;margin-bottom:10px;color:var(--text-normal)}
.modal input:focus{border-color:var(--accent)}
.modal .err{color:var(--red);font-size:13px;min-height:18px;margin-bottom:6px}
.modal-actions{display:flex;gap:8px;justify-content:flex-end}
.btn-secondary{background:var(--bg-secondary);border:none;padding:10px 16px;
  border-radius:8px;font-weight:500;color:var(--text-normal);transition:background .12s}
.btn-secondary:hover{background:var(--bg-hover)}
.btn-primary{background:var(--accent);color:#fff;border:none;
  padding:10px 22px;border-radius:8px;font-weight:600;transition:background .12s}
.btn-primary:hover:not(:disabled){background:var(--accent-hover)}
.btn-primary:disabled,.btn-secondary:disabled{cursor:default}

#toast{position:fixed;left:50%;bottom:40px;transform:translateX(-50%) translateY(20px);
  background:#2e3338;color:#fff;padding:11px 18px;border-radius:8px;font-size:14px;
  opacity:0;pointer-events:none;transition:opacity .2s, transform .2s;z-index:300;
  box-shadow:0 8px 24px rgba(0,0,0,.25);max-width:80%;text-align:center;
  word-break:break-all;line-height:1.4}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

@media (max-width:720px){
  #app{position:relative;overflow:hidden}
  #sidebar{position:absolute;inset:0;width:100%;z-index:2;
    border-right:none;transition:transform .26s ease}
  #chatPane{position:absolute;inset:0;z-index:3;transform:translateX(100%);
    transition:transform .26s ease;box-shadow:-8px 0 24px rgba(0,0,0,.10)}
  #app.chat-open #sidebar{transform:translateX(-26%)}
  #app.chat-open #chatPane{transform:translateX(0)}
  #backBtn{display:flex}
  .m{max-width:86%}
}
</style>
</head>
<body>

<div id="login">
  <form class="card" id="authForm" autocomplete="on">
    <div class="tabs">
      <button type="button" id="tabLogin" class="active">Вход</button>
      <button type="button" id="tabReg">Регистрация</button>
    </div>
    <input id="loginIn" type="text" placeholder="Логин" autocapitalize="off"
           spellcheck="false" maxlength="24" autocomplete="username">
    <input id="pwIn" type="password" placeholder="Пароль" autocomplete="current-password">
    <label class="remember">
      <input type="checkbox" id="rememberIn">
      <span>Запомнить меня</span>
    </label>
    <button type="submit" id="submitBtn">Войти</button>
    <div id="authErr"></div>
    <div id="authNote"></div>
  </form>
</div>

<div id="app">
  <aside id="sidebar">
    <div class="sidebar-header">
      <div class="avatar" id="myAvatar">?</div>
      <div class="my-info" id="myInfo" title="Нажмите, чтобы скопировать адрес">
        <div class="my-name" id="myLogin">—</div>
        <div class="my-sub" id="myDomain"></div>
      </div>
      <button class="icon-btn" id="addBtn" title="Добавить чат" aria-label="Добавить">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/>
        </svg>
      </button>
      <button class="icon-btn" id="logoutBtn" title="Выйти" aria-label="Выйти">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M16 13v-2H7V8l-5 4 5 4v-3zM20 3h-8v2h8v14h-8v2h8a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z"/>
        </svg>
      </button>
    </div>
    <div class="sidebar-title">Чаты</div>
    <div id="contacts"></div>
  </aside>

  <section id="chatPane">
    <div id="emptyState">
      <svg width="90" height="90" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM7 9h10v2H7V9zm6 5H7v-2h6v2zm4-6H7V6h10v2z"/>
      </svg>
      <p>Выберите чат или добавьте новый</p>
    </div>
    <div id="conversation">
      <div class="conv-header">
        <button class="icon-btn" id="backBtn" aria-label="Назад">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/>
          </svg>
        </button>
        <div class="conv-peer">
          <div class="peer-name" id="peerName">—</div>
          <div class="peer-sub" id="peerSub"></div>
        </div>
      </div>
      <div id="msgs"></div>
      <div id="composer">
        <input id="inp" placeholder="Написать сообщение…" autocomplete="off"
               autocapitalize="sentences">
        <button id="sendBtn" aria-label="Отправить">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M2.01 21 23 12 2.01 3 2 10l15 2-15 2z"/>
          </svg>
        </button>
      </div>
    </div>
  </section>
</div>

<div class="modal-backdrop" id="addModal">
  <div class="modal">
    <h3>Новый чат</h3>
    <div class="hint">
      Введите логин (<b>bob</b>) для пользователя этого сервера
      или полный адрес (<b>bob@server.com</b>) для пользователя другого сервера.
    </div>
    <input id="addInput" placeholder="bob или bob@server.com"
           autocapitalize="off" spellcheck="false" autocomplete="off">
    <div class="err" id="addErr"></div>
    <div class="modal-actions">
      <button class="btn-secondary" id="addCancel" type="button">Отмена</button>
      <button class="btn-primary" id="addConfirm" type="button">Добавить</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
(() => {
"use strict";

/* ================= PROTOCOL ================= */
const T = {REGISTER:1, AUTH:2, AUTH_OK:3, MSG:4, PING:5, PONG:6, ERROR:7,
           HELLO:8, USER_STATUS:9, SYNC:10, USERS:11, INTRO:12, CONTACT_ADD:13};
const _enc = new TextEncoder(), _dec = new TextDecoder();

function pack(type, obj){
  const p = _enc.encode(JSON.stringify(obj));
  const buf = new ArrayBuffer(5 + p.length);
  const dv = new DataView(buf);
  dv.setUint8(0, type);
  dv.setUint32(1, p.length, false);
  new Uint8Array(buf, 5).set(p);
  return buf;
}
function unpack(buf){
  const dv = new DataView(buf);
  const len = dv.getUint32(1, false);
  return [dv.getUint8(0), JSON.parse(_dec.decode(new Uint8Array(buf, 5, len)))];
}

/* ================= CRYPTO ================= */
function b64(u8){ let s=""; for(let i=0;i<u8.length;i++) s+=String.fromCharCode(u8[i]); return btoa(s); }
function unb64(s){ const b=atob(s); const u=new Uint8Array(b.length); for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i); return u; }
function b64urlToBytes(s){
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  return unb64(s);
}
// восстанавливаем raw pubkey (0x04 || X || Y) из приватного JWK
function jwkToRawPub(jwk){
  if (!jwk || !jwk.x || !jwk.y) return null;
  const x = b64urlToBytes(jwk.x);
  const y = b64urlToBytes(jwk.y);
  if (x.length !== 32 || y.length !== 32) return null;
  const raw = new Uint8Array(65);
  raw[0] = 4;
  raw.set(x, 1);
  raw.set(y, 33);
  return b64(raw);
}

async function genKeyPair(){
  return crypto.subtle.generateKey({name:"ECDH",namedCurve:"P-256"},true,["deriveKey"]);
}
async function exportPubRaw(pub){
  return b64(new Uint8Array(await crypto.subtle.exportKey("raw", pub)));
}
async function exportPrivJWK(priv){ return await crypto.subtle.exportKey("jwk", priv); }
async function importPrivJWK(jwk){
  return crypto.subtle.importKey("jwk",jwk,{name:"ECDH",namedCurve:"P-256"},true,["deriveKey"]);
}
async function importPubRaw(str){
  return crypto.subtle.importKey("raw",unb64(str),{name:"ECDH",namedCurve:"P-256"},true,[]);
}
async function deriveAesKey(priv, pub){
  return crypto.subtle.deriveKey({name:"ECDH",public:pub},priv,
    {name:"AES-GCM",length:256},false,["encrypt","decrypt"]);
}
async function aesEncrypt(key, text){
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const buf = await crypto.subtle.encrypt({name:"AES-GCM",iv}, key, _enc.encode(text));
  return {ct: b64(new Uint8Array(buf)), iv: b64(iv)};
}
async function aesDecrypt(key, ct, iv){
  const pt = await crypto.subtle.decrypt({name:"AES-GCM",iv:unb64(iv)}, key, unb64(ct));
  return _dec.decode(pt);
}

/* ================= STATE ================= */
let ws = null;
let helloDomain = null;
let me = null;
let myPrivKey = null;
let myPubRaw = null;
let sessionPassword = null;
let contacts = [];
const convKeys = {};      // uid -> CryptoKey
const threads = {};       // uid -> [{...}]
const unread = {};
const seenIds = new Set();
let activePeer = null;
let reconnectAttempts = 0;
let heartbeatTimer = null;
let mode = "login";
let authPayload = null;

const $ = id => document.getElementById(id);
const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
                   : Date.now().toString(36) + Math.random().toString(36).slice(2,10));

/* ================= UTILS ================= */
function splitUid(uid){
  const i = uid.indexOf("@");
  if (i < 0) return [uid, ""];
  return [uid.slice(0, i), uid.slice(i+1)];
}
function avatarColor(uid){
  const colors = ["#5865f2","#3ba55d","#faa61a","#ed4245","#eb459e","#9b59b6","#1abc9c","#e67e22"];
  let h = 0;
  for (let i=0;i<uid.length;i++) h = (h * 31 + uid.charCodeAt(i)) | 0;
  return colors[Math.abs(h) % colors.length];
}
function avatarChar(uid){ return (uid || "?")[0].toUpperCase(); }

let toastTimer = null;
function toast(msg){
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 1800);
}

/* ================= STORAGE ================= */
// ключ зависит от домена текущего сервера (location.host)
function storeKeyFor(login){
  return "fed_key_" + login + "@" + location.host;
}
function storeKeyOld(login){
  return "fed_key_" + login;
}
function lsContactsKey(){ return "fed_contacts_" + (me ? me.uid : "unknown"); }
const LS_REMEMBER = "fed_remember";

function saveContacts(){
  try {
    localStorage.setItem(lsContactsKey(),
      JSON.stringify(contacts.map(c => ({uid: c.uid, pub: c.pub}))));
  } catch(e){}
}
function loadContacts(){
  try {
    const raw = localStorage.getItem(lsContactsKey());
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return arr.map(c => ({uid: c.uid, pub: c.pub, online: null}));
  } catch(e){ return []; }
}

/* ================= CONTACT LIST ================= */
function renderContacts(){
  const box = $("contacts");
  box.innerHTML = "";
  if (!contacts.length){
    const d = document.createElement("div");
    d.className = "empty-list";
    d.innerHTML = "Пока нет чатов.<br>Нажмите <b>+</b> сверху, чтобы добавить пользователя по логину или адресу.";
    box.appendChild(d);
    return;
  }
  const sorted = contacts.slice().sort((a,b) => {
    const ua = unread[a.uid] > 0 ? 1 : 0;
    const ub = unread[b.uid] > 0 ? 1 : 0;
    if (ua !== ub) return ub - ua;
    const la = threads[a.uid] || [], lb = threads[b.uid] || [];
    const ta = la.length ? la[la.length-1].ts : 0;
    const tb = lb.length ? lb[lb.length-1].ts : 0;
    if (ta !== tb) return tb - ta;
    const oa = a.online === true ? 1 : 0, ob = b.online === true ? 1 : 0;
    if (oa !== ob) return ob - oa;
    return a.uid.localeCompare(b.uid);
  });
  for (const c of sorted){
    const isActive = c.uid === activePeer;
    const hasUnread = unread[c.uid] > 0;
    const el = document.createElement("div");
    el.className = "contact" + (isActive ? " active" : "") + (hasUnread ? " unread" : "");
    el.onclick = () => selectPeer(c.uid);

    const wrap = document.createElement("div");
    wrap.className = "avatar-wrap";
    const av = document.createElement("div");
    av.className = "avatar";
    av.style.background = avatarColor(c.uid);
    av.textContent = avatarChar(c.uid);
    const dot = document.createElement("span");
    const st = c.online === true ? "online" : (c.online === false ? "" : "unknown");
    dot.className = "status-dot " + st;
    wrap.append(av, dot);

    const info = document.createElement("div");
    info.className = "contact-info";
    const nm = document.createElement("div");
    nm.className = "contact-name";
    const [login, dom] = splitUid(c.uid);
    const nameSpan = document.createElement("span");
    nameSpan.textContent = login;
    nm.appendChild(nameSpan);
    if (dom && helloDomain && dom !== helloDomain){
      const d = document.createElement("span");
      d.className = "dom";
      d.textContent = "@" + dom;
      nm.appendChild(d);
    }
    const pv = document.createElement("div");
    pv.className = "contact-preview";
    const t = threads[c.uid];
    if (t && t.length){
      const last = t[t.length-1];
      const mine = last.from === me.uid;
      pv.textContent = (mine ? "Вы: " : "") + (last.broken ? "⚠ зашифровано" : last.text);
    } else {
      pv.textContent = c.online === true ? "в сети"
                    : (c.online === false ? "не в сети" : "статус неизвестен");
    }
    info.append(nm, pv);
    el.append(wrap, info);
    if (hasUnread){
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = unread[c.uid] > 99 ? "99+" : unread[c.uid];
      el.appendChild(b);
    }
    box.appendChild(el);
  }
}

/* ================= SELECT ================= */
function selectPeer(uid){
  activePeer = uid;
  unread[uid] = 0;
  const [login, dom] = splitUid(uid);
  $("peerName").textContent = login;
  const c = contacts.find(x => x.uid === uid);
  const sub = $("peerSub");
  const parts = [];
  if (dom && helloDomain && dom !== helloDomain) parts.push("@" + dom);
  if (c){
    if (c.online === true) parts.push("в сети");
    else if (c.online === false) parts.push("не в сети");
    else if (dom && dom !== helloDomain) parts.push("другой сервер");
  }
  sub.textContent = parts.join(" · ");
  sub.className = "peer-sub" + (c && c.online === true ? " online" : "");
  $("chatPane").classList.add("has-chat");
  $("app").classList.add("chat-open");
  renderContacts();
  renderThread();
  setTimeout(() => $("inp").focus(), 60);
}
function goBack(){ $("app").classList.remove("chat-open"); }

/* ================= THREAD ================= */
function fmtTime(ts){ return new Date(ts).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}); }
function fmtDay(ts){
  const d = new Date(ts), t = new Date();
  if (d.toDateString() === t.toDateString()) return "Сегодня";
  const y = new Date(t); y.setDate(y.getDate()-1);
  if (d.toDateString() === y.toDateString()) return "Вчера";
  return d.toLocaleDateString();
}
function buildMsgEl(m){
  const mine = m.from === me.uid;
  const el = document.createElement("div");
  el.className = "m" + (mine ? " me" : "") + (m.broken ? " broken" : "");
  const txt = document.createElement("div");
  txt.textContent = m.text;
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = fmtTime(m.ts);
  el.append(txt, ts);
  return el;
}
function appendMsg(m){
  const box = $("msgs");
  const day = fmtDay(m.ts);
  const seps = box.querySelectorAll(".day-sep");
  if (!seps.length || seps[seps.length-1].textContent !== day){
    const s = document.createElement("div");
    s.className = "day-sep"; s.textContent = day;
    box.appendChild(s);
  }
  box.appendChild(buildMsgEl(m));
  box.scrollTop = box.scrollHeight;
}
function renderThread(){
  const box = $("msgs");
  box.innerHTML = "";
  if (!activePeer) return;
  const list = threads[activePeer] || [];
  let lastDay = "";
  for (const m of list){
    const day = fmtDay(m.ts);
    if (day !== lastDay){
      lastDay = day;
      const s = document.createElement("div");
      s.className = "day-sep"; s.textContent = day;
      box.appendChild(s);
    }
    box.appendChild(buildMsgEl(m));
  }
  box.scrollTop = box.scrollHeight;
}

/* ================= CONTACT / DECRYPT ================= */
async function getConvKeyFromPub(uid, pub){
  const cacheKey = uid + "|" + pub.slice(0, 16);
  if (convKeys[cacheKey]) return convKeys[cacheKey];
  try {
    const p = await importPubRaw(pub);
    const k = await deriveAesKey(myPrivKey, p);
    convKeys[cacheKey] = k;
    return k;
  } catch(e){ return null; }
}

/**
 * Пытаемся расшифровать сообщение, используя несколько возможных pubkey:
 * сначала актуальный from_pub (из самого сообщения), потом сохранённый в контакте.
 * Если расшифровка удалась с from_pub и он отличается от сохранённого —
 * обновляем контакт (это исправляет устаревшие pubkey).
 */
async function tryDecryptIncoming(m, peerUid){
  const contact = contacts.find(c => c.uid === peerUid);
  const incomingPub = (m.from !== me.uid && m.from_pub) ? m.from_pub : "";
  const candidates = [];
  if (incomingPub) candidates.push(incomingPub);
  if (contact && contact.pub && contact.pub !== incomingPub) candidates.push(contact.pub);

  for (const pub of candidates){
    const key = await getConvKeyFromPub(peerUid, pub);
    if (!key) continue;
    try {
      const text = await aesDecrypt(key, m.ct, m.iv);
      // расшифровка удалась: синхронизируем pub в контакте, если он был неверный
      if (contact){
        if (incomingPub && contact.pub !== incomingPub){
          contact.pub = incomingPub;
          saveContacts();
        }
      } else if (incomingPub){
        contacts.push({uid: peerUid, pub: incomingPub, online: null});
        saveContacts();
        renderContacts();
      }
      return {text, broken: false};
    } catch(e){ /* пробуем следующий pub */ }
  }
  return null;
}

/**
 * Убедиться, что контакт существует. Возвращает контакт или null.
 * Если pub из сообщения есть — используем его.
 */
async function ensureContact(peerUid, preferredPub){
  let c = contacts.find(x => x.uid === peerUid);
  if (preferredPub){
    if (!c){
      c = {uid: peerUid, pub: preferredPub, online: null};
      contacts.push(c); saveContacts(); renderContacts();
    } else if (c.pub !== preferredPub){
      c.pub = preferredPub; saveContacts();
    }
    return c;
  }
  if (c) return c;
  try {
    const r = await fetch("/api/pubkey?uid=" + encodeURIComponent(peerUid));
    if (r.ok){
      const d = await r.json();
      c = {uid: peerUid, pub: d.pub, online: d.online};
      contacts.push(c); saveContacts(); renderContacts();
      return c;
    }
  } catch(e){}
  return null;
}

/* ================= WS ================= */
function connect(){
  return new Promise((resolve, reject) => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.binaryType = "arraybuffer";
    let settled = false;
    const to = setTimeout(() => {
      if (!settled){ settled = true; try{ws.close();}catch(e){}; reject(new Error("Таймаут")); }
    }, 10000);
    ws.onopen = () => ws.send(pack(mode === "register" ? T.REGISTER : T.AUTH, authPayload));
    ws.onmessage = async ev => {
      let type, obj;
      try { [type, obj] = unpack(ev.data); } catch(e){ return; }
      if (type === T.HELLO){ helloDomain = obj.domain; return; }
      if (!settled){
        if (type === T.AUTH_OK){ settled = true; clearTimeout(to); resolve(obj); return; }
        if (type === T.ERROR){ settled = true; clearTimeout(to);
          try{ws.close();}catch(e){}; reject(new Error(obj.msg || "Ошибка")); return; }
      }
      await handleFrame(type, obj);
    };
    ws.onclose = () => {
      if (!settled){ settled = true; clearTimeout(to); reject(new Error("Соединение закрыто")); }
      else if (me) onDisconnect();
    };
    ws.onerror = () => {
      if (!settled){ settled = true; clearTimeout(to); reject(new Error("Ошибка соединения")); }
    };
  });
}

async function handleFrame(type, obj){
  switch(type){
    case T.MSG: await onIncomingMsg(obj); break;
    case T.CONTACT_ADD: await onContactAdd(obj); break;
    case T.USER_STATUS: {
      const c = contacts.find(x => x.uid === obj.uid);
      if (c){
        c.online = obj.online;
        renderContacts();
        if (activePeer === obj.uid){
          const sub = $("peerSub");
          const [_, dom] = splitUid(obj.uid);
          const parts = [];
          if (dom && helloDomain && dom !== helloDomain) parts.push("@" + dom);
          parts.push(obj.online ? "в сети" : "не в сети");
          sub.textContent = parts.join(" · ");
          sub.className = "peer-sub" + (obj.online ? " online" : "");
        }
      }
      break;
    }
    case T.USERS: {
      for (const u of (obj.users || [])){
        const c = contacts.find(x => x.uid === u.uid);
        if (c) c.online = u.online;
      }
      renderContacts();
      break;
    }
    case T.PING: if (ws && ws.readyState === 1) ws.send(pack(T.PONG, {})); break;
    case T.PONG: break;
    case T.ERROR: console.warn("server:", obj.msg); break;
  }
}

async function onContactAdd(obj){
  const uid = (obj.uid || "").toLowerCase();
  if (!uid || (me && uid === me.uid)) return;
  const existing = contacts.find(c => c.uid === uid);
  if (existing){
    if (obj.pub && existing.pub !== obj.pub){
      existing.pub = obj.pub;
      saveContacts();
    }
    return;
  }
  const contact = {uid, pub: obj.pub || "", online: null};
  if (!contact.pub){
    try {
      const r = await fetch("/api/pubkey?uid=" + encodeURIComponent(uid));
      if (r.ok){ const d = await r.json(); contact.pub = d.pub; }
    } catch(e){}
  }
  contacts.push(contact);
  saveContacts();
  renderContacts();
  if (ws && ws.readyState === 1) ws.send(pack(T.SYNC, {uids: [uid]}));
  const [login] = splitUid(uid);
  toast(login + " добавил(а) вас в контакты");
}

async function onIncomingMsg(m){
  if (seenIds.has(m.id)) return;
  seenIds.add(m.id);
  const peerUid = m.from === me.uid ? m.to : m.from;

  // Гарантируем контакт (для входящих — сразу с from_pub, это авторитетный источник)
  await ensureContact(peerUid, m.from !== me.uid ? m.from_pub : "");
  // Пробуем расшифровать (использует from_pub И contact.pub)
  const result = await tryDecryptIncoming(m, peerUid);
  const msg = {
    id: m.id, from: m.from, to: m.to,
    text: result ? result.text : "⚠ не удалось расшифровать",
    ts: m.ts,
    broken: !result,
  };
  (threads[peerUid] = threads[peerUid] || []).push(msg);
  if (activePeer === peerUid){
    appendMsg(msg);
    renderContacts();
  } else {
    unread[peerUid] = (unread[peerUid] || 0) + 1;
    renderContacts();
  }
}

/* ================= AUTH ================= */
function setErr(m){ $("authErr").textContent = m || ""; }
function setNote(m){ $("authNote").textContent = m || ""; }
function setBusy(btn, busy){ btn.disabled = busy; btn.classList.toggle("busy", busy); }

async function doAuth(login, password, remember){
  login = (login || "").trim().toLowerCase();
  if (!login || !password){ setErr("Заполните все поля"); return; }
  if (!/^[a-z0-9._-]{3,24}$/.test(login)){ setErr("Логин 3–24: a-z 0-9 . _ -"); return; }
  if (mode === "register" && password.length < 6){ setErr("Пароль минимум 6 символов"); return; }
  setErr(""); setNote("");

  try {
    if (mode === "register"){
      // Новая регистрация — всегда новый ключ, сервер сохранит pub при REGISTER
      const kp = await genKeyPair();
      myPrivKey = kp.privateKey;
      const jwk = await exportPrivJWK(kp.privateKey);
      myPubRaw = await exportPubRaw(kp.publicKey);
      // сохраняем и JWK, и pub (pub понадобится при login)
      try {
        localStorage.setItem(storeKeyFor(login), JSON.stringify(jwk));
        localStorage.setItem(storeKeyFor(login) + ":pub", myPubRaw);
        localStorage.removeItem(storeKeyOld(login));
      } catch(e){}
      authPayload = {login, password, pub: myPubRaw};
    } else {
      // Вход: сначала пробуем достать сохранённый ключ
      let saved = null, savedPub = null;
      try {
        saved = localStorage.getItem(storeKeyFor(login))
             || localStorage.getItem(storeKeyOld(login));
        savedPub = localStorage.getItem(storeKeyFor(login) + ":pub");
      } catch(e){}

      if (saved){
        try {
          const jwk = JSON.parse(saved);
          myPrivKey = await importPrivJWK(jwk);
          // ВАЖНО: pub восстанавливаем ИЗ САМОГО КЛЮЧА, а не тянем с сервера
          myPubRaw = jwkToRawPub(jwk) || savedPub;
        } catch(e){ setNote("⚠ Ключ повреждён."); }
      }

      if (!myPrivKey){
        // Нет сохранённого — генерируем новый
        setNote("⚠ Приватный ключ не найден в этом браузере — старые сообщения не расшифруются.");
        const kp = await genKeyPair();
        myPrivKey = kp.privateKey;
        const jwk = await exportPrivJWK(kp.privateKey);
        myPubRaw = await exportPubRaw(kp.publicKey);
        try {
          localStorage.setItem(storeKeyFor(login), JSON.stringify(jwk));
          localStorage.setItem(storeKeyFor(login) + ":pub", myPubRaw);
        } catch(e){}
      }

      // Всегда отправляем актуальный pub — сервер обновит у себя, если расходится
      authPayload = {login, password};
      if (myPubRaw) authPayload.pub = myPubRaw;
    }

    setBusy($("submitBtn"), true);
    const ok = await connect();
    sessionPassword = password;
    me = {login: ok.login, uid: ok.uid};

    // На login: если сервер вернул свой pub и он совпадает с нашим — ок.
    // Если у нас нет своего pub (маловероятно) — подсосём с сервера.
    if (!myPubRaw){
      try {
        const r = await fetch("/api/pubkey?uid=" + encodeURIComponent(me.uid));
        if (r.ok){ const d = await r.json(); myPubRaw = d.pub; }
      } catch(e){}
    }

    if (remember){
      try { localStorage.setItem(LS_REMEMBER, JSON.stringify({login: me.login, password})); }
      catch(e){}
    } else {
      try { localStorage.removeItem(LS_REMEMBER); } catch(e){}
    }

    $("myLogin").textContent = me.login;
    $("myDomain").textContent = "@" + (helloDomain || "");
    $("myAvatar").textContent = avatarChar(me.login);
    $("myAvatar").style.background = avatarColor(me.uid);
    $("login").style.display = "none";
    $("app").classList.add("on");

    contacts = loadContacts();
    renderContacts();
    await loadHistory(ok.history || []);
    await applyPendingIntros(ok.pending_intros || []);
    if (contacts.length){
      ws.send(pack(T.SYNC, {uids: contacts.map(c => c.uid)}));
    }
    startHeartbeat();
    reconnectAttempts = 0;
  } catch(e){
    setErr(e.message || "Ошибка");
    setBusy($("submitBtn"), false);
  }
}

async function applyPendingIntros(intros){
  let changed = false;
  for (const intro of intros){
    const uid = (intro.uid || "").toLowerCase();
    if (!uid || uid === me.uid) continue;
    const ex = contacts.find(c => c.uid === uid);
    if (ex){
      if (intro.pub && ex.pub !== intro.pub){ ex.pub = intro.pub; changed = true; }
      continue;
    }
    contacts.push({uid, pub: intro.pub || "", online: null});
    changed = true;
  }
  if (changed){ saveContacts(); renderContacts(); }
}

async function loadHistory(history){
  const byPeer = {};
  for (const m of history){
    const peer = m.from === me.uid ? m.to : m.from;
    (byPeer[peer] = byPeer[peer] || []).push(m);
    seenIds.add(m.id);
    if (m.from !== me.uid && m.from_pub){
      const ex = contacts.find(c => c.uid === m.from);
      if (!ex){ contacts.push({uid: m.from, pub: m.from_pub, online: null}); }
      else if (ex.pub !== m.from_pub){ ex.pub = m.from_pub; }
    }
  }
  saveContacts();
  for (const peer of Object.keys(byPeer)){
    await ensureContact(peer, "");
    const list = byPeer[peer].sort((a,b) => a.ts - b.ts);
    const out = [];
    for (const m of list){
      const r = await tryDecryptIncoming(m, peer);
      out.push({
        id: m.id, from: m.from, to: m.to,
        text: r ? r.text : "⚠ не удалось расшифровать",
        ts: m.ts, broken: !r,
      });
    }
    threads[peer] = out;
  }
  saveContacts();
  renderContacts();
}

/* ================= SEND ================= */
async function sendMessage(){
  if (!activePeer || !ws || ws.readyState !== 1) return;
  const inp = $("inp");
  const text = inp.value.trim();
  if (!text) return;
  const contact = contacts.find(c => c.uid === activePeer);
  if (!contact){ alert("Контакт не найден"); return; }
  if (!contact.pub){ alert("Нет ключа получателя"); return; }
  const key = await getConvKeyFromPub(activePeer, contact.pub);
  if (!key){ alert("Нет ключа получателя"); return; }
  const {ct, iv} = await aesEncrypt(key, text);
  const id = uuid();
  const localMsg = {id, from: me.uid, to: activePeer, text, ts: Date.now()};
  seenIds.add(id);
  (threads[activePeer] = threads[activePeer] || []).push(localMsg);
  appendMsg(localMsg);
  renderContacts();
  inp.value = "";
  inp.focus();
  ws.send(pack(T.MSG, {id, to: activePeer, ct, iv, from_pub: myPubRaw}));
}

/* ================= ADD CONTACT ================= */
function openAddModal(){
  $("addInput").value = "";
  $("addErr").textContent = "";
  $("addModal").classList.add("open");
  setTimeout(() => $("addInput").focus(), 40);
}
function closeAddModal(){
  $("addModal").classList.remove("open");
  setBusy($("addConfirm"), false);
}

async function addContactFromInput(){
  const raw = $("addInput").value.trim().toLowerCase();
  const err = $("addErr");
  err.textContent = "";
  if (!raw){ err.textContent = "Введите логин или адрес"; return; }

  let uid = raw;
  if (!uid.includes("@")){
    if (!helloDomain){ err.textContent = "Неизвестен домен сервера"; return; }
    uid = uid + "@" + helloDomain;
  }
  const [login, dom] = splitUid(uid);
  if (!login || !dom){ err.textContent = "Неверный формат"; return; }
  if (uid === me.uid){ err.textContent = "Это ваш собственный адрес"; return; }
  if (contacts.find(c => c.uid === uid)){ err.textContent = "Уже добавлен"; return; }

  setBusy($("addConfirm"), true);
  try {
    const r = await fetch("/api/pubkey?uid=" + encodeURIComponent(uid));
    if (!r.ok){
      const d = await r.json().catch(() => ({}));
      throw new Error(d.error || "Не удалось получить ключ");
    }
    const data = await r.json();
    contacts.push({uid: data.uid, pub: data.pub, online: data.online});
    saveContacts();
    renderContacts();
    if (ws && ws.readyState === 1){
      ws.send(pack(T.SYNC, {uids: [data.uid]}));
      const [_, d] = splitUid(data.uid);
      if (d && helloDomain && d !== helloDomain){
        ws.send(pack(T.INTRO, {to: data.uid}));
      }
    }
    closeAddModal();
    selectPeer(data.uid);
  } catch(e){
    err.textContent = e.message || "Ошибка";
    setBusy($("addConfirm"), false);
  }
}

/* ================= RECONNECT ================= */
function startHeartbeat(){
  clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => {
    if (ws && ws.readyState === 1) ws.send(pack(T.PING, {t: Date.now()}));
  }, 25000);
}
async function onDisconnect(){
  clearInterval(heartbeatTimer);
  if (!sessionPassword) return;
  if (reconnectAttempts >= 5){ alert("Соединение потеряно. Обновите страницу."); return; }
  reconnectAttempts++;
  try {
    authPayload = {login: me.login, password: sessionPassword};
    if (myPubRaw) authPayload.pub = myPubRaw;
    mode = "login";
    const ok = await connect();
    me = {login: ok.login, uid: ok.uid};
    if (contacts.length) ws.send(pack(T.SYNC, {uids: contacts.map(c => c.uid)}));
    startHeartbeat();
    reconnectAttempts = 0;
  } catch(e){
    setTimeout(onDisconnect, 800 * reconnectAttempts);
  }
}

/* ================= COPY MY UID ================= */
async function copyMyUid(){
  if (!me || !helloDomain) return;
  const full = me.login + "@" + helloDomain;
  try {
    await navigator.clipboard.writeText(full);
    toast("Скопировано: " + full);
  } catch(e){
    try {
      const ta = document.createElement("textarea");
      ta.value = full;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      toast("Скопировано: " + full);
    } catch(e2){ toast("Не удалось скопировать: " + full); }
  }
}

/* ================= UI BIND ================= */
function setMode(m){
  mode = m;
  $("tabLogin").classList.toggle("active", m === "login");
  $("tabReg").classList.toggle("active", m === "register");
  $("submitBtn").textContent = m === "login" ? "Войти" : "Создать аккаунт";
  $("pwIn").autocomplete = m === "login" ? "current-password" : "new-password";
  setErr(""); setNote("");
}
$("tabLogin").onclick = () => setMode("login");
$("tabReg").onclick   = () => setMode("register");
$("authForm").addEventListener("submit", e => {
  e.preventDefault();
  if ($("submitBtn").disabled) return;
  doAuth($("loginIn").value, $("pwIn").value, $("rememberIn").checked);
});

$("logoutBtn").onclick = () => {
  sessionPassword = null;
  try { localStorage.removeItem(LS_REMEMBER); } catch(e){}
  try { ws && ws.close(); } catch(e){}
  location.reload();
};

$("addBtn").onclick = openAddModal;
$("addCancel").onclick = closeAddModal;
$("addConfirm").onclick = () => { if (!$("addConfirm").disabled) addContactFromInput(); };
$("addInput").addEventListener("keydown", e => {
  if (e.key === "Enter"){ e.preventDefault(); addContactFromInput(); }
  else if (e.key === "Escape"){ closeAddModal(); }
});
$("addModal").addEventListener("click", e => {
  if (e.target === $("addModal")) closeAddModal();
});

$("sendBtn").onclick = () => sendMessage();
$("inp").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); sendMessage(); }
});
$("backBtn").onclick = goBack;
$("myInfo").onclick = copyMyUid;

/* ================= AUTOLOGIN ================= */
(function boot(){
  setTimeout(() => {
    let auto = null;
    try {
      const raw = localStorage.getItem(LS_REMEMBER);
      if (raw) auto = JSON.parse(raw);
    } catch(e){}
    if (auto && auto.login && auto.password){
      $("loginIn").value = auto.login;
      $("pwIn").value = auto.password;
      $("rememberIn").checked = true;
      setTimeout(() => {
        if (!$("submitBtn").disabled){
          doAuth(auto.login, auto.password, true).catch(() => {
            try { localStorage.removeItem(LS_REMEMBER); } catch(e){}
          });
        }
      }, 250);
    } else {
      $("loginIn").focus();
    }
  }, 80);
})();

})();
</script>
</body>
</html>
"""


# ============================ RUN ============================
if __name__ == "__main__":
    print(f"Server starting on http://0.0.0.0:{PORT}")
    if PUBLIC_DOMAIN:
        print(f"Public domain: {PUBLIC_DOMAIN}")
    else:
        print("DOMAIN not set — will use Host header from first request")
    try:
        import uvloop  # type: ignore
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", loop="uvloop")
    except ImportError:
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
