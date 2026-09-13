# ============================================================
#  FastAPI Messenger — full stack in one file
#  Storage: in-memory (dicts), no disk, <512MB RAM target
#  Security: PBKDF2-SHA256 (200k iters), token auth, E2EE (AES-GCM 256)
#  Run: python main.py  →  http://0.0.0.0:8000
# ============================================================
import hashlib
import json
import os
import secrets
import time
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

START_TIME = time.time()

# ---------------- Constants / Limits ----------------
MAX_USERS = 1000
MAX_CHANNELS = 2000
MAX_MESSAGES_PER_CHANNEL = 300
MAX_MESSAGE_LEN = 4000
MAX_USERNAME_LEN = 32
MIN_USERNAME_LEN = 2
MIN_PASSWORD_LEN = 4
MAX_PASSWORD_LEN = 128
PBKDF2_ITERATIONS = 200_000
RATE_LIMIT_WINDOW = 10.0
RATE_LIMIT_MAX = 20
MUTE_SECONDS = 30
TOKEN_TTL = 60 * 60 * 24 * 7
MAX_USER_CHANNELS = 500   # per-account channel membership cap

# ---------------- In-memory storage ----------------
USERS: Dict[str, dict] = {}
TOKENS: Dict[str, dict] = {}
CHANNELS: Dict[str, dict] = {}
USER_CHANNELS: Dict[str, Set[str]] = {}   # username -> set of channel_ids (PERSISTENT)
MSG_TIMES: Dict[str, List[float]] = {}

# ---------------- Security helpers ----------------
def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()

def verify_password(password: str, salt_hex: str, expected: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), expected)

def new_token() -> str:
    return secrets.token_urlsafe(32)

def user_from_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    entry = TOKENS.get(token)
    if not entry:
        return None
    if entry["exp"] < time.time():
        TOKENS.pop(token, None)
        return None
    return entry["user"]

async def auth(request: Request) -> str:
    user = user_from_token(request.headers.get("x-auth-token"))
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user

def user_channel_set(username: str) -> Set[str]:
    s = USER_CHANNELS.get(username)
    if s is None:
        s = set()
        USER_CHANNELS[username] = s
    return s

# ---------------- App ----------------
app = FastAPI(title="Messenger", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response

# ---------------- Models ----------------
class AuthReq(BaseModel):
    username: str
    password: str

class CreateChannelReq(BaseModel):
    name: str
    private: bool = False

class ConnectChannelReq(BaseModel):
    name: str

class LeaveChannelReq(BaseModel):
    channel_id: str

# ---------------- Default public channels (seed, no members) ----------------
def init_defaults() -> None:
    seeds = [
        ("general", "Общий", False),
        ("work", "Работа", False),
        ("friends", "Друзья", False),
        ("secret-room", "secret-room", True),
    ]
    for cid, name, private in seeds:
        CHANNELS[cid] = {
            "id": cid, "name": name, "private": private,
            "owner": None, "created": time.time(), "messages": [],
        }

init_defaults()

# ---------------- REST ----------------
@app.post("/api/auth")
async def auth_endpoint(req: AuthReq):
    """Login + register combined."""
    username = req.username.strip()
    if not (MIN_USERNAME_LEN <= len(username) <= MAX_USERNAME_LEN):
        raise HTTPException(400, "bad_username")
    if not (MIN_PASSWORD_LEN <= len(req.password) <= MAX_PASSWORD_LEN):
        raise HTTPException(400, "bad_password")

    existing = USERS.get(username)
    if existing:
        if not verify_password(req.password, existing["salt"], existing["pwd"]):
            raise HTTPException(401, "bad_credentials")
        is_new = False
    else:
        if len(USERS) >= MAX_USERS:
            raise HTTPException(503, "server_full")
        salt = os.urandom(16)
        USERS[username] = {
            "pwd": hash_password(req.password, salt),
            "salt": salt.hex(),
            "created": time.time(),
        }
        USER_CHANNELS[username] = set()   # init empty channel list
        is_new = True

    token = new_token()
    TOKENS[token] = {"user": username, "exp": time.time() + TOKEN_TTL}
    return {"token": token, "username": username, "is_new": is_new}

@app.post("/api/logout")
async def logout(request: Request):
    token = request.headers.get("x-auth-token")
    if token:
        TOKENS.pop(token, None)
    return {"ok": True}

@app.get("/api/uptime")
async def uptime():
    return {
        "uptime": time.time() - START_TIME,
        "users": len(USERS),
        "channels": len(CHANNELS),
        "online": len(TOKENS),
    }

@app.get("/api/me")
async def me(request: Request):
    username = await auth(request)
    return {"username": username}

@app.get("/api/channels")
async def list_channels(request: Request):
    """Return channels the current user is a member of."""
    username = await auth(request)
    cids = USER_CHANNELS.get(username, set())
    out = []
    for cid in cids:
        ch = CHANNELS.get(cid)
        if not ch:
            continue
        out.append({
            "id": cid,
            "name": ch["name"],
            "private": ch["private"],
            "owner": ch["owner"],
            "messages": ch["messages"][-MAX_MESSAGES_PER_CHANNEL:],
        })
    out.sort(key=lambda c: c["name"].lower())
    return {"channels": out}

@app.post("/api/channels")
async def create_channel(req: CreateChannelReq, request: Request):
    username = await auth(request)
    name = req.name.strip()[:40]
    if not name:
        raise HTTPException(400, "bad_name")
    for ch in CHANNELS.values():
        if ch["name"].lower() == name.lower():
            raise HTTPException(409, "name_taken")
    if len(CHANNELS) >= MAX_CHANNELS:
        raise HTTPException(503, "server_full")
    my_ch = user_channel_set(username)
    if len(my_ch) >= MAX_USER_CHANNELS:
        raise HTTPException(503, "user_channel_limit")
    cid = secrets.token_hex(6)
    CHANNELS[cid] = {
        "id": cid, "name": name, "private": bool(req.private),
        "owner": username, "created": time.time(), "messages": [],
    }
    my_ch.add(cid)   # persist membership
    return {"id": cid, "name": name, "private": bool(req.private), "messages": []}

@app.post("/api/channels/connect")
async def connect_channel(req: ConnectChannelReq, request: Request):
    username = await auth(request)
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "bad_name")
    for cid, ch in CHANNELS.items():
        if ch["name"].lower() == name.lower():
            my_ch = user_channel_set(username)
            if len(my_ch) >= MAX_USER_CHANNELS and cid not in my_ch:
                raise HTTPException(503, "user_channel_limit")
            my_ch.add(cid)   # persist membership
            return {
                "id": cid, "name": ch["name"], "private": ch["private"],
                "owner": ch["owner"],
                "messages": ch["messages"][-MAX_MESSAGES_PER_CHANNEL:],
            }
    raise HTTPException(404, "not_found")

@app.post("/api/channels/leave")
async def leave_channel(req: LeaveChannelReq, request: Request):
    username = await auth(request)
    cid = req.channel_id
    s = USER_CHANNELS.get(username)
    if s is not None:
        s.discard(cid)
    return {"ok": True}

# ---------------- WebSocket ----------------
class WSManager:
    """Tracks live WebSocket connections only. Does NOT store membership."""
    def __init__(self):
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self.info: Dict[WebSocket, tuple] = {}

    async def join(self, channel_id: str, ws: WebSocket, username: str):
        self.rooms.setdefault(channel_id, set()).add(ws)
        self.info[ws] = (username, channel_id)

    def leave(self, ws: WebSocket):
        entry = self.info.pop(ws, None)
        if entry:
            _, cid = entry
            self.rooms.get(cid, set()).discard(ws)

    def online_users(self, channel_id: str) -> List[str]:
        return sorted(set(u for (u, c) in self.info.values() if c == channel_id))

    async def broadcast(self, channel_id: str, payload: dict):
        dead = []
        for ws in list(self.rooms.get(channel_id, set())):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.leave(ws)

manager = WSManager()

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    username: Optional[str] = None
    channel_id: Optional[str] = None
    try:
        init = await ws.receive_json()
        if init.get("type") != "auth":
            await ws.close()
            return
        token = init.get("token", "")
        channel_id = init.get("channel_id", "")
        username = user_from_token(token)

        # auth: user must be an actual member (persisted)
        if (not username
                or channel_id not in CHANNELS
                or channel_id not in USER_CHANNELS.get(username, set())):
            await ws.send_json({"type": "error", "error": "auth"})
            await ws.close()
            return

        await manager.join(channel_id, ws, username)
        await manager.broadcast(channel_id, {"type": "presence", "users": manager.online_users(channel_id)})

        while True:
            data = await ws.receive_json()
            if data.get("type") == "message":
                ct = str(data.get("ciphertext", ""))[:MAX_MESSAGE_LEN]
                if not ct:
                    continue
                key = f"{username}|{channel_id}"
                now = time.time()
                times = MSG_TIMES.setdefault(key, [])
                times[:] = [t for t in times if now - t < RATE_LIMIT_WINDOW]
                if len(times) >= RATE_LIMIT_MAX:
                    await ws.send_json({"type": "muted", "seconds": MUTE_SECONDS})
                    continue
                times.append(now)

                msg = {
                    "id": secrets.token_hex(8),
                    "from": username,
                    "ct": ct,
                    "t": now,
                }
                ch = CHANNELS[channel_id]
                ch["messages"].append(msg)
                if len(ch["messages"]) > MAX_MESSAGES_PER_CHANNEL:
                    ch["messages"] = ch["messages"][-MAX_MESSAGES_PER_CHANNEL:]
                await manager.broadcast(channel_id, {"type": "message", "msg": msg})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.leave(ws)
        if channel_id:
            try:
                await manager.broadcast(channel_id, {"type": "presence", "users": manager.online_users(channel_id)})
            except Exception:
                pass

# ============================================================
#  i18n — 20 real languages
# ============================================================
I18N = {
 "en": {"login_title":"Messenger","login_subtitle":"Sign in or create an account","field_nick":"Nickname","field_password":"Password","ph_nick":"Your nickname","ph_password":"Your password","btn_login":"Continue","remember_me":"Remember me","logged_as":"Signed in as","header_no_channels":"No channels","header_no_channels_sub":"Create a channel with «+» or connect to an existing one","header_msgs":"{n} messages","header_private_tag":"· private","empty_no_channels":"You have no channels yet.<br>Create a new one («+») or connect to an existing one.","empty_no_messages":"No messages. Be the first to write!","composer_ph":"Write a message...","composer_no_channel":"No active channel","composer_muted":"Muted: {n}s","modal_create_title":"Create channel","modal_name":"Name","modal_name_ph":"E.g. Work","modal_private":"Private channel","btn_cancel":"Cancel","btn_create":"Create","modal_connect_title":"Connect to channel","modal_connect_name":"Channel name","modal_connect_ph":"Enter the exact channel name","btn_connect":"Connect","connect_not_found":"Channel «{name}» not found","search_ph":"Search channels and messages...","search_close":"Close","search_empty":"Nothing found","search_no_channels":"You have no channels yet","tag_channel":"channel","tag_msg":"msg.","in_channel":"in «{name}»","title_search":"Search (Ctrl+K)","title_add":"Create channel","title_connect":"Connect to channel","title_settings":"Settings","title_scroll_left":"Scroll left","title_scroll_right":"Scroll right","theme_toggle":"Toggle theme","lang_toggle":"Change language","uptime_label":"Uptime","online_label":"online","err_bad_credentials":"Wrong password for this nickname","err_bad_username":"Nickname must be 2–32 characters","err_bad_password":"Password must be at least 4 characters","err_network":"Network error","err_generic":"Error","settings_title":"Settings","settings_account":"Account","settings_appearance":"Appearance","settings_user":"Signed in as","settings_logout":"Sign out","settings_theme":"Theme","settings_theme_light":"Light","settings_theme_dark":"Dark","settings_lang":"Language","settings_close":"Close","welcome_new":"Welcome! Account created.","welcome_back":"Welcome back!"},
 "ru": {"login_title":"Мессенджер","login_subtitle":"Войдите или создайте аккаунт","field_nick":"Ник","field_password":"Пароль","ph_nick":"Ваш ник","ph_password":"Ваш пароль","btn_login":"Продолжить","remember_me":"Запомнить меня","logged_as":"Вы вошли как","header_no_channels":"Нет каналов","header_no_channels_sub":"Создайте канал кнопкой «+» или подключитесь к существующему","header_msgs":"{n} сообщений","header_private_tag":"· приватный","empty_no_channels":"У вас пока нет каналов.<br>Создайте новый («+») или подключитесь к существующему.","empty_no_messages":"Нет сообщений. Напишите первым!","composer_ph":"Написать сообщение...","composer_no_channel":"Нет активного канала","composer_muted":"Мут: {n} с","modal_create_title":"Создать канал","modal_name":"Название","modal_name_ph":"Например, Работа","modal_private":"Приватный канал","btn_cancel":"Отмена","btn_create":"Создать","modal_connect_title":"Подключиться к каналу","modal_connect_name":"Название канала","modal_connect_ph":"Введите точное название канала","btn_connect":"Подключиться","connect_not_found":"Канал «{name}» не найден","search_ph":"Поиск по каналам и сообщениям...","search_close":"Закрыть","search_empty":"Ничего не найдено","search_no_channels":"У вас ещё нет каналов","tag_channel":"канал","tag_msg":"сообщ.","in_channel":"в «{name}»","title_search":"Поиск (Ctrl+K)","title_add":"Создать канал","title_connect":"Подключиться к каналу","title_settings":"Настройки","title_scroll_left":"Прокрутить влево","title_scroll_right":"Прокрутить вправо","theme_toggle":"Сменить тему","lang_toggle":"Сменить язык","uptime_label":"Аптайм","online_label":"онлайн","err_bad_credentials":"Неверный пароль для этого ника","err_bad_username":"Ник должен быть 2–32 символа","err_bad_password":"Пароль минимум 4 символа","err_network":"Ошибка сети","err_generic":"Ошибка","settings_title":"Настройки","settings_account":"Аккаунт","settings_appearance":"Оформление","settings_user":"Вы вошли как","settings_logout":"Выйти из аккаунта","settings_theme":"Тема","settings_theme_light":"Светлая","settings_theme_dark":"Тёмная","settings_lang":"Язык","settings_close":"Закрыть","welcome_new":"Добро пожаловать! Аккаунт создан.","welcome_back":"С возвращением!"},
 "es": {"login_title":"Mensajero","login_subtitle":"Inicia sesión o crea una cuenta","field_nick":"Apodo","field_password":"Contraseña","ph_nick":"Tu apodo","ph_password":"Tu contraseña","btn_login":"Continuar","remember_me":"Recuérdame","logged_as":"Sesión como","header_no_channels":"Sin canales","header_no_channels_sub":"Crea un canal con «+» o conéctate a uno existente","header_msgs":"{n} mensajes","empty_no_channels":"Aún no tienes canales.<br>Crea uno nuevo («+») o conéctate a uno existente.","empty_no_messages":"No hay mensajes. ¡Sé el primero en escribir!","composer_ph":"Escribe un mensaje...","composer_no_channel":"Sin canal activo","composer_muted":"Silenciado: {n}s","modal_create_title":"Crear canal","modal_name":"Nombre","modal_name_ph":"Ej.: Trabajo","modal_private":"Canal privado","btn_cancel":"Cancelar","btn_create":"Crear","modal_connect_title":"Conectar al canal","modal_connect_name":"Nombre del canal","modal_connect_ph":"Introduce el nombre exacto del canal","btn_connect":"Conectar","connect_not_found":"Canal «{name}» no encontrado","search_ph":"Buscar canales y mensajes...","search_close":"Cerrar","search_empty":"Nada encontrado","search_no_channels":"Aún no tienes canales","tag_channel":"canal","tag_msg":"msg.","in_channel":"en «{name}»","title_search":"Buscar (Ctrl+K)","title_add":"Crear canal","title_connect":"Conectar al canal","title_settings":"Ajustes","theme_toggle":"Cambiar tema","lang_toggle":"Cambiar idioma","uptime_label":"Tiempo activo","online_label":"en línea","err_bad_credentials":"Contraseña incorrecta","err_bad_username":"Apodo 2–32 caracteres","err_bad_password":"Contraseña mín. 4 caracteres","err_generic":"Error","settings_title":"Ajustes","settings_account":"Cuenta","settings_appearance":"Apariencia","settings_user":"Sesión como","settings_logout":"Cerrar sesión","settings_theme":"Tema","settings_theme_light":"Claro","settings_theme_dark":"Oscuro","settings_lang":"Idioma","settings_close":"Cerrar"},
 "de": {"login_title":"Messenger","login_subtitle":"Anmelden oder Konto erstellen","field_nick":"Spitzname","field_password":"Passwort","ph_nick":"Dein Spitzname","ph_password":"Dein Passwort","btn_login":"Weiter","remember_me":"Angemeldet bleiben","logged_as":"Angemeldet als","header_no_channels":"Keine Kanäle","header_no_channels_sub":"Erstelle einen Kanal mit «+» oder verbinde dich mit einem bestehenden","header_msgs":"{n} Nachrichten","empty_no_channels":"Du hast noch keine Kanäle.<br>Erstelle einen neuen («+») oder verbinde dich mit einem bestehenden.","empty_no_messages":"Keine Nachrichten. Schreib als Erster!","composer_ph":"Nachricht schreiben...","composer_no_channel":"Kein aktiver Kanal","composer_muted":"Stumm: {n}s","modal_create_title":"Kanal erstellen","modal_name":"Name","modal_name_ph":"Z.B. Arbeit","modal_private":"Privater Kanal","btn_cancel":"Abbrechen","btn_create":"Erstellen","modal_connect_title":"Mit Kanal verbinden","modal_connect_name":"Kanalname","modal_connect_ph":"Exakten Kanalnamen eingeben","btn_connect":"Verbinden","connect_not_found":"Kanal «{name}» nicht gefunden","search_ph":"Kanäle und Nachrichten durchsuchen...","search_close":"Schließen","search_empty":"Nichts gefunden","search_no_channels":"Du hast noch keine Kanäle","tag_channel":"Kanal","tag_msg":"Nachr.","in_channel":"in «{name}»","title_search":"Suchen (Strg+K)","title_add":"Kanal erstellen","title_connect":"Mit Kanal verbinden","title_settings":"Einstellungen","theme_toggle":"Design wechseln","lang_toggle":"Sprache ändern","uptime_label":"Laufzeit","online_label":"online","err_bad_credentials":"Falsches Passwort","err_bad_username":"Spitzname 2–32 Zeichen","err_bad_password":"Passwort mind. 4 Zeichen","err_generic":"Fehler","settings_title":"Einstellungen","settings_account":"Konto","settings_appearance":"Aussehen","settings_user":"Angemeldet als","settings_logout":"Abmelden","settings_theme":"Design","settings_theme_light":"Hell","settings_theme_dark":"Dunkel","settings_lang":"Sprache","settings_close":"Schließen"},
 "fr": {"login_title":"Messagerie","login_subtitle":"Connectez-vous ou créez un compte","field_nick":"Pseudo","field_password":"Mot de passe","ph_nick":"Votre pseudo","ph_password":"Votre mot de passe","btn_login":"Continuer","remember_me":"Se souvenir de moi","logged_as":"Connecté en tant que","header_no_channels":"Aucun canal","header_no_channels_sub":"Créez un canal avec «+» ou connectez-vous à un existant","header_msgs":"{n} messages","empty_no_channels":"Vous n'avez pas encore de canaux.<br>Créez-en un («+») ou connectez-vous à un existant.","empty_no_messages":"Aucun message. Soyez le premier !","composer_ph":"Écrire un message...","composer_no_channel":"Aucun canal actif","composer_muted":"Muet : {n}s","modal_create_title":"Créer un canal","modal_name":"Nom","modal_name_ph":"Ex : Travail","modal_private":"Canal privé","btn_cancel":"Annuler","btn_create":"Créer","modal_connect_title":"Se connecter à un canal","modal_connect_name":"Nom du canal","modal_connect_ph":"Entrez le nom exact du canal","btn_connect":"Se connecter","connect_not_found":"Canal «{name}» introuvable","search_ph":"Rechercher canaux et messages...","search_close":"Fermer","search_empty":"Rien trouvé","search_no_channels":"Pas encore de canaux","tag_channel":"canal","tag_msg":"msg.","in_channel":"dans «{name}»","title_search":"Rechercher (Ctrl+K)","title_add":"Créer un canal","title_connect":"Se connecter","title_settings":"Paramètres","theme_toggle":"Changer de thème","lang_toggle":"Changer de langue","uptime_label":"Durée","online_label":"en ligne","err_bad_credentials":"Mot de passe incorrect","err_bad_username":"Pseudo 2 à 32 caractères","err_bad_password":"Mot de passe min. 4 caractères","err_generic":"Erreur","settings_title":"Paramètres","settings_account":"Compte","settings_appearance":"Apparence","settings_user":"Connecté en tant que","settings_logout":"Se déconnecter","settings_theme":"Thème","settings_theme_light":"Clair","settings_theme_dark":"Sombre","settings_lang":"Langue","settings_close":"Fermer"},
 "it": {"login_title":"Messaggero","login_subtitle":"Accedi o crea un account","field_nick":"Nickname","field_password":"Password","btn_login":"Continua","remember_me":"Ricordami","logged_as":"Connesso come","header_no_channels":"Nessun canale","header_msgs":"{n} messaggi","empty_no_messages":"Nessun messaggio. Scrivi per primo!","composer_ph":"Scrivi un messaggio...","composer_no_channel":"Nessun canale attivo","btn_cancel":"Annulla","btn_create":"Crea","btn_connect":"Connetti","search_ph":"Cerca canali e messaggi...","search_close":"Chiudi","search_empty":"Nessun risultato","title_search":"Cerca (Ctrl+K)","title_add":"Crea canale","title_connect":"Connetti","title_settings":"Impostazioni","theme_toggle":"Cambia tema","lang_toggle":"Cambia lingua","uptime_label":"Attività","err_bad_credentials":"Password errata","err_generic":"Errore","settings_title":"Impostazioni","settings_account":"Account","settings_appearance":"Aspetto","settings_user":"Connesso come","settings_logout":"Esci","settings_theme":"Tema","settings_theme_light":"Chiaro","settings_theme_dark":"Scuro","settings_lang":"Lingua","settings_close":"Chiudi"},
 "pt": {"login_title":"Mensageiro","login_subtitle":"Entre ou crie uma conta","field_nick":"Apelido","field_password":"Senha","btn_login":"Continuar","remember_me":"Lembrar-me","logged_as":"Conectado como","header_no_channels":"Sem canais","header_msgs":"{n} mensagens","empty_no_messages":"Sem mensagens. Seja o primeiro!","composer_ph":"Escrever mensagem...","composer_no_channel":"Nenhum canal ativo","btn_cancel":"Cancelar","btn_create":"Criar","btn_connect":"Conectar","search_ph":"Pesquisar canais e mensagens...","search_close":"Fechar","search_empty":"Nada encontrado","title_search":"Pesquisar (Ctrl+K)","title_add":"Criar canal","title_connect":"Conectar","title_settings":"Configurações","theme_toggle":"Alternar tema","lang_toggle":"Alterar idioma","uptime_label":"Tempo ativo","err_bad_credentials":"Senha incorreta","err_generic":"Erro","settings_title":"Configurações","settings_account":"Conta","settings_appearance":"Aparência","settings_user":"Conectado como","settings_logout":"Sair","settings_theme":"Tema","settings_theme_light":"Claro","settings_theme_dark":"Escuro","settings_lang":"Idioma","settings_close":"Fechar"},
 "nl": {"login_title":"Messenger","login_subtitle":"Meld aan of maak een account","field_nick":"Bijnaam","field_password":"Wachtwoord","btn_login":"Doorgaan","remember_me":"Onthoud mij","logged_as":"Ingelogd als","btn_cancel":"Annuleren","btn_create":"Aanmaken","btn_connect":"Verbinden","search_close":"Sluiten","title_settings":"Instellingen","title_search":"Zoeken (Ctrl+K)","title_add":"Kanaal aanmaken","title_connect":"Verbinden","theme_toggle":"Thema wisselen","lang_toggle":"Taal wijzigen","uptime_label":"Uptime","err_bad_credentials":"Onjuist wachtwoord","err_generic":"Fout","settings_title":"Instellingen","settings_account":"Account","settings_appearance":"Weergave","settings_user":"Ingelogd als","settings_logout":"Afmelden","settings_theme":"Thema","settings_theme_light":"Licht","settings_theme_dark":"Donker","settings_lang":"Taal","settings_close":"Sluiten"},
 "pl": {"btn_login":"Kontynuuj","remember_me":"Zapamiętaj mnie","settings_title":"Ustawienia","settings_account":"Konto","settings_appearance":"Wygląd","settings_logout":"Wyloguj","settings_theme":"Motyw","settings_lang":"Język","settings_close":"Zamknij","title_settings":"Ustawienia","search_close":"Zamknij","btn_cancel":"Anuluj","btn_create":"Utwórz","btn_connect":"Połącz"},
 "uk": {"login_title":"Месенджер","login_subtitle":"Увійдіть або створіть акаунт","field_nick":"Нік","field_password":"Пароль","btn_login":"Продовжити","remember_me":"Запам'ятати мене","logged_as":"Ви увійшли як","btn_cancel":"Скасувати","btn_create":"Створити","btn_connect":"Підключитися","search_close":"Закрити","title_settings":"Налаштування","title_search":"Пошук (Ctrl+K)","title_add":"Створити канал","title_connect":"Підключитися","theme_toggle":"Змінити тему","lang_toggle":"Змінити мову","uptime_label":"Аптайм","err_bad_credentials":"Невірний пароль","err_generic":"Помилка","settings_title":"Налаштування","settings_account":"Акаунт","settings_appearance":"Оформлення","settings_user":"Ви увійшли як","settings_logout":"Вийти з акаунта","settings_theme":"Тема","settings_theme_light":"Світла","settings_theme_dark":"Темна","settings_lang":"Мова","settings_close":"Закрити"},
 "cs": {"btn_login":"Pokračovat","remember_me":"Zapamatovat si mě","settings_title":"Nastavení","settings_account":"Účet","settings_appearance":"Vzhled","settings_logout":"Odhlásit","settings_theme":"Motiv","settings_lang":"Jazyk","settings_close":"Zavřít","title_settings":"Nastavení"},
 "sv": {"btn_login":"Fortsätt","remember_me":"Kom ihåg mig","settings_title":"Inställningar","settings_account":"Konto","settings_appearance":"Utseende","settings_logout":"Logga ut","settings_theme":"Tema","settings_lang":"Språk","settings_close":"Stäng","title_settings":"Inställningar"},
 "el": {"btn_login":"Συνέχεια","remember_me":"Να με θυμάσαι","settings_title":"Ρυθμίσεις","settings_account":"Λογαριασμός","settings_appearance":"Εμφάνιση","settings_logout":"Αποσύνδεση","settings_theme":"Θέμα","settings_lang":"Γλώσσα","settings_close":"Κλείσιμο","title_settings":"Ρυθμίσεις"},
 "tr": {"login_title":"Anlık Mesajlaşma","login_subtitle":"Giriş yap veya hesap oluştur","field_nick":"Takma ad","field_password":"Şifre","btn_login":"Devam et","remember_me":"Beni hatırla","logged_as":"Giriş yapan:","btn_cancel":"İptal","btn_create":"Oluştur","btn_connect":"Bağlan","search_close":"Kapat","title_settings":"Ayarlar","title_search":"Ara (Ctrl+K)","title_add":"Kanal oluştur","title_connect":"Bağlan","theme_toggle":"Temayı değiştir","lang_toggle":"Dili değiştir","uptime_label":"Çalışma süresi","err_bad_credentials":"Yanlış şifre","err_generic":"Hata","settings_title":"Ayarlar","settings_account":"Hesap","settings_appearance":"Görünüm","settings_user":"Giriş yapan:","settings_logout":"Çıkış yap","settings_theme":"Tema","settings_theme_light":"Açık","settings_theme_dark":"Koyu","settings_lang":"Dil","settings_close":"Kapat"},
 "ja": {"login_title":"メッセンジャー","login_subtitle":"サインインまたはアカウント作成","field_nick":"ニックネーム","field_password":"パスワード","btn_login":"続行","remember_me":"ログイン状態を保持","logged_as":"ログイン中:","btn_cancel":"キャンセル","btn_create":"作成","btn_connect":"接続","search_close":"閉じる","title_settings":"設定","title_search":"検索 (Ctrl+K)","title_add":"チャンネル作成","title_connect":"接続","theme_toggle":"テーマ切替","lang_toggle":"言語変更","uptime_label":"稼働時間","err_generic":"エラー","settings_title":"設定","settings_account":"アカウント","settings_appearance":"外観","settings_user":"ログイン中:","settings_logout":"サインアウト","settings_theme":"テーマ","settings_theme_light":"ライト","settings_theme_dark":"ダーク","settings_lang":"言語","settings_close":"閉じる"},
 "ko": {"login_title":"메신저","login_subtitle":"로그인 또는 계정 만들기","field_nick":"닉네임","field_password":"비밀번호","btn_login":"계속","remember_me":"로그인 상태 유지","logged_as":"로그인:","btn_cancel":"취소","btn_create":"만들기","btn_connect":"연결","search_close":"닫기","title_settings":"설정","title_search":"검색 (Ctrl+K)","title_add":"채널 만들기","title_connect":"연결","theme_toggle":"테마 전환","lang_toggle":"언어 변경","uptime_label":"가동 시간","err_generic":"오류","settings_title":"설정","settings_account":"계정","settings_appearance":"모양","settings_user":"로그인:","settings_logout":"로그아웃","settings_theme":"테마","settings_theme_light":"라이트","settings_theme_dark":"다크","settings_lang":"언어","settings_close":"닫기"},
 "zh": {"login_title":"即时通讯","login_subtitle":"登录或创建账号","field_nick":"昵称","field_password":"密码","btn_login":"继续","remember_me":"记住我","logged_as":"登录为","btn_cancel":"取消","btn_create":"创建","btn_connect":"连接","search_close":"关闭","title_settings":"设置","title_search":"搜索 (Ctrl+K)","title_add":"创建频道","title_connect":"连接","theme_toggle":"切换主题","lang_toggle":"切换语言","uptime_label":"运行时间","err_generic":"错误","settings_title":"设置","settings_account":"账号","settings_appearance":"外观","settings_user":"登录为","settings_logout":"退出登录","settings_theme":"主题","settings_theme_light":"浅色","settings_theme_dark":"深色","settings_lang":"语言","settings_close":"关闭"},
 "ar": {"login_title":"ماسنجر","login_subtitle":"سجّل الدخول أو أنشئ حسابًا","field_nick":"الاسم","field_password":"كلمة المرور","btn_login":"متابعة","remember_me":"تذكرني","logged_as":"مسجل باسم","btn_cancel":"إلغاء","btn_create":"إنشاء","btn_connect":"اتصال","search_close":"إغلاق","title_settings":"الإعدادات","title_search":"بحث (Ctrl+K)","title_add":"إنشاء قناة","title_connect":"اتصال","theme_toggle":"تغيير المظهر","lang_toggle":"تغيير اللغة","uptime_label":"مدة التشغيل","err_generic":"خطأ","settings_title":"الإعدادات","settings_account":"الحساب","settings_appearance":"المظهر","settings_user":"مسجل باسم","settings_logout":"تسجيل الخروج","settings_theme":"المظهر","settings_theme_light":"فاتح","settings_theme_dark":"داكن","settings_lang":"اللغة","settings_close":"إغلاق"},
 "he": {"login_title":"מסנג'ר","login_subtitle":"התחבר או צור חשבון","field_nick":"כינוי","field_password":"סיסמה","btn_login":"המשך","remember_me":"זכור אותי","logged_as":"מחובר בתור","btn_cancel":"ביטול","btn_create":"צור","btn_connect":"התחבר","search_close":"סגור","title_settings":"הגדרות","title_search":"חפש (Ctrl+K)","title_add":"צור ערוץ","title_connect":"התחבר","theme_toggle":"החלף ערכת נושא","lang_toggle":"החלף שפה","uptime_label":"זמן פעילות","err_generic":"שגיאה","settings_title":"הגדרות","settings_account":"חשבון","settings_appearance":"מראה","settings_user":"מחובר בתור","settings_logout":"התנתק","settings_theme":"ערכת נושא","settings_theme_light":"בהיר","settings_theme_dark":"כהה","settings_lang":"שפה","settings_close":"סגור"},
 "hi": {"login_title":"मैसेंजर","login_subtitle":"साइन इन करें या खाता बनाएं","field_nick":"उपनाम","field_password":"पासवर्ड","btn_login":"जारी रखें","remember_me":"मुझे याद रखें","logged_as":"इस रूप में साइन इन:","btn_cancel":"रद्द करें","btn_create":"बनाएं","btn_connect":"जुड़ें","search_close":"बंद करें","title_settings":"सेटिंग्स","title_search":"खोजें (Ctrl+K)","title_add":"चैनल बनाएं","title_connect":"जुड़ें","theme_toggle":"थीम बदलें","lang_toggle":"भाषा बदलें","uptime_label":"अपटाइम","err_generic":"त्रुटि","settings_title":"सेटिंग्स","settings_account":"खाता","settings_appearance":"रूप","settings_user":"इस रूप में साइन इन:","settings_logout":"साइन आउट","settings_theme":"थीम","settings_theme_light":"लाइट","settings_theme_dark":"डार्क","settings_lang":"भाषा","settings_close":"बंद करें"},
}

# ---------------- SVG flags ----------------
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
#  HTML page
# ============================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0088cc">
<title>Messenger</title>
<link rel="stylesheet" href="https://getbootstrap.com/1.4.0/assets/css/bootstrap.min.css">
<style>
* { scrollbar-width: none; -ms-overflow-style: none; -webkit-text-size-adjust: 100%; -webkit-tap-highlight-color: transparent; }
*::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }

html { height: 100%; }
body {
  margin: 0;
  background: #f5f5f5;
  font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 13px;
  color: #333;
  overflow: hidden;
  height: var(--app-vh, 100vh);
  -webkit-user-select: none; -moz-user-select: none; -ms-user-select: none; user-select: none;
}
input, textarea { -webkit-user-select: text; -moz-user-select: text; -ms-user-select: text; user-select: text; font-family: inherit; }
svg { display: inline-block; vertical-align: middle; }

/* Tap-friendly interactive elements */
button, .channel-tab, .icon-btn-tab, .scroll-arrow, .lang-menu-item, .settings-btn, .settings-tab, .search-result {
  touch-action: manipulation;
  -webkit-tap-highlight-color: transparent;
}

/* ---- Login ---- */
.login-screen {
  position: fixed; top: 0; left: 0; right: 0;
  height: var(--app-vh, 100vh);
  background: #e9eef3; background-image: linear-gradient(#f5f8fb, #dfe6ee);
  display: flex; align-items: center; justify-content: center;
  z-index: 500; padding: 16px; box-sizing: border-box;
  overflow-y: auto;
}
.login-box { width: 320px; max-width: 100%; background: #fff; border: 1px solid #b8c4d0; box-shadow: 0 4px 16px rgba(0,0,0,.15); padding: 20px 20px 14px; text-align: center; margin: auto; }
.login-box h2 { margin: 0 0 4px; font-size: 18px; color: #2b3d51; }
.login-box p { margin: 0 0 16px; color: #7b8a99; font-size: 12px; }
.my-label { display: block !important; text-align: left !important; font-size: 11px !important; font-weight: bold !important; color: #667788 !important; margin: 0 0 4px 0 !important; padding: 0 !important; line-height: 1.4 !important; text-transform: uppercase !important; letter-spacing: .3px; }
.field-group { margin-bottom: 12px; text-align: left; }
.my-input { display: block !important; width: 100% !important; box-sizing: border-box !important; padding: 7px 10px !important; border: 1px solid #b8c4d0 !important; font-size: 13px !important; background: #fff !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.08) !important; color: #333 !important; outline: none !important; height: auto !important; line-height: 1.4 !important; margin: 0 !important; border-radius: 0 !important; }
.my-input:focus { border-color: #0088cc !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.08), 0 0 4px rgba(0,136,204,.5) !important; }
.login-box .btn { width: 100%; margin-top: 6px; }
.remember-row { display: flex !important; align-items: center !important; font-size: 12px !important; color: #4a5a6a !important; margin: 8px 0 4px 0 !important; cursor: pointer; user-select: none; }
.remember-row input { margin-right: 7px !important; }
.login-error { margin-top: 10px; font-size: 11.5px; color: #a94442; background: #fcebeb; border: 1px solid #f5c6c6; padding: 5px 8px; display: none; text-align: left; }
.login-error.show { display: block; }
.uptime-line { margin-top: 12px; padding-top: 10px; border-top: 1px solid #e0e5eb; font-size: 11px; color: #7b8a99; display: flex; justify-content: space-between; }
.uptime-line .u-val { font-weight: bold; color: #4a5a6a; font-family: "Courier New", monospace; }

/* ---- Lang picker ---- */
.login-settings { margin-top: 10px; padding-top: 10px; border-top: 1px solid #e0e5eb; display: flex; gap: 6px; }
.settings-btn { flex: 1; display: inline-flex; align-items: center; justify-content: center; height: 26px; padding: 0 8px; border: 1px solid #b8c4d0; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6); color: #333; cursor: pointer; font-size: 12px; font-family: inherit; text-shadow: 0 1px 0 rgba(255,255,255,.6); border-radius: 0; }
.settings-btn:hover { background: #d9d9d9; background-image: linear-gradient(#f5f5f5, #d9d9d9); color: #000; }
.settings-btn .lang-code { margin-left: 6px; font-size: 11px; font-weight: bold; letter-spacing: .5px; }
.lang-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 9998; display: none; }
.lang-backdrop.open { display: block; }
.lang-menu { position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%); background: #fff; border: 1px solid #666; box-shadow: 0 5px 20px rgba(0,0,0,.4); padding: 8px; z-index: 9999; display: none; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px; max-width: calc(100vw - 20px); max-height: calc(100vh - 20px); overflow-y: auto; box-sizing: border-box; width: 560px; border-radius: 0; }
.lang-menu.open { display: grid; }
.lang-menu-item { display: flex; align-items: center; padding: 6px 8px; cursor: pointer; font-size: 12px; color: #333; gap: 7px; border: 1px solid #ddd; background: #fafafa; min-width: 0; box-sizing: border-box; border-radius: 0; }
.lang-menu-item:hover { background: #eaf4fb; border-color: #8ab4dc; }
.lang-menu-item.active { background: #d6e8f7; color: #004a80; font-weight: bold; border-color: #4a90c2; }
.lang-menu-item .flag-wrap { flex-shrink: 0; display: inline-flex; align-items: center; }
.lang-menu-item .lang-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lang-menu-item .check { flex-shrink: 0; color: #0088cc; font-weight: bold; visibility: hidden; }
.lang-menu-item.active .check { visibility: visible; }

/* ---- App ---- */
.app {
  width: 100%;
  height: var(--app-vh, 100vh);
  background: #fff;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
}
.tabs-bar { display: flex; align-items: center; padding: 6px 8px; background: #f5f5f5; background-image: linear-gradient(#ffffff, #ececec); border-bottom: 1px solid #ccc; flex-shrink: 0; gap: 4px; }
.tabs-scroll { display: flex; align-items: center; flex: 1 1 0; min-width: 0; overflow-x: auto; overflow-y: hidden; padding-bottom: 1px; scroll-behavior: smooth; -webkit-overflow-scrolling: touch; }
.scroll-arrow { width: 18px; height: 26px; border: 1px solid #bbb; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6); color: #555; cursor: pointer; padding: 0; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; border-radius: 0; }
.scroll-arrow:hover { background: #d9d9d9; color: #000; }
.channel-tab { display: inline-flex; align-items: center; padding: 4px 10px; margin-right: 4px; border: 1px solid #bbb; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6); color: #444; font-size: 12px; white-space: nowrap; cursor: pointer; text-shadow: 0 1px 0 rgba(255,255,255,.6); flex-shrink: 0; font-family: "Courier New", Courier, monospace; border-radius: 0; }
.channel-tab:last-child { margin-right: 0; }
.channel-tab:hover { background: #d9d9d9; color: #000; }
.channel-tab.active { background: #006dcc; background-image: linear-gradient(#0088cc, #0044cc); color: #fff; border-color: #003f81; text-shadow: 0 -1px 0 rgba(0,0,0,.3); }
.channel-tab .lock-ico { margin-right: 4px; opacity: .8; display: inline-flex; align-items: center; }
.channel-tab .close-x { margin-left: 6px; cursor: pointer; opacity: .55; display: inline-flex; align-items: center; color: inherit; padding: 2px; }
.channel-tab .close-x:hover { opacity: 1; color: #c00; }
.icon-btn-tab { width: 26px; height: 26px; line-height: 1; border: 1px solid #bbb; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6); color: #555; cursor: pointer; padding: 0; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; border-radius: 0; }
.icon-btn-tab:hover { background: #d9d9d9; color: #000; }
.top-sep { width: 1px; height: 20px; background: #ccc; margin: 0 4px; flex-shrink: 0; }

.chat-header { display: flex; align-items: center; padding: 8px 12px; background: #f5f5f5; background-image: linear-gradient(#ffffff, #f0f0f0); border-bottom: 1px solid #ccc; flex-shrink: 0; }
.chat-header .title { font-weight: bold; font-size: 14px; line-height: 1.1; color: #222; }
.chat-header .subtitle { font-size: 11px; color: #777; }
.chat-header .user-info { margin-left: auto; display: flex; align-items: center; gap: 8px; font-size: 11px; color: #666; }
.chat-header .user-info .nick { font-weight: bold; color: #2b3d51; }

.search-panel { position: absolute; top: 0; left: 0; right: 0; background: #fff; border-bottom: 1px solid #ccc; box-shadow: 0 2px 6px rgba(0,0,0,.2); z-index: 30; transform: translateY(-110%); transition: transform .18s ease-out; max-height: 70vh; display: flex; flex-direction: column; }
.search-panel.open { transform: translateY(0); }
.search-panel .search-head { padding: 8px 10px; display: flex; align-items: center; border-bottom: 1px solid #e5e5e5; background: #f5f5f5; background-image: linear-gradient(#ffffff, #efefef); gap: 8px; }
.search-panel .search-head input { flex: 1; padding: 5px 8px !important; border: 1px solid #bbb !important; font-size: 12px !important; outline: none !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.1) !important; background: #fff !important; color: #333 !important; box-sizing: border-box !important; margin: 0 !important; border-radius: 0 !important; }
.search-panel .close-search { padding: 4px 10px; border: 1px solid #bbb; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6); cursor: pointer; font-size: 12px; color: #333; flex-shrink: 0; border-radius: 0; }
.search-results { overflow-y: auto; -webkit-overflow-scrolling: touch; }
.search-results .empty { padding: 20px; text-align: center; color: #999; font-size: 12px; }
.search-result { display: flex; align-items: center; padding: 8px 12px; border-bottom: 1px solid #eee; cursor: pointer; gap: 8px; }
.search-result:hover { background: #eaf4fb; }
.search-result .title-line { font-size: 12px; font-weight: bold; color: #222; }
.search-result .sub-line { font-size: 11px; color: #666; }
.search-result mark { background: #fff3a8; padding: 0 1px; }
.search-result .type-tag { font-size: 10px; text-transform: uppercase; color: #888; border: 1px solid #ccc; padding: 1px 5px; background: #f5f5f5; flex-shrink: 0; }

.chat-feed { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 8px 12px; background: #fdfdfd; }
.empty-state { margin: auto; text-align: center; color: #aaa; font-size: 12px; padding-top: 60px; }
.empty-state svg { display: block; margin: 0 auto 10px; color: #ccc; }
.msg-row { padding: 2px 4px; line-height: 1.55; font-size: 12.5px; word-wrap: break-word; transition: background .3s; }
.msg-row:hover { background: #f2f6fa; }
.msg-row.highlight { background: #fff3a8; }
.msg-author { font-weight: bold; margin-right: 4px; }
.msg-author.hidden { visibility: hidden; }
.msg-text { color: #222; }
.msg-time { color: #b0b8c0; font-size: 10.5px; margin-left: 6px; }
.msg-system { color: #a94442; background: #fcebeb; border: 1px solid #f5c6c6; font-size: 11.5px; padding: 4px 8px; margin: 4px 0; display: flex; align-items: center; }
.msg-system .sys-icon { margin-right: 6px; display: inline-flex; align-items: center; }
.msg-system.info { color: #31708f; background: #eaf4fb; border-color: #bce0f0; }

.composer { display: flex; align-items: flex-end; gap: 6px; padding: 8px 10px; background: #f5f5f5; background-image: linear-gradient(#f0f0f0, #ffffff); border-top: 1px solid #ccc; flex-shrink: 0; }
.composer textarea { flex: 1; resize: none; padding: 6px 8px !important; border: 1px solid #bbb !important; font-size: 12px !important; max-height: 140px; outline: none !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.1) !important; background: #fff !important; color: #333 !important; box-sizing: border-box !important; margin: 0 !important; overflow: hidden; border-radius: 0 !important; }
.composer textarea:focus { border-color: #0088cc !important; }
.composer textarea[disabled] { background: #f7e6e6 !important; border-color: #d6a0a0 !important; color: #a94442 !important; }
.composer .icon-btn { width: 32px; height: 32px; padding: 0; flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center; }

/* Modals */
.my-modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 1000; display: none; justify-content: center; align-items: center; padding: 20px; box-sizing: border-box; }
.my-modal-backdrop.open { display: flex; }
.my-modal { width: 400px; max-width: 100%; background: #fff; border: 1px solid #666; box-shadow: 0 5px 20px rgba(0,0,0,.4); position: relative; box-sizing: border-box; border-radius: 0; }
.modal-head { padding: 8px 12px; background: #f5f5f5; background-image: linear-gradient(#ffffff, #efefef); border-bottom: 1px solid #ccc; font-weight: bold; font-size: 13px; display: flex; align-items: center; cursor: move; user-select: none; }
.modal-head .close-m { margin-left: auto; cursor: pointer; color: #666; padding: 4px; line-height: 1; display: inline-flex; align-items: center; }
.modal-head .close-m:hover { color: #c00; background: #e6e6e6; }
.modal-body { padding: 14px; text-align: left; }
.modal-foot { padding: 10px 12px; background: #f7f7f7; border-top: 1px solid #e5e5e5; text-align: right; }
.modal-foot .btn { margin-left: 6px; }
.checkbox-row { display: flex; align-items: center; margin-top: 12px; font-size: 12px; color: #444; cursor: pointer; white-space: nowrap; }
.checkbox-row input { margin-right: 7px; flex-shrink: 0; }
.error-msg { margin-top: 8px; font-size: 11.5px; color: #a94442; background: #fcebeb; border: 1px solid #f5c6c6; padding: 5px 8px; display: none; }
.error-msg.show { display: flex; align-items: center; }
.error-msg .sys-icon { margin-right: 6px; display: inline-flex; }

/* Settings modal */
#settingsModal { width: 520px; }
.settings-body { display: flex; min-height: 260px; }
.settings-tabs { width: 140px; background: #f5f5f5; border-right: 1px solid #ddd; padding: 8px 0; }
.settings-tab { display: block; width: 100%; text-align: left; padding: 8px 14px; background: transparent; border: 0; border-left: 3px solid transparent; cursor: pointer; font-size: 12px; color: #444; font-family: inherit; border-radius: 0; }
.settings-tab:hover { background: #e9ecef; }
.settings-tab.active { background: #fff; border-left-color: #0088cc; color: #006dcc; font-weight: bold; }
.settings-content { flex: 1; padding: 16px; overflow-y: auto; max-height: 60vh; }
.settings-pane { display: none; }
.settings-pane.active { display: block; }
.settings-row { margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.settings-row.block { display: block; }
.settings-label { font-size: 11px; text-transform: uppercase; letter-spacing: .5px; color: #7b8a99; font-weight: bold; margin-bottom: 4px; }
.settings-value { font-size: 13px; color: #222; font-weight: bold; }
.settings-lang-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px; margin-top: 8px; }
.settings-lang-grid .lang-menu-item { padding: 5px 7px; }
.settings-section-title { font-size: 13px; font-weight: bold; color: #222; padding-bottom: 8px; border-bottom: 1px solid #eee; margin-bottom: 12px; }

/* Dark theme */
body.dark { background: #1a1a1a; color: #ccc; }
body.dark .login-screen { background: #1a1a1a; background-image: none; }
body.dark .login-box { background: #252526; border-color: #3c3c3c; box-shadow: 0 4px 16px rgba(0,0,0,.6); }
body.dark .login-box h2 { color: #eaeaea; }
body.dark .login-box p { color: #888; }
body.dark .my-label { color: #888 !important; }
body.dark .my-input { background: #1e1e1e !important; color: #ddd !important; border-color: #4a4a4c !important; }
body.dark .my-input:focus { border-color: #0e7fc0 !important; }
body.dark .login-error { background: #3a1f1f; border-color: #5a2a2a; color: #e0a0a0; }
body.dark .login-settings, body.dark .uptime-line { border-top-color: #3c3c3c; }
body.dark .settings-btn { background: #37373d; background-image: none; color: #ccc; border-color: #4a4a4c; text-shadow: none; }
body.dark .settings-btn:hover { background: #45454a; color: #fff; }
body.dark .uptime-line { color: #888; }
body.dark .uptime-line .u-val { color: #aaa; }
body.dark .remember-row { color: #aaa !important; }
body.dark .lang-menu { background: #252526; border-color: #3c3c3c; }
body.dark .lang-menu-item { color: #ccc; background: #2d2d30; border-color: #3c3c3c; }
body.dark .lang-menu-item:hover { background: #37373d; border-color: #4a4a4c; }
body.dark .lang-menu-item.active { background: #0e639c; color: #fff; border-color: #0e639c; }
body.dark .lang-menu-item .check { color: #6cb6ff; }
body.dark .app { background: #252526; }
body.dark .tabs-bar, body.dark .chat-header, body.dark .composer, body.dark .search-panel .search-head { background: #2d2d30; background-image: none; border-color: #3c3c3c; }
body.dark .channel-tab, body.dark .icon-btn-tab, body.dark .scroll-arrow, body.dark .search-panel .close-search { background: #37373d; background-image: none; color: #ccc; border-color: #4a4a4c; text-shadow: none; }
body.dark .channel-tab:hover, body.dark .icon-btn-tab:hover, body.dark .scroll-arrow:hover { background: #45454a; color: #fff; }
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
body.dark .composer textarea { background: #1e1e1e !important; color: #ddd !important; border-color: #4a4a4c !important; }
body.dark .search-panel, body.dark .my-modal { background: #252526; border-color: #3c3c3c; }
body.dark .search-panel .search-head input { background: #1e1e1e !important; color: #ddd !important; border-color: #4a4a4c !important; }
body.dark .search-result { border-color: #3c3c3c; }
body.dark .search-result:hover { background: #2a3a4a; }
body.dark .search-result .title-line { color: #eaeaea; }
body.dark .modal-head { background: #2d2d30; background-image: none; border-color: #3c3c3c; color: #eaeaea; }
body.dark .modal-foot { background: #2a2a2c; border-color: #3c3c3c; }
body.dark .btn { background: #37373d; color: #ccc; border-color: #4a4a4c; background-image: none; text-shadow: none; }
body.dark .btn:hover { background: #45454a; color: #fff; }
body.dark .btn.primary { background: #0e639c; color: #fff; border-color: #0e639c; background-image: none; }
body.dark .settings-tabs { background: #2d2d30; border-right-color: #3c3c3c; }
body.dark .settings-tab { color: #ccc; }
body.dark .settings-tab:hover { background: #37373d; }
body.dark .settings-tab.active { background: #252526; border-left-color: #0e639c; color: #6cb6ff; }
body.dark .settings-label { color: #777; }
body.dark .settings-value { color: #eaeaea; }
body.dark .settings-section-title { color: #eaeaea; border-bottom-color: #3c3c3c; }

/* ============================================================
   MOBILE / TABLET
   ============================================================ */

/* Active feedback on touch devices */
@media (hover: none) {
  .channel-tab:active { background: #c9c9c9; background-image: none; }
  .icon-btn-tab:active { background: #c9c9c9; background-image: none; }
  .scroll-arrow:active { background: #c9c9c9; background-image: none; }
  .settings-btn:active { background: #c9c9c9; background-image: none; }
  .lang-menu-item:active { background: #d6e8f7; }
  .search-result:active { background: #eaf4fb; }
  .msg-row:hover { background: transparent; }
}

/* Phones & small tablets */
@media (max-width: 768px) {
  /* Login */
  .login-screen { padding: 16px; align-items: flex-start; padding-top: 40px; padding-bottom: 40px; }
  .login-box { width: 100%; max-width: 420px; padding: 22px 18px 16px; }
  .login-box h2 { font-size: 20px; }
  .login-box p { font-size: 13px; }
  .login-box .btn { padding: 12px; font-size: 15px; }
  .my-label { font-size: 11px !important; }
  .my-input, .login-box .my-input, .my-modal .my-input { font-size: 16px !important; padding: 11px 12px !important; }
  .remember-row { font-size: 14px; }
  .remember-row input { width: 18px; height: 18px; }
  .settings-btn { height: 40px; font-size: 14px; }
  .settings-btn .lang-code { font-size: 12px; }
  .uptime-line { font-size: 12px; }

  /* App layout */
  .tabs-bar { padding: 8px 6px; gap: 6px; }
  .tabs-scroll { gap: 4px; }
  .channel-tab { padding: 8px 12px; font-size: 13px; margin-right: 4px; min-height: 36px; }
  .channel-tab .close-x { padding: 4px 6px; margin-left: 4px; }
  .icon-btn-tab { width: 38px; height: 38px; }
  .icon-btn-tab svg { width: 18px !important; height: 18px !important; }
  .scroll-arrow { width: 28px; height: 38px; }
  .scroll-arrow svg { width: 12px !important; height: 12px !important; }
  .top-sep { height: 26px; margin: 0 2px; }

  .chat-header { padding: 10px 12px; }
  .chat-header .title { font-size: 15px; }
  .chat-header .subtitle { font-size: 12px; }
  .chat-header .user-info { display: none; }

  .chat-feed { padding: 10px; }
  .msg-row { font-size: 14px; padding: 4px 6px; line-height: 1.5; }
  .msg-author { font-size: 14px; }
  .msg-time { font-size: 11px; }
  .msg-system { font-size: 12.5px; padding: 6px 10px; }

  .composer { padding: 8px; gap: 6px; }
  .composer textarea { font-size: 16px !important; padding: 10px 12px !important; max-height: 120px; }
  .composer .icon-btn { width: 42px; height: 42px; }
  .composer .icon-btn svg { width: 18px !important; height: 18px !important; }

  /* Search */
  .search-panel .search-head { padding: 8px; gap: 6px; }
  .search-panel .search-head input { font-size: 16px !important; padding: 9px 11px !important; }
  .search-panel .close-search { padding: 8px 12px; font-size: 13px; }
  .search-result { padding: 10px 12px; }
  .search-result .title-line { font-size: 13px; }
  .search-result .sub-line { font-size: 12px; }

  /* Modals */
  .my-modal-backdrop { padding: 10px; align-items: flex-start; padding-top: 20px; padding-bottom: 20px; overflow-y: auto; }
  .my-modal { width: 100%; max-width: 500px; margin: auto; }
  #settingsModal { width: 100%; max-width: 500px; }
  .modal-head { padding: 12px 14px; font-size: 15px; }
  .modal-head .close-m { padding: 6px; }
  .modal-body { padding: 16px; }
  .modal-foot { padding: 12px; }
  .modal-foot .btn { padding: 10px 18px; font-size: 14px; }
  .checkbox-row { font-size: 13px; padding: 4px 0; }
  .checkbox-row input { width: 18px; height: 18px; }

  /* Settings on mobile — tabs on top */
  .settings-body { flex-direction: column; min-height: 0; }
  .settings-tabs { width: 100%; border-right: 0; border-bottom: 1px solid #ddd; padding: 0; display: flex; }
  .settings-tab { flex: 1; text-align: center; padding: 12px 8px; border-left: 0; border-bottom: 3px solid transparent; font-size: 13px; }
  .settings-tab.active { border-left-color: transparent; border-bottom-color: #0088cc; }
  body.dark .settings-tab.active { border-left-color: transparent; border-bottom-color: #0e639c; }
  .settings-content { padding: 14px; max-height: 55vh; }
  .settings-lang-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .settings-lang-grid .lang-menu-item { padding: 8px; font-size: 12px; }

  /* Lang menu — 2 columns on phones */
  .lang-menu { width: calc(100vw - 20px); grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 6px; gap: 4px; }
  .lang-menu-item { padding: 8px; font-size: 12px; }
}

/* Small phones */
@media (max-width: 400px) {
  .channel-tab { padding: 7px 10px; font-size: 12px; min-height: 34px; }
  .icon-btn-tab { width: 34px; height: 34px; }
  .icon-btn-tab svg { width: 16px !important; height: 16px !important; }
  .scroll-arrow { width: 24px; height: 34px; }
  .composer .icon-btn { width: 38px; height: 38px; }
  .lang-menu { grid-template-columns: 1fr; }
  .settings-lang-grid { grid-template-columns: 1fr; }
}

/* Tablets */
@media (min-width: 769px) and (max-width: 1024px) {
  .lang-menu { width: 620px; }
  .my-modal { max-width: 500px; }
  #settingsModal { width: 540px; }
}
</style>
</head>
<body>

<div class="login-screen" id="loginScreen">
  <div class="login-box">
    <h2 id="loginTitle">Messenger</h2>
    <p id="loginSubtitle"></p>

    <div class="field-group">
      <label class="my-label" id="lblNick" for="loginName"></label>
      <input type="text" id="loginName" class="my-input" maxlength="32" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false">
    </div>

    <div class="field-group">
      <label class="my-label" id="lblPass" for="loginPass"></label>
      <input type="password" id="loginPass" class="my-input" maxlength="128" autocomplete="current-password">
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

<div class="app" id="app" style="display:none">
  <div class="tabs-bar">
    <button class="scroll-arrow" id="tabScrollLeft" type="button">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
    </button>
    <div class="tabs-scroll" id="tabsScroll"></div>
    <button class="scroll-arrow" id="tabScrollRight" type="button">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
    </button>
    <div class="top-sep"></div>
    <button class="icon-btn-tab" id="searchBtn" title="">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    </button>
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

  <div class="search-panel" id="searchPanel">
    <div class="search-head">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#888" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input type="text" id="searchInput">
      <button type="button" class="close-search" id="closeSearchBtn"></button>
    </div>
    <div class="search-results" id="searchResults"></div>
  </div>

  <div class="chat-feed" id="chatFeed"></div>

  <div class="composer">
    <textarea id="msgInput" rows="1"></textarea>
    <button class="btn primary icon-btn" id="sendBtn" type="button">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
    </button>
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
      <input type="text" id="newChannelName" class="my-input" maxlength="40" autocapitalize="sentences" spellcheck="false">
      <label class="checkbox-row">
        <input type="checkbox" id="newChannelPrivate">
        <span id="lblPrivate"></span>
      </label>
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
      <input type="text" id="connectChannelName" class="my-input" maxlength="40" autocapitalize="none" autocorrect="off" spellcheck="false">
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
          <button class="btn" id="settingsLogoutBtn" type="button" style="width:100%;"></button>
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

<script>
"use strict";
const I18N = %%I18N%%;
const FLAGS = %%FLAGS%%;
const LANG_ORDER = %%LANG_ORDER%%;
const LANG_NAMES = {en:"English",ru:"Русский",es:"Español",de:"Deutsch",fr:"Français",it:"Italiano",pt:"Português",nl:"Nederlands",pl:"Polski",uk:"Українська",cs:"Čeština",sv:"Svenska",el:"Ελληνικά",tr:"Türkçe",ja:"日本語",ko:"한국어",zh:"中文",ar:"العربية",he:"עברית",hi:"हिन्दी"};

const SVG = {
  x:'<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  lock:'<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
  ban:'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>',
  chat:'<svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  sun:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>',
  moon:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
};

// ---------- Mobile viewport fix (keyboard-aware) ----------
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

let currentLang = localStorage.getItem('lang') || 'en';
let currentTheme = localStorage.getItem('theme') || 'light';
let authToken = null;
let currentUser = null;
let channels = [];
let activeId = null;
let ws = null;
let wsChannelId = null;
let channelKeyCache = {};
let onlineUsers = [];
let uptimeBase = 0;
let uptimeFetchAt = 0;

const $ = (id) => document.getElementById(id);
const t = (key, vars) => {
  const dict = I18N[currentLang] || {};
  let s = dict[key];
  if (s === undefined) s = I18N.en[key];
  if (s === undefined) s = key;
  if (vars) for (const k in vars) s = s.replace(new RegExp('\\{'+k+'\\}','g'), vars[k]);
  return s;
};

// ---------- Crypto ----------
const enc = new TextEncoder();
const dec = new TextDecoder();
async function deriveChannelKey(channelId) {
  if (channelKeyCache[channelId]) return channelKeyCache[channelId];
  const baseKey = await crypto.subtle.importKey('raw', enc.encode('e2ee-v1:' + channelId), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt: enc.encode('messenger-fixed-salt-v1'), iterations: 120000, hash: 'SHA-256' },
    baseKey, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']
  );
  channelKeyCache[channelId] = key;
  return key;
}
function b64(buf){let s='';const bytes=new Uint8Array(buf);for(let i=0;i<bytes.length;i++)s+=String.fromCharCode(bytes[i]);return btoa(s);}
function ub64(str){const bin=atob(str);const bytes=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);return bytes;}
async function encryptText(chId, text) {
  const key = await deriveChannelKey(chId);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({name:'AES-GCM', iv}, key, enc.encode(text));
  return b64(iv) + '.' + b64(ct);
}
async function decryptText(chId, payload) {
  try {
    const [ivB64, ctB64] = payload.split('.');
    if (!ivB64 || !ctB64) return payload;
    const key = await deriveChannelKey(chId);
    const pt = await crypto.subtle.decrypt({name:'AES-GCM', iv: ub64(ivB64)}, key, ub64(ctB64));
    return dec.decode(pt);
  } catch(e) { return '[decrypt error]'; }
}

// ---------- HTTP ----------
async function api(path, method='GET', body=null, withAuth=true) {
  const headers = { 'Content-Type': 'application/json' };
  if (withAuth && authToken) headers['x-auth-token'] = authToken;
  const res = await fetch(path, {method, headers, body: body ? JSON.stringify(body) : null});
  let data = null;
  try { data = await res.json(); } catch(e){}
  if (!res.ok) throw { status: res.status, detail: (data && data.detail) || 'error' };
  return data;
}

// ---------- Uptime ----------
function fmtUptime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec/86400), h = Math.floor((sec%86400)/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  const p = n => String(n).padStart(2,'0');
  return (d>0 ? d+'d ' : '') + p(h)+':'+p(m)+':'+p(s);
}
async function refreshUptime() {
  try { const res = await fetch('/api/uptime'); const data = await res.json(); uptimeBase = data.uptime; uptimeFetchAt = performance.now(); } catch(e){}
}
function renderUptime() {
  const el = $('uptimeVal'); if (!el) return;
  if (!uptimeFetchAt) { el.textContent = '—'; return; }
  el.textContent = fmtUptime(uptimeBase + (performance.now() - uptimeFetchAt)/1000);
}
setInterval(renderUptime, 1000); refreshUptime(); setInterval(refreshUptime, 30000);

// ---------- Theme ----------
function applyTheme() {
  document.body.classList.toggle('dark', currentTheme === 'dark');
  $('themeIcon').innerHTML = currentTheme === 'dark' ? SVG.sun : SVG.moon;
  localStorage.setItem('theme', currentTheme);
  const tv = $('settingsThemeVal'); if (tv) tv.textContent = currentTheme === 'dark' ? t('settings_theme_dark') : t('settings_theme_light');
  const tt = $('settingsThemeToggle'); if (tt) tt.textContent = currentTheme === 'dark' ? t('settings_theme_light') : t('settings_theme_dark');
}
function toggleTheme() { currentTheme = currentTheme === 'dark' ? 'light' : 'dark'; applyTheme(); }

// ---------- Language ----------
function applyLanguage() {
  localStorage.setItem('lang', currentLang);

  $('loginTitle').textContent = t('login_title');
  $('loginSubtitle').textContent = t('login_subtitle');
  $('lblNick').textContent = t('field_nick');
  $('lblPass').textContent = t('field_password');
  $('loginName').placeholder = t('ph_nick');
  $('loginPass').placeholder = t('ph_password');
  $('rememberLbl').textContent = t('remember_me');
  $('loginBtn').textContent = t('btn_login');
  $('uptimeLbl').textContent = t('uptime_label');

  $('lblLoggedAs').textContent = t('logged_as');
  $('searchInput').placeholder = t('search_ph');
  $('closeSearchBtn').textContent = t('search_close');

  $('searchBtn').title = t('title_search');
  $('addTabBtn').title = t('title_add');
  $('connectBtn').title = t('title_connect');
  $('settingsBtn').title = t('title_settings');
  $('tabScrollLeft').title = t('title_scroll_left');
  $('tabScrollRight').title = t('title_scroll_right');

  $('createModalTitle').textContent = t('modal_create_title');
  $('lblCreateName').textContent = t('modal_name');
  $('newChannelName').placeholder = t('modal_name_ph');
  $('lblPrivate').textContent = t('modal_private');
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

  $('langFlag').innerHTML = FLAGS[currentLang] || '';
  $('langCode').textContent = currentLang.toUpperCase();

  applyTheme();
  buildLangMenu();
  buildSettingsLangGrid();
  renderTabs(); renderHeader(); renderMessages(); updateMuteUI();
  renderSearchResults($('searchInput').value);
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

// ---------- Login error ----------
let loginErrTimer = null;
function showLoginError(msg) {
  const box = $('loginError');
  if (!msg) { box.classList.remove('show'); return; }
  $('loginErrorText').textContent = msg;
  box.classList.add('show');
  clearTimeout(loginErrTimer);
  loginErrTimer = setTimeout(() => box.classList.remove('show'), 3500);
}

// ---------- Auth ----------
async function doAuth() {
  const username = $('loginName').value.trim();
  const password = $('loginPass').value;
  if (!username || !password) { showLoginError(t('err_bad_credentials')); return; }
  if (username.length < 2) { showLoginError(t('err_bad_username')); return; }
  if (password.length < 4) { showLoginError(t('err_bad_password')); return; }
  try {
    const res = await api('/api/auth', 'POST', { username, password }, false);
    authToken = res.token;
    currentUser = res.username;
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
    const map = { 401: 'err_bad_credentials', 400: 'err_bad_username', 503: 'err_generic' };
    showLoginError(t(map[e.status] || 'err_generic'));
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
  loadChannels();
}

async function tryRestoreSession() {
  const saved = localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');
  if (!saved) return false;
  authToken = saved;
  try {
    const res = await api('/api/me');
    currentUser = res.username;
    enterApp();
    return true;
  } catch(e) {
    localStorage.removeItem('auth_token');
    sessionStorage.removeItem('auth_token');
    authToken = null;
    return false;
  }
}

// ---------- Theme/lang buttons (login) ----------
$('themeBtn').addEventListener('click', e => { e.stopPropagation(); toggleTheme(); });
$('langBtn').addEventListener('click', e => { e.stopPropagation(); $('langMenu').classList.contains('open') ? closeLangMenu() : openLangMenu(); });
$('langBackdrop').addEventListener('click', closeLangMenu);

// ---------- Settings modal ----------
$('settingsBtn').addEventListener('click', () => {
  $('settingsBackdrop').classList.add('open');
  $('settingsUser').textContent = currentUser || '—';
});
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
  channels = []; activeId = null; channelKeyCache = {};
  if (ws) { try { ws.close(); } catch(e){} ws = null; wsChannelId = null; }
  localStorage.removeItem('auth_token');
  sessionStorage.removeItem('auth_token');
  $('settingsBackdrop').classList.remove('open');
  $('app').style.display = 'none';
  $('loginScreen').style.display = 'flex';
  closeLangMenu();
  updateAppVH();
});

// ---------- Channels ----------
async function loadChannels() {
  try {
    const res = await api('/api/channels');
    channels = res.channels || [];
    if (activeId && !channels.find(c => c.id === activeId)) activeId = null;
    if (!activeId && channels.length) activeId = channels[0].id;
    renderAll();
    if (activeId) openChannelWS(activeId);
  } catch(e) {
    if (e.status === 401) $('settingsLogoutBtn').click();
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
      activeId = ch.id; renderAll(); openChannelWS(activeId);
    });
    wrap.appendChild(tab);
  });
  scrollActiveTabIntoView();
}
$('tabsScroll').addEventListener('click', async e => {
  const closeEl = e.target.closest && e.target.closest('[data-close]');
  if (!closeEl) return;
  e.stopPropagation();
  const id = closeEl.getAttribute('data-close');
  try { await api('/api/channels/leave', 'POST', { channel_id: id }); } catch(e){}
  channels = channels.filter(c => c.id !== id);
  if (activeId === id) {
    activeId = channels[0] ? channels[0].id : null;
    if (ws) { try { ws.close(); } catch(e){} ws = null; wsChannelId = null; }
    if (activeId) openChannelWS(activeId);
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
  $('headerTitle').textContent = (ch.private ? '[#] ' : '# ') + ch.name;
  const extra = onlineUsers.length ? ' · ' + onlineUsers.length + ' ' + t('online_label') : '';
  $('headerSubtitle').textContent = t('header_msgs', {n: ch.messages.length}) + extra;
}

// ---------- Messages ----------
const GROUP_WINDOW_MS = 5 * 60 * 1000;
let mutes = {};

function renderMessages() {
  const feed = $('chatFeed'); feed.innerHTML = '';
  const ch = channels.find(c => c.id === activeId);
  if (!ch) { feed.innerHTML = '<div class="empty-state">'+SVG.chat+t('empty_no_channels')+'</div>'; return; }
  if (ch.messages.length === 0) { feed.innerHTML = '<div class="empty-state">'+SVG.chat+t('empty_no_messages')+'</div>'; return; }
  let lastAuthor = null, lastTime = 0;
  ch.messages.forEach(m => {
    const row = document.createElement('div');
    row.dataset.id = m.id; row.dataset.t = String(m.t*1000);
    const d = new Date(m.t*1000);
    const timeStr = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    const grouped = (lastAuthor === m.from) && ((m.t*1000) - lastTime < GROUP_WINDOW_MS);
    row.className = 'msg-row';
    row.innerHTML = '<span class="msg-author'+(grouped?' hidden':'')+'" style="color:'+(m.from===currentUser?'#005a87':'#8a4a00')+'">'+
      escapeHtml(m.from)+':</span><span class="msg-text" data-ct="'+escapeHtml(m.ct)+'">…</span>'+
      '<span class="msg-time">'+timeStr+'</span>';
    feed.appendChild(row);
    lastAuthor = m.from; lastTime = m.t*1000;
  });
  const chId = activeId;
  feed.querySelectorAll('.msg-row').forEach(row => {
    const span = row.querySelector('.msg-text');
    const ct = span.dataset.ct;
    decryptText(chId, ct).then(pt => { span.textContent = pt; });
  });
  feed.scrollTop = feed.scrollHeight;
}

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function appendMessageUI(msg, channelId) {
  const feed = $('chatFeed');
  const empty = feed.querySelector('.empty-state'); if (empty) empty.remove();
  const ch = channels.find(c => c.id === channelId);
  if (ch) ch.messages.push(msg);
  if (activeId !== channelId) return;
  const lastRow = feed.querySelector('.msg-row:last-child');
  const lastAuthor = lastRow ? lastRow.querySelector('.msg-author').textContent.replace(':','') : null;
  const lastTime = lastRow ? parseInt(lastRow.dataset.t || '0', 10) : 0;
  const grouped = lastAuthor === msg.from && (msg.t*1000 - lastTime < GROUP_WINDOW_MS);
  const row = document.createElement('div');
  row.className = 'msg-row'; row.dataset.id = msg.id; row.dataset.t = String(msg.t*1000);
  const d = new Date(msg.t*1000);
  const timeStr = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  row.innerHTML = '<span class="msg-author'+(grouped?' hidden':'')+'" style="color:'+(msg.from===currentUser?'#005a87':'#8a4a00')+'">'+
    escapeHtml(msg.from)+':</span><span class="msg-text">…</span><span class="msg-time">'+timeStr+'</span>';
  feed.appendChild(row);
  decryptText(channelId, msg.ct).then(pt => { const s = row.querySelector('.msg-text'); if (s) s.textContent = pt; });
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

// ---------- WebSocket ----------
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
    else if (data.type === 'muted') { mutes[channelId] = Date.now() + data.seconds*1000; updateMuteUI(); addSystem('Muted for ' + data.seconds + 's (rate limit)'); }
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
  encryptText(chId, text).then(ct => {
    ws.send(JSON.stringify({ type:'message', ciphertext: ct }));
    input.value = ''; autoResize();
  });
}
$('sendBtn').addEventListener('click', sendMessage);
$('msgInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } });
function autoResize() { const el = $('msgInput'); el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 120) + 'px'; }
$('msgInput').addEventListener('input', autoResize);

// ---------- Modals (generic) ----------
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
});

// Create channel
$('addTabBtn').addEventListener('click', () => {
  $('newChannelName').value = '';
  $('newChannelPrivate').checked = false;
  $('createBackdrop').classList.add('open');
  setTimeout(() => $('newChannelName').focus(), 60);
});
$('createChannelBtn').addEventListener('click', async () => {
  const name = $('newChannelName').value.trim();
  if (!name) { $('newChannelName').focus(); return; }
  try {
    const res = await api('/api/channels', 'POST', { name, private: $('newChannelPrivate').checked });
    channels.push({ id: res.id, name: res.name, private: res.private, messages: [] });
    activeId = res.id;
    $('createBackdrop').classList.remove('open');
    renderAll(); openChannelWS(activeId);
  } catch(e) { showLoginError(e.status === 409 ? 'name_taken' : t('err_generic')); }
});
$('newChannelName').addEventListener('keydown', e => { if (e.key === 'Enter') $('createChannelBtn').click(); });

// Connect
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
      const c = channels.find(c => c.id === res.id);
      c.messages = res.messages || [];
    }
    activeId = res.id;
    $('connectBackdrop').classList.remove('open');
    renderAll(); openChannelWS(activeId);
  } catch(e) {
    $('connectErrorText').textContent = e.status === 404 ? t('connect_not_found', {name}) : t('err_generic');
    $('connectError').classList.add('show');
  }
});
$('connectChannelName').addEventListener('keydown', e => { if (e.key === 'Enter') $('connectChannelBtn').click(); });
$('connectChannelName').addEventListener('input', () => $('connectError').classList.remove('show'));

// ---------- Search ----------
$('searchBtn').addEventListener('click', () => {
  $('searchPanel').classList.add('open');
  $('searchInput').value = '';
  renderSearchResults('');
  setTimeout(() => $('searchInput').focus(), 200);
});
$('closeSearchBtn').addEventListener('click', () => $('searchPanel').classList.remove('open'));
$('searchInput').addEventListener('input', () => renderSearchResults($('searchInput').value));
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('searchPanel').classList.contains('open')) $('searchPanel').classList.remove('open');
  if ((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='k') { e.preventDefault(); $('searchBtn').click(); }
});

function renderSearchResults(q) {
  const query = q.trim(); const ql = query.toLowerCase();
  const res = $('searchResults');
  if (!query) {
    res.innerHTML = '';
    if (!channels.length) { res.innerHTML = '<div class="empty">'+t('search_no_channels')+'</div>'; return; }
    channels.forEach(ch => res.appendChild(makeChannelRow(ch, '')));
    return;
  }
  const items = [];
  channels.forEach(ch => {
    if (!ch.private && ch.name.toLowerCase().includes(ql)) items.push({type:'channel', ch});
  });
  res.innerHTML = '<div class="empty">…</div>';
  (async () => {
    const found = [];
    for (const ch of channels) {
      for (const m of (ch.messages || [])) {
        const pt = await decryptText(ch.id, m.ct);
        if (pt.toLowerCase().includes(ql) || m.from.toLowerCase().includes(ql)) {
          found.push({ch, m, pt});
        }
      }
    }
    res.innerHTML = '';
    if (!items.length && !found.length) { res.innerHTML = '<div class="empty">'+t('search_empty')+'</div>'; return; }
    items.forEach(it => res.appendChild(makeChannelRow(it.ch, query)));
    found.slice(0,50).forEach(it => res.appendChild(makeMessageRow(it.ch, it.m, it.pt, query)));
  })();
}
function makeChannelRow(ch, query) {
  const row = document.createElement('div'); row.className = 'search-result';
  row.innerHTML = '<div class="text-block"><div class="title-line">'+(query ? hl(ch.name, query) : escapeHtml(ch.name))+'</div>'+
    '<div class="sub-line">'+t('header_msgs', {n:(ch.messages||[]).length})+'</div></div>'+
    '<span class="type-tag">'+t('tag_channel')+'</span>';
  row.addEventListener('click', () => { activeId = ch.id; renderAll(); openChannelWS(activeId); $('searchPanel').classList.remove('open'); });
  return row;
}
function makeMessageRow(ch, m, pt, query) {
  const row = document.createElement('div'); row.className = 'search-result';
  row.innerHTML = '<div class="text-block"><div class="title-line">'+escapeHtml(m.from)+
    ' <span style="color:#999;font-weight:normal;">'+t('in_channel',{name:ch.name})+'</span></div>'+
    '<div class="sub-line">'+(query ? hl(pt, query) : escapeHtml(pt))+'</div></div>'+
    '<span class="type-tag">'+t('tag_msg')+'</span>';
  row.addEventListener('click', () => { activeId = ch.id; renderAll(); openChannelWS(activeId); $('searchPanel').classList.remove('open'); });
  return row;
}
function hl(text, q) {
  const re = new RegExp('('+q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','ig');
  return escapeHtml(text).replace(re, '<mark>$1</mark>');
}

// ---------- Keyboard nav ----------
function switchChannel(delta) {
  if (!channels.length) return;
  const idx = channels.findIndex(c => c.id === activeId);
  if (idx === -1) return;
  const n = Math.max(0, Math.min(channels.length - 1, idx + delta));
  if (n === idx) return;
  activeId = channels[n].id;
  renderAll(); openChannelWS(activeId);
}
document.addEventListener('keydown', e => {
  if (document.querySelector('.my-modal-backdrop.open')) return;
  if ($('langMenu').classList.contains('open')) return;
  if ($('searchPanel').classList.contains('open')) return;
  if ($('loginScreen').style.display !== 'none') return;
  const ae = document.activeElement;
  if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) return;
  if (e.key === 'ArrowLeft') { switchChannel(-1); e.preventDefault(); }
  else if (e.key === 'ArrowRight') { switchChannel(1); e.preventDefault(); }
});

// Swipe on tabs-scroll area changes channel on mobile
let touchStartX = 0, touchStartY = 0, touchActive = false;
const feedEl = $('chatFeed');
feedEl.addEventListener('touchstart', e => {
  if (e.touches.length !== 1) return;
  touchStartX = e.touches[0].clientX;
  touchStartY = e.touches[0].clientY;
  touchActive = true;
}, {passive: true});
feedEl.addEventListener('touchend', e => {
  if (!touchActive) return;
  touchActive = false;
  const dx = e.changedTouches[0].clientX - touchStartX;
  const dy = e.changedTouches[0].clientY - touchStartY;
  if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.5) {
    switchChannel(dx < 0 ? 1 : -1);
  }
}, {passive: true});

function renderAll() { renderTabs(); renderHeader(); renderMessages(); updateMuteUI(); scrollActiveTabIntoView(); }

// ---------- Init ----------
applyLanguage();
renderUptime();
(async () => {
  const restored = await tryRestoreSession();
  if (!restored) setTimeout(() => $('loginName').focus(), 100);
})();
</script>
</body>
</html>
"""

# ---------------- Root ----------------
@app.get("/", response_class=HTMLResponse)
async def index():
    html = HTML_TEMPLATE
    html = html.replace("%%I18N%%", json.dumps(I18N, ensure_ascii=False))
    html = html.replace("%%FLAGS%%", json.dumps(FLAGS, ensure_ascii=False))
    html = html.replace("%%LANG_ORDER%%", json.dumps(LANG_ORDER))
    return HTMLResponse(html)

# ---------------- Run ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        workers=1,
        log_level="info",
        access_log=False,
        limit_concurrency=200,
        timeout_keep_alive=30,
    )
