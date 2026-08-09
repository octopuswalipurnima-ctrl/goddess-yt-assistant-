import random
from google import genai
from google.genai import types
from app.database.connection import SessionLocal
from app.utils.config import Config
from app.database.models import SystemState

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
            "The chat might affectionately call you 'honey' or 'honey bunny'—when they do, reply with a warm, playful, and affectionate tone. "
            "Use standard streaming lingo (e.g., 'cooking', 'clutch', 'choke', 'lobby', 'OP', 'rush'). Keep responses "
            "short, snappy, single-sentence, and perfectly timed. Never sound like a generic text bot."
        )

    async def generate_chat_reaction(self, direct_prompt: list, recent_messages: list) -> str:
        # Format context data for Gemini
        formatted_logs = "\n".join([f"{msg.get('username', 'User')}: {msg.get('text', '')}" for msg in recent_messages])
        
        # Combine the direct prompt and the context logs
        prompt_instruction = direct_prompt[0] if direct_prompt else "Provide a highly engaging, single-sentence reaction."
        
        prompt = (
            f"Review the following recent stream chat logs for context:\n{formatted_logs}\n\n"
            f"INSTRUCTION: {prompt_instruction}\n"
            "Do not include emojis unless appropriate, no quotes, or prefixing text. Just say the direct reaction phrase."
        )

        try:
            db = SessionLocal()
            sys_state = db.query(SystemState).first()
            if sys_state and sys_state.gemini_api_calls >= sys_state.gemini_api_cap:
                db.close()
                return "I'm focusing on the gameplay right now, ask me again in a bit!"

            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=self.base_persona,
                    temperature=0.8,
                    max_output_tokens=60
                )
            )

            if sys_state:
                sys_state.gemini_api_calls += 1
                db.commit()
            db.close()
            
            # 🚨 THE FIX: Safely check if text exists before calling .strip()
            if not response or not hasattr(response, 'text') or not response.text:
                return "I'm focusing on the stream right now, ask me again in a bit!"

            return response.text.strip()

        except Exception as e:
            try: db.close() 
            except: pass
            print(f"Gemini API Error: {e}")
            return "I'm focusing on the stream right now, ask me again in a bit!"

    async def generate_giveaway_reminder(self) -> str:
        """Generates dynamic variations of giveaway announcements so they never repeat."""
        prompt = (
            "Generate a natural, high-energy stream reminder alerting viewers about an upcoming giveaway. "
            "Keep it distinct, conversational, and aligned with Goddess's BGMI community style. "
            "Examples: 'Chat, keep stacking those coins, giveaway announcement coming up soon!' or 'Goddess, don't forget to brief the lobby on the giveaway details!' "
            "Return only the direct announcement sentence."
        )
        try:
            db = SessionLocal()
            sys_state = db.query(SystemState).first()
            if sys_state and sys_state.gemini_api_calls >= sys_state.gemini_api_cap:
                db.close()
                return "Hey chat! Make sure you're active and collecting your stream coins—giveaway details coming up!"

            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=self.base_persona,
                    temperature=0.9,
                    max_output_tokens=60
                )
            )

            if sys_state:
                sys_state.gemini_api_calls += 1
                db.commit()
            db.close()
            
            # 🚨 THE FIX: Safely check if text exists before calling .strip()
            if not response or not hasattr(response, 'text') or not response.text:
                return "Hey chat! Make sure you're active and collecting your stream coins—giveaway details coming up!"

            return response.text.strip()

        except Exception as e:
            try: db.close() 
            except: pass
            print(f"Gemini Reminder Error: {e}")
            return "Hey chat! Make sure you're active and collecting your stream coins—giveaway details coming up!"