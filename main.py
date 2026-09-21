import os
import time
import uuid
import json
import hashlib
import re
import urllib.request
from typing import Dict, List, Optional, Set

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


USERS: Dict[str, dict] = {}          # nick -> user
SESSIONS: Dict[str, str] = {}        # token -> nick

MAX_POST_LEN = 1000
MAX_COMMENT_LEN = 500
MAX_BIO_LEN = 200
TRUNCATE_LINES = 100
TRUNCATE_CHARS = 500

NICK_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")

RU_COUNTRIES = {"RU", "BY", "KZ", "UA", "KG", "TJ", "UZ", "AM", "AZ", "MD"}
_lang_cache: Dict[str, str] = {}


def hash_password(password: str) -> str:
    salt = uuid.uuid4().hex
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return salt + "$" + h


def check_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except Exception:
        return False
    return hashlib.sha256((salt + password).encode()).hexdigest() == h


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
    nick = SESSIONS.get(token)
    if not nick:
        return None
    return db_load_user(nick)


def get_voter_id(request: Request) -> str:
    u = get_current_user(request)
    if u:
        return "u:" + u["nick"]
    return "c:anon"


def db_load_user(nick: str) -> Optional[dict]:
    """Читает пользователя. Сначала Supabase, потом память."""
    if supabase:
        try:
            r = supabase.table("users").select("*").ilike("nick", nick).limit(1).execute()
            if r.data:
                row = r.data[0]
                row["following"] = set(row.get("following") or [])
                row["followers"] = set(row.get("followers") or [])
                row["bio"] = row.get("bio") or ""
                return row
        except Exception as e:
            print("[sldChat] db_load_user error:", e)
        return None
    for u in USERS.values():
        if u["nick"].lower() == nick.lower():
            return u
    return None


def db_save_user(u: dict) -> None:
    """Пишет в память → в Supabase → выкидывает из памяти."""
    USERS[u["nick"]] = u
    if not supabase:
        return
    try:
        supabase.table("users").upsert({
            "nick": u["nick"],
            "name": u["name"],
            "bio": u.get("bio", ""),
            "password": u["password"],
            "created_at": u["created_at"],
            "following": list(u.get("following") or []),
            "followers": list(u.get("followers") or []),
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
                out.append(row)
            return out
        except Exception as e:
            print("[sldChat] db_all_users error:", e)
            return []
    return list(USERS.values())


# ==================================================================
# DB СЛОЙ: posts + votes + comments + votes + notifications
# ==================================================================
def db_create_post(p: dict) -> None:
    if not supabase:
        POSTS_MEM[p["id"]] = p
        return
    try:
        supabase.table("posts").insert({
            "id": p["id"],
            "text": p["text"],
            "author": p["author"],
            "created_at": p["created_at"],
        }).execute()
    except Exception as e:
        print("[sldChat] db_create_post error:", e)


def db_get_post(pid: str) -> Optional[dict]:
    if supabase:
        try:
            r = supabase.table("posts").select("*").eq("id", pid).limit(1).execute()
            if not r.data:
                return None
            return r.data[0]
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
            return query.execute().data or []
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
    """direction = 0 → удалить голос."""
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
            return supabase.table("comments").select("*").in_("post_id", post_ids).order("created_at").execute().data or []
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
            "text": c["text"], "created_at": c["created_at"],
        }).execute()
    except Exception as e:
        print("[sldChat] db_create_comment error:", e)


def db_get_comment(cid: str) -> Optional[dict]:
    if supabase:
        try:
            r = supabase.table("comments").select("*").eq("id", cid).limit(1).execute()
            return r.data[0] if r.data else None
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
        "to_nick": to_nick,
        "type": ntype,
        "from_nick": from_nick,
        "post_id": post_id,
        "comment_id": comment_id,
        "text": text,
        "read": False,
        "created_at": time.time(),
    }
    if supabase:
        try:
            supabase.table("notifications").insert(n).execute()
        except Exception as e:
            print("[sldChat] db_notify error:", e)
        return
    NOTIFS_MEM.setdefault(to_nick, []).append(n)


def db_notifications(nick: str) -> List[dict]:
    if supabase:
        try:
            r = (supabase.table("notifications").select("*")
                 .eq("to_nick", nick).order("created_at", desc=True).limit(100).execute())
            return r.data or []
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


# ==================================================================
# СЕРИАЛИЗАЦИЯ
# ==================================================================
def build_posts_full(posts: List[dict], voter_id: str) -> List[dict]:
    """Собирает посты с голосами и комментариями (батчами)."""
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
            cvotes = cvmap.get(c["id"], {})
            cup = sum(1 for d in cvotes.values() if d == 1)
            cdown = sum(1 for d in cvotes.values() if d == -1)
            cuv = cvotes.get(voter_id, 0)
            clist.append({
                "id": c["id"], "text": c["text"], "created_at": c["created_at"],
                "author": c.get("author"), "parent_id": c.get("parent_id"),
                "upvotes": cup, "downvotes": cdown, "user_vote": cuv,
            })
        out.append({
            "id": p["id"],
            "text": p["text"],
            "created_at": p["created_at"],
            "author": p.get("author"),
            "upvotes": up,
            "downvotes": down,
            "user_vote": uv,
            "comments": clist,
        })
    return out


def serialize_user(u: dict, viewer_nick: Optional[str] = None) -> dict:
    return {
        "nick": u["nick"],
        "name": u["name"],
        "bio": u.get("bio") or "",
        "created_at": u["created_at"],
        "followers": len(u.get("followers") or []),
        "following": len(u.get("following") or []),
        "is_me": viewer_nick == u["nick"],
    }


# фолбэк-память (используется только если supabase = None)
POSTS_MEM: Dict[str, dict] = {}
NOTIFS_MEM: Dict[str, List[dict]] = {}


# ==================================================================
# МОДЕЛИ
# ==================================================================
class PostIn(BaseModel):
    text: str


class VoteIn(BaseModel):
    direction: int


class CommentIn(BaseModel):
    text: str
    parent_id: Optional[str] = None


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


# ==================================================================
# API: АВТОРИЗАЦИЯ
# ==================================================================
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
    }
    db_save_user(u)
    token = uuid.uuid4().hex
    SESSIONS[token] = nick
    return {"token": token, "user": serialize_user(u, nick)}


@app.post("/api/login")
def api_login(data: LoginIn):
    nick = data.nick.strip().lstrip("@")
    u = db_load_user(nick)
    if not u or not check_password(data.password, u["password"]):
        raise HTTPException(400, "err_bad_login")
    token = uuid.uuid4().hex
    SESSIONS[token] = u["nick"]
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


# ==================================================================
# API: ПОСТЫ
# ==================================================================
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
    u = get_current_user(request)
    if not u:
        raise HTTPException(401, "unauthorized")
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_POST_LEN:
        raise HTTPException(400, "too long")
    pid = uuid.uuid4().hex[:10]
    p = {"id": pid, "text": text, "author": u["nick"], "created_at": time.time()}
    db_create_post(p)
    return build_posts_full([p], "u:" + u["nick"])[0]


@app.post("/api/posts/{pid}/vote")
def api_vote_post(pid: str, v: VoteIn, request: Request):
    if not db_get_post(pid):
        raise HTTPException(404, "not found")
    if v.direction not in (-1, 1):
        raise HTTPException(400, "bad request")
    u = get_current_user(request)
    if not u:
        raise HTTPException(401, "unauthorized")
    vid = "u:" + u["nick"]

    # узнать текущий голос
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
    u = get_current_user(request)
    if not u:
        raise HTTPException(401, "unauthorized")
    post = db_get_post(pid)
    if not post:
        raise HTTPException(404, "not found")
    text = c.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_COMMENT_LEN:
        raise HTTPException(400, "too long")

    parent_id = c.parent_id or None
    if parent_id:
        parent = db_get_comment(parent_id)
        if not parent or parent["post_id"] != pid:
            raise HTTPException(400, "bad parent")
        # только 1 уровень: у родителя не должно быть своего родителя
        if parent.get("parent_id"):
            raise HTTPException(400, "reply_only_one_level")

    cid = uuid.uuid4().hex[:10]
    db_create_comment({
        "id": cid, "post_id": pid, "author": u["nick"],
        "parent_id": parent_id, "text": text, "created_at": time.time(),
    })

    # уведомление автору поста (если не он сам пишет)
    db_notify(post["author"], "comment", u["nick"], post_id=pid, comment_id=cid, text=text[:140])
    # если это ответ — уведомим автора родительского коммента
    if parent_id:
        p2 = db_get_comment(parent_id)
        if p2 and p2["author"] != u["nick"]:
            db_notify(p2["author"], "reply", u["nick"], post_id=pid, comment_id=cid, text=text[:140])

    return build_posts_full([post], "u:" + u["nick"])[0]


@app.post("/api/posts/{pid}/comments/{cid}/vote")
def api_vote_comment(pid: str, cid: str, v: VoteIn, request: Request):
    if not db_get_post(pid):
        raise HTTPException(404, "not found")
    if v.direction not in (-1, 1):
        raise HTTPException(400, "bad request")
    u = get_current_user(request)
    if not u:
        raise HTTPException(401, "unauthorized")
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


# ==================================================================
# API: ПОЛЬЗОВАТЕЛИ / ФОЛЛОВЕРЫ
# ==================================================================
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
    out = []
    for n in (u.get("followers") or []):
        fu = db_load_user(n)
        if fu:
            out.append(serialize_user(fu))
    out.sort(key=lambda x: x["nick"].lower())
    return {"users": out}


@app.get("/api/users/{nick}/following")
def api_following(nick: str, request: Request):
    u = db_load_user(nick)
    if not u:
        raise HTTPException(404, "not found")
    out = []
    for n in (u.get("following") or []):
        fu = db_load_user(n)
        if fu:
            out.append(serialize_user(fu))
    out.sort(key=lambda x: x["nick"].lower())
    return {"users": out}


@app.post("/api/users/{nick}/follow")
def api_follow(nick: str, request: Request):
    me = get_current_user(request)
    if not me:
        raise HTTPException(401, "unauthorized")
    target = db_load_user(nick)
    if not target:
        raise HTTPException(404, "not found")
    if target["nick"] == me["nick"]:
        raise HTTPException(400, "self")

    if target["nick"] not in (me.get("following") or set()):
        new_following = set(me.get("following") or set())
        new_following.add(target["nick"])
        db_update_user_fields(me["nick"], {"following": list(new_following)})

        new_followers = set(target.get("followers") or set())
        new_followers.add(me["nick"])
        db_update_user_fields(target["nick"], {"followers": list(new_followers)})

        db_notify(target["nick"], "follow", me["nick"])

    target = db_load_user(nick)
    data = serialize_user(target, me["nick"])
    data["is_following"] = True
    return data


@app.post("/api/users/{nick}/unfollow")
def api_unfollow(nick: str, request: Request):
    me = get_current_user(request)
    if not me:
        raise HTTPException(401, "unauthorized")
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


@app.post("/api/users/me/bio")
def api_set_bio(data: BioIn, request: Request):
    me = get_current_user(request)
    if not me:
        raise HTTPException(401, "unauthorized")
    bio = data.bio.strip()
    if len(bio) > MAX_BIO_LEN:
        raise HTTPException(400, "too long")
    db_update_user_fields(me["nick"], {"bio": bio})
    return {"ok": True, "bio": bio}


# ==================================================================
# API: УВЕДОМЛЕНИЯ
# ==================================================================
@app.get("/api/notifications")
def api_notifications(request: Request):
    me = get_current_user(request)
    if not me:
        raise HTTPException(401, "unauthorized")
    items = db_notifications(me["nick"])
    unread = sum(1 for n in items if not n.get("read"))
    return {"items": items, "unread": unread}


@app.post("/api/notifications/read")
def api_notifications_read(request: Request):
    me = get_current_user(request)
    if not me:
        raise HTTPException(401, "unauthorized")
    db_notifications_mark_read(me["nick"])
    return {"ok": True}


@app.post("/api/notifications/clear")
def api_notifications_clear(request: Request):
    me = get_current_user(request)
    if not me:
        raise HTTPException(401, "unauthorized")
    db_notifications_clear(me["nick"])
    return {"ok": True}


# ==================================================================
# ТЕКСТЫ
# ==================================================================
TEXTS = {
    "ru": {
        "search_ph": "Поиск по постам",
        "search": "Поиск",
        "theme": "Сменить тему",
        "post_ph": "Написать пост (до 1000 символов)",
        "comment_ph": "Написать комментарий",
        "reply_ph": "Ответить на комментарий",
        "publish": "Опубликовать",
        "send_comment": "Отправить",
        "reply": "Ответить",
        "cancel_reply": "Отмена",
        "no_posts": "Постов пока нет",
        "not_found": "Не найдено",
        "just_now": "только что",
        "sec_ago": "с", "min_ago": "мин", "hour_ago": "ч", "day_ago": "д",
        "read_more": "читать дальше",
        "copy": "Копировать", "copied": "Скопировано",
        "author_badge": "автор",
        "f_new": "Новые", "f_top": "Лучшие", "f_bottom": "Худшие", "f_old": "Старые",
        "f_all": "Все", "f_many": "Много комм.", "f_some": "Есть комм.", "f_none": "Без комм.",
        "nav_home": "Главная", "nav_profile": "Профиль",
        "nav_notifications": "Уведомления", "nav_settings": "Настройки",
        "nav_logout": "Выйти", "nav_register": "Регистрация", "nav_login": "Вход",
        "reg_title": "Регистрация", "log_title": "Вход",
        "name_ph": "Имя", "nick_ph": "Ник (@nick)",
        "pass_ph": "Пароль", "pass2_ph": "Повтор пароля",
        "reg_btn": "Создать аккаунт", "log_btn": "Войти",
        "to_login": "Уже есть аккаунт? Войти",
        "to_reg": "Нет аккаунта? Регистрация",
        "err_bad_nick": "Ник: 3-20 символов, латиница, цифры, _",
        "err_bad_name": "Имя: от 1 до 50 символов",
        "err_short_pass": "Пароль: минимум 6 символов",
        "err_pass_mismatch": "Пароли не совпадают",
        "err_nick_taken": "Ник уже занят",
        "err_bad_login": "Неверный ник или пароль",
        "err_auth_required": "Требуется вход",
        "login_to_post": "Войдите, чтобы писать посты",
        "login_to_comment": "Войдите, чтобы писать комментарии",
        "go_login": "Войти", "go_register": "Регистрация",
        "profile_followers": "подписчиков",
        "profile_following": "подписок",
        "follow": "Подписаться", "unfollow": "Отписаться",
        "own_profile": "Это ваш профиль",
        "no_user_posts": "Постов пока нет",
        "settings_title": "Настройки",
        "settings_theme": "Тема", "settings_lang": "Язык",
        "theme_light": "Светлая", "theme_dark": "Тёмная",
        "notif_title": "Уведомления",
        "notif_empty": "Уведомлений нет",
        "notif_follow": "подписался(-ась) на вас",
        "notif_comment": "оставил(а) комментарий",
        "notif_reply": "ответил(а) на ваш комментарий",
        "notif_clear": "Очистить все",
        "back_to_main": "В главное меню",
        "bio_ph": "Описание профиля...",
        "bio_save": "Сохранить",
        "bio_saved": "Сохранено",
        "bio_empty": "Описание пока не заполнено",
        "edit_bio": "Редактировать",
        "followers_title": "Подписчики",
        "following_title": "Подписки",
        "no_followers": "Подписчиков пока нет",
        "no_following": "Подписок пока нет",
    },
    "en": {
        "search_ph": "Search posts", "search": "Search", "theme": "Toggle theme",
        "post_ph": "Write a post (up to 1000 chars)",
        "comment_ph": "Write a comment",
        "reply_ph": "Reply to comment",
        "publish": "Publish", "send_comment": "Send",
        "reply": "Reply", "cancel_reply": "Cancel",
        "no_posts": "No posts yet", "not_found": "Not found",
        "just_now": "just now",
        "sec_ago": "s", "min_ago": "min", "hour_ago": "h", "day_ago": "d",
        "read_more": "read more",
        "copy": "Copy", "copied": "Copied",
        "author_badge": "author",
        "f_new": "New", "f_top": "Top", "f_bottom": "Worst", "f_old": "Old",
        "f_all": "All", "f_many": "Many", "f_some": "Some", "f_none": "None",
        "nav_home": "Home", "nav_profile": "Profile",
        "nav_notifications": "Notifications", "nav_settings": "Settings",
        "nav_logout": "Log out", "nav_register": "Sign up", "nav_login": "Log in",
        "reg_title": "Sign up", "log_title": "Log in",
        "name_ph": "Name", "nick_ph": "Nick (@nick)",
        "pass_ph": "Password", "pass2_ph": "Confirm password",
        "reg_btn": "Create account", "log_btn": "Log in",
        "to_login": "Already have an account? Log in",
        "to_reg": "No account? Sign up",
        "err_bad_nick": "Nick: 3-20 chars, letters/digits/_",
        "err_bad_name": "Name: 1-50 chars",
        "err_short_pass": "Password: min 6 chars",
        "err_pass_mismatch": "Passwords do not match",
        "err_nick_taken": "Nick already taken",
        "err_bad_login": "Wrong nick or password",
        "err_auth_required": "Login required",
        "login_to_post": "Log in to write posts",
        "login_to_comment": "Log in to write comments",
        "go_login": "Log in", "go_register": "Sign up",
        "profile_followers": "followers", "profile_following": "following",
        "follow": "Follow", "unfollow": "Unfollow",
        "own_profile": "This is your profile",
        "no_user_posts": "No posts yet",
        "settings_title": "Settings", "settings_theme": "Theme", "settings_lang": "Language",
        "theme_light": "Light", "theme_dark": "Dark",
        "notif_title": "Notifications", "notif_empty": "No notifications",
        "notif_follow": "followed you",
        "notif_comment": "commented",
        "notif_reply": "replied to your comment",
        "notif_clear": "Clear all",
        "back_to_main": "Back to main",
        "bio_ph": "Profile bio...", "bio_save": "Save", "bio_saved": "Saved",
        "bio_empty": "No bio yet", "edit_bio": "Edit",
        "followers_title": "Followers", "following_title": "Following",
        "no_followers": "No followers yet", "no_following": "No following yet",
    },
}


# ==================================================================
# SVG-ИКОНКИ
# ==================================================================
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
ICON_HOME = svg('<path d="M3 10l9-7 9 7v11a2 2 0 0 1-2 2h-4v-8h-6v8H5a2 2 0 0 1-2-2z"/>', size=15)
ICON_USER = svg('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>', size=15)
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


# ==================================================================
# CSS
# ==================================================================
CSS = """
:root, [data-theme="light"] {
  --bg:#ebebeb; --card:#ffffff; --line:#d4d4d4; --line-strong:#a8a8a8;
  --text:#101010; --muted:#767676; --hover:#f0f0f0;
  --accent:#101010; --accent-fg:#ffffff;
  --up:#1f9d55; --down:#d84343; --comment-bg:#f6f6f6;
  --danger:#d84343;
}
[data-theme="dark"] {
  --bg:#0a0a0a; --card:#141414; --line:#282828; --line-strong:#3a3a3a;
  --text:#ececec; --muted:#888888; --hover:#1e1e1e;
  --accent:#ececec; --accent-fg:#101010;
  --up:#2ecc71; --down:#e74c3c; --comment-bg:#1c1c1c;
  --danger:#e74c3c;
}
* { box-sizing: border-box; }
html, body {
  height: 100vh; margin: 0; padding: 0; overflow: hidden;
}
body {
  font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg); color: var(--text); font-size: 14px;
  -webkit-font-smoothing: antialiased;
  user-select: none; -webkit-user-select: none; -ms-user-select: none;
  display: flex;
}
input, textarea { user-select: text; -webkit-user-select: text; -ms-user-select: text; }

/* ============ Кастомные скроллбары ============ */
* { scrollbar-width: thin; scrollbar-color: var(--line-strong) transparent; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: var(--line-strong); border: 2px solid var(--card);
}
::-webkit-scrollbar-thumb:hover { background: var(--muted); }
::-webkit-scrollbar-corner { background: transparent; }

.layout {
  display: flex; width: 100%; height: 100vh;
  background: var(--card);
}
.main {
  flex: 1 1 auto; min-width: 0;
  display: flex; flex-direction: column;
  background: var(--card);
}
.main-header {
  flex: 0 0 auto; display: flex; align-items: center; gap: 6px;
  padding: 10px 12px; border-bottom: 1px solid var(--line);
}
.main-header .title {
  flex: 1; font-size: 15px; font-weight: 600; padding: 0 4px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.main-body { flex: 1 1 auto; overflow-y: auto; }

/* ============ Sidebar ============ */
.sidebar {
  flex: 0 0 260px; width: 260px;
  border-left: 1px solid var(--line);
  background: var(--card);
  display: flex; flex-direction: column;
  padding: 16px 12px;
}
.sidebar .logo {
  font-size: 20px; font-weight: 700; letter-spacing: -0.5px;
  padding: 4px 12px 20px;
}
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-btn {
  display: flex; align-items: center; gap: 10px;
  width: 100%; padding: 9px 12px;
  border: none; background: transparent;
  color: var(--text); font: inherit; font-size: 14px;
  cursor: pointer; text-align: left;
  transition: background .12s;
}
.nav-btn:hover { background: var(--hover); }
.nav-btn.active { background: var(--hover); font-weight: 600; }
.nav-btn svg { flex-shrink: 0; color: var(--muted); }
.nav-btn.active svg { color: var(--text); }
.sidebar .spacer { flex: 1; }
.sidebar-user {
  padding: 10px 12px; font-size: 13px; color: var(--muted);
  border-top: 1px solid var(--line);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* ============ Icon btn ============ */
.icon-btn {
  width: 32px; height: 32px;
  display: inline-flex; align-items: center; justify-content: center;
  background: transparent; border: 1px solid var(--line);
  color: var(--text); cursor: pointer; padding: 0; text-decoration: none;
  transition: background .12s, border-color .12s;
  flex-shrink: 0;
}
.icon-btn:hover { background: var(--hover); border-color: var(--line-strong); }
.icon-btn svg { display: block; }

/* ============ Search & filters ============ */
header.search-header {
  flex: 0 0 auto; display: flex; align-items: center; gap: 6px;
  padding: 10px 12px; border-bottom: 1px solid var(--line);
}
header.search-header input[type="search"] {
  flex: 1; min-width: 0; padding: 0 12px; height: 32px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-size: 14px; font-family: inherit;
  outline: none; transition: border-color .12s;
}
header.search-header input[type="search"]:focus { border-color: var(--line-strong); }

.filters {
  flex: 0 0 auto; display: flex; align-items: center;
  padding: 6px 8px; border-bottom: 1px solid var(--line);
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

/* ============ Posts ============ */
.empty { padding: 80px 20px; text-align: center; color: var(--muted); font-size: 13px; }
.post { padding: 14px 18px; border-bottom: 1px solid var(--line); background: var(--card); }
.post-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 12px; }
.post-author { font-weight: 600; color: var(--text); text-decoration: none; font-size: 13px; }
.post-author:hover { text-decoration: underline; }
.post-time { color: var(--muted); font-size: 12px; margin-left: auto; }
.post-text {
  font-size: 15px; line-height: 1.5; white-space: pre-wrap;
  word-wrap: break-word; overflow-wrap: anywhere; color: var(--text);
}
.read-more {
  display: inline-block; margin-top: 6px; color: var(--muted);
  text-decoration: none; font-size: 13px; border-bottom: 1px dashed currentColor;
}
.read-more:hover { color: var(--text); }
.post-actions { display: flex; align-items: center; gap: 1px; margin-top: 10px; font-size: 12px; color: var(--muted); }
.vote-btn, .action-btn {
  display: inline-flex; align-items: center; gap: 5px;
  height: 26px; padding: 0 8px; background: transparent; border: none;
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
.score {
  min-width: 18px; padding: 0 2px; text-align: center;
  font-weight: 600; font-size: 12px; color: var(--muted);
}
.score.up { color: var(--up); }
.score.down { color: var(--down); }

/* ============ Comments ============ */
.comments { margin-top: 12px; border-top: 1px solid var(--line); }
.comment {
  padding: 10px 12px; margin-top: 8px;
  background: var(--comment-bg); border-left: 2px solid var(--line-strong);
}
.comment.reply {
  margin-left: 28px; border-left-color: var(--muted);
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
  letter-spacing: .5px; padding: 1px 6px; border: 1px solid currentColor;
  color: var(--text);
}
.comment-time { font-size: 11px; color: var(--muted); margin-left: auto; }
.comment-text { font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; color: var(--text); }
.comment-actions { display: flex; align-items: center; gap: 1px; margin-top: 6px; font-size: 11px; color: var(--muted); }
.comment-actions .vote-btn { height: 22px; padding: 0 6px; font-size: 11px; }
.comment-actions .score { font-size: 11px; min-width: 14px; }
.comment-reply-btn {
  background: transparent; border: none; color: var(--muted);
  font-family: inherit; font-size: 11px; cursor: pointer;
  padding: 4px 8px; height: 22px; transition: color .12s, background .12s;
}
.comment-reply-btn:hover { color: var(--text); background: var(--hover); }

/* ============ Composer ============ */
.composer { flex: 0 0 auto; border-top: 1px solid var(--line); background: var(--card); padding: 10px 12px; }
.composer textarea {
  display: block; width: 100%; min-height: 160px; padding: 12px 14px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-family: inherit; font-size: 14px; line-height: 1.5;
  outline: none; resize: none; transition: border-color .12s;
}
.composer.composer-comment textarea { min-height: 90px; }
.composer textarea:focus { border-color: var(--line-strong); }
.composer textarea::placeholder { color: var(--muted); }
.composer textarea:disabled { opacity: .5; cursor: not-allowed; }
.composer-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 8px; }
.composer-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.counter { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.counter.warn { color: var(--danger); }
.reply-banner {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  background: var(--comment-bg); border: 1px solid var(--line);
  font-size: 12px; color: var(--muted); margin-bottom: 6px;
}
.reply-banner b { color: var(--text); font-weight: 600; }
.reply-banner button {
  margin-left: auto; background: transparent; border: none;
  color: var(--text); font-family: inherit; font-size: 12px;
  cursor: pointer; padding: 2px 8px; transition: background .12s;
}
.reply-banner button:hover { background: var(--hover); }

button.send {
  height: 32px; padding: 0 18px;
  background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent);
  font-family: inherit; font-size: 13px; font-weight: 500;
  cursor: pointer; transition: opacity .12s;
}
button.send:hover { opacity: .82; }
button.send:disabled { opacity: .28; cursor: default; }
.login-prompt { padding: 14px; text-align: center; color: var(--muted); font-size: 13px; }
.login-prompt a { color: var(--text); }

/* ============ Auth ============ */
.auth-wrap { max-width: 380px; margin: 0 auto; padding: 40px 20px; }
.auth-title { font-size: 22px; font-weight: 700; margin: 0 0 24px; letter-spacing: -0.5px; }
.auth-form { display: flex; flex-direction: column; gap: 10px; }
.auth-form input {
  width: 100%; padding: 11px 12px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-family: inherit; font-size: 14px;
  outline: none; transition: border-color .12s;
}
.auth-form input:focus { border-color: var(--line-strong); }
.auth-form button {
  margin-top: 6px; height: 40px;
  background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent);
  font-family: inherit; font-size: 14px; font-weight: 600;
  cursor: pointer; transition: opacity .12s;
}
.auth-form button:hover { opacity: .85; }
.auth-form button:disabled { opacity: .5; cursor: default; }
.auth-error { color: var(--danger); font-size: 13px; min-height: 18px; }
.auth-switch { margin-top: 20px; font-size: 13px; color: var(--muted); text-align: center; }
.auth-switch a { color: var(--text); cursor: pointer; text-decoration: underline; }

/* ============ Profile ============ */
.profile-header { padding: 24px 18px; border-bottom: 1px solid var(--line); }
.profile-name-big { font-size: 22px; font-weight: 700; letter-spacing: -0.4px; }
.profile-nick-small { color: var(--muted); font-size: 14px; margin-top: 2px; }
.profile-bio {
  margin-top: 12px; font-size: 14px; color: var(--text); line-height: 1.5;
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
}
.profile-bio-empty { color: var(--muted); font-style: italic; }
.profile-stats {
  display: flex; gap: 20px; margin-top: 14px; font-size: 13px; color: var(--muted);
  user-select: none;
}
.profile-stats b { color: var(--text); font-weight: 600; cursor: pointer; }
.profile-stats b:hover { text-decoration: underline; }
.profile-actions { margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap; }
.follow-btn {
  height: 32px; padding: 0 20px;
  background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent);
  font-family: inherit; font-size: 13px; font-weight: 600;
  cursor: pointer; transition: opacity .12s;
}
.follow-btn:hover { opacity: .85; }
.follow-btn.following { background: transparent; color: var(--text); border-color: var(--line-strong); }
.own-note { font-size: 13px; color: var(--muted); }

.bio-editor { margin-top: 12px; }
.bio-editor textarea {
  width: 100%; padding: 10px 12px; min-height: 70px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-family: inherit; font-size: 14px; line-height: 1.5;
  outline: none; resize: none;
}
.bio-editor textarea:focus { border-color: var(--line-strong); }
.bio-editor-row { display: flex; gap: 8px; margin-top: 6px; align-items: center; }
.btn-secondary {
  height: 32px; padding: 0 16px;
  background: transparent; color: var(--text);
  border: 1px solid var(--line);
  font-family: inherit; font-size: 13px; cursor: pointer;
  transition: background .12s, border-color .12s;
}
.btn-secondary:hover { background: var(--hover); border-color: var(--line-strong); }
.bio-saved-msg { font-size: 12px; color: var(--up); }

/* ============ Followers lists ============ */
.user-list { padding: 6px 0; }
.user-item {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 18px; border-bottom: 1px solid var(--line);
}
.user-item .user-info { flex: 1; min-width: 0; }
.user-item .user-nick {
  font-weight: 600; color: var(--text); text-decoration: none;
  font-size: 14px; display: block;
}
.user-item .user-nick:hover { text-decoration: underline; }
.user-item .user-name { font-size: 12px; color: var(--muted); margin-top: 2px; }
.user-item .follow-btn { height: 28px; padding: 0 14px; font-size: 12px; }

/* ============ Notifications ============ */
.notif-list { padding: 6px 0; }
.notif {
  display: block; padding: 12px 18px; border-bottom: 1px solid var(--line);
  font-size: 14px; line-height: 1.5; text-decoration: none; color: inherit;
}
.notif.unread { background: var(--hover); }
.notif-head { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.notif-author { font-weight: 600; color: var(--text); }
.notif-text { color: var(--muted); font-size: 13px; }
.notif-snippet {
  margin-top: 6px; padding: 8px 10px;
  background: var(--comment-bg); border-left: 2px solid var(--line-strong);
  font-size: 13px; color: var(--text);
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
}
.notif-time { font-size: 11px; color: var(--muted); margin-top: 4px; }
.notif-actions {
  padding: 12px 18px; border-bottom: 1px solid var(--line);
  display: flex; justify-content: flex-end;
}
.btn-danger {
  height: 30px; padding: 0 14px;
  background: transparent; color: var(--danger);
  border: 1px solid var(--danger);
  font-family: inherit; font-size: 12px; cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px;
  transition: background .12s;
}
.btn-danger:hover { background: var(--danger); color: var(--card); }

/* ============ Settings ============ */
.settings { padding: 24px 18px; }
.settings h2 {
  font-size: 13px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .5px; color: var(--muted); margin: 0 0 12px;
}
.settings-section { margin-bottom: 28px; }
.opt-row { display: flex; gap: 6px; flex-wrap: wrap; }
.opt {
  height: 32px; padding: 0 16px; background: transparent; color: var(--text);
  border: 1px solid var(--line); font-family: inherit; font-size: 13px;
  cursor: pointer; transition: border-color .12s, background .12s;
}
.opt:hover { background: var(--hover); }
.opt.active { background: var(--hover); border-color: var(--line-strong); font-weight: 600; }

/* ============ Highlight comment ============ */
.comment.highlight { animation: flash 1.6s ease-out; }
@keyframes flash {
  0% { background: var(--up); color: #fff; }
  100% { background: var(--comment-bg); }
}
"""


# ==================================================================
# JS
# ==================================================================
JS = r"""
var ICONS = {
  up: __ICON_UP__, down: __ICON_DOWN__, comment: __ICON_COMMENT__,
  copy: __ICON_COPY__, check: __ICON_CHECK__,
  moon: __ICON_MOON__, sun: __ICON_SUN__, back: __ICON_BACK__,
  home: __ICON_HOME__, user: __ICON_USER__, bell: __ICON_BELL__,
  gear: __ICON_GEAR__, logout: __ICON_LOGOUT__, plus: __ICON_PLUS__,
  login: __ICON_LOGIN__, trash: __ICON_TRASH__, search: __ICON_SEARCH__
};

var state = {
  user: null,
  token: localStorage.getItem('sldchat_token') || null,
  view: VIEW,
  viewData: VIEW_DATA || {},
  unread: 0,
  sortMode: 'new',
  commentFilter: 'any',
  searchQuery: '',
  composerDraft: '',
  replyTo: null,       // {id, author, text}
  highlightComment: null
};

// ============ HTTP ============
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
    state.user = null; state.token = null;
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

// ============ Utils ============
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
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

// ============ Theme ============
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

// ============ Navigation ============
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
  else if (path === '/notifications') { state.view = 'notifications'; state.viewData = {}; }
  else if (path === '/settings') { state.view = 'settings'; state.viewData = {}; }
  else if (path === '/register') { state.view = 'register'; state.viewData = {}; }
  else if (path === '/login') { state.view = 'login'; state.viewData = {}; }
  else { state.view = 'feed'; state.viewData = {}; }
  renderSidebar();
  renderMain();
}
window.addEventListener('popstate', handleRoute);

// ============ Auth ============
async function loadMe() {
  if (!state.token) return;
  try { state.user = await api('/api/me'); } catch(e) { state.user = null; }
}
async function doRegister(data) {
  var res = await api('/api/register', { method: 'POST', body: data });
  state.token = res.token;
  localStorage.setItem('sldchat_token', res.token);
  state.user = res.user;
  navigate('/');
}
async function doLogin(data) {
  var res = await api('/api/login', { method: 'POST', body: data });
  state.token = res.token;
  localStorage.setItem('sldchat_token', res.token);
  state.user = res.user;
  navigate('/');
}
async function doLogout() {
  try { await api('/api/logout', { method: 'POST' }); } catch(e) {}
  state.user = null; state.token = null;
  localStorage.removeItem('sldchat_token');
  navigate('/');
}

// ============ Sidebar ============
function navBtn(icon, label, active, action, count) {
  var cls = 'nav-btn' + (active ? ' active' : '');
  var labelWithCount = (count && count > 0) ? (label + ' (' + count + ')') : label;
  return '<button class="' + cls + '" data-nav="' + action + '">'
    + icon + '<span>' + escapeHtml(labelWithCount) + '</span></button>';
}
function renderSidebar() {
  var el = document.getElementById('sidebar');
  var html = '<div class="logo">sldchat</div><div class="nav">';
  html += navBtn(ICONS.home, tr('nav_home'), state.view === 'feed', 'home');
  if (state.user) {
    html += navBtn(ICONS.user, tr('nav_profile'),
      state.view === 'profile' && state.viewData.nick === state.user.nick, 'profile');
    html += navBtn(ICONS.bell, tr('nav_notifications'),
      state.view === 'notifications', 'notifications', state.unread);
    html += navBtn(ICONS.gear, tr('nav_settings'), state.view === 'settings', 'settings');
    html += navBtn(ICONS.logout, tr('nav_logout'), false, 'logout');
  } else {
    html += navBtn(ICONS.gear, tr('nav_settings'), state.view === 'settings', 'settings');
    html += navBtn(ICONS.plus, tr('nav_register'), state.view === 'register', 'register');
    html += navBtn(ICONS.login, tr('nav_login'), state.view === 'login', 'login');
  }
  html += '</div><div class="spacer"></div>';
  if (state.user) html += '<div class="sidebar-user">@' + escapeHtml(state.user.nick) + '</div>';
  el.innerHTML = html;
  el.querySelectorAll('[data-nav]').forEach(function(b){
    b.addEventListener('click', function(){
      var nav = b.dataset.nav;
      if (nav === 'home') navigate('/');
      else if (nav === 'profile') navigate('/u/' + encodeURIComponent(state.user.nick));
      else if (nav === 'notifications') navigate('/notifications');
      else if (nav === 'settings') navigate('/settings');
      else if (nav === 'register') navigate('/register');
      else if (nav === 'login') navigate('/login');
      else if (nav === 'logout') doLogout();
    });
  });
}

// ============ Main dispatcher ============
function renderMain() {
  var el = document.getElementById('main');
  if (state.view === 'feed') renderFeedView(el);
  else if (state.view === 'post') renderPostView(el);
  else if (state.view === 'profile') renderProfileView(el);
  else if (state.view === 'followers' || state.view === 'following') renderFollowListView(el);
  else if (state.view === 'notifications') renderNotificationsView(el);
  else if (state.view === 'settings') renderSettingsView(el);
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

// ============ Feed ============
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
  html += '<div class="main-body"><div id="feed"><div class="empty">…</div></div></div>';

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

// ============ Post view ============
function renderPostView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title"></div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="feed"><div class="empty">…</div></div></div>';
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

// ============ Profile ============
function renderProfileView(el) {
  var nick = state.viewData.nick || '';
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">@' + escapeHtml(nick) + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="profileHeader"></div><div id="feed"><div class="empty">…</div></div></div>';
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
    // имя сверху, ник снизу
    h += '<div class="profile-name-big">' + escapeHtml(u.name) + '</div>';
    h += '<div class="profile-nick-small">@' + escapeHtml(u.nick) + '</div>';

    // bio
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
    if (isMe) h += '<span class="own-note">' + tr('own_profile') + '</span>';
    else if (state.user) {
      h += '<button class="follow-btn' + (u.is_following ? ' following' : '') + '" id="followBtn">'
        + (u.is_following ? tr('unfollow') : tr('follow')) + '</button>';
    } else {
      h += '<a class="follow-btn" href="/login" data-link style="display:inline-flex;align-items:center;justify-content:center;text-decoration:none">' + tr('go_login') + '</a>';
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

    var data = await api('/api/posts?author=' + encodeURIComponent(u.nick));
    var posts = (data.posts || []).slice();
    posts.sort(function(a, b){ return b.created_at - a.created_at; });
    if (!posts.length) feedEl.innerHTML = '<div class="empty">' + escapeHtml(tr('no_user_posts')) + '</div>';
    else { feedEl.innerHTML = posts.map(function(p){ return renderPostHtml(p, false); }).join(''); bindPostActions(feedEl); }
  } catch(e) {
    headerEl.innerHTML = '<div class="empty">' + escapeHtml(tr('not_found')) + '</div>';
    feedEl.innerHTML = '';
  }
}

// ============ Followers / Following ============
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
  html += '<div class="main-body"><div id="list"><div class="empty">…</div></div></div>';
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
    wrap.innerHTML = '<div class="empty">—</div>';
  }
}

// ============ Notifications ============
function renderNotificationsView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('notif_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="notifList"><div class="empty">…</div></div></div>';
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
    if (data.unread > 0) {
      api('/api/notifications/read', { method: 'POST' }).catch(function(){});
      state.unread = 0;
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
  var author = '<a class="notif-author" href="/u/' + encodeURIComponent(n.from_nick) + '" data-link>@' + escapeHtml(n.from_nick) + '</a>';
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
  } else {
    text = '<div class="notif-text">' + author + '</div>';
  }
  var inner = text + '<div class="notif-time">' + timeAgo(n.created_at) + '</div>';
  if (link) {
    return '<a class="' + cls + '" href="' + link + '" data-link>' + inner + '</a>';
  }
  return '<div class="' + cls + '">' + inner + '</div>';
}

// ============ Settings ============
function renderSettingsView(el) {
  var theme = document.documentElement.getAttribute('data-theme') || 'light';
  var lang = LANG;
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('settings_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div class="settings">';
  html += '<div class="settings-section"><h2>' + tr('settings_theme') + '</h2><div class="opt-row">';
  html += '<button class="opt' + (theme==='light'?' active':'') + '" data-set-theme="light">' + tr('theme_light') + '</button>';
  html += '<button class="opt' + (theme==='dark'?' active':'') + '" data-set-theme="dark">' + tr('theme_dark') + '</button>';
  html += '</div></div>';
  html += '<div class="settings-section"><h2>' + tr('settings_lang') + '</h2><div class="opt-row">';
  html += '<button class="opt' + (lang==='ru'?' active':'') + '" data-set-lang="ru">Русский</button>';
  html += '<button class="opt' + (lang==='en'?' active':'') + '" data-set-lang="en">English</button>';
  html += '</div></div>';
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
}

// ============ Register ============
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

// ============ Login ============
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

// ============ Post HTML ============
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
  var readMore = truncated
    ? '<a class="read-more" href="/p/' + p.id + '" data-link>… ' + escapeHtml(tr('read_more')) + '</a>'
    : '';

  var authorLink = p.author
    ? '<a class="post-author" href="/u/' + encodeURIComponent(p.author) + '" data-link>@' + escapeHtml(p.author) + '</a>'
    : '';

  var commentBtn = '<button class="action-btn" data-action="comment" data-post-id="' + p.id + '">'
    + ICONS.comment + '<span>' + (p.comments ? p.comments.length : 0) + '</span></button>';
  var copyBtn = '<button class="action-btn" data-action="copy" data-post-id="' + p.id + '" title="' + escapeHtml(tr('copy')) + '">'
    + ICONS.copy + '</button>';

  var commentsHtml = '';
  if (showComments && p.comments && p.comments.length) {
    commentsHtml = '<div class="comments">' + renderCommentsTree(p.comments, p.author, p.id) + '</div>';
  }

  return ''
    + '<div class="post" data-post-id="' + p.id + '">'
    +   '<div class="post-meta">' + authorLink + '<span class="post-time">' + timeAgo(p.created_at) + '</span></div>'
    +   '<div class="post-text">' + escapeHtml(displayText) + '</div>'
    +   readMore
    +   '<div class="post-actions">'
    +     '<button class="vote-btn up ' + upCls + '" data-action="vote" data-post-id="' + p.id + '" data-dir="1">' + ICONS.up + '</button>'
    +     '<span class="score ' + scCls + '">' + score + '</span>'
    +     '<button class="vote-btn down ' + downCls + '" data-action="vote" data-post-id="' + p.id + '" data-dir="-1">' + ICONS.down + '</button>'
    +     commentBtn + copyBtn
    +   '</div>'
    +   commentsHtml
    + '</div>';
}

function renderCommentsTree(comments, postAuthor, postId) {
  // top-level
  var tops = comments.filter(function(c){ return !c.parent_id; }).sort(function(a,b){ return a.created_at - b.created_at; });
  var repliesBy = {};
  comments.forEach(function(c){
    if (c.parent_id) {
      (repliesBy[c.parent_id] = repliesBy[c.parent_id] || []).push(c);
    }
  });
  var html = '';
  tops.forEach(function(c){
    html += renderCommentHtml(c, postAuthor, postId, false);
    var reps = repliesBy[c.id] || [];
    reps.sort(function(a,b){ return a.created_at - b.created_at; });
    reps.forEach(function(r){
      html += renderCommentHtml(r, postAuthor, postId, true);
    });
  });
  return html;
}

function renderCommentHtml(c, postAuthor, postId, isReply) {
  var score = c.upvotes - c.downvotes;
  var upCls = c.user_vote === 1 ? 'active' : '';
  var downCls = c.user_vote === -1 ? 'active' : '';
  var scCls = scoreClass(c.upvotes, c.downvotes);
  var isAuthor = postAuthor && c.author === postAuthor;
  var cls = 'comment' + (isReply ? ' reply' : '') + (isAuthor ? ' is-author' : '');
  var authorHtml = c.author
    ? '<a class="comment-author" href="/u/' + encodeURIComponent(c.author) + '" data-link>@' + escapeHtml(c.author) + '</a>'
    : '';
  var badge = isAuthor
    ? '<span class="comment-author-badge">' + escapeHtml(tr('author_badge')) + '</span>'
    : '';
  // Кнопка "Ответить" — только для топ-уровневых комментариев и только для залогиненных
  var replyBtn = '';
  if (!isReply && state.user) {
    replyBtn = '<button class="comment-reply-btn" data-action="reply" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-author="' + escapeHtml(c.author || '') + '">' + tr('reply') + '</button>';
  }

  return ''
    + '<div class="' + cls + '" data-comment-id="' + c.id + '">'
    +   '<div class="comment-meta">' + authorHtml + badge + '<span class="comment-time">' + timeAgo(c.created_at) + '</span></div>'
    +   '<div class="comment-text">' + escapeHtml(c.text) + '</div>'
    +   '<div class="comment-actions">'
    +     '<button class="vote-btn up ' + upCls + '" data-action="vote-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-dir="1">' + ICONS.up + '</button>'
    +     '<span class="score ' + scCls + '">' + score + '</span>'
    +     '<button class="vote-btn down ' + downCls + '" data-action="vote-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-dir="-1">' + ICONS.down + '</button>'
    +     replyBtn
    +   '</div>'
    + '</div>';
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
      try {
        if (action === 'vote') {
          if (!state.user) { navigate('/login'); return; }
          await api('/api/posts/' + postId + '/vote', { method: 'POST', body: { direction: dir } });
          refreshCurrentView();
        } else if (action === 'vote-comment') {
          if (!state.user) { navigate('/login'); return; }
          await api('/api/posts/' + postId + '/comments/' + commentId + '/vote', { method: 'POST', body: { direction: dir } });
          refreshCurrentView();
        } else if (action === 'comment') {
          navigate('/p/' + postId);
        } else if (action === 'copy') {
          await copyPost(postId, btn);
        } else if (action === 'reply') {
          state.replyTo = { id: commentId, author: btn.dataset.author || '' };
          if (state.view !== 'post') { navigate('/p/' + postId); return; }
          if (state._renderReplyBanner) state._renderReplyBanner();
          var ta = document.getElementById('newComment');
          if (ta) ta.focus();
        }
      } catch(err) { alert(tr(err.message) || err.message); }
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

// ============ Notifications polling ============
async function pollNotifications() {
  if (!state.user) return;
  try {
    var data = await api('/api/notifications');
    var newUnread = data.unread;
    var changed = (newUnread !== state.unread);
    state.unread = newUnread;
    if (changed) renderSidebar();
  } catch(e) {}
}

// ============ Init ============
(async function init() {
  await loadMe();
  if (state.user && (state.view === 'login' || state.view === 'register')) {
    history.replaceState({}, '', '/');
    state.view = 'feed'; state.viewData = {};
  }
  renderSidebar();
  renderMain();
  if (state.user) {
    pollNotifications();
    setInterval(pollNotifications, 15000);
  }
  setInterval(function(){
    if (document.hidden) return;
    if (state.view === 'feed') loadFeed();
    else if (state.view === 'post') loadPostView();
  }, 5000);
})();
"""


# ==================================================================
# РЕНДЕР СТРАНИЦЫ
# ==================================================================
def render_page(lang: str, view: str, view_data: Optional[dict] = None) -> str:
    t = TEXTS[lang]
    view_data = view_data or {}

    js = (JS
          .replace("__ICON_UP__", json.dumps(ICON_UP))
          .replace("__ICON_DOWN__", json.dumps(ICON_DOWN))
          .replace("__ICON_COMMENT__", json.dumps(ICON_COMMENT))
          .replace("__ICON_COPY__", json.dumps(ICON_COPY))
          .replace("__ICON_CHECK__", json.dumps(ICON_CHECK))
          .replace("__ICON_MOON__", json.dumps(ICON_MOON))
          .replace("__ICON_SUN__", json.dumps(ICON_SUN))
          .replace("__ICON_BACK__", json.dumps(ICON_BACK))
          .replace("__ICON_HOME__", json.dumps(ICON_HOME))
          .replace("__ICON_USER__", json.dumps(ICON_USER))
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
        '<meta name="viewport" content="width=1000" />\n'
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
        f'const TRUNCATE_LINES = {TRUNCATE_LINES};\n'
        f'const TRUNCATE_CHARS = {TRUNCATE_CHARS};\n'
        f'const T = {json.dumps(t, ensure_ascii=False)};\n'
        + js +
        '\n</script>\n'
        '</body>\n'
        '</html>'
    )


# ==================================================================
# РОУТЫ
# ==================================================================
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


@app.get("/notifications", response_class=HTMLResponse)
def page_notifications(request: Request):
    return render_page(get_lang(request), "notifications")


@app.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request):
    return render_page(get_lang(request), "settings")


@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request):
    return render_page(get_lang(request), "register")


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return render_page(get_lang(request), "login")
