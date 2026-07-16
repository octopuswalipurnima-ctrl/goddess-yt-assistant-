from fastapi import WebSocket

class OverlayManager:
    def __init__(self):
        # Stores active OBS connections: { "sync_code": [WebSocket1, WebSocket2] }
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, sync_code: str):
        await websocket.accept()
        if sync_code not in self.active_connections:
            self.active_connections[sync_code] = []
        self.active_connections[sync_code].append(websocket)
        print(f"[OBS CONNECTED] Overlay active for Streamer Code: {sync_code}")

    def disconnect(self, websocket: WebSocket, sync_code: str):
        if sync_code in self.active_connections:
            self.active_connections[sync_code].remove(websocket)
            if not self.active_connections[sync_code]:
                del self.active_connections[sync_code]
        print(f"[OBS DISCONNECTED] Streamer Code: {sync_code}")

    async def send_alert(self, sync_code: str, data: dict):
        if sync_code in self.active_connections:
            for connection in self.active_connections[sync_code]:
                try:
                    await connection.send_json(data)
                except Exception as e:
                    print(f"[WEBSOCKET ERROR] {e}")

# Singleton instance to be used across the app
overlay_manager = OverlayManager()