# ============================================================
#  sldchat — FastAPI + Supabase
#  Run:
#    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python main.py
# ============================================================
import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

START_TIME = time.time()

MAX_MESSAGE_LEN = 8000
MAX_USERNAME_LEN = 32
MIN_USERNAME_LEN = 2
MIN_PASSWORD_LEN = 4
MAX_PASSWORD_LEN = 128
PBKDF2_ITERATIONS = 200_000
RATE_LIMIT_WINDOW = 10.0
RATE_LIMIT_MAX = 25
MUTE_SECONDS = 30
TOKEN_TTL_DAYS = 7
MAX_BODY_SIZE = 64 * 1024
GLOBAL_RATE_WINDOW = 60.0
GLOBAL_RATE_MAX = 900
AUTH_RATE_WINDOW = 60.0
AUTH_RATE_MAX = 20
WS_PER_IP_MAX = 6
MESSAGE_FETCH_LIMIT = 200
USERNAME_RE = re.compile(r"^[^\s@:<>\"'&]{2,32}$")
MSG_ID_RE = re.compile(r"^[0-9a-fA-F\-]{8,64}$")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or ""

IP_RATE: Dict[str, List[float]] = {}
WS_PER_IP: Dict[str, int] = {}
MSG_TIMES: Dict[str, List[float]] = {}

def log(msg: str) -> None:
    try: print(f"[sldchat {time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception: pass

def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()

def verify_password(password: str, salt_hex: str, expected: str) -> bool:
    try: salt = bytes.fromhex(salt_hex)
    except ValueError: return False
    return secrets.compare_digest(hash_password(password, salt), expected)

def new_token() -> str: return secrets.token_urlsafe(32)
def new_enc_key() -> str: return secrets.token_hex(32)

def parse_ts(s: str) -> float:
    if not s: return 0.0
    try: return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception: return 0.0

# ---------------- Supabase ----------------
def _sb_headers(prefer: Optional[str] = None) -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer: h["Prefer"] = prefer
    return h

_http_client: Optional[httpx.AsyncClient] = None
async def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=10,
            limits=httpx.Limits(max_keepalive_connections=60, max_connections=120),
        )
    return _http_client

async def sb_get(path: str, params: Optional[dict] = None) -> list:
    c = await _client()
    r = await c.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=_sb_headers(), params=params or {})
    if r.status_code >= 400:
        raise RuntimeError(f"sb_get {path} → {r.status_code} {r.text[:200]}")
    return r.json()

async def sb_post(path: str, data, prefer: str = "return=representation") -> list:
    c = await _client()
    r = await c.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=_sb_headers(prefer), json=data)
    if r.status_code >= 400:
        raise RuntimeError(f"sb_post {path} → {r.status_code} {r.text[:200]}")
    if r.status_code in (200, 201) and r.text:
        try: return r.json()
        except Exception: return []
    return []

async def sb_delete(path: str, params: dict) -> bool:
    c = await _client()
    r = await c.delete(f"{SUPABASE_URL}/rest/v1/{path}", headers=_sb_headers(), params=params)
    return r.status_code < 400

async def get_user_by_token(token: Optional[str]) -> Optional[dict]:
    if not token: return None
    try:
        rows = await sb_get("tokens", {
            "select": "expires_at,users(id,username)",
            "token": f"eq.{token}", "limit": "1",
        })
    except Exception:
        return None
    if not rows: return None
    row = rows[0]
    exp = parse_ts(row.get("expires_at", ""))
    if exp and exp < time.time():
        try: await sb_delete("tokens", {"token": f"eq.{token}"})
        except Exception: pass
        return None
    u = row.get("users")
    if not u: return None
    return {"id": u["id"], "username": u["username"]}

async def auth(request: Request) -> dict:
    u = await get_user_by_token(request.headers.get("x-auth-token"))
    if not u: raise HTTPException(status_code=401, detail="unauthorized")
    return u

# ---------------- IP / rate limit ----------------
def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd: return fwd.split(",")[0].strip()
    cf = request.headers.get("cf-connecting-ip")
    if cf: return cf.strip()
    return request.client.host if request.client else "0.0.0.0"

def get_ws_ip(ws: WebSocket) -> str:
    try:
        fwd = ws.headers.get("x-forwarded-for")
        if fwd: return fwd.split(",")[0].strip()
        cf = ws.headers.get("cf-connecting-ip")
        if cf: return cf.strip()
    except Exception: pass
    return ws.client.host if ws.client else "0.0.0.0"

def rate_check(key: str, window: float, limit: int) -> bool:
    now = time.time()
    lst = IP_RATE.get(key)
    if lst is None: IP_RATE[key] = [now]; return True
    cutoff = now - window; i = 0
    for t in lst:
        if t >= cutoff: break
        i += 1
    if i: del lst[:i]
    if len(lst) >= limit: return False
    lst.append(now); return True

# ---------------- App ----------------
app = FastAPI(title="sldchat", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","POST"], allow_headers=["*"])

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://getbootstrap.com; "
    "img-src 'self' data:; font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'; "
    "upgrade-insecure-requests"
)

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.method in ("POST","PUT","PATCH"):
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > MAX_BODY_SIZE:
                    return JSONResponse({"detail": "payload_too_large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "bad_request"}, status_code=400)
    ip = get_client_ip(request)
    if not rate_check(f"req:{ip}", GLOBAL_RATE_WINDOW, GLOBAL_RATE_MAX):
        return JSONResponse({"detail": "rate_limited"}, status_code=429)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Server"] = "sldchat"
    return response

# ---------------- Models ----------------
class AuthReq(BaseModel):
    username: str
    password: str

class CreateChannelReq(BaseModel):
    name: str

class ConnectChannelReq(BaseModel):
    name: str

class LeaveChannelReq(BaseModel):
    channel_id: str

# ---------------- REST ----------------
@app.post("/api/auth")
async def auth_endpoint(req: AuthReq, request: Request):
    ip = get_client_ip(request)
    if not rate_check(f"auth:{ip}", AUTH_RATE_WINDOW, AUTH_RATE_MAX):
        raise HTTPException(429, "too_many_attempts")

    username = req.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "bad_username")
    if not (MIN_PASSWORD_LEN <= len(req.password) <= MAX_PASSWORD_LEN):
        raise HTTPException(400, "bad_password")

    try:
        rows = await sb_get("users", {
            "select": "id,username,password_hash,salt",
            "username": f"ilike.{username}", "limit": "1",
        })
    except Exception as e:
        log(f"sb_get users error: {e}")
        raise HTTPException(503, "db_error")

    if rows:
        u = rows[0]
        if not verify_password(req.password, u["salt"], u["password_hash"]):
            raise HTTPException(401, "bad_credentials")
        user = {"id": u["id"], "username": u["username"]}
        is_new = False
        log(f"AUTH login {user['username']} ip={ip}")
    else:
        salt = os.urandom(16)
        try:
            created = await sb_post("users", {
                "username": username,
                "password_hash": hash_password(req.password, salt),
                "salt": salt.hex(),
                "show_in_list": True,
            })
        except Exception as e:
            log(f"register insert error: {e}")
            raise HTTPException(409, "user_exists")
        if not created:
            raise HTTPException(503, "db_error")
        row = created[0]
        user = {"id": row["id"], "username": row["username"]}
        is_new = True
        log(f"AUTH register {user['username']} ip={ip}")

    token = new_token()
    exp_iso = (datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)).isoformat()
    try:
        await sb_post("tokens", {"token": token, "user_id": user["id"], "expires_at": exp_iso},
                      prefer="return=minimal")
    except Exception as e:
        log(f"token insert error: {e}")
        raise HTTPException(503, "db_error")

    return {"token": token, "username": user["username"], "is_new": is_new}

@app.post("/api/logout")
async def logout(request: Request):
    token = request.headers.get("x-auth-token")
    if token:
        try: await sb_delete("tokens", {"token": f"eq.{token}"})
        except Exception: pass
    return {"ok": True}

@app.get("/api/uptime")
async def uptime():
    return {"uptime": time.time()-START_TIME, "name": "sldchat"}

@app.get("/api/me")
async def me(request: Request):
    u = await auth(request)
    return {"username": u["username"]}

def _channel_from_row(ch: dict, msgs: list) -> dict:
    out_msgs = []
    for m in msgs:
        u = m.get("users") or {}
        out_msgs.append({
            "id": m["id"],
            "from": u.get("username", "?"),
            "ct": m["ciphertext"],
            "t": parse_ts(m["created_at"]),
        })
    return {"id": ch["id"], "name": ch["name"], "private": bool(ch.get("private", True)),
            "enc_key": ch.get("enc_key"), "messages": out_msgs}

async def _fetch_channel_messages(channel_id: str) -> list:
    rows = await sb_get("messages", {
        "select": "id,ciphertext,created_at,users(username)",
        "channel_id": f"eq.{channel_id}",
        "order": "created_at.desc", "limit": str(MESSAGE_FETCH_LIMIT),
    })
    rows.reverse()
    return rows

@app.get("/api/channels")
async def list_channels(request: Request):
    me = await auth(request)
    try:
        rows = await sb_get("channel_members", {
            "select": "channel_id,channels(id,name,private,enc_key,created_at)",
            "user_id": f"eq.{me['id']}",
        })
    except Exception:
        raise HTTPException(503, "db_error")

    async def load_one(r):
        ch = r.get("channels")
        if not ch: return None
        try: msgs = await _fetch_channel_messages(ch["id"])
        except Exception: msgs = []
        return _channel_from_row(ch, msgs)

    results = await asyncio.gather(*(load_one(r) for r in rows))
    out = [c for c in results if c]
    out.sort(key=lambda c: c["name"].lower())
    return {"channels": out}

@app.post("/api/channels")
async def create_channel(req: CreateChannelReq, request: Request):
    me = await auth(request)
    name = req.name.strip()[:40]
    if not name: raise HTTPException(400, "bad_name")
    try:
        existing = await sb_get("channels", {"select": "id", "name": f"ilike.{name}", "limit": "1"})
    except Exception:
        raise HTTPException(503, "db_error")
    if existing: raise HTTPException(409, "name_taken")
    enc_key = new_enc_key()
    try:
        created = await sb_post("channels", {
            "name": name, "private": True, "owner_id": me["id"], "enc_key": enc_key,
        })
        if not created: raise HTTPException(503, "db_error")
        ch = created[0]
        await sb_post("channel_members", {"channel_id": ch["id"], "user_id": me["id"]},
                      prefer="return=minimal")
    except HTTPException:
        raise
    except Exception as e:
        log(f"create_channel error: {e}")
        raise HTTPException(409, "name_taken")
    return {"id": ch["id"], "name": ch["name"], "private": True, "enc_key": enc_key, "messages": []}

@app.post("/api/channels/connect")
async def connect_channel(req: ConnectChannelReq, request: Request):
    me = await auth(request)
    name = req.name.strip()
    if not name: raise HTTPException(400, "bad_name")
    try:
        rows = await sb_get("channels", {
            "select": "id,name,private,enc_key",
            "name": f"ilike.{name}", "limit": "1",
        })
    except Exception:
        raise HTTPException(503, "db_error")
    if not rows: raise HTTPException(404, "not_found")
    ch = rows[0]
    try:
        await sb_post("channel_members", {"channel_id": ch["id"], "user_id": me["id"]},
                      prefer="resolution=ignore-duplicates,return=minimal")
    except Exception:
        pass
    try:
        msgs = await _fetch_channel_messages(ch["id"])
    except Exception:
        msgs = []
    return _channel_from_row(ch, msgs)

@app.post("/api/channels/leave")
async def leave_channel(req: LeaveChannelReq, request: Request):
    me = await auth(request)
    await sb_delete("channel_members",
                    {"channel_id": f"eq.{req.channel_id}", "user_id": f"eq.{me['id']}"})
    return {"ok": True}

@app.get("/api/channels/{channel_id}/members")
async def channel_members(channel_id: str, request: Request):
    me = await auth(request)
    try:
        mine = await sb_get("channel_members", {
            "select": "user_id",
            "channel_id": f"eq.{channel_id}", "user_id": f"eq.{me['id']}", "limit": "1",
        })
    except Exception:
        raise HTTPException(503, "db_error")
    if not mine: raise HTTPException(403, "not_member")
    try:
        rows = await sb_get("channel_members", {
            "select": "users(username)",
            "channel_id": f"eq.{channel_id}", "order": "joined_at.asc", "limit": "500",
        })
    except Exception:
        raise HTTPException(503, "db_error")
    out = []
    for r in rows:
        u = r.get("users")
        if u: out.append({"username": u["username"]})
    return {"members": out}

# ---------------- WebSocket ----------------
class WSManager:
    def __init__(self):
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self.info: Dict[WebSocket, tuple] = {}

    async def join(self, cid, ws, user):
        self.rooms.setdefault(cid, set()).add(ws)
        self.info[ws] = (user, cid)

    def leave(self, ws):
        e = self.info.pop(ws, None)
        if e:
            _, cid = e
            self.rooms.get(cid, set()).discard(ws)

    def online_users(self, cid) -> List[str]:
        return sorted(set(u["username"] for (u, c) in self.info.values() if c == cid))

    async def broadcast(self, cid, payload):
        sockets = list(self.rooms.get(cid, set()))
        if not sockets: return
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        results = await asyncio.gather(*(ws.send_text(text) for ws in sockets), return_exceptions=True)
        for ws, res in zip(sockets, results):
            if isinstance(res, Exception): self.leave(ws)

manager = WSManager()

async def persist_message(mid: str, channel_id: str, user_id: str, ct: str):
    try:
        await sb_post("messages", {
            "id": mid, "channel_id": channel_id, "user_id": user_id, "ciphertext": ct,
        }, prefer="resolution=ignore-duplicates,return=minimal")
    except Exception as e:
        log(f"persist_message failed: {e}")

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ip = get_ws_ip(ws)
    current = WS_PER_IP.get(ip, 0)
    if current >= WS_PER_IP_MAX:
        try: await ws.close(code=1008)
        except Exception: pass
        return
    WS_PER_IP[ip] = current + 1

    user = None
    channel_id = None
    try:
        init = await ws.receive_json()
        if init.get("type") != "auth":
            await ws.close(); return
        token = init.get("token","")
        channel_id = init.get("channel_id","")
        user = await get_user_by_token(token)
        if not user or not channel_id:
            await ws.send_json({"type":"error","error":"auth"}); await ws.close(); return
        try:
            mine = await sb_get("channel_members", {
                "select": "user_id",
                "channel_id": f"eq.{channel_id}", "user_id": f"eq.{user['id']}", "limit": "1",
            })
        except Exception:
            mine = []
        if not mine:
            await ws.send_json({"type":"error","error":"auth"}); await ws.close(); return

        await manager.join(channel_id, ws, user)
        await manager.broadcast(channel_id, {"type":"presence","users":manager.online_users(channel_id)})

        while True:
            data = await ws.receive_json()
            t = data.get("type")
            if t == "message":
                ct = str(data.get("ciphertext",""))[:MAX_MESSAGE_LEN]
                if not ct: continue
                mid = str(data.get("id","")).strip()
                if not MSG_ID_RE.match(mid):
                    mid = secrets.token_hex(16)
                key = f"{user['id']}|{channel_id}"
                now = time.time()
                times = MSG_TIMES.setdefault(key, [])
                cutoff = now - RATE_LIMIT_WINDOW; i = 0
                for tt in times:
                    if tt >= cutoff: break
                    i += 1
                if i: del times[:i]
                if len(times) >= RATE_LIMIT_MAX:
                    await ws.send_json({"type":"muted","seconds":MUTE_SECONDS}); continue
                times.append(now)

                msg = {"id": mid, "from": user["username"], "ct": ct, "t": now}
                await manager.broadcast(channel_id, {"type":"message","msg":msg})
                asyncio.create_task(persist_message(mid, channel_id, user["id"], ct))
            elif t == "ping":
                try: await ws.send_json({"type":"pong"})
                except Exception: pass

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.leave(ws)
        WS_PER_IP[ip] = max(0, WS_PER_IP.get(ip, 1) - 1)
        if channel_id:
            try:
                await manager.broadcast(channel_id, {"type":"presence","users":manager.online_users(channel_id)})
            except Exception: pass

# ---------------- i18n ----------------
I18N = {
 "en": {
   "login_title":"sldchat","login_subtitle":"Sign in or create an account",
   "field_nick":"Nickname","field_password":"Password","ph_nick":"Your nickname","ph_password":"Your password",
   "btn_login":"Continue","btn_wait":"Please wait...","booting":"Loading...","remember_me":"Remember me",
   "logged_as":"Signed in as","header_no_channels":"No channels",
   "header_no_channels_sub":"Open Channels to create or join","header_msgs":"{n} messages",
   "empty_no_channels":"You have no channels yet.","empty_no_messages":"No messages. Be the first to write!",
   "composer_ph":"Write a message...","composer_no_channel":"No active channel","composer_muted":"Muted: {n}s",
   "modal_create_title":"Create private channel","modal_name":"Name","modal_name_ph":"E.g. Work",
   "btn_cancel":"Cancel","btn_create":"Create","modal_connect_title":"Connect to channel",
   "modal_connect_name":"Channel name","modal_connect_ph":"Enter the exact channel name","btn_connect":"Connect",
   "connect_not_found":"Channel «{name}» not found","title_add":"Create channel","title_connect":"Connect to channel",
   "title_settings":"Settings","theme_toggle":"Toggle theme","lang_toggle":"Change language","uptime_label":"Uptime",
   "online_label":"online","err_bad_credentials":"Wrong password for this nickname",
   "err_bad_username":"Nickname must be 2–32 characters, no spaces","err_bad_password":"Password must be at least 4 characters",
   "err_generic":"Error","err_rate_limited":"Too many requests, try later","settings_title":"Settings",
   "settings_account":"Account","settings_appearance":"Appearance","settings_user":"Signed in as",
   "settings_logout":"Sign out","settings_theme":"Theme","settings_theme_light":"Light","settings_theme_dark":"Dark",
   "settings_lang":"Language","settings_close":"Close","users_you":"(you)","mobile_channels":"Channels",
   "leave_channel":"Leave channel","members_title":"Members","members_online":"online",
   "msg_menu_mention":"Mention","msg_menu_copy":"Copy text","msg_copied":"Copied to clipboard","msg_copy_failed":"Copy failed",
   "ext_link_title":"External link","ext_link_warning":"This link is not affiliated with us. Open at your own risk.",
   "ext_link_continue":"Continue","ext_link_back":"Back to app"
 },
 "ru": {
   "login_title":"sldchat","login_subtitle":"Войдите или создайте аккаунт",
   "field_nick":"Ник","field_password":"Пароль","ph_nick":"Ваш ник","ph_password":"Ваш пароль",
   "btn_login":"Продолжить","btn_wait":"Пожалуйста подождите...","booting":"Загрузка...","remember_me":"Запомнить меня",
   "logged_as":"Вы вошли как","header_no_channels":"Нет каналов",
   "header_no_channels_sub":"Откройте «Каналы», чтобы создать или вступить","header_msgs":"{n} сообщений",
   "empty_no_channels":"У вас пока нет каналов.","empty_no_messages":"Нет сообщений. Напишите первым!",
   "composer_ph":"Написать сообщение...","composer_no_channel":"Нет активного канала","composer_muted":"Мут: {n} с",
   "modal_create_title":"Создать приватный канал","modal_name":"Название","modal_name_ph":"Например, Работа",
   "btn_cancel":"Отмена","btn_create":"Создать","modal_connect_title":"Подключиться к каналу",
   "modal_connect_name":"Название канала","modal_connect_ph":"Введите точное название канала","btn_connect":"Подключиться",
   "connect_not_found":"Канал «{name}» не найден","title_add":"Создать канал","title_connect":"Подключиться к каналу",
   "title_settings":"Настройки","theme_toggle":"Сменить тему","lang_toggle":"Сменить язык","uptime_label":"Аптайм",
   "online_label":"онлайн","err_bad_credentials":"Неверный пароль для этого ника",
   "err_bad_username":"Ник 2–32 символа, без пробелов","err_bad_password":"Пароль минимум 4 символа",
   "err_generic":"Ошибка","err_rate_limited":"Слишком много запросов","settings_title":"Настройки",
   "settings_account":"Аккаунт","settings_appearance":"Оформление","settings_user":"Вы вошли как",
   "settings_logout":"Выйти из аккаунта","settings_theme":"Тема","settings_theme_light":"Светлая","settings_theme_dark":"Тёмная",
   "settings_lang":"Язык","settings_close":"Закрыть","users_you":"(вы)","mobile_channels":"Каналы",
   "leave_channel":"Покинуть канал","members_title":"Участники","members_online":"онлайн",
   "msg_menu_mention":"Упомянуть","msg_menu_copy":"Скопировать текст","msg_copied":"Скопировано","msg_copy_failed":"Не удалось скопировать",
   "ext_link_title":"Внешняя ссылка","ext_link_warning":"Выбранная ссылка не как не связана с нами. Открывайте на свой страх и риск.",
   "ext_link_continue":"Продолжить","ext_link_back":"Вернуться в приложение"
 },
}
for _c in ["es","de","fr","it","pt","nl","pl","uk","cs","sv","el","tr","ja","ko","zh","ar","he","hi"]:
    I18N.setdefault(_c, {})

_F = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="20" height="{hh}" style="border:1px solid rgba(0,0,0,.25)">{body}</svg>'
FLAGS = {
 "en": _F.format(w=60, h=30, hh=10, body='<rect width="60" height="30" fill="#012169"/><path d="M0 0 L60 30 M60 0 L0 30" stroke="#fff" stroke-width="6"/><path d="M0 0 L60 30 M60 0 L0 30" stroke="#C8102E" stroke-width="3"/><path d="M30 0 V30 M0 15 H60" stroke="#fff" stroke-width="10"/><path d="M30 0 V30 M0 15 H60" stroke="#C8102E" stroke-width="6"/>'),
 "ru": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="7" fill="#fff"/><rect y="7" width="30" height="7" fill="#0039A6"/><rect y="14" width="30" height="6" fill="#D52B1E"/>'),
 "es": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#AA151B"/><rect y="5" width="30" height="10" fill="#F1BF00"/>'),
 "de": _F.format(w=30, h=18, hh=12, body='<rect width="30" height="6" fill="#000"/><rect y="6" width="30" height="6" fill="#DD0000"/><rect y="12" width="30" height="6" fill="#FFCE00"/>'),
 "fr": _F.format(w=30, h=20, hh=13, body='<rect width="10" height="20" fill="#002395"/><rect x="10" width="10" height="20" fill="#fff"/><rect x="20" width="10" height="20" fill="#ED2939"/>'),
 "it": _F.format(w=30, h=20, hh=13, body='<rect width="10" height="20" fill="#009246"/><rect x="10" width="10" height="20" fill="#fff"/><rect x="20" width="10" height="20" fill="#CE2B37"/>'),
 "pt": _F.format(w=30, h=20, hh=13, body='<rect width="12" height="20" fill="#006600"/><rect x="12" width="18" height="20" fill="#FF0000"/><circle cx="12" cy="10" r="4" fill="#FFFF00" stroke="#fff" stroke-width="0.7"/><circle cx="12" cy="10" r="2" fill="#FF0000"/>'),
 "nl": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="7" fill="#AE1C28"/><rect y="7" width="30" height="6" fill="#fff"/><rect y="13" width="30" height="7" fill="#21468B"/>'),
 "pl": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="10" fill="#fff"/><rect y="10" width="30" height="10" fill="#DC143C"/>'),
 "uk": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="10" fill="#0057B7"/><rect y="10" width="30" height="10" fill="#FFDD00"/>'),
 "cs": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="10" fill="#fff"/><rect y="10" width="30" height="10" fill="#D7141A"/><polygon points="0,0 15,10 0,20" fill="#11457E"/>'),
 "sv": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#006AA7"/><rect x="9" width="4" height="20" fill="#FECC00"/><rect y="8" width="30" height="4" fill="#FECC00"/>'),
 "el": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#0D5EAF"/><rect y="3" width="30" height="3" fill="#fff"/><rect y="9" width="30" height="3" fill="#fff"/><rect y="15" width="30" height="3" fill="#fff"/><rect width="10" height="10" fill="#0D5EAF"/><rect y="3" width="10" height="2" fill="#fff"/><rect x="4" width="2" height="10" fill="#fff"/>'),
 "tr": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#E30A17"/><circle cx="11" cy="10" r="4.5" fill="#fff"/><circle cx="12.8" cy="10" r="3.6" fill="#E30A17"/><polygon points="18,8.4 18.41,9.43 19.52,9.5 18.67,10.22 18.94,11.29 18,10.7 17.06,11.29 17.33,10.22 16.48,9.5 17.59,9.43" fill="#fff"/>'),
 "ja": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#fff"/><circle cx="15" cy="10" r="6" fill="#BC002D"/>'),
 "ko": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#fff"/><circle cx="15" cy="10" r="4" fill="#CD2E3A"/><path d="M11 10 a4 4 0 0 0 8 0 a4 4 0 0 0 -8 0 z" fill="#0047A0" clip-path="inset(0 0 0 50%)"/>'),
 "zh": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#DE2910"/><polygon points="5,4 6.2,7.5 3,5.4 7,5.4 3.8,7.5" fill="#FFDE00"/>'),
 "ar": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#006C35"/>'),
 "he": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#fff"/><rect y="2" width="30" height="3" fill="#0038B8"/><rect y="15" width="30" height="3" fill="#0038B8"/><polygon points="15,6 18,12 12,12" fill="none" stroke="#0038B8" stroke-width="0.7"/><polygon points="15,14 12,8 18,8" fill="none" stroke="#0038B8" stroke-width="0.7"/>'),
 "hi": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#fff"/><rect width="30" height="7" fill="#FF9933"/><rect y="13" width="30" height="7" fill="#138808"/><circle cx="15" cy="10" r="2.5" fill="none" stroke="#000080" stroke-width="0.7"/>'),
}
LANG_ORDER = ["en","ru","es","de","fr","it","pt","nl","pl","uk","cs","sv","el","tr","ja","ko","zh","ar","he","hi"]

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru" data-state="login">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0088cc">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<meta name="robots" content="noindex, nofollow, noarchive, nosnippet">
<meta name="referrer" content="no-referrer">
<title>sldchat</title>
<script>
(function() {
  try {
    var tok = localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');
    document.documentElement.setAttribute('data-state', tok ? 'boot' : 'login');
  } catch (e) {
    document.documentElement.setAttribute('data-state', 'login');
  }
})();
</script>
<link rel="stylesheet" href="https://getbootstrap.com/1.4.0/assets/css/bootstrap.min.css">
<style>
* { scrollbar-width: none; -ms-overflow-style: none; -webkit-text-size-adjust: 100%; -webkit-tap-highlight-color: transparent; }
*::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }
html { height: 100%; }
body { margin: 0; background: #f5f5f5; font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 13px; color: #333; overflow: hidden; height: var(--app-vh, 100vh);
  -webkit-user-select: none; -moz-user-select: none; -ms-user-select: none; user-select: none;
  -webkit-touch-callout: none; }
input, textarea { -webkit-user-select: text; -moz-user-select: text; -ms-user-select: text; user-select: text; font-family: inherit; }
svg { display: inline-block; vertical-align: middle; }
button, .channel-tab, .icon-btn-tab, .scroll-arrow, .lang-menu-item, .settings-btn,
.settings-tab, .mcp-item, .member-item { touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
@media print { body { display: none !important; } }

.boot-screen { position: fixed; inset: 0; background: #e9eef3;
  background-image: linear-gradient(#f5f8fb, #dfe6ee);
  display: none; align-items: center; justify-content: center;
  flex-direction: column; gap: 18px; z-index: 550; }
.boot-screen .boot-spinner { width: 34px; height: 34px;
  border: 3px solid #b8c4d0; border-top-color: #0088cc;
  border-radius: 50%; animation: spin .8s linear infinite; }
.boot-screen .boot-text { font-size: 13px; color: #4a5a6a; }
@keyframes spin { to { transform: rotate(360deg); } }
html[data-state="boot"]  .login-screen { display: none !important; }
html[data-state="boot"]  .app          { display: none !important; }
html[data-state="boot"]  .boot-screen  { display: flex !important; }
html[data-state="login"] .boot-screen  { display: none !important; }
html[data-state="login"] .app          { display: none !important; }
html[data-state="app"]   .login-screen { display: none !important; }
html[data-state="app"]   .boot-screen  { display: none !important; }
html[data-state="app"]   .app          { display: flex !important; }
.spinner { display: inline-block; width: 14px; height: 14px;
  border: 2px solid rgba(255,255,255,.35); border-top-color: #fff;
  border-radius: 50%; animation: spin .7s linear infinite;
  vertical-align: -2px; margin-right: 6px; }
.btn[disabled] { opacity: .7; cursor: not-allowed; }

.login-screen { position: fixed; top: 0; left: 0; right: 0; height: var(--app-vh, 100vh);
  background: #e9eef3; background-image: linear-gradient(#f5f8fb, #dfe6ee);
  display: flex; align-items: center; justify-content: center;
  z-index: 500; padding: 16px; box-sizing: border-box; overflow-y: auto; }
.login-box { width: 340px; max-width: 100%; background: #fff; border: 1px solid #b8c4d0;
  box-shadow: 0 4px 16px rgba(0,0,0,.15); padding: 20px 20px 14px; text-align: center;
  margin: auto; box-sizing: border-box; }
.login-box h2 { margin: 0 0 4px; font-size: 18px; color: #2b3d51; }
.login-box p { margin: 0 0 16px; color: #7b8a99; font-size: 12px; }
.my-label { display: block !important; text-align: left !important; font-size: 11px !important;
  font-weight: bold !important; color: #667788 !important; margin: 0 0 4px 0 !important;
  padding: 0 !important; line-height: 1.4 !important; text-transform: uppercase !important; letter-spacing: .3px; }
.field-group { margin-bottom: 12px; text-align: left; }
.my-input { display: block !important; width: 100% !important; box-sizing: border-box !important;
  padding: 7px 10px !important; border: 1px solid #b8c4d0 !important; font-size: 13px !important;
  background: #fff !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.08) !important;
  color: #333 !important; outline: none !important; height: auto !important;
  line-height: 1.4 !important; margin: 0 !important; border-radius: 0 !important; }
.my-input:focus { border-color: #0088cc !important; }
.login-box .btn { width: 100%; margin-top: 6px; min-height: 38px; }
.login-error { margin-top: 10px; font-size: 11.5px; color: #a94442; background: #fcebeb;
  border: 1px solid #f5c6c6; padding: 5px 8px; display: none; text-align: left; }
.login-error.show { display: block; }
.uptime-line { margin-top: 12px; padding-top: 10px; border-top: 1px solid #e0e5eb;
  font-size: 11px; color: #7b8a99; display: flex; justify-content: space-between; }
.uptime-line .u-val { font-weight: bold; color: #4a5a6a; font-family: "Courier New", monospace; }
.remember-row { display: flex !important; align-items: flex-start !important; font-size: 12px !important;
  color: #4a5a6a !important; margin: 8px 0 4px 0 !important; cursor: pointer;
  user-select: none; width: 100%; box-sizing: border-box; gap: 7px; }
.remember-row input { flex: 0 0 auto; width: 15px; height: 15px; margin: 1px 0 0 0; padding: 0; }
.remember-row > span { flex: 1 1 auto; min-width: 0; text-align: left; line-height: 1.35; overflow-wrap: break-word; }
.login-settings { margin-top: 10px; padding-top: 10px; border-top: 1px solid #e0e5eb;
  display: flex; gap: 6px; }
.settings-btn { flex: 1; display: inline-flex; align-items: center; justify-content: center;
  height: 26px; padding: 0 8px; border: 1px solid #b8c4d0;
  background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6);
  color: #333; cursor: pointer; font-size: 12px; font-family: inherit;
  text-shadow: 0 1px 0 rgba(255,255,255,.6); border-radius: 0; }
.settings-btn:hover { background: #d9d9d9; color: #000; }
.settings-btn .lang-code { margin-left: 6px; font-size: 11px; font-weight: bold; letter-spacing: .5px; }
.lang-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 9998; display: none; }
.lang-backdrop.open { display: block; }
.lang-menu { position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%);
  background: #fff; border: 1px solid #666; box-shadow: 0 5px 20px rgba(0,0,0,.4);
  padding: 8px; z-index: 9999; display: none;
  grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px;
  max-width: calc(100vw - 20px); max-height: calc(100vh - 20px); overflow-y: auto;
  box-sizing: border-box; width: 560px; border-radius: 0; }
.lang-menu.open { display: grid; }
.lang-menu-item { display: flex; align-items: center; padding: 6px 8px; cursor: pointer;
  font-size: 12px; color: #333; gap: 7px; border: 1px solid #ddd; background: #fafafa;
  min-width: 0; box-sizing: border-box; border-radius: 0; }
.lang-menu-item:hover { background: #eaf4fb; border-color: #8ab4dc; }
.lang-menu-item.active { background: #d6e8f7; color: #004a80; font-weight: bold; border-color: #4a90c2; }
.lang-menu-item .flag-wrap { flex-shrink: 0; display: inline-flex; align-items: center; }
.lang-menu-item .lang-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lang-menu-item .check { flex-shrink: 0; color: #0088cc; font-weight: bold; visibility: hidden; }
.lang-menu-item.active .check { visibility: visible; }

.app { width: 100%; height: var(--app-vh, 100vh); background: #fff;
  flex-direction: column; position: relative; overflow: hidden; }
.tabs-bar { display: flex; align-items: center; padding: 6px 8px;
  background: #f5f5f5; background-image: linear-gradient(#ffffff, #ececec);
  border-bottom: 1px solid #ccc; flex-shrink: 0; gap: 4px; }
.tabs-scroll { display: flex; align-items: center; flex: 1 1 0; min-width: 0;
  overflow-x: auto; overflow-y: hidden; padding-bottom: 1px; scroll-behavior: smooth; }
.scroll-arrow { width: 18px; height: 26px; border: 1px solid #bbb; background: #e6e6e6;
  background-image: linear-gradient(#ffffff, #e6e6e6); color: #555; cursor: pointer;
  padding: 0; display: inline-flex; align-items: center; justify-content: center;
  flex-shrink: 0; border-radius: 0; }
.scroll-arrow:hover { background: #d9d9d9; color: #000; }
.channel-tab { display: inline-flex; align-items: center; padding: 4px 10px; margin-right: 4px;
  border: 1px solid #bbb; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6);
  color: #444; font-size: 12px; white-space: nowrap; cursor: pointer;
  text-shadow: 0 1px 0 rgba(255,255,255,.6); flex-shrink: 0;
  font-family: "Courier New", Courier, monospace; border-radius: 0; }
.channel-tab:last-child { margin-right: 0; }
.channel-tab:hover { background: #d9d9d9; color: #000; }
.channel-tab.active { background: #006dcc; background-image: linear-gradient(#0088cc, #0044cc);
  color: #fff; border-color: #003f81; text-shadow: 0 -1px 0 rgba(0,0,0,.3); }
.channel-tab .lock-ico { margin-right: 4px; opacity: .8; display: inline-flex; align-items: center; }
.channel-tab .close-x { margin-left: 6px; cursor: pointer; opacity: .55;
  display: inline-flex; align-items: center; color: inherit; padding: 2px; }
.channel-tab .close-x:hover { opacity: 1; color: #c00; }
.icon-btn-tab { width: 26px; height: 26px; line-height: 1; border: 1px solid #bbb;
  background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6);
  color: #555; cursor: pointer; padding: 0; display: inline-flex;
  align-items: center; justify-content: center; flex-shrink: 0; border-radius: 0; }
.icon-btn-tab:hover { background: #d9d9d9; color: #000; }
.top-sep { width: 1px; height: 20px; background: #ccc; margin: 0 4px; flex-shrink: 0; }
.mobile-topbar { display: none; }
.mobile-channels-backdrop, .mobile-channels-panel { display: none; }
.members-backdrop { display: none; }

.chat-header { display: flex; align-items: center; padding: 8px 12px;
  background: #f5f5f5; background-image: linear-gradient(#ffffff, #f0f0f0);
  border-bottom: 1px solid #ccc; flex-shrink: 0; gap: 8px; }
.chat-header .title { font-weight: bold; font-size: 14px; line-height: 1.1; color: #222; }
.chat-header .subtitle { font-size: 11px; color: #777; }
.chat-header .user-info { margin-left: auto; display: flex; align-items: center;
  gap: 8px; font-size: 11px; color: #666; }
.chat-header .user-info .nick { font-weight: bold; color: #2b3d51; }
.members-toggle { display: none; }

.chat-body { flex: 1; display: flex; min-height: 0; overflow: hidden; }
.chat-feed { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch;
  padding: 8px 12px; background: #fdfdfd; position: relative; min-width: 0; }
.empty-state { margin: auto; text-align: center; color: #aaa; font-size: 12px; padding-top: 60px; }
.empty-state svg { display: block; margin: 0 auto 10px; color: #ccc; }

.chat-members { width: 220px; flex-shrink: 0; background: #f7f8fa;
  border-left: 1px solid #e0e0e0; display: flex; flex-direction: column; overflow: hidden; }
.members-head { padding: 10px 14px; border-bottom: 1px solid #e0e0e0;
  font-family: "Courier New", Courier, monospace;
  font-size: 12px; font-weight: bold; color: #2b3d51;
  display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.members-count { background: #e9eef3; color: #4a5a6a;
  font-size: 11px; font-weight: bold; padding: 1px 6px; border-radius: 8px; margin-left: auto; }
.members-list { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 4px 0; }
.member-item { display: flex; align-items: center; gap: 8px;
  padding: 6px 14px; font-size: 12.5px; color: #333; cursor: default;
  transition: background .15s; }
.member-item:hover { background: #eef3f8; }
.member-item.self { background: #eaf4fb; }
.member-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: #bbb; }
.member-dot.online { box-shadow: 0 0 0 2px rgba(76,175,80,.25); }
.member-name { flex: 1; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; font-weight: bold; }
.member-you { font-size: 10.5px; color: #999; font-weight: normal; margin-left: 3px; }
.members-empty { padding: 30px 14px; text-align: center; color: #aaa; font-size: 12px; }

.msg-row { display: grid; grid-template-columns: minmax(70px, 110px) 1fr;
  column-gap: 10px; padding: 3px 4px; align-items: start;
  font-size: 13px; line-height: 1.5; border-radius: 3px;
  transition: background .15s; word-wrap: break-word; }
.msg-row:hover { background: #f2f6fa; }
.msg-row.highlight { background: #fff3a8; }
.msg-row.grouped { padding-top: 0; }
.msg-row.mentioned-me {
  background: #fff3a8;
  border-left: 3px solid #f5b800;
  padding-left: 7px;
  animation: mentionPop 1.6s ease-out;
}
@keyframes mentionPop {
  0%   { background: #ffe49c; box-shadow: 0 0 0 4px rgba(245,184,0,.25); }
  60%  { background: #fff3a8; box-shadow: 0 0 0 0 rgba(245,184,0,0); }
  100% { background: #fff3a8; box-shadow: none; }
}
body.dark .msg-row.mentioned-me {
  background: #4d4218; border-left-color: #d4a017;
}
body.dark .msg-row.mentioned-me:hover { background: #554a1d; }
.msg-author { font-weight: bold; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; margin-top: 1px; }
.msg-author.hidden { visibility: hidden; }
.msg-content { min-width: 0; word-break: break-word; overflow-wrap: anywhere; }
.msg-text { color: #222; }
.msg-text code { background: #eef1f5; color: #c7254e;
  padding: 1px 5px; border-radius: 3px;
  font-family: "Courier New", monospace; font-size: 12.5px; }
body.dark .msg-text code { background: #2d2d33; color: #ff9db2; }
.msg-time { color: #b0b8c0; font-size: 10.5px; margin-left: 6px; white-space: nowrap; }
.mention { background: #ffe066; color: #7a4a00; font-weight: bold;
  padding: 0 4px; border-radius: 3px; }
body.dark .mention { background: #5a4a00; color: #ffe680; }
a.ext-link { color: #006dcc; text-decoration: underline; cursor: pointer;
  word-break: break-all; }
a.ext-link:hover { color: #005a9e; }
body.dark a.ext-link { color: #6cb6ff; }
body.dark a.ext-link:hover { color: #8ac9ff; }
.msg-system { color: #a94442; background: #fcebeb; border: 1px solid #f5c6c6;
  font-size: 11.5px; padding: 4px 8px; margin: 4px 0; display: flex;
  align-items: center; border-radius: 3px; }

.mention-pop { position: absolute; z-index: 40;
  background: #fff; border: 1px solid #bbb; box-shadow: 0 4px 12px rgba(0,0,0,.2);
  max-height: 220px; overflow-y: auto; min-width: 180px; display: none; }
.mention-pop.open { display: block; }
.mention-pop-item { display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; cursor: pointer; font-size: 13px; color: #333; }
.mention-pop-item:hover, .mention-pop-item.active { background: #eaf4fb; }
.mention-pop-item .mp-name { font-weight: bold; }

.composer { display: flex; align-items: flex-end; gap: 6px; padding: 8px 10px;
  background: #f5f5f5; background-image: linear-gradient(#f0f0f0, #ffffff);
  border-top: 1px solid #ccc; flex-shrink: 0; position: relative;
  padding-bottom: calc(8px + env(safe-area-inset-bottom, 0px)); }
.composer textarea { flex: 1; resize: none; padding: 6px 8px !important;
  border: 1px solid #bbb !important; font-size: 12px !important; max-height: 140px;
  outline: none !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.1) !important;
  background: #fff !important; color: #333 !important; box-sizing: border-box !important;
  margin: 0 !important; overflow: hidden; border-radius: 0 !important; }
.composer textarea:focus { border-color: #0088cc !important; }
.composer textarea[disabled] { background: #f7e6e6 !important; border-color: #d6a0a0 !important;
  color: #a94442 !important; }
.composer .icon-btn { width: 32px; height: 32px; padding: 0; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center; }

.msg-menu-backdrop { position: fixed; inset: 0; z-index: 999; }
.msg-menu { position: fixed; z-index: 1000;
  background: #fff; border: 1px solid #b8c4d0;
  box-shadow: 0 4px 16px rgba(0,0,0,.25);
  min-width: 200px; padding: 4px 0; border-radius: 0; }
.msg-menu-item { padding: 11px 16px; cursor: pointer;
  font-size: 13px; color: #333;
  display: flex; align-items: center; gap: 10px; user-select: none; }
.msg-menu-item svg { flex-shrink: 0; color: #6c757d; }
.msg-menu-item:hover { background: #eaf4fb; }
.msg-menu-item:active { background: #d6e8f7; }
.msg-menu-item + .msg-menu-item { border-top: 1px solid #eee; }
body.dark .msg-menu { background: #2d2d30; border-color: #3c3c3c; box-shadow: 0 4px 16px rgba(0,0,0,.6); }
body.dark .msg-menu-item { color: #ddd; }
body.dark .msg-menu-item svg { color: #888; }
body.dark .msg-menu-item:hover { background: #37373d; }
body.dark .msg-menu-item + .msg-menu-item { border-top-color: #3c3c3c; }

.my-modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.45);
  z-index: 1000; display: none; justify-content: center; align-items: center;
  padding: 20px; box-sizing: border-box; }
.my-modal-backdrop.open { display: flex; }
.my-modal { width: 400px; max-width: 100%; background: #fff; border: 1px solid #666;
  box-shadow: 0 5px 20px rgba(0,0,0,.4); position: relative;
  box-sizing: border-box; border-radius: 0; }
.modal-head { padding: 8px 12px; background: #f5f5f5;
  background-image: linear-gradient(#ffffff, #efefef); border-bottom: 1px solid #ccc;
  font-weight: bold; font-size: 13px; display: flex; align-items: center;
  cursor: move; user-select: none; }
.modal-head .close-m { margin-left: auto; cursor: pointer; color: #666;
  padding: 4px; line-height: 1; display: inline-flex; align-items: center; }
.modal-head .close-m:hover { color: #c00; background: #e6e6e6; }
.modal-body { padding: 14px; text-align: left; }
.modal-foot { padding: 10px 12px; background: #f7f7f7; border-top: 1px solid #e5e5e5;
  text-align: right; }
.modal-foot .btn { margin-left: 6px; }
.error-msg { margin-top: 8px; font-size: 11.5px; color: #a94442; background: #fcebeb;
  border: 1px solid #f5c6c6; padding: 5px 8px; display: none; }
.error-msg.show { display: flex; align-items: center; }

.ext-link-warning { display: flex; gap: 10px; align-items: flex-start;
  padding: 12px; background: #fff7e0; border: 1px solid #f0dc98;
  border-radius: 4px; margin-bottom: 12px; font-size: 12.5px; color: #7a5a00;
  line-height: 1.45; }
.ext-link-warning svg { flex-shrink: 0; margin-top: 1px; color: #b8860b; }
body.dark .ext-link-warning { background: #3a3118; border-color: #5a4a18; color: #e0c890; }
body.dark .ext-link-warning svg { color: #d4a017; }
.ext-link-url { font-family: "Courier New", monospace;
  font-size: 12px; color: #006dcc; word-break: break-all;
  padding: 8px 10px; background: #f5f7fa;
  border: 1px solid #e0e5eb; border-radius: 3px; margin-top: 4px; }
body.dark .ext-link-url { background: #1e1e1e; border-color: #3c3c3c; color: #6cb6ff; }

#settingsModal { width: 520px; }
.settings-body { display: flex; min-height: 220px; }
.settings-tabs { width: 160px; background: #f5f5f5; border-right: 1px solid #ddd;
  padding: 8px 0; flex-shrink: 0; }
.settings-tab { display: block; width: 100%; text-align: left; padding: 9px 14px;
  background: transparent; border: 0; border-left: 3px solid transparent;
  cursor: pointer; font-size: 12px; color: #444; font-family: inherit; border-radius: 0; }
.settings-tab:hover { background: #e9ecef; }
.settings-tab.active { background: #fff; border-left-color: #0088cc;
  color: #006dcc; font-weight: bold; }
.settings-content { flex: 1; padding: 16px; overflow-y: auto; max-height: 60vh; min-width: 0; }
.settings-pane { display: none; }
.settings-pane.active { display: block; }
.settings-row { margin-bottom: 16px; display: flex; justify-content: space-between;
  align-items: center; gap: 10px; }
.settings-row.block { display: block; }
.settings-label { font-size: 11px; text-transform: uppercase; letter-spacing: .5px;
  color: #7b8a99; font-weight: bold; margin-bottom: 4px; }
.settings-value { font-size: 13px; color: #222; font-weight: bold; }
.settings-lang-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 5px; margin-top: 8px; }
.settings-lang-grid .lang-menu-item { padding: 5px 7px; }
.settings-section-title { font-size: 13px; font-weight: bold; color: #222;
  padding-bottom: 8px; border-bottom: 1px solid #eee; margin-bottom: 12px; }

body.dark { background: #1a1a1a; color: #ccc; }
body.dark .boot-screen { background: #1a1a1a; background-image: none; }
body.dark .boot-screen .boot-text { color: #888; }
body.dark .boot-screen .boot-spinner { border-color: #3c3c3c; border-top-color: #0e639c; }
body.dark .login-screen { background: #1a1a1a; background-image: none; }
body.dark .login-box { background: #252526; border-color: #3c3c3c; }
body.dark .login-box h2 { color: #eaeaea; }
body.dark .login-box p { color: #888; }
body.dark .my-label { color: #888 !important; }
body.dark .my-input { background: #1e1e1e !important; color: #ddd !important; border-color: #4a4a4c !important; }
body.dark .login-error { background: #3a1f1f; border-color: #5a2a2a; color: #e0a0a0; }
body.dark .login-settings, body.dark .uptime-line { border-top-color: #3c3c3c; }
body.dark .settings-btn { background: #37373d; background-image: none; color: #ccc;
  border-color: #4a4a4c; text-shadow: none; }
body.dark .uptime-line { color: #888; }
body.dark .uptime-line .u-val { color: #aaa; }
body.dark .remember-row { color: #aaa !important; }
body.dark .lang-menu { background: #252526; border-color: #3c3c3c; }
body.dark .lang-menu-item { color: #ccc; background: #2d2d30; border-color: #3c3c3c; }
body.dark .lang-menu-item.active { background: #0e639c; color: #fff; border-color: #0e639c; }
body.dark .lang-menu-item .check { color: #6cb6ff; }
body.dark .app { background: #252526; }
body.dark .tabs-bar, body.dark .chat-header, body.dark .composer, body.dark .mobile-topbar {
  background: #2d2d30; background-image: none; border-color: #3c3c3c; }
body.dark .channel-tab, body.dark .icon-btn-tab, body.dark .scroll-arrow, body.dark .mobile-channels-btn {
  background: #37373d; background-image: none; color: #ccc;
  border-color: #4a4a4c; text-shadow: none; }
body.dark .channel-tab.active { background: #0e639c; border-color: #0e639c; color: #fff; }
body.dark .top-sep { background: #4a4a4c; }
body.dark .chat-header .title { color: #eaeaea; }
body.dark .chat-header .user-info .nick { color: #6cb6ff; }
body.dark .chat-feed { background: #1e1e1e; }
body.dark .msg-text { color: #ddd; }
body.dark .msg-row:hover { background: #2a2d33; }
body.dark .msg-row.highlight { background: #4d4218; }
body.dark .msg-system { background: #3a1f1f; border-color: #5a2a2a; color: #e0a0a0; }
body.dark .empty-state { color: #666; }
body.dark .composer textarea { background: #1e1e1e !important; color: #ddd !important;
  border-color: #4a4a4c !important; }
body.dark .my-modal { background: #252526; border-color: #3c3c3c; }
body.dark .modal-head { background: #2d2d30; background-image: none;
  border-color: #3c3c3c; color: #eaeaea; }
body.dark .modal-foot { background: #2a2a2c; border-color: #3c3c3c; }
body.dark .btn { background: #37373d; color: #ccc; border-color: #4a4a4c;
  background-image: none; text-shadow: none; }
body.dark .btn.primary { background: #0e639c; color: #fff;
  border-color: #0e639c; background-image: none; }
body.dark .settings-tabs { background: #2d2d30; border-right-color: #3c3c3c; }
body.dark .settings-tab { color: #ccc; }
body.dark .settings-tab.active { background: #252526; border-left-color: #0e639c; color: #6cb6ff; }
body.dark .settings-label { color: #777; }
body.dark .settings-value { color: #eaeaea; }
body.dark .settings-section-title { color: #eaeaea; border-bottom-color: #3c3c3c; }
body.dark .mention-pop { background: #252526; border-color: #3c3c3c; color: #ddd; }
body.dark .mention-pop-item { color: #ddd; }
body.dark .mention-pop-item:hover, body.dark .mention-pop-item.active { background: #37373d; }
body.dark .chat-members { background: #232326; border-left-color: #3c3c3c; }
body.dark .members-head { background: #232326; border-bottom-color: #3c3c3c; color: #eaeaea; }
body.dark .members-count { background: #37373d; color: #aaa; }
body.dark .member-item { color: #ccc; }
body.dark .member-item:hover { background: #2a2d33; }
body.dark .member-item.self { background: #1c3a5a; }
body.dark .member-you { color: #777; }
body.dark .mobile-channels-panel { background: #252526; border-color: #3c3c3c; }
body.dark .mcp-head { background: #2d2d30; border-color: #3c3c3c; color: #eaeaea; }
body.dark .mcp-item { border-color: #3c3c3c; color: #ccc; }
body.dark .mcp-item.active { background: #0e639c; color: #fff; }
body.dark .mcp-actions { background: #2a2a2c; border-color: #3c3c3c; }
body.dark .mcp-empty { color: #666; }

@media (hover: none) {
  .channel-tab:active { background: #c9c9c9; background-image: none; }
  .icon-btn-tab:active, .scroll-arrow:active, .settings-btn:active { background: #c9c9c9; background-image: none; }
  .lang-menu-item:active { background: #d6e8f7; }
  .msg-row:hover { background: transparent; }
  .member-item:hover { background: transparent; }
}

@media (max-width: 768px) {
  .login-screen { padding: 16px; align-items: flex-start; padding-top: 32px; padding-bottom: 40px; }
  .login-box { width: 100%; max-width: 420px; padding: 22px 18px 16px; }
  .login-box h2 { font-size: 20px; }
  .login-box p { font-size: 13px; }
  .login-box .btn { padding: 14px; font-size: 15px; min-height: 48px; }
  .my-input, .login-box .my-input, .my-modal .my-input { font-size: 16px !important; padding: 12px 12px !important; }
  .remember-row { font-size: 14px; }
  .remember-row input { width: 18px; height: 18px; margin-top: 2px; }
  .settings-btn { height: 44px; font-size: 14px; }
  .uptime-line { font-size: 12px; }

  .tabs-bar { display: none !important; }

  .mobile-topbar { display: flex; align-items: center; gap: 8px;
    padding: 8px 10px; background: #f5f5f5;
    background-image: linear-gradient(#ffffff, #ececec);
    border-bottom: 1px solid #ccc; flex-shrink: 0; }
  .mobile-channels-btn { flex: 1; display: inline-flex; align-items: center; gap: 10px;
    padding: 12px 14px; border: 1px solid #bbb;
    background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6);
    color: #333; cursor: pointer;
    font-family: "Courier New", Courier, monospace;
    font-size: 14px; font-weight: bold; text-align: left;
    border-radius: 0; min-height: 48px; }
  .mobile-channels-btn:active { background: #d0d0d0; background-image: none; }
  .mobile-channels-btn svg { width: 20px !important; height: 20px !important; flex-shrink: 0; }
  .mobile-settings-btn { width: 48px; height: 48px; padding: 0; flex-shrink: 0;
    display: inline-flex; align-items: center; justify-content: center; }
  .mobile-settings-btn svg { width: 22px !important; height: 22px !important; }

  .chat-header { padding: 10px 12px; }
  .chat-header .title { font-size: 15px; }
  .chat-header .subtitle { font-size: 12px; }
  .chat-header .user-info { display: none; }
  .members-toggle { display: inline-flex; margin-left: auto;
    width: 42px; height: 42px; }
  .members-toggle svg { width: 20px !important; height: 20px !important; }

  .chat-feed { padding: 8px 10px; }
  .msg-row { grid-template-columns: minmax(58px, 84px) 1fr;
    column-gap: 8px; padding: 4px 4px; font-size: 14.5px; }
  .msg-author { font-size: 14px; }
  .msg-time { font-size: 11px; }
  .msg-row.mentioned-me { padding-left: 6px; }

  .composer { padding: 8px 10px; gap: 8px; }
  .composer textarea { font-size: 16px !important; padding: 12px 12px !important;
    max-height: 120px; }
  .composer .icon-btn { width: 48px; height: 48px; }
  .composer .icon-btn svg { width: 20px !important; height: 20px !important; }

  .mention-pop { min-width: 200px; }

  .my-modal-backdrop { padding: 10px; align-items: flex-start;
    padding-top: 20px; padding-bottom: 20px; overflow-y: auto; }
  .my-modal { width: 100%; max-width: 500px; margin: auto; }
  #settingsModal { width: 100%; max-width: 500px; }
  .modal-head { padding: 12px 14px; font-size: 15px; }
  .modal-body { padding: 16px; }
  .modal-foot { padding: 12px; }
  .modal-foot .btn { padding: 12px 20px; font-size: 14px; min-height: 44px; }

  .settings-body { flex-direction: column; min-height: 0; }
  .settings-tabs { width: 100%; border-right: 0; border-bottom: 1px solid #ddd;
    padding: 0; display: flex; }
  .settings-tab { flex: 1; text-align: center; padding: 14px 4px;
    border-left: 0; border-bottom: 3px solid transparent; font-size: 13px; }
  .settings-tab.active { border-left-color: transparent; border-bottom-color: #0088cc; }
  body.dark .settings-tab.active { border-left-color: transparent; border-bottom-color: #0e639c; }
  .settings-content { padding: 14px; max-height: 55vh; }
  .settings-lang-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }

  .lang-menu { width: calc(100vw - 20px); grid-template-columns: repeat(2, minmax(0, 1fr));
    padding: 6px; gap: 4px; }
  .lang-menu-item { padding: 10px; font-size: 12.5px; }

  .mobile-channels-backdrop { display: block; position: fixed; inset: 0;
    background: rgba(0,0,0,.45); z-index: 900;
    opacity: 0; pointer-events: none; transition: opacity .2s ease-out; }
  .mobile-channels-backdrop.open { opacity: 1; pointer-events: auto; }
  .mobile-channels-panel { display: flex; flex-direction: column;
    position: fixed; top: 0; left: 0; bottom: 0;
    width: 88%; max-width: 340px;
    background: #fff; z-index: 901;
    box-shadow: 4px 0 20px rgba(0,0,0,.35);
    transform: translateX(-100%); transition: transform .2s ease-out;
    pointer-events: none; border-right: 1px solid #b8c4d0; }
  .mobile-channels-panel.open { transform: translateX(0); pointer-events: auto; }
  .mcp-head { padding: 14px 16px; flex-shrink: 0;
    border-bottom: 1px solid #ccc;
    background: #f5f5f5; background-image: linear-gradient(#ffffff, #efefef);
    display: flex; align-items: center;
    font-family: "Courier New", Courier, monospace;
    font-weight: bold; font-size: 15px; color: #222; }
  .mcp-close { margin-left: auto; background: transparent; border: 0;
    cursor: pointer; padding: 8px; color: #666;
    display: inline-flex; align-items: center; border-radius: 0; }
  .mcp-list { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 4px 0; }
  .mcp-item { display: flex; align-items: center; gap: 10px;
    padding: 8px 8px 8px 16px; border-bottom: 1px solid #f0f0f0;
    cursor: pointer;
    font-family: "Courier New", Courier, monospace;
    font-size: 14.5px; color: #333;
    min-height: 54px; box-sizing: border-box; }
  .mcp-item:active { background: #eaf4fb; }
  .mcp-item.active { background: #d6e8f7; color: #004a80; font-weight: bold; }
  .mcp-item .lock-ico { display: inline-flex; align-items: center; flex-shrink: 0; }
  .mcp-item .mcp-name { flex: 1; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .mcp-item .mcp-close-btn { flex-shrink: 0; width: 40px; height: 40px;
    display: inline-flex; align-items: center; justify-content: center;
    color: #888; cursor: pointer; border-radius: 0; }
  .mcp-item .mcp-close-btn:active { color: #c00; background: rgba(192,0,0,.1); }
  .mcp-empty { padding: 40px 16px; text-align: center; color: #999; font-size: 13px; }
  .mcp-actions { padding: 12px; border-top: 1px solid #ccc;
    background: #f7f7f7; flex-shrink: 0; display: flex; gap: 8px;
    padding-bottom: calc(12px + env(safe-area-inset-bottom, 0px)); }
  .mcp-actions .btn { flex: 1; padding: 14px 10px; font-size: 13px;
    border-radius: 0; min-height: 48px; }

  .members-backdrop { display: block; position: fixed; inset: 0;
    background: rgba(0,0,0,.45); z-index: 901;
    opacity: 0; pointer-events: none; transition: opacity .2s ease-out; }
  .members-backdrop.open { opacity: 1; pointer-events: auto; }
  .chat-members { position: fixed; top: 0; right: 0; bottom: 0;
    width: 84%; max-width: 320px; z-index: 902;
    transform: translateX(100%); transition: transform .2s ease-out;
    box-shadow: -4px 0 20px rgba(0,0,0,.35); border-left: 0; }
  .chat-members.open { transform: translateX(0); }
  .members-head { padding: 14px 16px; font-size: 14px; }
  .member-item { padding: 12px 16px; font-size: 14px; min-height: 48px; }
  .member-dot { width: 10px; height: 10px; }

  .msg-menu { min-width: 220px; }
  .msg-menu-item { padding: 14px 18px; font-size: 15px; min-height: 48px; }
}

@media (max-width: 400px) {
  .msg-row { grid-template-columns: minmax(52px, 72px) 1fr; column-gap: 6px; }
  .msg-author { font-size: 13.5px; }
  .lang-menu { grid-template-columns: 1fr; }
  .settings-lang-grid { grid-template-columns: 1fr; }
}

@media (min-width: 769px) and (max-width: 1024px) {
  .lang-menu { width: 620px; }
  .my-modal { max-width: 500px; }
  #settingsModal { width: 560px; }
  .msg-row { grid-template-columns: minmax(80px, 120px) 1fr; }
  .chat-members { width: 200px; }
}
</style>
</head>
<body>

<div class="boot-screen" id="bootScreen">
  <div class="boot-spinner"></div>
  <div class="boot-text" id="bootText">Loading...</div>
</div>

<div class="login-screen" id="loginScreen">
  <div class="login-box">
    <h2 id="loginTitle">sldchat</h2>
    <p id="loginSubtitle"></p>
    <div class="field-group">
      <label class="my-label" id="lblNick" for="loginName"></label>
      <input type="text" id="loginName" class="my-input" maxlength="32"
             autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false">
    </div>
    <div class="field-group">
      <label class="my-label" id="lblPass" for="loginPass"></label>
      <input type="password" id="loginPass" class="my-input" maxlength="128" autocomplete="new-password">
    </div>
    <label class="remember-row">
      <input type="checkbox" id="rememberMe" checked>
      <span id="rememberLbl"></span>
    </label>
    <button class="btn primary" id="loginBtn" type="button"></button>
    <div class="login-error" id="loginError"><span id="loginErrorText"></span></div>
    <div class="login-settings">
      <button class="settings-btn" id="themeBtn" type="button"><span id="themeIcon"></span></button>
      <button class="settings-btn" id="langBtn" type="button">
        <span id="langFlag"></span>
        <span class="lang-code" id="langCode"></span>
      </button>
    </div>
    <div class="uptime-line">
      <span id="uptimeLbl">Uptime</span>
      <span class="u-val" id="uptimeVal">—</span>
    </div>
  </div>
</div>

<div class="lang-backdrop" id="langBackdrop"></div>
<div class="lang-menu" id="langMenu"></div>

<div class="app" id="app">
  <div class="mobile-topbar" id="mobileTopbar">
    <button class="mobile-channels-btn" id="mobileChannelsBtn" type="button">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>
        <line x1="8" y1="18" x2="21" y2="18"/>
        <line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/>
        <line x1="3" y1="18" x2="3.01" y2="18"/>
      </svg>
      <span id="mobileChannelsLbl">Channels</span>
    </button>
    <button class="icon-btn-tab mobile-settings-btn" id="mobileSettingsBtn" type="button">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3"/>
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
      </svg>
    </button>
  </div>

  <div class="tabs-bar">
    <button class="scroll-arrow" id="tabScrollLeft" type="button">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
    </button>
    <div class="tabs-scroll" id="tabsScroll"></div>
    <button class="scroll-arrow" id="tabScrollRight" type="button">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
    </button>
    <div class="top-sep"></div>
    <button class="icon-btn-tab" id="addTabBtn">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
    </button>
    <button class="icon-btn-tab" id="connectBtn">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
        <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
      </svg>
    </button>
    <div class="top-sep"></div>
    <button class="icon-btn-tab" id="settingsBtn">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3"/>
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
      </svg>
    </button>
  </div>

  <div class="chat-header">
    <div>
      <div class="title" id="headerTitle">—</div>
      <div class="subtitle" id="headerSubtitle">—</div>
    </div>
    <div class="user-info">
      <span id="lblLoggedAs"></span>
      <span class="nick" id="headerUser">—</span>
    </div>
    <button class="icon-btn-tab members-toggle" id="membersToggleBtn" type="button">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
        <circle cx="9" cy="7" r="4"/>
        <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
        <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
      </svg>
    </button>
  </div>

  <div class="chat-body">
    <div class="chat-feed" id="chatFeed"></div>
    <aside class="chat-members" id="chatMembers">
      <div class="members-head">
        <span id="membersTitle">Members</span>
        <span class="members-count" id="membersCount">0</span>
      </div>
      <div class="members-list" id="membersList"></div>
    </aside>
  </div>

  <div class="composer">
    <textarea id="msgInput" rows="1" autocomplete="off" autocapitalize="sentences" spellcheck="false"></textarea>
    <button class="btn primary icon-btn" id="sendBtn" type="button">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
    </button>
    <div class="mention-pop" id="mentionPop"></div>
  </div>
</div>

<div class="members-backdrop" id="membersBackdrop"></div>

<div class="mobile-channels-backdrop" id="mobileChannelsBackdrop"></div>
<div class="mobile-channels-panel" id="mobileChannelsPanel">
  <div class="mcp-head">
    <span id="mcpTitle">Channels</span>
    <button class="mcp-close" id="mcpClose" type="button">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="mcp-list" id="mcpList"></div>
  <div class="mcp-actions">
    <button class="btn primary" id="mcpCreateBtn" type="button"></button>
    <button class="btn" id="mcpConnectBtn" type="button"></button>
  </div>
</div>

<div class="my-modal-backdrop" id="createBackdrop">
  <div class="my-modal" id="newChannelModal">
    <div class="modal-head" id="createModalHead">
      <span id="createModalTitle"></span>
      <span class="close-m" data-close-modal="create">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </span>
    </div>
    <div class="modal-body">
      <label class="my-label" id="lblCreateName" for="newChannelName"></label>
      <input type="text" id="newChannelName" class="my-input" maxlength="40" autocomplete="off">
    </div>
    <div class="modal-foot">
      <button class="btn" type="button" data-close-modal="create" id="btnCancelCreate"></button>
      <button class="btn primary" id="createChannelBtn" type="button"></button>
    </div>
  </div>
</div>

<div class="my-modal-backdrop" id="connectBackdrop">
  <div class="my-modal" id="connectModal">
    <div class="modal-head" id="connectModalHead">
      <span id="connectModalTitle"></span>
      <span class="close-m" data-close-modal="connect">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </span>
    </div>
    <div class="modal-body">
      <label class="my-label" id="lblConnectName" for="connectChannelName"></label>
      <input type="text" id="connectChannelName" class="my-input" maxlength="40" autocomplete="off">
      <div class="error-msg" id="connectError"><span class="sys-icon">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="13"/></svg>
      </span><span id="connectErrorText"></span></div>
    </div>
    <div class="modal-foot">
      <button class="btn" type="button" data-close-modal="connect" id="btnCancelConnect"></button>
      <button class="btn primary" id="connectChannelBtn" type="button"></button>
    </div>
  </div>
</div>

<div class="my-modal-backdrop" id="settingsBackdrop">
  <div class="my-modal" id="settingsModal">
    <div class="modal-head" id="settingsModalHead">
      <span id="settingsTitle"></span>
      <span class="close-m" data-close-modal="settings">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </span>
    </div>
    <div class="settings-body">
      <div class="settings-tabs">
        <button class="settings-tab active" data-tab="account" id="tabAccount"></button>
        <button class="settings-tab" data-tab="appearance" id="tabAppearance"></button>
      </div>
      <div class="settings-content">
        <div class="settings-pane active" data-pane="account">
          <div class="settings-section-title" id="settingsAccountHead"></div>
          <div class="settings-row">
            <div>
              <div class="settings-label" id="settingsUserLabel"></div>
              <div class="settings-value" id="settingsUser">—</div>
            </div>
          </div>
          <button class="btn" id="settingsLogoutBtn" type="button" style="width:100%; margin-top:14px;"></button>
        </div>
        <div class="settings-pane" data-pane="appearance">
          <div class="settings-section-title" id="settingsAppearanceHead"></div>
          <div class="settings-row">
            <div>
              <div class="settings-label" id="settingsThemeLabel"></div>
              <div class="settings-value" id="settingsThemeVal">—</div>
            </div>
            <button class="btn" id="settingsThemeToggle" type="button"></button>
          </div>
          <div class="settings-row block">
            <div class="settings-label" id="settingsLangLabel"></div>
            <div class="settings-lang-grid" id="settingsLangGrid"></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="my-modal-backdrop" id="extLinkBackdrop">
  <div class="my-modal" id="extLinkModal">
    <div class="modal-head" id="extLinkModalHead">
      <span id="extLinkTitle"></span>
      <span class="close-m" data-close-modal="extLink">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </span>
    </div>
    <div class="modal-body">
      <div class="ext-link-warning">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/>
          <line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
        <div id="extLinkWarning"></div>
      </div>
      <div class="ext-link-url" id="extLinkUrl"></div>
    </div>
    <div class="modal-foot">
      <button class="btn" id="extLinkBack" type="button"></button>
      <button class="btn primary" id="extLinkGo" type="button"></button>
    </div>
  </div>
</div>

<script>
"use strict";
/* CLIENT PROTECTION */
(function() {
  const isEditable = (el) => el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
  document.addEventListener('selectstart', e => { if (!isEditable(e.target)) e.preventDefault(); }, true);
  document.addEventListener('copy', e => { if (!isEditable(e.target)) e.preventDefault(); }, true);
  document.addEventListener('cut', e => { if (!isEditable(e.target)) e.preventDefault(); }, true);
  document.addEventListener('dragstart', e => e.preventDefault(), true);
  document.addEventListener('dragover', e => e.preventDefault(), true);
  document.addEventListener('drop', e => e.preventDefault(), true);
  document.addEventListener('keydown', e => {
    const k = (e.key || '').toLowerCase();
    if (e.key === 'F12') { e.preventDefault(); return false; }
    if (e.ctrlKey && e.shiftKey && ['i','j','c','k'].includes(k)) { e.preventDefault(); return false; }
    if (e.ctrlKey && !e.shiftKey && !e.altKey) {
      if (k === 'u' || k === 's' || k === 'p') { e.preventDefault(); return false; }
      if (k === 'a' && !isEditable(e.target)) { e.preventDefault(); return false; }
    }
  }, true);
  try { window.print = () => {}; } catch(e){}
  document.addEventListener('beforeprint', e => e.preventDefault());
  const noop = () => {};
  window.__err = (...a) => { try { (console.__errOrig || console.error).apply(console, a); } catch(e){} };
  try { console.__errOrig = console.error.bind(console); } catch(e){}
  try {
    console.log = noop; console.info = noop; console.warn = noop; console.debug = noop;
    console.error = noop; console.trace = noop; console.dir = noop; console.table = noop;
  } catch(e){}
  setInterval(() => { try { console.clear && console.clear(); } catch(e){} }, 4000);
})();
</script>

<script>
"use strict";
const I18N = %%I18N%%;
const FLAGS = %%FLAGS%%;
const LANG_ORDER = %%LANG_ORDER%%;
const LANG_NAMES = {en:"English",ru:"Русский",es:"Español",de:"Deutsch",fr:"Français",it:"Italiano",pt:"Português",nl:"Nederlands",pl:"Polski",uk:"Українська",cs:"Čeština",sv:"Svenska",el:"Ελληνικά",tr:"Türkçe",ja:"日本語",ko:"한국어",zh:"中文",ar:"العربية",he:"עברית",hi:"हिन्दी"};
const CACHE_KEY = "sldchat_cache_v1";

const SVG = {
  x:'<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  lock:'<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
  ban:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>',
  chat:'<svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  sun:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>',
  moon:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
  at:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-3.92 7.94"/></svg>',
  copy:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
};

const USER_COLORS = [
  '#0088cc','#5bb75b','#da4f49','#faa732','#6f42c1','#49afcd','#d63384','#20c997',
  '#b8860b','#e83e8c','#ff6347','#4682b4','#8b4513','#2e8b57','#9932cc','#ff8c00',
  '#1f77b4','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2','#7f7f7f','#bcbd22'
];
function colorForUser(name) {
  if (!name) return '#888';
  let h = 0; const s = String(name);
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return USER_COLORS[Math.abs(h) % USER_COLORS.length];
}
function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random()*16|0;
    const v = c === 'x' ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escapeRegExp(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

// EMOJI
const EMOJI_MAP = {
  like:'👍','+1':'👍',thumbsup:'👍',good:'👍',
  dislike:'👎','-1':'👎',thumbsdown:'👎',bad:'👎',
  heart:'❤️',love:'❤️',smile:'😊',happy:'😊',blush:'😊',
  laugh:'😂',lol:'😂',joy:'😂',haha:'😂',
  cry:'😢',sad:'😢',angry:'😠',mad:'😠',rage:'😡',wink:'😉',
  think:'🤔',thinking:'🤔',ok:'👌',ok_hand:'👌',
  fire:'🔥',hot:'🔥',lit:'🔥',star:'⭐',star2:'🌟','100':'💯',
  rocket:'🚀',party:'🎉',tada:'🎉',celebrate:'🎉',clap:'👏',
  pray:'🙏',wave:'👋',hi:'👋',hello:'👋',eyes:'👀',sweat:'😅',
  cool:'😎',sunglasses:'😎',wow:'😮',surprised:'😮',kiss:'😘',
  sleepy:'😴',tired:'😴',nerd:'🤓',
  dog:'🐶',cat:'🐱',pizza:'🍕',beer:'🍺',coffee:'☕',cake:'🎂',gift:'🎁',
  check:'✅',done:'✅',yes:'✅',x:'❌',cross:'❌',no:'❌',warn:'⚠️',warning:'⚠️',
  info:'ℹ️',question:'❓',excl:'❗',
  clown:'🤡',ghost:'👻',alien:'👽',robot:'🤖',money:'💰',crown:'👑',flag:'🏁',
  soccer:'⚽',basketball:'🏀',game:'🎮',music:'🎵',book:'📚',bulb:'💡',
  lock:'🔒',key:'🔑',phone:'📱',computer:'💻',mail:'✉️',bell:'🔔',zap:'⚡',
  boom:'💥',bomb:'💣',gun:'🔫',knife:'🔪',skull:'💀',poop:'💩',
  rainbow:'🌈',sun:'☀️',moon:'🌙',cloud:'☁️',snow:'❄️',umbrella:'☔',
  apple:'🍎',banana:'🍌',grape:'🍇',watermelon:'🍉',burger:'🍔',fries:'🍟',
  sushi:'🍣',ramen:'🍜',icecream:'🍦',candy:'🍬',cookie:'🍪',
  medal:'🏅',trophy:'🏆',diamond:'💎',needle:'💉',pill:'💊',balloon:'🎈',
  confetti:'🎊',package:'📦',hourglass:'⏳',clock:'⏰',calendar:'📅',
  camera:'📷',movie:'🎬',tv:'📺',headphones:'🎧',mic:'🎤',speaker:'🔊',
  search:'🔍',hammer:'🔨',wrench:'🔧',gear:'⚙️',scissors:'✂️',
  pen:'✏️',paperclip:'📎',pushpin:'📌',bookmark:'🔖',trash:'🗑️',recycle:'♻️',
  arrow_up:'⬆️',arrow_down:'⬇️',arrow_left:'⬅️',arrow_right:'➡️',
  muscle:'💪',point_up:'☝️',raised_hands:'🙌',handshake:'🤝',fist:'✊',
  punch:'👊',victory:'✌️',peace:'✌️',metal:'🤘',call_me:'🤙',cross_fingers:'🤞'
};

const URL_MARK = '\uE000';
const URL_RE = /\bhttps?:\/\/[^\s<>"'`]+/gi;
const WWW_RE = /\bwww\.[^\s<>"'`]+/gi;

function applyEmojis(escaped) {
  return escaped.replace(/:([a-zA-Z0-9_+\-]{1,32}):/g, (full, name) => {
    const key = name.toLowerCase();
    return EMOJI_MAP[key] || full;
  });
}
function applyFormatting(s) {
  s = s.replace(/`([^`\n]+)`/g, (m, c) => '<code>'+c+'</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<i>$2</i>');
  s = s.replace(/__([^_\n]+)__/g, '<u>$1</u>');
  s = s.replace(/~~([^~\n]+)~~/g, '<s>$1</s>');
  return s;
}
function renderRichText(text, channelId) {
  let s = escapeHtml(text);
  const urls = [];
  const tailRe = /[.,;:!?)\]}>]+$/;

  s = s.replace(URL_RE, m => {
    let tail = '';
    const match = m.match(tailRe);
    if (match) { tail = match[0]; m = m.slice(0, -tail.length); }
    urls.push(m);
    return URL_MARK + 'U' + (urls.length - 1) + URL_MARK + tail;
  });
  s = s.replace(WWW_RE, m => {
    let tail = '';
    const match = m.match(tailRe);
    if (match) { tail = match[0]; m = m.slice(0, -tail.length); }
    urls.push('http://' + m);
    return URL_MARK + 'U' + (urls.length - 1) + URL_MARK + tail;
  });

  s = applyFormatting(s);
  s = applyEmojis(s);

  const members = memberSetByChannel[channelId];
  if (members && members.size) {
    s = s.replace(/@([^\s@:<>"'&]{2,32})/gu, (full, name) => {
      if (members.has(name.toLowerCase())) return '<span class="mention">@'+name+'</span>';
      return full;
    });
  }

  s = s.replace(new RegExp(URL_MARK + 'U(\\d+)' + URL_MARK, 'g'), (full, idx) => {
    const url = urls[parseInt(idx, 10)];
    const display = url.replace(/^https?:\/\//, '');
    const shown = display.length > 60 ? display.slice(0, 60) + '…' : display;
    return '<a class="ext-link" href="#" data-url="' + escapeHtml(url) + '" rel="noopener noreferrer">' + escapeHtml(shown) + '</a>';
  });

  return s;
}

const decryptedCache = {};

function textMentionsMe(text) {
  if (!currentUser) return false;
  try {
    const re = new RegExp('@' + escapeRegExp(currentUser) + '(?![\\w])', 'i');
    return re.test(text);
  } catch(e) { return false; }
}

function updateAppVH() {
  const h = (window.visualViewport && window.visualViewport.height) || window.innerHeight;
  document.documentElement.style.setProperty('--app-vh', h + 'px');
}
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', updateAppVH);
  window.visualViewport.addEventListener('scroll', updateAppVH);
}
window.addEventListener('resize', updateAppVH);
window.addEventListener('orientationchange', () => setTimeout(updateAppVH, 100));
updateAppVH();
document.addEventListener('contextmenu', e => e.preventDefault());

let currentLang = localStorage.getItem('lang') || 'en';
let currentTheme = localStorage.getItem('theme') || 'light';
let authToken = null;
let currentUser = null;
let channels = [];
let activeId = null;
let ws = null;
let wsChannelId = null;
let channelKeys = {};
let onlineUsers = [];
let uptimeBase = 0, uptimeFetchAt = 0;
let authInFlight = false;
let channelMembers = {};
let memberSetByChannel = {};
let mentionState = { open:false, items:[], selected:0, startIdx:-1 };
let msgMenuEl = null;

const $ = (id) => document.getElementById(id);
const t = (key, vars) => {
  const dict = I18N[currentLang] || {};
  let s = dict[key];
  if (s === undefined) s = I18N.en[key];
  if (s === undefined) s = key;
  if (vars) for (const k in vars) s = s.replace(new RegExp('\\{'+k+'\\}','g'), vars[k]);
  return s;
};

function setState(st) {
  document.documentElement.setAttribute('data-state', st);
  if (st === 'login') updateAppVH();
}

// Cache
function saveCache() {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({
      ts: Date.now(),
      username: currentUser,
      channels: channels.map(c => ({ id: c.id, name: c.name, private: c.private, enc_key: c.enc_key }))
    }));
  } catch(e) {}
}
function loadCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const d = JSON.parse(raw);
    if (!d || !Array.isArray(d.channels)) return null;
    if (d.ts && Date.now() - d.ts > 7*24*3600*1000) return null;
    return d;
  } catch(e) { return null; }
}
function clearCache() { try { localStorage.removeItem(CACHE_KEY); } catch(e){} }

// Crypto
const enc = new TextEncoder();
const dec = new TextDecoder();
function hexToBytes(hex) {
  const arr = new Uint8Array(hex.length / 2);
  for (let i = 0; i < arr.length; i++) arr[i] = parseInt(hex.substr(i*2, 2), 16);
  return arr;
}
function b64(buf){let s='';const bytes=new Uint8Array(buf);for(let i=0;i<bytes.length;i++)s+=String.fromCharCode(bytes[i]);return btoa(s);}
function ub64(str){const bin=atob(str);const bytes=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);return bytes;}
async function importChannelKey(channelId, hexKey) {
  if (channelKeys[channelId]) return channelKeys[channelId];
  const kb = hexToBytes(hexKey);
  const key = await crypto.subtle.importKey('raw', kb, { name: 'AES-GCM', length: 256 }, false, ['encrypt','decrypt']);
  channelKeys[channelId] = key;
  return key;
}
async function encryptText(id, text) {
  const key = channelKeys[id];
  if (!key) throw new Error('no key');
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({name:'AES-GCM', iv}, key, enc.encode(text));
  return b64(iv) + '.' + b64(ct);
}
async function decryptText(id, payload) {
  try {
    const [ivB64, ctB64] = payload.split('.');
    if (!ivB64 || !ctB64) return payload;
    const key = channelKeys[id];
    if (!key) return '[no key]';
    const pt = await crypto.subtle.decrypt({name:'AES-GCM', iv: ub64(ivB64)}, key, ub64(ctB64));
    return dec.decode(pt);
  } catch(e) { return '[decrypt error]'; }
}

async function api(path, method='GET', body=null, withAuth=true) {
  const headers = { 'Content-Type': 'application/json' };
  if (withAuth && authToken) headers['x-auth-token'] = authToken;
  const res = await fetch(path, {method, headers, body: body ? JSON.stringify(body) : null});
  let data = null; try { data = await res.json(); } catch(e){}
  if (!res.ok) throw { status: res.status, detail: (data && data.detail) || 'error' };
  return data;
}

function fmtUptime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec/86400), h = Math.floor((sec%86400)/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  const p = n => String(n).padStart(2,'0');
  return (d>0 ? d+'d ' : '') + p(h)+':'+p(m)+':'+p(s);
}
async function refreshUptime() {
  try { const res = await fetch('/api/uptime'); const data = await res.json();
    uptimeBase = data.uptime; uptimeFetchAt = performance.now(); } catch(e){}
}
function renderUptime() {
  const el = $('uptimeVal'); if (!el) return;
  if (!uptimeFetchAt) { el.textContent = '—'; return; }
  el.textContent = fmtUptime(uptimeBase + (performance.now() - uptimeFetchAt)/1000);
}
setInterval(renderUptime, 1000); refreshUptime(); setInterval(refreshUptime, 30000);

function applyTheme() {
  document.body.classList.toggle('dark', currentTheme === 'dark');
  const ti = $('themeIcon'); if (ti) ti.innerHTML = currentTheme === 'dark' ? SVG.sun : SVG.moon;
  localStorage.setItem('theme', currentTheme);
  const tv = $('settingsThemeVal'); if (tv) tv.textContent = currentTheme === 'dark' ? t('settings_theme_dark') : t('settings_theme_light');
  const tt = $('settingsThemeToggle'); if (tt) tt.textContent = currentTheme === 'dark' ? t('settings_theme_light') : t('settings_theme_dark');
}
function toggleTheme() { currentTheme = currentTheme === 'dark' ? 'light' : 'dark'; applyTheme(); }

function applyLanguage() {
  localStorage.setItem('lang', currentLang);
  $('bootText').textContent = t('booting');
  $('loginTitle').textContent = t('login_title');
  $('loginSubtitle').textContent = t('login_subtitle');
  $('lblNick').textContent = t('field_nick');
  $('lblPass').textContent = t('field_password');
  $('loginName').placeholder = t('ph_nick');
  $('loginPass').placeholder = t('ph_password');
  $('rememberLbl').textContent = t('remember_me');
  if (!authInFlight) $('loginBtn').textContent = t('btn_login');
  $('uptimeLbl').textContent = t('uptime_label');
  $('lblLoggedAs').textContent = t('logged_as');
  $('addTabBtn').title = t('title_add');
  $('connectBtn').title = t('title_connect');
  $('settingsBtn').title = t('title_settings');
  $('mobileSettingsBtn').title = t('title_settings');
  $('createModalTitle').textContent = t('modal_create_title');
  $('lblCreateName').textContent = t('modal_name');
  $('newChannelName').placeholder = t('modal_name_ph');
  $('btnCancelCreate').textContent = t('btn_cancel');
  $('createChannelBtn').textContent = t('btn_create');
  $('connectModalTitle').textContent = t('modal_connect_title');
  $('lblConnectName').textContent = t('modal_connect_name');
  $('connectChannelName').placeholder = t('modal_connect_ph');
  $('btnCancelConnect').textContent = t('btn_cancel');
  $('connectChannelBtn').textContent = t('btn_connect');
  $('settingsTitle').textContent = t('settings_title');
  $('tabAccount').textContent = t('settings_account');
  $('tabAppearance').textContent = t('settings_appearance');
  $('settingsAccountHead').textContent = t('settings_account');
  $('settingsUserLabel').textContent = t('settings_user');
  $('settingsLogoutBtn').textContent = t('settings_logout');
  $('settingsAppearanceHead').textContent = t('settings_appearance');
  $('settingsThemeLabel').textContent = t('settings_theme');
  $('settingsLangLabel').textContent = t('settings_lang');
  $('mobileChannelsLbl').textContent = t('mobile_channels');
  $('mcpTitle').textContent = t('mobile_channels');
  $('mcpCreateBtn').textContent = t('modal_create_title');
  $('mcpConnectBtn').textContent = t('modal_connect_title');
  $('membersTitle').textContent = t('members_title');
  $('extLinkTitle').textContent = t('ext_link_title');
  $('extLinkWarning').textContent = t('ext_link_warning');
  $('extLinkBack').textContent = t('ext_link_back');
  $('extLinkGo').textContent = t('ext_link_continue');
  $('langFlag').innerHTML = FLAGS[currentLang] || '';
  $('langCode').textContent = currentLang.toUpperCase();
  applyTheme();
  buildLangMenu(); buildSettingsLangGrid();
  renderTabs(); renderHeader(); renderMessages(); updateMuteUI();
  renderMobileChannelList(); renderMembersSidebar();
}

function buildLangMenu() {
  const menu = $('langMenu'); menu.innerHTML = '';
  LANG_ORDER.forEach(code => {
    const item = document.createElement('div');
    item.className = 'lang-menu-item' + (code === currentLang ? ' active' : '');
    item.innerHTML = '<span class="flag-wrap">'+FLAGS[code]+'</span><span class="lang-name">'+LANG_NAMES[code]+'</span><span class="check">✓</span>';
    item.addEventListener('click', e => { e.stopPropagation(); currentLang = code; closeLangMenu(); applyLanguage(); });
    menu.appendChild(item);
  });
}
function buildSettingsLangGrid() {
  const grid = $('settingsLangGrid'); if (!grid) return;
  grid.innerHTML = '';
  LANG_ORDER.forEach(code => {
    const item = document.createElement('div');
    item.className = 'lang-menu-item' + (code === currentLang ? ' active' : '');
    item.innerHTML = '<span class="flag-wrap">'+FLAGS[code]+'</span><span class="lang-name">'+LANG_NAMES[code]+'</span><span class="check">✓</span>';
    item.addEventListener('click', () => { currentLang = code; applyLanguage(); });
    grid.appendChild(item);
  });
}
function openLangMenu() { $('langBackdrop').classList.add('open'); $('langMenu').classList.add('open'); }
function closeLangMenu() { $('langBackdrop').classList.remove('open'); $('langMenu').classList.remove('open'); }

let loginErrTimer = null;
function showLoginError(msg) {
  const box = $('loginError');
  if (!msg) { box.classList.remove('show'); return; }
  $('loginErrorText').textContent = msg;
  box.classList.add('show');
  clearTimeout(loginErrTimer);
  loginErrTimer = setTimeout(() => box.classList.remove('show'), 5000);
}

function setAuthBtnLoading(loading) {
  const btn = $('loginBtn');
  if (loading) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>' + t('btn_wait');
  } else {
    btn.disabled = false;
    btn.textContent = t('btn_login');
  }
}

async function doAuth() {
  if (authInFlight) return;
  const username = $('loginName').value.trim();
  const password = $('loginPass').value;
  if (!username || !password) { showLoginError(t('err_bad_credentials')); return; }
  if (username.length < 2) { showLoginError(t('err_bad_username')); return; }
  if (/[\s@:<>"'&]/.test(username)) { showLoginError(t('err_bad_username')); return; }
  if (password.length < 4) { showLoginError(t('err_bad_password')); return; }

  authInFlight = true;
  setAuthBtnLoading(true);
  $('loginName').disabled = true;
  $('loginPass').disabled = true;

  try {
    const res = await api('/api/auth', 'POST', { username, password }, false);
    authToken = res.token; currentUser = res.username;
    if ($('rememberMe').checked) {
      localStorage.setItem('auth_token', res.token);
      sessionStorage.removeItem('auth_token');
    } else {
      sessionStorage.setItem('auth_token', res.token);
      localStorage.removeItem('auth_token');
    }
    $('loginName').value = ''; $('loginPass').value = '';
    enterApp();
  } catch (e) {
    let msg;
    if (e.detail === 'bad_username') msg = t('err_bad_username');
    else if (e.detail === 'bad_password') msg = t('err_bad_password');
    else if (e.detail === 'too_many_attempts' || e.status === 429) msg = t('err_rate_limited');
    else if (e.status === 401) msg = t('err_bad_credentials');
    else msg = t('err_generic');
    showLoginError(msg);
  } finally {
    authInFlight = false;
    setAuthBtnLoading(false);
    $('loginName').disabled = false;
    $('loginPass').disabled = false;
  }
}
$('loginBtn').addEventListener('click', doAuth);
$('loginName').addEventListener('keydown', e => { if (e.key === 'Enter') $('loginPass').focus(); });
$('loginPass').addEventListener('keydown', e => { if (e.key === 'Enter') doAuth(); });

function enterApp() {
  $('headerUser').textContent = currentUser;
  $('settingsUser').textContent = currentUser;
  setState('app');
  updateAppVH();
  loadChannels();
}

async function tryRestoreSession() {
  const saved = localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');
  if (!saved) { setState('login'); return false; }
  authToken = saved;

  const cached = loadCache();
  if (cached && cached.username && cached.channels.length) {
    currentUser = cached.username;
    channels = cached.channels.map(c => ({ ...c, messages: [] }));
    for (const ch of channels) {
      if (ch.enc_key) { try { await importChannelKey(ch.id, ch.enc_key); } catch(e){} }
    }
    activeId = channels[0] ? channels[0].id : null;
    $('headerUser').textContent = currentUser;
    $('settingsUser').textContent = currentUser;
    setState('app');
    updateAppVH();
    renderAll();
    try {
      const me = await api('/api/me');
      currentUser = me.username;
      $('headerUser').textContent = currentUser;
      $('settingsUser').textContent = currentUser;
      loadChannels();
      return true;
    } catch(e) {
      localStorage.removeItem('auth_token'); sessionStorage.removeItem('auth_token');
      clearCache(); authToken = null; currentUser = null; channels = [];
      setState('login');
      return false;
    }
  }

  try {
    const me = await api('/api/me');
    currentUser = me.username;
    enterApp();
    return true;
  } catch(e) {
    localStorage.removeItem('auth_token'); sessionStorage.removeItem('auth_token');
    authToken = null;
    setState('login');
    return false;
  }
}

$('themeBtn').addEventListener('click', e => { e.stopPropagation(); toggleTheme(); });
$('langBtn').addEventListener('click', e => { e.stopPropagation();
  $('langMenu').classList.contains('open') ? closeLangMenu() : openLangMenu(); });
$('langBackdrop').addEventListener('click', closeLangMenu);

function openSettingsModal() {
  $('settingsBackdrop').classList.add('open');
  $('settingsUser').textContent = currentUser || '—';
}
$('settingsBtn').addEventListener('click', openSettingsModal);
$('mobileSettingsBtn').addEventListener('click', openSettingsModal);
document.querySelectorAll('.settings-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const target = tab.dataset.tab;
    document.querySelectorAll('.settings-tab').forEach(x => x.classList.toggle('active', x === tab));
    document.querySelectorAll('.settings-pane').forEach(p => p.classList.toggle('active', p.dataset.pane === target));
  });
});
$('settingsThemeToggle').addEventListener('click', toggleTheme);
$('settingsLogoutBtn').addEventListener('click', async () => {
  try { await api('/api/logout', 'POST'); } catch(e){}
  authToken = null; currentUser = null;
  channels = []; activeId = null; channelKeys = {};
  channelMembers = {}; memberSetByChannel = {}; Object.keys(decryptedCache).forEach(k => delete decryptedCache[k]);
  if (ws) { try { ws.close(); } catch(e){} ws = null; wsChannelId = null; }
  localStorage.removeItem('auth_token'); sessionStorage.removeItem('auth_token');
  clearCache();
  $('settingsBackdrop').classList.remove('open');
  closeMobileChannels(); closeMembersSidebar(); hideMsgMenu();
  setState('login');
  closeLangMenu(); updateAppVH();
});

// Members sidebar
function openMembersSidebar() {
  $('chatMembers').classList.add('open');
  $('membersBackdrop').classList.add('open');
}
function closeMembersSidebar() {
  $('chatMembers').classList.remove('open');
  $('membersBackdrop').classList.remove('open');
}
$('membersToggleBtn').addEventListener('click', openMembersSidebar);
$('membersBackdrop').addEventListener('click', closeMembersSidebar);

function renderMembersSidebar() {
  const box = $('membersList'); if (!box) return;
  const ch = channels.find(c => c.id === activeId);
  if (!ch) { box.innerHTML = '<div class="members-empty">—</div>'; $('membersCount').textContent = '0'; return; }
  const members = channelMembers[activeId] || [];
  const onlineSet = new Set((onlineUsers||[]).map(u => String(u).toLowerCase()));
  $('membersCount').textContent = String(members.length);
  if (!members.length) { box.innerHTML = '<div class="members-empty">—</div>'; return; }
  const sorted = [...members].sort((a, b) => {
    const ao = onlineSet.has(a.username.toLowerCase());
    const bo = onlineSet.has(b.username.toLowerCase());
    if (ao !== bo) return ao ? -1 : 1;
    return a.username.localeCompare(b.username);
  });
  box.innerHTML = '';
  sorted.forEach(m => {
    const isOnline = onlineSet.has(m.username.toLowerCase());
    const color = colorForUser(m.username);
    const isSelf = m.username === currentUser;
    const row = document.createElement('div');
    row.className = 'member-item' + (isSelf ? ' self' : '');
    row.innerHTML =
      '<span class="member-dot'+(isOnline?' online':'')+'" style="background:'+(isOnline?'#4caf50':'#bbb')+';"></span>' +
      '<span class="member-name" style="color:'+color+';">'+escapeHtml(m.username)+
        (isSelf ? '<span class="member-you"> '+t('users_you')+'</span>' : '')+
      '</span>';
    box.appendChild(row);
  });
}

// Mobile channels
function openMobileChannels() {
  renderMobileChannelList();
  $('mobileChannelsBackdrop').classList.add('open');
  $('mobileChannelsPanel').classList.add('open');
}
function closeMobileChannels() {
  $('mobileChannelsBackdrop').classList.remove('open');
  $('mobileChannelsPanel').classList.remove('open');
}
function renderMobileChannelList() {
  const box = $('mcpList'); if (!box) return;
  box.innerHTML = '';
  if (!channels.length) { box.innerHTML = '<div class="mcp-empty">'+t('empty_no_channels')+'</div>'; return; }
  channels.forEach(ch => {
    const item = document.createElement('div');
    item.className = 'mcp-item' + (ch.id === activeId ? ' active' : '');
    if (ch.private) {
      const l = document.createElement('span'); l.className = 'lock-ico'; l.innerHTML = SVG.lock;
      item.appendChild(l);
    }
    const n = document.createElement('span'); n.className = 'mcp-name'; n.textContent = ch.name;
    item.appendChild(n);
    const cl = document.createElement('span'); cl.className = 'mcp-close-btn'; cl.title = t('leave_channel');
    cl.innerHTML = SVG.x;
    cl.addEventListener('click', (e) => { e.stopPropagation(); leaveChannel(ch.id); });
    item.appendChild(cl);
    item.addEventListener('click', (e) => {
      if (e.target.closest && e.target.closest('.mcp-close-btn')) return;
      activeId = ch.id;
      closeMobileChannels();
      renderAll();
      openChannelWS(activeId);
      loadChannelMembers(activeId);
    });
    box.appendChild(item);
  });
}

async function leaveChannel(id) {
  try { await api('/api/channels/leave', 'POST', { channel_id: id }); } catch(e){}
  channels = channels.filter(c => c.id !== id);
  delete channelMembers[id]; delete memberSetByChannel[id]; delete channelKeys[id];
  if (activeId === id) {
    activeId = channels[0] ? channels[0].id : null;
    if (ws) { try { ws.close(); } catch(e){} ws = null; wsChannelId = null; }
    if (activeId) { openChannelWS(activeId); loadChannelMembers(activeId); }
  }
  renderAll(); renderMobileChannelList(); saveCache();
}

$('mobileChannelsBtn').addEventListener('click', openMobileChannels);
$('mcpClose').addEventListener('click', closeMobileChannels);
$('mobileChannelsBackdrop').addEventListener('click', closeMobileChannels);
$('mcpCreateBtn').addEventListener('click', () => { closeMobileChannels(); setTimeout(() => $('addTabBtn').click(), 60); });
$('mcpConnectBtn').addEventListener('click', () => { closeMobileChannels(); setTimeout(() => $('connectBtn').click(), 60); });

async function loadChannels() {
  try {
    const res = await api('/api/channels');
    const fresh = res.channels || [];
    for (const ch of fresh) {
      if (ch.enc_key) { try { await importChannelKey(ch.id, ch.enc_key); } catch(e){} }
    }
    channels = fresh;
    if (activeId && !channels.find(c => c.id === activeId)) activeId = null;
    if (!activeId && channels.length) activeId = channels[0].id;
    renderAll();
    if (activeId) { openChannelWS(activeId); loadChannelMembers(activeId); }
    saveCache();
  } catch(e) {
    if (e.status === 401) {
      authToken = null; currentUser = null; channels = []; clearCache();
      localStorage.removeItem('auth_token'); sessionStorage.removeItem('auth_token');
      setState('login');
    }
  }
}

async function loadChannelMembers(channelId) {
  try {
    const res = await api('/api/channels/' + encodeURIComponent(channelId) + '/members');
    channelMembers[channelId] = res.members || [];
    const s = new Set();
    (res.members || []).forEach(m => s.add(String(m.username).toLowerCase()));
    memberSetByChannel[channelId] = s;
    if (activeId === channelId) { renderMessages(); renderMembersSidebar(); }
  } catch (e) {
    channelMembers[channelId] = [];
    memberSetByChannel[channelId] = new Set();
  }
}

function renderTabs() {
  const wrap = $('tabsScroll'); wrap.innerHTML = '';
  channels.forEach(ch => {
    const tab = document.createElement('div');
    tab.className = 'channel-tab' + (ch.id === activeId ? ' active' : '');
    tab.dataset.id = ch.id;
    if (ch.private) {
      const l = document.createElement('span'); l.className = 'lock-ico'; l.innerHTML = SVG.lock;
      tab.appendChild(l);
    }
    const n = document.createElement('span'); n.textContent = ch.name; tab.appendChild(n);
    const x = document.createElement('span'); x.className = 'close-x'; x.dataset.close = ch.id; x.innerHTML = SVG.x;
    tab.appendChild(x);
    tab.addEventListener('click', e => {
      if (e.target.closest && e.target.closest('[data-close]')) return;
      if (activeId === ch.id) return;
      activeId = ch.id; renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
    });
    wrap.appendChild(tab);
  });
  scrollActiveTabIntoView();
  renderMobileChannelList();
}
$('tabsScroll').addEventListener('click', async e => {
  const closeEl = e.target.closest && e.target.closest('[data-close]');
  if (!closeEl) return;
  e.stopPropagation();
  leaveChannel(closeEl.getAttribute('data-close'));
});
function scrollActiveTabIntoView() {
  const wrap = $('tabsScroll');
  const activeTab = wrap.querySelector('.channel-tab.active');
  if (!activeTab) return;
  const wr = wrap.getBoundingClientRect(), tr = activeTab.getBoundingClientRect();
  if (tr.left < wr.left) wrap.scrollLeft += tr.left - wr.left - 6;
  else if (tr.right > wr.right) wrap.scrollLeft += tr.right - wr.right + 6;
}
$('tabScrollLeft').addEventListener('click', () => { $('tabsScroll').scrollLeft -= 140; });
$('tabScrollRight').addEventListener('click', () => { $('tabsScroll').scrollLeft += 140; });

function renderHeader() {
  const ch = channels.find(c => c.id === activeId);
  if (!ch) {
    $('headerTitle').textContent = t('header_no_channels');
    $('headerSubtitle').textContent = t('header_no_channels_sub');
    return;
  }
  $('headerTitle').textContent = '[#] ' + ch.name;
  const extra = onlineUsers.length ? ' · ' + onlineUsers.length + ' ' + t('online_label') : '';
  $('headerSubtitle').textContent = t('header_msgs', {n: ch.messages.length}) + extra;
}

const GROUP_WINDOW_MS = 5 * 60 * 1000;
let mutes = {};

function buildMsgRow(m, prevAuthor, prevTime) {
  const row = document.createElement('div');
  row.className = 'msg-row';
  row.dataset.id = m.id; row.dataset.t = String(m.t*1000);
  row.dataset.author = m.from;
  const d = new Date(m.t*1000);
  const timeStr = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const grouped = (prevAuthor === m.from) && ((m.t*1000) - prevTime < GROUP_WINDOW_MS);
  if (grouped) row.classList.add('grouped');

  const color = colorForUser(m.from);

  const authEl = document.createElement('span');
  authEl.className = 'msg-author' + (grouped ? ' hidden' : '');
  authEl.textContent = m.from;
  authEl.style.color = color;

  const contentEl = document.createElement('span');
  contentEl.className = 'msg-content';
  const textEl = document.createElement('span'); textEl.className = 'msg-text'; textEl.textContent = '…';
  const timeEl = document.createElement('span'); timeEl.className = 'msg-time'; timeEl.textContent = timeStr;
  contentEl.appendChild(textEl); contentEl.appendChild(timeEl);

  row.appendChild(authEl); row.appendChild(contentEl);
  row.setAttribute('data-ct', m.ct || '');
  return { el: row, textEl };
}

function applyDecryption(row, channelId, ct) {
  const span = row.querySelector('.msg-text');
  decryptText(channelId, ct).then(pt => {
    decryptedCache[row.dataset.id] = pt;
    span.innerHTML = renderRichText(pt, channelId);
    if (textMentionsMe(pt)) row.classList.add('mentioned-me');
  });
}

function renderMessages() {
  const feed = $('chatFeed'); feed.innerHTML = '';
  const ch = channels.find(c => c.id === activeId);
  if (!ch) { feed.innerHTML = '<div class="empty-state">'+SVG.chat+t('empty_no_channels')+'</div>'; return; }
  if (ch.messages.length === 0) { feed.innerHTML = '<div class="empty-state">'+SVG.chat+t('empty_no_messages')+'</div>'; return; }
  let lastAuthor = null, lastTime = 0;
  ch.messages.forEach(m => {
    const { el } = buildMsgRow(m, lastAuthor, lastTime);
    feed.appendChild(el);
    lastAuthor = m.from; lastTime = m.t*1000;
  });
  const chId = activeId;
  feed.querySelectorAll('.msg-row[data-ct]').forEach(row => {
    const ct = row.getAttribute('data-ct');
    if (!ct) return;
    applyDecryption(row, chId, ct);
  });
  feed.scrollTop = feed.scrollHeight;
}

function appendMessageUI(msg, channelId) {
  const feed = $('chatFeed');
  const existing = feed.querySelector('.msg-row[data-id="'+CSS.escape(msg.id)+'"]');
  const ch = channels.find(c => c.id === channelId);
  if (ch) {
    if (!ch.messages.find(mm => mm.id === msg.id)) {
      ch.messages.push(msg);
      if (ch.messages.length > 300) ch.messages = ch.messages.slice(-300);
    }
  }
  if (existing) {
    if (msg.ct) {
      existing.setAttribute('data-ct', msg.ct);
      applyDecryption(existing, channelId, msg.ct);
    }
    return;
  }
  if (activeId !== channelId) return;
  const empty = feed.querySelector('.empty-state'); if (empty) empty.remove();
  const lastRow = feed.querySelector('.msg-row:last-child');
  const prevAuthor = lastRow ? (lastRow.querySelector('.msg-author').textContent || '') : null;
  const prevTime = lastRow ? parseInt(lastRow.dataset.t || '0', 10) : 0;
  const { el, textEl } = buildMsgRow(msg, prevAuthor, prevTime);
  feed.appendChild(el);
  if (msg.ct) {
    applyDecryption(el, channelId, msg.ct);
  } else {
    textEl.textContent = '[sending...]';
  }
  feed.scrollTop = feed.scrollHeight;
}

function addSystem(text) {
  const feed = $('chatFeed');
  const empty = feed.querySelector('.empty-state'); if (empty) empty.remove();
  const row = document.createElement('div');
  row.className = 'msg-row msg-system';
  row.innerHTML = '<span class="sys-icon">'+SVG.ban+'</span><span>'+escapeHtml(text)+'</span>';
  feed.appendChild(row); feed.scrollTop = feed.scrollHeight;
}
function updateMuteUI() {
  const input = $('msgInput'), sendBtn = $('sendBtn');
  const ch = channels.find(c => c.id === activeId);
  if (!ch) { input.disabled = true; input.placeholder = t('composer_no_channel'); sendBtn.disabled = true; return; }
  const rem = Math.max(0, Math.ceil(((mutes[ch.id]||0) - Date.now())/1000));
  if (rem > 0) { input.disabled = true; input.placeholder = t('composer_muted', {n: rem}); sendBtn.disabled = true; }
  else { input.disabled = false; input.placeholder = t('composer_ph'); sendBtn.disabled = false; }
}
setInterval(updateMuteUI, 1000);

function openChannelWS(channelId) {
  if (ws && wsChannelId === channelId && ws.readyState === WebSocket.OPEN) return;
  if (ws) { try { ws.close(); } catch(e){} ws = null; }
  wsChannelId = channelId; onlineUsers = [];
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onopen = () => ws.send(JSON.stringify({ type:'auth', token: authToken, channel_id: channelId }));
  ws.onmessage = ev => {
    let data; try { data = JSON.parse(ev.data); } catch(e){ return; }
    if (data.type === 'message' && data.msg) { appendMessageUI(data.msg, channelId); renderHeader(); }
    else if (data.type === 'presence') { onlineUsers = data.users || []; renderHeader(); renderMembersSidebar(); }
    else if (data.type === 'muted') { mutes[channelId] = Date.now() + data.seconds*1000; updateMuteUI(); addSystem('Muted for ' + data.seconds + 's'); }
    else if (data.type === 'error' && data.error === 'auth') { $('settingsLogoutBtn').click(); }
  };
  ws.onclose = () => { if (wsChannelId === channelId) ws = null; };
}

function sendMessage() {
  const input = $('msgInput');
  const text = input.value.trim();
  if (!text) return;
  const chId = activeId;
  if (!chId || !ws || ws.readyState !== WebSocket.OPEN) return;
  if ((mutes[chId]||0) > Date.now()) { updateMuteUI(); return; }
  if (!channelKeys[chId]) { addSystem('No encryption key'); return; }
  const mid = uuid();
  const tNow = Date.now()/1000;
  encryptText(chId, text).then(ct => {
    const optimistic = { id: mid, from: currentUser, ct: ct, t: tNow };
    appendMessageUI(optimistic, chId);
    ws.send(JSON.stringify({ type:'message', id: mid, ciphertext: ct }));
    input.value = ''; autoResize(); hideMentionPop();
    try { input.focus(); } catch(e) {}
  });
}
$('sendBtn').addEventListener('click', sendMessage);
$('sendBtn').addEventListener('mousedown', e => e.preventDefault());
$('sendBtn').addEventListener('touchstart', e => {}, {passive: true});

$('msgInput').addEventListener('keydown', e => {
  if (mentionState.open) {
    if (e.key === 'ArrowDown') { e.preventDefault(); mentionState.selected = Math.min(mentionState.items.length-1, mentionState.selected+1); renderMentionPop(); return; }
    if (e.key === 'ArrowUp')   { e.preventDefault(); mentionState.selected = Math.max(0, mentionState.selected-1); renderMentionPop(); return; }
    if (e.key === 'Tab' || e.key === 'Enter') { e.preventDefault(); pickMention(mentionState.selected); return; }
    if (e.key === 'Escape') { e.preventDefault(); hideMentionPop(); return; }
  }
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
function autoResize() { const el = $('msgInput'); el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 120) + 'px'; }
$('msgInput').addEventListener('input', () => { autoResize(); updateMentionState(); });

// Mentions
function currentMention() {
  const ta = $('msgInput');
  const v = ta.value, pos = ta.selectionStart;
  let i = pos - 1;
  while (i >= 0) {
    const c = v[i];
    if (c === '@') {
      const prev = i > 0 ? v[i-1] : ' ';
      if (/\s|^/.test(prev)) {
        const word = v.slice(i+1, pos);
        if (/^[^\s@:<>"'&]{0,32}$/.test(word)) return { start: i, end: pos, word };
      }
      return null;
    }
    if (/\s/.test(c)) return null;
    if (pos - i > 33) return null;
    i--;
  }
  return null;
}
function updateMentionState() {
  const cur = currentMention();
  if (!cur || !activeId) { hideMentionPop(); return; }
  const members = channelMembers[activeId] || [];
  const q = cur.word.toLowerCase();
  const items = members
    .filter(m => m.username.toLowerCase() !== (currentUser||'').toLowerCase())
    .filter(m => !q || m.username.toLowerCase().startsWith(q))
    .slice(0, 8);
  if (!items.length) { hideMentionPop(); return; }
  mentionState.open = true;
  mentionState.items = items;
  mentionState.startIdx = cur.start;
  mentionState.selected = 0;
  renderMentionPop();
}
function renderMentionPop() {
  const pop = $('mentionPop'); if (!pop) return;
  pop.innerHTML = '';
  if (!mentionState.open) { pop.classList.remove('open'); return; }
  mentionState.items.forEach((m, idx) => {
    const item = document.createElement('div');
    item.className = 'mention-pop-item' + (idx === mentionState.selected ? ' active' : '');
    item.innerHTML = '<span class="mp-name" style="color:'+colorForUser(m.username)+';">@'+escapeHtml(m.username)+'</span>';
    item.addEventListener('mousedown', e => { e.preventDefault(); pickMention(idx); });
    pop.appendChild(item);
  });
  pop.classList.add('open');
}
function hideMentionPop() {
  mentionState.open = false;
  const pop = $('mentionPop'); if (pop) { pop.classList.remove('open'); pop.innerHTML = ''; }
}
function pickMention(idx) {
  if (!mentionState.open) return;
  const m = mentionState.items[idx]; if (!m) return;
  const ta = $('msgInput');
  const v = ta.value;
  const before = v.slice(0, mentionState.startIdx);
  const after = v.slice(mentionState.startIdx).replace(/^@[^\s@:<>"'&]{0,32}/, '');
  const insertion = '@' + m.username + ' ';
  ta.value = before + insertion + after;
  const newPos = (before + insertion).length;
  ta.setSelectionRange(newPos, newPos);
  hideMentionPop(); autoResize(); ta.focus();
}

// Message context menu
function showMsgMenu(rowEl, x, y) {
  hideMsgMenu();
  const msgId = rowEl.dataset.id;
  const author = rowEl.dataset.author || (rowEl.querySelector('.msg-author') ? rowEl.querySelector('.msg-author').textContent.trim() : '');
  const text = decryptedCache[msgId] || '';

  const back = document.createElement('div');
  back.className = 'msg-menu-backdrop';
  back.addEventListener('mousedown', hideMsgMenu);
  back.addEventListener('touchstart', hideMsgMenu, {passive: true});
  back.addEventListener('contextmenu', e => { e.preventDefault(); hideMsgMenu(); });
  document.body.appendChild(back);

  const menu = document.createElement('div');
  menu.className = 'msg-menu';

  const mw = 220, mh = 110;
  let mx = Math.min(x, window.innerWidth - mw - 8);
  let my = Math.min(y, window.innerHeight - mh - 8);
  mx = Math.max(8, mx); my = Math.max(8, my);
  menu.style.left = mx + 'px';
  menu.style.top = my + 'px';

  const mentionItem = document.createElement('div');
  mentionItem.className = 'msg-menu-item';
  mentionItem.innerHTML = SVG.at + '<span>' + escapeHtml(t('msg_menu_mention') + ' @' + author) + '</span>';
  mentionItem.addEventListener('click', e => {
    e.stopPropagation();
    const ta = $('msgInput');
    const prefix = ta.value && !/\s$/.test(ta.value) ? ' ' : '';
    ta.value = ta.value + prefix + '@' + author + ' ';
    ta.focus();
    hideMsgMenu();
  });
  menu.appendChild(mentionItem);

  const copyItem = document.createElement('div');
  copyItem.className = 'msg-menu-item';
  copyItem.innerHTML = SVG.copy + '<span>' + escapeHtml(t('msg_menu_copy')) + '</span>';
  copyItem.addEventListener('click', async e => {
    e.stopPropagation();
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      addSystem(t('msg_copied'));
    } catch(err) { addSystem(t('msg_copy_failed')); }
    hideMsgMenu();
  });
  menu.appendChild(copyItem);

  document.body.appendChild(menu);
  msgMenuEl = { menu, back };
}
function hideMsgMenu() {
  if (msgMenuEl) {
    try { msgMenuEl.menu.remove(); } catch(e){}
    try { msgMenuEl.back.remove(); } catch(e){}
    msgMenuEl = null;
  }
}

document.addEventListener('contextmenu', e => {
  const row = e.target.closest && e.target.closest('.msg-row[data-id]');
  if (row && !row.classList.contains('msg-system')) {
    e.preventDefault();
    showMsgMenu(row, e.clientX, e.clientY);
    return;
  }
  e.preventDefault();
});

(function() {
  let timer = null, sx = 0, sy = 0, fired = false;
  document.addEventListener('touchstart', e => {
    if (e.touches.length !== 1) return;
    const row = e.target.closest && e.target.closest('.msg-row[data-id]');
    if (!row || row.classList.contains('msg-system')) return;
    sx = e.touches[0].clientX; sy = e.touches[0].clientY;
    fired = false;
    timer = setTimeout(() => {
      fired = true;
      if (navigator.vibrate) try { navigator.vibrate(15); } catch(_) {}
      showMsgMenu(row, sx, sy);
    }, 500);
  }, {passive: true});
  document.addEventListener('touchmove', e => {
    if (!timer) return;
    const t = e.touches[0];
    if (Math.abs(t.clientX - sx) > 10 || Math.abs(t.clientY - sy) > 10) {
      clearTimeout(timer); timer = null;
    }
  }, {passive: true});
  document.addEventListener('touchend', e => {
    if (timer) { clearTimeout(timer); timer = null; }
    if (fired) { e.preventDefault(); fired = false; }
  }, {passive: false});
  document.addEventListener('touchcancel', () => {
    if (timer) { clearTimeout(timer); timer = null; }
  }, {passive: true});
})();

// External link modal
let pendingExtUrl = null;
function showExtLinkModal(url) {
  pendingExtUrl = url;
  $('extLinkUrl').textContent = url;
  $('extLinkBackdrop').classList.add('open');
}
function closeExtLinkModal() {
  pendingExtUrl = null;
  $('extLinkBackdrop').classList.remove('open');
}
$('extLinkBack').addEventListener('click', closeExtLinkModal);
$('extLinkGo').addEventListener('click', () => {
  const url = pendingExtUrl;
  closeExtLinkModal();
  if (url) { try { window.open(url, '_blank', 'noopener,noreferrer'); } catch(e) {} }
});

document.addEventListener('click', e => {
  const a = e.target.closest && e.target.closest('a.ext-link');
  if (!a) return;
  e.preventDefault();
  const url = a.getAttribute('data-url');
  if (url) showExtLinkModal(url);
});

document.addEventListener('click', e => {
  const el = e.target.closest && e.target.closest('[data-close-modal]');
  if (!el) return;
  const w = el.getAttribute('data-close-modal');
  if (w === 'create') $('createBackdrop').classList.remove('open');
  if (w === 'connect') $('connectBackdrop').classList.remove('open');
  if (w === 'settings') $('settingsBackdrop').classList.remove('open');
  if (w === 'extLink') closeExtLinkModal();
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  ['createBackdrop','connectBackdrop','settingsBackdrop','extLinkBackdrop'].forEach(id => $(id).classList.remove('open'));
  if ($('langMenu').classList.contains('open')) closeLangMenu();
  if ($('mobileChannelsPanel').classList.contains('open')) closeMobileChannels();
  if ($('chatMembers').classList.contains('open')) closeMembersSidebar();
  hideMentionPop(); hideMsgMenu();
});

$('addTabBtn').addEventListener('click', () => {
  $('newChannelName').value = '';
  $('createBackdrop').classList.add('open');
  setTimeout(() => $('newChannelName').focus(), 60);
});
$('createChannelBtn').addEventListener('click', async () => {
  const name = $('newChannelName').value.trim();
  if (!name) { $('newChannelName').focus(); return; }
  try {
    const res = await api('/api/channels', 'POST', { name });
    if (res.enc_key) await importChannelKey(res.id, res.enc_key);
    channels.push({ id: res.id, name: res.name, private: true, enc_key: res.enc_key, messages: [] });
    activeId = res.id;
    $('createBackdrop').classList.remove('open');
    renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
    saveCache();
  } catch(e) { showLoginError(e.status === 409 ? 'name_taken' : t('err_generic')); }
});
$('newChannelName').addEventListener('keydown', e => { if (e.key === 'Enter') $('createChannelBtn').click(); });

$('connectBtn').addEventListener('click', () => {
  $('connectChannelName').value = '';
  $('connectError').classList.remove('show');
  $('connectBackdrop').classList.add('open');
  setTimeout(() => $('connectChannelName').focus(), 60);
});
$('connectChannelBtn').addEventListener('click', async () => {
  const name = $('connectChannelName').value.trim();
  if (!name) { $('connectChannelName').focus(); return; }
  try {
    const res = await api('/api/channels/connect', 'POST', { name });
    if (res.enc_key) await importChannelKey(res.id, res.enc_key);
    if (!channels.find(c => c.id === res.id)) {
      channels.push({ id: res.id, name: res.name, private: res.private, enc_key: res.enc_key, messages: res.messages || [] });
    } else {
      const c = channels.find(c => c.id === res.id); c.messages = res.messages || [];
    }
    activeId = res.id;
    $('connectBackdrop').classList.remove('open');
    renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
    saveCache();
  } catch(e) {
    $('connectErrorText').textContent = e.status === 404 ? t('connect_not_found', {name}) : t('err_generic');
    $('connectError').classList.add('show');
  }
});
$('connectChannelName').addEventListener('keydown', e => { if (e.key === 'Enter') $('connectChannelBtn').click(); });
$('connectChannelName').addEventListener('input', () => $('connectError').classList.remove('show'));

function switchChannel(delta) {
  if (!channels.length) return;
  const idx = channels.findIndex(c => c.id === activeId);
  if (idx === -1) return;
  const n = Math.max(0, Math.min(channels.length - 1, idx + delta));
  if (n === idx) return;
  activeId = channels[n].id;
  renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
}
document.addEventListener('keydown', e => {
  if (document.querySelector('.my-modal-backdrop.open')) return;
  if ($('langMenu').classList.contains('open')) return;
  if ($('mobileChannelsPanel').classList.contains('open')) return;
  if ($('chatMembers').classList.contains('open')) return;
  if (document.documentElement.getAttribute('data-state') !== 'app') return;
  if (mentionState.open) return;
  if (msgMenuEl) return;
  const ae = document.activeElement;
  if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) return;
  if (e.key === 'ArrowLeft') { switchChannel(-1); e.preventDefault(); }
  else if (e.key === 'ArrowRight') { switchChannel(1); e.preventDefault(); }
});

let touchStartX = 0, touchStartY = 0, touchActive = false;
const feedEl = $('chatFeed');
feedEl.addEventListener('touchstart', e => {
  if (e.touches.length !== 1) return;
  touchStartX = e.touches[0].clientX; touchStartY = e.touches[0].clientY; touchActive = true;
}, {passive: true});
feedEl.addEventListener('touchend', e => {
  if (!touchActive) return;
  touchActive = false;
  const dx = e.changedTouches[0].clientX - touchStartX;
  const dy = e.changedTouches[0].clientY - touchStartY;
  if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.5) switchChannel(dx < 0 ? 1 : -1);
}, {passive: true});

function renderAll() {
  renderTabs(); renderHeader(); renderMessages(); updateMuteUI();
  scrollActiveTabIntoView(); renderMembersSidebar();
}

applyLanguage();
renderUptime();
(async () => {
  const ok = await tryRestoreSession();
  if (!ok) {
    setTimeout(() => $('loginName').focus(), 100);
  }
})();
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def index():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return HTMLResponse(
            "<h1 style='font-family:sans-serif'>sldchat</h1>"
            "<p>Missing env: <code>SUPABASE_URL</code> and <code>SUPABASE_SERVICE_KEY</code> must be set.</p>",
            status_code=500,
        )
    html = HTML_TEMPLATE
    html = html.replace("%%I18N%%", json.dumps(I18N, ensure_ascii=False))
    html = html.replace("%%FLAGS%%", json.dumps(FLAGS, ensure_ascii=False))
    html = html.replace("%%LANG_ORDER%%", json.dumps(LANG_ORDER))
    return HTMLResponse(html)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars")
    log(f"Starting sldchat on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1,
                log_level="info", access_log=False,
                limit_concurrency=200, timeout_keep_alive=30)
