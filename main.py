# main.py
# pip install fastapi uvicorn supabase
# uvicorn main:app --reload

import os
import time
import uuid
import json
import hmac
import hashlib
import secrets
import re
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="sldChat")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("[sldChat] Supabase connected")
    except Exception as e:
        print("[sldChat] Supabase init error:", e)

USERS: Dict[str, dict] = {}
SESSIONS: Dict[str, dict] = {}
POSTS_MEM: Dict[str, dict] = {}
NOTIFS_MEM: Dict[str, List[dict]] = {}
DM_THREADS_MEM: Dict[str, dict] = {}
DM_MSGS_MEM: Dict[str, List[dict]] = {}

MAX_POST_LEN = 1000
MAX_COMMENT_LEN = 500
MAX_BIO_LEN = 200
MAX_DM_LEN = 2000
TRUNCATE_LINES = 100
TRUNCATE_CHARS = 500

NICK_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
MENTION_RE = re.compile(r"(?<![a-zA-Z0-9_])@([a-zA-Z0-9_]{3,20})")

RU_COUNTRIES = {"RU", "BY", "KZ", "UA", "KG", "TJ", "UZ", "AM", "AZ", "MD"}
_lang_cache: Dict[str, str] = {}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 100_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2${iterations}${salt}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if parts[0] == "pbkdf2":
            _, iters, salt, expected = parts
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
            return hmac.compare_digest(dk.hex(), expected)
        salt, expected = stored.split("$", 1)
        h = hashlib.sha256((salt + password).encode()).hexdigest()
        return hmac.compare_digest(h, expected)
    except Exception:
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def ts_to_iso(ts) -> str:
    if isinstance(ts, str):
        return ts
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(tz=timezone.utc).isoformat()


def iso_to_ts(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    if not s:
        return time.time()
    try:
        if isinstance(s, str) and s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return time.time()


def extract_mentions(text: str) -> List[str]:
    out, seen = [], set()
    for m in MENTION_RE.finditer(text):
        n = m.group(1)
        if n.lower() in seen:
            continue
        seen.add(n.lower())
        out.append(n)
    return out


def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def detect_lang(ip: str, accept_language: str) -> str:
    key = ip or "unknown"
    if key in _lang_cache:
        return _lang_cache[key]
    lang = None
    private = (
        not ip or ip.startswith("127.") or ip.startswith("10.")
        or ip.startswith("192.168.") or ip.startswith("172.")
        or ip in ("::1", "localhost")
    )
    if not private:
        try:
            req = urllib.request.Request(
                f"http://ip-api.com/json/{ip}?fields=countryCode",
                headers={"User-Agent": "sldChat"},
            )
            with urllib.request.urlopen(req, timeout=2) as r:
                data = json.loads(r.read().decode())
                cc = (data.get("countryCode") or "").upper()
                if cc in RU_COUNTRIES:
                    lang = "ru"
                elif cc:
                    lang = "en"
        except Exception:
            pass
    if not lang:
        al = (accept_language or "").lower()
        lang = "ru" if (al.startswith("ru") or ",ru" in al or "ru-" in al) else "en"
    _lang_cache[key] = lang
    return lang


def get_lang(request: Request) -> str:
    qp = request.query_params.get("lang")
    if qp in ("ru", "en"):
        return qp
    ck = request.cookies.get("sldchat_lang")
    if ck in ("ru", "en"):
        return ck
    return detect_lang(get_client_ip(request), request.headers.get("accept-language", ""))


def get_current_user(request: Request) -> Optional[dict]:
    token = request.headers.get("x-auth")
    if not token:
        return None
    sess = SESSIONS.get(token)
    if not sess:
        return None
    nick = sess.get("nick")
    if not nick:
        return None
    return db_load_user(nick)


def require_user(request: Request) -> dict:
    u = get_current_user(request)
    if not u:
        raise HTTPException(401, "unauthorized")
    return u


def db_load_user(nick: str) -> Optional[dict]:
    if not nick:
        return None
    if supabase:
        try:
            r = supabase.table("users").select("*").ilike("nick", nick).limit(1).execute()
            if r.data:
                row = r.data[0]
                row["following"] = set(row.get("following") or [])
                row["followers"] = set(row.get("followers") or [])
                row["bio"] = row.get("bio") or ""
                row["created_at"] = iso_to_ts(row.get("created_at"))
                return row
        except Exception as e:
            print("[sldChat] db_load_user error:", e)
        return None
    for u in USERS.values():
        if u["nick"].lower() == nick.lower():
            return u
    return None


def db_save_user(u: dict) -> None:
    USERS[u["nick"]] = u
    if not supabase:
        return
    try:
        supabase.table("users").upsert({
            "nick": u["nick"],
            "name": u["name"],
            "bio": u.get("bio", ""),
            "password": u["password"],
            "created_at": ts_to_iso(u["created_at"]),
            "following": list(u.get("following") or []),
            "followers": list(u.get("followers") or []),
            "allow_followers_view": u.get("allow_followers_view", True),
            "allow_following_view": u.get("allow_following_view", True),
        }).execute()
    except Exception as e:
        print("[sldChat] db_save_user error:", e)
    USERS.pop(u["nick"], None)


def db_update_user_fields(nick: str, patch: dict) -> None:
    if not supabase:
        if nick in USERS:
            USERS[nick].update(patch)
        return
    try:
        supabase.table("users").update(patch).eq("nick", nick).execute()
    except Exception as e:
        print("[sldChat] db_update_user_fields error:", e)


def db_all_users() -> List[dict]:
    if supabase:
        try:
            r = supabase.table("users").select("*").execute()
            out = []
            for row in r.data or []:
                row["following"] = set(row.get("following") or [])
                row["followers"] = set(row.get("followers") or [])
                row["bio"] = row.get("bio") or ""
                row["created_at"] = iso_to_ts(row.get("created_at"))
                out.append(row)
            return out
        except Exception as e:
            print("[sldChat] db_all_users error:", e)
            return []
    return list(USERS.values())


def db_create_post(p: dict) -> None:
    if not supabase:
        POSTS_MEM[p["id"]] = p
        return
    try:
        supabase.table("posts").insert({
            "id": p["id"], "text": p["text"], "author": p["author"],
            "created_at": ts_to_iso(p["created_at"]),
        }).execute()
    except Exception as e:
        print("[sldChat] db_create_post error:", e)


def db_update_post_text(pid: str, text: str) -> None:
    if not supabase:
        if pid in POSTS_MEM:
            POSTS_MEM[pid]["text"] = text
        return
    try:
        supabase.table("posts").update({"text": text}).eq("id", pid).execute()
    except Exception as e:
        print("[sldChat] db_update_post error:", e)


def db_delete_post(pid: str) -> None:
    if not supabase:
        POSTS_MEM.pop(pid, None)
        return
    try:
        supabase.table("notifications").delete().eq("post_id", pid).execute()
        supabase.table("posts").delete().eq("id", pid).execute()
    except Exception as e:
        print("[sldChat] db_delete_post error:", e)


def db_get_post(pid: str) -> Optional[dict]:
    if supabase:
        try:
            r = supabase.table("posts").select("*").eq("id", pid).limit(1).execute()
            if not r.data:
                return None
            row = r.data[0]
            row["created_at"] = iso_to_ts(row.get("created_at"))
            return row
        except Exception as e:
            print("[sldChat] db_get_post error:", e)
            return None
    return POSTS_MEM.get(pid)


def db_list_posts(q: str = "", author: str = "") -> List[dict]:
    if supabase:
        try:
            query = supabase.table("posts").select("*")
            if author:
                query = query.eq("author", author)
            if q:
                query = query.ilike("text", f"%{q}%")
            query = query.order("created_at", desc=True).limit(300)
            rows = query.execute().data or []
            for row in rows:
                row["created_at"] = iso_to_ts(row.get("created_at"))
            return rows
        except Exception as e:
            print("[sldChat] db_list_posts error:", e)
            return []
    items = list(POSTS_MEM.values())
    if author:
        items = [p for p in items if p["author"] == author]
    if q:
        n = q.lower()
        items = [p for p in items if n in p["text"].lower()]
    items.sort(key=lambda p: p["created_at"], reverse=True)
    return items


def db_post_votes(post_ids: List[str]) -> List[dict]:
    if not post_ids:
        return []
    if supabase:
        try:
            return supabase.table("post_votes").select("*").in_("post_id", post_ids).execute().data or []
        except Exception as e:
            print("[sldChat] db_post_votes error:", e)
            return []
    out = []
    for pid in post_ids:
        p = POSTS_MEM.get(pid)
        if p:
            for vid, d in p.get("votes", {}).items():
                out.append({"post_id": pid, "voter_id": vid, "direction": d})
    return out


def db_set_post_vote(post_id: str, voter_id: str, direction: int) -> None:
    if supabase:
        try:
            if direction == 0:
                supabase.table("post_votes").delete().eq("post_id", post_id).eq("voter_id", voter_id).execute()
            else:
                supabase.table("post_votes").upsert({
                    "post_id": post_id, "voter_id": voter_id, "direction": direction,
                }).execute()
        except Exception as e:
            print("[sldChat] db_set_post_vote error:", e)
        return
    p = POSTS_MEM.get(post_id)
    if not p:
        return
    if direction == 0:
        p["votes"].pop(voter_id, None)
    else:
        p["votes"][voter_id] = direction


def db_comments_for_posts(post_ids: List[str]) -> List[dict]:
    if not post_ids:
        return []
    if supabase:
        try:
            rows = (supabase.table("comments").select("*")
                    .in_("post_id", post_ids)
                    .order("created_at")
                    .execute().data or [])
            for row in rows:
                row["created_at"] = iso_to_ts(row.get("created_at"))
            return rows
        except Exception as e:
            print("[sldChat] db_comments_for_posts error:", e)
            return []
    out = []
    for pid in post_ids:
        p = POSTS_MEM.get(pid)
        if p:
            for c in p.get("comments", []):
                out.append({
                    "id": c["id"], "post_id": pid, "author": c["author"],
                    "parent_id": c.get("parent_id"),
                    "text": c["text"], "created_at": c["created_at"],
                })
    return out


def db_comment_votes(comment_ids: List[str]) -> List[dict]:
    if not comment_ids:
        return []
    if supabase:
        try:
            return supabase.table("comment_votes").select("*").in_("comment_id", comment_ids).execute().data or []
        except Exception as e:
            print("[sldChat] db_comment_votes error:", e)
            return []
    out = []
    for pid, p in POSTS_MEM.items():
        for c in p.get("comments", []):
            if c["id"] in comment_ids:
                for vid, d in c.get("votes", {}).items():
                    out.append({"comment_id": c["id"], "voter_id": vid, "direction": d})
    return out


def db_create_comment(c: dict) -> None:
    if not supabase:
        p = POSTS_MEM.get(c["post_id"])
        if p:
            p.setdefault("comments", []).append({
                "id": c["id"], "author": c["author"], "parent_id": c.get("parent_id"),
                "text": c["text"], "created_at": c["created_at"], "votes": {},
            })
        return
    try:
        supabase.table("comments").insert({
            "id": c["id"], "post_id": c["post_id"], "author": c["author"],
            "parent_id": c.get("parent_id"),
            "text": c["text"], "created_at": ts_to_iso(c["created_at"]),
        }).execute()
    except Exception as e:
        print("[sldChat] db_create_comment error:", e)


def db_update_comment_text(cid: str, text: str) -> None:
    if not supabase:
        for p in POSTS_MEM.values():
            for c in p.get("comments", []):
                if c["id"] == cid:
                    c["text"] = text
                    return
        return
    try:
        supabase.table("comments").update({"text": text}).eq("id", cid).execute()
    except Exception as e:
        print("[sldChat] db_update_comment error:", e)


def db_delete_comment(cid: str) -> None:
    if not supabase:
        for p in POSTS_MEM.values():
            p["comments"] = [c for c in p.get("comments", []) if c["id"] != cid]
        return
    try:
        supabase.table("notifications").delete().eq("comment_id", cid).execute()
        supabase.table("comments").delete().eq("id", cid).execute()
    except Exception as e:
        print("[sldChat] db_delete_comment error:", e)


def db_get_comment(cid: str) -> Optional[dict]:
    if supabase:
        try:
            r = supabase.table("comments").select("*").eq("id", cid).limit(1).execute()
            if not r.data:
                return None
            row = r.data[0]
            row["created_at"] = iso_to_ts(row.get("created_at"))
            return row
        except Exception as e:
            print("[sldChat] db_get_comment error:", e)
            return None
    for p in POSTS_MEM.values():
        for c in p.get("comments", []):
            if c["id"] == cid:
                return {
                    "id": c["id"], "post_id": p["id"], "author": c["author"],
                    "parent_id": c.get("parent_id"),
                    "text": c["text"], "created_at": c["created_at"],
                }
    return None


def db_set_comment_vote(cid: str, voter_id: str, direction: int) -> None:
    if supabase:
        try:
            if direction == 0:
                supabase.table("comment_votes").delete().eq("comment_id", cid).eq("voter_id", voter_id).execute()
            else:
                supabase.table("comment_votes").upsert({
                    "comment_id": cid, "voter_id": voter_id, "direction": direction,
                }).execute()
        except Exception as e:
            print("[sldChat] db_set_comment_vote error:", e)
        return
    for p in POSTS_MEM.values():
        for c in p.get("comments", []):
            if c["id"] == cid:
                if direction == 0:
                    c["votes"].pop(voter_id, None)
                else:
                    c["votes"][voter_id] = direction
                return


def db_notify(to_nick: str, ntype: str, from_nick: str,
              post_id: str = "", comment_id: str = "", text: str = "") -> None:
    if not to_nick or to_nick == from_nick:
        return
    n = {
        "id": uuid.uuid4().hex[:10],
        "to_nick": to_nick, "type": ntype, "from_nick": from_nick,
        "post_id": post_id, "comment_id": comment_id,
        "text": text, "read": False, "created_at": time.time(),
    }
    if supabase:
        try:
            supabase.table("notifications").insert({
                "id": n["id"], "to_nick": n["to_nick"], "type": n["type"],
                "from_nick": n["from_nick"], "post_id": n["post_id"],
                "comment_id": n["comment_id"] or None,
                "text": n["text"], "read": n["read"],
                "created_at": ts_to_iso(n["created_at"]),
            }).execute()
        except Exception as e:
            print("[sldChat] db_notify error:", e)
        return
    NOTIFS_MEM.setdefault(to_nick, []).append(n)


def db_notifications(nick: str) -> List[dict]:
    if supabase:
        try:
            r = (supabase.table("notifications").select("*")
                 .eq("to_nick", nick).order("created_at", desc=True).limit(100).execute())
            rows = r.data or []
            for row in rows:
                row["created_at"] = iso_to_ts(row.get("created_at"))
            return rows
        except Exception as e:
            print("[sldChat] db_notifications error:", e)
            return []
    items = list(NOTIFS_MEM.get(nick, []))
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items[:100]


def db_notifications_mark_read(nick: str) -> None:
    if supabase:
        try:
            supabase.table("notifications").update({"read": True}).eq("to_nick", nick).execute()
        except Exception as e:
            print("[sldChat] db_notif_read error:", e)
        return
    for n in NOTIFS_MEM.get(nick, []):
        n["read"] = True


def db_notifications_clear(nick: str) -> None:
    if supabase:
        try:
            supabase.table("notifications").delete().eq("to_nick", nick).execute()
        except Exception as e:
            print("[sldChat] db_notif_clear error:", e)
        return
    NOTIFS_MEM[nick] = []


def thread_pair(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a < b else (b, a)


def db_dm_find_thread(a: str, b: str) -> Optional[dict]:
    ua, ub = thread_pair(a, b)
    if supabase:
        try:
            r = (supabase.table("dm_threads").select("*")
                 .eq("user_a", ua).eq("user_b", ub).limit(1).execute())
            if r.data:
                row = r.data[0]
                row["created_at"] = iso_to_ts(row.get("created_at"))
                row["last_message_at"] = iso_to_ts(row.get("last_message_at"))
                row["last_read_a"] = iso_to_ts(row.get("last_read_a")) if row.get("last_read_a") else 0
                row["last_read_b"] = iso_to_ts(row.get("last_read_b")) if row.get("last_read_b") else 0
                return row
        except Exception as e:
            print("[sldChat] db_dm_find_thread error:", e)
        return None
    return DM_THREADS_MEM.get(ua + "|" + ub)


def db_dm_create_thread(a: str, b: str) -> dict:
    ua, ub = thread_pair(a, b)
    tid = uuid.uuid4().hex[:12]
    row = {
        "id": tid, "user_a": ua, "user_b": ub,
        "created_at": time.time(), "last_message_at": time.time(),
        "last_read_a": 0, "last_read_b": 0,
    }
    if supabase:
        try:
            supabase.table("dm_threads").insert({
                "id": tid, "user_a": ua, "user_b": ub,
                "created_at": ts_to_iso(row["created_at"]),
                "last_message_at": ts_to_iso(row["last_message_at"]),
            }).execute()
        except Exception as e:
            print("[sldChat] db_dm_create_thread error:", e)
        return row
    DM_THREADS_MEM[ua + "|" + ub] = row
    return row


def db_dm_get_or_create_thread(a: str, b: str) -> dict:
    t = db_dm_find_thread(a, b)
    if t:
        return t
    return db_dm_create_thread(a, b)


def db_dm_update_thread_last_message(tid: str, ts: float) -> None:
    if supabase:
        try:
            supabase.table("dm_threads").update({"last_message_at": ts_to_iso(ts)}).eq("id", tid).execute()
        except Exception as e:
            print("[sldChat] db_dm_update_thread error:", e)
        return
    for k, v in DM_THREADS_MEM.items():
        if v["id"] == tid:
            v["last_message_at"] = ts
            return


def db_dm_mark_read(tid: str, nick: str) -> None:
    t = db_dm_get_thread_by_id(tid)
    if not t:
        return
    now = time.time()
    field = "last_read_a" if t["user_a"] == nick else "last_read_b"
    if supabase:
        try:
            supabase.table("dm_threads").update({field: ts_to_iso(now)}).eq("id", tid).execute()
        except Exception as e:
            print("[sldChat] db_dm_mark_read error:", e)
        return
    for k, v in DM_THREADS_MEM.items():
        if v["id"] == tid:
            v[field] = now
            return


def db_dm_get_thread_by_id(tid: str) -> Optional[dict]:
    if supabase:
        try:
            r = supabase.table("dm_threads").select("*").eq("id", tid).limit(1).execute()
            if not r.data:
                return None
            row = r.data[0]
            row["created_at"] = iso_to_ts(row.get("created_at"))
            row["last_message_at"] = iso_to_ts(row.get("last_message_at"))
            row["last_read_a"] = iso_to_ts(row.get("last_read_a")) if row.get("last_read_a") else 0
            row["last_read_b"] = iso_to_ts(row.get("last_read_b")) if row.get("last_read_b") else 0
            return row
        except Exception as e:
            print("[sldChat] db_dm_get_thread error:", e)
            return None
    for v in DM_THREADS_MEM.values():
        if v["id"] == tid:
            return v
    return None


def db_dm_create_message(tid: str, from_nick: str, text: str) -> dict:
    mid = uuid.uuid4().hex[:12]
    ts = time.time()
    row = {"id": mid, "thread_id": tid, "from_nick": from_nick, "text": text, "created_at": ts}
    if supabase:
        try:
            supabase.table("dm_messages").insert({
                "id": mid, "thread_id": tid, "from_nick": from_nick,
                "text": text, "created_at": ts_to_iso(ts),
            }).execute()
        except Exception as e:
            print("[sldChat] db_dm_create_message error:", e)
        return row
    DM_MSGS_MEM.setdefault(tid, []).append(row)
    return row


def db_dm_messages(tid: str) -> List[dict]:
    if supabase:
        try:
            r = (supabase.table("dm_messages").select("*")
                 .eq("thread_id", tid).order("created_at").limit(500).execute())
            rows = r.data or []
            for row in rows:
                row["created_at"] = iso_to_ts(row.get("created_at"))
            return rows
        except Exception as e:
            print("[sldChat] db_dm_messages error:", e)
            return []
    items = list(DM_MSGS_MEM.get(tid, []))
    items.sort(key=lambda x: x["created_at"])
    return items


def db_dm_my_threads(nick: str) -> List[dict]:
    if supabase:
        try:
            r1 = (supabase.table("dm_threads").select("*")
                  .eq("user_a", nick).order("last_message_at", desc=True).limit(100).execute())
            r2 = (supabase.table("dm_threads").select("*")
                  .eq("user_b", nick).order("last_message_at", desc=True).limit(100).execute())
            rows = (r1.data or []) + (r2.data or [])
            for row in rows:
                row["created_at"] = iso_to_ts(row.get("created_at"))
                row["last_message_at"] = iso_to_ts(row.get("last_message_at"))
                row["last_read_a"] = iso_to_ts(row.get("last_read_a")) if row.get("last_read_a") else 0
                row["last_read_b"] = iso_to_ts(row.get("last_read_b")) if row.get("last_read_b") else 0
            rows.sort(key=lambda x: x["last_message_at"], reverse=True)
            return rows
        except Exception as e:
            print("[sldChat] db_dm_my_threads error:", e)
            return []
    rows = [v for v in DM_THREADS_MEM.values() if v["user_a"] == nick or v["user_b"] == nick]
    rows.sort(key=lambda x: x["last_message_at"], reverse=True)
    return rows


def db_dm_unread_count(nick: str) -> int:
    threads = db_dm_my_threads(nick)
    total = 0
    for t in threads:
        my_read = t["last_read_a"] if t["user_a"] == nick else t["last_read_b"]
        other = t["user_b"] if t["user_a"] == nick else t["user_a"]
        msgs = db_dm_messages(t["id"])
        for m in msgs:
            if m["from_nick"] == other and m["created_at"] > (my_read or 0):
                total += 1
    return total


def build_posts_full(posts: List[dict], voter_id: str) -> List[dict]:
    if not posts:
        return []
    post_ids = [p["id"] for p in posts]
    votes = db_post_votes(post_ids)
    vmap: Dict[str, Dict[str, int]] = {}
    for v in votes:
        vmap.setdefault(v["post_id"], {})[v["voter_id"]] = v["direction"]

    comments = db_comments_for_posts(post_ids)
    cmap: Dict[str, List[dict]] = {}
    for c in comments:
        cmap.setdefault(c["post_id"], []).append(c)

    comment_ids = [c["id"] for c in comments]
    cvotes = db_comment_votes(comment_ids)
    cvmap: Dict[str, Dict[str, int]] = {}
    for v in cvotes:
        cvmap.setdefault(v["comment_id"], {})[v["voter_id"]] = v["direction"]

    out = []
    for p in posts:
        pvotes = vmap.get(p["id"], {})
        up = sum(1 for d in pvotes.values() if d == 1)
        down = sum(1 for d in pvotes.values() if d == -1)
        uv = pvotes.get(voter_id, 0)
        clist = []
        for c in cmap.get(p["id"], []):
            cv = cvmap.get(c["id"], {})
            cup = sum(1 for d in cv.values() if d == 1)
            cdown = sum(1 for d in cv.values() if d == -1)
            cuv = cv.get(voter_id, 0)
            clist.append({
                "id": c["id"], "text": c["text"], "created_at": c["created_at"],
                "author": c.get("author"), "parent_id": c.get("parent_id"),
                "upvotes": cup, "downvotes": cdown, "user_vote": cuv,
            })
        out.append({
            "id": p["id"], "text": p["text"], "created_at": p["created_at"],
            "author": p.get("author"),
            "upvotes": up, "downvotes": down, "user_vote": uv,
            "comments": clist,
        })
    return out


def serialize_user(u: dict, viewer_nick: Optional[str] = None) -> dict:
    d = {
        "nick": u["nick"], "name": u["name"], "bio": u.get("bio") or "",
        "created_at": u["created_at"],
        "followers": len(u.get("followers") or []),
        "following": len(u.get("following") or []),
        "is_me": viewer_nick == u["nick"],
    }
    if viewer_nick == u["nick"]:
        d["allow_followers_view"] = u.get("allow_followers_view", True)
        d["allow_following_view"] = u.get("allow_following_view", True)
    return d


class PostIn(BaseModel):
    text: str


class PostEditIn(BaseModel):
    text: str


class VoteIn(BaseModel):
    direction: int


class CommentIn(BaseModel):
    text: str
    parent_id: Optional[str] = None


class CommentEditIn(BaseModel):
    text: str


class RegisterIn(BaseModel):
    name: str
    nick: str
    password: str
    password_confirm: str


class LoginIn(BaseModel):
    nick: str
    password: str


class BioIn(BaseModel):
    bio: str


class SettingsIn(BaseModel):
    allow_followers_view: Optional[bool] = None
    allow_following_view: Optional[bool] = None


class DMSendIn(BaseModel):
    to: str
    text: str


@app.post("/api/register")
def api_register(data: RegisterIn):
    name = data.name.strip()
    nick = data.nick.strip().lstrip("@")
    if not NICK_RE.match(nick):
        raise HTTPException(400, "err_bad_nick")
    if len(name) < 1 or len(name) > 50:
        raise HTTPException(400, "err_bad_name")
    if len(data.password) < 6:
        raise HTTPException(400, "err_short_pass")
    if data.password != data.password_confirm:
        raise HTTPException(400, "err_pass_mismatch")
    if db_load_user(nick):
        raise HTTPException(400, "err_nick_taken")

    u = {
        "nick": nick, "name": name, "bio": "",
        "password": hash_password(data.password),
        "created_at": time.time(),
        "following": set(), "followers": set(),
        "allow_followers_view": True, "allow_following_view": True,
    }
    db_save_user(u)
    token = new_token()
    SESSIONS[token] = {"nick": nick, "created_at": time.time()}
    return {"token": token, "user": serialize_user(u, nick)}


@app.post("/api/login")
def api_login(data: LoginIn):
    nick = data.nick.strip().lstrip("@")
    u = db_load_user(nick)
    if not u or not check_password(data.password, u["password"]):
        raise HTTPException(400, "err_bad_login")
    token = new_token()
    SESSIONS[token] = {"nick": u["nick"], "created_at": time.time()}
    return {"token": token, "user": serialize_user(u, u["nick"])}


@app.post("/api/logout")
def api_logout(request: Request):
    token = request.headers.get("x-auth")
    if token:
        SESSIONS.pop(token, None)
    return {"ok": True}


@app.get("/api/me")
def api_me(request: Request):
    u = get_current_user(request)
    if not u:
        raise HTTPException(401, "unauthorized")
    return serialize_user(u, u["nick"])


@app.post("/api/users/me/bio")
def api_set_bio(data: BioIn, request: Request):
    me = require_user(request)
    bio = data.bio.strip()
    if len(bio) > MAX_BIO_LEN:
        raise HTTPException(400, "too long")
    db_update_user_fields(me["nick"], {"bio": bio})
    return {"ok": True, "bio": bio}


@app.post("/api/users/me/settings")
def api_set_settings(data: SettingsIn, request: Request):
    me = require_user(request)
    patch = {}
    if data.allow_followers_view is not None:
        patch["allow_followers_view"] = bool(data.allow_followers_view)
    if data.allow_following_view is not None:
        patch["allow_following_view"] = bool(data.allow_following_view)
    if patch:
        db_update_user_fields(me["nick"], patch)
    return {"ok": True}


@app.get("/api/users")
def api_users_list(request: Request, q: str = ""):
    users = db_all_users()
    if q:
        n = q.lower().strip().lstrip("@")
        users = [u for u in users if n in u["nick"].lower() or n in (u.get("name") or "").lower()]
    users.sort(key=lambda u: u["nick"].lower())
    viewer = get_current_user(request)
    vn = viewer["nick"] if viewer else None
    return {"users": [serialize_user(u, vn) for u in users[:200]]}


@app.get("/api/users/{nick}")
def api_user(nick: str, request: Request):
    u = db_load_user(nick)
    if not u:
        raise HTTPException(404, "not found")
    viewer = get_current_user(request)
    data = serialize_user(u, viewer["nick"] if viewer else None)
    data["is_following"] = bool(viewer and u["nick"] in (viewer.get("following") or set()))
    return data


@app.get("/api/users/{nick}/followers")
def api_followers(nick: str, request: Request):
    u = db_load_user(nick)
    if not u:
        raise HTTPException(404, "not found")
    viewer = get_current_user(request)
    vn = viewer["nick"] if viewer else None
    if vn != u["nick"] and not u.get("allow_followers_view", True):
        raise HTTPException(403, "err_private_followers")
    out = []
    for n in (u.get("followers") or []):
        fu = db_load_user(n)
        if fu:
            out.append(serialize_user(fu, vn))
    out.sort(key=lambda x: x["nick"].lower())
    return {"users": out}


@app.get("/api/users/{nick}/following")
def api_following(nick: str, request: Request):
    u = db_load_user(nick)
    if not u:
        raise HTTPException(404, "not found")
    viewer = get_current_user(request)
    vn = viewer["nick"] if viewer else None
    if vn != u["nick"] and not u.get("allow_following_view", True):
        raise HTTPException(403, "err_private_following")
    out = []
    for n in (u.get("following") or []):
        fu = db_load_user(n)
        if fu:
            out.append(serialize_user(fu, vn))
    out.sort(key=lambda x: x["nick"].lower())
    return {"users": out}


@app.post("/api/users/{nick}/follow")
def api_follow(nick: str, request: Request):
    me = require_user(request)
    target = db_load_user(nick)
    if not target:
        raise HTTPException(404, "not found")
    if target["nick"] == me["nick"]:
        raise HTTPException(400, "self")

    if target["nick"] not in (me.get("following") or set()):
        nf = set(me.get("following") or set())
        nf.add(target["nick"])
        db_update_user_fields(me["nick"], {"following": list(nf)})
        nfw = set(target.get("followers") or set())
        nfw.add(me["nick"])
        db_update_user_fields(target["nick"], {"followers": list(nfw)})
        db_notify(target["nick"], "follow", me["nick"])

    target = db_load_user(nick)
    data = serialize_user(target, me["nick"])
    data["is_following"] = True
    return data


@app.post("/api/users/{nick}/unfollow")
def api_unfollow(nick: str, request: Request):
    me = require_user(request)
    target = db_load_user(nick)
    if not target:
        raise HTTPException(404, "not found")

    if target["nick"] in (me.get("following") or set()):
        nf = set(me.get("following") or set())
        nf.discard(target["nick"])
        db_update_user_fields(me["nick"], {"following": list(nf)})
        nfw = set(target.get("followers") or set())
        nfw.discard(me["nick"])
        db_update_user_fields(target["nick"], {"followers": list(nfw)})

    target = db_load_user(nick)
    data = serialize_user(target, me["nick"])
    data["is_following"] = False
    return data


@app.get("/api/posts")
def api_list(request: Request, q: str = "", author: str = ""):
    posts = db_list_posts(q=q, author=author)
    u = get_current_user(request)
    vid = "u:" + u["nick"] if u else "c:anon"
    return {"posts": build_posts_full(posts, vid)}


@app.get("/api/posts/{pid}")
def api_get(pid: str, request: Request):
    p = db_get_post(pid)
    if not p:
        raise HTTPException(404, "not found")
    u = get_current_user(request)
    vid = "u:" + u["nick"] if u else "c:anon"
    return build_posts_full([p], vid)[0]


@app.post("/api/posts")
def api_create(payload: PostIn, request: Request):
    u = require_user(request)
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_POST_LEN:
        raise HTTPException(400, "too long")
    pid = uuid.uuid4().hex[:10]
    p = {"id": pid, "text": text, "author": u["nick"], "created_at": time.time()}
    db_create_post(p)

    notified = set()
    for nick in extract_mentions(text):
        if nick == u["nick"]:
            continue
        key = nick.lower()
        if key in notified:
            continue
        if not db_load_user(nick):
            continue
        db_notify(nick, "mention", u["nick"], post_id=pid, text=text[:140])
        notified.add(key)

    return build_posts_full([p], "u:" + u["nick"])[0]


@app.put("/api/posts/{pid}")
def api_edit_post(pid: str, payload: PostEditIn, request: Request):
    u = require_user(request)
    p = db_get_post(pid)
    if not p:
        raise HTTPException(404, "not found")
    if p["author"] != u["nick"]:
        raise HTTPException(403, "forbidden")
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_POST_LEN:
        raise HTTPException(400, "too long")
    db_update_post_text(pid, text)
    p = db_get_post(pid)
    return build_posts_full([p], "u:" + u["nick"])[0]


@app.delete("/api/posts/{pid}")
def api_delete_post(pid: str, request: Request):
    u = require_user(request)
    p = db_get_post(pid)
    if not p:
        raise HTTPException(404, "not found")
    if p["author"] != u["nick"]:
        raise HTTPException(403, "forbidden")
    db_delete_post(pid)
    return {"ok": True}


@app.post("/api/posts/{pid}/vote")
def api_vote_post(pid: str, v: VoteIn, request: Request):
    if not db_get_post(pid):
        raise HTTPException(404, "not found")
    if v.direction not in (-1, 1):
        raise HTTPException(400, "bad request")
    u = require_user(request)
    vid = "u:" + u["nick"]

    existing = db_post_votes([pid])
    cur = 0
    for row in existing:
        if row["voter_id"] == vid:
            cur = row["direction"]
            break
    new = 0 if cur == v.direction else v.direction
    db_set_post_vote(pid, vid, new)

    p = db_get_post(pid)
    return build_posts_full([p], vid)[0]


@app.post("/api/posts/{pid}/comments")
def api_add_comment(pid: str, c: CommentIn, request: Request):
    u = require_user(request)
    post = db_get_post(pid)
    if not post:
        raise HTTPException(404, "not found")
    text = c.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_COMMENT_LEN:
        raise HTTPException(400, "too long")

    parent_id = c.parent_id or None
    parent = None
    if parent_id:
        parent = db_get_comment(parent_id)
        if not parent or parent["post_id"] != pid:
            raise HTTPException(400, "bad parent")
        if parent.get("parent_id"):
            raise HTTPException(400, "reply_only_one_level")

    cid = uuid.uuid4().hex[:10]
    db_create_comment({
        "id": cid, "post_id": pid, "author": u["nick"],
        "parent_id": parent_id, "text": text, "created_at": time.time(),
    })

    notified = set()
    if parent_id and parent:
        if parent["author"] != u["nick"]:
            db_notify(parent["author"], "reply", u["nick"], post_id=pid, comment_id=cid, text=text[:140])
            notified.add(parent["author"].lower())
    else:
        if post["author"] != u["nick"]:
            db_notify(post["author"], "comment", u["nick"], post_id=pid, comment_id=cid, text=text[:140])
            notified.add(post["author"].lower())

    for nick in extract_mentions(text):
        if nick == u["nick"]:
            continue
        key = nick.lower()
        if key in notified:
            continue
        if not db_load_user(nick):
            continue
        db_notify(nick, "mention", u["nick"], post_id=pid, comment_id=cid, text=text[:140])
        notified.add(key)

    return build_posts_full([post], "u:" + u["nick"])[0]


@app.put("/api/posts/{pid}/comments/{cid}")
def api_edit_comment(pid: str, cid: str, payload: CommentEditIn, request: Request):
    u = require_user(request)
    c = db_get_comment(cid)
    if not c or c["post_id"] != pid:
        raise HTTPException(404, "not found")
    if c["author"] != u["nick"]:
        raise HTTPException(403, "forbidden")
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_COMMENT_LEN:
        raise HTTPException(400, "too long")
    db_update_comment_text(cid, text)
    post = db_get_post(pid)
    return build_posts_full([post], "u:" + u["nick"])[0]


@app.delete("/api/posts/{pid}/comments/{cid}")
def api_delete_comment(pid: str, cid: str, request: Request):
    u = require_user(request)
    c = db_get_comment(cid)
    if not c or c["post_id"] != pid:
        raise HTTPException(404, "not found")
    if c["author"] != u["nick"]:
        raise HTTPException(403, "forbidden")
    db_delete_comment(cid)
    post = db_get_post(pid)
    return build_posts_full([post], "u:" + u["nick"])[0]


@app.post("/api/posts/{pid}/comments/{cid}/vote")
def api_vote_comment(pid: str, cid: str, v: VoteIn, request: Request):
    if not db_get_post(pid):
        raise HTTPException(404, "not found")
    if v.direction not in (-1, 1):
        raise HTTPException(400, "bad request")
    u = require_user(request)
    vid = "u:" + u["nick"]

    existing = db_comment_votes([cid])
    cur = 0
    for row in existing:
        if row["voter_id"] == vid:
            cur = row["direction"]
            break
    new = 0 if cur == v.direction else v.direction
    db_set_comment_vote(cid, vid, new)

    p = db_get_post(pid)
    return build_posts_full([p], vid)[0]


@app.get("/api/notifications")
def api_notifications(request: Request):
    me = require_user(request)
    items = db_notifications(me["nick"])
    unread = sum(1 for n in items if not n.get("read"))
    return {"items": items, "unread": unread}


@app.post("/api/notifications/read")
def api_notifications_read(request: Request):
    me = require_user(request)
    db_notifications_mark_read(me["nick"])
    return {"ok": True}


@app.post("/api/notifications/clear")
def api_notifications_clear(request: Request):
    me = require_user(request)
    db_notifications_clear(me["nick"])
    return {"ok": True}


def serialize_dm_thread(t: dict, me: str) -> dict:
    other = t["user_b"] if t["user_a"] == me else t["user_a"]
    ou = db_load_user(other)
    msgs = db_dm_messages(t["id"])
    last = msgs[-1] if msgs else None
    my_read = t["last_read_a"] if t["user_a"] == me else t["last_read_b"]
    unread = 0
    for m in msgs:
        if m["from_nick"] == other and m["created_at"] > (my_read or 0):
            unread += 1
    return {
        "id": t["id"],
        "other_nick": other,
        "other_name": (ou or {}).get("name", other),
        "last_message": (last or {}).get("text", ""),
        "last_message_at": t["last_message_at"],
        "last_from_me": bool(last and last["from_nick"] == me),
        "unread": unread,
    }


@app.get("/api/dm/threads")
def api_dm_threads(request: Request):
    me = require_user(request)
    threads = db_dm_my_threads(me["nick"])
    out = [serialize_dm_thread(t, me["nick"]) for t in threads]
    return {"threads": out, "unread": sum(x["unread"] for x in out)}


@app.get("/api/dm/unread")
def api_dm_unread(request: Request):
    me = require_user(request)
    return {"unread": db_dm_unread_count(me["nick"])}


@app.get("/api/dm/with/{nick}")
def api_dm_with(nick: str, request: Request):
    me = require_user(request)
    other = db_load_user(nick)
    if not other:
        raise HTTPException(404, "not found")
    if other["nick"] == me["nick"]:
        raise HTTPException(400, "self")

    t = db_dm_find_thread(me["nick"], other["nick"])
    if not t:
        return {
            "thread_id": None,
            "other": serialize_user(other, me["nick"]),
            "messages": [],
        }

    msgs = db_dm_messages(t["id"])
    db_dm_mark_read(t["id"], me["nick"])
    return {
        "thread_id": t["id"],
        "other": serialize_user(other, me["nick"]),
        "messages": [
            {"id": m["id"], "from_nick": m["from_nick"], "text": m["text"],
             "created_at": m["created_at"], "mine": m["from_nick"] == me["nick"]}
            for m in msgs
        ],
    }


@app.get("/api/dm/thread/{tid}")
def api_dm_thread(tid: str, request: Request):
    me = require_user(request)
    t = db_dm_get_thread_by_id(tid)
    if not t:
        raise HTTPException(404, "not found")
    if me["nick"] != t["user_a"] and me["nick"] != t["user_b"]:
        raise HTTPException(403, "forbidden")
    other_nick = t["user_b"] if t["user_a"] == me["nick"] else t["user_a"]
    other = db_load_user(other_nick)
    msgs = db_dm_messages(tid)
    db_dm_mark_read(tid, me["nick"])
    return {
        "thread_id": tid,
        "other": serialize_user(other or {"nick": other_nick, "name": other_nick,
                                          "bio": "", "created_at": 0,
                                          "following": set(), "followers": set()}, me["nick"]),
        "messages": [
            {"id": m["id"], "from_nick": m["from_nick"], "text": m["text"],
             "created_at": m["created_at"], "mine": m["from_nick"] == me["nick"]}
            for m in msgs
        ],
    }


@app.post("/api/dm/send")
def api_dm_send(data: DMSendIn, request: Request):
    me = require_user(request)
    other = db_load_user(data.to)
    if not other:
        raise HTTPException(404, "not found")
    if other["nick"] == me["nick"]:
        raise HTTPException(400, "self")
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_DM_LEN:
        raise HTTPException(400, "too long")

    t = db_dm_get_or_create_thread(me["nick"], other["nick"])
    msg = db_dm_create_message(t["id"], me["nick"], text)
    db_dm_update_thread_last_message(t["id"], msg["created_at"])
    return {
        "thread_id": t["id"],
        "message": {
            "id": msg["id"], "from_nick": msg["from_nick"],
            "text": msg["text"], "created_at": msg["created_at"], "mine": True,
        },
    }


TEXTS = {
    "ru": {
        "search_ph": "Поиск по постам", "search": "Поиск", "theme": "Сменить тему",
        "post_ph": "Написать пост (до 1000 символов)",
        "comment_ph": "Написать комментарий", "reply_ph": "Ответить на комментарий",
        "publish": "Опубликовать", "send_comment": "Отправить",
        "reply": "Ответить", "cancel_reply": "Отмена",
        "no_posts": "Постов пока нет", "not_found": "Не найдено",
        "just_now": "только что", "sec_ago": "с", "min_ago": "мин", "hour_ago": "ч", "day_ago": "д",
        "read_more": "читать дальше", "copy": "Копировать", "copied": "Скопировано",
        "edit": "Редактировать", "delete": "Удалить", "save": "Сохранить",
        "cancel": "Отмена", "confirm_delete": "Удалить без возможности восстановления?",
        "author_badge": "автор",
        "f_new": "Новые", "f_top": "Лучшие", "f_bottom": "Худшие", "f_old": "Старые",
        "f_all": "Все", "f_many": "Много комм.", "f_some": "Есть комм.", "f_none": "Без комм.",
        "nav_home": "Главная", "nav_users": "Люди", "nav_messages": "Сообщения",
        "nav_profile": "Профиль", "nav_notifications": "Уведомления",
        "nav_settings": "Настройки", "nav_logout": "Выйти",
        "nav_register": "Регистрация", "nav_login": "Вход",
        "reg_title": "Регистрация", "log_title": "Вход",
        "name_ph": "Имя", "nick_ph": "Ник (@nick)",
        "pass_ph": "Пароль", "pass2_ph": "Повтор пароля",
        "reg_btn": "Создать аккаунт", "log_btn": "Войти",
        "to_login": "Уже есть аккаунт? Войти", "to_reg": "Нет аккаунта? Регистрация",
        "err_bad_nick": "Ник: 3-20 символов, латиница, цифры, _",
        "err_bad_name": "Имя: от 1 до 50 символов",
        "err_short_pass": "Пароль: минимум 6 символов",
        "err_pass_mismatch": "Пароли не совпадают",
        "err_nick_taken": "Ник уже занят",
        "err_bad_login": "Неверный ник или пароль",
        "err_auth_required": "Требуется вход",
        "err_private_followers": "Пользователь скрыл своих подписчиков",
        "err_private_following": "Пользователь скрыл свои подписки",
        "login_to_post": "Войдите, чтобы писать посты",
        "login_to_comment": "Войдите, чтобы писать комментарии",
        "login_to_dm": "Войдите, чтобы писать сообщения",
        "go_login": "Войти", "go_register": "Регистрация",
        "profile_followers": "подписчиков", "profile_following": "подписок",
        "follow": "Подписаться", "unfollow": "Отписаться",
        "message": "Написать",
        "own_profile": "Это ваш профиль", "no_user_posts": "Постов пока нет",
        "settings_title": "Настройки",
        "settings_appearance": "Оформление", "settings_theme": "Тема", "settings_lang": "Язык",
        "settings_privacy": "Конфиденциальность",
        "settings_allow_followers": "Разрешить просматривать подписчиков",
        "settings_allow_following": "Разрешить просматривать подписки",
        "settings_info": "Инфо",
        "settings_policy": "Политика конфиденциальности",
        "settings_desc": "sldChat — минималистичная соцсеть: посты, комментарии, апвоуты, подписки и личные сообщения.",
        "settings_authors": "Авторы",
        "theme_light": "Светлая", "theme_dark": "Тёмная",
        "notif_title": "Уведомления", "notif_empty": "Уведомлений нет",
        "notif_follow": "подписался(-ась) на вас",
        "notif_comment": "оставил(а) комментарий",
        "notif_reply": "ответил(а) на ваш комментарий",
        "notif_mention": "упомянул(а) вас",
        "notif_clear": "Очистить все", "back_to_main": "В главное меню",
        "bio_ph": "Описание профиля...", "bio_save": "Сохранить", "bio_saved": "Сохранено",
        "bio_empty": "Описание пока не заполнено", "edit_bio": "Редактировать",
        "followers_title": "Подписчики", "following_title": "Подписки",
        "no_followers": "Подписчиков пока нет", "no_following": "Подписок пока нет",
        "users_title": "Пользователи", "users_search_ph": "Поиск по нику или имени",
        "no_users": "Никого не найдено",
        "policy_title": "Политика конфиденциальности",
        "policy_content": (
            "1. Мы храним минимум данных: имя, ник, пароль (в виде pbkdf2-хеша), посты, "
            "комментарии, голоса, подписки, сообщения и уведомления.\n\n"
            "2. Пароль хранится только в виде хеша pbkdf2-hmac-sha256 (100 000 итераций) с солью. "
            "Мы не можем его восстановить.\n\n"
            "3. Данные хранятся на серверах Supabase (Postgres). Мы не продаём и не передаём "
            "их третьим лицам.\n\n"
            "4. Приватность профиля управляется пользователем в Настройках: можно скрыть список "
            "подписчиков и подписок.\n\n"
            "5. Вы можете в любой момент удалить свои посты и комментарии — они исчезают из базы. "
            "Редактировать и удалять чужие посты и комментарии невозможно.\n\n"
            "6. Личные сообщения доступны только участникам диалога.\n\n"
            "7. Для определения языка интерфейса используется IP-геолокация (IP не сохраняется).\n\n"
            "8. Сервис предоставляется as-is без гарантий."
        ),
        "spinner": "Загрузка…",
        "dm_title": "Сообщения",
        "dm_empty": "Здесь будут ваши переписки",
        "dm_search_ph": "Поиск по перепискам",
        "dm_with": "Чат с",
        "dm_send_ph": "Написать сообщение...",
        "dm_send": "Отправить",
        "dm_no_messages": "Напишите первое сообщение",
        "dm_send_first": "Написать сообщение",
        "err_dm_self": "Нельзя написать самому себе",
    },
    "en": {
        "search_ph": "Search posts", "search": "Search", "theme": "Toggle theme",
        "post_ph": "Write a post (up to 1000 chars)",
        "comment_ph": "Write a comment", "reply_ph": "Reply to comment",
        "publish": "Publish", "send_comment": "Send",
        "reply": "Reply", "cancel_reply": "Cancel",
        "no_posts": "No posts yet", "not_found": "Not found",
        "just_now": "just now", "sec_ago": "s", "min_ago": "min", "hour_ago": "h", "day_ago": "d",
        "read_more": "read more", "copy": "Copy", "copied": "Copied",
        "edit": "Edit", "delete": "Delete", "save": "Save",
        "cancel": "Cancel", "confirm_delete": "Delete permanently?",
        "author_badge": "author",
        "f_new": "New", "f_top": "Top", "f_bottom": "Worst", "f_old": "Old",
        "f_all": "All", "f_many": "Many", "f_some": "Some", "f_none": "None",
        "nav_home": "Home", "nav_users": "People", "nav_messages": "Messages",
        "nav_profile": "Profile", "nav_notifications": "Notifications",
        "nav_settings": "Settings", "nav_logout": "Log out",
        "nav_register": "Sign up", "nav_login": "Log in",
        "reg_title": "Sign up", "log_title": "Log in",
        "name_ph": "Name", "nick_ph": "Nick (@nick)",
        "pass_ph": "Password", "pass2_ph": "Confirm password",
        "reg_btn": "Create account", "log_btn": "Log in",
        "to_login": "Already have an account? Log in", "to_reg": "No account? Sign up",
        "err_bad_nick": "Nick: 3-20 chars, letters/digits/_",
        "err_bad_name": "Name: 1-50 chars",
        "err_short_pass": "Password: min 6 chars",
        "err_pass_mismatch": "Passwords do not match",
        "err_nick_taken": "Nick already taken",
        "err_bad_login": "Wrong nick or password",
        "err_auth_required": "Login required",
        "err_private_followers": "User hid their followers",
        "err_private_following": "User hid their following",
        "login_to_post": "Log in to write posts",
        "login_to_comment": "Log in to write comments",
        "login_to_dm": "Log in to send messages",
        "go_login": "Log in", "go_register": "Sign up",
        "profile_followers": "followers", "profile_following": "following",
        "follow": "Follow", "unfollow": "Unfollow",
        "message": "Message",
        "own_profile": "This is your profile", "no_user_posts": "No posts yet",
        "settings_title": "Settings",
        "settings_appearance": "Appearance", "settings_theme": "Theme", "settings_lang": "Language",
        "settings_privacy": "Privacy",
        "settings_allow_followers": "Allow viewing followers",
        "settings_allow_following": "Allow viewing following",
        "settings_info": "Info",
        "settings_policy": "Privacy policy",
        "settings_desc": "sldChat is a minimalist social network: posts, comments, upvotes, follows and direct messages.",
        "settings_authors": "Authors",
        "theme_light": "Light", "theme_dark": "Dark",
        "notif_title": "Notifications", "notif_empty": "No notifications",
        "notif_follow": "followed you",
        "notif_comment": "commented",
        "notif_reply": "replied to your comment",
        "notif_mention": "mentioned you",
        "notif_clear": "Clear all", "back_to_main": "Back to main",
        "bio_ph": "Profile bio...", "bio_save": "Save", "bio_saved": "Saved",
        "bio_empty": "No bio yet", "edit_bio": "Edit",
        "followers_title": "Followers", "following_title": "Following",
        "no_followers": "No followers yet", "no_following": "No following yet",
        "users_title": "Users", "users_search_ph": "Search by nick or name",
        "no_users": "No users found",
        "policy_title": "Privacy Policy",
        "policy_content": (
            "1. We store minimum data: name, nick, password (pbkdf2 hash), posts, comments, "
            "votes, follows, messages and notifications.\n\n"
            "2. Password is stored only as a pbkdf2-hmac-sha256 hash (100 000 iterations) with a salt. "
            "We cannot recover it.\n\n"
            "3. Data is stored on Supabase (Postgres) servers. We do not sell or share it with third parties.\n\n"
            "4. Profile privacy is controlled by the user in Settings: you can hide followers and following.\n\n"
            "5. You can delete your posts and comments at any time — they are removed from the database. "
            "You cannot edit or delete other users' posts and comments.\n\n"
            "6. Direct messages are visible only to conversation participants.\n\n"
            "7. IP geolocation is used to determine the interface language (IP is not stored).\n\n"
            "8. The service is provided as-is without warranty."
        ),
        "spinner": "Loading…",
        "dm_title": "Messages",
        "dm_empty": "Your conversations will appear here",
        "dm_search_ph": "Search conversations",
        "dm_with": "Chat with",
        "dm_send_ph": "Write a message...",
        "dm_send": "Send",
        "dm_no_messages": "Send the first message",
        "dm_send_first": "Send a message",
        "err_dm_self": "Cannot message yourself",
    },
}


def svg(paths: str, size: int = 16, sw: float = 2) -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="{sw}" stroke-linecap="round" '
        f'stroke-linejoin="round">{paths}</svg>'
    )


ICON_SEARCH = svg('<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>')
ICON_MOON = svg('<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>')
ICON_SUN = svg(
    '<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/>'
    '<line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/>'
    '<line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/>'
    '<line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/>'
    '<line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/>')
ICON_BACK = svg('<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>')
ICON_UP = svg('<polyline points="6 15 12 9 18 15"/>', size=12, sw=2.5)
ICON_DOWN = svg('<polyline points="6 9 12 15 18 9"/>', size=12, sw=2.5)
ICON_COMMENT = svg(
    '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
    size=13)
ICON_COPY = svg('<rect x="9" y="9" width="12" height="12"/><path d="M5 15H3V3h12v2"/>', size=13)
ICON_CHECK = svg('<polyline points="20 6 9 17 4 12"/>', size=13, sw=2.5)
ICON_EDIT = svg(
    '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>', size=13)
ICON_HOME = svg('<path d="M3 10l9-7 9 7v11a2 2 0 0 1-2 2h-4v-8h-6v8H5a2 2 0 0 1-2-2z"/>', size=15)
ICON_USERS = svg(
    '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/>'
    '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>'
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/>', size=15)
ICON_USER = svg('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>', size=15)
ICON_MAIL = svg(
    '<rect x="3" y="5" width="18" height="14" rx="0" ry="0"/>'
    '<polyline points="3 7 12 13 21 7"/>', size=15)
ICON_BELL = svg(
    '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>'
    '<path d="M13.7 21a2 2 0 0 1-3.4 0"/>', size=15)
ICON_GEAR = svg(
    '<circle cx="12" cy="12" r="3"/>'
    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    size=15)
ICON_LOGOUT = svg(
    '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
    '<polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>', size=15)
ICON_PLUS = svg('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>', size=15)
ICON_LOGIN = svg(
    '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>'
    '<polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>', size=15)
ICON_TRASH = svg(
    '<polyline points="3 6 5 6 21 6"/>'
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
    '<path d="M10 11v6M14 11v6"/>', size=14)

FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' fill='%23101010'/%3E"
    "%3Cpath d='M14 16h36v24H28l-14 12V16z' fill='%23ffffff'/%3E"
    "%3C/svg%3E"
)


CSS = """
:root, [data-theme="light"] {
  --bg:#ebebeb; --card:#ffffff; --line:#d4d4d4; --line-strong:#a8a8a8;
  --text:#101010; --muted:#767676; --hover:#f0f0f0;
  --accent:#101010; --accent-fg:#ffffff;
  --up:#1f9d55; --down:#d84343; --comment-bg:#f6f6f6;
  --danger:#d84343; --mention:#3b7dd8;
  --bubble-mine:#101010; --bubble-mine-fg:#ffffff;
  --bubble-theirs:#f0f0f0; --bubble-theirs-fg:#101010;
}
[data-theme="dark"] {
  --bg:#0a0a0a; --card:#141414; --line:#282828; --line-strong:#3a3a3a;
  --text:#ececec; --muted:#888888; --hover:#1e1e1e;
  --accent:#ececec; --accent-fg:#101010;
  --up:#2ecc71; --down:#e74c3c; --comment-bg:#1c1c1c;
  --danger:#e74c3c; --mention:#6aa9ff;
  --bubble-mine:#ececec; --bubble-mine-fg:#101010;
  --bubble-theirs:#1e1e1e; --bubble-theirs-fg:#ececec;
}
* { box-sizing: border-box; }
html, body { height: 100vh; margin: 0; padding: 0; overflow: hidden; }
body {
  font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg); color: var(--text); font-size: 14px;
  -webkit-font-smoothing: antialiased;
  user-select: none; -webkit-user-select: none; -ms-user-select: none;
  display: flex;
}
input, textarea { user-select: text; -webkit-user-select: text; -ms-user-select: text; }
* { scrollbar-width: thin; scrollbar-color: var(--line-strong) transparent; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--line-strong); border: 2px solid var(--card); }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }
::-webkit-scrollbar-corner { background: transparent; }

.layout { display: flex; width: 100%; height: 100vh; background: var(--card); }
.main { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; background: var(--card); }
.main-header {
  flex: 0 0 auto; display: flex; align-items: center; gap: 6px;
  padding: 10px 14px; border-bottom: 1px solid var(--line);
  min-height: 52px;
}
.main-header .title {
  flex: 1; font-size: 15px; font-weight: 600; padding: 0 6px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.main-body { flex: 1 1 auto; overflow-y: auto; overflow-x: hidden; }

.sidebar {
  flex: 0 0 264px; width: 264px; border-left: 1px solid var(--line);
  background: var(--card); display: flex; flex-direction: column;
  padding: 18px 12px 12px;
}
.sidebar .logo {
  font-size: 20px; font-weight: 700; letter-spacing: -0.5px;
  padding: 4px 12px 22px; user-select: none;
}
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-btn {
  display: flex; align-items: center; gap: 12px;
  width: 100%; padding: 10px 12px; border: none; background: transparent;
  color: var(--text); font: inherit; font-size: 14px;
  cursor: pointer; text-align: left; transition: background .12s;
  position: relative;
}
.nav-btn:hover { background: var(--hover); }
.nav-btn.active { background: var(--hover); font-weight: 600; }
.nav-btn svg { flex-shrink: 0; color: var(--muted); }
.nav-btn.active svg { color: var(--text); }
.nav-btn .badge {
  margin-left: auto; min-width: 18px; height: 18px;
  background: var(--danger); color: #fff;
  font-size: 11px; font-weight: 700; line-height: 18px;
  text-align: center; padding: 0 5px;
}
.sidebar .spacer { flex: 1; }
.sidebar-user {
  padding: 12px; font-size: 13px; color: var(--muted);
  border-top: 1px solid var(--line);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  display: flex; align-items: center; gap: 8px;
}
.sidebar-user .dot {
  width: 8px; height: 8px; background: var(--up); flex-shrink: 0;
}

.icon-btn {
  width: 32px; height: 32px; display: inline-flex; align-items: center; justify-content: center;
  background: transparent; border: 1px solid var(--line);
  color: var(--text); cursor: pointer; padding: 0; text-decoration: none;
  transition: background .12s, border-color .12s; flex-shrink: 0;
}
.icon-btn:hover { background: var(--hover); border-color: var(--line-strong); }
.icon-btn svg { display: block; }

header.search-header {
  flex: 0 0 auto; display: flex; align-items: center; gap: 6px;
  padding: 10px 14px; border-bottom: 1px solid var(--line);
}
header.search-header input[type="search"], header.search-header input[type="text"] {
  flex: 1; min-width: 0; padding: 0 12px; height: 32px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-size: 14px; font-family: inherit;
  outline: none; transition: border-color .12s;
}
header.search-header input:focus { border-color: var(--line-strong); }

.filters {
  flex: 0 0 auto; display: flex; align-items: center;
  padding: 6px 10px; border-bottom: 1px solid var(--line);
  overflow-x: auto; scrollbar-width: none;
}
.filters::-webkit-scrollbar { display: none; }
.filter-group { display: flex; align-items: center; gap: 1px; flex-shrink: 0; }
.filter-sep { width: 1px; height: 16px; background: var(--line); margin: 0 8px; flex-shrink: 0; }
.filter {
  height: 26px; padding: 0 10px; border: none; background: transparent;
  color: var(--muted); font-family: inherit; font-size: 12px; font-weight: 500;
  cursor: pointer; white-space: nowrap; transition: color .12s, background .12s;
}
.filter:hover { background: var(--hover); color: var(--text); }
.filter.active { color: var(--text); background: var(--hover); }

.empty { padding: 80px 20px; text-align: center; color: var(--muted); font-size: 13px; }

.spinner-wrap { padding: 60px 0; text-align: center; }
.spinner {
  display: inline-block; width: 24px; height: 24px;
  border: 2px solid var(--line); border-top-color: var(--text);
  animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.post { padding: 16px 20px; border-bottom: 1px solid var(--line); background: var(--card); }
.post-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 12px; }
.post-author { font-weight: 600; color: var(--text); text-decoration: none; font-size: 13px; }
.post-author:hover { text-decoration: underline; }
.post-time { color: var(--muted); font-size: 12px; margin-left: auto; }
.post-text {
  font-size: 15px; line-height: 1.55; white-space: pre-wrap;
  word-wrap: break-word; overflow-wrap: anywhere; color: var(--text);
}
.mention { color: var(--mention); text-decoration: none; font-weight: 500; }
.mention:hover { text-decoration: underline; }
.read-more {
  display: inline-block; margin-top: 8px; color: var(--muted);
  text-decoration: none; font-size: 13px; border-bottom: 1px dashed currentColor;
}
.read-more:hover { color: var(--text); }
.post-actions {
  display: flex; align-items: center; gap: 2px;
  margin-top: 12px; font-size: 12px; color: var(--muted);
}
.vote-btn, .action-btn {
  display: inline-flex; align-items: center; gap: 5px;
  height: 28px; padding: 0 9px; background: transparent; border: none;
  color: var(--muted); cursor: pointer; font-family: inherit; font-size: 12px; font-weight: 500;
  transition: color .12s, background .12s;
}
.vote-btn:hover, .action-btn:hover { background: var(--hover); }
.vote-btn svg, .action-btn svg { display: block; }
.vote-btn.up:hover { color: var(--up); }
.vote-btn.down:hover { color: var(--down); }
.vote-btn.up.active { color: var(--up); }
.vote-btn.down.active { color: var(--down); }
.action-btn:hover { color: var(--text); }
.action-btn.copied { color: var(--up); }
.action-btn.danger:hover { color: var(--danger); }
.score {
  min-width: 20px; padding: 0 3px; text-align: center;
  font-weight: 600; font-size: 12px; color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.score.up { color: var(--up); }
.score.down { color: var(--down); }

.comments { margin-top: 14px; border-top: 1px solid var(--line); }
.comment {
  padding: 12px 14px; margin-top: 10px;
  background: var(--comment-bg); border-left: 2px solid var(--line-strong);
}
.comment.reply {
  margin-left: 32px; border-left-color: var(--muted);
  background: transparent; border: 1px solid var(--line); border-left-width: 2px;
}
.comment.is-author { border-left-color: #fff; box-shadow: 0 0 0 1px #fff inset; }
[data-theme="light"] .comment.is-author {
  border-left-color: #101010; box-shadow: 0 0 0 1px #101010 inset;
}
.comment-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
.comment-author { font-size: 12px; font-weight: 600; color: var(--text); text-decoration: none; }
.comment-author:hover { text-decoration: underline; }
.comment-author-badge {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .5px; padding: 1px 6px; border: 1px solid currentColor; color: var(--text);
}
.comment-time { font-size: 11px; color: var(--muted); margin-left: auto; }
.comment-text { font-size: 13px; line-height: 1.55; white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; color: var(--text); }
.comment-actions { display: flex; align-items: center; gap: 2px; margin-top: 8px; font-size: 11px; color: var(--muted); }
.comment-actions .vote-btn { height: 22px; padding: 0 6px; font-size: 11px; }
.comment-actions .score { font-size: 11px; min-width: 14px; }
.comment-reply-btn, .comment-edit-btn {
  background: transparent; border: none; color: var(--muted);
  font-family: inherit; font-size: 11px; cursor: pointer;
  padding: 4px 8px; height: 22px; display: inline-flex; align-items: center; gap: 4px;
  transition: color .12s, background .12s;
}
.comment-reply-btn:hover, .comment-edit-btn:hover { color: var(--text); background: var(--hover); }
.comment-edit-btn.danger:hover { color: var(--danger); }
.comment-edit-btn svg { display: block; }

.inline-editor { margin-top: 8px; }
.inline-editor textarea {
  display: block; width: 100%; padding: 10px 12px; min-height: 80px;
  border: 1px solid var(--line-strong); background: var(--card);
  color: var(--text); font-family: inherit; font-size: 14px; line-height: 1.5;
  outline: none; resize: none;
}
.inline-editor .edit-actions {
  display: flex; justify-content: flex-end; gap: 6px; margin-top: 6px;
}
.inline-editor .edit-actions button {
  height: 30px; padding: 0 14px; font-family: inherit; font-size: 13px; cursor: pointer;
  border: 1px solid var(--line); background: transparent; color: var(--text);
}
.inline-editor .edit-actions button:hover { background: var(--hover); }
.inline-editor .edit-actions button.edit-save {
  background: var(--accent); color: var(--accent-fg); border-color: var(--accent);
}

.composer { flex: 0 0 auto; border-top: 1px solid var(--line); background: var(--card); padding: 12px 14px; }
.composer textarea {
  display: block; width: 100%; min-height: 140px; padding: 12px 14px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-family: inherit; font-size: 14px; line-height: 1.5;
  outline: none; resize: none; transition: border-color .12s;
}
.composer.composer-comment textarea { min-height: 84px; }
.composer textarea:focus { border-color: var(--line-strong); }
.composer textarea::placeholder { color: var(--muted); }
.composer textarea:disabled { opacity: .5; cursor: not-allowed; }
.composer-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 10px; }
.composer-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.counter { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.counter.warn { color: var(--danger); }
.reply-banner {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  background: var(--comment-bg); border: 1px solid var(--line);
  font-size: 12px; color: var(--muted); margin-bottom: 8px;
}
.reply-banner b { color: var(--text); font-weight: 600; }
.reply-banner button {
  margin-left: auto; background: transparent; border: none;
  color: var(--text); font-family: inherit; font-size: 12px;
  cursor: pointer; padding: 2px 8px;
}
.reply-banner button:hover { background: var(--hover); }

button.send {
  height: 34px; padding: 0 20px;
  background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent);
  font-family: inherit; font-size: 13px; font-weight: 500;
  cursor: pointer; transition: opacity .12s;
}
button.send:hover { opacity: .85; }
button.send:disabled { opacity: .3; cursor: default; }
.login-prompt { padding: 16px; text-align: center; color: var(--muted); font-size: 13px; }
.login-prompt a { color: var(--text); }

.auth-wrap { max-width: 380px; margin: 0 auto; padding: 40px 20px; }
.auth-title { font-size: 22px; font-weight: 700; margin: 0 0 24px; letter-spacing: -0.5px; }
.auth-form { display: flex; flex-direction: column; gap: 10px; }
.auth-form input {
  width: 100%; padding: 12px 14px; border: 1px solid var(--line); background: transparent;
  color: var(--text); font-family: inherit; font-size: 14px;
  outline: none; transition: border-color .12s;
}
.auth-form input:focus { border-color: var(--line-strong); }
.auth-form button {
  margin-top: 6px; height: 42px; background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent);
  font-family: inherit; font-size: 14px; font-weight: 600;
  cursor: pointer; transition: opacity .12s;
}
.auth-form button:hover { opacity: .85; }
.auth-form button:disabled { opacity: .5; cursor: default; }
.auth-error { color: var(--danger); font-size: 13px; min-height: 18px; }
.auth-switch { margin-top: 20px; font-size: 13px; color: var(--muted); text-align: center; }
.auth-switch a { color: var(--text); cursor: pointer; text-decoration: underline; }

.profile-header { padding: 24px 20px; border-bottom: 1px solid var(--line); }
.profile-name-big { font-size: 22px; font-weight: 700; letter-spacing: -0.4px; }
.profile-nick-small { color: var(--muted); font-size: 14px; margin-top: 3px; }
.profile-bio {
  margin-top: 14px; font-size: 14px; color: var(--text); line-height: 1.55;
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
}
.profile-bio-empty { color: var(--muted); font-style: italic; }
.profile-stats {
  display: flex; gap: 20px; margin-top: 16px; font-size: 13px; color: var(--muted);
  user-select: none;
}
.profile-stats b { color: var(--text); font-weight: 600; cursor: pointer; }
.profile-stats b:hover { text-decoration: underline; }
.profile-actions { margin-top: 18px; display: flex; gap: 8px; flex-wrap: wrap; }
.follow-btn {
  height: 34px; padding: 0 20px; background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent);
  font-family: inherit; font-size: 13px; font-weight: 600;
  cursor: pointer; transition: opacity .12s;
  display: inline-flex; align-items: center; gap: 8px;
}
.follow-btn:hover { opacity: .85; }
.follow-btn.following { background: transparent; color: var(--text); border-color: var(--line-strong); }
.follow-btn.secondary { background: transparent; color: var(--text); border-color: var(--line-strong); }
.own-note { font-size: 13px; color: var(--muted); }

.bio-editor { margin-top: 14px; }
.bio-editor textarea {
  width: 100%; padding: 12px; min-height: 72px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-family: inherit; font-size: 14px; line-height: 1.5;
  outline: none; resize: none;
}
.bio-editor textarea:focus { border-color: var(--line-strong); }
.bio-editor-row { display: flex; gap: 8px; margin-top: 8px; align-items: center; }
.btn-secondary {
  height: 34px; padding: 0 16px; background: transparent; color: var(--text);
  border: 1px solid var(--line);
  font-family: inherit; font-size: 13px; cursor: pointer;
  transition: background .12s, border-color .12s;
}
.btn-secondary:hover { background: var(--hover); border-color: var(--line-strong); }
.bio-saved-msg { font-size: 12px; color: var(--up); }

.user-list { padding: 6px 0; }
.user-item { display: flex; align-items: center; gap: 12px; padding: 14px 20px; border-bottom: 1px solid var(--line); }
.user-item .user-info { flex: 1; min-width: 0; }
.user-item .user-nick {
  font-weight: 600; color: var(--text); text-decoration: none;
  font-size: 14px; display: block;
}
.user-item .user-nick:hover { text-decoration: underline; }
.user-item .user-name { font-size: 12px; color: var(--muted); margin-top: 2px; }

.notif-list { padding: 6px 0; }
.notif {
  display: block; padding: 14px 20px; border-bottom: 1px solid var(--line);
  font-size: 14px; line-height: 1.5; text-decoration: none; color: inherit;
}
.notif.unread { background: var(--hover); }
.notif-head { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.notif-author { font-weight: 600; color: var(--text); }
.notif-text { color: var(--muted); font-size: 13px; }
.notif-snippet {
  margin-top: 6px; padding: 8px 10px; background: var(--comment-bg); border-left: 2px solid var(--line-strong);
  font-size: 13px; color: var(--text);
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
}
.notif-time { font-size: 11px; color: var(--muted); margin-top: 4px; }
.notif-actions { padding: 12px 20px; border-bottom: 1px solid var(--line); display: flex; justify-content: flex-end; }
.btn-danger {
  height: 32px; padding: 0 14px; background: transparent; color: var(--danger);
  border: 1px solid var(--danger);
  font-family: inherit; font-size: 12px; cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px;
  transition: background .12s;
}
.btn-danger:hover { background: var(--danger); color: var(--card); }

.settings { padding: 24px 20px; max-width: 640px; }
.settings h2 {
  font-size: 12px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .6px; color: var(--muted); margin: 0 0 12px;
}
.settings-section { margin-bottom: 36px; }
.opt-row { display: flex; gap: 6px; flex-wrap: wrap; }
.opt {
  height: 34px; padding: 0 18px; background: transparent; color: var(--text);
  border: 1px solid var(--line); font-family: inherit; font-size: 13px;
  cursor: pointer; transition: border-color .12s, background .12s;
}
.opt:hover { background: var(--hover); }
.opt.active { background: var(--hover); border-color: var(--line-strong); font-weight: 600; }

.toggle-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 0; border-bottom: 1px solid var(--line);
  font-size: 14px;
}
.toggle-row:last-child { border-bottom: none; }
.toggle {
  position: relative; width: 40px; height: 22px;
  background: var(--line); cursor: pointer; transition: background .15s;
  border: 1px solid var(--line); flex-shrink: 0;
}
.toggle::after {
  content: ''; position: absolute; left: 1px; top: 1px;
  width: 16px; height: 16px; background: var(--card);
  transition: transform .15s;
}
.toggle.on { background: var(--up); border-color: var(--up); }
.toggle.on::after { transform: translateX(18px); background: #fff; }

.settings-desc { font-size: 13px; line-height: 1.65; color: var(--muted); margin: 0 0 14px; }
.settings-link {
  display: inline-block; color: var(--text); text-decoration: underline;
  font-size: 13px; cursor: pointer;
}
.settings-authors { font-size: 14px; color: var(--text); }

.policy { padding: 24px; max-width: 720px; }
.policy h1 { font-size: 20px; margin: 0 0 18px; font-weight: 700; }
.policy p { font-size: 14px; line-height: 1.7; color: var(--text); white-space: pre-wrap; margin: 0; }

.dm-list { padding: 6px 0; }
.dm-thread {
  display: flex; gap: 12px; padding: 14px 20px; border-bottom: 1px solid var(--line);
  text-decoration: none; color: inherit; transition: background .12s;
}
.dm-thread:hover { background: var(--hover); }
.dm-thread .info { flex: 1; min-width: 0; }
.dm-thread .who { font-weight: 600; font-size: 14px; display: flex; gap: 8px; align-items: baseline; }
.dm-thread .preview { font-size: 13px; color: var(--muted); margin-top: 3px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dm-thread .meta { flex-shrink: 0; display: flex; flex-direction: column;
  align-items: flex-end; gap: 6px; }
.dm-thread .time { font-size: 11px; color: var(--muted); }
.dm-thread .unread-badge {
  min-width: 20px; height: 20px; line-height: 20px;
  background: var(--danger); color: #fff;
  font-size: 11px; font-weight: 700; text-align: center; padding: 0 6px;
}
.dm-thread .mine-label { font-size: 11px; color: var(--muted); }

.chat { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.chat-body {
  flex: 1; overflow-y: auto; overflow-x: hidden;
  padding: 18px 20px; background: var(--card);
}
.chat-intro {
  text-align: center; color: var(--muted); font-size: 13px;
  padding: 20px 0;
}
.chat-msg { max-width: 72%; margin-bottom: 10px; padding: 9px 13px;
  word-wrap: break-word; overflow-wrap: anywhere; white-space: pre-wrap;
  font-size: 14px; line-height: 1.45; }
.chat-msg.mine { margin-left: auto; background: var(--bubble-mine); color: var(--bubble-mine-fg); }
.chat-msg.theirs { margin-right: auto; background: var(--bubble-theirs); color: var(--bubble-theirs-fg); }
.chat-msg .chat-time { font-size: 10px; opacity: .65; margin-top: 4px; }
.chat-composer {
  flex: 0 0 auto; border-top: 1px solid var(--line); padding: 10px 14px;
  display: flex; gap: 10px; align-items: flex-end; background: var(--card);
}
.chat-composer textarea {
  flex: 1; min-height: 40px; max-height: 160px; padding: 10px 12px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-family: inherit; font-size: 14px; line-height: 1.4;
  outline: none; resize: none; transition: border-color .12s;
}
.chat-composer textarea:focus { border-color: var(--line-strong); }
.chat-composer button {
  height: 40px; padding: 0 18px;
  background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent);
  font-family: inherit; font-size: 13px; font-weight: 500;
  cursor: pointer; transition: opacity .12s;
}
.chat-composer button:hover { opacity: .85; }
.chat-composer button:disabled { opacity: .3; cursor: default; }

@media (max-width: 900px) {
  body { font-size: 15px; }
  .layout { flex-direction: column; }
  .main { order: 1; height: calc(100vh - 58px); }
  .sidebar {
    order: 2; width: 100%; height: 58px;
    flex-direction: row; border-left: none;
    border-top: 1px solid var(--line);
    padding: 0;
    flex: 0 0 58px;
  }
  .sidebar .logo { display: none; }
  .sidebar .spacer { display: none; }
  .sidebar-user { display: none; }
  .nav {
    flex-direction: row; flex: 1;
    justify-content: space-around; align-items: stretch;
    gap: 0;
  }
  .nav-btn {
    flex-direction: column; gap: 3px; padding: 6px 4px;
    flex: 1; justify-content: center; align-items: center;
    text-align: center;
  }
  .nav-btn span { font-size: 10px; line-height: 1; }
  .nav-btn svg { width: 20px; height: 20px; }
  .nav-btn .badge {
    position: absolute; top: 4px; right: 20%;
    margin: 0; min-width: 16px; height: 16px;
    line-height: 16px; font-size: 10px; padding: 0 4px;
  }
  .main-header { min-height: 46px; padding: 8px 12px; }
  .main-header .title { font-size: 14px; }
  .post { padding: 14px 16px; }
  .post-text { font-size: 15px; }
  .composer { padding: 10px 12px; }
  .composer textarea { min-height: 90px; }
  .composer.composer-comment textarea { min-height: 70px; }
  .profile-header { padding: 20px 16px; }
  .chat-body { padding: 14px 14px; }
  .chat-msg { max-width: 85%; }
  .chat-composer { padding: 8px 12px; gap: 8px; }
  .chat-composer button { padding: 0 14px; }
  .user-item { padding: 12px 16px; }
  .notif { padding: 12px 16px; }
  .settings { padding: 20px 16px; }
  .dm-thread { padding: 12px 16px; }
}
"""


JS = r"""
var ICONS = {
  up: __ICON_UP__, down: __ICON_DOWN__, comment: __ICON_COMMENT__,
  copy: __ICON_COPY__, check: __ICON_CHECK__, edit: __ICON_EDIT__,
  moon: __ICON_MOON__, sun: __ICON_SUN__, back: __ICON_BACK__,
  home: __ICON_HOME__, users: __ICON_USERS__, user: __ICON_USER__,
  mail: __ICON_MAIL__, bell: __ICON_BELL__, gear: __ICON_GEAR__,
  logout: __ICON_LOGOUT__, plus: __ICON_PLUS__, login: __ICON_LOGIN__,
  trash: __ICON_TRASH__, search: __ICON_SEARCH__
};

var cachedUser = null;
try { cachedUser = JSON.parse(localStorage.getItem('sldchat_user') || 'null'); } catch(e) { cachedUser = null; }

var state = {
  user: cachedUser,
  token: localStorage.getItem('sldchat_token') || null,
  view: VIEW,
  viewData: VIEW_DATA || {},
  unreadNotif: 0,
  unreadDM: 0,
  sortMode: 'new',
  commentFilter: 'any',
  searchQuery: '',
  usersQuery: '',
  dmQuery: '',
  composerDraft: '',
  replyTo: null,
  highlightComment: null,
  suppressRefresh: 0
};

function setUser(u) {
  state.user = u;
  if (u) localStorage.setItem('sldchat_user', JSON.stringify(u));
  else localStorage.removeItem('sldchat_user');
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  if (state.token) opts.headers['X-Auth'] = state.token;
  if (opts.body && typeof opts.body === 'object') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  var r = await fetch(path, opts);
  if (r.status === 401) {
    setUser(null); state.token = null;
    localStorage.removeItem('sldchat_token');
    throw new Error('unauthorized');
  }
  if (!r.ok) {
    var err = {};
    try { err = await r.json(); } catch(e) {}
    throw new Error(err.detail || 'error');
  }
  return r.json();
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function linkifyMentions(escaped) {
  return escaped.replace(/(^|[^a-zA-Z0-9_])@([a-zA-Z0-9_]{3,20})/g, function(_, pre, nick){
    return pre + '<a class="mention" href="/u/' + encodeURIComponent(nick) + '" data-link>@' + nick + '</a>';
  });
}
function tr(k) { return T[k] || k; }
function timeAgo(ts) {
  var d = Math.floor(Date.now()/1000 - ts);
  if (d < 5) return tr('just_now');
  if (d < 60) return d + ' ' + tr('sec_ago');
  var m = Math.floor(d/60);
  if (m < 60) return m + ' ' + tr('min_ago');
  var h = Math.floor(m/60);
  if (h < 24) return h + ' ' + tr('hour_ago');
  var days = Math.floor(h/24);
  if (days < 30) return days + ' ' + tr('day_ago');
  return new Date(ts*1000).toLocaleDateString();
}
function scoreClass(up, down) {
  var s = up - down; if (s > 0) return 'up'; if (s < 0) return 'down'; return '';
}
function truncateText(text) {
  var lines = text.split('\n');
  var out = text, truncated = false;
  if (lines.length > TRUNCATE_LINES) { out = lines.slice(0, TRUNCATE_LINES).join('\n'); truncated = true; }
  if (out.length > TRUNCATE_CHARS) { out = out.slice(0, TRUNCATE_CHARS); truncated = true; }
  if (truncated) out = out.replace(/\s+$/, '') + '…';
  return { text: out, truncated: truncated };
}
function spinner() { return '<div class="spinner-wrap"><div class="spinner"></div></div>'; }

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('sldchat_theme', theme);
  var el = document.getElementById('mainThemeBtn');
  if (el) el.innerHTML = (theme === 'dark') ? ICONS.sun : ICONS.moon;
}
function toggleTheme() {
  var cur = document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}
applyTheme(localStorage.getItem('sldchat_theme') || 'light');

function navigate(url) {
  history.pushState({}, '', url);
  handleRoute();
}
function handleRoute() {
  var path = location.pathname;
  var m;
  if (path === '/' || path === '') { state.view = 'feed'; state.viewData = {}; }
  else if ((m = path.match(/^\/p\/([a-z0-9]+)$/))) {
    state.view = 'post';
    state.viewData = { post_id: m[1] };
    state.highlightComment = (location.hash && location.hash.match(/^#c-(.+)$/)) ? location.hash.slice(3) : null;
  }
  else if ((m = path.match(/^\/u\/([^\/]+)\/followers$/))) {
    state.view = 'followers'; state.viewData = { nick: decodeURIComponent(m[1]) };
  }
  else if ((m = path.match(/^\/u\/([^\/]+)\/following$/))) {
    state.view = 'following'; state.viewData = { nick: decodeURIComponent(m[1]) };
  }
  else if ((m = path.match(/^\/u\/(.+)$/))) {
    state.view = 'profile'; state.viewData = { nick: decodeURIComponent(m[1]) };
  }
  else if ((m = path.match(/^\/messages\/(.+)$/))) {
    state.view = 'chat'; state.viewData = { nick: decodeURIComponent(m[1]) };
  }
  else if (path === '/messages') { state.view = 'messages'; state.viewData = {}; }
  else if (path === '/users') { state.view = 'users'; state.viewData = {}; }
  else if (path === '/notifications') { state.view = 'notifications'; state.viewData = {}; }
  else if (path === '/settings') { state.view = 'settings'; state.viewData = {}; }
  else if (path === '/policy') { state.view = 'policy'; state.viewData = {}; }
  else if (path === '/register') { state.view = 'register'; state.viewData = {}; }
  else if (path === '/login') { state.view = 'login'; state.viewData = {}; }
  else { state.view = 'feed'; state.viewData = {}; }
  renderSidebar();
  renderMain();
}
window.addEventListener('popstate', handleRoute);

async function loadMe() {
  if (!state.token) return;
  try {
    var u = await api('/api/me');
    setUser(u);
  } catch(e) { setUser(null); }
}

async function doRegister(data) {
  var res = await api('/api/register', { method: 'POST', body: data });
  state.token = res.token;
  localStorage.setItem('sldchat_token', res.token);
  setUser(res.user);
  navigate('/');
}
async function doLogin(data) {
  var res = await api('/api/login', { method: 'POST', body: data });
  state.token = res.token;
  localStorage.setItem('sldchat_token', res.token);
  setUser(res.user);
  navigate('/');
}
async function doLogout() {
  try { await api('/api/logout', { method: 'POST' }); } catch(e) {}
  setUser(null);
  state.token = null;
  localStorage.removeItem('sldchat_token');
  navigate('/');
}

function navBtn(icon, label, active, action, count) {
  var cls = 'nav-btn' + (active ? ' active' : '');
  var badge = (count && count > 0) ? '<span class="badge">' + (count > 99 ? '99+' : count) + '</span>' : '';
  return '<button class="' + cls + '" data-nav="' + action + '" title="' + escapeHtml(label) + '">'
    + icon + '<span>' + escapeHtml(label) + '</span>' + badge + '</button>';
}
function renderSidebar() {
  var el = document.getElementById('sidebar');
  var html = '<div class="logo">sldchat</div><div class="nav">';
  html += navBtn(ICONS.home, tr('nav_home'), state.view === 'feed', 'home');
  html += navBtn(ICONS.users, tr('nav_users'), state.view === 'users', 'users');
  if (state.user) {
    html += navBtn(ICONS.mail, tr('nav_messages'), state.view === 'messages' || state.view === 'chat', 'messages', state.unreadDM);
    html += navBtn(ICONS.bell, tr('nav_notifications'), state.view === 'notifications', 'notifications', state.unreadNotif);
    html += navBtn(ICONS.user, tr('nav_profile'),
      state.view === 'profile' && state.viewData.nick === state.user.nick, 'profile');
    html += navBtn(ICONS.gear, tr('nav_settings'), state.view === 'settings', 'settings');
    html += navBtn(ICONS.logout, tr('nav_logout'), false, 'logout');
  } else {
    html += navBtn(ICONS.gear, tr('nav_settings'), state.view === 'settings', 'settings');
    html += navBtn(ICONS.plus, tr('nav_register'), state.view === 'register', 'register');
    html += navBtn(ICONS.login, tr('nav_login'), state.view === 'login', 'login');
  }
  html += '</div><div class="spacer"></div>';
  if (state.user) {
    html += '<div class="sidebar-user"><span class="dot"></span>@' + escapeHtml(state.user.nick) + '</div>';
  }
  el.innerHTML = html;
  el.querySelectorAll('[data-nav]').forEach(function(b){
    b.addEventListener('click', function(){
      var nav = b.dataset.nav;
      if (nav === 'home') navigate('/');
      else if (nav === 'users') navigate('/users');
      else if (nav === 'messages') navigate('/messages');
      else if (nav === 'profile') navigate('/u/' + encodeURIComponent(state.user.nick));
      else if (nav === 'notifications') navigate('/notifications');
      else if (nav === 'settings') navigate('/settings');
      else if (nav === 'register') navigate('/register');
      else if (nav === 'login') navigate('/login');
      else if (nav === 'logout') doLogout();
    });
  });
}

function renderMain() {
  var el = document.getElementById('main');
  if (state.view === 'feed') renderFeedView(el);
  else if (state.view === 'post') renderPostView(el);
  else if (state.view === 'profile') renderProfileView(el);
  else if (state.view === 'followers' || state.view === 'following') renderFollowListView(el);
  else if (state.view === 'users') renderUsersView(el);
  else if (state.view === 'messages') renderMessagesView(el);
  else if (state.view === 'chat') renderChatView(el);
  else if (state.view === 'notifications') renderNotificationsView(el);
  else if (state.view === 'settings') renderSettingsView(el);
  else if (state.view === 'policy') renderPolicyView(el);
  else if (state.view === 'register') renderRegisterView(el);
  else if (state.view === 'login') renderLoginView(el);
  else renderFeedView(el);
}
function attachThemeBtn() {
  var b = document.getElementById('mainThemeBtn');
  if (b) {
    b.innerHTML = (document.documentElement.getAttribute('data-theme') === 'dark') ? ICONS.sun : ICONS.moon;
    b.addEventListener('click', toggleTheme);
  }
}
function bindLinks(root) {
  root.querySelectorAll('[data-link]').forEach(function(a){
    if (a.dataset.linkBound) return;
    a.dataset.linkBound = '1';
    a.addEventListener('click', function(e){
      e.preventDefault();
      navigate(a.getAttribute('href'));
    });
  });
}

function renderFeedView(el) {
  var html = '';
  html += '<header class="search-header">';
  html += '<input id="search" type="search" placeholder="' + escapeHtml(tr('search_ph')) + '" autocomplete="off" spellcheck="false" value="' + escapeHtml(state.searchQuery) + '" />';
  html += '<button class="icon-btn" id="searchBtn" title="' + escapeHtml(tr('search')) + '">' + ICONS.search + '</button>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="filters" id="filters">';
  html += '<div class="filter-group">';
  html += '<button class="filter' + (state.sortMode==='new'?' active':'') + '" data-sort="new">' + tr('f_new') + '</button>';
  html += '<button class="filter' + (state.sortMode==='top'?' active':'') + '" data-sort="top">' + tr('f_top') + '</button>';
  html += '<button class="filter' + (state.sortMode==='bottom'?' active':'') + '" data-sort="bottom">' + tr('f_bottom') + '</button>';
  html += '<button class="filter' + (state.sortMode==='old'?' active':'') + '" data-sort="old">' + tr('f_old') + '</button>';
  html += '</div><div class="filter-sep"></div><div class="filter-group">';
  html += '<button class="filter' + (state.commentFilter==='any'?' active':'') + '" data-comments="any">' + tr('f_all') + '</button>';
  html += '<button class="filter' + (state.commentFilter==='many'?' active':'') + '" data-comments="many">' + tr('f_many') + '</button>';
  html += '<button class="filter' + (state.commentFilter==='some'?' active':'') + '" data-comments="some">' + tr('f_some') + '</button>';
  html += '<button class="filter' + (state.commentFilter==='none'?' active':'') + '" data-comments="none">' + tr('f_none') + '</button>';
  html += '</div></div>';
  html += '<div class="main-body"><div id="feed">' + spinner() + '</div></div>';

  if (state.user) {
    html += '<div class="composer">';
    html += '<textarea id="newPost" maxlength="' + MAX_POST_LEN + '" placeholder="' + escapeHtml(tr('post_ph')) + '"></textarea>';
    html += '<div class="composer-row">';
    html += '<div class="composer-left"><div id="counter" class="counter">0 / ' + MAX_POST_LEN + '</div></div>';
    html += '<button id="send" class="send" disabled>' + tr('publish') + '</button>';
    html += '</div></div>';
  } else {
    html += '<div class="composer"><div class="login-prompt">'
      + escapeHtml(tr('login_to_post')) + ' · <a href="/login" data-link>' + tr('go_login') + '</a> · <a href="/register" data-link>' + tr('go_register') + '</a>'
      + '</div></div>';
  }
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);

  var filtersEl = document.getElementById('filters');
  filtersEl.addEventListener('click', function(e){
    var b = e.target.closest('.filter');
    if (!b) return;
    if (b.dataset.sort) {
      state.sortMode = b.dataset.sort;
      filtersEl.querySelectorAll('[data-sort]').forEach(function(x){ x.classList.toggle('active', x === b); });
    } else if (b.dataset.comments) {
      state.commentFilter = b.dataset.comments;
      filtersEl.querySelectorAll('[data-comments]').forEach(function(x){ x.classList.toggle('active', x === b); });
    }
    loadFeed();
  });

  var searchEl = document.getElementById('search');
  var searchBtn = document.getElementById('searchBtn');
  var tId;
  searchEl.addEventListener('input', function(){
    state.searchQuery = searchEl.value;
    clearTimeout(tId); tId = setTimeout(loadFeed, 250);
  });
  searchEl.addEventListener('keydown', function(e){
    if (e.key === 'Enter') { e.preventDefault(); loadFeed(); }
  });
  searchBtn.addEventListener('click', function(e){ e.preventDefault(); loadFeed(); });

  if (state.user) {
    var inputEl = document.getElementById('newPost');
    var sendBtn = document.getElementById('send');
    var counter = document.getElementById('counter');
    inputEl.value = state.composerDraft || '';
    function updateCounter(){
      var len = inputEl.value.length;
      counter.textContent = len + ' / ' + MAX_POST_LEN;
      counter.classList.toggle('warn', len >= MAX_POST_LEN);
      sendBtn.disabled = len === 0 || len > MAX_POST_LEN;
    }
    inputEl.addEventListener('input', function(){ state.composerDraft = inputEl.value; updateCounter(); });
    inputEl.addEventListener('keydown', function(e){
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendPost(); }
    });
    sendBtn.addEventListener('click', sendPost);
    updateCounter();
    async function sendPost(){
      var text = inputEl.value.trim();
      if (!text) return;
      sendBtn.disabled = true;
      try {
        await api('/api/posts', { method: 'POST', body: { text: text } });
        inputEl.value = ''; state.composerDraft = '';
        updateCounter();
        state.searchQuery = '';
        await loadFeed();
      } catch(e) { alert(tr(e.message) || e.message); }
      finally { updateCounter(); }
    }
  }
  loadFeed();
}

async function loadFeed() {
  var feedEl = document.getElementById('feed');
  if (!feedEl) return;
  try {
    var q = state.searchQuery.trim();
    var data = await api('/api/posts?q=' + encodeURIComponent(q));
    var posts = applyFilters(data.posts || []);
    if (!posts.length) { feedEl.innerHTML = '<div class="empty">' + escapeHtml(tr('no_posts')) + '</div>'; return; }
    feedEl.innerHTML = posts.map(function(p){ return renderPostHtml(p, false); }).join('');
    bindPostActions(feedEl);
    bindLinks(feedEl);
  } catch(e) { feedEl.innerHTML = '<div class="empty">—</div>'; }
}
function applyFilters(posts) {
  if (state.commentFilter === 'some') posts = posts.filter(function(p){ return p.comments.length > 0; });
  else if (state.commentFilter === 'many') posts = posts.filter(function(p){ return p.comments.length >= 3; });
  else if (state.commentFilter === 'none') posts = posts.filter(function(p){ return p.comments.length === 0; });
  posts.sort(function(a, b){
    var sa = a.upvotes - a.downvotes, sb = b.upvotes - b.downvotes;
    if (state.sortMode === 'new') return b.created_at - a.created_at;
    if (state.sortMode === 'old') return a.created_at - b.created_at;
    if (state.sortMode === 'top') return (sb - sa) || (b.created_at - a.created_at);
    if (state.sortMode === 'bottom') return (sa - sb) || (b.created_at - a.created_at);
    return 0;
  });
  return posts;
}

function renderPostView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title"></div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="feed">' + spinner() + '</div></div>';
  if (state.user) {
    html += '<div class="composer composer-comment" id="composer">';
    html += '<div id="replyBanner"></div>';
    html += '<textarea id="newComment" maxlength="' + MAX_COMMENT_LEN + '" placeholder="' + escapeHtml(tr('comment_ph')) + '"></textarea>';
    html += '<div class="composer-row">';
    html += '<div class="composer-left"><div id="counter" class="counter">0 / ' + MAX_COMMENT_LEN + '</div></div>';
    html += '<button id="send" class="send" disabled>' + tr('send_comment') + '</button>';
    html += '</div></div>';
  } else {
    html += '<div class="composer"><div class="login-prompt">'
      + escapeHtml(tr('login_to_comment')) + ' · <a href="/login" data-link>' + tr('go_login') + '</a>'
      + '</div></div>';
  }
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);

  if (state.user) {
    var inputEl = document.getElementById('newComment');
    var sendBtn = document.getElementById('send');
    var counter = document.getElementById('counter');
    var replyBanner = document.getElementById('replyBanner');
    function renderReplyBanner() {
      if (!state.replyTo) { replyBanner.innerHTML = ''; inputEl.placeholder = tr('comment_ph'); return; }
      replyBanner.innerHTML = '<div class="reply-banner">' + tr('reply') + ': <b>@' + escapeHtml(state.replyTo.author) + '</b> <button id="cancelReplyBtn">' + tr('cancel_reply') + '</button></div>';
      inputEl.placeholder = tr('reply_ph');
      document.getElementById('cancelReplyBtn').addEventListener('click', function(){
        state.replyTo = null; renderReplyBanner();
      });
    }
    renderReplyBanner();
    state._renderReplyBanner = renderReplyBanner;

    function updateCounter(){
      var len = inputEl.value.length;
      counter.textContent = len + ' / ' + MAX_COMMENT_LEN;
      counter.classList.toggle('warn', len >= MAX_COMMENT_LEN);
      sendBtn.disabled = len === 0 || len > MAX_COMMENT_LEN;
    }
    inputEl.addEventListener('input', updateCounter);
    inputEl.addEventListener('keydown', function(e){
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendComment(); }
    });
    sendBtn.addEventListener('click', sendComment);
    updateCounter();

    async function sendComment(){
      var text = inputEl.value.trim();
      if (!text) return;
      sendBtn.disabled = true;
      try {
        var body = { text: text };
        if (state.replyTo) body.parent_id = state.replyTo.id;
        await api('/api/posts/' + state.viewData.post_id + '/comments', { method: 'POST', body: body });
        inputEl.value = '';
        state.replyTo = null; renderReplyBanner();
        updateCounter();
        await loadPostView();
      } catch(e) { alert(tr(e.message) || e.message); }
      finally { updateCounter(); }
    }
  }
  loadPostView();
}

async function loadPostView() {
  var feedEl = document.getElementById('feed');
  if (!feedEl) return;
  try {
    var p = await api('/api/posts/' + state.viewData.post_id);
    feedEl.innerHTML = renderPostHtml(p, true);
    bindPostActions(feedEl);
    bindLinks(feedEl);
    if (state.highlightComment) {
      var node = feedEl.querySelector('[data-comment-id="' + state.highlightComment + '"]');
      if (node) {
        node.classList.add('highlight');
        setTimeout(function(){ node.scrollIntoView({behavior:'smooth', block:'center'}); }, 60);
        state.highlightComment = null;
      }
    }
  } catch(e) { feedEl.innerHTML = '<div class="empty">' + escapeHtml(tr('not_found')) + '</div>'; }
}

function renderProfileView(el) {
  var nick = state.viewData.nick || '';
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">@' + escapeHtml(nick) + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="profileHeader">' + spinner() + '</div><div id="feed"></div></div>';
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);
  loadProfile(nick);
}

async function loadProfile(nick) {
  var headerEl = document.getElementById('profileHeader');
  var feedEl = document.getElementById('feed');
  try {
    var u = await api('/api/users/' + encodeURIComponent(nick));
    var isMe = state.user && state.user.nick === u.nick;

    var h = '<div class="profile-header">';
    h += '<div class="profile-name-big">' + escapeHtml(u.name) + '</div>';
    h += '<div class="profile-nick-small">@' + escapeHtml(u.nick) + '</div>';

    if (isMe) {
      h += '<div class="bio-editor">';
      h += '<textarea id="bioInput" maxlength="' + MAX_BIO_LEN + '" placeholder="' + escapeHtml(tr('bio_ph')) + '">' + escapeHtml(u.bio || '') + '</textarea>';
      h += '<div class="bio-editor-row"><button class="btn-secondary" id="bioSaveBtn">' + tr('bio_save') + '</button><span class="bio-saved-msg" id="bioSaved"></span></div>';
      h += '</div>';
    } else {
      if (u.bio) h += '<div class="profile-bio">' + escapeHtml(u.bio) + '</div>';
      else h += '<div class="profile-bio profile-bio-empty">' + escapeHtml(tr('bio_empty')) + '</div>';
    }

    h += '<div class="profile-stats">';
    h += '<span><b id="followersLink">' + u.followers + '</b> ' + tr('profile_followers') + '</span>';
    h += '<span><b id="followingLink">' + u.following + '</b> ' + tr('profile_following') + '</span>';
    h += '</div>';
    h += '<div class="profile-actions">';
    if (isMe) {
      h += '<span class="own-note">' + tr('own_profile') + '</span>';
    } else if (state.user) {
      h += '<button class="follow-btn' + (u.is_following ? ' following' : '') + '" id="followBtn">'
        + (u.is_following ? tr('unfollow') : tr('follow')) + '</button>';
      h += '<button class="follow-btn secondary" id="dmBtn">' + ICONS.mail + ' ' + tr('message') + '</button>';
    } else {
      h += '<a class="follow-btn" href="/login" data-link style="text-decoration:none">' + tr('go_login') + '</a>';
    }
    h += '</div></div>';
    headerEl.innerHTML = h;
    bindLinks(headerEl);

    document.getElementById('followersLink').addEventListener('click', function(){
      navigate('/u/' + encodeURIComponent(u.nick) + '/followers');
    });
    document.getElementById('followingLink').addEventListener('click', function(){
      navigate('/u/' + encodeURIComponent(u.nick) + '/following');
    });

    if (isMe) {
      document.getElementById('bioSaveBtn').addEventListener('click', async function(){
        var bio = document.getElementById('bioInput').value.trim();
        try {
          await api('/api/users/me/bio', { method: 'POST', body: { bio: bio } });
          var msg = document.getElementById('bioSaved');
          msg.textContent = tr('bio_saved');
          setTimeout(function(){ msg.textContent = ''; }, 1500);
        } catch(e) { alert(tr(e.message) || e.message); }
      });
    }

    var btn = document.getElementById('followBtn');
    if (btn) {
      btn.addEventListener('click', async function(){
        try {
          if (btn.classList.contains('following')) {
            await api('/api/users/' + encodeURIComponent(u.nick) + '/unfollow', { method: 'POST' });
          } else {
            await api('/api/users/' + encodeURIComponent(u.nick) + '/follow', { method: 'POST' });
          }
          loadProfile(nick);
        } catch(e) { alert(tr(e.message) || e.message); }
      });
    }
    var dmBtn = document.getElementById('dmBtn');
    if (dmBtn) dmBtn.addEventListener('click', function(){
      navigate('/messages/' + encodeURIComponent(u.nick));
    });

    var data = await api('/api/posts?author=' + encodeURIComponent(u.nick));
    var posts = (data.posts || []).slice();
    posts.sort(function(a, b){ return b.created_at - a.created_at; });
    if (!posts.length) feedEl.innerHTML = '<div class="empty">' + escapeHtml(tr('no_user_posts')) + '</div>';
    else {
      feedEl.innerHTML = posts.map(function(p){ return renderPostHtml(p, false); }).join('');
      bindPostActions(feedEl);
      bindLinks(feedEl);
    }
  } catch(e) {
    headerEl.innerHTML = '<div class="empty">' + escapeHtml(tr('not_found')) + '</div>';
    feedEl.innerHTML = '';
  }
}

function renderFollowListView(el) {
  var nick = state.viewData.nick;
  var isFollowers = state.view === 'followers';
  var title = isFollowers ? tr('followers_title') : tr('following_title');
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/u/' + encodeURIComponent(nick) + '" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">@' + escapeHtml(nick) + ' · ' + escapeHtml(title) + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="list">' + spinner() + '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);
  loadFollowList(nick, isFollowers);
}

async function loadFollowList(nick, isFollowers) {
  var wrap = document.getElementById('list');
  try {
    var path = isFollowers
      ? ('/api/users/' + encodeURIComponent(nick) + '/followers')
      : ('/api/users/' + encodeURIComponent(nick) + '/following');
    var data = await api(path);
    var users = data.users || [];
    if (!users.length) {
      wrap.innerHTML = '<div class="empty">' + escapeHtml(isFollowers ? tr('no_followers') : tr('no_following')) + '</div>';
      return;
    }
    wrap.innerHTML = '<div class="user-list">' + users.map(function(u){
      return '<div class="user-item">'
        + '<div class="user-info">'
        + '<a class="user-nick" href="/u/' + encodeURIComponent(u.nick) + '" data-link>@' + escapeHtml(u.nick) + '</a>'
        + '<div class="user-name">' + escapeHtml(u.name) + '</div>'
        + '</div></div>';
    }).join('') + '</div>';
    bindLinks(wrap);
  } catch(e) {
    wrap.innerHTML = '<div class="empty">' + escapeHtml(tr(e.message) || '—') + '</div>';
  }
}

function renderUsersView(el) {
  var html = '';
  html += '<header class="search-header">';
  html += '<input id="usersSearch" type="text" placeholder="' + escapeHtml(tr('users_search_ph')) + '" autocomplete="off" spellcheck="false" value="' + escapeHtml(state.usersQuery) + '" />';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="list">' + spinner() + '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);
  var searchEl = document.getElementById('usersSearch');
  var tId;
  searchEl.addEventListener('input', function(){
    state.usersQuery = searchEl.value;
    clearTimeout(tId); tId = setTimeout(loadUsers, 250);
  });
  loadUsers();
}

async function loadUsers() {
  var wrap = document.getElementById('list');
  if (!wrap) return;
  try {
    var q = (state.usersQuery || '').trim();
    var data = await api('/api/users?q=' + encodeURIComponent(q));
    var users = data.users || [];
    if (!users.length) {
      wrap.innerHTML = '<div class="empty">' + escapeHtml(tr('no_users')) + '</div>';
      return;
    }
    wrap.innerHTML = '<div class="user-list">' + users.map(function(u){
      return '<div class="user-item">'
        + '<div class="user-info">'
        + '<a class="user-nick" href="/u/' + encodeURIComponent(u.nick) + '" data-link>@' + escapeHtml(u.nick) + '</a>'
        + '<div class="user-name">' + escapeHtml(u.name) + '</div>'
        + '</div></div>';
    }).join('') + '</div>';
    bindLinks(wrap);
  } catch(e) { wrap.innerHTML = '<div class="empty">—</div>'; }
}

function renderMessagesView(el) {
  var html = '';
  html += '<header class="search-header">';
  html += '<input id="dmSearch" type="text" placeholder="' + escapeHtml(tr('dm_search_ph')) + '" autocomplete="off" spellcheck="false" value="' + escapeHtml(state.dmQuery) + '" />';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="list">' + spinner() + '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);
  var searchEl = document.getElementById('dmSearch');
  var tId;
  searchEl.addEventListener('input', function(){
    state.dmQuery = searchEl.value;
    clearTimeout(tId); tId = setTimeout(loadDMs, 200);
  });
  loadDMs();
}

async function loadDMs() {
  var wrap = document.getElementById('list');
  if (!wrap) return;
  try {
    if (!state.user) {
      wrap.innerHTML = '<div class="login-prompt">' + escapeHtml(tr('login_to_dm')) + ' · <a href="/login" data-link>' + tr('go_login') + '</a></div>';
      bindLinks(wrap);
      return;
    }
    var data = await api('/api/dm/threads');
    var threads = data.threads || [];
    state.unreadDM = data.unread || 0;
    renderSidebar();
    var q = (state.dmQuery || '').trim().toLowerCase();
    if (q) threads = threads.filter(function(t){
      return t.other_nick.toLowerCase().includes(q) || (t.other_name || '').toLowerCase().includes(q);
    });
    if (!threads.length) {
      wrap.innerHTML = '<div class="empty">' + escapeHtml(tr('dm_empty')) + '</div>';
      return;
    }
    wrap.innerHTML = '<div class="dm-list">' + threads.map(function(t){
      var badge = t.unread > 0 ? '<span class="unread-badge">' + t.unread + '</span>' : '';
      var mineLabel = t.last_from_me ? '<span class="mine-label">вы: </span>' : '';
      return '<a class="dm-thread" href="/messages/' + encodeURIComponent(t.other_nick) + '" data-link>'
        + '<div class="info">'
        + '<div class="who">@' + escapeHtml(t.other_nick) + '</div>'
        + '<div class="preview">' + mineLabel + escapeHtml(t.last_message || '') + '</div>'
        + '</div>'
        + '<div class="meta">'
        + '<span class="time">' + timeAgo(t.last_message_at) + '</span>'
        + badge
        + '</div></a>';
    }).join('') + '</div>';
    bindLinks(wrap);
  } catch(e) { wrap.innerHTML = '<div class="empty">—</div>'; }
}

function renderChatView(el) {
  var nick = state.viewData.nick || '';
  if (!state.user) {
    el.innerHTML = '<header class="main-header"><a href="/messages" class="icon-btn" data-link>' + ICONS.back + '</a><div class="title">@' + escapeHtml(nick) + '</div></header>'
      + '<div class="main-body"><div class="login-prompt">' + escapeHtml(tr('login_to_dm')) + ' · <a href="/login" data-link>' + tr('go_login') + '</a></div></div>';
    attachThemeBtn(); bindLinks(el);
    return;
  }
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/messages" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">@' + escapeHtml(nick) + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div class="chat">';
  html += '<div class="chat-body" id="chatBody">' + spinner() + '</div>';
  html += '<div class="chat-composer">';
  html += '<textarea id="dmInput" maxlength="' + MAX_DM_LEN + '" placeholder="' + escapeHtml(tr('dm_send_ph')) + '" rows="1"></textarea>';
  html += '<button id="dmSend" disabled>' + tr('dm_send') + '</button>';
  html += '</div>';
  html += '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);

  var inputEl = document.getElementById('dmInput');
  var sendBtn = document.getElementById('dmSend');

  function updateCounter() {
    var v = inputEl.value;
    sendBtn.disabled = v.trim().length === 0;
  }
  inputEl.addEventListener('input', updateCounter);
  inputEl.addEventListener('keydown', function(e){
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendDM();
    }
  });
  sendBtn.addEventListener('click', sendDM);
  updateCounter();

  async function sendDM() {
    var text = inputEl.value.trim();
    if (!text) return;
    sendBtn.disabled = true;
    try {
      await api('/api/dm/send', { method: 'POST', body: { to: nick, text: text } });
      inputEl.value = '';
      updateCounter();
      await loadChat();
    } catch(e) {
      alert(tr(e.message) || e.message);
      updateCounter();
    }
  }

  loadChat();
}

async function loadChat() {
  var body = document.getElementById('chatBody');
  if (!body) return;
  var nick = state.viewData.nick;
  try {
    var data = await api('/api/dm/with/' + encodeURIComponent(nick));
    if (!data.messages || !data.messages.length) {
      body.innerHTML = '<div class="chat-intro">' + escapeHtml(tr('dm_no_messages')) + '</div>';
      return;
    }
    body.innerHTML = data.messages.map(function(m){
      return '<div class="chat-msg ' + (m.mine ? 'mine' : 'theirs') + '">'
        + escapeHtml(m.text)
        + '<div class="chat-time">' + timeAgo(m.created_at) + '</div>'
        + '</div>';
    }).join('');
    body.scrollTop = body.scrollHeight;
  } catch(e) {
    body.innerHTML = '<div class="empty">' + escapeHtml(tr('not_found')) + '</div>';
  }
}

function renderNotificationsView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('notif_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="notifList">' + spinner() + '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);
  loadNotifications();
}

async function loadNotifications() {
  var wrap = document.getElementById('notifList');
  if (!wrap) return;
  try {
    var data = await api('/api/notifications');
    var items = data.items || [];
    state.unreadNotif = data.unread || 0;
    if (data.unread > 0) {
      api('/api/notifications/read', { method: 'POST' }).catch(function(){});
      state.unreadNotif = 0;
      renderSidebar();
    }
    if (!items.length) {
      wrap.innerHTML = '<div class="empty">' + escapeHtml(tr('notif_empty')) + '</div>';
      return;
    }
    var html = '<div class="notif-actions"><button class="btn-danger" id="clearNotifsBtn">' + ICONS.trash + ' ' + tr('notif_clear') + '</button></div>';
    html += '<div class="notif-list">' + items.map(renderNotifHtml).join('') + '</div>';
    wrap.innerHTML = html;
    bindLinks(wrap);
    document.getElementById('clearNotifsBtn').addEventListener('click', async function(){
      if (!confirm(tr('notif_clear') + '?')) return;
      try {
        await api('/api/notifications/clear', { method: 'POST' });
        loadNotifications();
      } catch(e) { alert(tr(e.message) || e.message); }
    });
  } catch(e) { wrap.innerHTML = '<div class="empty">—</div>'; }
}

function renderNotifHtml(n) {
  var cls = 'notif' + (n.read ? '' : ' unread');
  var author = '<span class="notif-author">@' + escapeHtml(n.from_nick || '?') + '</span>';
  var text = '';
  var link = null;
  if (n.type === 'follow') {
    text = '<div class="notif-head">' + author + '<span class="notif-text">' + tr('notif_follow') + '</span></div>';
    link = '/u/' + encodeURIComponent(n.from_nick);
  } else if (n.type === 'comment') {
    text = '<div class="notif-head">' + author + '<span class="notif-text">' + tr('notif_comment') + '</span></div>';
    if (n.text) text += '<div class="notif-snippet">' + escapeHtml(n.text) + '</div>';
    link = n.post_id ? ('/p/' + n.post_id + (n.comment_id ? ('#c-' + n.comment_id) : '')) : null;
  } else if (n.type === 'reply') {
    text = '<div class="notif-head">' + author + '<span class="notif-text">' + tr('notif_reply') + '</span></div>';
    if (n.text) text += '<div class="notif-snippet">' + escapeHtml(n.text) + '</div>';
    link = n.post_id ? ('/p/' + n.post_id + (n.comment_id ? ('#c-' + n.comment_id) : '')) : null;
  } else if (n.type === 'mention') {
    text = '<div class="notif-head">' + author + '<span class="notif-text">' + tr('notif_mention') + '</span></div>';
    if (n.text) text += '<div class="notif-snippet">' + escapeHtml(n.text) + '</div>';
    link = n.post_id ? ('/p/' + n.post_id + (n.comment_id ? ('#c-' + n.comment_id) : '')) : null;
  } else {
    text = '<div class="notif-text">' + author + '</div>';
  }
  var inner = text + '<div class="notif-time">' + timeAgo(n.created_at) + '</div>';
  if (link) return '<a class="' + cls + '" href="' + link + '" data-link>' + inner + '</a>';
  return '<div class="' + cls + '">' + inner + '</div>';
}

function renderSettingsView(el) {
  var theme = document.documentElement.getAttribute('data-theme') || 'light';
  var lang = LANG;
  var me = state.user || {};
  var allowF = me.allow_followers_view !== false;
  var allowG = me.allow_following_view !== false;

  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('settings_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div class="settings">';

  html += '<div class="settings-section"><h2>' + tr('settings_appearance') + '</h2>';
  html += '<div style="margin-bottom:14px"><div style="font-size:12px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">' + tr('settings_theme') + '</div><div class="opt-row">';
  html += '<button class="opt' + (theme==='light'?' active':'') + '" data-set-theme="light">' + tr('theme_light') + '</button>';
  html += '<button class="opt' + (theme==='dark'?' active':'') + '" data-set-theme="dark">' + tr('theme_dark') + '</button>';
  html += '</div></div>';
  html += '<div><div style="font-size:12px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">' + tr('settings_lang') + '</div><div class="opt-row">';
  html += '<button class="opt' + (lang==='ru'?' active':'') + '" data-set-lang="ru">Русский</button>';
  html += '<button class="opt' + (lang==='en'?' active':'') + '" data-set-lang="en">English</button>';
  html += '</div></div>';
  html += '</div>';

  if (state.user) {
    html += '<div class="settings-section"><h2>' + tr('settings_privacy') + '</h2>';
    html += '<div class="toggle-row"><span>' + tr('settings_allow_followers') + '</span>'
      + '<div class="toggle' + (allowF ? ' on' : '') + '" data-toggle="allow_followers_view"></div></div>';
    html += '<div class="toggle-row"><span>' + tr('settings_allow_following') + '</span>'
      + '<div class="toggle' + (allowG ? ' on' : '') + '" data-toggle="allow_following_view"></div></div>';
    html += '</div>';
  }

  html += '<div class="settings-section"><h2>' + tr('settings_info') + '</h2>';
  html += '<p class="settings-desc">' + tr('settings_desc') + '</p>';
  html += '<p style="margin:0 0 14px"><a class="settings-link" href="/policy" data-link>' + tr('settings_policy') + '</a></p>';
  html += '<div style="font-size:12px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">' + tr('settings_authors') + '</div>';
  html += '<div class="settings-authors">SldShr, DeepSeek</div>';
  html += '</div>';

  html += '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);

  el.querySelectorAll('[data-set-theme]').forEach(function(b){
    b.addEventListener('click', function(){ applyTheme(b.dataset.setTheme); renderSettingsView(el); });
  });
  el.querySelectorAll('[data-set-lang]').forEach(function(b){
    b.addEventListener('click', function(){
      document.cookie = 'sldchat_lang=' + b.dataset.setLang + '; path=/; max-age=' + (60*60*24*365);
      location.reload();
    });
  });
  el.querySelectorAll('[data-toggle]').forEach(function(t){
    t.addEventListener('click', async function(){
      var key = t.dataset.toggle;
      var newVal = !t.classList.contains('on');
      t.classList.toggle('on', newVal);
      var patch = {};
      patch[key] = newVal;
      var meNow = state.user || {};
      meNow[key] = newVal;
      setUser(meNow);
      try {
        await api('/api/users/me/settings', { method: 'POST', body: patch });
      } catch(e) {
        t.classList.toggle('on', !newVal);
        meNow[key] = !newVal;
        setUser(meNow);
        alert(tr(e.message) || e.message);
      }
    });
  });
}

function renderPolicyView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/settings" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('policy_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div class="policy">';
  html += '<h1>' + tr('policy_title') + '</h1>';
  html += '<p>' + escapeHtml(tr('policy_content')) + '</p>';
  html += '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);
}

function renderRegisterView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('reg_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div class="auth-wrap">';
  html += '<h1 class="auth-title">' + tr('reg_title') + '</h1>';
  html += '<form class="auth-form" id="regForm">';
  html += '<input type="text" name="name" placeholder="' + escapeHtml(tr('name_ph')) + '" maxlength="50" required />';
  html += '<input type="text" name="nick" placeholder="' + escapeHtml(tr('nick_ph')) + '" maxlength="20" required />';
  html += '<input type="password" name="password" placeholder="' + escapeHtml(tr('pass_ph')) + '" required />';
  html += '<input type="password" name="password_confirm" placeholder="' + escapeHtml(tr('pass2_ph')) + '" required />';
  html += '<div class="auth-error" id="regError"></div>';
  html += '<button type="submit">' + tr('reg_btn') + '</button>';
  html += '</form>';
  html += '<div class="auth-switch"><a id="toLogin">' + tr('to_login') + '</a></div>';
  html += '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);
  document.getElementById('toLogin').addEventListener('click', function(){ navigate('/login'); });
  var form = document.getElementById('regForm');
  var errEl = document.getElementById('regError');
  form.addEventListener('submit', async function(e){
    e.preventDefault();
    errEl.textContent = '';
    var fd = new FormData(form);
    try {
      await doRegister({
        name: fd.get('name'), nick: fd.get('nick'),
        password: fd.get('password'), password_confirm: fd.get('password_confirm')
      });
    } catch(err) { errEl.textContent = tr(err.message) || err.message; }
  });
}

function renderLoginView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('log_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div class="auth-wrap">';
  html += '<h1 class="auth-title">' + tr('log_title') + '</h1>';
  html += '<form class="auth-form" id="logForm">';
  html += '<input type="text" name="nick" placeholder="' + escapeHtml(tr('nick_ph')) + '" required />';
  html += '<input type="password" name="password" placeholder="' + escapeHtml(tr('pass_ph')) + '" required />';
  html += '<div class="auth-error" id="logError"></div>';
  html += '<button type="submit">' + tr('log_btn') + '</button>';
  html += '</form>';
  html += '<div class="auth-switch"><a id="toReg">' + tr('to_reg') + '</a></div>';
  html += '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();
  bindLinks(el);
  document.getElementById('toReg').addEventListener('click', function(){ navigate('/register'); });
  var form = document.getElementById('logForm');
  var errEl = document.getElementById('logError');
  form.addEventListener('submit', async function(e){
    e.preventDefault();
    errEl.textContent = '';
    var fd = new FormData(form);
    try { await doLogin({ nick: fd.get('nick'), password: fd.get('password') }); }
    catch(err) { errEl.textContent = tr(err.message) || err.message; }
  });
}

function renderPostHtml(p, showComments) {
  var score = p.upvotes - p.downvotes;
  var upCls = p.user_vote === 1 ? 'active' : '';
  var downCls = p.user_vote === -1 ? 'active' : '';
  var scCls = scoreClass(p.upvotes, p.downvotes);

  var displayText = p.text;
  var truncated = false;
  if (!showComments) {
    var res = truncateText(p.text);
    displayText = res.text;
    truncated = res.truncated;
  }
  var bodyHtml = linkifyMentions(escapeHtml(displayText));
  var readMore = truncated
    ? '<a class="read-more" href="/p/' + p.id + '" data-link>… ' + escapeHtml(tr('read_more')) + '</a>'
    : '';

  var authorLink = p.author
    ? '<a class="post-author" href="/u/' + encodeURIComponent(p.author) + '" data-link>@' + escapeHtml(p.author) + '</a>'
    : '';

  var isMine = state.user && p.author === state.user.nick;

  var commentBtn = '<button class="action-btn" data-action="comment" data-post-id="' + p.id + '">'
    + ICONS.comment + '<span>' + (p.comments ? p.comments.length : 0) + '</span></button>';
  var copyBtn = '<button class="action-btn" data-action="copy" data-post-id="' + p.id + '" title="' + escapeHtml(tr('copy')) + '">'
    + ICONS.copy + '</button>';
  var editBtn = isMine
    ? '<button class="action-btn" data-action="edit-post" data-post-id="' + p.id + '" title="' + escapeHtml(tr('edit')) + '">' + ICONS.edit + '</button>'
    : '';
  var delBtn = isMine
    ? '<button class="action-btn danger" data-action="delete-post" data-post-id="' + p.id + '" title="' + escapeHtml(tr('delete')) + '">' + ICONS.trash + '</button>'
    : '';

  var commentsHtml = '';
  if (showComments && p.comments && p.comments.length) {
    commentsHtml = '<div class="comments">' + renderCommentsTree(p.comments, p.author, p.id) + '</div>';
  }

  return ''
    + '<div class="post" data-post-id="' + p.id + '">'
    +   '<div class="post-meta">' + authorLink + '<span class="post-time">' + timeAgo(p.created_at) + '</span></div>'
    +   '<div class="post-text" data-raw="' + escapeHtml(p.text) + '">' + bodyHtml + '</div>'
    +   readMore
    +   '<div class="post-actions">'
    +     '<button class="vote-btn up ' + upCls + '" data-action="vote" data-post-id="' + p.id + '" data-dir="1">' + ICONS.up + '</button>'
    +     '<span class="score ' + scCls + '">' + score + '</span>'
    +     '<button class="vote-btn down ' + downCls + '" data-action="vote" data-post-id="' + p.id + '" data-dir="-1">' + ICONS.down + '</button>'
    +     commentBtn + copyBtn + editBtn + delBtn
    +   '</div>'
    +   commentsHtml
    + '</div>';
}

function renderCommentsTree(comments, postAuthor, postId) {
  var tops = comments.filter(function(c){ return !c.parent_id; }).sort(function(a,b){ return a.created_at - b.created_at; });
  var repliesBy = {};
  comments.forEach(function(c){
    if (c.parent_id) (repliesBy[c.parent_id] = repliesBy[c.parent_id] || []).push(c);
  });
  var html = '';
  tops.forEach(function(c){
    html += renderCommentHtml(c, postAuthor, postId, false);
    var reps = repliesBy[c.id] || [];
    reps.sort(function(a,b){ return a.created_at - b.created_at; });
    reps.forEach(function(r){ html += renderCommentHtml(r, postAuthor, postId, true); });
  });
  return html;
}

function renderCommentHtml(c, postAuthor, postId, isReply) {
  var score = c.upvotes - c.downvotes;
  var upCls = c.user_vote === 1 ? 'active' : '';
  var downCls = c.user_vote === -1 ? 'active' : '';
  var scCls = scoreClass(c.upvotes, c.downvotes);
  var isAuthor = postAuthor && c.author === postAuthor;
  var isMine = state.user && c.author === state.user.nick;
  var cls = 'comment' + (isReply ? ' reply' : '') + (isAuthor ? ' is-author' : '');
  var authorHtml = c.author
    ? '<a class="comment-author" href="/u/' + encodeURIComponent(c.author) + '" data-link>@' + escapeHtml(c.author) + '</a>'
    : '';
  var badge = isAuthor ? '<span class="comment-author-badge">' + escapeHtml(tr('author_badge')) + '</span>' : '';

  var replyBtn = '';
  if (!isReply && state.user) {
    replyBtn = '<button class="comment-reply-btn" data-action="reply" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-author="' + escapeHtml(c.author || '') + '">' + tr('reply') + '</button>';
  }
  var editBtn = isMine
    ? '<button class="comment-edit-btn" data-action="edit-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '">' + ICONS.edit + '</button>'
    : '';
  var delBtn = isMine
    ? '<button class="comment-edit-btn danger" data-action="delete-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '">' + ICONS.trash + '</button>'
    : '';

  var bodyHtml = linkifyMentions(escapeHtml(c.text));

  return ''
    + '<div class="' + cls + '" data-comment-id="' + c.id + '">'
    +   '<div class="comment-meta">' + authorHtml + badge + '<span class="comment-time">' + timeAgo(c.created_at) + '</span></div>'
    +   '<div class="comment-text" data-raw="' + escapeHtml(c.text) + '">' + bodyHtml + '</div>'
    +   '<div class="comment-actions">'
    +     '<button class="vote-btn up ' + upCls + '" data-action="vote-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-dir="1">' + ICONS.up + '</button>'
    +     '<span class="score ' + scCls + '">' + score + '</span>'
    +     '<button class="vote-btn down ' + downCls + '" data-action="vote-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-dir="-1">' + ICONS.down + '</button>'
    +     replyBtn + editBtn + delBtn
    +   '</div>'
    + '</div>';
}

function applyVoteUI(targetBtn, dir) {
  var row = targetBtn.closest('.post-actions, .comment-actions');
  if (!row) return null;
  var upBtn = row.querySelector('.vote-btn.up');
  var downBtn = row.querySelector('.vote-btn.down');
  var scoreEl = row.querySelector('.score');
  if (!upBtn || !downBtn || !scoreEl) return null;

  var oldScore = parseInt(scoreEl.textContent, 10) || 0;
  var wasUp = upBtn.classList.contains('active');
  var wasDown = downBtn.classList.contains('active');

  var newUp = wasUp, newDown = wasDown, newScore = oldScore;
  if (dir === 1) {
    if (wasUp) { newUp = false; newScore = oldScore - 1; }
    else if (wasDown) { newUp = true; newDown = false; newScore = oldScore + 2; }
    else { newUp = true; newScore = oldScore + 1; }
  } else {
    if (wasDown) { newDown = false; newScore = oldScore + 1; }
    else if (wasUp) { newDown = true; newUp = false; newScore = oldScore - 2; }
    else { newDown = true; newScore = oldScore - 1; }
  }
  scoreEl.textContent = newScore;
  scoreEl.classList.remove('up','down');
  if (newScore > 0) scoreEl.classList.add('up');
  else if (newScore < 0) scoreEl.classList.add('down');
  upBtn.classList.toggle('active', newUp);
  downBtn.classList.toggle('active', newDown);

  return { upBtn: upBtn, downBtn: downBtn, scoreEl: scoreEl, oldScore: oldScore, wasUp: wasUp, wasDown: wasDown };
}

function revertVote(snap) {
  if (!snap) return;
  snap.scoreEl.textContent = snap.oldScore;
  snap.scoreEl.classList.remove('up','down');
  if (snap.oldScore > 0) snap.scoreEl.classList.add('up');
  else if (snap.oldScore < 0) snap.scoreEl.classList.add('down');
  snap.upBtn.classList.toggle('active', snap.wasUp);
  snap.downBtn.classList.toggle('active', snap.wasDown);
}

function startInlineEdit(container, textEl, initialText, onSave) {
  var editor = document.createElement('div');
  editor.className = 'inline-editor';
  editor.innerHTML = '<textarea></textarea>'
    + '<div class="edit-actions">'
    + '<button class="edit-cancel">' + escapeHtml(tr('cancel')) + '</button>'
    + '<button class="edit-save">' + escapeHtml(tr('save')) + '</button>'
    + '</div>';
  textEl.style.display = 'none';
  container.insertBefore(editor, textEl);

  var ta = editor.querySelector('textarea');
  ta.value = initialText;
  ta.focus();
  try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch(e) {}

  var cancelBtn = editor.querySelector('.edit-cancel');
  var saveBtn = editor.querySelector('.edit-save');

  function close() {
    if (editor.parentNode) editor.parentNode.removeChild(editor);
    textEl.style.display = '';
  }
  cancelBtn.addEventListener('click', close);
  saveBtn.addEventListener('click', async function(){
    var v = ta.value.trim();
    if (!v) return;
    saveBtn.disabled = true;
    try {
      await onSave(v);
      close();
    } catch(e) {
      alert(tr(e.message) || e.message);
      saveBtn.disabled = false;
    }
  });
}

function bindPostActions(root) {
  root.querySelectorAll('[data-action]').forEach(function(btn){
    if (btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', async function(e){
      e.preventDefault();
      var action = btn.dataset.action;
      var postId = btn.dataset.postId;
      var commentId = btn.dataset.commentId;
      var dir = parseInt(btn.dataset.dir || '0', 10);

      if (action === 'vote') {
        if (!state.user) { navigate('/login'); return; }
        var snap = applyVoteUI(btn, dir);
        state.suppressRefresh = Date.now() + 3000;
        try {
          await api('/api/posts/' + postId + '/vote', { method: 'POST', body: { direction: dir } });
        } catch(err) {
          revertVote(snap);
          alert(tr(err.message) || err.message);
        }
        return;
      }
      if (action === 'vote-comment') {
        if (!state.user) { navigate('/login'); return; }
        var snap2 = applyVoteUI(btn, dir);
        state.suppressRefresh = Date.now() + 3000;
        try {
          await api('/api/posts/' + postId + '/comments/' + commentId + '/vote', { method: 'POST', body: { direction: dir } });
        } catch(err) {
          revertVote(snap2);
          alert(tr(err.message) || err.message);
        }
        return;
      }
      if (action === 'comment') { navigate('/p/' + postId); return; }
      if (action === 'copy') { await copyPost(postId, btn); return; }
      if (action === 'reply') {
        state.replyTo = { id: commentId, author: btn.dataset.author || '' };
        if (state.view !== 'post') { navigate('/p/' + postId); return; }
        if (state._renderReplyBanner) state._renderReplyBanner();
        var ta = document.getElementById('newComment');
        if (ta) ta.focus();
        return;
      }
      if (action === 'edit-post') {
        var postEl = btn.closest('.post');
        var textEl = postEl.querySelector('.post-text');
        var raw = textEl.getAttribute('data-raw') || '';
        startInlineEdit(postEl, textEl, raw, async function(newText){
          await api('/api/posts/' + postId, { method: 'PUT', body: { text: newText } });
          refreshCurrentView();
        });
        return;
      }
      if (action === 'delete-post') {
        if (!confirm(tr('confirm_delete'))) return;
        try {
          await api('/api/posts/' + postId, { method: 'DELETE' });
          if (state.view === 'post') navigate('/');
          else refreshCurrentView();
        } catch(err) { alert(tr(err.message) || err.message); }
        return;
      }
      if (action === 'edit-comment') {
        var cEl = btn.closest('.comment');
        var cTextEl = cEl.querySelector('.comment-text');
        var cRaw = cTextEl.getAttribute('data-raw') || '';
        startInlineEdit(cEl, cTextEl, cRaw, async function(newText){
          await api('/api/posts/' + postId + '/comments/' + commentId, { method: 'PUT', body: { text: newText } });
          refreshCurrentView();
        });
        return;
      }
      if (action === 'delete-comment') {
        if (!confirm(tr('confirm_delete'))) return;
        try {
          await api('/api/posts/' + postId + '/comments/' + commentId, { method: 'DELETE' });
          refreshCurrentView();
        } catch(err) { alert(tr(err.message) || err.message); }
        return;
      }
    });
  });
  root.querySelectorAll('[data-link]').forEach(function(a){
    if (a.dataset.linkBound) return;
    a.dataset.linkBound = '1';
    a.addEventListener('click', function(e){
      e.preventDefault();
      navigate(a.getAttribute('href'));
    });
  });
}

async function refreshCurrentView() {
  if (state.view === 'feed') await loadFeed();
  else if (state.view === 'post') await loadPostView();
  else if (state.view === 'profile') await loadProfile(state.viewData.nick);
}

async function copyPost(postId, btn) {
  try {
    var p = await api('/api/posts/' + postId);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(p.text);
    } else {
      var ta = document.createElement('textarea');
      ta.value = p.text; ta.style.position='fixed'; ta.style.opacity='0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); document.body.removeChild(ta);
    }
    btn.innerHTML = ICONS.check;
    btn.classList.add('copied');
    setTimeout(function(){ btn.innerHTML = ICONS.copy; btn.classList.remove('copied'); }, 1200);
  } catch(e) { alert('Copy failed'); }
}

async function pollCounters() {
  if (!state.user) return;
  try {
    var r = await Promise.all([
      api('/api/notifications').catch(function(){ return null; }),
      api('/api/dm/unread').catch(function(){ return null; }),
    ]);
    var changed = false;
    if (r[0]) {
      var nu = r[0].unread || 0;
      if (nu !== state.unreadNotif) { state.unreadNotif = nu; changed = true; }
    }
    if (r[1]) {
      var du = r[1].unread || 0;
      if (du !== state.unreadDM) { state.unreadDM = du; changed = true; }
    }
    if (changed) renderSidebar();
  } catch(e) {}
}

(function init() {
  if (state.user && (state.view === 'login' || state.view === 'register')) {
    history.replaceState({}, '', '/');
    state.view = 'feed'; state.viewData = {};
  }
  renderSidebar();
  renderMain();

  loadMe().then(function(){
    if (state.user && (state.view === 'login' || state.view === 'register')) {
      history.replaceState({}, '', '/');
      state.view = 'feed'; state.viewData = {};
    }
    renderSidebar();
    renderMain();
    if (state.user) {
      pollCounters();
      setInterval(pollCounters, 15000);
    }
  });

  setInterval(function(){
    if (document.hidden) return;
    if (state.suppressRefresh && Date.now() < state.suppressRefresh) return;
    if (state.view === 'feed') loadFeed();
    else if (state.view === 'post') loadPostView();
    else if (state.view === 'chat') loadChat();
  }, 5000);
})();
"""


def render_page(lang: str, view: str, view_data: Optional[dict] = None) -> str:
    t = TEXTS[lang]
    view_data = view_data or {}

    js = (JS
          .replace("__ICON_UP__", json.dumps(ICON_UP))
          .replace("__ICON_DOWN__", json.dumps(ICON_DOWN))
          .replace("__ICON_COMMENT__", json.dumps(ICON_COMMENT))
          .replace("__ICON_COPY__", json.dumps(ICON_COPY))
          .replace("__ICON_CHECK__", json.dumps(ICON_CHECK))
          .replace("__ICON_EDIT__", json.dumps(ICON_EDIT))
          .replace("__ICON_MOON__", json.dumps(ICON_MOON))
          .replace("__ICON_SUN__", json.dumps(ICON_SUN))
          .replace("__ICON_BACK__", json.dumps(ICON_BACK))
          .replace("__ICON_HOME__", json.dumps(ICON_HOME))
          .replace("__ICON_USERS__", json.dumps(ICON_USERS))
          .replace("__ICON_USER__", json.dumps(ICON_USER))
          .replace("__ICON_MAIL__", json.dumps(ICON_MAIL))
          .replace("__ICON_BELL__", json.dumps(ICON_BELL))
          .replace("__ICON_GEAR__", json.dumps(ICON_GEAR))
          .replace("__ICON_LOGOUT__", json.dumps(ICON_LOGOUT))
          .replace("__ICON_PLUS__", json.dumps(ICON_PLUS))
          .replace("__ICON_LOGIN__", json.dumps(ICON_LOGIN))
          .replace("__ICON_TRASH__", json.dumps(ICON_TRASH))
          .replace("__ICON_SEARCH__", json.dumps(ICON_SEARCH)))

    return (
        '<!DOCTYPE html>\n'
        f'<html lang="{lang}" data-theme="light">\n'
        '<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '<meta name="color-scheme" content="light dark" />\n'
        f'<link rel="icon" type="image/svg+xml" href="{FAVICON}" />\n'
        '<title>sldChat</title>\n'
        '<style>' + CSS + '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="layout">\n'
        '  <main class="main" id="main"></main>\n'
        '  <aside class="sidebar" id="sidebar"></aside>\n'
        '</div>\n'
        '<script>\n'
        f'const LANG = {json.dumps(lang)};\n'
        f'const VIEW = {json.dumps(view)};\n'
        f'const VIEW_DATA = {json.dumps(view_data)};\n'
        f'const MAX_POST_LEN = {MAX_POST_LEN};\n'
        f'const MAX_COMMENT_LEN = {MAX_COMMENT_LEN};\n'
        f'const MAX_BIO_LEN = {MAX_BIO_LEN};\n'
        f'const MAX_DM_LEN = {MAX_DM_LEN};\n'
        f'const TRUNCATE_LINES = {TRUNCATE_LINES};\n'
        f'const TRUNCATE_CHARS = {TRUNCATE_CHARS};\n'
        f'const T = {json.dumps(t, ensure_ascii=False)};\n'
        + js +
        '\n</script>\n'
        '</body>\n'
        '</html>'
    )


@app.get("/", response_class=HTMLResponse)
def page_index(request: Request):
    return render_page(get_lang(request), "feed")


@app.get("/p/{post_id}", response_class=HTMLResponse)
def page_post(post_id: str, request: Request):
    if not db_get_post(post_id):
        raise HTTPException(404, "not found")
    return render_page(get_lang(request), "post", {"post_id": post_id})


@app.get("/u/{nick}", response_class=HTMLResponse)
def page_user(nick: str, request: Request):
    return render_page(get_lang(request), "profile", {"nick": nick})


@app.get("/u/{nick}/followers", response_class=HTMLResponse)
def page_followers(nick: str, request: Request):
    return render_page(get_lang(request), "followers", {"nick": nick})


@app.get("/u/{nick}/following", response_class=HTMLResponse)
def page_following(nick: str, request: Request):
    return render_page(get_lang(request), "following", {"nick": nick})


@app.get("/users", response_class=HTMLResponse)
def page_users(request: Request):
    return render_page(get_lang(request), "users")


@app.get("/messages", response_class=HTMLResponse)
def page_messages(request: Request):
    return render_page(get_lang(request), "messages")


@app.get("/messages/{nick}", response_class=HTMLResponse)
def page_chat(nick: str, request: Request):
    return render_page(get_lang(request), "chat", {"nick": nick})


@app.get("/notifications", response_class=HTMLResponse)
def page_notifications(request: Request):
    return render_page(get_lang(request), "notifications")


@app.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request):
    return render_page(get_lang(request), "settings")


@app.get("/policy", response_class=HTMLResponse)
def page_policy(request: Request):
    return render_page(get_lang(request), "policy")


@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request):
    return render_page(get_lang(request), "register")


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return render_page(get_lang(request), "login")
