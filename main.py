import uvicorn
import uuid
from fastapi import FastAPI, Request, Form, Response, Cookie, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional

app = FastAPI(title="слд")

# --- Имитация базы данных (в памяти, без файлов) ---
users_db = {"admin": "admin"}
sessions = {}
posts_db = [
    {
        "id": "1",
        "author": "Flowseal",
        "date": "вчера в 19:21",
        "title": "скачать трейнер на Silent Hill не вирус",
        "content": "Шифрованный пейлоад, в нём будет из стиллера и RCE. Данные отправляются по пути flyfilecloud[.]click/NQsPERph/backend/api/app.php",
        "likes": 55,
        "views": 4219,
        "comments": 15,
        "image": "https://via.placeholder.com/600x300/2a2a2a/4ade80?text=Code+Payload"
    },
    {
        "id": "2",
        "author": "FappyGG",
        "date": "вчера в 19:51",
        "title": "Братишкин слив",
        "content": "Скачал вирус от ванчеза и слили его данные...",
        "likes": 39,
        "views": 2400,
        "comments": 7,
        "image": "https://via.placeholder.com/600x300/2a2a2a/a855f7?text=Video+Stream"
    }
]

# --- HTML Шаблоны и CSS (Встроенные, без внешних файлов) ---
base_css = """
<style>
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #121212; color: #e0e0e0; margin: 0; padding: 0; }
    a { color: #a855f7; text-decoration: none; }
    .navbar { display: flex; justify-content: space-between; align-items: center; padding: 15px 30px; background-color: #1e1e1e; border-bottom: 1px solid #333; }
    .nav-links a { margin-right: 20px; color: #ccc; font-weight: bold; }
    .nav-links a.active { color: #4ade80; }
    .container { max-width: 1200px; margin: 30px auto; padding: 0 20px; }
    .feed-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 20px; }
    .card { background-color: #1e1e1e; border-radius: 10px; overflow: hidden; border: 1px solid #2a2a2a; display: flex; flex-direction: column; }
    .card-img { width: 100%; height: 200px; object-fit: cover; background: #000; }
    .card-body { padding: 15px; flex-grow: 1; }
    .card-title { font-size: 1.1rem; font-weight: bold; margin: 0 0 10px 0; color: #fff; }
    .card-meta { font-size: 0.8rem; color: #888; display: flex; justify-content: space-between; margin-bottom: 10px; }
    .card-text { font-size: 0.9rem; color: #aaa; }
    .card-footer { padding: 10px 15px; background-color: #252525; display: flex; gap: 15px; font-size: 0.8rem; color: #888; }
    .btn { background-color: #a855f7; color: white; border: none; padding: 8px 16px; border-radius: 5px; cursor: pointer; font-weight: bold; text-decoration: none; display: inline-block; }
    .btn-green { background-color: #4ade80; color: #000; }
    .form-group { margin-bottom: 15px; }
    .form-group label { display: block; margin-bottom: 5px; color: #ccc; }
    .form-control { width: 100%; padding: 10px; background: #2a2a2a; border: 1px solid #444; color: white; border-radius: 5px; box-sizing: border-box; }
    .form-control:focus { outline: none; border-color: #a855f7; }
</style>
"""

def render_page(title: str, content: str, user: str = None):
    nav_auth = f'<span style="color:#4ade80; font-weight:bold;">@{user}</span> <a href="/logout" style="color:#ff5555; margin-left:10px;">Выйти</a>' if user else '<a href="/login" class="btn">Войти</a>'
    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>{title} - слд</title>
        {base_css}
    </head>
    <body>
        <nav class="navbar">
            <div style="display:flex; align-items:center; gap:20px;">
                <h2 style="margin:0; color:#4ade80; font-family:monospace;">слд</h2>
                <div class="nav-links">
                    <a href="/" class="active">Лента</a>
                    <a href="/upload">Новый пост</a>
                </div>
            </div>
            <div style="display:flex; gap:10px; align-items:center;">
                <input type="text" placeholder="Поиск..." class="form-control" style="width:200px;">
                {nav_auth}
            </div>
        </nav>
        <div class="container">
            {content}
        </div>
    </body>
    </html>
    """

# --- Маршруты ---

@app.get("/", response_class=HTMLResponse)
async def feed(session_id: Optional[str] = Cookie(None)):
    user = sessions.get(session_id)
    cards = ""
    for p in posts_db:
        cards += f"""
        <div class="card">
            <img src="{p['image']}" class="card-img" alt="post image">
            <div class="card-body">
                <div class="card-meta">
                    <span>👤 {p['author']}</span>
                    <span>{p['date']}</span>
                </div>
                <a href="/post/{p['id']}"><h3 class="card-title">{p['title']}</h3></a>
                <p class="card-text">{p['content'][:100]}...</p>
            </div>
            <div class="card-footer">
                <span>👁 {p['views']}</span>
                <span>❤️ {p['likes']}</span>
                <span>💬 {p['comments']}</span>
            </div>
        </div>
        """
    content = f'<h2>Лента</h2><div class="feed-grid">{cards}</div>'
    return render_page("Лента", content, user)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    content = """
    <div style="max-width: 400px; margin: 50px auto; background: #1e1e1e; padding: 30px; border-radius: 10px; border: 1px solid #333;">
        <h2 style="text-align: center; color: #fff;">Вход в слд</h2>
        <p style="text-align: center; color: #888; font-size: 0.9rem;">Авторизация через собственный сервер</p>
        <form method="POST" action="/login">
            <div class="form-group">
                <label>Логин</label>
                <input type="text" name="username" class="form-control" required>
            </div>
            <div class="form-group">
                <label>Пароль</label>
                <input type="password" name="password" class="form-control" required>
            </div>
            <button type="submit" class="btn" style="width: 100%;">Войти</button>
        </form>
        <p style="text-align:center; margin-top:15px; font-size:0.8rem; color:#888;">Тестовый доступ: admin / admin</p>
    </div>
    """
    return render_page("Вход", content)


@app.post("/login")
async def login_action(username: str = Form(...), password: str = Form(...)):
    if username in users_db and users_db[username] == password:
        session_id = str(uuid.uuid4())
        sessions[session_id] = username
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="session_id", value=session_id, httponly=True)
        return response
    raise HTTPException(status_code=400, detail="Неверный логин или пароль")


@app.get("/logout")
async def logout(response: Response, session_id: Optional[str] = Cookie(None)):
    if session_id in sessions:
        del sessions[session_id]
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session_id")
    return response


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(session_id: Optional[str] = Cookie(None)):
    user = sessions.get(session_id)
    if not user:
        return RedirectResponse(url="/login")
    
    content = """
    <div style="max-width: 800px; margin: 0 auto; background: #1e1e1e; padding: 30px; border-radius: 10px; border: 1px solid #333;">
        <h2 style="color: #fff;">Загрузка файлов</h2>
        <p style="color: #888;">Для загрузки файлов и публикации постов необходимо авторизоваться!</p>
        <form method="POST" action="/upload">
            <div class="form-group">
                <label>Название файла*</label>
                <input type="text" name="title" class="form-control" placeholder="скачать фото где девушка..." required>
            </div>
            <div class="form-group">
                <label>Описание файла</label>
                <textarea name="content" class="form-control" rows="4" placeholder="где девушка лежит огромная жирная под самолетом вентилятором..."></textarea>
            </div>
            <div class="form-group">
                <label>Ссылка на изображение (эмуляция загрузки файла)</label>
                <input type="url" name="image_url" class="form-control" placeholder="https://example.com/image.jpg" value="https://via.placeholder.com/600x300">
            </div>
            <div class="form-group" style="display: flex; gap: 10px;">
                <button type="button" class="btn btn-green" style="flex: 1; opacity: 0.5;">Опубликовать в ленту</button>
                <button type="button" class="btn" style="flex: 1; opacity: 0.5;">Доступ по ссылке</button>
            </div>
            <button type="submit" class="btn btn-green" style="width: 100%; margin-top: 10px;">Добавить файл</button>
        </form>
    </div>
    """
    return render_page("Загрузка", content, user)


@app.post("/upload")
async def upload_action(
    session_id: Optional[str] = Cookie(None),
    title: str = Form(...),
    content: str = Form(""),
    image_url: str = Form(...)
):
    user = sessions.get(session_id)
    if not user:
        return RedirectResponse(url="/login")
    
    new_post = {
        "id": str(len(posts_db) + 1),
        "author": user,
        "date": "только что",
        "title": title,
        "content": content,
        "likes": 0,
        "views": 0,
        "comments": 0,
        "image": image_url
    }
    posts_db.insert(0, new_post)
    return RedirectResponse(url="/", status_code=303)


@app.get("/post/{post_id}", response_class=HTMLResponse)
async def view_post(post_id: str, session_id: Optional[str] = Cookie(None)):
    user = sessions.get(session_id)
    post = next((p for p in posts_db if p["id"] == post_id), None)
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")
    
    post["views"] += 1
    
    content = f"""
    <div style="max-width: 800px; margin: 0 auto;">
        <a href="/" style="color: #888; font-size: 0.9rem;">← Назад в ленту</a>
        <div class="card" style="margin-top: 20px;">
            <img src="{post['image']}" class="card-img" style="height: auto; max-height: 500px; object-fit: contain; background: #000;">
            <div class="card-body">
                <div class="card-meta">
                    <span>👤 {post['author']}</span>
                    <span>{post['date']}</span>
                </div>
                <h2 style="color: #fff; margin-top: 0;">{post['title']}</h2>
                <p style="color: #ccc; line-height: 1.6; font-size: 1.05rem;">{post['content']}</p>
            </div>
            <div class="card-footer" style="font-size: 1rem; padding: 15px;">
                <span>👁 {post['views']}</span>
                <span>❤️ {post['likes']}</span>
                <span>💬 {post['comments']}</span>
            </div>
        </div>
        
        <div style="margin-top: 30px;">
            <h3 style="color: #fff;">Комментарии</h3>
            <p style="color: #888; font-size: 0.9rem;">Войдите, чтобы оставить комментарий.</p>
        </div>
    </div>
    """
    return render_page(post['title'], content, user)


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
