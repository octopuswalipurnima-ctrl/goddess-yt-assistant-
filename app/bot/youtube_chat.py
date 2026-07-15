import asyncio
import random
import time
import requests
import threading
from datetime import datetime, timezone
import secrets
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from app.database.connection import SessionLocal
# ADDED Streamer to the import list
from app.database.models import User, XP, Coin, ChatLog, DiscordLink, Streamer
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
        
        # Simulating the active streamer for now (Phase 1 Database)
        self.streamer_id = 1
        self.active_stream_id = "live_chat_id_placeholder"
        self.live_chat_id = None
        
        self.watched_users = set()
        
        # --- NEW: Anti-Spam Memory ---
        self.spam_tracker = {} # Tracks timestamps of when users send messages

        self.banned_words = {
            "mc", "bc", "bsdk", "mkc", "chutiya", "gandu", 
            "bitch", "fuck", "asshole", "madarchod", "bhenchod",
            "nigga", "nigger", "slut", "whore"
        }

    def send_discord_log(self, webhook_url: str, action_type: str, username: str, text: str, reason: str):
        """Fires a formatted audit log to the streamer's private Discord server."""
        if not webhook_url:
            return
            
        def fire_webhook():
            embed = {
                "title": f"🚨 Action: {action_type}",
                "description": f"**User:** {username}\n**Reason:** {reason}\n**Message:** `{text}`",
                "color": 16711680, # Red color
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            try:
                requests.post(webhook_url, json={"embeds": [embed]})
            except Exception as e:
                print(f"[DISCORD WEBHOOK ERROR]: {e}")
                
        # Run in a background thread so it doesn't slow down the YouTube chat reader
        threading.Thread(target=fire_webhook).start()

    async def send_message(self, text):
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
        except Exception as e:
            print(f"[YOUTUBE SEND ERROR]: {e}")

    async def delete_message(self, message_id):
        if not message_id:
            return
        try:
            self.youtube.liveChatMessages().delete(id=message_id).execute()
        except Exception as e:
            print(f"[YOUTUBE DELETE ERROR]: {e}")

    def calculate_level_up(self, current_xp: int, current_level: int) -> int:
        xp_needed = current_level * 150
        if current_xp >= xp_needed:
            return current_level + 1
        return current_level

    async def process_message(self, yt_user_id: str, username: str, message_text: str, message_id: str = None):
        db = SessionLocal()
        try:
            # Get the streamer's settings (so we know where to send the Discord log)
            streamer = db.query(Streamer).filter(Streamer.id == self.streamer_id).first()
            webhook_url = streamer.discord_webhook_url if streamer else None

            text_words = message_text.lower().split()

            # -----------------------------------------
            # MODERATION 1: The Spam Filter
            # -----------------------------------------
            current_time = time.time()
            user_times = self.spam_tracker.get(username, [])
            # Only keep timestamps from the last 5 seconds
            user_times = [t for t in user_times if current_time - t < 5]
            user_times.append(current_time)
            self.spam_tracker[username] = user_times
            
            if len(user_times) > 4: # If they send 5+ messages in 5 seconds
                await self.delete_message(message_id)
                self.send_discord_log(webhook_url, "Spam Timeout", username, message_text, "Exceeded rate limit (5+ messages in 5s)")
                return

            # -----------------------------------------
            # MODERATION 2: The Static Wall
            # -----------------------------------------
            if any(word in text_words for word in self.banned_words):
                await self.delete_message(message_id)
                self.send_discord_log(webhook_url, "Banned Word Filter", username, message_text, "Triggered hardcoded blocklist")
                return 

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
            # MODERATION 3: Smart Pre-Filter & AI Scan
            # -----------------------------------------
            is_watched = username.lower() in self.watched_users
            
            if len(text_words) >= 3 or is_watched:
                eval_result = await self.ai.evaluate_for_moderation(username, message_text)
                if eval_result.get("flagged"):
                    await self.delete_message(message_id)
                    reason = eval_result.get('reason', 'AI flagged as toxic')
                    self.send_discord_log(webhook_url, "AI Cultural Scan", username, message_text, reason)
                    return 

            # -----------------------------------------
            # REWARDS & ECONOMY
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

            # Check Standard Commands
            if command_text == "!link":
                link_record = db.query(DiscordLink).filter(DiscordLink.user_id == user.id).first()
                if link_record:
                    await self.send_message(f"@{username}, your Discord code is: {link_record.sync_code}")

            elif command_text == "!stats":
                await self.send_message(f"📊 @{username} | Level: {user.xp.level} | Coins: 🪙 {user.coins.balance}")

            if self.active_stream_id:
                db.add(ChatLog(stream_id=self.active_stream_id, user_id=user.id, message=message_text))
            db.commit()

        except Exception as e:
            db.rollback()
            print(f"Error processing chat message: {e}")
        finally:
            db.close()

    async def run(self):
        print("[YOUTUBE DETECTOR] Scanning for active livestreams...")
        self.active_stream_id = "1"
        while True:
            await asyncio.sleep(10)