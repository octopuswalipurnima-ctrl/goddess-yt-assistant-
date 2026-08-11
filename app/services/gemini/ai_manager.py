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
        # Aligning model name globally
        self.model_name = "gemini-2.5-flash"
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
            cred = gemini_cred_manager.get_healthy_credential()
            if not cred: 
                logger.error("[GEMINI API] 🚨 No healthy API keys available to use!")
                return None

            await self.rate_limiter.acquire(1)
            
            def _execute_sdk_call():
                genai.configure(api_key=cred.secret)
                
                # Combine system instruction and prompt for maximum SDK compatibility
                full_content = f"System Instruction: {system_instruction}\n\nUser Request: {prompt}"
                
                # Use the standard generative model generation with safety fallbacks
                for mod_name in ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-pro"]:
                    try:
                        model = genai.GenerativeModel(model_name=mod_name)
                        return model.generate_content(
                            full_content,
                            generation_config=genai.types.GenerationConfig(
                                temperature=temperature,
                                max_output_tokens=max_output_tokens
                            )
                        )
                    except Exception:
                        continue
                raise Exception("All fallback Gemini model endpoints failed with this API key.")
            
            try:
                response = await asyncio.to_thread(_execute_sdk_call)
                if not response or not hasattr(response, 'text') or not response.text:
                    return None
                
                cred.successful_requests += 1
                return response.text.strip()
                
            except Exception as e:
                err_msg = str(e).lower()
                logger.error(f"[GEMINI SDK ERROR on {cred.identifier}] {e}")
                
                if "429" in err_msg or "quota" in err_msg or "exhausted" in err_msg:
                    cred.apply_cooldown(seconds=120)
                return None

        try:
            return await self.queue_manager.execute(priority, _raw_generate)
        except Exception as e:
            logger.error(f"[GEMINI QUEUE ERROR] {e}")
            return None

gemini_api_manager = GeminiAPIManager()