#!/bin/bash
# Create a guaranteed UNENCRYPTED Matrix room for the Viber bridge control room.
#
# Element's UI defaults to E2EE for 'private' rooms, and encryption cannot be
# disabled once set. This script uses the createRoom API with preset=private_chat
# and an empty initial_state, which produces an unencrypted room.
#
# Usage:
#   bash create-control-room.sh '<YOUR_ADMIN_TOKEN>' '<BRIDGE_USER_ID>'
#
# Example:
#   bash create-control-room.sh 'syt_c2FtLnJ1c3NlbGw_...' '@viber:example.com'
#
# Environment:
#   HOMESERVER   Matrix homeserver URL (default: https://matrix.example.com)
#   ROOM_NAME    Display name of the room (default: 'Viber Control')

set -e

ADMIN_TOKEN="${1:?usage: create-control-room.sh <admin_access_token> <bridge_user_id>}"
BRIDGE_USER="${2:?usage: create-control-room.sh <admin_access_token> <bridge_user_id>}"
HOMESERVER="${HOMESERVER:-https://matrix.example.com}"
ROOM_NAME="${ROOM_NAME:-Viber Control}"

echo "Creating unencrypted room $ROOM_NAME on $HOMESERVER..."
echo "  Inviting: $BRIDGE_USER"

RESP=$(curl -sS -XPOST \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{
        \"name\": \"$ROOM_NAME\",
        \"preset\": \"private_chat\",
        \"invite\": [\"$BRIDGE_USER\"],
        \"initial_state\": []
    }" \
    "$HOMESERVER/_matrix/client/v3/createRoom")

echo ""
echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"

if echo "$RESP" | grep -q '"errcode"'; then
    exit 1
fi

ROOM_ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['room_id'])")

echo ""
echo "========================================================"
echo "  Room created (unencrypted)."
echo "  Room ID: $ROOM_ID"
echo ""
echo "  Next steps:"
echo "    1. Paste this room_id into scripts/config.yaml as matrix.control_room_id"
echo "    2. Run: bash accept-invite.sh '$ROOM_ID' '<bridge_access_token>'"
echo "========================================================"
