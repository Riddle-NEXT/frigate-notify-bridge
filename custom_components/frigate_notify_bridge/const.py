"""Constants for Frigate Notify Bridge integration."""
from typing import Final

DOMAIN: Final = "frigate_notify_bridge"
MANUFACTURER: Final = "Frigate Mobile"

# Configuration keys
CONF_FRIGATE_URL: Final = "frigate_url"
CONF_FRIGATE_USERNAME: Final = "frigate_username"
CONF_FRIGATE_PASSWORD: Final = "frigate_password"
CONF_MQTT_HOST: Final = "mqtt_host"
CONF_MQTT_PORT: Final = "mqtt_port"
CONF_MQTT_USERNAME: Final = "mqtt_username"
CONF_MQTT_PASSWORD: Final = "mqtt_password"
CONF_MQTT_TOPIC_PREFIX: Final = "mqtt_topic_prefix"
CONF_USE_HA_MQTT: Final = "use_ha_mqtt"
CONF_USE_FRIGATE_INTEGRATION: Final = "use_frigate_integration"
CONF_HOME_SSIDS: Final = "home_ssids"
CONF_DEBUG_LOGGING: Final = "debug_logging"

# Push provider configuration
CONF_PUSH_PROVIDER: Final = "push_provider"
CONF_FCM_CREDENTIALS: Final = "fcm_credentials"
CONF_FCM_PROJECT_ID: Final = "fcm_project_id"
CONF_FCM_SETUP_METHOD: Final = "fcm_setup_method"
CONF_FIREBASE_PROJECT: Final = "firebase_project"
CONF_NTFY_URL: Final = "ntfy_url"
CONF_NTFY_TOPIC: Final = "ntfy_topic"
CONF_NTFY_TOKEN: Final = "ntfy_token"
CONF_PUSHOVER_USER_KEY: Final = "pushover_user_key"
CONF_PUSHOVER_API_TOKEN: Final = "pushover_api_token"

# FCM setup methods
FCM_SETUP_OAUTH: Final = "oauth"
FCM_SETUP_MANUAL: Final = "manual"

# FCM API
FCM_SEND_URL: Final = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
GOOGLE_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
FCM_SCOPE: Final = "https://www.googleapis.com/auth/firebase.messaging"
FCM_TOKEN_CACHE_BUFFER_SECONDS: Final = 300  # Refresh 5 min early

# Google OAuth — authorization code flow via relay page
GOOGLE_AUTH_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_REDIRECT_URI: Final = (
    "https://lowkeynext.github.io/frigate-notify-bridge/callback"
)
GOOGLE_OAUTH_SCOPES: Final = [
    "https://www.googleapis.com/auth/cloud-platform",
]
GOOGLE_REVOKE_URL: Final = "https://oauth2.googleapis.com/revoke"
# Embedded OAuth client ID (public, not secret — used to initiate the auth flow)
GOOGLE_CLIENT_ID: Final = (
    "732144175760-gsf70tipdiou8mfo4vicf323fla8jtpu.apps.googleusercontent.com"
)

# Google Cloud Resource Manager API
GCP_CREATE_PROJECT_URL: Final = (
    "https://cloudresourcemanager.googleapis.com/v3/projects"
)
GCP_GET_PROJECT_URL: Final = (
    "https://cloudresourcemanager.googleapis.com/v3/projects/{project_id}"
)
GCP_OPERATIONS_URL: Final = (
    "https://cloudresourcemanager.googleapis.com/v3/{operation_name}"
)

# Firebase Management API
FIREBASE_LIST_PROJECTS_URL: Final = "https://firebase.googleapis.com/v1beta1/projects"
FIREBASE_ADD_URL: Final = (
    "https://firebase.googleapis.com/v1beta1/projects/{project_id}:addFirebase"
)
FIREBASE_OPERATIONS_URL: Final = (
    "https://firebase.googleapis.com/v1beta1/{operation_name}"
)

# Google IAM API
IAM_CREATE_SA_URL: Final = (
    "https://iam.googleapis.com/v1/projects/{project_id}/serviceAccounts"
)
IAM_CREATE_KEY_URL: Final = (
    "https://iam.googleapis.com/v1/projects/{project_id}"
    "/serviceAccounts/{sa_email}/keys"
)
IAM_SA_EMAIL_TEMPLATE: Final = "{sa_name}@{project_id}.iam.gserviceaccount.com"

# Service Usage API (enable FCM)
SERVICE_USAGE_ENABLE_URL: Final = (
    "https://serviceusage.googleapis.com/v1/projects/{project_id}"
    "/services/{service_name}:enable"
)
FCM_SERVICE_NAME: Final = "fcm.googleapis.com"
IAM_SERVICE_NAME: Final = "iam.googleapis.com"

# Service account defaults
FCM_SA_NAME: Final = "frigate-notify-bridge"
FCM_SA_DISPLAY_NAME: Final = "Frigate Notify Bridge"

# Push providers
PUSH_PROVIDER_FCM: Final = "fcm"
PUSH_PROVIDER_NTFY: Final = "ntfy"
PUSH_PROVIDER_PUSHOVER: Final = "pushover"

PUSH_PROVIDERS: Final = [
    PUSH_PROVIDER_FCM,
    PUSH_PROVIDER_NTFY,
    PUSH_PROVIDER_PUSHOVER,
]

# Default values
DEFAULT_MQTT_PORT: Final = 1883
DEFAULT_MQTT_TOPIC_PREFIX: Final = "frigate"
DEFAULT_NTFY_URL: Final = "https://ntfy.sh"

# Pairing
PAIRING_TOKEN_EXPIRY_SECONDS: Final = 600  # 10 minutes
PAIRING_CODE_LENGTH: Final = 6
QR_CODE_VERSION: Final = 3

# API paths
API_BASE_PATH: Final = "/api/frigate_notify_bridge"
API_PAIR_PATH: Final = f"{API_BASE_PATH}/pair"
API_DEVICES_PATH: Final = f"{API_BASE_PATH}/devices"
API_CONFIG_PATH: Final = f"{API_BASE_PATH}/config"
API_STATUS_PATH: Final = f"{API_BASE_PATH}/status"
API_QR_PATH: Final = f"{API_BASE_PATH}/pairing/qr"
API_FRIGATE_PROXY_PATH: Final = f"{API_BASE_PATH}/frigate"

# MQTT Topics
MQTT_EVENTS_TOPIC: Final = "events"
MQTT_REVIEWS_TOPIC: Final = "reviews"

# Event types to notify on
NOTIFY_EVENT_TYPES: Final = ["new", "update", "end"]

# Notification defaults
DEFAULT_NOTIFICATION_TITLE: Final = "Frigate Alert"
DEFAULT_COOLDOWN_SECONDS: Final = 60

# Storage keys
STORAGE_KEY: Final = f"{DOMAIN}.storage"
STORAGE_VERSION: Final = 1

# Signals
SIGNAL_DEVICE_REGISTERED: Final = f"{DOMAIN}_device_registered"
SIGNAL_DEVICE_REMOVED: Final = f"{DOMAIN}_device_removed"
SIGNAL_DEVICE_UPDATED: Final = f"{DOMAIN}_device_updated"
SIGNAL_CONFIG_UPDATED: Final = f"{DOMAIN}_config_updated"

# Firebase client configuration (for mobile app dynamic init)
CONF_FIREBASE_CLIENT_CONFIG: Final = "firebase_client_config"

# Firebase Web Apps API
FIREBASE_WEB_APPS_URL: Final = (
    "https://firebase.googleapis.com/v1beta1/projects/{project_id}/webApps"
)
FIREBASE_WEB_APP_CONFIG_URL: Final = (
    "https://firebase.googleapis.com/v1beta1/projects/{project_id}/webApps/{app_id}/config"
)

# Push relay
CONF_RELAY_URL: Final = "relay_url"
CONF_RELAY_BRIDGE_ID: Final = "relay_bridge_id"
CONF_RELAY_BRIDGE_SECRET: Final = "relay_bridge_secret"
CONF_RELAY_E2E_KEY: Final = "relay_e2e_key"
PUSH_PROVIDER_RELAY: Final = "relay"
DEFAULT_RELAY_URL: Final = "https://frigate-mobile.web.app"

# Events
EVENT_DEVICE_PAIRED: Final = f"{DOMAIN}_device_paired"
EVENT_DEVICE_REMOVED: Final = f"{DOMAIN}_device_removed"

# Platforms
PLATFORMS: Final = ["binary_sensor", "button", "sensor", "switch"]

# Per-device entity constants
DEVICE_ONLINE_THRESHOLD_MINUTES: Final = 5
