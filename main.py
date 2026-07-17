import os
import re
import asyncio
import random
import string
import json
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
from app.database.models import User, XP, Streamer, AlertTemplate, GoalWidget
from app.dashboard.auth import router as auth_router
from app.dashboard.routes import router as dashboard_router
from app.bot.youtube_chat import YouTubeChatMonitor, DETECTED_VIDEOS
from app.bot.discord_bot import start_discord_bot
from app.services.scheduler import start_scheduler
from app.services.websocket import overlay_manager

# --- NEW EXTENSIONS ROUTER IMPORT ---
from app.api.creator_economy import router as economy_router

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
# FRONTEND DASHBOARD ROUTES
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
    active_theme = request.session.get("active_theme", "neon")
    custom_css = request.session.get("custom_css", "")
    return templates.TemplateResponse(
        request=request,
        name="overlay.html", 
        context={"request": request, "sync_code": sync_code, "active_theme": active_theme, "custom_css": custom_css}
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

@app.post("/custom-alert")
async def custom_alert(
    request: Request,
    alert_title: str = Form(...),
    alert_message: str = Form(...),
    db: Session = Depends(get_db)
):
    """Allows streamers to fire custom on-screen widgets via the dashboard."""
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if streamer and streamer.server_sync_code:
            custom_payload = {
                "type": "alert",
                "event_type": "newSponsorEvent",
                "author": alert_title,
                "message": alert_message,
                "amount": "📢 ANNOUNCEMENT"
            }
            await overlay_manager.send_alert(streamer.server_sync_code, custom_payload)
    return RedirectResponse(url="/", status_code=303)

@app.post("/select-theme")
async def select_theme(request: Request, theme_name: str = Form(...), db: Session = Depends(get_db)):
    """Handles selection of free inbuilt themes."""
    request.session["active_theme"] = theme_name
    return RedirectResponse(url="/?theme_updated=true", status_code=303)

@app.post("/upload-custom-widget")
async def upload_custom_widget(
    request: Request, 
    custom_css: str = Form(...), 
    db: Session = Depends(get_db)
):
    """Gatekeeper route: Charges ₹20 unless the user is a Dev (Sarthak or Goddess)."""
    streamer_id = request.session.get("streamer_id")
    if not streamer_id:
        return RedirectResponse(url="/", status_code=303)
        
    streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
    if not streamer:
        return RedirectResponse(url="/", status_code=303)

    # THE DEV BYPASS LOGIC
    channel_name = streamer.channel_name.lower()
    is_dev = "sarthak" in channel_name or "goddess" in channel_name

    if is_dev:
        print(f"[SYSTEM] Dev bypass authorized for {streamer.channel_name}. Uploading widget for free.")
        request.session["custom_css"] = custom_css
        request.session["active_theme"] = "custom"
        return RedirectResponse(url="/?custom_success=dev_bypass", status_code=303)
    else:
        print(f"[SYSTEM] Standard user {streamer.channel_name} attempted custom upload. Redirecting to payment gateway.")
        return RedirectResponse(url="/?error=payment_required_20_inr", status_code=303)

# ---------------------------------------------------------
# VISUAL ENGINE AND GOAL ROUTES
# ---------------------------------------------------------
@app.post("/api/save-alert-layout")
async def save_alert_layout(request: Request, layout_config: str = Form(...), db: Session = Depends(get_db)):
    """Saves the output from the visual Alert Builder. Guarded by the Premium Gate."""
    streamer_id = request.session.get("streamer_id")
    if not streamer_id:
        return RedirectResponse(url="/", status_code=303)
        
    streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
    if not streamer:
         return RedirectResponse(url="/", status_code=303)
    
    # REUSE EXISTING PREMIUM LOGIC
    channel_name = streamer.channel_name.lower()
    is_dev = "sarthak" in channel_name or "goddess" in channel_name
    has_paid = request.session.get("has_paid_premium", False) 
    
    if not (is_dev or has_paid):
        return RedirectResponse(url="/?error=payment_required_20_inr", status_code=303)
        
    try:
        parsed_config = json.loads(layout_config)
    except json.JSONDecodeError:
         return RedirectResponse(url="/?error=invalid_json", status_code=303)
    
    # Update or create template
    template = db.query(AlertTemplate).filter(AlertTemplate.streamer_id == streamer_id).first()
    if not template:
        template = AlertTemplate(streamer_id=streamer_id, config_json=parsed_config)
        db.add(template)
    else:
        template.config_json = parsed_config
    
    db.commit()
    
    # Instantly push the new config to OBS via WebSocket (No reload needed)
    if streamer.server_sync_code:
         await overlay_manager.send_alert(streamer.server_sync_code, {"type": "config_update", "config": parsed_config})
         
    return RedirectResponse(url="/?success=layout_saved", status_code=303)

@app.post("/api/update-goal")
async def update_goal(request: Request, goal_id: int = Form(...), amount: int = Form(...), db: Session = Depends(get_db)):
    """Fired by the backend when a sub/dono happens to progress the goal bar."""
    goal = db.query(GoalWidget).filter(GoalWidget.id == goal_id).first()
    if goal:
        goal.current_amount += amount
        db.commit()
        # Ping OBS to animate the progress bar filling up
        streamer = db.query(Streamer).filter(Streamer.id == goal.streamer_id).first()
        if streamer and streamer.server_sync_code:
            await overlay_manager.send_alert(streamer.server_sync_code, {
                "type": "goal_update",
                "goal_id": goal.id,
                "current": goal.current_amount,
                "target": goal.target_amount
            })
    return {"status": "success"}

# ---------------------------------------------------------
# NEW: AI MODERATION ENDPOINTS
# ---------------------------------------------------------
@app.post("/api/moderation/process-message")
async def process_chat_message(
    request: Request, 
    user_id: int = Form(...),
    message_text: str = Form(...), 
    db: Session = Depends(get_db)
):
    """
    Highly optimized multi-layered moderation pipeline framework.
    Evaluates local rules, cross-references trust vectors, and consults Gemini when required.
    """
    streamer_id = request.session.get("streamer_id")
    if not streamer_id:
        return {"verdict": "Ignored", "action": "None", "reason": "No active session authentication."}

    # Gather context tracking history from recent active database chat configurations
    from app.database.models import ChatLog, ViewerTrust, ModActionLog
    from app.services.moderation.rule_engine import LocalRuleEngine
    from app.services.moderation.gemini_client import GeminiModeratorEngine
    
    recent_logs = db.query(ChatLog).filter(ChatLog.streamer_id == streamer_id).order_by(ChatLog.timestamp.desc()).limit(10).all()
    context_list = [log.message for log in reversed(recent_logs)]

    # Layer 0: Trust Vectors Bypass Logic
    trust = db.query(ViewerTrust).filter(ViewerTrust.user_id == user_id, ViewerTrust.streamer_id == streamer_id).first()
    if trust and (trust.is_whitelisted or trust.trust_score > 85.0):
        return {"verdict": "Safe", "action": "None", "reason": "High user trust score bypass applied."}

    # Layer 1: Run Local Rule Engine Engine
    local_engine = LocalRuleEngine()
    local_eval = local_engine.evaluate(message_text)
    
    if local_eval["verdict"] != "Questionable":
        # Log action locally and terminate early to save API cost
        log_entry = ModActionLog(
            streamer_id=streamer_id, message_content=message_text,
            layer_triggered="Layer 1 (Local)", classification=local_eval["verdict"],
            recommended_action=local_eval["verdict"], reason=local_eval["reason"]
        )
        db.add(log_entry)
        db.commit()
        return {"verdict": local_eval["verdict"], "action": local_eval["verdict"], "reason": local_eval["reason"]}

    # Layer 2: Run Gemini Contextual Intelligence Module
    ai_engine = GeminiModeratorEngine(db)
    ai_verdict = await ai_engine.analyze_message(message_text, context_list)

    # Commit action logging vectors
    log_entry = ModActionLog(
        streamer_id=streamer_id, message_content=message_text,
        layer_triggered="Layer 2 (Gemini AI)", classification=ai_verdict.get("classification"),
        recommended_action=ai_verdict.get("recommended_action"), reason=ai_verdict.get("reason")
    )
    db.add(log_entry)
    db.commit()

    return {
        "verdict": ai_verdict.get("recommended_action"),
        "action": ai_verdict.get("recommended_action"),
        "reason": ai_verdict.get("reason"),
        "confidence": ai_verdict.get("confidence")
    }

# ---------------------------------------------------------
# MOUNT ADDITIONAL ROUTERS
# ---------------------------------------------------------
app.include_router(dashboard_router)
app.include_router(auth_router)

# --- MOUNT CREATOR ECONOMY PIPELINES ---
app.include_router(economy_router)


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