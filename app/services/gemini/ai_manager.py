import asyncio
import logging
from typing import Optional
from google import genai
from google.genai import types

from app.services.common.rate_limiter import TokenBucketRateLimiter
from app.services.common.queue_manager import APIQueueManager, Priority
from app.services.common.credential_manager import gemini_cred_manager

logger = logging.getLogger("goddess_stream_manager")

class GeminiAPIManager:
    def __init__(self):
        # Default primary and fallback models using current supported SDK standards
        self.primary_model = "gemini-2.5-flash"
        self.fallback_model = "gemini-2.5-flash-lite"
        self.rate_limiter = TokenBucketRateLimiter(capacity=5, refill_rate_per_second=0.5)
        self.queue_manager = APIQueueManager(max_concurrent=2)

    async def generate_content(
        self, 
        prompt: str, 
        system_instruction: Optional[str] = None, 
        temperature: float = 0.8, 
        max_output_tokens: int = 60,
        priority: Priority = Priority.LOW
    ) -> Optional[str]:
        """
        Public method matching existing signature. Preserves queue priority,
        rate limiting, and credential rotation while using the modern google-genai SDK.
        """
        
        async def _raw_generate():
            cred = gemini_cred_manager.get_healthy_credential()
            if not cred: 
                logger.error("[GEMINI API] 🚨 No healthy API keys available to use!")
                return None

            await self.rate_limiter.acquire(1)
            
            def _execute_sdk_call():
                # Initialize per-request client using rotating credential secret (Safe & Isolated)
                client = genai.Client(api_key=cred.secret)
                
                # Build configuration safely using types.GenerateContentConfig
                config_args = {
                    "temperature": temperature,
                    "max_output_tokens": max_output_tokens,
                }
                if system_instruction:
                    config_args["system_instruction"] = system_instruction
                
                config = types.GenerateContentConfig(**config_args)

                models_to_try = [self.primary_model, self.fallback_model]
                last_exception = None

                for model_name in models_to_try:
                    try:
                        response = client.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config=config
                        )
                        if response and hasattr(response, 'text') and response.text:
                            return response.text.strip()
                    except Exception as e:
                        last_exception = e
                        err_str = str(e).lower()
                        # If permanent 404 / model not found, try fallback model
                        if "404" in err_str or "not found" in err_str or "unsupported" in err_str:
                            logger.warning(f"[{cred.identifier}] Model {model_name} unavailable (404). Trying fallback model...")
                            continue
                        raise e
                
                if last_exception:
                    raise last_exception
                return None
            
            try:
                response_text = await asyncio.to_thread(_execute_sdk_call)
                if response_text:
                    cred.successful_requests += 1
                    return response_text
                return None
                
            except Exception as e:
                err_msg = str(e).lower()
                logger.error(f"[GEMINI SDK ERROR on {cred.identifier}] {e}")
                
                if "429" in err_msg or "quota" in err_msg or "exhausted" in err_msg:
                    cred.apply_cooldown(seconds=120)  # 2-minute cooldown for exhausted keys
                return None

        try:
            return await self.queue_manager.execute(priority, _raw_generate)
        except Exception as e:
            logger.error(f"[GEMINI QUEUE ERROR] {e}")
            return None

# Global instance preserving existing imports across the application
gemini_api_manager = GeminiAPIManager()