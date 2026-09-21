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
        # Структура: {username: {"password": str, "display_name": str, "bio": str}}
        self.users = {}
        # Структура: [{"id": str, "author": str, "content": str, "timestamp": str}]
        self.posts = []
        # Структура: {session_token: username}
        self.sessions = {}

db = InMemoryStorage()

app = FastAPI(title="Litodon")

# --- Pydantic модели ---
class UserRegister(BaseModel):
    username: str
    display_name: str
    password: str
    confirm_password: str

class UserLogin(BaseModel):
    username: str
    password: str

class PostCreate(BaseModel):
    content: str

class ProfileUpdate(BaseModel):
    display_name: str
    bio: str

# --- API Эндпоинты ---

@app.post("/api/register")
async def register(data: UserRegister, response: Response):
    if data.username in db.users:
        raise HTTPException(status_code=400, detail="Этот ник уже занят")
    if not data.username or not data.password or not data.display_name:
        raise HTTPException(status_code=400, detail="Заполните все обязательные поля")
    if data.password != data.confirm_password:
        raise HTTPException(status_code=400, detail="Пароли не совпадают")
    if len(data.password) < 4:
        raise HTTPException(status_code=400, detail="Пароль должен быть не менее 4 символов")

    db.users[data.username] = {
        "password": data.password,
        "display_name": data.display_name,
        "bio": ""
    }
    token = str(uuid.uuid4())
    db.sessions[token] = data.username
    response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax")
    return {"message": "Успешная регистрация", "username": data.username}

@app.post("/api/login")
async def login(data: UserLogin, response: Response):
    user = db.users.get(data.username)
    if not user or user["password"] != data.password:
        raise HTTPException(status_code=400, detail="Неверный ник или пароль")
    
    token = str(uuid.uuid4())
    db.sessions[token] = data.username
    response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax")
    return {"message": "Успешный вход", "username": data.username}

@app.post("/api/logout")
async def logout(response: Response, session_token: Optional[str] = Cookie(None)):
    if session_token in db.sessions:
        del db.sessions[session_token]
    response.delete_cookie("session_token")
    return {"message": "Вы вышли из системы"}

@app.get("/api/me")
async def get_me(session_token: Optional[str] = Cookie(None)):
    if session_token and session_token in db.sessions:
        username = db.sessions[session_token]
        user_data = db.users[username]
        return {
            "username": username,
            "display_name": user_data["display_name"],
            "bio": user_data["bio"]
        }
    return {"username": None}

@app.post("/api/profile/update")
async def update_profile(data: ProfileUpdate, session_token: Optional[str] = Cookie(None)):
    if not session_token or session_token not in db.sessions:
        raise HTTPException(status_code=401, detail="Неавторизован")
    
    username = db.sessions[session_token]
    db.users[username]["display_name"] = data.display_name
    db.users[username]["bio"] = data.bio
    return {"message": "Профиль обновлен", "display_name": data.display_name, "bio": data.bio}

@app.get("/api/posts")
async def get_posts():
    result = []
    for post in sorted(db.posts, key=lambda x: x["timestamp"], reverse=True):
        author_data = db.users.get(post["author"], {})
        result.append({
            "id": post["id"],
            "author": post["author"],
            "display_name": author_data.get("display_name", post["author"]),
            "content": post["content"],
            "timestamp": post["timestamp"]
        })
    return result

@app.get("/api/profile/{username}/posts")
async def get_profile_posts(username: str):
    result = []
    for post in sorted(db.posts, key=lambda x: x["timestamp"], reverse=True):
        if post["author"] == username:
            author_data = db.users.get(post["author"], {})
            result.append({
                "id": post["id"],
                "author": post["author"],
                "display_name": author_data.get("display_name", post["author"]),
                "content": post["content"],
                "timestamp": post["timestamp"]
            })
    return result

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


# --- HTML / CSS / JS Интерфейс ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Litodon</title>
    <style>
        :root {
            --bg-color: #191b22;
            --col-bg: #282c37;
            --border-color: #393f4f;
            --text-main: #d9e1e8;
            --text-muted: #606984;
            --accent: #6364ff;
            --accent-hover: #5051db;
            --danger: #df405a;
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

        .app-container {
            display: grid;
            grid-template-columns: 285px 1fr 285px;
            max-width: 1200px;
            margin: 0 auto;
            height: 100vh;
            border-left: 1px solid var(--border-color);
            border-right: 1px solid var(--border-color);
        }

        .column { height: 100%; overflow-y: auto; padding: 16px; }
        .column::-webkit-scrollbar { width: 8px; }
        .column::-webkit-scrollbar-thumb { background: var(--border-color); border-radius: 4px; }

        .left-col { border-right: 1px solid var(--border-color); display: flex; flex-direction: column; }
        .center-col { padding: 0; }
        .right-col { border-left: 1px solid var(--border-color); }

        /* --- Левая колонка --- */
        .search-box { position: relative; margin-bottom: 24px; }
        .search-box input {
            width: 100%; background: var(--col-bg); border: 1px solid var(--border-color);
            border-radius: 4px; padding: 8px 12px 8px 36px; color: var(--text-main);
            font-size: 14px; outline: none;
        }
        .search-box input:focus { border-color: var(--accent); }
        .search-box svg {
            position: absolute; left: 10px; top: 50%; transform: translateY(-50%);
            width: 16px; height: 16px; fill: var(--text-muted);
        }

        .left-col p { margin-bottom: 16px; font-size: 14px; }
        
        .stats-grid {
            display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
            margin: 24px 0; font-size: 13px;
        }
        .stat-label { color: var(--text-muted); font-size: 11px; text-transform: uppercase; margin-bottom: 4px; }
        .stat-value { font-weight: bold; color: white; }

        .footer-links {
            margin-top: auto; font-size: 12px; color: var(--text-muted); line-height: 1.8;
        }
        .footer-links a { color: var(--text-muted); text-decoration: none; }
        .footer-links a:hover { text-decoration: underline; }

        /* --- Центральная колонка: Шапка --- */
        .center-header {
            display: flex; justify-content: space-between; align-items: center;
            padding: 12px 16px; border-bottom: 1px solid var(--border-color);
            background: var(--bg-color); position: sticky; top: 0; z-index: 10;
        }
        .center-header h1 { font-size: 18px; color: white; margin: 0; }
        .user-controls { display: flex; align-items: center; gap: 12px; }
        .user-controls span { font-weight: bold; color: white; font-size: 14px; }
        .logout-btn {
            background: transparent; border: none; cursor: pointer; display: flex;
            align-items: center; color: var(--text-muted); padding: 4px; border-radius: 4px;
        }
        .logout-btn:hover { color: var(--danger); background: rgba(223, 64, 90, 0.1); }
        .logout-btn svg { width: 20px; height: 20px; fill: currentColor; }

        /* --- Компоненты --- */
        .btn {
            width: 100%; padding: 10px; border-radius: 4px; font-size: 14px;
            font-weight: 600; cursor: pointer; text-align: center; border: none; transition: background 0.2s;
        }
        .btn-primary { background: var(--accent); color: white; }
        .btn-primary:hover { background: var(--accent-hover); }
        .btn-secondary { background: transparent; color: white; border: 1px solid var(--border-color); }
        .btn-secondary:hover { background: var(--col-bg); }
        .btn-danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
        .btn-danger:hover { background: rgba(223, 64, 90, 0.1); }
        .btn-text { background: transparent; color: var(--text-muted); border: none; padding: 4px 8px; width: auto; font-size: 13px; }
        .btn-text:hover { color: white; }

        /* --- Правая колонка (Навигация) --- */
        .right-nav { display: flex; flex-direction: column; gap: 8px; margin-top: 24px; }
        .right-nav .btn { text-align: left; padding: 12px 16px; display: flex; align-items: center; gap: 12px; }
        .right-nav .btn svg { width: 20px; height: 20px; fill: currentColor; }
        .right-col h1 { font-size: 22px; color: white; margin-bottom: 24px; }
        .trending-header {
            display: flex; align-items: center; gap: 4px; color: var(--text-muted);
            font-size: 11px; text-transform: uppercase; font-weight: 600; margin-bottom: 16px;
        }
        .trending-header svg { width: 14px; height: 14px; fill: var(--text-muted); }
        .right-col p { font-size: 14px; margin-bottom: 16px; }
        .right-col .highlight { font-weight: bold; color: white; }

        /* --- Формы авторизации --- */
        .auth-container { padding: 32px; max-width: 400px; margin: 0 auto; }
        .auth-container h2 { font-size: 24px; color: white; margin-bottom: 24px; text-align: center; }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; font-size: 13px; color: var(--text-muted); margin-bottom: 6px; }
        .form-group input, .form-group textarea {
            width: 100%; background: var(--bg-color); border: 1px solid var(--border-color);
            border-radius: 4px; padding: 10px; color: white; font-size: 14px; outline: none;
            font-family: inherit;
        }
        .form-group input:focus, .form-group textarea:focus { border-color: var(--accent); }
        .error-msg { color: var(--danger); font-size: 13px; margin-bottom: 16px; text-align: center; }
        .auth-switch { text-align: center; margin-top: 16px; font-size: 14px; color: var(--text-muted); }
        .auth-switch a { color: var(--accent); cursor: pointer; text-decoration: none; }
        .auth-switch a:hover { text-decoration: underline; }

        /* --- Лента и посты --- */
        .compose-box { padding: 16px; border-bottom: 1px solid var(--border-color); display: flex; gap: 12px; }
        .compose-main { flex-grow: 1; display: flex; flex-direction: column; }
        .compose-textarea {
            width: 100%; background: transparent; border: none; color: white;
            font-family: inherit; font-size: 15px; resize: none; outline: none;
            min-height: 60px; margin-bottom: 8px;
        }
        .compose-textarea::placeholder { color: var(--text-muted); }
        .compose-actions {
            display: flex; justify-content: space-between; align-items: center;
            border-top: 1px solid var(--border-color); padding-top: 12px;
        }
        .compose-icons { display: flex; gap: 16px; color: var(--text-muted); }
        .compose-icons svg { width: 20px; height: 20px; fill: currentColor; cursor: pointer; transition: color 0.2s; }
        .compose-icons svg:hover { color: var(--accent); }
        .btn-publish {
            background: var(--accent); color: white; border: none; border-radius: 4px;
            padding: 8px 16px; font-size: 14px; font-weight: 600; cursor: pointer;
        }
        .btn-publish:hover { background: var(--accent-hover); }

        .feed-container { padding: 16px; }
        .post-card {
            background: var(--col-bg); border: 1px solid var(--border-color);
            border-radius: 4px; padding: 16px; margin-bottom: 10px;
        }
        .post-header { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
        .post-author-name { font-weight: bold; color: white; font-size: 15px; }
        .post-author-handle { color: var(--text-muted); font-size: 13px; }
        .post-date { color: var(--text-muted); font-size: 12px; margin-left: auto; }
        .post-content { white-space: pre-wrap; word-break: break-word; font-size: 15px; margin-bottom: 12px; }
        .post-actions { display: flex; gap: 24px; color: var(--text-muted); }
        .post-actions svg { width: 18px; height: 18px; fill: currentColor; cursor: pointer; }
        .post-actions svg:hover { color: var(--accent); }

        /* --- Профиль --- */
        .profile-header { padding: 24px; border-bottom: 1px solid var(--border-color); }
        .profile-name { font-size: 24px; font-weight: bold; color: white; margin-bottom: 4px; }
        .profile-handle { font-size: 16px; color: var(--text-muted); margin-bottom: 16px; }
        .profile-bio { font-size: 15px; margin-bottom: 16px; white-space: pre-wrap; }
        .profile-stats { display: flex; gap: 24px; font-size: 14px; color: var(--text-muted); }

        /* --- Настройки --- */
        .settings-container { padding: 24px; max-width: 500px; }
        .settings-container h2 { font-size: 20px; color: white; margin-bottom: 24px; }

        /* --- Правила --- */
        .rules-container { padding: 16px; }
        .rules-container h2 { font-size: 20px; color: white; margin-bottom: 16px; }
        .rules-container h3 { font-size: 16px; color: white; margin: 20px 0 8px 0; }
        .rules-container p { margin-bottom: 12px; }
        .rules-container ul { padding-left: 20px; margin-bottom: 16px; }
        .rules-container li { margin-bottom: 4px; }

        .hidden { display: none !important; }

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
            
            <p><span style="font-weight:700; color:white;">litodon</span> — русскоязычный сервер социальной сети. Зона общения, свободная от рекламы и шпионажа.</p>
            
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
                <p><a href="#">litodon: Об этом сервере</a> · <a href="#">Состояние сервера</a> · <a href="#">Каталог профилей</a> · <a href="#">Политика конфиденциальности</a></p>
                <p>litodon: <a href="#">О проекте</a> · <a href="#">Скачать приложение</a> · <a href="#">Сочетания клавиш</a> · v1.0.0</p>
            </div>
        </div>

        <!-- Центральная колонка (Динамическая) -->
        <div class="column center-col" id="center-column">
            
            <!-- Шапка (видна только авторизованным) -->
            <div id="user-header" class="center-header hidden">
                <h1 id="header-title">Главная</h1>
                <div class="user-controls">
                    <span id="header-user-info"></span>
                    <button onclick="logout()" class="logout-btn" title="Выйти">
                        <svg viewBox="0 0 24 24"><path d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg>
                    </button>
                </div>
            </div>

            <!-- View: Авторизация -->
            <div id="view-auth" class="auth-container">
                <div id="auth-error" class="error-msg hidden"></div>
                
                <!-- Форма входа -->
                <div id="login-form">
                    <h2>Войти в litodon</h2>
                    <div class="form-group">
                        <label>Ник (@username)</label>
                        <input type="text" id="login-username" placeholder="username">
                    </div>
                    <div class="form-group">
                        <label>Пароль</label>
                        <input type="password" id="login-password" placeholder="••••••••">
                    </div>
                    <button class="btn btn-primary" onclick="submitLogin()">Войти</button>
                    <div class="auth-switch">
                        Нет аккаунта? <a onclick="switchAuthView('register')">Зарегистрироваться</a>
                    </div>
                </div>

                <!-- Форма регистрации -->
                <div id="register-form" class="hidden">
                    <h2>Регистрация</h2>
                    <div class="form-group">
                        <label>Отображаемое имя</label>
                        <input type="text" id="reg-display-name" placeholder="Иван Иванов">
                    </div>
                    <div class="form-group">
                        <label>Ник (@username)</label>
                        <input type="text" id="reg-username" placeholder="ivan">
                    </div>
                    <div class="form-group">
                        <label>Пароль</label>
                        <input type="password" id="reg-password" placeholder="••••••••">
                    </div>
                    <div class="form-group">
                        <label>Повтор пароля</label>
                        <input type="password" id="reg-confirm-password" placeholder="••••••••">
                    </div>
                    <button class="btn btn-primary" onclick="submitRegister()">Создать аккаунт</button>
                    <div class="auth-switch">
                        Уже есть аккаунт? <a onclick="switchAuthView('login')">Войти</a>
                    </div>
                </div>

                <!-- Правила (видны только гостям) -->
                <div class="rules-container" style="margin-top: 32px; border-top: 1px solid var(--border-color); padding-top: 24px;">
                    <h3>LML — зона безопасного общения</h3>
                    <p>Разжигание ненависти, травля и преследование строго запрещены.</p>
                </div>
            </div>

            <!-- View: Главная -->
            <div id="view-home" class="hidden">
                <div class="compose-box">
                    <div class="compose-main">
                        <textarea id="post-content" class="compose-textarea" placeholder="Что у вас нового?"></textarea>
                        <div class="compose-actions">
                            <div class="compose-icons">
                                <svg viewBox="0 0 24 24"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>
                                <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
                            </div>
                            <button class="btn-publish" onclick="submitPost()">Опубликовать</button>
                        </div>
                    </div>
                </div>
                <div class="feed-container" id="home-timeline"></div>
            </div>

            <!-- View: Профиль -->
            <div id="view-profile" class="hidden">
                <div class="profile-header" id="profile-info">
                    <!-- Данные профиля рендерятся через JS -->
                </div>
                <div class="feed-container" id="profile-timeline"></div>
            </div>

            <!-- View: Настройки -->
            <div id="view-settings" class="hidden">
                <div class="settings-container">
                    <h2>Настройки профиля</h2>
                    <div id="settings-error" class="error-msg hidden"></div>
                    <div class="form-group">
                        <label>Отображаемое имя</label>
                        <input type="text" id="settings-display-name">
                    </div>
                    <div class="form-group">
                        <label>Описание профиля (Bio)</label>
                        <textarea id="settings-bio" rows="4"></textarea>
                    </div>
                    <button class="btn btn-primary" onclick="saveSettings()">Сохранить изменения</button>
                </div>
            </div>

        </div>

        <!-- Правая колонка (Навигация) -->
        <div class="column right-col">
            <h1>litodon</h1>

            <!-- Навигация для авторизованных -->
            <div id="right-nav" class="right-nav hidden">
                <button class="btn btn-secondary" onclick="renderView('home')">
                    <svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
                    Главная
                </button>
                <button class="btn btn-secondary" onclick="renderView('profile')">
                    <svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
                    Профиль
                </button>
                <button class="btn btn-secondary" onclick="renderView('settings')">
                    <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.04.24.24.41.48.41h3.84c.24 0 .43-.17.47-.41l.36-2.54c.59-.24 1.13-.57 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>
                    Настройки
                </button>
            </div>

            <!-- Информация для гостей -->
            <div id="guest-info">
                <div class="trending-header">
                    <svg viewBox="0 0 24 24"><path d="M16 6l2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6z"/></svg>
                    Актуальное
                </div>
                <p class="highlight">litodon — лучший способ быть в курсе всего происходящего.</p>
                <p>Подписывайтесь на кого угодно и читайте ленту в хронологическом порядке. Никаких алгоритмов, рекламы и кликбейта.</p>
            </div>
        </div>
    </div>

    <script>
        let currentUser = null;
        let currentView = 'auth';

        // --- Инициализация ---
        async function init() {
            const res = await fetch('/api/me');
            const data = await res.json();
            if (data.username) {
                currentUser = data;
                renderView('home');
            } else {
                currentUser = null;
                renderView('auth');
            }
        }

        // --- Управление интерфейсом ---
        function renderView(view) {
            currentView = view;
            
            // Скрываем все центральные блоки
            document.getElementById('view-auth').classList.add('hidden');
            document.getElementById('view-home').classList.add('hidden');
            document.getElementById('view-profile').classList.add('hidden');
            document.getElementById('view-settings').classList.add('hidden');

            const header = document.getElementById('user-header');
            const guestInfo = document.getElementById('guest-info');
            const rightNav = document.getElementById('right-nav');

            if (currentUser) {
                header.classList.remove('hidden');
                guestInfo.classList.add('hidden');
                rightNav.classList.remove('hidden');
                document.getElementById('header-user-info').innerText = `@${currentUser.username}`;

                if (view === 'home') {
                    document.getElementById('header-title').innerText = 'Главная';
                    document.getElementById('view-home').classList.remove('hidden');
                    loadPosts();
                } else if (view === 'profile') {
                    document.getElementById('header-title').innerText = 'Профиль';
                    document.getElementById('view-profile').classList.remove('hidden');
                    loadProfile();
                } else if (view === 'settings') {
                    document.getElementById('header-title').innerText = 'Настройки';
                    document.getElementById('view-settings').classList.remove('hidden');
                    loadSettings();
                }
            } else {
                header.classList.add('hidden');
                guestInfo.classList.remove('hidden');
                rightNav.classList.add('hidden');
                document.getElementById('view-auth').classList.remove('hidden');
            }
        }

        function switchAuthView(view) {
            document.getElementById('auth-error').classList.add('hidden');
            if (view === 'login') {
                document.getElementById('login-form').classList.remove('hidden');
                document.getElementById('register-form').classList.add('hidden');
            } else {
                document.getElementById('login-form').classList.add('hidden');
                document.getElementById('register-form').classList.remove('hidden');
            }
        }

        // --- Аутентификация ---
        async function submitLogin() {
            const username = document.getElementById('login-username').value;
            const password = document.getElementById('login-password').value;
            const errorDiv = document.getElementById('auth-error');

            try {
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail);
                
                await init();
            } catch (err) {
                errorDiv.innerText = err.message;
                errorDiv.classList.remove('hidden');
            }
        }

        async function submitRegister() {
            const display_name = document.getElementById('reg-display-name').value;
            const username = document.getElementById('reg-username').value;
            const password = document.getElementById('reg-password').value;
            const confirm_password = document.getElementById('reg-confirm-password').value;
            const errorDiv = document.getElementById('auth-error');

            try {
                const res = await fetch('/api/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, display_name, password, confirm_password })
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail);
                
                await init();
            } catch (err) {
                errorDiv.innerText = err.message;
                errorDiv.classList.remove('hidden');
            }
        }

        async function logout() {
            await fetch('/api/logout', { method: 'POST' });
            currentUser = null;
            renderView('auth');
        }

        // --- Лента и посты ---
        async function loadPosts() {
            const res = await fetch('/api/posts');
            const posts = await res.json();
            const timeline = document.getElementById('home-timeline');
            timeline.innerHTML = '';

            if (posts.length === 0) {
                timeline.innerHTML = '<p style="text-align:center; color:var(--text-muted); padding:32px 0;">Пока нет постов. Будьте первым!</p>';
                return;
            }
            posts.forEach(post => timeline.appendChild(createPostElement(post)));
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
            }
        }

        function createPostElement(post) {
            const date = new Date(post.timestamp).toLocaleString('ru-RU');
            const postEl = document.createElement('div');
            postEl.className = 'post-card';
            postEl.innerHTML = `
                <div class="post-header">
                    <span class="post-author-name">${escapeHtml(post.display_name)}</span>
                    <span class="post-author-handle">@${escapeHtml(post.author)}</span>
                    <span class="post-date">${date}</span>
                </div>
                <div class="post-content">${escapeHtml(post.content)}</div>
                <div class="post-actions">
                    <svg viewBox="0 0 24 24"><path d="M10 9V5l-7 7 7 7v-4.1c5 0 8.5 1.6 11 5.1-1-5-4-10-11-11z"/></svg>
                    <svg viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg>
                    <svg viewBox="0 0 24 24"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>
                </div>
            `;
            return postEl;
        }

        // --- Профиль ---
        async function loadProfile() {
            const infoDiv = document.getElementById('profile-info');
            const timeline = document.getElementById('profile-timeline');
            
            infoDiv.innerHTML = `
                <div class="profile-name">${escapeHtml(currentUser.display_name)}</div>
                <div class="profile-handle">@${escapeHtml(currentUser.username)}</div>
                <div class="profile-bio">${escapeHtml(currentUser.bio || 'Нет описания')}</div>
                <div class="profile-stats">
                    <span><strong>0</strong> подписчиков</span>
                    <span><strong>0</strong> подписок</span>
                </div>
            `;

            const res = await fetch(`/api/profile/${currentUser.username}/posts`);
            const posts = await res.json();
            timeline.innerHTML = '';
            
            if (posts.length === 0) {
                timeline.innerHTML = '<p style="text-align:center; color:var(--text-muted); padding:32px 0;">У вас пока нет постов.</p>';
                return;
            }
            posts.forEach(post => timeline.appendChild(createPostElement(post)));
        }

        // --- Настройки ---
        function loadSettings() {
            document.getElementById('settings-display-name').value = currentUser.display_name;
            document.getElementById('settings-bio').value = currentUser.bio || '';
            document.getElementById('settings-error').classList.add('hidden');
        }

        async function saveSettings() {
            const display_name = document.getElementById('settings-display-name').value;
            const bio = document.getElementById('settings-bio').value;
            const errorDiv = document.getElementById('settings-error');

            try {
                const res = await fetch('/api/profile/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ display_name, bio })
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail);

                currentUser.display_name = data.display_name;
                currentUser.bio = data.bio;
                
                // Возвращаемся в профиль, чтобы увидеть изменения
                renderView('profile');
            } catch (err) {
                errorDiv.innerText = err.message;
                errorDiv.classList.remove('hidden');
            }
        }

        // Защита от XSS
        function escapeHtml(text) {
            if (!text) return '';
            const div = document.createElement('div');
            div.innerText = text;
            return div.innerHTML;
        }

        // Запуск
        init();
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return HTML_TEMPLATE

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
