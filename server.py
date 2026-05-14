from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlite3
import json
import hashlib
import os
import base64
from datetime import datetime
from typing import Dict, List, Optional
import uvicorn
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Создаём папку для аватаров
os.makedirs("avatars", exist_ok=True)

# ========== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    
    # Таблица пользователей (расширенная)
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        avatar TEXT DEFAULT '',
        bio TEXT DEFAULT '',
        status TEXT DEFAULT 'online',
        last_seen TIMESTAMP,
        created_at TIMESTAMP,
        phone TEXT DEFAULT '',
        email TEXT DEFAULT ''
    )''')
    
    # Таблица личных сообщений
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user TEXT NOT NULL,
        to_user TEXT NOT NULL,
        message TEXT NOT NULL,
        timestamp TIMESTAMP,
        is_read BOOLEAN DEFAULT 0
    )''')
    
    # Таблица групповых чатов
    c.execute('''CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        avatar TEXT DEFAULT '',
        created_by TEXT NOT NULL,
        created_at TIMESTAMP
    )''')
    
    # Участники групп
    c.execute('''CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER,
        username TEXT NOT NULL,
        joined_at TIMESTAMP,
        role TEXT DEFAULT 'member',
        UNIQUE(group_id, username)
    )''')
    
    # Сообщения в группах
    c.execute('''CREATE TABLE IF NOT EXISTS group_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        from_user TEXT NOT NULL,
        message TEXT NOT NULL,
        timestamp TIMESTAMP
    )''')
    
    # Добавляем тестового пользователя, если его нет
    test_password = hashlib.sha256("test123".encode()).hexdigest()
    c.execute("INSERT OR IGNORE INTO users (username, password, bio, created_at) VALUES (?, ?, ?, ?)",
              ("testuser", test_password, "Это тестовый аккаунт! Пишите мне 😊", datetime.now().isoformat()))
    
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")

init_db()

# ========== УПРАВЛЕНИЕ WEBSOCKET ==========
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.active_connections[username] = websocket
        # Обновляем статус в БД
        conn = sqlite3.connect('messenger.db')
        c = conn.cursor()
        c.execute("UPDATE users SET status='online', last_seen=? WHERE username=?", 
                  (datetime.now().isoformat(), username))
        conn.commit()
        conn.close()
        await self.broadcast_user_list()
        print(f"✅ {username} подключился. Онлайн: {len(self.active_connections)}")
    
    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]
            # Обновляем статус
            conn = sqlite3.connect('messenger.db')
            c = conn.cursor()
            c.execute("UPDATE users SET status='offline', last_seen=? WHERE username=?", 
                      (datetime.now().isoformat(), username))
            conn.commit()
            conn.close()
            print(f"❌ {username} отключился. Онлайн: {len(self.active_connections)}")
    
    async def send_personal(self, message: dict, username: str):
        if username in self.active_connections:
            try:
                await self.active_connections[username].send_json(message)
            except:
                pass
    
    async def broadcast_user_list(self):
        conn = sqlite3.connect('messenger.db')
        c = conn.cursor()
        c.execute("SELECT username, status, avatar, bio FROM users")
        users = [{"username": row[0], "status": row[1], "avatar": row[2], "bio": row[3]} for row in c.fetchall()]
        conn.close()
        
        for ws in self.active_connections.values():
            try:
                await ws.send_json({"type": "user_list", "users": users})
            except:
                pass
    
    async def send_to_group(self, group_id: int, message: dict, exclude: str = None):
        conn = sqlite3.connect('messenger.db')
        c = conn.cursor()
        c.execute("SELECT username FROM group_members WHERE group_id=?", (group_id,))
        members = [row[0] for row in c.fetchall()]
        conn.close()
        
        for member in members:
            if member != exclude and member in self.active_connections:
                await self.send_personal(message, member)

manager = ConnectionManager()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def save_private_message(from_user, to_user, message):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("INSERT INTO messages (from_user, to_user, message, timestamp) VALUES (?, ?, ?, ?)",
              (from_user, to_user, message, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def save_group_message(group_id, from_user, message):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("INSERT INTO group_messages (group_id, from_user, message, timestamp) VALUES (?, ?, ?, ?)",
              (group_id, from_user, message, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# ========== API ЭНДПОИНТЫ ==========
@app.get("/")
async def root():
    return FileResponse("index.html")

@app.get("/health")
async def health():
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    user_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM messages")
    msg_count = c.fetchone()[0]
    conn.close()
    return {
        "status": "alive",
        "users": user_count,
        "messages": msg_count,
        "online": len(manager.active_connections),
        "timestamp": datetime.now().isoformat()
    }

# ----- АВТОРИЗАЦИЯ -----
class AuthData(BaseModel):
    username: str
    password: str

@app.post("/register")
async def register(data: AuthData):
    hashed = hashlib.sha256(data.password.encode()).hexdigest()
    try:
        conn = sqlite3.connect('messenger.db')
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password, created_at, status) VALUES (?, ?, ?, ?)",
                  (data.username, hashed, datetime.now().isoformat(), "online"))
        conn.commit()
        conn.close()
        return {"success": True, "message": "User created"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Username already exists")

@app.post("/login")
async def login(data: AuthData):
    hashed = hashlib.sha256(data.password.encode()).hexdigest()
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("SELECT username, avatar, bio, phone, email FROM users WHERE username=? AND password=?", 
              (data.username, hashed))
    user = c.fetchone()
    conn.close()
    
    if user:
        return {
            "success": True,
            "username": user[0],
            "avatar": user[1] or "",
            "bio": user[2] or "",
            "phone": user[3] or "",
            "email": user[4] or ""
        }
    raise HTTPException(status_code=401, detail="Invalid credentials")

# ----- ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ -----
@app.get("/profile/{username}")
async def get_profile(username: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("SELECT username, avatar, bio, status, last_seen, phone, email, created_at FROM users WHERE username=?", (username,))
    user = c.fetchone()
    conn.close()
    
    if user:
        return {
            "username": user[0],
            "avatar": user[1] or "",
            "bio": user[2] or "",
            "status": user[3] or "offline",
            "last_seen": user[4],
            "phone": user[5] or "",
            "email": user[6] or "",
            "joined": user[7]
        }
    raise HTTPException(status_code=404, detail="User not found")

@app.post("/update_profile")
async def update_profile(
    username: str = Form(...),
    bio: str = Form(None),
    phone: str = Form(None),
    email: str = Form(None),
    avatar: UploadFile = File(None)
):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    
    updates = []
    params = []
    
    if bio is not None:
        updates.append("bio=?")
        params.append(bio)
    
    if phone is not None:
        updates.append("phone=?")
        params.append(phone)
    
    if email is not None:
        updates.append("email=?")
        params.append(email)
    
    if avatar:
        avatar_data = await avatar.read()
        avatar_base64 = base64.b64encode(avatar_data).decode('utf-8')
        avatar_save = f"data:{avatar.content_type};base64,{avatar_base64}"
        updates.append("avatar=?")
        params.append(avatar_save)
    
    if updates:
        params.append(username)
        c.execute(f"UPDATE users SET {', '.join(updates)} WHERE username=?", params)
        conn.commit()
    
    conn.close()
    return {"success": True, "message": "Profile updated"}

# ----- ЧАТЫ -----
@app.get("/history/private/{user1}/{user2}")
async def private_history(user1: str, user2: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("""SELECT from_user, message, timestamp 
                 FROM messages 
                 WHERE (from_user=? AND to_user=?) OR (from_user=? AND to_user=?)
                 ORDER BY timestamp LIMIT 200""", 
              (user1, user2, user2, user1))
    messages = [{"from": row[0], "text": row[1], "time": row[2]} for row in c.fetchall()]
    conn.close()
    return messages

# ----- ГРУППЫ -----
class GroupCreate(BaseModel):
    name: str
    description: str
    created_by: str

@app.post("/group/create")
async def create_group(group: GroupCreate):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("INSERT INTO groups (name, description, created_by, created_at) VALUES (?, ?, ?, ?)",
              (group.name, group.description, group.created_by, datetime.now().isoformat()))
    group_id = c.lastrowid
    c.execute("INSERT INTO group_members (group_id, username, role, joined_at) VALUES (?, ?, 'admin', ?)",
              (group_id, group.created_by, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"success": True, "group_id": group_id}

@app.get("/group/list/{username}")
async def list_groups(username: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("""SELECT g.id, g.name, g.description, g.avatar 
                 FROM groups g 
                 JOIN group_members gm ON g.id = gm.group_id 
                 WHERE gm.username=?""", (username,))
    groups = [{"id": row[0], "name": row[1], "description": row[2], "avatar": row[3]} for row in c.fetchall()]
    conn.close()
    return groups

@app.get("/group/members/{group_id}")
async def group_members(group_id: int):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("SELECT username, role FROM group_members WHERE group_id=?", (group_id,))
    members = [{"username": row[0], "role": row[1]} for row in c.fetchall()]
    conn.close()
    return members

@app.post("/group/add_member")
async def add_member(group_id: int, username: str, added_by: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    try:
        c.execute("INSERT INTO group_members (group_id, username, joined_at) VALUES (?, ?, ?)",
                  (group_id, username, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return {"success": True}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="User already in group")

@app.get("/history/group/{group_id}")
async def group_history(group_id: int):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("""SELECT from_user, message, timestamp 
                 FROM group_messages 
                 WHERE group_id=?
                 ORDER BY timestamp LIMIT 200""", (group_id,))
    messages = [{"from": row[0], "text": row[1], "time": row[2]} for row in c.fetchall()]
    conn.close()
    return messages

@app.get("/user/search/{query}")
async def search_users(query: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE username LIKE ? LIMIT 10", (f"%{query}%",))
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

# ========== WEBSOCKET ==========
@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(websocket, username)
    try:
        while True:
            data = await websocket.receive_json()
            
            if data["type"] == "private":
                save_private_message(username, data["to"], data["message"])
                await manager.send_personal({
                    "type": "private",
                    "from": username,
                    "message": data["message"],
                    "time": datetime.now().strftime("%H:%M")
                }, data["to"])
                await manager.send_personal({
                    "type": "private",
                    "from": username,
                    "message": data["message"],
                    "time": datetime.now().strftime("%H:%M"),
                    "is_own": True
                }, username)
            
            elif data["type"] == "group":
                save_group_message(data["group_id"], username, data["message"])
                await manager.send_to_group(data["group_id"], {
                    "type": "group",
                    "group_id": data["group_id"],
                    "from": username,
                    "message": data["message"],
                    "time": datetime.now().strftime("%H:%M")
                }, exclude=username)
                await manager.send_personal({
                    "type": "group",
                    "group_id": data["group_id"],
                    "from": username,
                    "message": data["message"],
                    "time": datetime.now().strftime("%H:%M"),
                    "is_own": True
                }, username)
                
    except WebSocketDisconnect:
        manager.disconnect(username)
        await manager.broadcast_user_list()

if __name__ == "__main__":
    print("🚀 Макс Мессенджер запущен!")
    print("📊 Health check: /health")
    print("👤 Тестовый пользователь: testuser / test123")
    uvicorn.run(app, host="0.0.0.0", port=8000)