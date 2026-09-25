import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List

import uvicorn
from fastapi import (
    FastAPI, HTTPException, Header, Query,
    WebSocket, WebSocketDisconnect,
)
from pydantic import BaseModel


# ───────────────────────────────────────────────── in-memory store ──
@dataclass
class User:
    username: str
    password_hash: str
    salt: str


@dataclass
class Message:
    sender: str
    recipient: str
    text: str
    timestamp: float


@dataclass
class Store:
    users: Dict[str, User] = field(default_factory=dict)
    sessions: Dict[str, str] = field(default_factory=dict)
    messages: Dict[str, List[Message]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @staticmethod
    def _key(a: str, b: str) -> str:
        return "|".join(sorted((a, b)))

    def add_message(self, msg: Message) -> None:
        self.messages.setdefault(self._key(msg.sender, msg.recipient), []).append(msg)

    def get_conversation(self, a: str, b: str) -> List[Message]:
        return list(self.messages.get(self._key(a, b), []))


store = Store()


def hash_pwd(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 100_000
    ).hex()


# ───────────────────────────────────────── WebSocket manager ──
class ConnectionManager:
    def __init__(self) -> None:
        self.active: Dict[str, WebSocket] = {}

    async def connect(self, user: str, ws: WebSocket) -> None:
        await ws.accept()
        old = self.active.get(user)
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        self.active[user] = ws
        await self.broadcast_user_list()

    async def disconnect(self, user: str) -> None:
        if user in self.active:
            del self.active[user]
            await self.broadcast_user_list()

    async def send_to(self, user: str, data: dict) -> None:
        ws = self.active.get(user)
        if ws is None:
            return
        try:
            await ws.send_json(data)
        except Exception:
            self.active.pop(user, None)

    async def broadcast(self, data: dict) -> None:
        for u, ws in list(self.active.items()):
            try:
                await ws.send_json(data)
            except Exception:
                self.active.pop(u, None)

    async def broadcast_user_list(self) -> None:
        await self.broadcast({
            "type": "user_list",
            "users": sorted(store.users.keys()),
            "online": sorted(self.active.keys()),
        })


manager = ConnectionManager()
app = FastAPI(title="Terminal Messenger (in-memory)", version="1.0")


# ────────────────────────────────────────────────── schemas ──
class AuthReq(BaseModel):
    username: str
    password: str


class MessageReq(BaseModel):
    to: str
    text: str


# ───────────────────────────────────────────────────── auth ──
@app.post("/api/register")
async def register(req: AuthReq):
    if len(req.username) < 2 or len(req.password) < 4:
        raise HTTPException(400, "Username or password too short")
    if not req.username.replace("_", "").isalnum():
        raise HTTPException(400, "Username may only contain letters, digits, _")

    async with store.lock:
        if req.username in store.users:
            raise HTTPException(400, "Username already taken")
        salt = secrets.token_hex(16)
        store.users[req.username] = User(
            username=req.username,
            password_hash=hash_pwd(req.password, salt),
            salt=salt,
        )
    return {"ok": True}


@app.post("/api/login")
async def login(req: AuthReq):
    async with store.lock:
        user = store.users.get(req.username)
        if not user or hash_pwd(req.password, user.salt) != user.password_hash:
            raise HTTPException(401, "Invalid credentials")
        token = secrets.token_urlsafe(32)
        store.sessions[token] = req.username
    return {"token": token, "username": req.username}


def auth_user(token: str) -> str:
    username = store.sessions.get(token)
    if not username:
        raise HTTPException(401, "Invalid token")
    return username


# ──────────────────────────────────────────── endpoints ──
@app.get("/api/users")
def list_users(authorization: str = Header(...)):
    me = auth_user(authorization)
    users = sorted(u for u in store.users.keys() if u != me)
    return {"users": users, "online": sorted(manager.active.keys())}


@app.get("/api/messages/{peer}")
def get_messages(peer: str, authorization: str = Header(...)):
    me = auth_user(authorization)
    if peer not in store.users:
        raise HTTPException(404, "User not found")
    msgs = store.get_conversation(me, peer)
    return {
        "messages": [
            {"sender": m.sender, "recipient": m.recipient,
             "text": m.text, "timestamp": m.timestamp}
            for m in msgs
        ]
    }


@app.post("/api/messages")
async def post_message(req: MessageReq, authorization: str = Header(...)):
    me = auth_user(authorization)
    if req.to not in store.users:
        raise HTTPException(404, "Recipient not found")
    if not req.text.strip():
        raise HTTPException(400, "Empty message")

    msg = Message(sender=me, recipient=req.to, text=req.text, timestamp=time.time())
    async with store.lock:
        store.add_message(msg)

    payload = {"sender": msg.sender, "recipient": msg.recipient,
               "text": msg.text, "timestamp": msg.timestamp}
    await manager.send_to(req.to, {"type": "message", "message": payload})
    return {"ok": True, "message": payload}


# ──────────────────────────────────────────── WebSocket ──
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = Query(...)):
    username = store.sessions.get(token)
    if not username:
        await websocket.close(code=1008)
        return

    await manager.connect(username, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(username)


@app.get("/api/stats")
def stats():
    return {
        "users": len(store.users),
        "sessions": len(store.sessions),
        "conversations": len(store.messages),
        "online": list(manager.active.keys()),
        "total_messages": sum(len(v) for v in store.messages.values()),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
