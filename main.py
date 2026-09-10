import random
import string
import datetime
from typing import Dict, Optional
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="RAM-based IRC Chat")

# Data structure to hold active rooms in RAM
# Structure: { room_code: { "max_users": int, "connections": { websocket: nickname } } }
rooms: Dict[str, dict] = {}


class CreateRoomRequest(BaseModel):
    max_users: int = Field(..., ge=2, le=100, description="Максимальное количество людей в комнате")


class RoomStatusResponse(BaseModel):
    exists: bool
    current_users: int
    max_users: int


def generate_room_code(length: int = 6) -> str:
    """Генерация уникального 6-значного кода комнаты из букв и цифр."""
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
        if code not in rooms:
            return code


@app.post("/api/create-room")
async def create_room(req: CreateRoomRequest):
    """Создание новой чат-комнаты в ОЗУ."""
    code = generate_room_code()
    rooms[code] = {
        "max_users": req.max_users,
        "connections": {}  # websocket: nickname
    }
    return {"room_code": code, "max_users": req.max_users}


@app.get("/api/check-room/{code}")
async def check_room(code: str):
    """Проверка существования комнаты и количества участников."""
    code = code.upper().strip()
    if code not in rooms:
        return {"exists": False, "current_users": 0, "max_users": 0}

    room = rooms[code]
    return {
        "exists": True,
        "current_users": len(room["connections"]),
        "max_users": room["max_users"]
    }


@app.websocket("/ws/{code}/{nickname}")
async def websocket_endpoint(websocket: WebSocket, code: str, nickname: str):
    code = code.upper().strip()
    nickname = nickname.strip()

    # Валидация существования комнаты
    if code not in rooms:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    room = rooms[code]

    # Проверка на лимит пользователей
    if len(room["connections"]) >= room["max_users"]:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Принимаем соединение
    await websocket.accept()
    room["connections"][websocket] = nickname

    # Уведомляем остальных о новом участнике
    # ОБРАТИТЕ ВНИМАНИЕ: Старые сообщения НЕ отправляются новому пользователю!
    join_time = datetime.datetime.now().strftime("%H:%M")
    system_msg = {
        "type": "system",
        "text": f"Пользователь {nickname} вошел в чат.",
        "time": join_time,
        "online": len(room["connections"]),
        "max": room["max_users"]
    }
    await broadcast_to_room(code, system_msg)

    try:
        while True:
            # Получение текста сообщения от клиента
            text = await websocket.receive_text()
            if text.strip():
                msg_time = datetime.datetime.now().strftime("%H:%M")
                user_msg = {
                    "type": "message",
                    "sender": nickname,
                    "text": text,
                    "time": msg_time
                }
                # Рассылка сообщения ВСЕМ подключенным участникам комнаты в данный момент
                await broadcast_to_room(code, user_msg)
    except WebSocketDisconnect:
        # Удаление соединения при отключении
        if code in rooms and websocket in rooms[code]["connections"]:
            del rooms[code]["connections"][websocket]
            leave_time = datetime.datetime.now().strftime("%H:%M")

            # Если в комнате никого не осталось — удаляем ее из памяти
            if len(rooms[code]["connections"]) == 0:
                del rooms[code]
            else:
                # Оповещаем остальных об уходе пользователя
                leave_msg = {
                    "type": "system",
                    "text": f"Пользователь {nickname} вышел из чата.",
                    "time": leave_time,
                    "online": len(rooms[code]["connections"]),
                    "max": rooms[code]["max_users"]
                }
                await broadcast_to_room(code, leave_msg)


async def broadcast_to_room(code: str, message: dict):
    """Отправка JSON-сообщения всем активным клиентам комнаты."""
    if code in rooms:
        dead_connections = []
        for ws in rooms[code]["connections"].keys():
            try:
                await ws.send_json(message)
            except Exception:
                dead_connections.append(ws)

        for ws in dead_connections:
            if ws in rooms[code]["connections"]:
                del rooms[code]["connections"][ws]


@app.get("/", response_class=HTMLResponse)
async def get_index():
    html_content = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>FastAPI RAM IRC Chat</title>
        <!-- Bootstrap 3 CSS -->
        <link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.4.1/css/bootstrap.min.css">
        <!-- jQuery and Bootstrap 3 JS -->
        <script src="https://ajax.googleapis.com/ajax/libs/jquery/3.5.1/jquery.min.js"></script>
        <script src="https://maxcdn.bootstrapcdn.com/bootstrap/3.4.1/js/bootstrap.min.js"></script>
        <style>
            body {
                background-color: #f4f6f9;
                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                padding-top: 20px;
            }
            .main-container {
                max-width: 800px;
                margin: 0 auto;
            }
            .panel {
                border-radius: 8px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.08);
            }
            .panel-heading {
                border-top-left-radius: 7px;
                border-top-right-radius: 7px;
                font-weight: bold;
            }
            #chat-window {
                height: 380px;
                overflow-y: auto;
                background: #1e1e2f;
                color: #e0e0e0;
                padding: 15px;
                border-radius: 4px;
                margin-bottom: 15px;
                font-family: 'Courier New', Courier, monospace;
            }
            .msg-item {
                margin-bottom: 8px;
                word-wrap: break-word;
            }
            .msg-time {
                color: #888;
                font-size: 0.85em;
                margin-right: 5px;
            }
            .msg-system {
                color: #f39c12;
                font-style: italic;
            }
            .msg-user {
                color: #2ecc71;
                font-weight: bold;
            }
            .msg-self {
                color: #3498db;
                font-weight: bold;
            }
            .msg-text {
                color: #ffffff;
            }
            .status-badge {
                font-size: 0.9em;
                padding: 5px 10px;
            }
            .disabled-overlay {
                opacity: 0.5;
                pointer-events: none;
            }
        </style>
    </head>
    <body>
        <div class="container main-container">
            <!-- 1. НИКНЕЙМ (В самом верху, обязательный) -->
            <div class="panel panel-primary">
                <div class="panel-heading">
                    <span class="glyphicon glyphicon-user"></span> 1. Шаг: Ваш никнейм (Обязательно)
                </div>
                <div class="panel-body">
                    <div class="form-group" id="nickname-group">
                        <label for="nickname-input">Введите никнейм для входа в чат:</label>
                        <div class="input-group">
                            <input type="text" id="nickname-input" class="form-control" placeholder="Например: CyberNinja" maxlength="20">
                            <span class="input-group-btn">
                                <button class="btn btn-primary" type="button" id="btn-set-nickname">Сохранить ник</button>
                            </span>
                        </div>
                        <p class="help-block" id="nickname-hint">Без никнейма выбор комнат недоступен.</p>
                    </div>
                </div>
            </div>

            <!-- 2. ВЫБОР ДЕЙСТВИЯ: ВХОД ИЛИ СОЗДАНИЕ -->
            <div id="actions-panel" class="disabled-overlay">
                <div class="panel panel-default">
                    <div class="panel-heading">
                        <span class="glyphicon glyphicon-option-horizontal"></span> 2. Шаг: Выберите действие
                    </div>
                    <div class="panel-body">
                        <ul class="nav nav-tabs nav-justified" id="action-tabs">
                            <li class="active"><a data-toggle="tab" href="#tab-join">Войти по коду</a></li>
                            <li><a data-toggle="tab" href="#tab-create">Создать комнату</a></li>
                        </ul>

                        <div class="tab-content" style="padding-top: 20px;">
                            <!-- Вход по коду -->
                            <div id="tab-join" class="tab-pane fade in active">
                                <div class="row">
                                    <div class="col-sm-8 col-sm-offset-2">
                                        <div class="form-group">
                                            <label for="join-code-input">Код комнаты (6 символов):</label>
                                            <input type="text" id="join-code-input" class="form-control text-uppercase" placeholder="X Y Z 1 2 3" maxlength="6">
                                        </div>
                                        <button class="btn btn-success btn-block btn-lg" id="btn-join-room">
                                            <span class="glyphicon glyphicon-log-in"></span> Войти в комнату
                                        </button>
                                    </div>
                                </div>
                            </div>

                            <!-- Создание комнаты -->
                            <div id="tab-create" class="tab-pane fade">
                                <div class="row">
                                    <div class="col-sm-8 col-sm-offset-2">
                                        <div class="form-group">
                                            <label for="max-users-select">Максимум участников в комнате:</label>
                                            <select id="max-users-select" class="form-control">
                                                <option value="2">2 человека (Приватный)</option>
                                                <option value="5" selected>5 человек</option>
                                                <option value="10">10 человек</option>
                                                <option value="20">20 человек</option>
                                                <option value="50">50 человек</option>
                                            </select>
                                        </div>
                                        <button class="btn btn-primary btn-block btn-lg" id="btn-create-room">
                                            <span class="glyphicon glyphicon-plus"></span> Создать комнату
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 3. ИНТЕРФЕЙС IRC ЧАТА (Скрыт по умолчанию) -->
            <div id="chat-panel" style="display: none;">
                <div class="panel panel-dark" style="background-color: #2b2b3d; color: white;">
                    <div class="panel-heading" style="background-color: #1a1a26; color: white; border-bottom: 1px solid #333;">
                        <div class="row">
                            <div class="col-xs-6">
                                <strong>Код комнаты:</strong> <span id="display-room-code" class="label label-warning" style="font-size: 1.1em;">------</span>
                            </div>
                            <div class="col-xs-6 text-right">
                                <span class="label label-info status-badge" id="online-counter">Онлайн: 0 / 0</span>
                                <button class="btn btn-danger btn-xs" id="btn-leave-room" style="margin-left: 10px;">
                                    <span class="glyphicon glyphicon-off"></span> Выйти
                                </button>
                            </div>
                        </div>
                    </div>
                    <div class="panel-body" style="background-color: #181824;">
                        <!-- Окно чата -->
                        <div id="chat-window"></div>

                        <!-- Форма отправки сообщения -->
                        <form id="msg-form" onsubmit="return false;">
                            <div class="input-group">
                                <input type="text" id="msg-input" class="form-control" placeholder="Напишите сообщение..." autocomplete="off">
                                <span class="input-group-btn">
                                    <button class="btn btn-success" type="submit" id="btn-send-msg">
                                        <span class="glyphicon glyphicon-send"></span> Отправить
                                    </button>
                                </span>
                            </div>
                        </form>
                    </div>
                </div>
            </div>

            <!-- Уведомления и системные сообщения -->
            <div id="alert-box" class="alert alert-danger" style="display: none; margin-top: 15px;"></div>
        </div>

        <!-- JS Логика приложения -->
        <script>
            let currentNickname = "";
            let currentRoomCode = "";
            let socket = null;

            // Вспомогательная функция для всплывающих уведомлений
            function showAlert(msg) {
                const box = $('#alert-box');
                box.text(msg).fadeIn();
                setTimeout(() => box.fadeOut(), 4000);
            }

            // 1. Установка и сохранение никнейма
            $('#btn-set-nickname').click(function() {
                const val = $('#nickname-input').val().trim();
                if (!val) {
                    showAlert("Пожалуйста, введите корректный никнейм!");
                    return;
                }
                currentNickname = val;
                $('#nickname-input').prop('disabled', true);
                $('#btn-set-nickname').prop('disabled', true).addClass('btn-success').removeClass('btn-primary').html('<span class="glyphicon glyphicon-check"></span> Ник сохранен');
                $('#nickname-hint').html('<b class="text-success">Никнейм установлен: ' + currentNickname + '</b>');
                
                // Разблокируем панель выбора комнат
                $('#actions-panel').removeClass('disabled-overlay');
            });

            // Нажатие Enter в поле никнейма
            $('#nickname-input').keypress(function(e) {
                if (e.which === 13) $('#btn-set-nickname').click();
            });

            // 2. Создание комнаты
            $('#btn-create-room').click(async function() {
                if (!currentNickname) {
                    showAlert("Сначала введите никнейм вверху страницы!");
                    return;
                }

                const maxUsers = parseInt($('#max-users-select').val());

                try {
                    const response = await fetch('/api/create-room', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ max_users: maxUsers })
                    });
                    const data = await response.json();
                    
                    if (data.room_code) {
                        joinWebSocketRoom(data.room_code);
                    }
                } catch (err) {
                    showAlert("Ошибка при создании комнаты. Попробуйте еще раз.");
                }
            });

            // 3. Вход в комнату по коду
            $('#btn-join-room').click(async function() {
                if (!currentNickname) {
                    showAlert("Сначала введите никнейм вверху страницы!");
                    return;
                }

                const code = $('#join-code-input').val().trim().toUpperCase();
                if (code.length !== 6) {
                    showAlert("Код комнаты должен состоять ровно из 6 символов!");
                    return;
                }

                try {
                    // Проверяем существование и наполненность комнаты перед подключением
                    const res = await fetch('/api/check-room/' + code);
                    const info = await res.json();

                    if (!info.exists) {
                        showAlert("Комната с таким кодом не найдена!");
                        return;
                    }

                    if (info.current_users >= info.max_users) {
                        showAlert("Комната переполнена (" + info.current_users + "/" + info.max_users + ")!");
                        return;
                    }

                    joinWebSocketRoom(code);
                } catch (err) {
                    showAlert("Ошибка при проверке комнаты.");
                }
            });

            // 4. Подключение к WebSocket чата
            function joinWebSocketRoom(code) {
                currentRoomCode = code;
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = `${protocol}//${window.location.host}/ws/${code}/${encodeURIComponent(currentNickname)}`;

                socket = new WebSocket(wsUrl);

                socket.onopen = function() {
                    // Скрываем панели настройки и показываем интерфейс чата
                    $('#actions-panel').hide();
                    $('.panel-primary').hide(); // Скрываем поле никнейма во время чата
                    $('#chat-panel').show();
                    $('#display-room-code').text(code);
                    $('#chat-window').empty(); // Очищаем старые записи
                    
                    appendSystemMessage("Вы успешно подключились к комнате " + code + ". История предыдущих сообщений недоступна.");
                };

                socket.onmessage = function(event) {
                    const data = JSON.parse(event.data);

                    if (data.type === "system") {
                        appendSystemMessage(`[${data.time}] ${data.text}`);
                        if (data.online !== undefined) {
                            $('#online-counter').text(`Онлайн: ${data.online} / ${data.max}`);
                        }
                    } else if (data.type === "message") {
                        appendUserMessage(data.time, data.sender, data.text);
                    }
                };

                socket.onclose = function(event) {
                    showAlert("Соединение с чатом закрыто.");
                    leaveRoomUI();
                };

                socket.onerror = function() {
                    showAlert("Ошибка соединения WebSocket.");
                    leaveRoomUI();
                };
            }

            // Отправка сообщений
            $('#msg-form').submit(function() {
                const text = $('#msg-input').val().trim();
                if (text && socket && socket.readyState === WebSocket.OPEN) {
                    socket.send(text);
                    $('#msg-input').val('');
                }
                return false;
            });

            // Выход из комнаты
            $('#btn-leave-room').click(function() {
                if (socket) {
                    socket.close();
                }
                leaveRoomUI();
            });

            function leaveRoomUI() {
                $('#chat-panel').hide();
                $('.panel-primary').show();
                $('#actions-panel').show();
                currentRoomCode = "";
            }

            function appendSystemMessage(text) {
                const win = $('#chat-window');
                win.append(`<div class="msg-item msg-system"><span class="glyphicon glyphicon-info-sign"></span> ${escapeHtml(text)}</div>`);
                win.scrollTop(win[0].scrollHeight);
            }

            function appendUserMessage(time, sender, text) {
                const win = $('#chat-window');
                const isSelf = sender === currentNickname;
                const senderClass = isSelf ? "msg-self" : "msg-user";
                
                win.append(`
                    <div class="msg-item">
                        <span class="msg-time">[${time}]</span>
                        <span class="${senderClass}">&lt;${escapeHtml(sender)}&gt;:</span>
                        <span class="msg-text">${escapeHtml(text)}</span>
                    </div>
                `);
                win.scrollTop(win[0].scrollHeight);
            }

            function escapeHtml(string) {
                return String(string).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    print("Запуск FastAPI IRC сервера...")
    print("Откройте в браузере: http://127.0.0.1:8000")
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
