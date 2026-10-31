"""Desktop notifications whose click plays: one D-Bus call of the plugin's own, and nothing about the show in argv.

**The notification** is one `org.freedesktop.Notifications.Notify` call on the session bus (the Desktop Notifications
Specification 9.1.2: `susssasa{sv}i`), made by `sessionbus.call` in this process, so no other program is started and
no title, body, release or id is ever in a command line (`/proc/<pid>/cmdline` is readable by every local account).
The arguments are what Omarchy's `omarchy-notification-send` built before (4.0.6, `/usr/bin/omarchy-notification-send`,
read 2026-10-31): app name `OmaAnime`, no replaced id, no icon, the title as the summary, the body
escaped (`markup_body`), no actions, the hints `urgency` (a byte, 1 = normal) and `omarchy-exec-argv` (the click, a
JSON array of strings), and `TOAST_MS`. A D-Bus string is a typed value, never an option, so a title like `-u` or
`--glyph` is the summary as written (the old `headline()` existed only because that command read its options before
the headline). The server's id comes back and is logged. A bus that cannot be reached, refuses the call or does not
answer within `sessionbus.TIMEOUT_S` is a `notify send` FAIL line with the stage, the error and what to check. A PASS
and a FAIL alike say which bus: `socket` (the one connected to), `source` (where its address came from) and `skipped`.

**The toast** (Omarchy's shell, `plugins/notifications/Service.qml`): a normal one is on screen for the timeout it
asks, between 8 s and 30 s (`durationFor`: `normalPopupDuration` 8000, `maxPopupDuration` 30000), so `TOAST_MS`
30000 gives it the whole 30 s. Then it is in the history, still clickable there while it is kept: in 4.0.6 while it is
among the newest `historyLimit` (10), with no age cut; in 4.5.0-rc2 (`Store.js`) a row still on screen while it is
among the newest `LIVE_LIMIT` (100), with no age cut, and a closed one while it is among the newest `HISTORY_LIMIT`
(100) and closed less than `RETENTION_HOURS` (24) ago, whichever cut comes first (`retain`). A shell restart keeps
it: the shell writes `omarchy-exec-argv` to its persistence files. On a click it runs that argv detached, with no
shell (`invokePopupDefault`, `Util.execArgv`), after a structural check (`NotificationLogic.parseExecArgv`: a
non-empty array of strings whose first word does not start with `-`).

**The click** is `[BIN, "--trace", <click trace>, "notify-click", <token>]`: the token is 128 random bits in hex, and
names a file of this module's (`clicks_dir()`, `~/.cache/omaanime/notify-clicks/<token>.json`, 0600 in a 0700 folder)
holding the action's words (`play <id> <ep> …`, `player resume <id> <ep>`), the sender's trace and the time.
`notify-click` (`clicked`) checks the token's form, reads the file and checks the words against `CLICK_ACTIONS`;
`cli.main` then runs them in the same process through its own parser and handlers, so the ids never reach a second
argv. The file stays after a click (a history row can be clicked twice); the newest `CLICKS_KEEP` are kept, pruned by
count when one is written, not by age: 4.0.6 and 4.5.0-rc2's rows on screen are dropped by count alone, so a row of
any age may still be clicked, and 4.5.0-rc2's 24 h cut on closed rows only drops a row sooner than the count would.
200 is above both of 4.5.0-rc2's lists together (100 on screen and 100 closed), so every row any version still shows
keeps its file. A file kept past its row costs about 300
bytes. A notification with no action carries no `omarchy-exec-argv` and writes no file.

Each notification's click runs under a fresh trace (`notify-<action>-<time>-<rand>`), logged with the sender's trace
as `parent`: two Plays pressed from one watcher are two plays, never one trace.

**The sound** (`ready_sound`), only for an episode a long wait brought and loaded paused (`send(sound=True)`): the
notification servers here play none. Omarchy's shell runs Quickshell 0.3.2's `NotificationServer` with no `sound`
capability (its capability strings: actions, body, body-hyperlinks, body-images, body-markup, icon-static,
inline-reply, persistence, action-icons; the freedesktop spec: a server that returns "sound" "must support the
"sound-file" and "suppress-sound" hints"), so a `sound-name`/`sound-file` hint would play nothing. So the plugin plays
one sound file itself, detached, as Omarchy's own `hooks/battery-low.d/play-warning-sound.sample` does
(`mpv --no-video` on a freedesktop sound): `pw-play --media-role=Notification <file>` (PipeWire's player, man
`pw-cat`; `--list-containers` reads `oga`), else `mpv` with no config, no video, no terminal and no IPC of its own (so
it never touches the plugin's player). The file is the first of `SOUND_FILES` on disk (sound-theme-freedesktop's
`complete.oga`, then two others); none, or no player: no sound, logged, and the notification is sent all the same.
With Omarchy's do-not-disturb on (`DND_FILE`'s `dnd`, the shell's own `~/.local/state/omarchy/notifications.json`)
the shell silences the toast into its history, and the sound is not played either. Every sound is one `notify sound`
line: what was played with what, or why nothing was.

`OMAANIME_SOUND_PLAYER` names another program in place of the sound's player (empty: as if it were absent), and
`OMAANIME_DND_FILE` another do-not-disturb file in place of the shell's. The tests set both; nothing in the plugin
does. A test reaches only a bus of its own (`DBUS_SESSION_BUS_ADDRESS`), or none.
"""
from __future__ import annotations

import html
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from . import core, sessionbus
from .core import Log, StageError

APP_NAME = "OmaAnime"
NOTIFY = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
          "Notify", "susssasa{sv}i")
# How long the toast asks to stay on screen: the shell's own maximum (`maxPopupDuration`).
TOAST_MS = 30000
# The freedesktop urgency levels are a byte: 0 low, 1 normal, 2 critical (spec "Urgency Levels").
URGENCY_NORMAL = 1
# The hint Omarchy's shell reads as the click's argv (`NotificationLogic.execArgvFromHints`, 4.5's `Store.js`).
EXEC_HINT = "omarchy-exec-argv"
# Click files kept, the newest by write time: above each of Omarchy's row counts (4.0.6's `historyLimit` 10,
# 4.5.0-rc2's `LIVE_LIMIT` and `HISTORY_LIMIT` 100). No age cut: 4.0.6 has none (4.5.0-rc2's 24 h drops rows sooner).
CLICKS_KEEP = 200
CLICKS_NAME = "notify-clicks"
TOKEN = re.compile(r"[0-9a-f]{32}")
# What a click may run: each action's leading words, and the options it may carry after the two numbers (`--release`
# takes the info hash).
CLICK_ACTIONS: dict[tuple[str, ...], frozenset[str]] = {
    ("play",): frozenset({"--show", "--no-pin", "--release"}),
    ("player", "resume"): frozenset(),
}
NUMBER = re.compile(r"[0-9]{1,9}")
HASH = re.compile(r"[0-9A-Za-z]{32,40}")
# A click file larger than this is not read: one is about 300 bytes.
CLICK_MAX_BYTES = 16384
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
# Module names so a test spawns nothing real and reaches no bus of the desktop's.
_spawn = subprocess.Popen
_spawn_sound = subprocess.Popen
_bus_call = sessionbus.call


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


def click_argv(trace: str, token: str) -> list[str]:
    """What a click runs: the plugin's command under the notification's own trace, and the token. Nothing else."""
    return [str(BIN), "--trace", trace, "notify-click", token]


def markup_body(body: str) -> str:
    """The body as text inside notification markup: `&`, `<` and `>` escaped, so a title's `<b>`, `<a href>` or `&`
    reads as written. The freedesktop spec lets a server read the body as markup (Omarchy 4.0.6's `NotificationCard.qml`
    draws it as `Text.StyledText`, the summary as `Text.PlainText`), so only the body is escaped: an escaped summary
    would show `&amp;`."""
    return html.escape(body, quote=False)


def clicks_dir() -> Path:
    return core.CACHE_DIR / CLICKS_NAME


def prune_clicks() -> int:
    """Keep the newest `CLICKS_KEEP` click files (by write time, then name); delete the rest. How many went."""
    found = []
    for p in clicks_dir().glob("*.json"):
        try:
            if TOKEN.fullmatch(p.stem):
                found.append((p.stat().st_mtime_ns, p.name, p))
        except OSError:
            continue
    found.sort(reverse=True)
    gone = 0
    for _, _, p in found[CLICKS_KEEP:]:
        try:
            p.unlink()
            gone += 1
        except OSError:
            pass
    return gone


def save_click(spec: dict) -> tuple[str, int]:
    """Write the action of `spec` under a new token (0600, in a 0700 folder: `core.write_cache`), then prune. The
    token, and how many old files the prune removed."""
    token = secrets.token_hex(16)
    action = spec["actions"][0]
    core.write_cache(clicks_dir() / f"{token}.json",
                     {"version": 1, "name": action["name"], "args": action["args"], "trace": spec["trace"],
                      "parent": spec["parent"], "created": time.time()})
    return token, prune_clicks()


def refused_words(words) -> str | None:
    """Why a click may not run `words`, or None when they are one of `CLICK_ACTIONS` in its own shape: the leading
    words, the AniList id and the episode as numbers, then only that action's options (`--release` with a hash)."""
    if not isinstance(words, list) or not words or not all(isinstance(w, str) for w in words):
        return "the action is not a list of words"
    for head, options in CLICK_ACTIONS.items():
        if tuple(words[:len(head)]) != head:
            continue
        rest = words[len(head):]
        if len(rest) < 2 or not (NUMBER.fullmatch(rest[0]) and NUMBER.fullmatch(rest[1])):
            return f"{' '.join(head)} without its AniList id and episode as numbers"
        extra = iter(rest[2:])
        for word in extra:
            if word not in options:
                return f"{' '.join(head)} with {word!r}, not an option a notification's click passes"
            if word == "--release" and not HASH.fullmatch(next(extra, "")):
                return "--release without an info hash"
        return None
    return f"{words[0]!r} is not an action a notification runs"


def clicked(log: Log, token: str) -> list[str]:
    """The checked words of the action `token` names, for `cli.main` to run in this process; one `notify click` PASS
    line with the sender's trace. A malformed, unknown or unreadable token, or words not allowed, raise `StageError`
    (`cli.main`'s one FAIL line, exit 1)."""
    t0 = time.monotonic()
    suggest = ("the notification's click names no action this plugin wrote; play the episode from Browse (the "
               "token's file may have been pruned: the newest {} are kept)").format(CLICKS_KEEP)
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise StageError("notify.click", "a malformed token (32 lowercase hex digits expected)", suggest,
                         token_length=len(token) if isinstance(token, str) else None)
    path = clicks_dir() / f"{token}.json"
    try:
        if path.stat().st_size > CLICK_MAX_BYTES:
            raise StageError("notify.click", f"the token's file is over {CLICK_MAX_BYTES} bytes", suggest, token=token)
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StageError("notify.click", "no file for this token", suggest, token=token) from None
    except (OSError, ValueError) as exc:
        raise StageError("notify.click", f"the token's file could not be read: {type(exc).__name__}: {exc}", suggest,
                         token=token) from None
    words = record.get("args") if isinstance(record, dict) else None
    why = refused_words(words)
    if why:
        raise StageError("notify.click", f"refused: {why}", suggest, token=token)
    created = record.get("created")
    log.info("notify", "click", result="PASS", token=token, action=record.get("name"), args=words,
             parent=record.get("parent"), sent_trace=record.get("trace"),
             age_s=round(time.time() - created, 1) if isinstance(created, (int, float)) else None,
             ms=(time.monotonic() - t0) * 1000, reason="the viewer clicked the notification: its action runs here")
    return list(words)


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


def ready_sound(log: Log, parent: str = "", *,
                played_reason: str = "the episode is ready and loaded paused; the viewer may have looked away") -> dict:
    """Play the ready sound once, detached (nothing waits for it): `{played, player, file, pid, dnd, reason}`, one
    `notify sound` line, whose PASS `reason` is the caller's `played_reason` (a play's paused episode by default; the
    watcher's tell names its own). Not played with do-not-disturb on, without a sound file, or without a player."""
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
            reason = played_reason
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
    binary, `click` the sentence the body ends with, "Click to play it."), sent on the session bus in this process.
    Returns once the server answered or the bus failed: `{sent, route: "dbus", id, token, spec}`.
    `sound`: the ready sound plays too (`ready_sound`), after the notification is handed over, sent or not."""
    action = (actions or [None])[0]
    spec = {"title": str(title), "body": " ".join(w for w in (str(body), (action or {}).get("click") or "") if w),
            "trace": fresh_trace(action and action["name"]), "parent": trace or getattr(log, "trace", ""),
            "actions": [{"name": str(action["name"]), "label": str(action["label"]),
                         "args": [str(x) for x in action["args"]]}] if action else []}
    t0 = time.monotonic()
    hints = {"urgency": ("y", URGENCY_NORMAL)}
    token, pruned, click_error, reply, error, stage, suggest = None, 0, "", {}, "", "", ""
    if action:
        try:
            token, pruned = save_click(spec)
            hints[EXEC_HINT] = ("s", json.dumps(click_argv(spec["trace"], token), ensure_ascii=False))
        except OSError as exc:
            # The viewer is still told; the click cannot be, so the toast carries none.
            click_error = f"{type(exc).__name__}: {exc}"
    try:
        reply = _bus_call(*NOTIFY, [APP_NAME, 0, "", spec["title"], markup_body(spec["body"]), [], hints, TOAST_MS])
        sent = True
        # Which bus, on a PASS and a FAIL alike: the socket, where its address came from, the entries skipped.
        bus = {k: reply.get(k) for k in ("socket", "source", "skipped")}
    except sessionbus.BusError as exc:
        sent, error, stage, suggest = False, str(exc), exc.stage, exc.suggest
        bus = {"socket": exc.socket, "source": exc.source, "skipped": exc.skipped}
        if exc.name == "org.freedesktop.DBus.Error.ServiceUnknown":
            suggest = "no notification server owns org.freedesktop.Notifications: is Omarchy's shell running " \
                      "(`omarchy restart shell`)?"
    if not sent and token:
        (clicks_dir() / f"{token}.json").unlink(missing_ok=True)       # nothing can click a notification not shown
        token = None
    nid = (reply.get("values") or [None])[0]
    log.emit("info" if sent and not click_error else "warn", "notify", "send", result="PASS" if sent else "FAIL",
             route="dbus", id=nid, title=title, body=spec["body"], actions=[a["name"] for a in spec["actions"]],
             click=action["args"] if token else None, click_trace=spec["trace"], token=token, pruned=pruned or None,
             click_error=click_error or None, parent=spec["parent"], failed_at=stage or None, error=error or None,
             socket=bus["socket"], source=bus["source"], skipped=bus["skipped"] or None,
             ms=(time.monotonic() - t0) * 1000, reason=why,
             suggest=("the notification did not go out; the viewer is not told this time: " + suggest) if not sent
             else "the notification went out without its click: the cache folder could not be written (free space "
                  "or fix its permissions); the episode plays from Browse" if click_error else "")
    out = {"sent": sent, "route": "dbus", "id": nid, "token": token, "spec": spec}
    if sound:
        out["sound"] = ready_sound(log, spec["parent"])
    return out
