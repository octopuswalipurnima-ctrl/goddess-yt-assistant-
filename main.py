import asyncio
import uvicorn
from fastapi import FastAPI, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.connection import init_db, get_db
from app.database.models import User
from app.dashboard.auth import router as auth_router
from app.dashboard.routes import router as dashboard_router
from app.bot.youtube_chat import YouTubeChatMonitor
from app.bot.discord_bot import start_discord_bot
from app.services.scheduler import start_scheduler

app = FastAPI(title="Goddess Stream Manager")

# Tell FastAPI where the CSS and HTML files live
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Mount our authentication and secondary routers
app.include_router(dashboard_router)
app.include_router(auth_router)

# -----------------------------------------
# FRONTEND DASHBOARD ROUTES
# -----------------------------------------
@app.get("/")
async def serve_dashboard(request: Request, db: Session = Depends(get_db)):
    # Fetch all users to populate the Leaderboard table
    viewers = db.query(User).all()
    
    # Provide temporary settings for the UI toggles
    settings = {
        "ai_cohost_enabled": True,
        "giveaway_reminders_enabled": False
    }
    
    # Safely hand the variables over to index.html
    return templates.TemplateResponse("index.html", {
        "request": request,
        "viewers": viewers,
        "settings": settings
    })

@app.post("/toggle-ai")
async def toggle_ai(mode: str = Form(...)):
    print(f"[DASHBOARD ACTION] Toggled setting: {mode}")
    # Refresh the page instantly
    return RedirectResponse(url="/", status_code=303)

@app.post("/manual-announcement")
async def manual_announcement(message: str = Form(...)):
    print(f"[DASHBOARD ACTION] Sending manual announcement: {message}")
    # Refresh the page instantly
    return RedirectResponse(url="/", status_code=303)


# -----------------------------------------
# BACKGROUND WORKERS & STARTUP LOGIC
# -----------------------------------------
# --- THE FIX: We must store our background tasks safely ---
# Python 3.12 will forcefully "clean up" and kill any async tasks 
# if they aren't saved to a list. This protects our bots!
running_tasks = []

@app.on_event("startup")
async def startup_event():
    print("[STARTUP] Initializing systems...")
    
    # 1. Initialize SQLite tables
    init_db()
    
    # 2. Fire up background cron loops
    start_scheduler()
    
    # 3. Mount async worker integrations securely
    yt_monitor = YouTubeChatMonitor()
    
    # Save them to the global list to protect them from the Garbage Collector
    task1 = asyncio.create_task(yt_monitor.run())
    task2 = asyncio.create_task(start_discord_bot())
    
    running_tasks.append(task1)
    running_tasks.append(task2)
    
    print("[STARTUP] Web Admin Dashboard, YouTube Engine, and Discord Bot are active!")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)