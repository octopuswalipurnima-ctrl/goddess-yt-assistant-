import re
from collections import Counter
from sqlalchemy.orm import Session
# Import the learning rules model for the dynamic pipeline
from app.database.models import AutoLearnedRule

class LocalRuleEngine:
    def __init__(self, db: Session = None, streamer_id: int = None):
        """
        Initializes the static heuristics and loads dynamic continuous learning rules.
        (db and streamer_id are optional for backward compatibility in tests).
        """
        self.db = db
        self.streamer_id = streamer_id
        
        # --- STATIC HEURISTICS ---
        # Extendable bad words list and common scam patterns
        self.blacklist_words = {"scamlink", "freecoins", "hackpubg", "hackbgmi", "sellcoins"}
        self.scam_domains = re.compile(r"(bit\.ly|t\.co|free-vbucks|get-coins|claim-gift)", re.IGNORECASE)
        self.contact_pattern = re.compile(r"(\+?\d{2,4}[-.\s]?\d{7,10}|[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")
        
        # --- DYNAMIC RULES (Continuous Learning) ---
        self.dynamic_rules = []
        if self.db and self.streamer_id:
            # Fetch both active rules (which take action) and shadow rules (which run silently to learn)
            self.dynamic_rules = self.db.query(AutoLearnedRule).filter(
                AutoLearnedRule.streamer_id == self.streamer_id,
                AutoLearnedRule.status.in_(["active", "shadowing"])
            ).all()
        
    def evaluate(self, text: str) -> dict:
        """
        Executes pure local sync heuristics and dynamic continuous learning rules.
        Returns evaluation dict with local confidence flags and shadow rule triggers.
        """
        result = {
            "verdict": "Questionable", 
            "reason": "Ambiguous semantic content.",
            "shadow_triggers": [] # NEW: Tracks silent hits for calibration
        }
        
        if not text or len(text.strip()) == 0:
            result.update({"verdict": "Safe", "reason": "Empty string"})
            return result

        normalized = text.lower().strip()
        words = normalized.split()

        # -------------------------------------------------------------
        # LAYER 1.5: DYNAMIC CONTINUOUS LEARNING RULES
        # -------------------------------------------------------------
        for rule in self.dynamic_rules:
            try:
                # Support both keyword matches and Regex evaluation
                if rule.rule_type == "exact_match" and rule.pattern.lower() in normalized:
                    matched = True
                elif rule.rule_type == "regex" and re.search(rule.pattern, normalized, re.IGNORECASE):
                    matched = True
                else:
                    matched = False
                    
                if matched:
                    if rule.status == "active":
                        # Active rule overrides baseline checks
                        result["verdict"] = rule.target_action
                        result["reason"] = f"Matched continuous learning AI rule: {rule.pattern}"
                    elif rule.status == "shadowing":
                        # Shadow rules don't take action, they just log their trigger for confidence testing
                        result["shadow_triggers"].append(rule.id)
            except re.error:
                # Failsafe: if the AI generates a broken regex, it won't crash the server
                continue 

        # If a trained active rule caught it, return immediately to save compute time
        if result["verdict"] != "Questionable":
            return result

        # -------------------------------------------------------------
        # LAYER 1.0: STATIC HEURISTICS
        # -------------------------------------------------------------
        # 1. Direct Keyword Matching
        if any(word in self.blacklist_words for word in words):
            result.update({"verdict": "Dangerous", "reason": "Blacklisted scam keyword matching."})
            return result

        # 2. Link, Phone, and Email Detection
        if self.scam_domains.search(normalized) or self.contact_pattern.search(normalized):
            result.update({"verdict": "Dangerous", "reason": "Scam pattern, unauthorized URI link, or PII exposed."})
            return result

        # 3. Text Character Flooding & Caps Spam Heuristics
        if len(text) > 10:
            caps_ratio = sum(1 for c in text if c.isupper()) / len(text)
            if caps_ratio > 0.75:
                result.update({"verdict": "Spam", "reason": "Excessive caps utilization flag."})
                return result

            # Check for character extensions (e.g. "gooooooal")
            counts = Counter(normalized)
            if any(count > 6 for count in counts.values()):
                # Potential emote wave or harmless chatter, flag as Questionable context
                result.update({"verdict": "Questionable", "reason": "Character replication spike."})
                return result

        # 4. Standard Base Cases
        # Simple short words or greetings are explicitly short-circuited
        if len(words) <= 2 and normalized in {"gg", "hi", "hello", "op", "nice", "w", "clutch"}:
            result.update({"verdict": "Safe", "reason": "Common short safe chat token."})
            return result

        # Catch-all defaults to Questionable to undergo AI processing down-pipeline if needed
        return result

    def calibrate_shadow_rules(self, shadow_ids: list, final_gemini_verdict: str):
        """
        NEW: Called by main.py AFTER Gemini returns a final result.
        Updates the confidence scores of the shadow rules that triggered.
        """
        if not self.db or not shadow_ids:
            return
            
        for rule_id in shadow_ids:
            rule = self.db.query(AutoLearnedRule).filter(AutoLearnedRule.id == rule_id).first()
            if not rule:
                continue
            
            rule.shadow_hits += 1
            
            # If Gemini marked the message Safe, this shadow rule caused a False Positive
            if final_gemini_verdict == "Safe":
                rule.false_positives += 1
            
            # Calibration Formula: Penalize False Positives heavily (2x weight)
            penalty_weight = 2.0
            raw_score = (rule.shadow_hits - (rule.false_positives * penalty_weight)) / rule.shadow_hits
            
            # Clamp the score strictly between 0.0 and 1.0 (0% to 100%)
            rule.confidence_score = max(0.0, min(1.0, raw_score))
            
            # State Machine: Auto-promote to dashboard if highly confident
            # (e.g., requires at least 10 hits and 95% accuracy to be proposed to admins)
            if rule.shadow_hits > 10 and rule.confidence_score >= 0.95:
                rule.status = "proposed"
                
        self.db.commit()