import os
import asyncio
from datetime import datetime
from app.services.websocket import overlay_manager # Reusing existing pipeline

class LocalClipManager:
    def __init__(self):
        self.clip_directory = "./static/clips"
        os.makedirs(self.clip_directory, exist_ok=True)

    async def trigger_clip(self, streamer_id: int, sync_code: str, duration: int = 60, source: str = "Dashboard"):
        """
        Signals the connected OBS instance to save the rolling replay buffer.
        Assumes OBS Replay Buffer is active.
        """
        # Send a secure WebSocket command to the streamer's OBS Browser Source / Plugin
        clip_command = {
            "type": "system_command",
            "action": "save_replay_buffer",
            "metadata": {
                "duration": duration,
                "trigger": source,
                "timestamp": datetime.utcnow().isoformat()
            }
        }
        await overlay_manager.send_alert(sync_code, clip_command)
        
        # Note: In a full local setup, OBS saves the file locally. 
        # A lightweight background watcher (or OBS post-process script) would ping the bot 
        # to update the `ClipRecord` database entry with the final file path.
        return {"status": "triggered", "message": f"Clip requested via {source}"}