import os
import random
import asyncio
import logging
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.database.connection import SessionLocal

from app.database.models import User, Coin, XP, CustomCommand, Streamer, VIPGuest, WaitingListEntry
from app.ai.generator import AIBrain
from app.services.websocket import overlay_manager
from app.services.youtube.yt_api_manager import yt_api_manager
from app.bot.youtube_chat import DETECTED_VIDEOS

logger = logging.getLogger("goddess_stream_manager")

scheduler = AsyncIOScheduler()
ai_brain = AIBrain()

# Global variable to track if birthday wish was already sent today
BIRTHDAY_WISHED_DATE = None

async def award_passive_rewards():
    """Awards passive coins and XP to active viewers across all active streams every 5 minutes."""
    db = SessionLocal()
    try:
        streamers = db.query(Streamer).filter(Streamer.is_active == True).all()
        for streamer in streamers:
            eff_id = streamer.effective_id
            xp_records = db.query(XP).filter(XP.streamer_id == eff_id).all()
            for xp_rec in xp_records:
                xp_rec.current_xp += 10
                user = db.query(User).filter(User.id == xp_rec.user_id).first()
                if user and user.coins:
                    user.coins[0].balance += 20
                    user.coins[0].lifetime_earned += 20
        db.commit()
        logger.info("[SCHEDULER] Distributed passive rewards to active viewers.")
    except Exception as e:
        db.rollback()
        logger.error(f"[SCHEDULER ERROR] Error distributing rewards: {e}")
    finally:
        db.close()

async def trigger_ai_giveaway_reminder():
    try:
        reminder = await ai_brain.generate_giveaway_reminder()
        logger.info(f"[AI GIVEAWAY REMINDER]: {reminder}")
    except Exception as e:
        logger.error(f"[SCHEDULER ERROR] Failed AI giveaway reminder: {e}")

async def reset_vip_greetings():
    db = SessionLocal()
    try:
        db.query(VIPGuest).update({VIPGuest.has_been_greeted: False})
        db.commit()
    except Exception as e:
        db.rollback()
    finally:
        db.close()

async def prune_afk_waiting_list():
    db = SessionLocal()
    try:
        afk_threshold = datetime.now(timezone.utc) - timedelta(minutes=10)
        afk_entries = db.query(WaitingListEntry).join(User).filter(User.last_seen < afk_threshold).all()
        for entry in afk_entries:
            db.delete(entry)
        if afk_entries:
            db.commit()
    except Exception as e:
        db.rollback()
    finally:
        db.close()

async def start_timed_command_loop():
    """
    Background loop that continuously monitors CustomCommands 
    and posts them to active YouTube live chats when intervals expire.
    """
    logger.info("[SCHEDULER] Timed Command Repetition Loop Engine Started!")
    while True:
        await asyncio.sleep(60) # Scan database every 60 seconds
        
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            # Find commands that are active and have an interval set
            active_timers = db.query(CustomCommand).filter(
                CustomCommand.interval_minutes > 0,
                CustomCommand.is_active == True
            ).all()
            
            for cmd in active_timers:
                last_run = cmd.last_triggered_at.replace(tzinfo=timezone.utc) if cmd.last_triggered_at else datetime.min.replace(tzinfo=timezone.utc)
                
                if now >= last_run + timedelta(minutes=cmd.interval_minutes):
                    # Loop through all videos the bot is currently watching
                    for video_id, streamer_id in list(DETECTED_VIDEOS.items()):
                        # Check if this command belongs to the streamer currently broadcasting this video
                        if streamer_id == cmd.streamer_id:
                            _, chat_id = await yt_api_manager.get_chat_from_video(video_id)
                            if chat_id:
                                # Post directly to YouTube Chat
                                await yt_api_manager.send_chat_message(chat_id, cmd.response_text)
                                logger.info(f"[REPETITION] Posted {cmd.command_trigger} to video {video_id}")
                    
                    cmd.last_triggered_at = now
            db.commit()
        except Exception as e:
            logger.error(f"[REPETITION ERROR] {e}")
        finally:
            db.close()

async def websub_renewal_loop():
    await asyncio.sleep(15) 
    while True:
        base_url = os.environ.get("BASE_URL", "").rstrip("/")
        if base_url:
            callback_url = f"{base_url}/api/youtube-webhook"
            hub_url = "https://pubsubhubbub.appspot.com/subscribe"
            
            def renew():
                db = SessionLocal()
                try:
                    streamers = db.query(Streamer).filter(Streamer.youtube_channel_id != None).all()
                    for streamer in streamers:
                        topic_url = f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={streamer.youtube_channel_id}"
                        data = {"hub.callback": callback_url, "hub.topic": topic_url, "hub.verify": "async", "hub.mode": "subscribe"}
                        req = urllib.request.Request(hub_url, data=urllib.parse.urlencode(data).encode("utf-8"), method="POST")
                        try:
                            urllib.request.urlopen(req)
                        except: pass
                finally:
                    db.close()
            await asyncio.to_thread(renew)
        await asyncio.sleep(259200) # 3 days


async def check_goddess_birthday_wish():
    """
    Checks if today is August 12th in IST (India Standard Time).
    Sends a special birthday wish in YouTube Live Chat at 12:00 AM midnight
    or whenever Goddess goes live on 12/08.
    """
    global BIRTHDAY_WISHED_DATE
    
    # Calculate Indian Standard Time (UTC + 5 hours 30 mins)
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    today_str = ist_now.strftime("%Y-%m-%d")
    
    # Target: August 12th (Month 8, Day 12)
    if ist_now.month == 8 and ist_now.day == 12:
        # Only execute if we haven't wished her today yet
        if BIRTHDAY_WISHED_DATE != today_str:
            if DETECTED_VIDEOS:
                db = SessionLocal()
                try:
                    # Find Goddess's internal DB ID using her exact YouTube Channel ID
                    goddess = db.query(Streamer).filter(Streamer.youtube_channel_id == "UCGH_osSgL2FCsBYe6XMxlSQ").first()
                    
                    if goddess:
                        for video_id, streamer_id in list(DETECTED_VIDEOS.items()):
                            # If her stream is currently detected as active
                            if streamer_id == goddess.id:
                                _, chat_id = await yt_api_manager.get_chat_from_video(video_id)
                                if chat_id:
                                    birthday_message = (
                                        "🎂🎉 HAPPY BIRTHDAY GODDESS! 🥳✨ "
                                        "Wishing you an epic year ahead filled with insane clutches, master-class recoil, "
                                        "and non-stop chicken dinners! Have the most incredible day! ❤️🎮👑"
                                    )
                                    await yt_api_manager.send_chat_message(chat_id, birthday_message)
                                    
                                    # Mark as completed for today
                                    BIRTHDAY_WISHED_DATE = today_str
                                    logger.info(f"[BIRTHDAY] 🎈 Sent birthday wish to Goddess in chat {chat_id}!")
                                    break
                except Exception as e:
                    logger.error(f"[BIRTHDAY ERROR] {e}")
                finally:
                    db.close()


def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(award_passive_rewards, 'interval', minutes=5)
        scheduler.add_job(prune_afk_waiting_list, 'interval', minutes=2)
        scheduler.add_job(reset_vip_greetings, 'cron', hour=3)
        
        # Check for Goddess's Birthday trigger every 1 minute
        scheduler.add_job(check_goddess_birthday_wish, 'interval', minutes=1, id='goddess_birthday', replace_existing=True)
        
        scheduler.start()
        logger.info("[SCHEDULER] Background maintenance jobs & Birthday Checker initialized.")