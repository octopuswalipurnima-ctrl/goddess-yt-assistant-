import os
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
    # ProxyHeadersMiddleware in main.py will automatically ensure this is 'https' on Railway
    redirect_uri = str(request.url_for('auth_callback'))
        
    print(f"[AUTH ROUTE] Requesting login via: {redirect_uri}", flush=True)
    return await oauth.google.authorize_redirect(request, redirect_uri)

@router.get("/auth", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    print(f"[AUTH DEBUG] Callback reached. Fetching access token...", flush=True)
    try:
        # Removed the duplicate redirect_uri argument that was causing the crash!
        token = await oauth.google.authorize_access_token(request)
        print(f"[AUTH DEBUG] Token keys received: {list(token.keys())}", flush=True)
        
        # Fallback parsing if userinfo dictionary is nested differently
        userinfo = token.get('userinfo')
        if not userinfo and 'id_token' in token:
            print("[AUTH DEBUG] userinfo missing, attempting to extract from profile scope...", flush=True)
            userinfo = await oauth.google.parse_id_token(request, token)

        if userinfo:
            name = userinfo.get("name")
            youtube_id = userinfo.get("sub")
            print(f"[AUTH DEBUG] Parsed Userinfo - Name: {name}, YouTube ID: {youtube_id}", flush=True)
            
            streamer = db.query(Streamer).filter(Streamer.youtube_channel_id == youtube_id).first()
            if not streamer:
                print(f"[AUTH DEBUG] New Streamer account creating for: {name}", flush=True)
                streamer = Streamer(
                    youtube_channel_id=youtube_id,
                    channel_name=name,
                    is_active=True
                )
                db.add(streamer)
                db.commit()
                db.refresh(streamer)
            
            # Write to session memory storage
            request.session['streamer_id'] = streamer.id
            request.session['streamer_name'] = name
            print(f"[AUTH SUCCESS] Session saved for {name}. Active ID: {streamer.id}", flush=True)
        else:
            print("[AUTH WARNING] Google authentication completed but userinfo fields were unreadable.", flush=True)
            
    except Exception as e:
        print(f"[AUTH ERROR] Runtime breakdown inside authorization loop: {e}", flush=True)
        traceback.print_exc()
        
    return RedirectResponse(url="/", status_code=303)

@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)