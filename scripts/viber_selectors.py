"""
Viber Desktop UI element selectors.

These selectors describe where to find key parts of the Viber Desktop window
via Windows UI Automation. Viber ships as a Qt/QML application
("Rakuten Viber"), and its controls expose auto-generated class names like
`SideBarContent_QMLTYPE_752`. The trailing number can shift between Viber
releases, so most selectors match the *prefix* via regex.

How to refresh these after a Viber update
-----------------------------------------
    python viber_client.py --inspect
    python viber_client.py --inspect-subtree SideBarContent
    python viber_client.py --inspect-subtree StackView

and/or use Microsoft's "Accessibility Insights for Windows".

Fields
------
    control_type : UIA control type (e.g. "WindowControl", "EditControl")
    name         : exact Name property, or regex if regex_name=True
    automation_id: substring match on AutomationId if set
    class_name   : regex-matched against ClassName (QML class names include
                   an auto-generated numeric suffix that shifts across
                   releases, so we match the prefix)
    regex_name   : if True, `name` is treated as a regex
    regex_class  : if True, `class_name` is treated as a regex (default True
                   for Qt QML classes)
    fallback     : plain-English description for logs when not found
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
# Title is "Rakuten Viber" on current desktop builds; older/localized builds
# may just say "Viber". Regex keeps both working.
MAIN_WINDOW = Selector(
    control_type="WindowControl",
    regex_name=True,
    name=r".*Viber.*",
    class_name=r"MainWindow_QMLTYPE_\d+",
    fallback="Viber main window (MainWindow_QMLTYPE_*)",
)

# The big ApplicationWindowContentControl GroupControl that holds everything.
APP_CONTENT = Selector(
    control_type="GroupControl",
    automation_id="ApplicationWindowContentControl",
    class_name=r"QQuickControl",
    regex_class=True,
    fallback="ApplicationWindowContentControl",
)

# The split view separating the left sidebar (chat list) from the right pane
# (active chat).
SPLIT_VIEW = Selector(
    control_type="PaneControl",
    class_name=r"SplitView_QMLTYPE_\d+",
    fallback="SplitView between sidebar and active chat",
)

# Left side: the pane containing the chat list + search.
SIDEBAR_CONTENT = Selector(
    control_type="PaneControl",
    class_name=r"SideBarContent_QMLTYPE_\d+",
    fallback="Left sidebar content",
)

# Right side: the stacked pane showing the currently open conversation.
CHAT_STACK = Selector(
    control_type="PaneControl",
    class_name=r"StackView_QMLTYPE_\d+",
    fallback="Right-side StackView (active chat)",
)


# ---------------------------------------------------------------------------
# Sidebar / conversation list
# ---------------------------------------------------------------------------
# Top search field at the top of the sidebar.
SEARCH_BOX = Selector(
    control_type="EditControl",
    class_name=r"TextFieldItem_QMLTYPE_\d+",
    fallback="Sidebar search box (TextFieldItem)",
)

# Individual rows in the conversation list.
# Viber wraps each row in a QML "ListViewDelegateLoader".
CONVERSATION_ITEM = Selector(
    control_type="GroupControl",
    class_name=r"ListViewDelegateLoader_QMLTYPE_\d+",
    fallback="Chat row (ListViewDelegateLoader)",
)

# The "select this row" clickable element inside a delegate loader.
# Often a GroupControl one level deeper — viber_client.py searches inside each
# CONVERSATION_ITEM for any control with a Name property.
CONVERSATION_ITEM_LABEL = Selector(
    control_type="TextControl",
    fallback="Text inside a chat row (contact name / last message)",
)

# Unread badge on a conversation row. Not yet confirmed from inspect output;
# will be a small TextControl whose Name is a digit (e.g. "3").
UNREAD_BADGE = Selector(
    control_type="TextControl",
    regex_name=True,
    name=r"^\d+$",
    fallback="Unread count badge (numeric TextControl)",
)


# ---------------------------------------------------------------------------
# Active chat (right pane)
# ---------------------------------------------------------------------------
# Header at the top of the active chat — contact / group name.
# Needs inspect-subtree confirmation. Keeping a broad match until then.
CHAT_HEADER_TITLE = Selector(
    control_type="TextControl",
    fallback="Chat header / active contact name (TBD from inspect-subtree StackView)",
)

# Scrollable list of messages. Likely a ListView-like QML class.
MESSAGE_LIST = Selector(
    control_type="ListControl",
    class_name=r"(ChatListView|MessageListView|ListView)_QMLTYPE_\d+",
    fallback="Message list (TBD from inspect-subtree StackView)",
)

# Individual message bubble within the message list.
MESSAGE_ITEM = Selector(
    control_type="GroupControl",
    class_name=r"(ChatMessage|MessageDelegate|Bubble).*_QMLTYPE_\d+",
    fallback="Message bubble (TBD)",
)

# Message input box at the bottom of the active chat.
# We already see TextFieldItem_QMLTYPE_94 used for the search box — the input
# box is typically a different TextArea-style QML type. Confirm with inspect.
INPUT_BOX = Selector(
    control_type="EditControl",
    class_name=r"(TextAreaItem|ChatInput|MessageInput).*_QMLTYPE_\d+",
    fallback="Message input box (TBD from inspect-subtree StackView)",
)

# Send button is sometimes present, sometimes Viber just uses Enter.
SEND_BUTTON = Selector(
    control_type="ButtonControl",
    class_name=r"(SendButton|IconButton).*_QMLTYPE_\d+",
    fallback="Send button (may not exist; Enter key is the fallback)",
)


# ---------------------------------------------------------------------------
# Message direction hints
# ---------------------------------------------------------------------------
# Substrings searched against ClassName / AutomationId of message bubbles to
# classify outgoing vs incoming. To be tuned after observing real bubbles.
OUTGOING_HINTS = ["outgoing", "sent", "mymessage", "ownmessage", "sender"]
INCOMING_HINTS = ["incoming", "received", "peermessage", "othermessage"]
