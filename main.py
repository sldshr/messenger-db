import os
import hashlib
import uuid
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL и SUPABASE_KEY должны быть установлены")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
BUCKET_NAME = "post-images"
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
TOKEN_EXPIRE_HOURS = 24 * 30

app = FastAPI(title="Social Network API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _ensure_bucket():
    try:
        buckets = supabase.storage.list_buckets()
        names = []
        for b in buckets:
            names.append(b.get("name") if isinstance(b, dict) else getattr(b, "name", None))
        if BUCKET_NAME not in names:
            supabase.storage.create_bucket(BUCKET_NAME, options={"public": True})
            print(f"[startup] created bucket '{BUCKET_NAME}'")
        else:
            print(f"[startup] bucket '{BUCKET_NAME}' exists")
    except Exception as e:
        print(f"[startup] bucket check failed: {e}")
        traceback.print_exc()


# ---------- Модели ----------
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


# ---------- Утилиты ----------
def hash_password(password: str) -> str:
    return hashlib.sha256((password + SECRET_KEY).encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash


def create_session(user_id: str) -> str:
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


def _fetch_images(post_id: str) -> List[str]:
    resp = supabase.table("post_images").select("image_url").eq("post_id", post_id).order("position").execute()
    return [img["image_url"] for img in resp.data] if resp.data else []


def _esc(s: Optional[str]) -> str:
    if not s:
        return ""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


# ---------- Аутентификация ----------
@app.post("/register")
async def register(req: RegisterRequest):
    existing = supabase.table("profiles").select("id").eq("username", req.username).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="Логин уже занят")

    user_id = str(uuid.uuid4())
    password_hash = hash_password(req.password)
    supabase.table("profiles").insert({
        "id": user_id,
        "username": req.username,
        "password_hash": password_hash,
    }).execute()

    token = create_session(user_id)
    return {"token": token, "user_id": user_id, "username": req.username, "display_name": None}


@app.post("/login")
async def login(req: LoginRequest):
    resp = supabase.table("profiles").select("*").eq("username", req.username).execute()
    if not resp.data:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    user = resp.data[0]
    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = create_session(user["id"])
    return {"token": token, "user_id": user["id"], "username": user["username"],
            "display_name": user.get("display_name")}


@app.post("/logout")
async def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        supabase.table("sessions").delete().eq("token", token).execute()
    return {"status": "ok"}


# ---------- Профиль ----------
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


# ---------- Загрузка ----------
@app.post("/upload")
async def upload_image(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Пустой файл")

    ct = (file.content_type or "").lower()
    if ct not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        name = file.filename or ""
        ext_guess = name.rsplit(".", 1)[-1].lower() if "." in name else "webp"
        ct = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
              "png": "image/png", "webp": "image/webp",
              "gif": "image/gif"}.get(ext_guess, "image/webp")

    ext = {"image/jpeg": "jpg", "image/png": "png",
           "image/webp": "webp", "image/gif": "gif"}.get(ct, "webp")

    filename = f"{user['id']}/{uuid.uuid4().hex}.{ext}"

    try:
        try:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=filename, file=contents,
                file_options={"content-type": ct, "upsert": "false"},
            )
        except Exception as inner:
            if "not found" in str(inner).lower():
                supabase.storage.create_bucket(BUCKET_NAME, options={"public": True})
                supabase.storage.from_(BUCKET_NAME).upload(
                    path=filename, file=contents,
                    file_options={"content-type": ct, "upsert": "false"},
                )
            else:
                raise

        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
        if isinstance(public_url, dict):
            public_url = public_url.get("publicUrl") or public_url.get("public_url") or ""
        return {"url": public_url}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")


# ---------- Посты ----------
@app.post("/posts")
async def create_post(post: PostCreate, user: dict = Depends(get_current_user)):
    if len(post.image_urls) > 5:
        raise HTTPException(status_code=400, detail="Максимум 5 изображений")
    resp = supabase.table("posts").insert({
        "user_id": user["id"], "description": post.description,
    }).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Ошибка создания поста")
    post_id = resp.data[0]["id"]
    for idx, url in enumerate(post.image_urls):
        supabase.table("post_images").insert({
            "post_id": post_id, "image_url": url, "position": idx,
        }).execute()
    return {"post_id": post_id}


@app.get("/my-posts")
async def my_posts(user: dict = Depends(get_current_user)):
    posts_resp = supabase.table("posts").select("*") \
        .eq("user_id", user["id"]).order("created_at", desc=True).execute()
    result = []
    for p in posts_resp.data or []:
        result.append({
            "id": p["id"],
            "description": p.get("description") or "",
            "created_at": p.get("created_at"),
            "images": _fetch_images(p["id"]),
            "share_url": f"https://sldchat.fastapicloud.dev/posts/{p['id']}",
        })
    return {"posts": result}


@app.delete("/posts/{post_id}")
async def delete_post(post_id: str, user: dict = Depends(get_current_user)):
    post_resp = supabase.table("posts").select("*").eq("id", post_id).execute()
    if not post_resp.data:
        raise HTTPException(status_code=404, detail="Пост не найден")
    if post_resp.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет прав на удаление")
    for url in _fetch_images(post_id):
        try:
            marker = f"/object/public/{BUCKET_NAME}/"
            if marker in url:
                supabase.storage.from_(BUCKET_NAME).remove([url.split(marker, 1)[1]])
        except Exception as e:
            print("[delete] storage remove failed:", e)
    supabase.table("posts").delete().eq("id", post_id).execute()
    return {"status": "ok"}


# ---------- JSON для приложения ----------
@app.get("/posts/{post_id}/json")
async def get_post_json(post_id: str):
    resp = supabase.table("posts").select("*, profiles(username, display_name, bio)") \
        .eq("id", post_id).execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Пост не найден")
    post = resp.data[0]
    profile = post.get("profiles") or {}
    return {
        "id": post["id"],
        "author": profile.get("display_name") or profile.get("username") or "",
        "username": profile.get("username") or "",
        "author_bio": profile.get("bio") or "",
        "description": post.get("description") or "",
        "images": _fetch_images(post_id),
        "created_at": post.get("created_at"),
    }


# ---------- HTML для всех (и ПК, и мобильных) ----------
@app.get("/posts/{post_id}")
async def get_post_html(post_id: str):
    resp = supabase.table("posts").select("*, profiles(username, display_name, bio)") \
        .eq("id", post_id).execute()
    if not resp.data:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>Пост не найден</h2></body></html>",
            status_code=404,
        )
    post = resp.data[0]
    profile = post.get("profiles") or {}
    author = profile.get("display_name") or profile.get("username") or "Unknown"
    username = profile.get("username") or ""
    bio = profile.get("bio") or ""
    desc = post.get("description") or ""
    created = (post.get("created_at") or "")[:10]
    images = _fetch_images(post_id)

    imgs_html = "".join(
        f'<figure><img src="{u}" alt=""/></figure>' for u in images
    ) or '<p class="empty">Нет изображений</p>'

    bio_html = f'<div class="bio">{_esc(bio)}</div>' if bio else ""

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(author)} — пост</title>
<style>
  :root {{
    --bg: #f2f2f7; --card: #fff; --text: #1a1a1a; --muted: #6b6b70;
    --border: #e4e4ea; --accent: #0a84ff;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0e0e10; --card:#1c1c1e; --text:#f2f2f7; --muted:#9a9aa0;
             --border:#2a2a2e; --accent:#0a84ff; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    line-height: 1.5;
  }}
  .container {{ max-width: 720px; margin: 0 auto; padding: 24px 16px 60px; }}
  .card {{
    background: var(--card); border-radius: 16px; overflow: hidden;
    box-shadow: 0 2px 12px rgba(0,0,0,.06); border: 1px solid var(--border);
  }}
  .header {{ padding: 24px 24px 12px; border-bottom: 1px solid var(--border); }}
  .author {{ font-size: 1.4em; font-weight: 700; margin: 0 0 4px; }}
  .username {{ color: var(--muted); font-size: .95em; }}
  .bio {{ margin-top: 12px; padding: 12px 14px; background: rgba(127,127,127,.08);
          border-radius: 10px; color: var(--text); font-size: .95em; white-space: pre-wrap; }}
  .body {{ padding: 20px 24px 24px; }}
  .desc {{ font-size: 1.05em; margin: 0 0 20px; white-space: pre-wrap; }}
  .desc-empty {{ color: var(--muted); font-style: italic; }}
  figure {{ margin: 0 0 12px; }}
  figure img {{ width: 100%; height: auto; border-radius: 12px; display: block;
                background: rgba(127,127,127,.08); }}
  .empty {{ color: var(--muted); }}
  .footer {{ padding: 16px 24px; color: var(--muted); font-size: .85em;
             border-top: 1px solid var(--border); }}
</style>
</head>
<body>
  <div class="container">
    <div class="card">
      <div class="header">
        <h1 class="author">{_esc(author)}</h1>
        <div class="username">@{_esc(username)}</div>
        {bio_html}
      </div>
      <div class="body">
        {f'<p class="desc">{_esc(desc)}</p>' if desc else '<p class="desc desc-empty">Без описания</p>'}
        {imgs_html}
      </div>
      <div class="footer">Опубликовано: {created}</div>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/")
async def root():
    return {"message": "Social Network API", "status": "running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
