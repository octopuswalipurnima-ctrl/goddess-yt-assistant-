import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.connection import Base
from app.database.models import Coin, RewardItem, Streamer, User
from app.services.chat_commands import ChatActor, ChatCommandService


class ChatCommandServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.stream_a = Streamer(youtube_channel_id="stream-a", channel_name="A")
        self.stream_b = Streamer(youtube_channel_id="stream-b", channel_name="B")
        self.db.add_all([self.stream_a, self.stream_b]); self.db.commit()
        self.owner = ChatActor("owner", "Owner", True, True)
        self.viewer = ChatActor("viewer", "Viewer", False, False)

    def tearDown(self):
        self.db.close(); self.engine.dispose()

    def execute(self, message, actor=None, message_id="m1", stream=None):
        return ChatCommandService(self.db, (stream or self.stream_a).id, message_id, actor or self.owner).execute(message)

    def test_custom_commands_are_owner_or_moderator_and_stream_scoped(self):
        moderator = ChatActor("mod", "Mod", True, False)
        self.assertEqual(self.execute("!adduk !discord Join us"), "✅ !discord created.")
        self.assertIn("Owner permission", self.execute("!edituk !discord | nope", self.viewer, "m2"))
        self.assertIn("not found", self.execute("!deluk !discord", message_id="m3", stream=self.stream_b))
        self.assertEqual(self.execute("!edituk !discord | New text", moderator, "m4"), "✅ !discord updated.")

    def test_moderator_can_manage_existing_repeat_and_store_commands(self):
        moderator = ChatActor("mod", "Mod", True, False)
        self.assertEqual(self.execute("!adduk !rules Be kind", moderator, "mod-command-add"), "✅ !rules created.")
        self.assertEqual(self.execute("!reptuk !rules 5", moderator, "mod-command-repeat"), "✅ !rules repeats every 5 minute(s).")
        self.assertEqual(self.execute("!deluk !rules", moderator, "mod-command-delete"), "✅ !rules deleted.")
        self.assertEqual(self.execute("!addst VIP | Role | Very important player | 50", moderator, "mod-store-add"), "✅ VIP added to the store.")
        self.assertEqual(self.execute("!editst VIP | Role | Updated reward | 75", moderator, "mod-store-edit"), "✅ VIP updated.")
        self.assertEqual(self.execute("!delst VIP", moderator, "mod-store-delete"), "✅ VIP disabled.")

    def test_viewer_cannot_manage_commands_repeat_or_store(self):
        self.assertIn("Owner permission", self.execute("!adduk !rules Be kind", self.viewer, "viewer-command-add"))
        self.assertIn("Owner permission", self.execute("!reptuk !rules 5", self.viewer, "viewer-command-repeat"))
        self.assertIn("Owner permission", self.execute("!addst VIP | Role | Very important player | 50", self.viewer, "viewer-store-add"))

    def test_owner_or_moderator_can_manage_persona_but_viewer_cannot(self):
        moderator = ChatActor("mod", "Mod", True, False)
        self.assertEqual(self.execute("!persona roast", moderator, "persona-roast"), "✅ Roast persona activated.")
        self.db.refresh(self.stream_a)
        self.assertTrue(self.stream_a.persona_enabled)
        self.assertEqual(self.stream_a.personality_mode, "roast")
        self.assertEqual(self.execute("!persona view", moderator, "persona-view"), "ℹ️ Persona: roast.")
        self.assertEqual(self.execute("!persona off", self.owner, "persona-off"), "✅ Persona mode disabled.")
        self.db.refresh(self.stream_a)
        self.assertFalse(self.stream_a.persona_enabled)
        self.assertIn("Moderator permission", self.execute("!persona hype", self.viewer, "persona-viewer"))

    def test_duplicate_mutation_is_not_replayed(self):
        self.assertEqual(self.execute("!adduk !rules Be kind", message_id="yt-1"), "✅ !rules created.")
        self.assertIsNone(self.execute("!adduk !other Should not run", message_id="yt-1"))

    def test_queue_is_fifo_and_prevents_duplicate_join(self):
        other = ChatActor("other", "Other", False, False)
        self.assertIn("#1", self.execute("!join", self.viewer, "q1"))
        self.assertIn("already queued", self.execute("!join", self.viewer, "q2"))
        self.assertIn("#2", self.execute("!join", other, "q3"))
        self.assertEqual(self.execute("!next1v1", message_id="q4"), "⚔️ Up next: @Viewer!")

    def test_purchase_deducts_stream_coin_balance_once(self):
        viewer = User(youtube_id="viewer", username="Viewer")
        self.db.add(viewer); self.db.flush()
        self.db.add(Coin(user_id=viewer.id, streamer_id=self.stream_a.id, balance=100, lifetime_earned=100))
        self.db.add(RewardItem(streamer_id=self.stream_a.id, name="VIP", category="Role", description="VIP", cost=50))
        self.db.commit()
        self.assertIn("redeemed VIP", self.execute("!buy VIP", self.viewer, "buy-1"))
        self.assertIsNone(self.execute("!buy VIP", self.viewer, "buy-1"))
        balance = self.db.query(Coin).filter_by(user_id=viewer.id, streamer_id=self.stream_a.id).one().balance
        self.assertEqual(balance, 50)

    def test_command_user_creation_populates_all_legacy_youtube_identities(self):
        self.assertIn("#1", self.execute("!join", self.viewer, "identity-1"))
        user = self.db.query(User).filter_by(youtube_id="viewer").one()
        self.assertEqual(user.channel_id, "viewer")
        self.assertEqual(user.youtube_user_id, "viewer")

    def test_log_channel_is_moderator_only_persistent_and_idempotent(self):
        moderator = ChatActor("mod", "Mod", True, False)
        channel_id = "123456789012345678"
        self.assertIn("Moderator permission", self.execute(f"!setlogchannel {channel_id}", self.viewer, "log-1"))
        self.assertIn("linked successfully", self.execute(f"!setlogchannel {channel_id}", moderator, "log-2"))
        self.assertEqual(self.stream_a.discord_log_channel_id, channel_id)
        # A new session represents a process/service reinitialization reading
        # the persisted Streamer setting rather than in-memory command state.
        restarted_db = self.Session()
        try:
            response = ChatCommandService(restarted_db, self.stream_a.id, "log-3", moderator).execute("!getlogchannel")
            self.assertIn(channel_id, response)
        finally:
            restarted_db.close()
        self.assertIsNone(self.execute(f"!setlogchannel {channel_id}", moderator, "log-2"))

    def test_log_channel_rejects_invalid_or_extra_arguments(self):
        self.assertIn("could not be completed safely", self.execute("!setlogchannel invalid", self.owner, "invalid-1"))
        self.assertIn("could not be completed safely", self.execute("!setlogchannel 123456789012345678 extra", self.owner, "invalid-2"))


if __name__ == "__main__":
    unittest.main()
