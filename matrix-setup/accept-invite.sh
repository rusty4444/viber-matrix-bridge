#!/bin/bash
# Accept a pending Matrix room invite for the @viber bridge user.
#
# Run this once after inviting @viber:example.com to the control room,
# and again for any future rooms you invite the bridge into manually.
#
# Usage:
#   bash accept-invite.sh '<ROOM_ID_OR_ALIAS>' '<ACCESS_TOKEN>'
#
# Examples:
#   bash accept-invite.sh '!abc123:example.com' 'syt_dmliZXI_xxx...'
#   bash accept-invite.sh '#viber-control:example.com' 'syt_dmliZXI_xxx...'

set -e

ROOM="${1:?usage: accept-invite.sh <room_id_or_alias> <access_token>}"
TOKEN="${2:?usage: accept-invite.sh <room_id_or_alias> <access_token>}"
HOMESERVER="${HOMESERVER:-https://matrix.example.com}"

# URL-encode the room ID/alias (handles ! # : safely)
ENCODED=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$ROOM")

echo "Joining $ROOM as the access-token owner..."
RESP=$(curl -sS -XPOST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{}' \
    "$HOMESERVER/_matrix/client/v3/join/$ENCODED")

echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"

# Exit non-zero if the response contains an error code
if echo "$RESP" | grep -q '"errcode"'; then
    exit 1
fi

echo ""
echo "✅ Joined. Bridge should now see messages in this room."
