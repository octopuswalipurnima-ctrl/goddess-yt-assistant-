import os
import re
import asyncio
import random
import string
import json
import urllib.request
import urllib.parse
import urllib.error
import uvicorn
import logging
import uuid
import secrets
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Depends, Form, WebSocket, WebSocketDisconnect, Response, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from sqlalchemy.orm import Session

from app.database.connection import init_db, get_db
from app.database.models import (
    User, XP, Streamer, AlertTemplate, GoalWidget, ClipRecord, 
    CustomCommand, VIPGuest, Coin, WaitingListEntry, ChatLog
)
from app.dashboard.auth import router as auth_router
from app.dashboard.routes import router as dashboard_router

# --- IMPORTING AI GLOBALS ---
from app.bot.youtube_chat import (
    YouTubeChatMonitor, DETECTED_VIDEOS, DISCONNECT_QUEUE, 
    MANUAL_MOD_MODE, AI_OBSERVER_MODE
)

from app.bot.discord_bot import start_discord_bot
from app.services.scheduler import start_scheduler, start_timed_command_loop, websub_renewal_loop
from app.services.websocket import overlay_manager
from app.api.creator_economy import router as economy_router
from app.utils.config import Config

# ---------------------------------------------------------
# COMPREHENSIVE LOGGING CONFIGURATION
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
)
logger = logging.getLogger("goddess_stream_manager")

# 1. CREATE FASTAPI INSTANCE FIRST
app = FastAPI(title="Goddess Stream Manager")

# 2. MIDDLEWARE & BROWSER SESSIONS
app.add_middleware(
    SessionMiddleware, 
    secret_key="super-secret-goddess-key-change-later",
    max_age=3600 * 24 * 7,
    https_only=False,  # Fixed: Prevents Railway proxy from dropping session cookies
    same_site="lax" 
)

app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=("*",))

if not os.path.exists("static"):
    os.makedirs("static")
if not os.path.exists("templates"):
    os.makedirs("templates")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------
# WEBSUB AUTO-SUBSCRIBER HELPER
# ---------------------------------------------------------
def subscribe_websub(channel_uc_id: str):
    def _sub():
        hub_url = "https://pubsubhubbub.appspot.com/subscribe"
        callback_url = "https://goddess-yt-assistant-production-b575.up.railway.app/api/youtube-webhook"
        topic_url = f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_uc_id}"
        data = {"hub.callback": callback_url, "hub.topic": topic_url, "hub.verify": "async", "hub.mode": "subscribe"}
        encoded_data = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(hub_url, data=encoded_data, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                logger.info(f"[WEBSUB AUTO-SUB] Subscribed {channel_uc_id} with status {resp.status}")
        except Exception as e:
            logger.error(f"[WEBSUB AUTO-SUB FAILED] {channel_uc_id}: {e}")
    asyncio.to_thread(_sub)


# ---------------------------------------------------------
# FRONTEND DASHBOARD ROUTES
# ---------------------------------------------------------
@app.get("/")
async def serve_dashboard(request: Request, db: Session = Depends(get_db)):
    try:
        logger.info("[DASHBOARD] Serving main streamer dashboard...")
        streamer_id = request.session.get("streamer_id")
        
        if not streamer_id:
            logger.info("[DASHBOARD] Unauthenticated session. Rendering logged-out view.")
            return templates.TemplateResponse(
                request=request, 
                name="index.html", 
                context={
                    "request": request, "streamer_name": None, "viewers": [], 
                    "settings": {}, "clips": [], "commands": [], "vips": [], "active_videos": []
                }
            )
            
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        
        if not streamer:
            logger.warning(f"[DASHBOARD] Session streamer ID {streamer_id} not found in database. Clearing session.")
            request.session.clear()
            return RedirectResponse(url="/", status_code=303)
        
        if not streamer.server_sync_code:
            streamer.server_sync_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            db.commit()
            
        effective_id = streamer.effective_id
            
        viewers = db.query(User).join(XP).filter(XP.streamer_id == effective_id).all()
        recent_clips = db.query(ClipRecord).filter(ClipRecord.streamer_id == effective_id).order_by(ClipRecord.id.desc()).limit(6).all()
        commands = db.query(CustomCommand).filter(CustomCommand.streamer_id == effective_id).all()
        vips = db.query(VIPGuest).filter(VIPGuest.streamer_id == effective_id).all()
        
        settings = {
            "ai_cohost_enabled": streamer.ai_cohost_enabled,
            "giveaway_reminders_enabled": streamer.giveaway_reminders_enabled,
            "server_sync_code": streamer.server_sync_code,
            "is_discord_linked": bool(streamer.discord_guild_id),
            "manual_mod_mode": MANUAL_MOD_MODE.get(effective_id, True),
            "ai_observer_mode": AI_OBSERVER_MODE.get(effective_id, True),
            "linked_primary_id": streamer.linked_primary_id,
            "sync_settings": streamer.sync_settings
        }
        
        active_video_ids = list(DETECTED_VIDEOS.keys())
        
        return templates.TemplateResponse(
            request=request, 
            name="index.html", 
            context={
                "request": request, "streamer_name": streamer.channel_name, "viewers": viewers,
                "settings": settings, "clips": recent_clips, "commands": commands, "vips": vips,
                "active_videos": active_video_ids
            }
        )
    except Exception as e:
        logger.exception(f"[DASHBOARD ERROR] Failed to render dashboard: {e}")
        return templates.TemplateResponse(request=request, name="index.html", context={"request": request, "streamer_name": None, "viewers": [], "settings": {}, "clips": [], "commands": [], "vips": [], "active_videos": []})


@app.post("/toggle-setting")
async def toggle_setting(request: Request, setting: str = Form(...), db: Session = Depends(get_db)):
    try:
        streamer_id = request.session.get("streamer_id")
        if streamer_id:
            streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
            if streamer:
                effective_id = streamer.effective_id
                if setting == "ai_cohost":
                    streamer.ai_cohost_enabled = not streamer.ai_cohost_enabled
                elif setting == "giveaways":
                    streamer.giveaway_reminders_enabled = not streamer.giveaway_reminders_enabled
                elif setting == "manual_mod_mode":
                    current_mode = MANUAL_MOD_MODE.get(effective_id, True)
                    MANUAL_MOD_MODE[effective_id] = not current_mode
                elif setting == "ai_observer_mode":
                    current_mode = AI_OBSERVER_MODE.get(effective_id, True)
                    AI_OBSERVER_MODE[effective_id] = not current_mode
                elif setting == "sync_settings":
                    streamer.sync_settings = not streamer.sync_settings
                db.commit()
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.exception(f"[SETTINGS ERROR] Failed to toggle setting '{setting}': {e}")
        return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------
# DATABASE-BACKED LIVE CHAT READER (0 API COST)
# ---------------------------------------------------------
@app.get("/api/live-chat")
async def get_live_chat(request: Request, last_id: int = 0, db: Session = Depends(get_db)):
    """Polls local database for chat messages across linked channels to save YouTube API Quota."""
    try:
        streamer_id = request.session.get("streamer_id")
        if not streamer_id:
            return {"messages": []}
            
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if not streamer:
            return {"messages": []}
            
        valid_ids = [streamer.id]
        if streamer.linked_primary_id: 
            valid_ids.append(streamer.linked_primary_id)
        linked_channels = db.query(Streamer).filter(Streamer.linked_primary_id == streamer.id).all()
        for c in linked_channels: 
            valid_ids.append(c.id)
            
        logs = db.query(ChatLog).filter(
            ChatLog.streamer_id.in_(valid_ids),
            ChatLog.id > last_id
        ).order_by(ChatLog.id.asc()).limit(50).all()
        
        messages = []
        for log in logs:
            c_name = log.streamer.channel_name if log.streamer else "Guest"
            badge = "".join([w[0] for w in c_name.split()[:2]]).upper()[:2] if c_name else "YT"
            
            messages.append({
                "id": log.id,
                "username": log.user.username if log.user else "Unknown",
                "message": log.message,
                "badge": badge,
                "time": log.timestamp.strftime("%H:%M") if log.timestamp else "Now"
            })
            
        return {"messages": messages}
    except Exception as e:
        logger.error(f"[LIVE CHAT API ERROR] {e}")
        return {"messages": []}


# ---------------------------------------------------------
# ACCOUNT LINKING / SYNC ROUTES
# ---------------------------------------------------------
@app.post("/api/account/link")
async def link_secondary_account(
    request: Request, 
    target_sync_code: str = Form(...), 
    db: Session = Depends(get_db)
):
    try:
        current_streamer_id = request.session.get("streamer_id")
        if not current_streamer_id: return RedirectResponse(url="/", status_code=303)

        current_streamer = db.query(Streamer).filter(Streamer.id == current_streamer_id).first()
        if not current_streamer: return RedirectResponse(url="/", status_code=303)

        clean_code = target_sync_code.strip().upper()
        primary_streamer = db.query(Streamer).filter(Streamer.server_sync_code == clean_code).first()

        if not primary_streamer or primary_streamer.id == current_streamer.id:
            return RedirectResponse(url="/?error=invalid_sync_code", status_code=303)

        current_streamer.linked_primary_id = primary_streamer.effective_id
        db.commit()
        return RedirectResponse(url="/?success=account_linked", status_code=303)

    except Exception as e:
        logger.exception(f"[ACCOUNT LINK ERROR] {e}")
        return RedirectResponse(url="/?error=link_failed", status_code=303)


@app.post("/api/account/unlink")
async def unlink_account(request: Request, db: Session = Depends(get_db)):
    try:
        current_streamer_id = request.session.get("streamer_id")
        if not current_streamer_id: return RedirectResponse(url="/", status_code=303)

        current_streamer = db.query(Streamer).filter(Streamer.id == current_streamer_id).first()
        if current_streamer and current_streamer.linked_primary_id:
            current_streamer.linked_primary_id = None
            db.commit()

        return RedirectResponse(url="/?success=account_unlinked", status_code=303)
    except Exception as e:
        return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------
# GUEST & PANIC BOT DEPLOYMENT ROUTES
# ---------------------------------------------------------
@app.post("/guest-join")
async def guest_join(request: Request, stream_url: str = Form(...)):
    try:
        yt_regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/live\/)([a-zA-Z0-9_-]{11})"
        match = re.search(yt_regex, stream_url)
        if match:
            DETECTED_VIDEOS[match.group(1)] = None
        return RedirectResponse(url="/?guest=true", status_code=303)
    except Exception as e:
        return RedirectResponse(url="/", status_code=303)


@app.post("/api/panic-button")
async def panic_button_protocol(request: Request, db: Session = Depends(get_db)):
    try:
        streamer_id = request.session.get("streamer_id")
        if not streamer_id: return RedirectResponse(url="/", status_code=303)
            
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if not streamer: return RedirectResponse(url="/?error=invalid_channel", status_code=303)
            
        api_key = os.environ.get("YOUTUBE_API_KEY")
        if not api_key: return RedirectResponse(url="/?error=missing_api_key", status_code=303)
            
        safe_channel_name = urllib.parse.quote(streamer.channel_name)
        search_url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={safe_channel_name}&eventType=live&type=video&key={api_key}"
        
        def fetch_live_stream():
            try:
                with urllib.request.urlopen(search_url) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as e:
                error_details = e.read().decode()
                raise Exception(f"Google API Rejected Request: {e.code} - {error_details}")
        
        data = await asyncio.to_thread(fetch_live_stream)
        
        if "items" in data and len(data["items"]) > 0:
            video_id = data["items"][0]["id"]["videoId"]
            DETECTED_VIDEOS[video_id] = streamer.effective_id
            if streamer.youtube_channel_id: subscribe_websub(streamer.youtube_channel_id)
            return RedirectResponse(url="/?success=bot_deployed", status_code=303)
        else:
            return RedirectResponse(url="/?error=not_live", status_code=303)
            
    except Exception as e:
        return RedirectResponse(url="/?error=api_crash", status_code=303)


@app.post("/api/deploy-bot")
async def deploy_bot_manually(request: Request, stream_url: str = Form(...), db: Session = Depends(get_db)):
    try:
        streamer_id = request.session.get("streamer_id")
        if not streamer_id: return RedirectResponse(url="/", status_code=303)
            
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if not streamer: return RedirectResponse(url="/", status_code=303)
            
        yt_regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/live\/)([a-zA-Z0-9_-]{11})"
        match = re.search(yt_regex, stream_url.strip())
        video_id = match.group(1) if match else stream_url.strip()
        
        if video_id:
            DETECTED_VIDEOS[video_id] = streamer.effective_id
            if streamer.youtube_channel_id: subscribe_websub(streamer.youtube_channel_id)
            if streamer.server_sync_code:
                await overlay_manager.send_alert(streamer.server_sync_code, {
                    "type": "alert", "event_type": "newSponsorEvent", "author": "🤖 SYSTEM CONNECTED",
                    "message": "mod hajir hai janab uk malik ki kami nhi hone dega 😁😸 (Mods type !checkup)", "amount": "✅ ONLINE"
                })
            return RedirectResponse(url="/?success=bot_deployed", status_code=303)
        return RedirectResponse(url="/?error=invalid_url", status_code=303)
    except Exception as e:
        return RedirectResponse(url="/?error=deploy_failed", status_code=303)


@app.post("/api/disconnect-bot")
async def disconnect_bot_manually(request: Request, video_id: str = Form(...)):
    try:
        if video_id in DETECTED_VIDEOS:
            del DETECTED_VIDEOS[video_id]
            
        DISCONNECT_QUEUE.add(video_id)
        logger.info(f"[BOT DISCONNECT] Stopped monitoring stream: {video_id}. API usage returning to 0%.")
        
        return RedirectResponse(url="/?success=disconnected", status_code=303)
    except Exception as e:
        logger.error(f"[DISCONNECT ERROR] {e}")
        return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------
# ADMIN CONTROL PANEL ROUTES
# ---------------------------------------------------------
security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    input_user = credentials.username.strip()
    input_pass = credentials.password.strip()
    if input_user == "admin" and input_pass == "goddess2026":
        return input_user
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Credentials", headers={"WWW-Authenticate": "Basic"})

@app.get("/admin")
async def serve_admin_panel(request: Request, admin_user: str = Depends(verify_admin), db: Session = Depends(get_db)):
    try:
        return templates.TemplateResponse(
            request=request, name="admin.html",
            context={"request": request, "admin_name": admin_user.capitalize(), "total_users": db.query(User).count(), "total_streamers": db.query(Streamer).count(), "all_streamers": db.query(Streamer).all(), "active_video_ids": list(DETECTED_VIDEOS.keys())}
        )
    except Exception as e:
        return RedirectResponse(url="/", status_code=303)

@app.post("/api/admin/force-join")
async def admin_force_join(request: Request, target_streamer_id: int = Form(...), stream_url: str = Form(...), admin_user: str = Depends(verify_admin), db: Session = Depends(get_db)):
    try:
        match = re.search(r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/live\/)([a-zA-Z0-9_-]{11})", stream_url.strip())
        video_id = match.group(1) if match else stream_url.strip()
        target_streamer = db.query(Streamer).filter(Streamer.id == target_streamer_id).first()
        if target_streamer:
            DETECTED_VIDEOS[video_id] = target_streamer.effective_id
        return RedirectResponse(url="/admin?success=bot_deployed", status_code=303)
    except Exception:
        return RedirectResponse(url="/admin?error=deploy_failed", status_code=303)

@app.post("/api/admin/disconnect-stream")
async def admin_disconnect_stream(request: Request, video_id: str = Form(...), admin_user: str = Depends(verify_admin)):
    if video_id in DETECTED_VIDEOS:
        del DETECTED_VIDEOS[video_id]
    DISCONNECT_QUEUE.add(video_id)
    return RedirectResponse(url="/admin?success=stream_disconnected", status_code=303)


# ---------------------------------------------------------
# WEBSUB & OBS ROUTES
# ---------------------------------------------------------
@app.get("/api/youtube-webhook")
async def verify_youtube_webhook(request: Request):
    challenge = request.query_params.get("hub.challenge")
    return Response(content=challenge, media_type="text/plain") if challenge else Response(status_code=400)

@app.post("/api/youtube-webhook")
async def receive_youtube_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        xml_data = await request.body()
        root = ET.fromstring(xml_data)
        namespaces = {'atom': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}

        entry = root.find('atom:entry', namespaces)
        if entry is not None:
            video_id_element = entry.find('yt:videoId', namespaces)
            channel_id_element = entry.find('yt:channelId', namespaces)
            
            if video_id_element is not None:
                video_id = video_id_element.text
                streamer_id = None
                if channel_id_element is not None:
                    channel_id = channel_id_element.text
                    streamer = db.query(Streamer).filter(Streamer.youtube_channel_id == channel_id).first()
                    if streamer:
                        streamer_id = streamer.effective_id
                        if streamer.server_sync_code:
                            await overlay_manager.send_alert(streamer.server_sync_code, {
                                "type": "alert", "event_type": "newSponsorEvent", "author": "🤖 SYSTEM CONNECTED",
                                "message": "mod hajir hai janab uk malik ki kami nhi hone dega 😁😸 (Mods type !checkup)", "amount": "✅ ONLINE"
                            })
                DETECTED_VIDEOS[video_id] = streamer_id

        return Response(status_code=204) 
    except Exception as e:
        return Response(status_code=200)

@app.get("/overlay/{sync_code}")
async def render_overlay(request: Request, sync_code: str):
    return templates.TemplateResponse(request=request, name="overlay.html", context={"request": request, "sync_code": sync_code, "active_theme": request.session.get("active_theme", "neon"), "custom_css": request.session.get("custom_css", "")})

@app.websocket("/ws/overlay/{sync_code}")
async def websocket_overlay(websocket: WebSocket, sync_code: str):
    await overlay_manager.connect(websocket, sync_code)
    try:
        while True:
            data = await websocket.receive_text()
    except Exception:
        overlay_manager.disconnect(websocket, sync_code)

@app.post("/test-alert")
async def test_alert(request: Request, db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        st = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if st and st.server_sync_code:
            await overlay_manager.send_alert(st.server_sync_code, {"type": "alert", "event_type": "superChatEvent", "author": "System Tester", "message": "Test Alert Working!", "amount": "$50.00"})
    return RedirectResponse(url="/", status_code=303)

@app.post("/custom-alert")
async def custom_alert(request: Request, alert_title: str = Form(...), alert_message: str = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        st = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if st and st.server_sync_code:
            await overlay_manager.send_alert(st.server_sync_code, {"type": "alert", "event_type": "newSponsorEvent", "author": alert_title, "message": alert_message, "amount": "📢 ANNOUNCEMENT"})
    return RedirectResponse(url="/", status_code=303)

@app.post("/select-theme")
async def select_theme(request: Request, theme_name: str = Form(...)):
    request.session["active_theme"] = theme_name
    return RedirectResponse(url="/?theme_updated=true", status_code=303)

@app.post("/upload-custom-widget")
async def upload_custom_widget(request: Request, custom_css: str = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        st = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if st and ("sarthak" in st.channel_name.lower() or "goddess" in st.channel_name.lower()):
            request.session["custom_css"] = custom_css
            request.session["active_theme"] = "custom"
            return RedirectResponse(url="/?custom_success=dev_bypass", status_code=303)
    return RedirectResponse(url="/?error=payment_required_20_inr", status_code=303)

@app.post("/api/save-alert-layout")
async def save_alert_layout(request: Request, layout_config: str = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        st = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if st:
            parsed = json.loads(layout_config)
            tmpl = db.query(AlertTemplate).filter(AlertTemplate.streamer_id == streamer_id).first()
            if not tmpl: db.add(AlertTemplate(streamer_id=streamer_id, config_json=parsed))
            else: tmpl.config_json = parsed
            db.commit()
            if st.server_sync_code: await overlay_manager.send_alert(st.server_sync_code, {"type": "config_update", "config": parsed})
    return RedirectResponse(url="/?success=layout_saved", status_code=303)

@app.post("/api/commands/add")
async def add_custom_command(request: Request, command_trigger: str = Form(...), response_text: str = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        st = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        eff_id = st.effective_id if st else streamer_id
        trigger = command_trigger.strip().lower() if command_trigger.strip().startswith("!") else f"!{command_trigger.strip().lower()}"
        db.add(CustomCommand(streamer_id=eff_id, command_trigger=trigger, response_text=response_text))
        db.commit()
    return RedirectResponse(url="/?success=command_added", status_code=303)

@app.post("/api/commands/delete")
async def delete_custom_command(request: Request, command_id: int = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        st = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        eff_id = st.effective_id if st else streamer_id
        cmd = db.query(CustomCommand).filter(CustomCommand.id == command_id, CustomCommand.streamer_id == eff_id).first()
        if cmd: 
            db.delete(cmd)
            db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/api/vip/add")
async def add_vip_guest(request: Request, target_username: str = Form(...), custom_reply: str = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        st = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        eff_id = st.effective_id if st else streamer_id
        target = target_username.strip().lower() if target_username.strip().startswith("@") else f"@{target_username.strip().lower()}"
        db.add(VIPGuest(streamer_id=eff_id, target_username=target, custom_reply=custom_reply))
        db.commit()
    return RedirectResponse(url="/?success=vip_added", status_code=303)

@app.post("/api/vip/delete")
async def delete_vip_guest(request: Request, vip_id: int = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        st = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        eff_id = st.effective_id if st else streamer_id
        vip = db.query(VIPGuest).filter(VIPGuest.id == vip_id, VIPGuest.streamer_id == eff_id).first()
        if vip: 
            db.delete(vip)
            db.commit()
    return RedirectResponse(url="/", status_code=303)

app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(economy_router)

running_tasks = []

@app.on_event("startup")
async def startup_event():
    init_db()
    start_scheduler()
    yt_monitor = YouTubeChatMonitor()
    running_tasks.extend([
        asyncio.create_task(yt_monitor.run()),
        asyncio.create_task(start_discord_bot()),
        asyncio.create_task(start_timed_command_loop()),
        asyncio.create_task(websub_renewal_loop())
    ])

if __name__ == "__main__":
    railway_port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=railway_port, reload=False, proxy_headers=True, forwarded_allow_ips="*")