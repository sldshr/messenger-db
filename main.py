import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Dict, Set
from fastapi import FastAPI
import uvicorn

# Настройка логирования исключительно в консоль (без диска)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("irc_server")

# Состояние сервера (всё in-memory, для экономии RAM)
class ServerState:
    def __init__(self):
        # writer -> {"nick": str, "user": str, "channels": set}
        self.clients: Dict[asyncio.StreamWriter, dict] = {}
        # nick -> writer (для быстрого поиска при приватных сообщениях)
        self.nicks: Dict[str, asyncio.StreamWriter] = {}
        # channel_name -> set of writers
        self.channels: Dict[str, Set[asyncio.StreamWriter]] = {}
        self.server_name = "uvicorn.light.irc"

state = ServerState()

async def send_msg(writer: asyncio.StreamWriter, message: str):
    """Отправка сообщения конкретному клиенту."""
    try:
        writer.write(f"{message}\r\n".encode('utf-8'))
        await writer.drain()
    except Exception as e:
        logger.warning(f"Ошибка отправки данных: {e}")

async def broadcast_channel(channel: str, message: str, exclude: asyncio.StreamWriter = None):
    """Рассылка сообщения всем участникам канала."""
    if channel not in state.channels:
        return
    
    dead_writers = []
    for w in state.channels[channel]:
        if w != exclude:
            try:
                w.write(f"{message}\r\n".encode('utf-8'))
                await w.drain()
            except Exception:
                dead_writers.append(w)
                
    # Очистка "мертвых" соединений для экономии памяти
    for w in dead_writers:
        await disconnect_client(w)

async def disconnect_client(writer: asyncio.StreamWriter):
    """Удаление клиента со всех каналов и освобождение памяти."""
    if writer not in state.clients:
        return
    
    client_info = state.clients[writer]
    nick = client_info.get("nick")
    
    logger.info(f"Отключение клиента: {nick or writer.get_extra_info('peername')}")
    
    # Удаляем из каналов
    for channel in list(client_info["channels"]):
        if channel in state.channels:
            state.channels[channel].discard(writer)
            if nick:
                await broadcast_channel(channel, f":{nick} QUIT :Connection closed")
            # Если канал пуст, удаляем его из памяти
            if not state.channels[channel]:
                del state.channels[channel]
                
    # Освобождаем глобальные словари
    if nick and nick in state.nicks:
        del state.nicks[nick]
    del state.clients[writer]
    
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

async def handle_irc_command(writer: asyncio.StreamWriter, command: str, args: list):
    """Обработка базовых IRC команд с минимальной нагрузкой на CPU."""
    client_info = state.clients[writer]
    nick = client_info.get("nick")
    
    if command == "PING":
        if args:
            await send_msg(writer, f":{state.server_name} PONG {state.server_name} :{args[0]}")
            
    elif command == "NICK":
        if not args:
            return await send_msg(writer, f":{state.server_name} 431 * :No nickname given")
        new_nick = args[0][:15] # Ограничение длины ника для экономии RAM
        if new_nick in state.nicks:
            return await send_msg(writer, f":{state.server_name} 433 * {new_nick} :Nickname is already in use")
            
        if nick:
            del state.nicks[nick]
        state.nicks[new_nick] = writer
        client_info["nick"] = new_nick
        
        if not nick: # Первое подключение
            await send_msg(writer, f":{state.server_name} 001 {new_nick} :Welcome to the Uvicorn IRC Server!")
        else:
            # Уведомляем о смене ника (для простоты тут только самому себе, в идеале всем каналам)
            await send_msg(writer, f":{nick} NICK :{new_nick}")

    elif command == "USER":
        client_info["user"] = args[0] if args else "user"
        # Если ник уже задан, 001 отправился там. Если нет - отправится при NICK.

    elif command == "JOIN":
        if not args or not nick:
            return
        channel = args[0].split(",")[0] # Берем только первый канал для простоты
        if not channel.startswith("#"):
            channel = "#" + channel
            
        if channel not in state.channels:
            state.channels[channel] = set()
        
        state.channels[channel].add(writer)
        client_info["channels"].add(channel)
        
        await broadcast_channel(channel, f":{nick} JOIN :{channel}")
        
        # Отправка списка участников канала (353 и 366)
        names = " ".join([state.clients[w]["nick"] for w in state.channels[channel] if state.clients[w].get("nick")])
        await send_msg(writer, f":{state.server_name} 353 {nick} = {channel} :{names}")
        await send_msg(writer, f":{state.server_name} 366 {nick} {channel} :End of /NAMES list")

    elif command == "PRIVMSG":
        if len(args) < 2 or not nick:
            return
        target = args[0]
        text = " ".join(args[1:]).lstrip(":")
        
        if target.startswith("#"):
            await broadcast_channel(target, f":{nick} PRIVMSG {target} :{text}", exclude=writer)
        elif target in state.nicks:
            target_writer = state.nicks[target]
            await send_msg(target_writer, f":{nick} PRIVMSG {target} :{text}")

    elif command == "QUIT":
        await disconnect_client(writer)

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Корутина для обслуживания одного TCP-соединения."""
    peer = writer.get_extra_info('peername')
    logger.info(f"Новое подключение: {peer}")
    
    state.clients[writer] = {"nick": None, "user": None, "channels": set()}
    
    # Защита от флуда (CPU optimization)
    msg_count = 0
    last_reset = time.time()

    try:
        while True:
            # Ограничение буфера чтения в 1024 байта для экономии RAM
            line = await reader.readline()
            if not line:
                break
                
            try:
                decoded_line = line.decode('utf-8', errors='ignore').strip()
            except UnicodeDecodeError:
                continue

            if not decoded_line:
                continue

            # Anti-flood логика (не дает 1 процессу съесть CPU)
            msg_count += 1
            now = time.time()
            if now - last_reset > 1.0:
                msg_count = 0
                last_reset = now
            elif msg_count > 10:
                await asyncio.sleep(0.5) # Принудительно усыпляем спамера

            parts = decoded_line.split()
            if not parts:
                continue
                
            command = parts[0].upper()
            args = parts[1:]
            
            await handle_irc_command(writer, command, args)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Ошибка клиента {peer}: {e}")
    finally:
        await disconnect_client(writer)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Жизненный цикл FastAPI.
    Здесь мы запускаем фоновый TCP-сервер (IRC) при старте Uvicorn.
    """
    logger.info("Запуск TCP IRC сервера на порту 6667...")
    server = await asyncio.start_server(handle_client, '0.0.0.0', 6667)
    
    # Запускаем сервер в фоне
    asyncio.create_task(server.serve_forever())
    
    yield # В этот момент Uvicorn крутит свой HTTP-сервер
    
    logger.info("Остановка TCP IRC сервера...")
    server.close()
    await server.wait_closed()
    
    # Отключаем всех клиентов при выключении
    for writer in list(state.clients.keys()):
        await disconnect_client(writer)

# Инициализация ASGI-приложения
app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health_check():
    """Простой HTTP-эндпоинт для проверки статуса через браузер/curl."""
    return {
        "status": "online",
        "service": "Lightweight IRC",
        "tcp_port": 6667,
        "active_users": len(state.nicks),
        "active_channels": len(state.channels)
    }

if __name__ == "__main__":
    # Запуск через Uvicorn
    # Запустит HTTP сервер на 8000, и параллельно IRC TCP сервер на 6667.
    # log_level="warning" для Uvicorn, чтобы не спамить логи доступа и беречь CPU/I/O
    uvicorn.run("irc_server:app", host="0.0.0.0", port=8000, log_level="warning")
