"""Command pipeline and argparse entry point."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import functools
import getpass
import hashlib
import json
import math
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
import traceback
import types
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

from .core import *  # noqa: F401,F403
from .anizip import airdate, anizip_mappings, episode_ids, movie_sources
from . import anizip
from . import anilist as anilistmod
from .score import parse_title, has_language, rank_candidates, score_candidate, pack_range_excludes, pack_ranges
from .score import audio_dub, dub_reading, original_audio
from .providers import (RealDebrid, TorBox, BlockGuard, PROVIDER_NAMES, range_check, torbox_torrent_info,
                        torbox_usable, torbox_plan_name)
from .mpv import Mpv
from . import mpv as mpvmod
from .anilist import (AniList, franchise_shows, next_episode, not_out, known_titles, known_spellings, plan_hints,
                      prequel_shows, related_shows, relation_nodes, remembered_episodes, saved_answer,
                      token_expiry, valid_media_id, shown as log_value,
                      FRANCHISE_QUERY, RATE_FILE, TOP100_CACHE_NAME, TOP100_MAX_AGE)
from . import indexers
from .indexers import release_key
from .score import (title_matches_episode, record_group_langs, propagate_verification, pick_episode_file,
                    load_group_stats, static_group_priors, order_ranked, group_stats_report, backfill_group_stats,
                    season_number, sibling_names, sibling_reading, AMBIGUOUS_SIBLINGS, movie_episode_reason,
                    guard_reading, sibling_name,
                    skipped_summary, title_numbers, title_episode_log, FRANCHISE_SHOW, FILE_NUMBER_NOT_EPISODE,
                    FILE_SEASON_RELATIVE, FIRST_SEASON_TAG, COMPILATION_FILM, compilation_film_reading, OTHER_ENTRY_TAG,
                    spelling_reading, FRANCHISE_PART_TAG, extra_parts, title_parts, FIRST_SEASON_EXACT,
                    guard_reads_drop, GUARD_DROP_RULE, first_season_pack_reading, OVA_ENTRY_TAG, first_season_reading,
                    first_season_stated, release_matches_target, FIRST_SEASON_SHOW_SKIP, pack_show_reason,
                    related_entry_reading, RELATED_ENTRY_TAG)
from .providers import probe_tracks
from . import providers as providersmod
from . import trace as tracemod
from . import player as playermod
from . import playstate
from . import prefetch as prefetchmod
from . import private_args
from . import covers
from . import thumbs as thumbsmod
from . import extsubs
from . import subs as subsmod
from . import say as saymod
from . import searches
from . import pins
from . import pending as pendingmod
from . import notify as notifymod
from . import watcher as watchermod
from . import torznab_accounts
from . import torznab_local
from . import torbox_device
from . import addon_check, addon_help
# The `status` command's code lives where `bin/omarchy-anime` can reach it without importing this module.
from .status import (ADVANCED_SETTINGS, SHEET_SETTINGS, emit_json, fail_internal, fail_stage, player_state,
                     report_setting_notes)

# `player resume`'s detached play, by name so a test spawns nothing real.
_spawn_detached = subprocess.Popen

# ----------------------------------------------------------------------------- pipeline

def title_variants(anilist_id: int, mapping: dict, log: Log | None = None,
                   anilist=None) -> tuple[list[str], str]:
    """Titles to query indexers with, and to match their results against: AniList
    romaji/english/native/synonyms (remembered by the list commands), then ani.zip's titles. Romaji
    first because Kumo-raws and most groups name releases that way.

    The same list is what `release_matches_target` compares a free-text indexer's result to, which is why an
    alternate name costs nothing extra: `[AnimeQX] GO FOR BEACON` matches the entry whose romaji is
    `Deng Ta Qianjin X` because AniList's own synonyms carry the English title.

    Given `log` and `anilist` (a getter for the command's AniList client), titles no
    list command has remembered yet are fetched once (`fetch_titles`); without them, or when that
    fetch fails, ani.zip's are used. Only the callers that search pass them.

    The season the entry *is* (`EpisodeQuery.season`) is not read from this whole list but by
    `entry_season`, which leaves ani.zip's `x-jat` out: it is AniDB's fan shorthand (`tdl s8`), not a title."""
    out: list[str] = []
    known = (fetch_titles(anilist, log, anilist_id) if log is not None and anilist is not None
             else known_titles(anilist_id)) or {}
    for t in (known.get("romaji"), known.get("english"), known.get("native"),
              *(known.get("synonyms") or [])[:2]):
        if t and t not in out:
            out.append(t)
    zt = mapping.get("titles") or {}
    for t in anizip_spellings(mapping):
        if t and t not in out:
            out.append(t)
    series_title = known.get("romaji") or known.get("english") or zt.get("en") or zt.get("x-jat") or ""
    return out, series_title


def anizip_spellings(mapping: dict) -> list[str]:
    """ani.zip's two spellings of the entry, in the order `title_variants` appends them: `x-jat` (AniDB's romaji
    short title, often a fan code such as `tdl s8` or `tdl3`) and `en` (its English title)."""
    zt = (mapping or {}).get("titles") or {}
    return [t for t in (zt.get("x-jat"), zt.get("en")) if t]


def entry_season(log: Log, anilist_id: int, variants: list[str], mapping: dict) -> int | None:
    """The one season the search filters by (`EpisodeQuery.season`; the season test rejects only an
    explicit disagreement): the first title that states one (`extsubs.entry_seasons`, the fan code left out by origin),
    or None when none does and the season test is dormant. Logged once per resolve, under `resolve` since `_resolve`
    decides it, with the title it came from, every stated reading, and ani.zip's readings beside them (`x-jat` is
    shown and not used)."""
    anilist_titles = known_spellings(anilist_id)
    stated = extsubs.entry_seasons(variants, mapping, anilist_titles)
    zt = (mapping or {}).get("titles") or {}
    anizip = {k: {"title": zt[k], "season": season_number(zt[k])} for k in ("x-jat", "en") if zt.get(k)}
    season, source = stated[0] if stated else (None, None)
    log.info("resolve", "season", result="PASS" if season else "SKIP", anilist_id=anilist_id, season=season,
             source=source, stated=[[n, t] for n, t in stated], anizip=anizip, anilist_titles=len(anilist_titles),
             reason=("the first title stating a season; a release stating another is rejected"
                     if season else "no title states a season: a release of any season passes the season test"),
             suggest="" if season or anizip.get("x-jat", {}).get("season") is None else
                     f"ani.zip's x-jat reads {anizip['x-jat']['season']} and is not consulted: a fan code's "
                     f"number is not the season groups write")
    return season

def entry_identity(log: Log, anilist_id: int, mapping: dict, variants: list[str], stage: str,
                   movie_pairs: Iterable[tuple[int, int]] | None = None) -> dict:
    """Who the entry is, for choosing a file inside a pack: its titles (`title_variants`) and whether it
    is a later season (`extsubs.entry_is_sequel`, from the seasons its titles state and ani.zip's episode 1). Logged
    once per resolve, with what the verdict rests on. Seasons come from `extsubs.entry_seasons`, so ani.zip's `x-jat`
    number is not a season claim here either (a fan code made one entry a sequel for the wrong reason; ani.zip's
    episode 1 still does). `episode1` is ani.zip's `[seasonNumber, episodeNumber]` for episode 1, or
    None; it decides whether a group's season code (`S02`) may meet a title's trailing number, and `title_numbers` logs
    each such title with that verdict (`score.season_code_refusal`). `season_map` is how ani.zip files
    an entry AniList numbers on across TVDB seasons (`anizip.season_map`), or None for any other entry; a pack that
    counts a later season from 1 has its files read through it (`score.season_relative_reading`). Episode-independent,
    as `episode1` is: the pick looks up its own episode's pair. Logged without its pair table: `unknown` when ani.zip
    pairs no episode, `not_across_seasons` when the entry is in one TVDB season (or runs ahead of it).
    For a one-file movie (`movie_is_one_file`), `movie_pairs` (its ani.zip season-0 pairs, possibly empty) makes the
    identity a movie's (`movie`, `movie_pairs`), whose file inside a torrent of several videos is the movie's, never
    episode 1's (`score.pick_movie_file`); logged as `movie` (None for any other entry)."""
    title_season = {n for n, _ in extsubs.entry_seasons(variants, mapping, known_spellings(anilist_id))}
    sequel = extsubs.entry_is_sequel(mapping, title_season)
    ep1 = ((mapping or {}).get("episodes") or {}).get("1") or {}
    pair = [ep1.get("seasonNumber"), ep1.get("episodeNumber")]
    episode1 = pair if all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in pair) else None
    smap = anizip.season_map(mapping)
    season_map = smap if smap and smap["seasons"] else None
    log.info("score.file_identity", stage, result="PASS" if sequel else "SKIP", anilist=anilist_id,
             sequel=sequel, titles=len(variants), sample=list(variants[:4]), title_seasons=sorted(title_season),
             episode1=episode1 or "unknown", title_numbers=title_numbers(variants, episode1),
             movie=None if movie_pairs is None else sorted(list(p) for p in movie_pairs),
             season_map=({k: smap[k] for k in ("first", "seasons", "counts")} if season_map else
                         "unknown" if smap is None else "not_across_seasons"),
             reason=("a one-file movie: its file in a torrent of several videos is its season-0 pair's, else the "
                     "largest video" if movie_pairs is not None else
                     "a later season: a pack's file must name one of these titles, in its path or a single-show pack "
                     "title" if sequel else "a first season (or no ani.zip season): a pack's file is picked by its "
                     "number, as before"))
    identity = {"titles": list(variants), "sequel": sequel, "episode1": episode1, "season_map": season_map}
    if movie_pairs is not None:
        identity.update(movie=True, movie_pairs=sorted(list(p) for p in movie_pairs))
    return identity


def entry_titles(anilist_id: int, variants: list[str]) -> tuple:
    """Every title the entry has: `title_variants`' list, which holds only two of AniList's synonyms, then every
    spelling AniList gives (`known_spellings`, the titles cache), each once. Read by the first-season
    trailing-number keep (`score.first_season_reading`) and, on every resolve, by the show test
    (`score.spelling_reading`), the film cut's own-title keep and the ambiguity check (`score.guard_reading`)."""
    out = list(variants or ())
    for t in known_spellings(anilist_id):
        if t not in out:
            out.append(t)
    return tuple(out)


def first_season_for(log: Log, anilist_id: int, episode: int, season: int | None, entry: dict, mapping: dict,
                     movie: bool = False, single_episode: bool = False) -> bool:
    """Is this entry TVDB's first season by ani.zip, so that a single stating season 2 or later is
    another season's episode (`EpisodeQuery.first_season`)? A season-1 entry names no season (`entry_season` is None),
    so the season test kept every `2nd Season - 05` for it. The resolve already holds ani.zip's answer and nothing of
    AniList's relations for a first season (the franchise reading reads those only for a later one), so the evidence is
    ani.zip's: every episode it pairs is in TVDB season 1, episode 1 is `S1E1`, and the asked episode has its own pair
    there. Not for a title stating a season, a sequel (the entry identity's verdict), a movie or a one-episode entry
    (their own rules), nor an entry ani.zip pairs nothing for, or not this episode (an airing show's newest episode):
    those keep the season test. An entry AniList numbers on across TVDB seasons (an absolute-numbered entry) has pairs
    past season 1, so it is out too. One `resolve first_season` line says which, with the evidence."""
    smap = anizip.season_map(mapping)
    seasons = sorted({s for s, _ in smap["pairs"].values()}) if smap else []
    pair = anizip.episode_pair(smap, episode)[0] if smap else None
    ep1 = (entry or {}).get("episode1")
    skip = ("a title states the entry's season: the season test compares with it" if season is not None
            else "a later season (the entry identity's verdict): its franchise is read" if (entry or {}).get("sequel")
            else "a movie: its own release rules apply" if movie
            else "a one-episode entry: its releases may state another season's code" if single_episode
            else "ani.zip pairs no episode of this entry with a TVDB season" if smap is None
            else f"ani.zip files this entry in TVDB seasons {seasons}, not in season 1 alone" if seasons != [1]
            else f"ani.zip files episode 1 as {ep1}, not S1E1" if ep1 != [1, 1]
            else f"ani.zip has no pair for episode {episode}" if pair is None else None)
    log.info("resolve", "first_season", result="SKIP" if skip else "PASS", anilist_id=anilist_id, episode=episode,
             season=season, sequel=(entry or {}).get("sequel"), episode1=ep1 or "unknown", pair=pair,
             tvdb_seasons=seasons, reason=skip or "ani.zip files the entry as TVDB season 1 and its titles state no "
                                                  "season: a single, or a pack of one season, stating season 2 or "
                                                  "later is another season's")
    return skip is None


def entry_format(anilist_id: int, mapping: dict, log: Log | None = None,
                 stage: str = "score.ambiguous") -> tuple[dict, list[str] | None]:
    """Is this entry a movie, by the rule the subtitle plan uses (`anizip.movie_sources`: ani.zip's
    `mappings.type` or AniList's `format` from the titles cache, `plan_hints`). `(format_from, movie_from)`."""
    return movie_sources(mapping, plan_hints(anilist_id).get("anilist_format") or "", log, stage,
                         anilist_id=anilist_id)


# The formats whose episodes are a TV series' (AniList's `TV`/`TV_SHORT`, ani.zip's `TV`).
TV_SERIES_FORMATS = ("TV", "TV_SHORT")


def tv_series_entry(log: Log, anilist_id: int, format_from: dict, movie: bool, single_episode: bool) -> bool:
    """Is the entry a TV series, whose episode is no OVA (`EpisodeQuery.tv_series`)? A source of
    `entry_format` (ani.zip's `mappings.type`, AniList's `format`) says `TV` or `TV_SHORT`, neither names another
    format, and it is neither a movie nor a one-episode entry (their own rules read their names). An OVA or ONA series
    and an entry neither source names are not: their own episodes may say `OVA`, and an unknown format keeps today's
    filter. Logged once as `resolve tv_series`, with both sources; when one says TV and the other names another
    format, the reason says the sources disagree and `suggest` names both. The one gate:
    `index_search` passes the answer through as it is."""
    named = {src: value for src, value in (format_from or {}).items() if value}
    tv_from = sorted(src for src, value in named.items() if value in TV_SERIES_FORMATS)
    conflict = bool(tv_from) and len(tv_from) < len(named) and not movie and not single_episode
    tv = bool(named) and len(tv_from) == len(named) and not movie and not single_episode
    said = ", ".join(f"{src} {value}" for src, value in sorted(named.items()))
    log.info("resolve", "tv_series", result="PASS" if tv else "SKIP", anilist_id=anilist_id, format_from=format_from,
             reason=("a TV series: a single stating an OVA before its episode marker is another entry" if tv
                     else "a movie" if movie else "a one-episode entry" if single_episode else
                     "no source names its format" if not named else
                     f"the sources disagree ({said}): today's filter" if conflict else
                     f"not a TV series ({said}): its own episodes may say OVA"),
             suggest="check the entry's format on anilist.co and ani.zip's mappings.type" if conflict else "")
    return tv


def anilist_episode_count(anilist_id: int, chain: dict | None = None) -> tuple[int | None, str, int | None]:
    """AniList's episode count for an entry: `(count, source, fresh)`. `fresh` is this resolve's relations answer
    (`chain`), which wins over the remembered titles cache; `count` is None when neither knows."""
    fresh = (chain or {}).get("episodes")
    fresh = fresh if isinstance(fresh, int) and not isinstance(fresh, bool) and fresh > 0 else None
    if fresh is not None:
        return fresh, "anilist.relations", fresh
    return remembered_episodes(anilist_id), "anilist.titles", None


def movie_is_one_file(log: Log, anilist_id: int, episode: int, chain: dict | None = None) -> bool:
    """Is this movie one file, so that its file in a torrent of several videos is the movie's
    (`score.pick_movie_file`), not episode `episode`'s? Only for its episode 1, when AniList counts one episode or the
    count is unknown (a movie almost always has one). A movie entry AniList counts in parts (`episodes` 2 or more), or
    asked for another episode, keeps the number rule: its `- 02` is its part 2. One `score.file_identity movie` line."""
    count, source, _ = anilist_episode_count(anilist_id, chain)
    one = int(episode) == 1 and (count is None or count <= 1)
    log.info("score.file_identity", "movie", result="PASS" if one else "SKIP", anilist_id=anilist_id, episode=episode,
             episodes=count, source=source if count is not None else None,
             reason=("one file: its file in a torrent of several videos is the movie's (season-0 pair, else the "
                     "largest video)" if one else
                     f"episode {episode} of {count if count is not None else 'an unknown count'}: picked by its "
                     "number, as before"))
    return one


def one_episode_entry(log: Log, anilist_id: int, episode: int, movie: bool, chain: dict | None = None,
                      stage: str = "resolve") -> bool:
    """Is this a one-episode entry asked for its episode 1 (any non-movie entry whose AniList
    `episodes` is 1, whatever its format; a movie has its own rules)? Its releases say `S04E30`, `Special 2` or
    nothing, so the search reads them by the entry's titles (`EpisodeQuery.single_episode`). The count is AniList's:
    this entry's relations answer when resolve fetched it (`chain`, for an entry ani.zip has no AniDB id for),
    else the titles cache (`remember_titles`). ani.zip's `episodeCount` is AniDB's and differs (measured). An unknown
    count keeps the numbered search. `stage` names the line's component (`releases.query` from the picker)."""
    remembered = remembered_episodes(anilist_id)
    count, source, fresh = anilist_episode_count(anilist_id, chain)
    if remembered is not None and fresh is not None and fresh != remembered:
        log.warn(stage, "single_episode", result="WARN", anilist_id=anilist_id, titles_cache=remembered,
                 relations=fresh, suggest="the remembered AniList titles are older than AniList's count; this "
                                          "resolve uses the relations answer, and the next list or search of this "
                                          "show rewrites them")
    single = count == 1 and int(episode) == 1 and not movie
    if movie:
        why = "a movie: its own release rules apply"
    elif count is None:
        why = "AniList's episode count is not known here (no remembered count, no relations answer): numbered search"
    elif single:
        why = "one episode: releases are read by the entry's titles, not by an episode number"
    else:
        why = f"{count} episodes: numbered search" if count != 1 else f"episode {episode} of a one-episode entry"
    log.info(stage, "single_episode", result="PASS" if single else "SKIP", anilist_id=anilist_id,
             episode=episode, episodes=count, source=source if count is not None else None, movie=movie, reason=why)
    return single


def fetch_related_chain(log: Log, anilist_id: int, episode: int, movie: bool, pinned: bool, chains: dict,
                        fetch_chain) -> str:
    """Fetch this entry's relations answer (the related shows' titles) when it is remembered as
    one-episode and resolve holds no answer yet: before the count is decided, so that answer's count still wins.
    Returns the answer's source for `related_shows_for`: "walk" (the prequel walk already fetched
    it), "fetched", "failed", or "skipped". One `resolve related_shows.fetch` line says
    which, with the request's time or the reason it was not made."""
    if anilist_id in chains:
        log.info("resolve", "related_shows.fetch", result="SKIP", anilist_id=anilist_id, source="walk",
                 reason="the prequel walk already read this entry's relations answer")
        return "walk"
    remembered = remembered_episodes(anilist_id)
    skip = ("a movie: its own release rules apply" if movie
            else f"episode {episode}: only episode 1 of a one-episode entry reads related shows" if int(episode) != 1
            else "a pinned play searches nothing and asks AniList for nothing" if pinned
            else f"the remembered count is {remembered}, not 1: not read as a one-episode entry"
            if remembered != 1 else None)
    if skip:
        log.info("resolve", "related_shows.fetch", result="SKIP", anilist_id=anilist_id, reason=skip)
        return "skipped"
    t0 = time.monotonic()
    try:
        fetch_chain(anilist_id)
    except StageError as exc:
        chains[anilist_id] = None
        log.warn("resolve", "related_shows.fetch", result="WARN", anilist_id=anilist_id,
                 ms=round((time.monotonic() - t0) * 1000), error=str(exc),
                 suggest="AniList's relations answer failed: this one-episode entry is read by its titles alone, "
                         "and a related show's episodes that hold one of them are not dropped")
        return "failed"
    log.info("resolve", "related_shows.fetch", result="PASS", anilist_id=anilist_id,
             ms=round((time.monotonic() - t0) * 1000),
             reason="remembered one-episode, no relations answer yet")
    return "fetched"


def relations_read(log: Log, stage: str, anilist_id: int, reader, chain) -> tuple:
    """`reader(chain)` (`anilist.related_shows`, `prequel_shows`), `()` without an answer. An answer of the wrong type
    (an edge that is a string, an id that is a word or `Infinity`: the readers raise on it) degrades as a failed read
    does, with one WARN, instead of ending the resolve."""
    if not chain:
        return ()
    try:
        return reader(chain, log)
    except (AttributeError, TypeError, ValueError, ArithmeticError) as exc:
        log.warn("resolve", stage, result="WARN", anilist_id=anilist_id, error=f"{type(exc).__name__}: {exc}"[:300],
                 suggest="AniList's relations answer could not be read: the play goes on as after a failed read")
        return ()


def related_shows_for(log: Log, anilist_id: int, chains: dict, source: str) -> tuple:
    """The related shows (`anilist.related_shows`) of a one-episode entry, from this resolve's relations
    answer (`chains`; `source` is `fetch_related_chain`'s answer), logged once. `()` when the answer is missing (a
    pinned play, or a failed fetch): the entry is then read by its titles alone."""
    chain = chains.get(anilist_id)
    related = relations_read(log, "related_shows", anilist_id, related_shows, chain)
    ids = sorted({rid for rid, _, _ in related})
    if chain:
        log.info("resolve", "related_shows", result="PASS" if ids else "EMPTY", anilist_id=anilist_id, ids=ids,
                 titles=len(related), source=source,
                 reason="a name that is an episode of one of these shows is dropped" if ids else
                        "no related entry with more than one episode")
    elif source in ("failed", "skipped"):
        # A pinned play searches nothing, and a failed read has its own WARN above.
        log.info("resolve", "related_shows", result="SKIP", anilist_id=anilist_id, ids=[], source=source,
                 reason="the relations read failed (see the `related_shows.fetch` WARN): related shows' episodes are "
                        "not dropped" if source == "failed" else "no relations answer: this play searches nothing")
    else:
        log.warn("resolve", "related_shows", result="WARN", anilist_id=anilist_id, ids=[], source=source,
                 suggest="no AniList relations answer in this resolve: a related show's episodes that hold one of "
                         "this entry's titles are not dropped")
    return related


def prequel_shows_for(log: Log, anilist_id: int, chains: dict) -> tuple:
    """A one-episode entry's one-episode prequels (`anilist.prequel_shows`), from this resolve's
    relations answer (`chains`), logged once: a name that numbers on from one of them is the entry's own
    (`score.prequel_continuation`). `()` when the answer is missing (`related_shows_for` says why)."""
    chain = chains.get(anilist_id)
    prequels = relations_read(log, "prequel_shows", anilist_id, prequel_shows, chain)
    shows = sorted({(rid, eps) for rid, _, eps in prequels})
    log.info("resolve", "prequel_shows", result="PASS" if shows else "EMPTY" if chain else "SKIP",
             anilist_id=anilist_id, shows=[list(x) for x in shows], titles=len(prequels),
             reason="a name that is one of their titles with the episode after that show's last is the entry's own"
                    if shows else "no prequel with one episode" if chain
                    else "no relations answer in this resolve")
    return prequels


FRANCHISE_IDS_LOGGED = 25                 # ids on one `resolve franchise_shows` line
NOT_EPISODE_LOGGED = 25                   # names labelled "not episode N" on one `releases.query` line


def franchise_miss_path(anilist_id: int) -> Path:
    """A failed franchise read is remembered beside the failed titles fetch's `titles-miss/`."""
    return CACHE_DIR / "anilist" / "franchise-miss" / f"{anilist_id}.json"


# How long a franchise answer (`franchise_cache_path`) is used without asking AniList again. Relations
# change when a season or special is announced, not per play. A stale answer errs towards today's filter (a show added
# since is not dropped), except for a show whose count AniList did not know yet: the rule reads a null count as any
# number, so an answer holding one is kept a day, until the count a finished show gets is read.
# 8 days, not 7, so a weekly watcher's next play still finds it.
FRANCHISE_TTL = 8 * 24 * 3600
FRANCHISE_UNKNOWN_COUNT_TTL = 24 * 3600
# When AniList cannot be asked, an answer this old at most is still used, if every count in it
# is known (a null count is the direction a stale answer drops wrongly). Past it, today's filter.
FRANCHISE_STALE_MAX_AGE = 30 * 24 * 3600


def franchise_ttl(shows: tuple) -> int:
    """How long a franchise answer is kept, from its `franchise_shows` `(id, title, format, episodes)`:
    `FRANCHISE_UNKNOWN_COUNT_TTL` when a show's `episodes` is null (the rule reads it as any number) or there is no
    show (an EMPTY answer: usually a new cour AniList has not linked yet), else `FRANCHISE_TTL`. The one
    place both the hit check and the logged `ttl_s` take it from."""
    episodes = [count for _id, _title, _format, count in shows]
    known = bool(episodes) and all(count is not None for count in episodes)
    return FRANCHISE_TTL if known else FRANCHISE_UNKNOWN_COUNT_TTL
# The answer's shape is `FRANCHISE_QUERY`'s: a cached answer of another query is read again, not parsed.
FRANCHISE_QUERY_KEY = hashlib.sha1(FRANCHISE_QUERY.encode("utf-8")).hexdigest()[:12]


def franchise_cache_path(anilist_id: int) -> Path:
    """The last franchise answer (AniList's raw `Media`), beside `franchise-miss/`."""
    return CACHE_DIR / "anilist" / "franchise" / f"{anilist_id}.json"


def cached_franchise(anilist_id: int, log: Log | None = None) -> tuple[tuple | None, dict]:
    """The franchise shows of the cached answer whenever it parses (`cache=hit` when it is younger than
    its TTL; a stale one is kept for `stale_franchise`), and what the cache held for the `resolve franchise_shows` line
    (`cache`: none, hit, stale, future, other_query or corrupt, with `age_s`, `ttl_s` and `cache_error`). The raw
    answer is stored, so a change to `franchise_shows` needs no flush. A file that cannot be read, is not JSON, has the
    wrong shape, answers for another id or no longer parses is corrupt: it is removed when it can be (`removed`, and
    `remove_error` when not) and the read goes to AniList (the cache never fails a resolve). A `ts` in the
    future (a clock that ran ahead, a file from another machine) is `future`: read again and rewritten, never a hit."""
    # Not `cached_json`: it returns None for a missing and a corrupt file alike and ages by mtime, so neither the
    # `none`/`corrupt` split nor the WARN could be logged; the age is the stored `ts` (as torznab_prowlarr's `ts`).
    path = franchise_cache_path(anilist_id)
    try:
        try:
            # No `exists()` first; it answers False on EACCES, hiding the error.
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, {"cache": "none"}
        stored = json.loads(text)
        if not isinstance(stored, dict) or not isinstance(stored.get("media"), dict):
            raise ValueError("not an object holding the answer's `media`")
        ts = stored.get("ts")
        # JSON's `NaN`/`Infinity` and an overflowing number parse as numbers no age can be taken of
        # (`math.isfinite` raises `OverflowError` on an int past a float's range).
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            raise ValueError(f"no time it was read (ts={str(ts)[:40]})")
        age = time.time() - ts
        if stored.get("query") != FRANCHISE_QUERY_KEY:
            return None, {"cache": "other_query", "age_s": round(age)}
        if stored["media"].get("id") != anilist_id:
            raise ValueError(f"the answer is for id {stored['media'].get('id')!r}")
        shows = franchise_shows(stored["media"], log)
        ttl = franchise_ttl(shows)
        facts = {"cache": "future" if age < 0 else "hit" if age < ttl else "stale", "age_s": round(age), "ttl_s": ttl}
    # A file nested past the parser's depth raises RecursionError from `json.loads`.
    except (OSError, ValueError, ArithmeticError, AttributeError, TypeError, RecursionError) as exc:
        facts = {"cache": "corrupt", "cache_error": f"{type(exc).__name__}: {exc}", "removed": True}
        try:
            path.unlink(missing_ok=True)
        except OSError as uerr:                 # a directory in its place, or a folder the plugin cannot write
            facts.update(removed=False, remove_error=f"{type(uerr).__name__}: {uerr}")
        return None, facts
    return shows, facts


def stale_franchise(shows: tuple | None, cache: dict) -> tuple | None:
    """A graduated response: a stale cached answer to use when AniList cannot be asked (the
    read failed, or its miss marker is fresh), when it has shows, every count known (`franchise_ttl` is the full TTL)
    and is at most `FRANCHISE_STALE_MAX_AGE` old; else None, and the filter is today's."""
    if cache.get("cache") != "stale" or not shows or franchise_ttl(shows) != FRANCHISE_TTL:
        return None
    return shows if cache.get("age_s", FRANCHISE_STALE_MAX_AGE + 1) <= FRANCHISE_STALE_MAX_AGE else None


def remember_franchise(anilist_id: int, media: dict) -> str:
    """Cache a franchise answer that parsed (`franchise_shows_for` calls it after the parse, so a
    malformed answer is never kept), atomically (`write_cache`). Returns the `cache_write` field: PASS, SKIP when the
    answer names no id of this entry (AniList's `Media: null`), or FAIL with the error; a write that fails is logged,
    never raised."""
    if not isinstance(media, dict) or media.get("id") != anilist_id:
        return "SKIP: the answer names no entry with this id"
    try:
        write_cache(franchise_cache_path(anilist_id), {"query": FRANCHISE_QUERY_KEY, "ts": time.time(),
                                                       "anilist_id": anilist_id, "media": media})
    except OSError as exc:
        return f"FAIL: {type(exc).__name__}: {exc}"
    return "PASS"


def franchise_shows_for(log: Log, anilist_id: int, entry: dict, movie: bool, single_episode: bool, pinned: bool,
                        anilist) -> tuple:
    """The shows two relation hops around a later-season entry (`anilist.franchise_shows`, one public
    AniList read), whose exact titles the filter reads as another show's single (`query.franchise_shows`). Not read for
    a one-episode entry (its related shows are read instead) or a pinned play (it searches nothing). A
    failed read logs a WARN and keeps today's filter. One `resolve franchise_shows` line says which, with the ids and
    the request's time.

    A first season (no sequel verdict from the entry identity, `entry`) reads them too, for the stated-part
    reading only, and only when `index_search` finds a name stating a part none of the entry's titles state
    (`first_season_parts`): `Show Part 2 - 04` is the second part's episode 4, not the first's. The same cache, miss
    marker and one attempt; the line says `sequel: null` and which reading the shows feed.

    The read is enrichment, so it is one attempt (`AniList.franchise`,
    `retries=1`: a hung AniList costs one `httpTimeout`, not three plus backoff), and a failure is remembered for
    `TITLES_MISS_TTL` (`franchise_miss_path`), as a failed titles fetch is remembered: a play of the same show
    within the hour skips the read (SKIP, with the miss's age and error) instead of paying it again.

    An answer that parsed is cached (`franchise_cache_path`, `franchise_ttl`), so a play of the same
    show within it asks AniList nothing. The cache is read before the miss marker (the two can coexist after a race:
    one resolve's read fails after another's succeeded; the cache is read first and wins), and a stale, future-dated,
    corrupt or other query's cache falls through to the read, which overwrites it. When AniList cannot be asked (the
    read fails, or the marker is fresh), a stale answer `stale_franchise` accepts is used (`source=cache_stale`). The
    PASS/EMPTY line says `source` (cache or anilist) with the answer's `age_s` and `ttl_s`; after a read, `cache` says
    what the cache held (none, stale, future, other_query, corrupt) with its `cache_age_s`, and `cache_write` whether
    the answer was kept. A corrupt file adds a `franchise_shows.cache` WARN.

    A movie reads them too (the same read, cache, miss marker and one attempt), for one rule only: a
    release naming another entry of its franchise without an episode marker is dropped after the passes
    (`related_entry_check`). `index_search` never hands a movie's shows to the later-season franchise reading. The line
    says `movie`."""
    skip = ("a one-episode entry: its related shows are read instead" if single_episode and not movie
            else "a pinned play searches nothing and asks AniList for nothing" if pinned else None)
    if skip:
        log.info("resolve", "franchise_shows", result="SKIP", anilist_id=anilist_id,
                 sequel=(entry or {}).get("sequel"), reason=skip)
        return ()
    t0 = time.monotonic()
    shows, cache = cached_franchise(anilist_id, log)
    if cache["cache"] == "corrupt":
        path = franchise_cache_path(anilist_id)
        # Say whether the file is gone, so a WARN repeated on every play explains itself.
        log.warn("resolve", "franchise_shows.cache", result="WARN", anilist_id=anilist_id, path=str(path),
                 error=cache["cache_error"], removed=cache["removed"], remove_error=cache.get("remove_error"),
                 suggest="the cached franchise answer could not be used and was removed; AniList is read again and "
                         "the answer rewrites it" if cache["removed"] else
                         f"the cached franchise answer could not be used, nor removed: every play of this show reads "
                         f"AniList until {path} is deleted by hand")
    if cache["cache"] == "hit":
        log_franchise_shows(log, anilist_id, entry, shows, t0, movie, source="cache", **cache)
        return shows
    # Not used: what the cache held, for the line of the read (or of the miss marker's SKIP).
    unused = {"cache": cache["cache"], "cache_age_s": cache.get("age_s"), "cache_error": cache.get("cache_error")}
    fallback = stale_franchise(shows, cache)
    # When AniList cannot be asked, the stale answer's shows go on the same line as `cache_stale`.
    served = ({"source": "cache_stale", "age_s": cache["age_s"], **franchise_fields(fallback)} if fallback else {})
    miss_path = franchise_miss_path(anilist_id)
    miss = cached_json(miss_path, TITLES_MISS_TTL)
    if isinstance(miss, dict):
        try:
            age = round(time.time() - miss_path.stat().st_mtime)
        except OSError:
            age = None
        log.info("resolve", "franchise_shows", result="SKIP", anilist_id=anilist_id, sequel=entry["sequel"],
                 miss_age_s=age, ttl_s=TITLES_MISS_TTL, last_error=miss.get("error"), **unused,
                 **(served or {"source": "none"}),
                 reason=f"the franchise read failed within the hour: the cached answer, {cache.get('age_s')} s old, "
                        "is used without asking AniList again" if fallback else
                        "the franchise read failed within the hour: today's filter, without asking AniList again")
        return fallback or ()
    try:
        media = anilist().franchise(anilist_id)
        shows = franchise_shows(media, log)
    except (StageError, AttributeError, TypeError, ValueError, ArithmeticError) as exc:
        # An answer of the wrong shape (a string edge or title, a non-numeric
        # id, `Infinity`) degrades as a failed read does, instead of ending the resolve.
        error = f"{type(exc).__name__}: {exc}"
        try:
            write_cache(miss_path, {"error": error, "ts": time.time()})
            marked = True
        except OSError as werr:
            marked, error = False, f"{error}; the miss marker was not written: {werr}"
        log.warn("resolve", "franchise_shows", result="WARN", anilist_id=anilist_id, sequel=entry["sequel"],
                 ms=round((time.monotonic() - t0) * 1000), error=error, **unused,
                 **(served or {"source": "anilist"}), miss_marker_ttl_s=TITLES_MISS_TTL if marked else None,
                 suggest=f"AniList's relations answer failed: the cached answer, {cache.get('age_s')} s old, is used "
                         "(every count in it known); AniList is asked again after the miss marker's hour" if fallback
                 else "AniList's relations answer failed: a release naming another entry of this movie's franchise "
                      f"without an episode marker is kept this time ({RELATED_ENTRY_TAG})" if movie
                 else "AniList's relations answer failed: a single of another part of the franchise is kept this "
                      "time (the resolve goes on)" if not entry.get("sequel")
                 else "AniList's relations answer failed: a single of another season or show of the franchise "
                      "that names no season is kept this time")
        return fallback or ()
    cache_write = remember_franchise(anilist_id, media)
    ttl = franchise_ttl(shows) if cache_write == "PASS" else None
    log_franchise_shows(log, anilist_id, entry, shows, t0, movie, source="anilist", age_s=0 if ttl else None,
                        ttl_s=ttl, cache_write=cache_write, **unused)
    return shows


# What a first season's franchise shows are read for (the `resolve franchise_shows` and `index.search
# franchise_part` lines).
FIRST_SEASON_PARTS = ("a first season: only a single stating a part none of the entry's titles state, where a show is "
                      "one of those titles plus that part, is dropped as `franchise_show`; a name "
                      "that is exactly another show's title is kept")


def log_franchise_shows(log: Log, anilist_id: int, entry: dict, shows: tuple, t0: float, movie: bool = False,
                        **source) -> None:
    """The PASS/EMPTY `resolve franchise_shows` line, for an answer read now or cached (`source`,
    `cache`, `age_s`, `ttl_s`, and `cache_write` after a read). A movie's line says `movie` and its rule."""
    fields = franchise_fields(shows)
    log.info("resolve", "franchise_shows", result="PASS" if fields["ids"] else "EMPTY", anilist_id=anilist_id,
             sequel=entry["sequel"], ms=round((time.monotonic() - t0) * 1000), **fields, **source,
             **({"movie": True} if movie else {}),
             reason="no related anime two hops around this entry" if not fields["ids"] else
                    RELATED_ENTRY_REASON if movie else
                    "a later season: a single whose words are exactly one of these shows' titles is dropped as "
                    "`franchise_show`" if entry["sequel"] else FIRST_SEASON_PARTS)


def franchise_fields(shows: tuple) -> dict:
    """The shows' fields of a `resolve franchise_shows` line: ids and the titles compared, each capped."""
    ids = sorted({s[0] for s in shows})
    compared = sorted({s[1] for s in shows})          # the titles a name is compared with
    return {"shows": len(ids), "titles": len(shows), "ids": ids[:FRANCHISE_IDS_LOGGED],
            "more": max(0, len(ids) - FRANCHISE_IDS_LOGGED), "compared": compared[:FRANCHISE_IDS_LOGGED],
            "compared_more": max(0, len(compared) - FRANCHISE_IDS_LOGGED)}


# How long a failed or empty title fetch is remembered, so a play of the same show
# does not pay the fetch again while AniList is down, and an id AniList does not know (its answer is
# HTTP 404 "Not Found.", measured 2026-09-28) is not asked on every play.
TITLES_MISS_TTL = 3600


def titles_miss_path(anilist_id: int) -> Path:
    return CACHE_DIR / "anilist" / "titles-miss" / f"{anilist_id}.json"


def fetch_titles(anilist, log: Log, anilist_id: int) -> dict | None:
    """The remembered AniList titles, fetched once when no list command has shown the
    show yet (a keybind play, auto-next into an unbrowsed sequel, the CLI). `anilist` returns the
    command's client. None when there are none to be had: the caller searches with ani.zip's titles.
    A failed or empty fetch is remembered for `TITLES_MISS_TTL`; the next play after that asks again."""
    known = known_titles(anilist_id)
    if known:
        log.debug("titles", "fetch", result="HIT", source="cache", anilist_id=anilist_id)
        return known
    miss_path = titles_miss_path(anilist_id)
    miss = cached_json(miss_path, TITLES_MISS_TTL)
    if isinstance(miss, dict):
        try:
            age = round(time.time() - miss_path.stat().st_mtime)
        except OSError:
            age = None
        log.info("titles", "fetch", result="SKIP", source="anizip", anilist_id=anilist_id, miss_age_s=age,
                 ttl_s=TITLES_MISS_TTL, last_result=miss.get("result"), last_error=miss.get("error"),
                 suggest="a fetch for this id failed or found nothing within the hour; searching with ani.zip's "
                         "titles. Browsing the show remembers AniList's")
        return None
    client = None
    error, error_stage = "", None
    try:
        client = anilist()
        client.media_titles(anilist_id)
        known = known_titles(anilist_id)
        result = "PASS" if known else "EMPTY"
        if not known:
            error = "AniList answered with no media for this id"
    except Exception as exc:  # noqa: BLE001 - titles are enrichment; ani.zip's still search
        errors = exc.data.get("errors") if isinstance(exc, StageError) else None
        # An id AniList does not know is HTTP 404 "Not Found." (measured 2026-09-28): an answer, not a failure.
        result = "EMPTY" if errors and all(e.get("status") == 404 for e in errors) else "FAIL"
        error, error_stage = f"{type(exc).__name__}: {exc}", getattr(exc, "stage", None)
    if result == "PASS":
        log.info("titles", "fetch", result="PASS", source="anilist", anilist_id=anilist_id,
                 romaji=known.get("romaji"), english=known.get("english"),
                 synonyms=len(known.get("synonyms") or []), anonymous=client.answered_anonymously)
        return known
    try:
        write_cache(miss_path, {"result": result, "error": error, "ts": time.time()})
        marked = True
    except OSError as exc:
        marked, error = False, f"{error}; the miss marker was not written: {exc}"
    log.warn("titles", "fetch", result=result, source="anizip", anilist_id=anilist_id, error=error,
             error_stage=error_stage, miss_marker_ttl_s=TITLES_MISS_TTL if marked else None,
             suggest=("AniList has no media with this id" if result == "EMPTY" else
                      "AniList could not be read") + "; the indexers are asked with ani.zip's titles only, "
                      "which can miss packs named by the full title")
    return None


def lazy_anilist(http: Http, log: Log):
    """One AniList client for a command, built (and its token read) on first use only. A client
    remembers a rejected token, so sharing one keeps a command at one rejected request."""
    made: list = []

    def get() -> AniList:
        if not made:
            made.append(AniList(http, log, AniList.load_token()))
        return made[0]
    return get


PACK_PASS_MIN_PLAYABLE = 3      # fewer seeded releases that can play the episode than this asks for packs
PACK_NAMES_LOGGED = 40          # names on the `index.search packs` line (as torznab's line is bounded)


def pack_pass_decision(releases: list[dict], episode: int | None = None, alt: Iterable[int] = (),
                       season: int | None = None, titles: Iterable[str] = ()) -> tuple[bool, list[tuple], list[tuple]]:
    """The pack pass's gate, as `index_search` asks it: `(run, counted, out_of_range)`. `counted` is
    the seeded releases that can play the episode, each as `(release, pack_by)`; `out_of_range` the seeded packs that
    cannot, each as `(release, ranges)`: a pack whose named ranges hold neither the episode nor an alt number
    (`score.pack_range_excludes`, the prefilter's `pack range`). Each title is parsed once, as the ranking parses it
    (`parse_title` with the entry's season and titles), and its `pack_by` is that reading's ("" for a single), so the
    log line says which counted releases the reading took for packs. `run` is fewer than `PACK_PASS_MIN_PLAYABLE`
    counted; `index_search` also needs `allowBatches` on and a title to search by. With no episode every seeded
    release counts, as before."""
    counted, out_of_range = [], []
    for r in releases:
        if not (r.get("seeders") or 0) > 0:
            continue
        parsed = parse_title(str(r.get("title") or ""), season, titles)
        if pack_range_excludes(r, parsed, episode, list(alt)):
            out_of_range.append((r, pack_ranges(str(r.get("title") or ""))))
        else:
            counted.append((r, parsed["pack_by"]))
    return len(counted) < PACK_PASS_MIN_PLAYABLE, counted, out_of_range


def needs_batch_query(releases: list[dict], episode: int | None = None, alt: Iterable[int] = (),
                      season: int | None = None, titles: Iterable[str] = ()) -> bool:
    """The production gate's `run` (`pack_pass_decision`, which `index_search` calls), for callers that want
    the answer alone: look for season packs when fewer than three seeded releases can play the episode."""
    return pack_pass_decision(releases, episode, alt, season, titles)[0]


def merge_releases(earlier: list[dict], later: list[dict]) -> list[dict]:
    """Union keyed by infohash. The earlier list's record wins (it may carry attachments), but the later
    list's live seeder/leecher counts overwrite it: an index can answer null for old releases (Lantern x
    Lantern 2011). Folded pairwise across the indexers in their order.

    The winner keeps every other indexer that returned the same infohash in `also_from`, each
    as `{indexer, release_id}` under that indexer's own release id, in the order they were merged, so the
    subtitle tracks can be asked of another one when the winner's add-on cannot read them
    (`indexers.returned_by`)."""
    by_hash: dict[str, dict] = {}
    listed: dict[int, tuple[list, indexers.RefSet]] = {}
    for r in earlier:
        h = str(r.get("info_hash") or "").lower()
        if h:
            by_hash[h] = r
    for i in later:
        h = str(i.get("info_hash") or "").lower()
        if not h:
            continue
        if h in by_hash:
            if i.get("seeders") is not None:
                by_hash[h]["seeders"] = i["seeders"]
                by_hash[h]["leechers"] = i.get("leechers")
                by_hash[h]["seeders_from"] = i.get("indexer") or i.get("source")
            _add_also_from(by_hash[h], i, listed)
        else:
            by_hash[h] = i
    return list(by_hash.values())


def _add_also_from(winner: dict, other: dict, listed: dict) -> None:
    """`other`'s own ref and its `also_from` join `winner`'s `also_from` (a fresh list, never
    shared between records), skipping the winner's own indexer (a second id from it changes nothing about
    who is asked) and any ref already listed. The pack pass returns the first pass's releases again, so the
    same indexer arriving twice is the common case. A list in the record: a release id is any JSON value, and
    a list or an object cannot be hashed (`indexers.RefSet` tests them). `listed`, one merge's winners by `id`, keeps
    each winner's list and refs across its records: rebuilt per record, one hash under N ids was N²."""
    own = release_key(winner)
    kept = listed.get(id(winner))
    if kept is None:
        have = list(winner.get("also_from") or [])
        kept = listed[id(winner)] = (have, indexers.RefSet(release_key(a) for a in have if isinstance(a, dict)))
    have, refs = kept
    for ref in indexers.returned_by(other):
        if ref.indexer != own.indexer and ref not in refs:
            have.append(ref.as_dict())
            refs.add(ref)
    if have:
        winner["also_from"] = have


def _ask_indexer(ix, capability: str, releases: list[dict], log: Log, ctx, arg, budget_s: float, about: dict,
                 failures: list | None) -> list[dict] | None:
    """One add-on call, folded into `releases`: its answer (dicts only) tagged with the indexer's name
    and merged by infohash, the earlier record winning (`merge_releases`). None when the call failed
    (logged by `indexers.call`); the caller keeps what it had.

    The answer is normalised here, at the add-on boundary (`indexers.normalize_releases`): one field
    of the wrong type (`seeders` "12", `title` None, an ISO timestamp) used to raise later, outside the guarded call,
    and end the whole play as an internal error with every other add-on's releases. What was dropped, coerced or
    refused is one WARN line per call. The records the add-on handed to `query.reject` (the picker's rejected list)
    are normalised the same way, in place, and counted on that line under `rejected_records`."""
    found = indexers.call(ix, capability, log, ctx, arg, budget_s=budget_s, about=about, failures=failures)
    if found is None:
        return None
    found, changed = indexers.normalize_releases(found)
    rejected, rejected_changed = getattr(arg, "rejected", None), {}
    if rejected:
        kept_rejected, rejected_changed = indexers.normalize_releases([rel for rel, _, _ in rejected])
        kept_ids = {id(r) for r in kept_rejected}
        rejected[:] = [entry for entry in rejected if id(entry[0]) in kept_ids]
    if changed or rejected_changed:
        # FAIL when the answer had releases and none was kept; PARTIAL when usable ones came beside fixed or dropped.
        log.warn("indexers", f"{capability}.releases", result="FAIL" if changed and not found else "PARTIAL",
                 indexer=ix.name, **about, kept=len(found), **changed,
                 **({"rejected_records": {"kept": len(rejected), **rejected_changed}} if rejected_changed else {}),
                 **({"title_max": RELEASE_TITLE_MAX} if "cut" in changed or "cut" in rejected_changed else {}),
                 suggest=f"{ix.file} answered release fields of the wrong type or size (docs/addons.md: title and a "
                         f"40-hex info_hash required; counts, bytes and Unix seconds as numbers); fixed, cut or "
                         f"dropped")
    for r in found:
        r["indexer"] = ix.name
    return merge_releases(releases, found)


def _search_indexers(reg, http: Http, log: Log, settings: dict, query, rejected_sink: "RejectedSink | None",
                     failures: list | None = None) -> list[dict]:
    """One pass over the loaded indexers, in order. Each call gets its own copy of the query, with the
    hashes found so far as `known`; its releases are tagged with the indexer's name and folded into the
    union, and what it rejected reaches the picker only if the call finished inside its budget."""
    ctx = indexers.context(http, log, settings)
    budget_s = indexers.budget(settings)
    about = {"episode": query.episode, "pack_pass": query.pack_pass}
    releases: list[dict] = []
    for ix in reg.loaded:
        q = query.for_call(frozenset(str(r.get("info_hash") or "").lower() for r in releases))
        merged = _ask_indexer(ix, "episode_search", releases, log, ctx, q, budget_s, about, failures)
        if merged is None:
            continue
        releases = merged
        if rejected_sink is not None:
            # The part-named drops of this call, with its indexer, for `index_search`'s line.
            rejected_sink.part_drops.extend([ix.name, title, why] for title, why in q.part_drops)
            rejected_sink.entry_drops.extend([ix.name, title, code, why] for title, code, why in q.entry_drops)
            for rel, code, why in q.rejected:
                rejected_sink.add(rel, code, why, ix.name)          # once, by the hash read lower-case
    return releases


class RejectedSink(list):
    """`index_search`'s list of `(release, code, why)` an add-on rejected, which
    also remembers the indexer that rejected each (`indexer_of`, by the record's `id`), for the `index.search
    first_season` line. The records themselves are left as the add-on gave them. `part_drops`,
    `[indexer, title, why]` of each name an add-on's `query.matches` dropped as another show of the franchise while it
    states a part, whether or not the add-on handed it to `query.reject`, for the `index.search franchise_part` line.
    `entry_drops`, `[indexer, title, code, why]` of each name `query.matches` dropped as another entry of
    the franchise (`EpisodeQuery.entry_drops`), handed to `reject` or not, for the ambiguity guard.
    `pack_pass_drops`, the title of each pack the plugin itself dropped from the pack pass (`first_season_packs`), and
    `pack_pass_kept`, why it kept one it read or left unread. `unread_kept`,
    why `stated_season_check` kept a first-pass record stating season 2 or later unread (the show and episode test
    failed), for the same line's `kept`. A drop goes on it through `add`, once per release."""

    def __init__(self, items=()):
        super().__init__(items)
        self._hashes: set[str] = set()          # `add`'s keys of the first `_keyed` records
        self._keyed = 0
        self.indexer_of: dict[int, str] = {}
        self.part_drops: list[list] = []
        self.entry_drops: list[list] = []
        self.pack_pass_drops: list[str] = []     # titles the plugin dropped from the pack pass
        self.pack_pass_kept: dict[str, str] = {}  # title -> why the pack pass read it and kept it, or left it
        self.unread_kept: dict[str, str] = {}     # title -> why the first pass's record was kept unread

    # A change other than an append keys the list again at the next `add`.
    def _rekey(name):
        def changed(self, *args):
            out = getattr(list, name)(self, *args)
            self._hashes, self._keyed = set(), 0
            return out
        return changed
    __setitem__, __delitem__, insert, remove, pop, clear = map(
        _rekey, ("__setitem__", "__delitem__", "insert", "remove", "pop", "clear"))
    del _rekey

    def add(self, rel: dict, code: str, why: str, indexer: str | None = None) -> bool:
        """Put a release an add-on or the plugin dropped on the sink once, by its info hash read
        lower-case (`searches._hash`; one add-on may spell a torrent's hash in upper case and another in lower case),
        and remember the `indexer` that dropped it when one is given. Returns whether it was added; a release already on
        the sink keeps its first record, code and indexer. A release with no hash is keyed `""`, so only the first
        hashless drop is kept, as the lower-casing sites did: nothing else names its torrent. The plugin's
        own `franchise_show` and `compilation_film` drops (`first_season_parts`, `drop_compilation_films`) are appended
        without this test, as they always were: they are its own reading of a release an add-on may already have
        rejected under another code, and the ambiguity guard keys on their code (`guard_reads_drop`). Call `add` unless
        a drop must be on the sink even when its torrent already is.

        The hashes are kept in a set beside the list, so 40,000 drops add in linear time (each compared with every
        record before it was quadratic); records appended without `add` are keyed at the next `add`, and any
        other change (`sink[:] = …`, `remove`) keys the whole list again."""
        key = searches._hash(rel)
        for r, *_ in self[self._keyed:]:           # every record appended since the last call, by `add` or not
            self._hashes.add(searches._hash(r))
        self._keyed = len(self)
        if key in self._hashes:
            return False
        self.append((rel, code, why))
        if indexer is not None:
            self.indexer_of[id(rel)] = indexer
        return True


FIRST_SEASON_NAMES_LOGGED = 40          # names on one `index.search first_season` line
OTHER_ENTRY_NAMES_LOGGED = 40           # names on one `index.search compilation_film`/`spelling_only` line
FRANCHISE_PART_NAMES_LOGGED = 40        # names on one `index.search franchise_part` line
OVA_ENTRY_NAMES_LOGGED = 40             # names on one `index.search ova_entry` line
GUARD_SET_ASIDE_LOGGED = 25             # names on one `score.ambiguous check` line's `set_aside`
GUARD_DROPPED_LOGGED = 25               # siblings on one `score.ambiguous check` line's `from_dropped`
STATED_SEASON_NAMES_LOGGED = 40         # names on one `index.search stated_season` line


def guard_drops(sink: "RejectedSink") -> list[tuple[str, str, str]]:
    """`(title, code, why)` of each name `index_search` dropped that the ambiguity guard reads
    (`score.guard_reads_drop`): the sink's records (an add-on's `reject`, the plugin's own part and film drops) and every
    such drop `query.matches` made (`entry_drops`; an add-on may hand only `series` and `season` to `reject`), each title
    once, with the reason it was dropped (the guard's line names it)."""
    seen: dict[str, tuple[str, str]] = {}
    for rel, code, why in sink:
        if guard_reads_drop(code, why):
            seen.setdefault(str(rel.get("title") or ""), (code, why))
    for _ix, title, code, why in sink.entry_drops:
        seen.setdefault(str(title or ""), (code, why))
    return [(title, code, why) for title, (code, why) in seen.items()]


def first_season_packs(packs: list[dict], query: "indexers.EpisodeQuery", sink: RejectedSink,
                       first_pass: set) -> list[dict]:
    """The pack pass's records for a first-season entry (`query.first_season`), less each pack stating
    one season of 2 or more, whichever add-on returned it. An add-on's pack pass need not ask `query.matches`: a
    free-text add-on may keep every `is_pack` name, and the built-in Torznab add-on keeps a pack whose range misses the
    episode by asking again without the episode test, which is where `matches` asks the season. So the plugin reads
    every pack, as it reads a movie's (`packs_movie`), by `score.first_season_pack_reading`: its season only when the
    show test passes, so another show's pack is left as the add-on returned it. Each drop goes to the sink with code
    `season` and its add-on, so the `index.search first_season` line names it (`pack_pass`) and the picker lists it;
    each pack read and kept, or left unread, has its reason in `sink.pack_pass_kept` for that line's `kept`.
    Another show's pack is dropped before this reading on the pack pass (`pack_pass_show_check`), so the
    show-test skip (`FIRST_SEASON_SHOW_SKIP`) is reached only on the first pass (`stated_season_unread`).

    A record whose info hash the first pass returned (`first_pass`) is not read: the merge
    keeps the first pass's record, so the same release would be ranked and also listed as dropped."""
    kept = []
    for rel in packs:
        title = str(rel.get("title") or "")
        if not query._first_season_asked() or str(rel.get("info_hash") or "").lower() in first_pass:
            kept.append(rel)
            continue
        reading = first_season_pack_reading(title, query.variants, query.episode, query.alt_numbers, query.season,
                                            query.titles, (*query.variants, *query.first_season_titles))[1]
        if not reading.reason:
            kept.append(rel)
            if reading.why:
                sink.pack_pass_kept.setdefault(title, reading.why)
            continue
        sink.pack_pass_drops.append(title)
        sink.add(rel, "season", reading.reason, str(rel.get("indexer") or "?"))
    return kept


PACK_SHOW_NAMES_LOGGED = 40             # names on one `index.search pack_show` line
PACK_SHOW_REASON = ("the pack pass: a record of another show is dropped, whichever add-on returned it, by the show "
                    "test the built-in Torznab add-on asks a pack (the episode not asked); a record whose info hash "
                    "the first pass returned is left to the merge, which keeps the first pass's record")


def pack_pass_show_check(log: Log, packs: list[dict], query: "indexers.EpisodeQuery", sink: RejectedSink,
                         first_pass: set) -> list[dict]:
    """The pack pass's records, less each of another show. An add-on's pack pass need not ask
    `query.matches`: a free-text add-on may keep every `is_pack` name its pack query found, and a pack query for
    `<title> batch` may find other shows' packs too. The built-in Torznab add-on asks each pack `query.matches`, and a
    pack failing the episode test alone again without it (`score.release_matches_target(check_episode=False)`, the
    show and season tests); the plugin asks every pack-pass record that same call (`score.pack_show_reason`), with the
    entry's every title (`query.titles`), so a pack passing Torznab's keep passes here. The episode test is never
    asked: a pack's episode test is its range, which the prefilter reads, and `title_matches_episode` fails
    a range pack for an episode inside it (`Show S2 (01-12)` for episode 7).

    A drop goes to the sink with Torznab's code and reason (`series 0.41`, or the spelling-only reason), so the
    picker lists it as it lists Torznab's, and the ambiguity guard does not read it (`guard_reads_drop`: a `series`
    drop is no franchise evidence, on either pass). A record whose info hash the first pass returned is not asked: the
    merge keeps the first pass's record, which `stated_season_check` has read, so an episode-id search's
    record is never dropped here. One `index.search pack_show` line whenever the pack pass returned a record,
    naming each drop with its add-on and why."""
    kept, names, skipped = [], [], 0
    for rel in packs:
        title, ix = str(rel.get("title") or ""), str(rel.get("indexer") or "?")
        key = str(rel.get("info_hash") or "").lower()
        if key in first_pass:
            skipped += 1
            kept.append(rel)
            continue
        why = pack_show_reason(title, query.variants, query.episode, query.alt_numbers, query.season, query.titles)
        if not why:
            kept.append(rel)
            continue
        names.append([ix, title, why])
        sink.add(rel, why.split()[0], why, ix)
    if packs:
        log.info("index.search", "pack_show", result="PASS" if names else "EMPTY", episode=query.episode,
                 checked=len(packs) - skipped, first_pass=skipped, dropped=len(names),
                 names=names[:PACK_SHOW_NAMES_LOGGED], more=max(0, len(names) - PACK_SHOW_NAMES_LOGGED),
                 reason=PACK_SHOW_REASON)
    return kept


RELATED_ENTRY_NAMES_LOGGED = 40         # drops, and kept names, on one `index.search related_entry` line
RELATED_ENTRY_REASON = ("a movie: a release naming another entry of its franchise (two relation hops) without an "
                        "episode marker, which matched the movie only through titles that entry shares, is dropped "
                        f"after each pass, whichever add-on returned it ({RELATED_ENTRY_TAG})")


def related_entry_check(log: Log, releases: list[dict], query: "indexers.EpisodeQuery", related: tuple,
                        sink: RejectedSink, pack_pass: bool = False) -> list[dict]:
    """A movie's records, less each that names another entry of its franchise (`related`,
    `franchise_shows_for`) without an episode marker (`score.related_entry_reading`, with every title of the movie,
    `query.titles`, and its season-0 pair). An episode-id search's record never asks `query.matches` (one filed the
    franchise's earlier film, `[Grp] Harbor Song Movie - Lantern Road`, under the movie's episode), and the generic
    synonym every film of a franchise holds passes the show test, so the plugin asks every record: the first pass's
    after their merge, before the pack pass's gate counts them (as the stated-season check does), and the pack pass's
    before theirs.

    A drop goes to the sink with code `series` and its add-on, as the pack pass's show-test drops do: the picker lists
    it, and the ambiguity guard does not read it (`guard_reads_drop`), so a dropped film of the franchise is no sibling.
    One `index.search related_entry` line per pass whenever a movie with related entries returned a record: each drop as
    `[add-on, title, why]` (`why` names the related entry and the movie title it matched through) and, under `kept`,
    each record holding a related entry's title in full that stayed, with why."""
    if not (query.movie and related and releases):
        return releases
    kept, names, near = [], [], []
    for rel in releases:
        title, ix = str(rel.get("title") or ""), str(rel.get("indexer") or "?")
        reading = related_entry_reading(title, query.titles or query.variants, related, query.movie_pairs)
        if reading.reason:
            names.append([ix, title, reading.reason])
            sink.add(rel, reading.reason.split()[0], reading.reason, ix)
            continue
        kept.append(rel)
        if reading.why:
            near.append([ix, title, reading.why])
    log.info("index.search", "related_entry", result="PASS" if names else "EMPTY", episode=query.episode,
             on="pack pass" if pack_pass else "first pass", checked=len(releases), dropped=len(names),
             names=names[:RELATED_ENTRY_NAMES_LOGGED], more=max(0, len(names) - RELATED_ENTRY_NAMES_LOGGED),
             kept=near[:RELATED_ENTRY_NAMES_LOGGED], kept_more=max(0, len(near) - RELATED_ENTRY_NAMES_LOGGED),
             related=sorted({r[0] for r in related})[:FRANCHISE_IDS_LOGGED], reason=RELATED_ENTRY_REASON)
    return kept


# Why a first season's record stating season 0 (no reading reads it) is kept
SEASON_ZERO_KEPT = ("states season 0, which the first-season reading does not read (it reads 2 and later; a stated "
                    "season 0 is a known gap)")


def stated_season_unread(rel: dict, title: str, query: "indexers.EpisodeQuery", pack: bool):
    """The first-season reading of a first-pass record, asked as `query.matches` asks
    it, only of a name the show and episode test passes: `(reading, "")`, or `(None, why)` when that test fails and the
    season is not read. A single is asked `score.release_matches_target` (the query's variants, episode, alt numbers,
    season and titles). A pack is asked `score.first_season_pack_reading`, the pack pass's own (its show test without
    the episode: a pack's episode test is its range, `release_matches_target`'s `check_episode=False`), and then its
    range (`score.pack_range_excludes`, the prefilter's `pack range`). The reading itself is the shared
    `score.first_season_reading`, every title (`first_season_titles`), the pack reading for a pack."""
    own = (*query.variants, *query.first_season_titles)
    if pack:
        shown, reading = first_season_pack_reading(title, query.variants, query.episode, query.alt_numbers,
                                                   query.season, query.titles, own)
        if not shown:
            return None, reading.why or FIRST_SEASON_SHOW_SKIP
        if pack_range_excludes(rel, parse_title(title, query.season, query.titles), query.episode, query.alt_numbers):
            return None, (f"its range misses episode {query.episode}: its season is not read (the prefilter drops it "
                          f"as `pack range`)")
        return reading, ""
    shown, why = release_matches_target(title, query.variants, query.episode, query.alt_numbers,
                                        target_season=query.season, titles=query.titles)
    if not shown:
        return None, f"the show or episode test fails ({why}): its season is not read"
    return first_season_reading(title, own), ""


def stated_season_check(log: Log, releases: list[dict], query: "indexers.EpisodeQuery",
                        sink: RejectedSink) -> tuple[list[dict], dict]:
    """The first pass's records, merged, less each single or pack of the entry's show and episode stating
    a season 2 or later for a first-season entry (`query.first_season`), whichever add-on returned it. An episode-id
    search (an add-on that finds releases by the episode's id) never asks `query.matches`, where the first-season
    readings read the season, and `merge_releases` keeps its record although a free-text add-on dropped the same hash.
    Its linkage answers the show, not the season (a recorded `[<Grp>] <Show> S02E03 … (<Show> Season 2)` for a season-1
    entry). So the plugin asks every record what `matches` asks before the season: the show and episode test, then the
    one stated-season reading (`stated_season_unread`). A record an add-on kept by `matches` passed both, so only one
    it kept unasked can be dropped here. Each drop goes to the sink with code `season` and its add-on, as the
    first-season reading's do (the picker lists it, the `index.search first_season` line names it, the ambiguity guard
    reads it). Not for a movie or a one-episode entry (`_first_season_asked`), as the first-season reading is not.

    A record that fails the show and episode test is kept as its add-on returned
    it, its season unread, and is never handed on as this entry's other season, which the guard would read: a dead or
    out-of-range other-season name is left to the prefilter. A record stating season 0 is logged kept
    (`SEASON_ZERO_KEPT`).

    An entry whose titles state a season (`query.season`, not a movie or a one-episode entry) is not asked the add-ons'
    season test here: its records stating another season are logged kept, with why. Measured: the
    two such records that are right are a later entry's own episode by another count (a season-4 entry's `<Show>
    S01E58`, TVDB's season 1, and `<Show> <Arc> - S01E22`), which that test would drop. One `index.search
    stated_season` line when a record states a season other than the entry's, naming each with its add-on, the
    decision and the reading, and the rule asked in `reason`. Returns the records kept and the dropped ones by info
    hash (`{hash: [add-on, title]}`), so the pack pass cannot merge the same release back."""
    first = query._first_season_asked()
    own = (*query.variants, *query.first_season_titles)
    kept, names, near, gone = [], [], [], {}
    for rel in releases:
        title, ix = str(rel.get("title") or ""), str(rel.get("indexer") or "?")
        if first:
            if query.first_season_read(title) is None:       # the one stated-season gate
                if first_season_stated(title, own)[0] == 0:
                    near.append([ix, title, SEASON_ZERO_KEPT])
                kept.append(rel)
                continue
            reading, unread = stated_season_unread(rel, title, query, query.is_pack(title))
            if reading is None:
                why = f"{unread}; {ix}'s record, kept as its add-on returned it"
                near.append([ix, title, why])
                sink.unread_kept.setdefault(title, why)
                kept.append(rel)
                continue
            if reading.reason:
                names.append([ix, title, f"{reading.reason}; {ix}'s record, kept by its add-on without query.matches"])
                key = str(rel.get("info_hash") or "").lower()
                gone[key] = [ix, title]
                sink.add(rel, "season", reading.reason, ix)
                continue
            near.append([ix, title, reading.why])
        elif query.season is not None and not (query.movie or query.single_episode):
            stated = season_number(title)
            if stated is not None and stated != query.season:
                near.append([ix, title, f"states season {stated}, the entry's titles {query.season}: "
                                        + ("a pack, which the add-ons' season test exempts" if query.is_pack(title) else
                                           "the add-ons' season test is not asked of a record after the merge")])
        kept.append(rel)
    if names or near:
        log.info("index.search", "stated_season", result="PASS" if names else "EMPTY", episode=query.episode,
                 season=query.season, first_season=first, checked=len(releases), dropped=len(names),
                 names=names[:STATED_SEASON_NAMES_LOGGED], more=max(0, len(names) - STATED_SEASON_NAMES_LOGGED),
                 kept=near[:STATED_SEASON_NAMES_LOGGED], kept_more=max(0, len(near) - STATED_SEASON_NAMES_LOGGED),
                 reason=STATED_SEASON_FIRST if first else STATED_SEASON_LOG_ONLY)
    return kept, gone


# The `index.search stated_season` line's `reason`, by the rule it asked.
STATED_SEASON_FIRST = ("a first season: every add-on's first-pass record is asked the show and episode test and then "
                       "the stated-season reading, as query.matches asks them; one an add-on kept without "
                       "query.matches whose name states season 2 or later is dropped, one failing the test is kept "
                       "unread")
STATED_SEASON_LOG_ONLY = ("an entry whose titles state a season: a record stating another season is logged kept; "
                          "the add-ons' season test is not asked of a record after the merge")


def first_season_parts(log: Log, releases: list[dict], query: "indexers.EpisodeQuery", read: Callable[[], tuple],
                       sink: "RejectedSink") -> tuple[list[dict], "indexers.EpisodeQuery | None", list[list]]:
    """A first season's search without another part's singles. "First season" is the entry identity's: no sequel
    verdict (`_resolve`'s `lazy_first`), not the first-season reading's `first_season` (ani.zip's S1E1). After the first
    pass, when a returned name states a part before its episode marker that none of the entry's titles state
    (`score.extra_parts`, the question the `franchise_part` reading asks first), the franchise is read (`read`,
    `_resolve`'s `franchise_shows_for`: its cache, miss marker and WARN) and each such name is asked the stated-part
    reading alone (`EpisodeQuery.franchise_reading(parts_only=True)`), of every add-on's records, as
    `drop_compilation_films` asks after the merge. No such name: AniList is not asked. A drop goes to `sink` as
    `franchise_show` (not a picker row; `picked_release` finds a viewer's earlier pick of it) and to `sink.part_drops`,
    which the `index.search franchise_part` line names. Returns the releases kept, the query the reading asked (None
    when it was not asked), and `near`: `[indexer, title, why]` of each other returned name whose words touch a
    franchise title, with why it stayed (a name that is exactly another show's title, `FIRST_SEASON_EXACT`; logging
    only, no read). One `index.search first_season_parts` line either way, naming the parts that asked the read."""
    own = title_parts((*query.variants, *query.titles))
    extra = [(r, extra_parts(str(r.get("title") or ""), own, query.episode, query.alt_numbers)) for r in releases]
    named = [[r.get("indexer"), str(r.get("title") or ""), sorted(p)] for r, p in extra if p]
    log.info("index.search", "first_season_parts", result="PASS" if named else "SKIP", episode=query.episode,
             checked=len(releases), stating=named[:FRANCHISE_PART_NAMES_LOGGED],
             stating_more=max(0, len(named) - FRANCHISE_PART_NAMES_LOGGED),
             reason=("a first season: a returned name states a part before its episode marker that none of the "
                     "entry's titles state, so the franchise is read for the stated-part reading"
                     if named else "a first season: no returned name states a part before its episode marker that "
                     "none of the entry's titles state, so AniList is not asked for the franchise"))
    if not named:
        return releases, None, []
    shows = tuple(read() or ())
    if not shows:
        return releases, None, []
    parts = dataclasses.replace(query, franchise_shows=shows, part_drops=[])
    kept, near = [], []
    for rel, stated in extra:
        title = str(rel.get("title") or "")
        reading = parts.franchise_reading(title, parts_only=True)
        if stated and reading is not None and reading.reason:
            sink.append((rel, FRANCHISE_SHOW, reading.reason))
            sink.indexer_of[id(rel)] = rel.get("indexer")
            sink.part_drops.append([rel.get("indexer"), title, reading.reason])
            continue
        if not stated and reading is not None and reading.near:
            near.append([rel.get("indexer"), title, reading.why])
        kept.append(rel)
    # Another show's exact title first: the 40 names the line holds are for "why wasn't it dropped?".
    near.sort(key=lambda n: FIRST_SEASON_EXACT not in str(n[2]))
    return kept, parts, near


def drop_compilation_films(log: Log, releases: list[dict], episode: int, alt: list[int], variants: list[str],
                           sink: "RejectedSink") -> list[dict]:
    """A TV entry's search without the releases that are a film cut of the show
    (`score.compilation_film_reading`; `variants` here is every title of the entry, so a title naming a film keeps its
    own releases). Asked after the merge, of every add-on's records: an episode-id search keeps a film filed under the
    arc's first episode (`Nekoyomigatari (White) the Movie` for Yomigatari Second Season's episode 1) without asking
    `query.matches`. Each drop goes to `sink` (code `compilation_film`, not a picker row, as the franchise reading's are
    not), so a viewer's earlier pick of it is found again. One `index.search compilation_film` line when a name carries
    a film word, with each drop and each such name kept and why."""
    kept, films, near = [], [], []
    for r in releases:
        title = str(r.get("title") or "")
        reading = compilation_film_reading(title, episode, alt, variants)
        if reading.reason:
            films.append([r.get("indexer"), title, reading.reason])
            sink.append((r, COMPILATION_FILM, reading.reason))
            sink.indexer_of[id(r)] = r.get("indexer")
            continue
        kept.append(r)
        if reading.why:
            near.append([r.get("indexer"), title, reading.why])
    if films or near:
        log.info("index.search", "compilation_film", result="PASS" if films else "EMPTY", episode=episode,
                 dropped=len(films), names=films[:OTHER_ENTRY_NAMES_LOGGED],
                 more=max(0, len(films) - OTHER_ENTRY_NAMES_LOGGED), kept=near[:OTHER_ENTRY_NAMES_LOGGED],
                 kept_more=max(0, len(near) - OTHER_ENTRY_NAMES_LOGGED),
                 reason="a TV entry's episode: a release that names a film and states no episode is a film cut of the "
                        "show, not the episode")
    return kept


def index_search(http: Http, log: Log, ids: dict, episode: int, variants: list[str], series_title: str, settings: dict,
                 rejected_sink: list | None = None, reg=None, movie: bool = False,
                 movie_pairs: Iterable[tuple[int, int]] = (), single_episode: bool = False,
                 related_shows: tuple = (), season: int | None = None, franchise_shows: tuple = (),
                 first_season: bool = False, first_season_titles: tuple = (), titles: tuple = (),
                 prequel_shows: tuple = (), first_season_franchise: Callable[[], tuple] | None = None,
                 tv_series: bool = False) -> list[dict]:
    """Every loaded indexer add-on's releases for this episode, keyed by infohash.
    `reg` is a registry this request already loaded (`releases`); else the folder is loaded here.

    `season` is the season this AniList entry is, as `entry_season` read it from the entry's titles,
    or None when no title states one. The filter (`query.matches`) rejects only an explicit disagreement —
    a release that names no season stays compatible with every season, and a pack is exempt. Measured when it
    shipped (corrected 2026-09-19): the asymmetric rule closes 2 of 378 items, loses 0 positives.

    `rejected_sink` gets `(release, code, why)` for each release an add-on rejects for naming
    another show or season, for the release picker. When fewer than three seeded releases came
    back and packs are allowed, a second pass asks the indexers for packs. A seeded pack whose range
    excludes the episode is not one of the three (`pack_pass_decision`); `index.search packs` logs the decision either
    way.

    `movie_pairs` is a movie's ani.zip season-0 pair (`anizip.movie_pairs`), the one episode marker the
    movie's own release may state; an add-on reads it as `query.movie_pairs`, and `query.matches` checks it. The pack
    pass of a movie drops every pack that states another episode (`Seasons 1-4`, `Batch 1-26`): an add-on keeps any
    pack there, and `pick_episode_file` would take the TV series' episode 1 from it.

    `single_episode` (`one_episode_entry`) reaches the add-ons as `query.single_episode`: the entry's
    releases need not say episode 1, and `query.matches` reads them by the entry's titles instead. Its
    `related_shows` (`related_shows_for`) reach them as `query.related_shows`, whose episodes `query.matches` drops.
    Its `prequel_shows` (`prequel_shows_for`) reach them as `query.prequel_shows`: `query.matches` keeps a
    name numbered on from one of them.

    A later-season entry's `franchise_shows` (`franchise_shows_for`) reach them as
    `query.franchise_shows` (not for a movie or a one-episode entry): `query.matches` drops a single whose words are
    exactly one of those shows' titles (`franchise_show`), the earlier cour's `Show - 01` that names no season.
    A first season's shows are not read before the search. `first_season_franchise` (`_resolve`'s
    `franchise_shows_for`) is called after the first pass only when a returned name states a part none of the entry's
    titles state (`first_season_parts`), and then only the stated-part reading drops, of every add-on's records.

    `first_season` (`first_season_for`: ani.zip files the entry as TVDB season 1 and no title states a
    season) reaches them as `query.first_season`, with every title of the entry (`first_season_titles`,
    `entry_titles`): `query.matches` drops a single stating season 2 or later unless a title ends in that season. One
    `index.search first_season` line names each drop an add-on handed to `query.reject` (the `season` code
    the season test's own drops carry) as `[indexer, title, why]`, and each returned name stating such a season with why
    it stayed (`kept`: a pack of several seasons, or a title's number). An add-on that drops without `reject` is not
    seen there. A pack stating one season of 2 or more is dropped too, by `matches` and, on the pack pass, by
    the plugin (`first_season_packs`). And after the first pass, of every add-on's records that pass the
    show and episode test (`stated_season_check`), so an episode-id search that never asks `matches` keeps no other
    season's name of the entry's episode.

    Every title of the entry (`titles`, `entry_titles`) reaches the add-ons as `query.titles`, which the
    show test reads to keep a name it passed by spelling alone; a name sharing a word with none of them is another show
    (`Hanayomigatari 1-5` for Yomigatari Second Season, whose synonym `Kabukiyomigatari` it is spelt like). After the
    merge, a TV entry's search drops a film cut of the show (`drop_compilation_films`), whichever add-on returned it.

    The pack pass's records, whichever add-on returned them, are asked the show test the built-in Torznab
    add-on asks a pack (`pack_pass_show_check`, the episode not asked), so another show's pack an add-on kept without
    `matches` is dropped (`series`, a picker row); a record whose hash the first pass returned is left to the merge.

    For a movie, `franchise_shows` are its related entries (`franchise_shows_for` reads them for a movie
    too), never the later-season reading's `query.franchise_shows`: after each pass, a record of any add-on naming one
    of them without an episode marker, which matched the movie only through titles that entry shares, is dropped
    (`related_entry_check`).

    With no add-on loaded it stops before asking anything, at `indexers.NONE_STAGE`
    (`indexers.require`), rather than returning nothing and failing as "no releases found"."""
    t0 = time.monotonic()
    reg = reg if reg is not None else indexers.load(settings, log)
    indexers.require(reg, log)                               # none loaded is not "no releases"
    alt = ids.get("alt_numbers") or []
    packs_allowed = as_bool(settings.get("allowBatches"), True)          # "false" is False
    query = indexers.EpisodeQuery(ids=dict(ids), episode=episode, variants=list(variants), series_title=series_title,
                                  alt_numbers=list(alt), season=season, packs_allowed=packs_allowed,
                                  movie=bool(movie),                   # `entry_format`'s answer, not a new rule
                                  movie_pairs=frozenset(movie_pairs or ()) if movie else frozenset(),
                                  single_episode=bool(single_episode) and not movie,
                                  related_shows=tuple(related_shows or ()) if single_episode and not movie else (),
                                  franchise_shows=tuple(franchise_shows or ()) if not (single_episode or movie) else (),
                                  first_season=bool(first_season),
                                  first_season_titles=tuple(first_season_titles or ()) if first_season else (),
                                  titles=tuple(titles or ()),
                                  prequel_shows=tuple(prequel_shows or ()) if single_episode and not movie else (),
                                  tv_series=bool(tv_series))        # `tv_series_entry` is the one gate
    failures: list = []
    # The drops are read back from the sink, with the indexer of each, so one is kept even when the
    # caller passes none; the caller's list gets the same entries at the end.
    sink = RejectedSink(rejected_sink or ())
    releases = _search_indexers(reg, http, log, settings, query, sink, failures)
    if not releases and failures:
        raise indexers.failed(failures)                      # nothing found because add-ons failed is that failure
    # Before the pack pass's gate counts them, so a dropped record is not one of the three playable releases.
    releases, unasked_drops = stated_season_check(log, releases, query, sink)
    # A movie's related entries (`franchise_shows_for`'s answer, never `query.franchise_shows`), before the
    # pack pass's gate.
    related_entries = tuple(franchise_shows or ()) if movie else ()
    releases = related_entry_check(log, releases, query, related_entries, sink)
    parts_query, near = None, []                             # what a first season's part reading asked
    if first_season_franchise is not None and not (movie or single_episode or query.franchise_shows):
        releases, parts_query, near = first_season_parts(log, releases, query, first_season_franchise, sink)
    few, counted, out_of_range = pack_pass_decision(releases, episode, alt, season, variants)
    run = packs_allowed and few and bool(variants)
    # The decision is logged both ways, with what counted and the seeded packs that did not.
    log.info("index.search", "packs", result="PASS" if run else "SKIP", decision="ran" if run else "skipped",
             reason=("allowBatches is off" if not packs_allowed else
                     f"{len(counted)} seeded releases can play the episode, at least {PACK_PASS_MIN_PLAYABLE}"
                     if not few else
                     "no title to search packs by" if not variants else
                     f"fewer than {PACK_PASS_MIN_PLAYABLE} seeded releases can play the episode"),
             episode=episode, counted=len(counted), threshold=PACK_PASS_MIN_PLAYABLE,
             # A counted release the title reading takes for a pack says which pattern (`[…, pack_by]`).
             names=[[r.get("seeders"), r.get("title"), *([by] if by else [])] for r, by in counted[:PACK_NAMES_LOGGED]],
             more=max(0, len(counted) - PACK_NAMES_LOGGED),
             out_of_range=[[r.get("seeders"), r.get("title"), [list(x) for x in ranges]]
                           for r, ranges in out_of_range[:PACK_NAMES_LOGGED]],
             out_of_range_more=max(0, len(out_of_range) - PACK_NAMES_LOGGED),
             indexers=[i.name for i in reg.loaded])
    first_pass = {str(r.get("info_hash") or "").lower() for r in releases}    # which pass kept a name
    if run:
        query.pack_pass = True
        packs = _search_indexers(reg, http, log, settings, query, sink)
        # A release the first pass's records were dropped as (its info hash) is not merged back from the pack
        # pass; it is on the sink already.
        # Each as [add-on, title, the first-pass record's add-on and title it hit]
        again = [[str(p.get("indexer") or "?"), str(p.get("title") or ""), *unasked_drops[h]] for p in packs
                 for h in [str(p.get("info_hash") or "").lower()] if h in unasked_drops]
        if again:
            packs = [p for p in packs if str(p.get("info_hash") or "").lower() not in unasked_drops]
            log.info("index.search", "stated_season_again", result="PASS", episode=episode, dropped=len(again),
                     names=again[:STATED_SEASON_NAMES_LOGGED],
                     reason="the pack pass returned a release the first pass's stated-season check dropped; "
                            "it is not merged back")
        # Another show's pack, whichever add-on returned it, before any reading of the entry's own packs.
        packs = pack_pass_show_check(log, packs, query, sink, first_pass)
        packs = related_entry_check(log, packs, query, related_entries, sink, pack_pass=True)  # related entries
        if movie:
            kept, gone = [], []
            for rel in packs:
                reason = movie_episode_reason(str(rel.get("title") or ""), query.movie_pairs)
                (gone if reason else kept).append([str(rel.get("title") or ""), reason] if reason else rel)
            log.info("index.search", "packs_movie", result="PASS" if kept else "EMPTY", kept=len(kept),
                     movie_episode=len(gone), movie_pairs=[list(p) for p in sorted(query.movie_pairs)])
            if gone:
                log.debug("index.search", "packs_movie", reason="movie_episode", count=len(gone),
                          titles=gone[:25])
            packs = kept
        if query.first_season:
            packs = first_season_packs(packs, query, sink, first_pass)
        releases = merge_releases(releases, packs)
    if not (movie or single_episode):
        releases = drop_compilation_films(log, releases, episode, alt, tuple(titles or ()) or variants, sink)
    # Each name an add-on dropped because the show test passed it by spelling alone, with the reason,
    # read back from the sink (`query.reject`) as the first-season line is; and each returned name that shares no word
    # with any title, with the clause that kept it.
    spelled = [[sink.indexer_of.get(id(rel)), str(rel.get("title") or ""), why] for rel, code, why in sink
               if code == "series" and OTHER_ENTRY_TAG in str(why or "")]
    spelled_kept = [[r.get("indexer"), title, why] for r in releases for title in [str(r.get("title") or "")]
                    for why in [spelling_reading(title, variants, titles).why] if why]
    if spelled or spelled_kept:
        log.info("index.search", "spelling_only", result="PASS" if spelled else "EMPTY", episode=episode,
                 dropped=len(spelled), names=spelled[:OTHER_ENTRY_NAMES_LOGGED],
                 more=max(0, len(spelled) - OTHER_ENTRY_NAMES_LOGGED), kept=spelled_kept[:OTHER_ENTRY_NAMES_LOGGED],
                 kept_more=max(0, len(spelled_kept) - OTHER_ENTRY_NAMES_LOGGED),
                 reason="a name the show test passed by spelling alone, sharing no word with any of the entry's "
                        "titles, is another show")
    if rejected_sink is not None:
        rejected_sink[:] = sink
        if isinstance(rejected_sink, RejectedSink):           # the guard's record of the franchise drops
            rejected_sink.entry_drops[:] = sink.entry_drops
    if query.first_season:
        drops = [[sink.indexer_of.get(id(rel)), str(rel.get("title") or ""), why] for rel, _, why in sink
                 if FIRST_SEASON_TAG in str(why or "")]
        kept = [[r.get("indexer"), title, why] for r in releases
                for title in [str(r.get("title") or "")]
                for why in [sink.pack_pass_kept.get(title) or sink.unread_kept.get(title)
                            or query.first_season_kept(title)] if why]
        log.info("index.search", "first_season", result="PASS" if drops else "EMPTY", episode=episode,
                 dropped=len(drops), names=drops[:FIRST_SEASON_NAMES_LOGGED],
                 more=max(0, len(drops) - FIRST_SEASON_NAMES_LOGGED),
                 kept=kept[:FIRST_SEASON_NAMES_LOGGED], kept_more=max(0, len(kept) - FIRST_SEASON_NAMES_LOGGED),
                 # which of `names` the plugin dropped from the pack pass, not an add-on
                 pack_pass=sink.pack_pass_drops[:FIRST_SEASON_NAMES_LOGGED],
                 reason="ani.zip files the entry as TVDB season 1 and its titles state none: a single, or a pack of "
                        "one season, stating season 2 or later is another season's unless one of the entry's "
                        "titles ends in that season")
    part_query = query if query.franchise_shows else parts_query
    parts_only = part_query is not query                     # a first season's reading, the part alone
    if part_query is not None:
        # Each name `query.matches` dropped as another show of the franchise while it states a part,
        # whichever add-on asked (`sink.part_drops`: an add-on may count the drop without `query.reject`), and each
        # returned name stating a part, with why it stayed (a name its add-on kept without
        # asking `matches` is named with its indexer and the pass that returned it).
        parts = list({(i, t): [i, t, w] for i, t, w in sink.part_drops}.values())
        kept = [[r.get("indexer"), title, why] for r in releases for title in [str(r.get("title") or "")]
                for why in [part_query.part_kept(title, r.get("indexer"),
                                                 str(r.get("info_hash") or "").lower() not in first_pass,
                                                 parts_only=parts_only)] if why] + near
        if parts or kept:
            log.info("index.search", "franchise_part", result="PASS" if parts else "EMPTY", episode=episode,
                     dropped=len(parts), names=parts[:FRANCHISE_PART_NAMES_LOGGED],
                     more=max(0, len(parts) - FRANCHISE_PART_NAMES_LOGGED),
                     kept=kept[:FRANCHISE_PART_NAMES_LOGGED], kept_more=max(0, len(kept) - FRANCHISE_PART_NAMES_LOGGED),
                     reason=(FIRST_SEASON_PARTS if parts_only else
                             "a later season: a single stating a part none of the entry's titles state is the "
                             f"franchise show that is one of those titles plus that part ({FRANCHISE_PART_TAG})")
                            + ", and `Part 2 - 10` is a single, not the range 2-10")
    if query.tv_series:
        # Each single a TV series dropped as an OVA, whichever add-on
        # asked (`sink.entry_drops`: an add-on may count the drop without `query.reject`, and its reason is the OVA
        # reading's only here), with the indexer and why, as the `franchise_part` line reads its drops.
        ovas = list({(i, t): [i, t, w] for i, t, code, w in sink.entry_drops
                     if code == FRANCHISE_SHOW and str(w or "").endswith(f"({OVA_ENTRY_TAG})")}.values())
        log.info("index.search", "ova_entry", result="PASS" if ovas else "EMPTY", episode=episode, dropped=len(ovas),
                 names=ovas[:OVA_ENTRY_NAMES_LOGGED], more=max(0, len(ovas) - OVA_ENTRY_NAMES_LOGGED),
                 reason="a TV series: a single stating OVA, OAV or OAD before its episode marker, which no title of "
                        f"the entry holds, is another entry of the franchise ({OVA_ENTRY_TAG})")
    by_indexer: dict[str, int] = {}
    for r in releases:
        by_indexer[r.get("indexer") or "?"] = by_indexer.get(r.get("indexer") or "?", 0) + 1
    log.info("index.search", "merged", result="PASS" if releases else "EMPTY", ms=(time.monotonic() - t0) * 1000,
             total=len(releases), by_indexer=by_indexer,
             also_from=sum(1 for r in releases if r.get("also_from")),    # returned by more than one
             # The titles the show test read, and where they came from.
             titles=len(set(variants) | set(titles or ())),
             titles_from={"title_variants": len(variants), "anilist_titles": len(set(titles or ()) - set(variants))})
    return releases


_PUBLIC_KEYS = ("id", "indexer", "title", "info_hash", "seeders", "total_size", "num_files", "score", "terms",
                "verified_langs", "has_lang", "tracks_status", "rd_cached", "file", "source")


def public_record(cand: dict) -> dict:
    """A release as the resolve's JSON shows it: its public keys only."""
    return {k: cand.get(k) for k in _PUBLIC_KEYS}


def public_candidates(ranked: list[dict]) -> list[dict]:
    return [public_record(c) for c in ranked[:10]]


# TorBox torrent-info lookups per play, the best-ranked unknown packs first. They run side by
# side, so a play waits at most one `torrentInfoSeconds` (plus the socket's slack) for all of them.
TORRENT_INFO_MAX_PACKS = 4


def _set_files(cand: dict, files: list[dict], complete: bool, source: str) -> None:
    """A pack's file list and what it proves: `files_complete` (only a whole list may drop the pack) and
    `files_from` ("provider", "indexer", "torrentinfo"; the `score.file_pick` lines carry it)."""
    cand["files"], cand["files_complete"], cand["files_from"] = files, complete, source


def _take_files(cand: dict, got: dict | None, source: str) -> bool:
    """Take a file-list answer (`FILES_*`) onto the candidate if it holds a list. True when it did."""
    if not got or got.get("status") not in (FILES_COMPLETE, FILES_INCOMPLETE) or not got.get("files"):
        return False
    _set_files(cand, got["files"], got["status"] == FILES_COMPLETE, source)
    return True


def learn_pack_files(ranked: list[dict], http: Http, log: Log, settings: dict,
                     file_source: "indexers.FileLists | None", picked: bool = False,
                     guard: "BlockGuard | None" = None) -> None:
    """The file list of each pack that has none yet, from the add-on that returned it (the
    capability **file list per release**). A list the provider already gave (TorBox's `checkcached`, for
    a cached pack) is kept: it is the torrent itself and carries sizes. Sets `files`, `files_complete`
    (only a complete list may drop a pack) and `files_from`.

    When no loaded add-on offers file lists, TorBox's torrent info reads an uncached pack's list from the
    BitTorrent network without adding it (`torrent_info_files`)."""
    unknown = []
    for cand in ranked:
        if not (cand.get("parsed") or {}).get("batch"):
            continue
        if cand.get("files"):
            _set_files(cand, cand["files"], True, "provider")      # TorBox's `checkcached`: the torrent itself
            continue
        if not _take_files(cand, file_source.read(cand) if file_source is not None else None, "indexer"):
            unknown.append(cand)
    if unknown:
        torrent_info_files(unknown, http, log, settings, file_source, picked, guard)


def torrent_info_files(packs: list[dict], http: Http, log: Log, settings: dict,
                       file_source: "indexers.FileLists | None", picked: bool = False,
                       guard: "BlockGuard | None" = None) -> None:
    """The provider's side of the file-list fallback for packs nothing has listed. Asked only
    when no loaded add-on offers file lists, the lookup is on (`torrentInfoSeconds` > 0) and TorBox is one of
    the viewer's providers with a token: a release's hash goes to TorBox only if the viewer chose TorBox. A
    lookup that times out or fails leaves the pack unknown, as before.

    Never for a release the viewer picked (a pin, or the release picker): a complete list without the
    episode drops the pack, and for a pin that drops the pin (`play_pinned`). The file matcher misses names
    it cannot read, so only the provider that plays the pack may prove it dead."""
    seconds = settings["torrentInfoSeconds"]          # coerced and bounded by `core.coerce_settings`
    why = ""
    if picked:
        why = "the viewer picked this release (a pin or the release picker); only the provider that plays it may prove it lacks the episode"
    elif file_source is not None and file_source.offered:
        why = f"indexer add-ons list files ({', '.join(file_source.names)}); their answer stands"
    elif seconds <= 0:
        why = "torrentInfoSeconds is 0"
    elif guard is not None and guard.remembered("torbox") is not None:
        why = "TorBox refused this account recently (provider block guard); nothing is sent to it"
    elif not torbox_usable(settings):
        why = "TorBox is not one of the providers with a token, so no release hash is sent to it"
    if why:
        log.info("score.file_list", "torrentinfo", result="SKIP", packs=len(packs), reason=why)
        return
    asked = packs[:TORRENT_INFO_MAX_PACKS]
    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(asked)) as pool:
        answers = list(pool.map(lambda c: torbox_torrent_info(http, log, c.get("info_hash"), seconds), asked))
    n = sum(_take_files(cand, got, "torrentinfo") for cand, got in zip(asked, answers))
    log.info("score.file_list", "torrentinfo", result="PASS" if n else "EMPTY",
             ms=(time.monotonic() - t0) * 1000, asked=len(asked), learned=n,
             not_asked=len(packs) - len(asked), timeout_s=seconds,
             reason=f"at most {TORRENT_INFO_MAX_PACKS} packs per play, best-ranked first")


def pick_pack_files(ranked: list[dict], log: Log, settings: dict, lang: str,
                    rejected_sink: list | None = None) -> list[dict]:
    """For each pack whose file list is known, pick the episode's file and rescore on its size.
    Packs whose list is unknown stay (a provider lists the files when it adds the torrent); packs whose
    complete list does not contain the episode are dropped here. A partial list can pick a file but drops
    nothing. No I/O: the lists were learned before, by `learn_pack_files`."""
    kept = []
    no_identity = sum(1 for c in ranked if (c.get("parsed") or {}).get("batch") and "entry" not in c)
    if no_identity:
        log.info("score.file_pick", "identity", result="SKIP", packs=no_identity,
                 reason="no entry identity on these packs: their file is picked by its number alone")
    stats = load_group_stats()
    static = static_group_priors(settings)
    for cand in ranked:
        if not (cand.get("parsed") or {}).get("batch"):
            kept.append(cand)
            continue
        files = cand.get("files")
        if not files:
            log.info("score.file_pick", "unknown", id=cand["id"], title=cand["title"],
                     reason="file list not known until a provider adds the torrent")
            kept.append(cand)
            continue
        skipped: list = []
        picked = pick_episode_file(files, cand["episode"], cand.get("alt"), cand.get("entry"), cand.get("title") or "",
                                   skipped)
        passed_over = skipped_summary(skipped, cand.get("entry"))
        if not picked and not cand.get("files_complete", True):
            log.info("score.file_pick", "partial", result="SKIP", id=cand["id"], title=cand["title"],
                     files=len(files), source=cand.get("files_from"), **passed_over,
                     reason="the known part of the pack's file list has no file for this episode; kept, "
                            "the provider lists the rest when it adds the torrent")
            kept.append(cand)
            continue
        if not picked:
            # A `number_not_episode` skip is a file read by its episode marker, not an identity refusal;
            # nor is a `season_relative` one, a file ani.zip's pair says is another episode.
            identity_refused = any(s["code"] not in (FILE_NUMBER_NOT_EPISODE, FILE_SEASON_RELATIVE) for s in skipped)
            season_relative = any(s["code"] == FILE_SEASON_RELATIVE for s in skipped)
            log.warn("score.file_pick", "miss", result="FAIL", id=cand["id"], title=cand["title"], files=len(files),
                     source=cand.get("files_from"), **passed_over,
                     code="pack_file_identity" if identity_refused else "no_episode_file",
                     suggest=("the pack's files numbered like this episode belong to another show or season"
                              "; dropped" if identity_refused else
                              "the pack does not contain this episode; dropped" + (
                                  " (its files count a later season from 1, and ani.zip pairs this episode with no "
                                  "file of it)" if season_relative else
                                  " (a file whose stray number is the episode states another in its marker)"
                                  if skipped else "")))
            if rejected_sink is not None:
                rejected_sink.append(cand)
            continue
        cand["file"] = {k: picked.get(k) for k in ("id", "name", "basename", "size", "index", "matched")}
        total, terms = score_candidate(cand, cand["parsed"], lang, stage="final",
                                       preferred_resolution=settings["preferredResolution"],
                                       group_stats=stats, static_priors=static)
        # The pin's group bonus is added outside `score_candidate` (rank_candidates), so the rescore
        # carries it from the terms it replaces. Assigned, not added to the old terms: once however often this runs.
        pin_bonus = (cand.get("terms") or {}).get("pin.group")
        if pin_bonus:
            terms["pin.group"] = pin_bonus
            total += pin_bonus
        cand["score"], cand["terms"] = round(total, 1), terms
        log.info("score.file_pick", "pick", result="PASS", id=cand["id"], file=cand["file"]["basename"],
                 matched=picked["matched"], size=picked.get("size"), score=cand["score"], source=cand.get("files_from"),
                 pin_bonus=pin_bonus, identity=picked.get("identity"), read=picked.get("read"), **passed_over)
        kept.append(cand)
    order_ranked(kept, log, lang, "file_pick")      # a bare sort here undid the verified-language rule
    return kept


def donor_pool(ranked: list[dict], releases: list[dict]) -> list[dict]:
    """Ranked candidates first, then a light candidate for every release the prefilter dropped.

    Prefilter reasons are about *streaming* a release ("no seeders", size, batch); none of them is a
    reason to ignore its subtitle attachment, which the index's storage host serves regardless.
    """
    out = list(ranked)
    seen = {str(c.get("info_hash") or "").lower() for c in ranked}
    for r in releases:
        h = str(r.get("info_hash") or "").lower()
        if not h or h in seen:
            continue
        seen.add(h)
        out.append({"id": r.get("id"), "indexer": r.get("indexer"), "also_from": r.get("also_from"),
                    "title": r.get("title"),
                    "info_hash": r.get("info_hash"), "seeders": r.get("seeders"), "tracks": None, "dropped": True,
                    "parsed": parse_title(str(r.get("title") or ""))})
    return out


# The words the user reads while a play is being
# assembled came from a hand-built table here — a second vocabulary next to `say._STAGES`, and one
# that could (and did) drift into jargon a viewer should never read. `saymod.stage(provider, detail)`
# is the single source now; nothing here names the sentence, only what the sentence needs to know
# (which provider, and which one is tried next).
# `PROVIDER_NAMES` (from `providers`) is the same vocabulary in the middle of a sentence:
# a status line that says a provider has been given up on has to name the one being tried instead,
# or it reads as "the play has stopped". `saymod.stage`'s own `NEXT_NAMES` is the viewer's copy of
# it.


def next_provider_key(provider: str, providers) -> str:
    """The provider key next in the user's own order, or `""` if none is left."""
    order = [str(p) for p in (providers or [])]
    rest = order[order.index(provider) + 1:] if provider in order else []
    return rest[0] if rest else ""


def next_provider_words(provider: str, providers) -> str:
    """"… — trying X", for the trace's own vocabulary (`PROVIDER_NAMES`) — `saymod.stage`'s
    `nextProvider` detail is the viewer's version of the same fact."""
    nxt = next_provider_key(provider, providers)
    if not nxt:
        return "nothing else is left to try"
    return f"trying {PROVIDER_NAMES.get(nxt, nxt)}"


def local_outlook(ranked: list[dict], releases: list[dict], dropped: dict, settings: dict) -> dict:
    """What the run knows about its own chances before any provider is asked.

    Logged whether or not it changes behaviour, because it is the evidence the failure message is
    built from — "21 of 25 releases have no seeder and the four that survive are all complete-series
    packs" is the diagnosis, and none of it was reachable outside `rank_candidates`' debug lines.
    """
    debrid = [p for p in settings.get("providers", [])
              if p in ("torbox", "realdebrid") and (CONFIG_DIR / f"{p}-token").exists()]
    return {
        "releases": len(releases),
        "survivors": len(ranked),
        "dropped": dict(dropped),
        "unseeded": int(dropped.get("no seeders", 0)),
        "batches": sum(1 for c in ranked if (c.get("parsed") or {}).get("batch")),
        "seeded_singles": sum(1 for c in ranked if not (c.get("parsed") or {}).get("batch")
                              and (c.get("seeders") or 0) > 0),
        "files_unknown": sum(1 for c in ranked if not c.get("file")),
        "debrid": debrid,
        "top_score": ranked[0]["score"] if ranked else None,
    }


# AniList splits a season where AniDB does not (Copper Kite Run "1st STAGE" / "2nd & 3rd STAGE"),
# and ani.zip hangs the AniDB entry on the first AniList id only. These bound the walk to its prequels.
PREQUEL_FORMATS = {"TV", "TV_SHORT", "ONA"}
PREQUEL_MAX_HOPS = 3
PREQUEL_AIRDATE_SLACK_DAYS = 1          # AniDB dates are Japanese broadcast days, AniList's are UTC instants
# A prequel's episode count shifts the episode number: past this it is not AniList's (366 the most recorded).
PREQUEL_EPISODES_MAX = 100_000


def _airdate_agrees(airing_at, airdate) -> bool | None:
    """True/False when both sides are known, None when either is missing (then it cannot veto)."""
    if not airing_at or not airdate:
        return None
    try:
        aired = datetime.fromtimestamp(int(airing_at), timezone.utc).date()
        listed = datetime.strptime(str(airdate)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return abs((aired - listed).days) <= PREQUEL_AIRDATE_SLACK_DAYS


def inherit_prequel_mapping(log: Log, anilist_id: int, episode: int, fetch_chain, fetch_mapping) -> tuple[dict, int] | None:
    """(prequel's ani.zip mapping, the episode number in it) when this entry's episode continues a
    prequel's AniDB entry; None otherwise. Every refusal is logged: a wrong inheritance plays the
    wrong episode, which is worse than "not found".

    An answer of the wrong type inherits nothing, as a prequel with no count does: relations that are not
    objects (`relation_nodes`), a prequel whose id is not an AniList id (`valid_media_id`; the id names ani.zip's
    cache file) or whose count is not an int in 1..PREQUEL_EPISODES_MAX."""
    start = fetch_chain(anilist_id) or {}
    schedule = start.get("airingSchedule") if isinstance(start, dict) else None
    airing_at = next((n.get("airingAt") for n in (schedule.get("nodes") if isinstance(schedule, dict) else None) or []
                      if isinstance(n, dict) and n.get("episode") == episode), None)
    node, offset = start, 0
    for hop in range(1, PREQUEL_MAX_HOPS + 1):
        try:
            pre = next((n for rel, n in relation_nodes(node)
                        if rel == "PREQUEL" and n.get("type", "ANIME") == "ANIME"
                        and isinstance(n.get("format"), str) and n["format"] in PREQUEL_FORMATS), None)
        except TypeError as exc:
            log.warn("map.inherit", "none", result="SKIP", anilist_id=anilist_id, episode=episode, hop=hop,
                     error=str(exc), reason="the relations answer is malformed: nothing is inherited",
                     suggest="AniList's relations answer is malformed; the play searches by this entry's own ids")
            return None
        if not pre:
            log.info("map.inherit", "none", result="EMPTY", anilist_id=anilist_id, episode=episode, hop=hop,
                     reason="no TV/ONA prequel")
            return None
        pre_id, episodes = valid_media_id(pre.get("id")), pre.get("episodes")
        if not episodes:
            log.info("map.inherit", "none", result="EMPTY", anilist_id=anilist_id, episode=episode, hop=hop,
                     prequel=pre_id if pre_id is not None else log_value(pre.get("id")),
                     reason="prequel has no episode count, cannot offset")
            return None
        if pre_id is None or type(episodes) is not int or not 0 < episodes <= PREQUEL_EPISODES_MAX:
            log.warn("map.inherit", "none", result="SKIP", anilist_id=anilist_id, episode=episode, hop=hop,
                     prequel=log_value(pre.get("id")), episodes=log_value(episodes),
                     reason=f"the prequel's id is not an AniList id or its count not an int in "
                            f"1..{PREQUEL_EPISODES_MAX}: nothing is inherited",
                     suggest="AniList's relations answer is malformed; the play searches by this entry's own ids")
            return None
        offset += episodes
        mapping = fetch_mapping(pre_id) or {}
        shifted = episode + offset
        mapped = bool((mapping.get("mappings") or {}).get("anidb_id"))
        ep = (mapping.get("episodes") or {}).get(str(shifted)) or {}
        log.info("map.inherit", "hop", result="PASS", anilist_id=anilist_id, episode=episode, hop=hop,
                 prequel=pre_id, format=pre.get("format"), episodes=episodes, offset=offset,
                 mapped=mapped, key=bool(ep.get("anidbEid")), shifted=shifted)
        if mapped and ep.get("anidbEid"):
            agrees = _airdate_agrees(airing_at, airdate(ep))
            if agrees is False:
                log.warn("map.inherit", "none", result="FAIL", anilist_id=anilist_id, episode=episode,
                         prequel=pre_id, shifted=shifted, airingAt=airing_at, airdate=ep.get("airdate"),
                         reason="airdate disagrees", suggest="the prequel's AniDB entry is a different broadcast")
                return None
            log.info("map.inherit", "adopt", result="PASS", anilist_id=anilist_id, episode=episode,
                     prequel=pre_id, anidb_id=mapping["mappings"]["anidb_id"], shifted=shifted,
                     anidb_eid=ep["anidbEid"], airdate_check=agrees)
            return mapping, shifted
        if mapped:
            log.info("map.inherit", "none", result="EMPTY", anilist_id=anilist_id, episode=episode,
                     prequel=pre_id, shifted=shifted, reason="the prequel's AniDB entry has no such episode")
            return None
        node = fetch_chain(pre_id) or {}
    log.info("map.inherit", "none", result="EMPTY", anilist_id=anilist_id, episode=episode,
             reason=f"no mapped prequel within {PREQUEL_MAX_HOPS} hops")
    return None


def resolve_episode_ids(log: Log, anilist_id: int, episode: int, mapping: dict, fetch_chain, fetch_mapping) -> dict:
    """`episode_ids`, plus the prequel fallback for an entry ani.zip has no AniDB id for. The AniList
    number stays `episode`; the inherited one only feeds the indexers (eid) and `alt_numbers`."""
    if (mapping.get("mappings") or {}).get("anidb_id"):
        return episode_ids(mapping, episode, log)
    inherited = inherit_prequel_mapping(log, anilist_id, episode, fetch_chain, fetch_mapping)
    if not inherited:
        return episode_ids(mapping, episode, log)
    pmap, shifted = inherited
    ids = episode_ids(pmap, shifted, log)
    ids["alt_numbers"] = sorted({shifted, *ids["alt_numbers"]} - {episode})
    ids["inherited"] = {"from": int(pmap["mappings"].get("anilist_id") or 0) or None, "offset": shifted - episode,
                        "episode": shifted}
    return ids


def require_debrid(log: Log, settings: dict) -> list[str]:
    """The providers that can play, in order — those in `providers` with a token. None
    means nothing can ever start (the plugin never streams a torrent itself), so the play
    ends here, before any search or player, with a message that names the two services and no site."""
    providers = list(settings.get("providers") or [])
    tokens = [p for p in KNOWN_PROVIDERS if (CONFIG_DIR / f"{p}-token").exists()]
    usable = [p for p in providers if p in tokens]
    if usable:
        log.info("provider.none", "pass", result="PASS", providers=providers, tokens=tokens, usable=usable,
                 reason="at least one provider in the setting has a token")
        return usable
    # Nothing is usable, so any token found belongs to a provider the setting leaves out.
    log.warn("provider.none", "refuse", result="FAIL", providers=providers, tokens=tokens,
             suggest="add a TorBox or Real-Debrid token in Accounts"
             + (f", or add {', '.join(tokens)} to the providers setting" if tokens else ""))
    sentences = saymod.no_provider()
    detail = f"providers: {', '.join(providers) or 'none'}; tokens found: {', '.join(tokens) or 'none'}"
    if tokens:
        detail += f" (not in the providers setting: {', '.join(tokens)})"
    raise StageError("provider.none", sentences["headline"],
                     f"Add a TorBox or Real-Debrid token — {ACCOUNTS}. · {detail}", say=sentences)


def command_registry(args, log: Log, settings: dict) -> "indexers.Registry":
    """The command's indexer registry, `args.registry`, loaded on first use. Loaded once per command: the
    search, the subtitle-track reads and the play all use it."""
    if getattr(args, "registry", None) is None:
        args.registry = indexers.load(settings, log)
    return args.registry


def require_indexer_first(args, log: Log, settings: dict) -> None:
    """A play or a resolve with no indexer loaded stops here, before the player window opens and before
    any network call (ani.zip, AniList), as the release picker already does. It used to stop only at
    `index_search`'s check, after the window had flashed and the round trips were made; with ani.zip down the viewer
    got a network error instead. Loads the command's registry into `args.registry` when it has none. Two plays need
    no indexer and go on: a pick (`args.release`, played from the stored search's record) and a pinned pack (`_resolve`
    plays one that covers the episode without a search; whether it covers needs ani.zip's numbering, so any pinned
    pack goes on, and one that does not cover stops at `index_search`'s check as before). It decides once per command:
    `do_play`, its `do_resolve` and a pinned attempt's own `do_resolve` (whose `args` are a copy) share
    `args.indexer_checked`, so the trace holds one `early` line, the first call's."""
    if command_registry(args, log, settings).loaded or getattr(args, "indexer_checked", False):
        return
    args.indexer_checked = True
    pick = getattr(args, "release", None)
    pin = None if pick or getattr(args, "ignore_pin", False) else pins.get(args.anilist_id)
    if pick or (pin or {}).get("pack"):
        log.info(indexers.NONE_STAGE, "early", result="SKIP", anilist=args.anilist_id, episode=args.episode,
                 pick=pick, pinned=((pin or {}).get("pack") or {}).get("info_hash"),
                 reason="a pick plays from the stored search's record" if pick
                 else "a pinned pack plays from the pin's own record when it covers the episode, without a search")
        return
    indexers.require(args.registry, log)


# A pack TorBox has not cached is added (a download starts there) only with at least this
# many seeders; fewer and it would sit on the account unfinished. An unknown count on the
# viewer's own pick is not "fewer" — that pack is added.
PACK_MIN_SEEDERS = 10


def do_resolve(args, log: Log, settings: dict, dry_run: bool, ranked_sink: list | None = None,
               status: "playstate.PlayStatus | None" = None) -> dict:
    """`status` is the shared in-flight entry when this runs under `play`, and None under
    `resolve`. Every call on it is a no-op until it has been claimed, so this stays one code path.

    Every resolve keeps its search for the release picker (`searches`), a refusal included
    — the picker a refusal opens must not search a second time — but the next-episode prefetch's refusal.
    `args.release` (an infohash) plays exactly that release from the stored search."""
    if not dry_run:
        require_debrid(log, settings)
    search = searches.Search(args.anilist_id, args.episode, log, pick=getattr(args, "release", None),
                             keep=getattr(args, "keep_search", True))
    command_registry(args, log, settings)
    require_indexer_first(args, log, settings)
    if getattr(args, "track_source", None) is None:
        # One subtitle-track source per command, so an add-on that hangs is given up on once.
        args.track_source = indexers.SubtitleTracks(args.registry, Http(log, settings["httpTimeout"]), log, settings)
    if getattr(args, "file_source", None) is None:
        # Likewise one file-list source per command.
        args.file_source = indexers.FileLists(args.registry, Http(log, settings["httpTimeout"]), log, settings)
    try:
        return _resolve(args, log, settings, dry_run, ranked_sink, status, search)
    except (Held, Deferred):
        # Neither refused nor failed: the stored search the resolve wrote before the providers is kept as it is, so the
        # held play's "Watch the dub now" (`--release`) finds the dub in it after this process has exited.
        raise
    except StageError as exc:
        if isinstance(exc, Interrupted) and playstate.being_superseded():
            # Another play (the viewer's "Watch the dub now") is claiming over this one: nothing stopped and nothing
            # failed, so the picker's list is kept exactly as it was (no "Auto-play stopped", no release marked
            # failed). `player stop` takes no claim lock and still marks the search as before.
            log.info("releases.cache", "superseded", result="SKIP", anilist=args.anilist_id, episode=args.episode,
                     pick=search.pick, stage_failed=exc.stage,
                     reason="another play claimed over this one; the stored search is left as it was")
            raise
        probes = exc.data.get("probes") or []
        kinds = {saymod.probe_kind(p) for p in probes}
        if search.pick and "unavailable" in kinds and kinds <= {"unavailable", "blocked"}:
            # TorBox's own side refused this pick; nothing is known against the release, so the
            # picker's row is not marked failed (it would read "failed: …" as if the pack were bad).
            refused = next(p for p in probes if saymod.probe_kind(p) == "unavailable")
            log.info("releases.cache", "not_failed", result="SKIP", anilist=args.anilist_id,
                     episode=args.episode, info_hash=search.pick, error_code=refused.get("error_code"),
                     unavailable=refused.get("unavailable"), stage_failed=refused.get("stage"),
                     reason=f"TorBox refused on its own side ({refused.get('error_code')}); that is not this "
                            f"release failing")
        elif getattr(args, "prefetch", False):
            # A prefetch that stopped is not the viewer's play: the episode's list keeps no refusal from it.
            log.info("releases.cache", "refusal", result="SKIP", anilist=args.anilist_id, episode=args.episode,
                     stage_failed=exc.stage, reason="the next-episode prefetch stopped; the list shows no refusal")
        else:
            search.refused(exc)
        raise


def picked_release(http: Http, log: Log, args, ids: dict, variants: list[str], series_title: str,
                   settings: dict, search: "searches.Search",
                   movie_pairs: Iterable[tuple[int, int]] = (), single_episode: bool = False,
                   related_shows: tuple = (), season: int | None = None,
                   franchise_shows: tuple | Callable[[], tuple] = (), first_season: bool = False,
                   first_season_titles: tuple = (), titles: tuple = (), prequel_shows: tuple = (),
                   first_season_franchise: Callable[[], tuple] | None = None, tv_series: bool = False) -> list[dict]:
    """The release the viewer picked, as the index served it: from the pin (a pinned pack), from the
    stored search (any age: the pick came from it), else from one fresh search. Never rebuilt from
    its hash (a magnet rebuilt from it has no trackers). The fresh search carries the first search's filter inputs (the
    related shows, the franchise shows, the first season), so it finds what that search listed. `franchise_shows` may be
    a callable (`_resolve`'s `franchise_shows_for`): it is read only for that fresh search."""
    rel = getattr(args, "pinned_release", None)
    how = "pin"
    if rel is None:
        rel = searches.find_release(args.anilist_id, args.episode, search.pick)
        how = "stored search"
    if rel is None:
        rejected: list = []
        franchise_shows = franchise_shows() if callable(franchise_shows) else franchise_shows
        found = index_search(http, log, ids, args.episode, variants, series_title, settings, rejected_sink=rejected,
                             reg=getattr(args, "registry", None), movie=bool(getattr(search, "movie", False)),
                             movie_pairs=movie_pairs, single_episode=single_episode,
                             related_shows=related_shows, season=season, franchise_shows=franchise_shows,
                             first_season=first_season, first_season_titles=first_season_titles, titles=titles,
                             prequel_shows=prequel_shows, first_season_franchise=first_season_franchise,
                             tv_series=tv_series)
        pool = found + [r for r, _, _ in rejected]
        rel = next((r for r in pool if str(r.get("info_hash") or "").lower() == search.pick), None)
        how = "searched again"
    # A first season's read is asked only by a search, so its pick found without one says so too.
    if callable(franchise_shows) or (first_season_franchise is not None and how != "searched again"):
        log.info("resolve", "franchise_shows", result="SKIP", anilist_id=args.anilist_id,
                 reason=f"the pick was found in the {how}: no search, so the franchise is not read")
    log.info("play.release", "lookup", result="PASS" if rel else "FAIL", info_hash=search.pick, how=how,
             title=(rel or {}).get("title"), source=(rel or {}).get("source"),
             trackers=str((rel or {}).get("magnet") or (rel or {}).get("magnet_uri") or "").count("&tr="))
    if rel is None:
        raise StageError("release.gone", "The release you picked is no longer listed by any index.",
                         "choose another release", info_hash=search.pick)
    return [rel]


def pin_seeders(log: Log, args, pack: dict) -> tuple[int | None, str | None]:
    """The pinned pack's seeder count and where it came from, or None when it
    is unknown — never 0. The pin's own count (recorded at the pick) counts while it is younger than
    `pins.SEEDERS_FRESH_S`; a pin made before counts were recorded falls back to the episode's stored
    search younger than `searches.SEARCH_TTL`."""
    now = time.time()
    seeders = source = None
    age = None
    if pack.get("seeders") is not None and pack.get("seen_at"):
        source, age = "pin", round(now - float(pack["seen_at"]))
        if age < pins.SEEDERS_FRESH_S:
            seeders = int(pack["seeders"])
            reason = "the count recorded on the pin when it was made"
        else:
            reason = (f"the count recorded on the pin is older than {pins.SEEDERS_FRESH_S // 3600} h; "
                      "seeders unknown")
    else:
        stored = searches.load(args.anilist_id, args.episode) or {}
        seen = (stored.get("raw") or {}).get(pack["info_hash"])
        if seen is None:
            reason = (f"the pin has no count and no search younger than {searches.SEARCH_TTL // 60} min "
                      "holds this pack; seeders unknown")
        elif seen.get("seeders") is None:
            source, age = "stored search", round(now - float(stored["searched_at"]))
            reason = "the episode's stored search holds this pack without a count; seeders unknown"
        else:
            source, age = "stored search", round(now - float(stored["searched_at"]))
            seeders = int(seen["seeders"])
            reason = "the pin has no count; the episode's stored search holds this pack"
    log.info("pin.seeders", "lookup", result="PASS" if seeders is not None else "UNKNOWN",
             anilist=args.anilist_id, episode=args.episode, seeders=seeders, recorded=pack.get("seeders"),
             source=source, age_s=age, reason=reason)
    return seeders, source


def play_pinned(args, log: Log, settings: dict, dry_run: bool, ranked_sink: list | None,
                status: "playstate.PlayStatus | None", pin: dict,
                torbox: "TorBox | None") -> tuple[dict | None, str | None, str | None]:
    """A pinned pack that covers the episode plays without a search — the viewer chose it.
    `(result, why_not, kind)`: the result when it played; else None and "dropped" (proven dead, it left
    the pin), "stuck" (queued, but its progress has not moved for `pins.STUCK_S`), "unavailable" (an
    error on TorBox's own side; `kind` is its `unavailable` kind, else None), or None (it failed
    for another reason). A pack still downloading ends the play with that failure.

    `torbox` is the resolve's one TorBox client, handed to the pinned attempt so the search past a
    pin shares what it learned (the hourly-limit stop above all)."""
    pack = pin["pack"]
    log.info("pin.apply", "pack", result="PASS", anilist=args.anilist_id, episode=args.episode,
             title=pack.get("title"), range=pack.get("range"), files=len(pack.get("files") or []),
             reason="the pinned pack covers this episode; no search, no ambiguity check")
    run = types.SimpleNamespace(**vars(args))
    run.release = pack["info_hash"]
    run.torbox_client = torbox
    seeders, source = pin_seeders(log, args, pack)
    run.pinned_release = {"id": f"pin:{pack['info_hash'][:12]}", "title": pack.get("title"),
                          "info_hash": pack["info_hash"], "magnet": pack.get("magnet"), "source": "pin",
                          "seeders": seeders, "seeders_source": source if seeders is not None else None,
                          "leechers": None, "total_size": None, "num_files": None,
                          "timestamp": None, "trusted": False, "remake": False, "anidb_eid": None}
    try:
        return do_resolve(run, log, settings, dry_run, ranked_sink, status), None, None
    except StageError as exc:
        probes = [p for p in exc.data.get("probes") or [] if p.get("id") == run.pinned_release["id"]]
        kinds = [saymod.probe_kind(p) for p in probes]
        if exc.stage == "score.file_pick":
            pins.drop_pack(log, args.anilist_id, exc.stage, str(exc))
            return None, "dropped", None
        if "queued" in kinds:
            queued = probes[kinds.index("queued")]
            still = pins.note_queued(log, args.anilist_id, pack["info_hash"], queued.get("provider"),
                                     queued.get("torrent_id"), queued.get("progress"))
            if still >= pins.STUCK_S:
                log.warn("pin.apply", "stuck", result="SKIP", anilist=args.anilist_id,
                         provider=queued.get("provider"), torrent_id=queued.get("torrent_id"),
                         progress=queued.get("progress"), still_s=round(still),
                         reason=f"the pinned pack is queued but its progress has not moved for "
                                f"{pins.STUCK_S // 3600} h or more; searching past it for this play",
                         suggest="the pin stays and the next play asks the provider again; press p to choose "
                                 "another release, or x to forget the pin")
                return None, "stuck", None
            # The provider is downloading the viewer's pick. Searching past it would play
            # something they did not choose and hide the progress, so the play reports the download.
            log.info("pin.apply", "downloading", result="WAIT", anilist=args.anilist_id,
                     provider=queued.get("provider"), torrent_id=queued.get("torrent_id"),
                     progress=queued.get("progress"), reused=queued.get("reused"),
                     reason="the pinned pack is queued and still downloading; the pin stays and no search is made",
                     suggest="play again once the download has finished")
            raise
        if "unavailable" in kinds:
            # TorBox refused on its own side (no download server free, or a database or download
            # server error). The pin stays, it is not marked queued, and this play searches past the pin;
            # the next play asks again.
            refused = probes[kinds.index("unavailable")]
            log.info("pin.apply", "unavailable", result="SKIP", anilist=args.anilist_id,
                     provider=refused.get("provider"), error_code=refused.get("error_code"),
                     unavailable=refused.get("unavailable"), stage_failed=refused.get("stage"),
                     retry_after_s=refused.get("retry_after"),
                     reason=f"TorBox refused the pinned pack on its own side ({refused.get('error_code')} at "
                            f"{refused.get('stage')}); the pin stays and is not marked queued; searching past it "
                            f"for this play",
                     suggest=refused.get("suggest"))
            return None, "unavailable", refused.get("unavailable")
        if "skipped" in kinds:
            skipped = probes[kinds.index("skipped")]
            log.info("pin.apply", "skipped", result="SKIP", anilist=args.anilist_id, reason=skipped.get("error"),
                     suggest="the pinned pack stays pinned; searching instead")
        else:
            log.warn("pin.apply", "failed", result="FAIL", anilist=args.anilist_id, stage_failed=exc.stage,
                     error=str(exc), suggest="the pinned pack stays pinned (not proven dead); searching instead")
        return None, None, None


# The stage a reuse of the prefetch's release stops at when it does not apply (`play_prefetched`): not the release
# failing, so the full search keeps it (`reuse_keep`).
REUSE_STAGE = "prefetch.reuse"


def keep_tracks(info_hash: str, stats_key: str, langs: list[str], lang: str) -> None:
    """What a probe read of a release's subtitle tracks, kept: `tracks.json` (the ranking's `verified_langs` from then
    on) and its group's statistics."""
    known = cached_json(CACHE_DIR / "tracks.json", None) or {}
    known[str(info_hash).lower()] = langs
    write_cache(CACHE_DIR / "tracks.json", known)
    record_group_langs(stats_key, langs, lang)


def reuse_off(message: str, **data) -> StageError:
    """The reuse's attempt ends here and the full search runs in the same play, with the release kept in it."""
    return StageError(REUSE_STAGE, message, "the full search runs in this play", reuse_keep=True, **data)


def play_prefetched(args, log: Log, settings: dict, ranked_sink: list | None, status: "playstate.PlayStatus | None",
                    torbox: "TorBox | None") -> dict | None:
    """The release the next-episode prefetch found for this episode (`prefetch.reusable`) plays without a search: the
    play's own steps from its `checkcached` on (one hash), the file pick, ffprobe's language and audio, the link and the
    range check, TorBox alone. Only when nothing that decided it changed (`prefetch.fingerprint`), the episode's stored
    search (the prefetch's) still holds it, so the side-load has the search's donors, `require` knows it carries the
    language, its name is no dub under `avoid`, and TorBox calls it cached. One `prefetch reuse` line says SKIP and why,
    or TRY, then PASS once it resolved. The result when it played; else None and one `prefetch fallback` line, and the
    full search runs in the same play, this release in it: from there the play is a play that searched, so it ends as
    one ends (a release that failed here is tried again there, as a cached-only add: no download starts). Never raises
    but what a play lets through (`Held`, `Deferred`, `Interrupted`): an unexpected error in the gates or the attempt
    is a FAIL line and the search. The search past it shares `torbox`, as the search past a pin does."""
    if getattr(args, "prefetch", False) or getattr(args, "pinned_release", None) is not None:
        return None                               # the prefetch's own resolve; a pinned pack's attempt
    media_id, episode = int(args.anilist_id), int(args.episode)

    def skip(why: str, **facts) -> None:
        log.info("prefetch", "reuse", result="SKIP", mediaId=media_id, episode=episode, reason=why, **facts)
    if getattr(args, "release", None):
        # The viewer's pick, "Keep waiting" or "Watch the dub now": that release plays, whatever the prefetch found.
        return skip("a release was picked: it plays, not the prefetch's", pick=str(args.release).lower())
    try:
        got, why = prefetchmod.reusable(media_id, episode)
        if got is None:
            return skip(why)
        facts = {"info_hash": str(got.get("infoHash") or "").lower(), "prefetch_trace": got.get("trace"),
                 "state": got.get("state")}
        if torbox is None:
            return skip("TorBox is not asked in this play (no token)", **facts)
        now = prefetchmod.fingerprint(settings, fallback_language(settings), pins.get(media_id),
                                      command_registry(args, log, settings))
        saved = got.get("fingerprint") or {}
        changed = prefetchmod.differs(saved, now)
        if changed:
            return skip(f"what decides the pick changed since the prefetch ({', '.join(changed)}): the search decides",
                        differs=changed, was={k: saved.get(k) for k in changed}, now={k: now[k] for k in changed},
                        **facts)
        if settings.get("subtitlePolicy") == "require" and got.get("hasLang") is not True:
            return skip(f"subtitlePolicy=require and the prefetched release is not known to carry "
                        f"{settings['subtitleLanguage']}: its cover is looked for among the search's releases", **facts)
        if settings.get("dubs", DEFAULT_SETTINGS["dubs"]) == "avoid" and got.get("dubWords"):
            return skip("the prefetched release reads as a dub (dubs=avoid): whether an original exists is the "
                        "search's", **facts)
        if searches.find_release(media_id, episode, facts["info_hash"]) is None:
            # The side-load's donors for it are the search's other releases (`release_donor_pool`), as for a pick.
            return skip("the episode's stored search no longer holds the prefetched release: its side-load donors "
                        "are the search's", **facts)
        # Found to lack the viewer's language, it plays as the search's `prefer` fallback would, or the search runs.
        sideload, sideload_why = prefetchmod.sideload_reuse(got, settings.get("subtitlePolicy"),
                                                            settings["subtitleLanguage"])
    except Exception as exc:                      # noqa: BLE001 - the reuse is optional: the play searches as before
        log.warn("prefetch", "reuse", result="FAIL", mediaId=media_id, episode=episode,
                 error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()[-1500:],
                 suggest=f"the play searches as before; bug: report with `omarchy-anime trace {log.trace}`")
        return None
    log.info("prefetch", "reuse", result="TRY", mediaId=media_id, episode=episode, release=got.get("release"),
             search_ms=got.get("searchMs"), why=why, **facts, has_lang=got.get("hasLang"),
             cached_others=got.get("cachedOthers"), sideload=sideload, sideload_why=sideload_why,
             reason="nothing that decided the prefetch's pick changed: it plays without a search once this play's "
                    "checkcached calls it cached; if it lacks the viewer's language, " +
                    ("it plays side-loaded, as the search would" if sideload else "the search decides"))
    t0 = time.monotonic()
    run = types.SimpleNamespace(**vars(args))
    run.prefetched_release = dict(got["record"])
    run.prefetch_sideload, run.prefetch_sideload_why = sideload, sideload_why
    run.prefetch_donors = got.get("donorPool")
    run.keep_search = False                       # the episode's stored search stays the prefetch's, not this one's
    run.torbox_client = torbox
    try:
        # TorBox alone: the release is the one TorBox has cached; Real-Debrid's turn is the search's.
        done = do_resolve(run, log, {**settings, "providers": ["torbox"]}, False, ranked_sink, status)
    except (Held, Deferred, Interrupted):
        raise
    except StageError as exc:
        kinds = {saymod.probe_kind(p) for p in exc.data.get("probes") or []}
        reuse_keep = bool(exc.data.get("reuse_keep"))
        # What ended it: a gate of the attempt (`reuse_keep`: not cached, no viewer's language, a dub by name), or
        # TorBox's own side, a refused account, a dub by its audio (nothing against the release); else the release
        # failed here (no file for the episode, its link, its stream).
        failed = not (reuse_keep or (kinds and kinds <= {"unavailable", "blocked", "dubbed"}))
        log.info("prefetch", "fallback", result="FAIL" if failed else "SKIP", mediaId=media_id, episode=episode,
                 stage_failed=exc.stage, error=str(exc), release_failed=failed, kinds=sorted(kinds),
                 reuse_keep=reuse_keep, spent_ms=round((time.monotonic() - t0) * 1000), **facts,
                 reason="the prefetched release did not play: the full search runs in this play, the release in it, "
                        "and the play ends as a play that searched ends",
                 suggest="the release could not play here; the search tries it again with the others" if failed
                 else "")
        if status is not None:
            status.stage("search", saymod.stage("search"))       # the search runs now, not the attempt's ranking
        return None
    except Exception as exc:                      # noqa: BLE001 - the reuse is optional: the play searches as before
        log.warn("prefetch", "reuse", result="FAIL", mediaId=media_id, episode=episode,
                 error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()[-1500:],
                 spent_ms=round((time.monotonic() - t0) * 1000), **facts,
                 suggest=f"the play searches as before; bug: report with `omarchy-anime trace {log.trace}`")
        if status is not None:
            status.stage("search", saymod.stage("search"))
        return None
    fallback = bool((done.get("stream") or {}).get("subtitle_fallback"))
    log.info("prefetch", "reuse", result="PASS", mediaId=media_id, episode=episode, release=got.get("release"),
             saved_ms=got.get("searchMs"), spent_ms=round((time.monotonic() - t0) * 1000), **facts,
             subtitle_fallback=fallback, sideload_why=sideload_why if fallback else None,
             reason="the prefetched release resolved without a search" + (
                 f", without {settings['subtitleLanguage']}: the side-load puts it on screen, as after the search"
                 if fallback else ""))
    return done


def cover_kind(cover: dict | None) -> str | None:
    """What a `require` coverage answer rests on, for the log: "external", "donor" or None."""
    return None if not cover else "external" if cover.get("external") else "donor"


def _resolve(args, log: Log, settings: dict, dry_run: bool, ranked_sink: list | None,
             status: "playstate.PlayStatus | None", search: "searches.Search") -> dict:
    picked = search.pick
    # "Keep waiting" from a held play (`play --release <the original> --wait --dub <the dub>`): the pick is the
    # release TorBox is downloading, waited for again on the same download with the dub held as the choice.
    keep_waiting = bool(picked and getattr(args, "wait", False))
    # The release the next-episode prefetch saved, in `play_prefetched`'s attempt: the one release, no search.
    reused = getattr(args, "prefetched_release", None)
    if picked:
        # The viewer's pick overrides every filter, `require` included; its row showed why.
        settings = {**settings, "subtitlePolicy": "prefer"}
        log.info("play.release", "pick", result="PASS", info_hash=picked,
                 reason="the viewer picked this release: filters, the ambiguity check and require are skipped")
    http = Http(log, settings["httpTimeout"])
    say = status.stage if status is not None else (lambda *a, **k: None)
    t_all = time.monotonic()
    # The settings are the biggest input to every decision below (which releases pass, which
    # provider is tried, whether a missing language is fatal). Logging them turns "why did it play
    # that?" into a question the trace answers.
    log.info("resolve", "settings", result="PASS", subtitleLanguage=settings["subtitleLanguage"],
             subtitleLanguage_from=setting_source("subtitleLanguage", settings),
             subtitleFallbackLanguage=fallback_language(settings) or FALLBACK_OFF,
             subtitleFallbackLanguage_from=setting_source("subtitleFallbackLanguage", settings),
             # The regional choice, which only the track pick, `slang` and the subtitle databases read.
             subtitleLanguageVariant=setting_variant(settings),
             subtitleFallbackLanguageVariant=setting_variant(settings, "subtitleFallbackLanguage"),
             # A stored code the load refused (the default above is in its place).
             **{f"{k}_refused": setting_refused(k, settings) for k in ("subtitleLanguage", "subtitleFallbackLanguage")
                if setting_refused(k, settings) is not None},
             subtitlePolicy=settings["subtitlePolicy"], providers=settings["providers"],
             probeCount=settings["probeCount"], allowBatches=settings.get("allowBatches"),
             preferredResolution=settings.get("preferredResolution"),
             config_exists=(CONFIG_DIR / "config.json").exists())
    # The message is always `saymod.stage(name)` — the numbers that
    # used to ride in the sentence (`len(releases)`, budgets, counts) go in `detail` instead, where
    # the trace still gets them (`PlayStatus.stage` logs `detail` on every call) without putting a
    # jargon word or an assembled string where the popout's history renders it.
    say("search", saymod.stage("search"))
    mapping = anizip_mappings(http, log, args.anilist_id)
    # The AniList client is only built if ani.zip has no AniDB id for this entry.
    anilist = lazy_anilist(http, log)
    chains: dict = {}                   # this entry's own relations answer carries its episode count

    def fetch_chain(media_id: int) -> dict:
        got = anilist().sequel_chain(media_id)
        chains.setdefault(media_id, got)
        return got
    ids = resolve_episode_ids(log, args.anilist_id, args.episode, mapping, fetch_chain,
                              lambda media_id: anizip_mappings(http, log, media_id))
    # One TorBox client per resolve. `play_pinned` hands it to its pinned attempt
    # (`torbox_client`), so the search past a pin shares what the client learned — the hourly-limit
    # stop above all. It lives as long as this call; a later resolve builds its own.
    torbox_client = getattr(args, "torbox_client", None)
    if torbox_client is None and torbox_usable(settings):
        torbox_client = TorBox(http, log, read_token("torbox"))
    # The prefetch's attempt comes after the play's own pin check (`play_prefetched`): not a second one.
    pin = None if picked or reused is not None else pins.get(args.anilist_id)
    pin_dropped = False
    # The pin check runs before the titles are fetched, so it reads the remembered ones (no request). Only a
    # pinned pack with recorded files needs it (a range decides alone), so only then is it built and logged.
    pin_entry = entry_identity(log, args.anilist_id, mapping, title_variants(args.anilist_id, mapping)[0], "pin") if (
        pin and not getattr(args, "ignore_pin", False) and (pin.get("pack") or {}).get("files")) else None
    if (pin and not getattr(args, "ignore_pin", False)
            and pins.covers(pin.get("pack"), args.episode, ids.get("alt_numbers") or [], pin_entry, log)):
        done, why_not, kind = play_pinned(args, log, settings, dry_run, ranked_sink, status, pin, torbox_client)
        if done is not None:
            return done
        if torbox_client is not None and hasattr(torbox_client, "retry_failed_listing"):
            # The play reads the account once, but a read the pinned attempt could not make is not this search's
            # answer: it reads again (once at most, and only after a failed read).
            torbox_client.retry_failed_listing("the search past the pin reads the account itself: the pinned "
                                               "attempt's read failed")
        pin_dropped = why_not == "dropped"
        if pin_dropped:
            say("search", saymod.stage("search", {"pinDropped": True}), pinDropped=True)
        elif why_not == "stuck":
            hours = pins.STUCK_S // 3600
            say("search", saymod.stage("search", {"pinStuck": hours}), pinStuck=hours)
        elif why_not == "unavailable":
            # The error's kind picks the words, from the same row of `say` as the headline.
            say("search", saymod.stage("search", {"pinUnavailable": kind}), pinUnavailable=kind)
        pin = pins.get(args.anilist_id)
    # The release the prefetch found for this episode plays without a search when nothing that decided it changed and
    # TorBox has it cached (`play_prefetched`); otherwise this play searches as below.
    if not (dry_run or reused is not None):
        done = play_prefetched(args, log, settings, ranked_sink, status, torbox_client)
        if done is not None:
            return done
    # The titles are looked up here, after the pin check, so a covering pin's play
    # makes no AniList request. The pinned attempt itself (`pinned_release`) searches nothing and
    # reads the remembered titles only; any other resolve may fetch them, with the walk's client.
    pinned = getattr(args, "pinned_release", None) is not None
    variants, series_title = title_variants(args.anilist_id, mapping, log,
                                            None if pinned or reused is not None else anilist)
    search.variants = variants
    # The season this entry is, from its titles (never ani.zip's `x-jat` fan code), decided and logged
    # here once; `index_search` hands it to every add-on's filter.
    season = entry_season(log, args.anilist_id, variants, mapping)
    # Decided before any picker row is built, so a movie's rows name no group word as their show.
    format_from, movie_from = entry_format(args.anilist_id, mapping, log)
    movie = search.movie = bool(movie_from)
    # The one episode marker a movie's own release may state (its ani.zip pair).
    movie_pairs = anizip.movie_pairs(mapping, args.episode) if movie else set()
    # An entry remembered as one-episode needs its relations answer (the related shows' titles).
    related_source = fetch_related_chain(log, args.anilist_id, args.episode, movie, pinned, chains, fetch_chain)
    single_episode = one_episode_entry(log, args.anilist_id, args.episode, movie, chains.get(args.anilist_id))
    related = related_shows_for(log, args.anilist_id, chains, related_source) if single_episode else ()
    # A name numbered on from one of its one-episode prequels is the entry's own; a TV series' episode is no OVA.
    prequels = prequel_shows_for(log, args.anilist_id, chains) if single_episode else ()
    tv_series = tv_series_entry(log, args.anilist_id, format_from, movie, single_episode)
    # The entry identity, decided before the search: a later season's franchise shows reach the
    # filter, which drops the earlier cour's single that names no season (one AniList read per show per the franchise
    # cache's TTL, none for a pinned play). A first season's are read only when a name the search returned states a
    # part none of its titles state (`first_season_parts`), for the stated-part reading alone.
    one_file = movie and movie_is_one_file(log, args.anilist_id, args.episode, chains.get(args.anilist_id))
    entry = entry_identity(log, args.anilist_id, mapping, variants, "resolve", movie_pairs if one_file else None)
    # A first season names no season, so ani.zip says whether a single stating season 2 is another's.
    first = first_season_for(log, args.anilist_id, args.episode, season, entry, mapping, movie, single_episode)
    own_titles = entry_titles(args.anilist_id, variants)      # every title, for the show test and the guard
    first_titles = own_titles if first else ()
    # A viewer's pick reads it only when `picked_release` must search again.
    read_franchise = functools.partial(franchise_shows_for, log, args.anilist_id, entry, movie, single_episode, pinned,
                                       anilist)
    lazy_first = not (entry.get("sequel") or movie or single_episode or pinned)
    # The prefetch's release was found by a search that read the franchise; this play searches nothing.
    franchise = () if lazy_first or reused is not None else read_franchise if picked and not pinned else (
        read_franchise())
    first_franchise = read_franchise if lazy_first else None
    guard_dropped: list = []                   # the franchise drops the guard still reads
    if picked:
        releases = picked_release(http, log, args, ids, variants, series_title, settings, search,
                                  movie_pairs=movie_pairs, single_episode=single_episode, related_shows=related,
                                  season=season, franchise_shows=franchise, first_season=first,
                                  first_season_titles=first_titles, titles=own_titles, prequel_shows=prequels,
                                  first_season_franchise=first_franchise, tv_series=tv_series)
        # A pick still gets a side-loaded track, and its donors are the other releases of the
        # search it was picked from — the one-release list alone has no sibling to donate one.
        donor_releases = releases                # `release_donor_pool` adds the stored search's releases
    elif reused is not None:
        # As the index served it to the prefetch's search (its magnet's trackers included); every filter passed then.
        releases = donor_releases = [dict(reused)]
    else:
        # Releases an add-on rejected as another show or season, with every franchise drop `matches` made.
        wrong_show = RejectedSink()
        t_search = time.monotonic()
        releases = index_search(http, log, ids, args.episode, variants, series_title, settings,
                                rejected_sink=wrong_show, reg=getattr(args, "registry", None), movie=movie,
                                movie_pairs=movie_pairs, single_episode=single_episode, related_shows=related,
                                season=season, franchise_shows=franchise, first_season=first,
                                first_season_titles=first_titles, titles=own_titles, prequel_shows=prequels,
                                first_season_franchise=first_franchise, tv_series=tv_series)
        search.add_raw(releases)
        if getattr(args, "prefetch", False):
            # What the play of this episode reuses (`do_prefetch` saves the release it stops at): the releases as the
            # index served them, and what the search took.
            args.prefetch_search = {"raw": search.raw, "ms": round((time.monotonic() - t_search) * 1000)}
        guard_dropped = guard_drops(wrong_show)
        for rel, code, _ in wrong_show:
            # The franchise reading's drops are not picker rows; they stay in `picked_release`'s pool.
            # Nor is a film cut dropped for a TV episode (a film is no episode, as a movie's `movie_episode`
            # is none).
            if code not in (FRANCHISE_SHOW, COMPILATION_FILM):
                search.reject(rel, code)
        donor_releases = releases
    if not releases:
        raise StageError("index.search", f"no releases found for anilist {args.anilist_id} episode {args.episode}",
                         "check that the episode has aired on AniList; try again in an hour", ids=ids, queries=variants)
    say("rank", saymod.stage("rank"), releases=len(releases))
    dropped: dict = {}
    # The episode reaches the prefilter so a pack whose range cannot hold it costs no provider probe.
    prefilter_rejected: list = []
    ranked = rank_candidates(http, log, releases, settings, dropped_sink=dropped, episode=args.episode,
                             alt=ids.get("alt_numbers") or [], rejected_sink=prefilter_rejected,
                             keep_all=bool(picked) or reused is not None, preferred_group=(pin or {}).get("group"),
                             track_source=args.track_source, season=season, titles=variants)
    if pin and pin.get("group"):
        boosted = [c["id"] for c in ranked if "pin.group" in (c.get("terms") or {})]
        log.info("pin.apply", "group", result="PASS" if boosted else "EMPTY", anilist=args.anilist_id,
                 group=pin["group"], boosted=len(boosted), bonus=pins.GROUP_BONUS,
                 reason="the pin's group is preferred; it does not skip any filter or the ambiguity check")
    for rel, code in prefilter_rejected:
        search.reject(rel, code)
    search.set_passed(ranked)
    if not ranked:
        words = saymod.filtered_out(dropped, len(releases))
        raise StageError("score.prefilter", words["message"], words["suggest"], releases=len(releases),
                         dropped=dropped)
    # When the releases name several different shows of one franchise, the index answered for
    # the franchise, not this entry (an index measured filing every Mimi torrent under the 1993 OVA). A guess
    # there plays another show; the user chose a refusal over the guess.
    # A movie's unmarked release names ("English", "VOSTFR", a scene group) are not other shows.
    titles = [c["title"] for c in ranked]
    # A word in a script the matched title does not use (a bilingual name's Chinese title) is not a show.
    # An episode, season or format word (`Capitulos`, `the Movie`) and a word of any of the entry's own
    # titles (AniList's synonyms name its arcs and subtitles) name no other show.
    # The guard reads every name the search returned, a name dropped as another entry of the franchise
    # too (`guard_drops`, the rule `score.GUARD_DROP_RULE`), so `Show II - 04` dropped for season 1 still counts.
    # An add-on's stop rule counts such a drop as new (`EpisodeQuery.entry_drops`), so a drop the guard
    # reads cannot end its search before the title whose names the guard needs. `from_dropped` names each sibling only a
    # dropped name gave, with the name and why it was dropped.
    ranked_siblings = [] if picked else sibling_names(titles, variants, movie=movie, own=own_titles)
    from_dropped: dict = {}
    for t, code, why in [] if picked else guard_dropped:
        name = guard_reading(t, variants, movie, own_titles)[0]
        if name and name not in ranked_siblings:
            from_dropped.setdefault(name, [name, t, code, why])
    siblings = sorted({*ranked_siblings, *from_dropped})
    ignored = (sorted(set(sibling_names(titles, variants)) - set(sibling_names(titles, variants, movie=True)))
               if movie and not picked else [])
    set_aside: dict = {}
    for t in [] if picked else titles:
        name, aside = guard_reading(t, variants, movie, own_titles)
        if aside:
            set_aside.setdefault(sibling_name(t, variants, movie), [name, aside])
    # The trace pairs each name the script rule changed with what it became (None: no longer a show), with the
    # scripts it was read in; the debug line lists the titles, which are also the picker rows it labels.
    readings = [] if picked else [(t, *sibling_reading(t, variants, movie)) for t in titles]
    changed = [(t, new, old, known) for t, new, old, known in readings if new != old]
    other_script = sorted({(old, new) for _, new, old, _ in changed}, key=lambda p: (p[0], p[1] or ""))
    log.info("score.ambiguous", "check", result="FAIL" if len(siblings) >= AMBIGUOUS_SIBLINGS else "PASS",
             siblings=siblings, threshold=AMBIGUOUS_SIBLINGS, candidates=len(ranked), picked=bool(picked),
             movie=movie, movie_from=movie_from, format_from=format_from, ignored=ignored,
             other_script=[list(p) for p in other_script][:25],
             set_aside=[[old, *v] for old, v in sorted(set_aside.items())][:GUARD_SET_ASIDE_LOGGED],
             set_aside_more=max(0, len(set_aside) - GUARD_SET_ASIDE_LOGGED),
             matched_scripts=sorted({s for _, _, _, known in changed for s in known}),
             from_dropped=[v for _, v in sorted(from_dropped.items())][:GUARD_DROPPED_LOGGED],
             from_dropped_more=max(0, len(from_dropped) - GUARD_DROPPED_LOGGED),
             dropped_read=0 if picked else len(guard_dropped), dropped_rule=GUARD_DROP_RULE)
    if changed:
        log.debug("score.ambiguous", "other_script", titles=[
            {"title": t, "every_script": old, "name": new, "scripts": known} for t, new, old, known in changed[:25]])
    if len(siblings) >= AMBIGUOUS_SIBLINGS:
        search.mark_other_shows(ranked, variants, movie=movie)
        shown = ", ".join(n.title() for n in sorted(siblings, key=len)[:2])
        raise StageError("score.ambiguous",
                         f"The releases found are {len(siblings)} other {series_title} shows ({shown}…); "
                         f"nothing was played.",
                         "the index can't tell this entry apart from its franchise; no safe release to pick",
                         siblings=siblings, candidates=[c["title"] for c in ranked[:5]])
    lang = settings["subtitleLanguage"]
    alt = ids.get("alt_numbers") or []
    # `require` means "the preferred language will be on screen", not "the chosen release
    # embeds it". Before refusing, ask whether a sibling release of the same episode can donate the
    # track — index only, nothing downloaded, and the attachment cache is shared with the play-path
    # sweep so this costs nothing twice. Without it, `require` refuses episodes the plugin can in
    # fact subtitle. A retimed donor satisfies `require`, and so does one whose timing could not be
    # verified: refusing to play because the *timing* is unproven is a cliff-edge refusal to
    # prevent, and the badge says so.
    def require_cover(target: dict) -> tuple[dict | None, int]:
        """A donor, else a subtitle-database file, that puts `lang` on screen for `target`; with how many
        releases the donor scan examined. Never raises: a failed lookup reads as "none found"."""
        if not as_bool(settings.get("sideloadSubtitles"), True):
            return None, 0
        scan = {"donors": [], "examined": 0}
        try:
            scan = subsmod.find_donor(http, log, donor_pool(ranked, releases), target, settings,
                                      episode=args.episode, alt=alt, track_source=args.track_source,
                                      episode_unstated=movie or single_episode)
        except Exception as exc:                 # noqa: BLE001 - a scan failure must read as "none found"
            log.warn("subs.donor", "require", result="FAIL", error=str(exc), target=target.get("id"))
        if scan["donors"]:
            donor = scan["donors"][0]
            log.info("score.decision", "require", result="PASS", target=target.get("id"),
                     reason=f"{target.get('title')} lacks {lang}, but {donor['title']} can donate one",
                     tier=donor["tier_name"])
            return {"available": True, "tier": donor["tier"], "tier_name": donor["tier_name"],
                    "donor": {k: donor.get(k) for k in ("indexer", "release_id", "title", "group", "source",
                                                        "seeders", "track_id", "forced", "anchor_lang",
                                                        "format")}}, scan["examined"]
        # A chosen variant asks the databases for its own code, as the play's worker will.
        external = extsubs.require_lookup(http, log, mapping, args.episode, variants, setting_variant(settings) or lang,
                                          args.anilist_id, target.get("title") or "", **plan_hints(args.anilist_id))
        if external:
            # A matching file found by search in a subtitle database is enough; the
            # play's worker downloads and loads it.
            log.info("score.decision", "require", result="PASS", target=target.get("id"),
                     reason=f"{target.get('title')} lacks {lang} and no release can donate it, but "
                            f"{external['service']} has {external['name']}")
            return {"available": True, "external": external}, scan["examined"]
        log.info("score.decision", "require", result="MISS", target=target.get("id"), examined=scan["examined"],
                 reason=f"no donor and no subtitle-database file for {lang}")
        return None, scan["examined"]

    sideload_info = None
    precheck_key = None                          # the release the pre-check's answer is about
    if reused is not None and settings["subtitlePolicy"] == "require" and ranked[0]["has_lang"] is not True:
        # A donor or a subtitle-database file for it is looked for among the search's other releases: the search's.
        raise reuse_off(f"the prefetched release is not known to carry {lang} (subtitlePolicy=require)")
    if settings["subtitlePolicy"] == "require" and ranked[0]["has_lang"] is not True:
        sideload_info, examined = require_cover(ranked[0])
        precheck_key = release_key(ranked[0])
        if sideload_info is None:
            raise StageError("score.decision",
                             f"No {lang} track in any of {examined or len(ranked)} releases and none to "
                             f"side-load. Switch subtitlePolicy to prefer, or choose a release (p).",
                             "switch subtitlePolicy to prefer, or press p to choose a release",
                             candidates=[c["title"] for c in ranked[:5]], examined=examined)
    for cand in ranked:
        cand["episode"], cand["alt"], cand["entry"] = args.episode, alt, entry
    # The side-load pool: every ranked candidate, **plus** the releases the prefilter dropped.
    # A dead torrent is a perfectly good donor — the .ass is served by the index's storage host and
    # needs no seeders — and "no seeders" is the single commonest prefilter reason,
    # so leaving them out would hide exactly the case side-loading exists for. `result["candidates"]`
    # stays the public top ten; the sink keeps the pool out of the JSON the CLI prints.
    if ranked_sink is not None:
        # A pick's and the prefetch's one release: their donors are the episode's stored search, of any age.
        ranked_sink[:] = (release_donor_pool(ranked[0], donor_releases, args.anilist_id, args.episode)
                          if picked or reused is not None else donor_pool(ranked, donor_releases))
    result = {
        "trace": log.trace, "anilist_id": args.anilist_id, "episode": args.episode, "ids": ids,
        "series_title": series_title, "episode_title": ids.get("title"), "subtitle_language": lang,
        "candidates": public_candidates(ranked),
        "chosen": None, "stream": None, "dry_run": dry_run,
        "sideload": sideload_info,
        "pin_dropped": pin_dropped,
        # A movie or a one-episode entry, whose releases need not state the episode; the subtitle
        # donors read after the resolve (`do_subs`, `spawn_external_worker`) read their names as before, any number.
        "episode_unstated": bool(movie or single_episode),
    }
    # One guard for the whole resolve. It is built here rather than at the provider loop
    # because TorBox's *cheap* call is up here, and a provider that has already refused the account
    # should not be asked anything at all — `checkcached` costs 968 ms (measured) to buy a +20 score
    # bonus for a cache nothing can be streamed from.
    guard = BlockGuard(log, settings)
    # TorBox cache status is one cheap batch call, so it feeds the score (+20) before anything is probed.
    torbox = None
    # This play's checkcached answer (orders and gates the TorBox adds), never `rd_cached`:
    # that one is persisted in stored searches (searches.py `_ROW_KEYS`) and can be stale.
    cached: dict = {}
    checked = False                              # `checkcached` answered (an empty answer is "none cached")
    # The per-resolve client built above (only when `torbox_usable`), the one the pinned attempt used.
    if torbox_client is not None and guard.remembered("torbox") is None:
        torbox = torbox_client
        try:
            cached = torbox.check_cached([c["info_hash"] for c in ranked if c.get("info_hash")])
            checked = True
        except StageError as exc:
            log.warn("torbox.checkcached", "skip", result="FAIL", error=str(exc))
            cached = {}
        if cached:
            for cand in ranked:
                entry = cached.get(cand["info_hash"].lower())
                if entry:
                    cand["rd_cached"] = True
                    cand["terms"]["avail.rd_cached"] = 20
                    cand["score"] = round(cand["score"] + 20, 1)
                    if entry.get("files"):
                        # cached packs: the file list is free here, read as every file list is
                        cand["files"] = pack_files(entry["files"], log, "torbox.checkcached",
                                                   hash=cand["info_hash"].lower())
            log.info("score", "recomputed", result="PASS", reason="torbox cache bonus",
                     cached=sum(1 for c in ranked if c.get("rd_cached")))
            order_ranked(ranked, log, lang, "torbox_cache")      # a bare sort undid the verified-language rule
    if reused is not None and str(reused.get("info_hash") or "").lower() not in cached:
        # Not cached (any more), or TorBox not asked: a download or a wait is the full search's to decide (a release
        # cached meanwhile plays at once there), with the one-add rule's lookup and `prefetched_first`.
        raise reuse_off("TorBox does not have the prefetched release cached" if checked else
                        "TorBox's cache could not be read for the prefetched release")
    learn_pack_files(ranked, http, log, settings, getattr(args, "file_source", None), picked=bool(picked),
                     guard=guard)
    no_file: list = []
    ranked = pick_pack_files(ranked, log, settings, lang, rejected_sink=no_file)
    for cand in no_file:
        search.reject(cand, "pack_files")
    search.set_passed(ranked)
    if not ranked:
        if picked:
            raise StageError("score.file_pick", f"The pack you picked has no file for episode {args.episode}.",
                             "choose another release")
        raise StageError("score.file_pick", f"no release contains episode {args.episode}", "pick another release or wait for a new one")
    result["candidates"] = public_candidates(ranked)
    if getattr(args, "prefetch", False):
        busy = (prefetchmod.play_starting(args.anilist_id, args.episode)
                or prefetchmod.list_written_since(args.anilist_id, args.episode,
                                                  getattr(args, "prefetch_started", None)))
        if busy:
            # The prefetch's list is the episode's stored search, unless the play of the episode has claimed (its own
            # search writes the list) or anything wrote the list since the prefetch started: newer than this one.
            search.keep = False
            log.info("releases.cache", "write", result="SKIP", anilist=args.anilist_id, episode=args.episode,
                     reason=f"{busy}: its own search writes the episode's list")
    search.save()
    if ranked_sink is not None:
        ranked_sink[:] = (release_donor_pool(ranked[0], donor_releases, args.anilist_id, args.episode)
                          if picked or reused is not None else donor_pool(ranked, donor_releases))

    if dry_run:
        result["chosen"] = result["candidates"][0]
        log.info("resolve", "done", result="PASS", ms=(time.monotonic() - t_all) * 1000, dry_run=True)
        return result

    probe_errors: list[dict] = []
    reuse_learned: list[tuple] = []           # the attempt's probe answers, kept once its release plays
    # The outlook: computed once, logged once, and carried on the failure so the message can be built
    # from what the run observed rather than from the stage names it happened to pass through.
    outlook = local_outlook(ranked, releases, dropped, settings)
    result["outlook"] = outlook
    log.info("resolve", "outlook", result="PASS", **outlook,
             reason="what this run knows about its own chances before any provider is asked")
    dubs = mark_dubs(ranked, log, settings, args.anilist_id, picked, own_titles)
    result["dub_only"] = dubs["only_dubs"]
    if reused is not None and dubs["only_dubs"]:
        # A list of one dub is "every release a dub" to the dub rule: whether an original exists is the search's.
        raise reuse_off("the prefetched release reads as a dub (dubs=avoid)")
    # The prefetched release may play without the viewer's language where the search's `prefer` fallback would
    # (`play_prefetched`, `prefetch.sideload_reuse`); else a release known to lack it is the search's to decide.
    sideload_reuse = reused is not None and bool(getattr(args, "prefetch_sideload", False))
    if reused is not None and ranked[0].get("has_lang") is False and not sideload_reuse:
        # Known to lack the viewer's language: whether another release carries it is the search's to find.
        raise reuse_off(f"the prefetched release has no {lang} subtitles "
                        f"({getattr(args, 'prefetch_sideload_why', '')})", langs=ranked[0].get("verified_langs"))
    if reused is not None and ranked_sink is not None:
        # The side-load's pool is the one the prefetch's search built, not the stored search's bare releases.
        ranked_sink[:] = reused_donor_pool(log, args, ranked[0], donor_releases)
    nothing_cached = torbox_nothing_cached(log, args, ranked, cached, checked, torbox is not None)
    # The order TorBox's releases are tried in: the play's own, except that a download the next-episode prefetch left
    # on the account is tried first among the uncached ones (`prefetched_first`), so this play adds nothing beside it.
    torbox_order = ranked if reused is not None else prefetched_first(log, args, ranked, cached, torbox, dubs)
    if getattr(args, "prefetch", False):
        # What the play's reuse reads of this search (`reuse_record`, `prefetch.sideload_reuse`): each cached release
        # TorBox's loop below tries (no dub by its name under `avoid`), with what the ranking read of its language.
        args.prefetch_cached = [{"infoHash": str(c.get("info_hash") or "").lower(), "hasLang": c.get("has_lang")}
                                for c in ranked if str(c.get("info_hash") or "").lower() in cached
                                and not (dubs["avoid"] and c.get("dub_words"))]

    def finish(cand: dict, stream: dict) -> dict | None:
        """Final gate: the stream must answer HTTP. None means "count it as a failed probe and go on"."""
        stream["range"] = range_check(http, log, stream["download"])
        if not stream["range"].get("reachable", True):
            probe_errors.append({"provider": stream.get("provider"), "id": cand["id"], "stage": "stream.probe",
                                 "error": f"unreachable ({stream['range'].get('error')})"})
            return None
        for learned in reuse_learned:
            keep_tracks(*learned)              # the attempt's release plays: what its probe read is kept now
        reuse_learned.clear()
        # `candidates` is the public top ten, but TorBox tries cached releases first and Real-Debrid walks the whole
        # list, so the chosen one may rank lower. The top ten's record carries what verify wrote onto it.
        key = release_key(cand)
        listed = next((c for c in result["candidates"] if release_key(c) == key), None)
        result["chosen"] = listed if listed is not None else public_record(cand)
        result["stream"] = stream
        # Neither "Keep waiting" nor "Watch the dub now" (a stopgap, `--no-pin`) is a release the viewer chose to keep.
        if picked and not keep_waiting and not getattr(args, "no_pin", False):
            result["pin"] = pins.record_pick(log, args.anilist_id, args.episode, cand, files=stream.get("files"))
        told_by_watcher = (result.get("waited") or {}).get("pending") == "watcher"
        if not cand.get("dub_words") and not taken_unsettled(args) and not told_by_watcher:
            # The viewer is watching the episode in its original audio now: the request to be told about it is done.
            # (A dub they took meanwhile leaves it standing: they asked about the original.) A request this play took
            # at its wait's ready is settled by `play_stream` once it has played or told; one the watcher marked
            # first is the watcher's: it removes it once its notification went out, and tells again if it did not.
            clear_pending(log, args, "a play of the episode found an original-audio release it could stream")
        rank = next((n for n, c in enumerate(ranked, 1) if release_key(c) == key), None)
        log.info("resolve", "done", result="PASS", ms=(time.monotonic() - t_all) * 1000, chosen=cand["id"],
                 provider=stream["provider"], seekable=stream["range"]["seekable"], rank=rank,
                 in_candidates=listed is not None)
        return result

    def torbox_side_row(cand: dict, exc: ProviderUnavailableError) -> dict:
        """The probe row of an error on TorBox's own side, as `say.torbox_side_words` and the diagnosis read it."""
        return {"provider": "torbox", "id": cand["id"], "stage": exc.stage, "error": str(exc),
                "unavailable": exc.data.get("unavailable"), "suggest": exc.suggest,
                "error_code": exc.data.get("error_code"), "retry_after": exc.data.get("retry_after"),
                "sent": exc.data.get("sent")}

    def gave_up(provider: str, exc: StageError, ms: float) -> bool:
        """One blocked release. True once the consecutive run is conclusive and this provider is done.

        Both debrid branches answer a block in the same words, because the words are half the fix —
        the other half is not making the calls.
        """
        if not guard.record_block(provider, exc, ms):
            return False
        n = guard.counts.get(provider, 0)
        probe_errors.append(guard.note(provider, skipped=max(0, len(ranked) - n)))
        # `blocked`/`nextProvider` are read by `say.stage` itself, so
        # the sentence says the provider stopped answering and names what is tried next.
        # `next_provider_words`/`PROVIDER_NAMES` still ride along in `detail` for the trace, in the
        # log's own fuller vocabulary.
        next_key = next_provider_key(provider, settings["providers"])
        say(provider, saymod.stage(provider, {"blocked": n, "nextProvider": next_key}),
            provider=provider, blocked=n, nextProvider=next_key,
            next=next_provider_words(provider, settings["providers"]))
        return True

    budget = int(settings["probeCount"])
    # The wait for TorBox's download that reached its cap without the episode (`providers.KEEP_WAITING_MAX_S`): the play
    # holds the viewer's choices once every provider has had its turn (`Held`), instead of failing.
    held = None
    # What the user is waiting for, in their words: `saymod.stage(provider)`.
    # A stage nobody can read is the same as no stage at all.
    for provider in settings["providers"]:
        # The verdict from a recent play, if it is still live. Skipping here is what makes
        # the *second* click cheap; the cap below is what makes the first one cheap.
        remembered = guard.remembered(provider)
        if remembered is not None:
            probe_errors.append(guard.note(provider, remembered=remembered))
            ago = max(0, round(time.time() - float(remembered.get("at") or 0)))
            next_key = next_provider_key(provider, settings["providers"])
            say(provider, saymod.stage(provider, {"skipped": True, "nextProvider": next_key}),
                provider=provider, skipped=True, nextProvider=next_key,
                consecutive=remembered.get("consecutive"), ago=ago,
                next=next_provider_words(provider, settings["providers"]))
            continue
        say(provider, saymod.stage(provider), provider=provider)
        if provider == "torbox":
            if torbox is None:
                continue
            probed = 0
            fallback = None  # best cached stream without the preferred language (plays when `fallback_plays`)
            # Under `require`, a donor or a subtitle-database file for the fallback release itself makes
            # it as good a fallback as under `prefer` (the play's side-load puts the track on screen);
            # without one `require` still refuses it. Worked out once, when the fallback is set.
            fallback_cover = None
            fallback_plays = False
            # `dubs=avoid`: the best cached dub set aside ((release, its probed audio)), and whether this
            # play's one uncached add (in its place, or with nothing cached at all) has been made.
            dub_held = None
            waited = False
            if keep_waiting:
                dub_held = held_dub_from_search(args, log, dubs["country"], own_titles)
            # The downloads this search's own adds started (`TorBox.downloads`; a pinned attempt before it shares the
            # client, and its pack is not this search's one download).
            downloads_before = len(getattr(torbox, "downloads", None) or [])

            def one_download():
                """The play allows one uncached download (a held dub, Keep waiting, or nothing cached), and TorBox
                accepted it in this search: its record, else None. Whatever the reads after the add do, no other
                release is added once it exists."""
                if not ((dubs["avoid"] and dub_held is not None) or keep_waiting or nothing_cached):
                    return None
                mine = (getattr(torbox, "downloads", None) or [])[downloads_before:]
                return mine[0] if mine else None

            def accepted_but_unread(cand: dict, exc: StageError) -> dict | None:
                """The one add was accepted but the read after it failed (a failed `mylist?id=`, "not listed", an
                error on TorBox's side): the stream to wait on, by its torrent id (`wait_ready` waits past failed
                polls, so the play holds rather than failing); None when that is not this case."""
                got = one_download()
                if waited or got is None or got["info_hash"] != str(cand.get("info_hash") or "").lower():
                    return None
                # The next-episode prefetch's download, still in TorBox's queue: waited for there by its hash, and the
                # wait then runs on the id TorBox lists it under (`prefetch.settle_queued`).
                settled = prefetchmod.settle_queued(torbox, log, cand, got, say)
                if settled is not None:
                    return settled
                log.warn("torbox.createtorrent", "accepted", result="WAIT", id=cand["id"], torrent_id=got["torrent_id"],
                         reused=got["reused"], stage_failed=exc.stage, error=str(exc),
                         error_code=exc.data.get("error_code"),
                         reason="TorBox has this play's one download but the read after it failed: the play waits on "
                                "it by its torrent id, and no other release is added",
                         suggest="the wait's polls read it again; see torbox.wait poll")
                return {"provider": "torbox", "cached": False, "torrent_id": got["torrent_id"], "status": None,
                        "progress": 0, "reused": got["reused"]}
            # Releases checkcached called cached first (stable, rank order kept within each half),
            # so an uncached add, which starts a download, happens only once no cached release played.
            for cand in sorted(torbox_order, key=lambda c: str(c.get("info_hash") or "").lower() not in cached):
                if probed >= budget:
                    break
                if (fallback is not None and fallback_plays
                        and str(cand.get("info_hash") or "").lower() not in cached):
                    # The cached fallback plays unless a *cached* release does better, so an
                    # uncached add now would only start a download this play never uses. The order
                    # above puts every cached release first, so the rest are all uncached.
                    log.info("torbox.createtorrent", "skip", result="SKIP", id=cand["id"], fallback=fallback[0]["id"],
                             not_added=sum(1 for c in ranked if str(c.get("info_hash") or "").lower() not in cached),
                             covered_by=cover_kind(fallback_cover),
                             reason="a cached release without the preferred subtitles is in hand "
                                    f"(subtitlePolicy={settings['subtitlePolicy']}); "
                                    "not adding releases TorBox has not cached")
                    break
                if waited and str(cand.get("info_hash") or "").lower() not in cached:
                    # The one uncached add was made and waited for, and what it brought did not play (its probed
                    # audio was a dub, or its stream failed): no second add starts another download.
                    log.info("torbox.createtorrent", "skip", result="SKIP", id=cand["id"],
                             nothing_cached=nothing_cached,
                             reason="this play's one uncached add was made and waited for"
                                    + ("" if nothing_cached else " (dubs=avoid: TorBox had only a dub)"))
                    break
                got = one_download()
                if got is not None and str(cand.get("info_hash") or "").lower() not in cached:
                    log.info("torbox.createtorrent", "skip", result="SKIP", id=cand["id"], torrent_id=got["torrent_id"],
                             nothing_cached=nothing_cached,
                             reason=f"TorBox accepted this play's one uncached add (torrent {got['torrent_id']}): "
                                    "no second download, whatever the reads after it did")
                    break
                if dubs["avoid"] and cand.get("dub_words"):
                    in_cache = str(cand.get("info_hash") or "").lower() in cached
                    if in_cache and dub_held is None:
                        dub_held = (cand, None)
                    if in_cache:
                        # A copy probed earlier turned out dubbed (ranked above this one): the failure names this dub
                        # TorBox has too, as a pick in Browse.
                        for row in probe_errors:
                            if row.get("stage") == "verify.audio" and not row.get("dub_cached"):
                                row["dub_cached"] = dub_held_words(cand, None)
                    log.info("verify", "dub_skip", result="SKIP", id=cand["id"], words=cand["dub_words"], rule="title",
                             cached=in_cache, held=dub_held is not None and dub_held[0] is cand,
                             reason="dubs=avoid: a dub is not tried while an original-audio release exists")
                    continue
                if cand["parsed"].get("batch") and not cand.get("rd_cached"):
                    # Unknown is not zero. Only the viewer's pick reaches here without a count (the
                    # prefilter drops the rest as "no seeders"): a pin, or a pick the index gave none for.
                    seeders = cand.get("seeders")
                    mine = str(cand.get("info_hash") or "").lower() == picked
                    if seeders is None and mine:
                        queue, why = True, "seeders unknown; queued because the viewer chose it"
                    elif (seeders or 0) >= PACK_MIN_SEEDERS:
                        queue, why = True, f"{seeders} seeders (at least {PACK_MIN_SEEDERS}); queued"
                    else:
                        queue, why = False, f"{seeders or 0} seeders (fewer than {PACK_MIN_SEEDERS}); not queued"
                    if seeders is None:
                        source = None
                    else:
                        source = cand.get("seeders_source") or "index"
                    log.info("torbox.pack_seeders", "decision", result="PASS" if queue else "SKIP", id=cand["id"],
                             seeders=seeders, known=seeders is not None, picked=mine, source=source,
                             minimum=PACK_MIN_SEEDERS, reason=f"TorBox has not cached this pack: {why}",
                             suggest="" if queue else
                             f"the pack starts a download only with {PACK_MIN_SEEDERS} or more seeders")
                    if not queue:
                        probe_errors.append({"provider": "torbox", "id": cand["id"], "stage": "torbox.skip",
                                             "skipped": "seeders", "seeders": seeders or 0,
                                             "error": f"a pack TorBox has not cached, with {seeders or 0} seeders; "
                                                      "not queued"})
                        continue
                probed += 1
                # Same message every time on purpose ("once per phase, never per
                # candidate release" — the history this feeds must not grow one entry per
                # candidate); the write still happens each time so `updatedAt` stays fresh and the
                # popout does not read a probe loop as an abandoned play.
                say("torbox", saymod.stage("torbox"), provider="torbox", tried=probed, budget=budget)
                t_probe = time.monotonic()
                # A pinned pack TorBox has not cached may already be on the account
                # from an earlier play; look there before adding it again.
                uncached = str(cand.get("info_hash") or "").lower() not in cached
                # This play's one uncached add (in place of a held dub, or with nothing cached), and "Keep waiting",
                # find a download already on the account (an earlier play of the episode made it) instead of adding
                # it again (`reused`).
                waits = keep_waiting or (not waited and ((dubs["avoid"] and dub_held is not None) or nothing_cached))
                # The account is listed once per play (`TorBox.listed`), so later lookups cost no request: a download
                # an earlier play left there is still found after an add was refused (no server free, the limit).
                reuse = uncached and (cand.get("source") == "pin" or waits)
                try:
                    stream = (torbox.resolve(cand, reuse_listed=True, lookup_only=waits and cand.get("source") != "pin")
                              if reuse else torbox.resolve(cand))
                except BlockedError as exc:
                    # 401/403: the token or the plan, the same answer for every release. A 403
                    # `AUTH_ERROR` is not one of these: it comes as `ProviderUnavailableError` below.
                    probe_errors.append({"provider": "torbox", "id": cand["id"], "stage": exc.stage, "error": str(exc)})
                    if gave_up("torbox", exc, (time.monotonic() - t_probe) * 1000):
                        break
                    continue
                except ProviderUnavailableError as exc:
                    # An error on TorBox's own side (`providers.TORBOX_SIDE`: no download server
                    # free, or a database, download-server or account-check error at any step). Not the release, so the next
                    # candidate is tried; not the account, so the provider block guard is left exactly as it was:
                    # neither a block nor proof TorBox works.
                    probe_errors.append(torbox_side_row(cand, exc))
                    if exc.data.get("sent") is False:
                        # An add the client did not send costs nothing, or two uncached
                        # releases ranked above a cached one use up `probeCount` and the cached one,
                        # which TorBox would still serve, is never tried.
                        probed -= 1
                    log.info("provider.block", "untouched", result="SKIP", provider="torbox", id=cand["id"],
                             error_code=exc.data.get("error_code"), sent=exc.data.get("sent"), probed=probed,
                             budget=budget,
                             unavailable=exc.data.get("unavailable"), stage_failed=exc.stage,
                             reason=f"TorBox refused on its own side ({exc.data.get('error_code')}), not the account; "
                                    f"the block guard is neither extended nor reset")
                    stream = accepted_but_unread(cand, exc)
                    if stream is None:
                        continue
                    probe_errors.pop()               # the wait's own row says what became of it
                except StageError as exc:
                    guard.record_ok("torbox")       # a real answer, so the block run is broken
                    stream = accepted_but_unread(cand, exc)
                    if stream is None:
                        probe_errors.append({"provider": "torbox", "id": cand["id"], "stage": exc.stage,
                                             "error": str(exc)})
                        continue
                guard.record_ok("torbox")
                if not stream["cached"]:
                    queued = {"provider": "torbox", "id": cand["id"], "stage": "torbox.mylist",
                              "error": f"uncached ({stream['status']} {stream.get('progress')}%)",
                              "torrent_id": stream["torrent_id"], "queued": True,
                              "progress": stream.get("progress"), "status": stream.get("status"),
                              "reused": bool(stream.get("reused"))}
                    probe_errors.append(queued)
                    if not ((dubs["avoid"] and dub_held is not None) or keep_waiting or nothing_cached) or waited:
                        continue
                    # TorBox had only a dub cached (`dubs=avoid`), or nothing at all, so this play's one uncached add
                    # (the best release that would play) is waited for; no other release is added either way. The
                    # viewer gets the choice at once: the dub now (when there is one), keep waiting, or be told when it
                    # is ready.
                    waited = True
                    if dub_held is not None:
                        queued["dub_cached"] = dub_held_words(*dub_held)
                    # The words follow what is on offer, not this play's own `checkcached`: no dub held, and either
                    # nothing cached or "Keep waiting" from a hold that offered no dub (`--wait` without `--dub`).
                    no_dub_on_offer = dub_held is None and (nothing_cached or (
                        keep_waiting and not getattr(args, "dub", None)))
                    facts = choice_facts(http, log, args, settings, cand, stream, dub_held, dubs["country"], alt,
                                         show=series_title, nothing_cached=no_dub_on_offer)
                    # Offered only while something can act on it: `wait_for_torbox` puts it on the entry once its
                    # handlers are in place and takes it off before they go; a hold puts it back (`Held`). "Keep
                    # waiting" from a hold already answered the question: that wait offers it confirmed (no sentence,
                    # no `w`; Tell me and the dub stay).
                    choice = wait_choice(args, facts, held=False, confirmed=keep_waiting)
                    t_wait = time.monotonic()
                    wait_began = time.time()
                    try:
                        waited_for = wait_for_torbox(torbox, log, cand, stream, settings, say, holder=status,
                                                     choice=choice, until_done=keep_waiting,
                                                     nothing_cached=facts["nothingCached"],
                                                     confirmed_choice=wait_choice(args, facts, held=False,
                                                                                  confirmed=True))
                    except Interrupted:
                        raise
                    except BlockedError as exc:
                        # A poll answered that TorBox refuses the account: read as the resolve reads it (the block
                        # guard, then the next provider); the download stays queued, so its row stays too.
                        probe_errors.append({"provider": "torbox", "id": cand["id"], "stage": exc.stage,
                                             "error": str(exc)})
                        gave_up("torbox", exc, (time.monotonic() - t_wait) * 1000)
                        break
                    if not waited_for.get("ready"):
                        queued.update(stage="torbox.wait", progress=waited_for.get("progress"),
                                      status=waited_for.get("status"), waited_s=waited_for.get("waited_s"),
                                      polls=waited_for.get("polls"),
                                      error=f"still downloading after {waited_for.get('waited_s')} s "
                                            f"({waited_for.get('status')} {waited_for.get('progress')}%)")
                        # The last poll's reading, with the time left the wait had from TorBox's bytes (`left_s`):
                        # the hold says the percentage, and the time left only when the wait had one.
                        held = {"facts": facts, "ended": waited_for.get("ended") or "budget",
                                "reading": {"state": waited_for.get("status"),
                                            **{k: waited_for.get(k) for k in ("progress", "eta", "downloaded", "size",
                                                                               "finished", "present", "left_s")}}}
                        break
                    # An earlier request to be told about the episode: this play takes it now (`take_pending`, under
                    # the pending lock), so exactly one of this play and the watcher tells the viewer. The play settles
                    # it once it has played it or told (`settle_taken`); if it ends without that, the request is given
                    # back to the watcher.
                    # What the play that loads it needs to know: it came from a wait, and how long that wait was
                    # (`ready_unseen`, the ready rule's one input): from the claim (the wall clock's moment, so a
                    # suspend counts; the wait's start for a resolve with no claim) to the moment TorBox had it, before
                    # the link and the load. `confirmed` ("Keep waiting" during the wait) is only logged.
                    ready_at = time.time()
                    claimed = getattr(status, "started_at", None)
                    result["waited"] = {"original": facts.get("original"), "show": series_title,
                                        "subtitles": facts["release"].get("subtitles"),
                                        "until_done": keep_waiting, "pending": take_pending(log, args),
                                        "claimed_at": claimed or wait_began, "ready_at": ready_at,
                                        "measured_from": "claim" if claimed else "wait_start",
                                        "confirmed": waited_for.get("confirmed_at") is not None}
                    # Downloaded: the link is asked for as the resolve asks for it, and its errors are its words,
                    # with the cached dub named (allowing dubs plays it now).
                    probe_errors.remove(queued)
                    t_link = time.monotonic()
                    try:
                        stream = torbox.link(cand, stream["torrent_id"])
                    except Interrupted:
                        raise
                    except BlockedError as exc:
                        probe_errors.append({"provider": "torbox", "id": cand["id"], "stage": exc.stage,
                                             "error": str(exc)})
                        gave_up("torbox", exc, (time.monotonic() - t_link) * 1000)
                        break
                    except ProviderUnavailableError as exc:
                        probe_errors.append({**torbox_side_row(cand, exc), "dub_cached": queued.get("dub_cached")})
                        log.info("provider.block", "untouched", result="SKIP", provider="torbox", id=cand["id"],
                                 error_code=exc.data.get("error_code"), sent=exc.data.get("sent"), probed=probed,
                                 budget=budget, unavailable=exc.data.get("unavailable"), stage_failed=exc.stage,
                                 reason=f"TorBox refused on its own side ({exc.data.get('error_code')}), not the "
                                        f"account; the block guard is neither extended nor reset")
                        break
                    except StageError as exc:
                        guard.record_ok("torbox")
                        probe_errors.append({"provider": "torbox", "id": cand["id"], "stage": exc.stage,
                                             "error": str(exc), "dub_cached": queued.get("dub_cached")})
                        break
                    if not stream["cached"]:
                        probe_errors.append(queued)
                        break
                audio: list = []
                if cand.get("has_lang") is not True:
                    # No subtitle tracks known for this release; read the container header instead.
                    langs = probe_tracks(log, stream["download"], audio_sink=audio)
                    if langs is not None:
                        cand["verified_langs"] = langs
                        cand["has_lang"] = has_language(langs, lang)
                        learned = (cand["info_hash"], (cand.get("parsed") or {}).get("stats_key", ""), langs, lang)
                        if reused is not None:
                            # The attempt's probe stays in this play's memory until the release plays (`finish`):
                            # the fallback's search then reads the cache a play that searched reads, and probes it.
                            reuse_learned.append(learned)
                        else:
                            keep_tracks(*learned)
                        for c in result["candidates"]:
                            if release_key(c) == release_key(cand):
                                c["verified_langs"], c["has_lang"] = langs, cand["has_lang"]
                        log.info("verify", "decision", result="PASS" if cand["has_lang"] else "MISS", id=cand["id"],
                                 has_lang=cand["has_lang"], langs=sorted(set(langs)))
                        if propagate_verification(ranked, log):
                            for c in ranked:
                                if c.get("inferred_from_sibling") and c.get("has_lang") is None:
                                    c["has_lang"] = has_language(c["verified_langs"], lang)
                if audio:
                    # The audio rule: every track tagged and none in the show's original language.
                    is_dub = audio_dub(audio, dubs["country"])
                    log.info("verify", "audio", result="MISS" if is_dub else "PASS" if is_dub is False else "SKIP",
                             id=cand["id"], audio=audio, country=dubs["country"], dub=is_dub,
                             acted=bool(is_dub and dubs["avoid"]),
                             reason="no track in the show's original language" if is_dub else
                                    "a track in the show's original language" if is_dub is False else
                                    "the country or a track's language is unknown: the name alone decides")
                    if is_dub and dubs["avoid"]:
                        cached_dub = dub_held      # a dub TorBox had cached all along (the held dub), still named
                        if dub_held is None:
                            dub_held = (cand, audio)
                        # Said if nothing else plays: TorBox has it (downloaded for this play, when it was the one
                        # download), and it is a dub (`say.dubbed_words`).
                        row = {"provider": "torbox", "id": cand["id"], "stage": "verify.audio",
                               "dubbed": dub_held_words(cand, audio)["lang"] or "",
                               "downloaded": bool(result.get("waited")),
                               "error": f"its audio is a dub ({', '.join(audio)})"}
                        if cached_dub is not None:
                            row["dub_cached"] = dub_held_words(*cached_dub)
                        probe_errors.append(row)
                        continue
                if cand.get("has_lang") is True or cand.get("has_lang") is None:
                    done = finish(cand, stream)
                    if done is not None:
                        return done
                if reused is not None and not (cand.get("has_lang") is False and sideload_reuse):
                    # No other release was tried for the viewer's language: the full search decides, this one in it
                    # (under `prefer` it is still its fallback). An unreachable stream has its row: left out there.
                    # Where the prefetch's search had no other cached release its loop would try, it is the fallback
                    # here too.
                    if cand.get("has_lang") is False:
                        raise reuse_off(f"the prefetched release has no {lang} subtitles "
                                        f"({getattr(args, 'prefetch_sideload_why', '')})",
                                        langs=cand.get("verified_langs"))
                    continue
                if fallback is None:
                    fallback = (cand, stream)
                    if settings["subtitlePolicy"] == "require":
                        same = sideload_info is not None and precheck_key == release_key(cand)
                        fallback_cover = sideload_info if same else require_cover(cand)[0]
                        log.info("verify", "fallback_cover", result="PASS" if fallback_cover else "MISS",
                                 id=cand["id"], covered_by=cover_kind(fallback_cover), reused=same,
                                 reason="the pre-check's answer is about this release" if same else
                                        "looked up for this release" if as_bool(settings.get("sideloadSubtitles"), True)
                                        else "sideloadSubtitles is off: nothing is side-loaded, nothing looked up")
                    fallback_plays = settings["subtitlePolicy"] == "prefer" or fallback_cover is not None
            if fallback and fallback_plays:
                cand, stream = fallback
                kind = cover_kind(fallback_cover)
                if kind is None:
                    why = "policy=prefer"
                elif kind == "external":
                    why = (f"policy=require, covered by {fallback_cover['external'].get('service')}'s "
                           f"{fallback_cover['external'].get('name')}")
                else:
                    why = f"policy=require, covered by a donor: {(fallback_cover.get('donor') or {}).get('title')}"
                if kind is not None:
                    result["sideload"] = fallback_cover      # what `sideload` says is about the release that plays
                log.warn("verify", "fallback", result="PASS", id=cand["id"], covered_by=kind,
                         reason=f"no {lang} track in probed candidates; {why}",
                         suggest="if no track shows, press p to choose a release that carries it" if kind else
                                 "set subtitlePolicy=require to refuse, or pick manually")
                stream["subtitle_fallback"] = True
                done = finish(cand, stream)
                if done is not None:
                    return done
            if fallback and not fallback_plays:        # a fallback that played and failed has its own row
                probe_errors.append({"provider": "torbox", "id": fallback[0]["id"], "stage": "verify",
                                     "error": f"cached but no {lang} subtitles (policy=require)"})
        elif provider == "realdebrid":
            if not (CONFIG_DIR / "realdebrid-token").exists():
                continue
            rd = RealDebrid(http, log, read_token("realdebrid"))
            probed = 0
            for n, cand in enumerate(ranked, 1):
                if probed >= budget:
                    break
                if dubs["avoid"] and cand.get("dub_words"):
                    log.info("verify", "dub_skip", result="SKIP", id=cand["id"], words=cand["dub_words"], rule="title",
                             provider="realdebrid",
                             reason="dubs=avoid: a dub is not tried while an original-audio release exists")
                    continue
                say("realdebrid", saymod.stage("realdebrid"), provider="realdebrid", tried=n, of=len(ranked))
                t_probe = time.monotonic()
                try:
                    stream = rd.resolve(cand, settings["rdPollSeconds"])
                except BlockedError as exc:
                    # Still does not consume the budget — one blocked release is the release's
                    # business, not the user's. What it does consume is the guard's
                    # consecutive count: `probeCount` bounds this loop only on the paths that
                    # increment `probed`, and without the guard only `len(ranked)` would bound this one.
                    probe_errors.append({"provider": "realdebrid", "id": cand["id"], "stage": exc.stage,
                                         "error": str(exc)})
                    if gave_up("realdebrid", exc, (time.monotonic() - t_probe) * 1000):
                        break
                    continue
                except StageError as exc:
                    probed += 1
                    guard.record_ok("realdebrid")   # uncached, dead, rate-limited: not a block
                    probe_errors.append({"provider": "realdebrid", "id": cand["id"], "stage": exc.stage, "error": str(exc)})
                    continue
                probed += 1
                guard.record_ok("realdebrid")
                if stream["cached"]:
                    stream["provider"] = "realdebrid"
                    done = finish(cand, stream)
                    if done is not None:
                        return done
                probe_errors.append({"provider": "realdebrid", "id": cand["id"], "stage": "rd.info",
                                     "error": f"uncached ({stream['status']} {stream.get('progress')}%)",
                                     "torrent_id": stream["torrent_id"], "queued": True,
                                     "progress": stream.get("progress"), "status": stream.get("status")})
    if held is not None:
        # No provider played it: the play stops waiting and holds the choices (not a failure, not a refusal).
        choice = wait_choice(args, held["facts"], held=True, reading=held["reading"])
        line = saymod.held_line(held["reading"])
        log.info("torbox.wait", "held", result="PASS", ended=held["ended"], torrent_id=held["facts"]["release"].get(
                 "torrentId"), state=held["reading"].get("state"), progress=held["reading"].get("progress"),
                 eta=held["reading"].get("eta"), left_s=held["reading"].get("left_s"),
                 downloaded=held["reading"].get("downloaded"), line=line,
                 actions=[a["id"] for a in choice["actions"]],
                 reason="the wait ended without the episode; the play holds the viewer's choices instead of failing")
        raise Held("torbox.wait", line, "", choice=choice, reading=held["reading"], ended=held["ended"],
                   probes=probe_errors)
    side_error = getattr(torbox, "side_error", None)
    torbox_rows = [p for p in probe_errors if p.get("provider") == "torbox"]
    if side_error and all(saymod.probe_kind(p) == "skipped" for p in torbox_rows):
        # TorBox refused on its own side earlier in this command (the pinned attempt shares this
        # client), and this search made no TorBox add of its own (every TorBox row is a skip, or there is
        # none). That refusal is still why the viewer's choice did not play, so it is carried into this
        # diagnosis instead of a headline that blames the releases. A search whose own add got another
        # answer (e.g. DOWNLOAD_NOT_CACHED) keeps that answer as its diagnosis.
        probe_errors.append({"provider": "torbox",
                             "id": side_error.get("id") or str(side_error.get("info_hash")),
                             "stage": side_error.get("stage"), "error": side_error.get("message"),
                             "unavailable": side_error.get("unavailable"), "error_code": side_error.get("error_code"),
                             "suggest": side_error.get("suggest"),
                             "retry_after": side_error.get("retry_after"), "sent": True, "carried": True})
        log.info("torbox.unavailable", "carried", result="PASS", info_hash=side_error.get("info_hash"),
                 id=side_error.get("id"), error_code=side_error.get("error_code"),
                 retry_after_s=side_error.get("retry_after"),
                 reason="TorBox refused on its own side earlier in this command and this search made no "
                        "TorBox add of its own; the diagnosis names it")
    elif side_error and not saymod.torbox_unavailable(probe_errors):
        log.info("torbox.unavailable", "not_carried", result="SKIP", info_hash=side_error.get("info_hash"),
                 id=side_error.get("id"), error_code=side_error.get("error_code"),
                 unavailable=side_error.get("unavailable"), torbox_rows=len(torbox_rows),
                 reason="this search's own TorBox answers decide its diagnosis")
    result["probe_errors"] = probe_errors
    # The **message** is the diagnosis, because the message is what all three surfaces lead
    # with — the popout's headline, the panel's one line, and the player's overlay. The stage detail
    # moves into `suggest`, behind the action, where it is still there to be read.
    # The candidate titles ride along so the UI can say "N releases were found but none passed"
    # instead of a bare stage name.
    headline, _ = diagnose_failure(probe_errors, settings, outlook)
    log.info("resolve", "diagnosis", result="PASS", headline=headline,
             probes=len(probe_errors), **{k: outlook[k] for k in ("survivors", "unseeded", "debrid")},
             reason="what the user is told, and what it was computed from")
    raise failure_error(probe_errors, settings, outlook, [c["title"] for c in ranked[:5]])


# ----------------------------------------------------------------------------- dubs

def mark_dubs(ranked: list[dict], log: Log, settings: dict, anilist_id: int, picked, titles=()) -> dict:
    """Read every ranked release's name for a dub (`score.dub_reading`, into `dub_words`) and decide what the `dubs`
    setting does with them in this play: `avoid` (set dubs aside: an original-audio release exists), `only_dubs`
    (`avoid`, but every release is a dub: the dub plays, with a notice) or neither (`allow`, or the viewer's own pick,
    which plays whatever it is). `titles` are the entry's own (a dub word inside one is the show's name, not the
    audio). One `score dub` line per release marked, one `score dubs` line for the decision."""
    country = (known_titles(anilist_id) or {}).get("countryOfOrigin")
    setting = settings.get("dubs", DEFAULT_SETTINGS["dubs"])
    for c in ranked:
        c["dub_words"] = dub_reading(c.get("title") or "", country, titles)
        if c["dub_words"]:
            log.info("score", "dub", result="PASS", id=c.get("id"), title=c.get("title"), words=c["dub_words"],
                     rule="title", country=country, dubs=setting)
    marked = sum(1 for c in ranked if c.get("dub_words"))
    applies = setting == "avoid" and not picked
    out = {"avoid": applies and marked < len(ranked), "only_dubs": applies and 0 < marked == len(ranked),
           "country": country}
    if picked:
        why = "the viewer picked this release: it plays whatever its audio"
    elif setting != "avoid":
        why = "dubs=allow: dubs are ranked and played as before"
    elif out["only_dubs"]:
        why = "every release found is a dub: the best one plays, and the loading screen says so"
    elif marked:
        why = "dubs=avoid: no dub plays while an original-audio release exists"
    else:
        why = "no release name reads as a dub; probed audio is still read where a release is probed"
    log.info("score", "dubs", result="PASS", setting=setting, country=country, marked=marked,
             originals=len(ranked) - marked, avoid=out["avoid"], only_dubs=out["only_dubs"], picked=bool(picked),
             audio_rule=country is not None, reason=why)
    return out


def _dub_language(words) -> str | None:
    """The language a dub's words name (`English Dub`, `Eng Dub`, `GerDub`), as a person writes it; None if none."""
    for word in re.findall(r"[^\W\d_]+", str(words or "")):
        word = re.sub(r"dub\w*$", "", word.lower())
        for code, (aliases, names) in LANGUAGE_TABLE.items():
            if word and (word == code or word in aliases or word in names):
                return language_name(code)
    return None


def dub_held_words(cand: dict, audio: list | None) -> dict:
    """What the stop's clause names of a cached dub: its language (its words, else its probed audio), its group and
    its height (`say.dub_clause`)."""
    parsed = cand.get("parsed") or parse_title(cand.get("title") or "")
    lang = _dub_language(cand.get("dub_words")) or (language_name(audio[0]) if audio else None)
    return {"lang": lang, "group": parsed.get("group") or "", "resolution": parsed.get("resolution")}


def clear_pending(log: Log, args, why: str) -> bool:
    """Drop the episode's pending download, if there is one (`pending.remove`, logged with `why`)."""
    if not any(int(e.get("mediaId") or 0) == args.anilist_id and int(e.get("episode") or 0) == args.episode
               for e in pendingmod.load()):
        return False
    return pendingmod.remove(args.anilist_id, args.episode, log, why)


def take_pending(log: Log, args) -> str | None:
    """At a wait's ready, the episode's request to be told about it: taken by this play (`pending.mark`, under the
    lock, with this play's pid, so the watcher's own mark fails and it tells nothing), "play"; already told or marked by
    the watcher, "watcher"; None when there is no request."""
    entry = next((e for e in pendingmod.load() if int(e.get("mediaId") or 0) == args.anilist_id
                  and int(e.get("episode") or 0) == args.episode), None)
    if entry is None:
        return None
    tid = entry.get("torrentId")
    back = None if entry.get("notified") else pendingmod.mark(
        args.anilist_id, args.episode, tid, log, notified=time.time(), notified_by="play", notified_pid=os.getpid(),
        notified_why="a play of the episode waited for it and it is ready")
    if back is None:
        log.info("pending", "take", result="SKIP", mediaId=args.anilist_id, episode=args.episode, torrentId=tid,
                 reason="the watcher told the viewer, or marked the request, first: this play sends no notification")
        return "watcher"
    args.pending_taken = {"torrentId": tid, "settled": False}
    log.info("pending", "take", result="PASS", mediaId=args.anilist_id, episode=args.episode, torrentId=tid,
             reason="this play took the request: the watcher tells nothing for it")
    return "play"


def taken_unsettled(args) -> bool:
    taken = getattr(args, "pending_taken", None)
    return bool(taken) and not taken["settled"]


def settle_taken(log: Log, args, how: str, why: str) -> None:
    """The request this play took (`take_pending`): removed once the play has played the episode or told the viewer
    ("remove"); given back to the watcher otherwise ("unmark": the mark comes off and a watcher is started for it)."""
    if not taken_unsettled(args):
        return
    taken = args.pending_taken
    taken["settled"] = True
    if how == "remove":
        pendingmod.remove(args.anilist_id, args.episode, log, why, torrent_id=taken["torrentId"])
        return
    back = pendingmod.unmark(args.anilist_id, args.episode, taken["torrentId"], log, "notified", "notified_why",
                             "notified_by", "notified_pid")
    pid = watchermod.ensure(log, why, force=True) if back else None
    log.emit("info" if back else "warn", "pending", "give-back", result="PASS" if back else "FAIL",
             mediaId=args.anilist_id, episode=args.episode, torrentId=taken["torrentId"], watcher_pid=pid, reason=why,
             suggest="" if back else "the request still reads as told; the watcher drops it and the viewer is not told")


def held_dub_from_search(args, log: Log, country, titles) -> tuple | None:
    """"Keep waiting" (`--wait --dub <hash>`): the dub the waiting play held, from the episode's stored search, as
    `(release, None)` (`dub_held`'s shape), so the choice is offered again; None when the search no longer holds it."""
    rel = searches.find_release(args.anilist_id, args.episode, getattr(args, "dub", None)) if getattr(
        args, "dub", None) else None
    log.info("torbox.choice", "dub", result="PASS" if rel else "SKIP", info_hash=getattr(args, "dub", None),
             title=(rel or {}).get("title"),
             reason="the dub the waiting play held, from the stored search" if rel else
                    "no dub named, or the stored search no longer holds it: the choice offers no dub")
    if not rel:
        return None
    cand = dict(rel, parsed=parse_title(rel.get("title") or ""))
    cand["dub_words"] = dub_reading(cand.get("title") or "", country, titles)
    return cand, None


def release_donor_pool(target: dict, releases: list[dict], anilist_id, episode) -> list[dict]:
    """The side-load's donor pool for a play of one chosen release (`play --release`): the release, then a light
    candidate (`donor_pool`) for every release the episode's stored search saw. The dub's own play side-loads from this
    pool (`subs.prepare`), and the choice's subtitle promise (`dub_subtitles`) looks in the same one: the two agree."""
    return donor_pool([target], list(releases) + searches.raw_releases(anilist_id, episode))


def reused_donor_pool(log: Log, args, target: dict, releases: list[dict]) -> list[dict]:
    """The side-load's donor pool for the prefetch's release (`play_prefetched`): the pool the prefetch's search built
    (`donor_pool` over its ranking), saved as its shape (`donorPool`: each release's hash in the pool's order, and
    whether the ranking had read its tracks). The releases come from the episode's stored search (that search's), the
    release itself at its place, and the read ones' tracks are read again (`track_source`, the add-on's cache), so
    `find_donor` and the subtitle worker's timing references see what that search's pool held, not the stored
    releases in the index's order with no tracks (which sends `find_donor` to sweep them all). An entry with no shape
    (written before it), or a stored search that no longer holds one of its releases: `release_donor_pool`, as
    before. One `prefetch donors` line."""
    t0 = time.monotonic()
    shape = getattr(args, "prefetch_donors", None)
    own = str(target.get("info_hash") or "").lower()
    if not (isinstance(shape, list) and shape and all(isinstance(s, list) and len(s) == 2 for s in shape)):
        shape = None
    raw = ((searches.load(args.anilist_id, args.episode, None) or {}).get("raw") or {}) if shape else {}
    raw = {str(h).lower(): r for h, r in raw.items()}
    missing = [h for h, _ in shape or [] if str(h).lower() != own and str(h).lower() not in raw]
    if shape is None or missing:
        log.info("prefetch", "donors", result="SKIP", ms=(time.monotonic() - t0) * 1000, info_hash=own,
                 missing=len(missing),
                 reason="the stored search no longer holds every release of the prefetch's pool" if missing else
                        "the prefetch saved no pool (an entry written before it): the stored search's releases")
        return release_donor_pool(target, releases, args.anilist_id, args.episode)
    out, read, unread = [], 0, 0
    tracks = getattr(args, "track_source", None)
    for h, had_tracks in shape:
        h = str(h).lower()
        if h == own:
            out.append(target)
            continue
        cand = donor_pool([], [raw[h]])[0]
        cand.pop("dropped", None)                 # its place in the pool says what it was
        if had_tracks and tracks is not None and tracks.can_read(cand):
            got = tracks.read(cand)
            if got and got.get("tracks"):
                cand["tracks"], cand["tracks_from"] = got["tracks"], got["read_from"]
                read += 1
            else:
                unread += 1
        elif had_tracks:
            unread += 1
        out.append(cand)
    if own not in {str(h).lower() for h, _ in shape}:
        out.insert(0, target)
    log.info("prefetch", "donors", result="PASS", ms=(time.monotonic() - t0) * 1000, info_hash=own, releases=len(out),
             tracks_read=read, tracks_unread=unread,
             reason="the side-load's pool is the prefetch's search's, in its order")
    return out


def dub_subtitles(http: Http, log: Log, dub: dict, settings: dict, args, alt) -> str | None:
    """The viewer's subtitle language, by name, when a verified source puts it on screen for the dub's own play: the
    dub's verified tracks, else the donor that play's side-load would find. That is `subs.find_donor` over
    `release_donor_pool` (index only, nothing downloaded) with `subs.prepare`'s arguments, so the promise is the dub
    play's own lookup. None otherwise, `sideloadSubtitles` off or no add-on offering tracks included: the choice then
    names no subtitles. Never a subtitle database: that is a search against a daily quota, not a verified source."""
    lang = settings["subtitleLanguage"]
    if has_language(dub.get("verified_langs") or [], lang):
        log.info("torbox.choice", "subtitles", result="PASS", id=dub.get("id"), lang=lang, source="own tracks")
        return language_name(lang)
    if not as_bool(settings.get("sideloadSubtitles"), True):
        log.info("torbox.choice", "subtitles", result="SKIP", id=dub.get("id"), lang=lang,
                 reason="sideloadSubtitles is off: the dub plays with its own tracks only")
        return None
    tracks = getattr(args, "track_source", None)
    if tracks is not None and not tracks.offered:
        log.info("torbox.choice", "subtitles", result="SKIP", id=dub.get("id"), lang=lang,
                 reason="no loaded indexer add-on offers subtitle tracks, so the dub's play can side-load none")
        return None
    target = dict(dub)                       # the lookup fills in the target's tracks; the play's copy is its own
    try:
        scan = subsmod.find_donor(http, log, release_donor_pool(target, [], args.anilist_id, args.episode), target,
                                  settings, episode=args.episode, alt=alt,
                                  budget_s=min(subsmod.DONOR_BUDGET_S, subsmod.PREPARE_BUDGET_S), track_source=tracks)
    except Exception as exc:                 # noqa: BLE001 - a failed lookup names no subtitles, and never stops a play
        log.warn("torbox.choice", "subtitles", result="FAIL", id=dub.get("id"), lang=lang, error=str(exc),
                 suggest="the choice names no subtitles; the dub's play still side-loads what it finds")
        return None
    donor = (scan.get("donors") or [None])[0]
    log.info("torbox.choice", "subtitles", result="PASS" if donor else "MISS", id=dub.get("id"), lang=lang,
             source="donor" if donor else None, donor=(donor or {}).get("title"), examined=scan.get("examined"),
             reason="a verified track the dub's play side-loads" if donor else
                    "no verified source for the dub's play: the choice names no subtitles")
    return language_name(lang) if donor else None


def torbox_nothing_cached(log: Log, args, ranked: list[dict], cached: dict, checked: bool, asked: bool) -> bool:
    """Whether this play's `checkcached` answered and TorBox holds none of the releases that passed: then the play's
    one uncached add, the best-ranked release that would play, is waited for and the viewer's choices offered, as for
    a held dub. Not when `checkcached` failed (nothing is known), and not in a pinned pack's own attempt, which keeps
    its own rule (`play_pinned`: queued, the stuck clock, searched past). One `torbox.wait rule` line."""
    if not asked:
        log.info("torbox.wait", "rule", result="SKIP", nothing_cached=False, checked=False, ranked=len(ranked),
                 reason="TorBox is not asked in this play (no TorBox client, or a refusal the block guard remembers)")
        return False
    pinned = getattr(args, "pinned_release", None) is not None
    # "Keep waiting" from a play that held a dub (`--dub`): TorBox has that dub, so the dub's rule, not this one.
    dub_named = bool(getattr(args, "dub", None))
    hits = sum(1 for c in ranked if str(c.get("info_hash") or "").lower() in cached)
    applies = checked and not pinned and not dub_named and hits == 0
    # The next-episode prefetch runs this resolve too, and stops at its one add: it never waits or offers a choice.
    prefetch = bool(getattr(args, "prefetch", False))
    log.info("torbox.wait", "rule", result="PASS" if applies else "SKIP", nothing_cached=applies, checked=checked,
             cached=hits, ranked=len(ranked), pinned=pinned, dub_named=dub_named, prefetch=prefetch,
             reason=("TorBox has none of this episode's releases: the prefetch makes the one add and stops (it never "
                     "waits)" if prefetch else
                     "TorBox has none of this episode's releases: one add, waited for, with the viewer's choices")
             if applies else "checkcached did not answer: the adds are as before" if not checked else
             "TorBox has a release cached: those are tried first" if hits else
             "a pinned pack's own attempt keeps its rule" if pinned else
             "keep waiting on a play that held a dub TorBox has: the dub's rule")
    return applies


def prefetched_first(log: Log, args, ranked: list[dict], cached: dict, torbox, dubs: dict) -> list[dict]:
    """The order this play tries TorBox's releases in: `ranked`, except that the download the next-episode prefetch
    left for this episode (`prefetch.found`), when it is still on the account and TorBox has not cached it yet, goes
    first among the uncached ones. This play's own search may now rank another release first (a new upload); adding
    that one would start a second download beside the one nearly done, so the one on the account is tried first, as a
    pin's group is preferred: it must be among this search's releases (every filter passed), not a dub under
    `dubs=avoid`, and not lack the viewer's language while a release ranked above it carries it. Once TorBox has it
    cached, the cached releases are tried in rank order as before (no download either way). A pick, a play without a
    prefetch and a play with no TorBox client keep the order as it is, with no line."""
    got = None if torbox is None or getattr(args, "release", None) else prefetchmod.found(args.anilist_id,
                                                                                           args.episode)
    if got is None:
        return ranked
    want = str(got["infoHash"]).lower()

    def keep(why: str, result: str = "SKIP") -> list[dict]:
        log.info("prefetch", "prefer", result=result, info_hash=want, prefetch_trace=got.get("trace"),
                 torrent_id=got.get("torrentId"), reason=why)
        return ranked
    at = next((i for i, c in enumerate(ranked) if str(c.get("info_hash") or "").lower() == want), None)
    if at is None:
        return keep("the prefetched release is not among this search's releases; the play's own order")
    cand = ranked[at]
    if want in cached:
        return keep("TorBox has the prefetched release cached now: the cached releases are tried in rank order")
    if dubs.get("avoid") and cand.get("dub_words"):
        return keep("the prefetched release reads as a dub (dubs=avoid); the play's own order")
    above = [c for c in ranked[:at] if str(c.get("info_hash") or "").lower() not in cached]
    if cand.get("has_lang") is False and any(c.get("has_lang") is True for c in above):
        return keep("the prefetched release lacks the viewer's language and a release ranked above it carries it")
    queued = got.get("state") == prefetchmod.QUEUED
    if not above and not queued:
        return keep("the prefetched release is already the first uncached one", "PASS")
    try:
        listed = torbox.listed(want)
    except StageError as exc:
        return keep(f"the account's list could not be read ({exc}); the play's own order")
    source = "the account's list"
    if listed is None and queued:
        # A queued add is not on `mylist`: found in TorBox's queue, it joins this play's one listing, so the one-add
        # rule's lookup (`TorBox.listed`) finds it, and adds nothing.
        try:
            listed = prefetchmod.queued_entry(torbox, want)
        except Interrupted:
            raise
        except StageError as exc:
            return keep(f"the prefetched release is not on the account's list and TorBox's queue could not be read "
                        f"({exc}); the play's own order")
        source = "TorBox's queue"
        if listed is not None and isinstance(getattr(torbox, "account", None), list):
            torbox.account.append({"id": listed.get("id"), "hash": want, "download_state": prefetchmod.QUEUED})
            # What `prefetch.settle_queued` waits on, once the one-add lookup has reused it.
            torbox.prefetch_queued = {want: listed.get("id")}
    if listed is None:
        return keep("the prefetched release is in neither the account's list nor TorBox's queue any more; the play's "
                    "own order" if queued else "the prefetched release is no longer on the account; the play's own "
                    "order")
    if not above:
        return keep(f"the prefetched release is already the first uncached one (found on {source})", "PASS")
    log.info("prefetch", "prefer", result="PASS", info_hash=want, prefetch_trace=got.get("trace"),
             torrent_id=listed.get("id"), was_rank=at + 1, passed_over=[c.get("id") for c in above][:5],
             found_on=source,
             reason=f"the prefetched download is on {source}: tried first among the uncached releases, so no "
                    f"second download starts beside it")
    return [cand] + [c for c in ranked if c is not cand]


def added_audio(cand: dict, country) -> str | None:
    """The audio language the release TorBox is downloading is in, by name, as the choice and the ready notification
    say it: a dub's own language when its name reads as one (`dubs=allow`, or every release a dub), else the show's
    original language (`countryOfOrigin`); None when neither is known."""
    if cand.get("dub_words"):
        return _dub_language(cand.get("dub_words"))
    tags = (original_audio(country) or ((None,), ()))[0]
    return language_name(tags[0]) if tags[0] else None


def choice_facts(http: Http, log: Log, args, settings: dict, cand: dict, stream: dict, dub_held, country, alt,
                 show: str | None = None, nothing_cached: bool = False) -> dict:
    """What the viewer's choice is about, once, when this play's one uncached add was made: in place of a held dub,
    or with nothing cached at all (`nothing_cached`). The audio language of the release TorBox is downloading
    (`added_audio`), that release, the dub and its subtitles."""
    original = added_audio(cand, country)
    dub = None
    if dub_held is not None:
        words = dub_held_words(*dub_held)
        dub = {"infoHash": str(dub_held[0].get("info_hash") or "").lower(), "title": dub_held[0].get("title"),
               "lang": words["lang"], "group": words["group"], "resolution": words["resolution"],
               "subtitles": dub_subtitles(http, log, dub_held[0], settings, args, alt)}
    facts = {"original": original, "country": country, "dub": dub, "show": show or None,
             "nothingCached": bool(nothing_cached),
             # The one download is itself a dub: its language ("" when its name does not say), else None.
             "addedDub": (_dub_language(cand.get("dub_words")) or "") if cand.get("dub_words") else None,
             "release": {"infoHash": str(cand.get("info_hash") or "").lower(), "title": cand.get("title"),
                         "id": cand.get("id"), "torrentId": stream.get("torrent_id"),
                         # The language the original-audio release's own verified tracks carry: what "ready …
                         # with Arabic subtitles" may say of it.
                         "subtitles": language_name(settings["subtitleLanguage"]) if cand.get("has_lang") is True
                         else None},
             "subtitleLanguage": language_name(settings["subtitleLanguage"])}
    log.info("torbox.choice", "offer", result="PASS", original=original, country=country,
             dub=(dub or {}).get("title"), dub_lang=(dub or {}).get("lang"), subtitles=(dub or {}).get("subtitles"),
             release=cand.get("title"), torrent_id=stream.get("torrent_id"), reused=bool(stream.get("reused")),
             nothing_cached=bool(nothing_cached),
             reason="TorBox has none of the episode's releases; it is downloading the best one, so the viewer is "
                    "offered waiting, or being told when it is ready" if nothing_cached else
                    "TorBox has only a dub; it is downloading the original-audio release, so the viewer is offered "
                    "the dub now, waiting, or being told when it is ready")
    return facts


def wait_choice(args, facts: dict, held: bool, reading: dict | None = None, confirmed: bool = False) -> dict:
    """The viewer's choices as every surface shows them and runs them: the sentence (naming the show), then each
    action with its key and the CLI command it runs (surfaces add the binary and `--trace`). The dub's action only when
    TorBox has a dub; with nothing cached the two are "Keep waiting" and "Tell me when it's ready". Every one is live
    for the whole wait: during it "Keep waiting" (`confirm`, `w`) tells the waiting play the viewer is there
    and wants it (`keep-waiting`: a signal, no new process, no second add); held, it starts another wait on the same
    download (`--wait`, no second add) that waits until TorBox is done. `confirmed`: the viewer pressed it, so the
    question goes (no sentence, no "Keep waiting") and the dub and "Tell me" stay, for a change of mind."""
    media, ep = str(args.anilist_id), str(args.episode)
    dub, release = facts.get("dub"), facts["release"]
    actions = []
    if dub and dub.get("infoHash"):
        actions.append({"id": "dub", "key": "d",
                        "label": saymod.dub_choice_label(dub.get("lang"), dub.get("subtitles")),
                        "args": ["play", media, ep, "--release", dub["infoHash"], "--no-pin", "--show"]})
    if held:
        wait_args = ["play", media, ep, "--release", release["infoHash"], "--wait", "--show"]
        if dub and dub.get("infoHash"):
            wait_args[-1:-1] = ["--dub", dub["infoHash"]]
        actions.append({"id": "wait", "key": "w", "label": saymod.KEEP_WAITING, "args": wait_args})
    elif not confirmed:
        actions.append({"id": "confirm", "key": "w", "label": saymod.KEEP_WAITING, "args": ["keep-waiting", media, ep]})
    actions.append({"id": "later", "key": "n", "label": saymod.TELL_ME, "args": ["later", media, ep],
                    "ack": saymod.later_line(args.episode)})
    headline = "" if confirmed and not held else saymod.wait_headline(
        facts.get("show"), args.episode, facts.get("original"), held=held, reading=reading,
        nothing_cached=bool(facts.get("nothingCached")), added_dub=facts.get("addedDub"))
    out = {"headline": headline, "held": held, "confirmed": bool(confirmed and not held),
           "nothingCached": bool(facts.get("nothingCached")),
           "original": facts.get("original"), "subtitles": (dub or {}).get("subtitles"), "dub": dub,
           "show": facts.get("show"),
           "release": release, "actions": actions}
    if held:
        out["estimate"] = saymod.eta_words((reading or {}).get("left_s"))      # from TorBox's bytes, never its eta
        out["line"] = saymod.held_line(reading)
    return out


def wait_for_torbox(torbox: TorBox, log: Log, cand: dict, stream: dict, settings: dict, say, holder=None,
                    choice: dict | None = None, until_done: bool = False, nothing_cached: bool = False,
                    confirmed_choice: dict | None = None) -> dict:
    """The play waits for TorBox to download `cand` (added uncached in place of a cached dub, or with nothing cached
    at all: `nothing_cached`): `TorBox.wait_ready` until TorBox is done, the viewer chooses, or
    `providers.KEEP_WAITING_MAX_S` (then the caller holds), the status line giving TorBox's state and, once it reports
    progress, its percentage, the speed and the time left from its byte counts on every poll, every 3 s
    (`say.wait_line`). No estimate ends it. A stop meanwhile
    (`player stop`, or another play superseding this one: both send SIGTERM) ends the wait at once as `Interrupted`, and
    the viewer's "Tell me when it's ready" (`playstate.DEFER_SIGNAL`) as `Deferred`; the download stays in TorBox. The
    viewer's "Keep waiting" (`playstate.CONFIRM_SIGNAL`) only confirms it: within a second the entry's choice becomes
    `confirmed_choice` (the question gone, the line as it was), logged `torbox.wait confirmed`, and the answer carries
    `confirmed_at` (wall clock), which the log of the ready rule names (the rule itself reads only the wait's length).

    `choice` (the viewer's choices) goes on `holder`'s entries (the play's `PlayStatus`) once the handlers are in
    place, and comes off before they go: the choices are offered only while this play can act on them.
    `until_done`: the viewer chose "Keep waiting" from a hold, and the line says the play waits until TorBox is done."""
    budget = providersmod.KEEP_WAITING_MAX_S
    seen = {"progress": stream.get("progress") or 0, "state": stream.get("status"), "eta": None,
            "downloaded": stream.get("downloaded"), "size": None, "finished": None, "present": None, "speed": None,
            "left_s": None}
    confirmed: dict = {}

    def on_poll(reading):
        seen.update(reading)
        say("torbox", saymod.wait_line(seen, until_done=until_done), provider="torbox", torboxWait=True,
            downloading=seen["progress"], torboxState=seen.get("state"), speed=seen.get("speed"),
            leftS=seen.get("left_s"), untilDone=until_done or None)

    def stopped(signum, frame):
        # Only a raise: the handler runs at whatever line the main thread is on, a log line holding `Log.emit`'s
        # lock included, so it takes no lock and writes nothing. The words are built here (pure, from the progress
        # already in memory), so a stop that lands past the `except` below (the last status write) still carries them;
        # the `except` only logs.
        raise Interrupted("torbox.wait", f"Stopped while TorBox was downloading it ({seen['progress']} %)",
                          "the download carries on in TorBox; play the episode again in a few minutes",
                          say=saymod.wait_stopped(seen["progress"]), torrent_id=stream.get("torrent_id"),
                          signal=signum)

    def deferred(signum, frame):
        # The same rule as `stopped`: only a raise, its words built from what is in memory.
        raise Deferred("torbox.wait", saymod.later_line(cand.get("episode")), "", torrent_id=stream.get("torrent_id"),
                       signal=signum, reading=dict(seen))

    def confirm(signum, frame):
        # Not even a raise: the moment is recorded, and `on_tick` (between two slices of the sleep) acts on it. The
        # latest press counts: it is the viewer's latest sign of being there.
        confirmed["at"] = time.time()

    def on_tick():
        if "at" not in confirmed or confirmed.get("shown") == confirmed["at"]:
            return
        first = "shown" not in confirmed
        confirmed["shown"] = confirmed["at"]
        log.info("torbox.wait", "confirmed", result="PASS", torrent_id=stream.get("torrent_id"),
                 progress=seen["progress"], state=seen.get("state"), eta=seen.get("eta"), again=not first,
                 reason="the viewer chose to keep waiting during the wait: the question leaves the screen, the line "
                        "stays, and nothing is added or restarted")
        if holder is not None and confirmed_choice:
            holder.choice = confirmed_choice
        on_poll(seen)                        # the entry carries the confirmed choice at once

    log.info("torbox.wait", "start", result="PASS", id=cand.get("id"), title=cand.get("title"),
             torrent_id=stream.get("torrent_id"), state=stream.get("status"), progress=stream.get("progress"),
             downloaded=stream.get("downloaded"), budget_s=budget, until_done=until_done, nothing_cached=nothing_cached,
             starts_by_itself_s=starts_by_itself_s(settings),
             reason="the viewer chose to keep waiting: the play waits until TorBox is done" if until_done else
                    "TorBox has none of this episode's releases cached, so the play waits for its one add until "
                    "TorBox is done or the viewer chooses" if nothing_cached else
                    "dubs=avoid: TorBox has only a dub cached, so the play waits for this original-audio release "
                    "until TorBox is done or the viewer chooses")
    # The handlers are in place before the entry says `torboxWait` (what `player stop` looks for) and before it
    # carries the choices (what "Tell me when it's ready" and "Keep waiting" signal), and the entry stops saying both
    # before they go: a stop never finds a waiting entry this play is not ready to end cleanly.
    unset = object()                         # no handler of ours was installed
    previous = {signal.SIGTERM: unset, playstate.DEFER_SIGNAL: unset, playstate.CONFIRM_SIGNAL: unset}
    for sig, handler in ((signal.SIGTERM, stopped), (playstate.DEFER_SIGNAL, deferred),
                         (playstate.CONFIRM_SIGNAL, confirm)):
        try:
            previous[sig] = signal.signal(sig, handler)
        except ValueError:                   # not the main thread: a stop ends the process as before
            pass
    try:
        if holder is not None and choice:
            holder.choice = choice
        on_poll(seen)
        out = torbox.wait_ready(stream["torrent_id"], budget, on_poll=on_poll, progress=seen["progress"],
                                state=seen["state"], holds=True, on_tick=on_tick, downloaded=seen["downloaded"])
        # A press during the last poll is not shown (the wait is over) but still counts for the ready rule.
        return {**out, "confirmed_at": confirmed.get("at")}
    except Deferred as exc:
        log.info("torbox.wait", "deferred", result="PASS", signal=exc.data.get("signal"),
                 torrent_id=stream.get("torrent_id"), progress=seen["progress"],
                 reason="the viewer asked to be told when it is ready; the download carries on in TorBox")
        raise
    except Interrupted as exc:
        log.warn("torbox.wait", "stopped", result="FAIL", signal=exc.data.get("signal"),
                 torrent_id=stream.get("torrent_id"), progress=seen["progress"],
                 suggest="the download carries on in TorBox; play the episode again later")
        raise
    finally:
        try:
            if holder is not None:
                holder.choice = None
            say("torbox", saymod.wait_line(seen, until_done=until_done), provider="torbox",
                downloading=seen["progress"])
        finally:
            # `signal.signal` answers None for a handler set outside Python: that one is the default here.
            for sig, before in previous.items():
                if before is not unset:
                    signal.signal(sig, signal.SIG_DFL if before is None else before)


# Where a missing debrid account is fixed. Named the way the user reaches it, because
# "configure a provider" is a thing to look up and "open Accounts from …" is a thing to do. The one
# wording lives in `say` (it used to name Super+A, which nothing binds).
ACCOUNTS = saymod.ACCOUNTS_OPEN


def diagnose_failure(probes: list[dict], settings: dict, outlook: dict) -> tuple[str, str]:
    """(what is wrong, what to do about it) — one plain sentence each.

    Built from what the run observed, not from the stages it passed through. The old text was a
    per-provider dump of `"<stage> <error>"`, in which the fact that decided everything appeared as
    a trailing clause.

    The order below is the order the causes matter in: an account that is missing or has been
    refused is a thing the user can fix now, a cache that has not finished is a thing that fixes
    itself, and the shape of the release list is the answer when neither of those is wrong.

    One exception comes first. When a release TorBox queued is why the play stopped,
    the popout and the player's overlay (which lead with this headline and this action) say what the
    panel says (`say.failure`), whatever another provider answered. Probe rows are read with
    `say.probe_kind`, from their fields, never from their words.
    """
    kinds = [saymod.probe_kind(p) for p in probes]
    blocked = [p for p, k in zip(probes, kinds) if k == "blocked"]
    uncached = [p for p, k in zip(probes, kinds) if k == "queued"]
    packs_only = bool(outlook.get("survivors")) and outlook.get("survivors") == outlook.get("batches")

    if not probes:
        # Nothing was asked, so the shape of the release list says nothing about why.
        return ("No provider was in a position to try this episode.",
                f"Check the TorBox or Real-Debrid token — {ACCOUNTS}.")
    downloading = saymod.torbox_downloading(probes)
    if downloading is not None:
        return f"TorBox is downloading it ({downloading} %).", saymod.dub_clause(probes)
    dubbed = saymod.dubbed_words(probes)
    side = saymod.torbox_side_words(probes)
    if dubbed is not None and not (side and not uncached):
        return dubbed["headline"], dubbed["action"]
    if side and not uncached and dubbed is not None:
        # TorBox's own words lead; the copy that turned out to be a dub is the panel's hint (`say.failure`), and
        # no clause here says that allowing dubs plays a dub it may not.
        return side[0], side[1]
    if side and not uncached:
        # TorBox refused on its own side; `say.failure` gives the panel the same words, in
        # the same place (ahead of a blocked provider, which a stale verdict can add without asking).
        # Not when another provider queued a release: that download carrying on is the better news.
        # A link that failed after the wait carries the cached dub `dubs=avoid` set aside, so the action names it.
        return side[0], saymod.dub_clause(probes, side[1])
    if blocked:
        names = sorted({PROVIDER_NAMES.get(str(p.get("provider")), str(p.get("provider"))) for p in blocked})
        joined = " and ".join(names)
        return (f"{joined} refused the account for every release tried.",
                f"Check the token and the plan — {ACCOUNTS}.")
    if uncached:
        names = sorted({PROVIDER_NAMES.get(str(p.get("provider")), str(p.get("provider"))) for p in uncached})
        joined = " and ".join(names)
        return (f"{joined} has none of these releases cached yet.",
                "Try again in a few minutes — the download carries on there — or pick another release.")
    if packs_only:
        n = outlook.get("survivors") or 0
        return (f"Every release still standing is a complete-series pack, and none of the {n} could "
                f"be opened at this episode.",
                "Try again later, when a single-episode release with seeders exists.")
    if saymod.has_dub_cached(probes):
        return "No provider could deliver this episode.", saymod.dub_clause(probes, "Try again in a little while.")
    return ("No provider could deliver this episode.",
            "Try another release, or run `omarchy-anime trace last` to see what each one answered.")


def describe_probe_errors(probes: list[dict], settings: dict, outlook: dict | None = None) -> str:
    """The action first, then what each provider actually answered.

    The stage detail is kept — it is how a failure is diagnosed afterwards, and it is what proved
    on 2026-09-15 that the popout must never blame a missing token that is present. It just stops
    being the headline. ` · ` is the separator the UI splits on to show the action on its own.
    """
    action = diagnose_failure(probes, settings, outlook or {})[1]
    if not probes:
        return action
    by: dict[str, list[dict]] = {}
    for pr in probes:
        by.setdefault(str(pr.get("provider")), []).append(pr)
    # Only the first three entries per provider are shown, and "it refused everything and
    # was given up on" is the one the user needs — it arrives last, so it is sorted to the front.
    for ps in by.values():
        ps.sort(key=lambda pr: 0 if pr.get("stage") == "provider.block" else 1)
    parts = [f"{prov}: " + "; ".join(f"{pr.get('stage')} {pr.get('error')}" for pr in ps[:3]) for prov, ps in by.items()]
    skipped = [prov for prov in settings.get("providers", []) if prov not in by]
    detail = " | ".join(parts)
    if skipped:
        detail += f" | not tried (no token or disabled): {', '.join(skipped)}"
    return f"{action} · {detail}"


def failure_error(probes: list[dict], settings: dict, outlook: dict,
                  candidates: list[str]) -> StageError:
    """The one place a resolve failure becomes an error object.

    `suggest` keeps the stage detail — it is the log's, and `omarchy-anime trace` reads it. The
    sentences travel beside it in `say`, so no surface has to split a string on ` · ` to find
    something a person can read.
    """
    headline, _ = diagnose_failure(probes, settings, outlook)
    try:
        sentences = saymod.failure(probes, settings, outlook)
    except Exception:  # noqa: BLE001 - a polish path must not turn a play into a *different* failure
        sentences = {}
    return StageError("resolve", headline,
                      describe_probe_errors(probes, settings, outlook),
                      probes=probes, candidates=candidates, outlook=outlook,
                      say=sentences)


def text_search(reg, http: Http, log: Log, words: str, settings: dict, movie: bool = False) -> list[dict]:
    """The viewer's words, asked of every loaded add-on that offers `text_search`, in the
    `indexers` order, and merged as episode search merges them: keyed by infohash, the earlier
    indexer's record wins and a later live seeder count replaces its. Nothing found because
    add-ons failed raises that failure."""
    ctx = indexers.context(http, log, settings)
    budget_s = indexers.budget(settings)
    query = indexers.TextQuery(words=words, movie=bool(movie))   # frozen: every add-on can share the one object
    releases: list[dict] = []
    failures: list = []
    for ix in reg.loaded:
        if not ix.offers("text_search"):
            continue
        merged = _ask_indexer(ix, "text_search", releases, log, ctx, query, budget_s, {"q": words}, failures)
        if merged is not None:
            releases = merged
    if not releases and failures:
        raise indexers.failed(failures)
    return releases


def query_releases(args, log: Log, settings: dict, reg) -> None:
    """The picker's `/`. The viewer's words go, as typed, to every add-on offering text search
    (`text_search`). They skip the "is this the right show" test — the viewer chose them
    — and keep the episode test only as a label, so a pack or a mis-numbered title stays pickable.
    The result replaces the stored list; the raw releases of the previous search are kept, so a row
    picked from either still plays."""
    http = Http(log, settings["httpTimeout"])
    words = str(args.query or "").strip()
    mapping = anizip_mappings(http, log, args.anilist_id)
    try:
        alt = episode_ids(mapping, args.episode, log).get("alt_numbers") or []
    except Exception:                        # noqa: BLE001 - a missing mapping only loses the alt numbers
        alt = []
    search = searches.Search(args.anilist_id, args.episode, log)
    search.variants, _ = title_variants(args.anilist_id, mapping, log, lazy_anilist(http, log))
    # A movie's rows name no group word as their show.
    search.movie = bool(entry_format(args.anilist_id, mapping, log, "releases.query")[1])
    search.query = words
    releases = text_search(reg, http, log, words, settings, movie=search.movie)
    search.add_raw(releases)
    # A one-episode entry (a movie) is never numbered in a release title, so the label would say
    # "not episode 1" of the only thing it could be (live 2026-09-26, the Glass Harbor movie).
    single = int((mapping or {}).get("episodeCount") or 0) == 1
    # A movie's or a one-episode entry's names need not state the episode (AniList's count, as the
    # resolve decides it, read from the titles cache: ani.zip's can be missing or AniDB's), so they keep the old
    # reading, any number. AniList's count only relaxes the label, it does not skip it as ani.zip's does: skipping
    # it would unlabel 194 names in a measured sample, other shows' episodes among them (a related
    # series' `- 60`…`- 87`, `Vigilantes S01E02`…), not only the entry's own releases.
    unstated = search.movie or one_episode_entry(log, args.anilist_id, args.episode, search.movie,
                                                 stage="releases.query")
    keep, not_episode = [], []
    for rel in releases:
        if (not single and not parse_title(rel.get("title", "")).get("batch")
                and not title_matches_episode(rel.get("title", ""), args.episode, alt, any_number=unstated)):
            search.reject(rel, "episode", str(args.episode))
            not_episode.append([rel.get("title", ""), title_episode_log(rel.get("title", ""), unstated)])
        else:
            keep.append(rel)
    rejected: list = []
    ranked = rank_candidates(http, log, keep, settings, dropped_sink={}, episode=args.episode, alt=alt,
                             rejected_sink=rejected, track_source=indexers.SubtitleTracks(reg, http, log, settings))
    for rel, code in rejected:
        search.reject(rel, code)
    search.set_passed(ranked)
    old = searches.load(args.anilist_id, args.episode, None) or {}
    for h, rel in (old.get("raw") or {}).items():
        search.raw.setdefault(h, rel)
    search.save()
    # Each name labelled "not episode N", with how its episode was read.
    log.info("releases.query", "done", result="PASS" if releases else "EMPTY", q=words, found=len(releases),
             passed=len(search.passed), rejected=len(search.rejected), alt=alt,
             not_episode=not_episode[:NOT_EPISODE_LOGGED],
             not_episode_more=max(0, len(not_episode) - NOT_EPISODE_LOGGED))


def do_releases(args, log: Log, settings: dict) -> dict:
    """What the release picker lists for one episode. The stored search when it is under
    `searches.SEARCH_TTL` old, else the search and ranking half of a resolve — no provider is
    asked anything. A refusal is part of the answer, not an error: it is why the picker opened."""
    reg = indexers.load(settings, log)
    # `/` (own words) is offered only when a loaded add-on has text search. Read fresh on
    # every request, never stored: an add-on dropped in since the search was kept counts now.
    offered_by = [ix.name for ix in reg.loaded if ix.offers("text_search")]
    log.info("releases", "text_search", result="PASS" if offered_by else "SKIP", offered_by=offered_by,
             loaded=[ix.name for ix in reg.loaded], text_search=bool(offered_by))
    words = str(getattr(args, "query", None) or "").strip()
    if not reg.loaded:
        # Nothing can search. A stored list with releases in it is still an answer (a pick
        # from it plays from its stored record); own words, `--refresh` and an empty stored list (one the
        # older code kept, "…has aired") all need a search, so they fail as no indexer add-on.
        stored = None if (args.refresh or words) else searches.load(args.anilist_id, args.episode)
        log.info("releases", "no_indexer", result="PASS" if (stored or {}).get("raw") else "FAIL",
                 refresh=bool(args.refresh), q=words or None, stored_releases=len((stored or {}).get("raw") or {}),
                 reason="a stored list with releases is served" if (stored or {}).get("raw")
                 else "no add-on loaded and nothing stored to show")
        if not (stored or {}).get("raw"):
            indexers.require(reg, log)
    if words and not offered_by:
        # The picker hides `/` then; asked anyway (the CLI), the episode's list is the answer.
        log.info("releases.query", "skip", result="SKIP", q=words, loaded=[i.name for i in reg.loaded],
                 reason="no loaded indexer add-on offers text search",
                 suggest="own-words search needs an indexer add-on with text search; listing the episode's search")
    own_words = bool(words and offered_by)
    if own_words:
        query_releases(args, log, settings, reg)
        data = searches.load(args.anilist_id, args.episode, None)
        from_cache = False
    else:
        data = None if args.refresh else searches.load(args.anilist_id, args.episode)
        from_cache = data is not None
        if data is None:
            # `ignore_pin`: the list is what the indexes hold, not what the pin would play. A covering pin
            # short-circuits the resolve before any search, which left the picker empty exactly
            # when the viewer opened it to move off the pin (live 2026-09-26, LxL ep 10).
            run = types.SimpleNamespace(anilist_id=args.anilist_id, episode=args.episode, ignore_pin=True,
                                        registry=reg)
            try:
                do_resolve(run, log, settings, dry_run=True)
            except StageError as exc:
                log.info("releases", "refusal", result="PASS", refused_stage=exc.stage,
                         reason="the list is still shown; the refusal is why the picker opened")
            data = searches.load(args.anilist_id, args.episode, None)
    if data is None:
        raise StageError("releases", "No search could be made for this episode.", "try again in a moment")
    out = {"ok": True, "trace": log.trace, **searches.public(data, from_cache), "pinned": pins.get(args.anilist_id),
           "subtitle_language": settings["subtitleLanguage"], "text_search": bool(offered_by),
           # The tags that are the viewer's language, so the picker line finds it (`BrowseModel._subsText`).
           "subtitle_aliases": sorted(set(LANGUAGE_TABLE.get(settings["subtitleLanguage"], ((), ()))[0])
                                      | {settings["subtitleLanguage"]})}
    if not own_words:
        log.info("releases.cache", "hit" if from_cache else "miss", result="PASS", anilist=args.anilist_id,
                 episode=args.episode, age_s=out["age_s"], passed=len(out["releases"]),
                 rejected=len(out["rejected"]), refusal=(out["refusal"] or {}).get("stage"))
    return out


def do_picker_notice(args, log: Log) -> dict:
    """Auto-next refused while the player is up. Say so on screen and lend `p` to the picker;
    the player stays on the episode that ended. Nothing happens without a running player."""
    mpv = Mpv(log)
    if not (MPV_SOCKET.exists() and mpv._try_connect()):
        log.info("player.picker_key", "arm", result="SKIP", reason="no running player")
        return {"ok": True, "armed": False}
    return {"ok": True, **playermod.arm_picker_key(mpv, log, args.anilist_id, args.episode)}


# How long "Tell me when it's ready" watches a signalled play's entry before it answers. The play's handler only
# raises, so a play in its wait ends within one poll's request (`providers.WAIT_POLL_TIMEOUT_S`); one that has not by
# then still ends itself `later` (and hides the window) when the request returns.
LATER_WAIT_S = 10.0
# The stages of a play that has gone past its wait for TorBox: it is loading the episode (or has).
PAST_THE_WAIT = ("load", "done")


def hide_player(log: Log) -> dict | None:
    """The player window hidden, when a player is running: what `player hide` does. None when there is none."""
    mpv = Mpv(log)
    if not (MPV_SOCKET.exists() and mpv._try_connect()):
        return None
    try:
        return playermod.set_window(log, "hide", int(mpv.command("get_property", "pid")), False)
    except StageError as exc:
        log.warn("play.later", "hide", result="FAIL", error=str(exc),
                 suggest="the window stays up; Esc or the popout hides it")
        return None


def do_later(args, log: Log) -> dict:
    """"Tell me when it's ready", from either surface: the play of this episode that waits for TorBox's download of
    the original-audio release (or held its choices after the wait) ends without a failure, the download is recorded
    as pending (`pending.add`; a watcher of pending downloads reads it) and the player window is hidden.

    The pending entry is written first and read back; when it did not land, nothing is ended and the answer says so.
    A waiting play is then signalled (`playstate.defer_waiting`) and ends itself; a held one (its process gone) is
    ended here (`playstate.end_held`). What happened is read from the play's entry, never guessed from the time: it
    ended `later`, it held meanwhile (ended here too), it went past its wait (loading the episode: the pending entry
    is removed again and the viewer gets the episode), or it is still waiting after `LATER_WAIT_S` (the request stays
    recorded and the play ends itself shortly)."""
    t0 = time.monotonic()
    entry = playstate.read() or {}
    same = (int(entry.get("mediaId") or 0) == args.anilist_id and int(entry.get("episode") or 0) == args.episode)
    choice = entry.get("choice") if same else None
    state = entry.get("state")
    out = {"ok": True, "later": False, "anilist_id": args.anilist_id, "episode": args.episode, "message": ""}
    if not choice or state not in ("running", playstate.HELD):
        log.info("play.later", "nothing", result="SKIP", anilist=args.anilist_id, episode=args.episode,
                 found_episode=entry.get("episode"), state=state, choice=bool(choice),
                 reason="no play of this episode is waiting for TorBox with the viewer's choices")
        return {**out, "message": f"Episode {args.episode} is not waiting for TorBox."}
    release, dub = choice.get("release") or {}, choice.get("dub") or {}
    # The show as the choice's sentence named it (the notification's title), else the play's own title.
    record = pendingmod.add({"mediaId": args.anilist_id, "episode": args.episode,
                             "title": choice.get("show") or entry.get("title"),
                             "torrentId": release.get("torrentId"), "infoHash": release.get("infoHash"),
                             "release": release.get("title"), "audio": choice.get("original"),
                             "subtitles": release.get("subtitles") or None, "trace": entry.get("trace"),
                             "dub": {k: dub.get(k) for k in ("infoHash", "title", "lang")} if dub else None,
                             "nothingCached": bool(choice.get("nothingCached"))}, log)
    if record is None:
        # Nothing would tell the viewer: the play is left as it is (waiting, or holding its choices).
        log.warn("play.later", "unsaved", result="FAIL", trace=entry.get("trace"), episode=args.episode, state=state,
                 ms=(time.monotonic() - t0) * 1000, suggest="the pending download did not land; the play is left alone")
        return {**out, "message": "That couldn't be saved, so the player keeps waiting. Try again in a moment."}
    trace, line = entry.get("trace"), saymod.later_line(args.episode)
    ended, decision, now = None, state, entry
    if state == playstate.HELD:
        ended = playstate.end_held(trace, line, log)
    else:
        sent = playstate.defer_waiting(entry, log, hide=not getattr(args, "no_hide", False))
        if not sent["signalled"]:
            # The waiting play is gone without a word (killed): its entry is ended here, as a held one is.
            ended, decision = playstate.end_held(trace, line, log, from_state="running"), "gone"
        else:
            deadline = time.monotonic() + LATER_WAIT_S
            decision = None
            while decision is None:
                now = playstate.read() or {}
                if now.get("trace") != trace:
                    decision = "claimed"
                elif now.get("state") == playstate.LATER:
                    ended, decision = now, "later"
                elif now.get("state") == playstate.HELD:
                    ended, decision = playstate.end_held(trace, line, log), "held_meanwhile"
                elif now.get("state") == "done" or now.get("stage") in PAST_THE_WAIT:
                    decision = "moved_on"
                elif now.get("state") == "failed":
                    # The play failed (or was stopped), but the download it was told about carries on in TorBox:
                    # the request stands, and nothing is "ready".
                    decision = "failed"
                elif time.monotonic() >= deadline:
                    # Still running at the deadline. Still in its wait (the choices and `torboxWait` on the entry):
                    # it ends itself when its request returns. Anywhere else the signal landed outside the wait (the
                    # no-op guard took it): the request stands, but nothing will end this play.
                    waiting = bool(now.get("choice")) and bool((now.get("detail") or {}).get("torboxWait"))
                    decision = "still_waiting" if waiting else "recorded"
                else:
                    time.sleep(0.1)
    if decision in ("moved_on", "claimed"):
        # The episode is loading (or another play took over): nothing to tell the viewer about later.
        pendingmod.remove(args.anilist_id, args.episode, log, f"the play {decision.replace('_', ' ')}")
    # The player asks with `--no-hide`: it says the answer on its OSD and hides the window a moment later itself. A play
    # in the background never hides it: the window holds the episode the viewer is watching.
    no_hide = bool(getattr(args, "no_hide", False))
    window = hide_player(log) if ended is not None and not no_hide and not entry.get("background") else None
    watched_by = None
    if decision not in ("moved_on", "claimed"):
        # The request stands: the watcher follows it from now (started unless one runs; never a second).
        watched_by = watchermod.ensure(log, "a pending download was recorded", force=True)
    log.info("play.later", "done", result="PASS" if decision != "claimed" else "SKIP", anilist=args.anilist_id,
             episode=args.episode, from_state=state, decision=decision, ms=(time.monotonic() - t0) * 1000,
             entry_stage=now.get("stage"), state_now=now.get("state"), choice=bool(now.get("choice")),
             torbox_wait=bool((now.get("detail") or {}).get("torboxWait")),
             pending=decision not in ("moved_on", "claimed"), watcher_pid=watched_by,
             background=bool(entry.get("background")),
             hidden=bool(window and window.get("visible") is False), no_hide=no_hide,
             reason={"later": "the play ended later",
                     "held_meanwhile": "the play held meanwhile; its entry is ended here",
                     "gone": "the waiting play was gone; its entry is ended here",
                     playstate.HELD: "the held play's entry is ended here",
                     "still_waiting": "the play is still waiting; the request is recorded and it ends itself shortly",
                     "failed": "the play failed meanwhile; the download carries on in TorBox and the request stands",
                     "recorded": "the play is past its wait but not loading; the request stands, nothing ends it",
                     "moved_on": "the play went past its wait (TorBox finished); it plays the episode",
                     "claimed": "another play claimed meanwhile"}.get(decision, decision))
    # Whether the play itself ended here (`later`, held, gone): the player hides its window a moment after its answer
    # only then, never for a play still waiting, past its wait, or failed.
    out["ended"] = ended is not None
    if decision == "still_waiting" and no_hide:
        # That play ends itself `later` shortly, after the player had its answer on screen for `LATER_WAIT_S`: it
        # hides the window itself then, so the marker that told it not to goes.
        playstate.quiet_file().unlink(missing_ok=True)
        # The play may have ended `later` between the deadline's read and the unlink, reading the marker first (so it
        # hid nothing): read its entry once more, and if it ended, the player hides the window as for any ended play.
        again = playstate.read() or {}
        landed = again.get("trace") == trace and again.get("state") == playstate.LATER
        if landed:
            out["ended"] = True
        log.info("play.later", "quiet", result="PASS", trace=trace, removed=True, ended_meanwhile=landed,
                 reason="the play ended later before the marker went; the player hides the window" if landed else
                        "the play is still waiting; it hides the window itself when it ends")
    if decision == "moved_on":
        return {**out, "message": f"Episode {args.episode} is ready and starting now."}
    if decision == "claimed":
        return out
    if decision == "still_waiting":
        return {**out, "later": True, "ending": True, "pending": record,
                "message": f"{line} The player stops waiting in a moment."}
    if decision in ("failed", "recorded"):
        return {**out, "later": True, "pending": record, "message": line}
    return {**out, "later": ended is not None, "pending": record, "window": window,
            "message": line if ended is not None else ""}


def do_keep_waiting(args, log: Log) -> dict:
    """"Keep waiting" pressed while the play still waits: the play of this episode that waits for TorBox with
    the viewer's choices is told so (`playstate.confirm_waiting`, a signal its wait only records). It keeps waiting on
    the same download, in the same process: nothing is added, nothing restarted, no claim raced. Its question leaves
    the entry within a second and its line stays (the ready rule, `ready_unseen`, reads only the wait's length).
    Anything else (no such play, a held one, one past its wait) is answered and left alone."""
    entry = playstate.read() or {}
    same = (int(entry.get("mediaId") or 0) == args.anilist_id and int(entry.get("episode") or 0) == args.episode)
    waiting = (same and entry.get("state") == "running" and bool(entry.get("choice"))
               and bool((entry.get("detail") or {}).get("torboxWait")))
    out = {"ok": True, "confirmed": False, "anilist_id": args.anilist_id, "episode": args.episode, "message": ""}
    if not waiting:
        log.info("play.confirm", "nothing", result="SKIP", anilist=args.anilist_id, episode=args.episode,
                 found_episode=entry.get("episode"), state=entry.get("state"), choice=bool(entry.get("choice")),
                 reason="no play of this episode is waiting for TorBox with the viewer's choices")
        return {**out, "message": f"Episode {args.episode} is not waiting for TorBox."}
    sent = playstate.confirm_waiting(entry, log)
    log.info("play.confirm", "done", result="PASS" if sent["signalled"] else "SKIP", trace=entry.get("trace"),
             episode=args.episode, signalled=sent["signalled"], why=sent["reason"] or None,
             reason="the waiting play was told the viewer keeps waiting" if sent["signalled"] else
                    "the waiting play could not be told; its entry is left as it is")
    return {**out, "confirmed": sent["signalled"], "trace": entry.get("trace"),
            "message": "" if sent["signalled"] else f"Episode {args.episode} is not waiting for TorBox."}


def do_pin(args, log: Log) -> dict:
    if args.action == "forget":
        return {"ok": True, "forgot": pins.forget(log, args.anilist_id)}
    return {"ok": True, "pin": pins.get(args.anilist_id)}


def not_out_gate(args, log: Log, known: dict) -> dict | None:
    """A play of an entry AniList lists as not yet released, with no airing schedule (`anilist.not_out`), is answered
    here: before the claim (which would stop a running play), before any player launch or show, and before any indexer
    call. It reads the titles cache `do_play` already read (`known`, which every list, search and media answer
    rewrites): no request. A titles file with no `status` (written before it kept one, or never written) falls back to
    the newest AniList answer saved on disk that carries the entry (`anilist.saved_answer`: the lists, Top 100,
    Airing), still with no request; with none, the play goes on. Nothing is written to `play-status.json` and nothing
    is asked of the player. The answer is the JSON (Browse and the popout show it, and pass `--no-notify`); any other
    caller is detached and reads only `play-status.json`, so it gets one notification instead. None: the play goes
    on."""
    mid = int(args.anilist_id)
    path = CACHE_DIR / "anilist" / "titles" / f"{mid}.json"
    try:
        age_s = round(time.time() - path.stat().st_mtime, 1)
    except OSError:
        age_s = None
    subject, fallback = known, None
    if not known.get("status"):
        t0 = time.monotonic()
        saved = saved_answer(mid)
        fallback = {"titles_file": str(path), "titles_age_s": age_s, "files_read": saved["read"] if saved else None,
                    "ms": round((time.monotonic() - t0) * 1000, 1)}
        if saved:
            media = saved["media"]
            t = media.get("title") if isinstance(media.get("title"), dict) else {}
            subject = {**known, "status": media.get("status"), "nextAiringEpisode": media.get("nextAiringEpisode"),
                       "startDate": media.get("startDate"),
                       "english": known.get("english") or t.get("english"),
                       "romaji": known.get("romaji") or t.get("romaji"),
                       "native": known.get("native") or t.get("native")}
            path, age_s = Path(saved["source"]), saved["age_s"]
    facts = {"anilist": args.anilist_id, "episode": args.episode, "status": subject.get("status"),
             "schedule": subject.get("nextAiringEpisode"), "startDate": subject.get("startDate"),
             "source": str(path), "age_s": age_s, "fallback": fallback}
    if not subject.get("status"):
        log.info("play.not_out", "pass", result="UNKNOWN", **facts,
                 reason="the titles cache has no status (written before it carried one, or never written) and no "
                        "AniList answer saved on disk carries the entry: whether it is out is unknown, so the play "
                        "goes on")
        return None
    if not not_out(subject):
        log.info("play.not_out", "pass", result="PASS", **facts,
                 reason="out: AniList's status is not NOT_YET_RELEASED" if subject.get("status") != "NOT_YET_RELEASED"
                 else "NOT_YET_RELEASED with an airing schedule: the episodes before the scheduled one aired")
        return None
    title = subject.get("english") or subject.get("romaji") or subject.get("native") or ""
    words = saymod.not_out(title, subject.get("startDate"))
    message = " ".join(w for w in (words["headline"], words["hint"]) if w)
    notice = None
    if not getattr(args, "no_notify", False):
        notice = notifymod.send(log, title or "Not out yet", message, trace=log.trace,
                                why="a play of an entry that is not out, from a caller that does not show the answer")
    log.info("play.not_out", "refuse", result="SKIP", **facts, title=title,
             notified=bool(notice and notice.get("sent")), no_notify=bool(getattr(args, "no_notify", False)),
             reason="NOT_YET_RELEASED with no airing schedule: no episode is out; refused before the claim, the player "
                    "and any search",
             suggest="if it is out, open it in Browse (that reads AniList again and rewrites the titles cache) and "
                     "play it again")
    return {"ok": True, "refused": "not_out", "trace": log.trace, "anilist_id": args.anilist_id,
            "episode": args.episode, "message": message, "say": words, "notice": notice}


# Each read of the busy probe waits this long at most: a player whose core stopped answering (`Mpv.ensure_running`'s
# watchdog case) costs the play two seconds a read, and reads as not busy.
BUSY_READ_S = 2.0


def player_busy(log: Log) -> dict:
    """Whether the player is busy with an episode the viewer is watching, from the player alone (no request, and no
    player is ever launched here): it runs, a file is loaded (`idle-active` false, the menu's own test for "loaded"),
    not at its end (`eof-reached` false: under `--keep-open=yes` an ended episode stays loaded, paused on its last
    frame, and auto-next starts from there), in a window on screen (`player.window_visible`). Paused counts: a viewer
    who paused mid-episode is still on it. A hidden window, an ended episode, an idle or closed player: not busy, and
    the play shows its wait in the player as before. Hyprland is asked only when a file is loaded and not ended. A
    read that fails (no reply in `BUSY_READ_S`, a Hyprland read that fails) or an answer that is not a plain true or
    false is unknown, and unknown is not busy; the one exception is `eof-reached`, which mpv leaves without a value
    while a `loadfile` swaps the file: unknown there is not ended. Logged once, `play.busy decide`, with every fact and
    the reason."""
    t0 = time.monotonic()
    facts = {"running": False, "pid": None, "idle": None, "ended": None, "paused": None, "visible": None}
    why = ""
    # No client at all without the socket: a play that never reaches the player builds none (`test_no_debrid`).
    mpv = Mpv(log) if MPV_SOCKET.exists() else None
    if mpv is None or not mpv._try_connect():
        why = "no player is running"
    else:
        facts["running"] = True
        try:
            pid = mpv.command("get_property", "pid", timeout=BUSY_READ_S)
            facts["pid"] = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
            for key, prop in (("idle", "idle-active"), ("ended", "eof-reached"), ("paused", "pause")):
                try:
                    value = mpv.command("get_property", prop, timeout=BUSY_READ_S)
                except mpvmod.PlayerClosed:
                    raise
                except StageError:
                    # No reply in time, or no value: `eof-reached` has none while no file is loaded, and none while a
                    # `loadfile` swaps the file (then a file is on its way in: not known to be ended, so not ended).
                    value = None
                facts[key] = value if isinstance(value, bool) else None
        except StageError as exc:            # `PlayerClosed` included: the player went away under the probe
            why = f"the player did not answer ({exc})"
        finally:
            mpv._close()
        if not why:
            if facts["idle"] is not False:
                why = "no episode is loaded" if facts["idle"] else "whether an episode is loaded is unknown"
            elif facts["ended"] is True:
                why = "the episode on the player has ended"
            elif facts["pid"] is None:
                why = "the player's pid is unknown, so its window cannot be found"
            else:
                try:
                    facts["visible"] = bool(playermod.window_visible(log, facts["pid"]))
                except StageError as exc:    # hyprctl failed, timed out or answered no JSON (`capture_player`'s rule)
                    why = f"whether the player's window is on screen is unknown ({exc})"
                else:
                    if not facts["visible"]:
                        why = "the player's window is hidden"
    busy = not why
    if busy:
        paused = " (paused)" if facts["paused"] else ""
        reason = (f"an episode is loaded, not at its end, in a window on screen{paused}: the play runs in the "
                  "background and touches nothing of it until its own episode is ready")
    else:
        reason = f"{why}: the play shows its wait in the player"
    log.info("play.busy", "decide", result="PASS", busy=busy, **facts, ms=round((time.monotonic() - t0) * 1000, 1),
             reason=reason)
    return {"busy": busy, **facts, "reason": why or "watched"}


def do_play(args, log: Log, settings: dict) -> dict:
    """The gate and the narration live here; `play_stream` is the play itself.

    Nothing in the plugin used to know that a play was in flight, so a second click 22 s into a 43 s
    play was accepted and ran concurrently. The claim below is the one place that can be true of at
    most one process.
    """
    # The show's name from the local title cache every list command writes — no request, and it
    # makes the shared entry self-describing for a surface that has nothing else to go on.
    known = known_titles(args.anilist_id) or {}
    refused = not_out_gate(args, log, known)
    if refused is not None:
        return refused
    status = playstate.PlayStatus(log, args.anilist_id, args.episode, trace=log.trace,
                                  title=known.get("romaji") or known.get("english") or "")
    # A release the viewer chose supersedes a play of the same episode that waits with its choices (`decide`).
    status.chosen = bool(getattr(args, "release", None))
    # The player busy with an episode the viewer is watching: this play runs in the background. It claims (the
    # one in-flight slot: a second play of this episode is answered "already", and Browse and the popout show its line
    # from the entry), but touches nothing of the player (no launch, park, picture, message or show) until its own
    # episode is ready; then `play_stream` asks the player again.
    status.background = player_busy(log)["busy"]
    # "Tell me when it's ready" signals a waiting play; any play outside its wait catches the signal and does nothing
    # rather than dying of it. `wait_for_torbox` sets its own handler meanwhile.
    playstate.guard_defer_signal()
    decision = status.claim()
    if not decision["claimed"]:
        # Not an error: the episode the user asked for *is* being started, by a process that is
        # already publishing its progress. Exit 0 so no surface paints this as a failure.
        return {"ok": True, "claimed": False, "action": decision["action"], "trace": log.trace,
                "anilist_id": args.anilist_id, "episode": args.episode,
                "message": decision["message"], "holder": decision["trace"],
                "play": playstate.public()}
    try:
        # A play that can never start says so before a player window opens.
        require_debrid(log, settings)
        require_indexer_first(args, log, settings)          # and so does one with nothing to search
        if not status.background:
            # The window comes up at claim time with the loading screen on it, so the
            # 5-28 s resolve is spent looking at the show. Order: player → park → art → message → show
            # → resolve. "Never flash an empty window" holds because the backdrop precedes `show`.
            mpv = Mpv(log)
            mpv.ensure_running()
            pid = int(mpv.command("get_property", "pid"))
            window = playermod.park(log, pid)
            # The claim's entry is the last thing written until `set_art`, and `ensure_running` (which
            # may launch a player) plus `park` sit in between. `ServiceLogic.PLAY_ABANDON_S` is 20 s of
            # silence before the popout treats the writer as gone, so refresh `updatedAt` here rather
            # than let the gap between the two writes decide.
            status.stage("start", saymod.stage("start"))
            # The art step gets its own, much shorter HTTP client. `httpTimeout` is 15 s with 3 retries
            # and backoff — ~49 s worst case for one blackholed host — and `loading_art`'s own budget
            # only gates *entry* to a step, never a request already in flight. Nothing writes the status
            # entry between the claim above and `set_art` below, so that one request would silence the
            # play past `playstate.ABANDON_AFTER_S` (20 s) and every surface would call the writer gone.
            # Nothing is lost by the cap: the resolve refetches the ani.zip mapping a moment later
            # anyway (`map.anizip` runs in the search stage), so a slow host costs the loading screen
            # its picture, not the episode.
            art_http = Http(log, min(2.0, float(settings["httpTimeout"])))
            art = loading_art(log, art_http, args.anilist_id, args.episode, status.title)
            status.set_art(art)
            if EPISODES_SCRIPT.exists():
                mpv.ensure_script(str(EPISODES_SCRIPT), EPISODES_SCRIPT_KEY)
            mpv.script_message_to(EPISODES_SCRIPT_NAME, "art")
            if args.show:
                playermod.show(log)
            out = play_stream(args, log, settings, status, mpv=mpv, window=window)
        else:
            # The loading screen's picture is still worked out (it reads no player): it is on the entry, and the player
            # draws it only once nothing is watched (the episode ended meanwhile: auto-next's case).
            status.set_art(loading_art(log, Http(log, min(2.0, float(settings["httpTimeout"]))), args.anilist_id,
                                       args.episode, status.title))
            out = play_stream(args, log, settings, status, background=True)
    except Held as exc:
        # The wait ended without the episode: the play holds the viewer's choices, the window stays up. Exit 0 with
        # `held`, so no surface reads it as a failure or opens the picker for it.
        entry = status.hold(str(exc), exc.data["choice"], ended=exc.data.get("ended"))
        return {"ok": True, "held": True, "claimed": True, "trace": log.trace, "anilist_id": args.anilist_id,
                "episode": args.episode, "message": str(exc), "choice": exc.data["choice"],
                "play": playstate.public() if entry else None}
    except Deferred as exc:
        # "Tell me when it's ready" during the wait: `later` recorded the pending download; this play ends without
        # a failure (`later` hides the window once this entry says so).
        status.later(saymod.later_line(args.episode), ended="later")
        # `later` hides the window too once it sees this entry; a play whose wait ended after `later` stopped watching
        # (one slow poll) hides it itself. Hiding twice is a no-op. Not when the player asked (`later --no-hide`): it
        # says its answer first and hides a moment later. Never from the background: the window is the episode the
        # viewer is watching.
        window = hide_player(log) if playstate.later_hides(log.trace, log, background=status.background) else None
        return {"ok": True, "later": True, "claimed": True, "trace": log.trace, "anilist_id": args.anilist_id,
                "episode": args.episode, "message": saymod.later_line(args.episode), "play": playstate.public(),
                "window": window}
    except Interrupted as exc:
        if playstate.being_superseded():
            # Another play (the viewer's "Watch the dub now", or any new play) is claiming over this one and holds
            # the claim lock while it stops it: its entry is the one that matters, so this play writes nothing more
            # and ends without an error a surface would show.
            log.info("play.state", "superseded", result="PASS", episode=args.episode, stage_failed=exc.stage,
                     reason="another play claimed over this one while it waited; its entry is left to it")
            return {"ok": True, "superseded": True, "claimed": True, "trace": log.trace,
                    "anilist_id": args.anilist_id, "episode": args.episode, "message": str(exc)}
        log.info("play.state", "stopped", result="PASS", episode=args.episode, stage_failed=exc.stage, superseded=False,
                 lock="free", reason="no claim holds the lock: the viewer's own stop (player stop); the entry says so")
        status.fail(exc.stage, str(exc), exc.suggest, say=exc.data.get("say") or {})
        raise
    except mpvmod.PlayerClosed as exc:
        # The player went away mid-start (a restart, the window closed): plain words, no "bug" to report. The player
        # is gone, so Browse and the popout are where they are read (`say`).
        words = exc.data.get("say") or saymod.player_closed(args.episode)
        said = f"{words['headline']} {words['action']}"
        log.warn("play.state", "player_closed", result="FAIL", episode=args.episode, error=str(exc),
                 cause=exc.data.get("error"), command=exc.data.get("command"),
                 suggest="the player went away while the play started it (a player restart, or its window closed); "
                         "playing again starts a new one")
        status.fail("mpv.closed", said, "", say=words)
        raise mpvmod.PlayerClosed("mpv.closed", said, "", say=words, cause=exc.data.get("error")) from exc
    except StageError as exc:
        # The reason has to outlive this process: the player and the popout are where it is read,
        # and neither of them was watching when it happened.
        status.fail(exc.stage, str(exc), exc.suggest, say=exc.data.get("say") or {})
        raise
    except Exception as exc:                            # noqa: BLE001 - same, for the unexpected kind
        status.fail("internal", f"{type(exc).__name__}: {exc}",
                    f"bug: report with `omarchy-anime trace {log.trace}`")
        raise
    finally:
        # A request this play took at its wait's ready and neither played nor told (a failed link, a stop, a crash):
        # the watcher's again.
        settle_taken(log, args, "unmark", "the play that took the request ended without playing it")
    out["claimed"] = True
    if decision["action"] != "claim":
        out["superseded"] = {"action": decision["action"], "trace": decision["trace"],
                             "pid": decision["pid"], "reason": decision["reason"]}
    return out


def certificate_check(log: Log, ev: dict | None, stream: dict, episode, t0: float) -> None:
    """Fails the play when the player refused the stream server's certificate (the launcher's `--tls-verify=yes`):
    nothing was sent to that server. `ev` is the load's end, marked by `Mpv.wait_event`. Logged with the host only:
    the URL's query is the key. The error and suggest are the viewer's sentence (`PlayerClosed`'s way), so the player's
    own line (`playstate.render`), the popout and the corner line all read it; the host stays in the log."""
    if not (ev and ev.get("tls_refused")):
        return
    log.error("mpv.loadfile", "certificate", result="FAIL", host=providersmod.link_host(stream["download"]),
              provider=stream["provider"], reason=ev["tls_refused"], ms=(time.monotonic() - t0) * 1000,
              suggest="the network between this machine and the server presented a certificate that does not "
                      "check out (a captive portal or an interception); playing it again on another network shows "
                      "which")
    words = saymod.certificate_refused(episode, stream["provider"])
    raise StageError("mpv.loadfile", words["headline"], words["action"], say=words)


def play_stream(args, log: Log, settings: dict, status: playstate.PlayStatus, mpv: Mpv | None = None,
                window: dict | None = None, background: bool = False) -> dict:
    ranked: list[dict] = []
    result = do_resolve(args, log, settings, dry_run=False, ranked_sink=ranked, status=status)
    stream = result["stream"]
    if background:
        # Ready: the player is asked again. Still busy after a wait, nothing of it is touched
        # (`ready_in_the_background`); anything else takes it as any play does.
        mpv = take_the_player(args, log, settings, status, result)
        if mpv is None:
            return ready_in_the_background(args, log, status, result)
    if mpv is None:
        mpv = Mpv(log)
        mpv.ensure_running()
    try:
        pid = int(mpv.command("get_property", "pid"))
    except mpvmod.PlayerClosed as exc:
        if not result.get("waited"):
            raise
        # The player this play opened went away during the wait. A player restarted meanwhile (a new pid on the
        # plugin's socket) takes the episode under the ready rule, as the old one would have; only with no player up
        # is the viewer called back by the notification. Its window is left where it is (not parked).
        mpv = reconnect_player(log, exc)
        if mpv is None:
            raise ready_with_the_player_gone(args, log, settings, result, exc) from exc
        pid = int(mpv.command("get_property", "pid"))
    # Park before loading, so a freshly started window never flashes on the current workspace.
    # `do_play` already parked (and showed, with the backdrop up) before the resolve; this
    # is the path for a caller that did not.
    if window is None:
        window = playermod.park(log, pid)
    start = float(args.start or 0)
    if not start and not args.no_resume:
        chosen_hash = (result.get("chosen") or {}).get("info_hash")
        saved = playermod.read_resume(args.anilist_id, args.episode) or {}
        start = playermod.start_position(args.anilist_id, args.episode, info_hash=chosen_hash)
        other = bool(saved.get("pos")) and not start and saved.get("info_hash") not in (None, str(chosen_hash or "").lower())
        log.info("resume", "decide", result="PASS", start=start, info_hash=chosen_hash,
                 saved_for=saved.get("info_hash"),
                 reason="stored position" if start else
                 ("the stored position belongs to another release" if other else "no stored position"))
    t0 = time.monotonic()

    def loading():
        """The load stage, refreshed every tick so `updatedAt` stays fresh while mpv opens the stream
        (the popout reads 20 s of silence as an abandoned play)."""
        status.stage("load", saymod.stage("load", {"dubOnly": result.get("dub_only")}), provider=stream["provider"],
                     dubOnly=result.get("dub_only") or None)

    loading()

    # The font follows the current settings on every play, not the player's launch.
    try:
        playermod.apply_subtitle_font(mpv, log, settings)
    except Exception as exc:                 # noqa: BLE001 - a font must never stop a play
        log.warn("player.sub_font", "set", result="FAIL", error=str(exc))
    # mpv's own track choice at load follows the viewer's language, then the second one.
    try:
        playermod.apply_subtitle_langs(mpv, log, settings)
    except Exception as exc:                 # noqa: BLE001 - a track preference must never stop a play
        log.warn("player.slang", "set", result="FAIL", error=str(exc))
    # A refused auto-next borrowed `p` for the picker; any play gives it back.
    try:
        playermod.disarm_picker_key(mpv, log)
    except Exception as exc:                 # noqa: BLE001 - a key binding must never stop a play
        log.warn("player.picker_key", "disarm", result="FAIL", error=str(exc))
    # The forced title and the player stamp go on before `loadfile` (`prepare_load` says why).
    media_title = playermod.episode_title(result["series_title"], args.episode)
    playermod.prepare_load(mpv, log, media_title,
                           playermod.playing_stamp(args.anilist_id, args.episode, result["chosen"]["info_hash"]))
    # The episode came from a wait for TorBox longer than `torboxWaitSeconds` (`ready_unseen`): the viewer will have
    # looked away, so it loads paused, the window is left as it is (on screen or hidden), the viewer is told by a
    # notification with a sound, and the position does not move, so no tick marks it watched and AniList is not
    # written. Otherwise it plays as any play does.
    waited = result.get("waited")
    unseen = False
    if waited:
        unseen, ready_why, wait_s = ready_unseen(waited, settings)
        # The window is only information here: hiding the player changes nothing about the rule.
        log.info("play.ready", "unseen" if unseen else "seen", result="PASS", episode=args.episode,
                 wait_s=wait_s, starts_by_itself_s=starts_by_itself_s(settings),
                 measured_from=waited.get("measured_from"), until_done=waited.get("until_done"),
                 confirmed=waited.get("confirmed"), visible=playermod.window_visible(log, pid), reason=ready_why)
    if unseen:
        playermod.load_file(mpv, log, stream["download"], media_title, start, paused=True)
    else:
        playermod.load_file(mpv, log, stream["download"], media_title, start)
    ev = mpv.wait_event("file-loaded", timeout=60, on_tick=loading)
    certificate_check(log, ev, stream, args.episode, t0)
    if not ev or ev.get("event") != "file-loaded":
        raise StageError("mpv.loadfile", "mpv did not load the stream within 60 s", "check the RD link in a browser",
                         event=ev)
    log.info("mpv.loadfile", "done", result="PASS", ms=(time.monotonic() - t0) * 1000)
    if not unseen:
        # On screen and loaded: the viewer has the episode. The request this play took is done now, not after the
        # post-load work (a play killed during it must not be given back and told "ready" late).
        settle_taken(log, args, "remove", "a play of the episode loaded it on screen")
    # mpv keeps every property across files — `--reset-on-next-file` defaults to "Do not reset
    # anything" (installed man page) — so both the pause `--keep-open=yes` set at the end of the
    # last episode and a sub-delay nudge typed for it are still in force here.
    # Neither reset may fail the play; `reset_for_new_file` never raises.
    if unseen:
        mpv.reset_for_new_file(keep_paused=True)
    else:
        mpv.reset_for_new_file()
    tracks = mpv.command("get_property", "track-list") or []
    subs = [t for t in tracks if t.get("type") == "sub"]
    lang = settings["subtitleLanguage"]
    variant = setting_variant(settings)
    current = current_id = None
    try:
        current = mpv.command("get_property", "current-tracks/sub/lang")
    except StageError:
        pass
    id_error = None
    try:
        current_id = mpv.command("get_property", "current-tracks/sub/id")
    except StageError as exc:
        id_error = str(exc)
    # mpv's own track: by id, else the track-list's `selected` flag, else the first with mpv's tag. `on_from` says
    # which, on both `mpv.track` lines (with the id read's error when it failed).
    on, on_from = None, "none"
    for source, test in (("id", lambda t: current_id is not None and t.get("id") == current_id),
                         ("selected", lambda t: t.get("selected")),
                         ("tag", lambda t: current and t.get("lang") == current)):
        on = next((t for t in subs if test(t)), None)
        if on is not None:
            on_from = source
            break
    found_by = {"on_from": on_from, **({"id_error": id_error} if id_error else {})}
    # The second language (`subtitleFallbackLanguage`, the one the ranking rewarded) chooses the track when no track
    # has the viewer's, by the same steps. `off` leaves mpv's own choice.
    second = fallback_language(settings)
    second = second if second and second != lang else None
    second_variant = setting_variant(settings, "subtitleFallbackLanguage") if second else None
    pick, step, which = pick_sub_track(subs, on, lang, variant, second, second_variant)
    shown = which              # the table language of the track on screen
    if pick is not None:
        # A track picked by its title (untagged or `und`) is named by the language it was picked for, not its tag.
        current = f"title:{pick.get('title')}" if step == "title" else pick.get("lang")
    if pick is not None and pick is not on:
        mpv.command("set_property", "sid", pick["id"])
        extra = ({"second": second, "reason": f"no track has {lang}; the second language {second} chooses it"}
                 if which == second else {})
        log.info("mpv.track", "fallback", result="PASS", sid=pick["id"], lang=current, step=step,
                 tag=pick.get("lang"), variant=variant if which == lang else second_variant, was=current_id,
                 **found_by, **extra)
    observed = sorted({(t.get("lang") or "und") for t in subs})
    tracks_cache = CACHE_DIR / "tracks.json"
    known = cached_json(tracks_cache, None) or {}
    if result["chosen"]["info_hash"].lower() not in known:
        known[result["chosen"]["info_hash"].lower()] = observed
        write_cache(tracks_cache, known)
        record_group_langs(parse_title(result["chosen"]["title"]).get("stats_key", ""), observed, lang)
    audio = None
    try:
        audio = mpv.command("get_property", "current-tracks/audio/lang")
    except StageError:
        pass
    # Any region or script of the viewer's language is the viewer's language: the pick's step decides, not
    # the tag. `step` names the step that chose the track (`none`: no track has either language, mpv's choice).
    sub_fallback = shown != lang
    log.info("mpv.track", "selected", result="FALLBACK" if sub_fallback else "PASS",
             sub=current, audio=audio, subs=observed, step=(step if which == lang else "second") if pick else "none",
             tag=(pick or {}).get("lang"), variant=variant, kept=pick is on if pick else None, **found_by)
    # The container has no track in the preferred language, so borrow one from a sibling
    # release of the same episode. Everything below degrades to exactly the behaviour above;
    # `subs.prepare` never raises and `sub-add` failing leaves the current track selected.
    sideload = None
    sub_source = "embedded" if not sub_fallback else "none"
    if sub_fallback and as_bool(settings.get("sideloadSubtitles"), True):
        http_subs = Http(log, settings["httpTimeout"])
        sideload = subsmod.prepare(http_subs, log, ranked, chosen_candidate(ranked, result["chosen"]), settings,
                                   episode=args.episode, alt=(result.get("ids") or {}).get("alt_numbers"),
                                   anidb_eid=(result.get("ids") or {}).get("anidb_eid"),
                                   observed_langs=observed, trace=log.trace,
                                   track_source=getattr(args, "track_source", None))
        if sideload:
            try:
                readback = mpv.sub_add(sideload["path"], f"{subsmod.language_name(lang)} (external)", lang)
            except StageError as exc:
                log.warn("subs.load", "fail", result="FAIL", error=str(exc), path=sideload["path"],
                         suggest="mpv refused the file; playing with whatever track was already selected")
                readback = {"lang": None, "external": None}
            if subsmod.accept_readback(readback, lang):
                current, sub_fallback, sub_source, shown = readback["lang"], False, "sideload", lang
                sideload["external"] = readback.get("external")
            else:
                log.warn("subs.load", "readback", result="FAIL", lang=readback.get("lang"),
                         suggest="sub-add did not select the side-loaded track; leaving the embedded one")
                sideload = None
    result["playback"] = {"socket": str(MPV_SOCKET), "sub_lang": current, "audio_lang": audio, "sub_tracks": observed,
                          "media_title": media_title, "start": start,
                          "sub_fallback": sub_fallback, "sub_source": sub_source,
                          "sideload": {k: sideload.get(k) for k in
                                       ("path", "offset", "timing_verified", "anchor_lang", "match_ratio",
                                        "forced", "format", "tier_name", "from_cache")} if sideload else None}
    # Now-playing snapshot for the bar (cover, title, next). One AniList call; playback must not fail without it.
    media = None
    http = Http(log, settings["httpTimeout"])
    try:
        media = AniList(http, log, AniList.load_token()).media(args.anilist_id)
    except StageError as exc:
        log.warn("anilist.media", "skip", result="FAIL", error=str(exc), suggest="now-playing will lack cover and next")
    snapshot = {k: (media or {}).get(k) for k in ("id", "title", "episodes", "status", "format", "nextAiringEpisode",
                                                  "coverImage")}
    titles = snapshot.get("title") or {}
    progress = ((media or {}).get("mediaListEntry") or {}).get("progress") or 0
    list_status = ((media or {}).get("mediaListEntry") or {}).get("status")
    now = {
        "trace": log.trace, "mediaId": args.anilist_id, "episode": args.episode,
        "title": titles.get("english") or titles.get("romaji") or result["series_title"],
        "episodeTitle": result.get("episode_title"), "cover": (snapshot.get("coverImage") or {}).get("large"),
        "episodes": snapshot.get("episodes"), "infohash": result["chosen"]["info_hash"],
        "release": result["chosen"]["title"], "provider": stream["provider"], "startedAt": time.time(),
        "file": (result["chosen"].get("file") or {}).get("basename") or stream.get("filename"),
        "fromPack": bool(result["chosen"].get("file")),
        "subLang": current, "subFallback": sub_fallback, "subtitleLanguage": lang, "listProgress": progress,
        # A REPEATING entry's rewatch is gated on `listProgress`, not on resume.json (`do_tick`);
        # the forced title is what a `player restart` forces again.
        "listStatus": list_status, "mediaTitle": media_title,
        # The names the panel shows (`langName` falls back to the code for a file written before).
        "subtitleLanguageName": language_name(variant or lang),
        # The track by its own tag (`Portuguese (Brazil)`, `Arabic` for `ar-SA`); one picked by its title by the
        # language it was picked for.
        "subLangName": language_name(shown if not current or str(current).startswith("title:") else current)
        if (shown or current) else None,
        # Where the subtitles on screen came from, so the popout can say it.
        # A track picked by its title is named by the language it was picked for, not `title:…`.
        **subsmod.now_playing_keys(sideload, sub_source, shown if str(current or "").startswith("title:") else current,
                                   lang, stream["provider"]),
        "media": snapshot, "next": next_episode(snapshot, progress),
    }
    # The popout draws this cover from a file, never over https. One ~41 KB download on a
    # path that has already spent seconds finding a release; a failure only costs the placeholder.
    covers.fetch_many(http, log, covers.collect_urls(snapshot), budget_s=3.0)
    covers.attach_now_playing(now)
    playermod.write_now_playing(now)
    # The in-player episode list, derived from what `now` already holds. `http` is already
    # open for the covers fetch above, so thumbnails ride the same client.
    refresh_episode_panel(log, mpv, media or now.get("media"), int(now.get("listProgress") or 0), args.episode,
                          http=http)
    log.info("nowplaying", "write", result="PASS", path=str(playermod.NOW_PLAYING_FILE), mediaId=args.anilist_id,
             episode=args.episode, cover=bool(now["cover"]), coverPath=bool((now.get("coverPath") or {}).get("large")))
    result["now_playing"] = now
    result["window"] = window
    if args.show and not unseen:
        result["window"] = {**window, **playermod.show(log)}
    if unseen and waited.get("pending") == "watcher":
        # The watcher told the viewer about this episode first: one notification is enough.
        log.info("notify", "send", result="SKIP", episode=args.episode,
                 reason="the watcher already told the viewer this episode is ready")
    elif unseen:
        result["ready_notice"] = notifymod.send(
            log, waited.get("show") or result["series_title"],
            saymod.ready_body(args.episode, waited.get("original"), waited.get("subtitles")),
            [{"name": "play", "label": "Play", "args": ["player", "resume", str(args.anilist_id), str(args.episode)],
              "click": saymod.CLICK_PLAY}],
            why=f"the episode is ready and loaded paused: {ready_why}", sound=True)
        if result["ready_notice"].get("sent"):
            settle_taken(log, args, "remove", "a play of the episode loaded it paused and told the viewer")
        else:
            settle_taken(log, args, "unmark", "the play's own notification did not go out; the watcher tells instead")
    settle_taken(log, args, "remove", "a play of the episode streamed it")
    # No release carries the language, or the borrowed track's timing is unverified —
    # ask the subtitle databases in the background; the play does not wait for them.
    if sub_fallback or (sideload and sideload.get("timing_verified") is not True):
        result["external"] = spawn_external_worker(log, settings, args, result, ranked, mpv,
                                                   nothing_on=sub_fallback, donor_unverified=bool(sideload))
    if unseen:
        status.finish(saymod.ready_paused(args.episode), provider=stream["provider"], episode=args.episode,
                      releaseTitle=result["chosen"]["title"], readyPaused=True)
    else:
        status.finish(provider=stream["provider"], episode=args.episode, releaseTitle=result["chosen"]["title"])
    try:
        thumbsmod.maybe_prune(log)
    except Exception as exc:                            # noqa: BLE001
        log.warn("thumbs.prune", "after-play", result="FAIL", error=f"{type(exc).__name__}: {exc}",
                 suggest="the picture cache was not pruned; `omarchy-anime thumbs prune` does it by hand")
    # The borrowed subtitles, behind its own once-a-day stamp (`subs/.pruned`).
    try:
        subsmod.maybe_prune(log, settings)
    except Exception as exc:                            # noqa: BLE001
        log.warn("subs.prune", "after-play", result="FAIL", error=f"{type(exc).__name__}: {exc}",
                 suggest="the subtitle cache was not pruned; `omarchy-anime subs prune` does it by hand")
    result["play"] = playstate.public()
    return result


def reconnect_player(log: Log, exc: Exception) -> Mpv | None:
    """A player answering on the plugin's socket now (one restarted while the play waited), connected, or None when
    none is up. Logged `play.ready reconnect` with the new pid, or that no player answered."""
    t0 = time.monotonic()
    mpv = Mpv(log)
    up = MPV_SOCKET.exists() and mpv._try_connect() and mpv.healthy(timeout=3.0)
    pid = None
    if up:
        try:
            pid = int(mpv.command("get_property", "pid"))
        except (StageError, TypeError, ValueError):
            up = False
    log.info("play.ready", "reconnect", result="PASS" if up else "MISS", pid=pid, error=str(exc),
             ms=(time.monotonic() - t0) * 1000,
             reason="a player is up again on the plugin's socket (restarted during the wait): the episode loads into "
                    "it under the ready rule" if up else
                    "no player answers on the plugin's socket: the viewer is called back by the notification")
    return mpv if up else None


def tell_ready(args, log: Log, result: dict, why: str, sound: bool, told: str) -> dict | None:
    """The ready notification for an episode this play waited for and does not load: "Episode 3 is ready in Japanese
    with Arabic subtitles." with a Play action, whose click plays it (`player resume`: the player still has it, or a
    play of it starts). None when the watcher told the viewer first. A notice that went out settles the request this
    play took (`settle_taken`, logged with `told`)."""
    waited = result["waited"]
    if waited.get("pending") == "watcher":
        log.info("notify", "send", result="SKIP", episode=args.episode,
                 reason="the watcher already told the viewer this episode is ready")
        return None
    notice = notifymod.send(
        log, waited.get("show") or result["series_title"],
        saymod.ready_body(args.episode, waited.get("original"), waited.get("subtitles")),
        [{"name": "play", "label": "Play", "args": ["player", "resume", str(args.anilist_id), str(args.episode)],
          "click": saymod.CLICK_PLAY}],
        why=why, sound=sound)
    if notice.get("sent"):
        settle_taken(log, args, "remove", told)
    return notice


def take_the_player(args, log: Log, settings: dict, status: playstate.PlayStatus, result: dict) -> Mpv | None:
    """A play in the background has its episode: the player is asked again (`player_busy`). None when it waited
    and the player is still busy with the episode the viewer is watching: nothing of the player is touched. Otherwise
    the play takes the player and goes on as any play: its entries stop saying `background`, and the episode loads
    under the ready rule (a wait longer than `torboxWaitSeconds` loads paused, with the notification). A play that
    found its episode cached loads it at once even over the episode being watched: the viewer asked for it, and it is
    ready. After a wait the player is never launched: closed meanwhile, the viewer is called back by the notification
    (`ready_with_the_player_gone`); a cached play starts it as any play does. Logged `play.background take` or
    `keep`."""
    waited = bool(result.get("waited"))
    now = player_busy(log)
    if waited and now["busy"]:
        log.info("play.background", "keep", result="PASS", episode=args.episode, waited=True,
                 reason="TorBox has the episode, and the player is still busy with the episode the viewer is "
                        "watching: nothing of it is touched; the viewer is told and chooses when to switch")
        return None
    mpv = Mpv(log)
    if waited and not (MPV_SOCKET.exists() and mpv._try_connect()):
        log.info("play.background", "take", result="MISS", episode=args.episode, waited=True,
                 reason="TorBox has the episode, but no player is running any more: none is started by itself")
        raise ready_with_the_player_gone(args, log, settings, result, mpvmod.PlayerClosed(
            "mpv.closed", "no player answers on the plugin's socket", error="no player", command="connect"))
    if not waited:
        mpv.ensure_running()
    status.background = False
    log.info("play.background", "take", result="PASS", episode=args.episode, waited=waited, busy=now["busy"],
             reason="the episode was cached: the viewer asked for it and it is ready, so it replaces the one on the "
                    "player" if not waited else f"the player is not busy any more ({now['reason']}): the episode "
                    "loads under the ready rule")
    return mpv


def ready_in_the_background(args, log: Log, status: playstate.PlayStatus, result: dict) -> dict:
    """TorBox has the episode a play in the background waited for, and the player is still busy with the one the viewer
    is watching: nothing is loaded and nothing of the player is touched. The viewer is told by the ready
    notification with its sound (they chose when they started it; the switch is theirs now: its click, or the
    player's own next), and the play ends `later`, its line saying the episode is ready."""
    notice = tell_ready(args, log, result, "the episode is ready and the player is busy with the episode the viewer "
                        "is watching", sound=True, told="the play in the background told the viewer the episode is "
                        "ready")
    words = saymod.ready_in_the_background(args.episode)
    status.later(words, readyInBackground=True, notified=bool(notice and notice.get("sent")) or None)
    log.info("play.ready", "background", result="PASS", episode=args.episode,
             notified=bool(notice and notice.get("sent")), watcher_told=notice is None,
             reason="the episode is ready; the one being watched plays on, and the viewer switches when they choose")
    return {"ok": True, "later": True, "readyInBackground": True, "trace": log.trace, "anilist_id": args.anilist_id,
            "episode": args.episode, "message": words, "notice": notice, "play": playstate.public()}


def ready_with_the_player_gone(args, log: Log, settings: dict, result: dict, exc: Exception) -> mpvmod.PlayerClosed:
    """TorBox has the episode, but the player went away during the wait (closed, or a player restart, perhaps
    hours ago). Nothing is loaded and no window is brought back; the viewer is called back as a paused ready episode
    calls them: the ready notification, whose click starts the player and plays it (`player resume` plays it anew
    when the player does not have it), with the sound after a wait longer than `torboxWaitSeconds`. Unless the
    watcher told them first. The play then ends with the plain words, saying it is ready. Chosen over reopening the
    player: the viewer closed it, and a window that comes back by itself hours later is the surprise the ready rule
    avoids."""
    unseen, ready_why, wait_s = ready_unseen(result["waited"], settings)
    notice = tell_ready(args, log, result, f"the episode is ready but the player closed during the wait: {ready_why}",
                        unseen, "the player was gone at ready and the viewer was told")
    log.warn("play.ready", "player_gone", result="FAIL", episode=args.episode, wait_s=wait_s,
             notified=bool(notice and notice.get("sent")), sound=unseen, error=str(exc),
             cause=getattr(exc, "data", {}).get("error"),
             suggest="the player closed while the play waited for TorBox; the viewer was notified, and playing the "
                     "episode again starts a new player")
    words = saymod.player_closed(args.episode, ready=True)
    return mpvmod.PlayerClosed("mpv.closed", f"{words['headline']} {words['action']}", "", say=words,
                               error=getattr(exc, "data", {}).get("error"), command="get_property")


def starts_by_itself_s(settings: dict) -> float:
    """`torboxWaitSeconds`: the longest wait for TorBox after which the ready episode still starts by itself."""
    return float(settings.get("torboxWaitSeconds", DEFAULT_SETTINGS["torboxWaitSeconds"]))


def ready_unseen(waited: dict, settings: dict) -> tuple[bool, str, float | None]:
    """Whether an episode a wait for TorBox brought loads paused, why, and the wait's length: from the claim
    (`waited["claimed_at"]`, wall clock) to the moment TorBox had it (`ready_at`). The length is the only input: a
    wait of at most `torboxWaitSeconds` (60 s by default) plays by itself, as any play does; a longer one loads
    paused ("Space plays it") with the ready notification and its sound, since the viewer will have looked away.
    Whether the window is on screen, and whether the viewer pressed "Keep waiting", change nothing. 0 always loads it
    paused. The "Keep waiting" play from a hold (`until_done`) follows the same rule from its own claim (the `w` that
    started it)."""
    starts_s = starts_by_itself_s(settings)
    if not waited.get("claimed_at") or not waited.get("ready_at"):
        # No length to judge by: the safe side (an episode nobody watches must never play to the watched threshold).
        return True, ("the wait's length is not known (no claim or no ready time): it loads paused, and the viewer "
                      "is notified"), None
    wait_s = round(max(0.0, float(waited["ready_at"]) - float(waited["claimed_at"])), 1)
    if starts_s <= 0:
        return True, ("torboxWaitSeconds is 0: a ready episode always loads paused, and the viewer is notified"), wait_s
    if wait_s > starts_s:
        return True, (f"the wait was {wait_s:g} s, longer than torboxWaitSeconds ({starts_s:g} s): it loads paused "
                      f"and the viewer is notified"), wait_s
    return False, f"the wait was {wait_s:g} s, within torboxWaitSeconds ({starts_s:g} s): it plays", wait_s


def untagged(track: dict) -> bool:
    """A track mpv names no language for: no tag, or `und`."""
    return str(track.get("lang") or "und").lower() == "und"


def pick_sub_track(subs: list[dict], on: dict | None, lang: str, variant: str | None = None,
                   second: str | None = None, second_variant: str | None = None):
    """The subtitle track to show: for the viewer's language `lang`, the chosen `variant` (skipped when
    none), a bare tag, any other region or script of it, then an untagged (or `und`) track whose title names it; then
    the second language the same way. A track tagged with another language is not the viewer's by its title. Several
    tracks at one step: mpv's own (`on`) if it is one of them, else the first in track order.
    Returns (track, step, the table language it answers), or (None, None, None) when no track has either."""
    for code, var in ((lang, variant), (second, second_variant)):
        if not code:
            continue
        for step in (*(s for s in TRACK_STEPS if s != "exact" or var), "title"):
            found = [t for t in subs if (untagged(t) and track_title_names(t.get("title"), code) if step == "title"
                                         else tag_step(t.get("lang"), code, var) == step)]
            if found:
                return (on if on in found else found[0]), step, code
    return None, None, None


def chosen_candidate(ranked: list[dict], chosen: dict) -> dict:
    """The ranked record of the release a resolve chose (its tracks, its indexer), found by indexer and
    release id: two add-ons may number their releases alike. A minimal record when it is not there."""
    key = release_key(chosen)
    return next((c for c in ranked if release_key(c) == key), None) or {
        "id": chosen["id"], "indexer": chosen.get("indexer"), "title": chosen["title"],
        "info_hash": chosen["info_hash"], "parsed": parse_title(chosen["title"] or "")}


def list_answer(entries: list[dict], stale: dict | None) -> dict | list:
    """What `watching`, `list`, `top` and `airing` print: the bare list, as always, or, when AniList could not be read
    and the saved copy answered, `{"ok": true, "entries": [...], "stale": {savedAt, ageSeconds, reason}}`. `stale` is
    the read's own marker (`list_stale`, `top_stale`, `airing_stale`)."""
    rows = covers.attach(entries)
    return {"ok": True, "entries": rows, "stale": stale} if stale else rows


def do_anilist(args, log: Log, settings: dict) -> dict | list:
    http = Http(log, settings["httpTimeout"])
    client = AniList(http, log, AniList.load_token())
    # Every media-bearing payload leaves with `coverPath` next to `coverImage`: a local file the
    # shell can hand to Qt without a single https request. Nothing is downloaded here —
    # `covers warm` does that — so a cached list stays a local read.
    if args.cmd in ("watching", "list"):
        # `watching` is `list --status CURRENT`; it stays as its own command because Service.qml,
        # Panel.qml and docs/terminal.md all call it by that name.
        status = "CURRENT" if args.cmd == "watching" else str(getattr(args, "status", "CURRENT") or "CURRENT").upper()
        max_age = 0 if args.refresh else 300
        if args.user:
            entries = client.list_entries(status, user_name=args.user, max_age=max_age)
            return list_answer(entries, client.list_stale)
        if not client.token:
            # `reason` is what the shell reads to say "sign in" instead of "loading" forever.
            raise StageError("anilist.list", "not authenticated and no --user given", "omarchy-anime auth anilist",
                             reason="signed_out")
        if not client.user_id:
            # A token that names no user (no JWT `sub`) cannot read a list; it is a token
            # to replace, not a generic failure.
            raise StageError("anilist.list", "the AniList token names no user", "omarchy-anime auth anilist",
                             reason="token_rejected")
        entries = client.list_entries(status, user_id=client.user_id, max_age=max_age)
        return list_answer(entries, client.list_stale)
    # Top 100 and Airing answer an outage with their saved copy too, `--refresh` included.
    if args.cmd == "top":
        top = client.top100(max_age=0 if args.refresh else TOP100_MAX_AGE)
        return list_answer(top, client.top_stale)
    if args.cmd == "airing":
        airing = client.airing_from_top(max_age=0 if args.refresh else 86400)
        return list_answer(airing, client.airing_stale)
    if args.cmd == "search":
        return covers.attach(client.search(args.query, args.limit))
    if args.cmd == "media":
        m = client.media(args.anilist_id)
        entry = m.get("mediaListEntry") or {}
        m["next"] = next_episode(m, entry.get("progress") or 0)
        # `streamingEpisodes` is on the single-media query for `player.episode_stills` only, and
        # that runs inside the CLI (`do_episodes --fetch`, which calls `client.media` itself).
        # Nothing in the shell reads the key, and a long show lists one entry per episode — ONE
        # PIECE is ~1250 objects of title + thumbnail URL + site — so shipping it would push a
        # few hundred KB through the `Process` pipe and JSON.parse on every Browse detail open.
        m.pop("streamingEpisodes", None)
        return covers.attach(m)
    if args.cmd == "progress":
        # The one caller that may lower the list — the user typed the number.
        return client.save_progress(args.anilist_id, args.episode, args.complete,
                                    dry_run=bool(getattr(args, "dry_run", False)), allow_lower=True)
    if args.cmd == "flush-progress":
        return {"flushed": client.flush_pending()}
    raise StageError("cli", f"unknown anilist command {args.cmd}")


def media_cover_urls(ids: list[int], sizes=("large", "medium")) -> list[str]:
    """Cover URLs for media ids, from the local title cache that every list command writes
    (`anilist/titles/<id>.json`). No AniList request: a search warm must not cost a second call."""
    out: list[str] = []
    for mid in ids:
        cover = (known_titles(int(mid)) or {}).get("cover") or {}
        for key in sizes:
            if cover.get(key):
                out.append(cover[key])
    return out


def media_cover_map(ids: list[int]) -> dict[str, dict]:
    """{media id: {large, medium}} of what is on disk now — the shape the overlay merges into a
    search result list, so the QML never touches a cover URL."""
    out: dict[str, dict] = {}
    for mid in ids:
        cover = (known_titles(int(mid)) or {}).get("cover") or {}
        out[str(mid)] = {key: covers.have(cover.get(key) or "") for key in ("large", "medium")}
    return out


def media_id_words(log: Log, words: list[str]) -> list[int]:
    """`--media`'s words as AniList ids: ASCII digits naming an id `valid_media_id` takes. Any other word (a sign, a
    space, another script's digits, 0, past 2^31-1) is skipped with one WARN naming how many and the first few; the
    rest are warmed."""
    ids, bad = [], []
    for word in words:
        mid = valid_media_id(int(word)) if re.fullmatch(r"[0-9]{1,10}", str(word)) else None
        (ids if mid is not None else bad).append(mid if mid is not None else str(word))
    if bad:
        log.warn("covers.warm", "media_refused", result="FAIL", refused=len(bad), first=[log_value(w) for w in bad[:3]],
                 kept=len(ids), suggest="a --media word that is not an AniList id is skipped; the others are warmed")
    return ids


def do_covers(args, log: Log, settings: dict) -> dict:
    """`covers warm|prune|status` — the only place cover images are downloaded."""
    args.media = media_id_words(log, args.media)
    if args.action == "status":
        return covers.status()
    if args.action == "prune":
        out = covers.prune(max_age_days=args.max_age_days, max_bytes=args.max_bytes)
        log.info("covers.prune", "done", result="PASS", **out)
        return out

    t0 = time.monotonic()
    http = Http(log, settings["httpTimeout"])
    client = AniList(http, log, AniList.load_token())
    urls: list[str] = []
    media: dict[str, dict] = {}
    views = ["watching", "top", "airing"] if args.view == "all" else ([args.view] if args.view else [])
    for view in views:
        try:
            if view == "watching":
                if not client.token:
                    log.warn("covers.warm", "skip", result="FAIL", view=view, suggest="omarchy-anime auth anilist")
                    continue
                payload = client.watching(user_id=client.user_id)
            elif view == "top":
                payload = client.top100()
            else:
                payload = client.airing_from_top()
        except StageError as exc:
            log.warn("covers.warm", "skip", result="FAIL", view=view, error=str(exc))
            continue
        # The popout and the detail header draw the large cover; the list rows only need the small
        # one, which is ~9.5 KB against ~41 KB. Warming a hundred rows therefore costs about 1 MB.
        sizes = ("large", "medium") if view == "watching" else ("medium",)
        urls += covers.collect_urls(payload, sizes=sizes)
    if args.media:
        urls += media_cover_urls(args.media)
        # A title file written before covers existed (or by a version without the `cover` key) has
        # no URL to warm; the next live fetch of that media rewrites it. Say so rather than silently
        # returning an empty map.
        unknown = [mid for mid in args.media if not (known_titles(int(mid)) or {}).get("cover")]
        if unknown:
            log.warn("covers.warm", "unknown-media", result="FAIL", ids=unknown,
                     suggest="no cover URL in the local title cache yet; it lands on the next watching/top/search fetch")
    if args.url:
        urls += [u for u in args.url if u]

    requested = list(dict.fromkeys(urls))
    hit = [u for u in requested if covers.have(u)]
    paths = covers.fetch_many(http, log, requested, workers=args.workers, budget_s=args.budget)
    if args.media:
        media = media_cover_map(args.media)
    covers.maybe_prune(log)
    return {"requested": len(requested), "hit": len(hit), "downloaded": max(0, len(paths) - len(hit)),
            "failed": len(requested) - len(paths), "ms": round((time.monotonic() - t0) * 1000, 1),
            "media": media, "paths": paths}


def resolve_for_subs(args, log: Log, settings: dict) -> tuple[list[dict], dict, dict]:
    """The resolve half of a side-load: the full ranked list and the release that would be played.

    `subs find|fetch` run the real pipeline rather than a private one, so what the terminal reports
    is what a play would do. `--dry-run` semantics: nothing is written to any provider.
    """
    ranked: list[dict] = []
    result = do_resolve(args, log, settings, dry_run=True, ranked_sink=ranked)
    chosen = ranked[0] if ranked else {}
    return ranked, chosen, result


def do_thumbs(args, log: Log, settings: dict) -> dict:
    """`thumbs prune|status` -- the derived-picture cache. Sibling of `covers`/`subs`."""
    if args.action == "status":
        return thumbsmod.status()
    out = thumbsmod.prune(max_age_days=args.max_age_days, max_bytes=args.max_bytes)
    log.info("thumbs.prune", "done", result="PASS", **out)
    return out


def do_groupstats(args, log: Log, settings: dict) -> dict:
    """The learned group prior: what it knows, and rebuilding it from the log.

    `backfill` is a dry run unless `--apply` is given, because the prior feeds `group_prior` inside
    `score_candidate`, which computes both the pre-rank order and the final score — writing it
    shifts which release wins, not only which release is probed first.
    """
    lang = settings["subtitleLanguage"]
    if getattr(args, "action", "report") == "report":
        rep = group_stats_report()
        log.info("groupstats", "report", result="PASS", **{k: v for k, v in rep.items() if k != "path"})
        return {"lang": lang, **rep}
    res = backfill_group_stats(lang, apply=bool(getattr(args, "apply", False)))
    log.info("groupstats", "backfill", result="PASS" if res["applied"] else "DRY_RUN",
             lang=lang, rows=res["rows_considered"], skipped=res["skipped"],
             keys_before=res["before"]["keys"], keys_after=res["after"]["keys"],
             active_before=res["before"]["active"], active_after=res["after"]["active"],
             suggest="" if res["applied"] else "re-run with --apply to write it")
    return res


def do_subs(args, log: Log, settings: dict) -> dict:
    """`subs find|fetch|prune|status` — the side-load stage, testable from the terminal without
    playing anything."""
    if args.action == "status":
        return subsmod.status()
    if args.action == "prune":
        # The limit is `subsCacheMaxBytes`, as the settings say; `--max-bytes` still beats it.
        flag = args.max_bytes is not None
        limit = args.max_bytes if flag else settings["subsCacheMaxBytes"]
        out = subsmod.prune(max_age_days=args.max_age_days, max_bytes=limit)
        log.info("subs.prune", "done", result="PASS", max_bytes=limit,
                 max_bytes_from="--max-bytes" if flag else "subsCacheMaxBytes", **out)
        return out

    if args.anilist_id is None or args.episode is None:
        raise StageError("cli.subs", f"`subs {args.action}` needs an anilist id and an episode",
                         "omarchy-anime subs find <mediaId> <episode>")
    if args.action == "external":
        return do_subs_external(args, log, settings)
    http = Http(log, settings["httpTimeout"])
    ranked, chosen, result = resolve_for_subs(args, log, settings)
    lang = settings["subtitleLanguage"]
    out = {
        "trace": log.trace, "anilist_id": args.anilist_id, "episode": args.episode,
        "subtitle_language": lang,
        "chosen": {k: chosen.get(k) for k in ("id", "title", "info_hash", "seeders", "has_lang", "verified_langs")},
    }
    if chosen.get("has_lang") is True:
        out["needed"] = False
        out["reason"] = f"the chosen release already embeds {lang}; nothing to side-load"
        log.info("subs.sideload", "skip", result="PASS", reason=out["reason"], chosen_id=chosen.get("id"))
        return out
    out["needed"] = True
    track_source = args.track_source
    scan = subsmod.find_donor(http, log, ranked, chosen, settings,
                              episode=args.episode, alt=(result.get("ids") or {}).get("alt_numbers"),
                              budget_s=args.budget, workers=args.workers, track_source=track_source,
                              episode_unstated=bool(result.get("episode_unstated")))
    out.update({k: scan[k] for k in ("examined", "swept", "ms", "from_cache", "target_langs")})
    # find_donor may have verified the chosen release to learn the anchor language; report what it saw.
    out["chosen"]["verified_langs"] = chosen.get("verified_langs")
    out["donors"] = [{k: d.get(k) for k in ("indexer", "release_id", "title", "group", "source", "tier", "tier_name",
                                            "seeders", "track_id", "lang", "format", "forced",
                                            "default", "anchor_lang", "size")}
                     for d in scan["donors"][:10]]
    if args.action == "find":
        if args.offset and scan["donors"]:
            donor = scan["donors"][0]
            out["offset"] = {k: v for k, v in
                             subsmod.estimate_from_anchors(log, donor, chosen, "", donor["format"], settings,
                                                           track_source=track_source).items()}
        return out
    # fetch
    if not scan["donors"] and not track_source.offered:
        log.warn("subs.donor", "fetch", result="FAIL", loaded=[ix.name for ix in args.registry.loaded],
                 suggest="add an indexer add-on that offers subtitle tracks (subtitle_tracks and subtitle_text)")
        raise StageError("subs.donor", "No loaded indexer add-on can read subtitle tracks, so no release can "
                                       "donate one.",
                         "add an indexer add-on that offers subtitle tracks; docs/addons.md documents the interface")
    if not scan["donors"]:
        raise StageError("subs.donor", f"no release of this episode carries a {lang} subtitle track",
                         f"{scan['examined']} releases were read; the source platform likely has no {lang} for this title")
    donor = scan["donors"][0]
    fetched = subsmod.fetch_track(log, track_source, donor)
    if not fetched:
        raise StageError("subs.fetch", f"the {lang} track of {donor.get('title')} could not be downloaded",
                         "see the subs.fetch line in `omarchy-anime trace last`")
    est = subsmod.estimate_from_anchors(log, donor, chosen, fetched["text"], fetched["format"], settings,
                                        track_source=track_source)
    shifted = subsmod.shift_text(fetched["text"], fetched["format"], est["offset"])
    placed = subsmod.write_track(log, chosen.get("info_hash"), lang, donor, fetched, text=shifted,
                                 meta_extra={k: est[k] for k in ("offset", "match_ratio", "bucket_span",
                                                                 "timing_verified", "anchor_lang", "applied")},
                                 anidb_eid=(result.get("ids") or {}).get("anidb_eid"), trace=log.trace)
    out["offset"] = est
    out["fetched"] = {k: fetched.get(k) for k in ("path", "bytes_plain", "encoding", "format", "cached")}
    out["cached"] = placed
    subsmod.maybe_prune(log, settings)
    return out


def do_subs_external(args, log: Log, settings: dict) -> dict:
    """`subs external <id> <ep>`: what the subtitle databases have for this episode and
    which files would load. `--release-id INDEXER ID` names the playing release (its show ids come
    first) and `--release` its file name (ranking affinity). Nothing is downloaded yet."""
    http = Http(log, settings["httpTimeout"])
    # `--lang` folds an alias as the settings do (`zho` -> `chi`) and refuses a code the table lacks.
    # A variant (the setting's, or `--lang pt-BR`) is what the databases are asked for and what the track is named;
    # `extsubs` folds it to its language wherever it needs one.
    lang = (language_variant(args.lang) or language_code(args.lang)) if args.lang \
        else setting_variant(settings) or settings["subtitleLanguage"]
    if lang is None:
        raise StageError("subs.external", f"{args.lang!r} is not a language the plugin knows",
                         f"--lang takes one of: {', '.join([*LANGUAGE_TABLE, *LANGUAGE_VARIANTS])}")
    mapping = anizip_mappings(http, log, args.anilist_id)
    variants, _ = title_variants(args.anilist_id, mapping, log, lazy_anilist(http, log))
    loaded: list = []

    def registry() -> indexers.Registry:
        """The command's one registry, loaded on first need (show ids, the worker's timing references)."""
        if not loaded:
            loaded.append(indexers.load(settings, log))
        return loaded[0]

    release_ids, ids_source = playing_show_ids(http, log, settings, args.release_id, registry)
    plan = extsubs.episode_plan(mapping, args.episode, variants, release_ids=release_ids, ids_source=ids_source,
                                log=log, **plan_hints(args.anilist_id))
    if args.load:
        return subs_external_load(args, log, http, plan, lang, settings, registry)
    if args.fetch:
        reference = extsubs.reference_from_file(args.reference, log) if args.reference else None
        got = extsubs.prepare(http, log, plan, lang, args.anilist_id, playing_title=args.release or "",
                              reference=reference)
        return {"trace": log.trace, "anilist_id": args.anilist_id, "episode": args.episode, "lang": lang,
                "reference": {"file": args.reference, "cues": len(reference)} if reference else None,
                "tracks": [{k: t.get(k) for k in ("service", "name", "path", "offset", "timing_verified",
                                                 "match_ratio", "reason", "cues", "cached", "affinity",
                                                 "downloads", "file_id")} for t in got["tracks"]],
                "downloads": got["downloads"], "skipped": got["skipped"], "services": got["services"],
                "quota": extsubs.quota()}
    found = extsubs.search(http, log, plan, lang, playing_title=args.release or "")
    keys = ("service", "name", "release", "uploader", "downloads", "hearing_impaired", "machine", "format",
            "season", "episode", "pack", "zip", "check", "step", "affinity", "affinity_why", "file_id")
    return {
        "trace": log.trace, "anilist_id": args.anilist_id, "episode": args.episode, "lang": lang,
        "plan": {k: plan[k] for k in ("tmdb", "imdb", "ids_from", "season", "season_episode", "absolute",
                                      "numbering", "movie", "title")},
        "services": {svc: {"status": r["status"], "steps": r["steps"], "kept": len(r["kept"]),
                           "dropped": dict(r["dropped"]), "ms": r.get("ms")}
                     for svc, r in found["services"].items()},
        "candidates": [{k: c.get(k) for k in keys} for c in found["candidates"][:args.limit]],
        "would_load": [f"{c['service']}: {c['name']}" for c in found["candidates"][:extsubs.TOP_N]],
        "dry_run": True,
    }


def playing_show_ids(http: Http, log: Log, settings: dict, release_id, registry) -> tuple[dict, str]:
    """`--release-id INDEXER ID`: the playing release's show ids from its indexer add-on's `show_ids`
    and that indexer's name; `({}, "")` when none was named or none can be had. Never refuses:
    an indexer that is not loaded or lacks the capability, or an add-on that fails, leaves ani.zip's ids
    alone in the plan. The id is passed as typed (text); the add-on reads it as it numbers its releases.
    One `extsubs.plan release_ids` line says which it was. `registry()` gives the command's one `Registry`."""
    if not release_id:
        log.debug("extsubs.plan", "release_ids", result="SKIP", reason="no --release-id: ani.zip's ids only")
        return {}, ""
    indexer, rid = str(release_id[0]), str(release_id[1])
    reg = registry()
    source = indexers.ShowIds(reg, http, log, settings)
    ix = reg.find(indexer)
    if ix is None:
        # The ids' source may name its indexer in another case (an older build printed it in lower case); it
        # finds its add-on when exactly one loaded add-on has it.
        same = [x for x in reg.loaded if x.name.lower() == indexer.lower()]
        if len(same) == 1:
            ix = same[0]
            log.info("extsubs.plan", "release_id_name", result="PASS", typed=indexer, indexer=ix.name,
                     reason="no add-on has that exact name; one loaded add-on has it in another case")
            indexer = ix.name
    offered = source.names
    takes = (f"--release-id takes one of: {', '.join(offered)}" if offered
             else "no loaded indexer add-on offers show ids")
    if ix is None:
        log.warn("extsubs.plan", "release_ids", result="SKIP", indexer=indexer, release_id=rid,
                 reason=f"{indexer} is not a loaded indexer add-on", offered=offered,
                 loaded=[x.name for x in reg.loaded],
                 suggest=f"{takes}; the plan uses ani.zip's ids alone")
        return {}, ""
    if not source.offers(ix):
        log.info("extsubs.plan", "release_ids", result="SKIP", indexer=indexer, release_id=rid,
                 reason=f"{indexer} does not offer show ids", offered=offered,
                 suggest=f"the plan uses ani.zip's ids alone ({takes})")
        return {}, ""
    got = source.read({"indexer": indexer, "id": rid})
    ids = {k: v for k, v in ((got or {}).get("ids") or {}).items() if v is not None}
    status = (got or {}).get("status") or "failed"
    result = {"complete": "PASS", "empty": "EMPTY"}.get(status, "FAIL")
    emit = log.warn if result == "FAIL" else log.info
    emit("extsubs.plan", "release_ids", result=result, indexer=indexer, release_id=rid, ids=ids,
         suggest="" if ids else f"{indexer} gave no show ids for this release; the plan uses ani.zip's alone"
         if result == "EMPTY" else f"{indexer}'s show_ids failed or answered nothing usable (the indexers "
                                   f"show_ids line says why); the plan uses ani.zip's ids alone")
    return ids, (indexer if ids else "")


def subs_external_load(args, log: Log, http: Http, plan: dict, lang: str, settings: dict, registry=None) -> dict:
    """The detached worker: runs beside playback, adds the best three external files to the running
    player and updates the popout. Everything is checked against the episode it was started for."""
    expect = {"mediaId": args.anilist_id, "episode": args.episode, "infohash": args.info_hash}
    out = {"trace": log.trace, "anilist_id": args.anilist_id, "episode": args.episode, "lang": lang}
    check = extsubs.still_playing(expect)
    if not check["playing"]:
        log.info("extsubs.worker", "start", result="SKIP", reason="the episode is no longer playing",
                 by=check["by"], why=check["reason"])
        return {**out, "loaded": None, "reason": "the episode is no longer playing"}
    refs = reference_releases_arg(args.reference_releases, log)
    if args.reference:
        reference, used = extsubs.reference_from_file(args.reference, log), {"file": args.reference}
    elif refs:
        # The worker's own registry; the timing reference is read through the add-ons.
        reg = registry() if registry is not None else indexers.load(settings, log)
        track_source = indexers.SubtitleTracks(reg, http, log, settings)
        reference, used = extsubs.reference_from_releases(log, track_source, refs)
    else:
        reference, used = None, {}
    got = extsubs.prepare(http, log, plan, lang, args.anilist_id, playing_title=args.release or "",
                          reference=reference)
    tracks = got["tracks"]
    try:
        extsubs.maybe_prune(log)
    except Exception as exc:                            # noqa: BLE001
        log.warn("extsubs.prune", "worker", result="FAIL", error=f"{type(exc).__name__}: {exc}",
                 suggest="old shifted copies stay in extsubs/tracks until the next day's try")
    out.update({"reference": used or None, "tracks": [{k: t.get(k) for k in ("service", "name", "path", "offset",
                                                                          "timing_verified", "reason")}
                                                       for t in tracks], "skipped": got["skipped"]})
    if not tracks:
        log.info("extsubs.worker", "done", result="EMPTY", reason="nothing fits this episode")
        return {**out, "loaded": None}
    mpv = Mpv(log)
    if not mpv.healthy():
        log.warn("extsubs.worker", "mpv", result="FAIL", suggest="the player is gone; nothing to load into")
        return {**out, "loaded": None}
    may_replace = bool(args.nothing_on) or (bool(args.donor_unverified) and tracks[0].get("timing_verified") is True)
    loaded = extsubs.load_into_player(log, mpv, tracks, lang, expect, args.sid, may_replace)
    out["loaded"] = loaded
    out["now_playing"] = extsubs.update_now_playing(log, expect, lang, tracks, loaded, mpv)
    return out


def reference_releases_arg(value, log: Log) -> list[dict]:
    """The worker's `--reference-releases`: a JSON list of `{indexer, release_id}` (JSON, because an
    indexer's name may hold any character and a release id keeps its type). Junk is logged, not fatal."""
    if not value:
        return []
    try:
        refs = json.loads(value)
    except ValueError as exc:
        refs = exc
    if not isinstance(refs, list):
        log.warn("extsubs.worker", "references", result="FAIL", value=str(value)[:200],
                 suggest="--reference-releases is not a JSON list; no timing reference from the releases")
        return []
    return [release_key(r).as_dict()
            for r in refs if isinstance(r, dict) and r.get("indexer") and r.get("release_id") is not None]


def spawn_external_worker(log: Log, settings: dict, args, result: dict, ranked: list[dict], mpv: Mpv,
                          nothing_on: bool, donor_unverified: bool) -> dict | None:
    """Start `subs external --load` detached, under this play's trace, and return at once. Never raises.

    The timing references are the playing release and up to two same-source releases of the
    episode whose indexer offers subtitle tracks, each named by indexer and release id; none without it.
    Each is named by the indexer its tracks are asked of (`Registry.tracks_ref`), which may be a
    later indexer that returned the same release; the ranked record carries `also_from`, the public
    `chosen` does not."""
    if not as_bool(settings.get("sideloadSubtitles"), True):
        return None
    if not any((CONFIG_DIR / f"{s}-token").exists() for s in extsubs.SERVICES):
        log.info("extsubs.worker", "spawn", result="SKIP", reason="no Subdl or OpenSubtitles key")
        return None
    chosen = result.get("chosen") or {}
    try:
        sid = mpv.command("get_property", "sid")
    except StageError:
        sid = None
    reg = getattr(args, "registry", None) or indexers.Registry()
    refs: list[dict] = []
    chosen_rec = next((c for c in ranked if release_key(c) == release_key(chosen)), chosen)
    # The ranked record's reading, which knows the entry's season (a season pack is a pack, never a donor).
    chosen_parsed = chosen_rec.get("parsed") or parse_title(chosen.get("title") or "")
    chosen_ref = reg.tracks_ref(chosen_rec)
    if chosen_ref is not None and not chosen_parsed.get("batch"):
        refs.append(chosen_ref.as_dict())
    for c in ranked:
        found = reg.tracks_ref(c)
        ref = found.as_dict() if found is not None else None
        if len(refs) >= 3 or ref is None or ref in refs:
            continue
        parsed = c.get("parsed") or parse_title(c.get("title") or "")
        if parsed.get("batch") or not title_matches_episode(c.get("title") or "", args.episode,
                                                            any_number=bool(result.get("episode_unstated"))):
            continue
        tier, _ = subsmod.donor_tier(chosen_parsed, parsed, chosen_rec, c)
        if tier <= subsmod.TIER_SAME_SOURCE:
            refs.append(ref)
    release = (chosen.get("file") or {}).get("basename") or chosen.get("title") or ""
    # Each value glued to its option: a file named `-Show.S01E04.mkv` given as its own word is read as an
    # option, and the worker exits 2 before anything runs. Every word after the subcommand goes in `OMAANIME_ARGS`
    # (`private_args`): the show, the release and its file name are not for the command line.
    words = [str(args.anilist_id), str(args.episode), "--load", f"--info-hash={chosen.get('info_hash') or ''}",
             f"--sid={sid}", f"--release={release}", f"--reference-releases={json.dumps(refs)}"]
    words += ["--nothing-on"] if nothing_on else []
    words += ["--donor-unverified"] if donor_unverified else []
    argv, env = private_args.command(["--trace", log.trace, "subs", "external"], words)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    except OSError as exc:
        log.warn("extsubs.worker", "spawn", result="FAIL", error=str(exc))
        return None
    log.info("extsubs.worker", "spawn", result="PASS", pid=proc.pid, sid=sid, reference_releases=refs,
             nothing_on=nothing_on, donor_unverified=donor_unverified)
    return {"pid": proc.pid, "reference_releases": refs}


# The sheet's first screen: the keys a user turns while watching.
# --------------------------------------------------------------------------- accounts
#
# Three providers, one shape. A token is verified against its provider **before** the file is
# written, so a typo never replaces a working token with a broken one, and the token itself never
# leaves `~/.config/omaanime/<provider>-token` (mode 600) — not into argv, not into the log,
# not into `shell.json`, not into this command's own output.

PROVIDERS = ("anilist", "torbox", "realdebrid", "subdl", "opensubtitles")

ACCOUNTS_FILE = "accounts.json"
ACCOUNTS_TTL = 600.0            # 10 minutes; `--refresh` ignores it

SIGN_IN_PAGES = {
    "torbox": "https://torbox.app/settings",
    "realdebrid": "https://real-debrid.com/apitoken",
    # Subdl's own docs link "Get an API key" here. OpenSubtitles' key belongs to a "consumer";
    # the page answers automated reads with 403, so this address is from its docs, not fetched.
    "subdl": "https://subdl.com/panel/api",
    "opensubtitles": "https://www.opensubtitles.com/en/consumers",
}


def sign_in_url(provider: str, settings: dict) -> str:
    """Where the user gets a token. AniList's is built from the configured client id rather than a
    constant, because `anilistClientId` is a setting (an implicit-grant id is public)."""
    if provider == "anilist":
        return AniList.pin_url(as_int(settings.get("anilistClientId"), 51175))
    return SIGN_IN_PAGES.get(provider, "")


def verify_token(provider: str, token: str, log: Log, settings: dict) -> dict:
    """Ask the provider who this token belongs to. Raises StageError when it does not resolve."""
    http = Http(log, settings["httpTimeout"])
    if provider == "anilist":
        who = AniList(http, log, token).viewer()
        if not who.get("id"):
            raise StageError("auth.verify", "AniList did not recognise that token",
                             "copy the whole token from the pin page")
        return {"user": who.get("name"), "userId": who.get("id"), **token_expiry(token)}
    if provider in extsubs.SERVICES:
        return extsubs.verify_key(provider, token, http, log)
    if provider == "torbox":
        u = TorBox(http, log, token).user() or {}
        # `plan` stays TorBox's number; `planName` is what the Accounts row shows, None for an undocumented
        # one.
        name = torbox_plan_name(u.get("plan"))
        log.emit("debug" if name else "info", "accounts", "plan", result="PASS" if name else "UNKNOWN",
                 provider="torbox", plan=u.get("plan"), name=name,
                 suggest="" if name else "TorBox documents plans 0-3 on /v1/api/user/me; the row shows no plan")
        return {"plan": u.get("plan"), "planName": name, "expiresAt": u.get("premium_expires_at"),
                "subscribed": u.get("is_subscribed")}
    u = RealDebrid(http, log, token).user() or {}
    if not u.get("username"):
        raise StageError("auth.verify", "Real-Debrid did not recognise that token",
                         "re-create it at https://real-debrid.com/apitoken")
    return {"user": u.get("username"), "plan": u.get("type"), "expiresAt": u.get("expiration"),
            "premium": u.get("premium")}


def read_secret(provider: str, from_stdin: bool, settings: dict) -> str:
    """The token, from stdin or from an un-echoed prompt. **Never from argv**: a value on
    the command line lands in the shell history and in `/proc/<pid>/cmdline`, where any process of
    the same user can read it for as long as the command runs."""
    if from_stdin:
        return sys.stdin.readline().strip()
    sys.stderr.write(f"Open this page, sign in, and copy the token:\n  "
                     f"{sign_in_url(provider, settings)}\n")
    sys.stderr.flush()
    # `getpass` falls back to a warning and a plain read when there is no terminal. That is a
    # documented degradation, not a regression: before this, the prompt echoed unconditionally.
    return getpass.getpass("token (not echoed): ", stream=sys.stderr).strip()


def device_event(payload: dict) -> None:
    """One sign-in progress line on stderr for the sheet: compact JSON with `"event":
    "device"`, which no log line carries, then a plain sentence for a person at a terminal. The
    final result stays on stdout, the same shape as a pasted token's."""
    line = {"event": "device", **payload}
    sys.stderr.write(json_controls(redact_text(json.dumps(line, ensure_ascii=False, separators=(",", ":")))) + "\n")
    state = payload.get("state")
    if state == "code":
        text = (f"Open {payload.get('url')} and enter the code {payload.get('code')}"
                f" (or scan the QR code in Accounts). It is valid for {round((payload.get('window') or 600) / 60)} minutes.")
    elif state == "pending":
        text = f"waiting for approval… ({payload.get('left')} s left)"
    elif state == "transient":
        text = f"TorBox did not answer; asking again ({payload.get('left')} s left)"
    else:
        text = {"granted": "approved; checking the token with TorBox…", "expired": "the code expired",
                "denied": "the sign-in was declined", "failed": "the sign-in failed",
                "saved": "signed in to TorBox; the token is saved with mode 600",
                "cancelled": "cancelled; no token was saved"}.get(str(state), "")
    if text:                        # TorBox's url and code as given: printed, never acted on by the terminal
        sys.stderr.write(escape_controls(redact_text(text), lines=False) + "\n")
    sys.stderr.flush()


def do_auth_device(args, log: Log, settings: dict) -> dict:
    """`auth torbox --device`: show a code, wait for the user to approve it at TorBox, then
    save the token through the same verify-write-read-back path as a pasted one."""
    http = Http(log, settings["httpTimeout"])
    try:
        started = torbox_device.start(http, log)
        shown = torbox_device.public(started)
        device_event({"state": "code", **shown, "qr": torbox_device.qr_rows(shown["verificationUrl"], log)})
        token = torbox_device.wait_for_token(http, log, started, device_event)
    except KeyboardInterrupt:
        # Only the wait is interruptible this way; the save after it is not wrapped, so a Ctrl-C
        # there is Python's own and cannot be mistaken for "nothing was saved".
        device_event({"state": "cancelled"})
        raise Interrupted("torbox.device.wait", "Sign-in cancelled; no token was saved", torbox_device.AGAIN,
                          deviceState="cancelled") from None
    out = save_verified_token("torbox", token, log, settings)
    device_event({"state": "saved"})
    return {**out, "method": "device"}


def do_auth(args, log: Log, settings: dict) -> dict:
    provider = str(args.provider)
    if getattr(args, "device", False):
        if provider != "torbox":
            raise StageError("auth.device", f"--device is TorBox's sign-in; {provider} takes a token",
                             f"omarchy-anime auth {provider}")
        return do_auth_device(args, log, settings)
    token = read_secret(provider, args.token == "-", settings)
    if not token:
        raise StageError("auth.read", "no token given",
                         f"printf '%s\\n' \"$TOKEN\" | omarchy-anime auth {provider} --token -")
    return save_verified_token(provider, token, log, settings)


def save_verified_token(provider: str, token: str, log: Log, settings: dict) -> dict:
    """Verify with the provider, then write mode 600 and read it back. The one save path, whether
    the token was pasted or came from TorBox's device code, so the two cannot drift. The scrubber knows the token
    before the check sends it: a service whose refusal repeats it must not put it in the log, the answer or `trace`."""
    remember_secret(token)
    if len(token) < TOKEN_MIN_LEN:                  # never sent: no real token is this short (`core.TOKEN_MIN_LEN`)
        log.warn("auth", "verify", result="FAIL", provider=provider, length=len(token), min_length=TOKEN_MIN_LEN,
                 reason="too short to be a token; not sent")
        raise StageError("auth.read", f"that is {len(token)} characters; a {provider} token is {TOKEN_MIN_LEN} or more",
                         "copy the whole token from the provider's page and paste it again")
    t0 = time.monotonic()
    who = verify_token(provider, token, log, settings)
    log.info("auth", "verify", result="PASS", provider=provider, ms=(time.monotonic() - t0) * 1000)
    path = write_token(provider, token)
    # Read it back, and check the mode rather than trusting the write.
    mode = token_mode(provider)
    ok = read_token(provider) == token
    log.emit("info" if ok and mode == "600" else "error", "auth", "save",
             result="PASS" if ok and mode == "600" else "FAIL", provider=provider,
             path=str(path), mode=mode,
             suggest="" if ok and mode == "600" else
             f"the token file at {home_shown(str(path))} did not come back as written")
    if not ok:
        raise StageError("auth.save", f"{path} did not read back as written", "check the filesystem")
    invalidate_derived(provider, log)
    return {"provider": provider, "saved": True, "mode": mode, **who}


def derived_caches(provider: str) -> list[Path]:
    """What a token produced, and must not outlive it. Signing out of AniList that left the cached
    list behind would show the previous account's shows to the next one."""
    out = [CACHE_DIR / ACCOUNTS_FILE]
    if provider == "anilist":
        # Top 100 carries the viewer's `mediaListEntry` (it hides completed shows), and Airing is
        # built from it. Written signed out or with a rejected token, both hold `null` entries for a day.
        out += [CACHE_DIR / "anilist" / TOP100_CACHE_NAME, CACHE_DIR / "anilist" / "airing-from-top.json"]
        out += sorted((CACHE_DIR / "anilist").glob("watching-*.json")) if (CACHE_DIR / "anilist").exists() else []
        out += sorted((CACHE_DIR / "anilist").glob("list-*.json")) if (CACHE_DIR / "anilist").exists() else []
        # The fallback-only copies a progress write leaves (`anilist.saved_copy`): the same account's lists.
        out += sorted((CACHE_DIR / "anilist").glob("saved-*.json")) if (CACHE_DIR / "anilist").exists() else []
    return out


def invalidate_derived(provider: str, log: Log | None = None) -> list[str]:
    """At a sign-in and a sign-out: the token's caches (`derived_caches`), and for AniList the queued writes that name
    no account (`anilist.drop_unowned_pending`)."""
    removed = []
    for path in derived_caches(provider):
        try:
            if path.exists():
                path.unlink()
                removed.append(str(path))
        except OSError:
            continue
    if provider == "anilist" and log is not None:
        try:
            if anilistmod.drop_unowned_pending(log):
                removed.append(str(anilistmod.pending_path()))
        except OSError as exc:
            log.warn("auth", "signout", result="FAIL", provider=provider, error=str(exc),
                     suggest="the queued AniList writes without an account could not be dropped")
    return removed


# The records one account left that a sign-out removes beside its caches: the next account must not inherit them
# (docs/privacy.md, "Signing out"). Each shared file loses this provider's entry only.
SHARED_RECORDS = {"torbox": lambda: providersmod.block_file(), "realdebrid": lambda: providersmod.block_file(),
                  "subdl": lambda: extsubs._quota_path(), "opensubtitles": lambda: extsubs._quota_path()}


def remove_account_records(provider: str, log: Log) -> tuple[list[str], list[str]]:
    """What a sign-out removes beside the token's caches: the records the account left (`SHARED_RECORDS`; for TorBox
    its pending downloads and prefetch record, under their locks). Each on its own: one that cannot be removed is
    logged with its path and error, and the rest still go. Returns (removed, failed)."""
    removed: list[str] = []
    failed: list[str] = []

    def attempt(shown: str, step) -> None:
        try:
            if step():
                removed.append(shown)
        except OSError as exc:
            failed.append(shown)
            log.warn("auth", "signout.record", result="FAIL", provider=provider, path=home_shown(shown),
                     error=f"{type(exc).__name__}: {exc.strerror or exc}",
                     suggest=f"this record of the signed-out account is still there; delete {home_shown(shown)}")

    def unlink(locked, path) -> bool:
        with locked(log):
            if not path().exists():
                return False
            path().unlink()
            return True

    def drop_entry(path) -> bool:
        store = cached_json(path, None)
        if not (isinstance(store, dict) and provider in store):
            return False
        store.pop(provider)
        write_cache(path, store)
        return True

    if provider == "torbox":                    # TorBox torrent ids of the account: the watcher's and the prefetch's
        for locked, path in ((pendingmod._locked, pendingmod.path), (prefetchmod._locked, prefetchmod.path)):
            attempt(str(path()), lambda locked=locked, path=path: unlink(locked, path))
    shared = SHARED_RECORDS.get(provider)
    if shared is not None:
        path = shared()
        attempt(f"{path}#{provider}", lambda: drop_entry(path))
    return removed, failed


def do_signout(args, log: Log, settings: dict) -> dict:
    provider = str(args.provider)
    existed, path = remove_token(provider)
    removed = invalidate_derived(provider, log)
    records, failed = remove_account_records(provider, log)
    removed += records
    log.emit("warn" if failed else "info", "auth", "signout", result="PARTIAL" if failed else "PASS",
             provider=provider, removed=existed, path=str(path), caches=len(removed), records=records,
             failed=failed or None, suggest="some records of the account are still there; see the lines above"
             if failed else "")
    return {"provider": provider, "removed": existed, "path": str(path), "caches": removed, "failed": failed}


def account_state(provider: str, log: Log, settings: dict) -> dict:
    """One provider's state. `present` is the **file's** existence, never the call's success, so a
    provider that is down reads as "signed in, and here is the error" rather than as signed out."""
    out: dict = {"present": False, "signInUrl": sign_in_url(provider, settings)}
    try:
        token = read_token(provider)
    except StageError as exc:
        path = CONFIG_DIR / f"{provider}-token"
        if path.exists():                       # present but unreadable: a mode problem, say so
            out.update({"present": True, "mode": token_mode(provider), "error": str(exc)})
        return out
    out.update({"present": True, "masked": redact(token), "mode": token_mode(provider)})
    if provider == "anilist":
        # No network: the expiry is a claim inside the token we already hold.
        out.update(token_expiry(token))
        return out
    try:
        out.update(verify_token(provider, token, log, settings))
    except StageError as exc:
        out["error"] = str(exc)
    return out


def do_accounts(args, log: Log, settings: dict) -> dict:
    """The providers, plus `indexers`: which add-ons loaded and which were refused. The
    add-on block is read fresh on every call and never cached, so a file dropped in or fixed shows
    the next time the sheet opens rather than ten minutes later; reading it is local (an
    add-on does no network work at import)."""
    path = CACHE_DIR / ACCOUNTS_FILE
    addons = {"indexers": indexers.load(settings, log).status(), "torznab": torznab_accounts.listing()}
    if not getattr(args, "refresh", False):
        cached = cached_json(path, ACCOUNTS_TTL)
        if isinstance(cached, dict) and cached.get("anilist"):
            log.debug("accounts", "cache", result="PASS", age_s=round(time.time() - path.stat().st_mtime))
            return {**{p: cached[p] for p in PROVIDERS if p in cached}, **addons}
    out = {p: account_state(p, log, settings) for p in PROVIDERS}
    write_cache(path, out)
    # It carries a username and a plan expiry. Not a secret, but not the world's business either.
    set_private(path, 0o600)
    log.info("accounts", "read", result="PASS",
             present=[p for p in PROVIDERS if out[p].get("present")],
             errors=[p for p in PROVIDERS if out[p].get("error")])
    return {**out, **addons}


def read_torznab_server(args) -> tuple[str, str]:
    """(address, API key) for `torznab add`. The key never comes from argv: from stdin with
    `--stdin` (the address first, unless it is given as an argument, then the key, one line each; the Accounts
    sheet sends both this way, so a pasted address that holds a key is not on a command line either), or from
    an un-echoed prompt."""
    url = str(args.server or "")
    if getattr(args, "stdin", False):
        if not url:
            url = sys.stdin.readline().strip()
        return url, sys.stdin.readline().strip()
    if not url:
        sys.stderr.write("The address of your Prowlarr (e.g. http://host:9696) or a Torznab feed URL: ")
        sys.stderr.flush()
        url = sys.stdin.readline().strip()
    return url, getpass.getpass("API key (not echoed; empty for none): ", stream=sys.stderr).strip()


def do_addons(args, log: Log, settings: dict) -> "dict | str":
    """`addons spec|check|prompt`. `spec` and `prompt` answer plain text (a `str`, which `main` prints as it is),
    or JSON with `--json`; `check` answers JSON, with a summary for a person on stderr."""
    where = addon_help.context()
    if args.addons_cmd == "spec":
        log.info("addons", "spec", result="PASS", json=args.json, **where)
        return addon_help.spec_data(where["cmd"], where["dir"]) if args.json else \
            addon_help.spec_text(where["cmd"], where["dir"])
    if args.addons_cmd == "prompt":
        if args.kind is None and not args.json:
            raise StageError("addons.prompt", f"name the prompt: {' or '.join(addon_help.KINDS)}",
                             "or pass --json for all of them")
        kinds = [args.kind] if args.kind else list(addon_help.KINDS)
        log.info("addons", "prompt", result="PASS", kinds=kinds, json=args.json, **where)
        if args.json:
            return {"prompts": {k: addon_help.prompt(k, where["cmd"], where["dir"]) for k in kinds}, **where}
        return addon_help.prompt(args.kind, where["cmd"], where["dir"])
    if args.live and not (args.title or "").strip():
        raise StageError("addons.check", "--live needs --title: the show the real indexer is searched for",
                         'e.g. --live --title "<a show>" --episode 1')
    out = addon_check.check(Path(args.file), log, settings, call=not args.no_call, live=args.live,
                            title=(args.title or "").strip() or None, episode=args.episode, timeout=args.timeout)
    # The add-on's and an indexer's own text is in it: scrubbed, and printed with its control characters escaped.
    sys.stderr.write(home_shown(escape_controls(redact_text(addon_check.summary(out)))))
    return out


def do_torznab(args, log: Log, settings: dict) -> dict:
    """`torznab list|add|check|remove|find-local`: the Accounts sheet's Torznab servers. The key is never
    printed. `find-local` asks loopback's Prowlarr and Jackett ports without a key and saves nothing."""
    action = str(args.action)
    if action == "list":
        return torznab_accounts.listing()
    if action == "find-local":
        # It asks fixed loopback addresses only; an address, id or name given to it would be silently ignored.
        given = [n for n, v in (("an address", args.server), ("--id", args.id), ("--name", args.name)) if v]
        if given:
            raise StageError("torznab.find-local", f"find-local takes no {' or '.join(given)}",
                             "it asks only 127.0.0.1:9696 and 127.0.0.1:9117; run `omarchy-anime torznab find-local`")
        return torznab_local.find(log)
    if action == "remove":
        if not (args.server or args.id):
            raise StageError("torznab.remove", "Which server? Give its address or --id", "omarchy-anime torznab list")
        return torznab_accounts.remove(log, url=args.server or "", row_id=args.id or "")
    http = Http(log, settings["httpTimeout"])
    if action == "check":
        return torznab_accounts.check(http, log, settings, args.server or None, args.budget, row_id=args.id or "")
    url, key = read_torznab_server(args)
    return torznab_accounts.add(http, log, settings, url, key, name=args.name or "", budget_s=args.budget)


def do_episodes(args, log: Log, settings: dict) -> dict:
    """`episodes <mediaId>` — the in-player panel's payload, derived from `now-playing.json` and the
    cached ani.zip mapping. No network call: everything it needs is already local.

    Without `--write` it only prints, so the file the player is reading cannot be changed by
    inspecting it.
    """
    np = playermod.read_now_playing() or {}
    media_id = int(args.anilist_id or np.get("mediaId") or 0)
    if not media_id:
        raise StageError("cli.episodes", "no media id and nothing is playing",
                         "omarchy-anime episodes <mediaId>")
    now_playing_show = np.get("mediaId") == media_id and np.get("media")
    if now_playing_show and not getattr(args, "fetch", False):
        media, progress = np["media"], int(np.get("listProgress") or 0)
        playing = int(np.get("episode") or 0) or None
        source = "now-playing"
    else:
        # Not the playing show, or `--fetch` was given: the now-playing snapshot is a deliberately
        # small 7-key slice (it is polled every tick by the popout) and drops `streamingEpisodes`,
        # so `episode_stills` sees nothing from it. Read the full media through the CLI's own
        # (cached) AniList client either way; `progress`/`playing` still come from now-playing when
        # this is the playing show, since that is the live truth for those two fields.
        http = Http(log, settings["httpTimeout"])
        client = AniList(http, log, AniList.load_token())
        media = client.media(media_id)
        if now_playing_show:
            progress = int(np.get("listProgress") or 0)
            playing = int(np.get("episode") or 0) or None
            source = "now-playing"
        else:
            progress = int((media.get("mediaListEntry") or {}).get("progress") or 0)
            playing, source = None, "anilist"
    payload = playermod.build_episodes(media, progress, playing, log=log)
    fetched = False
    if getattr(args, "fetch", False):
        http = Http(log, settings["httpTimeout"])
        if not (playermod.MAPPINGS_DIR / f"{media_id}.json").exists():
            try:
                anizip.anizip_mappings(http, log, media_id)
                payload = playermod.build_episodes(media, progress, playing, log=log)   # now with titles/images
            except StageError as exc:
                log.warn("episodes", "mapping", result="FAIL", mediaId=media_id, error=str(exc),
                         suggest="the grid shows numbers and titles only until the mapping is reachable")
        try:
            thumbsmod.ensure(http, log, media_id, payload["episodes"], thumbsmod.THUMB_W, thumbsmod.THUMB_H,
                             anchor=(playing or progress + 1))
            thumbsmod.ensure_show_art(http, log, media_id, payload.get("fanartUrl", ""), payload.get("logoUrl", ""))
            fetched = True
        except Exception as exc:                  # noqa: BLE001
            log.warn("thumbs", "episodes", result="FAIL", mediaId=media_id, error=f"{type(exc).__name__}: {exc}")
    thumbsmod.attach(payload, thumbsmod.THUMB_W, thumbsmod.THUMB_H)
    payload["fetched"] = fetched
    log.info("episodes", "build", result="PASS", mediaId=media_id, source=source,
             count=len(payload["episodes"]), progress=payload["progress"], playing=payload["playing"])
    if args.write:
        path = playermod.write_episodes(media_id, payload)
        log.info("episodes", "write", result="PASS", path=str(path), mediaId=media_id)
        payload["path"] = str(path)
    return payload


EPISODES_SCRIPT = Path(__file__).resolve().parent.parent.parent / "mpv" / "omarchy-anime-episodes.lua"
EPISODES_SCRIPT_KEY = "omarchy-anime/episodes"
EPISODES_SCRIPT_NAME = "omarchy_anime_episodes"


def refresh_episode_panel(log: Log, mpv: Mpv, media: dict | None, progress: int, playing: int | None,
                          http=None) -> None:
    """Rewrite `episodes/<mediaId>.json` and tell the in-player panel to re-read it.

    Every step degrades to nothing: a player without the script answers `error running
    command` to the message, an unwritable cache logs and returns, and the play continues either
    way. Costs no network call of its own — everything comes from the payload the caller already
    holds plus the ani.zip mapping already on disk — unless `http` is given, in which case
    thumbnails get a best-effort top-up too.
    """
    media_id = int((media or {}).get("id") or 0)
    if not media_id:
        return
    try:
        payload = playermod.build_episodes(media, progress, playing, log=log)
        # Attaching a thumbnail already sitting on disk needs no
        # http client -- only *fetching a new one* does. Without this, a caller with no client
        # (`resume_into`, reached from a player restart) drew a text-only panel forever, even for
        # episodes `ensure()` had already converted for this exact show. Best-effort either way:
        # a caller with no client, or a fetch that fails, still writes exactly the
        # payload it wrote before, and the panel renders the text rows it always did. This has to
        # run *before* `write_episodes` below — the JSON on disk is the only thing the Lua panel
        # reads, so a picture path added after the write would never reach it. `attach`
        # reads disk only, which is exactly what a caller with no client needs here; that rule still
        # holds, `cached()` is no longer the mechanism.
        try:
            if http is not None:
                anchor = int(playing) if playing else int(progress or 0) + 1
                thumbsmod.ensure(http, log, payload["mediaId"], payload["episodes"],
                                 thumbsmod.THUMB_W, thumbsmod.THUMB_H, anchor=anchor)
                thumbsmod.ensure_show_art(http, log, payload["mediaId"], payload.get("fanartUrl", ""),
                                          payload.get("logoUrl", ""))
            thumbsmod.attach(payload, thumbsmod.THUMB_W, thumbsmod.THUMB_H)
        except Exception as exc:                  # noqa: BLE001
            log.warn("thumbs", "panel", result="FAIL", error=f"{type(exc).__name__}: {exc}",
                     suggest="the episode panel renders without pictures")
        path = playermod.write_episodes(media_id, payload)
    except Exception as exc:                   # noqa: BLE001 - the play-start path must not die on a
        # corrupt mapping or an unexpected write failure.
        log.warn("episodes", "write", result="FAIL", mediaId=media_id, error=f"{type(exc).__name__}: {exc}",
                 suggest="the in-player episode panel will show stale data")
        return
    log.info("episodes", "write", result="PASS", path=str(path), mediaId=media_id,
             count=len(payload["episodes"]), progress=payload["progress"], playing=payload["playing"])
    # A player started before this plugin version has no --script=; load it once over IPC.
    if EPISODES_SCRIPT.exists():
        mpv.ensure_script(str(EPISODES_SCRIPT), EPISODES_SCRIPT_KEY)
    mpv.script_message_to(EPISODES_SCRIPT_NAME, "reload", str(media_id))


def loading_art(log: Log, http, media_id: int, episode: int, title: str, budget_s: float = 1.5,
                clock: Callable[[], float] | None = None) -> dict:
    """The loading screen's pictures: the blurred still of this episode as the
    backdrop source (else the blurred fanart), the show's logo, and the two title strings. Runs
    between the claim and the resolve, so it is capped, best-effort, and never raises. On an
    ordinary play the still was pre-blurred by the last watched tick's rebuild, so this is one
    local ffmpeg run and no network; a cold show or a jump outside the window pays up to the cap.

    `clock` is the one time source for this function's deadline and its per-sub-step timings
    (`time.monotonic` when None); a test passes a fake one so it can assert on those numbers
    without racing ffmpeg."""
    clock = clock or time.monotonic
    t0 = clock()
    deadline = t0 + max(0.0, float(budget_s))
    out = {"backdrop": "", "logo": "", "still": "", "title": str(title or ""), "episodeTitle": ""}
    # A cold show spends the whole budget and can still come back with nothing, and the
    # single `ms` on the done line cannot say which sub-step ate it. These are that breakdown; they
    # change no behaviour and no budget, they only make the next decision about the cap measurable
    # instead of guessed at. A step that never ran stays 0.0.
    spent = {"mapping_ms": 0.0, "still_ms": 0.0, "showArt_ms": 0.0, "backdrop_ms": 0.0}

    def left() -> float:
        return max(0.0, deadline - clock())

    # Entering a step that cannot finish is worse than skipping it: the mapping fetch is one HTTP
    # round trip to ani.zip and the measured cold case spent it and then had nothing left for the
    # pictures the mapping was fetched *for*. Below this floor the resolve will fetch the mapping a
    # moment later anyway (`map.anizip` runs in the search stage), so nothing is lost but the wait.
    MAPPING_FLOOR_S = 0.5

    try:
        media_id, episode = int(media_id), int(episode)
        if not (playermod.MAPPINGS_DIR / f"{media_id}.json").exists():
            if left() < MAPPING_FLOOR_S:
                log.info("play.art", "mapping", result="SKIP", mediaId=media_id, left=round(left(), 3),
                         floor=MAPPING_FLOOR_S,
                         suggest="the mapping will be fetched by the resolve; the loading screen shows text only")
            else:
                t_map = clock()
                try:
                    anizip.anizip_mappings(http, log, media_id)
                except StageError as exc:
                    log.warn("play.art", "mapping", result="FAIL", mediaId=media_id, error=str(exc),
                             suggest="the loading screen falls back to the fanart backdrop and shows no episode title")
                spent["mapping_ms"] = round((clock() - t_map) * 1000, 1)
        meta = playermod.episode_meta(media_id, log).get(episode) or {}
        images = playermod.show_images(media_id, log)
        out["episodeTitle"] = str(meta.get("title") or "")
        still_url = str(meta.get("image") or "")
        # The still: pre-blurred by an earlier rebuild, else fetched and converted now.
        blur = thumbsmod.still_blur_path(media_id, episode)
        if not blur.exists() and still_url and left() > 0:
            t_still = clock()
            paths = covers.fetch_many(http, log, [still_url], budget_s=left())
            src = paths.get(still_url)
            if src and left() > 0:
                # The budget has to reach the subprocess: gating *entry* to a step leaves the step
                # itself on `FFMPEG_TIMEOUT_S`, and two of those is 40 s with nothing written.
                thumbsmod.convert_forms(Path(src), media_id, episode, thumbsmod.THUMB_W, thumbsmod.THUMB_H,
                                        timeout_s=min(left(), thumbsmod.FFMPEG_TIMEOUT_S), log=log)
            spent["still_ms"] = round((clock() - t_still) * 1000, 1)
        if blur.exists():
            out["still"] = str(blur)
        # Show art: cached after the show's first play.
        if left() > 0:
            # `budget_s` bounds its fetch, `timeout_s` its two conversions -- both inside this
            # function's window, so both answer to this function's deadline.
            t_show = clock()
            art = thumbsmod.ensure_show_art(http, log, media_id, images.get("fanart", ""), images.get("logo", ""),
                                            budget_s=left(),
                                            timeout_s=min(left(), thumbsmod.FFMPEG_TIMEOUT_S))
            out["logo"] = art["logo"]
            spent["showArt_ms"] = round((clock() - t_show) * 1000, 1)
        fan_blur = thumbsmod.fanart_blur_path(media_id)
        source = blur if blur.exists() else (fan_blur if fan_blur.exists() else None)
        if source is not None and left() > 0:
            t_back = clock()
            out["backdrop"] = thumbsmod.make_backdrop(source, thumbsmod.BACKDROP_W, thumbsmod.BACKDROP_H,
                                                      timeout_s=min(left(), thumbsmod.FFMPEG_TIMEOUT_S), log=log)
            spent["backdrop_ms"] = round((clock() - t_back) * 1000, 1)
    except Exception as exc:                           # noqa: BLE001 - never on the play path
        log.warn("play.art", "step", result="FAIL", mediaId=media_id, episode=episode,
                 error=f"{type(exc).__name__}: {exc}", suggest="the loading screen shows text only")
    log.info("play.art", "done", result="PASS", ms=(clock() - t0) * 1000, mediaId=media_id,
             episode=episode, backdrop=bool(out["backdrop"]), logo=bool(out["logo"]), still=bool(out["still"]),
             **spent)
    return out


def do_status(log: Log) -> dict:
    # `status.do_status` is the bin's copy (the idle poll does not import this module); this one stays for `cli.main`
    # and its tests' `cli.MPV_SOCKET`.
    mpv = Mpv(log)
    try:
        running, foreign = MPV_SOCKET.exists() and mpv._try_connect(), None
    except mpvmod.ForeignPeer as exc:              # as `status.do_status`: no player of ours, the poll still answers
        running, foreign = False, str(exc)
    out = player_state(log, mpv, running)
    return {**out, "playerWarning": foreign} if foreign else out


def capture_player(log: Log, mpv: Mpv, running: bool) -> dict:
    """What the running player is doing, read before it is replaced.

    `path` is mpv's own answer for the file it has open — the stream URL, which `now-playing.json`
    deliberately does not store — so the resume can re-load exactly what was playing without a
    second resolve (no index call, no provider call, no new debrid link).
    """
    out: dict = {"running": running, "responsive": False, "pid": None, "path": None, "pos": 0.0,
                 "dur": 0.0, "pause": False, "subDelay": 0.0, "idle": True,
                 "visible": False, "stamp": None}
    if not running:
        log.info("player.restart", "capture", result="PASS", running=False, note="no player to replace")
        return out
    out["responsive"] = mpv.healthy(timeout=3.0)
    if out["responsive"]:
        props = (("pid", "pid"), ("path", "path"), ("pos", "time-pos"), ("dur", "duration"),
                 ("pause", "pause"), ("subDelay", "sub-delay"),
                 ("idle", "idle-active"), ("stamp", playermod.PLAYING_KEY))
        # Not media-title: a restart forces its own, and read mid-load it is the token-bearing URL.
        for key, prop in props:
            try:
                value = mpv.command("get_property", prop)
            except StageError:
                continue
            if value is not None:
                out[key] = value
    else:
        # A core that no longer answers still has a pid, and replacing it is the whole point.
        out["pid"] = mpv._owner_pid()
    for key in ("pos", "dur", "subDelay"):
        try:
            out[key] = float(out[key] or 0)
        except (TypeError, ValueError):
            out[key] = 0.0
    out["idle"], out["pause"] = bool(out["idle"]), bool(out["pause"])
    try:
        out["visible"] = playermod.window_visible(log, out["pid"])      # what the viewer saw
    except StageError:
        pass
    log.info("player.restart", "capture", result="PASS" if out["pid"] else "FAIL",
             pid=out["pid"], responsive=out["responsive"], idle=out["idle"], pos=round(out["pos"], 3),
             dur=round(out["dur"], 3), paused=out["pause"], subDelay=out["subDelay"],
             hasFile=bool(out["path"]), visible=out["visible"], stamp=out["stamp"],
             suggest="" if out["pid"] else "no pid for the running player; it cannot be replaced safely")
    return out


def do_player_restart(args, log: Log, settings: dict) -> dict:
    """Replace the player **process**, and put the user back where they were.

    `player stop` cannot do this: it sends mpv the `stop` command, and with `--idle=yes` the process
    survives with the same pid and the same in-memory Lua script. mpv reads `--script=` once, at
    process start, so a new process is the only way to pick up a changed script.

    Order matters: capture and persist the position **before** anything is killed, so an interrupted
    restart costs the user nothing worse than a stopped player at the right position.
    """
    t0 = time.monotonic()
    mpv = Mpv(log)
    running = MPV_SOCKET.exists() and mpv._try_connect()
    before = capture_player(log, mpv, running)
    np = playermod.read_now_playing()
    stale = playermod.staleness(before["pid"]) if before["pid"] else None
    # The resume store is the source of truth for "where was I", and the restart must not
    # be the one thing that can lose it: write the live position through it now and read it back.
    # A restart inside a switch's gap (the next file loaded, now-playing.json not yet rewritten) must not
    # store the new file's position under the previous episode; the old process's player stamp says which file.
    skip_store = not restart_names_now_playing(log, before, np)
    stored = None if skip_store else store_restart_position(log, before, np)
    old_pid = before["pid"]
    stop = None
    if old_pid:
        mpv._close()
        stop = playermod.terminate(log, old_pid, expect=MPV_SOCKET)
        MPV_SOCKET.unlink(missing_ok=True)      # the launcher does this too; a stale socket must not outlive the process
    elif running:
        raise StageError("player.restart", "a player is running but its pid could not be read",
                         "run `omarchy-anime player status`; kill the mpv process by hand if it is wedged")
    # The fresh process, through the same launcher `player start` uses.
    fresh = Mpv(log)
    t_start = time.monotonic()
    fresh.ensure_running()
    new_pid = int(fresh.command("get_property", "pid"))
    replaced = bool(old_pid) and new_pid != old_pid
    log.emit("info" if (new_pid and (replaced or not old_pid)) else "error", "player.restart", "start",
             result="PASS" if (new_pid and (replaced or not old_pid)) else "FAIL",
             ms=(time.monotonic() - t_start) * 1000, oldPid=old_pid, newPid=new_pid, replaced=replaced,
             suggest="" if (replaced or not old_pid) else "the new player has the same pid as the old one; "
                                                          "that cannot happen unless nothing was replaced")
    window = playermod.park(log, new_pid)
    # The whole point of the restart is the script, so read back that the new process has it.
    try:
        loaded = fresh.command("get_property", f"user-data/{EPISODES_SCRIPT_KEY}")
    except StageError:
        loaded = None
    log.emit("info" if loaded else "warn", "player.restart", "script", result="PASS" if loaded else "FAIL",
             key=EPISODES_SCRIPT_KEY, loaded=bool(loaded),
             suggest="" if loaded else "the new player did not load the episode panel; check that "
                                       "plugin/mpv/omarchy-anime-episodes.lua is readable")
    resumed = None
    if before["path"] and not before["idle"]:
        # A now-playing.json that names another file would title and panel the wrong episode.
        resumed = resume_into(log, fresh, before, None if skip_store else np)
    else:
        log.info("player.restart", "resume", result="PASS", loaded=False,
                 note="nothing was playing; the fresh player is parked and idle")
    if before["visible"]:
        playermod.show(log)
    out = {"restarted": True, "oldPid": old_pid, "newPid": new_pid, "replaced": replaced,
           "stale": stale, "stop": stop, "resumed": resumed, "stored": stored,
           "scriptLoaded": bool(loaded), "window": window, "visible": before["visible"],
           "ms": round((time.monotonic() - t0) * 1000, 1)}
    log.info("player.restart", "done", result="PASS", ms=(time.monotonic() - t0) * 1000,
             oldPid=old_pid, newPid=new_pid, resumed=bool(resumed),
             wasStale=bool(stale and stale.get("stale")))
    return out


def restart_names_now_playing(log: Log, before: dict, np: dict | None) -> bool:
    """False when the old process's player stamp names another file than now-playing.json. A restart inside a
    switch's gap (the next file loaded, now-playing not yet rewritten) must not store the new file's position
    under the previous episode, nor title the restart with it. No player stamp (or no now-playing) is no mismatch."""
    if not (np and np.get("mediaId") and np.get("episode")):
        return True
    parsed = playermod.parse_stamp(before.get("stamp"))
    same = playermod.stamp_matches({"state": "set" if parsed else "unset", "stamp": parsed},
                                   np["mediaId"], np["episode"], np.get("infohash"))
    if same["match"] is False:
        log.info("player.restart", "resume.store", result="SKIP", mediaId=np["mediaId"], episode=np["episode"],
                 why=same["reason"], stamp=before.get("stamp"),
                 suggest="the position belongs to the file the player has loaded, not to now-playing's episode")
        return False
    return True


def store_restart_position(log: Log, before: dict, np: dict | None) -> dict | None:
    """The live position written through the resume store before the old player is killed, and read back."""
    if not (np and before["pos"] > 0 and before["dur"] > 0 and np.get("mediaId") and np.get("episode")):
        return None
    playermod.store_resume(np["mediaId"], np["episode"], before["pos"], before["dur"], info_hash=np.get("infohash"))
    readback = playermod.read_resume(np["mediaId"], np["episode"]) or {}
    stored = {"mediaId": np["mediaId"], "episode": np["episode"], "pos": readback.get("pos"),
              "start": playermod.start_position(np["mediaId"], np["episode"])}
    ok = abs(float(readback.get("pos") or -1) - before["pos"]) < 0.001
    log.emit("info" if ok else "warn", "player.restart", "resume.store", result="PASS" if ok else "FAIL",
             mediaId=np["mediaId"], episode=np["episode"], wanted=round(before["pos"], 3),
             readback=readback.get("pos"), startPosition=stored["start"], watched=readback.get("watched"),
             suggest="" if ok else "the resume store did not take the live position; the restart "
                                   "will still re-load from the captured position")
    return stored


def restart_title(np: dict | None) -> str:
    """The title a restarted player forces: the play's own, then the show and episode, then a neutral one."""
    np = np or {}
    if np.get("mediaTitle"):
        return str(np["mediaTitle"])
    if np.get("title") and np.get("episode"):
        return playermod.episode_title(np["title"], np["episode"])
    return "Anime"


def resume_into(log: Log, mpv: Mpv, before: dict, np: dict | None) -> dict:
    """Re-load into a fresh player what the replaced one was playing, at the same position.

    The same `loadfile <url> replace -1 start=<seconds>` `do_play` uses, plus the two
    properties mpv does not carry across a process: the forced media title and `sub-delay`.
    """
    t0 = time.monotonic()
    start = float(before["pos"] or 0)
    # A fresh process has no forced title and no player stamp; both go on before `loadfile`
    # (`prepare_load`). The old player's media-title is not reused: read mid-load it is the URL.
    stamp = before.get("stamp") or (playermod.playing_stamp(np["mediaId"], np["episode"], np.get("infohash"))
                                    if np and np.get("mediaId") and np.get("episode") else None)
    title = restart_title(np)
    playermod.prepare_load(mpv, log, title, stamp)
    playermod.load_file(mpv, log, before["path"], title, start, stage="player.restart")
    ev = mpv.wait_event("file-loaded", timeout=60)
    ok = bool(ev and ev.get("event") == "file-loaded")
    out = {"loaded": ok, "start": round(start, 3), "path": before["path"], "pos": None, "drift": None,
           "paused": before["pause"], "subDelay": before["subDelay"]}
    if not ok:
        log.error("player.restart", "resume", result="FAIL", ms=(time.monotonic() - t0) * 1000,
                  start=out["start"], event=ev,
                  suggest="the fresh player could not re-open the stream; the position is stored, so "
                          "playing the episode again resumes it (the link may have expired)")
        return out
    if abs(before["subDelay"]) > 0.001:
        try:
            mpv.set_sub_delay(before["subDelay"])       # sub-delay does not survive a process
        except StageError:
            pass
    if before["pause"]:
        try:
            mpv.command("set_property", "pause", True)
        except StageError:
            pass
    try:
        out["pos"] = mpv.command("get_property", "time-pos")
        out["drift"] = round(abs(float(out["pos"] or 0) - start), 3)
    except StageError:
        pass
    # A seek lands on a keyframe, so a couple of seconds either way is the codec, not a fault.
    close = out["drift"] is not None and out["drift"] <= 5.0
    log.emit("info" if close else "warn", "player.restart", "resume", result="PASS" if close else "FAIL",
             ms=(time.monotonic() - t0) * 1000, start=out["start"], readback=out["pos"], drift=out["drift"],
             paused=out["paused"], subDelay=out["subDelay"],
             suggest="" if close else "the fresh player did not land near the captured position; the "
                                      "stored resume position is still correct")
    if np and np.get("media"):
        refresh_episode_panel(log, mpv, np.get("media"), int(np.get("listProgress") or 0), np.get("episode"))
    return out


def player_resume(args, log: Log, mpv: Mpv, running: bool) -> dict:
    """`player resume <mediaId> <episode>`: the "Play" of a notification for an episode that loaded paused (no one
    was watching when it was ready). When the player still has that episode, the window is shown and it plays, read
    back; when it has moved on (another episode, a stop, a player closed), `play <mediaId> <episode> --show` runs,
    detached, under this trace. Without the two numbers: whatever is loaded, shown and played."""
    media = int(args.delta) if getattr(args, "delta", None) not in (None, "") else None
    episode = int(args.episode_arg) if getattr(args, "episode_arg", None) not in (None, "") else None
    now = playermod.read_now_playing() or {}
    here = running and (media is None or (int(now.get("mediaId") or 0) == media
                                          and int(now.get("episode") or 0) == episode))
    log.info("player.resume", "decide", result="PASS", mediaId=media, episode=episode, running=running,
             now_playing=[now.get("mediaId"), now.get("episode")], loaded=here,
             reason="the player still has the episode: show it and play" if here else
                    "the player no longer has the episode: play it" if media is not None else
                    "no player is running and no episode was named")
    if here:
        pid = int(mpv.command("get_property", "pid"))
        window = playermod.set_window(log, "show", pid, True)
        paused = mpv.set_pause(False)
        ok = paused is False
        log.emit("info" if ok else "warn", "mpv.pause", "resume", result="PASS" if ok else "FAIL", paused=paused,
                 visible=window.get("visible"),
                 suggest="" if ok else "mpv did not read back unpaused; Space in the player plays it")
        return {"paused": paused, "window": window, "played": None}
    if media is None:
        raise StageError("player", "mpv is not running", "omarchy-anime player start")
    argv, env = private_args.command(["--trace", log.trace, "play"], [str(media), str(episode), "--show"])
    try:
        pid = _spawn_detached(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True, close_fds=True, env=env).pid
    except OSError as exc:
        log.warn("player.resume", "spawn", result="FAIL", error=str(exc))
        pid = None
    log.emit("info" if pid else "warn", "player.resume", "play", result="PASS" if pid else "FAIL", pid=pid,
             argv=[*argv[3:], *json.loads(env[private_args.ENV])],
             suggest="" if pid else "the play could not be started; play the episode from Browse")
    return {"paused": None, "window": None, "played": {"pid": pid, "mediaId": media, "episode": episode}}


def do_player(args, log: Log, settings: dict) -> dict:
    mpv = Mpv(log)
    if args.action == "stop":
        # A play waiting for TorBox's download would otherwise load the episode minutes after the viewer
        # stopped: the stop ends that play too. Any other play in flight is left alone.
        waiting = playstate.stop_waiting(log)
        if waiting is not None and not (MPV_SOCKET.exists() and mpv._try_connect()):
            return {"running": False, "visible": False, "play_stopped": waiting}
    if args.action == "restart":
        return do_player_restart(args, log, settings)
    if args.action == "start":
        # `start` on a player that is already running is a re-park and nothing else, so it
        # is also the place a user finds out that the running player predates its own script.
        was_running = MPV_SOCKET.exists() and mpv._try_connect() and mpv.healthy(timeout=3.0)
        old_pid = None
        if was_running:
            try:
                old_pid = int(mpv.command("get_property", "pid"))
            except StageError:
                old_pid = None
        mpv._close()
        mpv.ensure_running()
        pid = int(mpv.command("get_property", "pid"))
        out = {"running": True, "pid": pid, "window": playermod.park(log, pid)}
        out["stale"] = playermod.staleness(pid)
        if was_running and pid == old_pid and out["stale"]["stale"]:
            out["note"] = ("this player was already running and predates its own script — "
                           + out["stale"]["reason"])
            out["suggest"] = "omarchy-anime player restart"
            log.warn("player.start", "stale", result="FAIL", pid=pid, reason=out["stale"]["reason"],
                     file=(out["stale"]["newest"] or {}).get("path"),
                     suggest="mpv loads --script= once, at process start, so this `start` changed "
                             "nothing about it; `omarchy-anime player restart` replaces the process "
                             "and puts the episode back where it was")
        return out
    running = MPV_SOCKET.exists() and mpv._try_connect()
    if args.action == "status":
        return player_state(log, mpv, running)
    if args.action == "resume":
        return player_resume(args, log, mpv, running)
    if not running:
        if args.action in ("hide", "stop"):
            return {"running": False, "visible": False}
        raise StageError("player", "mpv is not running", "omarchy-anime player start")
    pid = int(mpv.command("get_property", "pid"))
    if args.action in ("show", "hide", "toggle"):
        # Decided from what the viewer sees, before anything moves; an unparked window (a manually
        # started mpv, or one moved to a workspace with SUPER+SHIFT+2) is parked on the way either way.
        want = {"show": True, "hide": False, "toggle": None}[args.action]
        return playermod.set_window(log, args.action, pid, want)
    if args.action == "pause":
        mpv.command("cycle", "pause")
        paused = mpv.command("get_property", "pause")        # read back
        log.info("mpv.pause", "toggle", result="PASS", paused=paused)
        return {"paused": paused}
    if args.action == "sub-delay":
        before = mpv.command("get_property", "sub-delay")
        before = float(before) if before is not None else 0.0
        if args.reset:
            wanted, how = 0.0, "reset"
        elif args.set_value is not None:
            wanted, how = float(args.set_value), "set"
        elif args.delta is not None:
            try:
                wanted, how = before + float(args.delta), "nudge"
            except ValueError:
                raise StageError("player.sub-delay", f"{args.delta!r} is not a number of seconds",
                                 "omarchy-anime player sub-delay +1.5")
        else:
            raise StageError("player.sub-delay", "no delta, --set or --reset given",
                             "omarchy-anime player sub-delay +1.5 | --set 38.74 | --reset")
        after = mpv.set_sub_delay(wanted)
        return {"before": round(before, 3), "wanted": round(wanted, 3), "after": round(after, 3),
                "delta": round(after - before, 3), "how": how}
    if args.action == "stop":
        mpv.command("stop")
        playermod.clear_now_playing()
        idle = mpv.command("get_property", "idle-active")
        log.info("mpv.stop", "done", result="PASS" if idle else "FAIL", idle=idle)
        # Hidden after, as the viewer sees it, so a window moved to a workspace is parked too.
        return {"stopped": True, "idle": idle, "play_stopped": waiting,
                **playermod.set_window(log, "stop", pid, False)}
    raise StageError("player", f"unknown action {args.action}")


EOF_READS = ("time-pos", "duration", "stream-pos", "file-size")


def player_stamp(log: Log, at_eof: bool = False) -> dict:
    """The running player's stamp (`playermod.PLAYING_KEY`), read over its own short-lived connection.

    On an eof tick the same connection also reads what the player says about its position (`EOF_READS`, under
    `"read"`): the service sends its own estimate, mpv's `time-pos` polled every `pollIntervalSec` and stepped
    in between, which a seek to the end does not refresh before the eof edge fires (32 ms later, measured on mpv
    0.41). A property that does not answer is None, and the caller keeps the tick's number for it."""
    t0 = time.monotonic()
    client = Mpv(log, MPV_SOCKET)
    read, errors = {}, {}
    if not (MPV_SOCKET.exists() and client._try_connect()):
        out = {"state": "unreadable", "stamp": None, "error": "no player socket"}
    else:
        try:
            out = playermod.read_stamp(client, log)
            if at_eof and out["state"] != "unreadable":         # an unreadable core answers nothing else either
                # One deadline for all the reads: a core that stops answering after the
                # stamp costs `STAMP_TIMEOUT_S` once, not once per property. Each read waits only for the time left,
                # so the first timeout spends the deadline and ends the reads.
                deadline = time.monotonic() + playermod.STAMP_TIMEOUT_S
                for prop in EOF_READS:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        read[prop], errors[prop] = None, "skipped: the reads ran out of time"
                        continue
                    read[prop], cause = eof_read(client, prop, left)
                    if cause:
                        errors[prop] = cause                    # why the tick's own number was kept
        finally:
            client._close()
    log.debug("player.stamp", "read", result="PASS" if out["state"] == "set" else "SKIP",
              ms=(time.monotonic() - t0) * 1000, **out,
              **({"read": read, "read_errors": errors} if at_eof else {}))
    return {**out, "read": read, "read_errors": errors}


def eof_read(client: Mpv, prop: str, timeout: float) -> tuple[float | None, str | None]:
    """One eof read: (the number, None), or (None, why): `timeout`, mpv's own error (`property unavailable`),
    `null`, or `not a number`."""
    try:
        value = client.command("get_property", prop, timeout=timeout)
    except StageError as exc:
        return None, "timeout" if "no reply" in str(exc) else str(exc).rsplit(": ", 1)[-1]
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if value is None:
        return None, "null"
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None, "not a number"
    return float(value), None


def is_completed(media: dict | None, episode: int) -> bool:
    """Send COMPLETED only when this is the last episode **and** the show has finished
    airing. The last episode that exists today is not the last episode there will be: marking a
    RELEASING show COMPLETED takes it off the user's Watching list until the next one airs."""
    total = (media or {}).get("episodes")
    return bool(total and episode >= int(total) and (media or {}).get("status") == "FINISHED")


def do_tick(args, log: Log, settings: dict) -> dict:
    """Called by the service every few seconds with mpv's position. Stores resume, marks watched once."""
    prev = playermod.read_resume(args.anilist_id, args.episode)
    np_now = playermod.read_now_playing() or {}
    names_this = (int(np_now.get("mediaId") or 0) == int(args.anilist_id)
                  and int(np_now.get("episode") or 0) == int(args.episode))
    playing = np_now.get("infohash") if names_this else None
    # Which transport produced this tick. "socket" is the mpv IPC socket the service holds;
    # "status-poll" is the fallback that runs off `omarchy-anime status` when that socket is down.
    # Without it a log with ticks in it cannot answer "was the socket alive at the time?".
    source = str(getattr(args, "source", "") or "socket")
    # The service names the episode from now-playing.json, which a play rewrites seconds after
    # its `loadfile`; the player stamp says what the position belongs to. A mismatch is a switch
    # in progress and the tick is dropped. No player stamp (a player from before it, or no answer) keeps the
    # old behaviour.
    stamp = player_stamp(log, at_eof=bool(args.eof))
    check = playermod.stamp_matches(stamp, args.anilist_id, args.episode, info_hash=playing)
    if check["match"] is False:
        log.info("tick", "decide", result="SKIP", pos=args.pos, dur=args.dur, eof=args.eof, source=source,
                 reason="other_file", why=check["reason"], stamp=stamp.get("stamp"),
                 suggest="nothing written: the position belongs to the file the player has loaded now")
        return {"mediaId": args.anilist_id, "episode": args.episode, "watched": False,
                "already": bool(prev and prev.get("watched")), "ratio": 0.0, "reason": "other_file",
                "position": (prev or {}).get("pos"), "source": source, "dropped": True, "cut": False}
    if check["match"] and (stamp.get("stamp") or {}).get("infohash"):
        playing = stamp["stamp"]["infohash"]
    # An eof decides watched or cut by the position, so it is judged by what the player says when it
    # answers (`player_stamp`), not by the service's estimate; a property that does not answer keeps the tick's.
    read = stamp.get("read") or {}
    pos = read["time-pos"] if read.get("time-pos") is not None else args.pos
    sent_dur = read["duration"] if (read.get("duration") or 0) > 0 else args.dur
    entry = playermod.store_resume(args.anilist_id, args.episode, pos, sent_dur, info_hash=playing)
    # A rewatch (the list entry is REPEATING) of an episode past the list's progress is not
    # "already" watched because resume.json flagged it in an earlier watch. `listProgress` moves on the
    # watched edge below, so the rule still fires once per episode.
    gate = prev
    rewatch = (names_this and np_now.get("listStatus") == "REPEATING"
               and int(args.episode) > int(np_now.get("listProgress") or 0))
    if rewatch and prev and prev.get("watched"):
        gate = {**prev, "watched": False}
    # An eof tick in the unload gap reports duration 0; the length already stored is the one to judge by.
    dur = sent_dur if float(sent_dur or 0) > 0 or not args.eof else entry["dur"]
    decision = playermod.watched_decision(gate, pos, dur, settings["watchedThreshold"], args.eof,
                                          stream_pos=read.get("stream-pos"), file_size=read.get("file-size"))
    # Before the returns below, which nearly every tick takes: the next episode's download starts two minutes in. It
    # never costs the tick its own work (the position, the watched rule, AniList, `next`): a failure is one line.
    try:
        prefetched = prefetch_next(log, args, np_now if names_this else {}, pos, settings)
    except Exception as exc:                             # noqa: BLE001 - logged; the tick goes on without it
        prefetched = {"episode": None, "reason": f"failed: {type(exc).__name__}: {exc}"}
        log.warn("prefetch", "decide", result="FAIL", error=prefetched["reason"],
                 suggest="the next episode is not downloaded ahead; check the cache folder (prefetch.json)")
    log.info("tick", "decide", result="PASS", pos=pos, pos_sent=args.pos, pos_read=read.get("time-pos"),
             dur=args.dur, dur_read=read.get("duration"), judged_dur=dur, eof=args.eof,
             stream_pos=read.get("stream-pos"), file_size=read.get("file-size"),
             read_errors=stamp.get("read_errors") or None,
             source=source, threshold=settings["watchedThreshold"], stamp=check["reason"],
             rewatch=bool(rewatch), prefetch=prefetched["reason"], **decision)
    out = {"mediaId": args.anilist_id, "episode": args.episode, "watched": decision["watched"],
           "already": bool(gate and gate.get("watched")), "ratio": round(decision["ratio"], 4),
           "reason": decision["reason"], "position": entry["pos"], "source": source, "cut": decision["cut"]}
    if prefetched.get("trace"):
        out["prefetch"] = prefetched
    if decision["cut"]:
        # The stream ended short of the end. The position above is the resume point; no
        # AniList write, no watched flag, and no `next`, so the service does not auto-advance.
        out["why"] = decision["why"]
        first = playermod.note_cut(args.anilist_id, args.episode, pos)
        log.emit("warn" if first else "info", "tick", "eof_cut", result="SKIP", pos=pos, dur=dur, why=out["why"],
                 repeat=not first,
                 suggest="the stream stopped early (network or the provider's link); playing the episode "
                         "again resumes here" if first else "the player is still at the cut reported above")
    if not decision["watched"]:
        # The tick the service auto-advances on is the one that reports EOF, and by then
        # this episode is `already` watched — the 85% threshold always fires first, tens of seconds
        # earlier, and `watched` is a one-shot. Without `next` here the service has nothing to
        # advance to and the chain ends. It is already on disk from the threshold tick, so this
        # costs one read and writes nothing.
        if args.eof and out["already"] and not decision["cut"]:
            np = playermod.read_now_playing() or {}
            if np.get("mediaId") == args.anilist_id and np.get("next"):
                # Not `np["next"]` as stored. That was seeded from the list progress at play
                # time and is only corrected by the watched edge, which an already-watched episode
                # never fires — so it can name the episode that just ended. Recompute from this one.
                seeded = np["next"]
                out["next"] = playermod.advance_next(np, args.episode)["next"]
                log.info("tick", "next", result="PASS", episode=args.episode, next=out["next"], seeded=seeded,
                         reason="eof on an already-watched episode: next recomputed from it, not the play-time seed")
        return out
    # Flag first: a slow or failed AniList write must not re-fire on the next tick. save_progress queues retries.
    playermod.mark_watched(args.anilist_id, args.episode)
    np = playermod.read_now_playing() or {}
    media = np.get("media") if np.get("mediaId") == args.anilist_id else None
    http = Http(log, settings["httpTimeout"])
    client = AniList(http, log, AniList.load_token())
    if media is None:
        try:
            media = client.media(args.anilist_id)
        except StageError as exc:
            log.warn("anilist.media", "skip", result="FAIL", error=str(exc))
    complete = is_completed(media, args.episode)
    # The decision before the write (save_progress keeps a REPEATING entry REPEATING, from its own list
    # read), then the outcome after it whatever happens: what went out, the error, and whether it was queued.
    intended = "COMPLETED" if complete else "REPEATING" if np_now.get("listStatus") == "REPEATING" else "CURRENT"
    log.info("progress", "decide", result="PASS", mediaId=args.anilist_id, episode=args.episode,
             episodes=(media or {}).get("episodes"), media_status=(media or {}).get("status"),
             complete=complete, list_status=np_now.get("listStatus"), intended=intended)
    error = None
    try:
        out["progress"] = client.save_progress(args.anilist_id, args.episode, complete)
    except StageError as exc:
        error = str(exc)
        out["progress"] = {"saved": None, "verified": False, "error": error, "queued": True}
    except Exception as exc:                            # noqa: BLE001 - logged here, re-raised to `main`
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        res = out.get("progress") or {}
        went = getattr(client, "last_write", None) or {}
        log.emit("info" if res.get("verified") else "warn", "progress", "outcome",
                 result="PASS" if res.get("saved") and res.get("verified") else "SKIP" if res.get("skipped")
                 else "FAIL", mediaId=args.anilist_id, episode=args.episode, sent=went.get("status"),
                 answered=went.get("answered"), verified=res.get("verified"), skipped=res.get("skipped"),
                 error=error, queued=bool(getattr(client, "last_queued", False)))
    if np.get("mediaId") == args.anilist_id:
        np = playermod.advance_next(np, args.episode)
        np["watchedAt"] = time.time()
        np["listProgress"] = max(int(np.get("listProgress") or 0), args.episode)
        playermod.write_now_playing(np)
        out["next"] = np["next"]
        log.info("nowplaying", "advance", result="PASS", next=np["next"])
    # The ticked episode's cell flips `next` -> `watched` and the panel repaints without a
    # keypress. The CLI knows exactly when the data changed and already holds a socket, so there is
    # no observe_property, no mtime poll and no timer.
    if media:
        mpv_client = Mpv(log)
        if MPV_SOCKET.exists() and mpv_client._try_connect():
            refresh_episode_panel(log, mpv_client, media, max(int(np.get("listProgress") or 0), args.episode),
                                  (np.get("next") or {}).get("episode"), http=http)
    return out


def prefetch_next(log: Log, args, now_playing: dict, pos: float, settings: dict) -> dict:
    """The tick's part of the next-episode prefetch: `{episode, reason}`, plus `trace` and `started` on the tick
    that starts it. `prefetch.wanted` decides from this tick alone (no request); the episode's latch makes it at most
    one start per episode whatever the ticks, restarts and Services; the command runs detached, so the tick never
    waits on it. `now_playing` is empty when it names another episode than this tick's."""
    if not now_playing:
        return {"episode": None, "reason": "now-playing names another episode"}
    nxt, reason = prefetchmod.wanted(now_playing, args.episode, pos, settings)
    if nxt is None:
        return {"episode": None, "reason": reason}
    # The rest of the episode's ticks find it latched: one read without the lock answers them. The lock decides only
    # when there is no entry yet (`latch`).
    seen = prefetchmod.entry(args.anilist_id, nxt)
    if seen is not None:
        return {"episode": nxt, "reason": f"episode {nxt} was already prefetched ({seen.get('state')})"}
    trace = prefetchmod.new_trace()
    taken = prefetchmod.latch(args.anilist_id, nxt, {"from": args.episode, "trace": trace, "playTrace": log.trace},
                              log)
    if taken is not None:
        return {"episode": nxt, "reason": f"episode {nxt} was already prefetched ({taken.get('state')})"}
    started = prefetchmod.start(log, args.anilist_id, nxt, args.episode, trace)
    return {"episode": nxt, "reason": reason, "trace": trace, "started": started is not None}


def do_prefetch(args, log: Log, settings: dict) -> dict:
    """`omarchy-anime prefetch <mediaId> <episode>`: download this episode at TorBox ahead of its play, as the
    play would pick it, and stop. The tick starts it detached two minutes into the episode before; a run by hand works
    the same. Every gate is one `prefetch gate` line; the play's own resolve runs with `prefetch.Client`, which stops
    at the first TorBox step (`prefetch.Stop`), and `prefetch done` says what came of it. Nothing reaches the viewer:
    every failure is logged, recorded in `prefetch.json`, and answered in the JSON with exit 0."""
    media_id, episode = int(args.anilist_id), int(args.episode)
    started = time.time()                          # what wrote the episode's list after this is newer than its search

    def done(outcome: str, reason: str, release: dict | None = None, hint: str = "", saved: dict | None = None,
             **facts) -> dict:
        result = "PASS" if outcome in (*prefetchmod.FOUND, prefetchmod.CACHED) else (
            "FAIL" if outcome == prefetchmod.FAILED else "SKIP")
        release = release or {}
        log.emit("warn" if result == "FAIL" else "info", "prefetch", "done", result=result, outcome=outcome,
                 mediaId=media_id, episode=episode, from_episode=getattr(args, "from_episode", None),
                 info_hash=release.get("infoHash") or None, release=release.get("title"), reason=reason, **facts,
                 suggest=(hint or "the play of this episode adds it as before") if result == "FAIL" else "")
        try:
            prefetchmod.update(media_id, episode, log, state=outcome, reason=reason, trace=log.trace,
                               infoHash=release.get("infoHash") or None, release=release.get("title"),
                               torrentId=facts.get("torrentId"), verified=facts.get("verified"), **(saved or {}))
        except Exception as exc:                         # noqa: BLE001 - the answer and the line above still stand
            log.warn("prefetch", "record", result="FAIL", error=f"{type(exc).__name__}: {exc}",
                     path=str(prefetchmod.path()),
                     suggest="what the prefetch did is not on disk; the later play still finds the download on TorBox")
        return {"mediaId": media_id, "episode": episode, "prefetch": outcome,
                "added": outcome in (prefetchmod.ADDED, prefetchmod.QUEUED),
                "reason": reason, "release": release.get("title"), "infoHash": release.get("infoHash") or None,
                "torrentId": facts.get("torrentId"), "trace": log.trace}

    def gate(name: str, why: str | None) -> bool:
        log.info("prefetch", "gate", result="SKIP" if why else "PASS", gate=name, mediaId=media_id, episode=episode,
                 reason=why or "passed")
        return bool(why)

    def aired() -> str | None:
        """Why the episode cannot be downloaded yet, from the AniList snapshot the play of the episode before wrote
        (no request), else from AniList; None when it has aired and is within the show's count."""
        now_playing = playermod.read_now_playing() or {}
        media = now_playing.get("media") if int(now_playing.get("mediaId") or 0) == media_id else None
        if not media:
            try:
                media = AniList(Http(log, settings["httpTimeout"]), log, AniList.load_token()).media(media_id)
            except StageError as exc:
                log.warn("anilist.media", "skip", result="FAIL", error=str(exc),
                         suggest="the prefetch skips (whether the episode aired is unknown); its play decides again")
        if not media:
            return "AniList could not be read: whether the episode aired is unknown"
        nxt = next_episode(media, episode - 1)
        if nxt["episode"] != episode:
            return f"episode {episode} is past the show's count ({media.get('episodes')})"
        return None if nxt["aired"] else f"episode {episode} has not aired"

    gates = (
        ("setting", lambda: None if as_bool(settings.get("prefetchNext"), True) else "prefetchNext is off"),
        ("torbox", lambda: prefetchmod.torbox_leads(settings)),
        ("blocked", lambda: "TorBox refused the account recently (the block guard remembers it)"
         if BlockGuard(log, settings).remembered("torbox") is not None else None),
        ("aired", aired),
        ("pin", lambda: "a pinned pack plays this show: it is cached or on the account already"
         if (pins.get(media_id) or {}).get("pack") else None),
        ("play", lambda: prefetchmod.play_starting(media_id, episode)))
    try:
        for name, ask in gates:
            why = ask()
            if gate(name, why):
                return done(prefetchmod.SKIPPED, why)
        try:
            token = read_token("torbox")
        except StageError as exc:
            # The file exists (the `token` gate), but `read_token` refuses it: a group- or world-readable file.
            return done(prefetchmod.FAILED, f"the TorBox token cannot be read: {exc}", hint=exc.suggest,
                        stage_failed=exc.stage)
        # Its search is the episode's stored search, as a play's is: the picker's list, and the reused play's donors.
        run = types.SimpleNamespace(anilist_id=media_id, episode=episode, release=None, track_source=None,
                                    keep_search=True, prefetch=True, prefetch_started=started,
                                    torbox_client=prefetchmod.Client(
                                        Http(log, settings["httpTimeout"]), log, token, media_id, episode))
        run.donor_pool = []                        # the side-load's pool, whose shape the play's reuse rebuilds
        try:
            # TorBox alone: the prefetch is for TorBox's download, and Real-Debrid's turn would add a magnet there.
            do_resolve(run, log, {**settings, "providers": ["torbox"]}, dry_run=False, ranked_sink=run.donor_pool)
        except prefetchmod.Stop as stop:
            return done(stop.outcome, stop.reason, stop.release, saved=reuse_record(log, run, settings, stop),
                        **stop.facts)
        except StageError as exc:
            # The search found nothing that would play (not aired yet at the indexers, every release filtered), or
            # TorBox was not asked: the play of the episode decides again.
            return done(prefetchmod.SKIPPED, f"the play's resolve stopped before TorBox: {exc}", stage_failed=exc.stage)
        return done(prefetchmod.FAILED, "the resolve returned a stream, which the prefetch never asks for")
    except Exception as exc:                             # noqa: BLE001 - a background run: recorded, never shown
        # The traceback goes on the line, as `fail_internal` logs it: the command's stderr is /dev/null.
        return done(prefetchmod.FAILED, f"unexpected: {type(exc).__name__}: {exc}",
                    hint=f"bug: report with `omarchy-anime trace {log.trace}`; the play of this episode adds it as "
                         f"before", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()[-1500:])


def reuse_record(log: Log, run, settings: dict, stop: "prefetchmod.Stop") -> dict | None:
    """What the play of the episode needs to play the prefetch's release without a search (`play_prefetched`): the
    release as the index served it (JSON-plain, so it reads back equal), the search's time, what the ranking read of
    it, and the `fingerprint` of the settings the play has (`settings` before the prefetch's TorBox-only providers).
    None for an outcome that named no release, or a release the search's record does not hold. Never raises: the
    prefetch's outcome (an add TorBox took) is recorded whatever happens here, with no release to reuse."""
    try:
        raw = ((getattr(run, "prefetch_search", None) or {}).get("raw") or {}).get(stop.release["infoHash"])
        if stop.outcome not in prefetchmod.REUSABLE or not raw:
            log.info("prefetch", "saved", result="SKIP", outcome=stop.outcome,
                     info_hash=stop.release["infoHash"] or None,
                     reason="no release to reuse" if stop.outcome not in prefetchmod.REUSABLE else
                            "the search's record does not hold the release; the play searches as before")
            return None
        record = json.loads(json.dumps({k: v for k, v in raw.items() if k != "parsed"}, default=str))
        fingerprint = prefetchmod.fingerprint(settings, fallback_language(settings), pins.get(run.anilist_id),
                                              getattr(run, "registry", None))
        # What the reuse needs when the release lacks the viewer's language (`prefetch.sideload_reuse`): the other
        # cached releases TorBox's loop would have tried, by what the ranking read of their language; and the shape of
        # the side-load's pool (`reused_donor_pool`). Neither when the search did not get that far.
        found = getattr(run, "prefetch_cached", None)
        others = None if found is None else [c for c in found if c["infoHash"] != stop.release["infoHash"]]
        extra = {}
        if others is not None:
            extra["cachedOthers"] = {"cached": len(others),
                                     "withLang": sum(1 for c in others if c["hasLang"] is True),
                                     "unknown": sum(1 for c in others if c["hasLang"] is None)}
        pool = getattr(run, "donor_pool", None) or []
        if pool:
            extra["donorPool"] = [[str(c.get("info_hash") or "").lower(), bool(c.get("tracks"))] for c in pool]
    except Exception as exc:                             # noqa: BLE001 - the outcome stands; the play searches
        log.warn("prefetch", "saved", result="FAIL", outcome=stop.outcome, info_hash=stop.release.get("infoHash"),
                 error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()[-1500:],
                 suggest=f"the prefetch's outcome is kept and the play searches as before; bug: report with "
                         f"`omarchy-anime trace {log.trace}`")
        return None
    log.info("prefetch", "saved", result="PASS", outcome=stop.outcome, info_hash=stop.release["infoHash"],
             search_ms=run.prefetch_search.get("ms"), fingerprint=fingerprint, **stop.pick,
             cached_others=extra.get("cachedOthers"), donor_pool=len(extra.get("donorPool") or []),
             reason="the play of this episode can play it without a search")
    return {"record": record, "fingerprint": fingerprint, "searchMs": run.prefetch_search.get("ms"), **stop.pick,
            **extra}


# ----------------------------------------------------------------------------- cli

CLI_DESCRIPTION = """\
The command behind the Anime plugin for Omarchy: it finds a release through your indexer add-ons, streams it
through TorBox or Real-Debrid into mpv, and keeps your AniList list in step. A command prints its answer as JSON
and logs to stderr and to ~/.cache/omaanime/log/; `omarchy-anime trace last` shows the newest thing you started."""


def stdin_only(value: str) -> str:
    """`--token` takes nothing but `-`. This is a breaking change to a one-user command and it is the
    point: there is no way left to put a token in argv. Rejected by argparse, which exits
    2 **before** the `cli/start` line is written, so the refused value is never logged either."""
    if value != "-":
        raise argparse.ArgumentTypeError(
            "--token no longer takes a value: it would land in your shell history and in "
            "/proc/<pid>/cmdline. Use\n"
            "    printf '%s\\n' \"$TOKEN\" | omarchy-anime auth <provider> --token -\n"
            "or run `omarchy-anime auth <provider>` with no --token for an un-echoed prompt.")
    return value


def main(argv: list[str] | None = None, from_env: int = 0) -> int:
    """The CLI. `from_env`: how many of `argv`'s words came in `OMAANIME_ARGS` (`private_args.take`), for the
    `cli start` line."""
    ap = argparse.ArgumentParser(prog="omarchy-anime", description=CLI_DESCRIPTION,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", help="trace id for log correlation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("resolve", help="find the best release and a stream URL for it from TorBox or Real-Debrid, "
                                       "in the order of the `providers` setting")
    p.add_argument("anilist_id", type=int)
    p.add_argument("episode", type=int)
    p.add_argument("--dry-run", action="store_true",
                   help="stop at the ranking: nothing is added to TorBox or Real-Debrid (TorBox is still asked "
                        "which releases it already has)")
    p.add_argument("--release", metavar="INFOHASH", help="play exactly this release from the episode's stored search")
    p = sub.add_parser("play", help="resolve, then load the stream into mpv")
    p.add_argument("anilist_id", type=int)
    p.add_argument("episode", type=int)
    p.add_argument("--start", type=float, default=0.0, help="resume position in seconds (default: the stored one)")
    p.add_argument("--no-resume", action="store_true", help="ignore the stored position")
    p.add_argument("--show", action="store_true", help="show the player window once the loading screen is up, before the stream is resolved")
    p.add_argument("--release", metavar="INFOHASH", help="play exactly this release from the episode's stored search (the release picker)")
    p.add_argument("--wait", action="store_true",
                   help="with --release: wait for TorBox to download it, on the download already on the account "
                        "(a held play's \"Keep waiting\")")
    p.add_argument("--no-pin", action="store_true",
                   help="with --release: play it without remembering it as the show's pin (a waiting play's "
                        "\"Watch the dub now\", a stopgap)")
    p.add_argument("--dub", metavar="INFOHASH",
                   help="with --wait: the cached dub the choice offers meanwhile, from the stored search")
    p.add_argument("--no-notify", action="store_true",
                   help="the caller shows the answer itself (Browse, the popout): a play refused before it starts "
                        "(an entry that is not out yet) is not also told by a notification")
    p = sub.add_parser("prefetch", help="download this episode at TorBox ahead of its play, as the play would pick it "
                                        "(one add when TorBox has not cached it, no stream); the tick starts it two "
                                        "minutes into the episode before (prefetchNext)")
    p.add_argument("anilist_id", type=int)
    p.add_argument("episode", type=int)
    p.add_argument("--from", dest="from_episode", type=int, default=None, metavar="EPISODE",
                   help="the episode playing when the tick started it (for the log)")
    sub.add_parser("watch", help="follow the downloads the viewer asked to be told about (\"Tell me when it's ready\") "
                                 "and notify when each is ready; one at a time, it exits when nothing is pending")
    p = sub.add_parser("notify-click", help=argparse.SUPPRESS)       # a notification's click (`notify.clicked`)
    p.add_argument("token")
    p = sub.add_parser("later", help="\"Tell me when it's ready\": end the play of this episode that waits for (or "
                                     "held) TorBox's download, and record the download as pending")
    p.add_argument("anilist_id", type=int)
    p.add_argument("episode", type=int)
    p.add_argument("--no-hide", action="store_true",
                   help="leave the player window up (the player says the answer, then hides it itself)")
    p = sub.add_parser("keep-waiting", help="\"Keep waiting\" during the wait: tell the play of this episode that "
                                            "waits for TorBox's download that the viewer wants it; nothing is added "
                                            "or restarted")
    p.add_argument("anilist_id", type=int)
    p.add_argument("episode", type=int)
    p = sub.add_parser("releases", help="the release picker's list for one episode: passed and rejected releases")
    p.add_argument("anilist_id", type=int)
    p.add_argument("episode", type=int)
    p.add_argument("--refresh", action="store_true", help="search again even if the stored search is fresh")
    p.add_argument("--query", metavar="WORDS", help="search with these words instead, through the indexer add-ons that offer text search")
    p = sub.add_parser("picker-notice", help="a play refused while the player is up (auto-next, or the player's "
                                             "own episode list): tell the player and lend p to the release picker")
    p.add_argument("anilist_id", type=int)
    p.add_argument("episode", type=int)
    p = sub.add_parser("pin", help="the remembered pick for one show: show or forget it")
    p.add_argument("action", choices=["show", "forget"])
    p.add_argument("anilist_id", type=int)
    p = sub.add_parser("episodes", help="the in-player episode list's payload for one show")
    p.add_argument("anilist_id", type=int, nargs="?", default=None,
                   help="default: whatever is playing")
    p.add_argument("--write", action="store_true",
                   help="also write ~/.cache/omaanime/episodes/<mediaId>.json")
    p.add_argument("--fetch", action="store_true",
                   help="fetch the mapping if missing, then the pictures around the cursor (network)")
    sub.add_parser("status", help="player state: mpv, Hyprland window, now-playing, resume (same as `player status`)")
    p = sub.add_parser("player",
                       help="window and transport control: "
                            "player <start|restart|show|hide|toggle|pause|resume|stop|status|sub-delay>. "
                            "`resume <mediaId> <episode>` shows and plays that paused episode (or plays it anew). "
                            "`stop` stops playback and leaves the process running; `restart` replaces "
                            "the process and re-loads what was playing")
    p.add_argument("action", choices=["start", "restart", "show", "hide", "toggle", "pause", "resume", "stop",
                                      "status", "sub-delay"])
    p.add_argument("delta", nargs="?", default=None, metavar="SECONDS",
                   help="sub-delay only: seconds to add to the current delay, e.g. +1.5 or -0.5; resume: the AniList "
                        "id of the episode a notification's Play is for")
    p.add_argument("episode_arg", nargs="?", default=None, metavar="EPISODE",
                   help="resume only: the episode; the player plays it if it has it, else it is played anew")
    p.add_argument("--set", dest="set_value", type=float, default=None, metavar="SECONDS",
                   help="sub-delay only: the absolute delay to apply")
    p.add_argument("--reset", action="store_true", help="sub-delay only: back to 0.0")
    p = sub.add_parser("tick", help="store the playback position and apply the watched rule (called by the service)")
    p.add_argument("anilist_id", type=int)
    p.add_argument("episode", type=int)
    p.add_argument("pos", type=float, help="time-pos seconds")
    p.add_argument("dur", type=float, help="duration seconds (0 if unknown)")
    p.add_argument("--eof", action="store_true", help="mpv reported eof-reached")
    p.add_argument("--source", default="socket", choices=["socket", "status-poll"],
                   help="which transport produced this tick; recorded in the log line")
    sub.add_parser("rd-user", help="Real-Debrid account check")
    sub.add_parser("torbox-user", help="TorBox account check")
    p = sub.add_parser("anizip", help="raw ani.zip mapping")
    p.add_argument("anilist_id", type=int)
    # AniList
    p = sub.add_parser("auth", help="store a provider token: auth <provider> [--token -], or auth torbox --device")
    p.add_argument("provider", choices=list(PROVIDERS))
    how = p.add_mutually_exclusive_group()
    how.add_argument("--device", action="store_true",
                     help="torbox only: sign in with a 6-digit code at a short URL instead of pasting a token")
    how.add_argument("--token", type=stdin_only, metavar="-",
                   help="read one line from stdin. This is the only accepted form: a token given on "
                        "the command line lands in your shell history and in /proc/<pid>/cmdline. "
                        "Omit it for an un-echoed prompt.")
    p = sub.add_parser("signout", help="remove a provider token and everything derived from it")
    p.add_argument("provider", choices=list(PROVIDERS))
    p = sub.add_parser("torznab", help="Torznab servers (Accounts): torznab list|add|check|remove|find-local; never "
                                       "prints a key")
    p.add_argument("action", choices=["list", "add", "check", "remove", "find-local"],
                   help="find-local: is a Prowlarr (127.0.0.1:9696) or Jackett (127.0.0.1:9117) running on this "
                        "computer? Asked without a key; nothing is saved")
    p.add_argument("server", nargs="?", default=None, metavar="ADDRESS",
                   help="the server's address as `torznab list` shows it (check: all when omitted; add: prompted "
                        "when omitted)")
    p.add_argument("--stdin", action="store_true",
                   help="add: read the address (unless given) and then the API key from stdin, one line each; "
                        "without it the key is asked for at an un-echoed prompt. A key is never an argument.")
    p.add_argument("--name", default="", help="add: a name of your own for the server")
    p.add_argument("--id", default="", metavar="ID",
                   help="check and remove: the server's id as `torznab list` shows it (what the Accounts sheet "
                        "sends; an address is never on its command line)")
    p.add_argument("--budget", type=float, default=torznab_accounts.CHECK_BUDGET_S, metavar="SECONDS",
                   help="add and check: how long the check may take (default %(default)s)")
    p = sub.add_parser("addons", help="indexer add-ons: addons spec|check|prompt; spec and prompt print plain "
                                      "text, not JSON")
    addons_sub = p.add_subparsers(dest="addons_cmd", required=True)
    q = addons_sub.add_parser("spec", help="the add-on interface as plain text, for you or your AI agent to read "
                                           "whole (stdout is text here, not JSON)")
    q.add_argument("--json", action="store_true", help="the interface as JSON instead")
    q = addons_sub.add_parser("check", help="load one add-on file as the plugin would, wherever it is, and ask its "
                                            "episode_search once; a JSON answer (ok, problems, releases), a summary "
                                            "on stderr, exit 0 only when it would load and answer well")
    q.add_argument("file", help="the add-on's .py file")
    q.add_argument("--no-call", action="store_true", help="only load it; do not call episode_search")
    q.add_argument("--live", action="store_true",
                   help="search the real indexer for --title and --episode; without it every request through "
                        "ctx.http is answered with ctx.HttpError, and a connection the add-on's own Python opens "
                        "through the usual socket calls is refused (a check, not a sandbox: other ways out, and a "
                        "program it starts, are not watched)")
    q.add_argument("--title", default=None, help="--live: the show to search for")
    q.add_argument("--episode", type=int, default=1, help="--live: the episode (default %(default)s)")
    q.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                   help="how long the load and the call may each take (default: the indexerTimeoutSeconds "
                        "setting)")
    q = addons_sub.add_parser("prompt", help="a ready prompt for your AI agent: build (write an add-on with you) or "
                                             "prowlarr (set up Prowlarr or Jackett); plain text, not JSON")
    q.add_argument("kind", nargs="?", default=None, choices=list(addon_help.KINDS))
    q.add_argument("--json", action="store_true", help="as JSON; without a kind, every prompt")
    p = sub.add_parser("accounts", help="AniList, TorBox, Real-Debrid, Subdl and OpenSubtitles, your Torznab "
                                        "servers, and the indexer add-ons with why any did not load; never prints a "
                                        "token")
    p.add_argument("--refresh", action="store_true", help="ignore the 10-minute cache")
    p = sub.add_parser("watching", help="Continue Watching (AniList CURRENT list) with next-episode info")
    p.add_argument("--user", help="public AniList username (default: the authenticated user)")
    p.add_argument("--refresh", action="store_true", help="ignore the 5-minute cache")
    p = sub.add_parser("list", help="any of the six AniList lists: list --status PLANNING|PAUSED|COMPLETED|DROPPED|REPEATING|CURRENT")
    p.add_argument("--status", default="CURRENT", help="MediaListStatus (default CURRENT, i.e. the same thing `watching` prints)")
    p.add_argument("--user", help="public AniList username (default: the authenticated user)")
    p.add_argument("--refresh", action="store_true", help="ignore the 5-minute cache")
    p = sub.add_parser("top", help="AniList top 100 all-time (SCORE_DESC)")
    p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("airing", help="shows airing now, reached from the top 100: one row each, at its best rank")
    p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("search", help="search anime by title")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("media", help="one anime with relations and the user's list entry")
    p.add_argument("anilist_id", type=int)
    p = sub.add_parser("progress", help="write progress to AniList and verify it")
    p.add_argument("anilist_id", type=int)
    p.add_argument("episode", type=int)
    p.add_argument("--dry-run", action="store_true",
                   help="print the mutation that would be sent and make no request")
    p.add_argument("--complete", action="store_true", help="mark COMPLETED instead of CURRENT")
    sub.add_parser("flush-progress", help="retry queued progress writes")
    # covers: the shell never fetches an image itself.
    p = sub.add_parser("covers", help="cover-image cache: covers warm|prune|status")
    p.add_argument("action", choices=["warm", "prune", "status"])
    p.add_argument("--view", choices=["watching", "top", "airing", "all"],
                   help="warm every cover of a view's payload (reads the same caches the list commands read)")
    p.add_argument("--media", nargs="*", default=[], metavar="ID",
                   help="warm these AniList media ids (whole numbers; another word is skipped and logged); the URLs "
                        "come from the local title cache, not from AniList")
    p.add_argument("--url", nargs="*", default=[], metavar="URL", help="warm these exact cover URLs")
    p.add_argument("--workers", type=int, default=covers.DEFAULT_WORKERS)
    p.add_argument("--budget", type=float, default=covers.DEFAULT_BUDGET_S, metavar="SECONDS")
    p.add_argument("--max-age-days", type=float, default=covers.PRUNE_MAX_AGE_DAYS)
    p.add_argument("--max-bytes", type=int, default=covers.PRUNE_MAX_BYTES)
    p = sub.add_parser("thumbs", help="derived-picture cache: thumbs prune|status")
    p.add_argument("action", choices=["prune", "status"])
    p.add_argument("--max-age-days", type=float, default=thumbsmod.PRUNE_MAX_AGE_DAYS)
    p.add_argument("--max-bytes", type=int, default=thumbsmod.PRUNE_MAX_BYTES)
    # subs: the side-load stage, exercisable without playing anything
    p = sub.add_parser("subs", help="side-loaded subtitles: subs find|fetch|external|prune|status")
    p.add_argument("action", choices=["find", "fetch", "external", "prune", "status"])
    p.add_argument("anilist_id", type=int, nargs="?")
    p.add_argument("episode", type=int, nargs="?")
    p.add_argument("--budget", type=float, default=subsmod.DONOR_BUDGET_S, metavar="SECONDS",
                   help="donor sweep budget")
    p.add_argument("--workers", type=int, default=subsmod.DONOR_WORKERS)
    p.add_argument("--offset", action="store_true",
                   help="with `find`: also estimate the constant offset of the best donor against the chosen release")
    p.add_argument("--max-age-days", type=float, default=subsmod.PRUNE_MAX_AGE_DAYS)
    p.add_argument("--max-bytes", type=int, default=None,
                   help="with `prune`: the size limit (default: subsCacheMaxBytes)")
    # external: the subtitle databases
    p.add_argument("--release-id", nargs=2, metavar=("INDEXER", "ID"),
                   help="with `external`: the playing release, as its indexer's name and that indexer's id; "
                        "its show ids (from that indexer add-on, when it offers them) come before ani.zip's")
    p.add_argument("--release", help="with `external`: the playing file's name, for ranking")
    p.add_argument("--lang", help="with `external`: ISO 639-2 code or a regional choice (`pt-BR`) "
                                  "(default: subtitleLanguage)")
    p.add_argument("--limit", type=int, default=10, help="with `external`: candidates listed")
    p.add_argument("--dry-run", action="store_true", help="with `external`: list what would load (the default)")
    p.add_argument("--fetch", action="store_true",
                   help="with `external`: download, time and place the best three (no player involved)")
    p.add_argument("--reference", metavar="FILE",
                   help="with `external --fetch|--load`: a subtitle file timed to the playing release")
    p.add_argument("--load", action="store_true",
                   help="with `external`: the worker a play starts — add the best three to the running player")
    p.add_argument("--info-hash", help=argparse.SUPPRESS)
    p.add_argument("--sid", help=argparse.SUPPRESS)
    p.add_argument("--reference-releases", help=argparse.SUPPRESS)
    p.add_argument("--nothing-on", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--donor-unverified", action="store_true", help=argparse.SUPPRESS)
    # tracing
    p = sub.add_parser("groupstats",
                       help="the learned per-group subtitle prior: groupstats report|backfill [--apply]")
    p.add_argument("action", choices=["report", "backfill"], nargs="?", default="report")
    p.add_argument("--apply", action="store_true",
                   help="write the rebuilt prior (default is a dry run that changes nothing)")

    p = sub.add_parser("trace", help="render one trace from the log as a timeline: trace <id|last>, or --tail")
    p.add_argument("id", nargs="?", default="last",
                   help=f"a trace id, or `last` (the default): the newest trace of {tracemod.STARTED}. It skips "
                        f"{tracemod.SKIPPED}")
    p.add_argument("--json", action="store_true", help="machine-readable timeline")
    p.add_argument("--tail", type=float, metavar="SECONDS", help="follow today's log for N seconds (0 = dump new lines once)")
    p = sub.add_parser("log-event", help="append one stage line under the current trace (used by the QML side)")
    p.add_argument("component")
    p.add_argument("stage")
    p.add_argument("--result", default="")
    p.add_argument("--level", default="info")
    p.add_argument("--data", default="{}", help="JSON object")
    args = ap.parse_args(argv)

    # Trace id: born at the user action. QML passes --trace; children inherit OMARCHY_ANIME_TRACE. The trace command
    # never borrows one: its `trace/last` line under a borrowed id would be that trace's newest line.
    borrowed = None if args.cmd == "trace" else (args.trace or os.environ.get("OMARCHY_ANIME_TRACE"))
    trace = borrowed or f"{args.cmd}-{int(time.time())}-{random.randrange(16**4):04x}"
    os.environ["OMARCHY_ANIME_TRACE"] = trace
    log = Log(trace)
    t0 = time.monotonic()
    # The trace command adds no `cli start`/`end` to the log it renders; `trace last` writes one `trace/last` line.
    observer = args.cmd == "trace"
    if not observer:
        log.info("cli", "start", cmd=args.cmd, argv=safe_argv(argv or sys.argv[1:]), fromEnv=from_env,
                 version=VERSION)
    try:
        settings = load_settings()
        report_setting_notes(log, CONFIG_DIR)
        if args.cmd == "notify-click":
            # The click's action, named by its token, runs here under the click's trace: its ids never reach an argv.
            words = notifymod.clicked(log, args.token)
            try:
                args = ap.parse_args(words)
            except SystemExit:
                raise StageError("notify.click", f"the CLI refused the click's words: {words!r}",
                                 "play the episode from Browse") from None
        if args.cmd == "resolve":
            out = do_resolve(args, log, settings, args.dry_run)
        elif args.cmd == "play":
            out = do_play(args, log, settings)
        elif args.cmd == "releases":
            out = do_releases(args, log, settings)
        elif args.cmd == "pin":
            out = do_pin(args, log)
        elif args.cmd == "picker-notice":
            out = do_picker_notice(args, log)
        elif args.cmd == "later":
            out = do_later(args, log)
        elif args.cmd == "keep-waiting":
            out = do_keep_waiting(args, log)
        elif args.cmd == "watch":
            out = watchermod.run(log, settings)
        elif args.cmd == "prefetch":
            out = do_prefetch(args, log, settings)
        elif args.cmd == "status":
            out = do_status(log)
        elif args.cmd == "player":
            out = do_player(args, log, settings)
        elif args.cmd == "tick":
            out = do_tick(args, log, settings)
        elif args.cmd == "rd-user":
            out = RealDebrid(Http(log, settings["httpTimeout"]), log, read_token("realdebrid")).user()
            out = {k: out.get(k) for k in ("username", "type", "expiration", "premium")}
        elif args.cmd == "torbox-user":
            u = TorBox(Http(log, settings["httpTimeout"]), log, read_token("torbox")).user()
            out = {k: u.get(k) for k in ("plan", "premium_expires_at", "total_downloaded", "is_subscribed")}
        elif args.cmd == "episodes":
            out = do_episodes(args, log, settings)
        elif args.cmd == "covers":
            out = do_covers(args, log, settings)
        elif args.cmd == "thumbs":
            out = do_thumbs(args, log, settings)
        elif args.cmd == "subs":
            out = do_subs(args, log, settings)
        elif args.cmd == "groupstats":
            out = do_groupstats(args, log, settings)
        elif args.cmd == "auth":
            out = do_auth(args, log, settings)
        elif args.cmd == "signout":
            out = do_signout(args, log, settings)
        elif args.cmd == "accounts":
            out = do_accounts(args, log, settings)
        elif args.cmd == "torznab":
            out = do_torznab(args, log, settings)
        elif args.cmd == "addons":
            out = do_addons(args, log, settings)
            if isinstance(out, str):                    # `spec` and `prompt`: the text itself, not JSON
                sys.stdout.write(out)
                sys.stdout.flush()
                log.info("cli", "end", result="PASS", ms=(time.monotonic() - t0) * 1000)
                return 0
            if out.get("ok") is False:                  # `check` that found a problem: its answer, and exit 1
                log.warn("cli", "end", result="FAIL", ms=(time.monotonic() - t0) * 1000)
                emit_json(out)
                return 1
        elif args.cmd in ("watching", "list", "top", "airing", "search", "media", "progress", "flush-progress"):
            out = do_anilist(args, log, settings)
        elif args.cmd == "trace":
            if args.tail is not None:
                for line in tracemod.tail(args.tail):
                    sys.stdout.write(home_shown(json_controls(redact_text(line))) + "\n")
                    sys.stdout.flush()
                return 0
            lines = tracemod.read_log_lines()
            pick = tracemod.pick_last([l for l in lines if l.get("trace") != trace]) if args.id == "last" else None
            report = tracemod.render(lines, (pick["trace"] if pick else args.id) or "")
            if not args.json:
                note = tracemod.last_note(pick) if pick else ""
                text = tracemod.format_text(report)
                sys.stdout.write(home_shown(escape_controls(redact_text(f"{note}\n{text}" if note else text))) + "\n")
                sys.stdout.flush()      # before the log line below, which `Log` also echoes on stderr
            if pick:
                # The one line the trace command writes, under its own trace (`trace._own_trace`).
                log.info(tracemod.PICK_COMPONENT, tracemod.PICK_STAGE, result="PASS" if pick["trace"] else "EMPTY",
                         ms=(time.monotonic() - t0) * 1000, picked=pick["trace"], rule=pick["rule"],
                         skipped=pick["skipped"], skipped_by=pick["skipped_by"], lines=len(lines))
            if not args.json:
                return 0
            # What a user is asked to paste: their home folder as `~` here too (`emit_json` keeps every other answer's
            # paths absolute, which the shell opens).
            out = json.loads(home_shown(json.dumps({**report, "last": pick} if pick else report, ensure_ascii=False)))
        elif args.cmd == "log-event":
            try:
                data = json.loads(args.data or "{}")
            except json.JSONDecodeError as exc:
                raise StageError("cli.log-event", f"--data is not JSON: {exc}")
            payload = dict(data) if isinstance(data, dict) else {"data": data}
            # `emit` takes level/component/stage/result/ms/suggest as named parameters, so
            # a payload key of the same name arrives twice and Python raises before anything is
            # written — which is how every play.failed event was lost. The caller's value describes
            # the event, not the log line, so it is kept under `event` rather than dropped or
            # allowed to overwrite the line's own field.
            clash = {k: payload.pop(k) for k in list(payload) if k in Log.RESERVED}
            if clash:
                payload["event"] = {**clash, **(payload.get("event") or {})}
            log.emit(args.level, args.component, args.stage, result=args.result, **payload)
            out = {"trace": trace, "logged": True}
        elif args.cmd == "anizip":
            m = anizip_mappings(Http(log, settings["httpTimeout"]), log, args.anilist_id)
            out = {"mappings": m["mappings"], "titles": m.get("titles"), "episodeCount": m.get("episodeCount")}
        else:
            raise StageError("cli", f"unknown command {args.cmd}")
        if not observer:
            log.info("cli", "end", result="PASS", ms=(time.monotonic() - t0) * 1000)
        emit_json(out)
        return 0
    except StageError as exc:
        return fail_stage(log, exc, t0, trace)
    except Exception as exc:  # noqa: BLE001 - last resort: the popout and the trace must still see a reason
        return fail_internal(log, exc, t0, trace)


