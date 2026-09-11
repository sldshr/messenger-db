import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Dict, Set, Union, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

# =====================================================================
# --- НАСТРОЙКИ СЕРВЕРА (API & КОНФИГУРАЦИЯ) ---
# =====================================================================
SERVER_NAME = "sldshr.onrunxbuild.com"

WELCOME_MESSAGE = r""" ___ _    ___  ___ _  _ ___ 
 / __| |  |   \| __| || | _ \
 \__ \ |__| |) |__ \ __ |   /
 |___/____|___/|___/_||_|_|_\
-[ sldshr.onrunxbuild.com ]-"""

SERVER_RULES = """1. Не флудить и не спамить.
2. Уважать других участников чата.
3. Запрещено использование ботов без разрешения.
4. Приятного общения в нашем уютном терминале!"""
# =====================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("irc_server")

class Client:
    """Универсальная обертка для TCP и WebSocket клиентов."""
    def __init__(self, writer: Optional[asyncio.StreamWriter] = None, websocket: Optional[WebSocket] = None):
        self.writer = writer
        self.websocket = websocket
        self.nick: Optional[str] = None
        self.user: Optional[str] = None
        self.channels: Set[str] = set()
        self.registered: bool = False
        self.is_websocket: bool = websocket is not None

    async def send(self, message: str):
        """Отправка сообщения клиенту независимо от типа подключения."""
        try:
            if self.is_websocket and self.websocket:
                await self.websocket.send_text(message)
            elif self.writer:
                self.writer.write(f"{message}\r\n".encode('utf-8'))
                await self.writer.drain()
        except Exception as e:
            logger.debug(f"Ошибка отправки клиенту {self.nick}: {e}")

class ServerState:
    def __init__(self):
        self.clients: Set[Client] = set()
        self.nicks: Dict[str, Client] = {}
        self.channels: Dict[str, Set[Client]] = {}
        self.server_name = SERVER_NAME

state = ServerState()

async def broadcast_channel(channel: str, message: str, exclude: Optional[Client] = None):
    """Рассылка сообщения всем участникам канала."""
    if channel not in state.channels:
        return
    
    dead_clients = []
    for client in list(state.channels[channel]):
        if client != exclude:
            try:
                await client.send(message)
            except Exception:
                dead_clients.append(client)
                
    for client in dead_clients:
        await disconnect_client(client)

async def disconnect_client(client: Client):
    """Удаление клиента из памяти и каналов."""
    if client not in state.clients:
        return
    
    nick = client.nick
    logger.info(f"Отключение клиента: {nick or 'Unregistered'}")
    
    for channel in list(client.channels):
        if channel in state.channels:
            state.channels[channel].discard(client)
            if nick:
                await broadcast_channel(channel, f":{nick} QUIT :Connection closed")
            if not state.channels[channel]:
                del state.channels[channel]
                
    if nick and nick in state.nicks:
        del state.nicks[nick]
    state.clients.discard(client)
    
    if client.writer:
        try:
            client.writer.close()
            await client.writer.wait_closed()
        except Exception:
            pass

async def check_registration(client: Client):
    """Приветствие клиента по RFC 1459/2812."""
    if not client.registered and client.nick and client.user:
        client.registered = True
        nick = client.nick
        srv = state.server_name
        
        await client.send(f":{srv} 001 {nick} :Welcome to the Lightweight Web & TCP IRC Server!")
        await client.send(f":{srv} 002 {nick} :Your host is {srv}, running version 2.0")
        await client.send(f":{srv} 003 {nick} :This server was created today")
        await client.send(f":{srv} 004 {nick} {srv} 2.0 o o")
        await client.send(f":{srv} 422 {nick} :MOTD File is missing")

async def handle_irc_line(client: Client, line: str):
    """Единый парсер IRC команд для TCP и WebSockets."""
    line = line.strip()
    if not line:
        return

    parts = line.split()
    command = parts[0].upper()
    args = parts[1:]

    nick = client.nick

    if command == "CAP":
        if args and args[0].upper() == "LS":
            await client.send(f":{state.server_name} CAP * LS :")

    elif command == "PING":
        if args:
            await client.send(f":{state.server_name} PONG {state.server_name} :{args[0]}")

    elif command == "NICK":
        if not args:
            return await client.send(f":{state.server_name} 431 * :No nickname given")
        new_nick = re.sub(r'[^a-zA-Z0-9_\[\]\{\}\-\\]', '', args[0])[:15] or f"User{int(time.time())%1000}"
        
        if new_nick in state.nicks and state.nicks[new_nick] != client:
            return await client.send(f":{state.server_name} 433 * {new_nick} :Nickname is already in use")
            
        if nick:
            del state.nicks[nick]
            await broadcast_channel("#general", f":{nick} NICK :{new_nick}")
        
        state.nicks[new_nick] = client
        client.nick = new_nick
        
        if nick:
            await client.send(f":{nick} NICK :{new_nick}")
        else:
            await check_registration(client)

    elif command == "USER":
        client.user = args[0] if args else "webuser"
        await check_registration(client)

    elif command == "JOIN":
        if not args or not client.nick:
            return
        channels = args[0].split(",")
        for channel in channels:
            if not channel.startswith("#"):
                channel = "#" + channel
                
            if channel not in state.channels:
                state.channels[channel] = set()
            
            state.channels[channel].add(client)
            client.channels.add(channel)
            
            await broadcast_channel(channel, f":{client.nick} JOIN :{channel}")
            
            # Список участников (353 / 366)
            names = " ".join([c.nick for c in state.channels[channel] if c.nick])
            await client.send(f":{state.server_name} 353 {client.nick} = {channel} :{names}")
            await client.send(f":{state.server_name} 366 {client.nick} {channel} :End of /NAMES list")

    elif command == "PRIVMSG":
        if len(args) < 2 or not client.nick:
            return
        target = args[0]
        text = " ".join(args[1:]).lstrip(":")
        
        if target.startswith("#"):
            await broadcast_channel(target, f":{client.nick} PRIVMSG {target} :{text}", exclude=client)
        elif target in state.nicks:
            target_client = state.nicks[target]
            await target_client.send(f":{client.nick} PRIVMSG {target} :{text}")

    elif command == "QUIT":
        await disconnect_client(client)

    elif command == "RULES":
        # Отправляем правила построчно
        for line in SERVER_RULES.split('\n'):
            await client.send(f":{state.server_name} 211 {client.nick} :{line}")

    elif command == "SETCOLOR":
        # Кастомная команда для передачи цвета HTML клиентам
        if args and client.nick:
            color = args[0][:7] # Ограничиваем длину (напр. #FFA500)
            notified = set()
            # Рассылаем новый цвет всем участникам общих каналов
            for channel in client.channels:
                if channel in state.channels:
                    for c in state.channels[channel]:
                        if c not in notified:
                            await c.send(f":{client.nick} SETCOLOR :{color}")
                            notified.add(c)

async def handle_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Обслуживание классических TCP клиентов (порт 6667)."""
    client = Client(writer=writer)
    state.clients.add(client)
    
    msg_count = 0
    last_reset = time.time()

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            decoded_line = line.decode('utf-8', errors='ignore')
            
            # Anti-flood защита CPU
            msg_count += 1
            now = time.time()
            if now - last_reset > 1.0:
                msg_count = 0
                last_reset = now
            elif msg_count > 15:
                await asyncio.sleep(0.3)

            await handle_irc_line(client, decoded_line)
    except Exception:
        pass
    finally:
        await disconnect_client(client)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Запуск фонового TCP IRC сервера параллельно с Uvicorn."""
    logger.info("Запуск TCP IRC сервера на порту 6667...")
    server = await asyncio.start_server(handle_tcp_client, '0.0.0.0', 6667)
    asyncio.create_task(server.serve_forever())
    
    yield
    
    logger.info("Остановка серверов...")
    server.close()
    await server.wait_closed()

app = FastAPI(lifespan=lifespan)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Обработка веб-клиентов через WebSockets."""
    await websocket.accept()
    client = Client(websocket=websocket)
    state.clients.add(client)
    
    try:
        while True:
            data = await websocket.receive_text()
            # Веб-клиент может отправлять команды, разделенные переновосом строки
            for line in data.split("\n"):
                await handle_irc_line(client, line)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS error: {e}")
    finally:
        await disconnect_client(client)

@app.get("/", response_class=HTMLResponse)
async def get_web_chat():
    """Аутентичный классический IRC веб-интерфейс в стиле mIRC / HexChat / WeeChat."""
    html_template = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{SERVER_NAME}}</title>
    <style>
        /* Reset & Base: Early 2010s Web Style */
        * { box-sizing: border-box; }
        html, body {
            height: 100%; margin: 0; padding: 0;
            background: #2b2b2b;
            background-image: linear-gradient(to bottom, #333 0%, #222 100%);
            color: #e0e0e0;
            font-family: 'Tahoma', 'Verdana', sans-serif;
            font-size: 13px;
            overflow: hidden;
        }

        /* Top Header Bar */
        #irc-header {
            height: 38px;
            background: linear-gradient(to bottom, #4a4a4a, #2a2a2a);
            border-bottom: 1px solid #111;
            box-shadow: 0 1px 4px rgba(0,0,0,0.5);
            display: flex;
            align-items: center;
            padding: 0 15px;
            font-weight: bold;
            color: #fff;
            text-shadow: 0 -1px 0 #000;
            z-index: 10;
            position: relative;
        }
        #irc-header .topic {
            color: #aaa;
            font-weight: normal;
            margin-left: 20px;
            font-size: 12px;
            font-style: italic;
        }

        /* Main Workspace */
        #irc-main {
            display: flex;
            height: calc(100% - 68px); /* 38px header + 30px footer */
            width: 100%;
        }

        /* Sidebar (Channels / Users) */
        #irc-sidebar {
            width: 200px;
            background: #2a2a2a;
            border-right: 1px solid #1a1a1a;
            box-shadow: inset -1px 0 5px rgba(0,0,0,0.2);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
        }
        .sidebar-section {
            padding: 6px 10px;
            background: linear-gradient(to bottom, #3a3a3a, #303030);
            color: #fff;
            font-size: 11px;
            text-transform: uppercase;
            border-bottom: 1px solid #1a1a1a;
            border-top: 1px solid #444;
            text-shadow: 0 -1px 0 #000;
        }
        .sidebar-section:first-child { border-top: none; }
        .sidebar-list {
            list-style: none; margin: 0; padding: 0;
            overflow-y: auto; flex-grow: 1;
        }
        .sidebar-list li {
            padding: 5px 10px; cursor: pointer;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            border-bottom: 1px solid #222;
            color: #ccc;
        }
        .sidebar-list li:hover { background: #333; color: #fff; }
        .sidebar-list li.active {
            background: linear-gradient(to bottom, #007bb5, #005f8c);
            color: #fff;
            border-bottom: 1px solid #003f5e;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.2);
        }

        /* Center Chat Log (Strict WeeChat style) */
        #irc-log-container {
            flex-grow: 1;
            display: flex;
            flex-direction: column;
            background: #181818; 
            box-shadow: inset 2px 2px 5px rgba(0,0,0,0.3);
        }
        #irc-log {
            flex-grow: 1;
            padding: 10px;
            overflow-y: auto;
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: 13px;
            line-height: 1.4;
        }

        /* WeeChat Column Layout */
        .log-line { 
            display: flex; 
            margin-bottom: 2px;
            border-left: 2px solid transparent;
        }
        .log-line:hover { background: rgba(255,255,255,0.03); }
        .log-time { 
            color: #666; 
            width: 70px; 
            flex-shrink: 0; 
        }
        .log-nick { 
            width: 120px; 
            text-align: right; 
            padding-right: 8px; 
            margin-right: 8px; 
            border-right: 1px solid #333; 
            font-weight: bold; 
            flex-shrink: 0; 
            white-space: nowrap; 
            overflow: hidden; 
            text-overflow: ellipsis;
        }
        .log-text { 
            color: #d0d0d0; 
            flex-grow: 1; 
            word-break: break-word; 
            white-space: pre-wrap;
        }
        
        /* System Messages & Mentions styling */
        .log-sys .log-nick { color: #888; font-weight: normal; }
        .log-sys .log-text { color: #00af5f; }
        .log-notice .log-text { color: #d78700; }
        .log-action .log-text { color: #af77a7; font-style: italic; }
        .log-error .log-text { color: #ff5f5f; }
        
        /* Highlight Mentions */
        .log-mention { 
            background: rgba(255, 165, 0, 0.15) !important; 
            border-left: 2px solid #ffa500; 
        }
        .mention-highlight { 
            background: rgba(255, 165, 0, 0.4); 
            font-weight: bold; 
            padding: 0 3px; 
            border-radius: 3px; 
            color: #fff; 
        }

        /* Bottom Command & Input Bar */
        #irc-status-bar {
            height: 24px;
            background: linear-gradient(to bottom, #3a3a3a, #2a2a2a);
            color: #ccc;
            font-size: 11px;
            padding: 0 10px;
            border-top: 1px solid #111;
            border-bottom: 1px solid #111;
            display: flex;
            align-items: center;
            justify-content: space-between;
            text-shadow: 0 -1px 0 #000;
        }
        #irc-input-container {
            height: 30px;
            background: #222;
            display: flex;
            align-items: center;
            padding: 0 10px;
            border-top: 1px solid #444;
        }
        #irc-prompt {
            color: #00ff66;
            font-weight: bold;
            padding-right: 10px;
            white-space: nowrap;
            font-family: 'Consolas', monospace;
        }
        #irc-input {
            width: 100%;
            background: #111;
            border: 1px solid #333;
            border-radius: 3px;
            padding: 4px 8px;
            color: #fff;
            font-family: 'Consolas', monospace;
            font-size: 13px;
            box-shadow: inset 0 1px 3px rgba(0,0,0,0.5);
            outline: none;
        }
        #irc-input:focus { border-color: #007bb5; }

        /* Overlay Nick Modal */
        #login-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.75);
            display: flex; align-items: center; justify-content: center;
            z-index: 9999;
        }
        .login-box {
            background: linear-gradient(to bottom, #333, #1f1f1f);
            border: 1px solid #000;
            border-radius: 8px;
            padding: 20px 30px;
            width: 380px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.8), inset 0 1px 0 rgba(255,255,255,0.1);
            text-align: center;
        }
        /* ASCII Banner */
        .ascii-art {
            font-family: 'Consolas', monospace;
            color: #00ff66;
            font-size: 12px;
            line-height: 1.1;
            margin-bottom: 20px;
            text-shadow: 0 0 5px rgba(0, 255, 102, 0.3);
            white-space: pre;
            text-align: center;
        }
        .login-box label {
            display: block;
            color: #aaa;
            font-size: 12px;
            margin-bottom: 8px;
            text-align: left;
            font-weight: bold;
            text-shadow: 0 -1px 0 #000;
        }
        .login-box input {
            width: 100%;
            background: #111;
            border: 1px solid #000;
            border-radius: 4px;
            color: #fff;
            padding: 8px 10px;
            margin-bottom: 20px;
            font-family: 'Tahoma', sans-serif;
            box-shadow: inset 0 2px 4px rgba(0,0,0,0.6);
            outline: none;
        }
        .login-box input:focus { border-color: #0088cc; }
        .login-box button {
            width: 100%;
            background: linear-gradient(to bottom, #0088cc, #0044cc);
            border: 1px solid #002266;
            border-radius: 4px;
            color: #fff;
            padding: 10px;
            cursor: pointer;
            font-family: 'Tahoma', sans-serif;
            font-weight: bold;
            font-size: 14px;
            text-shadow: 0 -1px 0 rgba(0,0,0,0.5);
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.3), 0 2px 3px rgba(0,0,0,0.4);
            transition: all 0.1s;
        }
        .login-box button:hover {
            background: linear-gradient(to bottom, #0099dd, #0055dd);
        }
        .login-box button:active {
            background: #0044cc;
            box-shadow: inset 0 2px 5px rgba(0,0,0,0.5);
        }
        
        #ascii-loader {
            color: #00ff66;
            font-family: 'Consolas', monospace;
            margin-left: 10px;
        }
    </style>
</head>
<body>

    <!-- Startup Login Box with ASCII Art -->
    <div id="login-overlay">
        <div class="login-box">
            <div class="ascii-art">{{WELCOME_MESSAGE}}</div>
            <label>ВВЕДИТЕ НИКНЕЙМ:</label>
            <input type="text" id="nick-input" maxlength="15" autocomplete="off" onkeypress="if(event.key==='Enter') connectChat()">
            <button onclick="connectChat()">ПОДКЛЮЧИТЬСЯ К СЕТИ</button>
        </div>
    </div>

    <!-- Header / Channel Topic -->
    <div id="irc-header">
        <span id="header-chan">#general</span>
        <span class="topic" id="header-topic">Добро пожаловать на сервер {{SERVER_NAME}}</span>
    </div>

    <!-- Main Section -->
    <div id="irc-main">
        <!-- Sidebar -->
        <div id="irc-sidebar">
            <div style="display: flex; flex-direction: column; height: 100%; width: 100%;">
                <div class="sidebar-section">Каналы</div>
                <ul class="sidebar-list" id="chan-list">
                    <li class="active" onclick="switchChannel('#general')">#general</li>
                </ul>
                <div class="sidebar-section">Участники (<span id="user-count">0</span>)</div>
                <ul class="sidebar-list" id="user-list">
                    <!-- Dynamic Users -->
                </ul>
            </div>
        </div>

        <!-- Chat Log Window -->
        <div id="irc-log-container">
            <div id="irc-log"></div>
            
            <!-- Status Bar -->
            <div id="irc-status-bar">
                <div>[<span id="st-time">00:00</span>] [<span id="st-nick">Guest</span>] [<span id="st-chan">#general</span>]</div>
                <div>Server: {{SERVER_NAME}} <span id="ascii-loader">[|]</span></div>
            </div>

            <!-- Input Box -->
            <div id="irc-input-container">
                <span id="irc-prompt">#general &gt;</span>
                <input type="text" id="irc-input" autocomplete="off" placeholder="Напишите сообщение или команду (/help, /nick, /join, /rules)...">
            </div>
        </div>
    </div>

    <script>
        let ws = null;
        let currentNick = '';
        let currentChannel = '#general';
        let users = new Set();
        let customColors = {}; // Хранилище кастомных цветов
        
        const NICK_COLORS = ['#ff5f5f', '#00af5f', '#d78700', '#5f87ff', '#af77a7', '#00afaf', '#d75ffd', '#5fd700'];

        // ASCII Loading Animation
        const loaderFrames = ['[|]', '[/]', '[-]', '[\\\\]'];
        let loaderIdx = 0;
        setInterval(() => {
            const el = document.getElementById('ascii-loader');
            if (el) {
                el.innerText = loaderFrames[loaderIdx];
                loaderIdx = (loaderIdx + 1) % loaderFrames.length;
            }
        }, 150);

        document.getElementById('nick-input').value = 'User' + Math.floor(Math.random() * 899 + 100);
        document.getElementById('nick-input').focus();

        function getNickColor(nick) {
            if (customColors[nick]) return customColors[nick]; // Если есть кастомный цвет, используем его
            
            let hash = 0;
            for (let i = 0; i < nick.length; i++) {
                hash = nick.charCodeAt(i) + ((hash << 5) - hash);
            }
            return NICK_COLORS[Math.abs(hash) % NICK_COLORS.length];
        }

        function getTimeStr() {
            const d = new Date();
            return d.toTimeString().split(' ')[0];
        }

        function updateStatusBar() {
            document.getElementById('st-time').innerText = getTimeStr().substring(0, 5);
            document.getElementById('st-nick').innerText = currentNick || 'Guest';
            document.getElementById('st-chan').innerText = currentChannel;
            document.getElementById('irc-prompt').innerText = currentChannel + ' >';
        }

        setInterval(updateStatusBar, 10000);

        function connectChat() {
            const input = document.getElementById('nick-input').value.trim();
            if (!input) return;

            currentNick = input;
            document.getElementById('login-overlay').style.display = 'none';
            document.getElementById('irc-input').focus();
            updateStatusBar();

            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;

            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                ws.send(`CAP LS 302\\r\\n`);
                ws.send(`NICK ${currentNick}\\r\\n`);
                ws.send(`USER ${currentNick} 0 * :Web User\\r\\n`);
                ws.send(`JOIN ${currentChannel}\\r\\n`);
                addSysMessage(`Подключение к серверу {{SERVER_NAME}} установлено.`);
            };

            ws.onmessage = (event) => {
                const lines = event.data.split('\\n');
                lines.forEach(line => parseIRCLine(line.trim()));
            };

            ws.onclose = () => {
                addErrorMessage("Соединение с сервером разорвано.");
            };
        }

        function switchChannel(chan) {
            if (chan === currentChannel) return;
            currentChannel = chan;
            document.getElementById('header-chan').innerText = chan;
            document.getElementById('irc-log').innerHTML = '';
            addSysMessage(`Переключено на ${chan}`);
            updateStatusBar();
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(`JOIN ${currentChannel}\\r\\n`);
            }
            document.getElementById('irc-input').focus();
        }

        function parseIRCLine(line) {
            if (!line) return;

            if (line.startsWith('PING')) {
                ws.send(`PONG ${line.split(' ')[1]}\\r\\n`);
                return;
            }

            const match = line.match(/^(?::([^ !]+)(?:![^ ]+)? )?([A-Z0-9]+) (?:([^:]+)?)(?::(.*))?$/);
            if (!match) return;

            const [, prefix, command, rawParams, trailing] = match;
            const params = rawParams ? rawParams.trim().split(' ') : [];

            if (command === 'PRIVMSG') {
                const sender = prefix;
                const target = params[0] || currentChannel;
                const text = trailing;
                if (text && text.startsWith('\\x01ACTION') && text.endsWith('\\x01')) {
                    const actionText = text.substring(8, text.length - 1);
                    addActionMessage(sender, actionText);
                } else {
                    // Проверка на личное сообщение (Whisper)
                    if (!target.startsWith('#')) {
                        addChatMessage(sender, `[ЛС от ${sender}] ${text}`, false);
                    } else {
                        addChatMessage(sender, text);
                    }
                }
            } else if (command === 'SETCOLOR') {
                const sender = prefix;
                const color = trailing || params[0];
                customColors[sender] = color;
                updateUsersUI();
            } else if (command === '211') { // Код для правил
                addNoticeMessage(`[Правило] ${trailing}`);
            } else if (command === 'JOIN') {
                const sender = prefix;
                users.add(sender);
                updateUsersUI();
                addSysMessage(`--> ${sender} присоединился к ${currentChannel}`);
            } else if (command === 'QUIT') {
                const sender = prefix;
                users.delete(sender);
                updateUsersUI();
                addSysMessage(`<-- ${sender} вышел (${trailing || 'Connection closed'})`);
            } else if (command === '353') {
                if (trailing) {
                    trailing.split(' ').forEach(u => u && users.add(u));
                    updateUsersUI();
                }
            } else if (command === 'NICK') {
                const oldNick = prefix;
                const newNick = trailing || params[0];
                if (oldNick === currentNick) {
                    currentNick = newNick;
                    updateStatusBar();
                }
                users.delete(oldNick);
                users.add(newNick);
                // Сохраняем цвет для нового ника
                if (customColors[oldNick]) {
                    customColors[newNick] = customColors[oldNick];
                    delete customColors[oldNick];
                }
                updateUsersUI();
                addSysMessage(`--- ${oldNick} теперь известен как ${newNick}`);
            } else if (command === 'NOTICE' || command === '001' || command === '002' || command === '003') {
                if (trailing) addNoticeMessage(`*** ${trailing}`);
            }
        }

        document.getElementById('irc-input').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                const msg = this.value.trim();
                if (!msg || !ws) return;

                if (msg.startsWith('/')) {
                    handleCommand(msg);
                } else {
                    ws.send(`PRIVMSG ${currentChannel} :${msg}\\r\\n`);
                    addChatMessage(currentNick, msg, true);
                }
                this.value = '';
            }
        });

        function handleCommand(cmdStr) {
            const parts = cmdStr.substring(1).split(' ');
            const cmd = parts[0].toUpperCase();
            const args = parts.slice(1);
            const arg = args.join(' ');

            if (cmd === 'NICK') {
                const newNick = args[0];
                const newColor = args[1]; // Опциональный цвет: /nick Slava #00ff00
                
                if (newNick) ws.send(`NICK ${newNick}\\r\\n`);
                if (newColor && newColor.startsWith('#')) {
                    ws.send(`SETCOLOR ${newColor}\\r\\n`);
                }
            } else if (cmd === 'MSG' || cmd === 'W') {
                const target = args[0];
                const text = args.slice(1).join(' ');
                if (target && text) {
                    ws.send(`PRIVMSG ${target} :${text}\\r\\n`);
                    addChatMessage(currentNick, `-> [ЛС для ${target}] ${text}`, true);
                }
            } else if (cmd === 'RULES') {
                ws.send(`RULES\\r\\n`);
            } else if (cmd === 'JOIN') {
                if (arg) switchChannel(arg.startsWith('#') ? arg : '#' + arg);
            } else if (cmd === 'ME') {
                if (arg) {
                    ws.send(`PRIVMSG ${currentChannel} :\\x01ACTION ${arg}\\x01\\r\\n`);
                    addActionMessage(currentNick, arg);
                }
            } else if (cmd === 'CLEAR') {
                document.getElementById('irc-log').innerHTML = '';
            } else if (cmd === 'HELP') {
                addNoticeMessage("Команды: /nick <ник> [#цвет], /join <#канал>, /msg <ник> <текст>, /me <действие>, /rules, /clear");
            } else {
                ws.send(`${cmd} ${arg}\\r\\n`);
            }
        }

        function createLogLine(time, nickHtml, textHtml, className = '') {
            const log = document.getElementById('irc-log');
            const div = document.createElement('div');
            div.className = `log-line ${className}`;
            div.innerHTML = `<div class="log-time">[${time}]</div>` +
                            `<div class="log-nick">${nickHtml}</div>` +
                            `<div class="log-text">${textHtml}</div>`;
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
        }

        function addChatMessage(author, text, isSelf = false) {
            const color = isSelf ? '#5f87ff' : getNickColor(author);
            let className = '';
            let safeTextHtml = escapeHtml(text);
            
            // Логика упоминаний (Highlight Mentions)
            if (!isSelf && currentNick && text.toLowerCase().includes(currentNick.toLowerCase())) {
                className = 'log-mention';
                // Экранируем ник для безопасного регулярного выражения
                const safeNick = currentNick.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                const regex = new RegExp(`(${safeNick})`, 'gi');
                safeTextHtml = safeTextHtml.replace(regex, '<span class="mention-highlight">$1</span>');
            }

            const nickHtml = `<span style="color: ${color};">${escapeHtml(author)}</span>`;
            createLogLine(getTimeStr(), nickHtml, safeTextHtml, className);
        }

        function addActionMessage(author, actionText) {
            createLogLine(getTimeStr(), '*', `${escapeHtml(author)} ${escapeHtml(actionText)}`, 'log-action');
        }

        function addSysMessage(text) {
            createLogLine(getTimeStr(), '==', escapeHtml(text), 'log-sys');
        }

        function addNoticeMessage(text) {
            createLogLine(getTimeStr(), '--', escapeHtml(text), 'log-notice');
        }

        function addErrorMessage(text) {
            createLogLine(getTimeStr(), '!!', escapeHtml(text), 'log-error');
        }

        function updateUsersUI() {
            const list = document.getElementById('user-list');
            document.getElementById('user-count').innerText = users.size;
            list.innerHTML = '';
            users.forEach(u => {
                const li = document.createElement('li');
                li.style.color = getNickColor(u);
                li.innerText = (u === currentNick ? '@' : ' ') + u;
                list.appendChild(li);
            });
        }

        function escapeHtml(str) {
            return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        }
    </script>
</body>
</html>
    """
    
    # Внедряем переменные API прямо в HTML с помощью строковой замены
    html_rendered = html_template.replace("{{SERVER_NAME}}", SERVER_NAME).replace("{{WELCOME_MESSAGE}}", WELCOME_MESSAGE)
    
    return HTMLResponse(content=html_rendered)

if __name__ == "__main__":
    uvicorn.run("irc_server:app", host="0.0.0.0", port=8000, log_level="warning")
