import os
import random
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from app.utils.config import Config

class YouTubeChatMonitor:
    def __init__(self):
        # 1. Build the secure "Write Access" credentials using the new Railway tokens
        self.credentials = Credentials(
            token=None,
            refresh_token=Config.YOUTUBE_REFRESH_TOKEN,
            client_id=Config.YOUTUBE_CLIENT_ID,
            client_secret=Config.YOUTUBE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token"
        )
        
        # 2. Connect to YouTube with full read/write chat permissions
        self.youtube = build('youtube', 'v3', credentials=self.credentials)
        self.live_chat_id = None # Fetched automatically when the stream starts

    async def send_message(self, text):
        """Physically types a message into the YouTube live chat."""
        if not self.live_chat_id:
            return
        
        try:
            request = self.youtube.liveChatMessages().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": self.live_chat_id,
                        "type": "textMessageEvent",
                        "textMessageDetails": {
                            "messageText": text
                        }
                    }
                }
            )
            request.execute()
            print(f"[BOT SENT]: {text}")
        except Exception as e:
            print(f"[YOUTUBE SEND ERROR]: {e}")

    async def process_message(self, user_name, message):
        """Runs every time a viewer types in chat."""
        # [Your existing database/XP logic remains running here in the background]
        
        # --- THE 10% CO-HOST FEATURE ---
        # Roll a digital dice between 1 and 100
        dice_roll = random.randint(1, 100)
        
        if dice_roll <= 10:
            print(f"[CO-HOST] 10% chance hit! Replying to {user_name}...")
            
            # Formulate the prompt for Gemini with custom stream vibes
            prompt = (
                f"You are the witty, helpful co-host for a BGMI gaming streamer named Goddess Live. "
                f"A viewer named {user_name} just said: '{message}'. "
                f"Generate a fun, short reply (under 100 characters). You can occasionally drop subtle flexes about clean AR recoil control or playing with no gyroscope if it fits the conversation."
            )
            
            try:
                # Call your AI brain to generate the response (adjust self.ai_brain to match your setup)
                reply_text = await self.ai_brain.generate_giveaway_reminder(prompt)
                
                # Send the generated message directly to YouTube!
                await self.send_message(reply_text)
            except Exception as e:
                print(f"[AI GENERATION ERROR]: {e}")