import time
import logging
from typing import List, Optional

logger = logging.getLogger("goddess_stream_manager")

class Credential:
    def __init__(self, identifier: str, secret: str):
        self.identifier = identifier
        self.secret = secret
        self.status = "ACTIVE"  # ACTIVE, COOLDOWN, EXHAUSTED
        self.cooldown_until = 0.0
        
        # Internal tracking (Never logs the secret)
        self.successful_requests = 0
        self.failed_requests = 0

    def is_healthy(self) -> bool:
        if self.status == "COOLDOWN" and time.time() > self.cooldown_until:
            self.status = "ACTIVE"
            logger.info(f"[{self.identifier}] Cooldown complete. Restored to ACTIVE.")
        
        return self.status == "ACTIVE"

    def apply_cooldown(self, seconds: int = 60):
        self.status = "COOLDOWN"
        self.cooldown_until = time.time() + seconds
        self.failed_requests += 1
        logger.warning(f"[{self.identifier}] Rate limited! Placed on {seconds}s cooldown.")


class CredentialManager:
    def __init__(self, service_name: str, keys: List[str]):
        self.service_name = service_name
        self.credentials = [
            Credential(f"{service_name}_Project_{i+1}", key) for i, key in enumerate(keys)
        ]
        self._current_index = 0

    def get_healthy_credential(self) -> Optional[Credential]:
        """Finds the next available healthy API key without blind rotation."""
        start_index = self._current_index
        
        for _ in range(len(self.credentials)):
            cred = self.credentials[self._current_index]
            self._current_index = (self._current_index + 1) % len(self.credentials)
            
            if cred.is_healthy():
                return cred
                
        logger.error(f"[{self.service_name}] 🚨 FATAL: All configured projects are currently exhausted or in cooldown!")
        return None

    def has_healthy_credential(self) -> bool:
        """Check availability without rotating credentials or triggering retries."""
        return any(credential.is_healthy() for credential in self.credentials)

# Initialize Global Managers based on Config
from app.utils.config import Config
gemini_cred_manager = CredentialManager("Gemini_AI", Config.GEMINI_API_KEYS)
# Note: YouTube read-keys can be managed here if needed in the future
