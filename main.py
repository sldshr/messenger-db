import os
import re
import json
import hmac
import hashlib
import base64
import secrets
import time
import datetime
import asyncio

from typing import Optional, Dict, List


# ---------- ЗАГРУЗКА .env (без внешних зависимостей) ----------
def _load_env_file(path: str = ".env"):
    """Примитивный парсер .env (VARIABLE=value). Возвращает True при успехе."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, val)
        return True
    except FileNotFoundError:
        return False


# Загружаем .env из папки скрипта и из рабочей директории
_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
_load_env_file(".env")

# ---------- FastAPI ----------
try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse
except ImportError:
    raise SystemExit(
        "Установите зависимости: pip install fastapi uvicorn asyncpg python-dotenv"
    )

import asyncpg

# ============================================================
#  КОНФИГУРАЦИЯ
# ============================================================

# Строка подключения к Supabase (PostgreSQL).
# Задайте через переменную окружения DATABASE_URL или файл .env
# Пример для Supabase:
#   postgresql://postgres.XXXX:ПАРОЛЬ@aws-0-XX.pooler.supabase.com:6543/postgres
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://USER:PASSWORD@HOST:PORT/postgres",
)

# Секретный ключ для подписи JWT-токенов. Обязательно смените!
JWT_SECRET = os.environ.get("JWT_SECRET", "SUPER_SECRET_KEY_ПОМЕНЯЙТЕ_МЕНЯ")

TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 дней

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# ============================================================
#  УТИЛИТЫ: ХЕШИРОВАНИЕ ПАРОЛЕЙ И JWT
# ============================================================

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """PBKDF2-HMAC-SHA256, 120000 итераций (безопасно и быстро)."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 120_000
    )
    return f"{b64url(salt)}${b64url(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, dk_b64 = stored.split("$")
        salt = b64url_decode(salt_b64)
        expected = b64url_decode(dk_b64)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, 120_000
        )
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def create_token(user_id: int, username: str, exp: Optional[int] = None) -> str:
    """Создаёт подписанный JWT-подобный токен (HS256, без внешних зависимостей)."""
    header = {"alg": "HS256", "typ": "JWT"}
    if exp is None:
        exp = int(time.time()) + TOKEN_TTL_SECONDS
    payload = {"sub": str(user_id), "name": username, "exp": exp}
    header_b64 = b64url(json.dumps(header).encode("utf-8"))
    payload_b64 = b64url(json.dumps(payload).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(
        JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256
    ).digest()
    return f"{header_b64}.{payload_b64}.{b64url(signature)}"


def decode_token(token: str) -> Optional[dict]:
    """Проверяет подпись и возвращает payload токена (или None)."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected = hmac.new(
            JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, b64url_decode(sig_b64)):
            return None
        payload = json.loads(b64url_decode(payload_b64))
        if payload["exp"] < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ============================================================
#  БАЗА ДАННЫХ (SUPABASE / POSTGRESQL)
# ============================================================

class Database:
    def __init__(self):
        self._pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                dsn=DATABASE_URL,
                min_size=1,
                max_size=5,          # мало соединений => мало памяти
                command_timeout=30,
                # ВАЖНО для Supabase pooler (pgbouncer transaction mode):
                # asyncpg кэширует prepared statements, а pgbouncer их не
                # переживает -> периодические 500 InvalidSQLStatementNameError.
                # statement_cache_size=0 полностью отключает кэш.
                statement_cache_size=0,
            )
        return self._pool

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        """Ошибки, после которых запрос можно безопасно повторить."""
        if isinstance(exc, (TimeoutError, ConnectionRefusedError, ConnectionResetError)):
            return True
        PG = getattr(asyncpg, "exceptions", None)
        if PG is not None:
            return isinstance(exc, (
                PG.InvalidSQLStatementNameError,
                PG.ConnectionDoesNotExistError,
                PG.InterfaceError,
                PG.TooManyConnectionsError,
            ))
        return False

    async def execute(self, query, *args):
        for attempt in range(3):
            pool = await self.connect()
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        return await conn.execute(query, *args)
            except Exception as exc:
                if attempt == 2 or not self._retryable(exc):
                    raise
                await asyncio.sleep(0.15 * (attempt + 1))

    async def fetchrow(self, query, *args):
        for attempt in range(3):
            pool = await self.connect()
            try:
                async with pool.acquire() as conn:
                    return await conn.fetchrow(query, *args)
            except Exception as exc:
                if attempt == 2 or not self._retryable(exc):
                    raise
                await asyncio.sleep(0.15 * (attempt + 1))

    async def fetchval(self, query, *args):
        for attempt in range(3):
            pool = await self.connect()
            try:
                async with pool.acquire() as conn:
                    return await conn.fetchval(query, *args)
            except Exception as exc:
                if attempt == 2 or not self._retryable(exc):
                    raise
                await asyncio.sleep(0.15 * (attempt + 1))

    async def fetch(self, query, *args):
        for attempt in range(3):
            pool = await self.connect()
            try:
                async with pool.acquire() as conn:
                    return await conn.fetch(query, *args)
            except Exception as exc:
                if attempt == 2 or not self._retryable(exc):
                    raise
                await asyncio.sleep(0.15 * (attempt + 1))


db = Database()

# ============================================================
#  МЕНЕДЖЕР WEB-SOCKET СОЕДИНЕНИЙ
# ============================================================

class ConnectionManager:
    """Держит активные WebSocket-соединения пользователей."""

    def __init__(self):
        # user_id -> WebSocket
        self.active: Dict[int, WebSocket] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active[user_id] = websocket

    def disconnect(self, user_id: int):
        self.active.pop(user_id, None)

    async def send_to_user(self, user_id: int, data: dict):
        ws = self.active.get(user_id)
        if ws:
            try:
                await ws.send_text(json.dumps(data, ensure_ascii=False))
            except Exception:
                self.disconnect(user_id)


manager = ConnectionManager()

# ============================================================
#  ПРИЛОЖЕНИЕ FASTAPI
# ============================================================

app = FastAPI(title="Messenger")


@app.on_event("startup")
async def on_startup():
    try:
        await db.connect()
    except Exception as e:
        # Не падаем при старте, если БД недоступна — ошибки появятся при запросах
        print(f"[!] Не удалось подключиться к базе данных: {e}")
        print("[!] Проверьте переменную DATABASE_URL и наличие таблиц (schema.sql)")


@app.on_event("shutdown")
async def on_shutdown():
    try:
        await db.close()
    except Exception:
        pass


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def get_token_from_header(authorization: Optional[str]) -> Optional[str]:
    """Извлекает JWT из значения заголовка Authorization (Bearer ...)."""
    if not authorization:
        return None
    parts = authorization.split()
    # Поддерживаем "Bearer <token>" и просто "<token>"
    if parts[0].lower() == "bearer" and len(parts) == 2:
        return parts[1]
    return authorization.strip()


async def auth_user(request: Request) -> Optional[dict]:
    """Читает Authorization-заголовок напрямую из request и проверяет токен."""
    # Явно извлекаем заголовок: так надёжнее, чем магия имён параметров FastAPI
    authorization = request.headers.get("authorization") or request.headers.get("Authorization")
    if not authorization:
        return None
    token = get_token_from_header(authorization)
    if not token:
        return None
    payload = decode_token(token)
    if payload is None:
        # Диагностика: токен получен, но не прошёл проверку
        print(f"[auth] Токен отклонён. len={len(token)}, нач.={token[:12]}...")
    return payload



# ---------- API: РЕГИСТРАЦИЯ ----------

@app.post("/api/register")
async def register(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Неверный формат данных")

    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not (3 <= len(username) <= 32):
        raise HTTPException(400, "Имя пользователя должно быть от 3 до 32 символов")
    if not re.match(r"^[a-zA-Z0-9_\u0400-\u04FF]+$", username):
        raise HTTPException(400, "Имя пользователя: только буквы, цифры и _")
    if len(password) < 6:
        raise HTTPException(400, "Пароль должен быть не короче 6 символов")

    existing = await db.fetchrow(
        "SELECT 1 FROM users WHERE username = $1", username,
    )
    if existing:
        raise HTTPException(409, "Пользователь с таким именем уже существует")

    user_id = await db.fetchval(
        """INSERT INTO users (username, password_hash)
           VALUES ($1, $2) RETURNING id""",
        username, hash_password(password),
    )

    token = create_token(user_id, username)
    return {"token": token,
            "user": {"id": user_id, "username": username, "avatar": None, "bio": ""}}


# ---------- API: ВХОД ----------

@app.post("/api/login")
async def login(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Неверный формат данных")

    login_input = (data.get("login") or "").strip()
    password = data.get("password") or ""

    row = await db.fetchrow(
        "SELECT id, username, avatar, bio, password_hash FROM users WHERE username = $1",
        login_input,
    )
    if not row or not verify_password(password, row["password_hash"]):
        raise HTTPException(401, "Неверный логин или пароль")

    token = create_token(row["id"], row["username"])
    return {
        "token": token,
        "user": {"id": row["id"], "username": row["username"],
                 "avatar": row["avatar"], "bio": row["bio"] or ""},
    }



# ---------- API: ПОЛУЧИТЬ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ ----------

@app.get("/api/me")
async def me(request: Request):
    payload = await auth_user(request)
    if not payload:
        raise HTTPException(401, "Не авторизован")
    row = await db.fetchrow(
        "SELECT id, username, avatar, bio FROM users WHERE id = $1", int(payload["sub"])
    )
    if not row:
        raise HTTPException(401, "Пользователь не найден")
    return {"user": {"id": row["id"], "username": row["username"],
                     "avatar": row["avatar"], "bio": row["bio"] or ""}}


# ---------- API: ИЗМЕНИТЬ ПРОФИЛЬ (аватар, описание) ----------

@app.patch("/api/profile")
async def update_profile(request: Request):
    payload = await auth_user(request)
    if not payload:
        raise HTTPException(401, "Не авторизован")
    me_id = int(payload["sub"])

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Неверный формат данных")

    avatar = (data.get("avatar") or "").strip()
    bio = (data.get("bio") or "").strip()

    # Проверка аватара: пустой, или data-URL base64-картинки не более ~512 КБ
    if avatar and not avatar.startswith("data:image/"):
        raise HTTPException(400, "Аватар должен быть base64-картинкой (data:image/...)")
    if len(avatar) > 1_100_000:   # ~750 КБ в base64
        raise HTTPException(400, "Аватар слишком большой (макс ~512 КБ)")
    if len(bio) > 300:
        raise HTTPException(400, "Описание должно быть не длиннее 300 символов")

    await db.execute(
        "UPDATE users SET avatar = $1, bio = $2 WHERE id = $3",
        avatar or None, bio, me_id,
    )
    return {"user": {"id": me_id, "username": payload["name"],
                     "avatar": avatar or None, "bio": bio}}


# ---------- API: СПИСОК ПОЛЬЗОВАТЕЛЕЙ ----------

@app.get("/api/users")
async def list_users(request: Request):
    payload = await auth_user(request)
    if not payload:
        raise HTTPException(401, "Не авторизован")

    rows = await db.fetch(
        """SELECT id, username, avatar, bio FROM users
           WHERE id <> $1 ORDER BY username""", int(payload["sub"])
    )
    online = set(manager.active.keys())
    return {
        "users": [
            {"id": r["id"], "username": r["username"], "avatar": r["avatar"],
             "bio": r["bio"] or "", "online": r["id"] in online}
            for r in rows
        ]
    }


# ---------- API: СПИСОК ДИАЛОГОВ (для "переписки") ----------

@app.get("/api/dialogs")
async def list_dialogs(request: Request):
    payload = await auth_user(request)
    if not payload:
        raise HTTPException(401, "Не авторизован")
    me_id = int(payload["sub"])

    rows = await db.fetch(
        """SELECT DISTINCT ON (peer) peer, u.username, u.avatar,
                  (SELECT content FROM messages m2
                   WHERE (m2.sender_id = $1 AND m2.receiver_id = peer)
                      OR (m2.sender_id = peer AND m2.receiver_id = $1)
                   ORDER BY m2.id DESC LIMIT 1) AS last_content,
                  (SELECT created_at FROM messages m3
                   WHERE (m3.sender_id = $1 AND m3.receiver_id = peer)
                      OR (m3.sender_id = peer AND m3.receiver_id = $1)
                   ORDER BY m3.id DESC LIMIT 1) AS last_at
        FROM (SELECT sender_id AS peer FROM messages WHERE receiver_id = $1
              UNION
              SELECT receiver_id AS peer FROM messages WHERE sender_id = $1) AS p
        JOIN users u ON u.id = p.peer
        ORDER BY peer, last_at DESC""",
        me_id,
    )

    online = set(manager.active.keys())
    dialogs = []
    for r in rows:
        dialogs.append({
            "id": r["peer"],
            "username": r["username"],
            "avatar": r["avatar"],
            "last_content": r["last_content"],
            "last_at": r["last_at"].isoformat() if r["last_at"] else None,
            "online": r["peer"] in online,
        })
    # Сортируем по времени последнего сообщения (новые сверху)
    dialogs.sort(key=lambda d: d["last_at"] or "", reverse=True)
    return {"dialogs": dialogs}



# ---------- API: ИСТОРИЯ ПЕРЕПИСКИ ----------

@app.get("/api/messages/{peer_id}")
async def get_messages(peer_id: int, before: int = 0, request: Request = None):
    # request инжектируется FastAPI (объект Request)
    payload = await auth_user(request)
    if not payload:
        raise HTTPException(401, "Не авторизован")
    me_id = int(payload["sub"])
    limit = 100

    if before == 0:
        rows = await db.fetch(
            """SELECT id, sender_id, receiver_id, content, created_at
               FROM messages
               WHERE (sender_id = $1 AND receiver_id = $2)
                  OR (sender_id = $2 AND receiver_id = $1)
               ORDER BY id DESC LIMIT $3""",
            me_id, peer_id, limit,
        )
    else:
        rows = await db.fetch(
            """SELECT id, sender_id, receiver_id, content, created_at
               FROM messages
               WHERE ((sender_id = $1 AND receiver_id = $2)
                  OR (sender_id = $2 AND receiver_id = $1))
                 AND id < $3
               ORDER BY id DESC LIMIT $4""",
            me_id, peer_id, before, limit,
        )

    messages = [
        {
            "id": r["id"],
            "sender_id": r["sender_id"],
            "receiver_id": r["receiver_id"],
            "content": r["content"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    messages.reverse()  # chronological order
    return {"messages": messages}


# ---------- API: ОТПРАВКА СООБЩЕНИЯ (HTTP fallback) ----------

@app.post("/api/send")
async def send_message(request: Request):
    payload = await auth_user(request)
    if not payload:
        raise HTTPException(401, "Не авторизован")
    me_id = int(payload["sub"])

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Неверный формат")
    receiver_id = int(data.get("receiver_id") or 0)
    content = (data.get("content") or "").strip()

    if receiver_id == me_id:
        raise HTTPException(400, "Нельзя отправить сообщение самому себе")
    if not content or len(content) > 4000:
        raise HTTPException(400, "Сообщение должно быть от 1 до 4000 символов")

    row = await db.fetchrow(
        """INSERT INTO messages (sender_id, receiver_id, content)
           VALUES ($1, $2, $3) RETURNING id, created_at""",
        me_id, receiver_id, content,
    )

    message = {
        "id": row["id"],
        "sender_id": me_id,
        "receiver_id": receiver_id,
        "content": content,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }

    # Транслируем получателю (и себе) в реальном времени
    await manager.send_to_user(receiver_id, {"type": "message", "message": message})
    await manager.send_to_user(me_id, {"type": "message", "message": message})

    return {"ok": True, "message": message}


# ---------- WEB SOCKET ----------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    payload = decode_token(token) if token else None
    if not payload:
        await websocket.close(code=4401)
        return

    user_id = int(payload["sub"])
    await manager.connect(user_id, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue

            if data.get("type") == "message":
                receiver_id = int(data.get("receiver_id") or 0)
                content = (data.get("content") or "").strip()
                if receiver_id <= 0 or receiver_id == user_id:
                    continue
                if not content or len(content) > 4000:
                    continue

                row = await db.fetchrow(
                    """INSERT INTO messages (sender_id, receiver_id, content)
                       VALUES ($1, $2, $3) RETURNING id, created_at""",
                    user_id, receiver_id, content,
                )
                message = {
                    "id": row["id"],
                    "sender_id": user_id,
                    "receiver_id": receiver_id,
                    "content": content,
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                }
                await manager.send_to_user(receiver_id, {"type": "message", "message": message})
                await manager.send_to_user(user_id, {"type": "message", "message": message})

            elif data.get("type") == "typing":
                receiver_id = int(data.get("receiver_id") or 0)
                await manager.send_to_user(
                    receiver_id, {"type": "typing", "sender_id": user_id}
                )

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(user_id)


# ============================================================
#  ФРОНТЕНД (HTML + JS) — одиночный файл
# ============================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Messenger</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --accent: #4f8cff;
    --accent-dark: #3a6fd8;
    --text: #e6e9ef;
    --muted: #8a90a0;
    --danger: #ff5c5c;
    --radius: 12px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }
  /* ---------- AUTH ---------- */
  .auth-wrap { width: 100%; max-width: 380px; padding: 20px; }
  .auth-card {
    background: var(--panel);
    border-radius: var(--radius);
    padding: 32px 28px;
    box-shadow: 0 12px 40px rgba(0,0,0,.5);
  }
  .auth-card h1 { font-size: 24px; margin-bottom: 4px; }
  .auth-card .sub { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
  .field { margin-bottom: 16px; }
  .field label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
  .field input {
    width: 100%;
    padding: 12px 14px;
    border-radius: 8px;
    border: 1px solid #2a2f3a;
    background: #0f1115;
    color: var(--text);
    font-size: 15px;
    outline: none;
    transition: border .2s;
  }
  .field input:focus { border-color: var(--accent); }
  .btn {
    width: 100%;
    padding: 13px;
    border: none;
    border-radius: 8px;
    background: var(--accent);
    color: #fff;
    font-size: 16px;
    font-weight: 600;
    cursor: pointer;
    transition: background .2s;
  }
  .btn:hover { background: var(--accent-dark); }
  .switch { margin-top: 18px; text-align: center; font-size: 14px; color: var(--muted); }
  .switch a { color: var(--accent); cursor: pointer; text-decoration: none; }
  .err {
    background: rgba(255,92,92,.12);
    color: var(--danger);
    padding: 10px 12px;
    border-radius: 8px;
    font-size: 13px;
    margin-bottom: 16px;
    display: none;
  }
  .err.show { display: block; }
  /* ---------- APP ---------- */
  .app { display: none; width: 100%; height: 100%; }
  .app.show { display: flex; }
  .sidebar {
    width: 280px;
    min-width: 220px;
    background: var(--panel);
    border-right: 1px solid #1f232c;
    display: flex;
    flex-direction: column;
    height: 100%;
  }
  .me {
    padding: 16px;
    border-bottom: 1px solid #1f232c;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .me .avatar {
    width: 36px; height: 36px;
    border-radius: 50%;
    background: var(--accent);
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; flex-shrink: 0;
  }
  .me .name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 600; }
  .me .logout { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 18px; }
  .me .logout:hover { color: var(--danger); }
  .search-box { padding: 12px; }
  .search-box input {
    width: 100%; padding: 10px 12px;
    border-radius: 8px; border: 1px solid #2a2f3a;
    background: #0f1115; color: var(--text);
    outline: none; font-size: 14px;
  }
  .users-list { flex: 1; overflow-y: auto; padding: 0 8px 8px; }
  .user-item {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 12px; border-radius: 8px; cursor: pointer;
    transition: background .15s;
  }
  .user-item:hover { background: rgba(255,255,255,.05); }
  .user-item.active { background: rgba(79,140,255,.18); }
  .user-item .avatar { width: 38px; height: 38px; border-radius: 50%; background: #2a2f3a; display: flex; align-items: center; justify-content: center; font-weight: 700; flex-shrink: 0; }
  .user-item .uinfo { flex: 1; min-width: 0; }
  .user-item .uname { font-weight: 600; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-left: 6px; background: #3aa76d; }
  .status-dot.off { background: var(--muted); }
  .no-users { color: var(--muted); text-align: center; font-size: 13px; padding: 20px; }
  /* Chat */
  .chat {
    flex: 1; display: none; flex-direction: column; height: 100%; min-width: 0;
  }
  .chat.active { display: flex; }
  .chat-header {
    padding: 14px 18px; border-bottom: 1px solid #1f232c;
    display: flex; align-items: center; gap: 12px;
    background: var(--panel);
  }
  .chat-header .avatar { width: 34px; height: 34px; border-radius: 50%; background: #2a2f3a; display: flex; align-items: center; justify-content: center; font-weight: 700; }
  .chat-header .name { font-weight: 600; }
  .chat-header .online { font-size: 12px; color: var(--muted); }
  .messages { flex: 1; overflow-y: auto; padding: 18px; display: flex; flex-direction: column; gap: 6px; }
  .msg {
    max-width: 65%; padding: 9px 13px; border-radius: 14px;
    font-size: 15px; line-height: 1.4; word-wrap: break-word; white-space: pre-wrap;
  }
  .msg.mine { align-self: flex-end; background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
  .msg.theirs { align-self: flex-start; background: #232833; border-bottom-left-radius: 4px; }
  .msg .time { display: block; font-size: 11px; margin-top: 4px; opacity: .7; text-align: right; }
  .typing { color: var(--muted); font-size: 13px; padding: 4px 18px; font-style: italic; }
  .chat-input {
    padding: 12px 14px; border-top: 1px solid #1f232c;
    display: flex; gap: 10px; background: var(--panel);
  }
  .chat-input textarea {
    flex: 1; resize: none; padding: 12px 14px;
    border-radius: 10px; border: 1px solid #2a2f3a;
    background: #0f1115; color: var(--text);
    font-size: 15px; outline: none; font-family: inherit; max-height: 120px;
  }
  .chat-input textarea:focus { border-color: var(--accent); }
  .chat-input button {
    align-self: flex-end; padding: 12px 16px; border: none;
    border-radius: 10px; background: var(--accent); color: #fff;
    font-size: 15px; font-weight: 600; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center;
  }
  .chat-input button:hover { background: var(--accent-dark); }
  .empty-chat { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--muted); font-size: 16px; }
  /* Scrollbars */
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-thumb { background: #2a2f3a; border-radius: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  /* Mobile */
  @media (max-width: 640px) {
    .sidebar { width: 100%; }
    .chat { display: none; }
    .chat.active.open { display: flex; position: absolute; inset: 0; background: var(--bg); }
    .sidebar.hidden { display: none; }
    .back-btn { display: inline-flex; background: none; border: none; color: var(--text); font-size: 20px; cursor: pointer; margin-right: 4px; }
    .msg { max-width: 80%; }
  }
  .back-btn { display: none; cursor: pointer; align-items: center; justify-content: center; background: none; border: none; color: var(--text); }
  /* Welcome (чат не выбран) */
  .welcome {
    flex: 1; display: flex; align-items: center; justify-content: center;
    background: var(--bg); height: 100%;
  }
  .welcome-box { text-align: center; color: var(--muted); padding: 20px; }
  .welcome-icon { font-size: 48px; margin-bottom: 12px; }
  .welcome-box h2 { color: var(--text); margin-bottom: 6px; font-size: 20px; }
  .welcome-box p { font-size: 14px; }
  /* Модалка профиля */
  .modal-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.6);
    display: flex; align-items: center; justify-content: center; z-index: 100;
    padding: 20px;
  }
  .modal {
    background: var(--panel); border-radius: var(--radius);
    width: 100%; max-width: 380px; box-shadow: 0 12px 40px rgba(0,0,0,.5);
    overflow: hidden;
  }
  .modal-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 20px; border-bottom: 1px solid #1f232c;
  }
  .modal-header h3 { font-size: 18px; }
  .modal-close { background: none; border: none; color: var(--muted); font-size: 24px; cursor: pointer; line-height: 1; display: inline-flex; align-items: center; justify-content: center; }
  .modal-close:hover { color: var(--danger); }
  .modal-body { padding: 20px; display: flex; flex-direction: column; gap: 14px; }
  .modal-body textarea {
    width: 100%; background: #0f1115; color: var(--text);
    border: 1px solid #2a2f3a; border-radius: 8px; padding: 10px;
    font-family: inherit; font-size: 14px; resize: vertical;
  }
  .modal-body textarea:focus { outline: none; border-color: var(--accent); }
  .btn.btn-small { width: auto; padding: 9px 16px; font-size: 14px; }
  .profile-avatar-preview {
    width: 90px; height: 90px; border-radius: 50%; margin: 0 auto;
    background: #2a2f3a; display: flex; align-items: center; justify-content: center;
    font-size: 36px; font-weight: 700; cursor: pointer; overflow: hidden;
    background-size: cover; background-position: center;
  }
  .profile-avatar-preview img { width: 100%; height: 100%; object-fit: cover; }
  /* Аватары в списках/шапке как картинки */
  .avatar-img {
    width: 100%; height: 100%; border-radius: 50%; object-fit: cover;
  }
  /* Кнопки в me */
  .me .logout { padding: 6px 10px; font-size: 13px; border-radius: 6px; border: 1px solid #2a2f3a; cursor: pointer; background: #0f1115; color: var(--muted); }
  .me .logout:hover { color: var(--text); }
  .me .icon-btn { display: inline-flex; align-items: center; justify-content: center; padding: 6px 8px; }
  .side-tabs {
    display: flex; gap: 6px; padding: 0 12px 8px;
  }
  .side-tab {
    flex: 1; padding: 8px; border-radius: 8px; border: 1px solid #2a2f3a;
    background: #0f1115; color: var(--text); cursor: pointer; font-size: 13px;
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    text-align: center;
  }
  .side-tab.active { background: rgba(79,140,255,.18); border-color: var(--accent); }
  .dialog-item .preview { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .empty-list { color: var(--muted); text-align: center; font-size: 13px; padding: 16px; }
</style>
</head>
<body>

<!-- ================= AUTH ================= -->
<div class="auth-wrap" id="authWrap">
  <div class="auth-card">
    <h1 id="authTitle">Вход</h1>
    <div class="sub" id="authSub">С возвращением!</div>
    <div class="err" id="authErr"></div>

    <div class="field">
      <label id="loginLabel">Логин</label>
      <input type="text" id="authLogin" placeholder="Введите логин" autocomplete="username">
    </div>
    <div class="field">
      <label>Пароль</label>
      <input type="password" id="authPass" placeholder="••••••••" autocomplete="current-password">
    </div>

    <button class="btn" id="authBtn" onclick="submitAuth()">Войти</button>
    <div class="switch">
      <span id="switchText">Нет аккаунта?</span>
      <a id="switchLink" onclick="toggleAuth()">Зарегистрироваться</a>
    </div>
  </div>
</div>

<!-- ================= APP ================= -->
<div class="app" id="app">
  <div class="sidebar" id="sidebar">
    <div class="me">
      <div class="avatar" id="meAvatar" onclick="openProfile()">?</div>
      <div class="name" id="meName" onclick="openProfile()">—</div>
      <button class="logout icon-btn" title="Профиль" onclick="openProfile()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      </button>
      <button class="logout" title="Выйти" onclick="logout()">Выход</button>
    </div>
    <div class="search-box">
      <input type="text" id="userSearch" placeholder="Поиск собеседника..." oninput="onSearch()">
    </div>
    <div class="side-tabs">
      <button class="side-tab active" id="tabDialogs" onclick="setTab('dialogs')">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        <span>Диалоги</span>
      </button>
      <button class="side-tab" id="tabUsers" onclick="setTab('users')">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        <span>Люди</span>
      </button>
    </div>
    <div class="users-list" id="usersList"></div>
  </div>

  <div class="chat" id="chat">
    <div class="chat-header">
      <button class="back-btn" onclick="closeChatMobile()">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
      </button>
      <div class="avatar" id="peerAvatar">?</div>
      <div>
        <div class="name" id="peerName">—</div>
        <div class="online" id="peerOnline"></div>
      </div>
    </div>
    <div class="messages" id="messages"></div>
    <div class="typing" id="typing" style="display:none;">печатает...</div>
    <div class="chat-input">
      <textarea id="msgInput" placeholder="Введите сообщение..." rows="1"
        onkeydown="onInputKey(event)"></textarea>
      <button class="send-btn" onclick="sendMessage()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      </button>
    </div>
  </div>

  <!-- ===== ПУСТОЙ СТАРТ (когда чат не открыт) ===== -->
  <div class="welcome" id="welcome">
    <div class="welcome-box">
      <div class="welcome-icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      </div>
      <h2>Выберите собеседника</h2>
      <p>Откройте диалог слева, чтобы начать переписку</p>
    </div>
  </div>
</div>

<!-- ================= МОДАЛКА ПРОФИЛЯ ================= -->
<div class="modal-overlay" id="profileModal" style="display:none;">
  <div class="modal">
    <div class="modal-header">
      <h3>Мой профиль</h3>
      <button class="modal-close" onclick="closeProfile()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="modal-body">
      <div class="profile-avatar-preview" id="profileAvatarPreview" onclick="document.getElementById('avatarInput').click()">
        <span id="profileAvatarText">?</span>
      </div>
      <button class="btn btn-small" onclick="document.getElementById('avatarInput').click()">Загрузить аватар</button>
      <input type="file" id="avatarInput" accept="image/*" style="display:none;" onchange="onAvatarSelected(event)">
      <div class="field">
        <label>Никнейм</label>
        <input type="text" id="profileName" disabled>
      </div>
      <div class="field">
        <label>Описание (о себе)</label>
        <textarea id="profileBio" rows="3" maxlength="300" placeholder="Расскажите о себе..."></textarea>
      </div>
      <div class="err" id="profileErr"></div>
      <button class="btn" onclick="saveProfile()">Сохранить</button>
    </div>
  </div>
</div>

<script>
// ============================================================
//  СОСТОЯНИЕ
// ============================================================
let TOKEN = localStorage.getItem('token') || null;
let ME = null;
let users = [];
let activePeer = null;          // id собеседника
let messagesCache = {};         // peer_id -> [messages]
let ws = null;
let typingTimeout = null;

// ============================================================
//  API HELPERS
// ============================================================
async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { logout(); throw new Error('Unauthorized'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || 'Ошибка сервера');
  return data;
}

// ============================================================
//  AUTH UI
// ============================================================
let isRegister = false;

function toggleAuth() {
  isRegister = !isRegister;
  document.getElementById('authTitle').textContent = isRegister ? 'Регистрация' : 'Вход';
  document.getElementById('authSub').textContent = isRegister ? 'Создайте аккаунт' : 'С возвращением!';
  document.getElementById('loginLabel').textContent = isRegister ? 'Имя пользователя' : 'Логин';
  document.getElementById('authBtn').textContent = isRegister ? 'Зарегистрироваться' : 'Войти';
  document.getElementById('switchText').textContent = isRegister ? 'Уже есть аккаунт?' : 'Нет аккаунта?';
  document.getElementById('switchLink').textContent = isRegister ? 'Войти' : 'Зарегистрироваться';
  document.getElementById('authErr').classList.remove('show');
}

function showErr(msg) {
  const el = document.getElementById('authErr');
  el.textContent = msg;
  el.classList.add('show');
}

async function submitAuth() {
  const err = document.getElementById('authErr');
  err.classList.remove('show');
  const login = document.getElementById('authLogin').value.trim();
  const pass = document.getElementById('authPass').value;
  const btn = document.getElementById('authBtn');
  btn.disabled = true;

  try {
    let data;
    if (isRegister) {
      data = await api('/api/register', {
        method: 'POST',
        body: JSON.stringify({ username: login, password: pass })
      });
    } else {
      data = await api('/api/login', {
        method: 'POST',
        body: JSON.stringify({ login, password: pass })
      });
    }
    TOKEN = data.token;
    localStorage.setItem('token', TOKEN);
    ME = data.user;
    await initApp();
  } catch (e) {
    showErr(e.message);
  } finally {
    btn.disabled = false;
  }
}

function logout() {
  TOKEN = null;
  localStorage.removeItem('token');
  ME = null;
  activePeer = null;
  users = [];
  dialogs = [];
  messagesCache = {};
  if (ws) { try { ws.close(); } catch(e){} ws = null; }
  document.getElementById('app').classList.remove('show');
  document.getElementById('authWrap').style.display = '';
  document.getElementById('authPass').value = '';
  document.getElementById('authLogin').value = '';
  document.getElementById('welcome').style.display = 'none';
  document.getElementById('chat').classList.remove('active');
  document.getElementById('profileModal').style.display = 'none';
}

// ============================================================
//  INIT APP
// ============================================================
let searchTerm = '';
let dialogs = [];
let currentTab = 'dialogs';

async function initApp() {
  document.getElementById('authWrap').style.display = 'none';
  document.getElementById('app').classList.add('show');
  document.getElementById('meName').textContent = ME.username;
  renderMeAvatar();

  currentTab = 'dialogs';
  updateTabButtons();
  await Promise.all([loadUsers(), loadDialogs()]);
  renderSidebar();
  connectWS();

  // welcome-экран виден по умолчанию, чат скрыт
  document.getElementById('welcome').style.display = 'flex';
  document.getElementById('chat').classList.remove('open');
}

function renderMeAvatar() {
  const el = document.getElementById('meAvatar');
  if (ME.avatar) {
    el.innerHTML = `<img class="avatar-img" src="${ME.avatar}" alt="">`;
    el.style.background = 'transparent';
  } else {
    el.textContent = ME.username[0].toUpperCase();
    el.style.background = 'var(--accent)';
  }
}

// ============================================================
//  USERS & DIALOGS
// ============================================================
async function loadUsers() {
  try {
    const data = await api('/api/users');
    users = data.users || [];
  } catch (e) { console.error(e); }
}

async function loadDialogs() {
  try {
    const data = await api('/api/dialogs');
    dialogs = data.dialogs || [];
  } catch (e) { console.error(e); }
}

function onSearch() {
  searchTerm = document.getElementById('userSearch').value.trim().toLowerCase();
  renderSidebar();
}

function setTab(tab) {
  currentTab = tab;
  updateTabButtons();
  renderSidebar();
}

function updateTabButtons() {
  document.getElementById('tabDialogs').classList.toggle('active', currentTab === 'dialogs');
  document.getElementById('tabUsers').classList.toggle('active', currentTab === 'users');
}

function renderSidebar() {
  if (currentTab === 'dialogs') renderDialogs();
  else renderUsers();
}

function renderDialogs() {
  const list = document.getElementById('usersList');
  if (dialogs.length === 0) {
    list.innerHTML = `<div class="empty-list">Пока нет диалогов.<br>Нажмите «<svg style="vertical-align:-3px" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg> Люди», чтобы найти собеседника.</div>`;
    return;
  }
  // учитываем поиск
  let arr = dialogs;
  if (searchTerm) {
    arr = arr.filter(d => d.username.toLowerCase().includes(searchTerm));
    if (arr.length === 0) {
      list.innerHTML = '<div class="empty-list">Ничего не найдено</div>';
      return;
    }
  }
  list.innerHTML = arr.map(d => `
    <div class="user-item dialog-item ${activePeer === d.id ? 'active' : ''}" onclick="openChat(${d.id})">
      ${avatarHtml(d, '38px')}
      <div class="uinfo">
        <div class="uname">${escapeHtml(d.username)}
          <span class="status-dot ${d.online ? '' : 'off'}"></span>
        </div>
        <div class="preview">${escapeHtml(d.last_content || 'Нет сообщений')}</div>
      </div>
    </div>
  `).join('');
}

function renderUsers() {
  const list = document.getElementById('usersList');
  const filtered = users.filter(u =>
    !searchTerm || u.username.toLowerCase().includes(searchTerm)
  );
  if (filtered.length === 0) {
    list.innerHTML = '<div class="no-users">Пользователей не найдено</div>';
    return;
  }
  list.innerHTML = filtered.map(u => `
    <div class="user-item ${activePeer === u.id ? 'active' : ''}" onclick="openChat(${u.id})">
      ${avatarHtml(u, '38px')}
      <div class="uinfo">
        <div class="uname">${escapeHtml(u.username)}
          <span class="status-dot ${u.online ? '' : 'off'}"></span>
        </div>
        ${u.bio ? `<div class="preview">${escapeHtml(u.bio)}</div>` : ''}
      </div>
    </div>
  `).join('');
}

function avatarHtml(user, size) {
  if (user.avatar) {
    return `<div class="avatar" style="width:${size};height:${size};overflow:hidden;background:#2a2f3a;">
      <img class="avatar-img" src="${user.avatar}" alt=""></div>`;
  }
  const letter = (user.username || '?')[0].toUpperCase();
  return `<div class="avatar" style="width:${size};height:${size};">${escapeHtml(letter)}</div>`;
}

// ============================================================
//  CHAT
// ============================================================
function findPeer(peerId) {
  return users.find(u => u.id === peerId) || dialogs.find(d => d.id === peerId) || {};
}

async function openChat(peerId) {
  activePeer = peerId;
  const peer = findPeer(peerId);
  document.getElementById('peerName').textContent = peer.username || '?';
  const pa = document.getElementById('peerAvatar');
  if (peer.avatar) {
    pa.innerHTML = `<img class="avatar-img" src="${peer.avatar}" alt="">`;
    pa.style.background = 'transparent';
  } else {
    pa.textContent = (peer.username || '?')[0].toUpperCase();
    pa.style.background = '#2a2f3a';
  }
  updatePeerOnline();

  // Показываем чат, прячем welcome
  document.getElementById('welcome').style.display = 'none';
  const chatEl = document.getElementById('chat');
  chatEl.classList.add('active');
  chatEl.style.display = 'flex';

  // Mobile
  if (window.innerWidth <= 640) {
    document.getElementById('sidebar').classList.add('hidden');
    document.getElementById('chat').classList.add('open');
  }

  renderSidebar();

  // ВСЕГДА тянем свежую историю из БД (чтобы переписка появлялась)
  try {
    const data = await api('/api/messages/' + peerId);
    const fresh = data.messages || [];
    // Сохраняем те temp-сообщения, которых ещё нет в истории
    const existing = messagesCache[peerId] || [];
    const pendingTemps = existing.filter(x =>
      typeof x.id === 'string' && x.id.indexOf('temp-') === 0 &&
      !fresh.some(m => m.content === x.content && m.sender_id === ME.id)
    );
    messagesCache[peerId] = fresh.concat(pendingTemps);
  } catch (e) {
    if (!messagesCache[peerId]) messagesCache[peerId] = [];
  }
  renderMessages();
  scrollToBottom();
  document.getElementById('msgInput').focus();
}

function closeChat() {
  activePeer = null;
  const chatEl = document.getElementById('chat');
  chatEl.classList.remove('active');
  chatEl.style.display = 'none';
  document.getElementById('welcome').style.display = 'flex';
  renderSidebar();
}

function closeChatMobile() {
  const chatEl = document.getElementById('chat');
  chatEl.classList.remove('open');
  document.getElementById('sidebar').classList.remove('hidden');
}

function updatePeerOnline() {
  const peer = findPeer(activePeer);
  const el = document.getElementById('peerOnline');
  if (peer && peer.online) {
    el.textContent = 'в сети';
    el.style.color = '#3aa76d';
  } else {
    el.textContent = 'не в сети';
    el.style.color = '#8a90a0';
  }
}

function renderMessages() {
  const box = document.getElementById('messages');
  const msgs = messagesCache[activePeer] || [];
  if (msgs.length === 0) {
    box.innerHTML = '<div class="empty-chat">Напишите первое сообщение!</div>';
    return;
  }
  box.innerHTML = msgs.map(m => {
    const mine = m.sender_id === ME.id;
    const time = m.created_at ? new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
    return `<div class="msg ${mine ? 'mine' : 'theirs'}">${escapeHtml(m.content)}
      <span class="time">${time}</span></div>`;
  }).join('');
}

function scrollToBottom() {
  const box = document.getElementById('messages');
  box.scrollTop = box.scrollHeight;
}

function escapeHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
            .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ============================================================
//  ПРОФИЛЬ
// ============================================================
let pendingAvatar = null;

function openProfile() {
  document.getElementById('profileName').value = ME.username || '';
  document.getElementById('profileBio').value = ME.bio || '';
  pendingAvatar = ME.avatar || null;
  updateProfilePreview();
  document.getElementById('profileErr').classList.remove('show');
  document.getElementById('profileModal').style.display = 'flex';
}

function closeProfile() {
  document.getElementById('profileModal').style.display = 'none';
}

function updateProfilePreview() {
  const prev = document.getElementById('profileAvatarPreview');
  const txt = document.getElementById('profileAvatarText');
  if (pendingAvatar) {
    prev.style.backgroundImage = `url(${pendingAvatar})`;
    txt.textContent = '';
  } else {
    prev.style.backgroundImage = '';
    txt.textContent = (ME.username || '?')[0].toUpperCase();
  }
}

function onAvatarSelected(e) {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  if (file.size > 700 * 1024) {
    document.getElementById('profileErr').textContent = 'Картинка слишком большая (макс 700 КБ)';
    document.getElementById('profileErr').classList.add('show');
    return;
  }
  const reader = new FileReader();
  reader.onload = (ev) => {
    pendingAvatar = ev.target.result;   // data:image/...;base64,...
    updateProfilePreview();
  };
  reader.readAsDataURL(file);
}

async function saveProfile() {
  const bio = document.getElementById('profileBio').value.trim();
  const err = document.getElementById('profileErr');
  err.classList.remove('show');
  try {
    const data = await api('/api/profile', {
      method: 'PATCH',
      body: JSON.stringify({ avatar: pendingAvatar || '', bio })
    });
    ME.avatar = data.user.avatar;
    ME.bio = data.user.bio;
    renderMeAvatar();
    closeProfile();
    renderSidebar();
  } catch (e) {
    err.textContent = e.message;
    err.classList.add('show');
  }
}

// ============================================================
//  SENDING
// ============================================================
function onInputKey(e) {
  const el = document.getElementById('msgInput');
  // auto-resize
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
  // typing indicator via ws
  if (ws && ws.readyState === 1 && activePeer) {
    ws.send(JSON.stringify({ type: 'typing', receiver_id: activePeer }));
  }
}

async function sendMessage() {
  const el = document.getElementById('msgInput');
  const content = el.value.trim();
  if (!content || !activePeer) return;

  el.value = '';
  el.style.height = 'auto';

  // Оптимистично показываем сообщение сразу (без ожидания сервера)
  const tempMsg = {
    id: 'temp-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7),
    sender_id: ME.id,
    receiver_id: activePeer,
    content,
    created_at: new Date().toISOString(),
  };
  pushMessage(activePeer, tempMsg);
  renderMessages();
  scrollToBottom();

  // Отправляем ВСЕГДА через HTTP POST (гарантированно доходит и сохраняется).
  // Эхо от сервера придёт по WS и заменит временное сообщение на настоящее.
  try {
    await api('/api/send', {
      method: 'POST',
      body: JSON.stringify({ receiver_id: activePeer, content })
    });
    // Обновляем список диалогов (превью последнего сообщения)
    await loadDialogs();
    renderSidebar();
  } catch (e) {
    // Убираем оптимистичное сообщение при ошибке
    messagesCache[activePeer] = (messagesCache[activePeer] || []).filter(m => m.id !== tempMsg.id);
    renderMessages();
    alert(e.message);
  }
  el.focus();
}

// Добавляет сообщение в кэш БЕЗ дубликатов.
// WS-эхо подтверждает моё оптимистичное сообщение: temp-запись ЗАМЕНЯЕТСЯ
// на настоящую (на том же месте, чтобы сохранить порядок).
// Повторные эхо с одинаковым id игнорируются.
function pushMessage(peerId, m) {
  if (!messagesCache[peerId]) messagesCache[peerId] = [];
  const arr = messagesCache[peerId];

  // Предотвращаем дубликаты по реальному id
  if (typeof m.id === 'number' && arr.some(x => x.id === m.id)) return;

  // Серверное подтверждение моего оптимистичного сообщения (temp-...)
  if (typeof m.id === 'number' && m.sender_id === ME.id) {
    for (let i = arr.length - 1; i >= 0; i--) {
      const x = arr[i];
      if (typeof x.id === 'string' && x.id.indexOf('temp-') === 0 &&
          x.sender_id === ME.id && x.content === m.content) {
        arr[i] = m;      // заменяем на месте, сохраняя позицию
        return;
      }
    }
  }
  arr.push(m);
}

// ============================================================
//  WEBSOCKET
// ============================================================
function connectWS() {
  if (!TOKEN) return;                       // не подключаемся без токена
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);

  ws.onmessage = (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch(err) { return; }

    if (data.type === 'message') {
      const m = data.message;
      // add to cache for the involved chats (без дубликатов)
      const peerId = (m.sender_id === ME.id) ? m.receiver_id : m.sender_id;
      pushMessage(peerId, m);

      // If it's the active chat, rerender
      if (peerId === activePeer) {
        renderMessages();
        scrollToBottom();
      } else {
        // notify via title or subtle
        maybeNotify(m);
      }
      // Обновляем превью диалогов
      loadDialogs().then(() => renderSidebar());
    } else if (data.type === 'typing') {
      if (data.sender_id === activePeer) {
        const el = document.getElementById('typing');
        el.style.display = 'block';
        clearTimeout(typingTimeout);
        typingTimeout = setTimeout(() => el.style.display = 'none', 2000);
      }
    }
  };

  ws.onclose = () => {
    if (TOKEN) setTimeout(connectWS, 3000); // reconnect
  };
  ws.onerror = () => { try { ws.close(); } catch(e){} };
}

function maybeNotify(m) {
  try {
    if (Notification && Notification.permission === 'granted') {
      const peer = users.find(u => u.id === m.sender_id);
      new Notification(`${peer ? peer.username : 'Сообщение'}`, { body: m.content });
    }
  } catch(e) {}
}

// ============================================================
//  BOOT
// ============================================================
(async function boot() {
  if (TOKEN) {
    try {
      const data = await api('/api/me');
      ME = data.user;
      await initApp();
      // request notification permission
      if ('Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission();
      }
    } catch (e) {
      logout();
    }
  }
})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML_PAGE)


# ============================================================
#  ЗАПУСК
# ============================================================

if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("  MESSENGER запущен!")
    print(f"  Сайт:     http://localhost:{PORT}")
    print(f"  Хост:     {HOST}")
    print("=" * 60)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
