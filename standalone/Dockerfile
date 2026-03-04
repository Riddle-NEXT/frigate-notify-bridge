# Frigate Notify Bridge - Standalone Docker Image
# For users without Home Assistant

FROM python:3.11-slim

LABEL maintainer="Frigate Mobile" \
      description="Push notification bridge for Frigate NVR" \
      version="0.1.0"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

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
    FCM_CREDENTIALS_FILE="/config/firebase-credentials.json" \
    NTFY_URL="https://ntfy.sh" \
    NTFY_TOPIC="" \
    NTFY_TOKEN="" \
    PUSHOVER_USER_KEY="" \
    PUSHOVER_API_TOKEN="" \
    API_PORT="8199" \
    LOG_LEVEL="INFO"

# Expose API port
EXPOSE 8199

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8199/health')" || exit 1

# Run the standalone server
CMD ["python", "-m", "frigate_notify_bridge.main"]
