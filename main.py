# main.py — SLD Posts server
# Deploy: fastapicloud / uvicorn main:app --host 0.0.0.0 --port 8000

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import time, random

app = FastAPI(title="SLD Posts", version="1.0")

users = {}   # uuid -> {uuid, nick}
posts = {}   # id (6-цифр) -> {id, author_uuid, author_nick, text, tags, photos, timestamp}

class RegisterReq(BaseModel):
    uuid: str
    nick: str

class UpdateNickReq(BaseModel):
    uuid: str
    nick: str

class CreatePostReq(BaseModel):
    author_uuid: str
    text: str = ""
    tags: List[str] = []
    photos: List[str] = []   # base64 JPEG

class UpdatePostReq(BaseModel):
    author_uuid: str
    text: Optional[str] = None
    tags: Optional[List[str]] = None
    photos: Optional[List[str]] = None

def now_ms() -> int:
    return int(time.time() * 1000)

def gen_id() -> str:
    for _ in range(500):
        s = f"{random.randint(100000, 999999):06d}"
        if s not in posts:
            return s
    raise HTTPException(500, "id generation failed")

def ensure_user(uid, nick=None):
    u = users.get(uid)
    if u is None:
        u = {"uuid": uid, "nick": nick or ("User-" + uid[:6])}
        users[uid] = u
    return u

def normalize_tags(tags: List[str]) -> List[str]:
    out = []
    for t in tags or []:
        t = (t or "").strip()
        if not t:
            continue
        if not t.startswith("#"):
            t = "#" + t
        if t not in out:
            out.append(t)
    return out[:20]

@app.get("/")
def root():
    return {"ok": True, "service": "SLD Posts", "users": len(users), "posts": len(posts)}

@app.get("/health")
def health():
    return {"status": "healthy", "ts": now_ms()}

@app.post("/register")
def register(r: RegisterReq):
    n = (r.nick or "").strip()
    if not n: raise HTTPException(400, "empty nick")
    if len(n) > 32: n = n[:32]
    u = ensure_user(r.uuid, n)
    u["nick"] = n
    return u

@app.post("/update_nick")
def update_nick(r: UpdateNickReq):
    if r.uuid not in users: raise HTTPException(404, "user not found")
    n = (r.nick or "").strip()
    if not n: raise HTTPException(400, "empty nick")
    if len(n) > 32: n = n[:32]
    users[r.uuid]["nick"] = n
    # обновим ник автора во всех его постах
    for p in posts.values():
        if p["author_uuid"] == r.uuid:
            p["author_nick"] = n
    return users[r.uuid]

@app.post("/post")
def create_post(r: CreatePostReq):
    if r.author_uuid not in users: raise HTTPException(404, "author not found")
    text = (r.text or "")[:10000]
    tags = normalize_tags(r.tags)
    photos = (r.photos or [])[:5]
    if not text.strip() and not photos:
        raise HTTPException(400, "empty post")
    pid = gen_id()
    p = {
        "id": pid,
        "author_uuid": r.author_uuid,
        "author_nick": users[r.author_uuid]["nick"],
        "text": text,
        "tags": tags,
        "photos": photos,
        "timestamp": now_ms(),
    }
    posts[pid] = p
    return p

@app.put("/post/{pid}")
def update_post(pid: str, r: UpdatePostReq):
    p = posts.get(pid)
    if p is None: raise HTTPException(404, "post not found")
    if p["author_uuid"] != r.author_uuid:
        raise HTTPException(403, "not owner")
    if r.text is not None:
        p["text"] = (r.text or "")[:10000]
    if r.tags is not None:
        p["tags"] = normalize_tags(r.tags)
    if r.photos is not None:
        p["photos"] = (r.photos or [])[:5]
    p["author_nick"] = users.get(r.author_uuid, {}).get("nick", p.get("author_nick", ""))
    p["timestamp"] = now_ms()
    return p

@app.delete("/post/{pid}")
def delete_post(pid: str, author_uuid: str):
    p = posts.get(pid)
    if p is None: raise HTTPException(404, "post not found")
    if p["author_uuid"] != author_uuid:
        raise HTTPException(403, "not owner")
    posts.pop(pid, None)
    return {"ok": True}

@app.get("/post/{pid}")
def get_post(pid: str):
    p = posts.get(pid)
    if p is None: raise HTTPException(404, "post not found")
    return p

@app.get("/my_posts/{uid}")
def my_posts(uid: str):
    if uid not in users: raise HTTPException(404, "user not found")
    lst = [p for p in posts.values() if p["author_uuid"] == uid]
    lst.sort(key=lambda x: x["timestamp"], reverse=True)
    return lst

@app.get("/feed")
def feed(limit: int = 30, offset: int = 0):
    lst = list(posts.values())
    lst.sort(key=lambda x: x["timestamp"], reverse=True)
    return lst[offset:offset + limit]

@app.post("/reset/{uid}")
def reset(uid: str):
    if uid not in users: raise HTTPException(404, "user not found")
    users.pop(uid, None)
    for k in [k for k, v in posts.items() if v["author_uuid"] == uid]:
        posts.pop(k, None)
    return {"ok": True}
