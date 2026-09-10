import os
# Единый глобальный сервер с категориями каналов
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
MESSAGES = {ch["id"]: [] for ch in CHANNELS}
CONNECTIONS: Dict[WebSocket, dict] = {}

class AdminLoginReq(BaseModel):
    username: str
    password: str

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


@app.get("/admin", response_class=HTMLResponse)
async def get_admin_page(request: Request):
    """Панель администратора."""
    is_admin = request.cookies.get("admin_auth") == "true"
    
    if not is_admin:
        # Страница авторизации админа
        return HTMLResponse("""
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <title>Admin Login</title>
            <style>
                body { background-color: #313338; color: #dbdee1; font-family: sans-serif; display: flex; height: 100vh; align-items: center; justify-content: center; margin: 0;}
                .card { background-color: #2b2d31; padding: 40px; border-radius: 12px; text-align: center; width: 320px; box-shadow: 0 4px 15px rgba(0,0,0,0.2); }
                input { margin-bottom: 15px; background: #1e1f22; border: 1px solid #111214; color: white; padding: 12px; width: 100%; border-radius: 4px; box-sizing: border-box; }
                button { background: #5865f2; border: none; color: white; padding: 12px; width: 100%; border-radius: 4px; font-weight: bold; cursor: pointer; }
                button:hover { background: #4752c4; }
            </style>
        </head>
        <body>
            <div class="card">
                <h3 style="margin-top:0;">Админ-панель</h3>
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
    
    # Дашборд админа
    channels_html = "".join([
        f"<li style='margin-bottom: 6px; display:flex; justify-content:space-between; align-items:center; background:#1e1f22; padding:8px 12px; border-radius:4px;'>"
        f"<span><small style='color:#949ba4; margin-right:8px;'>[{ch.get('category','General')}]</small><b>#{ch['name']}</b></span>"
        f"<a href='/admin/clear?channel_id={ch['id']}' style='color: #f23f43; margin-left:10px; text-decoration:none; font-weight:bold;'>[Очистить]</a></li>"
        for ch in CHANNELS
    ])
    
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Admin Dashboard</title>
        <style>
            body {{ background-color: #313338; color: #dbdee1; font-family: sans-serif; padding: 40px; margin: 0; }}
            .card {{ background-color: #2b2d31; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }}
            a.btn {{ display: inline-block; padding: 10px 15px; border-radius: 4px; text-decoration: none; color: white; margin-right: 10px; font-weight: bold; }}
            .btn-gray {{ background: #4e5058; }}
            .btn-gray:hover {{ background: #6d6f78; }}
            .btn-red {{ background: #da373c; }}
            .btn-red:hover {{ background: #a1282c; }}
            h2 {{ margin-top: 0; color: white; }}
        </style>
    </head>
    <body>
        <h2>Управление сервером</h2>
        <div style="margin-bottom: 20px;">
            <a href="/" class="btn btn-gray">Вернуться в чат</a>
            <a href="/admin/logout" class="btn btn-red">Выйти из админки</a>
        </div>
        
        <div style="display: flex; gap: 20px; max-width: 900px;">
            <div class="card" style="flex: 1;">
                <h3 style="margin-top:0;">Статистика (ОЗУ)</h3>
                <p>Пользователей онлайн: <b style="color: #23a55a; font-size: 18px;">{len(CONNECTIONS)}</b></p>
                <p>Всего сессий: <b>{len(sessions_db)}</b></p>
                <p>Всего каналов: <b>{len(CHANNELS)}</b></p>
            </div>
            <div class="card" style="flex: 2;">
                <h3 style="margin-top:0;">Управление каналами</h3>
                <ul style="list-style: none; padding: 0; max-height: 420px; overflow-y: auto;">
                    {channels_html}
                </ul>
            </div>
        </div>
    </body>
    </html>
    """)

@app.post("/admin/login")
async def admin_login_api(data: AdminLoginReq):
    """API авторизации админа."""
    if data.username == "admin" and data.password == "admin":
        response = JSONResponse({"status": "ok"})
        response.set_cookie("admin_auth", "true", max_age=86400, httponly=True)
        return response
    raise HTTPException(status_code=401, detail="Неверные данные")

@app.get("/admin/logout")
async def admin_logout():
    """Выход из админки."""
    response = RedirectResponse("/admin")
    response.delete_cookie("admin_auth")
    return response

@app.get("/admin/clear")
async def admin_clear_channel(channel_id: str, request: Request):
    """Очистка истории канала."""
    if request.cookies.get("admin_auth") == "true":
        if channel_id in MESSAGES:
            MESSAGES[channel_id] = []
            await broadcast_to_all({"type": "channel_cleared", "channel_id": channel_id})
    return RedirectResponse("/admin")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    client_id = str(uuid.uuid4())
    user_info = {"id": client_id, "nickname": "Guest", "picture": ""}
    CONNECTIONS[websocket] = user_info

    try:
        while True:
            raw_text = await websocket.receive_text()
            data = json.loads(raw_text)
            msg_type = data.get("type")

            # 1. Инициализация
            if msg_type == "init":
                user_info["id"] = data.get("user_id", client_id)
                user_info["nickname"] = data.get("nickname", "Guest")
                user_info["picture"] = data.get("picture", "")
                
                await websocket.send_json({
                    "type": "init_success",
                    "channels": {"text": CHANNELS},
                    "history": MESSAGES
                })
                
                await broadcast_members_update()

            # 2. Текстовое сообщение
            elif msg_type == "chat_message":
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
                        "channel_id": channel_id
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


async def broadcast_to_all(message: dict):
    """Отправка сообщения всем подключенным клиентам."""
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
    """Рассылка обновленного списка онлайн участников."""
    members = [
        {
            "id": info["id"],
            "nickname": info["nickname"],
            "picture": info["picture"]
        }
        for info in CONNECTIONS.values()
    ]
    await broadcast_to_all({"type": "members_update", "members": members})


@app.get("/", response_class=HTMLResponse)
async def get_index():
    html_content = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Global Chat Server</title>
        <!-- Bootstrap 3 & FontAwesome -->
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
            }

            * { box-sizing: border-box; }
            body {
                background-color: var(--bg-primary);
                color: var(--text-normal);
                font-family: 'gg sans', 'Noto Sans', Helvetica, Arial, sans-serif;
                margin: 0; padding: 0; height: 100vh; overflow: hidden;
            }

            #auth-screen {
                display: flex; height: 100vh; justify-content: center; align-items: center;
                background: linear-gradient(135deg, #1e1f22 0%, #2b2d31 100%);
            }
            .auth-card {
                background-color: var(--bg-secondary); padding: 40px; border-radius: 12px;
                box-shadow: 0 8px 32px rgba(0,0,0,0.5); width: 100%; max-width: 440px; text-align: center;
            }

            #app-layout { display: none; height: 100vh; width: 100vw; flex-direction: row; }

            .channels-sidebar { width: 240px; background-color: var(--bg-secondary); display: flex; flex-direction: column; }
            .server-header {
                height: 48px; padding: 0 16px; border-bottom: 1px solid rgba(0,0,0,0.2);
                display: flex; align-items: center; justify-content: space-between; font-weight: bold; font-size: 16px;
                box-shadow: 0 1px 2px rgba(0,0,0,0.2);
            }
            .channels-list { flex: 1; overflow-y: auto; padding: 12px 8px; }
            .channel-item {
                display: flex; align-items: center; padding: 8px 10px; border-radius: 4px;
                color: var(--text-muted); cursor: pointer; margin-bottom: 2px; font-size: 15px;
            }
            .channel-item i { margin-right: 8px; width: 18px; text-align: center; }
            .channel-item:hover { background-color: rgba(255,255,255,0.05); color: var(--text-normal); }
            .channel-item.active { background-color: var(--bg-accent); color: white; }

            .user-profile-bar { height: 52px; background-color: #232428; padding: 0 8px; display: flex; align-items: center; justify-content: space-between; }
            .user-avatar-small { width: 32px; height: 32px; border-radius: 50%; margin-right: 8px; object-fit: cover; }
            .user-info-text { flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
            .user-info-text .nick { font-weight: bold; font-size: 14px; }

            .main-content { flex: 1; display: flex; flex-direction: column; background-color: var(--bg-primary); }
            .chat-top-bar { height: 48px; padding: 0 16px; border-bottom: 1px solid rgba(0,0,0,0.2); display: flex; align-items: center; gap: 8px; font-weight: bold; font-size: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.2); }
            
            /* Стилизация сообщений */
            .chat-messages { flex: 1; padding: 16px 0; overflow-y: auto; display: flex; flex-direction: column; scroll-behavior: auto; }
            .msg-item { display: flex; padding: 2px 16px; margin-top: 14px; position: relative; min-height: 40px; }
            .msg-item:hover { background-color: rgba(255, 255, 255, 0.03); }
            .msg-item.grouped { margin-top: 0; padding-top: 1px; padding-bottom: 1px; min-height: 22px; }
            
            .msg-avatar-container { width: 40px; height: 40px; margin-right: 16px; flex-shrink: 0; position: relative; }
            .msg-avatar { width: 40px; height: 40px; border-radius: 50%; background-color: var(--brand); flex-shrink: 0; cursor: pointer; object-fit: cover; }
            .msg-body { flex: 1; display: flex; flex-direction: column; min-width: 0; }
            
            /* Стили для сгруппированных сообщений (смещение ровно 56px = 40px аватар + 16px отступ) */
            .msg-item.grouped .msg-body { margin-left: 56px; }
            
            .msg-header { display: flex; gap: 8px; align-items: baseline; margin-bottom: 2px; line-height: 1.25; }
            .msg-author { font-weight: 600; color: white; cursor: pointer; font-size: 15px; }
            .msg-author:hover { text-decoration: underline; }
            .msg-time { font-size: 12px; color: var(--text-muted); }
            .msg-text { color: var(--text-normal); word-break: break-word; line-height: 1.375; font-size: 15px; white-space: pre-wrap; }
            
            .msg-time-hover { position: absolute; left: 0px; top: 2px; width: 52px; text-align: right; font-size: 10px; color: var(--text-muted); display: none; line-height: 1.4; user-select: none; }
            .msg-item.grouped:hover .msg-time-hover { display: block; }

            .category-header {
                font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase;
                margin: 16px 0 4px 8px; letter-spacing: 0.5px; display: flex; align-items: center; user-select: none;
            }

            /* Статус онлайна */
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
                    <div id="text-channels-list">
                        <!-- Категории и каналы рендерятся тут -->
                    </div>
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

                <div class="chat-messages" id="messages-container">
                    <!-- Сообщения -->
                </div>

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
                <div id="members-list-container">
                    <!-- Участники -->
                </div>
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

            $('#nick-input').keypress(function(e) {
                if (e.which === 13) $('#btn-login').click();
            });

            function connectToServer() {
                if(socket) socket.close();

                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

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
                        renderChannels(data.channels.text);
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
                    else if (data.type === 'channel_cleared') {
                        if (data.channel_id === currentChannelId) {
                            $('#messages-container').empty();
                            $('#messages-container').append('<div style="text-align:center; color:var(--text-muted); margin-top:20px;">История очищена администратором.</div>');
                        }
                    }
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
                    textList.append(`
                        <div class="category-header">
                            <i class="fa fa-chevron-down" style="font-size: 8px; margin-right: 6px;"></i>
                            ${catName}
                        </div>
                    `);

                    catChannels.forEach(ch => {
                        const activeClass = ch.id === currentChannelId ? 'active' : '';
                        textList.append(`
                            <div class="channel-item ${activeClass}" onclick="switchChannel('${ch.id}', '${ch.name}', this)">
                                <i class="fa fa-hashtag"></i> ${ch.name}
                            </div>
                        `);
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
                
                // Переподключение для получения новой истории (простой метод)
                connectToServer();
            }

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
                
                // Проверяем, можно ли сгруппировать сообщение (тот же автор и прошло менее 5 минут)
                const isGrouped = (lastMsgSenderId === msg.sender_id) && (ts - lastMsgTimestamp < 300); 
                
                const formattedText = parseMarkdown(msg.text);

                if (isGrouped) {
                    container.append(`
                        <div class="msg-item grouped">
                            <span class="msg-time-hover">${msg.time}</span>
                            <div class="msg-body">
                                <div class="msg-text">${formattedText}</div>
                            </div>
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
                
                // Умный скролл: скроллим вниз только если мы уже внизу, или если сообщение наше
                const domEl = container[0];
                const atBottom = domEl.scrollHeight - domEl.scrollTop <= domEl.clientHeight + 150;
                if (atBottom || msg.sender_id === currentUser.id) {
                    container.scrollTop(domEl.scrollHeight);
                }
            }

            function loadChatHistory(history) {
                $('#messages-container').empty();
                lastMsgSenderId = null;
                lastMsgTimestamp = 0;
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
    print("Запуск Discord RAM Сервера...")
    print("Откройте в браузере: http://127.0.0.1:8000")
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
