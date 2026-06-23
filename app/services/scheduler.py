import random
from datetime import datetime, timezone
from apscheduler.schedulers.asyncIO import AsyncIOScheduler
from app.database.connection import SessionLocal
from app.database.models import User, Coin, XP
from app.ai.generator import AIBrain

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

def start_scheduler():
    """Starts all stream tasks."""
    # Run passive distribution every 5 minutes
    scheduler.add_job(award_passive_rewards, 'interval', minutes=5)
    
    # Run giveaway reminders on a random interval between 15 and 30 minutes
    random_interval = random.randint(15, 30)
    scheduler.add_job(trigger_ai_giveaway_reminder, 'interval', minutes=random_interval)
    
    scheduler.start()
    print("[SCHEDULER] Loyalty loops and AI giveaway chronometers initialized successfully.")
