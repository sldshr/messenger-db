import uvicorn
from fastapi import FastAPI, Response, HTTPException, Cookie
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import uuid
from datetime import datetime

# --- ИНИЦИАЛИЗАЦИЯ IN-MEMORY ХРАНИЛИЩА ---
class InMemoryStorage:
    def __init__(self):
        self.users = {}       # {username: {"password": str, "display_name": str, "bio": str}}
        self.posts = []       # [{"id": str, "author": str, "content": str, "timestamp": str}]
        self.sessions = {}    # {session_token: username}
        self.comments = []    # [{"id": str, "post_id": str, "author": str, "content": str, "timestamp": str}]
        # Голоса: {target_id: {username: "up"|"down"}}
        self.votes = {}

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

class CommentCreate(BaseModel):
    content: str

class ProfileUpdate(BaseModel):
    display_name: str
    bio: str

class VoteRequest(BaseModel):
    target_id: str
    target_type: str  # "post" или "comment"
    vote_type: str    # "up" или "down"

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

def enrich_post(post, current_user):
    post_id = post["id"]
    votes = db.votes.get(post_id, {})
    upvotes = sum(1 for v in votes.values() if v == "up")
    downvotes = sum(1 for v in votes.values() if v == "down")
    user_vote = votes.get(current_user) if current_user else None
    author_data = db.users.get(post["author"], {})
    return {
        **post,
        "display_name": author_data.get("display_name", post["author"]),
        "upvotes": upvotes,
        "downvotes": downvotes,
        "user_vote": user_vote
    }

@app.get("/api/posts")
async def get_posts(session_token: Optional[str] = Cookie(None)):
    current_user = db.sessions.get(session_token) if session_token else None
    result = []
    for post in sorted(db.posts, key=lambda x: x["timestamp"], reverse=True):
        result.append(enrich_post(post, current_user))
    return result

@app.get("/api/posts/{post_id}")
async def get_post(post_id: str, session_token: Optional[str] = Cookie(None)):
    current_user = db.sessions.get(session_token) if session_token else None
    for post in db.posts:
        if post["id"] == post_id:
            return enrich_post(post, current_user)
    raise HTTPException(status_code=404, detail="Пост не найден")

@app.get("/api/posts/{post_id}/comments")
async def get_comments(post_id: str, session_token: Optional[str] = Cookie(None)):
    current_user = db.sessions.get(session_token) if session_token else None
    result = []
    for comment in db.comments:
        if comment["post_id"] == post_id:
            votes = db.votes.get(comment["id"], {})
            upvotes = sum(1 for v in votes.values() if v == "up")
            downvotes = sum(1 for v in votes.values() if v == "down")
            user_vote = votes.get(current_user) if current_user else None
            author_data = db.users.get(comment["author"], {})
            result.append({
                **comment,
                "display_name": author_data.get("display_name", comment["author"]),
                "upvotes": upvotes,
                "downvotes": downvotes,
                "user_vote": user_vote
            })
    return sorted(result, key=lambda x: x["timestamp"], reverse=True)

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

@app.post("/api/posts/{post_id}/comments")
async def create_comment(post_id: str, data: CommentCreate, session_token: Optional[str] = Cookie(None)):
    if not session_token or session_token not in db.sessions:
        raise HTTPException(status_code=401, detail="Неавторизован")
    if not data.content.strip():
        raise HTTPException(status_code=400, detail="Комментарий не может быть пустым")

    # Проверяем, существует ли пост
    if not any(p["id"] == post_id for p in db.posts):
        raise HTTPException(status_code=404, detail="Пост не найден")

    username = db.sessions[session_token]
    new_comment = {
        "id": str(uuid.uuid4()),
        "post_id": post_id,
        "author": username,
        "content": data.content,
        "timestamp": datetime.now().isoformat()
    }
    db.comments.append(new_comment)
    return new_comment

@app.post("/api/vote")
async def vote(data: VoteRequest, session_token: Optional[str] = Cookie(None)):
    if not session_token or session_token not in db.sessions:
        raise HTTPException(status_code=401, detail="Неавторизован для голосования")
    
    username = db.sessions[session_token]
    target_id = data.target_id
    
    # Инициализируем словарь голосов для цели, если его нет
    if target_id not in db.votes:
        db.votes[target_id] = {}
    
    # Если пользователь уже голосовал, удаляем старый голос
    if username in db.votes[target_id]:
        old_vote = db.votes[target_id][username]
        if old_vote == data.vote_type:
            # Если голос совпадает, отменяем голос (toggle)
            del db.votes[target_id][username]
            return {"message": "Голос отменен"}
    
    # Записываем новый голос
    db.votes[target_id][username] = data.vote_type
    return {"message": "Голос учтен"}


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
            /* Темная тема (по умолчанию) */
            --bg-color: #191b22;
            --col-bg: #282c37;
            --border-color: #393f4f;
            --text-main: #d9e1e8;
            --text-muted: #606984;
            --accent: #6364ff;
            --accent-hover: #5051db;
            --danger: #df405a;
            --upvote: #ff4500;
            --downvote: #7193ff;
        }

        [data-theme="light"] {
            /* Светлая тема */
            --bg-color: #f0f2f5;
            --col-bg: #ffffff;
            --border-color: #d1d5db;
            --text-main: #1a1a1a;
            --text-muted: #65676b;
            --accent: #6364ff;
            --accent-hover: #5051db;
            --danger: #df405a;
            --upvote: #ff4500;
            --downvote: #7193ff;
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
            transition: background-color 0.2s, color 0.2s;
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
        .stat-value { font-weight: bold; color: var(--text-main); }

        /* --- Центральная колонка: Шапка --- */
        .center-header {
            display: flex; justify-content: space-between; align-items: center;
            padding: 12px 16px; border-bottom: 1px solid var(--border-color);
            background: var(--bg-color); position: sticky; top: 0; z-index: 10;
        }
        .center-header h1 { font-size: 18px; color: var(--text-main); margin: 0; }
        .user-controls { display: flex; align-items: center; gap: 12px; }
        .user-controls span { font-weight: bold; color: var(--text-main); font-size: 14px; }
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
        .btn-secondary { background: transparent; color: var(--text-main); border: 1px solid var(--border-color); }
        .btn-secondary:hover { background: var(--col-bg); }
        .btn-danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
        .btn-danger:hover { background: rgba(223, 64, 90, 0.1); }
        .btn-text { background: transparent; color: var(--text-muted); border: none; padding: 4px 8px; width: auto; font-size: 13px; }
        .btn-text:hover { color: var(--text-main); }

        /* --- Правая колонка (Навигация) --- */
        .right-nav { display: flex; flex-direction: column; gap: 8px; margin-top: 24px; }
        .right-nav .btn { text-align: left; padding: 12px 16px; display: flex; align-items: center; gap: 12px; }
        .right-nav .btn svg { width: 20px; height: 20px; fill: currentColor; }
        .right-col h1 { font-size: 22px; color: var(--text-main); margin-bottom: 24px; }
        .trending-header {
            display: flex; align-items: center; gap: 4px; color: var(--text-muted);
            font-size: 11px; text-transform: uppercase; font-weight: 600; margin-bottom: 16px;
        }
        .trending-header svg { width: 14px; height: 14px; fill: var(--text-muted); }
        .right-col p { font-size: 14px; margin-bottom: 16px; }
        .right-col .highlight { font-weight: bold; color: var(--text-main); }

        /* --- Формы авторизации --- */
        .auth-container { padding: 32px; max-width: 400px; margin: 0 auto; }
        .auth-container h2 { font-size: 24px; color: var(--text-main); margin-bottom: 24px; text-align: center; }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; font-size: 13px; color: var(--text-muted); margin-bottom: 6px; }
        .form-group input, .form-group textarea, .form-group select {
            width: 100%; background: var(--bg-color); border: 1px solid var(--border-color);
            border-radius: 4px; padding: 10px; color: var(--text-main); font-size: 14px; outline: none;
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
            width: 100%; background: transparent; border: none; color: var(--text-main);
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
            display: flex; gap: 12px;
        }
        .vote-controls {
            display: flex; flex-direction: column; align-items: center; gap: 4px;
            min-width: 30px;
        }
        .vote-btn {
            background: transparent; border: none; cursor: pointer; color: var(--text-muted);
            display: flex; align-items: center; justify-content: center; padding: 4px;
            border-radius: 4px; transition: color 0.2s, background 0.2s;
        }
        .vote-btn:hover { background: rgba(128, 128, 128, 0.1); }
        .vote-btn.upvote:hover, .vote-btn.upvote.active { color: var(--upvote); }
        .vote-btn.downvote:hover, .vote-btn.downvote.active { color: var(--downvote); }
        .vote-btn svg { width: 20px; height: 20px; fill: currentColor; }
        .vote-count { font-size: 13px; font-weight: bold; }
        .vote-count.up { color: var(--upvote); }
        .vote-count.down { color: var(--downvote); }

        .post-main { flex-grow: 1; }
        .post-header { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
        .post-author-name { font-weight: bold; color: var(--text-main); font-size: 15px; }
        .post-author-handle { color: var(--text-muted); font-size: 13px; }
        .post-date { color: var(--text-muted); font-size: 12px; margin-left: auto; }
        .post-content { white-space: pre-wrap; word-break: break-word; font-size: 15px; margin-bottom: 12px; cursor: pointer; }
        .post-content:hover { color: var(--accent); }
        .post-footer { display: flex; gap: 16px; font-size: 13px; color: var(--text-muted); }
        .post-footer a { color: var(--text-muted); text-decoration: none; display: flex; align-items: center; gap: 4px; }
        .post-footer a:hover { color: var(--accent); }
        .post-footer svg { width: 16px; height: 16px; fill: currentColor; }

        /* --- Профиль --- */
        .profile-header { padding: 24px; border-bottom: 1px solid var(--border-color); }
        .profile-name { font-size: 24px; font-weight: bold; color: var(--text-main); margin-bottom: 4px; }
        .profile-handle { font-size: 16px; color: var(--text-muted); margin-bottom: 16px; }
        .profile-bio { font-size: 15px; margin-bottom: 16px; white-space: pre-wrap; }
        .profile-stats { display: flex; gap: 24px; font-size: 14px; color: var(--text-muted); }

        /* --- Настройки --- */
        .settings-container { padding: 24px; max-width: 500px; }
        .settings-container h2 { font-size: 20px; color: var(--text-main); margin-bottom: 24px; }
        .settings-section { margin-bottom: 32px; border-bottom: 1px solid var(--border-color); padding-bottom: 24px; }
        .settings-section:last-child { border-bottom: none; }

        /* --- Страница поста --- */
        .post-detail-container { padding: 16px; }
        .back-btn { display: inline-flex; align-items: center; gap: 8px; color: var(--text-muted); text-decoration: none; margin-bottom: 16px; cursor: pointer; font-size: 14px; }
        .back-btn:hover { color: var(--text-main); }
        .back-btn svg { width: 20px; height: 20px; fill: currentColor; }
        
        .comments-section { margin-top: 24px; border-top: 1px solid var(--border-color); padding-top: 16px; }
        .comments-section h3 { font-size: 16px; color: var(--text-main); margin-bottom: 16px; }
        .comment-form { display: flex; flex-direction: column; gap: 8px; margin-bottom: 24px; }
        .comment-form textarea {
            width: 100%; background: var(--col-bg); border: 1px solid var(--border-color);
            border-radius: 4px; padding: 10px; color: var(--text-main); font-family: inherit;
            font-size: 14px; resize: vertical; min-height: 60px; outline: none;
        }
        .comment-form textarea:focus { border-color: var(--accent); }
        .comment-card {
            background: var(--col-bg); border: 1px solid var(--border-color);
            border-radius: 4px; padding: 12px; margin-bottom: 10px;
            display: flex; gap: 12px;
        }
        .comment-main { flex-grow: 1; }
        .comment-header { display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; }
        .comment-author { font-weight: bold; color: var(--text-main); font-size: 14px; }
        .comment-date { color: var(--text-muted); font-size: 12px; margin-left: auto; }
        .comment-content { font-size: 14px; white-space: pre-wrap; }

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
            
            <p><span style="font-weight:700; color:var(--text-main);">litodon</span> — русскоязычный сервер социальной сети. Зона общения, свободная от рекламы и шпионажа.</p>
            
            <div class="stats-grid">
                <div>
                    <div class="stat-label">Управляется:</div>
                    <div class="stat-value">@mo</div>
                </div>
                <div>
                    <div class="stat-label">Статистика:</div>
                    <div class="stat-value">686 <span style="font-weight:400; color:var(--text-muted); font-size:12px;">активные</span></div>
                </div>
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
                
                <div id="login-form">
                    <h2 id="t-login-title">Войти в litodon</h2>
                    <div class="form-group">
                        <label id="t-username-label">Ник (@username)</label>
                        <input type="text" id="login-username" placeholder="username">
                    </div>
                    <div class="form-group">
                        <label id="t-password-label">Пароль</label>
                        <input type="password" id="login-password" placeholder="••••••••">
                    </div>
                    <button class="btn btn-primary" onclick="submitLogin()" id="t-login-btn">Войти</button>
                    <div class="auth-switch">
                        <span id="t-no-account">Нет аккаунта?</span> <a onclick="switchAuthView('register')" id="t-register-link">Зарегистрироваться</a>
                    </div>
                </div>

                <div id="register-form" class="hidden">
                    <h2 id="t-register-title">Регистрация</h2>
                    <div class="form-group">
                        <label id="t-display-name-label">Отображаемое имя</label>
                        <input type="text" id="reg-display-name" placeholder="Иван Иванов">
                    </div>
                    <div class="form-group">
                        <label id="t-username-label-reg">Ник (@username)</label>
                        <input type="text" id="reg-username" placeholder="ivan">
                    </div>
                    <div class="form-group">
                        <label id="t-password-label-reg">Пароль</label>
                        <input type="password" id="reg-password" placeholder="••••••••">
                    </div>
                    <div class="form-group">
                        <label id="t-confirm-password-label">Повтор пароля</label>
                        <input type="password" id="reg-confirm-password" placeholder="••••••••">
                    </div>
                    <button class="btn btn-primary" onclick="submitRegister()" id="t-register-btn">Создать аккаунт</button>
                    <div class="auth-switch">
                        <span id="t-have-account">Уже есть аккаунт?</span> <a onclick="switchAuthView('login')" id="t-login-link">Войти</a>
                    </div>
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
                            <button class="btn-publish" onclick="submitPost()" id="t-publish-btn">Опубликовать</button>
                        </div>
                    </div>
                </div>
                <div class="feed-container" id="home-timeline"></div>
            </div>

            <!-- View: Страница поста -->
            <div id="view-post" class="hidden">
                <div class="post-detail-container">
                    <a class="back-btn" onclick="renderView('home')">
                        <svg viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
                        <span id="t-back">Назад</span>
                    </a>
                    <div id="post-detail-content"></div>
                    
                    <div class="comments-section">
                        <h3 id="t-comments">Комментарии</h3>
                        <div class="comment-form">
                            <textarea id="comment-content" placeholder="Написать комментарий..."></textarea>
                            <button class="btn btn-primary" onclick="submitComment()" id="t-send-comment">Отправить</button>
                        </div>
                        <div id="comments-list"></div>
                    </div>
                </div>
            </div>

            <!-- View: Профиль -->
            <div id="view-profile" class="hidden">
                <div class="profile-header" id="profile-info"></div>
                <div class="feed-container" id="profile-timeline"></div>
            </div>

            <!-- View: Настройки -->
            <div id="view-settings" class="hidden">
                <div class="settings-container">
                    <h2 id="t-settings-title">Настройки</h2>
                    
                    <div class="settings-section">
                        <div class="form-group">
                            <label id="t-language">Язык интерфейса</label>
                            <select id="settings-language" onchange="changeLanguage(this.value)">
                                <option value="ru">Русский</option>
                                <option value="en">English</option>
                            </select>
                        </div>
                    </div>
                    
                    <div class="settings-section">
                        <div class="form-group">
                            <label id="t-theme">Тема оформления</label>
                            <select id="settings-theme" onchange="changeTheme(this.value)">
                                <option value="dark">Тёмная</option>
                                <option value="light">Светлая</option>
                            </select>
                        </div>
                    </div>
                </div>
            </div>

        </div>

        <!-- Правая колонка (Навигация) -->
        <div class="column right-col">
            <h1>litodon</h1>

            <div id="right-nav" class="right-nav hidden">
                <button class="btn btn-secondary" onclick="renderView('home')">
                    <svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
                    <span id="t-nav-home">Главная</span>
                </button>
                <button class="btn btn-secondary" onclick="renderView('profile')">
                    <svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
                    <span id="t-nav-profile">Профиль</span>
                </button>
                <button class="btn btn-secondary" onclick="renderView('settings')">
                    <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.04.24.24.41.48.41h3.84c.24 0 .43-.17.47-.41l.36-2.54c.59-.24 1.13-.57 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>
                    <span id="t-nav-settings">Настройки</span>
                </button>
            </div>

            <div id="guest-info">
                <div class="trending-header">
                    <svg viewBox="0 0 24 24"><path d="M16 6l2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6z"/></svg>
                    <span id="t-trending">Актуальное</span>
                </div>
                <p class="highlight" id="t-guest-highlight">litodon — лучший способ быть в курсе всего происходящего.</p>
                <p id="t-guest-desc">Подписывайтесь на кого угодно и читайте ленту в хронологическом порядке. Никаких алгоритмов, рекламы и кликбейта.</p>
            </div>
        </div>
    </div>

    <script>
        // --- Словарь переводов ---
        const translations = {
            ru: {
                login_title: "Войти в litodon", username: "Ник (@username)", password: "Пароль",
                login_btn: "Войти", no_account: "Нет аккаунта?", register_link: "Зарегистрироваться",
                register_title: "Регистрация", display_name: "Отображаемое имя", confirm_password: "Повтор пароля",
                register_btn: "Создать аккаунт", have_account: "Уже есть аккаунт?", login_link: "Войти",
                publish_btn: "Опубликовать", back: "Назад", comments: "Комментарии",
                send_comment: "Отправить", settings_title: "Настройки", language: "Язык интерфейса",
                theme: "Тема оформления", nav_home: "Главная", nav_profile: "Профиль", nav_settings: "Настройки",
                trending: "Актуальное", guest_highlight: "litodon — лучший способ быть в курсе всего происходящего.",
                guest_desc: "Подписывайтесь на кого угодно и читайте ленту в хронологическом порядке. Никаких алгоритмов, рекламы и кликбейта.",
                edit_profile: "Редактировать", save_profile: "Сохранить", edit_name: "Имя", edit_bio: "Описание",
                no_posts: "Пока нет постов.", no_comments: "Нет комментариев. Будьте первым!",
                logout: "Выйти", header_home: "Главная", header_profile: "Профиль", header_settings: "Настройки"
            },
            en: {
                login_title: "Login to litodon", username: "Username (@nick)", password: "Password",
                login_btn: "Login", no_account: "No account?", register_link: "Register",
                register_title: "Registration", display_name: "Display Name", confirm_password: "Confirm Password",
                register_btn: "Create Account", have_account: "Already have an account?", login_link: "Login",
                publish_btn: "Publish", back: "Back", comments: "Comments",
                send_comment: "Send", settings_title: "Settings", language: "Language",
                theme: "Theme", nav_home: "Home", nav_profile: "Profile", nav_settings: "Settings",
                trending: "Trending", guest_highlight: "litodon is the best way to stay up to date.",
                guest_desc: "Follow anyone and read the feed in chronological order. No algorithms, ads or clickbait.",
                edit_profile: "Edit", save_profile: "Save", edit_name: "Name", edit_bio: "Bio",
                no_posts: "No posts yet.", no_comments: "No comments yet. Be the first!",
                logout: "Logout", header_home: "Home", header_profile: "Profile", header_settings: "Settings"
            }
        };

        let currentLang = 'ru';
        let currentTheme = 'dark';
        let currentUser = null;
        let currentView = 'auth';
        let currentPostId = null;
        let isEditingProfile = false;

        function t(key) { return translations[currentLang][key] || key; }

        function applyTheme() {
            document.body.setAttribute('data-theme', currentTheme);
            const themeSelect = document.getElementById('settings-theme');
            if (themeSelect) themeSelect.value = currentTheme;
        }

        function changeTheme(theme) {
            currentTheme = theme;
            applyTheme();
        }

        function changeLanguage(lang) {
            currentLang = lang;
            updateTranslations();
            // Перерисовываем текущий вид, чтобы применить переводы
            renderView(currentView);
        }

        function updateTranslations() {
            document.getElementById('t-login-title').innerText = t('login_title');
            document.getElementById('t-username-label').innerText = t('username');
            document.getElementById('t-password-label').innerText = t('password');
            document.getElementById('t-login-btn').innerText = t('login_btn');
            document.getElementById('t-no-account').innerText = t('no_account');
            document.getElementById('t-register-link').innerText = t('register_link');
            
            document.getElementById('t-register-title').innerText = t('register_title');
            document.getElementById('t-display-name-label').innerText = t('display_name');
            document.getElementById('t-username-label-reg').innerText = t('username');
            document.getElementById('t-password-label-reg').innerText = t('password');
            document.getElementById('t-confirm-password-label').innerText = t('confirm_password');
            document.getElementById('t-register-btn').innerText = t('register_btn');
            document.getElementById('t-have-account').innerText = t('have_account');
            document.getElementById('t-login-link').innerText = t('login_link');

            document.getElementById('t-publish-btn').innerText = t('publish_btn');
            document.getElementById('t-back').innerText = t('back');
            document.getElementById('t-comments').innerText = t('comments');
            document.getElementById('t-send-comment').innerText = t('send_comment');
            document.getElementById('t-settings-title').innerText = t('settings_title');
            document.getElementById('t-language').innerText = t('language');
            document.getElementById('t-theme').innerText = t('theme');
            
            document.getElementById('t-nav-home').innerText = t('nav_home');
            document.getElementById('t-nav-profile').innerText = t('nav_profile');
            document.getElementById('t-nav-settings').innerText = t('nav_settings');
            document.getElementById('t-trending').innerText = t('trending');
            document.getElementById('t-guest-highlight').innerText = t('guest_highlight');
            document.getElementById('t-guest-desc').innerText = t('guest_desc');

            document.getElementById('settings-language').value = currentLang;
            document.getElementById('settings-theme').value = currentTheme;
        }

        // --- Инициализация ---
        async function init() {
            applyTheme();
            const res = await fetch('/api/me');
            const data = await res.json();
            if (data.username) {
                currentUser = data;
                renderView('home');
            } else {
                currentUser = null;
                renderView('auth');
            }
            updateTranslations();
        }

        // --- Управление интерфейсом ---
        function renderView(view) {
            currentView = view;
            isEditingProfile = false; // Сбрасываем режим редактирования при смене вкладки
            
            document.getElementById('view-auth').classList.add('hidden');
            document.getElementById('view-home').classList.add('hidden');
            document.getElementById('view-post').classList.add('hidden');
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
                    document.getElementById('header-title').innerText = t('header_home');
                    document.getElementById('view-home').classList.remove('hidden');
                    loadPosts();
                } else if (view === 'profile') {
                    document.getElementById('header-title').innerText = t('header_profile');
                    document.getElementById('view-profile').classList.remove('hidden');
                    loadProfile();
                } else if (view === 'settings') {
                    document.getElementById('header-title').innerText = t('header_settings');
                    document.getElementById('view-settings').classList.remove('hidden');
                } else if (view === 'post') {
                    document.getElementById('header-title').innerText = t('comments');
                    document.getElementById('view-post').classList.remove('hidden');
                    loadPostDetail();
                }
            } else {
                header.classList.add('hidden');
                guestInfo.classList.remove('hidden');
                rightNav.classList.add('hidden');
                document.getElementById('view-auth').classList.remove('hidden');
            }
            updateTranslations();
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
                timeline.innerHTML = `<p style="text-align:center; color:var(--text-muted); padding:32px 0;">${t('no_posts')}</p>`;
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
            const date = new Date(post.timestamp).toLocaleString(currentLang === 'ru' ? 'ru-RU' : 'en-US');
            const postEl = document.createElement('div');
            postEl.className = 'post-card';
            
            const upClass = post.user_vote === 'up' ? 'active' : '';
            const downClass = post.user_vote === 'down' ? 'active' : '';

            postEl.innerHTML = `
                <div class="vote-controls">
                    <button class="vote-btn upvote ${upClass}" onclick="handleVote('${post.id}', 'post', 'up')">
                        <svg viewBox="0 0 24 24"><path d="M12 4l-8 8h6v8h4v-8h6z"/></svg>
                    </button>
                    <span class="vote-count ${post.upvotes > post.downvotes ? 'up' : (post.downvotes > post.upvotes ? 'down' : '')}">
                        ${post.upvotes - post.downvotes}
                    </span>
                    <button class="vote-btn downvote ${downClass}" onclick="handleVote('${post.id}', 'post', 'down')">
                        <svg viewBox="0 0 24 24"><path d="M12 20l8-8h-6V4h-4v8H4z"/></svg>
                    </button>
                </div>
                <div class="post-main">
                    <div class="post-header">
                        <span class="post-author-name">${escapeHtml(post.display_name)}</span>
                        <span class="post-author-handle">@${escapeHtml(post.author)}</span>
                        <span class="post-date">${date}</span>
                    </div>
                    <div class="post-content" onclick="openPost('${post.id}')">${escapeHtml(post.content)}</div>
                    <div class="post-footer">
                        <a onclick="openPost('${post.id}')">
                            <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg>
                            ${t('comments')}
                        </a>
                    </div>
                </div>
            `;
            return postEl;
        }

        function openPost(postId) {
            currentPostId = postId;
            renderView('post');
        }

        // --- Детали поста и комментарии ---
        async function loadPostDetail() {
            const postRes = await fetch(`/api/posts/${currentPostId}`);
            const post = await postRes.json();
            
            const container = document.getElementById('post-detail-content');
            const date = new Date(post.timestamp).toLocaleString(currentLang === 'ru' ? 'ru-RU' : 'en-US');
            
            container.innerHTML = `
                <div class="post-card" style="margin-bottom: 0; border-bottom-left-radius: 0; border-bottom-right-radius: 0; border-bottom: none;">
                    <div class="vote-controls">
                        <button class="vote-btn upvote ${post.user_vote === 'up' ? 'active' : ''}" onclick="handleVote('${post.id}', 'post', 'up')">
                            <svg viewBox="0 0 24 24"><path d="M12 4l-8 8h6v8h4v-8h6z"/></svg>
                        </button>
                        <span class="vote-count ${post.upvotes > post.downvotes ? 'up' : (post.downvotes > post.upvotes ? 'down' : '')}">
                            ${post.upvotes - post.downvotes}
                        </span>
                        <button class="vote-btn downvote ${post.user_vote === 'down' ? 'active' : ''}" onclick="handleVote('${post.id}', 'post', 'down')">
                            <svg viewBox="0 0 24 24"><path d="M12 20l8-8h-6V4h-4v8H4z"/></svg>
                        </button>
                    </div>
                    <div class="post-main">
                        <div class="post-header">
                            <span class="post-author-name">${escapeHtml(post.display_name)}</span>
                            <span class="post-author-handle">@${escapeHtml(post.author)}</span>
                            <span class="post-date">${date}</span>
                        </div>
                        <div class="post-content">${escapeHtml(post.content)}</div>
                    </div>
                </div>
            `;
            
            loadComments();
        }

        async function loadComments() {
            const res = await fetch(`/api/posts/${currentPostId}/comments`);
            const comments = await res.json();
            const list = document.getElementById('comments-list');
            list.innerHTML = '';

            if (comments.length === 0) {
                list.innerHTML = `<p style="text-align:center; color:var(--text-muted); padding:16px 0;">${t('no_comments')}</p>`;
                return;
            }

            comments.forEach(comment => {
                const date = new Date(comment.timestamp).toLocaleString(currentLang === 'ru' ? 'ru-RU' : 'en-US');
                const el = document.createElement('div');
                el.className = 'comment-card';
                el.innerHTML = `
                    <div class="vote-controls">
                        <button class="vote-btn upvote ${comment.user_vote === 'up' ? 'active' : ''}" onclick="handleVote('${comment.id}', 'comment', 'up')">
                            <svg viewBox="0 0 24 24"><path d="M12 4l-8 8h6v8h4v-8h6z"/></svg>
                        </button>
                        <span class="vote-count ${comment.upvotes > comment.downvotes ? 'up' : (comment.downvotes > comment.upvotes ? 'down' : '')}">
                            ${comment.upvotes - comment.downvotes}
                        </span>
                        <button class="vote-btn downvote ${comment.user_vote === 'down' ? 'active' : ''}" onclick="handleVote('${comment.id}', 'comment', 'down')">
                            <svg viewBox="0 0 24 24"><path d="M12 20l8-8h-6V4h-4v8H4z"/></svg>
                        </button>
                    </div>
                    <div class="comment-main">
                        <div class="comment-header">
                            <span class="comment-author">${escapeHtml(comment.display_name)}</span>
                            <span class="post-author-handle">@${escapeHtml(comment.author)}</span>
                            <span class="comment-date">${date}</span>
                        </div>
                        <div class="comment-content">${escapeHtml(comment.content)}</div>
                    </div>
                `;
                list.appendChild(el);
            });
        }

        async function submitComment() {
            const content = document.getElementById('comment-content').value;
            if (!content.trim()) return;

            const res = await fetch(`/api/posts/${currentPostId}/comments`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content })
            });

            if (res.ok) {
                document.getElementById('comment-content').value = '';
                loadComments();
            }
        }

        async function handleVote(targetId, targetType, voteType) {
            if (!currentUser) return;
            
            const res = await fetch('/api/vote', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target_id: targetId, target_type: targetType, vote_type: voteType })
            });

            if (res.ok) {
                if (targetType === 'post') {
                    if (currentView === 'post') loadPostDetail();
                    else loadPosts();
                } else if (targetType === 'comment') {
                    loadComments();
                }
            }
        }

        // --- Профиль ---
        async function loadProfile() {
            const infoDiv = document.getElementById('profile-info');
            
            if (isEditingProfile) {
                infoDiv.innerHTML = `
                    <div class="form-group">
                        <label>${t('edit_name')}</label>
                        <input type="text" id="edit-display-name" value="${escapeHtml(currentUser.display_name)}">
                    </div>
                    <div class="form-group">
                        <label>${t('edit_bio')}</label>
                        <textarea id="edit-bio" rows="4">${escapeHtml(currentUser.bio || '')}</textarea>
                    </div>
                    <div style="display:flex; gap:8px;">
                        <button class="btn btn-primary" onclick="saveProfile()">${t('save_profile')}</button>
                        <button class="btn btn-secondary" onclick="toggleProfileEdit()">${t('back')}</button>
                    </div>
                `;
            } else {
                infoDiv.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                        <div>
                            <div class="profile-name">${escapeHtml(currentUser.display_name)}</div>
                            <div class="profile-handle">@${escapeHtml(currentUser.username)}</div>
                        </div>
                        <button class="btn btn-secondary" style="width:auto;" onclick="toggleProfileEdit()">${t('edit_profile')}</button>
                    </div>
                    <div class="profile-bio">${escapeHtml(currentUser.bio || 'Нет описания')}</div>
                    <div class="profile-stats">
                        <span><strong>0</strong> подписчиков</span>
                        <span><strong>0</strong> подписок</span>
                    </div>
                `;
            }

            const res = await fetch(`/api/posts`);
            const allPosts = await res.json();
            const userPosts = allPosts.filter(p => p.author === currentUser.username);
            
            const timeline = document.getElementById('profile-timeline');
            timeline.innerHTML = '';
            
            if (userPosts.length === 0) {
                timeline.innerHTML = `<p style="text-align:center; color:var(--text-muted); padding:32px 0;">${t('no_posts')}</p>`;
                return;
            }
            userPosts.forEach(post => timeline.appendChild(createPostElement(post)));
        }

        function toggleProfileEdit() {
            isEditingProfile = !isEditingProfile;
            loadProfile();
        }

        async function saveProfile() {
            const display_name = document.getElementById('edit-display-name').value;
            const bio = document.getElementById('edit-bio').value;

            const res = await fetch('/api/profile/update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ display_name, bio })
            });
            
            if (res.ok) {
                const data = await res.json();
                currentUser.display_name = data.display_name;
                currentUser.bio = data.bio;
                isEditingProfile = false;
                loadProfile();
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
