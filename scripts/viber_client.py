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
import ctypes
import ctypes.wintypes
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

# UIA property IDs (from UIAutomationClient.h).
# https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-automation-element-propids
UIA_BoundingRectanglePropertyId = 30001
UIA_IsOffscreenPropertyId = 30022


class _UIA_RECT(ctypes.Structure):
    """tagRECT as returned by IUIAutomationElement::get_CurrentBoundingRectangle.

    UIA uses (left, top, right, bottom) in physical screen pixels (not DIPs).
    """
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]

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


def _rect_from_variant(v) -> tuple[int, int, int, int] | None:
    """GetCurrentPropertyValue(UIA_BoundingRectanglePropertyId) returns a
    VARIANT holding a SAFEARRAY of 4 doubles: [left, top, width, height].
    comtypes usually unwraps it to a tuple/list."""
    if v is None:
        return None
    try:
        arr = list(v)
    except Exception:
        return None
    if len(arr) != 4:
        return None
    try:
        l = int(round(arr[0])); t = int(round(arr[1]))
        w = int(round(arr[2])); h = int(round(arr[3]))
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    return (l, t, l + w, t + h)


def _bounds_via_com(el) -> tuple[int, int, int, int] | None:
    """Try three direct COM paths on the raw IUIAutomationElement.

    Python-UIAutomation-for-Windows (``uiautomation`` package) exposes the
    underlying COM object as ``Control.Element`` (sometimes ``_element`` on
    older versions). This bypasses the library's wrapper, which is where the
    (0,0,0,0) / None bug lives.

    Paths tried, in order:
      A. ``element.CurrentBoundingRectangle`` — comtypes-generated property
         that wraps ``get_CurrentBoundingRectangle``. Returns a tagRECT.
      B. ``element.GetCurrentPropertyValue(UIA_BoundingRectanglePropertyId)``
         — returns a VARIANT holding [left, top, width, height] doubles.
      C. Raw vtable call to ``get_CurrentBoundingRectangle`` via ctypes.
    """
    raw = getattr(el, "Element", None) or getattr(el, "_element", None)
    if raw is None:
        return None

    # Path A: comtypes-wrapped property
    try:
        rect = raw.CurrentBoundingRectangle
        if rect is not None:
            l = int(rect.left); t = int(rect.top)
            r = int(rect.right); b = int(rect.bottom)
            if r - l > 0 and b - t > 0:
                return (l, t, r, b)
    except Exception:
        pass

    # Path B: GetCurrentPropertyValue -> VARIANT (SAFEARRAY of 4 doubles)
    try:
        v = raw.GetCurrentPropertyValue(UIA_BoundingRectanglePropertyId)
        got = _rect_from_variant(v)
        if got is not None:
            return got
    except Exception:
        pass

    # Path C: raw vtable call via ctypes.
    # IUIAutomationElement methods are declared in UIAutomationClient.h; the
    # layout we need is: [0]=QueryInterface [1]=AddRef [2]=Release ...
    # get_CurrentBoundingRectangle is reliably the 9th method on the v-table
    # for IUIAutomationElement (index 8 zero-based). We locate it via the
    # Python attribute that comtypes resolves for us, so we don't have to
    # hard-code the slot.
    try:
        # comtypes exposes each method as a bound callable on the proxy.
        fn = getattr(raw, "_get_CurrentBoundingRectangle", None)
        if fn is not None:
            rect = _UIA_RECT()
            hr = fn(ctypes.byref(rect))
            if hr == 0 and (rect.right - rect.left) > 0 and (rect.bottom - rect.top) > 0:
                return (int(rect.left), int(rect.top),
                        int(rect.right), int(rect.bottom))
    except Exception:
        pass

    return None


def _debug_bounds_all(el) -> str:
    """Diagnostic: try every bounds-reading path and return a short string
    summarising which ones worked. Only used by --inspect-chat fallbacks."""
    parts = []
    # Wrapped property.
    try:
        r = el.BoundingRectangle
        if r is None:
            parts.append("wrap=None")
        else:
            w = r.right - r.left; h = r.bottom - r.top
            parts.append(f"wrap=({r.left},{r.top},{r.right},{r.bottom},{w}x{h})")
    except Exception as e:
        parts.append(f"wrap!={type(e).__name__}")

    raw = getattr(el, "Element", None) or getattr(el, "_element", None)
    if raw is None:
        parts.append("raw=None")
        return " | ".join(parts)

    # Path A
    try:
        rr = raw.CurrentBoundingRectangle
        parts.append(f"A=({rr.left},{rr.top},{rr.right},{rr.bottom})")
    except Exception as e:
        parts.append(f"A!={type(e).__name__}:{e}")
    # Path B
    try:
        v = raw.GetCurrentPropertyValue(UIA_BoundingRectanglePropertyId)
        try:
            as_list = list(v) if v is not None else None
        except Exception:
            as_list = repr(v)[:60]
        parts.append(f"B={as_list}")
    except Exception as e:
        parts.append(f"B!={type(e).__name__}:{e}")
    # Path C (raw vtable)
    try:
        fn = getattr(raw, "_get_CurrentBoundingRectangle", None)
        if fn is None:
            parts.append("C=no-attr")
        else:
            rect = _UIA_RECT()
            hr = fn(ctypes.byref(rect))
            parts.append(f"C(hr={hr:#x})=({rect.left},{rect.top},{rect.right},{rect.bottom})")
    except Exception as e:
        parts.append(f"C!={type(e).__name__}:{e}")
    # Offscreen hint
    try:
        off = raw.GetCurrentPropertyValue(UIA_IsOffscreenPropertyId)
        parts.append(f"off={off}")
    except Exception:
        pass
    return " | ".join(parts)


def _read_bounds_live(el, retries: int = 2, settle: float = 0.05) -> tuple[int, int, int, int] | None:
    """Read BoundingRectangle, working around the known uiautomation quirk
    where freshly-found proxies return (0,0,0,0) or None from the wrapped
    .BoundingRectangle property even though the underlying UIA element has
    a valid rect.

    See https://github.com/yinkaisheng/Python-UIAutomation-for-Windows/issues/212

    Strategy (each attempt, short-circuits on first success):
      1. Library property: ``el.BoundingRectangle`` (fast path).
      2. Direct COM on ``el.Element``:
           a. ``CurrentBoundingRectangle`` (comtypes property)
           b. ``GetCurrentPropertyValue(30001)`` (VARIANT)
           c. ``_get_CurrentBoundingRectangle`` raw vtable call
      3. ``el.Refind()`` to force the library to re-resolve the UIA element
         from its stored condition, then loop.
      4. Small sleep + retry.

    Returns (left, top, right, bottom) in physical screen pixels, or None.
    """
    for attempt in range(retries + 1):
        # 1. Wrapped property (normally works).
        try:
            r = el.BoundingRectangle
            if r is not None:
                l, t, rt, b = r.left, r.top, r.right, r.bottom
                if rt - l > 0 and b - t > 0:
                    return (l, t, rt, b)
        except Exception:
            pass

        # 2. Direct COM fallback — the important one.
        got = _bounds_via_com(el)
        if got is not None:
            return got

        # 3. Refind and loop.
        try:
            el.Refind()
        except Exception:
            pass
        if attempt < retries:
            time.sleep(settle)

    return None


def _visible_bounds(el) -> tuple[int, int, int, int] | None:
    """Return (left, top, right, bottom) of el if it's actually rendered
    on screen (non-zero area). None if virtualized / off-screen / no bounds.
    Uses _read_bounds_live so stale proxies get a Refind retry first.
    """
    return _read_bounds_live(el)


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
        have zero-size bounding rects even for "present" controls. And if
        Viber isn't the foreground window when we SendKeys, the keystrokes
        go into whichever window IS foreground (terminal, Matrix client).

        Windows 11 deliberately blocks SetForegroundWindow() from non-
        foreground processes as an anti-focus-stealing measure. The
        well-known workaround is to temporarily attach our thread's input
        queue to the foreground window's thread, which lifts the block.
        """
        # 1. Library-level focus (harmless if it doesn't work).
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

        # 2. Win32 AttachThreadInput trick to defeat Windows 11's
        #    foreground-lock. No-op on non-Windows platforms (import
        #    will fail silently).
        try:
            hwnd = self.window.NativeWindowHandle
            if hwnd:
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                SW_RESTORE = 9
                # If minimised, un-minimise first.
                try:
                    if user32.IsIconic(hwnd):
                        user32.ShowWindow(hwnd, SW_RESTORE)
                except Exception:
                    pass
                # Attach our thread's input queue to the foreground
                # thread, call SetForegroundWindow, then detach.
                fg_hwnd = user32.GetForegroundWindow()
                our_tid = kernel32.GetCurrentThreadId()
                fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
                attached = False
                if fg_tid and fg_tid != our_tid:
                    try:
                        attached = bool(user32.AttachThreadInput(our_tid, fg_tid, True))
                    except Exception:
                        attached = False
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                    user32.SetFocus(hwnd)
                except Exception:
                    pass
                if attached:
                    try:
                        user32.AttachThreadInput(our_tid, fg_tid, False)
                    except Exception:
                        pass
        except Exception as e:
            log.debug("_focus_window win32 path: %s", e)
        # Small settle so Qt has time to redraw / populate delegates.
        time.sleep(0.1)

    def _read_search_text(self, search_ctrl) -> str:
        """Return the current value of the search box, or ''. Used to
        verify a paste / typed query actually landed in the box."""
        if search_ctrl is None:
            return ""
        for getter in (
            lambda: search_ctrl.GetValuePattern().Value,
            lambda: search_ctrl.GetLegacyIAccessiblePattern().Value,
            lambda: search_ctrl.Name,
        ):
            try:
                v = getter()
                if v:
                    return str(v).strip()
            except Exception:
                continue
        return ""

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
            # Focus the search box and paste the query. Verify the paste
            # actually landed by reading the box's Value back; if it
            # didn't, retry up to 3 times. This catches the common
            # failure where Viber wasn't the foreground window so
            # SendKeys went to the terminal / Matrix client.
            pyperclip.copy(name)
            pasted_ok = False
            for attempt in range(3):
                try:
                    search.Click(simulateMove=False)
                except Exception as e:
                    log.debug("attempt %d: search.Click failed: %s", attempt + 1, e)
                time.sleep(0.15)
                try:
                    auto.SendKeys("{Ctrl}a", waitTime=0.05)
                    auto.SendKeys("{Delete}", waitTime=0.05)
                    auto.SendKeys("{Ctrl}v", waitTime=0.1)
                except Exception as e:
                    log.debug("attempt %d: SendKeys failed: %s", attempt + 1, e)
                time.sleep(0.5)
                current = self._read_search_text(search)
                if current and name.lower() in current.lower():
                    log.info("search-box text confirmed on attempt %d: %r",
                             attempt + 1, current[:60])
                    pasted_ok = True
                    break
                log.warning("attempt %d: expected %r in search box, got %r; "
                            "re-focusing Viber and retrying",
                            attempt + 1, name, current[:60])
                # Re-focus and try again. The foreground may have been
                # stolen by the terminal that logged the previous line.
                self._focus_window()
                time.sleep(0.2)

            if not pasted_ok:
                log.error(
                    "Could not get %r into Viber's search box after 3 "
                    "attempts. Viber may not be accepting keyboard input "
                    "(minimised? covered by a modal? focus-stolen?). Last "
                    "read-back: %r. Try foregrounding Viber manually.",
                    name, self._read_search_text(search)[:60],
                )
                return False

            # Give the list time to re-filter after the (confirmed) paste.
            time.sleep(0.5)

            # Viber has shipped these rows under multiple QML-type suffixes
            # across versions (ListViewDelegateLoader_QMLTYPE_465_QML_<NNNN>).
            # We match the stable prefix and filter by sidebar geometry.
            ROW_CLASS_PREFIX = "ListViewDelegateLoader_QMLTYPE_465"

            def _enumerate_search_delegates() -> list[tuple]:
                """Walk the UIA tree and CAPTURE bounds + name inline for
                every search-result delegate. Returns a deduped, top-sorted
                list of (ctrl, (l,t,r,b), class_name, name_text, aid) tuples.

                Rationale for inline capture: ``uiautomation``'s foundIndex
                traversal returns fresh UIA proxies per iteration, and
                proxies collected earlier can report zero-area
                BoundingRectangle when re-queried later (Qt's IAccessible
                sometimes detaches proxies after further tree activity).
                """
                captured: list[tuple] = []
                for i in range(1, 40):
                    try:
                        ctrl = auto.GroupControl(
                            searchFromControl=self.window,
                            AutomationId="delegateLoader",
                            foundIndex=i,
                            searchDepth=4,
                        )
                        if not ctrl.Exists(0.0, 0):
                            break
                        try:
                            cls = ctrl.ClassName or ""
                        except Exception:
                            cls = ""
                        if not cls.startswith(ROW_CLASS_PREFIX):
                            # Skip small unrelated delegates (e.g. the
                            # profile button at the top-left, a QQuickLoader
                            # that also carries AID='delegateLoader').
                            continue
                        bounds = _read_bounds_live(ctrl)
                        if bounds is None:
                            continue
                        w = bounds[2] - bounds[0]
                        h = bounds[3] - bounds[1]
                        # Sidebar search-result row geometry: width
                        # ~280–340 px, height ~60–90 px. Be generous.
                        if w < 200 or w > 400:
                            continue
                        if h < 40 or h > 200:
                            continue
                        try:
                            row_name = (ctrl.Name or "").strip()
                        except Exception:
                            row_name = ""
                        try:
                            row_aid = (ctrl.AutomationId or "").strip()
                        except Exception:
                            row_aid = ""
                        captured.append((ctrl, bounds, cls, row_name, row_aid))
                    except Exception:
                        break

                # Secondary fallback: if the AID-based enumeration produced
                # nothing, try the old exact-class selectors for both known
                # suffixes.
                if not captured:
                    for suffix in ("_QML_1615", "_QML_1643"):
                        for i in range(1, 15):
                            try:
                                ctrl = auto.GroupControl(
                                    searchFromControl=self.window,
                                    ClassName=f"{ROW_CLASS_PREFIX}{suffix}",
                                    foundIndex=i,
                                    searchDepth=3,
                                )
                                if not ctrl.Exists(0.0, 0):
                                    break
                                bounds = _read_bounds_live(ctrl)
                                if bounds is None:
                                    continue
                                try:
                                    row_name = (ctrl.Name or "").strip()
                                except Exception:
                                    row_name = ""
                                try:
                                    row_aid = (ctrl.AutomationId or "").strip()
                                except Exception:
                                    row_aid = ""
                                captured.append((ctrl, bounds,
                                                 f"{ROW_CLASS_PREFIX}{suffix}",
                                                 row_name, row_aid))
                            except Exception:
                                break
                        if captured:
                            break

                # Dedup by (left, top) — Qt's UIA bug sometimes returns the
                # same element many times via different tree paths.
                seen_pos = set()
                uniq: list[tuple] = []
                for tup in captured:
                    key = (tup[1][0], tup[1][1])
                    if key in seen_pos:
                        continue
                    seen_pos.add(key)
                    uniq.append(tup)
                # Sort by cached top coordinate (don't re-query bounds).
                uniq.sort(key=lambda t: t[1][1])
                return uniq

            # Retry enumeration up to 3 times if we get 0 rows. Qt's QML
            # list is virtualized and sometimes takes noticeably longer
            # than the initial 0.5 s to populate delegates in the UIA tree
            # — especially on a repeat search for the same name, where
            # Qt's accessibility cache appears to briefly hold stale empty
            # state. Between retries, re-verify the search text is still
            # in the box (a focus change could have cleared it) and
            # re-foreground Viber to keep it accepting input.
            unique: list[tuple] = []
            ENUM_RETRIES = 3
            for enum_attempt in range(ENUM_RETRIES):
                unique = _enumerate_search_delegates()
                if unique:
                    if enum_attempt > 0:
                        log.info("delegate enumeration succeeded on attempt %d",
                                 enum_attempt + 1)
                    break
                if enum_attempt == ENUM_RETRIES - 1:
                    break
                # Confirm the search text is still in the box; if it was
                # cleared by focus loss, re-paste before retrying.
                current = self._read_search_text(search)
                if not current or name.lower() not in current.lower():
                    log.warning("enum attempt %d: 0 rows AND search box "
                                "lost text (%r); re-pasting",
                                enum_attempt + 1, current[:60])
                    self._focus_window()
                    time.sleep(0.15)
                    try:
                        search.Click(simulateMove=False)
                    except Exception:
                        pass
                    time.sleep(0.1)
                    try:
                        auto.SendKeys("{Ctrl}a", waitTime=0.05)
                        auto.SendKeys("{Delete}", waitTime=0.05)
                        auto.SendKeys("{Ctrl}v", waitTime=0.1)
                    except Exception as e:
                        log.debug("re-paste SendKeys failed: %s", e)
                else:
                    log.info("enum attempt %d: 0 rows, search text intact "
                            "(%r); waiting for Qt to populate delegates",
                            enum_attempt + 1, current[:60])
                # Progressively longer waits: 0.8 s, 1.5 s.
                time.sleep(0.8 + 0.7 * enum_attempt)

            log.info("after search %r: %d unique delegate(s)",
                     name, len(unique))

            if not unique:
                log.error("No search-result rows matched after search %r "
                          "(tried %d enumeration attempts). Viber may be "
                          "minimised, not focused, or the contact name "
                          "didn't match anything. Tried AID=delegateLoader "
                          "with class prefix %r and geometry "
                          "200–400px x 40–200px.",
                          name, ENUM_RETRIES, ROW_CLASS_PREFIX)
                return False

            # Log every captured row's cached properties for diagnostics.
            # Viber groups search results into sections (Conversations,
            # Contacts, Channels); we prefer the Conversations row whose
            # Name matches the query.
            for idx, (ctrl, bnds, cls, row_name, row_aid) in enumerate(unique):
                log.info("  row[%d] name=%r aid=%r bounds=(%d,%d,%d,%d) h=%d cls=%r",
                         idx, row_name[:40], row_aid[:30],
                         bnds[0], bnds[1], bnds[2], bnds[3], bnds[3] - bnds[1],
                         cls[:50])

            # Pick strategy, best-first (all operate on cached row_name):
            #   1. Exact case-insensitive Name match
            #   2. Name prefix match
            #   3. Name substring match
            #   4. All query-words present (any order) — handles multi-word
            #      contacts where Viber adds a suffix to the display name
            #   5. Topmost row (no name match)
            query_lc = name.strip().lower()
            query_words = [w for w in query_lc.split() if w]
            target_tup = None
            pick_reason = ""
            for pred, label in [
                (lambda n: n.lower() == query_lc, "exact-name match"),
                (lambda n: n.lower().startswith(query_lc), "name-prefix match"),
                (lambda n: query_lc in n.lower(), "name-substring match"),
                (lambda n: all(w in n.lower() for w in query_words) if query_words else False,
                 "all-words-present match"),
            ]:
                matches = [t for t in unique if pred(t[3])]
                if matches:
                    matches.sort(key=lambda t: t[1][1])
                    target_tup = matches[0]
                    pick_reason = label
                    break
            if target_tup is None:
                target_tup = unique[0]  # already sorted by top
                pick_reason = "topmost row (no name match)"

            # Use the CACHED bounds for click coordinates, not a re-query.
            target, bnds, _cls, _nm, _aid = target_tup
            cl_left, cl_top, cl_right, cl_bottom = bnds
            click_x = cl_left + 50
            click_y = cl_top + min(30, max(10, (cl_bottom - cl_top) // 3))
            r = type("_R", (), {"left": cl_left, "top": cl_top,
                                "right": cl_right, "bottom": cl_bottom})()
            # Alias for the diagnostic log line below.
            def _row_name(c):
                return _nm
            log.info("clicking search result at screen (%d,%d) [picked=%r "
                     "reason=%s bounds=(%d,%d,%d,%d) h=%d]",
                     click_x, click_y, _row_name(target)[:40], pick_reason,
                     r.left, r.top, r.right, r.bottom, r.bottom - r.top)
            try:
                auto.Click(click_x, click_y, waitTime=0.1)
            except Exception as e:
                log.error("Click at (%d,%d) failed: %s", click_x, click_y, e)
                return False

            # Chat is visually open on the right, but Viber is now in
            # hybrid 'search+chat' mode: the StackView and FeedDelegate
            # are NOT exposed to UIA even though they're rendered. Qt's
            # accessibility bridge only populates them in 'normal chat
            # mode' — i.e. a chat opened from the normal conversation
            # list, not from search results.
            #
            # Two-step navigation (restored from 6e0cf41, removed by
            # mistake in 99bbb78):
            #   1. Clear search with keyboard only (no UIA clicks to
            #      preserve the tree).
            #   2. Re-click the topmost visible delegate in the normal
            #      conversation list. Since we just opened this chat,
            #      it will have bubbled to the top of the recency-sorted
            #      list. Clicking it re-opens the same chat in 'normal
            #      chat mode' which DOES expose the StackView.
            time.sleep(0.5)
            try:
                auto.SendKeys("{Esc}", waitTime=0.15)
                auto.SendKeys("{Ctrl}a", waitTime=0.05)
                auto.SendKeys("{Delete}", waitTime=0.1)
                auto.SendKeys("{Esc}", waitTime=0.15)
            except Exception as e:
                log.debug("clearing search: %s", e)

            # Wait for the normal conversation list to redraw.
            time.sleep(1.0)

            # Enumerate normal-list delegates. Viber has shipped these
            # under multiple QML-type suffixes (_QML_1615, _QML_1643, and
            # possibly others on future builds) so we match the stable
            # prefix and filter by sidebar geometry — same pattern as the
            # first-click enumeration above.
            ROW_CLASS_PREFIX = "ListViewDelegateLoader_QMLTYPE_465"
            normal_captured: list[tuple] = []
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
                    try:
                        ncls = ctrl.ClassName or ""
                    except Exception:
                        ncls = ""
                    if not ncls.startswith(ROW_CLASS_PREFIX):
                        continue
                    nb = _read_bounds_live(ctrl)
                    if nb is None:
                        continue
                    nw = nb[2] - nb[0]; nh = nb[3] - nb[1]
                    if nw < 200 or nw > 400 or nh < 40 or nh > 200:
                        continue
                    normal_captured.append((ctrl, nb, ncls))
                except Exception:
                    break
            # Dedup by (left, top) and sort by top.
            nseen = set(); normal_unique: list[tuple] = []
            for tup in normal_captured:
                key = (tup[1][0], tup[1][1])
                if key in nseen:
                    continue
                nseen.add(key)
                normal_unique.append(tup)
            normal_unique.sort(key=lambda t: t[1][1])

            if normal_unique:
                _nctrl, nb, ncls = normal_unique[0]
                ncx = nb[0] + 50
                ncy = nb[1] + min(30, max(10, (nb[3] - nb[1]) // 3))
                log.info("re-clicking topmost normal-list row at (%d,%d) "
                         "[bounds=(%d,%d,%d,%d) cls=%r] to enter normal chat mode",
                         ncx, ncy, nb[0], nb[1], nb[2], nb[3], ncls[:50])
                try:
                    auto.Click(ncx, ncy, waitTime=0.1)
                except Exception as e:
                    log.debug("normal-list re-click failed: %s", e)
            else:
                log.warning("no normal-list rows visible after clearing search; "
                            "cannot re-click to enter normal mode")

            # Give Viber time to transition and expose the StackView.
            time.sleep(1.5)

            # Verify via native FindFirst for StackView / FeedDelegate /
            # input box. Any of the three is a strong signal the right
            # pane is actually in the UIA tree now.
            def _verify_now() -> bool:
                stack = _native_find(self.window, "PaneControl",
                                     STACKVIEW_EXACT_CLASS,
                                     timeout=1.0, search_depth=20)
                if stack is not None:
                    log.info("chat opened (StackView found)")
                    return True
                feed = _native_find(self.window, "GroupControl",
                                    MESSAGE_ITEM_EXACT_CLASS,
                                    timeout=0.5, search_depth=20)
                if feed is not None:
                    log.info("chat opened (FeedDelegate found)")
                    return True
                inp = _native_find(self.window, "EditControl",
                                   INPUT_BOX_EXACT_CLASS,
                                   timeout=0.5, search_depth=20)
                if inp is not None:
                    log.info("chat opened (input box found)")
                    return True
                return False

            if _verify_now():
                return True

            # Second chance: wait a bit longer and try again. Qt can be
            # slow on heavy chats (lots of history / attachments).
            time.sleep(1.5)
            if _verify_now():
                return True

            # Last resort: if we DID successfully click a row with real
            # bounds, the chat is probably open visually even if Qt isn't
            # exposing the pane. Trust the click and let downstream
            # send/read surface specific errors.
            log.warning(
                "Chat verification could not locate StackView, FeedDelegate "
                "or input box after two-step navigation. Clicked row bounds=%r. "
                "Trusting the click; downstream send/read will give a more "
                "specific error if the chat didn't actually open.",
                bnds,
            )
            return True
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
    print(f"    _find_all returned {len(all_rows)} raw CONVERSATION_ROW candidates")
    # Cache bounds + class + name INLINE to avoid Qt's stale-proxy bug
    # where re-reading BoundingRectangle returns zero-area rects.
    captured = []
    for rr in all_rows:
        bn = _read_bounds_live(rr)
        if bn is None:
            continue
        try:
            cls = (rr.ClassName or "")
            nm  = (rr.Name or "").strip()
        except Exception:
            cls, nm = "", ""
        captured.append((rr, bn, cls, nm))
    # dedup by (left, top)
    seen = set()
    unique = []
    for tup in captured:
        key = (tup[1][0], tup[1][1])
        if key in seen:
            continue
        seen.add(key)
        unique.append(tup)
    unique.sort(key=lambda t: t[1][1])
    print(f"    after visibility + dedup: {len(unique)} unique rows")
    for idx, (_r, bn, cls, nm) in enumerate(unique):
        print(f"      row[{idx}] name={nm!r:20s} bounds=({bn[0]},{bn[1]},{bn[2]},{bn[3]}) cls={cls!r}")
    if not unique:
        print("    No visible rows found. Falling back to sidebar-scan diagnostic:")
        # Show per-path diagnostic on the first few RAW rows so we can see
        # which COM path (wrapped / A / B / C) is actually working, if any.
        print("    Per-path diagnostic for first raw rows:")
        for i, rr in enumerate(all_rows[:6]):
            try:
                dbg = _debug_bounds_all(rr)
                cls = (rr.ClassName or "")
                nm = (rr.Name or "").strip()
                print(f"      raw[{i}] name={nm!r:20s} cls={cls!r}")
                print(f"        {dbg}")
            except Exception as e:
                print(f"      raw[{i}] debug error: {e}")
        # Last-ditch: enumerate delegateLoader GroupControls anywhere in
        # the window, print everything, so we can see what's actually there.
        print("    Sidebar delegateLoader scan:")
        for i in range(1, 40):
            try:
                ctrl = auto.GroupControl(
                    searchFromControl=c.window,
                    AutomationId="delegateLoader",
                    foundIndex=i,
                    searchDepth=4,
                )
                if not ctrl.Exists(0.0, 0):
                    break
                cls = (ctrl.ClassName or "")
                bn = _read_bounds_live(ctrl)
                bs = f"({bn[0]},{bn[1]},{bn[2]},{bn[3]})" if bn else "no-rect"
                nm = (ctrl.Name or "").strip()
                print(f"      [{i}] name={nm!r:20s} bounds={bs} cls={cls!r}")
                if bn is None:
                    # Dump per-path diagnostic on the first row that still
                    # has no rect — this is the row we actually need to click.
                    print(f"        {_debug_bounds_all(ctrl)}")
            except Exception as e:
                print(f"      [{i}] error: {e}")
                break
        return
    r0, bn, _cls, _nm = unique[0]
    cx, cy = bn[0] + 50, bn[1] + 30
    print(f"    Clicking ({cx},{cy}) — first of {len(unique)} unique rows (name={_nm!r})")
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
        print("  python viber_client.py --inspect-chat <contact-name>")
