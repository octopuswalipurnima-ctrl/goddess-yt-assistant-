import os
import re
import asyncio
import random
import string
import uvicorn
from fastapi import FastAPI, Request, Depends, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from sqlalchemy.orm import Session

from app.database.connection import init_db, get_db
from app.database.models import User, XP, Streamer
from app.dashboard.auth import router as auth_router
from app.dashboard.routes import router as dashboard_router
from app.bot.youtube_chat import YouTubeChatMonitor, DETECTED_VIDEOS
from app.bot.discord_bot import start_discord_bot
from app.services.scheduler import start_scheduler
from app.services.websocket import overlay_manager

app = FastAPI(title="Goddess Stream Manager")

# ---------------------------------------------------------
# MIDDLEWARE & BROWSER SESSIONS
# ---------------------------------------------------------
class ForceHTTPSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if "localhost" not in str(request.url) and request.headers.get("x-forwarded-proto") == "http":
            request.scope["scheme"] = "https"
        elif "localhost" not in str(request.url) and str(request.url).startswith("http://"):
            request.scope["scheme"] = "https"
        return await call_next(request)

app.add_middleware(ForceHTTPSMiddleware)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=("*",))

app.add_middleware(
    SessionMiddleware, 
    secret_key="super-secret-goddess-key-change-later",
    max_age=3600 * 24 * 7,
    https_only=False,
    same_site="lax"
)

# AUTOMATIC SAFEGUARD
if not os.path.exists("static"):
    os.makedirs("static")
if not os.path.exists("templates"):
    os.makedirs("templates")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------
# FRONTEND DASHBOARD ROUTES (WITH GUEST & OBS WIDGETS)
# ---------------------------------------------------------
@app.get("/")
async def serve_dashboard(request: Request, db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    
    if not streamer_id:
        return templates.TemplateResponse(
            request=request, 
            name="index.html", 
            context={
                "request": request, 
                "streamer_name": None, 
                "viewers": [], 
                "settings": {}
            }
        )
        
    streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
    
    if not streamer:
        request.session.clear()
        return RedirectResponse(url="/", status_code=303)
    
    if not streamer.server_sync_code:
        streamer.server_sync_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        db.commit()
        
    viewers = db.query(User).join(XP).filter(XP.streamer_id == streamer_id).all()
    
    settings = {
        "ai_cohost_enabled": streamer.ai_cohost_enabled,
        "giveaway_reminders_enabled": streamer.giveaway_reminders_enabled,
        "server_sync_code": streamer.server_sync_code,
        "is_discord_linked": bool(streamer.discord_guild_id)
    }
    
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={
            "request": request,
            "streamer_name": streamer.channel_name,
            "viewers": viewers,
            "settings": settings
        }
    )

@app.post("/toggle-setting")
async def toggle_setting(request: Request, setting: str = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if streamer:
            if setting == "ai_cohost":
                streamer.ai_cohost_enabled = not streamer.ai_cohost_enabled
            elif setting == "giveaways":
                streamer.giveaway_reminders_enabled = not streamer.giveaway_reminders_enabled
            db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/guest-join")
async def guest_join(request: Request, stream_url: str = Form(...)):
    yt_regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/live\/)([a-zA-Z0-9_-]{11})"
    match = re.search(yt_regex, stream_url)
    
    if match:
        video_id = match.group(1)
        print(f"[GUEST MODE] Summon request received for Video ID: {video_id}")
        DETECTED_VIDEOS.add(video_id)
        
    return RedirectResponse(url="/?guest=true", status_code=303)

# ---------------------------------------------------------
# OBS WEBSOCKET & WIDGET ROUTES
# ---------------------------------------------------------
@app.get("/overlay/{sync_code}")
async def render_overlay(request: Request, sync_code: str):
    """The actual webpage that OBS loads as a Browser Source."""
    return templates.TemplateResponse(
        request=request,
        name="overlay.html", 
        context={"request": request, "sync_code": sync_code}
    )

@app.websocket("/ws/overlay/{sync_code}")
async def websocket_overlay(websocket: WebSocket, sync_code: str):
    """The real-time connection from OBS to the backend."""
    await overlay_manager.connect(websocket, sync_code)
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        overlay_manager.disconnect(websocket, sync_code)

@app.post("/test-alert")
async def test_alert(request: Request, db: Session = Depends(get_db)):
    """Allows streamers to test their OBS widget from the dashboard."""
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if streamer and streamer.server_sync_code:
            test_payload = {
                "type": "alert",
                "event_type": "superChatEvent",
                "author": "System Tester",
                "message": "This is a test Super Chat! Your OBS widget is working perfectly!",
                "amount": "$50.00"
            }
            await overlay_manager.send_alert(streamer.server_sync_code, test_payload)
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------
# MOUNT ADDITIONAL ROUTERS
# ---------------------------------------------------------
app.include_router(dashboard_router)
app.include_router(auth_router)


# ---------------------------------------------------------
# BACKGROUND WORKERS & STARTUP LOGIC
# ---------------------------------------------------------
running_tasks = []

@app.on_event("startup")
async def startup_event():
    print("[STARTUP] Initializing systems...")
    init_db()
    start_scheduler()
    
    yt_monitor = YouTubeChatMonitor()
    
    task1 = asyncio.create_task(yt_monitor.run())
    task2 = asyncio.create_task(start_discord_bot())
    
    running_tasks.append(task1)
    running_tasks.append(task2)
    
    print("[STARTUP] Web Admin Dashboard, YouTube Engine, and Discord Bot are active!")

if __name__ == "__main__":
    railway_port = int(os.environ.get("PORT", 8000))
    should_reload = False if os.environ.get("PORT") else True
    
    print(f"[BOOT] Launching Uvicorn server on port {railway_port}...")
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=railway_port, 
        reload=should_reload, 
        proxy_headers=True, 
        forwarded_allow_ips="*"
    )