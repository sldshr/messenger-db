import os
import json
import random
import string
import datetime
import uuid
from typing import Dict, List, Optional
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, HTTPException, status, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "discord-ram-chat-secret-key-12345")

app = FastAPI(title="RAM Discord-like Server Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ОЗУ структура хранилища сессий (без сторонних зависимостей типа itsdangerous)
sessions_db: Dict[str, dict] = {}

# ОЗУ структура серверов
# {
#   server_code: {
#       "id": server_code,
#       "name": str,
#       "owner_id": str,
#       "max_users": int,
#       "channels": {
#           "text": [{"id": "general", "name": "general"}, {"id": "off-topic", "name": "off-topic"}],
#           "voice": [{"id": "voice-general", "name": "General Voice"}, {"id": "voice-gaming", "name": "Gaming Lounge"}]
#       },
#       "messages": { "general": [], "off-topic": [] },
#       "connections": { websocket: { "user_id": str, "nickname": str, "picture": str, "voice_channel": None } }
#   }
# }
servers: Dict[str, dict] = {}


class CreateServerRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=32)
    max_users: int = Field(50, ge=2, le=200)


def generate_server_code(length: int = 6) -> str:
    """Генерация уникального 6-значного кода сервера."""
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
        if code not in servers:
            return code


def get_current_user(request: Request) -> Optional[dict]:
    """Получение авторизованного пользователя из Cookie/ОЗУ сессий."""
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions_db:
        return sessions_db[session_id]
    return None


@app.get("/api/me")
async def get_me(request: Request):
    """Получение текущего залогиненного пользователя."""
    user = get_current_user(request)
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "user": user}


@app.post("/api/login")
async def login(nickname: str):
    """Вход по никнейму с сохранением сессии в ОЗУ."""
    nickname = nickname.strip()
    if not nickname:
        raise HTTPException(status_code=400, detail="Никнейм не может быть пустым")
    
    session_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    avatar_url = f"https://api.dicebear.com/7.x/bottts/svg?seed={nickname}"
    
    user_data = {
        "id": user_id,
        "nickname": nickname,
        "picture": avatar_url
    }
    sessions_db[session_id] = user_data

    response = JSONResponse(content={"status": "ok", "user": user_data})
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        max_age=86400 * 30,
        samesite="lax"
    )
    return response


@app.get("/logout")
async def logout(request: Request):
    """Выход из аккаунта."""
    session_id = request.cookies.get("session_id")
    if session_id in sessions_db:
        del sessions_db[session_id]
    response = RedirectResponse("/")
    response.delete_cookie("session_id")
    return response


@app.post("/api/create-server")
async def create_server(req: CreateServerRequest, request: Request):
    """Создание нового сервера с Discord-структурой каналов."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Необходима авторизация")

    code = generate_server_code()
    servers[code] = {
        "id": code,
        "name": req.name,
        "owner_id": user["id"],
        "max_users": req.max_users,
        "channels": {
            "text": [
                {"id": "general", "name": "general"},
                {"id": "off-topic", "name": "off-topic"}
            ],
            "voice": [
                {"id": "voice-general", "name": "General Voice"},
                {"id": "voice-gaming", "name": "Gaming Lounge"}
            ]
        },
        "messages": {
            "general": [],
            "off-topic": []
        },
        "connections": {}
    }
    return {"server_code": code, "name": req.name}


@app.get("/api/server/{code}")
async def get_server_info(code: str, request: Request):
    """Получение информации о сервере."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Необходима авторизация")

    code = code.upper().strip()
    if code not in servers:
        raise HTTPException(status_code=404, detail="Сервер не найден")

    server = servers[code]
    return {
        "id": server["id"],
        "name": server["name"],
        "max_users": server["max_users"],
        "channels": server["channels"],
        "current_users": len(server["connections"])
    }


@app.websocket("/ws/{server_code}")
async def websocket_endpoint(websocket: WebSocket, server_code: str):
    server_code = server_code.upper().strip()

    if server_code not in servers:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    server = servers[server_code]

    if len(server["connections"]) >= server["max_users"]:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    client_id = str(uuid.uuid4())
    user_info = {"id": client_id, "nickname": "Guest", "picture": "", "voice_channel": None}
    server["connections"][websocket] = user_info

    try:
        while True:
            raw_text = await websocket.receive_text()
            data = json.loads(raw_text)
            msg_type = data.get("type")

            # 1. Инициализация профиля клиента
            if msg_type == "init":
                user_info["id"] = data.get("user_id", client_id)
                user_info["nickname"] = data.get("nickname", "Guest")
                user_info["picture"] = data.get("picture", "")
                
                await websocket.send_json({
                    "type": "init_success",
                    "server_id": server_code,
                    "channels": server["channels"],
                    "history": server["messages"]
                })
                
                await broadcast_members_update(server_code)

            # 2. Текстовое сообщение в канал
            elif msg_type == "chat_message":
                channel_id = data.get("channel_id", "general")
                text = data.get("text", "").strip()
                if text and channel_id in server["messages"]:
                    msg_obj = {
                        "id": str(uuid.uuid4()),
                        "sender_id": user_info["id"],
                        "sender_name": user_info["nickname"],
                        "sender_picture": user_info["picture"],
                        "text": text,
                        "time": datetime.datetime.now().strftime("%H:%M"),
                        "channel_id": channel_id
                    }
                    server["messages"][channel_id].append(msg_obj)
                    if len(server["messages"][channel_id]) > 100:
                        server["messages"][channel_id].pop(0)

                    await broadcast_to_server(server_code, {
                        "type": "new_message",
                        "message": msg_obj
                    })

            # 3. Вход в Голосовой Канал WebRTC Mesh
            elif msg_type == "join_voice":
                v_channel = data.get("channel_id")
                user_info["voice_channel"] = v_channel
                
                # Уведомляем участников голосового канала
                await broadcast_to_server(server_code, {
                    "type": "user_joined_voice",
                    "user_id": user_info["id"],
                    "nickname": user_info["nickname"],
                    "picture": user_info["picture"],
                    "channel_id": v_channel
                })
                await broadcast_members_update(server_code)

            elif msg_type == "leave_voice":
                old_v = user_info["voice_channel"]
                user_info["voice_channel"] = None
                
                await broadcast_to_server(server_code, {
                    "type": "user_left_voice",
                    "user_id": user_info["id"],
                    "channel_id": old_v
                })
                await broadcast_members_update(server_code)

            # 4. WebRTC Сигнализация для Голосовых каналов (P2P Mesh)
            elif msg_type in ["voice_offer", "voice_answer", "voice_ice"]:
                target_id = data.get("target_id")
                if target_id:
                    data["sender_id"] = user_info["id"]
                    await send_to_user_id(server_code, target_id, data)

            # 5. Статус микрофона/динамиков и Voice Activity Detection
            elif msg_type == "voice_state":
                await broadcast_to_server(server_code, {
                    "type": "voice_state_update",
                    "user_id": user_info["id"],
                    "muted": data.get("muted", False),
                    "deafened": data.get("deafened", False),
                    "speaking": data.get("speaking", False)
                })

    except WebSocketDisconnect:
        if server_code in servers and websocket in servers[server_code]["connections"]:
            u_info = servers[server_code]["connections"][websocket]
            del servers[server_code]["connections"][websocket]
            
            if len(servers[server_code]["connections"]) == 0:
                del servers[server_code]
            else:
                if u_info.get("voice_channel"):
                    await broadcast_to_server(server_code, {
                        "type": "user_left_voice",
                        "user_id": u_info["id"],
                        "channel_id": u_info["voice_channel"]
                    })
                await broadcast_members_update(server_code)


async def broadcast_to_server(server_code: str, message: dict, exclude_ws: WebSocket = None):
    """Отправка сообщения всем подключенным клиентам сервера."""
    if server_code in servers:
        dead = []
        for ws in servers[server_code]["connections"].keys():
            if ws == exclude_ws:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in servers[server_code]["connections"]:
                del servers[server_code]["connections"][ws]


async def send_to_user_id(server_code: str, target_id: str, message: dict):
    """Маршрутизация WebRTC P2P пакета конкретному клиенту."""
    if server_code in servers:
        for ws, info in list(servers[server_code]["connections"].items()):
            if info["id"] == target_id:
                try:
                    await ws.send_json(message)
                except Exception:
                    pass
                break


async def broadcast_members_update(server_code: str):
    """Рассылка обновленного списка онлайн участников."""
    if server_code in servers:
        members = [
            {
                "id": info["id"],
                "nickname": info["nickname"],
                "picture": info["picture"],
                "voice_channel": info.get("voice_channel")
            }
            for info in servers[server_code]["connections"].values()
        ]
        await broadcast_to_server(server_code, {"type": "members_update", "members": members})


@app.get("/", response_class=HTMLResponse)
async def get_index():
    html_content = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Discord Web RAM Server</title>
        <!-- Bootstrap 3 & FontAwesome -->
        <link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.4.1/css/bootstrap.min.css">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css">
        <script src="https://ajax.googleapis.com/ajax/libs/jquery/3.5.1/jquery.min.js"></script>
        <script src="https://maxcdn.bootstrapcdn.com/bootstrap/3.4.1/js/bootstrap.min.js"></script>
        
        <style>
            :root {
                --bg-tertiary: #1e1f22;
                --bg-secondary: #2b2d31;
                --bg-primary: #313338;
                --bg-accent: #383a40;
                --text-normal: #dbdee1;
                --text-muted: #949ba4;
                --brand: #5865f2;
                --brand-hover: #4752c4;
                --green: #23a55a;
                --red: #f23f43;
                --border: #1e1f22;
            }

            * { box-sizing: border-box; }
            body {
                background-color: var(--bg-primary);
                color: var(--text-normal);
                font-family: 'gg sans', 'Noto Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif;
                margin: 0;
                padding: 0;
                height: 100vh;
                overflow: hidden;
            }

            /* --- ЛОГИН ЭКРАН --- */
            #auth-screen {
                display: flex;
                height: 100vh;
                justify-content: center;
                align-items: center;
                background: linear-gradient(135deg, #1e1f22 0%, #2b2d31 100%);
            }
            .auth-card {
                background-color: var(--bg-secondary);
                padding: 40px;
                border-radius: 12px;
                box-shadow: 0 8px 32px rgba(0,0,0,0.5);
                width: 100%;
                max-width: 440px;
                text-align: center;
            }
            .btn-google {
                background-color: #ffffff;
                color: #757575;
                font-weight: bold;
                border-radius: 4px;
                padding: 12px 20px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
                width: 100%;
                margin-top: 20px;
                text-decoration: none !important;
                font-size: 16px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.2);
                transition: background 0.2s;
            }
            .btn-google:hover { background-color: #f1f1f1; color: #333; }

            /* --- ОСНОВНОЙ DISCORD ИНТЕРФЕЙС --- */
            #app-layout {
                display: none;
                height: 100vh;
                width: 100vw;
                flex-direction: row;
            }

            /* 1. Левая колонка: Иконки серверов */
            .servers-sidebar {
                width: 72px;
                background-color: var(--bg-tertiary);
                display: flex;
                flex-direction: column;
                align-items: center;
                padding-top: 12px;
                gap: 8px;
            }
            .server-icon {
                width: 48px;
                height: 48px;
                border-radius: 50%;
                background-color: var(--bg-primary);
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: bold;
                font-size: 18px;
                cursor: pointer;
                transition: all 0.2s;
                color: var(--text-normal);
                position: relative;
            }
            .server-icon:hover, .server-icon.active {
                border-radius: 16px;
                background-color: var(--brand);
                color: white;
            }
            .server-icon-add {
                background-color: var(--bg-accent);
                color: var(--green);
            }
            .server-icon-add:hover {
                background-color: var(--green);
                color: white;
            }

            /* 2. Колонка каналов сервера */
            .channels-sidebar {
                width: 240px;
                background-color: var(--bg-secondary);
                display: flex;
                flex-direction: column;
            }
            .server-header {
                height: 48px;
                padding: 0 16px;
                border-bottom: 1px solid rgba(0,0,0,0.2);
                display: flex;
                align-items: center;
                justify-content: space-between;
                font-weight: bold;
                font-size: 16px;
                box-shadow: 0 1px 2px rgba(0,0,0,0.2);
            }
            .channels-list {
                flex: 1;
                overflow-y: auto;
                padding: 12px 8px;
            }
            .channel-category {
                font-size: 12px;
                font-weight: bold;
                color: var(--text-muted);
                text-transform: uppercase;
                margin: 16px 0 6px 8px;
            }
            .channel-item {
                display: flex;
                align-items: center;
                padding: 8px 10px;
                border-radius: 4px;
                color: var(--text-muted);
                cursor: pointer;
                margin-bottom: 2px;
                font-size: 15px;
            }
            .channel-item i { margin-right: 8px; width: 18px; text-align: center; }
            .channel-item:hover { background-color: rgba(255,255,255,0.05); color: var(--text-normal); }
            .channel-item.active { background-color: var(--bg-accent); color: white; }

            /* Профиль юзера внизу сайдбара */
            .user-profile-bar {
                height: 52px;
                background-color: #232428;
                padding: 0 8px;
                display: flex;
                align-items: center;
                justify-content: space-between;
            }
            .user-avatar-small {
                width: 32px;
                height: 32px;
                border-radius: 50%;
                margin-right: 8px;
                object-fit: cover;
                background-color: var(--brand);
            }
            .user-info-text { flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
            .user-info-text .nick { font-weight: bold; font-size: 14px; }

            /* 3. Центральная область: Текстовый чат или Голосовая комната */
            .main-content {
                flex: 1;
                display: flex;
                flex-direction: column;
                background-color: var(--bg-primary);
            }
            .chat-top-bar {
                height: 48px;
                padding: 0 16px;
                border-bottom: 1px solid rgba(0,0,0,0.2);
                display: flex;
                align-items: center;
                gap: 8px;
                font-weight: bold;
                font-size: 16px;
                box-shadow: 0 1px 2px rgba(0,0,0,0.2);
            }
            .chat-messages {
                flex: 1;
                padding: 16px;
                overflow-y: auto;
                display: flex;
                flex-direction: column;
                gap: 16px;
            }
            .msg-group {
                display: flex;
                gap: 16px;
            }
            .msg-avatar {
                width: 40px;
                height: 40px;
                border-radius: 50%;
                background-color: var(--brand);
            }
            .msg-header { display: flex; gap: 8px; align-items: baseline; }
            .msg-author { font-weight: bold; color: white; }
            .msg-time { font-size: 12px; color: var(--text-muted); }
            .msg-text { margin-top: 4px; color: var(--text-normal); word-break: break-word; }

            .chat-input-container {
                padding: 0 16px 24px 16px;
            }
            .chat-input-box {
                background-color: #383a40;
                border-radius: 8px;
                padding: 12px 16px;
                display: flex;
                align-items: center;
            }
            .chat-input-box input {
                background: transparent;
                border: none;
                outline: none;
                color: white;
                width: 100%;
                font-size: 15px;
            }

            /* Панель Голосового Канала */
            .voice-panel {
                background-color: #111214;
                padding: 8px 12px;
                display: none;
                flex-direction: column;
                gap: 8px;
                border-bottom: 1px solid var(--border);
            }
            .voice-panel-info { display: flex; justify-content: space-between; align-items: center; }
            .voice-panel-actions { display: flex; gap: 8px; }

            /* 4. Правая колонка: Участники */
            .members-sidebar {
                width: 240px;
                background-color: var(--bg-secondary);
                padding: 16px 8px;
                display: flex;
                flex-direction: column;
                gap: 4px;
            }
            .member-item {
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 6px 8px;
                border-radius: 4px;
                cursor: pointer;
            }
            .member-item:hover { background-color: rgba(255,255,255,0.05); }
            .member-avatar-wrap { position: relative; }
            .member-avatar { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; }
            
            /* Voice Activity подсветка */
            .member-avatar.speaking {
                box-shadow: 0 0 0 3px var(--green);
            }

            /* Модалки */
            .modal-content {
                background-color: var(--bg-secondary);
                color: var(--text-normal);
                border: 1px solid var(--border);
            }
            .modal-header { border-bottom: 1px solid var(--border); }
            .modal-footer { border-top: 1px solid var(--border); }
            .form-control { background-color: #1e1f22; border: 1px solid #111214; color: white; }
            .form-control:focus { border-color: var(--brand); box-shadow: none; }
        </style>
    </head>
    <body>

        <!-- ЭКРАН АВТОРИЗАЦИИ -->
        <div id="auth-screen">
            <div class="auth-card">
                <i class="fa fa-gamepad" style="font-size: 64px; color: var(--brand);"></i>
                <h2 style="margin-top: 15px; font-weight: bold;">Добро пожаловать!</h2>
                <p style="color: var(--text-muted);">Введите никнейм для входа на сервер</p>

                <div class="input-group" style="margin-top: 30px;">
                    <input type="text" id="nick-input" class="form-control" placeholder="Ваш Никнейм..." style="height: 44px; font-size: 16px;">
                    <span class="input-group-btn">
                        <button class="btn btn-primary" id="btn-login" style="background-color: var(--brand); border: none; height: 44px; padding: 0 20px; font-weight: bold;">Войти</button>
                    </span>
                </div>
            </div>
        </div>

        <!-- ГЛАВНЫЙ ИНТЕРФЕЙС DISCORD -->
        <div id="app-layout">
            <!-- 1. Список серверов -->
            <div class="servers-sidebar">
                <div class="server-icon active" id="btn-home-server" title="Главная">
                    <i class="fa fa-discord"></i>
                </div>
                <div style="width: 32px; height: 2px; background-color: var(--bg-accent); margin: 4px 0;"></div>
                
                <div id="servers-container" style="display: flex; flex-direction: column; gap: 8px;">
                    <!-- Динамические иконки серверов -->
                </div>

                <div class="server-icon server-icon-add" id="btn-add-server" title="Создать / Войти на сервер">
                    <i class="fa fa-plus"></i>
                </div>
            </div>

            <!-- 2. Сайдбар каналов -->
            <div class="channels-sidebar">
                <div class="server-header">
                    <span id="current-server-name">Сервер</span>
                    <i class="fa fa-chevron-down" style="font-size: 12px;"></i>
                </div>

                <div class="channels-list">
                    <div class="channel-category">Текстовые каналы</div>
                    <div id="text-channels-list">
                        <!-- #general, #off-topic -->
                    </div>

                    <div class="channel-category">Голосовые каналы</div>
                    <div id="voice-channels-list">
                        <!-- 🔊 General Voice -->
                    </div>
                </div>

                <!-- Панель активного голоса -->
                <div class="voice-panel" id="active-voice-bar">
                    <div class="voice-panel-info">
                        <span style="color: var(--green); font-size: 12px; font-weight: bold;">
                            <i class="fa fa-signal"></i> Голос подключен / <span id="connected-voice-name">General</span>
                        </span>
                        <button class="btn btn-xs btn-danger" id="btn-disconnect-voice"><i class="fa fa-phone"></i></button>
                    </div>
                    <div class="voice-panel-actions">
                        <button class="btn btn-sm btn-default" id="btn-toggle-mic" style="flex:1;"><i class="fa fa-microphone"></i> Микрофон</button>
                        <button class="btn btn-sm btn-default" id="btn-toggle-deaf" style="flex:1;"><i class="fa fa-headphones"></i> Звук</button>
                    </div>
                </div>

                <!-- Профиль пользователя -->
                <div class="user-profile-bar">
                    <img src="" id="user-avatar-img" class="user-avatar-small" alt="">
                    <div class="user-info-text">
                        <div class="nick" id="user-nickname-display">User</div>
                        <div style="font-size: 11px; color: var(--text-muted);" id="user-id-display">#0000</div>
                    </div>
                    <a href="/logout" class="btn btn-link btn-xs" style="color: var(--text-muted);" title="Выйти"><i class="fa fa-sign-out"></i></a>
                </div>
            </div>

            <!-- 3. Главный чат -->
            <div class="main-content">
                <div class="chat-top-bar">
                    <i class="fa fa-hashtag" style="color: var(--text-muted);" id="channel-type-icon"></i>
                    <span id="current-channel-title">general</span>
                    <span style="font-size: 12px; color: var(--text-muted); font-weight: normal; margin-left: 10px;" id="server-code-badge">Код: ------</span>
                </div>

                <div class="chat-messages" id="messages-container">
                    <!-- Сообщения чата -->
                </div>

                <div class="chat-input-container">
                    <div class="chat-input-box">
                        <input type="text" id="chat-input" placeholder="Написать в #general..." autocomplete="off">
                    </div>
                </div>
            </div>

            <!-- 4. Список участников -->
            <div class="members-sidebar">
                <div class="channel-category" style="margin-top: 0;">В сети — <span id="members-count">0</span></div>
                <div id="members-list-container">
                    <!-- Участники -->
                </div>
            </div>
        </div>

        <!-- Модалка создания/входа на сервер -->
        <div id="serverModal" class="modal fade" role="dialog">
            <div class="modal-dialog modal-sm">
                <div class="modal-content">
                    <div class="modal-header">
                        <button type="button" class="close" data-dismiss="modal">&times;</button>
                        <h4 class="modal-title">Сервер</h4>
                    </div>
                    <div class="modal-body">
                        <ul class="nav nav-tabs nav-justified" style="margin-bottom: 15px;">
                            <li class="active"><a data-toggle="tab" href="#tab-join-s">Войти</a></li>
                            <li><a data-toggle="tab" href="#tab-create-s">Создать</a></li>
                        </ul>
                        <div class="tab-content">
                            <div id="tab-join-s" class="tab-pane fade in active">
                                <div class="form-group">
                                    <label>Код сервера (6 символов):</label>
                                    <input type="text" id="join-server-code" class="form-control text-uppercase" maxlength="6" placeholder="X Y Z 1 2 3">
                                </div>
                                <button class="btn btn-success btn-block" id="btn-submit-join">Присоединиться</button>
                            </div>
                            <div id="tab-create-s" class="tab-pane fade">
                                <div class="form-group">
                                    <label>Название сервера:</label>
                                    <input type="text" id="create-server-name" class="form-control" placeholder="Мой Супер Сервер">
                                </div>
                                <button class="btn btn-primary btn-block" id="btn-submit-create">Создать сервер</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <script>
            let currentUser = null;
            let currentServerCode = null;
            let currentChannelId = 'general';
            let activeVoiceChannelId = null;
            let socket = null;

            // WebRTC Voice переменных
            let localStream = null;
            let peers = {}; // target_id: RTCPeerConnection
            let isMuted = false;
            let isDeafened = false;
            let audioContext = null;
            let analyser = null;
            let microphone = null;

            const rtcConfig = { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] };

            // 1. Проверка авторизации
            async function checkAuth() {
                try {
                    const res = await fetch('/api/me');
                    const data = await res.json();
                    
                    if (data.authenticated) {
                        currentUser = data.user;
                        $('#auth-screen').hide();
                        $('#app-layout').css('display', 'flex');
                        
                        $('#user-nickname-display').text(currentUser.nickname);
                        $('#user-id-display').text('#' + currentUser.id.substring(0, 4));
                        $('#user-avatar-img').attr('src', currentUser.picture || `https://api.dicebear.com/7.x/bottts/svg?seed=${currentUser.nickname}`);
                    } else {
                        $('#auth-screen').show();
                        $('#app-layout').hide();
                    }
                } catch(e) { console.error(e); }
            }

            // Вход по никнейму
            $('#btn-login').click(async function() {
                const nick = $('#nick-input').val().trim();
                if(!nick) return;
                const res = await fetch('/api/login?nickname=' + encodeURIComponent(nick), { method: 'POST' });
                if (res.ok) checkAuth();
            });

            $('#nick-input').keypress(function(e) {
                if (e.which === 13) $('#btn-login').click();
            });

            // 2. Управление серверами
            $('#btn-add-server').click(function() { $('#serverModal').modal('show'); });

            $('#btn-submit-create').click(async function() {
                const name = $('#create-server-name').val().trim();
                if(!name) return;

                const res = await fetch('/api/create-server', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ name: name, max_users: 50 })
                });
                const data = await res.json();
                if (data.server_code) {
                    $('#serverModal').modal('hide');
                    connectToServer(data.server_code);
                }
            });

            $('#btn-submit-join').click(async function() {
                const code = $('#join-server-code').val().trim().toUpperCase();
                if(code.length !== 6) return;

                const res = await fetch('/api/server/' + code);
                if(res.ok) {
                    $('#serverModal').modal('hide');
                    connectToServer(code);
                } else {
                    alert('Сервер не найден!');
                }
            });

            // 3. WebSocket Соединение с сервером
            function connectToServer(code) {
                if(socket) socket.close();

                currentServerCode = code;
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                socket = new WebSocket(`${protocol}//${window.location.host}/ws/${code}`);

                socket.onopen = function() {
                    socket.send(JSON.stringify({
                        type: 'init',
                        user_id: currentUser.id,
                        nickname: currentUser.nickname,
                        picture: currentUser.picture
                    }));
                };

                socket.onmessage = function(event) {
                    const data = JSON.parse(event.data);

                    if (data.type === 'init_success') {
                        $('#server-code-badge').text('Код инвайта: ' + code);
                        renderChannels(data.channels);
                        loadChatHistory(data.history[currentChannelId] || []);
                    }
                    else if (data.type === 'new_message') {
                        if (data.message.channel_id === currentChannelId) {
                            appendChatMessage(data.message);
                        }
                    }
                    else if (data.type === 'members_update') {
                        renderMembers(data.members);
                    }
                    // ====== WebRTC Voice сигналы ======
                    else if (data.type === 'user_joined_voice') {
                        if (activeVoiceChannelId && data.channel_id === activeVoiceChannelId && data.user_id !== currentUser.id) {
                            // Создаем Offer для нового участника голоса
                            createVoiceOffer(data.user_id);
                        }
                    }
                    else if (data.type === 'voice_offer') {
                        handleVoiceOffer(data.sender_id, data.sdp);
                    }
                    else if (data.type === 'voice_answer') {
                        if (peers[data.sender_id]) {
                            peers[data.sender_id].setRemoteDescription(new RTCSessionDescription(data.sdp));
                        }
                    }
                    else if (data.type === 'voice_ice') {
                        if (peers[data.sender_id]) {
                            peers[data.sender_id].addIceCandidate(new RTCIceCandidate(data.candidate)).catch(e => console.error(e));
                        }
                    }
                    else if (data.type === 'voice_state_update') {
                        updateVoiceActivityIndicator(data.user_id, data.speaking);
                    }
                };
            }

            // Рендер списков каналов
            function renderChannels(channels) {
                const textList = $('#text-channels-list').empty();
                const voiceList = $('#voice-channels-list').empty();

                channels.text.forEach(ch => {
                    const activeClass = ch.id === currentChannelId ? 'active' : '';
                    textList.append(`
                        <div class="channel-item ${activeClass}" onclick="switchChannel('${ch.id}', '${ch.name}')">
                            <i class="fa fa-hashtag"></i> ${ch.name}
                        </div>
                    `);
                });

                channels.voice.forEach(ch => {
                    voiceList.append(`
                        <div class="channel-item" onclick="joinVoiceChannel('${ch.id}', '${ch.name}')">
                            <i class="fa fa-volume-up"></i> ${ch.name}
                        </div>
                    `);
                });
            }

            function switchChannel(chId, chName) {
                currentChannelId = chId;
                $('#current-channel-title').text(chName);
                $('#text-channels-list .channel-item').removeClass('active');
                event.currentTarget.classList.add('active');
                $('#messages-container').empty();
            }

            // Отправка сообщений
            $('#chat-input').keypress(function(e) {
                if (e.which === 13) {
                    const txt = $(this).val().trim();
                    if (txt && socket) {
                        socket.send(JSON.stringify({
                            type: 'chat_message',
                            channel_id: currentChannelId,
                            text: txt
                        }));
                        $(this).val('');
                    }
                }
            });

            function appendChatMessage(msg) {
                const container = $('#messages-container');
                const avatar = msg.sender_picture || `https://api.dicebear.com/7.x/bottts/svg?seed=${msg.sender_name}`;
                container.append(`
                    <div class="msg-group">
                        <img src="${avatar}" class="msg-avatar">
                        <div>
                            <div class="msg-header">
                                <span class="msg-author">${msg.sender_name}</span>
                                <span class="msg-time">${msg.time}</span>
                            </div>
                            <div class="msg-text">${msg.text}</div>
                        </div>
                    </div>
                `);
                container.scrollTop(container[0].scrollHeight);
            }

            function loadChatHistory(history) {
                $('#messages-container').empty();
                history.forEach(appendChatMessage);
            }

            function renderMembers(members) {
                $('#members-count').text(members.length);
                const container = $('#members-list-container').empty();
                members.forEach(m => {
                    const avatar = m.picture || `https://api.dicebear.com/7.x/bottts/svg?seed=${m.nickname}`;
                    container.append(`
                        <div class="member-item">
                            <div class="member-avatar-wrap">
                                <img src="${avatar}" class="member-avatar" id="avatar-user-${m.id}">
                            </div>
                            <span style="font-weight: 500;">${m.nickname}</span>
                        </div>
                    `);
                });
            }

            // ====== WebRTC Voice Меш ======
            async function joinVoiceChannel(vId, vName) {
                if (activeVoiceChannelId === vId) return;
                
                leaveVoiceChannel();

                try {
                    localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
                    activeVoiceChannelId = vId;
                    
                    $('#connected-voice-name').text(vName);
                    $('#active-voice-bar').css('display', 'flex');

                    // Настройка детекции голоса (Speaking indicator)
                    setupVoiceDetection();

                    socket.send(JSON.stringify({
                        type: 'join_voice',
                        channel_id: vId
                    }));
                } catch(err) {
                    alert('Не удалось получить доступ к микрофону!');
                }
            }

            function leaveVoiceChannel() {
                if (!activeVoiceChannelId) return;

                if (localStream) {
                    localStream.getTracks().forEach(t => t.stop());
                    localStream = null;
                }

                Object.values(peers).forEach(pc => pc.close());
                peers = {};

                socket.send(JSON.stringify({ type: 'leave_voice' }));
                activeVoiceChannelId = null;
                $('#active-voice-bar').hide();
            }

            $('#btn-disconnect-voice').click(leaveVoiceChannel);

            async function createVoiceOffer(targetId) {
                const pc = new RTCPeerConnection(rtcConfig);
                peers[targetId] = pc;

                localStream.getTracks().forEach(track => pc.addTrack(track, localStream));

                pc.onicecandidate = e => {
                    if (e.candidate) {
                        socket.send(JSON.stringify({
                            type: 'voice_ice',
                            target_id: targetId,
                            candidate: e.candidate
                        }));
                    }
                };

                pc.ontrack = e => playRemoteAudio(targetId, e.streams[0]);

                const offer = await pc.createOffer();
                await pc.setLocalDescription(offer);

                socket.send(JSON.stringify({
                    type: 'voice_offer',
                    target_id: targetId,
                    sdp: pc.localDescription
                }));
            }

            async function handleVoiceOffer(senderId, sdp) {
                const pc = new RTCPeerConnection(rtcConfig);
                peers[senderId] = pc;

                localStream.getTracks().forEach(track => pc.addTrack(track, localStream));

                pc.onicecandidate = e => {
                    if (e.candidate) {
                        socket.send(JSON.stringify({
                            type: 'voice_ice',
                            target_id: senderId,
                            candidate: e.candidate
                        }));
                    }
                };

                pc.ontrack = e => playRemoteAudio(senderId, e.streams[0]);

                await pc.setRemoteDescription(new RTCSessionDescription(sdp));
                const answer = await pc.createAnswer();
                await pc.setLocalDescription(answer);

                socket.send(JSON.stringify({
                    type: 'voice_answer',
                    target_id: senderId,
                    sdp: pc.localDescription
                }));
            }

            function playRemoteAudio(userId, stream) {
                let audio = document.getElementById('audio-' + userId);
                if (!audio) {
                    audio = document.createElement('audio');
                    audio.id = 'audio-' + userId;
                    audio.autoplay = true;
                    document.body.appendChild(audio);
                }
                audio.srcObject = stream;
            }

            // Детекция активности голоса (Voice Activity Detection)
            function setupVoiceDetection() {
                audioContext = new (window.AudioContext || window.webkitAudioContext)();
                analyser = audioContext.createAnalyser();
                microphone = audioContext.createMediaStreamSource(localStream);
                microphone.connect(analyser);
                analyser.fftSize = 256;

                const bufferLength = analyser.frequencyBinCount;
                const dataArray = new Uint8Array(bufferLength);

                let isSpeaking = false;
                setInterval(() => {
                    if (!localStream || isMuted) return;
                    analyser.getByteFrequencyData(dataArray);
                    let sum = 0;
                    for (let i = 0; i < bufferLength; i++) sum += dataArray[i];
                    let average = sum / bufferLength;

                    let speakingNow = average > 15;
                    if (speakingNow !== isSpeaking) {
                        isSpeaking = speakingNow;
                        socket.send(JSON.stringify({ type: 'voice_state', speaking: isSpeaking }));
                        updateVoiceActivityIndicator(currentUser.id, isSpeaking);
                    }
                }, 100);
            }

            function updateVoiceActivityIndicator(userId, speaking) {
                const el = $('#avatar-user-' + userId);
                if (speaking) el.addClass('speaking');
                else el.removeClass('speaking');
            }

            $('#btn-toggle-mic').click(function() {
                if(!localStream) return;
                isMuted = !isMuted;
                localStream.getAudioTracks()[0].enabled = !isMuted;
                $(this).toggleClass('btn-danger', isMuted);
            });

            checkAuth();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    print("Запуск Discord RAM Сервера...")
    print("Откройте в браузере: http://127.0.0.1:8000")
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
