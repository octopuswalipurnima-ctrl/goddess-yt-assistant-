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
                return "Yo chat! Focusing on this intense lobby right now, let's get this chicken dinner!"

            await self.rate_limiter.acquire(1)
            
            def _execute_sdk_call():
                genai.configure(api_key=cred.secret)
                full_content = f"System Instruction: {system_instruction}\n\nUser Request: {prompt}"
                
                # Loop through standard available models
                for mod_name in ["gemini-1.5-flash", "gemini-pro", "gemini-1.5-pro"]:
                    try:
                        model = genai.GenerativeModel(model_name=mod_name)
                        res = model.generate_content(
                            full_content,
                            generation_config=genai.types.GenerationConfig(
                                temperature=temperature,
                                max_output_tokens=max_output_tokens
                            )
                        )
                        if res and hasattr(res, 'text') and res.text:
                            return res.text.strip()
                    except Exception:
                        continue
                return None
            
            try:
                response_text = await asyncio.to_thread(_execute_sdk_call)
                if response_text:
                    cred.successful_requests += 1
                    return response_text
                
                # If all SDK calls return 404, provide a natural fallback response matching Goddess's BGMI persona
                logger.warning(f"[{cred.identifier}] All Gemini model endpoints returned 404. Using smart streamer fallback response.")
                return "Let's go chat! No-gyro recoil control is locked in today, let's secure the win!"
                
            except Exception as e:
                logger.error(f"[GEMINI SDK ERROR on {cred.identifier}] {e}")
                return "Let's go chat! No-gyro recoil control is locked in today, let's secure the win!"

        try:
            return await self.queue_manager.execute(priority, _raw_generate)
        except Exception as e:
            logger.error(f"[GEMINI QUEUE ERROR] {e}")
            return "Let's go chat! No-gyro recoil control is locked in today, let's secure the win!"

gemini_api_manager = GeminiAPIManager()