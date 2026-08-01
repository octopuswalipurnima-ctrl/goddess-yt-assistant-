import os
from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from app.database.connection import get_db
from app.database.models import SystemState, Streamer

router = APIRouter(prefix="/admin", tags=["Admin Panel"])

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(CURRENT_DIR))
TEMPLATES_DIR = os.path.join(ROOT_DIR, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

def get_system_state(db: Session):
    state = db.query(SystemState).first()
    if not state:
        state = SystemState()
        db.add(state)
        db.commit()
        db.refresh(state)
    return state

@router.get("/", response_class=HTMLResponse)
async def admin_panel(request: Request, db: Session = Depends(get_db)):
    state = get_system_state(db)
    streamers = db.query(Streamer).all()

    # Query the yt_monitor state if it exists
    yt_monitor = getattr(request.app.state, "yt_monitor", None)
    active_streams = []

    if yt_monitor:
        for stream_key, chat_info in yt_monitor.active_streams.items():
            if isinstance(chat_info, dict):
                start_time = chat_info.get("start_time")
                if start_time:
                    uptime = datetime.now(timezone.utc) - start_time
                    uptime_str = str(uptime).split('.')[0] # Remove microseconds
                else:
                    uptime_str = "Unknown"

                chat_id = chat_info.get("chat_id")
            else:
                chat_id = chat_info
                uptime_str = "Unknown"

            # Resolve streamer name
            name = f"Guest/Unknown ({stream_key})"
            if isinstance(stream_key, int) or str(stream_key).isdigit():
                 st = db.query(Streamer).filter(Streamer.id == int(stream_key)).first()
                 if st: name = st.channel_name

            active_streams.append({"name": name, "chat_id": chat_id, "uptime": uptime_str})

    return templates.TemplateResponse(
        request=request,
        name="admin/admin_panel.html",
        context={
            "state": state,
            "streamers": streamers,
            "active_streams": active_streams
        }
    )

@router.post("/update-caps")
async def update_caps(
    request: Request,
    youtube_cap: int = Form(...),
    gemini_cap: int = Form(...),
    db: Session = Depends(get_db)
):
    state = get_system_state(db)
    state.youtube_api_cap = youtube_cap
    state.gemini_api_cap = gemini_cap
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)
