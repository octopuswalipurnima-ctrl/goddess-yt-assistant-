import os
import json
import hashlib
from fastapi import Request
from sqlalchemy.orm import Session
from google import genai
from google.genai import types
from app.database.models import DecisionCache, SystemState

# Initialize modern GenAI SDK
# Make sure to set GEMINI_API_KEY in your Railway deployment environment variables
client = genai.Client()

class GeminiModeratorEngine:
    def __init__(self, db: Session):
        self.db = db
        self.model_name = "gemini-2.5-flash"

    def get_cached_verdict(self, text: str) -> dict:
        """Checks structural cache layer to prevent redundant remote requests."""
        msg_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        cached = self.db.query(DecisionCache).filter(DecisionCache.message_hash == msg_hash).first()
        if cached:
            return cached.classification_json
        return None

    def cache_verdict(self, text: str, decision: dict):
        """Commits analyzed data into structural cache with unique signatures."""
        msg_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        new_cache = DecisionCache(message_hash=msg_hash, message_text=text, classification_json=decision)
        try:
            self.db.add(new_cache)
            self.db.commit()
        except Exception:
            self.db.rollback()

    async def analyze_message(self, text: str, context_history: list) -> dict:
        """
        Processes suspicious or ambiguous text against the context of past messages.
        Returns a strict JSON validation block.
        """
        # 1. Structural Cache Check
        cached_result = self.get_cached_verdict(text)
        if cached_result:
            return cached_result

        # 2. Structured Prompt Formulation
        history_str = "\n".join([f"- {m}" for m in context_history[-10:]])
        
        system_instruction = (
            "You are an expert stream moderation system. Analyze the target message considering "
            "the provided chat context history. Understand Hindi, English, Hinglish, Urdu, sarcasm, "
            "covert bullying, and gaming slang. "
            "You must return ONLY a structured JSON object fitting this schema: "
            '{"classification": string, "severity": "Low"|"Medium"|"High", "confidence": int, '
            '"recommended_action": "Safe"|"Warn"|"Delete"|"Timeout"|"Ban", "reason": string}'
        )

        user_content = (
            f"Chat History Context:\n{history_str}\n\n"
            f"Target Message to Moderate: '{text}'"
        )

        try:
            # API Cap Check
            sys_state = self.db.query(SystemState).first()
            if sys_state and sys_state.gemini_api_calls >= sys_state.gemini_api_cap:
                return {
                    "classification": "CapExceeded",
                    "severity": "Low",
                    "confidence": 100,
                    "recommended_action": "Safe",
                    "reason": "Gemini API cap exceeded. Defaulting to Safe."
                }

            # Modern structured generation call with mandatory JSON Schema response configuration
            response = client.models.generate_content(
                model=self.model_name,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    temperature=0.1, # Keep decisions deterministic and highly reliable
                    max_output_tokens=150
                )
            )
            
            if sys_state:
                sys_state.gemini_api_calls += 1
                self.db.commit()


            decision = json.loads(response.text)
            
            # 3. Update structural cache asynchronously if marked safe or cleanly classifiable
            if decision.get("recommended_action") in ["Safe", "Delete", "Ban"]:
                self.cache_verdict(text, decision)
                
            return decision

        except Exception as e:
            # Resilient fallback mechanics to prevent production pipeline thread blocking
            return {
                "classification": "ErrorFallback",
                "severity": "Low",
                "confidence": 100,
                "recommended_action": "Safe",
                "reason": f"AI Engine execution failure gracefully bypassed: {str(e)}"
            }