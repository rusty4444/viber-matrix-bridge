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
             │ viber-bridge.py      │ ◄──────────────────► │ Synapse on DS218   │
             │ (Windows service)    │   @viber:samprim.net │ matrix.samprim.net │
             └──────────────────────┘                      └────────────────────┘
```

- A dedicated Matrix user `@viber:samprim.net` posts & receives on your behalf
- Each Viber conversation gets its own Matrix room (auto-created)
- A bootstrap **control room** lets you run `!list`, `!pair`, `!status` commands
- State (room ↔ conversation mappings, dedup cache) in a local SQLite DB

## Components in this folder

```
viber-bridge/
├── README.md                           ← this file
├── matrix-setup/
│   └── register-viber-user.sh          ← run on DS218 to create @viber user
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

### 1. Register the bridge user on Synapse (DS218)

SSH to the DS218 and run the helper script, or manually:

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
  https://matrix.samprim.net/_matrix/client/v3/login
```

### 2. Invite `@viber:samprim.net` to a control room

In Element (as `@sam.russell:samprim.net`):
1. Create a new **private** room named something like "Viber Control"
2. Invite `@viber:samprim.net`
3. Copy the room ID (Room Settings → Advanced → Internal room ID, looks like `!abc123:samprim.net`)
4. Paste into `config.yaml` as `matrix.control_room_id`

**Then accept the invite on the bridge user's behalf** — the @viber account has no UI, so you do it with a single API call. Use the helper:

```bash
bash matrix-setup/accept-invite.sh '!abc123:samprim.net' 'syt_dmliZXI_xxx...'
```

(You'll run this again for any future room you invite `@viber` into manually.)

### 3. Create the working folder on Windows

On the Windows Media Server:

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

### 4. Install Viber Desktop on the Windows Media Server

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
- `[matrix] connected as @viber:samprim.net`
- `[viber] attached to Viber window`
- `[control] posted ready message in !abc:samprim.net`

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
| `!status` | Show uptime, Viber connection state, last message timestamps |
| `!list` | List Viber conversations detected and their mapped rooms |
| `!pair <#room-alias or !id> <viber-conversation-name>` | Manually pair a Matrix room to a Viber chat |
| `!unpair <viber-conversation-name>` | Remove a pairing |
| `!reload` | Re-scan Viber conversation list |
| `!help` | Show commands |

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
