"""Pins — the viewer's remembered pick for one show (a "Pin").

`CACHE_DIR/pins.json`, keyed by AniList id (a pin belongs to its own entry only). A pin has a
preferred group and, when the pick was a pack, a pinned pack stored with its full magnet (a
magnet rebuilt from the hash has no trackers) and what is known of its range and files. `record_pick` writes
pins; a play applies them.
"""
from __future__ import annotations

import os
import time

from .core import CACHE_DIR, Log, cached_json, write_cache
from .score import (FILE_NUMBER_NOT_EPISODE, FILE_SEASON_RELATIVE, identity_tokens, pack_ranges, parse_title,
                    pick_episode_file, skipped_summary)

PINS = CACHE_DIR / "pins.json"
# Below the subtitle terms (verified language +60, language in the title +45), so the
# viewer's standing preference for their language still wins over a group they once picked.
GROUP_BONUS = 30
# How long a seeder count recorded on the pin is evidence. A count under the pack
# minimum skips the pinned pack only while younger than this; older, the count is unknown and the
# pack is queued (the viewer chose it).
SEEDERS_FRESH_S = 24 * 3600
# A queued pinned pack whose progress has not moved for this long is searched past
# for that play (the pin stays; the next play asks the account again).
STUCK_S = 6 * 3600


def _load() -> dict:
    data = cached_json(PINS, None)
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    write_cache(PINS, data)
    try:
        os.chmod(PINS, 0o600)
    except OSError:
        pass


def get(anilist_id) -> dict | None:
    return _load().get(str(int(anilist_id)))


def _file_names(cand: dict) -> list[str] | None:
    files = cand.get("files") or []
    names = [str(f.get("name") or f.get("short_name") or "") for f in files if isinstance(f, dict)]
    names = [n for n in names if n]
    return names or None


def covers(pack: dict | None, episode, alt: list | None = None, entry: dict | None = None,
           log: Log | None = None) -> bool:
    """A pinned pack covers an episode when its title's range holds it (or an absolute
    `alt` number), or when the file list recorded when it played has a file for it. A pack with
    neither covers nothing: this is the one rule that skips the refusal of a name that is the franchise as a
    whole, so it never guesses.

    `entry` (`cli.entry_identity`) is asked of the recorded files as of any pack's: a later-season entry's pin
    whose only files numbered like the episode are another show's or season's does not cover it. Logged as
    `pin.covers identity` when it decides.

    For an absolute-numbered entry (`entry["season_map"]`), a title stating a season after
    the entry's first (`… S02 (01-40)`) counts that season from 1, so its range is not the entry's episodes: the range
    is not read, and the recorded files decide (logged as `pin.covers range`). Without recorded files such a pin
    covers nothing, and the search runs."""
    if not pack:
        return False
    wanted = {int(episode), *(int(a) for a in (alt or []) if a is not None)}
    smap = (entry or {}).get("season_map")
    later = sorted(s for s in identity_tokens(str(pack.get("title") or ""))[1] if smap and s > smap["first"])
    if later and pack.get("range") and log is not None:
        log.info("pin.covers", "range", result="SKIP", title=pack.get("title"), episode=episode,
                 range=pack.get("range"), seasons=later,
                 reason="the title's range counts a later season from 1, not the entry's episodes; "
                        "the recorded files decide")
    for lo, hi in [] if later else pack.get("range") or []:
        if any(int(lo) <= n <= int(hi) for n in wanted):
            return True
    files = pack.get("files") or []
    if files:
        skipped: list = []
        got = pick_episode_file([{"name": n} for n in files], int(episode), list(alt or []), entry,
                                str(pack.get("title") or ""), skipped)
        if log is not None and (skipped or entry is None):
            if entry is None:
                reason = ("no entry identity: the recorded files are read by their episode marker alone, "
                          "not checked against the entry's titles")
            elif all(s["code"] == FILE_NUMBER_NOT_EPISODE for s in skipped):
                reason = ("a recorded file's stray number is the episode, but its marker states another"
                          + ("; a later file is the episode" if got else ""))
            elif all(s["code"] in (FILE_NUMBER_NOT_EPISODE, FILE_SEASON_RELATIVE) for s in skipped):
                reason = ("the recorded files count a later season from 1, and ani.zip pairs this episode with "
                          + ("another of them" if got else "none of them"))
            elif got:
                reason = "a file of the entry, past other shows' or seasons' files"
            else:
                reason = "the pinned pack's files numbered like this episode belong to another show or season"
            log.info("pin.covers", "identity", result="PASS" if got else "SKIP", title=pack.get("title"),
                     episode=episode, file=(got or {}).get("basename"), identity=(got or {}).get("identity"),
                     read=(got or {}).get("read"), **skipped_summary(skipped, entry), reason=reason)
        return got is not None
    return False


def record_pick(log: Log, anilist_id, episode, cand: dict, files: list[str] | None = None) -> dict:
    """Every pick that plays pins its group and, for a pack, the pack. `files` is the list a
    provider learned while starting it, when it reports one: without a file list a rangeless title
    could never cover an episode."""
    parsed = cand.get("parsed") or parse_title(cand.get("title", ""))
    pack = None
    data = _load()
    if parsed.get("batch"):
        h = str(cand.get("info_hash") or "").lower()
        names = [str(f) for f in files if f] if files else _file_names(cand)
        old = (data.get(str(int(anilist_id))) or {}).get("pack") or {}
        same = old.get("info_hash") == h
        if not names:
            # A play that learned no file list must not wipe the one an earlier play recorded, or a
            # rangeless pack stops covering.
            names = old.get("files") if same else None
        # The count the pick was made on, and when. A pinned play replays the pin's
        # own count, so it keeps the old record: re-stamping it would keep an old count fresh forever.
        if cand.get("source") == "pin":
            seeders, seen_at = (old.get("seeders"), old.get("seen_at")) if same else (None, None)
        else:
            seeders = cand.get("seeders")
            seen_at = time.time() if seeders is not None else None
        pack = {"info_hash": h, "magnet": cand.get("magnet"),
                "title": cand.get("title"), "range": [list(r) for r in pack_ranges(cand.get("title", ""))],
                "files": names, "seeders": seeders, "seen_at": seen_at}
    pin = {"group": parsed.get("group"), "pack": pack, "episode": int(episode), "picked_at": time.time(),
           "title": cand.get("title")}
    data[str(int(anilist_id))] = pin
    _save(data)
    log.info("pin.write", "pick", result="PASS", anilist=int(anilist_id), episode=int(episode),
             group=pin["group"], pack=bool(pack), range=(pack or {}).get("range"),
             files=len((pack or {}).get("files") or []), seeders=(pack or {}).get("seeders"),
             title=cand.get("title"))
    return pin


def note_queued(log: Log, anilist_id, info_hash: str, provider: str, torrent_id, progress) -> float:
    """A provider is downloading the pinned pack. Remember its progress on the pin and
    when that progress last moved; return how long it has not moved (0 when it just did)."""
    data = _load()
    pack = (data.get(str(int(anilist_id))) or {}).get("pack") or {}
    if pack.get("info_hash") != str(info_hash).lower():
        return 0.0
    now = time.time()
    progress = int(progress or 0)
    was = pack.get("queued") or {}
    moved = was.get("progress") != progress or not was.get("moved_at")
    moved_at = now if moved else float(was["moved_at"])
    pack["queued"] = {"provider": provider, "torrent_id": torrent_id, "progress": progress, "moved_at": moved_at}
    _save(data)
    still = round(now - moved_at)
    if moved:
        reason = "progress moved since the last play (or this is the first)"
    else:
        reason = f"progress unchanged for {still} s"
    log.info("pin.queued", "progress", result="PASS", anilist=int(anilist_id), provider=provider,
             torrent_id=torrent_id, progress=progress, was=was.get("progress"), moved=moved, still_s=still,
             reason=reason)
    return float(still)


def drop_pack(log: Log, anilist_id, stage: str, error: str) -> None:
    """A pinned pack proven dead leaves the pin; the preferred group stays."""
    data = _load()
    pin = data.get(str(int(anilist_id)))
    if not pin or not pin.get("pack"):
        return
    gone = pin["pack"]
    pin["pack"] = None
    _save(data)
    log.warn("pin.drop", "dead", result="FAIL", anilist=int(anilist_id), title=gone.get("title"),
             info_hash=gone.get("info_hash"), failed_stage=stage, error=error, group_kept=pin.get("group"),
             suggest="the pinned pack is dead; its group is still preferred")


def forget(log: Log, anilist_id) -> bool:
    data = _load()
    gone = data.pop(str(int(anilist_id)), None)
    if gone is not None:
        _save(data)
    log.info("pin.forget", "done", result="PASS" if gone else "EMPTY", anilist=int(anilist_id),
             group=(gone or {}).get("group"))
    return gone is not None
