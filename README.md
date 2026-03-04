# Frigate Notify Bridge

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/LowkeyNEXT/frigate-notify-bridge.svg?style=for-the-badge)](https://github.com/LowkeyNEXT/frigate-notify-bridge/releases)
[![License](https://img.shields.io/github/license/LowkeyNEXT/frigate-notify-bridge.svg?style=for-the-badge)](LICENSE)

Push notification bridge for [Frigate NVR](https://frigate.video/) and the [Frigate Mobile](https://github.com/LowkeyNEXT/FrigateMobile) app.

This project enables push notifications from Frigate events to your mobile device, with support for:
- **Push Relay (Recommended)** - Easiest setup, handles credentials for you
- **Firebase Cloud Messaging (FCM)** - Advanced, self-hosted push notifications
- **ntfy** - Self-hostable, open-source alternative
- **Pushover** - Simple, one-time purchase service

## Features

- **QR Code Pairing** - Easy device setup by scanning a QR code
- **Home Assistant Integration** - Full integration via HACS with config flow UI
- **Standalone Mode** - Docker container for users without Home Assistant
- **Nabu Casa Cloud Support** - Automatic remote access via Home Assistant Cloud
- **WebRTC Relay** - Use Nabu Casa's TURN servers for video streaming
- **Multi-device Support** - Send notifications to multiple paired devices
- **Per-device Filtering** - Configure cameras, labels, and zones per device
- **Cooldown Support** - Prevent notification spam with configurable cooldowns

## Installation

### Option 1: Home Assistant Integration (Recommended)

The easiest way to use Frigate Notify Bridge is as a Home Assistant custom integration.

#### Quick Install

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=LowkeyNEXT&repository=frigate-notify-bridge&category=integration)

Or follow the manual steps below.

#### Prerequisites
- Home Assistant 2024.1.0 or newer
- [HACS](https://hacs.xyz/) installed
- MQTT integration configured (recommended)
- Frigate integration (optional, for automatic discovery)

#### Install via HACS

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots menu → "Custom repositories"
4. Add this repository URL: `https://github.com/LowkeyNEXT/frigate-notify-bridge`
5. Select category: "Integration"
6. Click "Add"
7. Search for "Frigate Notify Bridge" and install
8. Restart Home Assistant

#### Manual Installation

1. Download the latest release from the [Releases](https://github.com/LowkeyNEXT/frigate-notify-bridge/releases) page
2. Extract the `custom_components/frigate_notify_bridge` folder
3. Copy it to your Home Assistant `config/custom_components/` directory
4. Restart Home Assistant

#### Configuration

1. Go to Settings → Devices & Services → Add Integration
2. Search for "Frigate Notify Bridge"
3. Follow the setup wizard:
   - Configure Frigate connection (or use existing Frigate integration)
   - Configure MQTT (or use existing HA MQTT)
   - Choose push notification provider
   - Set up relay or Firebase/ntfy/Pushover credentials

### Option 2: Standalone Docker (Without Home Assistant)

For users without Home Assistant, run the bridge as a standalone Docker container.

#### Using Pre-built Image

```yaml
services:
  frigate-notify-bridge:
    image: ghcr.io/LowkeyNEXT/frigate-notify-bridge:latest
    container_name: frigate-notify-bridge
    restart: unless-stopped
    ports:
      - "8199:8199"
    environment:
      # Required
      FRIGATE_URL: "http://frigate:5000"
      MQTT_HOST: "mqtt"
      MQTT_PORT: "1883"

      # Push provider (relay, fcm, ntfy, or pushover)
      PUSH_PROVIDER: "relay"
      RELAY_URL: "https://push.frigate-mobile.app"
      # Bridge ID, Secret, and E2E key must be generated or provided
      RELAY_BRIDGE_ID: "your_bridge_id"
      RELAY_BRIDGE_SECRET: "your_bridge_secret"
      RELAY_E2E_KEY: "your_e2e_encryption_key"

    volumes:
      - ./config:/config
```

See [`standalone/docker-compose.example.yml`](standalone/docker-compose.example.yml) for full configuration options.

#### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FRIGATE_URL` | Yes | - | Frigate API URL (e.g., `http://frigate:5000`) |
| `FRIGATE_USERNAME` | No | - | Frigate username (if auth enabled) |
| `FRIGATE_PASSWORD` | No | - | Frigate password |
| `MQTT_HOST` | Yes | - | MQTT broker hostname |
| `MQTT_PORT` | No | `1883` | MQTT broker port |
| `MQTT_USERNAME` | No | - | MQTT username |
| `MQTT_PASSWORD` | No | - | MQTT password |
| `MQTT_TOPIC_PREFIX` | No | `frigate` | Frigate MQTT topic prefix |
| `PUSH_PROVIDER` | No | `relay` | Push provider: `relay`, `fcm`, `ntfy`, or `pushover` |
| `RELAY_URL` | relay | `https://push.frigate-mobile.app` | Push relay service URL |
| `RELAY_BRIDGE_ID` | relay | - | Unique bridge ID |
| `RELAY_BRIDGE_SECRET` | relay | - | Bridge authentication secret |
| `RELAY_E2E_KEY` | relay | - | End-to-end encryption key |
| `FCM_CREDENTIALS_FILE` | FCM | `/config/firebase-credentials.json` | Path to Firebase credentials |
| `NTFY_URL` | ntfy | `https://ntfy.sh` | ntfy server URL |
| `NTFY_TOPIC` | ntfy | - | ntfy topic name |
| `NTFY_TOKEN` | No | - | ntfy access token |
| `PUSHOVER_USER_KEY` | Pushover | - | Pushover user key |
| `PUSHOVER_API_TOKEN` | Pushover | - | Pushover API token |
| `API_PORT` | No | `8199` | API server port |
| `EXTERNAL_URL` | No | - | External URL for remote access |
| `LOG_LEVEL` | No | `INFO` | Logging level |

## Push Provider Setup

### Push Relay (Recommended)

The Push Relay is the easiest way to get notifications. It securely forwards encrypted notifications from your bridge to your iOS devices. The Frigate Mobile app handles all Firebase credentials automatically so you don't have to set up a Google Cloud project yourself.

- **Relay URL:** `https://push.frigate-mobile.app`
- **Security:** All notification content is end-to-end encrypted before leaving your network. The relay cannot read your camera names, event labels, or snapshots.
- **Ease of Use:** When using the Home Assistant integration, a unique Bridge ID, Bridge Secret, and E2E Encryption Key are generated for you automatically.

**Configuration:**
```yaml
PUSH_PROVIDER: "relay"
RELAY_URL: "https://push.frigate-mobile.app"
RELAY_BRIDGE_ID: "..."
RELAY_BRIDGE_SECRET: "..."
RELAY_E2E_KEY: "..."
```

### Advanced: Self-hosted Options

If you prefer to manage your own notification infrastructure, you can use these providers.

#### Firebase Cloud Messaging (FCM)

FCM provides reliable, fast push notifications through Google's infrastructure.

1. Go to [Firebase Console](https://console.firebase.google.com/)
2. Create a new project
3. Go to Project Settings → Service Accounts
4. Click "Generate new private key"
5. Save the JSON file and provide it to the bridge

#### ntfy (Self-Hostable)

[ntfy](https://ntfy.sh/) is an open-source notification service you can self-host.

1. Choose a topic name (e.g., `my-frigate-alerts`)
2. Optionally set up your own ntfy server

**Configuration:**
```yaml
PUSH_PROVIDER: "ntfy"
NTFY_URL: "https://ntfy.sh"
NTFY_TOPIC: "my-frigate-alerts"
```

#### Pushover

[Pushover](https://pushover.net/) is a simple notification service with a one-time $5 purchase.

1. Create account at [pushover.net](https://pushover.net/)
2. Get your User Key and create an application for an API Token

## Mobile App Pairing

### QR Code Pairing (Recommended)

1. In Home Assistant, go to the Frigate Notify Bridge integration
2. Click "Generate Pairing Code"
3. In the Frigate Mobile app, tap "Scan QR Code" during setup
4. Scan the QR code displayed in Home Assistant

The QR code contains:
- Server connection details (internal and external URLs)
- Push notification configuration
- Pairing authentication token

### Manual Pairing

If QR code scanning isn't available:

1. Generate a pairing code in the integration
2. In the mobile app, choose "Enter Code Manually"
3. Enter the 6-character code and your server URL

## Troubleshooting

### Notifications not received

1. Check that MQTT is connected and receiving Frigate events
2. Verify push provider credentials are correct
3. Check device has a valid push token in the app settings
4. Review Home Assistant logs for errors

### Relay errors

- **Relay unreachable:** It's normal to see a warning about the relay being unreachable before you've finished the initial configuration and created a config entry.
- **Pairing fails:** Ensure your Home Assistant instance is accessible from the internet or you are on the same local network as your phone.

### QR code not scanning

1. Ensure good lighting and camera focus
2. Try the "Enter Code Manually" option
3. Check that the bridge API is accessible

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Related Projects

- [Frigate NVR](https://frigate.video/) - AI-powered NVR
- [Frigate Mobile](https://github.com/LowkeyNEXT/FrigateMobile) - iOS app for Frigate
- [Home Assistant](https://www.home-assistant.io/) - Home automation platform
- [HACS](https://hacs.xyz/) - Home Assistant Community Store
