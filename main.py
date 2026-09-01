import os

# Force load the .env file immediately so the app never crashes from missing keys
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, Depends, Form, Response, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal, init_db, get_db
from app.database.models import (
    User, XP, Streamer, AlertTemplate, GoalWidget, ClipRecord, 
    CustomCommand, VIPGuest, Coin, WaitingListEntry, ChatLog,
    TeamMember, TeamInvite, AuditLog
)

# --- IMPORTING AI GLOBALS ---
from app.bot.youtube_chat import (
    YouTubeChatMonitor, DETECTED_VIDEOS, DISCONNECT_QUEUE, 
    MANUAL_MOD_MODE, AI_OBSERVER_MODE
)

from app.bot.discord_bot import start_discord_bot
from app.services.scheduler import start_scheduler, start_timed_command_loop, websub_renewal_loop
from app.api.creator_economy import router as economy_router
from app.utils.config import Config
from app.services.emergency_stop import emergency_stop
from app.services.discord_events import discord_events
from app.services.youtube.yt_api_manager import yt_api_manager
from app.services.youtube.monitored_channels import ensure_monitored_channels

# Import the new AI Trainer Service
from app.services.ai_trainer import trainer_service

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
    secret_key=Config.SESSION_SECRET or secrets.token_urlsafe(32),
    max_age=3600 * 24 * 7,
    https_only=Config.SESSION_HTTPS_ONLY,
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
# SECURITY: ROLE-BASED ACCESS CONTROL (RBAC)
# ---------------------------------------------------------
def ensure_direct_dashboard_workspace(request: Request, db: Session) -> Streamer:
    """Initialize a dashboard workspace without fabricating a website user."""
    streamer = db.query(Streamer).order_by(Streamer.id.asc()).first()
    if not streamer:
        streamer = Streamer(youtube_channel_id=None, channel_name="Dashboard Channel", is_active=False)
        db.add(streamer); db.flush()
    db.commit()
    # Clear the retired website-login principal.  A dashboard workspace is
    # selected by its streamer id, not by a fabricated row in `users`.
    request.session.pop("user_id", None)
    request.session.update({"streamer_id": request.session.get("streamer_id") or streamer.id, "streamer_name": streamer.channel_name})
    return streamer


def require_role(allowed_roles: list):
    async def role_checker(request: Request, db: Session = Depends(get_db)):
        active_dashboard_id = request.session.get("streamer_id")
        if not active_dashboard_id:
            streamer = ensure_direct_dashboard_workspace(request, db)
            active_dashboard_id = streamer.id

        streamer = db.query(Streamer).filter(Streamer.id == active_dashboard_id).first()
        current_role = "owner" if streamer else None

        if not current_role or current_role not in allowed_roles:
            try:
                db.add(AuditLog(
                    streamer_id=active_dashboard_id,
                    user_id=None,
                    action="UNAUTHORIZED_ACCESS_ATTEMPT",
                    details=f"Attempted to access restricted route requiring {allowed_roles}"
                ))
                db.commit()
            except Exception:
                db.rollback()
            raise HTTPException(status_code=303, headers={"Location": "/?error=unauthorized_role"})
            
        return {"user_id": None, "role": current_role}
    return role_checker


# ---------------------------------------------------------
# WEBSUB AUTO-SUBSCRIBER HELPER
# ---------------------------------------------------------
YOUTUBE_CHANNEL_ID_RE = re.compile(r"^UC[a-zA-Z0-9_-]{22}$")
websub_start_lock = asyncio.Lock()

def websub_topic(channel_uc_id: str) -> str:
    return f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_uc_id}"

def subscribe_websub(channel_uc_id: str, mode: str = "subscribe") -> bool:
    """Use the existing WebSub hub; callers never block dashboard responses."""
    if not YOUTUBE_CHANNEL_ID_RE.fullmatch(channel_uc_id) or mode not in {"subscribe", "unsubscribe"}:
        return False
    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    if not base_url:
        logger.warning("WebSub request skipped: BASE_URL is not configured")
        return False
    def _sub():
        hub_url = "https://pubsubhubbub.appspot.com/subscribe"
        callback_url = f"{base_url}/api/youtube-webhook"
        data = {"hub.callback": callback_url, "hub.topic": websub_topic(channel_uc_id), "hub.verify": "async", "hub.mode": mode}
        encoded_data = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(hub_url, data=encoded_data, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                logger.info("WebSub %s request accepted for configured channel (status %s)", mode, resp.status)
        except Exception as e:
            logger.error("WebSub %s request failed: %s", mode, type(e).__name__)
    asyncio.create_task(asyncio.to_thread(_sub))
    return True


# ---------------------------------------------------------
# WORKSPACE SWITCHER ROUTE
# ---------------------------------------------------------
@app.get("/switch-workspace/{target_id}")
async def switch_workspace(target_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        if not request.session.get("streamer_id"):
            ensure_direct_dashboard_workspace(request, db)
            
        streamer = db.query(Streamer).filter(Streamer.id == target_id).first()
        if not streamer:
            return RedirectResponse(url="/?error=not_found", status_code=303)
            
        request.session["streamer_id"] = target_id
                
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.error(f"[WORKSPACE SWITCH ERROR] {e}")
        return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------
# FRONTEND DASHBOARD ROUTES
# ---------------------------------------------------------
@app.get("/")
async def serve_dashboard(request: Request, db: Session = Depends(get_db)):
    try:
        # A stale cookie from the retired website-login flow must not become a
        # dashboard identity or trigger a users-table write.
        request.session.pop("user_id", None)
        my_streamer = db.query(Streamer).filter(Streamer.id == request.session.get("streamer_id")).first()
        if not my_streamer:
            my_streamer = ensure_direct_dashboard_workspace(request, db)

        active_streamer_id = request.session.get("streamer_id")
        if not active_streamer_id:
            active_streamer_id = my_streamer.id
            request.session["streamer_id"] = active_streamer_id
            
        streamer = db.query(Streamer).filter(Streamer.id == active_streamer_id).first()
        if not streamer:
            active_streamer_id = my_streamer.id
            request.session["streamer_id"] = active_streamer_id
            streamer = my_streamer
            
        if not streamer.server_sync_code:
            streamer.server_sync_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            db.commit()

        workspaces = [{"id": my_streamer.id, "name": my_streamer.channel_name, "role": "owner"}]

        effective_id = streamer.effective_id
        viewers = db.query(User).join(XP).filter(XP.streamer_id == effective_id).all()
        recent_clips = db.query(ClipRecord).filter(ClipRecord.streamer_id == effective_id).order_by(ClipRecord.id.desc()).limit(6).all()
        commands = db.query(CustomCommand).filter(CustomCommand.streamer_id == effective_id).all()
        vips = db.query(VIPGuest).filter(VIPGuest.streamer_id == effective_id).all()
        
        ui_role = "viewer"
        ui_role = "owner"
        
        settings = {
            "is_active": getattr(streamer, 'is_active', False),
            "ai_cohost_enabled": streamer.ai_cohost_enabled,
            "giveaway_reminders_enabled": streamer.giveaway_reminders_enabled,
            "server_sync_code": streamer.server_sync_code,
            "youtube_channel_id": streamer.youtube_channel_id,
            "is_discord_linked": bool(streamer.discord_guild_id),
            "manual_mod_mode": MANUAL_MOD_MODE.get(effective_id, True),
            "ai_observer_mode": AI_OBSERVER_MODE.get(effective_id, True),
            "linked_primary_id": streamer.linked_primary_id,
            "sync_settings": streamer.sync_settings,
            "user_role": ui_role,
            "active_streamer_id": active_streamer_id
        }
        
        return templates.TemplateResponse(
            request=request, name="index.html", 
            context={"request": request, "streamer_name": streamer.channel_name, "viewers": viewers, "settings": settings, "clips": recent_clips, "commands": commands, "vips": vips, "active_videos": list(DETECTED_VIDEOS.keys()), "workspaces": workspaces}
        )
    except Exception as e:
        logger.error(f"[DASHBOARD ERROR] {e}")
        return templates.TemplateResponse(request=request, name="index.html", context={"request": request, "streamer_name": None, "viewers": [], "settings": {}, "clips": [], "commands": [], "vips": [], "active_videos": [], "workspaces": []})


@app.get("/dashboard")
async def dashboard_alias(request: Request, db: Session = Depends(get_db)):
    """Serve the same dashboard for the explicit dashboard URL, no auth redirect."""
    return await serve_dashboard(request, db)


@app.post("/api/websub/channel")
async def save_websub_channel(request: Request, youtube_channel_id: str = Form(...), db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner"]))):
    channel_id = youtube_channel_id.strip()
    if not YOUTUBE_CHANNEL_ID_RE.fullmatch(channel_id):
        return RedirectResponse(url="/?error=invalid_channel_id", status_code=303)
    streamer = db.query(Streamer).filter_by(id=request.session.get("streamer_id")).first()
    streamer.youtube_channel_id = channel_id
    db.add(AuditLog(streamer_id=streamer.id, user_id=auth["user_id"], action="YOUTUBE_CHANNEL_SET", details=channel_id))
    db.commit()
    return RedirectResponse(url="/?success=channel_saved", status_code=303)


@app.post("/api/websub/{action}")
async def control_websub(action: str, request: Request, db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner"]))):
    streamer = db.query(Streamer).filter_by(id=request.session.get("streamer_id")).first()
    mode = "subscribe" if action == "enable" else "unsubscribe" if action == "disable" else None
    if not mode or not streamer or not streamer.youtube_channel_id or not subscribe_websub(streamer.youtube_channel_id, mode):
        return RedirectResponse(url="/?error=websub_unavailable", status_code=303)
    db.add(AuditLog(streamer_id=streamer.id, user_id=auth["user_id"], action=f"WEBSUB_{mode.upper()}", details=streamer.youtube_channel_id))
    db.commit()
    return RedirectResponse(url=f"/?success=websub_{mode}d", status_code=303)


@app.post("/toggle-setting")
async def toggle_setting(
    request: Request, 
    setting: str = Form(...), 
    db: Session = Depends(get_db),
    auth: dict = Depends(require_role(["owner", "manager"]))
):
    try:
        streamer_id = request.session.get("streamer_id")
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        effective_id = streamer.effective_id
        
        # ⚡ SMART POWER TOGGLE
        if setting == "bot_power": streamer.is_active = not streamer.is_active
        elif setting == "ai_cohost": streamer.ai_cohost_enabled = not streamer.ai_cohost_enabled
        elif setting == "giveaways": streamer.giveaway_reminders_enabled = not streamer.giveaway_reminders_enabled
        elif setting == "manual_mod_mode": MANUAL_MOD_MODE[effective_id] = not MANUAL_MOD_MODE.get(effective_id, True)
        elif setting == "ai_observer_mode": AI_OBSERVER_MODE[effective_id] = not AI_OBSERVER_MODE.get(effective_id, True)
        elif setting == "sync_settings": streamer.sync_settings = not streamer.sync_settings
        
        db.add(AuditLog(streamer_id=streamer_id, user_id=auth["user_id"], action="TOGGLE_SETTING", details=f"{setting} changed"))
        db.commit()
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.error(f"[TOGGLE SETTING ERROR] {e}")
        db.rollback()
        return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------
# TEAM & MAGIC INVITE ROUTES
# ---------------------------------------------------------
@app.post("/api/team/generate-invite")
async def generate_team_invite(
    request: Request, 
    role: str = Form(...), 
    db: Session = Depends(get_db),
    auth: dict = Depends(require_role(["owner", "manager"]))
):
    try:
        streamer_id = request.session.get("streamer_id")
        invite_token = f"inv_{secrets.token_hex(12)}"
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        
        db.add(TeamInvite(streamer_id=streamer_id, invite_code=invite_token, role=role, created_by_id=auth["user_id"], expires_at=expires))
        db.add(AuditLog(streamer_id=streamer_id, user_id=auth["user_id"], action="GENERATE_INVITE", details=f"Generated {role} invite link."))
        db.commit()
        
        base_url = str(request.base_url).rstrip("/")
        magic_link = f"{base_url}/invite/{invite_token}"
        safe_link = urllib.parse.quote(magic_link, safe=":/") 
        
        return RedirectResponse(url=f"/?invite_generated={safe_link}", status_code=303)
    except Exception as e:
        logger.error(f"[GENERATE INVITE ERROR] {e}")
        db.rollback()
        return RedirectResponse(url="/?error=server_crash", status_code=303)


@app.get("/invite/{invite_code}")
async def accept_team_invite(invite_code: str, request: Request, db: Session = Depends(get_db)):
    try:
        invite = db.query(TeamInvite).filter(TeamInvite.invite_code == invite_code, TeamInvite.is_used == False).first()
        
        if not invite or invite.expires_at < datetime.now(timezone.utc):
            return RedirectResponse(url="/?error=invalid_invite", status_code=303)
            
        # Website login has been retired, so an invite selects a workspace but
        # does not manufacture a `users` identity or a fake team membership.
        invite.is_used = True
        db.add(AuditLog(streamer_id=invite.streamer_id, user_id=None, action="JOINED_TEAM", details=f"Direct dashboard invite accepted as {invite.role}."))
        db.commit()
        
        request.session["streamer_id"] = invite.streamer_id
        return RedirectResponse(url="/?success=team_joined", status_code=303)
    except Exception as e:
        logger.error(f"[ACCEPT INVITE ERROR] {e}")
        db.rollback()
        return RedirectResponse(url="/?error=server_crash", status_code=303)


# ---------------------------------------------------------
# DATABASE-BACKED LIVE CHAT READER
# ---------------------------------------------------------
@app.get("/api/live-chat")
async def get_live_chat(request: Request, last_id: int = 0, db: Session = Depends(get_db)):
    try:
        streamer_id = request.session.get("streamer_id")
        if not streamer_id: return {"messages": []}
            
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if not streamer: return {"messages": []}
            
        valid_ids = [streamer.id]
        if streamer.linked_primary_id: valid_ids.append(streamer.linked_primary_id)
        for c in db.query(Streamer).filter(Streamer.linked_primary_id == streamer.id).all(): valid_ids.append(c.id)
            
        logs = db.query(ChatLog).filter(ChatLog.streamer_id.in_(valid_ids), ChatLog.id > last_id).order_by(ChatLog.id.asc()).limit(50).all()
        
        messages = []
        for log in logs:
            c_name = log.streamer.channel_name if log.streamer else "Guest"
            badge = "".join([w[0] for w in c_name.split()[:2]]).upper()[:2] if c_name else "YT"
            messages.append({"id": log.id, "username": log.user.username if log.user else "Unknown", "message": log.message, "badge": badge, "time": log.timestamp.strftime("%H:%M") if log.timestamp else "Now"})
            
        return {"messages": messages}
    except Exception as e:
        return {"messages": []}


# ---------------------------------------------------------
# ACCOUNT LINKING / SYNC ROUTES
# ---------------------------------------------------------
@app.post("/api/account/link")
async def link_secondary_account(request: Request, target_sync_code: str = Form(...), db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner"]))):
    try:
        current_streamer = db.query(Streamer).filter(Streamer.id == request.session.get("streamer_id")).first()
        primary_streamer = db.query(Streamer).filter(Streamer.server_sync_code == target_sync_code.strip().upper()).first()

        if not primary_streamer or primary_streamer.id == current_streamer.id: return RedirectResponse(url="/?error=invalid_sync_code", status_code=303)

        current_streamer.linked_primary_id = primary_streamer.effective_id
        db.add(AuditLog(streamer_id=current_streamer.id, user_id=auth["user_id"], action="ACCOUNT_LINKED", details=f"Linked to primary ID {primary_streamer.effective_id}"))
        db.commit()
        return RedirectResponse(url="/?success=account_linked", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/?error=link_failed", status_code=303)


@app.post("/api/account/unlink")
async def unlink_account(request: Request, db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner"]))):
    try:
        current_streamer = db.query(Streamer).filter(Streamer.id == request.session.get("streamer_id")).first()
        if current_streamer and current_streamer.linked_primary_id:
            current_streamer.linked_primary_id = None
            db.add(AuditLog(streamer_id=current_streamer.id, user_id=auth["user_id"], action="ACCOUNT_UNLINKED", details="Account sync severed."))
            db.commit()
        return RedirectResponse(url="/?success=account_unlinked", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/?error=server_crash", status_code=303)


# ---------------------------------------------------------
# GUEST & PANIC BOT DEPLOYMENT ROUTES
# ---------------------------------------------------------
@app.post("/guest-join")
async def guest_join(request: Request, stream_url: str = Form(...)):
    yt_regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/live\/)([a-zA-Z0-9_-]{11})"
    match = re.search(yt_regex, stream_url)
    if match: DETECTED_VIDEOS[match.group(1)] = None
    return RedirectResponse(url="/?guest=true", status_code=303)


@app.post("/api/panic-button")
async def panic_button_protocol(request: Request, db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner", "manager", "moderator"]))):
    try:
        streamer = db.query(Streamer).filter(Streamer.id == request.session.get("streamer_id")).first()
        api_key = os.environ.get("YOUTUBE_API_KEY")
        if not api_key: return RedirectResponse(url="/?error=missing_api_key", status_code=303)
            
        safe_channel_name = urllib.parse.quote(streamer.channel_name)
        search_url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={safe_channel_name}&eventType=live&type=video&key={api_key}"
        
        data = await asyncio.to_thread(lambda: json.loads(urllib.request.urlopen(search_url).read().decode()))
        
        if "items" in data and len(data["items"]) > 0:
            video_id = data["items"][0]["id"]["videoId"]
            DETECTED_VIDEOS[video_id] = streamer.effective_id
            if streamer.youtube_channel_id: subscribe_websub(streamer.youtube_channel_id)
            
            db.add(AuditLog(streamer_id=streamer.id, user_id=auth["user_id"], action="BOT_DEPLOYED", details=f"Panic button deployed bot to {video_id}"))
            db.commit()
            return RedirectResponse(url="/?success=bot_deployed", status_code=303)
        return RedirectResponse(url="/?error=not_live", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/?error=api_crash", status_code=303)


@app.post("/api/deploy-bot")
async def deploy_bot_manually(request: Request, stream_url: str = Form(...), db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner", "manager", "moderator"]))):
    try:
        streamer = db.query(Streamer).filter(Streamer.id == request.session.get("streamer_id")).first()
        yt_regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/live\/)([a-zA-Z0-9_-]{11})"
        match = re.search(yt_regex, stream_url.strip())
        video_id = match.group(1) if match else stream_url.strip()
        
        if video_id:
            DETECTED_VIDEOS[video_id] = streamer.effective_id
            if streamer.youtube_channel_id: subscribe_websub(streamer.youtube_channel_id)
            db.add(AuditLog(streamer_id=streamer.id, user_id=auth["user_id"], action="BOT_DEPLOYED", details=f"Manually deployed to {video_id}"))
            db.commit()
            return RedirectResponse(url="/?success=bot_deployed", status_code=303)
        return RedirectResponse(url="/?error=invalid_url", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/?error=server_crash", status_code=303)


# ⚡ AUTO-POWER OFF WHEN DISCONNECTED
@app.post("/api/disconnect-bot")
async def disconnect_bot_manually(request: Request, video_id: str = Form(...), db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner", "manager", "moderator"]))):
    try:
        if video_id in DETECTED_VIDEOS: del DETECTED_VIDEOS[video_id]
        DISCONNECT_QUEUE.add(video_id)
        
        streamer = db.query(Streamer).filter(Streamer.id == request.session.get("streamer_id")).first()
        if streamer:
            streamer.is_active = False # Power OFF the bot to save quota
            
            valid_ids = [streamer.id]
            if streamer.linked_primary_id: valid_ids.append(streamer.linked_primary_id)
            for c in db.query(Streamer).filter(Streamer.linked_primary_id == streamer.id).all(): valid_ids.append(c.id)
            db.query(ChatLog).filter(ChatLog.streamer_id.in_(valid_ids)).delete(synchronize_session=False)
            
            db.add(AuditLog(streamer_id=streamer.id, user_id=auth["user_id"], action="BOT_DISCONNECTED", details=f"Severed bot from {video_id}. Power set to OFF."))
            db.commit()
            
        return RedirectResponse(url="/?success=disconnected", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/?error=server_crash", status_code=303)


# ---------------------------------------------------------
# COMMANDS & VIP MANAGEMENT
# ---------------------------------------------------------
@app.post("/api/commands/add")
async def add_custom_command(request: Request, command_trigger: str = Form(...), response_text: str = Form(...), db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner", "manager", "moderator", "editor"]))):
    try:
        streamer = db.query(Streamer).filter(Streamer.id == request.session.get("streamer_id")).first()
        eff_id = streamer.effective_id if streamer else request.session.get("streamer_id")
        trigger = command_trigger.strip().lower() if command_trigger.strip().startswith("!") else f"!{command_trigger.strip().lower()}"
        
        db.add(CustomCommand(streamer_id=eff_id, command_trigger=trigger, response_text=response_text))
        db.add(AuditLog(streamer_id=streamer.id, user_id=auth["user_id"], action="COMMAND_CREATED", details=f"Created {trigger}"))
        db.commit()
        return RedirectResponse(url="/?success=command_added", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/?error=server_crash", status_code=303)


@app.post("/api/commands/delete")
async def delete_custom_command(request: Request, command_id: int = Form(...), db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner", "manager", "moderator", "editor"]))):
    try:
        streamer = db.query(Streamer).filter(Streamer.id == request.session.get("streamer_id")).first()
        eff_id = streamer.effective_id if streamer else request.session.get("streamer_id")
        cmd = db.query(CustomCommand).filter(CustomCommand.id == command_id, CustomCommand.streamer_id == eff_id).first()
        
        if cmd: 
            trigger = cmd.command_trigger
            db.delete(cmd)
            db.add(AuditLog(streamer_id=streamer.id, user_id=auth["user_id"], action="COMMAND_DELETED", details=f"Deleted {trigger}"))
            db.commit()
        return RedirectResponse(url="/", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/?error=server_crash", status_code=303)


@app.post("/api/vip/add")
async def add_vip_guest(request: Request, target_username: str = Form(...), custom_reply: str = Form(...), db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner", "manager", "editor"]))):
    try:
        streamer = db.query(Streamer).filter(Streamer.id == request.session.get("streamer_id")).first()
        eff_id = streamer.effective_id if streamer else request.session.get("streamer_id")
        target = target_username.strip().lower() if target_username.strip().startswith("@") else f"@{target_username.strip().lower()}"
        
        db.add(VIPGuest(streamer_id=eff_id, target_username=target, custom_reply=custom_reply))
        db.add(AuditLog(streamer_id=streamer.id, user_id=auth["user_id"], action="VIP_ADDED", details=f"Added {target} to VIP Greeter"))
        db.commit()
        return RedirectResponse(url="/?success=vip_added", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/?error=server_crash", status_code=303)


@app.post("/api/vip/delete")
async def delete_vip_guest(request: Request, vip_id: int = Form(...), db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner", "manager", "editor"]))):
    try:
        streamer = db.query(Streamer).filter(Streamer.id == request.session.get("streamer_id")).first()
        eff_id = streamer.effective_id if streamer else request.session.get("streamer_id")
        vip = db.query(VIPGuest).filter(VIPGuest.id == vip_id, VIPGuest.streamer_id == eff_id).first()
        
        if vip: 
            target = vip.target_username
            db.delete(vip)
            db.add(AuditLog(streamer_id=streamer.id, user_id=auth["user_id"], action="VIP_REMOVED", details=f"Removed {target} from VIPs"))
            db.commit()
        return RedirectResponse(url="/", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/?error=server_crash", status_code=303)


# ---------------------------------------------------------
# AI MODERATION TRAINING (DEV ROUTE)
# ---------------------------------------------------------
@app.post("/api/dev/train-moderation")
async def train_bot_moderation(
    request: Request,
    dev_username: str = Form(...),
    input_type: str = Form(...), # "transcript", "written_condition", "video_data"
    content: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_role(["owner", "manager"]))
):
    """
    Dev-Only Route: Trains the AI moderation engine on transcripts or written conditions.
    """
    streamer_id = request.session.get("streamer_id", 1)
    
    try:
        result = await trainer_service.train_from_content(
            db=db,
            dev_username=dev_username,
            streamer_id=streamer_id,
            data_content=content,
            input_type=input_type
        )
    except Exception as exc:
        db.rollback()
        logger.exception("[AI TRAINING] Persistence failed type=%s", type(exc).__name__)
        return RedirectResponse(url="/?error=training_persistence_failed", status_code=303)

    if result["success"]:
        return RedirectResponse(
            url=f"/?success=ai_trained&count={result['rules_added']}", 
            status_code=303
        )
    else:
        return RedirectResponse(
            url=f"/?error=training_failed&msg={urllib.parse.quote(result['error'])}", 
            status_code=303
        )


@app.get("/healthz")
async def healthz(db: Session = Depends(get_db)):
    """Readiness endpoint without disclosing credentials or internal state."""
    db.execute(text("SELECT 1"))
    return {"status": "stopped" if emergency_stop.is_stopped(db) else "ok"}

@app.post("/admin/emergency-stop")
async def set_emergency_stop(request: Request, enabled: bool = Form(...), reason: str = Form(""), db: Session = Depends(get_db), auth: dict = Depends(require_role(["owner"]))):
    streamer_id = request.session.get("streamer_id")
    emergency_stop.set(db, enabled, reason)
    db.add(AuditLog(streamer_id=streamer_id, user_id=auth["user_id"], action="EMERGENCY_STOP_SET", details=(reason or "cleared")[:500]))
    db.commit()
    discord_events.emit("Emergency stop", f"{'ENABLED' if enabled else 'CLEARED'}: {(reason or 'no reason')[:500]}")
    return RedirectResponse(url="/", status_code=303)

@app.get("/api/youtube-webhook")
async def verify_youtube_webhook(request: Request):
    challenge = request.query_params.get("hub.challenge")
    return Response(content=challenge, media_type="text/plain") if challenge else Response(status_code=400)

# ⚡ AUTO-POWER ON WHEN WEBSUB SEES YOU GO LIVE
@app.post("/api/youtube-webhook")
async def receive_youtube_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        xml_data = await request.body()
        root = ET.fromstring(xml_data)
        namespaces = {'atom': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}

        entry = root.find('atom:entry', namespaces)
        if entry is None:
            logger.info("[WEBSUB] Feed notification contains no entry; ignored")
            return Response(status_code=204)

        video_id_element = entry.find('yt:videoId', namespaces)
        channel_id_element = entry.find('yt:channelId', namespaces)
        video_id = video_id_element.text if video_id_element is not None else None
        channel_id = channel_id_element.text if channel_id_element is not None else None
        if not video_id or not channel_id:
            logger.warning("[WEBSUB] Malformed notification missing video or channel id")
            return Response(status_code=204)

        streamer = db.query(Streamer).filter(Streamer.youtube_channel_id == channel_id).first()
        if not streamer:
            logger.info("[WEBSUB] Ignored unmonitored channel notification")
            return Response(status_code=204)

        logger.info("[WEBSUB] Matched monitored channel; verifying live broadcast video=%s", video_id)
        yt_api_manager.invalidate_live_video(video_id)
        broadcast = await yt_api_manager.resolve_live_broadcast(video_id, expected_channel_id=channel_id)
        if not broadcast:
            # An end/update notification should release an existing monitor;
            # scheduled/non-live notifications are harmless no-ops.
            DISCONNECT_QUEUE.add(video_id)
            logger.info("[WEBSUB] No active live chat for video=%s; no session started", video_id)
            return Response(status_code=204)

        async with websub_start_lock:
            monitor = getattr(app.state, "yt_monitor", None)
            if video_id in DETECTED_VIDEOS or (monitor and video_id in monitor.active_streams):
                logger.info("[WEBSUB] Duplicate delivery ignored for live video=%s", video_id)
                return Response(status_code=204)
            streamer.is_active = True
            db.add(AuditLog(
                streamer_id=streamer.id,
                user_id=None,
                action="WEBSUB_LIVE_SESSION_QUEUED",
                details=video_id,
            ))
            db.commit()
            # The existing YouTubeChatMonitor consumes this map, resolves the
            # cached liveChatId, and owns all chat/moderation behavior.
            DETECTED_VIDEOS[video_id] = streamer.effective_id
            logger.info("[WEBSUB] Live broadcast queued video=%s chat=%s", video_id, broadcast["chat_id"])
        return Response(status_code=204) 
    except Exception as e:
        logger.error(f"[WEBSUB WEBHOOK ERROR] {e}")
        return Response(status_code=200)


app.include_router(economy_router)

running_tasks = []

@app.on_event("startup")
async def startup_event():
    init_db()
    try:
        from migrations.runner import run as run_migrations
        run_migrations(bootstrap=False)
    except Exception as e:
        logger.warning("[STARTUP] Migration runner note: %s", e)
    db = SessionLocal()
    try:
        monitored_streamers = ensure_monitored_channels(db)
        channel_ids = [streamer.youtube_channel_id for streamer in monitored_streamers if streamer.youtube_channel_id]
    finally:
        db.close()
    for channel_id in channel_ids:
        subscribe_websub(channel_id)
    start_scheduler()
    yt_monitor = YouTubeChatMonitor()
    app.state.yt_monitor = yt_monitor
    running_tasks.extend([
        asyncio.create_task(yt_monitor.run()),
        asyncio.create_task(start_discord_bot()),
        asyncio.create_task(start_timed_command_loop()),
        asyncio.create_task(websub_renewal_loop())
    ])

@app.on_event("shutdown")
async def shutdown_event():
    for task in running_tasks:
        task.cancel()
    await discord_events.close()

if __name__ == "__main__":
    railway_port = int(os.environ.get("PORT", 8000))
    # Standardized format to prevent import failures in Railway
    uvicorn.run("main:app", host="0.0.0.0", port=railway_port, proxy_headers=True, forwarded_allow_ips="*")
