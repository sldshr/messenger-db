"""
MESSENGER SERVER — Pure ASGI, optimized for fast startup & low RAM (512MB)
No web panel, no stats tracking, no reply_to.
"""
import asyncio, hashlib, json, os, re, time as _time
from datetime import datetime, timezone
from urllib.parse import parse_qs

# ===== CONFIG =====
SUPABASE_URL = os.environ.get("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "YOUR_SUPABASE_ANON_KEY")
# ==================

_sb = None
_sb_lock = asyncio.Lock()

# ===== PSUTIL (optional, for /api/system) =====
_psutil = None
def _get_psutil():
    global _psutil
    if _psutil is None:
        try:
            import psutil as _p
            _psutil = _p
        except ImportError:
            pass
    return _psutil

async def get_sb():
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
    """Run blocking Supabase call in thread — prevents event loop freeze."""
    return await asyncio.to_thread(fn)

# ===== HANDLER =====

async def handle(method, path, body, qs):
    data = {}
    if body:
        try: data = json.loads(body)
        except: pass
    def gk(k):
        v = qs.get(k, [None])[0]
        return v if v else data.get(k)

    # --- Fast routes (no DB) ---
    if method == "GET" and path == "/api/ping":
        return ok({"pong": True, "time": now_iso()})

    # --- System info (RAM / CPU) ---
    if method == "GET" and path == "/api/system":
        p = _get_psutil()
        if p is None:
            return ok({"ram_percent": 0, "ram_used_mb": 0, "ram_total_mb": 0, "cpu_percent": 0, "available": False})
        vm = p.virtual_memory()
        return ok({"ram_percent": round(vm.percent, 1),
                    "ram_used_mb": round(vm.used / 1048576),
                    "ram_total_mb": round(vm.total / 1048576),
                    "cpu_percent": round(p.cpu_percent(interval=0.1), 1),
                    "available": True})

    # Lazy Supabase init — only for DB routes (fast routes above need no DB)
    sb = await get_sb()

    # --- Auth ---
    if method == "POST" and path == "/api/register":
        u = (data.get("username") or "").strip()
        p = data.get("password") or ""
        if not u or not p: return er("Username and password required")
        if len(u) < 2 or len(u) > 30: return er("Username: 2-30 chars")
        if len(p) < 4: return er("Password: min 4 chars")
        ex = await sb_query(lambda: sb.table("users").select("id").eq("username", u).execute())
        if ex.data: return er("Username already taken")
        res = await sb_query(lambda: sb.table("users").insert({"username": u, "password_hash": hp(p)}).execute())
        if res.data: return ok({"user_id": res.data[0]["id"], "username": u})
        return er("Failed", 500)

    if method == "POST" and path == "/api/login":
        u = (data.get("username") or "").strip()
        p = data.get("password") or ""
        if not u or not p: return er("Username and password required")
        res = await sb_query(lambda: sb.table("users").select("id,username").eq("username", u).eq("password_hash", hp(p)).execute())
        if not res.data: return er("Invalid credentials", 401)
        return ok({"user_id": res.data[0]["id"], "username": res.data[0]["username"]})

    # --- Channels ---
    if path == "/api/channels" and method == "GET":
        u = gk("user_id")
        if not u: return er("user_id required", 401)
        chs_r = await sb_query(lambda: sb.table("channels").select("*").order("created_at", desc=False).execute())
        chs = chs_r.data or []
        my_r = await sb_query(lambda: sb.table("channel_members").select("channel_id").eq("user_id", u).execute())
        my_ids = {m["channel_id"] for m in (my_r.data or [])}
        # AUTO-JOIN: user is automatically added to every public channel
        need_join = [ch["id"] for ch in chs if ch["id"] not in my_ids and ch.get("is_public", True)]
        for c in need_join:
            await sb_query(lambda cid=c: sb.table("channel_members").insert({"channel_id": cid, "user_id": u}).execute())
            my_ids.add(c)
        # Single query: get ALL member counts at once (no N+1)
        all_m = await sb_query(lambda: sb.table("channel_members").select("channel_id").execute())
        counts = {}
        for m in (all_m.data or []):
            cid = m["channel_id"]
            counts[cid] = counts.get(cid, 0) + 1
        res = []
        for ch in chs:
            res.append({"id": ch["id"], "name": ch["name"], "description": ch.get("description",""),
                         "created_by": ch.get("created_by"), "created_at": ch.get("created_at"),
                         "channel_type": ch.get("channel_type", "user"),
                         "member_count": counts.get(ch["id"], 0), "is_member": ch["id"] in my_ids})
        return ok(res)

    if path == "/api/channels" and method == "POST":
        u = gk("user_id")
        if not u: return er("user_id required", 401)
        name = (data.get("name") or "").strip()
        desc = (data.get("description") or "").strip()
        ctype = data.get("channel_type", "user")
        if not name: return er("Channel name required")
        res = await sb_query(lambda: sb.table("channels").insert({"name": name, "description": desc, "created_by": u, "channel_type": ctype}).execute())
        if res.data:
            ch = res.data[0]
            await sb_query(lambda: sb.table("channel_members").insert({"channel_id": ch["id"], "user_id": u, "role": "admin"}).execute())
            return ok({"id": ch["id"], "name": ch["name"]})
        return er("Failed", 500)

    # --- Channel sub-routes ---
    m = re.match(r"^/api/channels/([^/]+)(/(join|leave|members|messages))?$", path)
    if m:
        cid, action = m.group(1), m.group(3)

        if method == "POST" and action == "join":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            ch = await sb_query(lambda: sb.table("channels").select("id").eq("id", cid).execute())
            if not ch.data: return er("Not found", 404)
            ex = await sb_query(lambda: sb.table("channel_members").select("channel_id").eq("channel_id", cid).eq("user_id", u).execute())
            if ex.data: return ok({"message": "Already a member"})
            await sb_query(lambda: sb.table("channel_members").insert({"channel_id": cid, "user_id": u}).execute())
            return ok({"message": "Joined"})

        if method == "POST" and action == "leave":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            await sb_query(lambda: sb.table("channel_members").delete().eq("channel_id", cid).eq("user_id", u).execute())
            return ok({"message": "Left"})

        if method == "GET" and action == "members":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            res = await sb_query(lambda: sb.table("channel_members").select("user_id,role").eq("channel_id", cid).execute())
            result = []
            for mm in (res.data or []):
                ur = await sb_query(lambda uid=mm["user_id"]: sb.table("users").select("username").eq("id", uid).execute())
                un = ur.data[0]["username"] if ur.data else "?"
                result.append({"user_id": mm["user_id"], "username": un, "role": mm.get("role","member")})
            return ok(result)

        if method == "GET" and action == "messages":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            limit = min(int(qs.get("limit", ["200"])[0]), 200)
            before = qs.get("before", [None])[0]
            def _get_msgs():
                q = sb.table("messages").select("id,user_id,content,created_at").eq("channel_id", cid).order("created_at", desc=True).limit(limit)
                if before: q = q.lt("created_at", before)
                return q.execute()
            msgs_r = await sb_query(_get_msgs)
            msgs = list(reversed(msgs_r.data or []))
            uids = list({mm["user_id"] for mm in msgs})
            uc = {}
            if uids:
                def _get_users():
                    return sb.table("users").select("id,username").in_("id", uids).execute()
                users_r = await sb_query(_get_users)
                for usr in (users_r.data or []):
                    uc[usr["id"]] = usr["username"]
            result = []
            for mm in msgs:
                item = {"id": mm["id"], "user_id": mm["user_id"],
                        "username": uc.get(mm["user_id"], "?"),
                        "content": mm["content"], "created_at": mm["created_at"]}
                result.append(item)
            return ok(result)

        if method == "POST" and action == "messages":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            content = (data.get("content") or "").strip()
            if not content: return er("Content required")
            if len(content) > 5000: return er("Too long")
            member = await sb_query(lambda: sb.table("channel_members").select("channel_id").eq("channel_id", cid).eq("user_id", u).execute())
            if not member.data: return er("Not a member", 403)
            msg_data = {"channel_id": cid, "user_id": u, "content": content}
            res = await sb_query(lambda: sb.table("messages").insert(msg_data).execute())
            if res.data:
                mm = res.data[0]
                return ok({"id": mm["id"], "created_at": mm["created_at"]})
            return er("Failed", 500)

    # --- Message edit/delete: PATCH/DELETE /api/channels/{cid}/messages/{mid} ---
    m2 = re.match(r"^/api/channels/([^/]+)/messages/([^/]+)$", path)
    if m2:
        cid, mid = m2.group(1), m2.group(2)

        if method == "PATCH":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            content = (data.get("content") or "").strip()
            if not content: return er("Content required")
            if len(content) > 5000: return er("Too long")
            msg_r = await sb_query(lambda: sb.table("messages").select("id,user_id").eq("id", mid).execute())
            if not msg_r.data: return er("Message not found", 404)
            if msg_r.data[0]["user_id"] != u: return er("Not your message", 403)
            await sb_query(lambda: sb.table("messages").update({"content": content}).eq("id", mid).execute())
            return ok({"id": mid})

        if method == "DELETE":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            msg_r = await sb_query(lambda: sb.table("messages").select("id,user_id").eq("id", mid).execute())
            if not msg_r.data: return er("Message not found", 404)
            if msg_r.data[0]["user_id"] != u: return er("Not your message", 403)
            await sb_query(lambda: sb.table("messages").delete().eq("id", mid).execute())
            return ok({"deleted": mid})

    # All users
    if path == "/api/users" and method == "GET":
        res = await sb_query(lambda: sb.table("users").select("id,username").order("username").execute())
        return ok(res.data or [])

    # Edit profile
    m3 = re.match(r"^/api/users/([^/]+)$", path)
    if m3 and method == "PATCH":
        uid_target = m3.group(1)
        u = gk("user_id")
        if not u: return er("user_id required", 401)
        if u != uid_target: return er("Cannot edit other users", 403)
        new_name = (data.get("username") or "").strip()
        new_pass = (data.get("password") or "").strip()
        updates = {}
        if new_name:
            if len(new_name) < 2 or len(new_name) > 30: return er("Username: 2-30 chars")
            ex = await sb_query(lambda: sb.table("users").select("id").eq("username", new_name).execute())
            if ex.data and ex.data[0]["id"] != u: return er("Username taken")
            updates["username"] = new_name
        if new_pass:
            if len(new_pass) < 4: return er("Password: min 4 chars")
            updates["password_hash"] = hp(new_pass)
        if not updates: return er("Nothing to update")
        await sb_query(lambda: sb.table("users").update(updates).eq("id", u).execute())
        return ok({"updated": True, "username": updates.get("username")})

    return er("Not found", 404)


# ===== ASGI APP =====

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
            handle(scope["method"], scope["path"], body, qs), timeout=30
        )
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
