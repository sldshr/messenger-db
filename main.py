"""
Terminal Messenger — одностраничный 1-на-1 мессенджер на FastAPI.

Запуск:
    pip install fastapi uvicorn
    python main.py

Использование:
    Веб-терминал:   http://localhost:8000/
    Netcat:         nc localhost 2323

Всё хранится в оперативной памяти. При перезапуске — пусто.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse


# ======================= Модель данных =======================

@dataclass
class Message:
    sender: str
    recipient: str
    text: str
    ts: float


@dataclass
class Client:
    """Живое подключение. Для web и tcp различается только send_raw."""
    username: str
    kind: str  # 'web' | 'tcp'
    send_raw: Callable[[str], Awaitable[None]]
    last_peer: Optional[str] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, text: str) -> None:
        # Лок гарантирует, что сообщения одному клиенту
        # не перемешаются, даже если их шлют из разных корутин.
        async with self._lock:
            await self.send_raw(text)


class QuitSignal(Exception):
    """Поднимается при /quit, чтобы выйти из цикла чтения."""


class Hub:
    """Общее состояние: клиенты и история переписок."""

    def __init__(self) -> None:
        self.clients: dict[str, Client] = {}
        # ключ — (userA, userB) в алфавитном порядке
        self.history: dict[tuple[str, str], list[Message]] = {}

    @staticmethod
    def _key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a < b else (b, a)

    def register(self, client: Client) -> bool:
        if client.username in self.clients:
            return False
        self.clients[client.username] = client
        return True

    def unregister(self, username: str) -> None:
        self.clients.pop(username, None)

    def online(self) -> list[str]:
        return sorted(self.clients.keys())

    async def send_to(self, username: str, text: str) -> bool:
        c = self.clients.get(username)
        if c is None:
            return False
        try:
            await c.send(text)
            return True
        except Exception:
            return False

    async def broadcast_system(self, text: str, exclude: Optional[str] = None) -> None:
        for name, c in list(self.clients.items()):
            if name == exclude:
                continue
            try:
                await c.send(text)
            except Exception:
                pass

    async def deliver(self, sender: str, recipient: str, text: str) -> tuple[bool, str]:
        if sender == recipient:
            return False, "нельзя отправить сообщение самому себе"
        if recipient not in self.clients:
            return False, f"пользователь '{recipient}' не в сети"

        msg = Message(sender, recipient, text, time.time())
        self.history.setdefault(self._key(sender, recipient), []).append(msg)
        await self.send_to(recipient, f"< {sender}: {text}")
        return True, "ok"

    def get_history(self, a: str, b: str, limit: int = 20) -> list[Message]:
        return self.history.get(self._key(a, b), [])[-limit:]


hub = Hub()


# ======================= Команды (общее для web/tcp) =======================

HELP_TEXT = (
    "Команды:\n"
    "  /to <user> <text>   отправить сообщение\n"
    "  /history <user>     последние 20 сообщений\n"
    "  /users              кто онлайн\n"
    "  /help               справка\n"
    "  /quit               выйти\n"
    "\n"
    "Без команды текст уйдёт последнему собеседнику."
)


def valid_username(u: str) -> bool:
    if not (1 <= len(u) <= 32):
        return False
    return not any(c.isspace() or ord(c) < 32 for c in u)


async def handle_command(client: Client, line: str) -> None:
    line = line.rstrip("\r\n")
    if not line.strip():
        return

    if line.startswith("/"):
        parts = line.split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd in ("/help", "/?"):
            await client.send(HELP_TEXT)
            return

        if cmd == "/users":
            users = [u for u in hub.online() if u != client.username]
            if not users:
                await client.send("[вы единственный онлайн]")
            else:
                await client.send("Онлайн: " + ", ".join(users))
            return

        if cmd == "/to":
            if len(parts) < 3:
                await client.send("Использование: /to <user> <text>")
                return
            target, text = parts[1], parts[2]
            ok, err = await hub.deliver(client.username, target, text)
            if ok:
                client.last_peer = target
                await client.send(f"> {target}: {text}")
            else:
                await client.send(f"[ошибка] {err}")
            return

        if cmd == "/history":
            if len(parts) < 2:
                await client.send("Использование: /history <user>")
                return
            peer = parts[1]
            msgs = hub.get_history(client.username, peer)
            if not msgs:
                await client.send(f"[нет истории с '{peer}']")
                return
            await client.send(f"--- история с {peer} ---")
            for m in msgs:
                who = "you" if m.sender == client.username else m.sender
                t = time.strftime("%H:%M:%S", time.localtime(m.ts))
                await client.send(f"[{t}] {who}: {m.text}")
            await client.send("--- конец ---")
            return

        if cmd in ("/quit", "/exit"):
            await client.send("[пока]")
            raise QuitSignal()

        await client.send(f"[неизвестная команда: {cmd}] введите /help")
        return

    # Обычный текст — уходит последнему собеседнику
    if not client.last_peer:
        await client.send("[нет собеседника] используйте: /to <user> <text>")
        return
    target = client.last_peer
    ok, err = await hub.deliver(client.username, target, line)
    if ok:
        await client.send(f"> {target}: {line}")
    else:
        await client.send(f"[ошибка] {err}")


async def start_session(client: Client) -> None:
    await client.send(f"[вы вошли как {client.username}]")
    await hub.broadcast_system(f"* {client.username} присоединился",
                               exclude=client.username)
    await client.send(HELP_TEXT)


async def end_session(username: str) -> None:
    hub.unregister(username)
    await hub.broadcast_system(f"* {username} покинул чат")


# ======================= TCP-обработчик (netcat) =======================

async def tcp_handler(reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
    async def send_raw(text: str) -> None:
        writer.write((text + "\r\n").encode("utf-8"))
        await writer.drain()

    username: Optional[str] = None
    try:
        await send_raw("=== Terminal Messenger ===")
        await send_raw("Введите имя пользователя:")

        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=60.0)
        except asyncio.TimeoutError:
            return

        candidate = raw.decode("utf-8", errors="replace").strip()
        if not valid_username(candidate):
            await send_raw("[некорректное имя]")
            return

        client = Client(username=candidate, kind="tcp", send_raw=send_raw)
        if not hub.register(client):
            await send_raw(f"[имя '{candidate}' уже занято]")
            return

        username = candidate
        await start_session(client)

        while True:
            raw = await reader.readline()
            if not raw:
                break
            try:
                await handle_command(client, raw.decode("utf-8", errors="replace"))
            except QuitSignal:
                break

    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    except Exception:
        pass
    finally:
        if username:
            await end_session(username)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ======================= HTML-терминал для веба =======================

INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminal Messenger</title>
<style>
  :root { --fg:#33ff66; --bg:#050807; --dim:#1a8a3a; }
  * { box-sizing: border-box; }
  html, body {
    margin:0; padding:0; height:100%;
    background: var(--bg); color: var(--fg);
    font-family: "Menlo","Consolas","Courier New",monospace;
    font-size: 14px; line-height: 1.4; overflow: hidden;
  }
  #wrap { display:flex; flex-direction:column; height:100vh; padding:8px; }
  #header {
    color: var(--dim);
    border-bottom: 1px dashed var(--dim);
    padding-bottom: 4px; margin-bottom: 6px; font-size: 12px;
  }
  #terminal {
    flex:1; overflow-y:auto; white-space:pre-wrap; word-break:break-word;
    padding-right:4px; scrollbar-width:thin; scrollbar-color:var(--dim) var(--bg);
  }
  #terminal::-webkit-scrollbar { width:8px; }
  #terminal::-webkit-scrollbar-thumb { background: var(--dim); }
  #terminal::-webkit-scrollbar-track { background: var(--bg); }
  #inputrow {
    display:flex; border-top:1px solid var(--dim);
    padding-top:4px; margin-top:4px; align-items:center;
  }
  #prompt { color: var(--fg); padding-right: 8px; user-select:none; }
  #input {
    flex:1; background:transparent; color:var(--fg); border:none; outline:none;
    font: inherit; caret-color: var(--fg);
  }
</style>
</head>
<body>
<div id="wrap">
  <div id="header">Terminal Messenger :: websocket :: in-memory</div>
  <div id="terminal"></div>
  <div id="inputrow">
    <span id="prompt">&gt;</span>
    <input id="input" autofocus autocomplete="off" autocapitalize="off" spellcheck="false">
  </div>
</div>
<script>
(function () {
  const term = document.getElementById('terminal');
  const input = document.getElementById('input');
  const promptEl = document.getElementById('prompt');

  let ws = null;
  let loggedIn = false;
  let closed = false;

  function append(text) {
    const div = document.createElement('div');
    div.textContent = text;
    term.appendChild(div);
    term.scrollTop = term.scrollHeight;
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(proto + '//' + location.host + '/ws');

    ws.onopen = () => append('[*] подключено');

    ws.onmessage = (ev) => {
      const text = ev.data;
      append(text);
      if (text.indexOf('[вы вошли как') === 0) loggedIn = true;
    };

    ws.onclose = () => {
      if (closed) return;
      closed = true;
      append('[*] соединение закрыто');
      promptEl.textContent = '!';
      input.disabled = true;
    };

    ws.onerror = () => append('[*] ошибка соединения');
  }

  connect();

  input.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter') return;
    const line = input.value;
    input.value = '';
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    // Локальное эхо только пока не вошли — чтобы видеть, что печатаешь в имя.
    if (!loggedIn) append('> ' + line);
    ws.send(line);
  });

  document.addEventListener('click', () => input.focus());
  window.addEventListener('focus', () => input.focus());
})();
</script>
</body>
</html>
"""


# ======================= FastAPI =======================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Параллельно с uvicorn поднимаем TCP-сервер для netcat.
    server = await asyncio.start_server(tcp_handler, "0.0.0.0", 2323)
    app.state.tcp_server = server

    print("=" * 60)
    print("Terminal Messenger запущен")
    print("  Web:     http://localhost:8000/")
    print("  Netcat:  nc localhost 2323")
    print("=" * 60)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()


app = FastAPI(lifespan=lifespan, title="Terminal Messenger")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    async def send_raw(text: str) -> None:
        await ws.send_text(text)

    username: Optional[str] = None
    try:
        await send_raw("Введите имя пользователя:")

        try:
            first = await ws.receive_text()
        except WebSocketDisconnect:
            return

        candidate = first.strip()
        if not valid_username(candidate):
            await send_raw("[некорректное имя]")
            return

        client = Client(username=candidate, kind="web", send_raw=send_raw)
        if not hub.register(client):
            await send_raw(f"[имя '{candidate}' уже занято]")
            return

        username = candidate
        await start_session(client)

        while True:
            try:
                text = await ws.receive_text()
            except WebSocketDisconnect:
                break
            try:
                await handle_command(client, text)
            except QuitSignal:
                break

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if username:
            await end_session(username)
        try:
            await ws.close()
        except Exception:
            pass


# ======================= Точка входа =======================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
