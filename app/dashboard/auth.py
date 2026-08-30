"""Password-only dashboard authentication; YouTube credentials remain bot-only."""
import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.database.models import Streamer, User
from app.utils.config import Config

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def ensure_dashboard_identity(db: Session) -> tuple[User, Streamer]:
    """Create a local dashboard owner without inventing a YouTube identity."""
    identity = f"dashboard-admin:{Config.ADMIN_USERNAME}"
    user = db.query(User).filter_by(youtube_id=identity).first()
    if not user:
        user = User(youtube_id=identity, username=Config.ADMIN_USERNAME)
        db.add(user)
        db.flush()
    # Prefer the existing production streamer configuration so this login
    # migration does not create a parallel manual-channel workspace.
    streamer = db.query(Streamer).order_by(Streamer.id.asc()).first()
    if not streamer:
        streamer = Streamer(youtube_channel_id=None, channel_name="Dashboard Channel", is_active=False)
        db.add(streamer)
        db.flush()
    db.commit()
    return user, streamer


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("is_admin"):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": request.query_params.get("error") == "invalid_credentials"})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    configured = Config.ADMIN_USERNAME and Config.ADMIN_PASSWORD_HASH
    valid = False
    if configured and username == Config.ADMIN_USERNAME:
        try:
            valid = bcrypt.checkpw(password.encode("utf-8"), Config.ADMIN_PASSWORD_HASH.encode("utf-8"))
        except (ValueError, TypeError):
            valid = False
    if not valid:
        return RedirectResponse(url="/login?error=invalid_credentials", status_code=303)

    user, streamer = ensure_dashboard_identity(db)
    request.session.clear()
    request.session.update({"is_admin": True, "user_id": user.id, "streamer_id": streamer.id, "streamer_name": streamer.channel_name})
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
