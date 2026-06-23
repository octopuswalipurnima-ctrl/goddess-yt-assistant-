import asyncio
from datetime import datetime, timezone
import secrets
from googleapiclient.discovery import build
from app.database.connection import SessionLocal
from app.database.models import User, XP, Coin, ChatLog, StreamSession, DiscordLink
from app.ai.generator import AIBrain
from app.utils.config import Config

class YouTubeChatMonitor:
    def __init__(self):
        self.youtube = build('youtube', 'v3', developerKey=Config.YOUTUBE_API_KEY)
        self.ai = AIBrain()
        self.active_stream_id = None
        self.live_chat_id = None

    def calculate_level_up(self, current_xp: int, current_level: int) -> int:
        """Progressively harder leveling formula: XP needed = level * 150"""
        xp_needed = current_level * 150
        if current_xp >= xp_needed:
            return current_level + 1
        return current_level

    async def process_message(self, yt_user_id: str, username: str, message_text: str):
        """Processes a chat message: Updates viewer account, XP, coins, and checks AI triggers."""
        db = SessionLocal()
        try:
            # 1. Fetch or create master user
            user = db.query(User).filter(User.youtube_id == yt_user_id).first()
            if not user:
                user = User(youtube_id=yt_user_id, username=username)
                db.add(user)
                db.flush() # Generates user.id

                # Init XP & Coins systems
                db.add(XP(user_id=user.id, current_xp=10, level=1, total_messages=1))
                db.add(Coin(user_id=user.id, balance=50, lifetime_earned=50))
                
                # Generate unique link code for Discord
                sync_code = f"GODDESS-{secrets.token_hex(2).upper()}"
                db.add(DiscordLink(user_id=user.id, sync_code=sync_code))
            else:
                user.last_seen = datetime.now(timezone.utc)
                user.xp.current_xp += 15 # +15 XP per message
                user.xp.total_messages += 1
                user.coins.balance += 5  # +5 Coins per message
                user.coins.lifetime_earned += 5
                
                # Check for Level Up
                new_level = self.calculate_level_up(user.xp.current_xp, user.xp.level)
                if new_level > user.xp.level:
                    user.xp.level = new_level
                    print(f"[LOYALTY] {username} leveled up to Level {new_level}!")

            # 2. Log message to history
            if self.active_stream_id:
                db.add(ChatLog(stream_id=self.active_stream_id, user_id=user.id, message=message_text))
            
            db.commit()

            # 3. Pull recent chat logs context for Gemini
            recent_logs = db.query(ChatLog, User.username).join(User).order_index = ChatLog.id.desc()
            recent_msgs = [{"username": log.User.username, "text": log.ChatLog.message} for log in recent_logs[:15]]
            
            # Let AI Co-Host evaluate context
            ai_comment = await self.ai.generate_chat_reaction(chat_context=[], recent_messages=recent_msgs)
            if ai_comment:
                print(f"[AI CO-HOST]: {ai_comment}")
                # In production, this would make an API call to insert a message into YouTube Live Chat.

        except Exception as e:
            db.rollback()
            print(f"Error processing chat message: {e}")
        finally:
            db.close()

    async def run(self):
        """Simulates polling live chat data loop safely."""
        print("[YOUTUBE DETECTOR] Scanning for active livestreams by Goddess...")
        # Simulating finding a live window
        self.active_stream_id = 1
        print("[YOUTUBE CONNECTED] Synchronized with YouTube Live Chat feed successfully.")
        
        while True:
            # Main polling execution sleep cycle
            await asyncio.sleep(10)