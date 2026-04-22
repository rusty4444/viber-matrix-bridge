"""Viber ↔ Matrix bridge entrypoint.

Runs two concurrent loops:
  - Matrix sync loop (nio) — handles commands + outgoing Matrix→Viber messages
  - Viber poll loop — reads Viber Desktop for new messages and posts to Matrix

Usage:
    python bridge.py --config config.yaml
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

import yaml

from state import State, hash_msg
from matrix_client import MatrixClient
from viber_client import ViberClient, ViberError


log = logging.getLogger("bridge")


# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Basic validation
    for key in ("matrix", "viber", "bridge"):
        if key not in cfg:
            raise SystemExit(f"config missing section: {key}")
    return cfg


def setup_logging(cfg: dict):
    level = getattr(logging, cfg["bridge"].get("log_level", "INFO").upper(), logging.INFO)
    logfile = cfg["bridge"].get("log_file")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = State(cfg["bridge"]["state_db"])
        self.viber = ViberClient(cfg["viber"])
        self.matrix = MatrixClient(
            cfg["matrix"],
            on_control_command=self._on_control,
            on_room_message=self._on_matrix_message,
        )
        self._stop = asyncio.Event()

    # ---- Startup -----------------------------------------------------
    async def start(self):
        await self.state.init()
        await self.matrix.start()
        try:
            self.viber.attach()
        except ViberError as e:
            log.error("Viber not available at startup: %s", e)
            await self.matrix.send_notice(
                self.cfg["matrix"]["control_room_id"],
                f"⚠️ Viber not available: {e}",
            )
            # Keep going — Viber may come back later

        if self.cfg["bridge"].get("startup_message"):
            await self.matrix.send_notice(
                self.cfg["matrix"]["control_room_id"],
                f"✅ {self.cfg['bridge']['startup_message']}",
            )

    async def run(self):
        await self.start()

        # Install signal handlers
        loop = asyncio.get_running_loop()
        try:
            for s in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(s, self._stop.set)
        except NotImplementedError:
            # Windows + some event loop types don't support this; that's fine
            pass

        tasks = [
            asyncio.create_task(self.matrix.run_forever(), name="matrix-sync"),
            asyncio.create_task(self._viber_loop(), name="viber-poll"),
            asyncio.create_task(self._housekeeping_loop(), name="housekeeping"),
        ]
        done, pending = await asyncio.wait(
            tasks + [asyncio.create_task(self._stop.wait(), name="stop")],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in done:
            if t.exception() and t.get_name() != "stop":
                log.error("task %s crashed: %s", t.get_name(), t.exception())
        await self.matrix.stop()

    # ---- Viber poll loop ---------------------------------------------
    async def _viber_loop(self):
        interval = self.cfg["viber"].get("poll_interval_seconds", 3)
        while not self._stop.is_set():
            try:
                await self._scan_viber()
            except ViberError as e:
                log.warning("viber error: %s — will retry", e)
                self.viber.window = None
            except Exception:
                log.exception("viber loop error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _scan_viber(self):
        """Poll every *mapped* Viber chat for new messages.

        We can't reliably detect unread counts across the conversation list
        because Viber's delegate rows don't expose their contact name via
        UIA. So instead we iterate over chats that have already been paired
        to a Matrix room (via \x60!pair\x60 in the control room) and read each.
        """
        if self.viber.window is None:
            try:
                self.viber.attach()
            except ViberError:
                return

        mappings = await self.state.list_mappings()
        if not mappings:
            return
        limit = self.cfg["bridge"].get("max_backfill_per_chat", 5)
        for viber_name, room_id in mappings:
            try:
                messages = self.viber.read_new_messages(viber_name, limit=limit)
            except Exception:
                log.exception("reading %r failed", viber_name)
                continue
            if not messages:
                continue
            for m in messages:
                h = hash_msg(viber_name, m.sender, m.text)
                if await self.state.seen(h):
                    continue
                if m.outgoing:
                    # Message we (or another Viber client) sent — mark seen silently
                    await self.state.mark_seen(h, "viber->matrix")
                    continue
                body = f"{m.sender}: {m.text}"
                try:
                    await self.matrix.send_text(room_id, body)
                    await self.state.mark_seen(h, "viber->matrix")
                except Exception:
                    log.exception("failed to post to Matrix")

    # ---- Matrix→Viber -------------------------------------------------
    async def _on_matrix_message(self, room_id: str, sender: str, body: str):
        viber_name = await self.state.get_viber_for_room(room_id)
        if viber_name is None:
            log.debug("ignoring message in unmapped room %s", room_id)
            return

        # Echo suppression: record that we're about to send this
        h = hash_msg(viber_name, "me", body)
        await self.state.mark_seen(h, "matrix->viber")

        try:
            ok = self.viber.send_message(viber_name, body)
            if not ok:
                await self.matrix.send_notice(
                    room_id, f"⚠️ failed to deliver to Viber chat {viber_name!r}"
                )
        except ViberError as e:
            await self.matrix.send_notice(room_id, f"⚠️ Viber error: {e}")

    # ---- Control-room commands ---------------------------------------
    async def _on_control(self, cmd: str, args: list[str], sender: str) -> str | None:
        if cmd == "help":
            return (
                "Commands:\n"
                "  !status — bridge & Viber status\n"
                "  !list — mapped Viber chats\n"
                "  !scan — count of visible Viber conversation rows (names not readable)\n"
                "  !addchat <viber name> — create a Matrix room for this Viber chat\n"
                "  !pair <!room_id> <viber name> — manually pair an existing room\n"
                "  !unpair <viber name> — remove pairing\n"
                "  !test <viber name> — try navigating to a chat and reading messages\n"
                "  !reload — re-attach to Viber\n"
            )
        if cmd == "status":
            up = int(self.matrix.uptime_seconds)
            viber_ok = self.viber.window is not None
            mappings = await self.state.list_mappings()
            return (
                f"uptime: {up}s\n"
                f"viber attached: {viber_ok}\n"
                f"mapped chats: {len(mappings)}"
            )
        if cmd == "list":
            mappings = await self.state.list_mappings()
            if not mappings:
                return "(no mappings yet)"
            return "\n".join(f"  {v}  →  {r}" for v, r in mappings)
        if cmd == "scan":
            try:
                convs = self.viber.list_conversations()
            except ViberError as e:
                return f"viber error: {e}"
            if not convs:
                return "(no conversations detected — check selectors)"
            return (f"{len(convs)} conversation row(s) visible.\n"
                    f"Note: Viber doesn't expose contact names on row delegates,\n"
                    f"so use !addchat <name> or !pair to pair a specific chat.")
        if cmd == "addchat":
            if not args:
                return "usage: !addchat <viber contact or group name>"
            vname = " ".join(args)
            existing = await self.state.get_room_for_viber(vname)
            if existing:
                return f"already paired: {vname!r} → {existing}"
            # Verify we can actually reach that chat before creating a room
            if not self.viber.open_conversation_by_search(vname):
                return f"could not find a Viber chat matching {vname!r}"
            room_id = await self.matrix.create_bridge_room(vname)
            await self.state.set_mapping(vname, room_id)
            return f"created & paired: {vname!r} → {room_id}"
        if cmd == "test":
            if not args:
                return "usage: !test <viber contact or group name>"
            vname = " ".join(args)
            try:
                opened = self.viber.open_conversation_by_search(vname)
                if not opened:
                    return f"could not open {vname!r}"
                msgs = self.viber.read_new_messages(vname, limit=5)
                if not msgs:
                    return f"opened {vname!r} but no messages read (check MESSAGE_ITEM selector)"
                lines = [f"opened {vname!r}, read {len(msgs)} recent message(s):"]
                for m in msgs[-5:]:
                    d = "→" if m.outgoing else "←"
                    lines.append(f"  {d} {m.text[:80]}")
                return "\n".join(lines)
            except Exception as e:
                return f"test failed: {e}"
        if cmd == "pair":
            if len(args) < 2:
                return "usage: !pair <!room_id> <viber name...>"
            room_id = args[0]
            vname = " ".join(args[1:])
            await self.state.set_mapping(vname, room_id)
            return f"paired {vname!r} → {room_id}"
        if cmd == "unpair":
            if not args:
                return "usage: !unpair <viber name>"
            vname = " ".join(args)
            await self.state.delete_mapping(vname)
            return f"unpaired {vname!r}"
        if cmd == "reload":
            try:
                self.viber.window = None
                self.viber.attach()
                return "reattached to Viber"
            except ViberError as e:
                return f"failed: {e}"
        return f"unknown command: {cmd}"

    # ---- Housekeeping -------------------------------------------------
    async def _housekeeping_loop(self):
        while not self._stop.is_set():
            try:
                await self.state.purge_old(
                    self.cfg["bridge"].get("echo_suppression_window_seconds", 30) * 10
                )
            except Exception:
                log.exception("housekeeping error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)
    log.info("starting viber bridge")

    bridge = Bridge(cfg)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
