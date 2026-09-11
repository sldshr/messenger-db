import os
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form, Request
from fastapi.responses import HTMLResponse
import uvicorn

# Настройки Cloudflare Turnstile
# Твой секретный ключ (указан в задании)
TURNSTILE_SECRET = "0x4AAAAAAEt2kX9fNPZNVSsCEur4myw93h4"
# ВАЖНО: Ниже укажи свой SITEKEY от Cloudflare (публичный ключ). 
TURNSTILE_SITEKEY = "0x4AAAAAAEt2kcFzE58AuS_r" 

# Список из более чем 20 стандартных каналов
DEFAULT_CHANNELS = [
    "#general", "#random", "#news", "#music", "#gaming",
    "#programming", "#python", "#javascript", "#movies", "#anime",
    "#books", "#science", "#space", "#technology", "#hardware",
    "#art", "#design", "#photography", "#memes", "#sports",
    "#fitness", "#food", "#travel", "#cars", "#help"
]

# Весь фронтенд в одной переменной (HTML + CSS + JS)
HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>IRC Lite Web</title>
    <!-- Исправленный CDN для Bootstrap 1.4.0 (загрузка напрямую из архива GitHub через jsDelivr) -->
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/twbs/bootstrap@v1.4.0/bootstrap.min.css">
    <!-- Cloudflare Turnstile -->
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
    <style>
        body { padding-top: 60px; background-color: #f5f5f5; }
        .hidden { display: none !important; }
        .chat-container { background: #fff; border: 1px solid #ccc; border-radius: 4px; height: 65vh; overflow-y: auto; padding: 15px; margin-bottom: 15px; box-shadow: inset 0 1px 1px rgba(0,0,0,.05); }
        .sidebar { background: #fff; border: 1px solid #ccc; border-radius: 4px; height: 65vh; overflow-y: auto; padding: 15px; }
        .message { margin-bottom: 5px; word-wrap: break-word; }
        .sys-message { color: #888; font-style: italic; }
        .channel-item { cursor: pointer; padding: 5px; border-radius: 3px; }
        .channel-item:hover { background-color: #e6e6e6; }
        .channel-active { background-color: #0064cd; color: white !important; }
        .channel-active:hover { background-color: #004b9a; }
        
        /* Исправления для форм Bootstrap 1.4 */
        form { margin-bottom: 0; }
        .login-wrapper { background: #fff; padding: 30px; border-radius: 6px; box-shadow: 0 2px 6px rgba(0,0,0,0.1); margin-top: 50px; }
    </style>
</head>
<body>

    <div class="topbar">
      <div class="fill">
        <div class="container">
          <h3><a href="#">IRC Lite Web (No History)</a></h3>
        </div>
      </div>
    </div>

    <div class="container" id="login-view">
        <div class="row">
            <div class="span10 offset3 login-wrapper">
                <h2>Вход в мессенджер</h2>
                <p>Представьтесь для входа. История сообщений не сохраняется.</p>
                <form id="login-form" onsubmit="login(event)">
                    <div class="clearfix">
                        <label for="username">Имя пользователя</label>
                        <div class="input">
                            <input class="xlarge" id="username" name="username" type="text" maxlength="20" required placeholder="Например: Hacker99">
                        </div>
                    </div>
                    
                    <div class="clearfix">
                        <label>Конфиденциальность</label>
                        <div class="input">
                            <ul class="inputs-list">
                                <li>
                                    <label>
                                        <input type="checkbox" name="privacy" required>
                                        <span>Я подтверждаю, что мне есть 18 лет, и принимаю <a href="#">условия конфиденциальности</a>.</span>
                                    </label>
                                </li>
                            </ul>
                        </div>
                    </div>

                    <div class="clearfix">
                        <label>Защита от ботов</label>
                        <div class="input">
                            <!-- Cloudflare Turnstile Widget -->
                            <div class="cf-turnstile" data-sitekey="TURNSTILE_SITEKEY_PLACEHOLDER"></div>
                        </div>
                    </div>

                    <div class="actions">
                        <button type="submit" class="btn primary large" id="login-btn">Присоединиться</button>
                        <span id="login-error" style="color: red; margin-left: 10px;"></span>
                    </div>
                </form>
            </div>
        </div>
    </div>

    <div class="container hidden" id="chat-view">
        <div class="row">
            <div class="span4">
                <div class="sidebar">
                    <h4>Публичные каналы</h4>
                    <ul class="unstyled" id="channel-list">
                        <!-- Каналы будут добавлены через JS -->
                    </ul>
                    
                    <hr>
                    <h4>Секретный канал</h4>
                    <p style="font-size: 11px; color:#666;">Создайте или присоединитесь к скрытому каналу по точному имени.</p>
                    <form onsubmit="joinCustomChannel(event)">
                        <input type="text" id="custom-channel" class="span3" placeholder="#секрет" required>
                        <button type="submit" class="btn small" style="margin-top: 5px;">Перейти</button>
                    </form>
                </div>
            </div>
            
            <div class="span12">
                <h3 id="current-channel-title">#general</h3>
                <div class="chat-container" id="chat-box">
                    <div class="sys-message">Добро пожаловать! История сообщений отключена.</div>
                </div>
                
                <form id="message-form" onsubmit="sendMessage(event)">
                    <div class="row">
                        <div class="span10">
                            <input class="span10" type="text" id="message-input" autocomplete="off" placeholder="Введите сообщение..." required>
                        </div>
                        <div class="span2">
                            <button type="submit" class="btn primary span2">Отправить</button>
                        </div>
                    </div>
                </form>
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
            const turnstileResponse = document.querySelector('[name="cf-turnstile-response"]').value;

            if (!turnstileResponse) {
                errorSpan.innerText = "Пожалуйста, пройдите проверку капчи.";
                return;
            }

            btn.disabled = true;
            errorSpan.innerText = "Вход...";

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
                    errorSpan.innerText = data.message || "Ошибка входа.";
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
                const li = document.createElement('li');
                li.className = "channel-item" + (ch === currentChannel ? " channel-active" : "");
                li.innerText = ch;
                li.onclick = () => switchChannel(ch);
                list.appendChild(li);
            });
        }

        function switchChannel(channelName) {
            if (channelName === currentChannel) return;
            
            if (!channelName.startsWith('#')) channelName = '#' + channelName;
            
            currentChannel = channelName;
            document.getElementById('current-channel-title').innerText = currentChannel;
            document.getElementById('chat-box').innerHTML = '<div class="sys-message">Подключение к ' + currentChannel + '...</div>';
            
            renderChannels(); // Обновляем выделение
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
                const chatBox = document.getElementById('chat-box');
                const div = document.createElement('div');
                div.className = "message";
                div.innerHTML = event.data; // Ожидаем безопасный HTML с бэкенда
                chatBox.appendChild(div);
                chatBox.scrollTop = chatBox.scrollHeight;
            };
            
            ws.onclose = function() {
                const chatBox = document.getElementById('chat-box');
                chatBox.innerHTML += '<div class="sys-message" style="color:red;">Соединение потеряно.</div>';
            };
        }

        function sendMessage(e) {
            e.preventDefault();
            const input = document.getElementById('message-input');
            const msg = input.value.trim();
            if (msg && ws && ws.readyState === WebSocket.OPEN) {
                ws.send(msg);
                input.value = "";
            }
        }
    </script>
</body>
</html>
"""

app = FastAPI()

class ConnectionManager:
    def __init__(self):
        # Хранит все активные WebSocket соединения в памяти: dict[str, list[WebSocket]]
        # При достижении 0 пользователей в кастомном канале, он автоматически очищается для экономии ОЗУ.
        self.active_connections: dict[str, list[WebSocket]] = {
            ch: [] for ch in DEFAULT_CHANNELS
        }

    async def connect(self, websocket: WebSocket, channel: str):
        await websocket.accept()
        if channel not in self.active_connections:
            self.active_connections[channel] = []
        self.active_connections[channel].append(websocket)

    def disconnect(self, websocket: WebSocket, channel: str):
        if channel in self.active_connections:
            self.active_connections[channel].remove(websocket)
            # Очистка пустых кастомных каналов для предотвращения утечки памяти
            if channel not in DEFAULT_CHANNELS and len(self.active_connections[channel]) == 0:
                del self.active_connections[channel]

    async def broadcast(self, message: str, channel: str):
        if channel in self.active_connections:
            for connection in self.active_connections[channel]:
                try:
                    await connection.send_text(message)
                except Exception:
                    pass # Игнорируем ошибки отправки (например, клиент резко отключился)

manager = ConnectionManager()

@app.get("/")
async def get_home():
    import json
    # Подстановка переменных в HTML перед отдачей пользователю
    html = HTML_CONTENT.replace("TURNSTILE_SITEKEY_PLACEHOLDER", TURNSTILE_SITEKEY)
    html = html.replace("DEFAULT_CHANNELS_PLACEHOLDER", json.dumps(DEFAULT_CHANNELS))
    return HTMLResponse(html)

@app.post("/login")
async def login(username: str = Form(...), cf_turnstile_response: str = Form(...)):
    # Проверка Cloudflare Turnstile (Асинхронный POST-запрос к API)
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data={
                "secret": TURNSTILE_SECRET,
                "response": cf_turnstile_response
            })
            data = resp.json()
            if not data.get("success"):
                return {"status": "error", "message": "Проверка безопасности не пройдена. Попробуйте еще раз."}
        except Exception:
            return {"status": "error", "message": "Ошибка связи с серверами Cloudflare."}

    # Возвращаем подтверждение входа
    return {"status": "ok", "username": username}

@app.websocket("/ws/{username}/{channel}")
async def websocket_endpoint(websocket: WebSocket, username: str, channel: str):
    await manager.connect(websocket, channel)
    
    # Защита от XSS (очень базовая), так как сообщения транслируются в HTML
    safe_username = username.replace("<", "&lt;").replace(">", "&gt;")
    await manager.broadcast(f"<div class='sys-message'><i>Пользователь <b>{safe_username}</b> присоединился к каналу.</i></div>", channel)
    
    try:
        while True:
            data = await websocket.receive_text()
            # Санитаризация сообщений (в памяти, не пишем на диск!)
            safe_data = data.replace("<", "&lt;").replace(">", "&gt;")
            
            # Трансляция всем участникам
            await manager.broadcast(f"<b>{safe_username}:</b> {safe_data}", channel)
            
    except WebSocketDisconnect:
        manager.disconnect(websocket, channel)
        await manager.broadcast(f"<div class='sys-message'><i>Пользователь <b>{safe_username}</b> покинул канал.</i></div>", channel)


if __name__ == "__main__":
    # Запуск сервера. Подходит для минимальных VPS с 512MB RAM
    uvicorn.run(app, host="0.0.0.0", port=8000)
