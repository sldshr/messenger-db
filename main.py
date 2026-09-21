import uvicorn
from fastapi import FastAPI, Response, HTTPException, Cookie
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import uuid
from datetime import datetime

# --- ИНИЦИАЛИЗАЦИЯ IN-MEMORY ХРАНИЛИЩА (В ОПЕРАТИВНОЙ ПАМЯТИ) ---
class InMemoryStorage:
    def __init__(self):
        self.users = {}       # {username: password}
        self.posts = []       # [{"id": str, "author": str, "content": str, "timestamp": str}]
        self.sessions = {}    # {session_token: username}

db = InMemoryStorage()

app = FastAPI(title="Litodon")

# --- Pydantic модели ---
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
    
    db.users[user.username] = user.password
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
    db.posts.append(new_post)
    return new_post


# --- HTML / CSS / JS Интерфейс (Чистый CSS, без Tailwind) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Litodon</title>
    <style>
        /* --- Точные цвета Mastodon Dark Theme --- */
        :root {
            --bg-color: #191b22;
            --col-bg: #282c37;
            --border-color: #393f4f;
            --text-main: #d9e1e8;
            --text-muted: #606984;
            --accent: #6364ff;
            --accent-hover: #5051db;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            background-color: var(--bg-color);
            color: var(--text-main);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            height: 100vh;
            overflow: hidden;
            font-size: 15px;
            line-height: 1.5;
        }

        /* --- Сетка макета --- */
        .app-container {
            display: grid;
            grid-template-columns: 285px 1fr 285px;
            max-width: 1200px;
            margin: 0 auto;
            height: 100vh;
            border-left: 1px solid var(--border-color);
            border-right: 1px solid var(--border-color);
        }

        .column {
            height: 100%;
            overflow-y: auto;
            padding: 16px;
        }
        .column::-webkit-scrollbar { width: 8px; }
        .column::-webkit-scrollbar-thumb { background: var(--border-color); border-radius: 4px; }

        .left-col { border-right: 1px solid var(--border-color); }
        .center-col { padding: 16px 24px; }
        .right-col { border-left: 1px solid var(--border-color); }

        /* --- Левая колонка --- */
        .search-box {
            position: relative;
            margin-bottom: 24px;
        }
        .search-box input {
            width: 100%;
            background: var(--col-bg);
            border: 1px solid var(--border-color);
            border-radius: 4px;
            padding: 8px 12px 8px 36px;
            color: var(--text-main);
            font-size: 14px;
            outline: none;
        }
        .search-box input:focus { border-color: var(--accent); }
        .search-box svg {
            position: absolute;
            left: 10px;
            top: 50%;
            transform: translateY(-50%);
            width: 16px;
            height: 16px;
            fill: var(--text-muted);
        }

        .left-col p { margin-bottom: 16px; font-size: 14px; }
        
        .illustration {
            width: 100%;
            height: 140px;
            background: linear-gradient(135deg, #fceabb 0%, #f8b500 100%);
            border-radius: 6px;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin: 24px 0;
            font-size: 13px;
        }
        .stat-label { color: var(--text-muted); font-size: 11px; text-transform: uppercase; margin-bottom: 4px; }
        .stat-value { font-weight: bold; color: white; }

        .footer-links {
            margin-top: auto;
            font-size: 12px;
            color: var(--text-muted);
            line-height: 1.8;
        }
        .footer-links a { color: var(--text-muted); text-decoration: none; }
        .footer-links a:hover { text-decoration: underline; }

        /* --- Центральная колонка --- */
        .rules-container h2 { font-size: 20px; color: white; margin-bottom: 16px; }
        .rules-container h3 { font-size: 16px; color: white; margin: 20px 0 8px 0; }
        .rules-container p { margin-bottom: 12px; }
        .rules-container ul { padding-left: 20px; margin-bottom: 16px; }
        .rules-container li { margin-bottom: 4px; }

        /* --- Правая колонка --- */
        .logo-header {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 24px;
        }
        .logo-icon {
            width: 32px; height: 32px;
            background: var(--accent);
            border-radius: 6px;
            display: flex; align-items: center; justify-content: center;
            color: white; font-weight: bold; font-size: 18px;
        }
        .logo-header h1 { font-size: 22px; color: white; margin: 0; }

        .trending-header {
            display: flex; align-items: center; gap: 4px;
            color: var(--text-muted); font-size: 11px; text-transform: uppercase; font-weight: 600;
            margin-bottom: 16px;
        }
        .trending-header svg { width: 14px; height: 14px; fill: var(--text-muted); }

        .right-col p { font-size: 14px; margin-bottom: 16px; }
        .right-col .highlight { font-weight: bold; color: white; }

        .auth-buttons { display: flex; flex-direction: column; gap: 12px; margin-top: 24px; }
        
        .btn {
            width: 100%;
            padding: 10px;
            border-radius: 4px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            text-align: center;
            border: none;
            transition: background 0.2s;
        }
        .btn-primary { background: var(--accent); color: white; }
        .btn-primary:hover { background: var(--accent-hover); }
        .btn-secondary { background: transparent; color: white; border: 1px solid var(--border-color); }
        .btn-secondary:hover { background: var(--col-bg); }
        .btn-text { background: transparent; color: var(--text-muted); border: none; padding: 4px 8px; width: auto; font-size: 13px; }
        .btn-text:hover { color: white; }

        /* --- Лента и посты --- */
        .post-form {
            background: var(--col-bg);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 16px;
            margin-bottom: 24px;
        }
        .post-form textarea {
            width: 100%;
            background: transparent;
            border: none;
            color: white;
            font-family: inherit;
            font-size: 15px;
            resize: none;
            outline: none;
            min-height: 60px;
        }
        .post-form textarea::placeholder { color: var(--text-muted); }
        .post-form .actions {
            display: flex; justify-content: flex-end; margin-top: 8px;
            border-top: 1px solid var(--border-color); padding-top: 12px;
        }

        .post-card {
            background: var(--col-bg);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 16px;
            margin-bottom: 16px;
        }
        .post-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
        .avatar {
            width: 28px; height: 28px; background: var(--accent); border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 12px; font-weight: bold; color: white;
        }
        .post-author { font-weight: bold; color: white; }
        .post-date { color: var(--text-muted); font-size: 12px; }
        .post-content { white-space: pre-wrap; word-break: break-word; }

        /* --- Модальное окно --- */
        .modal-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7);
            display: none; align-items: center; justify-content: center;
            z-index: 1000;
        }
        .modal {
            background: var(--col-bg);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            width: 100%; max-width: 400px;
            padding: 24px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.5);
        }
        .modal h2 { font-size: 18px; color: white; margin-bottom: 16px; }
        .form-group { margin-bottom: 16px; }
        .form-group input {
            width: 100%;
            background: var(--bg-color);
            border: 1px solid var(--border-color);
            border-radius: 4px;
            padding: 10px;
            color: white;
            font-size: 14px;
            outline: none;
        }
        .form-group input:focus { border-color: var(--accent); }
        .error-msg { color: #ff5252; font-size: 13px; margin-bottom: 16px; display: none; }
        .modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }

        @media (max-width: 900px) {
            .app-container { grid-template-columns: 1fr; }
            .left-col, .right-col { display: none; }
        }
    </style>
</head>
<body>
    <div class="app-container">
        
        <!-- Левая колонка -->
        <div class="column left-col">
            <div class="search-box">
                <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
                <input type="text" placeholder="Поиск">
            </div>
            
            <p><span style="font-weight:700; color:white;">mastodon.ml</span> — это один из многих независимых серверов Mastodon, которые вы можете использовать, чтобы присоединиться к сети Fediverse.</p>
            
            <div class="illustration">
                <!-- Векторная иллюстрация вместо эмодзи -->
                <svg width="100" height="100" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <circle cx="50" cy="50" r="40" fill="rgba(255,255,255,0.2)"/>
                    <path d="M50 20C33.4 20 20 33.4 20 50s13.4 30 30 30 30-13.4 30-30-13.4-30-30-30zm0 50c-11 0-20-9-20-20s9-20 20-20 20 9 20 20-9 20-20 20z" fill="rgba(0,0,0,0.3)"/>
                    <path d="M50 35c-8.3 0-15 6.7-15 15s6.7 15 15 15 15-6.7 15-15-6.7-15-15-15zm0 20c-2.8 0-5-2.2-5-5s2.2-5 5-5 5 2.2 5 5-2.2 5-5 5z" fill="rgba(0,0,0,0.5)"/>
                </svg>
            </div>

            <p>Русскоязычный сервер социальной сети Mastodon. Зона общения, свободная от рекламы и шпионажа, теперь и на русском языке.</p>
            
            <div class="stats-grid">
                <div>
                    <div class="stat-label">Управляется:</div>
                    <div class="stat-value">@mo</div>
                </div>
                <div>
                    <div class="stat-label">Статистика сервера:</div>
                    <div class="stat-value">686 <span style="font-weight:400; color:var(--text-muted); font-size:12px;">активные пользователи</span></div>
                </div>
            </div>

            <div class="footer-links">
                <p><a href="#">mastodon.ml: Об этом сервере</a> · <a href="#">Состояние сервера</a> · <a href="#">Каталог профилей</a> · <a href="#">Политика конфиденциальности</a></p>
                <p>Mastodon: <a href="#">О проекте</a> · <a href="#">Скачать приложение</a> · <a href="#">Сочетания клавиш</a> · <a href="#">Исходный код</a> · v4.7.2</p>
            </div>
        </div>

        <!-- Центральная колонка -->
        <div class="column center-col">
            
            <!-- Шапка (видна только авторизованным) -->
            <div id="user-header" style="display:none; justify-content:space-between; align-items:center; margin-bottom:24px; padding-bottom:16px; border-bottom:1px solid var(--border-color);">
                <h1 style="font-size:20px; color:white; margin:0;">Litodon</h1>
                <div style="display:flex; align-items:center; gap:16px;">
                    <span id="user-info" style="font-weight:bold; color:white;"></span>
                    <button onclick="logout()" class="btn btn-text">Выйти</button>
                </div>
            </div>

            <!-- Блок правил (виден только гостям) -->
            <div id="rules-container" class="rules-container">
                <h2>Подробнее</h2>
                
                <h3>LML — про общение людей</h3>
                <ul>
                    <li>Аккаунты, предназначенные исключительно для коммерческой деятельности запрещены</li>
                    <li>Для аккаунтов ботов/групп в профиле должен быть указан аккаунт ответственного</li>
                    <li>Боты, которые просто пересылают контент с других сайтов должны постить вне публичных лент</li>
                </ul>

                <h3>LML — зона безопасного общения</h3>
                <h4 style="font-weight:bold; margin-bottom:4px;">Не место для ненависти</h4>
                <p>Разжигание ненависти по признакам, которые люди не выбирали — полностью под запретом</p>
                <ul>
                    <li>Гомофобия, трансфобия, энифобия, и т.д</li>
                    <li>Сексизм, расизм, нацизм, и т.д</li>
                </ul>
                <p>Сюда также входит одобрение/поддержка вышеупомянутых взглядов</p>

                <h3>Не место для травли</h3>
                <p>Полностью запрещено</p>
                <ul>
                    <li>Разглашение чужой конфиденциальной информации</li>
                    <li>Преследование</li>
                    <li>Попытки обойти блок</li>
                    <li>Имперсонация, выдача себя за других</li>
                </ul>

                <p style="margin-top:16px;">Даже если вы делаете это вне LML, это может привести к блокировке вашего аккаунта здесь.</p>
                <p>Повторяющееся агрессивное поведение, разжигание конфликтов, может занять некоторое время. Мы изучаем все поступающие жалобы.</p>
            </div>

            <!-- Форма поста и лента (видны только авторизованным) -->
            <div id="user-feed-container" style="display:none;">
                <div class="post-form">
                    <textarea id="post-content" placeholder="Что нового?"></textarea>
                    <div class="actions">
                        <button onclick="submitPost()" class="btn btn-primary" style="width:auto;">Отправить</button>
                    </div>
                </div>
                <div id="timeline"></div>
            </div>

        </div>

        <!-- Правая колонка -->
        <div class="column right-col">
            <div class="logo-header">
                <div class="logo-icon">L</div>
                <h1>Litodon</h1>
            </div>

            <div class="trending-header">
                <svg viewBox="0 0 24 24"><path d="M16 6l2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6z"/></svg>
                Актуальное
            </div>

            <p class="highlight">Mastodon — лучший способ быть в курсе всего происходящего.</p>
            <p>Подписывайтесь на кого угодно в федиверсе и читайте ленту в хронологическом порядке. Никаких алгоритмов, рекламы и кликбейта.</p>
            
            <div id="auth-buttons" class="auth-buttons">
                <button onclick="openModal('register')" class="btn btn-primary">Зарегистрироваться</button>
                <button onclick="openModal('login')" class="btn btn-secondary">Войти</button>
            </div>
        </div>
    </div>

    <!-- Модальное окно авторизации -->
    <div id="auth-modal" class="modal-overlay">
        <div class="modal">
            <h2 id="auth-title">Войти</h2>
            <div id="auth-error" class="error-msg"></div>
            <div class="form-group">
                <input type="text" id="username" placeholder="Имя пользователя">
            </div>
            <div class="form-group">
                <input type="password" id="password" placeholder="Пароль">
            </div>
            <div class="modal-actions">
                <button onclick="closeModal()" class="btn btn-text">Отмена</button>
                <button id="auth-submit" onclick="submitAuth()" class="btn btn-primary" style="width:auto;">Войти</button>
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

            const rulesContainer = document.getElementById('rules-container');
            const userFeedContainer = document.getElementById('user-feed-container');
            const userHeader = document.getElementById('user-header');
            const userInfo = document.getElementById('user-info');
            const authButtons = document.getElementById('auth-buttons');

            if (currentUser) {
                // Пользователь авторизован
                rulesContainer.style.display = 'none';
                userFeedContainer.style.display = 'block';
                userHeader.style.display = 'flex';
                userInfo.innerText = `@${currentUser}`;
                authButtons.style.display = 'none';
                loadPosts();
            } else {
                // Гость
                rulesContainer.style.display = 'block';
                userFeedContainer.style.display = 'none';
                userHeader.style.display = 'none';
                authButtons.style.display = 'flex';
            }
        }

        function openModal(mode) {
            currentAuthMode = mode;
            document.getElementById('auth-modal').style.display = 'flex';
            document.getElementById('auth-title').innerText = mode === 'login' ? 'Войти' : 'Зарегистрироваться';
            document.getElementById('auth-submit').innerText = mode === 'login' ? 'Войти' : 'Создать аккаунт';
            document.getElementById('auth-error').style.display = 'none';
            document.getElementById('username').value = '';
            document.getElementById('password').value = '';
        }

        function closeModal() {
            document.getElementById('auth-modal').style.display = 'none';
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
                errorDiv.style.display = 'block';
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
                timeline.innerHTML = '<p style="text-align:center; color:var(--text-muted); padding:32px 0;">Пока нет постов. Будьте первым!</p>';
                return;
            }

            posts.forEach(post => {
                const date = new Date(post.timestamp).toLocaleString('ru-RU');
                const postEl = document.createElement('div');
                postEl.className = 'post-card';
                postEl.innerHTML = `
                    <div class="post-header">
                        <div class="avatar">${post.author[0].toUpperCase()}</div>
                        <span class="post-author">@${post.author}</span>
                        <span class="post-date">${date}</span>
                    </div>
                    <div class="post-content">${escapeHtml(post.content)}</div>
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

        // Защита от XSS
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.innerText = text;
            return div.innerHTML;
        }

        // Инициализация
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
