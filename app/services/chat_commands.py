"""Bounded, stream-scoped command handling for YouTube Live Chat.

This module intentionally uses the application's existing SQLAlchemy models.
It does not accept executable input; chat text is only persisted/displayed data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    AuditLog, ChatCommandExecution, Coin, CustomCommand, RewardItem,
    StoreRedemption, User, WaitingListEntry, XP,
)

MAX_COMMAND_NAME = 32
MAX_RESPONSE = 350
MAX_STORE_ITEMS = 3
MAX_QUEUE_SIZE = 100
# The existing scheduler persists and scans repetition intervals in minutes.
MIN_REPEAT_MINUTES = 1
MAX_REPEAT_MINUTES = 1440
PROTECTED_COMMANDS = {
    "!adduk", "!deluk", "!edituk", "!reptuk", "!join", "!next1v1",
    "!coins", "!rank", "!store", "!buy", "!addst", "!delst", "!editst",
    "!chps",
}
MUTATING_COMMANDS = {
    "!adduk", "!deluk", "!edituk", "!reptuk", "!join", "!next1v1",
    "!buy", "!addst", "!delst", "!editst",
}
COMMAND_RE = re.compile(r"^![a-z0-9][a-z0-9_-]{0,30}$")


@dataclass(frozen=True)
class ChatActor:
    youtube_id: str
    username: str
    is_moderator: bool
    is_owner: bool

    @property
    def role(self) -> str:
        if self.is_owner:
            return "owner"
        if self.is_moderator:
            return "moderator"
        return "viewer"


class ChatCommandService:
    def __init__(self, db: Session, streamer_id: int, message_id: str, actor: ChatActor):
        self.db, self.streamer_id, self.message_id, self.actor = db, streamer_id, message_id, actor

    def execute(self, raw_message: str) -> Optional[str]:
        raw_message = (raw_message or "").strip()
        if not raw_message.startswith("!"):
            return None
        command, _, argument_text = raw_message.partition(" ")
        command = command.lower()
        if command not in PROTECTED_COMMANDS:
            return None

        if command in MUTATING_COMMANDS and self._already_executed(command):
            return None
        try:
            response = self._dispatch(command, argument_text.strip())
            if response is not None:
                self.db.commit()
            elif command in MUTATING_COMMANDS:
                # The moderation executor lives in YouTubeChatMonitor because it
                # owns the current-stream message buffer.  Keep its idempotency
                # row in the same transaction, so a successful legacy action's
                # commit also records this message id.
                self.db.flush()
            return response
        except (ValueError, IntegrityError):
            self.db.rollback()
            return "❌ That command could not be completed safely."

    def _dispatch(self, command: str, args: str) -> Optional[str]:
        if command in {"!adduk", "!deluk", "!edituk", "!reptuk", "!addst", "!delst", "!editst"}:
            if not self.actor.is_owner:
                return "❌ Owner permission required."
        elif command == "!next1v1" and not self.actor.is_moderator:
            return "❌ Moderator permission required."

        if command == "!adduk": return self._add_command(args)
        if command == "!deluk": return self._delete_command(args)
        if command == "!edituk": return self._edit_command(args)
        if command == "!reptuk": return self._repeat_command(args)
        if command == "!join": return self._join()
        if command == "!next1v1": return self._next()
        if command == "!coins": return self._coins()
        if command == "!rank": return self._rank()
        if command == "!store": return self._store()
        if command == "!buy": return self._buy(args)
        if command == "!addst": return self._add_store(args)
        if command == "!delst": return self._delete_store(args)
        if command == "!editst": return self._edit_store(args)
        if command == "!chps": return "ℹ️ Channel-points rewards are not configured for this stream."
        # Moderation actions remain in YouTubeChatMonitor because they require its
        # bounded recent-message buffer and the existing YouTube moderation client.
        return None

    def _user(self) -> User:
        user = self.db.query(User).filter(User.youtube_id == self.actor.youtube_id).first()
        if not user:
            user = User(youtube_id=self.actor.youtube_id, username=self.actor.username)
            self.db.add(user); self.db.flush()
            self.db.add(XP(user_id=user.id, streamer_id=self.streamer_id, current_xp=0, level=1, total_messages=0))
            self.db.add(Coin(user_id=user.id, streamer_id=self.streamer_id, balance=0, lifetime_earned=0))
        return user

    def _coin(self, user: User) -> Coin:
        coin = self.db.query(Coin).filter(Coin.user_id == user.id, Coin.streamer_id == self.streamer_id).first()
        if not coin:
            coin = Coin(user_id=user.id, streamer_id=self.streamer_id, balance=0, lifetime_earned=0)
            self.db.add(coin); self.db.flush()
        return coin

    def _audit(self, action: str, details: str = "") -> None:
        user = self._user()
        self.db.add(AuditLog(streamer_id=self.streamer_id, user_id=user.id, action=action, details=details[:500]))

    def _already_executed(self, command: str) -> bool:
        existing = self.db.query(ChatCommandExecution).filter(
            ChatCommandExecution.streamer_id == self.streamer_id,
            ChatCommandExecution.message_id == self.message_id,
        ).first()
        if existing:
            return True
        self.db.add(ChatCommandExecution(streamer_id=self.streamer_id, message_id=self.message_id, command=command))
        return False

    @staticmethod
    def _command_name(value: str) -> str:
        value = value.strip().lower()
        if not value.startswith("!"): value = "!" + value
        if not COMMAND_RE.fullmatch(value) or value in PROTECTED_COMMANDS:
            raise ValueError("invalid command name")
        return value

    def _add_command(self, args: str) -> str:
        trigger, sep, response = args.partition(" ")
        trigger = self._command_name(trigger)
        if not sep or not response.strip() or len(response.strip()) > MAX_RESPONSE:
            raise ValueError("invalid response")
        if self.db.query(CustomCommand).filter_by(streamer_id=self.streamer_id, command_trigger=trigger).first():
            return f"❌ {trigger} already exists; use !edituk."
        self.db.add(CustomCommand(streamer_id=self.streamer_id, command_trigger=trigger, response_text=response.strip()))
        self._audit("COMMAND_CREATED", trigger)
        return f"✅ {trigger} created."

    def _delete_command(self, args: str) -> str:
        trigger = self._command_name(args.split()[0] if args else "")
        item = self.db.query(CustomCommand).filter_by(streamer_id=self.streamer_id, command_trigger=trigger).first()
        if not item: return f"❌ {trigger} was not found."
        self.db.delete(item); self._audit("COMMAND_DELETED", trigger)
        return f"✅ {trigger} deleted."

    def _edit_command(self, args: str) -> str:
        trigger, sep, response = args.partition("|")
        if not sep: trigger, sep, response = args.partition(" ")
        trigger = self._command_name(trigger)
        if not sep or not response.strip() or len(response.strip()) > MAX_RESPONSE: raise ValueError("invalid response")
        item = self.db.query(CustomCommand).filter_by(streamer_id=self.streamer_id, command_trigger=trigger).first()
        if not item: return f"❌ {trigger} was not found."
        item.response_text = response.strip(); self._audit("COMMAND_UPDATED", trigger)
        return f"✅ {trigger} updated."

    def _repeat_command(self, args: str) -> str:
        parts = args.split()
        if len(parts) != 2: raise ValueError("invalid repeat syntax")
        trigger = self._command_name(parts[0])
        item = self.db.query(CustomCommand).filter_by(streamer_id=self.streamer_id, command_trigger=trigger).first()
        if not item: return f"❌ {trigger} was not found."
        if parts[1].lower() == "off":
            item.interval_minutes = 0; item.is_active = True; self._audit("COMMAND_REPEAT_DISABLED", trigger)
            return f"✅ Repetition disabled for {trigger}."
        minutes = int(parts[1])
        if not MIN_REPEAT_MINUTES <= minutes <= MAX_REPEAT_MINUTES: raise ValueError("repeat out of range")
        item.interval_minutes = minutes; item.is_active = True
        self._audit("COMMAND_REPEAT_ENABLED", f"{trigger}:{minutes}m")
        return f"✅ {trigger} repeats every {minutes} minute(s)."

    def _join(self) -> str:
        user = self._user()
        if self.db.query(WaitingListEntry).filter_by(streamer_id=self.streamer_id, user_id=user.id).first():
            return f"⚠️ @{self.actor.username}, you are already queued."
        count = self.db.query(WaitingListEntry).filter_by(streamer_id=self.streamer_id).count()
        if count >= MAX_QUEUE_SIZE: return "❌ The 1v1 queue is full."
        self.db.add(WaitingListEntry(streamer_id=self.streamer_id, user_id=user.id)); self._audit("1V1_JOIN")
        return f"✅ @{self.actor.username} joined the 1v1 queue at #{count + 1}."

    def _next(self) -> str:
        entry = self.db.query(WaitingListEntry).filter_by(streamer_id=self.streamer_id).order_by(WaitingListEntry.joined_at.asc(), WaitingListEntry.id.asc()).first()
        if not entry: return "❌ The 1v1 queue is empty."
        name = entry.user.username; self.db.delete(entry); self._audit("1V1_NEXT", name)
        return f"⚔️ Up next: @{name}!"

    def _coins(self) -> str:
        return f"🪙 @{self.actor.username}: {self._coin(self._user()).balance} coins."

    def _rank(self) -> str:
        user = self._user()
        xp = self.db.query(XP).filter_by(user_id=user.id, streamer_id=self.streamer_id).first()
        if not xp: return "📊 No rank data yet."
        rank = self.db.query(XP).filter(XP.streamer_id == self.streamer_id, XP.current_xp > xp.current_xp).count() + 1
        return f"📊 @{self.actor.username}: level {xp.level}, {xp.current_xp} XP, rank #{rank}."

    def _store(self) -> str:
        items = self.db.query(RewardItem).filter_by(streamer_id=self.streamer_id, is_active=True).order_by(RewardItem.cost.asc()).limit(MAX_STORE_ITEMS).all()
        if not items: return "🛍️ The store is empty."
        return "🛍️ " + " | ".join(f"{item.name}: {item.cost} 🪙" for item in items)

    def _buy(self, args: str) -> str:
        name = args.strip()
        if not name or len(name) > 80: raise ValueError("invalid item")
        item = self.db.query(RewardItem).filter(RewardItem.streamer_id == self.streamer_id, RewardItem.is_active == True, RewardItem.name.ilike(name)).first()
        if not item: return "❌ That store item is unavailable."
        user, coin = self._user(), self._coin(self._user())
        if item.stock == 0: return "❌ That item is out of stock."
        if coin.balance < item.cost: return "❌ You do not have enough coins."
        coin.balance -= item.cost
        if item.stock > 0: item.stock -= 1
        self.db.add(StoreRedemption(reward_id=item.id, user_id=user.id, streamer_id=self.streamer_id))
        self._audit("STORE_PURCHASE", item.name)
        return f"✅ @{self.actor.username} redeemed {item.name}."

    def _add_store(self, args: str) -> str:
        parts = [part.strip() for part in args.split("|")]
        if len(parts) != 4 or not all(parts) or len(parts[0]) > 80 or len(parts[2]) > 300: raise ValueError("invalid store item")
        name, category, description, cost_text = parts
        cost = int(cost_text)
        if not 0 <= cost <= 1_000_000: raise ValueError("invalid cost")
        if self.db.query(RewardItem).filter(RewardItem.streamer_id == self.streamer_id, RewardItem.name.ilike(name)).first(): return "❌ That store item already exists."
        self.db.add(RewardItem(streamer_id=self.streamer_id, name=name, category=category, description=description, cost=cost))
        self._audit("STORE_ITEM_CREATED", name)
        return f"✅ {name} added to the store."

    def _delete_store(self, args: str) -> str:
        item = self.db.query(RewardItem).filter(RewardItem.streamer_id == self.streamer_id, RewardItem.name.ilike(args.strip())).first()
        if not item: return "❌ That store item was not found."
        item.is_active = False; self._audit("STORE_ITEM_DELETED", item.name)
        return f"✅ {item.name} disabled."

    def _edit_store(self, args: str) -> str:
        parts = [part.strip() for part in args.split("|")]
        if len(parts) != 4: raise ValueError("invalid store edit")
        item = self.db.query(RewardItem).filter(RewardItem.streamer_id == self.streamer_id, RewardItem.name.ilike(parts[0])).first()
        if not item or not all(parts[1:]) or len(parts[2]) > 300: raise ValueError("invalid store edit")
        cost = int(parts[3])
        if not 0 <= cost <= 1_000_000: raise ValueError("invalid cost")
        item.category, item.description, item.cost = parts[1], parts[2], cost
        self._audit("STORE_ITEM_UPDATED", item.name)
        return f"✅ {item.name} updated."
