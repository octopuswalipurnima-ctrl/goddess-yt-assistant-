import asyncio
import logging
from typing import Optional
from google import genai
from google.genai import types
import httpx

from app.services.common.rate_limiter import TokenBucketRateLimiter
from app.services.common.queue_manager import APIQueueManager, Priority
from app.services.common.credential_manager import gemini_cred_manager
from app.utils.config import Config

logger = logging.getLogger("goddess_stream_manager")


class AIProviderUnavailableError(RuntimeError):
    """No configured provider could produce a response."""


class AIResponseEmptyError(RuntimeError):
    """A provider completed successfully but supplied no usable text."""


class GeminiAPIManager:
    def __init__(self):
        # Default primary and fallback models using current supported SDK standards
        self.primary_model = Config.GEMINI_PRIMARY_MODEL
        self.fallback_model = Config.GEMINI_FALLBACK_MODEL
        self.rate_limiter = TokenBucketRateLimiter(capacity=5, refill_rate_per_second=0.5)
        self.queue_manager = APIQueueManager(max_concurrent=2)

    @property
    def openrouter_available(self) -> bool:
        return bool(Config.OPENROUTER_ENABLED and Config.OPENROUTER_API_KEY and Config.OPENROUTER_MODEL)

    async def _generate_openrouter(self, prompt: str, system_instruction: Optional[str], temperature: float, max_output_tokens: int) -> str:
        """OpenRouter is a sequential fallback; it never duplicates a healthy Gemini call."""
        if not self.openrouter_available:
            raise AIProviderUnavailableError("OpenRouter is not configured")
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0)) as client:
                response = await client.post(
                    f"{Config.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {Config.OPENROUTER_API_KEY}"},
                    json={"model": Config.OPENROUTER_MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_output_tokens},
                )
                response.raise_for_status()
                payload = response.json()
                choices = payload.get("choices") if isinstance(payload, dict) else None
                content = choices[0].get("message", {}).get("content") if isinstance(choices, list) and choices else None
                if isinstance(content, str) and content.strip():
                    logger.info("[OPENROUTER] Request succeeded")
                    return content.strip()
                logger.warning("[OPENROUTER] Request completed with an empty response")
                raise AIResponseEmptyError("OpenRouter returned no usable text")
        except AIResponseEmptyError:
            raise
        except httpx.HTTPStatusError as exc:
            # Never log response bodies or headers: they may contain private
            # content or credentials.
            logger.warning("[OPENROUTER] Request failed with status=%s", exc.response.status_code)
            raise AIProviderUnavailableError("OpenRouter request failed") from exc
        except Exception as exc:
            logger.warning("[OPENROUTER] Request failed type=%s", type(exc).__name__)
            raise AIProviderUnavailableError("OpenRouter request failed") from exc

    async def generate_content(
        self, 
        prompt: str, 
        system_instruction: Optional[str] = None, 
        temperature: float = 0.8, 
        max_output_tokens: int = 60,
        priority: Priority = Priority.LOW
    ) -> str:
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

        # When credential rotation has already established that every Gemini
        # project is unavailable, do not enqueue or retry Gemini. Go directly
        # to the one configured fallback request.
        if not gemini_cred_manager.has_healthy_credential():
            logger.warning("[GEMINI API] No healthy API keys available; falling back to OpenRouter")
            return await self._generate_openrouter(prompt, system_instruction, temperature, max_output_tokens)

        try:
            result = await self.queue_manager.execute(priority, _raw_generate)
            if isinstance(result, str) and result.strip():
                return result.strip()
            logger.warning("[AI_MANAGER] Gemini produced no usable response; falling back to OpenRouter")
        except Exception as exc:
            logger.warning("[AI_MANAGER] Gemini request failed type=%s; falling back to OpenRouter", type(exc).__name__)

        return await self._generate_openrouter(prompt, system_instruction, temperature, max_output_tokens)

# Global instance preserving existing imports across the application
gemini_api_manager = GeminiAPIManager()
