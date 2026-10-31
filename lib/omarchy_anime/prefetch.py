"""The next episode on TorBox while this one plays.

A viewer watches in order, and an episode TorBox has not cached takes as long as its swarm takes (one measured: 952 s,
one seeder at 0.4-1.8 MB/s and minutes stalled with none). A 24-minute episode is longer than that, so the download of
episode N+1 starts while N plays, and N+1 is cached, or on the account and nearly done, by the time the viewer gets
to it.

- **When**: the tick (`cli.do_tick`) asks `wanted` every time; once the viewer is `AFTER_S` into episode N, at least
  that long after it loaded, and N+1 has aired, is within the show's count, is not behind the list's progress (a
  rewatch of an old episode is not a sign of what comes next, unless the entry is REPEATING), and TorBox leads the
  providers with a token set, the tick takes the latch (`latch`) and starts `omarchy-anime prefetch <mediaId> <N+1>`
  detached (`start`). It never waits for it.
- **What**: the command (`cli.do_prefetch`) runs the play's own search, ranking, dub rule, pin and language pick for
  N+1 through `cli.do_resolve`, with a TorBox client of its own (`Client`) that ends the resolve at the first release
  the play would stream or add (`Stop`): one TorBox has cached, or one already on the account, adds nothing; any other
  is added once (`seed` 3, as the play adds) and nothing else is sent, whatever TorBox answers. No player, no claim, no
  stream, no wait, no notification. Its search is the episode's stored search, as a play's is (the picker's list and
  the side-load's donors), but never a refusal from it, nor one written while a play of the episode is starting.
- **The later play** finds it: cached (once TorBox has downloaded it, `checkcached` calls it cached and the cached-only
  add answers "Found Cached Torrent" with the same torrent id, seen on two replays of downloaded episodes), or on the
  account (`TorBox.listed`, the one-add rule's lookup), or in TorBox's queue (`queued_entry`); and when its own search
  now ranks another release first, it still tries the prefetched one first (`cli.prefetched_first`).
- **The later play skips its search** (`reusable`, `cli.play_prefetched`): the command saves the release it
  stopped at as the index served it (`record`), what its search took (`searchMs`), and the `fingerprint` of everything
  that decided it. A play of the episode within `REUSE_S`, with no pick, the same fingerprint and the stored search
  still holding the release (its side-load donors), plays that release without a search when this play's own
  `checkcached` calls it cached; anything else, and any failure of it after that, is the full search, in the same
  play. A release that lacks the viewer's language plays too, side-loaded, where the search would play it: under
  `prefer`, when the prefetch's search had no other cached release its loop would try (`sideload_reuse`); its
  side-load pool is the one that search built (`donorPool`, `cli.reused_donor_pool`).

`prefetch.json` (the cache) holds one entry per (mediaId, episode): the latch, taken under `flock` before the command
starts (at most one per episode, across ticks, restarts and one Service per monitor), and then what the command did.
TorBox's account is the truth for what is there; the entry is only the hint the play's selection step checks against
it. Entries older than `KEEP_S` are dropped at the next write.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import time
from pathlib import Path

from . import core, notify, playstate, private_args, searches, torznab_servers
from . import providers as providersmod
from .anilist import next_episode
# CACHE_DIR is rebound by the tests.
from .core import (CACHE_DIR, BlockedError, Interrupted, Log, StageError, as_bool, cached_json,  # noqa: F401
                   write_cache)
from .providers import TorBox, torbox_usable

FILE_NAME = "prefetch.json"
LOCK_NAME = "prefetch.lock"
LOCK_WAIT_S = 2.0
# How much of episode N the viewer has watched when the download of N+1 starts: this far into the file, and this long
# since it loaded (`startedAt`), so a resume or a seek past it is not two minutes watched. A measured wait was 952 s
# of a 1420 s episode: started here it is ready about 350 s before N ends, while a viewer who opens an episode and
# leaves within two minutes (a sample, a wrong show) costs no add.
AFTER_S = 120.0
# An entry is kept this long, then dropped: long enough to stay latched for an episode watched days later, short enough
# that the file stays small.
KEEP_S = 14 * 24 * 3600.0
# What the command did, as its JSON answer and the entry's `state` say it. `ADDED` and `ON_ACCOUNT` name a download the
# later play should find first.
STARTED, ADDED, CACHED, ON_ACCOUNT, SKIPPED, FAILED = "started", "added", "cached", "on_account", "skipped", "failed"
# The add was accepted into TorBox's queue (its active-download slots full): it is in `GET /queued/getqueued`, which
# `mylist` does not list, under the id the add answered (`queued_id`).
QUEUED = "queued"
FOUND = (ADDED, ON_ACCOUNT, QUEUED)
# The outcomes whose release a later play may play without its search (`reusable`): every one that named the play's
# pick. The play's own `checkcached` then decides: only a release TorBox has cached is played that way.
REUSABLE = (CACHED, *FOUND)
# How long after the prefetch found it a play still plays its release without a search. The prefetch saw the releases
# of that moment: a later upload (a v2, a group with the viewer's language) is found only by a search. Recorded gaps
# from the prefetch to the play: 15 s, 55 s, 54 s and 35 min; six hours keeps a binge session, not yesterday's.
REUSE_S = 6 * 3600.0
# The settings that decide which release a play picks, beside the parts `fingerprint` names on their own. The two
# subtitle-check ones decide how many releases' tracks the ranking reads, so which ones are known to carry the language.
RANKING_KEYS = ("preferredResolution", "allowBatches", "preferredGroups", "englishOnlyGroups", "indexers",
                "sideloadSubtitles", "probeCount", "subtitleCheckCount", "subtitleCheckSeconds")
# A `started` entry older than this is a prefetch that ended without writing what it did (killed with the session, a
# crash): not "still running". The command has no deadline of its own, so this is one: the six recorded runs took
# 0.0-14.7 s from their first line to `prefetch done`; five minutes is twenty times the longest.
RUNNING_S = 300.0
# TorBox's queue of downloads waiting for a slot (collection, "Get Queued Downloads", read 2026-10-23): each entry has
# `id`, `hash`, `name` and `type`; an empty queue answers HTTP 404 with `success: true, error: ITEM_NOT_FOUND,
# data: []`, a normal answer here (`expected`), not a failed request.
QUEUED_PATH = "/queued/getqueued"
# A play over a queued prefetch waits in the queue first (`settle_queued`): one queue read this often (TorBox allows
# 300 requests a minute per token), each one writing the play's status, so it never reads as a play gone silent.
QUEUE_POLL_S = 6.0
QUEUED_WORDS = "TorBox has it queued, waiting for a free download slot."
_clock = time.time
_spawn = subprocess.Popen


def path() -> Path:
    return CACHE_DIR / FILE_NAME


def key(media_id, episode) -> str:
    return f"{int(media_id)}:{int(episode)}"


def _locked(log: Log):
    """`flock` around a read-modify-write of the file (`core.flocked`): a lock that cannot be had within `LOCK_WAIT_S`
    is logged and the write goes ahead."""
    return core.flocked(CACHE_DIR / LOCK_NAME, log, "prefetch", "the prefetch", LOCK_WAIT_S)


def load() -> dict:
    data = cached_json(path(), None)
    return data if isinstance(data, dict) else {}


def _write(entries: dict) -> None:
    write_cache(path(), entries)
    try:
        os.chmod(path(), 0o600)
    except OSError:
        pass


def entry(media_id, episode) -> dict | None:
    got = load().get(key(media_id, episode))
    return got if isinstance(got, dict) else None


def found(media_id, episode) -> dict | None:
    """The download the prefetch left for this episode (added, or found already on the account), younger than
    `KEEP_S`; else None."""
    got = entry(media_id, episode)
    if not got or got.get("state") not in FOUND or not got.get("infoHash"):
        return None
    if _clock() - float(got.get("doneAt") or got.get("at") or 0) > KEEP_S:
        return None
    return got


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _plain(value):
    """`value` as `prefetch.json` reads it back (a tuple a list), so a part compares equal across the write."""
    return json.loads(json.dumps(value, default=str))


def _addons(registry) -> list[str]:
    """Each loaded add-on as `name:digest`, by content: add-ons declare only `API_VERSION`, so the file's bytes stand
    in for a version (a copy that rewrites the same bytes is the same add-on). The built-in Torznab add-on is its
    servers' addresses and names, never their keys; not its feed count, which a play's search refreshes from the
    server (`torznab_prowlarr.known_feeds`), so it can differ between the prefetch and the play with nothing changed.
    An unreadable file reads `?`."""
    out = []
    for ix in getattr(registry, "loaded", None) or []:
        if ix.builtin:
            got = torznab_servers.read()
            out.append(f"{ix.name}:{_digest([[s.url, s.name] for s in got.servers])}")
            continue
        where = getattr(ix.module, "__file__", None)
        try:
            body = Path(where).read_bytes() if where else None
        except OSError:
            body = None
        if body is None:
            out.append(f"{ix.name}:?")
            continue
        out.append(f"{ix.name}:{hashlib.sha256(body).hexdigest()[:12]}")
    return out


def fingerprint(settings: dict, second: str | None, pin: dict | None, registry) -> dict:
    """What decided the release a play of the episode picks, in named parts, so a play names the ones that changed
    since the prefetch (`differs`): the viewer's languages (`second` is the resolved second language, None when off),
    `subtitlePolicy`, `dubs`, the providers' order, the show's pin (its pack's hash and its group), the loaded add-ons
    (`_addons`, by content) and the other ranking settings (`RANKING_KEYS`). Plain values but the add-ons' digests
    (none is a secret, and a SKIP line then shows what changed); JSON-plain, so it reads back equal."""
    return {"languages": [str(settings.get("subtitleLanguage") or ""), second or core.FALLBACK_OFF],
            "subtitlePolicy": str(settings.get("subtitlePolicy") or ""), "dubs": str(settings.get("dubs") or ""),
            "providers": [str(p) for p in settings.get("providers") or []],
            "pin": _plain([((pin or {}).get("pack") or {}).get("info_hash"), (pin or {}).get("group")]) if pin
            else None,
            "addons": _addons(registry),
            "ranking": _plain({k: settings.get(k) for k in RANKING_KEYS})}


def differs(saved: dict, now: dict) -> list[str]:
    """The parts of `now` the saved fingerprint does not hold the same (a part it lacks differs)."""
    return [name for name, value in now.items() if not isinstance(saved, dict) or saved.get(name) != value]


def reusable(media_id, episode) -> tuple[dict | None, str]:
    """The entry whose saved release a play of this episode may play without its search, else None; and why. Pure
    apart from the one read of the file: the fingerprint, TorBox's cache and the pick are the play's to check."""
    got = entry(media_id, episode)
    if not got:
        return None, "no prefetch of this episode"
    state = got.get("state")
    age = _clock() - float(got.get("doneAt") or got.get("at") or 0)
    if state == STARTED:
        if age > RUNNING_S:
            return None, (f"the prefetch of this episode ended without an outcome (started {age:.0f} s ago, over "
                          f"{RUNNING_S:g} s)")
        return None, f"the prefetch of this episode is still running (started {age:.0f} s ago)"
    if state not in REUSABLE:
        return None, f"the prefetch named no release ({state}: {got.get('reason')})"
    record = got.get("record")
    if (not isinstance(record, dict) or not got.get("fingerprint")
            or str(record.get("info_hash") or "").lower() != str(got.get("infoHash") or "").lower()):
        return None, "the prefetch saved no release to play (an entry written before the reuse)"
    if age > REUSE_S:
        return None, (f"the prefetch found it {age / 3600:.1f} h ago, over {REUSE_S / 3600:g} h: only a search "
                      f"finds a later upload")
    return got, f"the prefetch found it {age:.0f} s ago ({state})"


def sideload_reuse(got: dict, policy: str, lang: str) -> tuple[bool, str]:
    """Whether the prefetched release plays without the viewer's language (`subtitle_fallback`, side-loaded) when the
    play finds it lacks it, and why. That is where a search under `prefer` ends: its cached releases are tried first,
    and the first cached one without the language plays once none of them carries it. So it plays only when the
    prefetch's own search had no other cached release its loop would try (`cachedOthers`): then the search's fallback
    can only be this one. With others, whatever they carried, the search decides: one may carry the language or be
    unread (probed first), and among releases that lack it the search plays the first in its own order, which moves
    with the seeders. Pure."""
    if policy != "prefer":
        return False, f"subtitlePolicy={policy}: a release without {lang} plays only under prefer"
    others = got.get("cachedOthers")
    if not isinstance(others, dict):
        return False, "the prefetch saved no count of its other cached releases (an entry written before it)"
    cached, with_lang, unknown = (int(others.get(k) or 0) for k in ("cached", "withLang", "unknown"))
    if cached:
        return False, (f"TorBox had {cached} other cached release(s) the search would try ({with_lang} with {lang}, "
                       f"{unknown} unread): the search decides among them")
    return True, ("TorBox had no other cached release the search would try: a search under prefer plays this one too, "
                  "side-loaded")


def latch(media_id, episode, record: dict, log: Log) -> dict | None:
    """Take the episode's latch: under the lock, no entry for it yet, then `record` written and read back. The entry
    already there when another tick (or another Service) took it first, else None once it is ours. Old entries go."""
    now = _clock()
    k = key(media_id, episode)
    with _locked(log):
        entries = {name: e for name, e in load().items()
                   if isinstance(e, dict) and now - float(e.get("doneAt") or e.get("at") or 0) <= KEEP_S}
        if k in entries:
            return entries[k]
        entries[k] = {**record, "mediaId": int(media_id), "episode": int(episode), "state": STARTED, "at": now}
        _write(entries)
    back = entry(media_id, episode)
    if not back or back.get("at") != now:
        # Not on disk: a second tick would take it again, so the start is not made (no latch, no prefetch).
        log.warn("prefetch", "latch", result="FAIL", key=k, path=str(path()),
                 suggest="the prefetch entry did not land; check the cache folder's permissions")
        return {"state": FAILED, "reason": "the latch could not be written"}
    return None


def update(media_id, episode, log: Log, **fields) -> dict | None:
    """Set `fields` on the episode's entry (made if missing: a command run by hand) and read it back."""
    k = key(media_id, episode)
    with _locked(log):
        entries = load()
        old = entries.get(k) if isinstance(entries.get(k), dict) else {
            "mediaId": int(media_id), "episode": int(episode), "at": _clock()}
        entries[k] = {**old, **fields, "doneAt": _clock()}
        _write(entries)
    back = entry(media_id, episode)
    if not back or any(back.get(name) != value for name, value in fields.items()):
        log.warn("prefetch", "record", result="FAIL", key=k, path=str(path()),
                 suggest="what the prefetch did is not on disk; the later play still finds the download on TorBox")
        return None
    return back


def torbox_leads(settings: dict) -> str | None:
    """Why the prefetch is not for this setup, or None: TorBox must be the first provider (the play asks it first) and
    have a token file (its contents are not read). The one check the trigger and the command share."""
    providers = list(settings.get("providers") or [])
    if not providers or providers[0] != "torbox":
        return "TorBox is not the first provider"
    return None if torbox_usable(settings) else "no TorBox token"


def wanted(now_playing: dict, episode: int, pos: float, settings: dict) -> tuple[int | None, str]:
    """From a tick of `episode` (whose now-playing it is) at `pos` seconds: the episode to download next, or None, and
    why. Cheap and pure apart from the token file's existence: the tick asks it every time."""
    if not as_bool(settings.get("prefetchNext"), True):
        return None, "prefetchNext is off"
    if float(pos or 0) < AFTER_S:
        return None, f"under {AFTER_S:g} s into the episode"
    loaded = now_playing.get("startedAt")
    if not isinstance(loaded, (int, float)) or isinstance(loaded, bool):
        return None, "now-playing says no load time: how long the episode was watched is unknown"
    if _clock() - float(loaded) < AFTER_S:
        return None, f"loaded under {AFTER_S:g} s ago (a resume or a seek): not two minutes watched yet"
    media = now_playing.get("media") or {}
    if not media:
        return None, "the play wrote no AniList snapshot: whether the next episode aired is unknown"
    nxt = next_episode(media, episode)
    if nxt["episode"] is None:
        return None, f"episode {episode} is the show's last ({media.get('episodes')})"
    if not nxt["aired"]:
        return None, f"episode {nxt['episode']} has not aired"
    progress = int(now_playing.get("listProgress") or 0)
    if now_playing.get("listStatus") != "REPEATING" and nxt["episode"] <= progress:
        return None, f"a rewatch: episode {nxt['episode']} is already watched (list progress {progress})"
    leads = torbox_leads(settings)
    if leads:
        return None, leads
    return nxt["episode"], f"{float(pos):g} s into episode {episode}; episode {nxt['episode']} has aired"


def new_trace() -> str:
    """The prefetch's own trace id: `omarchy-anime trace <it>` shows its search, its one add or why there was none. Time
    and a random part only: it is on the prefetch's command line, where any account can read it (`private_args`)."""
    return f"prefetch-{int(_clock())}-{random.randrange(16**4):04x}"


def start(log: Log, media_id: int, episode: int, from_episode: int, trace: str) -> int | None:
    """Start `omarchy-anime --trace <trace> prefetch <mediaId> <episode>` detached, with no pipes: its pid, or None when
    it could not be started (logged and recorded; the latch stays, so nothing retries it). The show and the episodes
    go in `OMAANIME_ARGS`, not on the command line (`private_args`)."""
    argv, env = private_args.command(["--trace", trace, "prefetch"],
                                     [str(int(media_id)), str(int(episode)), "--from", str(int(from_episode))])
    try:
        proc = _spawn(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      start_new_session=True, close_fds=True, env=env)
    except OSError as exc:
        log.warn("prefetch", "spawn", result="FAIL", mediaId=media_id, episode=episode, error=str(exc),
                 prefetch_trace=trace, suggest="the next episode is not downloaded ahead; its play adds it as before")
        update(media_id, episode, log, state=FAILED, reason=f"could not start: {exc}")
        return None
    log.info("prefetch", "spawn", result="PASS", mediaId=media_id, episode=episode, pid=proc.pid,
             prefetch_trace=trace, reason=f"episode {from_episode} is playing; `omarchy-anime trace {trace}` shows "
                                          f"what the prefetch did")
    return proc.pid


def play_starting(media_id, episode) -> str | None:
    """Why the prefetch must not add now: a play of this very episode has claimed and is still working (it makes its
    own add, and two side by side would be two downloads); else None."""
    got = playstate.read() or {}
    if got.get("state") != "running":
        return None
    if int(got.get("mediaId") or 0) != int(media_id) or int(got.get("episode") or 0) != int(episode):
        return None
    if _clock() - float(got.get("updatedAt") or 0) > playstate.SILENT_AFTER_S:
        return None
    return f"a play of episode {episode} is starting (trace {got.get('trace')}); it makes its own add"


def list_written_since(media_id, episode, since: float | None) -> str | None:
    """Why the prefetch must not write the episode's stored search: something wrote it after the prefetch started
    (`since`, wall clock), whatever became of that writer: a play's search, its refusal or the picker's search
    (`Search.save`: `searched_at`), a pick's `failed` mark (`mark_failed` keeps `searched_at`: the mark's own `at`).
    Those are the file's only writers, each stamped with `time.time()`; the file's mtime is not read (the kernel's
    coarse clock can lag `time.time()`). Else None. `play_starting` covers a play that has claimed and not written
    yet."""
    if since is None:
        return None
    try:
        data = cached_json(searches.path(media_id, episode), None) or {}
        marks = [float((m or {}).get("at") or 0) for m in (data.get("failed") or {}).values()]
        written = max([float(data.get("searched_at") or 0), *marks])
    except (TypeError, ValueError, AttributeError):
        return None
    if written <= since:
        return None
    return (f"the episode's stored search was written {written - since:.1f} s after the prefetch started (trace "
            f"{data.get('trace')}, refusal {bool(data.get('refusal'))}, failed marks {len(data.get('failed') or {})})")


def queued_entry(torbox: TorBox, info_hash: str) -> dict | None:
    """This hash's entry in TorBox's queue of downloads waiting for a slot (`QUEUED_PATH`), or None when it is not
    there. One read, no write. A queue that cannot be read raises its `StageError` (logged here); a refused account and
    a stop pass through unlogged, as they do from `TorBox.listed`."""
    info_hash = str(info_hash).lower()
    try:
        data = torbox._call("queued", "GET", QUEUED_PATH, query="type=torrent&bypass_cache=true",
                            expected=(404,)).get("data")
    except (BlockedError, Interrupted):
        raise
    except StageError as exc:
        torbox.log.warn("prefetch", "queued", result="FAIL", info_hash=info_hash, error=str(exc),
                        suggest="TorBox's queue could not be read; whether the download is still queued is unknown")
        raise
    rows = data if isinstance(data, list) else []
    row = next((r for r in rows if isinstance(r, dict) and str(r.get("hash") or "").lower() == info_hash), None)
    torbox.log.info("prefetch", "queued", result="PASS" if row else "EMPTY", info_hash=info_hash, queued=len(rows),
                    queued_id=(row or {}).get("id"),
                    reason="in TorBox's queue, waiting for a download slot" if row else "not in TorBox's queue")
    return row


def settle_queued(torbox: TorBox, log: Log, cand: dict, got: dict, say) -> dict | None:
    """Before the wait: the play reused the prefetch's download from TorBox's queue (`cli.prefetched_first` puts it on
    the play's listing, `torbox.prefetch_queued`), and `mylist?id=` does not list the queue's id. TorBox does not
    document that the id lives on once a slot frees, so the download is followed by its hash: each `QUEUE_POLL_S` one
    queue read, the status saying `QUEUED_WORDS` (no percentage, no state); once the hash has left the queue, one read
    of the account finds it there under whatever id TorBox gave it, and that stream is returned for the normal wait.
    None when this is not that case, or when the hash is in neither list (the caller goes on as for a vanished
    download). The three-hour cap (`providers.KEEP_WAITING_MAX_S`) ends it with a plain `StageError`."""
    info_hash = str(got.get("info_hash") or "").lower()
    queued_id = (getattr(torbox, "prefetch_queued", None) or {}).get(info_hash)
    if queued_id is None or str(queued_id) != str(got.get("torrent_id")):
        return None
    began = providersmod._wall_clock()
    polls = 0
    while True:
        polls += 1
        try:
            row, unread = queued_entry(torbox, info_hash), None
        except Interrupted:
            raise
        except StageError as exc:                       # logged by `queued_entry`; read again at the next poll
            row, unread = None, str(exc)
        waited = round(providersmod._wall_clock() - began, 1)
        if row is None and unread is None:
            break
        log.info("prefetch", "queued_wait", result="WAIT", info_hash=info_hash, queued_id=queued_id, poll=polls,
                 waited_s=waited, unread=unread, next_s=QUEUE_POLL_S,
                 reason="still in TorBox's queue, waiting for a free download slot" if unread is None else
                        "TorBox's queue could not be read; read again at the next poll")
        say("torbox", QUEUED_WORDS, torboxQueued=True, waited_s=waited)
        if waited >= providersmod.KEEP_WAITING_MAX_S:
            raise StageError("torbox.queued", f"TorBox still has episode {cand.get('episode')} queued, waiting for a "
                                              f"free download slot.",
                             "play it again later: the download stays in TorBox's queue", queued_id=queued_id)
        providersmod._wait_sleep(QUEUE_POLL_S)
    try:
        data = torbox._call("mylist", "GET", "/torrents/mylist", query="bypass_cache=true").get("data")
    except Interrupted:
        raise
    except StageError as exc:
        log.warn("prefetch", "queued_left", result="FAIL", info_hash=info_hash, queued_id=queued_id, error=str(exc),
                 suggest="the account's list could not be read; the play waits on the queue's id as before")
        return None
    rows = data if isinstance(data, list) else []
    row = next((r for r in rows if isinstance(r, dict) and (
        str(r.get("hash") or "").lower() == info_hash
        or info_hash in [str(a).lower() for a in r.get("alternative_hashes") or []])), None)
    if row is None or row.get("id") is None:
        log.warn("prefetch", "queued_left", result="FAIL", info_hash=info_hash, queued_id=queued_id, listed=len(rows),
                 polls=polls, suggest="gone from TorBox's queue and not on the account: the play goes on as for a "
                                      "download that vanished")
        return None
    got["torrent_id"] = row["id"]
    log.info("prefetch", "queued_left", result="PASS", info_hash=info_hash, queued_id=queued_id, torrent_id=row["id"],
             download_state=row.get("download_state"), polls=polls,
             reason="out of TorBox's queue and on the account: the wait runs on the id TorBox lists it under")
    return {"provider": "torbox", "cached": False, "torrent_id": row["id"], "status": row.get("download_state"),
            "progress": round(float(row.get("progress") or 0) * 100), "reused": True,
            "downloaded": row.get("total_downloaded")}


class Stop(Exception):
    """How `Client` ends the play's resolve at its first TorBox step. Not a `StageError`: every handler in
    `cli._resolve`, `cli.do_resolve` and `cli.play_pinned` lets it through, so no second release is tried."""

    def __init__(self, outcome: str, reason: str, cand: dict, **facts):
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason
        self.release = {"id": cand.get("id"), "title": cand.get("title"),
                        "infoHash": str(cand.get("info_hash") or "").lower()}
        # What the play's ranking read of it, for the later play's reuse gates (`cli.play_prefetched`).
        self.pick = {"hasLang": cand.get("has_lang"), "dubWords": cand.get("dub_words") or None}
        self.facts = facts


class Client(TorBox):
    """TorBox for the prefetch. `checkcached` and the account's list are TorBox's own; `resolve`, which the play's
    loop calls for the release it would stream or add, never streams: it stops (`Stop`) as `cached`, `on_account`,
    `skipped` (the cache or the account could not be read, or a play of the episode is starting), `added`, or
    `failed` (TorBox refused the one add: a limit, no server free, the account), with at most one `createtorrent`."""

    def __init__(self, http, log: Log, token: str, media_id: int, episode: int):
        super().__init__(http, log, token)
        self.media_id, self.episode = int(media_id), int(episode)
        self.answered = False                  # this client's `checkcached` answered (an empty answer counts)

    def check_cached(self, hashes: list[str]) -> dict[str, dict]:
        out = super().check_cached(hashes)
        self.answered = True
        return out

    def resolve(self, cand: dict, pick_file: str | None = None, reuse_listed: bool = False,
                lookup_only: bool = False) -> dict:
        t0 = time.monotonic()
        info_hash = str(cand.get("info_hash") or "").lower()
        if not self.answered:
            raise Stop(SKIPPED, "TorBox's cache could not be read: whether the episode needs a download is unknown",
                       cand)
        if info_hash in self.cached_hashes:
            raise Stop(CACHED, "TorBox has the release the play would pick cached: it starts at once", cand)
        try:
            listed = self.listed(info_hash)
        except StageError as exc:
            raise Stop(SKIPPED, f"the account's list could not be read ({exc}): an add might be a second download",
                       cand, error=str(exc)) from exc
        if listed is not None:
            raise Stop(ON_ACCOUNT, "the release the play would pick is already on the account", cand,
                       torrentId=listed.get("id"), progress=listed.get("progress"),
                       downloadState=listed.get("download_state"))
        busy = play_starting(self.media_id, self.episode)
        if busy:
            raise Stop(SKIPPED, busy, cand)
        try:
            torrent_id, cached_now = self._add(cand, info_hash, False, t0)
        except StageError as exc:
            raise Stop(FAILED, str(exc), cand, error=str(exc), stage_failed=exc.stage, status=exc.data.get("status"),
                       errorCode=exc.data.get("error_code"), limit=exc.data.get("limit"),
                       retryAfter=exc.data.get("retry_after")) from exc
        if cached_now:
            raise Stop(CACHED, "TorBox answered the add from its cache: no download was started", cand,
                       torrentId=torrent_id)
        back = self.read_back(torrent_id, info_hash)
        if back.get("queued"):
            raise Stop(QUEUED, "one add, in TorBox's queue: it starts when a download slot is free", cand,
                       torrentId=torrent_id, **back)
        raise Stop(ADDED, "one add: TorBox downloads it while this episode plays", cand, torrentId=torrent_id, **back)

    def read_back(self, torrent_id, info_hash: str) -> dict:
        """The add, read back: one `mylist?id=` of the new torrent (its state, never its stream), and when the list does
        not have it, TorBox's queue (`queued_entry`). `verified` False when neither has it (yet), which the later
        play's own lookup of the account still covers."""
        try:
            got = self._call("mylist", "GET", "/torrents/mylist",
                             query=f"id={torrent_id}&bypass_cache=true").get("data")
        except StageError as exc:
            got, error = None, str(exc)
        else:
            error = None
        row = got[0] if isinstance(got, list) and got else got if isinstance(got, dict) else None
        verified = bool(row) and str(row.get("id")) == str(torrent_id)
        try:
            queued = None if verified else queued_entry(self, info_hash)
        except StageError:                              # logged by `queued_entry`; the record says unverified
            queued = None
        found_it = verified or queued is not None
        (self.log.info if found_it else self.log.warn)(
            "prefetch", "read_back", result="PASS" if found_it else "FAIL", torrent_id=torrent_id, error=error,
            listed=verified, queued=queued is not None, download_state=(row or {}).get("download_state"),
            progress=(row or {}).get("progress"),
            suggest="" if found_it else "TorBox accepted the add but did not list it; the play looks the account up")
        if queued is not None:
            return {"verified": True, "queued": True, "downloadState": QUEUED}
        return {"verified": verified, "downloadState": (row or {}).get("download_state")}

    def link(self, cand: dict, torrent_id, pick_file: str | None = None) -> dict:
        raise Stop(SKIPPED, "the prefetch asks for no stream", cand)
