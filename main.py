import uvicorn
from fastapi import FastAPI, Response, HTTPException, Cookie
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import uuid
from datetime import datetime

# --- IN-MEMORY STORAGE ---
class InMemoryStorage:
    def __init__(self):
        self.users = {}
        self.posts = []
        self.sessions = {}
        self.comments = []
        self.votes = {}

db = InMemoryStorage()
app = FastAPI(title="Litodon")

# --- Pydantic ---
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
    target_type: str
    vote_type: str

# --- API ---
@app.post("/api/register")
async def register(data: UserRegister, response: Response):
    if data.username in db.users:
        raise HTTPException(status_code=400, detail="ERR_USERNAME_TAKEN")
    if not data.username or not data.password or not data.display_name:
        raise HTTPException(status_code=400, detail="ERR_FILL_ALL")
    if data.password != data.confirm_password:
        raise HTTPException(status_code=400, detail="ERR_PASSWORDS_MISMATCH")
    if len(data.password) < 4:
        raise HTTPException(status_code=400, detail="ERR_PASSWORD_SHORT")

    db.users[data.username] = {
        "password": data.password,
        "display_name": data.display_name,
        "bio": ""
    }
    token = str(uuid.uuid4())
    db.sessions[token] = data.username
    response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax")
    return {"message": "OK", "username": data.username}

@app.post("/api/login")
async def login(data: UserLogin, response: Response):
    user = db.users.get(data.username)
    if not user or user["password"] != data.password:
        raise HTTPException(status_code=400, detail="ERR_INVALID_CREDENTIALS")
    token = str(uuid.uuid4())
    db.sessions[token] = data.username
    response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax")
    return {"message": "OK", "username": data.username}

@app.post("/api/logout")
async def logout(response: Response, session_token: Optional[str] = Cookie(None)):
    if session_token in db.sessions:
        del db.sessions[session_token]
    response.delete_cookie("session_token")
    return {"message": "OK"}

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
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
    username = db.sessions[session_token]
    db.users[username]["display_name"] = data.display_name
    db.users[username]["bio"] = data.bio
    return {"message": "OK", "display_name": data.display_name, "bio": data.bio}

def enrich_post(post, current_user):
    votes = db.votes.get(post["id"], {})
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
    return [enrich_post(p, current_user) for p in sorted(db.posts, key=lambda x: x["timestamp"], reverse=True)]

@app.get("/api/posts/{post_id}")
async def get_post(post_id: str, session_token: Optional[str] = Cookie(None)):
    current_user = db.sessions.get(session_token) if session_token else None
    for post in db.posts:
        if post["id"] == post_id:
            return enrich_post(post, current_user)
    raise HTTPException(status_code=404, detail="ERR_POST_NOT_FOUND")

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
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
    if not post.content.strip():
        raise HTTPException(status_code=400, detail="ERR_EMPTY_POST")
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
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
    if not data.content.strip():
        raise HTTPException(status_code=400, detail="ERR_EMPTY_COMMENT")
    if not any(p["id"] == post_id for p in db.posts):
        raise HTTPException(status_code=404, detail="ERR_POST_NOT_FOUND")
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
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
    username = db.sessions[session_token]
    if data.target_id not in db.votes:
        db.votes[data.target_id] = {}
    if username in db.votes[data.target_id]:
        if db.votes[data.target_id][username] == data.vote_type:
            del db.votes[data.target_id][username]
            return {"message": "OK"}
    db.votes[data.target_id][username] = data.vote_type
    return {"message": "OK"}

@app.get("/api/search")
async def search(q: str = "", session_token: Optional[str] = Cookie(None)):
    current_user = db.sessions.get(session_token) if session_token else None
    q_lower = q.lower().strip()
    if not q_lower:
        return {"query": q, "users": [], "posts": [], "comments": []}

    users_result = []
    for uname, udata in db.users.items():
        if (q_lower in uname.lower() or
            q_lower in udata.get("display_name", "").lower() or
            q_lower in udata.get("bio", "").lower()):
            users_result.append({
                "username": uname,
                "display_name": udata.get("display_name", uname),
                "bio": udata.get("bio", "")
            })

    posts_result = []
    for post in db.posts:
        if q_lower in post["content"].lower():
            posts_result.append(enrich_post(post, current_user))

    comments_result = []
    for comment in db.comments:
        if q_lower in comment["content"].lower():
            votes = db.votes.get(comment["id"], {})
            upvotes = sum(1 for v in votes.values() if v == "up")
            downvotes = sum(1 for v in votes.values() if v == "down")
            user_vote = votes.get(current_user) if current_user else None
            author_data = db.users.get(comment["author"], {})
            # Get post author for context
            post_author = ""
            for p in db.posts:
                if p["id"] == comment["post_id"]:
                    post_author = p["author"]
                    break
            comments_result.append({
                **comment,
                "display_name": author_data.get("display_name", comment["author"]),
                "post_author": post_author,
                "upvotes": upvotes,
                "downvotes": downvotes,
                "user_vote": user_vote
            })

    return {
        "query": q,
        "users": users_result,
        "posts": posts_result,
        "comments": comments_result
    }

# --- FAVICON SVG ---
FAVICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#6364ff"/><path d="M11 8v16h10v-3h-7V8z" fill="#fff"/></svg>'''

@app.get("/favicon.svg")
async def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


# --- MAIN APP HTML ---
MAIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Litodon</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
:root, [data-theme="dark"] {
    --bg: #191b22;
    --bg-secondary: #282c37;
    --bg-hover: #313543;
    --bg-input: #191b22;
    --border: #393f4f;
    --border-light: #2c313d;
    --text: #d9e1e8;
    --text-heading: #ffffff;
    --text-muted: #7d869a;
    --accent: #6364ff;
    --accent-hover: #5051db;
    --accent-text: #ffffff;
    --danger: #df405a;
    --upvote: #ff6b35;
    --downvote: #7193ff;
    --shadow: 0 4px 16px rgba(0,0,0,0.35);
    --shadow-sm: 0 2px 6px rgba(0,0,0,0.2);
}

[data-theme="light"] {
    --bg: #f4f6f9;
    --bg-secondary: #ffffff;
    --bg-hover: #f0f2f5;
    --bg-input: #ffffff;
    --border: #d8dee6;
    --border-light: #e8edf2;
    --text: #2e3440;
    --text-heading: #191b22;
    --text-muted: #6b7381;
    --accent: #6364ff;
    --accent-hover: #5051db;
    --accent-text: #ffffff;
    --danger: #d93025;
    --upvote: #e8501a;
    --downvote: #4267c9;
    --shadow: 0 4px 16px rgba(0,0,0,0.08);
    --shadow-sm: 0 2px 6px rgba(0,0,0,0.05);
}

* { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    height: 100vh;
    overflow: hidden;
    font-size: 15px;
    line-height: 1.5;
    transition: background 0.25s ease, color 0.25s ease;
    -webkit-user-select: none;
    -moz-user-select: none;
    user-select: none;
}

input, textarea, select {
    -webkit-user-select: text;
    -moz-user-select: text;
    user-select: text;
}

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

.app {
    display: grid;
    grid-template-columns: 260px 1fr 260px;
    max-width: 1240px;
    margin: 0 auto;
    height: 100vh;
    border-left: 1px solid var(--border);
    border-right: 1px solid var(--border);
}

.col {
    height: 100%;
    overflow-y: auto;
    padding: 20px 16px;
}
.col.left { border-right: 1px solid var(--border); display: flex; flex-direction: column; }
.col.center { padding: 0; }
.col.right { border-left: 1px solid var(--border); }

/* --- Brand --- */
.brand {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 24px;
    padding: 4px;
}
.brand-icon {
    width: 32px; height: 32px;
    background: var(--accent);
    border-radius: 7px;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    box-shadow: var(--shadow-sm);
}
.brand-icon svg { width: 20px; height: 20px; }
.brand-name {
    font-size: 20px;
    font-weight: 700;
    color: var(--text-heading);
    letter-spacing: -0.3px;
}

/* --- Search --- */
.search-wrap {
    position: relative;
    margin-bottom: 20px;
}
.search-wrap svg {
    position: absolute;
    left: 12px; top: 50%; transform: translateY(-50%);
    width: 16px; height: 16px;
    fill: var(--text-muted);
    pointer-events: none;
}
.search-input {
    width: 100%;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px 10px 36px;
    color: var(--text);
    font-size: 14px;
    outline: none;
    transition: border 0.2s, background 0.2s;
    font-family: inherit;
}
.search-input:focus {
    border-color: var(--accent);
    background: var(--bg-input);
}
.search-input::placeholder { color: var(--text-muted); }

/* --- Left footer --- */
.left-footer {
    margin-top: auto;
    display: flex;
    flex-direction: column;
    gap: 4px;
}
.footer-btn {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    background: transparent;
    border: none;
    border-radius: 8px;
    color: var(--text-muted);
    font-size: 13px;
    font-family: inherit;
    cursor: pointer;
    text-decoration: none;
    transition: background 0.15s, color 0.15s;
    text-align: left;
}
.footer-btn:hover { background: var(--bg-secondary); color: var(--text); }
.footer-btn svg { width: 16px; height: 16px; fill: currentColor; flex-shrink: 0; }

/* --- Right nav --- */
.right-brand {
    display: flex; align-items: center; gap: 10px;
    padding: 4px 8px; margin-bottom: 20px;
}
.nav-list { display: flex; flex-direction: column; gap: 2px; margin-bottom: 24px; }
.nav-btn {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 11px 14px;
    background: transparent;
    border: none;
    border-radius: 8px;
    color: var(--text);
    font-size: 14px;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    text-align: left;
    transition: background 0.15s;
    width: 100%;
}
.nav-btn:hover { background: var(--bg-secondary); }
.nav-btn.active { background: var(--bg-secondary); color: var(--accent); }
.nav-btn svg { width: 20px; height: 20px; fill: currentColor; flex-shrink: 0; }

.user-badge {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px;
    background: var(--bg-secondary);
    border-radius: 8px;
    border: 1px solid var(--border);
    margin-top: auto;
}
.user-badge-info { flex: 1; min-width: 0; }
.user-badge-name {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-heading);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.user-badge-handle {
    font-size: 12px;
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.logout-btn {
    background: transparent;
    border: none;
    cursor: pointer;
    padding: 6px;
    border-radius: 6px;
    color: var(--text-muted);
    display: flex;
    align-items: center;
    transition: color 0.15s, background 0.15s;
}
.logout-btn:hover { color: var(--danger); background: rgba(223,64,90,0.1); }
.logout-btn svg { width: 18px; height: 18px; fill: currentColor; }

/* --- Center header --- */
.center-header {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--bg);
    position: sticky;
    top: 0;
    z-index: 10;
    backdrop-filter: blur(8px);
}
.center-title {
    font-size: 17px;
    font-weight: 700;
    color: var(--text-heading);
}

/* --- Buttons --- */
.btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 10px 18px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    border: none;
    transition: background 0.15s, transform 0.05s;
    text-decoration: none;
}
.btn:active { transform: scale(0.98); }
.btn-primary { background: var(--accent); color: var(--accent-text); }
.btn-primary:hover { background: var(--accent-hover); }
.btn-secondary {
    background: var(--bg-secondary);
    color: var(--text);
    border: 1px solid var(--border);
}
.btn-secondary:hover { background: var(--bg-hover); }
.btn-text {
    background: transparent;
    color: var(--text-muted);
    padding: 6px 10px;
    font-size: 13px;
}
.btn-text:hover { color: var(--text); background: var(--bg-secondary); }
.btn-full { width: 100%; }
.btn-sm { padding: 6px 12px; font-size: 13px; }

/* --- Inputs --- */
.form-group { margin-bottom: 16px; }
.form-label {
    display: block;
    font-size: 13px;
    font-weight: 500;
    color: var(--text-muted);
    margin-bottom: 6px;
}
.form-input, .form-textarea, .form-select {
    width: 100%;
    background: var(--bg-input);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    color: var(--text);
    font-size: 14px;
    font-family: inherit;
    outline: none;
    transition: border 0.15s, box-shadow 0.15s;
    resize: none;
}
.form-input:focus, .form-textarea:focus, .form-select:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(99,100,255,0.15);
}
.form-input::placeholder, .form-textarea::placeholder { color: var(--text-muted); }
.form-textarea { resize: none; min-height: 70px; line-height: 1.5; }

.error-box {
    background: rgba(223,64,90,0.1);
    border: 1px solid var(--danger);
    color: var(--danger);
    padding: 10px 12px;
    border-radius: 8px;
    font-size: 13px;
    margin-bottom: 16px;
}

/* --- Auth screen --- */
.auth-wrap {
    max-width: 380px;
    margin: 40px auto;
    padding: 0 16px;
}
.auth-title {
    font-size: 24px;
    font-weight: 700;
    color: var(--text-heading);
    margin-bottom: 24px;
    text-align: center;
}
.auth-switch {
    text-align: center;
    margin-top: 20px;
    font-size: 14px;
    color: var(--text-muted);
}
.auth-switch a {
    color: var(--accent);
    cursor: pointer;
    text-decoration: none;
    font-weight: 500;
}
.auth-switch a:hover { text-decoration: underline; }

/* --- Compose --- */
.compose {
    padding: 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    gap: 14px;
    background: var(--bg-secondary);
}
.compose-body { flex: 1; display: flex; flex-direction: column; }
.compose-textarea {
    width: 100%;
    background: transparent;
    border: none;
    color: var(--text);
    font-family: inherit;
    font-size: 15px;
    resize: none;
    outline: none;
    min-height: 60px;
    padding: 4px 0;
}
.compose-textarea::placeholder { color: var(--text-muted); }
.compose-actions {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-top: 1px solid var(--border-light);
    padding-top: 12px;
    margin-top: 8px;
}
.compose-icons { display: flex; gap: 14px; color: var(--text-muted); }
.compose-icons svg { width: 18px; height: 18px; fill: currentColor; cursor: pointer; transition: color 0.15s; }
.compose-icons svg:hover { color: var(--accent); }

/* --- Feed --- */
.feed { padding: 16px 20px; }
.empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: 48px 20px;
    font-size: 14px;
}

.post {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 12px;
    display: flex;
    gap: 12px;
    transition: border 0.15s, box-shadow 0.15s;
}
.post:hover { border-color: var(--text-muted); box-shadow: var(--shadow-sm); }

.vote-col {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    flex-shrink: 0;
    min-width: 34px;
}
.vote-btn {
    background: transparent;
    border: none;
    cursor: pointer;
    color: var(--text-muted);
    padding: 4px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: color 0.15s, background 0.15s;
}
.vote-btn:hover { background: var(--bg-hover); color: var(--text); }
.vote-btn.up:hover, .vote-btn.up.active { color: var(--upvote); }
.vote-btn.down:hover, .vote-btn.down.active { color: var(--downvote); }
.vote-btn svg { width: 18px; height: 18px; fill: currentColor; }
.vote-count {
    font-size: 13px;
    font-weight: 700;
    color: var(--text);
    line-height: 1;
    padding: 2px 0;
}
.vote-count.up { color: var(--upvote); }
.vote-count.down { color: var(--downvote); }

.post-body { flex: 1; min-width: 0; }
.post-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
    flex-wrap: wrap;
}
.post-author {
    font-size: 14px;
    font-weight: 700;
    color: var(--text-heading);
}
.post-handle { color: var(--text-muted); font-size: 13px; }
.post-date {
    color: var(--text-muted);
    font-size: 12px;
    margin-left: auto;
}
.post-content {
    font-size: 15px;
    white-space: pre-wrap;
    word-break: break-word;
    margin-bottom: 10px;
    cursor: pointer;
    color: var(--text);
}
.post-content:hover { color: var(--accent); }
.post-actions {
    display: flex;
    gap: 20px;
    font-size: 13px;
    color: var(--text-muted);
}
.post-action {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    background: none;
    border: none;
    color: inherit;
    font-family: inherit;
    font-size: 13px;
    padding: 4px 8px;
    border-radius: 6px;
    transition: background 0.15s, color 0.15s;
}
.post-action:hover { background: var(--bg-hover); color: var(--accent); }
.post-action svg { width: 16px; height: 16px; fill: currentColor; }

/* --- Comment --- */
.comment {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 10px;
    display: flex;
    gap: 10px;
}
.comment-body { flex: 1; min-width: 0; }
.comment-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-bottom: 6px;
    flex-wrap: wrap;
}
.comment-author { font-size: 13px; font-weight: 700; color: var(--text-heading); }
.comment-handle { color: var(--text-muted); font-size: 12px; }
.comment-date { color: var(--text-muted); font-size: 12px; margin-left: auto; }
.comment-content { font-size: 14px; white-space: pre-wrap; word-break: break-word; }

/* --- Profile --- */
.profile-head {
    padding: 24px 20px;
    border-bottom: 1px solid var(--border);
}
.profile-top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    margin-bottom: 12px;
}
.profile-name {
    font-size: 22px;
    font-weight: 700;
    color: var(--text-heading);
    line-height: 1.2;
}
.profile-handle { font-size: 14px; color: var(--text-muted); margin-top: 2px; }
.profile-bio {
    font-size: 14px;
    color: var(--text);
    white-space: pre-wrap;
    margin-bottom: 12px;
    line-height: 1.6;
}
.profile-stats {
    display: flex;
    gap: 20px;
    font-size: 13px;
    color: var(--text-muted);
}
.profile-stats strong { color: var(--text-heading); }

/* --- Search results --- */
.search-results-wrap { padding: 20px; }
.search-query-head {
    font-size: 14px;
    color: var(--text-muted);
    margin-bottom: 20px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
}
.search-query-head strong { color: var(--text-heading); font-size: 16px; }
.search-section { margin-bottom: 28px; }
.search-section-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 12px;
}
.search-user-card {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 14px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 8px;
}
.search-user-avatar {
    width: 36px; height: 36px;
    background: var(--accent);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    color: white; font-weight: 700; font-size: 16px;
    flex-shrink: 0;
}
.search-user-info { flex: 1; min-width: 0; }
.search-user-name { font-weight: 700; color: var(--text-heading); font-size: 14px; }
.search-user-handle { color: var(--text-muted); font-size: 13px; }
.search-user-bio { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
.search-comment-context {
    font-size: 12px;
    color: var(--text-muted);
    margin-bottom: 4px;
}
.search-comment-context a {
    color: var(--accent);
    cursor: pointer;
    text-decoration: none;
}

/* --- Back link --- */
.back-link {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: var(--text-muted);
    font-size: 13px;
    cursor: pointer;
    padding: 6px 10px;
    border-radius: 6px;
    background: none;
    border: none;
    font-family: inherit;
    margin-bottom: 16px;
    transition: color 0.15s, background 0.15s;
}
.back-link:hover { color: var(--text); background: var(--bg-secondary); }
.back-link svg { width: 16px; height: 16px; fill: currentColor; }

.comments-section {
    padding: 20px;
    border-top: 1px solid var(--border);
}
.comments-section-title {
    font-size: 15px;
    font-weight: 700;
    color: var(--text-heading);
    margin-bottom: 16px;
}
.comment-form { margin-bottom: 20px; }
.comment-form .form-textarea { min-height: 60px; margin-bottom: 8px; }

/* --- Settings --- */
.settings-wrap { padding: 24px 20px; max-width: 520px; }
.settings-section {
    padding-bottom: 24px;
    margin-bottom: 24px;
    border-bottom: 1px solid var(--border);
}
.settings-section:last-child { border-bottom: none; }
.settings-section-title {
    font-size: 15px;
    font-weight: 700;
    color: var(--text-heading);
    margin-bottom: 14px;
}

.hidden { display: none !important; }

@media (max-width: 900px) {
    .app { grid-template-columns: 1fr; }
    .col.left, .col.right { display: none; }
}
</style>
</head>
<body>
<div class="app">

    <!-- LEFT COLUMN -->
    <aside class="col left">
        <div class="brand">
            <div class="brand-icon">
                <svg viewBox="0 0 32 32"><path d="M11 8v16h10v-3h-7V8z" fill="#fff"/></svg>
            </div>
            <div class="brand-name">Litodon</div>
        </div>

        <div class="search-wrap">
            <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
            <input type="text" id="search-input" class="search-input" data-t="search_placeholder" placeholder="Поиск">
        </div>

        <div class="left-footer">
            <a href="/privacy" target="_blank" class="footer-btn">
                <svg viewBox="0 0 24 24"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V6.3l7-3.11v8.8z"/></svg>
                <span data-t="privacy">Политика конфиденциальности</span>
            </a>
            <a href="/changelogs" target="_blank" class="footer-btn">
                <svg viewBox="0 0 24 24"><path d="M13 3c-4.97 0-9 4.03-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42C8.27 19.99 10.51 21 13 21c4.97 0 9-4.03 9-9s-4.03-9-9-9zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z"/></svg>
                <span data-t="changelogs">История изменений</span>
            </a>
        </div>
    </aside>

    <!-- CENTER COLUMN -->
    <main class="col center" id="center">
        <div id="user-header" class="center-header hidden">
            <div class="center-title" id="center-title">Litodon</div>
        </div>

        <!-- AUTH VIEW -->
        <div id="view-auth" class="auth-wrap">
            <div id="auth-error" class="error-box hidden"></div>

            <div id="login-form">
                <h2 class="auth-title" data-t="login_title">Войти в Litodon</h2>
                <div class="form-group">
                    <label class="form-label" data-t="username_label">Ник</label>
                    <input type="text" id="login-username" class="form-input" placeholder="username">
                </div>
                <div class="form-group">
                    <label class="form-label" data-t="password_label">Пароль</label>
                    <input type="password" id="login-password" class="form-input" placeholder="••••••••">
                </div>
                <button class="btn btn-primary btn-full" onclick="submitLogin()" data-t="login_btn">Войти</button>
                <div class="auth-switch">
                    <span data-t="no_account">Нет аккаунта?</span> <a onclick="switchAuth('register')" data-t="register_link">Зарегистрироваться</a>
                </div>
            </div>

            <div id="register-form" class="hidden">
                <h2 class="auth-title" data-t="register_title">Регистрация</h2>
                <div class="form-group">
                    <label class="form-label" data-t="display_name_label">Отображаемое имя</label>
                    <input type="text" id="reg-display-name" class="form-input" placeholder="Иван Иванов">
                </div>
                <div class="form-group">
                    <label class="form-label" data-t="username_label">Ник</label>
                    <input type="text" id="reg-username" class="form-input" placeholder="ivan">
                </div>
                <div class="form-group">
                    <label class="form-label" data-t="password_label">Пароль</label>
                    <input type="password" id="reg-password" class="form-input" placeholder="••••••••">
                </div>
                <div class="form-group">
                    <label class="form-label" data-t="confirm_password_label">Повтор пароля</label>
                    <input type="password" id="reg-confirm-password" class="form-input" placeholder="••••••••">
                </div>
                <button class="btn btn-primary btn-full" onclick="submitRegister()" data-t="register_btn">Создать аккаунт</button>
                <div class="auth-switch">
                    <span data-t="have_account">Уже есть аккаунт?</span> <a onclick="switchAuth('login')" data-t="login_link">Войти</a>
                </div>
            </div>
        </div>

        <!-- HOME VIEW -->
        <div id="view-home" class="hidden">
            <div class="compose">
                <div class="compose-body">
                    <textarea id="post-content" class="compose-textarea" data-t="post_placeholder" placeholder="Что нового?"></textarea>
                    <div class="compose-actions">
                        <div class="compose-icons">
                            <svg viewBox="0 0 24 24"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>
                            <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
                        </div>
                        <button class="btn btn-primary btn-sm" onclick="submitPost()" data-t="publish_btn">Опубликовать</button>
                    </div>
                </div>
            </div>
            <div class="feed" id="home-feed"></div>
        </div>

        <!-- POST DETAIL VIEW -->
        <div id="view-post" class="hidden">
            <div style="padding: 20px;">
                <button class="back-link" onclick="renderView('home')">
                    <svg viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
                    <span data-t="back">Назад</span>
                </button>
                <div id="post-detail"></div>
            </div>
            <div class="comments-section">
                <div class="comments-section-title" data-t="comments">Комментарии</div>
                <div class="comment-form">
                    <textarea id="comment-content" class="form-textarea" data-t="comment_placeholder" placeholder="Написать комментарий..."></textarea>
                    <button class="btn btn-primary btn-sm" onclick="submitComment()" data-t="send_comment">Отправить</button>
                </div>
                <div id="comments-list"></div>
            </div>
        </div>

        <!-- PROFILE VIEW -->
        <div id="view-profile" class="hidden">
            <div class="profile-head" id="profile-info"></div>
            <div class="feed" id="profile-feed"></div>
        </div>

        <!-- SETTINGS VIEW -->
        <div id="view-settings" class="hidden">
            <div class="settings-wrap">
                <div class="settings-section">
                    <div class="settings-section-title" data-t="language_label">Язык интерфейса</div>
                    <select id="settings-language" class="form-select" onchange="changeLanguage(this.value)">
                        <option value="ru">Русский</option>
                        <option value="en">English</option>
                    </select>
                </div>
                <div class="settings-section">
                    <div class="settings-section-title" data-t="theme_label">Тема оформления</div>
                    <select id="settings-theme" class="form-select" onchange="changeTheme(this.value)">
                        <option value="dark" data-t="theme_dark">Тёмная</option>
                        <option value="light" data-t="theme_light">Светлая</option>
                    </select>
                </div>
            </div>
        </div>

        <!-- SEARCH VIEW -->
        <div id="view-search" class="hidden">
            <div class="search-results-wrap">
                <div class="search-query-head">
                    <span data-t="search_results_for">Результаты поиска:</span> <strong id="search-query-display"></strong>
                </div>
                <div class="search-section" id="search-users-section">
                    <div class="search-section-title" data-t="search_users">Пользователи</div>
                    <div id="search-users-list"></div>
                </div>
                <div class="search-section" id="search-posts-section">
                    <div class="search-section-title" data-t="search_posts">Посты</div>
                    <div id="search-posts-list"></div>
                </div>
                <div class="search-section" id="search-comments-section">
                    <div class="search-section-title" data-t="search_comments">Комментарии</div>
                    <div id="search-comments-list"></div>
                </div>
            </div>
        </div>
    </main>

    <!-- RIGHT COLUMN -->
    <aside class="col right">
        <div class="right-brand">
            <div class="brand-icon">
                <svg viewBox="0 0 32 32"><path d="M11 8v16h10v-3h-7V8z" fill="#fff"/></svg>
            </div>
            <div class="brand-name">Litodon</div>
        </div>

        <nav class="nav-list" id="nav-list">
            <button class="nav-btn" onclick="renderView('home')" id="nav-home">
                <svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
                <span data-t="nav_home">Главная</span>
            </button>
            <button class="nav-btn" onclick="renderView('profile')" id="nav-profile">
                <svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
                <span data-t="nav_profile">Профиль</span>
            </button>
            <button class="nav-btn" onclick="renderView('settings')" id="nav-settings">
                <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.04.24.24.41.48.41h3.84c.24 0 .43-.17.47-.41l.36-2.54c.59-.24 1.13-.57 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>
                <span data-t="nav_settings">Настройки</span>
            </button>
        </nav>

        <div class="user-badge hidden" id="user-badge">
            <div class="user-badge-info">
                <div class="user-badge-name" id="user-badge-name"></div>
                <div class="user-badge-handle" id="user-badge-handle"></div>
            </div>
            <button class="logout-btn" onclick="logout()" title="logout">
                <svg viewBox="0 0 24 24"><path d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg>
            </button>
        </div>
    </aside>
</div>

<script>
const translations = {
    ru: {
        search_placeholder: "Поиск",
        privacy: "Политика конфиденциальности",
        changelogs: "История изменений",
        login_title: "Войти в Litodon",
        register_title: "Регистрация",
        username_label: "Ник",
        password_label: "Пароль",
        display_name_label: "Отображаемое имя",
        confirm_password_label: "Повтор пароля",
        login_btn: "Войти",
        register_btn: "Создать аккаунт",
        no_account: "Нет аккаунта?",
        have_account: "Уже есть аккаунт?",
        register_link: "Зарегистрироваться",
        login_link: "Войти",
        post_placeholder: "Что нового?",
        publish_btn: "Опубликовать",
        back: "Назад",
        comments: "Комментарии",
        comment_placeholder: "Написать комментарий...",
        send_comment: "Отправить",
        language_label: "Язык интерфейса",
        theme_label: "Тема оформления",
        theme_dark: "Тёмная",
        theme_light: "Светлая",
        nav_home: "Главная",
        nav_profile: "Профиль",
        nav_settings: "Настройки",
        edit_profile: "Редактировать",
        save: "Сохранить",
        cancel: "Отмена",
        edit_name_label: "Отображаемое имя",
        edit_bio_label: "Описание профиля",
        no_bio: "Нет описания",
        no_posts: "Пока нет постов.",
        no_comments: "Нет комментариев. Будьте первым!",
        no_results: "Ничего не найдено.",
        search_results_for: "Результаты поиска:",
        search_users: "Пользователи",
        search_posts: "Посты",
        search_comments: "Комментарии",
        comments_count: "комментариев",
        post_context: "Комментарий к посту от",
        followers: "подписчиков",
        following: "подписок",
        header_home: "Главная",
        header_profile: "Профиль",
        header_settings: "Настройки",
        header_post: "Пост",
        header_search: "Поиск",
        // errors
        ERR_USERNAME_TAKEN: "Этот ник уже занят",
        ERR_FILL_ALL: "Заполните все обязательные поля",
        ERR_PASSWORDS_MISMATCH: "Пароли не совпадают",
        ERR_PASSWORD_SHORT: "Пароль должен быть не менее 4 символов",
        ERR_INVALID_CREDENTIALS: "Неверный ник или пароль",
        ERR_UNAUTHORIZED: "Требуется авторизация",
        ERR_EMPTY_POST: "Пост не может быть пустым",
        ERR_EMPTY_COMMENT: "Комментарий не может быть пустым",
        ERR_POST_NOT_FOUND: "Пост не найден",
        ERR_UNKNOWN: "Произошла ошибка"
    },
    en: {
        search_placeholder: "Search",
        privacy: "Privacy policy",
        changelogs: "Changelog",
        login_title: "Sign in to Litodon",
        register_title: "Sign up",
        username_label: "Username",
        password_label: "Password",
        display_name_label: "Display name",
        confirm_password_label: "Confirm password",
        login_btn: "Sign in",
        register_btn: "Create account",
        no_account: "No account?",
        have_account: "Already have an account?",
        register_link: "Sign up",
        login_link: "Sign in",
        post_placeholder: "What's new?",
        publish_btn: "Publish",
        back: "Back",
        comments: "Comments",
        comment_placeholder: "Write a comment...",
        send_comment: "Send",
        language_label: "Interface language",
        theme_label: "Theme",
        theme_dark: "Dark",
        theme_light: "Light",
        nav_home: "Home",
        nav_profile: "Profile",
        nav_settings: "Settings",
        edit_profile: "Edit",
        save: "Save",
        cancel: "Cancel",
        edit_name_label: "Display name",
        edit_bio_label: "Profile bio",
        no_bio: "No bio",
        no_posts: "No posts yet.",
        no_comments: "No comments yet. Be the first!",
        no_results: "Nothing found.",
        search_results_for: "Search results:",
        search_users: "Users",
        search_posts: "Posts",
        search_comments: "Comments",
        comments_count: "comments",
        post_context: "Comment on post by",
        followers: "followers",
        following: "following",
        header_home: "Home",
        header_profile: "Profile",
        header_settings: "Settings",
        header_post: "Post",
        header_search: "Search",
        // errors
        ERR_USERNAME_TAKEN: "This username is taken",
        ERR_FILL_ALL: "Please fill all required fields",
        ERR_PASSWORDS_MISMATCH: "Passwords do not match",
        ERR_PASSWORD_SHORT: "Password must be at least 4 characters",
        ERR_INVALID_CREDENTIALS: "Invalid username or password",
        ERR_UNAUTHORIZED: "Authorization required",
        ERR_EMPTY_POST: "Post cannot be empty",
        ERR_EMPTY_COMMENT: "Comment cannot be empty",
        ERR_POST_NOT_FOUND: "Post not found",
        ERR_UNKNOWN: "An error occurred"
    }
};

let currentLang = localStorage.getItem('litodon_lang') || 'ru';
let currentTheme = localStorage.getItem('litodon_theme') || 'dark';
let currentUser = null;
let currentView = 'auth';
let currentPostId = null;
let isEditingProfile = false;

function t(key) {
    return (translations[currentLang] && translations[currentLang][key]) || key;
}

function applyTranslations() {
    document.querySelectorAll('[data-t]').forEach(el => {
        const key = el.getAttribute('data-t');
        el.textContent = t(key);
    });
    document.querySelectorAll('[data-t-placeholder]').forEach(el => {
        const key = el.getAttribute('data-t-placeholder');
        el.placeholder = t(key);
    });
    const searchInput = document.getElementById('search-input');
    if (searchInput) searchInput.placeholder = t('search_placeholder');
    document.getElementById('settings-language').value = currentLang;
    document.getElementById('settings-theme').value = currentTheme;
}

function applyTheme() {
    document.documentElement.setAttribute('data-theme', currentTheme);
    document.body.setAttribute('data-theme', currentTheme);
}

function changeTheme(theme) {
    currentTheme = theme;
    localStorage.setItem('litodon_theme', theme);
    applyTheme();
}

function changeLanguage(lang) {
    currentLang = lang;
    localStorage.setItem('litodon_lang', lang);
    applyTranslations();
    renderView(currentView, true);
}

function translateError(code) {
    return t(code) || t('ERR_UNKNOWN');
}

async function init() {
    applyTheme();
    applyTranslations();
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

function renderView(view, keepQuery) {
    currentView = view;
    if (!keepQuery) isEditingProfile = false;

    ['auth','home','post','profile','settings','search'].forEach(v => {
        const el = document.getElementById('view-' + v);
        if (el) el.classList.add('hidden');
    });

    const header = document.getElementById('user-header');
    const userBadge = document.getElementById('user-badge');
    const navHome = document.getElementById('nav-home');
    const navProfile = document.getElementById('nav-profile');
    const navSettings = document.getElementById('nav-settings');
    [navHome, navProfile, navSettings].forEach(n => n && n.classList.remove('active'));

    if (currentUser) {
        header.classList.remove('hidden');
        userBadge.classList.remove('hidden');
        document.getElementById('user-badge-name').textContent = currentUser.display_name;
        document.getElementById('user-badge-handle').textContent = '@' + currentUser.username;
    } else {
        header.classList.add('hidden');
        userBadge.classList.add('hidden');
    }

    const titleEl = document.getElementById('center-title');

    if (view === 'auth') {
        document.getElementById('view-auth').classList.remove('hidden');
        titleEl.textContent = '';
    } else if (view === 'home') {
        document.getElementById('view-home').classList.remove('hidden');
        titleEl.textContent = t('header_home');
        if (navHome) navHome.classList.add('active');
        loadHomeFeed();
    } else if (view === 'profile') {
        document.getElementById('view-profile').classList.remove('hidden');
        titleEl.textContent = t('header_profile');
        if (navProfile) navProfile.classList.add('active');
        loadProfile();
    } else if (view === 'settings') {
        document.getElementById('view-settings').classList.remove('hidden');
        titleEl.textContent = t('header_settings');
        if (navSettings) navSettings.classList.add('active');
        document.getElementById('settings-language').value = currentLang;
        document.getElementById('settings-theme').value = currentTheme;
    } else if (view === 'post') {
        document.getElementById('view-post').classList.remove('hidden');
        titleEl.textContent = t('header_post');
        loadPostDetail();
    } else if (view === 'search') {
        document.getElementById('view-search').classList.remove('hidden');
        titleEl.textContent = t('header_search');
    }
}

function switchAuth(view) {
    document.getElementById('auth-error').classList.add('hidden');
    if (view === 'login') {
        document.getElementById('login-form').classList.remove('hidden');
        document.getElementById('register-form').classList.add('hidden');
    } else {
        document.getElementById('login-form').classList.add('hidden');
        document.getElementById('register-form').classList.remove('hidden');
    }
}

async function submitLogin() {
    const username = document.getElementById('login-username').value;
    const password = document.getElementById('login-password').value;
    const errorDiv = document.getElementById('auth-error');
    try {
        const res = await fetch('/api/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username, password})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);
        document.getElementById('login-username').value = '';
        document.getElementById('login-password').value = '';
        await init();
    } catch (err) {
        errorDiv.textContent = translateError(err.message);
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
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username, display_name, password, confirm_password})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);
        ['reg-display-name','reg-username','reg-password','reg-confirm-password'].forEach(id => document.getElementById(id).value = '');
        await init();
    } catch (err) {
        errorDiv.textContent = translateError(err.message);
        errorDiv.classList.remove('hidden');
    }
}

async function logout() {
    await fetch('/api/logout', {method: 'POST'});
    currentUser = null;
    renderView('auth');
}

async function loadHomeFeed() {
    const res = await fetch('/api/posts');
    const posts = await res.json();
    const feed = document.getElementById('home-feed');
    feed.innerHTML = '';
    if (posts.length === 0) {
        feed.innerHTML = '<div class="empty-state">' + t('no_posts') + '</div>';
        return;
    }
    posts.forEach(p => feed.appendChild(createPostEl(p)));
}

async function submitPost() {
    const ta = document.getElementById('post-content');
    const content = ta.value;
    if (!content.trim()) return;
    const res = await fetch('/api/posts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content})
    });
    if (res.ok) {
        ta.value = '';
        loadHomeFeed();
    }
}

function createPostEl(post) {
    const date = new Date(post.timestamp).toLocaleString(currentLang === 'ru' ? 'ru-RU' : 'en-US');
    const el = document.createElement('div');
    el.className = 'post';
    const upActive = post.user_vote === 'up' ? 'active' : '';
    const downActive = post.user_vote === 'down' ? 'active' : '';
    const score = post.upvotes - post.downvotes;
    const scoreClass = score > 0 ? 'up' : (score < 0 ? 'down' : '');
    el.innerHTML = `
        <div class="vote-col">
            <button class="vote-btn up ${upActive}" onclick="handleVote('${post.id}','post','up')">
                <svg viewBox="0 0 24 24"><path d="M12 4l-8 8h6v8h4v-8h6z"/></svg>
            </button>
            <span class="vote-count ${scoreClass}">${score}</span>
            <button class="vote-btn down ${downActive}" onclick="handleVote('${post.id}','post','down')">
                <svg viewBox="0 0 24 24"><path d="M12 20l8-8h-6V4h-4v8H4z"/></svg>
            </button>
        </div>
        <div class="post-body">
            <div class="post-head">
                <span class="post-author">${esc(post.display_name)}</span>
                <span class="post-handle">@${esc(post.author)}</span>
                <span class="post-date">${date}</span>
            </div>
            <div class="post-content" onclick="openPost('${post.id}')">${esc(post.content)}</div>
            <div class="post-actions">
                <button class="post-action" onclick="openPost('${post.id}')">
                    <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg>
                    ${t('comments')}
                </button>
            </div>
        </div>
    `;
    return el;
}

function openPost(id) {
    currentPostId = id;
    renderView('post');
}

async function loadPostDetail() {
    const res = await fetch('/api/posts/' + currentPostId);
    if (!res.ok) { renderView('home'); return; }
    const post = await res.json();
    const container = document.getElementById('post-detail');
    container.innerHTML = '';
    container.appendChild(createPostEl(post));
    loadComments();
}

async function loadComments() {
    const res = await fetch('/api/posts/' + currentPostId + '/comments');
    const comments = await res.json();
    const list = document.getElementById('comments-list');
    list.innerHTML = '';
    if (comments.length === 0) {
        list.innerHTML = '<div class="empty-state">' + t('no_comments') + '</div>';
        return;
    }
    comments.forEach(c => list.appendChild(createCommentEl(c)));
}

function createCommentEl(c) {
    const date = new Date(c.timestamp).toLocaleString(currentLang === 'ru' ? 'ru-RU' : 'en-US');
    const el = document.createElement('div');
    el.className = 'comment';
    const upActive = c.user_vote === 'up' ? 'active' : '';
    const downActive = c.user_vote === 'down' ? 'active' : '';
    const score = c.upvotes - c.downvotes;
    const scoreClass = score > 0 ? 'up' : (score < 0 ? 'down' : '');
    el.innerHTML = `
        <div class="vote-col">
            <button class="vote-btn up ${upActive}" onclick="handleVote('${c.id}','comment','up')">
                <svg viewBox="0 0 24 24"><path d="M12 4l-8 8h6v8h4v-8h6z"/></svg>
            </button>
            <span class="vote-count ${scoreClass}">${score}</span>
            <button class="vote-btn down ${downActive}" onclick="handleVote('${c.id}','comment','down')">
                <svg viewBox="0 0 24 24"><path d="M12 20l8-8h-6V4h-4v8H4z"/></svg>
            </button>
        </div>
        <div class="comment-body">
            <div class="comment-head">
                <span class="comment-author">${esc(c.display_name)}</span>
                <span class="comment-handle">@${esc(c.author)}</span>
                <span class="comment-date">${date}</span>
            </div>
            <div class="comment-content">${esc(c.content)}</div>
        </div>
    `;
    return el;
}

async function submitComment() {
    const ta = document.getElementById('comment-content');
    const content = ta.value;
    if (!content.trim()) return;
    const res = await fetch('/api/posts/' + currentPostId + '/comments', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content})
    });
    if (res.ok) {
        ta.value = '';
        loadComments();
    }
}

async function handleVote(targetId, targetType, voteType) {
    if (!currentUser) return;
    const res = await fetch('/api/vote', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target_id: targetId, target_type: targetType, vote_type: voteType})
    });
    if (res.ok) {
        if (currentView === 'post') {
            loadPostDetail();
        } else if (currentView === 'home') {
            loadHomeFeed();
        } else if (currentView === 'profile') {
            loadProfile();
        } else if (currentView === 'search') {
            const q = document.getElementById('search-query-display').textContent;
            if (q) performSearch(q);
        }
    }
}

async function loadProfile() {
    const infoDiv = document.getElementById('profile-info');
    if (!currentUser) return;

    if (isEditingProfile) {
        infoDiv.innerHTML = `
            <div class="form-group">
                <label class="form-label">${t('edit_name_label')}</label>
                <input type="text" id="edit-display-name" class="form-input" value="${esc(currentUser.display_name)}">
            </div>
            <div class="form-group">
                <label class="form-label">${t('edit_bio_label')}</label>
                <textarea id="edit-bio" class="form-textarea">${esc(currentUser.bio || '')}</textarea>
            </div>
            <div style="display:flex; gap:8px;">
                <button class="btn btn-primary btn-sm" onclick="saveProfile()">${t('save')}</button>
                <button class="btn btn-secondary btn-sm" onclick="toggleProfileEdit()">${t('cancel')}</button>
            </div>
        `;
    } else {
        infoDiv.innerHTML = `
            <div class="profile-top">
                <div>
                    <div class="profile-name">${esc(currentUser.display_name)}</div>
                    <div class="profile-handle">@${esc(currentUser.username)}</div>
                </div>
                <button class="btn btn-secondary btn-sm" onclick="toggleProfileEdit()">${t('edit_profile')}</button>
            </div>
            <div class="profile-bio">${esc(currentUser.bio || t('no_bio'))}</div>
            <div class="profile-stats">
                <span><strong>0</strong> ${t('followers')}</span>
                <span><strong>0</strong> ${t('following')}</span>
            </div>
        `;
    }

    const res = await fetch('/api/posts');
    const all = await res.json();
    const mine = all.filter(p => p.author === currentUser.username);
    const feed = document.getElementById('profile-feed');
    feed.innerHTML = '';
    if (mine.length === 0) {
        feed.innerHTML = '<div class="empty-state">' + t('no_posts') + '</div>';
        return;
    }
    mine.forEach(p => feed.appendChild(createPostEl(p)));
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
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({display_name, bio})
    });
    if (res.ok) {
        const data = await res.json();
        currentUser.display_name = data.display_name;
        currentUser.bio = data.bio;
        document.getElementById('user-badge-name').textContent = currentUser.display_name;
        isEditingProfile = false;
        loadProfile();
    }
}

// --- SEARCH ---
document.getElementById('search-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') {
        const q = e.target.value.trim();
        if (q) performSearch(q);
    }
});

async function performSearch(query) {
    renderView('search', true);
    document.getElementById('search-query-display').textContent = query;
    const res = await fetch('/api/search?q=' + encodeURIComponent(query));
    const data = await res.json();

    const usersList = document.getElementById('search-users-list');
    usersList.innerHTML = '';
    if (data.users.length === 0) {
        usersList.innerHTML = '<div class="empty-state">' + t('no_results') + '</div>';
    } else {
        data.users.forEach(u => {
            const el = document.createElement('div');
            el.className = 'search-user-card';
            el.innerHTML = `
                <div class="search-user-avatar">${esc(u.display_name[0] || '?').toUpperCase()}</div>
                <div class="search-user-info">
                    <div class="search-user-name">${esc(u.display_name)}</div>
                    <div class="search-user-handle">@${esc(u.username)}</div>
                    ${u.bio ? '<div class="search-user-bio">' + esc(u.bio) + '</div>' : ''}
                </div>
            `;
            usersList.appendChild(el);
        });
    }

    const postsList = document.getElementById('search-posts-list');
    postsList.innerHTML = '';
    if (data.posts.length === 0) {
        postsList.innerHTML = '<div class="empty-state">' + t('no_results') + '</div>';
    } else {
        data.posts.forEach(p => postsList.appendChild(createPostEl(p)));
    }

    const commentsList = document.getElementById('search-comments-list');
    commentsList.innerHTML = '';
    if (data.comments.length === 0) {
        commentsList.innerHTML = '<div class="empty-state">' + t('no_results') + '</div>';
    } else {
        data.comments.forEach(c => {
            const el = document.createElement('div');
            el.className = 'comment';
            const score = c.upvotes - c.downvotes;
            const scoreClass = score > 0 ? 'up' : (score < 0 ? 'down' : '');
            el.innerHTML = `
                <div class="vote-col">
                    <button class="vote-btn up ${c.user_vote === 'up' ? 'active' : ''}" onclick="handleVote('${c.id}','comment','up')">
                        <svg viewBox="0 0 24 24"><path d="M12 4l-8 8h6v8h4v-8h6z"/></svg>
                    </button>
                    <span class="vote-count ${scoreClass}">${score}</span>
                    <button class="vote-btn down ${c.user_vote === 'down' ? 'active' : ''}" onclick="handleVote('${c.id}','comment','down')">
                        <svg viewBox="0 0 24 24"><path d="M12 20l8-8h-6V4h-4v8H4z"/></svg>
                    </button>
                </div>
                <div class="comment-body">
                    <div class="search-comment-context">${t('post_context')} @${esc(c.post_author)} — <a onclick="openPost('${c.post_id}')">${t('back')}</a></div>
                    <div class="comment-head">
                        <span class="comment-author">${esc(c.display_name)}</span>
                        <span class="comment-handle">@${esc(c.author)}</span>
                    </div>
                    <div class="comment-content">${esc(c.content)}</div>
                </div>
            `;
            commentsList.appendChild(el);
        });
    }
}

function esc(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

// Disable right click (except on inputs/textareas)
document.addEventListener('contextmenu', e => {
    if (!e.target.closest('input, textarea')) {
        e.preventDefault();
    }
});

init();
</script>
</body>
</html>
"""


# --- PRIVACY PAGE ---
PRIVACY_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Privacy Policy — Litodon</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
:root { --bg: #191b22; --bg-secondary: #282c37; --border: #393f4f; --text: #d9e1e8; --text-heading: #ffffff; --text-muted: #7d869a; --accent: #6364ff; }
[data-theme="light"] { --bg: #f4f6f9; --bg-secondary: #ffffff; --border: #d8dee6; --text: #2e3440; --text-heading: #191b22; --text-muted: #6b7381; --accent: #6364ff; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; line-height: 1.7; padding: 40px 20px; font-size: 15px; }
.wrap { max-width: 720px; margin: 0 auto; }
.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 32px; }
.brand-icon { width: 36px; height: 36px; background: var(--accent); border-radius: 8px; display: flex; align-items: center; justify-content: center; }
.brand-icon svg { width: 22px; height: 22px; }
.brand-name { font-size: 20px; font-weight: 700; color: var(--text-heading); }
h1 { font-size: 28px; color: var(--text-heading); margin-bottom: 8px; }
.subtitle { color: var(--text-muted); margin-bottom: 32px; font-size: 14px; }
h2 { font-size: 18px; color: var(--text-heading); margin: 28px 0 12px; }
p { margin-bottom: 14px; color: var(--text); }
ul { padding-left: 24px; margin-bottom: 14px; }
li { margin-bottom: 6px; }
a.back { display: inline-flex; align-items: center; gap: 8px; margin-top: 32px; color: var(--accent); text-decoration: none; font-size: 14px; font-weight: 500; }
a.back:hover { text-decoration: underline; }
.lang-switch { position: absolute; top: 20px; right: 20px; display: flex; gap: 8px; }
.lang-btn { background: var(--bg-secondary); border: 1px solid var(--border); color: var(--text); padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; font-family: inherit; }
.lang-btn.active { background: var(--accent); color: white; border-color: var(--accent); }
.hidden { display: none !important; }
</style>
</head>
<body>
<div class="lang-switch">
    <button class="lang-btn" id="btn-ru" onclick="setLang('ru')">Русский</button>
    <button class="lang-btn" id="btn-en" onclick="setLang('en')">English</button>
</div>
<div class="wrap">
    <div class="brand">
        <div class="brand-icon"><svg viewBox="0 0 32 32"><path d="M11 8v16h10v-3h-7V8z" fill="#fff"/></svg></div>
        <div class="brand-name">Litodon</div>
    </div>

    <div id="content-ru">
        <h1>Политика конфиденциальности</h1>
        <div class="subtitle">Последнее обновление: 21 сентября 2026</div>
        <p>Litodon уважает вашу конфиденциальность. В этом документе описано, какие данные мы собираем и как их используем.</p>
        <h2>1. Какие данные мы собираем</h2>
        <ul>
            <li>Имя пользователя (ник) и отображаемое имя</li>
            <li>Пароль в зашифрованном виде</li>
            <li>Посты, комментарии и голоса, которые вы оставляете</li>
            <li>Технические данные сессии (cookie для авторизации)</li>
        </ul>
        <h2>2. Как мы используем данные</h2>
        <p>Данные используются исключительно для обеспечения работы сервиса: аутентификации, отображения вашего профиля и контента.</p>
        <h2>3. Хранение данных</h2>
        <p>Все данные хранятся в оперативной памяти сервера и удаляются при его перезапуске. Мы не передаём данные третьим лицам.</p>
        <h2>4. Cookie</h2>
        <p>Мы используем один сессионный cookie для поддержания авторизации. Он не содержит личной информации и удаляется при выходе из системы.</p>
        <h2>5. Ваши права</h2>
        <p>Вы можете в любой момент удалить свои посты и комментарии или запросить удаление аккаунта.</p>
        <h2>6. Контакты</h2>
        <p>По вопросам, связанным с конфиденциальностью, обращайтесь к администрации сервера.</p>
    </div>

    <div id="content-en" class="hidden">
        <h1>Privacy Policy</h1>
        <div class="subtitle">Last updated: September 21, 2026</div>
        <p>Litodon respects your privacy. This document describes what data we collect and how we use it.</p>
        <h2>1. Data we collect</h2>
        <ul>
            <li>Username and display name</li>
            <li>Encrypted password</li>
            <li>Posts, comments and votes you submit</li>
            <li>Session technical data (authorization cookie)</li>
        </ul>
        <h2>2. How we use data</h2>
        <p>Data is used solely to operate the service: authentication, displaying your profile and content.</p>
        <h2>3. Data storage</h2>
        <p>All data is stored in the server's RAM and is deleted on restart. We do not share data with third parties.</p>
        <h2>4. Cookies</h2>
        <p>We use a single session cookie to maintain authorization. It contains no personal information and is deleted on logout.</p>
        <h2>5. Your rights</h2>
        <p>You can delete your posts and comments at any time or request account deletion.</p>
        <h2>6. Contact</h2>
        <p>For privacy-related inquiries, please contact the server administration.</p>
    </div>

    <a href="/" class="back">← <span id="back-text">Вернуться на главную</span></a>
</div>
<script>
let lang = localStorage.getItem('litodon_lang') || 'ru';
let theme = localStorage.getItem('litodon_theme') || 'dark';
document.documentElement.setAttribute('data-theme', theme);
function setLang(l) {
    lang = l;
    localStorage.setItem('litodon_lang', l);
    document.getElementById('content-ru').classList.toggle('hidden', l !== 'ru');
    document.getElementById('content-en').classList.toggle('hidden', l !== 'en');
    document.getElementById('btn-ru').classList.toggle('active', l === 'ru');
    document.getElementById('btn-en').classList.toggle('active', l === 'en');
    document.getElementById('back-text').textContent = l === 'ru' ? 'Вернуться на главную' : 'Back to home';
    document.title = l === 'ru' ? 'Политика конфиденциальности — Litodon' : 'Privacy Policy — Litodon';
}
setLang(lang);
</script>
</body>
</html>
"""


# --- CHANGELOGS PAGE ---
CHANGELOGS_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Changelog — Litodon</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
:root { --bg: #191b22; --bg-secondary: #282c37; --border: #393f4f; --text: #d9e1e8; --text-heading: #ffffff; --text-muted: #7d869a; --accent: #6364ff; }
[data-theme="light"] { --bg: #f4f6f9; --bg-secondary: #ffffff; --border: #d8dee6; --text: #2e3440; --text-heading: #191b22; --text-muted: #6b7381; --accent: #6364ff; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; line-height: 1.7; padding: 40px 20px; font-size: 15px; }
.wrap { max-width: 720px; margin: 0 auto; }
.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 32px; }
.brand-icon { width: 36px; height: 36px; background: var(--accent); border-radius: 8px; display: flex; align-items: center; justify-content: center; }
.brand-icon svg { width: 22px; height: 22px; }
.brand-name { font-size: 20px; font-weight: 700; color: var(--text-heading); }
h1 { font-size: 28px; color: var(--text-heading); margin-bottom: 8px; }
.subtitle { color: var(--text-muted); margin-bottom: 32px; font-size: 14px; }
.version-block { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 16px; }
.version-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.version-tag { background: var(--accent); color: white; padding: 3px 10px; border-radius: 6px; font-size: 13px; font-weight: 700; }
.version-date { color: var(--text-muted); font-size: 13px; }
ul { padding-left: 24px; margin: 0; }
li { margin-bottom: 6px; }
a.back { display: inline-flex; align-items: center; gap: 8px; margin-top: 32px; color: var(--accent); text-decoration: none; font-size: 14px; font-weight: 500; }
a.back:hover { text-decoration: underline; }
.lang-switch { position: absolute; top: 20px; right: 20px; display: flex; gap: 8px; }
.lang-btn { background: var(--bg-secondary); border: 1px solid var(--border); color: var(--text); padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; font-family: inherit; }
.lang-btn.active { background: var(--accent); color: white; border-color: var(--accent); }
.hidden { display: none !important; }
</style>
</head>
<body>
<div class="lang-switch">
    <button class="lang-btn" id="btn-ru" onclick="setLang('ru')">Русский</button>
    <button class="lang-btn" id="btn-en" onclick="setLang('en')">English</button>
</div>
<div class="wrap">
    <div class="brand">
        <div class="brand-icon"><svg viewBox="0 0 32 32"><path d="M11 8v16h10v-3h-7V8z" fill="#fff"/></svg></div>
        <div class="brand-name">Litodon</div>
    </div>

    <div id="content-ru">
        <h1>История изменений</h1>
        <div class="subtitle">Все обновления Litodon</div>

        <div class="version-block">
            <div class="version-head">
                <span class="version-tag">v1.0.0</span>
                <span class="version-date">21 сентября 2026</span>
            </div>
            <ul>
                <li>Первый публичный релиз Litodon</li>
                <li>Регистрация и вход по нику и паролю</li>
                <li>Создание постов и комментариев</li>
                <li>Система голосования (апвоуты и даунвоуты)</li>
                <li>Профиль пользователя с возможностью редактирования</li>
                <li>Поиск по пользователям, постам и комментариям</li>
                <li>Поддержка русского и английского языков</li>
                <li>Тёмная и светлая темы оформления</li>
            </ul>
        </div>
    </div>

    <div id="content-en" class="hidden">
        <h1>Changelog</h1>
        <div class="subtitle">All Litodon updates</div>

        <div class="version-block">
            <div class="version-head">
                <span class="version-tag">v1.0.0</span>
                <span class="version-date">September 21, 2026</span>
            </div>
            <ul>
                <li>First public release of Litodon</li>
                <li>Sign up and sign in with username and password</li>
                <li>Create posts and comments</li>
                <li>Voting system (upvotes and downvotes)</li>
                <li>User profile with editing capabilities</li>
                <li>Search across users, posts and comments</li>
                <li>Russian and English language support</li>
                <li>Dark and light themes</li>
            </ul>
        </div>
    </div>

    <a href="/" class="back">← <span id="back-text">Вернуться на главную</span></a>
</div>
<script>
let lang = localStorage.getItem('litodon_lang') || 'ru';
let theme = localStorage.getItem('litodon_theme') || 'dark';
document.documentElement.setAttribute('data-theme', theme);
function setLang(l) {
    lang = l;
    localStorage.setItem('litodon_lang', l);
    document.getElementById('content-ru').classList.toggle('hidden', l !== 'ru');
    document.getElementById('content-en').classList.toggle('hidden', l !== 'en');
    document.getElementById('btn-ru').classList.toggle('active', l === 'ru');
    document.getElementById('btn-en').classList.toggle('active', l === 'en');
    document.getElementById('back-text').textContent = l === 'ru' ? 'Вернуться на главную' : 'Back to home';
    document.title = l === 'ru' ? 'История изменений — Litodon' : 'Changelog — Litodon';
}
setLang(lang);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return MAIN_HTML

@app.get("/privacy", response_class=HTMLResponse)
async def serve_privacy():
    return PRIVACY_HTML

@app.get("/changelogs", response_class=HTMLResponse)
async def serve_changelogs():
    return CHANGELOGS_HTML

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
