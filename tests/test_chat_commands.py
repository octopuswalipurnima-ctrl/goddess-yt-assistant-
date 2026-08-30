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

    def test_custom_commands_are_owner_only_and_stream_scoped(self):
        self.assertEqual(self.execute("!adduk !discord Join us"), "✅ !discord created.")
        self.assertIn("Owner permission", self.execute("!edituk !discord | nope", self.viewer, "m2"))
        self.assertIn("not found", self.execute("!deluk !discord", message_id="m3", stream=self.stream_b))
        self.assertEqual(self.execute("!edituk !discord | New text", message_id="m4"), "✅ !discord updated.")

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


if __name__ == "__main__":
    unittest.main()
