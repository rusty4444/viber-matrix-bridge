# Viber ↔ Matrix Bridge (Windows UI Automation)

> 🟡 **WORKING IN LIMITED MODE — STILL BEING SHAKEN OUT** 🟡
>
> A personal Viber ↔ Matrix bridge that drives Viber Desktop via Windows
> UI Automation. **If you landed here from a search: this works for one
> person on one Viber build; do not assume it will on yours. Read the
> caveats.**
>
> **Current status (last updated 22 April 2026):**
>
> Working:
> - ✅ Matrix user registered, unencrypted control room set up (see [#9](../../issues/9))
> - ✅ Viber Desktop window attach via Qt QML class (see [#6](../../issues/6))
> - ✅ Typing into Viber's search box
> - ✅ Search-result row enumeration via `AutomationId=delegateLoader` + class prefix `ListViewDelegateLoader_QMLTYPE_465_*` + sidebar-geometry filter (robust across Viber QML-type suffix bumps — see [#20](../../issues/20), [#21](../../issues/21), [#23](../../issues/23))
> - ✅ Bounding-rectangle reads stabilised via `.Refind()` retry — works around the known `uiautomation` library bug where fresh proxies return `(0,0,0,0)` (see [#23](../../issues/23))
> - ✅ Chat-open verification after click (StackView or FeedDelegate, with right-pane click to knock Viber out of hybrid search+chat mode)
> - ✅ Reading message bubbles (all UIA text patterns: Name / Value / Text / LegacyIAccessible)
> - ✅ Geometry-based outgoing-message detection (bubble offset from chat-pane midpoint) — no longer depends on unreliable `FeedDelegate` direction hints
> - ✅ Echo suppression via content hash + single-shot consume (so a legitimate reply with the same text still gets through after the echo is dropped — see [#17](../../issues/17))
> - ✅ Matrix → Viber send to a paired chat
> - ✅ Manual `!readhere` / `!pairhere` workflow for the most reliable pairing (you navigate in Viber; bridge reads what's open)
> - ✅ Opt-in `!poll` loop with 30 s minimum interval + global asyncio lock so polling never races user commands (see [#8](../../issues/8))
>
> Partially working / needs more testing:
> - 🟡 `!addchat <name>` — search-and-click pairing. Recently fixed (see [#20](../../issues/20)/[#21](../../issues/21)/[#23](../../issues/23)); currently under shake-out for single-word and multi-word contact names.
> - 🟡 Incoming Viber → Matrix message pickup. Works via `!readhere` on demand. Continuous pickup requires `!poll on` (see below); that path needs more live runtime before I trust it.
>
> Not yet done:
> - ❌ Media (images, files, stickers, reactions) — text only
> - ❌ Reliable handling of Viber auto-updates that shift class names (current code is suffix-agnostic for `ListViewDelegateLoader_QMLTYPE_465_*` but not for every selector)
> - ❌ Windows service install is untested after the recent round of fixes
>
> For full history including every dead-end, see the issue tracker.

### Recommended workflow

1. **Pair chats first, with polling OFF.** Open Viber manually so it's focused, then in the control room run `!addchat <name>` for each contact or group you want to bridge. If `!addchat` misbehaves for a given contact, open that chat manually in Viber and run `!pairhere <name>` instead — it's the most reliable path because you pick the chat, the bridge just reads what's open.
2. **Turn polling on only after chats are paired** — `!poll on` in the control room. Polling navigates to every paired chat each cycle (minimum 30 s) and **steals Viber focus** while it does so. With no pairings, there's nothing to poll. Enabling it before pairing chats only adds noise.
3. Matrix → Viber send works regardless of `!poll` state, because it's event-driven off your Matrix message.
4. Incoming Viber → Matrix needs `!poll on`, OR you can run `!readhere` manually while a chat is open to flush its recent messages into the paired Matrix room.

`!poll status` reports the current toggle. `!poll off` disables and the loop goes idle (no navigation, no focus theft) until re-enabled or the bridge is restarted. The toggle is runtime-only — set `bridge.poll_enabled: true` in `config.yaml` if you want it on across restarts.

**Experimental** personal bridge for Viber by driving the Viber Desktop client with Windows UI Automation. Not a real mautrix bridge — this is a pragmatic workaround since Viber has no public client API.

## How it works

```
                   Viber servers
                         │
                         ▼
             ┌──────────────────────┐
             │ Viber Desktop (GUI)  │
             │ on Windows Media Svr │
             └──────────┬───────────┘
                        │ UI Automation (pywinauto + uiautomation)
                        ▼
             ┌──────────────────────┐      matrix-nio      ┌────────────────────┐
             │ viber-bridge.py      │ ◄──────────────────► │ Synapse on Synapse host   │
             │ (Windows service)    │   @viber:example.com │ matrix.example.com │
             └──────────────────────┘                      └────────────────────┘
```

- A dedicated Matrix user `@viber:example.com` posts & receives on your behalf
- Each Viber conversation gets its own Matrix room (auto-created)
- A bootstrap **control room** lets you run `!list`, `!pair`, `!status` commands
- State (room ↔ conversation mappings, dedup cache) in a local SQLite DB

## Components in this folder

```
viber-bridge/
├── README.md                           ← this file
├── matrix-setup/
│   └── register-viber-user.sh          ← run on Synapse host to create @viber user
├── scripts/
│   ├── config.example.yaml             ← copy to config.yaml and fill in
│   ├── requirements.txt                ← Python deps
│   ├── bridge.py                       ← main entrypoint
│   ├── viber_client.py                 ← Viber Desktop UIA driver
│   ├── viber_selectors.py              ← UI element selectors (tune after install)
│   ├── matrix_client.py                ← Matrix side (nio wrapper)
│   ├── state.py                        ← SQLite state store
│   ├── install-service.bat             ← NSSM service installer
│   └── uninstall-service.bat
```

## Setup — Step by step

### 1. Register the bridge user on Synapse (Synapse host)

SSH to the Synapse host and run the helper script, or manually:

```bash
sudo docker exec -it synapse register_new_matrix_user \
  -u viber -p '<strong-password>' \
  --no-admin -c /data/homeserver.yaml \
  http://localhost:8008
```

Then log in once via Element (or `curl` to `/login`) to grab an **access token** — you'll paste that into `config.yaml`.

Easy way to get a token with curl:

```bash
curl -XPOST -d '{"type":"m.login.password","user":"viber","password":"<password>"}' \
  https://matrix.example.com/_matrix/client/v3/login
```

### 2. Create an **UNENCRYPTED** control room

> 🚨 **Critical:** The control room MUST NOT be end-to-end encrypted.
> This bridge is built without libolm (see Step 5), so it cannot decrypt
> Megolm events. Matrix rooms cannot have encryption turned off once it's
> been turned on — if you pick wrong, you'll have to create a new room.
>
> Element's default for "private" rooms **is encrypted**, and the toggle
> is buried. The reliable way is to create the room via the API.

Run the helper with your **admin** access token (yours, not the bridge's):

```bash
bash matrix-setup/create-control-room.sh '<your_admin_token>' '@viber:example.com'
```

Your admin token is in Element: Settings → Help & About → Advanced → Access Token.

The helper will print the new room ID. Then:

1. Paste the room ID into `scripts/config.yaml` as `matrix.control_room_id`
2. Accept the invite as `@viber`:
   ```bash
   bash matrix-setup/accept-invite.sh '<new_room_id>' '<viber_access_token>'
   ```

(If you must do it in the Element UI: **uncheck** "Enable end-to-end encryption" when creating the room. Do this via Room Options → Security, not the default room-create dialog which hides the setting.)

### 3. Create the working folder on Windows

On the Windows host:

1. Create `C:\viber-bridge\`
2. Copy **the contents of** `scripts/` into that folder (not the `scripts` folder itself) — so you end up with:

   ```
   C:\viber-bridge\
   ├── bridge.py
   ├── viber_client.py
   ├── viber_selectors.py
   ├── matrix_client.py
   ├── state.py
   ├── config.example.yaml
   ├── requirements.txt
   ├── install-service.bat
   └── uninstall-service.bat
   ```

   The service installer batch files assume this exact layout (`C:\viber-bridge\`). If you put things elsewhere, edit the paths inside `install-service.bat`.

### 4. Install Viber Desktop on the Windows host

- Download from [viber.com](https://www.viber.com/en/download/) and sign in with your phone number
- Important: the bridge **must run in the same Windows user session** where Viber is signed in
- Disable Viber's auto-update if possible (updates can break the UI tree)
- Leave Viber running and pinned; the bridge needs the window to exist

### 5. Install Python + dependencies on Windows

Assuming Python 3.11+ is installed:

```powershell
cd C:\viber-bridge
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

> **Note:** we use `matrix-nio` *without* the `[e2e]` extra. That extra pulls
> in `python-olm` which needs libolm + a C toolchain to build on Windows.
> This bridge uses unencrypted rooms (consistent with the rest of the Matrix
> setup), so no E2E dependency is needed.

### 6. Configure

```powershell
copy config.example.yaml config.yaml
notepad config.yaml
```

Fill in: Matrix homeserver URL, access token, control room ID, Viber window title, polling intervals.

### 7. Verify Viber UI selectors

Viber's UI tree changes between versions. Run the selector test tool:

```powershell
python viber_client.py --inspect
```

It will print the top-level control tree. Compare against `viber_selectors.py` and adjust names/automation IDs if needed. Microsoft's **Accessibility Insights for Windows** is very helpful here.

### 8. First run (foreground, to watch logs)

```powershell
python bridge.py --config config.yaml
```

You should see:
- `[matrix] connected as @viber:example.com`
- `[viber] attached to Viber window`
- `[control] posted ready message in !abc:example.com`

In the control room, try `!status` to confirm the bridge is responsive.

### 9. Install as a Windows service

Once stable, install with NSSM (download NSSM first and put `nssm.exe` in the folder):

```powershell
install-service.bat
```

To remove: `uninstall-service.bat`.

**Critical:** The service must run as the logged-in user, not LocalSystem, or it can't see the Viber window. The batch file handles this — you'll be prompted for the Windows password.

## Control room commands

| Command | Effect |
|---|---|
| `!help` | Show commands |
| `!status` | Uptime, Viber attachment state, number of paired chats |
| `!list` | List currently paired chats |
| `!scan` | Count of visible conversation rows (names are not readable — see below) |
| `!readhere` | Read the last few messages from the chat currently open in Viber (diagnostic; does not create a pairing) |
| `!pairhere <viber name>` | Pair the chat currently open in Viber to a new Matrix room — **most reliable**, you navigate in Viber, bridge reads what's open |
| `!addchat <viber name>` | Search for a Viber chat by name, click the top match, then pair a new Matrix room. Can fail if UIA misbehaves on the target build — fall back to `!pairhere` |
| `!pair <!room_id> <viber name>` | Pair an existing Matrix room to a Viber chat manually (no Viber interaction) |
| `!unpair <viber name>` | Remove a pairing |
| `!test <viber name>` | Open a chat by search and read the last 5 messages (diagnostic) |
| `!poll on\|off\|status` | Toggle the incoming-message poll loop at runtime (default OFF; 30 s minimum interval; not persistent across restarts) |
| `!reload` | Re-attach to the Viber window |

### Why you have to pair chats explicitly

Viber Desktop is a Qt/QML app. Its conversation-row controls expose no
contact name or children to Windows UI Automation — we can see that N rows
exist in the sidebar, but not who each row represents. So the bridge can't
"auto-discover unread chats" the way a real mautrix bridge does.

Instead, you tell the bridge about each chat you want to bridge. Two paths:

1. **`!pairhere <name>`** (recommended). Open the chat manually in Viber,
   then run this command. The bridge reads the messages currently on
   screen and creates a Matrix room for them. Zero UIA navigation needed
   on the bridge side, so this is the least fragile.
2. **`!addchat <name>`**. The bridge types the name into Viber's search
   box, enumerates the result rows, clicks the best match, verifies the
   chat opened, and creates a Matrix room. Relies on a lot of UIA
   cooperation — works on most Viber builds but can break on new ones.

Once a chat is paired:
- **Matrix → Viber** send works immediately (event-driven).
- **Viber → Matrix** pickup requires `!poll on` (OFF by default because
  polling steals Viber focus each cycle), or run `!readhere` manually
  while the chat is open to flush recent messages.

## Limits & caveats (read these)

- **Text only** in v1. No images, files, stickers, or reactions.
- **No encryption.** Per-chat rooms are unencrypted (consistent with your other bridges).
- **Echo suppression** uses a content hash (sender-independent) plus a single-shot consume: when you send from Matrix, the bridge records the content hash; the next time that exact text appears in the paired Viber chat, it's dropped once. If the peer legitimately replies with the same text, only the first occurrence (your echo) is dropped — subsequent identical messages get through. Messages you type from Viber Desktop or your phone still forward into Matrix as normal.
- **One Viber client per account.** Viber only allows one Desktop session; if you open Viber on another PC, this one logs out.
- **Fragile.** Any Viber Desktop update can shift the UI tree and require selector tweaks.
- **Greyzone.** Automating Viber Desktop is not officially supported. For personal use it's fine, but don't scale this.

## Troubleshooting

- **"Cannot find Viber window"** → Viber isn't running, or the title changed. Check `viber.window_title` in config.
- **Service starts but nothing happens** → NSSM is probably running as SYSTEM, not your user. Reinstall service interactively.
- **Messages arrive in Matrix but not back to Viber** → Viber input box selector broke. Run `viber_client.py --inspect` and fix `viber_selectors.py`.
- **Bridge hangs after Windows lock** → Windows disables UIA for locked sessions. Use `gpedit` to keep session active, or run the Windows box headless-never-locks.
