#!/bin/bash
# Register @viber:samprim.net on Synapse and obtain an access token.
# Run on the DS218 over SSH.
#
# Usage:  sudo bash register-viber-user.sh <password>

set -e

PASSWORD="${1:?usage: register-viber-user.sh <password>}"
HOMESERVER_YAML="/volume1/docker/matrix-homeserver/synapse/homeserver.yaml"
HOMESERVER_URL="http://localhost:8008"
PUBLIC_URL="https://matrix.samprim.net"

echo "[1/2] Registering @viber on Synapse..."
sudo docker exec -i synapse register_new_matrix_user \
    -u viber -p "$PASSWORD" --no-admin \
    -c /data/homeserver.yaml "$HOMESERVER_URL" || echo "(user may already exist, continuing)"

echo "[2/2] Requesting access token via public endpoint..."
TOKEN_JSON=$(curl -sS -XPOST \
    -H 'Content-Type: application/json' \
    -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"viber\"},\"password\":\"$PASSWORD\",\"initial_device_display_name\":\"viber-bridge\"}" \
    "$PUBLIC_URL/_matrix/client/v3/login")

ACCESS_TOKEN=$(echo "$TOKEN_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
DEVICE_ID=$(echo "$TOKEN_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['device_id'])")
USER_ID=$(echo "$TOKEN_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['user_id'])")

echo ""
echo "========================================================"
echo "  Save these into scripts/config.yaml on Windows:"
echo "========================================================"
echo "  user_id:      $USER_ID"
echo "  access_token: $ACCESS_TOKEN"
echo "  device_id:    $DEVICE_ID"
echo "========================================================"
