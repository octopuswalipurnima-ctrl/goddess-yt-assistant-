import json
import logging
from typing import List, Dict, Any

from app.database.connection import SessionLocal
from app.database.models import SystemState
from app.services.gemini.ai_manager import gemini_api_manager
from app.services.common.queue_manager import Priority

logger = logging.getLogger("goddess_stream_manager")

class AIBrain:
    def __init__(self):
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
        
        prompt_instruction = direct_prompt[0] if direct_prompt else "Provide a highly engaging, single-sentence reaction."
        
        prompt = (
            f"Review the following recent stream chat logs for context:\n{formatted_logs}\n\n"
            f"INSTRUCTION: {prompt_instruction}\n"
            "Do not include emojis unless appropriate, no quotes, or prefixing text. Just say the direct reaction phrase."
        )

        db = SessionLocal()
        try:
            response_text = await gemini_api_manager.generate_content(
                prompt=prompt,
                system_instruction=self.base_persona,
                temperature=0.8,
                max_output_tokens=60,
                priority=Priority.LOW
            )

            if response_text:
                sys_state = db.query(SystemState).first()
                if sys_state:
                    sys_state.gemini_api_calls += 1
                    db.commit()
                return response_text
            
            return "AI returned an empty response. Check Railway logs."

        except Exception as e:
            if db: db.rollback()
            logger.error(f"🚨 [AI CHAT REACTION CRASH] Details: {e}")
            return f"AI Error: {str(e)[:30]}"
        finally:
            db.close()

    async def generate_giveaway_reminder(self) -> str:
        """Generates dynamic variations of giveaway announcements so they never repeat."""
        prompt = (
            "Generate a natural, high-energy stream reminder alerting viewers about an upcoming giveaway. "
            "Keep it distinct, conversational, and aligned with Goddess's BGMI community style. "
            "Examples: 'Chat, keep stacking those coins, giveaway announcement coming up soon!' or 'Goddess, don't forget to brief the lobby on the giveaway details!' "
            "Return only the direct announcement sentence."
        )
        db = SessionLocal()
        try:
            sys_state = db.query(SystemState).first()
            if sys_state and sys_state.gemini_api_calls >= sys_state.gemini_api_cap:
                return "Hey chat! Make sure you're active and collecting your stream coins—giveaway details coming up!"

            response_text = await gemini_api_manager.generate_content(
                prompt=prompt,
                system_instruction=self.base_persona,
                temperature=0.9,
                max_output_tokens=60,
                priority=Priority.LOW
            )

            if response_text:
                if sys_state:
                    sys_state.gemini_api_calls += 1
                    db.commit()
                return response_text

            return "Hey chat! Make sure you're active and collecting your stream coins—giveaway details coming up!"

        except Exception as e:
            if db: db.rollback()
            logger.error(f"Gemini Reminder Error: {e}")
            return "Hey chat! Make sure you're active and collecting your stream coins—giveaway details coming up!"
        finally:
            db.close()

    async def analyze_training_transcript(self, input_text: str, input_type: str = "transcript") -> List[Dict[str, Any]]:
        """
        Analyzes video transcripts or written moderation scenarios provided by Devs.
        Extracts structured moderation rules with bulletproof JSON parsing.
        """
        prompt = (
            "Extract moderation rules from this text. Return ONLY a valid JSON array of objects. "
            "Do not include markdown, backticks, or preamble text.\n\n"
            "Schema for each object in the array:\n"
            "{\n"
            "  \"pattern\": \"exact string or regex to block\",\n"
            "  \"rule_type\": \"exact_match\",\n"
            "  \"target_action\": \"Delete\",\n"
            "  \"reason\": \"explanation\",\n"
            "  \"confidence_score\": 1.0\n"
            "}\n\n"
            f"Text to analyze:\n{input_text}"
        )

        try:
            raw_response = await gemini_api_manager.generate_content(
                prompt=prompt,
                system_instruction="You are a strict data extraction AI. Output RAW JSON arrays only.",
                temperature=0.1,  # Lowered temperature so the AI doesn't get creative with formatting
                max_output_tokens=800,
                priority=Priority.HIGH
            )

            if not raw_response:
                return []

            import re
            import json
            
            # 1. Bulletproof Regex to find the JSON array even if Gemini adds chatty text
            match = re.search(r'\[.*\]', raw_response, re.DOTALL)
            
            if match:
                clean_json = match.group(0)
            else:
                # 2. Brutal string cleaning fallback
                clean_json = raw_response.replace("```json", "").replace("```", "").strip()
                
            parsed_rules = json.loads(clean_json)
            return parsed_rules if isinstance(parsed_rules, list) else []

        except Exception as e:
            logger.error(f"[AI TRAINING ERROR] Failed: {e} | Raw Response: {raw_response if 'raw_response' in locals() else 'None'}")
            
            # 3. Ultimate Fallback: If Gemini completely breaks the JSON format, force a manual rule so the dev isn't blocked.
            if "subscribe" in input_text.lower() or "youtube" in input_text.lower():
                return [{
                    "pattern": "subscribe to my channel",
                    "rule_type": "contextual",
                    "target_action": "Delete",
                    "reason": "Fallback auto-generated rule for self-promotion.",
                    "confidence_score": 1.0
                }]
            return []