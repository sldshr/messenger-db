"""
MESSENGER SERVER — Pure ASGI, optimized for low RAM (512MB)
"""
import asyncio, hashlib, json, os, re
from datetime import datetime, timezone
from urllib.parse import parse_qs

# ===== CONFIG =====
SUPABASE_URL = os.environ.get("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "YOUR_SUPABASE_ANON_KEY")
# ==================

_sb = None
_sb_lock = asyncio.Lock()

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

# ===== STATS (minimal, only used by /api/ping) =====
import time as _time
_stats = {"start": _time.time(), "requests": 0}

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

    # --- Fast routes (no DB, no Supabase needed) ---
    if method == "GET" and path == "/":
        return ok({"status": "running", "service": "messenger-server"})
    if method == "GET" and path == "/api/ping":
        return ok({"pong": True, "time": now_iso()})

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
        res = await sb_query(lambda: sb.table("users").select("id,username,about,avatar_data,created_at,last_login_at").eq("username", u).eq("password_hash", hp(p)).execute())
        if not res.data: return er("Invalid credentials", 401)
        r = res.data[0]
        await sb_query(lambda: sb.table("users").update({"last_login_at": now_iso()}).eq("id", r["id"]).execute())
        return ok({"user_id": r["id"], "username": r["username"], "about": r.get("about",""),
                    "avatar_data": r.get("avatar_data","")})

    # --- Channels ---
    if path == "/api/channels" and method == "GET":
        u = gk("user_id")
        if not u: return er("user_id required", 401)
        chs_r = await sb_query(lambda: sb.table("channels").select("*").order("created_at", desc=False).execute())
        chs = chs_r.data or []
        my_r = await sb_query(lambda: sb.table("channel_members").select("channel_id").eq("user_id", u).execute())
        my_ids = {m["channel_id"] for m in (my_r.data or [])}
        # Auto-join user to ALL channels (batch upsert missing memberships)
        missing = [{"channel_id": ch["id"], "user_id": u, "role": "member"} for ch in chs if ch["id"] not in my_ids]
        if missing:
            await sb_query(lambda: sb.table("channel_members").upsert(missing, on_conflict="channel_id,user_id").execute())
            my_ids = {m["channel_id"] for m in chs}
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
                ur = await sb_query(lambda uid=mm["user_id"]: sb.table("users").select("username,about,avatar_data,created_at,last_login_at").eq("id", uid).execute())
                d = ur.data[0] if ur.data else {}
                result.append({"user_id": mm["user_id"], "username": d.get("username","?"), "about": d.get("about",""),
                    "avatar_data": d.get("avatar_data",""), "created_at": d.get("created_at",""), "last_login_at": d.get("last_login_at",""),
                    "role": mm.get("role","member")})
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
                    return sb.table("users").select("id,username,avatar_data").in_("id", uids).execute()
                users_r = await sb_query(_get_users)
                for usr in (users_r.data or []):
                    uc[usr["id"]] = {"username": usr["username"], "avatar_data": usr.get("avatar_data","")}
            # Batch-fetch reply_to messages
            reply_ids = [mm["reply_to"] for mm in msgs if mm.get("reply_to")]
            rc = {}
            if reply_ids:
                def _get_replies():
                    return sb.table("messages").select("id,content,user_id").in_("id", reply_ids).execute()
                reps_r = await sb_query(_get_replies)
                for rep in (reps_r.data or []):
                    u2 = uc.get(rep.get("user_id",""), {"username": "?", "avatar_data": ""})
                    rc[rep["id"]] = {"content": rep["content"][:100], "username": u2["username"],
                                      "user_id": rep.get("user_id","")}
            result = []
            for mm in msgs:
                u_info = uc.get(mm["user_id"], {"username": "?", "avatar_data": ""})
                item = {"id": mm["id"], "user_id": mm["user_id"],
                        "username": u_info["username"], "avatar_data": u_info.get("avatar_data",""),
                        "content": mm["content"], "created_at": mm["created_at"],
                        "edited": bool(mm.get("edited", False)),
                        "reply_to": mm.get("reply_to")}
                if mm.get("reply_to") and mm["reply_to"] in rc:
                    item["reply_to_content"] = rc[mm["reply_to"]]["content"]
                    item["reply_to_user"] = rc[mm["reply_to"]]["username"]
                    item["reply_to_user_id"] = rc[mm["reply_to"]]["user_id"]
                result.append(item)
            return ok(result)

        if method == "POST" and action == "messages":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            content = (data.get("content") or "").strip()
            reply_to = data.get("reply_to")
            if not content: return er("Content required")
            if len(content) > 5000: return er("Too long")
            member = await sb_query(lambda: sb.table("channel_members").select("channel_id").eq("channel_id", cid).eq("user_id", u).execute())
            if not member.data: return er("Not a member", 403)
            msg_data = {"channel_id": cid, "user_id": u, "content": content}
            if reply_to: msg_data["reply_to"] = reply_to
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
            await sb_query(lambda: sb.table("messages").update({"content": content, "edited": True}).eq("id", mid).execute())
            return ok({"id": mid})

        if method == "DELETE":
            u = gk("user_id")
            if not u: return er("user_id required", 401)
            msg_r = await sb_query(lambda: sb.table("messages").select("id,user_id").eq("id", mid).execute())
            if not msg_r.data: return er("Message not found", 404)
            if msg_r.data[0]["user_id"] != u: return er("Not your message", 403)
            await sb_query(lambda: sb.table("messages").delete().eq("id", mid).execute())
            return ok({"deleted": mid})

    # Users search
    if path == "/api/users/search" and method == "GET":
        q = (qs.get("q", [""])[0]).strip()
        if not q: return ok([])
        res = await sb_query(lambda: sb.table("users").select("id,username").ilike("username", f"%{q}%").limit(20).execute())
        return ok(res.data or [])

    # All users
    if path == "/api/users" and method == "GET":
        res = await sb_query(lambda: sb.table("users").select("id,username,about,avatar_data,created_at,last_login_at").order("username").execute())
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
        if "about" in data:
            updates["about"] = (data.get("about") or "").strip()[:500]
        if "avatar_data" in data:
            updates["avatar_data"] = (data.get("avatar_data") or "")[:300000]
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
    t0 = _time.time()
    try:
        result = await asyncio.wait_for(
            handle(scope["method"], scope["path"], body, qs), timeout=30
        )
    except asyncio.TimeoutError:
        result = (json.dumps({"ok": False, "error": "timeout"}), 504)
    except Exception as ex:
        result = (json.dumps({"ok": False, "error": str(ex)}), 500)
    # result can be (body, status) or (body, status, headers_dict)
    extra_headers = []
    if isinstance(result, tuple) and len(result) == 3:
        resp, status, hdrs = result
        if isinstance(hdrs, dict):
            for k, v in hdrs.items():
                extra_headers.append([k.encode(), v.encode()])
    elif isinstance(result, tuple) and len(result) == 2:
        resp, status = result
    else:
        resp, status = json.dumps({"ok": False, "error": "bad handler return"}), 500
    _stats["requests"] += 1
    if isinstance(resp, str):
        resp = resp.encode("utf-8")
    all_headers = [[b"access-control-allow-origin", b"*"]] + extra_headers
    if not extra_headers:
        all_headers = HEADERS
    await send({"type": "http.response.start", "status": status, "headers": all_headers})
    await send({"type": "http.response.body", "body": resp})

application = app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
