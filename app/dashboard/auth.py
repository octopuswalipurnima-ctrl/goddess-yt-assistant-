import os
from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
from sqlalchemy.orm import Session
import traceback

from app.database.connection import get_db
from app.database.models import Streamer, User
from app.utils.config import Config

router = APIRouter()
oauth = OAuth()

# FIX 1: Injecting a real Browser User-Agent into the backend client to bypass Cloudflare/Bot blocks
oauth.register(
    name='google',
    client_id=Config.YOUTUBE_CLIENT_ID,
    client_secret=Config.YOUTUBE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile https://www.googleapis.com/auth/youtube.readonly',
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json'
        }
    }
)

@router.get("/login")
async def login(request: Request):
    # Dynamically build the URL based on the current environment
    redirect_uri = str(request.url_for('auth_callback'))
    
    # SECURITY OVERRIDE: Force HTTPS for the live Railway server
    if "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://")
        
    print(f"[AUTH ROUTE] Requesting login via: {redirect_uri}", flush=True)
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    print(f"[AUTH DEBUG] Callback reached. Fetching access token...", flush=True)
    try:
        # We rely on the ForceHTTPSMiddleware in main.py to handle the internal request scheme here
        # so we DO NOT pass the redirect_uri argument here, preventing the TypeError crash.
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
            
            # --- PHASE 2/3 SYNC: POPULATE GLOBAL USER IDENTITY FOR RBAC ---
            user = db.query(User).filter(User.youtube_id == youtube_id).first()
            if not user:
                user = User(youtube_id=youtube_id, username=name)
                db.add(user)
                db.commit()
                db.refresh(user)

            # Check for existing Streamer profile (Owner)
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
            
            # Write to session memory storage (INCLUDING NEW user_id)
            request.session['user_id'] = user.id  
            request.session['streamer_id'] = streamer.id
            request.session['streamer_name'] = name
            print(f"[AUTH SUCCESS] Session saved for {name}. User ID: {user.id}, Streamer ID: {streamer.id}", flush=True)
            
            # --- MAGIC INVITE REDIRECT LOGIC ---
            # If they logged in because they clicked a team invite link, send them back to process it!
            pending_invite = request.session.pop("pending_invite", None)
            if pending_invite:
                print(f"[AUTH DEBUG] Consuming pending magic invite: {pending_invite}", flush=True)
                return RedirectResponse(url=f"/invite/{pending_invite}", status_code=303)

        else:
            print("[AUTH WARNING] Google authentication completed but userinfo fields were unreadable.", flush=True)
            
    except Exception as e:
        print(f"[AUTH ERROR] Runtime breakdown inside authorization loop: {e}", flush=True)
        traceback.print_exc()
        # If it crashes, send them to the homepage with a clear error so they aren't stuck in a blank loop
        return RedirectResponse(url="/?error=auth_failed", status_code=303)
        
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)