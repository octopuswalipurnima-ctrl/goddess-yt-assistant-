"""Production-owned YouTube channels that use the normal Streamer workflow."""
from sqlalchemy.orm import Session

from app.database.models import AuditLog, Streamer


# Keep the monitored-channel source of truth in one place.  Streamer rows are
# still the runtime authority used by WebSub renewal and the chat monitor.
MONITORED_CHANNEL_IDS = (
    "UCGH_osSgL2FCsBYe6XMxlSQ",
    "UCCMwadkzXrznmMpZd5ek6PA",
    "UCf4bzltnoyrCM_SAXLTfvAg",
    "UCVQ8Qn1JPuZV8VzOgIdUGxQ",
)


def ensure_monitored_channels(db: Session) -> list[Streamer]:
    """Create missing monitored Streamer rows once; never duplicate records."""
    streamers = []
    changed = False
    for channel_id in MONITORED_CHANNEL_IDS:
        streamer = db.query(Streamer).filter(Streamer.youtube_channel_id == channel_id).first()
        if streamer is None:
            streamer = Streamer(
                youtube_channel_id=channel_id,
                channel_name=channel_id,
                is_active=True,
            )
            db.add(streamer)
            db.flush()
            db.add(AuditLog(
                streamer_id=streamer.id,
                user_id=None,
                action="MONITORED_CHANNEL_REGISTERED",
                details=channel_id,
            ))
            changed = True
        streamers.append(streamer)
    if changed:
        db.commit()
    return streamers
