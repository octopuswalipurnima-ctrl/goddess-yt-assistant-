from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from sqlalchemy.orm import Session
from app.database.connection import get_db
from app.database.models import User, XP, Coin, ChatLog
from app.utils.config import Config

router = APIRouter()
templates = Jinja2Templates(directory="templates")

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
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "viewers": viewers,
        "chat_logs": chat_logs,
        "settings": GLOBAL_SETTINGS
    })

@router.post("/toggle-ai")
async def toggle_ai(mode: str = Form(...)):
    if mode in GLOBAL_SETTINGS:
        GLOBAL_SETTINGS[mode] = not GLOBAL_SETTINGS[mode]
    return RedirectResponse(url="/", status_code=303)

@router.post("/manual-announcement")
async def manual_announcement(message: str = Form(...)):
    print(f"[MANUAL ANNOUNCEMENT BY ADMIN]: {message}")
    return RedirectResponse(url="/", status_code=303)