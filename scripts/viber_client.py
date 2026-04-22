"""Viber Desktop client driver via Windows UI Automation.

This module is intentionally defensive: Viber's UI tree is not public API and
changes across versions. Every selector is centralized in `viber_selectors.py`.

Public API:
    client = ViberClient(config)
    client.attach()                    -> raises if Viber not running
    client.list_conversations()        -> [ViberConversation]
    client.open_conversation(name)     -> bool
    client.read_new_messages(name, limit) -> [ViberMessage]
    client.send_message(name, text)    -> bool
    client.inspect()                   -> dump top-level controls (for debugging)
"""

from __future__ import annotations
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import pyperclip

try:
    import uiautomation as auto
except ImportError:
    auto = None

from viber_selectors import (
    MAIN_WINDOW,
    CONVERSATION_LIST,
    CONVERSATION_ITEM,
    UNREAD_BADGE,
    CHAT_HEADER_TITLE,
    MESSAGE_LIST,
    MESSAGE_ITEM,
    INPUT_BOX,
    SEND_BUTTON,
    OUTGOING_HINTS,
)

log = logging.getLogger("viber")


@dataclass
class ViberConversation:
    name: str
    unread: int = 0


@dataclass
class ViberMessage:
    conversation: str
    sender: str             # "me" for outgoing, contact name for incoming
    text: str
    ts: float = field(default_factory=time.time)
    outgoing: bool = False


class ViberError(Exception):
    pass


def _find(parent, selector, timeout: float = 2.0):
    """Locate a UIA element under parent per selector. None if not found."""
    if auto is None:
        raise ViberError("uiautomation not available on this platform")

    kwargs = {}
    if selector.name and not selector.regex_name:
        kwargs["Name"] = selector.name
    if selector.automation_id:
        kwargs["AutomationId"] = selector.automation_id
    if selector.class_name:
        kwargs["ClassName"] = selector.class_name

    ctype = getattr(auto, selector.control_type, None)
    if ctype is None:
        raise ViberError(f"Unknown control type {selector.control_type}")

    # Regex name requires a search-and-filter approach.
    if selector.regex_name and selector.name:
        deadline = time.monotonic() + timeout
        pat = re.compile(selector.name, re.IGNORECASE)
        while time.monotonic() < deadline:
            for c in parent.GetChildren():
                try:
                    if c.ControlTypeName == selector.control_type and pat.search(c.Name or ""):
                        return c
                except Exception:
                    continue
            time.sleep(0.1)
        return None

    el = ctype(searchFromControl=parent, **kwargs)
    if el.Exists(timeout, 0.1):
        return el
    return None


def _walk(element, depth=0, max_depth=4):
    """Yield (depth, element) for a UIA subtree (for --inspect)."""
    yield depth, element
    if depth >= max_depth:
        return
    try:
        for c in element.GetChildren():
            yield from _walk(c, depth + 1, max_depth)
    except Exception:
        return


class ViberClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.window = None
        self._last_seen_per_chat: dict[str, str] = {}   # conversation -> last msg text

    # ---- Attachment ---------------------------------------------------
    def attach(self):
        if auto is None:
            raise ViberError("Install on Windows with uiautomation package.")
        # Find the Viber main window
        title_sub = self.cfg.get("window_title", "Viber")
        root = auto.GetRootControl()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for w in root.GetChildren():
                try:
                    if w.ControlTypeName == "WindowControl" and title_sub.lower() in (w.Name or "").lower():
                        self.window = w
                        log.info("attached to Viber window: %r", w.Name)
                        return
                except Exception:
                    pass
            time.sleep(0.5)
        raise ViberError(f"Could not find Viber window matching {title_sub!r}")

    def _ensure_attached(self):
        if self.window is None or not self.window.Exists(0, 0):
            self.attach()

    # ---- Inspection (debug) ------------------------------------------
    def inspect(self):
        self._ensure_attached()
        print(f"Top-level children of Viber window ({self.window.Name!r}):")
        for depth, el in _walk(self.window, max_depth=3):
            indent = "  " * depth
            try:
                print(f"{indent}- [{el.ControlTypeName}] name={el.Name!r} "
                      f"automationId={el.AutomationId!r} class={el.ClassName!r}")
            except Exception as e:
                print(f"{indent}- <error: {e}>")

    # ---- Conversations ------------------------------------------------
    def list_conversations(self) -> list[ViberConversation]:
        self._ensure_attached()
        chat_list = _find(self.window, CONVERSATION_LIST)
        if chat_list is None:
            log.warning("Conversation list not found — check CONVERSATION_LIST selector")
            return []

        convs: list[ViberConversation] = []
        for item in chat_list.GetChildren():
            if item.ControlTypeName != "ListItemControl":
                continue
            name = (item.Name or "").strip()
            if not name:
                continue
            # First line of Name is usually the contact/group
            primary = name.splitlines()[0].strip()
            unread = 0
            badge = _find(item, UNREAD_BADGE, timeout=0.1)
            if badge and (badge.Name or "").strip().isdigit():
                unread = int(badge.Name.strip())
            convs.append(ViberConversation(name=primary, unread=unread))
        return convs

    def open_conversation(self, name: str) -> bool:
        self._ensure_attached()
        chat_list = _find(self.window, CONVERSATION_LIST)
        if chat_list is None:
            return False
        target = None
        lname = name.lower()
        for item in chat_list.GetChildren():
            if item.ControlTypeName != "ListItemControl":
                continue
            primary = (item.Name or "").splitlines()[0].strip().lower()
            if primary == lname:
                target = item
                break
        if target is None:
            log.warning("Conversation %r not found", name)
            return False
        try:
            target.Click(simulateMove=False)
            time.sleep(self.cfg.get("click_pause_ms", 150) / 1000.0)
            return True
        except Exception as e:
            log.error("Click failed on %r: %s", name, e)
            return False

    # ---- Reading messages --------------------------------------------
    def read_new_messages(self, name: str, limit: int = 20) -> list[ViberMessage]:
        """Read the last `limit` messages from the currently open chat.

        Returns only those *after* the last one we remember seeing in this chat.
        """
        self._ensure_attached()
        if not self.open_conversation(name):
            return []

        time.sleep(0.3)  # let Viber render
        msg_list = _find(self.window, MESSAGE_LIST)
        if msg_list is None:
            log.warning("Message list not found — check MESSAGE_LIST selector")
            return []

        items = [c for c in msg_list.GetChildren()
                 if c.ControlTypeName == "ListItemControl"]
        items = items[-limit:]  # most recent N

        messages: list[ViberMessage] = []
        for it in items:
            text = (it.Name or "").strip()
            if not text:
                continue
            # Try to detect outgoing vs incoming via class/automation hints
            outgoing = any(h in (it.ClassName or "").lower() for h in OUTGOING_HINTS) \
                    or any(h in (it.AutomationId or "").lower() for h in OUTGOING_HINTS)
            sender = "me" if outgoing else name
            messages.append(ViberMessage(
                conversation=name, sender=sender, text=text, outgoing=outgoing
            ))

        last_seen = self._last_seen_per_chat.get(name)
        if last_seen is None:
            # First read — treat everything as already seen, only remember marker
            if messages:
                self._last_seen_per_chat[name] = messages[-1].text
            return []

        # Return only messages after the last_seen marker
        new_msgs: list[ViberMessage] = []
        found = False
        for m in messages:
            if not found:
                if m.text == last_seen:
                    found = True
                continue
            new_msgs.append(m)

        if not found:
            # Marker scrolled off — return all but the oldest half to be safe
            new_msgs = messages[len(messages)//2:]

        if new_msgs:
            self._last_seen_per_chat[name] = new_msgs[-1].text
        return new_msgs

    # ---- Sending ------------------------------------------------------
    def send_message(self, name: str, text: str) -> bool:
        self._ensure_attached()
        if not self.open_conversation(name):
            return False
        inp = _find(self.window, INPUT_BOX)
        if inp is None:
            log.error("Input box not found — check INPUT_BOX selector")
            return False
        try:
            inp.Click(simulateMove=False)
            time.sleep(0.1)
            # Clear existing
            auto.SendKeys("{Ctrl}a", waitTime=0.05)
            auto.SendKeys("{Delete}", waitTime=0.05)
            # Paste from clipboard — much more reliable for unicode than SendKeys
            pyperclip.copy(text)
            auto.SendKeys("{Ctrl}v", waitTime=0.1)
            time.sleep(0.1)
            auto.SendKeys("{Enter}", waitTime=0.05)
            return True
        except Exception as e:
            log.error("send_message failed: %s", e)
            return False


# CLI inspection helper ----------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if "--inspect" in sys.argv:
        c = ViberClient({"window_title": "Viber"})
        c.attach()
        c.inspect()
    else:
        print("Run with --inspect to dump the Viber UI tree.")
