import asyncio
import time
import requests
import threading
from datetime import datetime, timezone
import secrets
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from app.database.connection import SessionLocal
from app.database.models import User, XP, Coin, ChatLog, DiscordLink, Streamer
from app.ai.generator import AIBrain
from app.utils.config import Config

# ---------------------------------------------------------
# SHARED MEMORY: Used to pass video links from Discord to YouTube
# ---------------------------------------------------------
DETECTED_VIDEOS = set()


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
        
        # --- Multi-Tenant Trackers ---
        self.active_streams = {}  # Format: {streamer_id: live_chat_id}
        self.next_page_tokens = {} # Format: {live_chat_id: next_page_token}
        
        self.watched_users = set()
        self.spam_tracker = {}
        
        # --- VIP Trackers and Command Memory ---
        self.greeted_users = set()
        self.custom_commands = {}

        self.banned_words = {
            "mc", "bc", "bsdk", "mkc", "chutiya", "gandu", 
            "bitch", "fuck", "asshole", "madarchod", "bhenchod",
            "nigga", "nigger", "slut", "whore"
        }

    def send_discord_log(self, webhook_url: str, action_type: str, username: str, text: str, reason: str):
        if not webhook_url:
            return
        def fire_webhook():
            embed = {
                "title": f"🚨 Action: {action_type}",
                "description": f"**User:** {username}\n**Reason:** {reason}\n**Message:** `{text}`",
                "color": 16711680,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            try:
                requests.post(webhook_url, json={"embeds": [embed]})
            except Exception as e:
                pass
        threading.Thread(target=fire_webhook).start()

    async def send_message(self, text: str, live_chat_id: str):
        if not live_chat_id:
            return
        try:
            self.youtube.liveChatMessages().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": live_chat_id,
                        "type": "textMessageEvent",
                        "textMessageDetails": {"messageText": text}
                    }
                }
            ).execute()
        except Exception as e:
            print(f"[YOUTUBE SEND ERROR]: {e}")

    async def delete_message(self, message_id: str):
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

    async def process_message(self, yt_user_id: str, username: str, message_text: str, message_id: str, streamer_id: int, live_chat_id: str):
        db = SessionLocal()
        try:
            streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
            webhook_url = streamer.discord_webhook_url if streamer else None

            text_words = message_text.lower().split()
            clean_username = username.lower().replace("@", "")

            # -----------------------------------------
            # The VIP Arrival Greeter
            # -----------------------------------------
            if clean_username not in self.greeted_users:
                self.greeted_users.add(clean_username) 
                
                if clean_username in ["uk_hi_kahda", "goddessislive"]:
                    await self.send_message(f"👑 The bot owner arrived! Welcome @{username}!", live_chat_id)
                elif clean_username == "insinzu":
                    await self.send_message(f"welcome bhabhi 😁 @{username}", live_chat_id)

            # -----------------------------------------
            # MODERATION 1: The Spam Filter
            # -----------------------------------------
            current_time = time.time()
            user_times = self.spam_tracker.get(username, [])
            user_times = [t for t in user_times if current_time - t < 5]
            user_times.append(current_time)
            self.spam_tracker[username] = user_times
            
            if len(user_times) > 4:
                await self.delete_message(message_id)
                self.send_discord_log(webhook_url, "Spam Timeout", username, message_text, "Exceeded rate limit")
                return

            # -----------------------------------------
            # MODERATION 2: The Static Wall
            # -----------------------------------------
            if any(word in text_words for word in self.banned_words):
                await self.delete_message(message_id)
                self.send_discord_log(webhook_url, "Banned Word Filter", username, message_text, "Hardcoded blocklist")
                return 

            # -----------------------------------------
            # COMMANDS & CUSTOM COMMAND BUILDER
            # -----------------------------------------
            command_text = message_text.strip().lower()
            
            # Build a new command via chat
            if message_text.lower().startswith("/goddess "):
                parts = message_text.split(" ", 2)
                if len(parts) >= 3:
                    trigger = parts[1].lower()
                    response = parts[2]
                    self.custom_commands[trigger] = response
                    await self.send_message(f"✅ Command '{trigger}' is now live!", live_chat_id)
                return
            
            # Execute dynamically created commands
            if command_text in self.custom_commands:
                await self.send_message(self.custom_commands[command_text], live_chat_id)
                return

            # Standard Mod Commands
            if command_text.startswith("!watch "):
                target = message_text.split("@")[-1].strip()
                self.watched_users.add(target.lower())
                await self.send_message(f"👁️ Goddess AI is monitoring {target}.", live_chat_id)
                return
            elif command_text.startswith("!unwatch "):
                target = message_text.split("@")[-1].strip()
                self.watched_users.discard(target.lower())
                await self.send_message(f"✅ Goddess AI stopped monitoring {target}.", live_chat_id)
                return

            # -----------------------------------------
            # MODERATION 3: AI Scan
            # -----------------------------------------
            is_watched = username.lower() in self.watched_users
            if len(text_words) >= 3 or is_watched:
                eval_result = await self.ai.evaluate_for_moderation(username, message_text)
                if eval_result.get("flagged"):
                    await self.delete_message(message_id)
                    reason = eval_result.get('reason', 'AI flagged')
                    self.send_discord_log(webhook_url, "AI Scan", username, message_text, reason)
                    return 

            # -----------------------------------------
            # REWARDS & ECONOMY
            # -----------------------------------------
            user = db.query(User).filter(User.youtube_id == yt_user_id).first()
            if not user:
                user = User(youtube_id=yt_user_id, username=username)
                db.add(user)
                db.flush() 

                db.add(XP(user_id=user.id, streamer_id=streamer_id, current_xp=10, level=1, total_messages=1))
                db.add(Coin(user_id=user.id, balance=50, lifetime_earned=50))
                db.add(DiscordLink(user_id=user.id, sync_code=f"GODDESS-{secrets.token_hex(2).upper()}"))
            else:
                user.last_seen = datetime.now(timezone.utc)
                
                # Fetch their specific XP profile for THIS streamer
                xp_profile = db.query(XP).filter(XP.user_id == user.id, XP.streamer_id == streamer_id).first()
                if not xp_profile:
                    xp_profile = XP(user_id=user.id, streamer_id=streamer_id, current_xp=0, level=1, total_messages=0)
                    db.add(xp_profile)
                
                xp_profile.current_xp += 15 
                xp_profile.total_messages += 1
                
                # Coins are global across the platform
                user.coins[0].balance += 5  
                user.coins[0].lifetime_earned += 5
                
                new_level = self.calculate_level_up(xp_profile.current_xp, xp_profile.level)
                if new_level > xp_profile.level:
                    xp_profile.level = new_level

            if command_text == "!link":
                link_record = db.query(DiscordLink).filter(DiscordLink.user_id == user.id).first()
                if link_record:
                    await self.send_message(f"@{username}, your Discord code is: {link_record.sync_code}", live_chat_id)

            elif command_text == "!stats":
                xp_profile = db.query(XP).filter(XP.user_id == user.id, XP.streamer_id == streamer_id).first()
                await self.send_message(f"📊 @{username} | Level: {xp_profile.level} | Coins: 🪙 {user.coins[0].balance}", live_chat_id)

            db.add(ChatLog(stream_id=streamer_id, user_id=user.id, message=message_text))
            db.commit()

        except Exception as e:
            db.rollback()
            print(f"Error processing chat message: {e}")
        finally:
            db.close()


    # ---------------------------------------------------------
    # MULTI-TENANT ENGINE & API QUOTA MANAGER
    # ---------------------------------------------------------
    def get_chat_from_video(self, video_id: str):
        # This costs ONLY 1 Quota Unit!
        try:
            res = self.youtube.videos().list(
                part="snippet,liveStreamingDetails",
                id=video_id
            ).execute()
            
            if not res.get("items"):
                return None, None
                
            item = res["items"][0]
            channel_id = item["snippet"]["channelId"]
            details = item.get("liveStreamingDetails", {})
            active_chat_id = details.get("activeLiveChatId")
            
            return channel_id, active_chat_id
        except Exception as e:
            print(f"[YOUTUBE API ERROR] Fetching video details: {e}")
            return None, None

    async def run(self):
        print("[YOUTUBE DETECTOR] Event-Driven Engine Online. Waiting for Discord pings...")
        global DETECTED_VIDEOS
        
        while True:
            db = SessionLocal()
            try:
                # 1. Did Discord detect any new YouTube links?
                videos_to_check = list(DETECTED_VIDEOS)
                DETECTED_VIDEOS.clear() # Clear it so we don't double-check
                
                for video_id in videos_to_check:
                    print(f"[QUOTA MANAGER] Discord found a link! Verifying Video ID: {video_id}")
                    channel_id, chat_id = self.get_chat_from_video(video_id)
                    
                    if channel_id and chat_id:
                        # 2. Check if this channel belongs to a registered streamer
                        streamer = db.query(Streamer).filter(Streamer.youtube_channel_id == channel_id, Streamer.is_active == True).first()
                        
                        if streamer and streamer.id not in self.active_streams:
                            self.active_streams[streamer.id] = chat_id
                            print(f"[LIVE] 🟢 Connected to {streamer.channel_name}'s stream!")
                
                # 3. Pull chat for active streams (Costs 1 unit)
                active_streamer_ids = list(self.active_streams.keys())
                for streamer_id in active_streamer_ids:
                    chat_id = self.active_streams[streamer_id]
                    token = self.next_page_tokens.get(chat_id)
                    
                    try:
                        response = self.youtube.liveChatMessages().list(
                            liveChatId=chat_id,
                            part="snippet,authorDetails",
                            pageToken=token
                        ).execute()
                        
                        self.next_page_tokens[chat_id] = response.get("nextPageToken")
                        
                        for item in response.get("items", []):
                            msg_text = item["snippet"]["textMessageDetails"]["messageText"]
                            author_name = item["authorDetails"]["displayName"]
                            author_id = item["authorDetails"]["channelId"]
                            msg_id = item["id"]
                            
                            await self.process_message(
                                yt_user_id=author_id, 
                                username=author_name, 
                                message_text=msg_text, 
                                message_id=msg_id,
                                streamer_id=streamer_id,
                                live_chat_id=chat_id
                            )
                    except Exception as fetch_err:
                        print(f"[STREAM ENDED] 🔴 Disconnected from chat.")
                        del self.active_streams[streamer_id]

            except Exception as e:
                print(f"[CORE LOOP ERROR] {e}")
            finally:
                db.close()
            
            await asyncio.sleep(5)