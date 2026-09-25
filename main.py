# main.py
# SLD Chat server — FastAPI, всё хранится в оперативке.
# Deploy: fastapicloud / uvicorn main:app --host 0.0.0.0 --port 8000

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import time
import uuid as uuidlib

app = FastAPI(title="SLD Chat", version="1.0")

# ====================== STORAGE ======================
users = {}     # uuid -> {uuid, nick, created_at, last_seen}
messages = []  # list of dicts

# ====================== MODELS ======================
class RegisterReq(BaseModel):
    uuid: str
    nick: str

class UpdateNickReq(BaseModel):
    uuid: str
    nick: str

class SendReq(BaseModel):
    from_uuid: str
    to_uuid: str
    text: str

# ====================== UTILS ======================
def now_ms() -> int:
    return int(time.time() * 1000)

def ensure_user(uid: str, nick: Optional[str] = None) -> dict:
    u = users.get(uid)
    if u is None:
        u = {
            "uuid": uid,
            "nick": (nick or ("User-" + uid[:6])).strip() or ("User-" + uid[:6]),
            "created_at": now_ms(),
            "last_seen": now_ms(),
        }
        users[uid] = u
    return u

# ====================== ROUTES ======================
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "SLD Chat",
        "users": len(users),
        "messages": len(messages),
    }

@app.get("/health")
def health():
    return {"status": "healthy", "ts": now_ms()}

@app.post("/register")
def register(r: RegisterReq):
    n = (r.nick or "").strip()
    if not n:
        raise HTTPException(400, "empty nick")
    if len(n) > 32:
        n = n[:32]
    u = ensure_user(r.uuid, n)
    u["nick"] = n
    u["last_seen"] = now_ms()
    return u

@app.post("/update_nick")
def update_nick(r: UpdateNickReq):
    if r.uuid not in users:
        raise HTTPException(404, "user not found")
    n = (r.nick or "").strip()
    if not n:
        raise HTTPException(400, "empty nick")
    if len(n) > 32:
        n = n[:32]
    users[r.uuid]["nick"] = n
    users[r.uuid]["last_seen"] = now_ms()
    return users[r.uuid]

@app.get("/users")
def all_users():
    return list(users.values())

@app.get("/user/{uid}")
def get_user(uid: str):
    u = users.get(uid)
    if u is None:
        raise HTTPException(404, "user not found")
    return u

@app.post("/send")
def send(r: SendReq):
    if r.from_uuid not in users:
        raise HTTPException(404, "sender not found")
    txt = (r.text or "").strip()
    if not txt:
        raise HTTPException(400, "empty message")
    if len(txt) > 4000:
        txt = txt[:4000]
    if r.to_uuid not in users:
        ensure_user(r.to_uuid)
    m = {
        "id": str(uuidlib.uuid4()),
        "from_uuid": r.from_uuid,
        "to_uuid": r.to_uuid,
        "text": txt,
        "timestamp": now_ms(),
        "read": False,
    }
    messages.append(m)
    users[r.from_uuid]["last_seen"] = now_ms()
    return m

@app.get("/messages")
def get_messages(user1: str, user2: str, since: Optional[int] = 0):
    since = int(since or 0)
    out = []
    for m in messages:
        if m["timestamp"] <= since:
            continue
        if (m["from_uuid"] == user1 and m["to_uuid"] == user2) or \
           (m["from_uuid"] == user2 and m["to_uuid"] == user1):
            out.append(m)
    return out

@app.get("/chats/{uid}")
def get_chats(uid: str):
    if uid not in users:
        raise HTTPException(404, "user not found")
    last = {}
    for m in messages:
        if m["from_uuid"] == uid:
            partner = m["to_uuid"]
        elif m["to_uuid"] == uid:
            partner = m["from_uuid"]
        else:
            continue
        if partner not in last or m["timestamp"] > last[partner]["timestamp"]:
            last[partner] = m
    res = []
    for partner, m in last.items():
        pu = users.get(partner) or ensure_user(partner)
        res.append({
            "partner_uuid": partner,
            "partner_nick": pu["nick"],
            "last_message": m["text"],
            "last_timestamp": m["timestamp"],
            "last_from": m["from_uuid"],
        })
    res.sort(key=lambda x: x["last_timestamp"], reverse=True)
    return res

@app.post("/reset/{uid}")
def reset_user(uid: str):
    if uid not in users:
        raise HTTPException(404, "user not found")
    users.pop(uid, None)
    global messages
    messages = [m for m in messages if m["from_uuid"] != uid and m["to_uuid"] != uid]
    return {"ok": True}

# run: uvicorn main:app --host 0.0.0.0 --port 8000
