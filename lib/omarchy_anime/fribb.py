"""Fribb's anime-lists: whether an entry's TMDB id is a movie's or a TV series'.

ani.zip's `mappings.themoviedb_id` is a bare number with no kind, and for a movie entry it is often the parent TV
series' (678 of Fribb's 1,603 MOVIE entries with a TMDB id carry a `tv` one, 2026-10-27). Fribb's
`anime-list-full.json` carries the same numbers with the kind: `{"tv": 42511}` or `{"movie": [129]}`. The plugin keeps
a derived index of it (AniList id -> kind and numbers, nothing else) and the subtitle plan (`extsubs.episode_plan`)
reads it. Verified on commit c6012439 (2026-10-27): a JSON list of 39,597 objects, `anilist_id` an int and unique,
`themoviedb_id` absent or exactly one of `{"tv": int}` / `{"movie": [int, ...]}`; 8,278 entries carry both.

Fetched only by the subtitle worker (`subs external`, which runs after the play started), never on the resolve's
path: `require_lookup` reads what is on disk. The request carries nothing of the viewer's (one fixed URL)."""
from __future__ import annotations

import re
import time

from . import core
from .anilist import shown, valid_media_id
from .core import Http, Log, StageError, cached_json, write_cache

FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
INDEX_NAME = "tmdb-kinds.json"
ATTEMPT_NAME = "tmdb-kinds-attempt.json"
INDEX_VERSION = 1
# Fribb's list is rebuilt weekly (automated commits e25609f8, 08dc828d, c6012439: 2026-10-13, -20, -27); an id's
# kind does not change once filed, so an older index is still used when a refresh fails, whatever its age.
FRESH_S = 7 * 86400
# After a failed refresh, the next is tried no sooner than this: a list refused as too large or malformed would
# otherwise be read again on every play.
RETRY_S = 6 * 3600
# 7,512,806 bytes on 2026-11-01; 32 MiB is 4.4 times that. Larger than `DEFAULT_MAX_BYTES` (16 MiB, 2.2 times) so
# that the list's growth does not quietly freeze the index; parsing today's list costs 0.13 s and about 40 MB.
MAX_BYTES = 32 * 1024 * 1024
# Bounds on an untrusted answer: 39,597 objects and at most 3 numbers per entry on 2026-11-01.
MAX_ITEMS = 400_000
MAX_NUMBERS = 20
# A list with fewer usable entries than this is not Fribb's (8,278 on 2026-11-01): the old index is kept.
MIN_ENTRIES = 1000
TMDB_ID_MAX = 2**31 - 1
TIMEOUT_S = 20.0
KINDS = ("tv", "movie")
# The entry types whose IMDb id is a series': a movie's IMDb id one of them carries is the series', not the movie's.
SERIES_TYPES = ("TV", "ONA")
IMDB_RE = re.compile(r"tt\d{7,10}")


def index_path():
    return core.CACHE_DIR / INDEX_NAME


def _number(value) -> int | None:
    return value if type(value) is int and 0 < value <= TMDB_ID_MAX else None


def _entry(tmdb) -> list | None:
    """Fribb's `themoviedb_id` as `[kind, [numbers]]`, or None when it is not exactly one known kind with numbers."""
    if not isinstance(tmdb, dict) or len(tmdb) != 1:
        return None
    kind, value = next(iter(tmdb.items()))
    if kind not in KINDS:
        return None
    values = value if isinstance(value, list) else [value]
    if not values or len(values) > MAX_NUMBERS:
        return None
    numbers = [_number(v) for v in values]
    return None if None in numbers else [kind, sorted(set(numbers))]


def _imdb_ids(value) -> list[str]:
    """Fribb's `imdb_id` list as the well-formed ids in it (it carries `""` for some); [] for anything else."""
    if not isinstance(value, list) or len(value) > MAX_NUMBERS:
        return []
    return [v for v in value if isinstance(v, str) and IMDB_RE.fullmatch(v)]


def build_index(body) -> tuple[dict | None, dict]:
    """The derived index from Fribb's answer, and counts for the log. None when the answer is refused whole: not a
    list, more than `MAX_ITEMS` objects, or fewer than `MIN_ENTRIES` usable entries. An entry whose AniList id or
    TMDB field is malformed is skipped and counted; an AniList id named twice with different answers is dropped.

    Each entry is `[kind, numbers, imdb]`: `imdb` the IMDb ids Fribb lists for the entry that no other TV or ONA
    object carries. When the TMDB id is dropped for its kind, only such an id may lead the subtitle plan's id step:
    198 of the 678 tv-kind MOVIE entries of 2026-10-27 carry only their series' IMDb ids, which would reach the series'
    files (`extsubs.planned_tmdb_kind`)."""
    if not isinstance(body, list):
        return None, {"refused": f"not a list: {type(body).__name__}"}
    if len(body) > MAX_ITEMS:
        return None, {"refused": f"{len(body)} objects, more than {MAX_ITEMS}"}
    entries: dict[str, list] = {}
    conflicts: set[str] = set()
    counts = {"items": len(body), "no_tmdb": 0, "bad": 0, "conflicts": 0}
    series: dict[str, set[int]] = {}                   # IMDb id -> the TV/ONA objects (by position) that carry it
    for n, item in enumerate(body):
        if isinstance(item, dict) and item.get("type") in SERIES_TYPES:
            for imdb in _imdb_ids(item.get("imdb_id")):
                series.setdefault(imdb, set()).add(n)
    for n, item in enumerate(body):
        if not isinstance(item, dict):
            counts["bad"] += 1
            continue
        if item.get("themoviedb_id") is None:
            counts["no_tmdb"] += 1
            continue
        aid, entry = valid_media_id(item.get("anilist_id")), _entry(item.get("themoviedb_id"))
        if aid is None:
            counts["no_tmdb" if item.get("anilist_id") is None else "bad"] += 1
            continue
        if entry is None:
            counts["bad"] += 1
            continue
        key = str(aid)
        entry = [*entry, sorted({i for i in _imdb_ids(item.get("imdb_id")) if not series.get(i, set()) - {n}})]
        if key in entries and entries[key] != entry:
            conflicts.add(key)
        entries.setdefault(key, entry)
    for key in conflicts:
        del entries[key]
    counts["conflicts"], counts["entries"] = len(conflicts), len(entries)
    if len(entries) < MIN_ENTRIES:
        return None, {**counts, "refused": f"{len(entries)} usable entries, fewer than {MIN_ENTRIES}"}
    return entries, counts


def read_index(log: Log | None = None) -> dict | None:
    """The index on disk when it is well formed (`version`, `fetched_at`, `entries`), else None."""
    data = cached_json(index_path(), None)
    ok = isinstance(data, dict) and data.get("version") == INDEX_VERSION and isinstance(data.get("entries"), dict) \
        and isinstance(data.get("fetched_at"), (int, float)) and not isinstance(data.get("fetched_at"), bool)
    if data is not None and not ok and log:
        log.warn("fribb.index", "read", result="FAIL", path=str(index_path()),
                 suggest="the TMDB kinds file is not one this build wrote; it is fetched again by the next play")
    return data if ok else None


def _attempt_recent(now: float) -> dict | None:
    """The last failed refresh when it is younger than `RETRY_S`."""
    data = cached_json(core.CACHE_DIR / ATTEMPT_NAME, RETRY_S)
    return data if isinstance(data, dict) and isinstance(data.get("at"), (int, float)) and \
        now - data["at"] < RETRY_S else None


def _remember_attempt(reason: str) -> None:
    """The failed refresh, so the next is tried after `RETRY_S`; a cache that cannot be written costs the back-off."""
    try:
        write_cache(core.CACHE_DIR / ATTEMPT_NAME, {"at": time.time(), "reason": reason[:300]})
    except OSError:
        pass


def refresh(http: Http, log: Log) -> dict | None:
    """Fetch Fribb's list and write the index atomically; the new index, or None when the fetch or the list failed
    (the failure is logged and remembered for `RETRY_S`; the old index on disk is left as it was)."""
    t0 = time.monotonic()
    reason, counts = None, {}
    try:
        status, body, headers = http.request("fribb.index", "GET", FRIBB_URL, retries=1, timeout=TIMEOUT_S,
                                             max_bytes=MAX_BYTES, max_retry_after=0)
        if status != 200:
            reason = f"Fribb's list answered {status}"
        else:
            entries, counts = build_index(body)
            if entries is None:
                reason = f"the list was refused: {counts['refused']}"
    except StageError as exc:
        reason = str(exc)
    ms = (time.monotonic() - t0) * 1000
    if reason:
        log.warn("fribb.index", "refresh", result="FAIL", ms=ms, url=FRIBB_URL, reason=reason, **counts,
                 suggest=f"the kinds already on disk (if any) are used; tried again after {RETRY_S // 3600} h")
        _remember_attempt(reason)
        return None
    etag = next((v for k, v in (headers or {}).items() if k.lower() == "etag"), None)
    index = {"version": INDEX_VERSION, "source": FRIBB_URL, "fetched_at": time.time(),
             "etag": etag if isinstance(etag, str) else None, "entries": entries}
    try:
        write_cache(index_path(), index)
    except OSError as exc:
        log.warn("fribb.index", "write", result="FAIL", path=str(index_path()), error=str(exc),
                 suggest="the kinds are used for this play and fetched again next play")
        return index
    (core.CACHE_DIR / ATTEMPT_NAME).unlink(missing_ok=True)
    written = read_index()
    log.info("fribb.index", "refresh", result="PASS" if written else "FAIL", ms=ms, url=FRIBB_URL, etag=index["etag"],
             **counts, path=str(index_path()),
             suggest=None if written else "the index did not read back; the kinds are used for this play only")
    return index


def lookup(http: Http | None, log: Log, anilist_id, *, refresh_due: bool) -> dict:
    """What Fribb says of this entry's TMDB id: `{"index": "fresh"|"stale"|"none", "entry": {"kind", "ids", "imdb"} |
    None, "age_h", "why"}`; `imdb` is None for an index written before it carried the IMDb ids (`build_index`).
    With `refresh_due` (the subtitle worker only), an index older than `FRESH_S` or missing is fetched first, unless
    a refresh failed less than `RETRY_S` ago; a failed one falls back to the index on disk. Without it nothing is
    fetched (the resolve's `require` pre-check). Never raises."""
    try:
        now = time.time()
        index = read_index(log)
        age = now - index["fetched_at"] if index else None
        why = None
        if refresh_due and http is not None and (index is None or age > FRESH_S):
            failed = _attempt_recent(now)
            if failed:
                why = f"a refresh failed {round((now - failed['at']) / 3600, 1)} h ago ({failed.get('reason')})"
                log.info("fribb.index", "refresh", result="SKIP", reason=why,
                         age_h=round(age / 3600, 1) if age is not None else None)
            else:
                try:
                    fetched = refresh(http, log)
                except Exception as exc:                # noqa: BLE001 - the index on disk still answers
                    log.warn("fribb.index", "refresh", result="FAIL", url=FRIBB_URL,
                             error=f"{type(exc).__name__}: {exc}",
                             suggest=f"the kinds already on disk (if any) are used; tried again after "
                                     f"{RETRY_S // 3600} h")
                    _remember_attempt(f"{type(exc).__name__}: {exc}")
                    fetched = None
                if fetched:
                    index, age = fetched, 0.0
                else:
                    why = "the refresh failed"
        if index is None:
            return {"index": "none", "entry": None, "age_h": None,
                    "why": why or ("no TMDB kinds on disk yet" if not refresh_due else "no TMDB kinds")}
        state = "fresh" if age <= FRESH_S else "stale"
        aid = valid_media_id(anilist_id)
        raw = index["entries"].get(str(aid)) if aid is not None else None
        entry = _entry({raw[0]: raw[1]}) if isinstance(raw, list) and len(raw) in (2, 3) and isinstance(raw[0], str) \
            else None
        imdb = (_imdb_ids(raw[2]) if isinstance(raw[2], list) else []) if entry and len(raw) == 3 else None
        return {"index": state, "entry": {"kind": entry[0], "ids": entry[1], "imdb": imdb} if entry else None,
                "age_h": round(age / 3600, 1), "why": why}
    except Exception as exc:                            # noqa: BLE001 - the kind is a hint; the subtitles go on
        log.warn("fribb.index", "lookup", result="FAIL", anilist_id=shown(anilist_id),
                 error=f"{type(exc).__name__}: {exc}", suggest="the plan keeps ani.zip's TMDB id as before")
        return {"index": "none", "entry": None, "age_h": None, "why": f"{type(exc).__name__}"}
