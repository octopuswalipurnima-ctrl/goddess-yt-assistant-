import asyncio
import time
import requests
import threading
import random
import secrets
from datetime import datetime, timezone
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from app.database.connection import SessionLocal
from app.database.models import User, XP, Coin, ChatLog, DiscordLink, Streamer, SystemState
from app.ai.generator import AIBrain
from app.utils.config import Config
from app.services.websocket import overlay_manager

# ---------------------------------------------------------
# SHARED MEMORY & DEVELOPER OVERRIDES
# ---------------------------------------------------------
DETECTED_VIDEOS = {}  # Changed to Dictionary to accept Forced Streamer IDs
DISCONNECT_QUEUE = set()

# Accept Handles, Display Names, and Channel IDs
DEV_IDENTIFIERS = {
    "@uk_hi_kahda", "uk_hi_kahda", "uk hi kahda", "ukhikahda",
    "@goddessislive", "goddessislive", "goddess live",
    "@nawaboislive", "nawaboislive", "nawabo is live",
    "uccmwadkzxrenmmpzd5ek6pa"
}


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
        self.active_streams = {}  
        self.next_page_tokens = {} 
        self.stream_modes = {}
        
        # --- Escalating AI Monitor ---
        self.monitored_users = {} 
        self.is_ai_active = True
        self.spam_tracker = {}
        
        # --- VIP Trackers and Command Memory ---
        self.greeted_users = set()
        self.custom_commands = {}

        # --- BATTLE ROYALE STATE ---
        self.br_games = {}

        self.banned_words = {
            "mc", "bc", "bsdk", "mkc", "chutiya", "gandu", 
            "bitch", "fuck", "asshole", "madarchod", "bhenchod",
            "nigga", "nigger", "slut", "whore"
        }

    # ---------------------------------------------------------
    # API ACTION METHODS (NON-BLOCKING ASYNC FIX)
    # ---------------------------------------------------------
    def send_discord_log(self, webhook_url: str, action_type: str, username: str, text: str, reason: str):
        if not webhook_url: return
        def fire_webhook():
            embed = {
                "title": f"🚨 Action: {action_type}",
                "description": f"**User:** {username}\n**Reason:** {reason}\n**Message:** `{text}`",
                "color": 16711680,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            try: requests.post(webhook_url, json={"embeds": [embed]})
            except: pass
        threading.Thread(target=fire_webhook).start()

    async def send_message(self, text: str, live_chat_id: str):
        if not live_chat_id: return
        def _execute_send():
            return self.youtube.liveChatMessages().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": live_chat_id,
                        "type": "textMessageEvent",
                        "textMessageDetails": {"messageText": text}
                    }
                }
            ).execute()

        try:
            await asyncio.to_thread(_execute_send)
            print(f"[YOUTUBE CHAT SENT]: {text}")
        except Exception as e:
            print(f"[YOUTUBE SEND ERROR] Failed to send message '{text}': {e}")

    async def delete_message(self, message_id: str):
        if not message_id: return
        try:
            await asyncio.to_thread(lambda: self.youtube.liveChatMessages().delete(id=message_id).execute())
        except Exception as e: print(f"[YOUTUBE DELETE ERROR]: {e}")

    async def timeout_user(self, live_chat_id: str, channel_id: str, duration_seconds: int = 300):
        if not live_chat_id or not channel_id: return
        try:
            await asyncio.to_thread(lambda: self.youtube.liveChatBans().insert(
                part="snippet",
                body={"snippet": {"liveChatId": live_chat_id, "type": "temporary", "temporaryBanDurationMinutes": int(duration_seconds / 60), "bannedUserDetails": {"channelId": channel_id}}}
            ).execute())
        except Exception as e: print(f"[YOUTUBE TIMEOUT ERROR]: {e}")

    async def ban_user(self, live_chat_id: str, channel_id: str):
        if not live_chat_id or not channel_id: return
        try:
            await asyncio.to_thread(lambda: self.youtube.liveChatBans().insert(
                part="snippet",
                body={"snippet": {"liveChatId": live_chat_id, "type": "permanent", "bannedUserDetails": {"channelId": channel_id}}}
            ).execute())
        except Exception as e: print(f"[YOUTUBE BAN ERROR]: {e}")

    def calculate_level_up(self, current_xp: int, current_level: int) -> int:
        xp_needed = current_level * 150
        return current_level + 1 if current_xp >= xp_needed else current_level

    # ---------------------------------------------------------
    # BATTLE ROYALE BACKGROUND ENGINE
    # ---------------------------------------------------------
    async def run_br_game(self, live_chat_id: str):
        await asyncio.sleep(45) 
        
        game = self.br_games.get(live_chat_id)
        if not game or len(game['players']) < 2:
            await self.send_message("❌ Not enough players joined the Battle Royale. Cancelled.", live_chat_id)
            if game: game['state'] = 'ended'
            return

        game['state'] = 'running'
        await self.send_message(f"⚔️ BATTLE ROYALE BEGINS! {len(game['players'])} players drop in. May the best viewer win!", live_chat_id)

        while True:
            await asyncio.sleep(15) 
            
            alive = [uid for uid, data in game['players'].items() if data['lives'] > 0]
            if len(alive) <= 1: break

            if random.random() < 0.25:
                game['airdrop'] = True
                await self.send_message("🪂 AN AIRDROP HAS APPEARED! First alive player to type '!claim airdrop' gets an extra life!", live_chat_id)
                await asyncio.sleep(10) 
                if game['airdrop']:
                    game['airdrop'] = False 
                    await self.send_message("💨 The airdrop was lost to the zone...", live_chat_id)

            alive = [uid for uid, data in game['players'].items() if data['lives'] > 0]
            if len(alive) <= 1: break

            victim_id = random.choice(alive)
            game['players'][victim_id]['lives'] -= 1
            v_name = game['players'][victim_id]['name']

            if game['players'][victim_id]['lives'] > 0:
                await self.send_message(f"💥 @{v_name} took a fatal hit, but their Airdrop extra life saved them!", live_chat_id)
            else:
                death_msg = random.choice(["was sniped from across the map", "stepped on a landmine", "fell to the zone", "was eliminated by Goddess AI", "got ambushed in a bush"])
                await self.send_message(f"☠️ @{v_name} {death_msg}! {len(alive)-1} players remain.", live_chat_id)

        alive = [uid for uid, data in game['players'].items() if data['lives'] > 0]
        if len(alive) == 1:
            winner_id = alive[0]
            winner_name = game['players'][winner_id]['name']
            prize = game['prize']
            
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.youtube_id == winner_id).first()
                if user and user.coins:
                    user.coins[0].balance += prize
                    user.coins[0].lifetime_earned += prize
                    db.commit()
            except Exception as e: print(f"BR Prize Error: {e}")
            finally: db.close()

            await self.send_message(f"🏆 WINNER WINNER! @{winner_name} survived the Battle Royale and won 🪙 {prize} coins!", live_chat_id)
        else:
            await self.send_message("☠️ Everyone died in the final zone... No winners this time!", live_chat_id)

        game['state'] = 'ended'


    # ---------------------------------------------------------
    # DONATION & MEMBERSHIP PROCESSOR
    # ---------------------------------------------------------
    async def handle_support_event(self, event_type: str, snippet: dict, author_name: str, yt_user_id: str, streamer_id, live_chat_id: str, is_guest: bool = False):
        db = SessionLocal() if not is_guest else None
        try:
            message = ""
            coin_bonus = 0
            amount_str = ""

            if event_type == "superChatEvent":
                amount_str = snippet.get("superChatDetails", {}).get("displayString", "a Super Chat")
                message = f"🎉 WOW! Thank you so much @{author_name} for the {amount_str}! You are amazing!"
                coin_bonus = 500
            elif event_type == "superStickerEvent":
                amount_str = snippet.get("superStickerDetails", {}).get("displayString", "a Super Sticker")
                message = f"💖 Thank you @{author_name} for the {amount_str} Super Sticker!"
                coin_bonus = 300
            elif event_type == "newSponsorEvent":
                message = f"🎊 Welcome to the VIP family, @{author_name}! Thank you for becoming a member!"
                coin_bonus = 1000
            elif event_type == "membershipGiftingEvent":
                count = snippet.get("membershipGiftingDetails", {}).get("giftMembershipsCount", 1)
                message = f"🎁 INCREDIBLE! @{author_name} just gifted {count} memberships to the chat! Legend!"
                coin_bonus = 1000 * count
            elif event_type == "memberMilestoneChatEvent":
                months = snippet.get("memberMilestoneChatDetails", {}).get("memberMonth", 2)
                message = f"🎂 Happy {months} month membership anniversary, @{author_name}! Thanks for the continued support!"
                coin_bonus = 500

            if not is_guest and coin_bonus > 0:
                user = db.query(User).filter(User.youtube_id == yt_user_id).first()
                if not user:
                    user = User(youtube_id=yt_user_id, username=author_name)
                    db.add(user)
                    db.flush() 
                    db.add(XP(user_id=user.id, streamer_id=streamer_id, current_xp=0, level=1, total_messages=0))
                    db.add(Coin(user_id=user.id, balance=0, lifetime_earned=0))
                    db.add(DiscordLink(user_id=user.id, sync_code=f"GODDESS-{secrets.token_hex(2).upper()}"))
                    db.commit()

                if user.coins:
                    user.coins[0].balance += coin_bonus
                    user.coins[0].lifetime_earned += coin_bonus
                    db.commit()

                streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
                if streamer and streamer.server_sync_code:
                    alert_payload = {
                        "type": "alert",
                        "event_type": event_type,
                        "author": author_name,
                        "message": message,
                        "amount": amount_str
                    }
                    await overlay_manager.send_alert(streamer.server_sync_code, alert_payload)

            if message:
                await self.send_message(message, live_chat_id)
                
        except Exception as e:
            if not is_guest: db.rollback()
            print(f"[DONATION ERROR] {e}")
        finally:
            if not is_guest: db.close()


    # ---------------------------------------------------------
    # CORE MESSAGE PROCESSOR
    # ---------------------------------------------------------
    async def process_message(self, yt_user_id: str, username: str, message_text: str, message_id: str, streamer_id, live_chat_id: str, is_mod: bool, is_guest: bool = False):
        db = SessionLocal() if not is_guest else None
        try:
            text_words = message_text.lower().split()
            clean_username = username.strip().lower()
            command_text = message_text.strip().lower()

            # --- FLEXIBLE DEVELOPER OVERRIDE CHECK ---
            is_dev = (
                clean_username in DEV_IDENTIFIERS or 
                clean_username.replace(" ", "") in DEV_IDENTIFIERS or
                f"@{clean_username.replace(' ', '')}" in DEV_IDENTIFIERS or
                yt_user_id.lower() in DEV_IDENTIFIERS
            )
            
            if is_dev:
                is_mod = True

            # --- GUEST MODE OVERRIDE ---
            if is_guest:
                if is_mod and command_text.startswith("!"):
                    parts = command_text.split(" ")
                    cmd = parts[0]
                    args = parts[1:]
                    
                    if cmd == "!checkup":
                        await self.send_message("🤖 MOD CHECKUP: 1. !so 2. !giveaway start 3. /goddess !cmd response | Dev Discord: 998489383239946292", live_chat_id)
                    elif cmd == "!so" and args:
                        await self.send_message(f"🌟 Huge shoutout to {args[0].replace('@', '')}!", live_chat_id)
                    elif cmd == "!giveaway" and args and args[0] == "start":
                        await self.send_message("🎉 A giveaway has started! Type !join to enter!", live_chat_id)
                        
                if command_text.startswith("/goddess ") and is_mod:
                    parts = command_text.split(" ", 2)
                    if len(parts) >= 3:
                        self.custom_commands[parts[1].lower()] = parts[2]
                        await self.send_message(f"✅ Command '{parts[1].lower()}' is live!", live_chat_id)
                elif command_text in self.custom_commands:
                    await self.send_message(self.custom_commands[command_text], live_chat_id)
                    
                return # EXIT EARLY for Guest Mode

            # --- PREMIUM MODE LOGIC ---
            streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
            webhook_url = streamer.discord_webhook_url if streamer else None

            # 1. MODERATOR COMMANDS
            if is_mod and command_text.startswith("!"):
                parts = command_text.split(" ")
                command = parts[0]
                args = parts[1:]

                if command == "!checkup":
                    await self.send_message("🤖 MOD CHECKUP: 1. !adduk !test hi 2. !edituk !test yo 3. !deluk !test 4. !reptuk !test 5 5. !next | Dev Discord: 998489383239946292", live_chat_id)
                    return
                elif command == "!so" and args:
                    await self.send_message(f"🌟 Huge shoutout to {args[0].replace('@', '')}! Go check out their amazing content and drop a sub!", live_chat_id)
                    return
                elif command == "!warn" and args:
                    reason = " ".join(args[1:]) if len(args) > 1 else "No reason provided."
                    await self.send_message(f"⚠️ @{args[0].replace('@', '')}, please follow the channel rules. Reason: {reason}", live_chat_id)
                    return
                elif command == "!ai":
                    self.is_ai_active = (args[0] == "on") if args else self.is_ai_active
                    await self.send_message(f"🤖 AI Co-Host is now {'ONLINE' if self.is_ai_active else 'PAUSED'}.", live_chat_id)
                    return
                elif command == "!giveaway" and args and args[0] == "start":
                    await self.send_message("🎉 A giveaway has started! Type !join to enter!", live_chat_id)
                    return
                elif command == "!monitor" and args:
                    target_user = args[0].lower().replace("@", "")
                    self.monitored_users[target_user] = {"yt_user_id": None, "strikes": 0, "last_checked": datetime.min.replace(tzinfo=timezone.utc)}
                    await self.send_message(f"👁️ AI is now actively monitoring {target_user} for violations.", live_chat_id)
                    return
                elif command == "!unmonitor" and args:
                    target_user = args[0].lower().replace("@", "")
                    if target_user in self.monitored_users:
                        del self.monitored_users[target_user]
                        await self.send_message(f"🛑 AI has stopped monitoring {target_user}.", live_chat_id)
                    return
                elif command == "!br" and args and args[0] == "start":
                    prize = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1000
                    if live_chat_id in self.br_games and self.br_games[live_chat_id]['state'] != 'ended':
                        await self.send_message("⚔️ A Battle Royale is already running!", live_chat_id)
                        return
                    
                    self.br_games[live_chat_id] = {"state": "waiting", "players": {}, "airdrop": False, "prize": prize}
                    await self.send_message(f"⚔️ BATTLE ROYALE starting! Prize: 🪙 {prize}! Type !joinbr to enter. Game begins in 45 seconds!", live_chat_id)
                    asyncio.create_task(self.run_br_game(live_chat_id))
                    return

            # 2. AUTOMATED MODERATION
            if any(word in text_words for word in self.banned_words):
                await self.delete_message(message_id)
                self.send_discord_log(webhook_url, "Banned Word Filter", username, message_text, "Hardcoded blocklist")
                return 

            current_time = time.time()
            user_times = self.spam_tracker.get(username, [])
            user_times = [t for t in user_times if current_time - t < 5]
            user_times.append(current_time)
            self.spam_tracker[username] = user_times
            if len(user_times) > 4:
                await self.delete_message(message_id)
                self.send_discord_log(webhook_url, "Spam Timeout", username, message_text, "Exceeded rate limit")
                return

            if clean_username.replace("@", "") in self.monitored_users and self.is_ai_active:
                user_data = self.monitored_users[clean_username.replace("@", "")]
                now = datetime.now(timezone.utc)
                user_data["yt_user_id"] = yt_user_id
                
                if (now - user_data["last_checked"]).total_seconds() >= 300: 
                    user_data["last_checked"] = now
                    eval_result = await self.ai.evaluate_for_moderation(username, message_text)
                    if eval_result.get("flagged"):
                        user_data["strikes"] += 1
                        await self.delete_message(message_id)
                        if user_data["strikes"] == 1: await self.send_message(f"⚠️ @{username}, warning for inappropriate behavior.", live_chat_id)
                        elif user_data["strikes"] == 2:
                            await self.send_message(f"⏱️ @{username} timed out by AI.", live_chat_id)
                            await self.timeout_user(live_chat_id, yt_user_id, 300)
                        else:
                            await self.send_message(f"🚫 @{username} hidden for repeated violations.", live_chat_id)
                            await self.ban_user(live_chat_id, yt_user_id)
                            del self.monitored_users[clean_username.replace("@", "")]
                        return

            # 3. REWARDS & ECONOMY SETUP
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
                xp_profile = db.query(XP).filter(XP.user_id == user.id, XP.streamer_id == streamer_id).first()
                if not xp_profile:
                    xp_profile = XP(user_id=user.id, streamer_id=streamer_id, current_xp=0, level=1, total_messages=0)
                    db.add(xp_profile)
                
                xp_profile.current_xp += 15 
                xp_profile.total_messages += 1
                user.coins[0].balance += 5  
                user.coins[0].lifetime_earned += 5
                
                new_level = self.calculate_level_up(xp_profile.current_xp, xp_profile.level)
                if new_level > xp_profile.level: xp_profile.level = new_level

            # 4. VIEWER COMMANDS
            parts = command_text.split()
            cmd = parts[0] if parts else ""

            if cmd == "!link":
                link_record = db.query(DiscordLink).filter(DiscordLink.user_id == user.id).first()
                if link_record: await self.send_message(f"@{username}, your Discord code is: {link_record.sync_code}", live_chat_id)
            elif cmd == "!stats":
                xp_prof = db.query(XP).filter(XP.user_id == user.id, XP.streamer_id == streamer_id).first()
                await self.send_message(f"📊 @{username} | Level: {xp_prof.level} | Coins: 🪙 {user.coins[0].balance}", live_chat_id)

            elif cmd in ["!flip", "!dice", "!spin"]:
                if len(parts) < 2 or not parts[1].isdigit():
                    await self.send_message(f"❌ @{username}, specify an amount! (e.g., {cmd} 10)", live_chat_id)
                else:
                    bet = int(parts[1])
                    if bet <= 0 or user.coins[0].balance < bet:
                        await self.send_message(f"❌ @{username}, you don't have enough coins for that bet!", live_chat_id)
                    else:
                        user.coins[0].balance -= bet 
                        
                        if cmd == "!flip":
                            choice = parts[2] if len(parts)>2 and parts[2] in ["heads", "tails"] else "heads"
                            result = random.choice(["heads", "tails"])
                            if choice == result:
                                win = bet * 2
                                user.coins[0].balance += win
                                user.coins[0].lifetime_earned += win
                                await self.send_message(f"🪙 Flipped {result}! @{username} wins 🪙 {win}!", live_chat_id)
                            else:
                                await self.send_message(f"🪙 Flipped {result}... @{username} lost 🪙 {bet}.", live_chat_id)
                                
                        elif cmd == "!dice":
                            roll = random.randint(1, 6)
                            if roll in [4, 5]:
                                win = int(bet * 1.5)
                                user.coins[0].balance += win
                                user.coins[0].lifetime_earned += win
                                await self.send_message(f"🎲 Rolled a {roll}! @{username} wins 🪙 {win}!", live_chat_id)
                            elif roll == 6:
                                win = bet * 3
                                user.coins[0].balance += win
                                user.coins[0].lifetime_earned += win
                                await self.send_message(f"🎲 CRITICAL ROLL 6! @{username} wins 🪙 {win}!", live_chat_id)
                            else:
                                await self.send_message(f"🎲 Rolled a {roll}... @{username} lost 🪙 {bet}.", live_chat_id)
                                
                        elif cmd == "!spin":
                            multiplier = random.choice([0, 0, 0.5, 1.2, 2, 3])
                            win = int(bet * multiplier)
                            user.coins[0].balance += win
                            user.coins[0].lifetime_earned += win
                            if multiplier == 0:
                                await self.send_message(f"🎡 The wheel landed on 0x... @{username} lost everything.", live_chat_id)
                            else:
                                await self.send_message(f"🎡 The wheel landed on {multiplier}x! @{username} walks away with 🪙 {win}!", live_chat_id)

            elif cmd == "!joinbr":
                game = self.br_games.get(live_chat_id)
                if game and game['state'] == 'waiting':
                    if yt_user_id not in game['players']:
                        game['players'][yt_user_id] = {"name": username, "lives": 1}
                        await self.send_message(f"✅ @{username} dropped into the Battle Royale!", live_chat_id)
                        
            elif command_text == "!claim airdrop":
                game = self.br_games.get(live_chat_id)
                if game and game['state'] == 'running' and game['airdrop']:
                    p = game['players'].get(yt_user_id)
                    if p and p['lives'] > 0:
                        p['lives'] += 1
                        game['airdrop'] = False
                        await self.send_message(f"🪂 @{username} claimed the Airdrop! You now have {p['lives']} lives!", live_chat_id)

            # Custom Commands (Trigger Check)
            if command_text in self.custom_commands:
                await self.send_message(self.custom_commands[command_text], live_chat_id)

            db.add(ChatLog(stream_id=streamer_id, user_id=user.id, message=message_text))
            db.commit()

        except Exception as e:
            if not is_guest: db.rollback()
            print(f"Error processing chat message: {e}")
        finally:
            if not is_guest: db.close()


    # ---------------------------------------------------------
    # MULTI-TENANT ENGINE & API QUOTA MANAGER
    # ---------------------------------------------------------
    def get_chat_from_video(self, video_id: str):
        try:
            res = self.youtube.videos().list(part="snippet,liveStreamingDetails", id=video_id).execute()
            if not res.get("items"): return None, None
            item = res["items"][0]
            return item["snippet"]["channelId"], item.get("liveStreamingDetails", {}).get("activeLiveChatId")
        except Exception as e:
            print(f"[YOUTUBE API ERROR] Fetching video details: {e}")
            return None, None

    async def run(self):
        print("[YOUTUBE DETECTOR] Event-Driven Engine Online. Waiting for Discord pings...")
        global DETECTED_VIDEOS, DISCONNECT_QUEUE
        
        while True:
            db = SessionLocal()
            try:
                # 1. Handle incoming Disconnect Requests first
                disconnects = list(DISCONNECT_QUEUE)
                DISCONNECT_QUEUE.clear()
                for vid in disconnects:
                    keys_to_del = [k for k, v in self.active_streams.items() if v.get("video_id") == vid]
                    for k in keys_to_del:
                        del self.active_streams[k]
                        if k in self.stream_modes:
                            del self.stream_modes[k]
                        print(f"[DISCONNECT] Bot manually removed from video {vid}")

                # 2. Process new Video links
                videos_to_check = dict(DETECTED_VIDEOS)
                DETECTED_VIDEOS.clear() 
                
                for video_id, forced_streamer_id in videos_to_check.items():
                    channel_id, chat_id = self.get_chat_from_video(video_id)
                    
                    if channel_id and chat_id:
                        # >>> GOD MODE CHECK: Use Forced Dev Profile if present, otherwise auto-detect
                        if forced_streamer_id:
                            streamer = db.query(Streamer).filter(Streamer.id == forced_streamer_id).first()
                        else:
                            streamer = db.query(Streamer).filter(Streamer.youtube_channel_id == channel_id, Streamer.is_active == True).first()
                        
                        stream_key = streamer.id if streamer else f"guest_{channel_id}"
                        is_guest = streamer is None
                        
                        if stream_key not in self.active_streams:
                            self.active_streams[stream_key] = {"chat_id": chat_id, "video_id": video_id, "start_time": datetime.now(timezone.utc)}
                            self.stream_modes[stream_key] = "guest" if is_guest else "premium"
                            
                            mode_str = "🟡 GUEST" if is_guest else "🟢 PREMIUM"
                            name = streamer.channel_name if streamer else f"Guest Stream ({channel_id})"
                            print(f"[LIVE] {mode_str} Connected to {name}'s chat!")

                            if is_guest:
                                await self.send_message("👋 Hello! Goddess AI (Guest Mode) has successfully connected to the chat!", chat_id)
                            else:
                                await self.send_message("🤖 mod hajir hai janab uk malik ki kami nhi hone dega 😁😸 (Mods type !checkup)", chat_id)
                
                # 3. Read Chat Messages
                active_streamer_ids = list(self.active_streams.keys())
                for streamer_key in active_streamer_ids:
                    chat_info = self.active_streams[streamer_key]
                    chat_id = chat_info["chat_id"] if isinstance(chat_info, dict) else chat_info
                    token = self.next_page_tokens.get(chat_id)
                    is_guest = self.stream_modes.get(streamer_key) == "guest"
                    
                    try:
                        sys_state = db.query(SystemState).first()
                        if sys_state and sys_state.youtube_api_calls >= sys_state.youtube_api_cap:
                            print(f"[API CAP] YouTube API cap reached ({sys_state.youtube_api_calls}/{sys_state.youtube_api_cap}). Skipping fetch.")
                            continue

                        response = self.youtube.liveChatMessages().list(liveChatId=chat_id, part="snippet,authorDetails", pageToken=token).execute()

                        if sys_state:
                            sys_state.youtube_api_calls += 1
                            db.commit()

                        self.next_page_tokens[chat_id] = response.get("nextPageToken")
                        
                        for item in response.get("items", []):
                            snippet = item["snippet"]
                            event_type = snippet["type"]
                            author_details = item["authorDetails"]
                            
                            author_name = author_details["displayName"]
                            author_id = author_details["channelId"]
                            msg_id = item["id"]
                            is_mod = author_details.get("isChatModerator", False) or author_details.get("isChatOwner", False)
                            
                            if event_type == "textMessageEvent":
                                msg_text = snippet["textMessageDetails"]["messageText"]
                                await self.process_message(author_id, author_name, msg_text, msg_id, streamer_key, chat_id, is_mod, is_guest)
                            elif event_type in ["superChatEvent", "superStickerEvent", "newSponsorEvent", "membershipGiftingEvent", "memberMilestoneChatEvent"]:
                                await self.handle_support_event(event_type, snippet, author_name, author_id, streamer_key, chat_id, is_guest)
                                
                    except Exception as fetch_err:
                        print(f"[STREAM ENDED] 🔴 Disconnected from chat.")
                        del self.active_streams[streamer_key]
                        if streamer_key in self.stream_modes:
                            del self.stream_modes[streamer_key]

            except Exception as e:
                print(f"[CORE LOOP ERROR] {e}")
            finally:
                db.close()
            
            await asyncio.sleep(5)