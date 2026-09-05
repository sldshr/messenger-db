import os
import jwt
from aiohttp import web

# Переменные окружения Supabase (задаются в панели RunxBuild)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://your-project.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "your-anon-key")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "your-supabase-jwt-secret")

# Структура активных websocket-подключений в оперативной памяти сервера
ROOMS = {f"room_{i}": set() for i in range(1, 9)}

# Безопасная валидация токена пользователя (без единого обращения к БД)
def verify_supabase_token(token):
    try:
        # Проверяем подпись токена встроенным секретным ключом проекта Supabase
        payload = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
        return payload  # Возвращает словарь с данными сессии юзера (id, email, роль)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None

# Асинхронная проверка существования и активности комнаты в Supabase
async def is_room_active_in_supabase(room_id):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    async with web.ClientSession() as session:
        url = f"{SUPABASE_URL}/rest/v1/rooms?id=eq.{room_id}&is_active=eq.true&select=*"
        try:
            async with session.get(url, headers=headers, timeout=3) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return len(data) > 0
        except Exception:
            return False
    return False

# Раздача Frontend-интерфейса
async def handle_index(request):
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Safe Voice & Chat</title>
        <meta charset="utf-8">
        <style>
            body { font-family: sans-serif; background: #121212; color: #fff; display: flex; flex-direction: column; align-items: center; padding: 20px; }
            .container { width: 420px; background: #1e1e1e; padding: 20px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.5); text-align: center; }
            .btn { width: 100%; padding: 12px; background: #00bcd4; border: none; color: white; cursor: pointer; border-radius: 4px; font-weight: bold; margin-top: 10px; font-size: 15px;}
            .btn:disabled { background: #444; cursor: not-allowed; }
            select, input { width: 100%; padding: 10px; background: #2d2d2d; color: white; border: 1px solid #444; border-radius: 4px; box-sizing: border-box; margin-bottom: 10px; }
            #chatBox { height: 160px; overflow-y: auto; background: #151515; border: 1px solid #333; margin-top: 15px; padding: 10px; border-radius: 4px; font-size: 14px; text-align: left; }
            .msg { margin-bottom: 5px; word-break: break-all; }
            .status { color: #ff9800; font-weight: bold; margin: 10px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>Supabase Voice & Text Service</h2>
            
            <label><b>Шаг 1:</b> Токен доступа Supabase (JWT)</label>
            <input type="text" id="tokenInput" placeholder="Вставьте access_token из supabase.auth">
            
            <label>Имя в чате:</label>
            <input type="text" id="usernameInput" value="Пользователь">
            
            <label><b>Шаг 2:</b> Выберите комнату (из SQL БД):</label>
            <select id="roomSelect"></select>
            
            <button class="btn" id="connectBtn" onclick="connect()">Войти в закрытый канал</button>
            
            <div class="status" id="status">Статус: Ожидание ввода</div>
            
            <div id="chatBox"><i>Авторизуйтесь для доступа к истории канала...</i></div>
            <br>
            <input type="text" id="msgInput" placeholder="Нажмите Enter для отправки..." onkeydown="if(event.key==='Enter') sendText()" disabled>
        </div>

        <script>
            // Автоматически генерируем опции для селектора
            const select = document.getElementById('roomSelect');
            for(let i=1; i<=8; i++) {
                let opt = document.createElement('option');
                opt.value = `room_${i}`;
                opt.innerText = `Комната + Чат №${i}`;
                select.appendChild(opt);
            }

            let ws;
            async function connect() {
                const token = document.getElementById('tokenInput').value.trim();
                const room = select.value;
                if(!token) return alert('Ошибка безопасности: Требуется токен Supabase!');
                
                if(ws) ws.close();
                document.getElementById('status').innerText = 'Проверка прав в базе данных...';
                
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                ws = new WebSocket(`${protocol}//${window.location.host}/ws?room_id=${room}&token=${token}`);

                ws.onopen = () => {
                    document.getElementById('status').innerText = `Защищенное соединение установлено! Вы в ${room}`;
                    document.getElementById('status').style.color = '#4CAF50';
                    document.getElementById('msgInput').disabled = false;
                    document.getElementById('chatBox').innerHTML = '<i>Система безопасности: Вы вошли в зашифрованный канал.</i><br>';
                    startAudio(ws);
                };

                ws.onmessage = async (event) => {
                    if (event.data instanceof Blob) {
                        // Поток аудио
                        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
                        const arrayBuffer = await event.data.arrayBuffer();
                        audioContext.decodeAudioData(arrayBuffer, (buffer) => {
                            const source = audioContext.createBufferSource();
                            source.buffer = buffer;
                            source.connect(audioContext.destination);
                            source.start();
                        });
                    } else {
                        // Текстовый чат
                        const data = JSON.parse(event.data);
                        const chatBox = document.getElementById('chatBox');
                        chatBox.innerHTML += `<div class="msg"><b>\${data.user}:</b> \${data.text}</div>`;
                        chatBox.scrollTop = chatBox.scrollHeight;
                    }
                };

                ws.onclose = (e) => {
                    document.getElementById('msgInput').disabled = true;
                    document.getElementById('status').style.color = '#f44336';
                    if (e.code === 4001) document.getElementById('status').innerText = 'Ошибка: В комнате лимит 5 человек!';
                    else if (e.code === 4003) document.getElementById('status').innerText = 'Ошибка безопасности: Неверный токен или комната отключена!';
                    else document.getElementById('status').innerText = 'Доступ закрыт. Соединение разорвано.';
                };
            }

            function sendText() {
                const input = document.getElementById('msgInput');
                const username = document.getElementById('usernameInput').value;
                if(!input.value.trim() || !ws || ws.readyState !== WebSocket.OPEN) return;
                
                const packet = { user: username, text: input.value };
                ws.send(JSON.stringify(packet));
                
                const chatBox = document.getElementById('chatBox');
                chatBox.innerHTML += `<div class="msg" style="color: #00bcd4;"><b>Вы:</b> \${input.value}</div>`;
                chatBox.scrollTop = chatBox.scrollHeight;
                input.value = '';
            }

            function startAudio(websocket) {
                navigator.mediaDevices.getUserMedia({ 
                    audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000 }, 
                    video: false 
                })
                .then(stream => {
                    const mediaRecorder = new MediaRecorder(stream, { 
                        mimeType: 'audio/webm;codecs=opus',
                        audioBitsPerSecond: 12000 // Минимальный битрейт для разгрузки сети
                    });
                    mediaRecorder.ondataavailable = e => {
                        if (e.data.size > 0 && websocket.readyState === WebSocket.OPEN) {
                            websocket.send(e.data);
                        }
                    };
                    // ЭКСТРЕМАЛЬНАЯ ОПТИМИЗАЦИЯ ПОД 0.1 CPU: Отправка раз в 300мс!
                    mediaRecorder.start(300); 
                }).catch(err => console.log('Аудио-девайс заблокирован:', err));
            }
        </script>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')

# WebSocket маршрутизатор (Голос + Текст + Валидация)
async def handle_websocket(request):
    room_id = request.query.get("room_id")
    token = request.query.get("token")
    
    # 1. Первичная проверка структуры
    if room_id not in ROOMS or not token:
        return web.Response(status=400, text="Bad Request")

    # 2. Быстрая криптографическая проверка токена
    user_payload = verify_supabase_token(token)
    if not user_payload:
        return web.Response(status=401, text="Unauthorized Token")

    # 3. Легкий асинхронный запрос статуса комнаты в БД Supabase
    is_valid_room = await is_room_active_in_supabase(room_id)
    if not is_valid_room:
        return web.Response(status=403, text="Room Inactive or Forbidden")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # 4. Лимит до 5 человек на одну комнату
    if len(ROOMS[room_id]) >= 5:
        await ws.close(code=4001, message=b"Full")
        return ws

    ROOMS[room_id].add(ws)

    try:
        async for msg in ws:
            # Релейный пересыльщик бинарного аудио
            if msg.type == web.WSMsgType.BINARY:
                for user in ROOMS[room_id]:
                    if user != ws and not user.closed:
                        await user.send_bytes(msg.data)
            # Релейный пересыльщик текстовых пакетов чата
            elif msg.type == web.WSMsgType.TEXT:
                for user in ROOMS[room_id]:
                    if user != ws and not user.closed:
                        await user.send_str(msg.data)
    except Exception:
        pass
    finally:
        if ws in ROOMS[room_id]:
            ROOMS[room_id].remove(ws)
            
    return ws

app = web.Application()
app.add_routes([
    web.get('/', handle_index),
    web.get('/ws', handle_websocket)
])

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    web.run_app(app, host='0.0.0.0', port=port)
