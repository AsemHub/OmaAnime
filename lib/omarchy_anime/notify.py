"""Desktop notifications whose click plays, and never a process left waiting on one.

**On Omarchy** (`omarchy-notification-send` on PATH; Omarchy 4.0.4's `/usr/bin/omarchy-notification-send`, read
2026-10-20): `omarchy-notification-send --app-name OmaAnime -u normal -t 30000 <headline> <body> --exec <bin> --trace
<t> …`.
It calls `org.freedesktop.Notifications.Notify` through `busctl` (never `notify-send`), so a dash-leading headline is
text, unless it is one of the command's own options, which it reads before the headline and which no `--` can end
(`-u`, `-t`, `-p`, `--app-name=…`, `OPTIONS`): then `OmaAnime` is the headline and the title heads the body
(`headline`). The click command rides as the `omarchy-exec-argv` hint, built from the words after `--exec` (which must
come last, as separate words). The shell's notifications service (`/usr/share/omarchy/shell/plugins/notifications/
Service.qml`, `invokePopupDefault`) runs that argv detached on a click, and keeps it in its persistence files, so a
toast restored after a shell restart, and its row in the history, stay clickable. A normal toast is on screen for
the timeout it asks, between 8 s and 30 s (`durationFor`: `normalPopupDuration` 8000, `maxPopupDuration` 30000; no
`-t` asks for nothing and gets 8 s), so `-t 30000` gives it the whole 30 s. After that the notification is in Omarchy's
history and still clickable there while it is among the newest ten (`historyLimit` 10), and after a shell restart.
The timeout drives nothing else in the shell. The command returns as soon as the notification is sent: nothing waits.

**Elsewhere** (`notify-send`, libnotify 0.8.8: `notify-send --help` and its man page): `-a OmaAnime -t EXPIRE_MS -A
default=<label> -- <title> <body>`. `-A` "Implies --wait to wait for user input" and prints the chosen action's NAME;
the action is named `default`, the identifier a click invokes ("The default action (usually invoked by clicking the
notification) should have a key named "default"", the freedesktop notification spec). The caller never waits: a
detached child (`omarchy-anime notify-wait`, its own session, no pipes) runs `notify-send`, waits at most
`CHILD_MAX_S` (past which `notify-send` is killed), and runs the action, detached.

Each notification's click runs under a fresh trace (`notify-<action>-<time>-<rand>`), logged with the sender's trace
as `parent`: two Plays pressed from one watcher are two plays, never one trace.

**The sound** (`ready_sound`), only for an episode a long wait brought and loaded paused (`send(sound=True)`): the
notification servers here play none. Omarchy's shell runs Quickshell 0.3.2's `NotificationServer` with no `sound`
capability (its capability strings: actions, body, body-hyperlinks, body-images, body-markup, icon-static,
inline-reply, persistence, action-icons; the freedesktop spec: a server that returns "sound" "must support the
"sound-file" and "suppress-sound" hints"), and `omarchy-notification-send` builds only the urgency, `omarchy-glyph`,
`image-path` and `omarchy-exec-argv` hints, so `sound-name`/`sound-file` cannot be passed. So the plugin plays one
sound file itself, detached, as Omarchy's own `hooks/battery-low.d/play-warning-sound.sample` does
(`mpv --no-video` on a freedesktop sound): `pw-play --media-role=Notification <file>` (PipeWire's player, man
`pw-cat`; `--list-containers` reads `oga`), else `mpv` with no config, no video, no terminal and no IPC of its own (so
it never touches the plugin's player). The file is the first of `SOUND_FILES` on disk (sound-theme-freedesktop's
`complete.oga`, then two others); none, or no player: no sound, logged, and the notification is sent all the same.
With Omarchy's do-not-disturb on (`DND_FILE`'s `dnd`, the shell's own `~/.local/state/omarchy/notifications.json`)
the shell silences the toast into its history, and the sound is not played either. Every sound is one `notify sound`
line: what was played with what, or why nothing was.

`OMAANIME_NOTIFY_SEND` names another program in place of `notify-send`, `OMAANIME_OMARCHY_NOTIFY` another in place
of `omarchy-notification-send`, `OMAANIME_SOUND_PLAYER` another in place of the sound's player (empty: as if it
were absent), and `OMAANIME_DND_FILE` another do-not-disturb file in place of the shell's. The tests set all four;
nothing in the plugin does.
"""
from __future__ import annotations

import html
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .core import Log

APP_NAME = "OmaAnime"
OMARCHY_SEND = "omarchy-notification-send"
# notify-send's route: how long a notification asks to stay up, and so how long its child waits for a click at most.
EXPIRE_MS = 4 * 3600 * 1000
# The child's hard lifetime on that route: `notify-send` is killed past it, whatever the server does with `-t`.
CHILD_MAX_S = EXPIRE_MS / 1000 + 60
# Omarchy's route: the command only sends; this bounds a hung bus call.
OMARCHY_TIMEOUT_S = 15.0
# Omarchy's route: how long the toast asks to stay on screen, the shell's own maximum (`maxPopupDuration`).
OMARCHY_TOAST_MS = 30000
# `omarchy-notification-send`'s options (4.0.4, `parse_omarchy_option` and `known_flag`), which it reads before the
# headline, each also as `--opt=value` for the long ones.
OPTIONS = frozenset({"-g", "--glyph", "-u", "--urgency", "--app-name", "-i", "--icon", "--image", "-r", "--replace-id",
                     "-t", "--expire-time", "-p", "--print-id"})
BIN = Path(__file__).resolve().parents[2] / "bin" / "omarchy-anime"
# The ready sound's file: the first of these on disk (sound-theme-freedesktop, which libcanberra requires; Omarchy's
# battery hook sample names the same directory).
SOUND_FILES = tuple(f"/usr/share/sounds/freedesktop/stereo/{name}.oga"
                    for name in ("complete", "message-new-instant", "bell"))
# The players tried in order, the first on PATH; `mpv` is the plugin's own requirement, so one is always there.
SOUND_PLAYERS = ("pw-play", "mpv")
# Omarchy's do-not-disturb, as the shell keeps it (`plugins/notifications/Service.qml`: `home + "/.local/state/omarchy/"
# + "notifications.json"`, `{"version": 3, "dnd": true}`): read only.
DND_FILE = Path.home() / ".local" / "state" / "omarchy" / "notifications.json"
# Module names so a test spawns nothing real.
_spawn = subprocess.Popen
_run = subprocess.run
_spawn_sound = subprocess.Popen


def notify_send() -> str:
    return os.environ.get("OMAANIME_NOTIFY_SEND") or "notify-send"


def omarchy_send() -> str | None:
    """`omarchy-notification-send`'s path when Omarchy is here, else None."""
    named = os.environ.get("OMAANIME_OMARCHY_NOTIFY")
    if named is not None:
        return named or None
    return shutil.which(OMARCHY_SEND)


def fresh_trace(action: str | None) -> str:
    return f"notify-{action or 'none'}-{int(time.time())}-{random.randrange(16**4):04x}"


def _detached(argv: list[str]) -> int | None:
    """`argv` in a session of its own with no pipes (so no caller ever waits on it). Its pid, or None."""
    try:
        proc = _spawn(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      start_new_session=True, close_fds=True)
    except OSError:
        return None
    return proc.pid


def click_argv(spec: dict) -> list[str]:
    """What a click runs: the plugin's command, under the notification's own trace, then the action's words."""
    action = (spec.get("actions") or [None])[0]
    return [str(BIN), "--trace", spec["trace"], *action["args"]] if action else []


def headline(title: str, body: str) -> tuple[str, str]:
    """The headline and body for `omarchy-notification-send`. A title it would read as one of its options (exactly an
    option, or a long option's `--opt=` form) becomes text in the body, quoted so it cannot be read as an option there
    either; the headline is then `OmaAnime`."""
    long_form = title.startswith("--") and "=" in title and title.split("=", 1)[0] in OPTIONS
    if title in OPTIONS or long_form:
        return APP_NAME, f"\u201c{title}\u201d: {body}"
    return title, body


def markup_body(body: str) -> str:
    """The body as text inside notification markup: `&`, `<` and `>` escaped, so a title's `<b>`, `<a href>` or `&`
    reads as written. The freedesktop spec lets a server read the body as markup (Omarchy 4.0.6's `NotificationCard.qml`
    draws it as `Text.StyledText`, the summary as `Text.PlainText`), so only the body is escaped: an escaped summary
    would show `&amp;`."""
    return html.escape(body, quote=False)


def omarchy_argv(command: str, spec: dict) -> list[str]:
    """`omarchy-notification-send`'s command for one notification; `--exec` last, its words separate."""
    title, body = headline(str(spec["title"]), str(spec["body"]))
    argv = [command, "--app-name", APP_NAME, "-u", "normal", "-t", str(OMARCHY_TOAST_MS), title, markup_body(body)]
    click = click_argv(spec)
    return argv + (["--exec", *click] if click else [])


def argv_for(spec: dict) -> list[str]:
    """`notify-send`'s command for one notification: the action as `default`, `--` before the positionals."""
    argv = [notify_send(), "-a", APP_NAME, "-t", str(EXPIRE_MS)]
    for a in (spec.get("actions") or [])[:1]:
        argv += ["-A", f"default={a['label']}"]
    return argv + ["--", str(spec.get("title") or APP_NAME), markup_body(str(spec.get("body") or ""))]


def sound_player() -> str | None:
    """The sound's player: `OMAANIME_SOUND_PLAYER` when set (empty: none), else the first of `SOUND_PLAYERS` on PATH."""
    named = os.environ.get("OMAANIME_SOUND_PLAYER")
    if named is not None:
        return named or None
    return next((found for found in (shutil.which(p) for p in SOUND_PLAYERS) if found), None)


def sound_argv(player: str, path: str) -> list[str]:
    """The player's command for one sound file: PipeWire's `pw-play` with the notification role; anything else (mpv,
    or a test's fake) as mpv is told: no config (no scripts, no IPC socket), no video window, no terminal output."""
    if Path(player).name == "pw-play":
        return [player, "--media-role=Notification", path]
    return [player, "--no-config", "--no-video", "--no-terminal", "--really-quiet", path]


def dnd_file() -> Path:
    """The do-not-disturb file read: `OMAANIME_DND_FILE` when set (the tests' own), else the shell's (`DND_FILE`)."""
    named = os.environ.get("OMAANIME_DND_FILE")
    return Path(named) if named else DND_FILE


def do_not_disturb() -> bool | None:
    """Omarchy's do-not-disturb (`dnd_file()`'s `dnd`): True or False, None when the file is not there or not
    readable (not Omarchy, or never set)."""
    try:
        value = json.loads(dnd_file().read_text()).get("dnd")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def ready_sound(log: Log, parent: str = "") -> dict:
    """Play the ready sound once, detached (nothing waits for it): `{played, player, file, pid, dnd, reason}`, one
    `notify sound` line. Not played with do-not-disturb on, without a sound file, or without a player."""
    t0 = time.monotonic()
    dnd = do_not_disturb()
    path = next((f for f in SOUND_FILES if Path(f).is_file()), None)
    player = sound_player()
    pid, argv = None, None
    if dnd:
        reason = "Omarchy's do-not-disturb is on: the shell silences the notification, so no sound either"
    elif path is None:
        reason = f"no sound file on disk ({', '.join(SOUND_FILES)}); the notification goes out silent"
    elif player is None:
        reason = f"no sound player on PATH ({', '.join(SOUND_PLAYERS)}); the notification goes out silent"
    else:
        argv = sound_argv(player, path)
        try:
            pid = _spawn_sound(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True, close_fds=True).pid
            reason = "the episode is ready and loaded paused; the viewer may have looked away"
        except OSError as exc:
            reason = f"the player could not be started: {type(exc).__name__}: {exc}"
    played = pid is not None
    log.emit("info" if played or dnd or path is None else "warn", "notify", "sound",
             result="PASS" if played else "SKIP", player=player, file=path, argv=argv, pid=pid, dnd=dnd,
             dnd_file=str(dnd_file()), parent=parent or getattr(log, "trace", ""), ms=(time.monotonic() - t0) * 1000,
             reason=reason,
             suggest="" if played or dnd or path is None else
             "install pipewire-audio (pw-play), or check that mpv runs; the notification went out without a sound")
    return {"played": played, "player": player, "file": path, "pid": pid, "dnd": dnd, "reason": reason}


def send(log: Log, title: str, body: str, actions: list[dict] | None = None, *, trace: str = "", why: str = "",
         sound: bool = False) -> dict:
    """One notification with at most one action (`{name, label, args, click}`: `args` a CLI command without the
    binary, `click` the sentence the body ends with, "Click to play it."). Returns at once on both routes: `sent` is
    whether it was handed to the notification server (Omarchy's route) or to its detached child (notify-send's).
    `sound`: the ready sound plays too (`ready_sound`), after the notification is handed over, sent or not."""
    action = (actions or [None])[0]
    spec = {"title": str(title), "body": " ".join(w for w in (str(body), (action or {}).get("click") or "") if w),
            "trace": fresh_trace(action and action["name"]), "parent": trace or getattr(log, "trace", ""),
            "actions": [{"name": str(action["name"]), "label": str(action["label"]),
                         "args": [str(x) for x in action["args"]]}] if action else []}
    command = omarchy_send()
    t0 = time.monotonic()
    if command:
        route, pid, error = "omarchy", None, ""
        argv = omarchy_argv(command, spec)
        try:
            run = _run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=OMARCHY_TIMEOUT_S)
            sent = run.returncode == 0
            error = "" if sent else (run.stderr or "").strip()[:300] or f"exit {run.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            sent, error = False, f"{type(exc).__name__}: {exc}"
    else:
        route, argv = "notify-send", argv_for(spec)
        pid = _detached([sys.executable, str(BIN), "--trace", spec["trace"], "notify-wait", json.dumps(spec)])
        sent, error = pid is not None, "" if pid else "the notification's child could not be started"
    log.emit("info" if sent else "warn", "notify", "send", result="PASS" if sent else "FAIL", route=route, pid=pid,
             title=title, body=spec["body"], actions=[a["name"] for a in spec["actions"]],
             click=click_argv(spec)[3:] or None, click_trace=spec["trace"], parent=spec["parent"], error=error or None,
             ms=(time.monotonic() - t0) * 1000, reason=why,
             suggest="" if sent else "the notification did not go out; the viewer is not told this time")
    out = {"sent": sent, "route": route, "pid": pid, "spec": spec, "argv": argv}
    if sound:
        out["sound"] = ready_sound(log, spec["parent"])
    return out


def wait(log: Log, spec: dict) -> dict:
    """notify-send's detached child: show the notification, wait for a click (at most `CHILD_MAX_S`), run the action
    detached. Logged: what was shown, what was chosen (or that nothing was), how long it took, the parent trace."""
    t0 = time.monotonic()
    argv = argv_for(spec)
    chosen, outcome = "", "closed"
    try:
        run = _run(argv, capture_output=True, text=True, timeout=CHILD_MAX_S)
        chosen = (run.stdout or "").strip().splitlines()[-1] if (run.stdout or "").strip() else ""
        outcome = "chosen" if chosen else ("closed" if run.returncode == 0 else f"exit {run.returncode}")
    except subprocess.TimeoutExpired:
        outcome = "capped"
    except OSError as exc:
        outcome = f"not started: {exc}"
    action = (spec.get("actions") or [None])[0] if chosen == "default" else None
    pid = _detached([sys.executable, *click_argv(spec)]) if action else None
    ok = outcome in ("closed", "capped") or (outcome == "chosen" and (action is None or pid is not None))
    log.emit("info" if ok else "warn", "notify", "answer", result="PASS" if ok else "FAIL", outcome=outcome,
             chosen=chosen or None, ran=action["args"] if action else None, pid=pid,
             ms=(time.monotonic() - t0) * 1000, cap_s=CHILD_MAX_S, title=spec.get("title"), parent=spec.get("parent"),
             reason="the viewer clicked it" if action else
                    "no action chosen: the notification was closed, expired or reached the cap",
             suggest="" if ok else ("the viewer clicked, but the play could not be started" if action else
                                    "notify-send did not show it"))
    return {"outcome": outcome, "chosen": chosen or None, "ran": action["args"] if action else None, "pid": pid}
