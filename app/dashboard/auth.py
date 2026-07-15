import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database.connection import get_db
from app.database.models import Streamer
from app.utils.config import Config

router = APIRouter()

# The exact URL Google will send them back to after they log in
# Make sure to change YOUR_RAILWAY_URL to your actual live URL!
REDIRECT_URI = goddess-yt-assistant-production.up.railway.app

@router.get("/login")
async def login_with_youtube():
    # 1. Send the YouTuber to Google's permission screen
    scopes = "https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/youtube.force-ssl"
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={Config.YOUTUBE_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code&scope={scopes}&access_type=offline&prompt=consent"
    )
    return RedirectResponse(auth_url)

@router.get("/auth/callback")
async def auth_callback(code: str, db: Session = Depends(get_db)):
    # 2. They came back! Exchange their temporary code for a permanent Refresh Token
    token_data = {
        "code": code,
        "client_id": Config.YOUTUBE_CLIENT_ID,
        "client_secret": Config.YOUTUBE_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code"
    }
    response = requests.post("https://oauth2.googleapis.com/token", data=token_data)
    tokens = response.json()

     refresh_token = tokens.get("refresh_token")
     access_token = tokens.get("access_token")

    if not refresh_token:
        return {"error": "Google didn't send a refresh token. Try logging in again!"}

    # 3. Ask Google for the channel ID and name using the new access token
    headers = {"Authorization": f"Bearer {access_token}"}
    channel_resp = requests.get("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true", headers=headers)
    channel_data = channel_resp.json()
    
    channel_id = channel_data["items"][0]["id"]
    channel_name = channel_data["items"][0]["snippet"]["title"]

    # 4. Save this YouTuber into your SaaS Database!
    streamer = db.query(Streamer).filter(Streamer.youtube_channel_id == channel_id).first()
    if not streamer:
        streamer = Streamer(youtube_channel_id=channel_id, channel_name=channel_name, oauth_refresh_token=refresh_token)
        db.add(streamer)
    else:
        streamer.oauth_refresh_token = refresh_token
    
    db.commit()
    return {"message": f"Successfully linked {channel_name}! Welcome to Goddess Assistant."}