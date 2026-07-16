import re
import discord
from discord.ext import commands
from app.database.connection import SessionLocal
from app.database.models import User, DiscordLink
from app.utils.config import Config

# ---------------------------------------------------------
# IMPORT SHARED MEMORY: Connects Discord to YouTube Engine
# ---------------------------------------------------------
from app.bot.youtube_chat import DETECTED_VIDEOS

# Enable intents (Message Content is REQUIRED for the link scanner!)
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"[DISCORD BOT] Logged in as {bot.user.name} and ready to scan for links!")
    try:
        synced = await bot.tree.sync()
        print(f"[DISCORD BOT] Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"[DISCORD BOT ERROR] Syncing commands: {e}")

# ---------------------------------------------------------
# THE LINK SCANNER: Feeds the YouTube API Quota Saver
# ---------------------------------------------------------
@bot.event
async def on_message(message):
    # Ignore our own bot messages
    if message.author == bot.user:
        return

    # Look for YouTube links (matches both youtube.com/watch?v= and youtu.be/)
    yt_regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]{11})"
    match = re.search(yt_regex, message.content)
    
    if match:
        video_id = match.group(1)
        print(f"[DISCORD BOT] Detected YouTube Link from {message.author.name}! Passing Video ID '{video_id}' to YouTube Engine...")
        
        # Send the Video ID to the shared variable in youtube_chat.py
        DETECTED_VIDEOS.add(video_id)
        
    # VERY IMPORTANT: Without this, slash commands like /link and /profile will break!
    await bot.process_commands(message)

# ---------------------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------------------
@bot.tree.command(name="link", description="Link your YouTube account using your secret code.")
async def link_account(interaction: discord.Interaction, code: str):
    db = SessionLocal()
    try:
        link_record = db.query(DiscordLink).filter(DiscordLink.sync_code == code.upper()).first()
        if not link_record:
            await interaction.response.send_message("❌ Invalid code. Type `!link` in the YouTube chat to get your code.", ephemeral=True)
            return
        
        if link_record.discord_id:
            await interaction.response.send_message("⚠️ This code has already been used.", ephemeral=True)
            return
            
        link_record.discord_id = str(interaction.user.id)
        db.commit()
        await interaction.response.send_message(f"✅ Success! Your YouTube account is now linked to your Discord.", ephemeral=True)
    finally:
        db.close()

@bot.tree.command(name="profile", description="View your global Streamer Loyalty Profile.")
async def view_profile(interaction: discord.Interaction):
    db = SessionLocal()
    try:
        link_record = db.query(DiscordLink).filter(DiscordLink.discord_id == str(interaction.user.id)).first()
        if not link_record:
            await interaction.response.send_message("❌ You haven't linked your account yet. Use `/link` first!", ephemeral=True)
            return

        user = db.query(User).filter(User.id == link_record.user_id).first()
        
        # Calculate Global Platform Stats across all streamers they watch
        total_xp = sum(xp.current_xp for xp in user.xps) if user.xps else 0
        highest_level = max([xp.level for xp in user.xps]) if user.xps else 1
        coin_balance = user.coins[0].balance if user.coins else 0
        
        embed = discord.Embed(title=f"👑 {user.username}'s Profile", color=discord.Color.purple())
        embed.add_field(name="Highest Level", value=f"⭐ {highest_level}", inline=True)
        embed.add_field(name="Global XP", value=f"✨ {total_xp}", inline=True)
        embed.add_field(name="Coins", value=f"🪙 {coin_balance}", inline=True)
        
        await interaction.response.send_message(embed=embed)
    finally:
        db.close()

async def start_discord_bot():
    if not Config.DISCORD_BOT_TOKEN:
        print("[DISCORD BOT] Skipping... No token provided.")
        return
    await bot.start(Config.DISCORD_BOT_TOKEN)