# main.py
# pip install fastapi uvicorn
# Переменные окружения: SUPABASE_URL, SUPABASE_KEY (service_role)

import base64
import hashlib
import html as html_module
import ipaddress
import json
import os
import re
import secrets
import struct
import time
import zlib
from typing import Dict, List, Optional, Set
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import (FastAPI, Header, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

# ============================================================
#                    SUPABASE
# ============================================================

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or ""
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

if not SUPABASE_ENABLED:
    raise RuntimeError("SUPABASE_URL и SUPABASE_KEY обязательны")


def _sb_sync(method: str, path: str,
             json_data=None, params=None, prefer=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        url += "?" + urlencode(params)
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    body = json.dumps(json_data).encode("utf-8") if json_data is not None else None
    req = UrlRequest(url, data=body, method=method, headers=headers)
    try:
        with urlopen(req, timeout=10) as r:
            raw = r.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except HTTPError as e:
        try:
            err = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            err = ""
        print(f"[supabase] {method} {path} -> {e.code}: {err}")
        return None
    except Exception as e:
        print(f"[supabase] {method} {path} -> {e}")
        return None


async def sb(method: str, path: str,
             json_data=None, params=None, prefer=None):
    return await run_in_threadpool(
        _sb_sync, method, path, json_data, params, prefer
    )


# ============================================================
#                    ХЕЛПЕРЫ ПАРОЛЯ И АВАТАРА
# ============================================================

def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    return salt + "$" + hashlib.sha256((salt + pw).encode()).hexdigest()


def verify_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    return hashlib.sha256((salt + pw).encode()).hexdigest() == h


def normalize_avatar(avatar):
    if not isinstance(avatar, list) or len(avatar) != 64:
        return ["#ffffff"] * 64
    return [
        a if isinstance(a, str) and a.startswith("#") and len(a) == 7 else "#ffffff"
        for a in avatar
    ]


def _parse_ts(s):
    if isinstance(s, (int, float)):
        return float(s)
    if not s:
        return time.time()
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


# ============================================================
#                    DB: USERS
# ============================================================

async def db_get_user(nick: str) -> Optional[dict]:
    rows = await sb("GET", "users",
                    params={"select": "nick,name,password,avatar",
                            "nick": f"eq.{nick}", "limit": "1"})
    if not rows:
        return None
    u = rows[0]
    return {
        "nick": u["nick"],
        "name": u.get("name", u["nick"]),
        "password": u.get("password", ""),
        "avatar": normalize_avatar(u.get("avatar")),
    }


async def db_get_users(nicks: List[str]) -> Dict[str, dict]:
    """Batch-загрузка пользователей по списку ников."""
    nicks = [n for n in dict.fromkeys(nicks) if n]  # dedupe + keep order
    if not nicks:
        return {}
    rows = await sb("GET", "users",
                    params={"select": "nick,name,avatar",
                            "nick": f"in.({','.join(nicks)})"})
    out = {}
    for u in (rows or []):
        out[u["nick"]] = {
            "nick": u["nick"],
            "name": u.get("name", u["nick"]),
            "avatar": normalize_avatar(u.get("avatar")),
        }
    return out


async def db_create_user(nick: str, name: str, password: str, avatar: list):
    await sb("POST", "users",
             json_data={
                 "nick": nick,
                 "name": name,
                 "password": password,
                 "avatar": avatar,
             },
             prefer="return=minimal")


async def db_update_user(nick: str, name: str, avatar: list):
    await sb("PATCH", "users",
             params={"nick": f"eq.{nick}"},
             json_data={"name": name, "avatar": avatar},
             prefer="return=minimal")


# ============================================================
#                    DB: SESSIONS
# ============================================================

async def db_create_session(token: str, nick: str):
    await sb("POST", "sessions",
             json_data={"token": token, "nick": nick},
             prefer="return=minimal")


async def db_get_session(token: str) -> Optional[str]:
    rows = await sb("GET", "sessions",
                    params={"select": "nick",
                            "token": f"eq.{token}", "limit": "1"})
    if not rows:
        return None
    return rows[0]["nick"]


async def db_del_session(token: str):
    await sb("DELETE", "sessions", params={"token": f"eq.{token}"})


async def db_del_sessions_of(nick: str):
    await sb("DELETE", "sessions", params={"nick": f"eq.{nick}"})


# ============================================================
#                    DB: CONTACTS
# ============================================================

async def db_get_contacts(nick: str) -> Set[str]:
    rows = await sb("GET", "contacts",
                    params={"select": "contact_nick",
                            "user_nick": f"eq.{nick}"})
    return {r["contact_nick"] for r in (rows or [])}


async def db_add_contact(a: str, b: str):
    await sb("POST", "contacts",
             json_data={"user_nick": a, "contact_nick": b},
             prefer="resolution=merge-duplicates,return=minimal")


async def db_del_contact(a: str, b: str):
    await sb("DELETE", "contacts",
             params={"user_nick": f"eq.{a}", "contact_nick": f"eq.{b}"})


# ============================================================
#                    DB: BLACKLIST
# ============================================================

async def db_get_blacklist(nick: str) -> Set[str]:
    rows = await sb("GET", "blacklist",
                    params={"select": "blocked_nick",
                            "user_nick": f"eq.{nick}"})
    return {r["blocked_nick"] for r in (rows or [])}


async def db_add_blacklist(a: str, b: str):
    await sb("POST", "blacklist",
             json_data={"user_nick": a, "blocked_nick": b},
             prefer="resolution=merge-duplicates,return=minimal")


async def db_del_blacklist(a: str, b: str):
    await sb("DELETE", "blacklist",
             params={"user_nick": f"eq.{a}", "blocked_nick": f"eq.{b}"})


# ============================================================
#                    DB: MESSAGES
# ============================================================

async def db_save_message(m: dict):
    await sb("POST", "messages",
             json_data={
                 "id": m["id"],
                 "sender": m["from"],
                 "recipient": m["to"],
                 "body": m["text"],
             },
             prefer="return=minimal")


async def db_get_messages(a: str, b: str) -> List[dict]:
    """Возвращает переписку a↔b в порядке возрастания времени."""
    rows = await sb("GET", "messages", params={
        "select": "id,sender,recipient,body,created_at",
        "or": f"(and(sender.eq.{a},recipient.eq.{b}),"
              f"and(sender.eq.{b},recipient.eq.{a}))",
        "order": "created_at.asc",
    })
    return [
        {
            "id": r["id"],
            "from": r["sender"],
            "to": r["recipient"],
            "text": r.get("body", ""),
            "ts": _parse_ts(r.get("created_at")),
        }
        for r in (rows or [])
    ]


async def db_del_messages_between(a: str, b: str):
    await sb("DELETE", "messages",
             params={"sender": f"eq.{a}", "recipient": f"eq.{b}"})
    await sb("DELETE", "messages",
             params={"sender": f"eq.{b}", "recipient": f"eq.{a}"})


# ============================================================
#                    АВТОРИЗАЦИЯ / СЕССИИ
# ============================================================

CONNECTIONS: Dict[str, list] = {}   # только WebSocket — в памяти
OG_CACHE: Dict[str, dict] = {}      # кэш OG — в памяти (это просто кэш)


async def auth(token: Optional[str]) -> str:
    if not token:
        raise HTTPException(401, "Не авторизован")
    nick = await db_get_session(token)
    if not nick:
        raise HTTPException(401, "Сессия истекла")
    return nick


async def user_public(nick: str) -> Optional[dict]:
    u = await db_get_user(nick)
    if not u:
        return None
    return {"nick": u["nick"], "name": u["name"], "avatar": u["avatar"]}


async def push_to(nick: str, event: dict):
    conns = CONNECTIONS.get(nick)
    if not conns:
        return
    payload = json.dumps(event, ensure_ascii=False)
    dead = []
    for ws in list(conns):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            conns.remove(ws)
        except ValueError:
            pass
    if not conns:
        CONNECTIONS.pop(nick, None)


# ============================================================
#                         FASTAPI
# ============================================================

app = FastAPI(title="Direct")


class EncBody(BaseModel):
    data: str


def parse(body: EncBody) -> dict:
    try:
        return json.loads(dec_str(body.data))
    except Exception:
        raise HTTPException(400, "Повреждённый запрос")


# ============================================================
#                    ШИФРОВАНИЕ ЗАПРОСОВ
# ============================================================

XK = b"DirectSecret2024"


def _xor(b: bytes) -> bytes:
    return bytes(c ^ XK[i % len(XK)] for i, c in enumerate(b))


def enc_str(s: str) -> str:
    return base64.b64encode(_xor(s.encode("utf-8"))).decode()


def dec_str(s: str) -> str:
    return _xor(base64.b64decode(s.encode())).decode("utf-8")


# ============================================================
#                          WEBSOCKET
# ============================================================

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = Query("")):
    await websocket.accept()
    nick = await db_get_session(token) if token else None
    if not nick:
        await websocket.close(code=4001)
        return
    CONNECTIONS.setdefault(nick, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        conns = CONNECTIONS.get(nick)
        if conns and websocket in conns:
            conns.remove(websocket)
            if not conns:
                CONNECTIONS.pop(nick, None)


# ============================================================
#                          AUTH API
# ============================================================

@app.post("/api/register")
async def register(body: EncBody):
    d = parse(body)
    nick = (d.get("nick") or "").strip()
    name = (d.get("name") or "").strip()
    pw = d.get("password") or ""
    pw2 = d.get("password2") or ""

    if not nick or not name or not pw:
        raise HTTPException(400, "Заполните все поля")
    if len(nick) < 3:
        raise HTTPException(400, "Ник минимум 3 символа")
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", nick):
        raise HTTPException(400, "Ник: только буквы, цифры, _ . -")
    if pw != pw2:
        raise HTTPException(400, "Пароли не совпадают")
    if len(pw) < 4:
        raise HTTPException(400, "Пароль минимум 4 символа")

    # Проверка занятости — всегда из БД
    existing = await db_get_user(nick)
    if existing:
        raise HTTPException(400, "Такой ник уже занят")

    avatar = normalize_avatar(d.get("avatar"))
    await db_create_user(nick, name, hash_pw(pw), avatar)

    token = secrets.token_urlsafe(24)
    await db_create_session(token, nick)
    return {
        "ok": True,
        "token": token,
        "me": {"nick": nick, "name": name, "avatar": avatar},
    }


@app.post("/api/login")
async def login(body: EncBody):
    d = parse(body)
    nick = (d.get("nick") or "").strip()
    pw = d.get("password") or ""
    u = await db_get_user(nick)
    if not u or not verify_pw(pw, u["password"]):
        raise HTTPException(400, "Неверный ник или пароль")
    token = secrets.token_urlsafe(24)
    await db_create_session(token, nick)
    return {
        "ok": True,
        "token": token,
        "me": {"nick": nick, "name": u["name"], "avatar": u["avatar"]},
    }


@app.post("/api/logout")
async def logout(x_token: Optional[str] = Header(None)):
    if x_token:
        await db_del_session(x_token)
    return {"ok": True}


@app.get("/api/me")
async def me(x_token: Optional[str] = Header(None)):
    nick = await auth(x_token)
    u = await db_get_user(nick)
    if not u:
        raise HTTPException(401, "Пользователь не найден")

    contact_nicks = await db_get_contacts(nick)
    black_nicks = await db_get_blacklist(nick)

    all_nicks = list(contact_nicks | black_nicks)
    users_map = await db_get_users(all_nicks)

    contacts_out = [users_map[n] for n in contact_nicks if n in users_map]
    blacklist_out = [users_map[n] for n in black_nicks if n in users_map]

    return {
        "me": {"nick": nick, "name": u["name"], "avatar": u["avatar"]},
        "contacts": contacts_out,
        "blacklist": blacklist_out,
    }


@app.post("/api/profile/update")
async def profile_update(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = await auth(x_token)
    d = parse(body)
    name = (d.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")
    if len(name) > 60:
        raise HTTPException(400, "Имя слишком длинное")

    u = await db_get_user(nick)
    if not u:
        raise HTTPException(401, "Пользователь не найден")

    avatar = normalize_avatar(d.get("avatar")) if "avatar" in d else u["avatar"]
    await db_update_user(nick, name, avatar)

    me_pub = {"nick": nick, "name": name, "avatar": avatar}

    # оповещаем контакты
    contact_nicks = await db_get_contacts(nick)
    for c in contact_nicks:
        await push_to(c, {"type": "contact_updated", "user": me_pub})

    return {"ok": True, "me": me_pub}


# ============================================================
#                          SEARCH
# ============================================================

@app.post("/api/search")
async def search(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = await auth(x_token)
    d = parse(body)
    q = (d.get("nick") or "").strip().lstrip("@")
    if not q:
        raise HTTPException(400, "Введите ник")
    if q == nick:
        raise HTTPException(400, "Это ваш собственный ник")

    target = await db_get_user(q)
    if not target:
        raise HTTPException(404, "Пользователь не найден")

    my_blacklist = await db_get_blacklist(nick)
    if q in my_blacklist:
        raise HTTPException(403, "Пользователь в вашем чёрном списке")

    their_blacklist = await db_get_blacklist(q)
    if nick in their_blacklist:
        raise HTTPException(403, "Пользователь недоступен")

    return {"user": {"nick": q, "name": target["name"], "avatar": target["avatar"]}}


# ============================================================
#                           CHAT
# ============================================================

@app.post("/api/chat/start")
async def start_chat(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = await auth(x_token)
    d = parse(body)
    peer = (d.get("nick") or "").strip().lstrip("@")
    if peer == nick:
        raise HTTPException(400, "Некорректный пользователь")

    target = await db_get_user(peer)
    if not target:
        raise HTTPException(400, "Некорректный пользователь")

    my_bl = await db_get_blacklist(nick)
    if peer in my_bl:
        raise HTTPException(403, "Пользователь в чёрном списке")
    their_bl = await db_get_blacklist(peer)
    if nick in their_bl:
        raise HTTPException(403, "Пользователь недоступен")

    await db_add_contact(nick, peer)
    await db_add_contact(peer, nick)

    me_pub = await user_public(nick)
    peer_pub = {"nick": peer, "name": target["name"], "avatar": target["avatar"]}

    await push_to(peer, {"type": "contact_added", "user": me_pub})
    await push_to(nick, {"type": "contact_added", "user": peer_pub})
    return {"ok": True, "peer": peer_pub}


@app.post("/api/chat/send")
async def send_msg(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = await auth(x_token)
    d = parse(body)
    peer = d.get("to")
    text = (d.get("text") or "").strip()
    if not peer or not text:
        raise HTTPException(400, "Ошибка отправки")
    if len(text) > 2000:
        raise HTTPException(400, "Сообщение слишком длинное")

    target = await db_get_user(peer)
    if not target:
        raise HTTPException(400, "Пользователь не найден")

    my_bl = await db_get_blacklist(nick)
    if peer in my_bl:
        raise HTTPException(403, "Вы добавили пользователя в чёрный список")
    their_bl = await db_get_blacklist(peer)
    if nick in their_bl:
        raise HTTPException(403, "Пользователь добавил вас в чёрный список")

    # Авто-восстановление контакта, если один из пары удалил чат
    my_contacts = await db_get_contacts(nick)
    if peer not in my_contacts:
        await db_add_contact(nick, peer)
        await push_to(nick, {"type": "contact_added",
                             "user": {"nick": peer,
                                      "name": target["name"],
                                      "avatar": target["avatar"]}})

    their_contacts = await db_get_contacts(peer)
    if nick not in their_contacts:
        me_pub = await user_public(nick)
        await db_add_contact(peer, nick)
        await push_to(peer, {"type": "contact_added", "user": me_pub})

    msg = {
        "id": secrets.token_hex(8),
        "from": nick,
        "to": peer,
        "text": text,
        "ts": time.time(),
    }
    await db_save_message(msg)

    await push_to(peer, {"type": "message", "msg": msg})
    await push_to(nick, {"type": "message", "msg": msg, "self": True})
    return {"ok": True, "msg": msg}


@app.post("/api/chat/end")
async def end_chat(body: EncBody, x_token: Optional[str] = Header(None)):
    """Удаляет контакт с обеих сторон и всю переписку."""
    nick = await auth(x_token)
    d = parse(body)
    peer = (d.get("nick") or "").strip().lstrip("@")
    if peer == nick:
        raise HTTPException(400, "Некорректный пользователь")
    if not await db_get_user(peer):
        raise HTTPException(400, "Некорректный пользователь")

    await db_del_contact(nick, peer)
    await db_del_contact(peer, nick)
    await db_del_messages_between(nick, peer)

    await push_to(peer, {"type": "contact_removed", "nick": nick})
    await push_to(nick, {"type": "contact_removed", "nick": peer})
    return {"ok": True}


@app.post("/api/chat/messages")
async def messages(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = await auth(x_token)
    d = parse(body)
    peer = d.get("peer")
    target = await db_get_user(peer)
    if not target:
        raise HTTPException(400, "Пользователь не найден")

    msgs = await db_get_messages(nick, peer)
    return {
        "messages": msgs,
        "peer": {"nick": peer, "name": target["name"], "avatar": target["avatar"]},
    }


# ============================================================
#                        BLACKLIST
# ============================================================

@app.post("/api/blacklist/add")
async def bl_add(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = await auth(x_token)
    d = parse(body)
    peer = (d.get("nick") or "").strip().lstrip("@")
    if peer == nick:
        raise HTTPException(400, "Некорректный пользователь")
    if not await db_get_user(peer):
        raise HTTPException(400, "Некорректный пользователь")

    await db_add_blacklist(nick, peer)
    await db_del_contact(nick, peer)

    await push_to(nick, {"type": "blacklist_changed"})
    return {"ok": True}


@app.post("/api/blacklist/remove")
async def bl_remove(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = await auth(x_token)
    d = parse(body)
    peer = (d.get("nick") or "").strip().lstrip("@")
    await db_del_blacklist(nick, peer)
    await push_to(nick, {"type": "blacklist_changed"})
    return {"ok": True}


# ============================================================
#                    PNG-ГЕНЕРАТОР АВАТАРА
# ============================================================

def make_png(colors, scale: int = 32) -> bytes:
    GRID = 8
    W = H = GRID * scale
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        gy = y // scale
        row_base = gy * GRID
        for x in range(W):
            gx = x // scale
            c = colors[row_base + gx]
            raw.append(int(c[1:3], 16))
            raw.append(int(c[3:5], 16))
            raw.append(int(c[5:7], 16))

    def chunk(ctype: bytes, data: bytes) -> bytes:
        body = ctype + data
        return struct.pack(">I", len(data)) + body + \
               struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


@app.get("/avatar/{nick}.png")
async def avatar_png(nick: str):
    u = await db_get_user(nick)
    colors = u["avatar"] if u else ["#cfd8dc"] * 64
    png = make_png(colors, scale=32)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=600"})


# ============================================================
#                    OPENGRAPH ПАРСЕР
# ============================================================

_OG_PATTERNS = [
    re.compile(r'<meta[^>]*\bproperty=["\'](og:[a-zA-Z0-9:_-]+)["\'][^>]*\bcontent=["\']([^"\']*)["\']', re.I),
    re.compile(r'<meta[^>]*\bcontent=["\']([^"\']*)["\'][^>]*\bproperty=["\'](og:[a-zA-Z0-9:_-]+)["\']', re.I),
    re.compile(r'<meta[^>]*\bname=["\'](twitter:[a-zA-Z0-9:_-]+)["\'][^>]*\bcontent=["\']([^"\']*)["\']', re.I),
    re.compile(r'<meta[^>]*\bcontent=["\']([^"\']*)["\'][^>]*\bname=["\'](twitter:[a-zA-Z0-9:_-]+)["\']', re.I),
]
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_WS_RE = re.compile(r"\s+")


def is_safe_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").strip()
    if not host:
        return False
    if host.lower() in ("localhost", "localhost.localdomain"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        pass
    return True


def fetch_og(url: str) -> dict:
    if url in OG_CACHE:
        return OG_CACHE[url]
    try:
        req = UrlRequest(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8,ru;q=0.6",
        })
        with urlopen(req, timeout=6) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            if "html" not in ct and "xml" not in ct:
                out = {"error": "not_html"}
                OG_CACHE[url] = out
                return out
            body = r.read(300_000)
        text = body.decode("utf-8", errors="ignore")
    except Exception as e:
        out = {"error": str(e)[:120]}
        OG_CACHE[url] = out
        return out

    data: Dict[str, str] = {}
    for pat in _OG_PATTERNS:
        for m in pat.finditer(text):
            key = m.group(1).lower()
            if key not in data:
                data[key] = m.group(2)

    if "og:title" not in data and "twitter:title" in data:
        data["og:title"] = data["twitter:title"]
    if "og:description" not in data and "twitter:description" in data:
        data["og:description"] = data["twitter:description"]
    if "og:image" not in data and "twitter:image" in data:
        data["og:image"] = data["twitter:image"]

    if "og:title" not in data:
        tm = _TITLE_RE.search(text)
        if tm:
            data["og:title"] = _WS_RE.sub(" ", tm.group(1)).strip()[:200]

    image = (data.get("og:image") or "").strip()
    if image and not image.startswith(("http://", "https://")):
        image = urljoin(url, image)
    if image and not image.startswith(("http://", "https://")):
        image = ""

    out = {
        "url": url,
        "title": html_module.unescape((data.get("og:title") or "").strip())[:200],
        "description": html_module.unescape((data.get("og:description") or "").strip())[:400],
        "image": image[:1500],
        "site_name": html_module.unescape((data.get("og:site_name") or "").strip())[:80],
        "type": (data.get("og:type") or "").strip()[:40],
    }
    OG_CACHE[url] = out
    return out


@app.post("/api/og")
async def og_endpoint(body: EncBody, x_token: Optional[str] = Header(None)):
    await auth(x_token)
    d = parse(body)
    url = (d.get("url") or "").strip()
    if not is_safe_url(url):
        raise HTTPException(400, "Invalid URL")
    return await run_in_threadpool(fetch_og, url)


# ============================================================
#                          FRONTEND
# ============================================================

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover,interactive-widget=resizes-content">
<meta name="theme-color" content="#0a84ff">
<meta name="color-scheme" content="light dark">
__OG_TAGS__
<title>__TITLE__</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{
  margin:0;padding:0;height:100%;width:100%;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,sans-serif;
  overscroll-behavior:none;
  user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;
  overflow:hidden;
}
input,textarea{user-select:text;-webkit-user-select:text}

:root{
  --bg:#f2f2f7;--fg:#111;--card:#fff;--muted:#8a8a8e;--border:#e3e3e8;
  --accent:#0a84ff;--danger:#ff3b30;
  --in-bub:#fff;--in-fg:#111;--out-bub:#0a84ff;--out-fg:#fff;
  --overlay:rgba(0,0,0,.45);--sidebar:#f7f7fa;
}
body.dark{
  --bg:#0d0d0f;--fg:#f2f2f7;--card:#1c1c1e;--muted:#8e8e93;--border:#2c2c2e;
  --accent:#0a84ff;--danger:#ff453a;
  --in-bub:#2c2c2e;--in-fg:#f2f2f7;--out-bub:#0a84ff;--out-fg:#fff;
  --overlay:rgba(0,0,0,.65);--sidebar:#141416;
}
body{background:var(--bg);color:var(--fg);transition:background .2s,color .2s}

#app{
  position:fixed;
  top:var(--app-top,0px);
  left:0;right:0;
  height:var(--app-h,100dvh);
  display:flex;flex-direction:column;
  overflow:hidden;
  background:var(--bg);
  transition:height .18s ease-out;
}
@media (min-width:900px){
  #app{position:fixed;top:0;left:0;right:0;height:100vh;height:100dvh}
}

.screen{
  position:absolute;inset:0;
  display:flex;flex-direction:column;
  opacity:0;visibility:hidden;pointer-events:none;
  transition:opacity .28s ease,visibility .28s ease;
}
.screen.active{opacity:1;visibility:visible;pointer-events:auto}

#screen-loading{align-items:center;justify-content:center;background:var(--bg)}
.spinner{
  width:40px;height:40px;
  border:3px solid var(--border);
  border-top-color:var(--accent);
  border-radius:50%;
  animation:spin .9s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-label{margin-top:14px;color:var(--muted);font-size:14px;letter-spacing:.5px}

#screen-auth{flex-direction:column;align-items:center;overflow-y:auto}
.auth-wrap{padding:28px 22px;display:flex;flex-direction:column;gap:14px;min-height:100%;
  width:100%;max-width:440px}
.logo{font-size:38px;font-weight:800;letter-spacing:-1px;text-align:center;margin:14px 0 4px;
  background:linear-gradient(135deg,#0a84ff,#5856d6);-webkit-background-clip:text;
  background-clip:text;-webkit-text-fill-color:transparent}
.tabs{display:flex;background:var(--card);border-radius:12px;padding:4px;gap:4px;border:1px solid var(--border)}
.tab{flex:1;padding:10px;border:0;background:transparent;color:var(--fg);border-radius:9px;
  font-size:15px;font-weight:600;cursor:pointer;transition:.2s}
.tab.active{background:var(--accent);color:#fff}
.tabpane{display:flex;flex-direction:column;gap:10px;animation:fadeIn .25s ease}
.tabpane.hidden{display:none}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

input{
  width:100%;padding:13px 14px;border-radius:12px;border:1px solid var(--border);
  background:var(--card);color:var(--fg);font-size:16px;outline:none;
  transition:border-color .15s,box-shadow .15s;
}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(10,132,255,.15)}
.btn{
  padding:13px 16px;border-radius:12px;border:1px solid var(--border);
  background:var(--card);color:var(--fg);font-size:15px;font-weight:600;cursor:pointer;
  transition:transform .1s,background .15s,border-color .15s,color .15s,filter .15s;
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
}
.btn:active{transform:scale(.97)}
.btn.primary{background:var(--accent);color:#fff;border-color:transparent}
.btn.primary:hover{filter:brightness(1.06)}
.btn.danger{background:var(--danger);color:#fff;border-color:transparent}
.btn.full{width:100%}
.btn.small{padding:8px 12px;font-size:13px}
.btn.icon-only{padding:0;width:46px;height:46px;flex-shrink:0}
.row{display:flex;gap:8px}
.row input{flex:1}
.err{color:var(--danger);font-size:14px;text-align:center;min-height:18px}
.muted{color:var(--muted);font-size:13px}
.big-name{font-size:20px;font-weight:700;margin-top:6px}
.center{text-align:center;align-items:center}

.icon{width:22px;height:22px;display:block;flex-shrink:0;color:currentColor}
.icon-sm{width:18px;height:18px}
#svg-sprite{position:absolute;width:0;height:0;overflow:hidden}

.ava-editor{display:flex;flex-direction:column;gap:10px;align-items:center;background:var(--card);
  padding:14px;border-radius:16px;border:1px solid var(--border)}
.ava-editor canvas{width:220px;height:220px;border-radius:12px;background:#fff;
  touch-action:none;cursor:crosshair;image-rendering:pixelated}
.palette{display:grid;grid-template-columns:repeat(8,1fr);gap:6px;width:100%}
.swatch{width:100%;aspect-ratio:1;border-radius:8px;border:2px solid transparent;
  cursor:pointer;transition:.15s}
.swatch.active{border-color:var(--accent);transform:scale(1.12)}
.ava-tools{display:flex;gap:8px;width:100%}
.ava-tools .btn{flex:1}

#screen-app.active{flex-direction:row}
.sidebar{display:flex;flex-direction:column;overflow:hidden;background:var(--sidebar);
  border-right:1px solid var(--border)}
.chat-pane{display:flex;flex-direction:column;overflow:hidden;background:var(--bg);flex:1;min-width:0}

@media (max-width: 899px){
  #screen-app.active{flex-direction:row}
  .sidebar{flex:1;border-right:0}
  .chat-pane{display:none;flex:1}
  #screen-app.chat-open .sidebar{display:none}
  #screen-app.chat-open .chat-pane{display:flex}
  .back-mobile{display:flex !important}
}
@media (min-width: 900px){
  .sidebar{flex:0 0 340px;width:340px}
  .back-mobile{display:none !important}
}

.topbar{
  display:flex;align-items:center;gap:8px;padding:10px 12px;
  background:var(--card);border-bottom:1px solid var(--border);
  padding-top:max(10px,env(safe-area-inset-top));
  flex-shrink:0;
}
.ava-small{width:40px;height:40px;border-radius:50%;flex-shrink:0;background:#ddd;image-rendering:pixelated}
.me-info{flex:1;min-width:0;overflow:hidden}
.me-name{font-size:15px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.me-nick{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.icon-btn{
  width:40px;height:40px;border-radius:50%;border:0;background:transparent;color:var(--fg);
  cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;
  transition:background .15s,transform .1s;padding:0;
}
.icon-btn:active{background:var(--border);transform:scale(.94)}
.icon-btn:hover{background:var(--border)}
.icon-btn.danger{color:var(--danger)}
.icon-btn.accent{color:var(--accent)}

.list{flex:1;overflow-y:auto;padding:8px;background:var(--sidebar)}
.contact{
  display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:12px;
  background:transparent;margin-bottom:2px;cursor:pointer;
  transition:background .18s ease,border-color .18s ease,transform .12s ease;
  border:1px solid transparent;position:relative;
  will-change:transform,opacity;
}
.contact:hover{background:var(--card);border-color:var(--border)}
.contact:active{transform:scale(.985)}
.contact.selected{background:var(--card);border-color:var(--border)}
.contact canvas.ava-small{transition:transform .2s ease}
.contact.selected canvas.ava-small{transform:scale(1.04)}
.contact .badge{
  min-width:22px;height:22px;border-radius:11px;background:var(--accent);color:#fff;
  font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center;
  padding:0 6px;flex-shrink:0;
  will-change:transform,opacity;
}
.empty{text-align:center;color:var(--muted);padding:60px 20px;font-size:15px;line-height:1.5;
  animation:fadeIn .25s ease}

.empty-pane{
  display:none;flex:1;flex-direction:column;align-items:center;justify-content:center;
  color:var(--muted);gap:14px;font-size:15px;padding:30px;text-align:center;
}
.empty-pane .empty-icon{width:72px;height:72px;opacity:.25}
.empty-pane.hidden{display:none !important}
@media (min-width: 900px){ .empty-pane{display:flex} }

.chat-content{display:flex;flex-direction:column;flex:1;overflow:hidden;min-width:0;animation:fadeIn .25s ease}
.chat-content.hidden{display:none}

.messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:6px;
  scroll-behavior:smooth}
.bubble{
  max-width:78%;padding:9px 13px;border-radius:18px;font-size:15px;line-height:1.35;
  word-wrap:break-word;white-space:pre-wrap;
  animation:bubbleIn .22s cubic-bezier(.2,.8,.3,1);
}
@keyframes bubbleIn{from{opacity:0;transform:translateY(6px) scale(.98)}to{opacity:1;transform:none}}
.bubble.out{align-self:flex-end;background:var(--out-bub);color:var(--out-fg);border-bottom-right-radius:6px}
.bubble.in{align-self:flex-start;background:var(--in-bub);color:var(--in-fg);
  border-bottom-left-radius:6px;border:1px solid var(--border)}
.bubble .msg-link{color:inherit;text-decoration:underline;text-decoration-color:rgba(255,255,255,.5);
  cursor:pointer}
.bubble.in .msg-link{text-decoration-color:rgba(0,0,0,.25)}

.og-card{
  display:block;margin-top:8px;padding:0;
  background:rgba(0,0,0,.06);
  border-radius:12px;overflow:hidden;
  text-decoration:none;color:inherit;cursor:pointer;
  border:1px solid rgba(0,0,0,.08);
  animation:fadeIn .25s ease;
  max-width:320px;
}
.bubble.out .og-card{background:rgba(255,255,255,.14);border-color:rgba(255,255,255,.18)}
.og-card img{display:block;width:100%;height:auto;max-height:180px;object-fit:cover;background:rgba(0,0,0,.06)}
.og-body{padding:10px 12px}
.og-site{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;opacity:.7;margin-bottom:3px}
.og-title{font-size:14px;font-weight:700;line-height:1.3;margin-bottom:3px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.og-desc{font-size:12px;line-height:1.35;opacity:.8;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}

.composer{
  display:flex;gap:8px;padding:10px 12px;padding-bottom:max(10px,env(safe-area-inset-bottom));
  background:var(--card);border-top:1px solid var(--border);align-items:center;
}
.composer input{border-radius:20px}
.composer .btn{border-radius:50%;width:46px;height:46px;padding:0;flex-shrink:0}

.overlay{
  position:absolute;inset:0;background:var(--overlay);display:flex;align-items:flex-end;
  justify-content:center;z-index:50;
  opacity:1;visibility:visible;
  transition:opacity .22s ease,visibility .22s ease;
}
.overlay.hidden{opacity:0;visibility:hidden;pointer-events:none}
.modal{
  background:var(--card);width:100%;max-height:88%;border-radius:22px 22px 0 0;
  display:flex;flex-direction:column;padding-bottom:env(safe-area-inset-bottom);
  transform:translateY(0);
  transition:transform .28s cubic-bezier(.2,.8,.3,1);
}
.overlay.hidden .modal{transform:translateY(30px)}
@media(min-width:700px){
  .overlay{align-items:center}
  .modal{max-width:440px;border-radius:20px;max-height:80%}
  .overlay.hidden .modal{transform:translateY(20px) scale(.98)}
}
.modal-head{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;
  border-bottom:1px solid var(--border);font-size:17px;font-weight:700}
.modal-body{padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:14px}
.setting{display:flex;align-items:center;justify-content:space-between;gap:10px}
.setting-col{display:flex;flex-direction:column;gap:8px}
.setting-title{font-size:14px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.seg{display:flex;background:var(--bg);border-radius:10px;padding:3px;gap:3px;border:1px solid var(--border)}
.seg-btn{padding:7px 14px;border:0;background:transparent;color:var(--fg);border-radius:8px;
  cursor:pointer;font-size:14px;font-weight:600;transition:.15s}
.seg-btn.active{background:var(--accent);color:#fff}
.search-card{display:flex;flex-direction:column;align-items:center;gap:8px;padding:16px;
  background:var(--bg);border-radius:16px;border:1px solid var(--border);
  animation:fadeIn .25s ease}
.ava-mid{width:96px;height:96px;border-radius:50%;image-rendering:pixelated;background:#ddd}
#infoAva{width:140px;height:140px;border-radius:50%;image-rendering:pixelated;background:#ddd;margin:0 auto}
.bl-item{display:flex;align-items:center;gap:10px;padding:8px;background:var(--bg);
  border-radius:12px;margin-bottom:6px;animation:fadeIn .2s ease}
.bl-item canvas{width:32px;height:32px;border-radius:50%;image-rendering:pixelated;flex-shrink:0}
.bl-item .me-info{flex:1}
.bl-item button{width:32px;height:32px;border-radius:50%;border:0;background:var(--danger);
  color:#fff;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;padding:0}
.bl-item button .icon{width:16px;height:16px}

.toast{
  position:fixed;bottom:40px;left:50%;
  transform:translateX(-50%) translateY(20px);
  background:rgba(0,0,0,.88);color:#fff;
  padding:10px 18px;border-radius:20px;font-size:14px;font-weight:500;
  opacity:0;transition:opacity .25s,transform .25s;
  z-index:9999;pointer-events:none;
  box-shadow:0 8px 24px rgba(0,0,0,.3);
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
</style>
</head>
<body>

<svg id="svg-sprite" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <symbol id="i-gear" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="3"/>
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
  </symbol>
  <symbol id="i-arrow-left" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <line x1="19" y1="12" x2="5" y2="12"/>
    <polyline points="12 19 5 12 12 5"/>
  </symbol>
  <symbol id="i-info" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="10"/>
    <line x1="12" y1="16" x2="12" y2="12"/>
    <line x1="12" y1="8" x2="12.01" y2="8"/>
  </symbol>
  <symbol id="i-x" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <line x1="18" y1="6" x2="6" y2="18"/>
    <line x1="6" y1="6" x2="18" y2="18"/>
  </symbol>
  <symbol id="i-send" viewBox="0 0 24 24" fill="currentColor" stroke="none">
    <path d="M3.4 20.4l17.45-7.48a1 1 0 0 0 0-1.84L3.4 3.6a.99.99 0 0 0-1.39.91L2 9.12c0 .5.37.93.87.99L17 12 2.87 13.88c-.5.07-.87.5-.87 1l.01 4.61c0 .71.73 1.2 1.39.91z"/>
  </symbol>
  <symbol id="i-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
    <line x1="12" y1="5" x2="12" y2="19"/>
    <line x1="5" y1="12" x2="19" y2="12"/>
  </symbol>
  <symbol id="i-search" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="11" cy="11" r="7"/>
    <line x1="21" y1="21" x2="16.65" y2="16.65"/>
  </symbol>
  <symbol id="i-trash" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <polyline points="3 6 5 6 21 6"/>
    <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
    <path d="M10 11v6M14 11v6"/>
    <path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/>
  </symbol>
  <symbol id="i-chat" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
    <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
  </symbol>
  <symbol id="i-user" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
    <circle cx="12" cy="7" r="4"/>
  </symbol>
  <symbol id="i-link" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
    <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
  </symbol>
</svg>

<div id="app">

  <div class="screen active" id="screen-loading">
    <div class="spinner"></div>
    <div class="loading-label">Direct</div>
  </div>

  <div class="screen" id="screen-auth">
    <div class="auth-wrap">
      <h1 class="logo">Direct</h1>
      <div class="tabs">
        <button class="tab active" data-tab="login" data-i18n="login">Вход</button>
        <button class="tab" data-tab="reg" data-i18n="register">Регистрация</button>
      </div>

      <div id="tab-login" class="tabpane">
        <input id="li-nick" data-i18n-ph="ph_nick" placeholder="Ник" autocomplete="username">
        <input id="li-pass" type="password" data-i18n-ph="ph_pass" placeholder="Пароль" autocomplete="current-password">
        <button class="btn primary" onclick="doLogin()" data-i18n="login">Войти</button>
      </div>

      <div id="tab-reg" class="tabpane hidden">
        <div class="ava-editor">
          <canvas id="avaCanvas" width="256" height="256"></canvas>
          <div class="palette" id="palette"></div>
          <div class="ava-tools">
            <button class="btn small" onclick="regEditor.clear()" data-i18n="clear">Очистить</button>
            <button class="btn small" onclick="regEditor.random()" data-i18n="random">Случайно</button>
            <button class="btn small" onclick="regEditor.fill()" data-i18n="fill">Залить</button>
          </div>
        </div>
        <input id="rg-name" data-i18n-ph="ph_name" placeholder="Имя">
        <input id="rg-nick" data-i18n-ph="ph_nick" placeholder="Ник">
        <input id="rg-pass" type="password" data-i18n-ph="ph_pass" placeholder="Пароль">
        <input id="rg-pass2" type="password" data-i18n-ph="ph_pass2" placeholder="Повтор пароля">
        <button class="btn primary" onclick="doRegister()" data-i18n="create">Создать аккаунт</button>
      </div>

      <div id="auth-err" class="err"></div>
    </div>
  </div>

  <div class="screen" id="screen-app">

    <aside class="sidebar">
      <header class="topbar">
        <canvas class="ava-small" id="meAva" width="40" height="40"></canvas>
        <div class="me-info">
          <div class="me-name" id="meName">—</div>
          <div class="me-nick" id="meNick">@—</div>
        </div>
        <button class="icon-btn accent" onclick="openSearch()" aria-label="New chat" title="Найти друга">
          <svg class="icon"><use href="#i-plus"/></svg>
        </button>
        <button class="icon-btn" onclick="openSettings()" aria-label="Settings" title="Настройки">
          <svg class="icon"><use href="#i-gear"/></svg>
        </button>
      </header>

      <div class="list" id="contactsList"></div>
    </aside>

    <section class="chat-pane" id="chatPane">

      <div class="empty-pane" id="emptyPane">
        <svg class="empty-icon"><use href="#i-chat"/></svg>
        <div data-i18n="select_chat">Выберите чат слева</div>
      </div>

      <div class="chat-content hidden" id="chatContent">
        <header class="topbar">
          <button class="icon-btn back-mobile" onclick="closeChat()" aria-label="Back">
            <svg class="icon"><use href="#i-arrow-left"/></svg>
          </button>
          <canvas class="ava-small" id="peerAva" width="40" height="40"></canvas>
          <div class="me-info">
            <div class="me-name" id="peerName">—</div>
            <div class="me-nick" id="peerNick">@—</div>
          </div>
          <button class="icon-btn" onclick="openInfo()" aria-label="Info">
            <svg class="icon"><use href="#i-info"/></svg>
          </button>
          <button class="icon-btn danger" onclick="endChat()" aria-label="End">
            <svg class="icon"><use href="#i-x"/></svg>
          </button>
        </header>
        <div class="messages" id="messages"></div>
        <form class="composer" onsubmit="sendMsg(event)">
          <input id="msgInput" data-i18n-ph="ph_msg" placeholder="Сообщение..." autocomplete="off" enterkeyhint="send">
          <button class="btn primary" type="submit" aria-label="Send">
            <svg class="icon"><use href="#i-send"/></svg>
          </button>
        </form>
      </div>
    </section>
  </div>

  <div class="overlay hidden" id="modal-search" onclick="backdropClose(event,'modal-search')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head">
        <span data-i18n="find_user">Найти пользователя</span>
        <button class="icon-btn" onclick="closeModal('modal-search')" aria-label="Close">
          <svg class="icon"><use href="#i-x"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="row">
          <input id="searchNick" data-i18n-ph="ph_nick" placeholder="Ник">
          <button class="btn primary" onclick="doSearch()">
            <svg class="icon icon-sm"><use href="#i-search"/></svg>
            <span data-i18n="find">Найти</span>
          </button>
        </div>
        <div id="searchResult"></div>
      </div>
    </div>
  </div>

  <div class="overlay hidden" id="modal-settings" onclick="backdropClose(event,'modal-settings')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head">
        <span data-i18n="settings">Настройки</span>
        <button class="icon-btn" onclick="closeModal('modal-settings')" aria-label="Close">
          <svg class="icon"><use href="#i-x"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <button class="btn full" onclick="openEditProfile()">
          <svg class="icon icon-sm"><use href="#i-user"/></svg>
          <span data-i18n="edit_profile">Редактировать профиль</span>
        </button>
        <button class="btn full" onclick="copyMyLink()">
          <svg class="icon icon-sm"><use href="#i-link"/></svg>
          <span data-i18n="copy_my_link">Скопировать ссылку на профиль</span>
        </button>
        <div class="setting">
          <span data-i18n="theme">Тема</span>
          <div class="seg">
            <button class="seg-btn" data-theme="light" onclick="applyTheme('light')" data-i18n="light">Светлая</button>
            <button class="seg-btn" data-theme="dark" onclick="applyTheme('dark')" data-i18n="dark">Тёмная</button>
          </div>
        </div>
        <div class="setting">
          <span data-i18n="lang">Язык</span>
          <div class="seg">
            <button class="seg-btn" data-lang="ru" onclick="applyLang('ru')">RU</button>
            <button class="seg-btn" data-lang="en" onclick="applyLang('en')">EN</button>
          </div>
        </div>
        <div class="setting-col">
          <div class="setting-title" data-i18n="blacklist">Чёрный список</div>
          <div id="blacklistBox"></div>
          <div class="row">
            <input id="blNick" data-i18n-ph="ph_nick" placeholder="Ник">
            <button class="btn icon-only" onclick="blAdd()" aria-label="Add">
              <svg class="icon"><use href="#i-plus"/></svg>
            </button>
          </div>
        </div>
        <button class="btn danger full" onclick="logout()" data-i18n="logout">Выйти</button>
      </div>
    </div>
  </div>

  <div class="overlay hidden" id="modal-edit" onclick="backdropClose(event,'modal-edit')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head">
        <span data-i18n="edit_profile">Редактировать профиль</span>
        <button class="icon-btn" onclick="closeModal('modal-edit')" aria-label="Close">
          <svg class="icon"><use href="#i-x"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="ava-editor">
          <canvas id="editAvaCanvas" width="256" height="256"></canvas>
          <div class="palette" id="editPalette"></div>
          <div class="ava-tools">
            <button class="btn small" onclick="editEditor.clear()" data-i18n="clear">Очистить</button>
            <button class="btn small" onclick="editEditor.random()" data-i18n="random">Случайно</button>
            <button class="btn small" onclick="editEditor.fill()" data-i18n="fill">Залить</button>
          </div>
        </div>
        <input id="editName" data-i18n-ph="ph_name" placeholder="Имя">
        <div class="muted" style="text-align:center">@<span id="editNick"></span></div>
        <button class="btn primary full" onclick="saveProfile()" data-i18n="save">Сохранить</button>
      </div>
    </div>
  </div>

  <div class="overlay hidden" id="modal-info" onclick="backdropClose(event,'modal-info')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head">
        <span data-i18n="about_user">О человеке</span>
        <button class="icon-btn" onclick="closeModal('modal-info')" aria-label="Close">
          <svg class="icon"><use href="#i-x"/></svg>
        </button>
      </div>
      <div class="modal-body center">
        <canvas id="infoAva" width="160" height="160"></canvas>
        <div id="infoName" class="big-name"></div>
        <div id="infoNick" class="muted"></div>
        <button class="btn danger full" onclick="endChat()" data-i18n="end_chat">Завершить чат</button>
      </div>
    </div>
  </div>

</div>

<script>
document.addEventListener('contextmenu', e => e.preventDefault());
document.addEventListener('selectstart', e => {
  if (e.target.closest('input,textarea')) return;
  e.preventDefault();
});

/* ============================================================
                        ШИФРОВАНИЕ
   ============================================================ */
const XK = new TextEncoder().encode("DirectSecret2024");
function xorBytes(bytes){
  const out = new Uint8Array(bytes.length);
  for (let i=0;i<bytes.length;i++) out[i] = bytes[i] ^ XK[i % XK.length];
  return out;
}
function encStr(str){
  const b = new TextEncoder().encode(str);
  const x = xorBytes(b);
  let bin = '';
  for (let i=0;i<x.length;i++) bin += String.fromCharCode(x[i]);
  return btoa(bin);
}

/* ============================================================
                          I18N
   ============================================================ */
const I18N = {
  ru: {
    login:"Вход", register:"Регистрация", create:"Создать аккаунт",
    ph_nick:"Ник", ph_pass:"Пароль", ph_pass2:"Повтор пароля", ph_name:"Имя",
    ph_msg:"Сообщение...",
    clear:"Очистить", random:"Случайно", fill:"Залить",
    settings:"Настройки", theme:"Тема", lang:"Язык", light:"Светлая", dark:"Тёмная",
    blacklist:"Чёрный список", add:"Добавить", logout:"Выйти",
    find_user:"Найти пользователя", find:"Найти",
    start_chat:"Начать общение", about_user:"О человеке", end_chat:"Завершить чат",
    no_contacts:"Пока нет контактов.\nНажмите + чтобы найти друзей.",
    no_bl:"Список пуст",
    confirm_logout:"Выйти из аккаунта?",
    remove:"Убрать",
    new_chat:"Новый чат", select_chat:"Выберите чат слева",
    edit_profile:"Редактировать профиль", save:"Сохранить",
    copy_my_link:"Скопировать ссылку на профиль",
    link_copied:"Ссылка скопирована",
    profile_updated:"Профиль обновлён",
    confirm_end_chat:"Завершить чат?\nВся переписка и контакт будут удалены.",
    chat_ended:"Чат завершён"
  },
  en: {
    login:"Sign in", register:"Sign up", create:"Create account",
    ph_nick:"Nickname", ph_pass:"Password", ph_pass2:"Repeat password", ph_name:"Name",
    ph_msg:"Message...",
    clear:"Clear", random:"Random", fill:"Fill",
    settings:"Settings", theme:"Theme", lang:"Language", light:"Light", dark:"Dark",
    blacklist:"Blacklist", add:"Add", logout:"Log out",
    find_user:"Find user", find:"Find",
    start_chat:"Start chat", about_user:"About user", end_chat:"End chat",
    no_contacts:"No contacts yet.\nTap + to find friends.",
    no_bl:"List is empty",
    confirm_logout:"Log out?",
    remove:"Remove",
    new_chat:"New chat", select_chat:"Select a chat on the left",
    edit_profile:"Edit profile", save:"Save",
    copy_my_link:"Copy profile link",
    link_copied:"Link copied",
    profile_updated:"Profile updated",
    confirm_end_chat:"End chat?\nAll messages and the contact will be deleted.",
    chat_ended:"Chat ended"
  }
};
let LANG = localStorage.getItem('direct_lang') || 'ru';
function t(k){ return (I18N[LANG] && I18N[LANG][k]) || k; }

/* ============================================================
                        STATE
   ============================================================ */
let token = localStorage.getItem('direct_token') || null;
let me = null;
let contacts = [];
let blacklist = [];
let currentPeer = null;
let currentPeerData = null;
let ws = null;
let unread = {};
let renderedIds = new Set();
const ogClientCache = {};
const profileMatch = location.pathname.match(/^\/@([^/]+)$/);
let pendingTarget = profileMatch ? decodeURIComponent(profileMatch[1]) : null;

const contactEls = new Map();

/* ============================================================
                       API
   ============================================================ */
async function api(path, payload, method='POST'){
  const headers = {'Content-Type':'application/json'};
  if (token) headers['X-Token'] = token;
  const opts = {method, headers};
  if (method === 'POST'){
    opts.body = JSON.stringify({ data: encStr(JSON.stringify(payload || {})) });
  }
  const res = await fetch(path, opts);
  if (!res.ok){
    let msg = 'Ошибка';
    try { const j = await res.json(); msg = j.detail || msg; } catch(e){}
    throw new Error(msg);
  }
  return res.json();
}

/* ============================================================
                     WebSocket
   ============================================================ */
function connectWS(){
  if (!token || ws) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try {
    ws = new WebSocket(`${proto}//${location.host}/ws?token=${encodeURIComponent(token)}`);
  } catch(e){ ws = null; return; }
  ws.onmessage = ev => { try { handleWsEvent(JSON.parse(ev.data)); } catch(_){} };
  ws.onclose = () => { ws = null; if (token) setTimeout(connectWS, 1500); };
  ws.onerror = () => { try { ws && ws.close(); } catch(_){} };
  clearInterval(window.__hb);
  window.__hb = setInterval(() => {
    try { if (ws && ws.readyState === 1) ws.send('ping'); } catch(_){}
  }, 25000);
}

function disconnectWS(){
  clearInterval(window.__hb);
  if (ws){ try { ws.close(); } catch(_){} ws = null; }
}

function handleWsEvent(ev){
  if (!ev || !ev.type) return;
  if (ev.type === 'message'){
    const m = ev.msg;
    const isMine = m.from === me?.nick;
    if (currentPeer && (m.from === currentPeer || m.to === currentPeer)){
      appendMessage(m);
    }
    if (!isMine){
      const isCurrentChat = currentPeer === m.from;
      const isFocused = document.hasFocus();
      if (!isCurrentChat || !isFocused){
        playBeep();
        showDesktopNotification(m.from, m.text);
      }
      if (!isCurrentChat) addUnread(m.from);
      if (!contacts.find(c => c.nick === m.from)) refreshMe().catch(()=>{});
    }
  } else if (ev.type === 'contact_added'){
    refreshMe().catch(()=>{});
  } else if (ev.type === 'contact_updated'){
    const u = ev.user;
    const c = contacts.find(x => x.nick === u.nick);
    if (c){
      const changed = (c.name !== u.name) ||
                      (JSON.stringify(c.avatar) !== JSON.stringify(u.avatar));
      c.name = u.name; c.avatar = u.avatar;
      if (changed){
        const el = contactEls.get(u.nick);
        if (el) updateContactEl(el, c);
      }
    }
    if (currentPeer === u.nick){
      currentPeerData = u;
      document.getElementById('peerName').textContent = u.name;
      const pAva = document.getElementById('peerAva');
      if (pAva._avaKey !== JSON.stringify(u.avatar)){
        paintAva(pAva, u.avatar);
        pAva._avaKey = JSON.stringify(u.avatar);
      }
    }
  } else if (ev.type === 'contact_removed'){
    removeContactLocal(ev.nick);
  } else if (ev.type === 'blacklist_changed'){
    refreshMe().catch(()=>{});
  }
}

/* ============================================================
                     ЗВУК / УВЕДОМЛЕНИЯ
   ============================================================ */
let audioCtx = null;
function playBeep(){
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const now = audioCtx.currentTime;
    const tone = (freq, start, dur) => {
      const o = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      o.connect(g); g.connect(audioCtx.destination);
      o.type = 'sine'; o.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, now + start);
      g.gain.exponentialRampToValueAtTime(0.14, now + start + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, now + start + dur);
      o.start(now + start); o.stop(now + start + dur + 0.02);
    };
    tone(880, 0, 0.12); tone(1320, 0.09, 0.14);
  } catch(_){}
}

function requestNotifPermission(){
  if (!('Notification' in window)) return;
  if (Notification.permission === 'default'){
    try { Notification.requestPermission(); } catch(_){}
  }
}

function showDesktopNotification(fromNick, text){
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  if (document.hasFocus()) return;
  const u = contacts.find(c => c.nick === fromNick);
  const title = u ? u.name : fromNick;
  try {
    const n = new Notification(title, {
      body: text.length > 80 ? text.slice(0, 80) + '…' : text,
      tag: 'direct-' + fromNick, silent: true
    });
    n.onclick = () => { window.focus(); openChat(fromNick); };
  } catch(_){}
}

function addUnread(nick){
  unread[nick] = (unread[nick] || 0) + 1;
  const c = contacts.find(x => x.nick === nick);
  const el = contactEls.get(nick);
  if (c && el) updateContactEl(el, c);
  updateTitle();
}

function clearUnread(nick){
  if (!unread[nick]) return;
  delete unread[nick];
  const c = contacts.find(x => x.nick === nick);
  const el = contactEls.get(nick);
  if (c && el) updateContactEl(el, c);
  updateTitle();
}

function updateTitle(){
  const total = Object.values(unread).reduce((a,b)=>a+b, 0);
  document.title = total > 0 ? `(${total}) Direct` : 'Direct';
}

/* ============================================================
                       TOAST
   ============================================================ */
let toastEl = null;
function toast(msg){
  if (toastEl) toastEl.remove();
  toastEl = document.createElement('div');
  toastEl.className = 'toast';
  toastEl.textContent = msg;
  document.body.appendChild(toastEl);
  requestAnimationFrame(() => toastEl.classList.add('show'));
  setTimeout(() => {
    if (toastEl) {
      toastEl.classList.remove('show');
      setTimeout(() => { if (toastEl) { toastEl.remove(); toastEl = null; } }, 300);
    }
  }, 2000);
}

async function copyText(txt){
  try { await navigator.clipboard.writeText(txt); return true; }
  catch(_){
    try {
      const ta = document.createElement('textarea');
      ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
      return true;
    } catch(_){ return false; }
  }
}

/* ============================================================
                     AVATAR (8x8) — редактор
   ============================================================ */
const PALETTE = [
  '#000000','#ffffff','#8e8e93','#c7c7cc',
  '#ff3b30','#ff9500','#ffcc00','#34c759',
  '#00c7be','#0a84ff','#5856d6','#af52de',
  '#ff2d55','#5c3b1e','#f5c6a5','#b3e5fc'
];
const GRID = 8;

function makeAvatarEditor(canvasEl, paletteEl){
  let data = new Array(64).fill('#ffffff');
  let color = '#000000';
  let drawing = false;

  function render(){
    const ctx = canvasEl.getContext('2d');
    const W = canvasEl.width;
    const cell = W / GRID;
    ctx.clearRect(0, 0, W, W);
    for (let i = 0; i < 64; i++){
      ctx.fillStyle = data[i];
      ctx.fillRect((i % GRID) * cell, Math.floor(i / GRID) * cell, cell, cell);
    }
    ctx.strokeStyle = 'rgba(0,0,0,.14)';
    ctx.lineWidth = 1;
    for (let i = 1; i < GRID; i++){
      ctx.beginPath(); ctx.moveTo(i*cell,0); ctx.lineTo(i*cell,W); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0,i*cell); ctx.lineTo(W,i*cell); ctx.stroke();
    }
  }

  function buildPalette(){
    paletteEl.innerHTML = '';
    PALETTE.forEach((c, i) => {
      const b = document.createElement('div');
      b.className = 'swatch' + (i === 0 ? ' active' : '');
      b.style.background = c;
      b.onclick = () => {
        color = c;
        paletteEl.querySelectorAll('.swatch').forEach(s => s.classList.remove('active'));
        b.classList.add('active');
      };
      paletteEl.appendChild(b);
    });
  }

  function paintAt(cx, cy){
    const r = canvasEl.getBoundingClientRect();
    const x = Math.floor((cx - r.left) / (r.width / GRID));
    const y = Math.floor((cy - r.top) / (r.height / GRID));
    if (x < 0 || x > 7 || y < 0 || y > 7) return;
    data[y*GRID + x] = color;
    render();
  }

  canvasEl.addEventListener('pointerdown', e => {
    e.preventDefault(); drawing = true;
    try { canvasEl.setPointerCapture(e.pointerId); } catch(_){}
    paintAt(e.clientX, e.clientY);
  });
  canvasEl.addEventListener('pointermove', e => { if (drawing) paintAt(e.clientX, e.clientY); });
  canvasEl.addEventListener('pointerup', () => drawing = false);
  canvasEl.addEventListener('pointercancel', () => drawing = false);

  buildPalette(); render();

  return {
    getData: () => data.slice(),
    setData: (d) => { if (Array.isArray(d) && d.length === 64){ data = d.slice(); render(); } },
    clear: () => { data = new Array(64).fill('#ffffff'); render(); },
    fill:  () => { data = new Array(64).fill(color); render(); },
    random: () => {
      const cols = PALETTE.slice(2);
      const pick = () => cols[Math.floor(Math.random() * cols.length)];
      const c1 = pick(), c2 = pick(), c3 = pick();
      const half = [];
      for (let y = 0; y < 8; y++){
        const row = [];
        for (let x = 0; x < 4; x++){
          const r = Math.random();
          row.push(r < 0.4 ? c1 : r < 0.7 ? c2 : r < 0.9 ? c3 : '#ffffff');
        }
        half.push(row);
      }
      data = [];
      for (let y = 0; y < 8; y++){
        for (let x = 0; x < 4; x++) data.push(half[y][x]);
        for (let x = 3; x >= 0; x--) data.push(half[y][x]);
      }
      render();
    }
  };
}

function paintAva(canvas, data){
  const ctx = canvas.getContext('2d');
  const W = canvas.width;
  const cell = W / GRID;
  ctx.clearRect(0, 0, W, W);
  for (let i = 0; i < 64; i++){
    ctx.fillStyle = (data && data[i]) || '#ffffff';
    ctx.fillRect((i % GRID) * cell, Math.floor(i / GRID) * cell, cell, cell);
  }
}

let regEditor, editEditor;

/* ============================================================
                       UI
   ============================================================ */
function showScreen(id){
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}
function showErr(id, msg){
  const el = document.getElementById(id);
  el.textContent = msg;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.textContent = ''; }, 4000);
}
function openModal(id){ document.getElementById(id).classList.remove('hidden'); }
function closeModal(id){ document.getElementById(id).classList.add('hidden'); }
function backdropClose(e, id){ if (e.target.id === id) closeModal(id); }

/* ============================================================
                       THEME / LANG
   ============================================================ */
function applyTheme(th){
  document.body.classList.toggle('dark', th === 'dark');
  localStorage.setItem('direct_theme', th);
  document.querySelectorAll('[data-theme]').forEach(b =>
    b.classList.toggle('active', b.dataset.theme === th));
}
function applyLang(l){
  LANG = l;
  localStorage.setItem('direct_lang', l);
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const k = el.dataset.i18n;
    if (I18N[l][k]) el.textContent = I18N[l][k];
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    const k = el.dataset.i18nPh;
    if (I18N[l][k]) el.placeholder = I18N[l][k];
  });
  document.querySelectorAll('[data-lang]').forEach(b =>
    b.classList.toggle('active', b.dataset.lang === l));
  renderContacts();
  renderBlacklist();
}

/* ============================================================
                       AUTH
   ============================================================ */
document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const isLogin = tab.dataset.tab === 'login';
    document.getElementById('tab-login').classList.toggle('hidden', !isLogin);
    document.getElementById('tab-reg').classList.toggle('hidden', isLogin);
    document.getElementById('auth-err').textContent = '';
  };
});

async function doLogin(){
  const nick = document.getElementById('li-nick').value.trim();
  const password = document.getElementById('li-pass').value;
  if (!nick || !password){ showErr('auth-err', 'Введите ник и пароль'); return; }
  try {
    const r = await api('/api/login', { nick, password });
    token = r.token; me = r.me;
    localStorage.setItem('direct_token', token);
    requestNotifPermission();
    await refreshMe();
    showScreen('screen-app');
    connectWS();
    await handlePendingTarget();
  } catch(e){ showErr('auth-err', e.message); }
}

async function doRegister(){
  const name = document.getElementById('rg-name').value.trim();
  const nick = document.getElementById('rg-nick').value.trim();
  const password = document.getElementById('rg-pass').value;
  const password2 = document.getElementById('rg-pass2').value;
  try {
    const r = await api('/api/register', {
      name, nick, password, password2,
      avatar: regEditor.getData()
    });
    token = r.token; me = r.me;
    localStorage.setItem('direct_token', token);
    requestNotifPermission();
    await refreshMe();
    showScreen('screen-app');
    connectWS();
    await handlePendingTarget();
  } catch(e){ showErr('auth-err', e.message); }
}

async function logout(){
  if (!confirm(t('confirm_logout'))) return;
  try { await api('/api/logout', {}); } catch(_){}
  disconnectWS();
  token = null; me = null; contacts = []; blacklist = []; unread = {};
  for (const el of contactEls.values()) el.remove();
  contactEls.clear();
  document.getElementById('contactsList').innerHTML = '';
  localStorage.removeItem('direct_token');
  document.getElementById('li-nick').value = '';
  document.getElementById('li-pass').value = '';
  closeModal('modal-settings'); closeModal('modal-edit');
  closeChat(true); updateTitle();
  showScreen('screen-auth');
}

async function handlePendingTarget(){
  if (!pendingTarget) return;
  const target = pendingTarget; pendingTarget = null;
  try { history.replaceState({}, '', '/'); } catch(_){}
  if (!me || target === me.nick) return;
  try {
    const r = await api('/api/search', { nick: target });
    await startChat(r.user.nick);
  } catch(e){ toast(e.message || 'Не удалось открыть чат'); }
}

/* ============================================================
                       ME / CONTACTS
   ============================================================ */
async function refreshMe(){
  const r = await api('/api/me', null, 'GET');
  me = r.me; contacts = r.contacts; blacklist = r.blacklist;

  const meAvaEl = document.getElementById('meAva');
  const avaKey = JSON.stringify(me.avatar);
  if (meAvaEl._avaKey !== avaKey){
    paintAva(meAvaEl, me.avatar);
    meAvaEl._avaKey = avaKey;
  }
  const nameEl = document.getElementById('meName');
  if (nameEl.textContent !== me.name) nameEl.textContent = me.name;
  const nickEl = document.getElementById('meNick');
  const nickTxt = '@' + me.nick;
  if (nickEl.textContent !== nickTxt) nickEl.textContent = nickTxt;

  renderContacts();
  renderBlacklist();
}

/* --------- incremental DOM helpers --------- */

function createContactEl(c){
  const el = document.createElement('div');
  el.className = 'contact';
  el.dataset.nick = c.nick;

  const cv = document.createElement('canvas');
  cv.width = 44; cv.height = 44;
  cv.className = 'ava-small';
  paintAva(cv, c.avatar);
  el.appendChild(cv);

  const info = document.createElement('div');
  info.className = 'me-info';
  const nm = document.createElement('div');
  nm.className = 'me-name';
  nm.textContent = c.name;
  const nk = document.createElement('div');
  nk.className = 'me-nick';
  nk.textContent = '@' + c.nick;
  info.appendChild(nm); info.appendChild(nk);
  el.appendChild(info);

  const badge = document.createElement('span');
  badge.className = 'badge';
  badge.style.display = 'none';
  badge.style.opacity = '1';
  el.appendChild(badge);

  el._avaKey = JSON.stringify(c.avatar);
  el._badgeShown = false;

  el.addEventListener('click', () => openChat(el.dataset.nick));

  el.animate(
    [
      {opacity: 0, transform: 'translateY(6px)'},
      {opacity: 1, transform: 'translateY(0)'}
    ],
    {duration: 240, easing: 'cubic-bezier(.2,.8,.3,1)'}
  );

  return el;
}

function updateContactEl(el, c){
  const cv = el.querySelector('canvas');
  const avaKey = JSON.stringify(c.avatar);
  if (el._avaKey !== avaKey){
    paintAva(cv, c.avatar);
    el._avaKey = avaKey;
  }
  const nm = el.querySelector('.me-name');
  if (nm.textContent !== c.name) nm.textContent = c.name;
  const nk = el.querySelector('.me-nick');
  const nkTxt = '@' + c.nick;
  if (nk.textContent !== nkTxt) nk.textContent = nkTxt;

  const badge = el.querySelector('.badge');
  const count = unread[c.nick] || 0;
  const wantShow = count > 0;
  const wantTxt = count > 99 ? '99+' : String(count);

  if (wantShow){
    if (badge.textContent !== wantTxt) badge.textContent = wantTxt;
    if (!el._badgeShown){
      el._badgeShown = true;
      badge.style.display = 'flex';
      if (badge._anim){ try { badge._anim.cancel(); } catch(_){} badge._anim = null; }
      badge._anim = badge.animate(
        [
          {transform: 'scale(.45)', opacity: 0},
          {transform: 'scale(1.18)', opacity: 1, offset: .7},
          {transform: 'scale(1)', opacity: 1}
        ],
        {duration: 280, easing: 'cubic-bezier(.2,.8,.3,1)'}
      );
      badge._anim.onfinish = () => { badge._anim = null; };
    }
  } else {
    if (el._badgeShown){
      el._badgeShown = false;
      if (badge._anim){ try { badge._anim.cancel(); } catch(_){} badge._anim = null; }
      const a = badge.animate(
        [
          {transform: 'scale(1)', opacity: 1},
          {transform: 'scale(.5)', opacity: 0}
        ],
        {duration: 160, easing: 'ease-in', fill: 'forwards'}
      );
      a.onfinish = () => {
        if (!el._badgeShown) badge.style.display = 'none';
        try { a.cancel(); } catch(_){}
      };
      badge._anim = a;
    }
  }

  el.classList.toggle('selected', currentPeer === c.nick);
}

function renderContacts(){
  const box = document.getElementById('contactsList');
  const sorted = [...contacts].sort((a, b) => a.nick.localeCompare(b.nick));

  if (sorted.length === 0){
    for (const el of contactEls.values()) el.remove();
    contactEls.clear();
    let empty = box.querySelector('.empty');
    if (!empty){
      empty = document.createElement('div');
      empty.className = 'empty';
      box.appendChild(empty);
    }
    empty.textContent = t('no_contacts');
    return;
  }
  const empty = box.querySelector('.empty');
  if (empty) empty.remove();

  const wanted = new Set(sorted.map(c => c.nick));
  for (const [nick, el] of [...contactEls]){
    if (!wanted.has(nick)){
      contactEls.delete(nick);
      const a = el.animate(
        [
          {opacity: 1, transform: 'translateX(0)'},
          {opacity: 0, transform: 'translateX(-14px)'}
        ],
        {duration: 200, easing: 'ease-in', fill: 'forwards'}
      );
      a.onfinish = () => el.remove();
    }
  }

  for (const c of sorted){
    let el = contactEls.get(c.nick);
    if (!el){
      el = createContactEl(c);
      contactEls.set(c.nick, el);
      box.appendChild(el);
      updateContactEl(el, c);
    } else {
      updateContactEl(el, c);
    }
  }

  const currentDomOrder = Array.from(box.children)
    .filter(n => n.classList && n.classList.contains('contact'))
    .map(n => n.dataset.nick);
  const wantedOrder = sorted.map(c => c.nick);
  let orderChanged = currentDomOrder.length !== wantedOrder.length;
  if (!orderChanged){
    for (let i = 0; i < wantedOrder.length; i++){
      if (currentDomOrder[i] !== wantedOrder[i]){ orderChanged = true; break; }
    }
  }
  if (orderChanged){
    let prev = null;
    for (const c of sorted){
      const el = contactEls.get(c.nick);
      if (!el) continue;
      if (prev){
        if (el.previousElementSibling !== prev) prev.after(el);
      } else {
        if (box.firstElementChild !== el) box.prepend(el);
      }
      prev = el;
    }
  }
}

function removeContactLocal(nick, animate = true){
  contacts = contacts.filter(c => c.nick !== nick);

  const el = contactEls.get(nick);
  if (el){
    contactEls.delete(nick);
    if (animate){
      const a = el.animate(
        [
          {opacity: 1, transform: 'translateX(0)'},
          {opacity: 0, transform: 'translateX(-16px)'}
        ],
        {duration: 200, easing: 'ease-in', fill: 'forwards'}
      );
      a.onfinish = () => el.remove();
    } else {
      el.remove();
    }
  }

  if (unread[nick]){
    delete unread[nick];
    updateTitle();
  }

  if (currentPeer === nick){
    closeChat(true);
  }

  if (contacts.length === 0) renderContacts();
}

function renderBlacklist(){
  const box = document.getElementById('blacklistBox');
  box.innerHTML = '';
  if (!blacklist.length){
    const e = document.createElement('div');
    e.className = 'muted';
    e.style.padding = '6px 2px';
    e.textContent = t('no_bl');
    box.appendChild(e);
    return;
  }
  blacklist.forEach(u => {
    const el = document.createElement('div');
    el.className = 'bl-item';
    const cv = document.createElement('canvas');
    cv.width = 32; cv.height = 32;
    paintAva(cv, u.avatar);
    el.appendChild(cv);
    const info = document.createElement('div');
    info.className = 'me-info';
    const nm = document.createElement('div');
    nm.className = 'me-name'; nm.textContent = u.name;
    const nk = document.createElement('div');
    nk.className = 'me-nick'; nk.textContent = '@' + u.nick;
    info.appendChild(nm); info.appendChild(nk);
    el.appendChild(info);
    const btn = document.createElement('button');
    btn.title = t('remove');
    btn.setAttribute('aria-label', t('remove'));
    btn.innerHTML = '<svg class="icon"><use href="#i-trash"/></svg>';
    btn.onclick = () => blRemove(u.nick);
    el.appendChild(btn);
    box.appendChild(el);
  });
}

/* ============================================================
                       SEARCH
   ============================================================ */
function openSearch(){
  document.getElementById('searchNick').value = '';
  document.getElementById('searchResult').innerHTML = '';
  openModal('modal-search');
  setTimeout(() => document.getElementById('searchNick').focus(), 250);
}

async function doSearch(){
  const nick = document.getElementById('searchNick').value.trim();
  const box = document.getElementById('searchResult');
  box.innerHTML = '';
  if (!nick) return;
  try {
    const r = await api('/api/search', { nick });
    const u = r.user;
    const card = document.createElement('div');
    card.className = 'search-card';
    const cv = document.createElement('canvas');
    cv.width = 96; cv.height = 96;
    cv.className = 'ava-mid';
    paintAva(cv, u.avatar);
    card.appendChild(cv);
    const nm = document.createElement('div');
    nm.className = 'big-name'; nm.textContent = u.name;
    const nk = document.createElement('div');
    nk.className = 'muted'; nk.textContent = '@' + u.nick;
    card.appendChild(nm); card.appendChild(nk);
    const btn = document.createElement('button');
    btn.className = 'btn primary full';
    btn.textContent = t('start_chat');
    btn.onclick = () => { closeModal('modal-search'); startChat(u.nick); };
    card.appendChild(btn);
    box.appendChild(card);
  } catch(e){
    const er = document.createElement('div');
    er.className = 'err'; er.textContent = e.message;
    box.appendChild(er);
  }
}

/* ============================================================
                       CHAT
   ============================================================ */
async function startChat(peerNick){
  try {
    await api('/api/chat/start', { nick: peerNick });
    await refreshMe();
    openChat(peerNick);
  } catch(e){ toast(e.message); }
}

function resetMessages(){
  document.getElementById('messages').innerHTML = '';
  renderedIds = new Set();
}

function linkifyInto(container, text){
  const re = /(https?:\/\/[^\s<>"']+)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null){
    if (m.index > last) container.appendChild(document.createTextNode(text.slice(last, m.index)));
    const a = document.createElement('a');
    a.className = 'msg-link'; a.href = m[0]; a.textContent = m[0];
    a.onclick = async (e) => {
      e.preventDefault(); e.stopPropagation();
      const ok = await copyText(m[0]);
      toast(ok ? t('link_copied') : 'Copy failed');
    };
    container.appendChild(a);
    last = re.lastIndex;
  }
  if (last < text.length) container.appendChild(document.createTextNode(text.slice(last)));
}

function appendMessage(m, scroll = true){
  if (!m || !m.id) return;
  if (renderedIds.has(m.id)) return;
  renderedIds.add(m.id);

  const box = document.getElementById('messages');
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 160;

  const el = document.createElement('div');
  el.className = 'bubble ' + (m.from === me.nick ? 'out' : 'in');
  linkifyInto(el, m.text);
  box.appendChild(el);

  if (scroll && nearBottom) box.scrollTop = box.scrollHeight;

  const urls = m.text.match(/https?:\/\/[^\s<>"']+/g);
  if (urls && urls[0]) maybeRenderOg(el, urls[0]);
}

async function maybeRenderOg(bubbleEl, url){
  let data = ogClientCache[url];
  if (!data){
    try {
      data = await api('/api/og', { url });
      ogClientCache[url] = data;
    } catch(e){
      ogClientCache[url] = { error: true };
      return;
    }
  }
  if (!data || data.error || !data.title) return;
  if (bubbleEl.querySelector('.og-card')) return;

  const card = document.createElement('a');
  card.className = 'og-card'; card.href = url;
  card.onclick = async (e) => {
    e.preventDefault(); e.stopPropagation();
    const ok = await copyText(url);
    toast(ok ? t('link_copied') : 'Copy failed');
  };

  if (data.image){
    const img = document.createElement('img');
    img.src = data.image; img.loading = 'lazy'; img.alt = '';
    img.referrerPolicy = 'no-referrer';
    img.onerror = () => img.remove();
    card.appendChild(img);
  }

  const body = document.createElement('div');
  body.className = 'og-body';
  if (data.site_name){
    const s = document.createElement('div');
    s.className = 'og-site'; s.textContent = data.site_name;
    body.appendChild(s);
  }
  const ttl = document.createElement('div');
  ttl.className = 'og-title'; ttl.textContent = data.title;
  body.appendChild(ttl);
  if (data.description){
    const d = document.createElement('div');
    d.className = 'og-desc'; d.textContent = data.description;
    body.appendChild(d);
  }
  card.appendChild(body);
  bubbleEl.appendChild(card);

  const box = document.getElementById('messages');
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 500){
    box.scrollTop = box.scrollHeight;
  }
}

async function openChat(peerNick){
  try {
    const r = await api('/api/chat/messages', { peer: peerNick });
    currentPeer = peerNick;
    currentPeerData = r.peer;

    const pAvaEl = document.getElementById('peerAva');
    const pKey = JSON.stringify(r.peer.avatar);
    if (pAvaEl._avaKey !== pKey){
      paintAva(pAvaEl, r.peer.avatar);
      pAvaEl._avaKey = pKey;
    }
    document.getElementById('peerName').textContent = r.peer.name;
    document.getElementById('peerNick').textContent = '@' + r.peer.nick;

    resetMessages();
    r.messages.forEach(m => appendMessage(m, false));
    const box = document.getElementById('messages');
    box.scrollTop = box.scrollHeight;

    document.getElementById('chatContent').classList.remove('hidden');
    document.getElementById('emptyPane').classList.add('hidden');
    document.getElementById('screen-app').classList.add('chat-open');
    clearUnread(peerNick);

    for (const [nick, el] of contactEls){
      el.classList.toggle('selected', nick === peerNick);
    }

    setTimeout(() => document.getElementById('msgInput').focus(), 120);
  } catch(e){ toast(e.message); }
}

async function sendMsg(e){
  e.preventDefault();
  const inp = document.getElementById('msgInput');
  const text = inp.value.trim();
  if (!text || !currentPeer) return;
  inp.value = '';
  try {
    const r = await api('/api/chat/send', { to: currentPeer, text });
    appendMessage(r.msg);
    try { if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume(); } catch(_){}
  } catch(err){
    toast(err.message);
    inp.value = text;
  }
}

function closeChat(silent){
  currentPeer = null;
  currentPeerData = null;
  renderedIds = new Set();
  document.getElementById('messages').innerHTML = '';
  document.getElementById('chatContent').classList.add('hidden');
  document.getElementById('emptyPane').classList.remove('hidden');
  document.getElementById('screen-app').classList.remove('chat-open');
  for (const el of contactEls.values()) el.classList.remove('selected');
  if (!silent) refreshMe().catch(()=>{});
}

async function endChat(){
  if (!currentPeer) return;
  if (!confirm(t('confirm_end_chat'))) return;

  const peer = currentPeer;
  try {
    await api('/api/chat/end', { nick: peer });
  } catch(e){
    toast(e.message);
    return;
  }

  closeModal('modal-info');
  removeContactLocal(peer);
  toast(t('chat_ended'));
}

function openInfo(){
  if (!currentPeerData) return;
  paintAva(document.getElementById('infoAva'), currentPeerData.avatar);
  document.getElementById('infoName').textContent = currentPeerData.name;
  document.getElementById('infoNick').textContent = '@' + currentPeerData.nick;
  openModal('modal-info');
}

/* ============================================================
                       SETTINGS
   ============================================================ */
function openSettings(){ renderBlacklist(); openModal('modal-settings'); }

function openEditProfile(){
  if (!me) return;
  document.getElementById('editName').value = me.name;
  document.getElementById('editNick').textContent = me.nick;
  editEditor.setData(me.avatar);
  closeModal('modal-settings');
  openModal('modal-edit');
}

async function saveProfile(){
  const name = document.getElementById('editName').value.trim();
  if (!name){ toast('Введите имя'); return; }
  try {
    const r = await api('/api/profile/update', { name, avatar: editEditor.getData() });
    me = r.me;
    const meAvaEl = document.getElementById('meAva');
    paintAva(meAvaEl, me.avatar);
    meAvaEl._avaKey = JSON.stringify(me.avatar);
    document.getElementById('meName').textContent = me.name;
    document.getElementById('meNick').textContent = '@' + me.nick;
    closeModal('modal-edit');
    toast(t('profile_updated'));
  } catch(e){ toast(e.message); }
}

async function copyMyLink(){
  if (!me) return;
  const url = location.origin + '/@' + me.nick;
  const ok = await copyText(url);
  toast(ok ? t('link_copied') : 'Copy failed');
}

async function blAdd(){
  const inp = document.getElementById('blNick');
  const nick = inp.value.trim();
  if (!nick) return;
  try { await api('/api/blacklist/add', { nick }); inp.value = ''; await refreshMe(); }
  catch(e){ toast(e.message); }
}

async function blRemove(nick){
  try { await api('/api/blacklist/remove', { nick }); await refreshMe(); }
  catch(e){ toast(e.message); }
}

/* ============================================================
                 KEYBOARD / VIEWPORT
   ============================================================ */
function updateViewport(){
  const vv = window.visualViewport;
  if (!vv){
    document.documentElement.style.setProperty('--app-h', window.innerHeight + 'px');
    document.documentElement.style.setProperty('--app-top', '0px');
    return;
  }
  document.documentElement.style.setProperty('--app-h', vv.height + 'px');
  document.documentElement.style.setProperty('--app-top', vv.offsetTop + 'px');
}
if (window.visualViewport){
  window.visualViewport.addEventListener('resize', updateViewport);
  window.visualViewport.addEventListener('scroll', updateViewport);
}
window.addEventListener('resize', updateViewport);
window.addEventListener('orientationchange', () => setTimeout(updateViewport, 100));

function pinViewportAfterFocus(){
  setTimeout(() => {
    try { window.scrollTo(0, 0); } catch(_){}
    updateViewport();
    const box = document.getElementById('messages');
    if (box) box.scrollTop = box.scrollHeight;
  }, 80);
}

/* ============================================================
                       INIT / BOOT
   ============================================================ */
function delay(ms){ return new Promise(r => setTimeout(r, ms)); }

async function boot(){
  showScreen('screen-loading');
  const t0 = Date.now();
  const MIN = 350;

  if (token){
    try {
      const r = await api('/api/me', null, 'GET');
      me = r.me; contacts = r.contacts; blacklist = r.blacklist;
      const meAvaEl = document.getElementById('meAva');
      paintAva(meAvaEl, me.avatar);
      meAvaEl._avaKey = JSON.stringify(me.avatar);
      document.getElementById('meName').textContent = me.name;
      document.getElementById('meNick').textContent = '@' + me.nick;
      renderContacts(); renderBlacklist();
      const wait = MIN - (Date.now() - t0);
      if (wait > 0) await delay(wait);
      showScreen('screen-app');
      requestNotifPermission();
      connectWS();
      updateViewport();
      await handlePendingTarget();
      return;
    } catch(_){
      token = null;
      localStorage.removeItem('direct_token');
    }
  }

  const wait = MIN - (Date.now() - t0);
  if (wait > 0) await delay(wait);
  showScreen('screen-auth');
}

(function init(){
  applyTheme(localStorage.getItem('direct_theme') || 'light');
  applyLang(LANG);

  regEditor = makeAvatarEditor(
    document.getElementById('avaCanvas'),
    document.getElementById('palette')
  );
  editEditor = makeAvatarEditor(
    document.getElementById('editAvaCanvas'),
    document.getElementById('editPalette')
  );

  regEditor.random();
  updateViewport();

  document.getElementById('searchNick').addEventListener('keydown', e => {
    if (e.key === 'Enter'){ e.preventDefault(); doSearch(); }
  });
  document.getElementById('msgInput').addEventListener('focus', pinViewportAfterFocus);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && token && (!ws || ws.readyState > 1)) connectWS();
  });

  boot();
})();
</script>
</body>
</html>
"""


# ============================================================
#                       HTML-СТРАНИЦЫ
# ============================================================

def render_page(og_tags: str, title: str) -> str:
    return (PAGE_TEMPLATE
            .replace("__OG_TAGS__", og_tags)
            .replace("__TITLE__", html_module.escape(title)))


def _default_og(base: str) -> str:
    return (
        '<meta property="og:type" content="website">\n'
        '<meta property="og:title" content="Direct — простой мессенджер">\n'
        '<meta property="og:description" content="Приватный мессенджер с шифрованием и обменом сообщениями в реальном времени">\n'
        f'<meta property="og:url" content="{base}/">\n'
        '<meta name="twitter:card" content="summary">\n'
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    base = str(request.base_url).rstrip("/")
    return render_page(_default_og(base), "Direct — мессенджер")


@app.get("/@{nick}", response_class=HTMLResponse)
async def profile_page(nick: str, request: Request):
    base = str(request.base_url).rstrip("/")
    safe_nick = html_module.escape(nick)
    u = await db_get_user(nick)
    if u:
        name = html_module.escape(u["name"])
        og = (
            '<meta property="og:type" content="profile">\n'
            f'<meta property="og:title" content="{name} (@{safe_nick}) — Direct">\n'
            f'<meta property="og:description" content="Напишите мне в Direct → @{safe_nick}">\n'
            f'<meta property="og:image" content="{base}/avatar/{safe_nick}.png">\n'
            f'<meta property="og:image:width" content="256">\n'
            f'<meta property="og:image:height" content="256">\n'
            f'<meta property="og:url" content="{base}/@{safe_nick}">\n'
            '<meta name="twitter:card" content="summary">\n'
        )
        return render_page(og, f"{name} (@{nick}) — Direct")
    og = (
        '<meta property="og:type" content="website">\n'
        '<meta property="og:title" content="Direct">\n'
        f'<meta property="og:description" content="Пользователь @{safe_nick} не найден">\n'
        f'<meta property="og:url" content="{base}/@{safe_nick}">\n'
    )
    return render_page(og, "Direct")


# ============================================================
#                        ЗАПУСК
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
