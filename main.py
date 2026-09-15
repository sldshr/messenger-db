# ============================================================
#  SldClient — FastAPI + Supabase, single file
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

# ---------------- Limits / security ----------------
MAX_MESSAGE_LEN = 8000
MAX_USERNAME_LEN = 32
MIN_USERNAME_LEN = 2
MIN_PASSWORD_LEN = 4
MAX_PASSWORD_LEN = 128
PBKDF2_ITERATIONS = 200_000
RATE_LIMIT_WINDOW = 10.0
RATE_LIMIT_MAX = 20
MUTE_SECONDS = 30
TOKEN_TTL_DAYS = 7
MAX_BODY_SIZE = 64 * 1024
GLOBAL_RATE_WINDOW = 60.0
GLOBAL_RATE_MAX = 600
AUTH_RATE_WINDOW = 60.0
AUTH_RATE_MAX = 10
WS_PER_IP_MAX = 6
MESSAGE_FETCH_LIMIT = 300
USERNAME_RE = re.compile(r"^[^\s@:<>\"'&]{2,32}$")

TURNSTILE_SITEKEY = "0x4AAAAAAEt2kcFzE58AuS_r"
TURNSTILE_SECRET = "0x4AAAAAAEt2kX9fNPZNVSsCEur4myw93h4"
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
SKIP_TURNSTILE = os.environ.get("SKIP_TURNSTILE", "").lower() in ("1", "true", "yes")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or ""

# Per-process ephemeral state (not persisted — OK)
IP_RATE: Dict[str, List[float]] = {}
WS_PER_IP: Dict[str, int] = {}

def log(msg: str) -> None:
    try: print(f"[SldClient {time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception: pass

# ---------------- Password / tokens ----------------
def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()

def verify_password(password: str, salt_hex: str, expected: str) -> bool:
    try: salt = bytes.fromhex(salt_hex)
    except ValueError: return False
    return secrets.compare_digest(hash_password(password, salt), expected)

def new_token() -> str: return secrets.token_urlsafe(32)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_ts(s: str) -> float:
    if not s: return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0

# ---------------- Supabase REST client ----------------
def _sb_headers(prefer: Optional[str] = None) -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer: h["Prefer"] = prefer
    return h

async def sb_get(path: str, params: Optional[dict] = None) -> list:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase not configured")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=_sb_headers(), params=params or {})
        if r.status_code >= 400:
            raise RuntimeError(f"sb_get {path} → {r.status_code} {r.text[:200]}")
        return r.json()

async def sb_post(path: str, data, prefer: str = "return=representation") -> list:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=_sb_headers(prefer), json=data)
        if r.status_code >= 400:
            raise RuntimeError(f"sb_post {path} → {r.status_code} {r.text[:200]}")
        if r.status_code == 201 and r.text:
            try: return r.json()
            except Exception: return []
        return []

async def sb_patch(path: str, params: dict, data) -> list:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.patch(f"{SUPABASE_URL}/rest/v1/{path}", headers=_sb_headers("return=representation"),
                          params=params, json=data)
        if r.status_code >= 400:
            raise RuntimeError(f"sb_patch {path} → {r.status_code} {r.text[:200]}")
        try: return r.json()
        except Exception: return []

async def sb_delete(path: str, params: dict) -> bool:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(f"{SUPABASE_URL}/rest/v1/{path}", headers=_sb_headers(), params=params)
        return r.status_code < 400

async def get_user_by_token(token: Optional[str]) -> Optional[dict]:
    if not token: return None
    try:
        rows = await sb_get("tokens", {
            "select": "expires_at,users(id,username,avatar,show_in_list)",
            "token": f"eq.{token}",
            "limit": "1",
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
    return {"id": u["id"], "username": u["username"], "avatar": u.get("avatar", "?"),
            "show_in_list": u.get("show_in_list", True)}

async def auth(request: Request) -> dict:
    u = await get_user_by_token(request.headers.get("x-auth-token"))
    if not u: raise HTTPException(status_code=401, detail="unauthorized")
    return u

# ---------------- IP helpers / rate limit ----------------
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

# ---------------- Turnstile ----------------
def _verify_turnstile_sync(token: str, remote_ip: str) -> dict:
    try:
        body = urllib_encode({"secret": TURNSTILE_SECRET, "response": token, "remoteip": remote_ip})
        req = build_request(TURNSTILE_URL, body)
        with __import__("urllib.request", fromlist=["urlopen"]).urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"success": False, "error-codes": [f"exception: {e}"]}

def urllib_encode(d: dict) -> bytes:
    import urllib.parse
    return urllib.parse.urlencode(d).encode("utf-8")

def build_request(url: str, body: bytes):
    import urllib.request
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return req

async def verify_turnstile(token: str, remote_ip: str) -> tuple[bool, str]:
    if SKIP_TURNSTILE: return True, "skipped"
    if not token: return False, "no_token"
    result = await asyncio.to_thread(_verify_turnstile_sync, token, remote_ip)
    if result.get("success"): return True, "ok"
    return False, ",".join(str(c) for c in result.get("error-codes", ["unknown"]))

# ---------------- App ----------------
app = FastAPI(title="SldClient", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","POST"], allow_headers=["*"])

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://getbootstrap.com; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://challenges.cloudflare.com; "
    "frame-src https://challenges.cloudflare.com; "
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
    response.headers["Server"] = "SldClient"
    return response

# ---------------- Models ----------------
class AuthReq(BaseModel):
    username: str
    password: str
    avatar: str = "?"
    turnstile_token: str = ""

class CreateChannelReq(BaseModel):
    name: str

class ConnectChannelReq(BaseModel):
    name: str

class LeaveChannelReq(BaseModel):
    channel_id: str

class PrefsReq(BaseModel):
    show_in_list: bool

# ---------------- REST ----------------
@app.post("/api/auth")
async def auth_endpoint(req: AuthReq, request: Request):
    ip = get_client_ip(request)
    if not rate_check(f"auth:{ip}", AUTH_RATE_WINDOW, AUTH_RATE_MAX):
        raise HTTPException(429, "too_many_attempts")
    ok, reason = await verify_turnstile(req.turnstile_token, ip)
    if not ok:
        log(f"AUTH turnstile failed ip={ip} reason={reason}")
        raise HTTPException(400, "turnstile_failed")

    username = req.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "bad_username")
    if not (MIN_PASSWORD_LEN <= len(req.password) <= MAX_PASSWORD_LEN):
        raise HTTPException(400, "bad_password")

    try:
        rows = await sb_get("users", {
            "select": "id,username,password_hash,salt,avatar,show_in_list",
            "username": f"ilike.{username}",
            "limit": "1",
        })
    except Exception as e:
        log(f"sb_get users error: {e}")
        raise HTTPException(503, "db_error")

    if rows:
        u = rows[0]
        if not verify_password(req.password, u["salt"], u["password_hash"]):
            raise HTTPException(401, "bad_credentials")
        user = {"id": u["id"], "username": u["username"], "avatar": u.get("avatar", "?"),
                "show_in_list": u.get("show_in_list", True)}
        is_new = False
        log(f"AUTH login user={user['username']}")
    else:
        avatar = (req.avatar or "?").strip()[:2] or "?"
        salt = os.urandom(16)
        try:
            created = await sb_post("users", {
                "username": username,
                "password_hash": hash_password(req.password, salt),
                "salt": salt.hex(),
                "avatar": avatar,
                "show_in_list": True,
            })
        except Exception as e:
            log(f"register insert error: {e}")
            raise HTTPException(409, "user_exists")
        if not created:
            raise HTTPException(503, "db_error")
        row = created[0]
        user = {"id": row["id"], "username": row["username"],
                "avatar": row.get("avatar", avatar), "show_in_list": True}
        is_new = True
        log(f"AUTH register user={user['username']}")

    token = new_token()
    exp_iso = (datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)).isoformat()
    try:
        await sb_post("tokens", {"token": token, "user_id": user["id"], "expires_at": exp_iso},
                      prefer="return=minimal")
    except Exception as e:
        log(f"token insert error: {e}")
        raise HTTPException(503, "db_error")

    return {"token": token, "username": user["username"], "avatar": user["avatar"], "is_new": is_new}

@app.post("/api/logout")
async def logout(request: Request):
    token = request.headers.get("x-auth-token")
    if token:
        try: await sb_delete("tokens", {"token": f"eq.{token}"})
        except Exception: pass
    return {"ok": True}

@app.get("/api/uptime")
async def uptime():
    return {"uptime": time.time()-START_TIME, "name": "SldClient"}

@app.get("/api/me")
async def me(request: Request):
    u = await auth(request)
    return {"username": u["username"], "avatar": u["avatar"], "show_in_list": u["show_in_list"]}

@app.post("/api/me/preferences")
async def update_prefs(req: PrefsReq, request: Request):
    u = await auth(request)
    await sb_patch("users", {"id": f"eq.{u['id']}"}, {"show_in_list": bool(req.show_in_list)})
    return {"ok": True, "show_in_list": bool(req.show_in_list)}

@app.get("/api/users")
async def list_users(request: Request):
    me = await auth(request)
    online = manager.get_online_usernames()
    try:
        rows = await sb_get("users", {
            "select": "id,username,avatar,show_in_list",
            "order": "username.asc",
            "limit": "500",
        })
    except Exception:
        raise HTTPException(503, "db_error")
    out = []
    for r in rows:
        if r["id"] == me["id"]:
            out.append({"username": r["username"], "avatar": r.get("avatar", "?"),
                        "online": r["username"] in online, "self": True})
        elif r.get("show_in_list", True):
            out.append({"username": r["username"], "avatar": r.get("avatar", "?"),
                        "online": r["username"] in online, "self": False})
    return {"users": out, "online": len(online)}

def _channel_from_row(ch: dict, msgs: list) -> dict:
    out_msgs = []
    for m in msgs:
        u = m.get("users") or {}
        out_msgs.append({
            "id": m["id"],
            "from": u.get("username", "?"),
            "avatar": u.get("avatar", "?"),
            "ct": m["ciphertext"],
            "t": parse_ts(m["created_at"]),
        })
    return {"id": ch["id"], "name": ch["name"], "private": bool(ch.get("private", True)),
            "owner_id": ch.get("owner_id"), "messages": out_msgs}

async def _fetch_channel_messages(channel_id: str) -> list:
    rows = await sb_get("messages", {
        "select": "id,ciphertext,created_at,users(username,avatar)",
        "channel_id": f"eq.{channel_id}",
        "order": "created_at.desc",
        "limit": str(MESSAGE_FETCH_LIMIT),
    })
    rows.reverse()
    return rows

@app.get("/api/channels")
async def list_channels(request: Request):
    me = await auth(request)
    try:
        rows = await sb_get("channel_members", {
            "select": "channel_id,channels(id,name,private,owner_id,created_at)",
            "user_id": f"eq.{me['id']}",
        })
    except Exception:
        raise HTTPException(503, "db_error")
    out = []
    for r in rows:
        ch = r.get("channels")
        if not ch: continue
        try:
            msgs = await _fetch_channel_messages(ch["id"])
        except Exception:
            msgs = []
        out.append(_channel_from_row(ch, msgs))
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
    try:
        created = await sb_post("channels", {"name": name, "private": True, "owner_id": me["id"]})
        if not created: raise HTTPException(503, "db_error")
        ch = created[0]
        await sb_post("channel_members", {"channel_id": ch["id"], "user_id": me["id"]},
                      prefer="return=minimal")
    except HTTPException:
        raise
    except Exception as e:
        log(f"create_channel error: {e}")
        raise HTTPException(409, "name_taken")
    return {"id": ch["id"], "name": ch["name"], "private": True, "messages": []}

@app.post("/api/channels/connect")
async def connect_channel(req: ConnectChannelReq, request: Request):
    me = await auth(request)
    name = req.name.strip()
    if not name: raise HTTPException(400, "bad_name")
    try:
        rows = await sb_get("channels", {
            "select": "id,name,private,owner_id",
            "name": f"ilike.{name}",
            "limit": "1",
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
            "channel_id": f"eq.{channel_id}",
            "user_id": f"eq.{me['id']}",
            "limit": "1",
        })
    except Exception:
        raise HTTPException(503, "db_error")
    if not mine: raise HTTPException(403, "not_member")
    try:
        rows = await sb_get("channel_members", {
            "select": "users(username,avatar)",
            "channel_id": f"eq.{channel_id}",
            "order": "joined_at.asc",
            "limit": "500",
        })
    except Exception:
        raise HTTPException(503, "db_error")
    out = []
    for r in rows:
        u = r.get("users")
        if u: out.append({"username": u["username"], "avatar": u.get("avatar", "?")})
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

    def get_online_usernames(self) -> Set[str]:
        return set(u["username"] for (u, _) in self.info.values())

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
                "channel_id": f"eq.{channel_id}",
                "user_id": f"eq.{user['id']}",
                "limit": "1",
            })
        except Exception:
            mine = []
        if not mine:
            await ws.send_json({"type":"error","error":"auth"}); await ws.close(); return

        await manager.join(channel_id, ws, user)
        await manager.broadcast(channel_id,
            {"type":"presence","users":manager.online_users(channel_id)})

        while True:
            data = await ws.receive_json()
            t = data.get("type")
            if t == "message":
                ct = str(data.get("ciphertext",""))[:MAX_MESSAGE_LEN]
                if not ct: continue
                mid = str(data.get("id","")).strip()
                if not re.match(r"^[0-9a-fA-F-]{8,64}$", mid):
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
                    await ws.send_json({"type":"muted","seconds":MUTE_SECONDS})
                    continue
                times.append(now)

                # Broadcast IMMEDIATELY with client-generated id, persist in background
                msg = {
                    "id": mid,
                    "from": user["username"],
                    "avatar": user["avatar"],
                    "ct": ct,
                    "t": now,
                }
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
                await manager.broadcast(channel_id,
                    {"type":"presence","users":manager.online_users(channel_id)})
            except Exception:
                pass

# MSG_TIMES kept in-memory (rate-limit only)
MSG_TIMES: Dict[str, List[float]] = {}

# ---------------- i18n ----------------
I18N = {
 "en": {"login_title":"SldClient","login_subtitle":"Sign in or create an account","field_nick":"Nickname","field_password":"Password","field_avatar":"Avatar (1 char)","ph_nick":"Your nickname","ph_password":"Your password","ph_avatar":"A","avatar_hint":"Used for new accounts","btn_login":"Continue","remember_me":"Remember me","logged_as":"Signed in as","header_no_channels":"No channels","header_no_channels_sub":"Open Channels to create or join a channel","header_msgs":"{n} messages","empty_no_channels":"You have no channels yet.","empty_no_messages":"No messages. Be the first to write!","composer_ph":"Write a message...","composer_no_channel":"No active channel","composer_muted":"Muted: {n}s","modal_create_title":"Create private channel","modal_name":"Name","modal_name_ph":"E.g. Work","btn_cancel":"Cancel","btn_create":"Create","modal_connect_title":"Connect to channel","modal_connect_name":"Channel name","modal_connect_ph":"Enter the exact channel name","btn_connect":"Connect","connect_not_found":"Channel «{name}» not found","title_add":"Create channel","title_connect":"Connect to channel","title_settings":"Settings","theme_toggle":"Toggle theme","lang_toggle":"Change language","uptime_label":"Uptime","online_label":"online","err_bad_credentials":"Wrong password for this nickname","err_bad_username":"Nickname must be 2–32 characters, no spaces","err_bad_password":"Password must be at least 4 characters","err_generic":"Error","err_rate_limited":"Too many requests, please try again later","err_turnstile":"Security check failed. Please complete the checkbox above.","settings_title":"Settings","settings_account":"Account","settings_appearance":"Appearance","settings_users":"Users List","settings_show_in_list":"Show me in Users List","settings_user":"Signed in as","settings_logout":"Sign out","settings_theme":"Theme","settings_theme_light":"Light","settings_theme_dark":"Dark","settings_lang":"Language","settings_close":"Close","users_list_empty":"No users to show","users_you":"(you)","users_online":"online","mobile_channels":"Channels","mention_hint":"Type @ to mention"},
 "ru": {"login_title":"SldClient","login_subtitle":"Войдите или создайте аккаунт","field_nick":"Ник","field_password":"Пароль","field_avatar":"Аватар (1 символ)","ph_nick":"Ваш ник","ph_password":"Ваш пароль","ph_avatar":"А","avatar_hint":"Используется только для новых аккаунтов","btn_login":"Продолжить","remember_me":"Запомнить меня","logged_as":"Вы вошли как","header_no_channels":"Нет каналов","header_no_channels_sub":"Откройте «Каналы», чтобы создать или вступить","header_msgs":"{n} сообщений","empty_no_channels":"У вас пока нет каналов.","empty_no_messages":"Нет сообщений. Напишите первым!","composer_ph":"Написать сообщение...","composer_no_channel":"Нет активного канала","composer_muted":"Мут: {n} с","modal_create_title":"Создать приватный канал","modal_name":"Название","modal_name_ph":"Например, Работа","btn_cancel":"Отмена","btn_create":"Создать","modal_connect_title":"Подключиться к каналу","modal_connect_name":"Название канала","modal_connect_ph":"Введите точное название канала","btn_connect":"Подключиться","connect_not_found":"Канал «{name}» не найден","title_add":"Создать канал","title_connect":"Подключиться к каналу","title_settings":"Настройки","theme_toggle":"Сменить тему","lang_toggle":"Сменить язык","uptime_label":"Аптайм","online_label":"онлайн","err_bad_credentials":"Неверный пароль для этого ника","err_bad_username":"Ник 2–32 символа, без пробелов и @","err_bad_password":"Пароль минимум 4 символа","err_generic":"Ошибка","err_rate_limited":"Слишком много запросов, попробуйте позже","err_turnstile":"Проверка безопасности не пройдена. Отметьте галочку выше.","settings_title":"Настройки","settings_account":"Аккаунт","settings_appearance":"Оформление","settings_users":"Список пользователей","settings_show_in_list":"Показывать меня в списке","settings_user":"Вы вошли как","settings_logout":"Выйти из аккаунта","settings_theme":"Тема","settings_theme_light":"Светлая","settings_theme_dark":"Тёмная","settings_lang":"Язык","settings_close":"Закрыть","users_list_empty":"Нет пользователей","users_you":"(вы)","users_online":"онлайн","mobile_channels":"Каналы","mention_hint":"Введите @ чтобы упомянуть"},
}
# Fallbacks for other languages (fall back to EN via t())
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

# ============================================================
#  HTML
# ============================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0088cc">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, notranslate">
<meta name="referrer" content="no-referrer">
<title>SldClient</title>
<link rel="stylesheet" href="https://getbootstrap.com/1.4.0/assets/css/bootstrap.min.css">
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onTurnstileLoaded&render=explicit" async defer></script>
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
.settings-tab, .user-list-item, .mcp-item { touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
@media print { body { display: none !important; } }

/* Login */
.login-screen { position: fixed; top: 0; left: 0; right: 0; height: var(--app-vh, 100vh);
  background: #e9eef3; background-image: linear-gradient(#f5f8fb, #dfe6ee);
  display: flex; align-items: center; justify-content: center;
  z-index: 500; padding: 16px; box-sizing: border-box; overflow-y: auto; }
.login-box { width: 360px; max-width: 100%; background: #fff; border: 1px solid #b8c4d0;
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
.login-box .btn { width: 100%; margin-top: 6px; }
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
.avatar-row { display: flex; gap: 10px; align-items: flex-start; }
.avatar-row .avatar-input {
  flex: 0 0 auto; width: 52px; height: 52px; text-align: center; font-size: 22px;
  font-weight: bold; color: #fff; border: 1px solid #b8c4d0;
  background: #0088cc; padding: 0; box-sizing: border-box;
  outline: none; font-family: inherit; border-radius: 0;
}
.avatar-row .avatar-input:focus { border-color: #0088cc; box-shadow: 0 0 4px rgba(0,136,204,.5); }
.avatar-row .nick-wrap { flex: 1 1 auto; min-width: 0; }
.avatar-hint { font-size: 11px; color: #7b8a99; margin-top: 4px; }
#turnstileWidget { margin: 12px auto 6px; width: 100%; max-width: 320px; min-height: 72px;
  display: flex; align-items: center; justify-content: center; overflow: visible;
  position: relative; box-sizing: border-box; }
#turnstileWidget > div, #turnstileWidget iframe { margin: 0 auto !important; }
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
  display: flex; flex-direction: column; position: relative; overflow: hidden; }

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

.chat-header { display: flex; align-items: center; padding: 8px 12px;
  background: #f5f5f5; background-image: linear-gradient(#ffffff, #f0f0f0);
  border-bottom: 1px solid #ccc; flex-shrink: 0; gap: 8px; }
.chat-header .title { font-weight: bold; font-size: 14px; line-height: 1.1; color: #222; }
.chat-header .subtitle { font-size: 11px; color: #777; }
.chat-header .user-info { margin-left: auto; display: flex; align-items: center;
  gap: 8px; font-size: 11px; color: #666; }
.chat-header .user-info .nick { font-weight: bold; color: #2b3d51; }

.chat-feed { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch;
  padding: 8px 12px; background: #fdfdfd; position: relative; }
.empty-state { margin: auto; text-align: center; color: #aaa; font-size: 12px; padding-top: 60px; }
.empty-state svg { display: block; margin: 0 auto 10px; color: #ccc; }

/* 3-column message grid: avatar | nickname | content */
.msg-row { display: grid;
  grid-template-columns: 28px minmax(60px, 100px) 1fr;
  column-gap: 8px; row-gap: 0;
  padding: 3px 4px; align-items: start; font-size: 13px;
  line-height: 1.5; border-radius: 3px; transition: background .2s;
  word-wrap: break-word; }
.msg-row:hover { background: #f2f6fa; }
.msg-row.highlight { background: #fff3a8; }
.msg-row.grouped { padding-top: 0; }
.msg-avatar { width: 24px; height: 24px; border-radius: 4px;
  display: inline-flex; align-items: center; justify-content: center;
  color: #fff; font-weight: bold; font-size: 13px; line-height: 1;
  flex-shrink: 0; user-select: none; margin-top: 1px; }
.msg-avatar.hidden { visibility: hidden; }
.msg-author { font-weight: bold; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; margin-top: 1px; }
.msg-author.hidden { visibility: hidden; }
.msg-content { min-width: 0; word-break: break-word; overflow-wrap: anywhere; }
.msg-text { color: #222; }
.msg-time { color: #b0b8c0; font-size: 10.5px; margin-left: 6px; white-space: nowrap; }
.mention { background: #e1eefb; color: #005a9e; font-weight: bold;
  padding: 0 3px; border-radius: 3px; }
body.dark .mention { background: #1c3a5a; color: #8ac0ff; }
.msg-system { color: #a94442; background: #fcebeb; border: 1px solid #f5c6c6;
  font-size: 11.5px; padding: 4px 8px; margin: 4px 0; display: flex;
  align-items: center; border-radius: 3px; }

/* Mention autocomplete */
.mention-pop { position: absolute; z-index: 40;
  background: #fff; border: 1px solid #bbb; box-shadow: 0 4px 12px rgba(0,0,0,.2);
  max-height: 220px; overflow-y: auto; min-width: 180px; display: none; }
.mention-pop.open { display: block; }
.mention-pop-item { display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; cursor: pointer; font-size: 13px; color: #333; }
.mention-pop-item:hover, .mention-pop-item.active { background: #eaf4fb; }
.mention-pop-item .mp-avatar { width: 22px; height: 22px; border-radius: 3px;
  display: inline-flex; align-items: center; justify-content: center;
  color: #fff; font-weight: bold; font-size: 11px; flex-shrink: 0; }
.mention-pop-item .mp-name { font-weight: bold; }

.composer { display: flex; align-items: flex-end; gap: 6px; padding: 8px 10px;
  background: #f5f5f5; background-image: linear-gradient(#f0f0f0, #ffffff);
  border-top: 1px solid #ccc; flex-shrink: 0; position: relative; }
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

#settingsModal { width: 560px; }
.settings-body { display: flex; min-height: 280px; }
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
.settings-toggle-row { display: flex; align-items: flex-start; gap: 8px;
  padding: 8px 0; margin-top: 8px; font-size: 13px; color: #333;
  cursor: pointer; user-select: none; width: 100%; box-sizing: border-box; }
.settings-toggle-row input { flex: 0 0 auto; width: 16px; height: 16px; margin: 2px 0 0 0; padding: 0; }
.settings-toggle-row > span { flex: 1 1 auto; min-width: 0; line-height: 1.4; overflow-wrap: break-word; }

.users-list { display: flex; flex-direction: column; }
.user-list-item { display: flex; align-items: center; gap: 10px; padding: 8px 4px;
  border-bottom: 1px solid #f0f0f0; font-size: 13px; }
.user-list-item:last-child { border-bottom: 0; }
.user-dot { width: 10px; height: 10px; border-radius: 50%; background: #bbb;
  flex-shrink: 0; box-shadow: 0 0 0 2px rgba(0,0,0,.05); }
.user-dot.online { box-shadow: 0 0 0 2px rgba(76,175,80,.35); }
.user-list-item .user-avatar { width: 24px; height: 24px; border-radius: 4px;
  display: inline-flex; align-items: center; justify-content: center;
  color: #fff; font-weight: bold; font-size: 12px; flex-shrink: 0; }
.user-list-item .user-name { flex: 1; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; font-weight: bold; }
.user-list-item .user-you { font-size: 11px; color: #999; margin-left: 4px; font-weight: normal; }
.users-empty { padding: 30px 10px; text-align: center; color: #999; font-size: 12px; }

body.dark { background: #1a1a1a; color: #ccc; }
body.dark .login-screen { background: #1a1a1a; background-image: none; }
body.dark .login-box { background: #252526; border-color: #3c3c3c; }
body.dark .login-box h2 { color: #eaeaea; }
body.dark .login-box p { color: #888; }
body.dark .my-label { color: #888 !important; }
body.dark .my-input { background: #1e1e1e !important; color: #ddd !important; border-color: #4a4a4c !important; }
body.dark .avatar-input { border-color: #4a4a4c; }
body.dark .avatar-hint { color: #777; }
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
body.dark .settings-toggle-row { color: #ccc; }
body.dark .user-list-item { border-bottom-color: #3c3c3c; }
body.dark .user-list-item .user-name { color: #eaeaea; }
body.dark .mention-pop { background: #252526; border-color: #3c3c3c; color: #ddd; }
body.dark .mention-pop-item { color: #ddd; }
body.dark .mention-pop-item:hover, body.dark .mention-pop-item.active { background: #37373d; }
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
}

/* ============================================================
   MOBILE
   ============================================================ */
@media (max-width: 768px) {
  .login-screen { padding: 16px; align-items: flex-start; padding-top: 30px; padding-bottom: 40px; }
  .login-box { width: 100%; max-width: 420px; padding: 22px 18px 16px; }
  .login-box h2 { font-size: 20px; }
  .login-box p { font-size: 13px; }
  .login-box .btn { padding: 14px; font-size: 15px; }
  .my-input, .login-box .my-input, .my-modal .my-input { font-size: 16px !important; padding: 12px 12px !important; }
  .avatar-row .avatar-input { width: 58px; height: 58px; font-size: 26px; }
  .remember-row { font-size: 14px; }
  .remember-row input { width: 18px; height: 18px; margin-top: 2px; }
  .settings-btn { height: 40px; font-size: 14px; }
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
    border-radius: 0; min-height: 46px; }
  .mobile-channels-btn:active { background: #d0d0d0; background-image: none; }
  .mobile-channels-btn svg { width: 20px !important; height: 20px !important; flex-shrink: 0; }
  .mobile-settings-btn { width: 46px; height: 46px; padding: 0; flex-shrink: 0;
    display: inline-flex; align-items: center; justify-content: center; }
  .mobile-settings-btn svg { width: 20px !important; height: 20px !important; }

  .chat-header { padding: 10px 12px; }
  .chat-header .title { font-size: 15px; }
  .chat-header .subtitle { font-size: 12px; }
  .chat-header .user-info { display: none; }

  .chat-feed { padding: 8px; }
  .msg-row { grid-template-columns: 24px minmax(52px, 72px) 1fr;
    column-gap: 6px; padding: 3px 3px; font-size: 14.5px; }
  .msg-avatar { width: 22px; height: 22px; font-size: 12px; }
  .msg-author { font-size: 13.5px; }
  .msg-time { font-size: 11px; }

  .composer { padding: 8px 8px; gap: 6px; }
  .composer textarea { font-size: 16px !important; padding: 12px 12px !important;
    max-height: 120px; }
  .composer .icon-btn { width: 46px; height: 46px; }
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
    border-left: 0; border-bottom: 3px solid transparent; font-size: 12px; }
  .settings-tab.active { border-left-color: transparent; border-bottom-color: #0088cc; }
  body.dark .settings-tab.active { border-left-color: transparent; border-bottom-color: #0e639c; }
  .settings-content { padding: 14px; max-height: 55vh; }
  .settings-lang-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .settings-toggle-row { font-size: 14px; padding: 12px 0; }
  .settings-toggle-row input { width: 20px; height: 20px; }

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
  .mcp-close:active { color: #c00; }
  .mcp-list { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 4px 0; }
  .mcp-item { display: flex; align-items: center; gap: 10px;
    padding: 16px 16px; border-bottom: 1px solid #f0f0f0;
    cursor: pointer;
    font-family: "Courier New", Courier, monospace;
    font-size: 14.5px; color: #333;
    min-height: 54px; box-sizing: border-box; }
  .mcp-item:active { background: #eaf4fb; }
  .mcp-item.active { background: #d6e8f7; color: #004a80; font-weight: bold; }
  .mcp-item .lock-ico { display: inline-flex; align-items: center; flex-shrink: 0; }
  .mcp-item .mcp-name { flex: 1; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .mcp-empty { padding: 40px 16px; text-align: center;
    color: #999; font-size: 13px; }
  .mcp-actions { padding: 12px; border-top: 1px solid #ccc;
    background: #f7f7f7; flex-shrink: 0; display: flex; gap: 8px; }
  .mcp-actions .btn { flex: 1; padding: 14px 10px; font-size: 13px;
    border-radius: 0; min-height: 48px; }
}

@media (max-width: 400px) {
  .msg-row { grid-template-columns: 22px minmax(46px, 64px) 1fr; column-gap: 5px; }
  .msg-author { font-size: 12.5px; }
  .lang-menu { grid-template-columns: 1fr; }
  .settings-lang-grid { grid-template-columns: 1fr; }
}

@media (min-width: 769px) and (max-width: 1024px) {
  .lang-menu { width: 620px; }
  .my-modal { max-width: 500px; }
  #settingsModal { width: 600px; }
  .msg-row { grid-template-columns: 28px minmax(60px, 110px) 1fr; }
}
</style>
</head>
<body>

<div class="login-screen" id="loginScreen">
  <div class="login-box">
    <h2 id="loginTitle">SldClient</h2>
    <p id="loginSubtitle"></p>

    <div class="field-group">
      <div class="avatar-row">
        <input type="text" id="loginAvatar" class="avatar-input" maxlength="1" value="" autocomplete="off" spellcheck="false">
        <div class="nick-wrap">
          <label class="my-label" id="lblNick" for="loginName"></label>
          <input type="text" id="loginName" class="my-input" maxlength="32"
                 autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false">
          <div class="avatar-hint" id="avatarHint"></div>
        </div>
      </div>
    </div>

    <div class="field-group">
      <label class="my-label" id="lblPass" for="loginPass"></label>
      <input type="password" id="loginPass" class="my-input" maxlength="128" autocomplete="new-password">
    </div>

    <label class="remember-row">
      <input type="checkbox" id="rememberMe" checked>
      <span id="rememberLbl"></span>
    </label>

    <div id="turnstileWidget"></div>

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

<div class="app" id="app" style="display:none">
  <div class="mobile-topbar" id="mobileTopbar">
    <button class="mobile-channels-btn" id="mobileChannelsBtn" type="button">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="8" y1="6" x2="21" y2="6"/>
        <line x1="8" y1="12" x2="21" y2="12"/>
        <line x1="8" y1="18" x2="21" y2="18"/>
        <line x1="3" y1="6" x2="3.01" y2="6"/>
        <line x1="3" y1="12" x2="3.01" y2="12"/>
        <line x1="3" y1="18" x2="3.01" y2="18"/>
      </svg>
      <span id="mobileChannelsLbl">Channels</span>
    </button>
    <button class="icon-btn-tab mobile-settings-btn" id="mobileSettingsBtn" type="button" title="Settings">
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
    <button class="icon-btn-tab" id="addTabBtn" title="">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
    </button>
    <button class="icon-btn-tab" id="connectBtn" title="">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
        <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
      </svg>
    </button>
    <div class="top-sep"></div>
    <button class="icon-btn-tab" id="settingsBtn" title="">
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
  </div>

  <div class="chat-feed" id="chatFeed"></div>

  <div class="composer">
    <textarea id="msgInput" rows="1" autocomplete="off" autocapitalize="sentences" spellcheck="false"></textarea>
    <button class="btn primary icon-btn" id="sendBtn" type="button">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
    </button>
    <div class="mention-pop" id="mentionPop"></div>
  </div>
</div>

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
        <button class="settings-tab" data-tab="users" id="tabUsers"></button>
      </div>
      <div class="settings-content">
        <div class="settings-pane active" data-pane="account">
          <div class="settings-section-title" id="settingsAccountHead"></div>
          <div class="settings-row">
            <div style="display:flex; align-items:center; gap:10px;">
              <div class="user-avatar" id="settingsUserAvatar">?</div>
              <div>
                <div class="settings-label" id="settingsUserLabel"></div>
                <div class="settings-value" id="settingsUser">—</div>
              </div>
            </div>
          </div>
          <label class="settings-toggle-row">
            <input type="checkbox" id="showInListToggle">
            <span id="showInListLabel"></span>
          </label>
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
        <div class="settings-pane" data-pane="users">
          <div class="settings-section-title" id="settingsUsersHead">Users List</div>
          <div class="users-list" id="usersList"></div>
        </div>
      </div>
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
const TURNSTILE_SITEKEY = "0x4AAAAAAEt2kcFzE58AuS_r";

const SVG = {
  x:'<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  lock:'<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
  ban:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>',
  chat:'<svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  sun:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>',
  moon:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
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
let currentAvatar = '?';
let channels = [];
let activeId = null;
let ws = null;
let wsChannelId = null;
let channelKeyCache = {};
let onlineUsers = [];
let uptimeBase = 0, uptimeFetchAt = 0;
let showInListPref = true;
let turnstileWidgetId = null;
let channelMembers = {};          // channelId -> [{username, avatar}]
let memberSetByChannel = {};      // channelId -> Set(lower(username))
let mentionState = { open:false, items:[], selected:0, startIdx:-1 };

const $ = (id) => document.getElementById(id);
const t = (key, vars) => {
  const dict = I18N[currentLang] || {};
  let s = dict[key];
  if (s === undefined) s = I18N.en[key];
  if (s === undefined) s = key;
  if (vars) for (const k in vars) s = s.replace(new RegExp('\\{'+k+'\\}','g'), vars[k]);
  return s;
};

// Turnstile
function onTurnstileLoaded() { renderTurnstile(); }
window.onTurnstileLoaded = onTurnstileLoaded;
function renderTurnstile() {
  if (!window.turnstile) return;
  const el = $('turnstileWidget'); if (!el) return;
  if (turnstileWidgetId !== null) {
    try { window.turnstile.remove(turnstileWidgetId); } catch (e) {}
    turnstileWidgetId = null;
  }
  el.innerHTML = '';
  try {
    turnstileWidgetId = window.turnstile.render(el, {
      sitekey: TURNSTILE_SITEKEY,
      theme: currentTheme === 'dark' ? 'dark' : 'light',
      size: 'normal',
    });
  } catch (e) { window.__err && window.__err('Turnstile render error', e); }
}
function getTurnstileToken() {
  if (!window.turnstile || turnstileWidgetId === null) return '';
  try { return window.turnstile.getResponse(turnstileWidgetId) || ''; } catch (e) { return ''; }
}
function resetTurnstile() {
  if (!window.turnstile || turnstileWidgetId === null) return;
  try { window.turnstile.reset(turnstileWidgetId); } catch (e) {}
}

// Crypto
const enc = new TextEncoder();
const dec = new TextDecoder();
async function deriveChannelKey(id) {
  if (channelKeyCache[id]) return channelKeyCache[id];
  const baseKey = await crypto.subtle.importKey('raw', enc.encode('e2ee-v1:' + id), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt: enc.encode('messenger-fixed-salt-v1'), iterations: 120000, hash: 'SHA-256' },
    baseKey, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']
  );
  channelKeyCache[id] = key; return key;
}
function b64(buf){let s='';const bytes=new Uint8Array(buf);for(let i=0;i<bytes.length;i++)s+=String.fromCharCode(bytes[i]);return btoa(s);}
function ub64(str){const bin=atob(str);const bytes=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);return bytes;}
async function encryptText(id, text) {
  const key = await deriveChannelKey(id);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({name:'AES-GCM', iv}, key, enc.encode(text));
  return b64(iv) + '.' + b64(ct);
}
async function decryptText(id, payload) {
  try {
    const [ivB64, ctB64] = payload.split('.');
    if (!ivB64 || !ctB64) return payload;
    const key = await deriveChannelKey(id);
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
  $('themeIcon').innerHTML = currentTheme === 'dark' ? SVG.sun : SVG.moon;
  localStorage.setItem('theme', currentTheme);
  const tv = $('settingsThemeVal'); if (tv) tv.textContent = currentTheme === 'dark' ? t('settings_theme_dark') : t('settings_theme_light');
  const tt = $('settingsThemeToggle'); if (tt) tt.textContent = currentTheme === 'dark' ? t('settings_theme_light') : t('settings_theme_dark');
}
function toggleTheme() { currentTheme = currentTheme === 'dark' ? 'light' : 'dark'; applyTheme(); }

function applyLanguage() {
  localStorage.setItem('lang', currentLang);
  $('loginTitle').textContent = t('login_title');
  $('loginSubtitle').textContent = t('login_subtitle');
  $('lblNick').textContent = t('field_nick');
  $('lblPass').textContent = t('field_password');
  $('loginName').placeholder = t('ph_nick');
  $('loginPass').placeholder = t('ph_password');
  $('loginAvatar').placeholder = t('ph_avatar');
  $('loginAvatar').title = t('field_avatar');
  $('avatarHint').textContent = t('avatar_hint');
  $('rememberLbl').textContent = t('remember_me');
  $('loginBtn').textContent = t('btn_login');
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
  $('tabUsers').textContent = t('settings_users');
  $('settingsAccountHead').textContent = t('settings_account');
  $('settingsUserLabel').textContent = t('settings_user');
  $('settingsLogoutBtn').textContent = t('settings_logout');
  $('settingsAppearanceHead').textContent = t('settings_appearance');
  $('settingsThemeLabel').textContent = t('settings_theme');
  $('settingsLangLabel').textContent = t('settings_lang');
  $('showInListLabel').textContent = t('settings_show_in_list');
  $('settingsUsersHead').textContent = t('settings_users');
  $('mobileChannelsLbl').textContent = t('mobile_channels');
  $('mcpTitle').textContent = t('mobile_channels');
  $('mcpCreateBtn').textContent = t('modal_create_title');
  $('mcpConnectBtn').textContent = t('modal_connect_title');
  $('langFlag').innerHTML = FLAGS[currentLang] || '';
  $('langCode').textContent = currentLang.toUpperCase();
  applyTheme();
  buildLangMenu(); buildSettingsLangGrid();
  renderTabs(); renderHeader(); renderMessages(); updateMuteUI();
  renderUsersList(); renderMobileChannelList();
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

async function doAuth() {
  const username = $('loginName').value.trim();
  const password = $('loginPass').value;
  const avatar = ($('loginAvatar').value.trim() || '?').slice(0, 1);
  if (!username || !password) { showLoginError(t('err_bad_credentials')); return; }
  if (username.length < 2) { showLoginError(t('err_bad_username')); return; }
  if (/[\s@:<>"'&]/.test(username)) { showLoginError(t('err_bad_username')); return; }
  if (password.length < 4) { showLoginError(t('err_bad_password')); return; }
  const tsToken = getTurnstileToken();
  if (!tsToken) { showLoginError(t('err_turnstile')); return; }
  try {
    const res = await api('/api/auth', 'POST',
      { username, password, avatar, turnstile_token: tsToken }, false);
    authToken = res.token; currentUser = res.username; currentAvatar = res.avatar || '?';
    if ($('rememberMe').checked) {
      localStorage.setItem('auth_token', res.token);
      sessionStorage.removeItem('auth_token');
    } else {
      sessionStorage.setItem('auth_token', res.token);
      localStorage.removeItem('auth_token');
    }
    $('loginName').value = ''; $('loginPass').value = ''; $('loginAvatar').value = '';
    resetTurnstile(); enterApp();
  } catch (e) {
    let msg;
    if (e.detail === 'turnstile_failed') msg = t('err_turnstile');
    else if (e.detail === 'bad_username') msg = t('err_bad_username');
    else if (e.detail === 'bad_password') msg = t('err_bad_password');
    else if (e.detail === 'too_many_attempts' || e.status === 429) msg = t('err_rate_limited');
    else if (e.status === 401) msg = t('err_bad_credentials');
    else msg = t('err_generic');
    showLoginError(msg); resetTurnstile();
  }
}
$('loginBtn').addEventListener('click', doAuth);
$('loginName').addEventListener('keydown', e => { if (e.key === 'Enter') $('loginPass').focus(); });
$('loginPass').addEventListener('keydown', e => { if (e.key === 'Enter') doAuth(); });
$('loginAvatar').addEventListener('input', e => {
  const v = e.target.value.replace(/[\s@:<>"'&]/g, '');
  e.target.value = v.slice(0, 1);
});

function updateAvatarDisplays() {
  const av = $('settingsUserAvatar');
  if (av) {
    av.textContent = currentAvatar || '?';
    av.style.background = colorForUser(currentUser || '?');
  }
}

function enterApp() {
  $('loginScreen').style.display = 'none';
  $('app').style.display = 'flex';
  $('headerUser').textContent = currentUser;
  $('settingsUser').textContent = currentUser;
  updateAvatarDisplays();
  updateAppVH();
  loadChannels(); loadMyPrefs();
}

async function loadMyPrefs() {
  try {
    const res = await api('/api/me');
    showInListPref = !!res.show_in_list;
    currentAvatar = res.avatar || '?';
    $('showInListToggle').checked = showInListPref;
    updateAvatarDisplays();
  } catch (e) {}
}

async function tryRestoreSession() {
  const saved = localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');
  if (!saved) return false;
  authToken = saved;
  try {
    const res = await api('/api/me');
    currentUser = res.username; currentAvatar = res.avatar || '?';
    enterApp(); return true;
  } catch(e) {
    localStorage.removeItem('auth_token'); sessionStorage.removeItem('auth_token');
    authToken = null; return false;
  }
}

$('themeBtn').addEventListener('click', e => { e.stopPropagation(); toggleTheme(); });
$('langBtn').addEventListener('click', e => { e.stopPropagation();
  $('langMenu').classList.contains('open') ? closeLangMenu() : openLangMenu(); });
$('langBackdrop').addEventListener('click', closeLangMenu);

function openSettingsModal() {
  $('settingsBackdrop').classList.add('open');
  $('settingsUser').textContent = currentUser || '—';
  $('showInListToggle').checked = showInListPref;
  updateAvatarDisplays();
  const activeTab = document.querySelector('.settings-tab.active');
  if (activeTab && activeTab.dataset.tab === 'users') loadUsersList();
}
$('settingsBtn').addEventListener('click', openSettingsModal);
$('mobileSettingsBtn').addEventListener('click', openSettingsModal);
document.querySelectorAll('.settings-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const target = tab.dataset.tab;
    document.querySelectorAll('.settings-tab').forEach(x => x.classList.toggle('active', x === tab));
    document.querySelectorAll('.settings-pane').forEach(p => p.classList.toggle('active', p.dataset.pane === target));
    if (target === 'users') loadUsersList();
  });
});
$('settingsThemeToggle').addEventListener('click', toggleTheme);
$('showInListToggle').addEventListener('change', async e => {
  const val = e.target.checked;
  try { await api('/api/me/preferences', 'POST', { show_in_list: val }); showInListPref = val; }
  catch (err) { e.target.checked = !val; }
});
$('settingsLogoutBtn').addEventListener('click', async () => {
  try { await api('/api/logout', 'POST'); } catch(e){}
  authToken = null; currentUser = null; currentAvatar = '?';
  channels = []; activeId = null; channelKeyCache = {};
  channelMembers = {}; memberSetByChannel = {};
  if (ws) { try { ws.close(); } catch(e){} ws = null; wsChannelId = null; }
  localStorage.removeItem('auth_token'); sessionStorage.removeItem('auth_token');
  $('settingsBackdrop').classList.remove('open');
  closeMobileChannels();
  $('app').style.display = 'none';
  $('loginScreen').style.display = 'flex';
  closeLangMenu(); updateAppVH(); resetTurnstile();
});

let usersListCache = [];
async function loadUsersList() {
  const box = $('usersList'); if (!box) return;
  if (!authToken) { box.innerHTML = '<div class="users-empty">'+t('users_list_empty')+'</div>'; return; }
  box.innerHTML = '<div class="users-empty">…</div>';
  try {
    const res = await api('/api/users');
    usersListCache = res.users || [];
    renderUsersList();
  } catch (e) {
    if (e.status === 401) { box.innerHTML = '<div class="users-empty">'+t('users_list_empty')+'</div>'; return; }
    box.innerHTML = '<div class="users-empty">'+t('err_generic')+'</div>';
  }
}
function renderUsersList() {
  const box = $('usersList'); if (!box) return;
  if (!usersListCache.length) { box.innerHTML = '<div class="users-empty">'+t('users_list_empty')+'</div>'; return; }
  box.innerHTML = '';
  usersListCache.forEach(u => {
    const color = colorForUser(u.username);
    const row = document.createElement('div');
    row.className = 'user-list-item' + (u.self ? ' self' : '');
    row.innerHTML =
      '<span class="user-dot'+(u.online?' online':'')+'" style="background:'+(u.online?'#4caf50':color)+';"></span>' +
      '<span class="user-avatar" style="background:'+color+';">'+escapeHtml((u.avatar||'?').slice(0,1))+'</span>' +
      '<span class="user-name" style="color:'+color+';">'+escapeHtml(u.username)+
        (u.self ? '<span class="user-you"> '+t('users_you')+'</span>' : '')+
      '</span>';
    box.appendChild(row);
  });
}

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
  if (!channels.length) {
    box.innerHTML = '<div class="mcp-empty">'+t('empty_no_channels')+'</div>';
    return;
  }
  channels.forEach(ch => {
    const item = document.createElement('div');
    item.className = 'mcp-item' + (ch.id === activeId ? ' active' : '');
    if (ch.private) {
      const l = document.createElement('span');
      l.className = 'lock-ico'; l.innerHTML = SVG.lock;
      item.appendChild(l);
    }
    const n = document.createElement('span');
    n.className = 'mcp-name'; n.textContent = ch.name;
    item.appendChild(n);
    item.addEventListener('click', () => {
      activeId = ch.id;
      closeMobileChannels();
      renderAll();
      openChannelWS(activeId);
      loadChannelMembers(activeId);
    });
    box.appendChild(item);
  });
}
$('mobileChannelsBtn').addEventListener('click', openMobileChannels);
$('mcpClose').addEventListener('click', closeMobileChannels);
$('mobileChannelsBackdrop').addEventListener('click', closeMobileChannels);
$('mcpCreateBtn').addEventListener('click', () => {
  closeMobileChannels(); setTimeout(() => $('addTabBtn').click(), 60);
});
$('mcpConnectBtn').addEventListener('click', () => {
  closeMobileChannels(); setTimeout(() => $('connectBtn').click(), 60);
});

async function loadChannels() {
  try {
    const res = await api('/api/channels');
    channels = res.channels || [];
    if (activeId && !channels.find(c => c.id === activeId)) activeId = null;
    if (!activeId && channels.length) activeId = channels[0].id;
    renderAll();
    if (activeId) {
      openChannelWS(activeId);
      loadChannelMembers(activeId);
    }
  } catch(e) { if (e.status === 401) $('settingsLogoutBtn').click(); }
}

async function loadChannelMembers(channelId) {
  try {
    const res = await api('/api/channels/' + encodeURIComponent(channelId) + '/members');
    channelMembers[channelId] = res.members || [];
    const s = new Set();
    (res.members || []).forEach(m => s.add(String(m.username).toLowerCase()));
    memberSetByChannel[channelId] = s;
    renderMessages();
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
      const l = document.createElement('span');
      l.className = 'lock-ico'; l.innerHTML = SVG.lock;
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
  const id = closeEl.getAttribute('data-close');
  try { await api('/api/channels/leave', 'POST', { channel_id: id }); } catch(e){}
  channels = channels.filter(c => c.id !== id);
  delete channelMembers[id]; delete memberSetByChannel[id];
  if (activeId === id) {
    activeId = channels[0] ? channels[0].id : null;
    if (ws) { try { ws.close(); } catch(e){} ws = null; wsChannelId = null; }
    if (activeId) { openChannelWS(activeId); loadChannelMembers(activeId); }
  }
  renderAll();
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

function renderMentions(text, channelId) {
  const members = memberSetByChannel[channelId];
  const esc = escapeHtml(text);
  if (!members || !members.size) return esc;
  return esc.replace(/@([^\s@:<>"'&]{2,32})/gu, (full, name) => {
    if (members.has(name.toLowerCase())) {
      return '<span class="mention">@'+name+'</span>';
    }
    return full;
  });
}

function buildMsgRow(m, prevAuthor, prevTime, channelId) {
  const row = document.createElement('div');
  row.className = 'msg-row';
  row.dataset.id = m.id; row.dataset.t = String(m.t*1000);
  const d = new Date(m.t*1000);
  const timeStr = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const grouped = (prevAuthor === m.from) && ((m.t*1000) - prevTime < GROUP_WINDOW_MS);
  if (grouped) row.classList.add('grouped');

  const color = colorForUser(m.from);

  const avEl = document.createElement('span');
  avEl.className = 'msg-avatar' + (grouped ? ' hidden' : '');
  avEl.style.background = color;
  avEl.textContent = (m.avatar || (m.from[0] || '?')).slice(0,1);

  const authEl = document.createElement('span');
  authEl.className = 'msg-author' + (grouped ? ' hidden' : '');
  authEl.textContent = m.from;
  authEl.style.color = color;

  const contentEl = document.createElement('span');
  contentEl.className = 'msg-content';
  const textEl = document.createElement('span'); textEl.className = 'msg-text'; textEl.textContent = '…';
  const timeEl = document.createElement('span'); timeEl.className = 'msg-time'; timeEl.textContent = timeStr;
  contentEl.appendChild(textEl); contentEl.appendChild(timeEl);

  row.appendChild(avEl); row.appendChild(authEl); row.appendChild(contentEl);
  row.setAttribute('data-ct', m.ct);
  return { el: row, textEl };
}

function renderMessages() {
  const feed = $('chatFeed'); feed.innerHTML = '';
  const ch = channels.find(c => c.id === activeId);
  if (!ch) { feed.innerHTML = '<div class="empty-state">'+SVG.chat+t('empty_no_channels')+'</div>'; return; }
  if (ch.messages.length === 0) { feed.innerHTML = '<div class="empty-state">'+SVG.chat+t('empty_no_messages')+'</div>'; return; }
  let lastAuthor = null, lastTime = 0;
  ch.messages.forEach(m => {
    const { el } = buildMsgRow(m, lastAuthor, lastTime, activeId);
    feed.appendChild(el);
    lastAuthor = m.from; lastTime = m.t*1000;
  });
  const chId = activeId;
  feed.querySelectorAll('.msg-row[data-ct]').forEach(row => {
    const span = row.querySelector('.msg-text');
    const ct = row.getAttribute('data-ct');
    decryptText(chId, ct).then(pt => { span.innerHTML = renderMentions(pt, chId); });
  });
  feed.scrollTop = feed.scrollHeight;
}

function appendMessageUI(msg, channelId, own) {
  const feed = $('chatFeed');
  const existing = feed.querySelector('.msg-row[data-id="'+CSS.escape(msg.id)+'"]');
  const ch = channels.find(c => c.id === channelId);
  if (ch) {
    if (!ch.messages.find(mm => mm.id === msg.id)) {
      ch.messages.push(msg);
      if (ch.messages.length > 300) ch.messages = ch.messages.slice(-300);
    }
  }
  if (existing) return;
  if (activeId !== channelId) return;
  const empty = feed.querySelector('.empty-state'); if (empty) empty.remove();
  const lastRow = feed.querySelector('.msg-row:last-child');
  const prevAuthor = lastRow ? (lastRow.querySelector('.msg-author').textContent || '') : null;
  const prevTime = lastRow ? parseInt(lastRow.dataset.t || '0', 10) : 0;
  const { el, textEl } = buildMsgRow(msg, prevAuthor, prevTime, channelId);
  feed.appendChild(el);
  decryptText(channelId, msg.ct).then(pt => { textEl.innerHTML = renderMentions(pt, channelId); });
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
    else if (data.type === 'presence') { onlineUsers = data.users || []; renderHeader(); }
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
  const mid = uuid();
  const tNow = Date.now()/1000;
  // optimistic local append (own flag not needed — server echo will dedupe)
  const optimistic = { id: mid, from: currentUser, avatar: currentAvatar, ct: '', t: tNow };
  encryptText(chId, text).then(ct => {
    optimistic.ct = ct;
    appendMessageUI(optimistic, chId);
    ws.send(JSON.stringify({ type:'message', id: mid, ciphertext: ct }));
    input.value = ''; autoResize(); hideMentionPop();
  });
}
$('sendBtn').addEventListener('click', sendMessage);
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

// ---------- Mentions autocomplete ----------
function currentMention() {
  const ta = $('msgInput');
  const v = ta.value;
  const pos = ta.selectionStart;
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
    item.innerHTML = '<span class="mp-avatar" style="background:'+colorForUser(m.username)+';">'+
      escapeHtml((m.avatar||'?').slice(0,1)) + '</span>' +
      '<span class="mp-name" style="color:'+colorForUser(m.username)+';">@'+escapeHtml(m.username)+'</span>';
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
  hideMentionPop();
  autoResize();
  ta.focus();
}

// Modals
document.addEventListener('click', e => {
  const el = e.target.closest && e.target.closest('[data-close-modal]');
  if (!el) return;
  const w = el.getAttribute('data-close-modal');
  if (w === 'create') $('createBackdrop').classList.remove('open');
  if (w === 'connect') $('connectBackdrop').classList.remove('open');
  if (w === 'settings') $('settingsBackdrop').classList.remove('open');
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  ['createBackdrop','connectBackdrop','settingsBackdrop'].forEach(id => $(id).classList.remove('open'));
  if ($('langMenu').classList.contains('open')) closeLangMenu();
  if ($('mobileChannelsPanel').classList.contains('open')) closeMobileChannels();
  hideMentionPop();
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
    channels.push({ id: res.id, name: res.name, private: true, messages: [] });
    activeId = res.id;
    $('createBackdrop').classList.remove('open');
    renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
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
    if (!channels.find(c => c.id === res.id)) {
      channels.push({ id: res.id, name: res.name, private: res.private, messages: res.messages || [] });
    } else {
      const c = channels.find(c => c.id === res.id); c.messages = res.messages || [];
    }
    activeId = res.id;
    $('connectBackdrop').classList.remove('open');
    renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
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
  if ($('loginScreen').style.display !== 'none') return;
  if (mentionState.open) return;
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

function renderAll() { renderTabs(); renderHeader(); renderMessages(); updateMuteUI(); scrollActiveTabIntoView(); }

applyLanguage();
renderUptime();
(async () => {
  const ok = await tryRestoreSession();
  if (!ok) {
    setTimeout(() => $('loginName').focus(), 100);
    let tries = 0;
    const iv = setInterval(() => {
      tries++;
      if (window.turnstile && $('turnstileWidget').children.length === 0) renderTurnstile();
      if ((window.turnstile && turnstileWidgetId !== null) || tries > 20) clearInterval(iv);
    }, 300);
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
            "<h1 style='font-family:sans-serif'>SldClient</h1>"
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
        log("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables")
    log(f"Starting SldClient on port {port}")
    log(f"Turnstile {'DISABLED' if SKIP_TURNSTILE else 'enabled'}")
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1,
                log_level="info", access_log=False,
                limit_concurrency=200, timeout_keep_alive=30)
