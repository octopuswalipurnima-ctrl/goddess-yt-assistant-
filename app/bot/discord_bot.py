import re
import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func
from app.database.connection import SessionLocal
# Added AutoLearnedRule and CostSavingsAnalytics for the CL Engine dashboard
from app.database.models import User, DiscordLink, Streamer, AutoLearnedRule, CostSavingsAnalytics
from app.utils.config import Config
from app.services.discord_events import discord_events

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
        discord_events.configure(bot)
        synced = await bot.tree.sync()
        print(f"[DISCORD BOT] Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"[DISCORD BOT ERROR] Syncing commands: {e}")

# ---------------------------------------------------------
# DYNAMIC LINK SCANNER: Captures Multi-Tenant Webhooks
# ---------------------------------------------------------
@bot.event
async def on_message(message):
    # Prevent self-loop triggers
    if message.author == bot.user:
        return

    # Check database to see if this message belongs to any registered streamer's announcement setup
    db = SessionLocal()
    streamer_match = None
    try:
        streamer_match = db.query(Streamer).filter(
            Streamer.discord_announcement_channel_id == str(message.channel.id)
        ).first()
    finally:
        db.close()

    # If this channel isn't a designated go-live channel for anyone, run standard link parsing or skip bots
    if not streamer_match:
        # Standard user links handling (if you want normal users to share regular clips/videos elsewhere)
        if not message.author.bot:
            yt_regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]{11})"
            match = re.search(yt_regex, message.content)
            if match:
                DETECTED_VIDEOS.add(match.group(1))
        await bot.process_commands(message)
        return

    # 🤖 THIRD-PARTY WEBHOOK & BOT INTERCEPTOR (Sapphire, CouchBot, etc.)
    # Grab the exact text WITHOUT converting to lowercase so we don't break the YouTube ID!
    content_to_check = message.content or ""

    # Pull text layers out of custom styled rich Embed objects sent by Sapphire
    if message.embeds:
        for embed in message.embeds:
            if embed.title:
                content_to_check += f" {embed.title}"
            if embed.description:
                content_to_check += f" {embed.description}"
            if embed.url:
                content_to_check += f" {embed.url}"

    # Target specific streaming links within the target channel
    yt_regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:watch\?v=|live\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})"
    match = re.search(yt_regex, content_to_check)
    
    if match:
        video_id = match.group(1)
        print(f"[PIPELINE SUCCESS] Intercepted Live Notice for {streamer_match.channel_name}! Stream ID logged: '{video_id}'")
        
        # Instantly feeds the engine without quota drain or manual inputs
        DETECTED_VIDEOS.add(video_id)
        
    await bot.process_commands(message)

# ---------------------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------------------

# --- THE DYNAMIC STREAMER SETUP COMMAND (ADMIN ONLY) ---
@bot.tree.command(name="setup", description="Link your Discord server and notification channel to Goddess AI.")
@app_commands.describe(
    sync_code="The unique 6-character connection code from your Goddess AI web dashboard",
    log_channel="Where should I send moderation logs?",
    announce_channel="The channel where Sapphire (or other alert bots) post your Go-Live notifications"
)
async def setup_dashboard(
    interaction: discord.Interaction, 
    sync_code: str, 
    log_channel: discord.TextChannel, 
    announce_channel: discord.TextChannel
):
    # 🔐 FORCE HIGHEST SERVER RANK: Only administrators can map system channels
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ **Access Denied.** You must hold the highest server authority (Administrator) to run this routing setup.", 
            ephemeral=True
        )
        return

    db = SessionLocal()
    try:
        # Cross-reference the unique cross-platform token generated on the web login page
        streamer = db.query(Streamer).filter(Streamer.server_sync_code == sync_code.upper()).first()
        
        if not streamer:
            await interaction.response.send_message(
                "❌ **Invalid Sync Code.** Open the web dashboard, generate a valid token, and try again.",
                ephemeral=True
            )
            return
            
        # Dynamically link the network configuration to the database
        streamer.discord_guild_id = str(interaction.guild_id)
        streamer.discord_log_channel_id = str(log_channel.id)
        streamer.discord_announcement_channel_id = str(announce_channel.id) # Saves dynamic target path
        
        db.commit()
        
        # Confirm success privately to the streamer
        success_msg = (
            f"👑 **Goddess AI Pipeline Established Successfully for {streamer.channel_name}!**\n\n"
            f"**Linked Server ID:** `{interaction.guild_id}`\n"
            f"**AI Mod Logs Channel:** {log_channel.mention}\n"
            f"**Live Scanner Channel:** {announce_channel.mention}\n\n"
            f"The scanner will now intercept Sapphire/third-party live alerts *specifically* for this stream structure."
        )
        await interaction.response.send_message(success_msg, ephemeral=True)
    finally:
        db.close()


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


# --- NEW: AI CONTINUOUS LEARNING ENGINE DASHBOARD COMMAND ---
@bot.tree.command(name="ai_stats", description="View Goddess AI Continuous Learning performance and cost savings.")
async def ai_stats(interaction: discord.Interaction):
    # Restrict to Server Admins to protect financial/system telemetry
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ **Access Denied.** Only Server Administrators can view system metrics.", ephemeral=True)
        return

    db = SessionLocal()
    try:
        # Cross-reference the database to find the Streamer ID tied to this Discord server
        streamer = db.query(Streamer).filter(Streamer.discord_guild_id == str(interaction.guild_id)).first()
        
        if not streamer:
            await interaction.response.send_message("❌ **Not Linked.** Run `/setup` to link this server to your Goddess AI dashboard first.", ephemeral=True)
            return
            
        # Aggregate lifetime API savings
        total_blocks = db.query(func.sum(CostSavingsAnalytics.layer_1_blocks)).filter(
            CostSavingsAnalytics.streamer_id == streamer.id
        ).scalar() or 0
        
        total_tokens = db.query(func.sum(CostSavingsAnalytics.estimated_tokens_saved)).filter(
            CostSavingsAnalytics.streamer_id == streamer.id
        ).scalar() or 0
        
        # Check rule matrix statuses
        active_rules = db.query(AutoLearnedRule).filter(
            AutoLearnedRule.streamer_id == streamer.id, 
            AutoLearnedRule.status == 'active'
        ).count()
        
        proposed_rules = db.query(AutoLearnedRule).filter(
            AutoLearnedRule.streamer_id == streamer.id, 
            AutoLearnedRule.status == 'proposed'
        ).count()

        # Build the visual UI Card
        embed = discord.Embed(
            title="🧠 Goddess AI: Continuous Learning Engine", 
            description="Live metric overview for your multi-layer moderation pipeline.",
            color=discord.Color.brand_green()
        )
        
        embed.add_field(name="Messages Blocked (Layer 1)", value=f"🛡️ **{total_blocks}** bypassed AI", inline=True)
        embed.add_field(name="API Tokens Saved", value=f"🪙 **{total_tokens:,}** tokens", inline=True)
        embed.add_field(name="Active Local Rules", value=f"📜 **{active_rules}** trained rules", inline=True)
        
        if proposed_rules > 0:
            embed.add_field(
                name="⚠️ Action Required", 
                value=f"The AI has proposed **{proposed_rules}** new rules with high confidence. Open the Web Dashboard to review and approve them.",
                inline=False
            )
            
        await interaction.response.send_message(embed=embed)
    finally:
        db.close()


async def start_discord_bot():
    if not Config.DISCORD_BOT_TOKEN:
        print("[DISCORD BOT] Skipping... No token provided.")
        return
    await bot.start(Config.DISCORD_BOT_TOKEN)
