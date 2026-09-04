"""
MESSENGER SERVER — Pure ASGI (uvicorn compatible)
"""
import hashlib, json, os, re
from datetime import datetime, timezone
from urllib.parse import parse_qs

# ===== CONFIG =====
SUPABASE_URL = os.environ.get("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "YOUR_SUPABASE_ANON_KEY")
# ==================

_sb = None
def get_sb():
    global _sb
    if _sb is None:
        from supabase import create_client
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb

def hp(p): return hashlib.sha256(p.encode()).hexdigest()
def now_iso(): return datetime.now(timezone.utc).isoformat()

def ok(d=None):
    return json.dumps({"ok": True, "data": d})

def er(m, c=400):
    return json.dumps({"ok": False, "error": m}), c

# ===== ROUTING =====

async def handle_request(method, path, body, qs):
    """Main router. Returns (body_str, status_code)."""
    data = {}
    if body:
        try: data = json.loads(body)
        except: pass

    def gk(key):
        """Get from query or body."""
        v = qs.get(key, [None])[0]
        if v: return v
        return data.get(key)

    # Health
    if method == "GET" and path == "/":
        return ok({"status": "running", "service": "messenger-server"}), 200

    # Ping
    if method == "GET" and path == "/api/ping":
        return ok({"pong": True, "time": now_iso()}), 200

    # Register
    if method == "POST" and path == "/api/register":
        u = (data.get("username") or "").strip()
        p = data.get("password") or ""
        if not u or not p: return er("Username and password required")
        if len(u) < 2 or len(u) > 30: return er("Username: 2-30 chars")
        if len(p) < 4: return er("Password: min 4 chars")
        ex = get_sb().table("users").select("id").eq("username", u).execute()
        if ex.data: return er("Username already taken")
        res = get_sb().table("users").insert({"username": u, "password_hash": hp(p)}).execute()
        if res.data:
            r = res.data[0]
            return ok({"user_id": r["id"], "username": r["username"]}), 200
        return er("Failed to create user", 500)

    # Login
    if method == "POST" and path == "/api/login":
        u = (data.get("username") or "").strip()
        p = data.get("password") or ""
        if not u or not p: return er("Username and password required")
        res = get_sb().table("users").select("id,username").eq("username", u).eq("password_hash", hp(p)).execute()
        if not res.data: return er("Invalid credentials", 401)
        r = res.data[0]
        return ok({"user_id": r["id"], "username": r["username"]}), 200

    # === CHANNELS ===

    if path == "/api/channels" and method == "GET":
        u = gk("user_id")
        if not u: return er("user_id required", 401)
        chs = get_sb().table("channels").select("*").order("created_at", desc=False).execute().data or []
        my = get_sb().table("channel_members").select("channel_id").eq("user_id", u).execute().data or []
        my_ids = {m["channel_id"] for m in my}
        res = []
        for ch in chs:
            cnt = get_sb().table("channel_members").select("user_id", count="exact").eq("channel_id", ch["id"]).execute()
            mc = len(cnt.data) if cnt.data else 0
            res.append({"id": ch["id"], "name": ch["name"], "description": ch.get("description",""),
                         "created_by": ch.get("created_by"), "created_at": ch.get("created_at"),
                         "member_count": mc, "is_member": ch["id"] in my_ids})
        return ok(res), 200

    if path == "/api/channels" and method == "POST":
        u = gk("user_id")
        if not u: return er("user_id required", 401)
        name = (data.get("name") or "").strip()
        desc = (data.get("description") or "").strip()
        if not name: return er("Channel name required")
        res = get_sb().table("channels").insert({"name": name, "description": desc, "created_by": u}).execute()
        if res.data:
            ch = res.data[0]
            get_sb().table("channel_members").insert({"channel_id": ch["id"], "user_id": u, "role": "admin"}).execute()
            return ok({"id": ch["id"], "name": ch["name"]}), 200
        return er("Failed", 500)

    # === CHANNEL SUB-ROUTES ===
    m = re.match(r"^/api/channels/([^/]+)(/(join|leave|members|messages))?$", path)
    if m:
        cid = m.group(1)
        action = m.group(3)

        if method == "POST" and action == "join":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            ch = get_sb().table("channels").select("id").eq("id", cid).execute()
            if not ch.data: return er("Not found", 404)
            ex = get_sb().table("channel_members").select("channel_id").eq("channel_id", cid).eq("user_id", u).execute()
            if ex.data: return ok({"message": "Already a member"}), 200
            get_sb().table("channel_members").insert({"channel_id": cid, "user_id": u}).execute()
            return ok({"message": "Joined"}), 200

        if method == "POST" and action == "leave":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            get_sb().table("channel_members").delete().eq("channel_id", cid).eq("user_id", u).execute()
            return ok({"message": "Left"}), 200

        if method == "GET" and action == "members":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            res = get_sb().table("channel_members").select("user_id,role,joined_at").eq("channel_id", cid).execute().data or []
            result = []
            for mm in res:
                ur = get_sb().table("users").select("username").eq("id", mm["user_id"]).execute()
                un = ur.data[0]["username"] if ur.data else "unknown"
                result.append({"user_id": mm["user_id"], "username": un, "role": mm.get("role","member")})
            return ok(result), 200

        if method == "GET" and action == "messages":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            limit = min(int(qs.get("limit", ["200"])[0]), 500)
            before = qs.get("before", [None])[0]
            q = get_sb().table("messages").select("id,user_id,content,created_at").eq("channel_id", cid).order("created_at", desc=True).limit(limit)
            if before: q = q.lt("created_at", before)
            msgs = list(reversed(q.execute().data or []))
            uc = {}
            result = []
            for mm in msgs:
                uid = mm["user_id"]
                if uid not in uc:
                    ur = get_sb().table("users").select("username").eq("id", uid).execute()
                    uc[uid] = ur.data[0]["username"] if ur.data else "unknown"
                result.append({"id": mm["id"], "user_id": uid, "username": uc[uid], "content": mm["content"], "created_at": mm["created_at"]})
            return ok(result), 200

        if method == "POST" and action == "messages":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            content = (data.get("content") or "").strip()
            if not content: return er("Content required")
            if len(content) > 5000: return er("Too long")
            member = get_sb().table("channel_members").select("channel_id").eq("channel_id", cid).eq("user_id", u).execute()
            if not member.data: return er("Not a member", 403)
            res = get_sb().table("messages").insert({"channel_id": cid, "user_id": u, "content": content}).execute()
            if res.data:
                mm = res.data[0]
                return ok({"id": mm["id"], "created_at": mm["created_at"]}), 200
            return er("Failed", 500)

    # Users search
    if path == "/api/users/search" and method == "GET":
        q = (qs.get("q", [""])[0]).strip()
        if not q: return ok([]), 200
        res = get_sb().table("users").select("id,username").ilike("username", f"%{q}%").limit(20).execute()
        return ok(res.data or []), 200

    return er("Not found", 404)


# ===== ASGI APP (uvicorn entry point) =====

CORS = [
    [b"content-type", b"application/json"],
    [b"access-control-allow-origin", b"*"],
    [b"access-control-allow-methods", b"GET,POST,OPTIONS"],
    [b"access-control-allow-headers", b"content-type"],
]

async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    if scope["method"] == "OPTIONS":
        await send({"type": "http.response.start", "status": 204, "headers": CORS})
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
        resp, status = await handle_request(scope["method"], scope["path"], body, qs)
    except Exception as ex:
        resp, status = json.dumps({"ok": False, "error": str(ex)}), 500
    if isinstance(resp, str):
        resp = resp.encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": CORS})
    await send({"type": "http.response.body", "body": resp})

# Алиас для платформ, которые ищут 'application'
application = app

# Локальный запуск: python server.py
if __name__ == "__main__":
    import asyncio
    async def main():
        config = uvicorn.Config(app, host="0.0.0.0", port=5000)
        server = uvicorn.Server(config)
        await server.serve()
    try:
        import uvicorn
        asyncio.run(main())
    except ImportError:
        print("Run: pip install uvicorn && uvicorn server:app")

