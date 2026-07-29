import re

with open("main.py", "r") as f:
    content = f.read()

join_logic = """
@app.post("/api/bot/join")
async def bot_join_channel(request: Request, db: Session = Depends(get_db)):
    \"\"\"Deploys the bot to the creator's channel as a moderator.\"\"\"
    streamer_id = request.session.get("streamer_id")
    if not streamer_id:
        return RedirectResponse(url="/", status_code=303)

    streamer = db.query(Streamer).filter(Streamer.id == streamer_id).first()
    if not streamer:
        return RedirectResponse(url="/?error=invalid_channel", status_code=303)

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        return RedirectResponse(url="/?error=missing_api_key", status_code=303)

    safe_channel_name = urllib.parse.quote(streamer.channel_name)
    search_url = (
        f"https://www.googleapis.com/youtube/v3/search?"
        f"part=snippet&q={safe_channel_name}&eventType=live&type=video&key={api_key}"
    )

    try:
        def fetch_live_stream():
            try:
                with urllib.request.urlopen(search_url) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as e:
                raise Exception(f"Google API Rejected Request: {e.code}")

        data = await asyncio.to_thread(fetch_live_stream)

        if "items" in data and len(data["items"]) > 0:
            video_id = data["items"][0]["id"]["videoId"]
            DETECTED_VIDEOS.add(video_id)
            return RedirectResponse(url="/?success=join_success", status_code=303)
        else:
            return RedirectResponse(url="/?error=join_not_live", status_code=303)

    except Exception as e:
        print(f"[/api/bot/join ERROR] {e}")
        return RedirectResponse(url="/?error=api_crash", status_code=303)

# ---------------------------------------------------------
"""

content = content.replace('# ---------------------------------------------------------\n# OBS WEBSOCKET & WIDGET ROUTES\n# ---------------------------------------------------------', join_logic + '# OBS WEBSOCKET & WIDGET ROUTES\n# ---------------------------------------------------------')

with open("main.py", "w") as f:
    f.write(content)
