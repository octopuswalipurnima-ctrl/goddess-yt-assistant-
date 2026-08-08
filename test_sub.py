import urllib.request
import urllib.parse
import random
import string
from sqlalchemy import text
from app.database.connection import SessionLocal, init_db, engine
from app.database.models import Streamer

# The three channels you want to register
channels = [
    {"uc_id": "UCGH_osSgL2FCsBYe6XMxlSQ", "name": "Goddess Live"},
    {"uc_id": "UCVQ8Qn1JPuZV8VzOgIdUGxQ", "name": "Channel 2"},
    {"uc_id": "UCCMwadkzXrznmMpZd5ek6PA", "name": "Channel 3"}
]

def subscribe_to_websub(uc_id):
    print(f"Sending WebSub request for {uc_id}...")
    hub_url = "https://pubsubhubbub.appspot.com/subscribe"
    callback_url = "https://goddess-yt-assistant-production-b575.up.railway.app/api/youtube-webhook"
    topic_url = f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={uc_id}"
    
    data = {
        "hub.callback": callback_url,
        "hub.topic": topic_url,
        "hub.verify": "async",
        "hub.mode": "subscribe"
    }
    
    encoded_data = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(hub_url, data=encoded_data, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"📡 Success! Google Hub responded for {uc_id} with status: {resp.status}")
    except Exception as e:
        print(f"❌ Failed to subscribe {uc_id}: {e}")

def main():
    print("--- 1. Initializing & Patching Database ---")
    init_db()  # Ensures the tables actually exist first
    
    # Force patch the missing columns so the e3q8 error never happens again
    with engine.begin() as conn:
        try:
            conn.execute(text("ALTER TABLE streamers ADD COLUMN linked_primary_id INTEGER;"))
            print("✅ Patched: Added 'linked_primary_id' column")
        except Exception:
            pass  # Fails silently if it already exists
        try:
            conn.execute(text("ALTER TABLE streamers ADD COLUMN sync_settings BOOLEAN DEFAULT 1;"))
            print("✅ Patched: Added 'sync_settings' column")
        except Exception:
            pass  # Fails silently if it already exists

    print("\n--- 2. Registering Channels ---")
    db = SessionLocal()
    try:
        for ch in channels:
            uc_id = ch["uc_id"]
            
            # Add the channel to your Database
            streamer = db.query(Streamer).filter(Streamer.youtube_channel_id == uc_id).first()
            if not streamer:
                # Generate a unique 6-character sync code for this channel
                sync_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                
                new_streamer = Streamer(
                    youtube_channel_id=uc_id,
                    channel_name=ch["name"],
                    server_sync_code=sync_code,
                    is_active=True
                )
                db.add(new_streamer)
                db.commit()
                print(f"✅ Added {uc_id} to database! (Sync Code: {sync_code})")
            else:
                print(f"⚠️ {uc_id} already exists in DB. Skipping database insert.")
            
            # Force the Google WebSub Registration
            subscribe_to_websub(uc_id)
            print("-" * 40)
            
    except Exception as e:
        print(f"Database Error: {e}")
    finally:
        db.close()
    
    print("Registration Complete!")

if __name__ == "__main__":
    main()