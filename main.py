import asyncio
from fastapi import FastAPI
import uvicorn
from app.database.connection import init_db
from app.dashboard.routes import router as dashboard_router
from app.bot.youtube_chat import YouTubeChatMonitor
from app.bot.discord_bot import start_discord_bot
from app.services.scheduler import start_scheduler

app = FastAPI(title="Goddess Stream Manager")

# Mount our dashboard panel routers
app.include_router(dashboard_router)

@app.on_event("startup")
async def startup_event():
    print("[STARTUP] Initializing systems...")
    
    # 1. Initialize SQLite tables
    init_db()
    
    # 2. Fire up background cron loops (XP distribution, AI giveaway timers)
    start_scheduler()
    
    # 3. Mount async worker integrations seamlessly into loop
    yt_monitor = YouTubeChatMonitor()
    asyncio.create_task(yt_monitor.run())
    asyncio.create_task(start_discord_bot())
    
    print("[STARTUP] Web Admin Dashboard, YouTube Engine, and Discord Bot are active!")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)