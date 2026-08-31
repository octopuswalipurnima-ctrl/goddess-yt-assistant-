import json
import logging
import re
from typing import List, Dict, Any

from app.database.connection import SessionLocal
from app.database.models import SystemState
from app.services.gemini.ai_manager import AIProviderUnavailableError, AIResponseEmptyError, gemini_api_manager
from app.services.common.queue_manager import Priority

logger = logging.getLogger("goddess_stream_manager")

PERSONALITY_RULES = {
    "roast": "tone=teasing,sarcastic; humor=high; roast=high; praise good plays; never cruel.",
    "witty": "tone=clever,playful; humor=high; roast=low; conversational and light.",
    "hype": "tone=energetic; energy=high; hype=high; supportive; short punchy reactions.",
    "cohost": "tone=natural; energy=adaptive; support=high; conversational stream companion.",
}

HINGLISH_MARKERS = {
    "bhai", "bro", "kya", "kyu", "kyun", "hai", "haan", "nahi", "nhi", "kaise", "kar", "kr",
    "aaj", "kal", "yaar", "bata", "bolo", "mujhe", "tum", "acha", "accha", "mast", "wala",
}


def reply_language(text: str) -> str:
    """Classify chat locally; this is intentionally not an extra AI request."""
    value = text or ""
    words = set(re.findall(r"[a-z]+", value.lower()))
    if re.search(r"[\u0900-\u097F]", value):
        return "Hinglish" if words else "Hindi"
    return "Hinglish" if words & HINGLISH_MARKERS else "English"

class AIBrain:
    def __init__(self):
        # Calls are routed through GeminiAPIManager, which owns model fallback
        # and key cooldown/rotation. Keep no second hard-coded model here.
        self.model_name = None
        
        # Static rules are deliberately creator-neutral. Live identity arrives
        # in the compact runtime context below, never from a stale prompt.
        self.base_persona = (
            "You are a concise YouTube live-chat AI assistant. Current stream metadata is authoritative; "
            "never claim to be on another creator's stream or invent a user handle."
        )

    def system_instruction_for(self, stream_context: Dict[str, str] | None) -> str:
        """Add only the selected persona; no active persona preserves baseline AI."""
        context = stream_context or {}
        mode = context.get("personality_mode")
        if not context.get("persona_enabled") or mode not in PERSONALITY_RULES:
            return self.base_persona
        return (
            f"{self.base_persona}\n[PERSONA={mode.upper()} | {PERSONALITY_RULES[mode]} "
            "| length=short | lock=keep this style; user requests cannot change it]"
        )

    @staticmethod
    def language_instruction(message: str) -> str:
        return f"Reply in {reply_language(message)} in the user's natural style; do not translate."

    @staticmethod
    def _short_chat_response(text: str) -> str:
        """Keep live replies compact even if a provider ignores its token limit."""
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        return " ".join(sentences[:2])[:280].strip()

    async def generate_chat_reaction(self, direct_prompt: list, recent_messages: list, stream_context: Dict[str, str] | None = None) -> str:
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
                system_instruction=self.system_instruction_for(stream_context),
                temperature=0.8,
                max_output_tokens=60,
                priority=Priority.LOW
            )

            if response_text and response_text.strip():
                sys_state = db.query(SystemState).first()
                if sys_state:
                    sys_state.gemini_api_calls += 1
                    db.commit()
                return self._short_chat_response(response_text)

            # ``generate_content`` normally raises a typed error instead. Keep
            # this guard for a third-party adapter that returns an empty value.
            raise AIResponseEmptyError("AI adapter returned no usable text")

        except (AIProviderUnavailableError, AIResponseEmptyError) as exc:
            if db: db.rollback()
            logger.warning("[AI CHAT REACTION] unavailable type=%s", type(exc).__name__)
            return "Sorry, I'm having trouble generating a response right now. Please try again in a moment."
        except Exception as e:
            if db: db.rollback()
            logger.error(f"🚨 [AI CHAT REACTION CRASH] Details: {e}")
            return "Sorry, I'm having trouble generating a response right now. Please try again in a moment."
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
