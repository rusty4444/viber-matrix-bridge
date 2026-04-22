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
    MESSAGE_ITEM_EXACT_CLASS,
    STACKVIEW_EXACT_CLASS,
    INPUT_BOX_EXACT_CLASS,
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


def _native_find(parent, control_type_name: str, class_name: str,
                 timeout: float = 5.0, search_depth: int = 20):
    """Find a control using uiautomation's NATIVE search (IUIAutomation::
    FindFirst under the hood), not our recursive GetChildren() walker.

    This is what Accessibility Insights uses, and crucially it returns
    fresh results after UI state changes (search->chat) where GetChildren()
    returns stale results for several seconds.

    Args:
        parent: parent uiautomation control
        control_type_name: one of 'PaneControl', 'GroupControl', 'EditControl', etc.
        class_name: EXACT ClassName to match (no regex)
        timeout: total seconds to wait for the control to appear
        search_depth: UIA tree depth limit
    """
    if auto is None:
        raise ViberError("uiautomation not available on this platform")
    ctype = getattr(auto, control_type_name, None)
    if ctype is None:
        return None
    try:
        ctrl = ctype(
            searchFromControl=parent,
            ClassName=class_name,
            searchDepth=search_depth,
        )
        if ctrl.Exists(timeout, 0.2):
            return ctrl
    except Exception as e:
        log.debug("_native_find failed: %s", e)
    return None


def _native_find_all(parent, control_type_name: str, class_name_regex: str,
                    search_depth: int = 20, max_count: int = 50):
    """Find all descendants matching a regex class name.

    Uses ``auto.<ControlType>(searchFromControl=..., foundIndex=N)`` which
    internally calls ``IUIAutomation::FindFirst`` once per index. This
    reliably returns multiple results (unlike GetDescendantControls which
    doesn't exist in uiautomation 2.0.x).

    Note: ClassName regex matching isn't supported by UIA's native search,
    so we pull by ControlType only and filter class names in Python.
    """
    if auto is None:
        return []
    ctype = getattr(auto, control_type_name, None)
    if ctype is None:
        return []
    pat = re.compile(class_name_regex)
    out = []
    for i in range(1, max_count + 1):
        try:
            ctrl = ctype(searchFromControl=parent, foundIndex=i,
                         searchDepth=search_depth)
            if not ctrl.Exists(0.0, 0):
                break
            cls = ctrl.ClassName or ""
            if pat.search(cls):
                out.append(ctrl)
        except Exception:
            break
    return out


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

    def _chat_stack(self, timeout: float = 2.0):
        """Return the right-side StackView (active chat pane), or None.

        Uses UIA's native FindFirst (via uiautomation's ControlType search)
        because our recursive GetChildren() walker returns stale results
        for several seconds after a UI mode change (search <-> chat).
        """
        # Try exact class name first (fast, native UIA path)
        s = _native_find(self.window, "PaneControl", STACKVIEW_EXACT_CLASS,
                         timeout=timeout, search_depth=20)
        if s is not None:
            return s
        # Fallback: regex match via GetDescendantControl
        matches = _native_find_all(self.window, "PaneControl",
                                    r"StackView_QMLTYPE_\d+", search_depth=20)
        return matches[0] if matches else None

    def _search_box(self):
        """Find the top search box via a SINGLE native FindFirst call.
        Do NOT use foundIndex iteration — that enumerates all EditControls
        including message TextEditItems, potentially triggering Qt to
        render / rebuild them and degrade the accessibility tree.
        """
        # Exact class name first (fast path)
        s = _native_find(self.window, "EditControl", "TextFieldItem_QMLTYPE_94",
                          timeout=1.5, search_depth=10)
        return s

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

    def _clear_search_if_open(self):
        """If the top search box has text, Viber hides the chat pane behind
        search results and our FeedDelegate enumeration fails.

        CRITICAL: do NOT click the search box to clear it. Clicking it focuses
        the search field and steals focus from the chat pane; Viber then
        won't enumerate the chat's FeedDelegates and a subsequent search
        starts from a broken state (observed: search returns 0 rows on the
        next call).

        Instead: send a global {Esc} via the window handle. Viber's search
        widget collapses on Esc and returns focus to the previously-active
        chat. If the search box is already empty, this is a no-op.
        """
        try:
            search = self._search_box()
            if search is None:
                return
            current = ""
            try:
                vp = search.GetValuePattern()
                current = (vp.Value or "") if vp is not None else ""
            except Exception:
                current = ""
            if not current.strip():
                return
            log.info("clearing stale search text %r with global Esc", current[:40])
            # Global Esc — routed to whatever currently has keyboard focus in
            # the Viber window. This collapses the search results and
            # restores the chat pane without us ever focusing the search box.
            try:
                auto.SendKeys("{Esc}", waitTime=0.15)
                time.sleep(0.3)
                # Verify cleared. If Esc alone didn't clear (some Viber builds
                # only dismiss autocomplete on first Esc), try once more.
                vp2 = search.GetValuePattern()
                still = (vp2.Value or "") if vp2 is not None else ""
                if still.strip():
                    auto.SendKeys("{Esc}", waitTime=0.15)
                    time.sleep(0.3)
            except Exception as e:
                log.debug("clear_search Esc failed: %s", e)
        except Exception as e:
            log.debug("_clear_search_if_open error: %s", e)

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
        # Note: do NOT call _clear_search_if_open() here. This function's
        # own "click search + Ctrl+A + Delete + paste" below already clears
        # any residual text. Adding a pre-clear was observed to break the
        # subsequent search (returning 0 rows) because of focus churn.

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

            # Enumerate search-result delegates. Search results use class
            # name ListViewDelegateLoader_QMLTYPE_465_QML_1643 (distinct
            # from the normal chat list's _1615 variant). Use the exact
            # class and a shallow search depth to minimise UIA activity
            # — each extra FindFirst call risks degrading Qt's tree.
            all_rows = []
            for i in range(1, 10):
                try:
                    ctrl = auto.GroupControl(
                        searchFromControl=self.window,
                        ClassName="ListViewDelegateLoader_QMLTYPE_465_QML_1643",
                        foundIndex=i,
                        searchDepth=3,
                    )
                    if not ctrl.Exists(0.0, 0):
                        break
                    all_rows.append(ctrl)
                except Exception:
                    break

            # Fallback: if exact class didn't match (Viber updated the
            # QML type number), fall back to the broader automationId
            # lookup with still-shallow depth.
            if not all_rows:
                for i in range(1, 15):
                    try:
                        ctrl = auto.GroupControl(
                            searchFromControl=self.window,
                            AutomationId="delegateLoader",
                            foundIndex=i,
                            searchDepth=4,
                        )
                        if not ctrl.Exists(0.0, 0):
                            break
                        # Skip non-row delegates (the small QQuickLoader
                        # buttons at the top also have automationId=delegateLoader)
                        b = ctrl.BoundingRectangle
                        if b is None or b.right - b.left < 200:
                            continue
                        all_rows.append(ctrl)
                    except Exception:
                        break

            visible_rows = [r for r in all_rows if _is_visible(r)]
            visible_rows = _dedup_by_position(visible_rows)
            visible_rows.sort(key=lambda r: r.BoundingRectangle.top)
            log.info("after search %r: %d delegate(s) total, %d unique visible",
                     name, len(all_rows), len(visible_rows))

            if not visible_rows:
                log.error("No VISIBLE conversation rows after search %r. "
                          "Viber may be minimised, not focused, or the contact "
                          "name didn't match anything.", name)
                return False

            # Log every visible row's name/class/geometry. Viber groups
            # search results into sections: Conversations (top), Contacts,
            # Channels. We want the row under 'Conversations' whose Name
            # matches the query — that's the already-paired chat history.
            # Clicking any other section row either opens a new
            # contact/channel view or a section header (no chat).
            def _row_name(c):
                try:
                    return (c.Name or "").strip()
                except Exception:
                    return ""
            def _row_aid(c):
                try:
                    return (c.AutomationId or "").strip()
                except Exception:
                    return ""
            def _row_height(c):
                b = c.BoundingRectangle
                return b.bottom - b.top
            def _row_top(c):
                return c.BoundingRectangle.top

            for i, rr in enumerate(visible_rows):
                try:
                    br = rr.BoundingRectangle
                    log.info("  row[%d] name=%r aid=%r bounds=(%d,%d,%d,%d) h=%d",
                             i, _row_name(rr)[:40], _row_aid(rr)[:30],
                             br.left, br.top, br.right, br.bottom,
                             br.bottom - br.top)
                except Exception:
                    pass

            # Pick strategy, best-first:
            #   1. Row whose Name exactly case-insensitively equals the query
            #      AND has a sane height (40–200px).
            #   2. Row whose Name starts with the query (same sane height).
            #   3. Row whose Name contains the query (same sane height).
            #   4. Topmost sane-height row (current behaviour).
            #   5. Shortest row (avoids the full-viewport container delegate).
            query_lc = name.strip().lower()
            def sane(r):
                h = _row_height(r)
                return 40 <= h <= 200
            sane_rows = [r for r in visible_rows if sane(r)]
            target = None
            pick_reason = ""
            for pred, label in [
                (lambda r: _row_name(r).lower() == query_lc, "exact-name match"),
                (lambda r: _row_name(r).lower().startswith(query_lc), "name-prefix match"),
                (lambda r: query_lc in _row_name(r).lower(), "name-substring match"),
            ]:
                matches = [r for r in sane_rows if pred(r)]
                if matches:
                    matches.sort(key=_row_top)
                    target = matches[0]
                    pick_reason = label
                    break
            if target is None:
                if sane_rows:
                    sane_rows.sort(key=_row_top)
                    target = sane_rows[0]
                    pick_reason = "topmost sane-height row (no name match)"
                else:
                    log.warning("no row with sane height 40–200px after search %r; "
                                "falling back to shortest of %d visible rows",
                                name, len(visible_rows))
                    target = min(visible_rows, key=_row_height)
                    pick_reason = "shortest fallback row"

            r = target.BoundingRectangle
            # Click near the top-left of the picked row (avatar/name area).
            click_x = r.left + 50
            click_y = r.top + min(30, max(10, (r.bottom - r.top) // 3))
            log.info("clicking search result at screen (%d,%d) [picked=%r "
                     "reason=%s bounds=(%d,%d,%d,%d) h=%d]",
                     click_x, click_y, _row_name(target)[:40], pick_reason,
                     r.left, r.top, r.right, r.bottom, r.bottom - r.top)
            try:
                auto.Click(click_x, click_y, waitTime=0.1)
            except Exception as e:
                log.error("Click at (%d,%d) failed: %s", click_x, click_y, e)
                return False

            # Chat is visually open on the right, but Viber is in hybrid
            # 'search+chat' mode: the StackView is NOT in the UIA tree.
            # Strategy: click inside the RIGHT PANE AREA (not on any
            # control, just within the chat panel bounds) to give that
            # side focus. This may transition Viber to normal chat mode
            # and expose the StackView. We calculate coordinates window-
            # relative so they work regardless of where Viber is on screen.
            time.sleep(0.5)

            # First, clear search with keyboard only (no UIA clicks).
            try:
                auto.SendKeys("{Esc}", waitTime=0.15)
                auto.SendKeys("{Ctrl}a", waitTime=0.05)
                auto.SendKeys("{Delete}", waitTime=0.1)
                auto.SendKeys("{Esc}", waitTime=0.15)
            except Exception as e:
                log.debug("clearing search: %s", e)

            time.sleep(0.5)

            # Click in the right pane area. Viber's layout is roughly:
            #   left 28% = conversation sidebar
            #   right 72% = active chat pane
            # Click at ~65% across, ~50% down to hit the message area.
            try:
                wb = self.window.BoundingRectangle
                if wb is not None and (wb.right - wb.left) > 0:
                    cx = wb.left + int((wb.right - wb.left) * 0.65)
                    cy = wb.top  + int((wb.bottom - wb.top) * 0.50)
                    log.info("clicking right-pane area at (%d,%d) to exit hybrid mode",
                             cx, cy)
                    auto.Click(cx, cy, waitTime=0.1)
            except Exception as e:
                log.debug("right-pane click: %s", e)

            # Give Viber time to transition and update the UIA tree. Some
            # chats (especially those with images / long histories) need
            # several seconds for Qt to finish rendering the pane; do a
            # retry loop and optionally re-click the right pane once to
            # knock it out of hybrid mode.
            attempts = [(1.5, False), (1.5, True), (2.0, False)]
            for wait_s, reclick in attempts:
                time.sleep(wait_s)
                if reclick:
                    try:
                        wb = self.window.BoundingRectangle
                        if wb is not None and (wb.right - wb.left) > 0:
                            cx = wb.left + int((wb.right - wb.left) * 0.65)
                            cy = wb.top  + int((wb.bottom - wb.top) * 0.50)
                            log.info("re-clicking right-pane area at (%d,%d)", cx, cy)
                            auto.Click(cx, cy, waitTime=0.1)
                    except Exception:
                        pass
                stack = _native_find(self.window, "PaneControl",
                                     STACKVIEW_EXACT_CLASS,
                                     timeout=1.0, search_depth=20)
                if stack is not None:
                    log.info("chat opened (StackView found after right-pane click)")
                    return True
                feed = _native_find(self.window, "GroupControl",
                                    MESSAGE_ITEM_EXACT_CLASS,
                                    timeout=0.5, search_depth=20)
                if feed is not None:
                    log.info("chat opened (FeedDelegate found, no StackView)")
                    return True
            log.error(
                "Chat verification failed after 3 attempts. Neither StackView "
                "nor FeedDelegate found. Tried: search click → clear search → "
                "right-pane click → re-click → wait."
            )
            return False
        except Exception as e:
            log.error("open_conversation_by_search(%r) failed: %s", name, e)
            return False

    # Keep the old name as an alias for bridge.py compatibility.
    def open_conversation(self, name: str) -> bool:
        return self.open_conversation_by_search(name)

    def read_current_chat_messages(self, limit: int = 20, conversation_label: str = "") -> list[ViberMessage]:
        """Read messages from WHATEVER chat is currently open in Viber.
        Does not navigate — uses only native FindFirst to enumerate the
        currently-visible FeedDelegate controls and read their ValuePattern.

        Before reading, clears any residual search text (which would hide
        the chat pane behind search results) and briefly focuses the window
        so Qt renders the delegate rows.
        """
        self._ensure_attached()
        self._focus_window()
        self._clear_search_if_open()
        time.sleep(0.2)
        return self._read_open_chat(conversation_label or "<current>", limit)

    # ---- Reading messages --------------------------------------------
    def read_new_messages(self, name: str, limit: int = 20) -> list[ViberMessage]:
        """Read the last `limit` messages from the chat with `name`.

        CRITICAL: this method uses ONLY native FindFirst-based lookups and
        direct child navigation. Never calls GetChildren() or does recursive
        tree walks — those corrupt Qt's accessibility tree and cause the
        whole chat pane to disappear from UIA (see issue #15).
        """
        self._ensure_attached()
        if not self.open_conversation(name):
            return []
        return self._read_open_chat(name, limit)

    def _read_open_chat(self, name: str, limit: int) -> list[ViberMessage]:
        """Shared implementation: read FeedDelegate values from the currently
        open chat (whichever one that is)."""
        time.sleep(0.5)  # let Viber render + Qt accessibility settle

        # Enumerate FeedDelegate GroupControls via foundIndex loop.
        # Each iteration is a single IUIAutomation::FindFirst call, not a
        # recursive walk, so Qt's tree stays healthy.
        items: list = []
        for i in range(1, limit * 2 + 5):
            try:
                ctrl = auto.GroupControl(
                    searchFromControl=self.window,
                    ClassName=MESSAGE_ITEM_EXACT_CLASS,
                    foundIndex=i,
                    searchDepth=20,
                )
                if not ctrl.Exists(0.0, 0):
                    break
                items.append(ctrl)
            except Exception:
                break

        # Keep unique positions, sort top-to-bottom, keep newest N
        items = _dedup_by_position(items)
        items = [i for i in items if _is_visible(i)]
        items.sort(key=lambda c: c.BoundingRectangle.top)
        log.info("read_new_messages(%r): %d FeedDelegate visible", name, len(items))
        if not items:
            # Diagnose why nothing was found so we can tell the caller
            # whether to complain about search state, window focus, or a
            # genuinely empty chat.
            try:
                sb = self._search_box()
                sb_val = ""
                if sb is not None:
                    try:
                        vp = sb.GetValuePattern()
                        sb_val = (vp.Value or "") if vp is not None else ""
                    except Exception:
                        pass
                stack = _native_find(self.window, "PaneControl",
                                     STACKVIEW_EXACT_CLASS,
                                     timeout=0.5, search_depth=20)
                log.warning(
                    "read_new_messages(%r) empty — search_box=%r stackview=%s "
                    "(if search_box is non-empty, a previous op left Viber in "
                    "search mode; if stackview is None, no chat is open)",
                    name, sb_val[:40], "present" if stack is not None else "absent",
                )
            except Exception:
                pass
        items = items[-limit:]

        # Geometry-based direction detection. Viber right-aligns outgoing
        # bubbles and left-aligns incoming ones, but never exposes that in
        # UIA class/id. We derive the chat pane's horizontal span from the
        # widest visible FeedDelegate row (delegates span the full chat-pane
        # width even when the bubble inside is right- or left-aligned).
        # A bubble whose *center* sits clearly in the right half of that
        # span is outgoing. Fall back to window geometry, then to None.
        pane_left: float | None = None
        pane_right: float | None = None
        try:
            # Widest row = most likely the full pane width. Delegate rows in
            # Qt tend to be uniform width across the chat; picking the max
            # width survives edge-cases like a single very-narrow bubble.
            widest = None
            widest_w = 0
            for c in items:
                br = c.BoundingRectangle
                w = br.right - br.left
                if w > widest_w:
                    widest_w = w
                    widest = br
            if widest is not None and widest_w > 100:
                pane_left = float(widest.left)
                pane_right = float(widest.right)
        except Exception:
            pass
        if pane_left is None or pane_right is None:
            try:
                wr = self.window.BoundingRectangle
                # Chat pane is the right ~70% of the window (left ~30% is the
                # conversation list). This is approximate but good enough to
                # decide left-vs-right alignment within the pane.
                pane_left = float(wr.left) + 0.30 * (wr.right - wr.left)
                pane_right = float(wr.right)
            except Exception:
                pane_left = None
                pane_right = None
        pane_mid_x: float | None = (
            (pane_left + pane_right) / 2.0
            if pane_left is not None and pane_right is not None
            else None
        )
        pane_width: float = (
            (pane_right - pane_left)
            if pane_left is not None and pane_right is not None
            else 0.0
        )

        messages: list[ViberMessage] = []
        for feed in items:
            # Descend to the TextEditItem grandchild to read the message text.
            # Use GetFirstChildControl() (direct COM call, not recursive) to
            # get the inner EditControl whose ValuePattern has the text.
            text = ""
            try:
                # Try first-child navigation first (cheapest)
                child = feed.GetFirstChildControl()
                while child is not None:
                    if child.ControlTypeName == "EditControl":
                        text = _read_text(child)
                        if text:
                            break
                    child = child.GetNextSiblingControl()
                # Fallback: read_text on the FeedDelegate itself
                if not text:
                    text = _read_text(feed)
            except Exception as e:
                log.debug("reading FeedDelegate failed: %s", e)
                text = _read_text(feed)
            if not text:
                continue
            outgoing = any(h in (feed.ClassName or "").lower() for h in OUTGOING_HINTS) \
                or any(h in (feed.AutomationId or "").lower() for h in OUTGOING_HINTS)
            # Geometry fallback — Viber's UIA tree uses the same ClassName
            # for both directions, so class/id hints almost always miss.
            # If the bubble's horizontal center sits clearly in the right
            # half of the chat pane, treat it as outgoing.
            if not outgoing and pane_mid_x is not None and pane_width > 0:
                try:
                    br = feed.BoundingRectangle
                    bubble_mid_x = (br.left + br.right) / 2.0
                    # Require the bubble center to be at least 5% of the
                    # pane width to the right of the midpoint. Avoids
                    # classifying full-width bubbles (e.g. system notices)
                    # or borderline cases as outgoing.
                    if bubble_mid_x > pane_mid_x + 0.05 * pane_width:
                        outgoing = True
                        log.debug(
                            "geom-outgoing: bubble_mid=%.0f pane_mid=%.0f width=%.0f",
                            bubble_mid_x, pane_mid_x, pane_width,
                        )
                except Exception:
                    pass
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
        """Send a message using ONLY native FindFirst lookups (no GetChildren
        recursion — that corrupts Qt's accessibility tree, see #15)."""
        self._ensure_attached()
        if not self.open_conversation_by_search(name):
            return False
        time.sleep(0.3)
        # Native-find the input box and send button directly from the window
        inp = _native_find(self.window, "EditControl", INPUT_BOX_EXACT_CLASS,
                            timeout=2.0, search_depth=20)
        if inp is None:
            log.error("Input box (QQuickTextEdit) not found after opening chat")
            return False
        try:
            inp.Click(simulateMove=False)
            time.sleep(0.15)
            auto.SendKeys("{Ctrl}a", waitTime=0.05)
            auto.SendKeys("{Delete}", waitTime=0.05)
            pyperclip.copy(text)
            auto.SendKeys("{Ctrl}v", waitTime=0.1)
            time.sleep(0.2)
            send_btn = _native_find(self.window, "ButtonControl",
                                     "SendToolbarButton",
                                     timeout=0.5, search_depth=20)
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


def _inspect_chat_main(contact_name: str, max_depth: int = 8):
    """Step through the open-chat sequence manually, dumping the UIA tree
    at key moments to diagnose what's visible and what isn't."""
    c = ViberClient({"window_title": "Viber"})
    c.attach()
    c._focus_window()
    time.sleep(0.2)

    # --- Step 1: type into search ---
    print(f"\n[1] Typing {contact_name!r} into search box...")
    search = c._search_box()
    if search is None:
        print("    No search box found.")
        return
    search.Click(simulateMove=False)
    time.sleep(0.2)
    auto.SendKeys("{Ctrl}a", waitTime=0.05)
    auto.SendKeys("{Delete}", waitTime=0.05)
    pyperclip.copy(contact_name)
    auto.SendKeys("{Ctrl}v", waitTime=0.1)
    time.sleep(1.0)

    # --- Step 2: click the topmost visible row ---
    print("\n[2] Clicking first visible search result...")
    app = c._app_content()
    all_rows = _find_all(app, CONVERSATION_ROW, recursive=True, max_depth=12)
    visible = _dedup_by_position([r for r in all_rows if _is_visible(r)])
    visible.sort(key=lambda r: r.BoundingRectangle.top)
    if not visible:
        print("    No visible rows found.")
        return
    r0 = visible[0]
    cx, cy = r0.BoundingRectangle.left + 50, r0.BoundingRectangle.top + 30
    print(f"    Clicking ({cx},{cy}) — first of {len(visible)} visible rows")
    auto.Click(cx, cy, waitTime=0.1)
    time.sleep(0.5)

    # --- Step 3: clear search ---
    print("\n[3] Clearing search (Escape + explicit clear)...")
    auto.SendKeys("{Esc}", waitTime=0.2)
    try:
        search.Click(simulateMove=False)
        auto.SendKeys("{Ctrl}a", waitTime=0.05)
        auto.SendKeys("{Delete}", waitTime=0.1)
        auto.SendKeys("{Esc}", waitTime=0.1)
    except Exception as e:
        print(f"    (clear error: {e})")
    time.sleep(1.0)

    # --- Step 4: dump tree with VIS markers ---
    print(f"\n[4] Window tree AFTER clear (max_depth={max_depth}) — VIS = non-zero bounds:")
    for depth, el in _walk(c.window, max_depth=max_depth):
        indent = "  " * depth
        vis = "VIS" if _is_visible(el) else "   "
        try:
            ctype = el.ControlTypeName
            cls   = (el.ClassName or "")
            aid   = (el.AutomationId or "")[-50:]
            b     = el.BoundingRectangle
            bs    = f"{b.left},{b.top},{b.right-b.left}x{b.bottom-b.top}" if b else "-"
        except Exception:
            continue
        print(f"{indent}[{vis}] {ctype:20s} class={cls!r:50s} rect={bs}")

    # --- Step 5: try all three find strategies ---
    print("\n[5] Trying each find strategy:")
    print(f"    _native_find StackView     : {_native_find(c.window, 'PaneControl', STACKVIEW_EXACT_CLASS, timeout=2.0)}")
    print(f"    _native_find QQuickTextEdit: {_native_find(c.window, 'EditControl',  INPUT_BOX_EXACT_CLASS,  timeout=2.0)}")
    print(f"    _native_find FeedDelegate  : {_native_find(c.window, 'GroupControl', MESSAGE_ITEM_EXACT_CLASS,timeout=2.0)}")
    native_all = _native_find_all(c.window, "GroupControl", r"FeedDelegate_QMLTYPE_\d+", search_depth=20)
    print(f"    _native_find_all FeedDel   : {len(native_all)} results")
    our_find = _find(c.window, CHAT_STACK, timeout=2.0)
    print(f"    _find (GetChildren) StackView: {our_find}")

    # --- Step 6: if StackView found, dump its content ---
    stack = _native_find(c.window, "PaneControl", STACKVIEW_EXACT_CLASS, timeout=1.0) \
            or _find(c.window, CHAT_STACK, timeout=1.0)
    if stack:
        print(f"\n[6] StackView found! Dumping content with pattern values:")
        _dump_content(stack, max_depth=6)
    else:
        print("\n[6] StackView not found by any method. Content dump skipped.")


def _inspect_active_main():
    """Inspect the CURRENTLY OPEN Viber chat without doing any navigation.

    Run this while Viber is already showing a chat you've opened manually
    (don't use any other --inspect commands first). This tests whether
    uiautomation's native FindFirst can see the chat pane in a clean state
    (no prior GetChildren() tree-walking by our script).
    """
    c = ViberClient({"window_title": "Viber"})
    c.attach()   # this only does GetRootControl + iterates top-level windows
    print("Testing native FindFirst finds (no GetChildren tree-walking):")
    print()

    stack = _native_find(c.window, "PaneControl", STACKVIEW_EXACT_CLASS, timeout=3.0, search_depth=20)
    print(f"  StackView_QMLTYPE_463 :  {stack}")
    feed  = _native_find(c.window, "GroupControl", MESSAGE_ITEM_EXACT_CLASS, timeout=3.0, search_depth=20)
    print(f"  FeedDelegate_QMLTYPE  :  {feed}")
    inp   = _native_find(c.window, "EditControl",  INPUT_BOX_EXACT_CLASS,   timeout=3.0, search_depth=20)
    print(f"  QQuickTextEdit        :  {inp}")
    send  = _native_find(c.window, "ButtonControl", "SendToolbarButton",     timeout=3.0, search_depth=20)
    print(f"  SendToolbarButton     :  {send}")

    print()
    print("Shallow tree dump (max_depth=4) — VIS = non-zero bounds:")
    for depth, el in _walk(c.window, max_depth=4):
        indent = "  " * depth
        vis = "VIS" if _is_visible(el) else "   "
        try:
            ctype = el.ControlTypeName
            cls   = el.ClassName or ""
            b     = el.BoundingRectangle
            bs    = f"{b.left},{b.top},{b.right-b.left}x{b.bottom-b.top}" if b else "-"
        except Exception:
            continue
        print(f"{indent}[{vis}] {ctype:20s} {cls:50s} rect={bs}")

    if stack:
        print("\nStackView found! Value-pattern dump of its children:")
        _dump_content(stack, max_depth=4)
    elif feed:
        print("\nFeedDelegate found (no StackView). ValuePattern dump:")
        print(f"  text = {_read_text(feed)!r}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    argv = sys.argv[1:]
    if argv and argv[0] == "--inspect-active":
        _inspect_active_main()
        sys.exit(0)

    if argv and argv[0] == "--inspect-chat":
        if len(argv) < 2:
            print("usage: viber_client.py --inspect-chat <contact-name> [max_depth=15]")
            sys.exit(1)
        depth = int(argv[2]) if len(argv) > 2 else 8
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
