"""Matrix side of the Viber bridge.

Thin wrapper around matrix-nio that handles:
  - Login via access token
  - Creating per-Viber-chat rooms (and inviting the admin user)
  - Listening for messages in the control room (for commands) and in mapped rooms
  - Posting incoming Viber messages to the right room
"""

from __future__ import annotations
import asyncio
import logging
from typing import Awaitable, Callable, Optional

from nio import (
    AsyncClient,
    AsyncClientConfig,
    RoomMessageText,
    RoomCreateResponse,
    RoomInviteResponse,
    MatrixRoom,
    LoginResponse,
)

log = logging.getLogger("matrix")


class MatrixClient:
    """
    Callbacks:
      on_control_command(cmd: str, args: list[str], sender: str) -> str | None
          Called for messages in the control room starting with '!'.
          Return a string to post as reply, or None.
      on_room_message(room_id: str, sender: str, body: str) -> None
          Called for messages in mapped rooms from the admin user.
    """

    def __init__(
        self,
        cfg: dict,
        on_control_command: Callable[[str, list[str], str], Awaitable[Optional[str]]],
        on_room_message: Callable[[str, str, str], Awaitable[None]],
    ):
        self.cfg = cfg
        self._on_cmd = on_control_command
        self._on_msg = on_room_message
        self.client: Optional[AsyncClient] = None
        self.control_room = cfg["control_room_id"]
        self.admin = cfg["admin_user_id"]
        self._started_at: Optional[float] = None
        # Skip-history threshold (Unix ms). Initialised here so the callback
        # never AttributeErrors even if an event fires before start() finishes.
        # Overwritten in start() with a tighter value just before sync.
        import time as _time
        self._start_ms = int(_time.time() * 1000)

    # ---- Lifecycle ---------------------------------------------------
    async def start(self):
        conf = AsyncClientConfig(store_sync_tokens=True, encryption_enabled=False)
        self.client = AsyncClient(
            homeserver=self.cfg["homeserver_url"],
            user=self.cfg["user_id"],
            device_id=self.cfg.get("device_id"),
            config=conf,
        )
        self.client.access_token = self.cfg["access_token"]
        self.client.user_id = self.cfg["user_id"]
        self.client.device_id = self.cfg.get("device_id")

        # Refresh start-time threshold to just before sync so any event the
        # initial sync delivers is correctly classified as historical.
        import time as _time
        self._start_ms = int(_time.time() * 1000)

        # Register message handler
        self.client.add_event_callback(self._on_event, RoomMessageText)

        # Do an initial sync to get up-to-date state (and skip old messages)
        log.info("doing initial sync...")
        await self.client.sync(timeout=10000, full_state=True)
        log.info("connected as %s", self.client.user_id)

    async def stop(self):
        if self.client:
            await self.client.close()

    async def run_forever(self):
        """Long-running sync loop."""
        assert self.client is not None
        import time
        self._started_at = time.time()
        await self.client.sync_forever(timeout=30000)

    @property
    def uptime_seconds(self) -> float:
        import time
        return 0 if self._started_at is None else time.time() - self._started_at

    # ---- Event dispatch ----------------------------------------------
    async def _on_event(self, room: MatrixRoom, event: RoomMessageText):
        # Skip historical events replayed on startup. The initial sync
        # delivers all unprocessed room history — we only want new messages.
        if hasattr(event, "server_timestamp") and \
                event.server_timestamp < self._start_ms - 5000:
            return
        # Ignore our own echoes
        if event.sender == self.client.user_id:
            return
        # Only accept from admin
        if event.sender != self.admin:
            log.debug("ignoring message from non-admin %s (expected %s)",
                      event.sender, self.admin)
            return

        body = (event.body or "").strip()

        if room.room_id == self.control_room:
            if body.startswith("!"):
                parts = body[1:].split()
                cmd = parts[0].lower() if parts else ""
                args = parts[1:]
                try:
                    reply = await self._on_cmd(cmd, args, event.sender)
                except Exception as e:
                    log.exception("control command failed")
                    reply = f"error: {e}"
                if reply:
                    await self.send_text(self.control_room, reply)
            return

        # Any other room: forward to Viber side
        await self._on_msg(room.room_id, event.sender, body)

    # ---- Sending ------------------------------------------------------
    async def send_text(self, room_id: str, body: str):
        assert self.client is not None
        await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": body},
        )

    async def send_notice(self, room_id: str, body: str):
        assert self.client is not None
        await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.notice", "body": body},
        )

    # ---- Room creation -----------------------------------------------
    async def create_bridge_room(self, viber_name: str) -> str:
        assert self.client is not None
        name = f"{self.cfg.get('room_name_prefix', 'Viber · ')}{viber_name}"
        resp = await self.client.room_create(
            name=name,
            topic=f"Bridged Viber conversation: {viber_name}",
            invite=[self.admin],
            is_direct=False,
        )
        if isinstance(resp, RoomCreateResponse):
            log.info("created room %s for %r", resp.room_id, viber_name)
            return resp.room_id
        raise RuntimeError(f"room_create failed: {resp}")

    async def ensure_invite(self, room_id: str):
        """Make sure the admin is a member of the room."""
        assert self.client is not None
        try:
            await self.client.room_invite(room_id, self.admin)
        except Exception:
            pass
