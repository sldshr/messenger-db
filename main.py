import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI()

# Класс для управления активными подключениями пользователей
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        # Рассылаем сообщение абсолютно всем подключенным пользователям
        for connection in self.active_connections:
            await connection.send_text(message)

manager = ConnectionManager()

# Главная страница (просто отдает HTML-интерфейс чата)
@app.get("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

# Эндпоинт для WebSocket-соединения мессенджера
@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Ждем сообщение от конкретного пользователя
            data = await websocket.receive_text()
            # Отправляем полученное сообщение всем участникам чата
            await manager.broadcast(data)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        await manager.broadcast("Один из пользователей покинул чат.")

# Этот блок важен для хостинга: он автоматически берет нужный порт из системы
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
