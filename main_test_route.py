from fastapi import APIRouter, Depends
from starlette.requests import Request
from starlette.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database.connection import get_db
from app.database.models import Streamer

router = APIRouter()

@router.get("/mock-login")
async def mock_login(request: Request, db: Session = Depends(get_db)):
    streamer = db.query(Streamer).filter(Streamer.id == 1).first()
    if not streamer:
        streamer = Streamer(id=1, channel_name="MockStreamer", is_active=True, youtube_channel_id="mock_id")
        db.add(streamer)
        db.commit()

    request.session["streamer_id"] = 1
    request.session["streamer_name"] = "MockStreamer"
    return RedirectResponse(url="/", status_code=303)
