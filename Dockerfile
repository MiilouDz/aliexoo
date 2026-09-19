# syntax=docker/dockerfile:1
FROM python:3.12-slim

# ca-certificates: needed for HTTPS calls to Telegram/AliExpress.
# Everything else stays out of the image on purpose (no build tools —
# every dependency here ships prebuilt wheels).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY aliexpress_common.py aliexpress_service.py telethon_login.py ./

# Default (local/relative) paths for the two files that must survive a
# restart — the Telethon login session and the per-channel catch-up
# state. On Fly.io these are pointed at the mounted volume instead via
# fly.toml's [env] block (e.g. TELETHON_SESSION=/data/monitor).
ENV TELETHON_SESSION=monitor \
    STATE_FILE=channel_state.json \
    PYTHONUNBUFFERED=1

# This is a background worker with no HTTP port to expose — it polls
# Telegram outbound, it doesn't get called inbound.
CMD ["python", "aliexpress_service.py"]
