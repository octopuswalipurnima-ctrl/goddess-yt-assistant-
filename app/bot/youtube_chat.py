import asyncio
import time
import random
import secrets
from datetime import datetime, timezone, timedelta

from app.database.connection import SessionLocal
from app.database.models import User, XP, Coin, ChatLog, DiscordLink, Streamer, SystemState, CustomCommand, WaitingListEntry, AutoLearnedRule, AuditLog, ChatCommandExecution
from app.ai.generator import AIBrain
from app.services.youtube.yt_api_manager import yt_api_manager
from app.services.chat_commands import ChatActor, ChatCommandService
from app.services.discord_events import discord_events
from app.services.emergency_stop import emergency_stop

# ---------------------------------------------------------
# SHARED MEMORY & DEVELOPER OVERRIDES
# ---------------------------------------------------------
DETECTED_VIDEOS = {}  
DISCONNECT_QUEUE = set()

MANUAL_MOD_MODE = {}    # Asking Mode (Default: True)
AI_OBSERVER_MODE = {}   # Learning Mode (Default: True)
PENDING_ACTIONS = {}


class YouTubeChatMonitor:
    def __init__(self):
        self.ai = AIBrain()
        
        self.active_streams = {}  
        self.monitored_users = {} 
        self.spam_tracker = {}
        self.hardened_rules = {}  
        
        self.greeted_users = set()
        self.custom_commands = {}
        self.br_games = {}
        # Per-stream, bounded metadata needed by moderator chat actions.  We keep
        # identifiers only; this is not an unbounded chat history.
        self.recent_messages = {}

        self.banned_words = {
            "mc", "bc", "bsdk", "mkc", "chutiya", "gandu", 
            "bitch", "fuck", "asshole", "madarchod", "bhenchod",
            "nigga", "nigger", "slut", "whore"
        }

    def load_learned_rules_for_streamer(self, streamer_id: int, db):
        """Loads dynamically trained AI rules from the DB into runtime memory."""
        if streamer_id not in self.hardened_rules:
            self.hardened_rules[streamer_id] = set()

        try:
            learned_rules = db.query(AutoLearnedRule).filter(
                AutoLearnedRule.streamer_id == streamer_id,
                AutoLearnedRule.status == 'active'
            ).all()

            for rule in learned_rules:
                if rule.pattern:
                    self.hardened_rules[streamer_id].add(rule.pattern.lower().strip())
        except Exception as e:
            print(f"[RULES LOAD ERROR] Could not load learned rules: {e}")

    # ---------------------------------------------------------
    # API ACTION METHODS (ROUTED VIA YT API MANAGER)
    # ---------------------------------------------------------
    def send_discord_log(self, channel_id: str, action_type: str, username: str, text: str, reason: str):
        """Queue structured logs; Discord outages cannot stall chat moderation."""
        discord_events.emit(f"Moderation: {action_type}", f"User: {username}\nReason: {reason}\nMessage: {text[:800]}", channel_id)

    async def send_message(self, text: str, live_chat_id: str):
        if not live_chat_id: return
        res = await yt_api_manager.send_chat_message(live_chat_id, text)
        if res:
            print(f"[YOUTUBE CHAT SENT]: {text}")

    async def delete_message(self, message_id: str):
        if not message_id: return
        await yt_api_manager.delete_chat_message(message_id)

    async def timeout_user(self, live_chat_id: str, channel_id: str, duration_seconds: int = 300):
        if not live_chat_id or not channel_id: return
        await yt_api_manager.ban_or_timeout_user(live_chat_id, channel_id, duration_seconds, is_permanent=False)

    async def ban_user(self, live_chat_id: str, channel_id: str):
        if not live_chat_id or not channel_id: return
        await yt_api_manager.ban_or_timeout_user(live_chat_id, channel_id, 0, is_permanent=True)

    def calculate_level_up(self, current_xp: int, current_level: int) -> int:
        xp_needed = current_level * 150
        return current_level + 1 if current_xp >= xp_needed else current_level

    # ---------------------------------------------------------
    # 🧠 AI OBSERVER ENGINE
    # ---------------------------------------------------------
    async def observe_and_learn(self, action_type: str, target_name: str, target_id: str, effective_id: int, webhook_url: str):
        if not AI_OBSERVER_MODE.get(effective_id, True): return
        
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.youtube_id == target_id).first()
            if not user: return
            
            recent_logs = db.query(ChatLog).filter(ChatLog.streamer_id == effective_id, ChatLog.user_id == user.id).order_by(ChatLog.timestamp.desc()).limit(3).all()
            if not recent_logs: return
            
            context_msgs = [log.message for log in reversed(recent_logs)]
            chat_history_str = " | ".join(context_msgs)
            
            trigger_message = recent_logs[0].message.lower().strip()
            
            if effective_id not in self.hardened_rules:
                self.hardened_rules[effective_id] = set()
                
            if len(trigger_message) > 3:
                self.hardened_rules[effective_id].add(trigger_message)
                hardened_status = f"`{trigger_message}` permanently added to Layer 1 Strict Blocklist."
            else:
                hardened_status = "Context too short for absolute strict filtering."

            insight = (
                f"**Observed Rule Enforcement:**\n"
                f"Moderator executed `{action_type}` against {target_name}.\n\n"
                f"**Contextual Chat History:**\n\"{chat_history_str}\"\n\n"
                f"**🛡️ Rules Layer Hardened:**\n{hardened_status}\n"
            )
            
            print(f"[AI OBSERVER] Learned from action on {target_name}. Rule Layer Hardened.")
            self.send_discord_log(webhook_url, "🧠 AI Observer: Rule Hardened", target_name, insight, "Automated Ruleset Calibration")
            
        except Exception as e:
            print(f"[OBSERVER ERROR] {e}")
        finally:
            db.close()

    # ---------------------------------------------------------
    # BATTLE ROYALE ENGINE
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
    # DONATION PROCESSOR
    # ---------------------------------------------------------
    async def handle_support_event(self, event_type: str, snippet: dict, author_name: str, yt_user_id: str, actual_id: int, effective_id: int, live_chat_id: str, is_guest: bool = False):
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
                    user = User(channel_id=yt_user_id, youtube_id=yt_user_id, youtube_user_id=yt_user_id, username=author_name)
                    db.add(user)
                    db.flush() 
                    db.add(XP(user_id=user.id, streamer_id=effective_id, current_xp=0, level=1, total_messages=0))
                    db.add(Coin(user_id=user.id, balance=0, lifetime_earned=0))
                    db.add(DiscordLink(user_id=user.id, sync_code=f"GODDESS-{secrets.token_hex(2).upper()}"))
                    db.commit()

                if user.coins:
                    user.coins[0].balance += coin_bonus
                    user.coins[0].lifetime_earned += coin_bonus
                    db.commit()

                streamer = db.query(Streamer).filter(Streamer.id == actual_id).first()
                if streamer:
                    discord_events.emit("YouTube support event", f"{event_type}: {author_name} {amount_str}"[:1800], streamer_id=streamer.id)

            if message: await self.send_message(message, live_chat_id)
        except Exception as e:
            if not is_guest: db.rollback()
        finally:
            if not is_guest: db.close()

    # ---------------------------------------------------------
    # CORE MESSAGE PROCESSOR
    # ---------------------------------------------------------
    async def process_message(self, yt_user_id: str, username: str, message_text: str, message_id: str, actual_id: int, effective_id: int, live_chat_id: str, is_mod: bool, is_guest: bool = False, is_owner: bool = False):
        db = SessionLocal() if not is_guest else None
        processing_stage = "initialization"
        try:
            text_words = message_text.lower().split()
            clean_username = username.strip().lower()
            command_text = message_text.strip().lower()

            # GUEST MODE OVERRIDE
            if is_guest:
                if is_mod and command_text.startswith("!"):
                    parts = command_text.split(" ")
                    cmd = parts[0]
                    args = parts[1:]
                    
                    if cmd in ["!checkup", "!cheakup"]:
                        await self.send_message("🤖 GUEST MOD CHECKUP: 1. !so 2. !giveaway start 3. /goddess !cmd response | Dev Discord: 998489383239946292", live_chat_id)
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
                
                print(f"✅ [CHAT CAUGHT] {username}: {message_text}")
                return 

            # PREMIUM MODE LOGIC
            streamer = db.query(Streamer).filter(Streamer.id == actual_id).first()
            webhook_url = streamer.discord_log_channel_id if streamer else None

            if emergency_stop.is_stopped(db):
                # Continue recording/reading chat, but do not perform outbound actions.
                return

            # Central command path for production operations.  It is deliberately
            # entered before the legacy command branches so one message cannot
            # perform both a new and legacy mutation.
            command_response = ChatCommandService(
                db, effective_id, message_id,
                ChatActor(yt_user_id, username, is_mod, is_owner),
            ).execute(message_text)
            if command_response is not None:
                await self.send_message(command_response, live_chat_id)
                return

            # Legacy moderation remains bound to the monitor's current-stream
            # buffer, but gets the same replay ledger as central commands.
            if is_mod and command_text.split(" ", 1)[0] in {"!delmsg", "!tout", "!hid", "!mod"}:
                if db.query(ChatCommandExecution).filter_by(streamer_id=effective_id, message_id=message_id).first():
                    return
                db.add(ChatCommandExecution(streamer_id=effective_id, message_id=message_id, command=command_text.split(" ", 1)[0]))

            # A bounded current-stream buffer allows !delmsg to act on a real
            # YouTube message id rather than resolving arbitrary user input.
            if not command_text.startswith("!"):
                messages = self.recent_messages.setdefault(effective_id, [])
                messages.append({"message_id": message_id, "yt_user_id": yt_user_id, "username": username, "is_mod": is_mod})
                del messages[:-100]
            
            manual_mod_approval = MANUAL_MOD_MODE.get(effective_id, True)
            ai_cohost_enabled = getattr(streamer, 'ai_cohost_enabled', True)

            # 1. MODERATOR COMMANDS
            if is_mod and command_text.startswith("!"):
                parts = command_text.split(" ")
                command = parts[0]
                args = parts[1:]

                if command in ["!checkup", "!cheakup"]:
                    await self.send_message("🤖 PREMIUM MOD CHECKUP: 1. !adduk !test hi 2. !edituk !test yo 3. !deluk !test 4. !reptuk !test 5 5. !timeout @user [secs] 6. !ban @user 7. !next | Dev Discord: 998489383239946292", live_chat_id)
                    return
                elif command == "!adduk" and len(args) >= 2:
                    trig = args[0].strip().lower()
                    if not trig.startswith("!"): trig = f"!{trig}"
                    resp = " ".join(args[1:])
                    existing = db.query(CustomCommand).filter(CustomCommand.streamer_id == effective_id, CustomCommand.command_trigger == trig).first()
                    if existing:
                        existing.response_text = resp
                    else:
                        db.add(CustomCommand(streamer_id=effective_id, command_trigger=trig, response_text=resp))
                    db.commit()
                    await self.send_message(f"✅ Command '{trig}' created/updated!", live_chat_id)
                    return
                elif command == "!edituk" and len(args) >= 2:
                    trig = args[0].strip().lower()
                    if not trig.startswith("!"): trig = f"!{trig}"
                    resp = " ".join(args[1:])
                    existing = db.query(CustomCommand).filter(CustomCommand.streamer_id == effective_id, CustomCommand.command_trigger == trig).first()
                    if existing:
                        existing.response_text = resp
                        db.commit()
                        await self.send_message(f"✏️ Command '{trig}' updated!", live_chat_id)
                    else:
                        await self.send_message(f"❌ Command '{trig}' does not exist.", live_chat_id)
                    return
                elif command == "!deluk" and args:
                    trig = args[0].strip().lower()
                    if not trig.startswith("!"): trig = f"!{trig}"
                    existing = db.query(CustomCommand).filter(CustomCommand.streamer_id == effective_id, CustomCommand.command_trigger == trig).first()
                    if existing:
                        db.delete(existing)
                        db.commit()
                        await self.send_message(f"🗑️ Command '{trig}' deleted!", live_chat_id)
                    else:
                        await self.send_message(f"❌ Command '{trig}' not found.", live_chat_id)
                    return
                elif command == "!reptuk" and len(args) >= 2:
                    trig = args[0].strip().lower()
                    if not trig.startswith("!"): trig = f"!{trig}"
                    
                    try:
                        interval = int(args[1])
                        existing = db.query(CustomCommand).filter(CustomCommand.streamer_id == effective_id, CustomCommand.command_trigger == trig).first()
                        
                        if existing:
                            existing.interval_minutes = interval
                            existing.is_active = (interval > 0)
                            db.commit()
                            
                            if interval > 0:
                                await self.send_message(f"⏱️ Timer set! '{trig}' will now auto-post every {interval} minutes.", live_chat_id)
                            else:
                                await self.send_message(f"🛑 Timer stopped for '{trig}'.", live_chat_id)
                        else:
                            await self.send_message(f"❌ Command '{trig}' not found. Create it with !adduk first.", live_chat_id)
                    except ValueError:
                        await self.send_message("❌ Invalid format. Use: !reptuk !command <minutes>", live_chat_id)
                    return
                elif command == "!so" and args:
                    await self.send_message(f"🌟 Huge shoutout to {args[0].replace('@', '')}! Go check out their content!", live_chat_id)
                    return
                elif command == "!monitor" and args:
                    target_user = args[0].lower().replace("@", "")
                    self.monitored_users[target_user] = {"yt_user_id": None, "strikes": 0, "last_checked": datetime.min.replace(tzinfo=timezone.utc)}
                    await self.send_message(f"👁️ AI is actively monitoring {target_user}.", live_chat_id)
                    return
                elif command == "!mod" and args and args[0] in {"allow", "ban", "ignore"}:
                    # Resolve only the most recently queued review for this stream;
                    # chat cannot provide an arbitrary review id or cross streams.
                    pending = next(
                        ((name, data) for name, data in reversed(list(PENDING_ACTIONS.items())) if data.get("streamer_id") == effective_id),
                        None,
                    )
                    if not pending:
                        await self.send_message("❌ No moderation review is pending.", live_chat_id)
                        return
                    target, action_data = pending
                    decision = args[0]
                    if decision == "ban":
                        if "msg_id" in action_data:
                            await self.delete_message(action_data["msg_id"])
                        if not await yt_api_manager.ban_or_timeout_user(live_chat_id, action_data["yt_id"], is_permanent=True):
                            await self.send_message("❌ YouTube rejected the moderation action.", live_chat_id)
                            return
                        audit_action, reply = "MOD_REVIEW_BANNED", f"🚫 @{target} hidden from chat."
                    elif decision == "allow":
                        audit_action, reply = "MOD_REVIEW_ALLOWED", f"✅ AI review for @{target} allowed."
                    else:
                        audit_action, reply = "MOD_REVIEW_IGNORED", f"✅ AI review for @{target} ignored."
                    actor = db.query(User).filter(User.youtube_id == yt_user_id).first()
                    if actor: db.add(AuditLog(streamer_id=effective_id, user_id=actor.id, action=audit_action, details=target))
                    del PENDING_ACTIONS[target]; db.commit()
                    await self.send_message(reply, live_chat_id)
                    return
                elif command == "!punish" and args:
                    target = args[0].lower().replace("@", "")
                    if target in PENDING_ACTIONS:
                        action_data = PENDING_ACTIONS[target]
                        strikes = action_data["strikes"]
                        
                        if "msg_id" in action_data:
                            await self.delete_message(action_data["msg_id"])
                        
                        if strikes == 1:
                            await self.send_message(f"⚠️ @{target}, you have been officially warned by Mods.", live_chat_id)
                            await self.observe_and_learn("Formal Mod Warning", target, action_data["yt_id"], effective_id, webhook_url)
                        elif strikes >= 2:
                            await self.send_message(f"⏱️ @{target} timed out by Mods.", live_chat_id)
                            await self.timeout_user(live_chat_id, action_data["yt_id"], 300)
                            await self.observe_and_learn("Mod 5-Minute Timeout", target, action_data["yt_id"], effective_id, webhook_url)
                        del PENDING_ACTIONS[target]
                    return
                elif command == "!ignore" and args:
                    target = args[0].lower().replace("@", "")
                    if target in PENDING_ACTIONS:
                        await self.send_message(f"✅ AI flag for @{target} dismissed by Mods.", live_chat_id)
                        del PENDING_ACTIONS[target]
                        if target in self.monitored_users:
                            self.monitored_users[target]["strikes"] = max(0, self.monitored_users[target]["strikes"] - 1)
                    return
                elif command == "!timeout" and args:
                    target = args[0].lower().replace("@", "")
                    duration = int(args[1]) if len(args) > 1 and args[1].isdigit() else 300
                    target_user_record = db.query(User).filter(User.username.ilike(f"%{target}%")).first()
                    if target_user_record:
                        await self.timeout_user(live_chat_id, target_user_record.youtube_id, duration)
                        await self.send_message(f"⏱️ @{target} has been timed out for {duration} seconds.", live_chat_id)
                        await self.observe_and_learn(f"Mod Explicit {duration}s Timeout", target, target_user_record.youtube_id, effective_id, webhook_url)
                    else:
                        await self.send_message(f"❌ Could not find YouTube ID for @{target}.", live_chat_id)
                    return
                elif command == "!tout" and args:
                    target = args[0].lower().replace("@", "")
                    duration = int(args[1]) if len(args) > 1 and args[1].isdigit() else 300
                    if not 1 <= duration <= 86400:
                        await self.send_message("❌ Timeout must be between 1 and 86400 seconds.", live_chat_id)
                        return
                    target_user_record = db.query(User).join(ChatLog).filter(ChatLog.streamer_id == actual_id, User.username.ilike(target)).order_by(ChatLog.id.desc()).first()
                    if target_user_record and await yt_api_manager.ban_or_timeout_user(live_chat_id, target_user_record.youtube_id, duration):
                        actor = db.query(User).filter(User.youtube_id == yt_user_id).first()
                        if actor: db.add(AuditLog(streamer_id=effective_id, user_id=actor.id, action="VIEWER_TIMEOUT", details=f"{target_user_record.youtube_id}:{duration}"))
                        db.commit()
                        await self.send_message(f"⏱️ @{target_user_record.username} timed out for {duration}s.", live_chat_id)
                    else:
                        await self.send_message("❌ Timeout failed; target was not found in this stream or YouTube rejected it.", live_chat_id)
                    return
                elif command == "!ban" and args:
                    target = args[0].lower().replace("@", "")
                    target_user_record = db.query(User).filter(User.username.ilike(f"%{target}%")).first()
                    if target_user_record:
                        await self.ban_user(live_chat_id, target_user_record.youtube_id)
                        await self.send_message(f"🚫 @{target} has been permanently banned from chat.", live_chat_id)
                        await self.observe_and_learn("Mod Explicit Permanent Ban", target, target_user_record.youtube_id, effective_id, webhook_url)
                    else:
                        await self.send_message(f"❌ Could not find YouTube ID for @{target}.", live_chat_id)
                    return
                elif command == "!hid" and args:
                    target = args[0].lower().replace("@", "")
                    target_user_record = db.query(User).join(ChatLog).filter(ChatLog.streamer_id == actual_id, User.username.ilike(target)).order_by(ChatLog.id.desc()).first()
                    if target_user_record and await yt_api_manager.ban_or_timeout_user(live_chat_id, target_user_record.youtube_id, is_permanent=True):
                        actor = db.query(User).filter(User.youtube_id == yt_user_id).first()
                        if actor: db.add(AuditLog(streamer_id=effective_id, user_id=actor.id, action="VIEWER_HIDDEN", details=target_user_record.youtube_id))
                        db.commit()
                        await self.send_message(f"🚫 @{target_user_record.username} hidden from chat.", live_chat_id)
                    else:
                        await self.send_message("❌ Hide failed; target was not found in this stream or YouTube rejected it.", live_chat_id)
                    return
                elif command == "!delmsg":
                    candidates = [entry for entry in self.recent_messages.get(effective_id, []) if not entry["is_mod"]]
                    if not candidates:
                        await self.send_message("❌ No eligible recent message exists.", live_chat_id)
                        return
                    target = candidates[-1]
                    if await yt_api_manager.delete_chat_message(target["message_id"]):
                        actor = db.query(User).filter(User.youtube_id == yt_user_id).first()
                        if actor: db.add(AuditLog(streamer_id=effective_id, user_id=actor.id, action="MESSAGE_DELETED", details=target["yt_user_id"]))
                        db.commit()
                        await self.send_message("✅ Most recent eligible message deleted.", live_chat_id)
                    else:
                        await self.send_message("❌ YouTube did not delete that message.", live_chat_id)
                    return
                elif command == "!next":
                    next_user = db.query(WaitingListEntry).filter(WaitingListEntry.streamer_id == effective_id).order_by(WaitingListEntry.joined_at.asc()).first()
                    if next_user:
                        target_name = next_user.user.username
                        db.delete(next_user)
                        db.commit()
                        await self.send_message(f"⚔️ UP NEXT: @{target_name}! Get ready for the 1v1 Arena!", live_chat_id)
                    else:
                        await self.send_message("❌ The 1v1 queue is currently empty.", live_chat_id)
                    return
                elif command == "!next1v1":
                    next_user = db.query(WaitingListEntry).filter(WaitingListEntry.streamer_id == effective_id).order_by(WaitingListEntry.joined_at.asc(), WaitingListEntry.id.asc()).first()
                    if next_user:
                        target_name = next_user.user.username; db.delete(next_user); db.commit()
                        await self.send_message(f"⚔️ UP NEXT: @{target_name}!", live_chat_id)
                    else:
                        await self.send_message("❌ The 1v1 queue is empty.", live_chat_id)
                    return

            # ---------------------------------------------------------
            # 💬 AI CHAT CO-HOST: DIRECT QUESTION & ANSWER SYSTEM
            # ---------------------------------------------------------
            bot_names = ["goddess", "goddess ai", "@goddess", "bot", "honey", "honey bunny"]
            
            if any(name in command_text for name in bot_names):
                if ai_cohost_enabled:
                    processing_stage = "ai_cohost"
                    now = time.time()
                    if effective_id not in self.monitored_users:
                        self.monitored_users[effective_id] = {}
                        
                    last_reply_time = self.monitored_users[effective_id].get('last_bot_reply', 0)

                    # 15-SECOND RATE LIMIT: Fast enough to chat, slow enough to block spam
                    if now - last_reply_time > 15:
                        recent_logs = db.query(ChatLog).filter(ChatLog.streamer_id == effective_id).order_by(ChatLog.timestamp.desc()).limit(6).all()
                        context = [{"username": log.user.username, "text": log.message} for log in reversed(recent_logs)]
                        
                        persona_context = None
                        if getattr(streamer, "persona_enabled", False):
                            persona_context = {"persona_enabled": True, "personality_mode": streamer.personality_mode}
                            speaker_role = "stream owner" if is_owner else "moderator" if is_mod else "viewer"
                            direct_prompt = [f"A {speaker_role} named '{username}' said: '{message_text}'. Reply naturally."]
                        else:
                            direct_prompt = [f"User '{username}' is directly talking to you. They said: '{message_text}'. Reply to them naturally and answer their question."]
                        
                        reaction = await self.ai.generate_chat_reaction(direct_prompt, context, persona_context)
                        
                        if reaction:
                            clean_reaction = reaction.replace(f"@{username}", "").strip()
                            await self.send_message(f"@{username} {clean_reaction}", live_chat_id)
                            self.monitored_users[effective_id]['last_bot_reply'] = now

            # 2. AUTOMATED MODERATION
            if any(word in text_words for word in self.banned_words):
                await self.delete_message(message_id)
                self.send_discord_log(webhook_url, "Banned Word Filter", username, message_text, "Hardcoded blocklist")
                return 

            if effective_id in self.hardened_rules:
                if any(learned_rule in command_text for learned_rule in self.hardened_rules[effective_id]):
                    await self.delete_message(message_id)
                    await self.timeout_user(live_chat_id, yt_user_id, 300)
                    self.send_discord_log(webhook_url, "🛡️ Hardened AI Filter Enforcement", username, message_text, "Matched previously learned Mod Action.")
                    return 

            # Spam Detection
            current_time = time.time()
            user_times = self.spam_tracker.get(username, [])
            user_times = [t for t in user_times if current_time - t < 5]
            user_times.append(current_time)
            self.spam_tracker[username] = user_times
            if len(user_times) > 4:
                await self.delete_message(message_id)
                self.send_discord_log(webhook_url, "Spam Timeout", username, message_text, "Exceeded rate limit")
                return

            # AI Moderation Engine
            clean_target = clean_username.replace("@", "")
            if clean_target in self.monitored_users and ai_cohost_enabled:
                user_data = self.monitored_users[clean_target]
                now = datetime.now(timezone.utc)
                user_data["yt_user_id"] = yt_user_id
                
                if (now - user_data.get("last_checked", datetime.min.replace(tzinfo=timezone.utc))).total_seconds() >= 300: 
                    user_data["last_checked"] = now
                    eval_result = await self.ai.evaluate_for_moderation(username, message_text)
                    if eval_result.get("flagged"):
                        user_data["strikes"] += 1
                        
                        if manual_mod_approval:
                            PENDING_ACTIONS[clean_target] = {"yt_id": yt_user_id, "strikes": user_data["strikes"], "msg_id": message_id, "streamer_id": effective_id}
                            await self.send_message(f"⚠️ [AI WARNING] @{username} flagged. Mods: type '!punish @{username}' or '!ignore @{username}'", live_chat_id)
                        else:
                            await self.delete_message(message_id)
                            if user_data["strikes"] == 1: await self.send_message(f"⚠️ @{username}, warning for inappropriate behavior.", live_chat_id)
                            elif user_data["strikes"] >= 2:
                                await self.send_message(f"⏱️ @{username} timed out by AI.", live_chat_id)
                                await self.timeout_user(live_chat_id, yt_user_id, 300)
                        return

            # 3. REWARDS & ECONOMY SETUP
            processing_stage = "viewer_persistence"
            user = db.query(User).filter(User.youtube_id == yt_user_id).first()
            if not user:
                user = User(channel_id=yt_user_id, youtube_id=yt_user_id, youtube_user_id=yt_user_id, username=username)
                db.add(user)
                db.flush() 
                db.add(XP(user_id=user.id, streamer_id=effective_id, current_xp=10, level=1, total_messages=1))
                db.add(Coin(user_id=user.id, balance=50, lifetime_earned=50))
                db.add(DiscordLink(user_id=user.id, sync_code=f"GODDESS-{secrets.token_hex(2).upper()}"))
            else:
                user.last_seen = datetime.now(timezone.utc)
                xp_profile = db.query(XP).filter(XP.user_id == user.id, XP.streamer_id == effective_id).first()
                if not xp_profile:
                    xp_profile = XP(user_id=user.id, streamer_id=effective_id, current_xp=0, level=1, total_messages=0)
                    db.add(xp_profile)
                
                xp_profile.current_xp += 15 
                if user.coins:
                    user.coins[0].balance += 5  
                new_level = self.calculate_level_up(xp_profile.current_xp, xp_profile.level)
                if new_level > xp_profile.level: xp_profile.level = new_level

            # 4. CUSTOM COMMANDS & GAMES
            parts = command_text.split()
            cmd = parts[0] if parts else ""

            if cmd == "!stats":
                xp_prof = db.query(XP).filter(XP.user_id == user.id, XP.streamer_id == effective_id).first()
                await self.send_message(f"📊 @{username} | Level: {xp_prof.level} | Coins: 🪙 {user.coins[0].balance}", live_chat_id)

            elif cmd in ["!flip", "!dice", "!spin"]:
                if len(parts) < 2 or not parts[1].isdigit():
                    await self.send_message(f"❌ @{username}, specify an amount! (e.g., {cmd} 10)", live_chat_id)
                else:
                    bet = int(parts[1])
                    if bet <= 0 or user.coins[0].balance < bet:
                        await self.send_message(f"❌ @{username}, you don't have enough coins!", live_chat_id)
                    else:
                        user.coins[0].balance -= bet 
                        
                        if cmd == "!flip":
                            choice = parts[2] if len(parts)>2 and parts[2] in ["heads", "tails"] else "heads"
                            result = random.choice(["heads", "tails"])
                            if choice == result:
                                win = bet * 2
                                user.coins[0].balance += win
                                await self.send_message(f"🪙 Flipped {result}! @{username} wins 🪙 {win}!", live_chat_id)
                            else:
                                await self.send_message(f"🪙 Flipped {result}... @{username} lost 🪙 {bet}.", live_chat_id)
                                
                        elif cmd == "!dice":
                            roll = random.randint(1, 6)
                            if roll in [4, 5]:
                                win = int(bet * 1.5)
                                user.coins[0].balance += win
                                await self.send_message(f"🎲 Rolled a {roll}! @{username} wins 🪙 {win}!", live_chat_id)
                            elif roll == 6:
                                win = bet * 3
                                user.coins[0].balance += win
                                await self.send_message(f"🎲 CRITICAL ROLL 6! @{username} wins 🪙 {win}!", live_chat_id)
                            else:
                                await self.send_message(f"🎲 Rolled a {roll}... @{username} lost 🪙 {bet}.", live_chat_id)
                                
                        elif cmd == "!spin":
                            multiplier = random.choice([0, 0, 0.5, 1.2, 2, 3])
                            win = int(bet * multiplier)
                            user.coins[0].balance += win
                            if multiplier == 0:
                                await self.send_message(f"🎡 Landed on 0x... @{username} lost everything.", live_chat_id)
                            else:
                                await self.send_message(f"🎡 Landed on {multiplier}x! @{username} wins 🪙 {win}!", live_chat_id)

            elif cmd == "!join":
                # Check if they are already in the queue
                existing = db.query(WaitingListEntry).filter(
                    WaitingListEntry.streamer_id == effective_id,
                    WaitingListEntry.user_id == user.id
                ).first()
                
                if existing:
                    await self.send_message(f"⚠️ @{username}, you are already in the queue!", live_chat_id)
                else:
                    db.add(WaitingListEntry(streamer_id=effective_id, user_id=user.id))
                    db.commit()
                    
                    # Calculate their position in line
                    position = db.query(WaitingListEntry).filter(WaitingListEntry.streamer_id == effective_id).count()
                    await self.send_message(f"✅ @{username} joined the 1v1 Arena Queue! You are #{position} in line.", live_chat_id)

            elif cmd == "!joinbr":
                game = self.br_games.get(live_chat_id)
                if game and game['state'] == 'waiting':
                    if yt_user_id not in game['players']:
                        game['players'][yt_user_id] = {"name": username, "lives": 1}
                        await self.send_message(f"✅ @{username} dropped into the Battle Royale!", live_chat_id)
                        
            elif command_text == "!claim airdrop":
                game = self.br_games.get(live_chat_id)
                if game and game['state'] == 'running' and game.get('airdrop'):
                    p = game['players'].get(yt_user_id)
                    if p and p['lives'] > 0:
                        p['lives'] += 1
                        game['airdrop'] = False
                        await self.send_message(f"🪂 @{username} claimed the Airdrop! You now have {p['lives']} lives!", live_chat_id)

            else:
                cmd_match = db.query(CustomCommand).filter(CustomCommand.streamer_id == effective_id, CustomCommand.command_trigger == command_text).first()
                if cmd_match:
                    await self.send_message(cmd_match.response_text, live_chat_id)

            db.add(ChatLog(streamer_id=actual_id, user_id=user.id, message=message_text))
            db.commit()
            print(f"✅ [CHAT CAUGHT] {username}: {message_text}")

        except Exception as e:
            if not is_guest: db.rollback()
            print(f"🚨 [PROCESS MSG ERROR] stage={processing_stage} type={type(e).__name__}: {e}")
        finally:
            if not is_guest: db.close()

    # ---------------------------------------------------------
    # 🚨 SMART SOS ERROR HANDLER
    # ---------------------------------------------------------
    async def _send_sos_notice(self, live_chat_id: str, actual_id: int, error_msg: str):
        db = SessionLocal()
        try:
            streamer = db.query(Streamer).filter(Streamer.id == actual_id).first()
            if not streamer: return

            # Calculate 10 minutes ago
            ten_mins_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
            
            # Check if developer @uk_hi_kahda is actively in the database and seen recently
            dev_user = db.query(User).filter(User.username.ilike('%uk_hi_kahda%')).first()
            
            tag_name = f"@{streamer.channel_name}" # Default to Streamer
            if dev_user and dev_user.last_seen and dev_user.last_seen >= ten_mins_ago:
                tag_name = "@uk_hi_kahda" # Dev is present! Tag them instead.

            # Clean and truncate the error message so YouTube doesn't block it for being too long
            clean_err = str(error_msg).replace('\n', ' ')[:40]
            
            # The custom Hindi SOS message
            sos_text = f"{tag_name} dikkat ho gyi hai malik aga yha ho to shi kro {clean_err} yha dikkat hui ha"
            
            # Use raw yt_api_manager to send to avoid looping exceptions
            await yt_api_manager.send_chat_message(live_chat_id, sos_text)
            print(f"🚨 [SOS DEPLOYED] Pinged {tag_name} in chat for assistance.")
            
        except Exception as e:
            print(f"[SOS FAILED] Could not send error message to chat: {e}")
        finally:
            db.close()

    # ---------------------------------------------------------
    # MULTI-TENANT ENGINE & LOOP
    # ---------------------------------------------------------
    async def get_chat_from_video(self, video_id: str):
        return await yt_api_manager.get_chat_from_video(video_id)

    async def run(self):
        print("[YOUTUBE DETECTOR] Event-Driven Scalable Engine Online...")
        global DETECTED_VIDEOS, DISCONNECT_QUEUE
        
        while True:
            db = SessionLocal()
            try:
                disconnects = list(DISCONNECT_QUEUE)
                DISCONNECT_QUEUE.clear()
                for vid in disconnects:
                    if vid in self.active_streams: del self.active_streams[vid]

                videos_to_check = dict(DETECTED_VIDEOS)
                DETECTED_VIDEOS.clear() 
                
                for video_id, target_streamer_id in videos_to_check.items():
                    channel_id, chat_id = await self.get_chat_from_video(video_id)
                    
                    if channel_id and chat_id:
                        if target_streamer_id: 
                            streamer = db.query(Streamer).filter(Streamer.id == target_streamer_id).first()
                        else: 
                            streamer = db.query(Streamer).filter(Streamer.youtube_channel_id == channel_id, Streamer.is_active == True).first()
                        
                        is_guest = streamer is None
                        
                        if video_id not in self.active_streams:
                            self.active_streams[video_id] = {
                                "chat_id": chat_id, 
                                "actual_id": streamer.id if streamer else None,
                                "effective_id": streamer.effective_id if streamer else None,
                                "is_guest": is_guest,
                                "next_page_token": None,
                                "error_count": 0,
                                "last_sos": 0.0 # Prevent spamming SOS messages
                            }
                            
                            print(f"[LIVE] {'🟡 GUEST' if is_guest else '🟢 PREMIUM'} Connected to video: {video_id} | Chat ID: {chat_id}")
                            if is_guest: await self.send_message("👋 Hello! Goddess AI (Guest Mode) has successfully connected to the chat!", chat_id)
                            else: await self.send_message("🤖 mod hajir hai janab uk malik ki kami nhi hone dega 😁😸 (Mods type !checkup)", chat_id)
                

                async def fetch_and_process(vid_id):
                    db_session = SessionLocal()
                    try:
                        stream_data = self.active_streams[vid_id]
                        chat_id = stream_data["chat_id"]
                        token = stream_data["next_page_token"]
                        is_guest = stream_data["is_guest"]
                        actual_id = stream_data["actual_id"]
                        effective_id = stream_data["effective_id"]

                        # Pull dynamically learned AI rules into memory
                        if effective_id:
                            self.load_learned_rules_for_streamer(effective_id, db_session)

                        sys_state = db_session.query(SystemState).first()
                        if sys_state and sys_state.youtube_api_calls >= sys_state.youtube_api_cap: return

                        # Scalable API call via Central YouTube API Manager
                        response = await yt_api_manager.get_live_chat_messages(chat_id, token)

                        if sys_state:
                            sys_state.youtube_api_calls += 1
                            db_session.commit()

                        self.active_streams[vid_id]["next_page_token"] = response.get("nextPageToken")
                        self.active_streams[vid_id]["error_count"] = 0
                        
                        items = response.get("items", [])

                        for item in items:
                            snippet = item["snippet"]
                            event_type = snippet["type"]

                            if event_type == "textMessageEvent":
                                await self.process_message(
                                    item["authorDetails"]["channelId"], 
                                    item["authorDetails"]["displayName"], 
                                    snippet["textMessageDetails"]["messageText"], 
                                    item["id"], actual_id, effective_id, chat_id, 
                                    item["authorDetails"].get("isChatModerator", False) or item["authorDetails"].get("isChatOwner", False),
                                    is_guest, item["authorDetails"].get("isChatOwner", False)
                                )
                            elif event_type in ["superChatEvent", "superStickerEvent", "newSponsorEvent", "membershipGiftingEvent", "memberMilestoneChatEvent"]:
                                await self.handle_support_event(event_type, snippet, item["authorDetails"]["displayName"], item["authorDetails"]["channelId"], actual_id, effective_id, chat_id, is_guest)

                    except Exception as e:
                        if vid_id in self.active_streams:
                            err_count = self.active_streams[vid_id].get("error_count", 0) + 1
                            self.active_streams[vid_id]["error_count"] = err_count
                            print(f"⚠️ [YOUTUBE CHAT GLITCH] Video {vid_id} error ({err_count}/5): {e}")
                            
                            # 🚨 FIRE SOS NOTICE ON THE 3RD CONSECUTIVE STRIKE
                            # (We use strike 3 so we don't spam chat for 1-second network blips)
                            now = time.time()
                            if err_count == 3 and (now - self.active_streams[vid_id]["last_sos"] > 300):
                                self.active_streams[vid_id]["last_sos"] = now
                                await self._send_sos_notice(chat_id, actual_id, str(e))
                            
                            # 🚨 THE FIX: AUTO POWER-OFF ON 5 ERRORS
                            if err_count >= 5:
                                print(f"🚨 [STREAM ENDED] Severing video {vid_id}. Powering down bot to 0% quota mode.")
                                if actual_id:
                                    try:
                                        st_record = db_session.query(Streamer).filter(Streamer.id == actual_id).first()
                                        if st_record:
                                            st_record.is_active = False
                                            db_session.commit()
                                    except Exception as db_err:
                                        db_session.rollback()
                                del self.active_streams[vid_id]
                    finally:
                        db_session.close()

                tasks = [fetch_and_process(vid_id) for vid_id in list(self.active_streams.keys())]
                if tasks:
                    await asyncio.gather(*tasks)
            except Exception as e: 
                print(f"🚨 [MAIN LOOP EXCEPTION] {e}")
            finally: 
                db.close()
            
            await asyncio.sleep(8)
