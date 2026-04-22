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
    CHAT_STACK,
    CHAT_INFO_PANEL,
    SEARCH_BOX,
    CONVERSATION_ROW,
    MESSAGE_ITEM,
    INPUT_BOX,
    SEND_BUTTON,
    SCROLL_TO_BOTTOM,
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


def _visible_bounds(el) -> tuple[int, int, int, int] | None:
    """Return (left, top, right, bottom) of el if it's actually rendered
    on screen (non-zero area). None if virtualized / off-screen / no bounds.
    """
    try:
        r = el.BoundingRectangle
        if r is None:
            return None
        # uiautomation's Rect has .left .top .right .bottom
        l, t, rt, b = r.left, r.top, r.right, r.bottom
        if rt - l <= 0 or b - t <= 0:
            return None
        return (l, t, rt, b)
    except Exception:
        return None


def _is_visible(el) -> bool:
    return _visible_bounds(el) is not None


def _dedup_by_position(controls):
    """Qt's UIA implementation exposes QQuickControl's children recursively
    across many nesting levels (a known Qt bug). ``_find_all(recursive=True)``
    therefore returns the same element 5-10+ times. Dedup by screen position.
    """
    seen = set()
    out = []
    for c in controls:
        try:
            r = c.BoundingRectangle
            key = (r.left, r.top, r.right, r.bottom)
        except Exception:
            out.append(c)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _read_text(el) -> str:
    """Extract text content from a UIA element.

    Viber's message bubbles are EditControls (TextEditItem_QMLTYPE_*) that
    expose their content through the Value pattern, not the Name property.
    Falls back to Name if Value is unavailable.
    """
    try:
        # 1. ValuePattern (for Edit-style controls)
        vp = el.GetValuePattern()
        if vp is not None:
            v = vp.Value
            if v:
                return v.strip()
    except Exception:
        pass
    try:
        # 2. TextPattern (for read-only document ranges)
        tp = el.GetTextPattern()
        if tp is not None:
            doc = tp.DocumentRange
            t = doc.GetText(-1)
            if t:
                return t.strip()
    except Exception:
        pass
    # 3. Name property (last resort)
    try:
        n = el.Name
        if n:
            return n.strip()
    except Exception:
        pass
    return ""


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

    # ---- Top-level panes --------------------------------------------
    def _app_content(self):
        return _find(self.window, APP_CONTENT, timeout=2.0)

    def _chat_stack(self):
        """Return the right-side StackView (active chat pane), or None."""
        return _find(self.window, CHAT_STACK, timeout=2.0)

    def _search_box(self):
        return _find(self.window, SEARCH_BOX, timeout=1.5)

    # ---- Conversation navigation ------------------------------------
    def list_conversations(self) -> list[ViberConversation]:
        """Return the visible conversation rows.

        NOTE: Viber's conversation-row delegates don't expose contact names
        via UIA (Name='' and no children). So this only returns row indices
        as stand-in names (e.g. "row:0"). Use this for *counting* visible
        rows, not for identifying who each chat is with. Use
        :meth:`open_conversation_by_search` to navigate by contact name.
        """
        self._ensure_attached()
        app = self._app_content()
        if app is None:
            log.warning("Could not find ApplicationWindowContentControl")
            return []
        rows = _find_all(app, CONVERSATION_ROW, recursive=True, max_depth=4)
        return [ViberConversation(name=f"row:{i}", unread=0)
                for i in range(len(rows))]

    def _focus_window(self):
        """Bring the Viber window to the foreground so UIA can actually click.
        Qt renders delegates lazily — off-screen / unfocused windows often
        have zero-size bounding rects even for "present" controls.
        """
        try:
            self.window.SetActive()
        except Exception:
            pass
        try:
            self.window.SetTopmost(True)
            time.sleep(0.05)
            self.window.SetTopmost(False)
        except Exception:
            pass
        try:
            self.window.SetFocus()
        except Exception:
            pass

    def open_conversation_by_search(self, name: str) -> bool:
        """Navigate to the chat with the given contact / group by typing the
        name into Viber's top search box and clicking the first VISIBLE result.

        Viber's QML virtualizes list delegates: non-visible rows exist in the
        UIA tree but have zero-size bounding rectangles and cannot be clicked.
        We must filter to only actually-rendered rows.
        """
        self._ensure_attached()
        self._focus_window()
        time.sleep(0.1)

        search = self._search_box()
        if search is None:
            log.error("Search box not found — check SEARCH_BOX selector")
            return False
        try:
            search.Click(simulateMove=False)
            time.sleep(0.15)
            auto.SendKeys("{Ctrl}a", waitTime=0.05)
            auto.SendKeys("{Delete}", waitTime=0.05)
            # Paste the query via clipboard (unicode-safe)
            pyperclip.copy(name)
            auto.SendKeys("{Ctrl}v", waitTime=0.1)
            # Give the list time to re-filter
            time.sleep(0.7)

            app = self._app_content()
            if app is None:
                log.error("ApplicationWindowContentControl not found after search")
                return False

            all_rows = _find_all(app, CONVERSATION_ROW, recursive=True, max_depth=12)
            visible_rows = [r for r in all_rows if _is_visible(r)]
            # De-duplicate: Qt UIA exposes each row at every nesting level of
            # QQuickControl, so we get N rows * depth duplicates.
            visible_rows = _dedup_by_position(visible_rows)
            # Sort topmost-first so the best search match is row[0].
            visible_rows.sort(key=lambda r: r.BoundingRectangle.top)
            log.info("after search %r: %d delegate(s) total, %d unique visible",
                     name, len(all_rows), len(visible_rows))

            if not visible_rows:
                log.error("No VISIBLE conversation rows after search %r. "
                          "Viber may be minimised, not focused, or the contact "
                          "name didn't match anything.", name)
                return False

            # Click the topmost visible row. IMPORTANT: we cannot use the
            # UIA control's default center click — Qt reports each row's
            # height as the full viewport-remaining height (e.g. 870px), so
            # "center" lands several rows below the actual item. Click near
            # the top-left of the reported bounds instead.
            target = visible_rows[0]
            r = target.BoundingRectangle
            # (left + 50, top + 30) reliably hits the avatar / name area of
            # a Viber list row.
            click_x = r.left + 50
            click_y = r.top + 30
            log.info("clicking search result at screen (%d,%d) "
                     "[row reports bounds=(%d,%d,%d,%d)]",
                     click_x, click_y, r.left, r.top, r.right, r.bottom)
            try:
                auto.Click(click_x, click_y, waitTime=0.1)
            except Exception as e:
                log.error("Click at (%d,%d) failed: %s", click_x, click_y, e)
                return False

            # Give Viber time to swap search UI out and paint the chat.
            # Qt's UIA tree can lag the visible UI by a second or two after a
            # mode change (search -> chat), so we retry patiently.
            post_click_wait = max(self.cfg.get("click_pause_ms", 1000), 1000) / 1000.0
            time.sleep(post_click_wait)

            # Re-attach the window handle in case the click caused Viber to
            # rebuild its top-level UIA object.
            try:
                self.window = None
                self.attach()
            except ViberError:
                pass

            # Look for ANY of: the active-chat StackView, OR the message input
            # box (QQuickTextEdit), OR a send button. Any of these = chat is
            # actually open. Retry over ~5s since the tree can settle slowly.
            stack = None
            input_box = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                stack = self._chat_stack()
                if stack is not None:
                    input_box = _find(stack, INPUT_BOX, timeout=0.2)
                    if input_box is not None:
                        break
                # Fallback: maybe StackView selector mismatches post-click; any
                # QQuickTextEdit in the window is a good sign.
                input_box = _find(self.window, INPUT_BOX, timeout=0.2)
                if input_box is not None and _is_visible(input_box):
                    break
                time.sleep(0.3)

            if stack is None and (input_box is None or not _is_visible(input_box)):
                log.error(
                    "Chat didn't open within 5s. Neither StackView nor a "
                    "visible message input was found. If Viber's right pane "
                    "shows a Channel viewer or 'info' panel, the top search "
                    "result wasn't a real Conversation."
                )
                return False
            log.info("chat opened successfully (stack=%s, input=%s)",
                     bool(stack), bool(input_box))

            # Only NOW clear the search, once we know we're in the chat
            try:
                search.Click(simulateMove=False)
                auto.SendKeys("{Ctrl}a", waitTime=0.05)
                auto.SendKeys("{Delete}", waitTime=0.05)
            except Exception:
                pass
            # Click back into the chat pane so next keystrokes go to input
            try:
                stack.Click(simulateMove=False)
            except Exception:
                pass
            return True
        except Exception as e:
            log.error("open_conversation_by_search(%r) failed: %s", name, e)
            return False

    # Keep the old name as an alias for bridge.py compatibility.
    def open_conversation(self, name: str) -> bool:
        return self.open_conversation_by_search(name)

    # ---- Reading messages --------------------------------------------
    def read_new_messages(self, name: str, limit: int = 20) -> list[ViberMessage]:
        """Read the last `limit` messages from the currently open chat.

        Returns only those *after* the last one we remember seeing in this chat.
        """
        self._ensure_attached()
        if not self.open_conversation(name):
            return []

        time.sleep(0.4)  # let Viber render the opened chat
        stack = self._chat_stack()
        if stack is None:
            log.warning("Active chat StackView not found")
            return []

        # Messages are TextEditItem EditControls directly inside the StackView.
        items = _find_all(stack, MESSAGE_ITEM, recursive=True, max_depth=4)
        items = items[-limit:]  # most recent N visible bubbles

        messages: list[ViberMessage] = []
        for it in items:
            text = _read_text(it)
            if not text:
                continue
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
        if not self.open_conversation_by_search(name):
            return False
        stack = self._chat_stack()
        if stack is None:
            return False
        inp = _find(stack, INPUT_BOX, timeout=1.5)
        if inp is None:
            log.error("Input box not found — check INPUT_BOX selector (expected QQuickTextEdit)")
            return False
        try:
            inp.Click(simulateMove=False)
            time.sleep(0.15)
            # Clear whatever's already typed
            auto.SendKeys("{Ctrl}a", waitTime=0.05)
            auto.SendKeys("{Delete}", waitTime=0.05)
            # Paste (reliable for unicode/emojis)
            pyperclip.copy(text)
            auto.SendKeys("{Ctrl}v", waitTime=0.1)
            time.sleep(0.2)
            # Prefer clicking the Send button; fall back to Enter.
            send_btn = _find(stack, SEND_BUTTON, timeout=0.5)
            if send_btn is not None:
                send_btn.Click(simulateMove=False)
            else:
                auto.SendKeys("{Enter}", waitTime=0.05)
            return True
        except Exception as e:
            log.error("send_message failed: %s", e)
            return False


# CLI inspection helper ----------------------------------------------------
def _dump_content(el, max_depth=15):
    """Dump a subtree, and for every leaf-ish control try to read its text
    via every available UIA pattern. Used by --inspect-chat to locate where
    Viber stores message text.
    """
    for depth, c in _walk(el, max_depth=max_depth):
        indent = "  " * depth
        try:
            ctype = c.ControlTypeName
            name = (c.Name or "").replace("\n", " \\n ")
            cls = c.ClassName or ""
            aid = c.AutomationId or ""
        except Exception as e:
            print(f"{indent}<err {e}>")
            continue

        # Gather text via all patterns
        texts = []
        if name:
            texts.append(f"Name={name[:120]!r}")
        try:
            vp = c.GetValuePattern()
            if vp and vp.Value:
                texts.append(f"Value={vp.Value[:120]!r}")
        except Exception:
            pass
        try:
            tp = c.GetTextPattern()
            if tp:
                t = tp.DocumentRange.GetText(200)
                if t and t.strip():
                    texts.append(f"Text={t.strip()[:120]!r}")
        except Exception:
            pass
        try:
            lp = c.GetLegacyIAccessiblePattern()
            if lp:
                lv = lp.Value
                if lv and lv.strip():
                    texts.append(f"LegacyValue={lv.strip()[:120]!r}")
        except Exception:
            pass

        text_blurb = (" | " + " ".join(texts)) if texts else ""
        print(f"{indent}- [{ctype}] class={cls!r} aid={aid[-60:]!r}{text_blurb}")


def _inspect_search_main(query: str, max_depth: int = 10):
    """Type a query into Viber's search box and dump the whole window tree,
    flagging which controls are actually visible on screen. Used to find
    where Viber renders search results.
    """
    c = ViberClient({"window_title": "Viber"})
    c.attach()
    c._focus_window()
    time.sleep(0.2)
    print(f"\n[1] Focusing Viber, typing {query!r} into search box...")
    search = c._search_box()
    if search is None:
        print("    No search box found. Aborting.")
        return
    search.Click(simulateMove=False)
    time.sleep(0.2)
    auto.SendKeys("{Ctrl}a", waitTime=0.05)
    auto.SendKeys("{Delete}", waitTime=0.05)
    pyperclip.copy(query)
    auto.SendKeys("{Ctrl}v", waitTime=0.1)
    time.sleep(1.0)  # let results render
    print(f"\n[2] Full window tree (max_depth={max_depth}) — 'VIS' marks controls with non-zero bounds:\n")
    for depth, el in _walk(c.window, max_depth=max_depth):
        indent = "  " * depth
        vis = "VIS" if _is_visible(el) else "   "
        try:
            ctype = el.ControlTypeName
            name = (el.Name or "")[:60]
            cls = el.ClassName or ""
            aid = (el.AutomationId or "")[-50:]
            b = el.BoundingRectangle
            bs = f"{b.left},{b.top},{b.right-b.left}x{b.bottom-b.top}" if b else "-"
        except Exception:
            continue
        print(f"{indent}[{vis}] {ctype} name={name!r} class={cls!r} aid={aid!r} rect={bs}")
    print("\n[3] Leaving Viber with search still populated so you can see it.")


def _inspect_chat_main(contact_name: str, max_depth: int = 15):
    """Open a chat and dump everything about its contents."""
    c = ViberClient({"window_title": "Viber"})
    c.attach()
    print(f"\n[1] Searching for and opening chat {contact_name!r}...")
    ok = c.open_conversation_by_search(contact_name)
    if not ok:
        print("    FAILED to open chat. Aborting.")
        return
    print("    Opened.")
    time.sleep(1.0)   # let Viber render
    stack = c._chat_stack()
    if stack is None:
        print("    Could not find StackView after opening chat.")
        return
    print(f"\n[2] StackView subtree (max_depth={max_depth}) with pattern-value dumps:\n")
    _dump_content(stack, max_depth=max_depth)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    argv = sys.argv[1:]
    if argv and argv[0] == "--inspect-chat":
        if len(argv) < 2:
            print("usage: viber_client.py --inspect-chat <contact-name> [max_depth=15]")
            sys.exit(1)
        depth = int(argv[2]) if len(argv) > 2 else 15
        _inspect_chat_main(argv[1], max_depth=depth)
        sys.exit(0)

    if argv and argv[0] == "--inspect-search":
        if len(argv) < 2:
            print("usage: viber_client.py --inspect-search <query> [max_depth=10]")
            sys.exit(1)
        depth = int(argv[2]) if len(argv) > 2 else 10
        _inspect_search_main(argv[1], max_depth=depth)
        sys.exit(0)

    c = ViberClient({"window_title": "Viber"})
    c.attach()

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
        print("  python viber_client.py --inspect-chat <contact-name> [max_depth=15]")
        print()
        print("Examples:")
        print("  python viber_client.py --inspect 10")
        print("  python viber_client.py --inspect-subtree StackView")
        print("  python viber_client.py --inspect-chat Candy")
