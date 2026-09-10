import asyncio
import json
from collections import deque
from typing import List, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI(title="In-Memory Fast Streamer")

# Хранение подключенных зрителей
viewers: Set[WebSocket] = set()

# Хранение последних 50 сообщений чата в памяти
chat_history = deque(maxlen=50)

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ru" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>In-Memory Streamer</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {
            background-color: #121212;
            color: #e0e0e0;
            display: flex;
            flex-direction: column;
            height: 100vh;
            margin: 0;
            overflow: hidden;
        }
        .main-container {
            flex-grow: 1;
            display: flex;
            padding: 15px;
            gap: 15px;
            height: 100%;
        }
        .stream-container {
            flex: 3;
            background-color: #000;
            border-radius: 8px;
            position: relative;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            box-shadow: 0 4px 6px rgba(0,0,0,0.5);
        }
        #streamImage {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
        }
        .live-indicator {
            position: absolute;
            top: 10px;
            left: 10px;
            background-color: rgba(255, 0, 0, 0.8);
            color: white;
            padding: 5px 10px;
            border-radius: 4px;
            font-weight: bold;
            font-family: monospace;
            z-index: 10;
        }
        .chat-container {
            flex: 1;
            background-color: #1e1e1e;
            border-radius: 8px;
            display: flex;
            flex-direction: column;
            border: 1px solid #333;
        }
        .chat-header {
            padding: 10px;
            background-color: #2c2c2c;
            border-bottom: 1px solid #444;
            font-weight: bold;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
        }
        .chat-messages {
            flex-grow: 1;
            overflow-y: auto;
            padding: 10px;
            font-family: monospace;
            font-size: 0.9em;
            display: flex;
            flex-direction: column;
            gap: 5px;
        }
        .chat-message {
            word-wrap: break-word;
        }
        .chat-input-area {
            padding: 10px;
            background-color: #2c2c2c;
            border-top: 1px solid #444;
            border-bottom-left-radius: 8px;
            border-bottom-right-radius: 8px;
        }
        @media (max-width: 768px) {
            .main-container {
                flex-direction: column;
            }
            .stream-container {
                flex: none;
                height: 50vh;
            }
            .chat-container {
                flex: 1;
            }
        }
    </style>
</head>
<body>

    <div class="main-container container-fluid">
        <!-- Левая часть: Стрим -->
        <div class="stream-container">
            <div class="live-indicator">LIVE 50 FPS</div>
            <img id="streamImage" src="" alt="Ожидание трансляции...">
        </div>

        <!-- Правая часть: Чат -->
        <div class="chat-container">
            <div class="chat-header">IRC Чат</div>
            <div class="chat-messages" id="chatMessages">
                <!-- Сообщения будут добавляться сюда -->
            </div>
            <div class="chat-input-area">
                <form id="chatForm" class="d-flex gap-2">
                    <input type="text" id="chatInput" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Сообщение..." autocomplete="off">
                    <button type="submit" class="btn btn-primary btn-sm">Отправить</button>
                </form>
            </div>
        </div>
    </div>

    <!-- Скрипт логики клиента -->
    <script>
        const streamImage = document.getElementById('streamImage');
        const chatMessages = document.getElementById('chatMessages');
        const chatForm = document.getElementById('chatForm');
        const chatInput = document.getElementById('chatInput');
        
        let currentObjectURL = null;

        // Определение URL для WebSocket
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws_viewer`;
        
        const ws = new WebSocket(wsUrl);
        ws.binaryType = 'blob'; // Важно для приема бинарных данных

        ws.onopen = () => {
            console.log("WebSocket подключен");
            appendSystemMessage("Подключено к серверу.");
        };

        ws.onclose = () => {
            console.log("WebSocket отключен");
            appendSystemMessage("Отключено от сервера. Попытка переподключения...");
            // В реальном приложении здесь должна быть логика реконнекта
        };

        ws.onerror = (error) => {
            console.error("WebSocket ошибка:", error);
            appendSystemMessage("Ошибка соединения.");
        };

        ws.onmessage = (event) => {
            // Обработка бинарных данных (кадры трансляции)
            if (event.data instanceof Blob) {
                if (currentObjectURL) {
                    URL.revokeObjectURL(currentObjectURL); // Освобождаем память от старого кадра
                }
                currentObjectURL = URL.createObjectURL(event.data);
                streamImage.src = currentObjectURL;
            } 
            // Обработка текстовых данных (сообщения чата)
            else if (typeof event.data === 'string') {
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === 'chat') {
                        appendChatMessage(data.message);
                    } else if (data.type === 'history') {
                        data.messages.forEach(msg => appendChatMessage(msg));
                    }
                } catch (e) {
                    console.error("Ошибка разбора сообщения:", e);
                }
            }
        };

        // Обработка отправки сообщения в чат
        chatForm.addEventListener('submit', (e) => {
            e.preventDefault();
            const message = chatInput.value.trim();
            if (message && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'chat', message: message }));
                chatInput.value = '';
            }
        });

        // Функция добавления сообщения пользователя в чат
        function appendChatMessage(msg) {
            const div = document.createElement('div');
            div.className = 'chat-message';
            // Простая защита от XSS
            div.textContent = `> ${msg}`; 
            chatMessages.appendChild(div);
            scrollToBottom();
        }

        // Функция добавления системного сообщения в чат
        function appendSystemMessage(msg) {
            const div = document.createElement('div');
            div.className = 'chat-message text-muted';
            div.textContent = `*** ${msg}`;
            chatMessages.appendChild(div);
            scrollToBottom();
        }

        // Прокрутка чата вниз
        function scrollToBottom() {
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def get_index():
    """Отдает главную страницу с плеером и чатом."""
    return HTML_CONTENT

@app.websocket("/stream_input")
async def websocket_stream_input(websocket: WebSocket):
    """
    Эндпоинт для стримера. Принимает бинарные кадры (JPEG) и рассылает их зрителям.
    """
    await websocket.accept()
    print("Стример подключен.")
    try:
        while True:
            # Ожидаем бинарные данные (сырые байты кадра)
            # Используем receive_bytes для минимизации накладных расходов
            data = await websocket.receive_bytes()
            
            # Асинхронно рассылаем кадр всем подключенным зрителям
            # Собираем задачи рассылки
            if viewers:
                send_tasks = []
                # Копируем сет viewers, чтобы избежать ошибки изменения размера во время итерации
                disconnected_viewers = set()
                for viewer_ws in viewers.copy():
                    try:
                         # Отправляем бинарные данные
                         send_tasks.append(viewer_ws.send_bytes(data))
                    except Exception:
                         # Если не удалось отправить (например, зритель отключился), помечаем на удаление
                         disconnected_viewers.add(viewer_ws)
                
                # Выполняем рассылку конкурентно
                if send_tasks:
                     await asyncio.gather(*send_tasks, return_exceptions=True)
                
                # Удаляем отключившихся зрителей
                if disconnected_viewers:
                     viewers.difference_update(disconnected_viewers)

    except WebSocketDisconnect:
        print("Стример отключился.")
    except Exception as e:
         print(f"Ошибка стримера: {e}")

@app.websocket("/ws_viewer")
async def websocket_viewer(websocket: WebSocket):
    """
    Эндпоинт для зрителей. Отправляет историю чата при подключении.
    Принимает текстовые сообщения от зрителя и рассылает их всем.
    Бинарные данные (видео) отправляются этому сокету из /stream_input.
    """
    await websocket.accept()
    viewers.add(websocket)
    print(f"Зритель подключен. Всего зрителей: {len(viewers)}")
    
    try:
        # При подключении отправляем историю чата
        if chat_history:
            history_msg = json.dumps({
                "type": "history",
                "messages": list(chat_history)
            })
            await websocket.send_text(history_msg)

        while True:
            # Ожидаем текстовые сообщения от зрителя (для чата)
            text_data = await websocket.receive_text()
            
            try:
                data = json.loads(text_data)
                if data.get("type") == "chat":
                    msg = data.get("message")
                    if msg:
                        # Ограничиваем длину сообщения для безопасности и экономии памяти
                        sanitized_msg = str(msg)[:200]
                        
                        # Сохраняем в историю
                        chat_history.append(sanitized_msg)
                        
                        # Формируем JSON для рассылки
                        broadcast_msg = json.dumps({
                            "type": "chat",
                            "message": sanitized_msg
                        })
                        
                        # Рассылаем всем зрителям
                        send_tasks = []
                        disconnected_viewers = set()
                        for viewer_ws in viewers.copy():
                            try:
                                send_tasks.append(viewer_ws.send_text(broadcast_msg))
                            except Exception:
                                disconnected_viewers.add(viewer_ws)
                        
                        if send_tasks:
                             await asyncio.gather(*send_tasks, return_exceptions=True)
                        
                        if disconnected_viewers:
                             viewers.difference_update(disconnected_viewers)

            except json.JSONDecodeError:
                # Игнорируем невалидный JSON
                pass

    except WebSocketDisconnect:
        viewers.remove(websocket)
        print(f"Зритель отключился. Осталось зрителей: {len(viewers)}")
    except Exception as e:
        if websocket in viewers:
            viewers.remove(websocket)
        print(f"Ошибка зрителя: {e}")

if __name__ == "__main__":
    import uvicorn
    # Запуск сервера с ограничением по worker'ам для экономии памяти
    # workers=1 достаточно для небольших нагрузок и строгого лимита ОЗУ
    uvicorn.run("streamer:app", host="0.0.0.0", port=8000, workers=1, log_level="warning")
