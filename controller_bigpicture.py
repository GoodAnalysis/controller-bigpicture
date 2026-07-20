#!/usr/bin/env python3
"""controller-bigpicture - open Steam Big Picture when an Xbox controller turns on.

Two ways to summon Big Picture:

  * Turn a controller on. The watcher polls XInput and fires on a genuine
    "none connected" -> "connected" transition.
  * Double-tap the Guide (Xbox) button. Explicit, so it ignores the guards
    below and works even mid-game.

Guards stop the automatic trigger from interrupting something you're playing.
A pad that idles out and reconnects, or a Steam Input handoff, must not throw
Big Picture over a running game - that pauses it:

  * A dropout must persist for several consecutive polls before it counts as a
    disconnect. XInput lies briefly, and a single bad poll otherwise reads as
    unplug + replug.
  * Nothing fires while a Steam game is running (even alt-tabbed or minimised),
    or while any fullscreen app owns the foreground.
  * Launches are rate-limited.

Also warns when a wireless pad's battery gets critical, and parks the mouse
pointer out of the way when Big Picture opens.

Windows only. Run it with native Windows Python - XInput is a host API and is
not reachable from inside WSL.

Usage:
    python controller_bigpicture.py              # watch; ignore a pad already on at start
    python controller_bigpicture.py --launch-now # also fire if a pad is already on
    python controller_bigpicture.py --wake       # also wake the display / dismiss screensaver
    python controller_bigpicture.py --no-guard   # launch even over a game / fullscreen app
    python controller_bigpicture.py --no-park    # leave the mouse pointer where it is
    python controller_bigpicture.py --log        # write a log to %LOCALAPPDATA%
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import winreg
from datetime import datetime

# --- configuration ---------------------------------------------------------

# XInput supports up to four controller slots (0-3).
XINPUT_MAX_CONTROLLERS = 4

# XInputGetState success return code.
ERROR_SUCCESS = 0

# The loop ticks fast so it can see a Guide double-tap; the slower jobs below
# are done every N ticks rather than every tick.
TICK_SECONDS = 0.05
CONNECT_CHECK_TICKS = 20    # -> once a second
BATTERY_CHECK_TICKS = 1200  # -> once a minute

# Consecutive "nothing connected" checks required before we believe the pad
# really went away. At one check a second this rides out dropouts of up to ~5s,
# which covers the Steam Input handoff and ordinary wireless blips.
DISCONNECT_CONFIRM_POLLS = 5

# Hard floor between two launches, whatever the detector claims.
RELAUNCH_COOLDOWN_SECONDS = 30.0

# Two Guide presses closer together than this count as a double-tap.
DOUBLE_TAP_WINDOW_SECONDS = 0.4

# Only inject the screensaver-dismissing keypress if the session looks idle.
# Injecting it into an active game is how you get phantom inputs.
IDLE_BEFORE_KEYPRESS_SECONDS = 60.0

# Don't nag more than once an hour about the same flat pad.
BATTERY_WARN_COOLDOWN_SECONDS = 3600.0

# steam://open/bigpicture hands off to Steam's protocol handler and opens the
# Big Picture / Gamepad UI. Steam does not need to be in the foreground; if it
# is closed, the handler starts it first.
STEAM_BIGPICTURE_URL = "steam://open/bigpicture"


# --- xinput plumbing -------------------------------------------------------

class _XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_uint16),
        ("bLeftTrigger", ctypes.c_uint8),
        ("bRightTrigger", ctypes.c_uint8),
        ("sThumbLX", ctypes.c_int16),
        ("sThumbLY", ctypes.c_int16),
        ("sThumbRX", ctypes.c_int16),
        ("sThumbRY", ctypes.c_int16),
    ]


class _XInputState(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_uint32),
        ("Gamepad", _XInputGamepad),
    ]


class _XInputBatteryInformation(ctypes.Structure):
    _fields_ = [
        ("BatteryType", ctypes.c_uint8),
        ("BatteryLevel", ctypes.c_uint8),
    ]


# XInput reports battery in four coarse buckets - there is no percentage in the
# API at all, so "warn under 10%" is approximated by EMPTY, the lowest bucket.
# Set BATTERY_WARN_AT to BATTERY_LEVEL_LOW for an earlier, chattier warning.
BATTERY_LEVEL_EMPTY = 0x00
BATTERY_LEVEL_LOW = 0x01
BATTERY_WARN_AT = BATTERY_LEVEL_EMPTY

# Wired pads and empty slots have no battery worth reporting.
BATTERY_TYPE_DISCONNECTED = 0x00
BATTERY_TYPE_WIRED = 0x01
BATTERY_DEVTYPE_GAMEPAD = 0x00

# Guide (Xbox) button. Absent from the documented wButtons flags because the
# published XInputGetState masks it out - see load_get_state_ex.
XINPUT_GAMEPAD_GUIDE = 0x0400


def load_xinput() -> "ctypes.WinDLL":
    """Load the newest XInput runtime present on this machine.

    xinput1_4 ships with Windows 8 and later. Older systems may only have the
    DirectX-redistributable 1_3 or the 9.1.0 stub, so we fall back in order.
    """
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return ctypes.windll.LoadLibrary(name)
        except OSError:
            continue
    raise RuntimeError(
        "No XInput DLL found. This script must run on native Windows "
        "(not WSL), where xinput1_4/1_3 is available."
    )


def load_get_state_ex(xinput: "ctypes.WinDLL"):
    """Bind XInputGetStateEx, exported only as ordinal 100.

    It is identical to XInputGetState except it does not mask off the Guide
    button, which is the whole point. Undocumented but stable since 2007, and
    present in xinput1_3 / xinput1_4. The 9.1.0 stub lacks it, so this can
    return None and the caller falls back to no Guide support.
    """
    try:
        proto = ctypes.WINFUNCTYPE(
            ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_XInputState)
        )
        return proto((100, xinput))
    except (AttributeError, ValueError, OSError):
        return None


def connected_slots(xinput: "ctypes.WinDLL") -> "list[int]":
    state = _XInputState()
    return [
        slot
        for slot in range(XINPUT_MAX_CONTROLLERS)
        if xinput.XInputGetState(slot, ctypes.byref(state)) == ERROR_SUCCESS
    ]


def any_controller_connected(xinput: "ctypes.WinDLL") -> bool:
    return bool(connected_slots(xinput))


def guide_is_down(get_state_ex) -> bool:
    """True if the Guide button is held on any connected pad."""
    if get_state_ex is None:
        return False
    state = _XInputState()
    for slot in range(XINPUT_MAX_CONTROLLERS):
        if get_state_ex(slot, ctypes.byref(state)) == ERROR_SUCCESS:
            if state.Gamepad.wButtons & XINPUT_GAMEPAD_GUIDE:
                return True
    return False


def battery_level(xinput: "ctypes.WinDLL", slot: int) -> "int | None":
    """Battery bucket for a slot, or None if wired / absent / unsupported."""
    info = _XInputBatteryInformation()
    try:
        rc = xinput.XInputGetBatteryInformation(
            slot, BATTERY_DEVTYPE_GAMEPAD, ctypes.byref(info)
        )
    except AttributeError:
        return None  # xinput9_1_0 doesn't export it
    if rc != ERROR_SUCCESS:
        return None
    if info.BatteryType in (BATTERY_TYPE_DISCONNECTED, BATTERY_TYPE_WIRED):
        return None
    return info.BatteryLevel


def launch_big_picture() -> None:
    # os.startfile uses ShellExecute, which respects the steam:// protocol
    # handler. (webbrowser.open would try a browser for non-http URLs.)
    os.startfile(STEAM_BIGPICTURE_URL)


# --- win32 structures and signatures ---------------------------------------

class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_uint32),
    ]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("dwTime", ctypes.c_uint32),
    ]


_user32_ready = False


def user32() -> "ctypes.WinDLL":
    """user32 with the signatures we need declared.

    Handles are pointer-sized. Without an explicit restype ctypes assumes a C
    int and silently truncates them on 64-bit, so every HWND comparison below
    would be garbage.
    """
    global _user32_ready
    u = ctypes.windll.user32
    if not _user32_ready:
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetShellWindow.restype = ctypes.c_void_p
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
        _user32_ready = True
    return u


# --- "is a game already on?" -----------------------------------------------

# SHQueryUserNotificationState results we treat as "do not interrupt".
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4

MONITOR_DEFAULTTONEAREST = 2


def steam_running_appid() -> int:
    """The appid of the Steam game currently running, or 0 for none.

    Steam keeps this up to date in the registry. It is the strongest signal we
    have, and unlike the foreground checks it stays true for a game that is
    alt-tabbed, minimised or on another monitor.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg.QueryValueEx(key, "RunningAppID")
        return int(value)
    except (OSError, ValueError, TypeError):
        return 0


def _shell_says_busy() -> bool:
    """Ask the shell whether a fullscreen D3D app or presentation is running.

    This is the reliable signal for exclusive-fullscreen games. Borderless
    windowed games often don't set it, which is why we measure geometry too.
    """
    state = ctypes.c_int()
    # S_OK is 0. Anything else (no shell, RDP session, ...) -> assume not busy.
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return False
    return state.value in (
        QUNS_BUSY,
        QUNS_RUNNING_D3D_FULL_SCREEN,
        QUNS_PRESENTATION_MODE,
    )


def _foreground_covers_monitor() -> bool:
    """True if the foreground window fills its monitor and isn't the desktop."""
    u = user32()
    hwnd = u.GetForegroundWindow()
    # No foreground window, or the desktop itself, means nothing to interrupt.
    if not hwnd or hwnd == u.GetShellWindow():
        return False

    rect = _RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False

    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    monitor = u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not u.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False

    m = info.rcMonitor
    return (
        rect.left <= m.left
        and rect.top <= m.top
        and rect.right >= m.right
        and rect.bottom >= m.bottom
    )


def blocked_reason() -> "str | None":
    """Why we should not auto-launch right now, or None if it's fine."""
    appid = steam_running_appid()
    if appid:
        return f"Steam game running (appid {appid})"
    if _shell_says_busy():
        return "fullscreen app in foreground"
    if _foreground_covers_monitor():
        return "foreground window fills the screen"
    return None


# --- display waking / cursor -----------------------------------------------

SPI_GETSCREENSAVERRUNNING = 0x0072
SM_CXSCREEN = 0
SM_CYSCREEN = 1


def _screensaver_running() -> bool:
    running = ctypes.c_int()
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_GETSCREENSAVERRUNNING, 0, ctypes.byref(running), 0
    )
    return bool(ok and running.value)


def _idle_seconds() -> float:
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    # GetTickCount wraps roughly every 49 days; mask to 32 bits so the
    # subtraction stays positive across the wrap.
    tick = ctypes.windll.kernel32.GetTickCount()
    return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0


def park_cursor() -> None:
    """Shove the pointer into the bottom-right corner.

    Big Picture is driven by the pad, so a pointer sat in the middle of the TV
    is just a smudge on the screen.
    """
    u = ctypes.windll.user32
    u.SetCursorPos(u.GetSystemMetrics(SM_CXSCREEN) - 1, u.GetSystemMetrics(SM_CYSCREEN) - 1)


def wake_display(log) -> None:
    """Turn the monitor back on and dismiss a running screensaver.

    This can NOT bypass a password/PIN lock screen - that is a Windows security
    boundary no user-space script can cross. It only helps when the session is
    unlocked underneath (monitor asleep or screensaver running), e.g. a couch /
    HTPC set to not require sign-in. If the session is genuinely locked, the most
    this does is light up the monitor showing the lock screen.
    """
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    KEYEVENTF_KEYUP = 0x0002
    VK_F15 = 0x7E  # exists in the API but does nothing visible in practice

    # Resetting the idle timer is inert - it never reaches the foreground app -
    # so it is always safe to do. This is what powers the monitor back on.
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED
    )

    # The keypress is NOT inert: injected input goes to whatever has focus. Only
    # send it when the session actually looks asleep, otherwise we are firing a
    # phantom key into a running game.
    if not (_screensaver_running() or _idle_seconds() >= IDLE_BEFORE_KEYPRESS_SECONDS):
        log("display already awake -> skipping keypress")
        return

    u = ctypes.windll.user32
    u.keybd_event(VK_F15, 0, 0, 0)
    u.keybd_event(VK_F15, 0, KEYEVENTF_KEYUP, 0)


# --- toast notifications ---------------------------------------------------

# Borrowing PowerShell's registered AppUserModelID. A toast needs a shortcut in
# the Start Menu registered to its AppID or Windows silently drops it, and
# PowerShell already has one.
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# Text is passed through the environment, not interpolated into the script, so
# there is nothing to quote or escape.
_TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$text = $xml.GetElementsByTagName('text')
$text.Item(0).AppendChild($xml.CreateTextNode($env:TOAST_TITLE)) > $null
$text.Item(1).AppendChild($xml.CreateTextNode($env:TOAST_BODY)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:TOAST_APP_ID).Show($toast)
"""

CREATE_NO_WINDOW = 0x08000000


def show_toast(title: str, body: str, log) -> None:
    """Raise a Windows toast. Never steals focus, so it is safe mid-game."""
    env = dict(
        os.environ, TOAST_TITLE=title, TOAST_BODY=body, TOAST_APP_ID=TOAST_APP_ID
    )
    try:
        subprocess.Popen(
            [
                "powershell.exe",  # WinRT types need Windows PowerShell, not pwsh
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _TOAST_SCRIPT,
            ],
            env=env,
            # Without this the console window flashes up on screen every time.
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        log(f"toast failed: {exc}")


# --- logging (optional) ----------------------------------------------------

def make_logger(enabled: bool):
    if not enabled:
        return lambda msg: None

    log_dir = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "controller-bigpicture",
    )
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "watcher.log")

    def log(msg: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        # Also echo to stdout for when run via `python` (not pythonw).
        print(line, flush=True)

    return log


# --- main loop -------------------------------------------------------------

def main() -> int:
    launch_now = "--launch-now" in sys.argv
    wake = "--wake" in sys.argv
    guard = "--no-guard" not in sys.argv
    park = "--no-park" not in sys.argv
    log = make_logger("--log" in sys.argv)

    try:
        xinput = load_xinput()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    get_state_ex = load_get_state_ex(xinput)

    def open_big_picture(why: str) -> bool:
        """Wake if asked, open Big Picture, park the pointer. True on success."""
        log(why + " -> " + ("waking + opening Big Picture" if wake else "opening Big Picture"))
        if wake:
            try:
                wake_display(log)
            except OSError as exc:
                log(f"wake failed: {exc}")
        try:
            launch_big_picture()
        except OSError as exc:
            log(f"failed to launch Big Picture: {exc}")
            return False
        if park:
            try:
                park_cursor()
            except OSError as exc:
                log(f"cursor park failed: {exc}")
        return True

    # Seed with the current state so a controller that was already on when the
    # watcher starts does NOT trigger a launch. --launch-now opts into firing on
    # that first detection instead (handy if you boot with the pad already on).
    was_connected = False if launch_now else any_controller_connected(xinput)
    missed_polls = 0
    last_launch = None

    guide_was_down = False
    last_guide_tap = None

    # slot -> monotonic time we last warned, so the nag can be rate-limited.
    battery_warned: "dict[int, float]" = {}

    log(
        f"watching (launch_now={launch_now}, wake={wake}, guard={guard}, park={park}, "
        f"guide={'yes' if get_state_ex else 'unavailable'}, seeded connected={was_connected})"
    )

    tick = 0
    while True:
        tick += 1

        # --- Guide double-tap: explicit, so it ignores the guards ----------
        guide_down = guide_is_down(get_state_ex)
        if guide_down and not guide_was_down:
            now = time.monotonic()
            if last_guide_tap is not None and now - last_guide_tap <= DOUBLE_TAP_WINDOW_SECONDS:
                last_guide_tap = None  # consume, so a third tap doesn't re-fire
                if open_big_picture("guide double-tap"):
                    last_launch = now
            else:
                last_guide_tap = now
        guide_was_down = guide_down

        # --- controller connect/disconnect --------------------------------
        if tick % CONNECT_CHECK_TICKS == 0:
            if any_controller_connected(xinput):
                missed_polls = 0
                if not was_connected:
                    # Latch immediately: whatever we decide below, this
                    # connection is handled and must not be reconsidered.
                    was_connected = True
                    now = time.monotonic()
                    reason = blocked_reason() if guard else None

                    if last_launch is not None and now - last_launch < RELAUNCH_COOLDOWN_SECONDS:
                        log("controller connected -> skipped (cooldown)")
                    elif reason:
                        log(f"controller connected -> skipped ({reason})")
                    elif open_big_picture("controller connected"):
                        last_launch = now
            else:
                # One bad poll is not a disconnect - XInput blips constantly
                # while a game is running. Require several in a row.
                missed_polls += 1
                if was_connected and missed_polls >= DISCONNECT_CONFIRM_POLLS:
                    log(f"controller disconnected (confirmed after {missed_polls} polls)")
                    was_connected = False

        # --- battery ------------------------------------------------------
        if tick % BATTERY_CHECK_TICKS == 0:
            now = time.monotonic()
            for slot in connected_slots(xinput):
                level = battery_level(xinput, slot)
                if level is None:
                    continue
                if level > BATTERY_WARN_AT:
                    battery_warned.pop(slot, None)  # re-arm once it's charged
                    continue
                warned_at = battery_warned.get(slot)
                if warned_at is None or now - warned_at >= BATTERY_WARN_COOLDOWN_SECONDS:
                    battery_warned[slot] = now
                    log(f"controller {slot} battery critical (level {level})")
                    show_toast(
                        "Controller battery critical",
                        f"Pad in slot {slot + 1} is about to die. Charge it or swap the batteries.",
                        log,
                    )

        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
