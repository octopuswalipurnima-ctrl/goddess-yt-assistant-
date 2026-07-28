import os
from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from sqlalchemy.orm import Session
from app.database.connection import get_db
from app.database.models import User, XP, Coin, ChatLog, Streamer, SystemSettings
from app.utils.config import Config
from app.bot.youtube_chat import ACTIVE_STREAMS_STATE

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

@router.get("/admin", response_class=HTMLResponse)
async def super_admin_dashboard(request: Request, db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if not streamer_id:
        return RedirectResponse(url="/", status_code=303)

    streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
    if not streamer:
        return RedirectResponse(url="/", status_code=303)

    # Check if the user is an admin
    DEV_YOUTUBE_IDS = {"@uk_hi_kahda", "@goddessislive"}
    channel_handle = streamer.channel_name.lower().replace(" ", "")
    if not channel_handle.startswith("@"):
        channel_handle = f"@{channel_handle}"

    if channel_handle not in DEV_YOUTUBE_IDS:
        print(f"[AUTH DENIED] {channel_handle} attempted to access super admin panel.", flush=True)
        return RedirectResponse(url="/", status_code=303)

    sys_settings = db.query(SystemSettings).first()
    if not sys_settings:
        sys_settings = SystemSettings()
        db.add(sys_settings)
        db.commit()
        db.refresh(sys_settings)

    all_streamers = db.query(Streamer).all()

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "active_streams": ACTIVE_STREAMS_STATE,
            "sys_settings": sys_settings,
            "streamers": all_streamers
        }
    )

@router.post("/admin/update-caps")
async def update_caps(request: Request, yt_cap: float = Form(...), gemini_cap: float = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if not streamer_id:
        return RedirectResponse(url="/", status_code=303)

    streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
    if not streamer:
        return RedirectResponse(url="/", status_code=303)

    DEV_YOUTUBE_IDS = {"@uk_hi_kahda", "@goddessislive"}
    channel_handle = streamer.channel_name.lower().replace(" ", "")
    if not channel_handle.startswith("@"):
        channel_handle = f"@{channel_handle}"

    if channel_handle not in DEV_YOUTUBE_IDS:
        return RedirectResponse(url="/", status_code=303)

    sys_settings = db.query(SystemSettings).first()
    if not sys_settings:
        sys_settings = SystemSettings()
        db.add(sys_settings)

    sys_settings.yt_api_cap = yt_cap
    sys_settings.gemini_api_cap = gemini_cap
    db.commit()

    return RedirectResponse(url="/admin", status_code=303)

@router.post("/toggle-ai")
async def toggle_ai(mode: str = Form(...)):
    if mode in GLOBAL_SETTINGS:
        GLOBAL_SETTINGS[mode] = not GLOBAL_SETTINGS[mode]
    return RedirectResponse(url="/", status_code=303)

@router.post("/manual-announcement")
async def manual_announcement(message: str = Form(...)):
    print(f"[MANUAL ANNOUNCEMENT BY ADMIN]: {message}")
    return RedirectResponse(url="/", status_code=303)