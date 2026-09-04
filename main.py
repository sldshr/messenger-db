"""
MESSENGER SERVER (Python + Flask + Supabase)
"""
import hashlib, json, time, os
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
from asgiref.wsgi import AsgiHandler

# ===== CONFIG =====
SUPABASE_URL = os.environ.get("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "YOUR_SUPABASE_ANON_KEY")
# ==================

app = Flask(__name__)
CORS(app)

_sb = None
def get_sb():
    global _sb
    if _sb is None:
        from supabase import create_client
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb

def hp(p): return hashlib.sha256(p.encode()).hexdigest()
def now_iso(): return datetime.now(timezone.utc).isoformat()
def ok(d=None): return jsonify({"ok": True, "data": d})
def er(m, c=400): return jsonify({"ok": False, "error": m}), c
def uid():
    u = request.args.get("user_id")
    if not u: u = (request.get_json(silent=True) or {}).get("user_id")
    return u
def require_user(f):
    @wraps(f)
    def w(*a, **kw):
        if not uid(): return er("user_id required", 401)
        return f(*a, **kw)
    return w

# ===== AUTH =====

@app.route("/api/register", methods=["POST"])
def register():
    d = request.get_json(silent=True) or {}
    u = (d.get("username") or "").strip()
    p = d.get("password") or ""
    if not u or not p: return er("Username and password required")
    if len(u) < 2 or len(u) > 30: return er("Username: 2-30 chars")
    if len(p) < 4: return er("Password: min 4 chars")
    ex = get_sb().table("users").select("id").eq("username", u).execute()
    if ex.data: return er("Username already taken")
    res = get_sb().table("users").insert({"username": u, "password_hash": hp(p)}).execute()
    if res.data:
        r = res.data[0]
        return ok({"user_id": r["id"], "username": r["username"]})
    return er("Failed to create user", 500)


@app.route("/api/login", methods=["POST"])
def login():
    d = request.get_json(silent=True) or {}
    u = (d.get("username") or "").strip()
    p = d.get("password") or ""
    if not u or not p: return er("Username and password required")
    res = get_sb().table("users").select("id,username").eq("username", u).eq("password_hash", hp(p)).execute()
    if not res.data: return er("Invalid credentials", 401)
    r = res.data[0]
    return ok({"user_id": r["id"], "username": r["username"]})


# ===== CHANNELS =====

@app.route("/api/channels", methods=["GET"])
@require_user
def list_channels():
    u = uid()
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
    return ok(res)


@app.route("/api/channels", methods=["POST"])
@require_user
def create_channel():
    u = uid()
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    desc = (d.get("description") or "").strip()
    if not name: return er("Channel name required")
    if len(name) > 50: return er("Name max 50 chars")
    res = get_sb().table("channels").insert({"name": name, "description": desc, "created_by": u}).execute()
    if res.data:
        ch = res.data[0]
        get_sb().table("channel_members").insert({"channel_id": ch["id"], "user_id": u, "role": "admin"}).execute()
        return ok({"id": ch["id"], "name": ch["name"]})
    return er("Failed", 500)


@app.route("/api/channels/<cid>/join", methods=["POST"])
@require_user
def join_channel(cid):
    u = uid()
    ch = get_sb().table("channels").select("id").eq("id", cid).execute()
    if not ch.data: return er("Not found", 404)
    ex = get_sb().table("channel_members").select("channel_id").eq("channel_id", cid).eq("user_id", u).execute()
    if ex.data: return ok({"message": "Already a member"})
    get_sb().table("channel_members").insert({"channel_id": cid, "user_id": u}).execute()
    return ok({"message": "Joined"})


@app.route("/api/channels/<cid>/leave", methods=["POST"])
@require_user
def leave_channel(cid):
    u = uid()
    get_sb().table("channel_members").delete().eq("channel_id", cid).eq("user_id", u).execute()
    return ok({"message": "Left"})


@app.route("/api/channels/<cid>/members", methods=["GET"])
@require_user
def get_members(cid):
    res = get_sb().table("channel_members").select("user_id,role,joined_at").eq("channel_id", cid).execute().data or []
    result = []
    for m in res:
        ur = get_sb().table("users").select("username").eq("id", m["user_id"]).execute()
        un = ur.data[0]["username"] if ur.data else "unknown"
        result.append({"user_id": m["user_id"], "username": un, "role": m.get("role","member"), "joined_at": m.get("joined_at")})
    return ok(result)


# ===== MESSAGES =====

@app.route("/api/channels/<cid>/messages", methods=["GET"])
@require_user
def get_messages(cid):
    limit = min(int(request.args.get("limit", 200)), 500)
    before = request.args.get("before")
    q = get_sb().table("messages").select("id,user_id,content,created_at").eq("channel_id", cid).order("created_at", desc=True).limit(limit)
    if before: q = q.lt("created_at", before)
    msgs = list(reversed(q.execute().data or []))
    uc = {}
    result = []
    for m in msgs:
        u = m["user_id"]
        if u not in uc:
            ur = get_sb().table("users").select("username").eq("id", u).execute()
            uc[u] = ur.data[0]["username"] if ur.data else "unknown"
        result.append({"id": m["id"], "user_id": u, "username": uc[u], "content": m["content"], "created_at": m["created_at"]})
    return ok(result)


@app.route("/api/channels/<cid>/messages", methods=["POST"])
@require_user
def send_message(cid):
    u = uid()
    d = request.get_json(silent=True) or {}
    content = (d.get("content") or "").strip()
    if not content: return er("Content required")
    if len(content) > 5000: return er("Too long")
    member = get_sb().table("channel_members").select("channel_id").eq("channel_id", cid).eq("user_id", u).execute()
    if not member.data: return er("Not a member", 403)
    res = get_sb().table("messages").insert({"channel_id": cid, "user_id": u, "content": content}).execute()
    if res.data:
        m = res.data[0]
        return ok({"id": m["id"], "created_at": m["created_at"]})
    return er("Failed", 500)


# ===== USERS =====

@app.route("/api/users/search", methods=["GET"])
def search_users():
    q = request.args.get("q", "").strip()
    if not q: return ok([])
    res = get_sb().table("users").select("id,username").ilike("username", f"%{q}%").limit(20).execute()
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
asgi_app = AsgiHandler(app)
