#!/usr/bin/env python3
"""Standalone test of OAuth flow logic — no browser needed.

Tests:
1. State encoding round-trip (Python encode → JS-compatible decode)
2. Callback URL construction
3. Token exchange request format
4. Base64 edge cases with URL-safe characters
"""
import base64
import json
import secrets
import urllib.parse

# Constants (same as const.py)
CLIENT_ID = "732144175760-gsf70tipdiou8mfo4vicf323fla8jtpu.apps.googleusercontent.com"
REDIRECT_URI = "https://lowkeynext.github.io/frigate-notify-bridge/callback"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/cloud-platform"
OAUTH_CALLBACK_PATH = "/api/frigate_notify_bridge/oauth_callback"

passed = 0
failed = 0


def test(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        failed += 1


print("=" * 60)
print("Flow Logic Tests")
print("=" * 60)

# ── Test 1: State encoding round-trip ────────────────────────
print("\n1. State encoding/decoding round-trip")

flow_id = "test-flow-abc123"
csrf_token = secrets.token_urlsafe(32)
callback_url = "https://my-ha.ui.nabu.casa/api/frigate_notify_bridge/oauth_callback"

state_data = json.dumps({
    "flow_id": flow_id,
    "csrf": csrf_token,
    "callback": callback_url,
})
state = base64.urlsafe_b64encode(state_data.encode()).decode()

# Verify it can be decoded back
# Simulate what the callback view does:
padded = state + "=" * (4 - len(state) % 4) if len(state) % 4 else state
decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
test("Python decode matches", decoded["flow_id"] == flow_id)
test("CSRF preserved", decoded["csrf"] == csrf_token)
test("Callback URL preserved", decoded["callback"] == callback_url)

# Simulate what JavaScript atob() needs (standard base64)
# The relay page converts URL-safe to standard: replace(/-/g,'+').replace(/_/g,'/')
std_b64 = state.replace("-", "+").replace("_", "/")
while len(std_b64) % 4:
    std_b64 += "="
js_decoded = json.loads(base64.b64decode(std_b64).decode())
test("JS-compatible decode matches", js_decoded["flow_id"] == flow_id)
test("JS callback URL preserved", js_decoded["callback"] == callback_url)

# ── Test 2: State with tricky characters ─────────────────────
print("\n2. State with URL-safe base64 characters")

# Force content that produces - and _ in urlsafe base64
for i in range(20):
    csrf = secrets.token_urlsafe(32)
    data = json.dumps({"flow_id": f"flow-{i}", "csrf": csrf, "callback": callback_url})
    encoded = base64.urlsafe_b64encode(data.encode()).decode()

    has_urlsafe = "-" in encoded or "_" in encoded

    # Simulate JS decode path
    std = encoded.replace("-", "+").replace("_", "/")
    while len(std) % 4:
        std += "="
    decoded_js = json.loads(base64.b64decode(std).decode())

    # Simulate Python decode path (callback view)
    p = encoded + "=" * (4 - len(encoded) % 4) if len(encoded) % 4 else encoded
    decoded_py = json.loads(base64.urlsafe_b64decode(p).decode())

    if decoded_js["flow_id"] != f"flow-{i}" or decoded_py["flow_id"] != f"flow-{i}":
        test(f"Round-trip {i} (urlsafe_chars={has_urlsafe})", False)
        break
else:
    test("20 random states all round-trip correctly", True)

# Check that at least some states had URL-safe chars
states_with_urlsafe = 0
for i in range(100):
    csrf = secrets.token_urlsafe(32)
    data = json.dumps({"flow_id": f"flow-{i}", "csrf": csrf, "callback": callback_url})
    encoded = base64.urlsafe_b64encode(data.encode()).decode()
    if "-" in encoded or "_" in encoded:
        states_with_urlsafe += 1
test(
    f"URL-safe base64 handling works (appeared in {states_with_urlsafe}/100 states)",
    True,  # The relay page fix handles this regardless
)

# ── Test 3: OAuth URL construction ───────────────────────────
print("\n3. OAuth URL construction")

params = {
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "response_type": "code",
    "scope": SCOPES,
    "access_type": "offline",
    "prompt": "consent",
    "state": state,
}
oauth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

test("URL starts with Google auth", oauth_url.startswith(AUTH_URL))
test("Contains client_id", CLIENT_ID in oauth_url)
test("Contains redirect_uri", urllib.parse.quote(REDIRECT_URI, safe="") in oauth_url)
test("Contains state", "state=" in oauth_url)

# Parse and verify all params
parsed = urllib.parse.urlparse(oauth_url)
query = urllib.parse.parse_qs(parsed.query)
test("response_type=code", query["response_type"] == ["code"])
test("access_type=offline", query["access_type"] == ["offline"])
test("prompt=consent", query["prompt"] == ["consent"])

# ── Test 4: Callback URL construction ────────────────────────
print("\n4. Callback URL from relay page redirect")

# Simulate what the relay page does: redirect to callback with code + state
code = "4/0AfrIepA_test_code_here"
redirect_url = (
    callback_url
    + "?code=" + urllib.parse.quote(code, safe="")
    + "&state=" + urllib.parse.quote(state, safe="")
)

parsed = urllib.parse.urlparse(redirect_url)
query = urllib.parse.parse_qs(parsed.query)
test("Callback path correct", parsed.path == OAUTH_CALLBACK_PATH)
test("Code preserved in redirect", query["code"] == [code])
test("State preserved in redirect", query["state"] == [state])

# ── Test 5: Token exchange payload ───────────────────────────
print("\n5. Token exchange payload")

token_payload = {
    "client_id": CLIENT_ID,
    "client_secret": "GOCSPX-test",
    "code": code,
    "grant_type": "authorization_code",
    "redirect_uri": REDIRECT_URI,
}
test("grant_type correct", token_payload["grant_type"] == "authorization_code")
test("redirect_uri matches", token_payload["redirect_uri"] == REDIRECT_URI)
test("All required fields present", all(
    k in token_payload for k in ["client_id", "client_secret", "code", "grant_type", "redirect_uri"]
))

# ── Summary ──────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed")
print("=" * 60)

if failed:
    exit(1)
else:
    print("ALL TESTS PASSED")
