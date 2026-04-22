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
    APP_CONTENT,
    SPLIT_VIEW,
    SIDEBAR_CONTENT,
    CHAT_STACK,
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


def _matches(el, selector) -> bool:
    """Does this element satisfy the given selector?"""
    try:
        if el.ControlTypeName != selector.control_type:
            return False
        if selector.name is not None:
            if selector.regex_name:
                if not re.search(selector.name, el.Name or "", re.IGNORECASE):
                    return False
            else:
                if (el.Name or "") != selector.name:
                    return False
        if selector.automation_id is not None:
            # AutomationId matching is always substring (UIA values are long paths)
            if selector.automation_id.lower() not in (el.AutomationId or "").lower():
                return False
        if selector.class_name is not None:
            if selector.regex_class:
                if not re.search(selector.class_name, el.ClassName or ""):
                    return False
            else:
                if (el.ClassName or "") != selector.class_name:
                    return False
        return True
    except Exception:
        return False


def _find(parent, selector, timeout: float = 2.0, recursive: bool = True):
    """Locate the first descendant of `parent` matching `selector`.
    Returns None if not found within `timeout` seconds.
    """
    if auto is None:
        raise ViberError("uiautomation not available on this platform")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _search_one(parent, selector, recursive=recursive)
        if result is not None:
            return result
        time.sleep(0.15)
    return None


def _search_one(parent, selector, recursive: bool = True, max_depth: int = 10, depth: int = 0):
    try:
        children = parent.GetChildren()
    except Exception:
        return None
    for c in children:
        if _matches(c, selector):
            return c
        if recursive and depth < max_depth:
            r = _search_one(c, selector, recursive=True, max_depth=max_depth, depth=depth + 1)
            if r is not None:
                return r
    return None


def _find_all(parent, selector, recursive: bool = True, max_depth: int = 10):
    """Find all descendants matching the selector."""
    results = []
    _collect(parent, selector, results, recursive=recursive, max_depth=max_depth)
    return results


def _collect(parent, selector, results, recursive: bool, max_depth: int, depth: int = 0):
    try:
        children = parent.GetChildren()
    except Exception:
        return
    for c in children:
        if _matches(c, selector):
            results.append(c)
        if recursive and depth < max_depth:
            _collect(c, selector, results, recursive=True, max_depth=max_depth, depth=depth + 1)


def _walk(element, depth=0, max_depth=8):
    """Yield (depth, element) for a UIA subtree (for --inspect)."""
    yield depth, element
    if depth >= max_depth:
        return
    try:
        for c in element.GetChildren():
            yield from _walk(c, depth + 1, max_depth)
    except Exception:
        return


def _find_first_matching(element, needle, depth=0, max_depth=10):
    """Depth-first search for any descendant whose ClassName or AutomationId
    contains `needle` (case-insensitive). Returns the first match or None."""
    needle_l = needle.lower()
    try:
        cn = (element.ClassName or "").lower()
        aid = (element.AutomationId or "").lower()
        if needle_l in cn or needle_l in aid:
            return element
    except Exception:
        pass
    if depth >= max_depth:
        return None
    try:
        for c in element.GetChildren():
            r = _find_first_matching(c, needle, depth + 1, max_depth)
            if r is not None:
                return r
    except Exception:
        return None
    return None


class ViberClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.window = None
        self._last_seen_per_chat: dict[str, str] = {}   # conversation -> last msg text

    # ---- Attachment ---------------------------------------------------
    def attach(self):
        """Find the Viber Desktop window.

        Matching priority:
          1. A top-level WindowControl whose ClassName matches
             ``MainWindow_QMLTYPE_\\d+`` (Viber's Qt QML main window class).
             This is the **primary** signal — title substrings are unreliable
             because any File Explorer window showing a folder named "viber"
             will match a naive title search.
          2. If no Qt match found, fall back to a title-substring match on the
             configured ``window_title`` (default "Viber").
        """
        if auto is None:
            raise ViberError("Install on Windows with uiautomation package.")

        title_sub = self.cfg.get("window_title", "Viber").lower()
        qt_class_re = re.compile(r"MainWindow_QMLTYPE_\d+")

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            root = auto.GetRootControl()
            candidates: list[tuple[int, object]] = []   # (score, window)
            for w in root.GetChildren():
                try:
                    if w.ControlTypeName != "WindowControl":
                        continue
                    name = (w.Name or "")
                    cls = (w.ClassName or "")
                    score = 0
                    if qt_class_re.search(cls):
                        score += 100   # strong: it's the Viber Qt main window
                    if title_sub and title_sub in name.lower():
                        score += 10
                    if "viber" in cls.lower():
                        score += 5
                    # Negative signal: obvious non-Viber windows whose
                    # titles accidentally contain 'viber'
                    if cls in ("CabinetWClass", "Shell_TrayWnd", "Chrome_WidgetWin_1"):
                        score -= 100
                    if score > 0:
                        candidates.append((score, w))
                        log.debug("candidate window: score=%d class=%r name=%r",
                                  score, cls, name)
                except Exception:
                    continue

            if candidates:
                candidates.sort(key=lambda t: t[0], reverse=True)
                best_score, best = candidates[0]
                if best_score >= 100:
                    self.window = best
                    log.info("attached to Viber window: class=%r name=%r score=%d",
                             best.ClassName, best.Name, best_score)
                    return
                # Only weak matches (title only) — log them but keep looking
                # briefly in case Viber is still launching.
                log.debug("only weak candidate(s) found; retrying: %s",
                          [(s, w.Name, w.ClassName) for s, w in candidates[:3]])
            time.sleep(0.5)

        # Final fallback: accept best weak candidate if we have one
        if candidates:
            best_score, best = candidates[0]
            log.warning("no strong Qt-class match; falling back to weak candidate "
                        "class=%r name=%r (score=%d). If this is wrong, close the "
                        "File Explorer window showing the viber-bridge folder, or "
                        "adjust viber.window_title in config.yaml.",
                        best.ClassName, best.Name, best_score)
            self.window = best
            return

        raise ViberError(
            f"Could not find the Viber Desktop window. Is Viber running and logged in? "
            f"Expected a WindowControl with ClassName matching 'MainWindow_QMLTYPE_\\d+'."
        )

    def _ensure_attached(self):
        if self.window is None or not self.window.Exists(0, 0):
            self.attach()

    # ---- Inspection (debug) ------------------------------------------
    def inspect(self, max_depth: int = 8):
        """Dump the whole Viber window subtree to stdout."""
        self._ensure_attached()
        print(f"Window: {self.window.Name!r} (max_depth={max_depth})")
        for depth, el in _walk(self.window, max_depth=max_depth):
            indent = "  " * depth
            try:
                print(f"{indent}- [{el.ControlTypeName}] name={el.Name!r} "
                      f"automationId={el.AutomationId!r} class={el.ClassName!r}")
            except Exception as e:
                print(f"{indent}- <error: {e}>")

    def inspect_subtree(self, needle: str, max_depth: int = 10):
        """Find the first descendant whose class or automationId matches
        `needle` (substring, case-insensitive) and dump that subtree only."""
        self._ensure_attached()
        root = _find_first_matching(self.window, needle)
        if root is None:
            print(f"No control found matching {needle!r}")
            return
        print(f"Subtree rooted at ClassName={root.ClassName!r} "
              f"automationId={root.AutomationId!r}")
        for depth, el in _walk(root, max_depth=max_depth):
            indent = "  " * depth
            try:
                print(f"{indent}- [{el.ControlTypeName}] name={el.Name!r} "
                      f"automationId={el.AutomationId!r} class={el.ClassName!r}")
            except Exception as e:
                print(f"{indent}- <error: {e}>")

    # ---- Conversations ------------------------------------------------
    def _sidebar(self):
        """Return the left-side SideBarContent pane, or None."""
        return _find(self.window, SIDEBAR_CONTENT, timeout=2.0)

    def _chat_stack(self):
        """Return the right-side StackView (active chat pane), or None."""
        return _find(self.window, CHAT_STACK, timeout=2.0)

    def _extract_row_label(self, row) -> str:
        """Pull the contact/group name out of a conversation row.
        Viber doesn't set Name on the delegate itself, so we descend and pick
        up the first non-empty TextControl."""
        if row.Name:
            return row.Name.splitlines()[0].strip()
        # Walk children, pick first TextControl with text
        try:
            for depth, el in _walk(row, max_depth=6):
                if el is row:
                    continue
                if el.ControlTypeName == "TextControl" and (el.Name or "").strip():
                    return el.Name.strip().splitlines()[0].strip()
        except Exception:
            pass
        return ""

    def list_conversations(self) -> list[ViberConversation]:
        self._ensure_attached()
        sidebar = self._sidebar()
        if sidebar is None:
            log.warning("Sidebar not found — check SIDEBAR_CONTENT selector")
            return []

        rows = _find_all(sidebar, CONVERSATION_ITEM, recursive=True, max_depth=6)
        convs: list[ViberConversation] = []
        for row in rows:
            label = self._extract_row_label(row)
            if not label:
                continue
            # Look for a numeric unread badge anywhere in the row subtree
            unread = 0
            badges = _find_all(row, UNREAD_BADGE, recursive=True, max_depth=6)
            for b in badges:
                try:
                    unread = max(unread, int((b.Name or "").strip()))
                except ValueError:
                    continue
            convs.append(ViberConversation(name=label, unread=unread))
        return convs

    def open_conversation(self, name: str) -> bool:
        self._ensure_attached()
        sidebar = self._sidebar()
        if sidebar is None:
            return False
        rows = _find_all(sidebar, CONVERSATION_ITEM, recursive=True, max_depth=6)
        target = None
        lname = name.lower()
        for row in rows:
            if self._extract_row_label(row).lower() == lname:
                target = row
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
        stack = self._chat_stack() or self.window
        msg_list = _find(stack, MESSAGE_LIST, timeout=1.5)
        if msg_list is None:
            log.warning("Message list not found — check MESSAGE_LIST selector")
            return []

        items = _find_all(msg_list, MESSAGE_ITEM, recursive=True, max_depth=4)
        if not items:
            # Fallback: any child control with a non-empty Name
            items = [c for c in msg_list.GetChildren() if (c.Name or "").strip()]
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
        stack = self._chat_stack() or self.window
        inp = _find(stack, INPUT_BOX, timeout=1.5)
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
    c = ViberClient({"window_title": "Viber"})
    c.attach()

    argv = sys.argv[1:]
    if argv and argv[0] == "--inspect-subtree":
        if len(argv) < 2:
            print("usage: viber_client.py --inspect-subtree <classname-or-automationid-substring>")
            sys.exit(1)
        depth = int(argv[2]) if len(argv) > 2 else 10
        c.inspect_subtree(argv[1], max_depth=depth)
    elif argv and argv[0] == "--inspect":
        depth = int(argv[1]) if len(argv) > 1 else 8
        c.inspect(max_depth=depth)
    else:
        print("Usage:")
        print("  python viber_client.py --inspect [max_depth=8]")
        print("  python viber_client.py --inspect-subtree <needle> [max_depth=10]")
        print()
        print("Examples:")
        print("  python viber_client.py --inspect 10")
        print("  python viber_client.py --inspect-subtree SideBarContent")
        print("  python viber_client.py --inspect-subtree StackView")
