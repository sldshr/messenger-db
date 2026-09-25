"""
TUI Messenger — сервер на FastAPI + uvicorn.
Все данные хранятся в оперативной памяти.

Запуск без TLS (для отладки):
    python server.py --port 8000

Запуск с TLS (HTTPS):
    python server.py --port 8443 --ssl-cert cert.pem --ssl-key key.pem

Self-signed сертификат (для локальных тестов):
    openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
        -days 365 -subj "/CN=localhost"
"""

from __future__ import annotations

import argparse
import secrets
from datetime import datetime
from typing import Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="TUI Messenger Server", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------- #
#  In-memory storage
# --------------------------------------------------------------------------- #

users: Dict[str, str] = {}          # username -> password
tokens: Dict[str, str] = {}         # token -> username
chats: Dict[int, dict] = {}         # chat_id -> {kind,name,members,messages}
_next_chat_id: int = 1


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# --------------------------------------------------------------------------- #
#  Pydantic-модели
# --------------------------------------------------------------------------- #

class AuthRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=4, max_length=128)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class DirectChatRequest(BaseModel):
    target: str


class GroupChatRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    members: List[str]


# --------------------------------------------------------------------------- #
#  Auth dependency
# --------------------------------------------------------------------------- #

def current_user(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Отсутствует Authorization-заголовок")
    token = authorization[7:].strip()
    username = tokens.get(token)
    if not username:
        raise HTTPException(401, "Невалидный или истёкший токен")
    return username


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #

@app.get("/")
def root():
    return {"service": "TUI Messenger", "status": "ok", "users": len(users), "chats": len(chats)}


@app.post("/api/register")
def register(req: AuthRequest):
    if req.username in users:
        raise HTTPException(400, "Такой пользователь уже существует")
    if " " in req.username:
        raise HTTPException(400, "Имя не должно содержать пробелов")
    users[req.username] = req.password
    return {"message": "Регистрация успешна"}


@app.post("/api/login")
def login(req: AuthRequest):
    if req.username not in users:
        raise HTTPException(401, "Пользователь не найден")
    if users[req.username] != req.password:
        raise HTTPException(401, "Неверный пароль")
    token = secrets.token_urlsafe(32)
    tokens[token] = req.username
    return {"token": token, "username": req.username}


@app.post("/api/logout")
def logout(user: str = Depends(current_user),
           authorization: Optional[str] = Header(None)):
    if authorization:
        tokens.pop(authorization[7:].strip(), None)
    return {"message": "OK"}


@app.get("/api/users")
def list_users(user: str = Depends(current_user)):
    return {"users": sorted(u for u in users if u != user)}


@app.get("/api/chats")
def list_chats(user: str = Depends(current_user)):
    result = []
    for cid, chat in chats.items():
        if user not in chat["members"]:
            continue
        if chat["kind"] == "direct":
            others = [m for m in chat["members"] if m != user]
            display = others[0] if others else "?"
        else:
            display = chat["name"]
        last = chat["messages"][-1] if chat["messages"] else None
        result.append({
            "id": cid,
            "kind": chat["kind"],
            "display": display,
            "members": chat["members"],
            "last_message": last,
            "message_count": len(chat["messages"]),
        })
    result.sort(key=lambda c: c["id"])
    return {"chats": result}


@app.post("/api/chats/direct")
def create_direct(req: DirectChatRequest, user: str = Depends(current_user)):
    global _next_chat_id
    target = req.target.strip()
    if target == user:
        raise HTTPException(400, "Нельзя создать чат с самим собой")
    if target not in users:
        raise HTTPException(404, f"Пользователь '{target}' не найден")
    for cid, chat in chats.items():
        if chat["kind"] == "direct" and set(chat["members"]) == {user, target}:
            return {"id": cid}
    cid = _next_chat_id
    _next_chat_id += 1
    chats[cid] = {"kind": "direct", "name": "", "members": [user, target], "messages": []}
    return {"id": cid}


@app.post("/api/chats/group")
def create_group(req: GroupChatRequest, user: str = Depends(current_user)):
    global _next_chat_id
    members = [m.strip() for m in req.members if m.strip()]
    if not members:
        raise HTTPException(400, "Нужен хотя бы один участник")
    unknown = [m for m in members if m not in users]
    if unknown:
        raise HTTPException(404, f"Не найдены: {', '.join(unknown)}")
    all_members = [user] + [m for m in members if m != user]
    cid = _next_chat_id
    _next_chat_id += 1
    chats[cid] = {"kind": "group", "name": req.name, "members": all_members, "messages": []}
    return {"id": cid}


def _check_access(chat_id: int, user: str) -> dict:
    chat = chats.get(chat_id)
    if not chat:
        raise HTTPException(404, "Чат не найден")
    if user not in chat["members"]:
        raise HTTPException(403, "Нет доступа к чату")
    return chat


@app.get("/api/chats/{chat_id}/messages")
def get_messages(chat_id: int, user: str = Depends(current_user)):
    chat = _check_access(chat_id, user)
    return {"messages": chat["messages"]}


@app.post("/api/chats/{chat_id}/messages")
def send_message(chat_id: int, req: MessageRequest, user: str = Depends(current_user)):
    chat = _check_access(chat_id, user)
    msg = {"sender": user, "text": req.text, "time": _now()}
    chat["messages"].append(msg)
    return {"message": msg}


# --------------------------------------------------------------------------- #
#  Точка входа
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TUI Messenger Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ssl-cert", default=None, help="Путь к сертификату (PEM)")
    parser.add_argument("--ssl-key", default=None, help="Путь к приватному ключу (PEM)")
    args = parser.parse_args()

    kwargs = {"host": args.host, "port": args.port}
    if args.ssl_cert and args.ssl_key:
        kwargs["ssl_certfile"] = args.ssl_cert
        kwargs["ssl_keyfile"] = args.ssl_key
        print(f"[+] HTTPS: https://{args.host}:{args.port}")
    else:
        print(f"[!] HTTP (без TLS): http://{args.host}:{args.port}")
        print("    Для HTTPS передайте --ssl-cert и --ssl-key")

    uvicorn.run(app, **kwargs)
