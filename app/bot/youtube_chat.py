import asyncio
import random
from datetime import datetime, timezone
import secrets
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from app.database.connection import SessionLocal
from app.database.models import User, XP, Coin, ChatLog, DiscordLink
from app.ai.generator import AIBrain
from app.utils.config import Config

class YouTubeChatMonitor:
    def __init__(self):
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
        
        # --- NEW: Level 2 Mod Target List ---
        self.watched_users = set()

        # --- NEW: Level 1 Static Banned Words ---
        self.banned_words = {
            "mc", "bc", "bsdk", "mkc", "chutiya", "gandu", 
            "bitch", "fuck", "asshole", "madarchod", "bhenchod",
            "nigga", "nigger", "slut", "whore"
        }

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
                        "textMessageDetails": {"messageText": text}
                    }
                }
            )
            request.execute()
            print(f"[BOT SENT]: {text}")
        except Exception as e:
            print(f"[YOUTUBE SEND ERROR]: {e}")

    async def delete_message(self, message_id):
        """Deletes a specific toxic message from the YouTube live chat."""
        if not message_id:
            return
        try:
            self.youtube.liveChatMessages().delete(id=message_id).execute()
            print(f"[MODERATION] Successfully deleted message ID: {message_id}")
        except Exception as e:
            print(f"[YOUTUBE DELETE ERROR]: {e}")

    def calculate_level_up(self, current_xp: int, current_level: int) -> int:
        xp_needed = current_level * 150
        if current_xp >= xp_needed:
            return current_level + 1
        return current_level

    # Added message_id parameter so we know which message to delete!
    async def process_message(self, yt_user_id: str, username: str, message_text: str, message_id: str = None):
        """Processes chat, runs the 3-Tier moderation, and updates DB."""
        db = SessionLocal()
        try:
            # -----------------------------------------
            # MODERATION TIER 1: The Static Wall
            # -----------------------------------------
            text_words = message_text.lower().split()
            if any(word in text_words for word in self.banned_words):
                await self.delete_message(message_id)
                print(f"🚫 [LEVEL 1 BAN] {username} used a banned word.")
                return # Stop completely, do not give them XP or Coins

            # --- MOD COMMANDS ---
            command_text = message_text.strip().lower()
            if command_text.startswith("!watch "):
                target = message_text.split("@")[-1].strip()
                self.watched_users.add(target.lower())
                await self.send_message(f"👁️ Goddess AI is now closely monitoring {target}.")
                return
            elif command_text.startswith("!unwatch "):
                target = message_text.split("@")[-1].strip()
                self.watched_users.discard(target.lower())
                await self.send_message(f"✅ Goddess AI has stopped monitoring {target}.")
                return

            # -----------------------------------------
            # MODERATION TIER 2 & 3: Smart Pre-Filter & AI Scan
            # -----------------------------------------
            is_watched = username.lower() in self.watched_users
            
            # Only send to AI if it's a full sentence (3+ words) OR if the user is watched
            if len(text_words) >= 3 or is_watched:
                eval_result = await self.ai.evaluate_for_moderation(username, message_text)
                if eval_result.get("flagged"):
                    await self.delete_message(message_id)
                    print(f"🤖 [LEVEL 3 AI BAN] {username}: {eval_result.get('reason')}")
                    return # Stop completely, toxic message deleted

            # -----------------------------------------
            # REWARDS & ECONOMY (Only runs if they survive moderation)
            # -----------------------------------------
            user = db.query(User).filter(User.youtube_id == yt_user_id).first()
            if not user:
                user = User(youtube_id=yt_user_id, username=username)
                db.add(user)
                db.flush() 

                db.add(XP(user_id=user.id, current_xp=10, level=1, total_messages=1))
                db.add(Coin(user_id=user.id, balance=50, lifetime_earned=50))
                db.add(DiscordLink(user_id=user.id, sync_code=f"GODDESS-{secrets.token_hex(2).upper()}"))
            else:
                user.last_seen = datetime.now(timezone.utc)
                user.xp.current_xp += 15 
                user.xp.total_messages += 1
                user.coins.balance += 5  
                user.coins.lifetime_earned += 5
                
                new_level = self.calculate_level_up(user.xp.current_xp, user.xp.level)
                if new_level > user.xp.level:
                    user.xp.level = new_level
                    print(f"[LOYALTY] {username} leveled up to Level {new_level}!")

            # Check Standard Commands
            if command_text == "!link":
                link_record = db.query(DiscordLink).filter(DiscordLink.user_id == user.id).first()
                if link_record:
                    await self.send_message(f"@{username}, your Discord code is: {link_record.sync_code}")

            elif command_text == "!stats":
                await self.send_message(f"📊 @{username} | Level: {user.xp.level} | Coins: 🪙 {user.coins.balance}")

            # Save Message and DB Data
            if self.active_stream_id:
                db.add(ChatLog(stream_id=self.active_stream_id, user_id=user.id, message=message_text))
            db.commit()

            # The 10% Co-Host Trigger
            if not command_text.startswith("!"):
                if random.randint(1, 100) <= 10:
                    recent_logs = db.query(ChatLog, User.username).join(User).order_by(ChatLog.id.desc())
                    recent_msgs = [{"username": log.User.username, "text": log.ChatLog.message} for log in recent_logs[:15]]
                    
                    bgmi_context = ["You are a witty co-host for Goddess Live.", "Keep replies short."]
                    ai_comment = await self.ai.generate_chat_reaction(chat_context=bgmi_context, recent_messages=recent_msgs)
                    if ai_comment:
                        await self.send_message(ai_comment)

        except Exception as e:
            db.rollback()
            print(f"Error processing chat message: {e}")
        finally:
            db.close()

    async def run(self):
        print("[YOUTUBE DETECTOR] Scanning for active livestreams...")
        self.active_stream_id = 1
        while True:
            await asyncio.sleep(10)