from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.database.models import Streamer
from app.utils.config import Config

router = APIRouter()
oauth = OAuth()

# Configure Google OAuth for multi-tenant YouTube verification
oauth.register(
    name='google',
    client_id=Config.YOUTUBE_CLIENT_ID,
    client_secret=Config.YOUTUBE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile https://www.googleapis.com/auth/youtube.readonly'
    }
)

@router.get("/login")
async def login(request: Request):
    # FORCE HTTPS: Dynamic secure callback injection for cross-environment safety on Railway
    redirect_uri = str(request.url_for('auth_callback'))
    if "localhost" not in redirect_uri and redirect_uri.startswith("http://"):
        redirect_uri = redirect_uri.replace("http://", "https://")
        
    return await oauth.google.authorize_redirect(request, redirect_uri)

@router.get("/auth", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    # FORCE HTTPS: Modifies internal request scopes to align state values under reverse proxies
    if "localhost" not in str(request.url) and str(request.url).startswith("http://"):
        request.scope["scheme"] = "https"
        
    try:
        token = await oauth.google.authorize_access_token(request)
        userinfo = token.get('userinfo')
        
        if userinfo:
            email = userinfo.get("email")
            name = userinfo.get("name")
            youtube_id = userinfo.get("sub")  # Google OAuth unique subject identifier
            
            # Locate or create the isolated Streamer profile entry
            streamer = db.query(Streamer).filter(Streamer.youtube_channel_id == youtube_id).first()
            if not streamer:
                streamer = Streamer(
                    youtube_channel_id=youtube_id,
                    channel_name=name,
                    is_active=True
                )
                db.add(streamer)
                db.commit()
                db.refresh(streamer)
            
            # Secure state caching into user browser cookies
            request.session['streamer_id'] = streamer.id
            request.session['streamer_name'] = name
            
    except Exception as e:
        print(f"[AUTH ERROR] Callback token extraction failure: {e}")
        
    return RedirectResponse(url="/")

@router.get("/logout")
async def logout(request: Request):
    request.session.clear()  # Purges the browser session cookies instantly
    return RedirectResponse(url="/")