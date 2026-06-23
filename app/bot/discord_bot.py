import discord
from discord import app_commands
from app.database.connection import SessionLocal
from app.database.models import DiscordLink, User
from app.utils.config import Config

class GoddessDiscordBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Syncs commands globally so they show up as slash options instantly
        await self.tree.sync()

bot_instance = GoddessDiscordBot()

@bot_instance.tree.command(name="link", description="Link your YouTube account with Discord to sync your level and coins.")
@app_commands.describe(code="The unique code generated when you chat on the stream (e.g. GODDESS-XXXX)")
async def link_account(interaction: discord.Interaction, code: str):
    db = SessionLocal()
    try:
        link_record = db.query(DiscordLink).filter(DiscordLink.sync_code == code.strip().upper()).first()
        if not link_record:
            await interaction.response.send_message("❌ Invalid sync code. Check your spelling or chat on stream to update.", ephemeral=True)
            return
        
        if link_record.is_linked:
            await interaction.response.send_message("⚠️ This code has already been successfully claimed.", ephemeral=True)
            return

        # Bind Discord ID to our user profile record
        link_record.discord_id = str(interaction.user.id)
        link_record.is_linked = True
        db.commit()
        
        await interaction.response.send_message(f"✅ Success! Connected your Discord profile to stream user: **{link_record.user.username}**.", ephemeral=True)
    except Exception as e:
        db.rollback()
        await interaction.response.send_message("❌ An error occurred during database alignment.", ephemeral=True)
    finally:
        db.close()

@bot_instance.tree.command(name="profile", description="Check your current Stream Level, XP progression, and Coin balance.")
async def view_profile(interaction: discord.Interaction):
    db = SessionLocal()
    try:
        link_record = db.query(DiscordLink).filter(DiscordLink.discord_id == str(interaction.user.id)).first()
        if not link_record or not link_record.is_linked:
            await interaction.response.send_message("❌ Your account is not linked yet. Use `/link [your-code]` first!", ephemeral=True)
            return

        user = link_record.user
        embed = discord.Embed(title=f"👑 Goddess Squad Profile: {user.username}", color=discord.Color.gold())
        embed.add_field(name="✨ Level", value=str(user.xp.level), inline=True)
        embed.add_field(name="📊 Total XP", value=f"{user.xp.current_xp} XP", inline=True)
        embed.add_field(name="🪙 Coin Balance", value=f"{user.coins.balance} Coins", inline=True)
        embed.set_footer(text="Keep watching Goddess streams to earn more levels and rewards!")
        
        await interaction.response.send_message(embed=embed)
    finally:
        db.close()

async def start_discord_bot():
    try:
        await bot_instance.start(Config.DISCORD_BOT_TOKEN)
    except Exception as e:
        print(f"Discord Bot failed to launch: {e}")