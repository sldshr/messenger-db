"""
sldchat — веб-чат (IRC/TeamSpeak-like) на FastAPI + Supabase.
Один файл. Все таблицы — с префиксом sldchat_, чтобы не конфликтовать
с другими проектами в той же БД.

ENV:
  SUPABASE_URL      https://xxxx.supabase.co
  SUPABASE_KEY      service_role key
  SUPABASE_DB_URL   (нужен при VERIFICATION=True) — ОБЯЗАТЕЛЬНО через pooler:
                    postgresql://postgres.<ref>:<URL-encoded-pass>@aws-0-<region>.pooler.supabase.com:6543/postgres

Запуск:  fastapi run main.py   или   uvicorn main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from typing import Optional, Dict, List
from urllib.parse import urlparse, quote

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from supabase import create_client, Client

try:
    from postgrest.exceptions import APIError as PgAPIError
except Exception:  # pragma: no cover
    class PgAPIError(Exception):
        pass


# ============================================================
#  КОНФИГ
# ============================================================
# True → при старте создадим/починим ТОЛЬКО свои таблицы (sldchat_*).
# Требует SUPABASE_DB_URL (лучше через pooler). Чужие таблицы не трогаем.
VERIFICATION = True

TABLE_PREFIX = "sldchat_"
T_USERS    = TABLE_PREFIX + "users"
T_TOKENS   = TABLE_PREFIX + "tokens"
T_CHANNELS = TABLE_PREFIX + "channels"
T_MESSAGES = TABLE_PREFIX + "messages"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Задай SUPABASE_URL и SUPABASE_KEY (service_role)")

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

REQUIRED_TABLES = {T_USERS, T_TOKENS, T_CHANNELS, T_MESSAGES}

SCHEMA_SQL = f"""
create table if not exists {T_USERS} (
    id uuid primary key default gen_random_uuid(),
    username text not null,
    username_lower text not null unique,
    salt text not null,
    pass_hash text not null,
    created timestamptz not null default now()
);
create table if not exists {T_TOKENS} (
    token text primary key,
    username text not null,
    created timestamptz not null default now()
);
create table if not exists {T_CHANNELS} (
    id text primary key,
    name text not null,
    owner text not null,
    created timestamptz not null default now()
);
create table if not exists {T_MESSAGES} (
    id uuid primary key default gen_random_uuid(),
    channel_id text not null,
    username text not null,
    text text not null,
    ts timestamptz not null default now()
);
create index if not exists {TABLE_PREFIX}messages_channel_ts_idx
    on {T_MESSAGES} (channel_id, ts desc);
"""


# ============================================================
#  ДИАГНОСТИКА URL
# ============================================================
def _diagnose_db_url(url: str) -> Optional[str]:
    """Возвращает текст-предупреждение, если URL похож на проблемный. Иначе None."""
    if not url:
        return None
    try:
        p = urlparse(url)
    except Exception:
        return "Не удалось разобрать SUPABASE_DB_URL."

    host = (p.hostname or "").lower()
    port = p.port
    user = p.username or ""

    tips = []
    if host.startswith("db.") and host.endswith(".supabase.co"):
        tips.append(
            "⚠️  Ты используешь прямое подключение db.<ref>.supabase.co — "
            "оно теперь IPv6-only. Если у хоста нет IPv6, будет "
            "'Is the server running on that host...'. Возьми Pooler URL: "
            "Supabase → Project Settings → Database → Connection string → "
            "Connection pooling (Session или Transaction)."
        )
    if not host.endswith(".pooler.supabase.com"):
        tips.append(
            "ℹ️  Хост не похож на pooler. Для IPv4 используй "
            "aws-0-<region>.pooler.supabase.com (порт 6543 для transaction, "
            "5432 для session), user = postgres.<project_ref>."
        )
    if host.endswith(".pooler.supabase.com") and "." not in user:
        tips.append(
            "ℹ️  Для pooler юзер должен быть 'postgres.<project_ref>', а не 'postgres'."
        )
    if port not in (5432, 6543):
        tips.append(f"ℹ️  Странный порт {port}: обычно 5432 или 6543.")

    return "\n".join(tips) if tips else None


def _mask_url(url: str) -> str:
    try:
        p = urlparse(url)
        if p.password:
            return url.replace(p.password, "***")
    except Exception:
        pass
    return url


# ============================================================
#  VERIFICATION / CHECK SCHEMA
# ============================================================
def _print_sql_hint(prefix: str = "") -> None:
    print("=" * 70)
    if prefix:
        print(prefix)
    print("[sldchat] Выполни этот SQL в Supabase → SQL Editor:")
    print("-" * 70)
    print(SCHEMA_SQL.strip())
    print("-" * 70)
    print("=" * 70)


def run_verification() -> None:
    """
    Создаёт/чинит ТОЛЬКО свои таблицы (sldchat_*). Чужие не трогает.
    НИКОГДА не роняет приложение: при ошибке печатает подсказку и идёт дальше.
    """
    print(f"[sldchat] VERIFICATION=True: проверяю таблицы '{TABLE_PREFIX}*' ...")

    if not SUPABASE_DB_URL:
        print("[sldchat] SUPABASE_DB_URL не задан — пропускаю миграцию.")
        _print_sql_hint()
        return

    diag = _diagnose_db_url(SUPABASE_DB_URL)
    if diag:
        print("[sldchat] Диагностика SUPABASE_DB_URL:")
        print(diag)
        print(f"[sldchat] URL (пароль скрыт): {_mask_url(SUPABASE_DB_URL)}")

    try:
        import psycopg2
    except ImportError:
        print("[sldchat] Нет psycopg2. Установи: pip install psycopg2-binary")
        _print_sql_hint()
        return

    conn = None
    try:
        conn = psycopg2.connect(SUPABASE_DB_URL, connect_timeout=10)
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute(
                "select tablename from pg_tables "
                "where schemaname = 'public' and tablename like %s;",
                (TABLE_PREFIX + "%",),
            )
            existing = {row[0] for row in cur.fetchall()}
            extras = existing - REQUIRED_TABLES
            for t in sorted(extras):
                print(f"[sldchat] удаляю лишнюю СВОЮ таблицу public.{t}")
                cur.execute(f'drop table if exists public."{t}" cascade;')

            cur.execute(SCHEMA_SQL)

            cur.execute(f"select count(*) from {T_CHANNELS};")
            if cur.fetchone()[0] == 0:
                cur.execute(
                    f"insert into {T_CHANNELS} (id, name, owner) values "
                    f"('general', 'General', 'system'), "
                    f"('random',  'Random',  'system');"
                )
            print("[sldchat] схема в порядке.")
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except Exception as e:
        print("=" * 70)
        print(f"[sldchat] Не удалось подключиться к Postgres: {type(e).__name__}: {e}")
        if diag:
            print(diag)
        _print_sql_hint(
            "[sldchat] Схему можно накатить вручную в Supabase → SQL Editor:"
        )
        print("[sldchat] Приложение всё равно продолжит работать, но /api/* "
              "будут падать, пока таблиц нет.")
        print("=" * 70)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def check_schema() -> None:
    """Быстрая проверка наличия таблиц через PostgREST. Не пишет — только читает."""
    missing = []
    for t in (T_USERS, T_TOKENS, T_CHANNELS, T_MESSAGES):
        try:
            sb.table(t).select("*").limit(1).execute()
        except Exception:
            missing.append(t)
    if missing:
        _print_sql_hint(
            f"[sldchat] ВНИМАНИЕ: не найдены таблицы: {', '.join(missing)}"
        )


if VERIFICATION:
    run_verification()
else:
    check_schema()


# ============================================================
#  ДОСТУП К ДАННЫМ
# ============================================================
def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def db_get_user(username_lower: str) -> Optional[dict]:
    r = sb.table(T_USERS).select("*").eq("username_lower", username_lower).limit(1).execute()
    return r.data[0] if r.data else None


def db_insert_user(username: str, password: str) -> dict:
    salt = secrets.token_hex(8)
    row = {
        "username": username,
        "username_lower": username.lower(),
        "salt": salt,
        "pass_hash": hash_pw(password, salt),
    }
    r = sb.table(T_USERS).insert(row).execute()
    return r.data[0]


def db_insert_token(token: str, username: str) -> None:
    sb.table(T_TOKENS).insert({"token": token, "username": username}).execute()


def db_user_by_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    r = sb.table(T_TOKENS).select("username").eq("token", token).limit(1).execute()
    return r.data[0]["username"] if r.data else None


def db_list_channels() -> List[dict]:
    r = sb.table(T_CHANNELS).select("*").execute()
    return r.data or []


def db_get_channel(cid: str) -> Optional[dict]:
    r = sb.table(T_CHANNELS).select("*").eq("id", cid).limit(1).execute()
    return r.data[0] if r.data else None


def db_create_channel(cid: str, name: str, owner: str) -> dict:
    r = sb.table(T_CHANNELS).insert({"id": cid, "name": name, "owner": owner}).execute()
    return r.data[0]


def db_recent_messages(limit: int = 500) -> List[dict]:
    r = sb.table(T_MESSAGES).select("*").order("ts", desc=True).limit(limit).execute()
    return r.data or []


def db_channel_messages(cid: str, limit: int = 200) -> List[dict]:
    r = (sb.table(T_MESSAGES)
         .select("*").eq("channel_id", cid)
         .order("ts", desc=True).limit(limit).execute())
    data = r.data or []
    data.reverse()
    return data


def db_insert_message(cid: str, username: str, text: str) -> dict:
    r = sb.table(T_MESSAGES).insert({
        "channel_id": cid,
        "username": username,
        "text": text,
    }).execute()
    return r.data[0]


def fmt_message(m: dict) -> dict:
    return {"id": m["id"], "channel": m["channel_id"],
            "user": m["username"], "text": m["text"], "ts": m["ts"]}


def fmt_last(m: Optional[dict]) -> Optional[dict]:
    if not m:
        return None
    return {"user": m["username"], "text": m["text"], "ts": m["ts"]}


# ============================================================
#  FASTAPI
# ============================================================
app = FastAPI(title="sldchat")

connections: Dict[WebSocket, dict] = {}


def _db_is_schema_error(e: Exception) -> bool:
    s = str(e)
    return (
        "PGRST205" in s          # table not found in schema cache
        or "PGRST204" in s       # column not found
        or "42P01" in s          # undefined_table
        or "42703" in s          # undefined_column
        or "does not exist" in s
    )


def require_user(x_auth_token: Optional[str] = Header(None)) -> str:
    try:
        username = db_user_by_token(x_auth_token)
    except PgAPIError as e:
        if _db_is_schema_error(e):
            raise HTTPException(503, "БД не настроена: создай таблицы sldchat_* "
                                     "(см. лог сервера)")
        raise HTTPException(500, "DB error")
    if not username:
        raise HTTPException(401, "Unauthorized")
    return username


class AuthBody(BaseModel):
    username: str
    password: str


class ChannelBody(BaseModel):
    name: str


# ---------- AUTH ----------
@app.post("/api/register")
def register(body: AuthBody):
    name = body.username.strip()
    if not (3 <= len(name) <= 24):
        raise HTTPException(400, "Имя: 3–24 символа")
    if not all(c.isalnum() or c in "_-" for c in name):
        raise HTTPException(400, "Только буквы, цифры, _ и -")
    if len(body.password) < 4:
        raise HTTPException(400, "Пароль: минимум 4 символа")

    try:
        if db_get_user(name.lower()):
            raise HTTPException(409, "Такое имя уже занято")
        user = db_insert_user(name, body.password)
        token = secrets.token_urlsafe(24)
        db_insert_token(token, user["username"])
    except HTTPException:
        raise
    except PgAPIError as e:
        if _db_is_schema_error(e):
            raise HTTPException(503, "БД не настроена: таблицы sldchat_* отсутствуют")
        s = str(e).lower()
        if "duplicate" in s or "unique" in s:
            raise HTTPException(409, "Такое имя уже занято")
        raise HTTPException(500, "DB error")
    return {"token": token, "username": user["username"]}


@app.post("/api/login")
def login(body: AuthBody):
    try:
        user = db_get_user(body.username.strip().lower())
    except PgAPIError as e:
        if _db_is_schema_error(e):
            raise HTTPException(503, "БД не настроена")
        raise HTTPException(500, "DB error")
    if not user or user["pass_hash"] != hash_pw(body.password, user["salt"]):
        raise HTTPException(401, "Неверный логин или пароль")
    token = secrets.token_urlsafe(24)
    db_insert_token(token, user["username"])
    return {"token": token, "username": user["username"]}


@app.get("/api/me")
def me(user: str = Depends(require_user)):
    return {"username": user}


# ---------- PRESENCE ----------
def _presence_counts() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for info in connections.values():
        cid = info.get("channel")
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
    return counts


async def broadcast_all(payload: dict) -> None:
    dead = []
    for ws in list(connections.keys()):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connections.pop(ws, None)


async def broadcast_presence() -> None:
    await broadcast_all({"type": "presence", "counts": _presence_counts()})


# ---------- CHANNELS ----------
@app.get("/api/channels")
def list_channels(user: str = Depends(require_user)):
    try:
        chans = db_list_channels()
        recent = db_recent_messages(500)
    except PgAPIError as e:
        if _db_is_schema_error(e):
            raise HTTPException(503, "БД не настроена: таблицы sldchat_* отсутствуют")
        raise HTTPException(500, "DB error")

    counts = _presence_counts()
    last_by_ch: Dict[str, dict] = {}
    for m in recent:
        if m["channel_id"] not in last_by_ch:
            last_by_ch[m["channel_id"]] = m

    out = []
    for c in chans:
        out.append({
            "id": c["id"],
            "name": c["name"],
            "owner": c["owner"],
            "online": counts.get(c["id"], 0),
            "last_message": fmt_last(last_by_ch.get(c["id"])),
        })
    out.sort(
        key=lambda c: (c["last_message"]["ts"] if c["last_message"] else ""),
        reverse=True,
    )
    return {"channels": out}


@app.post("/api/channels")
async def create_channel(body: ChannelBody, user: str = Depends(require_user)):
    raw = body.name.strip().lstrip("#").strip()
    if not (1 <= len(raw) <= 32):
        raise HTTPException(400, "Название: 1–32 символа")
    cid = "".join(c for c in raw.lower().replace(" ", "-") if c.isalnum() or c in "-_")
    if not cid:
        raise HTTPException(400, "Некорректное название")
    try:
        if db_get_channel(cid):
            raise HTTPException(409, "Канал уже существует")
        ch = db_create_channel(cid, raw, user)
    except HTTPException:
        raise
    except PgAPIError as e:
        if _db_is_schema_error(e):
            raise HTTPException(503, "БД не настроена")
        raise HTTPException(500, "DB error")
    await broadcast_all({"type": "channels_changed"})
    return {"id": ch["id"], "name": ch["name"]}


@app.get("/api/channels/{cid}/messages")
def channel_messages(cid: str, user: str = Depends(require_user), limit: int = 200):
    try:
        if not db_get_channel(cid):
            raise HTTPException(404, "Канал не найден")
        msgs = db_channel_messages(cid, limit)
    except HTTPException:
        raise
    except PgAPIError as e:
        if _db_is_schema_error(e):
            raise HTTPException(503, "БД не настроена")
        raise HTTPException(500, "DB error")
    return {"messages": [fmt_message(m) for m in msgs]}


# ---------- WEBSOCKET ----------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token")
    cid = ws.query_params.get("channel")

    try:
        username = await asyncio.to_thread(db_user_by_token, token)
        channel = await asyncio.to_thread(db_get_channel, cid) if cid else None
    except Exception:
        await ws.close(code=1011)
        return

    if not username or not channel:
        await ws.close(code=1008)
        return

    connections[ws] = {"username": username, "channel": cid}
    await broadcast_presence()

    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "message":
                text = (data.get("text") or "").strip()
                if not text or len(text) > 4000:
                    continue
                try:
                    msg = await asyncio.to_thread(db_insert_message, cid, username, text)
                except Exception:
                    continue
                await broadcast_all({"type": "message", "message": fmt_message(msg)})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        connections.pop(ws, None)
        try:
            await broadcast_presence()
        except Exception:
            pass


# ============================================================
#  ВСТРОЕННЫЙ КЛИЕНТ
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#517da2">
<title>sldchat</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0;
    -webkit-tap-highlight-color: transparent;
    -webkit-user-select: none; user-select: none;
    -webkit-touch-callout: none; }
input, textarea { -webkit-user-select: text; user-select: text; }
html, body { height: 100vh; height: 100dvh; overflow: hidden;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 14px; color: #222; background: #e6ebee; }
button { font-family: inherit; cursor: pointer; border: none; background: none; color: inherit; }
input, textarea { font-family: inherit; }
svg { display: block; }

#auth-screen { position: fixed; inset: 0; z-index: 100;
  display: flex; align-items: center; justify-content: center; padding: 20px;
  background: linear-gradient(140deg, #5c88ae 0%, #3c6591 55%, #2e5379 100%); }
.auth-card { width: 360px; max-width: 100%; background: #fff;
  border-radius: 14px; overflow: hidden;
  box-shadow: 0 24px 70px rgba(0,0,0,.4), 0 2px 6px rgba(0,0,0,.15); }
.auth-head { padding: 30px 26px 4px; text-align: center; }
.auth-logo { font-size: 32px; font-weight: 300; letter-spacing: 3px; color: #517da2; }
.auth-sub { font-size: 12px; color: #9aa5ad; margin-top: 4px; }
.auth-tabs { display: flex; padding: 0 22px; margin-top: 22px; border-bottom: 1px solid #e6ebee; }
.auth-tab { flex: 1; padding: 12px 0; font-size: 13.5px; font-weight: 600;
  color: #9aa5ad; border-bottom: 2px solid transparent;
  transition: color .15s, border-color .15s; }
.auth-tab:hover { color: #517da2; }
.auth-tab.active { color: #517da2; border-bottom-color: #517da2; }
.auth-body { padding: 18px 24px 26px; }
.auth-body input { width: 100%; padding: 12px 14px; margin-bottom: 12px;
  border: 1px solid #dfe5ea; border-radius: 8px; font-size: 14px; outline: none;
  background: #fbfcfd; color: #222;
  transition: border-color .15s, box-shadow .15s, background .15s; }
.auth-body input:focus { border-color: #517da2; background: #fff;
  box-shadow: 0 0 0 3px rgba(81,125,162,.14); }
.auth-error { color: #d64541; font-size: 12px; min-height: 16px; margin-bottom: 6px; }
.auth-submit { width: 100%; margin-top: 6px; padding: 12px;
  background: #517da2; color: #fff; border-radius: 8px;
  font-size: 14px; font-weight: 600; transition: background .15s, transform .06s; }
.auth-submit:hover { background: #46708f; }
.auth-submit:active { transform: scale(.985); }

#app { display: none; height: 100vh; height: 100dvh; }
#app.visible { display: flex; }

.sidebar { width: 300px; flex-shrink: 0; background: #fff;
  border-right: 1px solid #dfe5ea; display: flex; flex-direction: column; }
.sidebar-header { height: 56px; flex-shrink: 0; background: #517da2; color: #fff;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 12px 0 14px; }
.me { display: flex; align-items: center; gap: 10px; min-width: 0; }
.avatar { width: 34px; height: 34px; border-radius: 50%;
  background: rgba(255,255,255,.22); color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-weight: 600; font-size: 14px; text-transform: uppercase; flex-shrink: 0; }
#me-name { font-weight: 600; font-size: 14px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.icon-btn { width: 36px; height: 36px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  color: #fff; opacity: .85; flex-shrink: 0;
  transition: background .12s, opacity .12s; }
.icon-btn:hover { background: rgba(255,255,255,.14); opacity: 1; }

.search-wrap { position: relative; padding: 10px 12px;
  border-bottom: 1px solid #eef2f5; flex-shrink: 0; }
.search-icon { position: absolute; left: 22px; top: 50%; transform: translateY(-50%);
  color: #9aa5ad; pointer-events: none; }
.search-wrap input { width: 100%; padding: 9px 12px 9px 36px;
  background: #f2f5f8; border: 1px solid transparent; border-radius: 8px;
  font-size: 13px; outline: none; color: #222;
  transition: background .15s, border-color .15s; }
.search-wrap input:focus { background: #fff; border-color: #d8e0e7; }

.channel-list { flex: 1; overflow-y: auto; padding: 6px 0; }
.channel-item { display: flex; align-items: center; gap: 10px;
  padding: 10px 14px; cursor: pointer; transition: background .12s; }
.channel-item:hover { background: #f3f7fa; }
.channel-item.active { background: #517da2; color: #fff; }
.channel-item.active .channel-meta { color: rgba(255,255,255,.8); }
.channel-item.active .channel-last { color: rgba(255,255,255,.72); }
.channel-hash { width: 34px; height: 34px; flex-shrink: 0; border-radius: 50%;
  background: #eef3f7; color: #517da2;
  display: flex; align-items: center; justify-content: center;
  font-weight: 600; font-size: 15px; }
.channel-item.active .channel-hash { background: rgba(255,255,255,.2); color: #fff; }
.channel-body { flex: 1; min-width: 0; }
.channel-row1 { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.channel-name { font-weight: 600; font-size: 14px; color: inherit;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.channel-meta { font-size: 11px; color: #9aa5ad; flex-shrink: 0; }
.channel-last { font-size: 12px; color: #8a949c; margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.new-channel { display: flex; gap: 8px; padding: 10px 12px;
  border-top: 1px solid #eef2f5; background: #fbfcfd; flex-shrink: 0; }
.new-channel input { flex: 1; min-width: 0; padding: 9px 12px;
  border: 1px solid #dfe5ea; border-radius: 8px;
  font-size: 13px; outline: none; transition: border-color .15s; }
.new-channel input:focus { border-color: #517da2; }
.new-channel button { width: 38px; height: 38px; border-radius: 8px;
  background: #517da2; color: #fff; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  transition: background .15s, transform .06s; }
.new-channel button:hover { background: #46708f; }
.new-channel button:active { transform: scale(.95); }

.chat { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.chat-header { height: 56px; flex-shrink: 0; background: #517da2; color: #fff;
  display: flex; align-items: center; padding: 0 14px; gap: 8px; }
#back-btn { display: none; width: 36px; height: 36px;
  align-items: center; justify-content: center;
  border-radius: 8px; color: #fff; }
#back-btn:hover { background: rgba(255,255,255,.14); }
.chat-title { display: flex; flex-direction: column; min-width: 0; }
#chat-name { font-weight: 600; font-size: 14px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-users { font-size: 11px; opacity: .85; }

.messages { flex: 1; min-height: 0; overflow-y: auto;
  padding: 16px 18px 8px;
  background:
    radial-gradient(circle at 15% 20%, rgba(81,125,162,.06) 0, transparent 40%),
    radial-gradient(circle at 80% 75%, rgba(81,125,162,.06) 0, transparent 40%),
    #e6ebee; }
.empty { text-align: center; color: #8a949c; margin-top: 60px;
  font-size: 13px; line-height: 1.6; }
.msg { display: flex; margin-bottom: 4px; }
.msg.in { justify-content: flex-start; }
.msg.out { justify-content: flex-end; }
.msg + .msg.same-user { margin-top: 2px; }
.msg.same-user .name { display: none; }
.bubble { max-width: 72%; padding: 7px 11px 5px;
  border-radius: 12px; background: #fff;
  box-shadow: 0 1px 2px rgba(0,0,0,.08);
  word-wrap: break-word; overflow-wrap: break-word; }
.msg.in .bubble { border-top-left-radius: 4px; }
.msg.out .bubble { background: #eeffde; border-top-right-radius: 4px; }
.msg.same-user.in .bubble,
.msg.same-user.out .bubble { border-top-left-radius: 12px; border-top-right-radius: 12px; }
.name { font-size: 12.5px; font-weight: 600; color: #517da2; margin-bottom: 2px; }
.text { white-space: pre-wrap; line-height: 1.35; font-size: 14px; }
.time { font-size: 10.5px; color: #9aa5ad; text-align: right;
  margin-top: 3px; margin-left: 12px; }
.msg.out .time { color: #7d8b7d; }

.composer { display: flex; align-items: flex-end; gap: 10px;
  padding: 12px 16px calc(12px + env(safe-area-inset-bottom, 0));
  background: #fff; border-top: 1px solid #dfe5ea; flex-shrink: 0; }
#msg-input { flex: 1; min-width: 0; padding: 11px 14px;
  background: #f2f5f8; border: 1px solid transparent; border-radius: 22px;
  font-size: 14px; line-height: 1.4;
  max-height: 130px; min-height: 44px; resize: none; outline: none; color: #222;
  transition: background .15s, border-color .15s; }
#msg-input:focus { background: #fff; border-color: #d8e0e7; }
.send-btn { width: 44px; height: 44px; flex-shrink: 0;
  border-radius: 50%; background: #517da2; color: #fff;
  display: flex; align-items: center; justify-content: center;
  transition: background .15s, transform .06s; }
.send-btn:hover { background: #46708f; }
.send-btn:active { transform: scale(.94); }
.send-btn:disabled { background: #b7c6d2; cursor: default; }

@media (max-width: 800px) {
  #app.visible { display: block; position: relative; overflow: hidden; }
  .sidebar { position: absolute; inset: 0; width: 100%; border-right: none; }
  .chat { position: absolute; inset: 0; background: #e6ebee;
    transform: translateX(100%); transition: transform .24s ease; z-index: 5; }
  #app.chat-open .chat { transform: translateX(0); }
  #back-btn { display: flex; }
  .bubble { max-width: 82%; }
}
.channel-list::-webkit-scrollbar,
.messages::-webkit-scrollbar { width: 8px; height: 8px; }
.channel-list::-webkit-scrollbar-thumb,
.messages::-webkit-scrollbar-thumb { background: rgba(0,0,0,.12); border-radius: 4px; }
.channel-list::-webkit-scrollbar-thumb:hover,
.messages::-webkit-scrollbar-thumb:hover { background: rgba(0,0,0,.22); }
</style>
</head>
<body>

<div id="auth-screen">
  <div class="auth-card">
    <div class="auth-head">
      <div class="auth-logo">sldchat</div>
      <div class="auth-sub">простой веб-чат</div>
    </div>
    <div class="auth-tabs">
      <button type="button" class="auth-tab active" data-mode="login">Вход</button>
      <button type="button" class="auth-tab" data-mode="register">Регистрация</button>
    </div>
    <div class="auth-body">
      <div class="auth-error" id="auth-error"></div>
      <input id="auth-user" placeholder="Имя пользователя" autocomplete="username"
             autocapitalize="none" spellcheck="false" maxlength="24">
      <input id="auth-pass" type="password" placeholder="Пароль"
             autocomplete="current-password" maxlength="128">
      <button type="button" class="auth-submit" id="auth-submit">Войти</button>
    </div>
  </div>
</div>

<div id="app">
  <aside class="sidebar">
    <div class="sidebar-header">
      <div class="me">
        <span class="avatar" id="me-avatar">?</span>
        <span id="me-name">…</span>
      </div>
      <button class="icon-btn" id="logout-btn" title="Выйти" aria-label="Выйти">
        <svg viewBox="0 0 24 24" width="20" height="20">
          <path fill="currentColor" d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/>
        </svg>
      </button>
    </div>
    <div class="search-wrap">
      <svg class="search-icon" viewBox="0 0 24 24" width="16" height="16">
        <path fill="currentColor" d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/>
      </svg>
      <input id="search" placeholder="Поиск каналов">
    </div>
    <div class="channel-list" id="channel-list"></div>
    <div class="new-channel">
      <input id="new-channel-name" placeholder="Новый канал" maxlength="32">
      <button id="new-channel-btn" title="Создать" aria-label="Создать канал">
        <svg viewBox="0 0 24 24" width="20" height="20">
          <path fill="currentColor" d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/>
        </svg>
      </button>
    </div>
  </aside>

  <main class="chat" id="chat">
    <div class="chat-header">
      <button class="icon-btn" id="back-btn" aria-label="Назад">
        <svg viewBox="0 0 24 24" width="24" height="24">
          <path fill="currentColor" d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>
        </svg>
      </button>
      <div class="chat-title">
        <span id="chat-name">Выбери канал</span>
        <span class="chat-users" id="chat-users"></span>
      </div>
    </div>
    <div class="messages" id="messages">
      <div class="empty">Выбери канал слева,<br>чтобы начать общение</div>
    </div>
    <div class="composer">
      <textarea id="msg-input" placeholder="Написать сообщение..." rows="1"
                enterkeyhint="send"></textarea>
      <button class="send-btn" id="send-btn" aria-label="Отправить">
        <svg viewBox="0 0 24 24" width="22" height="22">
          <path fill="currentColor" d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
        </svg>
      </button>
    </div>
  </main>
</div>

<script>
document.addEventListener('contextmenu', e => e.preventDefault());

const state = {
  token: localStorage.getItem('sld_token') || null,
  username: null,
  channels: [],
  currentChannel: null,
  ws: null,
  reconnectTimer: null,
};
let authMode = 'login';

const $ = id => document.getElementById(id);

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers['X-Auth-Token'] = state.token;
  if (opts.body && typeof opts.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.json();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

function hhmm(ts) {
  const t = new Date(ts);
  if (isNaN(t)) return '';
  return String(t.getHours()).padStart(2,'0') + ':' + String(t.getMinutes()).padStart(2,'0');
}

document.querySelectorAll('.auth-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    authMode = tab.dataset.mode;
    document.querySelectorAll('.auth-tab').forEach(t =>
      t.classList.toggle('active', t === tab));
    $('auth-submit').textContent = authMode === 'login' ? 'Войти' : 'Создать аккаунт';
    $('auth-error').textContent = '';
  });
});

$('auth-submit').addEventListener('click', doAuth);
$('auth-user').addEventListener('keydown', e => { if (e.key === 'Enter') $('auth-pass').focus(); });
$('auth-pass').addEventListener('keydown', e => { if (e.key === 'Enter') doAuth(); });

async function doAuth() {
  const u = $('auth-user').value.trim();
  const p = $('auth-pass').value;
  const errEl = $('auth-error');
  errEl.textContent = '';
  if (!u || !p) { errEl.textContent = 'Заполни все поля'; return; }

  const path = authMode === 'login' ? '/api/login' : '/api/register';
  try {
    const data = await api(path, { method: 'POST', body: { username: u, password: p } });
    state.token = data.token;
    state.username = data.username;
    localStorage.setItem('sld_token', data.token);
    await startApp();
  } catch (e) {
    errEl.textContent = e.message || 'Ошибка';
  }
}

function logout() {
  if (state.ws) {
    try { state.ws.onclose = null; state.ws.close(); } catch (e) {}
    state.ws = null;
  }
  state.token = null;
  state.username = null;
  state.currentChannel = null;
  localStorage.removeItem('sld_token');
  $('auth-screen').style.display = '';
  $('app').classList.remove('visible', 'chat-open');
  $('auth-pass').value = '';
  $('auth-submit').textContent = authMode === 'login' ? 'Войти' : 'Создать аккаунт';
  $('auth-error').textContent = '';
}

async function startApp() {
  try {
    const me = await api('/api/me');
    state.username = me.username;
  } catch (e) { logout(); return; }

  $('auth-screen').style.display = 'none';
  $('app').classList.add('visible');
  $('me-name').textContent = state.username;
  $('me-avatar').textContent = (state.username[0] || '?').toUpperCase();

  await loadChannels();
}

async function loadChannels() {
  try {
    const data = await api('/api/channels');
    state.channels = data.channels;
    renderChannels();
    if (state.currentChannel) {
      const ch = state.channels.find(c => c.id === state.currentChannel);
      if (ch) $('chat-users').textContent = ch.online + ' онлайн';
    }
  } catch (e) { /* ignore */ }
}

function renderChannels() {
  const list = $('channel-list');
  const filter = $('search').value.toLowerCase().trim();
  list.innerHTML = '';

  for (const c of state.channels) {
    if (filter && !c.name.toLowerCase().includes(filter)) continue;

    const el = document.createElement('div');
    el.className = 'channel-item' + (state.currentChannel === c.id ? ' active' : '');
    el.dataset.id = c.id;

    const last = c.last_message
      ? `<div class="channel-last">${escapeHtml(c.last_message.user)}: ${escapeHtml(c.last_message.text)}</div>`
      : '';

    el.innerHTML =
      `<div class="channel-hash">#</div>
       <div class="channel-body">
         <div class="channel-row1">
           <span class="channel-name">${escapeHtml(c.name)}</span>
           <span class="channel-meta">${c.online || 0}</span>
         </div>
         ${last}
       </div>`;
    el.addEventListener('click', () => openChannel(c.id));
    list.appendChild(el);
  }
}

async function openChannel(id) {
  state.currentChannel = id;
  $('app').classList.add('chat-open');
  renderChannels();

  const ch = state.channels.find(c => c.id === id);
  $('chat-name').textContent = ch ? ('# ' + ch.name) : ('# ' + id);
  $('chat-users').textContent = ch ? (ch.online + ' онлайн') : '';

  try {
    const data = await api('/api/channels/' + encodeURIComponent(id) + '/messages');
    renderMessages(data.messages);
  } catch (e) {
    renderMessages([]);
  }
  reconnectWs(id);
}

function renderMessages(msgs) {
  const box = $('messages');
  box.innerHTML = '';
  if (!msgs || !msgs.length) {
    box.innerHTML = '<div class="empty">Пока нет сообщений.<br>Напиши первым 👋</div>';
    return;
  }
  let prevUser = null;
  for (const m of msgs) {
    appendMessage(m, { skipScroll: true, prevUser });
    prevUser = m.user;
  }
  box.scrollTop = box.scrollHeight;
}

function appendMessage(m, opts = {}) {
  const box = $('messages');
  const empty = box.querySelector('.empty');
  if (empty) empty.remove();

  const out = m.user === state.username;
  const sameUser = opts.prevUser === m.user;

  const div = document.createElement('div');
  div.className = 'msg ' + (out ? 'out' : 'in') + (sameUser ? ' same-user' : '');
  div.dataset.ts = m.ts || '';
  div.dataset.user = m.user;

  div.innerHTML =
    `<div class="bubble">
       ${out ? '' : `<div class="name">${escapeHtml(m.user)}</div>`}
       <div class="text">${escapeHtml(m.text)}</div>
       <div class="time">${hhmm(m.ts)}</div>
     </div>`;

  box.appendChild(div);
  if (!opts.skipScroll) box.scrollTop = box.scrollHeight;
}

function reconnectWs(channelId) {
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
  if (state.ws) {
    try { state.ws.onclose = null; state.ws.close(); } catch (e) {}
    state.ws = null;
  }

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws?token=${encodeURIComponent(state.token)}&channel=${encodeURIComponent(channelId)}`;
  const ws = new WebSocket(url);
  state.ws = ws;

  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }

    if (data.type === 'message') {
      const m = data.message;
      const ch = state.channels.find(c => c.id === m.channel);
      if (ch) ch.last_message = { user: m.user, text: m.text, ts: m.ts };

      if (m.channel === state.currentChannel) {
        const box = $('messages');
        const last = box.querySelector('.msg:last-child');
        const prevUser = last ? last.dataset.user : null;
        appendMessage(m, { prevUser });
      }
      renderChannels();
    } else if (data.type === 'presence') {
      const counts = data.counts || {};
      for (const c of state.channels) c.online = counts[c.id] || 0;
      renderChannels();
      if (state.currentChannel) {
        const c = state.channels.find(x => x.id === state.currentChannel);
        if (c) $('chat-users').textContent = c.online + ' онлайн';
      }
    } else if (data.type === 'channels_changed') {
      loadChannels();
    }
  };

  ws.onclose = () => {
    if (state.currentChannel === channelId) {
      state.reconnectTimer = setTimeout(() => {
        if (state.currentChannel === channelId) reconnectWs(channelId);
      }, 1500);
    }
  };
}

function sendMessage() {
  const input = $('msg-input');
  const text = input.value.trim();
  if (!text) return;
  if (!state.ws || state.ws.readyState !== 1) return;
  state.ws.send(JSON.stringify({ type: 'message', text }));
  input.value = '';
  input.style.height = 'auto';
}

$('send-btn').addEventListener('click', sendMessage);
$('msg-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
$('msg-input').addEventListener('input', e => {
  e.target.style.height = 'auto';
  e.target.style.height = Math.min(e.target.scrollHeight, 130) + 'px';
});

$('logout-btn').addEventListener('click', logout);
$('back-btn').addEventListener('click', () => $('app').classList.remove('chat-open'));
$('search').addEventListener('input', renderChannels);

$('new-channel-btn').addEventListener('click', async () => {
  const name = $('new-channel-name').value.trim();
  if (!name) return;
  try {
    await api('/api/channels', { method: 'POST', body: { name } });
    $('new-channel-name').value = '';
    await loadChannels();
  } catch (e) { alert(e.message); }
});
$('new-channel-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('new-channel-btn').click();
});

if (state.token) startApp();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


# ============================================================
#  ТОЧКА ВХОДА
# ============================================================
if __name__ == "__main__":
    # Запускается только при `python main.py`. При `fastapi run` / `uvicorn`
    # этот блок не выполняется — их раннер сам поднимает приложение.
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
