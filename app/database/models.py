from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, JSON, Float
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database.connection import Base

# --- NEW: The SaaS Streamer Table ---
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
    giveaway_reminders_enabled = Column(Boolean, default=False)
    
    # Secure Sync Code and Routing IDs
    server_sync_code = Column(String, unique=True, nullable=True)
    discord_guild_id = Column(String, nullable=True)
    discord_log_channel_id = Column(String, nullable=True)
    discord_announcement_channel_id = Column(String, nullable=True)

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


# --- The Viewer Table (Global Identity) ---
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    youtube_id = Column(String, unique=True, index=True)
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
    # Example: {"animation_in": "bounce", "bg_color": "#000", "show_avatar": True}
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


# --- NEW: AI Moderation System Extensions ---

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