# main.py
# litodon — лёгкая соцсеть в стиле 2015 года.
# Запуск:  pip install fastapi uvicorn python-multipart
#          python main.py
#        или:  uvicorn main:app --reload

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from html import escape
import hashlib
import secrets
import time
from datetime import datetime
from typing import Optional


app = FastAPI(title="litodon")


# ============================================================
#   ХРАНИЛИЩЕ (ВСЁ В ОПЕРАТИВКЕ, ПРИ ПЕРЕЗАПУСКЕ ОБНУЛЯЕТСЯ)
# ============================================================

users: dict = {}       # username -> {"salt": str, "pw": str}
sessions: dict = {}    # token    -> username
posts: list = []       # список постов
_counter = {"post": 0, "comment": 0}


def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def get_user(request: Request) -> Optional[str]:
    token = request.cookies.get("session")
    if token and token in sessions:
        return sessions[token]
    return None


def fmt_time(ts: float) -> str:
    diff = int(time.time() - ts)
    if diff < 60:
        return f"{diff} сек. назад"
    if diff < 3600:
        return f"{diff // 60} мин. назад"
    if diff < 86400:
        return f"{diff // 3600} ч. назад"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


# ============================================================
#   ОФОРМЛЕНИЕ (ЗЕЛЁНЫЙ СТИЛЬ 2015)
# ============================================================

CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: #dfeedf;
  font-family: Verdana, Geneva, Tahoma, sans-serif;
  font-size: 13px;
  color: #17381a;
  line-height: 1.45;
}
a { color: #2e7d32; }
a:hover { color: #1b5e20; }

.header {
  background: linear-gradient(#5cb860, #2e7d32);
  border-bottom: 3px solid #1b5e20;
  box-shadow: 0 2px 5px rgba(0,0,0,0.25);
}
.header-inner {
  width: 900px; margin: 0 auto; padding: 10px 12px;
  display: flex; align-items: center; justify-content: space-between;
}
.logo {
  font-size: 30px; font-weight: bold; color: #fff; text-decoration: none;
  letter-spacing: -1.5px;
  text-shadow: 1px 1px 0 #1b5e20, 2px 2px 4px rgba(0,0,0,0.35);
}
.logo span {
  color: #c8e6c9; font-size: 11px; letter-spacing: 0;
  margin-left: 8px; font-weight: normal; text-shadow: none;
  vertical-align: middle;
}
.auth { color: #e8f5e9; font-size: 12px; }
.auth a { color: #fff; }
.auth .user { margin-right: 8px; font-weight: bold; }

.container { width: 900px; margin: 16px auto 40px; }

.box {
  background: #fff;
  border: 1px solid #a5cfa5;
  border-radius: 4px;
  margin-bottom: 12px;
  box-shadow: 0 1px 2px rgba(27,94,32,0.12);
  overflow: hidden;
}
.box-title {
  background: linear-gradient(#eaf6ea, #c8e6c9);
  border-bottom: 1px solid #a5cfa5;
  padding: 6px 10px;
  font-weight: bold;
  color: #1b5e20;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.box-body { padding: 10px; }

textarea, input[type=text], input[type=password] {
  width: 100%;
  border: 1px solid #a5cfa5;
  border-radius: 3px;
  padding: 6px 8px;
  font-family: inherit;
  font-size: 13px;
  background: #f7fdf7;
  color: #17381a;
  outline: none;
}
textarea:focus, input:focus { border-color: #4caf50; background: #fff; }
textarea { resize: vertical; min-height: 60px; }

button, .btn {
  background: linear-gradient(#66bb6a, #43a047);
  border: 1px solid #2e7d32;
  color: #fff;
  padding: 5px 14px;
  border-radius: 3px;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  font-weight: bold;
  text-shadow: 0 1px 0 rgba(0,0,0,0.2);
}
button:hover { background: linear-gradient(#7cc87f, #4caf50); }
button:active { background: #2e7d32; }

.link-btn {
  background: none; border: none; color: #fff; text-decoration: underline;
  cursor: pointer; font-size: 12px; padding: 0; font-weight: normal;
  text-shadow: none;
}
.link-btn:hover { color: #c8e6c9; background: none; }

.post { display: flex; }
.votes {
  width: 56px; flex-shrink: 0;
  background: #f3faf3;
  border-right: 1px solid #d6ead6;
  padding: 10px 6px;
  display: flex; flex-direction: column; align-items: center; gap: 4px;
}
.vote-btn {
  width: 32px; height: 22px; padding: 0;
  background: #eaf6ea; color: #2e7d32;
  border: 1px solid #a5cfa5;
  font-size: 11px; line-height: 1;
  text-shadow: none;
  border-radius: 3px;
}
.vote-btn:hover { background: #c8e6c9; }
.vote-btn.active-up { background: #43a047; color: #fff; border-color: #2e7d32; }
.vote-btn.active-down { background: #e53935; color: #fff; border-color: #c62828; }
.score { font-weight: bold; font-size: 14px; }
.score-pos { color: #2e7d32; }
.score-neg { color: #c62828; }
.score-zero { color: #7a8f7a; }

.post-main { flex: 1; padding: 10px 12px; min-width: 0; }
.post-head { margin-bottom: 4px; }
.author { font-weight: bold; color: #1b5e20; }
.time { color: #8aa38a; font-size: 11px; margin-left: 8px; }
.post-text { font-size: 14px; word-wrap: break-word; overflow-wrap: break-word; }
.post-foot { margin-top: 6px; font-size: 11px; color: #8aa38a; }

.comments { margin-top: 8px; border-top: 1px dashed #c8e6c9; padding-top: 6px; }
.comment { padding: 4px 0; border-bottom: 1px dotted #e0f0e0; }
.comment:last-child { border-bottom: none; }
.c-author { font-weight: bold; color: #2e7d32; font-size: 12px; }
.c-time { color: #9cb89c; font-size: 11px; margin-left: 6px; }
.c-text { font-size: 12px; }

.cform { margin-top: 8px; display: flex; gap: 6px; }
.cform input { flex: 1; }
.cform-locked { margin-top: 8px; font-size: 11px; color: #8aa38a; }

.notice {
  padding: 10px 12px; background: #fffbe6; border-color: #e6d98a;
  color: #6b5b1b; font-size: 12px;
}
.empty {
  padding: 20px; text-align: center; color: #8aa38a; font-size: 12px;
}

.form-row { margin-bottom: 10px; }
.form-row label {
  display: block; font-size: 11px; color: #4b6b4b;
  margin-bottom: 3px; font-weight: bold;
}
.error {
  background: #fdecea; border: 1px solid #f0b0aa; color: #a02b22;
  padding: 8px 10px; border-radius: 3px; margin-bottom: 10px; font-size: 12px;
}

.footer { text-align: center; color: #7d9c7d; font-size: 11px; padding: 20px 0 30px; }
"""


def render_page(user: Optional[str], content: str) -> str:
    if user:
        auth_block = (
            f'<span class="user">&#128100; {escape(user)}</span>'
            f'<form method="post" action="/logout" style="display:inline">'
            f'<button class="link-btn" type="submit">Выход</button></form>'
        )
    else:
        auth_block = '<a href="/login">Вход</a> &nbsp;|&nbsp; <a href="/register">Регистрация</a>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=900">
<title>litodon</title>
<style>{CSS}</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <a href="/" class="logo">litodon<span>лёгкая соцсеть</span></a>
    <div class="auth">{auth_block}</div>
  </div>
</div>
<div class="container">
{content}
</div>
<div class="footer">litodon &copy; 2015 &middot; всё хранится в оперативной памяти</div>
</body>
</html>"""


# ============================================================
#   РЕНДЕР ПОСТОВ И ЛЕНТЫ
# ============================================================

def render_post(p: dict, user: Optional[str]) -> str:
    pid = p["id"]
    score = len(p["up"]) - len(p["down"])
    score_cls = "score-pos" if score > 0 else ("score-neg" if score < 0 else "score-zero")

    if user:
        up_cls = "vote-btn active-up" if user in p["up"] else "vote-btn"
        down_cls = "vote-btn active-down" if user in p["down"] else "vote-btn"
        votes = f"""
          <form method="post" action="/vote/{pid}">
            <input type="hidden" name="value" value="up">
            <button class="{up_cls}" title="Плюс">&#9650;</button>
          </form>
          <div class="score {score_cls}">{score}</div>
          <form method="post" action="/vote/{pid}">
            <input type="hidden" name="value" value="down">
            <button class="{down_cls}" title="Минус">&#9660;</button>
          </form>
        """
    else:
        votes = f'<div class="score {score_cls}">{score}</div>'

    # комментарии
    comments_html = ""
    if p["comments"]:
        items = "".join(
            f'<div class="comment">'
            f'<span class="c-author">{escape(c["author"])}</span>'
            f'<span class="c-time">{fmt_time(c["created"])}</span>'
            f'<div class="c-text">{escape(c["text"])}</div>'
            f'</div>'
            for c in p["comments"]
        )
        comments_html = f'<div class="comments">{items}</div>'

    if user:
        cform = (
            f'<form method="post" action="/comment/{pid}" class="cform">'
            f'<input type="text" name="text" maxlength="300" placeholder="Ваш комментарий..." required>'
            f'<button type="submit">Отправить</button></form>'
        )
    else:
        cform = '<div class="cform-locked"><a href="/login">Войдите</a>, чтобы оставить комментарий.</div>'

    body = escape(p["text"]).replace("\n", "<br>")

    return f"""
    <div class="box post">
      <div class="votes">{votes}</div>
      <div class="post-main">
        <div class="post-head">
          <span class="author">{escape(p["author"])}</span>
          <span class="time">{fmt_time(p["created"])}</span>
        </div>
        <div class="post-text">{body}</div>
        <div class="post-foot">комментариев: {len(p["comments"])}</div>
        {comments_html}
        {cform}
      </div>
    </div>"""


def render_feed(user: Optional[str]) -> str:
    if user:
        form = """
        <div class="box">
          <div class="box-title">Новый пост</div>
          <div class="box-body">
            <form method="post" action="/post">
              <textarea name="text" maxlength="500" placeholder="Что нового?" required></textarea>
              <div style="margin-top:8px">
                <button type="submit">Опубликовать</button>
              </div>
            </form>
          </div>
        </div>"""
    else:
        form = ('<div class="box notice">'
                'Чтобы писать посты, голосовать и комментировать — '
                '<a href="/login">войдите</a> или <a href="/register">зарегистрируйтесь</a>. '
                'Ленту можно читать без регистрации.'
                '</div>')

    if not posts:
        feed = '<div class="box empty">Пока постов нет. Будьте первым!</div>'
    else:
        ordered = sorted(posts, key=lambda x: x["created"], reverse=True)
        feed = "".join(render_post(p, user) for p in ordered)

    return form + feed


def render_login(err: Optional[str] = None) -> str:
    err_html = f'<div class="error">{escape(err)}</div>' if err else ""
    return f"""
    <div class="box" style="width:400px;margin:0 auto;">
      <div class="box-title">Вход</div>
      <div class="box-body">
        {err_html}
        <form method="post" action="/login">
          <div class="form-row"><label>Имя пользователя</label>
            <input type="text" name="username" required autofocus></div>
          <div class="form-row"><label>Пароль</label>
            <input type="password" name="password" required></div>
          <button type="submit">Войти</button>
        </form>
        <p style="font-size:12px;margin-bottom:0">Нет аккаунта? <a href="/register">Зарегистрироваться</a></p>
      </div>
    </div>"""


def render_register(err: Optional[str] = None) -> str:
    err_html = f'<div class="error">{escape(err)}</div>' if err else ""
    return f"""
    <div class="box" style="width:400px;margin:0 auto;">
      <div class="box-title">Регистрация</div>
      <div class="box-body">
        {err_html}
        <form method="post" action="/register">
          <div class="form-row"><label>Имя пользователя (от 3 символов)</label>
            <input type="text" name="username" required autofocus maxlength="20"></div>
          <div class="form-row"><label>Пароль (от 4 символов)</label>
            <input type="password" name="password" required></div>
          <button type="submit">Создать аккаунт</button>
        </form>
        <p style="font-size:12px;margin-bottom:0">Уже есть аккаунт? <a href="/login">Войти</a></p>
      </div>
    </div>"""


# ============================================================
#   МАРШРУТЫ
# ============================================================

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = get_user(request)
    return render_page(user, render_feed(user))


# ---------- регистрация ----------

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    user = get_user(request)
    if user:
        return RedirectResponse("/", status_code=303)
    return render_page(None, render_register())


@app.post("/register")
def register_post(request: Request,
                  username: str = Form(...),
                  password: str = Form(...)):
    username = username.strip()
    err = None
    if len(username) < 3:
        err = "Имя пользователя — минимум 3 символа."
    elif len(username) > 20:
        err = "Имя пользователя — максимум 20 символов."
    elif " " in username:
        err = "Имя пользователя не должно содержать пробелов."
    elif len(password) < 4:
        err = "Пароль — минимум 4 символа."
    elif username in users:
        err = "Такое имя уже занято."

    if err:
        return render_page(None, render_register(err))

    salt = secrets.token_hex(8)
    users[username] = {"salt": salt, "pw": hash_pw(password, salt)}

    token = secrets.token_hex(24)
    sessions[token] = username
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True, max_age=60 * 60 * 24 * 30)
    return resp


# ---------- вход / выход ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user = get_user(request)
    if user:
        return RedirectResponse("/", status_code=303)
    return render_page(None, render_login())


@app.post("/login")
def login_post(request: Request,
               username: str = Form(...),
               password: str = Form(...)):
    username = username.strip()
    acc = users.get(username)
    if not acc or acc["pw"] != hash_pw(password, acc["salt"]):
        return render_page(None, render_login("Неверное имя пользователя или пароль."))

    token = secrets.token_hex(24)
    sessions[token] = username
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True, max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        sessions.pop(token, None)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session")
    return resp


# ---------- создание поста ----------

@app.post("/post")
def create_post(request: Request, text: str = Form(...)):
    user = get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    text = text.strip()
    if text:
        _counter["post"] += 1
        posts.append({
            "id": _counter["post"],
            "author": user,
            "text": text[:500],
            "created": time.time(),
            "up": set(),
            "down": set(),
            "comments": [],
        })
    return RedirectResponse("/", status_code=303)


# ---------- голосование ----------

@app.post("/vote/{post_id}")
def vote(post_id: int, request: Request, value: str = Form(...)):
    user = get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    post = next((p for p in posts if p["id"] == post_id), None)
    if post:
        if value == "up":
            if user in post["up"]:
                post["up"].discard(user)
            else:
                post["up"].add(user)
                post["down"].discard(user)
        elif value == "down":
            if user in post["down"]:
                post["down"].discard(user)
            else:
                post["down"].add(user)
                post["up"].discard(user)

    return RedirectResponse("/", status_code=303)


# ---------- комментарии ----------

@app.post("/comment/{post_id}")
def add_comment(post_id: int, request: Request, text: str = Form(...)):
    user = get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    post = next((p for p in posts if p["id"] == post_id), None)
    text = text.strip()
    if post and text:
        _counter["comment"] += 1
        post["comments"].append({
            "id": _counter["comment"],
            "author": user,
            "text": text[:300],
            "created": time.time(),
        })

    return RedirectResponse("/", status_code=303)


# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
