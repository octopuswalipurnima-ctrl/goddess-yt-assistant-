import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.connection import Base
from app.database.models import SystemState
from app.services.discord_events import DiscordEventLogger
from app.services.emergency_stop import EmergencyStopController
from app.services.gemini.ai_manager import AIProviderUnavailableError, AIResponseEmptyError, GeminiAPIManager
from app.utils.config import Config


class EmergencyStopTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close(); self.engine.dispose()

    def test_stop_is_persistent_and_can_be_cleared(self):
        controller = EmergencyStopController()
        self.assertFalse(controller.is_stopped(self.db))
        controller.set(self.db, True, "operator test"); self.db.commit()
        self.assertTrue(controller.is_stopped(self.db))
        self.assertEqual(self.db.query(SystemState).one().emergency_reason, "operator test")
        controller.set(self.db, False); self.db.commit()
        self.assertFalse(controller.is_stopped(self.db))


class DiscordEventLoggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_client_does_not_block_or_raise(self):
        logger = DiscordEventLogger(max_queue=1)
        logger.emit("Moderation", "message")
        logger.configure(type("OfflineClient", (), {"is_ready": lambda self: False})())
        await asyncio.sleep(0)
        await logger.close()

    async def test_dynamic_streamer_channel_is_used(self):
        channel = type("Channel", (), {"send": AsyncMock()})()
        client = type("Client", (), {"is_ready": lambda self: True, "get_channel": lambda self, _: channel})()
        logger = DiscordEventLogger()
        logger._streamer_channel_id = lambda _: "123"
        logger.configure(client)
        logger.emit("Event", "body", streamer_id=7)
        await asyncio.wait_for(logger.queue.join(), timeout=1)
        channel.send.assert_awaited_once()
        await logger.close()

    async def test_sends_to_configured_channel(self):
        channel = type("Channel", (), {"send": AsyncMock()})()
        client = type("Client", (), {"is_ready": lambda self: True, "get_channel": lambda self, _: channel})()
        logger = DiscordEventLogger()
        logger.configure(client)
        logger.emit("Event", "body", "123")
        await asyncio.wait_for(logger.queue.join(), timeout=1)
        channel.send.assert_awaited_once()
        await logger.close()


class ModerationDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_ai_action_fails_closed_to_safe(self):
        # Import lazily so this test remains independent of live credentials.
        from app.services.modration.gemini_client import GeminiModeratorEngine
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            with patch("app.services.modration.gemini_client.gemini_api_manager.generate_content", new=AsyncMock(return_value='{"recommended_action":"erase"}')):
                result = await GeminiModeratorEngine(db).analyze_message("x", [])
            self.assertEqual(result["recommended_action"], "Safe")
            self.assertEqual(result["classification"], "ErrorFallback")
        finally:
            db.close(); engine.dispose()


class CohostTests(unittest.IsolatedAsyncioTestCase):
    def test_persona_context_is_optional_and_active_profile_is_compact(self):
        from app.ai.generator import AIBrain
        brain = AIBrain()
        self.assertEqual(brain.system_instruction_for(None), brain.base_persona)
        for mode in ("roast", "witty", "hype", "cohost"):
            instruction = brain.system_instruction_for({"persona_enabled": True, "personality_mode": mode})
            self.assertIn(f"PERSONA={mode.upper()}", instruction)
            self.assertNotIn("PERSONA=ROAST", instruction) if mode != "roast" else None
            self.assertNotIn("PERSONA=WITTY", instruction) if mode != "witty" else None
            self.assertNotIn("PERSONA=HYPE", instruction) if mode != "hype" else None
            self.assertNotIn("PERSONA=COHOST", instruction) if mode != "cohost" else None

    async def test_cohost_uses_shared_gemini_adapter(self):
        from app.ai.generator import AIBrain
        with patch("app.ai.generator.SessionLocal") as session_factory, patch("app.ai.generator.gemini_api_manager.generate_content", new=AsyncMock(return_value="Nice clutch!")) as generate:
            session = session_factory.return_value
            session.query.return_value.first.return_value = None
            answer = await AIBrain().generate_chat_reaction(["React"], [{"username": "viewer", "text": "wow"}])
        self.assertEqual(answer, "Nice clutch!")
        generate.assert_awaited_once()

    async def test_cohost_hides_provider_failures_from_chat(self):
        from app.ai.generator import AIBrain
        with patch("app.ai.generator.SessionLocal") as session_factory, patch(
            "app.ai.generator.gemini_api_manager.generate_content",
            new=AsyncMock(side_effect=AIProviderUnavailableError("offline")),
        ):
            answer = await AIBrain().generate_chat_reaction(["React"], [])
        self.assertEqual(answer, "Sorry, I'm having trouble generating a response right now. Please try again in a moment.")


class OpenRouterFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old = (Config.OPENROUTER_ENABLED, Config.OPENROUTER_API_KEY, Config.OPENROUTER_MODEL)
        Config.OPENROUTER_ENABLED, Config.OPENROUTER_API_KEY, Config.OPENROUTER_MODEL = True, "test-secret", "provider/model"

    def tearDown(self):
        Config.OPENROUTER_ENABLED, Config.OPENROUTER_API_KEY, Config.OPENROUTER_MODEL = self.old

    async def test_provider_initializes_and_missing_key_is_a_noop(self):
        manager = GeminiAPIManager()
        self.assertTrue(manager.openrouter_available)
        Config.OPENROUTER_API_KEY = None
        self.assertFalse(manager.openrouter_available)
        with self.assertRaises(AIProviderUnavailableError):
            await manager._generate_openrouter("p", None, 0.1, 10)

    async def test_fallback_runs_only_after_gemini_returns_no_result(self):
        manager = GeminiAPIManager()
        manager.queue_manager.execute = AsyncMock(return_value=None)
        manager._generate_openrouter = AsyncMock(return_value="fallback response")
        with patch("app.services.gemini.ai_manager.gemini_cred_manager.has_healthy_credential", return_value=True):
            self.assertEqual(await manager.generate_content("prompt"), "fallback response")
        manager._generate_openrouter.assert_awaited_once()

    async def test_existing_gemini_result_skips_openrouter(self):
        manager = GeminiAPIManager()
        manager.queue_manager.execute = AsyncMock(return_value="gemini response")
        manager._generate_openrouter = AsyncMock()
        with patch("app.services.gemini.ai_manager.gemini_cred_manager.has_healthy_credential", return_value=True):
            self.assertEqual(await manager.generate_content("prompt"), "gemini response")
        manager._generate_openrouter.assert_not_awaited()

    async def test_exhausted_gemini_skips_queue_and_uses_one_openrouter_request(self):
        manager = GeminiAPIManager()
        manager.queue_manager.execute = AsyncMock()
        manager._generate_openrouter = AsyncMock(return_value="fallback response")
        with patch("app.services.gemini.ai_manager.gemini_cred_manager.has_healthy_credential", return_value=False):
            self.assertEqual(await manager.generate_content("prompt"), "fallback response")
        manager.queue_manager.execute.assert_not_awaited()
        manager._generate_openrouter.assert_awaited_once()

    async def test_both_unavailable_raises_provider_error_not_empty_response(self):
        manager = GeminiAPIManager()
        manager._generate_openrouter = AsyncMock(side_effect=AIProviderUnavailableError("offline"))
        with patch("app.services.gemini.ai_manager.gemini_cred_manager.has_healthy_credential", return_value=False):
            with self.assertRaises(AIProviderUnavailableError):
                await manager.generate_content("prompt")

    async def test_genuine_empty_response_remains_distinct(self):
        manager = GeminiAPIManager()
        manager._generate_openrouter = AsyncMock(side_effect=AIResponseEmptyError("empty"))
        with patch("app.services.gemini.ai_manager.gemini_cred_manager.has_healthy_credential", return_value=False):
            with self.assertRaises(AIResponseEmptyError):
                await manager.generate_content("prompt")

    async def test_openrouter_error_is_safe_and_does_not_log_secret(self):
        manager = GeminiAPIManager()
        with patch("app.services.gemini.ai_manager.httpx.AsyncClient", side_effect=Exception("test-secret")):
            with self.assertLogs("goddess_stream_manager", level="WARNING") as logs:
                with self.assertRaises(AIProviderUnavailableError):
                    await manager._generate_openrouter("prompt", None, 0.1, 10)
        self.assertNotIn("test-secret", "\n".join(logs.output))
