import json
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from google import genai
from google.genai import types
from app.database.models import ChatLog, StreamAnalyticsMetric

client = genai.Client()

class LiveAnalyticsProcessor:
    def __init__(self, db: Session, streamer_id: int):
        self.db = db
        self.streamer_id = streamer_id
        self.model_name = "gemini-2.5-flash"

    async def compute_minute_metrics(self) -> dict:
        """Processes the past 60 seconds of chat logs into metrics with a single API call."""
        one_minute_ago = datetime.utcnow() - timedelta(seconds=60)
        logs = self.db.query(ChatLog).filter(
            ChatLog.streamer_id == self.streamer_id,
            ChatLog.timestamp >= one_minute_ago
        ).all()

        if not logs:
            return {"mood": {"positive": 50, "neutral": 50, "toxic": 0}, "highlight": False}

        message_compilation = "\n".join([f"[{log.user_id}]: {log.message}" for log in logs])
        
        prompt = (
            f"Analyze the following stream chat logs from the last 60 seconds:\n{message_compilation}\n\n"
            "Return a JSON object containing the combined community mood percentages (must total 100) "
            "and determine if a hype streaming highlight event occurred (e.g., massive bursts of W, GG, clutch, omg). "
            'Expected schema format: {"mood": {"positive": int, "neutral": int, "toxic": int}, "highlight": bool}'
        )

        try:
            response = client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2
                )
            )
            data = json.loads(response.text)
            
            # Commit metrics to database tracking repository
            metric = StreamAnalyticsMetric(
                streamer_id=self.streamer_id,
                mood_score=data.get("mood"),
                is_highlight=data.get("highlight", False)
            )
            self.db.add(metric)
            self.db.commit()
            return data
        except Exception:
            return {"mood": {"positive": 50, "neutral": 50, "toxic": 0}, "highlight": False}