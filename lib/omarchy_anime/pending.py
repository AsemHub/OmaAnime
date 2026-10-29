"""The downloads a viewer asked to be told about ("Tell me when it's ready").

A play that waits for TorBox's download (the original-audio release when TorBox had only a dub cached, or the best
release when it had nothing) offers the viewer its choices; "Tell me when it's ready" ends the play without a failure
and records the download here, one entry
per episode: the show, the episode, TorBox's torrent id, the release, the languages it was asked for in, and when.
`CACHE_DIR/pending-downloads.json`, written whole and atomically (`write_cache`), mode 600 like the stored searches.

The watcher that reads these entries and sends the notification with its Play action is built on this file:
`load` is what it reads, and `remove` is what it (and a later play that finds the episode cached) calls.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import core
from .core import CACHE_DIR, Log, cached_json, write_cache  # noqa: F401  (CACHE_DIR is rebound by the tests)

PENDING_NAME = "pending-downloads.json"
LOCK_NAME = "pending-downloads.lock"
# How long a writer waits for another one (`later` adding, the watcher removing) before going ahead and saying so.
LOCK_WAIT_S = 2.0
# The keys an entry carries, in the order the log shows them.
# `nothingCached`: TorBox had no copy of the episode when the play waited (no dub to offer at a give-up).
KEYS = ("mediaId", "episode", "title", "torrentId", "infoHash", "release", "audio", "subtitles", "dub", "nothingCached",
        "at", "trace")


def path() -> Path:
    return CACHE_DIR / PENDING_NAME


def _locked(log: Log):
    """`flock` around a read-modify-write of the file, so two writers never lose one's update. A lock that cannot be
    had within `LOCK_WAIT_S` is logged and the write goes ahead: a request is never dropped for want of the lock."""
    return core.flocked(CACHE_DIR / LOCK_NAME, log, "pending", "the pending downloads'", LOCK_WAIT_S)


def load() -> list[dict]:
    """Every pending entry, oldest first; [] when there is none or the file is unreadable."""
    data = cached_json(path(), None)
    entries = data.get("pending") if isinstance(data, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def unmark(media_id, episode, torrent_id, log: Log, *names: str) -> bool:
    """Take `names` off the episode's entry for that download, under the lock, and read it back: a notification that
    did not go out is not a tell, so the next tick may tell again. False when the entry is gone or it did not read
    back."""
    with _locked(log):
        entries = load()
        found = next((e for e in entries if _same(e, media_id, episode) and e.get("torrentId") == torrent_id), None)
        if found is None:
            return False
        for k in names:
            found.pop(k, None)
        error = ""
        try:
            _write(entries)
        except OSError as exc:
            error = str(exc)
        back = next((e for e in load() if _same(e, media_id, episode) and e.get("torrentId") == torrent_id), None)
    ok = back is not None and not any(k in back for k in names)
    log.emit("info" if ok else "warn", "pending", "unmark", result="PASS" if ok else "FAIL", mediaId=int(media_id),
             episode=int(episode), torrentId=torrent_id, fields=sorted(names), error=error or None,
             suggest="" if ok else "the entry still reads as told; the watcher drops it and the viewer is not told")
    return ok


def held_by_gone_play(entry: dict) -> bool:
    """An entry a play took at its wait's ready (`notified_by` "play") whose play is no longer running (killed with no
    unwind: a supersede's SIGTERM, a logout, an OOM): nobody told the viewer, so it is the watcher's to tell. One
    `/proc` read (`playstate.pid_is_play`) per such entry."""
    if entry.get("notified_by") != "play":
        return False
    from . import playstate                 # here, not at import: the bar's lean `status` reads this module
    return playstate.pid_is_play(entry.get("notified_pid")) is not True


def waiting() -> list[dict]:
    """The entries not yet told: what keeps a watcher running, and starts one. A told one left behind (a crash between
    the tell and the remove) is the watcher's to drop, never a reason to stay. One a play took and then died holding
    counts: the watcher gives it back (`watcher.Watcher.tidy`) and tells it."""
    return [e for e in load() if not e.get("notified") or held_by_gone_play(e)]


def _write(entries: list[dict]) -> None:
    write_cache(path(), {"pending": entries})
    try:
        os.chmod(path(), 0o600)
    except OSError:
        pass


def _same(entry: dict, media_id, episode) -> bool:
    return int(entry.get("mediaId") or 0) == int(media_id) and int(entry.get("episode") or 0) == int(episode)


def add(record: dict, log: Log) -> dict | None:
    """Record one pending download (replacing an older entry for the same episode), read it back, and log both.
    The entry as read back, or None when it did not land."""
    entry = {k: record.get(k) for k in KEYS}
    entry["at"] = entry.get("at") or time.time()
    t0 = time.monotonic()
    with _locked(log):
        before = load()
        kept = [e for e in before if not _same(e, entry["mediaId"], entry["episode"])]
        replaced = len(before) - len(kept)
        error = ""
        try:
            _write(kept + [entry])
        except OSError as exc:                       # a full disk, a read-only cache: the record did not land
            error = str(exc)
        back = next((e for e in load() if _same(e, entry["mediaId"], entry["episode"])), None)
    ok = back is not None and back.get("torrentId") == entry["torrentId"]
    log.emit("info" if ok else "error", "pending", "write", result="PASS" if ok else "FAIL",
             ms=(time.monotonic() - t0) * 1000, path=str(path()), replaced=replaced, entries=len(kept) + 1,
             error=error or None,
             **{k: entry[k] for k in ("mediaId", "episode", "torrentId", "infoHash", "audio", "subtitles",
                                      "nothingCached")},
             suggest="" if ok else "the pending download did not read back; nothing will tell the viewer about it")
    return back if ok else None


def remove(media_id, episode, log: Log, why: str, torrent_id=None) -> bool:
    """Drop the episode's pending entry; True when there was one. `why` is logged. With `torrent_id`, only an entry for
    that download: a newer request for the episode (another download) is kept."""
    with _locked(log):
        entries = load()
        kept = [e for e in entries if not (_same(e, media_id, episode)
                                           and (torrent_id is None or e.get("torrentId") == torrent_id))]
        if len(kept) == len(entries):
            return False
        try:
            _write(kept)
        except OSError as exc:                   # a full or read-only cache: the entry stays, and says so
            log.warn("pending", "remove", result="FAIL", mediaId=int(media_id), episode=int(episode),
                     torrentId=torrent_id, error=str(exc), reason=why,
                     suggest="the entry could not be removed; the watcher drops a told one at its next tick")
            return False
    log.info("pending", "remove", result="PASS", mediaId=int(media_id), episode=int(episode), entries=len(kept),
             torrentId=torrent_id, reason=why)
    return True


def mark(media_id, episode, torrent_id, log: Log, **fields) -> dict | None:
    """Set `fields` on the episode's entry for that download, under the lock, and read it back: the watcher marks an
    entry `notified` before it sends anything, so a crash or a second watcher never tells the viewer twice. None when
    the entry is gone (a play cleared it) or is another download's (a newer request)."""
    with _locked(log):
        entries = load()
        found = next((e for e in entries if _same(e, media_id, episode) and e.get("torrentId") == torrent_id), None)
        if found is None or any(found.get(k) for k in fields if k == "notified"):
            return None
        found.update(fields)
        try:
            _write(entries)
        except OSError as exc:
            log.warn("pending", "mark", result="FAIL", mediaId=int(media_id), episode=int(episode), error=str(exc),
                     suggest="the entry could not be marked; nothing is sent for it this time")
            return None
        back = next((e for e in load() if _same(e, media_id, episode) and e.get("torrentId") == torrent_id), None)
    ok = back is not None and all(back.get(k) == v for k, v in fields.items())
    log.emit("info" if ok else "warn", "pending", "mark", result="PASS" if ok else "FAIL", mediaId=int(media_id),
             episode=int(episode), torrentId=torrent_id, fields=sorted(fields))
    return back if ok else None
