# ============================================================
#  sldchat — self-hostable E2EE messenger (FastAPI + WebSocket)
#
#  Запуск:
#      pip install fastapi "uvicorn[standard]" pydantic
#      python sldchat.py
#      Открыть http://localhost:8000
#
#  Без внешних сервисов. Всё в оперативке. Одна команда.
# ============================================================
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

START_TIME = time.time()
APP_NAME = "sldchat"

# ---------------- Limits ----------------
MAX_USERS = 5000
MAX_CHANNELS = 5000
MAX_MESSAGES_PER_CHANNEL = 500
MAX_CT_LEN = 8000
MIN_USERNAME_LEN = 2
MAX_USERNAME_LEN = 32
MIN_PASSWORD_LEN = 4
MAX_PASSWORD_LEN = 128
PBKDF2_ITERATIONS = 200_000
RATE_LIMIT_WINDOW = 10.0
RATE_LIMIT_MAX = 25
MUTE_SECONDS = 20
TOKEN_TTL = 60 * 60 * 24 * 7
MAX_BODY_SIZE = 32 * 1024
GLOBAL_RATE_WINDOW = 60.0
GLOBAL_RATE_MAX = 800
AUTH_RATE_WINDOW = 60.0
AUTH_RATE_MAX = 12
WS_PER_IP_MAX = 8
USERNAME_RE = re.compile(r"^[^\s@:<>\"'&]{2,32}$")
CHANNEL_NAME_RE = re.compile(r"^[^\s@:<>\"'&/\\]{1,40}$")
MSG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

USERS: Dict[str, dict] = {}
TOKENS: Dict[str, dict] = {}
CHANNELS: Dict[str, dict] = {}
CHANNEL_MEMBERS: Dict[str, Set[str]] = {}
USER_CHANNELS: Dict[str, Set[str]] = {}
MSG_TIMES: Dict[str, List[float]] = {}
IP_RATE: Dict[str, List[float]] = {}
WS_PER_IP: Dict[str, int] = {}

def log(msg: str) -> None:
    try: print(f"[{APP_NAME} {time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception: pass

# ---------------- Crypto ----------------
def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()

def verify_password(password: str, salt_hex: str, expected: str) -> bool:
    try: salt = bytes.fromhex(salt_hex)
    except ValueError: return False
    return secrets.compare_digest(hash_password(password, salt), expected)

def new_token() -> str: return secrets.token_urlsafe(32)

def user_from_token(token: Optional[str]) -> Optional[str]:
    if not token: return None
    e = TOKENS.get(token)
    if not e: return None
    if e["exp"] < time.time():
        TOKENS.pop(token, None); return None
    return e["username"]

async def auth(request: Request) -> str:
    u = user_from_token(request.headers.get("x-auth-token"))
    if not u: raise HTTPException(401, "unauthorized")
    return u

# ---------------- IP / rate ----------------
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
    for tt in lst:
        if tt >= cutoff: break
        i += 1
    if i: del lst[:i]
    if len(lst) >= limit: return False
    lst.append(now); return True

# ---------------- App ----------------
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","POST"], allow_headers=["*"])

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://getbootstrap.com; "
    "img-src 'self' data:; font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
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
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=()"
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Server"] = APP_NAME
    return resp

class AuthReq(BaseModel):
    username: str
    password: str

class CreateChannelReq(BaseModel):
    name: str

class LookupChannelReq(BaseModel):
    name: str

class JoinChannelReq(BaseModel):
    channel_id: str

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
    username = req.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "bad_username")
    if not (MIN_PASSWORD_LEN <= len(req.password) <= MAX_PASSWORD_LEN):
        raise HTTPException(400, "bad_password")
    key = username.lower()
    existing = USERS.get(key)
    if existing:
        if not verify_password(req.password, existing["salt"], existing["pwd"]):
            raise HTTPException(401, "bad_credentials")
        is_new = False
    else:
        if len(USERS) >= MAX_USERS: raise HTTPException(503, "server_full")
        salt = os.urandom(16)
        USERS[key] = {
            "username": username,
            "pwd": hash_password(req.password, salt),
            "salt": salt.hex(),
            "created": time.time(),
            "show_in_list": True,
        }
        USER_CHANNELS[key] = set()
        is_new = True
    token = new_token()
    TOKENS[token] = {"username": USERS[key]["username"], "exp": time.time() + TOKEN_TTL}
    return {"token": token, "username": USERS[key]["username"], "is_new": is_new}

@app.post("/api/logout")
async def logout(request: Request):
    t = request.headers.get("x-auth-token")
    if t: TOKENS.pop(t, None)
    return {"ok": True}

@app.get("/api/uptime")
async def uptime():
    return {"uptime": time.time()-START_TIME, "name": APP_NAME,
            "users": len(USERS), "channels": len(CHANNELS), "online": len(TOKENS)}

@app.get("/api/me")
async def me(request: Request):
    u = await auth(request)
    data = USERS[u.lower()]
    return {"username": data["username"], "show_in_list": data.get("show_in_list", True)}

@app.post("/api/me/preferences")
async def update_prefs(req: PrefsReq, request: Request):
    u = await auth(request)
    USERS[u.lower()]["show_in_list"] = bool(req.show_in_list)
    return {"ok": True, "show_in_list": bool(req.show_in_list)}

@app.get("/api/users")
async def list_users(request: Request):
    me = await auth(request)
    online = manager.get_online_usernames()
    out = []
    for key, data in USERS.items():
        uname = data["username"]
        self_ = (uname == me)
        if self_ or data.get("show_in_list", True):
            out.append({"username": uname, "online": uname in online, "self": self_})
    out.sort(key=lambda x: x["username"].lower())
    return {"users": out, "online": len(online)}

def _channel_out(ch: dict, messages: List[dict]) -> dict:
    return {"id": ch["id"], "name": ch["name"], "private": True,
            "salt": ch["salt"], "messages": messages}

@app.get("/api/channels")
async def list_channels(request: Request):
    me = await auth(request)
    cids = USER_CHANNELS.get(me.lower(), set())
    out = []
    for cid in cids:
        ch = CHANNELS.get(cid)
        if not ch: continue
        out.append(_channel_out(ch, ch["messages"][-MAX_MESSAGES_PER_CHANNEL:]))
    out.sort(key=lambda c: c["name"].lower())
    return {"channels": out}

@app.post("/api/channels")
async def create_channel(req: CreateChannelReq, request: Request):
    me = await auth(request)
    name = req.name.strip()[:40]
    if not CHANNEL_NAME_RE.match(name): raise HTTPException(400, "bad_name")
    for ch in CHANNELS.values():
        if ch["name"].lower() == name.lower(): raise HTTPException(409, "name_taken")
    if len(CHANNELS) >= MAX_CHANNELS: raise HTTPException(503, "server_full")
    cid = secrets.token_hex(8)
    salt_b64 = base64.b64encode(os.urandom(16)).decode("ascii")
    CHANNELS[cid] = {"id": cid, "name": name, "private": True, "owner": me,
                     "salt": salt_b64, "created": time.time(), "messages": []}
    CHANNEL_MEMBERS[cid] = {me.lower()}
    USER_CHANNELS.setdefault(me.lower(), set()).add(cid)
    return {"id": cid, "name": name, "salt": salt_b64, "private": True, "messages": []}

@app.post("/api/channels/lookup")
async def lookup_channel(req: LookupChannelReq, request: Request):
    await auth(request)
    name = req.name.strip()
    if not name: raise HTTPException(400, "bad_name")
    for ch in CHANNELS.values():
        if ch["name"].lower() == name.lower():
            return {"id": ch["id"], "name": ch["name"], "salt": ch["salt"], "private": True}
    raise HTTPException(404, "not_found")

@app.post("/api/channels/join")
async def join_channel(req: JoinChannelReq, request: Request):
    me = await auth(request)
    cid = req.channel_id
    ch = CHANNELS.get(cid)
    if not ch: raise HTTPException(404, "not_found")
    CHANNEL_MEMBERS.setdefault(cid, set()).add(me.lower())
    USER_CHANNELS.setdefault(me.lower(), set()).add(cid)
    return _channel_out(ch, ch["messages"][-MAX_MESSAGES_PER_CHANNEL:])

@app.post("/api/channels/leave")
async def leave_channel(req: LeaveChannelReq, request: Request):
    me = await auth(request)
    cid = req.channel_id
    s = USER_CHANNELS.get(me.lower())
    if s is not None: s.discard(cid)
    CHANNEL_MEMBERS.get(cid, set()).discard(me.lower())
    return {"ok": True}

@app.get("/api/channels/{channel_id}/members")
async def channel_members(channel_id: str, request: Request):
    me = await auth(request)
    if me.lower() not in CHANNEL_MEMBERS.get(channel_id, set()):
        raise HTTPException(403, "not_member")
    out = []
    for lname in sorted(CHANNEL_MEMBERS.get(channel_id, set())):
        u = USERS.get(lname)
        if u: out.append({"username": u["username"]})
    return {"members": out}

# ---------------- WebSocket ----------------
class WSManager:
    def __init__(self):
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self.info: Dict[WebSocket, tuple] = {}

    async def join(self, cid, ws, username):
        self.rooms.setdefault(cid, set()).add(ws)
        self.info[ws] = (username, cid)

    def leave(self, ws):
        e = self.info.pop(ws, None)
        if e:
            _, cid = e
            self.rooms.get(cid, set()).discard(ws)

    def get_online_usernames(self) -> Set[str]:
        return set(u for (u, _) in self.info.values())

    def online_users(self, cid) -> List[str]:
        return sorted(set(u for (u, c) in self.info.values() if c == cid))

    async def broadcast(self, cid, payload: dict):
        sockets = list(self.rooms.get(cid, set()))
        if not sockets: return
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        results = await asyncio.gather(*(ws.send_text(text) for ws in sockets),
                                       return_exceptions=True)
        for ws, res in zip(sockets, results):
            if isinstance(res, Exception): self.leave(ws)

manager = WSManager()

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ip = get_ws_ip(ws)
    if WS_PER_IP.get(ip, 0) >= WS_PER_IP_MAX:
        try: await ws.close(code=1008)
        except Exception: pass
        return
    WS_PER_IP[ip] = WS_PER_IP.get(ip, 0) + 1

    username = None; channel_id = None
    try:
        init = await ws.receive_json()
        if init.get("t") != "a":
            await ws.close(); return
        token = init.get("tok", "")
        channel_id = init.get("ch", "")
        username = user_from_token(token)
        if not username or channel_id not in CHANNELS \
                or username.lower() not in CHANNEL_MEMBERS.get(channel_id, set()):
            await ws.send_json({"t":"e","e":"auth"}); await ws.close(); return
        await manager.join(channel_id, ws, username)
        await manager.broadcast(channel_id, {"t":"p","u":manager.online_users(channel_id)})

        while True:
            data = await ws.receive_json()
            t = data.get("t")
            if t == "m":
                ct = str(data.get("c",""))[:MAX_CT_LEN]
                if not ct: continue
                mid = str(data.get("i","")).strip()
                if not MSG_ID_RE.match(mid):
                    mid = secrets.token_urlsafe(12)
                key = f"{username.lower()}|{channel_id}"
                now = time.time()
                times = MSG_TIMES.setdefault(key, [])
                cutoff = now - RATE_LIMIT_WINDOW; i = 0
                for tt in times:
                    if tt >= cutoff: break
                    i += 1
                if i: del times[:i]
                if len(times) >= RATE_LIMIT_MAX:
                    await ws.send_json({"t":"mu","s":MUTE_SECONDS})
                    continue
                times.append(now)
                msg = {"i": mid, "u": username, "c": ct, "ts": now}
                ch = CHANNELS[channel_id]
                ch["messages"].append(msg)
                if len(ch["messages"]) > MAX_MESSAGES_PER_CHANNEL:
                    del ch["messages"][:-MAX_MESSAGES_PER_CHANNEL]
                await manager.broadcast(channel_id, {"t":"m", "m": msg})
            elif t == "ping":
                try: await ws.send_json({"t":"pong"})
                except Exception: pass
    except WebSocketDisconnect: pass
    except Exception: pass
    finally:
        manager.leave(ws)
        WS_PER_IP[ip] = max(0, WS_PER_IP.get(ip, 1) - 1)
        if channel_id:
            try:
                await manager.broadcast(channel_id,
                    {"t":"p","u":manager.online_users(channel_id)})
            except Exception: pass

# ============================================================
#  40+ LANGUAGES
# ============================================================

# Full translation template for a "core" language
def _L(**kw):
    """Build language dict from keyword args; missing keys fall back to EN."""
    return kw

# ---------- English is the ultimate fallback for every key ----------
EN = {
  "login_title":"sldchat","login_subtitle":"Sign in or create an account",
  "field_nick":"Nickname","field_password":"Password",
  "ph_nick":"Your nickname","ph_password":"Your password",
  "btn_login":"Continue","btn_wait":"Please wait…","remember_me":"Remember me",
  "logged_as":"Signed in as","header_no_channels":"No channels",
  "header_no_channels_sub":"Open Channels to create or join a channel",
  "header_msgs":"{n} messages","empty_no_channels":"You have no channels yet.",
  "empty_no_messages":"No messages. Be the first to write!",
  "composer_ph":"Write a message...","composer_no_channel":"No active channel",
  "composer_muted":"Muted: {n}s",
  "modal_create_title":"Create private channel","modal_name":"Name",
  "modal_name_ph":"E.g. work","modal_pass":"Channel passphrase",
  "modal_pass_ph":"Needed to read messages",
  "modal_pass_hint":"Only people who know this passphrase can decrypt messages. Server never sees it.",
  "btn_cancel":"Cancel","btn_create":"Create",
  "modal_connect_title":"Join channel","modal_connect_name":"Channel name",
  "modal_connect_ph":"Enter the exact channel name",
  "btn_lookup":"Find","btn_join":"Join",
  "connect_not_found":"Channel «{name}» not found",
  "title_add":"Create channel","title_join":"Join channel","title_settings":"Settings",
  "theme_toggle":"Toggle theme","lang_toggle":"Change language",
  "uptime_label":"Uptime","online_label":"online",
  "err_bad_credentials":"Wrong password for this nickname",
  "err_bad_username":"Nickname must be 2–32 characters",
  "err_bad_password":"Password must be at least 4 characters",
  "err_bad_passphrase":"Passphrase required (min 1 char)",
  "err_pass_wrong":"Cannot decrypt — wrong passphrase?",
  "err_generic":"Error","err_rate_limited":"Too many requests, please try again later",
  "settings_title":"Settings","settings_account":"Account",
  "settings_appearance":"Appearance","settings_users":"Users List",
  "settings_show_in_list":"Show me in Users List","settings_user":"Signed in as",
  "settings_logout":"Sign out","settings_theme":"Theme",
  "settings_theme_light":"Light","settings_theme_dark":"Dark",
  "settings_lang":"Language","settings_close":"Close",
  "users_list_empty":"No users to show","users_you":"(you)","users_online":"online",
  "mobile_channels":"Channels","mention_hint":"Type @ to mention",
  "leave_channel":"Leave",
  "lang_picker_title":"Language",
}

I18N = {}
I18N["en"] = EN

# ---------- Russian ----------
I18N["ru"] = {
  "login_title":"sldchat","login_subtitle":"Войдите или создайте аккаунт",
  "field_nick":"Ник","field_password":"Пароль",
  "ph_nick":"Ваш ник","ph_password":"Ваш пароль",
  "btn_login":"Продолжить","btn_wait":"Пожалуйста, подождите…",
  "remember_me":"Запомнить меня","logged_as":"Вы вошли как",
  "header_no_channels":"Нет каналов",
  "header_no_channels_sub":"Откройте «Каналы», чтобы создать или вступить",
  "header_msgs":"{n} сообщений","empty_no_channels":"У вас пока нет каналов.",
  "empty_no_messages":"Нет сообщений. Напишите первым!",
  "composer_ph":"Написать сообщение...","composer_no_channel":"Нет активного канала",
  "composer_muted":"Мут: {n} с",
  "modal_create_title":"Создать приватный канал","modal_name":"Название",
  "modal_name_ph":"Например, работа","modal_pass":"Пароль канала",
  "modal_pass_ph":"Нужен для чтения сообщений",
  "modal_pass_hint":"Только те, кто знает пароль, смогут расшифровать. Сервер его не видит.",
  "btn_cancel":"Отмена","btn_create":"Создать",
  "modal_connect_title":"Войти в канал","modal_connect_name":"Название канала",
  "modal_connect_ph":"Введите точное название",
  "btn_lookup":"Найти","btn_join":"Войти",
  "connect_not_found":"Канал «{name}» не найден",
  "title_add":"Создать канал","title_join":"Войти в канал","title_settings":"Настройки",
  "theme_toggle":"Сменить тему","lang_toggle":"Сменить язык",
  "uptime_label":"Аптайм","online_label":"онлайн",
  "err_bad_credentials":"Неверный пароль для этого ника",
  "err_bad_username":"Ник 2–32 символа","err_bad_password":"Пароль минимум 4 символа",
  "err_bad_passphrase":"Введите пароль канала (минимум 1 символ)",
  "err_pass_wrong":"Не удалось расшифровать — неверный пароль?",
  "err_generic":"Ошибка","err_rate_limited":"Слишком много запросов, попробуйте позже",
  "settings_title":"Настройки","settings_account":"Аккаунт",
  "settings_appearance":"Оформление","settings_users":"Список пользователей",
  "settings_show_in_list":"Показывать меня в списке","settings_user":"Вы вошли как",
  "settings_logout":"Выйти из аккаунта","settings_theme":"Тема",
  "settings_theme_light":"Светлая","settings_theme_dark":"Тёмная",
  "settings_lang":"Язык","settings_close":"Закрыть",
  "users_list_empty":"Нет пользователей","users_you":"(вы)","users_online":"онлайн",
  "mobile_channels":"Каналы","mention_hint":"Введите @ чтобы упомянуть",
  "leave_channel":"Покинуть",
  "lang_picker_title":"Язык",
}

# ---------- Short core translations ----------
# Only the most visible keys; anything else falls back to English.

I18N["es"] = _L(login_title="sldchat", login_subtitle="Inicia sesión o crea una cuenta",
  field_nick="Apodo", field_password="Contraseña", ph_nick="Tu apodo", ph_password="Tu contraseña",
  btn_login="Continuar", btn_wait="Por favor espera…", remember_me="Recuérdame",
  logged_as="Sesión como", header_no_channels="Sin canales",
  header_no_channels_sub="Abre Canales para crear o unirte", header_msgs="{n} mensajes",
  empty_no_channels="Aún no tienes canales.", empty_no_messages="Sin mensajes. ¡Sé el primero!",
  composer_ph="Escribe un mensaje...", composer_no_channel="Sin canal activo", composer_muted="Silenciado: {n}s",
  modal_create_title="Crear canal privado", modal_name="Nombre", modal_name_ph="Ej.: trabajo",
  modal_pass="Contraseña del canal", modal_pass_ph="Necesaria para leer",
  modal_pass_hint="Solo quienes sepan la contraseña podrán descifrar. El servidor no la ve.",
  btn_cancel="Cancelar", btn_create="Crear", modal_connect_title="Unirse al canal",
  modal_connect_name="Nombre del canal", modal_connect_ph="Escribe el nombre exacto",
  btn_lookup="Buscar", btn_join="Unirse", connect_not_found="Canal «{name}» no encontrado",
  title_add="Crear canal", title_join="Unirse", title_settings="Ajustes", theme_toggle="Cambiar tema",
  lang_toggle="Cambiar idioma", uptime_label="Tiempo activo", online_label="en línea",
  err_bad_credentials="Contraseña incorrecta", err_bad_username="Apodo 2–32 caracteres",
  err_bad_password="Contraseña mín. 4", err_bad_passphrase="Introduce la contraseña del canal",
  err_pass_wrong="No se puede descifrar — ¿contraseña incorrecta?", err_generic="Error",
  err_rate_limited="Demasiadas solicitudes", settings_title="Ajustes", settings_account="Cuenta",
  settings_appearance="Apariencia", settings_users="Usuarios", settings_show_in_list="Mostrarme en la lista",
  settings_user="Sesión como", settings_logout="Cerrar sesión", settings_theme="Tema",
  settings_theme_light="Claro", settings_theme_dark="Oscuro", settings_lang="Idioma",
  settings_close="Cerrar", users_list_empty="Sin usuarios", users_you="(tú)", users_online="en línea",
  mobile_channels="Canales", mention_hint="Escribe @ para mencionar", leave_channel="Salir",
  lang_picker_title="Idioma")

I18N["de"] = _L(login_title="sldchat", login_subtitle="Anmelden oder Konto erstellen",
  field_nick="Spitzname", field_password="Passwort", ph_nick="Dein Spitzname", ph_password="Dein Passwort",
  btn_login="Weiter", btn_wait="Bitte warten…", remember_me="Angemeldet bleiben",
  logged_as="Angemeldet als", header_no_channels="Keine Kanäle",
  header_no_channels_sub="Öffne Kanäle zum Erstellen oder Beitreten", header_msgs="{n} Nachrichten",
  empty_no_channels="Du hast noch keine Kanäle.", empty_no_messages="Keine Nachrichten. Schreib zuerst!",
  composer_ph="Nachricht schreiben...", composer_no_channel="Kein aktiver Kanal", composer_muted="Stumm: {n}s",
  modal_create_title="Privaten Kanal erstellen", modal_name="Name", modal_name_ph="Z.B. arbeit",
  modal_pass="Kanal-Passwort", modal_pass_ph="Zum Lesen erforderlich",
  modal_pass_hint="Nur wer das Passwort kennt, kann entschlüsseln. Server sieht es nie.",
  btn_cancel="Abbrechen", btn_create="Erstellen", modal_connect_title="Kanal beitreten",
  modal_connect_name="Kanalname", modal_connect_ph="Exakten Namen eingeben",
  btn_lookup="Suchen", btn_join="Beitreten", connect_not_found="Kanal «{name}» nicht gefunden",
  title_add="Kanal erstellen", title_join="Beitreten", title_settings="Einstellungen",
  theme_toggle="Design wechseln", lang_toggle="Sprache ändern", uptime_label="Laufzeit",
  online_label="online", err_bad_credentials="Falsches Passwort", err_bad_username="Spitzname 2–32 Zeichen",
  err_bad_password="Passwort mind. 4", err_bad_passphrase="Kanal-Passwort eingeben",
  err_pass_wrong="Entschlüsselung fehlgeschlagen", err_generic="Fehler",
  err_rate_limited="Zu viele Anfragen", settings_title="Einstellungen", settings_account="Konto",
  settings_appearance="Aussehen", settings_users="Benutzer", settings_show_in_list="In Liste anzeigen",
  settings_user="Angemeldet als", settings_logout="Abmelden", settings_theme="Design",
  settings_theme_light="Hell", settings_theme_dark="Dunkel", settings_lang="Sprache",
  settings_close="Schließen", users_list_empty="Keine Benutzer", users_you="(du)", users_online="online",
  mobile_channels="Kanäle", mention_hint="@ eingeben zum Erwähnen", leave_channel="Verlassen",
  lang_picker_title="Sprache")

I18N["fr"] = _L(login_title="sldchat", login_subtitle="Connectez-vous ou créez un compte",
  field_nick="Pseudo", field_password="Mot de passe", ph_nick="Votre pseudo", ph_password="Votre mot de passe",
  btn_login="Continuer", btn_wait="Veuillez patienter…", remember_me="Se souvenir de moi",
  logged_as="Connecté en tant que", header_no_channels="Aucun canal",
  header_no_channels_sub="Ouvrez Canaux pour créer ou rejoindre", header_msgs="{n} messages",
  empty_no_channels="Aucun canal.", empty_no_messages="Aucun message. Soyez le premier !",
  composer_ph="Écrire un message...", composer_no_channel="Aucun canal actif", composer_muted="Muet : {n}s",
  modal_create_title="Créer un canal privé", modal_name="Nom", modal_name_ph="Ex : travail",
  modal_pass="Mot de passe du canal", modal_pass_ph="Requis pour lire",
  modal_pass_hint="Seuls ceux qui connaissent le mot de passe peuvent déchiffrer. Le serveur ne le voit pas.",
  btn_cancel="Annuler", btn_create="Créer", modal_connect_title="Rejoindre un canal",
  modal_connect_name="Nom du canal", modal_connect_ph="Entrez le nom exact",
  btn_lookup="Chercher", btn_join="Rejoindre", connect_not_found="Canal «{name}» introuvable",
  title_add="Créer un canal", title_join="Rejoindre", title_settings="Paramètres",
  theme_toggle="Changer de thème", lang_toggle="Changer de langue", uptime_label="Durée",
  online_label="en ligne", err_bad_credentials="Mot de passe incorrect", err_bad_username="Pseudo 2–32 caractères",
  err_bad_password="Mot de passe min. 4", err_bad_passphrase="Mot de passe du canal requis",
  err_pass_wrong="Déchiffrement impossible — mauvais mot de passe ?", err_generic="Erreur",
  err_rate_limited="Trop de requêtes", settings_title="Paramètres", settings_account="Compte",
  settings_appearance="Apparence", settings_users="Utilisateurs", settings_show_in_list="Me montrer dans la liste",
  settings_user="Connecté en tant que", settings_logout="Se déconnecter", settings_theme="Thème",
  settings_theme_light="Clair", settings_theme_dark="Sombre", settings_lang="Langue",
  settings_close="Fermer", users_list_empty="Aucun utilisateur", users_you="(vous)", users_online="en ligne",
  mobile_channels="Canaux", mention_hint="Tapez @ pour mentionner", leave_channel="Quitter",
  lang_picker_title="Langue")

I18N["it"] = _L(login_title="sldchat", login_subtitle="Accedi o crea un account",
  field_nick="Nickname", field_password="Password", ph_nick="Il tuo nickname", ph_password="La tua password",
  btn_login="Continua", btn_wait="Attendere…", remember_me="Ricordami",
  logged_as="Connesso come", header_no_channels="Nessun canale",
  header_no_channels_sub="Apri Canali per creare o unirti", header_msgs="{n} messaggi",
  empty_no_channels="Non hai canali.", empty_no_messages="Nessun messaggio. Scrivi per primo!",
  composer_ph="Scrivi un messaggio...", composer_no_channel="Nessun canale attivo", composer_muted="Silenziato: {n}s",
  modal_create_title="Crea canale privato", modal_name="Nome", modal_name_ph="Es: lavoro",
  modal_pass="Password del canale", modal_pass_ph="Necessaria per leggere",
  modal_pass_hint="Solo chi conosce la password può decifrare. Il server non la vede.",
  btn_cancel="Annulla", btn_create="Crea", modal_connect_title="Unisciti al canale",
  modal_connect_name="Nome canale", modal_connect_ph="Inserisci il nome esatto",
  btn_lookup="Cerca", btn_join="Unisciti", connect_not_found="Canale «{name}» non trovato",
  title_add="Crea canale", title_join="Unisciti", title_settings="Impostazioni",
  theme_toggle="Cambia tema", lang_toggle="Cambia lingua", uptime_label="Attività",
  online_label="online", err_bad_credentials="Password errata", err_bad_username="Nickname 2–32",
  err_bad_password="Password min. 4", err_bad_passphrase="Password del canale richiesta",
  err_pass_wrong="Impossibile decifrare — password errata?", err_generic="Errore",
  err_rate_limited="Troppe richieste", settings_title="Impostazioni", settings_account="Account",
  settings_appearance="Aspetto", settings_users="Utenti", settings_show_in_list="Mostrami nella lista",
  settings_user="Connesso come", settings_logout="Esci", settings_theme="Tema",
  settings_theme_light="Chiaro", settings_theme_dark="Scuro", settings_lang="Lingua",
  settings_close="Chiudi", users_list_empty="Nessun utente", users_you="(tu)", users_online="online",
  mobile_channels="Canali", mention_hint="Digita @ per menzionare", leave_channel="Esci",
  lang_picker_title="Lingua")

I18N["pt"] = _L(login_title="sldchat", login_subtitle="Entre ou crie uma conta",
  field_nick="Apelido", field_password="Senha", ph_nick="Seu apelido", ph_password="Sua senha",
  btn_login="Continuar", btn_wait="Aguarde…", remember_me="Lembrar-me",
  logged_as="Conectado como", header_no_channels="Sem canais",
  header_no_channels_sub="Abra Canais para criar ou entrar", header_msgs="{n} mensagens",
  empty_no_channels="Sem canais.", empty_no_messages="Sem mensagens. Seja o primeiro!",
  composer_ph="Escreva uma mensagem...", composer_no_channel="Sem canal ativo", composer_muted="Silenciado: {n}s",
  modal_create_title="Criar canal privado", modal_name="Nome", modal_name_ph="Ex: trabalho",
  modal_pass="Senha do canal", modal_pass_ph="Necessária para ler",
  modal_pass_hint="Só quem sabe a senha pode decifrar. O servidor não vê.",
  btn_cancel="Cancelar", btn_create="Criar", modal_connect_title="Entrar no canal",
  modal_connect_name="Nome do canal", modal_connect_ph="Digite o nome exato",
  btn_lookup="Procurar", btn_join="Entrar", connect_not_found="Canal «{name}» não encontrado",
  title_add="Criar canal", title_join="Entrar", title_settings="Configurações",
  theme_toggle="Alternar tema", lang_toggle="Alterar idioma", uptime_label="Tempo ativo",
  online_label="online", err_bad_credentials="Senha incorreta", err_bad_username="Apelido 2–32",
  err_bad_password="Senha mín. 4", err_bad_passphrase="Senha do canal necessária",
  err_pass_wrong="Não foi possível decifrar", err_generic="Erro", err_rate_limited="Muitas requisições",
  settings_title="Configurações", settings_account="Conta", settings_appearance="Aparência",
  settings_users="Usuários", settings_show_in_list="Mostrar-me na lista",
  settings_user="Conectado como", settings_logout="Sair", settings_theme="Tema",
  settings_theme_light="Claro", settings_theme_dark="Escuro", settings_lang="Idioma",
  settings_close="Fechar", users_list_empty="Sem usuários", users_you="(você)", users_online="online",
  mobile_channels="Canais", mention_hint="Digite @ para mencionar", leave_channel="Sair",
  lang_picker_title="Idioma")

I18N["nl"] = _L(login_title="sldchat", login_subtitle="Meld aan of maak een account",
  field_nick="Bijnaam", field_password="Wachtwoord", ph_nick="Je bijnaam", ph_password="Je wachtwoord",
  btn_login="Doorgaan", btn_wait="Even geduld…", remember_me="Onthoud mij",
  logged_as="Ingelogd als", header_no_channels="Geen kanalen",
  header_no_channels_sub="Open Kanalen om te maken of lid te worden", header_msgs="{n} berichten",
  empty_no_channels="Nog geen kanalen.", empty_no_messages="Geen berichten. Schrijf als eerste!",
  composer_ph="Bericht schrijven...", composer_no_channel="Geen actief kanaal", composer_muted="Gedempt: {n}s",
  modal_create_title="Privékanaal aanmaken", modal_name="Naam", modal_name_ph="Bv. werk",
  modal_pass="Kanaalwachtwoord", modal_pass_ph="Nodig om te lezen",
  modal_pass_hint="Alleen wie het wachtwoord kent kan ontsleutelen. Server ziet het niet.",
  btn_cancel="Annuleren", btn_create="Aanmaken", modal_connect_title="Lid worden van kanaal",
  modal_connect_name="Kanaalnaam", modal_connect_ph="Voer de exacte naam in",
  btn_lookup="Zoeken", btn_join="Deelnemen", connect_not_found="Kanaal «{name}» niet gevonden",
  title_add="Kanaal aanmaken", title_join="Deelnemen", title_settings="Instellingen",
  theme_toggle="Thema wisselen", lang_toggle="Taal wijzigen", uptime_label="Uptime",
  online_label="online", err_bad_credentials="Verkeerd wachtwoord", err_bad_username="Bijnaam 2–32",
  err_bad_password="Wachtwoord min. 4", err_bad_passphrase="Kanaalwachtwoord vereist",
  err_pass_wrong="Ontsleuteling mislukt", err_generic="Fout", err_rate_limited="Te veel verzoeken",
  settings_title="Instellingen", settings_account="Account", settings_appearance="Weergave",
  settings_users="Gebruikers", settings_show_in_list="Toon mij in de lijst",
  settings_user="Ingelogd als", settings_logout="Afmelden", settings_theme="Thema",
  settings_theme_light="Licht", settings_theme_dark="Donker", settings_lang="Taal",
  settings_close="Sluiten", users_list_empty="Geen gebruikers", users_you="(jij)", users_online="online",
  mobile_channels="Kanalen", mention_hint="Typ @ om te vermelden", leave_channel="Verlaten",
  lang_picker_title="Taal")

I18N["pl"] = _L(login_title="sldchat", login_subtitle="Zaloguj się lub utwórz konto",
  field_nick="Pseudonim", field_password="Hasło", ph_nick="Twój pseudonim", ph_password="Twoje hasło",
  btn_login="Kontynuuj", btn_wait="Proszę czekać…", remember_me="Zapamiętaj mnie",
  logged_as="Zalogowany jako", header_no_channels="Brak kanałów",
  header_no_channels_sub="Otwórz Kanały, aby utworzyć lub dołączyć", header_msgs="{n} wiadomości",
  empty_no_channels="Brak kanałów.", empty_no_messages="Brak wiadomości. Napisz pierwszy!",
  composer_ph="Napisz wiadomość...", composer_no_channel="Brak aktywnego kanału", composer_muted="Wyciszony: {n}s",
  modal_create_title="Utwórz prywatny kanał", modal_name="Nazwa", modal_name_ph="Np. praca",
  modal_pass="Hasło kanału", modal_pass_ph="Wymagane do czytania",
  modal_pass_hint="Tylko znający hasło mogą odszyfrować. Serwer go nie widzi.",
  btn_cancel="Anuluj", btn_create="Utwórz", modal_connect_title="Dołącz do kanału",
  modal_connect_name="Nazwa kanału", modal_connect_ph="Wpisz dokładną nazwę",
  btn_lookup="Szukaj", btn_join="Dołącz", connect_not_found="Kanał «{name}» nie znaleziony",
  title_add="Utwórz kanał", title_join="Dołącz", title_settings="Ustawienia",
  theme_toggle="Zmień motyw", lang_toggle="Zmień język", uptime_label="Czas działania",
  online_label="online", err_bad_credentials="Błędne hasło", err_bad_username="Pseudonim 2–32",
  err_bad_password="Hasło min. 4", err_bad_passphrase="Hasło kanału wymagane",
  err_pass_wrong="Nie można odszyfrować", err_generic="Błąd", err_rate_limited="Zbyt wiele żądań",
  settings_title="Ustawienia", settings_account="Konto", settings_appearance="Wygląd",
  settings_users="Użytkownicy", settings_show_in_list="Pokaż mnie na liście",
  settings_user="Zalogowany jako", settings_logout="Wyloguj", settings_theme="Motyw",
  settings_theme_light="Jasny", settings_theme_dark="Ciemny", settings_lang="Język",
  settings_close="Zamknij", users_list_empty="Brak użytkowników", users_you="(ty)", users_online="online",
  mobile_channels="Kanały", mention_hint="Wpisz @ aby wspomnieć", leave_channel="Opuść",
  lang_picker_title="Język")

I18N["uk"] = _L(login_title="sldchat", login_subtitle="Увійдіть або створіть акаунт",
  field_nick="Нік", field_password="Пароль", ph_nick="Ваш нік", ph_password="Ваш пароль",
  btn_login="Продовжити", btn_wait="Будь ласка, зачекайте…", remember_me="Запам'ятати мене",
  logged_as="Ви увійшли як", header_no_channels="Немає каналів",
  header_no_channels_sub="Відкрийте «Канали», щоб створити або приєднатися",
  header_msgs="{n} повідомлень", empty_no_channels="Поки що немає каналів.",
  empty_no_messages="Немає повідомлень. Напишіть першим!",
  composer_ph="Написати повідомлення...", composer_no_channel="Немає активного каналу",
  composer_muted="Мут: {n} с", modal_create_title="Створити приватний канал",
  modal_name="Назва", modal_name_ph="Наприклад, робота", modal_pass="Пароль каналу",
  modal_pass_ph="Потрібен для читання", modal_pass_hint="Лише ті, хто знає пароль, зможуть розшифрувати. Сервер його не бачить.",
  btn_cancel="Скасувати", btn_create="Створити", modal_connect_title="Увійти в канал",
  modal_connect_name="Назва каналу", modal_connect_ph="Введіть точну назву",
  btn_lookup="Знайти", btn_join="Увійти", connect_not_found="Канал «{name}» не знайдено",
  title_add="Створити канал", title_join="Увійти", title_settings="Налаштування",
  theme_toggle="Змінити тему", lang_toggle="Змінити мову", uptime_label="Аптайм",
  online_label="онлайн", err_bad_credentials="Невірний пароль", err_bad_username="Нік 2–32",
  err_bad_password="Пароль мінімум 4", err_bad_passphrase="Введіть пароль каналу",
  err_pass_wrong="Не вдалося розшифрувати", err_generic="Помилка",
  err_rate_limited="Забагато запитів", settings_title="Налаштування", settings_account="Акаунт",
  settings_appearance="Оформлення", settings_users="Користувачі",
  settings_show_in_list="Показувати мене в списку", settings_user="Ви увійшли як",
  settings_logout="Вийти", settings_theme="Тема", settings_theme_light="Світла",
  settings_theme_dark="Темна", settings_lang="Мова", settings_close="Закрити",
  users_list_empty="Немає користувачів", users_you="(ви)", users_online="онлайн",
  mobile_channels="Канали", mention_hint="Введіть @ щоб згадати", leave_channel="Покинути",
  lang_picker_title="Мова")

I18N["tr"] = _L(login_title="sldchat", login_subtitle="Giriş yap veya hesap oluştur",
  field_nick="Takma ad", field_password="Şifre", ph_nick="Takma adınız", ph_password="Şifreniz",
  btn_login="Devam et", btn_wait="Lütfen bekleyin…", remember_me="Beni hatırla",
  logged_as="Giriş yapan:", header_no_channels="Kanal yok",
  header_no_channels_sub="Kanallar'ı açarak oluşturun veya katılın", header_msgs="{n} mesaj",
  empty_no_channels="Henüz kanalınız yok.", empty_no_messages="Mesaj yok. İlk yazan siz olun!",
  composer_ph="Mesaj yazın...", composer_no_channel="Aktif kanal yok", composer_muted="Sessiz: {n}sn",
  modal_create_title="Özel kanal oluştur", modal_name="Ad", modal_name_ph="Örn. iş",
  modal_pass="Kanal şifresi", modal_pass_ph="Okumak için gerekli",
  modal_pass_hint="Yalnızca şifreyi bilenler çözebilir. Sunucu görmez.",
  btn_cancel="İptal", btn_create="Oluştur", modal_connect_title="Kanala katıl",
  modal_connect_name="Kanal adı", modal_connect_ph="Tam adı girin",
  btn_lookup="Ara", btn_join="Katıl", connect_not_found="Kanal «{name}» bulunamadı",
  title_add="Kanal oluştur", title_join="Katıl", title_settings="Ayarlar",
  theme_toggle="Temayı değiştir", lang_toggle="Dili değiştir", uptime_label="Çalışma süresi",
  online_label="çevrimiçi", err_bad_credentials="Yanlış şifre", err_bad_username="Takma ad 2–32",
  err_bad_password="Şifre min. 4", err_bad_passphrase="Kanal şifresi gerekli",
  err_pass_wrong="Çözülemedi — yanlış şifre?", err_generic="Hata", err_rate_limited="Çok fazla istek",
  settings_title="Ayarlar", settings_account="Hesap", settings_appearance="Görünüm",
  settings_users="Kullanıcılar", settings_show_in_list="Listede göster",
  settings_user="Giriş yapan:", settings_logout="Çıkış yap", settings_theme="Tema",
  settings_theme_light="Açık", settings_theme_dark="Koyu", settings_lang="Dil",
  settings_close="Kapat", users_list_empty="Kullanıcı yok", users_you="(siz)", users_online="çevrimiçi",
  mobile_channels="Kanallar", mention_hint="@ yazın", leave_channel="Ayrıl",
  lang_picker_title="Dil")

I18N["ja"] = _L(login_title="sldchat", login_subtitle="サインインまたはアカウント作成",
  field_nick="ニックネーム", field_password="パスワード", ph_nick="ニックネーム", ph_password="パスワード",
  btn_login="続行", btn_wait="お待ちください…", remember_me="ログイン状態を保持",
  logged_as="ログイン中:", header_no_channels="チャンネルなし",
  header_no_channels_sub="「チャンネル」を開いて作成/参加", header_msgs="{n} 件のメッセージ",
  empty_no_channels="まだチャンネルがありません。", empty_no_messages="メッセージなし。最初に書いてみよう！",
  composer_ph="メッセージを入力...", composer_no_channel="アクティブなチャンネルなし", composer_muted="ミュート: {n}秒",
  modal_create_title="プライベートチャンネル作成", modal_name="名前", modal_name_ph="例: 仕事",
  modal_pass="チャンネルパスワード", modal_pass_ph="読むために必要",
  modal_pass_hint="パスワードを知る人だけが復号できます。サーバーは見ません。",
  btn_cancel="キャンセル", btn_create="作成", modal_connect_title="チャンネルに参加",
  modal_connect_name="チャンネル名", modal_connect_ph="正確な名前を入力",
  btn_lookup="検索", btn_join="参加", connect_not_found="チャンネル「{name}」が見つかりません",
  title_add="チャンネル作成", title_join="参加", title_settings="設定",
  theme_toggle="テーマ切替", lang_toggle="言語変更", uptime_label="稼働時間",
  online_label="オンライン", err_bad_credentials="パスワードが違います", err_bad_username="ニックネーム2〜32",
  err_bad_password="パスワード4文字以上", err_bad_passphrase="チャンネルパスワードを入力",
  err_pass_wrong="復号できません — パスワードが違う？", err_generic="エラー",
  err_rate_limited="リクエストが多すぎます", settings_title="設定", settings_account="アカウント",
  settings_appearance="外観", settings_users="ユーザー一覧", settings_show_in_list="一覧に自分を表示",
  settings_user="ログイン中:", settings_logout="サインアウト", settings_theme="テーマ",
  settings_theme_light="ライト", settings_theme_dark="ダーク", settings_lang="言語",
  settings_close="閉じる", users_list_empty="ユーザーがいません", users_you="(あなた)", users_online="オンライン",
  mobile_channels="チャンネル", mention_hint="@ でメンション", leave_channel="退出",
  lang_picker_title="言語")

I18N["ko"] = _L(login_title="sldchat", login_subtitle="로그인 또는 계정 만들기",
  field_nick="닉네임", field_password="비밀번호", ph_nick="닉네임", ph_password="비밀번호",
  btn_login="계속", btn_wait="잠시만 기다려주세요…", remember_me="로그인 상태 유지",
  logged_as="로그인:", header_no_channels="채널 없음",
  header_no_channels_sub="채널을 열어 생성 또는 참여하세요", header_msgs="{n}개 메시지",
  empty_no_channels="아직 채널이 없습니다.", empty_no_messages="메시지 없음. 먼저 작성해보세요!",
  composer_ph="메시지 입력...", composer_no_channel="활성 채널 없음", composer_muted="음소거: {n}초",
  modal_create_title="비공개 채널 만들기", modal_name="이름", modal_name_ph="예: 업무",
  modal_pass="채널 비밀번호", modal_pass_ph="읽기에 필요",
  modal_pass_hint="비밀번호를 아는 사람만 복호화할 수 있습니다. 서버는 보지 않습니다.",
  btn_cancel="취소", btn_create="만들기", modal_connect_title="채널 참여",
  modal_connect_name="채널 이름", modal_connect_ph="정확한 이름 입력",
  btn_lookup="검색", btn_join="참여", connect_not_found="채널 «{name}» 없음",
  title_add="채널 만들기", title_join="참여", title_settings="설정",
  theme_toggle="테마 전환", lang_toggle="언어 변경", uptime_label="가동 시간",
  online_label="온라인", err_bad_credentials="잘못된 비밀번호", err_bad_username="닉네임 2–32",
  err_bad_password="비밀번호 4자 이상", err_bad_passphrase="채널 비밀번호 필요",
  err_pass_wrong="복호화 실패", err_generic="오류", err_rate_limited="너무 많은 요청",
  settings_title="설정", settings_account="계정", settings_appearance="모양",
  settings_users="사용자 목록", settings_show_in_list="목록에 나를 표시",
  settings_user="로그인:", settings_logout="로그아웃", settings_theme="테마",
  settings_theme_light="라이트", settings_theme_dark="다크", settings_lang="언어",
  settings_close="닫기", users_list_empty="사용자 없음", users_you="(나)", users_online="온라인",
  mobile_channels="채널", mention_hint="@ 입력하여 멘션", leave_channel="나가기",
  lang_picker_title="언어")

I18N["zh"] = _L(login_title="sldchat", login_subtitle="登录或创建账号",
  field_nick="昵称", field_password="密码", ph_nick="你的昵称", ph_password="你的密码",
  btn_login="继续", btn_wait="请稍候…", remember_me="记住我",
  logged_as="登录为", header_no_channels="无频道",
  header_no_channels_sub="打开频道以创建或加入", header_msgs="{n} 条消息",
  empty_no_channels="还没有频道。", empty_no_messages="没有消息。成为第一个发言的人！",
  composer_ph="输入消息...", composer_no_channel="无活动频道", composer_muted="已禁言：{n}秒",
  modal_create_title="创建私密频道", modal_name="名称", modal_name_ph="例如：工作",
  modal_pass="频道密码", modal_pass_ph="阅读所需",
  modal_pass_hint="只有知道密码的人才能解密。服务器看不到。",
  btn_cancel="取消", btn_create="创建", modal_connect_title="加入频道",
  modal_connect_name="频道名称", modal_connect_ph="输入确切名称",
  btn_lookup="查找", btn_join="加入", connect_not_found="未找到频道 «{name}»",
  title_add="创建频道", title_join="加入", title_settings="设置",
  theme_toggle="切换主题", lang_toggle="切换语言", uptime_label="运行时间",
  online_label="在线", err_bad_credentials="密码错误", err_bad_username="昵称 2–32",
  err_bad_password="密码至少 4 位", err_bad_passphrase="需要频道密码",
  err_pass_wrong="无法解密", err_generic="错误", err_rate_limited="请求过多",
  settings_title="设置", settings_account="账号", settings_appearance="外观",
  settings_users="用户列表", settings_show_in_list="在列表中显示我",
  settings_user="登录为", settings_logout="退出登录", settings_theme="主题",
  settings_theme_light="浅色", settings_theme_dark="深色", settings_lang="语言",
  settings_close="关闭", users_list_empty="没有用户", users_you="(你)", users_online="在线",
  mobile_channels="频道", mention_hint="输入 @ 提及", leave_channel="离开",
  lang_picker_title="语言")

I18N["ar"] = _L(login_title="sldchat", login_subtitle="سجّل الدخول أو أنشئ حسابًا",
  field_nick="الاسم", field_password="كلمة المرور", ph_nick="اسمك", ph_password="كلمة مرورك",
  btn_login="متابعة", btn_wait="يرجى الانتظار…", remember_me="تذكرني",
  logged_as="مسجل باسم", header_no_channels="لا قنوات",
  header_no_channels_sub="افتح القنوات لإنشاء أو الانضمام", header_msgs="{n} رسالة",
  empty_no_channels="لا قنوات بعد.", empty_no_messages="لا رسائل. كن الأول!",
  composer_ph="اكتب رسالة...", composer_no_channel="لا قناة نشطة", composer_muted="مكتوم: {n} ث",
  modal_create_title="إنشاء قناة خاصة", modal_name="الاسم", modal_name_ph="مثلاً: العمل",
  modal_pass="كلمة مرور القناة", modal_pass_ph="مطلوبة للقراءة",
  modal_pass_hint="فقط من يعرف كلمة المرور يمكنه فك التشفير. الخادم لا يراها.",
  btn_cancel="إلغاء", btn_create="إنشاء", modal_connect_title="انضم إلى قناة",
  modal_connect_name="اسم القناة", modal_connect_ph="أدخل الاسم الدقيق",
  btn_lookup="بحث", btn_join="انضم", connect_not_found="لم يتم العثور على «{name}»",
  title_add="إنشاء قناة", title_join="انضم", title_settings="الإعدادات",
  theme_toggle="تغيير المظهر", lang_toggle="تغيير اللغة", uptime_label="مدة التشغيل",
  online_label="متصل", err_bad_credentials="كلمة مرور خاطئة", err_bad_username="الاسم 2–32",
  err_bad_password="كلمة المرور 4 أحرف على الأقل", err_bad_passphrase="كلمة مرور القناة مطلوبة",
  err_pass_wrong="فشل فك التشفير", err_generic="خطأ", err_rate_limited="طلبات كثيرة",
  settings_title="الإعدادات", settings_account="الحساب", settings_appearance="المظهر",
  settings_users="المستخدمون", settings_show_in_list="إظهاري في القائمة",
  settings_user="مسجل باسم", settings_logout="تسجيل الخروج", settings_theme="المظهر",
  settings_theme_light="فاتح", settings_theme_dark="داكن", settings_lang="اللغة",
  settings_close="إغلاق", users_list_empty="لا مستخدمين", users_you="(أنت)", users_online="متصل",
  mobile_channels="القنوات", mention_hint="اكتب @ للإشارة", leave_channel="مغادرة",
  lang_picker_title="اللغة")

I18N["fa"] = _L(login_title="sldchat", login_subtitle="وارد شوید یا حساب بسازید",
  field_nick="نام مستعار", field_password="رمز", ph_nick="نام شما", ph_password="رمز شما",
  btn_login="ادامه", btn_wait="لطفاً صبر کنید…", remember_me="مرا به خاطر بسپار",
  logged_as="وارد شده به عنوان", header_no_channels="کانالی نیست",
  header_no_channels_sub="کانال‌ها را باز کنید", header_msgs="{n} پیام",
  empty_no_channels="هنوز کانالی ندارید.", empty_no_messages="پیامی نیست. اولین نفر باشید!",
  composer_ph="پیام بنویسید...", composer_no_channel="کانال فعالی نیست", composer_muted="بی‌صدا: {n} ثانیه",
  modal_create_title="ساخت کانال خصوصی", modal_name="نام", modal_name_ph="مثلاً: کار",
  modal_pass="رمز کانال", modal_pass_ph="برای خواندن لازم است",
  modal_pass_hint="تنها کسانی که رمز را می‌دانند می‌توانند رمزگشایی کنند.",
  btn_cancel="لغو", btn_create="ایجاد", modal_connect_title="ورود به کانال",
  modal_connect_name="نام کانال", modal_connect_ph="نام دقیق را وارد کنید",
  btn_lookup="جستجو", btn_join="ورود", connect_not_found="کانال «{name}» یافت نشد",
  title_add="ساخت کانال", title_join="ورود", title_settings="تنظیمات",
  theme_toggle="تغییر پوسته", lang_toggle="تغییر زبان", uptime_label="زمان کار",
  online_label="آنلاین", err_bad_credentials="رمز اشتباه", err_bad_username="نام ۲–۳۲",
  err_bad_password="رمز حداقل ۴ کاراکتر", err_bad_passphrase="رمز کانال لازم است",
  err_pass_wrong="رمزگشایی ناموفق", err_generic="خطا", err_rate_limited="درخواست‌های زیاد",
  settings_title="تنظیمات", settings_account="حساب", settings_appearance="ظاهر",
  settings_users="کاربران", settings_show_in_list="مرا در فهرست نشان بده",
  settings_user="وارد شده به عنوان", settings_logout="خروج", settings_theme="پوسته",
  settings_theme_light="روشن", settings_theme_dark="تیره", settings_lang="زبان",
  settings_close="بستن", users_list_empty="کاربری نیست", users_you="(شما)", users_online="آنلاین",
  mobile_channels="کانال‌ها", mention_hint="@ برای اشاره", leave_channel="خروج",
  lang_picker_title="زبان")

I18N["hi"] = _L(login_title="sldchat", login_subtitle="साइन इन करें या खाता बनाएं",
  field_nick="उपनाम", field_password="पासवर्ड", ph_nick="आपका उपनाम", ph_password="आपका पासवर्ड",
  btn_login="जारी रखें", btn_wait="कृपया प्रतीक्षा करें…", remember_me="मुझे याद रखें",
  logged_as="इस रूप में:", header_no_channels="कोई चैनल नहीं",
  header_no_channels_sub="बनाने या जुड़ने के लिए चैनल खोलें", header_msgs="{n} संदेश",
  empty_no_channels="अभी कोई चैनल नहीं।", empty_no_messages="कोई संदेश नहीं। पहले लिखें!",
  composer_ph="संदेश लिखें...", composer_no_channel="कोई सक्रिय चैनल नहीं", composer_muted="म्यूट: {n}से",
  modal_create_title="निजी चैनल बनाएं", modal_name="नाम", modal_name_ph="जैसे: काम",
  modal_pass="चैनल पासवर्ड", modal_pass_ph="पढ़ने के लिए आवश्यक",
  modal_pass_hint="केवल पासवर्ड जानने वाले डिक्रिप्ट कर सकते हैं। सर्वर इसे नहीं देखता।",
  btn_cancel="रद्द करें", btn_create="बनाएं", modal_connect_title="चैनल से जुड़ें",
  modal_connect_name="चैनल का नाम", modal_connect_ph="सटीक नाम दर्ज करें",
  btn_lookup="खोजें", btn_join="जुड़ें", connect_not_found="चैनल «{name}» नहीं मिला",
  title_add="चैनल बनाएं", title_join="जुड़ें", title_settings="सेटिंग्स",
  theme_toggle="थीम बदलें", lang_toggle="भाषा बदलें", uptime_label="अपटाइम",
  online_label="ऑनलाइन", err_bad_credentials="गलत पासवर्ड", err_bad_username="उपनाम २–३२",
  err_bad_password="पासवर्ड कम से कम ४", err_bad_passphrase="चैनल पासवर्ड आवश्यक",
  err_pass_wrong="डिक्रिप्ट विफल", err_generic="त्रुटि", err_rate_limited="बहुत अनुरोध",
  settings_title="सेटिंग्स", settings_account="खाता", settings_appearance="रूप",
  settings_users="उपयोगकर्ता", settings_show_in_list="सूची में दिखाएं",
  settings_user="इस रूप में:", settings_logout="साइन आउट", settings_theme="थीम",
  settings_theme_light="लाइट", settings_theme_dark="डार्क", settings_lang="भाषा",
  settings_close="बंद करें", users_list_empty="कोई उपयोगकर्ता नहीं", users_you="(आप)", users_online="ऑनलाइन",
  mobile_channels="चैनल", mention_hint="@ लिखें", leave_channel="छोड़ें",
  lang_picker_title="भाषा")

I18N["id"] = _L(login_title="sldchat", login_subtitle="Masuk atau buat akun",
  field_nick="Nama panggilan", field_password="Kata sandi", ph_nick="Nama Anda", ph_password="Kata sandi Anda",
  btn_login="Lanjut", btn_wait="Mohon tunggu…", remember_me="Ingat saya",
  logged_as="Masuk sebagai", header_no_channels="Tidak ada kanal",
  header_no_channels_sub="Buka Kanal untuk membuat atau bergabung", header_msgs="{n} pesan",
  empty_no_channels="Belum ada kanal.", empty_no_messages="Tidak ada pesan. Jadilah yang pertama!",
  composer_ph="Tulis pesan...", composer_no_channel="Tidak ada kanal aktif", composer_muted="Dibisukan: {n}d",
  modal_create_title="Buat kanal pribadi", modal_name="Nama", modal_name_ph="Mis. kerja",
  modal_pass="Kata sandi kanal", modal_pass_ph="Perlu untuk membaca",
  modal_pass_hint="Hanya yang tahu kata sandi bisa mendekripsi. Server tidak melihatnya.",
  btn_cancel="Batal", btn_create="Buat", modal_connect_title="Gabung kanal",
  modal_connect_name="Nama kanal", modal_connect_ph="Masukkan nama persis",
  btn_lookup="Cari", btn_join="Gabung", connect_not_found="Kanal «{name}» tidak ditemukan",
  title_add="Buat kanal", title_join="Gabung", title_settings="Pengaturan",
  theme_toggle="Ganti tema", lang_toggle="Ganti bahasa", uptime_label="Uptime",
  online_label="daring", err_bad_credentials="Kata sandi salah", err_bad_username="Nama 2–32",
  err_bad_password="Kata sandi min. 4", err_bad_passphrase="Kata sandi kanal diperlukan",
  err_pass_wrong="Gagal mendekripsi", err_generic="Kesalahan", err_rate_limited="Terlalu banyak permintaan",
  settings_title="Pengaturan", settings_account="Akun", settings_appearance="Tampilan",
  settings_users="Pengguna", settings_show_in_list="Tampilkan saya di daftar",
  settings_user="Masuk sebagai", settings_logout="Keluar", settings_theme="Tema",
  settings_theme_light="Terang", settings_theme_dark="Gelap", settings_lang="Bahasa",
  settings_close="Tutup", users_list_empty="Tidak ada pengguna", users_you="(Anda)", users_online="daring",
  mobile_channels="Kanal", mention_hint="Ketik @ untuk menyebut", leave_channel="Keluar",
  lang_picker_title="Bahasa")

I18N["vi"] = _L(login_title="sldchat", login_subtitle="Đăng nhập hoặc tạo tài khoản",
  field_nick="Biệt danh", field_password="Mật khẩu", ph_nick="Biệt danh của bạn", ph_password="Mật khẩu của bạn",
  btn_login="Tiếp tục", btn_wait="Vui lòng đợi…", remember_me="Nhớ tôi",
  logged_as="Đăng nhập với tư cách", header_no_channels="Không có kênh",
  header_no_channels_sub="Mở Kênh để tạo hoặc tham gia", header_msgs="{n} tin nhắn",
  empty_no_channels="Chưa có kênh.", empty_no_messages="Chưa có tin nhắn. Hãy là người đầu tiên!",
  composer_ph="Viết tin nhắn...", composer_no_channel="Không có kênh hoạt động", composer_muted="Đã tắt tiếng: {n}s",
  modal_create_title="Tạo kênh riêng tư", modal_name="Tên", modal_name_ph="Ví dụ: công việc",
  modal_pass="Mật khẩu kênh", modal_pass_ph="Cần để đọc",
  modal_pass_hint="Chỉ người biết mật khẩu mới giải mã được. Máy chủ không thấy.",
  btn_cancel="Hủy", btn_create="Tạo", modal_connect_title="Tham gia kênh",
  modal_connect_name="Tên kênh", modal_connect_ph="Nhập tên chính xác",
  btn_lookup="Tìm", btn_join="Tham gia", connect_not_found="Không tìm thấy kênh «{name}»",
  title_add="Tạo kênh", title_join="Tham gia", title_settings="Cài đặt",
  theme_toggle="Đổi chủ đề", lang_toggle="Đổi ngôn ngữ", uptime_label="Thời gian hoạt động",
  online_label="trực tuyến", err_bad_credentials="Sai mật khẩu", err_bad_username="Biệt danh 2–32",
  err_bad_password="Mật khẩu tối thiểu 4", err_bad_passphrase="Cần mật khẩu kênh",
  err_pass_wrong="Không giải mã được", err_generic="Lỗi", err_rate_limited="Quá nhiều yêu cầu",
  settings_title="Cài đặt", settings_account="Tài khoản", settings_appearance="Giao diện",
  settings_users="Người dùng", settings_show_in_list="Hiển thị tôi trong danh sách",
  settings_user="Đăng nhập với tư cách", settings_logout="Đăng xuất", settings_theme="Chủ đề",
  settings_theme_light="Sáng", settings_theme_dark="Tối", settings_lang="Ngôn ngữ",
  settings_close="Đóng", users_list_empty="Không có người dùng", users_you="(bạn)", users_online="trực tuyến",
  mobile_channels="Kênh", mention_hint="Gõ @ để nhắc", leave_channel="Rời",
  lang_picker_title="Ngôn ngữ")

I18N["th"] = _L(login_title="sldchat", login_subtitle="เข้าสู่ระบบหรือสร้างบัญชี",
  field_nick="ชื่อเล่น", field_password="รหัสผ่าน", ph_nick="ชื่อเล่นของคุณ", ph_password="รหัสผ่านของคุณ",
  btn_login="ดำเนินการต่อ", btn_wait="กรุณารอสักครู่…", remember_me="จดจำฉัน",
  logged_as="เข้าสู่ระบบเป็น", header_no_channels="ไม่มีช่อง",
  header_no_channels_sub="เปิดช่องเพื่อสร้างหรือเข้าร่วม", header_msgs="{n} ข้อความ",
  empty_no_channels="ยังไม่มีช่อง", empty_no_messages="ไม่มีข้อความ มาเป็นคนแรกกันเถอะ!",
  composer_ph="เขียนข้อความ...", composer_no_channel="ไม่มีช่องที่ใช้งาน", composer_muted="ปิดเสียง: {n}วิ",
  modal_create_title="สร้างช่องส่วนตัว", modal_name="ชื่อ", modal_name_ph="เช่น งาน",
  modal_pass="รหัสผ่านช่อง", modal_pass_ph="ต้องใช้ในการอ่าน",
  modal_pass_hint="เฉพาะผู้ที่รู้รหัสผ่านจึงจะถอดรหัสได้ เซิร์ฟเวอร์ไม่เห็น",
  btn_cancel="ยกเลิก", btn_create="สร้าง", modal_connect_title="เข้าร่วมช่อง",
  modal_connect_name="ชื่อช่อง", modal_connect_ph="ใส่ชื่อที่ถูกต้อง",
  btn_lookup="ค้นหา", btn_join="เข้าร่วม", connect_not_found="ไม่พบช่อง «{name}»",
  title_add="สร้างช่อง", title_join="เข้าร่วม", title_settings="ตั้งค่า",
  theme_toggle="เปลี่ยนธีม", lang_toggle="เปลี่ยนภาษา", uptime_label="เวลาทำงาน",
  online_label="ออนไลน์", err_bad_credentials="รหัสผ่านผิด", err_bad_username="ชื่อเล่น 2–32",
  err_bad_password="รหัสผ่านอย่างน้อย 4", err_bad_passphrase="ต้องใส่รหัสผ่านช่อง",
  err_pass_wrong="ถอดรหัสไม่สำเร็จ", err_generic="ข้อผิดพลาด", err_rate_limited="คำขอมากเกินไป",
  settings_title="ตั้งค่า", settings_account="บัญชี", settings_appearance="รูปลักษณ์",
  settings_users="ผู้ใช้", settings_show_in_list="แสดงฉันในรายการ",
  settings_user="เข้าสู่ระบบเป็น", settings_logout="ออกจากระบบ", settings_theme="ธีม",
  settings_theme_light="สว่าง", settings_theme_dark="มืด", settings_lang="ภาษา",
  settings_close="ปิด", users_list_empty="ไม่มีผู้ใช้", users_you="(คุณ)", users_online="ออนไลน์",
  mobile_channels="ช่อง", mention_hint="พิมพ์ @ เพื่อกล่าวถึง", leave_channel="ออก",
  lang_picker_title="ภาษา")

I18N["he"] = _L(login_title="sldchat", login_subtitle="התחבר או צור חשבון",
  field_nick="כינוי", field_password="סיסמה", ph_nick="הכינוי שלך", ph_password="הסיסמה שלך",
  btn_login="המשך", btn_wait="אנא המתן…", remember_me="זכור אותי",
  logged_as="מחובר בתור", header_no_channels="אין ערוצים",
  header_no_channels_sub="פתח ערוצים כדי ליצור או להצטרף", header_msgs="{n} הודעות",
  empty_no_channels="אין לך ערוצים עדיין.", empty_no_messages="אין הודעות. היה הראשון לכתוב!",
  composer_ph="כתוב הודעה...", composer_no_channel="אין ערוץ פעיל", composer_muted="מושתק: {n} שניות",
  modal_create_title="צור ערוץ פרטי", modal_name="שם", modal_name_ph="לדוגמה: עבודה",
  modal_pass="סיסמת ערוץ", modal_pass_ph="נדרש לקריאה",
  modal_pass_hint="רק מי שיודע את הסיסמה יכול לפענח. השרת לא רואה אותה.",
  btn_cancel="ביטול", btn_create="צור", modal_connect_title="הצטרף לערוץ",
  modal_connect_name="שם הערוץ", modal_connect_ph="הזן שם מדויק",
  btn_lookup="חפש", btn_join="הצטרף", connect_not_found="הערוץ «{name}» לא נמצא",
  title_add="צור ערוץ", title_join="הצטרף", title_settings="הגדרות",
  theme_toggle="החלף ערכת נושא", lang_toggle="החלף שפה", uptime_label="זמן פעילות",
  online_label="מחובר", err_bad_credentials="סיסמה שגויה", err_bad_username="כינוי 2–32",
  err_bad_password="סיסמה לפחות 4", err_bad_passphrase="נדרשת סיסמת ערוץ",
  err_pass_wrong="פענוח נכשל", err_generic="שגיאה", err_rate_limited="בקשות רבות מדי",
  settings_title="הגדרות", settings_account="חשבון", settings_appearance="מראה",
  settings_users="משתמשים", settings_show_in_list="הצג אותי ברשימה",
  settings_user="מחובר בתור", settings_logout="התנתק", settings_theme="ערכת נושא",
  settings_theme_light="בהיר", settings_theme_dark="כהה", settings_lang="שפה",
  settings_close="סגור", users_list_empty="אין משתמשים", users_you="(אתה)", users_online="מחובר",
  mobile_channels="ערוצים", mention_hint="הקלד @ לתיוג", leave_channel="צא",
  lang_picker_title="שפה")

I18N["ro"] = _L(login_title="sldchat", login_subtitle="Autentifică-te sau creează un cont",
  field_nick="Poreclă", field_password="Parolă", ph_nick="Porecla ta", ph_password="Parola ta",
  btn_login="Continuă", btn_wait="Te rugăm să aștepți…", remember_me="Ține-mă minte",
  logged_as="Conectat ca", header_no_channels="Fără canale",
  header_no_channels_sub="Deschide Canale pentru a crea sau a te alătura", header_msgs="{n} mesaje",
  empty_no_channels="Nu ai canale încă.", empty_no_messages="Fără mesaje. Fii primul!",
  composer_ph="Scrie un mesaj...", composer_no_channel="Fără canal activ", composer_muted="Silențios: {n}s",
  modal_create_title="Creează canal privat", modal_name="Nume", modal_name_ph="Ex: muncă",
  modal_pass="Parola canalului", modal_pass_ph="Necesară pentru citire",
  modal_pass_hint="Doar cei care știu parola pot decripta. Serverul nu o vede.",
  btn_cancel="Anulează", btn_create="Creează", modal_connect_title="Alătură-te canalului",
  modal_connect_name="Numele canalului", modal_connect_ph="Introdu numele exact",
  btn_lookup="Caută", btn_join="Alătură-te", connect_not_found="Canalul «{name}» nu a fost găsit",
  title_add="Creează canal", title_join="Alătură-te", title_settings="Setări",
  theme_toggle="Schimbă tema", lang_toggle="Schimbă limba", uptime_label="Timp de funcționare",
  online_label="online", err_bad_credentials="Parolă greșită", err_bad_username="Poreclă 2–32",
  err_bad_password="Parola min. 4", err_bad_passphrase="Parola canalului necesară",
  err_pass_wrong="Decriptare eșuată", err_generic="Eroare", err_rate_limited="Prea multe cereri",
  settings_title="Setări", settings_account="Cont", settings_appearance="Aspect",
  settings_users="Utilizatori", settings_show_in_list="Arată-mă în listă",
  settings_user="Conectat ca", settings_logout="Deconectare", settings_theme="Temă",
  settings_theme_light="Luminos", settings_theme_dark="Întunecat", settings_lang="Limbă",
  settings_close="Închide", users_list_empty="Fără utilizatori", users_you="(tu)", users_online="online",
  mobile_channels="Canale", mention_hint="Tastează @ pentru a menționa", leave_channel="Părăsește",
  lang_picker_title="Limbă")

I18N["hu"] = _L(login_title="sldchat", login_subtitle="Jelentkezz be vagy hozz létre fiókot",
  field_nick="Becenév", field_password="Jelszó", ph_nick="Beceneved", ph_password="Jelszavad",
  btn_login="Tovább", btn_wait="Kérlek várj…", remember_me="Emlékezz rám",
  logged_as="Bejelentkezve mint", header_no_channels="Nincs csatorna",
  header_no_channels_sub="Nyisd meg a Csatornák panelt", header_msgs="{n} üzenet",
  empty_no_channels="Még nincs csatornád.", empty_no_messages="Nincs üzenet. Írj elsőként!",
  composer_ph="Írj üzenetet...", composer_no_channel="Nincs aktív csatorna", composer_muted="Némítva: {n}mp",
  modal_create_title="Privát csatorna létrehozása", modal_name="Név", modal_name_ph="Pl. munka",
  modal_pass="Csatorna jelszava", modal_pass_ph="Olvasáshoz szükséges",
  modal_pass_hint="Csak a jelszót ismerők tudják visszafejteni. A szerver nem látja.",
  btn_cancel="Mégse", btn_create="Létrehozás", modal_connect_title="Csatlakozás csatornához",
  modal_connect_name="Csatorna neve", modal_connect_ph="Írd be a pontos nevet",
  btn_lookup="Keresés", btn_join="Csatlakozás", connect_not_found="«{name}» csatorna nem található",
  title_add="Csatorna létrehozása", title_join="Csatlakozás", title_settings="Beállítások",
  theme_toggle="Téma váltása", lang_toggle="Nyelv váltása", uptime_label="Üzemidő",
  online_label="online", err_bad_credentials="Hibás jelszó", err_bad_username="Becenév 2–32",
  err_bad_password="Jelszó min. 4", err_bad_passphrase="Csatorna jelszó szükséges",
  err_pass_wrong="Visszafejtés sikertelen", err_generic="Hiba", err_rate_limited="Túl sok kérés",
  settings_title="Beállítások", settings_account="Fiók", settings_appearance="Megjelenés",
  settings_users="Felhasználók", settings_show_in_list="Mutass a listában",
  settings_user="Bejelentkezve mint", settings_logout="Kijelentkezés", settings_theme="Téma",
  settings_theme_light="Világos", settings_theme_dark="Sötét", settings_lang="Nyelv",
  settings_close="Bezárás", users_list_empty="Nincs felhasználó", users_you="(te)", users_online="online",
  mobile_channels="Csatornák", mention_hint="Írj @ a megemlítéshez", leave_channel="Elhagyás",
  lang_picker_title="Nyelv")

I18N["el"] = _L(login_title="sldchat", login_subtitle="Συνδεθείτε ή δημιουργήστε λογαριασμό",
  field_nick="Ψευδώνυμο", field_password="Κωδικός", ph_nick="Ψευδώνυμό σας", ph_password="Κωδικός σας",
  btn_login="Συνέχεια", btn_wait="Παρακαλώ περιμένετε…", remember_me="Να με θυμάσαι",
  logged_as="Συνδεδεμένος ως", header_no_channels="Δεν υπάρχουν κανάλια",
  header_no_channels_sub="Ανοίξτε τα Κανάλια", header_msgs="{n} μηνύματα",
  empty_no_channels="Δεν έχετε κανάλια.", empty_no_messages="Χωρίς μηνύματα. Γράψτε πρώτος!",
  composer_ph="Γράψτε μήνυμα...", composer_no_channel="Χωρίς ενεργό κανάλι", composer_muted="Σε σίγαση: {n}δ",
  modal_create_title="Δημιουργία ιδιωτικού καναλιού", modal_name="Όνομα", modal_name_ph="Π.χ. εργασία",
  modal_pass="Κωδικός καναλιού", modal_pass_ph="Απαιτείται για ανάγνωση",
  modal_pass_hint="Μόνο όσοι γνωρίζουν τον κωδικό μπορούν να αποκρυπτογραφήσουν.",
  btn_cancel="Άκυρο", btn_create="Δημιουργία", modal_connect_title="Σύνδεση σε κανάλι",
  modal_connect_name="Όνομα καναλιού", modal_connect_ph="Εισάγετε το ακριβές όνομα",
  btn_lookup="Αναζήτηση", btn_join="Σύνδεση", connect_not_found="Το κανάλι «{name}» δεν βρέθηκε",
  title_add="Δημιουργία καναλιού", title_join="Σύνδεση", title_settings="Ρυθμίσεις",
  theme_toggle="Αλλαγή θέματος", lang_toggle="Αλλαγή γλώσσας", uptime_label="Χρόνος λειτουργίας",
  online_label="συνδεδεμένοι", err_bad_credentials="Λάθος κωδικός", err_bad_username="Ψευδώνυμο 2–32",
  err_bad_password="Κωδικός τουλάχιστον 4", err_bad_passphrase="Απαιτείται κωδικός καναλιού",
  err_pass_wrong="Αποκρυπτογράφηση απέτυχε", err_generic="Σφάλμα", err_rate_limited="Πολλά αιτήματα",
  settings_title="Ρυθμίσεις", settings_account="Λογαριασμός", settings_appearance="Εμφάνιση",
  settings_users="Χρήστες", settings_show_in_list="Να εμφανίζομαι στη λίστα",
  settings_user="Συνδεδεμένος ως", settings_logout="Αποσύνδεση", settings_theme="Θέμα",
  settings_theme_light="Φωτεινό", settings_theme_dark="Σκούρο", settings_lang="Γλώσσα",
  settings_close="Κλείσιμο", users_list_empty="Χωρίς χρήστες", users_you="(εσύ)", users_online="συνδεδεμένοι",
  mobile_channels="Κανάλια", mention_hint="Πληκτρολογήστε @", leave_channel="Αποχώρηση",
  lang_picker_title="Γλώσσα")

I18N["sv"] = _L(login_title="sldchat", login_subtitle="Logga in eller skapa konto",
  field_nick="Smeknamn", field_password="Lösenord", ph_nick="Ditt smeknamn", ph_password="Ditt lösenord",
  btn_login="Fortsätt", btn_wait="Vänta…", remember_me="Kom ihåg mig",
  logged_as="Inloggad som", header_no_channels="Inga kanaler",
  header_no_channels_sub="Öppna Kanaler för att skapa eller gå med", header_msgs="{n} meddelanden",
  empty_no_channels="Inga kanaler än.", empty_no_messages="Inga meddelanden. Skriv först!",
  composer_ph="Skriv ett meddelande...", composer_no_channel="Ingen aktiv kanal", composer_muted="Tystad: {n}s",
  modal_create_title="Skapa privat kanal", modal_name="Namn", modal_name_ph="T.ex. arbete",
  modal_pass="Kanalens lösenord", modal_pass_ph="Behövs för att läsa",
  modal_pass_hint="Endast de som kan lösenordet kan dekryptera. Servern ser det inte.",
  btn_cancel="Avbryt", btn_create="Skapa", modal_connect_title="Gå med i kanal",
  modal_connect_name="Kanalens namn", modal_connect_ph="Ange det exakta namnet",
  btn_lookup="Sök", btn_join="Gå med", connect_not_found="Kanalen «{name}» hittades inte",
  title_add="Skapa kanal", title_join="Gå med", title_settings="Inställningar",
  theme_toggle="Växla tema", lang_toggle="Byt språk", uptime_label="Drifttid",
  online_label="online", err_bad_credentials="Fel lösenord", err_bad_username="Smeknamn 2–32",
  err_bad_password="Lösenord min. 4", err_bad_passphrase="Kanalens lösenord krävs",
  err_pass_wrong="Dekryptering misslyckades", err_generic="Fel", err_rate_limited="För många förfrågningar",
  settings_title="Inställningar", settings_account="Konto", settings_appearance="Utseende",
  settings_users="Användare", settings_show_in_list="Visa mig i listan",
  settings_user="Inloggad som", settings_logout="Logga ut", settings_theme="Tema",
  settings_theme_light="Ljus", settings_theme_dark="Mörk", settings_lang="Språk",
  settings_close="Stäng", users_list_empty="Inga användare", users_you="(du)", users_online="online",
  mobile_channels="Kanaler", mention_hint="Skriv @ för att nämna", leave_channel="Lämna",
  lang_picker_title="Språk")

I18N["no"] = _L(login_title="sldchat", login_subtitle="Logg inn eller opprett konto",
  field_nick="Kallenavn", field_password="Passord", ph_nick="Ditt kallenavn", ph_password="Ditt passord",
  btn_login="Fortsett", btn_wait="Vennligst vent…", remember_me="Husk meg",
  logged_as="Logget inn som", header_no_channels="Ingen kanaler",
  header_no_channels_sub="Åpne Kanaler for å opprette eller bli med", header_msgs="{n} meldinger",
  empty_no_channels="Ingen kanaler ennå.", empty_no_messages="Ingen meldinger. Bli den første!",
  composer_ph="Skriv en melding...", composer_no_channel="Ingen aktiv kanal", composer_muted="Dempet: {n}s",
  modal_create_title="Opprett privat kanal", modal_name="Navn", modal_name_ph="F.eks. arbeid",
  modal_pass="Kanalpassord", modal_pass_ph="Nødvendig for å lese",
  modal_pass_hint="Bare de som kjenner passordet kan dekryptere. Serveren ser det ikke.",
  btn_cancel="Avbryt", btn_create="Opprett", modal_connect_title="Bli med i kanal",
  modal_connect_name="Kanalnavn", modal_connect_ph="Skriv inn nøyaktig navn",
  btn_lookup="Søk", btn_join="Bli med", connect_not_found="Kanalen «{name}» ble ikke funnet",
  title_add="Opprett kanal", title_join="Bli med", title_settings="Innstillinger",
  theme_toggle="Bytt tema", lang_toggle="Bytt språk", uptime_label="Oppetid",
  online_label="pålogget", err_bad_credentials="Feil passord", err_bad_username="Kallenavn 2–32",
  err_bad_password="Passord min. 4", err_bad_passphrase="Kanalpassord kreves",
  err_pass_wrong="Dekryptering mislyktes", err_generic="Feil", err_rate_limited="For mange forespørsler",
  settings_title="Innstillinger", settings_account="Konto", settings_appearance="Utseende",
  settings_users="Brukere", settings_show_in_list="Vis meg i listen",
  settings_user="Logget inn som", settings_logout="Logg ut", settings_theme="Tema",
  settings_theme_light="Lyst", settings_theme_dark="Mørkt", settings_lang="Språk",
  settings_close="Lukk", users_list_empty="Ingen brukere", users_you="(deg)", users_online="pålogget",
  mobile_channels="Kanaler", mention_hint="Skriv @ for å nevne", leave_channel="Forlat",
  lang_picker_title="Språk")

I18N["da"] = _L(login_title="sldchat", login_subtitle="Log ind eller opret konto",
  field_nick="Kaldenavn", field_password="Adgangskode", ph_nick="Dit kaldenavn", ph_password="Din adgangskode",
  btn_login="Fortsæt", btn_wait="Vent venligst…", remember_me="Husk mig",
  logged_as="Logget ind som", header_no_channels="Ingen kanaler",
  header_no_channels_sub="Åbn Kanaler for at oprette eller deltage", header_msgs="{n} beskeder",
  empty_no_channels="Ingen kanaler endnu.", empty_no_messages="Ingen beskeder. Vær den første!",
  composer_ph="Skriv en besked...", composer_no_channel="Ingen aktiv kanal", composer_muted="Dæmpet: {n}s",
  modal_create_title="Opret privat kanal", modal_name="Navn", modal_name_ph="F.eks. arbejde",
  modal_pass="Kanaladgangskode", modal_pass_ph="Nødvendig for at læse",
  modal_pass_hint="Kun dem der kender adgangskoden kan dekryptere. Serveren ser den ikke.",
  btn_cancel="Annuller", btn_create="Opret", modal_connect_title="Deltag i kanal",
  modal_connect_name="Kanalnavn", modal_connect_ph="Indtast præcist navn",
  btn_lookup="Søg", btn_join="Deltag", connect_not_found="Kanalen «{name}» blev ikke fundet",
  title_add="Opret kanal", title_join="Deltag", title_settings="Indstillinger",
  theme_toggle="Skift tema", lang_toggle="Skift sprog", uptime_label="Oppetid",
  online_label="online", err_bad_credentials="Forkert adgangskode", err_bad_username="Kaldenavn 2–32",
  err_bad_password="Adgangskode min. 4", err_bad_passphrase="Kanaladgangskode påkrævet",
  err_pass_wrong="Dekryptering mislykkedes", err_generic="Fejl", err_rate_limited="For mange anmodninger",
  settings_title="Indstillinger", settings_account="Konto", settings_appearance="Udseende",
  settings_users="Brugere", settings_show_in_list="Vis mig på listen",
  settings_user="Logget ind som", settings_logout="Log ud", settings_theme="Tema",
  settings_theme_light="Lyst", settings_theme_dark="Mørkt", settings_lang="Sprog",
  settings_close="Luk", users_list_empty="Ingen brugere", users_you="(dig)", users_online="online",
  mobile_channels="Kanaler", mention_hint="Skriv @ for at nævne", leave_channel="Forlad",
  lang_picker_title="Sprog")

I18N["fi"] = _L(login_title="sldchat", login_subtitle="Kirjaudu sisään tai luo tili",
  field_nick="Nimimerkki", field_password="Salasana", ph_nick="Nimimerkkisi", ph_password="Salasanasi",
  btn_login="Jatka", btn_wait="Odota hetki…", remember_me="Muista minut",
  logged_as="Kirjautuneena", header_no_channels="Ei kanavia",
  header_no_channels_sub="Avaa Kanavat luodaksesi tai liittyäksesi", header_msgs="{n} viestiä",
  empty_no_channels="Ei kanavia vielä.", empty_no_messages="Ei viestejä. Kirjoita ensimmäisenä!",
  composer_ph="Kirjoita viesti...", composer_no_channel="Ei aktiivista kanavaa", composer_muted="Mykistetty: {n}s",
  modal_create_title="Luo yksityinen kanava", modal_name="Nimi", modal_name_ph="Esim. työ",
  modal_pass="Kanavan salasana", modal_pass_ph="Tarvitaan lukemiseen",
  modal_pass_hint="Vain salasanan tietävät voivat purkaa salauksen. Palvelin ei näe sitä.",
  btn_cancel="Peruuta", btn_create="Luo", modal_connect_title="Liity kanavaan",
  modal_connect_name="Kanavan nimi", modal_connect_ph="Syötä tarkka nimi",
  btn_lookup="Etsi", btn_join="Liity", connect_not_found="Kanavaa «{name}» ei löytynyt",
  title_add="Luo kanava", title_join="Liity", title_settings="Asetukset",
  theme_toggle="Vaihda teema", lang_toggle="Vaihda kieli", uptime_label="Käyttöaika",
  online_label="paikalla", err_bad_credentials="Väärä salasana", err_bad_username="Nimimerkki 2–32",
  err_bad_password="Salasana väh. 4", err_bad_passphrase="Kanavan salasana vaaditaan",
  err_pass_wrong="Salauksen purku epäonnistui", err_generic="Virhe", err_rate_limited="Liian monta pyyntöä",
  settings_title="Asetukset", settings_account="Tili", settings_appearance="Ulkoasu",
  settings_users="Käyttäjät", settings_show_in_list="Näytä minut luettelossa",
  settings_user="Kirjautuneena", settings_logout="Kirjaudu ulos", settings_theme="Teema",
  settings_theme_light="Vaalea", settings_theme_dark="Tumma", settings_lang="Kieli",
  settings_close="Sulje", users_list_empty="Ei käyttäjiä", users_you="(sinä)", users_online="paikalla",
  mobile_channels="Kanavat", mention_hint="Kirjoita @ mainitaksesi", leave_channel="Poistu",
  lang_picker_title="Kieli")

I18N["cs"] = _L(login_title="sldchat", login_subtitle="Přihlaste se nebo si vytvořte účet",
  field_nick="Přezdívka", field_password="Heslo", ph_nick="Vaše přezdívka", ph_password="Vaše heslo",
  btn_login="Pokračovat", btn_wait="Prosím čekejte…", remember_me="Zapamatovat si mě",
  logged_as="Přihlášen jako", header_no_channels="Žádné kanály",
  header_no_channels_sub="Otevřete Kanály", header_msgs="{n} zpráv",
  empty_no_channels="Zatím žádné kanály.", empty_no_messages="Žádné zprávy. Napište první!",
  composer_ph="Napište zprávu...", composer_no_channel="Žádný aktivní kanál", composer_muted="Ztlumeno: {n}s",
  modal_create_title="Vytvořit soukromý kanál", modal_name="Název", modal_name_ph="Např. práce",
  modal_pass="Heslo kanálu", modal_pass_ph="Nutné pro čtení",
  modal_pass_hint="Pouze ti, kdo znají heslo, mohou dešifrovat. Server ho nevidí.",
  btn_cancel="Zrušit", btn_create="Vytvořit", modal_connect_title="Připojit se ke kanálu",
  modal_connect_name="Název kanálu", modal_connect_ph="Zadejte přesný název",
  btn_lookup="Hledat", btn_join="Připojit", connect_not_found="Kanál «{name}» nenalezen",
  title_add="Vytvořit kanál", title_join="Připojit se", title_settings="Nastavení",
  theme_toggle="Změnit motiv", lang_toggle="Změnit jazyk", uptime_label="Doba běhu",
  online_label="online", err_bad_credentials="Špatné heslo", err_bad_username="Přezdívka 2–32",
  err_bad_password="Heslo min. 4", err_bad_passphrase="Heslo kanálu vyžadováno",
  err_pass_wrong="Dešifrování selhalo", err_generic="Chyba", err_rate_limited="Příliš mnoho požadavků",
  settings_title="Nastavení", settings_account="Účet", settings_appearance="Vzhled",
  settings_users="Uživatelé", settings_show_in_list="Zobrazit mě v seznamu",
  settings_user="Přihlášen jako", settings_logout="Odhlásit", settings_theme="Motiv",
  settings_theme_light="Světlý", settings_theme_dark="Tmavý", settings_lang="Jazyk",
  settings_close="Zavřít", users_list_empty="Žádní uživatelé", users_you="(vy)", users_online="online",
  mobile_channels="Kanály", mention_hint="Napište @ pro zmínku", leave_channel="Opustit",
  lang_picker_title="Jazyk")

I18N["sk"] = _L(login_title="sldchat", login_subtitle="Prihláste sa alebo si vytvorte účet",
  field_nick="Prezývka", field_password="Heslo", ph_nick="Vaša prezývka", ph_password="Vaše heslo",
  btn_login="Pokračovať", btn_wait="Prosím čakajte…", remember_me="Zapamätať si ma",
  logged_as="Prihlásený ako", header_no_channels="Žiadne kanály",
  header_no_channels_sub="Otvorte Kanály", header_msgs="{n} správ",
  empty_no_channels="Zatiaľ žiadne kanály.", empty_no_messages="Žiadne správy. Napíšte prvý!",
  composer_ph="Napíšte správu...", composer_no_channel="Žiadny aktívny kanál", composer_muted="Stlmené: {n}s",
  modal_create_title="Vytvoriť súkromný kanál", modal_name="Názov", modal_name_ph="Napr. práca",
  modal_pass="Heslo kanála", modal_pass_ph="Potrebné na čítanie",
  modal_pass_hint="Iba tí, čo poznajú heslo, môžu dešifrovať. Server ho nevidí.",
  btn_cancel="Zrušiť", btn_create="Vytvoriť", modal_connect_title="Pripojiť sa ku kanálu",
  modal_connect_name="Názov kanála", modal_connect_ph="Zadajte presný názov",
  btn_lookup="Hľadať", btn_join="Pripojiť", connect_not_found="Kanál «{name}» nenájdený",
  title_add="Vytvoriť kanál", title_join="Pripojiť sa", title_settings="Nastavenia",
  theme_toggle="Zmeniť motív", lang_toggle="Zmeniť jazyk", uptime_label="Doba behu",
  online_label="online", err_bad_credentials="Zlé heslo", err_bad_username="Prezývka 2–32",
  err_bad_password="Heslo min. 4", err_bad_passphrase="Heslo kanála povinné",
  err_pass_wrong="Dešifrovanie zlyhalo", err_generic="Chyba", err_rate_limited="Príliš veľa požiadaviek",
  settings_title="Nastavenia", settings_account="Účet", settings_appearance="Vzhľad",
  settings_users="Používatelia", settings_show_in_list="Zobraziť ma v zozname",
  settings_user="Prihlásený ako", settings_logout="Odhlásiť", settings_theme="Motív",
  settings_theme_light="Svetlý", settings_theme_dark="Tmavý", settings_lang="Jazyk",
  settings_close="Zavrieť", users_list_empty="Žiadni používatelia", users_you="(vy)", users_online="online",
  mobile_channels="Kanály", mention_hint="Napíšte @ pre zmienku", leave_channel="Opustiť",
  lang_picker_title="Jazyk")

I18N["bg"] = _L(login_title="sldchat", login_subtitle="Влезте или създайте акаунт",
  field_nick="Псевдоним", field_password="Парола", ph_nick="Псевдоним", ph_password="Парола",
  btn_login="Продължи", btn_wait="Моля изчакайте…", remember_me="Запомни ме",
  logged_as="Влезли сте като", header_no_channels="Няма канали",
  header_no_channels_sub="Отворете Канали", header_msgs="{n} съобщения",
  empty_no_channels="Още нямате канали.", empty_no_messages="Няма съобщения. Напишете първи!",
  composer_ph="Напишете съобщение...", composer_no_channel="Няма активен канал", composer_muted="Заглушен: {n}с",
  modal_create_title="Създайте частен канал", modal_name="Име", modal_name_ph="Напр. работа",
  modal_pass="Парола на канала", modal_pass_ph="Нужна за четене",
  modal_pass_hint="Само тези, които знаят паролата, могат да декриптират. Сървърът не я вижда.",
  btn_cancel="Отказ", btn_create="Създай", modal_connect_title="Присъедини се към канал",
  modal_connect_name="Име на канала", modal_connect_ph="Въведете точното име",
  btn_lookup="Търси", btn_join="Присъедини", connect_not_found="Канал «{name}» не е намерен",
  title_add="Създай канал", title_join="Присъедини се", title_settings="Настройки",
  theme_toggle="Смени тема", lang_toggle="Смени език", uptime_label="Време на работа",
  online_label="онлайн", err_bad_credentials="Грешна парола", err_bad_username="Псевдоним 2–32",
  err_bad_password="Парола мин. 4", err_bad_passphrase="Парола на канала е задължителна",
  err_pass_wrong="Неуспешно декриптиране", err_generic="Грешка", err_rate_limited="Твърде много заявки",
  settings_title="Настройки", settings_account="Акаунт", settings_appearance="Изглед",
  settings_users="Потребители", settings_show_in_list="Покажи ме в списъка",
  settings_user="Влезли сте като", settings_logout="Изход", settings_theme="Тема",
  settings_theme_light="Светла", settings_theme_dark="Тъмна", settings_lang="Език",
  settings_close="Затвори", users_list_empty="Няма потребители", users_you="(вие)", users_online="онлайн",
  mobile_channels="Канали", mention_hint="Напишете @ за споменаване", leave_channel="Напусни",
  lang_picker_title="Език")

# Simple fallbacks for remaining languages: only title/subtitle in the native script.
_SIMPLE = {
  "sr": ("sldchat","Пријавите се или направите налог"),
  "hr": ("sldchat","Prijavite se ili izradite račun"),
  "sl": ("sldchat","Prijavite se ali ustvarite račun"),
  "lt": ("sldchat","Prisijunkite arba susikurkite paskyrą"),
  "lv": ("sldchat","Piesakieties vai izveidojiet kontu"),
  "et": ("sldchat","Logige sisse või looge konto"),
  "is": ("sldchat","Skráðu þig inn eða búðu til reikning"),
  "ms": ("sldchat","Log masuk atau buat akaun"),
  "tl": ("sldchat","Mag-sign in o gumawa ng account"),
  "ur": ("sldchat","سائن ان کریں یا اکاؤنٹ بنائیں"),
  "bn": ("sldchat","সাইন ইন করুন বা অ্যাকাউন্ট তৈরি করুন"),
  "ta": ("sldchat","உள்நுழைக அல்லது கணக்கை உருவாக்கு"),
  "kk": ("sldchat","Кіріңіз немесе тіркелгі жасаңыз"),
  "az": ("sldchat","Daxil olun və ya hesab yaradın"),
  "ka": ("sldchat","შედით ან შექმენით ანგარიში"),
  "hy": ("sldchat","Մուտք գործեք կամ ստեղծեք հաշիվ"),
  "be": ("sldchat","Увайдзіце або стварыце акаўнт"),
}
for code, (t1, t2) in _SIMPLE.items():
    I18N[code] = {"login_title": t1, "login_subtitle": t2}

# ---------- LANG ORDER: 45 languages ----------
LANG_ORDER = [
  "en","ru","uk","be","pl","cs","sk","sl","hr","sr","bg",
  "de","nl","sv","no","da","fi","is","et","lv","lt",
  "fr","it","es","pt","ro","hu","el","tr",
  "ar","he","fa","ur","hi","bn","ta",
  "zh","ja","ko","th","vi","id","ms","tl",
  "kk","az","ka","hy",
]

LANG_NAMES = {
  "en":"English","ru":"Русский","uk":"Українська","be":"Беларуская","pl":"Polski",
  "cs":"Čeština","sk":"Slovenčina","sl":"Slovenščina","hr":"Hrvatski","sr":"Srpski",
  "bg":"Български","de":"Deutsch","nl":"Nederlands","sv":"Svenska","no":"Norsk",
  "da":"Dansk","fi":"Suomi","is":"Íslenska","et":"Eesti","lv":"Latviešu","lt":"Lietuvių",
  "fr":"Français","it":"Italiano","es":"Español","pt":"Português","ro":"Română",
  "hu":"Magyar","el":"Ελληνικά","tr":"Türkçe",
  "ar":"العربية","he":"עברית","fa":"فارسی","ur":"اردو","hi":"हिन्दी","bn":"বাংলা",
  "ta":"தமிழ்",
  "zh":"中文","ja":"日本語","ko":"한국어","th":"ไทย","vi":"Tiếng Việt",
  "id":"Bahasa Indonesia","ms":"Bahasa Melayu","tl":"Filipino",
  "kk":"Қазақша","az":"Azərbaycan","ka":"ქართული","hy":"Հայերեն",
}

# ---------- Flag SVG helpers ----------
def _svg(w, h, body):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="20" height="{round(20*h/w)}" style="border:1px solid rgba(0,0,0,.25)">{body}</svg>'

def _h3(c1, c2, c3):
    h = 20
    return _svg(30, h, f'<rect width="30" height="6.67" fill="{c1}"/><rect y="6.67" width="30" height="6.67" fill="{c2}"/><rect y="13.33" width="30" height="6.67" fill="{c3}"/>')

def _v3(c1, c2, c3):
    return _svg(30, 20, f'<rect width="10" height="20" fill="{c1}"/><rect x="10" width="10" height="20" fill="{c2}"/><rect x="20" width="10" height="20" fill="{c3}"/>')

def _h2(c1, c2):
    return _svg(30, 20, f'<rect width="30" height="10" fill="{c1}"/><rect y="10" width="30" height="10" fill="{c2}"/>')

def _nordic(bg, cross, cross2=None):
    body = f'<rect width="30" height="20" fill="{bg}"/>'
    if cross2:
        body += f'<rect x="9" width="6" height="20" fill="{cross2}"/><rect y="7" width="30" height="6" fill="{cross2}"/>'
    body += f'<rect x="10" width="4" height="20" fill="{cross}"/><rect y="8" width="30" height="4" fill="{cross}"/>'
    return _svg(30, 20, body)

FLAGS = {
  "en": _svg(60, 30, '<rect width="60" height="30" fill="#012169"/><path d="M0 0 L60 30 M60 0 L0 30" stroke="#fff" stroke-width="6"/><path d="M0 0 L60 30 M60 0 L0 30" stroke="#C8102E" stroke-width="3"/><path d="M30 0 V30 M0 15 H60" stroke="#fff" stroke-width="10"/><path d="M30 0 V30 M0 15 H60" stroke="#C8102E" stroke-width="6"/>'),
  "ru": _h3('#fff','#0039A6','#D52B1E'),
  "uk": _h2('#0057B7','#FFDD00'),
  "be": _svg(30, 20, '<rect width="30" height="13.33" fill="#C8313E"/><rect y="13.33" width="30" height="6.67" fill="#4AA657"/>'),
  "pl": _h2('#fff','#DC143C'),
  "cs": _svg(30, 20, '<rect width="30" height="10" fill="#fff"/><rect y="10" width="30" height="10" fill="#D7141A"/><polygon points="0,0 15,10 0,20" fill="#11457E"/>'),
  "sk": _h3('#fff','#0B4EA2','#EE1C25'),
  "sl": _h3('#fff','#005DA4','#ED1C24'),
  "hr": _h3('#FF0000','#fff','#171796'),
  "sr": _h3('#C6363C','#0C4076','#fff'),
  "bg": _h3('#fff','#00966E','#D62612'),
  "de": _h3('#000','#DD0000','#FFCE00'),
  "nl": _h3('#AE1C28','#fff','#21468B'),
  "sv": _nordic('#006AA7','#FECC00'),
  "no": _nordic('#BA0C2F','#fff','#00205B'),
  "da": _nordic('#C60C30','#fff'),
  "fi": _nordic('#fff','#003580'),
  "is": _nordic('#02529C','#fff','#DC1E35'),
  "et": _h3('#0072CE','#000','#fff'),
  "lv": _svg(30, 20, '<rect width="30" height="20" fill="#9E3039"/><rect y="8" width="30" height="4" fill="#fff"/>'),
  "lt": _h3('#FDB913','#006A44','#C1272D'),
  "fr": _v3('#002395','#fff','#ED2939'),
  "it": _v3('#009246','#fff','#CE2B37'),
  "es": _svg(30, 20, '<rect width="30" height="20" fill="#AA151B"/><rect y="5" width="30" height="10" fill="#F1BF00"/>'),
  "pt": _svg(30, 20, '<rect width="12" height="20" fill="#006600"/><rect x="12" width="18" height="20" fill="#FF0000"/><circle cx="12" cy="10" r="4" fill="#FFFF00" stroke="#fff" stroke-width="0.7"/><circle cx="12" cy="10" r="2" fill="#FF0000"/>'),
  "ro": _v3('#002B7F','#FCD116','#CE1126'),
  "hu": _h3('#CE2939','#fff','#477050'),
  "el": _svg(30, 20, '<rect width="30" height="20" fill="#0D5EAF"/><rect y="3" width="30" height="3" fill="#fff"/><rect y="9" width="30" height="3" fill="#fff"/><rect y="15" width="30" height="3" fill="#fff"/><rect width="10" height="10" fill="#0D5EAF"/><rect y="3" width="10" height="2" fill="#fff"/><rect x="4" width="2" height="10" fill="#fff"/>'),
  "tr": _svg(30, 20, '<rect width="30" height="20" fill="#E30A17"/><circle cx="11" cy="10" r="4.5" fill="#fff"/><circle cx="12.8" cy="10" r="3.6" fill="#E30A17"/><polygon points="18,8.4 18.41,9.43 19.52,9.5 18.67,10.22 18.94,11.29 18,10.7 17.06,11.29 17.33,10.22 16.48,9.5 17.59,9.43" fill="#fff"/>'),
  "ar": _svg(30, 20, '<rect width="30" height="20" fill="#006C35"/>'),
  "he": _svg(30, 20, '<rect width="30" height="20" fill="#fff"/><rect y="2" width="30" height="3" fill="#0038B8"/><rect y="15" width="30" height="3" fill="#0038B8"/><polygon points="15,6 18,12 12,12" fill="none" stroke="#0038B8" stroke-width="0.7"/><polygon points="15,14 12,8 18,8" fill="none" stroke="#0038B8" stroke-width="0.7"/>'),
  "fa": _h3('#239F40','#fff','#DA0000'),
  "ur": _svg(30, 20, '<rect width="30" height="20" fill="#01411C"/><circle cx="14" cy="10" r="4" fill="#fff"/><circle cx="15.5" cy="10" r="3.3" fill="#01411C"/><polygon points="19,8.5 20.3,10 19,11.5 18.6,9.9 17,9.5 18.2,8.6" fill="#fff"/>'),
  "hi": _h3('#FF9933','#fff','#138808'),
  "bn": _svg(30, 20, '<rect width="30" height="20" fill="#006A4E"/><circle cx="13" cy="10" r="5.5" fill="#F42A41"/>'),
  "ta": _h3('#FF9933','#fff','#138808'),
  "zh": _svg(30, 20, '<rect width="30" height="20" fill="#DE2910"/><polygon points="5,4 6.2,7.5 3,5.4 7,5.4 3.8,7.5" fill="#FFDE00"/>'),
  "ja": _svg(30, 20, '<rect width="30" height="20" fill="#fff"/><circle cx="15" cy="10" r="6" fill="#BC002D"/>'),
  "ko": _svg(30, 20, '<rect width="30" height="20" fill="#fff"/><circle cx="15" cy="10" r="4" fill="#CD2E3A"/><path d="M11 10 a4 4 0 0 0 8 0 a4 4 0 0 0 -8 0 z" fill="#0047A0" clip-path="inset(0 0 0 50%)"/>'),
  "th": _svg(30, 20, '<rect width="30" height="3.33" fill="#A51931"/><rect y="3.33" width="30" height="3.33" fill="#fff"/><rect y="6.67" width="30" height="6.67" fill="#2D2A4A"/><rect y="13.33" width="30" height="3.33" fill="#fff"/><rect y="16.67" width="30" height="3.33" fill="#A51931"/>'),
  "vi": _svg(30, 20, '<rect width="30" height="20" fill="#DA251D"/><polygon points="15,5 16.9,11.3 23.5,11.3 18.2,15.1 20.2,21 15,17.3 9.8,21 11.8,15.1 6.5,11.3 13.1,11.3" fill="#FFDA00"/>'),
  "id": _h2('#CE1126','#fff'),
  "ms": _svg(30, 20, '<rect width="30" height="20" fill="#fff"/><rect width="30" height="2.86" fill="#CC0001"/><rect y="5.71" width="30" height="2.86" fill="#CC0001"/><rect y="11.43" width="30" height="2.86" fill="#CC0001"/><rect y="17.14" width="30" height="2.86" fill="#CC0001"/><rect width="15" height="10" fill="#010066"/><circle cx="6" cy="5" r="2.2" fill="#FFCC00"/><circle cx="7" cy="5" r="2.2" fill="#010066"/>'),
  "tl": _svg(30, 20, '<rect width="30" height="10" fill="#0038A8"/><rect y="10" width="30" height="10" fill="#CE1126"/><polygon points="0,0 12,10 0,20" fill="#fff"/><circle cx="4" cy="10" r="1.6" fill="#FCD116"/>'),
  "kk": _svg(30, 20, '<rect width="30" height="20" fill="#00ABC2"/><circle cx="15" cy="10" r="3" fill="#FEC50C"/>'),
  "az": _h3('#0092BC','#E4002B','#00AE65'),
  "ka": _svg(30, 20, '<rect width="30" height="20" fill="#fff"/><rect x="10" width="10" height="20" fill="#FF0000"/><rect y="7" width="30" height="6" fill="#FF0000"/>'),
  "hy": _h3('#D90012','#0033A0','#F2A800'),
}

# ============================================================
#  HTML
# ============================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0088cc">
<meta name="robots" content="noindex, nofollow">
<title>sldchat</title>
<link rel="stylesheet" href="https://getbootstrap.com/1.4.0/assets/css/bootstrap.min.css">
<style>
* { scrollbar-width: none; -ms-overflow-style: none; -webkit-text-size-adjust: 100%; -webkit-tap-highlight-color: transparent; }
*::-webkit-scrollbar { display: none !important; }
html, body { margin: 0; height: 100%; }
body {
  background: #f5f5f5;
  font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 13px; color: #333; overflow: hidden;
  height: var(--app-vh, 100vh);
  -webkit-user-select: none; user-select: none;
  -webkit-touch-callout: none;
}
input, textarea { -webkit-user-select: text; user-select: text; font-family: inherit; }
svg { display: inline-block; vertical-align: middle; }
button, .channel-tab, .icon-btn-tab, .settings-tab, .mcp-item, .lang-menu-item {
  touch-action: manipulation; -webkit-tap-highlight-color: transparent;
}
@media print { body { display: none !important; } }

/* Login */
.login-screen { position: fixed; inset: 0; height: var(--app-vh, 100vh);
  background: #e9eef3; background-image: linear-gradient(#f5f8fb, #dfe6ee);
  display: flex; align-items: center; justify-content: center;
  padding: 16px; box-sizing: border-box; overflow-y: auto; z-index: 10; }
.login-box { width: 360px; max-width: 100%; background: #fff; border: 1px solid #b8c4d0;
  box-shadow: 0 4px 16px rgba(0,0,0,.15); padding: 22px 20px 16px;
  text-align: center; margin: auto; box-sizing: border-box; }
.login-box h2 { margin: 0 0 4px; font-size: 20px; color: #2b3d51; }
.login-box p { margin: 0 0 18px; color: #7b8a99; font-size: 12.5px; }
.my-label { display: block; text-align: left; font-size: 11px; font-weight: bold;
  color: #667788; margin: 0 0 4px; padding: 0; line-height: 1.4;
  text-transform: uppercase; letter-spacing: .3px; }
.field-group { margin-bottom: 12px; text-align: left; }
.my-input { display: block !important; width: 100% !important; box-sizing: border-box !important;
  padding: 9px 12px !important; border: 1px solid #b8c4d0 !important; font-size: 14px !important;
  background: #fff !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.08) !important;
  color: #333 !important; outline: none !important; border-radius: 0 !important; }
.my-input:focus { border-color: #0088cc !important; }
.login-box .btn { width: 100%; margin-top: 6px; padding: 12px; font-size: 15px; }
.btn[disabled] { opacity: .65; cursor: not-allowed; }
.login-error { margin-top: 10px; font-size: 12px; color: #a94442; background: #fcebeb;
  border: 1px solid #f5c6c6; padding: 7px 10px; display: none; text-align: left; }
.login-error.show { display: block; }
.uptime-line { margin-top: 14px; padding-top: 10px; border-top: 1px solid #e0e5eb;
  font-size: 11.5px; color: #7b8a99; display: flex; justify-content: space-between; }
.uptime-line .u-val { font-weight: bold; color: #4a5a6a; font-family: "Courier New", monospace; }
.remember-row { display: flex; align-items: flex-start; font-size: 13px;
  color: #4a5a6a; margin: 10px 0 4px; cursor: pointer; gap: 8px; }
.remember-row input { width: 16px; height: 16px; margin: 1px 0 0; flex-shrink: 0; }
.login-settings { margin-top: 12px; padding-top: 10px; border-top: 1px solid #e0e5eb;
  display: flex; gap: 6px; }
.settings-btn { flex: 1; display: inline-flex; align-items: center; justify-content: center;
  height: 34px; padding: 0 10px; border: 1px solid #b8c4d0;
  background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6);
  color: #333; cursor: pointer; font-size: 13px; border-radius: 0; }
.settings-btn:hover { background: #d9d9d9; }
.settings-btn .lang-code { margin-left: 6px; font-size: 12px; font-weight: bold; }
.spinner { display: inline-block; width: 14px; height: 14px;
  border: 2px solid rgba(255,255,255,.5); border-top-color: #fff;
  border-radius: 50%; animation: spin .7s linear infinite; vertical-align: -2px; margin-right: 6px; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Language picker */
.lang-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.5); z-index: 9998; display: none; }
.lang-backdrop.open { display: block; }
.lang-menu { position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%);
  background: #fff; border: 1px solid #666; box-shadow: 0 8px 30px rgba(0,0,0,.5);
  padding: 10px; z-index: 9999; display: none;
  grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px;
  max-width: calc(100vw - 20px); max-height: calc(100vh - 20px);
  overflow-y: auto; box-sizing: border-box; width: 780px; }
.lang-menu.open { display: grid; }
.lang-menu-header { grid-column: 1 / -1; padding: 4px 4px 10px;
  font-size: 15px; font-weight: bold; color: #222;
  border-bottom: 1px solid #eee; margin-bottom: 4px;
  display: flex; align-items: center; justify-content: space-between; }
.lang-menu-close { cursor: pointer; padding: 4px; color: #666; }
.lang-menu-close:hover { color: #c00; }
.lang-menu-item { display: flex; align-items: center; padding: 8px 10px; cursor: pointer;
  font-size: 12.5px; color: #333; gap: 8px; border: 1px solid #ddd;
  background: #fafafa; min-width: 0; box-sizing: border-box; }
.lang-menu-item:hover { background: #eaf4fb; border-color: #8ab4dc; }
.lang-menu-item.active { background: #d6e8f7; color: #004a80; font-weight: bold; border-color: #4a90c2; }
.lang-menu-item .flag-wrap { flex-shrink: 0; display: inline-flex; align-items: center; }
.lang-menu-item .lang-name { flex: 1; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.lang-menu-item .check { flex-shrink: 0; color: #0088cc; font-weight: bold; visibility: hidden; }
.lang-menu-item.active .check { visibility: visible; }

/* App */
.app { width: 100%; height: var(--app-vh, 100vh); background: #fff;
  display: flex; flex-direction: column; overflow: hidden; }
.mobile-topbar { display: none; }
.mobile-channels-backdrop, .mobile-channels-panel { display: none; }
.tabs-bar { display: flex; align-items: center; padding: 6px 8px;
  background: #f5f5f5; border-bottom: 1px solid #ccc; flex-shrink: 0; gap: 4px; }
.tabs-scroll { display: flex; align-items: center; flex: 1 1 0; min-width: 0;
  overflow-x: auto; padding-bottom: 1px; scroll-behavior: smooth; }
.scroll-arrow { width: 22px; height: 28px; border: 1px solid #bbb; background: #e6e6e6;
  color: #555; cursor: pointer; padding: 0; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center; }
.scroll-arrow:hover { background: #d9d9d9; }
.channel-tab { display: inline-flex; align-items: center; padding: 5px 12px; margin-right: 4px;
  border: 1px solid #bbb; background: #e6e6e6; color: #444; font-size: 13px;
  white-space: nowrap; cursor: pointer; flex-shrink: 0;
  font-family: "Courier New", Courier, monospace; }
.channel-tab:hover { background: #d9d9d9; }
.channel-tab.active { background: #006dcc; color: #fff; border-color: #003f81; }
.channel-tab .lock-ico { margin-right: 5px; opacity: .8; display: inline-flex; }
.channel-tab .close-x { margin-left: 8px; cursor: pointer; opacity: .6;
  display: inline-flex; color: inherit; padding: 2px; }
.channel-tab .close-x:hover { opacity: 1; color: #c00; }
.icon-btn-tab { width: 28px; height: 28px; border: 1px solid #bbb;
  background: #e6e6e6; color: #555; cursor: pointer; padding: 0;
  display: inline-flex; align-items: center; justify-content: center;
  flex-shrink: 0; }
.icon-btn-tab:hover { background: #d9d9d9; }
.top-sep { width: 1px; height: 20px; background: #ccc; margin: 0 4px; flex-shrink: 0; }
.chat-header { display: flex; align-items: center; padding: 10px 14px;
  background: #f5f5f5; border-bottom: 1px solid #ccc; flex-shrink: 0; gap: 8px; }
.chat-header .title { font-weight: bold; font-size: 15px; line-height: 1.15; color: #222; }
.chat-header .subtitle { font-size: 11.5px; color: #777; margin-top: 2px; }
.chat-header .user-info { margin-left: auto; display: flex; align-items: center;
  gap: 8px; font-size: 11.5px; color: #666; }
.chat-header .user-info .nick { font-weight: bold; color: #2b3d51; }
.chat-feed { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch;
  padding: 10px 14px; background: #fdfdfd; }
.empty-state { margin: auto; text-align: center; color: #aaa; font-size: 13px; padding-top: 60px; }
.msg-row { display: grid; grid-template-columns: minmax(70px, 130px) 1fr;
  column-gap: 8px; padding: 3px 4px; align-items: start;
  font-size: 13.5px; line-height: 1.5; word-wrap: break-word; }
.msg-row:hover { background: #f2f6fa; }
.msg-row.grouped { padding-top: 0; }
.msg-author { font-weight: bold; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.msg-author.hidden { visibility: hidden; }
.msg-content { min-width: 0; word-break: break-word; overflow-wrap: anywhere; }
.msg-text { color: #222; }
.msg-time { color: #b0b8c0; font-size: 11px; margin-left: 6px; white-space: nowrap; }
.mention { background: #e1eefb; color: #005a9e; font-weight: bold;
  padding: 0 3px; border-radius: 3px; }
.msg-system { color: #a94442; background: #fcebeb; border: 1px solid #f5c6c6;
  font-size: 12px; padding: 5px 10px; margin: 6px 0; }
.mention-pop { position: absolute; z-index: 40;
  background: #fff; border: 1px solid #bbb; box-shadow: 0 4px 12px rgba(0,0,0,.2);
  max-height: 240px; overflow-y: auto; min-width: 180px; display: none; }
.mention-pop.open { display: block; }
.mention-pop-item { padding: 10px 14px; cursor: pointer; font-size: 13.5px; color: #333; }
.mention-pop-item:hover, .mention-pop-item.active { background: #eaf4fb; }
.composer { display: flex; align-items: flex-end; gap: 6px; padding: 8px 12px;
  background: #f5f5f5; border-top: 1px solid #ccc; flex-shrink: 0; position: relative;
  padding-bottom: max(8px, env(safe-area-inset-bottom)); }
.composer textarea { flex: 1; resize: none; padding: 8px 12px !important;
  border: 1px solid #bbb !important; font-size: 14px !important; max-height: 140px;
  outline: none !important; background: #fff !important; color: #333 !important;
  box-sizing: border-box !important; margin: 0 !important; overflow: hidden; }
.composer textarea:focus { border-color: #0088cc !important; }
.composer textarea[disabled] { background: #f7e6e6 !important; color: #a94442 !important; }
.composer .icon-btn { width: 40px; height: 40px; padding: 0; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center; }
.my-modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.45);
  z-index: 1000; display: none; justify-content: center; align-items: center;
  padding: 20px; box-sizing: border-box; }
.my-modal-backdrop.open { display: flex; }
.my-modal { width: 420px; max-width: 100%; background: #fff; border: 1px solid #666;
  box-shadow: 0 5px 20px rgba(0,0,0,.4); }
.modal-head { padding: 12px 14px; background: #f5f5f5; border-bottom: 1px solid #ccc;
  font-weight: bold; font-size: 15px; display: flex; align-items: center; }
.modal-head .close-m { margin-left: auto; cursor: pointer; color: #666;
  padding: 6px; display: inline-flex; }
.modal-head .close-m:hover { color: #c00; }
.modal-body { padding: 16px; text-align: left; }
.modal-foot { padding: 12px; background: #f7f7f7; border-top: 1px solid #e5e5e5; text-align: right; }
.modal-foot .btn { margin-left: 6px; padding: 10px 20px; font-size: 14px; }
.field-hint { font-size: 11.5px; color: #7b8a99; margin-top: 5px; line-height: 1.4; }
.error-msg { margin-top: 10px; font-size: 12px; color: #a94442; background: #fcebeb;
  border: 1px solid #f5c6c6; padding: 6px 10px; display: none; }
.error-msg.show { display: block; }
#settingsModal { width: 560px; }
.settings-body { display: flex; min-height: 260px; }
.settings-tabs { width: 160px; background: #f5f5f5; border-right: 1px solid #ddd;
  padding: 8px 0; flex-shrink: 0; }
.settings-tab { display: block; width: 100%; text-align: left; padding: 11px 14px;
  background: transparent; border: 0; border-left: 3px solid transparent;
  cursor: pointer; font-size: 13px; color: #444; }
.settings-tab:hover { background: #e9ecef; }
.settings-tab.active { background: #fff; border-left-color: #0088cc;
  color: #006dcc; font-weight: bold; }
.settings-content { flex: 1; padding: 18px; overflow-y: auto; max-height: 60vh; }
.settings-pane { display: none; }
.settings-pane.active { display: block; }
.settings-row { margin-bottom: 16px; }
.settings-label { font-size: 11px; text-transform: uppercase; letter-spacing: .5px;
  color: #7b8a99; font-weight: bold; margin-bottom: 5px; }
.settings-value { font-size: 14px; color: #222; font-weight: bold; }
.settings-section-title { font-size: 14px; font-weight: bold; color: #222;
  padding-bottom: 8px; border-bottom: 1px solid #eee; margin-bottom: 14px; }
.settings-toggle-row { display: flex; align-items: flex-start; gap: 10px;
  padding: 10px 0; font-size: 14px; color: #333; cursor: pointer; }
.settings-toggle-row input { width: 18px; height: 18px; margin: 1px 0 0; flex-shrink: 0; }
.users-list { display: flex; flex-direction: column; }
.user-list-item { display: flex; align-items: center; gap: 10px; padding: 10px 4px;
  border-bottom: 1px solid #f0f0f0; font-size: 14px; }
.user-list-item:last-child { border-bottom: 0; }
.user-dot { width: 10px; height: 10px; border-radius: 50%; background: #bbb; flex-shrink: 0; }
.user-dot.online { background: #4caf50; }
.user-list-item .user-name { flex: 1; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; font-weight: bold; }
.user-list-item .user-you { font-size: 12px; color: #999; margin-left: 4px; font-weight: normal; }
.users-empty { padding: 30px 10px; text-align: center; color: #999; font-size: 13px; }
/* Dark */
body.dark { background: #1a1a1a; color: #ccc; }
body.dark .login-screen { background: #1a1a1a; background-image: none; }
body.dark .login-box { background: #252526; border-color: #3c3c3c; }
body.dark .login-box h2 { color: #eaeaea; }
body.dark .login-box p { color: #888; }
body.dark .my-label { color: #888 !important; }
body.dark .my-input { background: #1e1e1e !important; color: #ddd !important; border-color: #4a4a4c !important; }
body.dark .login-error { background: #3a1f1f; border-color: #5a2a2a; color: #e0a0a0; }
body.dark .login-settings, body.dark .uptime-line { border-top-color: #3c3c3c; }
body.dark .settings-btn { background: #37373d; background-image: none; color: #ccc; border-color: #4a4a4c; }
body.dark .uptime-line { color: #888; }
body.dark .remember-row { color: #aaa; }
body.dark .app { background: #252526; }
body.dark .tabs-bar, body.dark .chat-header, body.dark .composer, body.dark .mobile-topbar {
  background: #2d2d30; border-color: #3c3c3c; }
body.dark .channel-tab, body.dark .icon-btn-tab, body.dark .scroll-arrow, body.dark .mobile-channels-btn {
  background: #37373d; color: #ccc; border-color: #4a4a4c; }
body.dark .channel-tab.active { background: #0e639c; border-color: #0e639c; color: #fff; }
body.dark .top-sep { background: #4a4a4c; }
body.dark .chat-header .title { color: #eaeaea; }
body.dark .chat-feed { background: #1e1e1e; }
body.dark .msg-text { color: #ddd; }
body.dark .msg-row:hover { background: #2a2d33; }
body.dark .msg-system { background: #3a1f1f; border-color: #5a2a2a; color: #e0a0a0; }
body.dark .composer textarea { background: #1e1e1e !important; color: #ddd !important; border-color: #4a4a4c !important; }
body.dark .my-modal { background: #252526; border-color: #3c3c3c; }
body.dark .modal-head { background: #2d2d30; border-color: #3c3c3c; color: #eaeaea; }
body.dark .modal-foot { background: #2a2a2c; border-color: #3c3c3c; }
body.dark .btn { background: #37373d; color: #ccc; border-color: #4a4a4c; }
body.dark .btn.primary { background: #0e639c; color: #fff; border-color: #0e639c; }
body.dark .settings-tabs { background: #2d2d30; border-right-color: #3c3c3c; }
body.dark .settings-tab { color: #ccc; }
body.dark .settings-tab.active { background: #252526; border-left-color: #0e639c; color: #6cb6ff; }
body.dark .settings-label { color: #777; }
body.dark .settings-value { color: #eaeaea; }
body.dark .settings-section-title { color: #eaeaea; border-bottom-color: #3c3c3c; }
body.dark .user-list-item { border-bottom-color: #3c3c3c; }
body.dark .lang-menu { background: #252526; border-color: #3c3c3c; }
body.dark .lang-menu-header { color: #eaeaea; border-bottom-color: #3c3c3c; }
body.dark .lang-menu-item { color: #ccc; background: #2d2d30; border-color: #3c3c3c; }
body.dark .lang-menu-item:hover { background: #37373d; border-color: #4a4a4c; }
body.dark .lang-menu-item.active { background: #0e639c; color: #fff; border-color: #0e639c; }
body.dark .lang-menu-item .check { color: #6cb6ff; }
body.dark .mention-pop { background: #252526; border-color: #3c3c3c; color: #ddd; }
body.dark .mention-pop-item { color: #ddd; }
body.dark .mention-pop-item:hover, body.dark .mention-pop-item.active { background: #37373d; }
body.dark .mobile-channels-panel { background: #252526; border-color: #3c3c3c; }
body.dark .mcp-head { background: #2d2d30; border-color: #3c3c3c; color: #eaeaea; }
body.dark .mcp-item { border-color: #3c3c3c; color: #ccc; }
body.dark .mcp-item.active { background: #0e639c; color: #fff; }
body.dark .mcp-actions { background: #2a2a2c; border-color: #3c3c3c; }

@media (hover: none) {
  .channel-tab:active { background: #c9c9c9; }
  .icon-btn-tab:active, .scroll-arrow:active, .settings-btn:active, .lang-menu-item:active { background: #c9c9c9; }
  .msg-row:hover { background: transparent; }
}

@media (max-width: 768px) {
  .login-screen { align-items: flex-start; padding-top: 40px; padding-bottom: 40px; }
  .login-box { width: 100%; max-width: 420px; padding: 24px 18px 18px; }
  .login-box h2 { font-size: 22px; }
  .my-input { font-size: 16px !important; padding: 12px 14px !important; }
  .login-box .btn { padding: 14px; font-size: 16px; }
  .settings-btn { height: 44px; font-size: 14px; }
  .remember-row { font-size: 14px; }
  .tabs-bar { display: none !important; }
  .mobile-topbar { display: flex; align-items: center; gap: 8px;
    padding: 10px 12px; background: #f5f5f5; border-bottom: 1px solid #ccc; flex-shrink: 0; }
  .mobile-channels-btn { flex: 1; display: inline-flex; align-items: center; gap: 10px;
    padding: 12px 14px; border: 1px solid #bbb; background: #e6e6e6;
    color: #333; cursor: pointer; font-family: "Courier New", Courier, monospace;
    font-size: 15px; font-weight: bold; text-align: left; min-height: 48px; }
  .mobile-channels-btn svg { width: 20px !important; height: 20px !important; flex-shrink: 0; }
  .mobile-settings-btn { width: 48px; height: 48px; padding: 0;
    display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .mobile-settings-btn svg { width: 20px !important; height: 20px !important; }
  .chat-header { padding: 12px 14px; }
  .chat-header .title { font-size: 16px; }
  .chat-header .subtitle { font-size: 12px; }
  .chat-header .user-info { display: none; }
  .chat-feed { padding: 8px 10px; }
  .msg-row { grid-template-columns: minmax(52px, 78px) 1fr;
    column-gap: 6px; padding: 4px 3px; font-size: 15px; }
  .msg-author { font-size: 14px; }
  .msg-time { font-size: 11.5px; }
  .composer { padding: 10px 10px; gap: 8px; }
  .composer textarea { font-size: 16px !important; padding: 12px 14px !important; }
  .composer .icon-btn { width: 48px; height: 48px; }
  .composer .icon-btn svg { width: 22px !important; height: 22px !important; }
  .mention-pop-item { padding: 14px 16px; font-size: 15px; }
  .my-modal-backdrop { padding: 10px; align-items: flex-start;
    padding-top: 20px; padding-bottom: 20px; overflow-y: auto; }
  .my-modal { width: 100%; max-width: 500px; margin: auto; }
  #settingsModal { width: 100%; max-width: 500px; }
  .modal-head { padding: 14px 16px; font-size: 16px; }
  .modal-body { padding: 18px; }
  .modal-foot { padding: 14px; }
  .modal-foot .btn { padding: 12px 22px; font-size: 15px; min-height: 46px; }
  .settings-body { flex-direction: column; min-height: 0; }
  .settings-tabs { width: 100%; border-right: 0; border-bottom: 1px solid #ddd;
    padding: 0; display: flex; }
  .settings-tab { flex: 1; text-align: center; padding: 14px 4px;
    border-left: 0; border-bottom: 3px solid transparent; font-size: 13px; }
  .settings-tab.active { border-left-color: transparent; border-bottom-color: #0088cc; }
  body.dark .settings-tab.active { border-left-color: transparent; border-bottom-color: #0e639c; }
  .settings-content { padding: 16px; max-height: 55vh; }
  .settings-toggle-row { font-size: 15px; padding: 12px 0; }
  .settings-toggle-row input { width: 22px; height: 22px; }
  /* Language picker on mobile — 2 columns */
  .lang-menu { width: calc(100vw - 20px); grid-template-columns: repeat(2, minmax(0, 1fr));
    padding: 8px; gap: 5px; max-height: calc(100vh - 20px); }
  .lang-menu-item { padding: 10px; font-size: 13px; }
  .mobile-channels-backdrop { display: block; position: fixed; inset: 0;
    background: rgba(0,0,0,.45); z-index: 900;
    opacity: 0; pointer-events: none; transition: opacity .2s; }
  .mobile-channels-backdrop.open { opacity: 1; pointer-events: auto; }
  .mobile-channels-panel { display: flex; flex-direction: column;
    position: fixed; top: 0; left: 0; bottom: 0;
    width: 88%; max-width: 360px; background: #fff; z-index: 901;
    box-shadow: 4px 0 20px rgba(0,0,0,.35);
    transform: translateX(-100%); transition: transform .2s; pointer-events: none;
    border-right: 1px solid #b8c4d0;
    padding-top: env(safe-area-inset-top); padding-bottom: env(safe-area-inset-bottom); }
  .mobile-channels-panel.open { transform: translateX(0); pointer-events: auto; }
  .mcp-head { padding: 16px 18px; flex-shrink: 0; border-bottom: 1px solid #ccc;
    background: #f5f5f5; display: flex; align-items: center;
    font-family: "Courier New", Courier, monospace; font-weight: bold;
    font-size: 16px; color: #222; }
  .mcp-close { margin-left: auto; background: transparent; border: 0; cursor: pointer;
    padding: 8px; color: #666; display: inline-flex; }
  .mcp-list { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 4px 0; }
  .mcp-item { display: flex; align-items: center; gap: 10px;
    padding: 16px 16px; border-bottom: 1px solid #f0f0f0;
    cursor: pointer; font-family: "Courier New", Courier, monospace;
    font-size: 15px; color: #333; min-height: 56px; box-sizing: border-box; }
  .mcp-item:active { background: #eaf4fb; }
  .mcp-item.active { background: #d6e8f7; color: #004a80; font-weight: bold; }
  .mcp-item .lock-ico { display: inline-flex; flex-shrink: 0; }
  .mcp-item .mcp-name { flex: 1; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .mcp-item .mcp-leave { margin-left: 6px; padding: 8px; cursor: pointer;
    color: #c00; opacity: .7; display: inline-flex; flex-shrink: 0; }
  .mcp-item .mcp-leave:active { opacity: 1; }
  .mcp-empty { padding: 40px 16px; text-align: center; color: #999; font-size: 14px; }
  .mcp-actions { padding: 14px; border-top: 1px solid #ccc;
    background: #f7f7f7; flex-shrink: 0; display: flex; gap: 8px; }
  .mcp-actions .btn { flex: 1; padding: 14px 10px; font-size: 13px;
    min-height: 50px; }
}

@media (min-width: 769px) and (max-width: 1024px) {
  .lang-menu { grid-template-columns: repeat(3, minmax(0, 1fr)); width: 700px; }
}
@media (max-width: 400px) {
  .msg-row { grid-template-columns: minmax(46px, 66px) 1fr; column-gap: 5px; }
  .lang-menu { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

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

<!-- ============ LANGUAGE PICKER (added!) ============ -->
<div class="lang-backdrop" id="langBackdrop"></div>
<div class="lang-menu" id="langMenu"></div>

<div class="app" id="app" style="display:none">
  <div class="mobile-topbar">
    <button class="mobile-channels-btn" id="mobileChannelsBtn" type="button">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>
        <line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/>
        <line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>
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
    <button class="icon-btn-tab" id="addTabBtn" title="">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
    </button>
    <button class="icon-btn-tab" id="connectBtn" title="">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>
        <polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>
      </svg>
    </button>
    <div class="top-sep"></div>
    <button class="icon-btn-tab" id="settingsBtn" title="">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
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
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
    </button>
    <div class="mention-pop" id="mentionPop"></div>
  </div>
</div>

<div class="mobile-channels-backdrop" id="mobileChannelsBackdrop"></div>
<div class="mobile-channels-panel" id="mobileChannelsPanel">
  <div class="mcp-head">
    <span id="mcpTitle">Channels</span>
    <button class="mcp-close" id="mcpClose" type="button">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="mcp-list" id="mcpList"></div>
  <div class="mcp-actions">
    <button class="btn primary" id="mcpCreateBtn" type="button"></button>
    <button class="btn" id="mcpConnectBtn" type="button"></button>
  </div>
</div>

<div class="my-modal-backdrop" id="createBackdrop">
  <div class="my-modal">
    <div class="modal-head">
      <span id="createModalTitle"></span>
      <span class="close-m" data-close-modal="create">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </span>
    </div>
    <div class="modal-body">
      <label class="my-label" id="lblCreateName" for="newChannelName"></label>
      <input type="text" id="newChannelName" class="my-input" maxlength="40" autocomplete="off">
      <div class="field-group" style="margin-top:12px;">
        <label class="my-label" id="lblCreatePass" for="newChannelPass"></label>
        <input type="password" id="newChannelPass" class="my-input" maxlength="256" autocomplete="new-password">
        <div class="field-hint" id="createPassHint"></div>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" type="button" data-close-modal="create" id="btnCancelCreate"></button>
      <button class="btn primary" id="createChannelBtn" type="button"></button>
    </div>
  </div>
</div>

<div class="my-modal-backdrop" id="connectBackdrop">
  <div class="my-modal">
    <div class="modal-head">
      <span id="connectModalTitle"></span>
      <span class="close-m" data-close-modal="connect">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </span>
    </div>
    <div class="modal-body">
      <label class="my-label" id="lblConnectName" for="connectChannelName"></label>
      <input type="text" id="connectChannelName" class="my-input" maxlength="40" autocomplete="off">
      <div class="field-group" style="margin-top:12px;">
        <label class="my-label" id="lblConnectPass" for="connectChannelPass"></label>
        <input type="password" id="connectChannelPass" class="my-input" maxlength="256" autocomplete="new-password">
      </div>
      <div class="error-msg" id="connectError"><span id="connectErrorText"></span></div>
    </div>
    <div class="modal-foot">
      <button class="btn" type="button" data-close-modal="connect" id="btnCancelConnect"></button>
      <button class="btn primary" id="connectChannelBtn" type="button"></button>
    </div>
  </div>
</div>

<div class="my-modal-backdrop" id="settingsBackdrop">
  <div class="my-modal" id="settingsModal">
    <div class="modal-head">
      <span id="settingsTitle"></span>
      <span class="close-m" data-close-modal="settings">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
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
            <div class="settings-label" id="settingsUserLabel"></div>
            <div class="settings-value" id="settingsUser">—</div>
          </div>
          <label class="settings-toggle-row">
            <input type="checkbox" id="showInListToggle">
            <span id="showInListLabel"></span>
          </label>
          <button class="btn" id="settingsLogoutBtn" type="button" style="width:100%; margin-top:14px; padding:12px;"></button>
        </div>
        <div class="settings-pane" data-pane="appearance">
          <div class="settings-section-title" id="settingsAppearanceHead"></div>
          <div class="settings-row">
            <div class="settings-label" id="settingsThemeLabel"></div>
            <div class="settings-value" id="settingsThemeVal">—</div>
            <button class="btn" id="settingsThemeToggle" type="button" style="margin-top:8px;"></button>
          </div>
          <div class="settings-row">
            <div class="settings-label" id="settingsLangLabel"></div>
            <button class="btn" id="settingsLangBtn" type="button" style="margin-top:8px; width:100%; padding:12px; text-align:left;"></button>
          </div>
        </div>
        <div class="settings-pane" data-pane="users">
          <div class="settings-section-title" id="settingsUsersHead">Users</div>
          <div class="users-list" id="usersList"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
"use strict";
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
  try { console.log = noop; console.info = noop; console.warn = noop; console.debug = noop;
        console.error = noop; console.trace = noop; console.dir = noop; console.table = noop; } catch(e){}
  setInterval(() => { try { console.clear && console.clear(); } catch(e){} }, 4000);
})();
</script>

<script>
"use strict";
const I18N = %%I18N%%;
const FLAGS = %%FLAGS%%;
const LANG_ORDER = %%LANG_ORDER%%;
const LANG_NAMES = %%LANG_NAMES%%;
const PBKDF2_ITERATIONS = 250000;

const SVG = {
  lock:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
  x:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
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
    const r = Math.random()*16|0, v = c === 'x' ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function b64(buf){let s='';const b=new Uint8Array(buf);for(let i=0;i<b.length;i++)s+=String.fromCharCode(b[i]);return btoa(s);}
function ub64(str){const bin=atob(str);const b=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)b[i]=bin.charCodeAt(i);return b;}

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

let currentLang = localStorage.getItem('lang') || 'ru';
if (LANG_ORDER.indexOf(currentLang) < 0) currentLang = 'en';
let currentTheme = localStorage.getItem('theme') || 'light';
let authToken = null;
let currentUser = null;
let channels = [];
let activeId = null;
let ws = null;
let wsChannelId = null;
let channelKeys = {};
let channelPasses = {};
let channelSalt = {};
let channelMembers = {};
let memberSetByChannel = {};
let onlineUsers = [];
let uptimeBase = 0, uptimeFetchAt = 0;
let showInListPref = true;
let mentionState = { open:false, items:[], selected:0, startIdx:-1 };
let authPending = false;

const $ = (id) => document.getElementById(id);
const t = (key, vars) => {
  const dict = I18N[currentLang] || {};
  let s = dict[key];
  if (s === undefined) s = I18N.en[key];
  if (s === undefined) s = key;
  if (vars) for (const k in vars) s = s.replace(new RegExp('\\{'+k+'\\}','g'), vars[k]);
  return s;
};

/* =============== REAL E2EE =============== */
async function deriveChannelKey(passphrase, channelId, channelName, saltB64) {
  const salt = ub64(saltB64);
  const material = new TextEncoder().encode(
    (passphrase || '') + '\x00' + String(channelName).toLowerCase() + '\x00' + String(channelId)
  );
  const baseKey = await crypto.subtle.importKey('raw', material, 'PBKDF2', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits(
    { name: 'PBKDF2', salt, iterations: PBKDF2_ITERATIONS, hash: 'SHA-256' },
    baseKey, 256
  );
  return crypto.subtle.importKey('raw', bits, { name:'AES-GCM', length: 256 }, false, ['encrypt','decrypt']);
}
async function encryptText(cid, text) {
  const key = channelKeys[cid];
  if (!key) throw new Error('no key');
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name:'AES-GCM', iv }, key, new TextEncoder().encode(text));
  return b64(iv) + '.' + b64(ct);
}
async function decryptText(cid, payload) {
  try {
    const key = channelKeys[cid];
    if (!key) return null;
    const parts = String(payload).split('.');
    if (parts.length !== 2) return null;
    const pt = await crypto.subtle.decrypt({ name:'AES-GCM', iv: ub64(parts[0]) }, key, ub64(parts[1]));
    return new TextDecoder().decode(pt);
  } catch (e) { return null; }
}

async function api(path, method='GET', body=null, withAuth=true) {
  const headers = { 'Content-Type': 'application/json' };
  if (withAuth && authToken) headers['x-auth-token'] = authToken;
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : null });
  let data = null; try { data = await res.json(); } catch(e){}
  if (!res.ok) throw { status: res.status, detail: (data && data.detail) || 'error' };
  return data;
}

function fmtUptime(s) {
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s/86400), h = Math.floor((s%86400)/3600), m = Math.floor((s%3600)/60), sec = s%60;
  const p = n => String(n).padStart(2,'0');
  return (d>0 ? d+'d ' : '') + p(h)+':'+p(m)+':'+p(sec);
}
async function refreshUptime() {
  try { const r = await fetch('/api/uptime'); const d = await r.json();
    uptimeBase = d.uptime; uptimeFetchAt = performance.now(); } catch(e){}
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
  const v = $('settingsThemeVal'); if (v) v.textContent = currentTheme === 'dark' ? t('settings_theme_dark') : t('settings_theme_light');
  const b = $('settingsThemeToggle'); if (b) b.textContent = currentTheme === 'dark' ? t('settings_theme_light') : t('settings_theme_dark');
}
function toggleTheme() { currentTheme = currentTheme === 'dark' ? 'light' : 'dark'; applyTheme(); }

/* ============== Language picker (FIX) ============== */
function buildLangMenu() {
  const menu = $('langMenu'); if (!menu) return;
  menu.innerHTML = '';
  const header = document.createElement('div');
  header.className = 'lang-menu-header';
  header.innerHTML = '<span>' + t('lang_picker_title') + '</span>';
  const close = document.createElement('span');
  close.className = 'lang-menu-close';
  close.innerHTML = SVG.x;
  close.addEventListener('click', closeLangMenu);
  header.appendChild(close);
  menu.appendChild(header);

  LANG_ORDER.forEach(code => {
    const item = document.createElement('div');
    item.className = 'lang-menu-item' + (code === currentLang ? ' active' : '');
    item.innerHTML = '<span class="flag-wrap">' + (FLAGS[code] || '') + '</span>' +
                     '<span class="lang-name">' + (LANG_NAMES[code] || code) + '</span>' +
                     '<span class="check">✓</span>';
    item.addEventListener('click', e => {
      e.stopPropagation();
      currentLang = code;
      localStorage.setItem('lang', code);
      closeLangMenu();
      applyLanguage();
    });
    menu.appendChild(item);
  });
}
function openLangMenu() {
  const m = $('langMenu'); const b = $('langBackdrop');
  if (!m || !b) { window.__err && window.__err('langMenu or langBackdrop missing'); return; }
  m.classList.add('open');
  b.classList.add('open');
}
function closeLangMenu() {
  const m = $('langMenu'); const b = $('langBackdrop');
  if (m) m.classList.remove('open');
  if (b) b.classList.remove('open');
}
function toggleLangMenu() {
  const m = $('langMenu');
  if (!m) return;
  if (m.classList.contains('open')) closeLangMenu();
  else openLangMenu();
}

function applyLanguage() {
  localStorage.setItem('lang', currentLang);
  $('loginTitle').textContent = t('login_title');
  $('loginSubtitle').textContent = t('login_subtitle');
  $('lblNick').textContent = t('field_nick');
  $('lblPass').textContent = t('field_password');
  $('loginName').placeholder = t('ph_nick');
  $('loginPass').placeholder = t('ph_password');
  $('rememberLbl').textContent = t('remember_me');
  if (!authPending) $('loginBtn').textContent = t('btn_login');
  $('uptimeLbl').textContent = t('uptime_label');
  $('lblLoggedAs').textContent = t('logged_as');
  $('addTabBtn').title = t('title_add');
  $('connectBtn').title = t('title_join');
  $('settingsBtn').title = t('title_settings');
  $('mobileSettingsBtn').title = t('title_settings');
  $('mobileChannelsLbl').textContent = t('mobile_channels');
  $('mcpTitle').textContent = t('mobile_channels');
  $('mcpCreateBtn').textContent = t('title_add');
  $('mcpConnectBtn').textContent = t('title_join');
  $('createModalTitle').textContent = t('modal_create_title');
  $('lblCreateName').textContent = t('modal_name');
  $('newChannelName').placeholder = t('modal_name_ph');
  $('lblCreatePass').textContent = t('modal_pass');
  $('newChannelPass').placeholder = t('modal_pass_ph');
  $('createPassHint').textContent = t('modal_pass_hint');
  $('btnCancelCreate').textContent = t('btn_cancel');
  $('createChannelBtn').textContent = t('btn_create');
  $('connectModalTitle').textContent = t('modal_connect_title');
  $('lblConnectName').textContent = t('modal_connect_name');
  $('connectChannelName').placeholder = t('modal_connect_ph');
  $('lblConnectPass').textContent = t('modal_pass');
  $('connectChannelPass').placeholder = t('modal_pass_ph');
  $('btnCancelConnect').textContent = t('btn_cancel');
  $('connectChannelBtn').textContent = t('btn_join');
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
  $('langFlag').innerHTML = FLAGS[currentLang] || '';
  $('langCode').textContent = currentLang.toUpperCase();
  const lb = $('settingsLangBtn');
  if (lb) {
    lb.innerHTML = '<span style="display:inline-flex; align-items:center; gap:8px;">' +
      (FLAGS[currentLang] || '') + ' <span>' + (LANG_NAMES[currentLang] || currentLang) + '</span>' +
      '</span>';
  }
  applyTheme();
  buildLangMenu();
  renderTabs(); renderHeader(); renderMessages(); updateMuteUI();
  renderUsersList(); renderMobileChannelList();
}

/* ============== Login error / loading ============== */
let loginErrTimer = null;
function showLoginError(msg) {
  const box = $('loginError'); if (!box) return;
  if (!msg) { box.classList.remove('show'); return; }
  $('loginErrorText').textContent = msg;
  box.classList.add('show');
  clearTimeout(loginErrTimer);
  loginErrTimer = setTimeout(() => box.classList.remove('show'), 5000);
}
function setLoginLoading(loading) {
  authPending = !!loading;
  const b = $('loginBtn');
  if (!b) return;
  if (loading) {
    b.disabled = true;
    b.innerHTML = '<span class="spinner"></span>' + t('btn_wait');
  } else {
    b.disabled = false;
    b.textContent = t('btn_login');
  }
}

/* ============== Auth ============== */
async function doAuth() {
  if (authPending) return;
  const username = $('loginName').value.trim();
  const password = $('loginPass').value;
  if (!username || !password) { showLoginError(t('err_bad_credentials')); return; }
  if (username.length < 2) { showLoginError(t('err_bad_username')); return; }
  if (password.length < 4) { showLoginError(t('err_bad_password')); return; }
  setLoginLoading(true);
  showLoginError('');
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
    setLoginLoading(false);
  }
}
$('loginBtn').addEventListener('click', doAuth);
$('loginName').addEventListener('keydown', e => { if (e.key === 'Enter') $('loginPass').focus(); });
$('loginPass').addEventListener('keydown', e => { if (e.key === 'Enter') doAuth(); });

function enterApp() {
  $('loginScreen').style.display = 'none';
  $('app').style.display = 'flex';
  $('headerUser').textContent = currentUser;
  $('settingsUser').textContent = currentUser;
  updateAppVH();
  loadChannels(); loadMyPrefs();
}
async function loadMyPrefs() {
  try {
    const res = await api('/api/me');
    showInListPref = !!res.show_in_list;
    $('showInListToggle').checked = showInListPref;
  } catch (e) {}
}
async function tryRestoreSession() {
  const saved = localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');
  if (!saved) return false;
  authToken = saved;
  try {
    const res = await api('/api/me');
    currentUser = res.username;
    enterApp(); return true;
  } catch (e) {
    localStorage.removeItem('auth_token'); sessionStorage.removeItem('auth_token');
    authToken = null; return false;
  }
}

$('themeBtn').addEventListener('click', e => { e.stopPropagation(); toggleTheme(); });
$('langBtn').addEventListener('click', e => { e.stopPropagation(); toggleLangMenu(); });
$('langBackdrop').addEventListener('click', closeLangMenu);

function openSettingsModal() {
  $('settingsBackdrop').classList.add('open');
  $('settingsUser').textContent = currentUser || '—';
  $('showInListToggle').checked = showInListPref;
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
$('settingsLangBtn').addEventListener('click', () => {
  $('settingsBackdrop').classList.remove('open');
  openLangMenu();
});
$('showInListToggle').addEventListener('change', async e => {
  const val = e.target.checked;
  try { await api('/api/me/preferences', 'POST', { show_in_list: val }); showInListPref = val; }
  catch (err) { e.target.checked = !val; }
});
$('settingsLogoutBtn').addEventListener('click', async () => {
  try { await api('/api/logout', 'POST'); } catch(e){}
  authToken = null; currentUser = null;
  channels = []; activeId = null;
  channelKeys = {}; channelPasses = {}; channelSalt = {};
  channelMembers = {}; memberSetByChannel = {};
  if (ws) { try { ws.close(); } catch(e){} ws = null; wsChannelId = null; }
  localStorage.removeItem('auth_token'); sessionStorage.removeItem('auth_token');
  $('settingsBackdrop').classList.remove('open');
  closeMobileChannels();
  $('app').style.display = 'none';
  $('loginScreen').style.display = 'flex';
  updateAppVH();
});

/* ============== Users ============== */
let usersListCache = [];
async function loadUsersList() {
  const box = $('usersList'); if (!box) return;
  if (!authToken) { box.innerHTML = '<div class="users-empty">' + t('users_list_empty') + '</div>'; return; }
  box.innerHTML = '<div class="users-empty">…</div>';
  try { const r = await api('/api/users'); usersListCache = r.users || []; renderUsersList(); }
  catch (e) { box.innerHTML = '<div class="users-empty">' + t('err_generic') + '</div>'; }
}
function renderUsersList() {
  const box = $('usersList'); if (!box) return;
  if (!usersListCache.length) { box.innerHTML = '<div class="users-empty">' + t('users_list_empty') + '</div>'; return; }
  box.innerHTML = '';
  usersListCache.forEach(u => {
    const color = colorForUser(u.username);
    const row = document.createElement('div');
    row.className = 'user-list-item';
    row.innerHTML = '<span class="user-dot' + (u.online ? ' online' : '') + '"></span>' +
      '<span class="user-name" style="color:' + color + ';">' + escapeHtml(u.username) +
      (u.self ? '<span class="user-you"> ' + t('users_you') + '</span>' : '') + '</span>';
    box.appendChild(row);
  });
}

/* ============== Mobile channels drawer ============== */
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
    box.innerHTML = '<div class="mcp-empty">' + t('empty_no_channels') + '</div>';
    return;
  }
  channels.forEach(ch => {
    const item = document.createElement('div');
    item.className = 'mcp-item' + (ch.id === activeId ? ' active' : '');
    const l = document.createElement('span');
    l.className = 'lock-ico'; l.innerHTML = SVG.lock;
    item.appendChild(l);
    const n = document.createElement('span');
    n.className = 'mcp-name'; n.textContent = ch.name;
    item.appendChild(n);
    const x = document.createElement('span');
    x.className = 'mcp-leave';
    x.innerHTML = SVG.x;
    x.title = t('leave_channel');
    item.appendChild(x);
    item.addEventListener('click', e => {
      if (e.target.closest && e.target.closest('.mcp-leave')) return;
      activeId = ch.id;
      closeMobileChannels();
      renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
      deriveKeyForChannel(activeId);
    });
    x.addEventListener('click', async (e) => {
      e.stopPropagation();
      await leaveChannelById(ch.id);
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

async function leaveChannelById(cid) {
  try { await api('/api/channels/leave', 'POST', { channel_id: cid }); } catch(e){}
  channels = channels.filter(c => c.id !== cid);
  delete channelMembers[cid]; delete memberSetByChannel[cid];
  delete channelKeys[cid]; delete channelSalt[cid]; delete channelPasses[cid];
  if (activeId === cid) {
    activeId = channels[0] ? channels[0].id : null;
    if (ws) { try { ws.close(); } catch(e){} ws = null; wsChannelId = null; }
    if (activeId) { openChannelWS(activeId); loadChannelMembers(activeId); deriveKeyForChannel(activeId); }
  }
  renderAll();
}

/* ============== Channels ============== */
async function loadChannels() {
  try {
    const res = await api('/api/channels');
    channels = res.channels || [];
    channels.forEach(ch => { channelSalt[ch.id] = ch.salt; });
    if (activeId && !channels.find(c => c.id === activeId)) activeId = null;
    if (!activeId && channels.length) activeId = channels[0].id;
    renderAll();
    if (activeId) {
      await deriveKeyForChannel(activeId);
      openChannelWS(activeId); loadChannelMembers(activeId);
    }
  } catch(e) { if (e.status === 401) $('settingsLogoutBtn').click(); }
}

async function deriveKeyForChannel(cid) {
  if (channelKeys[cid]) return true;
  const ch = channels.find(c => c.id === cid);
  if (!ch || !ch.salt) return false;
  const stored = localStorage.getItem('sld_pass_' + cid);
  if (stored !== null) {
    try {
      channelKeys[cid] = await deriveChannelKey(stored, cid, ch.name, ch.salt);
      channelPasses[cid] = stored;
      return true;
    } catch (e) {}
  }
  const pass = window.prompt(t('modal_pass') + ': ' + ch.name, '');
  if (pass === null) return false;
  try {
    channelKeys[cid] = await deriveChannelKey(pass, cid, ch.name, ch.salt);
    channelPasses[cid] = pass;
    localStorage.setItem('sld_pass_' + cid, pass);
    renderMessages();
    return true;
  } catch (e) { alert(t('err_pass_wrong')); return false; }
}

async function loadChannelMembers(cid) {
  try {
    const res = await api('/api/channels/' + encodeURIComponent(cid) + '/members');
    channelMembers[cid] = res.members || [];
    memberSetByChannel[cid] = new Set((res.members || []).map(m => m.username.toLowerCase()));
    renderMessages();
  } catch (e) {
    channelMembers[cid] = [];
    memberSetByChannel[cid] = new Set();
  }
}

function renderTabs() {
  const wrap = $('tabsScroll'); wrap.innerHTML = '';
  channels.forEach(ch => {
    const tab = document.createElement('div');
    tab.className = 'channel-tab' + (ch.id === activeId ? ' active' : '');
    tab.dataset.id = ch.id;
    const l = document.createElement('span'); l.className = 'lock-ico'; l.innerHTML = SVG.lock;
    tab.appendChild(l);
    const n = document.createElement('span'); n.textContent = ch.name; tab.appendChild(n);
    const x = document.createElement('span'); x.className = 'close-x'; x.dataset.close = ch.id; x.innerHTML = SVG.x;
    tab.appendChild(x);
    tab.addEventListener('click', e => {
      if (e.target.closest && e.target.closest('[data-close]')) return;
      if (activeId === ch.id) return;
      activeId = ch.id; renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
      deriveKeyForChannel(activeId);
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
  await leaveChannelById(closeEl.getAttribute('data-close'));
});
function scrollActiveTabIntoView() {
  const wrap = $('tabsScroll');
  const at = wrap.querySelector('.channel-tab.active');
  if (!at) return;
  const wr = wrap.getBoundingClientRect(), tr = at.getBoundingClientRect();
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

/* ============== Messages ============== */
const GROUP_WINDOW_MS = 5 * 60 * 1000;
let mutes = {};

function renderMentions(text, cid) {
  const members = memberSetByChannel[cid];
  const esc = escapeHtml(text);
  if (!members || !members.size) return esc;
  return esc.replace(/@([^\s@:<>"'&]{2,32})/gu, (full, name) => {
    if (members.has(name.toLowerCase())) {
      return '<span class="mention">@' + name + '</span>';
    }
    return full;
  });
}

function buildMsgRow(m, prevAuthor, prevTime, cid) {
  const row = document.createElement('div');
  row.className = 'msg-row';
  row.dataset.id = m.i;
  row.dataset.t = String(m.ts * 1000);
  const ts = m.ts * 1000;
  const d = new Date(ts);
  const timeStr = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const grouped = (prevAuthor === m.u) && ((ts - prevTime) < GROUP_WINDOW_MS);
  if (grouped) row.classList.add('grouped');
  const color = colorForUser(m.u);
  const authEl = document.createElement('span');
  authEl.className = 'msg-author' + (grouped ? ' hidden' : '');
  authEl.textContent = m.u;
  authEl.style.color = color;
  const contentEl = document.createElement('span');
  contentEl.className = 'msg-content';
  const textEl = document.createElement('span'); textEl.className = 'msg-text'; textEl.textContent = '…';
  const timeEl = document.createElement('span'); timeEl.className = 'msg-time'; timeEl.textContent = timeStr;
  contentEl.appendChild(textEl); contentEl.appendChild(timeEl);
  row.appendChild(authEl); row.appendChild(contentEl);
  return { el: row, textEl };
}

function renderMessages() {
  const feed = $('chatFeed'); feed.innerHTML = '';
  const ch = channels.find(c => c.id === activeId);
  if (!ch) { feed.innerHTML = '<div class="empty-state">' + t('empty_no_channels') + '</div>'; return; }
  if (!ch.messages.length) { feed.innerHTML = '<div class="empty-state">' + t('empty_no_messages') + '</div>'; return; }
  let lastAuthor = null, lastTime = 0;
  const decryptJobs = [];
  ch.messages.forEach(m => {
    const { el, textEl } = buildMsgRow(m, lastAuthor, lastTime, activeId);
    feed.appendChild(el);
    decryptJobs.push({ cid: activeId, ct: m.c, el: textEl });
    lastAuthor = m.u; lastTime = m.ts * 1000;
  });
  decryptJobs.forEach(j => {
    decryptText(j.cid, j.ct).then(pt => {
      if (pt === null) j.el.textContent = '🔒';
      else j.el.innerHTML = renderMentions(pt, j.cid);
    });
  });
  feed.scrollTop = feed.scrollHeight;
}

function appendMessageUI(m, cid) {
  const ch = channels.find(c => c.id === cid);
  if (ch) {
    if (!ch.messages.find(x => x.i === m.i)) {
      ch.messages.push(m);
      if (ch.messages.length > 500) ch.messages = ch.messages.slice(-500);
    }
  }
  if (activeId !== cid) return;
  const feed = $('chatFeed');
  const existing = feed.querySelector('.msg-row[data-id="' + CSS.escape(m.i) + '"]');
  if (existing) return;
  const empty = feed.querySelector('.empty-state'); if (empty) empty.remove();
  const lastRow = feed.querySelector('.msg-row:last-child');
  const prevAuthor = lastRow ? (lastRow.querySelector('.msg-author').textContent || '') : null;
  const prevTime = lastRow ? parseInt(lastRow.dataset.t || '0', 10) : 0;
  const { el, textEl } = buildMsgRow(m, prevAuthor, prevTime, cid);
  feed.appendChild(el);
  decryptText(cid, m.c).then(pt => {
    if (pt === null) textEl.textContent = '🔒';
    else textEl.innerHTML = renderMentions(pt, cid);
  });
  feed.scrollTop = feed.scrollHeight;
}
function addSystem(text) {
  const feed = $('chatFeed');
  const empty = feed.querySelector('.empty-state'); if (empty) empty.remove();
  const row = document.createElement('div');
  row.className = 'msg-system';
  row.textContent = text;
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

/* ============== WebSocket ============== */
function openChannelWS(cid) {
  if (ws && wsChannelId === cid && ws.readyState === WebSocket.OPEN) return;
  if (ws) { try { ws.close(); } catch(e){} ws = null; }
  wsChannelId = cid; onlineUsers = [];
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onopen = () => ws.send(JSON.stringify({ t:'a', tok: authToken, ch: cid }));
  ws.onmessage = ev => {
    let data; try { data = JSON.parse(ev.data); } catch(e){ return; }
    if (data.t === 'm' && data.m) { appendMessageUI(data.m, cid); renderHeader(); }
    else if (data.t === 'p') { onlineUsers = data.u || []; renderHeader(); }
    else if (data.t === 'mu') { mutes[cid] = Date.now() + data.s*1000; updateMuteUI(); addSystem('Muted for ' + data.s + 's'); }
    else if (data.t === 'e' && data.e === 'auth') { $('settingsLogoutBtn').click(); }
  };
  ws.onclose = () => { if (wsChannelId === cid) ws = null; };
}

async function sendMessage() {
  const input = $('msgInput');
  const text = input.value.trim();
  if (!text) return;
  const cid = activeId;
  if (!cid) return;
  if (!channelKeys[cid]) { const ok = await deriveKeyForChannel(cid); if (!ok) return; }
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if ((mutes[cid]||0) > Date.now()) { updateMuteUI(); return; }
  const mid = uuid();
  const tNow = Date.now()/1000;
  const enc = await encryptText(cid, text);
  appendMessageUI({ i: mid, u: currentUser, c: enc, ts: tNow }, cid);
  ws.send(JSON.stringify({ t:'m', i: mid, c: enc }));
  input.value = ''; autoResize(); hideMentionPop();
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
function autoResize() { const el = $('msgInput'); el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 140) + 'px'; }
$('msgInput').addEventListener('input', () => { autoResize(); updateMentionState(); });

/* ============== Mentions ============== */
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
  mentionState = { open: true, items, selected: 0, startIdx: cur.start };
  renderMentionPop();
}
function renderMentionPop() {
  const pop = $('mentionPop'); if (!pop) return;
  pop.innerHTML = '';
  if (!mentionState.open) { pop.classList.remove('open'); return; }
  mentionState.items.forEach((m, idx) => {
    const item = document.createElement('div');
    item.className = 'mention-pop-item' + (idx === mentionState.selected ? ' active' : '');
    item.textContent = '@' + m.username;
    item.style.color = colorForUser(m.username);
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

/* ============== Modals ============== */
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
  if ($('langMenu').classList.contains('open')) { closeLangMenu(); return; }
  ['createBackdrop','connectBackdrop','settingsBackdrop'].forEach(id => $(id).classList.remove('open'));
  if ($('mobileChannelsPanel').classList.contains('open')) closeMobileChannels();
  hideMentionPop();
});

$('addTabBtn').addEventListener('click', () => {
  $('newChannelName').value = '';
  $('newChannelPass').value = '';
  $('createBackdrop').classList.add('open');
  setTimeout(() => $('newChannelName').focus(), 80);
});
$('createChannelBtn').addEventListener('click', async () => {
  const name = $('newChannelName').value.trim();
  const pass = $('newChannelPass').value;
  if (!name) { $('newChannelName').focus(); return; }
  if (!pass || pass.length < 1) { alert(t('err_bad_passphrase')); $('newChannelPass').focus(); return; }
  try {
    const res = await api('/api/channels', 'POST', { name });
    channelSalt[res.id] = res.salt;
    channelKeys[res.id] = await deriveChannelKey(pass, res.id, res.name, res.salt);
    channelPasses[res.id] = pass;
    localStorage.setItem('sld_pass_' + res.id, pass);
    channels.push({ id: res.id, name: res.name, salt: res.salt, private: true, messages: [] });
    activeId = res.id;
    $('createBackdrop').classList.remove('open');
    renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
  } catch(e) {
    const msg = e.status === 409 ? 'Name already taken' : t('err_generic');
    alert(msg);
  }
});

$('connectBtn').addEventListener('click', () => {
  $('connectChannelName').value = '';
  $('connectChannelPass').value = '';
  $('connectError').classList.remove('show');
  $('connectBackdrop').classList.add('open');
  setTimeout(() => $('connectChannelName').focus(), 80);
});
$('connectChannelBtn').addEventListener('click', async () => {
  const name = $('connectChannelName').value.trim();
  const pass = $('connectChannelPass').value;
  if (!name) { $('connectChannelName').focus(); return; }
  if (!pass || pass.length < 1) { alert(t('err_bad_passphrase')); $('connectChannelPass').focus(); return; }
  try {
    const look = await api('/api/channels/lookup', 'POST', { name });
    const key = await deriveChannelKey(pass, look.id, look.name, look.salt);
    const res = await api('/api/channels/join', 'POST', { channel_id: look.id });
    channelSalt[res.id] = res.salt || look.salt;
    channelKeys[res.id] = key;
    channelPasses[res.id] = pass;
    localStorage.setItem('sld_pass_' + res.id, pass);
    if (!channels.find(c => c.id === res.id)) {
      channels.push({ id: res.id, name: res.name, salt: look.salt, private: true, messages: res.messages || [] });
    } else {
      const c = channels.find(c => c.id === res.id); c.messages = res.messages || [];
    }
    activeId = res.id;
    $('connectBackdrop').classList.remove('open');
    renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
  } catch (e) {
    $('connectErrorText').textContent = e.status === 404
      ? t('connect_not_found', {name})
      : t('err_generic');
    $('connectError').classList.add('show');
  }
});
$('connectChannelName').addEventListener('input', () => $('connectError').classList.remove('show'));
$('connectChannelPass').addEventListener('input', () => $('connectError').classList.remove('show'));

/* ============== Keyboard nav ============== */
async function switchChannel(delta) {
  if (!channels.length) return;
  const idx = channels.findIndex(c => c.id === activeId);
  if (idx === -1) return;
  const n = Math.max(0, Math.min(channels.length - 1, idx + delta));
  if (n === idx) return;
  activeId = channels[n].id;
  renderAll(); openChannelWS(activeId); loadChannelMembers(activeId);
  await deriveKeyForChannel(activeId);
}
document.addEventListener('keydown', e => {
  if (document.querySelector('.my-modal-backdrop.open')) return;
  if ($('mobileChannelsPanel').classList.contains('open')) return;
  if ($('langMenu').classList.contains('open')) return;
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

/* ============== Init ============== */
applyLanguage();
renderUptime();
(async () => {
  const ok = await tryRestoreSession();
  if (!ok) setTimeout(() => $('loginName').focus(), 100);
})();
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def index():
    html = HTML_TEMPLATE
    html = html.replace("%%I18N%%", json.dumps(I18N, ensure_ascii=False))
    html = html.replace("%%FLAGS%%", json.dumps(FLAGS, ensure_ascii=False))
    html = html.replace("%%LANG_ORDER%%", json.dumps(LANG_ORDER))
    html = html.replace("%%LANG_NAMES%%", json.dumps(LANG_NAMES, ensure_ascii=False))
    return HTMLResponse(html)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    log(f"Starting {APP_NAME} on http://0.0.0.0:{port}")
    log(f"Languages: {len(LANG_ORDER)}")
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1,
                log_level="info", access_log=False,
                limit_concurrency=200, timeout_keep_alive=30)
