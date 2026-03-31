# Frigate Notify Bridge - Standalone Docker Image
# For users without Home Assistant
#
# Build from the frigate-notify-bridge/ root directory:
#   docker build -f standalone/Dockerfile .

FROM python:3.11-slim

LABEL maintainer="Frigate Mobile" \
      description="Push notification bridge for Frigate NVR" \
      version="0.2.0"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY standalone/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared core library (no HA dependency)
COPY frigate_notify_bridge_core/ ./frigate_notify_bridge_core/

# Copy standalone application code
COPY standalone/ .

# Create config directory
RUN mkdir -p /config

# Environment variables (with defaults)
ENV FRIGATE_URL="" \
    FRIGATE_USERNAME="" \
    FRIGATE_PASSWORD="" \
    MQTT_HOST="" \
    MQTT_PORT="1883" \
    MQTT_USERNAME="" \
    MQTT_PASSWORD="" \
    MQTT_TOPIC_PREFIX="frigate" \
    PUSH_PROVIDER="fcm" \
    # FCM (direct, no relay)
    FCM_CREDENTIALS_FILE="/config/firebase-credentials.json" \
    # Relay with E2E encryption (recommended for Frigate Mobile users)
    RELAY_URL="" \
    RELAY_BRIDGE_ID="" \
    RELAY_BRIDGE_SECRET="" \
    RELAY_E2E_KEY="" \
    # ntfy (alternative)
    NTFY_URL="https://ntfy.sh" \
    NTFY_TOPIC="" \
    NTFY_TOKEN="" \
    # Pushover (alternative)
    PUSHOVER_USER_KEY="" \
    PUSHOVER_API_TOKEN="" \
    # Server
    API_PORT="8199" \
    EXTERNAL_URL="" \
    # Admin token for bridge management API (Bridge Manager screen)
    ADMIN_TOKEN="" \
    LOG_LEVEL="INFO"

# Expose API port
EXPOSE 8199

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8199/health')" || exit 1

# Run the standalone server
CMD ["python", "-m", "frigate_notify_bridge.main"]
