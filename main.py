import asyncio
import logging
import time
import re
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

# ==========================================
# ⚙️ API И НАСТРОЙКИ СЕРВЕРА
# ==========================================
SERVER_NAME = "sldshr.onrunxbuild.com"
PORT_HTTP = 8000
PORT_TCP = 6667

WELCOME_MESSAGE = r""" ___ _    ___  ___ _  _ ___ 
 / __| |  |   \| __| || | _ \
 \__ \ |__| |) |__ \ __ |   /
 |___/____|___/|___/_||_|_|_\
-[ sldshr.onrunxbuild.com ]-"""

RULES = """1. Не спамить и не флудить.
2. Быть вежливым с участниками.
3. Уважайте личные границы.
4. Запрещено обсуждение нелегальных тем."""

# ==========================================
# НАСТРОЙКИ ЛОГИРОВАНИЯ (Без диска, только ОЗУ/Консоль)
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()] # Строго в stdout
)
logger = logging.getLogger("IRC")

# ==========================================
# ВЕБ-ИНТЕРФЕЙС (HTML, CSS, JS)
# Дизайн начала 2010х + WeeChat логгинг
# ==========================================
HTML_TEMPLATE = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{SERVER_NAME} - Web IRC</title>
    <style>
        /* Стиль 2010-х для каркаса */
        body {{
            margin: 0; padding: 0;
            background: #e9eaed;
            font-family: Tahoma, Arial, sans-serif;
            font-size: 14px;
            color: #333;
            display: flex; flex-direction: column; height: 100vh;
        }}
        
        .header-2010 {{
            background: linear-gradient(to bottom, #4a6692, #3b5379);
            color: white;
            padding: 10px 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
            border-bottom: 1px solid #2a3e5e;
            display: flex; justify-content: space-between; align-items: center;
            z-index: 10;
        }}
        .header-2010 h1 {{ margin: 0; font-size: 18px; text-shadow: 1px 1px 0 #222; }}
        
        .main-container {{
            display: flex; flex: 1; overflow: hidden;
            background: #fff;
            margin: 10px;
            border: 1px solid #ccc;
            border-radius: 4px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}

        /* WeeChat стиль для лога сообщений */
        .chat-area {{
            flex: 1; display: flex; flex-direction: column;
            background: #111; color: #d0d0d0;
            font-family: Consolas, Monaco, "Courier New", monospace;
        }}
        
        #irc-log-container {{
            flex: 1; overflow-y: auto; padding: 10px;
        }}
        
        /* Сетка сообщения WeeChat: Время | Ник | Текст */
        .irc-msg {{ display: flex; margin-bottom: 2px; line-height: 1.4; word-wrap: break-word; }}
        .msg-time {{ color: #5f87af; width: 65px; flex-shrink: 0; }}
        .msg-nick {{ width: 120px; text-align: right; margin-right: 15px; flex-shrink: 0; font-weight: bold; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .msg-text {{ flex: 1; }}
        
        .sys-msg {{ color: #00af5f; }}
        .err-msg {{ color: #d75f00; font-weight: bold; }}
        .mention {{ background-color: #5f5f00; color: #ffff00; padding: 0 4px; border-radius: 2px; }}

        /* Сайдбар (Участники) */
        .sidebar {{
            width: 220px; background: #f9f9f9;
            border-left: 1px solid #ddd;
            display: flex; flex-direction: column;
        }}
        .sidebar-header {{
            background: linear-gradient(to bottom, #f0f0f0, #e0e0e0);
            padding: 8px; border-bottom: 1px solid #ccc;
            font-weight: bold; text-align: center; color: #555;
            text-shadow: 1px 1px 0 #fff;
        }}
        .user-list {{
            list-style: none; padding: 0; margin: 0; overflow-y: auto; flex: 1;
        }}
        .user-list li {{
            padding: 6px 12px; cursor: pointer; border-bottom: 1px solid #eee;
            transition: background 0.2s;
        }}
        .user-list li:hover {{ background: #e6eef4; }}

        /* Панель ввода */
        .input-area {{
            background: #222; border-top: 1px solid #444; padding: 8px;
            display: flex; align-items: center;
        }}
        .status-prefix {{ color: #00afaf; font-weight: bold; margin-right: 10px; font-family: Consolas, monospace; }}
        #typing-indicator {{ color: #00ff00; font-size: 12px; font-style: italic; margin-right: 10px; width: 150px; text-align: right; }}
        
        .chat-input {{
            flex: 1; background: #333; color: #fff; border: 1px solid #555;
            padding: 6px 10px; font-family: Consolas, monospace;
            border-radius: 3px; outline: none;
        }}
        .chat-input:focus {{ border-color: #5f87ff; background: #1a1a1a; }}
        
        .ascii-art {{ color: #00afaf; font-weight: bold; white-space: pre; line-height: 1.2; margin-bottom: 10px; }}
        
        /* Модальное окно входа */
        #login-overlay {{
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.8); z-index: 100;
            display: flex; align-items: center; justify-content: center;
        }}
        .login-box {{
            background: #fff; padding: 20px; border-radius: 6px; width: 350px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.5); border: 1px solid #999;
        }}
        .login-box h2 {{ margin-top: 0; font-size: 18px; color: #3b5379; border-bottom: 1px solid #ccc; padding-bottom: 10px; }}
        .login-box input {{ width: 100%; padding: 8px; margin: 10px 0; border: 1px solid #ccc; border-radius: 3px; box-sizing: border-box; }}
        .btn-2010 {{
            background: linear-gradient(to bottom, #5cb85c, #4cae4c);
            border: 1px solid #398439; color: white; padding: 8px 15px;
            cursor: pointer; border-radius: 3px; font-weight: bold; text-shadow: 1px 1px 0 rgba(0,0,0,0.2);
            width: 100%;
        }}
        .btn-2010:hover {{ background: linear-gradient(to bottom, #4cae4c, #398439); }}
        
        ::-webkit-scrollbar {{ width: 8px; }}
        ::-webkit-scrollbar-track {{ background: #222; }}
        ::-webkit-scrollbar-thumb {{ background: #555; border-radius: 4px; }}
        ::-webkit-scrollbar-thumb:hover {{ background: #777; }}
    </style>
</head>
<body>

    <!-- Экран Входа -->
    <div id="login-overlay">
        <div class="login-box">
            <h2>Подключение к {SERVER_NAME}</h2>
            <div style="font-family: monospace; color: #5cb85c; font-size: 10px; white-space: pre; text-align: center; margin-bottom: 10px;">
{WELCOME_MESSAGE}
            </div>
            <input type="text" id="login-nick" placeholder="Ваш никнейм" value="Guest" maxlength="15" onkeypress="if(event.key === 'Enter') connectWS()">
            <button class="btn-2010" onclick="connectWS()">Войти в чат</button>
        </div>
    </div>

    <!-- Шапка -->
    <div class="header-2010">
        <h1>{SERVER_NAME}</h1>
        <div>Порт: 6667 (TCP) / WebSockets</div>
    </div>

    <!-- Основной каркас -->
    <div class="main-container">
        
        <!-- Окно чата -->
        <div class="chat-area">
            <div id="irc-log-container"></div>
            
            <div class="input-area">
                <div class="status-prefix" id="chan-prefix">#general &gt;</div>
                <div id="typing-indicator"></div>
                <input type="text" id="irc-input" class="chat-input" placeholder="Введите сообщение или /help..." autocomplete="off">
            </div>
        </div>

        <!-- Сайдбар участников -->
        <div class="sidebar">
            <div class="sidebar-header">Участники (<span id="user-count">0</span>)</div>
            <ul class="user-list" id="user-list"></ul>
        </div>
        
    </div>

    <script>
        let ws = null;
        let currentNick = '';
        let currentChannel = '#general';
        let users = new Set();
        let customColors = {{}}; 
        let typingUsers = new Set();
        let typingTimeouts = {{}};
        let lastTypingSend = 0;

        const NICK_COLORS = ['#ff5f5f', '#00af5f', '#d78700', '#5f87ff', '#af77a7', '#00afaf', '#d75ffd', '#5fd700'];

        function getNickColor(nick) {{
            if (customColors[nick]) return customColors[nick];
            let hash = 0;
            for (let i = 0; i < nick.length; i++) hash = nick.charCodeAt(i) + ((hash << 5) - hash);
            return NICK_COLORS[Math.abs(hash) % NICK_COLORS.length];
        }}

        function formatTime() {{
            const d = new Date();
            return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
        }}

        function escapeHtml(str) {{
            return str.replace(/[&<>'"]/g, tag => ({{
                '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
            }}[tag]));
        }}

        function renderMessage(text) {{
            let escaped = escapeHtml(text);
            // Подсветка упоминаний
            if (currentNick && escaped.includes(currentNick)) {{
                const regex = new RegExp(`(\\b${{currentNick}}\\b)`, 'gi');
                escaped = escaped.replace(regex, '<span class="mention">$1</span>');
            }}
            return escaped;
        }}

        function appendLog(nick, message, type='msg', rawColor=null) {{
            const logContainer = document.getElementById('irc-log-container');
            const row = document.createElement('div');
            row.className = 'irc-msg';
            
            const timeCol = document.createElement('div');
            timeCol.className = 'msg-time';
            timeCol.innerText = formatTime();
            
            const nickCol = document.createElement('div');
            nickCol.className = 'msg-nick';
            
            const textCol = document.createElement('div');
            textCol.className = 'msg-text';

            if (type === 'sys') {{
                nickCol.innerText = '--';
                textCol.className += ' sys-msg';
                textCol.innerHTML = renderMessage(message);
            }} else if (type === 'err') {{
                nickCol.innerText = '!!';
                textCol.className += ' err-msg';
                textCol.innerHTML = renderMessage(message);
            }} else if (type === 'action') {{
                nickCol.innerText = '*';
                const c = rawColor || getNickColor(nick);
                textCol.innerHTML = `<span style="color: ${{c}}; font-weight: bold;">${{escapeHtml(nick)}}</span> ${{renderMessage(message)}}`;
            }} else {{
                nickCol.innerText = `<${{nick}}>`;
                nickCol.style.color = rawColor || getNickColor(nick);
                textCol.innerHTML = renderMessage(message);
            }}

            row.appendChild(timeCol);
            row.appendChild(nickCol);
            row.appendChild(textCol);
            logContainer.appendChild(row);
            logContainer.scrollTop = logContainer.scrollHeight;
        }}

        function appendAscii(text) {{
            const logContainer = document.getElementById('irc-log-container');
            const el = document.createElement('div');
            el.className = 'ascii-art';
            el.innerText = text;
            logContainer.appendChild(el);
            logContainer.scrollTop = logContainer.scrollHeight;
        }}

        function updateUserList() {{
            const ul = document.getElementById('user-list');
            document.getElementById('user-count').innerText = users.size;
            ul.innerHTML = '';
            
            // Сортировка: сам юзер первый, остальные по алфавиту
            const sortedUsers = Array.from(users).sort((a, b) => {{
                if (a === currentNick) return -1;
                if (b === currentNick) return 1;
                return a.localeCompare(b);
            }});

            sortedUsers.forEach(u => {{
                const li = document.createElement('li');
                li.innerText = u;
                li.style.color = getNickColor(u);
                if (u === currentNick) li.style.fontWeight = 'bold';
                
                // Клик для упоминания
                li.onclick = () => {{
                    const input = document.getElementById('irc-input');
                    const mention = u + ', ';
                    if (!input.value.includes(mention)) {{
                        input.value = input.value ? input.value + ' ' + mention : mention;
                    }}
                    input.focus();
                }};
                ul.appendChild(li);
            }});
        }}

        function updateTyping() {{
            const ind = document.getElementById('typing-indicator');
            if (typingUsers.size === 0) {{
                ind.innerText = '';
            }} else {{
                const arr = Array.from(typingUsers);
                if (arr.length === 1) ind.innerText = arr[0] + ' печатает...';
                else if (arr.length === 2) ind.innerText = arr.join(' и ') + ' печатают...';
                else ind.innerText = 'Несколько человек печатают...';
            }}
        }}

        function connectWS() {{
            const nickInput = document.getElementById('login-nick').value.trim() || 'Guest';
            currentNick = nickInput;
            
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${{protocol}}//${{window.location.host}}/ws`;
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = () => {{
                document.getElementById('login-overlay').style.display = 'none';
                ws.send(`NICK ${{currentNick}}`);
                ws.send(`USER ${{currentNick}} 0 * :Web Client`);
            }};
            
            ws.onmessage = (event) => {{
                const lines = event.data.split('\\r\\n');
                for (let line of lines) {{
                    if (!line) continue;
                    parseIRCLine(line);
                }}
            }};
            
            ws.onclose = () => {{
                appendLog('Система', 'Соединение с сервером потеряно. Обновите страницу.', 'err');
                document.getElementById('irc-input').disabled = true;
            }};
        }}

        function parseIRCLine(line) {{
            let prefix = '';
            let command = '';
            let params = [];
            let trailing = '';

            if (line.startsWith(':')) {{
                const spaceIdx = line.indexOf(' ');
                prefix = line.substring(1, spaceIdx);
                line = line.substring(spaceIdx + 1);
            }}

            const trailingIdx = line.indexOf(' :');
            if (trailingIdx !== -1) {{
                trailing = line.substring(trailingIdx + 2);
                params = line.substring(0, trailingIdx).split(' ');
            }} else {{
                params = line.split(' ');
            }}
            command = params[0];
            params = params.slice(1);

            const senderNick = prefix.split('!')[0];

            if (command === 'PING') {{
                ws.send(`PONG :${{trailing || params[0]}}`);
            }} 
            else if (command === '001') {{
                appendAscii(`{WELCOME_MESSAGE}`);
                appendLog('Сервер', trailing, 'sys');
                ws.send(`JOIN ${{currentChannel}}`);
            }}
            else if (command === '353') {{ // Список юзеров в канале
                const channelUsers = trailing.split(' ');
                channelUsers.forEach(u => {{
                    if(u) users.add(u.replace(/^[~&@%+]/, '')); // убираем префиксы модераторов
                }});
                updateUserList();
            }}
            else if (command === 'JOIN') {{
                users.add(senderNick);
                updateUserList();
                appendLog('Сервер', `${{senderNick}} присоединился к каналу`, 'sys');
            }}
            else if (command === 'PART' || command === 'QUIT') {{
                users.delete(senderNick);
                typingUsers.delete(senderNick);
                updateTyping();
                updateUserList();
                appendLog('Сервер', `${{senderNick}} покинул канал (${{trailing}})`, 'sys');
            }}
            else if (command === 'NICK') {{
                if (senderNick === currentNick) currentNick = trailing;
                users.delete(senderNick);
                users.add(trailing);
                
                // Переносим кастомный цвет на новый ник
                if (customColors[senderNick]) {{
                    customColors[trailing] = customColors[senderNick];
                    delete customColors[senderNick];
                }}
                
                updateUserList();
                appendLog('Сервер', `${{senderNick}} теперь известен как ${{trailing}}`, 'sys');
            }}
            else if (command === 'PRIVMSG') {{
                const target = params[0];
                const msg = trailing;
                
                // Обработка статуса печатания
                typingUsers.delete(senderNick);
                updateTyping();

                if (msg.startsWith('\x01ACTION ') && msg.endsWith('\x01')) {{
                    appendLog(senderNick, msg.substring(8, msg.length - 1), 'action');
                }} else if (target === currentNick) {{
                    appendLog(senderNick, `[ЛС] ${{msg}}`, 'msg');
                }} else {{
                    appendLog(senderNick, msg, 'msg');
                }}
            }}
            else if (command === 'TYPING') {{
                if (senderNick !== currentNick) {{
                    typingUsers.add(senderNick);
                    updateTyping();
                    if (typingTimeouts[senderNick]) clearTimeout(typingTimeouts[senderNick]);
                    typingTimeouts[senderNick] = setTimeout(() => {{
                        typingUsers.delete(senderNick);
                        updateTyping();
                    }}, 3000);
                }}
            }}
            else if (command === 'SETCOLOR') {{
                customColors[senderNick] = trailing || params[0];
                updateUserList();
            }}
            else if (command === '372') {{ // MOTD / Rules
                appendLog('Сервер', trailing, 'sys');
            }}
            else if (command === 'NOTICE') {{
                appendLog('Сервер', trailing, 'sys');
            }}
        }}

        // Обработка ввода
        document.getElementById('irc-input').addEventListener('keypress', function(e) {{
            if (e.key === 'Enter') {{
                const val = this.value.trim();
                if (!val) return;

                if (val.startsWith('/')) {{
                    const args = val.split(' ');
                    const cmd = args[0].toLowerCase();
                    
                    if (cmd === '/nick' && args[1]) {{
                        ws.send(`NICK ${{args[1]}}`);
                        if (args[2] && args[2].startsWith('#')) {{
                            ws.send(`SETCOLOR ${{args[2]}}`);
                        }}
                    }} else if (cmd === '/join' && args[1]) {{
                        currentChannel = args[1];
                        document.getElementById('chan-prefix').innerText = currentChannel + ' >';
                        users.clear();
                        ws.send(`JOIN ${{args[1]}}`);
                    }} else if (cmd === '/me' && args.length > 1) {{
                        const action = args.slice(1).join(' ');
                        ws.send(`PRIVMSG ${{currentChannel}} :\\x01ACTION ${{action}}\\x01`);
                        appendLog(currentNick, action, 'action');
                    }} else if ((cmd === '/msg' || cmd === '/w') && args.length > 2) {{
                        const target = args[1];
                        const msg = args.slice(2).join(' ');
                        ws.send(`PRIVMSG ${{target}} :${{msg}}`);
                        appendLog(currentNick, `[ЛС -> ${{target}}] ${{msg}}`, 'msg');
                    }} else if (cmd === '/rules') {{
                        ws.send(`RULES`);
                    }} else if (cmd === '/clear') {{
                        document.getElementById('irc-log-container').innerHTML = '';
                    }} else {{
                        appendLog('Система', 'Неизвестная команда. Доступны: /nick [имя] [#цвет], /join #канал, /me действие, /msg ник текст, /rules, /clear', 'sys');
                    }}
                }} else {{
                    ws.send(`PRIVMSG ${{currentChannel}} :${{val}}`);
                    appendLog(currentNick, val, 'msg');
                }}
                this.value = '';
                
                // Сбрасываем свой статус "печатает"
                lastTypingSend = 0; 
            }}
        }});

        // Отправка статуса TYPING
        document.getElementById('irc-input').addEventListener('input', function() {{
            const now = Date.now();
            if (now - lastTypingSend > 2000 && ws && ws.readyState === WebSocket.OPEN) {{
                ws.send(`TYPING ${{currentChannel}}`);
                lastTypingSend = now;
            }}
        }});

        // Фокус на инпут при клике на окно чата
        document.getElementById('irc-log-container').addEventListener('click', () => {{
            const sel = window.getSelection();
            if (!sel.toString()) document.getElementById('irc-input').focus();
        }});
    </script>
</body>
</html>
"""

# ==========================================
# ВНУТРЕННЕЕ СОСТОЯНИЕ СЕРВЕРА (Только ОЗУ)
# ==========================================
class ServerState:
    def __init__(self):
        self.channels = {}  # "#channel_name": set(IRCClient)
        self.users = {}     # "nickname": IRCClient

state = ServerState()

class IRCClient:
    def __init__(self, writer=None, ws: WebSocket=None):
        self.writer = writer
        self.ws = ws
        self.nick = None
        self.username = None
        self.channels = set()
        self.last_msg_time = time.time()
        self.addr = "web.client" if ws else writer.get_extra_info('peername')[0]

    async def send(self, message: str):
        try:
            if self.ws:
                await self.ws.send_text(message)
            elif self.writer:
                self.writer.write((message + "\r\n").encode("utf-8"))
                await self.writer.drain()
        except Exception:
            pass # Игнорируем ошибки отключенных клиентов

async def broadcast_channel(channel: str, message: str, exclude: IRCClient = None):
    if channel in state.channels:
        # Собираем задачи для конкурентной отправки
        tasks = []
        for client in state.channels[channel]:
            if client != exclude:
                tasks.append(client.send(message))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

async def disconnect_client(client: IRCClient):
    if client.nick and client.nick in state.users:
        del state.users[client.nick]
        quit_msg = f":{client.nick}!{client.username}@{client.addr} QUIT :Client disconnected"
        
        for channel in list(client.channels):
            if channel in state.channels:
                if client in state.channels[channel]:
                    state.channels[channel].remove(client)
                await broadcast_channel(channel, quit_msg)
                # Удаляем пустые каналы для экономии ОЗУ
                if not state.channels[channel]:
                    del state.channels[channel]

    if client.writer:
        try:
            client.writer.close()
            await client.writer.wait_closed()
        except:
            pass

async def handle_irc_message(client: IRCClient, line: str):
    # ANTI-FLOOD защита (Экономия CPU)
    now = time.time()
    if now - client.last_msg_time < 0.1:
        await asyncio.sleep(0.5)
    client.last_msg_time = now

    line = line.strip()
    if not line: return

    parts = line.split(" ", 1)
    command = parts[0].upper()
    args_str = parts[1] if len(parts) > 1 else ""
    
    args = []
    trailing = None
    if " :" in args_str:
        main_args, trailing = args_str.split(" :", 1)
        args = main_args.split()
        args.append(trailing)
    else:
        args = args_str.split()

    # --- Обработка команд ---
    if command == "CAP":
        # Заглушка для современных клиентов типа The Lounge/HexChat
        await client.send(f":{SERVER_NAME} CAP * LS :")

    elif command == "NICK":
        if not args: return
        new_nick = args[0][:15] # Ограничение длины (ОЗУ)
        new_nick = re.sub(r'[^a-zA-Z0-9\[\]\\`_\^\{\|\}-]', '', new_nick)

        if new_nick in state.users and state.users[new_nick] != client:
            await client.send(f":{SERVER_NAME} 433 * {new_nick} :Nickname is already in use.")
            return

        old_nick = client.nick
        client.nick = new_nick
        state.users[new_nick] = client
        
        if old_nick:
            del state.users[old_nick]
            msg = f":{old_nick}!{client.username}@{client.addr} NICK :{new_nick}"
            await client.send(msg)
            for channel in client.channels:
                await broadcast_channel(channel, msg, exclude=client)

    elif command == "USER":
        if not args or len(args) < 1: return
        client.username = args[0][:10]
        if client.nick:
            # Успешное подключение - отправляем рукопожатие
            await client.send(f":{SERVER_NAME} 001 {client.nick} :Welcome to the Internet Relay Network {client.nick}")
            await client.send(f":{SERVER_NAME} 002 {client.nick} :Your host is {SERVER_NAME}, running version python-irc-1.0")
            await client.send(f":{SERVER_NAME} 003 {client.nick} :This server was created today")
            await client.send(f":{SERVER_NAME} 004 {client.nick} {SERVER_NAME} python-irc-1.0 o o")
            await client.send(f":{SERVER_NAME} 376 {client.nick} :End of /MOTD command.")

    elif command == "PING":
        pong_target = args[0] if args else SERVER_NAME
        await client.send(f":{SERVER_NAME} PONG {SERVER_NAME} :{pong_target}")

    elif command == "JOIN":
        if not args or not client.nick: return
        channel = args[0].split(",")[0]
        if not channel.startswith("#"): channel = "#" + channel
        
        client.channels.add(channel)
        if channel not in state.channels:
            state.channels[channel] = set()
        state.channels[channel].add(client)

        join_msg = f":{client.nick}!{client.username}@{client.addr} JOIN :{channel}"
        await broadcast_channel(channel, join_msg)

        # Отправляем список пользователей (NAMES)
        users_in_chan = " ".join([c.nick for c in state.channels[channel]])
        await client.send(f":{SERVER_NAME} 353 {client.nick} = {channel} :{users_in_chan}")
        await client.send(f":{SERVER_NAME} 366 {client.nick} {channel} :End of /NAMES list.")

    elif command == "PRIVMSG":
        if len(args) < 2 or not client.nick: return
        target = args[0]
        msg = args[1]
        
        out_msg = f":{client.nick}!{client.username}@{client.addr} PRIVMSG {target} :{msg}"
        if target.startswith("#"):
            await broadcast_channel(target, out_msg, exclude=client)
        elif target in state.users:
            await state.users[target].send(out_msg)

    elif command == "PART":
        if not args or not client.nick: return
        channel = args[0]
        if channel in client.channels:
            client.channels.remove(channel)
            if channel in state.channels and client in state.channels[channel]:
                state.channels[channel].remove(client)
            await broadcast_channel(channel, f":{client.nick}!{client.username}@{client.addr} PART {channel}")

    elif command == "QUIT":
        await disconnect_client(client)

    # Кастомные команды для Web-клиента
    elif command == "SETCOLOR":
        if args and client.nick:
            color = args[0][:7]
            notified = set()
            for channel in client.channels:
                if channel in state.channels:
                    for c in state.channels[channel]:
                        if c not in notified:
                            await c.send(f":{client.nick} SETCOLOR :{color}")
                            notified.add(c)

    elif command == "TYPING":
        if args and client.nick:
            target = args[0]
            if target.startswith("#"):
                await broadcast_channel(target, f":{client.nick} TYPING {target}", exclude=client)
                
    elif command == "RULES":
        if client.nick:
            for rule_line in RULES.split('\n'):
                await client.send(f":{SERVER_NAME} 372 {client.nick} :- {rule_line}")
            await client.send(f":{SERVER_NAME} 376 {client.nick} :End of RULES.")


# ==========================================
# FASTAPI & ASGI APP
# ==========================================
async def handle_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    client = IRCClient(writer=writer)
    try:
        while True:
            data = await reader.readline()
            if not data: break
            line = data.decode("utf-8", errors="ignore").strip()
            await handle_irc_message(client, line)
    except Exception as e:
        pass
    finally:
        await disconnect_client(client)

async def lifespan(app: FastAPI):
    # При старте FastAPI запускаем фоновый сырой TCP сервер
    server = await asyncio.start_server(handle_tcp_client, '0.0.0.0', PORT_TCP)
    logger.info(f"Raw TCP IRC Server started on port {PORT_TCP}")
    yield
    # Очистка при завершении
    server.close()
    await server.wait_closed()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def get_index():
    return HTMLResponse(HTML_TEMPLATE)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client = IRCClient(ws=websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Браузер может прислать склеенные сообщения, разбиваем
            for line in data.split('\n'):
                if line.strip():
                    await handle_irc_message(client, line)
    except WebSocketDisconnect:
        await disconnect_client(client)
    except Exception as e:
        await disconnect_client(client)

if __name__ == "__main__":
    logger.info(f"Starting ASGI Web UI on port {PORT_HTTP}...")
    # Запускаем Uvicorn. Настройка loop="asyncio" важна для корректной работы фоновых TCP сокетов
    uvicorn.run("irc_server:app", host="0.0.0.0", port=PORT_HTTP, loop="asyncio", log_level="warning")
