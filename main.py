import os
import asyncio
import uvicorn
from fastapi import FastAPI, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from sqlalchemy.orm import Session

from app.database.connection import init_db, get_db
from app.database.models import User, XP
from app.dashboard.auth import router as auth_router
from app.dashboard.routes import router as dashboard_router
from app.bot.youtube_chat import YouTubeChatMonitor
from app.bot.discord_bot import start_discord_bot
from app.services.scheduler import start_scheduler

app = FastAPI(title="Goddess Stream Manager")

# 1. FIX THE RAILWAY PROXY HEADERS (Forces HTTPS for Google OAuth)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=("*",))

# 2. BROWSER SESSIONS
app.add_middleware(
    SessionMiddleware, 
    secret_key="super-secret-goddess-key-change-later",
    max_age=3600 * 24 * 7 # Keeps you logged in for 7 days
)

# AUTOMATIC SAFEGUARD: Creates folders if Git missed them
if not os.path.exists("static"):
    os.makedirs("static")
if not os.path.exists("templates"):
    os.makedirs("templates")

# Mount assets and specify templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Mount authentication and dashboard routers
app.include_router(dashboard_router)
app.include_router(auth_router)


# -----------------------------------------
# FRONTEND DASHBOARD ROUTES
# -----------------------------------------
@app.get("/")
async def serve_dashboard(request: Request, db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    streamer_name = request.session.get("streamer_name")
    
    settings = {
        "ai_cohost_enabled": True,
        "giveaway_reminders_enabled": False
    }
    
    if not streamer_id:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "streamer_name": None,
            "viewers": [],
            "settings": settings
        })
        
    viewers = db.query(User).join(XP).filter(XP.streamer_id == streamer_id).all()
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "streamer_name": streamer_name,
        "viewers": viewers,
        "settings": settings
    })

@app.post("/toggle-ai")
async def toggle_ai(mode: str = Form(...)):
    print(f"[DASHBOARD ACTION] Toggled setting: {mode}")
    return RedirectResponse(url="/", status_code=303)

@app.post("/manual-announcement")
async def manual_announcement(message: str = Form(...)):
    print(f"[DASHBOARD ACTION] Sending manual announcement: {message}")
    return RedirectResponse(url="/", status_code=303)


# -----------------------------------------
# BACKGROUND WORKERS & STARTUP LOGIC
# -----------------------------------------
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
    # proxy_headers=True tells the server to respect the middleware we added above
    uvicorn.run("main:app", host="0.0.0.0", port=railway_port, reload=should_reload, proxy_headers=True, forwarded_allow_ips="*")