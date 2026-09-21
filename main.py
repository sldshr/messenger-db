import uvicorn
from fastapi import FastAPI, Response, HTTPException, Cookie
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import uuid
from datetime import datetime

# --- ИНИЦИАЛИЗАЦИЯ IN-MEMORY ХРАНИЛИЩА (В ОПЕРАТИВНОЙ ПАМЯТИ) ---
class InMemoryStorage:
    """
    Класс для хранения данных исключительно в оперативной памяти.
    При перезапуске сервера все данные будут безвозвратно утеряны.
    """
    def __init__(self):
        self.users = {}       # {username: password}
        self.posts = []       # [{"id": str, "author": str, "content": str, "timestamp": str}]
        self.sessions = {}    # {session_token: username}

# Создаем единственный экземпляр хранилища при запуске приложения
db = InMemoryStorage()

app = FastAPI(title="Litodon")

# --- Pydantic модели для валидации данных ---
class UserAuth(BaseModel):
    username: str
    password: str

class PostCreate(BaseModel):
    content: str

# --- API Эндпоинты ---

@app.post("/api/register")
async def register(user: UserAuth, response: Response):
    if user.username in db.users:
        raise HTTPException(status_code=400, detail="Пользователь уже существует")
    if not user.username or not user.password:
        raise HTTPException(status_code=400, detail="Заполните все поля")
    
    # Сохраняем пользователя в оперативку
    db.users[user.username] = user.password
    
    # Создаем сессию в оперативке
    token = str(uuid.uuid4())
    db.sessions[token] = user.username
    response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax")
    return {"message": "Успешная регистрация", "username": user.username}

@app.post("/api/login")
async def login(user: UserAuth, response: Response):
    if db.users.get(user.username) != user.password:
        raise HTTPException(status_code=400, detail="Неверное имя пользователя или пароль")
    
    token = str(uuid.uuid4())
    db.sessions[token] = user.username
    response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax")
    return {"message": "Успешный вход", "username": user.username}

@app.post("/api/logout")
async def logout(response: Response, session_token: Optional[str] = Cookie(None)):
    if session_token in db.sessions:
        del db.sessions[session_token]
    response.delete_cookie("session_token")
    return {"message": "Вы вышли из системы"}

@app.get("/api/me")
async def get_me(session_token: Optional[str] = Cookie(None)):
    if session_token and session_token in db.sessions:
        return {"username": db.sessions[session_token]}
    return {"username": None}

@app.get("/api/posts")
async def get_posts():
    # Возвращаем посты из оперативки в обратном хронологическом порядке
    return sorted(db.posts, key=lambda x: x["timestamp"], reverse=True)

@app.post("/api/posts")
async def create_post(post: PostCreate, session_token: Optional[str] = Cookie(None)):
    if not session_token or session_token not in db.sessions:
        raise HTTPException(status_code=401, detail="Неавторизован")
    
    if not post.content.strip():
        raise HTTPException(status_code=400, detail="Пост не может быть пустым")

    username = db.sessions[session_token]
    new_post = {
        "id": str(uuid.uuid4()),
        "author": username,
        "content": post.content,
        "timestamp": datetime.now().isoformat()
    }
    # Добавляем пост в оперативку
    db.posts.append(new_post)
    return new_post


# --- HTML / CSS / JS Интерфейс ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Litodon</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #191b22; color: #d9e1e8; font-family: sans-serif; }
        .column-bg { background-color: #282c37; }
        .border-color { border-color: #393f4f; }
        .accent-bg { background-color: #6364ff; }
        .accent-bg:hover { background-color: #5051db; }
        .text-muted { color: #606984; }
    </style>
</head>
<body class="h-screen overflow-hidden">
    <div class="grid grid-cols-1 md:grid-cols-4 h-full max-w-7xl mx-auto border-x border-color">
        
        <!-- Левая колонка -->
        <div class="hidden md:flex flex-col p-4 border-r border-color overflow-y-auto">
            <div class="relative mb-6">
                <input type="text" placeholder="Поиск" class="w-full bg-[#191b22] border border-color rounded-md py-2 px-3 text-sm focus:outline-none focus:border-[#6364ff]">
            </div>
            
            <p class="text-sm mb-4">
                <span class="font-bold text-white">litodon</span> — это один из многих независимых серверов Mastodon, которые вы можете использовать, чтобы присоединиться к сети Fediverse.
            </p>
            
            <div class="w-full h-32 bg-gradient-to-r from-yellow-200 to-blue-300 rounded-lg mb-6 flex items-center justify-center overflow-hidden relative">
                <div class="absolute inset-0 opacity-20 bg-[url('data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0MCIgaGVpZ2h0PSI0MCI+PGNpcmNsZSBjeD0iMjAiIGN5PSIyMCIgcj0iMTAiIGZpbGw9IiMwMDAiIC8+PC9zdmc+')]"></div>
                <span class="text-4xl z-10">🐘</span>
            </div>

            <p class="text-sm mb-4">Русскоязычный сервер социальной сети Mastodon. Зона общения, свободная от рекламы и шпионажа, теперь и на русском языке.</p>
            
            <div class="grid grid-cols-2 gap-4 text-sm mb-6">
                <div>
                    <div class="text-muted text-xs uppercase mb-1">Управляется:</div>
                    <div class="font-bold">@mo</div>
                </div>
                <div>
                    <div class="text-muted text-xs uppercase mb-1">Статистика сервера:</div>
                    <div class="font-bold">686 <span class="font-normal text-muted text-xs">активные пользователи</span></div>
                </div>
            </div>

            <div class="mt-auto text-xs text-muted space-y-1">
                <p><a href="#" class="hover:underline">litodon: Об этом сервере</a> · <a href="#" class="hover:underline">Состояние сервера</a> · <a href="#" class="hover:underline">Каталог профилей</a> · <a href="#" class="hover:underline">Политика конфиденциальности</a></p>
                <p>Litodon: <a href="#" class="hover:underline">О проекте</a> · <a href="#" class="hover:underline">Скачать приложение</a> · v1.0.0</p>
            </div>
        </div>

        <!-- Центральная колонка -->
        <div class="col-span-1 md:col-span-2 flex flex-col border-r border-color h-full overflow-hidden">
            <div class="p-4 border-b border-color flex justify-between items-center column-bg">
                <h1 class="text-xl font-bold text-white md:hidden">Litodon</h1>
                <div id="user-info" class="text-sm font-bold text-white ml-auto"></div>
                <button id="logout-btn" onclick="logout()" class="hidden text-sm text-muted hover:text-white ml-4">Выйти</button>
            </div>

            <div class="flex-1 overflow-y-auto p-4" id="main-content">
                <!-- Форма поста (появляется после входа) -->
                <div id="post-form-container" class="hidden mb-6 column-bg p-4 rounded-lg border border-color">
                    <textarea id="post-content" rows="3" class="w-full bg-[#191b22] text-white p-3 rounded border border-color focus:outline-none focus:border-[#6364ff] resize-none" placeholder="Что нового?"></textarea>
                    <div class="flex justify-end mt-2">
                        <button onclick="submitPost()" class="accent-bg text-white px-4 py-2 rounded font-bold transition">Отправить</button>
                    </div>
                </div>

                <!-- Лента постов -->
                <div id="timeline" class="space-y-4 hidden"></div>

                <!-- Правила сервера (видны только гостям) -->
                <div id="rules-container" class="space-y-6">
                    <h2 class="text-xl font-bold text-white">Подробнее</h2>
                    
                    <div>
                        <h3 class="font-bold text-white mb-2">LML — про общение людей</h3>
                        <ul class="list-disc pl-5 space-y-1 text-sm">
                            <li>Аккаунты, предназначенные исключительно для коммерческой деятельности запрещены</li>
                            <li>Для аккаунтов ботов/групп в профиле должен быть указан аккаунт ответственного</li>
                            <li>Боты, которые просто пересылают контент с других сайтов должны постить вне публичных лент</li>
                        </ul>
                    </div>

                    <div>
                        <h3 class="font-bold text-white mb-2">LML — зона безопасного общения</h3>
                        <h4 class="font-bold text-white text-sm mb-1">Не место для ненависти</h4>
                        <p class="text-sm mb-2">Разжигание ненависти по признакам, которые люди не выбирали — полностью под запретом</p>
                        <ul class="list-disc pl-5 space-y-1 text-sm mb-2">
                            <li>Гомофобия, трансфобия, энифобия, и т.д</li>
                            <li>Сексизм, расизм, нацизм, и т.д</li>
                        </ul>
                        <p class="text-sm">Сюда также входит одобрение/поддержка вышеупомянутых взглядов</p>
                    </div>

                    <div>
                        <h3 class="font-bold text-white mb-2">Не место для травли</h3>
                        <p class="text-sm mb-2">Полностью запрещено</p>
                        <ul class="list-disc pl-5 space-y-1 text-sm">
                            <li>Разглашение чужой конфиденциальной информации</li>
                            <li>Преследование</li>
                            <li>Попытки обойти блок</li>
                            <li>Имперсонация, выдача себя за других</li>
                        </ul>
                    </div>
                    
                    <p class="text-sm">Даже если вы делаете это вне LML, это может привести к блокировке вашего аккаунта здесь.</p>
                    <p class="text-sm">Повторяющееся агрессивное поведение, разжигание конфликтов, может занять некоторое время. Мы изучаем все поступающие жалобы.</p>
                </div>
            </div>
        </div>

        <!-- Правая колонка -->
        <div class="hidden md:flex flex-col p-4 column-bg overflow-y-auto">
            <div class="flex items-center gap-2 mb-8">
                <div class="w-8 h-8 accent-bg rounded-md flex items-center justify-center text-white font-bold">L</div>
                <h1 class="text-2xl font-bold text-white">litodon</h1>
            </div>

            <div class="mb-6">
                <h2 class="text-sm font-bold text-muted uppercase mb-3 flex items-center gap-1">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"></path></svg>
                    Актуальное
                </h2>
            </div>

            <div class="mb-6">
                <p class="font-bold text-white mb-2">Litodon — лучший способ быть в курсе всего происходящего.</p>
                <p class="text-sm mb-4">Подписывайтесь на кого угодно в федиверсе и читайте ленту в хронологическом порядке. Никаких алгоритмов, рекламы и кликбейта.</p>
                
                <div id="auth-buttons" class="space-y-2">
                    <button onclick="openModal('register')" class="w-full accent-bg text-white py-2 rounded font-bold transition">Зарегистрироваться</button>
                    <button onclick="openModal('login')" class="w-full border border-color text-white py-2 rounded font-bold hover:bg-[#393f4f] transition">Войти</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Модальное окно авторизации -->
    <div id="auth-modal" class="hidden fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4">
        <div class="column-bg p-6 rounded-lg w-full max-w-sm border border-color shadow-xl">
            <h2 id="auth-title" class="text-xl font-bold text-white mb-4">Войти</h2>
            <div id="auth-error" class="hidden text-red-500 text-sm mb-4"></div>
            <input type="text" id="username" placeholder="Имя пользователя" class="w-full bg-[#191b22] text-white p-3 rounded border border-color mb-3 focus:outline-none focus:border-[#6364ff]">
            <input type="password" id="password" placeholder="Пароль" class="w-full bg-[#191b22] text-white p-3 rounded border border-color mb-4 focus:outline-none focus:border-[#6364ff]">
            <div class="flex justify-end gap-3">
                <button onclick="closeModal()" class="px-4 py-2 text-muted hover:text-white transition">Отмена</button>
                <button id="auth-submit" onclick="submitAuth()" class="accent-bg text-white px-6 py-2 rounded font-bold transition">Войти</button>
            </div>
        </div>
    </div>

    <script>
        let currentAuthMode = 'login';
        let currentUser = null;

        // --- Управление интерфейсом ---
        async function checkAuth() {
            const res = await fetch('/api/me');
            const data = await res.json();
            currentUser = data.username;

            const authButtons = document.getElementById('auth-buttons');
            const postForm = document.getElementById('post-form-container');
            const rulesContainer = document.getElementById('rules-container');
            const timeline = document.getElementById('timeline');
            const userInfo = document.getElementById('user-info');
            const logoutBtn = document.getElementById('logout-btn');

            if (currentUser) {
                authButtons.classList.add('hidden');
                postForm.classList.remove('hidden');
                rulesContainer.classList.add('hidden');
                timeline.classList.remove('hidden');
                userInfo.innerText = `@${currentUser}`;
                logoutBtn.classList.remove('hidden');
                loadPosts();
            } else {
                authButtons.classList.remove('hidden');
                postForm.classList.add('hidden');
                rulesContainer.classList.remove('hidden');
                timeline.classList.add('hidden');
                userInfo.innerText = '';
                logoutBtn.classList.add('hidden');
            }
        }

        function openModal(mode) {
            currentAuthMode = mode;
            document.getElementById('auth-modal').classList.remove('hidden');
            document.getElementById('auth-title').innerText = mode === 'login' ? 'Войти' : 'Зарегистрироваться';
            document.getElementById('auth-submit').innerText = mode === 'login' ? 'Войти' : 'Создать аккаунт';
            document.getElementById('auth-error').classList.add('hidden');
            document.getElementById('username').value = '';
            document.getElementById('password').value = '';
        }

        function closeModal() {
            document.getElementById('auth-modal').classList.add('hidden');
        }

        async function submitAuth() {
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            const errorDiv = document.getElementById('auth-error');
            
            const endpoint = currentAuthMode === 'login' ? '/api/login' : '/api/register';
            
            try {
                const res = await fetch(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });
                
                const data = await res.json();
                
                if (!res.ok) {
                    throw new Error(data.detail || 'Ошибка авторизации');
                }
                
                closeModal();
                checkAuth();
            } catch (err) {
                errorDiv.innerText = err.message;
                errorDiv.classList.remove('hidden');
            }
        }

        async function logout() {
            await fetch('/api/logout', { method: 'POST' });
            checkAuth();
        }

        // --- Работа с постами ---
        async function loadPosts() {
            const res = await fetch('/api/posts');
            const posts = await res.json();
            const timeline = document.getElementById('timeline');
            timeline.innerHTML = '';

            if (posts.length === 0) {
                timeline.innerHTML = '<p class="text-muted text-center py-8">Пока нет постов. Будьте первым!</p>';
                return;
            }

            posts.forEach(post => {
                const date = new Date(post.timestamp).toLocaleString('ru-RU');
                const postEl = document.createElement('div');
                postEl.className = 'column-bg p-4 rounded-lg border border-color';
                postEl.innerHTML = `
                    <div class="flex items-center gap-2 mb-2">
                        <div class="w-6 h-6 accent-bg rounded-full flex items-center justify-center text-xs text-white font-bold">${post.author[0].toUpperCase()}</div>
                        <span class="font-bold text-white">@${post.author}</span>
                        <span class="text-muted text-xs">${date}</span>
                    </div>
                    <p class="text-sm whitespace-pre-wrap">${escapeHtml(post.content)}</p>
                `;
                timeline.appendChild(postEl);
            });
        }

        async function submitPost() {
            const content = document.getElementById('post-content').value;
            if (!content.trim()) return;

            const res = await fetch('/api/posts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content })
            });

            if (res.ok) {
                document.getElementById('post-content').value = '';
                loadPosts();
            } else {
                alert('Ошибка при отправке поста');
            }
        }

        // Простая защита от XSS
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.innerText = text;
            return div.innerHTML;
        }

        // Инициализация при загрузке
        checkAuth();
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return HTML_TEMPLATE

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
