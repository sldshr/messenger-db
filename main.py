import asyncio
import logging
import re
import time

# Настройка логирования для отслеживания событий сервера
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("IRC_Server")

SERVER_NAME = "python.irc.server"
SERVER_VERSION = "1.0"
CREATION_TIME = time.strftime("%Y-%m-%d %H:%M:%S")

class Channel:
    """Представляет IRC канал (чат-комнату)."""
    def __init__(self, name):
        self.name = name
        self.clients = set() # Множество подключенных клиентов

    def add_client(self, client):
        self.clients.add(client)

    def remove_client(self, client):
        if client in self.clients:
            self.clients.remove(client)

    async def broadcast(self, message, exclude_client=None):
        """Отправка сообщения всем пользователям в канале."""
        for client in self.clients:
            if client != exclude_client:
                await client.send_raw(message)

class Client:
    """Представляет подключенного IRC клиента."""
    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.addr = writer.get_extra_info('peername')
        
        self.nickname = None
        self.username = None
        self.realname = None
        self.registered = False
        
        self.channels = set() # Каналы, в которых находится клиент

    @property
    def prefix(self):
        """Формирует префикс клиента (nick!user@host)."""
        nick = self.nickname or "*"
        user = self.username or "unknown"
        host = self.addr[0]
        return f"{nick}!{user}@{host}"

    async def send_raw(self, message):
        """Отправка сырого сообщения клиенту."""
        if not message.endswith("\r\n"):
            message += "\r\n"
        try:
            self.writer.write(message.encode('utf-8'))
            await self.writer.drain()
            logger.debug(f"Отправлено {self.nickname or self.addr}: {message.strip()}")
        except Exception as e:
            logger.error(f"Ошибка отправки клиенту {self.addr}: {e}")

    async def send_numeric(self, code, text):
        """Отправка числового ответа (numeric reply)."""
        target = self.nickname or "*"
        message = f":{SERVER_NAME} {code:03d} {target} {text}"
        await self.send_raw(message)

    async def handle(self):
        """Основной цикл обработки входящих сообщений клиента."""
        logger.info(f"Новое подключение от {self.addr}")
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break # Клиент отключился
                
                message = line.decode('utf-8', errors='ignore').strip()
                if message:
                    logger.debug(f"Получено от {self.nickname or self.addr}: {message}")
                    await self.process_command(message)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Ошибка при работе с клиентом {self.addr}: {e}")
        finally:
            await self.disconnect()

    async def process_command(self, message):
        """Разбор и обработка IRC команд."""
        parts = message.split(' ')
        command = parts[0].upper()
        
        # Разделение аргументов (включая trailing параметры, начинающиеся с ':')
        args = []
        trailing = None
        for i, part in enumerate(parts[1:]):
            if part.startswith(':'):
                trailing = ' '.join(parts[1+i:])[1:]
                break
            else:
                args.append(part)
        
        if trailing is not None:
            args.append(trailing)

        handler_name = f"cmd_{command}"
        handler = getattr(self, handler_name, self.cmd_unknown)
        await handler(args)

    async def cmd_NICK(self, args):
        if not args:
            await self.send_numeric(431, ":No nickname given")
            return
        
        new_nick = args[0]
        # Проверка на занятость ника
        if new_nick in self.server.clients and self.server.clients[new_nick] != self:
            await self.send_numeric(433, f"{new_nick} :Nickname is already in use")
            return

        old_prefix = self.prefix if self.nickname else None
        
        if self.nickname:
            del self.server.clients[self.nickname]
        
        self.nickname = new_nick
        self.server.clients[self.nickname] = self

        if old_prefix:
            # Оповещение других пользователей о смене ника
            msg = f":{old_prefix} NICK :{new_nick}"
            await self.send_raw(msg)
            # Уведомляем каналы
            for channel in self.channels:
                await channel.broadcast(msg, exclude_client=self)
        
        await self.check_registration()

    async def cmd_USER(self, args):
        if len(args) < 4:
            await self.send_numeric(461, "USER :Not enough parameters")
            return
        if self.registered:
            await self.send_numeric(462, ":Unauthorized command (already registered)")
            return
            
        self.username = args[0]
        self.realname = args[3]
        await self.check_registration()

    async def check_registration(self):
        if not self.registered and self.nickname and self.username:
            self.registered = True
            logger.info(f"Клиент {self.addr} зарегистрирован как {self.prefix}")
            
            # Отправка стандартного приветствия IRC (RPL_WELCOME, etc.)
            await self.send_numeric(1, f":Welcome to the {SERVER_NAME} IRC Network {self.prefix}")
            await self.send_numeric(2, f":Your host is {SERVER_NAME}, running version {SERVER_VERSION}")
            await self.send_numeric(3, f":This server was created {CREATION_TIME}")
            await self.send_numeric(4, f"{SERVER_NAME} {SERVER_VERSION} o o")
            await self.send_numeric(422, ":MOTD File is missing") # Пропускаем Message of the Day

    async def cmd_PING(self, args):
        if not args:
            await self.send_numeric(409, ":No origin specified")
            return
        await self.send_raw(f":{SERVER_NAME} PONG {SERVER_NAME} :{args[0]}")

    async def cmd_unknown(self, args):
        # Игнорируем неизвестные команды (или можно отправлять 421 ERR_UNKNOWNCOMMAND)
        pass

    async def cmd_JOIN(self, args):
        if not self.registered:
            return
        if not args:
            await self.send_numeric(461, "JOIN :Not enough parameters")
            return
            
        channel_names = args[0].split(',')
        for chan_name in channel_names:
            if not chan_name.startswith('#'):
                chan_name = '#' + chan_name
                
            channel = self.server.get_or_create_channel(chan_name)
            channel.add_client(self)
            self.channels.add(channel)
            
            # Сообщаем всем в канале (включая самого себя), что пользователь вошел
            join_msg = f":{self.prefix} JOIN :{chan_name}"
            await channel.broadcast(join_msg)
            
            # Отправляем список пользователей в канале (RPL_NAMREPLY)
            names = " ".join([c.nickname for c in channel.clients])
            await self.send_numeric(353, f"= {chan_name} :{names}")
            await self.send_numeric(366, f"{chan_name} :End of /NAMES list.")

    async def cmd_PART(self, args):
        if not self.registered or not args:
            return
        
        channel_names = args[0].split(',')
        reason = args[1] if len(args) > 1 else "Leaving"
        
        for chan_name in channel_names:
            channel = self.server.channels.get(chan_name)
            if channel and self in channel.clients:
                part_msg = f":{self.prefix} PART {chan_name} :{reason}"
                await channel.broadcast(part_msg)
                
                channel.remove_client(self)
                self.channels.remove(channel)
                
                # Удаляем канал из сервера, если он пуст
                if not channel.clients:
                    del self.server.channels[chan_name]

    async def cmd_PRIVMSG(self, args):
        if not self.registered or len(args) < 2:
            return
            
        target_name = args[0]
        message = args[1]
        
        msg_to_send = f":{self.prefix} PRIVMSG {target_name} :{message}"
        
        if target_name.startswith('#'): # Сообщение в канал
            channel = self.server.channels.get(target_name)
            if channel:
                if self in channel.clients:
                    await channel.broadcast(msg_to_send, exclude_client=self)
                else:
                    await self.send_numeric(404, f"{target_name} :Cannot send to channel")
            else:
                await self.send_numeric(401, f"{target_name} :No such nick/channel")
        else: # Приватное сообщение пользователю
            target_client = self.server.clients.get(target_name)
            if target_client:
                await target_client.send_raw(msg_to_send)
            else:
                await self.send_numeric(401, f"{target_name} :No such nick/channel")

    async def cmd_QUIT(self, args):
        reason = args[0] if args else "Client Quit"
        await self.disconnect(reason)

    async def disconnect(self, reason="Connection closed"):
        """Обработка отключения клиента и очистка ресурсов."""
        if not hasattr(self, '_disconnected'):
            self._disconnected = True
            logger.info(f"Отключение клиента: {self.addr} ({self.nickname})")
            
            # Уведомляем каналы
            quit_msg = f":{self.prefix} QUIT :{reason}"
            for channel in list(self.channels):
                await channel.broadcast(quit_msg, exclude_client=self)
                channel.remove_client(self)
                if not channel.clients:
                    del self.server.channels[channel.name]
            
            # Удаляем из сервера
            if self.nickname and self.nickname in self.server.clients:
                del self.server.clients[self.nickname]
                
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass


class IRCServer:
    """Главный класс IRC сервера."""
    def __init__(self, host='0.0.0.0', port=6667):
        self.host = host
        self.port = port
        self.clients = {}  # nickname -> Client object
        self.channels = {} # channel_name -> Channel object

    def get_or_create_channel(self, name):
        if name not in self.channels:
            self.channels[name] = Channel(name)
        return self.channels[name]

    async def handle_client_connection(self, reader, writer):
        """Фабрика для новых подключений."""
        client = Client(self, reader, writer)
        await client.handle()

    async def start(self):
        """Запуск сервера."""
        server = await asyncio.start_server(
            self.handle_client_connection, self.host, self.port
        )
        addr = server.sockets[0].getsockname()
        logger.info(f"IRC Сервер запущен на {addr}")
        
        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    # Запуск сервера (по умолчанию на порту 6667)
    HOST = '0.0.0.0' # Измените на '0.0.0.0', чтобы сервер был доступен из локальной сети
    PORT = 6667
    
    server = IRCServer(host=HOST, port=PORT)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Сервер остановлен вручную.")
