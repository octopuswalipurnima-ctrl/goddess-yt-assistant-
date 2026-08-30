from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from app.database.connection import get_db
from app.database.models import RewardItem, StoreRedemption, ViewerProfile

router = APIRouter(prefix="/api/economy", tags=["Creator Economy"])

@router.get("/store/{streamer_id}")
async def get_store_items(streamer_id: int, db: Session = Depends(get_db)):
    """Fetch all active items for a creator's store."""
    items = db.query(RewardItem).filter(
        RewardItem.streamer_id == streamer_id,
        RewardItem.is_active == True
    ).all()
    return {"items": items}

@router.post("/store/redeem")
async def redeem_item(request: Request, item_id: int, user_id: int, db: Session = Depends(get_db)):
    """Handles the purchase logic, deducting coins and verifying limits."""
    # 1. Fetch Item and User Economy Data (Logic omitted for brevity)
    # 2. Check stock and cooldowns
    # 3. Deduct coins and log StoreRedemption
    # Notifications are emitted asynchronously by the chat-command path.
    pass

@router.get("/viewer/{streamer_id}/{user_id}")
async def get_viewer_profile(streamer_id: int, user_id: int, db: Session = Depends(get_db)):
    """Returns deep stream memory profile."""
    profile = db.query(ViewerProfile).filter_by(streamer_id=streamer_id, user_id=user_id).first()
    return profile
