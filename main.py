import random
import string
import datetime
import uuid
import json
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
    
    # Генерируем уникальный ID клиента для маршрутизации WebRTC P2P сообщений
    client_id = str(uuid.uuid4())
    room["connections"][websocket] = {"nickname": nickname, "id": client_id}

    # Отправляем клиенту его ID
    await websocket.send_json({"type": "welcome", "client_id": client_id})

    # Уведомляем остальных о новом участнике
    join_time = datetime.datetime.now().strftime("%H:%M")
    system_msg = {
        "type": "system",
        "text": f"joined",
        "nickname": nickname,
        "time": join_time,
        "online": len(room["connections"]),
        "max": room["max_users"]
    }
    await broadcast_to_room(code, system_msg)

    users_list = [info["nickname"] for info in room["connections"].values()]
    await broadcast_to_room(code, {"type": "users_update", "users": users_list})

    try:
        while True:
            text = await websocket.receive_text()
            try:
                data = json.loads(text)
                msg_type = data.get("type")
                
                if msg_type == "message":
                    if data.get("text", "").strip():
                        msg_time = datetime.datetime.now().strftime("%H:%M")
                        user_msg = {
                            "type": "message",
                            "sender": nickname,
                            "text": data.get("text"),
                            "time": msg_time
                        }
                        await broadcast_to_room(code, user_msg)
                        
                elif msg_type in ["webrtc_start", "webrtc_stop"]:
                    # Уведомляем всех в комнате о начале/конце стрима (кроме отправителя)
                    await broadcast_to_room(code, data, exclude_ws=websocket)
                    
                elif msg_type in ["webrtc_request", "webrtc_offer", "webrtc_answer", "webrtc_ice"]:
                    # P2P Маршрутизация сигналов WebRTC конкретному участнику по его ID
                    target_id = data.get("target_id")
                    if target_id:
                        await send_to_client(code, target_id, data)
                        
            except json.JSONDecodeError:
                pass # Игнорируем не-JSON сообщения
                
    except WebSocketDisconnect:
        # Удаление соединения при отключении
        if code in rooms and websocket in rooms[code]["connections"]:
            user_info = rooms[code]["connections"][websocket]
            del rooms[code]["connections"][websocket]
            leave_time = datetime.datetime.now().strftime("%H:%M")

            # Если в комнате никого не осталось — удаляем ее из памяти
            if len(rooms[code]["connections"]) == 0:
                del rooms[code]
            else:
                # Оповещаем остальных об уходе пользователя
                leave_msg = {
                    "type": "system",
                    "text": f"left",
                    "nickname": nickname,
                    "time": leave_time,
                    "online": len(rooms[code]["connections"]),
                    "max": rooms[code]["max_users"]
                }
                await broadcast_to_room(code, leave_msg)
                
                users_list = [info["nickname"] for info in rooms[code]["connections"].values()]
                await broadcast_to_room(code, {"type": "users_update", "users": users_list})
                
                # Сообщаем, что этот пир отключился, чтобы закрыть P2P
                await broadcast_to_room(code, {"type": "webrtc_stop", "sender_id": user_info["id"]})


async def broadcast_to_room(code: str, message: dict, exclude_ws: WebSocket = None):
    """Отправка JSON-сообщения всем активным клиентам комнаты."""
    if code in rooms:
        dead_connections = []
        for ws in rooms[code]["connections"].keys():
            if ws == exclude_ws:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead_connections.append(ws)

        for ws in dead_connections:
            if ws in rooms[code]["connections"]:
                del rooms[code]["connections"][ws]

async def send_to_client(code: str, target_id: str, message: dict):
    """Отправка сообщения конкретному клиенту по ID."""
    if code in rooms:
        for ws, info in list(rooms[code]["connections"].items()):
            if info["id"] == target_id:
                try:
                    await ws.send_json(message)
                except Exception:
                    pass
                break


@app.get("/", response_class=HTMLResponse)
async def get_index():
    html_content = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title data-i18n="appTitle">FastAPI RAM IRC Chat</title>
        <!-- Bootstrap 3 CSS -->
        <link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.4.1/css/bootstrap.min.css">
        <!-- Font Awesome для иконок -->
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css">
        <!-- jQuery and Bootstrap 3 JS -->
        <script src="https://ajax.googleapis.com/ajax/libs/jquery/3.5.1/jquery.min.js"></script>
        <script src="https://maxcdn.bootstrapcdn.com/bootstrap/3.4.1/js/bootstrap.min.js"></script>
        <style>
            :root {
                --bg-color: #f4f6f9;
                --text-color: #333333;
                --panel-bg: #ffffff;
                --border-color: #dddddd;
                --chat-bg: #e5e5ea;
                --bubble-self: #dcf8c6;
                --bubble-other: #ffffff;
                --header-bg: #337ab7;
                --header-text: #ffffff;
                --sidebar-bg: #f8f9fa;
                --input-bg: #ffffff;
                --input-text: #333333;
            }

            [data-theme="dark"] {
                --bg-color: #121212;
                --text-color: #e0e0e0;
                --panel-bg: #1e1e1e;
                --border-color: #333333;
                --chat-bg: #0d0d0d;
                --bubble-self: #056162;
                --bubble-other: #262d31;
                --header-bg: #232d36;
                --header-text: #e0e0e0;
                --sidebar-bg: #1e1e1e;
                --input-bg: #2a2f32;
                --input-text: #e0e0e0;
            }

            body {
                background-color: var(--bg-color);
                color: var(--text-color);
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                margin: 0;
                padding: 0;
                transition: background-color 0.3s, color 0.3s;
                height: 100vh;
                display: flex;
                flex-direction: column;
            }

            #landing-page {
                flex: 1;
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                padding: 20px;
            }

            .main-container {
                width: 100%;
                max-width: 600px;
                background: var(--panel-bg);
                padding: 30px;
                border-radius: 12px;
                box-shadow: 0 8px 24px rgba(0,0,0,0.15);
                border: 1px solid var(--border-color);
            }

            .site-footer {
                background-color: var(--panel-bg);
                border-top: 1px solid var(--border-color);
                padding: 15px 20px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                box-shadow: 0 -2px 10px rgba(0,0,0,0.05);
            }

            .footer-links a { margin-right: 15px; color: var(--text-color); text-decoration: none; font-weight: bold; }
            .footer-links a:hover { color: var(--header-bg); }

            #chat-page {
                display: none;
                flex: 1;
                flex-direction: column;
                height: 100vh;
            }

            .chat-header {
                background-color: var(--header-bg);
                color: var(--header-text);
                padding: 15px 20px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                box-shadow: 0 2px 5px rgba(0,0,0,0.2);
                z-index: 10;
            }

            .chat-body-wrapper {
                display: flex;
                flex-direction: row; /* Изменили на ряд для 3-х колонок */
                flex: 1;
                overflow: hidden;
            }

            .chat-sidebar {
                width: 250px;
                background-color: var(--sidebar-bg);
                border-right: 1px solid var(--border-color); /* Слева, поэтому бордер справа */
                display: flex;
                flex-direction: column;
                transition: width 0.3s;
                order: 1; /* Первая колонка */
            }

            .chat-center-area {
                flex: 2; /* Занимает центральное пространство */
                background-color: #000;
                display: flex;
                flex-direction: column;
                order: 2; /* Вторая колонка */
                position: relative;
                justify-content: center;
                align-items: center;
            }

            .chat-main-area {
                width: 350px; /* Фиксированная или адаптивная ширина чата */
                display: flex;
                flex-direction: column;
                background-color: var(--chat-bg);
                border-left: 1px solid var(--border-color);
                order: 3; /* Третья колонка */
            }

            /* Стили трансляции */
            #remote-video {
                width: 100%;
                height: 100%;
                object-fit: contain;
                display: none;
            }

            .stream-controls {
                position: absolute;
                bottom: 20px;
                left: 50%;
                transform: translateX(-50%);
                z-index: 100;
                display: flex;
                gap: 10px;
            }

            .no-stream-placeholder {
                color: #555;
                font-size: 1.5em;
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 10px;
            }
            [data-theme="dark"] .no-stream-placeholder { color: #aaa; }

            @media (max-width: 992px) {
                .chat-body-wrapper { flex-direction: column; }
                .chat-sidebar { width: 100%; height: 150px; border-right: none; border-bottom: 1px solid var(--border-color); }
                .chat-center-area { flex: 1; min-height: 300px; }
                .chat-main-area { width: 100%; flex: 1; border-left: none; }
            }

            #chat-window {
                flex: 1;
                padding: 20px;
                overflow-y: auto;
                display: flex;
                flex-direction: column;
            }

            .msg-row { display: flex; width: 100%; margin-bottom: 15px; }
            .msg-row-self { justify-content: flex-end; }
            .msg-row-other { justify-content: flex-start; }
            
            .msg-bubble {
                padding: 10px 14px;
                border-radius: 18px;
                max-width: 75%;
                position: relative;
                box-shadow: 0 1px 2px rgba(0,0,0,0.15);
                word-wrap: break-word;
            }
            .msg-self { background-color: var(--bubble-self); border-bottom-right-radius: 4px; color: var(--text-color); }
            .msg-other { background-color: var(--bubble-other); border-bottom-left-radius: 4px; color: var(--text-color); }
            
            .msg-sender { font-weight: bold; font-size: 0.85em; margin-bottom: 4px; }
            .msg-text { font-size: 1em; line-height: 1.4; }
            .msg-time { font-size: 0.7em; color: #888; text-align: right; margin-top: 4px; }
            
            .msg-system {
                align-self: center;
                background-color: rgba(0,0,0,0.1);
                color: var(--text-color);
                padding: 5px 12px;
                border-radius: 12px;
                font-size: 0.85em;
                margin-bottom: 15px;
            }
            [data-theme="dark"] .msg-system { background-color: rgba(255,255,255,0.1); }

            .chat-input-area {
                padding: 15px;
                background-color: var(--panel-bg);
                border-top: 1px solid var(--border-color);
            }
            .chat-input-area input {
                background-color: var(--input-bg);
                color: var(--input-text);
                border: 1px solid var(--border-color);
            }

            .sidebar-header {
                padding: 15px;
                background-color: var(--header-bg);
                color: var(--header-text);
                font-weight: bold;
                border-bottom: 1px solid var(--border-color);
                text-align: center;
            }
            .user-list {
                list-style: none;
                padding: 0;
                margin: 0;
                overflow-y: auto;
                flex: 1;
            }
            .user-list li {
                padding: 12px 15px;
                border-bottom: 1px solid var(--border-color);
                font-weight: 500;
                display: flex;
                align-items: center;
                color: var(--text-color);
            }
            .user-list li i { margin-right: 10px; }

            /* Всплывающие уведомления */
            #toast-container {
                position: fixed; top: 20px; right: 20px; z-index: 9999;
            }
            .toast-msg {
                background: #e74c3c; color: white; padding: 15px 20px;
                border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.2);
                margin-bottom: 10px; display: none;
            }

            body.obs-mode {
                background-color: transparent !important;
            }
            body.obs-mode #landing-page, 
            body.obs-mode .site-footer, 
            body.obs-mode .chat-header, 
            body.obs-mode .chat-input-area, 
            body.obs-mode .chat-sidebar {
                display: none !important;
            }
            body.obs-mode #chat-page {
                display: flex !important;
                background-color: transparent !important;
            }
            body.obs-mode .chat-main-area { width: 100% !important; background-color: transparent !important; border: none !important; }
            body.obs-mode .chat-center-area { display: none !important; }
            body.obs-mode #chat-window { padding-bottom: 40px; }
            body.obs-mode ::-webkit-scrollbar { display: none; }
            
            /* Twitch Style Chat for OBS */
            body.obs-mode #chat-window {
                justify-content: flex-end;
                padding: 10px;
                padding-bottom: 20px;
            }
            .twitch-msg {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                font-size: 18px;
                font-weight: bold;
                color: #ffffff;
                text-shadow: 1px 1px 0 #000, -1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000, 2px 2px 4px rgba(0,0,0,0.8);
                margin-bottom: 8px;
                line-height: 1.4;
                word-wrap: break-word;
                animation: fadeIn 0.3s ease-in;
            }
            .twitch-time { font-size: 0.75em; color: #cccccc; margin-right: 6px; }
            .twitch-name { margin-right: 6px; }
            .twitch-sys { color: #ffff00; font-style: italic; }
            
            @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        </style>
    </head>
    <body>
        <!-- Всплывающие ошибки -->
        <div id="toast-container"></div>

        <div id="landing-page">
            <div class="main-container">
                <h2 class="text-center" style="margin-bottom: 30px; font-weight: bold;">
                    <i class="fa fa-comments" style="color: #337ab7;"></i> <span data-i18n="appTitle">FastAPI RAM IRC Chat</span>
                </h2>

                <!-- 1. НИКНЕЙМ -->
                <div class="form-group">
                    <label for="nickname-input" style="font-size: 1.1em;" data-i18n="step1Title">1. Ваш никнейм (Обязательно):</label>
                    <div class="input-group input-group-lg">
                        <span class="input-group-addon"><i class="fa fa-user"></i></span>
                        <input type="text" id="nickname-input" class="form-control" placeholder="CyberNinja" maxlength="20">
                        <span class="input-group-btn">
                            <button class="btn btn-primary" type="button" id="btn-set-nickname" data-i18n="saveNickBtn">Сохранить</button>
                        </span>
                    </div>
                    <p class="help-block text-center" id="nickname-hint" style="margin-top: 10px;"></p>
                </div>

                <hr style="border-color: var(--border-color);">

                <!-- 2. ДЕЙСТВИЯ -->
                <div id="actions-panel" style="opacity: 0.4; pointer-events: none; transition: opacity 0.3s;">
                    <label style="font-size: 1.1em;" data-i18n="step2Title">2. Выберите действие:</label>
                    <ul class="nav nav-pills nav-justified" id="action-tabs" style="margin-bottom: 20px;">
                        <li class="active"><a data-toggle="pill" href="#tab-join" data-i18n="tabJoin">Войти по коду</a></li>
                        <li><a data-toggle="pill" href="#tab-create" data-i18n="tabCreate">Создать комнату</a></li>
                    </ul>

                    <div class="tab-content">
                        <!-- Вход по коду -->
                        <div id="tab-join" class="tab-pane fade in active">
                            <div class="form-group">
                                <label for="join-code-input" data-i18n="codeLabel">Код комнаты (6 символов):</label>
                                <input type="text" id="join-code-input" class="form-control input-lg text-uppercase" placeholder="X Y Z 1 2 3" maxlength="6">
                            </div>
                            <button class="btn btn-success btn-block btn-lg" id="btn-join-room">
                                <i class="fa fa-sign-in"></i> <span data-i18n="joinBtn">Войти в комнату</span>
                            </button>
                        </div>

                        <!-- Создание комнаты -->
                        <div id="tab-create" class="tab-pane fade">
                            <div class="form-group">
                                <label for="max-users-select" data-i18n="maxUsersLabel">Максимум участников:</label>
                                <select id="max-users-select" class="form-control input-lg">
                                    <option value="2">2</option>
                                    <option value="5" selected>5</option>
                                    <option value="10">10</option>
                                    <option value="20">20</option>
                                    <option value="50">50</option>
                                </select>
                            </div>
                            <button class="btn btn-info btn-block btn-lg" id="btn-create-room">
                                <i class="fa fa-plus"></i> <span data-i18n="createBtn">Создать комнату</span>
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <footer class="site-footer" style="justify-content: flex-end;">
            <div class="footer-controls">
                <button class="btn btn-default btn-sm" id="btn-toggle-lang"><i class="fa fa-language"></i> <span data-i18n="btnLang">English</span></button>
                <button class="btn btn-default btn-sm" id="btn-toggle-theme"><i class="fa fa-moon-o"></i> <span data-i18n="btnTheme">Сменить тему</span></button>
            </div>
        </footer>

        <div id="chat-page">
            <div class="chat-header">
                <div>
                    <strong><i class="fa fa-hashtag"></i> <span data-i18n="chatRoomCode">Комната:</span></strong> 
                    <span id="display-room-code" class="label label-warning" style="font-size: 1.2em; margin-left: 5px; letter-spacing: 2px;">------</span>
                </div>
                <div>
                    <button class="btn btn-sm btn-info" id="btn-obs" style="margin-right: 10px;">
                        <i class="fa fa-video-camera"></i> <span data-i18n="btnObs">OBS Код</span>
                    </button>
                    <button class="btn btn-sm btn-danger" id="btn-leave-room">
                        <i class="fa fa-sign-out"></i> <span data-i18n="btnLeave">Выйти</span>
                    </button>
                </div>
            </div>
            
            <div class="chat-body-wrapper">
                <!-- Левая колонка: Участники -->
                <div class="chat-sidebar">
                    <div class="sidebar-header sidebar-title">
                        <i class="fa fa-users"></i> <span data-i18n="chatOnline">Участники</span> (<span id="online-count">0</span>)
                    </div>
                    <ul class="user-list" id="users-list-ul">
                        <!-- Список пользователей -->
                    </ul>
                </div>

                <!-- Центральная колонка: Трансляция -->
                <div class="chat-center-area">
                    <video id="remote-video" autoplay playsinline></video>
                    <div class="no-stream-placeholder" id="no-stream-msg">
                        <i class="fa fa-television" style="font-size: 3em;"></i>
                        <span data-i18n="noStream">Трансляция не запущена</span>
                    </div>
                    <div class="stream-controls">
                        <button class="btn btn-success btn-lg" id="btn-share-screen" style="box-shadow: 0 4px 6px rgba(0,0,0,0.3);">
                            <i class="fa fa-desktop"></i> <span data-i18n="btnShare">Начать трансляцию</span>
                        </button>
                    </div>
                </div>
                
                <!-- Правая колонка: Чат -->
                <div class="chat-main-area">
                    <div id="chat-window"></div>
                    <div class="chat-input-area">
                        <form id="msg-form" onsubmit="return false;" style="display: flex;">
                            <input type="text" id="msg-input" class="form-control input-lg" placeholder="Написать сообщение..." autocomplete="off" style="flex: 1; border-top-right-radius: 0; border-bottom-right-radius: 0;">
                            <button class="btn btn-primary btn-lg" type="submit" id="btn-send-msg" style="border-top-left-radius: 0; border-bottom-left-radius: 0; width: 60px;" title="Отправить">
                                <i class="fa fa-paper-plane"></i>
                            </button>
                        </form>
                    </div>
                </div>
            </div>
        </div>

        <!-- Модальное окно OBS -->
        <div id="obsModal" class="modal fade" role="dialog">
            <div class="modal-dialog">
                <div class="modal-content" style="background-color: var(--panel-bg); color: var(--text-color);">
                    <div class="modal-header" style="border-color: var(--border-color);">
                        <button type="button" class="close" data-dismiss="modal" style="color: var(--text-color);">&times;</button>
                        <h4 class="modal-title"><i class="fa fa-video-camera"></i> <span data-i18n="obsModalTitle">Ссылка для OBS</span></h4>
                    </div>
                    <div class="modal-body">
                        <p data-i18n="obsModalBody">Скопируйте эту ссылку и добавьте как Browser Source в OBS Studio. Фон будет прозрачным.</p>
                        <input type="text" id="obs-link-input" class="form-control" readonly style="background-color: var(--input-bg); color: var(--input-text); border-color: var(--border-color);">
                    </div>
                    <div class="modal-footer" style="border-color: var(--border-color);">
                        <button type="button" class="btn btn-default" data-dismiss="modal" data-i18n="obsModalClose">Закрыть</button>
                    </div>
                </div>
            </div>
        </div>

        <script>
            let currentNickname = "";
            let currentRoomCode = "";
            let socket = null;
            let currentLang = 'ru';
            
            // WebRTC переменные
            let myClientId = null;
            let peers = {}; // Храним RTCPeerConnection для каждого получателя
            let localStream = null;
            const rtcConfig = { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] };

            const i18n = {
                ru: {
                    appTitle: "FastAPI RAM IRC Chat", step1Title: "1. Ваш никнейм (Обязательно):",
                    saveNickBtn: "Сохранить", nickSaved: "Никнейм установлен: ",
                    nickError: "Пожалуйста, введите корректный никнейм!", step2Title: "2. Выберите действие:",
                    tabJoin: "Войти по коду", tabCreate: "Создать комнату", codeLabel: "Код комнаты (6 символов):",
                    joinBtn: "Войти в комнату", maxUsersLabel: "Максимум участников:", createBtn: "Создать комнату",
                    btnLang: "English", btnTheme: "Сменить тему", chatRoomCode: "Комната:",
                    btnLeave: "Выйти", btnObs: "OBS Код", chatOnline: "Участники",
                    msgSend: "Отправить", obsModalTitle: "Ссылка для OBS",
                    obsModalBody: "Скопируйте эту ссылку и добавьте как Browser Source (Источник браузера) в OBS Studio. Фон будет прозрачным.",
                    obsModalClose: "Закрыть", sysJoined: "вошел в чат.", sysLeft: "вышел из чата.",
                    errCodeFormat: "Код комнаты должен состоять ровно из 6 символов!", errNeedNick: "Сначала введите никнейм!",
                    errCreate: "Ошибка при создании комнаты.", errCheck: "Ошибка при проверке комнаты.",
                    errNotFound: "Комната не найдена!", errFull: "Комната переполнена!", msgPlaceholder: "Написать сообщение...",
                    btnShare: "Начать трансляцию", btnStopShare: "Остановить трансляцию", noStream: "Трансляция не запущена"
                },
                en: {
                    appTitle: "FastAPI RAM IRC Chat", step1Title: "1. Your Nickname (Required):",
                    saveNickBtn: "Save", nickSaved: "Nickname set: ",
                    nickError: "Please enter a valid nickname!", step2Title: "2. Choose an action:",
                    tabJoin: "Join by Code", tabCreate: "Create Room", codeLabel: "Room Code (6 chars):",
                    joinBtn: "Join Room", maxUsersLabel: "Max participants:", createBtn: "Create Room",
                    btnLang: "Русский", btnTheme: "Toggle Theme", chatRoomCode: "Room:",
                    btnLeave: "Leave", btnObs: "OBS Code", chatOnline: "Participants",
                    msgSend: "Send", obsModalTitle: "OBS Link",
                    obsModalBody: "Copy this link and add it as a Browser Source in OBS Studio. The background will be transparent.",
                    obsModalClose: "Close", sysJoined: "joined the chat.", sysLeft: "left the chat.",
                    errCodeFormat: "Room code must be exactly 6 characters!", errNeedNick: "Enter a nickname first!",
                    errCreate: "Error creating room.", errCheck: "Error checking room.",
                    errNotFound: "Room not found!", errFull: "Room is full!", msgPlaceholder: "Type a message...",
                    btnShare: "Share Screen", btnStopShare: "Stop Sharing", noStream: "No active stream"
                }
            };

            function updateLanguage(lang) {
                currentLang = lang;
                const dict = i18n[lang];
                $('[data-i18n]').each(function() {
                    const key = $(this).attr('data-i18n');
                    if (dict[key]) $(this).text(dict[key]);
                });
                $('#msg-input').attr('placeholder', dict.msgPlaceholder);
            }

            $('#btn-toggle-lang').click(function() {
                updateLanguage(currentLang === 'ru' ? 'en' : 'ru');
            });

            $('#btn-toggle-theme').click(function() {
                const body = $('body');
                if(body.attr('data-theme') === 'dark') {
                    body.removeAttr('data-theme');
                } else {
                    body.attr('data-theme', 'dark');
                }
            });

            function showToast(msg) {
                const toast = $('<div class="toast-msg"></div>').text(msg);
                $('#toast-container').append(toast);
                toast.fadeIn(300).delay(3000).fadeOut(300, function() { $(this).remove(); });
            }

            function getAvatarColor(str) {
                let hash = 0;
                for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash);
                const c = (hash & 0x00FFFFFF).toString(16).toUpperCase();
                return '#' + '00000'.substring(0, 6 - c.length) + c;
            }

            function escapeHtml(string) {
                return String(string).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
            }

            const urlParams = new URLSearchParams(window.location.search);
            const obsCode = urlParams.get('obs');
            const chatCode = urlParams.get('chat');
            
            if (obsCode) {
                $('body').addClass('obs-mode');
                currentNickname = "OBS_Viewer_" + Math.floor(Math.random() * 10000);
                joinWebSocketRoom(obsCode);
            }

            // Восстановление никнейма из local storage
            const savedNick = localStorage.getItem('irc_nickname');
            if (savedNick) {
                $('#nickname-input').val(savedNick);
                
                // Если есть код комнаты в ссылке, автоматически сохраняем ник и входим
                if (chatCode) {
                    setTimeout(() => $('#btn-set-nickname').click(), 100);
                }
            }

            // Авто-заполнение кода комнаты из ссылки
            if (chatCode) {
                $('#join-code-input').val(chatCode);
            }

            $('#btn-obs').click(function() {
                const obsUrl = window.location.protocol + "//" + window.location.host + "/?obs=" + currentRoomCode;
                $('#obs-link-input').val(obsUrl);
                $('#obsModal').modal('show');
            });

            $('#btn-set-nickname').click(function() {
                const val = $('#nickname-input').val().trim();
                if (!val) { showToast(i18n[currentLang].nickError); return; }
                
                currentNickname = val;
                localStorage.setItem('irc_nickname', currentNickname); // Сохраняем ник
                
                $('#nickname-input').prop('disabled', true);
                $('#btn-set-nickname').prop('disabled', true).removeClass('btn-primary').addClass('btn-success').html('<i class="fa fa-check"></i>');
                $('#nickname-hint').html('<b class="text-success">' + i18n[currentLang].nickSaved + currentNickname + '</b>');
                
                $('#actions-panel').css({'opacity': '1', 'pointer-events': 'auto'});
                
                // Авто-вход если комната указана в ссылке
                if (chatCode && !socket) {
                    $('#btn-join-room').click();
                }
            });

            $('#nickname-input').keypress(function(e) { if (e.which === 13) $('#btn-set-nickname').click(); });

            $('#btn-create-room').click(async function() {
                if (!currentNickname) { showToast(i18n[currentLang].errNeedNick); return; }
                const maxUsers = parseInt($('#max-users-select').val());

                try {
                    const response = await fetch('/api/create-room', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ max_users: maxUsers })
                    });
                    const data = await response.json();
                    if (data.room_code) joinWebSocketRoom(data.room_code);
                } catch (err) { showToast(i18n[currentLang].errCreate); }
            });

            $('#btn-join-room').click(async function() {
                if (!currentNickname) { showToast(i18n[currentLang].errNeedNick); return; }
                const code = $('#join-code-input').val().trim().toUpperCase();
                if (code.length !== 6) { showToast(i18n[currentLang].errCodeFormat); return; }

                try {
                    const res = await fetch('/api/check-room/' + code);
                    const info = await res.json();
                    if (!info.exists) { showToast(i18n[currentLang].errNotFound); return; }
                    if (info.current_users >= info.max_users) { showToast(i18n[currentLang].errFull); return; }
                    joinWebSocketRoom(code);
                } catch (err) { showToast(i18n[currentLang].errCheck); }
            });

            function joinWebSocketRoom(code) {
                currentRoomCode = code;
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = `${protocol}//${window.location.host}/ws/${code}/${encodeURIComponent(currentNickname)}`;

                socket = new WebSocket(wsUrl);

                socket.onopen = function() {
                    $('#landing-page').hide();
                    $('.site-footer').hide();
                    $('#chat-page').css('display', 'flex');
                    $('#display-room-code').text(code);
                    $('#chat-window').empty();
                    
                    // Обновляем ссылку для сохранения сессии без перезагрузки страницы
                    if (!$('body').hasClass('obs-mode')) {
                        window.history.replaceState(null, '', '?chat=' + code);
                    }
                    
                    // Автоматический фокус на поле ввода
                    setTimeout(() => $('#msg-input').focus(), 100);
                };

                socket.onmessage = function(event) {
                    const data = JSON.parse(event.data);

                    if (data.type === "welcome") {
                        myClientId = data.client_id;
                    } else if (data.type === "system") {
                        const actionText = data.text === 'joined' ? i18n[currentLang].sysJoined : i18n[currentLang].sysLeft;
                        appendSystemMessage(`[${data.time}] ${escapeHtml(data.nickname)} ${actionText}`);
                    } else if (data.type === "message") {
                        appendUserMessage(data.time, data.sender, data.text);
                    } else if (data.type === "users_update") {
                        updateSidebar(data.users);
                    } 
                    // ====== WebRTC Сигналы ======
                    else if (data.type === "webrtc_start") {
                        // Кто-то начал трансляцию, запрашиваем у него Offer
                        if (data.sender_id !== myClientId) {
                            socket.send(JSON.stringify({ type: "webrtc_request", target_id: data.sender_id, sender_id: myClientId }));
                        }
                    } else if (data.type === "webrtc_request") {
                        // Я стример, кто-то запросил мой поток
                        if (localStream) {
                            const target = data.sender_id;
                            const pc = new RTCPeerConnection(rtcConfig);
                            peers[target] = pc;

                            localStream.getTracks().forEach(track => pc.addTrack(track, localStream));

                            pc.onicecandidate = e => {
                                if (e.candidate) {
                                    socket.send(JSON.stringify({ type: "webrtc_ice", target_id: target, sender_id: myClientId, candidate: e.candidate }));
                                }
                            };

                            pc.createOffer().then(offer => pc.setLocalDescription(offer)).then(() => {
                                socket.send(JSON.stringify({ type: "webrtc_offer", target_id: target, sender_id: myClientId, sdp: pc.localDescription }));
                            });
                        }
                    } else if (data.type === "webrtc_offer") {
                        // Я зритель, стример прислал мне Offer
                        const pc = new RTCPeerConnection(rtcConfig);
                        peers[data.sender_id] = pc;

                        pc.onicecandidate = e => {
                            if (e.candidate) {
                                socket.send(JSON.stringify({ type: "webrtc_ice", target_id: data.sender_id, sender_id: myClientId, candidate: e.candidate }));
                            }
                        };

                        pc.ontrack = e => {
                            const video = document.getElementById('remote-video');
                            if (video.srcObject !== e.streams[0]) {
                                video.srcObject = e.streams[0];
                                video.style.display = 'block';
                                $('#no-stream-msg').hide();
                            }
                        };

                        pc.setRemoteDescription(new RTCSessionDescription(data.sdp))
                          .then(() => pc.createAnswer())
                          .then(answer => pc.setLocalDescription(answer))
                          .then(() => {
                              socket.send(JSON.stringify({ type: "webrtc_answer", target_id: data.sender_id, sender_id: myClientId, sdp: pc.localDescription }));
                          });
                    } else if (data.type === "webrtc_answer") {
                        // Я стример, получил Answer от зрителя
                        const pc = peers[data.sender_id];
                        if (pc) pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
                    } else if (data.type === "webrtc_ice") {
                        // Обмен ICE кандидатами (и стример и зритель)
                        const pc = peers[data.sender_id];
                        if (pc) pc.addIceCandidate(new RTCIceCandidate(data.candidate)).catch(e => console.log(e));
                    } else if (data.type === "webrtc_stop") {
                        // Стример завершил трансляцию или вышел
                        if (peers[data.sender_id]) {
                            peers[data.sender_id].close();
                            delete peers[data.sender_id];
                        }
                        const video = document.getElementById('remote-video');
                        video.srcObject = null;
                        video.style.display = 'none';
                        $('#no-stream-msg').show();
                    }
                };

                socket.onclose = function() {
                    // Очистка WebRTC при обрыве
                    stopLocalStream();
                    if(!$('body').hasClass('obs-mode')) {
                        showToast("Соединение закрыто.");
                        leaveRoomUI();
                    }
                };
            }

            $('#msg-form').submit(function() {
                const text = $('#msg-input').val().trim();
                if (text && socket && socket.readyState === WebSocket.OPEN) {
                    socket.send(JSON.stringify({ type: "message", text: text })); // Отправляем в формате JSON
                    $('#msg-input').val('');
                }
                return false;
            });

            // --- Управление трансляцией ---
            $('#btn-share-screen').click(async function() {
                if (localStream) {
                    // Остановка трансляции
                    stopLocalStream();
                } else {
                    // Запуск трансляции
                    try {
                        localStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
                        
                        // Меняем кнопку
                        $(this).removeClass('btn-success').addClass('btn-danger').html(`<i class="fa fa-stop"></i> <span data-i18n="btnStopShare">${i18n[currentLang].btnStopShare}</span>`);
                        
                        // Показываем у себя
                        const video = document.getElementById('remote-video');
                        video.srcObject = localStream;
                        video.style.display = 'block';
                        video.muted = true; // Себя не слушаем (избегаем эхо)
                        $('#no-stream-msg').hide();

                        // Если закрыли доступ через панель браузера
                        localStream.getVideoTracks()[0].onended = () => { stopLocalStream(); };

                        // Сообщаем всем, что мы начали стримить
                        socket.send(JSON.stringify({ type: "webrtc_start", sender_id: myClientId }));

                    } catch (err) {
                        showToast("Доступ к экрану запрещен или ошибка.");
                    }
                }
            });

            function stopLocalStream() {
                if (localStream) {
                    localStream.getTracks().forEach(t => t.stop());
                    localStream = null;
                }
                $('#btn-share-screen').removeClass('btn-danger').addClass('btn-success').html(`<i class="fa fa-desktop"></i> <span data-i18n="btnShare">${i18n[currentLang].btnShare}</span>`);
                
                const video = document.getElementById('remote-video');
                video.srcObject = null;
                video.style.display = 'none';
                $('#no-stream-msg').show();

                if (socket && socket.readyState === WebSocket.OPEN && myClientId) {
                    socket.send(JSON.stringify({ type: "webrtc_stop", sender_id: myClientId }));
                }

                // Закрываем все соединения
                Object.values(peers).forEach(pc => pc.close());
                peers = {};
            }

            $('#btn-leave-room').click(function() {
                if (socket) socket.close();
                leaveRoomUI();
            });

            function leaveRoomUI() {
                $('#chat-page').hide();
                $('#landing-page').show();
                $('.site-footer').css('display', 'flex');
                currentRoomCode = "";
                $('#users-list-ul').empty();
                
                // Очищаем ссылку при выходе
                window.history.replaceState(null, '', '/');
            }

            function appendSystemMessage(text) {
                const win = $('#chat-window');
                if ($('body').hasClass('obs-mode')) {
                    win.append(`<div class="twitch-msg twitch-sys">${text}</div>`);
                } else {
                    win.append(`<div class="msg-system">${text}</div>`);
                }
                optimizeDOM(win);
                win.scrollTop(win[0].scrollHeight);
            }

            function appendUserMessage(time, sender, text) {
                const win = $('#chat-window');
                const isSelf = sender === currentNickname;
                const color = getAvatarColor(sender);
                const isObs = $('body').hasClass('obs-mode');

                if (isObs) {
                    win.append(`
                        <div class="twitch-msg">
                            <span class="twitch-time">[${time}]</span>
                            <span class="twitch-name" style="color: ${color};">${escapeHtml(sender)}:</span>
                            <span class="twitch-text">${escapeHtml(text)}</span>
                        </div>
                    `);
                } else {
                    const alignClass = isSelf ? "msg-row-self" : "msg-row-other";
                    const bubbleClass = isSelf ? "msg-self" : "msg-other";
                    
                    let senderHtml = !isSelf ? `<div class="msg-sender" style="color: ${color};">${escapeHtml(sender)}</div>` : "";

                    win.append(`
                        <div class="msg-row ${alignClass}">
                            <div class="msg-bubble ${bubbleClass}">
                                ${senderHtml}
                                <div class="msg-text">${escapeHtml(text)}</div>
                                <div class="msg-time">${time}</div>
                            </div>
                        </div>
                    `);
                }
                optimizeDOM(win);
                win.scrollTop(win[0].scrollHeight);
            }

            // Оптимизация: Удаление старых сообщений, чтобы не засорять память в OBS и долгих сессиях
            function optimizeDOM(win) {
                const maxMessages = 150;
                if (win.children().length > maxMessages) {
                    win.children().first().remove();
                }
            }

            function updateSidebar(users) {
                $('#online-count').text(users.length);
                const ul = $('#users-list-ul');
                ul.empty();
                users.forEach(u => {
                    const color = getAvatarColor(u);
                    const isSelf = u === currentNickname;
                    const weight = isSelf ? "bold" : "normal";
                    const mark = isSelf ? " (Вы)" : "";
                    ul.append(`<li><i class="fa fa-user-circle" style="color:${color}; font-size:1.2em;"></i> <span style="font-weight:${weight};">${escapeHtml(u)}${mark}</span></li>`);
                });
            }

            // Инициализация языка по умолчанию
            updateLanguage('ru');
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    print("Запуск FastAPI IRC сервера...")
    print("Откройте в браузере: http://127.0.0.1:8000")
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
