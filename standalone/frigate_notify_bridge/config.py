"""Configuration handling for standalone Frigate Notify Bridge."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Config:
    """Configuration for Frigate Notify Bridge."""

    # Frigate connection
    frigate_url: str = ""
    frigate_username: str = ""
    frigate_password: str = ""

    # MQTT connection
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_topic_prefix: str = "frigate"

    # Push provider: relay, fcm, ntfy, pushover
    push_provider: str = "fcm"

    # Relay configuration (PUSH_PROVIDER=relay)
    relay_url: str = ""
    relay_bridge_id: str = ""
    relay_bridge_secret: str = ""
    relay_e2e_key: str = ""  # 32-byte AES key, hex-encoded

    # FCM configuration
    fcm_credentials_file: str = "/config/firebase-credentials.json"
    fcm_credentials: dict[str, Any] = field(default_factory=dict)

    # ntfy configuration
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""

    # Pushover configuration
    pushover_user_key: str = ""
    pushover_api_token: str = ""

    # Server configuration
    api_port: int = 8199
    external_url: str = ""

    # Admin token for bridge management API (separate from device API tokens)
    admin_token: str = ""

    # Data directory
    data_dir: Path = field(default_factory=lambda: Path("/config"))

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors."""
        errors = []

        if not self.frigate_url:
            errors.append("FRIGATE_URL is required")

        if not self.mqtt_host:
            errors.append("MQTT_HOST is required")

        if self.push_provider == "relay":
            if not self.relay_url:
                errors.append("RELAY_URL is required for relay provider")
            if not self.relay_bridge_id:
                errors.append("RELAY_BRIDGE_ID is required for relay provider")
            if not self.relay_bridge_secret:
                errors.append("RELAY_BRIDGE_SECRET is required for relay provider")
            if not self.relay_e2e_key:
                errors.append("RELAY_E2E_KEY is required for relay provider")
            elif len(bytes.fromhex(self.relay_e2e_key)) != 32:
                errors.append("RELAY_E2E_KEY must be a 64-character hex string (32 bytes)")
        elif self.push_provider == "fcm":
            if not self.fcm_credentials and not Path(self.fcm_credentials_file).exists():
                errors.append(
                    f"FCM credentials file not found: {self.fcm_credentials_file}"
                )
        elif self.push_provider == "ntfy":
            if not self.ntfy_topic:
                errors.append("NTFY_TOPIC is required for ntfy provider")
        elif self.push_provider == "pushover":
            if not self.pushover_user_key or not self.pushover_api_token:
                errors.append(
                    "PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN are required"
                )

        return errors

    @property
    def relay_e2e_key_bytes(self) -> bytes:
        """Return relay E2E key as raw bytes."""
        return bytes.fromhex(self.relay_e2e_key) if self.relay_e2e_key else b""


def load_config() -> Config:
    """Load configuration from environment variables."""
    config = Config(
        # Frigate
        frigate_url=os.environ.get("FRIGATE_URL", "").rstrip("/"),
        frigate_username=os.environ.get("FRIGATE_USERNAME", ""),
        frigate_password=os.environ.get("FRIGATE_PASSWORD", ""),
        # MQTT
        mqtt_host=os.environ.get("MQTT_HOST", ""),
        mqtt_port=int(os.environ.get("MQTT_PORT", "1883")),
        mqtt_username=os.environ.get("MQTT_USERNAME", ""),
        mqtt_password=os.environ.get("MQTT_PASSWORD", ""),
        mqtt_topic_prefix=os.environ.get("MQTT_TOPIC_PREFIX", "frigate"),
        # Push provider
        push_provider=os.environ.get("PUSH_PROVIDER", "fcm").lower(),
        # Relay
        relay_url=os.environ.get("RELAY_URL", "").rstrip("/"),
        relay_bridge_id=os.environ.get("RELAY_BRIDGE_ID", ""),
        relay_bridge_secret=os.environ.get("RELAY_BRIDGE_SECRET", ""),
        relay_e2e_key=os.environ.get("RELAY_E2E_KEY", ""),
        # FCM
        fcm_credentials_file=os.environ.get(
            "FCM_CREDENTIALS_FILE", "/config/firebase-credentials.json"
        ),
        # ntfy
        ntfy_url=os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=os.environ.get("NTFY_TOPIC", ""),
        ntfy_token=os.environ.get("NTFY_TOKEN", ""),
        # Pushover
        pushover_user_key=os.environ.get("PUSHOVER_USER_KEY", ""),
        pushover_api_token=os.environ.get("PUSHOVER_API_TOKEN", ""),
        # Server
        api_port=int(os.environ.get("API_PORT", "8199")),
        external_url=os.environ.get("EXTERNAL_URL", ""),
        # Admin
        admin_token=os.environ.get("ADMIN_TOKEN", ""),
        # Data
        data_dir=Path(os.environ.get("DATA_DIR", "/config")),
    )

    # Load FCM credentials from file if using FCM
    if config.push_provider == "fcm":
        creds_path = Path(config.fcm_credentials_file)
        if creds_path.exists():
            with open(creds_path) as f:
                config.fcm_credentials = json.load(f)

    # Validate
    errors = config.validate()
    if errors:
        import sys
        print("Configuration errors:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)

    return config
