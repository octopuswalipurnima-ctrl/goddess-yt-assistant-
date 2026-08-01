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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Depends, Form, WebSocket, WebSocketDisconnect, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from sqlalchemy.orm import Session

from app.database.connection import init_db, get_db
from app.database.models import (
    User, XP, Streamer, AlertTemplate, GoalWidget, ClipRecord, 
    CustomCommand, VIPGuest, Coin, WaitingListEntry
)
from app.dashboard.auth import router as auth_router
from app.dashboard.routes import router as dashboard_router
from app.bot.youtube_chat import YouTubeChatMonitor, DETECTED_VIDEOS
from app.bot.discord_bot import start_discord_bot
from app.services.scheduler import start_scheduler, start_timed_command_loop
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

app = FastAPI(title="Goddess Stream Manager")

# ---------------------------------------------------------
# MIDDLEWARE & BROWSER SESSIONS (ORDER IS CRITICAL)
# ---------------------------------------------------------
is_production = os.environ.get("PORT") is not None

app.add_middleware(
    SessionMiddleware, 
    secret_key="super-secret-goddess-key-change-later",
    max_age=3600 * 24 * 7,
    https_only=is_production,
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
                    "request": request, 
                    "streamer_name": None, 
                    "viewers": [], 
                    "settings": {},
                    "clips": [],
                    "commands": [],
                    "vips": []
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
            logger.info(f"[DASHBOARD] Generated new sync code {streamer.server_sync_code} for streamer {streamer.channel_name}")
            
        viewers = db.query(User).join(XP).filter(XP.streamer_id == streamer_id).all()
        recent_clips = db.query(ClipRecord).filter(ClipRecord.streamer_id == streamer_id).order_by(ClipRecord.id.desc()).limit(6).all()
        commands = db.query(CustomCommand).filter(CustomCommand.streamer_id == streamer_id).all()
        vips = db.query(VIPGuest).filter(VIPGuest.streamer_id == streamer_id).all()
        
        settings = {
            "ai_cohost_enabled": streamer.ai_cohost_enabled,
            "giveaway_reminders_enabled": streamer.giveaway_reminders_enabled,
            "server_sync_code": streamer.server_sync_code,
            "is_discord_linked": bool(streamer.discord_guild_id)
        }
        
        logger.info(f"[DASHBOARD] Dashboard rendered successfully for streamer: {streamer.channel_name}")
        return templates.TemplateResponse(
            request=request, 
            name="index.html", 
            context={
                "request": request,
                "streamer_name": streamer.channel_name,
                "viewers": viewers,
                "settings": settings,
                "clips": recent_clips,
                "commands": commands,
                "vips": vips
            }
        )
    except Exception as e:
        logger.exception(f"[DASHBOARD ERROR] Failed to render dashboard: {e}")
        return templates.TemplateResponse(request=request, name="index.html", context={"request": request, "streamer_name": None, "viewers": [], "settings": {}, "clips": [], "commands": [], "vips": []})


@app.post("/toggle-setting")
async def toggle_setting(request: Request, setting: str = Form(...), db: Session = Depends(get_db)):
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[SETTINGS] Toggle request for setting '{setting}' by streamer ID {streamer_id}")
        if streamer_id:
            streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
            if streamer:
                if setting == "ai_cohost":
                    streamer.ai_cohost_enabled = not streamer.ai_cohost_enabled
                    logger.info(f"[SETTINGS] AI Co-Host toggled to {streamer.ai_cohost_enabled}")
                elif setting == "giveaways":
                    streamer.giveaway_reminders_enabled = not streamer.giveaway_reminders_enabled
                    logger.info(f"[SETTINGS] Giveaway Reminders toggled to {streamer.giveaway_reminders_enabled}")
                db.commit()
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.exception(f"[SETTINGS ERROR] Failed to toggle setting '{setting}': {e}")
        return RedirectResponse(url="/", status_code=303)


@app.post("/guest-join")
async def guest_join(request: Request, stream_url: str = Form(...)):
    session_id = str(uuid.uuid4())[:8]
    try:
        logger.info(f"[SESSION:{session_id}] [LIVE CHAT CONNECTION] Guest join request received for URL: {stream_url}")
        yt_regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/live\/)([a-zA-Z0-9_-]{11})"
        match = re.search(yt_regex, stream_url)
        
        if match:
            video_id = match.group(1)
            logger.info(f"[SESSION:{session_id}] [LIVE CHAT CONNECTION] Video ID extracted: {video_id}. Injecting into DETECTED_VIDEOS queue.")
            DETECTED_VIDEOS.add(video_id)
        else:
            logger.warning(f"[SESSION:{session_id}] [LIVE CHAT CONNECTION] Could not parse valid YouTube Video ID from input: {stream_url}")
            
        return RedirectResponse(url="/?guest=true", status_code=303)
    except Exception as e:
        logger.exception(f"[SESSION:{session_id}] [GUEST JOIN ERROR] Failed processing guest join request: {e}")
        return RedirectResponse(url="/", status_code=303)


@app.post("/api/panic-button")
async def panic_button_protocol(request: Request, db: Session = Depends(get_db)):
    session_id = str(uuid.uuid4())[:8]
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[SESSION:{session_id}] [PANIC BUTTON] Protocol activated for streamer ID: {streamer_id}")
        
        if not streamer_id:
            logger.warning(f"[SESSION:{session_id}] [PANIC BUTTON] Unauthorized invocation attempt without active streamer session.")
            return RedirectResponse(url="/", status_code=303)
            
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if not streamer:
            logger.error(f"[SESSION:{session_id}] [PANIC BUTTON] Streamer record missing for ID: {streamer_id}")
            return RedirectResponse(url="/?error=invalid_channel", status_code=303)
            
        logger.info(f"[SESSION:{session_id}] [YOUTUBE LIVE CHECK] Scanning YouTube for active live stream for channel: {streamer.channel_name}")
        
        api_key = os.environ.get("YOUTUBE_API_KEY")
        if not api_key:
            logger.critical(f"[SESSION:{session_id}] [PANIC BUTTON ERROR] YOUTUBE_API_KEY is missing from environment variables.")
            return RedirectResponse(url="/?error=missing_api_key", status_code=303)
            
        safe_channel_name = urllib.parse.quote(streamer.channel_name)
        search_url = (
            f"https://www.googleapis.com/youtube/v3/search?"
            f"part=snippet&q={safe_channel_name}&eventType=live&type=video&key={api_key}"
        )
        
        def fetch_live_stream():
            try:
                logger.info(f"[SESSION:{session_id}] [API RESPONSE] Requesting YouTube Search API endpoint...")
                with urllib.request.urlopen(search_url) as response:
                    res_body = response.read().decode()
                    logger.info(f"[SESSION:{session_id}] [API RESPONSE] Received response status 200 OK from YouTube API.")
                    return json.loads(res_body)
            except urllib.error.HTTPError as e:
                error_details = e.read().decode()
                logger.error(f"[SESSION:{session_id}] [API RESPONSE ERROR] HTTP {e.code}: {error_details}")
                raise Exception(f"Google API Rejected Request: {e.code} - {error_details}")
        
        data = await asyncio.to_thread(fetch_live_stream)
        
        if "items" in data and len(data["items"]) > 0:
            video_id = data["items"][0]["id"]["videoId"]
            logger.info(f"[SESSION:{session_id}] [YOUTUBE LIVE CHECK] Target stream acquired: Video ID {video_id}. Deploying chat bot.")
            DETECTED_VIDEOS.add(video_id)
            return RedirectResponse(url="/?success=bot_deployed", status_code=303)
        else:
            logger.info(f"[SESSION:{session_id}] [YOUTUBE LIVE CHECK] Scanned YouTube but no active live stream found for channel: {streamer.channel_name}")
            return RedirectResponse(url="/?error=not_live", status_code=303)
            
    except Exception as e:
        logger.exception(f"[SESSION:{session_id}] [PANIC BUTTON CRITICAL ERROR] Execution breakdown: {e}")
        return RedirectResponse(url="/?error=api_crash", status_code=303)


# ---------------------------------------------------------
# NEW: YOUTUBE WEBSUB (PUBSUBHUBBUB) NOTIFICATION ENDPOINTS
# ---------------------------------------------------------
@app.get("/api/youtube-webhook")
async def verify_youtube_webhook(request: Request):
    """Step 1: YouTube sends a GET request to verify the endpoint is real."""
    challenge = request.query_params.get("hub.challenge")
    if challenge:
        logger.info(f"[WEBSUB] Verification challenge received and accepted.")
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=400)

@app.post("/api/youtube-webhook")
async def receive_youtube_webhook(request: Request):
    """Step 2: YouTube sends a POST request with XML when a stream starts."""
    try:
        xml_data = await request.body()
        root = ET.fromstring(xml_data)

        # XML Namespaces used by YouTube's Atom feeds
        namespaces = {
            'atom': 'http://www.w3.org/2005/Atom',
            'yt': 'http://www.youtube.com/xml/schemas/2015'
        }

        entry = root.find('atom:entry', namespaces)
        if entry is not None:
            video_id_element = entry.find('yt:videoId', namespaces)
            if video_id_element is not None:
                video_id = video_id_element.text
                logger.info(f"[WEBSUB NOTIFICATION] Target stream detected! Video ID: {video_id}")
                
                # Instantly deploy the bot to this new live stream
                DETECTED_VIDEOS.add(video_id)

        # You MUST return 204 No Content so YouTube knows you received it
        return Response(status_code=204) 
    except Exception as e:
        logger.exception(f"[WEBSUB ERROR] Failed to parse payload: {e}")
        return Response(status_code=200)


# ---------------------------------------------------------
# OBS WEBSOCKET & WIDGET ROUTES
# ---------------------------------------------------------
@app.get("/overlay/{sync_code}")
async def render_overlay(request: Request, sync_code: str):
    try:
        logger.info(f"[OBS OVERLAY] Overlay browser source requested for sync code: {sync_code}")
        active_theme = request.session.get("active_theme", "neon")
        custom_css = request.session.get("custom_css", "")
        return templates.TemplateResponse(
            request=request,
            name="overlay.html", 
            context={"request": request, "sync_code": sync_code, "active_theme": active_theme, "custom_css": custom_css}
        )
    except Exception as e:
        logger.exception(f"[OBS OVERLAY ERROR] Failed to render overlay page: {e}")
        return templates.TemplateResponse(request=request, name="overlay.html", context={"request": request, "sync_code": sync_code, "active_theme": "neon", "custom_css": ""})


@app.websocket("/ws/overlay/{sync_code}")
async def websocket_overlay(websocket: WebSocket, sync_code: str):
    try:
        logger.info(f"[OBS WEBSOCKET] Client connecting for sync code: {sync_code}")
        await overlay_manager.connect(websocket, sync_code)
        logger.info(f"[OBS WEBSOCKET] Client connected successfully: {sync_code}")
        while True:
            data = await websocket.receive_text()
            logger.debug(f"[OBS WEBSOCKET] Frame payload received on {sync_code}: {data}")
    except WebSocketDisconnect:
        overlay_manager.disconnect(websocket, sync_code)
        logger.info(f"[OBS WEBSOCKET] Client disconnected for sync code: {sync_code}")
    except Exception as e:
        logger.exception(f"[OBS WEBSOCKET ERROR] Connection fault for sync code {sync_code}: {e}")
        overlay_manager.disconnect(websocket, sync_code)


@app.post("/test-alert")
async def test_alert(request: Request, db: Session = Depends(get_db)):
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[OBS ALERT] Firing test alert for streamer ID {streamer_id}")
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
                logger.info(f"[OBS ALERT] Test alert broadcasted to sync code: {streamer.server_sync_code}")
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.exception(f"[OBS ALERT ERROR] Failed to send test alert: {e}")
        return RedirectResponse(url="/", status_code=303)


@app.post("/custom-alert")
async def custom_alert(
    request: Request,
    alert_title: str = Form(...),
    alert_message: str = Form(...),
    db: Session = Depends(get_db)
):
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[CUSTOM ALERT] Launching custom alert '{alert_title}' for streamer ID {streamer_id}")
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
                logger.info(f"[CUSTOM ALERT] Payload broadcasted to sync code {streamer.server_sync_code}")
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.exception(f"[CUSTOM ALERT ERROR] Failed launching custom alert: {e}")
        return RedirectResponse(url="/", status_code=303)


@app.post("/select-theme")
async def select_theme(request: Request, theme_name: str = Form(...), db: Session = Depends(get_db)):
    try:
        logger.info(f"[THEME] Changing active widget theme to: {theme_name}")
        request.session["active_theme"] = theme_name
        return RedirectResponse(url="/?theme_updated=true", status_code=303)
    except Exception as e:
        logger.exception(f"[THEME ERROR] Failed updating active theme: {e}")
        return RedirectResponse(url="/", status_code=303)


@app.post("/upload-custom-widget")
async def upload_custom_widget(
    request: Request, 
    custom_css: str = Form(...), 
    db: Session = Depends(get_db)
):
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[CUSTOM WIDGET] Upload request for streamer ID {streamer_id}")
        if not streamer_id:
            return RedirectResponse(url="/", status_code=303)
            
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if not streamer:
            return RedirectResponse(url="/", status_code=303)

        channel_name = streamer.channel_name.lower()
        is_dev = "sarthak" in channel_name or "goddess" in channel_name

        if is_dev:
            logger.info(f"[CUSTOM WIDGET] Developer bypass authorized for {streamer.channel_name}.")
            request.session["custom_css"] = custom_css
            request.session["active_theme"] = "custom"
            return RedirectResponse(url="/?custom_success=dev_bypass", status_code=303)
        else:
            logger.info(f"[CUSTOM WIDGET] Payment gate triggered for non-dev user: {streamer.channel_name}")
            return RedirectResponse(url="/?error=payment_required_20_inr", status_code=303)
    except Exception as e:
        logger.exception(f"[CUSTOM WIDGET ERROR] Custom CSS upload fault: {e}")
        return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------
# VISUAL ENGINE AND GOAL ROUTES
# ---------------------------------------------------------
@app.post("/api/save-alert-layout")
async def save_alert_layout(request: Request, layout_config: str = Form(...), db: Session = Depends(get_db)):
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[VISUAL BUILDER] Saving alert layout for streamer ID {streamer_id}")
        if not streamer_id:
            return RedirectResponse(url="/", status_code=303)
            
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        if not streamer:
            return RedirectResponse(url="/", status_code=303)
        
        channel_name = streamer.channel_name.lower()
        is_dev = "sarthak" in channel_name or "goddess" in channel_name
        has_paid = request.session.get("has_paid_premium", False) 
        
        if not (is_dev or has_paid):
            logger.warning(f"[VISUAL BUILDER] Unauthorized layout save attempt by unpaid streamer: {streamer.channel_name}")
            return RedirectResponse(url="/?error=payment_required_20_inr", status_code=303)
            
        try:
            parsed_config = json.loads(layout_config)
        except json.JSONDecodeError:
            logger.error("[VISUAL BUILDER] Received invalid JSON payload from builder form.")
            return RedirectResponse(url="/?error=invalid_json", status_code=303)
        
        template = db.query(AlertTemplate).filter(AlertTemplate.streamer_id == streamer_id).first()
        if not template:
            template = AlertTemplate(streamer_id=streamer_id, config_json=parsed_config)
            db.add(template)
        else:
            template.config_json = parsed_config
        
        db.commit()
        logger.info(f"[VISUAL BUILDER] Alert layout saved in database for streamer ID {streamer_id}")
        
        if streamer.server_sync_code:
            await overlay_manager.send_alert(streamer.server_sync_code, {"type": "config_update", "config": parsed_config})
            logger.info(f"[VISUAL BUILDER] Live config update pushed to sync code: {streamer.server_sync_code}")
             
        return RedirectResponse(url="/?success=layout_saved", status_code=303)
    except Exception as e:
        logger.exception(f"[VISUAL BUILDER ERROR] Failed saving alert layout: {e}")
        return RedirectResponse(url="/", status_code=303)


@app.post("/api/update-goal")
async def update_goal(request: Request, goal_id: int = Form(...), amount: int = Form(...), db: Session = Depends(get_db)):
    try:
        logger.info(f"[GOAL ENGINE] Updating goal ID {goal_id} with amount +{amount}")
        goal = db.query(GoalWidget).filter(GoalWidget.id == goal_id).first()
        if goal:
            goal.current_amount += amount
            db.commit()
            streamer = db.query(Streamer).filter(Streamer.id == goal.streamer_id).first()
            if streamer and streamer.server_sync_code:
                await overlay_manager.send_alert(streamer.server_sync_code, {
                    "type": "goal_update",
                    "goal_id": goal.id,
                    "current": goal.current_amount,
                    "target": goal.target_amount
                })
                logger.info(f"[GOAL ENGINE] Goal progression broadcasted to sync code {streamer.server_sync_code}")
        return {"status": "success"}
    except Exception as e:
        logger.exception(f"[GOAL ENGINE ERROR] Failed updating goal widget {goal_id}: {e}")
        return {"status": "error", "reason": str(e)}


# ---------------------------------------------------------
# AI MODERATION ENDPOINTS
# ---------------------------------------------------------
DEV_YOUTUBE_IDS = {"@uk_hi_kahda", "@goddessislive"}

@app.post("/api/moderation/process-message")
async def process_chat_message(
    request: Request, 
    user_id: int = Form(...),
    username: str = Form(...), 
    message_text: str = Form(...), 
    db: Session = Depends(get_db)
):
    session_id = str(uuid.uuid4())[:8]
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[SESSION:{session_id}] [MESSAGE RECEIPT] Streamer ID {streamer_id} | User ID {user_id} ({username}): '{message_text}'")
        
        if not streamer_id:
            logger.warning(f"[SESSION:{session_id}] [MESSAGE RECEIPT] Processing rejected — session unauthenticated.")
            return {"verdict": "Ignored", "action": "None", "reason": "No active session authentication."}

        clean_username = username.strip().lower()
        if not clean_username.startswith("@"):
            clean_username = f"@{clean_username}"

        if clean_username in DEV_YOUTUBE_IDS:
            logger.info(f"[SESSION:{session_id}] [GOD MODE] Developer override activated for {clean_username}. Bypassing moderation.")
            return {"verdict": "Safe", "action": "None", "reason": "System Developer Bypass."}

        vip_entry = db.query(VIPGuest).filter(
            VIPGuest.streamer_id == streamer_id,
            VIPGuest.target_username == clean_username
        ).first()
        
        if vip_entry and not vip_entry.has_been_greeted:
            vip_entry.has_been_greeted = True
            db.commit()
            logger.info(f"[SESSION:{session_id}] [REPLY] [VIP ENTRANCE] Triggering VIP greeting for {clean_username}: '{vip_entry.custom_reply}'")
            return {"verdict": "Safe", "action": "Reply", "reason": "VIP Entrance Greeting", "bot_response": vip_entry.custom_reply}

        from app.database.models import ChatLog, ViewerTrust, ModActionLog, CustomCommand
        from app.services.moderation.rule_engine import LocalRuleEngine
        from app.services.moderation.gemini_client import GeminiModeratorEngine

        trust = db.query(ViewerTrust).filter(
            ViewerTrust.user_id == user_id, 
            ViewerTrust.streamer_id == streamer_id
        ).first()

        words_original = message_text.strip().split()
        words_lower = [w.lower() for w in words_original]

        db_user = db.query(User).filter(User.id == user_id).first()
        if db_user:
            db_user.last_seen = datetime.now(timezone.utc)
            db.commit()

        is_authorized_mod = (
            clean_username in DEV_YOUTUBE_IDS or 
            (trust and (trust.is_whitelisted or trust.trust_score > 85.0))
        )

        valid_mgmt_cmds = {"!adduk", "!edituk", "!deluk", "!reptuk"}

        if words_lower:
            cmd = words_lower[0]

            if cmd == "!coinflip" and len(words_lower) == 3:
                try:
                    bet = int(words_lower[1])
                    choice = words_lower[2]
                    
                    if choice not in ["heads", "tails"]:
                        bot_response = "❌ Use: !coinflip [amount] heads/tails"
                        logger.info(f"[SESSION:{session_id}] [REPLY] Invalid coinflip command format.")
                        return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}
                        
                    user_coin = db.query(Coin).filter(Coin.user_id == user_id, Coin.streamer_id == streamer_id).first()
                    if not user_coin or user_coin.balance < bet or bet <= 0:
                        bot_response = f"❌ You don't have enough coins for that bet, {username}!"
                        logger.info(f"[SESSION:{session_id}] [REPLY] Coinflip rejected — insufficient funds for {username}")
                        return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}
                        
                    result = random.choice(["heads", "tails"])
                    if result == choice:
                        user_coin.balance += bet
                        db.commit()
                        bot_response = f"🎉 It's {result}! You won {bet} coins, {username}! (Balance: {user_coin.balance})"
                    else:
                        user_coin.balance -= bet
                        db.commit()
                        bot_response = f"💀 It's {result}! You lost {bet} coins, {username}. (Balance: {user_coin.balance})"
                        
                    logger.info(f"[SESSION:{session_id}] [REPLY] Coinflip outcome for {username}: {bot_response}")
                    return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}
                except ValueError:
                    pass

            elif cmd == "!join":
                existing = db.query(WaitingListEntry).filter(
                    WaitingListEntry.streamer_id == streamer_id, 
                    WaitingListEntry.user_id == user_id
                ).first()
                if existing:
                    bot_response = f"⚠️ You are already in the waiting list, {username}!"
                    logger.info(f"[SESSION:{session_id}] [REPLY] Queue join rejected — {username} already queued.")
                    return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}
                    
                new_entry = WaitingListEntry(streamer_id=streamer_id, user_id=user_id)
                db.add(new_entry)
                db.commit()
                
                queue_pos = db.query(WaitingListEntry).filter(WaitingListEntry.streamer_id == streamer_id).count()
                bot_response = f"✅ {username} joined the 1v1 queue! You are position #{queue_pos}. Keep chatting every 10 mins so you aren't marked AFK."
                logger.info(f"[SESSION:{session_id}] [REPLY] {username} joined 1v1 queue at position #{queue_pos}")
                return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}

            elif cmd == "!queue":
                q = db.query(WaitingListEntry).filter(WaitingListEntry.streamer_id == streamer_id).order_by(WaitingListEntry.joined_at.asc()).limit(3).all()
                if not q:
                    bot_response = "The 1v1 queue is currently empty! Type !join to enter."
                else:
                    names = [entry.user.username for entry in q]
                    bot_response = f"🎮 Next in line: 1. {names[0]} " + (" ".join([f"{i+2}. {n}" for i, n in enumerate(names[1:])]))
                logger.info(f"[SESSION:{session_id}] [REPLY] Queue status returned: {bot_response}")
                return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}

            elif cmd == "!next" and is_authorized_mod:
                next_player = db.query(WaitingListEntry).filter(WaitingListEntry.streamer_id == streamer_id).order_by(WaitingListEntry.joined_at.asc()).first()
                if not next_player:
                    bot_response = "❌ The queue is empty."
                else:
                    db.delete(next_player)
                    db.commit()
                    bot_response = f"🔥 It's your turn, {next_player.user.username}! Send a request now."
                logger.info(f"[SESSION:{session_id}] [REPLY] Mod !next triggered: {bot_response}")
                return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}

            elif cmd in valid_mgmt_cmds and is_authorized_mod:
                action = cmd
                
                if action == "!adduk" and len(words_original) >= 3:
                    new_trigger = words_lower[1] if words_lower[1].startswith("!") else f"!{words_lower[1]}"
                    response_content = " ".join(words_original[2:])
                    
                    existing_cmd = db.query(CustomCommand).filter(
                        CustomCommand.streamer_id == streamer_id,
                        CustomCommand.command_trigger == new_trigger
                    ).first()
                    
                    if existing_cmd:
                        bot_response = f"❌ Command {new_trigger} already exists. Use !edituk to change it."
                    else:
                        new_cmd = CustomCommand(streamer_id=streamer_id, command_trigger=new_trigger, response_text=response_content)
                        db.add(new_cmd)
                        db.commit()
                        bot_response = f"✅ Command {new_trigger} has been successfully added!"
                    logger.info(f"[SESSION:{session_id}] [REPLY] !adduk result: {bot_response}")
                    return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}

                elif action == "!edituk" and len(words_original) >= 3:
                    target_trigger = words_lower[1] if words_lower[1].startswith("!") else f"!{words_lower[1]}"
                    response_content = " ".join(words_original[2:])
                    
                    existing_cmd = db.query(CustomCommand).filter(
                        CustomCommand.streamer_id == streamer_id,
                        CustomCommand.command_trigger == target_trigger
                    ).first()
                    
                    if existing_cmd:
                        existing_cmd.response_text = response_content
                        db.commit()
                        bot_response = f"✅ Command {target_trigger} has been successfully updated!"
                    else:
                        bot_response = f"❌ Command {target_trigger} not found. Use !adduk to create it."
                    logger.info(f"[SESSION:{session_id}] [REPLY] !edituk result: {bot_response}")
                    return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}

                elif action == "!deluk" and len(words_original) == 2:
                    target_trigger = words_lower[1] if words_lower[1].startswith("!") else f"!{words_lower[1]}"
                    existing_cmd = db.query(CustomCommand).filter(
                        CustomCommand.streamer_id == streamer_id,
                        CustomCommand.command_trigger == target_trigger
                    ).first()
                    
                    if existing_cmd:
                        db.delete(existing_cmd)
                        db.commit()
                        bot_response = f"🗑️ Command {target_trigger} deleted successfully."
                    else:
                        bot_response = f"❌ Command {target_trigger} not found."
                    logger.info(f"[SESSION:{session_id}] [REPLY] !deluk result: {bot_response}")
                    return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}

                elif action == "!reptuk" and len(words_original) == 3:
                    target_trigger = words_lower[1] if words_lower[1].startswith("!") else f"!{words_lower[1]}"
                    try:
                        minutes = int(words_original[2])
                        cmd_to_loop = db.query(CustomCommand).filter(
                            CustomCommand.streamer_id == streamer_id,
                            CustomCommand.command_trigger == target_trigger
                        ).first()
                        
                        if cmd_to_loop:
                            cmd_to_loop.interval_minutes = minutes
                            db.commit()
                            bot_response = f"🔄 {target_trigger} is now looping every {minutes} minutes." if minutes > 0 else f"🛑 {target_trigger} loop timer disabled."
                        else:
                            bot_response = f"❌ Command {target_trigger} not found. Use !adduk first!"
                    except ValueError:
                        bot_response = "❌ Invalid interval. Usage: !reptuk !trigger 15"
                    logger.info(f"[SESSION:{session_id}] [REPLY] !reptuk result: {bot_response}")
                    return {"verdict": "Safe", "action": "Reply", "bot_response": bot_response}

            elif cmd == "!clip":
                new_clip = ClipRecord(
                    streamer_id=streamer_id,
                    title="Viewer Chat Clip",
                    file_path="/static/clips/pending.mp4",
                    duration_seconds=60,
                    resolution="1080p",
                    trigger_source="Chat Command (!clip)"
                )
                db.add(new_clip)
                db.commit()

                streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
                if streamer and streamer.server_sync_code:
                    await overlay_manager.send_alert(
                        streamer.server_sync_code, 
                        {"type": "obs_save_replay_buffer", "message": "Chat triggered a clip!", "clip_id": new_clip.id}
                    )
                logger.info(f"[SESSION:{session_id}] [REPLY] Stream clip triggered successfully.")
                return {"verdict": "Safe", "action": "Command Executed", "reason": "Stream clip triggered successfully via chat."}

            elif cmd.startswith("!"):
                trigger = cmd
                custom_cmd = db.query(CustomCommand).filter(
                    CustomCommand.streamer_id == streamer_id,
                    CustomCommand.command_trigger == trigger,
                    CustomCommand.is_active == True
                ).first()
                
                if custom_cmd:
                    logger.info(f"[SESSION:{session_id}] [REPLY] Custom command '{trigger}' dispatched: {custom_cmd.response_text}")
                    return {"verdict": "Safe", "action": "Reply", "reason": "Custom command triggered.", "bot_response": custom_cmd.response_text}

        recent_logs = db.query(ChatLog).filter(ChatLog.streamer_id == streamer_id).order_by(ChatLog.timestamp.desc()).limit(10).all()
        context_list = [log.message for log in reversed(recent_logs)]

        if trust and (trust.is_whitelisted or trust.trust_score > 85.0):
            logger.info(f"[SESSION:{session_id}] [TRUST BYPASS] High trust score for user ID {user_id}. Moderation bypassed.")
            return {"verdict": "Safe", "action": "None", "reason": "High user trust score bypass applied."}

        local_engine = LocalRuleEngine(db=db, streamer_id=streamer_id)
        local_eval = local_engine.evaluate(message_text)
        
        if local_eval.get("verdict", "Questionable") != "Questionable":
            logger.info(f"[SESSION:{session_id}] [RULE ENGINE] Intercepted on Layer 1 (Local): Verdict={local_eval['verdict']}")
            log_entry = ModActionLog(
                streamer_id=streamer_id, message_content=message_text,
                layer_triggered="Layer 1 (Local)", classification=local_eval["verdict"],
                recommended_action=local_eval["verdict"], reason=local_eval["reason"]
            )
            db.add(log_entry)
            db.commit()
            return {"verdict": local_eval["verdict"], "action": local_eval["verdict"], "reason": local_eval["reason"]}

        logger.info(f"[SESSION:{session_id}] [GEMINI PROCESSING] Dispatching message to Gemini AI Moderation Engine...")
        ai_engine = GeminiModeratorEngine(db)
        ai_verdict = await ai_engine.analyze_message(message_text, context_list)
        logger.info(f"[SESSION:{session_id}] [GEMINI PROCESSING] Gemini Verdict received: Classification={ai_verdict.get('classification')}, Action={ai_verdict.get('recommended_action')}")
        
        if "shadow_triggers" in local_eval and local_eval["shadow_triggers"]:
            local_engine.calibrate_shadow_rules(local_eval["shadow_triggers"], ai_verdict.get("recommended_action"))

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
    except Exception as e:
        logger.exception(f"[SESSION:{session_id}] [MODERATION ERROR] Critical fault in chat message processing pipeline: {e}")
        return {"verdict": "Error", "action": "None", "reason": f"Moderation engine failure: {e}"}


# ---------------------------------------------------------
# VIP & COMMAND DASHBOARD ROUTES
# ---------------------------------------------------------
@app.post("/api/commands/add")
async def add_custom_command(
    request: Request, 
    command_trigger: str = Form(...), 
    response_text: str = Form(...), 
    db: Session = Depends(get_db)
):
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[COMMAND MGMT] Adding command '{command_trigger}' for streamer ID {streamer_id}")
        if not streamer_id:
            return RedirectResponse(url="/", status_code=303)

        clean_trigger = command_trigger.strip().lower()
        if not clean_trigger.startswith("!"):
            clean_trigger = f"!{clean_trigger}"

        new_cmd = CustomCommand(streamer_id=streamer_id, command_trigger=clean_trigger, response_text=response_text)
        db.add(new_cmd)
        db.commit()
        logger.info(f"[COMMAND MGMT] Command '{clean_trigger}' saved successfully.")
        return RedirectResponse(url="/?success=command_added", status_code=303)
    except Exception as e:
        logger.exception(f"[COMMAND MGMT ERROR] Failed adding command '{command_trigger}': {e}")
        return RedirectResponse(url="/", status_code=303)


@app.post("/api/commands/delete")
async def delete_custom_command(request: Request, command_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[COMMAND MGMT] Deleting command ID {command_id} for streamer ID {streamer_id}")
        if streamer_id:
            cmd = db.query(CustomCommand).filter(CustomCommand.id == command_id, CustomCommand.streamer_id == streamer_id).first()
            if cmd:
                db.delete(cmd)
                db.commit()
                logger.info(f"[COMMAND MGMT] Command ID {command_id} deleted successfully.")
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.exception(f"[COMMAND MGMT ERROR] Failed deleting command ID {command_id}: {e}")
        return RedirectResponse(url="/", status_code=303)


@app.post("/api/vip/add")
async def add_vip_guest(
    request: Request, 
    target_username: str = Form(...), 
    custom_reply: str = Form(...), 
    db: Session = Depends(get_db)
):
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[VIP MGMT] Adding VIP guest '{target_username}' for streamer ID {streamer_id}")
        if not streamer_id:
            return RedirectResponse(url="/", status_code=303)

        clean_username = target_username.strip().lower()
        if not clean_username.startswith("@"):
            clean_username = f"@{clean_username}"

        new_vip = VIPGuest(streamer_id=streamer_id, target_username=clean_username, custom_reply=custom_reply)
        db.add(new_vip)
        db.commit()
        logger.info(f"[VIP MGMT] VIP guest '{clean_username}' saved successfully.")
        return RedirectResponse(url="/?success=vip_added", status_code=303)
    except Exception as e:
        logger.exception(f"[VIP MGMT ERROR] Failed adding VIP '{target_username}': {e}")
        return RedirectResponse(url="/", status_code=303)


@app.post("/api/vip/delete")
async def delete_vip_guest(request: Request, vip_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        streamer_id = request.session.get("streamer_id")
        logger.info(f"[VIP MGMT] Deleting VIP ID {vip_id} for streamer ID {streamer_id}")
        if streamer_id:
            vip = db.query(VIPGuest).filter(VIPGuest.id == vip_id, VIPGuest.streamer_id == streamer_id).first()
            if vip:
                db.delete(vip)
                db.commit()
                logger.info(f"[VIP MGMT] VIP ID {vip_id} removed successfully.")
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.exception(f"[VIP MGMT ERROR] Failed removing VIP ID {vip_id}: {e}")
        return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------
# MOUNT ADDITIONAL ROUTERS
# ---------------------------------------------------------
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(economy_router)


# ---------------------------------------------------------
# BACKGROUND WORKERS & STARTUP LOGIC
# ---------------------------------------------------------
running_tasks = []

@app.on_event("startup")
async def startup_event():
    try:
        logger.info("[STARTUP] Initializing Goddess Stream Manager application engines...")
        init_db()
        logger.info("[STARTUP] Database connection and schema initialized.")
        
        start_scheduler()
        logger.info("[STARTUP] Background task scheduler initialized.")
        
        yt_monitor = YouTubeChatMonitor()
        
        logger.info("[DISCORD EVENT] Starting Discord Bot background worker task...")
        task1 = asyncio.create_task(yt_monitor.run())
        task2 = asyncio.create_task(start_discord_bot())
        task3 = asyncio.create_task(start_timed_command_loop())
        
        running_tasks.extend([task1, task2, task3])
        
        logger.info("[STARTUP] All services (Web Dashboard, YouTube Live Monitor, Discord Bot, Timed Commands) are ACTIVE!")
    except Exception as e:
        logger.exception(f"[STARTUP ERROR] Critical failure during application startup: {e}")


if __name__ == "__main__":
    try:
        railway_port = int(os.environ.get("PORT", 8000))
        should_reload = False if os.environ.get("PORT") else True
        
        logger.info(f"[BOOT] Launching Uvicorn server instance on 0.0.0.0:{railway_port} (reload={should_reload})...")
        uvicorn.run(
            "main:app", 
            host="0.0.0.0", 
            port=railway_port, 
            reload=should_reload, 
            proxy_headers=True, 
            forwarded_allow_ips="*"
        )
    except Exception as e:
        logger.exception(f"[BOOT ERROR] Fatal server crash during launch: {e}")