import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Dict, Set, Union, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

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
        self.server_name = "uvicorn.light.irc"

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
    return """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IRC Web Client</title>
    <!-- Classic Bootstrap 1.4.0 -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/twitter-bootstrap/1.4.0/css/bootstrap.min.css">
    <style>
        * { box-sizing: border-box; }
        html, body {
            height: 100%;
            margin: 0;
            padding: 0;
            background-color: #121212 !important;
            color: #d8d8d8;
            font-family: 'Consolas', 'Lucida Console', 'Courier New', monospace;
            font-size: 13px;
            overflow: hidden;
        }

        /* Top Header Bar */
        #irc-header {
            height: 32px;
            background: #1e1e1e;
            border-bottom: 1px solid #333;
            display: flex;
            align-items: center;
            padding: 0 10px;
            font-weight: bold;
            color: #33a1fd;
        }
        #irc-header .topic {
            color: #888;
            font-weight: normal;
            margin-left: 15px;
            font-size: 12px;
        }

        /* Main Workspace */
        #irc-main {
            display: flex;
            height: calc(100% - 62px);
            width: 100%;
            background: #141414;
        }

        /* Left/Right Sidebar */
        #irc-sidebar {
            width: 200px;
            background: #1a1a1a;
            border-right: 1px solid #2e2e2e;
            display: flex;
            flex-column: column;
            overflow: hidden;
            flex-shrink: 0;
        }
        .sidebar-section {
            padding: 5px 8px;
            background: #222;
            color: #888;
            font-size: 11px;
            text-transform: uppercase;
            border-bottom: 1px solid #333;
        }
        .sidebar-list {
            list-style: none;
            margin: 0;
            padding: 0;
            overflow-y: auto;
            flex-grow: 1;
        }
        .sidebar-list li {
            padding: 3px 8px;
            cursor: pointer;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .sidebar-list li:hover {
            background: #2a2a2a;
        }
        .sidebar-list li.active {
            background: #005f87;
            color: #fff;
        }

        /* Center Chat Log */
        #irc-log-container {
            flex-grow: 1;
            display: flex;
            flex-direction: column;
            background: #0f0f0f;
            overflow: hidden;
        }
        #irc-log {
            flex-grow: 1;
            padding: 8px 12px;
            overflow-y: auto;
            word-wrap: break-word;
            white-space: pre-wrap;
            line-height: 1.4;
        }

        /* IRC Log Line Styles */
        .log-line { margin-bottom: 2px; }
        .log-time { color: #555; margin-right: 6px; }
        .log-nick { font-weight: bold; margin-right: 6px; }
        .log-nick-self { color: #5f87ff !important; }
        .log-text { color: #e0e0e0; }
        .log-sys { color: #00af5f; }
        .log-notice { color: #d78700; }
        .log-action { color: #af77a7; font-style: italic; }
        .log-error { color: #ff5f5f; }

        /* Bottom Command & Input Bar */
        #irc-status-bar {
            height: 18px;
            background: #262626;
            color: #aaa;
            font-size: 11px;
            padding: 1px 10px;
            border-top: 1px solid #333;
            display: flex;
            justify-content: space-between;
        }
        #irc-input-container {
            height: 30px;
            background: #181818;
            border-top: 1px solid #333;
            display: flex;
            align-items: center;
            padding: 0 5px;
        }
        #irc-prompt {
            color: #00ff66;
            font-weight: bold;
            padding-right: 8px;
            white-space: nowrap;
        }
        #irc-input {
            width: 100%;
            background: transparent;
            border: none;
            outline: none;
            color: #fff;
            font-family: inherit;
            font-size: 13px;
        }

        /* Overlay Nick Modal */
        #login-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.85);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 9999;
        }
        .login-box {
            background: #1e1e1e;
            border: 1px solid #444;
            padding: 20px;
            width: 320px;
            box-shadow: 0 0 15px rgba(0,0,0,0.8);
        }
        .login-box h3 {
            margin: 0 0 15px 0;
            color: #33a1fd;
            font-size: 16px;
            border-bottom: 1px solid #333;
            padding-bottom: 5px;
        }
        .login-box input {
            width: 100%;
            background: #111;
            border: 1px solid #444;
            color: #fff;
            padding: 6px;
            margin-bottom: 12px;
            font-family: inherit;
        }
        .login-box button {
            width: 100%;
            background: #005f87;
            border: none;
            color: #fff;
            padding: 6px;
            cursor: pointer;
            font-family: inherit;
            font-weight: bold;
        }
        .login-box button:hover { background: #0087bd; }
    </style>
</head>
<body>

    <!-- Startup Login Box -->
    <div id="login-overlay">
        <div class="login-box">
            <h3>[ Light IRC Client ]</h3>
            <label style="color: #aaa; font-size: 11px;">ВВЕДИТЕ НИКНЕЙМ:</label>
            <input type="text" id="nick-input" maxlength="15" autocomplete="off">
            <button onclick="connectChat()">ПОДКЛЮЧИТЬСЯ</button>
        </div>
    </div>

    <!-- Header / Channel Topic -->
    <div id="irc-header">
        <span id="header-chan">#general</span>
        <span class="topic" id="header-topic">Канал чата</span>
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
                <div>Server: uvicorn.light.irc</div>
            </div>

            <!-- Input Box -->
            <div id="irc-input-container">
                <span id="irc-prompt">#general &gt;</span>
                <input type="text" id="irc-input" autocomplete="off" placeholder="Напишите сообщение или IRC команду (/help, /nick, /join)...">
            </div>
        </div>
    </div>

    <script>
        let ws = null;
        let currentNick = '';
        let currentChannel = '#general';
        let users = new Set();
        const NICK_COLORS = ['#ff5f5f', '#00af5f', '#d78700', '#5f87ff', '#af77a7', '#00afaf', '#d75ffd', '#5fd700'];

        document.getElementById('nick-input').value = 'User' + Math.floor(Math.random() * 899 + 100);

        function getNickColor(nick) {
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
            updateStatusBar();

            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;

            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                ws.send(`CAP LS 302\\r\\n`);
                ws.send(`NICK ${currentNick}\\r\\n`);
                ws.send(`USER ${currentNick} 0 * :Web User\\r\\n`);
                ws.send(`JOIN ${currentChannel}\\r\\n`);
                addSysMessage(`*** Подключение к серверу выполнено как ${currentNick}`);
            };

            ws.onmessage = (event) => {
                const lines = event.data.split('\\n');
                lines.forEach(line => parseIRCLine(line.trim()));
            };

            ws.onclose = () => {
                addErrorMessage("*** Соединение с сервером разорвано.");
            };
        }

        function switchChannel(chan) {
            if (chan === currentChannel) return;
            currentChannel = chan;
            document.getElementById('header-chan').innerText = chan;
            document.getElementById('irc-log').innerHTML = '';
            addSysMessage(`*** Переключено на ${chan}`);
            updateStatusBar();
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(`JOIN ${currentChannel}\\r\\n`);
            }
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
                const text = trailing;
                if (text && text.startsWith('\\x01ACTION') && text.endsWith('\\x01')) {
                    const actionText = text.substring(8, text.length - 1);
                    addActionMessage(sender, actionText);
                } else {
                    addChatMessage(sender, text);
                }
            } else if (command === 'JOIN') {
                const sender = prefix;
                users.add(sender);
                updateUsersUI();
                addSysMessage(`*** ${sender} вошел в ${currentChannel}`);
            } else if (command === 'QUIT') {
                const sender = prefix;
                users.delete(sender);
                updateUsersUI();
                addSysMessage(`*** ${sender} вышел (${trailing || 'Connection closed'})`);
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
                updateUsersUI();
                addSysMessage(`*** ${oldNick} сменил ник на ${newNick}`);
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
            const arg = parts.slice(1).join(' ');

            if (cmd === 'NICK') {
                if (arg) ws.send(`NICK ${arg}\\r\\n`);
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
                addNoticeMessage("*** Команды: /nick <ник>, /join <#канал>, /me <действие>, /clear, /help");
            } else {
                ws.send(`${cmd} ${arg}\\r\\n`);
            }
        }

        function addChatMessage(author, text, isSelf = false) {
            const log = document.getElementById('irc-log');
            const div = document.createElement('div');
            div.className = 'log-line';

            const color = isSelf ? '#5f87ff' : getNickColor(author);

            div.innerHTML = `<span class="log-time">[${getTimeStr()}]</span>` +
                            `<span class="log-nick" style="color: ${color};">&lt;${escapeHtml(author)}&gt;</span>` +
                            `<span class="log-text">${escapeHtml(text)}</span>`;

            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
        }

        function addActionMessage(author, actionText) {
            const log = document.getElementById('irc-log');
            const div = document.createElement('div');
            div.className = 'log-line log-action';
            div.innerHTML = `<span class="log-time">[${getTimeStr()}]</span>* ${escapeHtml(author)} ${escapeHtml(actionText)}`;
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
        }

        function addSysMessage(text) {
            const log = document.getElementById('irc-log');
            const div = document.createElement('div');
            div.className = 'log-line log-sys';
            div.innerHTML = `<span class="log-time">[${getTimeStr()}]</span>${escapeHtml(text)}`;
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
        }

        function addNoticeMessage(text) {
            const log = document.getElementById('irc-log');
            const div = document.createElement('div');
            div.className = 'log-line log-notice';
            div.innerHTML = `<span class="log-time">[${getTimeStr()}]</span>${escapeHtml(text)}`;
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
        }

        function addErrorMessage(text) {
            const log = document.getElementById('irc-log');
            const div = document.createElement('div');
            div.className = 'log-line log-error';
            div.innerHTML = `<span class="log-time">[${getTimeStr()}]</span>${escapeHtml(text)}`;
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
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

if __name__ == "__main__":
    uvicorn.run("irc_server:app", host="0.0.0.0", port=8000, log_level="warning")
