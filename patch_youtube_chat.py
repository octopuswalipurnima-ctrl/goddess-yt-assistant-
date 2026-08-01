import re

with open("app/bot/youtube_chat.py", "r") as f:
    content = f.read()

# Replace active_streams logic
content = content.replace('self.active_streams[stream_key] = chat_id',
                          'self.active_streams[stream_key] = {"chat_id": chat_id, "start_time": datetime.now(timezone.utc)}')

content = content.replace('chat_id = self.active_streams[streamer_key]',
                          'chat_info = self.active_streams[streamer_key]\n                    chat_id = chat_info["chat_id"] if isinstance(chat_info, dict) else chat_info')

# Add cap check logic
cap_logic = """
                    try:
                        # API Cap Check
                        sys_state = db.query(SystemState).first()
                        if sys_state and sys_state.youtube_api_calls >= sys_state.youtube_api_cap:
                            print(f"[API CAP] YouTube API cap reached ({sys_state.youtube_api_calls}/{sys_state.youtube_api_cap}). Skipping fetch.")
                            continue

                        response = self.youtube.liveChatMessages().list(liveChatId=chat_id, part="snippet,authorDetails", pageToken=token).execute()

                        if sys_state:
                            sys_state.youtube_api_calls += 1
                            db.commit()
"""
content = content.replace("""
                    try:
                        response = self.youtube.liveChatMessages().list(liveChatId=chat_id, part="snippet,authorDetails", pageToken=token).execute()""", cap_logic)

with open("app/bot/youtube_chat.py", "w") as f:
    f.write(content)
