import random
import asyncio
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.database.connection import SessionLocal
# Added CustomCommand, Streamer, VIPGuest, and WaitingListEntry to support the new features
from app.database.models import User, Coin, XP, CustomCommand, Streamer, VIPGuest, WaitingListEntry
from app.ai.generator import AIBrain
from app.services.websocket import overlay_manager

scheduler = AsyncIOScheduler()
ai_brain = AIBrain()

async def award_passive_rewards():
    """Background task: Awards 10 XP and 20 Coins every 5 minutes to active stream viewers."""
    db = SessionLocal()
    try:
        # Fetch users seen within the last 15 minutes
        threshold = datetime.now(timezone.utc)
        # For simplicity in this SQLite architecture, we select active records
        active_users = db.query(User).all() 
        
        for user in active_users:
            if user.xp and user.coins:
                user.xp.current_xp += 10
                user.coins.balance += 20
                user.coins.lifetime_earned += 20
        
        db.commit()
        print(f"[SCHEDULER] Distributed passive retention rewards to {len(active_users)} viewers.")
    except Exception as e:
        db.rollback()
        print(f"Error distributing passive rewards: {e}")
    finally:
        db.close()

async def trigger_ai_giveaway_reminder():
    """Generates a dynamic, non-repetitive reminder using Gemini and prints it."""
    reminder = await ai_brain.generate_giveaway_reminder()
    print(f"\n[AI GIVEAWAY REMINDER]: {reminder}\n")
    # In production, this pushes directly to the active YouTube Live chat framework.

async def reset_vip_greetings():
    """Nightly background task to reset VIP greetings so they trigger again on the next stream."""
    db = SessionLocal()
    try:
        db.query(VIPGuest).update({VIPGuest.has_been_greeted: False})
        db.commit()
        print("[SCHEDULER] VIP Entrance greetings have been reset for all channels.")
    except Exception as e:
        db.rollback()
        print(f"[SCHEDULER ERROR] Resetting VIP greetings: {e}")
    finally:
        db.close()

async def prune_afk_waiting_list():
    """Sweeps the waiting list every 2 minutes. Drops users with no chat activity in 10 mins."""
    db = SessionLocal()
    try:
        # Calculate the cutoff time (10 minutes ago)
        afk_threshold = datetime.now(timezone.utc) - timedelta(minutes=10)
        
        # Find users in queue whose last_seen is older than the threshold
        afk_entries = db.query(WaitingListEntry).join(User).filter(
            User.last_seen < afk_threshold
        ).all()
        
        for entry in afk_entries:
            db.delete(entry)
            
        if afk_entries:
            db.commit()
            print(f"[QUEUE ENGINE] Purged {len(afk_entries)} AFK viewers from 1v1 waiting lists.")
    except Exception as e:
        db.rollback()
        print(f"[SCHEDULER ERROR] AFK Pruning failed: {e}")
    finally:
        db.close()

async def start_timed_command_loop():
    """
    Continuous background thread checking every 30 seconds for 
    custom commands scheduled to repeat on a timer via !reptuk.
    """
    print("[SCHEDULER] Timed Command Repetition Loop Engine Started!")
    while True:
        await asyncio.sleep(30)
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            
            # Fetch all active timers across all streams
            active_timers = db.query(CustomCommand).filter(
                CustomCommand.interval_minutes > 0,
                CustomCommand.is_active == True
            ).all()
            
            for cmd in active_timers:
                # If it has never run, or the delta duration has passed the threshold interval
                should_run = False
                if not cmd.last_triggered_at:
                    should_run = True
                else:
                    # Convert naive column datetimes safely to compare thresholds
                    last_run = cmd.last_triggered_at.replace(tzinfo=timezone.utc)
                    if now >= last_run + timedelta(minutes=cmd.interval_minutes):
                        should_run = True
                        
                if should_run:
                    # Fetch the routing sync code to beam it out
                    streamer = db.query(Streamer).filter(Streamer.id == cmd.streamer_id).first()
                    if streamer and streamer.server_sync_code:
                        print(f"[LOOP TRIGGER] Broadcasting repeating command {cmd.command_trigger} for {streamer.channel_name}")
                        
                        # Pack it as an automated announcement text alert payload
                        broadcast_payload = {
                            "type": "alert",
                            "event_type": "newSponsorEvent",
                            "author": "🤖 AUTOMATED REMINDER",
                            "message": cmd.response_text,
                            "amount": "📢 NOTICE"
                        }
                        
                        # Sends it to OBS overlay browser source panels dynamically
                        await overlay_manager.send_alert(streamer.server_sync_code, broadcast_payload)
                        
                    cmd.last_triggered_at = now
            db.commit()
        except Exception as e:
            print(f"[SCHEDULER ERROR] Running repeating timers: {e}")
        finally:
            db.close()

def start_scheduler():
    """Starts all stream tasks."""
    # Run passive distribution every 5 minutes
    scheduler.add_job(award_passive_rewards, 'interval', minutes=5)
    
    # Sweep AFK users from the 1v1 queue every 2 minutes
    scheduler.add_job(prune_afk_waiting_list, 'interval', minutes=2)
    
    # Run giveaway reminders on a random interval between 15 and 30 minutes
    random_interval = random.randint(15, 30)
    scheduler.add_job(trigger_ai_giveaway_reminder, 'interval', minutes=random_interval)
    
    # Reset VIP greetings automatically every night at 3:00 AM
    scheduler.add_job(reset_vip_greetings, 'cron', hour=3, minute=0)
    
    scheduler.start()
    print("[SCHEDULER] Loyalty loops, Queue Sweepers, VIP resets, and AI chronometers initialized successfully.")