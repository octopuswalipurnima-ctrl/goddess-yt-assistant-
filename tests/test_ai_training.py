import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.connection import Base
from app.database.models import AuditLog, AutoLearnedRule, Streamer, User
from app.services.ai_trainer import ModerationTrainer


class ModerationTrainingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.streamer = Streamer(youtube_channel_id="stream-training", channel_name="Training Stream")
        self.developer = User(
            channel_id="UC-developer", youtube_id="UC-developer",
            youtube_user_id="UC-developer", username="Sarthak raj",
        )
        self.db.add_all([self.streamer, self.developer]); self.db.commit()

    def tearDown(self):
        self.db.close(); self.engine.dispose()

    async def test_training_persists_rules_and_stream_scoped_audit_atomically(self):
        extracted = [
            {"pattern": "mild toxicity", "target_action": "Warn"},
            {"pattern": "continued harassment", "target_action": "Timeout"},
        ]
        with patch("app.services.ai_trainer.ai_brain.analyze_training_transcript", new=AsyncMock(return_value=extracted)):
            result = await ModerationTrainer.train_from_content(
                self.db, "Sarthak raj", self.streamer.id, "moderation transcript",
            )
        self.assertTrue(result["success"])
        self.assertEqual(self.db.query(AutoLearnedRule).filter_by(streamer_id=self.streamer.id).count(), 2)
        audit = self.db.query(AuditLog).one()
        self.assertEqual(audit.streamer_id, self.streamer.id)
        self.assertEqual(audit.user_id, self.developer.id)
        self.assertEqual(audit.action, "TRAIN_AI_MODERATION")

    async def test_repeat_training_updates_existing_pattern_without_duplicates(self):
        extracted = [{"pattern": "spam phrase", "target_action": "Delete"}]
        with patch("app.services.ai_trainer.ai_brain.analyze_training_transcript", new=AsyncMock(return_value=extracted)):
            await ModerationTrainer.train_from_content(self.db, "Sarthak raj", self.streamer.id, "rule")
            await ModerationTrainer.train_from_content(self.db, "Sarthak raj", self.streamer.id, "rule")
        self.assertEqual(self.db.query(AutoLearnedRule).filter_by(streamer_id=self.streamer.id, pattern="spam phrase").count(), 1)
        self.assertEqual(self.db.query(AuditLog).filter_by(action="TRAIN_AI_MODERATION").count(), 2)

    async def test_unauthorized_developer_does_not_call_ai_or_write_data(self):
        with patch("app.services.ai_trainer.ai_brain.analyze_training_transcript", new=AsyncMock()) as extract:
            result = await ModerationTrainer.train_from_content(self.db, "not-authorized", self.streamer.id, "rule")
        self.assertFalse(result["success"])
        extract.assert_not_awaited()
        self.assertEqual(self.db.query(AutoLearnedRule).count(), 0)
        self.assertEqual(self.db.query(AuditLog).count(), 0)

