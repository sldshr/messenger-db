import os
import uuid
import json
import datetime
from typing import Dict, Optional, List, Set, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI()

sessions_db: Dict[str, dict] = {}
BANNED_USERS: Set[str] = set()
CONNECTIONS: Dict[WebSocket, dict] = {}

CHANNELS = [
    # 📌 ИНФОРМАЦИЯ
    {"id": "rules", "name": "правила", "category": "📌 ИНФОРМАЦИЯ"},
    {"id": "announcements", "name": "объявления", "category": "📌 ИНФОРМАЦИЯ"},
    {"id": "faq", "name": "faq", "category": "📌 ИНФОРМАЦИЯ"},
    
    # 💬 ТЕКСТОВЫЕ КАНАЛЫ
    {"id": "general", "name": "основной", "category": "💬 ТЕКСТОВЫЕ КАНАЛЫ"},
    {"id": "off-topic", "name": "флудилка", "category": "💬 ТЕКСТОВЫЕ КАНАЛЫ"},
    {"id": "memes", "name": "мемы", "category": "💬 ТЕКСТОВЫЕ КАНАЛЫ"},
    {"id": "media", "name": "медиа", "category": "💬 ТЕКСТОВЫЕ КАНАЛЫ"},
    {"id": "pets", "name": "питомцы", "category": "💬 ТЕКСТОВЫЕ КАНАЛЫ"},
    {"id": "food", "name": "кулинария", "category": "💬 ТЕКСТОВЫЕ КАНАЛЫ"},
    
    # 🎮 ИГРЫ
    {"id": "gaming", "name": "игровой-чат", "category": "🎮 ИГРЫ"},
    {"id": "find-party", "name": "поиск-пати", "category": "🎮 ИГРЫ"},
    {"id": "clips", "name": "клипы-хайлайты", "category": "🎮 ИГРЫ"},
    {"id": "minecraft", "name": "minecraft", "category": "🎮 ИГРЫ"},
    {"id": "dota2", "name": "dota-2", "category": "🎮 ИГРЫ"},
    {"id": "cs2", "name": "cs2", "category": "🎮 ИГРЫ"},

    # 💻 ТЕХНОЛОГИИ
    {"id": "tech", "name": "железо-софт", "category": "💻 ТЕХНОЛОГИИ"},
    {"id": "coding", "name": "программирование", "category": "💻 ТЕХНОЛОГИИ"},
    {"id": "ai", "name": "нейросети", "category": "💻 ТЕХНОЛОГИИ"},

    # 🎨 ТВОРЧЕСТВО
    {"id": "art", "name": "арт-дизайн", "category": "🎨 ТВОРЧЕСТВО"},
    {"id": "music", "name": "музыка", "category": "🎨 ТВОРЧЕСТВО"}
]

MESSAGES: Dict[str, List[dict]] = {ch["id"]: [] for ch in CHANNELS}

class AdminLoginReq(BaseModel):
    username: str
    password: str

class BroadcastReq(BaseModel):
    text: str
    channel_id: Optional[str] = None # Если None - во все каналы

class UserActionReq(BaseModel):
    user_id: str

def get_current_user(request: Request) -> Optional[dict]:
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions_db:
        user = sessions_db[session_id]
        if user["id"] in BANNED_USERS:
            return None # Забанен
        return user
    return None

async def broadcast_to_all(message: dict):
    dead = []
    for ws in CONNECTIONS.keys():
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in CONNECTIONS:
            del CONNECTIONS[ws]

async def broadcast_members_update():
    members = [
        {"id": info["id"], "nickname": info["nickname"], "picture": info["picture"]}
        for info in CONNECTIONS.values()
    ]
    await broadcast_to_all({"type": "members_update", "members": members})

async def disconnect_user(user_id: str, reason: str = "Вы были отключены администратором."):
    """Принудительно отключает все WebSocket сессии конкретного пользователя."""
    to_disconnect = []
    for ws, info in CONNECTIONS.items():
        if info["id"] == user_id:
            to_disconnect.append(ws)
    
    for ws in to_disconnect:
        try:
            await ws.send_json({"type": "force_disconnect", "reason": reason})
            await ws.close()
        except:
            pass
        if ws in CONNECTIONS:
            del CONNECTIONS[ws]
    await broadcast_members_update()

@app.get("/api/me")
async def get_me(request: Request):
    user = get_current_user(request)
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "user": user}

@app.post("/api/login")
async def login(nickname: str):
    nickname = nickname.strip()
    if not nickname:
        raise HTTPException(status_code=400, detail="Никнейм не может быть пустым")
    
    session_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4()) # В реальном приложении ID генерируется на основе уникальных данных
    avatar_url = f"https://api.dicebear.com/7.x/bottts/svg?seed={nickname}"
    
    user_data = {
        "id": user_id,
        "nickname": nickname,
        "picture": avatar_url
    }
    sessions_db[session_id] = user_data

    response = JSONResponse(content={"status": "ok", "user": user_data})
    response.set_cookie(key="session_id", value=session_id, httponly=True, max_age=86400 * 30, samesite="lax")
    return response

@app.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id in sessions_db:
        del sessions_db[session_id]
    response = RedirectResponse("/")
    response.delete_cookie("session_id")
    return response

def is_admin(request: Request) -> bool:
    return request.cookies.get("admin_auth") == "true"

@app.post("/admin/login")
async def admin_login_api(data: AdminLoginReq):
    if data.username == "admin" and data.password == "admin":
        response = JSONResponse({"status": "ok"})
        response.set_cookie("admin_auth", "true", max_age=86400, httponly=True)
        return response
    raise HTTPException(status_code=401, detail="Неверные данные")

@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin")
    response.delete_cookie("admin_auth")
    return response

@app.get("/admin/api/data")
async def admin_get_data(request: Request):
    """Возвращает данные для админ-панели."""
    if not is_admin(request): raise HTTPException(status_code=401)
    
    users_list = []
    for ws, info in CONNECTIONS.items():
        users_list.append({"id": info["id"], "nickname": info["nickname"]})
        
    banned_list = list(BANNED_USERS)
    
    return {
        "stats": {
            "online": len(CONNECTIONS),
            "sessions": len(sessions_db),
            "channels": len(CHANNELS)
        },
        "channels": CHANNELS,
        "online_users": users_list,
        "banned_users": banned_list
    }

@app.post("/admin/api/clear_channel")
async def admin_clear_ch(req: BroadcastReq, request: Request):
    if not is_admin(request): raise HTTPException(status_code=401)
    ch_id = req.channel_id
    if ch_id in MESSAGES:
        MESSAGES[ch_id] = []
        await broadcast_to_all({"type": "channel_cleared", "channel_id": ch_id})
    return {"status": "ok"}

@app.post("/admin/api/broadcast")
async def admin_send_broadcast(req: BroadcastReq, request: Request):
    """Отправка системного сообщения."""
    if not is_admin(request): raise HTTPException(status_code=401)
    
    targets = [req.channel_id] if req.channel_id and req.channel_id != "all" else [ch["id"] for ch in CHANNELS]
    
    msg_obj = {
        "id": str(uuid.uuid4()),
        "sender_id": "system",
        "sender_name": "Система",
        "sender_picture": "",
        "text": req.text,
        "time": datetime.datetime.now().strftime("%H:%M"),
        "timestamp": int(datetime.datetime.now().timestamp()),
        "is_system": True
    }
    
    for ch_id in targets:
        if ch_id in MESSAGES:
            msg_obj["channel_id"] = ch_id
            MESSAGES[ch_id].append(msg_obj)
            if len(MESSAGES[ch_id]) > 100: MESSAGES[ch_id].pop(0)
            
    await broadcast_to_all({
        "type": "new_message",
        "message": msg_obj,
        "global": req.channel_id == "all"
    })
    return {"status": "ok"}

@app.post("/admin/api/kick")
async def admin_kick(req: UserActionReq, request: Request):
    if not is_admin(request): raise HTTPException(status_code=401)
    await disconnect_user(req.user_id, "Вы были кикнуты администратором.")
    return {"status": "ok"}

@app.post("/admin/api/ban")
async def admin_ban(req: UserActionReq, request: Request):
    if not is_admin(request): raise HTTPException(status_code=401)
    BANNED_USERS.add(req.user_id)
    await disconnect_user(req.user_id, "Ваш аккаунт был заблокирован.")
    return {"status": "ok"}

@app.post("/admin/api/unban")
async def admin_unban(req: UserActionReq, request: Request):
    if not is_admin(request): raise HTTPException(status_code=401)
    if req.user_id in BANNED_USERS:
        BANNED_USERS.remove(req.user_id)
    return {"status": "ok"}

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_page(request: Request):
    if not is_admin(request):
        return HTMLResponse(r"""
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8"><title>Admin Login</title>
            <style>
                body { background-color: #1e1f22; color: #dbdee1; font-family: sans-serif; display: flex; height: 100vh; align-items: center; justify-content: center; margin: 0;}
                .card { background-color: #2b2d31; padding: 40px; border-radius: 12px; text-align: center; width: 320px; box-shadow: 0 4px 15px rgba(0,0,0,0.2); }
                input { margin-bottom: 15px; background: #1e1f22; border: 1px solid #111214; color: white; padding: 12px; width: 100%; border-radius: 4px; box-sizing: border-box; }
                button { background: #5865f2; border: none; color: white; padding: 12px; width: 100%; border-radius: 4px; font-weight: bold; cursor: pointer; }
                button:hover { background: #4752c4; }
            </style>
        </head>
        <body>
            <div class="card">
                <h3 style="margin-top:0;">Admin Access</h3>
                <input type="text" id="u" placeholder="Логин (admin)">
                <input type="password" id="p" placeholder="Пароль (admin)">
                <button onclick="login()">Войти</button>
            </div>
            <script>
                async function login() {
                    const res = await fetch('/admin/login', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({username: document.getElementById('u').value, password: document.getElementById('p').value})
                    });
                    if(res.ok) window.location.reload();
                    else alert('Неверные данные!');
                }
            </script>
        </body>
        </html>
        """)
    
    return HTMLResponse(r"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8"><title>Admin Dashboard</title>
        <style>
            :root { --bg-base: #1e1f22; --bg-panel: #2b2d31; --brand: #5865f2; --text: #dbdee1; --text-muted: #949ba4; --danger: #da373c; --success: #23a55a; }
            body { background-color: var(--bg-base); color: var(--text); font-family: sans-serif; margin: 0; display: flex; height: 100vh; overflow: hidden; }
            .sidebar { width: 260px; background-color: var(--bg-panel); display: flex; flex-direction: column; }
            .sidebar-header { padding: 20px; font-weight: bold; font-size: 18px; border-bottom: 1px solid #1e1f22; display: flex; justify-content: space-between; align-items: center;}
            .menu-item { padding: 15px 20px; cursor: pointer; color: var(--text-muted); font-weight: 500; }
            .menu-item:hover { background: rgba(255,255,255,0.05); color: white; }
            .menu-item.active { background: rgba(255,255,255,0.1); color: white; }
            
            .content { flex: 1; padding: 40px; overflow-y: auto; }
            .card { background: var(--bg-panel); padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }
            .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
            
            h2, h3 { margin-top: 0; color: white; }
            .btn { display: inline-block; padding: 8px 16px; border-radius: 4px; border: none; cursor: pointer; font-weight: bold; color: white; }
            .btn-brand { background: var(--brand); }
            .btn-brand:hover { background: #4752c4; }
            .btn-danger { background: var(--danger); }
            .btn-danger:hover { background: #a1282c; }
            .btn-sm { padding: 5px 10px; font-size: 12px; }
            
            table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            th, td { padding: 12px; text-align: left; border-bottom: 1px solid #3f4147; }
            th { color: var(--text-muted); text-transform: uppercase; font-size: 12px; }
            
            input, select { background: var(--bg-base); border: 1px solid #111214; color: white; padding: 10px; border-radius: 4px; width: 100%; box-sizing: border-box; margin-bottom: 10px;}
            
            .tab-content { display: none; }
            .tab-content.active { display: block; }
        </style>
    </head>
    <body>
        <div class="sidebar">
            <div class="sidebar-header">
                Админ Панель
                <a href="/" target="_blank" style="color: var(--text-muted); text-decoration: none;" title="В чат">↗</a>
            </div>
            <div class="menu-item active" onclick="switchTab('dashboard', this)">Обзор</div>
            <div class="menu-item" onclick="switchTab('users', this)">Пользователи</div>
            <div class="menu-item" onclick="switchTab('channels', this)">Каналы & Объявления</div>
            <div style="flex:1;"></div>
            <a href="/admin/logout" class="menu-item" style="color: var(--danger); text-decoration: none;">Выход</a>
        </div>
        
        <div class="content">
            <!-- DASHBOARD -->
            <div id="dashboard" class="tab-content active">
                <h2>Обзор сервера</h2>
                <div class="grid-3">
                    <div class="card">
                        <h3>Онлайн</h3>
                        <div style="font-size: 32px; font-weight: bold; color: var(--success);" id="stat-online">0</div>
                    </div>
                    <div class="card">
                        <h3>Всего сессий</h3>
                        <div style="font-size: 32px; font-weight: bold;" id="stat-sessions">0</div>
                    </div>
                    <div class="card">
                        <h3>Каналов</h3>
                        <div style="font-size: 32px; font-weight: bold;" id="stat-channels">0</div>
                    </div>
                </div>
                <div class="card">
                    <h3>Быстрая рассылка (System Broadcast)</h3>
                    <input type="text" id="br-text" placeholder="Введите важное объявление для всех...">
                    <button class="btn btn-brand" onclick="sendBroadcast('all')">Отправить всем</button>
                </div>
            </div>
            
            <!-- USERS -->
            <div id="users" class="tab-content">
                <h2>Управление пользователями</h2>
                <div class="card">
                    <h3>Сейчас онлайн</h3>
                    <table>
                        <thead><tr><th>ID</th><th>Никнейм</th><th>Действия</th></tr></thead>
                        <tbody id="table-online"></tbody>
                    </table>
                </div>
                <div class="card">
                    <h3>Заблокированные (Бан-лист)</h3>
                    <table>
                        <thead><tr><th>ID</th><th>Действия</th></tr></thead>
                        <tbody id="table-banned"></tbody>
                    </table>
                </div>
            </div>
            
            <!-- CHANNELS -->
            <div id="channels" class="tab-content">
                <h2>Каналы</h2>
                <div class="card">
                    <table style="max-height: 500px; display: block; overflow-y: auto;">
                        <tbody id="table-channels" style="width: 100%; display: table;"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <script>
            function switchTab(tabId, el) {
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.menu-item').forEach(t => t.classList.remove('active'));
                document.getElementById(tabId).classList.add('active');
                el.classList.add('active');
            }

            async function loadData() {
                const res = await fetch('/admin/api/data');
                if(!res.ok) return;
                const data = await res.json();
                
                // Stats
                document.getElementById('stat-online').innerText = data.stats.online;
                document.getElementById('stat-sessions').innerText = data.stats.sessions;
                document.getElementById('stat-channels').innerText = data.stats.channels;
                
                // Users
                const onlineHtml = data.online_users.map(u => `
                    <tr>
                        <td style="font-family: monospace; font-size: 11px;">${u.id}</td>
                        <td style="font-weight: bold;">${u.nickname}</td>
                        <td>
                            <button class="btn btn-brand btn-sm" onclick="action('kick', '${u.id}')">Кик</button>
                            <button class="btn btn-danger btn-sm" onclick="action('ban', '${u.id}')">Бан</button>
                        </td>
                    </tr>
                `).join('');
                document.getElementById('table-online').innerHTML = onlineHtml || '<tr><td colspan="3">Нет пользователей онлайн</td></tr>';
                
                // Banned
                const bannedHtml = data.banned_users.map(id => `
                    <tr>
                        <td style="font-family: monospace; font-size: 11px;">${id}</td>
                        <td><button class="btn btn-brand btn-sm" onclick="action('unban', '${id}')">Разбанить</button></td>
                    </tr>
                `).join('');
                document.getElementById('table-banned').innerHTML = bannedHtml || '<tr><td colspan="2">Бан-лист пуст</td></tr>';
                
                // Channels
                const chHtml = data.channels.map(ch => `
                    <tr>
                        <td style="color: var(--text-muted); font-size: 11px;">${ch.category}</td>
                        <td style="font-weight: bold;">#${ch.name}</td>
                        <td style="text-align: right;">
                            <button class="btn btn-brand btn-sm" onclick="promptBroadcast('${ch.id}', '${ch.name}')">Объявление</button>
                            <button class="btn btn-danger btn-sm" onclick="clearChannel('${ch.id}')">Очистить историю</button>
                        </td>
                    </tr>
                `).join('');
                document.getElementById('table-channels').innerHTML = chHtml;
            }

            async function action(type, userId) {
                if(!confirm(`Вы уверены, что хотите применить ${type} к пользователю?`)) return;
                await fetch(`/admin/api/${type}`, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({user_id: userId})
                });
                loadData();
            }

            async function clearChannel(chId) {
                if(!confirm('Очистить всю историю этого канала?')) return;
                await fetch('/admin/api/clear_channel', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({channel_id: chId, text: ""})
                });
            }

            async function sendBroadcast(channelId, text = null) {
                const msg = text || document.getElementById('br-text').value;
                if(!msg) return;
                await fetch('/admin/api/broadcast', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({channel_id: channelId, text: msg})
                });
                document.getElementById('br-text').value = '';
                alert('Отправлено!');
            }
            
            function promptBroadcast(chId, chName) {
                const msg = prompt(`Введите системное сообщение для канала #${chName}:`);
                if(msg) sendBroadcast(chId, msg);
            }

            // Auto-refresh
            loadData();
            setInterval(loadData, 5000);
        </script>
    </body>
    </html>
    """)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    client_id = str(uuid.uuid4())
    user_info = {"id": client_id, "nickname": "Guest", "picture": ""}

    try:
        while True:
            raw_text = await websocket.receive_text()
            data = json.loads(raw_text)
            msg_type = data.get("type")

            if msg_type == "init":
                user_id = data.get("user_id", client_id)
                # Проверка на бан при инициализации сокета
                if user_id in BANNED_USERS:
                    await websocket.send_json({"type": "force_disconnect", "reason": "Вы забанены на этом сервере."})
                    await websocket.close()
                    return

                user_info["id"] = user_id
                user_info["nickname"] = data.get("nickname", "Guest")
                user_info["picture"] = data.get("picture", "")
                
                CONNECTIONS[websocket] = user_info
                
                await websocket.send_json({
                    "type": "init_success",
                    "channels": {"text": CHANNELS},
                    "history": MESSAGES
                })
                await broadcast_members_update()

            elif msg_type == "chat_message":
                # Дополнительная проверка на бан при отправке
                if user_info["id"] in BANNED_USERS:
                    await websocket.close()
                    return

                channel_id = data.get("channel_id", "general")
                text = data.get("text", "").strip()
                if text and channel_id in MESSAGES:
                    msg_obj = {
                        "id": str(uuid.uuid4()),
                        "sender_id": user_info["id"],
                        "sender_name": user_info["nickname"],
                        "sender_picture": user_info["picture"],
                        "text": text,
                        "time": datetime.datetime.now().strftime("%H:%M"),
                        "timestamp": int(datetime.datetime.now().timestamp()),
                        "is_system": False
                    }
                    MESSAGES[channel_id].append(msg_obj)
                    if len(MESSAGES[channel_id]) > 100:
                        MESSAGES[channel_id].pop(0)

                    await broadcast_to_all({
                        "type": "new_message",
                        "message": msg_obj
                    })

    except WebSocketDisconnect:
        if websocket in CONNECTIONS:
            del CONNECTIONS[websocket]
            await broadcast_members_update()

@app.get("/", response_class=HTMLResponse)
async def get_index():
    # Используем сырую строку (raw string) 'r"""' чтобы Python не ругался на '\*' в регулярках JS
    html_content = r"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Global Chat Server</title>
        <link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.4.1/css/bootstrap.min.css">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css">
        <script src="https://ajax.googleapis.com/ajax/libs/jquery/3.5.1/jquery.min.js"></script>
        
        <style>
            :root {
                --bg-secondary: #2b2d31;
                --bg-primary: #313338;
                --bg-accent: #383a40;
                --text-normal: #dbdee1;
                --text-muted: #949ba4;
                --brand: #5865f2;
                --border: #1e1f22;
                --system: #faa61a;
            }

            * { box-sizing: border-box; }
            body { background-color: var(--bg-primary); color: var(--text-normal); font-family: 'gg sans', 'Noto Sans', Helvetica, Arial, sans-serif; margin: 0; padding: 0; height: 100vh; overflow: hidden; }

            #auth-screen { display: flex; height: 100vh; justify-content: center; align-items: center; background: linear-gradient(135deg, #1e1f22 0%, #2b2d31 100%); }
            .auth-card { background-color: var(--bg-secondary); padding: 40px; border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.5); width: 100%; max-width: 440px; text-align: center; }

            #app-layout { display: none; height: 100vh; width: 100vw; flex-direction: row; }

            .channels-sidebar { width: 240px; background-color: var(--bg-secondary); display: flex; flex-direction: column; }
            .server-header { height: 48px; padding: 0 16px; border-bottom: 1px solid rgba(0,0,0,0.2); display: flex; align-items: center; justify-content: space-between; font-weight: bold; font-size: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.2); }
            .channels-list { flex: 1; overflow-y: auto; padding: 12px 8px; }
            .channel-item { display: flex; align-items: center; padding: 8px 10px; border-radius: 4px; color: var(--text-muted); cursor: pointer; margin-bottom: 2px; font-size: 15px; }
            .channel-item i { margin-right: 8px; width: 18px; text-align: center; }
            .channel-item:hover { background-color: rgba(255,255,255,0.05); color: var(--text-normal); }
            .channel-item.active { background-color: var(--bg-accent); color: white; }

            .user-profile-bar { height: 52px; background-color: #232428; padding: 0 8px; display: flex; align-items: center; justify-content: space-between; }
            .user-avatar-small { width: 32px; height: 32px; border-radius: 50%; margin-right: 8px; object-fit: cover; }
            .user-info-text { flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
            .user-info-text .nick { font-weight: bold; font-size: 14px; }

            .main-content { flex: 1; display: flex; flex-direction: column; background-color: var(--bg-primary); }
            .chat-top-bar { height: 48px; padding: 0 16px; border-bottom: 1px solid rgba(0,0,0,0.2); display: flex; align-items: center; gap: 8px; font-weight: bold; font-size: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.2); }
            
            .chat-messages { flex: 1; padding: 16px 0; overflow-y: auto; display: flex; flex-direction: column; scroll-behavior: auto; }
            .msg-item { display: flex; padding: 2px 16px; margin-top: 14px; position: relative; min-height: 40px; }
            .msg-item:hover { background-color: rgba(255, 255, 255, 0.03); }
            .msg-item.grouped { margin-top: 0; padding-top: 1px; padding-bottom: 1px; min-height: 22px; }
            
            .msg-avatar-container { width: 40px; height: 40px; margin-right: 16px; flex-shrink: 0; position: relative; }
            .msg-avatar { width: 40px; height: 40px; border-radius: 50%; background-color: var(--brand); flex-shrink: 0; cursor: pointer; object-fit: cover; }
            .msg-body { flex: 1; display: flex; flex-direction: column; min-width: 0; }
            
            .msg-item.grouped .msg-body { margin-left: 56px; }
            
            .msg-header { display: flex; gap: 8px; align-items: baseline; margin-bottom: 2px; line-height: 1.25; }
            .msg-author { font-weight: 600; color: white; cursor: pointer; font-size: 15px; }
            .msg-author:hover { text-decoration: underline; }
            .msg-time { font-size: 12px; color: var(--text-muted); }
            .msg-text { color: var(--text-normal); word-break: break-word; line-height: 1.375; font-size: 15px; white-space: pre-wrap; }
            
            .msg-time-hover { position: absolute; left: 0px; top: 2px; width: 52px; text-align: right; font-size: 10px; color: var(--text-muted); display: none; line-height: 1.4; user-select: none; }
            .msg-item.grouped:hover .msg-time-hover { display: block; }
            
            /* Markdown styles */
            .md-bold { font-weight: bold; }
            .md-italic { font-style: italic; }
            .md-strike { text-decoration: line-through; }
            .md-code { background: #1e1f22; font-family: monospace; padding: 2px 4px; border-radius: 3px; font-size: 13px; }

            .category-header { font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; margin: 16px 0 4px 8px; letter-spacing: 0.5px; display: flex; align-items: center; user-select: none; }
            
            .status-wrapper { position: relative; display: inline-flex; }
            .status-dot { position: absolute; bottom: -2px; right: -2px; width: 12px; height: 12px; background-color: #23a55a; border-radius: 50%; border: 3px solid var(--bg-secondary); }

            .chat-input-container { padding: 0 16px 24px 16px; }
            .chat-input-box { background-color: #383a40; border-radius: 8px; padding: 12px 16px; display: flex; align-items: center; }
            .chat-input-box input { background: transparent; border: none; outline: none; color: white; width: 100%; font-size: 15px; }

            .members-sidebar { width: 240px; background-color: var(--bg-secondary); padding: 16px 8px; display: flex; flex-direction: column; gap: 4px; }
            .member-item { display: flex; align-items: center; gap: 12px; padding: 6px 8px; border-radius: 4px; }
            .member-item:hover { background-color: rgba(255,255,255,0.05); }
            .member-avatar { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; }
        </style>
    </head>
    <body>
        <div id="auth-screen">
            <div class="auth-card">
                <i class="fa fa-server" style="font-size: 64px; color: var(--brand);"></i>
                <h2 style="margin-top: 15px; font-weight: bold;">Глобальный Сервер</h2>
                <p style="color: var(--text-muted);">Введите никнейм для входа</p>

                <div class="input-group" style="margin-top: 30px;">
                    <input type="text" id="nick-input" class="form-control" placeholder="Ваш Никнейм..." style="height: 44px; font-size: 16px; background-color: #1e1f22; border: none; color: white;">
                    <span class="input-group-btn">
                        <button class="btn btn-primary" id="btn-login" style="background-color: var(--brand); border: none; height: 44px; padding: 0 20px; font-weight: bold;">Войти</button>
                    </span>
                </div>
            </div>
        </div>

        <div id="app-layout">
            <div class="channels-sidebar">
                <div class="server-header">
                    <span>Чат-Сервер</span>
                    <a href="/admin" target="_blank" style="color: var(--text-muted);" title="Панель администратора"><i class="fa fa-cog"></i></a>
                </div>

                <div class="channels-list">
                    <div id="text-channels-list"></div>
                </div>

                <div class="user-profile-bar">
                    <div class="status-wrapper">
                        <img src="" id="user-avatar-img" class="user-avatar-small" alt="">
                        <div class="status-dot" style="width: 10px; height: 10px; border-width: 2px;"></div>
                    </div>
                    <div class="user-info-text">
                        <div class="nick" id="user-nickname-display">User</div>
                    </div>
                    <a href="/logout" class="btn btn-link btn-xs" style="color: var(--text-muted);" title="Выйти"><i class="fa fa-sign-out"></i></a>
                </div>
            </div>

            <div class="main-content">
                <div class="chat-top-bar">
                    <i class="fa fa-hashtag" style="color: var(--text-muted);"></i>
                    <span id="current-channel-title">general</span>
                </div>

                <div class="chat-messages" id="messages-container"></div>

                <div class="chat-input-container">
                    <div class="chat-input-box">
                        <input type="text" id="chat-input" placeholder="Написать сообщение..." autocomplete="off">
                    </div>
                </div>
            </div>

            <div class="members-sidebar">
                <div style="font-size: 12px; font-weight: bold; color: var(--text-muted); text-transform: uppercase; margin-bottom: 10px; padding-left: 8px;">
                    В сети — <span id="members-count">0</span>
                </div>
                <div id="members-list-container"></div>
            </div>
        </div>

        <script>
            let currentUser = null;
            let currentChannelId = 'general';
            let socket = null;
            let lastMsgSenderId = null;
            let lastMsgTimestamp = 0;

            async function checkAuth() {
                try {
                    const res = await fetch('/api/me');
                    const data = await res.json();
                    
                    if (data.authenticated) {
                        currentUser = data.user;
                        $('#auth-screen').hide();
                        $('#app-layout').css('display', 'flex');
                        
                        $('#user-nickname-display').text(currentUser.nickname);
                        $('#user-avatar-img').attr('src', currentUser.picture);
                        
                        connectToServer();
                    } else {
                        $('#auth-screen').show();
                        $('#app-layout').hide();
                    }
                } catch(e) { console.error(e); }
            }

            $('#btn-login').click(async function() {
                const nick = $('#nick-input').val().trim();
                if(!nick) return;
                const res = await fetch('/api/login?nickname=' + encodeURIComponent(nick), { method: 'POST' });
                if (res.ok) checkAuth();
            });

            $('#nick-input').keypress(function(e) { if (e.which === 13) $('#btn-login').click(); });

            function connectToServer() {
                if(socket) socket.close();

                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

                socket.onopen = function() {
                    socket.send(JSON.stringify({
                        type: 'init', user_id: currentUser.id,
                        nickname: currentUser.nickname, picture: currentUser.picture
                    }));
                };

                socket.onmessage = function(event) {
                    const data = JSON.parse(event.data);

                    if (data.type === 'init_success') {
                        renderChannels(data.channels.text);
                        loadChatHistory(data.history[currentChannelId] || []);
                    }
                    else if (data.type === 'new_message') {
                        if (data.message.channel_id === currentChannelId || data.global) {
                            appendChatMessage(data.message);
                        }
                    }
                    else if (data.type === 'members_update') {
                        renderMembers(data.members);
                    }
                    else if (data.type === 'channel_cleared') {
                        if (data.channel_id === currentChannelId) {
                            $('#messages-container').empty();
                            $('#messages-container').append('<div style="text-align:center; color:var(--text-muted); margin-top:20px;">История очищена администратором.</div>');
                        }
                    }
                    else if (data.type === 'force_disconnect') {
                        socket.close();
                        alert(data.reason);
                        window.location.href = '/logout';
                    }
                };
                
                socket.onclose = function() {
                    // Prevent auto-reconnect loops if banned, but try reconnecting if just dropped
                    console.log("WebSocket closed");
                };
            }

            function renderChannels(channels) {
                const textList = $('#text-channels-list').empty();
                const categories = {};
                channels.forEach(ch => {
                    const cat = ch.category || 'КАНАЛЫ';
                    if (!categories[cat]) categories[cat] = [];
                    categories[cat].push(ch);
                });

                for (const [catName, catChannels] of Object.entries(categories)) {
                    textList.append(`<div class="category-header"><i class="fa fa-chevron-down" style="font-size: 8px; margin-right: 6px;"></i>${catName}</div>`);
                    catChannels.forEach(ch => {
                        const activeClass = ch.id === currentChannelId ? 'active' : '';
                        textList.append(`<div class="channel-item ${activeClass}" onclick="switchChannel('${ch.id}', '${ch.name}', this)"><i class="fa fa-hashtag"></i> ${ch.name}</div>`);
                    });
                }
            }

            window.switchChannel = function(chId, chName, element) {
                currentChannelId = chId;
                $('#current-channel-title').text(chName);
                
                $('.channel-item').removeClass('active');
                $(element).addClass('active');
                
                $('#messages-container').empty();
                lastMsgSenderId = null;
                lastMsgTimestamp = 0;
                
                connectToServer();
            }

            $('#chat-input').keypress(function(e) {
                if (e.which === 13) {
                    const txt = $(this).val().trim();
                    if (txt && socket && socket.readyState === WebSocket.OPEN) {
                        socket.send(JSON.stringify({ type: 'chat_message', channel_id: currentChannelId, text: txt }));
                        $(this).val('');
                    }
                }
            });

            function parseMarkdown(text) {
                let html = text.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                html = html.replace(/\*\*(.*?)\*\*/g, '<span class="md-bold">$1</span>');
                html = html.replace(/\*(.*?)\*/g, '<span class="md-italic">$1</span>');
                html = html.replace(/~~(.*?)~~/g, '<span class="md-strike">$1</span>');
                html = html.replace(/`(.*?)`/g, '<span class="md-code">$1</span>');
                return html;
            }

            function appendChatMessage(msg) {
                const container = $('#messages-container');
                const ts = msg.timestamp || 0;
                const formattedText = parseMarkdown(msg.text);
                
                // Системное сообщение от администратора (выделяется желтым)
                if (msg.is_system) {
                    container.append(`
                        <div class="msg-item" style="background-color: rgba(250, 166, 26, 0.08); border-left: 3px solid var(--system); margin-top: 20px; padding-top: 10px; padding-bottom: 10px;">
                            <div class="msg-avatar-container" style="display:flex; justify-content:center; align-items:flex-start;">
                                <i class="fa fa-bell" style="color: var(--system); font-size: 20px; margin-top: 4px;"></i>
                            </div>
                            <div class="msg-body">
                                <div class="msg-header">
                                    <span class="msg-author" style="color: var(--system); text-transform: uppercase; font-size: 13px; font-weight: 800;">${msg.sender_name}</span>
                                    <span class="msg-time">${msg.time}</span>
                                </div>
                                <div class="msg-text" style="color: white; font-weight: 500;">${formattedText}</div>
                            </div>
                        </div>
                    `);
                    lastMsgSenderId = null; 
                } 
                else {
                    const isGrouped = (lastMsgSenderId === msg.sender_id) && (ts - lastMsgTimestamp < 300); 

                    if (isGrouped) {
                        container.append(`
                            <div class="msg-item grouped">
                                <span class="msg-time-hover">${msg.time}</span>
                                <div class="msg-body"><div class="msg-text">${formattedText}</div></div>
                            </div>
                        `);
                    } else {
                        container.append(`
                            <div class="msg-item">
                                <div class="msg-avatar-container">
                                    <img src="${msg.sender_picture}" class="msg-avatar">
                                </div>
                                <div class="msg-body">
                                    <div class="msg-header">
                                        <span class="msg-author">${msg.sender_name}</span>
                                        <span class="msg-time">${msg.time}</span>
                                    </div>
                                    <div class="msg-text">${formattedText}</div>
                                </div>
                            </div>
                        `);
                    }
                    lastMsgSenderId = msg.sender_id;
                    lastMsgTimestamp = ts;
                }
                
                const domEl = container[0];
                const atBottom = domEl.scrollHeight - domEl.scrollTop <= domEl.clientHeight + 150;
                if (atBottom || msg.sender_id === currentUser.id) container.scrollTop(domEl.scrollHeight);
            }

            function loadChatHistory(history) {
                $('#messages-container').empty();
                lastMsgSenderId = null; lastMsgTimestamp = 0;
                history.forEach(appendChatMessage);
            }

            function renderMembers(members) {
                $('#members-count').text(members.length);
                const container = $('#members-list-container').empty();
                members.forEach(m => {
                    container.append(`
                        <div class="member-item">
                            <div class="status-wrapper">
                                <img src="${m.picture}" class="member-avatar">
                                <div class="status-dot"></div>
                            </div>
                            <span style="font-weight: 500;">${m.nickname}</span>
                        </div>
                    `);
                });
            }

            checkAuth();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    print("Запуск сервера с обновленной Админ Панелью...")
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
