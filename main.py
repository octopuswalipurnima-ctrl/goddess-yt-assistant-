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
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Depends, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from sqlalchemy.orm import Session

from app.database.connection import init_db, get_db
# Added Coin and WaitingListEntry for the new features
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

app = FastAPI(title="Goddess Stream Manager")

# ---------------------------------------------------------
# MIDDLEWARE & BROWSER SESSIONS (ORDER IS CRITICAL)
# ---------------------------------------------------------
is_production = os.environ.get("PORT") is not None

# 1. Add Session Middleware FIRST (Because FastAPI executes bottom-to-top, this runs LAST)
app.add_middleware(
    SessionMiddleware, 
    secret_key="super-secret-goddess-key-change-later",
    max_age=3600 * 24 * 7,
    https_only=is_production,
    same_site="lax" 
)

# 2. Add Proxy Middleware LAST (This runs FIRST, proving to the Session that Railway is using secure HTTPS)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=("*",))


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
                "settings": {},
                "clips": [],
                "commands": [],
                "vips": []
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
    
    # Fetch recent clips for the video gallery
    recent_clips = db.query(ClipRecord).filter(ClipRecord.streamer_id == streamer_id).order_by(ClipRecord.id.desc()).limit(6).all()
    
    # Fetch dynamic bot configuration data
    commands = db.query(CustomCommand).filter(CustomCommand.streamer_id == streamer_id).all()
    vips = db.query(VIPGuest).filter(VIPGuest.streamer_id == streamer_id).all()
    
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
            "settings": settings,
            "clips": recent_clips,
            "commands": commands,
            "vips": vips
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


@app.post("/api/panic-button")
async def panic_button_protocol(request: Request, db: Session = Depends(get_db)):
    """The Emergency Panic Button: Auto-finds the streamer's live video and forces the bot to join."""
    streamer_id = request.session.get("streamer_id")
    if not streamer_id:
        return RedirectResponse(url="/", status_code=303)
        
    streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
    if not streamer:
        return RedirectResponse(url="/?error=invalid_channel", status_code=303)
        
    print(f"[PANIC BUTTON] Protocol activated for {streamer.channel_name}!")
    
    # Directly grab the key from the environment to prevent config masking
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("[PANIC BUTTON ERROR] YOUTUBE_API_KEY is missing from Railway variables.")
        return RedirectResponse(url="/?error=missing_api_key", status_code=303)
        
    # URL encode the channel name to safely inject it into the link
    safe_channel_name = urllib.parse.quote(streamer.channel_name)
    
    # UPGRADE: Search by channel name instead of a strict channelId to bypass Google Account ID mismatch bugs
    search_url = (
        f"https://www.googleapis.com/youtube/v3/search?"
        f"part=snippet&q={safe_channel_name}&eventType=live&type=video&key={api_key}"
    )
    
    try:
        def fetch_live_stream():
            try:
                with urllib.request.urlopen(search_url) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as e:
                # UPGRADE: Capture the EXACT error Google sends back so we can see it in Railway logs
                error_details = e.read().decode()
                raise Exception(f"Google API Rejected Request: {e.code} - {error_details}")
        
        data = await asyncio.to_thread(fetch_live_stream)
        
        if "items" in data and len(data["items"]) > 0:
            video_id = data["items"][0]["id"]["videoId"]
            print(f"[PANIC BUTTON SUCCESS] Target acquired: {video_id}. Deploying bot...")
            DETECTED_VIDEOS.add(video_id)
            return RedirectResponse(url="/?success=bot_deployed", status_code=303)
        else:
            print(f"[PANIC BUTTON FAILED] Scanned YouTube but no active live stream was found for {streamer.channel_name}.")
            return RedirectResponse(url="/?error=not_live", status_code=303)
            
    except Exception as e:
        # This prints the exact Google error to your Railway Logs
        print(f"\n[PANIC BUTTON CRITICAL ERROR] \n{e}\n")
        return RedirectResponse(url="/?error=api_crash", status_code=303)


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
# Hardcoded developer IDs that have absolute immunity across all channels
DEV_YOUTUBE_IDS = {"@uk_hi_kahda", "@goddessislive"}

@app.post("/api/moderation/process-message")
async def process_chat_message(
    request: Request, 
    user_id: int = Form(...),
    username: str = Form(...), # Fetched explicitly to support Dev and VIP routing
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

    # Format the incoming username for accurate comparison checks
    clean_username = username.strip().lower()
    if not clean_username.startswith("@"):
        clean_username = f"@{clean_username}"

    # ---------------------------------------------------------
    # 1. DEVELOPER OVERRIDE (GOD MODE)
    # ---------------------------------------------------------
    if clean_username in DEV_YOUTUBE_IDS:
        print(f"[SYSTEM] Developer {clean_username} detected in chat. Bypassing all moderation layers.")
        return {
            "verdict": "Safe", 
            "action": "None", 
            "reason": "System Developer Bypass."
        }

    # ---------------------------------------------------------
    # 2. VIP CUSTOM GREETER
    # ---------------------------------------------------------
    vip_entry = db.query(VIPGuest).filter(
        VIPGuest.streamer_id == streamer_id,
        VIPGuest.target_username == clean_username
    ).first()
    
    # If they are a VIP and haven't been greeted yet this stream
    if vip_entry and not vip_entry.has_been_greeted:
        vip_entry.has_been_greeted = True
        db.commit()
        return {
            "verdict": "Safe", 
            "action": "Reply", 
            "reason": "VIP Entrance Greeting",
            "bot_response": vip_entry.custom_reply
        }

    # ---------------------------------------------------------
    # FETCH TRUST FOR MODERATOR CHECKS & AI BYPASS
    # ---------------------------------------------------------
    from app.database.models import ChatLog, ViewerTrust, ModActionLog, CustomCommand
    from app.services.moderation.rule_engine import LocalRuleEngine
    from app.services.moderation.gemini_client import GeminiModeratorEngine

    trust = db.query(ViewerTrust).filter(
        ViewerTrust.user_id == user_id, 
        ViewerTrust.streamer_id == streamer_id
    ).first()

    # We retain the original case for responses but parse with lowercase
    words_original = message_text.strip().split()
    words_lower = [w.lower() for w in words_original]

    # --- AFK UPDATE FOR QUEUE TRACKING ---
    db_user = db.query(User).filter(User.id == user_id).first()
    if db_user:
        db_user.last_seen = datetime.now(timezone.utc)
        db.commit()

    # Authority check: Devs or users with high trust score act as moderators
    is_authorized_mod = (
        clean_username in DEV_YOUTUBE_IDS or 
        (trust and (trust.is_whitelisted or trust.trust_score > 85.0))
    )

    valid_mgmt_cmds = {"!adduk", "!edituk", "!deluk", "!reptuk"}

    # ---------------------------------------------------------
    # 3. CHAT MANAGEMENT & GAMES PARSING
    # ---------------------------------------------------------
    if words_lower:
        cmd = words_lower[0]

        # --- ECONOMY MINI-GAMES: !coinflip [amount] [heads/tails] ---
        if cmd == "!coinflip" and len(words_lower) == 3:
            try:
                bet = int(words_lower[1])
                choice = words_lower[2]
                
                if choice not in ["heads", "tails"]:
                    return {"verdict": "Safe", "action": "Reply", "bot_response": "❌ Use: !coinflip [amount] heads/tails"}
                    
                user_coin = db.query(Coin).filter(Coin.user_id == user_id, Coin.streamer_id == streamer_id).first()
                if not user_coin or user_coin.balance < bet or bet <= 0:
                    return {"verdict": "Safe", "action": "Reply", "bot_response": f"❌ You don't have enough coins for that bet, {username}!"}
                    
                result = random.choice(["heads", "tails"])
                if result == choice:
                    user_coin.balance += bet
                    db.commit()
                    return {"verdict": "Safe", "action": "Reply", "bot_response": f"🎉 It's {result}! You won {bet} coins, {username}! (Balance: {user_coin.balance})"}
                else:
                    user_coin.balance -= bet
                    db.commit()
                    return {"verdict": "Safe", "action": "Reply", "bot_response": f"💀 It's {result}! You lost {bet} coins, {username}. (Balance: {user_coin.balance})"}
            except ValueError:
                pass

        # --- QUEUE MANAGER: !join (Enter 1v1 list) ---
        elif cmd == "!join":
            existing = db.query(WaitingListEntry).filter(
                WaitingListEntry.streamer_id == streamer_id, 
                WaitingListEntry.user_id == user_id
            ).first()
            if existing:
                return {"verdict": "Safe", "action": "Reply", "bot_response": f"⚠️ You are already in the waiting list, {username}!"}
                
            new_entry = WaitingListEntry(streamer_id=streamer_id, user_id=user_id)
            db.add(new_entry)
            db.commit()
            
            queue_pos = db.query(WaitingListEntry).filter(WaitingListEntry.streamer_id == streamer_id).count()
            return {"verdict": "Safe", "action": "Reply", "bot_response": f"✅ {username} joined the 1v1 queue! You are position #{queue_pos}. Keep chatting every 10 mins so you aren't marked AFK."}

        # --- QUEUE MANAGER: !queue (Check line) ---
        elif cmd == "!queue":
            q = db.query(WaitingListEntry).filter(WaitingListEntry.streamer_id == streamer_id).order_by(WaitingListEntry.joined_at.asc()).limit(3).all()
            if not q:
                return {"verdict": "Safe", "action": "Reply", "bot_response": "The 1v1 queue is currently empty! Type !join to enter."}
            
            names = [entry.user.username for entry in q]
            return {"verdict": "Safe", "action": "Reply", "bot_response": f"🎮 Next in line: 1. {names[0]} " + (" ".join([f"{i+2}. {n}" for i, n in enumerate(names[1:])]))}

        # --- QUEUE MANAGER: !next (MOD ONLY: Pull next player) ---
        elif cmd == "!next" and is_authorized_mod:
            next_player = db.query(WaitingListEntry).filter(WaitingListEntry.streamer_id == streamer_id).order_by(WaitingListEntry.joined_at.asc()).first()
            if not next_player:
                return {"verdict": "Safe", "action": "Reply", "bot_response": "❌ The queue is empty."}
                
            db.delete(next_player)
            db.commit()
            return {"verdict": "Safe", "action": "Reply", "bot_response": f"🔥 It's your turn, {next_player.user.username}! Send a request now."}

        # --- MODERATION COMMANDS ---
        elif cmd in valid_mgmt_cmds and is_authorized_mod:
            action = cmd
            
            # --- COMMAND 1: !adduk [trigger] [response text] ---
            if action == "!adduk" and len(words_original) >= 3:
                new_trigger = words_lower[1]
                if not new_trigger.startswith("!"):
                    new_trigger = f"!{new_trigger}"
                
                # Using original words array to preserve capitalization in the response
                response_content = " ".join(words_original[2:])
                
                # Check if command already exists to prevent accidental overwrites
                existing_cmd = db.query(CustomCommand).filter(
                    CustomCommand.streamer_id == streamer_id,
                    CustomCommand.command_trigger == new_trigger
                ).first()
                
                if existing_cmd:
                    return {"verdict": "Safe", "action": "Reply", "bot_response": f"❌ Command {new_trigger} already exists. Use !edituk to change it."}
                
                new_cmd = CustomCommand(
                    streamer_id=streamer_id,
                    command_trigger=new_trigger,
                    response_text=response_content
                )
                db.add(new_cmd)
                db.commit()
                return {"verdict": "Safe", "action": "Reply", "bot_response": f"✅ Command {new_trigger} has been successfully added!"}

            # --- COMMAND 2: !edituk [trigger] [new response text] ---
            elif action == "!edituk" and len(words_original) >= 3:
                target_trigger = words_lower[1]
                if not target_trigger.startswith("!"):
                    target_trigger = f"!{target_trigger}"
                
                # Preserve casing for response text
                response_content = " ".join(words_original[2:])
                
                existing_cmd = db.query(CustomCommand).filter(
                    CustomCommand.streamer_id == streamer_id,
                    CustomCommand.command_trigger == target_trigger
                ).first()
                
                if existing_cmd:
                    existing_cmd.response_text = response_content
                    db.commit()
                    return {"verdict": "Safe", "action": "Reply", "bot_response": f"✅ Command {target_trigger} has been successfully updated!"}
                else:
                    return {"verdict": "Safe", "action": "Reply", "bot_response": f"❌ Command {target_trigger} not found. Use !adduk to create it."}

            # --- COMMAND 3: !deluk [trigger] ---
            elif action == "!deluk" and len(words_original) == 2:
                target_trigger = words_lower[1]
                if not target_trigger.startswith("!"):
                    target_trigger = f"!{target_trigger}"
                    
                existing_cmd = db.query(CustomCommand).filter(
                    CustomCommand.streamer_id == streamer_id,
                    CustomCommand.command_trigger == target_trigger
                ).first()
                
                if existing_cmd:
                    db.delete(existing_cmd)
                    db.commit()
                    return {"verdict": "Safe", "action": "Reply", "bot_response": f"🗑️ Command {target_trigger} deleted successfully."}
                else:
                    return {"verdict": "Safe", "action": "Reply", "bot_response": f"❌ Command {target_trigger} not found."}

            # --- COMMAND 4: !reptuk [trigger] [interval_in_minutes] ---
            elif action == "!reptuk" and len(words_original) == 3:
                target_trigger = words_lower[1]
                if not target_trigger.startswith("!"):
                    target_trigger = f"!{target_trigger}"
                    
                try:
                    minutes = int(words_original[2])
                except ValueError:
                    return {"verdict": "Safe", "action": "Reply", "bot_response": "❌ Invalid interval. Usage: !reptuk !trigger 15"}
                    
                cmd_to_loop = db.query(CustomCommand).filter(
                    CustomCommand.streamer_id == streamer_id,
                    CustomCommand.command_trigger == target_trigger
                ).first()
                
                if cmd_to_loop:
                    cmd_to_loop.interval_minutes = minutes
                    db.commit()
                    status_msg = f"🔄 {target_trigger} is now looping every {minutes} minutes." if minutes > 0 else f"🛑 {target_trigger} loop timer disabled."
                    return {"verdict": "Safe", "action": "Reply", "bot_response": status_msg}
                else:
                    return {"verdict": "Safe", "action": "Reply", "bot_response": f"❌ Command {target_trigger} not found. Use !adduk first!"}

        # ---------------------------------------------------------
        # 4. CHAT COMMAND INTERCEPTOR (!clip & Custom Commands)
        # ---------------------------------------------------------
        elif cmd == "!clip":
            # Log the clip request in the database
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

            # Fire an instant WebSocket command to OBS to save the Replay Buffer
            streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
            if streamer and streamer.server_sync_code:
                await overlay_manager.send_alert(
                    streamer.server_sync_code, 
                    {
                        "type": "obs_save_replay_buffer", 
                        "message": "Chat triggered a clip!",
                        "clip_id": new_clip.id
                    }
                )

            return {
                "verdict": "Safe", 
                "action": "Command Executed", 
                "reason": "Stream clip triggered successfully via chat."
            }

        # Dynamic Custom Commands (Standard user triggering existing commands)
        elif cmd.startswith("!"):
            trigger = cmd
            
            custom_cmd = db.query(CustomCommand).filter(
                CustomCommand.streamer_id == streamer_id,
                CustomCommand.command_trigger == trigger,
                CustomCommand.is_active == True
            ).first()
            
            if custom_cmd:
                return {
                    "verdict": "Safe", 
                    "action": "Reply", 
                    "reason": "Custom command triggered.",
                    "bot_response": custom_cmd.response_text 
                }

    # ---------------------------------------------------------
    # 5. STANDARD AI MODERATION
    # ---------------------------------------------------------
    # Gather context tracking history from recent active database chat configurations
    recent_logs = db.query(ChatLog).filter(ChatLog.streamer_id == streamer_id).order_by(ChatLog.timestamp.desc()).limit(10).all()
    context_list = [log.message for log in reversed(recent_logs)]

    # Trust Vectors Bypass Logic
    if trust and (trust.is_whitelisted or trust.trust_score > 85.0):
        return {"verdict": "Safe", "action": "None", "reason": "High user trust score bypass applied."}

    # Layer 1: Run Local Rule Engine Engine (With Continuous Learning Shadow Support)
    local_engine = LocalRuleEngine(db=db, streamer_id=streamer_id)
    local_eval = local_engine.evaluate(message_text)
    
    if local_eval.get("verdict", "Questionable") != "Questionable":
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
    
    # Send shadow hits back to calibration
    if "shadow_triggers" in local_eval and local_eval["shadow_triggers"]:
        local_engine.calibrate_shadow_rules(local_eval["shadow_triggers"], ai_verdict.get("recommended_action"))

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
# VIP & COMMAND DASHBOARD ROUTES
# ---------------------------------------------------------
@app.post("/api/commands/add")
async def add_custom_command(
    request: Request, 
    command_trigger: str = Form(...), 
    response_text: str = Form(...), 
    db: Session = Depends(get_db)
):
    streamer_id = request.session.get("streamer_id")
    if not streamer_id:
        return RedirectResponse(url="/", status_code=303)

    clean_trigger = command_trigger.strip().lower()
    if not clean_trigger.startswith("!"):
        clean_trigger = f"!{clean_trigger}"

    new_cmd = CustomCommand(
        streamer_id=streamer_id,
        command_trigger=clean_trigger,
        response_text=response_text
    )
    db.add(new_cmd)
    db.commit()
    return RedirectResponse(url="/?success=command_added", status_code=303)

@app.post("/api/commands/delete")
async def delete_custom_command(request: Request, command_id: int = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        cmd = db.query(CustomCommand).filter(CustomCommand.id == command_id, CustomCommand.streamer_id == streamer_id).first()
        if cmd:
            db.delete(cmd)
            db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/api/vip/add")
async def add_vip_guest(
    request: Request, 
    target_username: str = Form(...), 
    custom_reply: str = Form(...), 
    db: Session = Depends(get_db)
):
    streamer_id = request.session.get("streamer_id")
    if not streamer_id:
        return RedirectResponse(url="/", status_code=303)

    clean_username = target_username.strip().lower()
    if not clean_username.startswith("@"):
        clean_username = f"@{clean_username}"

    new_vip = VIPGuest(
        streamer_id=streamer_id,
        target_username=clean_username,
        custom_reply=custom_reply
    )
    db.add(new_vip)
    db.commit()
    return RedirectResponse(url="/?success=vip_added", status_code=303)

@app.post("/api/vip/delete")
async def delete_vip_guest(request: Request, vip_id: int = Form(...), db: Session = Depends(get_db)):
    streamer_id = request.session.get("streamer_id")
    if streamer_id:
        vip = db.query(VIPGuest).filter(VIPGuest.id == vip_id, VIPGuest.streamer_id == streamer_id).first()
        if vip:
            db.delete(vip)
            db.commit()
    return RedirectResponse(url="/", status_code=303)

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
    task3 = asyncio.create_task(start_timed_command_loop())
    
    running_tasks.append(task1)
    running_tasks.append(task2)
    running_tasks.append(task3)
    
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