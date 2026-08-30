# Goddess YouTube Assistant

Chat-first YouTube moderation, queue, economy, store, Gemini moderation/co-host, and optional Discord event delivery.

## Production runbook

1. In Discord Developer Portal, revoke and regenerate any bot token previously posted in chat. Put the replacement only in Railway's `DISCORD_BOT_TOKEN` variable; never paste it into source, issues, or logs.
2. Configure `.env` from `.env.example` locally, or set the same variables in Railway. Set `SESSION_SECRET`, YouTube OAuth values, at least one `GEMINI_API_KEY_#`, and optionally `DISCORD_LOG_CHANNEL_ID`.
3. Run `python -m migrations.runner`, then `uvicorn main:app --host 0.0.0.0 --port $PORT`. Railway does this automatically through `railway.json`.
4. Verify `GET /healthz` returns `{"status":"ok"}`. The Discord bot is optional: its absence does not prevent YouTube processing.
5. Use the owner-only emergency-stop endpoint to halt outbound chat/moderation actions during an incident; clear it only after review. It is audit logged.

Dashboard access uses `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, and `SESSION_SECRET`; it does not use Google OAuth. Generate the bcrypt hash locally and put only the result in Railway:

```powershell
python -c "import bcrypt; print(bcrypt.hashpw(b'choose-a-strong-password', bcrypt.gensalt()).decode())"
```

Set `BASE_URL` to the public Railway application URL for the existing WebSub callback at `/api/youtube-webhook`. Configure the creator's UC-style YouTube channel ID from the dashboard before enabling WebSub.

Discord events use the configured bot and channel IDs, are queued with bounded backpressure, and are dropped rather than blocking YouTube processing during Discord outages. Per-stream `discord_log_channel_id` takes precedence over `DISCORD_LOG_CHANNEL_ID`.

## Test policy

Run `python -m unittest discover -s tests -v` with dependencies from `requirements.txt`. Tests use mocks and never contact YouTube, Gemini, or Discord. Live verification requires explicitly configured credentials plus a private/test YouTube stream and a non-production Discord channel.
