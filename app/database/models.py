from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Date, DateTime, JSON, Float, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database.connection import Base


def _channel_id_from_youtube_id(context):
    """Keep the legacy required channel identity aligned with YouTube identity."""
    return context.get_current_parameters().get("youtube_id")


def _youtube_user_id_from_youtube_id(context):
    """Populate the deployed legacy viewer-identity column from the same ID."""
    return context.get_current_parameters().get("youtube_id")


def _channel_id_from_streamer(context):
    """Keep the required audit channel identity aligned with streamer channel."""
    params = context.get_current_parameters()
    channel_id = params.get("channel_id")
    if channel_id:
        return channel_id
    streamer_id = params.get("streamer_id")
    if streamer_id is not None:
        try:
            from sqlalchemy import text
            res = context.connection.execute(
                text("SELECT youtube_channel_id FROM streamers WHERE id = :sid"),
                {"sid": streamer_id}
            ).scalar()
            if res:
                return res
        except Exception:
            pass
    return None


def _actor_user_id_from_user_id(context):
    """Keep deployed legacy actor_user_id aligned with user_id."""
    params = context.get_current_parameters()
    actor_id = params.get("actor_user_id")
    if actor_id is not None:
        return str(actor_id)
    user_id = params.get("user_id")
    return str(user_id) if user_id is not None else None


def _user_id_from_actor_user_id(context):
    """Keep user_id aligned with legacy actor_user_id."""
    params = context.get_current_parameters()
    user_id = params.get("user_id")
    if user_id is not None:
        return user_id
    actor_id = params.get("actor_user_id")
    if actor_id is not None:
        if isinstance(actor_id, int):
            return actor_id
        if isinstance(actor_id, str) and actor_id.isdigit():
            return int(actor_id)
    return None

# --- The SaaS Streamer Table ---
class Streamer(Base):
    __tablename__ = "streamers"
    
    id = Column(Integer, primary_key=True, index=True)
    youtube_channel_id = Column(String, unique=True, index=True)
    channel_name = Column(String)
    
    # Core SaaS Integrations
    oauth_refresh_token = Column(String, nullable=True)  # Lets the bot act on their behalf
    discord_webhook_url = Column(String, nullable=True)  # Where to send moderation logs
    is_active = Column(Boolean, default=True)
    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    # --- DASHBOARD & DISCORD SETTINGS ---
    ai_cohost_enabled = Column(Boolean, default=True)
    # Kept on the streamer so one channel cannot alter another channel's AI
    # tone.  ``cohost`` preserves the previous production default.
    personality_mode = Column(String, nullable=False, default="cohost")
    persona_enabled = Column(Boolean, nullable=False, default=False)
    giveaway_reminders_enabled = Column(Boolean, default=False)
    
    # Secure Sync Code and Routing IDs
    server_sync_code = Column(String, unique=True, nullable=True)
    discord_guild_id = Column(String, nullable=True)
    discord_log_channel_id = Column(String, nullable=True)
    discord_announcement_channel_id = Column(String, nullable=True)

    # --- ACCOUNT LINKING & SYNC ---
    linked_primary_id = Column(Integer, ForeignKey("streamers.id"), nullable=True)
    sync_settings = Column(Boolean, default=True) # True = Share commands/coins. False = Independent.
    
    @property
    def effective_id(self):
        """Returns the primary ID if linked AND synced, otherwise returns its own ID."""
        if self.linked_primary_id and self.sync_settings:
            return self.linked_primary_id
        return self.id

    # Linking the streamer to their channel's data
    xps = relationship("XP", back_populates="streamer")
    coins = relationship("Coin", back_populates="streamer")
    chat_logs = relationship("ChatLog", back_populates="streamer")
    
    # --- VISUAL BUILDER EXTENSIONS ---
    alert_templates = relationship("AlertTemplate", back_populates="streamer")
    goal_widgets = relationship("GoalWidget", back_populates="streamer")

    # --- AI MODERATION EXTENSIONS ---
    viewer_trusts = relationship("ViewerTrust", back_populates="streamer")
    mod_action_logs = relationship("ModActionLog", back_populates="streamer")
    analytics_metrics = relationship("StreamAnalyticsMetric", back_populates="streamer")

    # --- CREATOR ECONOMY & STORE EXPANSIONS ---
    economy_config = relationship("EconomyConfig", back_populates="streamer", uselist=False)
    reward_items = relationship("RewardItem", back_populates="streamer")
    store_redemptions = relationship("StoreRedemption", back_populates="streamer")
    
    # --- STREAM MEMORY & CLIPPER ---
    viewer_profiles = relationship("ViewerProfile", back_populates="streamer")
    clip_records = relationship("ClipRecord", back_populates="streamer")
    
    # --- PRO SUBSCRIPTION ---
    pro_subscription = relationship("ProSubscription", back_populates="streamer", uselist=False)

    # --- CONTINUOUS LEARNING PIPELINE EXTENSIONS ---
    auto_learned_rules = relationship("AutoLearnedRule", back_populates="streamer")
    cost_savings = relationship("CostSavingsAnalytics", back_populates="streamer")

    # --- CHAT MANAGEMENT EXTENSIONS ---
    custom_commands = relationship("CustomCommand", back_populates="streamer")
    vip_guests = relationship("VIPGuest", back_populates="streamer")
    
    # --- QUEUE MANAGER ---
    waiting_list_entries = relationship("WaitingListEntry", back_populates="streamer")
    
    # --- NEW: TEAM, RBAC & AUDIT EXTENSIONS ---
    team_members = relationship("TeamMember", back_populates="streamer", cascade="all, delete-orphan")
    invites = relationship("TeamInvite", back_populates="streamer", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="streamer", cascade="all, delete-orphan")


# --- The Viewer Table (Global Identity) ---
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    # `channel_id` and `youtube_user_id` are legacy, required YouTube viewer
    # identities in deployed schemas. `youtube_id` is the newer name used by
    # the bot. Store the same real YouTube identifier in all three.
    channel_id = Column(String, unique=True, index=True, nullable=False, default=_channel_id_from_youtube_id)
    youtube_id = Column(String, unique=True, index=True)
    youtube_user_id = Column(String, unique=True, index=True, nullable=False, default=_youtube_user_id_from_youtube_id)
    username = Column(String)
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    xps = relationship("XP", back_populates="user")
    coins = relationship("Coin", back_populates="user")
    chat_logs = relationship("ChatLog", back_populates="user")
    discord_links = relationship("DiscordLink", back_populates="user")
    
    # --- AI MODERATION EXTENSIONS ---
    viewer_trusts = relationship("ViewerTrust", back_populates="user")

    # --- STORE & MEMORY EXPANSIONS ---
    store_redemptions = relationship("StoreRedemption", back_populates="user")
    viewer_profiles = relationship("ViewerProfile", back_populates="user")
    
    # --- QUEUE MANAGER ---
    waiting_list_entries = relationship("WaitingListEntry", back_populates="user")
    
    # --- NEW: TEAM MEMBERSHIPS ---
    team_memberships = relationship("TeamMember", back_populates="user", cascade="all, delete-orphan")


# =========================================================================
# --- NEW LAYER: TEAM, RBAC & AUDIT MODELS ---
# =========================================================================

class TeamMember(Base):
    __tablename__ = "team_members"

    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    # Roles: 'manager', 'moderator', 'editor'
    role = Column(String, nullable=False, default="moderator")  
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    streamer = relationship("Streamer", back_populates="team_members")
    user = relationship("User", back_populates="team_memberships")


class TeamInvite(Base):
    __tablename__ = "team_invites"

    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"), nullable=False)
    invite_code = Column(String, unique=True, index=True, nullable=False)
    role = Column(String, nullable=False, default="moderator")
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_used = Column(Boolean, default=False)

    streamer = relationship("Streamer", back_populates="invites")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"), nullable=False)
    channel_id = Column(String, index=True, nullable=False, default=_channel_id_from_streamer)
    # Direct dashboard mode has no website user principal.  Operational events
    # remain stream-scoped and auditable without fabricating a viewer record.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, default=_user_id_from_actor_user_id)
    actor_user_id = Column(String, nullable=True, default=_actor_user_id_from_user_id)
    action = Column(String, nullable=False)  # e.g., 'TOGGLE_AI_COHOST', 'CREATE_COMMAND'
    details = Column(String, nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    streamer = relationship("Streamer", back_populates="audit_logs")


# --- Channel-Specific Stats (Multi-Tenant) ---
class XP(Base):
    __tablename__ = "xp"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    streamer_id = Column(Integer, ForeignKey("streamers.id")) # Tracks which channel this XP belongs to
    
    level = Column(Integer, default=1)
    current_xp = Column(Integer, default=0)
    total_messages = Column(Integer, default=0)

    user = relationship("User", back_populates="xps")
    streamer = relationship("Streamer", back_populates="xps")


class Coin(Base):
    __tablename__ = "coins"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    streamer_id = Column(Integer, ForeignKey("streamers.id")) # Tracks which channel these coins belong to
    
    balance = Column(Integer, default=0)
    lifetime_earned = Column(Integer, default=0)

    user = relationship("User", back_populates="coins")
    streamer = relationship("Streamer", back_populates="coins")


# --- Channel Logs & Links ---
class ChatLog(Base):
    __tablename__ = "chat_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id")) # Tracks which chat room this was in
    stream_id = Column(String, nullable=True) # YouTube's liveChatId
    user_id = Column(Integer, ForeignKey("users.id"))
    message = Column(String)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="chat_logs")
    streamer = relationship("Streamer", back_populates="chat_logs")


class ChatCommandExecution(Base):
    """Idempotency ledger for mutating YouTube chat commands.

    YouTube can redeliver a live-chat item, so a message id may only be used
    once within its stream scope.
    """
    __tablename__ = "chat_command_executions"
    __table_args__ = (UniqueConstraint("streamer_id", "message_id", name="uq_chat_command_execution_stream_message"),)

    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"), nullable=False, index=True)
    message_id = Column(String, nullable=False, index=True)
    command = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DiscordLink(Base):
    __tablename__ = "discord_links"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    streamer_id = Column(Integer, ForeignKey("streamers.id"), nullable=True)
    sync_code = Column(String, unique=True)
    discord_id = Column(String, nullable=True)

    user = relationship("User", back_populates="discord_links")


# --- Visual Engine & Widget Data ---
class AlertTemplate(Base):
    """Stores the custom layout built in the Visual Editor"""
    __tablename__ = "alert_templates"
    
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    is_active = Column(Boolean, default=True)
    
    # Stores all CSS, positions, animations, and enabled layers as a single JSON object
    config_json = Column(JSON, default={}) 

    streamer = relationship("Streamer", back_populates="alert_templates")


class GoalWidget(Base):
    """Stores active Sub/Member/Dono goals"""
    __tablename__ = "goal_widgets"
    
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    is_active = Column(Boolean, default=True)
    
    goal_type = Column(String, default="subscriber") # subscriber, donation, etc.
    target_amount = Column(Integer, default=100)
    current_amount = Column(Integer, default=0)
    title = Column(String, default="Sub Goal")
    
    # Stores the visual look of the progress bar
    theme_json = Column(JSON, default={})

    streamer = relationship("Streamer", back_populates="goal_widgets")


# --- AI Moderation System Extensions ---

class ViewerTrust(Base):
    """Tracks ongoing trust scores to determine if a user bypasses Gemini AI moderation."""
    __tablename__ = "viewer_trust"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    
    trust_score = Column(Float, default=50.0) # Scale 0.0 to 100.0
    total_messages_approved = Column(Integer, default=0)
    total_offenses = Column(Integer, default=0)
    is_whitelisted = Column(Boolean, default=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
    
    user = relationship("User", back_populates="viewer_trusts")
    streamer = relationship("Streamer", back_populates="viewer_trusts")


class ModActionLog(Base):
    """Logs decisions made by both the Local Rule Engine and Gemini Engine."""
    __tablename__ = "mod_action_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    username = Column(String, index=True)
    message_content = Column(String)
    
    layer_triggered = Column(String) # E.g., 'Layer 1 (Local)' or 'Layer 2 (Gemini AI)'
    classification = Column(String)
    recommended_action = Column(String) # Safe, Warn, Delete, Timeout, Ban
    applied_action = Column(String, default="Pending")
    reason = Column(String)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    streamer = relationship("Streamer", back_populates="mod_action_logs")


class DecisionCache(Base):
    """Stores a cache of Gemini API decisions to eliminate duplicate remote calls for identical text."""
    __tablename__ = "decision_caches"
    
    id = Column(Integer, primary_key=True, index=True)
    message_hash = Column(String(64), unique=True, index=True)
    message_text = Column(String, index=True)
    classification_json = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class StreamAnalyticsMetric(Base):
    """Batched historical snapshot of chat mood and highlights tracked per minute."""
    __tablename__ = "stream_analytics_metrics"
    
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    
    mood_score = Column(JSON) # Format: {"positive": 60, "neutral": 30, "toxic": 10}
    spam_ratio = Column(Float, default=0.0)
    is_highlight = Column(Boolean, default=False)
    
    streamer = relationship("Streamer", back_populates="analytics_metrics")


# --- CREATOR ECONOMY SETTINGS ---
class EconomyConfig(Base):
    """Allows streamers to customize their currency and leveling aesthetics."""
    __tablename__ = "economy_configs"
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"), unique=True)
    
    currency_name = Column(String, default="Coins")
    currency_icon = Column(String, default="🪙")
    xp_name = Column(String, default="XP")
    xp_color = Column(String, default="#a855f7")
    
    xp_gain_rate = Column(Float, default=1.0)
    coin_gain_rate = Column(Float, default=1.0)
    daily_bonus = Column(Integer, default=100)
    
    streamer = relationship("Streamer", back_populates="economy_config")


# --- REDEMPTION STORE ---
class RewardItem(Base):
    """Items available in the creator's redemption store."""
    __tablename__ = "reward_items"
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    
    name = Column(String, index=True)
    category = Column(String, default="Digital") # Digital, Gaming, Physical, etc.
    description = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    
    cost = Column(Integer, default=0)
    xp_requirement = Column(Integer, default=0)
    level_requirement = Column(Integer, default=1)
    
    stock = Column(Integer, default=-1) # -1 for unlimited
    daily_limit = Column(Integer, default=-1)
    
    is_active = Column(Boolean, default=True)
    requires_approval = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    streamer = relationship("Streamer", back_populates="reward_items")
    redemptions = relationship("StoreRedemption", back_populates="reward")


class StoreRedemption(Base):
    """Tracks viewer purchases and fulfillment status."""
    __tablename__ = "store_redemptions"
    id = Column(Integer, primary_key=True, index=True)
    reward_id = Column(Integer, ForeignKey("reward_items.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    
    status = Column(String, default="Pending") # Pending, Approved, Rejected, Refunded
    purchased_at = Column(DateTime(timezone=True), server_default=func.now())
    
    reward = relationship("RewardItem", back_populates="redemptions")
    user = relationship("User", back_populates="store_redemptions")
    streamer = relationship("Streamer", back_populates="store_redemptions")


# --- STREAM MEMORY & VIEWER PROFILES ---
class ViewerProfile(Base):
    """Deep long-term tracking for community members."""
    __tablename__ = "viewer_profiles"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    
    total_watch_time_minutes = Column(Integer, default=0)
    streams_attended = Column(Integer, default=1)
    current_streak = Column(Integer, default=1)
    highest_streak = Column(Integer, default=1)
    
    achievements_json = Column(JSON, default=[]) # E.g. ["First Message", "OG Viewer"]
    notes = Column(String, nullable=True) # Custom creator tags/notes
    last_attended = Column(DateTime(timezone=True), server_default=func.now())
    
    user = relationship("User", back_populates="viewer_profiles")
    streamer = relationship("Streamer", back_populates="viewer_profiles")


# --- LOCAL STREAM CLIPPER ---
class ClipRecord(Base):
    """Metadata for locally generated, non-AI stream clips."""
    __tablename__ = "clip_records"
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    
    title = Column(String, default="Stream Clip")
    file_path = Column(String) # Local or Cloud URI
    duration_seconds = Column(Integer)
    resolution = Column(String) # e.g., "1080p", "720p"
    
    is_favorite = Column(Boolean, default=False)
    trigger_source = Column(String) # "Hotkey", "Dashboard", "Command"
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    streamer = relationship("Streamer", back_populates="clip_records")


# --- SUBSCRIPTION EXPANSION ---
class ProSubscription(Base):
    """Tracks the PRO plan updates."""
    __tablename__ = "pro_subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"), unique=True)
    
    is_active = Column(Boolean, default=False)
    expires_at = Column(DateTime(timezone=True))
    auto_renew = Column(Boolean, default=False)
    
    streamer = relationship("Streamer", back_populates="pro_subscription")


# =========================================================================
# --- ADDITIONAL LAYER: CONTINUOUS LEARNING MODERATION ENGINE TABLES ---
# =========================================================================

class AutoLearnedRule(Base):
    """Stores regex/keyword rules dynamically generated by the learning engine engine."""
    __tablename__ = "auto_learned_rules"
    
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id", ondelete="CASCADE"))
    
    pattern = Column(String, index=True)       # The actual regex pattern string or keyword string
    rule_type = Column(String, default="regex") # "regex" or "exact_match"
    target_action = Column(String)             # E.g., "Timeout", "Delete", "Warn"
    
    # State Engine tracking states: "pending_shadow", "shadowing", "proposed", "active", "rejected"
    status = Column(String, default="active", index=True) 
    
    # Validation & Calibration Optimization Metrics Matrix
    confidence_score = Column(Float, default=0.9)
    shadow_hits = Column(Integer, default=0)
    false_positives = Column(Integer, default=0)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    streamer = relationship("Streamer", back_populates="auto_learned_rules")


class CostSavingsAnalytics(Base):
    """Aggregates local layer deterministic bypass hits to render the dashboard ROI metric charts."""
    __tablename__ = "cost_savings_analytics"
    
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    date = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    
    layer_1_blocks = Column(Integer, default=0)       # Direct interceptions processed safely without Gemini hit
    estimated_tokens_saved = Column(Integer, default=0) # Structural count of estimated API payload size saved
    
    streamer = relationship("Streamer", back_populates="cost_savings")

# =========================================================================
# --- CHAT MANAGEMENT EXTENSIONS ---
# =========================================================================

class VIPGuest(Base):
    """Stores custom greetings for specific users when they enter chat."""
    __tablename__ = "vip_guests"
    
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    
    target_username = Column(String, index=True) # e.g., "@uk_hi_kahda"
    custom_reply = Column(String)
    
    # This ensures the bot only greets them once per stream, not every single message
    has_been_greeted = Column(Boolean, default=False) 
    
    streamer = relationship("Streamer", back_populates="vip_guests")


class CustomCommand(Base):
    """Nightbot-style custom chat commands and automated repetition loops."""
    __tablename__ = "custom_commands"
    
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    
    command_trigger = Column(String, index=True) # E.g., "!discord"
    response_text = Column(String)               # E.g., "Join our server here: discord.gg/link"
    is_active = Column(Boolean, default=True)
    
    # --- TIMED LOOP EXTENSIONS ---
    interval_minutes = Column(Integer, default=0) # 0 means disabled, > 0 means it repeats
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    
    streamer = relationship("Streamer", back_populates="custom_commands")

# =========================================================================
# --- QUEUE MANAGER EXTENSIONS ---
# =========================================================================

class WaitingListEntry(Base):
    """Manages the 1v1 queue and AFK timers."""
    __tablename__ = "waiting_list"
    
    id = Column(Integer, primary_key=True, index=True)
    streamer_id = Column(Integer, ForeignKey("streamers.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    
    joined_at = Column(DateTime(timezone=True), server_default=func.now())
    
    user = relationship("User", back_populates="waiting_list_entries")
    streamer = relationship("Streamer", back_populates="waiting_list_entries")

class SystemState(Base):
    __tablename__ = "system_state"

    id = Column(Integer, primary_key=True, index=True)
    youtube_api_calls = Column(Integer, default=0)
    # The YouTube usage cap is a daily safety budget, not a lifetime switch.
    # NULL is tolerated for legacy deployments and is initialized by the
    # listener on its next poll.
    youtube_api_window_date = Column(Date, nullable=True)
    gemini_api_calls = Column(Integer, default=0)
    youtube_api_cap = Column(Integer, default=10000)
    gemini_api_cap = Column(Integer, default=1000)
    emergency_stop = Column(Boolean, default=False, nullable=False)
    emergency_reason = Column(String, nullable=True)
