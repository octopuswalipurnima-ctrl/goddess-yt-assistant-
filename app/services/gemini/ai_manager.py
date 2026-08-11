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
            # 1. Ask Credential Manager for a Healthy Key
            cred = gemini_cred_manager.get_healthy_credential()
            if not cred: 
                logger.error("[GEMINI API] 🚨 No healthy API keys available to use!")
                return None

            await self.rate_limiter.acquire(1)
            
            def _execute_sdk_call():
                genai.configure(api_key=cred.secret)
                
                # Attempt to use the newest 1.5 Flash model
                try:
                    model = genai.GenerativeModel(
                        model_name="gemini-1.5-flash",
                        system_instruction=system_instruction
                    )
                    return model.generate_content(
                        prompt,
                        generation_config=genai.types.GenerationConfig(
                            temperature=temperature,
                            max_output_tokens=max_output_tokens
                        )
                    )
                except Exception as model_err:
                    err_str = str(model_err).lower()
                    
                    # If Google throws a 404 (Model Not Found) due to region or key limits, FALLBACK to 1.0 Pro!
                    if "404" in err_str or "not found" in err_str:
                        logger.warning(f"[{cred.identifier}] 1.5-Flash restricted by Google. Falling back to universal gemini-pro.")
                        
                        fallback_model = genai.GenerativeModel(model_name="gemini-pro")
                        
                        # Gemini 1.0 doesn't support 'system_instruction' directly, so we inject it into the prompt!
                        combined_prompt = f"SYSTEM INSTRUCTION: {system_instruction}\n\nUSER PROMPT: {prompt}"
                        
                        return fallback_model.generate_content(
                            combined_prompt,
                            generation_config=genai.types.GenerationConfig(
                                temperature=temperature,
                                max_output_tokens=max_output_tokens
                            )
                        )
                    # If it's not a 404, raise the error so the outer try/except can catch rate limits
                    raise model_err
            
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