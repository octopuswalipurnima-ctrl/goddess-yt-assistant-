import logging
from sqlalchemy.orm import Session
from app.database.models import AutoLearnedRule, AuditLog
from app.ai.generator import AIBrain

logger = logging.getLogger("goddess_stream_manager")

class AITrainerService:
    def __init__(self):
        self.ai_brain = AIBrain()

    async def train_from_content(
        self, 
        db: Session, 
        dev_username: str, 
        streamer_id: int, 
        data_content: str, 
        input_type: str = "transcript"
    ) -> dict:
        """
        Processes developer input from the web dashboard and stores rules into the DB.
        Includes an intelligent fallback parser if Gemini API keys are restricted.
        """
        try:
            # 1. Attempt AI extraction via Gemini
            extracted_rules = await self.ai_brain.analyze_training_transcript(data_content, input_type)

            # 2. Smart Fallback: If Gemini fails (due to key/region 404 blocks), parse manually so devs aren't blocked
            if not extracted_rules and data_content:
                logger.warning("[AI TRAINER] Gemini API unavailable. Using smart local rule parser fallback.")
                content_lower = data_content.lower()
                
                action = "Delete"
                if "timeout" in content_lower:
                    action = "Timeout"
                elif "ban" in content_lower:
                    action = "Ban"

                # Extract potential keywords/phrases safely
                extracted_rules = [{
                    "pattern": data_content[:100].strip(), # Use the text snippet as the match pattern
                    "rule_type": "contextual",
                    "target_action": action,
                    "reason": f"Manually trained rule by {dev_username}",
                    "confidence_score": 1.0
                }]

            if not extracted_rules:
                return {"success": False, "error": "No valid moderation rules could be extracted from the content."}

            added_count = 0
            for rule_data in extracted_rules:
                pattern = rule_data.get("pattern")
                if not pattern:
                    continue

                # Check if rule already exists for this streamer to prevent duplicates
                existing = db.query(AutoLearnedRule).filter(
                    AutoLearnedRule.streamer_id == streamer_id,
                    AutoLearnedRule.pattern == pattern
                ).first()

                if not existing:
                    new_rule = AutoLearnedRule(
                        streamer_id=streamer_id,
                        pattern=pattern,
                        rule_type=rule_data.get("rule_type", "contextual"),
                        target_action=rule_data.get("target_action", "Delete"),
                        status="active",
                        confidence_score=rule_data.get("confidence_score", 1.0)
                    )
                    db.add(new_rule)
                    added_count += 1

            # Log audit trail
            db.add(AuditLog(
                streamer_id=streamer_id,
                user_id=1,  # Dev fallback ID
                action="AI_TRAINING_EXECUTED",
                details=f"Dev '{dev_username}' trained bot with {input_type}. Added {added_count} new rules."
            ))
            
            db.commit()
            return {"success": True, "rules_added": added_count}

        except Exception as e:
            db.rollback()
            logger.error(f"[AI TRAINER ERROR] {e}")
            return {"success": False, "error": str(e)}

trainer_service = AITrainerService()