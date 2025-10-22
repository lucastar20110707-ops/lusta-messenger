from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
import json
import time
import uvicorn
from database import get_db, User, Message
from datetime import datetime

app = FastAPI(title="LuStA Messenger")

# Разрешаем CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Храним активные WebSocket подключения
active_connections = {}
user_sessions = {}  # Храним ID пользователей по username

@app.get("/")
async def home():
    return {"message": "🔥 LuStA Messenger с БАЗОЙ ДАННЫХ!", "status": "success"}

# Регистрация пользователя
@app.post("/register")
async def register(
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    # Проверяем существует ли пользователь
    existing_user = db.query(User).filter(User.username == username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Пользователь уже существует")
    
    # Создаем нового пользователя
    new_user = User(username=username)
    new_user.set_password(password)
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    return {"message": "✅ Регистрация успешна!", "user_id": new_user.id}

# Авторизация
@app.post("/login")
async def login(
    username: str = Form(...), 
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.check_password(password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    
    # Сохраняем сессию
    user_sessions[username] = user.id
    
    return {
        "message": "✅ Вход выполнен!", 
        "user_id": user.id,
        "username": user.username
    }

# Получить список пользователей
@app.get("/users")
async def get_users(db: Session = Depends(get_db)):
    users = db.query(User).all()
    return {"users": [{"id": u.id, "username": u.username} for u in users]}

# Получить чаты пользователя
@app.get("/chats/{user_id}")
async def get_user_chats(user_id: int, db: Session = Depends(get_db)):
    # Находим всех пользователей, с которыми есть переписка
    sent_chats = db.query(Message.receiver_id).filter(Message.sender_id == user_id).distinct()
    received_chats = db.query(Message.sender_id).filter(Message.receiver_id == user_id).distinct()
    
    chat_partner_ids = set([id[0] for id in sent_chats] + [id[0] for id in received_chats])
    
    chats = []
    for partner_id in chat_partner_ids:
        partner = db.query(User).filter(User.id == partner_id).first()
        if partner:
            # Получаем последнее сообщение в чате
            last_message = db.query(Message).filter(
                ((Message.sender_id == user_id) & (Message.receiver_id == partner_id)) |
                ((Message.sender_id == partner_id) & (Message.receiver_id == user_id))
            ).order_by(Message.timestamp.desc()).first()
            
            chats.append({
                "partner_id": partner.id,
                "partner_username": partner.username,
                "last_message": last_message.content if last_message else "",
                "last_message_time": last_message.timestamp.isoformat() if last_message else "",
                "unread_count": db.query(Message).filter(
                    Message.sender_id == partner_id,
                    Message.receiver_id == user_id,
                    Message.is_read == 0
                ).count()
            })
    
    return {"chats": chats}

# Получить историю сообщений с конкретным пользователем
@app.get("/messages/{user_id}/{partner_id}")
async def get_messages(user_id: int, partner_id: int, db: Session = Depends(get_db)):
    messages = db.query(Message).filter(
        ((Message.sender_id == user_id) & (Message.receiver_id == partner_id)) |
        ((Message.sender_id == partner_id) & (Message.receiver_id == user_id))
    ).order_by(Message.timestamp.asc()).all()
    
    # Помечаем сообщения как прочитанные
    for message in messages:
        if message.receiver_id == user_id and message.is_read == 0:
            message.is_read = 1
    db.commit()
    
    return {"messages": [
        {
            "id": msg.id,
            "sender_id": msg.sender_id,
            "sender_username": msg.sender.username,
            "content": msg.content,
            "timestamp": msg.timestamp.isoformat(),
            "is_read": bool(msg.is_read)
        } for msg in messages
    ]}

# WebSocket для реального времени
@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await websocket.accept()
    
    # Получаем ID пользователя
    user_id = user_sessions.get(username)
    if not user_id:
        await websocket.close(code=1008, reason="User not authenticated")
        return
    
    active_connections[username] = {
        "websocket": websocket,
        "user_id": user_id
    }
    
    print(f"✅ {username} (ID: {user_id}) подключился. Онлайн: {len(active_connections)}")
    
    try:
        while True:
            data = await websocket.receive_text()
            message_data = json.loads(data)
            
            action = message_data.get("action")
            
            if action == "send_message":
                to_username = message_data.get("to")
                message_text = message_data.get("message")
                
                print(f"📨 {username} -> {to_username}: {message_text}")
                
                # Получаем базу данных для этого соединения
                db = next(get_db())
                
                # Находим получателя в базе
                receiver = db.query(User).filter(User.username == to_username).first()
                if not receiver:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"❌ Пользователь {to_username} не найден"
                    }))
                    continue
                
                # Сохраняем сообщение в базу
                new_message = Message(
                    sender_id=user_id,
                    receiver_id=receiver.id,
                    content=message_text,
                    timestamp=datetime.utcnow()
                )
                db.add(new_message)
                db.commit()
                
                # Отправляем получателю если он онлайн
                if to_username in active_connections:
                    await active_connections[to_username]["websocket"].send_text(json.dumps({
                        "type": "new_message",
                        "from": username,
                        "from_id": user_id,
                        "message": message_text,
                        "timestamp": new_message.timestamp.isoformat(),
                        "message_id": new_message.id
                    }))
                    
                    # Помечаем как доставленное
                    new_message.is_read = 1
                    db.commit()
                
                # Подтверждение отправителю
                await websocket.send_text(json.dumps({
                    "type": "message_sent",
                    "to": to_username,
                    "message_id": new_message.id,
                    "timestamp": new_message.timestamp.isoformat()
                }))
                
            elif action == "get_online_users":
                online_users = list(active_connections.keys())
                await websocket.send_text(json.dumps({
                    "type": "online_users",
                    "users": online_users,
                    "count": len(online_users)
                }))
                
    except WebSocketDisconnect:
        print(f"❌ {username} отключился")
        if username in active_connections:
            del active_connections[username]
    except Exception as e:
        print(f"⚠️ Ошибка у {username}: {e}")
        if username in active_connections:
            del active_connections[username]

if __name__ == "__main__":
    print("🚀 Запуск LuStA с БАЗОЙ ДАННЫХ...")
    print("📊 База данных: lusta.db")
    print("🌐 WebSocket: ws://localhost:8000/ws/{username}")
    print("⏹️  Остановка: Ctrl+C")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
