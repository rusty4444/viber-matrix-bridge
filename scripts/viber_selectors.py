"""
Viber Desktop UI element selectors.

These values describe where to find key parts of the Viber Desktop window via
Windows UI Automation. Viber updates frequently shift control names — if the
bridge stops working, run `python viber_client.py --inspect` and/or use
Microsoft's "Accessibility Insights for Windows" to re-identify these controls,
then update the values below.

Fields:
  name        — exact (or regex-matched) Name property of the UIA control
  control_type — uiautomation control type (e.g. 'ListControl', 'EditControl')
  automation_id — optional AutomationId if the control exposes one
  fallback     — plain-English description for logs when not found
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Selector:
    control_type: str
    name: Optional[str] = None
    automation_id: Optional[str] = None
    class_name: Optional[str] = None
    regex_name: bool = False
    fallback: str = ""


# Main window -------------------------------------------------------------
MAIN_WINDOW = Selector(
    control_type="WindowControl",
    class_name="Qt5QWindowIcon",   # Viber historically uses Qt
    regex_name=True,
    name=r".*Viber.*",
    fallback="Viber main window (Qt5QWindowIcon)",
)

# Left-hand conversation list --------------------------------------------
# The list of chats on the left side of the Viber window.
CONVERSATION_LIST = Selector(
    control_type="ListControl",
    automation_id="chatListView",
    fallback="Left-side chat list",
)

# A single row in the conversation list.
# The Name property typically contains the contact/group name plus last message.
CONVERSATION_ITEM = Selector(
    control_type="ListItemControl",
    fallback="Chat row (list item)",
)

# Unread indicator inside a conversation item (a small badge).
UNREAD_BADGE = Selector(
    control_type="TextControl",
    automation_id="unreadCountLabel",
    fallback="Unread count badge on chat row",
)

# Chat pane (right side) -------------------------------------------------
CHAT_HEADER_TITLE = Selector(
    control_type="TextControl",
    automation_id="chatHeaderTitle",
    fallback="Chat header / active contact name",
)

# The scrollable area that holds messages in the current chat.
MESSAGE_LIST = Selector(
    control_type="ListControl",
    automation_id="messageListView",
    fallback="Message list in open chat",
)

# Individual message bubble. We look inside for sender + text + timestamp.
MESSAGE_ITEM = Selector(
    control_type="ListItemControl",
    fallback="Message bubble",
)

# The text input box at the bottom.
INPUT_BOX = Selector(
    control_type="EditControl",
    automation_id="messageInputField",
    fallback="Bottom message input box",
)

# Send button (we prefer Enter key, but this is a fallback).
SEND_BUTTON = Selector(
    control_type="ButtonControl",
    automation_id="sendButton",
    fallback="Send button",
)

# Known message metadata patterns -----------------------------------------
# Viber marks bubbles as "outgoing" (from me) with a specific class / property.
# Adjust after inspecting live UI.
OUTGOING_HINTS = ["outgoing", "sent", "me"]
INCOMING_HINTS = ["incoming", "received"]
