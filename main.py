import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Set

app = FastAPI(title="Python HTTP-IRC Server")

# Хранилище активных подключений и каналов
# nickname -> WebSocket
active_users: Dict[str, WebSocket] = {}
# channel_name -> Set[nickname]
channels: Dict[str, Set[str]] = {}

async def broadcast_to_channel(channel: str, message: str, sender: str = None):
    """Отправка сообщения всем участникам канала"""
    if channel in channels:
        # Форматируем сообщение по стандарту IRC
        # Пример: :nick!user@host PRIVMSG #channel :text
        irc_msg = f":{sender or 'server'}!irc@python PRIVMSG {channel} :{message}\r\n"
        
        disconnected = []
        for user in channels[channel]:
            if user in active_users:
                try:
                    await active_users[user].send_text(irc_msg)
                except Exception:
                    disconnected.append(user)
                    
        # Очистка отвалившихся
        for user in disconnected:
            await handle_disconnect(user)

async def handle_disconnect(nickname: str):
    """Очистка данных при отключении пользователя"""
    if nickname in active_users:
        del active_users[nickname]
    
    for channel, users in list(channels.items()):
        if nickname in users:
            users.remove(nickname)
            await broadcast_to_channel(channel, f"{nickname} has quit", "server")
            if not users:
                del channels[channel]

@app.websocket("/webirc")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    nickname = None
    
    try:
        while True:
            # Получаем сырую строку от IRC-клиента
            data = await websocket.receive_text()
            lines = data.strip().split('\r\n')
            
            for line in lines:
                if not line:
                    continue
                parts = line.split()
                command = parts[0].upper()

                # 1. Обработка авторизации (NICK)
                if command == "NICK" and len(parts) > 1:
                    new_nick = parts[1]
                    if new_nick in active_users:
                        await websocket.send_text(f":server 433 * {new_nick} :Nickname is already in use\r\n")
                    else:
                        nickname = new_nick
                        active_users[nickname] = websocket
                        # Отправляем стандартное приветствие IRC (RPL_WELCOME)
                        await websocket.send_text(f":server 001 {nickname} :Welcome to Python HTTP-IRC Server!\r\n")

                # 2. Обработка Ping-Pong (важно для поддержания соединения)
                elif command == "PING":
                    await websocket.send_text(f":server PONG server\r\n")

                # 3. Вход на канал (JOIN)
                elif command == "JOIN" and len(parts) > 1:
                    if not nickname:
                        continue
                    channel = parts[1]
                    if channel not in channels:
                        channels[channel] = set()
                    
                    channels[channel].add(nickname)
                    # Оповещаем канал, что зашел новый юзер
                    await websocket.send_text(f":{nickname}!irc@python JOIN {channel}\r\n")
                    await broadcast_to_channel(channel, f"joined {channel}", nickname)

                # 4. Отправка сообщений (PRIVMSG)
                elif command == "PRIVMSG" and len(parts) > 2:
                    if not nickname:
                        continue
                    target = parts[1]
                    # Извлекаем текст сообщения (все, что после двоеточия)
                    message = line.split(' :', 1)[1] if ' :' in line else ' '.join(parts[2:])
                    
                    if target.startswith("#"):
                        # Отправляем в канал всем, кроме самого себя
                        if target in channels and nickname in channels[target]:
                            # Формируем и шлем остальным
                            irc_msg = f":{nickname}!irc@python PRIVMSG {target} :{message}\r\n"
                            for user in channels[target]:
                                if user != nickname and user in active_users:
                                    await active_users[user].send_text(irc_msg)

    except WebSocketDisconnect:
        if nickname:
            await handle_disconnect(nickname)
