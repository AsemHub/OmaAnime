"""One shared source of truth for an in-flight play.

The panel in the player, the popout in the bar and the CLI itself all need the same two answers:
*is a play running, and where has it got to?* Before this file each of them guessed on its own — the
Lua panel with an 8 s timer, `Service.qml` by watching its own child process, the CLI not at all —
and a play that takes 28-43 s went dark after 8 s while a second click destroyed the first one's
downloader. The guessing is what is replaced here, not the timings.

`~/.cache/omaanime/play-status.json` sits next to `now-playing.json` in a directory the CLI
already owns and both surfaces already read. Chosen over the alternatives for three reasons:
mpv's `user-data` cannot carry a failure that happens *before* mpv is touched (a `resolve` failure
is the common case and the one that showed the user nothing); it is invisible to the QML service
without a second IPC round trip; and it dies with the player, where a file survives the CLI process
being killed, which is exactly the state the next claim has to reason about.

Clocks, measured on this machine rather than assumed:

* Python's `time.monotonic()` **is** `CLOCK_MONOTONIC` and is comparable between processes on one
  boot — it printed 196276.344 against `/proc/uptime`'s 196276.34 — so `startedMono` is meaningful
  to any other process here, and a value in the *future* means the entry predates a reboot.
* mpv's `mp.get_time()` is **process-relative**: 4.93 s on a player 4.93 s old. The Lua side
  therefore cannot compare against `startedMono` at all, and renders from `elapsed` (which the
  writer recomputes monotonically at every write) plus the wall-clock drift since `updatedAt`.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import time
from pathlib import Path

from . import say as saymod
from .core import (CACHE_DIR, PLUGIN_ID, Log, cached_json, private_dir,  # noqa: F401  (CACHE_DIR: rebound by the tests)
                   write_cache)

_OWN_PLUGIN_DIR = f"/omarchy/plugins/{PLUGIN_ID}/".encode()

STATUS_NAME = "play-status.json"
LOCK_NAME = "play-status.lock"
# The two states a play waiting for TorBox's download (`dubs=avoid`, only a dub cached) ends in without failing: it
# stopped waiting and holds the viewer's three choices (`held`, the window stays up), or the viewer asked to be told
# when the download is ready (`later`, a pending download is recorded and the window hidden). Neither is in flight.
HELD = "held"
LATER = "later"
# A held play's choices, and a later play's acknowledgement, belong to their moment: an hour after `finishedAt` no
# surface shows them (no line, no buttons, no keys, no art). Mirrored, as `ABANDON_AFTER_S` is, by
# `omarchy-anime-episodes.lua`'s `HELD_SHOW_S`, `BrowseModel.HELD_SHOW_S` and `ServiceLogic.HELD_SHOW_S`
# (`test_wait_choice.TheMirrors` holds them equal).
HELD_SHOW_S = 3600.0

# An entry nobody is visibly working on is abandoned after this long. It is a floor on trust, not a
# cap on how long a play may take: a claim whose process is alive keeps it however slow the play is.
ABANDON_AFTER_S = 20.0
# ...but a live claim that says nothing for this long can be retried rather than refused forever
# (the gate degrades, it does not lock the user out of the episode they asked for). Mirrored by
# `omarchy-anime-episodes.lua`'s `BACKGROUND_TRUST_S`: how long the player's corner line says a quiet running play
# (`test_wait_choice.TheMirrors` holds them equal).
SILENT_AFTER_S = 60.0
# Superseding: ask, then insist. Same shape as `player.stop`.
TERM_WAIT_S = 3.0
KILL_WAIT_S = 2.0
# How long to wait for the claim lock before going ahead without it. A claim is a read and a write;
# anything holding it for two seconds is broken, and blocking the user's click on it would be worse.
LOCK_WAIT_S = 2.0


def status_file() -> Path:
    return CACHE_DIR / STATUS_NAME


def lock_file() -> Path:
    return CACHE_DIR / LOCK_NAME


# ----------------------------------------------------------------------------- reading

def read() -> dict | None:
    """The entry as it is on disk, or None. Never raises: every caller is on a user-facing path."""
    try:
        raw = status_file().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return entry if isinstance(entry, dict) else None


def elapsed_of(entry: dict | None, now: float) -> float:
    """Seconds since the play started: the writer's own monotonic count, plus the wall-clock drift
    since it last wrote. Clamped at zero because the reader's clock is not the writer's."""
    if not entry:
        return 0.0
    base = float(entry.get("elapsed") or 0.0)
    drift = float(now) - float(entry.get("updatedAt") or 0.0)
    return base + max(0.0, drift)


def render(entry: dict | None, now: float) -> str:
    """The one sentence every surface shows. Kept trivial on purpose: `omarchy-anime-episodes.lua`
    reimplements exactly this, and a format two implementations cannot agree on is worse than none."""
    if not entry:
        return ""
    episode = entry.get("episode")
    state = entry.get("state")
    if state == "failed":
        return f"Episode {episode} failed — {entry.get('error') or 'no reason given'}"
    if state == HELD:
        # A play that stopped waiting for TorBox and holds the viewer's choices: a plain sentence, no counter, for
        # `HELD_SHOW_S` after it held.
        if float(now) - float(entry.get("finishedAt") or 0.0) > HELD_SHOW_S:
            return ""
        return f"Episode {episode} — {entry.get('message') or 'waiting for your choice'}"
    if state != "running":
        return ""
    text = f"Episode {episode} — {entry.get('message') or 'starting'}"
    secs = int(elapsed_of(entry, now))
    return f"{text} · {secs}s" if secs >= 1 else text


def public(now: float | None = None) -> dict | None:
    """What `omarchy-anime status` carries: the entry plus the rendered sentence, so the QML side
    never has to know the vocabulary."""
    entry = read()
    if not entry:
        return None
    return {**entry, "text": render(entry, time.time() if now is None else now)}


# ----------------------------------------------------------------------------- the decision

def decide(entry: dict | None, media_id: int, episode: int, now: float, alive: bool | None,
           chosen: bool = False, background: bool = False) -> dict:
    """Pure: what a new play should do about the entry that is already there.

    `alive` is whether the entry's writer is still a play process — True, False, or None when there
    is no pid to ask about. Kept a parameter so this whole table is testable without a process.
    `chosen`: the new play is a release the viewer chose (`play --release`), which is how a waiting play's choices
    ("Watch the dub now", "Keep waiting") start. `background`: the new play runs in the background, because the player
    is busy with an episode the viewer is watching (`cli.player_busy`).

        no entry, or a finished one          -> claim       (a held or later entry is finished)
        the writer is gone                   -> take-over   (nothing to stop)
        nobody owns it and it went quiet     -> take-over
        the same episode, waiting with the viewer's choices, and the new play is one of them
                                             -> supersede   (the viewer chose)
        the same episode, recently alive     -> already     (say where it is; start nothing)
        the same episode, silent for a minute-> supersede   (retry it)
        a background play, and the entry's play is loading its episode (or has loaded it and is finishing)
                                             -> beside      (that play is the episode being watched: left running)
        a different episode                  -> supersede   (the user meant it)
    """
    out = {"action": "claim", "reason": "nothing in flight", "message": "", "pid": None, "trace": ""}
    if not entry or entry.get("state") != "running":
        return out
    pid = int(entry.get("pid") or 0) or None
    out["pid"], out["trace"] = pid, str(entry.get("trace") or "")
    quiet = max(0.0, float(now) - float(entry.get("updatedAt") or 0.0))
    if alive is False:
        return {**out, "action": "take-over", "reason": "the previous play's process is gone"}
    if alive is not True and quiet > ABANDON_AFTER_S:
        return {**out, "action": "take-over", "reason": f"no sign of the previous play for {int(quiet)}s"}
    same = (int(entry.get("mediaId") or 0) == int(media_id)
            and int(entry.get("episode") or 0) == int(episode))
    if same and chosen and entry.get("choice"):
        return {**out, "action": "supersede",
                "reason": f"the viewer chose from the choices of the play waiting for episode {episode}",
                "message": f"Starting what you chose for episode {episode}"}
    if same and quiet <= SILENT_AFTER_S:
        where = render(entry, now)
        # "Episode 9 — looking for a release · 12s" -> "Episode 9 is already starting — looking …"
        tail = where.split("—", 1)[1].strip() if "—" in where else "starting"
        return {**out, "action": "already", "reason": "the same episode is already starting",
                "message": f"Episode {episode} is already starting — {tail}"}
    if same:
        return {**out, "action": "supersede",
                "reason": f"the claim on episode {episode} has been silent for {int(quiet)}s",
                "message": f"Episode {episode} has been starting for {int(elapsed_of(entry, now))}s "
                           f"with no sign of progress — trying again"}
    if background and entry.get("stage") == "load":
        # Stopping it would cost the episode on screen what it still does after its `loadfile` (the borrowed
        # subtitles, the now-playing write). It runs on; its later writes find this play's entry and keep it
        # (`PlayStatus._owned`).
        return {**out, "action": "beside",
                "reason": f"episode {entry.get('episode')}'s play is loading the episode being watched; it keeps "
                          f"running and this play waits in the background"}
    return {**out, "action": "supersede", "reason": f"the user asked for episode {episode} instead",
            "message": f"Switching to episode {episode}"}


# ----------------------------------------------------------------------------- processes

def pid_is_play(pid) -> bool | None:
    """True when that pid is one of our play processes, False when it is something else, None when
    there is no such process.

    Read straight out of `/proc/<pid>/cmdline` by pid — never `pgrep -f`/`pkill -f`, which match on
    a pattern and would happily select an editor with the same string in its title. This is a
    pid-reuse guard, not a security boundary: between a play writing its pid and the next click, the
    kernel may well have handed that number to something else, and signalling it would be a second
    bug rather than the fix for the first.
    """
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        cmd = Path("/proc", str(pid), "cmdline").read_bytes()
    except OSError:
        return None
    args = cmd.split(b"\0")
    # `play` has to be an argument, not a substring: `--trace ui-play-…` rides on several other
    # commands and would otherwise make a `status` process look like a play.
    if not (b"play" in args and any(b"omarchy-anime" in a or b"omarchy_anime" in a for a in args)):
        return False
    # Another plugin with the same CLI name: a pid reused by *its* play, installed in another
    # plugin folder, is not ours to supersede.
    return not any(b"/omarchy/plugins/" in a and _OWN_PLUGIN_DIR not in a for a in args)


def still_running(pid) -> bool:
    """Is that pid still one of our play processes *right now*?

    Deliberately not `Path("/proc", pid).exists()`: a process that has exited but not been reaped is
    a zombie, and `/proc/<pid>` is still there for it. The kernel empties a zombie's `cmdline`, so
    asking what it is answers both questions at once — exited, reaped, reused, or never ours.
    """
    return pid_is_play(pid) is True


def stop_play(pid, log: Log, term_wait: float = TERM_WAIT_S, kill_wait: float = KILL_WAIT_S) -> dict:
    """End a superseded play. SIGTERM, then SIGKILL, then read `/proc` back."""
    t0 = time.monotonic()
    out = {"pid": int(pid or 0), "signalled": False, "gone": False, "signal": "", "reason": "", "ms": 0.0}
    if not out["pid"] or out["pid"] <= 0:
        out["reason"] = "no pid recorded"
        return out
    if out["pid"] == os.getpid():
        out["reason"] = "refusing to stop myself"
        log.warn("play.supersede", "stop", result="FAIL", **out)
        return out
    alive = pid_is_play(out["pid"])
    if alive is None:
        out.update(gone=True, reason="already gone")
        log.info("play.supersede", "stop", result="PASS", **out)
        return out
    if alive is False:
        out["reason"] = "not a play process"
        log.warn("play.supersede", "stop", result="FAIL", **out,
                 suggest="the pid in play-status.json has been reused; leaving it alone")
        return out
    for sig, wait in ((signal.SIGTERM, term_wait), (signal.SIGKILL, kill_wait)):
        try:
            os.kill(out["pid"], sig)
        except ProcessLookupError:
            out.update(gone=True, signal=sig.name, signalled=True)
            break
        except PermissionError:
            out["reason"] = "not ours to signal"
            break
        out.update(signalled=True, signal=sig.name)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not still_running(out["pid"]):
                out["gone"] = True
                break
            time.sleep(0.05)
        if out["gone"]:
            break
    out["ms"] = round((time.monotonic() - t0) * 1000, 1)
    log.emit("info" if out["gone"] else "error", "play.supersede", "stop",
             result="PASS" if out["gone"] else "FAIL", **out,
             suggest="" if out["gone"] else f"pid {out['pid']} survived SIGKILL; the new play may fight it")
    return out


# ----------------------------------------------------------------------------- writing

def stop_waiting(log: Log) -> dict | None:
    """The viewer's stop reaches a play that waits for TorBox's download (its entry's `detail.torboxWait`):
    `stop_play` ends it, and it fails with "stopped" in its own entry. None when no play is waiting, which leaves every
    other play in flight alone."""
    entry = read() or {}
    if entry.get("state") != "running" or not (entry.get("detail") or {}).get("torboxWait"):
        return None
    out = stop_play(entry.get("pid"), log)
    log.info("play.stop", "torbox_wait", result="PASS" if out.get("gone") else "FAIL", trace=entry.get("trace"),
             episode=entry.get("episode"), pid=out.get("pid"), gone=out.get("gone"), reason=out.get("reason") or
             "the viewer stopped the player while the play waited for TorBox; the download carries on there")
    return out


def being_superseded() -> bool:
    """Whether another play holds the claim lock right now. A claim holds it for the whole of `stop_play`, so a play
    unwinding from the SIGTERM a supersede sent finds it held, and one stopped by `player stop` (which takes no lock)
    finds it free. Never blocks: one non-blocking try, released at once."""
    try:
        fd = os.open(lock_file(), os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        return exc.errno in (errno.EACCES, errno.EAGAIN)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# "Tell me when it's ready" reaches a play still waiting for TorBox by this signal: its handler (`cli.wait_for_torbox`)
# only raises, and the play ends `later` instead of `failed`. Outside the wait the play catches it and does nothing
# (`guard_defer_signal`), so a late one never ends a play mid-load (the signal's default action ends the process).
DEFER_SIGNAL = signal.SIGUSR1
# "Keep waiting" pressed during the wait reaches the waiting play by this signal: its handler
# (`cli.wait_for_torbox`) only records the moment; the play takes the question off its entry and keeps waiting, with
# no second add and no new process. Outside the wait it is caught and does nothing, as `DEFER_SIGNAL` is.
CONFIRM_SIGNAL = signal.SIGUSR2


def _defer_outside_the_wait(signum, frame) -> None:
    """A "Tell me" or a "Keep waiting" that lands outside the wait: nothing to end, nothing to confirm."""


def guard_defer_signal() -> None:
    """Catch `DEFER_SIGNAL` and `CONFIRM_SIGNAL` with a handler that does nothing, for the rest of the play (either
    one's default action ends the process). A caught signal is reset to its default across `exec`, where an ignored
    one (`SIG_IGN`) would be inherited by everything the play starts: the player (`setsid -f mpv`) and every command
    its script spawns would ignore it for life. Main thread only."""
    for sig in (DEFER_SIGNAL, CONFIRM_SIGNAL):
        try:
            signal.signal(sig, _defer_outside_the_wait)
        except ValueError:                   # not the main thread (a test's worker): nothing to guard
            pass


def quiet_file() -> Path:
    return CACHE_DIR / "play-later-quiet.json"


def later_hides(trace: str, log: Log, background: bool = False) -> bool:
    """Whether the play `trace`, ended `later`, hides the window itself: not when the request came from the player
    (`later --no-hide`), which shows its answer first and hides a moment later, and never for a play in the background
    (`background`: the window holds the episode being watched). The marker is read once and removed."""
    mark = cached_json(quiet_file(), None) or {}
    quiet = mark.get("trace") == trace
    if quiet:
        quiet_file().unlink(missing_ok=True)
    log.info("play.later", "hide", result="SKIP" if quiet or background else "PASS", trace=trace,
             reason="a play in the background: the window holds the episode being watched" if background else
                    "the player asked: it says its answer, then hides" if quiet else "the play hides the window")
    return not quiet and not background


def _signal_play(entry: dict, sig, log: Log, component: str, gone: str, before=None, **extra) -> dict:
    """Send `sig` to the play that wrote `entry`, pid-checked as `stop_play` checks it (a play process of this plugin,
    never this process): what was sent and why, logged `<component> signal`. `before()` runs only when the pid is a
    live play, just before the signal; `gone` is the log's suggestion when nothing was sent."""
    out = {"pid": int(entry.get("pid") or 0), "signalled": False, "reason": "", **extra}
    alive = pid_is_play(out["pid"]) if out["pid"] > 0 and out["pid"] != os.getpid() else None
    if alive is not True:
        out["reason"] = "not a play process" if alive is False else "no play process"
    else:
        if before is not None:
            before()
        try:
            os.kill(out["pid"], sig)
            out["signalled"] = True
        except (ProcessLookupError, PermissionError) as exc:
            out["reason"] = type(exc).__name__
    log.info(component, "signal", result="PASS" if out["signalled"] else "SKIP", trace=entry.get("trace"),
             episode=entry.get("episode"), **out, suggest="" if out["signalled"] else gone)
    return out


def defer_waiting(entry: dict, log: Log, hide: bool = True) -> dict:
    """Ask the play that wrote `entry` (waiting, with its choices) to end as `later` (`DEFER_SIGNAL`). The play's own
    entry is the answer. With `hide` False the play is told, before the signal, to leave the window up
    (`later_hides`)."""
    def quiet():
        if hide:
            return
        try:
            write_cache(quiet_file(), {"trace": entry.get("trace"), "at": time.time()})
        except OSError as exc:
            log.warn("play.later", "quiet", result="FAIL", error=str(exc),
                     suggest="the waiting play will hide the window before the player's answer is read")
    return _signal_play(entry, DEFER_SIGNAL, log, "play.later",
                        "the waiting play is gone; its entry is ended here instead", before=quiet, hide=hide)


def confirm_waiting(entry: dict, log: Log) -> dict:
    """The viewer's "Keep waiting" during the wait reaches the play that wrote `entry` (waiting, with its choices):
    `CONFIRM_SIGNAL`. The play's own entry is the answer (its question goes, its line stays)."""
    return _signal_play(entry, CONFIRM_SIGNAL, log, "play.confirm", "the waiting play is gone; nothing to confirm")


def end_held(trace: str, message: str, log: Log, from_state: str = HELD) -> dict | None:
    """A held play (its process gone) ended as `later` by the viewer's "Tell me when it's ready": the entry rewritten
    under the claim lock, only while it is still that play's and in `from_state` (a new claim in between is kept).
    `from_state` "running" is a waiting play whose process is gone without a word. None when not."""
    with _Claim(log):
        entry = read() or {}
        if entry.get("trace") != trace or entry.get("state") != from_state:
            log.info("play.later", "held", result="SKIP", trace=trace, found=entry.get("trace"),
                     state=entry.get("state"), reason="the entry is no longer this held play's; left as it is")
            return None
        now = time.time()
        out = {k: v for k, v in entry.items() if k != "choice"}
        out.update(state=LATER, message=str(message), updatedAt=now, finishedAt=now)
        write_entry(out)
    log.info("play.state", LATER, result="PASS", trace=trace, episode=out.get("episode"), from_state=from_state,
             reason="the viewer asked to be told when TorBox has it; the held play's entry is ended here")
    return out


def write_entry(entry: dict) -> None:
    """Atomic (`write_cache`'s random temp name): two processes may be writing this file in the same millisecond,
    and the Lua panel reads it every half second."""
    write_cache(status_file(), entry)


def clear() -> None:
    status_file().unlink(missing_ok=True)


class _Claim:
    """`flock` around the read-modify-write. Two clicks 200 ms apart are two processes reaching this
    at once, and without the lock both read "nothing in flight" and both claim."""

    def __init__(self, log: Log, wait: float = LOCK_WAIT_S):
        self.log, self.wait, self.fd = log, wait, None

    def __enter__(self):
        path = lock_file()
        try:
            private_dir(path.parent)
            self.fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            self.log.warn("play.claim", "lock", result="FAIL", error=str(exc),
                          suggest="claiming without the lock; a simultaneous play could race")
            return self
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    # Never block the user's click on a lock. Go ahead and say so.
                    self.log.warn("play.claim", "lock", result="FAIL", error=str(exc), waited=self.wait,
                                  suggest="another play held the claim lock; proceeding unlocked")
                    return self
                time.sleep(0.02)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
            self.fd = None
        return False


class PlayStatus:
    """The writer. One per `omarchy-anime play` process; `resolve` makes one and never claims, which
    is why every update below is a no-op until `claim()` succeeds."""

    def __init__(self, log: Log, media_id: int, episode: int, trace: str = "", title: str = ""):
        self.log = log
        self.media_id, self.episode = int(media_id), int(episode)
        self.trace = trace or getattr(log, "trace", "")
        self.title = title
        self.claimed = False
        self.started_mono = 0.0
        # The claim's moment on the wall clock, recorded as it happens: a suspend during the play counts toward the
        # time since it, as it counts toward the wait's cap (a monotonic clock leaves suspended time out).
        self.claimed_at: float | None = None
        self._stage = ""
        self._message = ""
        self._said = ""
        # The coarse phase names this play has passed, and the
        # sentence last said for each — so a surface with no player window can render every step,
        # not just the current one, without knowing the vocabulary itself (that is `say.stage`'s
        # job, not the popout's). Reset per instance, which is correct: a new `PlayStatus` is a new
        # attempt (a fresh claim, superseding or taking over the old one), so its history starts over.
        self._stages: list[str] = []
        self._stage_text: dict[str, str] = {}
        # The loading screen's picture paths, written by `do_play` right after
        # the claim and carried on every entry so the Lua reads them from the file it already polls.
        self._art: dict = {}
        # The viewer's choices while the play waits for TorBox's download in place of a cached dub (`cli.wait_choice`):
        # carried on every running entry like `_art`, and on the held one, until the wait ends.
        self.choice: dict | None = None
        # Set by the claim: a release the viewer chose (`decide`'s `chosen`).
        self.chosen = False
        # Set before the claim: this play runs in the background, because the player is busy with an episode the
        # viewer is watching (`cli.player_busy`). Every entry it writes says so (`background`), so the player draws
        # nothing of it over that episode; cleared when the play takes the player.
        self.background = False

    @property
    def started_at(self) -> float | None:
        """The claim's moment on the wall clock (when the viewer, or auto-next for them, started this play), recorded
        at the claim; None before a claim."""
        return self.claimed_at if self.claimed else None

    def _owned(self, what: str) -> bool:
        """Whether this play still owns the entry: a play that claimed after it (a supersede) has its own trace there,
        and an unwinding writer must never overwrite it. Logged once per refused write."""
        on_disk = read() or {}
        theirs = str(on_disk.get("trace") or "")
        if not on_disk or theirs == self.trace:
            return True
        self.log.warn("play.state", "not_owned", result="SKIP", write=what, mine=self.trace, theirs=theirs,
                      state=on_disk.get("state"), suggest="another play claimed after this one; its entry is kept")
        return False

    def _base(self, state: str, stage: str, message: str, detail: dict, now: float) -> dict:
        """The fields every entry this play writes carries, in one place. `background` only while it runs in the
        background, so every other entry is what it was."""
        out = {"trace": self.trace, "mediaId": self.media_id, "episode": self.episode,
               "title": self.title, "state": state, "stage": str(stage), "message": str(message),
               "detail": {k: v for k, v in detail.items() if v is not None}, "pid": os.getpid(),
               "startedAt": now - (time.monotonic() - self.started_mono), "updatedAt": now,
               "startedMono": self.started_mono, "elapsed": round(time.monotonic() - self.started_mono, 2),
               "stages": list(self._stages), "stageText": dict(self._stage_text), "art": dict(self._art)}
        if self.background:
            out["background"] = True
        return out

    def _advance(self, stage: str, message: str) -> None:
        """Record one step of the history. Appended only the first time a name is seen at all — a
        probe loop that calls `stage()` many times with the same name (once per candidate, to keep
        `updatedAt` fresh) must not turn into many history entries (once per phase,
        never per candidate release) — while `stageText` is refreshed on every call, so it always
        carries the sentence last said for that stage.

        A *consecutive*-only check (append when the last entry
        differs) let a returning phase — a stage that comes back after another one — get appended a
        second time, at the end. `Logic.progressRows` then marked every element equal to
        `play.stage` as current, so both rows lit up, with a dimmed row between them, painting the
        play as going backwards. Checking membership across
        the whole list instead keeps a returning stage at its ORIGINAL position — it is still `done`
        once something else follows it, its `stageText` still carries the freshest sentence, and it
        never appears twice."""
        stage = str(stage or "")
        if not stage:
            return
        if stage not in self._stages:
            self._stages.append(stage)
        if message:
            self._stage_text[stage] = str(message)

    def set_art(self, art: dict) -> dict | None:
        """Store the loading-screen paths and rewrite the current entry now, so the player does not
        wait for the next stage to see them. No-op before a claim."""
        if not self.claimed:
            return None
        art = art if isinstance(art, dict) else {}
        self._art = {str(k): ("" if v is None else v) for k, v in art.items()}
        return self.stage(self._stage or "start", self._message or saymod.stage("start"))

    # -------------------------------------------------------------- claim

    def claim(self) -> dict:
        """Take the in-flight slot, or report why not. The only place that stops another play."""
        with _Claim(self.log):
            entry = read()
            pid = int((entry or {}).get("pid") or 0)
            # None only when the entry names no pid at all; a pid that is not there any more, or
            # that now belongs to something else, is False.
            alive = bool(pid_is_play(pid)) if pid > 0 else None
            # A `startedMono` in the future is an entry from before a reboot, so its pid means
            # nothing: CLOCK_MONOTONIC restarts at zero and the number has been handed out again.
            if alive and float((entry or {}).get("startedMono") or 0.0) > time.monotonic() + 1.0:
                self.log.info("play.claim", "previous-boot", result="PASS", pid=pid,
                              startedMono=(entry or {}).get("startedMono"))
                alive = False
            decision = decide(entry, self.media_id, self.episode, time.time(), alive, chosen=self.chosen,
                              background=self.background)
            decision["claimed"] = False
            if decision["action"] == "already":
                self.log.info("play.claim", "refused", result="PASS", action="already",
                              reason=decision["reason"], holder=decision["trace"],
                              holderPid=decision["pid"], episode=self.episode,
                              suggest="the episode already being started will appear on its own")
                return decision
            if decision["action"] == "supersede":
                self.log.warn("play.claim", "supersede", result="PASS", reason=decision["reason"],
                              holder=decision["trace"], holderPid=decision["pid"],
                              episode=self.episode, holderEpisode=(entry or {}).get("episode"))
                stop_play(decision["pid"], self.log)
            elif decision["action"] == "take-over":
                self.log.info("play.claim", "take-over", result="PASS", reason=decision["reason"],
                              holder=decision["trace"], holderPid=decision["pid"])
            elif decision["action"] == "beside":
                self.log.info("play.claim", "beside", result="PASS", reason=decision["reason"],
                              holder=decision["trace"], holderPid=decision["pid"], episode=self.episode,
                              holderEpisode=(entry or {}).get("episode"))
            now = time.time()
            self.started_mono = time.monotonic()
            self.claimed_at = now
            self.claimed = True
            self._stage, self._message = "start", saymod.stage("start")
            self._advance(self._stage, self._message)
            # The claim's own moment: it started now, nothing has elapsed.
            write_entry({**self._base("running", self._stage, self._message, {}, now),
                         "startedAt": now, "elapsed": 0.0})
            decision["claimed"] = True
            self.log.info("play.claim", "claimed", result="PASS", action=decision["action"],
                          mediaId=self.media_id, episode=self.episode, path=str(status_file()))
        # The claim is a state change, so read it back before anything relies on it.
        back = read() or {}
        if back.get("trace") != self.trace or back.get("state") != "running":
            self.log.error("play.claim", "verify", result="FAIL", wrote=self.trace,
                           readBack=back.get("trace"), state=back.get("state"),
                           suggest="another process claimed between the write and the read-back; "
                                   "the gate will fall back to the 2 s local marker")
        return decision

    # -------------------------------------------------------------- updates

    def stage(self, stage: str, message: str, /, **detail) -> dict | None:
        """Move the shared state on. Written every time (the second counter has to advance) but
        logged only when the words change, so a per-second progress update does not bury the trace.

        The `/` is not decoration: `detail` is caller-supplied, and without it a detail called
        `stage` or `message` would bind to this method's own parameters instead of landing in the
        entry — the same class of bug as spreading `detail` into a log call.
        """
        if not self.claimed or not self._owned("stage"):
            return None
        self._advance(stage, message)
        now = time.time()
        entry = self._base("running", stage, message, detail, now)
        if self.choice:
            entry["choice"] = self.choice
        write_entry(entry)
        said = f"{stage}:{message}"
        if said != self._said:
            self._said = said
            # `detail` goes in as one value, never spread: it is caller-supplied, and a key called
            # `episode`, `ms` or `result` would collide with `Log.emit`'s own parameters and raise a
            # TypeError *on the play path*. Measured live on 2026-09-16: `finish(episode=…)` did
            # exactly that and turned a play that had already loaded into a reported failure.
            self.log.debug("play.stage", str(stage), result="PASS", message=str(message),
                           elapsed=entry["elapsed"], detail=entry["detail"])
        self._stage, self._message = str(stage), str(message)
        return entry

    def finish(self, message: str | None = None, /, **detail) -> dict | None:
        """The play loaded its episode. `message`: what the player's line says of it (an episode ready while nobody
        watched loads paused, "Episode 3 is ready. Space plays it."); the stage's words otherwise."""
        if not self.claimed or not self._owned("done"):
            return None
        message = message or saymod.stage("done")
        self._advance("done", message)
        now = time.time()
        entry = {**self._base("done", "done", message, detail, now), "finishedAt": now}
        write_entry(entry)
        self.log.info("play.state", "done", result="PASS", episode=self.episode,
                      elapsed=entry["elapsed"], detail=entry["detail"])
        return entry

    def fail(self, stage: str, error: str, suggest: str = "", /, say: dict | None = None,
             **detail) -> dict | None:
        """The failure stays on disk: it is the only way a surface that was not watching — the
        player during a panel play, the popout during any of them — can say what went wrong.

        `say` is the structured `{"headline", "action", "hint"}` a resolve failure carries beside
        its stage detail — a named field like `failedStage` and `suggest`, not
        folded into `detail`, so a surface reads it without knowing this method's caller-supplied
        keys from its fixed ones.
        """
        if not self.claimed or not self._owned("failed"):
            return None
        now = time.time()
        # The history (`stages`/`stageText`, from `_base`) as it stood when the play failed — not advanced with
        # `stage` (a granular internal stage like "torbox.rangeCheck", not one of the coarse phase names) or with
        # `error` (a reason, not a phase sentence). Included anyway, unchanged, so a reader never sees it vanish on
        # the one entry that matters most for the popout.
        entry = {**self._base("failed", stage, error, detail, now), "failedStage": str(stage), "error": str(error),
                 "suggest": str(suggest), "say": say or {}, "finishedAt": now}
        write_entry(entry)
        self.log.info("play.state", "failed", result="FAIL", episode=self.episode,
                      failedStage=str(stage), elapsed=entry["elapsed"],
                      suggest=str(suggest) or "the player and the popout will show this")
        return entry

    def hold(self, message: str, choice: dict, /, **detail) -> dict | None:
        """The wait for TorBox reached its cap (`providers.KEEP_WAITING_MAX_S`) without the episode: not a failure.
        The entry keeps the viewer's choices (`choice`, its "Keep waiting" now a command) and the window stays up; the
        play process exits. A new play claims over it (`decide`)."""
        if not self.claimed or not self._owned(HELD):
            return None
        now = time.time()
        self.choice = choice
        entry = {**self._base(HELD, "torbox", message, detail, now), "finishedAt": now, "choice": choice}
        write_entry(entry)
        self.log.info("play.state", HELD, result="PASS", episode=self.episode, elapsed=entry["elapsed"],
                      message=str(message), actions=[a.get("id") for a in choice.get("actions") or []],
                      reason="the play stopped waiting for TorBox and holds the viewer's choices; not a failure")
        return entry

    def later(self, message: str, /, **detail) -> dict | None:
        """The play ends `later`, without a failure. Two callers: the viewer asked to be told when TorBox has the
        episode (the pending download is recorded, `pending.add`, and the window is hidden unless the play runs in the
        background); or a play in the background found its episode ready while the player was still busy
        (`cli.ready_in_the_background`: no pending download, nothing hidden, its line saying the episode is ready)."""
        if not self.claimed or not self._owned(LATER):
            return None
        now = time.time()
        self.choice = None
        entry = {**self._base(LATER, "torbox", message, detail, now), "finishedAt": now}
        write_entry(entry)
        self.log.info("play.state", LATER, result="PASS", episode=self.episode, elapsed=entry["elapsed"],
                      reason="the viewer asked to be told when TorBox has it; not a failure")
        return entry
