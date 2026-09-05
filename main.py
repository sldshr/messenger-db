"""
JAVSER — лёгкий ASGI-сервер для чат-клиента.
"""

import asyncio, hashlib, json, os, re
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs

# ============================================================
# КОНФИГУРАЦИЯ СЕРВЕРА
# ============================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "YOUR_SUPABASE_ANON_KEY")

# Название и описание сервера
SERVER_NAME = os.environ.get("SERVER_NAME", "")
SERVER_DESCRIPTION = os.environ.get("SERVER_DESCRIPTION", "")
# ============================================================

_sb = None
_sb_lock = asyncio.Lock()

# Очереди сообщений для long-polling: channel_id -> set(Queue)
_long_poll_queues = {}


async def get_sb():
    """Ленивая инициализация Supabase — только для БД-маршрутов."""
    global _sb
    if _sb is None:
        async with _sb_lock:
            if _sb is None:
                from supabase import create_client
                _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb


def hp(p): return hashlib.sha256(p.encode()).hexdigest()
def now_iso(): return datetime.now(timezone.utc).isoformat()
def ok(d=None): return json.dumps({"ok": True, "data": d}, ensure_ascii=False), 200
def er(m, c=400): return json.dumps({"ok": False, "error": m}, ensure_ascii=False), c


async def sb_query(fn):
    """Запуск блокирующего вызова Supabase в потоке."""
    return await asyncio.to_thread(fn)


# ============================================================
# LONG-POLLING (мгновенные сообщения)
# ============================================================

def _queue_for(channel_id):
    """Очередь уведомлений для канала."""
    if channel_id not in _long_poll_queues:
        _long_poll_queues[channel_id] = set()
    return _long_poll_queues[channel_id]


async def notify_channel(channel_id):
    """Пробудить всех клиентов, ожидающих новые сообщения в канале."""
    qs = _queue_for(channel_id)
    for q in list(qs):
        await q.put("new")


async def wait_for_message(channel_id, timeout=25.0):
    """Ожидание нового сообщения (long-poll, максимальное время 25 сек)."""
    q = asyncio.Queue()
    _queue_for(channel_id).add(q)
    try:
        try:
            await asyncio.wait_for(q.get(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            # Таймаут: могли появиться сообщения — перепроверим
            return await _has_new_messages(channel_id)
    finally:
        _queue_for(channel_id).discard(q)


async def _has_new_messages(channel_id):
    """Быстрая проверка наличия сообщений в канале."""
    try:
        sb = await get_sb()
        r = await sb_query(lambda: sb.table("messages")
            .select("id").eq("channel_id", channel_id).limit(1).execute())
        return bool(r.data)
    except Exception:
        return False


# ============================================================
# ОСНОВНОЙ ОБРАБОТЧИК
# ============================================================

async def handle(method, path, body, qs):
    data = {}
    if body:
        try: data = json.loads(body)
        except: pass

    def gk(k):
        v = qs.get(k, [None])[0]
        return v if v else data.get(k)

    # ---- Быстрые маршруты (без БД) ----
    if method == "GET" and path == "/api/ping":
        return ok({"pong": True, "time": now_iso()})

    # Информация о сервере (для кнопки "Check")
    if method == "GET" and path == "/api/server-info":
        sb = await get_sb()
        users_r = await sb_query(lambda: sb.table("users").select("id").execute())
        member_count = len(users_r.data or []) if users_r.data else 0
        return ok({
            "name": SERVER_NAME,
            "description": SERVER_DESCRIPTION,
            "member_count": member_count,
            "online": True,
        })

    # Проверка сессии после перезапуска сервера
    if method == "POST" and path == "/api/session/validate":
        sid = (data.get("session_id") or "").strip()
        if not sid: return er("session_id required", 401)
        sb = await get_sb()
        r = await sb_query(lambda: sb.table("sessions")
            .select("id,user_id").eq("id", sid).execute())
        if not r.data: return er("Session invalid", 401)
        await sb_query(lambda: sb.table("sessions")
            .update({"expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()})
            .eq("id", sid).execute())
        return ok({"valid": True, "user_id": r.data[0]["user_id"]})

    # Long-poll нового сообщения в канале
    m_lp = re.match(r"^/api/live/channel/([^/]+)/new$", path)
    if m_lp and method == "GET":
        channel_id = m_lp.group(1)
        got = await wait_for_message(channel_id)
        return ok({"new": got})

    # ============================================================
    # АВТОРИЗАЦИЯ
    # ============================================================
    sb = await get_sb()

    if method == "POST" and path == "/api/register":
        u = (data.get("username") or "").strip()
        p = data.get("password") or ""
        desc = (data.get("description") or "").strip()
        if not u or not p: return er("Username and password required")
        if len(u) < 2 or len(u) > 30: return er("Username: 2-30 chars")
        if len(p) < 4: return er("Password: min 4 chars")
        ex = await sb_query(lambda: sb.table("users").select("id").eq("username", u).execute())
        if ex.data: return er("Username already taken")
        res = await sb_query(lambda: sb.table("users")
            .insert({"username": u, "password_hash": hp(p), "description": desc}).execute())
        if not res.data: return er("Failed", 500)
        user = res.data[0]
        sess = await sb_query(lambda: sb.table("sessions")
            .insert({"user_id": user["id"]}).execute())
        sid = sess.data[0]["id"] if sess.data else ""
        return ok({"user_id": user["id"], "username": u, "session_id": sid})

    if method == "POST" and path == "/api/login":
        u = (data.get("username") or "").strip()
        p = data.get("password") or ""
        if not u or not p: return er("Username and password required")
        res = await sb_query(lambda: sb.table("users")
            .select("id,username,description,avatar_base64")
            .eq("username", u).eq("password_hash", hp(p)).execute())
        if not res.data: return er("Invalid credentials", 401)
        user = res.data[0]
        sess = await sb_query(lambda: sb.table("sessions")
            .insert({"user_id": user["id"]}).execute())
        sid = sess.data[0]["id"] if sess.data else ""
        return ok({
            "user_id": user["id"], "username": user["username"],
            "description": user.get("description", ""),
            "avatar_base64": user.get("avatar_base64", ""),
            "session_id": sid,
        })

    # ============================================================
    # КАНАЛЫ
    # ============================================================
    if path == "/api/channels" and method == "GET":
        u = gk("user_id")
        if not u: return er("user_id required", 401)
        chs_r = await sb_query(lambda: sb.table("channels").select("*").order("created_at", desc=False).execute())
        chs = chs_r.data or []
        my_r = await sb_query(lambda: sb.table("channel_members").select("channel_id").eq("user_id", u).execute())
        my_ids = {m["channel_id"] for m in (my_r.data or [])}
        # Автовход: пользователь входит во все публичные каналы
        need_join = [ch["id"] for ch in chs if ch["id"] not in my_ids and ch.get("is_public", True)]
        for c in need_join:
            await sb_query(lambda cid=c: sb.table("channel_members").insert({"channel_id": cid, "user_id": u}).execute())
            my_ids.add(c)
        counts_r = await sb_query(lambda: sb.table("channel_members").select("channel_id").execute())
        counts = {}
        for m in (counts_r.data or []):
            cid = m["channel_id"]
            counts[cid] = counts.get(cid, 0) + 1
        res = []
        for ch in chs:
            res.append({
                "id": ch["id"], "name": ch["name"],
                "description": ch.get("description", ""),
                "avatar_base64": ch.get("avatar_base64", ""),
                "member_count": counts.get(ch["id"], 0),
                "is_member": ch["id"] in my_ids,
            })
        return ok(res)

    # Создание канала: только имя, без выбора типа
    if path == "/api/channels" and method == "POST":
        u = gk("user_id")
        if not u: return er("user_id required", 401)
        name = (data.get("name") or "").strip()
        if not name: return er("Name required")
        if len(name) > 50: return er("Name too long")
        desc = (data.get("description") or "").strip()
        av = (data.get("avatar_base64") or "").strip()
        res = await sb_query(lambda: sb.table("channels")
            .insert({"name": name, "description": desc, "avatar_base64": av, "created_by": u}).execute())
        if not res.data: return er("Failed", 500)
        ch = res.data[0]
        await sb_query(lambda: sb.table("channel_members")
            .insert({"channel_id": ch["id"], "user_id": u}).execute())
        return ok({"id": ch["id"]})

    # Участники канала
    m_mem = re.match(r"^/api/channels/([^/]+)/members$", path)
    if m_mem and method == "GET":
        cid = m_mem.group(1)
        mem_r = await sb_query(lambda: sb.table("channel_members")
            .select("user_id").eq("channel_id", cid).execute())
        uids = [m["user_id"] for m in (mem_r.data or [])]
        if not uids: return ok([])
        us_r = await sb_query(lambda: sb.table("users")
            .select("id,username,avatar_base64,description").in_("id", uids).execute())
        res = [{
            "user_id": us["id"], "username": us["username"],
            "avatar_base64": us.get("avatar_base64", ""),
            "description": us.get("description", ""),
        } for us in (us_r.data or [])]
        return ok(res)

    # Обновление канала (описание/аватарка)
    m_ch = re.match(r"^/api/channels/([^/]+)$", path)
    if m_ch and method == "PATCH":
        cid = m_ch.group(1)
        u = gk("user_id")
        if not u: return er("user_id required", 401)
        ch_r = await sb_query(lambda: sb.table("channels").select("created_by").eq("id", cid).execute())
        if not ch_r.data: return er("Channel not found", 404)
        if ch_r.data[0].get("created_by") != u: return er("No permission", 403)
        updates = {}
        if "description" in data: updates["description"] = (data.get("description") or "").strip()
        if "avatar_base64" in data: updates["avatar_base64"] = (data.get("avatar_base64") or "").strip()
        if not updates: return er("Nothing to update")
        await sb_query(lambda: sb.table("channels").update(updates).eq("id", cid).execute())
        return ok({"updated": True})


    # ============================================================
    # СООБЩЕНИЯ
    # ============================================================
    m_msgs = re.match(r"^/api/channels/([^/]+)/messages$", path)
    if m_msgs:
        cid = m_msgs.group(1)
        if method == "GET":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            msgs_r = await sb_query(lambda: sb.table("messages")
                .select("id,user_id,content,created_at")
                .eq("channel_id", cid).order("created_at").limit(500).execute())
            msgs = msgs_r.data or []
            uids = list({m["user_id"] for m in msgs})
            users_map = {}
            if uids:
                us_r = await sb_query(lambda: sb.table("users")
                    .select("id,username,avatar_base64").in_("id", uids).execute())
                for us in (us_r.data or []):
                    users_map[us["id"]] = us
            res = []
            for m in msgs:
                author = users_map.get(m["user_id"], {})
                res.append({
                    "id": m["id"],
                    "user_id": m["user_id"],
                    "username": author.get("username", "?"),
                    "avatar_base64": author.get("avatar_base64", ""),
                    "content": m["content"],
                    "created_at": m["created_at"],
                })
            return ok(res)

        if method == "POST":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            content = (data.get("content") or "").strip()
            if not content: return er("Content required")
            if len(content) > 5000: return er("Too long")
            member = await sb_query(lambda: sb.table("channel_members")
                .select("channel_id").eq("channel_id", cid).eq("user_id", u).execute())
            if not member.data: return er("Not a member", 403)
            r = await sb_query(lambda: sb.table("messages")
                .insert({"channel_id": cid, "user_id": u, "content": content}).execute())
            # Мгновенная доставка всем ожидающим
            await notify_channel(cid)
            return ok({"id": r.data[0]["id"]})

    # Правка/удаление сообщения
    m_msg = re.match(r"^/api/channels/([^/]+)/messages/([^/]+)$", path)
    if m_msg:
        cid, mid = m_msg.group(1), m_msg.group(2)
        if method == "PATCH":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            content = (data.get("content") or "").strip()
            if not content: return er("Content required")
            msg_r = await sb_query(lambda: sb.table("messages").select("id,user_id").eq("id", mid).execute())
            if not msg_r.data: return er("Not found", 404)
            if msg_r.data[0]["user_id"] != u: return er("Not your message", 403)
            await sb_query(lambda: sb.table("messages").update({"content": content}).eq("id", mid).execute())
            await notify_channel(cid)
            return ok({"id": mid})
        if method == "DELETE":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            msg_r = await sb_query(lambda: sb.table("messages").select("id,user_id").eq("id", mid).execute())
            if not msg_r.data: return er("Not found", 404)
            if msg_r.data[0]["user_id"] != u: return er("Not your message", 403)
            await sb_query(lambda: sb.table("messages").delete().eq("id", mid).execute())
            await notify_channel(cid)
            return ok({"deleted": mid})


    # ============================================================
    # ПОЛЬЗОВАТЕЛИ
    # ============================================================
    if path == "/api/users" and method == "GET":
        res = await sb_query(lambda: sb.table("users").select("id,username,avatar_base64,description").order("username").execute())
        return ok(res.data or [])

    # Пользователь по id
    m_user = re.match(r"^/api/users/([^/]+)$", path)
    if m_user and method == "GET":
        uid = m_user.group(1)
        res = await sb_query(lambda: sb.table("users")
            .select("id,username,description,avatar_base64").eq("id", uid).execute())
        if not res.data: return er("Not found", 404)
        us = res.data[0]
        return ok({"id": us["id"], "username": us["username"],
                   "description": us.get("description", ""),
                   "avatar_base64": us.get("avatar_base64", "")})

    # Редактирование профиля (имя, пароль, описание, аватарка)
    if m_user and method == "PATCH":
        uid_target = m_user.group(1)
        u = gk("user_id")
        if not u: return er("user_id required", 401)
        if u != uid_target: return er("Cannot edit other users", 403)
        new_name = (data.get("username") or "").strip()
        new_pass = (data.get("password") or "").strip()
        new_desc = data.get("description")
        new_av = data.get("avatar_base64")
        updates = {}
        if new_name:
            if len(new_name) < 2 or len(new_name) > 30: return er("Username: 2-30 chars")
            ex = await sb_query(lambda: sb.table("users").select("id").eq("username", new_name).execute())
            if ex.data and ex.data[0]["id"] != u: return er("Username taken")
            updates["username"] = new_name
        if new_pass:
            if len(new_pass) < 4: return er("Password: min 4 chars")
            updates["password_hash"] = hp(new_pass)
        if new_desc is not None:
            updates["description"] = str(new_desc)[:500]
        if new_av is not None:
            updates["avatar_base64"] = str(new_av)
        if not updates: return er("Nothing to update")
        await sb_query(lambda: sb.table("users").update(updates).eq("id", u).execute())
        return ok({"updated": True, "username": updates.get("username")})

    return er("Not found", 404)


# ============================================================
# ASGI APP
# ============================================================

HEADERS = [
    [b"content-type", b"application/json"],
    [b"access-control-allow-origin", b"*"],
    [b"access-control-allow-methods", b"GET,POST,PATCH,DELETE,OPTIONS"],
    [b"access-control-allow-headers", b"content-type"],
]


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    if scope["method"] == "OPTIONS":
        await send({"type": "http.response.start", "status": 204, "headers": HEADERS})
        await send({"type": "http.response.body", "body": b""})
        return
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    qs = parse_qs(scope.get("query_string", b"").decode())
    try:
        resp, status = await asyncio.wait_for(
            handle(scope["method"], scope["path"], body, qs), timeout=35)
    except asyncio.TimeoutError:
        resp, status = json.dumps({"ok": False, "error": "timeout"}), 504
    except Exception as ex:
        resp, status = json.dumps({"ok": False, "error": str(ex)}), 500
    if isinstance(resp, str):
        resp = resp.encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": HEADERS})
    await send({"type": "http.response.body", "body": resp})


application = app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)

