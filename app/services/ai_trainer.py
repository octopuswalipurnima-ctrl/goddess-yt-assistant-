import logging
from typing import Dict, Any
from sqlalchemy.orm import Session

from app.database.models import AutoLearnedRule, Streamer, User, AuditLog
from app.ai.generator import AIBrain

logger = logging.getLogger("goddess_stream_manager")

# Authorized Dev handles / names
AUTHORIZED_DEVS = {
    "sarthak raj", "sarthakraj", "@sarthakraj", "@sarthak_raj",
    "@uk_hi_kahda", "uk_hi_kahda", "ukhikahda"
}

ai_brain = AIBrain()

class ModerationTrainer:

    @staticmethod
    def is_dev_authorized(username: str) -> bool:
        """Verifies if the requesting user is an authorized Developer."""
        clean_name = username.strip().lower()
        return (
            clean_name in AUTHORIZED_DEVS or
            clean_name.replace(" ", "") in AUTHORIZED_DEVS or
            f"@{clean_name.replace(' ', '')}" in AUTHORIZED_DEVS
        )

    @classmethod
    async def train_from_content(
        cls, 
        db: Session, 
        dev_username: str, 
        streamer_id: int, 
        data_content: str, 
        input_type: str = "written_condition"
    ) -> Dict[str, Any]:
        """
        Processes training input, extracts rules, and hardens the streamer's moderation layer.
        """
        # 1. Check Dev Authorization
        if not cls.is_dev_authorized(dev_username):
            return {"success": False, "error": f"User '{dev_username}' is not an authorized Developer."}

        # 2. Extract rules using Gemini AI
        extracted_rules = await ai_brain.analyze_training_transcript(data_content, input_type)

        if not extracted_rules:
            return {"success": False, "error": "No valid moderation rules could be extracted from the content."}

        rules_added = 0
        added_patterns = []

        # 3. Commit new rules to the database
        for rule_data in extracted_rules:
            pattern = rule_data.get("pattern", "").strip().lower()
            if not pattern:
                continue

            # Prevent duplicate rules
            existing = db.query(AutoLearnedRule).filter(
                AutoLearnedRule.streamer_id == streamer_id,
                AutoLearnedRule.pattern == pattern
            ).first()

            if existing:
                existing.status = "active"
                existing.confidence_score = rule_data.get("confidence_score", 0.9)
            else:
                new_rule = AutoLearnedRule(
                    streamer_id=streamer_id,
                    pattern=pattern,
                    rule_type=rule_data.get("rule_type", "exact_match"),
                    target_action=rule_data.get("target_action", "Delete"),
                    status="active", # Instantly active
                    confidence_score=rule_data.get("confidence_score", 0.9)
                )
                db.add(new_rule)

            rules_added += 1
            added_patterns.append(pattern)

        # Keep rule writes and the audit trail in one transaction. If either
        # fails, the caller receives a failure and no partial training result
        # is committed for a later retry to duplicate.
        streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
        dev_user = db.query(User).filter(User.username.ilike(f"%{dev_username}%")).first()

        if not streamer:
            db.rollback()
            return {"success": False, "error": "The selected stream workspace no longer exists."}

        db.add(AuditLog(
            streamer_id=streamer_id,
            # Website login is optional, so do not invent a viewer primary key
            # from a streamer id when no matching developer User exists.
            user_id=dev_user.id if dev_user else None,
            action="TRAIN_AI_MODERATION",
            details=f"Dev {dev_username} trained {rules_added} rule(s) via {input_type}."
        ))

        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("[AI TRAINER] Failed to persist rules and audit record")
            raise

        logger.info(f"🧠 [AI TRAINER] Dev '{dev_username}' successfully trained {rules_added} rules for Streamer #{streamer_id}.")

        return {
            "success": True,
            "rules_added": rules_added,
            "patterns": added_patterns,
            "streamer_name": streamer.channel_name if streamer else "Channel"
        }

trainer_service = ModerationTrainer()
