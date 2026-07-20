# controller-bigpicture

Open Steam **Big Picture** automatically the moment you turn an Xbox controller on.

There is no built-in Steam setting for this. Valve's "Guide button focuses Steam"
trick needs Steam already running and a manual double-tap of the Xbox button. This
is a tiny background watcher that does it for real: it polls **XInput**, so it sees
the pad whether it connects over Bluetooth, the Xbox wireless dongle, or USB, and
fires Steam's Big Picture the instant a new controller appears.

> **Windows only.** It must run on native Windows Python &mdash; XInput is a host API
> and is invisible from inside WSL.

## Requirements

- Windows 10 or 11
- [Python for Windows](https://www.python.org/downloads/windows/) 3.8+, installed
  with **"Add python.exe to PATH"** checked
- Steam installed (Steam registers the `steam://` protocol handler)

No third-party packages &mdash; the watcher uses only the Python standard library.

## Quick start (test it)

```powershell
python controller_bigpicture.py
```

Leave that running and turn your controller on. Big Picture should open. Run it with
`python` (not `pythonw`) for this first test so you can see any error in the console.
Press `Ctrl+C` to stop.

## Run it silently at login

Install a Startup entry that launches the watcher with `pythonw.exe` (no console
window):

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

That drops a shortcut in your Startup folder, so it starts automatically every time
you sign in. The installer also prints a command to start it immediately without
rebooting.

To also wake the monitor on connect, or log to a file, pass the matching switches:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Wake        # wake display too
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Wake -Log   # wake + log
```

To remove it:

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```

## Options

| Flag           | Effect |
| -------------- | ------ |
| _(none)_       | Watch for a **new** connection. A pad that is already on when the watcher starts is ignored, so rebooting with the controller on won't relaunch Big Picture. |
| `--launch-now` | Also fire if a controller is already connected at start. Use this if you usually boot with the pad already on and want Big Picture then too. |
| `--wake`       | On connect, also wake the monitor and dismiss the screensaver before opening Big Picture. **Cannot** bypass a password/PIN lock &mdash; see **Waking the screen** below. |
| `--no-guard`   | Open Big Picture even when a game is running or a fullscreen app owns the foreground. Off by default &mdash; see **Not interrupting your game** below. |
| `--no-park`    | Leave the mouse pointer where it is. By default it is parked in the bottom-right corner when Big Picture opens. |
| `--log`        | Write a timestamped log to `%LOCALAPPDATA%\controller-bigpicture\watcher.log`. Handy for confirming the silent `pythonw` instance is alive. |

## Waking the screen (`--wake`)

With `--wake`, the watcher also turns the monitor back on and dismisses a running
screensaver the moment the controller connects, so a couch PC goes from dark screen
straight to Big Picture.

**The important limit:** no user-space script can get past the Windows **password /
PIN lock screen** &mdash; that's a deliberate security boundary, not something to work
around. `--wake` only helps when the session is *unlocked underneath* (monitor asleep
or screensaver running). If the PC is genuinely locked, the most it does is light up
the monitor showing the lock screen; you still sign in.

To make a living-room PC go all the way to the desktop hands-free, change the Windows
setting rather than the script:

- **Settings &rarr; Accounts &rarr; Sign-in options &rarr; "If you've been away, when
  should Windows require you to sign in again?" &rarr; Never.**
- If a screensaver is set, untick **"On resume, display logon screen"** (Screen Saver
  Settings).
- For a secure auto-unlock that *does* satisfy the lock screen, use **Windows Hello**
  (face / fingerprint) &mdash; a controller can't supply that, but Hello can.

Storing your password to auto-type it is **not** supported here: it's a real security
risk and defeats the point of the lock.

## How it works

XInput exposes up to four controller slots. Once a second the watcher asks XInput
whether any slot is connected. On a transition from "none" to "connected" it calls
`os.startfile("steam://open/bigpicture")`, which hands off to Steam's protocol
handler (starting Steam first if it isn't running).

## Summon it on demand: Guide double-tap

Double-tap the **Guide** (Xbox) button and Big Picture opens, whatever is running.
This is explicit, so unlike the automatic trigger it ignores the guards below.

The Guide button is deliberately masked out of the documented `XInputGetState`.
Reading it needs `XInputGetStateEx`, exported only as **ordinal 100** &mdash;
undocumented, but stable since 2007 and present in `xinput1_3` / `xinput1_4`. On the
ancient `xinput9_1_0` stub it is missing, and the watcher logs `guide=unavailable`
and carries on with connect-detection only.

## Not interrupting your game

Naively, "did a controller just appear?" is a trap. **XInput lies briefly.** Steam
Input hides the physical pad and substitutes a virtual one when a game launches or
changes input config, and wireless pads drop out when they idle-power-off and
reconnect. A watcher that trusts a single failed poll sees unplug + replug, opens
Big Picture on top of your running game, and the game pauses because it lost focus.

Guards, in the order they are checked:

1. **Debounce** &mdash; a dropout must persist for 5 consecutive polls (~5s) before it
   counts as a disconnect, so blips never reset the "connected" latch.
2. **Game guard** &mdash; nothing fires while a Steam game is running. Read from
   `HKCU\Software\Valve\Steam\RunningAppID`, which stays true for a game that is
   alt-tabbed, minimised, or on another monitor &mdash; cases the foreground checks
   below would miss.
3. **Fullscreen guard** &mdash; checked two ways, since exclusive-fullscreen and
   borderless windowed games report differently: `SHQueryUserNotificationState`
   catches D3D fullscreen, and a window-rect-vs-monitor comparison catches
   borderless.
4. **Cooldown** &mdash; at most one launch per 30 seconds regardless.

`--wake`'s screensaver keypress is gated too: it is only injected when the
screensaver is actually running or the session has been idle 60s+. Otherwise it
would fire a phantom keypress straight into whatever you're playing.

Pass `--no-guard` to disable guards 2 and 3.

## Battery warning

A toast appears when a wireless pad's battery hits the bottom bucket. Toasts never
steal focus, so this is safe mid-game.

**On the threshold:** XInput has no battery percentage. `XInputGetBatteryInformation`
reports one of four coarse buckets only &mdash; `EMPTY`, `LOW`, `MEDIUM`, `FULL` &mdash;
so an exact "warn under 10%" is not expressible. The warning fires at `EMPTY`, the
lowest bucket. Set `BATTERY_WARN_AT` to `BATTERY_LEVEL_LOW` for an earlier, chattier
warning. Wired pads report no battery and are skipped.

## Troubleshooting

- **Nothing happens when I run it.** Make sure you're using native Windows Python,
  not WSL. From PowerShell, `python -c "import os; print(os.name)"` must print `nt`.
- **`No XInput DLL found`.** Very old Windows only. `xinput1_3` ships with the
  DirectX End-User Runtime; install that and retry.
- **Big Picture doesn't open but there's no error.** Confirm the URL works at all:
  paste `steam://open/bigpicture` into Win+R and press Enter. If that does nothing,
  the issue is Steam's protocol handler, not this script.
- **It opens Big Picture on every reboot.** You're probably passing `--launch-now`,
  or your controller reports connected at login. Drop the flag &mdash; the default
  already ignores an already-on pad.
- **`--wake` lights the monitor but I still see the lock screen.** Expected when the
  session requires a password/PIN. See **Waking the screen** above.

## Use it from your iPhone (iOS)

iOS can't run a background watcher and has no Big Picture of its own, so the Windows
behaviour doesn't port directly. Two setups *do* work &mdash; full steps in
[ios/README.md](ios/README.md):

1. **Controller connects to your iPhone &rarr; open a game app** (e.g. Steam Link to
   stream your PC). A Shortcuts *Bluetooth* automation, no code required.
2. **iPhone as a remote** that opens Big Picture on the PC, using the included
   `bigpicture_server.py`:

   ```powershell
   python bigpicture_server.py                  # http://<pc-ip>:8765/bigpicture
   python bigpicture_server.py --token mysecret # require ?token=mysecret
   ```

   An iPhone Home Screen shortcut then hits that URL over your LAN.

## Not on Windows?

- **Steam Deck / SteamOS** already boots into Gamepad UI; you don't need this.
- **macOS / Linux desktop** use a different controller API (IOKit / evdev), so the
  detection would need rewriting. Open an issue and say which.

## License

MIT &mdash; see [LICENSE](LICENSE).
