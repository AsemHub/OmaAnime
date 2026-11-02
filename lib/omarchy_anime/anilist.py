"""AniList GraphQL client and the pure list logic.

Every query below was verified live on 2026-09-14. Reads need no token;
writes need `Authorization: Bearer <jwt>`. The API is in a degraded 30 req/min state; every response's
`x-ratelimit-remaining` is read and the client sleeps when it runs low.
"""
from __future__ import annotations

import base64
import json
import math
import random
import re
import time
from pathlib import Path

from .core import (CACHE_DIR, CONFIG_DIR, LARGE_MAX_BYTES, Http, Log, StageError, cached_json, read_token, write_cache,
                   write_token)

GRAPHQL_URL = "https://graphql.anilist.co"
PIN_REDIRECT = "https://anilist.co/api/v2/oauth/pin"

# The six statuses AniList's MediaListStatus enum accepts. `REPEATING` is what anilist.co calls
# "Rewatching"; the API value is the one that goes on the wire.
LIST_STATUSES = ("CURRENT", "PLANNING", "COMPLETED", "PAUSED", "DROPPED", "REPEATING")
# The stages that read a whole list (`list_entries`): their answer grows with the viewer's list, so they read to
# `LARGE_MAX_BYTES`, every other query to the default cap.
LIST_STAGES = ("list", "watching")

MEDIA_FIELDS = """
  id idMal
  title { romaji english native }
  synonyms
  coverImage { large medium }
  bannerImage
  episodes duration status format season seasonYear countryOfOrigin
  averageScore meanScore popularity isAdult genres
  nextAiringEpisode { episode airingAt timeUntilAiring }
  startDate { year month day }
  endDate { year month day }
"""

RELATION_FIELDS = """
  relations { edges { relationType node {
    id type format status title { romaji english } episodes season seasonYear
    nextAiringEpisode { episode airingAt }
  } } }
"""

# Crunchyroll's per-episode thumbnails, the second still source. On the
# single-media query ONLY. `MEDIA_FIELDS` is shared by every list/top/search/airing query, and a
# long show's streaming list is hundreds of entries -- it would grow every list fetch and eat the
# AniList budget this client paces.
MEDIA_DETAIL_FIELDS = """
  streamingEpisodes { title thumbnail site }
"""

SEASON_FORMATS = {"TV", "TV_SHORT", "ONA", "OVA", "MOVIE", "SPECIAL"}

# Bumped whenever the top-100 payload gains a field, so a cached file written by an older
# version is not served for its 24-hour lifetime with the new field missing.
TOP100_CACHE_NAME = "top100-v2.json"
# How long `top100` serves its saved file before asking AniList again.
TOP100_MAX_AGE = 86400


def top100_path() -> Path:
    """Where `top100` saves its answer (read at call time: the tests move `CACHE_DIR`)."""
    return CACHE_DIR / "anilist" / TOP100_CACHE_NAME


# AniList has been in a degraded 30 requests/minute state since 2025 (seen live 2026-09-14);
# the real budget is read from `x-ratelimit-limit` on every response and this is only the fallback.
RATE_LIMIT = 30
# The budget is per account, not per process, and every CLI command is a new process: the last
# response's counters are left here so the next command starts paced instead of rediscovering them.
RATE_FILE = "anilist-rate.json"
# docs.anilist.co/guide/rate-limiting (read 2026-10-29): 90 requests a minute (30 while degraded); a 429 starts "a 1
# minute timeout", its `Retry-After` in seconds and `X-RateLimit-Reset` a Unix time. A value past these is not
# AniList's budget and is ignored: a limit of -1 or of 400 digits, a reset of `inf`, failed every call.
RATE_LIMIT_MAX = 10_000
RATE_AHEAD_MAX_S = 120

# What an answer may hand on, checked where it is read. An id names a file (`titles/<id>.json`, ani.zip's
# `mappings/<id>.json`) and a request: GraphQL's `Int` is a signed 32-bit number (spec, October 2021, 3.5.1); the
# largest id recorded is 217,434. Recorded maxima over 3,103 media (a play cache and a measurement corpus of answers,
# 2026-10-29): 21 synonyms on one entry, a 156-character title, 645 relation rows (the largest franchise answer,
# 110 entries two hops out). Each cap is several times that.
ANILIST_ID_MAX = 2**31 - 1
SYNONYMS_MAX = 100
TITLE_CHARS_MAX = 500
RELATION_ROWS_MAX = 5000


# ----------------------------------------------------------------------------- pure logic

def valid_media_id(value) -> int | None:
    """`value` when it is an AniList id: an int in 1..ANILIST_ID_MAX, not a bool. The one check an id taken from an
    answer passes before it names a file or a request: a string never does, so `"../x"` or `"1#/…"` cannot
    become a path."""
    return value if type(value) is int and 0 < value <= ANILIST_ID_MAX else None


def shown(value) -> str:
    """A value from an answer, short enough for a log line."""
    try:
        return repr(value[:40] if isinstance(value, str) else value)[:60]
    except ValueError:                  # an int past `sys.get_int_max_str_digits()`
        return f"<{type(value).__name__}>"


def bounded_titles(title, synonyms) -> tuple[dict, list[str], int]:
    """An answer's `title { romaji english native }` and `synonyms`, bounded: a title that is not a string,
    or is longer than TITLE_CHARS_MAX, is left out (None), and so is such a synonym; the first SYNONYMS_MAX synonyms
    are kept. A `title` that is not an object and `synonyms` that is not a list read as missing. Returns the titles,
    the synonyms and how many strings were left out (too long, or past the count)."""
    t = title if isinstance(title, dict) else {}
    names = {k: t[k] if isinstance(t.get(k), str) and len(t[k]) <= TITLE_CHARS_MAX else None
             for k in ("romaji", "english", "native")}
    dropped = sum(1 for k, v in names.items() if v is None and isinstance(t.get(k), str))
    given = synonyms if isinstance(synonyms, list) else []
    kept: list[str] = []
    for n, s in enumerate(given):
        if len(kept) == SYNONYMS_MAX:
            dropped += len(given) - n
            break
        if isinstance(s, str):
            if len(s) <= TITLE_CHARS_MAX:
                kept.append(s)
            else:
                dropped += 1
    return names, kept, dropped


def rate_record(state, now: float | None = None) -> dict | None:
    """The saved budget (`anilist-rate.json`) as `{remaining, limit, reset}` when it is this minute's and in AniList's
    range, else None: `reset` a finite time still ahead and at most RATE_AHEAD_MAX_S away, `limit` an int in
    1..RATE_LIMIT_MAX (RATE_LIMIT when missing), `remaining` an int, clamped to 0..limit. Every client and `status`
    read the file through it, so a bad record written before the check is ignored instead of failing each call."""
    if not isinstance(state, dict):
        return None
    now = time.time() if now is None else now
    try:
        reset = float(state.get("reset") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    limit, remaining = state.get("limit") or RATE_LIMIT, state.get("remaining")
    if (not (math.isfinite(reset) and now < reset <= now + RATE_AHEAD_MAX_S)
            or type(limit) is not int or not 1 <= limit <= RATE_LIMIT_MAX or type(remaining) is not int):
        return None
    return {"remaining": max(0, min(remaining, limit)), "limit": limit, "reset": reset}


def rate_expired(state, now: float | None = None) -> bool:
    """A saved budget whose `reset` is a finite time already past (or missing): last minute's, not a bad one."""
    try:
        reset = float(state.get("reset") or 0) if isinstance(state, dict) else math.nan
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(reset) and reset <= (time.time() if now is None else now)


def header_int(value, low: int, high: int) -> int | None:
    """A header's whole number in low..high, else None: ASCII digits only (no sign, no `٣`), at most 12 of them."""
    text = str(value).strip() if value is not None else ""
    if not (text.isascii() and text.isdecimal()) or len(text) > 12:
        return None
    n = int(text)
    return n if low <= n <= high else None

def jwt_claims(token: str) -> dict:
    """Decode the JWT payload without verifying (we only need `exp` and `sub`). A payload that is
    not a JSON object (`1`, `null`, `[]`) has no claims: every caller calls `.get` on it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


EXPIRY_WARNING_DAYS = 14


def token_expiry(token: str | None, now: float | None = None, warn_days: int = EXPIRY_WARNING_DAYS) -> dict:
    """Read the JWT `exp` locally and say how long the token has left.

    AniList's pin-flow tokens last a year and cannot be refreshed: when one expires every read still
    works (reads need no token) but every write fails, which looks like "my progress stopped saving"
    rather than "log in again". Costs nothing — the claim is in the token we already hold.
    """
    now = time.time() if now is None else now
    exp = jwt_claims(token or "").get("exp")
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return {"expiresAt": None, "daysLeft": None, "expiring": False, "expired": False}
    left = exp - now
    return {"expiresAt": exp, "daysLeft": max(0, int(left // 86400)), "expiring": left <= warn_days * 86400,
            "expired": left <= 0}


def walk_sequels(media_id: int, relations_for, max_depth: int = 8) -> list[dict]:
    """Follow SEQUEL edges (anime only) from media_id. `relations_for(id)` returns the edge list.
    Returns the chain of sequel nodes in order; cycle-safe."""
    chain: list[dict] = []
    seen = {media_id}
    current = media_id
    for _ in range(max_depth):
        edges = relations_for(current) or []
        nxt = None
        for edge in edges:
            node = edge.get("node") or {}
            if edge.get("relationType") != "SEQUEL" or node.get("type") != "ANIME":
                continue
            if node.get("format") not in SEASON_FORMATS or node.get("id") in seen:
                continue
            nxt = node
            break
        if nxt is None:
            break
        chain.append(nxt)
        seen.add(nxt["id"])
        current = nxt["id"]
    return chain


def airing_node(chain: list[dict]) -> dict | None:
    for node in chain:
        if node.get("status") == "RELEASING":
            return node
    return None


def merge_airing(rows: list[dict] | object) -> tuple[list[dict] | object, list[dict], int]:
    """One Airing row per airing show: the first row per `airing.id`, the rest dropped. Several Top 100
    entries of one franchise walk their sequel chains to the same airing season.

    The rows come in Top 100 order, so the kept row carries the show's best rank and the order stays the Top 100
    order. A row that reached the show through a sequel chain carries only the relation node (no cover); when a
    dropped row is the show's own Top 100 entry (`top.id == airing.id`, the full media), the kept row takes that media
    as its `airing`, without its `rank` (the row's rank is `top.rank`), and keeps its own `top` and `chain`.

    A row with no int airing id is kept as it is, and so is a cache file that is not a list (it is returned as
    given): the hit path returned those unchanged before the merge. Returns (rows, one `{id, kept, dropped,
    own_entry}` per merged show, rows dropped); `kept` and `dropped` are Top 100 ranks, `own_entry` the rank whose
    media the kept row took, or None."""
    if not isinstance(rows, list):
        return rows, [], 0

    def airing_id(row) -> int | None:
        airing = row.get("airing") if isinstance(row, dict) else None
        mid = airing.get("id") if isinstance(airing, dict) else None
        # AniList ids are ints; anything else (a `bool` is an `int` to Python) is never merged.
        return mid if isinstance(mid, int) and not isinstance(mid, bool) else None

    def top_of(row: dict) -> dict:
        top = row.get("top")
        return top if isinstance(top, dict) else {}

    kept: list = []
    at: dict[int, int] = {}                  # airing id -> its row's index in `kept`
    shows: dict[int, dict] = {}              # airing id -> what the merge did with it, in Top 100 order
    for row in rows:
        mid = airing_id(row)
        if mid is None:
            kept.append(row)
            continue
        if mid not in at:
            at[mid] = len(kept)
            kept.append(row)
            continue
        first = kept[at[mid]]
        show = shows.setdefault(mid, {"id": mid, "kept": top_of(first).get("rank"), "dropped": [], "own_entry": None})
        show["dropped"].append(top_of(row).get("rank"))
        # A dropped row that is the show's own entry means the kept one is not: Top 100 ids are unique.
        if show["own_entry"] is None and top_of(row).get("id") == mid:
            own = {k: v for k, v in row["airing"].items() if k != "rank"}
            kept[at[mid]] = dict(first, airing=own)
            show["own_entry"] = top_of(row).get("rank")
    return kept, list(shows.values()), len(rows) - len(kept)


def scheduled_episode(media: dict | None) -> int | None:
    """The episode AniList's airing schedule names next (`nextAiringEpisode.episode`, the first one not yet aired): a
    whole number above 0, or None for no schedule. A whole float (`3.0`) is its int, as JavaScript reads it; a string,
    a bool or a fraction is no schedule, never an error. `BrowseModel.js:scheduledEpisode` is the same test (the shared
    cells cases hold them equal)."""
    nxt = media.get("nextAiringEpisode") if isinstance(media, dict) else None
    value = nxt.get("episode") if isinstance(nxt, dict) else None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def not_out(media: dict | None) -> bool:
    """AniList lists the entry as not yet released and gives no airing schedule (`scheduled_episode`): no episode of
    it is out. A scheduled `NOT_YET_RELEASED` entry is not this (the episodes before the scheduled one aired), nor is
    any other status. `BrowseModel.js:notOut` is the same test."""
    if not isinstance(media, dict) or media.get("status") != "NOT_YET_RELEASED":
        return False
    return scheduled_episode(media) is None


def next_episode(media: dict, progress: int, now: float | None = None) -> dict:
    """The episode to play next, or the last one to rewatch when the next has not aired.
    Returns {episode, aired, airsAt, rewatch}.
    - aired=True: episode is available now.
    - aired=False with airsAt: the next episode is scheduled; rewatch=last watched.
    - aired=False, airsAt None, notOut=True: the entry is not out (`not_out`); rewatch=last watched.
    - episode=None: the show is finished and fully watched; rewatch=last."""
    now = time.time() if now is None else now
    progress = max(int(progress or 0), 0)
    total = media.get("episodes")
    nxt = media.get("nextAiringEpisode") or {}
    candidate = progress + 1
    if total and candidate > total:
        return {"episode": None, "aired": False, "airsAt": None, "rewatch": progress or None}
    if not_out(media):
        return {"episode": candidate, "aired": False, "airsAt": None, "rewatch": progress or None, "notOut": True}
    scheduled = scheduled_episode(media)
    if scheduled and candidate >= scheduled:
        # The next scheduled episode is the one (or before the one) we want: not out yet.
        return {"episode": candidate, "aired": False, "airsAt": nxt.get("airingAt"), "rewatch": progress or None}
    return {"episode": candidate, "aired": True, "airsAt": None, "rewatch": None}


def remember_titles(media: dict, log: Log | None = None) -> None:
    """Persist romaji/english/native/synonyms per media id so `resolve` can hand free-text indexers
    title variants and match their results without an AniList call.

    `native` is stored even though it usually also appears in `synonyms`: a group that titles its
    release in the original script is matched by the series test only if the script is in the
    variant list, and "usually" is not a guarantee worth relying on for a field the query already
    returns (`title { romaji english native }`, line 26).

    The cover URLs ride along for the same reason: `omarchy-anime covers warm --media <id>` then has
    the URLs locally and costs no AniList request.

    `seasonYear` and `format` are for the external-subtitle plan: a file dated for
    another series is dropped, and a MOVIE plans the movie search even where ani.zip has no `type`.
    `episodes` tells resolve a one-episode entry, whose releases do not say episode 1.
    `countryOfOrigin` (ISO 3166-1 alpha-2, "Where the media was created", AniList's Media reference, read
    2026-10-19) gives the show's original audio language for the dub reading (`score.original_audio`).
    `status`, `nextAiringEpisode` and `startDate` (a FuzzyDate: year, month, day, each optional) are what a play
    reads to refuse an entry that is not out (`not_out`, `cli.not_out_gate`), before anything else, with no request.
    `startDate` and `endDate` (FuzzyDates) give a TV series' first and last year (`cli.entry_years`), which the
    own-season rule reads a series year against (`score.later_series_years`).
    All come from `MEDIA_FIELDS`, which every caller queries, and are read back by `plan_hints`. A file
    written before a field existed lacks it until the next list, search or media fetch that goes to
    AniList rewrites it.

    The id names the file, so one that is not an AniList id (`valid_media_id`: a string such as `"../x"`) writes
    nothing, and the titles are bounded (`bounded_titles`), each with one line on `log`."""
    if not isinstance(media, dict) or not media.get("id"):
        return
    mid = valid_media_id(media["id"])
    if mid is None:
        if log is not None:
            log.warn("anilist.titles", "remember", result="SKIP", id=shown(media["id"]),
                     reason=f"the answer's id is not an AniList id (an int in 1..{ANILIST_ID_MAX}): it would name the "
                            "file, so nothing is written",
                     suggest="AniList answered an entry with a malformed id; the entry still shows, its titles are "
                             "not remembered")
        return
    names, synonyms, dropped = bounded_titles(media.get("title"), media.get("synonyms"))
    if dropped and log is not None:
        given = media.get("synonyms")
        log.warn("anilist.titles", "cap", result="CUT", anilist_id=mid, dropped=dropped,
                 synonyms=len(given) if isinstance(given, list) else None, synonyms_max=SYNONYMS_MAX,
                 title_chars_max=TITLE_CHARS_MAX,
                 reason="titles past the count or the length an AniList entry has are left out; the first are kept")
    cover = media.get("coverImage") if isinstance(media.get("coverImage"), dict) else {}
    nxt = media.get("nextAiringEpisode") if isinstance(media.get("nextAiringEpisode"), dict) else None
    rec = {**names, "synonyms": synonyms, "seasonYear": media.get("seasonYear"),
           "format": media.get("format"), "episodes": media.get("episodes"),
           "countryOfOrigin": media.get("countryOfOrigin"),
           "status": media.get("status"),
           # Without `timeUntilAiring`, which changes on every read and would rewrite the file each time.
           "nextAiringEpisode": {k: nxt.get(k) for k in ("episode", "airingAt")} if nxt else None,
           "startDate": media.get("startDate") if isinstance(media.get("startDate"), dict) else None,
           "endDate": media.get("endDate") if isinstance(media.get("endDate"), dict) else None,
           "cover": {k: cover.get(k) for k in ("large", "medium")}}
    path = CACHE_DIR / "anilist" / "titles" / f"{mid}.json"
    if cached_json(path, None) != rec:
        write_cache(path, rec)


def known_titles(media_id: int) -> dict | None:
    """The titles cache's record, its titles bounded as `remember_titles` bounds them: a file written before the caps
    costs no more than a new one. Not logged here (every play reads it several times); the next answer that
    rewrites the file logs its cut."""
    known = cached_json(CACHE_DIR / "anilist" / "titles" / f"{media_id}.json", None)
    if not isinstance(known, dict):
        return None
    names, synonyms, _dropped = bounded_titles(known, known.get("synonyms"))
    return {**known, **names, "synonyms": synonyms}


def known_spellings(media_id: int) -> list[str]:
    """Every spelling AniList gives the entry (romaji, english, native, all synonyms), from the titles
    cache; empty when no list command or fetch has remembered it. What tells an AniList title apart from ani.zip's
    `x-jat` fan code when the two are the same string (`extsubs.fan_code_only`)."""
    known = known_titles(media_id) or {}
    out: list[str] = []
    for t in (known.get("romaji"), known.get("english"), known.get("native"), *(known.get("synonyms") or [])):
        if t and t not in out:
            out.append(t)
    return out


def plan_hints(media_id: int) -> dict:
    """What the external-subtitle plan takes from the titles cache (`remember_titles`), as keyword
    arguments for `extsubs.episode_plan` and `extsubs.require_lookup`: `seasonYear`, `format`
    and the AniList spellings (`anilist_titles`, so the plan reads the entry's season from the same source
    as the resolve). A file written before a field existed gives None / "" for it."""
    known = known_titles(media_id) or {}
    return {"season_year": known.get("seasonYear"), "anilist_format": known.get("format") or "",
            "anilist_titles": known_spellings(media_id)}


def remembered_episodes(media_id: int) -> int | None:
    """AniList's episode count from the titles cache (`remember_titles`), or None when the file predates the
    field or AniList gave none (a show still airing)."""
    n = (known_titles(media_id) or {}).get("episodes")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None


# The AniList answers saved on disk that carry whole media (`MEDIA_FIELDS`): the user's lists (and the copies a
# progress write leaves, `saved_copy`), Top 100 and Airing. `saved_answer` reads them; nothing here asks AniList.
SAVED_ANSWERS = ("watching-*.json", "list-*.json", "saved-watching-*.json", "saved-list-*.json", TOP100_CACHE_NAME,
                 "airing-from-top.json")


def saved_answer(media_id: int) -> dict | None:
    """The newest AniList answer saved on disk that carries the entry with its `status` and its airing schedule, for
    a titles file that has neither (written before it kept them, or never written): `{media, source, age_s, read}`,
    or None. Newest by the file's mtime, which is when AniList gave it, except `airing-from-top.json`: its `airing`
    node can be copied from an older Top 100 answer, which is harmless because that node is always RELEASING, so the
    file can only let a play go on. A row is read as the entry itself (Top 100), its `media` (a list's row, never its
    `next`) or its `airing` (Airing; its `top` summary has no schedule). A node is taken only with `status` and the
    `nextAiringEpisode` key (null is an answer: no schedule): one without the key would make a scheduled entry not
    out. No request; `read` is how many files were parsed before one carried it."""
    folder = CACHE_DIR / "anilist"
    found = []
    for pattern in SAVED_ANSWERS:
        try:
            paths = list(folder.glob(pattern))
        except OSError:
            continue
        for path in paths:
            try:
                found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    read = 0
    for mtime, path in sorted(set(found), reverse=True):
        rows = cached_json(path, None)
        read += 1
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for node in (row, row.get("media"), row.get("airing")):
                if (isinstance(node, dict) and node.get("id") == media_id and node.get("status")
                        and "nextAiringEpisode" in node):
                    return {"media": node, "source": str(path), "age_s": round(time.time() - mtime, 1),
                            "read": read}
    return None


# ----------------------------------------------------------------------------- client

# The `reason`s AniList's answer to a rejected token carries. A public read that fails
# with one of them is sent again without the token.
REJECTED_TOKEN_REASONS = ("token_expired", "token_rejected")


def saved_copy(path: Path) -> Path:
    """The fallback-only copy of a saved list (`saved-watching-<key>.json`), outside the `watching-*.json` and
    `list-*.json` names a fresh read is served from."""
    return path.with_name(f"saved-{path.name}")


class Unreachable(StageError):
    """AniList could not be read for a reason on its side or the network's, never the account's. The stage,
    message and data are the failure's as before, so its JSON is unchanged; `kind` is the short reason a list read's
    saved copy carries (`list_entries`): unreachable, timeout, bad_answer, server_error, rate_limited, outage."""

    def __init__(self, kind: str, stage: str, message: str, suggest: str = "", **data):
        super().__init__(stage, message, suggest, **data)
        self.kind = kind


# A 403 whose message says one of these is about the sign-in, not an outage. AniList's own answers to a bad token are
# 400 "Invalid token" and 401 (probed 2026-09-27); this keeps a 403 worded like them on the account's side too.
# Token, unauthorized, authorization and authenticat… anywhere ("Unauthenticated", "token_expired"); sign in and log in
# with a leading boundary only, so the letters inside another word ("redesigning", "blogging", "catalog") are not the
# sign-in, while "sign into", "relogin" and "Logging in" are. "Not authorized", "Session expired", "JWT expired" and
# "insufficient scope" are the account's too. When in doubt, the account's side: a trailing boundary as well let
# "Unauthenticated." and "invalid_token" through to the saved list.
ACCOUNT_WORDS = re.compile(r"token|authori[sz]|authenticat|jwt|expired|(?<![a-z])session|(?<![a-z])scope"
                           r"|(?<![a-z])sign(?:ed|ing)?[ _-]?in|(?<![a-z])(?:re)?log(?:ged|ging)?[ _-]?in", re.I)


def transport_kind(exc: StageError) -> str | None:
    """The `Unreachable.kind` of an `Http.request` failure: no connection, a lost or late answer, a page that is not
    JSON or an answer past the cap (unreadable either way, so a list read falls back to its saved copy).
    None for the others (a bad address, a redirect elsewhere), which retrying later cannot fix."""
    if not exc.stage.endswith(".http"):
        return None
    reason = exc.data.get("reason")
    if reason == "answer_lost":
        return "timeout" if exc.data.get("timed_out") else "unreachable"
    if reason in ("not_json", "too_large"):
        return "bad_answer"
    return "unreachable" if reason is None else None


def answer_kind(status: int, data) -> str | None:
    """The `Unreachable.kind` of an answer AniList sent: any 5xx (after `Http`'s retries), and a 403 that does not speak
    of the token. The 403 of the 2026-10-21 outage: `{"errors": [{"message": "The AniList API has been temporarily
    disabled due to severe stability issues.", "status": 403, …}]}`, sent to a signed-in read and to an anonymous one
    alike. Its sentence is not matched: three outages this year, and the next may be worded otherwise."""
    if isinstance(status, int) and status >= 500:
        return "server_error"
    if status == 403:
        errors = data.get("errors") if isinstance(data, dict) else None
        words = " ".join(str(e.get("message") or "") for e in errors or [] if isinstance(e, dict))
        return None if ACCOUNT_WORDS.search(words) else "outage"
    return None

# The walk from an entry to its prequels (resolve's fallback when ani.zip has no AniDB id).
# A public read: no viewer fields. Each related entry's titles too, for `related_shows`.
CHAIN_QUERY = ("query ($id: Int) { Media(id: $id, type: ANIME) { id format status episodes "
               "airingSchedule(perPage: 50) { nodes { episode airingAt } } "
               "relations { edges { relationType node { id type format status episodes "
               "title { romaji english native } synonyms } } } } }")


def relation_nodes(media) -> list[tuple]:
    """`(relationType, node)` for each edge of an answer's `relations`. An empty part (no answer, no relations, a null
    edge or node) is no entry; a part of the wrong type (an edge that is a string, `relations` a list) raises
    `TypeError`, and every caller reads that as a failed read: the play goes on without the answer."""
    def part(value, kind: type, name: str):
        if value and not isinstance(value, kind):
            raise TypeError(f"{name} is not {'an object' if kind is dict else 'a list'}: {shown(value)}")
        return value or None
    rel = part(part(media, dict, "the answer") and media.get("relations"), dict, "relations")
    out = []
    for edge in part(rel and rel.get("edges"), list, "edges") or ():
        node = part(part(edge, dict, "an edge") and edge.get("node"), dict, "a node")
        if node:
            out.append((edge.get("relationType"), node))
    return out


class _Rows:
    """The rows a relations reader gives, bounded: each node's titles by `bounded_titles`, and at most
    RELATION_ROWS_MAX rows of whole nodes (the first ones). `done` logs what was left out, once per answer."""

    def __init__(self):
        self.rows: list[tuple] = []
        self.cut = {"nodes": 0, "titles": 0}

    @staticmethod
    def node_id(node: dict) -> int | None:
        """The node's AniList id; None for a missing one (the node is skipped, as before). One that is not an AniList id
        (`valid_media_id`: a word, `Infinity`, a path) raises `ValueError`, read as a failed read as above."""
        if not node.get("id"):
            return None
        rid = valid_media_id(node["id"])
        if rid is None:
            raise ValueError(f"a related entry's id is not an AniList id: {shown(node['id'])}")
        return rid

    def add(self, node: dict, row) -> None:
        """One row per title of `node`, `row(title)` building it, unless they would pass RELATION_ROWS_MAX."""
        names, synonyms, dropped = bounded_titles(node.get("title"), node.get("synonyms"))
        self.cut["titles"] += dropped
        titles = [x for x in (*names.values(), *synonyms) if x]
        if len(self.rows) + len(titles) > RELATION_ROWS_MAX:
            self.cut["nodes"] += 1
            return
        self.rows += [row(title) for title in titles]

    def done(self, log: Log | None, reader: str, media) -> tuple:
        if log is not None and any(self.cut.values()):
            log.warn("anilist.relations", reader, result="CUT", anilist_id=valid_media_id((media or {}).get("id")),
                     rows=len(self.rows), **{f"{k}_left_out": v for k, v in self.cut.items()},
                     rows_max=RELATION_ROWS_MAX, synonyms_max=SYNONYMS_MAX, title_chars_max=TITLE_CHARS_MAX,
                     reason="titles past an entry's count or length, and entries past the row cap, are left out; "
                            "the rest of the answer is read")
        return tuple(self.rows)


def related_shows(media: dict | None, log: Log | None = None) -> tuple:
    """`(id, title, format)` for every title and synonym of each entry related to this one (any relation
    type) that has more than one episode: `episodes` other than 1, a null count (still airing) included. Read from the
    relations answer (`CHAIN_QUERY`), bounded by `_Rows`. A one-episode entry drops a name that is one of these shows'
    episodes (`score.related_show_reason`)."""
    rows = _Rows()
    for _rel, node in relation_nodes(media):
        if node.get("type") not in (None, "ANIME") or node.get("episodes") == 1:
            continue
        rid = rows.node_id(node)
        if rid is not None:
            rows.add(node, lambda title, rid=rid, node=node: (rid, title, str(node.get("format") or "")))
    return rows.done(log, "related_shows", media)


def prequel_shows(media: dict | None, log: Log | None = None) -> tuple:
    """`(id, title, episodes)` for every title and synonym of each PREQUEL of this entry with one episode,
    from the relations answer (`CHAIN_QUERY`), bounded by `_Rows`: the first half of a split special. A one-episode
    entry keeps a name that numbers on from one of them (`score.prequel_continuation`): a group's `… Final Tide Part 3
    - 02` is the second special after the one-episode `… Final Tide Part 3`. A prequel with more episodes is not
    read: `Show - 13` after a 12-episode `Show` is as often its sequel's first episode, and such a show is one of
    the entry's related shows, whose episodes the entry drops. A one-episode prequel never is one (`related_shows`)."""
    rows = _Rows()
    for rel, node in relation_nodes(media):
        if (rel != "PREQUEL" or node.get("type") not in (None, "ANIME") or type(node.get("episodes")) is not int
                or node["episodes"] != 1):
            continue
        rid = rows.node_id(node)
        if rid is not None:
            rows.add(node, lambda title, rid=rid: (rid, title, 1))
    return rows.done(log, "prequel_shows", media)


# A later-season entry's relations two hops out, each related entry with its titles and episode count.
# Two hops because the first cour of a split season can sit behind a one-episode special (20751's PREQUEL is the
# special 20752, whose PREQUEL is 20595, the first cour). One public read, cached per show
# (`cli.franchise_ttl`); measured 0.3-0.8 s, 156 KB at most.
_FRANCHISE_NODE = "id type format episodes title { romaji english native } synonyms"
FRANCHISE_QUERY = ("query ($id: Int) { Media(id: $id, type: ANIME) { id relations { edges { relationType node { "
                   f"{_FRANCHISE_NODE} relations {{ edges {{ relationType node {{ {_FRANCHISE_NODE} }} }} }} "
                   "} } } } }")


def franchise_shows(media: dict | None, log: Log | None = None) -> tuple:
    """`(id, title, format, episodes)` for every title and synonym of each anime entry one or two
    relation hops from this one (any relation type), the entry itself left out, each entry once. `episodes` is
    AniList's count, None while it airs. Read from `FRANCHISE_QUERY`'s answer, bounded by `_Rows`; a later-season entry
    drops a single that names one of these shows and no more (`score.franchise_show_reason`)."""
    rows = _Rows()
    seen = {valid_media_id(media.get("id")) if isinstance(media, dict) else None}

    def add(node):
        if node.get("type") not in (None, "ANIME"):
            return
        rid = rows.node_id(node)
        if rid is None or rid in seen:
            return
        seen.add(rid)
        eps = node.get("episodes")
        eps = eps if isinstance(eps, int) and not isinstance(eps, bool) and eps > 0 else None
        rows.add(node, lambda title: (rid, title, str(node.get("format") or ""), eps))
    hop1 = [node for _rel, node in relation_nodes(media)]
    for node in hop1:
        add(node)
    for node in hop1:
        if node.get("type") in (None, "ANIME"):
            for _rel, node2 in relation_nodes(node):
                add(node2)
    return rows.done(log, "franchise_shows", media)


def is_mutation(query: str) -> bool:
    """A GraphQL document whose operation is a mutation. Leading comments (`# …`) are skipped."""
    lines = [l for l in query.strip().splitlines() if not l.lstrip().startswith("#")]
    return "\n".join(lines).lstrip().startswith("mutation")


PENDING_FILE = "pending-progress.json"


def pending_path() -> Path:
    return CACHE_DIR / PENDING_FILE


def _pending_entries(raw) -> list[dict]:
    return [q for q in raw if isinstance(q, dict)] if isinstance(raw, list) else []


def drop_unowned_pending(log: Log) -> int:
    """At a sign-out or a sign-in: drop the queued writes that name no account (an earlier build queued them), whose
    account can no longer be told; the stamped ones stay, each sent only to its own account. Returns the count."""
    path = pending_path()
    queue = _pending_entries(cached_json(path, None))
    keep = [q for q in queue if q.get("user") is not None]
    if len(keep) == len(queue):
        return 0
    write_cache(path, keep)
    log.warn("anilist.progress", "dropped", result="PASS", dropped=len(queue) - len(keep), kept=len(keep),
             mediaIds=[q.get("mediaId") for q in queue if q.get("user") is None],
             reason="queued with no account by an earlier build; the account changed, so it is not sent to the next "
                    "one")
    return len(queue) - len(keep)


class AniList:
    TOKEN_PATH = CONFIG_DIR / "anilist-token"

    def __init__(self, http: Http, log: Log, token: str | None = None):
        self.http, self.log = http, log
        self.token = token
        self.limit: int = RATE_LIMIT
        self.remaining: int | None = None
        self.reset_at: float | None = None
        self.rate_warning: str = ""
        # AniList rejected this client's token once; the client's later public reads skip it.
        # Per client, not per process: a command that wants one rejected request builds one client.
        self.token_was_rejected = False
        # Whether the last `query` was answered without the token (signed out, or retried).
        self.answered_anonymously = False
        # The last `list_entries` answered with its saved copy: {savedAt, ageSeconds, reason}, or None.
        self.list_stale: dict | None = None
        # The same for the last `top100` and `airing_from_top`.
        self.top_stale: dict | None = None
        self.airing_stale: dict | None = None
        self._adopt_rate_state()

    @property
    def user_id(self) -> int | None:
        """AniList user id from the JWT `sub` claim (verified: sub == Viewer.id). Saves a request per command."""
        if not self.token:
            return None
        try:
            return int(jwt_claims(self.token).get("sub") or 0) or None
        except (TypeError, ValueError):
            return None

    @classmethod
    def load_token(cls) -> str | None:
        return read_token("anilist") if cls.TOKEN_PATH.exists() else None

    # -- transport

    # -- rate limit: three steps, not one cliff

    def _adopt_rate_state(self) -> None:
        """Every CLI command is its own process, so the only memory of the last minute's budget is
        a file. Adopting it means a second command starts paced instead of discovering the limit."""
        # Last minute's budget says nothing about this minute, and a record out of AniList's range is not its
        # budget (`rate_record`). Only the second is logged: an expired record is every first command of a minute.
        saved = cached_json(CACHE_DIR / RATE_FILE, None)
        state = rate_record(saved)
        if state is None:
            if saved is not None and not rate_expired(saved):
                fields = saved if isinstance(saved, dict) else {}
                self.log.warn("anilist", "rate_record", result="IGNORED", record=shown(saved) if not fields else None,
                              **{k: shown(fields.get(k)) for k in ("remaining", "limit", "reset") if fields},
                              limit_max=RATE_LIMIT_MAX, ahead_max_s=RATE_AHEAD_MAX_S,
                              suggest="the saved AniList budget is out of the range AniList documents; it is not "
                                      "used, and the next answer rewrites it")
            return
        self.remaining, self.limit, self.reset_at = state["remaining"], state["limit"], state["reset"]

    def _read_rate_headers(self, stage: str, lower: dict) -> None:
        """An answer's `x-ratelimit-*` headers, each read on its own: `remaining` an int, clamped to the
        limit; `limit` an int in 1..RATE_LIMIT_MAX, else the one known; `reset` a finite time at most
        RATE_AHEAD_MAX_S ahead, else a minute from now, as when it is absent. A bad `remaining` leaves the budget
        as it was. Each bad value is named on one `anilist rate_header` line."""
        now = time.time()
        bad = {}
        remaining = header_int(lower["x-ratelimit-remaining"], 0, 10**12)
        if remaining is None:
            bad["x-ratelimit-remaining"] = lower["x-ratelimit-remaining"]
        raw_limit = lower.get("x-ratelimit-limit")
        limit = header_int(raw_limit, 1, RATE_LIMIT_MAX) if raw_limit else None
        if raw_limit and limit is None:
            bad["x-ratelimit-limit"] = raw_limit
        raw_reset = lower.get("x-ratelimit-reset")
        try:
            reset = float(raw_reset or 0)
        except (TypeError, ValueError):
            reset = math.nan
        if not (math.isfinite(reset) and reset <= now + RATE_AHEAD_MAX_S):
            bad["x-ratelimit-reset"], reset = raw_reset, 0
        if bad:
            self._ignored_rate_header(stage, bad)
        if remaining is None:
            return
        self.limit = limit or self.limit or RATE_LIMIT
        self.remaining = min(remaining, self.limit)
        self.reset_at = reset or (now + 60)
        self._record_rate_state()

    def _ignored_rate_header(self, stage: str, bad: dict) -> None:
        self.log.warn("anilist", "rate_header", result="IGNORED", call=stage,
                      headers={k: shown(v) for k, v in bad.items()}, limit_max=RATE_LIMIT_MAX,
                      ahead_max_s=RATE_AHEAD_MAX_S,
                      suggest="AniList sent a rate-limit header outside the range it documents; the value is "
                              "ignored and the budget paced as if it were absent")

    def _record_rate_state(self) -> None:
        if self.remaining is None:
            return
        write_cache(CACHE_DIR / RATE_FILE, {"remaining": self.remaining, "limit": self.limit,
                                            "reset": self.reset_at, "ts": time.time()})

    def _pace(self, stage: str) -> None:
        """Throttle at 80 % of the budget, warn at 90 %, refuse at 100 % with the reset."""
        if self.remaining is None:
            return
        limit = self.limit or RATE_LIMIT
        reset_in = max(0.0, (self.reset_at or 0) - time.time())
        if self.remaining <= 0:
            if reset_in <= 0:
                # The window has rolled over; a stale zero must not refuse forever.
                self.remaining, self.reset_at = None, None
                return
            raise Unreachable("rate_limited", f"anilist.{stage}",
                              f"AniList rate limit spent ({limit}/min); it resets in {int(reset_in) + 1}s",
                              "wait for the reset — the plugin will retry by itself", retry_in=int(reset_in) + 1)
        spacing = 60.0 / limit + random.uniform(0.05, 0.25)
        if self.remaining <= max(1, round(limit * 0.1)):                 # 90 % spent
            self.rate_warning = (f"AniList is nearly rate-limited: {self.remaining} of {limit} requests left, "
                                 f"resets in {int(reset_in) + 1}s")
            self.log.warn("anilist", "throttle", ms=spacing * 1000, remaining=self.remaining, limit=limit,
                          reset_in=round(reset_in, 1), step="90%", suggest=self.rate_warning)
            time.sleep(spacing)
        elif self.remaining <= max(2, round(limit * 0.2)):               # 80 % spent
            self.rate_warning = ""
            self.log.info("anilist", "throttle", ms=spacing * 1000, remaining=self.remaining, limit=limit,
                          reset_in=round(reset_in, 1), step="80%")
            time.sleep(spacing)
        else:
            self.rate_warning = ""

    def query(self, stage: str, query: str, variables: dict | None = None, retries: int = 3,
              anonymous_ok: bool = False) -> dict:
        """One GraphQL request. `anonymous_ok` marks a public read: one that needs no
        account and asks for none of the viewer's fields, with one named exception: Top 100 asks for
        `mediaListEntry`, which an anonymous answer carries as `null`, exactly as when signed out (the
        cache it writes is dropped at sign-in, `cli.derived_caches`). If AniList rejects the token, a
        public read is sent once more without the header, and this client's later public reads skip
        the header (Airing alone is a dozen requests against a 30/min budget). Every opt-in lives in
        this class; nothing outside it passes `anonymous_ok`."""
        send_token = bool(self.token) and not (anonymous_ok and self.token_was_rejected)
        if self.token and not send_token:
            self.log.debug(f"anilist.{stage}", "anonymous", result="SKIP",
                           why="this client's token was rejected earlier; a public read goes without it")
        try:
            data = self._post(stage, query, variables, retries, send_token)
            self.answered_anonymously = not send_token
            return data
        except StageError as exc:
            if not (anonymous_ok and send_token and exc.data.get("reason") in REJECTED_TOKEN_REASONS):
                raise
            self.token_was_rejected = True
            self.log.warn(f"anilist.{stage}", "retry_anonymous", result="RETRY", reason=exc.data["reason"],
                          suggest="AniList rejected the token; this read needs none, so it is sent once without "
                                  "it. run: omarchy-anime auth anilist")
        data = self._post(stage, query, variables, retries, send_token=False)
        self.answered_anonymously = True
        return data

    def _post(self, stage: str, query: str, variables: dict | None, retries: int, send_token: bool) -> dict:
        self._pace(stage)
        headers = {"Content-Type": "application/json"}
        if send_token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        # A GraphQL read is safe to send again after a lost answer, though it travels as a POST; a
        # mutation (a progress save) is not.
        cap = {"max_bytes": LARGE_MAX_BYTES} if stage in LIST_STAGES else {}     # else `Http`'s default cap
        try:
            status, data, hdrs = self.http.request(f"anilist.{stage}", "POST", GRAPHQL_URL, headers=headers, data=body,
                                                   retries=retries, idempotent=not is_mutation(query), **cap)
        except StageError as exc:
            kind = transport_kind(exc)
            if kind is None:
                raise
            raise Unreachable(kind, exc.stage, str(exc), exc.suggest, **exc.data) from exc
        lower = {k.lower(): v for k, v in (hdrs or {}).items()}
        if "x-ratelimit-remaining" in lower:
            self._read_rate_headers(stage, lower)
        if status == 401 or (isinstance(data, dict) and any(e.get("status") == 401 for e in data.get("errors") or [])):
            # A rejected token only when one was sent on this request (the same guard as the 400 answer below).
            raise StageError(f"anilist.{stage}", "AniList: unauthorized", "run: omarchy-anime auth anilist",
                             **({"reason": self._refusal_reason()} if send_token else {}))
        # AniList does not answer a bad token with 401. Probed live 2026-09-27 with a made-up
        # bearer and with a JWT whose `exp` had passed: HTTP 400, `errors: [{"message": "Invalid token",
        # "status": 400}]`. The message is the only thing that sets it apart from any other 400, so it
        # is matched here and nowhere else, and only when a token was sent. `reason` is what the shell
        # reads; the message stays the generic one below.
        if send_token and isinstance(data, dict) and any(
                str(e.get("message") or "") == "Invalid token" for e in data.get("errors") or []):
            raise StageError(f"anilist.{stage}", "AniList error: Invalid token", "run: omarchy-anime auth anilist",
                             reason=self._refusal_reason())
        if status == 429:
            raw = lower.get("retry-after")
            retry_after = header_int(raw, 0, RATE_AHEAD_MAX_S)
            if raw is not None and retry_after is None:
                self._ignored_rate_header(stage, {"retry-after": raw})
            raise Unreachable("rate_limited", f"anilist.{stage}", "AniList rate limit (30/min) exceeded",
                              "wait a minute", retry_after=retry_after)
        # A 5xx or the outage's 403 is the same failure as before, typed so a list read can answer with its saved copy.
        kind = answer_kind(status, data)

        def failure(message: str, **fields) -> StageError:
            return Unreachable(kind, f"anilist.{stage}", message, **fields) if kind else \
                StageError(f"anilist.{stage}", message, **fields)
        if not isinstance(data, dict):
            raise failure(f"AniList returned {status}: {str(data)[:200]}")
        if data.get("errors"):
            msg = "; ".join(str(e.get("message")) for e in data["errors"])
            raise failure(f"AniList error: {msg}", errors=data["errors"][:3])
        if kind:
            # A 5xx whose body is JSON without `errors` used to be read as an empty answer, and saved over the list.
            raise failure(f"AniList returned {status}")
        return data.get("data") or {}

    def _refusal_reason(self) -> str:
        """Why AniList refused the token, from the token's own `exp` claim (no request)."""
        return "token_expired" if token_expiry(self.token)["expired"] else "token_rejected"

    # -- auth

    # A staticmethod so the sign-in URL can be built without a client (and therefore without a
    # token): `accounts` needs it for a provider the user is not signed in to yet.
    @staticmethod
    def pin_url(client_id: int) -> str:
        return f"https://anilist.co/api/v2/oauth/authorize?client_id={client_id}&response_type=token"

    def viewer(self) -> dict:
        return self.query("viewer", "query { Viewer { id name } }").get("Viewer") or {}

    def save_token(self, token: str) -> dict:
        self.token = token.strip()
        who = self.viewer()
        if not who.get("id"):
            raise StageError("anilist.auth", "token did not resolve to a user")
        # `core.write_token` owns the mode ordering and creates CONFIG_DIR at 0700 (it used to take
        # the umask default, which is how the log directory ended up world-readable).
        write_token("anilist", self.token)
        exp = jwt_claims(self.token).get("exp")
        self.log.info("anilist.auth", "saved", result="PASS", user=who.get("name"), user_id=who.get("id"), exp=exp)
        return {"user": who, "exp": exp}

    # -- reads

    def list_entries(self, status: str = "CURRENT", user_name: str | None = None, user_id: int | None = None,
                     max_age: float = 300) -> list[dict]:
        """One of the user's six AniList lists. `watching()` is this with `CURRENT`.

        What AniList answers, measured 2026-09-16 with one read-only query per status: a status with entries
        comes back as its list (`"Planning"`), COMPLETED as one list per format (`"Completed TV"`, `"Completed
        Movie"`, `"Completed Special"`), and a status with none as `lists: []`. Each query took about 0.3 s.

        Two things that decide this code. **COMPLETED comes back split by format**, all three lists
        with `status == "COMPLETED"` and `isCustomList == false`, so the surviving lists are
        concatenated rather than indexed. And **an empty list is an absent list**, not an empty
        `entries` array, so nothing may reach into `lists[0]`.
        """
        want = str(status or "CURRENT").upper()
        if want not in LIST_STATUSES:
            raise StageError("anilist.list", f"unknown list status {status!r}",
                             f"one of {', '.join(LIST_STATUSES)}")
        key = user_name or (f"id{user_id}" if user_id else "me")
        # `watching-<key>.json` keeps its name: Service.qml, Panel.qml and docs/terminal.md all read it.
        path = CACHE_DIR / "anilist" / (f"watching-{key}.json" if want == "CURRENT" else f"list-{want}-{key}.json")
        self.list_stale = None
        cached = cached_json(path, max_age)
        if cached is not None:
            self.log.debug("anilist.list", "cache", result="HIT", key=key, status=want)
            return cached
        if not user_name and not user_id:
            raise StageError("anilist.list", "no user: pass --user or authenticate", "omarchy-anime auth anilist")
        q = f"""query ($userName: String, $userId: Int, $status: MediaListStatus) {{
          MediaListCollection(userName: $userName, userId: $userId, type: ANIME, status: $status) {{
            user {{ id name }}
            lists {{ name status isCustomList entries {{ id progress score status updatedAt media {{ {MEDIA_FIELDS} }} }} }}
          }} }}"""
        # AniList answers 500 when userName is present as null next to userId; send only the set one.
        variables = {k: v for k, v in {"userName": user_name, "userId": user_id}.items() if v is not None}
        variables["status"] = want
        try:
            data = self.query("list" if want != "CURRENT" else "watching", q, variables)
        except Unreachable as exc:
            return self._saved_list(path, want, key, exc)
        coll = data.get("MediaListCollection") or {}
        entries: list[dict] = []
        names = []
        for lst in coll.get("lists") or []:
            if lst.get("status") != want or lst.get("isCustomList"):
                continue
            names.append(lst.get("name"))
            for e in lst.get("entries") or []:
                m = e.get("media") or {}
                remember_titles(m, self.log)
                entries.append({"listId": e.get("id"), "progress": e.get("progress") or 0,
                                "score": e.get("score"), "status": e.get("status") or want,
                                "updatedAt": e.get("updatedAt"), "media": m,
                                "next": next_episode(m, e.get("progress") or 0)})
        entries.sort(key=lambda e: e.get("updatedAt") or 0, reverse=True)
        write_cache(path, entries)
        saved_copy(path).unlink(missing_ok=True)        # superseded by the list just read
        self.log.info("anilist.list", "fetch", result="PASS", status=want, lists=names,
                      user=(coll.get("user") or {}).get("name"), count=len(entries))
        return entries

    def _saved_list(self, path: Path, want: str, key: str, exc: Unreachable) -> list[dict]:
        """AniList cannot be read (`exc`): the list saved by the last good read of this status and key, whatever its
        age, with `list_stale` set; the fresh file first, then the copy a progress write left (`saved_copy`). No saved
        list: `exc` itself, today's failure."""
        entries, self.list_stale = self._saved_rows("anilist.list", (path, saved_copy(path)), exc,
                                                    "list of this status", status=want, key=key)
        return entries

    def _saved_rows(self, component: str, paths: tuple[Path, ...], exc: Unreachable, what: str,
                    **fields) -> tuple[list, dict]:
        """AniList cannot be read (`exc`): the rows the last good read saved, from the first of `paths` that holds a
        list, whatever its age, and their stale marker `{savedAt, ageSeconds, reason}` (the file's mtime, its age,
        `exc.kind`). One `fallback` line (STALE) names the path, age, reason, count and AniList's error. Nothing saved:
        `exc` itself, today's failure, after a MISS line. `what` names the answer in the lines' suggest."""
        for src in paths:
            entries = cached_json(src, None)
            try:
                saved_at = src.stat().st_mtime
            except OSError:
                continue
            if isinstance(entries, list):
                break
        else:
            self.log.warn(component, "fallback", result="MISS", **fields, path=str(paths[0]), reason=exc.kind,
                          error=str(exc), suggest=f"AniList cannot be read and no {what} was saved: the failure stands")
            raise exc
        age = max(0, int(time.time() - saved_at))
        self.log.warn(component, "fallback", result="STALE", **fields, path=str(src), age_s=age, reason=exc.kind,
                      count=len(entries), error=str(exc),
                      suggest=f"AniList cannot be read: these rows are the saved {what}, marked stale, until a read "
                              "succeeds")
        return entries, {"savedAt": int(saved_at), "ageSeconds": age, "reason": exc.kind}

    def watching(self, user_name: str | None = None, user_id: int | None = None, max_age: float = 300) -> list[dict]:
        return self.list_entries("CURRENT", user_name=user_name, user_id=user_id, max_age=max_age)

    def _invalidate_lists(self) -> None:
        """Every cached user list, after a write that can move an entry between them.

        A progress write can complete a show (CURRENT -> COMPLETED) or start one (PLANNING ->
        CURRENT), so clearing only `watching-*.json` would leave a stale Planning or Completed list
        behind in the very session that changed it. `top100.json` and the airing cache are not user
        lists and are left alone.

        Each list is renamed to its fallback-only copy (`saved_copy`), not deleted: a fresh read never serves it, and
        an outage that starts before the status is read again answers with it. The rename keeps the mtime,
        which is when the list was read.

        One file at a time: a file another process renamed or removed after the glob (a second write, a sign-out) is
        logged and skipped, and the rest are still renamed. A list left under its fresh name would be served for up to
        five minutes with the episode before the write.
        """
        try:
            found = [p for pattern in ("watching-*.json", "list-*.json") for p in (CACHE_DIR / "anilist").glob(pattern)]
        except OSError as exc:
            self.log.warn("anilist.list", "invalidate", result="FAIL", error=f"{type(exc).__name__}: {exc}",
                          suggest="the saved lists could not be listed; a fresh read may serve one from before the "
                                  "write for up to five minutes")
            return
        for p in found:
            to = saved_copy(p)
            try:
                p.replace(to)
            except OSError as exc:
                self.log.warn("anilist.list", "invalidate", result="FAIL", **{"from": str(p)}, to=str(to),
                              error=f"{type(exc).__name__}: {exc}",
                              suggest="this list was not renamed; if it is still there, a fresh read serves it for up "
                                      "to five minutes with the episode before the write")
                continue
            self.log.info("anilist.list", "invalidate", result="PASS", **{"from": str(p)}, to=str(to))

    def top100(self, max_age: float = TOP100_MAX_AGE) -> list[dict]:
        """AniList's top 100 by score, saved for a day. When AniList cannot be read (`Unreachable`, never an account
        failure), the saved Top 100 whatever its age, with `top_stale` set; `max_age=0` (`--refresh`) too: in the
        2026-10-21 outage two of the four failed opens were the viewer's `r` after the first."""
        self.top_stale = None
        try:
            return self._top100(max_age)
        except Unreachable as exc:
            top, self.top_stale = self._saved_rows("anilist.top", (top100_path(),), exc, "Top 100")
            return top

    def _top100(self, max_age: float = TOP100_MAX_AGE) -> list[dict]:
        """`top100` with no fallback: the saved file while it is under `max_age`, else AniList's answer, saved."""
        path = top100_path()
        cached = cached_json(path, max_age)
        if cached is not None:
            self.log.debug("anilist.top", "cache", result="HIT")
            return cached
        # `mediaListEntry` is the viewer's own entry; AniList scopes it to the bearer token
        # (verified live 2026-09-15 on a Page query). The Browse overlay needs it to hide shows
        # the user has already completed, which is why the cache file is
        # versioned: a pre-0.3.0 top100.json has no such field.
        page = (f"media(type: ANIME, sort: SCORE_DESC, isAdult: false) "
                f"{{ {MEDIA_FIELDS} mediaListEntry {{ id status progress }} }}")
        q = f"query {{ p1: Page(page: 1, perPage: 50) {{ {page} }} p2: Page(page: 2, perPage: 50) {{ {page} }} }}"
        data = self.query("top", q, anonymous_ok=True)
        items = (data.get("p1") or {}).get("media") or []
        items += (data.get("p2") or {}).get("media") or []
        out = [dict(m, rank=i + 1) for i, m in enumerate(items)]
        for m in out:
            remember_titles(m, self.log)
        write_cache(path, out)
        self.log.info("anilist.top", "fetch", result="PASS", count=len(out), first=(out[0]["title"]["romaji"] if out else None),
                      anonymous=self.answered_anonymously)
        return out

    def relations_batch(self, ids: list[int]) -> dict[int, list[dict]]:
        """Relations for up to 25 ids in one request via aliases."""
        out: dict[int, list[dict]] = {}
        for i in range(0, len(ids), 25):
            chunk = ids[i:i + 25]
            parts = " ".join(f"m{mid}: Media(id: {mid}) {{ id {RELATION_FIELDS} }}" for mid in chunk)
            data = self.query("relations", f"query {{ {parts} }}", anonymous_ok=True)
            for mid in chunk:
                node = data.get(f"m{mid}") or {}
                out[mid] = ((node.get("relations") or {}).get("edges")) or []
        return out

    def airing_from_top(self, max_age: float = 86400) -> list[dict]:
        """For each top-100 entry walk the sequel chain; report the airing season if any. When AniList cannot be read
        (`Unreachable`), the saved Airing list whatever its age, with `airing_stale` set, as `top100` does. A new
        Airing list is built from a Top 100 AniList answered or saved within a day, never from a stale one."""
        self.airing_stale = None
        path = CACHE_DIR / "anilist" / "airing-from-top.json"
        try:
            return self._airing_from_top(path, max_age)
        except Unreachable as exc:
            saved, self.airing_stale = self._saved_rows("anilist.airing", (path,), exc, "Airing list")
            # Merged as a cache hit is, and not rewritten: a write would make the saved copy look fresh.
            return merge_airing(saved)[0]

    def _airing_from_top(self, path: Path, max_age: float) -> list[dict]:
        """`airing_from_top` with no fallback."""
        cached = cached_json(path, max_age)
        if cached is not None:
            # A file written before the merge existed keeps its repeats for up to a day. It is not rewritten
            # here: a write resets the mtime `cached_json` ages it by, so a stale file would live another day.
            rows, shows, merged = merge_airing(cached)
            self.log.debug("anilist.airing", "cache", result="HIT", merged=merged,
                           merged_ids=[m["id"] for m in shows], merged_shows=shows)
            return rows
        top = self._top100()
        relations: dict[int, list[dict]] = {}
        frontier = [m["id"] for m in top]
        depth = 0
        while frontier and depth < 8:
            fetched = self.relations_batch([mid for mid in frontier if mid not in relations])
            relations.update(fetched)
            nxt = []
            for mid in frontier:
                for edge in relations.get(mid, []):
                    node = edge.get("node") or {}
                    if edge.get("relationType") == "SEQUEL" and node.get("type") == "ANIME" and node.get("id") not in relations:
                        nxt.append(node["id"])
            frontier = list(dict.fromkeys(nxt))
            depth += 1
        out = []
        for m in top:
            chain = walk_sequels(m["id"], lambda mid: relations.get(mid, []))
            node = m if m.get("status") == "RELEASING" else airing_node(chain)
            if node:
                out.append({"top": {k: m.get(k) for k in ("id", "rank", "title", "coverImage", "averageScore", "status")},
                            "airing": node, "chain": [{"id": n["id"], "title": n.get("title"), "status": n.get("status")} for n in chain]})
        out, shows, merged = merge_airing(out)
        write_cache(path, out)
        self.log.info("anilist.airing", "fetch", result="PASS", top=len(top), airing=len(out), merged=merged,
                      merged_ids=[m["id"] for m in shows], merged_shows=shows, relation_requests=depth,
                      titles=[(o["airing"].get("title") or {}).get("romaji") for o in out])
        return out

    def search(self, text: str, per_page: int = 10) -> list[dict]:
        q = f"""query ($q: String, $n: Int) {{ Page(page: 1, perPage: $n) {{
          media(search: $q, type: ANIME, isAdult: false, sort: SEARCH_MATCH) {{ {MEDIA_FIELDS} }} }} }}"""
        data = self.query("search", q, {"q": text, "n": per_page}, anonymous_ok=True)
        items = (data.get("Page") or {}).get("media") or []
        for m in items:
            remember_titles(m, self.log)
        self.log.info("anilist.search", "fetch", result="PASS", q=text, count=len(items),
                      anonymous=self.answered_anonymously)
        return items

    def media_titles(self, media_id: int, retries: int = 2) -> dict:
        """The titles of one show, for a play that starts before any list command
        remembered them. The list commands' own fields and no viewer fields, so a rejected token is
        retried without it; remembered exactly as the list commands remember theirs."""
        q = f"query ($id: Int) {{ Media(id: $id, type: ANIME) {{ {MEDIA_FIELDS} }} }}"
        m = self.query("titles", q, {"id": media_id}, retries=retries, anonymous_ok=True).get("Media") or {}
        remember_titles(m, self.log)
        return m

    def sequel_chain(self, media_id: int) -> dict:
        """One entry's format, airing schedule and relations, for resolve's prequel walk. A `Media` that is not an
        object reads as `Media: null` (no entry), with one WARN: every reader of the chain (the episode count, the
        related shows, the walk) then goes on as without an answer."""
        found = self.query("relations.chain", CHAIN_QUERY, {"id": media_id}, anonymous_ok=True).get("Media") or {}
        if not isinstance(found, dict):
            self.log.warn("anilist.relations.chain", "media", result="IGNORED", anilist_id=media_id,
                          media=shown(found), suggest="AniList answered the relations of this entry with something "
                                                      "that is not an entry; the play goes on without them")
            return {}
        return found

    def franchise(self, media_id: int) -> dict:
        """One entry's relations two hops out (`FRANCHISE_QUERY`, a public read), for `franchise_shows`.
        One attempt: the read is enrichment, and its absence is today's filter."""
        return self.query("relations.franchise", FRANCHISE_QUERY, {"id": media_id}, retries=1,
                          anonymous_ok=True).get("Media") or {}

    def media(self, media_id: int) -> dict:
        q = f"query ($id: Int) {{ Media(id: $id, type: ANIME) {{ {MEDIA_FIELDS} {MEDIA_DETAIL_FIELDS} {RELATION_FIELDS} mediaListEntry {{ id status progress }} }} }}"
        m = self.query("media", q, {"id": media_id}).get("Media") or {}
        remember_titles(m, self.log)
        return m

    # -- writes

    def _my_entry(self, stage: str, media_id: int) -> dict:
        """The token user's own list entry ({} when the show is not on the list). Without
        `userId` this query returns an arbitrary user's entry.

        A show that is not on the list is not `MediaList: null`: AniList answers with a GraphQL error
        `{"message": "Not Found.", "status": 404}` (measured 2026-09-25). That is an answer, not a
        failure, so it is {} here; any other error still raises."""
        if self.user_id is None:
            # The query without a userId is another account's entry — not something to decide on.
            raise StageError(f"anilist.{stage}", "no AniList user id in the token", "omarchy-anime auth anilist")
        try:
            return self.query(stage, "query ($id: Int, $user: Int) { MediaList(mediaId: $id, userId: $user, type: ANIME) "
                                     "{ id progress status repeat } }",
                              {"id": media_id, "user": self.user_id}).get("MediaList") or {}
        except StageError as exc:
            errors = (getattr(exc, "data", None) or {}).get("errors") or []
            if errors and all(e.get("status") == 404 for e in errors):
                return {}
            raise

    def save_progress(self, media_id: int, progress: int, complete: bool = False,
                      dry_run: bool = False, allow_lower: bool = False) -> dict:
        """SaveMediaListEntry then read back. Failures are queued for retry.

        `dry_run` returns exactly what would be sent and makes no request — the only way to exercise
        a write path (the Lists tab's "Start", for instance) without touching the user's real list.

        The list is read first, and a write that would lower `progress`, repeat it, or take a
        COMPLETED entry back to CURRENT is skipped — a rewatch never touches AniList. `allow_lower`
        is for the explicit `omarchy-anime progress` command only.

        A rewatch the viewer started on AniList is a REPEATING entry ("Re-watching", AniList's
        MediaListStatus reference), and it stays REPEATING while it advances; the last episode sends
        COMPLETED and no `repeat` (the rewatch count): AniList raises it itself on REPEATING -> COMPLETED.
        An AniList data moderator said so in anilist.co forum thread 53764 (2022-04-19), and users report
        it in threads 64572 (2023) and 86439 (2025); not live-tested. Sending N+1 would count twice. The
        status comes from the list read, so a queued write that `flush_pending` replays keeps it too.
        `dry_run` reads no list and says CURRENT.
        """
        # What this call sent and whether it queued, for the caller's outcome line,
        # which runs in a `finally` and so also after a read-back that raises.
        self.last_write, self.last_queued = None, False
        if not self.token:
            raise StageError("anilist.progress", "not authenticated", "omarchy-anime auth anilist")
        status = "COMPLETED" if complete else "CURRENT"
        if dry_run:
            out = {"dryRun": True, "would": {"mediaId": media_id, "status": status, "progress": progress}}
            self.log.info("anilist.progress", "dry-run", result="PASS", **out["would"])
            return out
        if not allow_lower:
            try:
                current = self._my_entry("progress.current", media_id)
            except StageError as exc:
                # A guard that cannot see the list does not guess. The write waits in the queue,
                # and flush_pending runs it back through this same check.
                self._queue_pending(media_id, progress, status, f"list unreadable: {exc}")
                raise
            have = int(current.get("progress") or 0)
            if current.get("status") == "REPEATING" and not complete:
                status = "REPEATING"
            skip = ""
            if current.get("status") == "COMPLETED" and not complete:
                skip = "would un-complete"
            elif current and (have > progress or (have == progress and current.get("status") == status)):
                skip = "would lower" if have > progress else "no change"
            self.log.info("anilist.progress", "guard", result="PASS", mediaId=media_id, progress=progress,
                          status=status, current=current, decision="skip" if skip else "write", reason=skip or "moves forward")
            if skip:
                # "would un-complete" and "would lower" are the same thing to every caller.
                return {"saved": None, "verified": True, "skipped": "would lower" if skip != "no change" else skip,
                        "current": current}
        q = """mutation ($mediaId: Int, $status: MediaListStatus, $progress: Int) {
          SaveMediaListEntry(mediaId: $mediaId, status: $status, progress: $progress) { id mediaId status progress updatedAt } }"""
        try:
            self.last_write = {"status": status, "progress": progress, "answered": False}
            data = self.query("progress", q, {"mediaId": media_id, "status": status, "progress": progress})
            self.last_write["answered"] = True
        except StageError as exc:
            self._queue_pending(media_id, progress, status, str(exc))
            raise
        saved = data.get("SaveMediaListEntry") or {}
        # Read back. MediaList without userId returns an arbitrary user's entry (observed: progress 500 for
        # another account), so the query is scoped to the token's user.
        check = self.query("progress.verify",
                           "query ($id: Int, $user: Int) { MediaList(mediaId: $id, userId: $user, type: ANIME) "
                           "{ id progress status repeat } }",
                           {"id": media_id, "user": self.user_id}).get("MediaList") or {}
        ok = check.get("progress") == progress and check.get("status") == status
        self.log.info("anilist.progress", "verify", result="PASS" if ok else "FAIL", mediaId=media_id, progress=progress,
                      status=status, read_back=check, suggest="" if ok else "AniList did not persist the write; queued for retry")
        if not ok:
            self._queue_pending(media_id, progress, status, "read-back mismatch")
        self._invalidate_lists()
        return {"saved": saved, "verified": ok, "status": status}

    def _queue_pending(self, media_id: int, progress: int, status: str, reason: str) -> None:
        """Queue a write AniList did not take, stamped with the account it was made under (`user`, the token's `sub`):
        `flush_pending` sends it to that account only, so a sign-out and another account's sign-in never put one
        viewer's progress on another's public list. One entry per account and show."""
        path = pending_path()
        user = self.user_id
        queue = [q for q in _pending_entries(cached_json(path, None))
                 if (q.get("user"), q.get("mediaId")) != (user, media_id)]
        queue.append({"mediaId": media_id, "progress": progress, "status": status, "reason": reason, "ts": time.time(),
                      "user": user})
        write_cache(path, queue)
        self.last_queued = True
        self.log.warn("anilist.progress", "queued", result="FAIL", mediaId=media_id, progress=progress, reason=reason,
                      user=user)

    def flush_pending(self) -> list[dict]:
        """Send the queued writes of this token's account, and those with no account (an earlier build's, which no
        sign-out or sign-in has dropped since: `drop_unowned_pending`). Another account's entries stay queued for it."""
        path = pending_path()
        queue = _pending_entries(cached_json(path, None))
        if not queue or not self.token:
            return []
        user = self.user_id
        mine = [q for q in queue if q.get("user") in (None, user)]
        if len(mine) < len(queue):
            self.log.info("anilist.progress", "flush", result="SKIP", user=user, other_accounts=len(queue) - len(mine),
                          reason="queued under another AniList account; sent only to that account")
        done = []
        for item in mine:
            try:
                self.save_progress(item["mediaId"], item["progress"], item["status"] == "COMPLETED")
                done.append(item)
                queue.remove(item)
            except StageError:
                continue
        write_cache(path, queue)
        return done
