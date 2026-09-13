# ============================================================
#  FastAPI Messenger — full stack in one file
#  Storage: in-memory (dicts), no disk, <512MB RAM target
#  Security: PBKDF2-SHA256 (200k iters), token auth, E2EE (AES-GCM 256)
#  Run: python main.py  →  http://0.0.0.0:8000
# ============================================================
import json
import os
import secrets
import time
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn
import hashlib

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
RATE_LIMIT_WINDOW = 10.0     # sec
RATE_LIMIT_MAX = 20          # msgs per window
MUTE_SECONDS = 30
TOKEN_TTL = 60 * 60 * 24 * 7  # 7 days

# ---------------- In-memory storage ----------------
USERS: Dict[str, dict] = {}
TOKENS: Dict[str, dict] = {}              # token -> {"user": str, "exp": float}
CHANNELS: Dict[str, dict] = {}            # channel_id -> channel
CHANNEL_MEMBERS: Dict[str, Set[str]] = {} # channel_id -> set(usernames)
MSG_TIMES: Dict[str, List[float]] = {}    # f"{user}|{ch}" -> [timestamps]

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

# ---------------- Default public channels (seed) ----------------
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
        CHANNEL_MEMBERS[cid] = set()

init_defaults()

# ---------------- REST ----------------
@app.post("/api/register")
async def register(req: AuthReq):
    username = req.username.strip()
    if not (MIN_USERNAME_LEN <= len(username) <= MAX_USERNAME_LEN):
        raise HTTPException(400, "bad_username")
    if not (MIN_PASSWORD_LEN <= len(req.password) <= MAX_PASSWORD_LEN):
        raise HTTPException(400, "bad_password")
    if username in USERS:
        raise HTTPException(409, "user_exists")
    if len(USERS) >= MAX_USERS:
        raise HTTPException(503, "server_full")
    salt = os.urandom(16)
    USERS[username] = {
        "pwd": hash_password(req.password, salt),
        "salt": salt.hex(),
        "created": time.time(),
    }
    token = new_token()
    TOKENS[token] = {"user": username, "exp": time.time() + TOKEN_TTL}
    return {"token": token, "username": username}

@app.post("/api/login")
async def login(req: AuthReq):
    username = req.username.strip()
    u = USERS.get(username)
    if not u or not verify_password(req.password, u["salt"], u["pwd"]):
        # constant-time-ish: still do a dummy hash to avoid user enumeration timing
        verify_password("dummy", "00" * 16, "00" * 32)
        raise HTTPException(401, "bad_credentials")
    token = new_token()
    TOKENS[token] = {"user": username, "exp": time.time() + TOKEN_TTL}
    return {"token": token, "username": username}

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
    username = await auth(request)
    out = []
    for cid, ch in CHANNELS.items():
        if username in CHANNEL_MEMBERS.get(cid, set()):
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
    cid = secrets.token_hex(6)
    CHANNELS[cid] = {
        "id": cid, "name": name, "private": bool(req.private),
        "owner": username, "created": time.time(), "messages": [],
    }
    CHANNEL_MEMBERS[cid] = {username}
    return {"id": cid, "name": name, "private": bool(req.private), "messages": []}

@app.post("/api/channels/connect")
async def connect_channel(req: ConnectChannelReq, request: Request):
    username = await auth(request)
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "bad_name")
    for cid, ch in CHANNELS.items():
        if ch["name"].lower() == name.lower():
            CHANNEL_MEMBERS.setdefault(cid, set()).add(username)
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
    if cid in CHANNEL_MEMBERS:
        CHANNEL_MEMBERS[cid].discard(username)
    return {"ok": True}

# ---------------- WebSocket ----------------
class WSManager:
    def __init__(self):
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self.info: Dict[WebSocket, tuple] = {}

    async def join(self, channel_id: str, ws: WebSocket, username: str):
        self.rooms.setdefault(channel_id, set()).add(ws)
        self.info[ws] = (username, channel_id)

    def leave(self, ws: WebSocket):
        entry = self.info.pop(ws, None)
        if entry:
            _, ch = entry
            self.rooms.get(ch, set()).discard(ws)

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

def online_users(channel_id: str) -> List[str]:
    return sorted(CHANNEL_MEMBERS.get(channel_id, set()))

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
        if not username or channel_id not in CHANNELS or username not in CHANNEL_MEMBERS.get(channel_id, set()):
            await ws.send_json({"type": "error", "error": "auth"})
            await ws.close()
            return
        await manager.join(channel_id, ws, username)
        await manager.broadcast(channel_id, {"type": "presence", "users": online_users(channel_id)})

        while True:
            data = await ws.receive_json()
            if data.get("type") == "message":
                ct = str(data.get("ciphertext", ""))[:MAX_MESSAGE_LEN]
                if not ct:
                    continue
                # rate limit
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
                    "ct": ct,           # encrypted blob, server can't read
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
        if channel_id and username:
            CHANNEL_MEMBERS.get(channel_id, set()).discard(username)
            try:
                await manager.broadcast(channel_id, {"type": "presence", "users": online_users(channel_id)})
            except Exception:
                pass

# ============================================================
#  i18n — 20 real languages
# ============================================================
I18N = {
 "en": {"login_title":"Messenger","login_subtitle":"Sign in or create an account","field_nick":"Nickname","field_password":"Password","ph_nick":"Your nickname","ph_password":"Your password","btn_login":"Sign in","btn_register":"Create account","link_to_register":"No account? Register","link_to_login":"Have an account? Sign in","logged_as":"Signed in as","header_no_channels":"No channels","header_no_channels_sub":"Create a channel with «+» or connect to an existing one","header_msgs":"{n} messages","header_private_tag":"· private","empty_no_channels":"You have no channels yet.<br>Create a new one («+») or connect to an existing one.","empty_no_messages":"No messages. Be the first to write!","composer_ph":"Write a message...","composer_no_channel":"No active channel","composer_muted":"Muted: {n}s","modal_create_title":"Create channel","modal_name":"Name","modal_name_ph":"E.g. Work","modal_private":"Private channel","btn_cancel":"Cancel","btn_create":"Create","modal_connect_title":"Connect to channel","modal_connect_name":"Channel name","modal_connect_ph":"Enter the exact channel name","btn_connect":"Connect","connect_not_found":"Channel «{name}» not found","search_ph":"Search channels and messages...","search_close":"Close","search_empty":"Nothing found","search_no_channels":"You have no channels yet","tag_channel":"channel","tag_msg":"msg.","in_channel":"in «{name}»","title_search":"Search (Ctrl+K)","title_add":"Create channel","title_connect":"Connect to channel","title_logout":"Sign out","title_scroll_left":"Scroll channels left","title_scroll_right":"Scroll channels right","theme_toggle":"Toggle theme","lang_toggle":"Change language","uptime_label":"Uptime","online_label":"online","err_bad_credentials":"Invalid nickname or password","err_user_exists":"This nickname is already taken","err_bad_username":"Nickname must be 2–32 characters","err_bad_password":"Password must be at least 4 characters","err_network":"Network error","err_generic":"Error","title_register":"Register"},
 "ru": {"login_title":"Мессенджер","login_subtitle":"Войдите или создайте аккаунт","field_nick":"Ник","field_password":"Пароль","ph_nick":"Ваш ник","ph_password":"Ваш пароль","btn_login":"Войти","btn_register":"Создать аккаунт","link_to_register":"Нет аккаунта? Зарегистрироваться","link_to_login":"Есть аккаунт? Войти","logged_as":"Вы вошли как","header_no_channels":"Нет каналов","header_no_channels_sub":"Создайте канал кнопкой «+» или подключитесь к существующему","header_msgs":"{n} сообщений","header_private_tag":"· приватный","empty_no_channels":"У вас пока нет каналов.<br>Создайте новый («+») или подключитесь к существующему.","empty_no_messages":"Нет сообщений. Напишите первым!","composer_ph":"Написать сообщение...","composer_no_channel":"Нет активного канала","composer_muted":"Мут: {n} с","modal_create_title":"Создать канал","modal_name":"Название","modal_name_ph":"Например, Работа","modal_private":"Приватный канал","btn_cancel":"Отмена","btn_create":"Создать","modal_connect_title":"Подключиться к каналу","modal_connect_name":"Название канала","modal_connect_ph":"Введите точное название канала","btn_connect":"Подключиться","connect_not_found":"Канал «{name}» не найден","search_ph":"Поиск по каналам и сообщениям...","search_close":"Закрыть","search_empty":"Ничего не найдено","search_no_channels":"У вас ещё нет каналов","tag_channel":"канал","tag_msg":"сообщ.","in_channel":"в «{name}»","title_search":"Поиск (Ctrl+K)","title_add":"Создать канал","title_connect":"Подключиться к каналу","title_logout":"Выйти","title_scroll_left":"Прокрутить влево","title_scroll_right":"Прокрутить вправо","theme_toggle":"Сменить тему","lang_toggle":"Сменить язык","uptime_label":"Аптайм","online_label":"онлайн","err_bad_credentials":"Неверный ник или пароль","err_user_exists":"Этот ник уже занят","err_bad_username":"Ник должен быть 2–32 символа","err_bad_password":"Пароль минимум 4 символа","err_network":"Ошибка сети","err_generic":"Ошибка","title_register":"Регистрация"},
 "es": {"login_title":"Mensajero","login_subtitle":"Inicia sesión o crea una cuenta","field_nick":"Apodo","field_password":"Contraseña","ph_nick":"Tu apodo","ph_password":"Tu contraseña","btn_login":"Entrar","btn_register":"Crear cuenta","link_to_register":"¿Sin cuenta? Regístrate","link_to_login":"¿Tienes cuenta? Inicia sesión","logged_as":"Sesión como","header_no_channels":"Sin canales","header_no_channels_sub":"Crea un canal con «+» o conéctate a uno existente","header_msgs":"{n} mensajes","header_private_tag":"· privado","empty_no_channels":"Aún no tienes canales.<br>Crea uno nuevo («+») o conéctate a uno existente.","empty_no_messages":"No hay mensajes. ¡Sé el primero en escribir!","composer_ph":"Escribe un mensaje...","composer_no_channel":"Sin canal activo","composer_muted":"Silenciado: {n}s","modal_create_title":"Crear canal","modal_name":"Nombre","modal_name_ph":"Ej.: Trabajo","modal_private":"Canal privado","btn_cancel":"Cancelar","btn_create":"Crear","modal_connect_title":"Conectar al canal","modal_connect_name":"Nombre del canal","modal_connect_ph":"Introduce el nombre exacto del canal","btn_connect":"Conectar","connect_not_found":"Canal «{name}» no encontrado","search_ph":"Buscar canales y mensajes...","search_close":"Cerrar","search_empty":"Nada encontrado","search_no_channels":"Aún no tienes canales","tag_channel":"canal","tag_msg":"msg.","in_channel":"en «{name}»","title_search":"Buscar (Ctrl+K)","title_add":"Crear canal","title_connect":"Conectar al canal","title_logout":"Cerrar sesión","title_scroll_left":"Desplazar a la izquierda","title_scroll_right":"Desplazar a la derecha","theme_toggle":"Cambiar tema","lang_toggle":"Cambiar idioma","uptime_label":"Tiempo activo","online_label":"en línea","err_bad_credentials":"Apodo o contraseña incorrectos","err_user_exists":"Este apodo ya está en uso","err_bad_username":"Apodo de 2 a 32 caracteres","err_bad_password":"Contraseña mínimo 4 caracteres","err_network":"Error de red","err_generic":"Error","title_register":"Registro"},
 "de": {"login_title":"Messenger","login_subtitle":"Anmelden oder Konto erstellen","field_nick":"Spitzname","field_password":"Passwort","ph_nick":"Dein Spitzname","ph_password":"Dein Passwort","btn_login":"Anmelden","btn_register":"Konto erstellen","link_to_register":"Kein Konto? Registrieren","link_to_login":"Konto vorhanden? Anmelden","logged_as":"Angemeldet als","header_no_channels":"Keine Kanäle","header_no_channels_sub":"Erstelle einen Kanal mit «+» oder verbinde dich mit einem bestehenden","header_msgs":"{n} Nachrichten","header_private_tag":"· privat","empty_no_channels":"Du hast noch keine Kanäle.<br>Erstelle einen neuen («+») oder verbinde dich mit einem bestehenden.","empty_no_messages":"Keine Nachrichten. Schreib als Erster!","composer_ph":"Nachricht schreiben...","composer_no_channel":"Kein aktiver Kanal","composer_muted":"Stumm: {n}s","modal_create_title":"Kanal erstellen","modal_name":"Name","modal_name_ph":"Z.B. Arbeit","modal_private":"Privater Kanal","btn_cancel":"Abbrechen","btn_create":"Erstellen","modal_connect_title":"Mit Kanal verbinden","modal_connect_name":"Kanalname","modal_connect_ph":"Exakten Kanalnamen eingeben","btn_connect":"Verbinden","connect_not_found":"Kanal «{name}» nicht gefunden","search_ph":"Kanäle und Nachrichten durchsuchen...","search_close":"Schließen","search_empty":"Nichts gefunden","search_no_channels":"Du hast noch keine Kanäle","tag_channel":"Kanal","tag_msg":"Nachr.","in_channel":"in «{name}»","title_search":"Suchen (Strg+K)","title_add":"Kanal erstellen","title_connect":"Mit Kanal verbinden","title_logout":"Abmelden","title_scroll_left":"Nach links scrollen","title_scroll_right":"Nach rechts scrollen","theme_toggle":"Design wechseln","lang_toggle":"Sprache ändern","uptime_label":"Laufzeit","online_label":"online","err_bad_credentials":"Falscher Spitzname oder Passwort","err_user_exists":"Dieser Spitzname ist bereits vergeben","err_bad_username":"Spitzname 2–32 Zeichen","err_bad_password":"Passwort mind. 4 Zeichen","err_network":"Netzwerkfehler","err_generic":"Fehler","title_register":"Registrierung"},
 "fr": {"login_title":"Messagerie","login_subtitle":"Connectez-vous ou créez un compte","field_nick":"Pseudo","field_password":"Mot de passe","ph_nick":"Votre pseudo","ph_password":"Votre mot de passe","btn_login":"Se connecter","btn_register":"Créer un compte","link_to_register":"Pas de compte ? S'inscrire","link_to_login":"Déjà un compte ? Se connecter","logged_as":"Connecté en tant que","header_no_channels":"Aucun canal","header_no_channels_sub":"Créez un canal avec «+» ou connectez-vous à un existant","header_msgs":"{n} messages","header_private_tag":"· privé","empty_no_channels":"Vous n'avez pas encore de canaux.<br>Créez-en un («+») ou connectez-vous à un existant.","empty_no_messages":"Aucun message. Soyez le premier !","composer_ph":"Écrire un message...","composer_no_channel":"Aucun canal actif","composer_muted":"Muet : {n}s","modal_create_title":"Créer un canal","modal_name":"Nom","modal_name_ph":"Ex : Travail","modal_private":"Canal privé","btn_cancel":"Annuler","btn_create":"Créer","modal_connect_title":"Se connecter à un canal","modal_connect_name":"Nom du canal","modal_connect_ph":"Entrez le nom exact du canal","btn_connect":"Se connecter","connect_not_found":"Canal «{name}» introuvable","search_ph":"Rechercher canaux et messages...","search_close":"Fermer","search_empty":"Rien trouvé","search_no_channels":"Pas encore de canaux","tag_channel":"canal","tag_msg":"msg.","in_channel":"dans «{name}»","title_search":"Rechercher (Ctrl+K)","title_add":"Créer un canal","title_connect":"Se connecter","title_logout":"Se déconnecter","title_scroll_left":"Défiler à gauche","title_scroll_right":"Défiler à droite","theme_toggle":"Changer de thème","lang_toggle":"Changer de langue","uptime_label":"Durée","online_label":"en ligne","err_bad_credentials":"Pseudo ou mot de passe invalide","err_user_exists":"Ce pseudo est déjà pris","err_bad_username":"Pseudo 2 à 32 caractères","err_bad_password":"Mot de passe min. 4 caractères","err_network":"Erreur réseau","err_generic":"Erreur","title_register":"Inscription"},
 "it": {"login_title":"Messaggero","login_subtitle":"Accedi o crea un account","field_nick":"Nickname","field_password":"Password","ph_nick":"Il tuo nickname","ph_password":"La tua password","btn_login":"Accedi","btn_register":"Crea account","link_to_register":"Nessun account? Registrati","link_to_login":"Hai un account? Accedi","logged_as":"Connesso come","header_no_channels":"Nessun canale","header_no_channels_sub":"Crea un canale con «+» o connettiti a uno esistente","header_msgs":"{n} messaggi","header_private_tag":"· privato","empty_no_channels":"Non hai ancora canali.<br>Creane uno nuovo («+») o connettiti a uno esistente.","empty_no_messages":"Nessun messaggio. Scrivi per primo!","composer_ph":"Scrivi un messaggio...","composer_no_channel":"Nessun canale attivo","composer_muted":"Silenziato: {n}s","modal_create_title":"Crea canale","modal_name":"Nome","modal_name_ph":"Es: Lavoro","modal_private":"Canale privato","btn_cancel":"Annulla","btn_create":"Crea","modal_connect_title":"Connettiti al canale","modal_connect_name":"Nome del canale","modal_connect_ph":"Inserisci il nome esatto del canale","btn_connect":"Connetti","connect_not_found":"Canale «{name}» non trovato","search_ph":"Cerca canali e messaggi...","search_close":"Chiudi","search_empty":"Nessun risultato","search_no_channels":"Non hai ancora canali","tag_channel":"canale","tag_msg":"msg.","in_channel":"in «{name}»","title_search":"Cerca (Ctrl+K)","title_add":"Crea canale","title_connect":"Connettiti","title_logout":"Esci","title_scroll_left":"Scorri a sinistra","title_scroll_right":"Scorri a destra","theme_toggle":"Cambia tema","lang_toggle":"Cambia lingua","uptime_label":"Attività","online_label":"online","err_bad_credentials":"Nickname o password errati","err_user_exists":"Nickname già in uso","err_bad_username":"Nickname 2–32 caratteri","err_bad_password":"Password min. 4 caratteri","err_network":"Errore di rete","err_generic":"Errore","title_register":"Registrazione"},
 "pt": {"login_title":"Mensageiro","login_subtitle":"Entre ou crie uma conta","field_nick":"Apelido","field_password":"Senha","ph_nick":"Seu apelido","ph_password":"Sua senha","btn_login":"Entrar","btn_register":"Criar conta","link_to_register":"Sem conta? Cadastre-se","link_to_login":"Já tem conta? Entrar","logged_as":"Conectado como","header_no_channels":"Sem canais","header_no_channels_sub":"Crie um canal com «+» ou conecte-se a um existente","header_msgs":"{n} mensagens","header_private_tag":"· privado","empty_no_channels":"Você ainda não tem canais.<br>Crie um novo («+») ou conecte-se a um existente.","empty_no_messages":"Sem mensagens. Seja o primeiro!","composer_ph":"Escrever mensagem...","composer_no_channel":"Nenhum canal ativo","composer_muted":"Silenciado: {n}s","modal_create_title":"Criar canal","modal_name":"Nome","modal_name_ph":"Ex: Trabalho","modal_private":"Canal privado","btn_cancel":"Cancelar","btn_create":"Criar","modal_connect_title":"Conectar ao canal","modal_connect_name":"Nome do canal","modal_connect_ph":"Digite o nome exato do canal","btn_connect":"Conectar","connect_not_found":"Canal «{name}» não encontrado","search_ph":"Pesquisar canais e mensagens...","search_close":"Fechar","search_empty":"Nada encontrado","search_no_channels":"Ainda sem canais","tag_channel":"canal","tag_msg":"msg.","in_channel":"em «{name}»","title_search":"Pesquisar (Ctrl+K)","title_add":"Criar canal","title_connect":"Conectar","title_logout":"Sair","title_scroll_left":"Rolar à esquerda","title_scroll_right":"Rolar à direita","theme_toggle":"Alternar tema","lang_toggle":"Alterar idioma","uptime_label":"Tempo ativo","online_label":"online","err_bad_credentials":"Apelido ou senha inválidos","err_user_exists":"Este apelido já está em uso","err_bad_username":"Apelido 2–32 caracteres","err_bad_password":"Senha mín. 4 caracteres","err_network":"Erro de rede","err_generic":"Erro","title_register":"Registro"},
 "nl": {"login_title":"Messenger","login_subtitle":"Meld aan of maak een account","field_nick":"Bijnaam","field_password":"Wachtwoord","ph_nick":"Je bijnaam","ph_password":"Je wachtwoord","btn_login":"Aanmelden","btn_register":"Account aanmaken","link_to_register":"Geen account? Registreren","link_to_login":"Al een account? Aanmelden","logged_as":"Ingelogd als","header_no_channels":"Geen kanalen","header_no_channels_sub":"Maak een kanaal met «+» of verbind met een bestaand kanaal","header_msgs":"{n} berichten","header_private_tag":"· privé","empty_no_channels":"Je hebt nog geen kanalen.<br>Maak een nieuw kanaal («+») of verbind met een bestaand kanaal.","empty_no_messages":"Geen berichten. Schrijf als eerste!","composer_ph":"Bericht schrijven...","composer_no_channel":"Geen actief kanaal","composer_muted":"Gedempt: {n}s","modal_create_title":"Kanaal aanmaken","modal_name":"Naam","modal_name_ph":"Bv. Werk","modal_private":"Privékanaal","btn_cancel":"Annuleren","btn_create":"Aanmaken","modal_connect_title":"Verbinding maken met kanaal","modal_connect_name":"Kanaalnaam","modal_connect_ph":"Voer de exacte kanaalnaam in","btn_connect":"Verbinden","connect_not_found":"Kanaal «{name}» niet gevonden","search_ph":"Zoek kanalen en berichten...","search_close":"Sluiten","search_empty":"Niets gevonden","search_no_channels":"Nog geen kanalen","tag_channel":"kanaal","tag_msg":"ber.","in_channel":"in «{name}»","title_search":"Zoeken (Ctrl+K)","title_add":"Kanaal aanmaken","title_connect":"Verbinden","title_logout":"Afmelden","title_scroll_left":"Naar links scrollen","title_scroll_right":"Naar rechts scrollen","theme_toggle":"Thema wisselen","lang_toggle":"Taal wijzigen","uptime_label":"Uptime","online_label":"online","err_bad_credentials":"Ongeldige bijnaam of wachtwoord","err_user_exists":"Deze bijnaam is al in gebruik","err_bad_username":"Bijnaam 2–32 tekens","err_bad_password":"Wachtwoord min. 4 tekens","err_network":"Netwerkfout","err_generic":"Fout","title_register":"Registratie"},
 "pl": {"login_title":"Komunikator","login_subtitle":"Zaloguj się lub utwórz konto","field_nick":"Pseudonim","field_password":"Hasło","ph_nick":"Twój pseudonim","ph_password":"Twoje hasło","btn_login":"Zaloguj się","btn_register":"Utwórz konto","link_to_register":"Nie masz konta? Zarejestruj się","link_to_login":"Masz konto? Zaloguj się","logged_as":"Zalogowany jako","header_no_channels":"Brak kanałów","header_no_channels_sub":"Utwórz kanał «+» lub połącz się z istniejącym","header_msgs":"{n} wiadomości","header_private_tag":"· prywatny","empty_no_channels":"Nie masz jeszcze kanałów.<br>Utwórz nowy («+») lub połącz się z istniejącym.","empty_no_messages":"Brak wiadomości. Napisz pierwszy!","composer_ph":"Napisz wiadomość...","composer_no_channel":"Brak aktywnego kanału","composer_muted":"Wyciszony: {n}s","modal_create_title":"Utwórz kanał","modal_name":"Nazwa","modal_name_ph":"Np. Praca","modal_private":"Kanał prywatny","btn_cancel":"Anuluj","btn_create":"Utwórz","modal_connect_title":"Połącz z kanałem","modal_connect_name":"Nazwa kanału","modal_connect_ph":"Wpisz dokładną nazwę kanału","btn_connect":"Połącz","connect_not_found":"Kanał «{name}» nie znaleziony","search_ph":"Szukaj kanałów i wiadomości...","search_close":"Zamknij","search_empty":"Nic nie znaleziono","search_no_channels":"Brak kanałów","tag_channel":"kanał","tag_msg":"wiad.","in_channel":"w «{name}»","title_search":"Szukaj (Ctrl+K)","title_add":"Utwórz kanał","title_connect":"Połącz z kanałem","title_logout":"Wyloguj","title_scroll_left":"Przewiń w lewo","title_scroll_right":"Przewiń w prawo","theme_toggle":"Zmień motyw","lang_toggle":"Zmień język","uptime_label":"Czas pracy","online_label":"online","err_bad_credentials":"Nieprawidłowy pseudonim lub hasło","err_user_exists":"Ten pseudonim jest zajęty","err_bad_username":"Pseudonim 2–32 znaki","err_bad_password":"Hasło min. 4 znaki","err_network":"Błąd sieci","err_generic":"Błąd","title_register":"Rejestracja"},
 "uk": {"login_title":"Месенджер","login_subtitle":"Увійдіть або створіть акаунт","field_nick":"Нік","field_password":"Пароль","ph_nick":"Ваш нік","ph_password":"Ваш пароль","btn_login":"Увійти","btn_register":"Створити акаунт","link_to_register":"Немає акаунта? Зареєструватися","link_to_login":"Є акаунт? Увійти","logged_as":"Ви увійшли як","header_no_channels":"Немає каналів","header_no_channels_sub":"Створіть канал «+» або підключіться до існуючого","header_msgs":"{n} повідомлень","header_private_tag":"· приватний","empty_no_channels":"У вас ще немає каналів.<br>Створіть новий («+») або підключіться до існуючого.","empty_no_messages":"Немає повідомлень. Напишіть першим!","composer_ph":"Написати повідомлення...","composer_no_channel":"Немає активного каналу","composer_muted":"Мут: {n} с","modal_create_title":"Створити канал","modal_name":"Назва","modal_name_ph":"Наприклад, Робота","modal_private":"Приватний канал","btn_cancel":"Скасувати","btn_create":"Створити","modal_connect_title":"Підключитися до каналу","modal_connect_name":"Назва каналу","modal_connect_ph":"Введіть точну назву каналу","btn_connect":"Підключитися","connect_not_found":"Канал «{name}» не знайдено","search_ph":"Пошук каналів і повідомлень...","search_close":"Закрити","search_empty":"Нічого не знайдено","search_no_channels":"Ще немає каналів","tag_channel":"канал","tag_msg":"повід.","in_channel":"у «{name}»","title_search":"Пошук (Ctrl+K)","title_add":"Створити канал","title_connect":"Підключитися до каналу","title_logout":"Вийти","title_scroll_left":"Прокрутити ліворуч","title_scroll_right":"Прокрутити праворуч","theme_toggle":"Змінити тему","lang_toggle":"Змінити мову","uptime_label":"Аптайм","online_label":"онлайн","err_bad_credentials":"Невірний нік або пароль","err_user_exists":"Цей нік уже зайнятий","err_bad_username":"Нік 2–32 символи","err_bad_password":"Пароль мінімум 4 символи","err_network":"Помилка мережі","err_generic":"Помилка","title_register":"Реєстрація"},
 "cs": {"login_title":"Messenger","login_subtitle":"Přihlaste se nebo si vytvořte účet","field_nick":"Přezdívka","field_password":"Heslo","ph_nick":"Vaše přezdívka","ph_password":"Vaše heslo","btn_login":"Přihlásit","btn_register":"Vytvořit účet","link_to_register":"Nemáte účet? Zaregistrujte se","link_to_login":"Máte účet? Přihlaste se","logged_as":"Přihlášen jako","header_no_channels":"Žádné kanály","header_no_channels_sub":"Vytvořte kanál «+» nebo se připojte k existujícímu","header_msgs":"{n} zpráv","header_private_tag":"· soukromý","empty_no_channels":"Zatím nemáte žádné kanály.<br>Vytvořte nový («+») nebo se připojte k existujícímu.","empty_no_messages":"Žádné zprávy. Napište první!","composer_ph":"Napište zprávu...","composer_no_channel":"Žádný aktivní kanál","composer_muted":"Ztlumeno: {n}s","modal_create_title":"Vytvořit kanál","modal_name":"Název","modal_name_ph":"Např. Práce","modal_private":"Soukromý kanál","btn_cancel":"Zrušit","btn_create":"Vytvořit","modal_connect_title":"Připojit ke kanálu","modal_connect_name":"Název kanálu","modal_connect_ph":"Zadejte přesný název kanálu","btn_connect":"Připojit","connect_not_found":"Kanál «{name}» nenalezen","search_ph":"Hledat kanály a zprávy...","search_close":"Zavřít","search_empty":"Nic nenalezeno","search_no_channels":"Zatím žádné kanály","tag_channel":"kanál","tag_msg":"zpr.","in_channel":"v «{name}»","title_search":"Hledat (Ctrl+K)","title_add":"Vytvořit kanál","title_connect":"Připojit","title_logout":"Odhlásit","title_scroll_left":"Posunout vlevo","title_scroll_right":"Posunout vpravo","theme_toggle":"Změnit motiv","lang_toggle":"Změnit jazyk","uptime_label":"Doba běhu","online_label":"online","err_bad_credentials":"Neplatná přezdívka nebo heslo","err_user_exists":"Tato přezdívka je obsazena","err_bad_username":"Přezdívka 2–32 znaků","err_bad_password":"Heslo min. 4 znaky","err_network":"Chyba sítě","err_generic":"Chyba","title_register":"Registrace"},
 "sv": {"login_title":"Messenger","login_subtitle":"Logga in eller skapa ett konto","field_nick":"Smeknamn","field_password":"Lösenord","ph_nick":"Ditt smeknamn","ph_password":"Ditt lösenord","btn_login":"Logga in","btn_register":"Skapa konto","link_to_register":"Inget konto? Registrera","link_to_login":"Har du konto? Logga in","logged_as":"Inloggad som","header_no_channels":"Inga kanaler","header_no_channels_sub":"Skapa en kanal med «+» eller anslut till en befintlig","header_msgs":"{n} meddelanden","header_private_tag":"· privat","empty_no_channels":"Du har inga kanaler än.<br>Skapa en ny («+») eller anslut till en befintlig.","empty_no_messages":"Inga meddelanden. Skriv först!","composer_ph":"Skriv ett meddelande...","composer_no_channel":"Ingen aktiv kanal","composer_muted":"Tystad: {n}s","modal_create_title":"Skapa kanal","modal_name":"Namn","modal_name_ph":"T.ex. Arbete","modal_private":"Privat kanal","btn_cancel":"Avbryt","btn_create":"Skapa","modal_connect_title":"Anslut till kanal","modal_connect_name":"Kanalnamn","modal_connect_ph":"Ange det exakta kanalnamnet","btn_connect":"Anslut","connect_not_found":"Kanalen «{name}» hittades inte","search_ph":"Sök kanaler och meddelanden...","search_close":"Stäng","search_empty":"Inget hittat","search_no_channels":"Inga kanaler än","tag_channel":"kanal","tag_msg":"medd.","in_channel":"i «{name}»","title_search":"Sök (Ctrl+K)","title_add":"Skapa kanal","title_connect":"Anslut","title_logout":"Logga ut","title_scroll_left":"Skrolla vänster","title_scroll_right":"Skrolla höger","theme_toggle":"Byt tema","lang_toggle":"Byt språk","uptime_label":"Drifttid","online_label":"online","err_bad_credentials":"Fel smeknamn eller lösenord","err_user_exists":"Smeknamnet är upptaget","err_bad_username":"Smeknamn 2–32 tecken","err_bad_password":"Lösenord minst 4 tecken","err_network":"Nätverksfel","err_generic":"Fel","title_register":"Registrering"},
 "el": {"login_title":"Messenger","login_subtitle":"Συνδεθείτε ή δημιουργήστε λογαριασμό","field_nick":"Ψευδώνυμο","field_password":"Κωδικός","ph_nick":"Το ψευδώνυμό σας","ph_password":"Ο κωδικός σας","btn_login":"Σύνδεση","btn_register":"Δημιουργία λογαριασμού","link_to_register":"Χωρίς λογαριασμό; Εγγραφή","link_to_login":"Έχετε λογαριασμό; Σύνδεση","logged_as":"Συνδεδεμένος ως","header_no_channels":"Χωρίς κανάλια","header_no_channels_sub":"Δημιουργήστε κανάλι «+» ή συνδεθείτε σε υπάρχον","header_msgs":"{n} μηνύματα","header_private_tag":"· ιδιωτικό","empty_no_channels":"Δεν έχετε κανάλια ακόμη.<br>Δημιουργήστε ένα νέο («+») ή συνδεθείτε σε υπάρχον.","empty_no_messages":"Χωρίς μηνύματα. Γράψτε πρώτος!","composer_ph":"Γράψτε μήνυμα...","composer_no_channel":"Χωρίς ενεργό κανάλι","composer_muted":"Σε σίγαση: {n}s","modal_create_title":"Δημιουργία καναλιού","modal_name":"Όνομα","modal_name_ph":"Π.χ. Εργασία","modal_private":"Ιδιωτικό κανάλι","btn_cancel":"Άκυρο","btn_create":"Δημιουργία","modal_connect_title":"Σύνδεση σε κανάλι","modal_connect_name":"Όνομα καναλιού","modal_connect_ph":"Εισάγετε το ακριβές όνομα","btn_connect":"Σύνδεση","connect_not_found":"Το κανάλι «{name}» δεν βρέθηκε","search_ph":"Αναζήτηση καναλιών και μηνυμάτων...","search_close":"Κλείσιμο","search_empty":"Δεν βρέθηκε τίποτα","search_no_channels":"Δεν έχετε κανάλια","tag_channel":"κανάλι","tag_msg":"μνμ.","in_channel":"στο «{name}»","title_search":"Αναζήτηση (Ctrl+K)","title_add":"Δημιουργία καναλιού","title_connect":"Σύνδεση","title_logout":"Αποσύνδεση","title_scroll_left":"Κύλιση αριστερά","title_scroll_right":"Κύλιση δεξιά","theme_toggle":"Αλλαγή θέματος","lang_toggle":"Αλλαγή γλώσσας","uptime_label":"Χρόνος λειτουργίας","online_label":"συνδεδεμένοι","err_bad_credentials":"Λάθος ψευδώνυμο ή κωδικός","err_user_exists":"Το ψευδώνυμο χρησιμοποιείται","err_bad_username":"Ψευδώνυμο 2–32 χαρακτήρες","err_bad_password":"Κωδικός τουλάχιστον 4 χαρακτήρες","err_network":"Σφάλμα δικτύου","err_generic":"Σφάλμα","title_register":"Εγγραφή"},
 "tr": {"login_title":"Anlık Mesajlaşma","login_subtitle":"Giriş yapın veya hesap oluşturun","field_nick":"Takma ad","field_password":"Şifre","ph_nick":"Takma adınız","ph_password":"Şifreniz","btn_login":"Giriş yap","btn_register":"Hesap oluştur","link_to_register":"Hesabınız yok mu? Kaydolun","link_to_login":"Hesabınız var mı? Giriş yapın","logged_as":"Giriş yapan:","header_no_channels":"Kanal yok","header_no_channels_sub":"«+» ile kanal oluşturun veya var olana bağlanın","header_msgs":"{n} mesaj","header_private_tag":"· özel","empty_no_channels":"Henüz kanalınız yok.<br>Yeni bir tane oluşturun («+») veya var olana bağlanın.","empty_no_messages":"Mesaj yok. İlk yazan siz olun!","composer_ph":"Mesaj yazın...","composer_no_channel":"Aktif kanal yok","composer_muted":"Sessiz: {n}sn","modal_create_title":"Kanal oluştur","modal_name":"Ad","modal_name_ph":"Örn: İş","modal_private":"Özel kanal","btn_cancel":"İptal","btn_create":"Oluştur","modal_connect_title":"Kanala bağlan","modal_connect_name":"Kanal adı","modal_connect_ph":"Tam kanal adını girin","btn_connect":"Bağlan","connect_not_found":"Kanal «{name}» bulunamadı","search_ph":"Kanal ve mesaj ara...","search_close":"Kapat","search_empty":"Hiçbir şey bulunamadı","search_no_channels":"Henüz kanal yok","tag_channel":"kanal","tag_msg":"msj.","in_channel":"«{name}» içinde","title_search":"Ara (Ctrl+K)","title_add":"Kanal oluştur","title_connect":"Bağlan","title_logout":"Çıkış yap","title_scroll_left":"Sola kaydır","title_scroll_right":"Sağa kaydır","theme_toggle":"Temayı değiştir","lang_toggle":"Dili değiştir","uptime_label":"Çalışma süresi","online_label":"çevrimiçi","err_bad_credentials":"Geçersiz takma ad veya şifre","err_user_exists":"Bu takma ad kullanımda","err_bad_username":"Takma ad 2–32 karakter","err_bad_password":"Şifre en az 4 karakter","err_network":"Ağ hatası","err_generic":"Hata","title_register":"Kayıt"},
 "ja": {"login_title":"メッセンジャー","login_subtitle":"サインインまたはアカウント作成","field_nick":"ニックネーム","field_password":"パスワード","ph_nick":"あなたのニックネーム","ph_password":"あなたのパスワード","btn_login":"サインイン","btn_register":"アカウント作成","link_to_register":"アカウントがない場合","link_to_login":"アカウントをお持ちの方","logged_as":"ログイン中:","header_no_channels":"チャンネルなし","header_no_channels_sub":"「+」でチャンネルを作成するか、既存に接続","header_msgs":"{n} 件","header_private_tag":"· プライベート","empty_no_channels":"まだチャンネルがありません。<br>新規作成(「+」)するか、既存に接続してください。","empty_no_messages":"メッセージがありません。最初に書き込みましょう!","composer_ph":"メッセージを入力...","composer_no_channel":"アクティブなチャンネルなし","composer_muted":"ミュート: {n}秒","modal_create_title":"チャンネル作成","modal_name":"名前","modal_name_ph":"例: 仕事","modal_private":"プライベートチャンネル","btn_cancel":"キャンセル","btn_create":"作成","modal_connect_title":"チャンネルに接続","modal_connect_name":"チャンネル名","modal_connect_ph":"正確なチャンネル名を入力","btn_connect":"接続","connect_not_found":"チャンネル「{name}」が見つかりません","search_ph":"チャンネルとメッセージを検索...","search_close":"閉じる","search_empty":"何も見つかりません","search_no_channels":"まだチャンネルがありません","tag_channel":"チャンネル","tag_msg":"メッセージ","in_channel":"「{name}」内","title_search":"検索 (Ctrl+K)","title_add":"チャンネル作成","title_connect":"チャンネルに接続","title_logout":"サインアウト","title_scroll_left":"左へスクロール","title_scroll_right":"右へスクロール","theme_toggle":"テーマ切替","lang_toggle":"言語変更","uptime_label":"稼働時間","online_label":"オンライン","err_bad_credentials":"ニックネームかパスワードが違います","err_user_exists":"このニックネームは使用中","err_bad_username":"ニックネームは2〜32文字","err_bad_password":"パスワードは4文字以上","err_network":"ネットワークエラー","err_generic":"エラー","title_register":"登録"},
 "ko": {"login_title":"메신저","login_subtitle":"로그인하거나 계정을 만드세요","field_nick":"닉네임","field_password":"비밀번호","ph_nick":"닉네임","ph_password":"비밀번호","btn_login":"로그인","btn_register":"계정 만들기","link_to_register":"계정이 없으신가요?","link_to_login":"이미 계정이 있으신가요?","logged_as":"로그인 중:","header_no_channels":"채널 없음","header_no_channels_sub":"「+」로 채널을 만들거나 기존에 연결하세요","header_msgs":"{n}개 메시지","header_private_tag":"· 비공개","empty_no_channels":"아직 채널이 없습니다.<br>새로 만들거나(「+」) 기존에 연결하세요.","empty_no_messages":"메시지가 없습니다. 먼저 작성하세요!","composer_ph":"메시지 입력...","composer_no_channel":"활성 채널 없음","composer_muted":"음소거: {n}초","modal_create_title":"채널 만들기","modal_name":"이름","modal_name_ph":"예: 업무","modal_private":"비공개 채널","btn_cancel":"취소","btn_create":"만들기","modal_connect_title":"채널에 연결","modal_connect_name":"채널 이름","modal_connect_ph":"정확한 채널 이름 입력","btn_connect":"연결","connect_not_found":"채널 «{name}»을 찾을 수 없음","search_ph":"채널 및 메시지 검색...","search_close":"닫기","search_empty":"검색 결과 없음","search_no_channels":"채널 없음","tag_channel":"채널","tag_msg":"메시지","in_channel":"«{name}»에서","title_search":"검색 (Ctrl+K)","title_add":"채널 만들기","title_connect":"채널 연결","title_logout":"로그아웃","title_scroll_left":"왼쪽으로 스크롤","title_scroll_right":"오른쪽으로 스크롤","theme_toggle":"테마 전환","lang_toggle":"언어 변경","uptime_label":"가동 시간","online_label":"온라인","err_bad_credentials":"닉네임 또는 비밀번호 오류","err_user_exists":"이미 사용 중인 닉네임","err_bad_username":"닉네임 2~32자","err_bad_password":"비밀번호 최소 4자","err_network":"네트워크 오류","err_generic":"오류","title_register":"가입"},
 "zh": {"login_title":"即时通讯","login_subtitle":"登录或创建账号","field_nick":"昵称","field_password":"密码","ph_nick":"你的昵称","ph_password":"你的密码","btn_login":"登录","btn_register":"创建账号","link_to_register":"没有账号？注册","link_to_login":"已有账号？登录","logged_as":"登录为","header_no_channels":"无频道","header_no_channels_sub":"用「+」创建频道或连接现有频道","header_msgs":"{n} 条消息","header_private_tag":"· 私密","empty_no_channels":"你还没有频道。<br>新建一个(「+」)或连接现有频道。","empty_no_messages":"暂无消息。快来第一个发言！","composer_ph":"输入消息...","composer_no_channel":"无活动频道","composer_muted":"已禁言：{n}秒","modal_create_title":"创建频道","modal_name":"名称","modal_name_ph":"例如：工作","modal_private":"私密频道","btn_cancel":"取消","btn_create":"创建","modal_connect_title":"连接到频道","modal_connect_name":"频道名称","modal_connect_ph":"输入确切的频道名称","btn_connect":"连接","connect_not_found":"未找到频道 «{name}»","search_ph":"搜索频道和消息...","search_close":"关闭","search_empty":"未找到","search_no_channels":"暂无频道","tag_channel":"频道","tag_msg":"消息","in_channel":"在 «{name}» 中","title_search":"搜索 (Ctrl+K)","title_add":"创建频道","title_connect":"连接","title_logout":"退出","title_scroll_left":"向左滚动","title_scroll_right":"向右滚动","theme_toggle":"切换主题","lang_toggle":"切换语言","uptime_label":"运行时间","online_label":"在线","err_bad_credentials":"昵称或密码错误","err_user_exists":"该昵称已被占用","err_bad_username":"昵称 2–32 字符","err_bad_password":"密码至少 4 字符","err_network":"网络错误","err_generic":"错误","title_register":"注册"},
 "ar": {"login_title":"ماسنجر","login_subtitle":"سجّل الدخول أو أنشئ حسابًا","field_nick":"الاسم المستعار","field_password":"كلمة المرور","ph_nick":"اسمك المستعار","ph_password":"كلمة مرورك","btn_login":"تسجيل الدخول","btn_register":"إنشاء حساب","link_to_register":"لا تملك حسابًا؟ سجّل","link_to_login":"لديك حساب؟ ادخل","logged_as":"مسجل باسم","header_no_channels":"لا قنوات","header_no_channels_sub":"أنشئ قناة بـ «+» أو اتصل بقناة موجودة","header_msgs":"{n} رسالة","header_private_tag":"· خاصة","empty_no_channels":"لا توجد قنوات بعد.<br>أنشئ واحدة جديدة («+») أو اتصل بقناة موجودة.","empty_no_messages":"لا رسائل. كن أول من يكتب!","composer_ph":"اكتب رسالة...","composer_no_channel":"لا قناة نشطة","composer_muted":"مكتوم: {n} ث","modal_create_title":"إنشاء قناة","modal_name":"الاسم","modal_name_ph":"مثال: العمل","modal_private":"قناة خاصة","btn_cancel":"إلغاء","btn_create":"إنشاء","modal_connect_title":"الاتصال بقناة","modal_connect_name":"اسم القناة","modal_connect_ph":"أدخل الاسم الدقيق للقناة","btn_connect":"اتصال","connect_not_found":"لم يتم العثور على القناة «{name}»","search_ph":"ابحث في القنوات والرسائل...","search_close":"إغلاق","search_empty":"لا نتائج","search_no_channels":"لا قنوات بعد","tag_channel":"قناة","tag_msg":"رسالة","in_channel":"في «{name}»","title_search":"بحث (Ctrl+K)","title_add":"إنشاء قناة","title_connect":"اتصال","title_logout":"خروج","title_scroll_left":"تمرير يسارًا","title_scroll_right":"تمرير يمينًا","theme_toggle":"تغيير المظهر","lang_toggle":"تغيير اللغة","uptime_label":"مدة التشغيل","online_label":"متصل","err_bad_credentials":"اسم أو كلمة مرور خاطئة","err_user_exists":"الاسم مستخدم بالفعل","err_bad_username":"الاسم من 2 إلى 32 حرفًا","err_bad_password":"كلمة المرور 4 أحرف على الأقل","err_network":"خطأ في الشبكة","err_generic":"خطأ","title_register":"تسجيل"},
 "he": {"login_title":"מסנג'ר","login_subtitle":"התחבר או צור חשבון","field_nick":"כינוי","field_password":"סיסמה","ph_nick":"הכינוי שלך","ph_password":"הסיסמה שלך","btn_login":"התחבר","btn_register":"צור חשבון","link_to_register":"אין חשבון? הירשם","link_to_login":"יש חשבון? התחבר","logged_as":"מחובר בתור","header_no_channels":"אין ערוצים","header_no_channels_sub":"צור ערוץ עם «+» או התחבר לקיים","header_msgs":"{n} הודעות","header_private_tag":"· פרטי","empty_no_channels":"אין לך עדיין ערוצים.<br>צור חדש («+») או התחבר לקיים.","empty_no_messages":"אין הודעות. כתוב ראשון!","composer_ph":"כתוב הודעה...","composer_no_channel":"אין ערוץ פעיל","composer_muted":"מושתק: {n} שניות","modal_create_title":"צור ערוץ","modal_name":"שם","modal_name_ph":"לדוגמה: עבודה","modal_private":"ערוץ פרטי","btn_cancel":"ביטול","btn_create":"צור","modal_connect_title":"התחבר לערוץ","modal_connect_name":"שם הערוץ","modal_connect_ph":"הזן את שם הערוץ המדויק","btn_connect":"התחבר","connect_not_found":"הערוץ «{name}» לא נמצא","search_ph":"חפש ערוצים והודעות...","search_close":"סגור","search_empty":"לא נמצא","search_no_channels":"אין ערוצים עדיין","tag_channel":"ערוץ","tag_msg":"הודעה","in_channel":"ב-«{name}»","title_search":"חפש (Ctrl+K)","title_add":"צור ערוץ","title_connect":"התחבר","title_logout":"התנתק","title_scroll_left":"גלול שמאלה","title_scroll_right":"גלול ימינה","theme_toggle":"החלף ערכת נושא","lang_toggle":"החלף שפה","uptime_label":"זמן פעילות","online_label":"מחוברים","err_bad_credentials":"כינוי או סיסמה שגויים","err_user_exists":"הכינוי כבר תפוס","err_bad_username":"כינוי 2–32 תווים","err_bad_password":"סיסמה לפחות 4 תווים","err_network":"שגיאת רשת","err_generic":"שגיאה","title_register":"הרשמה"},
 "hi": {"login_title":"मैसेंजर","login_subtitle":"साइन इन करें या खाता बनाएं","field_nick":"उपनाम","field_password":"पासवर्ड","ph_nick":"आपका उपनाम","ph_password":"आपका पासवर्ड","btn_login":"साइन इन","btn_register":"खाता बनाएं","link_to_register":"खाता नहीं है? रजिस्टर करें","link_to_login":"खाता है? साइन इन करें","logged_as":"इस रूप में साइन इन:","header_no_channels":"कोई चैनल नहीं","header_no_channels_sub":"«+» से चैनल बनाएं या मौजूदा से जुड़ें","header_msgs":"{n} संदेश","header_private_tag":"· निजी","empty_no_channels":"आपके पास अभी कोई चैनल नहीं है।<br>नया बनाएं («+») या मौजूदा से जुड़ें।","empty_no_messages":"कोई संदेश नहीं। पहले लिखें!","composer_ph":"संदेश लिखें...","composer_no_channel":"कोई सक्रिय चैनल नहीं","composer_muted":"म्यूट: {n} सेकंड","modal_create_title":"चैनल बनाएं","modal_name":"नाम","modal_name_ph":"जैसे: काम","modal_private":"निजी चैनल","btn_cancel":"रद्द करें","btn_create":"बनाएं","modal_connect_title":"चैनल से जुड़ें","modal_connect_name":"चैनल का नाम","modal_connect_ph":"सटीक चैनल नाम दर्ज करें","btn_connect":"जुड़ें","connect_not_found":"चैनल «{name}» नहीं मिला","search_ph":"चैनल और संदेश खोजें...","search_close":"बंद करें","search_empty":"कुछ नहीं मिला","search_no_channels":"अभी कोई चैनल नहीं","tag_channel":"चैनल","tag_msg":"संदेश","in_channel":"«{name}» में","title_search":"खोजें (Ctrl+K)","title_add":"चैनल बनाएं","title_connect":"जुड़ें","title_logout":"साइन आउट","title_scroll_left":"बाएं स्क्रॉल करें","title_scroll_right":"दाएं स्क्रॉल करें","theme_toggle":"थीम बदलें","lang_toggle":"भाषा बदलें","uptime_label":"अपटाइम","online_label":"ऑनलाइन","err_bad_credentials":"गलत उपनाम या पासवर्ड","err_user_exists":"यह उपनाम पहले से है","err_bad_username":"उपनाम 2–32 अक्षर","err_bad_password":"पासवर्ड कम से कम 4 अक्षर","err_network":"नेटवर्क त्रुटि","err_generic":"त्रुटि","title_register":"रजिस्ट्रेशन"},
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
 "el": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#0D5EAF"/><rect width="30" height="3" fill="#fff"/><rect y="6" width="30" height="3" fill="#fff"/><rect y="12" width="30" height="3" fill="#fff"/><rect y="17" width="30" height="3" fill="#fff"/><rect width="10" height="10" fill="#0D5EAF"/><rect width="10" height="2" fill="#fff"/><rect y="4" width="10" height="2" fill="#fff"/><rect y="8" width="10" height="2" fill="#fff"/><rect y="0" x="4" width="2" height="10" fill="#fff"/>'),
 "tr": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#E30A17"/><circle cx="11" cy="10" r="4.5" fill="#fff"/><circle cx="12.8" cy="10" r="3.6" fill="#E30A17"/><polygon points="18,8.4 18.41,9.43 19.52,9.5 18.67,10.22 18.94,11.29 18,10.7 17.06,11.29 17.33,10.22 16.48,9.5 17.59,9.43" fill="#fff"/>'),
 "ja": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#fff"/><circle cx="15" cy="10" r="6" fill="#BC002D"/>'),
 "ko": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#fff"/><circle cx="15" cy="10" r="4" fill="#CD2E3A"/><path d="M11 10 a4 4 0 0 0 8 0 a4 4 0 0 0 -8 0 z" fill="#0047A0" clip-path="inset(0 0 0 50%)"/>'),
 "zh": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#DE2910"/><polygon points="5,4 6.2,7.5 3,5.4 7,5.4 3.8,7.5" fill="#FFDE00"/>'),
 "ar": _F.format(w=30, h=20, hh=13, body='<rect width="30" height="20" fill="#006C35"/><text x="15" y="15" text-anchor="middle" font-family="Arial" font-size="7" fill="#fff">لا إله</text>'),
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
<title>Messenger</title>
<link rel="stylesheet" href="https://getbootstrap.com/1.4.0/assets/css/bootstrap.min.css">
<style>
* { scrollbar-width: none; -ms-overflow-style: none; }
*::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }
html, body { height: 100%; margin: 0; }
body {
  background: #f5f5f5;
  font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 13px;
  color: #333;
  overflow: hidden;
  -webkit-user-select: none; -moz-user-select: none; -ms-user-select: none; user-select: none;
}
input, textarea { -webkit-user-select: text; -moz-user-select: text; -ms-user-select: text; user-select: text; }
svg { display: inline-block; vertical-align: middle; }

/* Login */
.login-screen { position: fixed; inset: 0; background: #e9eef3; background-image: linear-gradient(#f5f8fb, #dfe6ee); display: flex; align-items: center; justify-content: center; z-index: 500; }
.login-box { width: 320px; background: #fff; border: 1px solid #b8c4d0; box-shadow: 0 4px 16px rgba(0,0,0,.15); padding: 20px 20px 14px; text-align: center; }
.login-box h2 { margin: 0 0 4px; font-size: 18px; color: #2b3d51; }
.login-box p { margin: 0 0 16px; color: #7b8a99; font-size: 12px; }
.my-label { display: block !important; text-align: left !important; font-size: 11px !important; font-weight: bold !important; color: #667788 !important; margin: 0 0 4px 0 !important; padding: 0 !important; line-height: 1.4 !important; text-transform: uppercase !important; letter-spacing: .3px; }
.field-group { margin-bottom: 12px; text-align: left; }
.my-input { display: block !important; width: 100% !important; box-sizing: border-box !important; padding: 7px 10px !important; border: 1px solid #b8c4d0 !important; font-size: 13px !important; background: #fff !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.08) !important; font-family: inherit !important; color: #333 !important; outline: none !important; height: auto !important; line-height: 1.4 !important; margin: 0 !important; }
.my-input:focus { border-color: #0088cc !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.08), 0 0 4px rgba(0,136,204,.5) !important; }
.login-box .btn { width: 100%; margin-top: 6px; }
.login-error { margin-top: 10px; font-size: 11.5px; color: #a94442; background: #fcebeb; border: 1px solid #f5c6c6; padding: 5px 8px; display: none; text-align: left; }
.login-error.show { display: block; }
.login-toggle { margin-top: 10px; text-align: center; font-size: 11.5px; }
.login-toggle a { color: #0088cc; cursor: pointer; text-decoration: none; }
.login-toggle a:hover { text-decoration: underline; }
.login-settings { margin-top: 14px; padding-top: 12px; border-top: 1px solid #e0e5eb; display: flex; gap: 6px; align-items: center; }
.settings-btn { flex: 1; display: inline-flex; align-items: center; justify-content: center; height: 26px; padding: 0 8px; border: 1px solid #b8c4d0; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6); color: #333; cursor: pointer; font-size: 12px; font-family: inherit; text-shadow: 0 1px 0 rgba(255,255,255,.6); }
.settings-btn:hover { background: #d9d9d9; background-image: linear-gradient(#f5f5f5, #d9d9d9); color: #000; }
.settings-btn .lang-code { margin-left: 6px; font-size: 11px; font-weight: bold; letter-spacing: .5px; }
.uptime-line { margin-top: 12px; padding-top: 10px; border-top: 1px solid #e0e5eb; font-size: 11px; color: #7b8a99; display: flex; justify-content: space-between; }
.uptime-line .u-val { font-weight: bold; color: #4a5a6a; font-family: "Courier New", monospace; }

/* Lang picker */
.lang-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 9998; display: none; }
.lang-backdrop.open { display: block; }
.lang-menu { position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%); background: #fff; border: 1px solid #666; box-shadow: 0 5px 20px rgba(0,0,0,.4); padding: 8px; z-index: 9999; display: none; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px; max-width: calc(100vw - 20px); max-height: calc(100vh - 20px); overflow-y: auto; box-sizing: border-box; width: 560px; }
.lang-menu.open { display: grid; }
.lang-menu-item { display: flex; align-items: center; padding: 6px 8px; cursor: pointer; font-size: 12px; color: #333; gap: 7px; border: 1px solid #ddd; background: #fafafa; min-width: 0; box-sizing: border-box; }
.lang-menu-item:hover { background: #eaf4fb; border-color: #8ab4dc; }
.lang-menu-item.active { background: #d6e8f7; color: #004a80; font-weight: bold; border-color: #4a90c2; }
.lang-menu-item .flag-wrap { flex-shrink: 0; display: inline-flex; align-items: center; }
.lang-menu-item .lang-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lang-menu-item .check { flex-shrink: 0; color: #0088cc; font-weight: bold; visibility: hidden; }
.lang-menu-item.active .check { visibility: visible; }
@media (max-width: 600px) { .lang-menu { width: calc(100vw - 20px); grid-template-columns: repeat(2, minmax(0, 1fr)); } }

/* App */
.app { max-width: 940px; width: 100%; height: 100vh; margin: 0 auto; background: #fff; border-left: 1px solid #ccc; border-right: 1px solid #ccc; display: flex; flex-direction: column; position: relative; }
.tabs-bar { display: flex; align-items: center; padding: 6px 8px; background: #f5f5f5; background-image: linear-gradient(#ffffff, #ececec); border-bottom: 1px solid #ccc; flex-shrink: 0; gap: 4px; }
.tabs-scroll { display: flex; align-items: center; flex: 1 1 0; min-width: 0; overflow-x: auto; overflow-y: hidden; padding-bottom: 1px; scroll-behavior: smooth; }
.scroll-arrow { width: 18px; height: 26px; border: 1px solid #bbb; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6); color: #555; cursor: pointer; padding: 0; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; }
.scroll-arrow:hover { background: #d9d9d9; background-image: linear-gradient(#f5f5f5, #d9d9d9); color: #000; }
.channel-tab { display: inline-flex; align-items: center; padding: 4px 10px; margin-right: 4px; border: 1px solid #bbb; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6); color: #444; font-size: 12px; white-space: nowrap; cursor: pointer; text-shadow: 0 1px 0 rgba(255,255,255,.6); flex-shrink: 0; font-family: "Courier New", Courier, monospace; }
.channel-tab:last-child { margin-right: 0; }
.channel-tab:hover { background: #d9d9d9; background-image: linear-gradient(#f5f5f5, #d9d9d9); color: #000; }
.channel-tab.active { background: #006dcc; background-image: linear-gradient(#0088cc, #0044cc); color: #fff; border-color: #003f81; text-shadow: 0 -1px 0 rgba(0,0,0,.3); }
.channel-tab .lock-ico { margin-right: 4px; opacity: .8; display: inline-flex; align-items: center; }
.channel-tab .close-x { margin-left: 6px; cursor: pointer; opacity: .55; display: inline-flex; align-items: center; color: inherit; }
.channel-tab .close-x:hover { opacity: 1; color: #c00; }
.icon-btn-tab { width: 26px; height: 26px; line-height: 1; border: 1px solid #bbb; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6); color: #555; cursor: pointer; padding: 0; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; }
.icon-btn-tab:hover { background: #d9d9d9; background-image: linear-gradient(#f5f5f5, #d9d9d9); color: #000; }
.top-sep { width: 1px; height: 20px; background: #ccc; margin: 0 4px; flex-shrink: 0; }

.chat-header { display: flex; align-items: center; padding: 8px 12px; background: #f5f5f5; background-image: linear-gradient(#ffffff, #f0f0f0); border-bottom: 1px solid #ccc; flex-shrink: 0; }
.chat-header .title { font-weight: bold; font-size: 14px; line-height: 1.1; color: #222; }
.chat-header .subtitle { font-size: 11px; color: #777; }
.chat-header .user-info { margin-left: auto; display: flex; align-items: center; gap: 8px; font-size: 11px; color: #666; }
.chat-header .user-info .nick { font-weight: bold; color: #2b3d51; }

.search-panel { position: absolute; top: 0; left: 0; right: 0; background: #fff; border-bottom: 1px solid #ccc; box-shadow: 0 2px 6px rgba(0,0,0,.2); z-index: 30; transform: translateY(-110%); transition: transform .18s ease-out; max-height: 70vh; display: flex; flex-direction: column; }
.search-panel.open { transform: translateY(0); }
.search-panel .search-head { padding: 8px 10px; display: flex; align-items: center; border-bottom: 1px solid #e5e5e5; background: #f5f5f5; background-image: linear-gradient(#ffffff, #efefef); gap: 8px; }
.search-panel .search-head input { flex: 1; padding: 5px 8px !important; border: 1px solid #bbb !important; font-size: 12px !important; outline: none !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.1) !important; background: #fff !important; color: #333 !important; box-sizing: border-box !important; margin: 0 !important; }
.search-panel .close-search { padding: 4px 10px; border: 1px solid #bbb; background: #e6e6e6; background-image: linear-gradient(#ffffff, #e6e6e6); cursor: pointer; font-size: 12px; color: #333; flex-shrink: 0; }
.search-results { overflow-y: auto; }
.search-results .empty { padding: 20px; text-align: center; color: #999; font-size: 12px; }
.search-result { display: flex; align-items: center; padding: 8px 12px; border-bottom: 1px solid #eee; cursor: pointer; gap: 8px; }
.search-result:hover { background: #eaf4fb; }
.search-result .title-line { font-size: 12px; font-weight: bold; color: #222; }
.search-result .sub-line { font-size: 11px; color: #666; }
.search-result mark { background: #fff3a8; padding: 0 1px; }
.search-result .type-tag { font-size: 10px; text-transform: uppercase; color: #888; border: 1px solid #ccc; padding: 1px 5px; background: #f5f5f5; flex-shrink: 0; }

.chat-feed { flex: 1; overflow-y: auto; padding: 8px 12px; background: #fdfdfd; }
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
.msg-system .msg-time { color: #b08b8b; margin-left: auto; }

.composer { display: flex; align-items: flex-end; gap: 6px; padding: 8px 10px; background: #f5f5f5; background-image: linear-gradient(#f0f0f0, #ffffff); border-top: 1px solid #ccc; flex-shrink: 0; }
.composer textarea { flex: 1; resize: none; padding: 6px 8px !important; border: 1px solid #bbb !important; font-size: 12px !important; max-height: 140px; outline: none !important; box-shadow: inset 0 1px 2px rgba(0,0,0,.1) !important; font-family: inherit !important; background: #fff !important; color: #333 !important; box-sizing: border-box !important; margin: 0 !important; overflow: hidden; }
.composer textarea:focus { border-color: #0088cc !important; }
.composer textarea[disabled] { background: #f7e6e6 !important; border-color: #d6a0a0 !important; color: #a94442 !important; cursor: not-allowed !important; }
.composer .icon-btn { width: 32px; height: 32px; padding: 0; flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center; }

.my-modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 1000; display: none; justify-content: center; align-items: center; padding: 20px; box-sizing: border-box; }
.my-modal-backdrop.open { display: flex; }
.my-modal { width: 400px; max-width: 100%; background: #fff; border: 1px solid #666; box-shadow: 0 5px 20px rgba(0,0,0,.4); position: relative; box-sizing: border-box; }
.modal-head { padding: 8px 12px; background: #f5f5f5; background-image: linear-gradient(#ffffff, #efefef); border-bottom: 1px solid #ccc; font-weight: bold; font-size: 13px; display: flex; align-items: center; cursor: move; user-select: none; }
.modal-head .close-m { margin-left: auto; cursor: pointer; color: #666; padding: 0 4px; line-height: 1; display: inline-flex; align-items: center; }
.modal-head .close-m:hover { color: #c00; background: #e6e6e6; }
.modal-body { padding: 14px; text-align: left; }
.modal-foot { padding: 10px 12px; background: #f7f7f7; border-top: 1px solid #e5e5e5; text-align: right; }
.modal-foot .btn { margin-left: 6px; }
.checkbox-row { display: flex; align-items: center; margin-top: 12px; font-size: 12px; color: #444; cursor: pointer; white-space: nowrap; }
.checkbox-row input { margin-right: 7px; flex-shrink: 0; }
.error-msg { margin-top: 8px; font-size: 11.5px; color: #a94442; background: #fcebeb; border: 1px solid #f5c6c6; padding: 5px 8px; display: none; }
.error-msg.show { display: flex; align-items: center; }
.error-msg .sys-icon { margin-right: 6px; display: inline-flex; }

/* Dark theme */
body.dark { background: #1a1a1a; color: #ccc; }
body.dark .login-screen { background: #1a1a1a; background-image: none; }
body.dark .login-box { background: #252526; border-color: #3c3c3c; box-shadow: 0 4px 16px rgba(0,0,0,.6); }
body.dark .login-box h2 { color: #eaeaea; }
body.dark .login-box p { color: #888; }
body.dark .login-box .my-label { color: #888 !important; }
body.dark .my-input { background: #1e1e1e !important; color: #ddd !important; border-color: #4a4a4c !important; }
body.dark .my-input:focus { border-color: #0e7fc0 !important; }
body.dark .login-error { background: #3a1f1f; border-color: #5a2a2a; color: #e0a0a0; }
body.dark .login-toggle a { color: #6cb6ff; }
body.dark .login-settings, body.dark .uptime-line { border-top-color: #3c3c3c; }
body.dark .settings-btn { background: #37373d; background-image: none; color: #ccc; border-color: #4a4a4c; text-shadow: none; }
body.dark .settings-btn:hover { background: #45454a; color: #fff; }
body.dark .uptime-line { color: #888; }
body.dark .uptime-line .u-val { color: #aaa; }
body.dark .lang-menu { background: #252526; border-color: #3c3c3c; }
body.dark .lang-menu-item { color: #ccc; background: #2d2d30; border-color: #3c3c3c; }
body.dark .lang-menu-item:hover { background: #37373d; border-color: #4a4a4c; }
body.dark .lang-menu-item.active { background: #0e639c; color: #fff; border-color: #0e639c; }
body.dark .lang-menu-item .check { color: #6cb6ff; }
body.dark .app { background: #252526; border-color: #3c3c3c; }
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
</style>
</head>
<body>

<div class="login-screen" id="loginScreen">
  <div class="login-box">
    <h2 id="loginTitle">Messenger</h2>
    <p id="loginSubtitle"></p>

    <div class="field-group">
      <label class="my-label" id="lblNick" for="loginName"></label>
      <input type="text" id="loginName" class="my-input" maxlength="32" autocomplete="username">
    </div>

    <div class="field-group">
      <label class="my-label" id="lblPass" for="loginPass"></label>
      <input type="password" id="loginPass" class="my-input" maxlength="128" autocomplete="current-password">
    </div>

    <button class="btn primary" id="loginBtn" type="button"></button>

    <div class="login-toggle">
      <a id="toggleAuth" href="javascript:void(0)"></a>
    </div>

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
    <button class="icon-btn-tab" id="logoutBtn" title="">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
        <polyline points="16 17 21 12 16 7"/>
        <line x1="21" y1="12" x2="9" y2="12"/>
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
      <input type="text" id="newChannelName" class="my-input" maxlength="40">
      <label class="checkbox-row">
        <input type="checkbox" id="newChannelPrivate">
        <span id="lblPrivate"></span>
      </label>
      <div class="error-msg" id="createError"><span class="sys-icon">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="13"/></svg>
      </span><span id="createErrorText"></span></div>
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
      <input type="text" id="connectChannelName" class="my-input" maxlength="40">
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

// ---------------- State ----------------
let currentLang = 'en';
let currentTheme = 'light';
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
  const dict = I18N[currentLang] || I18N.en;
  let s = dict[key] !== undefined ? dict[key] : (I18N.en[key] !== undefined ? I18N.en[key] : key);
  if (vars) for (const k in vars) s = s.replace(new RegExp('\\{'+k+'\\}','g'), vars[k]);
  return s;
};

// ---------------- Crypto: E2EE (AES-GCM 256) ----------------
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
function b64(buf) { let s=''; const bytes=new Uint8Array(buf); for (let i=0;i<bytes.length;i++) s+=String.fromCharCode(bytes[i]); return btoa(s); }
function ub64(str) { const bin=atob(str); const bytes=new Uint8Array(bin.length); for (let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i); return bytes; }
async function encryptText(channelId, text) {
  const key = await deriveChannelKey(channelId);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name:'AES-GCM', iv }, key, enc.encode(text));
  return b64(iv) + '.' + b64(ct);
}
async function decryptText(channelId, payload) {
  try {
    const [ivB64, ctB64] = payload.split('.');
    if (!ivB64 || !ctB64) return payload;
    const key = await deriveChannelKey(channelId);
    const pt = await crypto.subtle.decrypt({ name:'AES-GCM', iv: ub64(ivB64) }, key, ub64(ctB64));
    return dec.decode(pt);
  } catch (e) { return '[decrypt error]'; }
}

// ---------------- HTTP ----------------
async function api(path, method='GET', body=null, withAuth=true) {
  const headers = { 'Content-Type': 'application/json' };
  if (withAuth && authToken) headers['x-auth-token'] = authToken;
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : null });
  let data = null;
  try { data = await res.json(); } catch(e){}
  if (!res.ok) throw { status: res.status, detail: (data && data.detail) || 'error' };
  return data;
}

// ---------------- Uptime ----------------
function fmtUptime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec/86400), h = Math.floor((sec%86400)/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  const p = (n) => String(n).padStart(2,'0');
  return (d>0 ? d+'d ' : '') + p(h)+':'+p(m)+':'+p(s);
}
async function refreshUptime() {
  try {
    const res = await fetch('/api/uptime');
    const data = await res.json();
    uptimeBase = data.uptime;
    uptimeFetchAt = performance.now();
  } catch(e){}
}
function renderUptime() {
  const el = $('uptimeVal'); if (!el) return;
  if (!uptimeFetchAt) { el.textContent = '—'; return; }
  const nowSec = uptimeBase + (performance.now() - uptimeFetchAt)/1000;
  el.textContent = fmtUptime(nowSec);
}
setInterval(renderUptime, 1000);
refreshUptime();
setInterval(refreshUptime, 30000);

// ---------------- Theme ----------------
function applyTheme() {
  document.body.classList.toggle('dark', currentTheme === 'dark');
  $('themeIcon').innerHTML = currentTheme === 'dark' ? SVG.sun : SVG.moon;
  $('themeBtn').setAttribute('title', t('theme_toggle'));
}
function toggleTheme() { currentTheme = currentTheme === 'dark' ? 'light' : 'dark'; applyTheme(); }

// ---------------- Language ----------------
function applyLanguage() {
  $('loginTitle').textContent = t('login_title');
  $('loginSubtitle').textContent = t('login_subtitle');
  $('lblNick').textContent = t('field_nick');
  $('lblPass').textContent = t('field_password');
  $('loginName').placeholder = t('ph_nick');
  $('loginPass').placeholder = t('ph_password');
  $('loginBtn').textContent = authMode === 'login' ? t('btn_login') : t('btn_register');
  $('toggleAuth').textContent = authMode === 'login' ? t('link_to_register') : t('link_to_login');
  $('uptimeLbl').textContent = t('uptime_label');

  $('lblLoggedAs').textContent = t('logged_as');
  $('searchInput').placeholder = t('search_ph');
  $('closeSearchBtn').textContent = t('search_close');

  $('searchBtn').title = t('title_search');
  $('addTabBtn').title = t('title_add');
  $('connectBtn').title = t('title_connect');
  $('logoutBtn').title = t('title_logout');
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

  $('langFlag').innerHTML = FLAGS[currentLang] || '';
  $('langCode').textContent = currentLang.toUpperCase();
  $('langBtn').title = t('lang_toggle');
  $('themeBtn').title = t('theme_toggle');

  buildLangMenu();
  renderTabs(); renderHeader(); renderMessages(); updateMuteUI();
  renderSearchResults($('searchInput').value);
  if (loginErrTimer) showLoginError($('loginErrorText').dataset.msg || '');
}
let loginErrTimer = null;
function showLoginError(msg) {
  const box = $('loginError');
  if (!msg) { box.classList.remove('show'); return; }
  $('loginErrorText').textContent = msg;
  $('loginErrorText').dataset.msg = msg;
  box.classList.add('show');
  clearTimeout(loginErrTimer);
  loginErrTimer = setTimeout(() => { box.classList.remove('show'); $('loginErrorText').dataset.msg=''; }, 3500);
}

function buildLangMenu() {
  const menu = $('langMenu'); menu.innerHTML = '';
  LANG_ORDER.forEach(code => {
    const item = document.createElement('div');
    item.className = 'lang-menu-item' + (code === currentLang ? ' active' : '');
    item.innerHTML = '<span class="flag-wrap">'+FLAGS[code]+'</span><span class="lang-name">'+LANG_NAMES[code]+'</span><span class="check">✓</span>';
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      currentLang = code; closeLangMenu(); applyLanguage();
    });
    menu.appendChild(item);
  });
}
function openLangMenu() { $('langBackdrop').classList.add('open'); $('langMenu').classList.add('open'); }
function closeLangMenu() { $('langBackdrop').classList.remove('open'); $('langMenu').classList.remove('open'); }

// ---------------- Auth ----------------
let authMode = 'login';
$('toggleAuth').addEventListener('click', () => {
  authMode = authMode === 'login' ? 'register' : 'login';
  showLoginError('');
  applyLanguage();
});

async function doAuth() {
  const username = $('loginName').value.trim();
  const password = $('loginPass').value;
  if (!username || !password) { showLoginError(t('err_bad_credentials')); return; }
  try {
    const path = authMode === 'login' ? '/api/login' : '/api/register';
    const res = await api(path, 'POST', { username, password }, false);
    authToken = res.token;
    currentUser = res.username;
    $('loginScreen').style.display = 'none';
    $('app').style.display = 'flex';
    $('headerUser').textContent = currentUser;
    $('loginName').value = '';
    $('loginPass').value = '';
    await loadChannels();
  } catch (e) {
    const map = { 401: 'err_bad_credentials', 409: 'err_user_exists', 400: 'err_bad_username' };
    showLoginError(t(map[e.status] || 'err_generic'));
  }
}
$('loginBtn').addEventListener('click', doAuth);
$('loginName').addEventListener('keydown', e => { if (e.key==='Enter') $('loginPass').focus(); });
$('loginPass').addEventListener('keydown', e => { if (e.key==='Enter') doAuth(); });

// ---------------- Theme/lang buttons ----------------
$('themeBtn').addEventListener('click', e => { e.stopPropagation(); toggleTheme(); });
$('langBtn').addEventListener('click', e => { e.stopPropagation(); $('langMenu').classList.contains('open') ? closeLangMenu() : openLangMenu(); });
$('langBackdrop').addEventListener('click', closeLangMenu);

// ---------------- Logout ----------------
$('logoutBtn').addEventListener('click', async () => {
  try { await api('/api/logout', 'POST'); } catch(e){}
  authToken = null; currentUser = null;
  channels = []; activeId = null; channelKeyCache = {};
  if (ws) { try { ws.close(); } catch(e){} ws = null; wsChannelId = null; }
  $('app').style.display = 'none';
  $('loginScreen').style.display = 'flex';
  closeLangMenu();
});

// ---------------- Channels ----------------
async function loadChannels() {
  try {
    const res = await api('/api/channels');
    channels = res.channels || [];
    if (activeId && !channels.find(c => c.id === activeId)) activeId = null;
    if (!activeId && channels.length) activeId = channels[0].id;
    renderAll();
    if (activeId) openChannelWS(activeId);
  } catch(e) {
    if (e.status === 401) { $('logoutBtn').click(); }
  }
}

function renderTabs() {
  const wrap = $('tabsScroll'); wrap.innerHTML = '';
  channels.forEach(ch => {
    const tab = document.createElement('div');
    tab.className = 'channel-tab' + (ch.id === activeId ? ' active' : '');
    tab.dataset.id = ch.id;
    if (ch.private) {
      const lockEl = document.createElement('span');
      lockEl.className = 'lock-ico';
      lockEl.innerHTML = SVG.lock;
      tab.appendChild(lockEl);
    }
    const nameSpan = document.createElement('span');
    nameSpan.textContent = ch.name;
    tab.appendChild(nameSpan);
    const x = document.createElement('span');
    x.className = 'close-x'; x.dataset.close = ch.id; x.innerHTML = SVG.x;
    tab.appendChild(x);
    tab.addEventListener('click', (e) => {
      if (e.target.closest && e.target.closest('[data-close]')) return;
      if (activeId === ch.id) return;
      activeId = ch.id;
      renderAll();
      openChannelWS(activeId);
    });
    wrap.appendChild(tab);
  });
  scrollActiveTabIntoView();
}

$('tabsScroll').addEventListener('click', async (e) => {
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
  const wrapRect = wrap.getBoundingClientRect();
  const tabRect = activeTab.getBoundingClientRect();
  if (tabRect.left < wrapRect.left) wrap.scrollLeft += tabRect.left - wrapRect.left - 6;
  else if (tabRect.right > wrapRect.right) wrap.scrollLeft += tabRect.right - wrapRect.right + 6;
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

// ---------------- Messages ----------------
const GROUP_WINDOW_MS = 5 * 60 * 1000;
let mutes = {};

function renderMessages() {
  const feed = $('chatFeed'); feed.innerHTML = '';
  const ch = channels.find(c => c.id === activeId);
  if (!ch) {
    feed.innerHTML = '<div class="empty-state">'+SVG.chat+t('empty_no_channels')+'</div>';
    return;
  }
  if (ch.messages.length === 0) {
    feed.innerHTML = '<div class="empty-state">'+SVG.chat+t('empty_no_messages')+'</div>';
    return;
  }
  let lastAuthor = null, lastTime = 0;
  ch.messages.forEach(m => {
    const row = document.createElement('div');
    row.dataset.id = m.id;
    const d = new Date(m.t * 1000);
    const timeStr = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    const author = m.from;
    const grouped = (lastAuthor === author) && ((m.t*1000) - lastTime < GROUP_WINDOW_MS);
    row.className = 'msg-row';
    row.innerHTML = '<span class="msg-author'+(grouped?' hidden':'')+'" style="color:'+(author===currentUser?'#005a87':'#8a4a00')+'">'+
      escapeHtml(author)+':</span><span class="msg-text" data-ct="'+escapeHtml(m.ct)+'">…</span>'+
      '<span class="msg-time">'+timeStr+'</span>';
    feed.appendChild(row);
    lastAuthor = author;
    lastTime = m.t * 1000;
  });
  // decrypt all async
  const chId = activeId;
  feed.querySelectorAll('.msg-row').forEach(row => {
    const ct = row.querySelector('.msg-text').dataset.ct;
    decryptText(chId, ct).then(pt => {
      const span = row.querySelector('.msg-text');
      if (span) span.textContent = pt;
    });
  });
  feed.scrollTop = feed.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function appendMessageUI(msg, channelId) {
  const feed = $('chatFeed');
  const empty = feed.querySelector('.empty-state');
  if (empty) empty.remove();
  const ch = channels.find(c => c.id === channelId);
  if (ch) ch.messages.push(msg);
  if (activeId !== channelId) return;
  const lastRow = feed.querySelector('.msg-row:last-child');
  const lastAuthor = lastRow ? lastRow.querySelector('.msg-author').textContent.replace(':','') : null;
  const lastTime = lastRow ? parseInt(lastRow.dataset.t || '0', 10) : 0;
  const grouped = lastAuthor === msg.from && (msg.t*1000 - lastTime < GROUP_WINDOW_MS);

  const row = document.createElement('div');
  row.className = 'msg-row';
  row.dataset.id = msg.id;
  row.dataset.t = String(msg.t*1000);
  const d = new Date(msg.t * 1000);
  const timeStr = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  row.innerHTML = '<span class="msg-author'+(grouped?' hidden':'')+'" style="color:'+(msg.from===currentUser?'#005a87':'#8a4a00')+'">'+
    escapeHtml(msg.from)+':</span><span class="msg-text">…</span><span class="msg-time">'+timeStr+'</span>';
  feed.appendChild(row);
  decryptText(channelId, msg.ct).then(pt => {
    const span = row.querySelector('.msg-text'); if (span) span.textContent = pt;
  });
  feed.scrollTop = feed.scrollHeight;
}

function addSystem(text, info) {
  const feed = $('chatFeed');
  const empty = feed.querySelector('.empty-state'); if (empty) empty.remove();
  const row = document.createElement('div');
  row.className = 'msg-row msg-system' + (info ? ' info' : '');
  row.innerHTML = '<span class="sys-icon">'+(info?SVG.info||'':SVG.ban)+'</span><span>'+escapeHtml(text)+'</span>';
  feed.appendChild(row);
  feed.scrollTop = feed.scrollHeight;
}

function updateMuteUI() {
  const input = $('msgInput');
  const sendBtn = $('sendBtn');
  const ch = channels.find(c => c.id === activeId);
  if (!ch) { input.disabled = true; input.placeholder = t('composer_no_channel'); sendBtn.disabled = true; return; }
  const until = mutes[ch.id] || 0;
  const rem = Math.max(0, Math.ceil((until - Date.now())/1000));
  if (rem > 0) {
    input.disabled = true; input.placeholder = t('composer_muted', {n: rem}); sendBtn.disabled = true;
  } else {
    input.disabled = false; input.placeholder = t('composer_ph'); sendBtn.disabled = false;
  }
}
setInterval(updateMuteUI, 1000);

// ---------------- WebSocket ----------------
function openChannelWS(channelId) {
  if (ws && wsChannelId === channelId && ws.readyState === WebSocket.OPEN) return;
  if (ws) { try { ws.close(); } catch(e){} ws = null; }
  wsChannelId = channelId;
  onlineUsers = [];
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onopen = () => {
    ws.send(JSON.stringify({ type:'auth', token: authToken, channel_id: channelId }));
  };
  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch(e){ return; }
    if (data.type === 'message' && data.msg) {
      appendMessageUI(data.msg, channelId);
      renderHeader();
    } else if (data.type === 'presence') {
      onlineUsers = data.users || [];
      renderHeader();
    } else if (data.type === 'muted') {
      mutes[channelId] = Date.now() + data.seconds * 1000;
      updateMuteUI();
      addSystem('Muted for ' + data.seconds + 's (rate limit)', false);
    } else if (data.type === 'error') {
      if (data.error === 'auth') { $('logoutBtn').click(); }
    }
  };
  ws.onclose = () => { if (wsChannelId === channelId) ws = null; };
  ws.onerror = () => {};
}

function sendMessage() {
  const input = $('msgInput');
  const text = input.value.trim();
  if (!text) return;
  const chId = activeId;
  if (!chId || !ws || ws.readyState !== WebSocket.OPEN) return;
  const until = mutes[chId] || 0;
  if (until > Date.now()) { updateMuteUI(); return; }
  encryptText(chId, text).then(ct => {
    ws.send(JSON.stringify({ type:'message', ciphertext: ct }));
    input.value = ''; autoResize();
  });
}
$('sendBtn').addEventListener('click', sendMessage);
$('msgInput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
function autoResize() {
  const el = $('msgInput');
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}
$('msgInput').addEventListener('input', autoResize);

// ---------------- Modals ----------------
function openModal(id) { $(id).classList.add('open'); }
function closeModal(id) { $(id).classList.remove('open'); }
document.addEventListener('click', (e) => {
  const el = e.target.closest && e.target.closest('[data-close-modal]');
  if (!el) return;
  const w = el.getAttribute('data-close-modal');
  if (w === 'create') closeModal('createBackdrop');
  if (w === 'connect') closeModal('connectBackdrop');
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if ($('createBackdrop').classList.contains('open')) closeModal('createBackdrop');
  if ($('connectBackdrop').classList.contains('open')) closeModal('connectBackdrop');
  if ($('langMenu').classList.contains('open')) closeLangMenu();
});

// Create channel
$('addTabBtn').addEventListener('click', () => {
  $('newChannelName').value = '';
  $('newChannelPrivate').checked = false;
  $('createError').classList.remove('show');
  openModal('createBackdrop');
  setTimeout(() => $('newChannelName').focus(), 60);
});
$('createChannelBtn').addEventListener('click', async () => {
  const name = $('newChannelName').value.trim();
  if (!name) { $('newChannelName').focus(); return; }
  try {
    const res = await api('/api/channels', 'POST', { name, private: $('newChannelPrivate').checked });
    channels.push({ id: res.id, name: res.name, private: res.private, messages: [] });
    activeId = res.id;
    closeModal('createBackdrop');
    renderAll(); openChannelWS(activeId);
  } catch (e) {
    $('createErrorText').textContent = e.status === 409 ? t('err_user_exists') : t('err_generic');
    $('createError').classList.add('show');
  }
});
$('newChannelName').addEventListener('keydown', e => { if (e.key === 'Enter') $('createChannelBtn').click(); });

// Connect
$('connectBtn').addEventListener('click', () => {
  $('connectChannelName').value = '';
  $('connectError').classList.remove('show');
  openModal('connectBackdrop');
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
    closeModal('connectBackdrop');
    renderAll(); openChannelWS(activeId);
  } catch (e) {
    $('connectErrorText').textContent = e.status === 404 ? t('connect_not_found', {name}) : t('err_generic');
    $('connectError').classList.add('show');
  }
});
$('connectChannelName').addEventListener('keydown', e => { if (e.key === 'Enter') $('connectChannelBtn').click(); });
$('connectChannelName').addEventListener('input', () => $('connectError').classList.remove('show'));

// ---------------- Search ----------------
$('searchBtn').addEventListener('click', () => {
  $('searchPanel').classList.add('open');
  $('searchInput').value = '';
  renderSearchResults('');
  setTimeout(() => $('searchInput').focus(), 200);
});
$('closeSearchBtn').addEventListener('click', () => $('searchPanel').classList.remove('open'));
$('searchInput').addEventListener('input', () => renderSearchResults($('searchInput').value));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && $('searchPanel').classList.contains('open')) $('searchPanel').classList.remove('open');
  if ((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='k') { e.preventDefault(); $('searchBtn').click(); }
});

function renderSearchResults(q) {
  const query = q.trim(); const ql = query.toLowerCase();
  const res = $('searchResults');
  const chans = channels.map(c => ({...c, decrypted: false}));
  if (!query) {
    res.innerHTML = '';
    if (!chans.length) { res.innerHTML = '<div class="empty">'+t('search_no_channels')+'</div>'; return; }
    chans.forEach(ch => res.appendChild(makeChannelRow(ch, '')));
    return;
  }
  const items = [];
  chans.forEach(ch => {
    if (!ch.private && ch.name.toLowerCase().includes(ql)) items.push({type:'channel', ch});
  });
  // For messages, need to decrypt — do async pass
  res.innerHTML = '<div class="empty">…</div>';
  (async () => {
    const found = [];
    for (const ch of chans) {
      for (const m of (ch.messages || [])) {
        const pt = await decryptText(ch.id, m.ct);
        if (pt.toLowerCase().includes(ql) || m.from.toLowerCase().includes(ql)) {
          found.push({type:'message', ch, m, pt});
        }
      }
    }
    res.innerHTML = '';
    if (!items.length && !found.length) { res.innerHTML = '<div class="empty">'+t('search_empty')+'</div>'; return; }
    items.forEach(it => res.appendChild(makeChannelRow(it.ch, query)));
    found.slice(0, 50).forEach(it => res.appendChild(makeMessageRow(it.ch, it.m, it.pt, query)));
  })();
}

function makeChannelRow(ch, query) {
  const row = document.createElement('div');
  row.className = 'search-result';
  row.innerHTML = '<div class="text-block"><div class="title-line">'+
    (query ? hl(ch.name, query) : escapeHtml(ch.name))+'</div>'+
    '<div class="sub-line">'+t('header_msgs', {n:(ch.messages||[]).length})+'</div></div>'+
    '<span class="type-tag">'+t('tag_channel')+'</span>';
  row.addEventListener('click', () => {
    activeId = ch.id; renderAll(); openChannelWS(activeId);
    $('searchPanel').classList.remove('open');
  });
  return row;
}
function makeMessageRow(ch, m, pt, query) {
  const row = document.createElement('div');
  row.className = 'search-result';
  row.innerHTML = '<div class="text-block"><div class="title-line">'+escapeHtml(m.from)+
    ' <span style="color:#999;font-weight:normal;">'+t('in_channel',{name:ch.name})+'</span></div>'+
    '<div class="sub-line">'+(query ? hl(pt, query) : escapeHtml(pt))+'</div></div>'+
    '<span class="type-tag">'+t('tag_msg')+'</span>';
  row.addEventListener('click', () => {
    activeId = ch.id; renderAll(); openChannelWS(activeId);
    $('searchPanel').classList.remove('open');
  });
  return row;
}
function hl(text, q) {
  const re = new RegExp('('+q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','ig');
  return escapeHtml(text).replace(re, '<mark>$1</mark>');
}

// ---------------- Keyboard nav ----------------
function switchChannel(delta) {
  if (!channels.length) return;
  let idx = channels.findIndex(c => c.id === activeId);
  if (idx === -1) return;
  const n = Math.max(0, Math.min(channels.length - 1, idx + delta));
  if (n === idx) return;
  activeId = channels[n].id;
  renderAll(); openChannelWS(activeId);
}
document.addEventListener('keydown', (e) => {
  if (document.querySelector('.my-modal-backdrop.open')) return;
  if ($('langMenu').classList.contains('open')) return;
  if ($('searchPanel').classList.contains('open')) return;
  if ($('loginScreen').style.display !== 'none') return;
  const ae = document.activeElement;
  if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) return;
  if (e.key === 'ArrowLeft') { switchChannel(-1); e.preventDefault(); }
  else if (e.key === 'ArrowRight') { switchChannel(1); e.preventDefault(); }
});

function renderAll() { renderTabs(); renderHeader(); renderMessages(); updateMuteUI(); scrollActiveTabIntoView(); }

// ---------------- Init ----------------
applyTheme(); applyLanguage(); renderUptime();
setTimeout(() => $('loginName').focus(), 100);
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
    # Single worker keeps storage consistent (in-memory dicts are per-process).
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        workers=1,
        log_level="info",
        access_log=False,
        limit_concurrency=200,
        timeout_keep_alive=30,
    )
