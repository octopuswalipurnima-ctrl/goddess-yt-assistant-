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
from app.database.models import User, XP
from app.dashboard.auth import router as auth_router
from app.dashboard.routes import router as dashboard_router
from app.bot.youtube_chat import YouTubeChatMonitor
from app.bot.discord_bot import start_discord_bot
from app.services.scheduler import start_scheduler

app = FastAPI(title="Goddess Stream Manager")

# ---------------------------------------------------------
# GLOBAL PRODUCTION PROXY & HTTPS ENFORCEMENT
# ---------------------------------------------------------
# 1. Custom Middleware to force HTTPS scheme globally on Railway
class ForceHTTPSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # If running on Railway and the proxy forwarded it as HTTP, force HTTPS scope
        if "localhost" not in str(request.url) and request.headers.get("x-forwarded-proto") == "http":
            request.scope["scheme"] = "https"
        elif "localhost" not in str(request.url) and str(request.url).startswith("http://"):
            request.scope["scheme"] = "https"
        return await call_next(request)

app.add_middleware(ForceHTTPSMiddleware)

# 2. Respect reverse proxy headers passed by Railway
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=("*",))

# 3. Configure secure session tracking flags for browser cookies
is_production = os.environ.get("PORT") is not None
app.add_middleware(
    SessionMiddleware, 
    secret_key="super-secret-goddess-key-change-later",
    max_age=3600 * 24 * 7,            # 7 Days persistence
    https_only=is_production,          # Ensures secure cookie flags line up
    same_site="lax"                    # Allows smooth authentication transitions
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


# ---------------------------------------------------------
# FRONTEND DASHBOARD ROUTES
# ---------------------------------------------------------
@app.get("/")
async def serve_dashboard(request: Request, db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    streamer_name = request.session.get("streamer_name")
    
    # Print state debugging directly to Railway logs to verify cookie presence
    print(f"[DASHBOARD ROOT ACCESS] streamer_id: {streamer_id} | streamer_name: {streamer_name}", flush=True)
    
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