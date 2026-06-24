import asyncio
import random
from datetime import datetime, timezone
import secrets
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from app.database.connection import SessionLocal
from app.database.models import User, XP, Coin, ChatLog, StreamSession, DiscordLink
from app.ai.generator import AIBrain
from app.utils.config import Config

class YouTubeChatMonitor:
    def __init__(self):
        # 1. The secure OAuth connection replaces the old developerKey
        self.credentials = Credentials(
            token=None,
            refresh_token=Config.YOUTUBE_REFRESH_TOKEN,
            client_id=Config.YOUTUBE_CLIENT_ID,
            client_secret=Config.YOUTUBE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token"
        )
        self.youtube = build('youtube', 'v3', credentials=self.credentials)
        self.ai = AIBrain()
        self.active_stream_id = None
        self.live_chat_id = None

    async def send_message(self, text):
        """Physically types a message into the YouTube live chat."""
        if not self.live_chat_id:
            return
        
        try:
            request = self.youtube.liveChatMessages().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": self.live_chat_id,
                        "type": "textMessageEvent",
                        "textMessageDetails": {
                            "messageText": text
                        }
                    }
                }
            )
            request.execute()
            print(f"[BOT SENT]: {text}")
        except Exception as e:
            print(f"[YOUTUBE SEND ERROR]: {e}")

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

            # --- THE CHAT COMMANDS SYSTEM ---
            command_text = message_text.strip().lower()

            # Command 1: !link (Get Discord Code)
            if command_text == "!link":
                link_record = db.query(DiscordLink).filter(DiscordLink.user_id == user.id).first()
                if link_record:
                    reply_msg = f"@{username}, your secret Discord sync code is: {link_record.sync_code} — Go type /link in our Discord server!"
                    await self.send_message(reply_msg)

            # Command 2: !stats (Check Balance & Level)
            elif command_text == "!stats":
                reply_msg = f"📊 @{username} | Level: {user.xp.level} | XP: {user.xp.current_xp} | Coins: 🪙 {user.coins.balance}"
                await self.send_message(reply_msg)

            # 2. Log message to history
            if self.active_stream_id:
                db.add(ChatLog(stream_id=self.active_stream_id, user_id=user.id, message=message_text))
            
            # Save all the coin changes and XP to the database!
            db.commit()

            # 3. Pull recent chat logs context for Gemini
            recent_logs = db.query(ChatLog, User.username).join(User).order_by(ChatLog.id.desc())
            recent_msgs = [{"username": log.User.username, "text": log.ChatLog.message} for log in recent_logs[:15]]
            
            # --- THE 10% CO-HOST FEATURE ---
            # Don't let the AI interrupt commands, only trigger on normal chat
            if not command_text.startswith("!"):
                dice_roll = random.randint(1, 100)
                if dice_roll <= 10:
                    print(f"[CO-HOST] 10% chance hit! Evaluating context for {username}...")
                    
                    bgmi_context = [
                        "You are a witty co-host for a BGMI gaming stream named Goddess Live.",
                        "Keep replies short, engaging, and hype up the chat.",
                        "If relevant to the conversation, you can drop subtle nods to playing with no gyroscope or maintaining clean AR recoil."
                    ]
                    
                    ai_comment = await self.ai.generate_chat_reaction(chat_context=bgmi_context, recent_messages=recent_msgs)
                    
                    if ai_comment:
                        await self.send_message(ai_comment)

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