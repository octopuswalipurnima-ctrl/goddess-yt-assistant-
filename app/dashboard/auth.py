from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
from sqlalchemy.orm import Session
import traceback

from app.database.connection import get_db
from app.database.models import Streamer
from app.utils.config import Config

router = APIRouter()
oauth = OAuth()

oauth.register(
    name='google',
    client_id=Config.YOUTUBE_CLIENT_ID,
    client_secret=Config.YOUTUBE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile https://www.googleapis.com/auth/youtube.readonly'}
)

@router.get("/login")
async def login(request: Request):
    # With ProxyHeadersMiddleware, request.url_for automatically generates the correct https:// URL
    redirect_uri = str(request.url_for('auth_callback'))
    return await oauth.google.authorize_redirect(request, redirect_uri)

@router.get("/auth", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
        userinfo = token.get('userinfo')
        
        if userinfo:
            name = userinfo.get("name")
            youtube_id = userinfo.get("sub")
            
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
            
            request.session['streamer_id'] = streamer.id
            request.session['streamer_name'] = name
            print(f"[AUTH SUCCESS] Logged in as {name}", flush=True)
            
    except Exception as e:
        print(f"[AUTH ERROR] Failed to log in: {e}", flush=True)
        traceback.print_exc()
        
    # status_code=303 ensures the browser cleanly redirects without repeating the auth payload
    return RedirectResponse(url="/", status_code=303)

@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)