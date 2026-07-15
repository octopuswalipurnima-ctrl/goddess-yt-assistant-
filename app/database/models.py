from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime
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

    # Linking the streamer to their channel's data
    xps = relationship("XP", back_populates="streamer")
    coins = relationship("Coin", back_populates="streamer")
    chat_logs = relationship("ChatLog", back_populates="streamer")

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