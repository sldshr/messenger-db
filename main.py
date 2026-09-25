# main.py
import uvicorn
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict

app = FastAPI()

# Разрешаем CORS (чтобы Godot мог делать POST запросы)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Временная база данных в памяти
users_db = {}
active_connections: Dict[str, WebSocket] = {}
chat_history = []

class LoginData(BaseModel):
    username: str
    password: str

@app.post("/login")
async def login(data: LoginData):
    if data.username not in users_db:
        users_db[data.username] = data.password
        return {"status": "ok", "message": "Зарегистрирован и вошел"}
    elif users_db[data.username] == data.password:
        return {"status": "ok", "message": "Успешный вход"}
    else:
        return {"status": "error", "message": "Неверный пароль"}

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await websocket.accept()
    active_connections[username] = websocket
    
    # Отправляем историю сообщений
    await websocket.send_json({"type": "history", "messages": chat_history})
    await broadcast_users()

    try:
        while True:
            text = await websocket.receive_text()
            
            # Игнорируем пинг-сообщения от клиента (для поддержания соединения)
            if text == "__ping__":
                continue
                
            msg = {"sender": username, "text": text}
            chat_history.append(msg)
            await broadcast_message(msg)
            
    except WebSocketDisconnect:
        if username in active_connections:
            del active_connections[username]
        await broadcast_users()

async def broadcast_message(msg):
    for ws in active_connections.values():
        try:
            await ws.send_json({"type": "chat", "message": msg})
        except Exception:
            pass

async def broadcast_users():
    user_list = list(active_connections.keys())
    for ws in active_connections.values():
        try:
            await ws.send_json({"type": "users", "users": user_list})
        except Exception:
            pass

if __name__ == "__main__":
    # Для облака: слушаем 0.0.0.0 и берем порт из переменных окружения
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
