# Viber ↔ Matrix Bridge (Windows UI Automation)

> ⚠️ **THEORY / UNTESTED — v0.0.1**
>
> This is a **first-draft design** for a Viber ↔ Matrix bridge. None of it has been run against a live Viber Desktop yet. The approach is sound, but the specific UI Automation selectors (`AutomationId`, `ClassName`, etc. in [`viber_selectors.py`](scripts/viber_selectors.py)) are **educated guesses** — they will almost certainly need tweaking once Viber Desktop is installed and inspected.
>
> Plan is to try it end-to-end, report what breaks, then iterate on this repo. Issues and commits will track the actual state.

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
| `!addchat <viber name>` | Navigate to a Viber chat by name; create & pair a new Matrix room if found |
| `!pair <!room_id> <viber name>` | Pair an existing Matrix room to a Viber chat manually |
| `!unpair <viber name>` | Remove a pairing |
| `!test <viber name>` | Open a chat and read the last 5 messages (diagnostic) |
| `!reload` | Re-attach to the Viber window |

### Why you have to pair chats explicitly

Viber Desktop is a Qt/QML app. Its conversation-row controls expose no
contact name or children to Windows UI Automation — we can see that 4 rows
exist, but not who each row represents. So the bridge can't "auto-discover
 unread chats" the way a real mautrix bridge does.

Instead, you tell the bridge about each chat you want to bridge:

1. `!addchat Alice` — bridge types "Alice" into Viber's search box, clicks
   the first matching result, and if that works, creates a Matrix room for
   that chat.
2. From then on, the bridge polls that specific chat every few seconds by
   searching for its name again.

## Limits & caveats (read these)

- **Text only** in v1. No images, files, stickers, or reactions.
- **No encryption.** Per-chat rooms are unencrypted (consistent with your other bridges).
- **Echo suppression is best-effort.** If you send from your phone, the bridge will likely forward it back — dedup is by content+time hash with a 30-second window.
- **One Viber client per account.** Viber only allows one Desktop session; if you open Viber on another PC, this one logs out.
- **Fragile.** Any Viber Desktop update can shift the UI tree and require selector tweaks.
- **Greyzone.** Automating Viber Desktop is not officially supported. For personal use it's fine, but don't scale this.

## Troubleshooting

- **"Cannot find Viber window"** → Viber isn't running, or the title changed. Check `viber.window_title` in config.
- **Service starts but nothing happens** → NSSM is probably running as SYSTEM, not your user. Reinstall service interactively.
- **Messages arrive in Matrix but not back to Viber** → Viber input box selector broke. Run `viber_client.py --inspect` and fix `viber_selectors.py`.
- **Bridge hangs after Windows lock** → Windows disables UIA for locked sessions. Use `gpedit` to keep session active, or run the Windows box headless-never-locks.
