"""
Viber Desktop UI element selectors.

Real values observed from live Viber Desktop (build with window title
"Rakuten Viber"). Classes follow the pattern ``<Name>_QMLTYPE_<number>`` or
``<Name>_QMLTYPE_<number>_QML_<number>`` — the trailing numbers shift
between Viber releases, so most selectors use regex on the name prefix.

Key architectural notes (these caused us pain):
  * ``SideBarContent_QMLTYPE_752`` is the chat **info / details panel** that
    slides in from the right (GIFs, Links, Files, Block, Delete). It is NOT
    the conversation list.
  * The conversation list rows render as ``ListViewDelegateLoader_*``
    GroupControls directly under ``ApplicationWindowContentControl`` (at the
    same level as the SplitView), NOT inside it. Their Name property is
    empty and they expose no UIA children — contact names are *not* readable
    from the list. To navigate to a chat we have to use the search box.
  * ``StackView_QMLTYPE_463`` is the active chat pane (right side). It
    contains the message bubbles, input box, and send button.
  * Message bubbles render as ``TextEditItem_QMLTYPE_1162_*`` EditControls
    directly inside the StackView; their text is exposed via the UIA Value
    pattern (NOT the Name property).

How to refresh these after a Viber update
-----------------------------------------
    python viber_client.py --inspect 12
    python viber_client.py --inspect-subtree StackView
    python viber_client.py --inspect-subtree ApplicationWindowContent
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Selector:
    control_type: str
    name: Optional[str] = None
    automation_id: Optional[str] = None
    class_name: Optional[str] = None
    regex_name: bool = False
    regex_class: bool = True
    fallback: str = ""


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
MAIN_WINDOW = Selector(
    control_type="WindowControl",
    regex_name=True,
    name=r".*Viber.*",
    class_name=r"MainWindow_QMLTYPE_\d+",
    fallback="Viber main window (MainWindow_QMLTYPE_*)",
)

# The big content GroupControl that holds the whole app UI.
APP_CONTENT = Selector(
    control_type="GroupControl",
    automation_id="ApplicationWindowContentControl",
    class_name=r"QQuickControl",
    fallback="ApplicationWindowContentControl",
)

# The SplitView separating the chat list column (visually on the left) from
# the active chat / info pane.
SPLIT_VIEW = Selector(
    control_type="PaneControl",
    class_name=r"SplitView_QMLTYPE_\d+",
    fallback="SplitView_QMLTYPE_*",
)

# Right-side: the active chat pane with messages + input.
CHAT_STACK = Selector(
    control_type="PaneControl",
    class_name=r"StackView_QMLTYPE_\d+",
    fallback="Active chat StackView (right pane)",
)

# Right-side info / details panel (Block, Report, Delete, GIFs, Links, Files).
# Not used for normal flow — kept here so callers can explicitly avoid it.
CHAT_INFO_PANEL = Selector(
    control_type="PaneControl",
    class_name=r"SideBarContent_QMLTYPE_\d+",
    fallback="Chat info / details panel (NOT the conversation list)",
)


# ---------------------------------------------------------------------------
# Top bar / sidebar controls (conversation list column)
# ---------------------------------------------------------------------------
# Search box — TextFieldItem directly under ApplicationWindowContentControl.
# This is our primary mechanism for navigating to a specific chat, because
# the conversation-row delegates don't expose names.
SEARCH_BOX = Selector(
    control_type="EditControl",
    class_name=r"TextFieldItem_QMLTYPE_\d+",
    fallback="Top search / filter input box (TextFieldItem_QMLTYPE_*)",
)

# Conversation list rows. These live directly under APP_CONTENT, not under
# SplitView. They have automationId='delegateLoader', Name='', and no UIA
# children. Useful for counting visible rows and clicking by index, but not
# for reading contact names.
CONVERSATION_ROW = Selector(
    control_type="GroupControl",
    automation_id="delegateLoader",
    class_name=r"ListViewDelegateLoader_QMLTYPE_\d+_QML_\d+",
    fallback="Conversation row (ListViewDelegateLoader with automationId=delegateLoader)",
)


# ---------------------------------------------------------------------------
# Active chat (StackView)
# ---------------------------------------------------------------------------
# Each message bubble is a GroupControl with class ``FeedDelegate_QMLTYPE_1077``
# — CONFIRMED via Microsoft Accessibility Insights inspection of a live chat.
# Message text is exposed through the ValuePattern (not Name). Multiple
# FeedDelegate instances are siblings directly under the StackView.
MESSAGE_ITEM = Selector(
    control_type="GroupControl",
    class_name=r"FeedDelegate_QMLTYPE_\d+",
    fallback="Message bubble (FeedDelegate_QMLTYPE_* — text via ValuePattern)",
)

# Stable class-name prefixes. Qt assigns the trailing ``_QMLTYPE_<N>`` or
# ``_QML_<N>`` numbers in registration order at process start, so they
# shift across every Viber restart. Selectors that hardcode N stop working
# after a Viber relaunch. Match by prefix instead (via _native_find_prefix).
MESSAGE_ITEM_CLASS_PREFIX = "FeedDelegate_QMLTYPE_"
STACKVIEW_CLASS_PREFIX = "StackView_QMLTYPE_"
INPUT_BOX_CLASS_PREFIX = "QQuickTextEdit"          # covers bare & _QML_N variants
SEARCH_BOX_CLASS_PREFIX = "TextFieldItem_QMLTYPE_"
CONVERSATION_ROW_CLASS_PREFIX = "ListViewDelegateLoader_QMLTYPE_"

# Deprecated exact constants. Kept as aliases to the prefixes so older
# callers still work; new code should use the _CLASS_PREFIX names above.
MESSAGE_ITEM_EXACT_CLASS = MESSAGE_ITEM_CLASS_PREFIX
STACKVIEW_EXACT_CLASS = STACKVIEW_CLASS_PREFIX
INPUT_BOX_EXACT_CLASS = INPUT_BOX_CLASS_PREFIX

# Message input box. Viber uses a plain Qt QQuickTextEdit for this — NOT a
# TextFieldItem (which is the search box) or TextEditItem (which is a
# message bubble).
INPUT_BOX = Selector(
    control_type="EditControl",
    class_name=r"QQuickTextEdit(_QML_\d+)?",
    fallback="Message input (QQuickTextEdit inside StackView)",
)

# Send button. Has a stable automationId with NO QMLTYPE number — relatively
# safe across Viber releases.
SEND_BUTTON = Selector(
    control_type="ButtonControl",
    automation_id="SendToolbarButton",
    class_name=r"SendToolbarButton",
    fallback="Send button (automationId=SendToolbarButton)",
)

# Scroll-to-bottom button in the chat. Clicking this before reading ensures
# we're looking at the newest messages.
SCROLL_TO_BOTTOM = Selector(
    control_type="ButtonControl",
    class_name=r"ScrollToBottomItem_QMLTYPE_\d+(_QML_\d+)?",
    fallback="Scroll-to-bottom button",
)


# ---------------------------------------------------------------------------
# Message direction hints
# ---------------------------------------------------------------------------
# Viber doesn't put an obvious "outgoing" / "incoming" marker on the UIA node,
# so direction detection is heuristic and may need live tuning.
OUTGOING_HINTS = ["outgoing", "sent", "mymessage", "ownmessage", "sender"]
INCOMING_HINTS = ["incoming", "received", "peermessage", "othermessage"]
