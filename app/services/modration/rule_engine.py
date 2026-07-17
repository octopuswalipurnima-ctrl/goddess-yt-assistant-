import re
from collections import Counter

class LocalRuleEngine:
    def __init__(self):
        # Extendable bad words list and common scam patterns
        self.blacklist_words = {"scamlink", "freecoins", "hackpubg", "hackbgmi", "sellcoins"}
        self.scam_domains = re.compile(r"(bit\.ly|t\.co|free-vbucks|get-coins|claim-gift)", re.IGNORECASE)
        self.contact_pattern = re.compile(r"(\+?\d{2,4}[-.\s]?\d{7,10}|[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")
        
    def evaluate(self, text: str) -> dict:
        """
        Executes pure local sync heuristics without making API calls.
        Returns evaluation dict with local confidence flags.
        """
        if not text or len(text.strip()) == 0:
            return {"verdict": "Safe", "reason": "Empty string"}

        normalized = text.lower().strip()
        words = normalized.split()

        # 1. Direct Keyword Matching
        if any(word in self.blacklist_words for word in words):
            return {"verdict": "Dangerous", "reason": "Blacklisted scam keyword matching."}

        # 2. Link, Phone, and Email Detection
        if self.scam_domains.search(normalized) or self.contact_pattern.search(normalized):
            return {"verdict": "Dangerous", "reason": "Scam pattern, unauthorized URI link, or PII exposed."}

        # 3. Text Character Flooding & Caps Spam Heuristics
        if len(text) > 10:
            caps_ratio = sum(1 for c in text if c.isupper()) / len(text)
            if caps_ratio > 0.75:
                return {"verdict": "Spam", "reason": "Excessive caps utilization flag."}

            # Check for character extensions (e.g. "gooooooal")
            counts = Counter(normalized)
            if any(count > 6 for count in counts.values()):
                # Potential emote wave or harmless chatter, flag as Questionable context
                return {"verdict": "Questionable", "reason": "Character replication spike."}

        # 4. Standard Base Cases
        # Simple short words or greetings are explicitly short-circuited
        if len(words) <= 2 and normalized in {"gg", "hi", "hello", "op", "nice", "w", "clutch"}:
            return {"verdict": "Safe", "reason": "Common short safe chat token."}

        # Catch-all defaults to Questionable to undergo AI processing down-pipeline if needed
        return {"verdict": "Questionable", "reason": "Ambiguous semantic content."}