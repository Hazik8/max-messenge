from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import json
import hashlib
import os
import base64
from datetime import datetime
from typing import Dict, List
import asyncio
import uvicorn

app = FastAPI()

# Разрешаем всё для CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Инициализация БД
def init_db():
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    
    # Пользователи
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        avatar TEXT DEFAULT '',
        bio TEXT DEFAULT '',
        status TEXT DEFAULT 'offline',
        last_seen TIMESTAMP,
        created_at TIMESTAMP
    )''')
    
    # Личные сообщения
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user TEXT NOT NULL,
        to_user TEXT NOT NULL,
        message TEXT NOT NULL,
        timestamp TIMESTAMP,
        is_read BOOLEAN DEFAULT 0
    )''')
    
    # Группы
    c.execute('''CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
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
    
    # Тестовый пользователь
    test_password = hashlib.sha256("test123".encode()).hexdigest()
    c.execute("INSERT OR IGNORE INTO users (username, password, bio, created_at, status) VALUES (?, ?, ?, ?, ?)",
              ("testuser", test_password, "🔥 Тестовый аккаунт! Напишите мне", datetime.now().isoformat(), "offline"))
    
    conn.commit()
    conn.close()
    print("✅ База данных готова")

init_db()

# Управление WebSocket
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.active_connections[username] = websocket
        
        conn = sqlite3.connect('messenger.db')
        c = conn.cursor()
        c.execute("UPDATE users SET status='online', last_seen=? WHERE username=?", 
                  (datetime.now().isoformat(), username))
        conn.commit()
        conn.close()
        
        print(f"✅ {username} онлайн")
        await self.broadcast_users()
    
    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]
            conn = sqlite3.connect('messenger.db')
            c = conn.cursor()
            c.execute("UPDATE users SET status='offline', last_seen=? WHERE username=?", 
                      (datetime.now().isoformat(), username))
            conn.commit()
            conn.close()
            print(f"❌ {username} офлайн")
    
    async def send_to_user(self, username: str, message: dict):
        if username in self.active_connections:
            try:
                await self.active_connections[username].send_json(message)
            except:
                pass
    
    async def broadcast_users(self):
        conn = sqlite3.connect('messenger.db')
        c = conn.cursor()
        c.execute("SELECT username, status, avatar, bio FROM users")
        users = [{"username": r[0], "status": r[1], "avatar": r[2] or "", "bio": r[3] or ""} for r in c.fetchall()]
        conn.close()
        
        for ws in self.active_connections.values():
            try:
                await ws.send_json({"type": "users", "users": users})
            except:
                pass
    
    async def send_to_group(self, group_id: int, message: dict, exclude: str = None):
        conn = sqlite3.connect('messenger.db')
        c = conn.cursor()
        c.execute("SELECT username FROM group_members WHERE group_id=?", (group_id,))
        members = [r[0] for r in c.fetchall()]
        conn.close()
        
        for member in members:
            if member != exclude and member in self.active_connections:
                await self.send_to_user(member, message)

manager = ConnectionManager()

# Функции для БД
def save_private(from_user, to_user, message):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("INSERT INTO messages (from_user, to_user, message, timestamp) VALUES (?, ?, ?, ?)",
              (from_user, to_user, message, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def save_group(group_id, from_user, message):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("INSERT INTO group_messages (group_id, from_user, message, timestamp) VALUES (?, ?, ?, ?)",
              (group_id, from_user, message, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# API Эндпоинты
@app.get("/")
async def root():
    return FileResponse("index.html")

@app.get("/health")
async def health():
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    users = c.fetchone()[0]
    conn.close()
    return {"status": "ok", "users": users, "online": len(manager.active_connections)}

@app.post("/register")
async def register(username: str, password: str):
    hashed = hashlib.sha256(password.encode()).hexdigest()
    try:
        conn = sqlite3.connect('messenger.db')
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password, created_at, status) VALUES (?, ?, ?, ?)",
                  (username, hashed, datetime.now().isoformat(), "offline"))
        conn.commit()
        conn.close()
        return {"success": True}
    except:
        raise HTTPException(400, "Пользователь уже существует")

@app.post("/login")
async def login(username: str, password: str):
    hashed = hashlib.sha256(password.encode()).hexdigest()
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("SELECT username, avatar, bio FROM users WHERE username=? AND password=?", (username, hashed))
    user = c.fetchone()
    conn.close()
    if user:
        return {"success": True, "username": user[0], "avatar": user[1] or "", "bio": user[2] or ""}
    raise HTTPException(401, "Неверные данные")

@app.get("/profile/{username}")
async def get_profile(username: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("SELECT username, avatar, bio, status, last_seen FROM users WHERE username=?", (username,))
    user = c.fetchone()
    conn.close()
    if user:
        return {"username": user[0], "avatar": user[1] or "", "bio": user[2] or "", "status": user[3] or "offline", "last_seen": user[4]}
    raise HTTPException(404, "Не найден")

@app.post("/update_profile")
async def update_profile(
    username: str = Form(...),
    bio: str = Form(None),
    avatar: UploadFile = File(None)
):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    
    if bio is not None:
        c.execute("UPDATE users SET bio=? WHERE username=?", (bio, username))
    
    if avatar:
        data = await avatar.read()
        b64 = base64.b64encode(data).decode()
        avatar_data = f"data:{avatar.content_type};base64,{b64}"
        c.execute("UPDATE users SET avatar=? WHERE username=?", (avatar_data, username))
    
    conn.commit()
    conn.close()
    return {"success": True}

@app.get("/history/{user1}/{user2}")
async def get_history(user1: str, user2: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("""SELECT from_user, message, timestamp FROM messages 
                 WHERE (from_user=? AND to_user=?) OR (from_user=? AND to_user=?)
                 ORDER BY timestamp LIMIT 200""", (user1, user2, user2, user1))
    msgs = [{"from": r[0], "text": r[1], "time": r[2]} for r in c.fetchall()]
    conn.close()
    return msgs

@app.get("/groups/{username}")
async def get_groups(username: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("""SELECT g.id, g.name, g.description FROM groups g 
                 JOIN group_members gm ON g.id=gm.group_id WHERE gm.username=?""", (username,))
    groups = [{"id": r[0], "name": r[1], "description": r[2] or ""} for r in c.fetchall()]
    conn.close()
    return groups

@app.post("/group/create")
async def create_group(name: str, description: str, created_by: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("INSERT INTO groups (name, description, created_by, created_at) VALUES (?, ?, ?, ?)",
              (name, description, created_by, datetime.now().isoformat()))
    group_id = c.lastrowid
    c.execute("INSERT INTO group_members (group_id, username, role, joined_at) VALUES (?, ?, 'admin', ?)",
              (group_id, created_by, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"success": True, "group_id": group_id}

@app.post("/group/add")
async def add_to_group(group_id: int, username: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    try:
        c.execute("INSERT INTO group_members (group_id, username, joined_at) VALUES (?, ?, ?)",
                  (group_id, username, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return {"success": True}
    except:
        conn.close()
        raise HTTPException(400, "Уже в группе")

@app.get("/group/history/{group_id}")
async def group_history(group_id: int):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("SELECT from_user, message, timestamp FROM group_messages WHERE group_id=? ORDER BY timestamp LIMIT 200", (group_id,))
    msgs = [{"from": r[0], "text": r[1], "time": r[2]} for r in c.fetchall()]
    conn.close()
    return msgs

@app.get("/search/{query}")
async def search_users(query: str):
    conn = sqlite3.connect('messenger.db')
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE username LIKE ? LIMIT 10", (f"%{query}%",))
    users = [r[0] for r in c.fetchall()]
    conn.close()
    return users

# WebSocket
@app.websocket("/ws/{username}")
async def websocket_handler(websocket: WebSocket, username: str):
    await manager.connect(websocket, username)
    try:
        while True:
            data = await websocket.receive_json()
            print(f"📨 {username}: {data}")
            
            if data["type"] == "private":
                save_private(username, data["to"], data["message"])
                await manager.send_to_user(data["to"], {
                    "type": "private",
                    "from": username,
                    "message": data["message"],
                    "time": datetime.now().strftime("%H:%M")
                })
                await manager.send_to_user(username, {
                    "type": "private",
                    "from": username,
                    "message": data["message"],
                    "time": datetime.now().strftime("%H:%M"),
                    "is_own": True
                })
            
            elif data["type"] == "group":
                save_group(data["group_id"], username, data["message"])
                await manager.send_to_group(data["group_id"], {
                    "type": "group",
                    "group_id": data["group_id"],
                    "from": username,
                    "message": data["message"],
                    "time": datetime.now().strftime("%H:%M")
                }, exclude=username)
                await manager.send_to_user(username, {
                    "type": "group",
                    "group_id": data["group_id"],
                    "from": username,
                    "message": data["message"],
                    "time": datetime.now().strftime("%H:%M"),
                    "is_own": True
                })
                
    except WebSocketDisconnect:
        manager.disconnect(username)

if __name__ == "__main__":
    print("\n" + "="*50)
    print("🚀 МАКС МЕССЕНДЖЕР ЗАПУЩЕН")
    print("="*50)
    print("👤 Логин: testuser")
    print("🔑 Пароль: test123")
    print("="*50 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)