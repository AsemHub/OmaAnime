"""The watcher of pending downloads: "Tell me when it's ready" really tells.

`omarchy-anime watch` follows every entry of `pending-downloads.json` (`pending`) until TorBox has it, then sends one
desktop notification per entry (`notify.send`: "Episode 3 is ready in Japanese with Arabic subtitles." with a Play
action), or tells the viewer it gave up on it (a day old, TorBox no longer has it, or TorBox refused the account),
and removes the entry. One watcher at a time, under its own lock; it exits when nothing is pending, when no TorBox
token is set, or when the plugin is disabled (`core.plugin_enabled`, read from shell.json as the shell reads it).

Graduated, and inside TorBox's limits (300 requests a minute per token): each entry is polled on its own schedule, one
request per entry per tick at most, its gap growing from `FIRST_S` by `GROWTH` to `CAP_S`, each one drawn ±`JITTER`;
the watcher sleeps until the next entry is due. A poll is one attempt (`retries=1`): the next poll is the retry.

Re-armed cheaply (`ensure`): `later` starts it, and the bar's `status` (every few seconds) starts it only when an entry
is pending, a token is set, the plugin is enabled, no watcher holds the lock, and none was started in the last
`SPAWN_EVERY_S`. A watcher is started with no pipes (its own session, stdin/stdout/stderr to /dev/null), so no
command that reads another one's output (the player reads `later`'s) ever waits on it.

The clock (wall time: an entry's `at` is wall time, and a suspend counts), the sleep and the jitter's draw are module
names, so a test runs a day in no time.
"""
from __future__ import annotations

import errno
import fcntl
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import core, pending, notify, playstate
from . import say as saymod
from .core import CACHE_DIR, Http, Log, StageError, BlockedError  # noqa: F401  (CACHE_DIR is rebound by the tests)

LOCK_NAME = "pending-watch.lock"
SPAWNED_NAME = "pending-watch.spawned"      # when `ensure` last started a watcher (wall time; `spawn_stamp`)
FIRST_S = 30.0
GROWTH = 1.5
CAP_S = 300.0
JITTER = 0.25
# A pending download older than this is given up on, and the viewer told.
GIVE_UP_S = 24 * 3600.0
# TorBox answering the torrent is not there, this many polls in a row, before it is taken as gone (one answer could
# be its list catching up; `mylist?id=` for a missing torrent is not documented, so both its shapes count).
MISSING_POLLS = 2
# TorBox refusing the account, this many polls in a row, before every entry is given up: one refusal could be a token
# the viewer is replacing, so the token is read again before the next poll.
REFUSED_POLLS = 2
# Notifications that did not go out, per entry, before it is dropped: a send that keeps failing (no notification
# server, a title the command refuses) must not keep a watcher polling TorBox for ever. Past `GIVE_UP_S`, one is enough.
TELL_FAILS = 3
# The re-arm probe starts a watcher at most this often: a watcher that exits at once (it found no token, or crashed)
# is not restarted every few seconds.
SPAWN_EVERY_S = 300.0
# How long a starting watcher retries the lock: the probe's own try holds it for an instant.
ACQUIRE_S = 1.0
POLL_TIMEOUT_S = 15.0
BIN = notify.BIN
_clock, _sleep, _random = time.time, time.sleep, random.random
_spawn = subprocess.Popen


def lock_file() -> Path:
    return CACHE_DIR / LOCK_NAME


def gap(polls: int, draw: float) -> float:
    """The nap before an entry's next poll, after `polls` polls: `FIRST_S` × `GROWTH`^polls up to `CAP_S`, drawn
    ±`JITTER` (`draw` in [0, 1))."""
    nominal = min(CAP_S, FIRST_S * GROWTH ** max(0, polls))
    return nominal * (1 - JITTER + 2 * JITTER * draw)


def token_set() -> bool:
    """A TorBox token file exists (its contents are not read here)."""
    return (core.CONFIG_DIR / "torbox-token").exists()


def _try_lock(path: Path):
    """The lock's descriptor, held, or None when another holds it."""
    core.private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise


def running(log: Log | None = None) -> bool:
    """Whether a watcher holds the lock right now (one non-blocking try, released at once). A lock that cannot be
    opened reads as no watcher (one is started; it then says why it cannot run)."""
    try:
        fd = _try_lock(lock_file())
    except OSError as exc:
        if log is not None:
            log.warn("watch", "lock", result="FAIL", error=str(exc), path=str(lock_file()),
                     suggest="the watcher's lock could not be opened; check the cache folder's permissions")
        return False
    if fd is None:
        return True
    os.close(fd)
    return False


def spawn_stamp() -> Path:
    """Where `ensure` stamps a start: `$XDG_RUNTIME_DIR` (a tmpfs, apart from the cache's disk), so a cache that cannot
    be written does not lose the stamp and let the 10 s status poll start a watcher every time; the cache dir only when
    the variable is unset. Read at call time (the tests point it at a folder of their own)."""
    run = os.environ.get("XDG_RUNTIME_DIR")
    return Path(run) / f"{core.APP_NAME}-{SPAWNED_NAME}" if run else CACHE_DIR / SPAWNED_NAME


def _start(log: Log, why: str) -> int | None:
    """Start `omarchy-anime watch` detached, with no pipes. Its pid, or None."""
    trace = f"watch-{int(_clock())}-{random.randrange(16**4):04x}"
    try:
        proc = _spawn([sys.executable, str(BIN), "--trace", trace, "watch"], stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError as exc:
        log.warn("watch", "spawn", result="FAIL", error=str(exc), reason=why)
        return None
    try:
        core.write_cache(spawn_stamp(), {"at": _clock(), "pid": proc.pid, "trace": trace})
    except OSError as exc:
        log.warn("watch", "stamp", result="FAIL", error=str(exc),
                 suggest="the start was not stamped, so the status poll may start another watcher (it exits at once)")
    log.info("watch", "spawn", result="PASS", pid=proc.pid, watch_trace=trace, reason=why)
    return proc.pid


def ensure(log: Log, why: str, force: bool = False) -> int | None:
    """Start a watcher when one is needed and none runs: an entry pending, a token set, the plugin enabled, the lock
    free, and (unless `force`, which `later` uses for the entry it just wrote) none started in `SPAWN_EVERY_S`. Cheap:
    one small file read and one lock try when something is pending, nothing more otherwise. Logs a start, and with
    `force` (an explicit request) why nothing was started; the 10 s status path stays quiet."""
    why_not = ("nothing is pending" if not pending.waiting() else "no TorBox token is set" if not token_set() else
               "the plugin is disabled" if core.plugin_enabled() is False else
               "a watcher is running" if running(log) else None)
    if why_not:
        if force:
            log.info("watch", "ensure", result="SKIP", reason=why_not, asked=why)
        return None
    if not force:
        last = core.cached_json(spawn_stamp(), None) or {}
        if _clock() - float(last.get("at") or 0) < SPAWN_EVERY_S:
            return None
    return _start(log, why)


def _key(e: dict) -> tuple:
    return int(e.get("mediaId") or 0), int(e.get("episode") or 0), e.get("torrentId")


def play_action(entry: dict) -> list[str]:
    """The ready notification's Play: the episode, from the release that was downloaded when the stored search still
    holds it (no pin: the viewer picked nothing), else the plain play, which now finds it cached."""
    from . import searches                  # here, not at import: the bar's lean `status` imports this module
    media, ep = str(entry.get("mediaId")), str(entry.get("episode"))
    h = str(entry.get("infoHash") or "")
    if h and searches.find_release(entry.get("mediaId"), entry.get("episode"), h):
        return ["play", media, ep, "--release", h, "--no-pin", "--show"]
    return ["play", media, ep, "--show"]


def dub_action(entry: dict) -> dict | None:
    """The give-up notification's "Watch the English dub", when the entry recorded a dub the stored search holds."""
    from . import searches
    dub = entry.get("dub") or {}
    h = str(dub.get("infoHash") or "")
    if not h or not searches.find_release(entry.get("mediaId"), entry.get("episode"), h):
        return None
    return {"name": "dub", "label": saymod.dub_action_label(dub.get("lang")),
            "click": saymod.click_dub(dub.get("lang")),
            "args": ["play", str(entry.get("mediaId")), str(entry.get("episode")), "--release", h, "--no-pin",
                     "--show"]}


class Watcher:
    """One run of the watch loop. `torbox` is the TorBox client (`providers.TorBox`); `notifier` is `notify.send`;
    `sounder` is `notify.ready_sound`, played once a ready notification went out."""

    def __init__(self, log: Log, torbox, notifier=None, sounder=None):
        self.log, self.torbox = log, torbox
        self.notifier = notifier or notify.send
        self.sounder = sounder or notify.ready_sound
        self.seen: dict[tuple, dict] = {}       # per entry: polls, due, missing, the last reading
        self.unwritable = False                 # the cache refused `TELL_FAILS` marks: nothing can be told or removed

    def _tell(self, entry: dict, body: str, actions: list[dict], why: str, reading: dict, sound: bool = False) -> bool:
        """Mark the entry notified (so it is never told twice), send, remove. False when another took it first, or
        when the notification did not go out: then the mark comes off and a later tick tells again. `sound`: the ready
        sound plays once the notification went out, never for one that did not (the next poll's retry would sound
        again with nothing on screen). Told, the popout's line says the same, with the click's action (`show_told`)."""
        media, ep, tid = _key(entry)
        if pending.mark(media, ep, tid, self.log, notified=_clock(), notified_why=why) is None:
            still = next((e for e in pending.load() if _key(e) == (media, ep, tid)), None)
            if still is not None and not still.get("notified"):
                # The entry is there, unmarked, for this download: the mark did not land (a full or read-only cache).
                st = self.seen.setdefault(_key(entry), {"polls": 0, "missing": 0, "reading": {}})
                st["tell_fails"] = st.get("tell_fails", 0) + 1
                self.unwritable = st["tell_fails"] >= TELL_FAILS
                self.log.warn("watch", "tell", result="FAIL", mediaId=media, episode=ep, torrentId=tid, why=why,
                              reason="the cache cannot be written", fails=st["tell_fails"], of=TELL_FAILS,
                              stopping=self.unwritable,
                              suggest="the pending downloads file could not be marked (see pending mark); free space "
                                      "or fix the permissions of the cache folder")
                return False
            self.log.info("watch", "tell", result="SKIP", mediaId=media, episode=ep, torrentId=tid, why=why,
                          reason="the entry is gone, is another download's, or was already told")
            return False
        sent = self.notifier(self.log, entry.get("title") or notify.APP_NAME, body, actions, why=why)
        if not (isinstance(sent, dict) and sent.get("sent")):
            st = self.seen.setdefault(_key(entry), {"polls": 0, "missing": 0, "reading": {}})
            st["tell_fails"] = st.get("tell_fails", 0) + 1
            old = _clock() - float(entry.get("at") or _clock()) > GIVE_UP_S
            if st["tell_fails"] >= TELL_FAILS or old:
                removed = pending.remove(media, ep, self.log, "the notification could not be sent", torrent_id=tid)
                self.log.warn("watch", "tell", result="FAIL", mediaId=media, episode=ep, torrentId=tid, why=why,
                              body=body, gave_up=True, removed=removed, fails=st["tell_fails"], past_day=old,
                              suggest="no notification could be sent for this request, so it is dropped: the "
                                      "`notify send` lines' failed_at, error and suggest say why (the session bus, or "
                                      "the notification server: Omarchy's shell)")
                return False
            unmarked = pending.unmark(media, ep, tid, self.log, "notified", "notified_why")
            self.log.warn("watch", "tell", result="FAIL", mediaId=media, episode=ep, torrentId=tid, why=why,
                          body=body, unmarked=unmarked, fails=st["tell_fails"], of=TELL_FAILS,
                          next_s=round(st["due"] - _clock(), 1) if "due" in st else None,
                          suggest="the notification did not go out (see notify send); the next poll tries again"
                          if unmarked else "the notification did not go out and the entry still reads as told")
            return False
        if sound:
            self.sounder(self.log, (sent.get("spec") or {}).get("trace") or getattr(self.log, "trace", ""),
                         played_reason="the watcher's notification said the episode is ready; the viewer may have "
                                       "looked away")
        pending.remove(media, ep, self.log, f"told the viewer: {why}", torrent_id=tid)
        self.show_told(entry, body, actions, why, sent)
        self.log.info("watch", "told", result="PASS", mediaId=media, episode=ep, torrentId=tid, why=why,
                      body=body, actions=[a["name"] for a in actions], state=reading.get("state"),
                      progress=reading.get("progress"))
        return True

    def ready(self, entry: dict, reading: dict) -> bool:
        body = saymod.ready_body(entry.get("episode"), entry.get("audio"), entry.get("subtitles"))
        return self._tell(entry, body, [{"name": "play", "label": "Play", "args": play_action(entry),
                                         "click": saymod.CLICK_PLAY}], "ready", reading, sound=True)

    def show(self, entry: dict, reading: dict, now: float) -> dict | None:
        """The poll's reading on the play's `later` line in the popout (`playstate.later_update`): "Downloading episode
        1 on TorBox: 71 %", and when (`watch.at`, which keeps a followed wait's line from going after its hour)."""
        return self._line(entry, message=saymod.later_progress_line(_key(entry)[1], reading),
                          watch={"reading": dict(reading), "at": now})

    def show_told(self, entry: dict, body: str, actions: list[dict], why: str, sent: dict) -> dict | None:
        """The tell on the play's `later` line: the notification's body, and its click's action (its label and token:
        the popout's button runs `notify-click <token>`, the very click) when the notification carried one."""
        token = sent.get("token")
        action = {"name": actions[0]["name"], "label": actions[0]["label"], "token": token} if actions and token \
            else None
        return self._line(entry, message=body, told={"ready": why == "ready", "why": why, "at": _clock(),
                                                     "action": action})

    def _line(self, entry: dict, **fields) -> dict | None:
        """`playstate.later_update` on the entry's play with `fields`. Guarded: the popout's line is the watcher's
        courtesy, so a raise there (a hand-edited `play-status.json`, a bug) is one `watch line` FAIL, never the end of
        a run with other waits still to poll and tell."""
        media, ep, _ = _key(entry)
        trace = str(entry.get("trace") or "")
        try:
            return playstate.later_update(trace, media, ep, self.log, **fields)
        except Exception as exc:                        # noqa: BLE001 - see the docstring
            self.log.emit("warn", "watch", "line", result="FAIL", trace=trace, mediaId=media, episode=ep,
                          told="told" in fields,
                          reason=f"the popout's line could not be updated: {type(exc).__name__}: {exc}",
                          suggest="the popout keeps its last line, the notification is unchanged; run: omarchy-anime "
                                  "trace " + str(getattr(self.log, "trace", "") or "<the watcher's trace>"))
            return None

    def give_up(self, entry: dict, why: str, reading: dict) -> bool:
        reason = saymod.gave_up_reason(why, reading)
        body = saymod.gave_up_body(entry.get("episode"), entry.get("audio"), reason,
                                   nothing_cached=bool(entry.get("nothingCached")))
        # A refused account would refuse the dub's play too: no action that cannot work. With nothing cached there was
        # no dub; the body names the next step instead (`say.CHOOSE_ANOTHER`): a click runs only this command, which
        # cannot open the release list.
        dub = dub_action(entry) if why != "refused" else None
        return self._tell(entry, body, [dub] if dub else [], f"gave up: {reason}", reading)

    def poll(self, entry: dict, now: float) -> str:
        """One request for one entry: "ready", "gave_up", "refused" or "waiting". Logs the reading and the next nap."""
        key = _key(entry)
        st = self.seen.setdefault(key, {"polls": 0, "missing": 0, "reading": {}})
        media, ep, tid = key
        if st.pop("reread", False):
            self.reread_token()
        # Past its day, an entry is still asked once: TorBox may have finished it meanwhile (a laptop closed over a
        # weekend), and the give-up's words come from this reading, not from none.
        old = now - float(entry.get("at") or now) > GIVE_UP_S
        missing = False
        try:
            body = self.torbox._call("mylist", "GET", "/torrents/mylist", query=f"id={tid}&bypass_cache=true",
                                     retries=1, timeout=POLL_TIMEOUT_S)
            data = body.get("data")
            item = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) and data else None)
            missing = item is None
        except BlockedError as exc:
            st["refused"] = st.get("refused", 0) + 1
            st["polls"] += 1
            st["due"] = now + gap(st["polls"], _random())
            last = st["refused"] >= REFUSED_POLLS
            self.log.warn("watch", "poll", result="FAIL", mediaId=media, episode=ep, torrentId=tid, error=str(exc),
                          refused=st["refused"], of=REFUSED_POLLS, next_s=None if last else round(st["due"] - now, 1),
                          suggest="TorBox refused the account again; the viewer is told and the entry dropped" if last
                          else "TorBox refused the account once; the token is read again before the next poll")
            if not last:
                st["reread"] = True
                return "waiting"
            return "refused" if self.give_up(entry, "refused", st["reading"]) else "waiting"
        except StageError as exc:
            answer = exc.data.get("body") if isinstance(exc.data.get("body"), dict) else {}
            code = str(answer.get("error") or "")
            if code == "ITEM_NOT_FOUND":
                missing, item = True, None
            elif old:
                return "gave_up" if self.give_up(entry, "age", {}) else "waiting"
            else:
                st["polls"] += 1
                st["due"] = now + gap(st["polls"], _random())
                self.log.warn("watch", "poll", result="FAIL", mediaId=media, episode=ep, torrentId=tid,
                              error=str(exc), next_s=round(st["due"] - now, 1),
                              suggest="one poll failed; the next one is the retry")
                return "waiting"
        st["polls"] += 1
        st["refused"] = 0
        st["missing"] = st["missing"] + 1 if missing else 0
        if item is not None:
            st["reading"] = {"state": item.get("download_state"),
                             "progress": round(float(item.get("progress") or 0) * 100), "eta": item.get("eta"),
                             "downloaded": item.get("total_downloaded")}
        is_ready = bool(item and item.get("download_finished") and item.get("download_present"))
        st["due"] = now + gap(st["polls"], _random())
        self.log.info("watch", "poll", result="PASS" if is_ready else "WAIT", mediaId=media, episode=ep,
                      torrentId=tid, poll=st["polls"], missing=st["missing"], **st["reading"],
                      age_s=round(now - float(entry.get("at") or now)), next_s=round(st["due"] - now, 1))
        if is_ready:
            return "ready" if self.ready(entry, st["reading"]) else "waiting"
        if old:
            return "gave_up" if self.give_up(entry, "age", st["reading"] if item is not None else {}) else "waiting"
        if st["missing"] >= MISSING_POLLS:
            return "gave_up" if self.give_up(entry, "missing", st["reading"]) else "waiting"
        if item is not None:
            self.show(entry, st["reading"], now)
        return "waiting"

    def reread_token(self) -> bool:
        """The TorBox token read again from its file, after a refusal: a viewer who signed in again meanwhile is
        polled with the new one. True when it changed. The token itself is never logged."""
        try:
            token = core.read_token("torbox")
        except (StageError, OSError) as exc:
            self.log.warn("watch", "token", result="FAIL", error=str(exc),
                          suggest="the token could not be read again; the next poll uses the one it had")
            return False
        changed = bool(token) and token != getattr(self.torbox, "token", None)
        if changed:
            core.remember_secret(token)
            self.torbox.token, self.torbox.headers = token, {"Authorization": f"Bearer {token}"}
        self.log.info("watch", "token", result="PASS", changed=changed,
                      reason="read again after a refusal: a new sign-in is used from the next poll" if changed else
                             "read again after a refusal: the same token")
        return changed

    def tidy(self) -> int:
        """Drop the entries already told (marked `notified`) that a watcher before this one did not remove: told
        before it stopped. Logged per entry. An entry a play took at its wait's ready (`notified_by` "play") is left
        while that play's process lives: it settles the entry itself, or gives it back. Once that play is gone without
        either, nobody told the viewer: the entry is given back (unmarked, never removed) and the next poll tells it."""
        dropped = 0
        for e in [e for e in pending.load() if e.get("notified")]:
            media, ep, tid = _key(e)
            if e.get("notified_by") == "play":
                if not pending.held_by_gone_play(e):
                    self.log.debug("watch", "tidy", result="SKIP", mediaId=media, episode=ep, torrentId=tid,
                                   pid=e.get("notified_pid"), reason="a play holds it and is still running")
                    continue
                back = pending.unmark(media, ep, tid, self.log, "notified", "notified_why", "notified_by",
                                      "notified_pid")
                self.log.emit("info" if back else "warn", "watch", "tidy", result="PASS" if back else "FAIL",
                              mediaId=media, episode=ep, torrentId=tid, pid=e.get("notified_pid"), given_back=back,
                              reason="the play that took it is gone; the watcher tells it",
                              suggest="" if back else "the entry could not be given back; see pending unmark")
                continue
            if pending.remove(media, ep, self.log, "told before the watcher stopped", torrent_id=tid):
                dropped += 1
                self.log.info("watch", "tidy", result="PASS", mediaId=media, episode=ep, torrentId=tid,
                              notified=e.get("notified"), reason="told before it stopped; removed now")
        return dropped

    def stop_reason(self) -> str | None:
        if self.unwritable:
            return "the cache cannot be written"
        if not pending.waiting():
            return "nothing is pending"
        if not token_set():
            return "no TorBox token is set"
        if core.plugin_enabled() is False:
            return "the plugin is disabled"
        return None

    def run(self, max_ticks: int | None = None) -> dict:
        """Poll until a stop reason: each due entry once a tick, then sleep until the next is due."""
        ticks, told = 0, 0
        while True:
            self.tidy()
            why = self.stop_reason()
            if why or (max_ticks is not None and ticks >= max_ticks):
                self.log.emit("warn" if self.unwritable else "info", "watch", "exit",
                              result="FAIL" if self.unwritable else "PASS", ticks=ticks, told=told,
                              reason=why or f"stopped after {ticks} ticks",
                              suggest="nothing could be marked or removed in the cache; the request stays for the "
                                      "next watcher" if self.unwritable else "")
                return {"ok": True, "watching": False, "ticks": ticks, "told": told, "reason": why}
            ticks += 1
            now = _clock()
            entries = pending.waiting()
            refused = False
            for e in entries:
                st = self.seen.get(_key(e))
                if st is not None and st.get("due", 0) > now:
                    continue
                outcome = self.poll(e, now)
                told += outcome in ("ready", "gave_up", "refused")
                if outcome == "refused":
                    refused = True
                    break              # the account is refused for every entry: told for each in the next ticks
            keys = {_key(e) for e in pending.load()}
            dues = [st["due"] for k, st in self.seen.items() if k in keys and "due" in st]
            for k in [k for k in self.seen if k not in keys]:
                del self.seen[k]
            nap = 0.0 if refused else max(1.0, min(dues) - _clock()) if dues else FIRST_S
            if nap:
                _sleep(min(nap, CAP_S * (1 + JITTER)))


def run(log: Log, settings: dict, torbox=None, notifier=None, max_ticks: int | None = None, sounder=None) -> dict:
    """`omarchy-anime watch`: the single watcher. A second one exits at once (logged)."""
    deadline = time.monotonic() + ACQUIRE_S
    fd = None
    while fd is None:
        try:
            fd = _try_lock(lock_file())
        except OSError as exc:
            log.warn("watch", "start", result="FAIL", error=str(exc), suggest="the watcher's lock could not be opened")
            return {"ok": True, "watching": False, "reason": "lock unavailable"}
        if fd is None:
            if time.monotonic() >= deadline:
                log.info("watch", "start", result="SKIP", reason="another watcher holds the lock")
                return {"ok": True, "watching": False, "reason": "another watcher is running"}
            time.sleep(0.05)
    def stopped(signum, frame):
        # A logout or `kill`: the trace ends saying so, and the lock goes with the process.
        log.info("watch", "exit", result="PASS", reason="stopped", signal=signal.Signals(signum).name)
        raise SystemExit(128 + signum)
    try:
        previous = signal.signal(signal.SIGTERM, stopped)
    except ValueError:                    # not the main thread (the command always is): no handler to give back
        previous = None
    try:
        log.info("watch", "start", result="PASS", pid=os.getpid(), pending=len(pending.load()))
        if torbox is None and token_set():
            from .providers import TorBox
            torbox = TorBox(Http(log, float(settings.get("httpTimeout") or 15)), log, core.read_token("torbox"))
        out = Watcher(log, torbox, notifier, sounder).run(max_ticks=max_ticks)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        os.close(fd)
    # A request recorded between this watcher's last read and its lock's release found the lock held, so `later`
    # started nothing: read once more with the lock free, and start the next watcher for it.
    if out.get("reason") == "nothing is pending" and pending.waiting():
        out["next_pid"] = ensure(log, "a request landed while the watcher was exiting", force=True)
        log.info("watch", "rearm", result="PASS" if out["next_pid"] else "SKIP", pid=out["next_pid"],
                 reason="a request landed between the last read and the lock's release")
    return out
