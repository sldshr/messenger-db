import uvicorn
from fastapi import FastAPI, Response, HTTPException, Cookie
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import uuid
from datetime import datetime

class InMemoryStorage:
    def __init__(self):
        self.users = {}
        self.posts = []
        self.sessions = {}
        self.comments = []
        self.votes = {}
        self.follows = {}   # {follower: set(following)}

db = InMemoryStorage()
app = FastAPI(title="Litodon")

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

def get_current_user(session_token: Optional[str]):
    if session_token and session_token in db.sessions:
        return db.sessions[session_token]
    return None

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
    username = get_current_user(session_token)
    if username:
        user_data = db.users[username]
        return {
            "username": username,
            "display_name": user_data["display_name"],
            "bio": user_data["bio"]
        }
    return {"username": None}

@app.post("/api/profile/update")
async def update_profile(data: ProfileUpdate, session_token: Optional[str] = Cookie(None)):
    username = get_current_user(session_token)
    if not username:
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
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
    current_user = get_current_user(session_token)
    return [enrich_post(p, current_user) for p in sorted(db.posts, key=lambda x: x["timestamp"], reverse=True)]

@app.get("/api/posts/{post_id}")
async def get_post(post_id: str, session_token: Optional[str] = Cookie(None)):
    current_user = get_current_user(session_token)
    for post in db.posts:
        if post["id"] == post_id:
            return enrich_post(post, current_user)
    raise HTTPException(status_code=404, detail="ERR_POST_NOT_FOUND")

@app.get("/api/posts/{post_id}/comments")
async def get_comments(post_id: str, session_token: Optional[str] = Cookie(None)):
    current_user = get_current_user(session_token)
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
    username = get_current_user(session_token)
    if not username:
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
    if not post.content.strip():
        raise HTTPException(status_code=400, detail="ERR_EMPTY_POST")
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
    username = get_current_user(session_token)
    if not username:
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
    if not data.content.strip():
        raise HTTPException(status_code=400, detail="ERR_EMPTY_COMMENT")
    if not any(p["id"] == post_id for p in db.posts):
        raise HTTPException(status_code=404, detail="ERR_POST_NOT_FOUND")
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
    username = get_current_user(session_token)
    if not username:
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
    if data.target_id not in db.votes:
        db.votes[data.target_id] = {}
    if username in db.votes[data.target_id]:
        if db.votes[data.target_id][username] == data.vote_type:
            del db.votes[data.target_id][username]
            return {"message": "OK"}
    db.votes[data.target_id][username] = data.vote_type
    return {"message": "OK"}

# --- FOLLOW ---
@app.post("/api/follow/{username}")
async def follow(username: str, session_token: Optional[str] = Cookie(None)):
    me = get_current_user(session_token)
    if not me:
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
    if me == username:
        raise HTTPException(status_code=400, detail="ERR_CANNOT_FOLLOW_SELF")
    if username not in db.users:
        raise HTTPException(status_code=404, detail="ERR_USER_NOT_FOUND")
    if me not in db.follows:
        db.follows[me] = set()
    db.follows[me].add(username)
    return {"message": "OK", "is_following": True}

@app.post("/api/unfollow/{username}")
async def unfollow(username: str, session_token: Optional[str] = Cookie(None)):
    me = get_current_user(session_token)
    if not me:
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
    if me in db.follows:
        db.follows[me].discard(username)
    return {"message": "OK", "is_following": False}

@app.get("/api/profile/{username}")
async def get_profile(username: str, session_token: Optional[str] = Cookie(None)):
    if username not in db.users:
        raise HTTPException(status_code=404, detail="ERR_USER_NOT_FOUND")
    me = get_current_user(session_token)
    user = db.users[username]
    followers = sum(1 for f, following in db.follows.items() if username in following)
    following_count = len(db.follows.get(username, set()))
    is_following = bool(me and me in db.follows and username in db.follows[me])
    return {
        "username": username,
        "display_name": user["display_name"],
        "bio": user["bio"],
        "followers": followers,
        "following": following_count,
        "is_following": is_following
    }

@app.get("/api/profile/{username}/posts")
async def get_user_posts(username: str, session_token: Optional[str] = Cookie(None)):
    current_user = get_current_user(session_token)
    result = []
    for p in db.posts:
        if p["author"] == username:
            result.append(enrich_post(p, current_user))
    return sorted(result, key=lambda x: x["timestamp"], reverse=True)

@app.get("/api/notifications")
async def get_notifications(session_token: Optional[str] = Cookie(None)):
    username = get_current_user(session_token)
    if not username:
        raise HTTPException(status_code=401, detail="ERR_UNAUTHORIZED")
    my_post_ids = {p["id"]: p for p in db.posts if p["author"] == username}
    notifs = []
    for c in db.comments:
        if c["post_id"] in my_post_ids and c["author"] != username:
            post = my_post_ids[c["post_id"]]
            author_data = db.users.get(c["author"], {})
            notifs.append({
                "kind": "comment", "id": c["id"], "post_id": c["post_id"],
                "post_preview": post["content"][:100],
                "actor": c["author"],
                "actor_display_name": author_data.get("display_name", c["author"]),
                "content": c["content"], "timestamp": c["timestamp"]
            })
    for pid, post in my_post_ids.items():
        for voter, vote_type in db.votes.get(pid, {}).items():
            if voter != username:
                voter_data = db.users.get(voter, {})
                notifs.append({
                    "kind": "vote", "id": f"{pid}_{voter}", "post_id": pid,
                    "post_preview": post["content"][:100],
                    "actor": voter,
                    "actor_display_name": voter_data.get("display_name", voter),
                    "vote_type": vote_type, "timestamp": post["timestamp"]
                })
    notifs.sort(key=lambda x: x["timestamp"], reverse=True)
    return notifs

@app.get("/api/search")
async def search(q: str = "", session_token: Optional[str] = Cookie(None)):
    current_user = get_current_user(session_token)
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
    posts_result = [enrich_post(p, current_user) for p in db.posts if q_lower in p["content"].lower()]
    comments_result = []
    for comment in db.comments:
        if q_lower in comment["content"].lower():
            votes = db.votes.get(comment["id"], {})
            upvotes = sum(1 for v in votes.values() if v == "up")
            downvotes = sum(1 for v in votes.values() if v == "down")
            user_vote = votes.get(current_user) if current_user else None
            author_data = db.users.get(comment["author"], {})
            post_author = ""
            for p in db.posts:
                if p["id"] == comment["post_id"]:
                    post_author = p["author"]
                    break
            comments_result.append({
                **comment,
                "display_name": author_data.get("display_name", comment["author"]),
                "post_author": post_author,
                "upvotes": upvotes, "downvotes": downvotes, "user_vote": user_vote
            })
    return {"query": q, "users": users_result, "posts": posts_result, "comments": comments_result}

FAVICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#6364ff"/><path d="M11 8v16h10v-3h-7V8z" fill="#fff"/></svg>'''

@app.get("/favicon.svg")
async def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


MAIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Litodon</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
:root, [data-theme="dark"] {
    --bg: #191b22; --bg-secondary: #282c37; --bg-hover: #313543; --bg-input: #191b22;
    --border: #393f4f; --border-light: #2c313d;
    --text: #d9e1e8; --text-heading: #ffffff; --text-muted: #7d869a;
    --accent: #6364ff; --accent-hover: #5051db; --accent-text: #ffffff;
    --danger: #df405a; --danger-hover: #c63550;
    --upvote: #ff6b35; --downvote: #7193ff;
    --shadow: 0 4px 16px rgba(0,0,0,0.35); --shadow-sm: 0 2px 6px rgba(0,0,0,0.2);
    --topbar-h: 60px; --botnav-h: 64px;
}
[data-theme="light"] {
    --bg: #f4f6f9; --bg-secondary: #ffffff; --bg-hover: #f0f2f5; --bg-input: #ffffff;
    --border: #d8dee6; --border-light: #e8edf2;
    --text: #2e3440; --text-heading: #191b22; --text-muted: #6b7381;
    --accent: #6364ff; --accent-hover: #5051db; --accent-text: #ffffff;
    --danger: #d93025; --danger-hover: #b5261d;
    --upvote: #e8501a; --downvote: #4267c9;
    --shadow: 0 4px 16px rgba(0,0,0,0.08); --shadow-sm: 0 2px 6px rgba(0,0,0,0.05);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    height: 100vh; overflow: hidden; font-size: 15px; line-height: 1.5;
    transition: background 0.25s, color 0.25s;
    -webkit-user-select: none; -moz-user-select: none; user-select: none;
    -webkit-tap-highlight-color: transparent;
}
input, textarea, select { -webkit-user-select: text; -moz-user-select: text; user-select: text; }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 5px; }

/* --- App layout --- */
.app {
    display: flex; flex-direction: column;
    height: 100vh; max-width: 100%;
}
.topbar {
    flex-shrink: 0; background: var(--bg);
    border-bottom: 1px solid var(--border);
    z-index: 20;
}
.topbar-inner {
    max-width: 800px; margin: 0 auto;
    display: flex; align-items: center; gap: 12px;
    padding: 10px 16px; height: var(--topbar-h);
}
.brand {
    display: flex; align-items: center; gap: 8px;
    cursor: pointer; text-decoration: none; flex-shrink: 0;
}
.brand-icon {
    width: 34px; height: 34px; background: var(--accent); border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    box-shadow: var(--shadow-sm); flex-shrink: 0;
}
.brand-icon svg { width: 20px; height: 20px; }
.brand-name {
    font-size: 18px; font-weight: 700; color: var(--text-heading);
    letter-spacing: -0.3px;
}
.search-wrap { position: relative; flex: 1; min-width: 0; }
.search-wrap svg {
    position: absolute; left: 12px; top: 50%; transform: translateY(-50%);
    width: 16px; height: 16px; fill: var(--text-muted); pointer-events: none;
}
.search-input {
    width: 100%; background: var(--bg-secondary);
    border: 1px solid var(--border); border-radius: 20px;
    padding: 9px 16px 9px 38px; color: var(--text);
    font-size: 14px; outline: none; font-family: inherit;
    transition: border 0.2s, background 0.2s;
}
.search-input:focus { border-color: var(--accent); background: var(--bg-input); }
.search-input::placeholder { color: var(--text-muted); }

.content {
    flex: 1; overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    position: relative;
}
.content-inner { max-width: 800px; margin: 0 auto; padding-bottom: 20px; }

/* --- Bottom navigation --- */
.bottomnav {
    flex-shrink: 0; background: var(--bg);
    border-top: 1px solid var(--border);
    z-index: 20;
}
.bottomnav-inner {
    max-width: 800px; margin: 0 auto;
    display: flex; align-items: stretch;
    height: var(--botnav-h);
}
.bn-btn {
    flex: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 2px;
    background: transparent; border: none; cursor: pointer;
    color: var(--text-muted); font-family: inherit;
    font-size: 11px; font-weight: 500;
    padding: 8px 4px; transition: color 0.15s;
    position: relative;
}
.bn-btn:hover { color: var(--text); }
.bn-btn.active { color: var(--accent); }
.bn-btn svg { width: 22px; height: 22px; fill: currentColor; }
.bn-btn span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
.bn-btn.logout-btn-nav { color: var(--danger); }
.bn-btn.logout-btn-nav:hover { color: var(--danger-hover); }
.bn-icon-wrap { position: relative; display: flex; }
.bn-badge {
    position: absolute; top: -4px; right: -8px;
    background: var(--danger); color: white;
    font-size: 10px; font-weight: 700;
    padding: 1px 5px; border-radius: 8px;
    min-width: 16px; text-align: center; line-height: 14px;
}

/* --- Loading --- */
.loading-overlay {
    position: fixed; inset: 0; background: var(--bg);
    display: flex; align-items: center; justify-content: center;
    z-index: 9999; opacity: 1; visibility: visible;
    transition: opacity 0.35s, visibility 0.35s;
}
.loading-overlay.hidden { opacity: 0; visibility: hidden; pointer-events: none; }
.spinner {
    width: 44px; height: 44px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* --- Views --- */
.view { padding: 16px; }
.view.hidden { display: none !important; }

/* --- Auth --- */
.auth-wrap {
    max-width: 380px; margin: 20px auto; padding: 0 16px;
}
.auth-title {
    font-size: 22px; font-weight: 700; color: var(--text-heading);
    margin-bottom: 20px; text-align: center;
}
.auth-switch {
    text-align: center; margin-top: 18px; font-size: 14px; color: var(--text-muted);
}
.auth-switch a {
    color: var(--accent); cursor: pointer; text-decoration: none; font-weight: 500;
}
.auth-switch a:hover { text-decoration: underline; }

/* --- Buttons --- */
.btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    padding: 10px 18px; border-radius: 8px;
    font-size: 14px; font-weight: 600; font-family: inherit;
    cursor: pointer; border: none;
    transition: background 0.15s, transform 0.05s; text-decoration: none;
}
.btn:active { transform: scale(0.98); }
.btn-primary { background: var(--accent); color: var(--accent-text); }
.btn-primary:hover { background: var(--accent-hover); }
.btn-secondary { background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); }
.btn-secondary:hover { background: var(--bg-hover); }
.btn-danger { background: var(--danger); color: white; }
.btn-danger:hover { background: var(--danger-hover); }
.btn-full { width: 100%; }
.btn-sm { padding: 6px 12px; font-size: 13px; }

/* --- Form --- */
.form-group { margin-bottom: 14px; }
.form-label {
    display: block; font-size: 13px; font-weight: 500;
    color: var(--text-muted); margin-bottom: 6px;
}
.form-input, .form-textarea, .form-select {
    width: 100%; background: var(--bg-input);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 12px; color: var(--text); font-size: 14px;
    font-family: inherit; outline: none;
    transition: border 0.15s, box-shadow 0.15s; resize: none;
}
.form-input:focus, .form-textarea:focus, .form-select:focus {
    border-color: var(--accent); box-shadow: 0 0 0 3px rgba(99,100,255,0.15);
}
.form-input::placeholder, .form-textarea::placeholder { color: var(--text-muted); }
.form-textarea { min-height: 70px; line-height: 1.5; }

.error-box {
    background: rgba(223,64,90,0.1); border: 1px solid var(--danger);
    color: var(--danger); padding: 10px 12px; border-radius: 8px;
    font-size: 13px; margin-bottom: 16px;
}

/* --- Compose --- */
.compose {
    padding: 16px; border-bottom: 1px solid var(--border);
    display: flex; gap: 12px; background: var(--bg-secondary);
}
.compose-body { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.compose-textarea {
    width: 100%; background: transparent; border: none; color: var(--text);
    font-family: inherit; font-size: 15px; resize: none; outline: none;
    min-height: 56px; padding: 4px 0;
}
.compose-textarea::placeholder { color: var(--text-muted); }
.compose-actions {
    display: flex; justify-content: space-between; align-items: center;
    border-top: 1px solid var(--border-light); padding-top: 12px; margin-top: 8px;
}
.compose-icons { display: flex; gap: 14px; color: var(--text-muted); }
.compose-icons svg { width: 18px; height: 18px; fill: currentColor; cursor: pointer; transition: color 0.15s; }
.compose-icons svg:hover { color: var(--accent); }

/* --- Feed --- */
.feed { padding: 16px; }
.empty-state {
    text-align: center; color: var(--text-muted);
    padding: 40px 20px; font-size: 14px;
}
.post {
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px; margin-bottom: 12px;
    display: flex; gap: 10px;
    transition: border 0.15s, box-shadow 0.15s;
}
.post:hover { border-color: var(--text-muted); box-shadow: var(--shadow-sm); }
.vote-col {
    display: flex; flex-direction: column; align-items: center; gap: 2px;
    flex-shrink: 0; min-width: 32px;
}
.vote-btn {
    background: transparent; border: none; cursor: pointer;
    color: var(--text-muted); padding: 4px; border-radius: 6px;
    display: flex; align-items: center; justify-content: center;
    transition: color 0.15s, background 0.15s;
}
.vote-btn:hover { background: var(--bg-hover); color: var(--text); }
.vote-btn.up:hover, .vote-btn.up.active { color: var(--upvote); }
.vote-btn.down:hover, .vote-btn.down.active { color: var(--downvote); }
.vote-btn svg { width: 18px; height: 18px; fill: currentColor; }
.vote-count {
    font-size: 13px; font-weight: 700; color: var(--text);
    line-height: 1; padding: 2px 0;
}
.vote-count.up { color: var(--upvote); }
.vote-count.down { color: var(--downvote); }
.post-body { flex: 1; min-width: 0; }
.post-head {
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 6px; flex-wrap: wrap;
}
.post-author {
    font-size: 14px; font-weight: 700; color: var(--text-heading);
    cursor: pointer; transition: color 0.15s;
}
.post-author:hover { color: var(--accent); text-decoration: underline; }
.post-handle { color: var(--text-muted); font-size: 13px; cursor: pointer; }
.post-handle:hover { color: var(--accent); }
.post-date { color: var(--text-muted); font-size: 12px; margin-left: auto; }
.post-content {
    font-size: 15px; white-space: pre-wrap; word-break: break-word;
    margin-bottom: 8px; cursor: pointer; color: var(--text);
}
.post-content:hover { color: var(--accent); }
.post-actions { display: flex; gap: 16px; font-size: 13px; color: var(--text-muted); }
.post-action {
    display: inline-flex; align-items: center; gap: 6px;
    cursor: pointer; background: none; border: none; color: inherit;
    font-family: inherit; font-size: 13px;
    padding: 4px 8px; border-radius: 6px;
    transition: background 0.15s, color 0.15s;
}
.post-action:hover { background: var(--bg-hover); color: var(--accent); }
.post-action svg { width: 16px; height: 16px; fill: currentColor; }

/* --- Comments --- */
.comment {
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px; margin-bottom: 10px;
    display: flex; gap: 10px;
}
.comment-body { flex: 1; min-width: 0; }
.comment-head {
    display: flex; align-items: baseline; gap: 8px;
    margin-bottom: 6px; flex-wrap: wrap;
}
.comment-author {
    font-size: 13px; font-weight: 700; color: var(--text-heading);
    cursor: pointer;
}
.comment-author:hover { color: var(--accent); text-decoration: underline; }
.comment-handle { color: var(--text-muted); font-size: 12px; cursor: pointer; }
.comment-handle:hover { color: var(--accent); }
.comment-date { color: var(--text-muted); font-size: 12px; margin-left: auto; }
.comment-content { font-size: 14px; white-space: pre-wrap; word-break: break-word; }

/* --- Profile --- */
.profile-head { padding: 20px 16px; border-bottom: 1px solid var(--border); }
.profile-top {
    display: flex; justify-content: space-between; align-items: flex-start;
    gap: 12px; margin-bottom: 12px; flex-wrap: wrap;
}
.profile-name { font-size: 20px; font-weight: 700; color: var(--text-heading); line-height: 1.2; }
.profile-handle { font-size: 14px; color: var(--text-muted); margin-top: 2px; }
.profile-bio {
    font-size: 14px; color: var(--text); white-space: pre-wrap;
    margin-bottom: 12px; line-height: 1.6;
}
.profile-stats {
    display: flex; gap: 20px; font-size: 13px; color: var(--text-muted);
}
.profile-stats strong { color: var(--text-heading); }

/* --- Search results --- */
.search-results-wrap { padding: 16px; }
.search-query-head {
    font-size: 14px; color: var(--text-muted); margin-bottom: 20px;
    padding-bottom: 14px; border-bottom: 1px solid var(--border);
}
.search-query-head strong { color: var(--text-heading); font-size: 16px; }
.search-section { margin-bottom: 24px; }
.search-section-title {
    font-size: 12px; font-weight: 700; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px;
}
.search-user-card {
    display: flex; align-items: center; gap: 12px;
    padding: 12px 14px; background: var(--bg-secondary);
    border: 1px solid var(--border); border-radius: 10px;
    margin-bottom: 8px; cursor: pointer;
    transition: border 0.15s;
}
.search-user-card:hover { border-color: var(--accent); }
.search-user-avatar {
    width: 36px; height: 36px; background: var(--accent); border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    color: white; font-weight: 700; font-size: 16px; flex-shrink: 0;
}
.search-user-info { flex: 1; min-width: 0; }
.search-user-name { font-weight: 700; color: var(--text-heading); font-size: 14px; }
.search-user-handle { color: var(--text-muted); font-size: 13px; }
.search-user-bio { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
.search-comment-context { font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
.search-comment-context a { color: var(--accent); cursor: pointer; text-decoration: none; }

/* --- Back link --- */
.back-link {
    display: inline-flex; align-items: center; gap: 8px;
    color: var(--text-muted); font-size: 13px; cursor: pointer;
    padding: 6px 10px; border-radius: 6px;
    background: none; border: none; font-family: inherit;
    margin: 12px 16px 0;
    transition: color 0.15s, background 0.15s;
}
.back-link:hover { color: var(--text); background: var(--bg-secondary); }
.back-link svg { width: 16px; height: 16px; fill: currentColor; }
.comments-section { padding: 16px; border-top: 1px solid var(--border); }
.comments-section-title {
    font-size: 15px; font-weight: 700; color: var(--text-heading);
    margin-bottom: 14px;
}
.comment-form { margin-bottom: 20px; }
.comment-form .form-textarea { min-height: 60px; margin-bottom: 8px; }

/* --- Settings --- */
.settings-wrap { padding: 20px 16px; max-width: 520px; margin: 0 auto; }
.settings-section {
    padding-bottom: 20px; margin-bottom: 20px;
    border-bottom: 1px solid var(--border);
}
.settings-section:last-child { border-bottom: none; }
.settings-section-title {
    font-size: 15px; font-weight: 700; color: var(--text-heading);
    margin-bottom: 12px;
}
.settings-doc-link {
    display: flex; align-items: center; gap: 10px;
    padding: 12px 14px; background: var(--bg-secondary);
    border: 1px solid var(--border); border-radius: 8px;
    color: var(--text); font-size: 14px; font-family: inherit;
    text-decoration: none; margin-bottom: 8px;
    transition: border 0.15s, background 0.15s;
    cursor: pointer;
}
.settings-doc-link:hover { border-color: var(--accent); background: var(--bg-hover); }
.settings-doc-link svg { width: 18px; height: 18px; fill: var(--text-muted); flex-shrink: 0; }
.settings-doc-link:hover svg { fill: var(--accent); }

/* --- Notifications --- */
.notifications-wrap { padding: 16px; }
.notification-card {
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px; margin-bottom: 10px;
    display: flex; gap: 12px; cursor: pointer;
    transition: border 0.15s, box-shadow 0.15s;
}
.notification-card:hover { border-color: var(--accent); box-shadow: var(--shadow-sm); }
.notification-icon {
    width: 32px; height: 32px; border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
}
.notification-icon.comment { background: rgba(99,100,255,0.15); color: var(--accent); }
.notification-icon.upvote { background: rgba(255,107,53,0.15); color: var(--upvote); }
.notification-icon.downvote { background: rgba(113,147,255,0.15); color: var(--downvote); }
.notification-icon svg { width: 18px; height: 18px; fill: currentColor; }
.notification-body { flex: 1; min-width: 0; }
.notification-head { font-size: 13px; color: var(--text); margin-bottom: 4px; line-height: 1.5; }
.notification-actor { font-weight: 700; color: var(--text-heading); cursor: pointer; }
.notification-actor:hover { color: var(--accent); text-decoration: underline; }
.notification-time { color: var(--text-muted); font-size: 12px; margin-left: 6px; }
.notification-preview {
    font-size: 13px; color: var(--text-muted);
    padding: 8px 10px; background: var(--bg); border-radius: 6px;
    margin-top: 6px; border-left: 3px solid var(--border);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* --- Modal --- */
.modal-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.7);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000; padding: 20px;
    opacity: 0; visibility: hidden;
    transition: opacity 0.2s, visibility 0.2s;
}
.modal-overlay.visible { opacity: 1; visibility: visible; }
.modal {
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: 10px; width: 100%; max-width: 400px;
    padding: 22px; box-shadow: var(--shadow);
    transform: scale(0.95); transition: transform 0.2s;
}
.modal-overlay.visible .modal { transform: scale(1); }
.modal h3 { font-size: 18px; font-weight: 700; color: var(--text-heading); margin-bottom: 8px; }
.modal p { font-size: 14px; color: var(--text-muted); margin-bottom: 18px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; }

.hidden { display: none !important; }

/* --- Mobile adjustments --- */
@media (max-width: 640px) {
    .brand-name { display: none; }
    .view { padding: 12px; }
    .feed { padding: 12px; }
    .profile-head { padding: 16px 12px; }
    .bn-btn span { font-size: 10px; }
    .bn-btn svg { width: 20px; height: 20px; }
    .post { padding: 12px; }
    .search-input { padding: 8px 14px 8px 36px; font-size: 15px; }
}
</style>
</head>
<body>

<div id="loading-overlay" class="loading-overlay">
    <div class="spinner"></div>
</div>

<div class="app">
    <header class="topbar">
        <div class="topbar-inner">
            <a class="brand" onclick="navigate('/')">
                <div class="brand-icon">
                    <svg viewBox="0 0 32 32"><path d="M11 8v16h10v-3h-7V8z" fill="#fff"/></svg>
                </div>
                <span class="brand-name">Litodon</span>
            </a>
            <div class="search-wrap">
                <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
                <input type="text" id="search-input" class="search-input" placeholder="Поиск">
            </div>
        </div>
    </header>

    <main class="content" id="content">
        <div class="content-inner">

            <!-- LOGIN VIEW -->
            <div id="view-login" class="view hidden">
                <div class="auth-wrap">
                    <div id="login-error" class="error-box hidden"></div>
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
                        <span data-t="no_account">Нет аккаунта?</span> <a onclick="navigate('/register')" data-t="register_link">Зарегистрироваться</a>
                    </div>
                </div>
            </div>

            <!-- REGISTER VIEW -->
            <div id="view-register" class="view hidden">
                <div class="auth-wrap">
                    <div id="register-error" class="error-box hidden"></div>
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
                        <span data-t="have_account">Уже есть аккаунт?</span> <a onclick="navigate('/login')" data-t="login_link">Войти</a>
                    </div>
                </div>
            </div>

            <!-- HOME VIEW -->
            <div id="view-home" class="view hidden" style="padding: 0;">
                <div class="compose">
                    <div class="compose-body">
                        <textarea id="post-content" class="compose-textarea" placeholder="Что нового?"></textarea>
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

            <!-- POST VIEW -->
            <div id="view-post" class="view hidden" style="padding: 0;">
                <button class="back-link" onclick="goBack()">
                    <svg viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
                    <span data-t="back">Назад</span>
                </button>
                <div style="padding: 12px 16px 0;" id="post-detail"></div>
                <div class="comments-section">
                    <div class="comments-section-title" data-t="comments">Комментарии</div>
                    <div class="comment-form">
                        <textarea id="comment-content" class="form-textarea" placeholder="Написать комментарий..."></textarea>
                        <button class="btn btn-primary btn-sm" onclick="submitComment()" data-t="send_comment">Отправить</button>
                    </div>
                    <div id="comments-list"></div>
                </div>
            </div>

            <!-- PROFILE VIEW -->
            <div id="view-profile" class="view hidden" style="padding: 0;">
                <button class="back-link" id="profile-back" onclick="goBack()" style="display:none;">
                    <svg viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
                    <span data-t="back">Назад</span>
                </button>
                <div class="profile-head" id="profile-info"></div>
                <div class="feed" id="profile-feed"></div>
            </div>

            <!-- SETTINGS VIEW -->
            <div id="view-settings" class="view hidden">
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
                    <div class="settings-section">
                        <div class="settings-section-title" data-t="docs_label">Документы</div>
                        <a href="/privacy" target="_blank" class="settings-doc-link">
                            <svg viewBox="0 0 24 24"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4z"/></svg>
                            <span data-t="privacy">Политика конфиденциальности</span>
                        </a>
                        <a href="/changelogs" target="_blank" class="settings-doc-link">
                            <svg viewBox="0 0 24 24"><path d="M13 3c-4.97 0-9 4.03-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42C8.27 19.99 10.51 21 13 21c4.97 0 9-4.03 9-9s-4.03-9-9-9z"/></svg>
                            <span data-t="changelogs">История изменений</span>
                        </a>
                    </div>
                </div>
            </div>

            <!-- NOTIFICATIONS VIEW -->
            <div id="view-notifs" class="view hidden" style="padding: 0;">
                <div class="notifications-wrap" id="notifications-list"></div>
            </div>

            <!-- SEARCH VIEW -->
            <div id="view-search" class="view hidden" style="padding: 0;">
                <div class="search-results-wrap">
                    <div class="search-query-head">
                        <span data-t="search_results_for">Результаты поиска:</span> <strong id="search-query-display"></strong>
                    </div>
                    <div class="search-section">
                        <div class="search-section-title" data-t="search_users">Пользователи</div>
                        <div id="search-users-list"></div>
                    </div>
                    <div class="search-section">
                        <div class="search-section-title" data-t="search_posts">Посты</div>
                        <div id="search-posts-list"></div>
                    </div>
                    <div class="search-section">
                        <div class="search-section-title" data-t="search_comments">Комментарии</div>
                        <div id="search-comments-list"></div>
                    </div>
                </div>
            </div>

        </div>
    </main>

    <nav class="bottomnav" id="bottomnav">
        <div class="bottomnav-inner">
            <button class="bn-btn" id="bn-home" onclick="navigate('/')">
                <svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
                <span data-t="nav_home">Главная</span>
            </button>
            <button class="bn-btn" id="bn-notifs" onclick="navigate('/notifs')">
                <div class="bn-icon-wrap">
                    <svg viewBox="0 0 24 24"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"/></svg>
                    <span class="bn-badge hidden" id="bn-badge">0</span>
                </div>
                <span data-t="nav_notifications">Уведомления</span>
            </button>
            <button class="bn-btn" id="bn-profile" onclick="gotoOwnProfile()">
                <svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
                <span data-t="nav_profile">Профиль</span>
            </button>
            <button class="bn-btn" id="bn-settings" onclick="navigate('/settings')">
                <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.04.24.24.41.48.41h3.84c.24 0 .43-.17.47-.41l.36-2.54c.59-.24 1.13-.57 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>
                <span data-t="nav_settings">Настройки</span>
            </button>
            <button class="bn-btn logout-btn-nav hidden" id="bn-logout" onclick="openLogoutModal()">
                <svg viewBox="0 0 24 24"><path d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg>
                <span data-t="logout_btn">Выйти</span>
            </button>
        </div>
    </nav>
</div>

<!-- LOGOUT MODAL -->
<div id="logout-modal" class="modal-overlay">
    <div class="modal">
        <h3 data-t="logout_confirm_title">Выйти из аккаунта?</h3>
        <p data-t="logout_confirm_text">Вы уверены, что хотите выйти? Вам придётся ввести пароль снова, чтобы войти.</p>
        <div class="modal-actions">
            <button class="btn btn-secondary btn-sm" onclick="closeLogoutModal()" data-t="cancel">Отмена</button>
            <button class="btn btn-danger btn-sm" onclick="confirmLogout()" data-t="logout_btn">Выйти</button>
        </div>
    </div>
</div>

<script>
const translations = {
    ru: {
        search_placeholder: "Поиск",
        privacy: "Политика конфиденциальности",
        changelogs: "История изменений",
        docs_label: "Документы",
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
        nav_notifications: "Уведомления",
        logout_btn: "Выйти",
        logout_confirm_title: "Выйти из аккаунта?",
        logout_confirm_text: "Вы уверены, что хотите выйти? Вам придётся ввести пароль снова, чтобы войти.",
        cancel: "Отмена",
        edit_profile: "Редактировать",
        save: "Сохранить",
        edit_name_label: "Отображаемое имя",
        edit_bio_label: "Описание профиля",
        no_bio: "Нет описания",
        no_posts: "Пока нет постов.",
        no_comments: "Нет комментариев. Будьте первым!",
        no_results: "Ничего не найдено.",
        no_notifications: "Пока нет уведомлений.",
        search_results_for: "Результаты поиска:",
        search_users: "Пользователи",
        search_posts: "Посты",
        search_comments: "Комментарии",
        post_context: "Комментарий к посту от",
        followers: "подписчиков",
        following: "подписок",
        follow: "Подписаться",
        unfollow: "Отписаться",
        notif_commented: "оставил комментарий под вашим постом",
        notif_upvoted: "проголосовал за ваш пост",
        notif_downvoted: "проголосовал против вашего поста",
        ERR_USERNAME_TAKEN: "Этот ник уже занят",
        ERR_FILL_ALL: "Заполните все обязательные поля",
        ERR_PASSWORDS_MISMATCH: "Пароли не совпадают",
        ERR_PASSWORD_SHORT: "Пароль должен быть не менее 4 символов",
        ERR_INVALID_CREDENTIALS: "Неверный ник или пароль",
        ERR_UNAUTHORIZED: "Требуется авторизация",
        ERR_EMPTY_POST: "Пост не может быть пустым",
        ERR_EMPTY_COMMENT: "Комментарий не может быть пустым",
        ERR_POST_NOT_FOUND: "Пост не найден",
        ERR_USER_NOT_FOUND: "Пользователь не найден",
        ERR_CANNOT_FOLLOW_SELF: "Нельзя подписаться на себя",
        ERR_UNKNOWN: "Произошла ошибка"
    },
    en: {
        search_placeholder: "Search",
        privacy: "Privacy policy",
        changelogs: "Changelog",
        docs_label: "Documents",
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
        nav_notifications: "Notifications",
        logout_btn: "Logout",
        logout_confirm_title: "Log out?",
        logout_confirm_text: "Are you sure you want to log out? You'll need to enter your password again to sign in.",
        cancel: "Cancel",
        edit_profile: "Edit",
        save: "Save",
        edit_name_label: "Display name",
        edit_bio_label: "Profile bio",
        no_bio: "No bio",
        no_posts: "No posts yet.",
        no_comments: "No comments yet. Be the first!",
        no_results: "Nothing found.",
        no_notifications: "No notifications yet.",
        search_results_for: "Search results:",
        search_users: "Users",
        search_posts: "Posts",
        search_comments: "Comments",
        post_context: "Comment on post by",
        followers: "followers",
        following: "following",
        follow: "Follow",
        unfollow: "Unfollow",
        notif_commented: "commented on your post",
        notif_upvoted: "upvoted your post",
        notif_downvoted: "downvoted your post",
        ERR_USERNAME_TAKEN: "This username is taken",
        ERR_FILL_ALL: "Please fill all required fields",
        ERR_PASSWORDS_MISMATCH: "Passwords do not match",
        ERR_PASSWORD_SHORT: "Password must be at least 4 characters",
        ERR_INVALID_CREDENTIALS: "Invalid username or password",
        ERR_UNAUTHORIZED: "Authorization required",
        ERR_EMPTY_POST: "Post cannot be empty",
        ERR_EMPTY_COMMENT: "Comment cannot be empty",
        ERR_POST_NOT_FOUND: "Post not found",
        ERR_USER_NOT_FOUND: "User not found",
        ERR_CANNOT_FOLLOW_SELF: "You cannot follow yourself",
        ERR_UNKNOWN: "An error occurred"
    }
};

let currentLang = localStorage.getItem('litodon_lang') || 'ru';
let currentTheme = localStorage.getItem('litodon_theme') || 'dark';
let currentUser = null;
let currentPostId = null;
let currentViewedUsername = null;
let isEditingProfile = false;
let previousPath = '/';

function t(key) { return (translations[currentLang] && translations[currentLang][key]) || key; }

function applyTranslations() {
    document.querySelectorAll('[data-t]').forEach(el => {
        const key = el.getAttribute('data-t');
        el.textContent = t(key);
    });
    const si = document.getElementById('search-input');
    if (si) si.placeholder = t('search_placeholder');
    const pc = document.getElementById('post-content');
    if (pc) pc.placeholder = t('post_placeholder');
    const cc = document.getElementById('comment-content');
    if (cc) cc.placeholder = t('comment_placeholder');
    const langSel = document.getElementById('settings-language');
    if (langSel) langSel.value = currentLang;
    const themeSel = document.getElementById('settings-theme');
    if (themeSel) themeSel.value = currentTheme;
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
    handleRoute(true);
}

function translateError(code) { return t(code) || t('ERR_UNKNOWN'); }
function hideLoading() {
    const ov = document.getElementById('loading-overlay');
    if (ov) ov.classList.add('hidden');
}

function navigate(path, replace) {
    if (window.location.pathname === path && !replace) { handleRoute(); return; }
    if (replace) history.replaceState({}, '', path);
    else history.pushState({}, '', path);
    handleRoute();
}

function goBack() {
    if (window.history.length > 1) history.back();
    else navigate('/');
}

window.addEventListener('popstate', () => handleRoute());

function hideAllViews() {
    ['login','register','home','post','profile','settings','notifs','search'].forEach(v => {
        const el = document.getElementById('view-' + v);
        if (el) el.classList.add('hidden');
    });
    ['bn-home','bn-notifs','bn-profile','bn-settings','bn-logout'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.remove('active');
    });
}

function setActiveNav(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('active');
}

function updateBottomNavVisibility() {
    const bnLogout = document.getElementById('bn-logout');
    const bnNotifs = document.getElementById('bn-notifs');
    const bnProfile = document.getElementById('bn-profile');
    const bnSettings = document.getElementById('bn-settings');
    const bnHome = document.getElementById('bn-home');
    if (currentUser) {
        bnLogout.classList.remove('hidden');
        bnNotifs.classList.remove('hidden');
        bnProfile.classList.remove('hidden');
        bnSettings.classList.remove('hidden');
        bnHome.classList.remove('hidden');
    } else {
        bnLogout.classList.add('hidden');
        bnNotifs.classList.add('hidden');
        bnProfile.classList.add('hidden');
        bnSettings.classList.add('hidden');
        bnHome.classList.add('hidden');
    }
}

async function handleRoute(skipAuthCheck) {
    if (!skipAuthCheck) {
        const path = window.location.pathname;
        // If already initialized, just render
    }
    const path = window.location.pathname;
    hideAllViews();
    updateBottomNavVisibility();

    if (path === '/login' || path === '/register') {
        if (currentUser) { navigate('/', true); return; }
        if (path === '/login') {
            document.getElementById('view-login').classList.remove('hidden');
        } else {
            document.getElementById('view-register').classList.remove('hidden');
        }
        return;
    }

    if (!currentUser) {
        navigate('/login', true);
        return;
    }

    if (path === '/' || path === '') {
        document.getElementById('view-home').classList.remove('hidden');
        setActiveNav('bn-home');
        loadHomeFeed();
    } else if (path === '/notifs') {
        document.getElementById('view-notifs').classList.remove('hidden');
        setActiveNav('bn-notifs');
        loadNotifications();
    } else if (path === '/settings') {
        document.getElementById('view-settings').classList.remove('hidden');
        setActiveNav('bn-settings');
    } else if (path.startsWith('/post/')) {
        currentPostId = path.slice(6);
        document.getElementById('view-post').classList.remove('hidden');
        loadPostDetail();
    } else if (path.startsWith('/@')) {
        currentViewedUsername = decodeURIComponent(path.slice(2));
        document.getElementById('view-profile').classList.remove('hidden');
        if (currentViewedUsername === currentUser.username) {
            setActiveNav('bn-profile');
        }
        loadProfile();
    } else {
        navigate('/', true);
    }
}

async function init() {
    applyTheme();
    applyTranslations();
    try {
        const res = await fetch('/api/me');
        const data = await res.json();
        if (data.username) currentUser = data;
        else currentUser = null;
    } catch (e) { currentUser = null; }
    await handleRoute(true);
    if (currentUser) loadNotificationsCount();
    setTimeout(hideLoading, 150);
}

async function loadNotificationsCount() {
    if (!currentUser) return;
    try {
        const res = await fetch('/api/notifications');
        if (!res.ok) return;
        const data = await res.json();
        const badge = document.getElementById('bn-badge');
        if (data.length > 0) {
            badge.textContent = data.length > 99 ? '99+' : data.length;
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    } catch (e) {}
}

// --- AUTH ---
async function submitLogin() {
    const username = document.getElementById('login-username').value;
    const password = document.getElementById('login-password').value;
    const errorDiv = document.getElementById('login-error');
    try {
        const res = await fetch('/api/login', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username, password})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);
        document.getElementById('login-username').value = '';
        document.getElementById('login-password').value = '';
        currentUser = { username: data.username };
        // Fetch full profile
        const me = await fetch('/api/me').then(r => r.json());
        currentUser = me;
        navigate('/');
        loadNotificationsCount();
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
    const errorDiv = document.getElementById('register-error');
    try {
        const res = await fetch('/api/register', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username, display_name, password, confirm_password})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);
        ['reg-display-name','reg-username','reg-password','reg-confirm-password'].forEach(id => document.getElementById(id).value = '');
        const me = await fetch('/api/me').then(r => r.json());
        currentUser = me;
        navigate('/');
    } catch (err) {
        errorDiv.textContent = translateError(err.message);
        errorDiv.classList.remove('hidden');
    }
}

function openLogoutModal() { document.getElementById('logout-modal').classList.add('visible'); }
function closeLogoutModal() { document.getElementById('logout-modal').classList.remove('visible'); }
async function confirmLogout() {
    await fetch('/api/logout', {method: 'POST'});
    closeLogoutModal();
    currentUser = null;
    navigate('/login');
}

// --- HOME ---
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
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content})
    });
    if (res.ok) { ta.value = ''; loadHomeFeed(); }
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
                <span class="post-author" onclick="event.stopPropagation(); navigate('/@${esc(post.author)}')">${esc(post.display_name)}</span>
                <span class="post-handle" onclick="event.stopPropagation(); navigate('/@${esc(post.author)}')">@${esc(post.author)}</span>
                <span class="post-date">${date}</span>
            </div>
            <div class="post-content" onclick="navigate('/post/${post.id}')">${esc(post.content)}</div>
            <div class="post-actions">
                <button class="post-action" onclick="navigate('/post/${post.id}')">
                    <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg>
                    ${t('comments')}
                </button>
            </div>
        </div>
    `;
    return el;
}

// --- POST DETAIL ---
async function loadPostDetail() {
    const res = await fetch('/api/posts/' + currentPostId);
    if (!res.ok) { navigate('/'); return; }
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
                <span class="comment-author" onclick="navigate('/@${esc(c.author)}')">${esc(c.display_name)}</span>
                <span class="comment-handle" onclick="navigate('/@${esc(c.author)}')">@${esc(c.author)}</span>
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
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content})
    });
    if (res.ok) { ta.value = ''; loadComments(); }
}

// --- VOTE ---
async function handleVote(targetId, targetType, voteType) {
    if (!currentUser) return;
    const res = await fetch('/api/vote', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target_id: targetId, target_type: targetType, vote_type: voteType})
    });
    if (res.ok) {
        const path = window.location.pathname;
        if (path.startsWith('/post/')) loadPostDetail();
        else if (path === '/') loadHomeFeed();
        else if (path.startsWith('/@')) loadProfile();
        loadNotificationsCount();
    }
}

// --- PROFILE ---
async function loadProfile() {
    const username = currentViewedUsername;
    if (!username) { navigate('/'); return; }
    const isMe = currentUser && currentUser.username === username;

    const infoDiv = document.getElementById('profile-info');
    const feed = document.getElementById('profile-feed');
    const backBtn = document.getElementById('profile-back');
    if (isMe) backBtn.style.display = 'none';
    else backBtn.style.display = 'inline-flex';

    try {
        const res = await fetch('/api/profile/' + encodeURIComponent(username));
        if (!res.ok) { infoDiv.innerHTML = '<div class="empty-state">' + t('ERR_USER_NOT_FOUND') + '</div>'; feed.innerHTML = ''; return; }
        const user = await res.json();

        if (isMe && isEditingProfile) {
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
            const actionBtn = isMe
                ? `<button class="btn btn-secondary btn-sm" onclick="toggleProfileEdit()">${t('edit_profile')}</button>`
                : `<button class="btn ${user.is_following ? 'btn-secondary' : 'btn-primary'} btn-sm" onclick="toggleFollow('${esc(user.username)}', ${user.is_following})">${user.is_following ? t('unfollow') : t('follow')}</button>`;
            infoDiv.innerHTML = `
                <div class="profile-top">
                    <div>
                        <div class="profile-name">${esc(user.display_name)}</div>
                        <div class="profile-handle">@${esc(user.username)}</div>
                    </div>
                    ${actionBtn}
                </div>
                <div class="profile-bio">${esc(user.bio || t('no_bio'))}</div>
                <div class="profile-stats">
                    <span><strong>${user.followers}</strong> ${t('followers')}</span>
                    <span><strong>${user.following}</strong> ${t('following')}</span>
                </div>
            `;
        }

        const pRes = await fetch('/api/profile/' + encodeURIComponent(username) + '/posts');
        const posts = await pRes.json();
        feed.innerHTML = '';
        if (posts.length === 0) { feed.innerHTML = '<div class="empty-state">' + t('no_posts') + '</div>'; return; }
        posts.forEach(p => feed.appendChild(createPostEl(p)));
    } catch (e) {
        infoDiv.innerHTML = '<div class="empty-state">' + t('ERR_UNKNOWN') + '</div>';
    }
}

function toggleProfileEdit() { isEditingProfile = !isEditingProfile; loadProfile(); }

async function saveProfile() {
    const display_name = document.getElementById('edit-display-name').value;
    const bio = document.getElementById('edit-bio').value;
    const res = await fetch('/api/profile/update', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({display_name, bio})
    });
    if (res.ok) {
        const data = await res.json();
        currentUser.display_name = data.display_name;
        currentUser.bio = data.bio;
        isEditingProfile = false;
        loadProfile();
    }
}

async function toggleFollow(username, isFollowing) {
    const endpoint = isFollowing ? '/api/unfollow/' : '/api/follow/';
    const res = await fetch(endpoint + encodeURIComponent(username), {method: 'POST'});
    if (res.ok) loadProfile();
    else {
        const data = await res.json().catch(() => ({}));
        if (data.detail) alert(translateError(data.detail));
    }
}

function gotoOwnProfile() {
    if (!currentUser) { navigate('/login'); return; }
    navigate('/@' + currentUser.username);
}

// --- NOTIFICATIONS ---
async function loadNotifications() {
    const list = document.getElementById('notifications-list');
    list.innerHTML = '';
    try {
        const res = await fetch('/api/notifications');
        if (!res.ok) throw new Error('unauth');
        const notifs = await res.json();
        if (notifs.length === 0) {
            list.innerHTML = '<div class="empty-state">' + t('no_notifications') + '</div>';
            return;
        }
        notifs.forEach(n => list.appendChild(createNotificationEl(n)));
        const badge = document.getElementById('bn-badge');
        badge.classList.add('hidden');
    } catch (e) {
        list.innerHTML = '<div class="empty-state">' + t('ERR_UNKNOWN') + '</div>';
    }
}

function createNotificationEl(n) {
    const el = document.createElement('div');
    el.className = 'notification-card';
    el.onclick = () => navigate('/post/' + n.post_id);
    const date = new Date(n.timestamp).toLocaleString(currentLang === 'ru' ? 'ru-RU' : 'en-US');
    let iconSvg = '', iconClass = '', textKey = '';
    if (n.kind === 'comment') {
        iconClass = 'comment'; textKey = 'notif_commented';
        iconSvg = '<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>';
    } else if (n.kind === 'vote' && n.vote_type === 'up') {
        iconClass = 'upvote'; textKey = 'notif_upvoted';
        iconSvg = '<svg viewBox="0 0 24 24"><path d="M12 4l-8 8h6v8h4v-8h6z"/></svg>';
    } else if (n.kind === 'vote' && n.vote_type === 'down') {
        iconClass = 'downvote'; textKey = 'notif_downvoted';
        iconSvg = '<svg viewBox="0 0 24 24"><path d="M12 20l8-8h-6V4h-4v8H4z"/></svg>';
    }
    el.innerHTML = `
        <div class="notification-icon ${iconClass}">${iconSvg}</div>
        <div class="notification-body">
            <div class="notification-head">
                <span class="notification-actor" onclick="event.stopPropagation(); navigate('/@${esc(n.actor)}')">${esc(n.actor_display_name)}</span>
                ${t(textKey)}
                <span class="notification-time">${date}</span>
            </div>
            <div class="notification-preview">${esc(n.kind === 'comment' ? n.content : n.post_preview)}</div>
        </div>
    `;
    return el;
}

// --- SEARCH ---
document.getElementById('search-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') {
        const q = e.target.value.trim();
        if (q) performSearch(q);
    }
});

async function performSearch(query) {
    hideAllViews();
    document.getElementById('view-search').classList.remove('hidden');
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
            el.onclick = () => navigate('/@' + u.username);
            el.innerHTML = `
                <div class="search-user-avatar">${esc((u.display_name[0] || '?')).toUpperCase()}</div>
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
                    <div class="search-comment-context">${t('post_context')} <a onclick="navigate('/@${esc(c.post_author)}')">@${esc(c.post_author)}</a></div>
                    <div class="comment-head">
                        <span class="comment-author" onclick="navigate('/@${esc(c.author)}')">${esc(c.display_name)}</span>
                        <span class="comment-handle" onclick="navigate('/@${esc(c.author)}')">@${esc(c.author)}</span>
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

document.addEventListener('contextmenu', e => {
    if (!e.target.closest('input, textarea')) e.preventDefault();
});

document.getElementById('logout-modal').addEventListener('click', e => {
    if (e.target.id === 'logout-modal') closeLogoutModal();
});

init();
</script>
</body>
</html>
"""


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
h1 { font-size: 26px; color: var(--text-heading); margin-bottom: 8px; }
.subtitle { color: var(--text-muted); margin-bottom: 28px; font-size: 14px; }
h2 { font-size: 17px; color: var(--text-heading); margin: 24px 0 10px; }
p { margin-bottom: 12px; }
ul { padding-left: 24px; margin-bottom: 12px; }
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
            <li>Посты, комментарии и голоса</li>
            <li>Технические данные сессии (cookie для авторизации)</li>
        </ul>
        <h2>2. Как мы используем данные</h2>
        <p>Данные используются исключительно для обеспечения работы сервиса.</p>
        <h2>3. Хранение данных</h2>
        <p>Все данные хранятся в оперативной памяти сервера и удаляются при его перезапуске. Мы не передаём данные третьим лицам.</p>
        <h2>4. Cookie</h2>
        <p>Мы используем один сессионный cookie для поддержания авторизации.</p>
        <h2>5. Ваши права</h2>
        <p>Вы можете в любой момент удалить свои посты и комментарии или запросить удаление аккаунта.</p>
    </div>
    <div id="content-en" class="hidden">
        <h1>Privacy Policy</h1>
        <div class="subtitle">Last updated: September 21, 2026</div>
        <p>Litodon respects your privacy. This document describes what data we collect and how we use it.</p>
        <h2>1. Data we collect</h2>
        <ul>
            <li>Username and display name</li>
            <li>Encrypted password</li>
            <li>Posts, comments and votes</li>
            <li>Session technical data (authorization cookie)</li>
        </ul>
        <h2>2. How we use data</h2>
        <p>Data is used solely to operate the service.</p>
        <h2>3. Data storage</h2>
        <p>All data is stored in RAM and is deleted on restart. We do not share data with third parties.</p>
        <h2>4. Cookies</h2>
        <p>We use a single session cookie to maintain authorization.</p>
        <h2>5. Your rights</h2>
        <p>You can delete your posts and comments at any time or request account deletion.</p>
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
h1 { font-size: 26px; color: var(--text-heading); margin-bottom: 8px; }
.subtitle { color: var(--text-muted); margin-bottom: 28px; font-size: 14px; }
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
                <span class="version-tag">v1.2.0</span>
                <span class="version-date">21 сентября 2026</span>
            </div>
            <ul>
                <li>Реальные URL для всех разделов (/login, /notifs, /settings, /post/id, /@nick)</li>
                <li>Подписки на пользователей</li>
                <li>Мобильная адаптация интерфейса</li>
                <li>Меню перемещено вниз, поиск — вверх</li>
                <li>Privacy и Changelog доступны в настройках</li>
            </ul>
        </div>
        <div class="version-block">
            <div class="version-head">
                <span class="version-tag">v1.1.0</span>
                <span class="version-date">21 сентября 2026</span>
            </div>
            <ul>
                <li>Уведомления</li>
                <li>Профили других пользователей</li>
                <li>Спиннер загрузки</li>
            </ul>
        </div>
        <div class="version-block">
            <div class="version-head">
                <span class="version-tag">v1.0.0</span>
                <span class="version-date">21 сентября 2026</span>
            </div>
            <ul>
                <li>Первый публичный релиз Litodon</li>
                <li>Регистрация, посты, комментарии, голосование</li>
                <li>Поиск, редактирование профиля</li>
                <li>RU/EN, тёмная и светлая темы</li>
            </ul>
        </div>
    </div>
    <div id="content-en" class="hidden">
        <h1>Changelog</h1>
        <div class="subtitle">All Litodon updates</div>
        <div class="version-block">
            <div class="version-head">
                <span class="version-tag">v1.2.0</span>
                <span class="version-date">September 21, 2026</span>
            </div>
            <ul>
                <li>Real URLs for all sections (/login, /notifs, /settings, /post/id, /@nick)</li>
                <li>Follow users</li>
                <li>Mobile-responsive interface</li>
                <li>Menu moved to bottom, search to top</li>
                <li>Privacy and Changelog accessible in settings</li>
            </ul>
        </div>
        <div class="version-block">
            <div class="version-head">
                <span class="version-tag">v1.1.0</span>
                <span class="version-date">September 21, 2026</span>
            </div>
            <ul>
                <li>Notifications</li>
                <li>Other users' profiles</li>
                <li>Loading spinner</li>
            </ul>
        </div>
        <div class="version-block">
            <div class="version-head">
                <span class="version-tag">v1.0.0</span>
                <span class="version-date">September 21, 2026</span>
            </div>
            <ul>
                <li>First public release</li>
                <li>Sign up, posts, comments, voting</li>
                <li>Search, profile editing</li>
                <li>RU/EN, dark and light themes</li>
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


@app.get("/privacy", response_class=HTMLResponse)
async def serve_privacy():
    return PRIVACY_HTML

@app.get("/changelogs", response_class=HTMLResponse)
async def serve_changelogs():
    return CHANGELOGS_HTML

@app.get("/{full_path:path}", response_class=HTMLResponse)
async def serve_spa(full_path: str):
    return MAIN_HTML

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
