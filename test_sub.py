"""Optional local registration helper; Railway performs this during startup."""
from app.database.connection import SessionLocal
from app.services.youtube.monitored_channels import ensure_monitored_channels
from migrations.runner import run


def main():
    run()
    db = SessionLocal()
    try:
        streamers = ensure_monitored_channels(db)
        print(f"Registered {len(streamers)} monitored channels.")
        print("Restart the Railway service to apply WebSub subscriptions through its configured BASE_URL.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
