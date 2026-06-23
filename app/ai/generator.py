import random
from google import genai
from google.genai import types
from app.utils.config import Config

class AIBrain:
    def __init__(self):
        # Initializing the new Google GenAI Client
        self.client = genai.Client(api_key=Config.GEMINI_API_KEY)
        self.model_name = "gemini-2.5-flash"
        
        # Goddess-specific BGMI streamer personality framework
        self.base_persona = (
            "You are an expert AI Stream Co-Host for a popular female BGMI (Battlegrounds Mobile India) "
            "streamer named 'Goddess'. Goddess plays at a highly competitive level without a gyroscope "
            "(No-Gyro player) focusing on master-class lens sensitivity and flawless AR/shotgun recoil control.\n\n"
            "Your personality is hype, gamer-centric, supportive, witty, and deeply loyal to the community. "
            "Use standard streaming lingo (e.g., 'cooking', 'clutch', 'choke', 'lobby', 'OP', 'rush'). Keep responses "
            "short, snappy, single-sentence, and perfectly timed. Never sound like a generic text bot."
        )

    async def generate_chat_reaction(self, chat_context: list, recent_messages: list) -> str:
        """
        Implements the 70/20/10 rule.
        70% of the time: Observe silently (return None).
        20% of the time: React internally but keep quiet unless highly prompted.
        10% of the time: Speak out loud in chat.
        """
        roll = random.randint(1, 100)
        if roll > 15:  # Tweaked slightly to ~15% to keep chat vibrant but clean
            return None

        # Format context data for Gemini
        formatted_logs = "\n".join([f"{msg['username']}: {msg['text']}" for msg in recent_messages])
        
        prompt = (
            f"Review the following recent stream chat logs:\n{formatted_logs}\n\n"
            "Based on the conversation, provide a highly engaging, single-sentence reaction as Goddess's AI co-host. "
            "Comment on the gameplay intensity, the lobby strength, or complement Goddess's non-gyro crosshair placement. "
            "Do not include emojis, quotes, or prefixing text. Just say the direct reaction phrase."
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=self.base_persona,
                    temperature=0.8,
                    max_output_tokens=50
                )
            )
            return response.text.strip()
        except Exception as e:
            print(f"Gemini API Error: {e}")
            return None

    async def generate_giveaway_reminder(self) -> str:
        """Generates dynamic variations of giveaway announcements so they never repeat."""
        prompt = (
            "Generate a natural, high-energy stream reminder alerting viewers about an upcoming giveaway. "
            "Keep it distinct, conversational, and aligned with Goddess's BGMI community style. "
            "Examples: 'Chat, keep stacking those coins, giveaway announcement coming up soon!' or 'Goddess, don't forget to brief the lobby on the giveaway details!' "
            "Return only the direct announcement sentence."
        )
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=self.base_persona,
                    temperature=0.9,
                    max_output_tokens=60
                )
            )
            return response.text.strip()
        except Exception as e:
            print(f"Gemini Reminder Error: {e}")
            return "Hey chat! Make sure you're active and collecting your stream coins—giveaway details coming up!"