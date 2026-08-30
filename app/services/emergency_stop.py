"""Persistent circuit breaker for outbound chat and moderation actions."""
from sqlalchemy.orm import Session
from app.database.models import SystemState


class EmergencyStopController:
    def _state(self, db: Session) -> SystemState:
        state = db.query(SystemState).first()
        if not state:
            state = SystemState(); db.add(state); db.flush()
        return state

    def is_stopped(self, db: Session) -> bool:
        return bool(self._state(db).emergency_stop)

    def set(self, db: Session, enabled: bool, reason: str = "") -> None:
        state = self._state(db)
        state.emergency_stop = enabled
        state.emergency_reason = reason[:500] if enabled else None


emergency_stop = EmergencyStopController()
