import os
from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from sqlalchemy.orm import Session
from app.database.connection import get_db
from app.database.models import User, XP, Coin, ChatLog
from app.utils.config import Config

router = APIRouter()

# --- Absolute GPS Pathing for Templates ---
# 1. Find the exact folder this routes.py file is sitting in (dashboard folder)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# 2. Go UP to the 'app' folder, then UP again to the main root folder
ROOT_DIR = os.path.dirname(os.path.dirname(CURRENT_DIR))

# 3. Lock in the absolute path to the templates folder sitting in the root
TEMPLATES_DIR = os.path.join(ROOT_DIR, "templates")

# 4. Mount the templates using the unbreakable absolute path
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Simple state tracking for AI configurations
GLOBAL_SETTINGS = {
    "ai_cohost_enabled": True,
    "giveaway_reminders_enabled": True
}

@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    # Pull leaderboards and stats for display
    viewers = db.query(User).all()
    chat_logs = db.query(ChatLog).order_by(ChatLog.id.desc()).limit(10).all()
    
    # Updated return syntax required by newer versions of FastAPI
    return templates.TemplateResponse(
        request=request,
        name="index.html", 
        context={
            "viewers": viewers,
            "chat_logs": chat_logs,
            "settings": GLOBAL_SETTINGS
        }
    )

@router.post("/toggle-ai")
async def toggle_ai(mode: str = Form(...)):
    if mode in GLOBAL_SETTINGS:
        GLOBAL_SETTINGS[mode] = not GLOBAL_SETTINGS[mode]
    return RedirectResponse(url="/", status_code=303)

@router.post("/manual-announcement")
async def manual_announcement(message: str = Form(...)):
    print(f"[MANUAL ANNOUNCEMENT BY ADMIN]: {message}")
    return RedirectResponse(url="/", status_code=303)