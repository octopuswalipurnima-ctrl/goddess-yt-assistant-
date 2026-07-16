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
        
        # --- Escalating AI Monitor ---
        self.monitored_users = {} # Format: {clean_username: {"yt_user_id": str, "strikes": 0, "last_checked": datetime}}
        self.is_ai_active = True
        
        self.spam_tracker = {}
        
        # --- VIP Trackers and Command Memory ---
        self.greeted_users = set()
        self.custom_commands = {}

        self.banned_words = {
            "mc", "bc", "bsdk", "mkc", "chutiya", "gandu", 
            "bitch", "fuck", "asshole", "madarchod", "bhenchod",
            "nigga", "nigger", "slut", "whore"
        }

    # ---------------------------------------------------------
    # API ACTION METHODS
    # ---------------------------------------------------------
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

    async def timeout_user(self, live_chat_id: str, channel_id: str, duration_seconds: int = 300):
        if not live_chat_id or not channel_id:
            return
        try:
            self.youtube.liveChatBans().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": live_chat_id,
                        "type": "temporary",
                        "temporaryBanDurationMinutes": int(duration_seconds / 60),
                        "bannedUserDetails": {"channelId": channel_id}
                    }
                }
            ).execute()
        except Exception as e:
            print(f"[YOUTUBE TIMEOUT ERROR]: {e}")

    async def ban_user(self, live_chat_id: str, channel_id: str):
        if not live_chat_id or not channel_id:
            return
        try:
            self.youtube.liveChatBans().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": live_chat_id,
                        "type": "permanent",
                        "bannedUserDetails": {"channelId": channel_id}
                    }
                }
            ).execute()
        except Exception as e:
            print(f"[YOUTUBE BAN ERROR]: {e}")

    def calculate_level_up(self, current_xp: int, current_level: int) -> int:
        xp_needed = current_level * 150
        if current_xp >= xp_needed:
            return current_level + 1
        return current_level


    # ---------------------------------------------------------
    # CORE MESSAGE PROCESSOR
    # ---------------------------------------------------------
    async def process_message(self, yt_user_id: str, username: str, message_text: str, message_id: str, streamer_id: int, live_chat_id: str, is_mod: bool):
        db = SessionLocal()
        try:
            streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
            webhook_url = streamer.discord_webhook_url if streamer else None

            text_words = message_text.lower().split()
            clean_username = username.lower().replace("@", "")
            command_text = message_text.strip().lower()

            # -----------------------------------------
            # NEW: MODERATOR COMMANDS
            # -----------------------------------------
            if is_mod and command_text.startswith("!"):
                parts = command_text.split(" ")
                command = parts[0]
                args = parts[1:]

                if command == "!so" and args:
                    target_channel = args[0].replace("@", "")
                    await self.send_message(f"🌟 Huge shoutout to {target_channel}! Go check out their amazing content and drop a sub!", live_chat_id)
                    return
                    
                elif command == "!warn" and args:
                    target_user = args[0].replace("@", "")
                    reason = " ".join(args[1:]) if len(args) > 1 else "No reason provided."
                    await self.send_message(f"⚠️ @{target_user}, please follow the channel rules. Reason: {reason}", live_chat_id)
                    return
                    
                elif command == "!ai":
                    if args and args[0] == "off":
                        self.is_ai_active = False
                        await self.send_message("🤖 AI Co-Host has been paused by a moderator.", live_chat_id)
                    elif args and args[0] == "on":
                        self.is_ai_active = True
                        await self.send_message("🤖 AI Co-Host is back online!", live_chat_id)
                    return
                    
                elif command == "!giveaway" and args and args[0] == "start":
                    await self.send_message("🎉 A giveaway has started! Type !join to enter!", live_chat_id)
                    return

                elif command == "!monitor" and args:
                    target_user = args[0].lower().replace("@", "")
                    self.monitored_users[target_user] = {
                        "yt_user_id": None, # Will populate when they speak
                        "strikes": 0,
                        "last_checked": datetime.min.replace(tzinfo=timezone.utc)
                    }
                    await self.send_message(f"👁️ AI is now actively monitoring {target_user} for violations.", live_chat_id)
                    return
                    
                elif command == "!unmonitor" and args:
                    target_user = args[0].lower().replace("@", "")
                    if target_user in self.monitored_users:
                        del self.monitored_users[target_user]
                        await self.send_message(f"🛑 AI has stopped monitoring {target_user}.", live_chat_id)
                    return

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
            # MODERATION 3: Escalating AI Scan
            # -----------------------------------------
            if clean_username in self.monitored_users and self.is_ai_active:
                user_data = self.monitored_users[clean_username]
                now = datetime.now(timezone.utc)
                
                # Save their ID so we can ban/timeout if necessary
                user_data["yt_user_id"] = yt_user_id
                
                time_since_last_check = (now - user_data["last_checked"]).total_seconds()
                
                # 5-MINUTE COOLDOWN CHECK
                if time_since_last_check >= 300: 
                    user_data["last_checked"] = now
                    
                    eval_result = await self.ai.evaluate_for_moderation(username, message_text)
                    
                    if eval_result.get("flagged"):
                        user_data["strikes"] += 1
                        strike_count = user_data["strikes"]
                        reason = eval_result.get('reason', 'AI flagged')
                        
                        await self.delete_message(message_id)
                        self.send_discord_log(webhook_url, f"AI Strike {strike_count}", username, message_text, reason)
                        
                        if strike_count == 1:
                            await self.send_message(f"⚠️ @{username}, your message was flagged for inappropriate behavior. Please follow the rules.", live_chat_id)
                            
                        elif strike_count == 2:
                            await self.send_message(f"⏱️ @{username} has been timed out by the AI for continued violations.", live_chat_id)
                            await self.timeout_user(live_chat_id, yt_user_id, duration_seconds=300)
                            
                        elif strike_count >= 3:
                            await self.send_message(f"🚫 @{username} has been hidden from the channel for repeated violations.", live_chat_id)
                            await self.ban_user(live_chat_id, yt_user_id)
                            del self.monitored_users[clean_username]
                        return


            # -----------------------------------------
            # CUSTOM COMMAND BUILDER
            # -----------------------------------------
            if message_text.lower().startswith("/goddess ") and is_mod:
                parts = message_text.split(" ", 2)
                if len(parts) >= 3:
                    trigger = parts[1].lower()
                    response = parts[2]
                    self.custom_commands[trigger] = response
                    await self.send_message(f"✅ Command '{trigger}' is now live!", live_chat_id)
                return
            
            if command_text in self.custom_commands:
                await self.send_message(self.custom_commands[command_text], live_chat_id)
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
                            author_details = item["authorDetails"]
                            author_name = author_details["displayName"]
                            author_id = author_details["channelId"]
                            msg_id = item["id"]
                            
                            # Determine if user is a mod/owner
                            is_mod = author_details.get("isChatModerator", False) or author_details.get("isChatOwner", False)
                            
                            await self.process_message(
                                yt_user_id=author_id, 
                                username=author_name, 
                                message_text=msg_text, 
                                message_id=msg_id,
                                streamer_id=streamer_id,
                                live_chat_id=chat_id,
                                is_mod=is_mod
                            )
                    except Exception as fetch_err:
                        print(f"[STREAM ENDED] 🔴 Disconnected from chat.")
                        del self.active_streams[streamer_id]

            except Exception as e:
                print(f"[CORE LOOP ERROR] {e}")
            finally:
                db.close()
            
            await asyncio.sleep(5)