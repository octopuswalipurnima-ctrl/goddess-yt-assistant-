import os
import asyncio
import uvicorn
from fastapi import FastAPI, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from sqlalchemy.orm import Session

from app.database.connection import init_db, get_db
# Added Streamer to the imports to read/write settings
from app.database.models import User, XP, Streamer
from app.dashboard.auth import router as auth_router
from app.dashboard.routes import router as dashboard_router
from app.bot.youtube_chat import YouTubeChatMonitor
from app.bot.discord_bot import start_discord_bot
from app.services.scheduler import start_scheduler

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
# FRONTEND DASHBOARD ROUTES (UPGRADED WITH DATABASE SETTINGS)
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
        
    # Fetch the active streamer's specific settings from the database
    streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
    viewers = db.query(User).join(XP).filter(XP.streamer_id == streamer_id).all()
    
    settings = {
        "ai_cohost_enabled": streamer.ai_cohost_enabled if streamer else True,
        "giveaway_reminders_enabled": streamer.giveaway_reminders_enabled if streamer else False,
        "discord_guild_id": streamer.discord_guild_id if streamer else "",
        "discord_log_channel_id": streamer.discord_log_channel_id if streamer else ""
    }
    
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={
            "request": request,
            "streamer_name": streamer.channel_name if streamer else None,
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

@app.post("/update-discord")
async def update_discord(request: Request, guild_id: str = Form(""), channel_id: str = Form(""), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if streamer:
            streamer.discord_guild_id = guild_id
            streamer.discord_log_channel_id = channel_id
            db.commit()
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