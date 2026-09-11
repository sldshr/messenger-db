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
    """Вшитый HTML/JS Веб-интерфейс чата."""
    return """
<!DOCTYPE html>
<html lang="ru" class="h-full bg-slate-900">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IRC Web Chat</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: rgba(15, 23, 42, 0.6); }
        ::-webkit-scrollbar-thumb { background: rgba(51, 65, 85, 0.8); border-radius: 4px; }
    </style>
</head>
<body class="h-full font-sans text-slate-100 flex flex-col antialiased">

    <!-- Modal Nickname Prompt -->
    <div id="login-modal" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
        <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 w-full max-w-md shadow-2xl">
            <div class="text-center mb-6">
                <div class="w-12 h-12 bg-indigo-600/20 text-indigo-400 rounded-xl flex items-center justify-center mx-auto mb-3 text-2xl">
                    <i class="fa-solid fa-comments"></i>
                </div>
                <h2 class="text-xl font-bold">Добро пожаловать в IRC Чат</h2>
                <p class="text-xs text-slate-400 mt-1">Введите псевдоним для входа в сеть</p>
            </div>
            <form id="login-form" onsubmit="connectChat(event)">
                <input type="text" id="nick-input" required maxlength="15" placeholder="Ваш никнейм..." 
                       class="w-full px-4 py-3 bg-slate-900 border border-slate-700 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:outline-none mb-4 text-sm">
                <button type="submit" class="w-full py-3 bg-indigo-600 hover:bg-indigo-500 font-semibold rounded-xl transition shadow-lg shadow-indigo-600/30 text-sm">
                    Войти в чат
                </button>
            </form>
        </div>
    </div>

    <!-- Main Layout -->
    <div class="flex-1 flex flex-col md:flex-row overflow-hidden">
        
        <!-- Sidebar -->
        <div class="w-full md:w-64 bg-slate-950 border-r border-slate-800 flex flex-col flex-shrink-0">
            <!-- Header -->
            <div class="p-4 border-b border-slate-800 flex items-center justify-between">
                <div class="flex items-center gap-2">
                    <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse"></span>
                    <span class="font-bold text-sm tracking-wide">Light IRC</span>
                </div>
                <span id="current-user" class="text-xs bg-slate-800 px-2.5 py-1 rounded-md text-slate-300 font-mono">Guest</span>
            </div>

            <!-- Channel List -->
            <div class="p-3">
                <div class="text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-2 px-2">Каналы</div>
                <button onclick="switchChannel('#general')" class="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-indigo-600/10 text-indigo-400 font-medium text-sm">
                    <i class="fa-solid fa-hashtag text-xs"></i> general
                </button>
            </div>

            <!-- Online Users List -->
            <div class="flex-1 overflow-y-auto p-3 border-t border-slate-800/60">
                <div class="text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-2 px-2">
                    Участники (<span id="user-count">0</span>)
                </div>
                <div id="users-list" class="space-y-1">
                    <!-- Dynamic users -->
                </div>
            </div>
        </div>

        <!-- Chat Area -->
        <div class="flex-1 flex flex-col bg-slate-900 overflow-hidden">
            <!-- Channel Header -->
            <div class="h-14 border-b border-slate-800 px-6 flex items-center justify-between bg-slate-900/50">
                <div class="flex items-center gap-2">
                    <i class="fa-solid fa-hashtag text-indigo-400"></i>
                    <span class="font-semibold text-sm">general</span>
                </div>
                <div class="text-xs text-slate-500">Авто-подключение заблокировано на порт 80/443</div>
            </div>

            <!-- Messages Window -->
            <div id="chat-messages" class="flex-1 overflow-y-auto p-4 space-y-3 font-sans text-sm">
                <div class="text-center my-4">
                    <span class="text-xs bg-slate-800/80 text-slate-400 px-3 py-1.5 rounded-full">
                        Система готова. Ожидание подключения...
                    </span>
                </div>
            </div>

            <!-- Input Box -->
            <div class="p-4 border-t border-slate-800 bg-slate-950">
                <form id="chat-form" onsubmit="sendMessage(event)" class="flex gap-2">
                    <input type="text" id="message-input" autocomplete="off" placeholder="Напишите сообщение в #general..." 
                           class="flex-1 px-4 py-2.5 bg-slate-900 border border-slate-800 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:outline-none text-sm">
                    <button type="submit" class="px-5 py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white font-medium rounded-xl transition flex items-center justify-center">
                        <i class="fa-solid fa-paper-plane"></i>
                    </button>
                </form>
            </div>
        </div>
    </div>

    <!-- Client Logic JS -->
    <script>
        let ws = null;
        let currentNick = '';
        let currentChannel = '#general';
        let users = new Set();

        // Auto generation of random default nickname
        document.getElementById('nick-input').value = 'User' + Math.floor(Math.random() * 899 + 100);

        function connectChat(e) {
            e.preventDefault();
            const nickInput = document.getElementById('nick-input').value.trim();
            if (!nickInput) return;

            currentNick = nickInput;
            document.getElementById('current-user').innerText = currentNick;
            document.getElementById('login-modal').classList.add('hidden');

            // Connect via WebSocket (same host and port)
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;

            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                // Send IRC Handshake
                ws.send(`CAP LS 302\\r\\n`);
                ws.send(`NICK ${currentNick}\\r\\n`);
                ws.send(`USER ${currentNick} 0 * :Web User\\r\\n`);
                ws.send(`JOIN ${currentChannel}\\r\\n`);
            };

            ws.onmessage = (event) => {
                const lines = event.data.split('\\n');
                lines.forEach(line => parseIRCLine(line.trim()));
            };

            ws.onclose = () => {
                addSystemMessage("Соединение с сервером разорвано. Перезагрузите страницу.");
            };
        }

        function parseIRCLine(line) {
            if (!line) return;

            // Ping Handling
            if (line.startsWith('PING')) {
                ws.send(`PONG ${line.split(' ')[1]}\\r\\n`);
                return;
            }

            // Regex parsing standard IRC syntax
            const match = line.match(/^(?::([^ !]+)(?:![^ ]+)? )?([A-Z0-9]+) (?:([^:]+)?)(?::(.*))?$/);
            if (!match) return;

            const [, prefix, command, rawParams, trailing] = match;
            const params = rawParams ? rawParams.trim().split(' ') : [];

            if (command === 'PRIVMSG') {
                const sender = prefix;
                const text = trailing;
                addChatMessage(sender, text);
            } else if (command === 'JOIN') {
                const sender = prefix;
                users.add(sender);
                updateUsersUI();
                addSystemMessage(`Пользователь ${sender} вошел в чат`);
            } else if (command === 'QUIT') {
                const sender = prefix;
                users.delete(sender);
                updateUsersUI();
                addSystemMessage(`Пользователь ${sender} вышел`);
            } else if (command === '353') { // NAMES list
                if (trailing) {
                    trailing.split(' ').forEach(u => u && users.add(u));
                    updateUsersUI();
                }
            } else if (command === 'NICK') {
                const oldNick = prefix;
                const newNick = trailing || params[0];
                if (oldNick === currentNick) {
                    currentNick = newNick;
                    document.getElementById('current-user').innerText = currentNick;
                }
                users.delete(oldNick);
                users.add(newNick);
                updateUsersUI();
                addSystemMessage(`${oldNick} изменил ник на ${newNick}`);
            }
        }

        function sendMessage(e) {
            e.preventDefault();
            const input = document.getElementById('message-input');
            const msg = input.value.trim();
            if (!msg || !ws) return;

            if (msg.startsWith('/')) {
                // Command support like /nick or /join
                const parts = msg.substring(1).split(' ');
                const cmd = parts[0].toUpperCase();
                if (cmd === 'NICK') ws.send(`NICK ${parts[1]}\\r\\n`);
                else if (cmd === 'JOIN') ws.send(`JOIN ${parts[1]}\\r\\n`);
            } else {
                ws.send(`PRIVMSG ${currentChannel} :${msg}\\r\\n`);
                addChatMessage(currentNick, msg, true);
            }
            input.value = '';
        }

        function addChatMessage(author, text, isSelf = false) {
            const box = document.getElementById('chat-messages');
            const div = document.createElement('div');
            div.className = "flex flex-col gap-1";
            
            const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

            div.innerHTML = `
                <div class="flex items-center gap-2">
                    <span class="font-semibold text-xs ${isSelf ? 'text-indigo-400' : 'text-emerald-400'}">${escapeHtml(author)}</span>
                    <span class="text-[10px] text-slate-500">${time}</span>
                </div>
                <div class="bg-slate-800/60 border border-slate-800 rounded-lg px-3 py-2 text-slate-200 w-fit max-w-[85%] break-words">
                    ${escapeHtml(text)}
                </div>
            `;
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }

        function addSystemMessage(text) {
            const box = document.getElementById('chat-messages');
            const div = document.createElement('div');
            div.className = "text-center my-2";
            div.innerHTML = `<span class="text-[11px] text-slate-500 bg-slate-950 px-2.5 py-1 rounded-md border border-slate-800/80">${escapeHtml(text)}</span>`;
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }

        function updateUsersUI() {
            const list = document.getElementById('users-list');
            document.getElementById('user-count').innerText = users.size;
            list.innerHTML = '';
            users.forEach(u => {
                const userEl = document.createElement('div');
                userEl.className = "flex items-center gap-2 px-2 py-1.5 rounded text-xs text-slate-300 hover:bg-slate-800/50";
                userEl.innerHTML = `<span class="w-1.5 h-1.5 rounded-full ${u === currentNick ? 'bg-indigo-400' : 'bg-slate-500'}"></span> ${escapeHtml(u)}`;
                list.appendChild(userEl);
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
