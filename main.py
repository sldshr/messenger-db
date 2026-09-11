import os
import json
import datetime
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import HTMLResponse
import uvicorn

# Cloudflare Turnstile credentials
TURNSTILE_SECRET = "0x4AAAAAAEt2kX9fNPZNVSsCEur4myw93h4"
TURNSTILE_SITEKEY = "0x4AAAAAAEt2kcFzE58AuS_r" 

# Standard list of public IRC channels
DEFAULT_CHANNELS = [
    "#general", "#random", "#news", "#music", "#gaming",
    "#programming", "#python", "#javascript", "#movies", "#anime",
    "#books", "#science", "#space", "#technology", "#hardware",
    "#art", "#design", "#photography", "#memes", "#sports",
    "#fitness", "#food", "#travel", "#cars", "#help"
]

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IRC Lite Web Modern</title>
    <!-- CDN Bootstrap 1.4.0 (Compatibility) -->
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/twbs/bootstrap@v1.4.0/bootstrap.min.css">
    <!-- Cloudflare Turnstile SDK -->
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
    <style>
        :root {
            --bg-main: #0f172a;
            --bg-card: #1e293b;
            --bg-input: #334155;
            --bg-hover: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #3b82f6;
            --accent-hover: #2563eb;
            --success: #10b981;
            --border-color: #334155;
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            padding: 0;
            background-color: var(--bg-main);
            color: var(--text-main);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            height: 100vh;
            overflow: hidden;
        }

        .hidden { display: none !important; }

        /* Login Screen */
        .login-page {
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            background: radial-gradient(circle at center, #1e293b 0%, #0f172a 100%);
        }

        .login-card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 32px;
            width: 100%;
            max-width: 440px;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
        }

        .login-card h2 {
            color: var(--text-main);
            font-size: 24px;
            margin-bottom: 8px;
            font-weight: 700;
        }

        .login-card p {
            color: var(--text-muted);
            font-size: 13px;
            margin-bottom: 24px;
        }

        .form-group {
            margin-bottom: 18px;
        }

        .form-group label {
            display: block;
            color: var(--text-main);
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 6px;
        }

        .form-control-custom {
            width: 100%;
            padding: 10px 14px;
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            color: #fff;
            font-size: 14px;
            outline: none;
            transition: border-color 0.2s;
        }

        .form-control-custom:focus {
            border-color: var(--accent);
        }

        .btn-primary-custom {
            width: 100%;
            padding: 12px;
            background: var(--accent);
            color: white;
            border: none;
            border-radius: 6px;
            font-weight: 600;
            font-size: 15px;
            cursor: pointer;
            transition: background 0.2s;
        }

        .btn-primary-custom:hover {
            background: var(--accent-hover);
        }

        .checkbox-container {
            display: flex;
            align-items: flex-start;
            gap: 10px;
            font-size: 12px;
            color: var(--text-muted);
            margin-bottom: 18px;
        }

        .checkbox-container input {
            margin-top: 2px;
        }

        /* App Layout */
        .app-container {
            display: flex;
            height: 100vh;
            width: 100vw;
        }

        /* Sidebars Shared */
        .sidebar-left, .sidebar-right {
            background: var(--bg-card);
            border-right: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
            user-select: none;
        }

        .sidebar-left {
            width: 260px;
            min-width: 260px;
        }

        .sidebar-right {
            width: 240px;
            min-width: 240px;
            border-right: none;
            border-left: 1px solid var(--border-color);
        }

        .sidebar-header {
            padding: 16px;
            border-bottom: 1px solid var(--border-color);
            font-weight: 700;
            font-size: 15px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .sidebar-scroll {
            flex: 1;
            overflow-y: auto;
            padding: 12px 8px;
        }

        .section-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
            padding: 8px 8px 4px 8px;
            font-weight: 700;
        }

        .channel-item, .user-item {
            display: flex;
            align-items: center;
            padding: 8px 10px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            color: var(--text-muted);
            margin-bottom: 2px;
            transition: background 0.15s, color 0.15s;
        }

        .channel-item:hover, .user-item:hover {
            background: var(--bg-hover);
            color: var(--text-main);
        }

        .channel-item.active {
            background: var(--accent);
            color: #fff;
            font-weight: 600;
        }

        .channel-icon {
            margin-right: 8px;
            opacity: 0.7;
            font-weight: bold;
        }

        /* User List Items */
        .user-status-dot {
            width: 8px;
            height: 8px;
            background-color: var(--success);
            border-radius: 50%;
            margin-right: 10px;
            display: inline-block;
        }

        .user-badge {
            margin-left: auto;
            font-size: 10px;
            background: rgba(255, 255, 255, 0.1);
            padding: 2px 6px;
            border-radius: 10px;
            color: var(--text-muted);
        }

        /* Main Chat Panel */
        .chat-main {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: var(--bg-main);
        }

        .chat-header {
            height: 56px;
            border-bottom: 1px solid var(--border-color);
            padding: 0 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--bg-card);
        }

        .chat-header h3 {
            margin: 0;
            font-size: 18px;
            color: var(--text-main);
            font-weight: 700;
        }

        .chat-header .subtext {
            font-size: 12px;
            color: var(--text-muted);
        }

        .chat-messages {
            flex: 1;
            overflow-y: auto;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        /* Message Bubbles */
        .msg-row {
            display: flex;
            flex-direction: column;
            max-width: 85%;
        }

        .msg-meta {
            font-size: 11px;
            color: var(--text-muted);
            margin-bottom: 4px;
            display: flex;
            gap: 8px;
            align-items: center;
        }

        .msg-sender {
            font-weight: 600;
            color: #60a5fa;
        }

        .msg-bubble {
            background: var(--bg-card);
            padding: 10px 14px;
            border-radius: 8px;
            border: 1px solid var(--border-color);
            color: var(--text-main);
            font-size: 14px;
            line-height: 1.4;
            word-break: break-word;
        }

        .msg-row.me {
            align-self: flex-end;
        }

        .msg-row.me .msg-sender {
            color: #a7f3d0;
        }

        .msg-row.me .msg-bubble {
            background: #1e3a8a;
            border-color: #1d4ed8;
        }

        .sys-msg {
            align-self: center;
            background: rgba(51, 65, 85, 0.4);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 4px 14px;
            font-size: 12px;
            color: var(--text-muted);
            font-style: italic;
        }

        /* Chat Input Bar */
        .chat-input-area {
            padding: 16px 20px;
            background: var(--bg-card);
            border-top: 1px solid var(--border-color);
        }

        .chat-input-form {
            display: flex;
            gap: 10px;
            margin: 0;
        }

        .chat-input-form input {
            flex: 1;
            padding: 12px 16px;
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: #fff;
            font-size: 14px;
            outline: none;
        }

        .chat-input-form input:focus {
            border-color: var(--accent);
        }

        .chat-input-form button {
            padding: 0 24px;
            background: var(--accent);
            color: white;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
        }

        .chat-input-form button:hover {
            background: var(--accent-hover);
        }

        /* Custom Scrollbar */
        ::-webkit-scrollbar {
            width: 6px;
        }
        ::-webkit-scrollbar-track {
            background: transparent;
        }
        ::-webkit-scrollbar-thumb {
            background: #334155;
            border-radius: 3px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: #475569;
        }
    </style>
</head>
<body>

    <div class="login-page" id="login-view">
        <div class="login-card">
            <h2>IRC Lite Web</h2>
            <p>Вход без сохранения истории. Оперативная память 512 МБ.</p>
            
            <form id="login-form" onsubmit="login(event)">
                <div class="form-group">
                    <label for="username">Имя пользователя</label>
                    <input type="text" id="username" class="form-control-custom" placeholder="Например: Hacker99" maxlength="20" required autocomplete="off">
                </div>

                <div class="checkbox-container">
                    <input type="checkbox" id="privacy" required>
                    <label for="privacy">Мне есть 18 лет, соглашаюсь с <a href="#" style="color:var(--accent);">условиями конфиденциальности</a>.</label>
                </div>

                <div class="form-group">
                    <label>Проверка безопасности</label>
                    <div class="cf-turnstile" data-sitekey="TURNSTILE_SITEKEY_PLACEHOLDER"></div>
                </div>

                <button type="submit" id="login-btn" class="btn-primary-custom">Войти в чат</button>
                <div id="login-error" style="color: #ef4444; font-size: 13px; margin-top: 12px; text-align: center;"></div>
            </form>
        </div>
    </div>

    <div class="app-container hidden" id="chat-view">
        
        <!-- Left Sidebar: Channels -->
        <div class="sidebar-left">
            <div class="sidebar-header">
                <span>💬 IRC Channels</span>
            </div>
            
            <div class="sidebar-scroll">
                <div class="section-title">Публичные каналы</div>
                <div id="channel-list">
                    <!-- Dynamic Channels -->
                </div>

                <div class="section-title" style="margin-top: 16px;">Секретная комната</div>
                <div style="padding: 0 8px;">
                    <form onsubmit="joinCustomChannel(event)" style="margin:0;">
                        <input type="text" id="custom-channel" class="form-control-custom" placeholder="#секретный-чат" style="padding:8px 10px; font-size:12px; margin-bottom:6px;" required>
                        <button type="submit" class="btn-primary-custom" style="padding:6px; font-size:12px;">Перейти / Создать</button>
                    </form>
                </div>
            </div>
        </div>

        <!-- Middle: Main Chat Area -->
        <div class="chat-main">
            <div class="chat-header">
                <div>
                    <h3 id="current-channel-title">#general</h3>
                    <div class="subtext">Память только в ОЗУ • Сообщения не сохраняются</div>
                </div>
                <div>
                    <span class="user-badge" id="channel-users-count">Участников: 0</span>
                </div>
            </div>

            <div class="chat-messages" id="chat-box">
                <div class="sys-msg">Добро пожаловать в IRC Lite Web. Вы подключены к защищённой сети.</div>
            </div>

            <div class="chat-input-area">
                <form class="chat-input-form" onsubmit="sendMessage(event)">
                    <input type="text" id="message-input" placeholder="Напишите сообщение..." autocomplete="off" required>
                    <button type="submit">Отправить</button>
                </form>
            </div>
        </div>

        <!-- Right Sidebar: Users Online -->
        <div class="sidebar-right">
            <div class="sidebar-header">
                <span>👥 Участники</span>
            </div>

            <div class="sidebar-scroll">
                <div class="section-title">В этом канале (<span id="count-in-channel">0</span>)</div>
                <div id="users-in-channel-list">
                    <!-- Dynamic Channel Users -->
                </div>

                <div class="section-title" style="margin-top: 20px;">Все онлайн (<span id="count-global">0</span>)</div>
                <div id="users-global-list">
                    <!-- Dynamic Global Users -->
                </div>
            </div>
        </div>

    </div>

    <script>
        let currentUser = "";
        let currentChannel = "#general";
        let ws = null;
        const defaultChannels = DEFAULT_CHANNELS_PLACEHOLDER;

        async function login(e) {
            e.preventDefault();
            const btn = document.getElementById('login-btn');
            const errorSpan = document.getElementById('login-error');
            const username = document.getElementById('username').value.trim();
            const turnstileElem = document.querySelector('[name="cf-turnstile-response"]');
            const turnstileResponse = turnstileElem ? turnstileElem.value : "";

            if (!turnstileResponse) {
                errorSpan.innerText = "Пожалуйста, пройдите проверку капчи.";
                return;
            }

            btn.disabled = true;
            errorSpan.innerText = "Авторизация...";

            const formData = new FormData();
            formData.append('username', username);
            formData.append('cf_turnstile_response', turnstileResponse);

            try {
                const res = await fetch('/login', { method: 'POST', body: formData });
                const data = await res.json();

                if (data.status === "ok") {
                    currentUser = data.username;
                    document.getElementById('login-view').classList.add('hidden');
                    document.getElementById('chat-view').classList.remove('hidden');
                    renderChannels();
                    connectWs(currentChannel);
                } else {
                    errorSpan.innerText = data.message || "Ошибка авторизации.";
                    btn.disabled = false;
                }
            } catch (err) {
                errorSpan.innerText = "Ошибка соединения с сервером.";
                btn.disabled = false;
            }
        }

        function renderChannels() {
            const list = document.getElementById('channel-list');
            list.innerHTML = "";
            defaultChannels.forEach(ch => {
                const div = document.createElement('div');
                div.className = "channel-item" + (ch === currentChannel ? " active" : "");
                div.innerHTML = `<span class="channel-icon">#</span>${ch.replace('#', '')}`;
                div.onclick = () => switchChannel(ch);
                list.appendChild(div);
            });
        }

        function switchChannel(channelName) {
            if (!channelName.startsWith('#')) channelName = '#' + channelName;
            if (channelName === currentChannel) return;
            
            currentChannel = channelName;
            document.getElementById('current-channel-title').innerText = currentChannel;
            
            const chatBox = document.getElementById('chat-box');
            chatBox.innerHTML = `<div class="sys-msg">Переход в канал ${currentChannel}...</div>`;
            
            renderChannels();
            connectWs(currentChannel);
        }

        function joinCustomChannel(e) {
            e.preventDefault();
            const customInput = document.getElementById('custom-channel');
            let ch = customInput.value.trim();
            if (ch) {
                customInput.value = "";
                switchChannel(ch);
            }
        }

        function connectWs(channel) {
            if (ws) {
                ws.close();
            }
            
            const protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
            const wsUrl = protocol + window.location.host + "/ws/" + encodeURIComponent(currentUser) + "/" + encodeURIComponent(channel);
            
            ws = new WebSocket(wsUrl);
            
            ws.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    handleIncomingPacket(data);
                } catch(e) {
                    console.error("Invalid JSON format", event.data);
                }
            };
            
            ws.onclose = function() {
                appendSystemMessage("Соединение с сервером потеряно.");
            };
        }

        function handleIncomingPacket(packet) {
            if (packet.type === "message") {
                appendMessage(packet.username, packet.text, packet.timestamp);
            } else if (packet.type === "system") {
                appendSystemMessage(packet.text);
            } else if (packet.type === "presence") {
                updateUserLists(packet.channel_users, packet.global_users);
            }
        }

        function appendMessage(sender, text, timestamp) {
            const chatBox = document.getElementById('chat-box');
            const isMe = sender === currentUser;
            
            const row = document.createElement('div');
            row.className = "msg-row" + (isMe ? " me" : "");

            row.innerHTML = `
                <div class="msg-meta">
                    <span class="msg-sender">${escapeHtml(sender)}</span>
                    <span>${timestamp}</span>
                </div>
                <div class="msg-bubble">${escapeHtml(text)}</div>
            `;

            chatBox.appendChild(row);
            chatBox.scrollTop = chatBox.scrollHeight;
        }

        function appendSystemMessage(text) {
            const chatBox = document.getElementById('chat-box');
            const div = document.createElement('div');
            div.className = "sys-msg";
            div.innerText = text;
            chatBox.appendChild(div);
            chatBox.scrollTop = chatBox.scrollHeight;
        }

        function updateUserLists(channelUsers, globalUsers) {
            // Update channel list
            const channelUsersBox = document.getElementById('users-in-channel-list');
            const channelCountSpan = document.getElementById('count-in-channel');
            const badgeCount = document.getElementById('channel-users-count');
            
            channelUsersBox.innerHTML = "";
            channelCountSpan.innerText = channelUsers.length;
            badgeCount.innerText = `Участников: ${channelUsers.length}`;

            channelUsers.forEach(u => {
                const div = document.createElement('div');
                div.className = "user-item";
                div.innerHTML = `<span class="user-status-dot"></span><span>${escapeHtml(u)}</span>${u === currentUser ? ' <span class="user-badge">вы</span>' : ''}`;
                channelUsersBox.appendChild(div);
            });

            // Update global users list
            const globalUsersBox = document.getElementById('users-global-list');
            const globalCountSpan = document.getElementById('count-global');
            
            globalUsersBox.innerHTML = "";
            globalCountSpan.innerText = globalUsers.length;

            globalUsers.forEach(u => {
                const div = document.createElement('div');
                div.className = "user-item";
                div.innerHTML = `<span class="user-status-dot" style="background:#3b82f6;"></span><span>${escapeHtml(u)}</span>`;
                globalUsersBox.appendChild(div);
            });
        }

        function sendMessage(e) {
            e.preventDefault();
            const input = document.getElementById('message-input');
            const msg = input.value.trim();
            if (msg && ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: "chat", text: msg }));
                input.value = "";
            }
        }

        function escapeHtml(str) {
            return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
        }
    </script>
</body>
</html>
"""

app = FastAPI()

class ConnectionManager:
    def __init__(self):
        # Dictionary storing channel -> dict of {websocket: username}
        self.active_connections: dict[str, dict[WebSocket, str]] = {
            ch: {} for ch in DEFAULT_CHANNELS
        }

    async def connect(self, websocket: WebSocket, channel: str, username: str):
        await websocket.accept()
        if channel not in self.active_connections:
            self.active_connections[channel] = {}
        self.active_connections[channel][websocket] = username
        
        # Notify channel about join
        await self.broadcast_json({
            "type": "system",
            "text": f"Пользователь {username} присоединился к каналу."
        }, channel)
        
        # Broadcast updated presence list
        await self.broadcast_presence(channel)

    async def disconnect(self, websocket: WebSocket, channel: str, username: str):
        if channel in self.active_connections:
            if websocket in self.active_connections[channel]:
                del self.active_connections[channel][websocket]
            
            # Remove secret channel if empty to optimize 512MB RAM
            if channel not in DEFAULT_CHANNELS and len(self.active_connections[channel]) == 0:
                del self.active_connections[channel]
            else:
                await self.broadcast_json({
                    "type": "system",
                    "text": f"Пользователь {username} покинул канал."
                }, channel)
                await self.broadcast_presence(channel)

    def get_channel_users(self, channel: str) -> list[str]:
        if channel in self.active_connections:
            return list(self.active_connections[channel].values())
        return []

    def get_global_users(self) -> list[str]:
        all_users = set()
        for ch_conns in self.active_connections.values():
            all_users.update(ch_conns.values())
        return sorted(list(all_users))

    async def broadcast_presence(self, channel: str):
        global_users = self.get_global_users()
        channel_users = self.get_channel_users(channel)

        # Update presence for current channel users
        await self.broadcast_json({
            "type": "presence",
            "channel_users": channel_users,
            "global_users": global_users
        }, channel)

    async def broadcast_json(self, data: dict, channel: str):
        if channel in self.active_connections:
            # Send message to all sockets in channel
            dead_sockets = []
            for ws in list(self.active_connections[channel].keys()):
                try:
                    await ws.send_json(data)
                except Exception:
                    dead_sockets.append(ws)
            
            # Clean up dead sockets
            for ws in dead_sockets:
                if ws in self.active_connections[channel]:
                    del self.active_connections[channel][ws]

manager = ConnectionManager()

@app.get("/")
async def get_home():
    html = HTML_CONTENT.replace("TURNSTILE_SITEKEY_PLACEHOLDER", TURNSTILE_SITEKEY)
    html = html.replace("DEFAULT_CHANNELS_PLACEHOLDER", json.dumps(DEFAULT_CHANNELS))
    return HTMLResponse(html)

@app.post("/login")
async def login(username: str = Form(...), cf_turnstile_response: str = Form(...)):
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data={
                "secret": TURNSTILE_SECRET,
                "response": cf_turnstile_response
            })
            data = resp.json()
            if not data.get("success"):
                return {"status": "error", "message": "Проверка безопасности Cloudflare не пройдена."}
        except Exception:
            return {"status": "error", "message": "Ошибка связи с Cloudflare Turnstile."}

    return {"status": "ok", "username": username}

@app.websocket("/ws/{username}/{channel}")
async def websocket_endpoint(websocket: WebSocket, username: str, channel: str):
    await manager.connect(websocket, channel, username)
    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                packet = json.loads(raw_data)
                text = packet.get("text", "").strip()
                if text:
                    now_str = datetime.datetime.now().strftime("%H:%M")
                    await manager.broadcast_json({
                        "type": "message",
                        "username": username,
                        "text": text,
                        "timestamp": now_str
                    }, channel)
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        await manager.disconnect(websocket, channel, username)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
