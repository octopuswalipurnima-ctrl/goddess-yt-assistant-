import asyncio
import logging
from typing import Optional
import google.generativeai as genai

from app.services.common.rate_limiter import TokenBucketRateLimiter
from app.services.common.queue_manager import APIQueueManager, Priority
from app.services.common.credential_manager import gemini_cred_manager

logger = logging.getLogger("goddess_stream_manager")

class GeminiAPIManager:
    def __init__(self):
        # Rolling back to 1.5-flash to ensure 100% compatibility with older server packages
        self.model_name = "gemini-1.5-flash" 
        self.rate_limiter = TokenBucketRateLimiter(capacity=5, refill_rate_per_second=0.5)
        self.queue_manager = APIQueueManager(max_concurrent=2)

    async def generate_content(
        self, 
        prompt: str, 
        system_instruction: str, 
        temperature: float = 0.8, 
        max_output_tokens: int = 60,
        priority: Priority = Priority.LOW
    ) -> Optional[str]:
        
        async def _raw_generate():
            # 1. Ask Credential Manager for a Healthy Key
            cred = gemini_cred_manager.get_healthy_credential()
            if not cred: 
                logger.error("[GEMINI API] 🚨 No healthy API keys available to use!")
                return None

            await self.rate_limiter.acquire(1)
            
            def _execute_sdk_call():
                # Use standard generativeai syntax
                genai.configure(api_key=cred.secret)
                model = genai.GenerativeModel(
                    model_name=self.model_name,
                    system_instruction=system_instruction
                )
                return model.generate_content(
                    prompt,
                    generation_config=genai.types.GenerationConfig(
                        temperature=temperature,
                        max_output_tokens=max_output_tokens
                    )
                )
            
            try:
                response = await asyncio.to_thread(_execute_sdk_call)
                if not response or not hasattr(response, 'text') or not response.text:
                    return None
                
                cred.successful_requests += 1
                return response.text.strip()
                
            except Exception as e:
                # 2. Intelligent Failover: If Rate Limited/Quota Exceeded, put this key in timeout
                err_msg = str(e).lower()
                logger.error(f"[GEMINI SDK ERROR on {cred.identifier}] {e}")
                
                if "429" in err_msg or "quota" in err_msg or "exhausted" in err_msg:
                    cred.apply_cooldown(seconds=120)  # 2 minute timeout for this specific project
                return None

        try:
            return await self.queue_manager.execute(priority, _raw_generate)
        except Exception as e:
            logger.error(f"[GEMINI QUEUE ERROR] {e}")
            return None

# Global instance
gemini_api_manager = GeminiAPIManager()