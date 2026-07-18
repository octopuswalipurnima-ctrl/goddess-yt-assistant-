import asyncio
from sqlalchemy.orm import Session
from app.database.models import ModActionLog, AutoLearnedRule

async def run_learning_cluster(db: Session):
    """
    Background task to analyze Layer 2 decisions and propose Layer 1 rules.
    Runs nightly via the existing scheduler.
    """
    print("[CL-ENGINE] Initializing Continuous Learning Clustering...")
    
    # 1. Fetch recent actionable offenses exclusively caught by Layer 2
    recent_bans = db.query(ModActionLog).filter(
        ModActionLog.layer_triggered == "Layer 2 (Gemini AI)",
        ModActionLog.recommended_action.in_(["Timeout", "Ban", "Delete"])
    ).limit(500).all()

    if not recent_bans:
        return

    # 2. Cluster the messages (Conceptual implementation)
    # In production, use scikit-learn TF-IDF & DBSCAN or pass the batch to Gemini 
    # with a strict JSON schema asking it to output common Regex patterns.
    extracted_patterns = extract_common_patterns([log.message_content for log in recent_bans])

    # 3. Inject new patterns into the database as 'Shadow' rules
    for pattern_data in extracted_patterns:
        existing_rule = db.query(AutoLearnedRule).filter(
            AutoLearnedRule.pattern == pattern_data["regex"]
        ).first()
        
        if not existing_rule:
            new_rule = AutoLearnedRule(
                streamer_id=pattern_data["streamer_id"],
                pattern=pattern_data["regex"],
                rule_type="regex",
                target_action=pattern_data["suggested_action"],
                status="shadowing"
            )
            db.add(new_rule)
            print(f"[CL-ENGINE] New Shadow Rule injected: {new_rule.pattern}")
            
    db.commit()

def extract_common_patterns(messages):
    """
    Placeholder for NLP extraction logic. 
    Returns a list of dicts containing the extracted regex.
    """
    return []