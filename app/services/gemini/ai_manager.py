import asyncio
import logging
from typing import Optional
from google import genai
from google.genai import types

from app.utils.config import Config
from app.services.common.cache import global_cache
from app.services.common.rate_limiter import TokenBucketRateLimiter
from app.services.common.queue_manager import APIQueueManager, Priority

logger = logging.getLogger("goddess_stream_manager")

class GeminiAPIManager:
    def __init__(self):
        # Initializing Google GenAI Client
        self.client = genai.Client(api_key=Config.GEMINI_API_KEY)
        self.model_name = "gemini-2.5-flash"
        
        # Token Bucket: Allows bursts up to 5, refills at 0.5 tokens/sec (1 request per 2 seconds max)
        self.rate_limiter = TokenBucketRateLimiter(capacity=5, refill_rate_per_second=0.5)
        
        # Queue Manager: Limits concurrent AI generation tasks to 2
        self.queue_manager = APIQueueManager(max_concurrent=2)

    async def generate_content(
        self, 
        prompt: str, 
        system_instruction: str, 
        temperature: float = 0.8, 
        max_output_tokens: int = 60,
        priority: Priority = Priority.LOW
    ) -> Optional[str]:
        """
        Queues and executes a Gemini AI text generation request safely.
        Applies rate limiting, queue priorities, thread offloading, and error handling.
        """
        async def _raw_generate():
            await self.rate_limiter.acquire(1)
            
            # Offload synchronous Google GenAI SDK call to a worker thread
            def _execute_sdk_call():
                return self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens
                    )
                )
            
            response = await asyncio.to_thread(_execute_sdk_call)
            
            # Safe response validation
            if not response or not hasattr(response, 'text') or not response.text:
                return None
                
            return response.text.strip()

        try:
            return await self.queue_manager.execute(priority, _raw_generate)
        except Exception as e:
            logger.error(f"[GEMINI API MANAGER] Generation error: {e}")
            return None

# Global instance
gemini_api_manager = GeminiAPIManager()