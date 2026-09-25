# main.py
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, List

app = FastAPI()

# Разрешаем CORS (важно для запросов из Godot)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Временная база данных в памяти (для теста)
users_db = {} # {"username": "password"}
active_connections: Dict[str, WebSocket] = {}
chat_history = []

class LoginData(BaseModel):
    username: str
    password: str

@app.post("/login")
async def login(data: LoginData):
    # Простая логика: если нет - регистрируем, если есть - проверяем пароль
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
    
    # Отправляем историю сообщений новому пользователю
    await websocket.send_json({"type": "history", "messages": chat_history})
    
    # Оповещаем всех об обновлении списка пользователей
    await broadcast_users()

    try:
        while True:
            text = await websocket.receive_text()
            msg = {"sender": username, "text": text}
            chat_history.append(msg)
            await broadcast_message(msg)
    except WebSocketDisconnect:
        del active_connections[username]
        await broadcast_users()

async def broadcast_message(msg):
    for ws in active_connections.values():
        await ws.send_json({"type": "chat", "message": msg})

async def broadcast_users():
    user_list = list(active_connections.keys())
    for ws in active_connections.values():
        await ws.send_json({"type": "users", "users": user_list})

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
