import os
import hashlib
import uuid
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# --- Конфигурация ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL и SUPABASE_KEY должны быть установлены")

# Android package + SHA-256 fingerprint для App Links
ANDROID_PACKAGE = os.environ.get("ANDROID_PACKAGE", "com.example.socialnetwork")
ANDROID_SHA256  = os.environ.get("ANDROID_SHA256", "")  # добавьте свой отпечаток

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
            if isinstance(b, dict):
                names.append(b.get("name"))
            else:
                names.append(getattr(b, "name", None))
        if BUCKET_NAME not in names:
            supabase.storage.create_bucket(BUCKET_NAME, options={"public": True})
            print(f"[startup] created bucket '{BUCKET_NAME}'")
        else:
            print(f"[startup] bucket '{BUCKET_NAME}' exists")
    except Exception as e:
        print(f"[startup] bucket check failed: {e}")
        traceback.print_exc()


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


# --- Эндпоинты ---
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
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Пустой файл")

    ct = (file.content_type or "").lower()
    if ct not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        name = file.filename or ""
        ext_guess = name.rsplit(".", 1)[-1].lower() if "." in name else "jpg"
        ct = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
              "png": "image/png", "webp": "image/webp",
              "gif": "image/gif"}.get(ext_guess, "image/jpeg")

    ext = {"image/jpeg": "jpg", "image/png": "png",
           "image/webp": "webp", "image/gif": "gif"}.get(ct, "jpg")

    filename = f"{user['id']}/{uuid.uuid4().hex}.{ext}"

    try:
        try:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=filename,
                file=contents,
                file_options={"content-type": ct, "upsert": "false"},
            )
        except Exception as inner:
            msg = str(inner)
            if "Bucket not found" in msg or "not found" in msg.lower():
                supabase.storage.create_bucket(BUCKET_NAME, options={"public": True})
                supabase.storage.from_(BUCKET_NAME).upload(
                    path=filename,
                    file=contents,
                    file_options={"content-type": ct, "upsert": "false"},
                )
            else:
                raise

        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
        if isinstance(public_url, dict):
            public_url = public_url.get("publicUrl") or public_url.get("public_url") or ""

        return {"url": public_url}

    except Exception as e:
        print("[upload] FAILED:", repr(e))
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")


@app.post("/posts")
async def create_post(post: PostCreate, user: dict = Depends(get_current_user)):
    if len(post.image_urls) > 5:
        raise HTTPException(status_code=400, detail="Максимум 5 изображений")
    resp = supabase.table("posts").insert({
        "user_id": user["id"],
        "description": post.description,
    }).execute()
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


@app.get("/my-posts")
async def my_posts(user: dict = Depends(get_current_user)):
    """Список постов текущего пользователя с картинками и ссылкой."""
    posts_resp = supabase.table("posts").select("*") \
        .eq("user_id", user["id"]).order("created_at", desc=True).execute()
    result = []
    for p in posts_resp.data or []:
        imgs = _fetch_images(p["id"])
        result.append({
            "id": p["id"],
            "description": p.get("description") or "",
            "created_at": p.get("created_at"),
            "images": imgs,
            "share_url": f"https://sldchat.fastapicloud.dev/posts/{p['id']}",
        })
    return {"posts": result}


@app.delete("/posts/{post_id}")
async def delete_post(post_id: str, user: dict = Depends(get_current_user)):
    post_resp = supabase.table("posts").select("*").eq("id", post_id).execute()
    if not post_resp.data:
        raise HTTPException(status_code=404, detail="Пост не найден")
    post = post_resp.data[0]
    if post["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет прав на удаление")

    # Удаляем файлы из Storage
    for url in _fetch_images(post_id):
        try:
            # из публичного URL вытаскиваем путь внутри бакета
            marker = f"/object/public/{BUCKET_NAME}/"
            if marker in url:
                path = url.split(marker, 1)[1]
                supabase.storage.from_(BUCKET_NAME).remove([path])
        except Exception as e:
            print("[delete] storage remove failed:", e)

    # post_images удалятся каскадом (ON DELETE CASCADE)
    supabase.table("posts").delete().eq("id", post_id).execute()
    return {"status": "ok"}


@app.get("/posts/{post_id}")
async def get_post(post_id: str, request: Request):
    post_resp = supabase.table("posts").select("*, profiles(username, display_name)").eq("id", post_id).execute()
    if not post_resp.data:
        raise HTTPException(status_code=404, detail="Пост не найден")
    post = post_resp.data[0]
    images = _fetch_images(post_id)

    user_agent = request.headers.get("user-agent", "").lower()
    is_mobile = any(kw in user_agent for kw in ["android", "iphone", "ipad", "mobile"])

    author = post["profiles"].get("display_name") or post["profiles"]["username"]

    if is_mobile:
        # Страница-прослойка: пытается открыть приложение, иначе показывает кнопку
        deep_link = f"sldnet://post/{post_id}"
        app_link  = f"https://sldchat.fastapicloud.dev/posts/{post_id}"
        intent_url = (
            f"intent://sldchat.fastapicloud.dev/posts/{post_id}"
            f"#Intent;scheme=https;package={ANDROID_PACKAGE};end"
        )
        images_html = "".join(
            f'<img src="{url}" style="max-width:100%; margin:10px 0; border-radius:8px;" />'
            for url in images
        ) or "<em>Нет изображений</em>"
        desc = post["description"] or ""
        html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Пост от {author}</title>
    <style>
        body {{ font-family: -apple-system, Arial, sans-serif; max-width: 640px; margin: 0 auto;
                padding: 24px 20px 60px; color: #222; background: #f5f5f7; }}
        .card {{ background: #fff; border-radius: 14px; padding: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
        h1 {{ font-size: 1.25em; margin: 0 0 4px; }}
        .meta {{ color: #666; font-size: .9em; margin-bottom: 16px; }}
        .desc {{ font-size: 1.05em; line-height: 1.5; margin: 12px 0; white-space: pre-wrap; }}
        .open-btn {{ display:block; text-align:center; text-decoration:none; background:#0a84ff;
                     color:#fff; padding:14px; border-radius:12px; font-weight:600; margin: 20px 0; }}
        img {{ display:block; border-radius:8px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>{author}</h1>
        <div class="meta">@{post['profiles']['username']}</div>
        <a class="open-btn" href="{intent_url}">Открыть в приложении</a>
        <a class="open-btn" style="background:#34c759" href="{deep_link}">Открыть (если не сработало)</a>
        <div class="desc">{desc}</div>
        <div>{images_html}</div>
    </div>
    <script>
        // Авто-попытка открыть приложение через custom scheme
        setTimeout(function() {{
            window.location.href = "{deep_link}";
        }}, 100);
    </script>
</body>
</html>"""
        return HTMLResponse(content=html)

    # ПК: обычная страница
    desc = post["description"] or ""
    images_html = "".join(
        f'<img src="{url}" style="max-width:100%; margin:10px 0; border-radius:8px;" />'
        for url in images
    )
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


@app.get("/.well-known/assetlinks.json")
async def assetlinks():
    """Файл для верификации Android App Links."""
    if not ANDROID_SHA256:
        raise HTTPException(status_code=404, detail="ANDROID_SHA256 не настроен")
    return JSONResponse([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": ANDROID_PACKAGE,
            "sha256_cert_fingerprints": [ANDROID_SHA256],
        },
    }])


@app.get("/")
async def root():
    return {"message": "Social Network API", "status": "running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
