import os
import uvicorn
import hashlib
import random
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import List, Optional, Dict

from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
import jwt
import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET = os.getenv("JWT_SECRET")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_EMAIL = "sldshr.confirmation@gmail.com"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 30

if not DATABASE_URL or not JWT_SECRET:
    raise RuntimeError("DATABASE_URL or JWT_SECRET is not set in env variables")

app = FastAPI(title="PySer API Optimized")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

db_pool = None
verification_codes: Dict[str, dict] = {}
rate_limit_store: Dict[str, List[datetime]] = {}

@app.on_event("startup")
async def startup():
    global db_pool
    # Ограничиваем пул соединений DB (max_size=4), чтобы сохранить ОЗУ на сервере 512MB
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=4,
        max_inactive_connection_lifetime=300
    )

@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()

class SendCodeReq(BaseModel):
    email: str

class UserRegister(BaseModel):
    email: str
    display_name: str
    password: str
    code: str

class UserLogin(BaseModel):
    email: str
    code: str

class LoginCodeReq(BaseModel):
    email: str
    password: str

class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = ""
    avatar_base64: Optional[str] = ""

class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str
    bio: str
    avatar_base64: str

class FriendAdd(BaseModel):
    email: str

class FriendRequestResponse(BaseModel):
    request_id: int
    sender_id: int
    sender_email: str
    sender_display_name: str
    avatar_base64: str

class RequestAction(BaseModel):
    request_id: int

class BlockAction(BaseModel):
    target_id: int

class MessageEdit(BaseModel):
    message_id: int
    new_text: str

class MessageAction(BaseModel):
    message_id: int

class MessageBulkAction(BaseModel):
    message_ids: List[int]

class MessageSend(BaseModel):
    receiver_id: int
    message: str
    reply_to_id: Optional[int] = None

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    to_encode.update({"exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

async def get_current_user_id(token: str = Depends(oauth2_scheme)) -> int:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return int(payload.get("sub"))
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

def is_rate_limited(key: str, max_requests: int = 3, window_seconds: int = 60) -> bool:
    """Защита от спама: максимум max_requests за window_seconds"""
    now = datetime.utcnow()
    history = rate_limit_store.get(key, [])
    history = [t for t in history if (now - t).total_seconds() < window_seconds]
    rate_limit_store[key] = history
    if len(history) >= max_requests:
        return True
    history.append(now)
    return False

def send_email_sync(receiver_email: str, code: str):
    if not SMTP_PASSWORD:
        return
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_EMAIL
        msg['To'] = receiver_email
        msg['Subject'] = "Код подтверждения PySer"
        html = f"""
        <html><body style="font-family: Arial, sans-serif; padding: 20px; text-align: center; background: #f9fafb;">
            <div style="max-width: 400px; margin: 0 auto; background: white; padding: 30px; border-radius: 16px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <h2 style="color: #4F46E5; margin-bottom: 8px;">PySer Messenger</h2>
                <p style="color: #4B5563;">Ваш одноразовый код доступа:</p>
                <div style="background: #F3F4F6; padding: 15px; border-radius: 12px; margin: 20px 0;">
                    <span style="font-size: 32px; font-weight: bold; letter-spacing: 6px; color: #1F2937;">{code}</span>
                </div>
            </div>
        </body></html>
        """
        msg.attach(MIMEText(html, 'html'))
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10)
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print("SMTP Error:", e)

async def generate_and_send_code(email: str):
    email_clean = email.lower().strip()
    code = str(random.randint(100000, 999999))
    verification_codes[email_clean] = {
        "code": code,
        "expires": datetime.utcnow() + timedelta(minutes=10),
        "attempts": 0
    }
    # Неблокирующий запуск отправки
    asyncio.create_task(asyncio.to_thread(send_email_sync, email_clean, code))

def verify_code(email: str, code: str):
    email_clean = email.lower().strip()
    record = verification_codes.get(email_clean)
    if not record:
        raise HTTPException(status_code=400, detail="Код не запрошен")
    if datetime.utcnow() > record["expires"]:
        del verification_codes[email_clean]
        raise HTTPException(status_code=400, detail="Код истёк")
    if record["code"] != code.strip():
        record["attempts"] += 1
        if record["attempts"] >= 3:
            del verification_codes[email_clean]
            raise HTTPException(status_code=400, detail="Попытки исчерпаны")
        raise HTTPException(status_code=400, detail="Неверный код")
    del verification_codes[email_clean]

@app.post("/auth/send-code")
async def send_auth_code(req: SendCodeReq):
    email_clean = req.email.lower().strip()
    if is_rate_limited(email_clean, max_requests=3, window_seconds=60):
        # Если превышен лимит, не шлем новое письмо, просто сообщаем что код уже отправлен
        return {"msg": "Code already sent recently"}

    async with db_pool.acquire() as conn:
        if await conn.fetchrow("SELECT id FROM users WHERE LOWER(email) = $1", email_clean):
            raise HTTPException(status_code=400, detail="Email taken")
    await generate_and_send_code(email_clean)
    return {"msg": "Code sent"}

@app.post("/register")
async def register(user: UserRegister):
    email_clean = user.email.lower().strip()
    verify_code(email_clean, user.code)
    async with db_pool.acquire() as conn:
        if await conn.fetchrow("SELECT id FROM users WHERE LOWER(email) = $1", email_clean):
            raise HTTPException(status_code=400, detail="Email taken")
        row = await conn.fetchrow(
            "INSERT INTO users (email, display_name, password_hash) VALUES ($1, $2, $3) RETURNING id", 
            email_clean, user.display_name, hash_password(user.password)
        )
        token = create_access_token({"sub": str(row['id'])})
        # Возвращаем token прямо при регистрации для мгновенного авто-входа
        return {"access_token": token, "token_type": "bearer", "msg": "User created"}

@app.post("/auth/login-code")
async def login_send_code(req: LoginCodeReq):
    email_clean = req.email.lower().strip()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, password_hash FROM users WHERE LOWER(email) = $1", email_clean)
        if not row or row['password_hash'] != hash_password(req.password):
            raise HTTPException(status_code=400, detail="Неверный email или пароль")
    
    if is_rate_limited(email_clean, max_requests=3, window_seconds=60):
        return {"msg": "Code already sent recently"}

    await generate_and_send_code(email_clean)
    return {"msg": "Code sent"}

@app.post("/login")
async def login(user: UserLogin):
    email_clean = user.email.lower().strip()
    verify_code(email_clean, user.code)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE LOWER(email) = $1", email_clean)
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        token = create_access_token({"sub": str(row['id'])})
        return {"access_token": token, "token_type": "bearer"}

@app.get("/profile/me", response_model=UserResponse)
async def get_profile(user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "id": row['id'],
            "email": row['email'],
            "display_name": row['display_name'],
            "bio": row['bio'] or "",
            "avatar_base64": row['avatar_base64'] or ""
        }

@app.post("/profile/me")
async def update_profile(profile: ProfileUpdate, user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET display_name=COALESCE($1, display_name), bio=$2, avatar_base64=$3 WHERE id=$4", 
            profile.display_name, profile.bio, profile.avatar_base64, user_id
        )
        return {"msg": "Updated"}

@app.post("/friends/request")
async def send_friend_request(payload: FriendAdd, user_id: int = Depends(get_current_user_id)):
    target_email = payload.email.lower().strip()
    async with db_pool.acquire() as conn:
        target = await conn.fetchrow("SELECT id FROM users WHERE LOWER(email) = $1", target_email)
        if not target:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        if target['id'] == user_id:
            raise HTTPException(status_code=400, detail="Нельзя добавить самого себя")
        
        existing = await conn.fetchrow(
            "SELECT status FROM friendships WHERE (user_id=$1 AND friend_id=$2) OR (user_id=$2 AND friend_id=$1)", 
            user_id, target['id']
        )
        if existing:
            raise HTTPException(status_code=400, detail=f"Заявка уже существует ({existing['status']})")
            
        await conn.execute("INSERT INTO friendships (user_id, friend_id, status) VALUES ($1, $2, 'pending')", user_id, target['id'])
        return {"msg": "Request sent"}

@app.get("/friends/requests", response_model=List[FriendRequestResponse])
async def get_friend_requests(user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        query = """
            SELECT f.id as request_id, u.id as sender_id, u.email as sender_email, 
                   u.display_name as sender_display_name, u.avatar_base64
            FROM friendships f JOIN users u ON f.user_id = u.id
            WHERE f.friend_id = $1 AND f.status = 'pending'
        """
        rows = await conn.fetch(query, user_id)
        return [dict(r) for r in rows]

@app.post("/friends/accept")
async def accept_request(payload: RequestAction, user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE friendships SET status='accepted' WHERE id=$1 AND friend_id=$2", payload.request_id, user_id)
        return {"msg": "Accepted"}

@app.post("/friends/reject")
async def reject_request(payload: RequestAction, user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM friendships WHERE id=$1 AND friend_id=$2", payload.request_id, user_id)
        return {"msg": "Rejected"}

@app.post("/friends/remove")
async def remove_friend(payload: BlockAction, user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM friendships WHERE (user_id=$1 AND friend_id=$2) OR (user_id=$2 AND friend_id=$1)", user_id, payload.target_id)
        await conn.execute("DELETE FROM messages WHERE (sender_id=$1 AND receiver_id=$2) OR (sender_id=$2 AND receiver_id=$1)", user_id, payload.target_id)
        return {"msg": "Removed"}

@app.get("/friends", response_model=List[UserResponse])
async def get_friends(user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        query = """
            SELECT u.id, u.email, u.display_name, u.bio, u.avatar_base64
            FROM users u
            WHERE u.id IN (
                SELECT friend_id FROM friendships WHERE user_id = $1 AND status = 'accepted'
                UNION
                SELECT user_id FROM friendships WHERE friend_id = $1 AND status = 'accepted'
            ) AND u.id != $1
        """
        rows = await conn.fetch(query, user_id)
        return [
            {
                "id": r['id'],
                "email": r['email'],
                "display_name": r['display_name'],
                "bio": r['bio'] or "",
                "avatar_base64": r['avatar_base64'] or ""
            }
            for r in rows
        ]

@app.get("/chats")
async def get_chats(user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        query = """
            SELECT DISTINCT u.id, u.email, u.display_name, u.bio, u.avatar_base64,
            (
                SELECT message FROM messages 
                WHERE (sender_id = u.id AND receiver_id = $1) OR (sender_id = $1 AND receiver_id = u.id)
                ORDER BY created_at DESC LIMIT 1
            ) as last_message
            FROM users u
            WHERE u.id IN (
                SELECT friend_id FROM friendships WHERE user_id = $1 AND status = 'accepted'
                UNION 
                SELECT user_id FROM friendships WHERE friend_id = $1 AND status = 'accepted'
            ) AND u.id != $1
        """
        rows = await conn.fetch(query, user_id)
        chat_list = [
            {
                "id": r['id'],
                "email": r['email'],
                "display_name": r['display_name'],
                "bio": r['bio'] or "",
                "avatar_base64": r['avatar_base64'] or "",
                "last_message": r['last_message'] or ""
            }
            for r in rows
        ]
        
        me = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        if me: 
            last_msg_me = await conn.fetchval("SELECT message FROM messages WHERE sender_id=$1 AND receiver_id=$1 ORDER BY created_at DESC LIMIT 1", user_id)
            chat_list.insert(0, {
                "id": me['id'],
                "email": me['email'],
                "display_name": "Избранное (Saved)",
                "bio": "Ваши сохраненные сообщения",
                "avatar_base64": me['avatar_base64'] or "",
                "last_message": last_msg_me or ""
            })
        return chat_list

@app.post("/messages/send")
async def send_msg(payload: MessageSend, user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages (sender_id, receiver_id, message, reply_to_id) VALUES ($1, $2, $3, $4)", 
            user_id, payload.receiver_id, payload.message.strip(), payload.reply_to_id
        )
        return {"msg": "Sent"}

@app.get("/messages/conversation/{friend_id}")
async def get_conv(friend_id: int, user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        if user_id != friend_id:
            await conn.execute("UPDATE messages SET is_read = TRUE WHERE sender_id = $1 AND receiver_id = $2 AND is_read = FALSE", friend_id, user_id)

        if user_id == friend_id:
            query = """
                SELECT m.id, m.sender_id, u.display_name as sender_name, m.receiver_id, m.message, m.created_at, m.is_read, m.is_edited,
                       m.reply_to_id, r.message as reply_text, u_reply.display_name as reply_sender_name
                FROM messages m 
                JOIN users u ON m.sender_id = u.id
                LEFT JOIN messages r ON m.reply_to_id = r.id
                LEFT JOIN users u_reply ON r.sender_id = u_reply.id
                WHERE m.sender_id = $1 AND m.receiver_id = $1
                ORDER BY m.created_at ASC LIMIT 150
            """
            rows = await conn.fetch(query, user_id)
        else:
            query = """
                SELECT m.id, m.sender_id, u.display_name as sender_name, m.receiver_id, m.message, m.created_at, m.is_read, m.is_edited,
                       m.reply_to_id, r.message as reply_text, u_reply.display_name as reply_sender_name
                FROM messages m 
                JOIN users u ON m.sender_id = u.id
                LEFT JOIN messages r ON m.reply_to_id = r.id
                LEFT JOIN users u_reply ON r.sender_id = u_reply.id
                WHERE (m.sender_id = $1 AND m.receiver_id = $2) OR (m.sender_id = $2 AND m.receiver_id = $1)
                ORDER BY m.created_at ASC LIMIT 150
            """
            rows = await conn.fetch(query, user_id, friend_id)
        return [dict(r) for r in rows]

@app.post("/messages/edit")
async def edit_msg(payload: MessageEdit, user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        res = await conn.execute("UPDATE messages SET message = $1, is_edited = TRUE WHERE id = $2 AND sender_id = $3", payload.new_text.strip(), payload.message_id, user_id)
        if res == "UPDATE 0":
            raise HTTPException(status_code=403, detail="Not authorized or not found")
        return {"msg": "Edited"}

@app.post("/messages/delete")
async def delete_msg(payload: MessageAction, user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        res = await conn.execute("DELETE FROM messages WHERE id = $1 AND sender_id = $2", payload.message_id, user_id)
        if res == "DELETE 0":
            raise HTTPException(status_code=403, detail="Not authorized or not found")
        return {"msg": "Deleted"}

@app.post("/messages/delete-bulk")
async def delete_msgs_bulk(payload: MessageBulkAction, user_id: int = Depends(get_current_user_id)):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE id = ANY($1) AND sender_id = $2", payload.message_ids, user_id)
        return {"msg": "Deleted"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
