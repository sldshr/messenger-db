import os
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# --- Конфигурация ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")  # Service Role Key
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL и SUPABASE_KEY должны быть установлены")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
BUCKET_NAME = "post-images"
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
TOKEN_EXPIRE_HOURS = 24 * 30  # 30 дней

app = FastAPI(title="Social Network API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Модели ---
class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None

class PostCreate(BaseModel):
    description: str
    image_urls: List[str] = []

# --- Утилиты ---
def hash_password(password: str) -> str:
    return hashlib.sha256((password + SECRET_KEY).encode()).hexdigest()

def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash

def create_session(user_id: str) -> str:
    """Создаёт случайный токен и сохраняет сессию в БД."""
    token = uuid.uuid4().hex + uuid.uuid4().hex
    expires_at = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    supabase.table("sessions").insert({
        "token": token,
        "user_id": user_id,
        "expires_at": expires_at.isoformat(),
    }).execute()
    return token

def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Неавторизован")
    token = authorization[7:].strip()
    try:
        sess = supabase.table("sessions").select("*").eq("token", token).execute()
    except Exception:
        raise HTTPException(status_code=401, detail="Неверный токен")
    if not sess.data:
        raise HTTPException(status_code=401, detail="Неверный токен")

    session = sess.data[0]
    expires_at = datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00"))
    if expires_at < datetime.now(timezone.utc):
        supabase.table("sessions").delete().eq("token", token).execute()
        raise HTTPException(status_code=401, detail="Сессия истекла, войдите заново")

    user_resp = supabase.table("profiles").select("*").eq("id", session["user_id"]).execute()
    if not user_resp.data:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user_resp.data[0]

# --- Эндпоинты ---
@app.post("/register")
async def register(req: RegisterRequest):
    existing = supabase.table("profiles").select("id").eq("username", req.username).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="Логин уже занят")

    user_id = str(uuid.uuid4())
    password_hash = hash_password(req.password)
    data = {
        "id": user_id,
        "username": req.username,
        "password_hash": password_hash,
        # display_name намеренно не задаём — пользователь укажет его позже в профиле
    }
    supabase.table("profiles").insert(data).execute()

    token = create_session(user_id)
    return {
        "token": token,
        "user_id": user_id,
        "username": req.username,
        "display_name": None,
    }

@app.post("/login")
async def login(req: LoginRequest):
    resp = supabase.table("profiles").select("*").eq("username", req.username).execute()
    if not resp.data:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    user = resp.data[0]
    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = create_session(user["id"])
    return {
        "token": token,
        "user_id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name"),
    }

@app.post("/logout")
async def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        supabase.table("sessions").delete().eq("token", token).execute()
    return {"status": "ok"}

@app.get("/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name"),
        "bio": user.get("bio"),
        "avatar_url": user.get("avatar_url"),
        "created_at": user.get("created_at"),
    }

@app.put("/profile")
async def update_profile(update: ProfileUpdate, user: dict = Depends(get_current_user)):
    data = {k: v for k, v in update.dict().items() if v is not None}
    if not data:
        raise HTTPException(status_code=400, detail="Нет данных для обновления")
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    supabase.table("profiles").update(data).eq("id", user["id"]).execute()
    return {"status": "ok"}

@app.post("/upload")
async def upload_image(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"{user['id']}/{uuid.uuid4()}.{ext}"
    contents = await file.read()
    try:
        supabase.storage.from_(BUCKET_NAME).upload(
            path=filename,
            file=contents,
            file_options={"content-type": file.content_type or "image/jpeg", "upsert": "false"}
        )
        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
        return {"url": public_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка загрузки: {str(e)}")

@app.post("/posts")
async def create_post(post: PostCreate, user: dict = Depends(get_current_user)):
    if len(post.image_urls) > 5:
        raise HTTPException(status_code=400, detail="Максимум 5 изображений")
    post_data = {
        "user_id": user["id"],
        "description": post.description,
    }
    resp = supabase.table("posts").insert(post_data).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Ошибка создания поста")
    post_id = resp.data[0]["id"]
    for idx, url in enumerate(post.image_urls):
        supabase.table("post_images").insert({
            "post_id": post_id,
            "image_url": url,
            "position": idx,
        }).execute()
    return {"post_id": post_id}

@app.get("/posts/{post_id}")
async def get_post(post_id: str, request: Request):
    post_resp = supabase.table("posts").select("*, profiles(username, display_name)").eq("id", post_id).execute()
    if not post_resp.data:
        raise HTTPException(status_code=404, detail="Пост не найден")
    post = post_resp.data[0]
    images_resp = supabase.table("post_images").select("image_url").eq("post_id", post_id).order("position").execute()
    images = [img["image_url"] for img in images_resp.data] if images_resp.data else []

    user_agent = request.headers.get("user-agent", "").lower()
    is_mobile = any(kw in user_agent for kw in ["android", "iphone", "ipad", "mobile"])

    author = post["profiles"].get("display_name") or post["profiles"]["username"]

    if is_mobile:
        return JSONResponse({
            "id": post["id"],
            "author": author,
            "username": post["profiles"]["username"],
            "description": post["description"],
            "images": images,
            "created_at": post["created_at"],
        })
    else:
        desc = post["description"] or ""
        images_html = "".join(f'<img src="{url}" style="max-width:100%; margin:10px 0; border-radius:8px;" />' for url in images)
        html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Пост от {author}</title>
    <style>
        body {{ font-family: Arial, sans-serif; max-width: 700px; margin: 40px auto; padding: 0 20px; color: #333; }}
        h1 {{ font-size: 1.5em; color: #1a1a1a; }}
        .meta {{ color: #666; font-size: 0.9em; margin-bottom: 20px; }}
        .desc {{ font-size: 1.1em; line-height: 1.6; margin: 20px 0; }}
        .content {{ margin-top: 20px; }}
        img {{ display: block; border-radius: 8px; }}
    </style>
</head>
<body>
    <h1>Автор: {author}</h1>
    <div class="meta">@{post['profiles']['username']} · {post['created_at'][:10]}</div>
    <div class="desc"><strong>desc:</strong> {desc}</div>
    <div class="content"><strong>content:</strong><br/>{images_html if images_html else '<em>Нет изображений</em>'}</div>
</body>
</html>"""
        return HTMLResponse(content=html)

@app.get("/")
async def root():
    return {"message": "Social Network API", "status": "running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
