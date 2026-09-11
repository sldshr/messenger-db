import os
import json
import datetime
import secrets
import re
import time
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form, Request
from fastapi.responses import HTMLResponse
import uvicorn

TURNSTILE_SECRET = "0x4AAAAAAEt2kX9fNPZNVSsCEur4myw93h4"
TURNSTILE_SITEKEY = "0x4AAAAAAEt2kcFzE58AuS_r"

DEFAULT_CHANNELS = [
    "#general", "#random", "#news", "#music", "#gaming",
    "#programming", "#python", "#javascript", "#movies", "#anime",
    "#books", "#science", "#space", "#technology", "#hardware",
    "#art", "#design", "#photography", "#memes", "#sports",
    "#fitness", "#food", "#travel", "#cars", "#help"
]

NICK_REGEX = re.compile(r"^[a-zA-Z0-9_\-\u0400-\u04FF]{2,20}$")
CHANNEL_REGEX = re.compile(r"^#[a-zA-Z0-9_\-\u0400-\u04FF]{2,30}$")

# In-memory security and session management
VALID_SESSIONS: dict[str, dict] = {}
MAX_SESSION_AGE = 86400  # 24 hours
IP_CONNECTIONS: dict[str, int] = {}
MAX_CONNS_PER_IP = 5
MESSAGE_TIMESTAMPS: dict[WebSocket, list[float]] = {}

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IRC Lite Web</title>
    <!-- CDN Bootstrap 1.4.0 -->
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/twbs/bootstrap@v1.4.0/bootstrap.min.css">
    <!-- Cloudflare Turnstile SDK -->
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
    <style>
        :root {
            --bg-dark: #0d1117;
            --bg-card: #161b22;
            --bg-panel: #21262d;
            --bg-input: #0d1117;
            --border-main: #30363d;
            --text-primary: #c9d1d9;
            --text-muted: #8b949e;
            --text-heading: #f0f6fc;
            --accent-blue: #2f81f7;
            --accent-hover: #58a6ff;
            --status-green: #3fb950;
        }

        * {
            box-sizing: border-box !important;
        }

        body {
            margin: 0;
            padding: 0;
            background-color: var(--bg-dark);
            color: var(--text-primary);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            height: 100vh;
            overflow: hidden;
        }

        .hidden { display: none !important; }

        /* SVG Icon styling */
        .icon {
            display: inline-block;
            vertical-align: middle;
            fill: currentColor;
            flex-shrink: 0;
        }

        /* Login Layout Fixes */
        .login-page {
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            background: radial-gradient(circle at center, #161b22 0%, #0d1117 100%);
            padding: 16px;
        }

        .login-card {
            background: var(--bg-card);
            border: 1px solid var(--border-main);
            border-radius: 8px;
            padding: 32px 28px;
            width: 100%;
            max-width: 420px;
            box-shadow: 0 16px 32px rgba(0, 0, 0, 0.6);
        }

        .login-card-header {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 8px;
        }

        .login-card h2 {
            color: var(--text-heading);
            font-size: 20px;
            margin: 0;
            font-weight: 700;
        }

        .login-card p {
            color: var(--text-muted);
            font-size: 13px;
            margin-top: 4px;
            margin-bottom: 24px;
            line-height: 1.4;
        }

        .form-group {
            margin-bottom: 18px;
        }

        .form-group label {
            display: block;
            color: var(--text-primary);
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 6px;
        }

        .form-control-custom {
            width: 100%;
            padding: 10px 12px;
            background: var(--bg-input);
            border: 1px solid var(--border-main);
            border-radius: 6px;
            color: var(--text-heading);
            font-size: 14px;
            outline: none;
            transition: border-color 0.15s ease;
        }

        .form-control-custom:focus {
            border-color: var(--accent-blue);
        }

        /* Checkbox Box Fix */
        .checkbox-container {
            display: flex;
            align-items: flex-start;
            gap: 10px;
            background: var(--bg-panel);
            padding: 12px;
            border-radius: 6px;
            border: 1px solid var(--border-main);
            margin-bottom: 18px;
        }

        .checkbox-container input[type="checkbox"] {
            width: 16px;
            height: 16px;
            margin-top: 2px;
            cursor: pointer;
            flex-shrink: 0;
            accent-color: var(--accent-blue);
        }

        .checkbox-container label {
            font-size: 12px;
            color: var(--text-primary);
            line-height: 1.4;
            margin: 0;
            cursor: pointer;
            user-select: none;
        }

        .checkbox-container a {
            color: var(--accent-hover);
            text-decoration: underline;
        }

        /* Turnstile Container Strict Sizing */
        .turnstile-box {
            display: flex;
            justify-content: center;
            align-items: center;
            background: var(--bg-panel);
            border: 1px solid var(--border-main);
            border-radius: 6px;
            padding: 10px;
            margin-bottom: 20px;
            min-height: 75px;
            overflow: hidden;
        }

        .btn-primary-custom {
            width: 100%;
            padding: 10px 16px;
            background: #238636;
            color: #ffffff;
            border: 1px solid rgba(240,246,252,0.1);
            border-radius: 6px;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
            transition: background 0.15s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }

        .btn-primary-custom:hover {
            background: #2ea043;
        }

        /* Chat Application Interface */
        .app-container {
            display: flex;
            height: 100vh;
            width: 100vw;
        }

        .sidebar-left, .sidebar-right {
            background: var(--bg-card);
            border-right: 1px solid var(--border-main);
            display: flex;
            flex-direction: column;
            user-select: none;
        }

        .sidebar-left {
            width: 250px;
            min-width: 250px;
        }

        .sidebar-right {
            width: 240px;
            min-width: 240px;
            border-right: none;
            border-left: 1px solid var(--border-main);
        }

        .sidebar-header {
            padding: 14px 16px;
            border-bottom: 1px solid var(--border-main);
            font-weight: 700;
            font-size: 14px;
            color: var(--text-heading);
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--bg-dark);
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
            padding: 6px 8px;
            font-weight: 700;
        }

        .channel-item, .user-item {
            display: flex;
            align-items: center;
            padding: 6px 10px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
            color: var(--text-primary);
            margin-bottom: 2px;
            gap: 8px;
        }

        .channel-item:hover, .user-item:hover {
            background: var(--bg-panel);
            color: var(--text-heading);
        }

        .channel-item.active {
            background: var(--accent-blue);
            color: #ffffff;
            font-weight: 600;
        }

        .user-status-dot {
            width: 7px;
            height: 7px;
            background-color: var(--status-green);
            border-radius: 50%;
            flex-shrink: 0;
        }

        .user-badge {
            margin-left: auto;
            font-size: 10px;
            background: rgba(255, 255, 255, 0.1);
            padding: 1px 5px;
            border-radius: 4px;
            color: var(--text-muted);
        }

        .chat-main {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: var(--bg-dark);
        }

        .chat-header {
            height: 50px;
            border-bottom: 1px solid var(--border-main);
            padding: 0 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--bg-card);
        }

        .chat-header-title {
            display: flex;
            align-items: center;
            gap: 8px;
            color: var(--text-heading);
            font-weight: 700;
            font-size: 16px;
        }

        .chat-messages {
            flex: 1;
            overflow-y: auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
        }

        .msg-line {
            font-size: 13px;
            line-height: 1.5;
            word-break: break-word;
            display: flex;
            gap: 8px;
            align-items: baseline;
        }

        .msg-time {
            color: var(--text-muted);
            font-size: 11px;
            flex-shrink: 0;
        }

        .msg-sender {
            font-weight: 600;
            color: var(--accent-hover);
            flex-shrink: 0;
        }

        .msg-sender.me {
            color: var(--status-green);
        }

        .msg-text {
            color: var(--text-primary);
        }

        .sys-line {
            font-size: 12px;
            color: var(--text-muted);
            font-style: italic;
            padding: 2px 0;
        }

        .chat-input-area {
            padding: 12px 16px;
            background: var(--bg-card);
            border-top: 1px solid var(--border-main);
        }

        .chat-input-form {
            display: flex;
            gap: 8px;
            margin: 0;
        }

        .chat-input-form input {
            flex: 1;
            padding: 10px 12px;
            background: var(--bg-input);
            border: 1px solid var(--border-main);
            border-radius: 6px;
            color: var(--text-heading);
            font-size: 13px;
            outline: none;
        }

        .chat-input-form input:focus {
            border-color: var(--accent-blue);
        }

        .chat-input-form button {
            padding: 0 16px;
            background: var(--accent-blue);
            color: #ffffff;
            border: none;
            border-radius: 6px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .chat-input-form button:hover {
            background: #388bfd;
        }

        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #484f58; }
    </style>
</head>
<body>

    <div class="login-page" id="login-view">
        <div class="login-card">
            <div class="login-card-header">
                <svg class="icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
                <h2>IRC Lite Web</h2>
            </div>
            <p>Защищённый мессенджер без сохранения истории (RAM 512MB)</p>
            
            <form id="login-form" onsubmit="login(event)">
                <div class="form-group">
                    <label for="username">Имя пользователя</label>
                    <input type="text" id="username" class="form-control-custom" placeholder="Hacker99" maxlength="20" required autocomplete="off">
                </div>

                <div class="checkbox-container">
                    <input type="checkbox" id="privacy" required>
                    <label for="privacy">Мне есть 18 лет, я принимаю <a href="#" onclick="alert('Сообщения не сохраняются на диске.'); return false;">условия конфиденциальности</a>.</label>
                </div>

                <div class="form-group">
                    <label>Защита Cloudflare</label>
                    <div class="turnstile-box">
                        <div class="cf-turnstile" data-sitekey="TURNSTILE_SITEKEY_PLACEHOLDER" data-theme="dark"></div>
                    </div>
                </div>

                <button type="submit" id="login-btn" class="btn-primary-custom">
                    <svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
                    <span>Войти в чат</span>
                </button>
                <div id="login-error" style="color: #f85149; font-size: 12px; margin-top: 12px; text-align: center; font-weight: 600;"></div>
            </form>
        </div>
    </div>

    <div class="app-container hidden" id="chat-view">
        
        <!-- Left Sidebar: Channels -->
        <div class="sidebar-left">
            <div class="sidebar-header">
                <svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="9" x2="20" y2="9"></line><line x1="4" y1="15" x2="20" y2="15"></line><line x1="10" y1="3" x2="8" y2="21"></line><line x1="16" y1="3" x2="14" y2="21"></line></svg>
                <span>Каналы</span>
            </div>
            
            <div class="sidebar-scroll">
                <div class="section-title">Публичные</div>
                <div id="channel-list"></div>

                <div class="section-title" style="margin-top: 16px;">Секретная комната</div>
                <div style="padding: 0 6px;">
                    <form onsubmit="joinCustomChannel(event)" style="margin:0;">
                        <input type="text" id="custom-channel" class="form-control-custom" placeholder="#секрет" style="padding:6px 8px; font-size:12px; margin-bottom:6px;" maxlength="30" required>
                        <button type="submit" class="btn-primary-custom" style="padding:6px; font-size:12px; background:var(--accent-blue);">
                            <svg class="icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
                            <span>Войти / Создать</span>
                        </button>
                    </form>
                </div>
            </div>
        </div>

        <!-- Central Chat Box -->
        <div class="chat-main">
            <div class="chat-header">
                <div class="chat-header-title">
                    <svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="9" x2="20" y2="9"></line><line x1="4" y1="15" x2="20" y2="15"></line><line x1="10" y1="3" x2="8" y2="21"></line><line x1="16" y1="3" x2="14" y2="21"></line></svg>
                    <span id="current-channel-title">general</span>
                </div>
                <div>
                    <span class="user-badge" id="channel-users-count">Участников: 0</span>
                </div>
            </div>

            <div class="chat-messages" id="chat-box">
                <div class="sys-line">*** Добро пожаловать в IRC Lite Web. История сообщений не сохраняется.</div>
            </div>

            <div class="chat-input-area">
                <form class="chat-input-form" onsubmit="sendMessage(event)">
                    <input type="text" id="message-input" placeholder="Написать сообщение..." maxlength="500" autocomplete="off" required>
                    <button type="submit">
                        <svg class="icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
                    </button>
                </form>
            </div>
        </div>

        <!-- Right Sidebar: Users -->
        <div class="sidebar-right">
            <div class="sidebar-header">
                <svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg>
                <span>Участники</span>
            </div>

            <div class="sidebar-scroll">
                <div class="section-title">В канале (<span id="count-in-channel">0</span>)</div>
                <div id="users-in-channel-list"></div>

                <div class="section-title" style="margin-top: 20px;">Все онлайн (<span id="count-global">0</span>)</div>
                <div id="users-global-list"></div>
            </div>
        </div>

    </div>

    <script>
        let currentUser = "";
        let sessionToken = "";
        let currentChannel = "#general";
        let ws = null;
        const defaultChannels = DEFAULT_CHANNELS_PLACEHOLDER;

        const hashSvg = `<svg class="icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="9" x2="20" y2="9"></line><line x1="4" y1="15" x2="20" y2="15"></line><line x1="10" y1="3" x2="8" y2="21"></line><line x1="16" y1="3" x2="14" y2="21"></line></svg>`;

        async function login(e) {
            e.preventDefault();
            const btn = document.getElementById('login-btn');
            const errorSpan = document.getElementById('login-error');
            const username = document.getElementById('username').value.trim();
            const turnstileElem = document.querySelector('[name="cf-turnstile-response"]');
            const turnstileResponse = turnstileElem ? turnstileElem.value : "";

            if (!turnstileResponse) {
                errorSpan.innerText = "Пожалуйста, пройдите капчу.";
                return;
            }

            btn.disabled = true;
            errorSpan.innerText = "Проверка...";

            const formData = new FormData();
            formData.append('username', username);
            formData.append('cf_turnstile_response', turnstileResponse);

            try {
                const res = await fetch('/login', { method: 'POST', body: formData });
                const data = await res.json();

                if (data.status === "ok") {
                    currentUser = data.username;
                    sessionToken = data.token;
                    document.getElementById('login-view').classList.add('hidden');
                    document.getElementById('chat-view').classList.remove('hidden');
                    renderChannels();
                    connectWs(currentChannel);
                } else {
                    errorSpan.innerText = data.message || "Ошибка входа.";
                    btn.disabled = false;
                }
            } catch (err) {
                errorSpan.innerText = "Ошибка соединения.";
                btn.disabled = false;
            }
        }

        function renderChannels() {
            const list = document.getElementById('channel-list');
            list.innerHTML = "";
            defaultChannels.forEach(ch => {
                const div = document.createElement('div');
                div.className = "channel-item" + (ch === currentChannel ? " active" : "");
                div.innerHTML = `${hashSvg} <span>${ch.replace('#', '')}</span>`;
                div.onclick = () => switchChannel(ch);
                list.appendChild(div);
            });
        }

        function switchChannel(channelName) {
            if (!channelName.startsWith('#')) channelName = '#' + channelName;
            if (channelName === currentChannel) return;
            
            currentChannel = channelName;
            document.getElementById('current-channel-title').innerText = currentChannel.replace('#', '');
            
            const chatBox = document.getElementById('chat-box');
            chatBox.innerHTML = `<div class="sys-line">*** Переход в канал ${escapeHtml(currentChannel)}...</div>`;
            
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
            if (ws) ws.close();
            
            const protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
            const wsUrl = protocol + window.location.host + "/ws/" + encodeURIComponent(sessionToken) + "/" + encodeURIComponent(channel);
            
            ws = new WebSocket(wsUrl);
            
            ws.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    handleIncomingPacket(data);
                } catch(e) {}
            };
            
            ws.onclose = function(e) {
                if (e.code === 4001) appendSystemMessage("*** Сессия недействительна.");
                else if (e.code === 4002) appendSystemMessage("*** Превышен лимит подключений.");
                else appendSystemMessage("*** Соединение потеряно.");
            };
        }

        function handleIncomingPacket(packet) {
            if (packet.type === "message") {
                appendMessage(packet.username, packet.text, packet.timestamp);
            } else if (packet.type === "system") {
                appendSystemMessage("*** " + packet.text);
            } else if (packet.type === "presence") {
                updateUserLists(packet.channel_users, packet.global_users);
            }
        }

        function appendMessage(sender, text, timestamp) {
            const chatBox = document.getElementById('chat-box');
            const isMe = sender === currentUser;
            
            const row = document.createElement('div');
            row.className = "msg-line";

            row.innerHTML = `
                <span class="msg-time">[${timestamp}]</span>
                <span class="msg-sender ${isMe ? 'me' : ''}">&lt;${escapeHtml(sender)}&gt;</span>
                <span class="msg-text">${escapeHtml(text)}</span>
            `;

            chatBox.appendChild(row);
            chatBox.scrollTop = chatBox.scrollHeight;
        }

        function appendSystemMessage(text) {
            const chatBox = document.getElementById('chat-box');
            const div = document.createElement('div');
            div.className = "sys-line";
            div.innerText = text;
            chatBox.appendChild(div);
            chatBox.scrollTop = chatBox.scrollHeight;
        }

        function updateUserLists(channelUsers, globalUsers) {
            const channelUsersBox = document.getElementById('users-in-channel-list');
            document.getElementById('count-in-channel').innerText = channelUsers.length;
            document.getElementById('channel-users-count').innerText = `Участников: ${channelUsers.length}`;
            
            channelUsersBox.innerHTML = "";
            channelUsers.forEach(u => {
                const div = document.createElement('div');
                div.className = "user-item";
                div.innerHTML = `<span class="user-status-dot"></span><span>${escapeHtml(u)}</span>${u === currentUser ? '<span class="user-badge">вы</span>' : ''}`;
                channelUsersBox.appendChild(div);
            });

            const globalUsersBox = document.getElementById('users-global-list');
            document.getElementById('count-global').innerText = globalUsers.length;
            
            globalUsersBox.innerHTML = "";
            globalUsers.forEach(u => {
                const div = document.createElement('div');
                div.className = "user-item";
                div.innerHTML = `<span class="user-status-dot" style="background:#58a6ff;"></span><span>${escapeHtml(u)}</span>`;
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
            return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
        }
    </script>
</body>
</html>
"""

app = FastAPI()

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, dict[WebSocket, str]] = {
            ch: {} for ch in DEFAULT_CHANNELS
        }

    async def connect(self, websocket: WebSocket, channel: str, username: str):
        await websocket.accept()
        if channel not in self.active_connections:
            self.active_connections[channel] = {}
        self.active_connections[channel][websocket] = username
        
        await self.broadcast_json({
            "type": "system",
            "text": f"Пользователь {username} вошел в канал."
        }, channel)
        
        await self.broadcast_presence(channel)

    async def disconnect(self, websocket: WebSocket, channel: str, username: str):
        if channel in self.active_connections:
            if websocket in self.active_connections[channel]:
                del self.active_connections[channel][websocket]
            
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

        await self.broadcast_json({
            "type": "presence",
            "channel_users": channel_users,
            "global_users": global_users
        }, channel)

    async def broadcast_json(self, data: dict, channel: str):
        if channel in self.active_connections:
            dead_sockets = []
            for ws in list(self.active_connections[channel].keys()):
                try:
                    await ws.send_json(data)
                except Exception:
                    dead_sockets.append(ws)
            
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
async def login(request: Request, username: str = Form(...), cf_turnstile_response: str = Form(...)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    
    username = username.strip()
    if not NICK_REGEX.match(username):
        return {"status": "error", "message": "Никнейм должен содержать 2-20 символов."}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data={
                "secret": TURNSTILE_SECRET,
                "response": cf_turnstile_response,
                "remoteip": client_ip
            }, timeout=5.0)
            data = resp.json()
            if not data.get("success"):
                return {"status": "error", "message": "Капча Cloudflare не пройдена."}
        except Exception:
            return {"status": "error", "message": "Ошибка соединения с Cloudflare."}

    now = time.time()
    expired = [t for t, s in VALID_SESSIONS.items() if now - s["created_at"] > MAX_SESSION_AGE]
    for t in expired:
        del VALID_SESSIONS[t]

    token = secrets.token_hex(16)
    VALID_SESSIONS[token] = {
        "username": username,
        "created_at": now,
        "ip": client_ip
    }

    return {"status": "ok", "username": username, "token": token}

@app.websocket("/ws/{token}/{channel}")
async def websocket_endpoint(websocket: WebSocket, token: str, channel: str):
    session = VALID_SESSIONS.get(token)
    if not session:
        await websocket.close(code=4001, reason="Unauthorized session token")
        return
    
    username = session["username"]
    client_ip = websocket.client.host if websocket.client else "127.0.0.1"
    
    current_conns = IP_CONNECTIONS.get(client_ip, 0)
    if current_conns >= MAX_CONNS_PER_IP:
        await websocket.close(code=4002, reason="Too many connections")
        return

    channel = channel.strip().lower()
    if not channel.startswith("#"):
        channel = "#" + channel
    if not CHANNEL_REGEX.match(channel):
        channel = "#general"

    IP_CONNECTIONS[client_ip] = current_conns + 1
    MESSAGE_TIMESTAMPS[websocket] = []

    await manager.connect(websocket, channel, username)
    try:
        while True:
            raw_data = await websocket.receive_text()
            
            now = time.time()
            timestamps = MESSAGE_TIMESTAMPS.get(websocket, [])
            timestamps = [t for t in timestamps if now - t < 3.0]
            if len(timestamps) >= 5:
                await websocket.send_json({
                    "type": "system",
                    "text": "⚠️ Слишком частые сообщения. Подождите 3 секунды."
                })
                continue
            
            timestamps.append(now)
            MESSAGE_TIMESTAMPS[websocket] = timestamps

            try:
                packet = json.loads(raw_data)
                text = packet.get("text", "").strip()
                if text:
                    text = text[:500]
                    text = "".join(ch for ch in text if ch.isprintable() or ch in "\n\r\t")
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
        pass
    finally:
        if websocket in MESSAGE_TIMESTAMPS:
            del MESSAGE_TIMESTAMPS[websocket]
        if client_ip in IP_CONNECTIONS:
            IP_CONNECTIONS[client_ip] = max(0, IP_CONNECTIONS[client_ip] - 1)
        await manager.disconnect(websocket, channel, username)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
