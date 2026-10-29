"""api.ani.zip: AniList id -> cross-site ids and per-episode AniDB episode ids."""
from __future__ import annotations

import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .core import Http, Log, StageError, CACHE_DIR, ANIZIP_URL, cached_json, write_cache
from .anilist import shown, valid_media_id

# ----------------------------------------------------------------------------- ani.zip mapping

def anizip_mappings(http: Http, log: Log, anilist_id: int) -> dict:
    """AniList id -> {mappings, episodes, titles, episodeCount}. Verified 2026-09-14.

    The id names the cache file and the request, so one that is not an AniList id (`valid_media_id`: a prequel's
    `"1#/../x"` from a relations answer fetched show 1 and wrote it outside the cache) is refused before either."""
    if valid_media_id(anilist_id) is None:
        log.warn("map.anizip", "id", result="FAIL", anilist_id=shown(anilist_id),
                 reason="not an AniList id: it would name the cache file and the request; nothing is asked or written")
        raise StageError("map.anizip", f"not an AniList id: {shown(anilist_id)}", "check the AniList id")
    path = CACHE_DIR / "mappings" / f"{anilist_id}.json"
    cached = cached_json(path, 15 * 60)
    if cached:
        log.debug("map.anizip", "cache", result="HIT", anilist_id=anilist_id)
        return cached
    t0 = time.monotonic()
    status, body, _ = http.request("map.anizip", "GET", f"{ANIZIP_URL}?anilist_id={anilist_id}")
    if status != 200 or not isinstance(body, dict) or "mappings" not in body:
        raise StageError("map.anizip", f"api.ani.zip returned {status} for anilist_id={anilist_id}",
                         "try relations.yuna.moe or check the AniList id", body=str(body)[:200])
    write_cache(path, body)
    m = body["mappings"]
    log.info("map.anizip", "resolve", result="PASS", ms=(time.monotonic() - t0) * 1000, anilist_id=anilist_id,
             anidb_id=m.get("anidb_id"), kitsu_id=m.get("kitsu_id"), mal_id=m.get("mal_id"),
             episodes=body.get("episodeCount"), title=(body.get("titles") or {}).get("en"))
    return body


def movie_sources(mapping: dict, anilist_format: str = "", log: Log | None = None, stage: str = "extsubs.plan",
                  **context) -> tuple[dict, list[str] | None]:
    """Is this entry a movie: ani.zip's `mappings.type` or AniList's `format`, either one
    saying MOVIE, so neither source can veto it. The one rule for the subtitle plan (`extsubs.episode_plan`)
    and the resolve's sibling check (`cli.entry_format`). Returns `(format_from, movie_from)`: both raw values
    and the sources that said MOVIE (None when neither did). Neither source naming a format is not a movie:
    the sibling check then refuses rather than plays another show. When one says MOVIE and the other names
    another format, `log` gets a `<stage> movie CONFLICT` warning with `context`."""
    m = (mapping or {}).get("mappings") or {}
    format_from = {"ani.zip": str(m.get("type") or "").upper() or None,
                   "anilist": str(anilist_format or "").upper() or None}
    movie_from = [src for src, value in format_from.items() if value == "MOVIE"] or None
    if log is not None and movie_from and len(movie_from) == 1 and all(format_from.values()):
        log.warn(stage, "movie", result="CONFLICT", **context, format_from=format_from, movie_from=movie_from,
                 suggest="the two sources disagree on whether this entry is a movie; it is taken as one. "
                 "Check the entry's format on anilist.co and ani.zip's mappings.type")
    return format_from, movie_from


def movie_pairs(mapping: dict, episode: int = 1) -> set[tuple[int, int]]:
    """The TVDB special ani.zip files a movie as (`S00E10`), which a name may state and still be the
    movie. The pair only, never its episode number (a bare `- 10` names a series' episode); ani.zip's own
    `episodes[n]` only, never a release's `tvdb_season` guess; season 0 only, since a movie filed `S01E01` would make
    the series' first episode the movie. Empty when ani.zip gives no such pair. The subtitle plan
    (`extsubs.episode_plan`) and the resolve's release filter (`EpisodeQuery.movie_pairs`) both read it."""
    ep = ((mapping or {}).get("episodes") or {}).get(str(episode)) or {}
    season, number = ep.get("seasonNumber"), pos_int(ep.get("episodeNumber"))
    if isinstance(season, int) and not isinstance(season, bool) and season == 0 and number:
        return {(0, number)}
    return set()


def season_map(mapping: dict) -> dict | None:
    """How ani.zip files an entry's episodes in TVDB's seasons, for a pack's file pick
    (`score.pick_episode_file`): 11061's 148 episodes are TVDB's `S1E1`-`S1E58`, `S2E1`-`S2E78` and `S3E1`-`S3E12`, so a
    group that counts season 2 from 1 means episode 63 by `S02E05`, not 5. None when ani.zip pairs no regular episode
    (no answer, or no `seasonNumber`/`episodeNumber`). Else `first`, the TVDB season of the entry's first episode;
    `seasons`, the TVDB seasons after it whose episodes AniList numbers past the season's own (empty for an entry in
    one season, and for a later cour TVDB files as `S2E11`, ahead of AniList's 1: only an entry AniList numbers on
    across TVDB seasons has any); `counts`, the highest `episodeNumber` ani.zip gives each season; and `pairs`,
    AniList's episode to its `(seasonNumber, episodeNumber)`. Only the regular episodes (ani.zip's numbered keys) and
    seasons from 1 are read; season 0 is TVDB's specials. Episode-independent: the pick looks up its own episode
    (`episode_pair`)."""
    pairs: dict[int, tuple[int, int]] = {}
    for key, ep in ((mapping or {}).get("episodes") or {}).items():
        season, number = (ep or {}).get("seasonNumber"), pos_int((ep or {}).get("episodeNumber"))
        if str(key).isdigit() and isinstance(season, int) and not isinstance(season, bool) and season >= 1 and number:
            pairs[int(key)] = (season, number)
    if not pairs:
        return None
    first = pairs[min(pairs)][0]
    counts: dict[int, int] = {}
    for season, number in pairs.values():
        counts[season] = max(counts.get(season, 0), number)
    return {"first": first, "seasons": sorted({season for n, (season, number) in pairs.items()
                                               if season > first and n > number}),
            "counts": dict(sorted(counts.items())), "pairs": pairs}


def episode_pair(smap: dict, episode: int) -> tuple[list | None, bool]:
    """ani.zip's `[seasonNumber, episodeNumber]` for `episode` in a `season_map` (None when it has none), and
    whether another of the entry's episodes has that pair too (a pair that names two episodes names neither)."""
    pair = smap["pairs"].get(int(episode))
    return (list(pair) if pair else None), pair is not None and sum(p == pair for p in smap["pairs"].values()) > 1


def pos_int(value) -> int | None:
    """A positive integer from an int or a numeric string (ani.zip and the add-ons send both), else None."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def airdate(ep: dict) -> str | None:
    """An ani.zip episode's air date: `airdate` (AniDB's half) or `airDate` (TVDB's)."""
    return (ep or {}).get("airdate") or (ep or {}).get("airDate")


def episode_ids(mapping: dict, episode: int, log: Log) -> dict:
    """Trusted subset: only anidbEid; TVDB fields are known-bad for long series."""
    ep = (mapping.get("episodes") or {}).get(str(episode)) or {}
    out = {
        "anidb_id": mapping["mappings"].get("anidb_id"),
        "kitsu_id": mapping["mappings"].get("kitsu_id"),
        "anidb_eid": ep.get("anidbEid"),
        "title": (ep.get("title") or {}).get("en") or (ep.get("title") or {}).get("x-jat"),
        "airdate": airdate(ep),
        # Fansub groups sometimes number continuously across cours (Thistle Rakka-tan ep 4 appears as "- 44"/"S17E44")
        # or absolutely. Both come from the TVDB half of ani.zip; unreliable for Harbor Lights keys 9-22,
        # so they are only *additional* accepted numbers, never a replacement for the AniList number.
        "alt_numbers": sorted({int(v) for v in (ep.get("episodeNumber"), ep.get("absoluteEpisodeNumber"))
                               if isinstance(v, int) and v != episode and v > episode}),
    }
    log.info("map.episode", "resolve", result="PASS" if out["anidb_eid"] else "PARTIAL", episode=episode, **out,
             suggest="" if out["anidb_eid"] else "no anidbEid: fall back to ?aid= + title regex")
    return out


