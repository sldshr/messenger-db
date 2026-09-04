"""
MESSENGER SERVER (Python + Flask + Supabase)
Запуск: python server.py
"""
import hashlib, json, time
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify
from supabase import create_client

# ===== КОНФИГУРАЦИЯ — ВСТАВЬТЕ ВАШИ ДАННЫЕ =====
SUPABASE_URL = "https://jvvhhcxzywedqryenqvg.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imp2dmhoY3h6eXdlZHFyeWVucXZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODg1MTYxODcsImV4cCI6MjEwNDA5MjE4N30.oiZ_kBM1FXYsz0sXptOr_pOQFtoVgVXwm4fa71q1fqw"
# ================================================

app = Flask(__name__)
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()
def now_iso(): return datetime.now(timezone.utc).isoformat()
def ok(data=None): return jsonify({"ok": True, "data": data})
def err(msg, code=400): return jsonify({"ok": False, "error": msg}), code
def get_uid():
    uid = request.args.get("user_id")
    if not uid: uid = (request.get_json(silent=True) or {}).get("user_id")
    return uid

def require_user(f):
    @wraps(f)
    def w(*a, **kw):
        if not get_uid(): return err("user_id required", 401)
        return f(*a, **kw)
    return w


# ===== AUTH =====

@app.route("/api/register", methods=["POST"])
def register():
    d = request.get_json(silent=True) or {}
    uname = (d.get("username") or "").strip()
    pwd = d.get("password") or ""
    if not uname or not pwd: return err("Username and password required")
    if len(uname) < 2 or len(uname) > 30: return err("Username: 2-30 chars")
    if len(pwd) < 4: return err("Password: min 4 chars")
    ex = sb.table("users").select("id").eq("username", uname).execute()
    if ex.data: return err("Username already taken")
    res = sb.table("users").insert({"username": uname, "password_hash": hash_password(pwd)}).execute()
    if res.data:
        u = res.data[0]
        return ok({"user_id": u["id"], "username": u["username"]})
    return err("Failed to create user", 500)


@app.route("/api/login", methods=["POST"])
def login():
    d = request.get_json(silent=True) or {}
    uname = (d.get("username") or "").strip()
    pwd = d.get("password") or ""
    if not uname or not pwd: return err("Username and password required")
    res = sb.table("users").select("id,username").eq("username", uname).eq("password_hash", hash_password(pwd)).execute()
    if not res.data: return err("Invalid credentials", 401)
    u = res.data[0]
    return ok({"user_id": u["id"], "username": u["username"]})


# ===== CHANNELS =====

@app.route("/api/channels", methods=["GET"])
@require_user
def list_channels():
    uid = get_uid()
    chs = sb.table("channels").select("*").order("created_at", desc=False).execute().data or []
    my = sb.table("channel_members").select("channel_id").eq("user_id", uid).execute().data or []
    my_ids = {m["channel_id"] for m in my}
    result = []
    for ch in chs:
        cnt = sb.table("channel_members").select("user_id", count="exact").eq("channel_id", ch["id"]).execute()
        mc = len(cnt.data) if cnt.data else 0
        result.append({"id": ch["id"], "name": ch["name"], "description": ch.get("description",""),
                        "created_by": ch.get("created_by"), "created_at": ch.get("created_at"),
                        "member_count": mc, "is_member": ch["id"] in my_ids})
    return ok(result)


@app.route("/api/channels", methods=["POST"])
@require_user
def create_channel():
    uid = get_uid()
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    desc = (d.get("description") or "").strip()
    if not name: return err("Channel name required")
    if len(name) > 50: return err("Name max 50 chars")
    res = sb.table("channels").insert({"name": name, "description": desc, "created_by": uid}).execute()
    if res.data:
        ch = res.data[0]
        sb.table("channel_members").insert({"channel_id": ch["id"], "user_id": uid, "role": "admin"}).execute()
        return ok({"id": ch["id"], "name": ch["name"]})
    return err("Failed", 500)


@app.route("/api/channels/<cid>/join", methods=["POST"])
@require_user
def join_channel(cid):
    uid = get_uid()
    ch = sb.table("channels").select("id").eq("id", cid).execute()
    if not ch.data: return err("Not found", 404)
    ex = sb.table("channel_members").select("channel_id").eq("channel_id", cid).eq("user_id", uid).execute()
    if ex.data: return ok({"message": "Already a member"})
    sb.table("channel_members").insert({"channel_id": cid, "user_id": uid}).execute()
    return ok({"message": "Joined"})


@app.route("/api/channels/<cid>/leave", methods=["POST"])
@require_user
def leave_channel(cid):
    uid = get_uid()
    sb.table("channel_members").delete().eq("channel_id", cid).eq("user_id", uid).execute()
    return ok({"message": "Left"})


@app.route("/api/channels/<cid>/members", methods=["GET"])
@require_user
def get_members(cid):
    res = sb.table("channel_members").select("user_id,role,joined_at").eq("channel_id", cid).execute().data or []
    result = []
    for m in res:
        ur = sb.table("users").select("username").eq("id", m["user_id"]).execute()
        un = ur.data[0]["username"] if ur.data else "unknown"
        result.append({"user_id": m["user_id"], "username": un, "role": m.get("role","member"), "joined_at": m.get("joined_at")})
    return ok(result)


# ===== MESSAGES =====

@app.route("/api/channels/<cid>/messages", methods=["GET"])
@require_user
def get_messages(cid):
    limit = min(int(request.args.get("limit", 200)), 500)
    before = request.args.get("before")
    q = sb.table("messages").select("id,user_id,content,created_at").eq("channel_id", cid).order("created_at", desc=True).limit(limit)
    if before: q = q.lt("created_at", before)
    msgs = list(reversed(q.execute().data or []))
    ucache = {}
    result = []
    for m in msgs:
        uid = m["user_id"]
        if uid not in ucache:
            ur = sb.table("users").select("username").eq("id", uid).execute()
            ucache[uid] = ur.data[0]["username"] if ur.data else "unknown"
        result.append({"id": m["id"], "user_id": uid, "username": ucache[uid],
                        "content": m["content"], "created_at": m["created_at"]})
    return ok(result)


@app.route("/api/channels/<cid>/messages", methods=["POST"])
@require_user
def send_message(cid):
    uid = get_uid()
    d = request.get_json(silent=True) or {}
    content = (d.get("content") or "").strip()
    if not content: return err("Content required")
    if len(content) > 5000: return err("Too long")
    member = sb.table("channel_members").select("channel_id").eq("channel_id", cid).eq("user_id", uid).execute()
    if not member.data: return err("Not a member", 403)
    res = sb.table("messages").insert({"channel_id": cid, "user_id": uid, "content": content}).execute()
    if res.data:
        m = res.data[0]
        return ok({"id": m["id"], "created_at": m["created_at"]})
    return err("Failed", 500)


# ===== USERS =====

@app.route("/api/users/search", methods=["GET"])
def search_users():
    q = request.args.get("q", "").strip()
    if not q: return ok([])
    res = sb.table("users").select("id,username").ilike("username", f"%{q}%").limit(20).execute()
    return ok(res.data or [])


# ===== PING / HEALTH =====

@app.route("/api/ping", methods=["GET"])
def ping():
    return ok({"pong": True, "time": now_iso()})

@app.route("/", methods=["GET"])
def health():
    return ok({"status": "running", "service": "messenger-server"})


if __name__ == "__main__":
    print("=" * 50)
    print("  MESSENGER SERVER")
    print("=" * 50)
    print(f"  Supabase URL: {SUPABASE_URL}")
    print(f"  Supabase Key: {SUPABASE_KEY[:20]}...")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True)
