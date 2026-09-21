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


# --- HTML / CSS / JS Интерфейс (Material Design) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Litodon</title>
    <!-- Подключаем шрифты и иконки Google Material Design -->
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200" rel="stylesheet" />
    <style>
        /* --- Переменные Material Design (Dark Theme) --- */
        :root {
            --md-bg: #121212;
            --md-surface: #1e1e1e;
            --md-surface-hover: #2c2c2c;
            --md-primary: #6364ff;
            --md-primary-hover: #5051db;
            --md-on-primary: #ffffff;
            --md-text: #e0e0e0;
            --md-text-muted: #a0aec0;
            --md-border: #333333;
            --md-elevation-1: 0px 2px 1px -1px rgba(0,0,0,0.2), 0px 1px 1px 0px rgba(0,0,0,0.14), 0px 1px 3px 0px rgba(0,0,0,0.12);
            --md-elevation-2: 0px 3px 1px -2px rgba(0,0,0,0.2), 0px 2px 2px 0px rgba(0,0,0,0.14), 0px 1px 5px 0px rgba(0,0,0,0.12);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--md-bg);
            color: var(--md-text);
            font-family: 'Roboto', sans-serif;
            height: 100vh;
            overflow: hidden;
            display: flex;
        }

        /* --- Макет (Grid Layout) --- */
        .layout-grid {
            display: grid;
            grid-template-columns: 1fr 2fr 1fr;
            width: 100%;
            max-width: 1400px;
            margin: 0 auto;
            height: 100%;
            border-left: 1px solid var(--md-border);
            border-right: 1px solid var(--md-border);
        }

        .column {
            display: flex;
            flex-direction: column;
            height: 100%;
            overflow-y: auto;
            padding: 16px;
            border-right: 1px solid var(--md-border);
        }
        .column:last-child { border-right: none; }

        /* --- Material Input --- */
        .md-input-wrapper {
            position: relative;
            margin-bottom: 24px;
        }
        .md-input-wrapper .material-symbols-outlined {
            position: absolute;
            left: 12px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--md-text-muted);
            font-size: 20px;
        }
        .md-input {
            width: 100%;
            background-color: var(--md-surface);
            border: 1px solid var(--md-border);
            border-radius: 4px 4px 0 0;
            padding: 12px 12px 12px 40px;
            color: var(--md-text);
            font-family: 'Roboto', sans-serif;
            font-size: 14px;
            outline: none;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        .md-input:focus {
            border-bottom: 2px solid var(--md-primary);
            box-shadow: 0 1px 0 0 var(--md-primary);
        }

        /* --- Material Buttons --- */
        .md-btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 10px 24px;
            border-radius: 4px;
            font-family: 'Roboto', sans-serif;
            font-size: 14px;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            cursor: pointer;
            border: none;
            transition: background-color 0.2s, box-shadow 0.2s;
            width: 100%;
            text-decoration: none;
        }
        .md-btn-filled {
            background-color: var(--md-primary);
            color: var(--md-on-primary);
            box-shadow: var(--md-elevation-1);
        }
        .md-btn-filled:hover {
            background-color: var(--md-primary-hover);
            box-shadow: var(--md-elevation-2);
        }
        .md-btn-outlined {
            background-color: transparent;
            color: var(--md-text);
            border: 1px solid var(--md-border);
        }
        .md-btn-outlined:hover {
            background-color: var(--md-surface-hover);
        }
        .md-btn-text {
            background-color: transparent;
            color: var(--md-primary);
            padding: 8px 16px;
            width: auto;
            text-transform: none;
            font-weight: 700;
        }
        .md-btn-text:hover {
            background-color: rgba(99, 100, 255, 0.1);
        }

        /* --- Типографика и контент --- */
        h1, h2, h3 { font-weight: 500; color: #ffffff; }
        h1 { font-size: 24px; margin-bottom: 8px; }
        h2 { font-size: 20px; margin-bottom: 16px; margin-top: 24px; }
        h3 { font-size: 16px; margin-bottom: 8px; }
        p, li { font-size: 14px; line-height: 1.5; color: var(--md-text); }
        .text-muted { color: var(--md-text-muted); font-size: 12px; }
        
        ul { list-style-type: disc; padding-left: 20px; margin-bottom: 16px; }
        ul li { margin-bottom: 4px; }

        /* --- Специфические блоки --- */
        .stats-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin: 24px 0;
        }
        .stat-label { font-size: 10px; text-transform: uppercase; color: var(--md-text-muted); margin-bottom: 4px; }
        .stat-value { font-size: 14px; font-weight: 700; }
        
        .placeholder-img {
            width: 100%;
            height: 120px;
            background: linear-gradient(135deg, #fceabb 0%, #f8b500 100%);
            border-radius: 8px;
            margin: 16px 0;
            position: relative;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .footer-links {
            margin-top: auto;
            font-size: 12px;
            color: var(--md-text-muted);
            line-height: 1.8;
        }
        .footer-links a { color: var(--md-text-muted); text-decoration: none; }
        .footer-links a:hover { text-decoration: underline; }

        .logo-container {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 32px;
        }
        .logo-icon {
            width: 32px; height: 32px;
            background-color: var(--md-primary);
            border-radius: 8px;
            display: flex; align-items: center; justify-content: center;
            color: white; font-weight: bold; font-size: 18px;
        }

        /* --- Модальное окно --- */
        .md-modal-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7);
            display: none; align-items: center; justify-content: center;
            z-index: 1000;
        }
        .md-modal {
            background: var(--md-surface);
            padding: 24px;
            border-radius: 8px;
            width: 100%; max-width: 400px;
            box-shadow: var(--md-elevation-2);
            border: 1px solid var(--md-border);
        }
        .md-modal h2 { margin-top: 0; margin-bottom: 16px; }
        .modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
        
        /* --- Лента --- */
        .post-card {
            background: var(--md-surface);
            border: 1px solid var(--md-border);
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 16px;
            box-shadow: var(--md-elevation-1);
        }
        .post-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
        .avatar {
            width: 24px; height: 24px; background: var(--md-primary); border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 12px; font-weight: bold; color: white;
        }
        
        @media (max-width: 900px) {
            .layout-grid { grid-template-columns: 1fr; }
            .column { display: none; }
            .column.main-column { display: flex; }
        }
    </style>
</head>
<body>
    <div class="layout-grid">
        
        <!-- Левая колонка -->
        <div class="column">
            <div class="md-input-wrapper">
                <span class="material-symbols-outlined">search</span>
                <input type="text" class="md-input" placeholder="Поиск">
            </div>
            
            <p><span style="font-weight:700; color:white;">litodon</span> — это один из многих независимых серверов Mastodon, которые вы можете использовать, чтобы присоединиться к сети Fediverse.</p>
            
            <div class="placeholder-img">
                <!-- SVG иллюстрация (замена эмодзи) -->
                <svg width="80" height="80" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z" fill="rgba(0,0,0,0.2)"/>
                    <path d="M12 6c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6zm0 10c-2.21 0-4-1.79-4-4s1.79-4 4-4 4 1.79 4 4-1.79 4-4 4z" fill="rgba(0,0,0,0.4)"/>
                    <path d="M12 9c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3zm0 4c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1z" fill="rgba(0,0,0,0.6)"/>
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
                    <div class="stat-value">686 <span class="text-muted" style="font-weight:400;">активные пользователи</span></div>
                </div>
            </div>

            <div class="footer-links">
                <p><a href="#">litodon: Об этом сервере</a> · <a href="#">Состояние сервера</a> · <a href="#">Каталог профилей</a> · <a href="#">Политика конфиденциальности</a></p>
                <p>Litodon: <a href="#">О проекте</a> · <a href="#">Скачать приложение</a> · v1.0.0</p>
            </div>
        </div>

        <!-- Центральная колонка -->
        <div class="column main-column">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
                <h1 style="display:none;" id="mobile-logo">Litodon</h1>
                <div style="display:flex; align-items:center; gap:12px; margin-left:auto;">
                    <div id="user-info" style="font-weight:500; color:white;"></div>
                    <button id="logout-btn" onclick="logout()" class="md-btn md-btn-text" style="display:none; width:auto;">Выйти</button>
                </div>
            </div>

            <!-- Форма поста -->
            <div id="post-form-container" style="display:none; background:var(--md-surface); padding:16px; border-radius:8px; border:1px solid var(--md-border); margin-bottom:24px;">
                <textarea id="post-content" rows="3" class="md-input" style="padding:12px; border-radius:4px; resize:none; margin-bottom:8px;" placeholder="Что нового?"></textarea>
                <div style="display:flex; justify-content:flex-end;">
                    <button onclick="submitPost()" class="md-btn md-btn-filled" style="width:auto;">Отправить</button>
                </div>
            </div>

            <!-- Лента -->
            <div id="timeline" style="display:none;"></div>

            <!-- Правила (для гостей) -->
            <div id="rules-container">
                <h2>Подробнее</h2>
                
                <h3>LML — про общение людей</h3>
                <ul>
                    <li>Аккаунты, предназначенные исключительно для коммерческой деятельности запрещены</li>
                    <li>Для аккаунтов ботов/групп в профиле должен быть указан аккаунт ответственного</li>
                    <li>Боты, которые просто пересылают контент с других сайтов должны постить вне публичных лент</li>
                </ul>

                <h3>LML — зона безопасного общения</h3>
                <h4 style="font-size:14px; font-weight:700; margin-bottom:4px;">Не место для ненависти</h4>
                <p style="margin-bottom:8px;">Разжигание ненависти по признакам, которые люди не выбирали — полностью под запретом</p>
                <ul>
                    <li>Гомофобия, трансфобия, энифобия, и т.д</li>
                    <li>Сексизм, расизм, нацизм, и т.д</li>
                </ul>
                <p>Сюда также входит одобрение/поддержка вышеупомянутых взглядов</p>

                <h3 style="margin-top:24px;">Не место для травли</h3>
                <p style="margin-bottom:8px;">Полностью запрещено</p>
                <ul>
                    <li>Разглашение чужой конфиденциальной информации</li>
                    <li>Преследование</li>
                    <li>Попытки обойти блок</li>
                    <li>Имперсонация, выдача себя за других</li>
                </ul>

                <p style="margin-top:16px;">Даже если вы делаете это вне LML, это может привести к блокировке вашего аккаунта здесь.</p>
                <p style="margin-top:8px;">Повторяющееся агрессивное поведение, разжигание конфликтов, может занять некоторое время. Мы изучаем все поступающие жалобы.</p>
            </div>
        </div>

        <!-- Правая колонка -->
        <div class="column">
            <div class="logo-container">
                <div class="logo-icon">L</div>
                <h1 style="margin:0; font-size:24px;">litodon</h1>
            </div>

            <div style="margin-bottom:24px;">
                <div style="display:flex; align-items:center; gap:4px; color:var(--md-text-muted); font-size:12px; text-transform:uppercase; font-weight:500; margin-bottom:16px;">
                    <span class="material-symbols-outlined" style="font-size:16px;">trending_up</span>
                    Актуальное
                </div>
            </div>

            <p style="font-weight:700; color:white; margin-bottom:8px;">Litodon — лучший способ быть в курсе всего происходящего.</p>
            <p style="margin-bottom:24px;">Подписывайтесь на кого угодно в федиверсе и читайте ленту в хронологическом порядке. Никаких алгоритмов, рекламы и кликбейта.</p>
            
            <div id="auth-buttons" style="display:flex; flex-direction:column; gap:12px;">
                <button onclick="openModal('register')" class="md-btn md-btn-filled">Зарегистрироваться</button>
                <button onclick="openModal('login')" class="md-btn md-btn-outlined">Войти</button>
            </div>
        </div>
    </div>

    <!-- Модальное окно авторизации -->
    <div id="auth-modal" class="md-modal-overlay">
        <div class="md-modal">
            <h2 id="auth-title">Войти</h2>
            <div id="auth-error" style="display:none; color:#ff5252; font-size:14px; margin-bottom:16px;"></div>
            <div class="md-input-wrapper">
                <input type="text" id="username" class="md-input" placeholder="Имя пользователя" style="padding-left:12px;">
            </div>
            <div class="md-input-wrapper">
                <input type="password" id="password" class="md-input" placeholder="Пароль" style="padding-left:12px;">
            </div>
            <div class="modal-actions">
                <button onclick="closeModal()" class="md-btn md-btn-text">Отмена</button>
                <button id="auth-submit" onclick="submitAuth()" class="md-btn md-btn-filled" style="width:auto;">Войти</button>
            </div>
        </div>
    </div>

    <script>
        let currentAuthMode = 'login';
        let currentUser = null;

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
            const mobileLogo = document.getElementById('mobile-logo');

            if (currentUser) {
                authButtons.style.display = 'none';
                postForm.style.display = 'block';
                rulesContainer.style.display = 'none';
                timeline.style.display = 'block';
                userInfo.innerText = `@${currentUser}`;
                logoutBtn.style.display = 'block';
                mobileLogo.style.display = 'block';
                loadPosts();
            } else {
                authButtons.style.display = 'flex';
                postForm.style.display = 'none';
                rulesContainer.style.display = 'block';
                timeline.style.display = 'none';
                userInfo.innerText = '';
                logoutBtn.style.display = 'none';
                mobileLogo.style.display = 'none';
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

        async function loadPosts() {
            const res = await fetch('/api/posts');
            const posts = await res.json();
            const timeline = document.getElementById('timeline');
            timeline.innerHTML = '';

            if (posts.length === 0) {
                timeline.innerHTML = '<p class="text-muted" style="text-align:center; padding:32px 0;">Пока нет постов. Будьте первым!</p>';
                return;
            }

            posts.forEach(post => {
                const date = new Date(post.timestamp).toLocaleString('ru-RU');
                const postEl = document.createElement('div');
                postEl.className = 'post-card';
                postEl.innerHTML = `
                    <div class="post-header">
                        <div class="avatar">${post.author[0].toUpperCase()}</div>
                        <span style="font-weight:700; color:white;">@${post.author}</span>
                        <span class="text-muted">${date}</span>
                    </div>
                    <p style="white-space:pre-wrap;">${escapeHtml(post.content)}</p>
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

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.innerText = text;
            return div.innerHTML;
        }

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
